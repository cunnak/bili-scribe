"""大模型总结：提示词编排、长素材分块归约、结构化输出解析。

这个模块最重要的设计不是"怎么让总结好看"，而是"怎么防止它编"。素材本身可能
只有弹幕、或者只有标题简介，模型在这种输入下会本能地用先验知识补出通顺但虚假的
内容。所以提示词里下了三条硬约束：只用素材、弹幕必须标明是观众视角、素材不足
时写"素材未覆盖"而不是推测。时间戳也只在素材真实出现过时才允许填写。
"""

from __future__ import annotations

import difflib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..infra.llm import LlmClient
from . import frames as frames_mod
from . import roles
from .collector import Bundle
from .resolver import format_duration

MAX_CHUNKS = 8


class SummarizeError(RuntimeError):
    """模型没有返回可用的结构化结果。"""


SYSTEM_PROMPT = """你是一位严谨的视频内容编辑。用户给你一份从 B站 视频采集来的素材，你要把它整理成可信、克制的总结。

硬性要求：
1. 只使用素材里出现的信息。素材没说的不补充、不推测，不要用常识填空。
2. 素材可能来自弹幕。弹幕是观众反应，不是视频内容。基于弹幕的判断，表述上必须让读者看得出这是观众视角。
3. 素材不足以支撑某个结论时，直接写"素材未覆盖"，不要用合理推测顶上。
4. 不评价 UP 主本人，不站队，不煽动情绪，不使用营销式夸张措辞。
5. 时间戳只能引用素材中真实出现过的。素材不含时间戳时，timeline 一律返回空数组。
6. 只输出合法 JSON，不要用代码块包裹，不要在 JSON 之外写任何解释。"""

USER_TEMPLATE = """视频标题：{title}
UP 主：{up_name}
时长：{duration}
分区：{tname}
标签：{tags}
分P：第 {page} / {page_count} P
链接：{url}

视频简介：
{desc}

—— 素材来源 ——
类型：{source_label}（可信度：{source_trust}）
说明：{source_detail}
素材中是否含时间戳：{has_ts}

—— 素材正文 ——
{body}

请输出 JSON，字段与要求如下：
{{
  "one_line": "一句话说清这个视频在讲什么，40 字以内",
  "summary_md": "300-600 字的 Markdown 总结，用 2-4 个二级标题分段",
  "key_points": ["3-6 条要点，每条 30 字以内"],
  "timeline": [{{"t": "03:12", "text": "该节点讲了什么"}}],
  "caution": "素材的局限与不确定之处；无需说明则填空字符串",
  "comment_short": "给评论区用的短版总结，180-280 字，纯文本，不要 Markdown 标记，不要换行"
}}"""

MAP_SYSTEM = """你在做长素材的分段预读。对给定的一段素材，抽取"这一段讲了什么"的结构化笔记。

硬性要求：
1. 只记录素材里出现的信息，不补充、不推测。
2. 素材中出现的时间戳要原样保留，写在对应内容前面。
3. 笔记要能让人仅凭它还原这段内容。不要压缩成一堆语义模糊的关键词。
4. 直接输出笔记正文，不要前言、不要 JSON、不要总结句。"""


@dataclass
class Summary:
    one_line: str = ""
    summary_md: str = ""
    key_points: list[str] = field(default_factory=list)
    timeline: list[dict[str, str]] = field(default_factory=list)
    caution: str = ""
    comment_short: str = ""
    chunks: int = 1
    model: str = ""
    role: str = ""
    # 这次总结附了几帧画面。0 表示没附（未启用／采集失败／该视频没有抽帧图）。
    # 记它是因为"模型到底看没看到画面"直接决定产出可信度，事后必须能从记录里查。
    frames: int = 0
    # 这一版改了什么、为什么改。只有迭代打磨出来的版本才会有内容。
    # 它回答的是使用者心里那个问题：「我的要求被听进去了没有」——没有它，
    # 迭代就是盲调，只能凭感觉猜。
    revise_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "one_line": self.one_line,
            "summary_md": self.summary_md,
            "key_points": self.key_points,
            "timeline": self.timeline,
            "caution": self.caution,
            "comment_short": self.comment_short,
            "chunks": self.chunks,
            "model": self.model,
            "role": self.role,
            "frames": self.frames,
            "revise_note": self.revise_note,
        }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise SummarizeError("模型没有返回可解析的 JSON")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SummarizeError(f"模型返回的 JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise SummarizeError("模型返回的 JSON 顶层不是对象")
    return payload


def _split(text: str, size: int, overlap: int, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """切分长素材，并保证分块数不超过上限。

    先反推步长下限：K 个窗口要覆盖 N 个字符，步长至少是 (N-1)/(K-1)。
    步长定下来之后，窗口宽度取 `步长 + overlap`，这样既保住了块间重叠，
    又不会因为末尾残段多切出一块——这正是只按 `size - overlap` 推进会踩的坑。
    """
    length = len(text)
    if size <= 0 or length <= size or max_chunks <= 1:
        return [text]

    minimum_step = math.ceil((length - 1) / (max_chunks - 1))
    step = max(1, minimum_step, size - max(0, overlap))
    window = step + max(0, overlap)

    return [text[i : i + window] for i in range(0, length, step)]


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _coerce_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_coerce_text(item) for item in value) if text]


