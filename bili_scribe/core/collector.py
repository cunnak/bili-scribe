"""内容采集：沿降级链逐级尝试，直到拿到一份能支撑总结的素材。

降级链的排序依据是"离视频内容本体的距离"：

    人工字幕 > B站 AI 字幕 > 弹幕 > 标题／简介／标签

弹幕虽然量大，但它记录的是观众反应，不是作者说了什么。把它当成内容本体来总结，
结果一定是错的——所以它排在字幕之后，并且在提示词里被明确标注为"观众视角"。

整条链都拿不到素材时，采集器直接报错退出，不拼凑、不硬编。一份基于"标题+简介"
编出来的总结看着像模像样，实际上是把模型的先验知识伪装成了视频内容。
"""

from __future__ import annotations

import html
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import CollectConfig
from ..infra.bili_client import BiliClient, BiliError
from ..infra.endpoints import (
    ARCHIVE_TAGS,
    COMMENT_TYPE_VIDEO,
    DANMAKU_XML,
    PLATFORM_SUMMARY,
    PLAYER_V2,
    REPLY_MAIN,
    VIDEO_VIEW,
)
from . import srt as srt_mod

SOURCE_META: dict[str, tuple[str, str, str]] = {
    "subtitle_user": (
        "你上传的字幕",
        "未知",
        "由使用者提供的 srt 文件。内容未经平台或本工具校验——"
        "如果是第三方下载的，可能与这条视频的版本不一致；"
        "也可能只覆盖了视频的一部分（例如被裁剪过），"
        "所以不要把「素材里没有」当成「视频里没有」",
    ),
    "subtitle_human": ("人工字幕", "高", "UP 主上传或平台校对的字幕，最贴近视频原意"),
    "subtitle_ai": ("B站 AI 字幕", "中", "平台语音识别生成，存在同音字与断句错误"),
    "danmaku": ("弹幕", "低", "只反映观众反应与情绪高点，不等于视频内容本体"),
    "meta": ("标题／简介／标签", "低", "元数据，信息量有限，撑不起内容级总结"),
}

# 弹幕默认上限，防止把几分钟的高密度弹幕全塞进提示词
_DANMAKU_XML_PATTERN = re.compile(r'<d p="([^"]*)"[^>]*>(.*?)</d>', re.DOTALL)


class CollectError(RuntimeError):
    """素材采集失败，且没有可用的降级路径。"""


@dataclass
class SourceCheck:
    kind: str
    label: str
    available: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass
class Bundle:
    bvid: str
    aid: int
    cid: int
    page: int
    page_count: int
    title: str
    desc: str
    up_name: str
    up_mid: int
    duration: int
    tname: str
    tags: list[str] = field(default_factory=list)
    hot_replies: list[str] = field(default_factory=list)
    platform_summary: str = ""
    source_kind: str = "meta"
    source_label: str = ""
    source_trust: str = ""
    source_detail: str = ""
    body: str = ""
    warnings: list[str] = field(default_factory=list)
    checks: list[SourceCheck] = field(default_factory=list)

    def checks_as_dicts(self) -> list[dict[str, Any]]:
        return [check.as_dict() for check in self.checks]


# --------------------------------------------------------------------- 工具


def _inflate(raw: bytes) -> str:
    """弹幕接口返回的是裸 deflate 流，头部没有标准信息，得逐个窗口位试。"""
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS, 47):
        try:
            return zlib.decompress(raw, wbits).decode("utf-8", "ignore")
        except zlib.error:
            continue
    return raw.decode("utf-8", "ignore")


