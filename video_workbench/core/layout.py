"""目录模板与侧车文件（方案书 5.3.3）。

模板只有一层可变结构，但三个边界最容易出错，都在这一个文件里收口：

1. **省略空段**：没有合集就不该出现 ``/合集名/`` 这种空气层；单稿不该出现 ``P01_``。
   所以路径是"逐段拼"，不是拿一个 format 模板硬套——空段的 format 会留下 ``//``。
2. **重名不覆盖**：同一个 UP 可能有两个同名视频（改标题后重发很常见）。撞名时追加
   `` (2)``，但**同一个 bvid+cid 再来一次要复用原目录**，否则续传会变成"每次都新下载"。
3. **侧车即真相**：``source.wb.json`` 记下 bvid/cid/画质/来源，重启后没有它就只能靠
   目录名猜项目身份。

**三个命名域是故意的不对称，别去"统一"它：**

- **媒体文件名 = 内容**：跟着视频原标题走（``source_stem`` → ``finalize_source``）。
  用户在资源管理器里看到的就该是他认得出的东西，不该是一堆 ``source.mp4``。
- **侧车文件名 = 格式锚点**：固定 ``source.wb.json``。``allocate`` 要在**还不知道媒体
  文件名**的时候判断"这个目录是不是同一集"，靠的就是这个可预测的名字。改它会让旧目录的
  复用判定失效，重下同集反而新建一个重复项目目录——数据分裂比名字不一致严重得多。
- **下载暂存名 = 机器名**：固定 ``source``（见 ``core/downloader.py`` 的 ``-o``）。
  断点续传的 ``.part`` 名必须稳定，否则 UP 改一次标题续传就写到另一个文件上；而且标题
  一旦进 yt-dlp 的输出模板，就是拿外部可控字符串当模板片段用（``%(id)s`` 会被展开）。
  下载结束后由 ``finalize_source`` 一次性改成媒体文件名——**改名只有这一个归属**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..infra.files import is_within, sanitize_name, unique_path

SIDECAR = "source.wb.json"

# 下载器写盘用的暂存主干（``-o "source.%(ext)s"``）。**不是**最终文件名，别拿它拼路径。
SOURCE_STAGING = "source"

# 认作"这一集的视频文件"的扩展名。读侧兜底只按这份名单挑，不猜别的。
SOURCE_EXTS = (".mp4", ".mkv", ".flv", ".webm", ".ts", ".m4v")

# 一个项目目录里固定会出现的子目录（方案书 5.3.3 的目录树）。
SUBDIRS = ("frames", "clips", "export", "logs")

# 侧车文件里必须有这些键，少一个就不算完整（P1-03 验收项）。
SIDECAR_KEYS = (
    "bvid",
    "aid",
    "cid",
    "page",
    "title",
    "up",
    "collection",
    "quality_requested",
    "quality_actual",
    "url",
    "downloaded_at",
    "tool_version",
)


@dataclass(frozen=True)
class Layout:
    """一个项目的目录与关键文件。全部是绝对路径，不会再被 cwd 影响。"""

    root: Path

    @property
    def sidecar(self) -> Path:
        return self.root / SIDECAR

    @property
    def audio(self) -> Path:
        return self.root / "audio.wav"

    @property
    def transcript_json(self) -> Path:
        return self.root / "transcript.json"

    @property
    def transcript_srt(self) -> Path:
        return self.root / "transcript.srt"

    @property
    def subtitle_zh_srt(self) -> Path:
        return self.root / "subtitle.zh.srt"

    @property
    def subtitle_zh_ass(self) -> Path:
        return self.root / "subtitle.zh.ass"

    @property
    def subtitle_bilingual_ass(self) -> Path:
        return self.root / "subtitle.bilingual.ass"

    @property
    def summary_md(self) -> Path:
        return self.root / "summary.md"

    @property
    def summary_json(self) -> Path:
        return self.root / "summary.json"

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def clips(self) -> Path:
        return self.root / "clips"

    @property
    def export(self) -> Path:
        return self.root / "export"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def lock(self) -> Path:
        return self.root / ".lock"

    def ensure(self) -> "Layout":
        self.root.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRS:
            (self.root / name).mkdir(exist_ok=True)
        return self

    def rel(self, path: str | Path, root: str | Path) -> str:
        """把绝对路径转成相对 media_root 的字符串，写进数据库用。"""
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()

    def as_dict(self) -> dict[str, str]:
        # 故意不含 source：媒体文件名跟着标题走，只有 find_source/DB 知道它在哪。
        return {
            "root": str(self.root),
            "sidecar": str(self.sidecar),
            "audio": str(self.audio),
            "transcript_json": str(self.transcript_json),
            "transcript_srt": str(self.transcript_srt),
            "frames": str(self.frames),
            "clips": str(self.clips),
            "export": str(self.export),
            "logs": str(self.logs),
        }


def source_stem(title: str, part_title: str = "") -> str:
    """这一集的视频文件名主干（不含扩展名）。**命名规则只有这一处**。

    多 P 用 ``主标题 - 分P名``，和 :meth:`core.resolver.Item.display_title`（界面列表
    显示的那串）保持同一条规则——用户在界面上看到什么，资源管理器里就该是什么。
    清洗交给 :func:`video_workbench.infra.files.sanitize_name`：非法字符、首尾点、
    Win32 保留字、超长都在这里收口，空标题退化成 ``untitled`` 而不是抛异常。
    """
    text = str(title or "").strip()
    part = str(part_title or "").strip()
    if part and text and part != text:
        text = f"{text} - {part}"
    elif part and not text:
        # 没有主标题时只留分P名，不要留一个开头孤零零的 " - "。
        text = part
    return sanitize_name(text)


def finalize_source(root: str | Path, produced: str | Path, *, stem: str) -> Path:
    """把下载产物改成正式文件名 ``<stem><原扩展名>``，返回最终路径。

    两个后端（yt-dlp / BBDown）都在这一个函数汇合，保证命名一致。产物不在本目录时
    只如实返回原路径——**绝不跨目录动别人的文件**，路径由调用方写进 DB 与侧车。

    目标名已存在时覆盖它是安全的：能进到同一个项目目录，说明 :func:`allocate` 已经用
    侧车里的 bvid+cid 认定过"这就是这一集"，那个同名文件是上一次尝试留下的残留。
    清洗后的标题为空会退化成 ``untitled``，不会出现空文件名。
    """
    home = Path(str(root))
    src = Path(str(produced))
    if not src.is_file() or src.parent != home:
        return src
    target = home / f"{stem}{src.suffix.lower()}"
    if target == src:
        return target
    try:
        if target.exists():
            target.unlink()
        src.replace(target)
    except OSError:
        # 改名失败不判下载失败：文件确实在盘上，DB 记真实路径就行（读侧走 DB 优先）。
        return src
    return target


def find_source(
    root: str | Path,
    *,
    prefer: str | Path = "",
    stem: str = "",
) -> Path | None:
    """读侧唯一入口：这一集的视频文件在哪。**兜底 glob 只允许写在这里**。

    顺序：调用方给的权威路径（DB / 侧车）→ 标题名 → 历史遗留的 ``source.*`` →
    目录里**唯一**那一个视频文件。找不到、或者同一种命名下有多个候选，一律返回
    ``None`` 让上层重下——"挑最大的那个"或"挑最新的那个"这类猜法在文件名跟标题走之后
    会从兜底变成主路径，有把 B 视频记到 A 项目名下且不报错的风险。

    最后那条"唯一候选"不算猜：目录里只有一个视频文件时没有歧义，它覆盖的是"DB 路径失效
    但文件还在"的现场（用户手动挪动过、或标题改过之后重算的 stem 对不上）。

    旧目录（只有 ``source.mp4``）在这里天然兼容：不改名、不迁移，读得到就是读到。
    """
    given = str(prefer or "")
    if given:
        path = Path(given)
        if path.is_file():
            return path
    home = Path(str(root))
    if not home.is_dir():
        return None
    hits = [
        item
        for item in sorted(home.iterdir())
        if item.is_file() and item.suffix.lower() in SOURCE_EXTS and not item.name.startswith(".")
    ]
    if stem:
        exact = [item for item in hits if item.stem == stem]
        if len(exact) == 1:
            return exact[0]
    legacy = [item for item in hits if item.stem == SOURCE_STAGING]
    if len(legacy) == 1:
        return legacy[0]
    return hits[0] if len(hits) == 1 else None


def _segments(
    up_name: str,
    title: str,
    collection_title: str = "",
    part_index: int | None = None,
    part_title: str = "",
) -> list[str]:
    """按"空段省略"的规则拼出相对路径的各段。"""
    segs: list[str] = []
    up = sanitize_name(up_name) if up_name else ""
    segs.append(up or "未命名UP")
    coll = sanitize_name(collection_title) if collection_title else ""
    if coll:
        segs.append(coll)
    segs.append(sanitize_name(title) or "未命名视频")
    if part_index is not None:
        # 分P 用两位序号，超过 99 P 也还能正确排序（B站 上限就是 100）。
        label = f"P{part_index:02d}" if part_index < 100 else f"P{part_index}"
        name = sanitize_name(part_title) if part_title else ""
        segs.append(f"{label}_{name}" if name else label)
    return segs


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _same_item(sidecar: dict[str, Any], bvid: str, cid: int | None) -> bool:
    """目录是不是就是这一集：bvid 相同且（有 cid 时）cid 也相同。"""
    if not bvid or sidecar.get("bvid") != bvid:
        return False
    if cid is None:
        return True
    return sidecar.get("cid") in (None, cid)


def base_root(
    media_root: str | Path,
    *,
    up_name: str = "",
    title: str = "",
    collection_title: str = "",
    part_index: int | None = None,
    part_title: str = "",
) -> Path:
    """按命名规则算出这一集**本来**该落在哪，不看不避让、不追加序号。

    存在的理由只有一个：轻量总结（只给一份 srt）没有 bvid 可去重，目录就是它的
    身份。要在 allocate 之前先问一句「这个目录是不是已经有项目了」，就必须能拿到
    **没被避让逻辑改过**的那个路径——allocate 一旦发现目录存在会直接给出 " (2)"，
    拿它的返回值去查库，永远查不到上一行，同一个文件选两次就会长出两个项目。

    命名规则仍然只有 _segments 一处，这里只是把入口暴露出来，不另立一套。
    """
    root = Path(media_root)
    return root.joinpath(*_segments(up_name, title, collection_title, part_index, part_title))


def allocate(
    media_root: str | Path,
    *,
    up_name: str = "",
    title: str = "",
    collection_title: str = "",
    part_index: int | None = None,
    part_title: str = "",
    bvid: str = "",
    cid: int | None = None,
) -> Layout:
    """算出这一集该落在哪，必要时追加 `` (2)``。

    **什么时候复用**：目标目录已存在，且里面的 ``source.wb.json`` 就是同一个 bvid+cid
    ——那就是上次下的，续传要接着写。**什么时候避让**：目录存在但不是同一集（或者干脆
    没有侧车），说明是"同名不同内容"，追加序号，绝不覆盖别人的文件。
    """
    root = Path(media_root)
    target = root.joinpath(*_segments(up_name, title, collection_title, part_index, part_title))

    if not is_within(root, target):  # 理论上不会发生，但清洗逻辑一旦改动就该在这里拦住
        raise ValueError(f"目标路径越界：{target}")

    if not target.exists():
        return Layout(target)

    sidecar = _read_sidecar(target / SIDECAR)
    if _same_item(sidecar, bvid, cid):
        return Layout(target)

    # unique_path 处理" (2) (3) ..."的递增，但复用判断还要再看一眼——
    # 已经存在 " (2)" 而且正是这一集时，应该写回那一份。
    candidate = unique_path(target)
    sidecar2 = _read_sidecar(candidate / SIDECAR)
    if _same_item(sidecar2, bvid, cid):
        return Layout(candidate)
    return Layout(candidate)


def build_sidecar(
    *,
    bvid: str = "",
    aid: int | None = None,
    cid: int | None = None,
    page: int = 1,
    title: str = "",
    up: str = "",
    collection: str = "",
    quality_requested: str = "",
    quality_actual: str = "",
    url: str = "",
    downloaded_at: str = "",
    tool_version: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """拼出 ``source.wb.json`` 的内容。字段名与方案书 5.3.3 一一对应。"""
    data: dict[str, Any] = {
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "page": page,
        "title": title,
        "up": up,
        "collection": collection,
        "quality_requested": quality_requested,
        "quality_actual": quality_actual,
        "url": url,
        "downloaded_at": downloaded_at,
        "tool_version": tool_version,
    }
    if extra:
        data.update(extra)
    return data


def write_sidecar(layout: Layout, data: dict[str, Any]) -> Path:
    """写侧车。缺字段在这里就暴露，不要等到读的时候才发现。"""
    missing = [k for k in SIDECAR_KEYS if k not in data]
    if missing:
        raise ValueError(f"侧车缺字段：{', '.join(missing)}")
    layout.root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    layout.sidecar.write_text(text, encoding="utf-8")
    return layout.sidecar


def read_sidecar(path: str | Path) -> dict[str, Any]:
    return _read_sidecar(Path(path))
