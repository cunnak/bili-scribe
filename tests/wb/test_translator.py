"""翻译核心（P2-05）的单元测试。

覆盖四件"错了会静默出怪结果"的事：

1. 术语表解析与**长词优先**（"AI 芯片"不能被 "AI" 抢先换掉）；
2. ±2 上下文真的进了 prompt，而且跨批边界也给；
3. 单批失败只脏那几段，别的段的译文保住（这是"可重试"的前提）；
4. 双语四种排布的行序，以及 ASS 转义（花括号和换行是 ASS 的控制字符）。
"""

from __future__ import annotations

import json

import pytest

from video_workbench.core import translator
from video_workbench.core.subtitles import format_ass
from video_workbench.infra.llm import ChatError


class StubClient:
    """按调用顺序回吐回复；元素是异常就抛出。"""

    def __init__(self, replies, model="stub-model"):
        self.replies = list(replies)
        self.model = model
        self.base_url = "http://stub.local/v1"
        self.calls: list[list[dict]] = []

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


def segs(*texts, start=0):
    """造字幕段：id 依次 s1/s2…，每段 1 秒。"""
    return [
        {"id": f"s{i + 1}", "start_ms": (start + i) * 1000,
         "end_ms": (start + i) * 1000 + 900, "text": t, "words": []}
        for i, t in enumerate(texts)
    ]


# ---------------------------------------------------------------- 源语言与目标语言


def test_detect_source_japanese_wins_over_cjk():
    # 日文里混大量汉字，只看 CJK 会把日文判成中文
    assert translator.detect_source(segs("日本語です。今日はいい天気。")) == "ja"


def test_detect_source_chinese_and_english():
    assert translator.detect_source(segs("今天讲三件事")) == "zh"
    assert translator.detect_source(segs("hello there my friend")) == "en"


def test_resolve_target_auto_rules():
    # 方案书：源为中文默认译英文，其余默认译中文
    assert translator.resolve_target("auto", segs("中文句子")) == "en"
    assert translator.resolve_target("auto", segs("english sentence")) == "zh"
    assert translator.resolve_target("ja", segs("中文句子")) == "ja"


# ---------------------------------------------------------------- 术语表


def test_parse_glossary_accepts_loose_forms():
    pairs = translator.parse_glossary([
        "Transformer=Transformer 模型",
        " ＡＩ ＝ 人工智能 ",   # 全角等号 + 空格
        {"source": "KV Cache", "target": "KV 缓存"},
        "没有等号的一行",
    ])
    got = dict(pairs)
    assert got["Transformer"] == "Transformer 模型"
    assert got["ＡＩ"] == "人工智能"           # 全角等号与空格都要认
    assert got["KV Cache"] == "KV 缓存"
    assert "没有等号的一行" not in got


def test_parse_glossary_last_write_wins_and_sorts_long_first():
    pairs = translator.parse_glossary(["AI=人工智能", "AI 芯片=AI chip", "AI=智能"])
    assert pairs[0][0] == "AI 芯片"          # 长词必须在前
    assert dict(pairs)["AI"] == "智能"        # 后写的覆盖先写的


def test_apply_glossary_does_not_eat_longer_term():
    pairs = translator.parse_glossary(["AI=人工智能", "AI 芯片=芯片"])
    # 按长词优先替换，"AI 芯片" 整体命中，不会留下半个"芯片"
    assert translator.apply_glossary("AI 芯片很贵", pairs) == "芯片很贵"


def test_format_glossary_empty_is_explicit():
    assert translator.format_glossary([]) == "（无）"


# ---------------------------------------------------------------- prompt


def test_build_prompt_fills_every_placeholder():
    items = segs("第一句", "第二句", "第三句", "第四句", "第五句", "第六句", "第七句", "第八句")
    batch = items[3:5]          # s4、s5
    text = translator.build_prompt(batch, target="en", context=items,
                                   glossary=translator.parse_glossary(["AI=人工智能"]),
                                   template="T=$target_name|G=$glossary|B=$context_before|L=$lines|A=$context_after")
    assert "$" not in text
    assert "T=英文" in text
    assert "AI = 人工智能" in text
    # ±2：本批前两句、后两句进 prompt，第三句以外不给（多给会稀释小模型）
    assert "第一句" not in text
    assert "第二句" in text and "第三句" in text
    assert "第六句" in text and "第七句" in text
    assert "第八句" not in text
    assert "s4" in text and "s5" in text


def test_split_batches_keeps_order_and_tail():
    items = segs(*["句"] * 7)
    groups = translator.split_batches(items, 3)
    assert [len(g) for g in groups] == [3, 3, 1]
    assert [g[0]["id"] for g in groups] == ["s1", "s4", "s7"]


# ---------------------------------------------------------------- 解析与校验


def test_parse_reply_tolerates_fence_and_chatter():
    got = translator.parse_reply('好的：\n```json\n[{"id":"s1","translation":"hello"}]\n```')
    assert got == {"s1": "hello"}


def test_parse_reply_rejects_non_json():
    with pytest.raises(ChatError) as exc:
        translator.parse_reply("我不会译")
    assert exc.value.code == "LLM_BAD_RESPONSE"


def test_check_batch_ignores_length_but_blocks_missing_and_newline():
    batch = segs("一二三", "四五六")
    # 译文长短不管：中→英本来就不成比例
    assert translator.check_batch(batch, {"s1": "a very long english sentence indeed",
                                          "s2": "ok enough"}) == []
    assert translator.check_batch(batch, {"s1": "两\n行", "s2": "ok enough"})
    problems = translator.check_batch(batch, {})
    assert any("漏行" in p or "漏了" in p for p in problems)


