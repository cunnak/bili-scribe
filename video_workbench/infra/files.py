r"""路径与文件名安全（方案书 5.3.3）。

三条规则各自对应一类真实事故：

1. **非法字符**。Windows 上 ``<>:"/\|?*`` 会让 ``open()`` 抛 OSError，
   而标题里出现 ``?`` ``|`` 在 B站 视频标题里很常见——不处理就是"下载到一半崩"。
2. **末尾空格与点**。Win32 会在创建时静默把这些字符吃掉，于是数据库里记的路径
   和磁盘上的实际路径不一致，下次按 DB 找文件就找不到。
3. **越界**。所有由前端传来的相对路径都必须落在配置的根目录下，
   否则一个 ``../../`` 就能把导出写到任意位置。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from video_workbench.app.errors import WBError

# Win32 文件名禁用字符。控制字符另算（\x00-\x1f）。
_INVALID = set('<>:"/\\|?*')

# 设备名不能作为文件名主干，即使带扩展名也不行（CON.txt 同样打不开）。
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_SEGMENT = 120


def sanitize_name(name: str, max_len: int = MAX_SEGMENT) -> str:
    """把任意标题变成可用的单级文件名。**空结果返回 ``untitled``**。

    这里不抛异常：标题来自外部（B站 / 用户输入），清洗失败就让整个下载任务失败
    是不合理的。宁可得到一个 ``untitled``，也不要让任务卡在一个无法满足的条件上。
    """
    text = str(name or "")
    text = "".join(ch for ch in text if ch not in _INVALID and ord(ch) >= 32)
    # 连续空白压成一个：既避免超长，也避免 "a    b" 这类看着像路径分隔的目录名
    text = re.sub(r"\s+", " ", text).strip()
    # 首尾的点在这里不是装饰：Win32 会把它们吃掉，导致 DB 与磁盘不一致
    text = text.strip(" .")
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    if not text:
        return "untitled"
    if text.split(".", 1)[0].upper() in _RESERVED:
        text = "_" + text
    return text


def sanitize_relpath(rel: str, max_len: int = MAX_SEGMENT) -> str:
    """逐段清洗相对路径，保留层级。空段（模板里没填的占位符）自动丢弃。"""
    parts = [p for p in re.split(r"[\\/]+", str(rel or "")) if p not in ("", ".")]
    return os.sep.join(sanitize_name(p, max_len) for p in parts)


def resolve_under(root: str | os.PathLike, rel: str) -> Path:
    """把前端传来的相对路径解析到 ``root`` 之下，越界即拒绝。

    这是 **RPC 边界上的唯一入口**：任何接收路径的接口都必须经过它，
    否则 ``D:/export/../..`` 这类输入会把文件写到工作区外面。
    """
    text = str(rel or "").strip()
    if not text:
        raise WBError("VALIDATION", "路径不能为空")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or candidate.anchor:
        raise WBError("VALIDATION", f"必须是相对路径：{text}")
    parts = [p for p in re.split(r"[\\/]+", text) if p not in ("", ".")]
    if not parts:
        raise WBError("VALIDATION", f"路径无效：{text}")
    if any(p == ".." for p in parts):
        raise WBError("VALIDATION", f"路径不允许包含 ..：{text}")

    base = Path(root)
    target = base.joinpath(*parts)
    # 二次确认：normpath 之后再比前缀，防止 "a/./b" / 大小写 / 短名等绕法。
    base_n = os.path.normcase(os.path.abspath(base))
    target_n = os.path.normcase(os.path.abspath(target))
    if target_n != base_n and not target_n.startswith(base_n + os.sep):
        raise WBError("VALIDATION", f"路径越界：{text}")
    return target


def is_within(root: str | os.PathLike, target: str | os.PathLike) -> bool:
    base_n = os.path.normcase(os.path.abspath(root))
    target_n = os.path.normcase(os.path.abspath(target))
    return target_n == base_n or target_n.startswith(base_n + os.sep)


def unique_path(path: str | os.PathLike) -> Path:
    """已存在时追加 `` (2)`` `` (3)``……而不是报错。

    导出同名文件是常态（同一个切片重导两次），报错会让人以为功能坏了。
    """
    target = Path(path)
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise WBError("CONFLICT", f"同名文件过多：{target.name}")


def atomic_write_text(path: str | os.PathLike, text: str, encoding: str = "utf-8") -> Path:
    """同目录临时文件 + ``os.replace``：中途失败不会把目标截成半截。

    临时文件必须和目标**同目录**——跨盘 replace 会退化成复制再删除，失去原子性。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        # 失败时留下 .tmp 会污染导出目录，主动清掉
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def human_bytes(size: int | float | None) -> str:
    """给界面用的体积文本。``None`` 与 0 都显示 ``—``：没探测到就别假装是 0 B。"""
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover


def ensure_free_space(path: str | os.PathLike, need_bytes: int) -> dict[str, int]:
    """导出/下载前的磁盘守卫（P1-06）。不足就抛 ``DISK_FULL``。

    返回 ``{free, need}``，调用方可以把这两个数直接显示在提示里——只说"空间不足"
    而不说差多少，用户没法判断该清哪块盘。
    """
    target = Path(path)
    probe = target if target.is_dir() or not target.suffix else target.parent
    while not probe.exists() and probe.parent != probe:  # 目录还没建出来时往上找
        probe = probe.parent
    usage = shutil.disk_usage(str(probe))
    need = max(0, int(need_bytes or 0))
    if usage.free < need:
        raise WBError(
            "DISK_FULL",
            f"磁盘空间不足：需要 {human_bytes(need)}，可用 {human_bytes(usage.free)}",
            detail=str(probe),
        )
    return {"free": int(usage.free), "need": need}


def disk_usage(path: str | os.PathLike) -> dict[str, int] | None:
    """素材盘还剩多少——给状态条那一格用（它今天恒显示「磁盘 —」）。

    与 ensure_free_space 共用同一套「目录还没建出来就往上找已存在祖先」的探测
    逻辑，但语义完全不同：那条是写盘前的守卫，不足就抛 DISK_FULL；这条只读数，
    任何探测失败一律返回 None。

    为什么不抛：状态条是首屏的一部分，盘符拔了、网络盘断线、目录权限被改，
    都不该让整个 app.info 挂掉——那样界面连「哪里出错」都显示不出来。
    None 在界面上画成「磁盘 ?」，用户看得出是「读不到」而不是「没空间」。
    """
    try:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        usage = shutil.disk_usage(str(probe))
    except (OSError, ValueError):
        # OSError 覆盖 PermissionError / FileNotFoundError / 盘符不存在；
        # ValueError 是 Windows 上非法盘符会抛的那种。
        return None
    return {"free_bytes": int(usage.free), "total_bytes": int(usage.total)}


def has_free_space(path: str | os.PathLike, need_bytes: int) -> bool:
    try:
        ensure_free_space(path, need_bytes)
        return True
    except Exception:  # noqa: BLE001
        return False
