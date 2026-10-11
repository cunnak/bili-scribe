"""SRT 字幕解析：编码探测、样式清洗、时间戳提取。

## 为什么单独一个模块

平台字幕走 JSON（`from` / `content`），而 srt 是纯文本块结构——两件不同的事。
更麻烦的是 srt 在 Windows 上有四种常见编码，正文里还常混着 `<i>`、`{\\an8}`
这类样式标记，这些都不该进模型看到的文本。塞进 collector 会让那条本已复杂的
降级链更难读，所以单独成模块，只做"把文件变成 (时间, 文本) 列表"这一件事。

## 刻意不做 15 秒窗聚合

collector 里那套 `_merge_subtitle` 是为「B站 AI 字幕」设计的：它没有标点、
几字一行，必须按时间窗重新拼回句子。而 srt 通常已经是**规范的一整句一条**，
再按 15 秒粘合会把独立的对白并成一坨，甚至把两句话拼成一句。
所以这里只解析，不聚合。

## 时间格式化的重复

本模块自带一个 `format_clock`，和 collector 里的 `_stamp` 实现相同。这是有意的：
collector 要 import 本模块（用解析器），本模块不能再反向依赖它，否则成环。
三行代码换掉一个循环依赖，划算。
"""

from __future__ import annotations

import html
import re

# 编码探测顺序，按"实际出现频率"排，不是随便列的：
#   1. utf-8-sig  带 BOM 的 UTF-8——Windows 记事本存"UTF-8"默认加 BOM，最常见
#   2. utf-8      不带 BOM 的（它也能正确解出带 BOM 的内容，所以顺序无所谓）
#   3. gb18030    GBK 的超集，中文 Windows 老工具的默认编码
#   4. utf-16     没有 BOM 的 UTF-16（有 BOM 的在 _decode 里已优先处理）
#   5. big5       港台字幕常见
# BOM 在 _decode 里显式判断，所以这里不再列 utf-8-sig / utf-16：
# - utf-8-sig 对**无 BOM** 的 UTF-8 输入同样能解码成功，于是跟 utf-8 同分；
#   它若排在前面，会把一份纯 UTF-8 文件误报成 utf-8-sig。而回执里那个编码名
#   存在的全部意义就是准确（出乱码时它是唯一线索）。
# - utf-16 要求输入以 BOM 开头，没 BOM 时根本解不出来。
ENCODINGS = ("utf-8", "gb18030", "big5")

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16 = (b"\xff\xfe", b"\xfe\xff")

