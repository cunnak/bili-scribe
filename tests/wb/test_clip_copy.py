"""文案与封面（P3-08 服务层）。

三条约定：

1. 已有文案不重烧请求（除非显式 regenerate）——白跑一次模型换一个"差不多"的标题没意义。
2. 重写文案不动已挑好的封面（用户挑封面花的力气比点一下"重新生成"贵）。
3. 抽帧失败只记一笔，不能让文案任务整体失败。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from video_workbench.core import copywriter, cover

GOOD = {
    "title": "定价不是成本加法",
    "description": "同一款产品把价格翻三倍，这段讲清定价靠的是判断而不是算账。",
    "tags": ["定价策略", "商业判断", "产品思维"],
}


def _project(service):
    row = service.repo.project_create(
        title="文案用例",
        source_kind="local",
        root_dir=str(Path(service.cfg.media_root) / "copycase"),
    )
    service.repo.transcript_upsert(
        row["id"],
        language="zh",
        model="stub",
        segments=[
            {"id": "s1", "start_ms": 0, "end_ms": 10_000, "text": "先说第一句"},
            {"id": "s2", "start_ms": 10_000, "end_ms": 20_000, "text": "再说第二句"},
            {"id": "s3", "start_ms": 20_000, "end_ms": 30_000, "text": "最后一句"},
        ],
    )
    return row


def _candidate(service, project, **fields):
    base = {
        "kind": "highlight",
        "start_ms": 0,
        "end_ms": 20_000,
        "score": 80,
        "title": "片段标题",
        "reason": "理由",
        "quote": "先说第一句",
    }
    base.update(fields)
    return service.repo.candidate_insert(project["id"], **base)


def call(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok"), out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert not out.get("ok"), out
    return out["error"]["code"]


def wait_job(service, job_id, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务没在 {timeout}s 内结束：{service.jobs.get(job_id)}")


class Stub:
    model = "stub-model"

    def __init__(self, *replies):
        self.replies = list(replies)

    def chat(self, messages, **kwargs):
        item = self.replies.pop(0) if self.replies else RuntimeError("没有更多回复")
        if isinstance(item, Exception):
            raise item
        return item


def _stub_client(service, *replies):
    stub = Stub(*replies)
    service._clip_client = lambda: (stub, "stub-model")  # type: ignore[assignment]
    return stub


def test_copy_rejects_unknown_candidate(service):
    assert err(service, "clip.copy", {"candidate_id": "cand_nope"}) == "NOT_FOUND"


def test_copy_runs_job_and_stores_copy(service):
    project = _project(service)
    candidate = _candidate(service, project)
    _stub_client(service, json.dumps(GOOD, ensure_ascii=False))
    started = call(service, "clip.copy", {"candidate_id": candidate["id"]})
    job = wait_job(service, started["job"]["id"])
    assert job["status"] == "succeeded", job
    row = service.repo.candidate_get(candidate["id"])
    assert row["copy"]["title"] == GOOD["title"]
    assert row["copy"]["source"] == "llm"
    assert row["copy"]["edited"] is False


def test_copy_reuses_existing_without_burning_request(service):
    project = _project(service)
    candidate = _candidate(service, project, copy={"title": "已有标题", "tags": ["甲", "乙", "丙"]})
    stub = _stub_client(service, json.dumps(GOOD, ensure_ascii=False))
    out = call(service, "clip.copy", {"candidate_id": candidate["id"]})
    assert out["reused"] is True
    assert out["copy"]["title"] == "已有标题"
    assert stub.replies  # 一次都没消费 = 没发请求


def test_regenerate_overwrites_copy(service):
    project = _project(service)
    candidate = _candidate(service, project, copy={"title": "旧标题", "tags": ["旧"]})
    _stub_client(service, json.dumps(GOOD, ensure_ascii=False))
    started = call(service, "clip.copy", {"candidate_id": candidate["id"], "regenerate": True})
    wait_job(service, started["job"]["id"])
    assert service.repo.candidate_get(candidate["id"])["copy"]["title"] == GOOD["title"]


def test_regenerate_keeps_existing_cover(service):
    project = _project(service)
    picked = {"path": "C:/keep/cover.jpg", "at_ms": 7_000, "bytes": 10}
    candidate = _candidate(service, project, copy={"title": "旧", "cover": picked})
    _stub_client(service, json.dumps(GOOD, ensure_ascii=False))
    started = call(service, "clip.copy", {"candidate_id": candidate["id"], "regenerate": True})
    wait_job(service, started["job"]["id"])
    assert service.repo.candidate_get(candidate["id"])["copy"]["cover"] == picked


def test_failed_model_still_produces_rule_copy(service):
    project = _project(service)
    candidate = _candidate(service, project)
    _stub_client(service, RuntimeError("boom"), RuntimeError("boom"))
    started = call(service, "clip.copy", {"candidate_id": candidate["id"]})
    job = wait_job(service, started["job"]["id"])
    assert job["status"] == "succeeded"
    row = service.repo.candidate_get(candidate["id"])
    assert row["copy"]["source"] == "rule"
    assert row["copy"]["title"] == "片段标题"


def test_cover_failure_does_not_fail_copy_job(service):
    project = _project(service)
    candidate = _candidate(service, project)
    _stub_client(service, json.dumps(GOOD, ensure_ascii=False))
    service._grab_cover = lambda *a, **k: {}  # type: ignore[assignment]
    started = call(service, "clip.copy", {"candidate_id": candidate["id"]})
    job = wait_job(service, started["job"]["id"])
    assert job["status"] == "succeeded"
    assert "cover" not in service.repo.candidate_get(candidate["id"])["copy"]


def test_segments_outside_clip_fall_back_to_whole_transcript(service):
    project = _project(service)
    # 区间落在两句之间（20.5s–25s 只有 s3 的尾巴），取样时不能空着
    candidate = _candidate(service, project, start_ms=20_500, end_ms=25_000)
    texts = [seg["text"] for seg in service._candidate_segments(candidate)]
    assert texts == ["最后一句"]


def test_empty_interval_falls_back_to_all_segments(service):
    project = _project(service)
    candidate = _candidate(service, project, start_ms=100_000, end_ms=140_000)
    assert len(service._candidate_segments(candidate)) == 3


def test_copy_save_marks_edited_and_warns(service):
    project = _project(service)
    candidate = _candidate(service, project, copy={"title": "x", "tags": []})
    out = call(
        service,
        "clip.copy.save",
        {
            "candidate_id": candidate["id"],
            "title": "改过的标题",
            "description": "改过的简介",
            "tags": ["甲"],
        },
    )
    assert out["copy"]["edited"] is True
    assert out["copy"]["title"] == "改过的标题"
    assert any("标签只有" in item for item in out["problems"])


def test_copy_save_truncates_overlong_fields(service):
    project = _project(service)
    candidate = _candidate(service, project)
    out = call(
        service,
        "clip.copy.save",
        {
            "candidate_id": candidate["id"],
            "title": "标" * 200,
            "description": "描" * 500,
            "tags": ["甲", "乙", "丙", "丁", "戊", "己", "庚"],
        },
    )
    assert len(out["copy"]["title"]) <= copywriter.TITLE_MAX
    assert len(out["copy"]["description"]) <= copywriter.DESC_MAX
    assert len(out["copy"]["tags"]) <= copywriter.TAGS_MAX


def test_copy_save_rejects_unknown_candidate(service):
    assert (
        err(service, "clip.copy.save", {"candidate_id": "cand_nope", "title": "x"}) == "NOT_FOUND"
    )


def test_cover_requires_source_asset(service, monkeypatch):
    project = _project(service)
    candidate = _candidate(service, project)
    monkeypatch.setattr(service, "_grab_cover", lambda *a, **k: {})
    assert err(service, "clip.cover", {"candidate_id": candidate["id"]}) == "PROCESS_FAILED"


def test_cover_updates_copy_payload(service, monkeypatch):
    project = _project(service)
    candidate = _candidate(service, project)
    info = {"path": "C:/covers/x.jpg", "at_ms": 5_000, "bytes": 42}
    monkeypatch.setattr(service, "_grab_cover", lambda *a, **k: dict(info))
    out = call(service, "clip.cover", {"candidate_id": candidate["id"]})
    assert out["cover"] == info
    assert service.repo.candidate_get(candidate["id"])["copy"]["cover"] == info


def test_cover_frames_passes_clamped_count(service, monkeypatch):
    project = _project(service)
    candidate = _candidate(service, project)
    monkeypatch.setattr(service, "_source_file", lambda pid: "C:/src.mp4")
    seen = {}

    def fake_frames(ff, src, cand, root, *, count=4):
        seen["count"] = count
        return {"frames": [{"path": "C:/f1.jpg", "at_ms": 1_000}], "count": count}

    monkeypatch.setattr(cover, "frames_for", fake_frames)
    out = call(service, "clip.cover.frames", {"candidate_id": candidate["id"], "count": 99})
    assert seen["count"] == 8
    assert out["frames"][0]["at_ms"] == 1_000


def test_cover_upload_copies_file_into_project(service, tmp_path):
    project = _project(service)
    candidate = _candidate(service, project)
    upload = tmp_path / "up.png"
    upload.write_bytes(b"png")
    out = call(service, "clip.cover.upload", {"candidate_id": candidate["id"], "path": str(upload)})
    assert Path(out["cover"]["path"]).is_file()
    assert out["cover"]["source"] == "upload"
    assert (
        service.repo.candidate_get(candidate["id"])["copy"]["cover"]["path"] == out["cover"]["path"]
    )


def test_cover_upload_rejects_non_image(service, tmp_path):
    project = _project(service)
    candidate = _candidate(service, project)
    bad = tmp_path / "note.txt"
    bad.write_text("x", encoding="utf-8")
    assert (
        err(service, "clip.cover.upload", {"candidate_id": candidate["id"], "path": str(bad)})
        == "VALIDATION"
    )


def test_candidates_list_carries_copy(service):
    project = _project(service)
    _candidate(service, project, copy={"title": "带文案的"})
    data = call(service, "clip.candidates", {"id": project["id"]})
    assert data["candidates"][0]["copy"]["title"] == "带文案的"
