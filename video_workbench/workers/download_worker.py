"""下载 worker：把"一个待下载单位"落成磁盘上的一集（P1-02 / P1-03 / P1-07）。

这个文件只做编排，不实现下载与目录规则——那两件事分别在
``core/downloader.py``（怎么下）与 ``core/layout.py``（下到哪）。

三个决定值得写下来：

**1. 已下载就复用，不重下。** 用户从合集点进来、上次中断过、或者手滑点了两次，
   都会重复触发同一集。判据不是"目录存在"，而是"侧车里的 bvid+cid 就是这一集
   且文件还在"——目录还存在但视频被删了，那必须重下。

**2. 进度从 0.05 起跳。** 下载占了整个任务 5%→95% 的区间，剩下的 5% 留给
   "写侧车 / 抽元信息 / 入库"。这样进度条到 100% 的时候，库里已经有东西了，
   不会出现"进度满格但列表里找不到"。

**3. 失败要落到项目上。** 任务失败由 JobManager 记账，但用户是在素材库里看这一集
   的，所以 project.status/error 也必须同步——否则列表里它还是"就绪"。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..app.errors import WBError
from ..app.jobs import JobContext
from ..core import downloader as downloader_mod
from ..core.layout import (
    allocate,
    build_sidecar,
    finalize_source,
    find_source,
    read_sidecar,
    source_stem,
    write_sidecar,
)
from ..infra.db import now_iso

# 复用门槛：太小说明上次下到一半（或者文件被截断），不能当"已下载"
MIN_REUSE_BYTES = 64 * 1024

CookieProvider = Callable[[], str | None]
Prober = Callable[[Path], Any]

QUALITY_LABELS = {"720P", "1080P", "1080P+", "4K"}


def watch_url(bvid: str, page: int = 1) -> str:
    """拼出带分P参数的正站地址。用 bvid 而不是用户粘的原链：

    原链可能是短链、可能带一堆追踪参数，甚至可能是合集页。
    下载器只该看到"确定的一集"。
    """
    base = f"https://www.bilibili.com/video/{bvid}"
    return f"{base}?p={int(page)}" if int(page or 1) > 1 else base


def _preferred_source_path(repo: Any, project_id: str) -> str:
    """DB 里这一集源文件的路径——**读侧的权威来源**。取最新一条真实存在的。

    为什么要绕这一圈而不是直接拼文件名：媒体名跟着视频标题走之后，只有下载那一刻写进
    DB 的路径知道文件到底叫什么（UP 中途改标题、撞名追加 " (2)"、改名失败退回暂存名，
    都会让"按标题重算一遍名字"算出另一个答案）。
    """
    try:
        rows = repo.asset_list(project_id)
    except Exception:  # noqa: BLE001 —— 库里读不到不该让下载失败，退回按目录找
        return ""
    for asset in reversed(list(rows or [])):
        if str(asset.get("kind") or "") != "source":
            continue
        path = str(asset.get("path") or "")
        if path and Path(path).is_file():
            return path
    return ""


def _existing_source(
    repo: Any, project_id: str, layout_root: Path, *, stem: str
) -> Path | None:
    """盘上这一集的视频文件；兜底规则只写在 :func:`find_source` 里。"""
    return find_source(layout_root, prefer=_preferred_source_path(repo, project_id), stem=stem)


def _existing_is_same(
    source: Path | None, sidecar_path: Path, bvid: str, cid: int | None
) -> bool:
    """目录里那一份是不是就是这一集，且文件确实在。

    判据是**侧车里的 bvid+cid**，不是文件名——所以文件名换成视频标题之后，
    续传命中同一集这件事不受影响。
    """
    if source is None or not source.is_file() or source.stat().st_size < MIN_REUSE_BYTES:
        return False
    if not sidecar_path.is_file():
        return False
    data = read_sidecar(sidecar_path)
    if not data or data.get("bvid") != bvid:
        return False
    if cid is not None and int(data.get("cid") or 0) != int(cid):
        return False
    return True


def make_download_handler(
    *,
    repo: Any,
    media_root: str | Path,
    cookie_provider: CookieProvider | None = None,
    prober: Prober | None = None,
    # 抽帧器：签名 (video_path, project_root) -> Path | None。
    # 和 prober 一样从外面注，是为了让"下载"这条链在没装 ffmpeg 的机器上照样跑完，
    # 只是少一张缩略图——缩略图失败从来不该让下载判负。
    thumbs: Callable[[Path, str], Path | None] | None = None,
    downloader: Callable[..., Any] | None = None,
    tool_version: str = __version__,
    # P1-08：兜底后端。backend 是"这一次用哪个"，任务 payload 里的 backend 优先；
    # bbdown_path/bbdown_args 来自配置，只在走 BBDown 时用得上。
    backend: str = "ytdlp",
    bbdown_path: str = "",
    bbdown_args: str = "",
) -> Callable[[JobContext, dict[str, Any]], dict[str, Any]]:
    """装配一个 handler。依赖全部在这里注入，测试时换成假的即可。"""
    media_root = str(media_root)
    run_download = downloader or downloader_mod.download

    def handler(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        item = payload.get("item") or {}
        bvid = str(item.get("bvid") or payload.get("bvid") or "")
        if not bvid:
            raise WBError("VALIDATION", "缺少 bvid，无法下载")
        cid = item.get("cid")
        cid = int(cid) if cid not in (None, "") else None
        page_no = int(item.get("page") or payload.get("page") or 1)
        quality = str(payload.get("quality") or "1080P").upper()
        if quality not in QUALITY_LABELS:
            quality = "1080P"
        # 任务里写的后端优先（用户点「用 BBDown 重试」会带 bbdown），否则用配置默认。
        # 认不出的值退回 yt-dlp：宁可下不了，也不能静默换后端。
        use_backend = str(payload.get("backend") or backend or "ytdlp").strip().lower()
        if use_backend not in downloader_mod.BACKENDS:
            use_backend = "ytdlp"
        if use_backend == "bbdown":
            ctx.log("[bbdown] 本次用 BBDown 下载（yt-dlp 失败后的兜底）")

        ctx.check_cancel()
        ctx.progress(0.02, stage="准备目录")
        # 标题是外部可控字段，清洗与截断都在 source_stem 里收口；目录名和文件名用同一个
        # 来源，避免"目录叫 A、里面的文件叫 B"。
        source_title = str(item.get("title") or payload.get("title") or bvid)
        source_part = str(item.get("part_title") or "")
        stem = source_stem(source_title, source_part)
        layout = allocate(
            media_root,
            up_name=str(payload.get("up_name") or item.get("up_name") or ""),
            title=source_title,
            collection_title=str(payload.get("collection_title") or ""),
            part_index=page_no if int(item.get("page_count") or payload.get("page_count") or 0) > 1 else None,
            part_title=source_part,
            bvid=bvid,
            cid=cid,
        ).ensure()

        project = _upsert_project(
            repo,
            bvid=bvid,
            cid=cid,
            page_no=page_no,
            title=str(item.get("title") or bvid),
            up_name=str(payload.get("up_name") or ""),
            up_mid=item.get("up_mid"),
            collection_title=str(payload.get("collection_title") or ""),
            root_dir=str(layout.root),
            status="downloading",
        )
        project_id = str(project.get("id") or "")

        existing = _existing_source(repo, project_id, layout.root, stem=stem)
        if _existing_is_same(existing, layout.sidecar, bvid, cid):
            ctx.log(f"[reuse] 已存在同集文件，跳过下载：{existing.name}")
            _finish_project(repo, project_id, stage="downloaded")
            return {
                "project_id": project_id,
                "path": str(existing),
                "reused": True,
                "quality_actual": read_sidecar(layout.sidecar).get("quality_actual", ""),
            }

        cookie_file = None
        if cookie_provider is not None:
            try:
                cookie_file = cookie_provider()
            except Exception as exc:  # noqa: BLE001 —— 拿不到 Cookie 也要能下低清
                ctx.log(f"[cookie] 登录态不可用，按游客下载：{exc}")

        ctx.progress(0.05, stage="下载中")
        last_reported = 0.0

        def on_event(event: dict[str, Any]) -> None:
            nonlocal last_reported
            if event.get("type") != "progress":
                return
            percent = float(event.get("percent") or 0.0)
            now = time.monotonic()
            # 进度事件很密（yt-dlp 每 100ms 一条）。这里限到 ~4Hz 再往上抛，
            # 否则 SSE 会变成"刷屏"，界面反而更难读。
            if now - last_reported < 0.25 and percent < 100.0:
                return
            last_reported = now
            speed = event.get("speed")
            eta = event.get("eta")
            ctx.progress(
                0.05 + percent / 100.0 * 0.9,
                stage=f"下载中 {percent:.1f}%",
                speed=_speed_text(speed),
                eta=_eta_text(eta),
            )

        try:
            result = run_download(
                url=str(payload.get("url") or watch_url(bvid, page_no)),
                home=layout.root,
                quality=quality,
                cookie_file=cookie_file,
                on_event=on_event,
                cancel=ctx.should_cancel,
                need_bytes=int(payload.get("need_bytes") or 0),
                backend=use_backend,
                bbdown_bin=bbdown_path,
                bbdown_args=bbdown_args,
            )
        except WBError as exc:
            _mark_failed(repo, project_id, "下载失败")
            # 失败原因得能看见：detail 里是 yt-dlp 最后几十行输出，不写进日志的话
            # 用户只看到「yt-dlp 执行失败」六个字，既不知道是登录态还是网络。
            tail = str(getattr(exc, "detail", "") or "").strip()
            for line in tail.splitlines()[-12:]:
                ctx.log(f"[{exc.code}] {line}")
            raise

        ctx.check_cancel()
        ctx.progress(0.96, stage="写入元信息")
        # 改名只在这一步、只有一个归属：下载器全程写暂存名 source.<ext>（断点续传的
        # .part 名要稳定），下完之后换成视频标题。**以 result.path 为唯一真相**，
        # 不按 mtime / 体积去猜产物——名字跟标题走之后，那些启发式会从兜底变成主路径，
        # 有把 B 视频记到 A 项目名下且不报错的风险。
        source_path = finalize_source(layout.root, result.path, stem=stem)
        if source_path.name != Path(str(result.path)).name:
            ctx.log(f"[name] 源文件命名为：{source_path.name}")
        sidecar = build_sidecar(
            bvid=bvid,
            aid=item.get("aid"),
            cid=cid,
            page=page_no,
            title=str(item.get("title") or bvid),
            up=str(payload.get("up_name") or ""),
            collection=str(payload.get("collection_title") or ""),
            quality_requested=quality,
            quality_actual=getattr(result, "quality_actual", "") or "",
            url=str(payload.get("url") or watch_url(bvid, page_no)),
            downloaded_at=now_iso(),
            tool_version=tool_version,
        )
        write_sidecar(layout, sidecar)

        asset_fields: dict[str, Any] = {"bytes": source_path.stat().st_size}
        if prober is not None:
            try:
                info = prober(source_path)
                asset_fields.update(_probe_fields(info))
            except Exception as exc:  # noqa: BLE001 —— 元信息失败不该让下载判负
                ctx.log(f"[probe] 读取视频信息失败（不影响下载）：{exc}")

        # 单例 kind：身份键不含 path，改名不会留下第二行指向死文件的"原始视频"。
        repo.asset_upsert_singleton(project_id, "source", str(source_path), **asset_fields)

        # 缩略图放在 source 之后：列表页没有缩略图也能用，但有了它一眼能认出来。
        # 这一步失败只记日志——P1 的验收要的是"下载成功且文件在位"。
        if thumbs is not None:
            try:
                thumb = thumbs(source_path, str(layout.root))
            except Exception as exc:  # noqa: BLE001
                thumb = None
                ctx.log(f"[thumb] 抽帧失败（不影响下载）：{exc}")
            if thumb is not None:
                try:
                    repo.asset_upsert(project_id, "thumb", str(thumb), bytes=Path(thumb).stat().st_size)
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"[thumb] 记录缩略图失败：{exc}")

        _finish_project(repo, project_id, stage="downloaded")
        ctx.progress(1.0, stage="完成")
        return {
            "project_id": project_id,
            "path": str(source_path),
            "reused": False,
            "quality_requested": quality,
            "quality_actual": getattr(result, "quality_actual", "") or "",
            "elapsed_s": getattr(result, "elapsed_s", None),
            "batch_id": payload.get("batch_id"),
        }

    return handler


# ------------------------------------------------------------------ 小工具


def _speed_text(speed: Any) -> str | None:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    from ..infra.files import human_bytes

    return f"{human_bytes(value)}/s"


def _eta_text(eta: Any) -> str | None:
    try:
        seconds = int(float(eta))
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _probe_fields(info: Any) -> dict[str, Any]:
    """把 MediaInfo（或任何有同名属性的对象）摊成 asset 列。"""
    fields: dict[str, Any] = {}
    mapping = {
        "duration_ms": "duration_ms",
        "width": "width",
        "height": "height",
        "fps": "fps",
        "vcodec": "vcodec",
        "acodec": "acodec",
    }
    for attr, column in mapping.items():
        value = getattr(info, attr, None)
        if value not in (None, ""):
            fields[column] = value
    return fields


def _upsert_project(repo: Any, **fields: Any) -> dict[str, Any]:
    """同一集只该有一个项目。已有就更新目录与状态，没有才新建。"""
    bvid = fields.get("bvid") or ""
    cid = fields.get("cid")
    existing = repo.project_find_by_bvid(bvid, cid) if bvid else None
    if existing:
        repo.project_update(
            existing["id"],
            title=fields.get("title") or existing.get("title"),
            root_dir=fields.get("root_dir") or existing.get("root_dir"),
            status=fields.get("status") or existing.get("status"),
            error=None,
        )
        return repo.project_get(existing["id"]) or existing
    return repo.project_create(**fields)


def _finish_project(repo: Any, project_id: str, *, stage: str) -> None:
    if not project_id:
        return
    repo.project_update(project_id, stage=stage, status="ready", error=None)


def _mark_failed(repo: Any, project_id: str, message: str) -> None:
    if not project_id:
        return
    repo.project_update(project_id, status="failed", error=message)
