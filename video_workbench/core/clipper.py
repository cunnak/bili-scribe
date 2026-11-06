"""切片候选的粗筛（P3-01）。

这一层只回答一个问题：**这段视频里哪几截值得被剪出来？**
它不落库、不吸附切点、不导出——校验/去重/吸附在 P3-02，切点在 P3-04。

三个刻意的设计：

1. **模型只给"提示值"**（`start_hint_ms`）。名字里带 hint 是提醒调用方：
   这不是最终切点，P3-02 会把它吸到句边界上去。让模型直接吐最终切点，
   等于把"精确到帧"这件事交给一个连时间码都要靠换算的组件。
2. **热度是加成，不是替代**。弹幕多的位置常常只是梗，好内容不一定吵。
   所以弹幕只贡献最多 10 分，而且没有时间轴的弹幕直接不算——
   拿"高频弹幕"当热度会把它算到全片同一个位置，那是假数据。
3. **失败必须能兜底**。模型不吐 JSON 时退回规则候选（按弹幕峰值切窗口），
   用户看到的是"这几个位置看起来还行"，而不是一个红叉。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from video_workbench.app.errors import WBError
from video_workbench.infra.jsonfix import parse_json

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "resources" / "prompts" / "clip_candidates.md"
)

# 时长区间（方案书 §5.7.1）
RANGES: dict[str, tuple[int, int]] = {
    "highlight": (15_000, 90_000),
    "segment": (120_000, 480_000),
}
KIND_NAMES = {"highlight": "高光短切片", "segment": "长文分段"}
DEFAULT_COUNT = 12
PER_KIND_MAX = 8
HEAT_BONUS_MAX = 10
MISSING_TEXT_PENALTY = 12  # 缺标题/理由要降权：排序里它就该往后站
RULE_WINDOW_MS = 30_000  # 规则兜底的窗口长度
HEAT_BUCKET_MS = 5_000

# 切点吸附（P3-02）：容差来自方案书 §5.7.2 `clip.snap_tolerance_ms` 默认值
SNAP_TOLERANCE_MS = 2_000
OVERLAP_RATIO = 0.5  # 重叠超过较短那段的这个比例，就算重复

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# --------------------------------------------------------------------- prompt


def load_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 装机缺失才可能触发
        raise WBError("DEPENDENCY_MISSING", f"缺少提示词文件：{PROMPT_PATH.name}") from exc


def stamp(ms: int) -> str:
    """毫秒 → `mm:ss.mmm`。切片要精确到十分之一秒，只到秒看不出差别。"""
    value = max(0, int(ms))
    minutes, rest = divmod(value, 60_000)
    seconds, milli = divmod(rest, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milli:03d}"


def lines_text(segments: Sequence[dict[str, Any]], *, limit: int = 600) -> str:
    """字幕 → 一行一句的时间码文本。

    超过 ``limit`` 行就均匀抽样：整份字幕塞进去会让 prompt 涨到几万 token，
    而候选本来就只需要"有代表性的段落"。
    """
    rows = [seg for seg in segments or [] if str(seg.get("text", "")).strip()]
    if not rows:
        return "（没有字幕）"
    if len(rows) > limit:
        step = max(1, len(rows) // limit)
        rows = rows[::step][:limit]
    out = []
    for seg in rows:
        start = int(seg.get("start_ms") or 0)
        end = int(seg.get("end_ms") or start)
        out.append(f"[{stamp(start)}-{stamp(end)}] {str(seg.get('text', '')).strip()}")
    return "\n".join(out)


def describe_kinds(kinds: Sequence[str]) -> str:
    names = [KIND_NAMES[k] for k in kinds if k in KIND_NAMES]
    return "、".join(names) if names else "高光短切片"


# ---------------------------------------------------------------------- 热度


def heat_buckets(danmaku: Iterable[Any], *, bucket_ms: int = HEAT_BUCKET_MS) -> dict[int, int]:
    """弹幕 → 时间桶计数。

    只认带时间的弹幕（``{"time_ms": 1234}`` 或 ``{"progress_ms": 1234}``）。
    纯文本弹幕返回空表——没有时间的弹幕算不出"哪一段热闹"，
    硬拿高频词当热度会把它全算到第 0 秒，那就成了一份假数据。
    """
    buckets: dict[int, int] = {}
    span = max(1_000, int(bucket_ms))
    for item in danmaku or []:
        ms = None
        if isinstance(item, dict):
            for key in ("time_ms", "progress_ms", "stime_ms"):
                if item.get(key) is not None:
                    try:
                        ms = int(float(item[key]))
                    except (TypeError, ValueError):
                        ms = None
                    break
        if ms is None or ms < 0:
            continue
        slot = (ms // span) * span
        buckets[slot] = buckets.get(slot, 0) + 1
    return buckets


def heat_ratio(
    buckets: dict[int, int],
    start_ms: int,
    end_ms: int,
    *,
    bucket_ms: int = HEAT_BUCKET_MS,
) -> float:
    """某段里的弹幕密度，归一化到 0–1。

    分母是**最热那个桶的每秒条数**，分子是本段每秒条数——两边都除以时长，
    一段十分钟里散落着两百条弹幕才不会被算成"比五分钟里五十条更热"。
    桶按左闭右开计入，否则相邻两段的交界处会被算两遍。
    """
    if not buckets:
        return 0.0
    span_ms = max(1, int(end_ms) - int(start_ms))
    total = sum(count for slot, count in buckets.items() if int(start_ms) <= slot < int(end_ms))
    if total <= 0:
        return 0.0
    peak_density = (max(buckets.values()) or 1) / max(0.001, bucket_ms / 1000)
    density = total / max(0.001, span_ms / 1000)
    return min(1.0, density / peak_density) if peak_density > 0 else 0.0


def danmaku_note(danmaku: Sequence[Any], buckets: dict[int, int], *, limit: int = 12) -> str:
    """给模型看的弹幕摘要。**只说有什么，不说"这里好看"。**"""
    rows = list(danmaku or [])
    if not rows:
        return "（没有弹幕）"
    if not buckets:
        return f"（有 {len(rows)} 条弹幕但没有时间轴，无法定位热度；不要据此加分）"
    top = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    spots = "、".join(f"{stamp(slot)} 附近 {count} 条" for slot, count in top)
    return f"共 {len(rows)} 条弹幕，最集中的位置：{spots}"


# ---------------------------------------------------------------------- 解析


def parse_reply(raw: str) -> list[dict[str, Any]]:
    """模型输出 → 候选列表。

    容忍三件真实会发生的事：外面套了代码块围栏、前后有寒暄、``candidates``
    被写成裸数组。都不认就直接报 LLM_BAD_RESPONSE——让用户重试，
    而不是拿一个空列表假装"这段视频没亮点"。
    """
    text = str(raw or "").strip()
    if not text:
        raise WBError("LLM_BAD_RESPONSE", "模型没有返回内容")
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith(("{", "[")):
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise WBError("LLM_BAD_RESPONSE", "模型返回里找不到 JSON")
        text = text[start:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # 模型常把字符串写成多行（原始换行），严格解析会挂；先修一次再判死
        fixed = parse_json(text)
        if fixed is None:
            raise WBError("LLM_BAD_RESPONSE", f"模型返回的 JSON 解析失败：{exc.msg}") from exc
        data = fixed
    if isinstance(data, dict):
        rows = data.get("candidates")
    else:
        rows = data
    if not isinstance(rows, list):
        raise WBError("LLM_BAD_RESPONSE", "模型返回里没有 candidates 数组")
    return [item for item in rows if isinstance(item, dict)]


def _pick_ms(item: dict[str, Any], names: Sequence[str]) -> int | None:
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def normalize(
    candidates: Sequence[dict[str, Any]],
    *,
    duration_ms: int,
    kinds: Sequence[str] = ("highlight", "segment"),
    count: int = DEFAULT_COUNT,
    heat: dict[int, int] | None = None,
) -> dict[str, Any]:
    """把模型给的原始候选收拾成能存库的形状。

    做四件事，顺序不能换：**认字段 → 卡范围 → 算分 → 排序截断**。
    先排序再卡范围的话，被截短的候选会莫名其妙地排到前面去。

    返回 ``{"candidates": [...], "dropped": [{"reason": ..., "item": ...}], "notes": [...]}``。
    """
    wanted = [k for k in kinds if k in RANGES] or list(RANGES)
    total = max(0, int(duration_ms or 0))
    notes: list[str] = []
    dropped: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []

    for item in candidates or []:
        kind = str(item.get("kind", "")).strip()
        if kind not in RANGES:
            dropped.append({"reason": "kind 不认识", "item": item})
            continue
        if kind not in wanted:
            dropped.append({"reason": "不在本次要求的类型里", "item": item})
            continue
        start = _pick_ms(item, ("start_hint_ms", "start_ms", "start"))
        end = _pick_ms(item, ("end_hint_ms", "end_ms", "end"))
        if start is None or end is None:
            dropped.append({"reason": "缺起止时间", "item": item})
            continue
        start = max(0, start)
        end = min(total, end) if total else end
        if end <= start:
            dropped.append({"reason": "结束不晚于开始", "item": item})
            continue

        low, high = RANGES[kind]
        span = end - start
        trimmed = False
        if span < low:
            if start + low <= (total or start + low):
                end = start + low
            else:
                start = max(0, end - low)
            notes.append(f"「{item.get('title') or kind}」太短，已补到 {low // 1000} 秒")
            trimmed = True
        elif span > high:
            end = start + high
            notes.append(f"「{item.get('title') or kind}」太长，已截到 {high // 1000} 秒")
            trimmed = True
        if end <= start:
            dropped.append({"reason": "截断后没有长度", "item": item})
            continue

        title = str(item.get("title", "") or "").strip()
        reason = str(item.get("reason", "") or "").strip()
        raw_score = item.get("score")
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 50.0
        score = max(0.0, min(100.0, score))

        heat_bonus = 0
        if heat:
            ratio = heat_ratio(heat, start, end)
            heat_bonus = int(round(HEAT_BONUS_MAX * ratio))
        penalty = 0
        if not title:
            penalty += MISSING_TEXT_PENALTY
        if not reason:
            penalty += MISSING_TEXT_PENALTY

        tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()][:3]
        kept.append(
            {
                "kind": kind,
                "start_ms": start,
                "end_ms": end,
                "title": title or f"{KIND_NAMES[kind]} {stamp(start)}",
                "reason": reason,
                "quote": str(item.get("quote", "") or "").strip(),
                "score": int(round(max(0.0, min(100.0, score + heat_bonus - penalty)))),
                "base_score": int(round(score)),
                "heat_bonus": heat_bonus,
                "tags": tags,
                "trimmed": trimmed,
            }
        )

    kept.sort(key=lambda row: (-row["score"], row["start_ms"]))
    # 每种类型各自最多 8 个：高光给 12 个也不会有 12 段都值得剪
    counts: dict[str, int] = {}
    limited: list[dict[str, Any]] = []
    for row in kept:
        used = counts.get(row["kind"], 0)
        if used >= PER_KIND_MAX:
            dropped.append({"reason": f"{row['kind']} 已够 {PER_KIND_MAX} 个", "item": row})
            continue
        counts[row["kind"]] = used + 1
        limited.append(row)

    if len(limited) > count:
        dropped.extend({"reason": "超出总数上限", "item": row} for row in limited[count:])
        limited = limited[:count]

    return {"candidates": limited, "dropped": dropped, "notes": notes}


# ---------------------------------------------------------------------- 兜底


def rule_candidates(
    segments: Sequence[dict[str, Any]],
    *,
    duration_ms: int,
    kinds: Sequence[str] = ("highlight",),
    count: int = DEFAULT_COUNT,
    heat: dict[int, int] | None = None,
    window_ms: int = RULE_WINDOW_MS,
) -> list[dict[str, Any]]:
    """模型不可用时的规则候选。

    高光：按弹幕峰值切 ``window_ms`` 窗口；没有弹幕就沿时间轴均匀取。
    长段：从第一个句子开始，每 4 分钟切一章（章节本来就不该由"热度"决定）。

    这里明确**不评价内容好不好**，所以标题写死成"第 N 段"——假装有标题
    会让用户以为这套兜底也理解了内容。
    """
    rows = [seg for seg in segments or [] if str(seg.get("text", "")).strip()]
    total = max(int(duration_ms or 0), int(rows[-1].get("end_ms") or 0) if rows else 0)
    out: list[dict[str, Any]] = []
    wanted = [k for k in kinds if k in RANGES] or ["highlight"]

    if "highlight" in wanted and total > 0:
        starts: list[int] = []
        if heat:
            peak = max(heat.values()) or 1
            hot = [slot for slot, n in heat.items() if n >= max(2, peak * 0.6)]
            starts = sorted(hot)
        if not starts:
            step = max(window_ms, total // max(1, count))
            starts = list(range(0, max(1, total - window_ms + 1), step))[:count]
        for index, raw in enumerate(starts[:count], start=1):
            end = min(total, raw + window_ms)
            if end - raw < RANGES["highlight"][0]:
                raw = max(0, end - window_ms)
                end = min(total, raw + window_ms)
            if end <= raw:
                continue
            out.append(
                {
                    "kind": "highlight",
                    "start_ms": int(raw),
                    "end_ms": int(end),
                    "title": f"第 {index} 段（规则候选）",
                    "reason": "模型没给出可用结果，按时间窗口切的",
                    "quote": "",
                    "score": 50,
                    "base_score": 50,
                    "heat_bonus": 0,
                    "tags": [],
                    "trimmed": False,
                }
            )

    if "segment" in wanted and total > 0:
        chapter = 240_000
        pieces = max(1, min(PER_KIND_MAX, (total + chapter - 1) // chapter))
        size = total // pieces
        for index in range(pieces):
            start = index * size
            end = total if index == pieces - 1 else (index + 1) * size
            out.append(
                {
                    "kind": "segment",
                    "start_ms": start,
                    "end_ms": end,
                    "title": f"第 {index + 1} 章（规则候选）",
                    "reason": "按等长章节切的，模型没给出可用结果",
                    "quote": "",
                    "score": 50,
                    "base_score": 50,
                    "heat_bonus": 0,
                    "tags": [],
                    "trimmed": False,
                }
            )
    return out


def _refine_packed(
    packed: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    total: int,
    *,
    snap: bool,
    tolerance_ms: int,
) -> dict[str, Any]:
    """normalize 之后再走一遍吸附/去重，并把两份 notes 合成一份（去重保序）。"""
    if not snap:
        return packed
    out = refine(packed["candidates"], rows, duration_ms=total, tolerance_ms=tolerance_ms)
    notes = list(dict.fromkeys(list(packed["notes"]) + list(out["notes"])))
    return {
        "candidates": out["candidates"],
        "dropped": list(packed["dropped"]) + list(out["dropped"]),
        "notes": notes,
    }


# ---------------------------------------------------------------------- 入口


def generate(
    segments: Sequence[dict[str, Any]],
    *,
    client: Any,
    duration_ms: int = 0,
    kinds: Sequence[str] = ("highlight", "segment"),
    count: int = DEFAULT_COUNT,
    danmaku: Sequence[Any] | None = None,
    model: str = "",
    temperature: float = 0.4,
    prompt_template: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    retries: int = 1,
    snap: bool = True,
    tolerance_ms: int = SNAP_TOLERANCE_MS,
) -> dict[str, Any]:
    """跑一次候选生成：LLM 优先，失败退回规则。

    返回值里 ``source`` 说明这批候选是谁给的（``llm`` / ``rule``）——
    界面必须显示这个，不然兜底结果看起来和模型精挑的一模一样。
    """
    rows = [seg for seg in segments or [] if str(seg.get("text", "")).strip()]
    if not rows:
        raise WBError("CONFLICT", "这个项目还没有字幕，先转录再挑切片")
    total = int(duration_ms or 0) or max(int(seg.get("end_ms") or 0) for seg in rows)
    wanted = [k for k in kinds if k in RANGES] or list(RANGES)
    buckets = heat_buckets(danmaku or [])
    notes: list[str] = []
    if danmaku and not buckets:
        notes.append("弹幕没有时间轴，热度没有计入")

    template = prompt_template if prompt_template is not None else load_prompt()
    prompt = (
        template.replace("$duration", f"{total // 1000} 秒（{stamp(total)}）")
        .replace("$kinds", describe_kinds(wanted))
        .replace("$count", str(max(1, int(count))))
        .replace("$lines", lines_text(rows))
        .replace("$danmaku", danmaku_note(danmaku or [], buckets))
    )
    system = "你是严谨的视频剪辑编导。只输出 JSON，不解释。"

    usage: dict[str, Any] = {}
    model_name = model
    last_error: Exception | None = None
    attempt = 0
    while attempt <= max(0, retries):
        attempt += 1
        if on_progress:
            on_progress(f"第 {attempt} 次请求候选")
        try:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
            try:
                reply = client.chat(messages, json_mode=True, temperature=temperature)
            except TypeError:
                # 客户端不认 json_mode/temperature（早期桩、别的实现）→ 退化成裸调用。
                # 这一层必须自己兜住：写在 except 块里的调用不会再被下面的
                # `except Exception` 接住，模型没配时会一路抛到任务外面。
                reply = client.chat(messages)
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权都走兜底
            last_error = exc
            notes.append(f"模型请求失败：{type(exc).__name__}")
            continue
        text = reply if isinstance(reply, str) else str(getattr(reply, "text", "") or "")
        try:
            raw = parse_reply(text)
        except WBError as exc:
            last_error = exc
            notes.append(exc.message)
            continue
        packed = _refine_packed(
            normalize(raw, duration_ms=total, kinds=wanted, count=count, heat=buckets),
            rows,
            total,
            snap=snap,
            tolerance_ms=tolerance_ms,
        )
        if not packed["candidates"]:
            last_error = WBError("LLM_BAD_RESPONSE", "模型给的候选都被校验掉了")
            notes.extend(packed["notes"])
            continue
        notes.extend(packed["notes"])
        return {
            "candidates": packed["candidates"],
            "dropped": packed["dropped"],
            "notes": notes,
            "source": "llm",
            "model": model_name or str(getattr(client, "model", "") or ""),
            "usage": usage,
            "attempts": attempt,
            "duration_ms": total,
            "heat_buckets": len(buckets),
        }

    fallback = rule_candidates(rows, duration_ms=total, kinds=wanted, count=count, heat=buckets)
    if not fallback:
        detail = last_error.message if isinstance(last_error, WBError) else str(last_error or "")
        raise WBError("LLM_BAD_RESPONSE", "模型没给出可用候选，规则兜底也切不出来", detail)
    notes.append("已改用规则候选：位置可用，但标题和分数没有内容含义")
    packed = _refine_packed(
        normalize(fallback, duration_ms=total, kinds=wanted, count=count, heat=buckets),
        rows,
        total,
        snap=snap,
        tolerance_ms=tolerance_ms,
    )
    return {
        "candidates": packed["candidates"],
        "dropped": packed["dropped"],
        "notes": notes,
        "source": "rule",
        "model": "",
        "usage": usage,
        "attempts": attempt,
        "duration_ms": total,
        "heat_buckets": len(buckets),
    }


# ------------------------------------------------------------------ 切点吸附（P3-02）


def _rows(segments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        {
            "start_ms": int(seg.get("start_ms") or 0),
            "end_ms": int(seg.get("end_ms") or 0),
            "text": str(seg.get("text") or "").strip(),
            "words": list(seg.get("words") or []),
        }
        for seg in segments or []
    ]
    return sorted((row for row in rows if row["text"]), key=lambda row: row["start_ms"])


def _nearest(value: int, points: Sequence[int], tolerance_ms: int) -> tuple[int, bool]:
    """value 到最近的点，超出容差就不动。返回 (新值, 是否吸附成功)。"""
    if not points:
        return value, False
    best = min(points, key=lambda point: (abs(point - value), point))
    if abs(best - value) <= max(0, tolerance_ms):
        return best, True
    return value, False


def _word_points(rows: Sequence[dict[str, Any]]) -> tuple[list[int], list[int]]:
    """纯词级边界。没有词级时间戳就返回两个空表——空表本身就是"这里没有词边界"。"""
    starts: list[int] = []
    ends: list[int] = []
    for row in rows:
        for word in row.get("words") or []:
            if not str(word.get("text") or "").strip():
                continue
            starts.append(int(word.get("start_ms") or 0))
            ends.append(int(word.get("end_ms") or 0))
    return sorted(set(starts)), sorted(set(ends))


def _boundaries(rows: Sequence[dict[str, Any]], mode: str) -> tuple[list[int], list[int]]:
    """吸附候选点：``mode="word"`` 且真有词级时间戳时用词边界，否则退回句边界。"""
    starts = sorted({row["start_ms"] for row in rows})
    ends = sorted({row["end_ms"] for row in rows})
    if mode != "word":
        return starts, ends
    word_starts, word_ends = _word_points(rows)
    if not word_starts or not word_ends:
        return starts, ends
    return word_starts, word_ends


def trim_silence(segments: Sequence[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    """不要用静音开头/结尾。

    起止点落在两句话之间的空隙里时，起点往后挪到下一句开头、终点往前挪到上一句结尾。
    这条比"吸附到最近边界"更硬：宁可少半句，也不要用户一打开就是两秒静音。
    """
    rows = _rows(segments)
    start = max(0, int(start_ms))
    end = max(start, int(end_ms))
    notes: list[str] = []
    covered = [row for row in rows if row["end_ms"] > start and row["start_ms"] < end]
    if not covered:
        return {"start_ms": start, "end_ms": end, "trimmed_ms": 0, "notes": ["这一段里没有语音"]}

    inside_start = next((row for row in covered if row["start_ms"] <= start < row["end_ms"]), None)
    if inside_start is None:
        first = min(covered, key=lambda row: row["start_ms"])
        notes.append(f"开头 {first['start_ms'] - start} 毫秒是静音，已剪掉")
        start = first["start_ms"]

    inside_end = next((row for row in covered if row["start_ms"] < end <= row["end_ms"]), None)
    if inside_end is None:
        last = max(covered, key=lambda row: row["end_ms"])
        notes.append(f"结尾 {end - last['end_ms']} 毫秒是静音，已剪掉")
        end = last["end_ms"]

    return {"start_ms": start, "end_ms": end, "trimmed_ms": max(0, end - start), "notes": notes}


def snap_window(
    segments: Sequence[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    *,
    tolerance_ms: int = SNAP_TOLERANCE_MS,
    mode: str = "sentence",
) -> dict[str, Any]:
    """把起止点吸到句（或词）边界上。

    吸不上就**保持原值并把 ``boundary`` 留空**——前端据此给用户标一句
    「这段的切点不在句边界」，而不是偷偷挪到一个看起来整洁的位置。
    """
    rows = _rows(segments)
    starts, ends = _boundaries(rows, mode)
    word_starts, word_ends = _word_points(rows)
    start, start_ok = _nearest(int(start_ms), starts, tolerance_ms)
    end, end_ok = _nearest(int(end_ms), ends, tolerance_ms)
    if end <= start:
        return {
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "boundary": "",
            "notes": ["吸附后没有长度，已保留原值"],
        }
    notes: list[str] = []
    boundary = ""
    if start_ok and end_ok:
        # 标签要诚实：要的是词边界、但这段没有词级时间戳时，实际吸的是句边界
        both_words = start in word_starts and end in word_ends
        boundary = "word" if (mode == "word" and both_words) else "sentence"
    else:
        if not start_ok:
            notes.append("起点不在句边界上，已保留原值")
        if not end_ok:
            notes.append("终点不在句边界上，已保留原值")
    return {
        "start_ms": start,
        "end_ms": end,
        "boundary": boundary,
        "notes": notes,
    }


def _clamp_span(
    start: int, end: int, low: int, high: int, duration_ms: int
) -> tuple[int, int, list[str]]:
    """吸附后可能变短/变长，再按类型区间夹一次。"""
    notes: list[str] = []
    total = max(0, int(duration_ms or 0))
    start = max(0, start)
    end = min(total, end) if total else end
    if end <= start:
        return start, end, ["吸附后没有长度"]
    span = end - start
    if span < low:
        if start + low <= (total or start + low):
            end = start + low
        else:
            start = max(0, end - low)
        notes.append(f"吸附后太短，已补到 {low // 1000} 秒")
    elif span > high:
        end = start + high
        notes.append(f"吸附后太长，已截到 {high // 1000} 秒")
    return start, end, notes


def dedupe(
    candidates: Sequence[dict[str, Any]], *, overlap_ratio: float = OVERLAP_RATIO
) -> dict[str, Any]:
    """同一类型里重叠太多的候选只留一个（留分高的）。

    不同类型之间不算重复：一段 3 分钟的 `segment` 里包含一个 20 秒的 `highlight`
    是很正常的事，它们要分别导成不同尺寸的片子。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in sorted(
        candidates, key=lambda item: (-int(item.get("score") or 0), int(item["start_ms"]))
    ):
        clash = None
        for other in kept:
            if other["kind"] != row["kind"]:
                continue
            overlap = min(int(other["end_ms"]), int(row["end_ms"])) - max(
                int(other["start_ms"]), int(row["start_ms"])
            )
            if overlap <= 0:
                continue
            shorter = min(
                int(other["end_ms"]) - int(other["start_ms"]),
                int(row["end_ms"]) - int(row["start_ms"]),
            )
            if shorter > 0 and overlap / shorter >= max(0.0, min(1.0, overlap_ratio)):
                clash = other
                break
        if clash is None:
            kept.append(row)
        else:
            dropped.append(
                {
                    "reason": f"与「{clash.get('title') or '另一条'}」重叠超过 {int(overlap_ratio * 100)}%",
                    "item": row,
                }
            )
    return {"candidates": kept, "dropped": dropped}


