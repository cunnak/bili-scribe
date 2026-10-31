"""字幕翻译（P2-05）。

规则来自方案书 §5.5.4：**zh/en/ja、双语四选一、每次带 ±2 条上下文、术语表强制替换、
单条可重试、整批失败保留已完成部分。**

和 P2-04（优化）最不一样的地方：优化只改 ``text``，翻译要**同时留住原文和译文**
（``translated_text``）。所以这一层不覆盖 ``segments``，只往每条上贴一个译文字段，
存进 ``wb_subtitle`` 独立一行——原始字幕永远是原始字幕，翻译是它旁边的一份草稿。

三件必须由代码而不是由 prompt 保证的事：

1. **上下文只喂不译。** prompt 里前后各 2 条标成"上文/下文（勿译）"，回来的结果
   再按本批 id 过滤一次。模型偶尔会顺手把上文也译了，那不能算进结果。
2. **术语表是强制替换，不是建议。** 先让模型正常译，再按术语表做一次字面替换：
   表里 ``向量数据库=Vector DB`` 而译文写成 ``vector database``，以术语表为准。
   替换按原文长度降序做，避免短词把长词的子串吃掉（"AI" 抢在 "AI 芯片" 前面）。
3. **一段失败不拖整批。** 每段单独成败，失败段 ``translated_text`` 留空并记进
   ``failed``，界面上给"重试这一段"的按钮（``retry_ids``）。

翻译结果**不覆盖原始字幕**，写库与导出是两步；这一步只产出 segments 加译文。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import BILINGUAL_MODES, TRANSLATE_TARGETS
from ..infra.llm import ChatClient, ChatError

PROMPT_PATH = Path(__file__).resolve().parents[1] / "resources" / "prompts" / "translate.md"

DEFAULT_BATCH = 10
CONTEXT_WINDOW = 2      # ±2 条：方案书指定值。再多上下文会稀释小模型，更少会丢指代
MAX_MISSING = 0.2       # 缺行超过两成就整批判失败：宁可重跑，不要一份半翻译
MIN_TARGET = 2          # 少于 2 个字符基本是"翻译失败回了个标点"

# 语言码 → 中文提示名（进 prompt）与显示名（进界面）。
LANG_NAMES: dict[str, str] = {
    "zh": "中文",
    "en": "英文",
    "ja": "日文",
    "auto": "自动",
}

# 双语四选一 ``BILINGUAL_MODES`` 与目标语言 ``TRANSLATE_TARGETS`` 从 config 层导入
# （见文件头）：配置要校验这两个枚举，定义放在 core 会让配置反向依赖 core。

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

Translation = dict[str, Any]


# ------------------------------------------------------------------ 源语言与目标语言


_CJK = re.compile(r"[㐀-䶿一-鿿]")
_KANA = re.compile(r"[ぁ-んァ-ヶ]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_source(segments: Iterable[dict[str, Any]]) -> str:
    """粗判源语言：zh / ja / en / other。

    只要有假名就算日文——中文句子里不会出现假名，反过来日文里假名一定出现。
    剩下的看 CJK 与拉丁字母谁占多。这是为了定"默认目标语言"，不是翻译质量判断，
    所以不需要多准：判错了用户在下拉框里改一次就行。
    """
    kana = cjk = latin = 0
    for seg in segments:
        text = str(seg.get("text") or "")
        kana += len(_KANA.findall(text))
        cjk += len(_CJK.findall(text))
        latin += len(_LATIN.findall(text))
    if not (kana or cjk or latin):
        return "other"
    if kana:
        return "ja"
    if cjk >= latin:
        return "zh"
    return "en"


def resolve_target(requested: str, segments: Iterable[dict[str, Any]]) -> str:
    """``auto`` 的规则来自方案书：源为中文默认译成英文，其余默认译成中文。"""
    want = (requested or "auto").strip().lower()
    if want in ("zh", "en", "ja"):
        return want
    source = detect_source(segments)
    return "en" if source == "zh" else "zh"


# ------------------------------------------------------------------ 术语表


def parse_glossary(entries: Iterable[Any] | None) -> list[tuple[str, str]]:
    """把配置里的 ``source=target`` 列表解析成对。

    宽松处理是刻意的：术语表是手写的，多一个空格、用了全角等号、写成
    ``target<-source`` 都不该让整个翻译报错。认不出来的行直接跳过，
    但会在 ``glossary`` RPC 里回给界面，让用户看得见"哪条没生效"。
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in entries or []:
        if isinstance(raw, dict):
            src = str(raw.get("source") or "").strip()
            dst = str(raw.get("target") or "").strip()
        else:
            text = str(raw or "").strip()
            if not text:
                continue
            parts = re.split(r"[=＝→>]+", text, maxsplit=1)
            if len(parts) != 2:
                continue
            src, dst = parts[0].strip(), parts[1].strip()
        if not src or not dst or src == dst:
            continue
        if src in seen:          # 后写的覆盖先写的：取最后一次
            pairs = [p for p in pairs if p[0] != src]
        seen.add(src)
        pairs.append((src, dst))
    # 长词优先：不然 "AI" 会把 "AI 芯片" 里的 AI 先换掉，剩下半个"芯片"
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def format_glossary(pairs: Iterable[tuple[str, str]]) -> str:
    items = list(pairs)
    if not items:
        return "（无）"
    return "\n".join(f"{src} = {dst}" for src, dst in items)


