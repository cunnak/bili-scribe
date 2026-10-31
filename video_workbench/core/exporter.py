"""切片导出：文件名、FFmpeg 参数、切点偏移估算（P3-05）。

三件事刻意放在这里，而不是塞进 service：

1. **文件名**：`{项目}_{候选}_{start}-{end}_{aspect}.mp4`，逐段清洗。文件名是用户
   唯一会看到并右键复制的东西，中文、空格、`?`、`/` 都得先洗掉。
2. **参数**：精确重编码与 `-c copy` 是两条不同的命令，写成一个纯函数才测得住。
3. **切点偏移**：`-c copy` 只能切在关键帧上，实际起点会**早于**请求的起点。偏移量
   靠"导出文件实际时长 vs 期望时长"反推，不用猜、也不用扫全片关键帧。

规格来源：方案书 §5.7.3。
"""

from __future__ import annotations

from ..infra.files import sanitize_name

#: 导出模式：精确重编码（默认）/ 关键帧快速切
EXPORT_MODES = ("precise", "fast")
#: 字幕处理：不烧 / 硬烧 / 软字幕
SUBTITLE_MODES = ("none", "hard", "soft")
#: 画幅：内部只用这一个写法（文件名、数据库、滤镜分支都按它比）
ASPECTS = ("16x9", "9x16")
#: 但用户嘴里的画幅不是这个写法：方案书、界面文案、B站/抖音的说法全是 ``9:16``。
#: 命令行手打 ``--aspect 9:16`` 被判「画幅只能是 16x9 / 9x16」是我们的写法问题，
#: 不是用户的错，所以在入口处收一次，往下只走规范值。
ASPECT_ALIASES = {
    "16x9": "16x9",
    "16:9": "16x9",
    "16／9": "16x9",
    "horizontal": "16x9",
    "landscape": "16x9",
    "横屏": "16x9",
    "9x16": "9x16",
    "9:16": "9x16",
    "9／16": "9x16",
    "vertical": "9x16",
    "portrait": "9x16",
    "竖屏": "9x16",
}


def normalize_aspect(aspect: str) -> str:
    """把用户写法收成 ``ASPECTS`` 里的规范值；认不出来抛 ValueError（由服务层转 VALIDATION）。"""
    key = str(aspect or "").strip().lower().replace("：", ":").replace("／", "/")
    if not key:
        raise ValueError("画幅不能是空的")
    hit = ASPECT_ALIASES.get(key) or ASPECT_ALIASES.get(key.replace("/", ":"))
    if hit is None:
        raise ValueError(f"未知画幅：{aspect}（写 16x9 / 9x16，或 16:9 / 9:16 都行）")
    return hit

CRF = 18
PRESET = "medium"
AUDIO_KBPS = 192
SAMPLE_RATE = 48000
#: 文件名里各段最多留多少字（Windows 全路径 260 的老账，宁短不长）
NAME_LIMIT = 48


def clean_title(text: object, fallback: str = "未命名") -> str:
    """把标题洗成可进文件名的一小段。"""
    raw = str(text or "").strip()
    if not raw:
        return fallback
    # 先按 NAME_LIMIT 截，再走 sanitize_name：反过来的话截断点可能落在被清洗的字符上
    return sanitize_name(raw[: NAME_LIMIT * 2], max_len=NAME_LIMIT) or fallback


def _stamp(ms: int) -> str:
    """毫秒 → 文件名友好的时间码 `00m12.400`（冒号在 Windows 文件名里不合法）。"""
    total = max(0, int(ms))
    minutes, rest = divmod(total, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes:02d}m{seconds:02d}.{millis:03d}"


def mode_suffix(mode: str, subtitle_mode: str = "none") -> str:
    """模式后缀。

    别看这只是一段小尾巴：三种模式（精确/快速/硬烧）切同一段时输出**必须**不同名，
    否则后一次把前一次覆盖掉，队列里三行都写「完成」而磁盘上只剩一个文件——用户会
    以为导出了三条片。
    """
    if mode not in EXPORT_MODES:
        raise ValueError(f"未知导出模式：{mode}")
    if subtitle_mode not in SUBTITLE_MODES:
        raise ValueError(f"未知字幕处理：{subtitle_mode}")
    if subtitle_mode == "none":
        return mode
    return f"{mode}-{subtitle_mode}"


