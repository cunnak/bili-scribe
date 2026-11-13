"""字幕样式（P3-07 → 完整样式系统 v2）。

职责只有四件，别让它长胖：

1. **预设的形状与默认值**——`default`（描边）/ `rounded-default`（圆角条）。
   字段语义对齐 VideoCaptioner（方案书附录 C），但只对齐能力，不复制它的文件。
2. **校验**——字号/描边/圆角/透明度/缩放/角度/边距/颜色的范围与枚举。校验放在
   这一层而不是界面：JSON 导入、手工改库、RPC 直调都得过同一道闸。
3. **旧版归一**——v1 的数字是「720P 基准 + 平铺字段」，v2 是「1080P 基准 +
   主副 face」。归一只在读这一侧发生，写回去就都是 v2。
4. **参考尺寸与缩放因子**——横屏 1920×1080、竖屏 1080×1920（决策档 Q14）。

**ASS 字符串映射不在这里**：那是 `core/subtitles.py` 的活（它是格式 owner）。
`to_ass_style` 保留为兼容委托，只把请求转发过去，不维护第二套转换逻辑——
两份映射迟早各说各话，那正是「样式页看到 48、导出得到 54」的成因。
"""

from __future__ import annotations

import re
from typing import Any

from ..app.errors import WBError
from . import subtitles

# ---------------------------------------------------------------- 常量

#: 样式 schema 版本。没有这个键的配置一律按 v1（720P 基准、平铺字段）读。
SCHEMA_VERSION = 2

MODES = ("ass", "rounded")
LAYOUTS = ("original_top", "translated_top", "original_only", "translated_only")
TEXT_LENGTHS = ("short", "medium", "long")
ORIENTATIONS = ("landscape", "portrait")
BACKGROUNDS = ("dark", "light", "image")
BORDER_STYLES = (1, 3)
ALIGNMENTS = tuple(range(1, 10))

#: 参考画布（决策档 Q14）。样式里存的字号/边距都是这个尺寸下的像素。
REF_CANVAS = {"landscape": (1920, 1080), "portrait": (1080, 1920)}
REF_HEIGHT = {key: size[1] for key, size in REF_CANVAS.items()}

#: v1 的基准高度与换算系数。旧配置归一成 v2 时，尺寸类字段乘这个数。
LEGACY_BASE_HEIGHT = 720
LEGACY_SCALE = 1080 / LEGACY_BASE_HEIGHT  # 1.5

FONT_SIZE_MIN = 12
FONT_SIZE_MAX = 120
OUTLINE_MAX = 100
SHADOW_MAX = 100
BLUR_MAX = 100
SPACING_MIN = -20.0
SPACING_MAX = 20.0
SCALE_MIN = 10
SCALE_MAX = 1000
ANGLE_MIN = -360.0
ANGLE_MAX = 360.0
MARGIN_MIN = 0
MARGIN_MAX = 1000
OPACITY_MIN = 0.0
OPACITY_MAX = 1.0
RADIUS_MAX = 40
MIN_SIZE_GAP = 4
BAR_WARN_RATIO = 0.90

#: 720P 基准（方案书 5.11.2）。保留给旧调用方与单测；v2 的参考高度见 REF_HEIGHT。
BASE_HEIGHT = subtitles.ASS_BASE_HEIGHT

#: 缺字体时的确定后备（决策档 Q16：用系统字体、不打包字体文件）。
#: 挑微软雅黑而不是 Arial：中文在这个家族里一定有字形，Arial 落到中文字体上
#: 会变成"看起来随机"的宋体，用户以为设置没生效。
FONT_FALLBACK = "Microsoft YaHei"

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_SAMPLE = {
    "short": "这里是短句",
    "medium": "先把反例摆出来，说明直觉为什么不可靠",
    "long": "先把反例摆出来，说明直觉为什么不可靠；再把变量拆开，逐个控制做对比；最后落到可复现的操作顺序上",
}

#: face 字段分族。合并/缩放/校验都按这三张表走，别在三个地方各记一套键名。
FACE_BOOL_KEYS = ("bold", "italic", "underline", "strikeout")
FACE_HEX_KEYS = ("color", "outline_color", "background_color", "shadow_color")
FACE_FLOAT_KEYS = (
    "spacing",
    "opacity",
    "outline_opacity",
    "angle",
    "blur",
    "background_opacity",
    "shadow_opacity",
    "shadow_distance",
)
FACE_INT_KEYS = (
    "size",
    "outline_width",
    "scale_x",
    "scale_y",
    "alignment",
    "margin_l",
    "margin_r",
    "margin_v",
    "border_style",
)
#: 随画布高度等比缩放的字段。缩放百分比、角度、颜色、透明度**不重复缩放**。
FACE_SCALED_KEYS = (
    "size",
    "spacing",
    "outline_width",
    "blur",
    "margin_l",
    "margin_r",
    "margin_v",
    "shadow_distance",
)