def apply_glossary(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """术语强制替换。译文里出现术语表**源词**时替换成指定译法。"""
    out = text
    for src, dst in pairs:
        if src and src in out:
            out = out.replace(src, dst)
    return out


# ------------------------------------------------------------------ prompt


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _line(seg: dict[str, Any]) -> str:
    return (
        f'{seg.get("id")}\t'
        f'[{int(seg.get("start_ms") or 0)}-{int(seg.get("end_ms") or 0)}]\t'
        f'{seg.get("text") or ""}'
    )


def build_prompt(
    batch: list[dict[str, Any]],
    *,
    target: str,
    context: list[dict[str, Any]] | None = None,
    glossary: list[tuple[str, str]] | None = None,
    template: str | None = None,
) -> str:
    """组装一批的 prompt。``context`` 是整条字幕（含前后各 ``CONTEXT_WINDOW`` 条）。

    上下文取的是"这批在整条里的前后邻居"，不是"这批自己"——所以本批第一条的前一句
    可能是上一批的最后一句，那也要给，否则跨批边界的地方代词会断。
    """
    text = template if template is not None else load_prompt()
    first = context.index(batch[0]) if context and batch[0] in context else -1
    before = context[:first] if first > 0 else []
    after = context[first + len(batch):] if first >= 0 else []
    before = before[-CONTEXT_WINDOW:]
    after = after[:CONTEXT_WINDOW]
    return (
        text.replace("$target_name", LANG_NAMES.get(target, target))
        .replace("$glossary", format_glossary(glossary or []))
        .replace("$context_before", "\n".join(_line(seg) for seg in before) or "（无）")
        .replace("$context_after", "\n".join(_line(seg) for seg in after) or "（无）")
        .replace("$lines", "\n".join(_line(seg) for seg in batch))
    )


def split_batches(segments: Iterable[dict[str, Any]], size: int = DEFAULT_BATCH) -> list[list[dict[str, Any]]]:
    items = list(segments)
    step = max(1, int(size or DEFAULT_BATCH))
    return [items[i:i + step] for i in range(0, len(items), step)]


# ------------------------------------------------------------------ 解析与校验


def parse_reply(raw: str) -> dict[str, str]:
    """从模型回复里抠出 ``{id: text}``。允许代码围栏与前后废话。"""
    text = (raw or "").strip()
    if not text:
        raise ChatError("LLM_BAD_RESPONSE", "模型返回了空内容")
    block = _FENCE.search(text)
    if block:
        text = block.group(1).strip()
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            raise ChatError("LLM_BAD_RESPONSE", "模型没有返回 JSON 数组", text[:300])
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ChatError("LLM_BAD_RESPONSE", "模型返回的 JSON 解析失败", text[:300]) from exc
    if not isinstance(data, list):
        raise ChatError("LLM_BAD_RESPONSE", "模型返回的不是数组", text[:300])

    out: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        seg_id = str(item.get("id") or "").strip()
        if not seg_id:
            continue
        value = item.get("translated_text")
        if value is None:
            value = item.get("translation")
        if value is None:
            value = item.get("text")
        out[seg_id] = str(value if value is not None else "").strip()
    return out


def check_batch(batch: list[dict[str, Any]], after: dict[str, str]) -> list[str]:
    """一批的健全性检查。返回问题清单，空表示可用。

    翻译比优化宽松：译文长短与原文不成正比是常态（中→英长度能翻倍再翻倍），
    所以**这里不校验长度**。只拦三类真事故：漏行、整行原样照抄、空行太多。
    """
    problems: list[str] = []
    expected = [str(seg.get("id")) for seg in batch]
    missing = [sid for sid in expected if sid not in after]
    if missing and len(missing) > len(expected) * MAX_MISSING:
        problems.append(f"漏了 {len(missing)} 行（{missing[:3]}…）" if len(missing) > 3
                        else f"漏行：{missing}")
    for seg in batch:
        sid = str(seg.get("id"))
        got = after.get(sid)
        if got is None:
            continue
        if "\n" in got or "\r" in got:
            problems.append(f"{sid} 含换行（字幕一行只能一行）")
        elif len(got) < MIN_TARGET:
            problems.append(f"{sid} 译文过短：{got!r}")
    return problems


# ------------------------------------------------------------------ 主流程


def translate(
    segments: Iterable[dict[str, Any]],
    client: ChatClient,
    *,
    target: str = "en",
    glossary: Iterable[Any] | None = None,
    batch_size: int = DEFAULT_BATCH,
    temperature: float = 0.3,
    cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    prompt_template: str | None = None,
    retry_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """翻译一批字幕，返回 ``{segments, failed, model, target}``。

    - ``segments`` 是**全量**（含本次没重跑的段），每条多一个 ``translated_text``；
      没译出来的段这个字段是空串——界面据此显示"这段失败了，可重试"。
    - ``retry_ids`` 只重跑指定段（单条重试），其余保持库里已有译文。
    - 失败段进 ``failed``，带 id/code/message，界面按 id 挂"重试这一段"按钮。
    """
    client.require()
    items = [dict(seg) for seg in segments]
    # 输出契约：每条一定有 translated_text（没译出来就是空串）。
    # 不在这里补齐的话，前端要同时处理"没有这个键"和"键是空串"两种情况，
    # 而它只会写 `seg.translated_text || ""`——缺键在 JS 里不报错，在 Python 里会。
    for seg in items:
        seg.setdefault("translated_text", "")
    if not items:
        raise ChatError("VALIDATION", "没有可翻译的字幕")
    target = (target or "en").strip().lower()
    if target not in TRANSLATE_TARGETS or target == "auto":
        raise ChatError("VALIDATION", "目标语言只支持 zh / en / ja", target)

    pairs = parse_glossary(glossary)
    wanted = {str(i) for i in (retry_ids or []) if str(i).strip()}
    todo = [seg for seg in items if not wanted or str(seg.get("id")) in wanted]
    if not todo:
        raise ChatError("VALIDATION", "没有要翻译的字幕段")

    template = prompt_template if prompt_template is not None else load_prompt()
    groups = split_batches(todo, batch_size)
    failed: list[dict[str, Any]] = []
    fresh: set[str] = set()  # 本次真写进去的 id；旧的译文不算
    done = 0

    for group in groups:
        if cancel is not None and cancel():
            failed.append({"ids": [str(g.get("id")) for g in group], "code": "CANCELED", "message": "已取消"})
            break
        try:
            prompt = build_prompt(group, target=target, context=items,
                                  glossary=pairs, template=template)
            raw = client.chat(
                [
                    {"role": "system", "content": "你只输出 JSON 数组，不输出任何解释。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
            )
            after = parse_reply(raw)
            problems = check_batch(group, after)
            if problems:
                raise ChatError("LLM_BAD_RESPONSE", "模型译文不合格", "；".join(problems[:4]))
            # 只收本批的 id：模型把"上文"也顺手译了的情况，多余行直接丢
            by_id = {str(seg.get("id")): seg for seg in items}
            for sid, value in after.items():
                if sid not in by_id:
                    continue
                by_id[sid]["translated_text"] = apply_glossary(value, pairs)
                fresh.add(sid)
        except ChatError as exc:
            # 一段/一批失败不碰别的段：已有译文保持，界面给重试入口
            failed.append({
                "ids": [str(g.get("id")) for g in group],
                "code": exc.code,
                "message": getattr(exc, "message", str(exc)),
                "detail": getattr(exc, "detail", "")[:300],
            })
        done += 1
        if on_progress is not None:
            on_progress(done, len(groups))

    # 两个计数不能合并成一个：
    # - ``translated``＝**本次**真译出来的条数。重试两段时它只该是 2，
    #   服务层拿它判"整批都失败了"，合并成"有译文的段数"会让全批失败看起来像成功
    #   （库里还留着上次的译文，一加就是 19）。
    # - ``filled``＝全量里有译文的段数，界面显示"已译 19/19"要用它。
    translated = sum(
        1 for seg in items
        if str(seg.get("id")) in fresh and str(seg.get("translated_text") or "").strip()
    )
    filled = sum(1 for seg in items if str(seg.get("translated_text") or "").strip())
    return {
        "segments": items,
        "failed": failed,
        "batches": len(groups),
        "translated": translated,
        "filled": filled,
        "total": len(items),
        "model": client.model,
        "target": target,
        "glossary": [{"source": s, "target": d} for s, d in pairs],
    }


# ------------------------------------------------------------------ 双语合并


def bilingual_lines(
    seg: dict[str, Any],
    mode: str = "original_top",
) -> list[str]:
    """一条字幕按双语模式拆成 1–2 行文本。

    译文缺失时**退回原文单行**而不是留空：宁可看到原文，不要看到空白屏。
    """
    source = str(seg.get("text") or "").strip()
    target = str(seg.get("translated_text") or "").strip()
    if mode == "original_only" or not target:
        return [source] if source else []
    if mode == "translated_only":
        return [target]
    if mode == "translated_top":
        return [target, source]
    return [source, target]


def merge_bilingual(
    segments: Iterable[dict[str, Any]],
    mode: str = "original_top",
) -> list[dict[str, Any]]:
    """导出用的"文本已合并"段列表：``text`` 是合并后的行，原文另存 ``source_text``。"""
    if mode not in BILINGUAL_MODES:
        raise ChatError("VALIDATION", "双语模式只支持 " + " / ".join(BILINGUAL_MODES), mode)
    out: list[dict[str, Any]] = []
    for seg in segments:
        lines = bilingual_lines(seg, mode)
        if not lines:
            continue
        out.append({
            "id": str(seg.get("id")),
            "start_ms": int(seg.get("start_ms") or 0),
            "end_ms": int(seg.get("end_ms") or 0),
            "text": "\n".join(lines),
            "lines": lines,
            "source_text": str(seg.get("text") or ""),
            "translated_text": str(seg.get("translated_text") or ""),
        })
    return out
