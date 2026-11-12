"""发布草稿（P2-09）：把一版总结变成"能贴到 B站笔记里"的东西。

**这一层只做草稿，不发布。** 方案书原则 7 写死了「AI 产出一律是草稿」，
所以这里没有任何网络请求：拼正文、转 Delta、算字数、报越界，就这些。
真正的 publish_note 调用在 Phase 5 接上 —— 那时门禁（内容确认 / 账号与视频 /
频率与重复）才有意义，因为只有发布时才需要承担"发出去就撤不回"的代价。

复用 bili-scribe 的两件东西，不重写：

- `composer.build_publish_document(summary)`：正文形态（加粗一句话 + 分节 +
  要点 + 时间轴）是它定的，文案口味的事不该在这里第二个地方再定一次。
- `composer.markdown_to_delta(markdown)`：B站 笔记接口要 Quill Delta，它对
  未知属性很敏感（多塞一个 header 可能直接 -400），这套保守映射已经被真站验过。

字数口径（界面上要显示，所以必须写清楚）：
- **正文字数** = Delta 里所有 insert 文本去掉空白后的字符数，这是界面计数口径。
- **正文请求体**：B站 x/note/add 按表单编码后的请求体字节数校验，约 20000 B 上限；
  中文经 urlencode 后每字约 9 字节，逐行 op 的 Delta 很容易把请求体顶过线。
- 标题 ≤ 80：接口硬限制，超了发不出去，所以在**存草稿之前**就报出来。

**摘要字段已下线（2026-11-07）**：笔记接口的 `summary` 会被 `auto_comment`
自动发成评论区评论，而同一段话在正文里已经有了。界面上不再给这个输入框，
库里老行存的那份也不再认。接口里那个位置仍然要填（文档标的是"必要"），
填的是**正文开头**（`note_preview`），`auto_comment` 恒关。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from bili_scribe.core import composer
from bili_scribe.core.summarizer import Summary
from bili_scribe.infra.bili_client import BiliError
from bili_scribe.infra.endpoints import (
    FATAL_COMMENT_CODES,
    NOTE_ADD,
    NOTE_DEL,
    RETRYABLE_CODES,
)

from ..app.errors import WBError

TITLE_MAX = 80
# B站 x/note/add 的实测表单请求体上限约 20000 B；留 200 B 余量，
# 避免 csrf/cookie 长度差异把刚好卡线的草稿再送进 79501。
NOTE_BODY_MAX_BYTES = 19_800
DISCLOSURE = "（本总结由 AI 依据视频字幕生成，发布前请自己过一遍）"

# 方案书 §5.9.3 第 3 条：距上次发布 ≥ 60s。写成常量是因为界面上要显示
# "还要等 N 秒"，两个地方用同一个数才不会一个说 60 一个说 30。
GATE_INTERVAL_SECONDS = 60

# 正文里这些字段是**给操作者看的工作信息**，发出去只会稀释正文——
# composer.build_publish_document 已经替我们排除了元数据头/素材局限/平台摘要，
# 这里再排一遍"审核提示"，因为它属于内部嘱咐，不属于读者要看的内容。
_INTERNAL_MARKERS = ("素材局限", "采集提示")


def summary_from_content(content: dict[str, Any]) -> Summary:
    """把 `wb_summary.content` 还原成 bili-scribe 的 Summary。

    刻意逐字段列举而不是 `Summary(**content)`：content 里还可能带着
    notes/restored_from 这类工作台自己的键，直接解包会 TypeError。
    """
    content = content or {}
    timeline = []
    for item in content.get("timeline") or []:
        if isinstance(item, dict):
            timeline.append(
                {"t": str(item.get("t", "") or ""), "text": str(item.get("text", "") or "")}
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            timeline.append({"t": str(item[0]), "text": str(item[1])})
    points = [str(p) for p in (content.get("key_points") or []) if str(p).strip()]
    return Summary(
        one_line=str(content.get("one_line", "") or ""),
        summary_md=str(content.get("summary_md", "") or ""),
        key_points=points,
        timeline=timeline,
        caution=str(content.get("caution", "") or ""),
        comment_short=str(content.get("comment_short", "") or ""),
        chunks=int(content.get("chunks", 1) or 1),
        model=str(content.get("model", "") or ""),
        role=str(content.get("role", "") or ""),
        frames=int(content.get("frames", 0) or 0),
        revise_note=str(content.get("revise_note", "") or ""),
    )


def build_markdown(content: dict[str, Any], *, disclose: bool = True) -> str:
    """生成要发出去的正文（Markdown，只允许加粗）。

    **正文里只放读者要看的内容。** 三样东西被明确排除：

    - `caution`（"注意：素材为机器转录……"）：那是给操作者核对用的提醒，
      贴进正文会让读者第一眼看到一段自我否定，正文也从 2000 字涨到 2800 字。
    - `comment_short`（评论区短版）：它属于评论区，不属于正文；
      `composer.build_publish_document` 本来就不拼它，这里再写一句，
      免得以后有人觉得"正文太短，把短版补进去"。
    - 素材局限 / 采集提示：`_is_internal` 逐行滤掉。
    """
    summary = summary_from_content(content)
    body = composer.build_publish_document(summary)
    lines = [line for line in body.splitlines() if not _is_internal(line)]
    body = "\n".join(lines).strip()
    if disclose:
        body = body + "\n\n" + DISCLOSURE
    return body


def _is_internal(line: str) -> bool:
    stripped = line.strip()
    return any(marker in stripped for marker in _INTERNAL_MARKERS)


def to_delta(markdown: str) -> str:
    """Markdown → Quill Delta JSON 字符串（B站 笔记接口的 content 字段）。"""
    return composer.markdown_to_delta(markdown or "")


def plain_text(delta_json: str) -> str:
    """从 Delta JSON 取回纯文本（预览、字数、复制都用它）。"""
    try:
        ops = json.loads(delta_json) if delta_json else []
    except (TypeError, ValueError):
        return ""
    if not isinstance(ops, list):
        return ""
    out = []
    for op in ops:
        if isinstance(op, dict) and isinstance(op.get("insert"), str):
            out.append(op["insert"])
    return "".join(out)


def compact_delta(delta_json: str) -> str:
    """把相邻且 attributes 相同的文本 op 合并，减少重复的 Delta 包装。

    B站 笔记接口按 form-urlencoded 后的请求体大小校验。逐行一个
    ``{"insert": "\\n"}`` 的 Delta 会产生大量重复的 JSON 键与百分号转义；
    合并后纯文本与格式属性完全不变，只是请求体明显变小。

    非法 JSON 或非 list 输入原样返回，保持老草稿的降级路径不变。
    """
    try:
        ops = json.loads(delta_json) if delta_json else []
    except (TypeError, ValueError):
        return delta_json or ""
    if not isinstance(ops, list):
        return delta_json or ""

    merged: list[Any] = []
    for op in ops:
        if not isinstance(op, dict):
            merged.append(op)
            continue
        insert = op.get("insert")
        attrs = op.get("attributes") or {}
        if (
            merged
            and isinstance(merged[-1], dict)
            and isinstance(insert, str)
            and isinstance(merged[-1].get("insert"), str)
            and (merged[-1].get("attributes") or {}) == attrs
        ):
            merged[-1]["insert"] = str(merged[-1]["insert"]) + insert
            continue
        item = dict(op)
        if attrs:
            item["attributes"] = attrs
        else:
            item.pop("attributes", None)
        merged.append(item)
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))


def form_body_bytes(payload: dict[str, Any], csrf: str = "") -> int:
    """按 ``BiliClient.post_form`` 的编码方式计算请求体字节数。

    ``csrf`` 是真实发布时 ``post_form`` 会补上的 32 字符 token；门禁阶段
    用占位串计算，真实发送前再用客户端的 csrf 重算，二者只差长度相同的 token。
    """
    data = dict(payload or {})
    if csrf and "csrf" not in data:
        data["csrf"] = csrf
    return len(urlencode(data, encoding="utf-8", errors="strict").encode("utf-8"))


def count_chars(text: str) -> int:
    """字数口径：去掉所有空白后的字符数（见模块说明）。"""
    return len("".join((text or "").split()))


def default_title(project: dict[str, Any]) -> str:
    """标题默认取视频标题。

    **不在这里截断。** 截断会让「超过 80 字」这件事消失：界面显示一个刚好 80 字的
    标题、用户以为没事，发出去时却是被平台拒——而平台拒的理由他永远看不到。
    超长就如实超长，由 `build_draft` 报 warning、`check_draft` 拦下来。
    """
    return str((project or {}).get("title", "") or "").strip() or "视频笔记"


def default_summary(content: dict[str, Any]) -> str:
    """摘要字段已下线（2026-11-07），恒返回空串。

    原来这里默认取 `comment_short`（评论区短版）——268 字的卡片文案被当成笔记摘要
    发出去，而 B站 笔记摘要的口径是「约 200 字」，超了直接 12025 发不出去；
    而且这段文字与正文里的主体高度重复，用户看到的是"同一段话出现两次"。

    函数保留、签名不变：`build_draft` 与历史草稿行仍在读这个键，
    删掉它会让 `KeyError` 变成下一个 bug。参数 `content` 有意不再使用。
    """
    return ""


def build_draft(
    project: dict[str, Any],
    content: dict[str, Any],
    *,
    max_summary_chars: int = 400,
    disclose: bool = True,
    title: str = "",
    summary_text: str = "",
) -> dict[str, Any]:
    """生成一份草稿的完整载荷（不落库）。

    title 非空时用它（"重新生成"会保留用户已经改过的标题），
    这样界面上不会出现「改了个标题，点重新生成又被打回原标题」。

    `max_summary_chars` 与 `summary_text` 保留只为兼容老调用方与历史草稿行；
    摘要字段已下线（2026-11-07），两者都不再参与任何计算，`summary` 恒为空串。
    """
    markdown = build_markdown(content, disclose=disclose)
    delta = to_delta(markdown)
    text = plain_text(delta)
    draft_title = (title or default_title(project)).strip()
    draft_summary = ""

    warnings: list[str] = []
    if not draft_title:
        warnings.append("标题是空的")
    if len(draft_title) > TITLE_MAX:
        warnings.append(f"标题超过 {TITLE_MAX} 字（现在 {len(draft_title)} 字）")
    if not text.strip():
        warnings.append("正文是空的")

    return {
        "project_id": str((project or {}).get("id", "")),
        "project_title": str((project or {}).get("title", "") or ""),
        "title": draft_title,
        "summary": draft_summary,
        "content": delta,
        "content_text": text,
        "markdown": markdown,
        "chars": count_chars(text),
        "title_chars": len(draft_title),
        "summary_chars": len(draft_summary),
        "limits": {"title": TITLE_MAX},
        "warnings": warnings,
    }


def check_draft(draft: dict[str, Any]) -> None:
    """存库前的最后一道：越界的草稿存进去只会让人以为能发。"""
    warnings = draft.get("warnings") or []
    if not draft.get("title"):
        raise WBError("VALIDATION", "标题不能为空")
    if not str(draft.get("content_text", "")).strip():
        raise WBError("VALIDATION", "正文不能为空，先给这个项目出一版总结")
    hard = [w for w in warnings if "超过" in w]
    if hard:
        raise WBError("VALIDATION", "；".join(hard))


# 正文里这些行是**给操作者看的工作信息**，不该出现在发出去的笔记里。
# 老草稿（2026-11-07 之前存的）把 caution 直接拼进了正文——实测
# draft_8dfe01d36c95 里那一段是 742 字（占正文 27%），开头就是
# 「注意：素材为机器转录，错字错词极多且密集……」；draft_587f000022eb 里
# 也有一段「注意：素材为AI语音转录……」。用户明确要求这类内容不要进正文。
# 库里没有清零路径（老行不会自己变干净），所以在**出口这一处**统一洗：
# 读出来显示时洗、发出去之前洗——一处改动覆盖所有老行，不需要数据迁移。
_CAUTION_PREFIX = "注意："
_CAUTION_MARKERS = ("转录", "素材局限", "采集提示", "素材未确认")


def _is_caution_line(text: str) -> bool:
    """这一行是不是那段"素材有问题"的自我说明。

    必须同时满足「以注意：开头」与「带转录/素材局限这类标记」。
    **不能按"注意"两个字一刀切**：实测另一份草稿里有
    "流量注意力vs存量思维" 这种正文句子，那是内容本身。
    """
    stripped = text.strip()
    if not stripped.startswith(_CAUTION_PREFIX):
        return False
    return any(marker in stripped for marker in _CAUTION_MARKERS)


def strip_caution_lines(delta_json: str) -> str:
    """把正文里那段「注意：素材为机器转录……」整行拿掉，其余原样保留。

    按行处理是为了保住加粗属性：Delta 的 op 是「一段文字 + 属性」的序列，
    段落分隔靠 insert 里的换行符。这里先切成行、丢掉命中的整行、再拼回去，
    所以非命中行的 attributes 一个字都不动。
    """
    try:
        ops = json.loads(delta_json) if delta_json else []
    except (TypeError, ValueError):
        return delta_json or ""
    if not isinstance(ops, list):
        return delta_json or ""

    lines: list[list[dict[str, Any]]] = [[]]
    for op in ops:
        if not isinstance(op, dict):
            continue
        insert = op.get("insert")
        if not isinstance(insert, str):
            lines[-1].append(op)
            continue
        attrs = op.get("attributes")
        parts = insert.split("\n")
        for index, part in enumerate(parts):
            if part:
                seg: dict[str, Any] = {"insert": part}
                if attrs:
                    seg["attributes"] = attrs
                lines[-1].append(seg)
            if index < len(parts) - 1:
                lines.append([])

    kept: list[list[dict[str, Any]]] = []
    dropped = 0
    for line in lines:
        text = "".join(s.get("insert", "") for s in line if isinstance(s.get("insert"), str))
        if _is_caution_line(text):
            dropped += 1
            continue
        if not line and kept and not kept[-1]:
            continue  # 连续空行压成一行：拿掉整段之后不该留一个更大的洞
        kept.append(line)

    if not dropped:
        # 一个字都没删就把原文原样还回去：重新序列化会改掉空格与键序，
        # 而"用户手改过的正文以他的为准"这条要求逐字相同（test_service_publish
        # 的 test_edited_delta_wins_over_regenerated_markdown 就是拿字符串比）。
        return delta_json

    while kept and not kept[-1]:
        kept.pop()

    out: list[dict[str, Any]] = []
    for line in kept:
        out.extend(line)
        out.append({"insert": "\n"})
    while out and out[-1].get("insert") == "\n":
        out.pop()
    return json.dumps(out, ensure_ascii=False)


def row_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """草稿行的对外形状（附带字数，界面不用自己算）。"""
    if not row:
        return None
    view = dict(row)
    # 老草稿的正文里还留着那段「注意：素材为机器转录……」：读出来就洗掉，
    # 免得界面上字数、预览、复制三处都把它算进去（发布前还会再洗一次）。
    view["content"] = strip_caution_lines(view.get("content") or "")
    text = plain_text(view["content"])
    view["content_text"] = text
    view["chars"] = count_chars(text)
    view["title_chars"] = len(str(view.get("title", "") or ""))
    view["summary_chars"] = len(str(view.get("summary", "") or ""))
    return view


# --------------------------------------------------------------- 三道门禁（P4-04）


def _parse_time(value: Any) -> Any:
    """ISO 字符串 → datetime；解析不了就当没有（宁可少拦，不要误拦）。"""
    text = str(value or "").strip()
    if not text:
        return None
    from datetime import datetime

    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def _seconds_since(stamp: Any, *, now: Any = None) -> int | None:
    moment = _parse_time(stamp)
    if moment is None:
        return None
    from datetime import datetime, timezone

    base = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        base = base.replace(tzinfo=None)
    delta = base - moment
    return int(delta.total_seconds())


def gate_report(
    draft: dict[str, Any],
    *,
    project: dict[str, Any] | None = None,
    note: dict[str, Any] | None = None,
    published_note: dict[str, Any] | None = None,
    last_publish_at: str = "",
    summary_limit: int = 400,  # 摘要已下线，保留只为兼容老调用方；不参与判定
    min_interval_seconds: int = GATE_INTERVAL_SECONDS,
    confirmed: bool = False,
    logged_in: bool | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """三道门禁的**当前状态**。只读，不联网、不发任何请求。

    为什么要单独算一份状态：发布按钮的可用性、以及"为什么不能发"的理由，
    必须在界面上先说清楚；等用户点了才弹一个 79508，那是让人白跑一趟。

    status 有三档，别混：
    - ``ok``     ：这一关已经过了
    - ``blocked``：明确过不了，附 reason
    - ``todo``   ：还没到能判断的时候（如登录态要发布时才知道/还没勾确认）
    """
    draft = draft or {}
    project = project or {}
    note = note or {}

    gates: list[dict[str, Any]] = []

    # ---- 第一关：内容确认（能算的都算，勾选要用户来做）
    items: list[dict[str, Any]] = []
    title = str(draft.get("title", "") or "").strip()
    body = str(draft.get("content_text", "") or "").strip()
    items.append(
        {
            "text": f"标题 {len(title)} / {TITLE_MAX} 字",
            "ok": bool(title) and len(title) <= TITLE_MAX,
        }
    )
    content_delta = str(draft.get("content") or "")
    if not content_delta and body:
        # 单测/老调用方可能只给 content_text；按同一份正文估算请求体，
        # 真实服务路径始终会带完整 content。
        content_delta = json.dumps([{"insert": body}], ensure_ascii=False)
    body_bytes = form_body_bytes(
        note_payload(
            {"aid": draft.get("aid"), "title": draft.get("title"), "content": content_delta}
        ),
        csrf="x" * 32,
    )
    body_bytes_ok = bool(body) and body_bytes <= NOTE_BODY_MAX_BYTES
    items.append(
        {
            "text": f"正文 {len(''.join(body.split()))} 字 / 表单编码 {body_bytes} 字节",
            "ok": body_bytes_ok,
        }
    )
    items.append({"text": "已阅读并确认标题与正文", "ok": bool(confirmed)})
    # 标题和请求体是两个独立的「超限」；正文空只算空，不算「超限」。
    over = [items[0]["text"]] if not items[0]["ok"] else []
    if body and body_bytes > NOTE_BODY_MAX_BYTES:
        over.append(
            f"正文 {body_bytes} 字节（B站 约 20000 字节上限，本机安全线 {NOTE_BODY_MAX_BYTES} 字节）"
        )
    if not title or not body:
        content_status, content_reason = "blocked", "标题或正文是空的"
    elif over:
        content_status, content_reason = "blocked", "；".join(over)
    elif not confirmed:
        content_status, content_reason = "todo", "发布前要勾一下「我已阅读并确认」"
    else:
        content_status, content_reason = "ok", ""
    gates.append(
        {
            "id": "content",
            "label": "内容确认",
            "status": content_status,
            "reason": content_reason,
            "items": items,
        }
    )

    # ---- 第二关：账号与视频
    items = []
    project_bvid = str(project.get("bvid", "") or "")
    note_bvid = str(note.get("bvid", "") or draft.get("bvid", "") or "")
    same = bool(project_bvid) and (not note_bvid or note_bvid == project_bvid)
    # 内层引号必须是单引号：同一个 f-string 里复用外层引号是 Python 3.12 才允许的语法，
    # 而本项目最低运行时是 3.10（README.md「需要 Python 3.10+」）。写成双引号时
    # ruff 按 py310 判 invalid-syntax，3.10/3.11 上则直接 import 失败。
    items.append({"text": f"视频 BV 号 {project_bvid or '（没有）'}", "ok": bool(project_bvid)})
    # aid（av 号）是发布笔记的必填参数，库里历史行大多是空的（2026-11-07 之前
    # 下载落库时根本没记）。发布那一刻会自动补（项目目录的 sidecar → 视频信息），
    # 补不到才报错 —— 所以这里只如实标"现在没有"，不能因此把整关判成 blocked：
    # 那会让按钮永久灰着，而它其实发得出去。反过来，一个绿关配一个坏点也不行，
    # 用户点下去撞 CONFLICT 就是白跑一趟，所以要在关卡的说明里提前讲清楚。
    project_aid = project.get("aid") or note.get("aid") or draft.get("aid")
    items.append(
        {
            "text": (
                f"视频 aid（av 号）{project_aid}"
                if project_aid
                else "视频 aid（av 号）：库里没有，发布时自动补"
            ),
            "ok": bool(project_aid),
        }
    )
    items.append({"text": "草稿指向的是同一个视频", "ok": same})
    items.append(
        {
            "text": "登录状态："
            + ("已登录" if logged_in else ("未登录" if logged_in is False else "发布时再确认")),
            "ok": logged_in is not False,
        }
    )
    if not project_bvid:
        account_status, account_reason = (
            "blocked",
            "这个项目没有 BV 号，发不了笔记：在发布卡「视频号」里填 BV 号或 av 号，保存草稿后生效",
        )
    elif not same:
        account_status, account_reason = "blocked", "草稿的 BV 号与项目对不上"
    elif logged_in is False:
        account_status, account_reason = "blocked", "登录态失效了，先在设置里登录"
    elif logged_in is None:
        account_status, account_reason = "todo", "发布时会再核一次登录态与视频可访问性"
    else:
        account_status, account_reason = "ok", ""
        if not project_aid:
            # 过了，但有一项现在还没有：把补法写出来，别让人以为这一关是凭空绿的
            account_reason = "库里没有 av 号（aid），发布时会自动补；补不到就发不了"
    gates.append(
        {
            "id": "account",
            "label": "账号与视频",
            "status": account_status,
            "reason": account_reason,
            "items": items,
        }
    )

    # ---- 第三关：频率与重复
    items = []
    waited = _seconds_since(last_publish_at, now=now) if last_publish_at else None
    remain = 0
    if last_publish_at and waited is not None:
        remain = max(0, int(min_interval_seconds) - int(waited))
    items.append(
        {
            "text": ("距上次发布 %d 秒" % waited) if waited is not None else "距上次发布：还没发过",
            "ok": remain == 0,
        }
    )
    duplicate = bool(published_note) and str(published_note.get("status", "")) == "published"
    if duplicate:
        note_id = str(published_note.get("note_id", "") or "")
        items.append(
            {"text": "同一视频已有笔记" + (f"（{note_id}）" if note_id else ""), "ok": False}
        )
    else:
        items.append({"text": "同一视频没有未撤回的笔记", "ok": True})
    if duplicate:
        pace_status, pace_reason = "blocked", "这个视频已经发过笔记了，要重发先去 B站 删掉旧的"
    elif remain > 0:
        pace_status, pace_reason = (
            "blocked",
            f"还要等 {remain} 秒才能再发（限流 {min_interval_seconds} 秒）",
        )
    elif not body:
        pace_status, pace_reason = "blocked", "正文是空的"
    else:
        pace_status, pace_reason = "ok", ""
    gates.append(
        {
            "id": "pace",
            "label": "频率与重复",
            "status": pace_status,
            "reason": pace_reason,
            "items": items,
        }
    )

    ready = all(gate["status"] == "ok" for gate in gates)
    return {
        "gates": gates,
        "ready": ready,
        # 只有「字数上限」这一种东西走 limits：它是界面渲染输入框计数器的依据。
        # 限流不在这里 —— 那是 wait_seconds，两处都写会让界面不知道该信哪个
        # （2026-11-07 统一，此前 gate_report / build_draft / _publish_limits 三处不同形）。
        "limits": {"title": TITLE_MAX},
        # 还被卡住的门（按顺序），界面照这个顺序列"先处理这几项"
        "blockers": [gate["id"] for gate in gates if gate["status"] != "ok"],
        # 限流还要等多久：0 表示没在等。放在这里而不是让界面自己算——
        # 界面算就要拿 published_at 与本地时钟相减，用户改一下系统时间就骗过去了。
        "wait_seconds": int(remain),
    }


# ------------------------------------------------------------------ 发布

NOTE_TITLE_MAX = TITLE_MAX
# 摘要字段已下线（2026-11-07）：payload 的 summary 位发正文开头（note_preview），
# 不是用户可编辑的摘要。平台侧口径约 200 字；这个常量只作历史参考，不参与判定。
NOTE_SUMMARY_MAX = 200
NOTE_ATTEMPTS = 3

_NOTE_HINTS = {
    12015: "需要过验证码。这类拦截本应用不做绕过，请到网页端手动处理后再试。",
    12016: "内容被判成敏感信息，改一下措辞再发。",
    12025: "内容超长。平台对笔记标题有 80 字上限，正文也有长度限制，先把正文压短再试。",
    12035: "你被这位 UP 主加入了黑名单，在这个视频下发不了笔记。",
    12051: "平台判成重复内容。同一视频重复发会被风控，先撤回旧的那条。",
    12052: "这个视频的评论区已关闭。",
    -102: "账号处于封停状态，先把账号问题处理掉。",
    -111: "登录凭证过期了（csrf 失效），到设置里重新登录再发。",
    -101: "还没登录，或者登录已经失效，到设置里登录一次。",
    -509: "被限流了。等一会儿再发，别连着试。",
    # 79508 / 79513 是笔记接口专属码（方案书 §5.9.4 点名）：
    # 这两个都不能靠重试解决，也不能自动覆盖——重复笔记一旦被平台判成刷量更麻烦。
    79508: "这个视频下已经有你创建的笔记了。先去 B站 删掉旧的那条，再回来发；本应用不会覆盖它。",
    79513: "这个视频不支持笔记功能。可以改成发一条普通评论（本阶段还没做这个入口）。",
    79501: "笔记正文超过平台约 20000 字节的表单请求体上限。先删减正文再发；这不是登录态问题，重试不会变好。",
}


def note_error_text(exc: BaseException) -> str:
    """把 B站 的错误翻成人话。

    保留原始 code 与 message（用户要拿去搜索/反馈），后面接一句该怎么办。
    分不清的情况直说"可稍后重试"，不假装知道原因。
    """
    code = getattr(exc, "code", None)
    raw = str(getattr(exc, "raw_message", "") or getattr(exc, "message", "") or exc)
    text = f"[{code}] {raw}" if code is not None else raw
    hint = _NOTE_HINTS.get(code)
    if not hint:
        if code in FATAL_COMMENT_CODES:
            hint = "这类问题需要人工处理，本应用不会尝试绕过。"
        else:
            hint = "可稍后重试；反复失败先检查登录态是否失效。"
    return f"{text} —— {hint}"


def record_url(bvid: str = "", row: dict[str, Any] | None = None) -> str:
    """发布记录里那个"打开视频"要跳的地址。

    bvid 优先从行里取（记录可能是别的项目的），都空就不给链接——
    与其给一个 `https://www.bilibili.com/video/` 的空壳，不如让按钮不出现。
    """
    row = row or {}
    key = str(bvid or row.get("bvid") or "").strip()
    if not key:
        return ""
    return f"https://www.bilibili.com/video/{key}"


def record_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """发布记录的一行（列表用）：比草稿视图少正文，多了发布/撤回时间与链接。"""
    if not row:
        return None
    view = dict(row)
    view.pop("content", None)
    view["status_text"] = {
        "draft": "草稿",
        "published": "已发布",
        "withdrawn": "已撤回",
        "failed": "发布失败",
    }.get(str(view.get("status") or ""), str(view.get("status") or ""))
    view["url"] = record_url(str(view.get("bvid") or ""), view)
    view["title_chars"] = len(str(view.get("title", "") or ""))
    return view


NOTE_PREVIEW_CHARS = 80


def note_preview(content_delta: str, limit: int = NOTE_PREVIEW_CHARS) -> str:
    """笔记接口那个 `summary` 位置发什么：**正文开头的纯文本**，不是用户删掉的摘要。

    接口文档把这个字段标成"必要"，而它的原用途是笔记列表里的预览文本
    （`auto_comment=1` 时还会被拿去发一条评论区评论——本应用恒关）。

    发空串的风险：平台可能按"必填留空"直接 -400，用户看到的就是"点了发布发不出去"，
    而且这类失败本应用拿不到真站回执去验证，赌不起。
    发老草稿里那份的风险：那是用户已经在界面上删掉的摘要，发出去等于没删。

    真实客户端 BiliPai 的 `VideoNoteContentCodec.encode` 把 summary 截到 80 字，
    WB 跟着 80 走：既不为空、不多出用户没见过的话，也少占表单请求体。
    """
    text = " ".join(plain_text(content_delta).split())
    return text[:limit]


def note_payload(draft: dict[str, Any]) -> dict[str, Any]:
    """按笔记接口的形状组 payload（标题不截断：超长要在发之前被拦）。

    摘要字段已下线（2026-11-07）。`summary` 这个键仍要发——接口文档把它标成
    "必要"，而它的原用途是笔记列表里的预览文本（`auto_comment=1` 时还会被拿去
    发一条评论区评论，本应用恒关）。发什么见 `note_preview`：**正文开头**，
    既不空着（必填位留空可能直接 -400），也不多出用户没见过的话。

    注意这里**故意不读 `draft["summary"]`**：老草稿行里还留着下线前算好的那份，
    认它等于把用户已经删掉的摘要又发出去。调用方传什么都不影响结果。
    """
    draft = draft or {}
    # **不读 draft["summary"]。** 摘要在 2026-11-07 下线了，但老草稿行里还留着
    # 当年算好的那一份（实测 draft_8dfe01d36c95 里是 268 字，与 comment_short 同源）。
    # 只要这里还认它，发老草稿时就会把它塞进 payload 并把 auto_comment 打开，
    # 等于用户明明已经把摘要删掉了，发出去还是多一条评论——库里没清零路径，
    # 只能在出口这一处不认它（一处改动覆盖所有老行，不需要数据迁移）。
    content = compact_delta(strip_caution_lines(str(draft.get("content") or "")))
    return {
        "oid": draft.get("aid"),
        "oid_type": 0,
        "title": str(draft.get("title") or "视频笔记"),
        # 摘要键**必须发**（接口文档标的是"必要"），但发的是**正文开头**，不是老草稿
        # 里存的那份：那份是用户已经在界面上删掉的，认它等于把删掉的东西又发出去。
        # 之所以不干脆发空串：空串在接口里是"必填字段留空"，平台很可能直接 -400，
        # 而那正是"点了发布发不出去"的一种死法——本应用拿不到真站回执来赌这一把。
        # auto_comment 恒 0 ⇒ 这段预览只出现在笔记列表的预览位，不会被发成评论。
        "summary": note_preview(content),
        "content": content,
        # 真实客户端（BiliPai VideoNoteRepository）会带 cont_len；补上它，
        # 让平台按与真实客户端一致的形状计算正文长度。
        "cont_len": len(plain_text(content)),
        "cls": 1,
        "from": "save",
        "platform": "web",
        "publish": 1,
        "auto_comment": 0,
    }


def post_note(
    client: Any,
    draft: dict[str, Any],
    *,
    attempts: int = NOTE_ATTEMPTS,
    sleep: Any = None,
    on_retry: Any = None,
) -> dict[str, Any]:
    """发布一篇笔记。成功返回 ``{note_id, attempts, raw}``。

    只对 ``-509``（限流）重试；其余错误一律原样交回——验证码/敏感/重复这类
    重试一百次也一样，重试只会让风控更认定这是脚本。
    """
    import time as _time

    wait = sleep or _time.sleep
    payload = note_payload(draft)
    if not str(payload.get("content") or "").strip() or payload["content"].strip() == "[]":
        raise WBError("VALIDATION", "笔记正文是空的，先出一版总结")
    if not payload.get("oid"):
        raise WBError("CONFLICT", "这条草稿没有 aid，定位不到视频")
    body_bytes = form_body_bytes(payload, csrf=str(getattr(client, "csrf", "") or ""))
    if body_bytes > NOTE_BODY_MAX_BYTES:
        raise WBError(
            "VALIDATION",
            f"笔记正文超过 B站接口上限（约 20000 字节，当前 {body_bytes} 字节），请删减正文后再发",
        )

    last: BiliError | None = None
    for index in range(max(1, attempts)):
        try:
            data = client.post_form(NOTE_ADD, payload)
        except BiliError as exc:
            last = exc
            if exc.code in RETRYABLE_CODES and index < attempts - 1:
                pause = 3 * (index + 1)
                if on_retry:
                    on_retry(f"被限流（{exc.code}），{pause} 秒后重试")
                wait(pause)
                continue
            break
        else:
            note_id = ""
            if isinstance(data, dict):
                note_id = str(data.get("note_id") or "")
            return {"note_id": note_id, "attempts": index + 1, "raw": data}

    if last is None:
        raise WBError("BILI_ERROR", "发布失败，而且没拿到具体错误")
    raise WBError(
        "BILI_ERROR",
        note_error_text(last),
        detail=str(last),
        retryable=last.code in RETRYABLE_CODES,
    )


def withdraw_note(client: Any, *, aid: Any, note_id: str) -> dict[str, Any]:
    """撤回已发布的笔记（按 note_id 删，不是按 rpid）。"""
    if not note_id:
        raise WBError("CONFLICT", "这条没有 note_id，撤不回来")
    if not aid:
        raise WBError("CONFLICT", "这条草稿没有 aid，定位不到视频")
    try:
        client.post_form(NOTE_DEL, {"oid": aid, "note_id": str(note_id)})
    except BiliError as exc:
        raise WBError("BILI_ERROR", note_error_text(exc), detail=str(exc)) from exc
    return {"note_id": str(note_id), "aid": aid}
