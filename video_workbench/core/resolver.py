"""把一个链接解析成"要下载哪些东西"。

支持五种标识：``BV`` / ``av`` / ``ep`` / ``ss`` / ``md``，外加 ``b23.tv`` 短链与 ``?p=N``。
输出只有两种形态：

- **一个稿件**（单稿，或它的某一个分P）；
- **一组稿件**（一个稿件多 P、UP 的合集、番剧的一季）。

**为什么不让上层去判断"这是不是合集"。** 判断依据散落在三个接口的不同字段里
（``pages`` / ``ugc_season`` / ``episodes``），把这些差异关在这里，下载器只需要
"给我一串要下的东西"。上层如果自己判断，迟早会出现"点开合集只下了第一集"。

**缓存 10 分钟（方案书 7.1）。** 粘同一个链接、从合集点进分P再返回，都是高频动作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..app.errors import WBError
from .bili import BiliGateway

API = "https://api.bilibili.com"
VIDEO_VIEW = f"{API}/x/web-interface/view"
PGC_SEASON = f"{API}/pgc/view/web/season"

KIND_SINGLE = "single"
KIND_MULTI_P = "multi_p"
KIND_COLLECTION = "collection"
KIND_BANGUMI = "bangumi"

_BV = re.compile(r"[Bb][Vv]([0-9A-Za-z]{10})")
_AV = re.compile(r"(?<![0-9A-Za-z])[Aa][Vv](\d+)")
_EP = re.compile(r"(?<![0-9A-Za-z])[Ee][Pp](\d+)")
_SS = re.compile(r"(?<![0-9A-Za-z])[Ss][Ss](\d+)")
_MD = re.compile(r"(?<![0-9A-Za-z])[Mm][Dd](\d+)")
_SHORT = re.compile(r"^https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
_PAGE = re.compile(r"[?&]p=(\d+)", re.IGNORECASE)
_URL_ID = re.compile(r"/(?:video|bangumi/play|bangumi/media)/([0-9A-Za-z]+)", re.IGNORECASE)


@dataclass
class Item:
    """一个待下载单位。单稿也有且只有一个。"""

    bvid: str = ""
    aid: int | None = None
    cid: int | None = None
    page_no: int = 1
    title: str = ""
    part_title: str = ""
    # 这条自己的分P 数：合集/番剧的每条 BV 通常是 1，于是目录不再挂 P01_ 前缀。
    page_count: int = 1
    duration_ms: int = 0
    cover: str = ""

    @property
    def display_title(self) -> str:
        """给用户看的标题：多P 时带上分P名，单P 不重复。"""
        if self.part_title and self.part_title != self.title:
            return f"{self.title} - {self.part_title}"
        return self.title or self.bvid

    def to_payload(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "aid": self.aid,
            "cid": self.cid,
            "page": self.page_no,
            "title": self.title,
            "part_title": self.part_title,
            "page_count": self.page_count,
            "duration_ms": self.duration_ms,
            "cover": self.cover,
        }


@dataclass
class Resolution:
    kind: str
    title: str
    up_name: str = ""
    up_mid: int | None = None
    cover: str = ""
    collection_title: str = ""  # 合集/番剧名；单稿为空
    season_id: int | None = None
    items: list[Item] = field(default_factory=list)
    note: str = ""
    raw: str = ""

    @property
    def is_batch(self) -> bool:
        return len(self.items) > 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "up_name": self.up_name,
            "up_mid": self.up_mid,
            "cover": self.cover,
            "collection_title": self.collection_title,
            "season_id": self.season_id,
            "note": self.note,
            "raw": self.raw,
            "count": len(self.items),
            "items": [it.to_payload() for it in self.items],
        }


def _ms(seconds: Any) -> int:
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return 0


def _norm(text: str) -> str:
    return (text or "").strip()


def identify(raw: str) -> tuple[str, str, int]:
    """只看字符串就知道这是哪一种标识。

    返回 ``(kind, value, page)``，kind ∈ ``bv/av/ep/ss/md``；认不出来抛 VALIDATION。
    这一步不发请求——用户粘进来的东西十有八九是错的，先在最便宜的地方拒绝。
    """
    text = _norm(raw)
    if not text:
        raise WBError("VALIDATION", "请输入视频链接、BV 号或番剧链接")

    page = 1
    m = _PAGE.search(text)
    if m:
        page = max(1, int(m.group(1)))

    for kind, pattern in (("bv", _BV), ("av", _AV), ("ep", _EP), ("ss", _SS), ("md", _MD)):
        hit = pattern.search(text)
        if hit:
            value = hit.group(1)
            if kind == "bv":
                value = "BV" + value
            return kind, value, page

    # 兜底：从 URL 路径里捞 id（/video/BVxxx、/bangumi/play/ss123、/bangumi/media/md2823）
    hit = _URL_ID.search(text)
    if hit:
        token = hit.group(1)
        for kind, pattern in (("bv", _BV), ("av", _AV), ("ep", _EP), ("ss", _SS), ("md", _MD)):
            if pattern.search(token):
                value = pattern.search(token).group(1)
                return kind, ("BV" + value if kind == "bv" else value), page

    raise WBError(
        "VALIDATION",
        "没认出这是什么：可以粘视频链接、b23.tv 短链、BV/av 号，或番剧的 ep/ss/md 链接",
        detail=text[:200],
    )


def _from_view(view: dict[str, Any], page: int, raw: str) -> Resolution:
    """``x/web-interface/view`` 的返回 → Resolution。

    三种情况在这里分叉：
    - 有 ``ugc_season`` → 合集（可能同时有分P，此时以合集为准，因为用户给的是合集链接）；
    - ``pages`` 多于一条 → 多P；
    - 其余 → 单稿。
    """
    bvid = view.get("bvid") or ""
    aid = view.get("aid")
    title = view.get("title") or bvid
    owner = view.get("owner") or {}
    cover = view.get("pic") or ""
    season = view.get("ugc_season") or {}

    if season and season.get("sections"):
        items: list[Item] = []
        for section in season.get("sections") or []:
            for ep in section.get("episodes") or []:
                arc = ep.get("arc") or {}
                items.append(
                    Item(
                        bvid=ep.get("bvid") or "",
                        aid=ep.get("aid") or arc.get("aid"),
                        cid=ep.get("cid"),
                        page_no=1,
                        # 合集里每条 BV 的标题在 `arc.title`。整批都用父视频标题的话，
                        # 素材库 21 行会同名分不清谁是谁（真链路 BV1VZWc6dECG 撞到的）。
                        title=arc.get("title") or ep.get("title") or title,
                        part_title="",
                        page_count=len(ep.get("pages") or []) or 1,
                        duration_ms=_ms(ep.get("duration") or arc.get("duration")),
                        cover=arc.get("pic") or cover,
                    )
                )
        if items:
            return Resolution(
                kind=KIND_COLLECTION,
                title=title,
                up_name=owner.get("name") or "",
                up_mid=owner.get("mid"),
                cover=cover,
                collection_title=season.get("title") or "",
                items=items,
                raw=raw,
            )

    pages = view.get("pages") or []
    if len(pages) > 1:
        items = [
            Item(
                bvid=bvid,
                aid=aid,
                cid=p.get("cid"),
                page_no=int(p.get("page") or idx + 1),
                title=title,
                part_title=p.get("part") or "",
                page_count=len(pages),
                duration_ms=_ms(p.get("duration")),
                cover=cover,
            )
            for idx, p in enumerate(pages)
        ]
        # 带 ?p=N 的链接只下那一 P——用户点明了要哪一集，多下是噪声。
        wanted = [it for it in items if it.page_no == page] if page > 1 else items
        return Resolution(
            kind=KIND_MULTI_P,
            title=title,
            up_name=owner.get("name") or "",
            up_mid=owner.get("mid"),
            cover=cover,
            items=wanted or items,
            note="" if page > 1 else f"共 {len(items)} 个分P",
            raw=raw,
        )

    cid = pages[0].get("cid") if pages else view.get("cid")
    return Resolution(
        kind=KIND_SINGLE,
        title=title,
        up_name=owner.get("name") or "",
        up_mid=owner.get("mid"),
        cover=cover,
        items=[
            Item(
                bvid=bvid,
                aid=aid,
                cid=cid,
                page_no=1,
                title=title,
                duration_ms=_ms(view.get("duration")),
                cover=cover,
            )
        ],
        raw=raw,
    )


def _from_season(data: dict[str, Any], ep_id: int | None, raw: str) -> Resolution:
    """``pgc/view/web/season`` 的返回 → Resolution（番剧/影视）。"""
    title = data.get("title") or data.get("season_title") or ""
    cover = data.get("cover") or ""
    up = (data.get("up_info") or {}).get("uname") or ""
    episodes = data.get("episodes") or []
    items: list[Item] = []
    for ep in episodes:
        items.append(
            Item(
                bvid=ep.get("bvid") or "",
                aid=ep.get("aid"),
                cid=ep.get("cid"),
                page_no=1,
                # 番剧同理：整季同名会分不清，取本话标题，取不到再回落季名。
                title=ep.get("long_title") or ep.get("title") or title,
                part_title=ep.get("title") or ep.get("long_title") or "",
                duration_ms=_ms(ep.get("duration")),
                cover=ep.get("cover") or cover,
            )
        )
    if not items:
        raise WBError("NOT_FOUND", "这个番剧没有可下载的剧集", detail=str(data.get("season_id") or ""))
    if ep_id is not None:
        # episodes[i] 与 items[i] 一一对应（items 就是按同一顺序建的）。
        picked = [it for it, ep in zip(items, episodes) if str(ep.get("id")) == str(ep_id)]
        # 对得上就只下这一集；对不上（接口改版/字段缺失）就整季——宁可多下，也不错下。
        if picked:
            items = picked
    return Resolution(
        kind=KIND_BANGUMI,
        title=title,
        up_name=up,
        cover=cover,
        collection_title=title,
        season_id=data.get("season_id"),
        items=items,
        note="" if ep_id is None else "只下载指定的这一集",
        raw=raw,
    )


class Resolver:
    """解析入口。持有网关，所以同一实例内的缓存能跨调用生效。"""

    def __init__(self, gateway: BiliGateway) -> None:
        self.gw = gateway

    def resolve(self, raw: str) -> Resolution:
        text = _norm(raw)
        if _SHORT.match(text):
            try:
                text = self.gw.client().expand_short_link(text)
            except Exception as exc:  # noqa: BLE001
                raise WBError("BILI_ERROR", f"b23.tv 短链展开失败：{exc}", str(exc), retryable=True) from exc

        cache_key = f"resolve::{text.lower()}"
        cached = self.gw.cache.get(cache_key)
        if isinstance(cached, Resolution):
            return cached

        kind, value, page = identify(text)
        if kind == "bv":
            view = self.gw.get_data(VIDEO_VIEW, {"bvid": value}, action="查询稿件")
            result = _from_view(view, page, raw)
        elif kind == "av":
            view = self.gw.get_data(VIDEO_VIEW, {"aid": int(value)}, action="查询稿件")
            result = _from_view(view, page, raw)
        elif kind in ("ep", "ss", "md"):
            param = {"ep_id": int(value)} if kind == "ep" else (
                {"season_id": int(value)} if kind == "ss" else {"media_id": int(value)}
            )
            data = self.gw.get_data(PGC_SEASON, param, action="查询番剧")
            result = _from_season(data, int(value) if kind == "ep" else None, raw)
        else:  # pragma: no cover —— identify 已经穷举过
            raise WBError("VALIDATION", "无法识别的链接类型", detail=kind)

        self.gw.cache.put(cache_key, result)
        return result
