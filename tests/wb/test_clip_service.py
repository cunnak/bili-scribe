"""切片候选入库与改切点（P3-01/02/03 的服务层）。

两条约定写在这里：

1. **错误看信封，不抓异常**。`dispatch` 不抛——它把异常翻成
   `{ok:false, error:{code}}`，测试必须断言信封，否则测的是实现细节而不是契约。
2. 这里不碰 ffmpeg。导出是 P3-05 的事。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from video_workbench.core import clipper


def _project(service, with_transcript=True):
    row = service.repo.project_create(
        title="切片用例",
        source_kind="local",
        root_dir=str(Path(service.cfg.media_root) / "clipcase"),
    )
    if with_transcript:
        service.repo.transcript_upsert(
            row["id"],
            language="zh",
            model="stub",
            segments=[{"id": "s1", "start_ms": 0, "end_ms": 20_000, "text": "第一句"}],
        )
    return row


def call(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok"), out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert not out.get("ok"), out
    return out["error"]["code"]


def wait_job(service, job_id, timeout=8.0):
    """任务在后台线程里跑，断言前得等它落定。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务没在 {timeout}s 内结束：{service.jobs.get(job_id)}")


class StubClient:
    def __init__(self, reply):
        self.reply = reply
        self.model = "stub"
        self.calls = 0

    def chat(self, _messages, **_kwargs):
        self.calls += 1
        return self.reply


def _good_reply():
    return json.dumps(
        {
            "candidates": [
                {
                    "kind": "highlight",
                    "start_hint_ms": 0,
                    "end_hint_ms": 18_000,
                    "title": "第一段",
                    "reason": "开门见山",
                    "quote": "第一句",
                    "score": 80,
                }
            ]
        },
        ensure_ascii=False,
    )


def test_clip_analyze_requires_transcript(service):
    row = _project(service, with_transcript=False)
    assert err(service, "clip.analyze", {"project_id": row["id"]}) == "CONFLICT"


def test_clip_analyze_unknown_project(service):
    assert err(service, "clip.analyze", {"project_id": "pj_nope"}) == "NOT_FOUND"


def test_clip_analyze_writes_candidates(service, monkeypatch):
    row = _project(service)
    monkeypatch.setattr(service, "_clip_client", lambda: (StubClient(_good_reply()), "stub"))
    data = call(service, "clip.analyze", {"project_id": row["id"]})
    assert data["job"]["type"] == "clip.analyze"
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", job
    rows = service.repo.candidate_list(row["id"])
    assert [item["title"] for item in rows] == ["第一段"]
    assert rows[0]["source"] == "ai" and rows[0]["status"] == "draft"
    assert rows[0]["notes"]["origin"] == "llm"
    # 吸附原值留在 notes 里，P3-04 还原模型原话要用
    assert rows[0]["notes"]["raw_end_ms"] == 18_000


def test_reanalyze_keeps_locked_candidates(service, monkeypatch):
    row = _project(service)
    locked = service.repo.candidate_insert(
        row["id"], kind="highlight", start_ms=1_000, end_ms=16_000, status="locked", title="改过"
    )
    monkeypatch.setattr(service, "_clip_client", lambda: (StubClient(_good_reply()), "stub"))
    job_id = call(service, "clip.analyze", {"project_id": row["id"]})["job"]["id"]
    assert wait_job(service, job_id)["status"] == "succeeded"
    assert service.repo.candidate_get(locked["id"]) is not None, "锁定的候选不能被重跑清掉"
    assert len(service.repo.candidate_list(row["id"])) == 2


def test_reanalyze_replaces_old_ai_candidates(service, monkeypatch):
    row = _project(service)
    service.repo.candidate_insert(row["id"], kind="highlight", start_ms=0, end_ms=16_000, title="旧的")
    monkeypatch.setattr(service, "_clip_client", lambda: (StubClient(_good_reply()), "stub"))
    wait_job(service, call(service, "clip.analyze", {"project_id": row["id"]})["job"]["id"])
    titles = [item["title"] for item in service.repo.candidate_list(row["id"])]
    assert titles == ["第一段"], titles


