"""总结接口返回的 JSON 不是严格合法时，先就地修好再交给解析器。

真实失败现场（2026-11-01）：模型把 `summary_md` 写成多行，字符串里带原始换行，
`json.loads` 在「line 3 column 230」报 `Expecting ',' delimiter`。
这类输出人看着完全正常，是解析器太严——不该让用户重跑一遍八次预读。
"""
from __future__ import annotations

import json
import re
from typing import Any

#: 尾逗号：`,` 后面直接跟 `}` 或 `]`
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
#: 围栏：```json … ```
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def _links(text: str) -> str:
    body = (text or "").strip()
    body = _FENCE.sub("", body)
    start = body.find("{")
    end = body.rfind("}")
    if start != -1 and end > start:
        return body[start : end + 1]
    return body


_WS = " " + chr(9) + chr(10) + chr(13)


def _escape_inner_quotes(text: str) -> str:
    """把字符串内部没转义的 `"` 补成 `\\"`。

    真实现场（2026-11-01 14:31）：模型在正文里直接引用别人的话——
    `"实际改动": "删掉"赋能"这种说法"`，结构在第二个引号处就断了。
    `strict=False` 救不了它（那不是换行问题，是引号配对问题）。

    判据：引号后第一个非空白字符若是「结构收尾」（逗号 / 冒号 / 闭括号 / 文本结束），
    就当它结束了字符串；否则它只是正文里的引号，转义掉。
    """
    out: list[str] = []
    inside = False
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == chr(92) and inside:
            out.append(ch)
            escaped = True
            continue
        if ch != '"':
            out.append(ch)
            continue
        if not inside:
            inside = True
            out.append(ch)
            continue
        nxt = ""
        for j in range(i + 1, min(i + 8, len(text))):
            if text[j] not in _WS:
                nxt = text[j]
                break
        if nxt in ("", ",", ":", "}", "]"):
            inside = False
            out.append(ch)
        else:
            out.append(chr(92) + '"')
    return "".join(out)


def _try(text: str) -> Any:
    escaped = _escape_inner_quotes(text)
    for candidate in (
        text,
        _TRAILING_COMMA.sub(r"\1", text),
        escaped,
        _TRAILING_COMMA.sub(r"\1", escaped),
    ):
        try:
            # strict=False：允许字符串里出现原始换行/制表符——模型最常犯的就是这个
            return json.loads(candidate, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def repair_json(text: str) -> str:
    """能修就返回**规范化的 JSON 文本**，修不了返回空串（调用方自己决定重问还是报错）。

    返回值是重新序列化过的，所以严格解析器（bili-scribe 的 `json.loads`）也吃得下。
    """
    body = _links(text)
    if not body:
        return ""
    payload = _try(body)
    if payload is None:
        # 还有一种常见坏法：模型在 JSON 前后各说了一句话，且中间夹了第二个 `{`
        first = body.find("{")
        last = body.rfind("}")
        if first != -1 and last > first and (first, last) != (0, len(body) - 1):
            payload = _try(body[first : last + 1])
    if payload is None:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - 解析成功就一定能序列化
        return ""


def parse_json(text: str) -> Any:
    """修好再解析；修不了返回 ``None``（调用方自己决定报错文案）。"""
    fixed = repair_json(text)
    if fixed:
        try:
            return json.loads(fixed)
        except (json.JSONDecodeError, ValueError):  # pragma: no cover
            return None
    return None