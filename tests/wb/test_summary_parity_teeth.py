"""档位钉法的灵敏度自证（task-9：证明默认档翻正不是「写在注释里的一句话」）。

## 这个文件为什么必须在盘上
它最早是 `tests/wb/test_zz_tier_sensitivity.py`，文件头自己写着「一次性探针（跑完即删）」，
我照那句话删了，却在交付里把它的 8 项检查当成果报——**交付与仓库现状不一致，比一条红严重**。
现在它作为常驻用例落盘，并且把探针版的两个毛病改掉：
1. 探针用裸赋值 `A._parity_strict = lambda: strict` 且**跑完不还原**。
   `video_workbench.core.summarizer` 在同一次 pytest 会话里是同一个模块对象，
   漏出去的假函数会污染后面所有档位用例（那才是真的「两侧一起错」）。这里换成 try/finally
   还原，并单独立一条 `test_patch_does_not_leak` 守这个还原。
2. 探针把 8 项检查塞进一个函数，任何一项红了只能看到「整体失败」。拆成 8 条具名单元，
   板上和报错里都能直接指出是哪一半没牙。

## 为什么用运行时替换，而不是改产品文件
红线是只改 `tests/**`，`video_workbench/**` 归 vendor-engineer。要证明「翻正之后这些钉法真的
会跟着翻」，最干净的办法是把档位判定函数临时钉成某档：产品里所有档位分支都是**每次调用现读**
`_parity_strict()`（`video_workbench/core/summarizer.py:73-78`，没有 import 期缓存——那件事本身
由 `test_summary_parity.py::test_parity_tier_switches_within_one_process` 守），钉住它等价于
伪造一次默认值翻转，而且不碰任何产品字节。

## 为什么只观察这几个量，不重跑整条链路
整条 `run()` 一换档就会改变**请求次数**，同一份脚本在严格档够用、在宽松档会多出续写；
那时断言错位到底是档位问题还是脚本问题，分不开。所以这里只看「档位直接决定」的量：
`_json_budget` 的数值、`_strict_hint` 念不念开关、干净 JSON 发几次、只读捕获留不留条数、
坏 JSON 发几次。端到端一致性是 `test_summary_parity.py` 的活。

## 分工：⑦/⑧ 只数次数、不判异常类型（lead 判卷结论，别当漏写）
`measured()` 里那句 `except Exception`（`# noqa: BLE001`）单看是松的：产品若在第 1 次请求后
抛 `AttributeError`，⑦ 照样绿。这是**有意的**——本文件的判据只有一件事：
「请求次数随不随档位变」。失败形状（异常类型、`WBError.code`、前半句文案、末尾开关句）
在别处钉死了：
`test_summary_parity.py::test_default_tier_malformed_json_names_the_switch_and_keeps_the_old_prefix`
走 `pytest.raises(WBError)` + `code == "LLM_BAD_RESPONSE"` + 前半句逐字 + 末尾含
`WB_SUMMARY_PARITY_STRICT=0`；宽松档那一半由同文件的 `test_loose_tier_failure_does_not_name_the_switch`
与 B 组的 `test_strict_mode_adapter_does_not_retry_malformed_json` 收。
所以整套判据是**「次数归本文件，形状归 B3」**：在这儿再加一条类型断言就是与 B3 重复，
还会让「档位有没有牙」被形状问题掩盖。

同理，⑥ 那条「手工把 dense 造成 25 条再喂 `_apply_dense`」**不是** B1 的证据——
它只证明覆盖函数不看档位这一件事；B1（默认档从生产入口留住 25 条、且零额外请求）的
证据在 `test_summary_parity.py::test_default_tier_keeps_a_timeline_longer_than_the_cap`。
读的人别把 ⑥ 当成守住了那条命令。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(ROOT), str(ROOT / "tests" / "wb")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import test_summary_parity as T  # noqa: E402  复用同一份 FakeLLM/PAYLOAD，别造第二套假数据

A = importlib.import_module("video_workbench.core.summarizer")

#: 25 条时间线：超过 bs 的 20 条硬上限，①才有东西可保。
ROWS = 25

#: 喂给 `_json_budget` 的入参。**它只需要低于下限**（`A.MIN_JSON_TOKENS`），今天
#: `config.yaml` 实配正好是 4096，所以拿它当样本；但它不是合同里的期望值——
#: task-18 已把「把配置值抄进用例当期望」那种写法从 parity 里清掉（那等于自己考自己）。
#: 真正被钉的是：同一入参在严格档原样回去、在宽松档抬到下限。
SMALL_TOKENS = 4096

#: 也是**夹具输入**，不是合同期望值。与 `SMALL_TOKENS` 分开命名，是为了让读的人一眼看出
#: 下面那条逐轮牙齿不依赖「默认值等于多少」——它只要求「每一轮都等于当次配置值」。
BUDGET_CFG_TOKENS = 4096


@pytest.fixture
def clean_env(monkeypatch):
    """未设开关＝默认档；真正的档位由 `measured()` 里的临时替换决定。"""
    monkeypatch.delenv(A.PARITY_ENV, raising=False)
    yield


def measured(strict: bool) -> dict[str, object]:
    """把 `_parity_strict` 临时钉成某档，读出这一档下 5 个量的实测值，然后还原。"""
    original = A._parity_strict
    A._parity_strict = lambda: strict
    try:
        # 全部观察值必须在替换还生效时读出来。第一版把 `budget` / `hint` 写进了
        # `finally` 之后的 return 里——那时函数已经还原成真的 `_parity_strict`，
        # 于是「宽松档」这一列其实量的是默认档（永远 4096/念开关），两条断言假装在分档。
        budget = A._json_budget(SMALL_TOKENS)
        hint = f"{A.PARITY_ENV}=0" in A._strict_hint("前半句：X")

        rows = [{"t": f"00:{i:02d}", "text": f"第 {i} 条"} for i in range(1, ROWS + 1)]
        payload = {**T.PAYLOAD, "timeline": rows}

        # 干净 JSON：两档都该一次都不多——①是只读捕获，③在好回复上用不上。
        good = T.FakeLLM([payload])
        A.SummaryLLM(good, model="stub").chat(
            [{"role": "user", "content": "写一份总结"}], json_mode=True
        )

        # 只读捕获：模型给了 25 条，dense 就必须留住 25 条（①与档位无关）。
        # 注意这是「覆盖函数不看档位」的单元证据，不是 B1 的端到端证据（见文件头分工）。
        captured = A.SummaryLLM(T.FakeLLM([]), model="stub")
        captured.dense_timeline = rows
        kept = len(
            A._apply_dense(
                A.bs_summarizer.Summary(timeline=rows[:20], model="stub"), captured
            ).timeline
        )

        # 坏 JSON：严格档一次即弃；宽松档走阶梯，必然 >1。
        # 这里**故意不判异常类型/错误码**——判据只有「次数随不随档位变」；
        # 形状（WBError / LLM_BAD_RESPONSE / 前半句 / 末尾开关句）归
        # test_summary_parity.py 的 B3 与 B 组，见文件头「分工」一节。
        bad = T.FakeLLM(["这不是 JSON"] * 12)
        try:
            A.SummaryLLM(bad, model="stub").chat(
                [{"role": "user", "content": "写一份总结"}], json_mode=True
            )
        except Exception:  # noqa: BLE001  两档的出口形状不同（严格档原样抛、宽松档阶梯用尽后抛别的），这里只数请求次数
            pass
    finally:
        A._parity_strict = original

    return {
        "budget": budget,
        "hint": hint,
        "good_calls": len(good.requests),
        "kept": kept,
        "bad_calls": len(bad.requests),
    }


@pytest.fixture(scope="module")
def both_tiers():
    """两档各测一次，8 条断言共用同一份观察值（测一次要发 4 次假请求，别重复付）。"""
    return {"strict": measured(True), "loose": measured(False)}


def test_budget_strict_restores_the_configured_value(both_tiers, clean_env):
    """①：严格档 `_json_budget` 必须把入参**原样**回去，不抬不封（B4 靠这个量）。

    入参见上面的 `SMALL_TOKENS`（低于下限即可），期望值不抄配置——抄了就等于拿
    夹具的输入当输出比，恒真。
    """
    assert both_tiers["strict"]["budget"] == SMALL_TOKENS, (
        f"严格档预算没原样还原：入参 {SMALL_TOKENS}，实得 {both_tiers['strict']['budget']}"
    )


def test_budget_loose_lifts_to_the_floor(both_tiers, clean_env):
    """②：宽松档同一个入参必须抬到下限 12288（H2 与 truncate 那批 loose 用例靠这个量）。

    这条与上一条只差一个数字，但它是整套钉法的牙：如果 `_json_budget` 不再看档位，
    两条会同时绿或同时红，而现在它们各自只在一档成立。
    """
    assert both_tiers["loose"]["budget"] == A.MIN_JSON_TOKENS == 12288, (
        f"宽松档没抬到下限：{both_tiers['loose']['budget']}"
    )


def test_strict_failure_message_names_the_switch(both_tiers, clean_env):
    """③：严格档的坏 JSON 报错必须念出 `WB_SUMMARY_PARITY_STRICT=0`（降级可见，B3）。"""
    assert both_tiers["strict"]["hint"] is True, "严格档没把开关念出来"


def test_loose_failure_message_stays_silent_about_the_switch(both_tiers, clean_env):
    """④：宽松档的失败不许提开关——那时阶梯已经用尽，提它只会误导（B3 的反面对例）。"""
    assert both_tiers["loose"]["hint"] is False, "宽松档不该提对账开关"


def test_clean_reply_never_asks_twice_in_either_tier(both_tiers, clean_env):
    """⑤：干净 JSON 时两档都只发 1 次——钉住「①的捕获与②的预算都不许在正常路径偷请求」。"""
    assert both_tiers["strict"]["good_calls"] == 1, (
        f"严格档干净回复发了 {both_tiers['strict']['good_calls']} 次"
    )
    assert both_tiers["loose"]["good_calls"] == 1, (
        f"宽松档干净回复发了 {both_tiers['loose']['good_calls']} 次——"
        "多出来的是补密那一轮，稀疏素材才该付那个钱"
    )


def test_dense_override_keeps_every_row_in_either_tier(both_tiers, clean_env):
    """⑥：模型给 25 条时，两档的覆盖结果都必须是 25 条——①不受档位开关影响。

    这是拍板「默认严格但保留①」的核心：覆盖函数不许重新被 `if not _parity_strict()` 包住。
    端到端那一半在 `test_summary_parity.py::test_default_tier_keeps_a_timeline_longer_than_the_cap`。
    """
    assert both_tiers["strict"]["kept"] == ROWS, (
        f"严格档把超上限的条目又截掉了：{both_tiers['strict']['kept']}"
    )
    assert both_tiers["loose"]["kept"] == ROWS, f"宽松档也丢了条目：{both_tiers['loose']['kept']}"


def test_bad_reply_fails_after_one_request_in_strict_tier(both_tiers, clean_env):
    """⑦：严格档坏 JSON 必须一次即弃（③阶梯整条不走，B 组那条端到端用例的同源证据）。"""
    assert both_tiers["strict"]["bad_calls"] == 1, (
        f"严格档多问了 {both_tiers['strict']['bad_calls']} 次——加固没关干净"
    )


def test_bad_reply_walks_the_ladder_in_loose_tier(both_tiers, clean_env):
    """⑧：宽松档坏 JSON 必须真的走阶梯（>1 次），否则「阶梯还在」只是注释。"""
    assert both_tiers["loose"]["bad_calls"] > 1, (
        f"宽松档一次就弃了，阶梯没在跑：{both_tiers['loose']['bad_calls']}"
    )


def test_patch_does_not_leak(clean_env):
    """临时替换必须还原：漏出去的假 `_parity_strict` 会把后面所有档位用例一起带跑。

    这条不测产品，测的是本文件的写法——`measured()` 改成不设 finally、或者有人把替换
    搬到 fixture 外面，这里立刻红。顺带确认未设 env 时默认仍是严格档。
    """
    assert A._parity_strict.__name__ == "_parity_strict", (
        f"`_parity_strict` 还是被替换着的：{A._parity_strict!r}"
    )
    assert A._parity_strict() is True, "未设开关必须回到默认严格档"


def test_a_per_round_budget_boost_is_caught_by_the_b4_check(clean_env):
    """灵敏度（只证「抓得到」，不重复结论）：给严格档第三轮单独加预算，B4 那套判据必须抓到。

    分工（重要，别当成第二条逐轮锁）：
    `test_summary_parity.py::test_default_tier_every_round_budget_equals_the_configured_value`
    钉的是「**今天**没有逐轮抬升」，那是 B（按轮分配预算）被用户否决后的常驻锁；
    本条不重复那个 claim（重复＝第二个 owner，`AGENTS.md` 第六节），只做一件事：
    把「将来有人好心给 revise 轮加预算」这个坏法注入一次，证明 B4 用的那套判据
    （同一个 `_normalized()` + 同一个逐轮比较）**当场**抓到偏离，而不是因为两侧一起
    变化而照样绿。

    坏法为什么打在 `SummaryLLM._parity_max_tokens` 上：那是严格档每一轮预算的唯一决策点
    （`video_workbench/core/summarizer.py` 里 `SummaryLLM.chat()` 在 `_parity_strict()` 为真时
    把 `max_tokens=self._parity_max_tokens(max_tokens)` 交给注入的 client），所以即使这里注入的
    是 `FakeLLM`，请求里记下的仍然是**产品算出来的那个数**；类属性每次调用现读、没有 import 期
    缓存，运行时替换等价于「产品改成按轮分配」，且不碰一个产品字节。「只抬第三轮」正是按轮分配
    最自然的落地形状：generate/critique 够用了，而 revise 带着素材正文 + 待评稿 + 批判意见、
    输入最大（`AGENTS.md` 第四节实测那一轮 55013 字符），所以「给最后一段多留点输出」就是
    这个坏法本人。
    """
    cfg = T.make_cfg(max_tokens=BUDGET_CFG_TOKENS)
    original = A.SummaryLLM._parity_max_tokens
    seen: list[object] = []
    injected: list[int] = []

    def boosted(self, max_tokens):  # self 是被注入的 SummaryLLM 实例
        value = original(self, max_tokens)
        seen.append(value)
        if len(seen) == 3:  # 只把第三轮（revise）翻倍
            boosted_value = (value or cfg.llm.max_tokens) * 2
            injected.append(boosted_value)
            return boosted_value
        return value

    A.SummaryLLM._parity_max_tokens = boosted  # type: ignore[method-assign]
    try:
        project, segments = T._dense_project(), T._dense_segments()
        client = T.FakeLLM(list(T.three_pass_script()))
        A.run(project, segments, client=client, cfg=cfg, ctx=T.QuietCtx(), role="wengao")
    finally:
        A.SummaryLLM._parity_max_tokens = original  # type: ignore[method-assign]

    assert len(seen) >= 3, f"三轮都没跑到，坏法根本没注入进去：{seen!r}"
    assert injected, "第三次请求没经过 `_parity_max_tokens`，坏法没落到预算上"
    normalized = T._normalized(client.requests, cfg)
    deviated = [
        index for index, item in enumerate(normalized) if item["max_tokens"] != cfg.llm.max_tokens
    ]
    assert deviated == [2], (
        "注入「第三轮预算翻倍」后，B4 那套逐轮判据必须且只能抓到第 3 次请求："
        f"实得下标 {deviated}，逐轮读数 {[item['max_tokens'] for item in normalized]}"
    )
    # 抓到的偏离必须就是产品**真发出去**的那个数：比的是「注入函数返回的值 == 请求里记下的
    # 值」，所以管道断了（客户端没把 max_tokens 传下去）照样会红，不是自己考自己。
    # 刻意不写 `== cfg.llm.max_tokens * 2`：那种写法在「外部又叠一次同样注入」时会假红
    # （驱动器 A 变体就叠了），而本条要证的只是「偏离看得见、且看得准」。
    assert normalized[2]["max_tokens"] == injected[-1], (
        f"第三次请求记下的预算不是注入返回的那个数：请求 {normalized[2]['max_tokens']!r}，"
        f"注入 {injected[-1]!r}（逐轮读数 {[i['max_tokens'] for i in normalized]}）"
    )
    # 还原腿：`test_patch_does_not_leak` 守的是 `_parity_strict`，这个类属性是另一处运行时
    # 替换，形状相同、owner 不同，所以在本条末尾自证，不给漏还原留活口。
    assert A.SummaryLLM._parity_max_tokens is original, "类属性没还原，会污染后面所有用例"
