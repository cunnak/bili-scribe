# -*- coding: utf-8 -*-
"""合并：预检与规格统一。

两件事在这里定下来，因为后面所有命令都依赖它们：

1. **能不能直接 copy**——同编码/同分辨率/同帧率/同像素格式/同音频参数才行。
   判错不会有异常，只会得到花屏或者音画不同步，所以判据要列全、理由要能显示。
2. **统一到什么规格**——精确重编码得有一个目标。默认取第一个文件（用户放的顺序
   就是他心里的顺序），也可以在界面上改。

本模块只做「探测 + 判断 + 命名 + 估算」，不跑 ffmpeg 拼接：拼接命令在 P4-02/P4-03
接进来，免得一次写太多、出事分不清是判据错还是命令错。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from ..app.errors import WBError
from ..infra.files import sanitize_name

#: 判断「能不能直接 copy」要比的字段。少比一个就可能花屏，所以集中列在这里。
COPY_FIELDS: tuple[tuple[str, str], ...] = (
    ("vcodec", "视频编码"),
    ("width", "宽度"),
    ("height", "高度"),
    ("fps", "帧率"),
    ("pix_fmt", "像素格式"),
    ("acodec", "音频编码"),
    ("sample_rate", "采样率"),
    ("channels", "声道数"),
)

#: 输出规格可选值。`0` 表示「跟随第一个文件」。
FPS_CHOICES = (0, 24, 25, 30, 50, 60)


def _round_fps(value: float) -> float:
    """帧率比较用三位小数：29.97 与 29.970 是一回事，别判成不一致。"""
    return round(float(value or 0.0), 3)


def probe_item(ffmpeg: Any, path: str | Path, *, origin: str = "") -> dict[str, Any]:
    """探测一个待合并文件。**探测失败不抛**——一条坏文件不该让整张预检表消失。"""
    target = Path(str(path))
    item: dict[str, Any] = {
        "path": str(target),
        "name": target.name,
        "origin": origin,
        "exists": target.is_file(),
        "ok": False,
        "duration_ms": 0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "vcodec": "",
        "pix_fmt": "",
        "acodec": "",
        "sample_rate": 0,
        "channels": 0,
        "bytes": 0,
        "error": "",
    }
    if not item["exists"]:
        # 文件夹不是"文件没了"：用系统选择框选错（或手填了目录）时，
        # 用户需要看到的是"这是文件夹"，而不是一个查不到的视频文件。
        item["error"] = "这是一个文件夹，不是视频文件" if target.is_dir() else "文件不在了"
        return item
    try:
        info = ffmpeg.probe(target)
    except WBError as exc:
        item["error"] = exc.message
        return item
    except Exception as exc:  # 探测不该把任务带崩
        item["error"] = f"探测失败：{exc}"
        return item
    item.update(
        {
            "ok": True,
            "duration_ms": int(info.duration_ms),
            "width": int(info.width),
            "height": int(info.height),
            "fps": _round_fps(info.fps),
            "vcodec": str(info.vcodec),
            "pix_fmt": str(getattr(info, "pix_fmt", "") or ""),
            "acodec": str(info.acodec),
            "sample_rate": int(getattr(info, "sample_rate", 0) or 0),
            "channels": int(getattr(info, "channels", 0) or 0),
            "bytes": int(info.bytes),
            "has_video": bool(info.has_video),
            "has_audio": bool(info.has_audio),
        }
    )
    if not info.has_video:
        item["ok"] = False
        item["error"] = "这个文件没有视频流"
    return item


def _fmt_ms(value: int) -> str:
    value = max(0, int(value))
    total_s = value // 1000
    return f"{total_s // 60}:{total_s % 60:02d}"


def _fmt_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _value_text(field: str, value: Any) -> str:
    if field == "fps":
        return f"{float(value or 0):g} fps"
    if field in ("width", "height"):
        return str(int(value or 0))
    if field == "sample_rate":
        return f"{int(value or 0)} Hz"
    if field == "channels":
        return f"{int(value or 0)} 声道"
    return str(value or "—")


def differences(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列出不一致的字段。**只列真的不一致的**，一致的不出现在表里。"""
    usable = [i for i in items if i.get("ok")]
    if len(usable) < 2:
        return []
    out: list[dict[str, Any]] = []
    for field, label in COPY_FIELDS:
        values = [i.get(field) for i in usable]
        first = values[0]
        if all(v == first for v in values):
            continue
        out.append(
            {
                "field": field,
                "label": label,
                "values": [
                    {"name": i.get("name") or "", "value": _value_text(field, i.get(field))}
                    for i in usable
                ],
            }
        )
    return out