def _stamp(seconds: float) -> str:
    total = int(seconds or 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _pick_subtitle(subtitles: list[dict[str, Any]], prefer: list[str]) -> dict[str, Any] | None:
    if not subtitles:
        return None
    by_lang = {str(item.get("lan", "")): item for item in subtitles}
    for lang in prefer:
        if lang in by_lang:
            return by_lang[lang]
    for item in subtitles:
        lang = str(item.get("lan", "")).lower()
        if "zh" in lang or "cn" in lang:
            return item
    return subtitles[0]


def _parse_subtitle(payload: Any) -> list[tuple[float, str]]:
    """解析 json3 字幕，返回 (起始秒, 文本)。两种排布都要兼容。"""
    segments = (payload or {}).get("body") or []
    items: list[tuple[float, str]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("content"):
            text = str(segment["content"])
        else:
            text = "".join(str(part.get("utf8", "")) for part in (segment.get("segs") or []))
        text = text.strip()
        if not text:
            continue
        try:
            start = float(segment.get("from") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        items.append((start, text))
    return items


def _merge_subtitle(
    items: list[tuple[float, str]], window: float = 15.0, max_chars: int = 90
) -> list[tuple[float, str]]:
    """按时间窗把碎片字幕聚合成段落，并保留起始时间戳。

    平台 AI 字幕既没有标点，又被切成几字一行。原样拼给模型，模型很难判断章节
    边界，也很容易被支离破碎的语序带偏。按 15 秒窗聚合能把断句接回成句；
    留下的时间戳则让"时间轴"有真实依据，而不是靠模型编。
    """
    merged: list[tuple[float, str]] = []
    buffer: list[str] = []
    buffer_start: float | None = None

    for start, text in items:
        if buffer_start is None:
            buffer_start = start
        elif (start - buffer_start) >= window or len("".join(buffer)) >= max_chars:
            # 必须先结算上一段再收新内容。若把这一步放在 append 之后，
            # 触发换段的那一条就会被错误地并进上一段。
            merged.append((buffer_start, "".join(buffer)))
            buffer_start = start
            buffer = []
        buffer.append(text)

    if buffer and buffer_start is not None:
        merged.append((buffer_start, "".join(buffer)))
    return merged


# ----------------------------------------------------------------- 各来源


def _list_subtitles(
    client: BiliClient, aid: int, cid: int, cfg: CollectConfig
) -> tuple[dict[str, Any] | None, str]:
    """只拿字幕清单，不下载内容。

    单独抽出来，是为了"使用者已经上传了字幕、但仍要如实告诉他平台有没有字幕"
    这个场景——那种情况下只想知道有没有，不该白下一遍字幕文件。
    """
    if not client.is_logged_in:
        return None, "未登录。字幕接口对匿名请求恒定返回空列表，没有免登录路径"
    try:
        data = client.get_data(
            PLAYER_V2,
            {"aid": aid, "cid": cid, "platform": "web", "web_location": 1550101},
            signed=True,
        )
    except BiliError as exc:
        return None, f"播放器接口调用失败：{exc}"

    subtitles = ((data or {}).get("subtitle") or {}).get("subtitles") or []
    if not subtitles:
        return None, "该视频没有字幕：UP 主未上传，平台也未生成 AI 字幕"

    chosen = _pick_subtitle(subtitles, cfg.prefer_subtitle_lang)
    if not chosen or not chosen.get("subtitle_url"):
        return None, "字幕列表为空或条目缺少 subtitle_url"
    return chosen, ""


def _collect_subtitle(
    client: BiliClient, aid: int, cid: int, cfg: CollectConfig
) -> tuple[dict[str, Any] | None, str]:
    chosen, note = _list_subtitles(client, aid, cid, cfg)
    if not chosen:
        return None, note

    try:
        payload = client.get_json_url(chosen["subtitle_url"])
    except BiliError as exc:
        return None, f"字幕文件下载失败：{exc}"

    lines = _parse_subtitle(payload)
    if not lines:
        return None, "字幕文件解析后没有有效文本"

    merged = _merge_subtitle(lines)
    lang = str(chosen.get("lan", ""))
    is_ai = "ai" in lang.lower()
    return {
        "text": "\n".join(f"[{_stamp(start)}] {text}" for start, text in merged),
        "lang": lang,
        "lang_doc": str(chosen.get("lan_doc", "")),
        "is_ai": is_ai,
        "line_count": len(merged),
        "raw_segments": len(lines),
    }, ""


def _prepare_user_subtitle(
    raw: bytes, duration: int
) -> tuple[dict[str, Any] | None, list[str], str]:
    """解析使用者上传的 srt，返回 (素材, 警告, 说明)。

    **刻意不抛异常**：传错文件、编码不认识都是常事，这时应当降级回平台字幕
    并如实说明，而不是让一次总结直接失败。
    """
    try:
        items, encoding = srt_mod.parse_bytes(raw)
    except srt_mod.SrtError as exc:
        return None, [f"你上传的字幕文件用不了：{exc}"], ""

    warnings: list[str] = []
    if not srt_mod.is_ordered(items):
        items = sorted(items, key=lambda pair: pair[0])
        warnings.append("你上传的字幕时间戳不是递增的，已按时间重新排序")
    warnings.extend(srt_mod.check_alignment(items, duration))

    # 用户字幕**不做 15 秒窗聚合**：srt 通常已是规范的一整句一条，
    # 再按时间窗粘合会把独立对白并成一坨（理由见 core/srt.py 的模块说明）。
    return (
        {
            "text": "\n".join(f"[{_stamp(start)}] {text}" for start, text in items),
            "lang": "",
            "lang_doc": "",
            # 不给它定 human/ai：那两者的区分是平台字幕内部的事，
            # 用户字幕走独立的 source_kind。
            "is_ai": False,
            "line_count": len(items),
            "raw_segments": len(items),
            "encoding": encoding,
        },
        warnings,
        f"已解析 {len(items)} 段（识别为 {encoding} 编码）",
    )


def _collect_danmaku(
    client: BiliClient, cid: int, cfg: CollectConfig
) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = client.get_bytes(DANMAKU_XML, {"oid": cid})
    except BiliError as exc:
        return None, f"弹幕抓取失败：{exc}"

    xml = _inflate(raw)
    matches = _DANMAKU_XML_PATTERN.findall(xml)
    if not matches:
        return None, "该视频没有弹幕（或弹幕池为空）"

    parsed: list[tuple[float, str]] = []
    for attrs, raw_text in matches:
        text = html.unescape(re.sub(r"<[^>]+>", "", raw_text)).strip()
        if not text:
            continue
        stamp = attrs.split(",", 1)[0]
        try:
            seconds = float(stamp)
        except ValueError:
            seconds = 0.0
        parsed.append((seconds, text))

    if not parsed:
        return None, "弹幕内容解析后为空"

    counter = Counter(text for _s, text in parsed)
    hot_n = max(1, cfg.danmaku.hot_items)
    sample_n = max(1, cfg.danmaku.sample_items)
    hot = counter.most_common(hot_n)
    step = max(1, len(parsed) // sample_n)
    sampled = parsed[::step][:sample_n]

    blocks = [
        f"【高频弹幕】共 {len(parsed)} 条，下列为被重复发送最多的 {len(hot)} 条，"
        "代表观众反应最集中的位置："
    ]
    blocks += [f"{text}（出现 {count} 次）" for text, count in hot]
    blocks.append("")
    blocks.append(f"【时间轴采样】沿播放进度均匀取 {len(sampled)} 条，反映内容推进节奏：")
    blocks += [f"[{_stamp(seconds)}] {text}" for seconds, text in sampled]

    body = "\n".join(blocks)
    limit = max(1000, cfg.danmaku.max_items * 24)
    truncated = len(body) > limit
    if truncated:
        body = body[:limit] + "\n…（已截断）"

    return {
        "text": body,
        "total": len(parsed),
        "hot": len(hot),
        "sampled": len(sampled),
        "truncated": truncated,
    }, ""


def _collect_tags(client: BiliClient, bvid: str) -> list[str]:
    try:
        data = client.get_data(ARCHIVE_TAGS, {"bvid": bvid})
    except BiliError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item.get("tag_name", "")) for item in data if item.get("tag_name")][:12]


def _collect_replies(client: BiliClient, aid: int, limit: int = 20) -> list[str]:
    """热门评论。仅登录可用，失败就静默跳过——它只是佐料，不是必需。"""
    if not client.is_logged_in:
        return []
    try:
        data = client.get_data(
            REPLY_MAIN,
            {
                "oid": aid,
                "type": COMMENT_TYPE_VIDEO,
                "mode": 3,
                "ps": limit,
                "pn": 1,
                "web_location": 1315875,
            },
            signed=True,
        )
    except BiliError:
        return []
    replies = (data or {}).get("replies") or []
    out: list[str] = []
    for reply in replies[:limit]:
        message = ((reply.get("content") or {}).get("message") or "").strip()
        if message:
            out.append(f"{message}（赞 {reply.get('like', 0)}）")
    return out


def _collect_platform_summary(client: BiliClient, aid: int, cid: int, up_mid: int) -> str:
    """平台自带的 AI 摘要。只作对照展示，绝不喂给模型、也绝不冒充本工具的输出。"""
    if not client.is_logged_in:
        return ""
    try:
        data = client.get_data(
            PLATFORM_SUMMARY, {"aid": aid, "cid": cid, "up_mid": up_mid}, signed=True
        )
    except BiliError:
        return ""
    result = (data or {}).get("model_result") or {}
    parts: list[str] = []
    if result.get("summary"):
        parts.append(str(result["summary"]).strip())
    outline = result.get("outline")
    if isinstance(outline, list):
        for section in outline:
            if isinstance(section, dict) and section.get("title"):
                parts.append(f"· {section['title']}")
    return "\n".join(parts)


# ------------------------------------------------------------------- 主流程


def collect(
    client: BiliClient,
    bvid: str,
    page: int,
    cfg: CollectConfig,
    user_srt: bytes | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> Bundle:
    notify = progress or (lambda _stage, _message: None)
    warnings: list[str] = []
    checks: list[SourceCheck] = []

    notify("fetching_meta", "读取视频基本信息")
    view = client.get_data(VIDEO_VIEW, {"bvid": bvid})
    if not isinstance(view, dict) or not view.get("aid"):
        raise CollectError("稿件信息读取失败，接口没有返回有效的 aid")

    pages = view.get("pages") or []
    if not pages:
        raise CollectError("该稿件没有分P信息，无法确定 cid")

    page_index = min(max(page, 1), len(pages))
    if page_index != page:
        warnings.append(f"请求的是第 {page} P，但该稿件只有 {len(pages)} P，已改为第 {page_index} P")
    selected = pages[page_index - 1] or {}

    aid = int(view["aid"])
    cid = int(selected.get("cid") or view.get("cid") or 0)
    if not cid:
        raise CollectError("无法确定该分P的 cid")

    up = view.get("owner") or {}
    bundle = Bundle(
        bvid=view.get("bvid") or bvid,
        aid=aid,
        cid=cid,
        page=page_index,
        page_count=len(pages),
        title=str(view.get("title") or "").strip(),
        desc=str(view.get("desc") or "").strip(),
        up_name=str(up.get("name") or "").strip(),
        up_mid=int(up.get("mid") or 0),
        duration=int(view.get("duration") or selected.get("duration") or 0),
        tname=str(view.get("tname") or "").strip(),
        warnings=warnings,
    )

    notify("fetching_tags", "读取标签")
    bundle.tags = _collect_tags(client, bundle.bvid)

    # —— 字幕：使用者上传的优先 ——
    # 顺序是先处理上传件、再决定要不要采平台字幕。这样"平台到底有没有字幕"
    # 仍然会被查到并如实报告（杜绝静默忽略上传的文件），但已经上传时
    # 就不必白下一遍平台字幕文件。
    notify("fetching_subtitle", "尝试获取字幕")
    user_subtitle: dict[str, Any] | None = None
    if user_srt:
        user_subtitle, user_warnings, user_note = _prepare_user_subtitle(
            user_srt, bundle.duration
        )
        warnings.extend(user_warnings)
        checks.append(
            SourceCheck(
                "subtitle_user",
                "你上传的字幕",
                bool(user_subtitle),
                user_note or "未能解析，已回落到平台字幕",
            )
        )

    subtitle: dict[str, Any] | None = None
    subtitle_note = ""
    if user_subtitle:
        chosen, platform_note = _list_subtitles(client, aid, cid, cfg)
        if chosen:
            platform_note = (
                f"平台另有 {chosen.get('lan_doc') or chosen.get('lan')} 字幕，"
                "按你的选择本次未采用"
            )
        checks.append(SourceCheck("subtitle", "平台字幕", bool(chosen), platform_note))
    else:
        subtitle, subtitle_note = _collect_subtitle(client, aid, cid, cfg)
        checks.append(
            SourceCheck(
                "subtitle",
                "字幕",
                bool(subtitle),
                (
                    f"已取得 {subtitle['lang_doc'] or subtitle['lang']} 字幕，"
                    f"聚合为 {subtitle['line_count']} 段（原始 {subtitle['raw_segments']} 条）"
                    if subtitle
                    else subtitle_note
                ),
            )
        )

    # 判断"已有字幕"必须把上传件算进去，否则传了字幕还会再去抓一遍弹幕
    have_subtitle = bool(user_subtitle or subtitle)
    need_danmaku = cfg.danmaku.enabled and (
        not have_subtitle or cfg.danmaku.also_with_subtitle
    )
    danmaku: dict[str, Any] | None = None
    if need_danmaku:
        notify("fetching_danmaku", "尝试获取弹幕")
        danmaku, danmaku_note = _collect_danmaku(client, cid, cfg)
    else:
        danmaku_note = "已有字幕，按配置跳过弹幕以节省开销"
    checks.append(
        SourceCheck(
            "danmaku",
            "弹幕",
            bool(danmaku),
            (
                f"已取得 {danmaku['total']} 条弹幕，取高频 {danmaku['hot']} 条 + 时间轴 {danmaku['sampled']} 条"
                if danmaku
                else danmaku_note
            ),
        )
    )

    notify("fetching_replies", "读取热门评论")
    if not have_subtitle and danmaku is None:
        bundle.hot_replies = _collect_replies(client, aid)
    checks.append(
        SourceCheck(
            "replies",
            "热门评论",
            bool(bundle.hot_replies),
            f"已取得 {len(bundle.hot_replies)} 条热门评论"
            if bundle.hot_replies
            else "未获取（仅在缺少字幕与弹幕时才作为补充）",
        )
    )

    if cfg.include_platform_summary:
        notify("fetching_platform", "读取平台侧 AI 摘要（仅供对照）")
        bundle.platform_summary = _collect_platform_summary(client, aid, cid, bundle.up_mid)

    # —— 选主素材 ——
    # 使用者上传的排在最前：他特意准备了这份文件，就说明他要的是它。
    if user_subtitle:
        bundle.source_kind = "subtitle_user"
        bundle.body = user_subtitle["text"]
    elif subtitle:
        bundle.source_kind = "subtitle_human" if not subtitle["is_ai"] else "subtitle_ai"
        bundle.body = subtitle["text"]
        if subtitle["is_ai"]:
            warnings.append(
                "本次用的是平台 AI 字幕（语音识别），存在同音字与断句误差，"
                "总结中的专有名词与数字需自行复核"
            )
    elif danmaku:
        bundle.source_kind = "danmaku"
        bundle.body = danmaku["text"]
        warnings.append(
            "该视频没有字幕，本次总结基于弹幕。弹幕只反映观众反应，"
            "不能等同于视频讲了什么——结论的确定性明显低于字幕路径"
        )
    else:
        bundle.source_kind = "meta"
        parts = [f"标题：{bundle.title}", f"UP 主：{bundle.up_name}"]
        if bundle.desc:
            parts.append(f"简介：\n{bundle.desc}")
        if bundle.tags:
            parts.append("标签：" + "、".join(bundle.tags))
        if bundle.hot_replies:
            parts.append("热门评论：\n" + "\n".join(bundle.hot_replies))
        bundle.body = "\n\n".join(parts)
        warnings.append(
            "字幕与弹幕都不可用，只剩标题、简介这类元数据。"
            "这份总结覆盖不了视频内容，请把它当成主题提示，而不是内容总结"
        )

    label, trust, detail = SOURCE_META[bundle.source_kind]
    bundle.source_label = label
    bundle.source_trust = trust
    bundle.source_detail = detail
    bundle.checks = checks
    bundle.warnings = warnings
    return bundle
