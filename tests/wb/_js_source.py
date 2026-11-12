"""读前端源码的两个小工具：剥注释、取路由定义块。

为什么单独成模块：字面量禁令必须只看代码行。本仓库的注释习惯是把"为什么"写下来，
于是解释「以前这里调 WB.fs.pickFile」的注释里字面上就含 WB.fs —— 拿整份文件做子串
判断，会把"解释为什么不能这么写"判成"又这么写了"。这个仓库为此反复红过。

剥注释是**近似**实现（跟踪引号，引号外的 // 与 /* */ 丢掉），不追求完整 JS 词法分析：
这些源码里没有正则字面量里带 // 的写法。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_WEB_JS = ROOT / "video_workbench" / "web" / "js"


def web_js_root() -> Path:
    """被测源码目录。默认仓库里那份，可用 `WB_WEB_ROOT` 重定向。

    为什么要有这个开关：牙齿（故意改坏验红）**只准在 %TEMP% 下带哨兵名的副本里做**，
    不许改工作区文件再改回来——这个仓库为"按快照覆盖回写"付过两次代价，
    每次都把同一文件上别人已落地的改动一起抹了。有了开关，牙齿就有了合法入口。
    同一族既有开关：`WB_PROMPTS_DIR`、`WB_CLI_SOURCE_OVERRIDE`。
    """
    override = os.environ.get("WB_WEB_ROOT", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_WEB_JS


QUOTES = ("'", chr(34), chr(96))
BACKSLASH = chr(92)


def read_js(relative: str) -> str:
    path = web_js_root() / relative
    assert path.is_file(), f"文件不存在：{path}"
    return path.read_text(encoding="utf-8")


def code_only(source: str) -> str:
    """去掉注释，保留代码行（行号会变，所以别用它做行号断言）。

    按**行**扫描，不按整份文件扫描。理由是踩过：整份扫描时引号状态一旦被某个
    中文注释里的撇号带歪，后面整片文件都会被当成字符串，注释里的字面量就活下来了
    （实测「以前这里调 WB.fs.pickFile」那行注释因此被判成代码）。
    按行扫描的代价是跨行的模板字符串会被当成代码——这里不关心那种情况。
    """
    out: list[str] = []
    in_block = False
    for line in source.split(chr(10)):
        kept: list[str] = []
        quote = ""
        index = 0
        length = len(line)
        while index < length:
            char = line[index]
            pair = line[index : index + 2]
            if in_block:
                if pair == "*/":
                    in_block = False
                    index += 2
                    continue
                index += 1
                continue
            if quote:
                kept.append(char)
                if char == BACKSLASH and index + 1 < length:
                    kept.append(line[index + 1])
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                index += 1
                continue
            if pair == "//":
                break
            if pair == "/*":
                in_block = True
                index += 2
                continue
            if char in QUOTES:
                quote = char
            kept.append(char)
            index += 1
        out.append("".join(kept))
    return chr(10).join(out)


def block_of(source: str, opener: str) -> str:
    """从 opener 起取一段大括号配平的原文。

    用配平而不是正则：对象体里嵌着函数、数组、对象字面量，正则截出来的东西经常少半截，
    而"少半截"在读 destroy / keydown 时恰好会读成"没有"。
    """
    start = source.find(opener)
    assert start >= 0, f"找不到：{opener}"
    brace = source.find("{", start)
    if brace < 0:
        return source[start : start + 600]
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return source[start:]