def refine(
    candidates: Sequence[dict[str, Any]],
    segments: Sequence[dict[str, Any]],
    *,
    duration_ms: int = 0,
    tolerance_ms: int = SNAP_TOLERANCE_MS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> dict[str, Any]:
    """P3-02 整条流水线：剪静音 → 吸边界 → 夹区间 → 去重。

    顺序有讲究：先剪静音（否则吸附会把起点吸回到静音前那一句的末尾），
    再吸边界（把剩下的误差对齐到句/词），最后才去重（重叠是吸附之后才算得准的）。
    """
    rows = _rows(segments)
    total = int(duration_ms or 0) or (rows[-1]["end_ms"] if rows else 0)
    notes: list[str] = []
    snapped: list[dict[str, Any]] = []

    for row in candidates or []:
        kind = str(row.get("kind") or "")
        low, high = RANGES.get(kind, (0, 0))
        raw_start = int(row.get("start_ms") or 0)
        raw_end = int(row.get("end_ms") or raw_start)
        item = dict(row)
        item.setdefault("raw_start_ms", raw_start)
        item.setdefault("raw_end_ms", raw_end)

        trimmed = trim_silence(rows, raw_start, raw_end)
        notes.extend(trimmed["notes"])
        snapped_window = snap_window(
            rows, trimmed["start_ms"], trimmed["end_ms"], tolerance_ms=tolerance_ms
        )
        notes.extend(snapped_window["notes"])
        start, end, clamp_notes = _clamp_span(
            snapped_window["start_ms"], snapped_window["end_ms"], low, high, total
        )
        notes.extend(clamp_notes)

        item["start_ms"] = start
        item["end_ms"] = end
        item["boundary"] = snapped_window["boundary"]
        item["snap_notes"] = trimmed["notes"] + snapped_window["notes"] + clamp_notes
        item["silence_trimmed_ms"] = max(0, raw_end - raw_start) - (end - start)
        snapped.append(item)

    result = dedupe(snapped, overlap_ratio=overlap_ratio)
    return {"candidates": result["candidates"], "dropped": result["dropped"], "notes": notes}


# --------------------------------------------------------------- 入库形状（P3-03）


def to_rows(
    candidates: Sequence[dict[str, Any]], *, source: str = "ai", origin: str = ""
) -> list[dict[str, Any]]:
    """把候选换成 `repo.candidate_insert` 要的 kwargs。

    吸附过程中的原值与原因（`raw_*` / `boundary` / `snap_notes`）不进独立列，
    统一收进 `notes`——它们是"这次怎么算出来的"记录，不参与查询。

    `source` 是库里的两档（`ai` / `manual`）；模型给的和规则兜底的都算 `ai`，
    谁给的写进 `notes.origin`（`llm` / `rule`）。界面必须能看出"这批是兜底凑的"。
    """
    rows: list[dict[str, Any]] = []
    for item in candidates or []:
        start = int(item.get("start_ms") or 0)
        end = int(item.get("end_ms") or start)
        rows.append(
            {
                "kind": str(item.get("kind") or "highlight"),
                "start_ms": start,
                "end_ms": end,
                "score": int(item.get("score") or 0),
                "title": str(item.get("title") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "quote": str(item.get("quote") or "").strip(),
                "tags": list(item.get("tags") or []),
                "source": source,
                "notes": {
                    "origin": origin or source,
                    "raw_start_ms": int(item.get("raw_start_ms") or start),
                    "raw_end_ms": int(item.get("raw_end_ms") or end),
                    "boundary": str(item.get("boundary") or ""),
                    "snap_notes": list(item.get("snap_notes") or []),
                    "silence_trimmed_ms": int(item.get("silence_trimmed_ms") or 0),
                },
            }
        )
    return rows