# ---------------------------------------------------------------- 主流程


def test_translate_happy_path_returns_full_segments():
    items = segs("今天讲三件事", "然后第一件事", "第二件事")
    client = StubClient([json.dumps([{"id": s["id"], "text": f"en-{s['id']}"} for s in items])])
    out = translator.translate(items, client, target="en", batch_size=10)
    assert out["translated"] == 3 and out["total"] == 3 and out["failed"] == []
    assert [s["translated_text"] for s in out["segments"]] == ["en-s1", "en-s2", "en-s3"]
    assert out["target"] == "en"


def test_translate_drops_extra_ids_from_reply():
    # 模型顺手把"上文参考"也译了：多余的 id 必须丢掉，不能污染那些段
    items = segs("一", "二")
    client = StubClient([json.dumps([{"id": "s1", "text": "one"}, {"id": "s2", "text": "two"},
                                     {"id": "s9", "text": "ghost"}])])
    out = translator.translate(items, client, target="en")
    assert {s["id"] for s in out["segments"]} == {"s1", "s2"}
    assert all("ghost" not in str(s.get("translated_text")) for s in out["segments"])


def test_one_batch_failure_keeps_other_batches():
    items = segs("一", "二", "三", "四")
    client = StubClient([
        json.dumps([{"id": "s1", "text": "one"}, {"id": "s2", "text": "two"}]),
        ChatError("LLM_ERROR", "超时"),
    ])
    out = translator.translate(items, client, target="en", batch_size=2)
    assert out["translated"] == 2
    assert [s["translated_text"] for s in out["segments"]] == ["one", "two", "", ""]
    failed_ids = out["failed"][0]["ids"]
    assert failed_ids == ["s3", "s4"] and out["failed"][0]["code"] == "LLM_ERROR"


def test_retry_ids_only_rerun_selected_segments():
    items = segs("一", "二", "三")
    for s in items:
        s["translated_text"] = f"old-{s['id']}"
    client = StubClient([json.dumps([{"id": "s2", "text": "new-two"}])])
    out = translator.translate(items, client, target="en", batch_size=10, retry_ids=["s2"])
    assert len(client.calls) == 1
    assert [s["translated_text"] for s in out["segments"]] == ["old-s1", "new-two", "old-s3"]


def test_retry_counts_only_this_run_not_leftovers():
    """重试两段时 translated 只算这两段，filled 才算全量。

    合并成一个数会让"整批都失败"看起来像成功——库里还留着上次的译文，
    一加就是满篇，服务层就判不出这次其实一条都没译出来。
    """
    items = segs("一", "二", "三")
    for s in items:
        s["translated_text"] = f"old-{s['id']}"
    client = StubClient([json.dumps([{"id": "s2", "text": "new-two"}])])
    out = translator.translate(items, client, target="en", retry_ids=["s2"])
    assert out["translated"] == 1
    assert out["filled"] == 3
    assert out["total"] == 3


def test_retry_that_fails_every_batch_reports_zero_translated():
    items = segs("一", "二", "三")
    for s in items:
        s["translated_text"] = f"old-{s['id']}"
    client = StubClient(["这不是 JSON"])
    out = translator.translate(items, client, target="en", retry_ids=["s1", "s2"])
    assert out["translated"] == 0, "本次一条都没译出来，不能拿旧译文凑数"
    assert out["filled"] == 3, "旧译文还在（失败不该清空已确认的内容）"
    assert len(out["failed"]) == 1 and out["failed"][0]["code"] == "LLM_BAD_RESPONSE"


def test_translate_rejects_bad_target():
    with pytest.raises(ChatError):
        translator.translate(segs("一"), StubClient(["[]"]), target="fr")


def test_translate_applies_glossary_to_final_text():
    # prompt 里说了不算，落定再强制替换一次：管的是模型把源词原样带出来的情况
    items = segs("AI 很火")
    client = StubClient([json.dumps([{"id": "s1", "text": "AI is popular"}])])
    out = translator.translate(items, client, target="en", glossary=["AI=人工智能"])
    assert out["segments"][0]["translated_text"] == "人工智能 is popular"


# ---------------------------------------------------------------- 双语与 ASS


def test_bilingual_four_modes():
    items = [{"id": "s1", "start_ms": 0, "end_ms": 900, "text": "原文", "translated_text": "译文"}]
    assert translator.bilingual_lines(items[0], "original_top") == ["原文", "译文"]
    assert translator.bilingual_lines(items[0], "translated_top") == ["译文", "原文"]
    assert translator.bilingual_lines(items[0], "original_only") == ["原文"]
    assert translator.bilingual_lines(items[0], "translated_only") == ["译文"]
    # 没译文的段退回原文单行，不能输出空字幕
    assert translator.bilingual_lines({"text": "只有原文", "translated_text": ""},
                                      "translated_only") == ["只有原文"]


def test_merge_bilingual_rejects_bad_mode():
    with pytest.raises(ChatError):
        translator.merge_bilingual(segs("一"), "side_by_side")


def test_format_ass_two_lines_and_escaping():
    merged = translator.merge_bilingual(
        [{"id": "s1", "start_ms": 65_000, "end_ms": 71_200, "text": "你好{世界}",
          "translated_text": "hello"}],
        "original_top")
    text = format_ass(merged)
    assert "PlayResX: 1280" in text
    assert "Dialogue: 0,0:01:05.00,0:01:11.20" in text
    assert "你好（世界）" in text              # 花括号必须换掉，否则 ASS 把它当覆盖标签
    assert r"你好（世界）\Nhello" in text
