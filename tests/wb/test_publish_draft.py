"""发布草稿（P2-09）的核心层测试。

这一层最容易犯的错是「看起来对」：

1. 正文里混进**内部嘱咐**（素材局限、采集提示）——发出去的是给读者看的，不是给我的备忘；
2. 字数算成「含换行/含 Markdown 标记」——用户看到 217 字被拦，而平台其实只算 190；
3. 越界的草稿被**存进库**——存得下就等于能发，用户到平台才被拒。

所以三个断言都盯着"会不会在真实发布时打脸"。
"""

from __future__ import annotations

import json

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import publisher

CONTENT = {
    "one_line": "这期把注意力机制拆成能照做的步骤",
    "summary_md": "## 这期在讲什么\n讲三件事。",
    "key_points": ["先证伪直觉", "把变量拆开", "给出操作顺序"],
    "timeline": [{"t": "00:12", "text": "抛出反例"}, {"t": "01:05", "text": "拆变量"}],
    "caution": "数字与专有名词建议回视频核对",
    "comment_short": "一句话：把注意力拆成能照做的三步。",
    "chunks": 1,
    "model": "deepseek-chat",
    "role": "standard",
}

PROJECT = {"id": "pj_1", "title": "【硬核】三十分钟讲透注意力机制", "bvid": "BV1xx", "aid": 123}


def test_markdown_keeps_body_shape_and_drops_internal_notes():
    body = publisher.build_markdown(CONTENT, disclose=True)
    assert "**这期把注意力机制拆成能照做的步骤**" in body
    assert "## 要点" in body and "- 先证伪直觉" in body
    assert "## 时间轴" in body and "`00:12` 抛出反例" in body
    # 内部嘱咐不进正文
    assert "素材局限" not in body and "采集提示" not in body
    # 注意与 AI 披露要带上（读者需要知道这是机器写的）
    assert "注意：数字与专有名词建议回视频核对" in body
    assert publisher.DISCLOSURE in body


def test_disclose_can_be_turned_off():
    body = publisher.build_markdown(CONTENT, disclose=False)
    assert publisher.DISCLOSURE not in body
    assert "注意：" in body  # 关掉的只是 AI 披露，不是所有提示


def test_delta_roundtrip_loses_markdown_markers():
    delta = publisher.to_delta(publisher.build_markdown(CONTENT))
    ops = json.loads(delta)
    assert isinstance(ops, list) and ops
    # 只有 bold 一个属性——多塞 header/list 可能被平台 -400
    kinds = {key for op in ops for key in (op.get("attributes") or {})}
    assert kinds <= {"bold"}
    text = publisher.plain_text(delta)
    assert "**" not in text and "## " not in text
    assert "这期把注意力机制拆成能照做的步骤" in text


def test_count_chars_ignores_whitespace():
    assert publisher.count_chars("一二三") == 3
    assert publisher.count_chars("一 二\n三") == 3
    assert publisher.count_chars("") == 0


def test_default_summary_prefers_comment_short():
    assert publisher.default_summary(CONTENT) == "一句话：把注意力拆成能照做的三步。"
    # 没有短评就退回一句话
    assert publisher.default_summary({"one_line": "只有一句话"}) == "只有一句话"
    # 都没有就是空串，不编
    assert publisher.default_summary({}) == ""


def test_default_summary_is_not_silently_cut():
    """超长摘要必须原样留着，让 build_draft 报 warning。

    偷偷截到 200 字，用户看到的就是"平台显示的摘要"——但平台根本没收下，
    真正被拒时他手上没有任何线索。
    """
    long = {"comment_short": "长" * 300}
    assert len(publisher.default_summary(long)) == 300
    draft = publisher.build_draft(PROJECT, long, max_summary_chars=200)
    assert draft["summary_chars"] == 300
    assert any("摘要超过 200 字" in w for w in draft["warnings"])


def test_draft_reports_limits_and_chars():
    draft = publisher.build_draft(PROJECT, CONTENT, max_summary_chars=200)
    assert draft["title"] == PROJECT["title"]
    assert draft["limits"] == {"title": 80, "summary": 200}
    assert draft["chars"] == publisher.count_chars(draft["content_text"])
    assert draft["chars"] > 20
    assert draft["warnings"] == []


def test_draft_warns_instead_of_silently_truncating_over_limit():
    draft = publisher.build_draft(
        PROJECT, CONTENT, max_summary_chars=10, title="标题" * 50
    )
    joined = "；".join(draft["warnings"])
    assert "标题超过 80 字" in joined
    assert "摘要超过 10 字" in joined


def test_title_override_survives_regeneration():
    draft = publisher.build_draft(PROJECT, CONTENT, title="我自己想的标题")
    assert draft["title"] == "我自己想的标题"


def test_check_draft_refuses_empty_body_and_over_limit():
    draft = publisher.build_draft(PROJECT, CONTENT)
    with pytest.raises(WBError) as empty:
        publisher.check_draft({**draft, "content_text": "   "})
    assert empty.value.code == "VALIDATION"

    with pytest.raises(WBError) as over:
        publisher.check_draft({**draft, "warnings": ["摘要超过 200 字（现在 260 字）"]})
    assert over.value.code == "VALIDATION"
    assert "摘要超过" in over.value.message


def test_summary_from_content_tolerates_workbench_keys():
    summary = publisher.summary_from_content(
        {**CONTENT, "notes": ["工作台自己的键"], "restored_from": 1}
    )
    assert summary.one_line == CONTENT["one_line"]
    assert summary.timeline[0] == {"t": "00:12", "text": "抛出反例"}


def test_summary_from_content_accepts_pair_timeline():
    summary = publisher.summary_from_content({"timeline": [["00:05", "开场"]]})
    assert summary.timeline == [{"t": "00:05", "text": "开场"}]


def test_row_view_adds_counts():
    view = publisher.row_view({"id": "d1", "title": "标题", "summary": "摘要",
                               "content": publisher.to_delta("第一行\n第二行")})
    assert view["chars"] == 6
    assert view["title_chars"] == 2 and view["summary_chars"] == 2
    assert "第一行" in view["content_text"]
