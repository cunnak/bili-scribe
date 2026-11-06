"""本地导入（P1-05）。

**导入不搬文件。** 用户的素材往往已经在素材盘/剪辑工程里排好了目录，把 4GB 的
``.mov`` 复制一份到 ``media_root`` 会吃掉双倍空间，而且从此"改哪个"都说不清。
所以：**原文件留在原地**（路径记进 asset），``media_root`` 下只建这一集的
「衍生品目录」（frames/clips/export/logs）。

**同名 SRT 直接认。** 有字幕就不该再花 40 分钟转录。找到字幕时项目直接进
``stage=subtitle``（待校对），没有就 ``stage=new``（待转录）——这个判断只做一次，
后面所有页面看的都是同一个 stage。

**重复导入静默跳过。** 用户会把整个文件夹拖两次。第二次不该报错，也不该多出
第二条项目——按「视频绝对路径」判重。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from ..app.errors import WBError
from .layout import allocate
from .probe import is_video, make_thumbnail, probe_file, sibling_subtitles

Prober = Callable[[Path], Any]
ThumbMaker = Callable[..., Any]


def scan_folder(folder: str | Path, recursive: bool = True) -> list[Path]:
    """列出文件夹里的视频，按名字排序（排序稳定，用户看到的顺序每次都一样）。"""
    root = Path(folder)
    if not root.exists():
        raise WBError("NOT_FOUND", "目录不存在", str(root))
    pattern = "**/*" if recursive else "*"
    files = [p for p in root.glob(pattern) if p.is_file() and is_video(p)]
    return sorted(files, key=lambda p: (str(p.parent).lower(), p.name.lower()))


def resolve_inputs(paths: Iterable[str | Path], recursive: bool = True) -> list[Path]:
    """把"若干个文件/文件夹"摊平成视频文件列表。目录递归展开，非视频忽略。"""
    out: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        item = Path(str(raw))
        candidates = (
            scan_folder(item, recursive) if item.is_dir() else ([item] if is_video(item) else [])
        )
        for path in candidates:
            key = str(path.resolve()).lower()
            if key not in seen:
                seen.add(key)
                out.append(path)
    return out


def import_videos(
    paths: Iterable[str | Path],
    *,
    media_root: str | Path,
    repo: Any,
    prober: Prober | None = None,
    thumbs: ThumbMaker | None = None,
    make_thumbs: bool = True,
    recursive: bool = True,
) -> dict[str, Any]:
    """把一批本地视频收进素材库。返回导入结果清单（成功/跳过/失败都记）。"""
    files = resolve_inputs(paths, recursive=recursive)
    if not files:
        raise WBError("VALIDATION", "没有找到视频文件")

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for video in files:
        absolute = str(video.resolve())
        try:
            existing = _find_by_path(repo, absolute)
            if existing:
                skipped.append({"path": absolute, "reason": "已导入", "project_id": existing["id"]})
                continue

            layout = allocate(media_root, up_name="本地导入", title=video.stem).ensure()
            info = None
            if prober is not None:
                try:
                    info = prober(video)
                except Exception:  # noqa: BLE001 —— 读不出信息也允许入库，只是少了时长
                    info = None

            subtitles = sibling_subtitles(video)
            project = repo.project_create(
                title=video.stem,
                source_kind="local",
                root_dir=str(layout.root),
                stage="subtitle" if subtitles else "new",
                status="ready",
            )
            repo.asset_upsert(
                project["id"],
                "source",
                absolute,
                bytes=video.stat().st_size,
                **(_probe_fields(info) if info is not None else {}),
            )
            for sub in subtitles:
                repo.asset_upsert(project["id"], "subtitle", str(sub.resolve()), language="zh")

            thumb = None
            if make_thumbs and thumbs is not None:
                try:
                    thumb = thumbs(video, layout.root)
                except Exception:  # noqa: BLE001
                    thumb = None

            imported.append(
                {
                    "project_id": project["id"],
                    "path": absolute,
                    "root": str(layout.root),
                    "stage": project.get("stage"),
                    "subtitles": [str(s) for s in subtitles],
                    "thumb": str(thumb) if thumb else "",
                }
            )
        except WBError as exc:
            failed.append({"path": absolute, "code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 —— 单个文件失败不能中断整批
            failed.append({"path": absolute, "code": "INTERNAL", "message": str(exc)})

    return {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "total": len(files),
    }


def _find_by_path(repo: Any, absolute: str) -> dict[str, Any] | None:
    """按资产路径判重。查询走 SQL 而不是把全表拉进内存——素材库迟早会有上千条。"""
    conn = getattr(repo, "conn", None)
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT p.* FROM wb_project p JOIN wb_asset a ON a.project_id=p.id"
                " WHERE a.kind='source' AND a.path=? LIMIT 1",
                (absolute,),
            ).fetchone()
            if row:
                return dict(row)
            return None
        except Exception:  # noqa: BLE001
            pass
    return None


def _probe_fields(info: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for attr in ("duration_ms", "width", "height", "fps", "vcodec", "acodec"):
        value = getattr(info, attr, None)
        if value not in (None, ""):
            fields[attr] = value
    return fields


def default_import(
    paths: Iterable[str | Path],
    *,
    media_root: str | Path,
    repo: Any,
    ffmpeg_dir: str | None = None,
) -> dict[str, Any]:
    """给 service 用的默认装配：真 ffprobe + 真抽帧。"""
    from ..infra.ffmpeg import FFmpeg

    ff = FFmpeg(prefer_dir=ffmpeg_dir)

    def prober(path: Path) -> Any:
        return probe_file(path, ff)

    def thumbs(path: Path, root: str | Path) -> Any:
        return make_thumbnail(path, root, ff=ff)

    return import_videos(
        paths,
        media_root=media_root,
        repo=repo,
        prober=prober,
        thumbs=thumbs,
        make_thumbs=ff.available,
    )