def _coerce_timeline(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"t": "", "text": text})
            continue
        if not isinstance(item, dict):
            continue
        text = _coerce_text(item.get("text") or item.get("content"))
        if text:
            out.append({"t": _coerce_text(item.get("t") or item.get("time")), "text": text})
    return out[:20]


@dataclass
class PreparedMaterial:
    """预读之后的素材：喂给归约的正文 + 分块数。

    单独成对象，是为了让「预读」成为一次**可缓存、可复用的独立步骤**。
    迭代打磨时把它喂回去，就能跳过整段预读——实测它占一次 4 小时视频总结
    38% 的输入（32,663 / 86,428 tokens）。
    """

    body: str
    chunks: int = 1


@dataclass
class Refinement:
    """一次迭代打磨的输入。

    previous 给的是上一版的**完整 JSON**，而不是只给 summary_md——模型这次要
    输出同构的结构，给它上一版 JSON 是最直接的对应关系：哪些字段该长什么样、
    哪些不该动，一眼就能对上。
    """

    requirement: str
    previous: str


REFINE_SECTION = """

## 这是一次修订，不是重新开始

使用者对上一版总结不满意，提出了下面的修改要求。请**只改需要改的地方**，
其余部分尽量保持原样——尤其是已经写对、写清的部分，不要顺手重写。

—— 上一版总结（完整 JSON）——
{previous}

—— 本次修改要求 ——
{requirement}

修订的三条纪律：
1. **素材仍是唯一依据。** 修改要求不能成为引入素材外信息的理由。要求里提到的
   内容如果素材里找不到，就如实写「素材未覆盖」，不要为了满足要求而编。
2. **不要顺手改没被要求改的地方。** 使用者可能已经认可那些部分，
   重写会造成「改好了这里、弄坏了那里」。
3. 在 `revise_note` 字段里交代：你改了哪些部分、为什么改。如果这条要求无法在
   素材范围内满足，就在那个字段里说明原因，而不是硬凑。
"""


REFINE_CONSTRAINT = """

## ⚠️ 这一轮不是普通的一轮，而是「按使用者的要求修订」

使用者对上一版提出了下面这条修改要求，并且明确说**只改他提到的部分**：

—— 本次修改要求 ——
{requirement}

所以这一轮只关心两件事：
1. **这条要求有没有被满足。**
2. **素材保真**：有没有引入素材里没有的信息，或者丢掉素材里本来就有的东西。
   （这条永远算数，因为产物是要发到别人评论区的。）

除此之外的一切——文风、结构、措辞、你觉得"顺手可以更好"的地方——**一律不要动**。
使用者可能已经认可那些部分，按自己的标准去改就是"改好了这里、弄坏了那里"。

为什么这段话要重复贴在批判与修订两轮上：实测过。要求写着"只改开头那句，其余
一个字都别动"，但批判那一轮按角色自己的清单挑出了七处毛病，修订那一轮又老老实实
照单改了七处——使用者的要求被自己的质量流程覆盖掉了，而这一点从产出上完全看不出来。
"""

REVISE_NOTE_ORDER = """

最后，`revise_note` 要交代的是：**这一版相对使用者看到的那一版改了什么**
（包括按诊断清单做的修正），而不是把诊断清单本身复述一遍。使用者就是靠这一句话
判断"我的要求有没有被听进去"。
"""


# 使用者能看到的内容字段，顺序＝界面上的顺序。
# 不在这个表里的（chunks / model / frames / role / revise_note）是元信息：
# 它们变了不算"内容改动"，混进来只会让每一次比对都显示"改了很多"，等于没信号。
CONTENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("one_line", "一句话结论"),
    ("summary_md", "正文"),
    ("key_points", "要点"),
    ("timeline", "时间轴"),
    ("caution", "局限说明"),
    ("comment_short", "短版"),
)


