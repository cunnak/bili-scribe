"""字幕质检规则（任务清单 P2-03 验收：重叠/负时长要能标出来）。

为什么单独一个文件：``qc()`` 是"编辑器唯一的判断依据"——字幕要不要标黄、
状态栏说还有几条问题、P2-04 优化前要挑哪些段重译，全靠它。规则一旦漂了，
界面上看到的问题清单就是假的。

这里只测"能不能定位到具体段"，不测"这条是不是译错了"：后者属于 LLM 的事。
"""

from __future__ import annotations

from video_workbench.core.subtitles import qc


def seg(sid: str, start: int, end: int, text: str = "正常的一句话") -> dict:
    return {"id": sid, "start_ms": start, "end_ms": end, "text": text}


def kinds(items: list[dict]) -> list[str]:
    return [item["kind"] for item in items]


def test_clean_transcript_has_no_issues():
    items = qc([seg("a", 0, 2000), seg("b", 2000, 4500), seg("c", 4600, 7000)])
    assert items == []


def test_empty_text_is_error():
    issues = qc([seg("a", 0, 2000, "   ")])
    assert "empty" in kinds(issues)
    assert issues[0]["level"] == "error"
    assert issues[0]["segment_ids"] == ["a"]


def test_negative_duration_is_error_and_beats_too_short():
    # end <= start 时只报 negative 一条，不要同时刷 too_short——否则一段错
    # 会显示两个红点，用户以为错了两处。
    issues = qc([seg("a", 5000, 4000)])
    assert kinds(issues) == ["negative"]
    assert issues[0]["level"] == "error"


def test_too_short_and_too_long():
    short = qc([seg("a", 0, 200)])
    assert kinds(short) == ["too_short"]
    assert short[0]["level"] == "warn"

    long_one = qc([seg("a", 0, 30_000)])
    assert kinds(long_one) == ["too_long"]


def test_too_many_chars_uses_two_line_budget():
    text = "字" * 45
    issues = qc([seg("a", 0, 3000, text)])
    assert kinds(issues) == ["too_many_chars"]

    # 44 字刚好放得下（22 字 × 2 行），不该报
    assert qc([seg("a", 0, 3000, "字" * 44)]) == []


def test_overlap_reports_both_segments():
    issues = qc([seg("a", 0, 3000), seg("b", 2000, 5000)])
    overlap = [i for i in issues if i["kind"] == "overlap"]
    assert len(overlap) == 1
    assert overlap[0]["level"] == "error"
    # 两条段都要标：只说"后一条错了"，用户会以为是后一条的时间不对
    assert overlap[0]["segment_ids"] == ["a", "b"]


def test_big_gap_is_info_not_error():
    issues = qc([seg("a", 0, 2000), seg("b", 90_000, 92_000)])
    gap = [i for i in issues if i["kind"] == "big_gap"]
    assert len(gap) == 1
    assert gap[0]["level"] == "info"


def test_out_of_order_input_is_normalized_first():
    # 传进来的顺序乱了也要按时间排好再比，否则会报一堆假重叠
    issues = qc([seg("b", 2000, 5000), seg("a", 0, 3000)])
    assert [i["kind"] for i in issues] == ["overlap"]
