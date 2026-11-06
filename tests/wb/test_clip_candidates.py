"""切片候选粗筛（P3-01）的测试。

这一层要证明四件事：

1. **时间码对得上**：模型给的是毫秒，字幕给的是毫秒，喷出来的时间不能漂；
2. **越界被收拾**：太短补、太长截，且记在 notes 里（不许静默改用户的候选）；
3. **热度是有边界的加成**：最多 +10，且没有时间轴的弹幕一律不算分——
   否则"高频弹幕"会被当成全片最热，那是一份假数据；
4. **模型挂了有兜底**：规则候选能出来，并且明确标注 `source="rule"`。
"""

from __future__ import annotations

import json

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import clipper

SEGMENTS = [
    {"start_ms": 0, "end_ms": 3_000, "text": "今天讲三件事"},
    {"start_ms": 3_000, "end_ms": 8_000, "text": "第一件是把直觉证伪"},
    {"start_ms": 8_000, "end_ms": 15_000, "text": "你看这个地方，所有人都会算错"},
    {"start_ms": 15_000, "end_ms": 22_000, "text": "因为成本根本不是你想的那个成本"},
    {"start_ms": 22_000, "end_ms": 30_000, "text": "第二件是把变量拆开"},
]


def good_reply(**over):
    item = {
        "kind": "highlight",
        "start_hint_ms": 8_000,
        "end_hint_ms": 24_000,
        "title": "成本不是你想的那个",
        "reason": "有反例也有结论",
        "quote": "因为成本根本不是你想的那个成本",
        "score": 82,
        "tags": ["观点"],
    }
    item.update(over)
    return json.dumps({"candidates": [item]}, ensure_ascii=False)


class StubClient:
    def __init__(self, replies, model="stub-clip"):
        self.replies = list(replies)
        self.model = model
        self.calls: list[list] = []
        self.kwargs: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        self.kwargs.append(kwargs)
        if not self.replies:
            raise AssertionError("多要了一次回复")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ------------------------------------------------------------------ 文本与时间码


def test_stamp_is_millisecond_precise():
    assert clipper.stamp(0) == "00:00.000"
    assert clipper.stamp(8_000) == "00:08.000"
    assert clipper.stamp(125_400) == "02:05.400"
    assert clipper.stamp(3_723_000) == "62:03.000"


def test_lines_text_keeps_timecodes():
    body = clipper.lines_text(SEGMENTS)
    assert "[00:08.000-00:15.000] 你看这个地方，所有人都会算错" in body
    assert body.count("\n") == len(SEGMENTS) - 1


def test_lines_text_samples_long_transcripts():
    many = [
        {"start_ms": i * 1_000, "end_ms": i * 1_000 + 900, "text": f"第{i}句"} for i in range(2_000)
    ]
    body = clipper.lines_text(many, limit=100)
    assert body.count("\n") == 99


def test_lines_text_without_subtitles_says_so():
    assert clipper.lines_text([]) == "（没有字幕）"


# ------------------------------------------------------------------ 解析


def test_parse_accepts_bare_array():
    raw = json.dumps([{"kind": "highlight"}], ensure_ascii=False)
    assert clipper.parse_reply(raw) == [{"kind": "highlight"}]


def test_parse_strips_fence_and_chatter():
    raw = "好的，我挑了这些：\n```json\n" + good_reply() + "\n```\n希望有用。"
    rows = clipper.parse_reply(raw)
    assert len(rows) == 1 and rows[0]["kind"] == "highlight"


def test_parse_rejects_prose():
    with pytest.raises(WBError) as err:
        clipper.parse_reply("我觉得这一段挺好的，你看着剪吧")
    assert err.value.code == "LLM_BAD_RESPONSE"


def test_parse_rejects_missing_candidates_array():
    with pytest.raises(WBError):
        clipper.parse_reply('{"items": []}')


# ------------------------------------------------------------------ 校验与评分


def test_normalize_clamps_to_duration_and_range():
    packed = clipper.normalize(
        [
            {"kind": "highlight", "start_hint_ms": 30_000, "end_hint_ms": 40_000, "score": 90},
            {"kind": "segment", "start_hint_ms": 30_000, "end_hint_ms": 600_000, "score": 90},
        ],
        duration_ms=600_000,
    )
    first = packed["candidates"][0]
    assert first["start_ms"] == 30_000 and first["end_ms"] == 45_000, "太短应补到 15 秒"
    assert any("太短" in note for note in packed["notes"])
    second = [c for c in packed["candidates"] if c["kind"] == "segment"][0]
    assert second["end_ms"] == 30_000 + 480_000
    assert any("太长" in note for note in packed["notes"])


