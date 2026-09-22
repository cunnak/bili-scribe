"""全片抽帧：不下载视频，拿到整条片子的画面。

## 为什么用「裁单格」而不是「送整张拼图」

B站 的 `x/player/videoshot` 返回的是 **10×10 的网格拼图**（原图 4800×2700），
每格是一个时刻的画面（480×270）。直觉上把整张拼图交给模型，一次就能给 100 个时刻。

**实测推翻了这条路**：

| 方案 | 12 张的体积 | 每格有效分辨率 |
|---|---|---|
| 整张不缩 | 15.2 MB（发不出去） | 480×270 |
| 整张缩到长边 1600 | 4.5 MB | 160×90（字糊了） |
| **裁单格** | **~170 KB** | **480×270 原生** |

不缩则请求体发不出去；缩到发得出去，单格只剩 160×90——**画面上的字就读不出来，
而那正是加画面的全部价值**。裁单格绕开这个两难：体积只有整张的约 1/88，还是原生分辨率。

**成本实测**：12 帧裁单格 = 1,464 image_tokens（单帧 122），
占一条 4 小时视频总结总输入（86,428）的约 1.7%。
（整张拼图方案是 14,688，因为它会被端点缩到约 1M 像素再计费。）

## 画面里到底有没有值得加的东西

**取决于内容类型，这一点必须清楚**：

- **有 PPT／图表／代码的讲座** → 增量显著。实测能读出幻灯片标题、要点、图表标签、
  流程图框内文字，这些在字幕里完全没有。
- **纯口播直播（人像小窗 + 字幕 + 壁纸）** → 增量接近零。画面上的有效文字就是字幕本身，
  而字幕我们已经有全文了。

所以启用与否应该由使用者判断，不该默认对一切视频都生效。

## 已知的边缘情况

- **`index` 可能为空**：短一点的视频只有 `image` 没有时间戳（实测 35 分钟那条
  4 张拼图、0 个时间戳）。这时仍然可以用画面，但无法标注时间点，
  必须如实告诉模型"顺序是时间顺序，具体时刻未知"。
- **取景远则字小**：会场全景里的大屏投影，在 480×270 的单格里可能糊到不可读。
  这是抽帧接口的物理上限，不是参数能解决的。
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from ..config import FramesConfig
from ..infra.bili_client import BiliClient, BiliError
from ..infra.endpoints import VIDEOSHOT

# 拼图网格。接口会返回 img_x_len / img_y_len，这两个只是兜底默认值。
GRID_X_DEFAULT = 10
GRID_Y_DEFAULT = 10


class FrameError(RuntimeError):
    """抽帧这条路走不通了。调用方应当降级为"没有画面"，而不是让整条任务失败。"""


@dataclass
class Frame:
    """一帧画面。timestamp 为 None 表示接口没给时间戳。"""

    order: int
    timestamp: int | None
    data_url: str
    size: int

    @property
    def label(self) -> str:
        if self.timestamp is None:
            return f"第 {self.order} 帧"
        return f"{_hhmmss(self.timestamp)}"


@dataclass
class FrameSet:
    frames: list[Frame] = field(default_factory=list)
    total_cells: int = 0
    sprites: int = 0
    has_timestamps: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.frames)

    def timeline_note(self) -> str:
        """给模型看的一段说明：这些帧分别是什么时刻。"""
        if not self.frames:
            return ""
        if not self.has_timestamps:
            return (
                f"以下 {len(self.frames)} 帧按时间先后排列，"
                "但接口没有提供具体时刻，不要为它们编造时间点。"
            )
        return (
            f"以下 {len(self.frames)} 帧按时间先后排列，每帧标注了它在视频中的时刻："
            + "、".join(f.label for f in self.frames)
        )


def _hhmmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# ------------------------------------------------------------------ 选择


def pick_cells(total_cells: int, index_len: int, want: int) -> list[int]:
    """在可用格子里等距挑 `want` 个，返回格子的全局下标（升序）。

    用全局下标而不是 (拼图, 格内) 二元组：下标既能定位到图，也直接对应时间戳数组
    （`index[k]` 就是第 k 格的时间），少一层换算就少一处错。
    """
    if total_cells <= 0 or want <= 0:
        return []
    want = min(want, total_cells)
    if want == 1:
        return [0]
    # 有 index 时按时间戳选，保证铺满整条视频；没有时退化成按下标等距。
    span = (index_len - 1) if index_len else (total_cells - 1)
    picks: list[int] = []
    for i in range(want):
        p = round(i * span / (want - 1))
        picks.append(min(p, total_cells - 1))
    # 去重但保持升序（极短的视频会出现重复）
    out: list[int] = []
    for p in picks:
        if not out or p > out[-1]:
            out.append(p)
    return out


def cell_box(cell: int, grid_x: int, grid_y: int, cw: int, ch: int) -> tuple[int, int, int, int]:
    """全局格子下标 → 裁剪框。拼图内是行优先排布。"""
    per_sprite = grid_x * grid_y
    inner = cell % per_sprite
    row, col = divmod(inner, grid_x)
    return (col * cw, row * ch, (col + 1) * cw, (row + 1) * ch)


# ------------------------------------------------------------------ 缓存


def _cache_dir(db_path: str, aid: int) -> Path:
    """帧缓存放在数据库旁边，跟着 storage 配置走。"""
    return Path(db_path).parent / "frames" / str(aid)


def _load_meta(cache: Path) -> dict[str, Any] | None:
    f = cache / "meta.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


# ------------------------------------------------------------------ 主流程


def collect_frames(
    client: BiliClient,
    cfg: FramesConfig,
    aid: int | None,
    db_path: str,
    progress: Callable[[str, str], None] | None = None,
) -> FrameSet:
    """抓全片抽帧并裁成单格。

    任何一步走不通都抛 FrameError——调用方应当把它降级成"这次没有画面"，
    绝不能因为画面拿不到就让整条总结失败。
    """
    notify = progress or (lambda _stage, _message: None)

    if not cfg.enabled:
        raise FrameError("画面采集未启用")
    if not aid:
        raise FrameError("缺少 aid，抽帧接口只认 aid（bvid 拿不到时间戳）")
    if cfg.max_frames <= 0:
        raise FrameError("画面张数上限为 0")

    cache = _cache_dir(db_path, aid)
    cache.mkdir(parents=True, exist_ok=True)

    notify("fetching_frames", "读取全片抽帧索引")
    meta = _load_meta(cache)
    if meta is None:
        try:
            res = client.get_raw(VIDEOSHOT, params={"aid": aid, "index": 1})
        except BiliError as exc:
            raise FrameError(f"抽帧接口调用失败：{exc}") from exc
        code = res.get("code")
        if code != 0:
            raise FrameError(
                f"抽帧接口返回 code={code}：{res.get('message') or '未知原因'}"
            )
        data = res.get("data") or {}
        urls = [u if u.startswith("http") else "https:" + u for u in (data.get("image") or [])]
        if not urls:
            raise FrameError("该视频没有可用的抽帧图（接口返回空列表）")
        meta = {
            "urls": urls,
            "index": list(data.get("index") or []),
            "grid_x": int(data.get("img_x_len") or GRID_X_DEFAULT),
            "grid_y": int(data.get("img_y_len") or GRID_Y_DEFAULT),
            "cw": int(data.get("img_x_size") or 480),
            "ch": int(data.get("img_y_size") or 270),
        }
        (cache / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

    urls: list[str] = meta["urls"]
    index: list[int] = meta["index"]
    grid_x, grid_y = meta["grid_x"], meta["grid_y"]
    cw, ch = meta["cw"], meta["ch"]
    per_sprite = grid_x * grid_y
    total_cells = len(urls) * per_sprite

    picks = pick_cells(total_cells, len(index), cfg.max_frames)
    if not picks:
        raise FrameError("没有可选的画面帧")

    notes: list[str] = []
    if not index:
        notes.append("接口未提供时间戳，画面只能按先后顺序使用，不能标注具体时刻")
    if len(picks) < cfg.max_frames:
        notes.append(f"该视频只有 {total_cells} 个抽帧格子，少于设定的 {cfg.max_frames}")

    frames: list[Frame] = []
    fetched_sprites: dict[int, Image.Image] = {}
    for n, cell in enumerate(picks):
        cached = cache / f"{cell}.jpg"
        if cached.exists():
            raw = cached.read_bytes()
        else:
            sprite_i = cell // per_sprite
            if sprite_i not in fetched_sprites:
                notify("fetching_frames", f"下载抽帧拼图 {sprite_i + 1}/{len(urls)}")
                try:
                    raw_sprite = client.get_bytes(urls[sprite_i])
                except BiliError as exc:
                    raise FrameError(f"抽帧图下载失败：{exc}") from exc
                try:
                    fetched_sprites[sprite_i] = Image.open(
                        io.BytesIO(raw_sprite)
                    ).convert("RGB")
                except (UnidentifiedImageError, OSError) as exc:
                    raise FrameError(f"抽帧图不是有效图片：{exc}") from exc
            box = cell_box(cell, grid_x, grid_y, cw, ch)
            buf = io.BytesIO()
            fetched_sprites[sprite_i].crop(box).save(
                buf, "JPEG", quality=cfg.quality, optimize=True
            )
            raw = buf.getvalue()
            cached.write_bytes(raw)

        ts = index[cell] if cell < len(index) else None
        frames.append(
            Frame(
                order=n + 1,
                timestamp=ts,
                data_url="data:image/jpeg;base64," + base64.b64encode(raw).decode(),
                size=len(raw),
            )
        )

    notify(
        "fetching_frames",
        f"画面就绪：{len(frames)} 帧 / {sum(f.size for f in frames) / 1024:.0f} KB",
    )
    return FrameSet(
        frames=frames,
        total_cells=total_cells,
        sprites=len(urls),
        has_timestamps=bool(index),
        notes=notes,
    )


def as_content_blocks(frameset: FrameSet) -> list[dict[str, Any]]:
    """转成 OpenAI 兼容接口的 content 数组片段，接在文本后面即可。

    刻意**不**要求模型逐帧描述——实测那样做会烧掉大量输出 token
    （1539 completion tokens 里大半是在描述画面）。画面只该作为参考上下文。
    """
    if not frameset.frames:
        return []
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "—— 视频画面（抽帧）——\n"
                + frameset.timeline_note()
                + "\n这些画面是**参考材料**：只在画面提供了素材里没有的信息时使用它"
                "（例如幻灯片上的标题、图表标注、代码）。"
                "不要逐帧描述画面，不要因为画面好看就多说。"
                "画面看不清的内容一律不要推测。"
            ),
        }
    ]
    for f in frameset.frames:
        blocks.append({"type": "text", "text": f"【{f.label}】"})
        blocks.append({"type": "image_url", "image_url": {"url": f.data_url}})
    return blocks


def clear_cache(db_path: str, aid: int) -> int:
    """删掉某个视频的帧缓存，返回删除的文件数。

    用 rmtree 而不是逐文件 unlink：缓存目录里理论上只有裁好的单格和 meta.json，
    但真出了意外（半途中断留下的临时文件、子目录）时，逐文件删除会留下一个
    删不掉的目录，而"删了但没删干净"比"没删"更难被发现。
    """
    cache = _cache_dir(db_path, aid)
    if not cache.exists():
        return 0
    n = sum(1 for p in cache.rglob("*") if p.is_file())
    shutil.rmtree(cache, ignore_errors=True)
    if cache.exists():
        return 0
    _prune_empty_root(db_path)
    return n


def _prune_empty_root(db_path: str) -> None:
    """frames/ 空了就连它一起收掉，别留一个空壳目录。"""
    root = Path(db_path).parent / "frames"
    try:
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


def purge_stale(db_path: str, max_age_seconds: int = 2 * 3600) -> int:
    """清掉过期未动的帧缓存目录，返回清掉的目录数。

    正常路径下任务一结束就 clear_cache 了，所以这里通常无物可清。它兜的是
    **没走到清理的退出**——进程被杀、崩溃、断电。没有它，一次异常退出就会让
    别人的视频画面永久留在磁盘上，而使用者只会在某天看到一个不认识的目录、
    不知道那是什么、也不知道能不能删。

    按目录 mtime 判断：还在被写的目录 mtime 是新的，不会被误清。
    """
    root = Path(db_path).parent / "frames"
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for d in list(root.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        try:
            if now - d.stat().st_mtime < max_age_seconds:
                continue
        except OSError:
            continue
        if clear_cache(db_path, int(d.name)):
            removed += 1
    _prune_empty_root(db_path)
    return removed


def cache_size(db_path: str) -> int:
    """整个帧缓存占多少字节。"""
    root = Path(db_path).parent / "frames"
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


__all__ = [
    "Frame",
    "FrameError",
    "FrameSet",
    "as_content_blocks",
    "cache_size",
    "cell_box",
    "clear_cache",
    "collect_frames",
    "pick_cells",
    "purge_stale",
]
