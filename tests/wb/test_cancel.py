"""P5-01：取消要真杀进程、临时目录要清、失败要留日志。

这一组测试的价值不在于"代码跑得通"，而在于它们会**真的起子进程**：
用一个故意睡很久的 python 进程冒充 ffmpeg / yt-dlp / 转录 worker，
然后断言它在取消后的几秒内真的没了。用 mock 很容易写出"取消了但进程还在"的假绿。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.events import EventBus
from video_workbench.app.jobs import JobManager
from video_workbench.infra.db import open_and_migrate
from video_workbench.infra.ffmpeg import FFmpeg
from video_workbench.infra.repo import Repo

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _sleep_script(tmp_path: Path, seconds: int = 30) -> list[str]:
    """写一个能被打断的脚本：它自己睡，不读写任何文件。"""
    body = tmp_path / f"sleep_{seconds}.py"
    body.write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")
    return [sys.executable, str(body)]


def _alive(pid: int) -> bool:
    """Windows 上判断进程是否还在：拿不到句柄就是没了。"""
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return str(pid) in (out.stdout or "")


# ------------------------------------------------------------------ FFmpeg.run

def test_run_returns_stdout(tmp_path):
    ff = FFmpeg(timeout=10)
    out = ff.run([sys.executable, "-c", "print('hello')"])
    assert out.strip() == "hello"


def test_run_reports_nonzero_with_tail(tmp_path):
    ff = FFmpeg(timeout=10)
    with pytest.raises(WBError) as caught:
        ff.run([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    assert caught.value.code == "PROCESS_FAILED"
    assert "exit 3" in caught.value.message
    assert "boom" in caught.value.detail


def test_run_cancel_kills_process_tree(tmp_path):
    """取消要在亚秒级返回，并且那个进程真的死掉。"""
    ff = FFmpeg(timeout=60)
    started: list[int] = []

    def should_cancel() -> bool:
        return bool(started) and time.monotonic() - t0 > 0.4

    t0 = time.monotonic()
    with pytest.raises(WBError) as caught:
        ff.run(_sleep_script(tmp_path), should_cancel=should_cancel,
               on_proc=lambda proc: started.append(proc.pid))
    elapsed = time.monotonic() - t0

    assert caught.value.code == "CANCELED"
    assert elapsed < 10, f"取消花了 {elapsed:.1f} 秒，说明是在等命令自己跑完"
    assert started, "on_proc 没被调用"
    time.sleep(0.5)
    assert not _alive(started[0]), f"取消后进程 {started[0]} 还活着"


def test_run_timeout_kills_and_reports(tmp_path):
    ff = FFmpeg(timeout=60)
    t0 = time.monotonic()
    with pytest.raises(WBError) as caught:
        ff.run(_sleep_script(tmp_path), timeout=0.5)
    assert caught.value.code == "PROCESS_FAILED"
    assert "超时" in caught.value.message
    assert time.monotonic() - t0 < 10


# ------------------------------------------------------------------ JobManager

@pytest.fixture()
def manager(tmp_path):
    conn = open_and_migrate(tmp_path / "wb.db")
    repo = Repo(conn)
    events = EventBus()
    m = JobManager(repo, events, default_limit=2, tmp_root=tmp_path / "tmp")
    yield m, repo, tmp_path
    m.shutdown()
    conn.close()


def test_cancel_kills_registered_child(manager, tmp_path):
    """handler 卡在读进程输出上时，光置位事件是退不出来的——必须从外面杀。"""
    m, repo, _ = manager
    seen: dict[str, int] = {}

    def handler(ctx, payload):
        proc = subprocess.Popen(
            _sleep_script(tmp_path, 30),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ctx.register_process(proc)
        ctx.log("子进程起来了")
        # 故意不看取消标志：模拟"卡在等输出"的 handler
        proc.communicate()
        seen["pid"] = proc.pid

    m.register("test.stuck", handler, limit=1)
    job = m.submit("test.stuck", None, {})
    time.sleep(1.0)
    m.cancel(job["id"])
    deadline = time.time() + 12
    row: dict = {}
    while time.time() < deadline:
        row = m.get(job["id"])
        if row["status"] in ("canceled", "failed", "succeeded"):
            break
        time.sleep(0.05)
    assert row["status"] in ("canceled", "failed"), row


def test_failed_job_keeps_log_tail(manager):
    m, repo, _ = manager

    def handler(ctx, payload):
        ctx.log("第 1 行")
        ctx.log("第 2 行：要炸了")
        raise WBError("PROCESS_FAILED", "故意失败")

    m.register("test.fail", handler, limit=1)
    job = m.submit("test.fail", None, {})
    deadline = time.time() + 8
    row = {}
    while time.time() < deadline:
        row = m.get(job["id"])
        if row["status"] in ("failed", "canceled", "succeeded"):
            break
        time.sleep(0.05)
    assert row["status"] == "failed"
    assert row["result"]["log"] == ["第 1 行", "第 2 行：要炸了"]
    assert row["error_message"] == "故意失败"


def test_succeeded_job_does_not_store_log(manager):
    m, repo, _ = manager

    def handler(ctx, payload):
        ctx.log("这行不该进库")
        return {"ok": True}

    m.register("test.ok", handler, limit=1)
    job = m.submit("test.ok", None, {})
    deadline = time.time() + 8
    row = {}
    while time.time() < deadline:
        row = m.get(job["id"])
        if row["status"] in ("failed", "canceled", "succeeded"):
            break
        time.sleep(0.05)
    assert row["status"] == "succeeded"
    assert row["result"] == {"ok": True}, "成功任务不该背着日志"


def test_finish_cleans_job_tmp_dir(manager):
    m, repo, tmp_path = manager

    def handler(ctx, payload):
        assert m.tmp_root is not None
        work = m.tmp_root / ctx.job_id
        work.mkdir(parents=True, exist_ok=True)
        (work / "concat.txt").write_text("file 'x.mp4'\n", encoding="utf-8")
        return {"dir": str(work)}

    m.register("test.tmp", handler, limit=1)
    job = m.submit("test.tmp", None, {})
    deadline = time.time() + 8
    row = {}
    while time.time() < deadline:
        row = m.get(job["id"])
        if row["status"] in ("failed", "canceled", "succeeded"):
            break
        time.sleep(0.05)
    assert row["status"] == "succeeded"
    assert not Path(row["result"]["dir"]).exists(), "任务收尾没删自己的临时目录"
