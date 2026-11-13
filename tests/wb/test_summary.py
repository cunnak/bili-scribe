"""总结适配器（P2-07）的测试。

要证明的是四件事，缺一条这个功能就该被打回：

1. **素材对得上**：字幕进得去，时间戳还在（丢了时间戳，模型就不许写 timeline）；
2. **角色换得动**：换一个现役角色真的换了提示词，不是换了个下拉框（角色名现读，别写死）；
3. **版本存得住**：每次生成/打磨各存一版，恢复不删历史；
4. **失败说得清**：模型不吐 JSON 时报 LLM_BAD_RESPONSE，而不是把空总结入库。

## ``standard`` 退役之后，本目录里 ``role="standard"`` 的分类标准

（task-5。别凭字面量判断漏没漏改——按它走没走生成路径判断。）

1. **走生成路径 → 改成 ``"wengao"``。** 判据：这个字符串最终会流到
   ``summarizer.run/refine`` 的 ``role`` 参数、或 ``_summary_job`` /
   ``_collection_job`` / ``ov.run`` / ``summary.start`` 的 payload 里。退役角色在这条路上
   必须被拒（``WBError(VALIDATION)``），留着它只会让用例红在错误的原因上。
2. **直接写库、或手搭的历史行/内容 dict → 保留 ``"standard"`` 字面量。** 判据：它进的是
   ``repo.summary_insert(...)`` 的 ``role=`` / ``content={...}``，或测试自己造的 version dict。
   这些正是「退役前存的东西今天还读得出来」的证据；把它们一起改掉，就等于把这条保证删了。
   正证集中在 ``test_legacy_standard_rows_stay_readable_after_retirement`` 一处。
3. **角色清单本身 → 按退役更新断言。** 见 ``test_roles_endpoint_lists_every_role``。

与派单行表不一致的地方（按第 1/2 类判据核过代码才改判，已在交付里点名）：
``test_collection_overview.py:119``、``test_publish_gates.py:175``、
``test_summary_usage.py:156`` 实际是 ``repo.summary_insert`` 直接写库 → 保留；
``test_summary.py:366/374/386/391/444`` 实际走 ``run_job`` 生成 → 改成 ``wengao``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wb_parity_tiers import loose_env as loose_env  # noqa: F401  档位 fixture 由 pytest 注入

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
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage=stage
    )
    items = normalize(
        [
            {"start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": t}
            for i, t in enumerate(texts or ["今天讲三件事", "然后第一件事", "第二件事"])
        ]
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=items,
        language="zh",
        model="tiny",
        srt_path=str(root / "transcript.srt"),
    )
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
    body = summarizer.segments_text(
        [
            {"start_ms": 0, "end_ms": 900, "text": "开场"},
            {"start_ms": 3723000, "end_ms": 3724000, "text": "一小时处"},
        ]
    )
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
    body = summarizer.segments_text(
        [
            {"start_ms": 0, "text": "  "},
            {"start_ms": 1000, "text": "有内容"},
        ]
    )
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
        role="wengao",
    )
    assert result["summary"]["one_line"] == "这期讲三件事"
    assert result["role"] == "wengao"
    assert result["model"] == "stub-summary"
    # 同步断言（分类标准第 1 类）：原来 standard 是 passes=1，一次调用就出结果；
    # 换成本目录的默认方法论角色 wengao 之后走三轮回（生成 → 自我批判 → 修订），
    # 所以是 3 次。**这不是回归**，正是这次复刻要买的东西。
    assert result["calls"] == 3


def test_role_changes_the_system_prompt():
    """角色不是装饰：换一个**现役**角色必须换掉 system 提示词。

    第二个角色现读自 `ROLE_KEYS`（task-31 之前写死 `xueshu`，退役之后它连生成入口都进不去了，
    那条用例红的原因不是产品坏，是样本角色过期）。默认角色之外随便挑一个现役的够用：
    这里要证的是「角色真的参与提示词」，不是「某个特定角色的提示词长什么样」——
    特定角色的提示词由 `test_summary_prompt_source.py` 逐字钉。
    """
    other = next(key for key in summarizer.ROLE_KEYS if key != summarizer.DEFAULT_ROLE)
    one = StubClient([json.dumps(GOOD)])
    summarizer.run(
        {"title": "t"},
        [{"start_ms": 0, "text": "x"}],
        client=one,
        cfg=_cfg(),
        ctx=FakeCtx(),
        role=summarizer.DEFAULT_ROLE,
    )
    two = StubClient([json.dumps(GOOD)])
    summarizer.run(
        {"title": "t"},
        [{"start_ms": 0, "text": "x"}],
        client=two,
        cfg=_cfg(),
        ctx=FakeCtx(),
        role=other,
    )
    assert one.calls[0][0]["content"] != two.calls[0][0]["content"], (
        f"{summarizer.DEFAULT_ROLE} 与 {other} 的 system 相同——角色没参与提示词"
    )


def test_unknown_role_is_rejected():
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        summarizer.run(
            {"title": "t"},
            [{"start_ms": 0, "text": "x"}],
            client=StubClient([json.dumps(GOOD)]),
            cfg=_cfg(),
            ctx=FakeCtx(),
            role="nope",
        )
    assert exc.value.code == "VALIDATION"


def test_non_json_reply_raises_bad_response():
    from video_workbench.app.errors import WBError

    # 阶梯：主请求 →（截断才续写）→ 分批（正文/时间线两步）→ 重问 → 才失败。
    # 这里每次都给废话，所以四步都走完才抛。
    with pytest.raises(WBError) as exc:
        summarizer.run(
            {"title": "t"},
            [{"start_ms": 0, "text": "x"}],
            client=StubClient(
                ["我今天不想按格式回话", "分批正文也是废话", "分批时间线还是废话", "重问也还是废话"]
            ),
            cfg=_cfg(),
            ctx=FakeCtx(),
        )
    assert exc.value.code == "LLM_BAD_RESPONSE"


# 档位=宽松：重问一次是③；默认严格档一次即抛（parity I 组钉那边）。
def test_non_json_reply_is_asked_again_once(loose_env):
    """模型偶尔会把 JSON 说成人话；走完分批仍不行，重问一次能救回来就别让用户白跑一轮预读。"""
    from video_workbench.infra.jsonfix import repair_json

    assert repair_json(
        '{"one_line": "一行",' + chr(10) + ' "summary_md": "第一段' + chr(10) + '第二段"}'
    ), "字符串里的原始换行应该能修好"
    ctx = FakeCtx()
    result = summarizer.run(
        {"title": "t"},
        [{"start_ms": 0, "text": "x"}],
        client=StubClient(["我先说两句废话", "分批正文不行", "分批时间线也不行", json.dumps(GOOD)]),
        cfg=_cfg(),
        ctx=ctx,
    )
    assert result["summary"]["one_line"], "重问之后应该拿到一份总结"
    assert any("重问" in note for note in result.get("notes") or []), "没记下重问这件事"


def test_progress_reports_chinese_stages():
    ctx = FakeCtx()
    summarizer.run(
        {"title": "t"},
        [{"start_ms": 0, "text": "x"}],
        client=StubClient([json.dumps(GOOD)]),
        cfg=_cfg(),
        ctx=ctx,
    )
    stages = [stage for _value, stage in ctx.steps]
    assert "准备素材" in stages
    # 界面上不能出现 bili-scribe 的英文 stage。
    assert all(stage and stage.isascii() is False for stage in stages if stage)


# 档位=宽松：`response_format` 不被接受时退回普通请求，这段兜底在严格档 chat() 里被整段跳过。
def test_json_mode_rejected_falls_back_once(loose_env):
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
    result = summarizer.run(
        {"title": "t"}, [{"start_ms": 0, "text": "x"}], client=client, cfg=_cfg(), ctx=FakeCtx()
    )
    # 同步断言（分类标准第 1 类）：_cfg() 的默认角色换成 wengao 之后是三轮回，
    # 退回形状因此是 5 项——两个 JSON 请求（生成、修订）各配一次退回，
    # 中间那次「自我批判」本来就不开 json_mode。
    # 「每个 json_mode 请求都恰好有一次 False 紧跟其后」才是这条要守的东西。
    assert client.modes == [True, False, False, True, False]
    assert result["summary"]["one_line"]
    assert any("JSON 模式" in note for note in result["notes"])


def test_long_material_is_chunked_by_policy():
    """超过 max_input_chars 要分段预读——这是 bili-scribe 的核心能力，不能悄悄关掉。"""
    long_text = "长素材内容。" * 400  # ≈2400 字，配合调小的预算就会分块
    cfg = _cfg(max_input_chars=500, chunk_size=200, chunk_overlap=20)
    client = StubClient(["笔记一", "笔记二", json.dumps(GOOD)])
    result = summarizer.run(
        {"title": "t"},
        [{"start_ms": i * 1000, "text": long_text} for i in range(3)],
        client=client,
        cfg=cfg,
        ctx=FakeCtx(),
    )
    assert result["summary"]["chunks"] >= 2
    assert len(client.calls) > 1


def test_refine_requires_previous_and_keeps_material():
    from video_workbench.app.errors import WBError

    # 同步断言（分类标准第 1 类）：默认角色改成 wengao 之后是三轮回
    # （生成 → 批判 → 修订），最终那一版的字段来自**第三次**调用，
    # 所以 revise_note 要挂在脚本的第三项上；第一项只是草稿。
    client = StubClient(
        [
            json.dumps(GOOD),
            "诊断：正文偏长，要点没有排序。",
            json.dumps({**GOOD, "revise_note": "收紧了措辞"}),
        ]
    )
    with pytest.raises(WBError) as exc:
        summarizer.refine(
            {"title": "t"},
            [{"start_ms": 0, "text": "x"}],
            {},
            "再短一点",
            client=client,
            cfg=_cfg(),
            ctx=FakeCtx(),
        )
    assert exc.value.code == "CONFLICT"

    result = summarizer.refine(
        {"title": "t"},
        [{"start_ms": 0, "text": "内容还在"}],
        GOOD,
        "再短一点",
        client=client,
        cfg=_cfg(),
        ctx=FakeCtx(),
    )
    prompt = client.calls[0][1]["content"]
    assert "内容还在" in prompt  # 素材没被丢掉（否则会越改越远）
    assert "再短一点" in prompt
    assert result["summary"]["revise_note"] == "收紧了措辞"


def test_refine_rejects_too_short_requirement():
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        summarizer.refine(
            {"title": "t"},
            [{"start_ms": 0, "text": "x"}],
            GOOD,
            "改",
            client=StubClient([]),
            cfg=_cfg(),
            ctx=FakeCtx(),
        )
    assert exc.value.code == "VALIDATION"


# ------------------------------------------------------------------ 渲染


def test_markdown_carries_every_section():
    md = summarizer.to_markdown({**GOOD, "role": "wengao", "model": "m", "chunks": 2})
    for piece in (
        "这期讲三件事",
        "## 要点",
        "第一件",
        "## 时间线",
        "`00:01`",
        "## 评论用短版",
        "角色：wengao",
        "模型：m",
        "分段：2",
    ):
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
    result, ctx = run_job(
        service,
        monkeypatch,
        StubClient([json.dumps(GOOD)]),
        {"kind": "fresh", "role": "wengao"},
        project,
    )
    root = Path(service.repo.project_get(project["id"])["root_dir"])
    assert (root / "summary.md").is_file()
    assert (root / "summary.json").is_file()
    assert result["version"] == 1
    assert result["chars"] > 0
    saved = service.repo.summary_latest(project["id"])
    assert saved["content"]["one_line"] == "这期讲三件事"
    assert saved["role"] == "wengao"
    assert service.repo.project_get(project["id"])["stage"] == "summarized"
    assert any(
        item["kind"] == "summary" and item["path"] == str(root / "summary.md")
        for item in service.repo.asset_list(project["id"])
    )
    assert ctx.logs and "stub-summary" in ctx.logs[0]


def test_two_versions_both_kept_and_numbered(service, tmp_path, monkeypatch):
    # 第二版用哪个角色现读（原来写死 xueshu，task-31 退役后它进不了生成入口）。
    # 这条管的是「两版都在、版本号对、各自记住自己的角色」，角色只要是**两个不同的现役值**。
    second = next(key for key in summarizer.ROLE_KEYS if key != summarizer.DEFAULT_ROLE)
    project = make_project(service, tmp_path)
    run_job(
        service,
        monkeypatch,
        StubClient([json.dumps(GOOD)]),
        {"kind": "fresh", "role": summarizer.DEFAULT_ROLE},
        project,
    )
    run_job(
        service,
        monkeypatch,
        StubClient([json.dumps({**GOOD, "one_line": "第二版"})]),
        {"kind": "fresh", "role": second},
        project,
    )
    rows = service.repo.summary_list(project["id"])
    assert [row["version"] for row in rows] == [2, 1]
    assert {row["role"] for row in rows} == {summarizer.DEFAULT_ROLE, second}


def test_refine_creates_new_version_with_note(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    base = service.repo.summary_latest(project["id"])
    result, _ctx = run_job(
        service,
        monkeypatch,
        # 同步脚本长度（分类标准第 1 类）：默认角色改成 wengao 之后走三轮回，
        # 入库的是**修订那一次**的产出，所以 revise_note 挂在第三项。
        StubClient(
            [
                json.dumps(GOOD),
                "诊断：要点没有排序。",
                json.dumps({**GOOD, "revise_note": "把要点压到两条"}),
            ]
        ),
        {"kind": "refine", "requirement": "要点压到两条", "previous_id": base["id"]},
        project,
    )
    assert result["version"] == 2
    latest = service.repo.summary_latest(project["id"])
    assert latest["content"]["revise_note"] == "把要点压到两条"
    assert service.repo.summary_get(base["id"])["content"]["one_line"] == "这期讲三件事"


def test_restore_adds_a_version_instead_of_deleting(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    first = service.repo.summary_latest(project["id"])
    run_job(
        service,
        monkeypatch,
        StubClient([json.dumps({**GOOD, "one_line": "改过的"})]),
        {"kind": "fresh"},
        project,
    )
    data = rpc(service, "summary.restore", {"project_id": project["id"], "summary_id": first["id"]})
    assert data["summary"]["version"] == 3
    rows = service.repo.summary_list(project["id"])
    assert len(rows) == 3  # 历史一条没少
    assert rows[0]["content"]["one_line"] == "这期讲三件事"
    assert "恢复自第 1 版" in rows[0]["content"]["revise_note"]

    # 版本行必须把「恢复来源」当成来源，而不是拿它冒充实际内容改动。
    listed = rpc(service, "summary.versions", {"id": project["id"]})["versions"]
    assert listed[0]["version"] == 3
    assert listed[0]["restored_version"] == 1
    assert listed[0]["changed_fields"] is None


def test_version_rows_report_actual_content_changes(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, project)
    base = service.repo.summary_latest(project["id"])
    run_job(
        service,
        monkeypatch,
        StubClient(
            [
                json.dumps(GOOD),
                "诊断：只改一句话。",
                json.dumps({**GOOD, "one_line": "第二版一句话", "revise_note": "只改了开头"}),
            ]
        ),
        {"kind": "refine", "requirement": "只改开头", "previous_id": base["id"]},
        project,
    )
    rows = rpc(service, "summary.versions", {"id": project["id"]})["versions"]
    assert [row["version"] for row in rows] == [2, 1]
    assert rows[1]["changed_fields"] is None
    changed = {item["field"]: item for item in rows[0]["changed_fields"]}
    assert "one_line" in changed
    assert "summary_md" not in changed
    assert rows[0]["revise_note"] == "只改了开头"


def test_restore_rejects_foreign_project(service, tmp_path, monkeypatch):
    one = make_project(service, tmp_path)
    other = service.repo.project_create(title="别的", root_dir=str(tmp_path / "other"))
    run_job(service, monkeypatch, StubClient([json.dumps(GOOD)]), {"kind": "fresh"}, one)
    first = service.repo.summary_latest(one["id"])
    error = err(service, "summary.restore", {"project_id": other["id"], "summary_id": first["id"]})
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
    assert data["versions"][0]["role"] == "wengao"


def test_roles_endpoint_lists_every_role(service):
    """角色清单：退役的不再出现；清单 = 「两侧共有的那段」+「权威层自造的尾巴（现在为空）」。

    分类标准第 3 类（角色清单 → 按退役更新）：这条以前断言的是「八个角色都在」，
    其中 standard 还是默认位。退役之后它**不该再出现**——用户选到一个不能生成总结的角色，
    比少一个角色糟得多。顺序也钉住，因为那就是界面下拉的顺序。

    task-24 加了 `shipin` 之后改成两段式；task-31 把 `shipin` 合并进 `renhua`、把 `xueshu`
    退役之后，**后段合法地变成空**，那句 `assert extras` 删掉（它当初拦「新角色没进清单」，
    今天没有新角色要进清单；留着等于要求产品永远得有一个自造角色）。
    共有段改成现读：bs 的键序去掉权威层退役的那些——**不写死 6/7/8 任何一个**，
    本仓库已经因为写死名单在每次角色增删时红一整片（task-24 红 5 条、task-31 红 15 条）。
    现读不自证：锚点是逐字节锁定的 bs 原件（`test_summary_drift.py` 守着），
    而「退役集合等于哪三个键」这个身份信息只由 `test_summary_retired_roles.py` 钉一份。
    """
    from bili_scribe.core import roles as bs_roles

    data = rpc(service, "summary.roles")
    keys = [item["key"] for item in data["roles"]]
    bs_keys = [role.key for role in bs_roles.ALL_ROLES]
    retired = set(summarizer.RETIRED_ROLES)
    shared = [key for key in bs_keys if key not in retired]
    assert keys[: len(shared)] == shared, f"共有角色那一段漂了：期望 {shared} 开头，实得 {keys}"
    tail = keys[len(shared) :]
    assert all(key not in bs_keys for key in tail), (
        f"后段 {tail} 里出现了 bs 已有的角色：前段被删短了，不是新角色进来了"
    )
    assert "standard" not in keys, "退役角色还在清单里，用户选了就会拿到 VALIDATION"
    assert data["default"] in keys
    assert data["default"] == "wengao"


def test_legacy_standard_rows_stay_readable_after_retirement(service, tmp_path, monkeypatch):
    """退役只关**生成入口**，不关历史数据——分类标准第 2 类的正证。

    这条是专门补的：库里有退役前留下的版本行（role 写死 "standard"，直接经 repo 入库、
    不经过生成），它们必须仍然列得出来、取不出来会丢、也重新渲染得了。
    把「测试里还留着 standard 字面量」这件事写成一条有针对性的用例，而不是散落在各处的
    残留：散落会让人误以为漏改了，而这条让「历史可读」变成一个有人守的承诺。
    """
    from video_workbench.core import summarizer

    project = make_project(service, tmp_path)
    row = service.repo.summary_insert(
        project["id"],
        content={**GOOD, "role": "standard"},
        content_md=summarizer.to_markdown({**GOOD, "role": "standard"}),
        role="standard",
        model="stub-legacy",
    )

    assert [item["role"] for item in service.repo.summary_list(project["id"])] == ["standard"]
    assert service.repo.summary_latest(project["id"])["id"] == row["id"]
    assert service.repo.summary_get(row["id"])["content"]["one_line"] == GOOD["one_line"]

    data = rpc(service, "summary.get", {"id": project["id"]})
    assert data["found"] is True
    assert data["versions"][0]["role"] == "standard"
    # 界面上要能解释这个名字，不能抛——role_label 是退役角色的唯一出口。
    assert "已停用" in summarizer.role_label("standard")
    # 而生成入口必须拒绝它。summary_start 先查「模型配好了没」再查角色
    # （service.py:2542-2550），所以要先把假客户端装上，否则拿到的是
    # LLM_NOT_CONFIGURED，测不到角色这一层。
    configure(service, monkeypatch, StubClient([json.dumps(GOOD)]))
    error = err(service, "summary.start", {"project_id": project["id"], "role": "standard"})
    assert error["code"] == "VALIDATION"
    assert "standard" in error["message"], f"错误要说清是哪个角色不行：{error['message']}"


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
    # 四次都不是 JSON：主请求 → 分批正文 → 分批时间线 → 重问。分批也救不回来才该失败。
    configure(
        service,
        monkeypatch,
        StubClient(["不是 JSON", "分批正文不是", "分批时间线不是", "还不是 JSON"]),
    )
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
    cfg.llm.role = "wengao"
    return cfg
