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

import re
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
            return fixed
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
        # 重问时把输出预算放大一倍：第一次失败常常是正文太长被截断，
        # 原样再问一次只会再截断一次（真实现场：55 分钟视频的正文 3000+ 字）。
        budget = None if max_tokens is None else min(int(max_tokens) * 2, 16384)
        again = self.client.chat(
            retry_messages, temperature=temperature, max_tokens=budget, json_mode=True
        )
        fixed = repair_json(again)
        if fixed:
            return fixed
        tail = (again or "").strip()[-40:]
        hint = ""
        if not tail.endswith("}"):
            hint = ("（输出看起来被截断了——把「上下文与长素材」里的单次输出上限调大，"
                    "或换一个更听话的模型）")
        # 直接抛 WBError：SummarizeError 只在自己这一层的 run() 里被翻译成错误码，
        # 合集总览走的是 core/overview.py 的另一条路，漏出去就成了「内部错误」。
        raise WBError("LLM_BAD_RESPONSE", f"模型连续两次都没有给出可解析的 JSON{hint}：{tail}")


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
        duration=int(round(float(project.get("duration_sec") or 0))),
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
