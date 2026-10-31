"""P3-04 切点编辑：帧对齐、帧长来源、锁定保护、手动切片。

这里的验收目标是**切点误差 ≤ 1 帧**。帧长只有片源知道，所以：

1. `clip.candidates` 必须把 `frame_ms` 交给界面（±1 帧按钮、检查器文案都用它）；
2. 写库时要能把切点对齐到帧栅格（`align="frame"`），而不是让界面各算一次。

不碰 ffmpeg：探帧率走 `FFmpeg.probe`，测试里用假 MediaInfo 顶替，
不为了测一个除法去真的解码视频。
"""

from __future__ import annotations

from pathlib import Path

import pytest


class FakeInfo:
    """`FFmpeg.probe` 的最小替身：切片逻辑只读 fps。"""

    def __init__(self, fps: float) -> None:
        self.fps = fps
        self.width = 1280
        self.height = 720
        self.duration_ms = 60_000


def _project(service, *, with_source=True):
    root = Path(service.cfg.media_root) / "editcase"
    root.mkdir(parents=True, exist_ok=True)
    row = service.repo.project_create(
        title="切点编辑用例",
        source_kind="local",
        root_dir=str(root),
    )
    service.repo.transcript_upsert(
        row["id"],
        language="zh",
        model="stub",
        segments=[
            {"id": "s1", "start_ms": 0, "end_ms": 2_000, "text": "第一句"},
            {"id": "s2", "start_ms": 2_000, "end_ms": 40_000, "text": "第二句"},
            {"id": "s3", "start_ms": 40_000, "end_ms": 59_000, "text": "第三句"},
        ],
    )
    if with_source:
        media = root / "source.mp4"
        media.write_bytes(b"fake-mp4")
        service.repo.asset_upsert(row["id"], "source", str(media), bytes=8)
    return row


def _candidate(service, project_id, *, start=10_000, end=30_000):
    return service.repo.candidate_insert(
        project_id,
        kind="highlight",
        start_ms=start,
        end_ms=end,
        score=80,
        title="一条候选",
        source="ai",
        notes={"origin": "llm"},
    )