def preflight(
    ffmpeg: Any,
    paths: Iterable[str],
    *,
    width: int = 0,
    height: int = 0,
    fps: float = 0.0,
) -> dict[str, Any]:
    """合并预检：探测全部文件 + 判能否 copy + 给统一规格建议。

    返回的字段都是界面直接要用的形状，不让前端再算一遍（算两遍必然两边不一致）。
    """
    wanted = [str(p) for p in paths if str(p or "").strip()]
    items = [probe_item(ffmpeg, p) for p in wanted]
    usable = [i for i in items if i.get("ok")]
    missing = [i for i in items if not i["exists"]]
    broken = [i for i in items if i["exists"] and not i.get("ok")]
    diffs = differences(items)

    total_ms = sum(int(i.get("duration_ms") or 0) for i in usable)
    total_bytes = sum(int(i.get("bytes") or 0) for i in usable)

    # 目标规格：用户指定优先；没指定就跟第一个文件（用户放的顺序就是他心里的顺序）
    first = usable[0] if usable else {}
    target_w = int(width or first.get("width") or 1920)
    target_h = int(height or first.get("height") or 1080)
    target_fps = _round_fps(fps or first.get("fps") or 30)
    target_audio = int(first.get("sample_rate") or 48000)
    target_channels = int(first.get("channels") or 2)

    can_quick = bool(len(usable) >= 2 and not diffs and not missing and not broken)
    warnings: list[str] = []
    if len(usable) < 2:
        warnings.append("至少要两个片段才能合并")
    if missing:
        warnings.append(f"{len(missing)} 个文件不在了，先移出列表")
    if broken:
        warnings.append(f"{len(broken)} 个文件读不出视频流，先移出列表")
    if diffs and len(usable) >= 2:
        warnings.append("规格不一致，只能精确重编码")

    return {
        "items": items,
        "count": len(usable),
        "missing": [{"path": i["path"], "name": i["name"]} for i in missing],
        "broken": [{"path": i["path"], "name": i["name"], "error": i["error"]} for i in broken],
        "total_ms": total_ms,
        "total_text": _fmt_ms(total_ms),
        "total_bytes": total_bytes,
        "total_bytes_text": _fmt_bytes(total_bytes),
        "consistent": not diffs,
        "can_quick": can_quick,
        "differences": diffs,
        "target": {
            "width": target_w,
            "height": target_h,
            "fps": target_fps,
            "sample_rate": target_audio,
            "channels": target_channels,
        },
        "warnings": warnings,
        "checked_at": int(time.time()),
    }


def estimate(result: dict[str, Any], mode: str) -> dict[str, Any]:
    """粗估成片体积与耗时。**不精确是设计**——用来判断「要不要等」，不是承诺。

    copy：体积≈各段之和（封装开销忽略）；耗时按 0.02 倍时长算（只搬字节）。
    重编码：按目标像素×帧率折算相对 1080p30 的系数，再取 4 Mbps 基准。
    """
    total_ms = int(result.get("total_ms") or 0)
    seconds = total_ms / 1000.0
    if mode == "quick":
        size = int(result.get("total_bytes") or 0)
        cost = seconds * 0.02
    else:
        target = result.get("target") or {}
        pixels = max(1, int(target.get("width") or 1920) * int(target.get("height") or 1080))
        fps = float(target.get("fps") or 30)
        factor = (pixels / (1920 * 1080)) * (fps / 30.0)
        size = int(seconds * 4_000_000 / 8 * max(0.2, min(3.0, factor)))
        # x264 medium 在这台机器上大约 1.6 倍实时（1080p），按系数折算
        cost = seconds * 1.6 * max(0.3, min(4.0, factor))
    return {
        "mode": mode,
        "size_bytes": size,
        "size_text": _fmt_bytes(size),
        "seconds": round(cost, 1),
        "seconds_text": f"约 {max(1, int(round(cost)))} 秒",
    }


def output_name(items: list[dict[str, Any]], *, suffix: str = "") -> str:
    """成片文件名：`合并_3段_20261030-165502.mp4`。

    用时间戳而不是序号：同一批素材反复合并时不会互相覆盖，也不会出现 `(2)` 这种
    谁也看不懂的尾巴。
    """
    count = len([i for i in items if i.get("ok")]) or len(items)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    head = sanitize_name(f"合并_{count}段_{stamp}", max_len=60)
    return f"{head}{suffix}.mp4"


def default_output_dir(*roots: str) -> Path:
    """输出目录：项目根下的 `merge/`，没有项目就放第一个可写根。"""
    for root in roots:
        if root:
            return Path(root) / "merge"
    raise WBError("VALIDATION", "没有可用的输出目录")


# ---------------------------------------------------------------- 快速拼接


