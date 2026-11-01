"""输出被截断时应该「接着写」，而不是原样重问一遍。

真实现场（2026-11-01 17:33，`job_aa8162716645`）：两小时视频的时间线要 8k tokens，
配置的 `llm.max_tokens` 只有 4096，模型在数组中间被切断，错误信息尾部就是半个对象：

    },{"t":"1:34:34","text":"卖断型隐藏账：最先卖最好的留差

原样重问还是同一长度、还是在同一位置断。所以适配层要能识别这种形状，让它续写。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_workbench.core.summarizer import (  # noqa: E402
    MIN_JSON_TOKENS,
    SummaryLLM,
    _json_budget,
    _looks_truncated,
)

GOOD = {
    "one_line": "一句话结论",
    "summary_md": "第一段\n第二段",
    "key_points": ["要点一", "要点二"],
    "timeline": [{"t": "00:12", "text": "开场"}],
    "caution": "",
    "comment_short": "短评",
}

# 被截断的前半段：对象没闭合，卡在数组中间（与真实现场同形）。
HALF = '{"one_line":"一句话结论","summary_md":"第一段","timeline":[{"t":"00:12","text":"开场"},{"t":"1:34:34"'
# 续写回来的后半段：从断点接着写，自己不以 { 开头。
REST = ',"text":"卖断型隐藏账"}],"key_points":["要点一"],"caution":"","comment_short":"短评"}'


class RecClient:
    """记下每次调用的 kwargs（要看预算），按序回吐。"""

    def __init__(self, replies, model="stub-model"):
        self.replies = list(replies)
        self.model = model
        self.base_url = "http://stub.local/v1"
        self.calls: list[dict] = []

    @property
    def configured(self):
        return True

    def require(self):
        return None

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        item = self.replies.pop(0) if self.replies else json.dumps(GOOD, ensure_ascii=False)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def test_looks_truncated_recognises_half_object():
    assert _looks_truncated(HALF) is True
    assert _looks_truncated(json.dumps(GOOD, ensure_ascii=False)) is False
    # 「压根没写 JSON」不该被当成截断：那种要重问，不是续写。
    assert _looks_truncated("我不想按格式回话。") is False
    assert _looks_truncated("") is False


def test_budget_floors_at_twelve_thousand():
    # 两小时视频 level2 正文一万六千字：8192 token 到头会断在半句（v7 现场），抬到 12288
    assert MIN_JSON_TOKENS == 12288
    assert _json_budget(4096) == 12288         # 配置太小 → 抬到下限
    assert _json_budget(None) == 12288         # 没传 → 用下限
    assert _json_budget(6000) == 12288         # 两倍仍在下限之下 → 抬到下限
    assert _json_budget(9000) == 16384         # 两倍超上限 → 封顶
    assert _json_budget(20000) == 16384        # 封顶



def test_truncated_output_is_continued_not_reasked():
    client = RecClient([HALF, REST])
    llm = SummaryLLM(client, model="stub-model")
    notes: list[str] = []

    text = llm.chat(
        [{"role": "user", "content": "写一份总结"}],
        json_mode=True,
        note=notes.append,
        max_tokens=4096,
    )

    payload = json.loads(text)  # 必须严格可解析
    assert payload["one_line"] == "一句话结论"
    assert payload["timeline"][0]["t"] == "00:12"
    # 时间线里那条被截断的记录被续写补齐了
    assert payload["timeline"][1]["text"] == "卖断型隐藏账"

    assert len(client.calls) == 2, "只该续写一次，不该重问"
    assert any("截断" in n for n in llm.notes), llm.notes
    assert any("接着写" in s for s in notes), notes
    # 续写那一轮的预算必须抬到下限之上，否则还是断在同一处
    assert client.calls[1]["max_tokens"] >= MIN_JSON_TOKENS
    assert client.calls[1]["json_mode"] is True
    # 续写要带上「从断开处继续」的指令，且不能要求它从头再来
    last = client.calls[1]["messages"][-1]["content"]
    assert "接着写" in last and "不要重复" in last


def test_continuation_that_also_truncates_falls_back_to_reask():
    # 续写失败 → 分批失败（两步都给废话）→ 才重问
    client = RecClient([HALF, HALF, "分批正文不行", "分批时间线也不行",
                        json.dumps(GOOD, ensure_ascii=False)])
    llm = SummaryLLM(client, model="stub-model")

    text = llm.chat(
        [{"role": "user", "content": "写一份总结"}], json_mode=True, max_tokens=4096
    )

    assert json.loads(text)["one_line"] == "一句话结论"
    assert len(client.calls) >= 5, "续写 + 分批都失败 → 再重问一次"
    assert any("重问" in n for n in llm.notes), llm.notes
    # 重问那轮同样要抬预算
    assert client.calls[4]["max_tokens"] >= MIN_JSON_TOKENS



def test_repairable_output_short_circuits_without_extra_call():
    raw = '{"one_line":"一行",' + chr(10) + '"summary_md":"第一段' + chr(10) + '第二段"}'
    client = RecClient([raw])
    llm = SummaryLLM(client, model="stub-model")

    text = llm.chat([{"role": "user", "content": "写一份总结"}], json_mode=True)

    assert json.loads(text)["one_line"] == "一行"
    assert len(client.calls) == 1
    assert llm.notes == []
