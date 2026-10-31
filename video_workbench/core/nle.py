"""剪辑工程导出：剪映草稿 / 达芬奇 OTIO / 降级包（P6-04）。

三个通道输出的是**给别的软件用的工程文件**，不是成片，所以这里只做三件事：

1. **剪映草稿**：`draft_content.json` + `draft_meta_info.json` 两个文件放进一个文件夹，
   剪映打开草稿箱就能看到。骨架来自 `resources/export_templates/jianying/`——
   那不是凭空写的结构，是从本机剪映（5.9.0 / draft version 360000）真实存下来的草稿里
   剥掉素材内容后留下的壳。字段名、`check_flag`、`clip`、`hdr_settings` 这些看着没用的
   值都是剪映自己写的，改了可能就打不开，**不要"顺手精简"**。
2. **达芬奇 OTIO**：`.otio` 是 OpenTimelineIO 的 JSON，达芬奇"文件 → 导入 → 时间线"认它。
   只写一条视频轨 + Clip，`media_reference` 指向源文件的 `file://` URL（**不拷贝素材**）。
3. **降级包**：一个文件夹，装成片/字幕/封面/文案 + 一份 `导入说明.txt`。这是"剪映版本不认
   草稿、又不想装 OTIO 插件"时的兜底：人按说明手动拖进去就行。

**字幕样式不跟随**（方案书 Phase 6 明写）：我们的 ASS 样式（字号/描边/圆角条）是
`subtitles.format_ass` 的产物，剪映和达芬奇各有一套自己的文字样式系统，没有无损映射。
所以草稿里的字幕只用中性样式（白字、默认字号），并在结果与说明里明确写一句
"字幕样式不跟随，进软件里再调"。不做"看起来很像但细节全错"的伪映射。

规格来源：方案书 §附录 B 三通道、任务清单 P6-04。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..app.errors import WBError
from ..infra.files import ensure_free_space, sanitize_name, unique_path

# ---------------------------------------------------------------- 常量

#: 交付通道
CHANNELS = ("jianying", "otio", "bundle")
CHANNEL_LABELS = {
    "jianying": "剪映草稿",
    "otio": "达芬奇 OTIO",
    "bundle": "降级包",
}

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "resources" / "export_templates"
JIANYING_TEMPLATE_DIR = TEMPLATE_DIR / "jianying"
DRAFT_CONTENT_TEMPLATE = "draft_content.json"
DRAFT_META_TEMPLATE = "draft_meta_info.json"

#: 剪映草稿里的中性文字样式（size 是剪映自己的字号刻度，5 ≈ 默认）
TEXT_SIZE = 5
#: OTIO 默认帧率（达芬奇会按工程帧率重算，写 30 是它能读的常见值）
OTIO_RATE = 30

BUNDLE_NOTE = "导入说明.txt"
BUNDLE_COPY = "文案.txt"
BUNDLE_SUBTITLE = "字幕.srt"

#: 剪映草稿箱候选路径（不同版本/改名后位置会变，全部试一遍）
_JIANYING_ROOTS = (
    "JianyingPro/User Data/Projects/com.lveditor.draft",
    "JianyingPro/User Data/Projects/com.lveditor.draft.local",
    "CapCut/User Data/Projects/com.lveditor.draft",
    "CapCut/User Data/Projects/com.lveditor.draft.local",
)


# ---------------------------------------------------------------- 小工具


def us(ms: int | float) -> int:
    """毫秒 → 微秒。剪映草稿里的时间**全是微秒**，混了毫秒会缩成 0.001 倍。"""
    return int(round(float(ms) * 1000))


def new_id() -> str:
    """剪映的 id 是 32 位小写 hex，不带连字符。"""
    return uuid.uuid4().hex


def draft_uid() -> str:
    """`draft_meta_info.json` 的 `draft_id` 是大写带连字符的 GUID。"""
    return str(uuid.uuid4()).upper()


def check_channel(channel: str) -> str:
    value = str(channel or "").strip().lower()
    if value not in CHANNELS:
        raise WBError("VALIDATION", "交付通道只能是 jianying / otio / bundle")
    return value


def load_template(name: str, *, template_dir: Path | None = None) -> dict[str, Any]:
    """读骨架 JSON。缺文件是安装问题，直接说清楚少了哪个。"""
    base = Path(template_dir or JIANYING_TEMPLATE_DIR)
    path = base / name
    if not path.is_file():
        raise WBError("DEPENDENCY_MISSING", "缺少草稿模板文件", str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WBError("PROCESS_FAILED", "草稿模板读不出来", f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WBError("PROCESS_FAILED", "草稿模板格式不对", str(path))
    return data


def find_jianying_root(home: str | Path | None = None) -> Path | None:
    """找剪映的草稿箱目录；找不到返回 None（不是错误，装没装都可能）。"""
    base = Path(home) if home else Path(os.environ.get("LOCALAPPDATA") or Path.home())
    for rel in _JIANYING_ROOTS:
        path = base / Path(rel)
        if path.is_dir():
            return path
    return None


#: 达芬奇安装/用户目录的候选（只要有一个存在就当装了）
_RESOLVE_ROOTS = (
    "Blackmagic Design/DaVinci Resolve",
    "DaVinci Resolve Welcome",
)


def find_resolve_root(home: str | Path | None = None) -> Path | None:
    """找达芬奇的痕迹（用户目录或安装目录）。只用来在界面上给一句"检测到了"。

    显式传 `home` 时只看这个前缀（测试要能造"没装"的场景）；
    不传才去翻 Program Files——那是本机真实探测。
    """
    explicit = home is not None
    local = Path(home) if explicit else Path(os.environ.get("LOCALAPPDATA") or Path.home())
    for rel in _RESOLVE_ROOTS:
        path = local / Path(rel)
        if path.exists():
            return path
    if explicit:
        return None
    for base in (
        Path(os.environ.get("PROGRAMFILES") or "C:/Program Files"),
        Path(os.environ.get("PROGRAMFILES(X86)") or "C:/Program Files (x86)"),
    ):
        path = base / Path("Blackmagic Design")
        if path.is_dir():
            return path
    return None


def detect_apps(home: str | Path | None = None) -> dict[str, Any]:
    """本机装了哪些能接工程文件的软件（只读探测，不启动它们）。"""
    jianying = find_jianying_root(home)
    resolve = find_resolve_root(home)
    return {
        "jianying": {"found": bool(jianying), "root": str(jianying or "")},
        "resolve": {"found": bool(resolve), "root": str(resolve or "")},
    }


def safe_draft_name(text: object, fallback: str = "VideoWorkbench") -> str:
    """剪映用文件夹名当草稿名，所以名字得能当文件夹名。"""
    raw = str(text or "").strip()
    if not raw:
        # 空名字不能交给 sanitize_name：它会回 "untitled"，那就不该出现在草稿箱里
        return fallback
    return sanitize_name(raw, max_len=60) or fallback


# ---------------------------------------------------------------- 剪映草稿


def _text_content(text: str) -> str:
    """剪映的文字素材把富文本塞进 `content` 这个**字符串**里（是 JSON 的 JSON）。"""
    body = {
        "text": text,
        "styles": [
            {
                "range": [0, len(text)],
                "size": TEXT_SIZE,
                "bold": False,
                "italic": False,
                "underline": False,
                "strokes": [],
                "fill": {
                    "alpha": 1.0,
                    "content": {
                        "render_type": "solid",
                        "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]},
                    },
                },
            }
        ],
    }
    return json.dumps(body, ensure_ascii=False)


def _video_material(
    *, path: str, name: str, width: int, height: int, duration_ms: int
) -> dict[str, Any]:
    material_id = new_id()
    return {
        "audio_fade": None,
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "crop": {
            "upper_left_x": 0.0,
            "upper_left_y": 0.0,
            "upper_right_x": 1.0,
            "upper_right_y": 0.0,
            "lower_left_x": 0.0,
            "lower_left_y": 1.0,
            "lower_right_x": 1.0,
            "lower_right_y": 1.0,
        },
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "duration": us(duration_ms),
        "height": int(height),
        "id": material_id,
        "local_material_id": "",
        "material_id": material_id,
        "material_name": Path(path).name,
        "media_path": "",
        "path": str(path),
        "type": "video",
        "width": int(width),
    }


def _video_segment(
    *, material_id: str, source_start_ms: int, span_ms: int, extra_refs: list[str]
) -> dict[str, Any]:
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": new_id(),
        "material_id": material_id,
        "target_timerange": {"start": 0, "duration": us(span_ms)},
        "common_keyframes": [],
        "keyframe_refs": [],
        "source_timerange": {"start": us(source_start_ms), "duration": us(span_ms)},
        "speed": 1.0,
        "volume": 1.0,
        "extra_material_refs": extra_refs,
        "is_tone_modify": False,
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "render_index": 0,
    }


def _text_segment(
    *, material_id: str, start_ms: int, span_ms: int, track_render_index: int
) -> dict[str, Any]:
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": track_render_index,
        "visible": True,
        "id": new_id(),
        "material_id": material_id,
        "target_timerange": {"start": us(start_ms), "duration": us(span_ms)},
        "common_keyframes": [],
        "keyframe_refs": [],
        "source_timerange": {"start": 0, "duration": us(span_ms)},
        "speed": 1.0,
        "volume": 1.0,
        "extra_material_refs": [],
        "is_tone_modify": False,
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "render_index": 0,
    }


def jianying_document(
    template: dict[str, Any],
    *,
    name: str,
    video_path: str,
    source_duration_ms: int,
    width: int,
    height: int,
    start_ms: int,
    end_ms: int,
    fps: float = 30.0,
    subtitles: Iterable[dict[str, Any]] | None = None,
    bgm_path: str = "",
) -> dict[str, Any]:
    """按骨架拼出一份剪映草稿（`draft_content.json` 的内容）。

    只做这一段切片：素材指向源文件、`source_timerange` 卡在 `start_ms`，
    所以打开就是已经剪好的那一段，不用再自己掐头去尾。
    """
    span = max(1, int(end_ms) - int(start_ms))
    doc = copy.deepcopy(template)
    doc["id"] = draft_uid()
    doc["name"] = str(name)
    doc["fps"] = float(fps or 30.0)
    doc["duration"] = us(span)
    doc["canvas_config"] = {
        "width": int(width or 1920),
        "height": int(height or 1080),
        "ratio": "original",
    }

    materials = doc.setdefault("materials", {})
    speed_id = new_id()
    materials["speeds"] = [
        {"curve_speed": None, "id": speed_id, "mode": 0, "speed": 1.0, "type": "speed"}
    ]
    video = _video_material(
        path=str(video_path),
        name=Path(str(video_path)).name,
        width=width,
        height=height,
        duration_ms=max(int(source_duration_ms), span),
    )
    materials["videos"] = [video]
    doc["tracks"] = [
        {
            "attribute": 0,
            "flag": 0,
            "id": new_id(),
            "is_default_name": True,
            "name": "",
            "segments": [
                _video_segment(
                    material_id=video["id"],
                    source_start_ms=int(start_ms),
                    span_ms=span,
                    extra_refs=[speed_id],
                )
            ],
            "type": "video",
        }
    ]

    if bgm_path:
        audio_id = new_id()
        materials["audios"] = [
            {
                "app_id": 0,
                "category_id": "",
                "category_name": "local",
                "check_flag": 1,
                "duration": us(span),
                "id": audio_id,
                "local_material_id": "",
                "music_id": "",
                "name": Path(str(bgm_path)).name,
                "path": str(bgm_path),
                "request_id": "",
                "resource_id": "",
                "source_platform": 0,
                "team_id": "",
                "type": "extract_music",
                "wave_points": [],
            }
        ]
        doc["tracks"].append(
            {
                "attribute": 0,
                "flag": 0,
                "id": new_id(),
                "is_default_name": True,
                "name": "",
                "segments": [
                    _text_segment(
                        material_id=audio_id,
                        start_ms=0,
                        span_ms=span,
                        track_render_index=0,
                    )
                ],
                "type": "audio",
            }
        )

    rows = [s for s in (subtitles or []) if str(s.get("text") or "").strip()]
    if rows:
        texts: list[dict[str, Any]] = []
        segments: list[dict[str, Any]] = []
        for row in rows:
            body = str(row.get("text") or "").strip()
            start = max(0, int(row.get("start_ms") or 0) - int(start_ms))
            end = min(span, int(row.get("end_ms") or 0) - int(start_ms))
            if end - start < 100:
                continue
            material_id = new_id()
            texts.append(
                {
                    "id": material_id,
                    "content": _text_content(body),
                    "typesetting": 0,
                    "alignment": 1,
                    "letter_spacing": 0.0,
                    "line_spacing": 0.02,
                    "line_feed": 1,
                    "line_max_width": 0.82,
                    "force_apply_line_max_width": False,
                    "check_flag": 7,
                    "type": "subtitle",
                    "global_alpha": 1.0,
                }
            )
            segments.append(
                _text_segment(
                    material_id=material_id,
                    start_ms=start,
                    span_ms=end - start,
                    track_render_index=1,
                )
            )
        if segments:
            materials["texts"] = texts
            doc["tracks"].append(
                {
                    "attribute": 0,
                    "flag": 0,
                    "id": new_id(),
                    "is_default_name": True,
                    "name": "",
                    "segments": segments,
                    "type": "text",
                }
            )
    return doc


def jianying_meta(
    template: dict[str, Any],
    *,
    name: str,
    folder: Path,
    created_ms: int = 0,
) -> dict[str, Any]:
    """`draft_meta_info.json`：剪映靠 `draft_fold_path` 记住草稿在哪。"""
    doc = copy.deepcopy(template)
    text = str(folder).replace("\\", "/")
    doc["draft_name"] = str(name)
    doc["draft_fold_path"] = text
    doc["draft_root_path"] = str(Path(text).parent).replace("\\", "/")
    doc["draft_id"] = draft_uid()
    doc["draft_cover"] = "draft_cover.jpg"
    stamp = int(created_ms or 0) // 1000 if created_ms else 0
    if stamp:
        doc["tm_draft_create"] = stamp
        doc["tm_draft_modified"] = stamp
    return doc


def write_jianying(
    folder: Path,
    content: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """落两个文件。剪映认的是**文件夹名**，文件夹名和 `draft_name` 得一致。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    content_path = folder / DRAFT_CONTENT_TEMPLATE
    meta_path = folder / DRAFT_META_TEMPLATE
    content_path.write_text(
        json.dumps(content, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {
        "dir": str(folder),
        "files": [str(content_path), str(meta_path)],
        "bytes": content_path.stat().st_size + meta_path.stat().st_size,
    }


# ---------------------------------------------------------------- 达芬奇 OTIO


def _rational(value: float, rate: float = OTIO_RATE) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate), "value": float(value)}


