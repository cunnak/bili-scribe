"""总结适配器（P2-07）：把我们的项目数据包装成 ``Bundle``，交给自带的总结方法论。

**为什么是"适配"而不是"重写"。** 提示词里那几条反幻觉硬约束、长素材的分段预读
（map/reduce，带磁盘缓存）、以及角色的三轮回（自我批判 → 按批判修订），都是被真实
使用打磨出来的，重写等于把踩过的坑再踩一遍。所以方法本体是
``video_workbench/core/summary/`` 里那份与上游逐字节一致的副本，这里只做三件事：

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

import copy
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from bili_scribe.core.collector import Bundle

# 方法论本体在 video_workbench/core/summary/：那份副本与 bili_scribe 逐字节一致
# （只换了 import 段），角色字典/默认角色/退役角色由它的 __init__ 权威层给出。
from video_workbench.core.summary import (
    DEFAULT_ROLE,
    RETIRED_ROLES,
    PreparedMaterial,
    Refinement,
    SummarizeError,
    Summary,
    UnknownRole,
    describe_roles,
    get_role,
    role_label,
)
from video_workbench.core.summary import summarizer as bs_summarizer
# 显示名一律**经这个模块属性现取**（下面 `role_label(...)` 的三个调用点都是），不用 import
# 时绑进来的那个别名：别名在导入时就固定了函数对象，谁改了权威层的 `role_label`（或测试
# monkeypatch 它）都传不到这里，于是「改 owner 的输出，下游必须跟着变」这条行为锁会假绿。
# 别名本身留在上面的 from-import 清单里不动，那是 `summarizer.role_label` 这种取法的入口
# （`tests/wb/test_summary.py:558` 就在这么取）。副作用要说清：改成走模块属性之后，
# **patch 这个别名不再影响下面那几个调用点**，要换输出请 patch 权威层 `summary.role_label`。
from video_workbench.core import summary as summary_pkg
from video_workbench.core import prompt_source

from video_workbench.app.errors import WBError
from video_workbench.infra.jsonfix import repair_json
from video_workbench.infra.llm import ChatClient, ChatError

#: 角色清单（顺序即下拉顺序）。全部是方法论角色；``standard`` 已退役，不在清单里。
ROLES = describe_roles()
ROLE_KEYS = tuple(item["key"] for item in ROLES)

#: 对账开关：**默认开**。开着时适配层不做三件事——不抬输出预算（不 12288、不翻倍、
#: 不封 16384）、不抢救坏 JSON（修复→续写→分两次要→严格重问那套阶梯整条不走）、
#: 一次调用只发一次请求，让 ``video_workbench.core.summary`` 那份副本按上游语义跑，
#: 两边可以逐请求、逐字段比对。
#: 设 ``WB_SUMMARY_PARITY_STRICT=0``（或 false/off）才恢复 WB 的加固。这不是可有可无的
#: 保险：实测一天的日志里「已按更严格的要求重问一次」命中 7 次、「输出被截断了，正在
#: 接着写」5 次，说明坏 JSON 是真会发生——默认严格就默认失败，所以失败时要把开关念出来
#: （见 `_strict_hint`）。
#: 唯一不受这个开关影响的是「① 时间线补密」：有密时间线就覆盖 bs 的 20 条上限，
#: 没有就原样返回，一次额外请求都不发（见 `_apply_dense`）。
PARITY_ENV = "WB_SUMMARY_PARITY_STRICT"

#: 显式关掉对账模式的写法；除此以外的任何值（包括没设）都算严格档。
PARITY_OFF_VALUES = frozenset({"0", "false", "off"})


def _parity_strict() -> bool:
    """默认严格；只有显式写 0/false/off 才宽松。

    每次调用都现读环境变量，不在 import 时缓存（测试要在同一进程里来回切）。
    """
    return str(os.environ.get(PARITY_ENV, "")).strip().lower() not in PARITY_OFF_VALUES


def extra_prompt_on(cfg: Any, explicit: "bool | None" = None) -> bool:
    """附加提示词该不该接进去：显式入参优先，其次配置 ``llm.extra_prompt``（默认关）。

    这里只**取值**，不判断内容——拼不拼、拼在哪一段，全在 ``prompt_source`` 一处。
    默认关的理由写在那份模块的 docstring 里：`大概率管用 .txt` 是对抗性双角色提示词，
    和总结要的 JSON 契约正面冲突，开着会拖垮产出，所以开关交给人。
    """
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(getattr(cfg, "llm", None), "extra_prompt", False))


def _strict_hint(message: str) -> str:
    """严格档（默认）里坏 JSON 直接失败，得告诉用户是谁关掉了抢救。

    前半句是既有用例钉着的文案，一个字不动，只在末尾追加可操作提示。宽松档里这条
    失败是整套阶梯都用尽之后的结果，再提开关就是误导，所以不追加。
    """
    if not _parity_strict():
        return message
    return (f"{message} 对账模式（默认）不抢救坏 JSON；"
            f"需要抢救请设 {PARITY_ENV}=0 后重试。")


def resolve_role(explicit: "str | None",
                 fallback: "str | None" = None) -> tuple[str, str]:
    """取总结角色的**唯一**去处。返回 ``(role_key, notice)``。

    优先级是「调用方这次显式传的 > 配置或历史存的 > 默认角色」，但两类来源的
    待遇不一样——这是整段逻辑里唯一需要判断的地方：

    - 两个都空                    → ``(DEFAULT_ROLE, "")``
    - 选中的 key 可用              → ``(key, "")``
    - 选中的 key **只来自 fallback 且已退役** → ``(DEFAULT_ROLE, 中文说明)``。
      配置文件里或历史行里躺着退役值是**脏数据**，不是用户此刻的意图；用它把
      人挡在门外，等于拿历史惩罚现在的人。换默认角色，但必须把 notice 写进日志。
    - 用户**显式提交**的退役值，或任何**未知值** → ``WBError("VALIDATION", 权威层原话)``。
      显式提交＝意图，悄悄改写就是替用户改了主意；配置里拼错的角色名也不能静默
      修正成默认角色，否则用户永远不知道自己配的那一行没生效。

    什么算 explicit（别自查，规则在这）：**只有用户这次刚提交的值**。RPC 入参
    ``payload.role`` 是 explicit；``cfg.llm.role``、历史版本存的 ``role``、job 重放
    时从已落库 payload 里读出来的 role，全都是 fallback。
    """
    wanted = str(explicit or "").strip()
    from_fallback = False
    if not wanted:
        wanted = str(fallback or "").strip()
        from_fallback = True
    if not wanted:
        return DEFAULT_ROLE, ""
    try:
        role_obj = get_role(wanted)
    except UnknownRole as exc:
        if from_fallback and wanted in RETIRED_ROLES:
            return DEFAULT_ROLE, (f"{exc}；本次按默认角色"
                                  f"「{summary_pkg.role_label(DEFAULT_ROLE)}」")
        raise WBError("VALIDATION", str(exc)) from exc
    return role_obj.key, ""


def configured_role_issue(role_value: "str | None") -> tuple[str, str]:
    """把配置里存着的默认角色翻成「能不能直接用」，给设置页显示。返回 ``(脏值, 说明)``。

    值能直接用时返回 ``("", "")``。**这里不建第二条判定链**——判断仍然只在
    ``resolve_role``：它给出 notice 就是退役值，它抛 VALIDATION 就是未知值，其余算干净。
    为什么不直接把 ``resolve_role`` 的结果丢给界面：配置页的作用正是让用户把这一行改对，
    未知值在那里会报错——一报错用户就没有改的地方了，所以这一页只取它的**结论**、
    不取它的**异常**。措辞按两种情况分开（退役 ≠ 拼错，用户要做的事不一样）。
    """
    value = str(role_value or "").strip()
    if not value:
        return "", ""
    try:
        _key, notice = resolve_role("", value)
    except WBError as exc:
        return value, (f"配置里的默认角色「{value}」不是可用角色：{exc.message}，"
                       f"请改成下面列表里的一个")
    if notice:
        return value, (f"配置里的默认角色「{value}」已停用，"
                       f"生成时会自动按「{summary_pkg.role_label(DEFAULT_ROLE)}」走")
    return "", ""


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

    def __init__(self, client: ChatClient, *, model: str = "", cfg: Any = None) -> None:
        self.client = client
        self.model = model or client.model
        self.cfg = _ModelCfg(model=self.model)
        # 配置对象只为对账模式服务（要把请求上限还原成 cfg.llm.max_tokens）；
        # 普通路径不看它，行为与改动前一致。
        self.app_cfg = cfg
        self.notes: list[str] = []
        self.calls = 0
        # 计时从建 llm 起算：总结的耗时几乎全在模型上，
        # 而这里正是「这批请求开始」的位置。
        self.started_at = time.monotonic()
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
        if _parity_strict():
            # 对账模式：一次调用只发一次请求——不修 JSON、不续写、不拆分、不重问，
            # 也不做「端点不认 response_format 就退回普通请求」那层兜底。
            # 正文原样交出去，能不能解析由方法论那一层（``_extract_json``）判失败。
            reply = self.client.chat(
                messages,
                temperature=temperature,
                max_tokens=self._parity_max_tokens(max_tokens),
                json_mode=json_mode,
            )
            if json_mode:
                # 只读地数一遍这次回复里到底有几条时间线，然后原样交出正文。
                # 多发一个请求都没有，逐请求一致一个字都没破。
                self._capture_dense(reply)
            return reply
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

    def _parity_max_tokens(self, max_tokens: int | None) -> int | None:
        """对账模式下这次请求该给多少输出上限：按配置原样，不抬不封。

        上游 ``summarize()`` 调 ``llm.chat`` 时根本不写 ``max_tokens``，它指望端点
        按 ``cfg.llm.max_tokens`` 走；我们的 ``ChatClient`` 在 None 时干脆不发这个字段，
        那两边就不是同一个请求了。所以这里把配置值补上——仍然不抬到 12288、不翻倍。
        配置读不到（0）就不发这个字段，与上游一致。
        """
        if max_tokens is not None:
            return _json_budget(max_tokens)
        llm_cfg = getattr(self.app_cfg, "llm", None)
        configured = int(getattr(llm_cfg, "max_tokens", 0) or 0)
        return configured or None

    def _capture_dense(self, text: str) -> None:
        """严格档里 ① 的唯一数据来源：把这次回复真实的时间线条数记下来。

        为什么这一步不要多问一轮：模型给了几条，字节已经在手上了。丢掉它们的是
        副本 ``_coerce_timeline()`` 结尾那句 ``return out[:20]``，它只作用在交给
        方法论那一层的解析结果上，管不到适配器自己看的这份原文。所以「保留 ①」
        只需要在这里数一遍，``_apply_dense`` 覆盖时就有东西可用。

        三条边界，都是刻意的：
        1. 读不出就当没事发生——``_loads_object`` 返回空 dict 即返回。不许抛、不许改
           正文、不许因此再发一次请求；成败判据仍然归副本的 ``_extract_json()``。
        2. 只有真的多于 20 条才记，等于或少于都保持空：不截断的语义不能被「记一下」改坏。
        3. **存原文，不清洗**——清洗只在 ``_apply_dense()`` 里做一次（那里用副本的
           ``_coerce_text`` 洗成 ``t``/``text`` 同构条目，洗完再比条数）。一处存、一处洗：
           这儿也洗一遍、覆盖前再洗一遍，等于同一份数据两套时序，将来两边规则一漂移
           就说不清哪份是真的。为什么不干脆在这儿洗完存下来：这道门只负责「原文里到底
           有 20 条还是 25 条」，宽松档 ``:403`` 记的也是原文，两边存同一种东西才谈得上
           「语义一致」；在这儿洗会逼着宽松档也跟着洗，而那条路今天已经有自己的时序。

        宽松档不走这里：它照旧由 ``_finalize()``（``:403``/``:412``）记同一件事。
        """
        data = _loads_object(text)
        if not data:
            return
        rows = data.get("timeline") or []
        if isinstance(rows, list) and len(rows) > 20:
            self.dense_timeline = rows

    def _json_or_retry(self, text, messages, sink, temperature, max_tokens) -> str:
        """模型给的 JSON 只要还能读懂就修好交出去；真读不懂再问一次。

        为什么修：一份 8 段预读的总结，最后一步因为字符串里一个原始换行就整份失败，
        用户得从头再跑三分多钟。这类输出人眼看着完全正常，是解析器太严。

        为什么这里要把 None 换成配置里的预算（T56-1）：上游方法论调 ``llm.chat`` 时
        **压根不写 ``max_tokens``**，于是这条链收到的永远是 None，而下面四个消费点
        （``_finalize`` 重问、``_continue`` 续写、``_split_reply`` 分批、
        ``_timeline_reply`` 只要时间线）都拿 ``_json_budget(None)`` 算预算——宽松档
        那是硬编码下限 12288，而同一轮的主请求实际按配置走 32768，**重问比主请求还
        少 20480**，反而更容易再被截断。task-53 给 ``_json_budget`` 加的下界救不了
        这条路，因为传进去的 base 是 None 而不是配置值。
        只在**这一个入口**解析：主请求不动（它照旧按 None 走 ``ChatClient`` 自己的
        配置兜底），严格档更是不进这个函数（``chat()`` 在对账分支就 return 了），
        所以改动面只有「宽松档的补救请求」这一格。
        """
        if max_tokens is None:
            max_tokens = _request_max_tokens(self.app_cfg)
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
        # WB 目前唯一字幕来源是本机 ASR；一旦支持导入外部 srt/人工整稿，这里必须改成按来源判定，
        # 否则提示词里的可信度说明失真。
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


def _clean_dense(rows: list[Any]) -> list[dict[str, str]]:
    """把原文时间线洗成与副本同构的条目。

    规则**逐条对齐**副本 `_coerce_timeline()`（`core/summary/summarizer.py:168-183`），
    只去掉最后那句 `[:20]`：清洗只做清洗，上限是 ① 要突破的东西。不排序、不去重、
    不补时间戳——补时间戳要读全片索引，那是 `_timeline_reply` 那条路的事，混进来会让
    「① 只保住条数」这条命令变成「① 重新编一遍时间线」。

    为什么不在这儿另写一份 strip：文本取值全部走副本的 `_coerce_text()`。副本将来改
    语义，这里跟着变；自己抄一份就是第二套真相，而这轮一直在收的就是这种重复。
    """
    out: list[dict[str, str]] = []
    for item in rows or []:
        if isinstance(item, str):
            text = bs_summarizer._coerce_text(item)
            if text:
                out.append({"t": "", "text": text})
            continue
        if not isinstance(item, dict):
            continue
        text = bs_summarizer._coerce_text(item.get("text") or item.get("content"))
        if not text:
            continue
        out.append({"t": bs_summarizer._coerce_text(item.get("t") or item.get("time")),
                    "text": text})
    return out


def _apply_dense(summary: Any, llm: "SummaryLLM") -> Any:
    """把适配器里那份更密的时间线覆盖回 Summary。**不受对账模式开关影响**。

    bili-scribe 的 `_coerce_timeline()` 结尾 `return out[:20]` 是硬上限，两小时的视频
    因此永远只剩 20 条。它的业务代码不动，覆盖放在这一层做。

    密时间线有两个来源，两边记的是同一件事：
    - 严格档（默认）：`chat()` 拿到回复后 `_capture_dense()` 只读捕获原文里真实的
      条数（`len(rows) > 20` 才记），一次额外请求都不发。
    - 宽松档：`_finalize()` 在模型自报 >20 条时顺手记下（`:403`），或补写那一轮的
      结果（`:412`）。
    两个来源存的都可能是**原文条目**（模型爱用 `content`/`time` 而不是 `text`/`t`），
    所以覆盖前先 `_clean_dense()` 洗成副本同构——`to_markdown()` 与前端只读 `t`/`text`
    （渲染段 `:866-867`），未清洗的条目落进去，用户看到的时间线整段读不出内容。
    清洗会丢掉空 text 的条目，所以**洗完再比条数**：洗完不比已有的多就原样返回，
    不许为了「保条数」把洗不动的垃圾塞进去。
    两边都没有更密的条目时这里不成立，summary 原样返回。拍板的是「对账设默认，但
    保留 ①」，所以这道门不能加回 `if not _parity_strict()`：那等于把用户点名要保留
    的东西塞回开关里。
    """
    dense = _clean_dense(list(getattr(llm, "dense_timeline", None) or []))
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


def _chunks_of(summary: Any) -> int:
    value = getattr(summary, "chunks", 0) if summary is not None else 0
    if isinstance(value, (list, tuple, dict, set)):
        return len(value)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_of(llm: "SummaryLLM", summary: Any = None) -> dict[str, Any]:
    """一次总结的开销账目。

    字段名跟 bili-scribe 的用量面板对齐（输入/输出/调用/耗时/预读段数），
    这样两边看起来是同一件事，不用记两套说法。
    """
    client = getattr(llm, "client", None)
    prompt = int(getattr(client, "prompt_tokens", 0) or 0)
    completion = int(getattr(client, "completion_tokens", 0) or 0)
    total = int(getattr(client, "total_tokens", 0) or 0) or (prompt + completion)
    started = float(getattr(llm, "started_at", 0.0) or 0.0)
    seconds = round(max(0.0, time.monotonic() - started), 1) if started else 0.0
    return {
        "calls": int(llm.calls or 0),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "seconds": seconds,
        # bili-scribe 的 ``Summary.chunks`` 是段数（int），但测试里的假对象
        # 有时给列表；两种都认，认不出就按 0。
        "chunks": _chunks_of(summary),
    }


# -------------------------------------------------------------------- 预读复用
#
# 镜像 bili_scribe/app/service.py:252 的 `RefineContext` 与 :301/:347/:356 那套内存字典。
# 磁盘那层缓存在 `video_workbench/infra/notecache.py`（按内容寻址、跨任务跨进程），
# 这里这层仍然要留：同一版反复打磨时素材一个字没变，命中内存比命中磁盘更快、也不碰盘。
# 两层堵的是同一笔钱——bs `core/summarizer.py:392-394` 实测：4 小时视频 7 段预读占一次
# 总结 **38%** 的输入（32,663 tokens）；同一个 bvid 被重开成第二个任务时，第二遍那 13 万
# prompt tokens 本可以一分不花。


@dataclass
class RefineContext:
    """一次迭代打磨所需的上下文（对应 bs 的同名 dataclass）。

    ``bundle.body`` **已经被换成分段笔记**，不是素材原文——留在内存里的始终是模型写出来
    的摘要，第三方原文既不落库也不留着。元数据（标题、UP 主、时长、标签、来源）照常带着：
    归约提示词要用它们，而且都是很小、不敏感的字段。
    """

    bundle: Bundle
    material: PreparedMaterial


#: 内存里最多留几条预读结果。取 bs 的 200，理由照它 :266-282 那段实测：一条 ≈39 KB
#: （最坏 94 KB），200 条 ≈7.7–18 MB；原来写的是 20，**估错了约三个数量级**——20 条省下
#: 不到 1 MB 内存，代价是早先那条视频想再改一次就得整条重跑（4 小时视频 = 86,428 tokens，
#: 是真花钱）。内存几乎免费、重跑很贵，这个不对称就是取 200 的理由。
MAX_PREPARED = 200

_prepared: dict[str, RefineContext] = {}
_prepared_lock = threading.Lock()


def remember_prepared(key: str, context: RefineContext) -> None:
    """存下这次任务的预读结果，供后续「再改一版」复用。超出上限就淘汰最早的一条。"""
    if not key:
        return
    with _prepared_lock:
        _prepared.pop(key, None)  # 先删再插，让它排到队尾
        _prepared[key] = context
        while len(_prepared) > MAX_PREPARED:
            _prepared.pop(next(iter(_prepared)))


def get_prepared(key: str) -> RefineContext | None:
    """读预读结果。叫 get 不叫 take：**它不删除**——打磨是反复的，同一份笔记要支撑很多轮
    修订，每轮都取走的话第二次就没了。"""
    if not key:
        return None
    with _prepared_lock:
        return _prepared.get(key)


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
    cache: Any = None,
    task_key: str = "",
    extra_prompt: "bool | None" = None,
) -> dict[str, Any]:
    """跑一次总结。返回 ``{summary, usage, model, role, calls, notes, chars}``。

    ``cache`` 是磁盘预读缓存（``infra/notecache.NoteCache``），``task_key`` 是内存复用那层
    的钥匙——WB 里就是 ``project_id``，对应 bs 的 ``task_id``。两个都不传就是老行为：每次都
    重新预读；传了以后长素材第二次跑这段一个请求都不发。

    ``extra_prompt`` 覆盖配置里的 ``llm.extra_prompt``（默认关）。它按**请求**生效：
    只有归约那一段被包在开关里，预读不吃它（预读用的是 MAP_SYSTEM，与角色无关）。
    """
    bundle = build_bundle(project, segments, danmaku=danmaku,
                          platform_summary=platform_summary)
    llm = SummaryLLM(client, model=model, cfg=cfg)
    _attach_timeline(llm, project, segments)
    key, notice = resolve_role(role, getattr(cfg.llm, "role", ""))
    if notice:
        # 退役值来自配置（用户从没选过它），换默认角色并把原因写进日志。
        # 判定规则不在这里，见 resolve_role。
        ctx.log(notice)
    role_obj = get_role(key)
    ctx.progress(0.02, "准备素材")
    notify = _notify(ctx)
    try:
        # 预读单独跑一次，结果连同一份「笔记版 bundle」留在内存里，后续「再改一版」直接
        # 复用（见 refine()）。缓存的是**笔记版**：body 已被换成分段笔记，素材原文不进来。
        # 传 cache 是为了跨任务复用——同一个视频重开成第二个项目时，这一整段预读直接命中
        # 磁盘、一次模型调用都不发。
        material = bs_summarizer.prepare_material(
            bundle, llm, cfg, progress=notify, cache=cache)
        note_bundle = copy.copy(bundle)
        note_bundle.body = material.body
        remember_prepared(task_key, RefineContext(bundle=note_bundle, material=material))
        # 附加提示词按请求生效（contextvar，不是模块级布尔）：batch_worker 并发跑两个
        # job、一个开一个关时，两边的 system 各自正确。with 只包住归约那一段——
        # 上面的预读用的是 MAP_SYSTEM，与角色无关，不该被这个开关影响。
        with prompt_source.use_extra_prompt(extra_prompt_on(cfg, extra_prompt)):
            summary = bs_summarizer.summarize(
                bundle, llm, cfg, progress=notify, role_key=role_obj.key,
                prepared=material)
    except SummarizeError as exc:
        raise WBError("LLM_BAD_RESPONSE",
                      _strict_hint(f"模型没给出可用的总结 JSON：{exc}")) from exc
    except ChatError as exc:
        raise WBError(exc.code, exc.message, exc.detail) from exc

    # ① 时间线补密：无条件生效，不受对账模式开关影响（拍板的是「设成默认，但保留①」）。
    # 密条目有两个来源，两边记的是同一件事：严格档由 `chat()` 的 `_capture_dense()`
    # 只读捕获原文条数（零额外请求），宽松档由 `_finalize()` 记下。都没有更密的
    # 条目时这里空转一次：不发额外请求，也不改 summary。
    summary = _apply_dense(summary, llm)
    return {
        "summary": summary.to_dict(),
        "usage": _usage_of(llm, summary),
        "model": summary.model or llm.model,
        "role": role_obj.key,
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
    cache: Any = None,
    task_key: str = "",
    extra_prompt: "bool | None" = None,
) -> dict[str, Any]:
    """按用户的话改一版。素材**仍然带着**——只给上一版会像传话游戏一样越改越远。

    命中 ``task_key`` 的内存预读结果时**不重跑预读**（对应 bs 的 `run_refine`：
    「不重新采集、不重新预读——素材笔记直接从 ctx 拿」）。这就是迭代打磨的全部意义：
    同一份素材改很多版，只付一次预读的钱（4 小时视频省下 32,663 tokens，占一次总结
    输入的 38%）。没命中就看磁盘缓存 ``cache``，还没有才真的重跑一次预读。
    """
    demand = (requirement or "").strip()
    if len(demand) < 4:
        raise WBError("VALIDATION", "说一下你想怎么改（至少几个字）")
    if not previous:
        raise WBError("CONFLICT", "还没有上一版总结，先生成一次")
    bundle = build_bundle(project, segments, danmaku=danmaku)
    llm = SummaryLLM(client, model=model, cfg=cfg)
    _attach_timeline(llm, project, segments)
    key, notice = resolve_role(role,
                               str(previous.get("role") or "")
                               or getattr(cfg.llm, "role", ""))
    if notice:
        # 上一版很可能就是退役角色生成的（历史行里存着 standard）。这里不静默换：
        # 按默认角色重做，同时把「为什么换了角色」写进日志，用户看得见。
        # 判定规则不在这里，见 resolve_role。
        ctx.log(f"{notice}重做")
    role_obj = get_role(key)
    ctx.progress(0.05, "准备素材")
    notify = _notify(ctx)
    try:
        context = get_prepared(task_key)
        if context is not None:
            # 内存里已经有这版素材的笔记：直接拿，一个预读请求都不发。
            # 传进归约的是**笔记版 bundle**（body 已被换成分段笔记，素材原文不进来），
            # 与 bs `run_refine` 传的 `ctx.bundle` 同一个东西。
            material = context.material
            work_bundle = context.bundle
        else:
            material = bs_summarizer.prepare_material(
                bundle, llm, cfg, progress=notify, cache=cache)
            work_bundle = copy.copy(bundle)
            work_bundle.body = material.body
            remember_prepared(task_key, RefineContext(bundle=work_bundle, material=material))
        # 与 run() 同一个开关、同一个理由：打磨也要吃到当前该不该接附加提示词。
        with prompt_source.use_extra_prompt(extra_prompt_on(cfg, extra_prompt)):
            summary = bs_summarizer.summarize(
                work_bundle,
                llm,
                cfg,
                progress=notify,
                role_key=role_obj.key,
                prepared=material,
                refinement=Refinement(
                    requirement=demand,
                    previous=_json(previous),
                ),
            )
    except SummarizeError as exc:
        raise WBError("LLM_BAD_RESPONSE",
                      _strict_hint(f"模型没给出可用的总结 JSON：{exc}")) from exc
    except ChatError as exc:
        raise WBError(exc.code, exc.message, exc.detail) from exc
    # ① 同 run()：补密不受对账模式影响，dense 为空时逐字节原样返回。
    summary = _apply_dense(summary, llm)
    return {
        "summary": summary.to_dict(),
        "usage": _usage_of(llm, summary),
        "model": summary.model or llm.model,
        "role": role_obj.key,
        "role_name": role_obj.name,
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
    llm = SummaryLLM(client, cfg=cfg)
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
        # 现役角色沿用 key：导出正文里的这一行要和库里存的记录对得上。
        # 历史行里的退役/未知 key 不能只甩一个界面上已经找不到的字符串——
        # 既不报错也不留白，而是标出来它是什么。
        # 这里**故意不用** summary.role_label()（那个给界面用，返回「标准（已停用）」）：
        # 导出是出处，必须按存进库的 key 对回那一行；界面是展示，要说人话。
        # 两种口径同时存在是有意的——别为消掉一个「（已停用）」字面量把出处改掉。
        shown = str(data["role"])
        if shown not in ROLE_KEYS:
            shown += "（已停用）" if shown in RETIRED_ROLES else "（未知角色）"
        meta.append(f"角色：{shown}")
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
    # 真源三兄弟（prompt_source / prompt_bytes / prompt_sha8）直接透传权威层的判断，
    # 前端只渲染。这里不重算、不写文件名映射——映射的唯一出处是 prompt_source.MAP。
    #
    # 盘上事实（别误读成"display 随载荷走"）：**这一份只放公开角色**，字段是 `key`+`name`，
    # 没有 `display`，也永远不会有退役角色进来。退役行的中文名在
    # ``retired_row_labels()``（下面）单独产，两处不共用一个循环：
    # 公开角色的 `name` 是给下拉框当标签用的（选中要用 key），退役行的 `display` 是给
    # 「这个角色现在叫什么状态」用的（「学术分析（已停用）」这种整句），语义不同。
    return [
        {
            "key": item["key"],
            "name": item["name"],
            "tagline": item.get("tagline", ""),
            "passes": item.get("passes", 1),
            "prompt_source": item.get("prompt_source"),
            "prompt_bytes": item.get("prompt_bytes", 0),
            "prompt_sha8": item.get("prompt_sha8", ""),
        }
        for item in ROLES
    ]


def retired_row_labels() -> list[dict[str, str]]:
    """退役角色的载荷行 `{"key","display"}` —— 显示名的**唯一 owner 是权威层** ``role_label()``。

    为什么要单独一个函数：`summary.roles` 的 `retired` 一直是裸 key 列表（`standard`、
    `xueshu`、`shipin`），命令行直接把它打了出来，于是用户在 CLI 看到的是三个英文 key，
    而界面上同一条事实写作「学术分析（已停用）」。同一个事实两个 owner，就一定会漂移：
    `shipin` 是 task-31 才进退役集合的，`role_label()` 那边已经跟着现读改名，CLI 这边没人管。

    这里**不查任何表、不写任何中文**，只把权威层的输出搬进载荷；
    `display` 里带不带「（已停用）」由权威层决定（退役→「〈名字〉（已停用）」）。

    为什么 `retired` 那个裸 key 字段还留着、不改形状：前端 ``summary.js:234`` 把它接给
    ``roleText()``，那里用的是 ``state.roleRetired.indexOf(wanted)``——**只认字符串数组**。
    把 `retired` 换成对象数组会让它恒等 -1，历史行于是显示成「未知角色」，正是本单要修的
    那个症状的镜像版本。所以 `retired`（key，给"是不是退役"这个判定用）与
    `retired_labels`（key+中文名，给"怎么念出来"用）分两个字段放，两个都从同一批
    `RETIRED_ROLES` 现读派生，不存在第二处真源。
    """
    return [{"key": key, "display": summary_pkg.role_label(key)} for key in sorted(RETIRED_ROLES)]


#: 重问/续写时的输出预算下限。一份两小时视频的时间线实测要 8k 上下，而
#: ``llm.max_tokens`` 当年的默认值只有 4096——按配置原样重问必然再截断。
#: （2026-11-05 task-52 把默认值抬到 32768，这条下限在 base 更大时不再起作用，
#: 但仍然是「用户把 max_tokens 调回小值」那条路唯一的兜底，别当死代码删。）
MIN_JSON_TOKENS = 12288


def _request_max_tokens(cfg: Any) -> int | None:
    """本轮真实的输出预算 = 配置里的 ``llm.max_tokens``，读不到（0）就返回 None。

    为什么要在调用点把它传进去（T56-1）：方法论那条链（副本
    ``bili_scribe/core/summarizer.py`` 的 ``summarize → _json_or_retry → _split_reply
    → _timeline_reply``）自己**不看配置**，`summarize()` 的 `max_tokens` 形参默认就是
    None。副本一路把 None 交给 `_json_budget(None)`，宽松档算出来是硬编码下限
    ``MIN_JSON_TOKENS``=12288，而同一轮的主请求按配置走 32768——**坏 JSON 的重问比
    主请求少 20480**，反而更容易再被截断。task-53 在 `_json_budget` 里加的
    `max(base, …)` 下界对这条路径完全无效，因为 base 压根是 None。

    严格档（默认档）逐值零变化：`_json_budget` 在该档返回入参，
    `_parity_max_tokens` 还原出来的还是 `cfg.llm.max_tokens`，与改动前"不发这个字段、
    由 ChatClient 自己按配置补"是同一个数。

    配置读到 0 时仍然传 None：那是"这个字段别发"的原行为，不能在这里变成发 0。
    """
    return int(getattr(getattr(cfg, "llm", None), "max_tokens", 0) or 0) or None


def _json_budget(max_tokens: int | None, floor: int = MIN_JSON_TOKENS) -> int:
    """重问、续写、分批各步用多少输出预算：取「配置的两倍」与 floor 的较大者，
    封顶 16384，但**永不低于配置本身**（``base``）。

    默认（对账档，``WB_SUMMARY_PARITY_STRICT`` 没设就算）不抬到 12288、不翻倍、也不封
    16384：上游用的就是 ``cfg.llm.max_tokens``，多给一个 token 都算 WB 在改变输出预算。
    只有显式设成 0/false/off 才走上面那套抬升。严格档下这个函数**仍然被调用**（本文件
    五个消费点都在调它），但返回值恒等于入参，所以阶梯在结果里看不见——别说成
    「进不到」，那是错的读法，对着消费点一眼就能推翻。

    为什么不把 16384 当成安全上限来写这句话：这条封顶比 ``llm.max_tokens`` 的默认值
    活得久——默认值 2026-11-05 从 4096 抬到 32768（task-52）之后，「封顶 16384」在
    base>16384 时会把**补救请求削到比主请求还小**，坏 JSON 重问反而更容易被截断。
    加下界只救 base>16384 那一格（32768→32768），base≤16384 时逐值不变
    （4096→12288、8192/12288/16384→16384）⇒ 这不是重新设计阶梯，是补一条
    「补救请求不许比主请求更容易截断」的下界。常量 16384 本身没动。
    """
    if _parity_strict():
        return int(max_tokens or 0)
    base = int(max_tokens or 0)
    return max(base, min(max(int(floor), base * 2), 16384))


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