def _first_diff(a: str, b: str) -> str:
    """两段文字的第一处差异，截短给人看。界面上鼠标停上去显示的就是它。"""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "equal":
            old = (a[i1:i2] or "（空）")[:40]
            new = (b[j1:j2] or "（空）")[:40]
            return f"「{old}」→「{new}」"
    return ""


def _item_text(item: Any) -> str:
    """列表项 → 给人看的一行。

    `timeline` 是 `[{"t": "01:00", "text": "..."}]`，直接 str() 出来是个 Python
    字典字面量（还带单引号），放进「具体差异」里很难看，也看不出改了什么。
    """
    if isinstance(item, dict):
        clock = _coerce_text(item.get("t") or item.get("time"))
        text = _coerce_text(item.get("text") or item.get("content"))
        return f"{clock} {text}".strip()
    return _coerce_text(item)


def diff_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """相邻两版之间，**实际**变了的内容字段。

    存在的理由：`revise_note` 是模型自己写的，而实测它并不总准——有一次要求写着
    "其余部分一个字都别动"、说明也写着"其他字段均保持原样未动"，而 `key_points`
    六条里有一条被实质改写了（"…获取虚拟'交配权'的手" → "…克服恐惧的手段。"）。
    **自述不可靠，那就直接比**：两版的 JSON 都在库里，逐字段比一遍是纯本地计算，
    零成本，也不受模型影响。

    两个刻意的边界：

    - **只报内容字段**，见 CONTENT_FIELDS。
    - **只报客观事实**：哪个字段变了、变了多少、举个例。至于"这算不算顺手改的"，
      要看当时的要求是什么，程序判不了——硬做一个"它没说自己动了这个"的标记会两头
      误报（模型可以描述了改动却不点字段名；也可能点了名却没真改），而**看起来权威
      的误报比不报更糟**。判断留给人。

    列表字段要单独处理：`key_points` 常出现"项数没变、内容换了"，只比项数会漏掉它，
    所以要点出"有几项不同"并给一个例子。
    """
    changed: list[dict[str, Any]] = []
    for field, label in CONTENT_FIELDS:
        old, new = previous.get(field), current.get(field)
        if old == new:
            continue
        if isinstance(old, list) or isinstance(new, list):
            old_items = old if isinstance(old, list) else ([old] if old else [])
            new_items = new if isinstance(new, list) else ([new] if new else [])
            same = sum(1 for x, y in zip(old_items, new_items) if x == y)
            differ = max(len(old_items), len(new_items)) - same
            example = next(
                (
                    f"「{_item_text(x)[:36]}」→「{_item_text(y)[:36]}」"
                    for x, y in zip(old_items, new_items)
                    if x != y
                ),
                "",
            )
            changed.append(
                {
                    "field": field,
                    "label": label,
                    "kind": "list",
                    "detail": f"{len(old_items)} 项 → {len(new_items)} 项，其中 {differ} 项内容不同",
                    "ratio": None,
                    "example": example,
                }
            )
            continue

        old_text, new_text = _coerce_text(old), _coerce_text(new)
        ratio = difflib.SequenceMatcher(None, old_text, new_text).ratio()
        changed.append(
            {
                "field": field,
                "label": label,
                "kind": "text",
                "detail": f"{len(old_text)} → {len(new_text)} 字",
                "ratio": round(ratio, 2),
                "example": _first_diff(old_text, new_text),
            }
        )
    return changed


def build_previous_view(summary_json: dict[str, Any]) -> str:
    """把上一版整理成「给模型看的那份上一版」。

    **必须去掉 revise_note。** 那是上一版对自己的说明，模型看到之后会照抄，
    结果是每一版的「改动说明」都变成同一句话——而使用者恰恰要靠这句话判断
    「这一版到底改了什么、我的要求有没有被听进去」。这一点是活体验收时发现的：
    连续改两版，两版的 revise_note 一字不差，而产出其实变了。
    """
    view = {key: value for key, value in summary_json.items() if key != "revise_note"}
    return json.dumps(view, ensure_ascii=False, indent=1)


