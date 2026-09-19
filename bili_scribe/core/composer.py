"""评论组装：把结构化总结压成一条能发出去的评论。

评论区的硬约束是 1000 字符，而且平台对"重复内容"和"疑似推广"有额外风控。
所以这里做三件事：把 Markdown 洗成纯文本、预留出免责声明的空间再裁剪、
以及不在正文里塞链接或话题标签——那些都是典型的垃圾评论特征，会提高被拦的概率。
"""

from __future__ import annotations

import re

from ..config import PublishConfig
from .collector import Bundle
from .resolver import format_duration
from .summarizer import Summary

_MARKDOWN_NOISE = re.compile(r"[*`#>_~]+")
_WHITESPACE = re.compile(r"\s+")
_SENTENCE_ENDS = ("。", "！", "？", "；", "…", ".", "!", "?", ";")


def to_plain_text(text: str) -> str:
    """洗掉 Markdown 标记与换行，得到一段可直接粘贴的纯文本。"""
    cleaned = _MARKDOWN_NOISE.sub("", text or "")
    return _WHITESPACE.sub(" ", cleaned).strip()


def _trim_at_sentence(text: str, limit: int) -> tuple[str, bool]:
    """在句子边界收尾。宁可短一点，也不要拦腰截断一句话。"""
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    best = -1
    for marker in _SENTENCE_ENDS:
        best = max(best, window.rfind(marker))
    if best >= int(limit * 0.6):
        return window[: best + 1], True
    return window, True


def build_comment(summary: Summary, bundle: Bundle, cfg: PublishConfig) -> str:
    body = to_plain_text(summary.comment_short or summary.one_line or summary.summary_md)

    suffix = ""
    if cfg.disclose_ai and cfg.disclosure_text.strip():
        suffix = " " + cfg.disclosure_text.strip()

    # 先给免责声明留位置，否则裁到最后会把它切掉——那样反而更糟
    budget = max(80, cfg.max_comment_chars - len(suffix))
    body, cut = _trim_at_sentence(body, budget)
    if cut:
        body = body.rstrip("，,、 ") + "…"

    return (body + suffix).strip()


def build_preview(bundle: Bundle, summary: Summary, cfg: PublishConfig) -> str:
    """给预览面板用的完整 Markdown。这一段不会进评论区，可以写全。"""
    lines: list[str] = []
    lines.append(f"# {bundle.title or '（无标题）'}")
    lines.append("")
    lines.append(
        f"> {bundle.up_name or '未知 UP 主'} · {format_duration(bundle.duration)} · "
        f"{bundle.tname or '未分区'} · 第 {bundle.page}/{bundle.page_count} P"
    )
    lines.append(
        f"> 素材来源：{bundle.source_label}（可信度：{bundle.source_trust}）—— {bundle.source_detail}"
    )
    lines.append(f"> 原视频：https://www.bilibili.com/video/{bundle.bvid}")
    lines.append("")

    if summary.one_line:
        lines.append(f"**{summary.one_line}**")
        lines.append("")

    if summary.summary_md:
        lines.append(summary.summary_md)
        lines.append("")

    if summary.key_points:
        lines.append("## 要点")
        lines.extend(f"- {point}" for point in summary.key_points)
        lines.append("")

    if summary.timeline:
        lines.append("## 时间轴")
        for item in summary.timeline:
            stamp = item.get("t", "")
            lines.append(f"- `{stamp}` {item.get('text', '')}" if stamp else f"- {item.get('text', '')}")
        lines.append("")

    if summary.caution:
        lines.append("## 素材局限")
        lines.append(summary.caution)
        lines.append("")

    if bundle.warnings:
        lines.append("## 采集提示")
        lines.extend(f"- {warning}" for warning in bundle.warnings)
        lines.append("")

    if bundle.platform_summary:
        lines.append("## 平台侧摘要（仅对照，未参与本次生成）")
        lines.append(bundle.platform_summary)
        lines.append("")

    if summary.chunks > 1:
        lines.append(f"*素材较长，已分 {summary.chunks} 段预读后再归约。*")

    return "\n".join(lines).strip()
