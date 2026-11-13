"""B站字幕：列轨、取正文、转成字幕段（轻量总结的素材腿之一）。

**为什么不去调 ``bili_scribe.core.collector`` 的 ``_collect_subtitle``。** 那个函数把
"取字幕"和"走完整个降级链"绑在一起：它会继续去抓弹幕、简介、标签，失败时抛
``CollectError``。轻量总结只要字幕这一样，多抓的东西既花钱又可能因为别的接口挂了
把整条路带塌。所以这里**只借配方里的纯函数**（选轨、解析、15 秒窗聚合），自己管
登录判据与错误码。

**登录判据为什么必须自己写。** bs 的 ``_list_subtitles`` 写的是
``if not client.is_logged_in``，而 ``BiliClient.is_logged_in`` 是个**方法**——
bound method 恒为真，那个 if 对真客户端**从来不会成立**。WB 的网关
（``core/bili.py``）已经把两种形态都认了（``is_logged_in()`` 会判 callable），
所以这里走网关，不复制那条判据。

**未登录不是"没字幕"。** 字幕接口对匿名请求恒定返回空列表，与"UP 主没传"长得一模
一样。分不清这两件事，用户就会得到一句"这个视频没有字幕"然后去别处找——而实际上
他登录一下就有了。所以未登录单独报 ``AUTH_REQUIRED``，文案里点名"需要登录"。
"""

from __future__ import annotations

from typing import Any

from bili_scribe.core.collector import _merge_subtitle, _parse_subtitle, _pick_subtitle
from bili_scribe.infra.endpoints import PLAYER_V2
from video_workbench.app.errors import WBError
from video_workbench.core.bili import BiliGateway
from video_workbench.core.summary_source import segments_from_pairs

#: 播放器接口要的固定参数。``web_location`` 是前端埋点里的位置码，缺了会被判成
#: 非浏览器请求；``platform=web`` 决定返回 web 形态的字幕清单。
_PLAYER_PARAMS = {"platform": "web", "web_location": 1550101}

#: 15 秒窗聚合的两个参数，与 bs 的默认值保持一致（那边是 ``_merge_subtitle`` 的
#: 形参默认）。改这里等于改提示词看到的断句粒度，别随手调。
_MERGE_WINDOW = 15.0
_MERGE_MAX_CHARS = 90


def _logged_in(gateway: BiliGateway) -> bool:
    """网关的登录态。取不到就当没登录——未登录是可解释的状态，抛异常不是。"""
    try:
        return bool(gateway.is_logged_in())
    except WBError:
        return False


def _raw_tracks(gateway: BiliGateway, aid: int, cid: int) -> list[dict[str, Any]]:
    """播放器接口里的字幕清单。签名请求；没登录时上游返回空列表。"""
    data = gateway.get_data(
        PLAYER_V2,
        {"aid": int(aid), "cid": int(cid), **_PLAYER_PARAMS},
        action="查询字幕",
        signed=True,
    )
    subtitle = (data or {}).get("subtitle") or {}
    tracks = subtitle.get("subtitles") or []
    return [item for item in tracks if isinstance(item, dict)]


def list_tracks(gateway: BiliGateway, aid: int, cid: int) -> tuple[list[dict[str, Any]], str]:
    """列这条视频有哪些字幕轨，返回 ``(tracks, note)``。

    ``tracks`` 里每一项是 ``{lan, lan_doc, is_ai}``——只给页面需要的东西，
    不下发 ``subtitle_url``（那是服务端拿去下正文用的，页面拿它没用）。
    ``note`` 为空表示一切正常；非空时要原样显示给用户看。
    """
    if not _logged_in(gateway):
        return [], "B站字幕接口需要登录。字幕接口对匿名请求恒定返回空列表，没有免登录路径"
    try:
        tracks = _raw_tracks(gateway, aid, cid)
    except WBError as exc:
        return [], f"查询字幕失败：{exc.message}"
    if not tracks:
        return [], "该视频没有字幕：UP 主未上传，平台也未生成 AI 字幕"
    return [
        {
            "lan": str(item.get("lan") or ""),
            "lan_doc": str(item.get("lan_doc") or ""),
            "is_ai": "ai" in str(item.get("lan") or "").lower(),
        }
        for item in tracks
    ], ""


