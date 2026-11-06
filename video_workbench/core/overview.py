"""合集总览（P2-08）：把多集的单集总结合成一份"从哪一集开始看"的编辑判断。

**为什么单独一个模块，而不是塞进 ``summarizer.py``。** ``summarizer.py`` 的职责是
"适配 bili-scribe"——把我们的数据喂成它认识的形状、把它的进度翻成人话。合集总览
反过来：bili-scribe 没有这个能力（它的 summarizer 只吃一条视频的素材），提示词是
我们自己写的，也没有三轮回。两者只共用 ``SummaryLLM``（JSON 降级）和角色清单。

与单集总结的两个关键差别：

1. **输入是子项的总结，不重读原视频**（方案书 §5.6.2）。20 集就重跑 20 次 ASR 是不可
   接受的；子项总结已经把要点提炼过了。
2. **不套角色三轮回**。总览是编辑视角的编排，套学术方法论只会写出一堆不像总览的东西。

模型会编。所以 ``reconcile_collection`` 会把对不上任何子项的条目直接丢掉——界面里
出现一个不存在的链接，比少一条时间线难解释得多。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from video_workbench.app.errors import WBError
from video_workbench.core.summarizer import DEFAULT_ROLE, SummaryLLM
from video_workbench.infra.llm import ChatClient

#: 提示词文件。缺文件是打包错误，要立刻炸而不是悄悄少一段规矩。
PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "resources" / "prompts" / "collection_overview.md"
)

#: 少于两集就不叫合集。服务层也拦一次，这里再拦一次是因为这层可能被脚本直接调。
MIN_ITEMS = 2

#: 送进 prompt 的素材上限（字符）。子项一多，prompt 会爆；超过就按"每项留一句话 +
#: 两条要点"压缩，宁可少给细节，也不能让请求被拒或让费用失控。
MAX_CHARS = 16000

#: 压缩模式下每条子项保留的要点条数。
TIGHT_POINTS = 2

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _clip(text: Any, limit: int) -> str:
    body = " ".join(str(text or "").split())
    if limit > 0 and len(body) > limit:
        return body[:limit].rstrip() + "…"
    return body


def collection_items(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把「项目 + 它的最新一版总结」整理成 prompt 里的子项。

    ``children`` 每项形如 ``{project: {...}, summary: {...}}``（``summary`` 是
    ``repo.summary_*`` 的行，带解好的 ``content``）。顺序沿用传入顺序——服务层按
    ``created_at`` 排过，用户看到的"第几集"就是这个顺序。
    """
    items: list[dict[str, Any]] = []
    for entry in children or []:
        project = dict(entry.get("project") or {})
        row = dict(entry.get("summary") or {})
        content = dict(row.get("content") or {})
        points = [str(item) for item in (content.get("key_points") or []) if str(item).strip()]
        items.append(
            {
                "title": _clip(project.get("title"), 120),
                "one_line": _clip(content.get("one_line") or row.get("one_line"), 120),
                "points": points[:4],
                "url": str(project.get("source_url") or ""),
                "bvid": str(project.get("bvid") or ""),
                "minutes": max(0, int(project.get("duration_sec") or 0)) // 60,
                "version": int(row.get("version") or 0),
            }
        )
    return items


def format_items(items: list[dict[str, Any]], *, tight: bool = False) -> str:
    """子项 → prompt 里的列表文本。标题单独一行，方便模型照抄。"""
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        head = f"{index}. {item.get('title') or '（无标题）'}"
        if item.get("minutes"):
            head += f"（约 {item['minutes']} 分钟）"
        lines.append(head)
        if item.get("one_line"):
            lines.append(f"   一句话：{item['one_line']}")
        for point in item.get("points") or []:
            lines.append(f"   - {_clip(point, 120 if tight else 200)}")
        if item.get("url"):
            lines.append(f"   链接：{item['url']}")
    return "\n".join(lines)


def build_prompt(
    title: str, items: list[dict[str, Any]], *, template: str = "", extra: str = ""
) -> str:
    """填占位符。素材超长时自动退到"压缩模式"再填一次。"""
    body = template or load_prompt()
    rendered = format_items(items)
    if len(rendered) > MAX_CHARS:
        rendered = format_items(
            [dict(item, points=(item.get("points") or [])[:TIGHT_POINTS]) for item in items],
            tight=True,
        )
    text = (
        body.replace("$collection_title", title or "（未命名合集）")
        .replace("$count", str(len(items)))
        .replace("$items", rendered)
    )
    if extra:
        text += "\n\n## 补充要求\n\n" + extra
    return text