def escape_concat_path(path: str | Path) -> str:
    """concat demuxer 的 `file` 行转义。

    规则来自 ffmpeg 文档：单引号包起来的路径里，`'` 要写成 `'\''`；Windows 的反斜杠
    在 demuxer 里会被当转义符，所以统一转成正斜杠（ffmpeg 在 Windows 上认正斜杠）。
    """
    text = str(path).replace("\\", "/")
    # `'` 要写成「单引号 + 反斜杠 + 两个单引号」。直接写字面量太容易掉一层
    # 反斜杠（双引号串里的 \' 就是单引号，结果成了三个引号），所以用 chr(92) 拼。
    quote_fix = "'" + chr(92) + "''"
    text = text.replace("'", quote_fix)
    return f"file '{text}'"


def concat_list_text(paths: Iterable[str | Path]) -> str:
    """`concat.txt`：一行一个文件，末尾换行（ffmpeg 末行没换行也能读，但留着好排查）。"""
    lines = [escape_concat_path(p) for p in paths]
    return "\n".join(lines) + "\n"


def quick_command(ffmpeg_bin: str, list_path: str | Path, dest: str | Path) -> list[str]:
    """快速拼接：`-f concat -safe 0 -c copy`。

    **不重编码，所以只能用于规格一致的文件**（判据在 `differences()`）。`-safe 0`
    是必须的：列表里的路径多为绝对路径，默认的安全模式会拒绝。
    """
    return [
        str(ffmpeg_bin),
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def difference_text(result: dict[str, Any]) -> str:
    """把差异表压成一句话，给 `CONFLICT` 的 detail 用。"""
    rows = result.get("differences") or []
    if not rows:
        return ""
    parts: list[str] = []
    for row in rows:
        values = "、".join(f"{v.get('name')}={v.get('value')}" for v in row.get("values") or [])
        parts.append(f"{row.get('label')}（{values}）")
    return "规格不一致：" + "；".join(parts)


def block_reason(result: dict[str, Any], mode: str = "quick") -> str:
    """为什么不能走这条模式。空串表示可以。

    `quick` 的三条门槛分开写：条数不够、文件缺失、规格不一致——三种原因的处置办法
    完全不同（加文件 / 找回文件 / 换精确模式），不能合并成一句「不能合并」。

    **缺失/坏文件对两种模式都拦。** 精确重编码能兜住规格不一致，兜不住"文件不在"；
    以前这里只拦 quick，于是 precise 会静默只合并可用的子集，用户少了几段却不知道。
    条数不够仍只在这里拦 quick，precise 的条数由 service 单独判——这样保留
    `block_reason({"count": 0}, "precise") == ""` 的既有语义。
    """
    # `missing` / `broken` 是行列表（界面要显示是哪几个），不是计数
    missing = result.get("missing") or []
    if missing:
        return f"有 {len(missing)} 个文件不在了，先移出列表"
    broken = result.get("broken") or []
    if broken:
        return f"有 {len(broken)} 个文件读不出视频流，先移出列表"
    if mode != "quick":
        return ""
    count = int(result.get("count") or 0)
    if count < 2:
        return "至少要两个可用片段才能拼接"
    if result.get("differences"):
        return difference_text(result)
    return ""


# ---------------------------------------------------------------- 精确重编码
#
# 快速拼接（`-c copy`）只能用于规格一致的文件。规格不一致时必须重编码：每路先
# `scale` + `pad` 到同一画布、`fps` 统一帧率、`format` 统一像素格式，音频
# `aresample` 统一采样率与声道，最后用 `concat` 滤镜拼起来。这一步慢且有损，
# 但**一定能播**——这是"不同规格也能合并"的唯一诚实做法。


PRECISE_CRF = 18
PRECISE_PRESET = "medium"
PRECISE_AUDIO_KBPS = 192


def _even(value: int, floor: int = 2) -> int:
    """yuv420p 要求宽高是偶数，否则 ffmpeg 直接报 "width not divisible by 2"。"""
    return max(floor, int(value) // 2 * 2)


def precise_plan(
    items: list[dict[str, Any]],
    *,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    sample_rate: int = 48000,
    channels: int = 2,
) -> dict[str, Any]:
    """精确合并的滤镜图与参数。返回的 dict 直接喂给 `precise_command()`。

    没有音轨的片段不能直接 `[i:a]`（那是"流不存在"，不是静音），得用 `anullsrc`
    造一段等长静音补进去——所以这里会把需要补的时长单独列出来，由命令拼装侧
    追加成 lavfi 输入。缺音频的文件在合并里很常见（录屏、无麦摄像头）。
    """
    if len(items) < 2:
        raise WBError("VALIDATION", "至少要两个片段才能合并")
    w = _even(width)
    h = _even(height)
    rate = max(1.0, float(fps or 30.0))
    sr = max(8000, int(sample_rate or 48000))
    layout = "stereo" if int(channels or 2) >= 2 else "mono"

    parts: list[str] = []
    silence: list[float] = []
    base = len(items)  # lavfi 静音输入排在真实输入之后
    for index, item in enumerate(items):
        parts.append(
            f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"fps={rate:g},format=yuv420p[v{index}]"
        )
        if item.get("has_audio"):
            parts.append(
                f"[{index}:a]aresample={sr},"
                f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={layout}"
                f"[a{index}]"
            )
        else:
            seconds = max(0.05, round(int(item.get("duration_ms") or 0) / 1000.0, 3))
            silent_index = base + len(silence)
            silence.append(seconds)
            parts.append(
                f"[{silent_index}:a]atrim=0:{seconds},asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={layout}"
                f"[a{index}]"
            )

    links = "".join(f"[v{i}][a{i}]" for i in range(len(items)))
    parts.append(f"{links}concat=n={len(items)}:v=1:a=1[outv][outa]")

    return {
        "filter": ";".join(parts),
        "silence": silence,
        "width": w,
        "height": h,
        "fps": rate,
        "sample_rate": sr,
        "channels": 2 if layout == "stereo" else 1,
        "layout": layout,
        "count": len(items),
    }


def precise_command(
    ffmpeg_bin: str,
    *,
    inputs: Iterable[str | Path],
    dest: str | Path,
    plan: dict[str, Any],
    crf: int = PRECISE_CRF,
    preset: str = PRECISE_PRESET,
    audio_kbps: int = PRECISE_AUDIO_KBPS,
) -> list[str]:
    """精确合并命令。

    `-progress pipe:1` 让 ffmpeg 把进度写到 stdout（一行一个 `key=value`），
    我们据此算「已完成几个文件 + 当前文件百分比」。不加的时候只有 stderr 的一堆
    时间戳，解析起来更容易出错。
    """
    args: list[str] = [str(ffmpeg_bin), "-hide_banner", "-nostdin", "-y"]
    for path in inputs:
        args += ["-i", str(path)]
    for seconds in plan.get("silence") or []:
        args += [
            "-f",
            "lavfi",
            "-t",
            f"{float(seconds):g}",
            "-i",
            f"anullsrc=r={plan.get('sample_rate')}:cl={plan.get('layout')}",
        ]
    args += [
        "-filter_complex",
        str(plan.get("filter") or ""),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-crf",
        str(int(crf)),
        "-preset",
        str(preset),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{int(audio_kbps)}k",
        "-ar",
        str(int(plan.get("sample_rate") or 48000)),
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(dest),
    ]
    return args


def parse_progress_line(line: str) -> dict[str, str]:
    """`out_time_us=123456` / `progress=continue` 这类行拆成 dict。

    ffmpeg 同时会给 `out_time_ms`，但它其实是**微秒**（历史遗留命名），所以两个键
    都按微秒处理，别按字面意思当毫秒用。
    """
    text = (line or "").strip()
    if not text or "=" not in text:
        return {}
    key, _, value = text.partition("=")
    return {key.strip(): value.strip()}


def merge_progress(
    out_time_us: int,
    segments_ms: Iterable[int],
    *,
    expected_ms: int = 0,
) -> dict[str, Any]:
    """把「已输出多久」翻成「已完成几个片段 + 当前片段百分比」。

    进度按片段边界算而不是只给一个总百分比：用户在界面上看到的是"6 段里的第 3 段，
    这一段 42%"，比"总进度 38%"更能判断还要等多久。
    """
    spans = [int(v or 0) for v in segments_ms]
    total = int(expected_ms or 0) or sum(spans)
    done_ms = max(0, int(out_time_us)) // 1000
    prefix = 0
    for index, span in enumerate(spans):
        if done_ms < prefix + span or span <= 0:
            inside = 0.0 if span <= 0 else min(1.0, max(0.0, (done_ms - prefix) / span))
            fraction = (prefix + inside * span) / total if total else 0.0
            return {
                "done": index,
                "index": index + 1,
                "total": len(spans),
                "file_fraction": inside,
                "fraction": round(min(1.0, max(0.0, fraction)), 4),
            }
        prefix += span
    count = len(spans)
    return {
        "done": count,
        "index": count,
        "total": count,
        "file_fraction": 1.0,
        "fraction": 1.0 if total else 0.0,
    }


def progress_text(progress: dict[str, Any]) -> str:
    """进度文案：`第 3/6 段 42%`。"""
    index = int(progress.get("index") or 0)
    total = int(progress.get("total") or 0)
    percent = int(round(float(progress.get("file_fraction") or 0) * 100))
    if not total:
        return "重编码中"
    return f"第 {index}/{total} 段 {percent}%"


def fallback_note() -> str:
    """快速拼接失败、自动切精确时写进进度区的那句话（方案书 §5.8.4）。"""
    return "快速拼接失败，已切精确重编码"