def _time_range(start_ms: int, span_ms: int, rate: float = OTIO_RATE) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _rational(int(start_ms) / 1000.0 * rate, rate),
        "duration": _rational(max(1, int(span_ms)) / 1000.0 * rate, rate),
    }


def otio_document(
    *,
    name: str,
    video_path: str,
    start_ms: int,
    end_ms: int,
    width: int = 1920,
    height: int = 1080,
    rate: float = OTIO_RATE,
    source_duration_ms: int = 0,
) -> dict[str, Any]:
    """OTIO 时间线：一条视频轨，一个 Clip，素材用 `file://` 引用（不拷贝）。"""
    span = max(1, int(end_ms) - int(start_ms))
    target = Path(str(video_path))
    url = target.as_uri() if target.is_absolute() else Path(os.path.abspath(target)).as_uri()
    reference: dict[str, Any] = {
        "OTIO_SCHEMA": "ExternalReference.1",
        "metadata": {},
        "name": target.name,
        "target_url": url,
        "available_range": _time_range(0, max(int(source_duration_ms or 0), int(end_ms)), rate),
    }
    clip = {
        "OTIO_SCHEMA": "Clip.1",
        "metadata": {
            "video_workbench": {
                "source_start_ms": int(start_ms),
                "source_end_ms": int(end_ms),
            }
        },
        "name": str(name),
        "source_range": _time_range(int(start_ms), span, rate),
        "media_reference": reference,
        "effects": [],
        "markers": [],
        "enabled": True,
    }
    track = {
        "OTIO_SCHEMA": "Track.1",
        "metadata": {
            "video_workbench": {"width": int(width), "height": int(height)}
        },
        "name": "V1",
        "source_range": None,
        "effects": [],
        "markers": [],
        "enabled": True,
        "children": [clip],
        "kind": "Video",
    }
    stack = {
        "OTIO_SCHEMA": "Stack.1",
        "metadata": {},
        "name": "tracks",
        "source_range": None,
        "effects": [],
        "markers": [],
        "enabled": True,
        "children": [track],
    }
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {
            "video_workbench": {
                "width": int(width),
                "height": int(height),
                "note": "字幕样式不跟随：字幕在成片里烧好或单独导入，OTIO 只带画面",
            }
        },
        "name": str(name),
        "global_start_time": None,
        "tracks": stack,
    }