def test_normalize_drops_broken_candidates():
    packed = clipper.normalize(
        [
            {"kind": "gif", "start_hint_ms": 0, "end_hint_ms": 20_000},
            {"kind": "highlight", "start_hint_ms": 20_000},
            {"kind": "highlight", "start_hint_ms": 20_000, "end_hint_ms": 10_000},
        ],
        duration_ms=60_000,
    )
    assert packed["candidates"] == []
    reasons = "；".join(item["reason"] for item in packed["dropped"])
    assert "kind" in reasons and "缺起止" in reasons and "结束不晚于开始" in reasons


def test_normalize_filters_kind_but_keeps_type_cap():
    rows = [
        {
            "kind": "highlight",
            "start_hint_ms": i * 20_000,
            "end_hint_ms": i * 20_000 + 20_000,
            "score": 80,
            "title": f"第{i}个",
            "reason": "有结论",
        }
        for i in range(12)
    ]
    packed = clipper.normalize(rows, duration_ms=600_000, kinds=("highlight",), count=12)
    assert len(packed["candidates"]) == clipper.PER_KIND_MAX, "高光最多 8 个"
    assert all(row["kind"] == "highlight" for row in packed["candidates"])


def test_normalize_sorts_by_score_after_heat():
    heat = {i * 5_000: 1 for i in range(200)}
    heat[10_000] = 40  # 10–15s 最热
    rows = [
        {
            "kind": "highlight",
            "start_hint_ms": 100_000,
            "end_hint_ms": 120_000,
            "score": 58,
            "title": "冷区",
            "reason": "有结论",
        },
        {
            "kind": "highlight",
            "start_hint_ms": 10_000,
            "end_hint_ms": 30_000,
            "score": 55,
            "title": "热区",
            "reason": "有结论",
        },
    ]
    packed = clipper.normalize(rows, duration_ms=200_000, heat=heat)
    top = packed["candidates"][0]
    assert top["title"] == "热区", "热度加成没有把热区顶上来"
    assert top["heat_bonus"] > 0 and top["score"] <= 100


def test_heat_bonus_is_capped_and_penalised():
    heat = {i * 5_000: 100 for i in range(6)}
    packed = clipper.normalize(
        [
            {
                "kind": "highlight",
                "start_hint_ms": 0,
                "end_hint_ms": 30_000,
                "score": 95,
                "title": "满热度",
                "reason": "有理由",
            }
        ],
        duration_ms=60_000,
        heat=heat,
    )
    row = packed["candidates"][0]
    assert row["heat_bonus"] == clipper.HEAT_BONUS_MAX, "加成上限就是 10"
    assert row["score"] == 100, "分数上限 100"

    # 同样一段但没有标题和理由：占位标题要有，理由留空，分数被扣两次
    bare = clipper.normalize(
        [{"kind": "highlight", "start_hint_ms": 0, "end_hint_ms": 30_000, "score": 95}],
        duration_ms=60_000,
        heat=heat,
    )["candidates"][0]
    assert bare["title"], "缺标题时应补一个占位标题"
    assert bare["reason"] == ""
    assert bare["score"] == 95 + clipper.HEAT_BONUS_MAX - 2 * clipper.MISSING_TEXT_PENALTY


def test_missing_text_is_penalised():
    plain = clipper.normalize(
        [
            {
                "kind": "highlight",
                "start_hint_ms": 0,
                "end_hint_ms": 30_000,
                "score": 80,
                "title": "有标题",
                "reason": "有理由",
            }
        ],
        duration_ms=60_000,
    )["candidates"][0]
    empty = clipper.normalize(
        [{"kind": "highlight", "start_hint_ms": 0, "end_hint_ms": 30_000, "score": 80}],
        duration_ms=60_000,
    )["candidates"][0]
    assert empty["score"] == plain["score"] - 2 * clipper.MISSING_TEXT_PENALTY


# ------------------------------------------------------------------ 热度


def test_heat_buckets_need_timestamps():
    buckets = clipper.heat_buckets([{"time_ms": 12_400}, {"progress_ms": 13_000}, "纯文本弹幕"])
    assert buckets == {10_000: 2}, buckets


