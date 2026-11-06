"""切片封面（P3-08）。

默认取「切片起点 + 1 秒」那一帧——起点那帧常常是转场或字幕还没出来，
往后 1 秒是这段时间里最稳的经验值。也可以换帧（给毫秒）或上传一张图。

封面是**文件**，路径记在候选的 copy 里；换封面只覆盖同一个文件名，
不留一堆旧图在 `covers/` 里发霉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ..app.errors import WBError
from ..infra.files import sanitize_name, unique_path

DEFAULT_OFFSET_MS = 1000
COVER_WIDTH = 1280
COVER_QUALITY = 3
COVER_DIR = "covers"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def cover_dir(root: str | Path) -> Path:
    return Path(root) / COVER_DIR


def cover_path(root: str | Path, candidate_id: str) -> Path:
    """固定文件名：一个候选只有一张封面，换帧就是覆盖。"""
    safe = sanitize_name(candidate_id or "cover", max_len=48)
    return cover_dir(root) / f"{safe}.jpg"


def default_at(start_ms: int, end_ms: int, *, offset_ms: int = DEFAULT_OFFSET_MS) -> int:
    """默认抽帧位置：起点 + 1 秒，但不能越过终点。

    40 秒的切片用 1 秒没问题；0.8 秒的手动切片（P3-04 允许 ≥0.4 秒）里
    「起点 +1 秒」已经出界，直接夹到区间内，避免抽到下一段画面。
    """
    start = max(0, int(start_ms or 0))
    end = max(start + 1, int(end_ms or 0))
    at = start + max(0, int(offset_ms))
    return min(at, end - 1)


def clamp_at(start_ms: int, end_ms: int, at_ms: int) -> int:
    start = max(0, int(start_ms or 0))
    end = max(start + 1, int(end_ms or 0))
    return max(start, min(int(at_ms or 0), end - 1))


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def copy_image(src: str | Path, dest: str | Path) -> Path:
    """把上传的图片收进 covers/。只做字节拷贝——不做转码，避免又一次画质损失。"""
    import shutil

    source = Path(src)
    if not source.is_file():
        raise WBError("NOT_FOUND", "找不到这张图片", str(source))
    if not is_image(source):
        raise WBError("VALIDATION", "封面只支持 jpg / png / webp")
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def grab_frame(
    ffmpeg: Any,
    source: str | Path,
    candidate: dict[str, Any],
    root: str | Path,
    *,
    at_ms: int | None = None,
) -> dict[str, Any]:
    """抽帧写封面，返回 `{path, at_ms, bytes}`。

    `at_ms` 为空时用默认位置（起点 +1s）。抽帧失败由 ffmpeg 层抛 PROCESS_FAILED，
    这一层不吞——封面没出来就得让人看见。
    """
    start = int(candidate.get("start_ms") or 0)
    end = int(candidate.get("end_ms") or 0)
    at = default_at(start, end) if at_ms is None else clamp_at(start, end, at_ms)
    dest = cover_path(root, str(candidate.get("id") or "cover"))
    ffmpeg.thumbnail(source, dest, at=at / 1000.0, width=COVER_WIDTH, quality=COVER_QUALITY)
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise WBError("PROCESS_FAILED", "抽帧没有产生封面文件", str(dest))
    return {"path": str(dest), "at_ms": at, "bytes": dest.stat().st_size}


def frames_for(
    ffmpeg: Any, source: str | Path, candidate: dict[str, Any], root: str | Path, *, count: int = 4
) -> dict[str, Any]:
    """给界面一排可选帧：区间等分成 count 个候选点各抽一张。

    这是「换帧」最省事的做法——不用拖时间轴，直接看图挑。
    """
    start = int(candidate.get("start_ms") or 0)
    end = int(candidate.get("end_ms") or 0)
    span = max(1, end - start)
    total = max(1, int(count))
    picks: list[int] = []
    for index in range(total):
        # 均匀落在区间内侧，避开首尾各 5%
        ratio = (index + 1) / (total + 1)
        picks.append(clamp_at(start, end, start + int(span * ratio)))
    out: list[dict[str, Any]] = []
    base = cover_dir(root) / "frames"
    for index, at in enumerate(picks):
        dest = unique_path(base / f"{sanitize_name(str(candidate.get('id') or 'c'))}-{index}.jpg")
        ffmpeg.thumbnail(source, dest, at=at / 1000.0, width=COVER_WIDTH, quality=COVER_QUALITY)
        if dest.is_file() and dest.stat().st_size > 0:
            out.append({"at_ms": at, "path": str(dest), "bytes": dest.stat().st_size})
    return {"frames": out, "count": len(out)}


def frames_to_cleanup(root: str | Path, keep: Sequence[str]) -> list[str]:
    """挑出 frames/ 里该删的旧图（只保留本次给出过的）。

    不做这一步，换三次帧就留下十几张没人引用的 jpg——磁盘是用户的。
    """
    keep_set = {str(Path(item)) for item in keep or ()}
    base = cover_dir(root) / "frames"
    if not base.is_dir():
        return []
    return [str(item) for item in base.glob("*.jpg") if str(item) not in keep_set]
