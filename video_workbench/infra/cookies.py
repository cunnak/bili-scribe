"""Cookie 导出：把 bili-scribe 的登录态翻译成 yt-dlp 认识的 Netscape 文件。

**为什么要转一道。** B站的高清/大会员线路必须带登录 Cookie，而 yt-dlp 只认
``--cookies cookies.txt``（Netscape 格式）。我们复用的是 bili-scribe 的加密凭据库，
不能把 ``key.bin`` 直接喂给 yt-dlp——那是自家格式，喂过去等于没登录。

**为什么写到缓存目录而不是数据目录。** 这是从凭据库里**解出来的明文**，
放在 ``cache_root/cookies.bili.txt`` 里，过期就删；不放进 ``data/``，
避免用户把整个 data 目录拷给别人时顺手把 Cookie 也送出去。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# 由 VideoWorkbench 从 bili-scribe 登录态导出，仅供 yt-dlp 使用\n"
    "# 明文 Cookie，用完请删；不要提交到版本库\n"
)

DEFAULT_DOMAIN = ".bilibili.com"

# 只导 B站相关的键。凭据库里可能还有别的站（历史原因），一股脑写进 cookies.txt
# 会让 yt-dlp 把它们发给 bilibili.com——不该发的数据不发。
BILI_COOKIE_KEYS = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
    "sid",
)


def to_netscape(
    cookies: Mapping[str, Any],
    domain: str = DEFAULT_DOMAIN,
    expires: int = 0,
) -> str:
    """把 ``{name: value}`` 拼成 Netscape 行。值里的制表符会被替换——它会把行结构撑坏。"""
    lines: list[str] = []
    for name, value in cookies.items():
        if value is None:
            continue
        text = str(value).replace("\t", " ").replace("\n", " ").strip()
        if not text:
            continue
        # 域以 . 开头说明是通配域，第二列必须是 TRUE，否则 yt-dlp 只在精确域上发
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        lines.append("\t".join([domain, include_sub, "/", "TRUE", str(expires), name, text]))
    if not lines:
        return ""
    return HEADER + "\n".join(lines) + "\n"


def filter_bili(cookies: Mapping[str, Any]) -> dict[str, Any]:
    """挑出 B站需要的键；一个都没有就返回空（调用方据此判断"没登录"）。"""
    return {k: v for k, v in cookies.items() if k in BILI_COOKIE_KEYS and v}


def write_netscape(
    path: str | Path, cookies: Mapping[str, Any], domain: str = DEFAULT_DOMAIN
) -> Path:
    """写文件。权限收到 0600——除了当前用户不该有人读得到。"""
    target = Path(path)
    body = to_netscape(cookies, domain=domain)
    if not body:
        raise ValueError("没有可导出的 Cookie")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 只改只读位
        pass
    return target


def export_from_client(client: Any, path: str | Path) -> Path | None:
    """从 ``BiliClient`` 取 Cookie 并落盘。未登录或取不到就返回 None（不抛）。"""
    try:
        cookies = getattr(client, "cookies", None)
        if callable(cookies):
            cookies = cookies()
    except Exception:  # noqa: BLE001 —— 凭据坏了不能拦住整个下载
        return None
    if not isinstance(cookies, Mapping):
        return None
    picked = filter_bili(cookies)
    if not picked:
        return None
    try:
        return write_netscape(path, picked)
    except (OSError, ValueError):
        return None


def is_stale(path: str | Path, max_age_s: float = 6 * 3600) -> bool:
    """Cookie 放了太久就该重新导：B站的 SESSDATA 会滚，旧的可能已经失效。"""
    target = Path(path)
    if not target.exists():
        return True
    try:
        return (time.time() - target.stat().st_mtime) > max_age_s
    except OSError:
        return True


def remove(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def purge_stale(paths: Iterable[str | Path]) -> None:
    for item in paths:
        remove(item)
