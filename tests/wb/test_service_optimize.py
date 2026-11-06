"""字幕优化 RPC 的装配测试（P2-04）。

验三件事，都是"界面能不能信"的前提：

1. 没配模型时不建任务、不写库，错误码是 ``LLM_NOT_CONFIGURED``；
2. ``subtitle.optimize`` 只产 diff，**不动数据库**；
3. ``apply`` 落三处 + 存快照，``rollback`` 能原样退回去。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.core.subtitles import normalize
from video_workbench.infra.llm import ChatError


class StubClient:
    def __init__(self, replies, model="stub-model"):
        self.replies = list(replies)
        self.model = model
        self.base_url = "http://stub.local/v1"
        self.calls = []

    @property
    def configured(self):
        return True

    def require(self):
        return None

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        item = self.replies.pop(0) if self.replies else "[]"
        if isinstance(item, Exception):
            raise item
        return item


class FakeCtx:
    """只实现 handler 真正用到的那几个方法。"""

    def __init__(self):
        self.logs: list[str] = []
        self.steps: list[tuple[float | None, str | None]] = []
        self.cancelled = False

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.steps.append((value, stage))

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        return self.cancelled

    def check_cancel(self):
        if self.cancelled:
            raise RuntimeError("canceled")


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]


def configure(service, monkeypatch, client):
    service.cfg.override_llm(base_url=client.base_url, api_key="sk-test", model="stub-model")
    service.cfg.llm.model = client.model
    monkeypatch.setattr(service, "_llm_client", lambda task="subtitle": client)
    return client


def make_project(service, tmp_path: Path, *, texts=None) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="transcribed"
    )
    items = normalize(
        [
            {"start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": t}
            for i, t in enumerate(texts or ["嗯那个今天讲三件事", "然后然后第一件事", "第二件事"])
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


def stored_texts(service, project_id) -> list[str]:
    row = service.repo.transcript_get(project_id) or {}
    return [s["text"] for s in normalize(row.get("segments") or [])]


# ---------------------------------------------------------------- 未配置


def test_llm_test_without_config_reports_not_configured(service):
    service.cfg.llm.base_url = ""
    service.cfg.llm.api_key = ""
    service.cfg.llm.model = ""
    error = err(service, "llm.test")
    assert error["code"] == "LLM_NOT_CONFIGURED"


def test_optimize_without_config_does_not_create_job(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    service.cfg.llm.base_url = ""
    service.cfg.llm.api_key = ""
    service.cfg.llm.model = ""
    submitted = []
    monkeypatch.setattr(service.jobs, "submit", lambda *a, **k: submitted.append(a) or {"id": "x"})
    error = err(service, "subtitle.optimize", {"project_id": project["id"]})
    assert error["code"] == "LLM_NOT_CONFIGURED"
    assert submitted == []


def test_optimize_unknown_project_is_not_found(service, monkeypatch):
    configure(service, monkeypatch, StubClient([]))
    assert err(service, "subtitle.optimize", {"project_id": "pj_nope"})["code"] == "NOT_FOUND"


def test_optimize_project_without_transcript_is_not_found(service, tmp_path, monkeypatch):
    configure(service, monkeypatch, StubClient([]))
    project = service.repo.project_create(
        title="空", root_dir=str(tmp_path / "none"), stage="downloaded"
    )
    assert err(service, "subtitle.optimize", {"project_id": project["id"]})["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 接线


def test_llm_test_returns_latency_and_model(service, monkeypatch):
    client = configure(service, monkeypatch, StubClient(["OK"]))
    data = rpc(service, "llm.test")
    assert data["ok"] is True
    assert data["model"] == "stub-model"
    assert data["reply"] == "OK"
    assert len(client.calls) == 1
    assert isinstance(data["latency_ms"], int)


def test_llm_test_maps_chat_error(service, monkeypatch):
    configure(service, monkeypatch, StubClient([ChatError("LLM_ERROR", "连不上")]))
    error = err(service, "llm.test")
    assert error["code"] == "LLM_ERROR"


def test_optimize_submits_job_with_clamped_batch_size(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    seen = []

    def fake_submit(job_type, project_id=None, payload=None):
        seen.append((job_type, project_id, payload or {}))
        return {"id": "job_x", "type": job_type, "status": "queued"}

    monkeypatch.setattr(service.jobs, "submit", fake_submit)
    data = rpc(service, "subtitle.optimize", {"project_id": project["id"], "batch_size": 999})
    assert data["segments"] == 3 and data["model"] == "stub-model"
    job_type, project_id, payload = seen[0]
    assert job_type == "subtitle.optimize" and project_id == project["id"]
    # 80 是上限：一次要太多行，模型会开始糊弄
    assert payload["batch_size"] == 80


def test_optimize_can_target_subset(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    monkeypatch.setattr(service.jobs, "submit", lambda *a, **k: {"id": "job_x"})
    data = rpc(service, "subtitle.optimize", {"project_id": project["id"], "segment_ids": ["s2"]})
    assert data["segments"] == 1


# ---------------------------------------------------------------- handler


def test_handler_returns_changes_and_leaves_db_untouched(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    before = stored_texts(service, project["id"])
    client = configure(
        service,
        monkeypatch,
        StubClient(
            [
                json.dumps(
                    [
                        {"id": "s1", "text": "今天讲三件事。"},
                        {"id": "s2", "text": "第一件事。"},
                        {"id": "s3", "text": "第二件事。"},
                    ]
                ),
            ]
        ),
    )
    ctx = FakeCtx()
    result = service._optimize_job(ctx, {"project_id": project["id"], "batch_size": 30})
    assert result["changed"] == 3
    assert [c["id"] for c in result["changes"]] == ["s1", "s2", "s3"]
    assert result["failed"] == []
    assert ctx.steps and ctx.steps[-1][0] == 1.0
    # 关键断言：优化只是"看一眼"，没写库
    assert stored_texts(service, project["id"]) == before
    assert client.calls


def test_handler_reports_partial_failure_but_keeps_good_batches(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(
        service,
        monkeypatch,
        StubClient(
            [
                json.dumps([{"id": "s1", "text": "今天讲三件事。"}]),
                ChatError("LLM_ERROR", "超时"),
                ChatError("LLM_ERROR", "超时"),
            ]
        ),
    )
    ctx = FakeCtx()
    result = service._optimize_job(ctx, {"project_id": project["id"], "batch_size": 1})
    assert result["changed"] == 1
    assert len(result["failed"]) == 2
    assert any("2 批失败" in line for line in ctx.logs)


def test_handler_raises_when_every_batch_failed(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([ChatError("LLM_ERROR", "全挂")] * 3))
    with pytest.raises(Exception) as info:
        service._optimize_job(FakeCtx(), {"project_id": project["id"], "batch_size": 30})
    assert getattr(info.value, "code", "") == "LLM_ERROR"


# ---------------------------------------------------------------- 应用与回滚


def test_apply_writes_three_places_and_snapshot(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    changes = [
        {"id": "s1", "before": "嗯那个今天讲三件事", "after": "今天讲三件事。", "kind": "text"}
    ]
    data = rpc(
        service, "subtitle.optimize.apply", {"project_id": project["id"], "changes": changes}
    )
    assert data["applied"] == 1
    assert data["segments"][0]["text"] == "今天讲三件事。"
    root = Path(project["root_dir"])
    assert (root / "transcript.prev.json").is_file()
    assert "今天讲三件事。" in (root / "transcript.srt").read_text(encoding="utf-8")
    assert "今天讲三件事。" in (root / "transcript.json").read_text(encoding="utf-8")
    assert stored_texts(service, project["id"])[0] == "今天讲三件事。"


def test_apply_respects_accepted_subset(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    changes = [
        {"id": "s1", "before": "嗯那个今天讲三件事", "after": "今天讲三件事。", "kind": "text"},
        {"id": "s2", "before": "然后然后第一件事", "after": "第一件事。", "kind": "text"},
    ]
    data = rpc(
        service,
        "subtitle.optimize.apply",
        {"project_id": project["id"], "changes": changes, "accepted_ids": ["s1"]},
    )
    assert data["segments"][0]["text"] == "今天讲三件事。"
    assert data["segments"][1]["text"] == "然后然后第一件事"


def test_apply_without_changes_reports_zero_and_no_snapshot(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    data = rpc(service, "subtitle.optimize.apply", {"project_id": project["id"], "changes": []})
    assert data["applied"] == 0
    assert data["snapshot"] == ""
    assert not (Path(project["root_dir"]) / "transcript.prev.json").is_file()


def test_rollback_restores_previous_segments(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    original = stored_texts(service, project["id"])
    rpc(
        service,
        "subtitle.optimize.apply",
        {
            "project_id": project["id"],
            "changes": [{"id": "s1", "before": original[0], "after": "改过的。", "kind": "text"}],
        },
    )
    assert stored_texts(service, project["id"])[0] == "改过的。"
    data = rpc(service, "subtitle.rollback", {"id": project["id"]})
    assert [s["text"] for s in data["segments"]] == original
    assert stored_texts(service, project["id"]) == original
    assert data["restored_at"]


def test_rollback_without_snapshot_is_not_found(service, tmp_path):
    project = make_project(service, tmp_path)
    assert err(service, "subtitle.rollback", {"id": project["id"]})["code"] == "NOT_FOUND"


def test_rollback_rejects_empty_snapshot(service, tmp_path):
    project = make_project(service, tmp_path)
    (Path(project["root_dir"]) / "transcript.prev.json").write_text(
        json.dumps({"segments": []}), encoding="utf-8"
    )
    assert err(service, "subtitle.rollback", {"id": project["id"]})["code"] == "CONFLICT"
