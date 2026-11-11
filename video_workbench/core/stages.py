"""项目阶段词表：整个应用**只有这一份**。

为什么单独开一个模块：阶段字符串以前散在三处（`recovery.STAGE_ORDER`、
前端 `ui.js` 的 `STAGES`、以及各 worker 里手写的 `stage="..."`），
多一处副本就多一次"后端加了阶段、前端不认"或"前端写了个后端没有的桶"的机会。
后端要以它做回退顺序、facets 分组、阶段标签；模块属性保持可 patch
（`recovery.STAGE_ORDER is stages.STAGE_ORDER` 是同一对象，测试可以整体替换）。
"""

from __future__ import annotations

# 项目的正常推进顺序。`subtitle` 是"导入时自带字幕"的分支，排在 downloaded 之后；
# 回退时它和 transcribed 同级（两者都代表"有可用文本"）。
STAGE_ORDER = [
    "new",
    "downloaded",
    "subtitle",
    "transcribed",
    "summarized",
    "clipped",
    "merged",
    "published",
]

# 给用户看的阶段名。键必须覆盖 STAGE_ORDER 的每一项（下面的断言守着）；
# 文案改这里一处，列表 chips、详情、命令行都跟着走。
STAGE_LABELS = {
    "new": "待转录",
    "downloaded": "已下载",
    "subtitle": "有字幕",
    "transcribed": "已转录",
    "summarized": "已总结",
    "clipped": "已切片",
    "merged": "已合并",
    "published": "已发布",
}

# 老库里出现过 `analyzed` 这类历史值（前端曾经写死过一个后端不存在的桶）。
# 它们不在顺序里，但列表里仍可能出现；标签函数必须能兜住而不是抛 KeyError。
_MISSING_LABEL = "未知阶段"


def stage_label(key: str) -> str:
    """阶段 key → 中文标签。未知/空值不抛异常（库里可能有历史脏值）。"""
    text = str(key or "")
    if not text:
        return _MISSING_LABEL
    return STAGE_LABELS.get(text) or text


if set(STAGE_LABELS) != set(STAGE_ORDER):  # pragma: no cover - import 期自证
    raise RuntimeError(
        "STAGE_LABELS 与 STAGE_ORDER 不同源："
        f"多出 {sorted(set(STAGE_LABELS) - set(STAGE_ORDER))}，"
        f"缺少 {sorted(set(STAGE_ORDER) - set(STAGE_LABELS))}"
    )
