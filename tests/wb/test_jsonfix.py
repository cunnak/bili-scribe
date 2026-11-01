"""模型给的 JSON 不严格时先修再用；修不了重问一次。

真实现场（2026-11-01 03:38，`job_f15347700469`）：`summary_md` 里带原始换行，
`json.loads` 在 line 3 column 230 报 `Expecting ',' delimiter`，整份总结白跑。
"""
from __future__ import annotations

import json

import pytest

from video_workbench.core import summarizer
from video_workbench.core.summarizer import SummaryLLM
from video_workbench.infra.jsonfix import repair_json


class Flaky:
    """按序回吐；记录每次调用拿到的 messages，便于断言重问内容。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.model = "flaky"
        self.cfg = None
        self.calls: list[list] = []

    def chat(self, messages, **_kwargs):
        self.calls.append(list(messages))
        item = self.replies.pop(0) if self.replies else "{}"
        if isinstance(item, Exception):
            raise item
        return item


def test_repair_survives_raw_newlines_inside_strings():
    broken = '{"one_line": "一行",' + chr(10) + ' "summary_md": "第一段' + chr(10) + "第二段\"}"
    fixed = repair_json(broken)
    assert fixed, "原始换行应当能修"
    assert json.loads(fixed)["summary_md"].endswith("第二段")


def test_repair_drops_trailing_comma():
    fixed = repair_json('{"a": 1, "b": [1, 2,],}')
    assert json.loads(fixed) == {"a": 1, "b": [1, 2]}


def test_repair_strips_markdown_fence_and_chatter():
    fixed = repair_json('好的，这是结果：' + chr(10) + '```json' + chr(10)
                        + '{"a": 2}' + chr(10) + '```' + chr(10) + "希望有帮助")
    assert json.loads(fixed) == {"a": 2}


def test_repair_returns_empty_when_hopeless():
    assert repair_json("我今天不想按格式回话") == ""


def test_adapter_hands_canonical_json_to_the_parser():
    """适配器交出去的必须是**严格可解析**的文本，否则 bili-scribe 那边照样炸。"""
    llm = SummaryLLM(Flaky(['{"one_line": "一行",' + chr(10) + ' "summary_md": "第一段'
                            + chr(10) + '第二段"}']))
    text = llm.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert json.loads(text)["one_line"] == "一行"


def test_adapter_asks_again_once_then_succeeds():
    # 阶梯：主请求 → 分批（正文/时间线）→ 重问。分批都没成，重问救回来。
    client = Flaky(["这不是 JSON", "分批正文不行", "分批时间线也不行",
                    json.dumps({"one_line": "救回来了"})])
    notes: list[str] = []
    llm = SummaryLLM(client)
    text = llm.chat([{"role": "user", "content": "x"}], json_mode=True,
                    note=notes.append)
    assert json.loads(text)["one_line"] == "救回来了"
    assert llm.calls >= 4, llm.calls
    assert any("重问" in note for note in notes)
    # 重问那轮要把上一次的输出带回去，模型才知道要改什么
    echoed = [c for c in client.calls if len(c) >= 2 and c[-2]["role"] == "assistant"]
    assert echoed, "重问请求里没有回显上一次的输出"
    assert "JSON" in echoed[-1][-1]["content"]



def test_adapter_gives_up_after_second_failure_with_truncation_hint():
    # 主请求 → 分批正文 → 分批时间线 → 重问，四次都是废话才放弃
    llm = SummaryLLM(Flaky(["废话一", "废话二", "废话三", "废话四"]))
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        llm.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert "两次" in exc.value.message
    assert "截断" in exc.value.message + exc.value.detail



def test_non_json_mode_is_left_alone():
    llm = SummaryLLM(Flaky(["随便说点人话"]))
    assert llm.chat([{"role": "user", "content": "x"}]) == "随便说点人话"
    assert llm.calls == 1

def test_repair_escapes_quotes_inside_strings():
    """模型直接引用别人的话（正文里出现裸引号）——真实现场 2026-11-01 14:31。"""
    broken = '{"实际改动": "删掉"赋能"这种说法，仅改诊断。", "score": 3}'
    fixed = repair_json(broken)
    assert fixed, "正文里的裸引号应当能修"
    assert json.loads(fixed)["score"] == 3
    assert '"赋能"' in json.loads(fixed)["实际改动"]


def test_repair_keeps_real_structure_when_quotes_are_mixed():
    broken = '{"a": "他说"好"，然后走了", "b": [1, 2,]}'
    fixed = repair_json(broken)
    assert json.loads(fixed) == {"a": '他说"好"，然后走了', "b": [1, 2]}
