"""视频元信息与缩略图（P1-05 / P1-06）。

这层很薄，但有三件事必须在这里定死，否则后面每个页面都会各写一份：

1. **缩略图放哪**：固定的 ``frames/thumb.jpg``。不做"每集不同名字"，因为列表页
   要按路径直接拼，多一次数据库查询就是 100 个项目卡一下的原因。
2. **字幕算不算已有**：同名 ``.srt`` / ``.ass`` / ``.vtt`` 都认。用户从剪辑软件
   导出时经常是 ass，只认 srt 会让他以为"没字幕，得重新转录"。
3. **抽帧失败不算失败**：缩略图是装饰，视频能播才是正事。抽不出来就返回 None，
   让界面上先显示占位图。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..app.errors import WBError
from ..infra.ffmpeg import FFmpeg, MediaInfo, best_thumb_time

THUMB_NAME = "thumb.jpg"
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt")
VIDEO_EXTS = (".mp4", ".mkv", ".flv", ".mov", ".webm", ".m4v", ".ts", ".avi")

__all__ = [
    "MediaInfo",
    "FFmpeg",
    "THUMB_NAME",
    "SUBTITLE_EXTS",
    "VIDEO_EXTS",
    "is_video",
    "sibling_subtitles",
    "probe_file",
    "make_thumbnail",
    "thumb_path",
    "guess_aspect",
]


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def sibling_subtitles(video: str | Path) -> list[Path]:
    """同目录同名的字幕文件。按 ``.zh`` 之类的语言后缀猜不到，只认 stem 完全一致。"""
    target = Path(video)
    folder = target.parent
    stem = target.stem
    found: list[Path] = []
    if not folder.is_dir():
        return found
    for entry in folder.iterdir():
        if not entry.is_file() or entry.suffix.lower() not in SUBTITLE_EXTS:
            continue
        if entry.stem == stem:
            found.append(entry)
    return sorted(found)


def thumb_path(root: str | Path) -> Path:
    return Path(root) / "frames" / THUMB_NAME


def probe_file(path: str | Path, ff: FFmpeg | None = None) -> MediaInfo:
    return (ff or FFmpeg()).probe(path)


def make_thumbnail(
    video: str | Path,
    root: str | Path,
    *,
    ff: FFmpeg | None = None,
    at: float | None = None,
    width: int = 480,
) -> Path | None:
    """抽一帧存到 ``root/frames/thumb.jpg``。失败返回 None——绝不因为缩略图打断流程。"""
    runner = ff or FFmpeg()
    dest = thumb_path(root)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest  # 已经有了就别重抽：重复导入同一集会反复解帧
    if not runner.available:
        return None
    moment = at
    if moment is None:
        try:
            info = runner.probe(video)
            moment = best_thumb_time(info.duration_ms)
        except WBError:
            moment = 1.0
    try:
        return runner.thumbnail(video, dest, at=moment, width=width)
    except WBError:
        return None


def guess_aspect(width: int, height: int) -> str:
    """给界面用的比例标签。竖屏切片是这个项目的主要产物，必须一眼能认出来。"""
    if not width or not height:
        return ""
    ratio = width / height
    if ratio > 1.5:
        return "16:9" if ratio < 2.0 else "超宽"
    if ratio < 0.7:
        return "9:16"
    if 0.9 < ratio < 1.1:
        return "1:1"
    return f"{ratio:.2f}:1"
