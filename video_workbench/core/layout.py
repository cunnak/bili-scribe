"""目录模板与侧车文件（方案书 5.3.3）。

模板只有一层可变结构，但三个边界最容易出错，都在这一个文件里收口：

1. **省略空段**：没有合集就不该出现 ``/合集名/`` 这种空气层；单稿不该出现 ``P01_``。
   所以路径是"逐段拼"，不是拿一个 format 模板硬套——空段的 format 会留下 ``//``。
2. **重名不覆盖**：同一个 UP 可能有两个同名视频（改标题后重发很常见）。撞名时追加
   `` (2)``，但**同一个 bvid+cid 再来一次要复用原目录**，否则续传会变成"每次都新下载"。
3. **侧车即真相**：``source.wb.json`` 记下 bvid/cid/画质/来源，重启后没有它就只能靠
   目录名猜项目身份。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..infra.files import is_within, sanitize_name, unique_path

SIDECAR = "source.wb.json"

# 一个项目目录里固定会出现的子目录（方案书 5.3.3 的目录树）。
SUBDIRS = ("frames", "clips", "export", "logs")

# 侧车文件里必须有这些键，少一个就不算完整（P1-03 验收项）。
SIDECAR_KEYS = (
    "bvid", "aid", "cid", "page", "title", "up", "collection",
    "quality_requested", "quality_actual", "url", "downloaded_at", "tool_version",
)


@dataclass(frozen=True)
class Layout:
    """一个项目的目录与关键文件。全部是绝对路径，不会再被 cwd 影响。"""

    root: Path

    @property
    def source(self) -> Path:
        return self.root / "source.mp4"

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
        return {
            "root": str(self.root),
            "source": str(self.source),
            "sidecar": str(self.sidecar),
            "audio": str(self.audio),
            "transcript_json": str(self.transcript_json),
            "transcript_srt": str(self.transcript_srt),
            "frames": str(self.frames),
            "clips": str(self.clips),
            "export": str(self.export),
            "logs": str(self.logs),
        }


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
