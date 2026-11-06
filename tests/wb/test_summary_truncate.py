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

from wb_parity_tiers import loose_env as loose_env  # noqa: F401,E402  档位 fixture 由 pytest 注入

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


# 档位=宽松：②补救预算的阶梯只在宽松档生效（严格档由 parity 名下的 B4 逐轮钉直通）。
def test_budget_floors_at_twelve_thousand(loose_env):
    """补救阶梯的三条不变量：下限、封顶、以及「补救不许比主请求还小」。

    以前为什么绿（2026-11-05 之前）：最后一行写的是 `_json_budget(20000) == 16384`，钉的
    正是「base 高于封顶时照样被削到 16384」。默认值 4096→32768（task-52）之后那就是
    **倒挂**：坏 JSON 的重问/续写/分批反而只拿到 16384，比主请求少一半以上，越修越容易
    被截断。task-53（commit `c8b3376`）给阶梯补了下界
    （`max(base, min(max(floor, base*2), 16384))`），于是那行旧断言**该红**，不是回归。

    现在绿在哪——三面各钉一条，缺一面就没牙（牙齿见 `%TEMP%/parity-t56` 两个变体）：
    1. **下限**：小配置与 `None` 抬到 `MIN_JSON_TOKENS`；
    2. **封顶**：`base ≤ cap` 且两倍超 cap 时结果等于 cap —— 这条就是「有人把 16384 删了」
       的探测器（变体 B 实测只红这一组）；
    3. **下界（task-53 的规则本身）**：任何 base 的补救预算都 `>= base`，且 base 高于 cap 时
       **必须高于 cap**。刻意不写 `== base`：今天等于 base 只是两条边界的交集，不是规则；
       将来谁抬高 cap，规则照旧成立、实现可以变。

    `cap` 为什么写成字面量：产品里 16384 是 `video_workbench/core/summarizer.py` 第 1173 行
    的**裸数字**（`MIN_JSON_TOKENS` 才是常量），没有可导入的名字。这里把它登记成局部读数并
    断言「产品此刻确实是这个数」，而不是再造一个看起来像常量的 owner——vendor 哪天提成常量，
    这条与那行注释要一起改，改不到就会红，不会静默漂移。

    owner 分工（lead 的「一个 owner」要求）：本条只钉 `_json_budget()` **这个函数**的阶梯语义；
    「严格档每轮预算 == 配置值」由 `tests/wb/test_summary_parity.py::
    test_default_tier_every_round_budget_equals_the_configured_value`（B4）钉。两处不重叠：
    B4 看的是发出去的请求，本条看的是补救那几步的算法；B4 的 docstring 现在只指路到这里，
    不重复断言。
    """
    # 产品里的裸封顶值（`MIN_JSON_TOKENS` 才是常量，这个没有名字可导入）。
    # 下面第 2 组的全部前提都建立在它之上；vendor 哪天把它提成常量，这一行与
    # `test_summary_parity.py` 的 AST 现读要一起改，改不到会红，不会静默漂移。
    cap = 16384
    assert MIN_JSON_TOKENS == 12288

    # 1) 下限
    assert _json_budget(4096) == MIN_JSON_TOKENS  # 配置太小 → 抬到下限
    assert _json_budget(None) == MIN_JSON_TOKENS  # 没传 → 用下限
    assert _json_budget(6000) == MIN_JSON_TOKENS  # 两倍仍在下限之下 → 抬到下限

    # 2a) 封顶的**真**探测器：只取「未封顶值严格大于 cap」且 base 不高于 cap 的格子
    for base in (9000, 12288, cap):
        assert base <= cap < max(MIN_JSON_TOKENS, base * 2), (
            f"夹具前提不成立：base={base} 落不到「该被封顶」那一格"
        )
        assert _json_budget(base) == cap, f"封顶丢了：base={base} 实得 {_json_budget(base)}"

    # 2b) 边界格（lead 例子里的那一格，如实标注它测不到什么）：8192 的未封顶值**恰好等于**
    #     cap ⇒ 封顶删不删都是 16384。它能钉的是「task-53 的下界没把这一格改动」，
    #     不能当封顶的证据——证据在 2a。
    assert max(MIN_JSON_TOKENS, 8192 * 2) == cap, "8192 这一格的前提是未封顶值恰好等于 cap"
    assert _json_budget(8192) == cap, (
        f"改前读数被移动了：_json_budget(8192) 实得 {_json_budget(8192)}"
    )

    # 3) 下界：被翻正的那一格现在钉的是规则
    for base in (cap + 1, 20000, 32768, 65536):
        got = _json_budget(base)
        assert got >= base, f"倒挂回来了：base={base} 的补救只有 {got}，比主请求还小"
        assert got > cap, f"base={base} 高于封顶却被削到 {got}——封顶越过了下界"


# 档位=宽松：续写是③阶梯的一步。
def test_truncated_output_is_continued_not_reasked(loose_env):
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


# 档位=宽松：续写→拆分→重问的降级顺序，属③。
def test_continuation_that_also_truncates_falls_back_to_reask(loose_env):
    # 续写失败 → 分批失败（两步都给废话）→ 才重问
    client = RecClient(
        [HALF, HALF, "分批正文不行", "分批时间线也不行", json.dumps(GOOD, ensure_ascii=False)]
    )
    llm = SummaryLLM(client, model="stub-model")

    text = llm.chat([{"role": "user", "content": "写一份总结"}], json_mode=True, max_tokens=4096)

    assert json.loads(text)["one_line"] == "一句话结论"
    assert len(client.calls) >= 5, "续写 + 分批都失败 → 再重问一次"
    assert any("重问" in n for n in llm.notes), llm.notes
    # 重问那轮同样要抬预算
    assert client.calls[4]["max_tokens"] >= MIN_JSON_TOKENS


# 档位=宽松：repair_json 短路是③的第一步；严格档不修 JSON。
def test_repairable_output_short_circuits_without_extra_call(loose_env):
    raw = '{"one_line":"一行",' + chr(10) + '"summary_md":"第一段' + chr(10) + '第二段"}'
    client = RecClient([raw])
    llm = SummaryLLM(client, model="stub-model")

    text = llm.chat([{"role": "user", "content": "写一份总结"}], json_mode=True)

    assert json.loads(text)["one_line"] == "一行"
    assert len(client.calls) == 1
    assert llm.notes == []
