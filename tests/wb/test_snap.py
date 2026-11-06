"""切点吸附与去重（P3-02）的测试。

这一层要证明四件事：

1. **不拿静音当开头**：起止点落在两句话之间的空隙里，要往里收，不是往外放；
2. **吸附有边界也有记录**：吸上了标 `sentence`/`word`，吸不上就留空并写清原因——
   不许偷偷挪到一个看起来整洁的位置；
3. **重叠只在一类里算**：同一类的重叠丢低分，跨类（长段里套着高光）都要留；
4. **原值不丢**：`raw_start_ms`/`raw_end_ms` 是模型给的原话，吸附后的值另存，
   用户在界面上改坏了还能退回去。
"""

from __future__ import annotations

import json

from video_workbench.core import clipper

SEGMENTS = [
    {"start_ms": 0, "end_ms": 3_000, "text": "今天讲三件事"},
    {"start_ms": 3_000, "end_ms": 8_000, "text": "第一件是把直觉证伪"},
    # 8 秒到 12 秒是静音（换气 + 停了一下）
    {"start_ms": 12_000, "end_ms": 18_000, "text": "你看这个地方所有人都会算错"},
    {"start_ms": 18_000, "end_ms": 24_000, "text": "因为成本根本不是你想的那个成本"},
    {"start_ms": 30_000, "end_ms": 36_000, "text": "第二件是把变量拆开逐个对比"},
]

WORDS = [
    {
        "start_ms": 12_000,
        "end_ms": 18_000,
        "text": "你看这个地方所有人都会算错",
        "words": [
            {"start_ms": 12_000, "end_ms": 13_200, "text": "你看"},
            {"start_ms": 13_200, "end_ms": 14_600, "text": "这个地方"},
            {"start_ms": 14_600, "end_ms": 16_400, "text": "所有人"},
            {"start_ms": 16_400, "end_ms": 18_000, "text": "都会算错"},
        ],
    }
]


# ---------------------------------------------------------------- 静音裁剪


def test_trim_silence_moves_start_past_gap():
    """起点落在 8–12 秒的静音里 → 挪到下一句开头。"""
    out = clipper.trim_silence(SEGMENTS, 9_500, 20_000)
    assert out["start_ms"] == 12_000
    assert any("静音" in note for note in out["notes"])


def test_trim_silence_moves_end_back_before_gap():
    """终点落在 24–30 秒的静音里 → 收回到上一句结尾。"""
    out = clipper.trim_silence(SEGMENTS, 6_000, 27_000)
    assert out["end_ms"] == 24_000
    assert any("静音" in note for note in out["notes"])


def test_trim_silence_keeps_points_inside_speech():
    out = clipper.trim_silence(SEGMENTS, 13_000, 23_000)
    assert (out["start_ms"], out["end_ms"]) == (13_000, 23_000)
    assert out["notes"] == []


def test_trim_silence_reports_window_without_speech():
    out = clipper.trim_silence(SEGMENTS, 25_000, 29_000)
    assert out["notes"] and "没有语音" in out["notes"][0]
    assert (out["start_ms"], out["end_ms"]) == (25_000, 29_000), "拿不到证据就别乱挪"


# ---------------------------------------------------------------- 边界吸附


def test_snap_window_hits_sentence_boundaries():
    out = clipper.snap_window(SEGMENTS, 12_400, 23_600)
    assert (out["start_ms"], out["end_ms"]) == (12_000, 24_000)
    assert out["boundary"] == "sentence"
    assert out["notes"] == []


def test_snap_window_keeps_value_and_marks_when_out_of_tolerance():
    """离最近的句边界 3 秒（容差 2 秒）→ 不动，并说明原因。"""
    out = clipper.snap_window(SEGMENTS, 6_000, 21_000, tolerance_ms=2_000)
    assert out["start_ms"] == 6_000, "超容差不许挪"
    assert out["boundary"] == ""
    assert any("起点不在句边界" in note for note in out["notes"])


def test_snap_window_uses_word_boundaries_when_words_exist():
    out = clipper.snap_window(WORDS, 13_400, 18_000, mode="word", tolerance_ms=1_000)
    assert out["start_ms"] == 13_200
    assert out["boundary"] == "word"


def test_snap_window_labels_sentence_when_words_missing():
    """要词边界但没有词级时间戳，实际吸的是句边界——标签必须照实写。"""
    out = clipper.snap_window(SEGMENTS, 12_400, 23_600, mode="word")
    assert out["boundary"] == "sentence"


