"""合集总览（P2-08）的测试。

要证明的是五件事：

1. **输入只认子项总结**：不重读视频；有子项缺总结就不许出（否则等于让模型编）；
2. **标题对得上**：模型爱缩写标题、爱加书名号，对不上的条目必须丢掉，不能变成野链接；
3. **不覆盖子项**：总览写 ``scope='collection'``，子项那版总结一个字不动；
4. **能重出**：每次重出加一版，版本号连续；
5. **失败说得清**：模型不吐 JSON 时报 LLM_BAD_RESPONSE，且不留半条脏数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import overview as ov


GOOD = {
    "theme": "用三集把「怎么做研究」拆成可以照做的手册",
    "conclusions": [
        "先摆反例，比先讲方法有效",
        "把变量拆开逐个对比",
        "最后落到可复现的操作顺序",
    ],
    "timeline": [
        {"title": "第一集：反例", "one_line": "先讲直觉为什么不可靠", "url": "https://b23.tv/a"},
        {"title": "第二集：变量", "one_line": "拆成单变量对比", "url": "https://b23.tv/b"},
        {"title": "第三集：操作", "one_line": "给出一套可复现步骤", "url": "https://b23.tv/c"},
    ],
    "overlap": [{"kind": "重复", "text": "第二、三集都在讲对照组"}],
    "watch_order": [
        {"title": "第一集：反例", "reason": "先破后立"},
        {"title": "第二集：变量", "reason": "承接反例"},
        {"title": "第三集：操作", "reason": "落到动手"},
    ],
}


class StubClient:
    def __init__(self, replies, model="stub-overview"):
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


def child(project, one_line="一句话", points=None):
    return {
        "project": project,
        "summary": {"version": 1, "content": {"one_line": one_line,
                                             "key_points": points or ["要点一", "要点二"]}},
    }


def make_children(service, tmp_path: Path, count=3, *, collection="研究方法", with_summary=True):
    out = []
    for index in range(count):
        root = tmp_path / "media" / "UP" / collection / f"第{index + 1}集"
        root.mkdir(parents=True, exist_ok=True)
        project = service.repo.project_create(
            title=f"第{index + 1}集：反例" if index == 0 else f"第{index + 1}集：变量",
            up_name="UP", root_dir=str(root), stage="summarized",
            collection_title=collection, bvid=f"BV{index}",
        )
        if with_summary:
            service.repo.summary_insert(
                project["id"],
                content={"one_line": f"第{index + 1}集讲了什么", "key_points": ["要点"]},
                content_md="# 标题\n正文",
                role="standard",
                model="stub",
                status="ready",
            )
        out.append(project)
    return out


# ---------------------------------------------------------------- 素材与 prompt


def test_items_are_built_from_child_summaries():
    items = ov.collection_items([child({"title": "第一集", "source_url": "https://b23.tv/a"})])
    assert items[0]["title"] == "第一集"
    assert items[0]["one_line"] == "一句话"
    assert items[0]["url"] == "https://b23.tv/a"


def test_items_keep_at_most_four_points():
    items = ov.collection_items([child({"title": "x"}, points=[f"要点{i}" for i in range(9)])])
    assert len(items[0]["points"]) == 4


def test_prompt_fills_every_placeholder():
    template = ov.load_prompt()
    text = ov.build_prompt("合集名", ov.collection_items([child({"title": "第一集"}),
                                                         child({"title": "第二集"})]))
    assert "$collection_title" not in text and "$count" not in text and "$items" not in text
    assert "合集名" in text and "第一集" in text and "第二集" in text


def test_long_material_falls_back_to_tight_mode(monkeypatch):
    monkeypatch.setattr(ov, "MAX_CHARS", 200)
    items = ov.collection_items([child({"title": f"第{i}集"}, points=[f"要点{j}" for j in range(4)])
                                 for i in range(5)])
    text = ov.build_prompt("合集", items)
    # 压缩模式下每条只留两条要点："要点2" 不该出现
    assert "要点2" not in text


def test_parse_accepts_fenced_reply():
    content = ov.parse_collection("```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```")
    assert content["theme"] == GOOD["theme"]
    assert len(content["conclusions"]) == 3


def test_parse_truncates_to_seven_conclusions():
    payload = dict(GOOD, conclusions=[f"结论{i}" for i in range(12)])
    content = ov.parse_collection(json.dumps(payload, ensure_ascii=False))
    assert len(content["conclusions"]) == 7


def test_parse_rejects_empty_and_non_json_and_missing_theme():
    for bad in ("", "我不想按格式回话", json.dumps({"conclusions": ["x"]})):
        with pytest.raises(WBError) as exc:
            ov.parse_collection(bad)
        assert exc.value.code == "LLM_BAD_RESPONSE"


def test_conclusions_as_string_is_tolerated():
    payload = dict(GOOD)
    payload["conclusions"] = "只有一条"
    assert ov.parse_collection(json.dumps(payload))["conclusions"] == ["只有一条"]


# ---------------------------------------------------------------- 幻觉与对齐


def test_timeline_entries_that_match_nothing_are_dropped():
    items = ov.collection_items([
        child({"title": "第一集：反例", "source_url": "https://b23.tv/a"}),
        child({"title": "第二集：变量"}),
    ])
    payload = dict(GOOD)
    payload["timeline"] = [
        {"title": "第一集：反例", "one_line": "讲了反例", "url": "模型乱编的链接"},
        {"title": "番外：不存在的第四集", "one_line": "编的", "url": "https://b23.tv/zzz"},
    ]
    fixed, notes = ov.reconcile_collection(payload, items)
    assert len(fixed["timeline"]) == 1
    # 链接要用素材里的真链接，不是模型编的那个
    assert fixed["timeline"][0]["url"] == "https://b23.tv/a"
    assert any("对不上任何子项" in note for note in notes)


def test_title_is_restored_to_project_spelling():
    items = ov.collection_items([child({"title": "第3集：操作顺序"})])
    payload = dict(GOOD)
    payload["timeline"] = [{"title": "「第 3 集 操作顺序」", "one_line": "x", "url": ""}]
    fixed, _notes = ov.reconcile_collection(payload, items)
    assert fixed["timeline"][0]["title"] == "第3集：操作顺序"


# ---------------------------------------------------------------- 运行与约束


def test_run_refuses_collection_with_one_item():
    with pytest.raises(WBError) as exc:
        ov.run("单集", [child({"title": "唯一一集"})], client=StubClient([]),
               ctx=FakeCtx())
    assert exc.value.code == "CONFLICT"


def test_run_refuses_when_a_child_has_no_summary():
    with pytest.raises(WBError) as exc:
        ov.run("合集", [child({"title": "有"}), child({"title": "没有"}, one_line="")],
               client=StubClient([]), ctx=FakeCtx())
    assert exc.value.code == "CONFLICT"
    assert "缺总结" in exc.value.detail


def test_run_sends_one_request_and_returns_structure():
    client = StubClient([json.dumps(GOOD, ensure_ascii=False)])
    ctx = FakeCtx()
    result = ov.run("合集", [child({"title": "第一集：反例"}), child({"title": "第二集：变量"})],
                    client=client, ctx=ctx, role="standard")
    assert result["calls"] == 1
    assert result["content"]["theme"] == GOOD["theme"]
    assert result["content"]["item_count"] == 2
    assert any("共 2 集" in line for line in [ov.to_markdown(result["content"])])


def test_run_stores_notes_inside_content():
    """模型给的野标题被丢掉这件事，必须写进 content.notes。

    落库的是 content、界面读的也是 content.notes；只放进 run() 的返回值，
    用户就只会看到"模型给了 4 条 timeline，界面只有 3 条"而没有任何解释——
    这条曾经真的坏过（验收脚本 [FAIL] 野条目记进了 notes）。
    """
    wild = dict(GOOD)
    wild["timeline"] = [
        {"title": "第一集", "one_line": "a", "url": ""},
        {"title": "完全不存在的一集", "one_line": "b", "url": ""},
    ]
    client = StubClient([json.dumps(wild, ensure_ascii=False)])
    result = ov.run("合集", [child({"title": "第一集"}), child({"title": "第二集"})],
                    client=client, ctx=FakeCtx())
    notes = result["content"]["notes"]
    assert notes, "notes 没进 content"
    assert any("对不上" in line for line in notes), notes
    assert notes == result["notes"], "两处 notes 应该一致"


def test_run_does_not_use_role_three_pass_prompts():
    """合集总览只发一次请求——套角色三轮回会多花两倍钱且写得更不像总览。"""
    client = StubClient([json.dumps(GOOD, ensure_ascii=False)])
    ov.run("合集", [child({"title": "一"}), child({"title": "二"})],
           client=client, ctx=FakeCtx(), role="xueshu")
    assert len(client.calls) == 1


def test_markdown_has_disclaimer():
    body = ov.to_markdown({"collection_title": "合集", "theme": "主题",
                           "conclusions": ["结论"], "item_count": 3, "model": "m"})
    assert "AI 依据各集总结生成" in body


# ---------------------------------------------------------------- 服务层


def test_collection_list_groups_children(service, tmp_path):
    make_children(service, tmp_path, count=3)
    data = rpc(service, "collection.list")
    row = next(item for item in data["collections"] if item["title"] == "研究方法")
    assert row["items"] == 3
    assert row["summarized"] == 3
    assert row["ready"] is True
    assert row["has_overview"] is False


def test_overview_rejects_unknown_collection(service):
    error = err(service, "summary.collection.overview", {"collection": "不存在的合集"})
    assert error["code"] == "CONFLICT"


def test_overview_rejects_missing_child_summary(service, tmp_path):
    projects = make_children(service, tmp_path, count=3)
    # 删掉第三集的总结
    service.repo.summary_delete(service.repo.summary_latest(projects[2]["id"])["id"])
    error = err(service, "summary.collection.overview", {"collection": "研究方法"})
    assert error["code"] == "CONFLICT"
    assert "还没有总结" in error["message"]


def test_overview_requires_llm(service, tmp_path):
    make_children(service, tmp_path, count=3)
    error = err(service, "summary.collection.overview", {"collection": "研究方法"})
    assert error["code"] == "LLM_NOT_CONFIGURED"


def test_overview_job_writes_row_files_and_keeps_children(service, tmp_path, monkeypatch):
    projects = make_children(service, tmp_path, count=3)
    before = {item["id"]: len(service.repo.summary_list(item["id"])) for item in projects}
    client = StubClient([json.dumps(GOOD, ensure_ascii=False)])
    service.cfg.override_llm(base_url=client.base_url, api_key="sk-test", model="stub-model")
    service.cfg.llm.model = client.model
    monkeypatch.setattr(service, "_llm_client", lambda task="summary": client)

    result = service._collection_job(FakeCtx(), {"collection": "研究方法", "role": "standard"})
    assert result["version"] == 1
    assert result["items"] == 3

    collection_id = service.repo.collection_id_for("研究方法")
    row = service.repo.collection_summary_latest(collection_id)
    assert row["scope"] == "collection"
    assert row["project_id"] is None
    assert row["content"]["theme"] == GOOD["theme"]
    # 子项一版都没多
    after = {item["id"]: len(service.repo.summary_list(item["id"])) for item in projects}
    assert after == before
    # 文件落在合集目录（公共父目录）上
    root = tmp_path / "media" / "UP" / "研究方法"
    assert (root / "collection_overview.md").exists()
    assert (root / "collection_overview.json").exists()
    assert (root / "collection_overview.v1.json").exists()

    second = service._collection_job(FakeCtx(), {"collection": "研究方法"})
    assert second["version"] == 2


def test_overview_get_accepts_title_or_id(service, tmp_path, monkeypatch):
    make_children(service, tmp_path, count=3)
    client = StubClient([json.dumps(GOOD, ensure_ascii=False)])
    service.cfg.override_llm(base_url=client.base_url, api_key="sk-test", model="stub-model")
    service.cfg.llm.model = client.model
    monkeypatch.setattr(service, "_llm_client", lambda task="summary": client)
    service._collection_job(FakeCtx(), {"collection": "研究方法"})

    by_title = rpc(service, "summary.collection.get", {"id": "研究方法"})
    assert by_title["found"] is True
    assert by_title["overview"]["content"]["theme"] == GOOD["theme"]
    assert len(by_title["versions"]) == 1
    assert len(by_title["children"]) == 3

    by_id = rpc(service, "summary.collection.get", {"id": by_title["collection"]["id"]})
    assert by_id["overview"]["content"]["theme"] == GOOD["theme"]


def test_overview_failure_leaves_no_row(service, tmp_path, monkeypatch):
    make_children(service, tmp_path, count=3)
    client = StubClient(["我不按格式回话"])
    service.cfg.override_llm(base_url=client.base_url, api_key="sk-test", model="stub-model")
    service.cfg.llm.model = client.model
    monkeypatch.setattr(service, "_llm_client", lambda task="summary": client)
    with pytest.raises(WBError) as exc:
        service._collection_job(FakeCtx(), {"collection": "研究方法"})
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert service.repo.collection_summary_latest(
        service.repo.collection_id_for("研究方法")) is None


def test_overview_start_returns_job(service, tmp_path, monkeypatch):
    make_children(service, tmp_path, count=3)
    client = StubClient([])
    service.cfg.override_llm(base_url=client.base_url, api_key="sk-test", model="stub-model")
    service.cfg.llm.model = client.model
    monkeypatch.setattr(service, "_llm_client", lambda task="summary": client)
    data = rpc(service, "summary.collection.overview", {"collection": "研究方法"})
    assert data["items"] == 3
    assert data["job"]["type"] == "summary.collection"
