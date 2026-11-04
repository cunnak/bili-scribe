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
from wb_parity_tiers import loose_env  # noqa: F401  档位 fixture 由 pytest 注入


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


# 档位=宽松：交出去「严格可解析」靠 repair 这一步，严格档原样透传。
def test_adapter_hands_canonical_json_to_the_parser(loose_env):
    """适配器交出去的必须是**严格可解析**的文本，否则 bili-scribe 那边照样炸。"""
    llm = SummaryLLM(Flaky(['{"one_line": "一行",' + chr(10) + ' "summary_md": "第一段'
                            + chr(10) + '第二段"}']))
    text = llm.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert json.loads(text)["one_line"] == "一行"


# 档位=宽松：重问救场是③的最后一档。
def test_adapter_asks_again_once_then_succeeds(loose_env):
    # 阶梯：主请求 → 分批（正文/时间线）→ 重问。分批都没成，重问救回来。
    client = Flaky(["这不是 JSON", "分批正文不行", "分批时间线也不行",
                    json.dumps({"one_line": "救回来了"})])
    notes: list[str] = []
    llm = SummaryLLM(client)
    text = llm.chat([{"role": "user", "content": "x"}], json_mode=True,
                    note=notes.append)
    assert json.loads(text)["one_line"] == "救回来了"
    # 次数钉**精确值**，不钉下界：`>= 4` 是下界，5 也叫 ≥4，多花一次请求它不响。
    # 探针实测真值 5（`.venv/Scripts/python.exe <Temp>/t38_jsonfix_count.py`）。
    assert llm.calls == 5, f"阶梯调用数不是 5：{llm.calls}\nnotes={notes}"

    def _stage(note: str) -> str:
        if "分两次要" in note:
            return "split"          # 正文/时间线分批这一步
        if "正文这一步" in note:
            return "body-bad"       # 正文那批没给出可用 JSON
        if "重问" in note:
            return "retry"          # 严格化重问一次
        return f"other:{note}"

    # 次序＝先分批 → 正文坏 → 重问 → **又**分批（第二次 split 才是 5 次的来源）。
    # 有人把重问之后改成直接收摊，这里会少一次 split 并连带 calls 变 4 —— 那是要谈的
    # 成本变更，不是顺手改绿；想让它绿得先说清「省掉的这一轮拿回了什么」。
    assert [_stage(n) for n in notes] == ["split", "body-bad", "retry", "split"], notes
    assert any("重问" in note for note in notes)
    # 重问那轮要把上一次的输出带回去，模型才知道要改什么
    echoed = [c for c in client.calls if len(c) >= 2 and c[-2]["role"] == "assistant"]
    assert echoed, "重问请求里没有回显上一次的输出"
    assert "JSON" in echoed[-1][-1]["content"]



# 档位=宽松：钉「阶梯用尽之后的报错」；默认档那句 `设 WB_SUMMARY_PARITY_STRICT=0 后重试` 是另一种失败，两者不许混用。
def test_adapter_gives_up_after_second_failure_with_truncation_hint(loose_env):
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
