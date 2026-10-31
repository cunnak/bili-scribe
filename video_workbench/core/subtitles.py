"""字幕段的纯函数层（P2-03）。

**为什么这些操作要放在服务端。** 界面上的"分割/合并/平移/批量替换"看似可以在
JS 里改完再整份保存，但那样有两个后果：一是每次操作都要把 1000 段全量传一遍，
二是任何一种编辑的语义只存在于前端——导出、切片、总结各自又要重写一遍。所以
编辑一律走这里的函数，前端只发"意图"。

**段的基本形状**（与 ``transcript.json`` 一致）::

    {"id": "s1", "start_ms": 0, "end_ms": 1500, "text": "…", "words": [...]}

不变量：``0 <= start_ms < end_ms``；同一份时间轴上按 start 升序。函数出参保证
这一点，调用方不必自己排序。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 一句太长会在画面上糊成两行半，阅读体验差；太短则闪得人眼累。
MAX_CHARS_PER_LINE = 22
MAX_SEGMENT_MS = 12_000
MIN_SEGMENT_MS = 400
GAP_WARN_MS = 30      # 相邻段之间的小缝不报，人耳听不出来
OVERLAP_WARN_MS = 50  # 重叠超过这个值基本是编辑事故


def normalize(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按开始时间排序，并给缺 id 的段补一个稳定的 id。

    id 必须稳定：前端拿它做虚拟列表的 key、撤销栈的锚点。所以这里不重新编号
    已有 id，只补缺的。
    """
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for seg in segments:
        item = dict(seg)
        item["start_ms"] = int(item.get("start_ms") or 0)
        item["end_ms"] = int(item.get("end_ms") or 0)
        item["text"] = str(item.get("text") or "")
        item.setdefault("words", [])
        sid = str(item.get("id") or "")
        if not sid or sid in used:
            n = 1
            while f"s{n}" in used or f"s{n}" == sid:
                n += 1
            sid = f"s{n}"
        item["id"] = sid
        used.add(sid)
        out.append(item)
    out.sort(key=lambda s: (s["start_ms"], s["end_ms"]))
    return out


def window(segments: list[dict[str, Any]], start_ms: int,
            end_ms: int = 0) -> list[dict[str, Any]]:
    """裁出源视频 ``[start_ms, end_ms]`` 这段的字幕，并把时间平移成以 0 起算。

    切片烧录用。整批平移**不能**复用 `shift()`：那个函数为了不出负数时间码，会把
    偏移量夹到"最早一条不被推到负数"，而切片的偏移是按窗口起点定的——夹完就成了
    没平移，全错位。这里按段各自夹：窗口前的整段丢掉，跨起点的截断到 0。
    """
    base = int(start_ms or 0)
    out: list[dict[str, Any]] = []
    for seg in normalize(segments):
        s, e = int(seg["start_ms"]), int(seg["end_ms"])
        if e <= base:
            continue
        ns = max(0, s - base)
        ne = max(ns + 1, e - base)
        if end_ms > base:
            cut = int(end_ms) - base
            if ns >= cut:
                continue
            ne = min(ne, cut)
        item = dict(seg)
        item["start_ms"], item["end_ms"] = ns, ne
        words = []
        for word in seg.get("words") or []:
            ws, we = int(word.get("start_ms") or 0), int(word.get("end_ms") or 0)
            if we <= base or (end_ms > base and ws >= end_ms):
                continue
            copy = dict(word)
            copy["start_ms"] = max(0, ws - base)
            copy["end_ms"] = max(copy["start_ms"], min(we - base, ne))
            words.append(copy)
        if words:
            item["words"] = words
        item.pop("id", None)
        out.append(item)
    return renumber(out)


