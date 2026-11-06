"""退出竞态：连接关掉之后还来写库，不许把任务线程带着 traceback 打死。

全量回归里出现过一次（`tests/wb/test_clip_batch.py` 的线程 `wb-job-clip.export-58b2`）：

```
WBError: [PROCESS_FAILED] ffmpeg.EXE 执行失败（exit 3199971767）
  → jobs.py _run 的 except → _finish
  → sqlite3.ProgrammingError: Cannot operate on a closed database
```

机制：`JobManager.shutdown()` 的 join 有 2 秒超时，超时后线程仍然活着，而
`WorkbenchService.close()` 紧接着把连接关了。于是「失败原因」这条最该留下的信息
反而写不进去，线程以 unhandled exception 收场（pytest 报成一条 Warning）。

三条判据各守一层，都不依赖真实耗时/真实子进程：

1. **`Repo` 自己认得「已关」**：关掉之后再写库抛 `RepoClosed`，而不是底层的
   `sqlite3.ProgrammingError`——调用方才有东西可以 catch。
2. **`_finish` 吞下这一支并把损失说出来**：状态写不进库是既定语义（下次开机由
   `job_mark_interrupted` 标成 INTERRUPTED），但日志里必须有一句「收尾没写进库」，
   否则用户看到的就是「任务消失」。
3. **`shutdown()` 会把在跑任务的子进程杀掉**：只置 cancel event 时，handler 卡在
   `for line in proc.stdout` 上根本醒不过来，join 每次都超时——那才是这条竞态的
   真实触发条件。

本文件不发任何真请求、不起任何真子进程：`kill_tree` 用记账替身。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.events import EventBus
from video_workbench.app.jobs import JobManager
from video_workbench.infra.db import open_and_migrate
from video_workbench.infra.repo import Repo, RepoClosed


@pytest.fixture
def repo(tmp_path: Path):
    """真库（临时目录），用例结束关掉。"""
    conn = open_and_migrate(tmp_path / "wb.db")
    r = Repo(conn)
    try:
        yield r
    finally:
        r.close()


def _manager(repo: Repo, lines: list[str] | None = None) -> JobManager:
    # 注意别写成 `(lines or []).append`：空列表是假值，那样会 append 到另一个新列表上，
    # 断言拿到的是空——判据就成了「什么都没测」。
    logger = lines.append if lines is not None else None
    return JobManager(repo, EventBus(), default_limit=1, logger=logger)


# ------------------------------------------------------- 第一层：Repo 认得已关


def test_a_write_after_close_raises_the_typed_error_not_sqlite_internals(repo: Repo) -> None:
    """关掉之后写库抛 `RepoClosed`。

    抛底层异常时，调用方要么 catch 一个它本来不该知道的 sqlite 类型，要么什么都不 catch；
    两者都会把「程序正在退出」这件正常的事变成一条 traceback。
    """
    job = repo.job_create("job_x", "demo", None, {})
    repo.close()

    with pytest.raises(RepoClosed):
        repo.job_update(job["id"], status="failed")
    with pytest.raises(RepoClosed):
        repo.job_get(job["id"])


def test_close_is_idempotent_and_reports_its_state(repo: Repo) -> None:
    """关两次不炸；`closed` 能让调用方先问再写。"""
    assert repo.closed is False
    repo.close()
    repo.close()
    assert repo.closed is True


# ------------------------------------------------------- 第二层：_finish 吞下这一支


def test_a_finish_after_close_logs_the_loss_instead_of_raising(repo: Repo) -> None:
    """连接已关时收尾不抛，但必须在日志里说出「状态没写进库」。

    静默吞掉等于让任务在界面上凭空消失；抛出去等于把退出路径变成 traceback。
    唯一正确的第三种做法是把损失写进人看得见的地方。
    """
    lines: list[str] = []
    manager = _manager(repo, lines)
    job = repo.job_create("job_y", "demo", None, {})
    repo.close()

    manager._finish(job["id"], "failed", {"code": "INTERNAL", "message": "任务失败了"})

    said = [ln for ln in lines if "收尾没写进库" in ln]
    assert said, f"收尾没写进库这件事没被说出来，日志只有：{lines}"
    assert "job_y" in said[0], f"日志里没带任务 id：{said[0]}"
    assert "failed" in said[0] and "INTERNAL" in said[0], f"日志里没带状态与错误码：{said[0]}"


def test_a_progress_report_after_close_is_dropped_without_raising(repo: Repo) -> None:
    """进度写不进库就丢掉：真值在事件里，不值得让 handler 变成 INTERNAL 失败。"""
    manager = _manager(repo)
    job = repo.job_create("job_z", "demo", None, {})
    repo.close()

    manager._report(job["id"], 0.5, "导出中")  # 不抛即通过


def test_submit_after_close_says_the_program_is_exiting(repo: Repo) -> None:
    """`_closed` 检查通过之后连接才被关掉——这条竞态的另一半，对界面是同一句话。"""
    manager = _manager(repo)
    manager.register("demo", lambda ctx, payload: None)
    repo.close()

    with pytest.raises(WBError) as caught:
        manager.submit("demo", None, {})
    assert caught.value.code == "CONFLICT"
    assert "正在退出" in caught.value.message


# ------------------------------------------------------- 第三层：shutdown 杀子进程


def test_shutdown_kills_the_child_processes_of_a_running_job(repo: Repo, monkeypatch) -> None:
    """`shutdown()` 必须杀子进程，否则 handler 卡在 readline 上永远醒不过来。

    这条判据同时证明两件事：kill_tree 真被叫到了（pid 对上），以及线程真的走完了
    收尾腿——终态写进了库（`canceled`）。后者正是「连接还没关」的证明：如果 shutdown
    只置 event 不杀进程，这个 join 就会超时，测试结束时连接才关，收尾腿写库必炸。
    """
    killed: list[int] = []
    monkeypatch.setattr("video_workbench.core.downloader.kill_tree", killed.append)

    started = threading.Event()

    class FakeProc:
        pid = 4321

        def poll(self) -> None:
            return None

    def handler(ctx, payload):
        ctx.register_process(FakeProc())
        started.set()
        while not ctx.should_cancel():
            time.sleep(0.02)
        raise WBError("CANCELED", "任务已取消")

    manager = _manager(repo)
    manager.register("demo", handler, limit=1)
    job = manager.submit("demo", None, {})
    assert started.wait(timeout=5), "handler 没起来"

    manager.shutdown()

    assert killed == [4321], f"shutdown 没杀在跑任务的子进程，kill_tree 收到：{killed}"
    assert manager.get(job["id"])["status"] == "canceled", "收尾没写进库：线程没走完 _finish"


def test_a_real_service_close_leaves_the_repo_closed_and_is_idempotent(cfg_path: Path) -> None:
    """真后端关一次就够了；再关一次不许炸（退出路径会被调两次）。"""
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=str(cfg_path), picker=None)
    svc.close()

    assert svc.repo.closed is True
    svc.close()
