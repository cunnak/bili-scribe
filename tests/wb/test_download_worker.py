"""下载 worker 测试（P1-02 / P1-03 的编排部分）。

这里不打桩"下载"这个动作本身——``core/downloader`` 已有自己的测试。
这里只验收编排：**目录建对了吗、侧车写全了吗、重复触发会不会重下、
失败会不会写回项目状态。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core.downloader import DownloadResult
from video_workbench.core.layout import SIDECAR, SIDECAR_KEYS, read_sidecar, write_sidecar, allocate, build_sidecar
from video_workbench.workers import download_worker as W


class FakeCtx:
    """最小 JobContext 替身：只记事件，不做别的事。"""

    def __init__(self, cancel: bool = False):
        self.events: list[tuple] = []
        self.logs: list[str] = []
        self.canceled = cancel

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.events.append((value, stage, speed, eta))

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        return self.canceled

    def check_cancel(self):
        if self.canceled:
            raise WBError("CANCELED", "任务已取消")


def make_downloader(service, *, reuse_calls=None, result_path=None, error=None):
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        home = Path(kwargs["home"])
        target = home / "source.mp4"
        target.write_bytes(b"x" * (W.MIN_REUSE_BYTES + 1))
        for event in (
            {"type": "progress", "percent": 10.0, "speed": 1024 * 1024, "eta": 65},
            {"type": "progress", "percent": 100.0},
        ):
            kwargs["on_event"](event)
        return DownloadResult(path=target, height=1080, format_id="137+140", quality_requested="1080P")

    if reuse_calls is not None:
        reuse_calls.extend(calls)
    return run, calls


def payload(bvid="BV1xx411c7mD", **extra):
    body = {
        "item": {
            "bvid": bvid,
            "cid": 1001,
            "page": 1,
            "title": "第一讲 · 引论",
            "part_title": "",
            "aid": 42,
        },
        "up_name": "示例UP",
        "collection_title": "方法论合集",
        "quality": "1080P",
    }
    body.update(extra)
    return body


def test_download_creates_layout_sidecar_and_asset(service, tmp_path):
    run, calls = make_downloader(service)
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    ctx = FakeCtx()
    result = handler(ctx, payload())

    # 目录结构：{media_root}/{UP}/{合集}/{标题}/
    root = Path(result["path"]).parent
    assert root.parent.name == "方法论合集"
    assert root.parent.parent.name == "示例UP"
    for sub in ("frames", "clips", "export", "logs"):
        assert (root / sub).is_dir(), f"缺少子目录 {sub}"

    sidecar = read_sidecar(root / SIDECAR)
    assert set(SIDECAR_KEYS) <= set(sidecar), f"侧车缺字段：{set(SIDECAR_KEYS) - set(sidecar)}"
    assert sidecar["bvid"] == "BV1xx411c7mD"
    assert sidecar["cid"] == 1001
    assert sidecar["quality_requested"] == "1080P"
    assert sidecar["quality_actual"] == "1080P"

    project = service.repo.project_get(result["project_id"])
    assert project["stage"] == "downloaded"
    assert project["status"] == "ready"
    assets = service.repo.asset_list(result["project_id"])
    assert len(assets) == 1 and assets[0]["kind"] == "source"

    assert calls[0]["url"].endswith("/BV1xx411c7mD"), "单P 不该带 ?p=1"
    assert calls[0]["quality"] == "1080P"


def test_reuse_skips_second_download(service, tmp_path):
    run, calls = make_downloader(service)
    media = tmp_path / "media"
    handler = W.make_download_handler(repo=service.repo, media_root=media, downloader=run)

    first = handler(FakeCtx(), payload())
    assert Path(first["path"]).is_file()

    second = handler(FakeCtx(), payload())
    assert second["reused"] is True, "同一集重复提交必须复用"
    assert len(calls) == 1, "第二次不该真的下载"
    assert second["project_id"] == first["project_id"], "同一集不该产生第二个项目"


def test_truncated_file_is_redownloaded(service, tmp_path):
    run, calls = make_downloader(service)
    media = tmp_path / "media"
    handler = W.make_download_handler(repo=service.repo, media_root=media, downloader=run)

    first = handler(FakeCtx(), payload())
    Path(first["path"]).write_bytes(b"short")  # 模拟上次下到一半
    second = handler(FakeCtx(), payload())
    assert second["reused"] is False
    assert len(calls) == 2


def test_multi_part_gets_part_layer(service, tmp_path):
    run, calls = make_downloader(service)
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    body = payload(page_count=3)
    body["item"]["page"] = 2
    body["item"]["part_title"] = "第二讲"
    result = handler(FakeCtx(), body)
    root = Path(result["path"]).parent
    assert "第二讲" in root.name, f"分P 应建子层：{root}"
    assert calls[0]["url"].endswith("?p=2")


def test_failure_marks_project_failed(service, tmp_path):
    run, _ = make_downloader(service, error=WBError("AUTH_REQUIRED", "需要大会员"))
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    with pytest.raises(WBError):
        handler(FakeCtx(), payload())
    rows = service.repo.project_list("", 10, 0)
    assert rows and rows[0]["status"] == "failed"
    assert rows[0]["error"], "失败一定要留原因，否则列表里只是红一下"


def test_cancel_before_start_aborts(service, tmp_path):
    run, calls = make_downloader(service)
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    with pytest.raises(WBError) as info:
        handler(FakeCtx(cancel=True), payload())
    assert info.value.code == "CANCELED"
    assert calls == [], "取消后不该起下载进程"


def test_progress_is_mapped_and_throttled(service, tmp_path):
    run, _ = make_downloader(service)
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    ctx = FakeCtx()
    handler(ctx, payload())
    values = [v for v, _stage, _s, _e in ctx.events if v is not None]
    assert values[0] == pytest.approx(0.02, abs=0.01)
    assert values[-1] == pytest.approx(1.0), "结束必须是 1.0"
    assert all(0.0 <= v <= 1.0 for v in values), f"进度越界：{values}"
    assert any(stage and "下载中" in stage for _v, stage, _s, _e in ctx.events)
    assert len(ctx.events) < 12, "进度事件要节流，否则 SSE 刷屏"


def test_cookie_provider_failure_is_not_fatal(service, tmp_path):
    run, calls = make_downloader(service)

    def boom():
        raise RuntimeError("key.bin 坏了")

    handler = W.make_download_handler(
        repo=service.repo, media_root=tmp_path / "media", downloader=run, cookie_provider=boom
    )
    ctx = FakeCtx()
    handler(ctx, payload())
    assert calls[0]["cookie_file"] is None
    assert any("cookie" in line for line in ctx.logs)


def test_probe_populates_asset_fields(service, tmp_path):
    run, _ = make_downloader(service)

    class Info:
        duration_ms = 12345
        width = 1920
        height = 1080
        fps = 29.97
        vcodec = "h264"
        acodec = "aac"

    handler = W.make_download_handler(
        repo=service.repo, media_root=tmp_path / "media", downloader=run, prober=lambda _p: Info()
    )
    result = handler(FakeCtx(), payload())
    asset = service.repo.asset_list(result["project_id"])[0]
    assert asset["duration_ms"] == 12345 and asset["width"] == 1920 and asset["vcodec"] == "h264"


def test_watch_url():
    assert W.watch_url("BV1xx411c7mD") == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert W.watch_url("BV1xx411c7mD", 3).endswith("?p=3")


def test_missing_bvid_is_validation_error(service, tmp_path):
    run, _ = make_downloader(service)
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    with pytest.raises(WBError) as info:
        handler(FakeCtx(), {"item": {}})
    assert info.value.code == "VALIDATION"


def test_thumb_is_recorded_as_asset(service, tmp_path):
    """下载完要顺手抽一张封面：列表页没有它也能用，但有了才认得出是哪个视频。"""
    run, _ = make_downloader(service)
    media = tmp_path / "media"

    def thumbs(video: Path, root: str) -> Path:
        dest = Path(root) / "frames" / "thumb.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return dest

    handler = W.make_download_handler(repo=service.repo, media_root=media, downloader=run, thumbs=thumbs)
    result = handler(FakeCtx(), payload())
    kinds = {row["kind"]: row for row in service.repo.asset_list(result["project_id"])}
    assert "thumb" in kinds
    assert Path(kinds["thumb"]["path"]).is_file()


def test_thumb_failure_does_not_fail_download(service, tmp_path):
    """抽帧炸了不能让下载判负——用户要的是视频，不是封面。"""
    run, _ = make_downloader(service)

    def thumbs(video: Path, root: str):
        raise RuntimeError("ffmpeg 罢工")

    ctx = FakeCtx()
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run, thumbs=thumbs)
    result = handler(ctx, payload())
    project = service.repo.project_get(result["project_id"])
    assert project["stage"] == "downloaded"
    assert any("thumb" in line for line in ctx.logs)


def test_no_thumb_injection_is_fine(service, tmp_path):
    """没注抽帧器（比如没装 ffmpeg 的环境）照常跑完，只是少一张图。"""
    run, _ = make_downloader(service)
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=run)
    result = handler(FakeCtx(), payload())
    kinds = {row["kind"] for row in service.repo.asset_list(result["project_id"])}
    assert "source" in kinds and "thumb" not in kinds
