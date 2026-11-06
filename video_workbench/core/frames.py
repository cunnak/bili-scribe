"""本地 ffmpeg 抽帧：给总结附上画面参考。

## 为什么 WB 自己抽，不调 bili-scribe 的 `collect_frames`

bs 那条腿走的是 `x/player/videoshot`（服务端把整段视频做成 10×10 拼图再裁格），
前置条件是 **aid**。WB 库里 32 个项目 `aid` 全空（现读 `wb_project`），只有 `bvid` 有值，
要接那条腿得先补一遍 bvid→aid 的解析；而 `bili_scribe/infra/bili_client.py` 的
`get_bytes(url)` 是 `session.get(url, timeout=20)` —— 跟重定向、无 host 白名单、无大小上限，
url 又来自外部接口返回（不可信数据）。WB 侧不该原样继承这个面。

本地腿用的是已经装好的 ffmpeg + 已经在盘的源视频：现读 `wb_asset` 里 31 条 source 资产
有 24 条文件仍在盘。抽帧失败不毁整条总结（与 bs 的 `FrameError` 同一条口径：调用方降级
为"无画面"，不是整条任务失败）。

## 形状必须与副本要的一致

副本 `core/summary/summarizer.py` 里那句 `frames_mod.as_content_blocks(frameset)` 只认
`bili_scribe.core.frames.FrameSet`。所以这里**复用 bs 的数据类与转块函数**（纯数据、
不发网络请求），只自己实现"从本地文件抽帧"这一步。帧宽取 480 与 bs 原生单格一致。

## 谁建谁删，而且只删自己建的那个

临时 jpg 写进 `out_dir` 下面一个**独占子目录**（`summary-<随机>`），编码成 data URL 之后
只删这个子目录（帧已经在内存里）。与 bs `collect_frames` 的 `finally: _remove_dir()` 同一个口径：
缓存目录的生命周期不出这个函数。

**不能直接删 `out_dir` 本身**：项目里的 `frames/` 是 `Layout.frames` 那个目录，封面缩略图
`frames/thumb.jpg` 就住在里面（`app/service.py` 的 `has_thumb` 读的就是它）。整目录 rmtree
会把封面一起抹掉。bs 那边也踩过共享目录的坑（1/6 概率 FileNotFoundError 竞态），
所以两边都走独占目录。
"""

from __future__ import annotations

import base64
import secrets
import shutil
from pathlib import Path
from typing import Any, Callable

from bili_scribe.core.frames import Frame, FrameError, FrameSet

#: 抽帧宽度。bs 服务端单格原生就是 480，vision 模型也通常把长边压到 768 以内。
FRAME_WIDTH = 480
#: 单帧 base64 超过这个大小就丢掉（真实 480px JPEG 约 30–60KB，1.5MB 是异常值）。
MAX_IMAGE_BYTES = 1_500_000
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
