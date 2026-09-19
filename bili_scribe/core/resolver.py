"""把用户随手粘进来的东西，归拢成一件事：哪个稿件的第几个分P。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..infra.bili_client import BiliClient, BiliError
from ..infra.endpoints import VIDEO_VIEW

# BV 号本体是大小写敏感的，但前缀用户经常打成小写 bv
_BV_PATTERN = re.compile(r"[Bb][Vv]([0-9A-Za-z]{10})")
_AV_PATTERN = re.compile(r"[Aa][Vv](\d+)")
_SHORT_LINK_PATTERN = re.compile(r"^https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
_PAGE_PATTERN = re.compile(r"[?&]p=(\d+)", re.IGNORECASE)


class ResolveError(ValueError):
    """输入无法解析成一个明确的稿件。"""


@dataclass
class Target:
    bvid: str
    page: int = 1
    raw: str = ""
    aid: int | None = None

    @property
    def url(self) -> str:
        suffix = f"?p={self.page}" if self.page > 1 else ""
        return f"https://www.bilibili.com/video/{self.bvid}/{suffix}"


def resolve(raw: str, client: BiliClient) -> Target:
    text = (raw or "").strip()
    if not text:
        raise ResolveError("请输入视频链接或 BV 号")

    page = 1
    page_match = _PAGE_PATTERN.search(text)
    if page_match:
        page = max(1, int(page_match.group(1)))

    if _SHORT_LINK_PATTERN.match(text):
        try:
            text = client.expand_short_link(text)
        except BiliError as exc:
            raise ResolveError(f"b23.tv 短链展开失败：{exc}") from exc

    bv_match = _BV_PATTERN.search(text)
    if bv_match:
        return Target(bvid="BV" + bv_match.group(1), page=page, raw=raw)

    av_match = _AV_PATTERN.search(text)
    if av_match:
        aid = int(av_match.group(1))
        try:
            view = client.get_data(VIDEO_VIEW, {"aid": aid})
        except BiliError as exc:
            raise ResolveError(f"av{aid} 查询失败：{exc}") from exc
        bvid = (view or {}).get("bvid")
        if not bvid:
            raise ResolveError(f"av{aid} 没有返回对应的 bvid")
        return Target(bvid=bvid, page=page, raw=raw, aid=aid)

    raise ResolveError(
        "没认出 BV 号。可以粘贴完整链接、b23.tv 短链、BV 号或 av 号"
    )


def format_duration(seconds: int | float) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return "未知"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
