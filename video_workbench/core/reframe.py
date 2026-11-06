"""9:16 竖屏：居中裁切 / 模糊背景 / 人脸跟随（P3-06）。

三种模式都只是"从 1920×1080 里取一条 607.5×1080 的竖条"。差别在取哪一条、以及
取不到时怎么办：

- `center`：按 `crop_x`（0–1）取，默认居中。用户拖滑块就能改。
- `blur`：不裁掉画面，把原片放大铺满 1080×1920 当背景（加模糊 + 压暗 15%），
  前景按宽度缩放居中叠上去。上下留白没有黑边。
- `follow`：每 0.5 秒检测一次最大人脸，换成 `crop_x` 轨迹，再喂给 FFmpeg 的
  `crop` 表达式（`x` 支持 `t`），所以是连续移动而不是跳段。

**检测不到人脸不算失败**：这条路径必须能出片，回退居中并写原因。人脸检测要的
Haar 级联文件不内置（方案书 §0 第 3 条：模型按需下载），缺失时 `detect_focus`
返回 `detector="unavailable"`，调用方按居中处理。

规格来源：方案书 §5.7.4（模式与安全区）、§7.6（滤镜与轨迹规则）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 竖屏输出尺寸（方案书 §5.7.4）
WIDTH = 1080
HEIGHT = 1920
#: 9:16 裁切窗的宽高比（= 9 / 16）
ASPECT = 9 / 16
#: 安全区：顶部 120px、底部 260px 会被平台 UI 盖住
SAFE_TOP = 120
SAFE_BOTTOM = 260

VERTICAL_MODES = ("center", "blur", "follow")
#: 跟随模式每 0.5s 检测一次（方案书 §7.6）
DETECT_INTERVAL_MS = 500
#: 轨迹平滑：中值滤波窗口（奇数）与每帧最大位移（px）
MEDIAN_WINDOW = 5
MAX_STEP_PX = 8
#: 连续 3 秒没人脸就回中心
NO_FACE_RESET_MS = 3000
#: 模糊背景：模糊半径 / 迭代次数 / 背景亮度修正（方案书 §7.6）
BLUR_RADIUS = 20
BLUR_POWER = 2
BG_BRIGHTNESS = -0.15

#: 级联文件按需下载（不内置模型）；下载失败就退居中，不阻塞导出
CASCADE_NAME = "haarcascade_frontalface_default.xml"
CASCADE_URL = (
    "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/" + CASCADE_NAME
)
#: OpenCV 5 移除了 `CascadeClassifier`，改用 DNN 版 YuNet（232 KB ONNX，同样按需下载）。
#: LFS 直链走 media.githubusercontent.com；sha256 就是仓库里记录的 LFS oid，下载后校验。
YUNET_NAME = "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_detection_yunet/" + YUNET_NAME
)
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_MIN_BYTES = 100_000
#: YuNet 的置信度与 NMS 阈值（官方示例值，别乱调）
YUNET_SCORE = 0.6
YUNET_NMS = 0.3


# ---------------------------------------------------------------- 参数与滤镜


def check_mode(mode: str) -> str:
    if mode not in VERTICAL_MODES:
        raise ValueError(f"未知竖屏模式：{mode}")
    return mode


def clamp_x(value: object, default: float = 0.5) -> float:
    """`crop_x` 只能在 0–1 之间；非数字一律当默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(1.0, max(0.0, number))


def crop_rect(value: object) -> dict[str, int] | None:
    """把 `{w,h,x,y}` / `(w,h,x,y)` 归一成整数矩形；不合法返回 None。

    黑边裁剪的结果会一路穿到 FFmpeg 命令里，这里挡掉负数与非整数，
    免得拼出 `crop=-2:720:0:0` 这种一眼看不懂的报错。
    """
    if not value:
        return None
    if isinstance(value, dict):
        raw = [value.get(k) for k in ("w", "h", "x", "y")]
    else:
        try:
            raw = list(value)  # type: ignore[arg-type]
        except TypeError:
            return None
    if len(raw) < 4:
        return None
    try:
        w, h, x, y = (int(round(float(v or 0))) for v in raw[:4])
    except (TypeError, ValueError):
        return None
    if w <= 1 or h <= 1 or x < 0 or y < 0:
        return None
    return {"w": w, "h": h, "x": x, "y": y}


def _crop_prefix(src_crop: object) -> str:
    """黑边裁剪放最前面：先去黑边，再谈竖屏怎么取。"""
    rect = crop_rect(src_crop)
    if not rect:
        return ""
    return f"crop={rect['w']}:{rect['h']}:{rect['x']}:{rect['y']},"


CROP_RE = re.compile(r"crop=(\d+):(\d+):(-?\d+):(-?\d+)")


