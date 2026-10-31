"""字幕样式（P3-07）。

职责只有三件，别让它长胖：

1. **预设的形状与默认值**——`default`（描边）/ `rounded-default`（圆角条）。字段语义
   对齐 VideoCaptioner（方案书附录 C），但只对齐能力，不复制它的文件。
2. **校验**——字号/描边/圆角/透明度范围、主副字号差、圆角条宽度警告。校验放在
   这一层而不是界面：JSON 导入、手工改库、RPC 直调都得过同一道闸。
3. **翻译成 ASS**——样式字段是"720P 基准 + 语义命名"，ASS 要的是"目标像素 + BGR"。
   这层转换只写一次，导出与预览共用，否则「预览看着对、导出不一样」是必然的。

真正的 ASS 字符串拼装仍在 `subtitles.format_ass`（它是格式 owner），这里只负责
把嵌套的样式压平成它认识的键。
"""

from __future__ import annotations

from typing import Any

from ..app.errors import WBError
from . import subtitles

# ---------------------------------------------------------------- 常量

MODES = ("ass", "rounded")
LAYOUTS = ("original_top", "translated_top", "original_only", "translated_only")
TEXT_LENGTHS = ("short", "medium", "long")
ORIENTATIONS = ("landscape", "portrait")
BACKGROUNDS = ("dark", "light", "image")

FONT_SIZE_MIN = 12
FONT_SIZE_MAX = 120
OUTLINE_MAX = 10
RADIUS_MAX = 40
MIN_SIZE_GAP = 4
BAR_WARN_RATIO = 0.90

#: 720P 基准（方案书 5.11.2）。导出时按高度折算，不把目标值写回样式库。
BASE_HEIGHT = subtitles.ASS_BASE_HEIGHT

_SAMPLE = {
    "short": "这里是短句",
    "medium": "先把反例摆出来，说明直觉为什么不可靠",
    "long": "先把反例摆出来，说明直觉为什么不可靠；再把变量拆开，逐个控制做对比；最后落到可复现的操作顺序上",
}


def _primary(size: int = 48) -> dict[str, Any]:
    return {
        "font": "Microsoft YaHei",
        "size": size,
        "spacing": 0,
        "color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 2,
        "bold": False,
    }


def _preview() -> dict[str, Any]:
    return {"text_length": "medium", "orientation": "landscape", "background": "dark"}


def default_preset() -> dict[str, Any]:
    """内置 ``default``：普通 ASS，描边 + 阴影，主副字号 48/36。"""
    return {
        "id": "default",
        "name": "默认",
        "mode": "ass",
        "layout": "original_top",
        "primary": _primary(48),
        "secondary": _primary(36),
        "rounded": {
            "font": "Microsoft YaHei",
            "size": 40,
            "text_color": "#FFFFFF",
            "bg_color": "#000000",
            "bg_alpha": 0.6,
            "corner_radius": 8,
            "padding_h": 16,
            "padding_v": 8,
            "margin_bottom": 120,
            "line_spacing": 4,
            "letter_spacing": 0,
            "primary_box": True,
            "secondary_box": True,
        },
        "preview": _preview(),
    }


def rounded_preset() -> dict[str, Any]:
    """内置 ``rounded-default``：圆角背景条，白字压半透明黑底。"""
    style = default_preset()
    style.update({"id": "rounded-default", "name": "圆角默认", "mode": "rounded"})
    return style


def builtin_presets() -> list[dict[str, Any]]:
    return [default_preset(), rounded_preset()]


# ---------------------------------------------------------------- 工具