def test_snap_window_refuses_zero_length_result():
    out = clipper.snap_window(SEGMENTS, 12_100, 12_200, tolerance_ms=0)
    assert out["boundary"] == ""
    assert (out["start_ms"], out["end_ms"]) == (12_100, 12_200)


# ---------------------------------------------------------------- 去重


def _cand(kind, start, end, score, title):
    return {"kind": kind, "start_ms": start, "end_ms": end, "score": score, "title": title}


def test_dedupe_drops_lower_score_on_overlap():
    out = clipper.dedupe(
        [
            _cand("highlight", 0, 20_000, 70, "甲"),
            _cand("highlight", 8_000, 28_000, 85, "乙"),
        ]
    )
    assert [row["title"] for row in out["candidates"]] == ["乙"]
    assert out["dropped"] and "重叠" in out["dropped"][0]["reason"]


def test_dedupe_keeps_disjoint_windows():
    rows = [_cand("highlight", 0, 20_000, 70, "甲"), _cand("highlight", 40_000, 60_000, 70, "乙")]
    out = clipper.dedupe(rows)
    assert len(out["candidates"]) == 2 and out["dropped"] == []


def test_dedupe_treats_containment_as_overlap():
    out = clipper.dedupe(
        [
            _cand("segment", 0, 200_000, 80, "整章"),
            _cand("segment", 20_000, 60_000, 65, "一小截"),
        ]
    )
    assert [row["title"] for row in out["candidates"]] == ["整章"]


def test_dedupe_does_not_cross_kinds():
    """3 分钟的长段里套着 20 秒高光，两条都要留：它们导出的东西不一样。"""
    out = clipper.dedupe(
        [
            _cand("segment", 0, 180_000, 70, "整章"),
            _cand("highlight", 20_000, 40_000, 88, "高光"),
        ]
    )
    assert {row["kind"] for row in out["candidates"]} == {"segment", "highlight"}


def test_dedupe_keeps_slight_overlap():
    """重叠只有 2 秒 / 18 秒 ≈ 11%，不算重复。"""
    out = clipper.dedupe(
        [
            _cand("highlight", 0, 18_000, 70, "甲"),
            _cand("highlight", 16_000, 34_000, 72, "乙"),
        ]
    )
    assert len(out["candidates"]) == 2


# ---------------------------------------------------------------- 整条流水线


def test_refine_preserves_raw_values():
    out = clipper.refine(
        [_cand("highlight", 9_500, 27_000, 70, "甲")],
        SEGMENTS,
        duration_ms=36_000,
    )
    row = out["candidates"][0]
    assert (row["raw_start_ms"], row["raw_end_ms"]) == (9_500, 27_000)
    assert row["start_ms"] == 12_000, "先剪静音：8–12 秒的空隙要跳过"
    # 吸到 24_000 的句尾只剩 12 秒，低于高光下限，于是补到 15 秒 → 27_000
    assert row["end_ms"] == 27_000
    assert row["end_ms"] - row["start_ms"] == clipper.RANGES["highlight"][0]


def test_refine_trims_then_snaps_and_keeps_enough_length():
    """先剪静音再吸附：如果顺序倒了，起点会被吸到 8 秒那句的句尾，开头就是静音。"""
    out = clipper.refine(
        [_cand("highlight", 9_500, 20_000, 70, "甲")], SEGMENTS, duration_ms=36_000
    )
    row = out["candidates"][0]
    assert row["start_ms"] >= 12_000
    assert row["end_ms"] - row["start_ms"] >= 15_000, "短于下限要补齐"


def test_refine_truncates_overlong_segment():
    rows = [{"start_ms": 0, "end_ms": 700_000, "text": "一整个小时"}]
    out = clipper.refine([_cand("segment", 0, 700_000, 70, "整章")], rows, duration_ms=700_000)
    row = out["candidates"][0]
    assert row["end_ms"] - row["start_ms"] == clipper.RANGES["segment"][1]


def test_refine_clamps_out_of_range_candidate():
    """超界的候选先被夹回片长，仍在 0 之后。"""
    out = clipper.refine(
        [_cand("highlight", -5_000, 900_000, 70, "越界")], SEGMENTS, duration_ms=36_000
    )
    row = out["candidates"][0]
    assert row["start_ms"] >= 0
    assert row["end_ms"] <= 36_000
    assert 15_000 <= row["end_ms"] - row["start_ms"] <= 90_000


