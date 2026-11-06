"""服务端帧缓存与 purge（task-4）的判据。

全部离线。这里钉的是缓存生命周期，不是网络：命中不重下、坏 meta 自愈、
并发两次采集不互删、启动清理只清自己该清的那一层。
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_frames_server_leg import FakeFetch, FakeGateway, cfg2, jpeg_bytes, sprite_meta

from video_workbench.core import frames as frames_core


def make_cache_tree(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    thumb = root / "thumb.jpg"
    thumb.write_bytes(bytes.fromhex("ffd8") + b"cover" + bytes.fromhex("ffd9"))
    return thumb


def set_age(path: Path, days: float):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_two_concurrent_collects_do_not_delete_each_others_files(tmp_path: Path, monkeypatch):
    """同一缓存目录两线程同时采集：两次都成功，且目录里的文件都还在。

    关键不是"两次都缓存命中"，而是并发写入时没有谁在收尾 rmtree 共享目录。
    强制两次都走接口，确保它们真的在同一个目录里写。
    """
    cache = tmp_path / "frames" / "BV1concurrent"
    cfg = cfg2()
    payload = jpeg_bytes()
    start = threading.Barrier(2)
    in_gateway = threading.Barrier(2)

    class BarrierGateway(FakeGateway):
        def get_data(self, url, params=None):
            in_gateway.wait(timeout=5)
            time.sleep(0.05)
            return super().get_data(url, params)

    gateway = BarrierGateway(sprite_meta())
    fetch = FakeFetch(payload)
    # 强制两边都先经过接口，防止其中一边提前命中缓存把并发窗口缩没。
    monkeypatch.setattr(frames_core, "_load_meta", lambda cache_dir: None)

    def worker():
        start.wait(timeout=5)
        return frames_core.collect_from_api(gateway, fetch, 123, cfg, cache_dir=cache)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert all(len(result.frames) == 2 for result in results)
    assert (cache / "meta.json").is_file()
    assert (cache / "0.jpg").is_file() and (cache / "1.jpg").is_file()


def test_a_transient_replace_denial_does_not_break_the_collect(tmp_path: Path, monkeypatch):
    """Windows 上并发写同一格会抛 WinError 5：一次失败不该让整次抽帧失败。

    这条是并发用例的确定性版本。那条靠两个线程抢同一个 `0.jpg`，实测约 2/5 会红、
    单跑又常常绿——红的时候看到的是 `PermissionError: [WinError 5] 拒绝访问`
    （目标位置上其实已经有一份完整内容）。这里把「抢输」这件事直接注入，
    逼着 `_replace_into_place` 的重试腿必须存在。
    """
    cache = tmp_path / "frames" / "123456"
    real = frames_core.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(5, "拒绝访问", str(dst))
        return real(src, dst)

    monkeypatch.setattr(frames_core.os, "replace", flaky)

    result = frames_core.collect_from_api(
        FakeGateway(sprite_meta()), FakeFetch(jpeg_bytes()), 123, cfg2(), cache_dir=cache
    )

    assert len(result.frames) == 2, "重试腿没接住一次拒绝，整次抽帧白跑了"
    assert (cache / "0.jpg").is_file() and (cache / "1.jpg").is_file()
def test_collect_from_api_cache_hit_keeps_all_cells(tmp_path: Path):
    cache = tmp_path / "frames" / "BV1cache"
    gateway = FakeGateway(sprite_meta())
    fetch = FakeFetch(jpeg_bytes())
    frames_core.collect_from_api(gateway, fetch, 123, cfg2(), cache_dir=cache)

    again = frames_core.collect_from_api(gateway, fetch, 123, cfg2(), cache_dir=cache)

    assert [frame.order for frame in again.frames] == [1, 2]
    assert len(gateway.calls) == 1
    assert len(fetch.urls) == 1
    assert json.loads((cache / "meta.json").read_text(encoding="utf-8"))["urls"]


def test_purge_days_zero_never_removes_anything(tmp_path: Path):
    root = tmp_path / "frames"
    old = root / "123456"
    old.mkdir(parents=True)
    set_age(old, 365)

    assert frames_core.purge_stale_frames(root, 0) == 0
    assert old.is_dir(), "days<=0 是「不自动清理」，不是「立刻清」"


def test_purge_removes_only_old_named_cache_dirs(tmp_path: Path):
    root = tmp_path / "frames"
    thumb = make_cache_tree(root)
    old_aid = root / "123456"
    old_bv = root / "BV1abcdefghij"
    fresh = root / "654321"
    unknown = root / "keep-me"
    for path in (old_aid, old_bv, fresh, unknown):
        path.mkdir()
    set_age(old_aid, 30)
    set_age(old_bv, 30)
    set_age(unknown, 30)

    removed = frames_core.purge_stale_frames(root, 7)

    assert removed == 2, f"只该清两个过期 cache 目录，实得 {removed}"
    assert not old_aid.exists() and not old_bv.exists()
    assert fresh.is_dir(), "未过期的 cache 目录被清了"
    assert unknown.is_dir(), "名字不像 bvid/aid 的目录被清了"
    assert thumb.is_file(), "frames/thumb.jpg（封面）被清了"


def test_purge_uses_the_newest_mtime_inside_a_cache_dir(tmp_path: Path):
    root = tmp_path / "frames"
    root.mkdir(parents=True)
    cache = root / "123456"
    cache.mkdir()
    fresh_child = cache / "0.jpg"
    fresh_child.write_bytes(b"just-written")
    set_age(cache, 30)
    # 目录自己的 mtime 是旧的，但里面刚写过文件：不能按目录 mtime 误删。
    os.utime(fresh_child, None)

    assert frames_core.purge_stale_frames(root, 7) == 0
    assert cache.is_dir()
    assert fresh_child.is_file()


def test_purge_ignores_files_and_unknown_dirs(tmp_path: Path):
    root = tmp_path / "frames"
    root.mkdir(parents=True)
    loose = root / "loose.jpg"
    loose.write_bytes(b"not a cache dir")
    other = root / "summary-1234"
    other.mkdir()
    set_age(other, 365)

    assert frames_core.purge_stale_frames(root, 1) == 0
    assert loose.is_file() and other.is_dir()


def test_purge_missing_root_is_a_noop(tmp_path: Path):
    assert frames_core.purge_stale_frames(tmp_path / "does-not-exist", 7) == 0
