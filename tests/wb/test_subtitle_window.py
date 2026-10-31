"""`subtitles.window()`：切片烧字幕时把源时间的字幕裁成以 0 起算（P6 真链路实测撞出来的）。

为什么单独测：整批 `shift()` 为了不出负数时间码，会把偏移夹到"最早一条不被推负"，
拿它做切片平移会**静默地平移不足**——字幕全错位却仍然"导出成功"。窗口函数必须
按段各自夹，所以要有自己的用例。
"""

from __future__ import annotations

from video_workbench.core import subtitles


def segs():
    return [
        {"start_ms": 0, "end_ms": 2_000, "text": "开场"},
        {"start_ms": 12_000, "end_ms": 14_000, "text": "跨起点"},
        {"start_ms": 20_000, "end_ms": 22_000, "text": "窗口内"},
        {"start_ms": 40_000, "end_ms": 42_000, "text": "窗口后"},
    ]


def test_window_keeps_only_overlapping_and_shifts_to_zero():
    out = subtitles.window(segs(), 12_400, 34_800)
    assert [s["text"] for s in out] == ["跨起点", "窗口内"]
    assert [s["start_ms"] for s in out] == [0, 7_600]
    assert [s["end_ms"] for s in out] == [1_600, 9_600]


def test_window_ids_are_renumbered_and_sorted():
    out = subtitles.window(segs(), 12_400)
    assert [s["id"] for s in out] == ["s1", "s2", "s3"]   # 无 end 上限：窗口后那条也留
    assert out == sorted(out, key=lambda s: s["start_ms"])


def test_window_zero_start_is_a_copy():
    out = subtitles.window(segs(), 0)
    assert [(s["start_ms"], s["end_ms"]) for s in out] == [
        (0, 2_000), (12_000, 14_000), (20_000, 22_000), (40_000, 42_000)]


def test_window_shifts_word_timestamps_too():
    items = [{"start_ms": 20_000, "end_ms": 22_000, "text": "hai",
              "words": [{"word": "ha", "start_ms": 20_000, "end_ms": 21_000},
                        {"word": "i", "start_ms": 21_000, "end_ms": 22_000}]}]
    out = subtitles.window(items, 20_500, 21_500)
    assert len(out) == 1
    assert (out[0]["start_ms"], out[0]["end_ms"]) == (0, 1_000)
    assert [(w["start_ms"], w["end_ms"]) for w in out[0]["words"]] == [(0, 500), (500, 1_000)]


def test_window_empty_when_nothing_overlaps():
    assert subtitles.window([{"start_ms": 0, "end_ms": 1_000, "text": "x"}], 5_000, 6_000) == []


def test_window_drops_degenerate_tail_clamp():
    """末条只超出 1 毫秒也要留，但结束时间被削到窗口末尾，不能出现 0 长度。"""
    out = subtitles.window([{"start_ms": 4_000, "end_ms": 5_000, "text": "压线"}], 3_000, 4_001)
    assert [(s["start_ms"], s["end_ms"]) for s in out] == [(1_000, 1_001)]