def parse_collection(raw: str) -> dict[str, Any]:
    """把模型回复解析成总览结构。容忍围栏与前后废话，但不猜内容。"""
    text = str(raw or "").strip()
    if not text:
        raise WBError("LLM_BAD_RESPONSE", "模型返回了空内容")
    text = _FENCE_RE.sub("", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise WBError("LLM_BAD_RESPONSE", "模型没有按 JSON 格式回答")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise WBError("LLM_BAD_RESPONSE", "模型返回的 JSON 解析不了", str(exc)) from exc
    if not isinstance(data, dict):
        raise WBError("LLM_BAD_RESPONSE", "模型返回的不是一个 JSON 对象")

    theme = _clip(data.get("theme"), 120)
    if not theme:
        raise WBError("LLM_BAD_RESPONSE", "没有给出合集主题")

    def _strings(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("text") or item.get("title") or ""
            body = _clip(item, 300)
            if body:
                out.append(body)
        return out

    conclusions = _strings(data.get("conclusions"))
    if not conclusions:
        raise WBError("LLM_BAD_RESPONSE", "没有给出核心结论")

    def _rows(value: Any, *keys: str) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        out: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            row = {key: _clip(item.get(key), 300) for key in keys}
            if any(row.values()):
                out.append(row)
        return out

    return {
        "theme": theme,
        "conclusions": conclusions[:7],
        "timeline": _rows(data.get("timeline"), "title", "one_line", "url"),
        "overlap": _rows(data.get("overlap"), "kind", "text"),
        "watch_order": _rows(data.get("watch_order"), "title", "reason"),
    }


def _title_key(text: Any) -> str:
    """标题归一：去掉空白与标点后比较，避免「第 3 集：X」与「第3集:X」对不上。"""
    return re.sub(r"[\s\-_—·:：,，.。、\"'“”‘’()（）\[\]【】]+", "", str(text or "")).lower()


def reconcile_collection(
    content: dict[str, Any], items: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """把模型写的条目对齐回真实子项。

    只做两件事，都不改模型的话：

    - 标题对不上任何子项的条目**丢掉**（模型常把"第二集"缩写成自己编的名字，
      留着会让界面出现不存在的链接）；
    - 对上的补真实链接、真实集号，并把标题换成项目里的原文（模型改标点、
      加书名号很常见，界面应当显示原文）。

    返回 ``(整理后的 content, 说明列表)``。
    """
    notes: list[str] = []
    lookup: dict[str, dict[str, Any]] = {}
    for item in items:
        key = _title_key(item.get("title"))
        if key:
            lookup[key] = item

    def _match(title: str) -> dict[str, Any] | None:
        key = _title_key(title)
        if not key:
            return None
        if key in lookup:
            return lookup[key]
        for candidate, item in lookup.items():
            if key in candidate or candidate in key:
                return item
        return None

    def _fix(rows: list[dict[str, str]], label: str) -> list[dict[str, str]]:
        kept: list[dict[str, str]] = []
        dropped = 0
        for row in rows or []:
            item = _match(row.get("title", ""))
            if not item:
                dropped += 1
                continue
            out = dict(row)
            out["title"] = item.get("title") or row.get("title", "")
            out["url"] = item.get("url") or row.get("url", "")
            out["bvid"] = item.get("bvid") or ""
            kept.append(out)
        if dropped:
            notes.append(f"模型给的 {dropped} 条{label}对不上任何子项，已丢弃")
        return kept

    fixed = dict(content)
    fixed["timeline"] = _fix(content.get("timeline") or [], "时间线")
    fixed["watch_order"] = _fix(content.get("watch_order") or [], "观看顺序")
    fixed["items"] = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "bvid": item.get("bvid") or "",
            "one_line": item.get("one_line") or "",
        }
        for item in items
    ]
    covered = {_title_key(row.get("title")) for row in fixed["watch_order"]}
    if len(covered) < len(items):
        notes.append("观看顺序没覆盖全部子项")
    return fixed, notes


def run(
    title: str,
    children: list[dict[str, Any]],
    *,
    client: ChatClient,
    ctx: Any,
    role: str = "",
    model: str = "",
    prompt_template: str = "",
    extra: str = "",
    cfg: Any = None,
) -> dict[str, Any]:
    """跑一次合集总览。返回 ``{content, usage, model, role, calls, notes, items}``。

    ``cfg`` 是给 ``SummaryLLM`` 的那份配置：坏 JSON 的补救预算要按 ``llm.max_tokens``
    算（见 ``summarizer._request_max_tokens``），不传就走老行为——预算与配置无关。
    默认 ``None`` 是刻意的向后兼容，**产品内的调用方必须传**（task-59；
    ``tests/wb/test_summary_llm_cfg_wiring.py`` 锁的就是这一条）。
    """
    items = collection_items(children)
    if len(items) < MIN_ITEMS:
        raise WBError(
            "CONFLICT",
            f"合集至少要有 {MIN_ITEMS} 集才能出总览",
            f"当前只有 {len(items)} 集",
        )
    missing = [item.get("title") or "" for item in items if not item.get("one_line")]
    if missing:
        raise WBError(
            "CONFLICT",
            f"有 {len(missing)} 集还没有总结，先给它们各出一版",
            "缺总结的：" + "、".join(missing[:5]),
        )

    # 不传 cfg 时，这条路的补救预算恒等于那个写死的下限、与用户配置无关（task-57 的
    # helper 只在 cfg 在手时才生效）。产品码里不许再出现不传 cfg 的构造点——
    # 常驻锁见 tests/wb/test_summary_llm_cfg_wiring.py。
    llm = SummaryLLM(client, model=model, cfg=cfg)
    prompt = build_prompt(title, items, template=prompt_template, extra=extra)
    ctx.progress(0.2, f"读 {len(items)} 集总结")
    ctx.log(f"合并 {len(items)} 集，素材约 {len(prompt)} 字")
    reply = llm.chat(
        [
            {"role": "system", "content": "你是视频合集的编辑，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
        note=ctx.log,
        temperature=0.3,
    )
    ctx.progress(0.7, "整理总览")
    content = parse_collection(reply)
    content, notes = reconcile_collection(content, items)
    content["collection_title"] = title
    content["item_count"] = len(items)
    # notes 必须落进 content：落库的是 content，界面读的也是 content.notes。
    # 只放在返回值里，用户就只会看到"timeline 少了一条"而没有任何解释。
    content["notes"] = notes + list(llm.notes)
    content["model"] = llm.model
    content["role"] = role or DEFAULT_ROLE
    return {
        "content": content,
        "usage": {"calls": llm.calls},
        "model": llm.model,
        "role": role or DEFAULT_ROLE,
        "calls": llm.calls,
        "notes": notes + list(llm.notes),
        "items": items,
    }


def to_markdown(content: dict[str, Any]) -> str:
    """总览 → 人读的 Markdown（与单集 ``summary.md`` 同一用途，写进合集目录）。"""
    title = str(content.get("collection_title") or "合集总览")
    lines = [f"# {title}", ""]
    theme = str(content.get("theme") or "").strip()
    if theme:
        lines += [f"> {theme}", ""]
    conclusions = [str(item) for item in (content.get("conclusions") or []) if str(item).strip()]
    if conclusions:
        lines.append("## 核心结论")
        lines += [f"- {item}" for item in conclusions]
        lines.append("")
    order = content.get("watch_order") or []
    if order:
        lines.append("## 推荐观看顺序")
        for index, row in enumerate(order, start=1):
            reason = str(row.get("reason") or "").strip()
            lines.append(f"{index}. {row.get('title', '')}" + (f" —— {reason}" if reason else ""))
        lines.append("")
    timeline = content.get("timeline") or []
    if timeline:
        lines.append("## 分集")
        for row in timeline:
            one = str(row.get("one_line") or "").strip()
            url = str(row.get("url") or "").strip()
            tail = f"（{url}）" if url else ""
            lines.append(f"- **{row.get('title', '')}**：{one}{tail}")
        lines.append("")
    overlap = content.get("overlap") or []
    if overlap:
        lines.append("## 重复、分歧与缺口")
        for row in overlap:
            kind = str(row.get("kind") or "").strip() or "提示"
            lines.append(f"- [{kind}] {row.get('text', '')}")
        lines.append("")
    lines += [
        "---",
        "",
        f"共 {content.get('item_count') or 0} 集 · 模型 {content.get('model') or '—'}"
        f" · 角色 {content.get('role') or '—'}",
        "",
        "（本总览由 AI 依据各集总结生成，发布前请自己过一遍）",
    ]
    return "\n".join(lines)
