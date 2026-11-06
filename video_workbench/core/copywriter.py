"""切片文案：标题 / 简介 / 标签（P3-08）。

一条切片出去，观众先看到的是标题和简介，其次才是内容。所以这里的约束
都是"平台会硬拦"的那种：标题 ≤30 字、简介 ≤120 字、标签 3–5 个。

模型只负责**给候选**；超长、缺标签、标签重复一律在这里收口——界面显示什么
长度，就是平台会看到的长度，不要留到发布页再被拦。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Sequence

from ..app.errors import WBError

PROMPT_PATH = Path(__file__).resolve().parents[1] / "resources" / "prompts" / "clip_copy.md"

TITLE_MAX = 30
DESC_MAX = 120
TAGS_MIN = 3
TAGS_MAX = 5
TAG_MAX = 8
# 这些词在平台侧搜不到任何东西，占着 3–5 个名额只是浪费
TAG_STOP = (
    "视频",
    "剪辑",
    "片段",
    "精彩",
    "干货",
    "分享",
    "推荐",
    "原创",
    "短片",
)
DEFAULT_TEMPERATURE = 0.4

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_WS = re.compile(r"\s+")

# 平台侧没有「必须带这些字」的规则，但文案里这些词等于没写：
# 模型很爱回「精彩片段 精彩内容」，验收时看不出问题，发出去就是废标题。
EMPTY_WORDS = ("精彩片段", "精彩内容", "干货分享", "值得一看", "视频分享", "片段分享")


class CopyError(WBError):
    """文案生成失败。走 LLM_ERROR/LLM_BAD_RESPONSE 这一类码。"""


def load_prompt(path: Path | None = None) -> str:
    target = Path(path) if path else PROMPT_PATH
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CopyError("INTERNAL", "读不到文案提示词模板", str(exc)) from exc


# ------------------------------------------------------------------ 文本工具


def squeeze(text: Any) -> str:
    """压掉换行与多空格。标题/简介里的换行在投稿框里会变成怪东西。"""
    return _WS.sub(" ", str(text or "")).strip()


def cut(text: str, limit: int) -> str:
    """按字数截断。全角与半角都算一个字（中文平台的「字」就是 len）。"""
    value = squeeze(text)
    if len(value) <= limit:
        return value
    return value[: max(1, limit)].rstrip("，。、；：,.;: ")


def normalize_tags(raw: Any, *, fallback: Sequence[str] = ()) -> list[str]:
    """标签清洗：去 `#`、去空、去重、压长度，最多 TAGS_MAX 个。"""
    items: list[Any]
    if isinstance(raw, str):
        items = re.split(r"[,，、\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    for item in items:
        tag = squeeze(item).lstrip("#＃")
        # 话题词里不该有空格：「产品 思维」在平台侧会被当成两个词
        tag = _WS.sub("", tag).replace(",", "").replace("，", "")
        if not tag:
            continue
        # 平台侧搜不到任何东西的空词，占着 3–5 个名额只是浪费
        if tag in TAG_STOP:
            continue
        if len(tag) > TAG_MAX:
            tag = tag[:TAG_MAX]
        if tag in out:
            continue
        out.append(tag)
        if len(out) >= TAGS_MAX:
            break
    for item in fallback:
        if len(out) >= TAGS_MIN:
            break
        tag = _WS.sub("", squeeze(item)).lstrip("#＃")
        if tag and tag not in out and len(tag) <= TAG_MAX:
            out.append(tag)
    return out[:TAGS_MAX]


def keyword_tags(text: str, *, limit: int = TAGS_MAX) -> list[str]:
    """兜底标签：从字幕里挑最能当检索词的 2–6 字片段。

    不做分词（没引入依赖）：先按标点切句，长句再按「和/与/及/的」这类连接词劈开，
    还不够就退化成固定长度滑窗。够用就好——界面会写明这是兜底文案。
    """
    source = squeeze(text)
    chunks = re.split(r"[，。、；：！？,.;:!?\s]+", source)
    out: list[str] = []
    for chunk in chunks:
        for piece in re.split(r"[和与及的地得而就都还]", chunk):
            piece = piece.strip("的了吗呢吧啊是在")
            piece = piece[:6]
            if len(piece) < 2 or piece in out:
                continue
            out.append(piece)
            if len(out) >= limit:
                return out
    # 到最后还是不够：拿固定长度的串顶上（「定价策略和商业判断」这种没标点的长句）
    flat = _WS.sub("", source)
    for size in (4, 3, 2):
        for index in range(0, max(0, len(flat) - size + 1)):
            piece = flat[index : index + size]
            if piece in out:
                continue
            out.append(piece)
            if len(out) >= limit:
                return out
    return out[:limit]


def is_empty_title(title: str) -> bool:
    word = squeeze(title)
    if len(word) < 4:
        return True
    return any(word == item for item in EMPTY_WORDS)


# ------------------------------------------------------------------ 提示词


def lines_text(segments: Sequence[dict[str, Any]], *, limit: int = 40) -> str:
    """把切片范围内的字幕拼成块。超过 limit 句就取中间部分（首尾常见寒暄）。"""
    rows = [seg for seg in segments or [] if squeeze(seg.get("text"))]
    if len(rows) > limit:
        start = max(0, (len(rows) - limit) // 2)
        rows = rows[start : start + limit]
    return "\n".join(squeeze(seg.get("text")) for seg in rows) or "（这段没有字幕）"


def build_prompt(
    candidate: dict[str, Any],
    segments: Sequence[dict[str, Any]],
    *,
    project_title: str = "",
    danmaku: str = "",
    template: str | None = None,
) -> str:
    span = max(0, int(candidate.get("end_ms") or 0) - int(candidate.get("start_ms") or 0))
    text = template if template is not None else load_prompt()
    for key, value in (
        ("$project_title", squeeze(project_title) or "（未命名）"),
        ("$kind", "高光短切片" if candidate.get("kind") == "highlight" else "长文分段"),
        ("$span", f"{span / 1000:.1f} 秒"),
        ("$candidate_title", squeeze(candidate.get("title")) or "（无）"),
        ("$reason", squeeze(candidate.get("reason")) or "（无）"),
        ("$lines", lines_text(segments)),
        ("$danmaku", danmaku or "（无）"),
        ("$title_max", str(TITLE_MAX)),
        ("$desc_max", str(DESC_MAX)),
        ("$tags_min", str(TAGS_MIN)),
        ("$tags_max", str(TAGS_MAX)),
    ):
        text = text.replace(key, value)
    return text


# ------------------------------------------------------------------ 解析


def parse_reply(raw: str) -> dict[str, Any]:
    text = _FENCE.sub("", str(raw or "")).strip()
    if not text:
        raise CopyError("LLM_BAD_RESPONSE", "模型没有返回内容")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise CopyError("LLM_BAD_RESPONSE", "模型返回的不是 JSON 对象", text[:200])
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CopyError("LLM_BAD_RESPONSE", "模型返回的 JSON 解析不了", str(exc)) from exc
    if not isinstance(data, dict):
        raise CopyError("LLM_BAD_RESPONSE", "模型返回的 JSON 不是对象")
    return data


def normalize_copy(
    data: dict[str, Any],
    *,
    candidate: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """把模型的回包收成界面能直接用的一份文案。

    超长截断、标签补齐都在这里做，并且**把做过的事记进 notes**——
    「模型给的标题 41 字，已按要求截到 30 字」这种信息必须让人看见，
    否则用户以为模型不听话，或者以为字数没限制。
    """
    bag = notes if notes is not None else []
    candidate = candidate or {}
    raw_title = squeeze(data.get("title") or data.get("name") or "")
    title = cut(raw_title, TITLE_MAX)
    if raw_title and len(squeeze(raw_title)) > len(title):
        bag.append(f"标题超长（{len(squeeze(raw_title))} 字），已截到 {TITLE_MAX} 字")
    if is_empty_title(title):
        fallback = cut(candidate.get("title") or "", TITLE_MAX)
        if fallback:
            title = fallback
            bag.append("模型给的标题太笼统，改用切片标题")
    raw_desc = squeeze(data.get("description") or data.get("desc") or data.get("summary") or "")
    description = cut(raw_desc, DESC_MAX)
    if raw_desc and len(squeeze(raw_desc)) > len(description):
        bag.append(f"简介超长（{len(squeeze(raw_desc))} 字），已截到 {DESC_MAX} 字")
    if not description:
        description = cut(candidate.get("quote") or candidate.get("reason") or "", DESC_MAX)
        if description:
            bag.append("模型没给简介，用切片里的原句顶上")
    tags = normalize_tags(data.get("tags"))
    if len(tags) < TAGS_MIN:
        extra = keyword_tags(
            " ".join([raw_desc, squeeze(candidate.get("quote")), squeeze(candidate.get("title"))])
        )
        before = len(tags)
        tags = normalize_tags(tags, fallback=extra)
        if len(tags) > before:
            bag.append(f"模型只给了 {before} 个标签，已补到 {len(tags)} 个")
    if len(tags) < TAGS_MIN:
        bag.append("标签不足 3 个，界面里手动补两个再发")
    return {"title": title, "description": description, "tags": tags}


def rule_copy(candidate: dict[str, Any], *, project_title: str = "") -> dict[str, Any]:
    """模型不可用时的兜底：全部来自候选本身，不编内容。"""
    title = (
        cut(candidate.get("title") or "", TITLE_MAX)
        or cut(project_title, TITLE_MAX)
        or "未命名切片"
    )
    quote = squeeze(candidate.get("quote"))
    reason = squeeze(candidate.get("reason"))
    description = cut(" ".join(part for part in (reason, quote) if part), DESC_MAX)
    tags = normalize_tags(candidate.get("tags"), fallback=keyword_tags(quote or reason or title))
    return {"title": title, "description": description, "tags": tags}


# ------------------------------------------------------------------ 主流程


def generate(
    candidate: dict[str, Any],
    segments: Sequence[dict[str, Any]],
    *,
    client: Any,
    project_title: str = "",
    danmaku: str = "",
    model: str = "",
    temperature: float = DEFAULT_TEMPERATURE,
    prompt_template: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    retries: int = 1,
) -> dict[str, Any]:
    """跑一次文案生成。返回值带 ``source``：``llm`` 还是 ``rule``。

    兜底不阻塞：模型没配/超时/回废话时，用切片自己的标题与原句拼一份能用的，
    界面把 ``source`` 显示出来即可（"兜底文案"）。
    """
    notes: list[str] = []
    prompt = build_prompt(
        candidate, segments, project_title=project_title, danmaku=danmaku, template=prompt_template
    )
    system = "你是中文视频编辑，写投稿文案。只输出 JSON，不解释。"
    last_error: Exception | None = None
    attempt = 0
    while attempt <= max(0, retries):
        attempt += 1
        if on_progress:
            on_progress(f"第 {attempt} 次请求文案")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        try:
            try:
                reply = client.chat(messages, json_mode=True, temperature=temperature)
            except TypeError:
                # 与 clipper 同一层兜底：写在 except 里的调用不再被外层接住
                reply = client.chat(messages)
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权都走兜底
            last_error = exc
            notes.append(f"模型请求失败：{type(exc).__name__}")
            continue
        text = reply if isinstance(reply, str) else str(getattr(reply, "text", "") or "")
        try:
            data = parse_reply(text)
        except WBError as exc:
            last_error = exc
            notes.append(exc.message)
            continue
        copy = normalize_copy(data, candidate=candidate, notes=notes)
        if not copy["title"]:
            last_error = CopyError("LLM_BAD_RESPONSE", "模型没给出标题")
            notes.append("模型没给出标题")
            continue
        return {
            "copy": copy,
            "source": "llm",
            "model": model or str(getattr(client, "model", "") or ""),
            "notes": notes,
            "attempts": attempt,
        }

    copy = rule_copy(candidate, project_title=project_title)
    notes.append("已改用兜底文案：标题、简介、标签都取自带候选，没有经过模型润色")
    detail = last_error.message if isinstance(last_error, WBError) else str(last_error or "")
    return {
        "copy": copy,
        "source": "rule",
        "model": "",
        "notes": notes,
        "attempts": attempt,
        "detail": detail,
    }


def publish_block(copy: dict[str, Any]) -> str:
    """给发布页/复制按钮用的一整块文本。"""
    title = squeeze(copy.get("title"))
    description = squeeze(copy.get("description"))
    tags = " ".join("#" + tag for tag in copy.get("tags") or [])
    return "\n".join(part for part in (title, description, tags) if part)
