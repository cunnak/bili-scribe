"""素材来源（origin）：本机 ASR / B站 AI 字幕 / B站人工字幕 / 用户 srt。

**为什么要有这个模块。** 在它出现之前，`core/summarizer.py` 的 `build_bundle` 把
素材类型写死成「AI 转录字幕 / 本机 ASR」，因为那时 WB 唯一能拿到字幕的路就是自己跑
ASR。轻量总结（只输 av/BV、不下载视频）把这条前提打破了：同一个 `Bundle` 现在可能
装着平台 AI 字幕、平台人工字幕，或者用户自己给的 srt。**可信度说明必须跟着真实来源
走**——把 B站 AI 字幕说成「本机 ASR」不会让总结更差，但会把「素材未覆盖」这句话的
可信度整体拉低，因为模型被告知它读的是我们自己的机器转录。

**为什么单独一个模块而不是塞进 summarizer。** 判据是「谁拥有这张表」：`label` /
`trust` / `detail` 的规范映射只有一个 owner——`bili_scribe.core.collector.SOURCE_META`
（那里是 bs 的权威表，五个键：`subtitle_user`/`subtitle_human`/`subtitle_ai`/
`danmaku`/`meta`）。本模块**import 它，不抄第二份**，只补一件 bs 没有的事：
`asr` 这一支（本机语音识别），它不属于「B站字幕」，所以 bs 的表里没有它。

**origin 存在哪。** 存在项目的 `transcript.json` 里（键 `origin`）。选这个文件而不是
`wb_project` 新列的理由有两条：① `transcript.json` 已经是「这个项目有没有字幕」的
判据（`app/service.py` 的 `_decorate` 读它算 `has_transcript`），字幕的来源天然跟着
字幕走；② 加列要把 `SCHEMA_VERSION` 抬到 7、往 `_ALL_COLUMN_BUCKETS` 补桶，还会
连带撞 `tests/wb/test_publish_local_video.py` 里那条钉死 6 的断言——为一个字符串付
这么多迁移成本不值。缺 `origin` 键的老项目一律按 `asr` 读，与今天的行为逐字一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bili_scribe.core import srt as srt_mod
from bili_scribe.core.collector import SOURCE_META
from video_workbench.app.errors import WBError

#: 四种素材来源。**顺序有意义**：第一个是默认值，也是老项目（没有 origin 键）的读法。
ORIGINS = ("asr", "bili_ai", "bili_human", "user_srt")
DEFAULT_ORIGIN = ORIGINS[0]

#: origin -> Bundle.source_kind。前者是我们的词，后者是 bs 的词表（提示词与
#: `has_timestamps` 都按它判），两边不是同一个东西，所以要有这张映射。
_SOURCE_KIND = {
    "asr": "subtitle_ai",
    "bili_ai": "subtitle_ai",
    "bili_human": "subtitle_human",
    "user_srt": "subtitle_user",
}

#: origin -> SOURCE_META 的键。`asr` 没有对应项（见模块 docstring），单独写文案。
_META_KEY = {
    "bili_ai": "subtitle_ai",
    "bili_human": "subtitle_human",
    "user_srt": "subtitle_user",
}

#: 本机 ASR 的 label / trust。`source_kind` 仍是 `subtitle_ai`（它确实是机器转录），
#: 但 label 不能说成「B站 AI 字幕」——那是平台生成的东西，可信度不是一回事。
_ASR_LABEL = "AI 转录字幕"
_ASR_TRUST = "中（机器转录，可能有错字，时间戳可信）"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_origin(value: Any) -> str:
    """把任意输入收敛成 `ORIGINS` 里的一个值，认不出来就是默认值。

    为什么回落而不是抛：这个值来自磁盘上的历史文件，用户不会手改它，但版本回退、
    手工搬目录都会留下认不出来的值。为一个只影响文案的字段让整条总结跑不动，
    代价远大于收益。
    """
    text = _clean(value).lower()
    return text if text in ORIGINS else DEFAULT_ORIGIN


def read_origin(root_dir: str | Path) -> str:
    """读 `<root_dir>/transcript.json` 里的 origin。文件不在/坏了都按默认值。"""
    if not _clean(root_dir):
        return DEFAULT_ORIGIN
    path = Path(str(root_dir)) / "transcript.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_ORIGIN
    if not isinstance(payload, dict):
        return DEFAULT_ORIGIN
    return normalize_origin(payload.get("origin"))


def origin_of(project: dict[str, Any] | None) -> str:
    """从项目行读 origin（项目行里只有 `root_dir`，来源在它下面的 json 里）。"""
    if not project:
        return DEFAULT_ORIGIN
    return read_origin(_clean(project.get("root_dir")))


def bundle_meta(origin: str, *, segments: int, danmaku: int = 0) -> tuple[str, str, str, str]:
    """origin + 段数 -> `(source_kind, label, trust, detail)`，直接喂 `Bundle`。

    `detail` 的写法沿用 bs 的口径：先说来源，再说段数与时间戳，最后把该来源特有的
    风险说清。弹幕是附加素材，有就补一句，没有不提——空着说「另附弹幕 0 条」会让
    模型以为有弹幕但内容为空。
    """
    key = normalize_origin(origin)
    count = max(0, int(segments or 0))
    hot = max(0, int(danmaku or 0))
    if key == "asr":
        label, trust = _ASR_LABEL, _ASR_TRUST
        detail = f"字幕来自本机 ASR，共 {count} 段，含时间戳。"
    else:
        meta = SOURCE_META[_META_KEY[key]]
        label, trust, caveat = meta[0], meta[1], meta[2]
        if key == "bili_ai":
            head = f"B站 AI 字幕，共 {count} 段，含时间戳。"
        elif key == "bili_human":
            head = f"UP 主上传或平台校对的人工字幕，共 {count} 段，含时间戳。"
        else:
            head = f"你提供的 srt 字幕文件，共 {count} 段，含时间戳。"
        detail = f"{head}{caveat}。"
    if hot:
        detail += f"另附观众弹幕 {hot} 条。"
    return _SOURCE_KIND[key], label, trust, detail


def _next_start_ms(items: list[tuple[float, str]], index: int) -> int:
    """下一条的起始毫秒；最后一条没有下一条时返回 0（交给调用方兜底）。"""
    if index + 1 < len(items):
        return max(0, int(round(items[index + 1][0] * 1000.0)))
    return 0


def segments_from_pairs(
    items: list[tuple[float, str]], *, duration_ms: int = 0, prefix: str = "s"
) -> list[dict[str, Any]]:
    """`[(起始秒, 文本)]` -> 字幕段（与 `transcript.json` 同形状）。

    **结束时间怎么定**：字幕文件只给起点，不给终点。用「下一条的起点」当终点是最贴近
    实际的读法（一句话说到下一句开始）；最后一条没有下一条，退到视频时长，再退到
    「起点 + 4 秒」。三个兜底都要有——没有时长信息时（轻量模式拿不到 duration）用
    固定值比用 0 好：`end_ms == start_ms` 会被 `core/subtitles.py` 的归一化判成
    零长段，编辑器里显示成空行。
    """
    out: list[dict[str, Any]] = []
    total = len(items)
    for index, (start, text) in enumerate(items):
        body = _clean(text)
        if not body:
            continue
        start_ms = max(0, int(round(float(start or 0.0) * 1000.0)))
        end_ms = _next_start_ms(items, index)
        if end_ms <= start_ms:
            end_ms = max(start_ms + 1, int(duration_ms or 0))
        if end_ms <= start_ms:
            end_ms = start_ms + 4000
        out.append(
            {
                "id": f"{prefix}{len(out) + 1}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": body,
                "words": [],
            }
        )
    if total and not out:  # pragma: no cover - 全空文本时上游已判过
        raise WBError("CONFLICT", "字幕解析后没有有效文本")
    return out


def segments_from_srt(
    raw: bytes, *, duration_ms: int = 0
) -> tuple[list[dict[str, Any]], list[str], str]:
    """解析用户给的 srt，返回 `(segments, warnings, encoding)`。

    **不做 15 秒合并**（与 B站字幕那条路相反）：平台字幕被切成几字一行、没有标点，
    所以要按时间窗聚合把断句接回来；用户给的 srt 本来就带着人写好的断句，再合并一次
    等于把正确的句子边界揉掉。这条口径写在 `bili_scribe/core/srt.py` 的模块 docstring
    里，这里是它的下游，不另立一套。

    `warnings` 里放的是「时间轴与视频时长对不上」这类只警告不阻断的话——传错文件是
    唯一能靠时长发现的事故，而它的后果极隐蔽：模型会拿另一条视频的字幕写总结，
    读起来完全通顺，只是内容全错。
    """
    try:
        items, encoding = srt_mod.parse_bytes(raw)
    except srt_mod.SrtError as exc:
        raise WBError("VALIDATION", f"字幕文件读不了：{exc}", str(exc)) from exc
    except UnicodeDecodeError as exc:
        # srt.parse_bytes 声明只抛 SrtError，但它的编码探测在「有 UTF-16 BOM、内容却是
        # 残的」这种输入上会直接把 UnicodeDecodeError 穿出来（实测：b"\\xff\\xfe" + 十几个
        # 字节的 UTF-8 中文）。不接住的话，用户选错文件看到的是整段 traceback，而不是
        # 「这个字幕文件读不了」——同一个失败，两种说法，后者才是他需要的那句。
        raise WBError("VALIDATION", f"字幕文件读不了：{exc}", str(exc)) from exc
    warnings: list[str] = []
    if not srt_mod.is_ordered(items):
        warnings.append("字幕时间轴不是单调递增，已按时间重新排序")
        items = sorted(items, key=lambda pair: pair[0])
    if duration_ms > 0:
        warnings.extend(srt_mod.check_alignment(items, int(duration_ms // 1000)))
    segments = segments_from_pairs(items, duration_ms=duration_ms)
    if not segments:
        raise WBError("VALIDATION", "这个字幕文件里没有可用的文本")
    return segments, warnings, encoding
