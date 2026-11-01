"""单次输出装不下时，正文与时间线分两次要，再把两份 JSON 拼起来。

真实现场（2026-11-01 20:16，`job_9cc034d1a161`）：两小时视频的 level2 打磨，
续写补了一段、重问一次之后仍然不合法，错误尾部断在句中：

    …s在最缺钱时卖最便宜的价）；行政租金消失（园区租金低于市场价是招商工具，证券化后

说明不是「模型不会写 JSON」，是这一份输出（一万六千字正文 + 全量时间线）
单次 completion 装不下。拆成两次问，每次都在预算内。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.core.summarizer import SummaryLLM  # noqa: E402

BODY = {
    "one_line": "一句话结论",
    "summary_md": "第一段\n第二段",
    "key_points": ["要点一", "要点二"],
    "caution": "",
    "comment_short": "短评",
}
TIMELINE = {"timeline": [{"t": "00:12", "text": "开场"}, {"t": "1:34:34", "text": "结账"}]}
# 没闭合的对象：与真实现场同形（断在正文中间）。
TRUNCATED = '{"one_line":"一句话结论","summary_md":"第一段；行政租金消失（园区租金低于市场价'


class ScriptClient:
    """按序回吐，并记下每轮说了什么（要看分批指令有没有到）。"""

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
        item = self.replies.pop(0) if self.replies else "{}"
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(replies):
    client = ScriptClient(replies)
    llm = SummaryLLM(client)  # type: ignore[arg-type]
    return llm, client


def test_split_after_truncation_and_failed_retry():
    llm, client = make_llm([TRUNCATED, "", "还是不行", json.dumps(BODY, ensure_ascii=False),
                            json.dumps(TIMELINE, ensure_ascii=False)])
    notes: list[str] = []
    text = llm.chat(
        [{"role": "user", "content": "生成总结"}], json_mode=True, sink=notes.append
    )
    data = json.loads(text)
    assert data["one_line"] == "一句话结论"
    assert [row["t"] for row in data["timeline"]] == ["00:12", "1:34:34"]
    assert any("分两次" in note for note in llm.notes)
    # 第 1 次主请求、第 2 次续写、第 3 次重问、第 4 次正文、第 5 次时间线
    assert len(client.calls) == 5
    assert "不要输出 timeline" in client.calls[3]["messages"][-1]["content"]
    assert "只输出时间线" in client.calls[4]["messages"][-1]["content"]


def test_split_body_step_gets_bigger_budget():
    llm, client = make_llm([TRUNCATED, "", "还是不行", json.dumps(BODY, ensure_ascii=False),
                            json.dumps(TIMELINE, ensure_ascii=False)])
    llm.chat([{"role": "user", "content": "生成总结"}], json_mode=True, max_tokens=4096,
             sink=lambda _text: None)
    assert client.calls[3]["max_tokens"] >= 8192
    assert client.calls[4]["max_tokens"] >= 4096


def test_split_gives_up_when_body_step_is_garbage():
    llm, _ = make_llm([TRUNCATED, "", "还是不行", "我也不知道", "也不知道"])
    with pytest.raises(WBError) as err:
        llm.chat([{"role": "user", "content": "生成总结"}], json_mode=True,
                 sink=lambda _text: None)
    assert err.value.code == "LLM_BAD_RESPONSE"


def test_split_keeps_body_when_timeline_step_fails():
    llm, _ = make_llm([TRUNCATED, "", "还是不行", json.dumps(BODY, ensure_ascii=False),
                       "时间线我给不出来"])
    text = llm.chat([{"role": "user", "content": "生成总结"}], json_mode=True,
                    sink=lambda _text: None)
    data = json.loads(text)
    assert data["summary_md"].startswith("第一段")
    assert data["timeline"] == []
    assert any("没有时间线" in note for note in llm.notes)