def pick_track(
    tracks: list[dict[str, Any]], prefer: list[str] | tuple[str, ...], lang: str = ""
) -> dict[str, Any] | None:
    """挑一条字幕轨：指定 ``lang`` 优先，否则按配置的偏好顺序，再否则第一条中文。"""
    wanted = str(lang or "").strip()
    if wanted:
        for item in tracks:
            if str(item.get("lan") or "") == wanted:
                return item
    # ``_pick_subtitle`` 认的是带 ``subtitle_url`` 的原始条目，但选轨逻辑（按 lan 查、
    # 再找含 zh/cn 的、最后取第一条）与字段无关，所以这里可以直接喂精简后的形状。
    return _pick_subtitle(tracks, list(prefer or ()))


def fetch(
    gateway: BiliGateway,
    aid: int,
    cid: int,
    prefer: list[str] | tuple[str, ...],
    *,
    lang: str = "",
    duration_ms: int = 0,
) -> dict[str, Any]:
    """取一条字幕并转成字幕段。

    返回 ``{origin, lang, lang_doc, is_ai, segments, line_count, raw_segments}``。
    ``origin`` 是 ``core/summary_source.py`` 的词：``bili_ai`` 或 ``bili_human`，
    判据只有一条——平台自己在 ``lan`` 里写了 ``ai``（例如 ``ai-zh``）就是 AI 字幕。
    """
    if not _logged_in(gateway):
        raise WBError(
            "AUTH_REQUIRED",
            "B站字幕接口需要登录",
            "字幕接口对匿名请求恒定返回空列表，没有免登录路径；也可以改选本地字幕文件",
        )
    raw_tracks = _raw_tracks(gateway, aid, cid)
    if not raw_tracks:
        raise WBError(
            "CONFLICT",
            "该视频没有字幕：UP 主未上传，平台也未生成 AI 字幕",
            "没有字幕就没有素材可用；可以改选本地字幕文件",
        )
    chosen = pick_track(
        [
            {
                "lan": str(item.get("lan") or ""),
                "lan_doc": str(item.get("lan_doc") or ""),
                "is_ai": "ai" in str(item.get("lan") or "").lower(),
                "subtitle_url": str(item.get("subtitle_url") or ""),
            }
            for item in raw_tracks
        ],
        prefer,
        lang,
    )
    if not chosen or not chosen.get("subtitle_url"):
        raise WBError("CONFLICT", "字幕列表里没有可下载的条目", "条目缺少 subtitle_url")
    try:
        payload = gateway.client().get_json_url(chosen["subtitle_url"])
    except Exception as exc:  # noqa: BLE001 —— 统一翻成 B站错误
        raise WBError("BILI_ERROR", f"字幕文件下载失败：{exc}", str(exc)) from exc
    lines = _parse_subtitle(payload)
    if not lines:
        raise WBError("CONFLICT", "字幕文件解析后没有有效文本", chosen.get("subtitle_url") or "")
    merged = _merge_subtitle(lines, window=_MERGE_WINDOW, max_chars=_MERGE_MAX_CHARS)
    lan = str(chosen.get("lan") or "")
    is_ai = "ai" in lan.lower()
    return {
        "origin": "bili_ai" if is_ai else "bili_human",
        "lang": lan,
        "lang_doc": str(chosen.get("lan_doc") or ""),
        "is_ai": is_ai,
        "segments": segments_from_pairs(merged, duration_ms=duration_ms),
        "line_count": len(merged),
        "raw_segments": len(lines),
    }
