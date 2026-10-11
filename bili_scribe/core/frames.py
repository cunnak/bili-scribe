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

## 缓存的生死：谁建谁删

每次采集都在 `frames/<aid>-<随机>` 下建一个**独占**目录，采完（无论成败）立刻删掉。
画面在返回前已经进了内存（`Frame.data_url`），所以那些文件从那一刻起就没用了。

为什么不按 aid 复用同一个目录：那样两次采集会共用它，而清理的人并不知道自己删的
可能是别人正在写的目录。实测就是这么翻车的——迭代开始采集、主任务姗姗来迟的收尾
把它整个删掉，于是迭代写到第 2 帧时 FileNotFoundError，一整轮白跑（复现率约 1/6，
脚本见项目根的 `refine-debug-race.py`）。改成独占目录后，这个竞争是**从构造上**
消失的。`purge_stale` 仍然兜着"进程被杀／断电"留下的残骸。

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
import secrets
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


def _frames_root(db_path: str) -> Path:
    """帧缓存放在数据库旁边，跟着 storage 配置走。"""
    return Path(db_path).parent / "frames"


def _new_cache_dir(db_path: str, aid: int) -> Path:
    """给**这一次采集**建一个独占的缓存目录：`frames/<aid>-<随机>`。

    ## 为什么不是 `frames/<aid>`（这是踩出来的）

    原来是按 aid 命名的，于是「同一个视频的两次采集」共用同一个目录；而清理又是
    「任务结束时把这个 aid 的目录删掉」。两者一撞就是数据竞争：

        主任务 t+2.43s 采集完成、写库说 ready
                  t+2.66s 迭代开始采集，mkdir 并把 0.jpg 写进去
                  t+3.00s 主任务的 finally 才走到 clear_cache → 整个目录被删掉
                  → 迭代写到后面某一帧时 FileNotFoundError，这一轮白跑

    实测复现率约 1/6（脚本见项目根的 refine-debug-race.py），而且它长得非常像
    "偶发网络问题"。那次追到的间隙约 0.5 秒，**成因至今没定位**（已排除提交路径：
    整次 record_version 只有 5–7ms）——而这正是不该靠"把清理提前/延后"去修的理由：
    那是拿一个不理解的时序去赌。改成每次采集一个独占目录之后，"谁建谁删"成立，
    竞争从**构造上**消失，与那段间隙多长、多频繁都无关。
    """
    return _frames_root(db_path) / f"{aid}-{secrets.token_hex(4)}"


def _remove_dir(path: Path) -> int:
    """删掉一个缓存目录，返回删除的文件数。

    用 rmtree 而不是逐文件 unlink：真出了意外（半途中断留下的临时文件、子目录）时，
    逐文件删除会留下一个删不掉的目录，而"删了但没删干净"比"没删"更难被发现。
    """
    if not path.exists():
        return 0
    n = sum(1 for p in path.rglob("*") if p.is_file())
    shutil.rmtree(path, ignore_errors=True)
    return 0 if path.exists() else n


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

    cache = _new_cache_dir(db_path, aid)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        return _crop_frames(client, cfg, aid, cache, notify)
    finally:
        # **谁建谁删，就在这一个函数里。** 画面此时已经在内存里（Frame.data_url），
        # 所以文件从这一刻起就没用了——收得越早，"第三方内容留在磁盘上"的窗口越小。
        # 失败路径也走这里：接口报错、图片不是图片、写盘失败，一条都不会漏。
        _remove_dir(cache)
        _prune_empty_root(db_path)


def _crop_frames(client, cfg: FramesConfig, aid: int, cache: Path, notify) -> "FrameSet":
    """裁帧的主体。**只管用 cache，不管它的生死**——创建与删除都在 collect_frames 里。

    单独拆出来，是为了让"谁建谁删"一眼可见：主体里有十几处可能中途失败的地方，
    清理必须由一个 finally 包住整体，否则总有一条路径会漏。
    """
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
    """删掉某个视频留下的帧缓存，返回删除的文件数。

    现在**正常路径用不到它了**（采集自己收尾，见 collect_frames），留着是因为它
    表达的是一句仍然成立的话：「把这个视频的画面从磁盘上抹掉」。异常退出留下的
    残留、或者将来某个不自己收尾的调用方，都还需要它。
    """
    removed = 0
    for path in _frames_root(db_path).glob(f"{aid}-*"):
        removed += _remove_dir(path)
    if removed:
        _prune_empty_root(db_path)
    return removed


def _prune_empty_root(db_path: str) -> None:
    """frames/ 空了就连它一起收掉，别留一个空壳目录。

    只在"确认已经空了"时才 rmdir，所以别人刚建好的目录不会被它带走：
    那会让 root 非空，直接跳过。
    """
    root = _frames_root(db_path)
    try:
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


def _aid_of(dir_name: str) -> int | None:
    """缓存目录名 → aid。名字形如 `116630440972726-1a2b3c4d`。

    解析不出来就返回 None：**不是我们建的目录，一律不碰**。
    """
    head = dir_name.partition("-")[0]
    return int(head) if head.isdigit() else None


def purge_stale(db_path: str, max_age_seconds: int = 2 * 3600) -> int:
    """清掉过期未动的帧缓存目录，返回清掉的目录数。

    正常路径下采集自己就收干净了（见 collect_frames），所以这里通常无物可清。
    它兜的是**没走到清理的退出**——进程被杀、崩溃、断电。没有它，一次异常退出
    就会让别人的视频画面永久留在磁盘上，而使用者只会在某天看到一个不认识的
    目录、不知道那是什么、也不知道能不能删。

    按目录 mtime 判断：还在被写的目录 mtime 是新的，不会被误清。
    """
    root = _frames_root(db_path)
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for d in list(root.iterdir()):
        if not d.is_dir() or _aid_of(d.name) is None:
            continue
        try:
            if now - d.stat().st_mtime < max_age_seconds:
                continue
        except OSError:
            continue
        if _remove_dir(d):
            removed += 1
    _prune_empty_root(db_path)
    return removed


def cache_size(db_path: str) -> int:
    """整个帧缓存占多少字节。

    逐个文件 stat 时容错：采集是并发的，目录可能在遍历途中就没了
    （正常收尾），这时按"没有这个文件"算，不该让一个诊断函数崩掉。
    """
    root = _frames_root(db_path)
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


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