def call(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok"), out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert not out.get("ok"), out
    return out["error"]["code"]


def _fps(service, fps):
    """把探到的帧率钉死，避免依赖本机有没有装 ffmpeg。"""
    service._ffmpeg.probe = lambda _path: FakeInfo(fps)  # noqa: SLF001 - 测试替身


# --------------------------------------------------------------------- 帧长来源


def test_frame_ms_comes_from_probe(service):
    row = _project(service)
    _fps(service, 25.0)
    data = call(service, "clip.candidates", {"id": row["id"]})
    assert data["frame_ms"] == 40
    assert data["fps"] == pytest.approx(25.0)


def test_frame_ms_for_30fps(service):
    row = _project(service)
    _fps(service, 30.0)
    data = call(service, "clip.candidates", {"id": row["id"]})
    assert data["frame_ms"] == 33  # 1000/30 四舍五入
    assert data["fps"] == pytest.approx(1000 / 33, rel=1e-3)


def test_frame_ms_falls_back_without_source(service):
    row = _project(service, with_source=False)
    data = call(service, "clip.candidates", {"id": row["id"]})
    assert data["frame_ms"] == 40, "没有源文件时按 25fps 估算，并让界面照实写"


def test_frame_ms_survives_probe_failure(service):
    row = _project(service)

    def boom(_path):
        raise RuntimeError("ffprobe 不在")

    service._ffmpeg.probe = boom  # noqa: SLF001
    data = call(service, "clip.candidates", {"id": row["id"]})
    assert data["frame_ms"] == 40, "探测失败不能把整个界面打挂"


def test_frame_ms_ignores_absurd_fps(service):
    row = _project(service)
    _fps(service, 0.0)
    assert call(service, "clip.candidates", {"id": row["id"]})["frame_ms"] == 40


# --------------------------------------------------------------------- 帧对齐


def test_update_align_frame_snaps_both_edges(service):
    row = _project(service)
    _fps(service, 25.0)
    cand = _candidate(service, row["id"])
    data = call(
        service,
        "clip.update",
        {"id": cand["id"], "start_ms": 10_013, "end_ms": 20_027, "align": "frame"},
    )
    got = data["candidate"]
    assert got["start_ms"] == 10_000, got
    assert got["end_ms"] == 20_040, got


def test_update_align_error_within_half_frame(service):
    """验收口径：误差 ≤ 一帧（这里实际做到半帧，四舍五入的极限）。"""
    row = _project(service)
    _fps(service, 25.0)
    cand = _candidate(service, row["id"])
    frame = 40
    for start in (10_001, 10_019, 10_021, 10_039, 12_345, 33_333):
        data = call(
            service,
            "clip.update",
            {"id": cand["id"], "start_ms": start, "end_ms": 40_000, "align": "frame"},
        )
        got = data["candidate"]["start_ms"]
        assert abs(got - start) <= frame / 2, f"{start} → {got} 超过半帧"
        assert got % frame == 0, f"{got} 不在帧栅格上"


def test_update_without_align_keeps_raw_ms(service):
    """手动敲时间码的场景：用户写多少就存多少，不做悄悄对齐。"""
    row = _project(service)
    _fps(service, 25.0)
    cand = _candidate(service, row["id"])
    data = call(service, "clip.update", {"id": cand["id"], "start_ms": 10_013, "end_ms": 20_027})
    assert data["candidate"]["start_ms"] == 10_013
    assert data["candidate"]["end_ms"] == 20_027


def test_update_align_rejects_unknown_mode(service):
    row = _project(service)
    cand = _candidate(service, row["id"])
    assert call.__name__  # 保持 linter 安静
    assert err(service, "clip.update", {"id": cand["id"], "start_ms": 10_000, "align": "sample"}) == "VALIDATION"


def test_update_align_then_clamp_to_duration(service):
    """对齐之后仍要夹回片长——先对齐后夹取的顺序不能反。"""
    row = _project(service)
    _fps(service, 25.0)
    cand = _candidate(service, row["id"])
    data = call(
        service,
        "clip.update",
        {"id": cand["id"], "start_ms": 58_000, "end_ms": 59_999, "align": "frame"},
    )
    got = data["candidate"]
    assert got["end_ms"] <= 59_000, got  # 片长来自字幕末尾（没有真视频时长）
    assert got["end_ms"] % 40 == 0


def test_create_align_frame(service):
    row = _project(service)
    _fps(service, 25.0)
    data = call(
        service,
        "clip.create",
        {
            "project_id": row["id"],
            "kind": "segment",
            "start_ms": 5_013,
            "end_ms": 15_027,
            "title": "手切",
            "align": "frame",
        },
    )
    got = data["candidate"]
    assert (got["start_ms"], got["end_ms"]) == (5_000, 15_040)
    assert got["source"] == "manual"
    assert (got.get("notes") or {}).get("manual") is True


def test_create_align_rejects_unknown_mode(service):
    row = _project(service)
    assert err(
        service,
        "clip.create",
        {"project_id": row["id"], "start_ms": 0, "end_ms": 5_000, "align": "vad"},
    ) == "VALIDATION"


# --------------------------------------------------------------------- 锁定与手动切片


def test_locked_candidate_keeps_cut_while_others_can_move(service):
    row = _project(service)
    _fps(service, 25.0)
    locked = _candidate(service, row["id"], start=10_000, end=20_000)
    call(service, "clip.update", {"id": locked["id"], "status": "locked"})
    call(
        service,
        "clip.update",
        {"id": locked["id"], "start_ms": 12_000, "end_ms": 20_000, "align": "frame"},
    )
    got = service.repo.candidate_get(locked["id"])
    assert got["status"] == "locked", "对齐一下就把锁解了，锁等于没有"
    assert got["start_ms"] == 12_000


def test_locked_candidate_survives_reanalyze(service):
    """锁定后重新生成不覆盖（P3-04 验收项）。"""
    row = _project(service)
    cand = _candidate(service, row["id"], start=3_000, end=9_000)
    call(service, "clip.update", {"id": cand["id"], "status": "locked"})
    cleared = service.repo.candidate_clear(row["id"])
    assert cleared == 0, f"清掉了 {cleared} 条不该清的"
    assert service.repo.candidate_get(cand["id"])["status"] == "locked"


def test_manual_clip_is_exportable_shape(service):
    """手动切片要能进导出：时长 ≥ 0.4s、有起止、source=manual（P3-05 直接读这三样）。"""
    row = _project(service)
    _fps(service, 25.0)
    got = call(
        service,
        "clip.create",
        {"project_id": row["id"], "start_ms": 8_013, "end_ms": 10_100, "align": "frame"},
    )
    assert got["candidate"]["end_ms"] - got["candidate"]["start_ms"] >= 400


def test_manual_clip_short_is_rejected_after_align(service):
    """对齐后不足 0.4 秒要报错，不能靠对齐把非法区间变成合法。"""
    row = _project(service)
    _fps(service, 25.0)
    assert err(
        service,
        "clip.create",
        {"project_id": row["id"], "start_ms": 8_013, "end_ms": 8_100, "align": "frame"},
    ) == "VALIDATION"


def test_edit_marks_notes_but_align_alone_does_not(service):
    """只对齐不改值，不该被记成「手动改过切点」。"""
    row = _project(service)
    _fps(service, 25.0)
    cand = _candidate(service, row["id"], start=10_000, end=20_000)
    kept = call(
        service,
        "clip.update",
        {"id": cand["id"], "start_ms": 10_000, "end_ms": 20_000, "align": "frame"},
    )["candidate"]
    assert not (kept.get("notes") or {}).get("edited_ms")

    # 10.500s 对齐到 10.520s，值真的变了才该留痕
    moved = call(
        service,
        "clip.update",
        {"id": cand["id"], "start_ms": 10_500, "end_ms": 20_000, "align": "frame"},
    )["candidate"]
    assert (moved.get("notes") or {}).get("edited_ms") is True