def test_heat_ratio_is_density_not_raw_count():
    """一段十分钟里散着 200 条，不该比 5 秒里 20 条更热。"""
    buckets = {0: 4, 5_000: 20}
    # 0–5s：4 条/5 秒 = 0.8 条/秒；峰值 20 条/5 秒 = 4 条/秒 → 0.2
    assert clipper.heat_ratio(buckets, 0, 5_000) == pytest.approx(0.2)
    # 桶左闭右开：5_000 那个桶不算进 [0, 5_000)，但算进 [0, 10_000)
    assert clipper.heat_ratio(buckets, 5_000, 10_000) == pytest.approx(1.0)
    # 摊平到 10 秒，密度降到 2.4 条/秒 → 0.6
    assert clipper.heat_ratio(buckets, 0, 10_000) == pytest.approx(0.6)
    assert clipper.heat_ratio({}, 0, 10_000) == 0.0


def test_danmaku_note_says_when_timeline_missing():
    note = clipper.danmaku_note(["好听", "来了"], {})
    assert "没有时间轴" in note
    good = clipper.danmaku_note([{"time_ms": 1_000}], {0: 3})
    assert "最集中的位置" in good


# ------------------------------------------------------------------ 入口


def test_generate_returns_llm_candidates_with_heat():
    client = StubClient([good_reply()])
    out = clipper.generate(
        SEGMENTS,
        client=client,
        duration_ms=30_000,
        danmaku=[{"time_ms": 9_000}, {"time_ms": 10_000}],
        on_progress=lambda msg: None,
    )
    assert out["source"] == "llm" and out["attempts"] == 1
    row = out["candidates"][0]
    # 8_000 正好是句首，吸附不动；24_000 离句尾 22_000 更近（2 秒，在容差内），
    # 吸过去只剩 14 秒，低于高光下限 15 秒，于是再补 1 秒 → 23_000。
    # 原值 24_000 留在 raw_end_ms 里，用户想回退有据可查。
    assert row["start_ms"] == 8_000
    assert row["boundary"] == "sentence"
    assert row["raw_end_ms"] == 24_000
    assert row["end_ms"] == 23_000
    assert row["silence_trimmed_ms"] == 16_000 - 15_000  # 原长 − 最终长，正数＝剪短
    assert row["heat_bonus"] > 0
    assert out["heat_buckets"] == 2, "9s 与 10s 落在相邻两个 5 秒桶里"
    # prompt 里该有的东西都在
    prompt = client.calls[0][1]["content"]
    assert "$lines" not in prompt and "00:08.000" in prompt
    assert "最集中的位置" in prompt


def test_generate_retries_then_falls_back_to_rules():
    client = StubClient(["我不想按格式回话", "还是不想"])
    out = clipper.generate(
        SEGMENTS,
        client=client,
        duration_ms=30_000,
        kinds=("highlight",),
        count=2,
        on_progress=lambda msg: None,
    )
    assert out["source"] == "rule"
    assert out["attempts"] == 2, "失败要重试一次"
    assert out["candidates"], "兜底也得给候选"
    assert "规则候选" in out["candidates"][0]["title"]
    assert any("规则候选" in note for note in out["notes"])


def test_generate_falls_back_when_client_raises():
    class Boom:
        model = "boom"

        def chat(self, messages, **kwargs):
            raise RuntimeError("network down")

    out = clipper.generate(
        SEGMENTS,
        client=Boom(),
        duration_ms=30_000,
        kinds=("highlight",),
        on_progress=lambda msg: None,
    )
    assert out["source"] == "rule"
    assert any("network down" in note or "模型请求失败" in note for note in out["notes"])


def test_generate_without_subtitles_is_conflict():
    with pytest.raises(WBError) as err:
        clipper.generate([], client=StubClient([]), duration_ms=0)
    assert err.value.code == "CONFLICT"


def test_rule_candidates_respect_windows_and_chapters():
    rows = clipper.rule_candidates(
        SEGMENTS, duration_ms=600_000, kinds=("highlight", "segment"), heat={120_000: 9}
    )
    highlights = [r for r in rows if r["kind"] == "highlight"]
    segments = [r for r in rows if r["kind"] == "segment"]
    assert highlights[0]["start_ms"] == 120_000, "有弹幕峰值就该切在峰值上"
    assert all(r["end_ms"] - r["start_ms"] == clipper.RULE_WINDOW_MS for r in highlights)
    assert len(segments) == 3, "10 分钟按 4 分钟一章应切 3 段"
    assert segments[-1]["end_ms"] == 600_000, "最后一章要收在片尾"
