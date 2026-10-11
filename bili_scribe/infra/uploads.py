"""上传件的临存区：存进来 → 按 token 取出去 → 用完删掉。

使用者上传的 srt 要跨**两个请求**才能到手：先"解析并回显"（让他当场看出
段数、时间范围、编码对不对），再"提交任务"。中间得有个地方放着它。

## 为什么不用 multipart

FastAPI 的 `UploadFile` 需要额外的 `python-multipart` 依赖。这个项目的
requirements 一直只有 6 项、装完双击就能跑，为一个上传功能加依赖会带来
"升级后还得再装一个包才能用"的摩擦。所以接口收的是**原始字节**
（`application/octet-stream`），前端用 `file.arrayBuffer()` 直接读——
顺带还省掉了服务端的 multipart 解析，字节流拿来就能直接做编码探测。

## 安全

- token 一律先按字符集校验（URL-safe）才拼进路径，杜绝 `../` 这类路径穿越
- 落盘文件名**不使用**使用者给的原始文件名——它只用于界面回显，不参与拼路径
- 大小上限在接口层挡（见 MAX_BYTES）

## 生命周期

任务结束时删（连同失败的任务），另有启动时与每个任务开始时的兜底清理，
收走"传了文件但没提交任务"留下的那些。
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

# 5 MB。实测参考：1 小时视频约 0.12 MB、4 小时约 0.47 MB（每条字幕约 22 字），
# 所以这个上限对真实字幕留了十倍以上的余量，同时能挡住"选错文件"
# （比如误传了一个视频或压缩包）。
#
# 上限处解析约 0.5–0.8 秒（5 万条量级），所以接口里必须把它丢进线程池，
# 不能让它跑在事件循环上——见 app/main.py 的 api_parse_subtitle。
MAX_BYTES = 5 * 1024 * 1024

# URL-safe base64 的字符集。长度上界只是防御性的——我们自己只生成 24 个字符。
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def new_token() -> str:
    return secrets.token_urlsafe(18)


def _root(db_path: str) -> Path:
    return Path(db_path).parent / "uploads"


def _path(db_path: str, token: str) -> Path | None:
    """token → 落盘路径。**不合法一律返回 None**，绝不把外部字符串直接拼进路径。"""
    if not _TOKEN_RE.fullmatch(token or ""):
        return None
    return _root(db_path) / f"{token}.srt"


def save(db_path: str, token: str, raw: bytes) -> Path:
    target = _path(db_path, token)
    if target is None:
        raise ValueError("上传 token 不合法")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)  # 二进制写，不会有换行翻译
    return target


def exists(db_path: str, token: str) -> bool:
    """只问在不在，不读内容——提交任务前的校验用这个，省一次整文件 IO。"""
    target = _path(db_path, token)
    return target is not None and target.exists()


def load(db_path: str, token: str) -> bytes | None:
    """取回上传内容。过期、被清除、token 不合法都返回 None。"""
    target = _path(db_path, token)
    if target is None or not target.exists():
        return None
    try:
        return target.read_bytes()
    except OSError:
        return None


def discard(db_path: str, token: str) -> int:
    """删掉一个上传件，返回删除的文件数（0 或 1）。"""
    target = _path(db_path, token)
    if target is None or not target.exists():
        return 0
    try:
        target.unlink()
    except OSError:
        return 0
    _prune_empty_root(db_path)
    return 1


def _prune_empty_root(db_path: str) -> None:
    root = _root(db_path)
    try:
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


def purge_stale(db_path: str, max_age_seconds: int = 2 * 3600) -> int:
    """清掉过期未动的上传件，返回清掉的文件数。

    兜的是"传了文件却没提交任务"——使用者选了文件、看了一眼回执、然后就走了。
    没有它，那些文件会一直躺在磁盘上。按 mtime 判断，刚存的不会被误清。
    """
    root = _root(db_path)
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for item in list(root.glob("*.srt")):
        try:
            if now - item.stat().st_mtime < max_age_seconds:
                continue
            item.unlink()
            removed += 1
        except OSError:
            continue
    _prune_empty_root(db_path)
    return removed


def total_size(db_path: str) -> int:
    root = _root(db_path)
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.glob("*.srt") if p.is_file())


__all__ = [
    "MAX_BYTES",
    "discard",
    "exists",
    "load",
    "new_token",
    "purge_stale",
    "save",
    "total_size",
]
