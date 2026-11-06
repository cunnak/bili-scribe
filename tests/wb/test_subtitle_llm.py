"""字幕 LLM 优化的纯函数测试（P2-04）。

这个模块最该被测试的不是"模型好不好"，而是**模型说错话时我们怎么办**：
解析烂了、编了新行、偷偷扩写、偷偷删内容。每一条都必须退回原文，
而不是把半截结果写进用户字幕。
"""

from __future__ import annotations

import json

import pytest

from video_workbench.core import subtitle_llm as ollm
from video_workbench.core import subtitles as subs
from video_workbench.infra.llm import ChatError


def segs(*texts):
    out = []
    for i, text in enumerate(texts):
        out.append(
            {"id": f"s{i + 1}", "start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": text}
        )
    return out


class StubClient:
    """按顺序吐预先准备好的回复；回复可以是 str，也可以是异常。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.model = "stub-model"
        self.base_url = "http://stub"
        self.calls = []

    @property
    def configured(self):
        return True

    def require(self):
        return None

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.replies:
            return "[]"
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------- prompt


def test_prompt_contains_every_id_and_no_placeholder_left():
    items = segs("嗯那个", "我们今天讲三件事")
    text = ollm.build_prompt(items)
    assert "$count" not in text and "$lines" not in text
    assert "s1" in text and "s2" in text
    assert "共 2 行" in text


def test_split_batches_keeps_order_and_last_short_batch():
    items = segs(*[f"第{i}句" for i in range(7)])
    groups = ollm.split_batches(items, 3)
    assert [len(g) for g in groups] == [3, 3, 1]
    assert groups[0][0]["id"] == "s1" and groups[-1][0]["id"] == "s7"


def test_split_batches_treats_zero_size_as_default():
    """0 不该切成"每行一批"，那是把 batch_size 当开关用了。"""
    assert len(ollm.split_batches(segs("a", "b"), 0)) == 1


# ---------------------------------------------------------------- 解析


def test_parse_accepts_plain_array():
    out = ollm.parse_reply('[{"id":"s1","text":"你好。"}]')
    assert out == {"s1": "你好。"}


def test_parse_accepts_fenced_and_chatty_reply():
    raw = '好的，这是结果：\n```json\n[{"id":"s1","text":"你好。"}]\n```\n以上。'
    assert ollm.parse_reply(raw) == {"s1": "你好。"}


def test_parse_rejects_empty_reply():
    with pytest.raises(ChatError) as err:
        ollm.parse_reply("   ")
    assert err.value.code == "LLM_BAD_RESPONSE"


def test_parse_rejects_non_json():
    with pytest.raises(ChatError):
        ollm.parse_reply("抱歉，我无法处理。")


def test_parse_keeps_empty_text_for_merge():
    out = ollm.parse_reply('[{"id":"s1","text":"你好"},{"id":"s2","text":""}]')
    assert out["s2"] == ""


# ---------------------------------------------------------------- 校验


def test_check_batch_ok_for_reasonable_edit():
    before = segs("嗯那个我们今天讲三件事", "然后然后第二件事")
    after = {"s1": "我们今天讲三件事。", "s2": "第二件事。"}
    assert ollm.check_batch(before, after) == []


def test_check_batch_flags_missing_and_extra_ids():
    problems = ollm.check_batch(segs("a", "b"), {"s1": "a。"})
    assert any("缺少行" in p for p in problems)
    problems = ollm.check_batch(segs("a"), {"s1": "a。", "s9": "x"})
    assert any("多出" in p for p in problems)


def test_check_batch_flags_expansion_and_loss():
    before = segs("好")
    problems = ollm.check_batch(before, {"s1": "这是一段模型自己编出来的很长很长的话。"})
    assert any("扩写" in p for p in problems)
    before = segs("我们今天来讨论一个非常重要而且复杂的问题")
    problems = ollm.check_batch(before, {"s1": "重要"})
    assert any("丢内容" in p for p in problems)


def test_check_batch_flags_newline_and_too_long():
    assert any("换行" in p for p in ollm.check_batch(segs("a"), {"s1": "a\nb"}))
    long_text = "啊" * (ollm.MAX_CHARS + 5)
    assert any("太长" in p for p in ollm.check_batch(segs(long_text), {"s1": long_text}))


def test_check_batch_allows_empty_for_merge():
    assert ollm.check_batch(segs("嗯", "正文"), {"s1": "", "s2": "正文。"}) == []


# ---------------------------------------------------------------- 变化与应用


def test_build_changes_only_keeps_real_diffs():
    before = segs("嗯那个", "已正确")
    after = {"s1": "那个", "s2": "已正确。"}
    changes = ollm.build_changes(before, after)
    assert [c["id"] for c in changes] == ["s1", "s2"]
    assert changes[0]["before"] == "嗯那个" and changes[0]["after"] == "那个"
    assert changes[0]["kind"] == "text"


def test_build_changes_marks_empty_as_merge():
    changes = ollm.build_changes(segs("嗯"), {"s1": ""})
    assert changes[0]["kind"] == "merge"


def test_apply_changes_writes_text_and_merges_empty_into_previous():
    before = segs("今天讲三件事", "嗯", "第一件")
    changes = [
        {"id": "s1", "before": "今天讲三件事", "after": "今天讲三件事。", "kind": "text"},
        {"id": "s2", "before": "嗯", "after": "", "kind": "merge"},
    ]
    out = ollm.apply_changes(before, changes)
    assert [s["text"] for s in out] == ["今天讲三件事。", "第一件"]
    # 合并只吃掉被并走那段的终点，不挪后一段的起点
    assert out[0]["start_ms"] == 0
    assert out[0]["end_ms"] == 1900
    assert out[1]["start_ms"] == 2000


def test_apply_changes_honours_accepted_subset():
    before = segs("嗯", "啊")
    changes = [
        {"id": "s1", "before": "嗯", "after": "", "kind": "merge"},
        {"id": "s2", "before": "啊", "after": "", "kind": "merge"},
    ]
    out = ollm.apply_changes(before, changes, {"s2"})
    assert [s["text"] for s in out] == ["嗯"]


def test_apply_changes_without_changes_is_noop():
    before = segs("a", "b")
    assert ollm.apply_changes(before, []) == subs.normalize(before)


# ---------------------------------------------------------------- 主流程


def test_optimize_collects_changes_and_reports_progress():
    client = StubClient(
        [
            json.dumps(
                [{"id": "s1", "text": "我们今天讲三件事。"}, {"id": "s2", "text": "第一件事。"}]
            ),
        ]
    )
    progress = []
    out = ollm.optimize(
        segs("嗯那个我们今天讲三件事", "第一件事"),
        client,
        batch_size=10,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert out["batches"] == 1 and out["segments"] == 2
    assert out["changed"] == 2 and out["failed"] == []
    assert progress == [(1, 1)]
    assert out["model"] == "stub-model"


def test_optimize_isolates_failed_batch_and_keeps_others():
    """第二批炸了，第一批的改动照样要留着——这是分批的价值。"""
    good = json.dumps([{"id": "s1", "text": "第一段。"}])
    client = StubClient([good, ChatError("LLM_ERROR", "超时了")])
    out = ollm.optimize(segs("第一段", "第二段"), client, batch_size=1)
    assert out["batches"] == 2
    assert [c["id"] for c in out["changes"]] == ["s1"]
    assert len(out["failed"]) == 1 and out["failed"][0]["code"] == "LLM_ERROR"


def test_optimize_marks_bad_reply_as_failed_not_applied():
    client = StubClient(["我觉得这段字幕没问题（但没给 JSON）"])
    out = ollm.optimize(segs("原文"), client)
    assert out["changes"] == []
    assert out["failed"][0]["code"] == "LLM_BAD_RESPONSE"


def test_optimize_cancel_stops_further_batches():
    client = StubClient([json.dumps([{"id": "s1", "text": "第一段。"}])])
    out = ollm.optimize(segs("第一段", "第二段"), client, batch_size=1, cancel=lambda: True)
    assert out["changes"] == []
    assert out["failed"][0]["code"] == "CANCELED"


def test_optimize_requires_client_configuration():
    class Bare(StubClient):
        @property
        def configured(self):
            return False

        def require(self):
            raise ChatError("LLM_NOT_CONFIGURED", "没有配置")

    with pytest.raises(ChatError) as err:
        ollm.optimize(segs("a"), Bare([]))
    assert err.value.code == "LLM_NOT_CONFIGURED"


def test_optimize_rejects_empty_input():
    with pytest.raises(ChatError):
        ollm.optimize([], StubClient([]))
