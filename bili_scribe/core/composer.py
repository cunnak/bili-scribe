"""评论组装：把结构化总结压成一条能发出去的评论。

评论区的硬约束是 1000 字符，而且平台对"重复内容"和"疑似推广"有额外风控。
所以这里做三件事：把 Markdown 洗成纯文本、预留出免责声明的空间再裁剪、
以及不在正文里塞链接或话题标签——那些都是典型的垃圾评论特征，会提高被拦的概率。
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------- 发布长文


def build_publish_document(summary: Summary) -> str:
    """发到评论区的**完整长文**（不是概括版）。

    形态由用户明确指定：加粗的一句话开头 + 分节正文 + 要点 + 时间轴。
    与 build_preview 的区别是**刻意不带**元数据头、素材局限、采集提示、
    平台侧摘要——那些是给操作者看的工作信息，发出去只会稀释正文。

    普通评论 1000 字符装不下这种长度，所以它只能走笔记通道（见 publisher）。
    """
    parts: list[str] = []
    if summary.one_line:
        parts.append(f"**{summary.one_line}**")
        parts.append("")
    if summary.summary_md:
        parts.append(summary.summary_md.strip())
        parts.append("")
    if summary.key_points:
        parts.append("## 要点")
        parts.extend(f"- {point}" for point in summary.key_points)
        parts.append("")
    if summary.timeline:
        parts.append("## 时间轴")
        for item in summary.timeline:
            stamp = item.get("t", "")
            text = item.get("text", "")
            parts.append(f"- `{stamp}` {text}" if stamp else f"- {text}")
        parts.append("")
    return "\n".join(parts).strip()


_BOLD_SEGMENT = re.compile(r"\*\*(.+?)\*\*")


def _inline_ops(text: str) -> list[dict[str, object]]:
    """把一行里的 **加粗** 拆成 Delta 片段。其余标记一律按纯文本保留。"""
    ops: list[dict[str, object]] = []
    cursor = 0
    for match in _BOLD_SEGMENT.finditer(text):
        if match.start() > cursor:
            ops.append({"insert": text[cursor : match.start()]})
        ops.append({"insert": match.group(1), "attributes": {"bold": True}})
        cursor = match.end()
    if cursor < len(text):
        ops.append({"insert": text[cursor:]})
    if not ops:
        ops.append({"insert": text})
    return ops


def markdown_to_delta(markdown: str) -> str:
    """把 Markdown 转成 B站 笔记正文需要的 Quill Delta JSON。

    **刻意保守**：只使用文档示例里验证过的属性（bold），标题一律降级成加粗行，
    列表保留字面的 `- ` 前缀。B站 对未知属性的反应未知——多塞一个
    `header` 或 `list` 属性可能直接 -400，而降级成加粗最多只是不好看。
    拿到一次真实写入的响应之后再考虑放开。
    """
    ops: list[dict[str, object]] = []
    for raw_line in (markdown or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if ops:
                ops.append({"insert": "\n"})
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        for op in _inline_ops(stripped):
            ops.append(op)
        ops.append({"insert": "\n"})

    # Quill 约定正文以换行结尾；开头不要留空行
    while ops and ops[0].get("insert") == "\n":
        ops.pop(0)
    if not ops:
        ops.append({"insert": "\n"})
    return json.dumps(ops, ensure_ascii=False)