def test_unconfigured_model_falls_back_to_rules(service):
    _project(service)
    client, model = service._clip_client()
    out = clipper.generate(
        [{"start_ms": 0, "end_ms": 20_000, "text": "第一句"}],
        client=client,
        duration_ms=20_000,
        kinds=("highlight",),
        model=model,
    )
    assert out["source"] == "rule" and out["candidates"]
    assert any("模型" in note for note in out["notes"])


def test_clip_candidates_returns_boundaries_and_duration(service):
    row = _project(service)
    service.repo.candidate_insert(
        row["id"], kind="highlight", start_ms=0, end_ms=16_000, score=70, title="甲"
    )
    data = call(service, "clip.candidates", {"id": row["id"]})
    assert data["duration_ms"] == 20_000, "没有源视频时退回字幕最后一句的结束"
    assert data["boundaries"] == [0, 20_000], "磁吸线＝每句的起点与终点"
    assert [item["title"] for item in data["candidates"]] == ["甲"]


def test_clip_candidates_needs_project(service):
    assert err(service, "clip.candidates", {"id": "pj_nope"}) == "NOT_FOUND"


def test_clip_update_moves_cut_and_marks_edited(service):
    row = _project(service)
    cand = service.repo.candidate_insert(
        row["id"], kind="highlight", start_ms=0, end_ms=16_000, title="甲"
    )
    got = call(
        service, "clip.update", {"id": cand["id"], "start_ms": 2_000, "end_ms": 18_000}
    )["candidate"]
    assert (got["start_ms"], got["end_ms"]) == (2_000, 18_000)
    assert got["notes"]["edited_ms"] is True


def test_clip_update_rejects_too_short(service):
    row = _project(service)
    cand = service.repo.candidate_insert(row["id"], kind="highlight", start_ms=0, end_ms=16_000)
    code = err(service, "clip.update", {"id": cand["id"], "start_ms": 10_000, "end_ms": 10_100})
    assert code == "VALIDATION"


def test_clip_update_clamps_to_duration(service):
    row = _project(service)
    cand = service.repo.candidate_insert(row["id"], kind="highlight", start_ms=0, end_ms=16_000)
    got = call(service, "clip.update", {"id": cand["id"], "start_ms": 15_000, "end_ms": 99_000})[
        "candidate"
    ]
    assert got["end_ms"] == 20_000


def test_clip_update_locks_and_rejects_unknown_status(service):
    row = _project(service)
    cand = service.repo.candidate_insert(row["id"], kind="highlight", start_ms=0, end_ms=16_000)
    got = call(service, "clip.update", {"id": cand["id"], "status": "locked"})
    assert got["candidate"]["status"] == "locked"
    assert err(service, "clip.update", {"id": cand["id"], "status": "done"}) == "VALIDATION"


def test_clip_update_unknown_candidate(service):
    assert err(service, "clip.update", {"id": "cand_nope", "title": "x"}) == "NOT_FOUND"


def test_clip_create_manual_candidate(service):
    row = _project(service)
    got = call(
        service,
        "clip.create",
        {"project_id": row["id"], "kind": "highlight", "start_ms": 1_000, "end_ms": 8_000},
    )["candidate"]
    assert got["source"] == "manual"
    assert got["notes"]["manual"] is True
    assert got["title"] == "手动切片"


def test_clip_create_rejects_unknown_kind(service):
    row = _project(service)
    assert err(service, "clip.create", {"project_id": row["id"], "kind": "trailer"}) == "VALIDATION"


def test_clip_create_unknown_project(service):
    assert err(service, "clip.create", {"project_id": "pj_nope"}) == "NOT_FOUND"


def test_clip_delete(service):
    row = _project(service)
    cand = service.repo.candidate_insert(row["id"], kind="highlight", start_ms=0, end_ms=16_000)
    call(service, "clip.delete", {"id": cand["id"]})
    assert service.repo.candidate_get(cand["id"]) is None
    assert err(service, "clip.delete", {"id": cand["id"]}) == "NOT_FOUND"