def prepare_material(
    bundle: Bundle,
    llm: LlmClient,
    cfg: AppConfig,
    progress=None,
) -> PreparedMaterial:
    """把素材整理成「可以直接喂给归约」的正文。长素材在这里做分段预读。

    这一步被单独抽出来，是因为它同时满足两个条件：**最贵**、**最值得复用**。
    4 小时视频的 7 段预读占一次总结 38% 的输入；而它只依赖素材，不依赖角色或
    修改要求——也就是说，同一份素材迭代多少次，它就只需要跑一次。
    """
    notify = progress or (lambda _stage, _message: None)
    llm_cfg = cfg.llm
    body = bundle.body or ""

    if len(body) <= llm_cfg.max_input_chars:
        return PreparedMaterial(body=body, chunks=1)

    pieces = _split(body, llm_cfg.chunk_size, llm_cfg.chunk_overlap)
    chunks = len(pieces)
    notes: list[str] = []
    for index, piece in enumerate(pieces, 1):
        notify("mapping", f"分段预读 {index}/{chunks}")
        note = llm.chat(
            [
                {"role": "system", "content": MAP_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"这是第 {index} 段，共 {chunks} 段。"
                        f"素材类型：{bundle.source_label}\n\n{piece}"
                    ),
                },
            ],
            note=lambda message, _i=index: notify("mapping", f"第 {_i} 段：{message}"),
        )
        notes.append(note.strip())

    merged = "\n\n".join(
        f"【第 {index} 段笔记】\n{note}" for index, note in enumerate(notes, 1)
    )
    budget = llm_cfg.max_input_chars * 2
    if len(merged) > budget:
        merged = merged[:budget] + "\n…（笔记已截断）"
    return PreparedMaterial(body=merged, chunks=chunks)


