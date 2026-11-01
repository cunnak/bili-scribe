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
    # 时间线那一步没结果：键还在、值为空——正文照旧交付，不连坐。
    assert data["timeline"] == []


def test_dense_timeline_is_left_alone():
    """时间线本来就够密，就不要再花一次调用去补。"""
    dense = {"timeline": [{"t": "%02d:%02d" % (i // 2, (i % 2) * 30), "text": "第%d段" % i}
                          for i in range(40)]}
    llm, client = make_llm([json.dumps({**BODY, **dense}, ensure_ascii=False)])
    pump = [{"role": "user", "content": "字幕 [00:00] 开头 [120:00] 结尾"}]
    text = llm.chat(pump, json_mode=True, sink=lambda _t: None)
    assert len(json.loads(text)["timeline"]) == 40
    assert len(client.calls) == 1
    assert llm.notes == []


def test_sparse_timeline_is_topped_up():
    """两小时视频只给 20 条时间线时，单独补一轮到目标条数。"""
    sparse = {"timeline": [{"t": "1:%02d" % i, "text": "第%d段" % i} for i in range(20)]}
    fuller = {"timeline": [{"t": "%02d:%02d" % (i, 0), "text": "补第%d段" % i} for i in range(55)]}
    llm, client = make_llm([json.dumps({**BODY, **sparse}, ensure_ascii=False),
                            json.dumps(fuller, ensure_ascii=False)])
    pump = [{"role": "user", "content": "字幕 [00:00] 开头 [119:30] 结尾"}]
    text = llm.chat(pump, json_mode=True, sink=lambda _t: None)
    rows = json.loads(text)["timeline"]
    assert len(rows) == 55
    assert len(client.calls) == 2
    assert "至少 60 条" in client.calls[1]["messages"][-1]["content"]
    assert any("补到 55 条" in note for note in llm.notes)


def _sparse(messages):
    """两小时视频、但提示词里只有开头十几分钟的时间戳（真实形状）。"""
    return list(messages) + [
        {"role": "user", "content": "字幕 [02:49] 断更复播 [13:40] 这里就断了"}
    ]


def test_target_reads_real_duration_not_the_prompt():
    """片长不能用提示词里能看到的最后一个时间戳算。

    真实现场：最后那次 JSON 请求里只剩分段摘要，时间戳停在前 14 分钟，
    按它算分母会得出「13 分钟 → 20 条就够」，两小时视频的时间线永远只有 20 条。
    """
    from video_workbench.core.summarizer import _timeline_target

    guess = _timeline_target(_sparse([]))
    real = _timeline_target(_sparse([]), 2 * 60 * 60 * 1000)
    assert guess == 20  # 只看提示词：自洽但错
    assert real == 60  # 真片长：目标 60 条


def test_index_is_attached_and_reaches_the_timeline_prompt():
    """全片索引要挂到适配器上，并真的进到补写那一轮的提示词里。"""
    from video_workbench.core.summarizer import _attach_timeline, timeline_marks

    segments = [
        {"start_ms": 0, "text": "开场第一句"},
        {"start_ms": 61_000, "text": "一分钟处"},
        {"start_ms": 7_140_000, "text": "一小时五十九分"},
    ]
    marks = timeline_marks(segments)
    assert "00:00:00" in marks and "00:01:01" in marks and "01:59:00" in marks

    llm = SummaryLLM(ScriptClient([]), model="stub-model")
    _attach_timeline(llm, {"duration_sec": 7200}, segments)
    assert llm.duration_ms == 7_200_000
    assert "01:59:00" in llm.timeline_index


def test_sparse_timeline_is_topped_up_with_index_in_prompt():
    llm = SummaryLLM(ScriptClient([json.dumps(TIMELINE)]), model="stub-model")
    llm.duration_ms = 7_200_000
    llm.timeline_index = "00:00:00 开场\n01:59:00 收尾"
    fixed = json.dumps({"one_line": "一", "summary_md": "正文",
                        "timeline": [{"t": "02:49", "text": "开场"}]})
    llm._finalize(fixed, _sparse([]), lambda _m: None, None, 4096)
    sent = json.dumps(llm.client.calls[0]["messages"], ensure_ascii=False)
    assert "全片的时间轴索引" in sent and "01:59:00" in sent
    assert "至少 60 条" in sent
