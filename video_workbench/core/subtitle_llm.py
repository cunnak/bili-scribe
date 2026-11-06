"""字幕 LLM 优化（P2-04）。

规则全部来自方案书 §5.5.3：**补标点、合并碎句、去口头禅、修错别字；不改时间戳、
不改语义、不扩写。** 所以这个模块只碰 ``text`` 字段，一句时间码都不碰。

三个必须被代码而不是被 prompt 保证的东西：

1. **失败不破坏原文。** 每一批独立成败；解析不出来、id 对不上、长度异常，都只记一条
   ``failed``，原文原样留下。整批全失败也不会写库。
2. **结果必须可回滚。** 应用前把旧 segments 存成快照，回滚就是把快照写回去。
3. **模型说了不算。** 返回的每一行都要过校验：id 集合一致、没有换行、长度在合理区间。
   模型偶尔会"顺便润色"整段文字，这类输出会被长度校验挡掉。

合并的表达方式：模型把被合并走的行写成空字符串 ``""``。应用时把该段并进相邻段
（保留第一段的起点与最后一段的终点），这样时间轴仍然连续、不丢内容。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from ..infra.llm import ChatClient, ChatError

PROMPT_PATH = Path(__file__).resolve().parents[1] / "resources" / "prompts" / "subtitle_optimize.md"
DEFAULT_BATCH = 30
MAX_GROWTH = 2.0  # 超过原文两倍＝模型在扩写，直接判失败
MIN_RATIO = 0.25  # 掉到四分之一以下＝模型在丢内容
MAX_CHARS = 240  # 单行超过这个长度基本是合并错了段

Change = dict[str, Any]


# ------------------------------------------------------------------ prompt


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_prompt(segments: Iterable[dict[str, Any]], template: str | None = None) -> str:
    """把一批分段塞进模板。编号用真实的 segment id，回来才对得上。"""
    items = list(segments)
    lines = "\n".join(
        f"{seg.get('id')}\t[{int(seg.get('start_ms') or 0)}-{int(seg.get('end_ms') or 0)}]\t{seg.get('text') or ''}"
        for seg in items
    )
    text = template if template is not None else load_prompt()
    return text.replace("$count", str(len(items))).replace("$lines", lines)


def split_batches(
    segments: Iterable[dict[str, Any]], size: int = DEFAULT_BATCH
) -> list[list[dict[str, Any]]]:
    items = list(segments)
    step = max(1, int(size or DEFAULT_BATCH))
    return [items[i : i + step] for i in range(0, len(items), step)]


# ------------------------------------------------------------------ 解析


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_reply(raw: str) -> dict[str, str]:
    """从模型回复里抠出 ``{id: text}``。宽容到什么程度：允许代码围栏、允许前后废话。"""
    text = (raw or "").strip()
    if not text:
        raise ChatError("LLM_BAD_RESPONSE", "模型返回了空内容")
    block = _FENCE.search(text)
    if block:
        text = block.group(1).strip()
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            raise ChatError("LLM_BAD_RESPONSE", "模型没有返回 JSON 数组", text[:300])
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ChatError("LLM_BAD_RESPONSE", "模型返回的 JSON 解析失败", text[:300]) from exc
    if not isinstance(data, list):
        raise ChatError("LLM_BAD_RESPONSE", "模型返回的不是数组", text[:300])

    out: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        seg_id = str(item.get("id") or "").strip()
        if not seg_id:
            continue
        out[seg_id] = str(item.get("text") if item.get("text") is not None else "").strip()
    return out


def check_batch(before: list[dict[str, Any]], after: dict[str, str]) -> list[str]:
    """校验一批结果。返回问题清单，空清单表示可以用。"""
    problems: list[str] = []
    expected = [str(seg.get("id")) for seg in before]
    missing = [sid for sid in expected if sid not in after]
    extra = [sid for sid in after if sid not in set(expected)]
    if missing:
        problems.append(
            f"缺少 {len(missing)} 行（{missing[:3]}…）"
            if len(missing) > 3
            else f"缺少行：{missing}"
        )
    if extra:
        problems.append(
            f"多出 {len(extra)} 行（{extra[:3]}…）" if len(extra) > 3 else f"多出未知行：{extra}"
        )

    for seg in before:
        sid = str(seg.get("id"))
        if sid not in after:
            continue
        old = str(seg.get("text") or "").strip()
        new = after[sid]
        if "\n" in new or "\r" in new:
            problems.append(f"{sid} 含换行")
            continue
        if not new:
            continue  # 空 = 该行被合并走，允许
        if len(new) > MAX_CHARS:
            problems.append(f"{sid} 太长（{len(new)} 字）")
            continue
        if old:
            if len(new) > len(old) * MAX_GROWTH + 8:
                problems.append(f"{sid} 疑似扩写（{len(old)} → {len(new)} 字）")
            elif len(new) < len(old) * MIN_RATIO:
                problems.append(f"{sid} 疑似丢内容（{len(old)} → {len(new)} 字）")
    return problems


# ------------------------------------------------------------------ 主流程


def build_changes(before: list[dict[str, Any]], after: dict[str, str]) -> list[Change]:
    """只留真正变了的行，按原顺序。界面靠这个列表做 diff 预览。"""
    changes: list[Change] = []
    for seg in before:
        sid = str(seg.get("id"))
        old = str(seg.get("text") or "")
        if sid not in after:
            continue
        new = after[sid]
        if new.strip() == old.strip():
            continue
        changes.append(
            {
                "id": sid,
                "start_ms": int(seg.get("start_ms") or 0),
                "end_ms": int(seg.get("end_ms") or 0),
                "before": old,
                "after": new,
                "kind": "merge" if not new else "text",
            }
        )
    return changes


def optimize(
    segments: Iterable[dict[str, Any]],
    client: ChatClient,
    *,
    batch_size: int = DEFAULT_BATCH,
    temperature: float = 0.2,
    cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    prompt_template: str | None = None,
) -> dict[str, Any]:
    """跑完所有批次，返回 ``{changes, failed, batches, model}``。不写库、不改文件。"""
    client.require()
    items = list(segments)
    if not items:
        raise ChatError("VALIDATION", "没有可优化的字幕")

    template = prompt_template if prompt_template is not None else load_prompt()
    groups = split_batches(items, batch_size)
    changes: list[Change] = []
    failed: list[dict[str, Any]] = []
    done = 0

    for group in groups:
        if cancel is not None and cancel():
            failed.append(
                {"ids": [str(g.get("id")) for g in group], "code": "CANCELED", "message": "已取消"}
            )
            break
        try:
            raw = client.chat(
                [
                    {"role": "system", "content": "你只输出 JSON 数组，不输出任何解释。"},
                    {"role": "user", "content": build_prompt(group, template)},
                ],
                temperature=temperature,
            )
            after = parse_reply(raw)
            problems = check_batch(group, after)
            if problems:
                raise ChatError("LLM_BAD_RESPONSE", "模型输出不合规", "；".join(problems[:4]))
            changes.extend(build_changes(group, after))
        except ChatError as exc:
            # 一批失败不影响别的批：这正是"失败不破坏原文"的落点
            failed.append(
                {
                    "ids": [str(g.get("id")) for g in group],
                    "code": exc.code,
                    "message": getattr(exc, "message", str(exc)),
                    "detail": getattr(exc, "detail", "")[:300],
                }
            )
        done += 1
        if on_progress is not None:
            on_progress(done, len(groups))

    return {
        "changes": changes,
        "failed": failed,
        "batches": len(groups),
        "segments": len(items),
        "model": client.model,
        "changed": len(changes),
    }


def apply_changes(
    segments: Iterable[dict[str, Any]],
    changes: Iterable[Change],
    accepted_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """把（被接受的）改动落到 segments 上。空文本＝并进前一段。"""
    from . import subtitles as sub

    use = {
        str(c["id"]): str(c.get("after") or "")
        for c in changes
        if accepted_ids is None or str(c["id"]) in accepted_ids
    }
    current = sub.normalize(segments)
    if not use:
        return current

    merged: list[dict[str, Any]] = []
    for seg in current:
        sid = str(seg.get("id"))
        if sid in use and use[sid] == "":
            # 被合并走的行：把它并进上一条（没有上一条就留给下一条，极端情况）
            if merged:
                merged[-1] = {
                    **merged[-1],
                    "end_ms": int(seg.get("end_ms") or merged[-1]["end_ms"]),
                }
                continue
            merged.append(seg)
            continue
        if sid in use and use[sid] != str(seg.get("text") or ""):
            seg = {**seg, "text": use[sid]}
        merged.append(seg)
    return sub.normalize(merged)
