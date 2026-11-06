"""总结页可以选模型：`summary.models` 列清单，`summary.start/refine/collection.overview` 收 `model`。

选中的是「服务商id/模型名」（`parse_model_spec` 的同一套约定），空串＝跟随设置。
"""

from __future__ import annotations

import json

from test_summary import GOOD, StubClient, configure, err, make_project, rpc

from video_workbench.config import LlmProviderConfig


def _two_cards(service) -> None:
    service.cfg.llm.providers = [
        LlmProviderConfig(
            id="deepseek",
            name="DeepSeek 官方",
            protocol="openai",
            base_url="https://api.deepseek.com/v1",
            api_key="sk-a",
            models=["deepseek-chat", "deepseek-reasoner"],
        ),
        LlmProviderConfig(
            id="mo",
            name="Kimi",
            protocol="openai",
            base_url="https://api.moonshot.cn/v1",
            api_key="sk-b",
            models=["moonshot-v1-8k"],
        ),
    ]
    service.cfg.llm.active_provider = "deepseek"
    service.cfg.llm.model = "deepseek-chat"
    service.cfg.llm.tasks.summary = ""


def test_summary_models_lists_every_provider_model(service):
    _two_cards(service)
    data = rpc(service, "summary.models")
    specs = [item["spec"] for item in data["items"]]
    assert specs == ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner", "mo/moonshot-v1-8k"]
    assert data["default"] == "deepseek/deepseek-chat"
    assert data["configured"] is True
    first = data["items"][0]
    assert first["provider"] == "deepseek" and first["provider_name"] == "DeepSeek 官方"
    assert first["model"] == "deepseek-chat"


def test_summary_models_default_follows_task_mapping(service):
    _two_cards(service)
    service.cfg.llm.tasks.summary = "mo/moonshot-v1-8k"
    data = rpc(service, "summary.models")
    assert data["default"] == "mo/moonshot-v1-8k"


def test_summary_models_empty_when_no_provider(service):
    service.cfg.llm.providers = []
    data = rpc(service, "summary.models")
    assert data["items"] == []
    assert data["configured"] is False


def test_summary_start_forwards_bare_model(service, tmp_path, monkeypatch):
    _two_cards(service)
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    seen: dict = {}
    monkeypatch.setattr(
        service.jobs,
        "submit",
        lambda job_type, project_id=None, payload=None: (
            seen.update(payload or {}) or {"id": "job_s"}
        ),
    )
    data = rpc(
        service,
        "summary.start",
        {"project_id": project["id"], "role": "wengao", "model": "deepseek-reasoner"},
    )
    assert seen["model"] == "deepseek-reasoner"
    assert data["model"] == "deepseek-reasoner"


def test_summary_start_forwards_provider_qualified_model(service, tmp_path, monkeypatch):
    _two_cards(service)
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    seen: dict = {}
    monkeypatch.setattr(
        service.jobs,
        "submit",
        lambda job_type, project_id=None, payload=None: (
            seen.update(payload or {}) or {"id": "job_s"}
        ),
    )
    data = rpc(
        service,
        "summary.start",
        {"project_id": project["id"], "role": "wengao", "model": "mo/moonshot-v1-8k"},
    )
    assert seen["provider"] == "mo"
    assert seen["model"] == "moonshot-v1-8k"
    # RPC 返回值同样要报这个拆分后的形状：前端按 provider/model 两列显示，
    # 只让 payload 对、返回值还是拼串的话，界面显示与任务实际用的是两个东西。
    assert (data["provider"], data["model"]) == ("mo", "moonshot-v1-8k")


def test_summary_start_rejects_unknown_provider_in_model(service, tmp_path, monkeypatch):
    _two_cards(service)
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    error = err(
        service, "summary.start", {"project_id": project["id"], "role": "wengao", "model": "nope/x"}
    )
    assert error["code"] == "VALIDATION"
    assert "nope" in error["message"]


def test_refine_without_model_keeps_previous_version_model(service, tmp_path, monkeypatch):
    _two_cards(service)
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    # 保留 standard：直接写库/历史行的字面量，是「退役前数据仍可读」的证据（分类标准见 test_summary.py 文件头）
    service.repo.summary_insert(
        project_id=project["id"],
        role="standard",
        model="deepseek-reasoner",
        content_md="旧的一版",
        content={"one_line": "旧的一版"},
    )
    seen: dict = {}
    monkeypatch.setattr(
        service.jobs,
        "submit",
        lambda job_type, project_id=None, payload=None: (
            seen.update(payload or {}) or {"id": "job_s"}
        ),
    )
    rpc(service, "summary.refine", {"project_id": project["id"], "requirement": "把要点压到三条"})
    assert seen["model"] == "deepseek-reasoner", "没指定模型时不该悄悄换成默认模型"


def test_summary_job_uses_model_from_payload(service, tmp_path, monkeypatch):
    """任务真正用的客户端模型要跟着 payload 走，而不是只把名字写进版本行。"""
    _two_cards(service)
    project = make_project(service, tmp_path)
    stub = StubClient([json.dumps(GOOD)])
    configure(service, monkeypatch, stub)
    from test_summary import FakeCtx

    result = service._summary_job(
        FakeCtx(),
        {
            "project_id": project["id"],
            "kind": "fresh",
            "role": "wengao",
            "model": "deepseek-reasoner",
            "provider": "",
        },
    )
    assert stub.model == "deepseek-reasoner", "客户端没换成选中的模型"
    assert result["model"] == "deepseek-reasoner"