def _num(value: Any, fallback: float, *, integer: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return float(fallback)
    return float(int(round(number))) if integer else number


def _bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _merge_face(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    face = dict(base)
    if isinstance(patch, dict):
        for key, value in patch.items():
            if key not in face:
                continue  # 不认识的键直接丢，别把脏数据带进库里
            if key == "bold":
                face[key] = _bool(value, bool(face[key]))
            elif key in {"font", "color", "outline_color"}:
                face[key] = str(value or face[key])
            else:
                face[key] = int(_num(value, float(face[key])))
    return face


def normalize(payload: Any, *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把任意输入（界面表单 / 导入的 JSON / 库里的旧行）套成完整样式。

    缺字段用 ``base``（默认内置 default）补，多字段丢掉。**不做范围裁剪**——
    越界值要能报错给用户看，静默夹到边界只会让人以为设置生效了。
    """
    src = payload if isinstance(payload, dict) else {}
    seed = normalize_base(base) if base else default_preset()
    out: dict[str, Any] = {
        "id": str(src.get("id") or seed.get("id") or ""),
        "name": str(src.get("name") or seed.get("name") or ""),
        "mode": str(src.get("mode") or seed.get("mode") or "ass").strip().lower(),
        "layout": str(src.get("layout") or seed.get("layout") or "original_top").strip().lower(),
        "primary": _merge_face(seed["primary"], src.get("primary")),
        "secondary": _merge_face(seed["secondary"], src.get("secondary")),
        "rounded": _merge_rounded(seed["rounded"], src.get("rounded")),
        "preview": _merge_preview(seed["preview"], src.get("preview")),
    }
    return out


def _merge_rounded(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    rnd = dict(base)
    if not isinstance(patch, dict):
        return rnd
    for key, value in patch.items():
        if key not in rnd:
            continue
        if key in {"primary_box", "secondary_box"}:
            rnd[key] = _bool(value, bool(rnd[key]))
        elif key in {"font", "text_color", "bg_color"}:
            rnd[key] = str(value or rnd[key])
        elif key == "bg_alpha":
            rnd[key] = round(_num(value, float(rnd[key]), integer=False), 3)
        else:
            rnd[key] = int(_num(value, float(rnd[key])))
    return rnd


def _merge_preview(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    prev = dict(base)
    if isinstance(patch, dict):
        for key in ("text_length", "orientation", "background"):
            value = str(patch.get(key) or "").strip().lower()
            if value:
                prev[key] = value
    return prev


def normalize_base(base: dict[str, Any] | None) -> dict[str, Any]:
    """把库里的行（``{id,name,mode,config}``）摊成完整样式。

    老库里的 ``default`` 是**平铺**的（``font_size`` / ``primary_color`` / ``outline_width``，
    P0 时代的写法）。这里做一次字段搬家，而不是让 `style_to_ass` 永远同时认两套：
    搬家只发生在"读"这一侧，写回去就都是新结构了。
    """
    if not base:
        return default_preset()
    conf = base.get("config") if isinstance(base.get("config"), dict) else base
    conf = dict(conf) if isinstance(conf, dict) else {}
    if "primary" not in conf:
        legacy = {
            "size": conf.get("font_size"),
            "font": conf.get("font") or conf.get("font_name"),
            "color": conf.get("primary_color") or conf.get("text_color"),
            "outline_color": conf.get("outline_color") or conf.get("border_color"),
            "outline_width": conf.get("outline_width"),
            "bold": conf.get("bold"),
        }
        legacy = {k: v for k, v in legacy.items() if v is not None}
        if legacy:
            conf["primary"] = legacy
            # 副字幕没自己的老字段，按主字幕缩 12 号，至少保持层级
            secondary = dict(legacy)
            if "size" in secondary:
                secondary["size"] = max(12, int(secondary["size"]) - 12)
            conf["secondary"] = secondary
    if "rounded" not in conf and conf.get("margin_bottom") is not None:
        conf["rounded"] = {"margin_bottom": conf.get("margin_bottom")}
    merged = normalize(conf, base=None)
    if base.get("id"):
        merged["id"] = str(base["id"])
    if base.get("name"):
        merged["name"] = str(base["name"])
    if base.get("mode"):
        merged["mode"] = str(base["mode"])
    return merged


def preset_key(key: str) -> str:
    text = str(key or "").strip()
    for preset in builtin_presets():
        if text and text in {preset["id"], preset["name"]}:
            return str(preset["id"])
    return text


# ---------------------------------------------------------------- 校验


def _check_face(face: dict[str, Any], label: str, problems: list[str]) -> None:
    size = int(face.get("size") or 0)
    if not FONT_SIZE_MIN <= size <= FONT_SIZE_MAX:
        problems.append(f"{label}字号要在 {FONT_SIZE_MIN}–{FONT_SIZE_MAX}，现在是 {size}")
    outline = int(face.get("outline_width") or 0)
    if not 0 <= outline <= OUTLINE_MAX:
        problems.append(f"{label}描边要在 0–{OUTLINE_MAX}，现在是 {outline}")
    spacing = int(face.get("spacing") or 0)
    if not -20 <= spacing <= 20:
        problems.append(f"{label}字间距要在 -20–20，现在是 {spacing}")


def validate(style: dict[str, Any], *, allow_warnings: bool = True) -> tuple[dict[str, Any], list[str]]:
    """返回 ``(样式, 警告)``；硬错误抛 ``VALIDATION`` 并一次列全。

    只返回第一条错会让人改一轮跑一轮；这里把所有问题攒起来一起说。
    """
    mode = str(style.get("mode") or "").strip().lower()
    if mode not in MODES:
        raise WBError("VALIDATION", f"样式模式只能是 {' / '.join(MODES)}，现在是 {mode!r}")
    layout = str(style.get("layout") or "").strip().lower()
    if layout not in LAYOUTS:
        raise WBError("VALIDATION", f"版式只能是 {' / '.join(LAYOUTS)}，现在是 {layout!r}")

    problems: list[str] = []
    primary = style.get("primary") or {}
    secondary = style.get("secondary") or {}
    _check_face(primary, "主字幕", problems)
    _check_face(secondary, "副字幕", problems)

    gap = int(secondary.get("size") or 0) - int(primary.get("size") or 0)
    if gap < 0:
        gap = -gap
    if gap < MIN_SIZE_GAP:
        problems.append(f"主副字幕字号至少差 {MIN_SIZE_GAP}，现在是 {gap}")

    rnd = style.get("rounded") or {}
    radius = int(rnd.get("corner_radius") or 0)
    if not 0 <= radius <= RADIUS_MAX:
        problems.append(f"圆角要在 0–{RADIUS_MAX}，现在是 {radius}")
    alpha = float(rnd.get("bg_alpha") or 0)
    if not 0.0 <= alpha <= 1.0:
        problems.append(f"背景透明度要在 0–1，现在是 {alpha}")
    for key, label, low in (("padding_h", "水平内边距", 0), ("padding_v", "垂直内边距", 0),
                            ("margin_bottom", "底边距", 0)):
        value = int(rnd.get(key) or 0)
        if value < low or value > 400:
            problems.append(f"{label}要在 0–400，现在是 {value}")

    prev = style.get("preview") or {}
    for key, allowed, label in (("text_length", TEXT_LENGTHS, "预览文本长度"),
                                ("orientation", ORIENTATIONS, "预览画幅"),
                                ("background", BACKGROUNDS, "预览背景")):
        value = str(prev.get(key) or "")
        if value not in allowed:
            problems.append(f"{label}只能是 {' / '.join(allowed)}，现在是 {value!r}")

    if problems:
        raise WBError("VALIDATION", "；".join(problems), {"fields": problems})

    warnings: list[str] = []
    if mode == "rounded" and allow_warnings:
        ratio = bar_ratio(style)
        if ratio > BAR_WARN_RATIO:
            warnings.append(
                f"圆角条宽度约占画面 {int(round(ratio * 100))}%，超过 {int(BAR_WARN_RATIO * 100)}% 会顶到边"
            )
    return style, warnings


# ---------------------------------------------------------------- 几何估算


def sample_text(length: str = "medium") -> str:
    return _SAMPLE.get(str(length or "medium"), _SAMPLE["medium"])


def text_width(text: str, size: int, *, cjk_ratio: float = 1.0, latin_ratio: float = 0.55) -> float:
    """粗略估文本像素宽：中日韩按 1 个字宽，拉丁按 0.55。

    只为「圆角条会不会顶边」和预览用，不追求排版精度——真要精确得加载字体，
    那是 P6 的活，现在的判断标准是「有没有明显顶边」。
    """
    total = 0.0
    for char in str(text or ""):
        total += size * (cjk_ratio if ord(char) > 0x2E80 else latin_ratio)
    return total


def bar_ratio(style: dict[str, Any], *, canvas_width: int = 1280) -> float:
    rnd = style.get("rounded") or {}
    text = sample_text((style.get("preview") or {}).get("text_length") or "medium")
    width = text_width(text, int(rnd.get("size") or 40)) + 2 * int(rnd.get("padding_h") or 0)
    return width / float(canvas_width or 1280)


# ---------------------------------------------------------------- ASS 转换


def to_ass_style(style: dict[str, Any], *, height: int = BASE_HEIGHT) -> dict[str, Any]:
    """把样式压成 ``subtitles.format_ass`` 认识的键（目标高度已折算）。

    主字幕优先；``mode=rounded`` 时切成「背景框」形态：
    BorderStyle=3（libass 的实心框走 OutlineColour）+ 0 阴影 + 圆角近似为 0。
    """
    conf = normalize(style)
    factor = max(0.25, float(height or BASE_HEIGHT) / float(BASE_HEIGHT or 720))
    if conf["mode"] == "rounded":
        rnd = conf["rounded"]
        size = max(8, int(round(int(rnd["size"]) * factor)))
        pad_v = max(0, int(round(int(rnd["padding_v"]) * factor)))
        pad_h = max(0, int(round(int(rnd["padding_h"]) * factor)))
        alpha = max(0.0, min(1.0, float(rnd.get("bg_alpha") or 0)))
        return {
            "font": rnd["font"],
            "size": size,
            "outline": pad_v,           # BorderStyle=3 时 Outline 就是框的扩边
            "margin_v": max(0, int(round(int(rnd["margin_bottom"]) * factor))),
            "primary": rnd["text_color"],
            "outline_color": rnd["bg_color"],
            "shadow": 0,
            "bold": False,
            "border_style": 3,
            "spacing": int(round(int(rnd.get("letter_spacing") or 0) * factor)),
            "alignment": 2,
            "back_alpha": 1.0 - alpha,  # ASS 的 alpha 是反的
            "padding_h": pad_h,
            "corner_radius": int(rnd.get("corner_radius") or 0),
        }
    face = conf["primary"]
    return {
        "font": face["font"],
        "size": max(8, int(round(int(face["size"]) * factor))),
        "outline": max(0, int(round(int(face["outline_width"]) * factor))),
        "margin_v": max(0, int(round(42 * factor))),
        "primary": face["color"],
        "outline_color": face["outline_color"],
        "shadow": 1,
        "bold": bool(face.get("bold")),
        "border_style": 1,
        "spacing": int(round(int(face.get("spacing") or 0) * factor)),
        "alignment": 2,
        "back_alpha": 0.5,
    }


def canvas_size(style: dict[str, Any]) -> dict[str, int]:
    """预览画布尺寸。横屏 1280×720、竖屏 720×1280，与样式基准同源。"""
    if str((style.get("preview") or {}).get("orientation") or "landscape") == "portrait":
        return {"width": 720, "height": 1280}
    return {"width": 1280, "height": 720}


def preview_segments(style: dict[str, Any], *, count: int = 3,
                     texts: list[str] | None = None,
                     total_ms: int = 3000) -> list[dict[str, Any]]:
    """造几条预览段。时间不影响静态预览，但 ASS 需要起止时间。"""
    length = str((style.get("preview") or {}).get("text_length") or "medium")
    body = texts or [sample_text(length) for _ in range(max(1, count))]
    step = max(200, int(total_ms / max(1, len(body))))
    out: list[dict[str, Any]] = []
    for index, text in enumerate(body):
        out.append({
            "id": f"p{index + 1}",
            "start_ms": index * step,
            "end_ms": min(total_ms, (index + 1) * step),
            "text": text,
        })
    return out


def preview_ass(style: dict[str, Any], *, height: int = BASE_HEIGHT,
                texts: list[str] | None = None) -> str:
    """预览用的完整 ASS 文本（可直接喂 ffmpeg 的 ``ass`` 滤镜）。"""
    conf = to_ass_style(style, height=height)
    segs = preview_segments(style, texts=texts)
    return subtitles.format_ass(segs, style=conf, height=height)


def summary_row(style: dict[str, Any]) -> dict[str, Any]:
    """列表页要的轻量摘要：别把整份 config 塞进每一行。"""
    conf = normalize(style)
    return {
        "id": conf["id"],
        "name": conf["name"],
        "mode": conf["mode"],
        "layout": conf["layout"],
        "size": int(conf["primary"]["size"]),
        "font": conf["primary"]["font"],
    }
