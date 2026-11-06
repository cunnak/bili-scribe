"""字幕编辑 RPC 的装配测试（P2-03）。

这里验的是"一条 RPC = 一个编辑意图"这条边界：服务端算完、落三处、回全量。
纯函数本身在 ``test_subtitles.py`` 里单测，这里只盯接线和落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

from video_workbench.core.subtitles import normalize


def make_project(service, tmp_path: Path, *, segments=None) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="transcribed"
    )
    items = normalize(
        segments
        or [
            {"start_ms": 0, "end_ms": 1200, "text": "第一句"},
            {"start_ms": 1200, "end_ms": 2600, "text": "第二句"},
            {"start_ms": 2600, "end_ms": 4000, "text": "第三句"},
        ]
    )
    (root / "transcript.json").write_text(
        json.dumps({"segments": items, "language": "zh", "model": "tiny"}, ensure_ascii=False),
        encoding="utf-8",
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=items,
        language="zh",
        model="tiny",
        srt_path=str(root / "transcript.srt"),
    )
    return project


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def test_get_returns_segments_qc_stats(service, tmp_path):
    project = make_project(service, tmp_path)
    data = rpc(service, "subtitle.get", {"project_id": project["id"]})
    assert data["has_transcript"] is True
    assert len(data["segments"]) == 3
    assert data["segments"][0]["text"] == "第一句"
    assert data["stats"]["count"] == 3
    assert isinstance(data["qc"], list)


def test_get_without_transcript_is_empty_not_error(service, tmp_path):
    project = service.repo.project_create(
        title="空", root_dir=str(tmp_path / "none"), stage="downloaded"
    )
    data = rpc(service, "subtitle.get", {"project_id": project["id"]})
    assert data["has_transcript"] is False
    assert data["segments"] == []


def test_update_writes_three_places(service, tmp_path):
    project = make_project(service, tmp_path)
    seg_id = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"][1]["id"]
    data = rpc(
        service,
        "subtitle.op",
        {"project_id": project["id"], "op": "update", "segment_id": seg_id, "text": "改过的"},
    )
    assert data["segments"][1]["text"] == "改过的"

    root = Path(project["root_dir"])
    payload = json.loads((root / "transcript.json").read_text(encoding="utf-8"))
    assert payload["segments"][1]["text"] == "改过的"
    srt = (root / "transcript.srt").read_text(encoding="utf-8")
    assert "改过的" in srt and "00:00:01,200" in srt
    row = service.repo.transcript_get(project["id"])
    assert row["segments"][1]["text"] == "改过的"


def test_split_keeps_two_segments_and_grows_count(service, tmp_path):
    project = make_project(service, tmp_path)
    first = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"][0]
    data = rpc(
        service,
        "subtitle.op",
        {"project_id": project["id"], "op": "split", "segment_id": first["id"], "at_ms": 600},
    )
    assert len(data["segments"]) == 4
    assert data["segments"][0]["end_ms"] == 600
    assert data["segments"][1]["start_ms"] == 600


def test_merge_requires_adjacent(service, tmp_path):
    project = make_project(service, tmp_path)
    items = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"]

    # 先试不合法的：第 1、3 段中间夹着第 2 段，必须整单拒绝而不是悄悄吞掉中间那段
    bad = service.dispatch(
        "subtitle.op",
        {
            "project_id": project["id"],
            "op": "merge",
            "segment_ids": [items[0]["id"], items[2]["id"]],
        },
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "VALIDATION"
    assert len(rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"]) == 3

    ok = rpc(
        service,
        "subtitle.op",
        {
            "project_id": project["id"],
            "op": "merge",
            "segment_ids": [items[0]["id"], items[1]["id"]],
        },
    )
    assert len(ok["segments"]) == 2
    assert "第一句" in ok["segments"][0]["text"]
    assert ok["segments"][0]["end_ms"] == 2600


def test_shift_moves_only_selected(service, tmp_path):
    project = make_project(service, tmp_path)
    items = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"]
    data = rpc(
        service,
        "subtitle.op",
        {
            "project_id": project["id"],
            "op": "shift",
            "offset_ms": 500,
            "segment_ids": [items[0]["id"]],
        },
    )
    assert data["segments"][0]["start_ms"] == 500
    assert data["segments"][1]["start_ms"] == 1200


def test_replace_reports_hits(service, tmp_path):
    project = make_project(service, tmp_path)
    data = rpc(
        service,
        "subtitle.op",
        {
            "project_id": project["id"],
            "op": "replace",
            "pattern": "第(.)句",
            "replacement": r"第\1行",
            "regex": True,
        },
    )
    assert data["hits"] == 3
    assert data["segments"][0]["text"] == "第一行"


def test_replace_without_hit_keeps_everything(service, tmp_path):
    project = make_project(service, tmp_path)
    before = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"]
    data = rpc(
        service,
        "subtitle.op",
        {"project_id": project["id"], "op": "replace", "pattern": "找不到", "replacement": "x"},
    )
    assert data["hits"] == 0
    assert [s["id"] for s in data["segments"]] == [s["id"] for s in before]


def test_delete_and_insert(service, tmp_path):
    project = make_project(service, tmp_path)
    items = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"]
    data = rpc(
        service,
        "subtitle.op",
        {"project_id": project["id"], "op": "delete", "segment_ids": [items[1]["id"]]},
    )
    assert len(data["segments"]) == 2
    data = rpc(
        service,
        "subtitle.op",
        {
            "project_id": project["id"],
            "op": "insert",
            "segment": {"start_ms": 1000, "end_ms": 1800, "text": "插入的"},
        },
    )
    assert [s["text"] for s in data["segments"]] == ["第一句", "插入的", "第三句"]


def test_unknown_segment_is_not_found(service, tmp_path):
    project = make_project(service, tmp_path)
    out = service.dispatch(
        "subtitle.op",
        {"project_id": project["id"], "op": "update", "segment_id": "nope", "text": "x"},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"


def test_unknown_op_is_validation(service, tmp_path):
    project = make_project(service, tmp_path)
    out = service.dispatch("subtitle.op", {"project_id": project["id"], "op": "explode"})
    assert out["ok"] is False
    assert out["error"]["code"] == "VALIDATION"


def test_save_whole_transcript(service, tmp_path):
    project = make_project(service, tmp_path)
    items = rpc(service, "subtitle.get", {"project_id": project["id"]})["segments"]
    items[0]["text"] = "整份保存"
    data = rpc(service, "subtitle.save", {"project_id": project["id"], "segments": items})
    assert data["count"] == 3
    root = Path(project["root_dir"])
    assert "整份保存" in (root / "transcript.srt").read_text(encoding="utf-8")


def test_save_empty_is_rejected(service, tmp_path):
    project = make_project(service, tmp_path)
    out = service.dispatch("subtitle.save", {"project_id": project["id"], "segments": []})
    assert out["ok"] is False
    assert out["error"]["code"] == "VALIDATION"


def test_op_without_transcript_is_not_found(service, tmp_path):
    project = service.repo.project_create(
        title="空", root_dir=str(tmp_path / "none"), stage="downloaded"
    )
    out = service.dispatch(
        "subtitle.op", {"project_id": project["id"], "op": "shift", "offset_ms": 100}
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"
