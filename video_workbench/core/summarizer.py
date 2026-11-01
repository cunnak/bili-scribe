"""总结适配器（P2-07）：把我们的项目数据包装成 bili-scribe 的素材，交给它的总结器。

**为什么是"适配"而不是"重写"。** bili-scribe 的 summarizer 里有三件很难重做的事：
提示词里那几条反幻觉硬约束、长素材的分段预读（map/reduce，带磁盘缓存）、
以及角色的三轮回（自我批判 → 按批判修订）。这些是被真实使用打磨出来的，
重写等于把踩过的坑再踩一遍。所以这里只做三件事：

1. **把我们的字幕喂成它认识的 ``Bundle``**——素材类型标成 ``subtitle_ai``，
   带时间戳，这样它才允许写 ``timeline``。
2. **把 ``ChatClient`` 包成它认识的 ``LlmClient``**——它要 ``llm.cfg.model``
   和 ``chat(json_mode=True, note=...)``，我们两样都没有。
3. **把进度翻译成人话**——它的 stage 是 ``mapping/reducing/critiquing/revising``，
   直接甩给界面等于让用户看英文枚举。

总结结果是**草稿**（原则 7）：这里只负责产出一版并交给服务层入库，
发布、导出都不在这一层决定。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from bili_scribe.core import summarizer as bs_summarizer
from bili_scribe.core.collector import Bundle
from bili_scribe.core.roles import describe_roles as _describe_roles
from bili_scribe.core.roles import get_role
from bili_scribe.core.summarizer import (
    PreparedMaterial,
    Refinement,
    SummarizeError,
    Summary,
)

from video_workbench.app.errors import WBError
from video_workbench.infra.jsonfix import repair_json
from video_workbench.infra.llm import ChatClient, ChatError

#: 角色清单（顺序即下拉顺序）。``standard`` 不套方法论，其余会走三轮回。
ROLES = _describe_roles()
ROLE_KEYS = tuple(item["key"] for item in ROLES)
DEFAULT_ROLE = "standard"

#: 进度映射。bili-scribe 的 stage 只有四个，界面上要说得清"现在在干什么"。
#: 数值是**保守估计**：预读占大头的实测比例见 summarizer 的注释（约 38% 输入）。
STAGE_TEXT = {
    "mapping": "分段预读",
    "reducing": "生成总结",
    "critiquing": "自我批判",
    "revising": "按批判修订",
}
STAGE_PROGRESS = {
    "mapping": (0.05, 0.55),
    "reducing": (0.6, 0.8),
    "critiquing": (0.82, 0.88),
    "revising": (0.9, 0.97),
}


class SummaryLLM:
    """把 ``ChatClient`` 包成 bili-scribe 的 ``LlmClient`` 形状。

    两个必须对齐的细节：

    - ``cfg.model``：summarizer 会把它写进 ``Summary.model``，用户的版本列表里
      要能看见"这一版是哪个模型产出的"。
    - ``chat(json_mode=..., note=...)``：它靠 ``json_mode`` 要求结构化输出，
      靠 ``note`` 把重试、降级这类"发生过但没失败"的事情送到界面。
    """

    def __init__(self, client: ChatClient, *, model: str = "") -> None:
        self.client = client
        self.model = model or client.model
        self.cfg = _ModelCfg(model=self.model)
        self.notes: list[str] = []
        self.calls = 0
        # 片长与「全片时间轴索引」：最终那次 JSON 请求里往往只剩分段摘要，
        # 时间戳只覆盖开头一两段。片长不能从提示词里猜，得由调用方（run/refine）
        # 从项目真实时长与字幕里喂进来，否则时间线永远只写开头那十几分钟。
        self.duration_ms = 0
        self.timeline_index = ""
        # bili-scribe 的 `_coerce_timeline()` 结尾是 `return out[:20]`——它把时间线
        # 硬截到 20 条，且不能改它的业务代码。所以密度提升只能在这里留一份更长的，
        # 由 run()/refine() 在拿到 Summary 之后覆盖回去（见 `_apply_dense`）。
        self.dense_timeline: list[dict[str, str]] = []

    def chat(
        self,
        messages: Any,
        *,
        json_mode: bool = False,
        note: Callable[[str], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **_ignored: Any,
    ) -> str:
        sink = note or (lambda _message: None)
        self.calls += 1
        try:
            text = self.client.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        except ChatError as exc:
            # 有些兼容端点不认 response_format，直接 400。这不是"模型坏了"，
            # 是"它不认这个可选字段"，退回去再要一次就行——不能让用户因为
            # 一个可选字段就永远出不了总结。
            if json_mode and exc.code == "LLM_BAD_REQUEST":
                self.notes.append("这个模型不认 JSON 模式，已退回普通请求")
                sink("模型不支持 JSON 模式，改按普通请求重试")
                self.calls += 1
                text = self.client.chat(
                    messages, temperature=temperature, max_tokens=max_tokens)
            else:
                raise
        if json_mode:
            return self._json_or_retry(text, messages, sink, temperature, max_tokens)
        return text

    def _json_or_retry(self, text, messages, sink, temperature, max_tokens) -> str:
        """模型给的 JSON 只要还能读懂就修好交出去；真读不懂再问一次。

        为什么修：一份 8 段预读的总结，最后一步因为字符串里一个原始换行就整份失败，
        用户得从头再跑三分多钟。这类输出人眼看着完全正常，是解析器太严。
        """
        fixed = repair_json(text)
        if fixed:
            return self._finalize(fixed, messages, sink, temperature, max_tokens)
        # 截断：输出根本没写完（正文/时间线太长，预算在半个字符串里用完）。
        # 这种情况「原样重问」只会再截断一次——正确的动作是让它接着写。
        if _looks_truncated(text):
            sink("输出被截断了，正在接着写")
            merged = self._continue(text, messages, temperature, max_tokens)
            if merged:
                fixed = repair_json(merged)
                if fixed:
                    self.notes.append("输出被截断，接着写了一段才拿到完整 JSON")
                    return self._finalize(fixed, messages, sink, temperature, max_tokens)
                text = merged
        # 先试分批：截断是「装不下」，原样重问一次只会同样装不下（v7 现场两次都断在
        # 半句「**中国REITs与美国REITs出身差异。**」）。分批能过就不必再花一次重问。
        sink("一次装不下，改成正文与时间线分两次要，再拼起来")
        split_first = self._split_reply(messages, temperature, max_tokens, sink)
        if split_first:
            self.notes.append("单次输出装不下，改成正文与时间线分两次生成后合并")
            return self._finalize(split_first, messages, sink, temperature, max_tokens)
        sink("模型输出不是合法 JSON，已按更严格的要求重问一次")
        self.notes.append("模型输出的 JSON 无法修复，重问了一次")
        self.calls += 1
        retry_messages = list(messages) + [
            {"role": "assistant", "content": (text or "")[:4000]},
            {
                "role": "user",
                "content": (
                    "上面这段不是合法 JSON。请只输出一个 JSON 对象：不要解释文字、"
                    "不要 Markdown 围栏；字符串内部不要出现裸换行（换行写成 \n），"
                    "引用别人的话不要用英文双引号，改用「」或单引号。"
                    "字段与上一次保持一致。"
                ),
            },
        ]
        # 重问时把输出预算放大：第一次失败常常是正文太长被截断，
        # 原样再问一次只会再截断一次（真实现场：55 分钟视频的正文 3000+ 字、
        # 两小时视频的时间线一次就要 8k tokens）。
        budget = _json_budget(max_tokens)
        again = self.client.chat(
            retry_messages, temperature=temperature, max_tokens=budget, json_mode=True
        )
        fixed = repair_json(again)
        if fixed:
            return self._finalize(fixed, messages, sink, temperature, max_tokens)
        tail = (again or "").strip()[-40:]
        # 两次都装不下：把这一份大 JSON 拆成两次要（正文一次、时间线一次）再合并。
        # 两小时视频的真实现场就是这样——不是模型不会写，是单次输出装不下：
        # level2 正文一万六千字 + 全量时间线，一次 completion 到不了头。
        sink("一次装不下，改成正文与时间线分两次要，再拼起来")
        split = self._split_reply(messages, temperature, max_tokens, sink)
        if split:
            self.notes.append("单次输出装不下，改成正文与时间线分两次生成后合并")
            return self._finalize(split, messages, sink, temperature, max_tokens)
        hint = ""
        if not tail.endswith("}"):
            hint = ("（输出看起来被截断了——把「上下文与长素材」里的单次输出上限调大，"
                    "或换一个更听话的模型）")
        # 直接抛 WBError：SummarizeError 只在自己这一层的 run() 里被翻译成错误码，
        # 合集总览走的是 core/overview.py 的另一条路，漏出去就成了「内部错误」。
        raise WBError("LLM_BAD_RESPONSE", f"模型连续两次都没有给出可解析的 JSON{hint}：{tail}")

    def _split_reply(self, messages, temperature, max_tokens, sink) -> str:
        """正文与时间线分两次问，再把两份 JSON 拼成一个交出去。

        为什么不让模型自己「写短点」：`summary_md` 的长度是内容决定的，砍它等于砍质量；
        而 `timeline` 与正文是两种活（一个连贯叙述、一个逐条摘录），拆开问对质量
        几乎没有损失，却把单次输出从「装不下」变成「两次都宽裕」。
        正文那次明确不要 timeline；时间线那次带上正文摘要当上下文，只产时间线。
        """
        nonce = _short_nonce()
        body_messages = list(messages) + [
            {
                "role": "user",
                "content": (
                    "这一次分两步，这是第 1 步：只输出这些字段、一个 JSON 对象——"
                    "one_line、summary_md、key_points、caution、comment_short。"
                    "**不要输出 timeline**（时间线下一步单独要）。"
                    "除这几个键之外不要写别的键，不要解释文字，不要 Markdown 围栏。"
                    f"（分批标记 {nonce}-A）"
                ),
            }
        ]
        try:
            raw_body = self.client.chat(
                body_messages,
                temperature=temperature,
                max_tokens=_json_budget(max_tokens),
                json_mode=True,
            )
        except ChatError as exc:
            sink(f"正文这一步没成：{exc.message}")
            return ""
        self.calls += 1
        body = _loads_object(raw_body)
        if not body or not (body.get("summary_md") or body.get("one_line")):
            sink("正文这一步没给出可用的 JSON")
            return ""

        timeline = self._timeline_reply(
            messages, body, temperature, max_tokens,
            _timeline_target(messages, self.duration_ms), sink
        )
        body["timeline"] = timeline
        return json.dumps(body, ensure_ascii=False)

    def _timeline_reply(self, messages, body, temperature, max_tokens, least, sink) -> list:
        """单独要一轮时间线：只给这一项，输出短、几乎不会撞上限。

        `least` 是「至少几条」——成品里时间线的密度是按片长算的（见 `_timeline_target`），
        不按模型的自觉。要不到就返回空列表，让调用方决定降级还是保留原值。
        """
        digest = _plain(str(body.get("summary_md") or body.get("one_line") or ""))[:1200]
        want = f"至少 {int(least)} 条" if least else "最多 60 条"
        time_messages = list(messages) + [
            {"role": "assistant", "content": digest},
        ]
        marks = (self.timeline_index or "").strip()
        if marks:
            time_messages.append({
                "role": "user",
                "content": (
                    "这是全片的时间轴索引（每 60 秒取一条，格式「时间 内容」），"
                    "时间戳请从这些里面挑，覆盖到片尾：\n" + marks
                ),
            })
        time_messages.append({
            "role": "user",
            "content": (
                "只输出时间线，一个 JSON 对象，形如 "
                '{"timeline": [{"t": "03:12", "text": "这里讲了什么"}]}。'
                f"{want}、每条不超过 30 字，覆盖全片的关键转折与结论，"
                "前后两条之间不要隔太久；"
                "时间戳只能引用素材里真实出现过的。不要输出其它键，不要解释文字。"
                f"（时间线批次 {_short_nonce()}）"
            ),
        })
        try:
            raw = self.client.chat(
                time_messages,
                temperature=temperature,
                max_tokens=_json_budget(max_tokens, floor=4096),
                json_mode=True,
            )
        except ChatError as exc:
            sink(f"时间线这一步没成：{exc.message}")
            return []
        self.calls += 1
        return _loads_timeline(raw)

    def _finalize(self, fixed: str, messages, sink, temperature, max_tokens) -> str:
        """JSON 已经能解析了，最后再看一眼时间线的密度。

        真实现场：两小时视频的 v3 只给了 20 条时间线（平均 6 分钟一条），
        而用户要的是「完善时间线」。密度由片长决定才稳定，不能指望模型自觉，
        所以这里称一下：偏稀就单独补一轮（只产时间线，很便宜），补不动就保留原样。
        """
        data = _loads_object(fixed)
        if not data:
            return fixed
        rows = data.get("timeline") or []
        if len(rows) > 20:
            self.dense_timeline = rows
        target = _timeline_target(messages, self.duration_ms)
        if target and len(rows) < max(6, int(target * 0.6)):
            sink(f"时间线只有 {len(rows)} 条，按 {target} 条补一轮")
            extra = self._timeline_reply(
                messages, data, temperature, max_tokens, target, sink
            )
            if len(extra) > len(rows):
                data["timeline"] = extra
                self.dense_timeline = extra
                self.notes.append(f"时间线偏稀（{len(rows)} 条），补到 {len(extra)} 条")
                return json.dumps(data, ensure_ascii=False)
            self.notes.append(f"时间线补写没拿到更多条目（原 {len(rows)} 条）")
        return fixed

    def _continue(self, text, messages, temperature, max_tokens) -> str:
        """让它从断开处接着写，把两段拼起来。

        为什么不是「重问一遍」：一段两小时视频的时间线要 8k tokens，重问还是
        一样长、还是一样在同一个位置断。续写只补后半段，长度是原来的一半上下。
        """
        self.calls += 1
        cont_messages = list(messages) + [
            {"role": "assistant", "content": text or ""},
            {
                "role": "user",
                "content": (
                    "你上一条回复在 JSON 中途被长度限制截断了。请**从断开的位置接着写**，"
                    "只输出剩下的部分：不要重复已经写过的内容，不要重新开头，"
                    "不要解释，不要 Markdown 围栏。直接从被截断的那个值继续，"
                    "直到 JSON 完整结束。"
                ),
            },
        ]
        try:
            rest = self.client.chat(
                cont_messages,
                temperature=temperature,
                max_tokens=_json_budget(max_tokens),
                json_mode=True,
            )
        except ChatError:
            return ""
        if not rest:
            return ""
        return (text or "") + rest.strip()


class _ModelCfg:
    """``llm.cfg`` 的最小替身：summarizer 只读 ``model``。"""

    def __init__(self, model: str) -> None:
        self.model = model

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - 只是兜底
        raise AttributeError(name)


# ------------------------------------------------------------------ 素材组装


_TS_RE = re.compile(r"^\s*\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\]?")


def stamp(ms: int) -> str:
    """毫秒 → ``mm:ss``（超过一小时用 ``h:mm:ss``）。"""
    total = max(0, int(ms)) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def segments_text(segments: list[dict[str, Any]]) -> str:
    """字幕段 → 带时间戳的正文。

    时间戳必须保留：bili-scribe 只有在"素材里真有时间戳"时才允许模型写
    ``timeline``，一旦这里退化成纯文本，总结里的时间点就全没了。
    """
    lines: list[str] = []
    for seg in segments or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{stamp(seg.get('start_ms') or 0)}] {text}")
    return "\n".join(lines)


def build_bundle(
    project: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    danmaku: list[str] | None = None,
    platform_summary: str = "",
) -> Bundle:
    """项目 + 字幕 → bili-scribe 的 ``Bundle``。

    素材类型固定 ``subtitle_ai``：这段字幕是我们自己 ASR 出来的，不是平台字幕，
    也不是人工字幕。标错类型会让提示词里的可信度说明失真（模型会以为自己在读
    平台字幕，从而更少地写"素材未覆盖"）。
    """
    body = segments_text(segments)
    if not body.strip():
        raise WBError("CONFLICT", "这个项目还没有字幕，先转录再总结")
    hot = [str(item) for item in (danmaku or []) if str(item).strip()][:400]
    detail = (
        f"字幕来自本机 ASR，共 {len(segments)} 段，含时间戳。"
        if not hot
        else f"字幕来自本机 ASR，共 {len(segments)} 段，含时间戳；另附观众弹幕 {len(hot)} 条。"
    )
    return Bundle(
        bvid=str(project.get("bvid") or ""),
        aid=int(project.get("aid") or 0),
        cid=int(project.get("cid") or 0),
        page=int(project.get("page") or 1),
        page_count=int(project.get("page_count") or 1),
        title=str(project.get("title") or ""),
        desc=str(project.get("desc") or ""),
        up_name=str(project.get("up_name") or ""),
        up_mid=int(project.get("up_mid") or 0),
        # 项目表没有时长列，片长从字幕末条推（见 `_segment_end_ms`）。
        duration=int(round(_segment_end_ms(segments) / 1000.0)) or int(
            round(float(project.get("duration_sec") or 0))),
        tname=str(project.get("tname") or ""),
        tags=list(project.get("tags") or []),
        hot_replies=hot,
        platform_summary=platform_summary,
        source_kind="subtitle_ai",
        source_label="AI 转录字幕",
        source_trust="中（机器转录，可能有错字，时间戳可信）",
        source_detail=detail,
        body=body,
    )


def _mmss(ms: int) -> str:
    total = int(ms) // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def timeline_marks(segments: list[dict[str, Any]], *, step_ms: int = 60000,
                   limit: int = 140) -> str:
    """把字幕压成「每 60 秒一条」的时间轴索引，给补写时间线那一步当锚点。

    没有这份索引，模型只能从分段摘要里看到开头十几分钟的时间戳，写出来的时间线
    到那里就断——两小时视频的时间线末条停在 13:40 就是这么来的。
    """
    rows: list[str] = []
    next_at = 0
    for seg in segments or []:
        start = int(seg.get("start_ms") or 0)
        if start < next_at:
            continue
        text = " ".join(str(seg.get("text") or "").split())
        if not text:
            continue
        rows.append(f"{_mmss(start)} {text[:24]}")
        next_at = start + step_ms
        if len(rows) >= limit:
            break
    return "\n".join(rows)


def _segment_end_ms(segments: list[dict[str, Any]]) -> int:
    """字幕里最晚的结束时间＝真实片长。

    `wb_project` 表**没有时长列**（实测列：id/title/source_kind/bvid/aid/cid/page_no/
    up_mid/up_name/collection_id/collection_title/root_dir/stage/status/error/
    created_at/updated_at），所以 `project.get("duration_sec")` 恒为 0——片长只能
    从字幕本身拿，否则时间线密度永远按"不知道多长"处理。
    """
    end = 0
    for seg in segments or []:
        try:
            end = max(end, int(seg.get("end_ms") or 0))
        except (TypeError, ValueError):
            continue
    return end


def _apply_dense(summary: Any, llm: "SummaryLLM") -> Any:
    """把适配器里那份更密的时间线覆盖回 Summary。

    bili-scribe 的 `_coerce_timeline()` 结尾 `return out[:20]` 是硬上限，两小时的视频
    因此永远只剩 20 条。它的业务代码不动，覆盖放在这一层做。
    """
    dense = list(getattr(llm, "dense_timeline", None) or [])
    if len(dense) > len(getattr(summary, "timeline", None) or []):
        summary.timeline = dense
    return summary


def _attach_timeline(llm: "SummaryLLM", project: dict[str, Any],
                     segments: list[dict[str, Any]]) -> None:
    """把片长与全片索引接到适配器上（时间线密度靠它称重）。"""
    duration = _segment_end_ms(segments)
    if not duration:
        for key, scale in (("duration_ms", 1), ("duration_sec", 1000), ("duration", 1000)):
            try:
                value = int(float(project.get(key) or 0) * scale)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                duration = value
                break
    llm.duration_ms = duration
    llm.timeline_index = timeline_marks(segments)


# ------------------------------------------------------------------ 主流程


def _notify(ctx: Any) -> Callable[[str, str], None]:
    """把 bili-scribe 的 ``(stage, message)`` 翻成 job 的进度与阶段文案。"""

    state = {"stage": ""}

    def emit(stage: str, message: str) -> None:
        if stage != state["stage"]:
            state["stage"] = stage
        low, high = STAGE_PROGRESS.get(stage, (0.1, 0.9))
        ctx.progress(low if low == high else high, STAGE_TEXT.get(stage, message))
        if message:
            ctx.log(message)

    return emit


def run(
    project: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    client: ChatClient,
    cfg: Any,
    ctx: Any,
    role: str = "",
    danmaku: list[str] | None = None,
    platform_summary: str = "",
    model: str = "",
) -> dict[str, Any]:
    """跑一次总结。返回 ``{summary, usage, model, role, calls, notes, chars}``。"""
    bundle = build_bundle(project, segments, danmaku=danmaku,
                          platform_summary=platform_summary)
    llm = SummaryLLM(client, model=model)
    _attach_timeline(llm, project, segments)
    key = (role or getattr(cfg.llm, "role", "") or DEFAULT_ROLE)
    if key not in ROLE_KEYS:
        raise WBError("VALIDATION", f"没有这个角色：{key}")
    ctx.progress(0.02, "准备素材")
    try:
        summary = bs_summarizer.summarize(
            bundle, llm, cfg, progress=_notify(ctx), role_key=key)
    except SummarizeError as exc:
        raise WBError("LLM_BAD_RESPONSE", f"模型没给出可用的总结 JSON：{exc}") from exc
    except ChatError as exc:
        raise WBError(exc.code, exc.message, exc.detail) from exc

    summary = _apply_dense(summary, llm)
    role_obj = get_role(key)
    return {
        "summary": summary.to_dict(),
        "usage": {"calls": llm.calls},
        "model": summary.model or llm.model,
        "role": key,
        "role_name": role_obj.name,
        "calls": llm.calls,
        "notes": list(llm.notes),
        "chars": len(bundle.body),
    }


def refine(
    project: dict[str, Any],
    segments: list[dict[str, Any]],
    previous: dict[str, Any],
    requirement: str,
    *,
    client: ChatClient,
    cfg: Any,
    ctx: Any,
    role: str = "",
    danmaku: list[str] | None = None,
    model: str = "",
) -> dict[str, Any]:
    """按用户的话改一版。素材**仍然带着**——只给上一版会像传话游戏一样越改越远。"""
    demand = (requirement or "").strip()
    if len(demand) < 4:
        raise WBError("VALIDATION", "说一下你想怎么改（至少几个字）")
    if not previous:
        raise WBError("CONFLICT", "还没有上一版总结，先生成一次")
    bundle = build_bundle(project, segments, danmaku=danmaku)
    llm = SummaryLLM(client, model=model)
    _attach_timeline(llm, project, segments)
    key = role or str(previous.get("role") or DEFAULT_ROLE)
    if key not in ROLE_KEYS:
        key = DEFAULT_ROLE
    ctx.progress(0.05, "准备素材")
    try:
        summary = bs_summarizer.summarize(
            bundle,
            llm,
            cfg,
            progress=_notify(ctx),
            role_key=key,
            refinement=Refinement(
                requirement=demand,
                previous=_json(previous),
            ),
        )
    except SummarizeError as exc:
        raise WBError("LLM_BAD_RESPONSE", f"模型没给出可用的总结 JSON：{exc}") from exc
    except ChatError as exc:
        raise WBError(exc.code, exc.message, exc.detail) from exc
    summary = _apply_dense(summary, llm)
    return {
        "summary": summary.to_dict(),
        "usage": {"calls": llm.calls},
        "model": summary.model or llm.model,
        "role": key,
        "role_name": get_role(key).name,
        "calls": llm.calls,
        "notes": list(llm.notes),
        "chars": len(bundle.body),
        "requirement": demand,
    }


def prepare(
    project: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    client: ChatClient,
    cfg: Any,
    ctx: Any,
    cache: Any = None,
) -> PreparedMaterial:
    """只跑预读（长素材才真的发请求），给"先看看素材有多长"这类场景用。"""
    bundle = build_bundle(project, segments)
    llm = SummaryLLM(client)
    return bs_summarizer.prepare_material(bundle, llm, cfg, _notify(ctx), cache=cache)


# ------------------------------------------------------------------ 渲染


_TIMELINE_HEAD = "## 时间线"


def to_markdown(summary: dict[str, Any]) -> str:
    """↔ ``summary.md`` 落盘用的正文。

    为什么不直接存 ``summary_md`` 字段：一份总结里还有要点、时间线、注意事项、
    评论短版，只存中间那一段，别人拿到 md 会以为这就是全部。
    """
    data = summary or {}
    lines: list[str] = []
    one_line = str(data.get("one_line") or "").strip()
    if one_line:
        lines.append(f"> {one_line}")
        lines.append("")
    body = str(data.get("summary_md") or "").strip()
    if body:
        lines.append(body)
        lines.append("")
    points = [str(item).strip() for item in (data.get("key_points") or []) if str(item).strip()]
    if points:
        lines.append("## 要点")
        lines.extend(f"- {item}" for item in points)
        lines.append("")
    timeline = data.get("timeline") or []
    if timeline:
        lines.append(_TIMELINE_HEAD)
        for item in timeline:
            when = str(item.get("t") or "").strip()
            what = str(item.get("text") or "").strip()
            if what:
                lines.append(f"- `{when}` {what}" if when else f"- {what}")
        lines.append("")
    caution = str(data.get("caution") or "").strip()
    if caution:
        lines.append("## 注意")
        lines.append(caution)
        lines.append("")
    short = str(data.get("comment_short") or "").strip()
    if short:
        lines.append("## 评论用短版")
        lines.append(short)
        lines.append("")
    meta = []
    if data.get("role"):
        meta.append(f"角色：{data['role']}")
    if data.get("model"):
        meta.append(f"模型：{data['model']}")
    if data.get("chunks"):
        meta.append(f"分段：{data['chunks']}")
    if data.get("revise_note"):
        meta.append(f"本次改动：{data['revise_note']}")
    if meta:
        lines.append("---")
        lines.append("　".join(meta))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _json(data: Any) -> str:
    import json

    return json.dumps(data or {}, ensure_ascii=False)


#: 给服务层与前端用的角色清单（带 passes，界面要标"三轮回"）。
def roles_view() -> list[dict[str, Any]]:
    return [
        {
            "key": item["key"],
            "name": item["name"],
            "tagline": item.get("tagline", ""),
            "passes": item.get("passes", 1),
        }
        for item in ROLES
    ]

#: 重问/续写时的输出预算下限。一份两小时视频的时间线实测要 8k 上下，
#: 而 llm.max_tokens 的默认值只有 4096——按配置原样重问必然再截断。
MIN_JSON_TOKENS = 12288


def _json_budget(max_tokens: int | None, floor: int = MIN_JSON_TOKENS) -> int:
    """重问、续写、分批各步用多少输出预算：取「配置的两倍」与 floor 的较大者，上限 16384。"""
    base = int(max_tokens or 0)
    return min(max(int(floor), base * 2), 16384)


def _short_nonce() -> str:
    """分批请求的标记，只为了让日志里能分辨「同一次打磨的两次分批」。"""
    return uuid.uuid4().hex[:6]


def _loads_object(text: str) -> dict[str, Any]:
    """把一段（可能是坏 JSON 的）回复读成 dict；读不出就返回空 dict。"""
    fixed = repair_json(text)
    if fixed:
        try:
            value = json.loads(fixed)
        except (ValueError, TypeError):
            value = None
        if isinstance(value, dict):
            return value
    return {}


def _loads_timeline(text: str) -> list[dict[str, str]]:
    """时间线那一步的回复 → `[{"t": .., "text": ..}]`；只认带 t/text 的条目。"""
    value: Any = None
    fixed = repair_json(text)
    if fixed:
        try:
            value = json.loads(fixed)
        except (ValueError, TypeError):
            value = None
    if isinstance(value, dict):
        value = value.get("timeline")
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        at = str(item.get("t") or item.get("time") or "").strip()
        text_value = str(item.get("text") or item.get("desc") or "").strip()
        if at and text_value:
            rows.append({"t": at, "text": text_value})
    return rows


def _plain(text: str) -> str:
    """把 Markdown 压成一段连续文字，用来当时间线那一步的上下文摘要。"""
    return re.sub(r"\s+", " ", (text or "").replace("#", " ").replace("*", " ")).strip()


_TIME_RE = re.compile(r"(\d{1,3}):([0-5]\d)(?::([0-5]\d))?")


def _estimate_minutes(messages) -> int:
    """从提示词里能看到的最后一个时间戳估片长（分钟）。

    bili-scribe 会把带时间戳的字幕整份塞进提示词，所以最后一条时间戳≈片尾。
    估不出来就返回 0——宁可不算目标，也不要按错的分母硬凑条目。
    """
    longest = 0
    for item in messages or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str):
            continue
        for match in _TIME_RE.finditer(content):
            first, second, third = match.group(1), match.group(2), match.group(3)
            seconds = (
                int(first) * 3600 + int(second) * 60 + int(third)
                if third is not None
                else int(first) * 60 + int(second)
            )
            longest = max(longest, seconds)
    return longest // 60


def _timeline_target(messages, duration_ms: int = 0) -> int:
    """时间线该有多少条：半小时一条上下，夹在 20–60 之间；短片返回 0（不干预）。

    片长优先用调用方喂进来的真实时长。**不能只靠 ``messages`` 里能看到的时间戳**：
    最终那次 JSON 请求里常常只剩分段摘要，时间戳只覆盖开头十几分钟，按它算分母
    会得出"片长 13 分钟、20 条就够"的自洽假象——两小时视频因此永远只有 20 条。
    """
    minutes = int(duration_ms) // 60000 if int(duration_ms or 0) > 0 else _estimate_minutes(messages)
    if minutes < 10:
        return 0
    return max(20, min(60, int(round(minutes / 2.0))))


def _looks_truncated(text: str) -> bool:
    """输出是不是被长度限制截断在半路上。

    判据只看形状不看语义：去掉空白后不以 } 结尾，就说明这个对象没写完。
    反例是「模型压根没写 JSON」，该重问而不是续写——那种文本不会以 { 开头。
    """
    body = (text or "").strip()
    if not body:
        return False
    if body.endswith("}"):
        return False
    return "{" in body