def write_otio(path: Path, doc: dict[str, Any]) -> dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {"path": str(path), "bytes": path.stat().st_size}


# ---------------------------------------------------------------- 降级包


def bundle_notes(
    *,
    project_title: str,
    clip_title: str,
    start_ms: int,
    end_ms: int,
    files: Iterable[str] = (),
    subtitle_style_note: bool = True,
) -> str:
    """降级包里的说明书。写清"这是什么、怎么拖进去、哪些东西不会跟过来"。"""
    names = [Path(str(f)).name for f in files]
    lines = [
        f"VideoWorkbench 降级包 · {project_title} / {clip_title}",
        "",
        f"片段区间：{_human_ms(start_ms)} → {_human_ms(end_ms)}",
        "",
        "## 这个包里有什么",
    ]
    for name in names:
        lines.append(f"- {name}")
    if not names:
        lines.append("- （空）")
    lines += [
        "",
        "## 剪映怎么用",
        "1. 打开剪映专业版，新建一个草稿；",
        "2. 把本文件夹里的视频拖进时间线，需要的话把字幕文件拖到文字轨；",
        "3. 字幕若是 .srt，用「文本 → 智能字幕 → 导入字幕」；若是 .ass，剪映不认这个格式，"
        "请用同名的 .srt。",
        "",
        "## 达芬奇怎么用",
        "1. 打开达芬奇，新建工程（帧率建议与素材一致）；",
        "2. 把视频拖进媒体池，再拖到时间线；",
        "3. 字幕：把 .srt 拖到媒体池，然后拖到时间线对应的字幕轨上。",
        "",
        "## 会被丢掉的东西",
    ]
    if subtitle_style_note:
        lines += [
            "- **字幕样式不跟随**：这里的字号、描边、圆角条是 VideoWorkbench 的 ASS 参数，"
            "剪映/达芬奇各有一套文字样式，没有无损映射，进软件后需要自己调一次。",
        ]
    lines += [
        "- 人声闪避（BGM 自动压低）是导出时用滤镜做的，工程文件里没有这条逻辑；"
        "要可编辑，得在软件里手动打关键帧或重新混音。",
        "- 竖屏取景（居中/模糊/跟随）同上：降级包里是已经算好的画面，不是可调的参数。",
        "",
        "## 为什么会有这个包",
        "当剪映版本不认草稿、或不想装 OTIO 插件时，用它兜底：素材和说明都是现成的，"
        "手动拖进去花不了一分钟。",
        "",
    ]
    return "\n".join(lines)


