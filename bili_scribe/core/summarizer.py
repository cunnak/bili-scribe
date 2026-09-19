"""大模型总结：提示词编排、长素材分块归约、结构化输出解析。

这个模块最重要的设计不是"怎么让总结好看"，而是"怎么防止它编"。素材本身可能
只有弹幕、或者只有标题简介，模型在这种输入下会本能地用先验知识补出通顺但虚假的
内容。所以提示词里下了三条硬约束：只用素材、弹幕必须标明是观众视角、素材不足
时写"素材未覆盖"而不是推测。时间戳也只在素材真实出现过时才允许填写。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..infra.llm import LlmClient
from .collector import Bundle
from .resolver import format_duration

MAX_CHUNKS = 8


class SummarizeError(RuntimeError):
    """模型没有返回可用的结构化结果。"""


SYSTEM_PROMPT = """你是一位严谨的视频内容编辑。用户给你一份从 B站 视频采集来的素材，你要把它整理成可信、克制的总结。

硬性要求：
1. 只使用素材里出现的信息。素材没说的不补充、不推测，不要用常识填空。
2. 素材可能来自弹幕。弹幕是观众反应，不是视频内容。基于弹幕的判断，表述上必须让读者看得出这是观众视角。
3. 素材不足以支撑某个结论时，直接写"素材未覆盖"，不要用合理推测顶上。
4. 不评价 UP 主本人，不站队，不煽动情绪，不使用营销式夸张措辞。
5. 时间戳只能引用素材中真实出现过的。素材不含时间戳时，timeline 一律返回空数组。
6. 只输出合法 JSON，不要用代码块包裹，不要在 JSON 之外写任何解释。"""

USER_TEMPLATE = """视频标题：{title}
UP 主：{up_name}
时长：{duration}
分区：{tname}
标签：{tags}
分P：第 {page} / {page_count} P
链接：{url}

视频简介：
{desc}

—— 素材来源 ——
类型：{source_label}（可信度：{source_trust}）
说明：{source_detail}
素材中是否含时间戳：{has_ts}

—— 素材正文 ——
{body}

请输出 JSON，字段与要求如下：
{{
  "one_line": "一句话说清这个视频在讲什么，40 字以内",
  "summary_md": "300-600 字的 Markdown 总结，用 2-4 个二级标题分段",
  "key_points": ["3-6 条要点，每条 30 字以内"],
  "timeline": [{{"t": "03:12", "text": "该节点讲了什么"}}],
  "caution": "素材的局限与不确定之处；无需说明则填空字符串",
  "comment_short": "给评论区用的短版总结，180-280 字，纯文本，不要 Markdown 标记，不要换行"
}}"""

MAP_SYSTEM = """你在做长素材的分段预读。对给定的一段素材，抽取"这一段讲了什么"的结构化笔记。