#: 圆角条里随高度缩放的字段（角半径不缩放：它是几何形状，放大后不像同一块牌子）。
ROUNDED_SCALED_KEYS = (
    "size",
    "padding_h",
    "padding_v",
    "margin_bottom",
    "line_spacing",
    "letter_spacing",
)


def _face(size: int = 72) -> dict[str, Any]:
    """一个 face 的完整默认值（v2 单位：1080P 参考高度下的像素）。"""
    return {
        "font": "Microsoft YaHei",
        "size": size,
        "spacing": 0.0,
        "color": "#FFFFFF",
        "opacity": 1.0,
        "outline_color": "#000000",
        "outline_opacity": 1.0,
        "outline_width": 3,
        "bold": False,
        "italic": False,
        "underline": False,
        "strikeout": False,
        "scale_x": 100,
        "scale_y": 100,
        "angle": 0.0,
        "blur": 0.0,
        "alignment": 2,
        "margin_l": 60,
        "margin_r": 60,
        "margin_v": 63,
        "border_style": 1,
        "background_color": "#000000",
        "background_opacity": 0.5,
        "shadow_color": "#000000",
        "shadow_opacity": 1.0,
        "shadow_distance": 1.5,
    }


def _preview() -> dict[str, Any]:
    return {"text_length": "medium", "orientation": "landscape", "background": "dark"}


def _rounded() -> dict[str, Any]:
    """圆角背景条（v2 单位）。角半径不缩放，其余按 1.5 从旧值换算。"""
    return {
        "font": "Microsoft YaHei",
        "size": 60,
        "text_color": "#FFFFFF",
        "bg_color": "#000000",
        "bg_alpha": 0.6,
        "corner_radius": 8,
        "padding_h": 24,
        "padding_v": 12,
        "margin_bottom": 180,
        "line_spacing": 6,
        "letter_spacing": 0,
        "primary_box": True,
        "secondary_box": True,
    }


