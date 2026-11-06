"""服务端 aid 抽帧腿（task-4）的判据。

全部离线：gateway 是假对象，fetch_bytes 是假函数；不碰真网络、不起 GUI、不调模型。
要钉四类边界：

1. `fetch_sprite` 的 URL/大小/重定向守卫；
2. `collect_from_api` 的 aid 接口、裁格、缓存命中与坏 meta 自愈；
3. 服务层「本地腿优先、本地失败才走服务端、两腿都坏只降级」；
4. aid 从 bvid 走现有 Resolver，不新写解析。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from video_workbench.config import FramesConfig
from video_workbench.core import frames as frames_core


def jpeg_bytes(color=(200, 30, 30), size=(100, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def sprite_meta(*, urls=None, index=None, grid_x=2, grid_y=1, cw=50, ch=50):
    return {
        "image": urls or ["https://i0.hdslb.com/bfs/sprite.jpg"],
        "index": index if index is not None else [0, 30],
        "img_x_len": grid_x,
        "img_y_len": grid_y,
        "img_x_size": cw,
        "img_y_size": ch,
    }


class FakeGateway:
    def __init__(self, data=None, error: Exception | None = None):
        self.data = data
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def get_data(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if self.error is not None:
            raise self.error
        return self.data


class FakeFetch:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self.payload


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=64 * 1024):
        yield from self._chunks

    def close(self):
        self.closed = True


class Ctx:
    def __init__(self):
        self.logs: list[str] = []
        self.steps: list[tuple] = []

    def log(self, line):
        self.logs.append(line)

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.steps.append((value, stage))

    def should_cancel(self):
        return False

    def check_cancel(self):
        return None


def make_project(service, tmp_path: Path, *, bvid=None, aid=None, source=False):
    root = tmp_path / "media" / "UP" / "server-leg"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="服务端抽帧",
        up_name="UP",
        root_dir=str(root),
        stage="transcribed",
        bvid=bvid,
        aid=aid,
    )
    if source:
        path = root / "source.mp4"
        path.write_bytes(b"not a real video")
        service.repo.asset_upsert_singleton(
            project["id"], "source", str(path), bytes=2048, duration_ms=120_000
        )
    return project


def cfg2(quality: int = 85) -> FramesConfig:
    return FramesConfig(enabled=True, max_frames=2, quality=quality)


# ------------------------------------------------------------------ fetch_sprite


@pytest.mark.parametrize(
    "url",
    [
        "http://i0.hdslb.com/bfs/sprite.jpg",
        "https://evil.com/bfs/sprite.jpg",
        "https://evil.com/?x=https://i0.hdslb.com/bfs/sprite.jpg",
        "https://i0.hdslb.com@evil.com/bfs/sprite.jpg",
        "https://i0.hdslb.com:443/bfs/sprite.jpg",
        "https://i0.hdslb.com./bfs/sprite.jpg",
        "https://i0.hdslb.com.evil.com/bfs/sprite.jpg",
    ],
)
def test_fetch_sprite_rejects_untrusted_urls(monkeypatch, url):
    def no_network(*args, **kwargs):
        raise AssertionError(f"不该发网络请求：{args!r} {kwargs!r}")

    monkeypatch.setattr(frames_core.requests, "get", no_network)
    with pytest.raises(frames_core.FrameError):
        frames_core.fetch_sprite(url)


def test_fetch_sprite_returns_bytes_for_a_whitelisted_subdomain(monkeypatch):
    payload = bytes.fromhex("ffd8") + b"sprite" + bytes.fromhex("ffd9")
    response = FakeResponse(headers={"Content-Length": str(len(payload))}, chunks=[payload])
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return response

    monkeypatch.setattr(frames_core.requests, "get", fake_get)
    assert frames_core.fetch_sprite("https://i0.hdslb.com/bfs/sprite.jpg") == payload
    assert seen["kwargs"]["allow_redirects"] is False
    assert seen["kwargs"]["stream"] is True
    assert response.closed is True


def test_fetch_sprite_refuses_redirects(monkeypatch):
    response = FakeResponse(status=302, headers={"Location": "https://evil.com/x"})
    monkeypatch.setattr(frames_core.requests, "get", lambda *a, **k: response)
    with pytest.raises(frames_core.FrameError, match="重定向"):
        frames_core.fetch_sprite("https://i0.hdslb.com/bfs/sprite.jpg")
    assert response.closed is True


def test_fetch_sprite_refuses_declared_oversize(monkeypatch):
    response = FakeResponse(headers={"Content-Length": "999"}, chunks=[b"x"])
    monkeypatch.setattr(frames_core.requests, "get", lambda *a, **k: response)
    with pytest.raises(frames_core.FrameError, match="大小上限"):
        frames_core.fetch_sprite("https://i0.hdslb.com/bfs/sprite.jpg", max_bytes=10)


def test_fetch_sprite_refuses_streamed_oversize_without_content_length(monkeypatch):
    response = FakeResponse(headers={}, chunks=[b"1234", b"5678"])
    monkeypatch.setattr(frames_core.requests, "get", lambda *a, **k: response)
    with pytest.raises(frames_core.FrameError, match="大小上限"):
        frames_core.fetch_sprite("https://i0.hdslb.com/bfs/sprite.jpg", max_bytes=6)


# -------------------------------------------------------------- collect_from_api


def test_collect_from_api_calls_videoshot_by_aid_and_crops_cells(tmp_path: Path):
    cfg = cfg2()
    gateway = FakeGateway(sprite_meta())
    fetch = FakeFetch(jpeg_bytes())
    cache = tmp_path / "frames" / "BV1serverleg"

    frameset = frames_core.collect_from_api(gateway, fetch, 123456, cfg, cache_dir=cache)

    assert len(frameset.frames) == 2
    assert frameset.total_cells == 2
    assert frameset.sprites == 1
    assert frameset.has_timestamps is True
    assert "videoshot" in frameset.notes[0]
    assert gateway.calls[0][0].endswith("/x/player/videoshot")
    assert gateway.calls[0][1] == {"aid": 123456, "index": 1}
    assert [f.timestamp for f in frameset.frames] == [0, 30]
    assert fetch.urls == ["https://i0.hdslb.com/bfs/sprite.jpg"]
    assert (cache / "meta.json").is_file()
    for cell in (0, 1):
        assert (cache / f"{cell}.jpg").is_file()


def test_collect_from_api_hits_cache_without_a_second_request(tmp_path: Path):
    cfg = cfg2()
    gateway = FakeGateway(sprite_meta())
    fetch = FakeFetch(jpeg_bytes())
    cache = tmp_path / "frames" / "BV1serverleg"
    first = frames_core.collect_from_api(gateway, fetch, 123456, cfg, cache_dir=cache)
    progress: list[str] = []

    second = frames_core.collect_from_api(
        gateway, fetch, 123456, cfg, cache_dir=cache, progress=progress.append
    )

    assert len(second.frames) == len(first.frames) == 2
    assert len(gateway.calls) == 1, "缓存命中还去调了 videoshot"
    assert len(fetch.urls) == 1, "缓存命中还去下载了拼图"
    assert any("缓存" in line for line in progress), progress


def test_collect_from_api_refetches_when_meta_is_corrupt(tmp_path: Path):
    cfg = cfg2()
    cache = tmp_path / "frames" / "BV1serverleg"
    cache.mkdir(parents=True)
    (cache / "meta.json").write_text('{"max_frames": "not-an-int"}', encoding="utf-8")
    gateway = FakeGateway(sprite_meta())
    fetch = FakeFetch(jpeg_bytes())

    frameset = frames_core.collect_from_api(gateway, fetch, 123456, cfg, cache_dir=cache)

    assert len(frameset.frames) == 2
    assert len(gateway.calls) == 1, "坏 meta 应该重新请求，而不是把异常抛给总结链路"
    assert len(fetch.urls) == 1


def test_collect_from_api_refetches_when_quality_changes(tmp_path: Path):
    cache = tmp_path / "frames" / "BV1serverleg"
    gateway = FakeGateway(sprite_meta())
    fetch = FakeFetch(jpeg_bytes())
    frames_core.collect_from_api(gateway, fetch, 123456, cfg2(85), cache_dir=cache)

    frameset = frames_core.collect_from_api(gateway, fetch, 123456, cfg2(70), cache_dir=cache)

    assert len(frameset.frames) == 2
    assert len(gateway.calls) == 2, "quality 变了还按旧 meta 命中"
    assert len(fetch.urls) == 2, "quality 变了还复用了旧单格"


def test_collect_from_api_keeps_shared_cache_dir_after_return(tmp_path: Path):
    cache = tmp_path / "frames" / "BV1serverleg"
    frames_core.collect_from_api(
        FakeGateway(sprite_meta()), FakeFetch(jpeg_bytes()), 123456, cfg2(), cache_dir=cache
    )
    assert cache.is_dir(), "服务端腿不能在任务收尾删共享缓存目录"
    assert json.loads((cache / "meta.json").read_text(encoding="utf-8"))["max_frames"] == 2


# --------------------------------------------------------------- service layer


def test_service_prefers_local_leg_and_never_touches_the_network(service, monkeypatch, tmp_path):
    from test_summary_frames import RecordingFFmpeg

    project = make_project(service, tmp_path, bvid="BV1serverleg", aid=123456, source=True)
    service.cfg.collect.frames.max_frames = 2
    service._ffmpeg = RecordingFFmpeg(duration_ms=120_000)
    gateway = FakeGateway(error=AssertionError("本地腿成功时不该访问网关"))
    service._gateway = gateway
    ctx = Ctx()

    frameset = service._summary_frameset(ctx, project["id"])

    assert frameset is not None and len(frameset.frames) == 2
    assert gateway.calls == []
    assert not any("服务端" in line for line in ctx.logs), ctx.logs


def test_service_falls_back_to_server_leg_when_local_ffmpeg_fails(service, monkeypatch, tmp_path):
    from test_summary_frames import RecordingFFmpeg

    project = make_project(service, tmp_path, bvid="BV1serverleg", aid=123456, source=True)
    service.cfg.collect.frames.max_frames = 2
    service._ffmpeg = RecordingFFmpeg(can_thumb=False, duration_ms=120_000)
    gateway = FakeGateway(sprite_meta())
    service._gateway = gateway
    fetch = FakeFetch(jpeg_bytes())
    monkeypatch.setattr(frames_core, "fetch_sprite", fetch)
    ctx = Ctx()

    frameset = service._summary_frameset(ctx, project["id"])

    assert frameset is not None and len(frameset.frames) == 2
    assert gateway.calls[0][1]["aid"] == 123456
    assert any("本地抽帧失败" in line for line in ctx.logs), ctx.logs
    assert any("服务端" in line for line in ctx.logs), ctx.logs


def test_service_returns_none_when_both_legs_fail(service, monkeypatch, tmp_path):
    project = make_project(service, tmp_path, bvid="BV1serverleg", aid=123456)
    service.cfg.collect.frames.max_frames = 2
    service._gateway = FakeGateway(error=RuntimeError("接口挂了"))
    monkeypatch.setattr(frames_core, "fetch_sprite", FakeFetch(jpeg_bytes()))
    ctx = Ctx()

    frameset = service._summary_frameset(ctx, project["id"])

    assert frameset is None
    assert any("未附画面" in line for line in ctx.logs), ctx.logs
    assert any("服务端抽帧失败" in line for line in ctx.logs), ctx.logs


def test_service_without_bvid_does_not_touch_the_network(service, monkeypatch, tmp_path):
    project = make_project(service, tmp_path)
    service.cfg.collect.frames.max_frames = 2
    gateway = FakeGateway(error=AssertionError("没有 bvid 不该访问网关"))
    service._gateway = gateway
    ctx = Ctx()

    frameset = service._summary_frameset(ctx, project["id"])

    assert frameset is None
    assert gateway.calls == []
    assert any("缺少 bvid" in line for line in ctx.logs), ctx.logs


def test_service_resolves_aid_from_bvid_via_existing_resolver(service, monkeypatch, tmp_path):
    project = make_project(service, tmp_path, bvid="BV1serverleg")
    service.cfg.collect.frames.max_frames = 2
    gateway = FakeGateway(sprite_meta())
    service._gateway = gateway
    calls = []

    class FakeResolver:
        def resolve(self, raw):
            calls.append(raw)
            return SimpleNamespace(items=[SimpleNamespace(aid=777)])

    service._resolver = FakeResolver()
    monkeypatch.setattr(frames_core, "fetch_sprite", FakeFetch(jpeg_bytes()))
    ctx = Ctx()

    frameset = service._summary_frameset(ctx, project["id"])

    assert frameset is not None and len(frameset.frames) == 2
    assert calls == ["BV1serverleg"]
    assert gateway.calls[0][1]["aid"] == 777