def test_refine_marks_candidates_without_boundary():
    """句边界都在容差外：boundary 留空、原因写进 snap_notes，前端据此标提示。"""
    rows = [
        {"start_ms": 0, "end_ms": 1_000, "text": "嗯"},
        {"start_ms": 40_000, "end_ms": 41_000, "text": "好"},
    ]
    out = clipper.refine(
        [_cand("highlight", 20_000, 35_000, 70, "悬空")],
        rows,
        duration_ms=45_000,
        tolerance_ms=1_000,
    )
    row = out["candidates"][0]
    assert row["boundary"] == ""
    assert any("不在句边界" in note for note in row["snap_notes"])


def test_refine_dedupes_after_snapping():
    """两条原始提示只差一点点、吸附后几乎重合——去重要在吸附之后跑。"""
    rows = [
        {"start_ms": index * 10_000, "end_ms": index * 10_000 + 9_000, "text": f"第 {index} 句"}
        for index in range(60)
    ]
    out = clipper.refine(
        [
            _cand("segment", 120_400, 300_000, 70, "甲"),
            _cand("segment", 120_000, 290_000, 90, "乙"),
        ],
        rows,
        duration_ms=600_000,
    )
    assert len(out["candidates"]) == 1, "吸附到同一批句边界后重叠 100%"


# ---------------------------------------------------------------- generate 接线


class StubClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.model = "stub"
        self.calls = 0

    def chat(self, _messages, **_kwargs):
        self.calls += 1
        if isinstance(self.replies[0], Exception):
            raise self.replies.pop(0)
        return self.replies.pop(0)


def _reply(rows):
    return json.dumps({"candidates": rows}, ensure_ascii=False)


def test_generate_snaps_by_default():
    client = StubClient(
        [
            _reply(
                [
                    {
                        "kind": "highlight",
                        "start_hint_ms": 12_400,
                        "end_hint_ms": 23_600,
                        "title": "甲",
                        "reason": "有结论",
                        "score": 70,
                    }
                ]
            )
        ]
    )
    out = clipper.generate(SEGMENTS, client=client, duration_ms=36_000, kinds=("highlight",))
    row = out["candidates"][0]
    # 12_400 → 12_000（句首）；23_600 → 24_000（句尾）只剩 12 秒，补到 15 秒
    assert (row["start_ms"], row["end_ms"]) == (12_000, 27_000)
    assert row["boundary"] == "sentence"
    assert out["source"] == "llm"


def test_generate_can_skip_snapping():
    client = StubClient(
        [
            _reply(
                [
                    {
                        "kind": "highlight",
                        "start_hint_ms": 12_400,
                        "end_hint_ms": 23_600,
                        "title": "甲",
                        "reason": "有结论",
                        "score": 70,
                    }
                ]
            )
        ]
    )
    out = clipper.generate(
        SEGMENTS, client=client, duration_ms=36_000, kinds=("highlight",), snap=False
    )
    row = out["candidates"][0]
    # 关掉吸附就只剩 normalize 的区间夹取：起点原样，终点补到 15 秒下限
    assert row["start_ms"] == 12_400
    assert row["end_ms"] == 27_400
    assert "boundary" not in row, "没走吸附就不该有边界标记"


def test_generate_dedupes_overlapping_candidates():
    client = StubClient(
        [
            _reply(
                [
                    {
                        "kind": "highlight",
                        "start_hint_ms": 0,
                        "end_hint_ms": 20_000,
                        "title": "低分",
                        "reason": "有结论",
                        "score": 60,
                    },
                    {
                        "kind": "highlight",
                        "start_hint_ms": 3_000,
                        "end_hint_ms": 23_000,
                        "title": "高分",
                        "reason": "有结论",
                        "score": 90,
                    },
                ]
            )
        ]
    )
    out = clipper.generate(SEGMENTS, client=client, duration_ms=36_000, kinds=("highlight",))
    titles = [row["title"] for row in out["candidates"]]
    assert titles == ["高分"], titles
    assert any("重叠" in str(item.get("reason")) for item in out["dropped"])


def test_generate_rule_fallback_is_also_snapped():
    """兜底候选也要吸附：规则切出来的 30 秒窗口不一定落在句边界上。"""
    client = StubClient([RuntimeError("网络断了"), RuntimeError("又断了")])
    out = clipper.generate(
        SEGMENTS, client=client, duration_ms=36_000, kinds=("highlight",), retries=1
    )
    assert out["source"] == "rule"
    assert all(row["boundary"] in ("sentence", "word", "") for row in out["candidates"])
    assert all("raw_start_ms" in row for row in out["candidates"])