def renumber(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """重排后把 id 归一成 s1..sN，便于人读日志。编辑操作内部会调。"""
    for index, seg in enumerate(segments, start=1):
        seg["id"] = f"s{index}"
    return segments


def find(segments: list[dict[str, Any]], seg_id: str) -> int:
    for index, seg in enumerate(segments):
        if seg.get("id") == seg_id:
            return index
    raise KeyError(seg_id)


# ------------------------------------------------------------------ 编辑操作


def update_text(segments: list[dict[str, Any]], seg_id: str, text: str) -> list[dict[str, Any]]:
    out = normalize(segments)
    out[find(out, seg_id)]["text"] = str(text)
    return out


def shift(segments: list[dict[str, Any]], offset_ms: int,
          ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """整体（或选中若干段）平移时间。

    允许平移出负数吗？不允许——负数时间码在 SRT 里是非法的，很多播放器会直接
    丢弃整条字幕。整体平移时若第一段会被推成负值，就把整批统一截断到 0，这样
    相对间隔仍然保持，只是整体后移的量被吃掉了。
    """
    out = normalize(segments)
    target = set(ids or [])
    picked = [s for s in out if not target or s["id"] in target]
    if not picked:
        return out
    offset = int(offset_ms)
    if offset < 0:
        earliest = min(s["start_ms"] for s in picked)
        offset = max(offset, -earliest)
    for seg in picked:
        seg["start_ms"] = max(0, seg["start_ms"] + offset)
        seg["end_ms"] = max(seg["start_ms"] + 1, seg["end_ms"] + offset)
        for word in seg.get("words") or []:
            word["start_ms"] = max(0, int(word.get("start_ms") or 0) + offset)
            word["end_ms"] = max(word["start_ms"], int(word.get("end_ms") or 0) + offset)
    return normalize(out)


def split(segments: list[dict[str, Any]], seg_id: str, at_ms: int) -> list[dict[str, Any]]:
    """在 at_ms 处把一段切成两段，文本按词边界就近分配。

    切点落在词中间时按词分配：词表里最后一个 ``start_ms < at_ms`` 的词归前段。
    没有词级时间戳时退回按字符比例分配，这总比"把整句复制两遍"好。
    """
    out = normalize(segments)
    index = find(out, seg_id)
    seg = out[index]
    at = int(at_ms)
    if not (seg["start_ms"] < at < seg["end_ms"]):
        raise ValueError("切点不在这一段之内")

    words = list(seg.get("words") or [])
    if words:
        # 切点落在词中间时，按"最近词边界"落刀：找第一个开始时间 ≥ at 的词，
        # 它之前的都归前段。这样不会出现"前段吞掉整句、后段空着"。
        cut_index = next((i for i, w in enumerate(words) if int(w.get("start_ms") or 0) >= at), None)
        if cut_index in (None, 0):
            # at 落在最后一个词里（或第一个词之前）：把这个词按字符比例拆开
            target = 0 if cut_index == 0 else len(words) - 1
            word = dict(words[target])
            text = str(word.get("text") or "")
            span = max(1, int(word.get("end_ms") or at) - int(word.get("start_ms") or at))
            ratio = max(0.0, min(1.0, (at - int(word.get("start_ms") or 0)) / span))
            cut = max(0, min(len(text), int(round(len(text) * ratio))))
            if not text[:cut].strip():
                cut = min(len(text), cut + 1)
            head = dict(word, text=text[:cut], end_ms=at)
            tail = dict(word, text=text[cut:], start_ms=at)
            words = words[:target] + [head] + ([tail] if cut < len(text) else []) + words[target + 1:]
            cut_index = target + 1
        before, after = words[:cut_index], words[cut_index:]
        left_text = "".join(str(w.get("text") or "") for w in before).strip()
        right_text = "".join(str(w.get("text") or "") for w in after).strip()
        left_end = int(before[-1].get("end_ms") or at) if before else at
        right_start = int(after[0].get("start_ms") or at) if after else at
    else:
        text = str(seg.get("text") or "")
        ratio = (at - seg["start_ms"]) / max(1, seg["end_ms"] - seg["start_ms"])
        cut = max(0, min(len(text), int(round(len(text) * ratio))))
        left_text, right_text = text[:cut].strip(), text[cut:].strip()
        left_end, right_start = at, at
        before, after = [], []

    if not left_text or not right_text:
        raise ValueError("切点落在空白处，切出来会有一半是空句")

    left = {"id": seg["id"], "start_ms": seg["start_ms"], "end_ms": max(seg["start_ms"] + 1, left_end),
            "text": left_text, "words": before}
    right = {"id": f"{seg['id']}b", "start_ms": min(max(seg["start_ms"] + 1, right_start), seg["end_ms"] - 1),
             "end_ms": seg["end_ms"], "text": right_text, "words": after}
    out[index:index + 1] = [left, right]
    return renumber(normalize(out))


def merge(segments: list[dict[str, Any]], ids: Iterable[str]) -> list[dict[str, Any]]:
    """把选中的若干段合成一段。必须时间上连续（中间允许夹别的段吗？不允许）。

    "选中第 1、3 段"这种情况在界面上不该发生——如果发生了，说明用户其实想删中间
    那段。所以这里直接拒绝，而不是悄悄把中间那段也吞进来。
    """
    out = normalize(segments)
    picked = [str(i) for i in ids]
    if len(picked) < 2:
        raise ValueError("至少要选两段才谈得上合并")
    indexes = sorted(find(out, i) for i in picked)
    if indexes[-1] - indexes[0] != len(indexes) - 1:
        raise ValueError("只能合并相邻的段")

    block = out[indexes[0]:indexes[-1] + 1]
    words: list[dict[str, Any]] = []
    pieces: list[str] = []
    for seg in block:
        words.extend(seg.get("words") or [])
        text = str(seg.get("text") or "").strip()
        if text:
            pieces.append(text)
    merged = {
        "id": block[0]["id"],
        "start_ms": block[0]["start_ms"],
        "end_ms": block[-1]["end_ms"],
        "text": _join_text(pieces),
        "words": words,
    }
    out[indexes[0]:indexes[-1] + 1] = [merged]
    return renumber(normalize(out))


def replace_text(segments: list[dict[str, Any]], pattern: str, replacement: str,
                 *, regex: bool = False, ids: Iterable[str] | None = None,
                 case_sensitive: bool = True) -> tuple[list[dict[str, Any]], int]:
    """批量替换，返回 (新段表, 命中条数)。

    没有命中就原样返回，且**不改**任何东西——否则一次误操作也会把整份字幕重排。
    """
    if not pattern:
        raise ValueError("要替换的内容不能为空")
    out = normalize(segments)
    target = set(ids or [])
    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    hits = 0
    for seg in out:
        if target and seg["id"] not in target:
            continue
        text = str(seg.get("text") or "")
        new_text, count = matcher.subn(replacement, text)
        if count:
            hits += count
            seg["text"] = new_text
            seg["words"] = []  # 文本被改过，词级时间戳就不可信了
    return (out if hits else segments), hits


def delete(segments: list[dict[str, Any]], ids: Iterable[str]) -> list[dict[str, Any]]:
    drop = {str(i) for i in ids}
    out = [dict(s) for s in normalize(segments) if s["id"] not in drop]
    return renumber(out)


def insert(segments: list[dict[str, Any]], segment: dict[str, Any]) -> list[dict[str, Any]]:
    """手动插一条（转录漏句时的补救手段）。时间冲突不拦，交给 qc 标黄。"""
    out = normalize(segments)
    out.append(dict(segment))
    return renumber(normalize(out))


# ------------------------------------------------------------------ 质量检查


def qc(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回问题清单，每条带 level/kind/segment_ids/message。

    只报能定位的问题。"这条字幕可能翻错了"这种判断不在这里——那是 LLM 优化
    （P2-04）的活，检测器不该替人做决定。
    """
    items = normalize(segments)
    issues: list[dict[str, Any]] = []
    for index, seg in enumerate(items):
        span = seg["end_ms"] - seg["start_ms"]
        text = str(seg.get("text") or "")
        if not text.strip():
            issues.append(_issue("empty", "error", [seg["id"]], "字幕内容为空"))
        if seg["end_ms"] <= seg["start_ms"]:
            issues.append(_issue("negative", "error", [seg["id"]], "结束时间不大于开始时间"))
        elif span < MIN_SEGMENT_MS:
            issues.append(_issue("too_short", "warn", [seg["id"]], f"时长只有 {span}ms，一眼看不清"))
        elif span > MAX_SEGMENT_MS:
            issues.append(_issue("too_long", "warn", [seg["id"]], f"时长 {span / 1000:.1f}s，建议分割"))
        if len(text) > MAX_CHARS_PER_LINE * 2:
            issues.append(_issue("too_many_chars", "warn", [seg["id"]], f"{len(text)} 字，一行放不下"))
        if index:
            prev = items[index - 1]
            gap = seg["start_ms"] - prev["end_ms"]
            if gap < -OVERLAP_WARN_MS:
                issues.append(_issue("overlap", "error", [prev["id"], seg["id"]],
                                     f"与上一条重叠 {-gap}ms"))
            elif gap > 60_000:
                issues.append(_issue("big_gap", "info", [prev["id"], seg["id"]],
                                     f"中间空了 {gap / 1000:.0f}s，可能有漏句"))
    return issues


def _issue(kind: str, level: str, ids: list[str], message: str) -> dict[str, Any]:
    return {"kind": kind, "level": level, "segment_ids": ids, "message": message}


def _join_text(pieces: list[str]) -> str:
    """合并时拼接文本：中日文不加空格，英文之间补空格。"""
    out = ""
    for piece in pieces:
        if not out:
            out = piece
            continue
        if _is_ascii_word(out[-1]) and _is_ascii_word(piece[0]):
            out += " " + piece
        else:
            out += piece
    return out


def _is_ascii_word(char: str) -> bool:
    return bool(char) and char.isascii() and (char.isalnum() or char in ",.!?;:'\"")


def stats(segments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """给界面用的汇总：段数、总字数、时长、每分钟字数（判断语速是否合理）。"""
    items = normalize(segments)
    chars = sum(len(str(s.get("text") or "").strip()) for s in items)
    span = (items[-1]["end_ms"] - items[0]["start_ms"]) if items else 0
    minutes = span / 60_000 if span else 0
    return {
        "count": len(items),
        "chars": chars,
        "span_ms": span,
        "chars_per_minute": round(chars / minutes, 1) if minutes else 0.0,
        "first_ms": items[0]["start_ms"] if items else 0,
        "last_ms": items[-1]["end_ms"] if items else 0,
    }


# ------------------------------------------------------------------ ASS 写出
#
# 这里只放**够用的最小 ASS**：双语字幕要能直接扔进播放器看效果（P2-05 的验收项）。
# 完整导出（读 ``wb_style`` 的样式、按输出高度缩放字号与边距、VTT、软/硬字幕）
# 是 P2-06 的活，届时扩展这个函数而不是另起一个——两份 ASS 转义规则迟早各说各话。

ASS_HEADER = """[Script Info]
; VideoWorkbench 生成
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{primary},{outline_color},{back},{bold},0,0,0,100,100,{spacing},0,{border_style},{outline},{shadow},{alignment},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 720P 是字号基准（方案书 5.11.2）：导出 1080P 时字号/描边/边距按 1.5 倍等比放大，
# 否则播放器把 720P 的字号直接摆到 1080P 画面上，字会显得又小又飘。
ASS_BASE_HEIGHT = 720

# 没有 wb_style 可用时的兜底。数值与 ``wb_style`` 内置 default 同源，
# 不一致会让「样式页看到 54」和「导出得到 48」对不上。
ASS_DEFAULT = {
    "font": "Microsoft YaHei",
    "size": 54,
    "outline": 3,
    "margin_v": 42,
    "primary": "#FFFFFF",
    "outline_color": "#000000",
    "shadow": 1,
    "bold": True,
}


def ass_color(value: Any, fallback: str = "#FFFFFF") -> str:
    """``#RRGGBB`` → ASS 的 ``&HAABBGGRR``。

    ASS 是 **BGR** 顺序，且 alpha 是「0 = 不透明」，和 CSS 正好相反；
    直接塞 ``&H00FFFFFF`` 只是恰好白得对，换成红色立刻看出问题。
    """
    return ass_color_alpha(value, fallback, 1.0)


def ass_color_alpha(value: Any, fallback: str = "#FFFFFF", opacity: float = 0.0) -> str:
    """同上，但带透明度。``opacity`` 是 CSS 口径（1 = 不透明），写进 ASS 前翻转。

    圆角背景条要靠这个：ASS 的 ``&H00`` 前三段是 alpha，``&H99`` ≈ 40% 可见。
    """
    text = str(value or "").strip().lstrip("#")
    if len(text) == 8:  # 已经带 alpha
        text = text[2:]
    if len(text) != 6:
        text = str(fallback).lstrip("#")
    try:
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        r, g, b = 255, 255, 255
    alpha = max(0.0, min(1.0, float(opacity if opacity == opacity else 0.0)))
    aa = int(round((1.0 - alpha) * 255))
    return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}"


def style_to_ass(style: dict[str, Any] | None) -> dict[str, Any]:
    """把 ``wb_style.config`` 归一成 ASS 要的那几个键。

    字段名在样式表里是 ``font_size / outline_width / margin_v``，
    在 ASS 里是 ``size / outline / margin_v``；这层映射只写一次，
    免得每个调用点各记一套别名。

    P3-07 起样式可能是**嵌套**的（``primary{}`` / ``secondary{}`` / ``rounded{}``，
    方案书 5.11.2）。嵌套优先、平铺别名兜底：老库里 ``default`` 那行是平铺的，
    直接改字段会让「样式页显示 48、导出还是 54」。
    """
    conf = dict(ASS_DEFAULT)
    src = style or {}

    def pick(*names: str) -> Any:
        for name in names:
            if name in src and src[name] is not None:
                return src[name]
        return None

    face = src.get("primary") if isinstance(src.get("primary"), dict) else None
    rnd = src.get("rounded") if isinstance(src.get("rounded"), dict) else None
    mode = str(src.get("mode") or "").strip().lower()
    if rnd and (mode == "rounded" or face is None):
        # 圆角条：libass 没有圆角，用 BorderStyle=3 的实心框近似（框色 = OutlineColour）
        conf.update({
            "font": rnd.get("font") or conf["font"],
            "size": rnd.get("size") or conf["size"],
            "outline": rnd.get("padding_v") or 0,
            "margin_v": rnd.get("margin_bottom") or conf["margin_v"],
            "primary": rnd.get("text_color") or conf["primary"],
            "outline_color": rnd.get("bg_color") or conf["outline_color"],
            "shadow": 0,
            "bold": False,
            "border_style": 3,
            "spacing": rnd.get("letter_spacing") or 0,
            "back_alpha": round(1.0 - float(rnd.get("bg_alpha") or 0), 3),
        })
        return conf
    if face:
        conf.update({
            "font": face.get("font") or conf["font"],
            "size": face.get("size") or conf["size"],
            "outline": face.get("outline_width") if face.get("outline_width") is not None else conf["outline"],
            "primary": face.get("color") or conf["primary"],
            "outline_color": face.get("outline_color") or conf["outline_color"],
            "bold": bool(face.get("bold")),
            "spacing": face.get("spacing") or 0,
        })

    for key, names in (
        ("font", ("font", "font_name", "fontname")),
        ("size", ("font_size", "size")),
        ("outline", ("outline_width", "outline")),
        ("margin_v", ("margin_v", "margin_bottom")),
        ("primary", ("primary_color", "text_color", "primary")),
        ("outline_color", ("outline_color", "border_color")),
        ("shadow", ("shadow",)),
        ("bold", ("bold",)),
        ("spacing", ("letter_spacing", "spacing")),
        ("border_style", ("border_style",)),
        ("alignment", ("alignment",)),
        ("back_alpha", ("back_alpha",)),
    ):
        value = pick(*names)
        if value is not None:
            conf[key] = value
    return conf


def scale_style(
    style: dict[str, Any] | None,
    height: int,
    *,
    base_height: int = ASS_BASE_HEIGHT,
) -> dict[str, Any]:
    """按输出高度的比例放大字号/描边/边距，返回 ASS 归一后的键。"""
    conf = style_to_ass(style)
    factor = max(0.25, float(height or base_height) / float(base_height or ASS_BASE_HEIGHT))
    conf["size"] = max(8, int(round(float(conf["size"]) * factor)))
    conf["outline"] = max(0, int(round(float(conf["outline"]) * factor)))
    conf["margin_v"] = max(0, int(round(float(conf["margin_v"]) * factor)))
    conf["shadow"] = max(0, int(round(float(conf.get("shadow") or 0) * factor)))
    return conf


def ass_time(value: Any) -> str:
    """ASS 时间戳：``h:mm:ss.cc``——百分之一秒，不是毫秒，写错一位就差 10 倍。"""
    total = max(0, int(value or 0))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, ms = divmod(rest, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{ms // 10:02d}"


def _ass_escape(text: Any) -> str:
    """ASS 文本里没有转义语法，但 ``{}`` 会被当覆盖标签，换行必须换成 ``\\N``。"""
    return (
        str(text or "")
        .replace("{", "（")
        .replace("}", "）")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def format_ass(
    segments: Iterable[dict[str, Any]],
    *,
    font: str = "",
    size: int = 0,
    outline: int = 0,
    margin_v: int = 0,
    style: dict[str, Any] | None = None,
    height: int = ASS_BASE_HEIGHT,
) -> str:
    """把段写成 ASS。``lines`` 是两行时用 ``\\N`` 连接（双语字幕）。

    取文本优先级：``lines``（``translator.merge_bilingual`` 的输出）> ``text``。
    这样双语导出不用为了拼两行再造一份数据结构。

    ``style`` 是 ``wb_style.config`` 原样传进来；显式给了 ``size/outline/margin_v``
    就以显式值为准（调用方明确要的数字不该被配置盖掉）。
    """
    conf = scale_style(style, height)
    if font:
        conf["font"] = font
    if size > 0:
        conf["size"] = int(size)
    if outline > 0:
        conf["outline"] = int(outline)
    if margin_v > 0:
        conf["margin_v"] = int(margin_v)
    bold = "-1" if conf.get("bold") else "0"
    border_style = int(conf.get("border_style") or 1)
    # 圆角条（BorderStyle=3）的底色吃 OutlineColour；BackColour 只在含阴影时可见，
    # 这里仍给同一个颜色，免得播放器把阴影画成纯黑边。
    back_alpha = float(conf.get("back_alpha") if conf.get("back_alpha") is not None else 0.5)
    back = ass_color_alpha(conf.get("outline_color") if border_style == 3 else "#000000",
                           "#000000", back_alpha)
    header = ASS_HEADER.format(
        font=_ass_escape(conf["font"]),
        size=int(conf["size"]),
        outline=int(conf["outline"]),
        shadow=int(conf.get("shadow") or 0),
        margin_v=int(conf["margin_v"]),
        primary=ass_color(conf.get("primary"), "#FFFFFF"),
        outline_color=ass_color(conf.get("outline_color"), "#000000"),
        back=back,
        bold=bold,
        spacing=int(conf.get("spacing") or 0),
        border_style=border_style,
        alignment=int(conf.get("alignment") or 2),
    )
    out = [header]
    for seg in normalize(segments):
        raw = seg.get("lines")
        if isinstance(raw, list) and raw:
            body = "\\N".join(_ass_escape(line) for line in raw if str(line).strip())
        else:
            body = _ass_escape(seg.get("text") or "")
        # 只有空白的行也算空：ASS 里它是个"什么都不显示但占了时间"的事件，
        # 导出后数行数会多出来，用户以为丢了一句又找不到。
        if not body.strip():
            continue
        out.append(
            f"Dialogue: 0,{ass_time(seg['start_ms'])},{ass_time(seg['end_ms'])},"
            f"Default,,0,0,0,,{body}"
        )
    return "\n".join(out) + "\n"


def vtt_time(value: Any) -> str:
    """VTT 时间戳：``HH:MM:SS.mmm``——毫秒三位，和 SRT 的逗号不同。"""
    total = max(0, int(value or 0))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def format_vtt(segments: Iterable[dict[str, Any]], *, header: str = "") -> str:
    """WEBVTT。双语时 ``lines`` 直接写成两行正文——VTT 没有 ``\\N`` 这种转义。"""
    out = ["WEBVTT", ""]
    if header.strip():
        out = ["WEBVTT", f"NOTE {header.strip()}", ""]
    for seg in normalize(segments):
        raw = seg.get("lines")
        if isinstance(raw, list) and raw:
            body = "\n".join(str(line) for line in raw if str(line).strip())
        else:
            body = str(seg.get("text") or "")
        if not body.strip():
            continue
        # VTT 正文里 "-->" 会被当成新的定时行，出现就换成全角箭头
        body = body.replace("-->", "→")
        out.append(f"{vtt_time(seg['start_ms'])} --> {vtt_time(seg['end_ms'])}")
        out.append(body)
        out.append("")
    return "\n".join(out)