def default_preset() -> dict[str, Any]:
    """内置 `default`：普通 ASS，描边 + 阴影，主副字号 72/54（1080 基准）。

    旧观感不变：720 输出时 72 缩成 48、54 缩成 36，与 v1 的 48/36 逐像素等价。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "default",
        "name": "默认",
        "mode": "ass",
        "layout": "original_top",
        "vertical_gap": 12,
        "base_preset_id": "",
        "primary": _face(72),
        "secondary": _face(54),
        "rounded": _rounded(),
        "preview": _preview(),
    }


def rounded_preset() -> dict[str, Any]:
    """内置 `rounded-default`：圆角背景条，白字压半透明黑底。"""
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


def _hex(value: Any, fallback: str) -> str:
    """颜色一律收成大写 `#RRGGBB`；认不出来就保留原串，让 validate 报错。

    这里**不做**「猜一个相近颜色」：非法值要能一路走到校验闸，静默改写会让人
    以为设置生效了。
    """
    text = str(value or "").strip()
    if not text:
        return str(fallback)
    return text.upper()


def _merge_face(base: dict[str, Any], patch: Any, *, factor: float = 1.0) -> dict[str, Any]:
    face = dict(base)
    if not isinstance(patch, dict):
        return face
    for key, value in patch.items():
        if key not in face:
            continue  # 不认识的键直接丢，别把脏数据带进库里
        if key in FACE_BOOL_KEYS:
            face[key] = _bool(value, bool(face[key]))
        elif key in FACE_HEX_KEYS:
            face[key] = _hex(value, str(face[key]))
        elif key == "font":
            face[key] = str(value or face[key])
        elif key in FACE_FLOAT_KEYS:
            scale = factor if key in FACE_SCALED_KEYS else 1.0
            face[key] = round(_num(value, float(face[key]), integer=False) * scale, 3)
        else:
            scale = factor if key in FACE_SCALED_KEYS else 1.0
            face[key] = int(round(_num(value, float(face[key])) * scale))
    return face


def _merge_rounded(base: dict[str, Any], patch: Any, *, factor: float = 1.0) -> dict[str, Any]:
    rnd = dict(base)
    if not isinstance(patch, dict):
        return rnd
    for key, value in patch.items():
        if key not in rnd:
            continue
        if key in {"primary_box", "secondary_box"}:
            rnd[key] = _bool(value, bool(rnd[key]))
        elif key in {"font", "text_color", "bg_color"}:
            rnd[key] = _hex(value, str(rnd[key])) if key != "font" else str(value or rnd[key])
        elif key == "bg_alpha":
            rnd[key] = round(_num(value, float(rnd[key]), integer=False), 3)
        else:
            scale = factor if key in ROUNDED_SCALED_KEYS else 1.0
            rnd[key] = int(round(_num(value, float(rnd[key])) * scale))
    return rnd


def _merge_preview(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    prev = dict(base)
    if isinstance(patch, dict):
        for key in ("text_length", "orientation", "background"):
            value = str(patch.get(key) or "").strip().lower()
            if value:
                prev[key] = value
    return prev


def normalize(
    payload: Any, *, base: dict[str, Any] | None = None, factor: float = 1.0
) -> dict[str, Any]:
    """把任意输入（界面表单 / 导入的 JSON / 库里的旧行）套成完整样式。

    缺字段用 `base`（默认内置 default）补，多字段丢掉。**不做范围裁剪**——
    越界值要能报错给用户看，静默夹到边界只会让人以为设置生效了。

    `factor` 只给旧版归一用（v1 的 720 基准 → v2 的 1080 基准乘 1.5）：
    它只作用在**输入里真给了**的尺寸字段上，默认值本来就是 v2 单位，不该再乘。
    """
    src = payload if isinstance(payload, dict) else {}
    seed = normalize_base(base) if base else default_preset()
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": str(src.get("id") or seed.get("id") or ""),
        "name": str(src.get("name") or seed.get("name") or ""),
        "mode": str(src.get("mode") or seed.get("mode") or "ass").strip().lower(),
        "layout": str(src.get("layout") or seed.get("layout") or "original_top").strip().lower(),
        "vertical_gap": int(
            round(_num(src.get("vertical_gap"), float(seed.get("vertical_gap") or 0)) * factor)
        ),
        "base_preset_id": str(src.get("base_preset_id") or seed.get("base_preset_id") or ""),
        "primary": _merge_face(seed["primary"], src.get("primary"), factor=factor),
        "secondary": _merge_face(seed["secondary"], src.get("secondary"), factor=factor),
        "rounded": _merge_rounded(seed["rounded"], src.get("rounded"), factor=factor),
        "preview": _merge_preview(seed["preview"], src.get("preview")),
    }
    return out


def normalize_base(base: dict[str, Any] | None) -> dict[str, Any]:
    """把库里的行（`{id,name,mode,config}`）或任意旧配置摊成完整 v2 样式。

    老库里的 `default` 是**平铺**的（`font_size` / `primary_color` / `outline_width`，
    P0 时代的写法），而且数字是 **720 基准**。这里做两件事：字段搬家 + 尺寸乘 1.5。
    搬家只发生在"读"这一侧，写回去就都是新结构了。
    """
    if not base:
        return default_preset()
    conf = base.get("config") if isinstance(base.get("config"), dict) else base
    conf = dict(conf) if isinstance(conf, dict) else {}
    legacy = int(_num(conf.get("schema_version"), 1)) < SCHEMA_VERSION
    factor = LEGACY_SCALE if legacy else 1.0
    if "primary" not in conf:
        legacy_face = {
            "size": conf.get("font_size"),
            "font": conf.get("font") or conf.get("font_name"),
            "color": conf.get("primary_color") or conf.get("text_color"),
            "outline_color": conf.get("outline_color") or conf.get("border_color"),
            "outline_width": conf.get("outline_width"),
            "bold": conf.get("bold"),
        }
        legacy_face = {k: v for k, v in legacy_face.items() if v is not None}
        if legacy_face:
            conf["primary"] = legacy_face
            # 副字幕没自己的老字段，按主字幕缩 12 号（720 口径），至少保持层级
            secondary = dict(legacy_face)
            if "size" in secondary:
                secondary["size"] = max(12, int(secondary["size"]) - 12)
            conf["secondary"] = secondary
    if "rounded" not in conf and conf.get("margin_bottom") is not None:
        conf["rounded"] = {"margin_bottom": conf.get("margin_bottom")}
    merged = normalize(conf, base=None, factor=factor)
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


# ---------------------------------------------------------------- 参考尺寸


def orientation_of(style: dict[str, Any]) -> str:
    value = str((style.get("preview") or {}).get("orientation") or "landscape").strip().lower()
    return value if value in ORIENTATIONS else "landscape"


def reference_canvas(style: dict[str, Any]) -> dict[str, int]:
    """样式数字对应的参考画布。横屏 1920×1080、竖屏 1080×1920。"""
    width, height = REF_CANVAS[orientation_of(style)]
    return {"width": width, "height": height}


def ref_height(style: dict[str, Any]) -> int:
    return reference_canvas(style)["height"]


def scale_factor(style: dict[str, Any], height: int) -> float:
    """目标高度 / 参考高度。高度留 0 时按参考高度算（= 1.0）。"""
    base = float(ref_height(style) or 1080)
    target = float(height or 0) or base
    return max(0.25, target / base)


def canvas_size(style: dict[str, Any]) -> dict[str, int]:
    """**预览画布**尺寸（不是参考画布）。

    预览图固定 1280×720 / 720×1280：它只是界面里的一张示意图，按参考尺寸出图
    会让画布撑爆右栏。真实导出/烧录的高度由调用方给，缩放按参考高度算。
    """
    if orientation_of(style) == "portrait":
        return {"width": 720, "height": 1280}
    return {"width": 1280, "height": 720}


# ---------------------------------------------------------------- 校验


def _check_face(face: dict[str, Any], label: str, problems: list[str]) -> None:
    size = int(face.get("size") or 0)
    if not FONT_SIZE_MIN <= size <= FONT_SIZE_MAX:
        problems.append(f"{label}字号要在 {FONT_SIZE_MIN}–{FONT_SIZE_MAX}，现在是 {size}")
    outline = int(face.get("outline_width") or 0)
    if not 0 <= outline <= OUTLINE_MAX:
        problems.append(f"{label}描边要在 0–{OUTLINE_MAX}，现在是 {outline}")
    spacing = float(face.get("spacing") or 0)
    if not SPACING_MIN <= spacing <= SPACING_MAX:
        problems.append(f"{label}字间距要在 {SPACING_MIN:g}–{SPACING_MAX:g}，现在是 {spacing:g}")
    for key, label_text, low, high in (
        ("scale_x", "横向缩放", SCALE_MIN, SCALE_MAX),
        ("scale_y", "纵向缩放", SCALE_MIN, SCALE_MAX),
    ):
        value = int(face.get(key) or 0)
        if not low <= value <= high:
            problems.append(f"{label}{label_text}要在 {low}–{high}%，现在是 {value}")
    angle = float(face.get("angle") or 0)
    if not ANGLE_MIN <= angle <= ANGLE_MAX:
        problems.append(f"{label}旋转要在 {ANGLE_MIN:g}–{ANGLE_MAX:g}，现在是 {angle:g}")
    blur = float(face.get("blur") or 0)
    if not 0 <= blur <= BLUR_MAX:
        problems.append(f"{label}模糊要在 0–{BLUR_MAX}，现在是 {blur:g}")
    shadow = float(face.get("shadow_distance") or 0)
    if not 0 <= shadow <= SHADOW_MAX:
        problems.append(f"{label}阴影距离要在 0–{SHADOW_MAX}，现在是 {shadow:g}")
    for key, label_text in (("opacity", "不透明度"), ("outline_opacity", "描边不透明度")):
        value = float(face.get(key) or 0)
        if not OPACITY_MIN <= value <= OPACITY_MAX:
            problems.append(f"{label}{label_text}要在 0–1，现在是 {value:g}")
    value = float(face.get("background_opacity") or 0)
    if not OPACITY_MIN <= value <= OPACITY_MAX:
        problems.append(f"{label}背景不透明度要在 0–1，现在是 {value:g}")
    value = float(face.get("shadow_opacity") or 0)
    if not OPACITY_MIN <= value <= OPACITY_MAX:
        problems.append(f"{label}阴影不透明度要在 0–1，现在是 {value:g}")
    alignment = int(face.get("alignment") or 0)
    if alignment not in ALIGNMENTS:
        problems.append(f"{label}对齐方式要是 1–9（ASS 的 numpad 编号），现在是 {alignment}")
    border = int(face.get("border_style") or 0)
    if border not in BORDER_STYLES:
        problems.append(f"{label}边框样式只能是 1（描边+阴影）或 3（实心框），现在是 {border}")
    for key, label_text in (
        ("margin_l", "左边距"),
        ("margin_r", "右边距"),
        ("margin_v", "垂直边距"),
    ):
        value = int(face.get(key) or 0)
        if not MARGIN_MIN <= value <= MARGIN_MAX:
            problems.append(f"{label}{label_text}要在 {MARGIN_MIN}–{MARGIN_MAX}，现在是 {value}")
    for key, label_text in (
        ("color", "文字色"),
        ("outline_color", "描边色"),
        ("background_color", "背景色"),
        ("shadow_color", "阴影色"),
    ):
        value = str(face.get(key) or "")
        if not _HEX_RE.match(value):
            problems.append(f"{label}{label_text}要写成 #RRGGBB，现在是 {value!r}")


def validate(
    style: dict[str, Any], *, allow_warnings: bool = True
) -> tuple[dict[str, Any], list[str]]:
    """返回 `(样式, 警告)`；硬错误抛 `VALIDATION` 并一次列全。

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

    vertical_gap = int(style.get("vertical_gap") or 0)
    if not MARGIN_MIN <= vertical_gap <= MARGIN_MAX:
        problems.append(f"主副字幕间距要在 {MARGIN_MIN}–{MARGIN_MAX}，现在是 {vertical_gap}")

    rnd = style.get("rounded") or {}
    radius = int(rnd.get("corner_radius") or 0)
    if not 0 <= radius <= RADIUS_MAX:
        problems.append(f"圆角要在 0–{RADIUS_MAX}，现在是 {radius}")
    alpha = float(rnd.get("bg_alpha") or 0)
    if not 0.0 <= alpha <= 1.0:
        problems.append(f"背景透明度要在 0–1，现在是 {alpha}")
    for key, label, low in (
        ("padding_h", "水平内边距", 0),
        ("padding_v", "垂直内边距", 0),
        ("margin_bottom", "底边距", 0),
    ):
        value = int(rnd.get(key) or 0)
        if value < low or value > 400:
            problems.append(f"{label}要在 0–400，现在是 {value}")
    for key, label in (("text_color", "圆角文字色"), ("bg_color", "圆角背景色")):
        value = str(rnd.get(key) or "")
        if not _HEX_RE.match(value):
            problems.append(f"{label}要写成 #RRGGBB，现在是 {value!r}")

    prev = style.get("preview") or {}
    for key, allowed, label in (
        ("text_length", TEXT_LENGTHS, "预览文本长度"),
        ("orientation", ORIENTATIONS, "预览画幅"),
        ("background", BACKGROUNDS, "预览背景"),
    ):
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

    只为「圆角条会不会顶边」和预览用，不追求排版精度——真要精确得加载字体。
    """
    total = 0.0
    for char in str(text or ""):
        total += size * (cjk_ratio if ord(char) > 0x2E80 else latin_ratio)
    return total


def bar_ratio(style: dict[str, Any], *, canvas_width: int = 0) -> float:
    """圆角条占画面宽度的比例。`canvas_width` 留 0 ＝ 按参考画布宽度算。"""
    width_limit = int(canvas_width or 0) or reference_canvas(style)["width"]
    rnd = style.get("rounded") or {}
    text = sample_text((style.get("preview") or {}).get("text_length") or "medium")
    width = text_width(text, int(rnd.get("size") or 60)) + 2 * int(rnd.get("padding_h") or 0)
    return width / float(width_limit or 1920)


# ---------------------------------------------------------------- ASS 转换


def to_ass_style(style: dict[str, Any], *, height: int = BASE_HEIGHT) -> dict[str, Any]:
    """兼容委托：主字幕那条 ASS 样式记录。

    真正的字段映射在 `subtitles.ass_style_records`（格式 owner）。这里只转发，
    不复制一份转换逻辑——两份映射迟早各说各话。
    """
    return subtitles.ass_style_records(style, height=height)[0]


def preview_segments(
    style: dict[str, Any], *, count: int = 3, texts: list[str] | None = None, total_ms: int = 3000
) -> list[dict[str, Any]]:
    """造几条预览段。时间不影响静态预览，但 ASS 需要起止时间。"""
    length = str((style.get("preview") or {}).get("text_length") or "medium")
    body = texts or [sample_text(length) for _ in range(max(1, count))]
    step = max(200, int(total_ms / max(1, len(body))))
    out: list[dict[str, Any]] = []
    for index, text in enumerate(body):
        out.append(
            {
                "id": f"p{index + 1}",
                "start_ms": index * step,
                "end_ms": min(total_ms, (index + 1) * step),
                "text": text,
            }
        )
    return out


def preview_ass(
    style: dict[str, Any], *, height: int = BASE_HEIGHT, texts: list[str] | None = None
) -> str:
    """预览用的完整 ASS 文本（可直接喂 ffmpeg 的 `ass` 滤镜）。"""
    segs = preview_segments(style, texts=texts)
    return subtitles.format_ass(segs, style=style, height=height)


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
        "schema_version": conf["schema_version"],
    }
