"""抽帧：给总结附上画面参考。

## 两条腿

- **本地腿** `collect`：已经装好的 ffmpeg + 已经在盘的源视频。优先走这条，零出网。
- **服务端腿** `collect_from_api`：源视频不在盘上（或本地抽帧失败）时，走 B 站
  `x/player/videoshot` 的 10×10 拼图，按 aid 拉取并裁成单格。它需要 aid；WB 库里的
  项目只有 bvid，所以 `app/service.py` 先用 `Resolver` 把 bvid 解成 aid 再进来。

## 为什么服务端腿不直接调 bs `collect_frames`

bs 的实现是 `client.get_bytes(url)`，底层 `session.get(url, timeout=20)`：跟重定向、
无 host 白名单、无大小上限，而 url 来自外部接口返回（不可信数据）。这里把"取字节"
收成 `fetch_sprite`：只允许 https、只允许 hdslb/biliimg 白名单及其子域、不跟重定向、
Content-Length 与流式累计双重限长；任何违规抛 `FrameError`，上层只降级不崩任务。

## 形状必须与副本要的一致

副本 `core/summary/summarizer.py` 里那句 `frames_mod.as_content_blocks(frameset)` 只认
`bili_scribe.core.frames.FrameSet`。所以两条腿都**复用 bs 的数据类与转块函数**（纯数据、
不发网络请求），服务端腿只复用 `pick_cells` / `cell_box` 这两个纯函数。

## 本地腿：谁建谁删，而且只删自己建的那个

临时 jpg 写进 `out_dir` 下面一个**独占子目录**（`summary-<随机>`），编码成 data URL 之后
只删这个子目录（帧已经在内存里）。与 bs `collect_frames` 的 `finally: _remove_dir()` 同一个口径：
缓存目录的生命周期不出这个函数。

**不能直接删 `out_dir` 本身**：项目里的 `frames/` 是 `Layout.frames` 那个目录，封面缩略图
`frames/thumb.jpg` 就住在里面（`app/service.py` 的 `has_thumb` 读的就是它）。整目录 rmtree
会把封面一起抹掉。bs 那边也踩过共享目录的坑（1/6 概率 FileNotFoundError 竞态），
所以两边都走独占目录。

## 服务端腿：磁盘缓存是共享的，只靠原子替换

服务端腿的缓存按 `<cache_root>/frames/<bvid 或 aid>/` 组织，**不能**在任务收尾时删整个
目录——同一视频的两次采集可能并发。写入统一先写临时名再 `os.replace`，读者只看落地
文件；命中条件同时要求 `meta.json` 可用且 `max_frames` / `quality` 一致。清理只由启动时
的 `purge_stale_frames` 负责，按目录内最新 mtime 判断，`days <= 0` 表示不自动清理。
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError

from bili_scribe.core.frames import Frame, FrameError, FrameSet, cell_box, pick_cells
from bili_scribe.infra.endpoints import VIDEOSHOT

#: 抽帧宽度。bs 服务端单格原生就是 480，vision 模型也通常把长边压到 768 以内。
FRAME_WIDTH = 480
#: 单帧 base64 超过这个大小就丢掉（真实 480px JPEG 约 30–60KB，1.5MB 是异常值）。
MAX_IMAGE_BYTES = 1_500_000
#: 服务端拼图的下载上限。10×10、4800×2700 的 JPEG 实测约 1–3MB；8MB 留足余量，
#: 同时挡住接口被篡改后无限大的响应（不能只信 Content-Length，下面还会流式累计）。
MAX_SPRITE_BYTES = 8 * 1024 * 1024
#: 服务端拼图允许的 host。B 站图片 CDN 会在这两个域下轮换子域，因此按后缀放行。
SAFE_FRAME_HOSTS = ("hdslb.com", "biliimg.com")
#: ffmpeg `-q:v` 的取值范围（2 最好 31 最差）。
QV_BEST = 2
QV_WORST = 31


def quality_to_qv(quality: int) -> int:
    """把配置里的 JPEG quality（1–100，bs 的 Pillow 口径）换成 ffmpeg 的 `-q:v`（2–31）。

    两个旋钮方向相反、量纲也不同：Pillow 的 100 是最好，ffmpeg 的 2 是最好。
    配置字段沿用 bs 的语义（`quality: int = 85`，1–100），所以这里做一次线性换算，
    而不是另开一个用户看不懂的 `qv` 字段。85 ⇒ -q:v 6（实测换算表见
    `tests/wb/test_summary_frames.py`），够看清 PPT 上的字。
    """
    q = max(1, min(100, int(quality or 85)))
    return max(QV_BEST, min(QV_WORST, round((100 - q) * (QV_WORST - QV_BEST) / 99) + QV_BEST))


def frame_offsets(duration_ms: int, count: int) -> list[int]:
    """在整段时长里均匀取 `count` 个时刻（毫秒，不含首尾端点）。

    与 `clip_refine.frame_offsets` 同一个取法：`(i+1)/(count+1)` 处取点，
    避开片头黑场与片尾卡点。这里不 import 它——那个模块的常量是"精析 3 帧"的参数，
    借来用会让"总结要 12 帧"和"精析要 3 帧"两个旋钮将来漂到一起。
    """
    total = max(1, int(duration_ms))
    n = max(1, int(count))
    return [int(total * (i + 1) / (n + 1)) for i in range(n)]


def _hhmmss(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def data_url(path: str | Path) -> str:
    """读成 `data:image/jpeg;base64,...`。太大或空的返回空串（调用方丢掉这一帧）。"""
    file = Path(path)
    size = file.stat().st_size if file.is_file() else 0
    if not size or size > MAX_IMAGE_BYTES:
        return ""
    raw = base64.b64encode(file.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{raw}"


# ------------------------------------------------------------------ 服务端腿


def _validate_sprite_url(url: str) -> str:
    """校验拼图 URL：只放行 https、白名单域及其子域，且不许 userinfo/端口。

    接口返回的是外部数据，不能拿“看起来像 B 站”当证据。这里显式拒绝：
    - 非 https；
    - 带 userinfo（https://i0.hdslb.com@evil.com/）；
    - 显式端口（:443 也不放行，避免把默认端口当豁免）；
    - 域名只是把白名单当路径或查询串（https://evil.com/?x=https://i0.hdslb.com）；
    - 带结尾点的域名（i0.hdslb.com.），避免规范化差异成为绕过面。
    """
    if not isinstance(url, str) or not url.strip():
        raise FrameError("抽帧图地址为空")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise FrameError(f"抽帧图只允许 https：{url[:120]}")
    if parts.username is not None or parts.password is not None:
        raise FrameError(f"抽帧图地址不允许带用户信息：{url[:120]}")
    try:
        port = parts.port
    except ValueError as exc:
        raise FrameError(f"抽帧图地址端口非法：{url[:120]}") from exc
    if port is not None:
        raise FrameError(f"抽帧图地址不允许带端口：{url[:120]}")
    host = (parts.hostname or "").lower()
    if not host or host.endswith("."):
        raise FrameError(f"抽帧图地址没有合法域名：{url[:120]}")
    if not any(host == safe or host.endswith("." + safe) for safe in SAFE_FRAME_HOSTS):
        raise FrameError(f"抽帧图域名不在白名单：{host[:120]}")
    return url


def fetch_sprite(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = MAX_SPRITE_BYTES,
) -> bytes:
    """下载一张拼图，带 https/host 白名单、不跟重定向、双重大小上限与超时。

    返回原始字节；任何失败都抛 FrameError。这里是服务端腿唯一出网的地方，
    上层 collect_from_api 会把它降级成“这次没有画面”，不会让总结失败。
    """
    _validate_sprite_url(url)
    limit = int(max_bytes)
    if limit <= 0:
        raise FrameError("抽帧图大小上限必须为正数")
    try:
        response = requests.get(
            url,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - 网络层全部收成 FrameError
        raise FrameError(f"抽帧图下载失败：{exc}") from exc
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            raise FrameError(f"抽帧图返回重定向（HTTP {status}），已拒绝跟随")
        if status != 200:
            raise FrameError(f"抽帧图返回 HTTP {status}")
        headers = getattr(response, "headers", {}) or {}
        declared = headers.get("Content-Length")
        if declared not in (None, ""):
            try:
                declared_size = int(declared)
            except (TypeError, ValueError) as exc:
                raise FrameError("抽帧图 Content-Length 不是整数") from exc
            if declared_size < 0:
                raise FrameError("抽帧图 Content-Length 为负数")
            if declared_size > limit:
                raise FrameError(f"抽帧图超过大小上限：Content-Length={declared_size} > {limit}")
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise FrameError(f"抽帧图超过大小上限：已读 {total} > {limit}")
                chunks.append(chunk)
        except FrameError:
            raise
        except Exception as exc:  # noqa: BLE001 - 流式读取失败也收成 FrameError
            raise FrameError(f"抽帧图读取失败：{exc}") from exc
        raw = b"".join(chunks)
        if not raw:
            raise FrameError("抽帧图内容为空")
        return raw
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _replace_into_place(tmp: Path, path: Path) -> None:
    """把临时文件换成目标名，Windows 上并发写同一格时要能容错。

    实测（tests/wb/test_frames_cache.py 的并发用例，约 2/5 概率）：两个线程同时
    `os.replace` 到同一个 `0.jpg`，其中一个会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这不是数据坏了——目标位置上已经有另一份完整内容（同一 aid 同一格，字节等价）。
    所以先重试几次；仍拿不到独占时，只要目标已存在且非空就按成功处理。
    """
    last: OSError | None = None
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.002 * (attempt + 1))
    try:
        ok = path.is_file() and path.stat().st_size > 0
    except OSError:
        ok = False
    if ok:
        return
    raise last if last is not None else OSError("抽帧缓存写入失败")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时名 + os.replace：并发读者只会看到完整的旧文件或新文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_into_place(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _quality(cfg: Any) -> int:
    return max(1, min(100, int(getattr(cfg, "quality", 85) or 85)))


def _meta_from_api(data: Any, *, max_frames: int, quality: int) -> dict[str, Any] | None:
    """把 videoshot 的 data 归一成缓存 meta；字段不合法就返回 None。"""
    if not isinstance(data, dict):
        return None
    raw_urls = data.get("image") or data.get("urls") or []
    if not isinstance(raw_urls, list):
        return None
    urls: list[str] = []
    for item in raw_urls:
        if not isinstance(item, str) or not item.strip():
            continue
        urls.append(item if item.startswith("http") else "https:" + item)
    if not urls or len(urls) > 1000:
        return None
    raw_index = data.get("index") or []
    if not isinstance(raw_index, list):
        raw_index = []
    index: list[int | None] = []
    for item in raw_index:
        if item is None:
            index.append(None)
            continue
        try:
            index.append(int(item))
        except (TypeError, ValueError):
            return None
    try:
        grid_x = int(data.get("img_x_len") or 10)
        grid_y = int(data.get("img_y_len") or 10)
        cw = int(data.get("img_x_size") or FRAME_WIDTH)
        ch = int(data.get("img_y_size") or 270)
    except (TypeError, ValueError):
        return None
    if not (1 <= grid_x <= 100 and 1 <= grid_y <= 100):
        return None
    if grid_x * grid_y > 10_000 or not (0 < cw <= 10_000 and 0 < ch <= 10_000):
        return None
    return {
        "version": 1,
        "urls": urls,
        "index": index,
        "grid_x": grid_x,
        "grid_y": grid_y,
        "cw": cw,
        "ch": ch,
        "max_frames": int(max_frames),
        "quality": int(quality),
    }


def _load_meta(cache: Path) -> dict[str, Any] | None:
    path = cache / "meta.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return _meta_from_api(
            raw,
            max_frames=int(raw.get("max_frames") or 0),
            quality=int(raw.get("quality") or 0),
        )
    except (OSError, TypeError, ValueError):
        # 坏 meta 只是缓存未命中，不能把它抛到总结链路上去。
        return None


def _meta_matches(meta: dict[str, Any] | None, cfg: Any) -> bool:
    """缓存 meta 是否与当前两个旋钮一致；一致才允许复用已落地的单格文件。"""
    if not meta:
        return False
    want = int(getattr(cfg, "max_frames", 0) or 0)
    return (
        want > 0
        and int(meta.get("max_frames") or 0) == want
        and int(meta.get("quality") or 0) == _quality(cfg)
    )


def _is_jpeg(raw: bytes) -> bool:
    return len(raw) > 4 and raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")


def _cached_picks(meta: dict[str, Any], cfg: Any, cache: Path) -> list[int] | None:
    want = int(getattr(cfg, "max_frames", 0) or 0)
    if want <= 0:
        return None
    if int(meta.get("max_frames") or 0) != want:
        return None
    if int(meta.get("quality") or 0) != _quality(cfg):
        return None
    total = len(meta["urls"]) * int(meta["grid_x"]) * int(meta["grid_y"])
    picks = pick_cells(total, len(meta["index"]), want)
    if not picks:
        return None
    for cell in picks:
        path = cache / f"{cell}.jpg"
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return None
        except OSError:
            return None
    return picks


def _frame_notes(meta: dict[str, Any], picks: list[int], want: int) -> list[str]:
    notes = ["以下画面由 B站 videoshot 接口按 aid 抽取（服务端拼图裁格）。"]
    if not meta["index"]:
        notes.append("接口未提供时间戳，画面只能按先后顺序使用，不能标注具体时刻")
    if len(picks) < want:
        notes.append(f"该视频只有 {len(picks)} 个可用抽帧格子，少于设定的 {want}")
    return notes


def _read_cached_frames(meta: dict[str, Any], cache: Path, picks: list[int], want: int) -> FrameSet:
    frames: list[Frame] = []
    for order, cell in enumerate(picks, start=1):
        path = cache / f"{cell}.jpg"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise FrameError(f"帧缓存读取失败：{exc}") from exc
        if not _is_jpeg(raw):
            raise FrameError("帧缓存不是有效 JPEG")
        ts = meta["index"][cell] if cell < len(meta["index"]) else None
        frames.append(
            Frame(
                order=order,
                timestamp=ts,
                data_url="data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                size=len(raw),
            )
        )
    return FrameSet(
        frames=frames,
        total_cells=len(meta["urls"]) * int(meta["grid_x"]) * int(meta["grid_y"]),
        sprites=len(meta["urls"]),
        has_timestamps=bool(meta["index"]) and all(ts is not None for ts in meta["index"]),
        notes=_frame_notes(meta, picks, want),
    )


def _download_frames(
    meta: dict[str, Any],
    cache: Path,
    picks: list[int],
    cfg: Any,
    fetch_bytes: Callable[[str], bytes],
    progress: Callable[[str], None] | None,
    want: int,
    *,
    reuse_cached: bool = False,
) -> FrameSet:
    grid_x = int(meta["grid_x"])
    grid_y = int(meta["grid_y"])
    cw = int(meta["cw"])
    ch = int(meta["ch"])
    per_sprite = grid_x * grid_y
    quality = _quality(cfg)
    fetched_sprites: dict[int, Image.Image] = {}
    frames: list[Frame] = []
    for order, cell in enumerate(picks, start=1):
        cached = cache / f"{cell}.jpg"
        raw = b""
        if reuse_cached:
            try:
                if cached.is_file():
                    raw = cached.read_bytes()
            except OSError:
                raw = b""
        if not _is_jpeg(raw):
            sprite_i = cell // per_sprite
            if sprite_i not in fetched_sprites:
                if progress:
                    progress(f"下载抽帧拼图 {sprite_i + 1}/{len(meta['urls'])}")
                url = _validate_sprite_url(str(meta["urls"][sprite_i]))
                try:
                    sprite_raw = fetch_bytes(url)
                except FrameError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 下载腿异常统一成 FrameError
                    raise FrameError(f"抽帧图下载失败：{exc}") from exc
                if len(sprite_raw) > MAX_SPRITE_BYTES:
                    raise FrameError("抽帧图超过大小上限")
                try:
                    image = Image.open(io.BytesIO(sprite_raw)).convert("RGB")
                except (UnidentifiedImageError, OSError) as exc:
                    raise FrameError(f"抽帧图不是有效图片：{exc}") from exc
                if image.width < grid_x * cw or image.height < grid_y * ch:
                    raise FrameError(
                        f"抽帧图尺寸 {image.width}x{image.height} 小于声明网格 "
                        f"{grid_x}x{grid_y}×{cw}x{ch}"
                    )
                fetched_sprites[sprite_i] = image
            buf = io.BytesIO()
            fetched_sprites[sprite_i].crop(cell_box(cell, grid_x, grid_y, cw, ch)).save(
                buf, "JPEG", quality=quality, optimize=True
            )
            raw = buf.getvalue()
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise FrameError("单帧超过大小上限")
            _atomic_write_bytes(cached, raw)
        ts = meta["index"][cell] if cell < len(meta["index"]) else None
        frames.append(
            Frame(
                order=order,
                timestamp=ts,
                data_url="data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                size=len(raw),
            )
        )
    return FrameSet(
        frames=frames,
        total_cells=len(meta["urls"]) * per_sprite,
        sprites=len(meta["urls"]),
        has_timestamps=bool(meta["index"]) and all(ts is not None for ts in meta["index"]),
        notes=_frame_notes(meta, picks, want),
    )


def collect_from_api(
    gateway: Any,
    fetch_bytes: Callable[[str], bytes],
    aid: int | str | None,
    cfg: Any,
    *,
    cache_dir: str | Path,
    progress: Callable[[str], None] | None = None,
) -> FrameSet:
    """从 B 站 videoshot 接口采集拼图并裁成单格。

    gateway 只需提供 get_data(url, params)（与 WB 的 BiliGateway 同形）；
    fetch_bytes 只需接受一个 URL 返回 bytes（生产传 fetch_sprite，测试传假函数）。
    失败一律抛 FrameError，由 _summary_frameset 降级成无画面。
    """
    if not bool(getattr(cfg, "enabled", False)):
        raise FrameError("画面采集未启用")
    want = int(getattr(cfg, "max_frames", 0) or 0)
    if want <= 0:
        raise FrameError("每稿最多截帧是 0，没有画面可附")
    try:
        aid_value = int(aid) if aid is not None else 0
    except (TypeError, ValueError) as exc:
        raise FrameError(f"aid 非法：{aid!r}") from exc
    if aid_value <= 0:
        raise FrameError("缺少有效 aid，服务端抽帧接口只认 aid")

    cache = Path(cache_dir)
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FrameError(f"帧缓存目录不可用：{exc}") from exc

    cached_meta = _load_meta(cache)
    if cached_meta is not None:
        picks = _cached_picks(cached_meta, cfg, cache)
        if picks:
            try:
                frameset = _read_cached_frames(cached_meta, cache, picks, want)
            except FrameError:
                frameset = None
            if frameset is not None:
                if progress:
                    progress(
                        f"画面就绪：{len(frameset.frames)} 帧 / "
                        f"{frameset.total_bytes // 1024} KB（缓存）"
                    )
                return frameset

    try:
        data = gateway.get_data(VIDEOSHOT, {"aid": aid_value, "index": 1})
    except Exception as exc:  # noqa: BLE001 - 接口层异常统一成 FrameError
        raise FrameError(f"抽帧接口调用失败：{exc}") from exc
    meta = _meta_from_api(data, max_frames=want, quality=_quality(cfg))
    if meta is None:
        raise FrameError("抽帧接口返回的元数据不合法或没有可用拼图")
    total = len(meta["urls"]) * int(meta["grid_x"]) * int(meta["grid_y"])
    picks = pick_cells(total, len(meta["index"]), want)
    if not picks:
        raise FrameError("没有可选的画面帧")

    if progress:
        progress("读取全片抽帧索引")
    frameset = _download_frames(
        meta,
        cache,
        picks,
        cfg,
        fetch_bytes,
        progress,
        want,
        reuse_cached=_meta_matches(cached_meta, cfg),
    )
    _atomic_write_bytes(
        cache / "meta.json",
        json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )
    if progress:
        progress(f"画面就绪：{len(frameset.frames)} 帧 / {frameset.total_bytes // 1024} KB")
    return frameset


def _is_frame_cache_name(name: str) -> bool:
    """缓存目录名只认 bvid 或 aid，别的目录一律不碰（例如项目自己的 frames/thumb）。"""
    if name.isdigit():
        return True
    return (
        name.startswith("BV")
        and len(name) >= 10
        and all(ch.isascii() and ch.isalnum() for ch in name)
    )


def _latest_mtime(path: Path) -> float:
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for child in path.rglob("*"):
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest


def purge_stale_frames(root: str | Path, days: int) -> int:
    """清掉超过 days 天未动的服务端帧缓存，返回清掉的目录数。

    只处理 root 下名字像 bvid/aid 的直接子目录；文件（例如项目 frames/thumb.jpg）
    和其他目录不碰。days <= 0 表示不自动清理，与配置 frames_days 的语义一致。
    mtime 取目录内最新文件的时间，避免把正在被写的缓存误判成过期。
    """
    if int(days) <= 0:
        return 0
    base = Path(root)
    if not base.is_dir():
        return 0
    cutoff = time.time() - int(days) * 86400
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not _is_frame_cache_name(entry.name):
                continue
            if _latest_mtime(entry) >= cutoff:
                continue
            shutil.rmtree(entry)
            removed += 1
        except OSError:
            continue
    return removed


def collect(
    ffmpeg: Any,
    source: str | Path,
    cfg: Any,
    *,
    out_dir: str | Path,
    duration_ms: int = 0,
    progress: Callable[[str], None] | None = None,
) -> FrameSet:
    """从本地源视频抽 `cfg.max_frames` 帧，返回可交给副本的 `FrameSet`。

    `cfg` 只读三个字段：`enabled` / `max_frames` / `quality`（与 bs 的 `FramesConfig` 同形）。
    失败一律抛 `FrameError`，由调用方降级成"无画面"——与 bs 那条腿的错误面一致，
    免得上层要同时认识两种异常。
    """
    if not bool(getattr(cfg, "enabled", False)):
        raise FrameError("画面采集未启用")
    want = int(getattr(cfg, "max_frames", 0) or 0)
    if want <= 0:
        raise FrameError("每稿最多截帧是 0，没有画面可附")
    if not getattr(ffmpeg, "available", False):
        raise FrameError("本机没有 ffmpeg，抽不了帧")
    src = Path(str(source))
    if not src.is_file():
        raise FrameError(f"源视频不在盘上：{src.name}")

    total = int(duration_ms or 0)
    if total <= 0:
        try:
            total = int(ffmpeg.probe(src).duration_ms or 0)
        except Exception as exc:  # noqa: BLE001 - 读不到时长就按 0 处理，下面会报错
            raise FrameError(f"读不出源视频时长：{exc}") from exc
    if total <= 0:
        raise FrameError("源视频时长为 0，没法均匀取帧")

    parent = Path(out_dir)
    parent.mkdir(parents=True, exist_ok=True)
    # 独占子目录：只删自己建的那一个，不碰父目录里别人的文件（封面 thumb.jpg 就住在里面）。
    out = parent / f"summary-{secrets.token_hex(4)}"
    out.mkdir(parents=True, exist_ok=True)
    quality = quality_to_qv(int(getattr(cfg, "quality", 85) or 85))
    frames: list[Frame] = []
    notes: list[str] = ["以下画面由本地 ffmpeg 从源视频等距抽取。"]
    try:
        for order, at_ms in enumerate(frame_offsets(total, want), start=1):
            dest = out / f"f{order}.jpg"
            try:
                ffmpeg.thumbnail(src, dest, at=at_ms / 1000.0, width=FRAME_WIDTH, quality=quality)
            except Exception as exc:  # noqa: BLE001 - 单帧失败不该毁掉整次总结
                notes.append(f"第 {order} 帧没抽出来：{exc}")
                continue
            url = data_url(dest)
            if not url:
                notes.append(f"第 {order} 帧超过大小上限，已丢弃")
                continue
            frames.append(
                Frame(
                    order=len(frames) + 1,
                    timestamp=int(at_ms // 1000),
                    data_url=url,
                    size=dest.stat().st_size,
                )
            )
    finally:
        # 帧已经在内存里（data URL），临时目录立刻收掉：谁建谁删，与 bs 同口径。
        shutil.rmtree(out, ignore_errors=True)

    if not frames:
        raise FrameError("一帧都没抽出来")
    if len(frames) < want:
        notes.append(f"该视频只抽出 {len(frames)} 帧（计划 {want} 帧）。")
    if progress:
        progress(f"画面就绪：{len(frames)} 帧 / {sum(f.size for f in frames) // 1024} KB")
    return FrameSet(
        frames=frames,
        total_cells=want,
        sprites=0,
        has_timestamps=True,
        notes=notes,
    )


def timeline_label(seconds: int) -> str:
    """给测试与文档用：把帧时刻格式化成与 bs `Frame.label` 一致的样子。"""
    return _hhmmss(int(seconds))