硬性要求：
1. 只记录素材里出现的信息，不补充、不推测。
2. 素材中出现的时间戳要原样保留，写在对应内容前面。
3. 笔记要能让人仅凭它还原这段内容。不要压缩成一堆语义模糊的关键词。
4. 直接输出笔记正文，不要前言、不要 JSON、不要总结句。"""


@dataclass
class Summary:
    one_line: str = ""
    summary_md: str = ""
    key_points: list[str] = field(default_factory=list)
    timeline: list[dict[str, str]] = field(default_factory=list)
    caution: str = ""
    comment_short: str = ""
    chunks: int = 1
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "one_line": self.one_line,
            "summary_md": self.summary_md,
            "key_points": self.key_points,
            "timeline": self.timeline,
            "caution": self.caution,
            "comment_short": self.comment_short,
            "chunks": self.chunks,
            "model": self.model,
        }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise SummarizeError("模型没有返回可解析的 JSON")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SummarizeError(f"模型返回的 JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise SummarizeError("模型返回的 JSON 顶层不是对象")
    return payload


def _split(text: str, size: int, overlap: int, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """切分长素材，并保证分块数不超过上限。

    先反推步长下限：K 个窗口要覆盖 N 个字符，步长至少是 (N-1)/(K-1)。
    步长定下来之后，窗口宽度取 `步长 + overlap`，这样既保住了块间重叠，
    又不会因为末尾残段多切出一块——这正是只按 `size - overlap` 推进会踩的坑。
    """
    length = len(text)
    if size <= 0 or length <= size or max_chunks <= 1:
        return [text]

    minimum_step = math.ceil((length - 1) / (max_chunks - 1))
    step = max(1, minimum_step, size - max(0, overlap))
    window = step + max(0, overlap)

    return [text[i : i + window] for i in range(0, length, step)]


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _coerce_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_coerce_text(item) for item in value) if text]


def _coerce_timeline(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"t": "", "text": text})
            continue
        if not isinstance(item, dict):
            continue
        text = _coerce_text(item.get("text") or item.get("content"))
        if text:
            out.append({"t": _coerce_text(item.get("t") or item.get("time")), "text": text})
    return out[:20]


def summarize(
    bundle: Bundle,
    llm: LlmClient,
    cfg: AppConfig,
    progress=None,
) -> Summary:
    notify = progress or (lambda _stage, _message: None)
    llm_cfg = cfg.llm
    body = bundle.body or ""

    has_timestamps = bundle.source_kind in ("subtitle_human", "subtitle_ai", "danmaku")
    chunks = 1

    if len(body) > llm_cfg.max_input_chars:
        pieces = _split(body, llm_cfg.chunk_size, llm_cfg.chunk_overlap)
        chunks = len(pieces)
        notes: list[str] = []
        for index, piece in enumerate(pieces, 1):
            notify("mapping", f"分段预读 {index}/{chunks}")
            note = llm.chat(
                [
                    {"role": "system", "content": MAP_SYSTEM},
                    {
                        "role": "user",
                        "content": f"这是第 {index} 段，共 {chunks} 段。素材类型：{bundle.source_label}\n\n{piece}",
                    },
                ]
            )
            notes.append(note.strip())
        body = "\n\n".join(
            f"【第 {index} 段笔记】\n{note}" for index, note in enumerate(notes, 1)
        )
        budget = llm_cfg.max_input_chars * 2
        if len(body) > budget:
            body = body[:budget] + "\n…（笔记已截断）"

    notify("reducing", "生成结构化总结")
    user_prompt = USER_TEMPLATE.format(
        title=bundle.title or "（无）",
        up_name=bundle.up_name or "（未知）",
        duration=format_duration(bundle.duration),
        tname=bundle.tname or "（未知）",
        tags="、".join(bundle.tags) if bundle.tags else "（无）",
        page=bundle.page,
        page_count=bundle.page_count,
        url=f"https://www.bilibili.com/video/{bundle.bvid}",
        desc=bundle.desc or "（无简介）",
        source_label=bundle.source_label,
        source_trust=bundle.source_trust,
        source_detail=bundle.source_detail,
        has_ts="是" if has_timestamps else "否（不要生成 timeline 内容）",
        body=body,
    )

    raw = llm.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=True,
    )
    payload = _extract_json(raw)

    summary = Summary(
        one_line=_coerce_text(payload.get("one_line")),
        summary_md=_coerce_text(payload.get("summary_md")),
        key_points=_coerce_list(payload.get("key_points")),
        timeline=_coerce_timeline(payload.get("timeline")) if has_timestamps else [],
        caution=_coerce_text(payload.get("caution")),
        comment_short=_coerce_text(payload.get("comment_short")),
        chunks=chunks,
        model=llm.cfg.model,
    )

    if not summary.summary_md and not summary.one_line:
        raise SummarizeError("模型返回的总结为空，请重试或换个模型")
    if not summary.comment_short:
        # 评论字段缺失时退回一句话结论，保证闭环不至于断在这里
        summary.comment_short = summary.one_line or summary.summary_md[:200]
    return summary