def clip_filename(
    *,
    project_title: object,
    candidate_title: object,
    start_ms: int,
    end_ms: int,
    aspect: str = "16x9",
    suffix: str = "",
) -> str:
    """`{项目}_{候选}_{start}-{end}_{aspect}[_{模式}].mp4`。"""
    parts = [
        clean_title(project_title, "项目"),
        clean_title(candidate_title, "切片"),
        f"{_stamp(start_ms)}-{_stamp(end_ms)}",
        aspect if aspect in ASPECTS else "16x9",
    ]
    if suffix:
        parts.append(clean_title(suffix, "out"))
    return "_".join(parts) + ".mp4"


def _replace_maps(
    args: list[str], video_label: str, audio_label: str, subtitle_mode: str
) -> list[str]:
    """把已经排好的 `-map` 换成滤镜图里的标签。

    拼接顺序上 `-map` 先写（视频、音频、可选软字幕），音频滤镜图是后算的，
    所以只能回头改前两个值。数到第三个就停——第三个是软字幕轨，不能动。
    """
    out = list(args)
    seen = 0
    for index, token in enumerate(out):
        if token != "-map":
            continue
        if seen == 0:
            out[index + 1] = video_label
        elif seen == 1:
            out[index + 1] = audio_label
        seen += 1
        if seen >= 2:
            break
    return out