def _human_ms(ms: int) -> str:
    total = max(0, int(ms))
    minutes, rest = divmod(total, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis // 100:01d}"


def write_bundle(
    folder: Path,
    *,
    files: Iterable[tuple[str, str]],
    notes: str,
    copy_text: str = "",
) -> dict[str, Any]:
    """把 `(源路径, 目标文件名)` 逐条拷进包里，再写说明书与文案。

    拷贝前先算磁盘：一个降级包可能几百 MB，写到一半没空间比直接报错更糟。
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    wanted = [(str(src), str(name)) for src, name in files if str(src)]

    # 音频/视频可能是几百 MB：按实际大小 + 200MB 余量算，不够就提前报错
    need = 200 * 1024 * 1024
    for src, _name in wanted:
        try:
            need += Path(src).stat().st_size
        except OSError:
            continue
    ensure_free_space(folder, need)

    written: list[str] = []
    for src, name in wanted:
        source = Path(src)
        if not source.is_file():
            raise WBError("NOT_FOUND", "要打包的文件不在了", str(source))
        target = unique_path(folder / sanitize_name(name, max_len=80) or source.name)
        shutil.copy2(source, target)
        written.append(str(target))
    note_path = folder / BUNDLE_NOTE
    note_path.write_text(notes, encoding="utf-8")
    written.append(str(note_path))
    if copy_text.strip():
        copy_path = folder / BUNDLE_COPY
        copy_path.write_text(copy_text, encoding="utf-8")
        written.append(str(copy_path))
    return {
        "dir": str(folder),
        "files": written,
        "bytes": sum(Path(p).stat().st_size for p in written if Path(p).is_file()),
    }