def detect_crop(
    source: str | Path,
    *,
    ffmpeg_bin: str,
    start_ms: int = 0,
    duration_ms: int = 3000,
    limit: int = 24,
    timeout: float = 60.0,
) -> dict[str, int] | None:
    """用 FFmpeg `cropdetect` 找黑边，返回要去掉的矩形；没黑边返回 None。

    为什么要探测：素材自带压边的很常见（4:3 内容装进 16:9 帧里、老片左右黑条）。
    直接竖屏裁切会把黑边留在画面里，模糊背景还会把黑边一起放大成一片死黑。
    探测失败不该阻塞导出——调用方拿到 None 就按原尺寸算。
    """
    import subprocess

    start = max(0, int(start_ms))
    span = max(200, int(duration_ms))
    args = [
        ffmpeg_bin, "-hide_banner", "-nostats", "-loglevel", "info",
        "-ss", f"{start / 1000:.3f}", "-i", str(source),
        "-t", f"{span / 1000:.3f}",
        "-vf", f"cropdetect=limit={int(limit)}:round=2:reset=1",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 —— 参数为列表，不经 shell
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stderr or ""
    # 取出现次数最多的那个矩形（cropdetect 每帧都给一行，分辨率变化时会跳）
    counts: dict[tuple[int, int, int, int], int] = {}
    for match in CROP_RE.finditer(text):
        w, h, x, y = (int(v) for v in match.groups())
        if w <= 1 or h <= 1 or x < 0 or y < 0:
            continue
        counts[(w, h, x, y)] = counts.get((w, h, x, y), 0) + 1
    if not counts:
        return None
    rect = max(counts.items(), key=lambda kv: (kv[1], kv[0][0] * kv[0][1]))[0]
    return {"w": rect[0], "h": rect[1], "x": rect[2], "y": rect[3]}


def same_box(size: tuple[int, int], rect: dict[str, int] | None) -> bool:
    """探测结果是否等于整帧（也就是没黑边）。"""
    if not rect:
        return True
    return rect["x"] == 0 and rect["y"] == 0 and (
        rect["w"] >= size[0] or rect["h"] >= size[1]
    )


def crop_chain(crop_x: float = 0.5, *, src_crop: object = None) -> str:
    """居中裁切：从原片取 9:16 竖条再放大到 1080×1920。

    `ih*9/16` 是竖条宽度（高度不变），水平偏移量 = 剩余宽度 × crop_x。
    传了 `src_crop` 就先削掉素材自带的黑边——不削的话竖条里会留一条黑。
    """
    x = clamp_x(crop_x)
    return (
        f"{_crop_prefix(src_crop)}"
        f"crop=ih*9/16:ih:(iw-ih*9/16)*{x:.4f}:0,scale={WIDTH}:{HEIGHT}"
    )


def follow_chain(
    focus: Sequence[dict[str, Any]],
    *,
    duration_ms: int = 0,
    src_crop: object = None,
    cuts: Sequence[int] = (),
) -> str:
    """跟随裁切：`crop` 的 x 用时间表达式，指数平滑已在轨迹里做完了。

    注意 `iw` 是"削完黑边之后"的宽度（滤镜顺序决定），所以轨迹里的 x 也是按
    削边后的可用宽度算的——两边用同一套坐标，不会整体偏一段。
    """
    expr = crop_expression(focus, duration_ms=duration_ms, cuts=cuts)
    return f"{_crop_prefix(src_crop)}crop=ih*9/16:ih:'{expr}':0,scale={WIDTH}:{HEIGHT}"


def blur_graph(subtitle: str = "", *, src_crop: object = None) -> str:
    """模糊背景的 filter_complex。

    两个输入分支：一个是放大铺满 + 模糊 + 压暗的背景，一个是按宽度缩放的前景，
    叠在一起。字幕（硬烧）挂在最后一级，这样字幕不会被模糊。
    """
    cut = _crop_prefix(src_crop)
    bg = (
        f"[0:v]{cut}scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur={BLUR_RADIUS}:{BLUR_POWER},"
        f"eq=brightness={BG_BRIGHTNESS}[bg]"
    )
    fg = f"[0:v]{cut}scale={WIDTH}:-2[fg]"
    overlay = "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    if subtitle:
        from ..infra.ffmpeg import subtitle_filter

        return (
            f"{bg};{fg};{overlay}[vblur];"
            f"[vblur]{subtitle_filter(subtitle)}[vout]"
        )
    return f"{bg};{fg};{overlay}[vout]"


def point_x(point: dict[str, Any]) -> float:
    return clamp_x(point.get("x"))


def point_t(point: dict[str, Any]) -> int:
    try:
        return max(0, int(point.get("t_ms") or 0))
    except (TypeError, ValueError):
        return 0


def _simplify(points: Sequence[dict[str, Any]], *, step_ms: int = 2_000) -> list[tuple[float, float]]:
    """把轨迹压到"每 step_ms 一个点 + x 变化够大才留"。

    为什么要压：FFmpeg 的裁剪表达式是嵌套 `if`，点数直接决定表达式长度。480 秒的
    片子按 0.5 秒采样有 960 个点，表达式会长到几十 KB，命令行都传不进 Windows。
    压到 2 秒一个、并且把 x 只差不到 0.01 的相邻段合并，通常剩几十个点。
    """
    ordered = sorted(points, key=point_t)
    if not ordered:
        return []
    kept: list[tuple[float, float]] = []
    last_t = -1
    for point in ordered:
        t_ms = point_t(point)
        x = point_x(point)
        if kept and t_ms - last_t < step_ms:
            # 同一 step 内取后到的那个：越新的坐标越接近真实位置
            kept[-1] = (t_ms / 1000.0, x)
            last_t = t_ms
            continue
        if kept and abs(kept[-1][1] - x) < 0.01:
            last_t = t_ms
            continue
        kept.append((t_ms / 1000.0, x))
        last_t = t_ms
    return kept


def crop_expression(
    focus: Sequence[dict[str, Any]],
    *,
    duration_ms: int = 0,
    cuts: Sequence[int] = (),
) -> str:
    """生成 `crop` 的 x 表达式（关于 `t` 的分段线性函数）。

    `t` 是相对切片段的时间戳（`-ss` 之后从 0 开始），所以轨迹里的 `t_ms` 也用
    切片段内的时间轴，两边才对得上。

    `cuts` 给了就在切点插一对瞬时跳变关键帧（见 `jump_pairs`）：表达式本身只会做线性
    插值，不在切点硬拆开就等于"镜头切换变成横移"。
    """
    points = _simplify(focus)
    if not points:
        return "(iw-ih*9/16)*0.5000"
    points = jump_pairs(points, cuts)
    # 兼容 duration_ms：轨迹末点之后保持不变，时长本身不需要进表达式
    _ = duration_ms

    first_t, first_x = points[0]
    # 先拼"0–1 的归一化位置"，最后整体乘一次可用宽度。乘号只能出现一次：
    # 每层都乘会让表达式变成 (iw-ih*9/16)*(...*(iw-ih*9/16)*0.7)，量纲直接错。
    value = f"{points[-1][1]:.4f}"
    for index in range(len(points) - 2, -1, -1):
        t0, x0 = points[index]
        t1, x1 = points[index + 1]
        # 分母用真实间隔，只挡 0：切点那对关键帧只隔 20ms，若按 1 秒算，
        # 本该"一帧切过去"的位置会变成一秒的横移
        room = max(0.001, (t1 - t0))
        seg = f"({x0:.4f}+({x1 - x0:.4f})*(t-{t0:.3f})/{room:.3f})"
        value = f"if(lt(t,{t1:.3f}),{seg},{value})"
    # 第一段之前保持起点值
    value = f"if(lt(t,{first_t:.3f}),{first_x:.4f},{value})"
    return f"(iw-ih*9/16)*{value}"


def vertical_filters(
    *,
    vertical_mode: str = "center",
    crop_x: float = 0.5,
    focus: Sequence[dict[str, Any]] | None = None,
    duration_ms: int = 0,
    src_crop: object = None,
    cuts: Sequence[int] = (),
) -> tuple[str, str]:
    """返回 (vf, filter_complex)。二者只会有一个非空。

    - 居中/跟随 → vf
    - 模糊背景 → filter_complex（要两路输入）
    - `src_crop` 是素材自带黑边的裁剪矩形（`detect_crop` 的结果），三种模式都要先削掉
    """
    check_mode(vertical_mode)
    if vertical_mode == "blur":
        return "", blur_graph(src_crop=src_crop)
    if vertical_mode == "follow":
        return follow_chain(focus or [], duration_ms=duration_ms, src_crop=src_crop, cuts=cuts), ""
    return crop_chain(crop_x, src_crop=src_crop), ""


# ---------------------------------------------------------------- 轨迹


def median_smooth(values: Sequence[float], window: int = MEDIAN_WINDOW) -> list[float]:
    """中值滤波：去掉检测抖动，但保留"人往左走"这种台阶。"""
    if not values:
        return []
    if window <= 1 or len(values) < 3:
        return [float(v) for v in values]
    half = max(1, window // 2)
    out: list[float] = []
    for index in range(len(values)):
        low = max(0, index - half)
        high = min(len(values), index + half + 1)
        out.append(float(statistics.median(values[low:high])))
    return out


def limit_step(values: Sequence[float], *, max_step: float) -> list[float]:
    """限速：相邻采样点之间的跳变不超过 `max_step`（单位与 values 一致）。"""
    if not values:
        return []
    out = [float(values[0])]
    for value in values[1:]:
        prev = out[-1]
        delta = float(value) - prev
        if delta > max_step:
            delta = max_step
        elif delta < -max_step:
            delta = -max_step
        out.append(prev + delta)
    return out


def _smooth_chunk(points: Sequence[dict[str, Any]], *, frame_width: int, clip_span_ms: int) -> list[dict[str, Any]]:
    if not points:
        return []
    xs = [point_x(p) for p in points]
    smoothed = median_smooth(xs)
    crop_width = max(1.0, frame_width * ASPECT)
    room = max(1.0, frame_width - crop_width)
    max_step = (MAX_STEP_PX * max(1.0, clip_span_ms / 1000.0) / 0.5) / room
    max_step = max(0.002, min(1.0, max_step))
    limited = limit_step(smoothed, max_step=max_step)
    return [{"t_ms": point_t(p), "x": round(clamp_x(v), 4)} for p, v in zip(points, limited)]


def smooth_track(
    points: Sequence[dict[str, Any]],
    *,
    frame_width: int,
    clip_span_ms: int,
    cuts: Sequence[int] = (),
) -> list[dict[str, Any]]:
    """中值滤波 + 限速，返回 `[{t_ms, x}]`。

    限速的单位是"每帧 8px"，采样间隔是 0.5 秒，所以一个采样点允许的位移 =
    8px × 该间隔折算的帧数。这里是保守估计：按 25fps 算 0.5 秒 = 12.5 帧。

    `cuts` 是镜头切换点：平滑按切点分段，**跨切点既不做中值也不限速**。否则一次换镜头
    会被"限速"拉成几秒钟的横移，观众看到的是画面缓慢地飘过去。
    """
    if not points:
        return []
    rows = sorted(points, key=point_t)
    marks = sorted({int(c) for c in cuts if 0 < int(c) < max(1, int(clip_span_ms))})
    if not marks:
        return _smooth_chunk(rows, frame_width=frame_width, clip_span_ms=clip_span_ms)
    bounds = [0] + marks + [max(1, int(clip_span_ms))]
    out: list[dict[str, Any]] = []
    for index in range(len(bounds) - 1):
        low, high = bounds[index], bounds[index + 1]
        last = index == len(bounds) - 2
        chunk = [p for p in rows if point_t(p) >= low and (point_t(p) < high or (last and point_t(p) <= high))]
        out.extend(_smooth_chunk(chunk, frame_width=frame_width, clip_span_ms=clip_span_ms))
    return out


def fill_gaps(
    points: Sequence[dict[str, Any]],
    *,
    interval_ms: int = DETECT_INTERVAL_MS,
    span_ms: int,
    reset_ms: int = NO_FACE_RESET_MS,
) -> list[dict[str, Any]]:
    """把"某一帧没人脸"补上：保持上一个位置，连续 3 秒才回中心。

    没有这一步，轨迹会在没人脸的地方冒出一堆 x=null，导出时等于随机裁。
    """
    if span_ms <= 0:
        return list(points)
    interval = max(50, int(interval_ms))
    by_time = {point_t(p): p for p in points if p.get("x") is not None}
    out: list[dict[str, Any]] = []
    last_x = 0.5
    last_seen = -reset_ms
    for t_ms in range(0, max(1, span_ms), interval):
        hit = by_time.get(t_ms)
        if hit is not None:
            last_x = point_x(hit)
            last_seen = t_ms
            out.append({"t_ms": t_ms, "x": last_x, "manual": bool(hit.get("manual"))})
            continue
        if t_ms - last_seen >= reset_ms:
            last_x = 0.5
        out.append({"t_ms": t_ms, "x": round(last_x, 4), "manual": False})
    return out


def merge_manual(
    detected: Sequence[dict[str, Any]],
    manual: Sequence[dict[str, Any]],
    *,
    interval_ms: int = DETECT_INTERVAL_MS,
    span_ms: int,
) -> list[dict[str, Any]]:
    """人工关键帧优先：命中点直接覆盖，两个人工点之间线性过渡。

    只在"命中点"上替换是不够的——人工把 x 从 0.2 改到 0.8，中间那段如果还是检测
    值，画面会在两点之间来回抖。所以这里是按区间重算。
    """
    marks = sorted(
        ({"t_ms": point_t(p), "x": point_x(p)} for p in manual if p.get("manual", True)),
        key=lambda item: item["t_ms"],
    )
    if not marks:
        return list(detected)
    interval = max(50, int(interval_ms))
    span = max(1, int(span_ms))
    out: list[dict[str, Any]] = []
    for t_ms in range(0, span, interval):
        x = None
        for index, mark in enumerate(marks):
            if mark["t_ms"] <= t_ms:
                nxt = marks[index + 1] if index + 1 < len(marks) else None
                if nxt is None or t_ms < nxt["t_ms"]:
                    if nxt is None:
                        x = mark["x"]
                    else:
                        room = max(1.0, nxt["t_ms"] - mark["t_ms"])
                        ratio = (t_ms - mark["t_ms"]) / room
                        x = mark["x"] + (nxt["x"] - mark["x"]) * ratio
                    break
        if x is None:
            # 第一个关键帧之前：沿用检测值，没有检测值就用第一个关键帧
            base = next((point_x(p) for p in detected if point_t(p) == t_ms), marks[0]["x"])
            x = base
            out.append({"t_ms": t_ms, "x": round(clamp_x(x), 4), "manual": False})
            continue
        out.append({"t_ms": t_ms, "x": round(clamp_x(x), 4), "manual": True})
    return out


# ---------------------------------------------------------------- 检测


def cascade_path(models_root: str | Path) -> Path:
    return Path(models_root) / "opencv" / CASCADE_NAME


def have_cascade(models_root: str | Path) -> bool:
    path = cascade_path(models_root)
    return path.is_file() and path.stat().st_size > 1024


def ensure_cascade(models_root: str | Path, *, proxy: str = "", log=None) -> Path:
    """按需下载级联文件（930 KB）。

    不内置的理由与 whisper 模型一致：仓库里不放二进制模型。下载失败就抛错，由调用方
    退成居中裁切——导出不能因为"没人脸检测器"而卡住。
    """
    path = cascade_path(models_root)
    if have_cascade(models_root):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log("第一次用人物跟随，正在下载人脸检测文件（约 900 KB）")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(CASCADE_URL, timeout=60) as response:
        data = response.read()
    if len(data) < 1024:
        raise RuntimeError("人脸检测文件下载不完整")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def _load_cv2():
    try:
        import cv2  # noqa: PLC0415

        return cv2
    except Exception:  # pragma: no cover - 环境缺件
        return None


def yunet_path(models_root: str | Path) -> Path:
    return Path(models_root) / "opencv" / YUNET_NAME


def have_yunet(models_root: str | Path) -> bool:
    path = yunet_path(models_root)
    return path.is_file() and path.stat().st_size > YUNET_MIN_BYTES


def ensure_yunet(models_root: str | Path, *, proxy: str = "", log=None) -> Path:
    """下载 YuNet（232 KB）。校验 sha256——下载到半截的 ONNX 会让检测静默返回空。"""
    path = yunet_path(models_root)
    if have_yunet(models_root):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log("第一次用人物跟随，正在下载人脸检测模型（约 230 KB）")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(YUNET_URL, timeout=60) as response:
        data = response.read()
    if len(data) < YUNET_MIN_BYTES:
        raise RuntimeError("人脸检测模型下载不完整")
    digest = hashlib.sha256(data).hexdigest()
    if digest != YUNET_SHA256:
        raise RuntimeError("人脸检测模型校验不通过")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


class FaceDetector:
    """人脸检测器：Haar（OpenCV 4）或 YuNet（OpenCV 5 起）。

    两边接口不一样，这里收成一个 `detect(frame_bgr) -> [[x, y, w, h], ...]`，
    调用方（基础跟随与多主体采样）就不用各写一套 if。
    """

    def __init__(self, kind: str, detect, note: str = "") -> None:
        self.kind = kind
        self._detect = detect
        self.note = note

    def detect(self, frame) -> list[list[float]]:
        try:
            return self._detect(frame) or []
        except Exception:  # pragma: no cover - 单帧失败不该让整段检测崩掉
            return []


def _haar_detector(cv2, cascade_file: Path) -> FaceDetector:
    classifier = cv2.CascadeClassifier(str(cascade_file))
    if classifier.empty():
        raise RuntimeError("人脸检测文件读不了")

    def _detect(frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        boxes = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        return [[float(x), float(y), float(w), float(h)] for x, y, w, h in boxes]

    return FaceDetector("haar", _detect)


def ascii_model_path(model_file: str | Path, *, log=None) -> Path:
    """给 OpenCV 一个纯 ASCII 路径。

    OpenCV 5 的 dnn/ONNX 导入器在 Windows 上按字节打开文件，路径里带中文（很多人的
    桌面目录就叫「视频内容总结」这种）会直接 `Can't read ONNX file` —— 文件明明在。
    好在它认相对路径，于是把模型镜像到用户临时目录里的纯 ASCII 目录再喂给它。
    """
    path = Path(model_file)
    if str(path.resolve()).isascii():
        return path
    mirror_dir = Path(tempfile.gettempdir()) / "video_workbench" / "yunet"
    if not str(mirror_dir).isascii():  # 连临时目录都带中文就退到盘根
        mirror_dir = Path(os.environ.get("SystemDrive", "C:") + os.sep) / "wb-models" / "yunet"
    mirror = mirror_dir / path.name
    try:
        if not mirror.is_file() or mirror.stat().st_size != path.stat().st_size:
            mirror_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, mirror)
        if log:
            log("检测模型路径含中文，已镜像到纯英文路径下使用")
        return mirror
    except Exception as exc:  # 镜像失败就退回原路径，报错交给上层
        if log:
            log(f"检测模型镜像失败（{exc}），仍按原路径加载")
        return path


def _yunet_detector(cv2, model_file: Path, width: int, height: int) -> FaceDetector:
    size = (max(1, int(width)), max(1, int(height)))
    model_file = ascii_model_path(model_file)
    # 刚下载完的 ONNX 偶尔会被安全软件按住几百毫秒，读一次失败不代表文件坏了
    engine = None
    last_error: Exception | None = None
    for _ in range(3):
        try:
            engine = cv2.FaceDetectorYN.create(
                str(model_file), "", size, YUNET_SCORE, YUNET_NMS, 5000
            )
            break
        except Exception as exc:  # pragma: no cover - 依赖 Windows 上的瞬时占用
            last_error = exc
            time.sleep(0.3)
    if engine is None:
        raise RuntimeError(f"YuNet 模型读不了：{last_error}")
    state = {"w": 0, "h": 0}

    def _detect(frame):
        height_now, width_now = frame.shape[:2]
        if (width_now, height_now) != (state["w"], state["h"]):
            # 分辨率变了就得重设，否则 YuNet 会按旧尺寸解码，框全歪
            engine.setInputSize((int(width_now), int(height_now)))
            state["w"], state["h"] = int(width_now), int(height_now)
        _n, faces = engine.detect(frame)
        if faces is None:
            return []
        return [[float(row[0]), float(row[1]), float(row[2]), float(row[3])] for row in faces]

    return FaceDetector("yunet", _detect, "用 YuNet 检脸（OpenCV 5 没有 Haar 级联）")


def prepare_detector(
    cv2, models_root: str | Path, *, width: int = 0, height: int = 0, proxy: str = "", log=None
) -> tuple[FaceDetector | None, str]:
    """挑一个能用的检测器。返回 `(检测器, 不可用时的人话原因)`。

    优先 Haar（4.x 上更省事、无需模型推理），没有 `CascadeClassifier` 就走 YuNet。
    两者都不行就返回 None + 原因，由调用方退居中裁切——导出不能因为没人脸检测而卡住。
    """
    if hasattr(cv2, "CascadeClassifier"):
        try:
            return _haar_detector(cv2, ensure_cascade(models_root, proxy=proxy, log=log)), ""
        except Exception as exc:
            haar_reason = f"人脸检测文件下不下来（{exc}）"
    else:
        haar_reason = "当前 OpenCV 版本没有 Haar 级联检测器"
    if hasattr(cv2, "FaceDetectorYN"):
        try:
            model = ensure_yunet(models_root, proxy=proxy, log=log)
            return _yunet_detector(cv2, model, width or 1280, height or 720), ""
        except Exception as exc:
            return None, f"{haar_reason}，YuNet 模型也用不了（{exc}），按居中裁切"
    return None, f"{haar_reason}，也没有 YuNet，按居中裁切"


def _detect_focus_single(
    source: str | Path,
    *,
    start_ms: int,
    end_ms: int,
    models_root: str | Path,
    interval_ms: int = DETECT_INTERVAL_MS,
    proxy: str = "",
    log=None,
) -> dict[str, Any]:
    """P3-06 的基础跟随：每 0.5 秒抽一帧，**只取最大那张脸**。

    路径原样保留——P6-02 的"关闭增强"就是走这里。返回 `{focus, detector, notes, frames, hits}`：

    - `detector`：`"haar"` / `"yunet"` / `"unavailable"`（缺 cv2 或模型）/ `"no_face"`（跑通了但一张脸没见着）
    - `focus`：已经补洞、平滑过的轨迹，可直接存 `focus.json`
    """
    cv2 = _load_cv2()
    if cv2 is None:
        return {
            "focus": [],
            "detector": "unavailable",
            "notes": ["没装 opencv，跳过人脸检测，按居中裁切"],
            "frames": 0,
            "hits": 0,
        }
    try:
        detector_obj, reason = prepare_detector(cv2, models_root, proxy=proxy, log=log)
    except Exception as exc:  # 下载失败不影响导出
        detector_obj, reason = None, f"人脸检测器初始化失败（{exc}）"
    if detector_obj is None:
        return {
            "focus": [],
            "detector": "unavailable",
            "notes": [reason or "没有可用的人脸检测器，按居中裁切"],
            "frames": 0,
            "hits": 0,
        }

    span = max(1, int(end_ms) - int(start_ms))
    interval = max(50, int(interval_ms))
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return {
            "focus": [],
            "detector": "unavailable",
            "notes": ["这个文件读不了，跳过人脸检测，按居中裁切"],
            "frames": 0,
            "hits": 0,
        }
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or HEIGHT
    crop_width = max(1.0, height * ASPECT)
    room = max(1.0, width - crop_width)

    raw: list[dict[str, Any]] = []
    frames = 0
    hits = 0
    try:
        for t_ms in range(0, span, interval):
            capture.set(cv2.CAP_PROP_POS_MSEC, (int(start_ms) + t_ms) / 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                raw.append({"t_ms": t_ms, "x": None})
                continue
            frames += 1
            faces = detector_obj.detect(frame)
            if not faces:
                raw.append({"t_ms": t_ms, "x": None})
                continue
            # 最大的那张：说话的人通常离镜头最近
            x, y, w, h = max(faces, key=lambda box: int(box[2]) * int(box[3]))
            center = float(x) + float(w) / 2.0
            raw.append({"t_ms": t_ms, "x": round(clamp_x((center - crop_width / 2.0) / room), 4)})
            hits += 1
    finally:
        capture.release()

    hits_rows = [p for p in raw if p.get("x") is not None]
    detector = detector_obj.kind if hits else "no_face"
    notes: list[str] = []
    if not hits:
        notes.append("这段里没检测到人脸，按居中裁切")
    filled = fill_gaps(hits_rows, interval_ms=interval, span_ms=span)
    smoothed = smooth_track(filled, frame_width=width, clip_span_ms=span)
    return {
        "focus": smoothed,
        "detector": detector,
        "notes": notes,
        "frames": frames,
        "hits": hits,
    }


def detect_focus(
    source: str | Path,
    *,
    start_ms: int,
    end_ms: int,
    models_root: str | Path,
    interval_ms: int = DETECT_INTERVAL_MS,
    proxy: str = "",
    log=None,
    enhance: bool = True,
    sampler=None,
    subject: str = "",
) -> dict[str, Any]:
    """检测跟随轨迹。`enhance=True` 走 P6-02 的多主体 + 切点路径，否则走 P3-06 老路。

    两条路的返回形状一致（`focus/detector/notes/frames/hits`），增强版多给
    `subjects/tracks/cuts/subject/thumbs`——这样上层不用为"关掉增强"写两套调用。
    """
    if not enhance:
        out = _detect_focus_single(
            source,
            start_ms=start_ms,
            end_ms=end_ms,
            models_root=models_root,
            interval_ms=interval_ms,
            proxy=proxy,
            log=log,
        )
        out["enhance"] = False
        return out
    span = max(1, int(end_ms) - int(start_ms))
    got = detect_subjects(
        source,
        start_ms=start_ms,
        end_ms=end_ms,
        models_root=models_root,
        interval_ms=interval_ms,
        proxy=proxy,
        log=log,
        sampler=sampler,
        subject=subject,
    )
    return {
        "focus": got["focus"],
        "detector": got["detector"],
        "notes": got["notes"],
        "frames": got["frames"],
        "hits": got["hits"],
        "subjects": got["subjects"],
        "tracks": got["tracks"],
        "cuts": got["cuts"],
        "subject": got["subject"],
        "thumbs": got["thumbs"],
        "width": got["width"],
        "height": got["height"],
        "span_ms": span,
        "enhance": True,
    }


# ---------------------------------------------------------------- 多主体与镜头切换
#
# P6-02 的三件事都在这一节：
#   1. 多主体：一帧里可能有好几张脸，先把"同一个人"跨时间聚成一条轨迹，再由用户指定用哪条
#   2. 镜头切换：切点处画面会整体换一次，"相邻采样点不许跳"这条规则到此必须失效，
#      否则一次切换会被拉成几秒的横移；表达式里也要在切点插一对"瞬时跳变"关键帧
#   3. 关闭增强：走 `detect_focus(enhance=False)`，也就是 P3-06 那条"只取最大脸"的老路
MAX_SUBJECTS = 4
SUBJECT_TOL_RATIO = 0.07
CUT_RATIO_THRESHOLD = 0.30
CUT_JUMP_MS = 20
SUBJECT_IDS = ("sub_a", "sub_b", "sub_c", "sub_d")
SUBJECT_LABELS = ("主体 A", "主体 B", "主体 C", "主体 D")
POSITION_LABELS = ("左边那位", "中间那位", "右边那位")
SUBJECT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "resources" / "prompts" / "clip_focus_subject.md"
)


def load_subject_prompt() -> str:
    """读"选主体"的提示词。文件丢了也要能给个能用的默认问法，不能让检测挂掉。"""
    try:
        text = SUBJECT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or (
        "下面是一段视频里几帧被框住的画面，框上有编号。判断观众应该跟着哪一个人看"
        "（通常是在说话、离镜头最近、或者画面中心的那位）。只回 JSON："
        '{"subject": <编号或 id>, "reason": "<不超过 20 字>"}。'
    )


def face_center(face: Sequence[float]) -> tuple[float, float]:
    x, y, w, h = (float(value) for value in list(face)[:4])
    return x + w / 2.0, y + h / 2.0


def face_area_ratio(face: Sequence[float], *, width: float, height: float) -> float:
    """人脸框占整幅画面的面积比。用来判断"谁是主体"——离镜头近的脸面积大。"""
    _, _, w, h = (float(value) for value in list(face)[:4])
    return (w * h) / max(1.0, float(width) * float(height))


def _x_of_center(center_x: float, *, width: float, height: float) -> float:
    crop_width = max(1.0, float(height) * ASPECT)
    room = max(1.0, float(width) - crop_width)
    return round(clamp_x((float(center_x) - crop_width / 2.0) / room), 4)


def cluster_faces(
    samples: Sequence[dict[str, Any]],
    *,
    width: float,
    height: float,
    tol_ratio: float = SUBJECT_TOL_RATIO,
    max_subjects: int = MAX_SUBJECTS,
) -> list[dict[str, Any]]:
    """把逐帧的人脸框聚成"人"。

    判据只有一条：与上一次出现的位置足够近（`tol_ratio × 画面宽`）。同一帧里的两张脸
    一定分属两个主体——所以每帧每个主体最多收一次，避免两个人挨着时被合体。

    返回按 `frames` 降序：`[{id, track:[{t_ms,x}], frames, avg_x, area, box, hits}]`。
    """
    tolerance = max(1.0, float(width) * max(0.005, float(tol_ratio)))
    clusters: list[dict[str, Any]] = []
    for sample in sorted(samples, key=lambda item: int(item.get("t_ms") or 0)):
        t_ms = int(sample.get("t_ms") or 0)
        faces = [face for face in (sample.get("faces") or []) if len(list(face)) >= 4]
        faces.sort(key=lambda face: face_center(face)[0])
        used: set[int] = set()
        for face in faces:
            center_x, _ = face_center(face)
            best = -1
            best_gap = tolerance
            for index, cluster in enumerate(clusters):
                if index in used:
                    continue
                gap = abs(float(cluster["last_x"]) - center_x)
                if gap <= best_gap:
                    best, best_gap = index, gap
            if best < 0:
                clusters.append(
                    {
                        "last_x": center_x,
                        "track": [],
                        "frames": 0,
                        "area": 0.0,
                        "box": [float(v) for v in list(face)[:4]],
                    }
                )
                best = len(clusters) - 1
            cluster = clusters[best]
            used.add(best)
            cluster["last_x"] = center_x
            cluster["frames"] += 1
            cluster["area"] += face_area_ratio(face, width=width, height=height)
            cluster["track"].append({"t_ms": t_ms, "x": _x_of_center(center_x, width=width, height=height)})
            # 记最大那张脸当"这个人的框"：给界面画缩略图、给 vision 标号都用它
            if face_area_ratio(face, width=width, height=height) >= face_area_ratio(
                cluster["box"], width=width, height=height
            ):
                cluster["box"] = [float(v) for v in list(face)[:4]]

    clusters.sort(key=lambda item: (-int(item["frames"]), -float(item["area"])))
    out: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters[: max(1, int(max_subjects))]):
        track = sorted(cluster["track"], key=lambda point: int(point["t_ms"]))
        if not track:
            continue
        out.append(
            {
                "id": SUBJECT_IDS[index] if index < len(SUBJECT_IDS) else f"sub_{index + 1}",
                "track": track,
                "frames": int(cluster["frames"]),
                "hits": len(track),
                "avg_x": round(sum(point["x"] for point in track) / len(track), 4),
                "area": round(float(cluster["area"]) / max(1, int(cluster["frames"])), 6),
                "box": [round(v, 2) for v in cluster["box"]],
            }
        )
    return label_subjects(out)


def label_subjects(subjects: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按平均位置给可读标签：两个人叫"左边那位/右边那位"，比"主体 B"好认。"""
    rows = [dict(item) for item in subjects]
    order = sorted(range(len(rows)), key=lambda index: float(rows[index].get("avg_x") or 0.5))
    for rank, index in enumerate(order):
        if len(rows) <= len(POSITION_LABELS):
            label = POSITION_LABELS[rank]
        else:
            label = SUBJECT_LABELS[min(rank, len(SUBJECT_LABELS) - 1)]
        rows[index]["label"] = label
        rows[index]["order"] = rank
    return rows


def score_subject(subject: dict[str, Any], *, span_ms: int, width: float) -> int:
    """给主体打分（0–100）：出镜时长、稳定性、脸的大小、是否偏在画面边上。"""
    track = list(subject.get("track") or [])
    if not track:
        return 0
    span = max(1, int(span_ms))
    interval = max(50, int(DETECT_INTERVAL_MS))
    expected = max(1, span // interval)
    presence = min(1.0, len(track) / expected)
    xs = [float(point.get("x") or 0.5) for point in track]
    jitter = sum(abs(xs[i + 1] - xs[i]) for i in range(len(xs) - 1)) / max(1, len(xs) - 1)
    stability = max(0.0, 1.0 - min(1.0, jitter * 4.0))
    area = min(1.0, float(subject.get("area") or 0.0) * 12.0)
    center = max(0.0, 1.0 - min(1.0, abs(sum(xs) / len(xs) - 0.5) * 2.0))
    raw = 0.40 * presence + 0.25 * stability + 0.20 * area + 0.15 * center
    return int(round(max(0.0, min(1.0, raw)) * 100))


def rank_subjects(subjects: Sequence[dict[str, Any]], *, span_ms: int, width: float) -> list[dict[str, Any]]:
    rows = [dict(item) for item in subjects]
    for row in rows:
        row["score"] = score_subject(row, span_ms=span_ms, width=width)
    rows.sort(key=lambda item: -int(item.get("score") or 0))
    return rows


def pick_subject(subjects: Sequence[dict[str, Any]]) -> str:
    """默认主体 = 分数最高的那条；没有就用空串（居中裁）。"""
    best = ""
    best_score = -1
    for subject in subjects:
        score = int(subject.get("score") or 0)
        if score > best_score:
            best, best_score = str(subject.get("id") or ""), score
    return best


def subject_by_id(subjects: Sequence[dict[str, Any]], subject_id: str) -> dict[str, Any] | None:
    for subject in subjects:
        if str(subject.get("id") or "") == str(subject_id or ""):
            return subject
    return None


def cut_times(samples: Sequence[dict[str, Any]], *, threshold: float = CUT_RATIO_THRESHOLD) -> list[int]:
    """镜头切换点：相邻采样帧的缩略图平均差超过阈值就记一个切点。

    采样间隔 0.5 秒，所以切点精度就是 0.5 秒——够用：这里的目标不是"帧级精确切点"，
    而是"别在切点两侧插值"。阈值 0.30 是拿真片子试出来的：同机位内的动作幅度到不了，
    整幅换画面则远超。
    """
    out: list[int] = []
    for sample in sorted(samples, key=lambda item: int(item.get("t_ms") or 0)):
        try:
            score = float(sample.get("cut") or 0.0)
        except (TypeError, ValueError):
            continue
        if score >= float(threshold):
            out.append(int(sample.get("t_ms") or 0))
    return out


def jump_pairs(
    points: Sequence[tuple[float, float]],
    cuts: Sequence[int],
    *,
    gap_ms: int = CUT_JUMP_MS,
) -> list[tuple[float, float]]:
    """在切点插一对"瞬时跳变"关键帧：切点前保持旧值，切点后立刻用新值。

    没有这一步，表达式会在切点前后两点之间做线性插值，画面表现为一次缓慢横移——正是
    "切换镜头跳变"要避免的东西。两点间隔 20ms，肉眼看就是一帧切过去。
    """
    if not points or not cuts:
        return list(points)
    out: dict[float, float] = {round(float(t), 3): float(x) for t, x in points}
    for cut in sorted({int(value) for value in cuts}):
        t = float(cut) / 1000.0
        before = [(pt, px) for pt, px in points if pt < t]
        after = [(pt, px) for pt, px in points if pt >= t]
        if not before or not after:
            continue
        gap = max(1, int(gap_ms)) / 1000.0
        out[round(max(0.0, t - gap), 3)] = float(before[-1][1])
        out[round(t, 3)] = float(after[0][1])
    return sorted(out.items())


# ---------------------------------------------------------------- 抽取与检测


def _haar_sampler(
    source: str | Path,
    *,
    start_ms: int,
    span_ms: int,
    interval_ms: int,
    models_root: str | Path,
    proxy: str = "",
    log=None,
    thumb_limit: int = 3,
) -> dict[str, Any]:
    """真实采样：每 `interval_ms` 抽一帧，记下人脸框、切点分数、以及几张缩略图。

    缩略图只留"这一帧有两张以上脸"的，最多 `thumb_limit` 张——多主体判定交给 vision
    时要的是"多人同框"的那几帧，给单人镜头没有意义。
    """
    cv2 = _load_cv2()
    if cv2 is None:
        return {"samples": [], "detector": "unavailable", "notes": ["没装 opencv，跳过人脸检测，按居中裁切"],
                "frames": 0, "width": 0, "height": 0, "thumbs": []}
    try:
        detector_obj, reason = prepare_detector(cv2, models_root, proxy=proxy, log=log)
    except Exception as exc:  # 下载失败不影响导出
        detector_obj, reason = None, f"人脸检测器初始化失败（{exc}）"
    if detector_obj is None:
        return {"samples": [], "detector": "unavailable",
                "notes": [reason or "没有可用的人脸检测器，按居中裁切"],
                "frames": 0, "width": 0, "height": 0, "thumbs": []}
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return {"samples": [], "detector": "unavailable", "notes": ["这个文件读不了，跳过人脸检测，按居中裁切"],
                "frames": 0, "width": 0, "height": 0, "thumbs": []}
    width = float(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or float(HEIGHT)
    interval = max(50, int(interval_ms))
    samples: list[dict[str, Any]] = []
    thumbs: list[dict[str, Any]] = []
    frames = 0
    previous = None
    try:
        for t_ms in range(0, max(1, int(span_ms)), interval):
            capture.set(cv2.CAP_PROP_POS_MSEC, (int(start_ms) + t_ms) / 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            frames += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (32, 18))
            flat = small.flatten().astype("float32") / 255.0
            cut = 0.0
            if previous is not None and len(previous) == len(flat):
                cut = float(abs(flat - previous).mean())
            previous = flat
            boxes = detector_obj.detect(frame)
            faces = [[float(box[0]), float(box[1]), float(box[2]), float(box[3])] for box in boxes]
            faces.sort(key=lambda box: -box[2] * box[3])
            samples.append({"t_ms": t_ms, "faces": faces, "cut": round(cut, 4)})
            if len(faces) >= 2 and len(thumbs) < max(1, int(thumb_limit)):
                okj, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if okj:
                    thumbs.append({"t_ms": t_ms, "jpeg": buffer.tobytes(), "faces": faces})
    finally:
        capture.release()
    return {"samples": samples, "detector": detector_obj.kind if samples else "no_face", "notes": [],
            "frames": frames, "width": width, "height": height, "thumbs": thumbs}


def detect_subjects(
    source: str | Path,
    *,
    start_ms: int,
    end_ms: int,
    models_root: str | Path,
    interval_ms: int = DETECT_INTERVAL_MS,
    proxy: str = "",
    log=None,
    max_subjects: int = MAX_SUBJECTS,
    sampler=None,
    subject: str = "",
) -> dict[str, Any]:
    """增强版检测：多主体聚类 + 切点 + 各主体轨迹。

    `sampler(source, start_ms=…, span_ms=…, interval_ms=…, models_root=…)` 可注入：
    真实路径用 `_haar_sampler`，测试/验收用假采样器构造"两个主体 + 一次切换"，
    因为 Haar 没法凭空造出双人画面。
    """
    span = max(1, int(end_ms) - int(start_ms))
    interval = max(50, int(interval_ms))
    if sampler is None:
        got = _haar_sampler(source, start_ms=int(start_ms), span_ms=span, interval_ms=interval,
                            models_root=models_root, proxy=proxy, log=log)
    else:
        got = sampler(source, start_ms=int(start_ms), span_ms=span, interval_ms=interval,
                      models_root=models_root)
    samples = list(got.get("samples") or [])
    notes = list(got.get("notes") or [])
    width = float(got.get("width") or WIDTH)
    height = float(got.get("height") or HEIGHT)
    detector = str(got.get("detector") or ("haar" if samples else "no_face"))
    cuts = cut_times(samples)
    subjects = rank_subjects(
        cluster_faces(samples, width=width, height=height, max_subjects=max_subjects),
        span_ms=span,
        width=width,
    )
    hits = sum(1 for sample in samples if sample.get("faces"))
    if not subjects:
        notes.append("这段里没检测到人脸，按居中裁切")
    elif len(subjects) == 1:
        notes.append("只找到一个主体，跟随它")
    else:
        notes.append(f"找到 {len(subjects)} 个主体，可切换跟随对象")
    if cuts:
        notes.append(f"检测到 {len(cuts)} 处镜头切换，切换处不做平滑")
    chosen = str(subject or "") or pick_subject(subjects)
    selected = subject_by_id(subjects, chosen) or (subjects[0] if subjects else None)
    focus: list[dict[str, Any]] = []
    if selected:
        filled = fill_gaps(selected["track"], interval_ms=interval, span_ms=span)
        focus = smooth_track(filled, frame_width=int(width), clip_span_ms=span, cuts=cuts)
    # `unavailable` 要原样传上去（缺 cv2/级联）；只有"跑通了但一张脸没见着"才叫 no_face
    if detector == "haar" and not subjects:
        detector = "no_face"
    return {
        "focus": focus,
        "subjects": subjects,
        "tracks": {str(item["id"]): item["track"] for item in subjects},
        "subject": str(selected["id"]) if selected else "",
        "cuts": cuts,
        "detector": detector,
        "notes": notes,
        "frames": int(got.get("frames") or 0),
        "hits": hits,
        "thumbs": list(got.get("thumbs") or []),
        "width": width,
        "height": height,
    }


def track_of(subjects: Sequence[dict[str, Any]], subject_id: str) -> list[dict[str, Any]]:
    """取某个主体的原始轨迹（未补洞、未平滑）——切主体时据此重建，不必重跑检测。"""
    found = subject_by_id(subjects, subject_id)
    return list(found.get("track") or []) if found else []


def rebuild_focus(
    subjects: Sequence[dict[str, Any]],
    *,
    subject_id: str,
    span_ms: int,
    frame_width: int,
    cuts: Sequence[int] = (),
    interval_ms: int = DETECT_INTERVAL_MS,
) -> list[dict[str, Any]]:
    track = track_of(subjects, subject_id)
    if not track:
        return []
    filled = fill_gaps(track, interval_ms=interval_ms, span_ms=max(1, int(span_ms)))
    return smooth_track(filled, frame_width=max(1, int(frame_width)), clip_span_ms=max(1, int(span_ms)),
                        cuts=cuts)


# ---------------------------------------------------------------- vision 选主体


def annotate_frame(jpeg: bytes, boxes: Sequence[Sequence[float]], labels: Sequence[str]) -> bytes:
    """在缩略图上画编号框，好让 vision 能"指着图说第几个"。

    没装 Pillow 就原样返回——宁可少一层标注，也不能因为画图失败把整段精析打断。
    """
    if not boxes:
        return jpeg
    try:
        import io  # noqa: PLC0415

        from PIL import Image, ImageDraw  # noqa: PLC0415
    except Exception:  # pragma: no cover - 环境缺件
        return jpeg
    try:
        image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:  # pragma: no cover - 坏图
        return jpeg
    draw = ImageDraw.Draw(image)
    for index, box in enumerate(boxes):
        x, y, w, h = (float(value) for value in list(box)[:4])
        color = (216, 242, 75) if index == 0 else (255, 107, 53)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=4)
        text = labels[index] if index < len(labels) else str(index + 1)
        draw.text((x + 6, max(0.0, y - 16)), text, fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def subject_messages(
    items: Sequence[dict[str, Any]],
    *,
    prompt: str,
    subjects: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """一张图 + 一段名单：名单里写清每个编号对应谁（`sub_a`），让模型只回编号。"""
    roster = "\n".join(
        f"{index + 1}. {item.get('label') or item.get('id')}（id={item.get('id')}）"
        for index, item in enumerate(subjects)
    )
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"{prompt}\n\n画面里被框住的人：\n{roster}"}
    ]
    for item in items:
        content.append({"type": "image_url", "image_url": {"url": item["data_url"]}})
    return [{"role": "user", "content": content}]


def _json_object(text: str) -> dict[str, Any]:
    """宽松取 JSON 对象：剥掉代码块围栏、只取第一个 `{` 到最后一个 `}`。

    选主体是增强项，模型多说一句废话就整段失败不可接受——认不出来就当没选。
    """
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_subject_reply(text: str, subjects: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """解析 vision 的回复：`{"subject": "sub_b", "reason": "..."}`。

    容忍"编号"（1/2/3）与 id 两种写法；认不出来就返回空，由调用方退回几何打分。
    """
    data = _json_object(text)
    if not isinstance(data, dict):
        return {}
    raw = data.get("subject") if "subject" in data else data.get("id")
    reason = str(data.get("reason") or "").strip()
    picked = ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        index = int(raw) - 1
        if 0 <= index < len(subjects):
            picked = str(subjects[index].get("id") or "")
    elif isinstance(raw, str):
        token = raw.strip()
        digits = "".join(ch for ch in token if ch.isdigit())
        found = subject_by_id(subjects, token)
        if found is not None:
            picked = str(found.get("id") or "")
        elif digits and token.isdigit():
            index = int(digits) - 1
            if 0 <= index < len(subjects):
                picked = str(subjects[index].get("id") or "")
    if not picked:
        return {}
    return {"subject": picked, "reason": reason}


def vision_pick_subject(
    client,
    *,
    shots: Sequence[dict[str, Any]],
    subjects: Sequence[dict[str, Any]],
    model: str = "",
    prompt: str = "",
    box_of=None,
) -> dict[str, Any]:
    """让 vision 看几张"多人同框"的帧，指出该跟谁。

    `shots` 里每项 `{jpeg, faces}`；此函数只回 `{subject, reason, model}`，失败一律返回
    空 dict —— 精析是增强项，不该因为它挂掉就把整次检测判失败。
    """
    if client is None or not shots or len(subjects) < 2:
        return {}
    items: list[dict[str, Any]] = []
    for shot in shots[:3]:
        boxes = [face for face in (shot.get("faces") or [])][: len(subjects)]
        labels = [str(index + 1) for index in range(len(boxes))]
        drawn = annotate_frame(shot.get("jpeg") or b"", boxes, labels)
        import base64  # noqa: PLC0415

        items.append({"data_url": "data:image/jpeg;base64," + base64.b64encode(drawn).decode("ascii")})
    if not items:
        return {}
    messages = subject_messages(items, prompt=prompt, subjects=subjects)
    try:
        reply = client.chat(messages, temperature=0.1, max_tokens=300, model=model, json_mode=True)
    except Exception:  # pragma: no cover - 网络/额度问题都当"没选中"
        return {}
    text = reply if isinstance(reply, str) else getattr(reply, "text", "")
    parsed = parse_subject_reply(text, subjects)
    if not parsed:
        return {}
    parsed["model"] = model
    return parsed


# ---------------------------------------------------------------- 存取


def load_focus(path: str | Path) -> list[dict[str, Any]]:
    """读 `focus.json`。文件坏了当没有——不能让一个坏 json 卡住导出。"""
    target = Path(path)
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("focus") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "t_ms": point_t(item),
                "x": round(point_x(item), 4),
                "manual": bool(item.get("manual")),
            }
        )
    return sorted(out, key=lambda item: item["t_ms"])


def save_focus(path: str | Path, focus: Iterable[dict[str, Any]], *, meta: dict[str, Any] | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"t_ms": point_t(item), "x": round(point_x(item), 4), "manual": bool(item.get("manual"))}
        for item in focus
    ]
    rows.sort(key=lambda item: item["t_ms"])
    payload = {"focus": rows}
    if meta:
        payload.update(meta)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def manual_only(focus: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in focus if item.get("manual")]


def safe_area() -> dict[str, int]:
    """安全区尺寸（前端画遮罩用）。"""
    return {"top": SAFE_TOP, "bottom": SAFE_BOTTOM, "width": WIDTH, "height": HEIGHT}