def summarize(
    bundle: Bundle,
    llm: LlmClient,
    cfg: AppConfig,
    progress=None,
    role_key: str | None = None,
    frameset: frames_mod.FrameSet | None = None,
    prepared: PreparedMaterial | None = None,
    refinement: Refinement | None = None,
) -> Summary:
    """role_key 为单次任务覆盖角色；不传则用配置里的默认角色。

    做成参数而不是"临时改 cfg.llm.role"：任务跑在后台线程里，改共享配置
    会和别的任务互相踩，还会把一次性的选择意外持久化。

    frameset 是画面帧，**只接在最终归约那一次调用上**。理由：
    帧一旦进入预读或三轮回，就会被同一份素材反复送好几遍——预读最多 7 次、
    三轮回再 2 次，22k 的帧量会变成十几倍。而归约是唯一"把全部素材合起来
    看一遍"的调用，画面在那里最有价值，也只需要付一次钱。

    prepared 传了就用它当素材正文，跳过预读——迭代打磨走这条（见 prepare_material）。
    refinement 传了就是一次**修订**：会把上一版和本次修改要求接在提示词后面，
    并要求模型只改该改的地方。
    """
    notify = progress or (lambda _stage, _message: None)
    llm_cfg = cfg.llm

    has_timestamps = bundle.source_kind in (
        "subtitle_user",
        "subtitle_human",
        "subtitle_ai",
        "danmaku",
    )

    # 预读可复用：迭代打磨时调用方把上次的 PreparedMaterial 传回来。
    #
    # ⚠️ 判断"要不要预读"**不能看 body 长度**——笔记本身也可能超限。实测 4 小时
    # 视频的笔记是 19,772 字符，离 max_input_chars（24,000）只差一点；素材再长
    # 一些笔记就会超限，于是被当成新素材**又预读一遍**。那是二次压缩，信息会在
    # 没人察觉的情况下再掉一层。所以这里只认「有没有传 prepared」。
    material = (
        prepared if prepared is not None else prepare_material(bundle, llm, cfg, progress)
    )
    body = material.body
    chunks = material.chunks

    role = roles.get_role(role_key or llm_cfg.role)
    # 角色自带方法论时，用它的提示词，并把输出契约显式接在后面。
    # 角色原文里那些「打印阶段 1/2/3」「输出 ReAct 执行日志」的要求会击穿 JSON 解析、
    # 并把思考过程发进评论区，所以由 OUTPUT_CONTRACT 明确覆盖掉。
    system_prompt = SYSTEM_PROMPT
    if role.system:
        system_prompt = role.system.strip() + "\n" + roles.OUTPUT_CONTRACT

    notify("reducing", f"生成结构化总结（角色：{role.name}）")
    user_prompt = USER_TEMPLATE.format(
        title=bundle.title or "（无）",
        up_name=bundle.up_name or "（未知）",
        duration=format_duration(bundle.duration),
        tname=bundle.tname or "（未知）",
        tags="、".join(bundle.tags) if bundle.tags else "（无）",
        page=bundle.page,
        page_count=bundle.page_count,
        url=f"https://www.bilibili.com/video/{bundle.bvid}",
        desc=bundle.desc or "（无简介）",
        source_label=bundle.source_label,
        source_trust=bundle.source_trust,
        source_detail=bundle.source_detail,
        has_ts="是" if has_timestamps else "否（不要生成 timeline 内容）",
        body=body,
    )

    if refinement is not None:
        # 迭代打磨。注意上面 USER_TEMPLATE 里**素材笔记全文仍然带着**（body）——
        # 这是刻意的：若只给「上一版」，几轮「基于上一版改」下来会像传话游戏一样
        # 逐渐偏离素材，而这个产物是要发到别人评论区的东西。
        notify("reducing", f"按你的要求修订：{refinement.requirement.strip()[:40]}")
        user_prompt += REFINE_SECTION.format(
            previous=refinement.previous.strip(),
            requirement=refinement.requirement.strip(),
        )

    # 画面帧只在这一处进入请求：前面所有调用（预读、三轮回的批判与修订）都不带。
    user_content: Any = user_prompt
    frame_blocks = frames_mod.as_content_blocks(frameset) if frameset else []
    if frame_blocks:
        user_content = [{"type": "text", "text": user_prompt}] + frame_blocks
        notify(
            "reducing",
            f"已附 {len(frameset.frames)} 帧画面（{frameset.total_bytes / 1024:.0f} KB）",
        )
        for note in frameset.notes:
            notify("reducing", f"画面：{note}")

    raw = llm.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        json_mode=True,
        note=lambda message: notify("reducing", message),
    )
    payload = _extract_json(raw)

    # 三轮回：把「自我批判」从"要求模型打印出来"改成"真的多跑两次调用"。
    # 批判和修订都必须看到原始素材，否则无从判断是否失真——那样这三轮就只是走形式。
    if role.passes >= 3 and role.critique and role.revise:
        draft = json.dumps(payload, ensure_ascii=False)

        # 迭代打磨时，批判与修订这两轮**也必须知道使用者这次要求改什么**。
        # 否则批判会按角色自己的清单去挑毛病、修订再照单全收，把使用者已经认可的
        # 部分一起重写——正是这个功能一开始就要避免的事。详见 REFINE_CONSTRAINT。
        constraint = (
            REFINE_CONSTRAINT.format(requirement=refinement.requirement.strip())
            if refinement is not None
            else ""
        )

        notify("critiquing", f"{role.name}：自我批判")
        critique = llm.chat(
            [
                {"role": "system", "content": role.critique},
                {
                    "role": "user",
                    "content": f"—— 原始素材 ——\n{body}\n\n—— 待评文本 ——\n{draft}{constraint}",
                },
            ],
            note=lambda message: notify("critiquing", message),
        )

        notify("revising", f"{role.name}：按批判修订")
        revised = llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"{role.revise.format(critique=critique.strip())}\n\n"
                        f"—— 原始素材（核对是否失真用）——\n{body}\n\n"
                        f"—— 待修订文本 ——\n{draft}"
                        f"{constraint}"
                        f"{REVISE_NOTE_ORDER if refinement is not None else ''}"
                    ),
                },
            ],
            json_mode=True,
            note=lambda message: notify("revising", message),
        )
        payload = _extract_json(revised)

    summary = Summary(
        one_line=_coerce_text(payload.get("one_line")),
        summary_md=_coerce_text(payload.get("summary_md")),
        key_points=_coerce_list(payload.get("key_points")),
        timeline=_coerce_timeline(payload.get("timeline")) if has_timestamps else [],
        caution=_coerce_text(payload.get("caution")),
        comment_short=_coerce_text(payload.get("comment_short")),
        chunks=chunks,
        model=llm.cfg.model,
        role=role.key,
        frames=len(frameset.frames) if frameset else 0,
        # 只有修订版才记「改了什么」。初次生成时这个字段没有意义——那时没有
        # 上一版可比，模型填什么都是废话。
        revise_note=_coerce_text(payload.get("revise_note")) if refinement else "",
    )

    if not summary.summary_md and not summary.one_line:
        raise SummarizeError("模型返回的总结为空，请重试或换个模型")
    if not summary.comment_short:
        # 评论字段缺失时退回一句话结论，保证闭环不至于断在这里
        summary.comment_short = summary.one_line or summary.summary_md[:200]
    return summary
