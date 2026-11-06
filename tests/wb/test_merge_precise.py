# -*- coding: utf-8 -*-
"""P4-03：精确合并（filter_complex concat）、进度映射、失败切精确、取消清理。

假 FFmpeg 只验「我们怎么指挥它」；真滤镜像不像样、成片能不能播在
`scripts/accept_p4_precise.py` 里用真 ffmpeg 验。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

import pytest  # noqa: E402

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.core import merger  # noqa: E402
from video_workbench.infra.ffmpeg import MediaInfo  # noqa: E402


class FakeFFmpeg:
    """按文件名给规格：带 `hd` 的 1920×1080，其余 1280×720；带 `silent` 的没音轨。"""

    def __init__(
        self,
        *,
        duration_ms: int = 4000,
        copy_error: str = "",
        precise_error: str = "",
        precise_cancel: bool = False,
    ) -> None:
        self.duration_ms = duration_ms
        self.copy_error = copy_error
        self.precise_error = precise_error
        self.precise_cancel = precise_cancel
        self.copy_calls: list[tuple[Path, Path]] = []
        self.precise_calls: list[dict] = []

    def probe(self, path):
        target = str(path)
        name = Path(target).name
        hd = "hd" in name
        return MediaInfo(
            duration_ms=self.duration_ms,
            width=1920 if hd else 1280,
            height=1080 if hd else 720,
            fps=30.0 if hd else 25.0,
            vcodec="h264",
            acodec="" if "silent" in name else "aac",
            has_video=True,
            has_audio="silent" not in name,
            bytes=1_000_000,
            pix_fmt="yuv420p",
            sample_rate=0 if "silent" in name else 44100,
            channels=0 if "silent" in name else 2,
        )

    def merge_copy(self, list_path, dest, **kwargs):
        self.copy_calls.append((Path(list_path), Path(dest)))
        if self.copy_error:
            raise WBError("PROCESS_FAILED", self.copy_error, "fake stderr")
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-quick-mp4")
        return target

    def merge_precise(self, **kwargs):
        self.precise_calls.append(
            {
                "inputs": list(kwargs.get("inputs") or []),
                "dest": Path(kwargs.get("dest")),
                "plan": kwargs.get("plan") or {},
                "segments_ms": list(kwargs.get("segments_ms") or []),
                "expected_ms": int(kwargs.get("expected_ms") or 0),
                "on_progress": kwargs.get("on_progress"),
            }
        )
        target = Path(kwargs.get("dest"))
        report = kwargs.get("on_progress")
        if report:
            # 走一遍进度回调：证明「已完成文件数 + 当前百分比」真的接到了任务进度上
            segs = list(kwargs.get("segments_ms") or [])
            for micros in (0, 2_000_000, 999_000_000):
                report(
                    merger.merge_progress(
                        micros, segs, expected_ms=int(kwargs.get("expected_ms") or 0)
                    )
                )
        if self.precise_cancel:
            raise WBError("CANCELED", "已取消合并")
        if self.precise_error:
            raise WBError("PROCESS_FAILED", self.precise_error, "fake stderr")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-precise-mp4" * 100)
        return target


def make_files(tmp_path: Path, names: list[str]) -> list[str]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x" * 256)
        paths.append(str(path))
    return paths


def use_ffmpeg(service, monkeypatch, fake) -> None:
    monkeypatch.setattr(service, "_ffmpeg", fake)


def rpc(service, method, params=None):
    return service.router.dispatch(method, params or {})


def error_of(out):
    assert out.get("ok") is False, out
    return out["error"]


def wait_job(service, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 没有在 {timeout}s 内结束")


# ------------------------------------------------------------------ 滤镜与命令


def test_plan_pads_and_normalizes_every_input():
    plan = merger.precise_plan(
        [
            {"path": "a.mp4", "has_audio": True, "duration_ms": 4000},
            {"path": "b.mp4", "has_audio": True, "duration_ms": 6000},
        ],
        width=1080,
        height=1920,
        fps=30,
    )
    text = plan["filter"]
    assert plan["width"] == 1080 and plan["height"] == 1920
    assert "force_original_aspect_ratio=decrease" in text
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2" in text
    assert "fps=30" in text and "format=yuv420p" in text
    assert "aresample=48000" in text
    assert text.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]")


def test_plan_keeps_even_dimensions():
    plan = merger.precise_plan(
        [{"has_audio": True, "duration_ms": 1000}, {"has_audio": True, "duration_ms": 1000}],
        width=1081,
        height=607,
        fps=25,
    )
    assert (plan["width"], plan["height"]) == (1080, 606)


def test_plan_fills_silence_for_video_without_audio():
    plan = merger.precise_plan(
        [
            {"path": "a.mp4", "has_audio": True, "duration_ms": 4000},
            {"path": "b-silent.mp4", "has_audio": False, "duration_ms": 6500},
        ],
        width=1280,
        height=720,
        fps=30,
    )
    assert plan["silence"] == [6.5]
    # 没有音轨的那路不能写 [1:a]（流不存在不是静音），要走补进来的 anullsrc
    assert "[1:a]" not in plan["filter"]
    assert "[2:a]atrim=0:6.5" in plan["filter"]


def test_plan_needs_two_items():
    with pytest.raises(WBError) as box:
        merger.precise_plan([{"has_audio": True, "duration_ms": 1000}])
    assert box.value.code == "VALIDATION"


def test_command_order_and_progress_flag():
    plan = merger.precise_plan(
        [
            {"has_audio": True, "duration_ms": 4000},
            {"has_audio": False, "duration_ms": 5000},
        ],
        width=1280,
        height=720,
        fps=30,
    )
    args = merger.precise_command(
        "ffmpeg", inputs=["a.mp4", "b.mp4"], dest="out.mp4", plan=plan, crf=20, preset="fast"
    )
    assert args[0] == "ffmpeg"
    assert args.count("-i") == 3  # 两个真实输入 + 一个 anullsrc
    assert args[args.index("-filter_complex") + 1] == plan["filter"]
    assert args[args.index("-map") + 1] == "[outv]"
    assert args[args.index("-crf") + 1] == "20"
    assert args[args.index("-preset") + 1] == "fast"
    assert args[args.index("-progress") + 1] == "pipe:1"
    assert args[-1] == "out.mp4"


# ------------------------------------------------------------------ 进度


def test_parse_progress_line():
    assert merger.parse_progress_line("out_time_us=123456\n") == {"out_time_us": "123456"}
    assert merger.parse_progress_line("progress=continue") == {"progress": "continue"}
    assert merger.parse_progress_line("") == {}
    assert merger.parse_progress_line("没有等号") == {}


def test_merge_progress_splits_by_file():
    third = merger.merge_progress(5_000_000, [4000, 6000], expected_ms=10000)
    assert third["index"] == 2 and third["total"] == 2
    assert third["done"] == 1
    assert third["file_fraction"] == pytest.approx(1 / 6, abs=1e-6)
    assert third["fraction"] == pytest.approx(0.5, abs=1e-6)
    assert merger.progress_text(third) == "第 2/2 段 17%"


def test_merge_progress_at_end():
    last = merger.merge_progress(10_000_000, [4000, 6000], expected_ms=10000)
    assert last["index"] == 2 and last["fraction"] == 1.0
    assert last["file_fraction"] == 1.0


def test_merge_progress_with_zero_span():
    # 时长为 0 的片段（探测残缺）不能除零，也不能卡住进度
    out = merger.merge_progress(1_000_000, [0, 5000], expected_ms=0)
    assert out["total"] == 2
    assert 0.0 <= out["fraction"] <= 1.0


# ------------------------------------------------------------------ 服务层


def test_precise_mode_is_accepted_now(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["hd.mp4", "narrow.mp4"])
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(service, "merge.start", {"paths": paths, "mode": "precise", "out_dir": str(tmp_path)})
    assert out["ok"] is True, out
    job = wait_job(service, out["data"]["job"]["id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["mode"] == "precise"
    assert job["result"]["fallback_used"] is False
    assert Path(job["result"]["path"]).is_file()
    assert job["result"]["delta_ms"] == job["result"]["duration_ms"] - job["result"]["expected_ms"]


def test_precise_plan_is_built_from_preflight_target(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["hd.mp4", "narrow.mp4"])
    fake = FakeFFmpeg()
    use_ffmpeg(service, monkeypatch, fake)
    out = rpc(service, "merge.start", {"paths": paths, "mode": "precise", "out_dir": str(tmp_path)})
    wait_job(service, out["data"]["job"]["id"])
    call = fake.precise_calls[-1]
    plan = call["plan"]
    # 目标规格取第一个文件（hd 是 1920×1080/30fps）
    assert (plan["width"], plan["height"]) == (1920, 1080)
    assert plan["fps"] == 30
    assert call["segments_ms"] == [4000, 4000]
    assert call["expected_ms"] == 8000


def test_quick_failure_falls_back_to_precise(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    fake = FakeFFmpeg(copy_error="时间戳断层")
    use_ffmpeg(service, monkeypatch, fake)
    out = rpc(service, "merge.start", {"paths": paths, "mode": "quick", "out_dir": str(tmp_path)})
    assert out["ok"] is True, out
    job = wait_job(service, out["data"]["job"]["id"])
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["fallback_used"] is True
    assert result["mode"] == "precise"
    assert result["requested_mode"] == "quick"
    assert result["note"] == "快速拼接失败，已切精确重编码"
    assert fake.copy_calls and fake.precise_calls
    # 记录跟着改成 precise，并标上 fallback
    row = service.repo.merge_get(out["data"]["merge_id"])
    assert row["mode"] == "precise" and row["fallback_used"] is True
    assert row["status"] == "succeeded"


def test_fallback_only_once(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    fake = FakeFFmpeg(copy_error="坏了", precise_error="还是坏")
    use_ffmpeg(service, monkeypatch, fake)
    out = rpc(service, "merge.start", {"paths": paths, "mode": "quick", "out_dir": str(tmp_path)})
    job = wait_job(service, out["data"]["job"]["id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "PROCESS_FAILED"
    # 只切一次：精确那条只跑了一遍，没有第三次
    assert len(fake.precise_calls) == 1


def test_cancel_marks_record_and_leaves_no_output(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    fake = FakeFFmpeg(precise_cancel=True)
    use_ffmpeg(service, monkeypatch, fake)
    out = rpc(service, "merge.start", {"paths": paths, "mode": "precise", "out_dir": str(tmp_path)})
    target = Path(out["data"]["output"])
    job = wait_job(service, out["data"]["job"]["id"])
    assert job["status"] == "canceled", job
    assert not target.is_file()
    row = service.repo.merge_get(out["data"]["merge_id"])
    assert row["status"] == "canceled"


def test_temp_dir_is_cleaned_after_job(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(copy_error="x"))
    out = rpc(service, "merge.start", {"paths": paths, "mode": "quick", "out_dir": str(tmp_path)})
    job = wait_job(service, out["data"]["job"]["id"])
    tmp_root = Path(str(service.cfg.cache_root)) / "tmp"
    leftovers = [p for p in tmp_root.iterdir()] if tmp_root.is_dir() else []
    assert [p for p in leftovers if p.name == job["id"]] == []


def test_multiple_outputs_do_not_collide(service, tmp_path, monkeypatch):
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    first = rpc(
        service, "merge.start", {"paths": paths, "mode": "precise", "out_dir": str(tmp_path)}
    )
    wait_job(service, first["data"]["job"]["id"])
    second = rpc(
        service, "merge.start", {"paths": paths, "mode": "precise", "out_dir": str(tmp_path)}
    )
    wait_job(service, second["data"]["job"]["id"])
    assert first["data"]["name"] != second["data"]["name"]


# ------------------------------------------------------------------ 取消路径（真实进程）


class _SlowChild:
    """最小可杀进程：`python -c sleep`，用来验证取消时真的把它杀了。"""

    def __init__(self, seconds: float = 30.0) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def test_reap_kills_process_that_ignores_taskkill():
    from video_workbench.infra.ffmpeg import FFmpeg

    child = _SlowChild(30.0)
    try:
        started = time.monotonic()
        # 故意不调 kill_tree：模拟 taskkill 静默失败（pid 传错就是这么发生的）
        FFmpeg()._reap(child.proc, grace=0.4)
        elapsed = time.monotonic() - started
        assert child.proc.poll() is not None, "进程还活着"
        assert elapsed < 10, f"_reap 用了 {elapsed:.1f}s，太久了"
    finally:
        if child.proc.poll() is None:
            child.proc.kill()


def test_kill_tree_needs_pid_not_popen():
    """回归：kill_tree 收 pid。传 Popen 对象时 taskkill 静默失败、取消变成摆设。"""
    import inspect

    from video_workbench.core.downloader import kill_tree

    params = list(inspect.signature(kill_tree).parameters)
    assert params == ["pid"], params
    child = _SlowChild(30.0)
    try:
        kill_tree(child.proc.pid)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and child.proc.poll() is None:
            time.sleep(0.1)
        assert child.proc.poll() is not None, "kill_tree 没杀掉"
    finally:
        if child.proc.poll() is None:
            child.proc.kill()


def test_stale_tmp_dirs_are_swept_on_start(tmp_path):
    """崩过一次之后 cache/tmp 里会攒下 job_xxx：开机时清掉，不留假现场。"""
    from video_workbench.app.service import WorkbenchService
    from video_workbench.config import WorkbenchConfig

    cache = tmp_path / "cache"
    stale = cache / "tmp" / "job_deadbeef"
    stale.mkdir(parents=True)
    (stale / "concat.txt").write_text("file 'x'\n", encoding="utf-8")
    keep = cache / "keep.txt"
    keep.write_text("x", encoding="utf-8")

    cfg_path = tmp_path / "config.yaml"
    cfg = WorkbenchConfig()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    cfg.storage.cache_root = str(cache)
    cfg.storage.media_root = str(tmp_path / "media")
    cfg.storage.models_root = str(tmp_path / "models")
    cfg.storage.ffmpeg_dir = str(tmp_path / "bin")
    cfg.save(str(cfg_path))

    service = WorkbenchService(config_path=str(cfg_path), picker=None)
    try:
        assert service.tmp_swept == ["job_deadbeef"]
        assert not stale.exists()
        assert keep.is_file(), "不该顺手删 cache_root 里的别的东西"
    finally:
        service.close()