# 时间行。**小时段是可选的**：规范写法是 HH:MM:SS,mmm，但不少工具输出 MM:SS,mmm。
# 毫秒分隔符逗号和句点都要认（有些工具写 00:00:01.000）。
_ARROW = re.compile(
    r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
    r"\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)

# 只用来判断"这段文本像不像 srt"，不做解析（search 即可，anchors 无所谓）
_ANY_STAMP = re.compile(r"\d{1,2}:\d{2}[.,]\d{1,3}\s*-->")

# 样式标记：HTML 类（<i> <b> <font color=...>）与 ASS 类（{\an8} {\pos(..)}）
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_ASS_TAG = re.compile(r"\{\\[^{}]*\}")


class SrtError(RuntimeError):
    """这个文件读不出有效字幕。

    单独成异常类型，是为了让调用方能**降级**（回落到平台字幕）并如实告知，
    而不是让整条任务失败——使用者传错文件不该中断一次总结。
    """


def format_clock(seconds: float) -> str:
    total = int(seconds or 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _garble_score(text: str) -> int:
    """解码结果有多像乱码。

    为什么需要它：**多个编码经常都能"成功"解码同一段字节**——gb18030 尤其宽容，
    几乎不会抛异常。只按 try/except 走，一个 big5 文件会被 gb18030 解成乱码
    还"成功"返回。所以改成给每个候选打分，挑最不像乱码的那个。

    判据是"正常文本不该出现的字符"：
      - 私有使用区（U+E000–U+F8FF）：汉字正常落在 CJK 区，解错才会飘到这里
      - 裸控制符：换行制表符之外的控制字符
      - 替换字符 U+FFFD
    """
    score = 0
    for ch in text:
        code = ord(ch)
        if 0xE000 <= code <= 0xF8FF or code == 0xFFFD:
            score += 1
        elif code < 0x20 and ch not in "\n\r\t":
            score += 1
    return score


def _decode(raw: bytes) -> tuple[str, str]:
    """探测编码：先看 BOM，再对所有"解得出来且含时间行"的候选打分，取最干净的那个。"""
    if raw[:2] in _BOM_UTF16:
        return raw.decode("utf-16"), "utf-16"
    if raw[:3] == _BOM_UTF8:
        # 显式认 BOM 才能把 utf-8 与 utf-8-sig 分开——只靠候选打分是分不出来的，
        # 因为两种解码器对同一份无 BOM 文件给出同样的结果。
        return raw.decode("utf-8-sig"), "utf-8-sig"

    candidates: list[tuple[int, int, str, str]] = []
    for order, enc in enumerate(ENCODINGS):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 时间行是纯 ASCII，这层过滤能挡掉大部分"明显解错"的候选
        # （例如 utf-16 去解 utf-8 的文件，绝不会还原出 00:00:01 这样的串）。
        if not _ANY_STAMP.search(text):
            continue
        candidates.append((_garble_score(text), order, text, enc))

    if candidates:
        # 先比乱码分，同分时按 ENCODINGS 的顺序——那是按实际出现频率排的，
        # gb18030 在 big5 之前，对中文用户是更实用的默认。
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, text, enc = candidates[0]
        return text, enc

    raise SrtError(
        "认不出这个文件的编码（试过 "
        + "、".join(ENCODINGS)
        + "，以及 UTF-8 / UTF-16 的 BOM 标记），"
        "或者它里面没有 srt 的时间行（形如 00:00:01,000 --> 00:00:04,500）"
    )


def _to_seconds(hours: str | None, minutes: str, secs: str, millis: str) -> float:
    ms = int(millis.ljust(3, "0")[:3])  # ".5" 视作 500ms，不是 5ms
    return (int(hours) if hours else 0) * 3600 + int(minutes) * 60 + int(secs) + ms / 1000


def clean_markup(text: str) -> str:
    """剥掉样式标记与 HTML 实体。

    先剥标签再解实体：反过来的话，正文里本来想显示成字面量的 `&lt;i&gt;`
    会被当成真标签删掉。
    """
    text = _HTML_TAG.sub("", text)
    text = _ASS_TAG.sub("", text)
    return html.unescape(text).strip()


def _join_lines(parts: list[str]) -> str:
    """把一条字幕的多行合并成一行。

    中文字幕的换行是**显示折行**，不是句子边界，所以直接拼；
    但如果两侧都是 ASCII 单词字符（英文字幕），就得补一个空格，
    否则 `hello` + `world` 会粘成 `helloworld`。
    """
    cleaned = [piece for piece in (clean_markup(p) for p in parts) if piece]
    out = ""
    for piece in cleaned:
        if out and out[-1].isascii() and out[-1].isalnum() and piece[0].isascii() and piece[0].isalnum():
            out += " "
        out += piece
    return out.strip()


def parse_text(text: str) -> list[tuple[float, str]]:
    """解析 srt 正文，返回 [(起始秒, 文本)]，保持文件里的原顺序。

    编号行、多余空行、缺失的编号都能容忍——srt 没有强校验，各家工具输出很随意。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    items: list[tuple[float, str]] = []
    i = 0
    while i < len(lines):
        match = _ARROW.match(lines[i].strip())
        if not match:
            i += 1
            continue
        start = _to_seconds(*match.groups()[:4])
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        merged = _join_lines(body)
        if merged:
            items.append((start, merged))
    return items


def parse_bytes(raw: bytes) -> tuple[list[tuple[float, str]], str]:
    """完整解析：探测编码 → 解析 → 校验非空。

    返回 (条目, 识别到的编码)，编码名要回显给使用者——出现乱码时它是唯一的线索。
    """
    if not raw.strip():
        raise SrtError("这个文件是空的")
    text, encoding = _decode(raw)
    items = parse_text(text)
    if not items:
        raise SrtError("文件里有时间行，但没解析出任何有效文本")
    return items, encoding


def is_ordered(items: list[tuple[float, str]]) -> bool:
    """时间戳是否单调不减。"""
    return all(a[0] <= b[0] for a, b in zip(items, items[1:]))


def check_alignment(items: list[tuple[float, str]], duration: int) -> list[str]:
    """比对字幕时间轴与视频时长，返回警告（只警告，不阻止）。

    这是唯一能发现"传错文件"的手段——而传错的后果极其隐蔽：模型会拿
    一条视频的字幕去总结另一条视频，产出读起来完全通顺，只是内容全错。

    两条判据都留了余量，因为以下情况是合法的、不该被拦：
    分 P 上传的视频、只讲了开头一部分的录像、时间轴有轻微偏移的字幕。
    """
    if not items or not duration:
        return []
    first = min(start for start, _ in items)
    last = max(start for start, _ in items)
    warnings: list[str] = []
    if last > duration * 1.2:
        warnings.append(
            f"你上传的字幕最后一句在 {format_clock(last)}，而这条视频只有 {format_clock(duration)}——"
            "时间轴对不上，这份字幕可能不是这条视频的"
        )
    elif last < duration * 0.5:
        warnings.append(
            f"你上传的字幕只到 {format_clock(last)}，视频全长 {format_clock(duration)}——"
            "可能只覆盖了一部分内容，总结会缺掉后面的部分"
        )
    if first > max(60, duration * 0.1):
        warnings.append(
            f"你上传的字幕从 {format_clock(first)} 才开始，视频开头的 {format_clock(first)} 没有字幕覆盖"
        )
    return warnings


__all__ = [
    "ENCODINGS",
    "SrtError",
    "check_alignment",
    "clean_markup",
    "format_clock",
    "is_ordered",
    "parse_bytes",
    "parse_text",
]