def test_notes_column_survives_migration(tmp_path):
    """v1 老库补 `notes_json`：列被加回来，已有行不丢。"""
    from video_workbench.infra import db as db_mod

    path = tmp_path / "old.db"
    conn = db_mod.open_and_migrate(path)
    conn.execute(
        "INSERT INTO wb_project (id, title, source_kind, root_dir, stage, status,"
        " created_at, updated_at)"
        " VALUES ('p1','旧项目','local',?,'new','ready','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
        (str(tmp_path),),
    )
    conn.execute(
        "INSERT INTO wb_candidate (id, project_id, kind, start_ms, end_ms, source, created_at)"
        " VALUES ('c1','p1','highlight',0,1000,'ai','2026-01-01T00:00:00Z')"
    )
    conn.execute("ALTER TABLE wb_candidate DROP COLUMN notes_json")  # 退回 v1 的形状
    conn.commit()
    conn.close()

    conn = db_mod.open_and_migrate(path)
    info = db_mod.migrate(conn, path)
    assert info["to"] == db_mod.SCHEMA_VERSION
    columns = {row[1] for row in conn.execute("PRAGMA table_info(wb_candidate)")}
    assert "notes_json" in columns
    assert [row[0] for row in conn.execute("SELECT id FROM wb_candidate")] == ["c1"]
    conn.close()


# ---------------------------------------------------------------- P6-01 画面精析


def test_analyze_skips_vision_when_unconfigured(service, monkeypatch):
    """没配 vision 模型：整层安静跳过，候选照常入库。"""
    row = _project(service)
    monkeypatch.setattr(service, "_clip_client", lambda: (StubClient(_good_reply()), "stub"))
    job = wait_job(service, call(service, "clip.analyze", {"project_id": row["id"]})["job"]["id"])
    assert job["status"] == "succeeded", job
    assert service.repo.candidate_list(row["id"]), "跳过精析不能连候选都没了"
    assert job["result"]["refined"] == 0
    assert job["result"]["vision_model"] == ""
    assert any("没配 vision" in n for n in job["result"]["notes"]), job["result"]["notes"]


def test_analyze_runs_vision_when_configured(service, monkeypatch):
    """配了 vision：精析被调用，且拿到的是项目源文件与 job 级临时目录。"""
    from video_workbench.core import clip_refine

    row = _project(service)
    source = Path(service.cfg.media_root) / "clipcase" / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake")
    service.repo.asset_upsert(row["id"], kind="source", path=str(source))
    service.cfg.llm.tasks.vision = "vl-model"
    monkeypatch.setattr(service, "_clip_client", lambda: (StubClient(_good_reply()), "stub"))
    monkeypatch.setattr(service, "_vision_client", lambda: (StubClient("{}"), "vl-model"))
    seen: dict = {}

    def fake_refine(candidates, **kwargs):
        seen.update(kwargs)
        seen["count"] = len(candidates)
        rows = [dict(c, score=99, title="精析过的") for c in candidates]
        return {"candidates": rows, "refined": len(rows), "failed": 0,
                "notes": ["精析跑过了"], "model": kwargs.get("model"), "audio": 1}

    monkeypatch.setattr(clip_refine, "refine_candidates", fake_refine)
    job = wait_job(service, call(service, "clip.analyze", {"project_id": row["id"]})["job"]["id"])
    assert job["status"] == "succeeded", job
    assert seen["source_path"] == str(source)
    assert seen["model"] == "vl-model"
    assert Path(seen["work_dir"]).name == job["id"], "临时目录要按 job 分，取消时才好整块清"
    assert job["result"]["refined"] == seen["count"]
    assert job["result"]["audio_analyzed"] == 1
    titles = [c["title"] for c in service.repo.candidate_list(row["id"])]
    assert titles == ["精析过的"], "精析结果必须落库，不能只停在返回值里"


def test_analyze_refine_false_overrides_config(service, monkeypatch):
    """用户这次明确关掉精析：即使配了 vision 也不能偷偷跑。"""
    from video_workbench.core import clip_refine

    row = _project(service)
    service.cfg.llm.tasks.vision = "vl-model"
    monkeypatch.setattr(service, "_clip_client", lambda: (StubClient(_good_reply()), "stub"))
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("refine=False 时不该调用精析")

    monkeypatch.setattr(clip_refine, "refine_candidates", boom)
    job = wait_job(service, call(service, "clip.analyze",
                                 {"project_id": row["id"], "refine": False})["job"]["id"])
    assert job["status"] == "succeeded", job
    assert called["n"] == 0
    assert job["result"]["refined"] == 0