def build_args(
    ffmpeg_bin: str,
    *,
    src: str,
    dest: str,
    start_ms: int,
    end_ms: int,
    mode: str = "precise",
    subtitle: str = "",
    subtitle_mode: str = "none",
    crf: int = CRF,
    preset: str = PRESET,
    aspect: str = "16x9",
    vertical_mode: str = "center",
    crop_x: float = 0.5,
    focus: list[dict] | None = None,
    duration_ms: int = 0,
    src_crop: dict[str, int] | None = None,
    focus_cuts: list[int] | None = None,
    bgm: str = "",
    bgm_volume: float = 0.25,
    bgm_duck: bool = True,
    fade_ms: int = 0,
    source_audio: bool = True,
) -> list[str]:
    """拼导出命令。

    `-ss` 放在 `-i` 之前（快速定位），精确模式下配合 `-t` 重编码，切点仍然准；
    这是 FFmpeg 官方推荐的做法，比 `-ss` 放后面快一个量级而精度在 1 帧内。

    画幅与字幕要一起决定用 `-vf` 还是 `-filter_complex`：

    - 原比例 / 竖屏居中 / 竖屏跟随 → 单链 `-vf`（硬烧字幕接在这条链末尾）
    - 竖屏模糊背景 → 两路输入（背景 + 前景）叠一次，必须 `-filter_complex`，
      硬烧字幕挂在叠好之后那一级，否则字幕也被模糊
    - `src_crop` 是素材自带的黑边（`reframe.detect_crop` 的结果），三种竖屏模式都先削掉
    - P6-03 起：混 BGM / 闪避 / 淡入淡出也走 `-filter_complex`（音频侧链必须用图），
      此时视频这一侧若本来只有 `-vf`，也要折进图里拼成 `[0:v]…[vout]`

    映射写在分支里，不要在拼完 args 之后回头删 `-map`——软字幕那条路径也有 `-map`，
    删错就是把字幕轨丢了。
    """
    if mode not in EXPORT_MODES:
        raise ValueError(f"未知导出模式：{mode}")
    if subtitle_mode not in SUBTITLE_MODES:
        raise ValueError(f"未知字幕处理：{subtitle_mode}")
    if aspect not in ASPECTS:
        aspect = normalize_aspect(aspect)
    if subtitle_mode != "none" and not subtitle:
        raise ValueError("选了字幕处理却没给字幕文件")

    start = max(0, int(start_ms))
    span = max(1, int(end_ms) - start)

    # ---- 画面这一侧先算好，后面才知道视频要映射到哪条流
    graph = ""
    vf = ""
    if aspect == "9x16":
        from . import reframe

        vf, graph = reframe.vertical_filters(
            vertical_mode=vertical_mode,
            crop_x=crop_x,
            focus=focus or [],
            duration_ms=duration_ms or span,
            src_crop=src_crop,
            cuts=list(focus_cuts or ()),
        )
    if subtitle_mode == "hard":
        from ..infra.ffmpeg import subtitle_filter

        if graph:
            # 模糊背景：字幕要挂在叠好之后
            from . import reframe as _reframe

            graph = _reframe.blur_graph(subtitle, src_crop=src_crop)
        else:
            burned = subtitle_filter(subtitle)
            vf = f"{vf},{burned}" if vf else burned
    video_map = "[vout]" if graph else "0:v:0"

    args = [
        ffmpeg_bin,
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start / 1000:.3f}",
        "-i", src,
    ]
    next_input = 1
    if subtitle_mode == "soft":
        # 软字幕要第二条输入，BGM 的序号得往后挪
        next_input = 2
    bgm_index = next_input
    if bgm:
        # BGM 一般比切片长或短，`-stream_loop -1` 让它循环，长度由 `-t` 收口
        args += ["-stream_loop", "-1", "-i", bgm]
    if subtitle_mode == "soft":
        # `-ss` 只作用于第一个输入。字幕轨不 `-itsoffset` 回去，成片里的软字幕
        # 就按源时间走：前 start 秒一句没有，之后全错位。
        args += ["-itsoffset", f"{-start / 1000:.3f}", "-i", subtitle]
        args += ["-map", video_map, "-map", "0:a?", "-map", "1:0"]
    else:
        args += ["-map", video_map, "-map", "0:a?"]
    args += ["-t", f"{span / 1000:.3f}"]

    # ---- 音频这一侧（P6-03）：闪避/混音/淡入淡出都要滤镜图
    from . import audio_mix

    audio_chains, audio_label = audio_mix.build_audio_graph(
        span_ms=span,
        bgm=bool(bgm),
        volume=bgm_volume,
        duck=bgm_duck,
        fade_ms=fade_ms,
        bgm_index=bgm_index,
        source_audio=source_audio,
    )
    vfade = audio_mix.fade_filters(span, fade_ms, audio=False)

    if audio_chains or vfade:
        # 把视频侧也折进图：图里既有 [v?] 也有 [a?]，映射才能都指向标签
        chains: list[str] = []
        video_label = ""
        if graph:
            chains.append(graph)  # 模糊背景那条图自带 [vout]
            video_label = "[vout]"
        elif vf:
            chains.append(f"[0:v]{vf}[vpre]")
            video_label = "[vpre]"
        if vfade:
            source = video_label or "[0:v]"
            chains.append(f"{source}{','.join(vfade)}[vfinal]")
            video_label = "[vfinal]"
        chains.extend(audio_chains)
        if video_label or audio_label:
            args = _replace_maps(
                args,
                video_label or "0:v:0",
                audio_label or "0:a?",
                subtitle_mode,
            )
        if chains:
            args += ["-filter_complex", ";".join(chains)]
    elif graph:
        args += ["-filter_complex", graph]
    elif vf:
        args += ["-vf", vf]

    if mode == "precise":
        args += [
            "-c:v", "libx264", "-crf", str(int(crf)), "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", str(SAMPLE_RATE),
        ]
        if subtitle_mode == "soft":
            args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
    else:
        # 快速模式：不重编码。软字幕仍要把字幕编成 mov_text，所以这条路径只对
        # 视频/音频是 copy；带滤镜时 copy 会失败，所以竖屏/硬字幕强制走重编码。
        if graph or vf or audio_chains or vfade:
            args += [
                "-c:v", "libx264", "-crf", str(int(crf)), "-preset", preset,
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", str(SAMPLE_RATE),
            ]
            if subtitle_mode == "soft":
                args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
        else:
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            if subtitle_mode == "soft":
                args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]

    args += ["-movflags", "+faststart", dest]
    return args


def duration_delta_ms(expected_ms: int, actual_ms: int) -> int:
    """成片时长 − 期望时长（毫秒）。

    快速模式（`-c copy`）只会切在关键帧上：起点可能前移 → 成片**更长**；
    片尾也落在关键帧上 → 也可能更长或更短。所以这里保留符号，别把"更长"当成
    没偏移——第一版就是这么错的：多带 1.4 秒还报"正好落在关键帧上"。
    """
    return int(actual_ms) - int(expected_ms)


def offset_note(mode: str, delta_ms: int) -> str:
    """导出结果上要多写一句人话，别让用户以为快速模式切得准。"""
    if mode != "fast":
        return ""
    if delta_ms == 0:
        return "快速模式：这次切点正好落在关键帧上，时长与要求一致"
    if delta_ms > 0:
        return (
            f"快速模式：成片比要求长 {delta_ms} ms（切点吸到关键帧，开头会多带一点）"
        )
    return f"快速模式：成片比要求短 {-delta_ms} ms（片尾吸到关键帧）"
