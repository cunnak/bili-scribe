"""总结适配器（P2-07）的测试。

要证明的是四件事，缺一条这个功能就该被打回：

1. **素材对得上**：字幕进得去，时间戳还在（丢了时间戳，模型就不许写 timeline）；
2. **角色换得动**：选 ``xueshu`` 真的换了提示词，不是换了个下拉框；
3. **版本存得住**：每次生成/打磨各存一版，恢复不删历史；
4. **失败说得清**：模型不吐 JSON 时报 LLM_BAD_RESPONSE，而不是把空总结入库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.core import summarizer
from video_workbench.core.subtitles import normalize


GOOD = {
    "one_line": "这期讲三件事",
    "summary_md": "## 开头\n讲了三件事。",
    "key_points": ["第一件", "第二件"],
    "timeline": [{"t": "00:01", "text": "开场"}],
    "caution": "",
    "comment_short": "一句话：讲了三件事。",
}


class StubClient:
    """按序回吐。给了 dict 就序列化——真实客户端返回的本来就是字符串。"""

    def __init__(self, replies, model="stub-summary"):
        self.replies = list(replies)
        self.model = model
        self.base_url = "http://stub.local/v1"
        self.calls: list[list] = []

    @property
    def configured(self):
        return True

    def require(self):
        return None

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        item = self.replies.pop(0) if self.replies else json.dumps(GOOD)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


class FakeCtx:
    def __init__(self):
        self.logs: list[str] = []
        self.steps: list[tuple] = []

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.steps.append((value, stage))

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        return False

    def check_cancel(self):
        return None


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
    monkeypatch.setattr(service, "_llm_client", lambda task="summary": client)
    return client


def make_project(service, tmp_path: Path, *, texts=None, stage="transcribed"):
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(title="标题", up_name="UP", root_dir=str(root),
                                          stage=stage)
    items = normalize([
        {"start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": t}
        for i, t in enumerate(texts or ["今天讲三件事", "然后第一件事", "第二件事"])
    ])
    service.repo.transcript_upsert(project["id"], segments=items, language="zh",
                                   model="tiny", srt_path=str(root / "transcript.srt"))
    return project


def run_job(service, monkeypatch, client, payload, project=None):
    """直接调 handler：不经过线程，才能断言中间产物。"""
    configure(service, monkeypatch, client)
    ctx = FakeCtx()
    if project is not None:
        payload = {**payload, "project_id": project["id"]}
    result = service._summary_job(ctx, payload)
    return result, ctx


# ------------------------------------------------------------------ 素材


def test_segments_text_keeps_timestamps():
    body = summarizer.segments_text([
        {"start_ms": 0, "end_ms": 900, "text": "开场"},
        {"start_ms": 3723000, "end_ms": 3724000, "text": "一小时处"},
    ])
    assert "[00:00] 开场" in body
    assert "[1:02:03] 一小时处" in body


def test_build_bundle_marks_source_as_ai_subtitle(tmp_path):
    project = {"title": "标题", "up_name": "UP", "bvid": "BV1xx", "duration_sec": 12.5}
    bundle = summarizer.build_bundle(project, [{"start_ms": 0, "text": "内容"}])
    assert bundle.source_kind == "subtitle_ai"
    # 标成 subtitle_ai 才允许模型写 timeline；标错等于把时间线功能关掉。
    assert "时间戳" in bundle.source_detail
    assert bundle.duration == 12


def test_build_bundle_refuses_empty_segments():
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        summarizer.build_bundle({"title": "x"}, [])
    assert exc.value.code == "CONFLICT"


def test_segments_with_blank_text_are_skipped():
    body = summarizer.segments_text([
        {"start_ms": 0, "text": "  "},
        {"start_ms": 1000, "text": "有内容"},
    ])
    assert body == "[00:01] 有内容"


# ------------------------------------------------------------------ 主流程


def test_run_produces_summary_with_role_and_model():
    client = StubClient([json.dumps(GOOD)])
    ctx = FakeCtx()
    result = summarizer.run(
        {"title": "标题", "up_name": "UP"},
        [{"start_ms": 0, "text": "内容"}],
        client=client,
        cfg=_cfg(),
        ctx=ctx,
        role="standard",
    )
    assert result["summary"]["one_line"] == "这期讲三件事"
    assert result["role"] == "standard"
    assert result["model"] == "stub-summary"
    assert result["calls"] == 1


def test_role_changes_the_system_prompt():
    """角色不是装饰：选学术角色时必须换掉 system 提示词。"""
    one = StubClient([json.dumps(GOOD)])
    summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                   client=one, cfg=_cfg(), ctx=FakeCtx(), role="standard")
    two = StubClient([json.dumps(GOOD)])
    summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                   client=two, cfg=_cfg(), ctx=FakeCtx(), role="xueshu")
    assert one.calls[0][0]["content"] != two.calls[0][0]["content"]


def test_unknown_role_is_rejected():
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                       client=StubClient([json.dumps(GOOD)]), cfg=_cfg(),
                       ctx=FakeCtx(), role="nope")
    assert exc.value.code == "VALIDATION"


def test_non_json_reply_raises_bad_response():
    from video_workbench.app.errors import WBError

    # 第一次回废话 → 重问还是废话 → 再试着分批要（正文那一步又是废话）→ 才算失败。
    # 只回一次废话会触发重问，见下一条用例；分批是第三次尝试，见 test_summary_split。
    with pytest.raises(WBError) as exc:
        summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                       client=StubClient(
                           ["我今天不想按格式回话", "还是不按格式", "分批也还是废话"]
                       ),
                       cfg=_cfg(), ctx=FakeCtx())
    assert exc.value.code == "LLM_BAD_RESPONSE"


def test_non_json_reply_is_asked_again_once():
    """模型偶尔会把 JSON 说成人话；重问一次能救回来就别让用户白跑一轮预读。"""
    from video_workbench.infra.jsonfix import repair_json

    assert repair_json('{"one_line": "一行",' + chr(10) + ' "summary_md": "第一段' + chr(10) + '第二段"}'), \
        "字符串里的原始换行应该能修好"
    ctx = FakeCtx()
    result = summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                            client=StubClient(["我先说两句废话", json.dumps(GOOD)]),
                            cfg=_cfg(), ctx=ctx)
    assert result["summary"]["one_line"], "重问之后应该拿到一份总结"
    assert any("重问" in note for note in result.get("notes") or []), "没记下重问这件事"

def test_progress_reports_chinese_stages():
    ctx = FakeCtx()
    summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                   client=StubClient([json.dumps(GOOD)]), cfg=_cfg(), ctx=ctx)
    stages = [stage for _value, stage in ctx.steps]
    assert "准备素材" in stages
    # 界面上不能出现 bili-scribe 的英文 stage。
    assert all(stage and stage.isascii() is False for stage in stages if stage)


def test_json_mode_rejected_falls_back_once():
    """端点不认 response_format 时要能退回来，不能让用户永远出不了总结。"""

    class Picky(StubClient):
        def __init__(self):
            super().__init__([json.dumps(GOOD)])
            self.modes: list[bool] = []

        def chat(self, messages, **kwargs):
            self.modes.append(bool(kwargs.get("json_mode")))
            if kwargs.get("json_mode"):
                from video_workbench.infra.llm import ChatError

                raise ChatError("LLM_BAD_REQUEST", "不支持 response_format")
            return super().chat(messages, **kwargs)

    client = Picky()
    result = summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "x"}],
                            client=client, cfg=_cfg(), ctx=FakeCtx())
    assert client.modes == [True, False]
    assert result["summary"]["one_line"]
    assert any("JSON 模式" in note for note in result["notes"])


def test_long_material_is_chunked_by_policy():
    """超过 max_input_chars 要分段预读——这是 bili-scribe 的核心能力，不能悄悄关掉。"""
    long_text = "长素材内容。" * 400      # ≈2400 字，配合调小的预算就会分块
    cfg = _cfg(max_input_chars=500, chunk_size=200, chunk_overlap=20)
    client = StubClient(["笔记一", "笔记二", json.dumps(GOOD)])
    result = summarizer.run(
        {"title": "t"},
        [{"start_ms": i * 1000, "text": long_text} for i in range(3)],
        client=client, cfg=cfg, ctx=FakeCtx(),
    )
    assert result["summary"]["chunks"] >= 2
    assert len(client.calls) > 1


def test_refine_requires_previous_and_keeps_material():
    from video_workbench.app.errors import WBError

    client = StubClient([json.dumps({**GOOD, "revise_note": "收紧了措辞"})])
    with pytest.raises(WBError) as exc:
        summarizer.refine({"title": "t"}, [{"start_ms": 0, "text": "x"}], {},
                          "再短一点", client=client, cfg=_cfg(), ctx=FakeCtx())
    assert exc.value.code == "CONFLICT"

    result = summarizer.refine(
        {"title": "t"}, [{"start_ms": 0, "text": "内容还在"}], GOOD,
        "再短一点", client=client, cfg=_cfg(), ctx=FakeCtx())
    prompt = client.calls[0][1]["content"]
    assert "内容还在" in prompt          # 素材没被丢掉（否则会越改越远）
    assert "再短一点" in prompt
    assert result["summary"]["revise_note"] == "收紧了措辞"


def test_refine_rejects_too_short_requirement():
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        summarizer.refine({"title": "t"}, [{"start_ms": 0, "text": "x"}], GOOD, "改",
                          client=StubClient([]), cfg=_cfg(), ctx=FakeCtx())
    assert exc.value.code == "VALIDATION"


# ------------------------------------------------------------------ 渲染


def test_markdown_carries_every_section():
    md = summarizer.to_markdown({**GOOD, "role": "standard", "model": "m", "chunks": 2})
    for piece in ("这期讲三件事", "## 要点", "第一件", "## 时间线", "`00:01`",
                  "## 评论用短版", "角色：standard", "模型：m", "分段：2"):
        assert piece in md


def test_markdown_skips_empty_sections():
    md = summarizer.to_markdown({"one_line": "只有一句"})
    assert md.strip() == "> 只有一句"


# ------------------------------------------------------------------ 服务层


def test_summary_without_config_does_not_create_job(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    service.cfg.llm.base_url = ""
    service.cfg.llm.api_key = ""
    seen: list = []
    monkeypatch.setattr(service.jobs, "submit", lambda *a, **k: seen.append(a) or {"id": "j"})
    error = err(service, "summary.start", {"project_id": project["id"]})
    assert error["code"] == "LLM_NOT_CONFIGURED"
    assert seen == []


def test_summary_without_transcript_is_conflict(service, tmp_path, monkeypatch):
    root = tmp_path / "m" / "empty"
    root.mkdir(parents=True)
    project = service.repo.project_create(title="空", root_dir=str(root))
    configure(service, monkeypatch, StubClient([]))
    error = err(service, "summary.start", {"project_id": project["id"]})
    assert error["code"] == "CONFLICT"
    assert "转录" in error["message"]


def test_summary_start_submits_job_with_role(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    seen: dict = {}

    def fake_submit(job_type, project_id=None, payload=None):
        seen.update({"type": job_type, "payload": payload or {}})
        return {"id": "job_s"}

    monkeypatch.setattr(service.jobs, "submit", fake_submit)
    data = rpc(service, "summary.start", {"project_id": project["id"], "role": "fenxi"})
    assert seen["type"] == "summary.run"
    assert seen["payload"]["role"] == "fenxi"
    assert data["role"] == "fenxi"


def test_summary_start_rejects_unknown_role(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    error = err(service, "summary.start", {"project_id": project["id"], "role": "nope"})
    assert error["code"] == "VALIDATION"


def test_job_writes_files_and_version(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    result, ctx = run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]),
                          {"kind": "fresh", "role": "standard"}, project)
    root = Path(service.repo.project_get(project["id"])["root_dir"])
    assert (root / "summary.md").is_file()
    assert (root / "summary.json").is_file()
    assert result["version"] == 1
    assert result["chars"] > 0
    saved = service.repo.summary_latest(project["id"])
    assert saved["content"]["one_line"] == "这期讲三件事"
    assert saved["role"] == "standard"
    assert service.repo.project_get(project["id"])["stage"] == "summarized"
    assert any(
        item["kind"] == "summary" and item["path"] == str(root / "summary.md")
        for item in service.repo.asset_list(project["id"])
    )
    assert ctx.logs and "stub-summary" in ctx.logs[0]


def test_two_versions_both_kept_and_numbered(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]),
            {"kind": "fresh", "role": "standard"}, project)
    run_job(service, monkeypatch, StubClient([json.dumps({**GOOD, "one_line": "第二版"})]),
            {"kind": "fresh", "role": "xueshu"}, project)
    rows = service.repo.summary_list(project["id"])
    assert [row["version"] for row in rows] == [2, 1]
    assert {row["role"] for row in rows} == {"standard", "xueshu"}


def test_refine_creates_new_version_with_note(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]),
            {"kind": "fresh"}, project)
    base = service.repo.summary_latest(project["id"])
    result, _ctx = run_job(
        service, monkeypatch,
        StubClient([json.dumps({**GOOD, "revise_note": "把要点压到两条"})]),
        {"kind": "refine", "requirement": "要点压到两条", "previous_id": base["id"]},
        project)
    assert result["version"] == 2
    latest = service.repo.summary_latest(project["id"])
    assert latest["content"]["revise_note"] == "把要点压到两条"
    assert service.repo.summary_get(base["id"])["content"]["one_line"] == "这期讲三件事"


def test_restore_adds_a_version_instead_of_deleting(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    first = service.repo.summary_latest(project["id"])
    run_job(service, monkeypatch, StubClient([json.dumps({**GOOD, "one_line": "改过的"})]),
            {"kind": "fresh"}, project)
    data = rpc(service, "summary.restore",
               {"project_id": project["id"], "summary_id": first["id"]})
    assert data["summary"]["version"] == 3
    rows = service.repo.summary_list(project["id"])
    assert len(rows) == 3                     # 历史一条没少
    assert rows[0]["content"]["one_line"] == "这期讲三件事"
    assert "恢复自第 1 版" in rows[0]["content"]["revise_note"]


def test_restore_rejects_foreign_project(service, tmp_path, monkeypatch):
    one = make_project(service, tmp_path)
    other = service.repo.project_create(title="别的", root_dir=str(tmp_path / "other"))
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, one)
    first = service.repo.summary_latest(one["id"])
    error = err(service, "summary.restore",
                {"project_id": other["id"], "summary_id": first["id"]})
    assert error["code"] == "VALIDATION"


def test_get_returns_latest_and_versions(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    empty = rpc(service, "summary.get", {"id": project["id"]})
    assert empty["found"] is False and empty["versions"] == []
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    data = rpc(service, "summary.get", {"id": project["id"]})
    assert data["found"] is True
    assert data["summary"]["content"]["one_line"] == "这期讲三件事"
    assert data["versions"][0]["chars"] > 0
    assert data["versions"][0]["role"] == "standard"


def test_roles_endpoint_lists_every_role(service):
    data = rpc(service, "summary.roles")
    keys = [item["key"] for item in data["roles"]]
    assert "standard" in keys and "xueshu" in keys and "jinglian" in keys
    assert data["default"] in keys


def test_job_does_not_downgrade_late_stage(service, tmp_path, monkeypatch):
    """已经切过片的项目重出总结，阶段不能被拉回 summarized。"""
    project = make_project(service, tmp_path)
    service.repo.project_update(project["id"], stage="clipped")
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    assert service.repo.project_get(project["id"])["stage"] == "clipped"


def test_job_failure_keeps_previous_versions(service, tmp_path, monkeypatch):
    from video_workbench.app.errors import WBError

    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    # 三次都不是 JSON：主请求 → 重问 → 分批的正文那一步。分批也救不回来才该失败。
    configure(service, monkeypatch, StubClient(["不是 JSON", "还不是 JSON", "分批也不是 JSON"]))
    with pytest.raises(WBError) as exc:
        service._summary_job(FakeCtx(), {"project_id": project["id"], "kind": "fresh"})
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert len(service.repo.summary_list(project["id"])) == 1


def test_every_llm_error_code_is_registered():
    """模型层抛的每个码都必须在错误码表里。

    这条是回归：``LLM_AUTH`` 等六个码曾经**不在** ``ERROR_CODES`` 里，于是模型一返
    401/429/400/5xx，构造异常时先抛 ``ValueError("未知错误码")``——用户看到"内部错误"，
    而"key 不对 / 被限流"这些真正有用的信息全没了。
    """
    import re

    from video_workbench.app.errors import ERROR_CODES
    from video_workbench.infra import llm as llm_mod

    source = Path(llm_mod.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'ChatError\(\s*"([A-Z_]+)"', source))
    assert codes, "没扫到任何 ChatError，正则或源码结构变了"
    missing = sorted(code for code in codes if code not in ERROR_CODES)
    assert not missing, f"这些码没进 ERROR_CODES：{missing}"


def _cfg(**llm):
    """造一份只用于总结器的配置：真 config 里的其他字段这层用不到。"""

    from video_workbench.config import WorkbenchConfig

    cfg = WorkbenchConfig()
    for key, value in llm.items():
        setattr(cfg.llm, key, value)
    cfg.llm.max_tokens = 4096
    cfg.llm.temperature = 0.3
    cfg.llm.role = "standard"
    return cfg
