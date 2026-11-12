"""发布草稿（P2-09）的核心层测试。

这一层最容易犯的错是「看起来对」：

1. 正文里混进**内部嘱咐**（素材局限、采集提示、素材为机器转录的注意事项）——
   发出去的是给读者看的，不是给我的备忘；
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
    # 保留 standard：直接写库/历史行的字面量，是「退役前数据仍可读」的证据（分类标准见 test_summary.py 文件头）
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
    # AI 披露要带上（读者需要知道这是机器写的）
    assert publisher.DISCLOSURE in body


def test_caution_is_not_published_into_the_body():
    """`caution` 是写给作者的自查清单，不是给读者的正文。

    曾经它是拼进正文的（"注意：素材为机器转录，错字错词极多…"），
    结果是读者打开笔记，第一段先读到一段自我否定，正文从 2000 字涨到 2800 字。
    """
    body = publisher.build_markdown(CONTENT, disclose=True)
    assert CONTENT["caution"] not in body
    assert "注意：" not in body


def test_comment_short_is_not_published_into_the_body():
    """评论区短版是另一个发布位的内容，混进正文等于把同一段话说两遍。"""
    body = publisher.build_markdown(CONTENT, disclose=True)
    assert CONTENT["comment_short"] not in body


def test_disclose_can_be_turned_off():
    body = publisher.build_markdown(CONTENT, disclose=False)
    assert publisher.DISCLOSURE not in body
    # 关掉披露之后正文里不该再有别的尾巴：caution 已经不进正文了
    assert body.rstrip().endswith("拆变量")


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


def test_default_summary_is_retired():
    """摘要字段已下线（2026-11-07）：不管素材里有什么，都返回空串。

    函数留着只为兼容老调用方——它的签名里有 `content`，删掉会 KeyError。
    为什么下线：`comment_short` 是另一段文案，继续拿它当 summary 会在评论区重复；
    现在发布时 summary 从正文开头现算，`auto_comment=1` 负责同步评论卡。
    """
    assert publisher.default_summary(CONTENT) == ""
    assert publisher.default_summary({"one_line": "只有一句话"}) == ""
    assert publisher.default_summary({}) == ""


def test_an_over_long_summary_no_longer_warns():
    """摘要下线之后，超长短评不该再拦发布——它根本不会被发出去。"""
    long = {"comment_short": "长" * 300, **CONTENT}
    draft = publisher.build_draft(PROJECT, long)
    assert draft["summary"] == ""
    assert not any("摘要" in w for w in draft["warnings"])


def test_draft_reports_limits_and_chars():
    draft = publisher.build_draft(PROJECT, CONTENT)
    assert draft["title"] == PROJECT["title"]
    assert draft["limits"] == {"title": 80}
    assert draft["chars"] == publisher.count_chars(draft["content_text"])
    assert draft["chars"] > 20
    assert draft["warnings"] == []


def test_draft_warns_instead_of_silently_truncating_over_limit():
    draft = publisher.build_draft(PROJECT, CONTENT, title="标题" * 50)
    joined = "；".join(draft["warnings"])
    assert "标题超过 80 字" in joined
    # 摘要那一条已经不存在了（字段下线），不该再冒出来
    assert "摘要" not in joined


def test_title_override_survives_regeneration():
    draft = publisher.build_draft(PROJECT, CONTENT, title="我自己想的标题")
    assert draft["title"] == "我自己想的标题"


def test_check_draft_refuses_empty_body_and_over_limit():
    draft = publisher.build_draft(PROJECT, CONTENT)
    with pytest.raises(WBError) as empty:
        publisher.check_draft({**draft, "content_text": "   "})
    assert empty.value.code == "VALIDATION"

    with pytest.raises(WBError) as over:
        publisher.check_draft({**draft, "warnings": ["标题超过 80 字（现在 100 字）"]})
    assert over.value.code == "VALIDATION"
    assert "标题超过" in over.value.message


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
    view = publisher.row_view(
        {
            "id": "d1",
            "title": "标题",
            "summary": "摘要",
            "content": publisher.to_delta("第一行\n第二行"),
        }
    )
    assert view["chars"] == 6
    assert view["title_chars"] == 2 and view["summary_chars"] == 2
    assert "第一行" in view["content_text"]


LEGACY_CAUTION = (
    "注意：素材为机器转录，错字错词极多且密集：地名船名术语均有误（西芒杜为“西芒度”），"
    "引用前务必核对原文与数字。"
)


def _legacy_body(*, caution: str = LEGACY_CAUTION) -> str:
    """老草稿的形状：caution 被拼进正文最后一段，前面是正常内容。"""
    ops = [
        {"insert": "标题一句", "attributes": {"bold": True}},
        {"insert": "\n\n"},
        {"insert": "正文第一段，讲的是运费。"},
        {"insert": "\n\n"},
        {"insert": caution},
        {"insert": "\n"},
    ]
    return json.dumps(ops, ensure_ascii=False)


def test_a_legacy_caution_line_is_stripped_when_the_row_is_read():
    """老草稿（2026-11-07 之前存的）正文里那段「注意：素材为机器转录……」要洗掉。

    实录：draft_8dfe01d36c95 那一段是 742 字，占正文 27%，开头就是这段自我否定；
    用户在界面上早就看不到摘要了，但正文里这段一直在，发出去就是读者第一眼看到的东西。
    库里没有清零路径（老行不会自己变干净），所以在出口这一处洗。
    """
    view = publisher.row_view({"id": "d1", "title": "标题", "content": _legacy_body()})
    assert "机器转录" not in view["content_text"]
    assert "注意：" not in view["content_text"]
    assert "正文第一段" in view["content_text"]
    # 字数要跟着变：界面上的字数是拿洗过的正文算的
    assert view["chars"] == publisher.count_chars(view["content_text"])


def test_the_caution_stripper_keeps_a_sentence_that_merely_says_zhuyi():
    """**不能按「注意」两个字一刀切。**

    另一份草稿（draft_587f000022eb）里有 "- `15:37` 流量注意力vs存量思维" 这种句子，
    那是正文内容本身；按关键字切会把正常内容删掉。所以判据是「以注意：开头」
    且「带转录/素材局限这类标记」，两条同时成立。
    """
    keep = "- `15:37` 流量注意力vs存量思维：200万亿既不进也不出预算"
    view = publisher.row_view({"id": "d1", "title": "标题", "content": _legacy_body(caution=keep)})
    assert "流量注意力" in view["content_text"]


def test_the_caution_stripper_keeps_bold_attributes_on_other_lines():
    """按行切是为了保住属性：非命中行的加粗一个字都不该动。"""
    view = publisher.row_view({"id": "d1", "title": "标题", "content": _legacy_body()})
    ops = json.loads(view["content"])
    first = [o for o in ops if o.get("insert") == "标题一句"]
    assert first and first[0].get("attributes") == {"bold": True}


def test_a_legacy_caution_never_reaches_the_payload():
    """发布走的是库里的行，不能指望界面那边动过手 —— 出口再洗一遍。"""
    payload = publisher.note_payload({"aid": 1, "title": "标题", "content": _legacy_body()})
    text = publisher.plain_text(payload["content"])
    assert "机器转录" not in text
    assert "正文第一段" in text
