"""迁入副本与 bili_scribe 的**输出一致性对账**（task-5 起，task-9 翻正档位）。

## 档位：默认就是严格档（2026-11-03 拍板，task-9 的主体就是把矩阵翻过来）

| `WB_SUMMARY_PARITY_STRICT` | 档位 | ①时间线补密 | ②输出预算抬升 | ③坏 JSON 阶梯 |
|---|---|---|---|---|
| **未设**（用户的常态） | **严格（默认）** | 生效（task-11 的只读捕获，实测 25 条留住、仍 3 次请求） | 关 | 关，一次即失败并报出开关 |
| `0` / `false` / `off` | 宽松 | 生效 | 12288/翻倍/封顶 16384 | 修复→续写→拆分→重问 |
| 其它任何值（`1`/`TRUE`/`2`…） | 严格 | 同上 | 关 | 关 |

实测这行表来自 `video_workbench/core/summarizer.py:73-78`
（`not in PARITY_OFF_VALUES`，`PARITY_OFF_VALUES = {"0","false","off"}`）与本次探针。
**这个文件里每条用例都必须明确自己钉哪一档**；task-8 之前「默认=宽松」是历史状态，
现在它是错的假设——留着这个假设的用例会永远绿却什么都没守（翻正前那两条就是这样）。

## 什么叫「一致」——按 lead 2026-11-03 钉死的可执行判据

严格档（**默认即此**）下：

1. **请求序列一致**：WB 侧发出的每一次请求，`messages` 逐条、次数、`json_mode`、
   `temperature`、`max_tokens` 都与 bili_scribe 在同一 Bundle、同一 cfg 下完全相同；
2. **产出一致**：最终 `Summary` 逐字段相同。

**不比传输层实现。** bili_scribe 走 `requests`，WB 走自己的 `ChatClient`（urllib、
超时、错误码），那是两侧各自的既有事实，不在复刻范围内（lead 明确要求）。所以这里
断言的是「发到模型那边的**内容**」，不是「用哪个 HTTP 库」。
`max_tokens` 的归一理由见 `_normalized()`：上游根本不写这个参数（`bili_scribe/infra/llm.py:259`
是 `max_tokens or self.cfg.max_tokens`），而 WB 的 `ChatClient` 在 `None` 时
**干脆不发这个字段**——那是请求内容的真实差异，所以严格档由 `_parity_max_tokens()` 把
`cfg.llm.max_tokens` 补上（两侧同值才算一致）。**期望值一律现读 `cfg.llm.max_tokens`**，
不抄 `config.yaml` 里的数字当合同：那既是「自己考自己」（夹具本来就是照它设的），
又会在用户改配置（真模型实测：推理型 auto 在 4096 下把预算全烧在思考上、16384 才出
合法 JSON）时把用例变成假红。B4 那条现在拿 4096 与 16384 各跑一遍，证明这个数是流过去的。

## 方案 A：什么必须相同、什么允许不同（2026-11-03 用户拍板，task-18 的重头）

用户选了方案 A：**WB 的角色提示词真源换成 `video_workbench/resources/prompts/` 里的
txt 全文**，`bili_scribe` 一个字不改、继续用 `roles.py` 里的压缩版。A 的定义就是主动
打破「两侧 system 逐字相等」，所以这条差异**登记为有意差异**，处置方式不是放宽成
「不等就通过」，而是两侧各按构造规则断言（`assert_sequences_match_plan_a`）：

| 维度 | 要求 |
|---|---|
| 调用次数 | **必须相同**（A 不改轮次） |
| 阶段顺序指纹 `json_mode` | **必须相同**（三轮回形如 `[True, False, True]`） |
| 非 system 消息正文（用户素材、草稿、批判清单、本次要求） | **逐字相同** |
| `temperature` / `max_tokens`（还原后的有效值） | **必须相同** |
| `Summary.to_dict()` | **逐字相同**（提示词换了，输出形状不许换） |
| timeline 上限语义、错误码 | **必须相同** |
| 角色 system 的**正文** | 允许不同：bs = 压缩版 + OUTPUT_CONTRACT；WB = txt 全文 + OUTPUT_CONTRACT |
| 自我批判那轮的 system（`role.critique`） | **必须相同** ——A 只换角色本体，不换批判提示词 |

三条硬约束跟着 A 一起钉：契约**永远在末尾**（txt 原文里有「打印每个阶段完整输出」这类
指令，`roles.py:52-56` 那段契约就是来压住它们的，放前面 `_extract_json` 当场坏）；
差异只落在 `prompt_source.MAP` 那 4 个**有源**角色上，无源 3 个（shensi/xueshu/renhua）
两侧仍逐字相同；附加提示词（`大概率管用 .txt`，对抗性双角色）默认关，关着时它的特征串
不许出现在 system 里。成本口径按实测：A 只加在生成与修订**两轮**（长素材三轮 system 长度
形如 [5008, 219, 5008]），**预读不吃角色提示词**（发的是固定的 `MAP_SYSTEM`），
所以成本不随分段数放大——「长素材每段也带一次角色 system」那个说法不成立。

## 五类断言，各自防不同的失效

* **A 组 副本 vs 原件**（`bili_scribe.core.summarizer` ↔ `video_workbench.core.summary.summarizer`）
  ——逐字节副本该有的行为一致性；漂移测试守字节，这里守语义。
  注意副本虽然字节相同，**它读到的角色对象已经被权威层换过**（`core/summary/__init__.py`
  把 `_roles.get_role` 指向 `prompt_source.view_for` 并在导入时 `bind()`），所以 A 组的
  system 差异与 B 组同源、同一条规则守；漂移测试那边照旧按字节锁两份文件，互不相干。
  **副本从不读这个开关**（`core/summary/` 里没有 `_parity_strict`），所以 A/C–G 组与档位无关；
  A 组带 `default_env` 只是为了让环境变量干净（防止外部 shell 里的残留影响其它用例），
  它不是断言对象——别在这组里加档位主张。
* **B 组 适配层 vs 原件（严格档＝默认档）** ——A3 的正主：适配层加固塌回上游语义以后，
  整条 WB 链路发出的请求必须与 bs **逐维度**一致（方案 A 之后「一字不差」只适用于
  system 正文以外的一切，见上面那张表）。
* **C–G 组 分支覆盖** ——预读两条路、三轮回门、迭代打磨、失败路径、时间线 20 条硬上限。
* **H 组 宽松档（显式 `=0`）的差异清单** ——三处加固的**完整代价**实测化。它们在默认档
  已经不生效，所以必须钉在显式 `=0` 上；写成「永久差异清单」就等于宣布 WB 天生不一致。
* **I 组 默认档翻正后的新契约** ——①在默认档真的留住超上限的条数且零额外请求、
  没超上限时逐字节原样、坏 JSON 必须把开关念出来（宽松档反而不许提）、
  第一轮预算与 bs 同值、档位取值表、同进程来回切换，外加两处形状检查的灵敏度自证。

## ①（时间线补密）在默认档到底兑没兑现——判定标准按 task-11 的口径

拍板是「设成默认，但保留①」。**门拆了不等于①有内容**：`_apply_dense` 现在无条件调用
（`:875` / `:961`），而它读的 `dense_timeline` 当初唯一赋值点在 `_finalize()` 里
（`:462` / `:471`），`_finalize` 又只被 `_json_or_retry()` 调；严格档 `chat()` 早退、
整套阶梯不走 ⇒ 默认档 dense 恒空，①只在「③恰好跑过」的宽松档真的补密度。lead 复核后
判定这是 bug（用户点名要保留的东西在默认档其实没兑现），**task-11 已补上只读捕获**：
严格档每次 `chat()` 返回后 `_loads_object` 数一遍时间线，**只有 `len(rows) > 20` 才记**
（`core/summarizer.py:265-291`），零额外请求、读不出就当没事发生。本次实测确认已生效：
默认档模型给 25 条 → WB 留 25 条，而且仍是 3 次请求。

所以本文件对①的判据是四条，缺一条都守不住这次命令：

* **B1 从生产入口走**：`FakeLLM` 返回一段 `timeline` 真有 25 条的合法 JSON，
  默认档下 WB 的 `Summary.timeline` 必须是 **25**，同一份响应下 bs 原件是 **20**
  ——后者是**已登记的有意差异**（`bili_scribe` 的 `_coerce_timeline` `out[:20]` 不改，
  用户命令是「保留①」），不许当复刻失败，也不许把 WB 削到 20 来骗绿。**手工把 dense
  造成 25 条再喂给 `_apply_dense` 不算证据**：那测的是覆盖函数本身（task-4 就有的路径），
  守不住这条命令。
* **B1 同时钉「零额外请求」**：`calls`、请求次数与 `json_mode` 形状都必须和不补①时相同。
  少了这一句，将来有人为了让①有内容去**多问一轮**，测试照样绿——那是把加固变成加价。
  宽松档那轮「只输出时间线」的补密请求（实测 4 次）是老办法的实物证据，
  另有一条用例把它与默认档对比着钉住。
* **B6 钉两条边界**：18 条（没超上限）时 `dense_timeline` 必须仍为空且 `Summary` 与副本
  逐字节全等（捕获不许改坏不截断的路径）；半截 `{"timeline": [{"t"` 时解析必须静默失败、
  不多问一次，报错仍是 `LLM_BAD_RESPONSE` 且末尾带那句开关提示。
* **渲染性（本次发现，vendor 已在 task-12 修掉，两条用例留作回归锁）**：`_apply_dense`
  曾经是 `summary.timeline = dense` **原样覆盖**，不过副本那道 `_coerce_timeline` 的别名归一
  （`text or content`、`t or time`、裸字符串补成 `{"t":"", "text":…}`），而前端
  `video_workbench/web/js/pages/summary.js:943-944` 只读 `item.t` 与 `item.text`。模型给
  `{"time","content"}` 且超过 20 条时，界面上**条目数没丢、文字全丢**。vendor 当时把这代价
  写成「形状不如清洗过的整齐」，实际后果是整块空白——这条是我在写 B1 时顺手换键名试出来的。
  现在覆盖前走 `_clean_dense()`（`core/summarizer.py:643-668`，规则逐条对齐副本
  `_coerce_timeline`、只去掉 `[:20]`，文本取值复用副本的 `_coerce_text`），**洗完再比条数**
  （`:691-693`）。钉它的是 `test_dense_rows_left_by_the_cap_override_stay_renderable` 与
  `test_blank_dense_rows_are_dropped_instead_of_padded`；将来有人把清洗摘掉，或者为了
  「保住条数」把洗不出内容的条目塞回时间线，这两条会分别红。

历史语料的数字仍然成立，但**别用它推出「①没用」**：`~/.bili-scribe/bili-scribe.db` 的
`summary_version` **86 个真实历史版本**（本次逐行读 `summary_json.timeline` 复核）落库
条数 **p50=8 / p90=12 / max=19**，`==20` 零条、`>20` 零条、空 5 条——20 条硬上限在这份
语料上从没咬到。这只说明**过去**没有版本被截到；①要保的是「模型自己给出超过 20 条时，
那些条目不被丢掉，而且不多花一次请求」。看到 max=19 就断言①是多余的，是对这组数字的
误读——将来有人这么论证要删①，请他先回答 B1 那条 25 条的用例。

## 关掉 ③ 的代价是实测的，不是修辞

`%APPDATA%/VideoWorkbench/logs/wb-20261101.log`（本次 grep 复核）：
「已按更严格的要求重问一次」**7 次**、「输出被截断了，正在接着写」**5 次**、
「正文这一步没给出可用的 JSON」**1 次**、分两次要 **1 次**。
也就是说默认严格档会把这一天里 13 次「模型没吐出台式 JSON 但被救回来」的现场
直接变成失败。这是用户自己拍的板，代价必须由**报错替他说出来**——
所以 I 组钉那句开关提示（前半句 `模型没给出可用的总结 JSON：` 是既有用案钉着的文案，
一个字不许动，提示只准追加在末尾；宽松档里反而**不许**提开关，那时失败是阶梯用尽）。

## 防「两侧一起错」

只断言 `A == B` 的测试，在两侧被同一次改动同时带跑时会照常绿——那是假装测试。所以
每条对账断言旁边都钉一条 **bili_scribe 侧的实测期望值**（调用次数、`json_mode` 形状、
具体字段值），按 lead 的指令：「bs 的真实行为写进用例，别按 WB 现在的行为写」。

## 副本没落地时

统一 `pytest.fail` 并点名在等谁，**不许 skip**——skip 会被读成「这条不用管」。
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from bili_scribe.config import AppConfig
from bili_scribe.core.collector import Bundle
from bili_scribe.core import summarizer as bs_summarizer
from bili_scribe.core import roles as bs_roles
from video_workbench.core import prompt_source as ps

#: 合同值：cfg 固定这三个参数（`bili_scribe/config.py` 的默认值就是它们，这里显式写出来，
#: 免得以后上游改了默认值，对账基准跟着无声漂移）。
MAX_INPUT_CHARS = 24_000
CHUNK_SIZE = 6_000
CHUNK_OVERLAP = 400

#: bs 的 8 个角色里除 standard 之外全部存活；这 7 个是 WB 与 bs 的「共有角色」。
SHARED_ROLE_KEYS = ("xueshu", "renhua", "shensi", "wengao", "xiezuo", "fenxi",
                    "jinglian")

#: 模型该返回的结构化总结。内容本身不重要，**形状**要与 OUTPUT_CONTRACT 对齐。
PAYLOAD = {
    "one_line": "这期讲三件事",
    "summary_md": "## 开头\n讲了三件事。",
    "key_points": ["第一件", "第二件"],
    "timeline": [{"t": "00:01", "text": "开场"}],
    "caution": "",
    "comment_short": "一句话：讲了三件事。",
}

CRITIQUE_NOTE = "诊断清单：1. 要点偏长；2. 时间线只有一条。"

#: 适配层源码（形状检查读它，不读副本）。
ADAPTER_PY = Path(__file__).resolve().parents[2] / "video_workbench" / "core" / "summarizer.py"
#: 档位开关名。fixture 从产品侧读（`wb_parity_tiers.env_name()`），这里留一份字面量
#: 是给断言文案用的——产品改名时 `wb_parity_tiers` 会先红，这条跟着改，不会静默分叉。
PARITY_ENV_NAME = "WB_SUMMARY_PARITY_STRICT"

#: 时间线硬上限（`bili_scribe/core/summarizer.py:183` 的 `return out[:20]`）。
TIMELINE_CAP = 20

WAIT_FOR_VENDOR = (
    "`video_workbench/core/summary/` 还没有落地。这组用例在等 vendor-engineer 的 task-4。"
    "产品侧完成之前它们**应该**是红的——这不是测试写坏了，别放宽断言。"
)


# ------------------------------------------------------------------ 假 LLM（确定性）


class _ModelCfgView:
    def __init__(self, model: str) -> None:
        self.model = model


class FakeLLM:
    """同时充当 bs 的 `LlmClient` 与 WB 的 `ChatClient`：只记录、按脚本回吐。

    两侧共用一个类是**刻意的**。如果给两边各写一个假对象，那两个假对象本身的差异就会
    被读成「产品代码不一致」——排查的人会去读 summarizer，而问题其实在测试里。
    两边的调用形状本来就一样（`chat(messages, temperature=, max_tokens=, json_mode=,
    note=)`），差别只在 WB 的 `SummaryLLM` 会额外传 `**_ignored`，所以签名留一个 `**kw`。
    """

    def __init__(self, script, model: str = "parity-fake") -> None:
        self.script = list(script)
        self.cfg = _ModelCfgView(model)
        self.model = model
        self.base_url = "http://parity.local/v1"
        self.configured = True
        self.requests: list[dict] = []

    def require(self) -> None:
        return None

    def chat(self, messages, temperature=None, max_tokens=None, json_mode=False,
             note=None, **kw):
        self.requests.append({
            "messages": [dict(item) for item in messages],
            "json_mode": bool(json_mode),
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if not self.script:
            raise AssertionError(
                f"假 LLM 的脚本用完了（已发 {len(self.requests)} 次请求）——"
                "说明这一侧的实际调用次数比用例预期多")
        item = self.script.pop(0)
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)

    # --- 只对账用的派生量 -------------------------------------------------
    def shapes(self) -> list[tuple]:
        """每次请求的「阶段指纹」：(json_mode, 第 0 条 message 的内容前 12 字)。"""
        return [(r["json_mode"], str(r["messages"][0]["content"])[:12])
                for r in self.requests]


# --------------------------------------------------------------------- 素材与配置


def make_cfg(**llm_overrides) -> AppConfig:
    cfg = AppConfig()
    cfg.llm.max_input_chars = MAX_INPUT_CHARS
    cfg.llm.chunk_size = CHUNK_SIZE
    cfg.llm.chunk_overlap = CHUNK_OVERLAP
    cfg.llm.max_tokens = 4096
    cfg.llm.temperature = 0.3
    for key, value in llm_overrides.items():
        setattr(cfg.llm, key, value)
    return cfg


def make_bundle(body: str, *, source_kind: str = "subtitle_ai") -> Bundle:
    """固定素材。**必须带时间戳**，否则 bs 会把 timeline 强制清空，20 条上限那条就测不到。"""
    return Bundle(
        bvid="BV1paritycheck", aid=1, cid=1, page=1, page_count=1,
        title="对账素材", desc="固定简介", up_name="UP对账", up_mid=2, duration=600,
        tname="知识", tags=["对账", "固定"],
        source_kind=source_kind, source_label="AI 字幕", source_trust="中",
        source_detail="带时间戳", body=body)


SHORT_BODY = "[00:00] 开场\n[00:05] 讲第一件\n[00:12] 讲第二件"
UNIT = "这是固定的一句话内容，用来把素材推过分段预读的阈值。"
LONG_BODY = UNIT * 1200          # 31,200 字 > max_input_chars，触发分段预读

#: `_split(31200, 6000, 400, MAX_CHUNKS=8)`：step=max(ceil(31199/7), 5600)=5600 → 6 段。
EXPECTED_LONG_CHUNKS = 6


def three_pass_script(payload=None):
    """生成 → 自我批判 → 修订 的固定脚本（三轮回角色用）。"""
    body = dict(PAYLOAD if payload is None else payload)
    return [body, CRITIQUE_NOTE, body]


def single_pass_script(payload=None):
    return [dict(PAYLOAD if payload is None else payload)]


# ------------------------------------------------------------------ 取 WB 侧模块


def _wb_package():
    try:
        return importlib.import_module("video_workbench.core.summary")
    except ImportError as exc:
        pytest.fail(f"{WAIT_FOR_VENDOR}\n（ImportError：{exc}）")


def _wb_summarizer():
    """副本本体模块。

    `diff_fields` / `build_previous_view` 不在权威层的 `__all__` 里（合同只要求导出
    summarize/prepare_material/Summary/PreparedMaterial/Refinement/SummarizeError），
    所以这里直接取副本模块本体——它们也是被漂移测试逐字节锁住的那份代码。
    """
    try:
        return importlib.import_module("video_workbench.core.summary.summarizer")
    except ImportError as exc:
        pytest.fail(f"{WAIT_FOR_VENDOR}\n（ImportError：{exc}）")


def _adapter_module():
    """适配层模块本体（加固与档位开关都在它里面）。"""
    return importlib.import_module("video_workbench.core.summarizer")


def _parity_env_name() -> str:
    """开关名从产品侧取，不在测试里硬编码字符串（产品改名时这条要一起改）。"""
    return str(getattr(_adapter_module(), "PARITY_ENV", "WB_SUMMARY_PARITY_STRICT"))


@pytest.fixture
def strict_env(monkeypatch):
    """**显式**严格档（`=1`）。

    task-8 之后它与「未设」同档，所以它的作用不再是「打开严格」，而是钉住
    *非 OFF 值一律算严格* 这件事（`2`、`TRUE` 这种也算，见 I 组的取值表）。
    """
    monkeypatch.setenv(_parity_env_name(), "1")
    yield


@pytest.fixture
def loose_env(monkeypatch):
    """宽松档：只有 `0` / `false` / `off` 三种写法。

    原来标着 `default_env` 的两条用例（timeline 补密、坏 JSON 阶梯）现在**必须**用这个
    档位——它们在默认档已经不生效了，留在「未设」上就变成永远绿但什么都不守。
    """
    monkeypatch.setenv(_parity_env_name(), "0")
    yield


@pytest.fixture
def default_env(monkeypatch):
    """未设这个变量＝**严格档（用户的常态）**。

    名字保留是给 A 组那批「与档位无关」的用例继续用（它们只需要一个干净的 env），
    但它现在的语义与 task-8 之前**相反**：以前未设＝宽松加固开，现在未设＝严格。

    必须 delenv 而不是「什么都不做」：`tests/wb/conftest.py` 的 `_isolate_env` 清掉了
    WB_LLM_API_KEY 之类，但没有清 `WB_SUMMARY_PARITY_STRICT`。要是某人从设了这个变量的
    shell 里跑测试，本该钉默认档的用例会静默跑到宽松档上、断言照样通过——
    而那正是这几条唯一要防的事。
    """
    monkeypatch.delenv(_parity_env_name(), raising=False)
    yield


class QuietCtx:
    """适配层要的进度对象。"""

    def __init__(self) -> None:
        self.logs: list[str] = []

    def progress(self, value=None, stage=None, speed=None, eta=None):
        return None

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        return False

    def check_cancel(self):
        return None


def _requests_of(llm: FakeLLM) -> list[dict]:
    return llm.requests


# ============================ 方案 A 下的请求序列对账（2026-11-03 口径重定义）

#: 归一化时**必须**逐项比较的维度，写死成清单。
#: 原来只比「两个元组相等」——谁往归一化里少算一个字段是**静默通过**的
#: （vendor 收尾里承认他没逐行读完 `_effective` 到底归一化了哪些字段）。
#: 现在字段名显式列出，`test_the_normalization_covers_every_listed_field` 兜住它。
EFFECTIVE_FIELDS = ("json_mode", "temperature", "max_tokens", "messages")

#: 用户拍板方案 A：**唯一**允许不同的一处是角色 system 的正文。
PLAN_A_NOTE = (
    "方案 A 有意差异（登记，不是缺陷）：WB 的角色 system 正文换成包内 txt 全文，"
    "bs 一个字不改、继续用 roles.py 压缩版。允许不同的**只有** system 正文；"
    "调用次数、阶段顺序（json_mode 指纹）、非 system 消息逐字、temperature、"
    "max_tokens、`Summary.to_dict()` 字段集合、timeline 上限语义、错误码——"
    "一条都不放宽。而且差异只落在 `prompt_source.MAP` 里那 4 个有源角色上，"
    "无源三角色（shensi/xueshu/renhua）两侧必须仍逐字相同。"
)

#: 附加提示词的特征串（对抗性双角色）。默认关时两侧都不该出现。
EXTRA_MARKERS = ("奇美拉", "Guardian")


def _normalized(requests, cfg: AppConfig) -> list[dict]:
    """把两侧记录的请求还原成「模型实际会收到什么」，键固定为 `EFFECTIVE_FIELDS`。

    为什么必须还原而不是直接比原始 kwargs：`summarize()` 调 `llm.chat` 时**根本不写**
    `max_tokens`/`temperature`，是客户端在发请求时代码补上配置值
    （`bili_scribe/infra/llm.py:259`：`"max_tokens": max_tokens or self.cfg.max_tokens`、
    `"temperature": self.cfg.temperature if temperature is None else temperature`）；
    WB 的对账模式 `_parity_max_tokens` 为了做同样的事会把 cfg 值**显式**填进 kwargs。
    所以「原始 kwargs 相等」是错的判据，「还原后的有效值相等」才是 lead 那句
    「请求序列一致」的可执行形式。**期望值一律从传进来的 cfg 现读**，不写死数字。
    """
    return [
        {
            "json_mode": bool(r["json_mode"]),
            "temperature": cfg.llm.temperature if r["temperature"] is None else r["temperature"],
            "max_tokens": cfg.llm.max_tokens if r["max_tokens"] is None else r["max_tokens"],
            "messages": r["messages"],
        }
        for r in requests
    ]


def _txt_for(role_key: str) -> str | None:
    """某个角色的 txt 真源逐字原文；无源角色返回 None。"""
    name = ps.MAP.get(role_key)
    return ps.load(name) if name else None


def _assert_wb_system_by_rule(system: str, role_key: str, bs_role) -> None:
    """WB 侧 system 的**构造规则**（不是「等于某段拼好的字符串」）。

    有源角色四条：以 txt 前 40 字开头 / 以 OUTPUT_CONTRACT 结尾 / 契约在 txt **之后** /
    附加提示词默认关时那两个特征串不得出现。
    无源角色：必须与 bs 逐字相同——A 不碰它们，这条同时是「少绑一个也没发现」的兜底。
    """
    contract = bs_roles.OUTPUT_CONTRACT.strip()
    text = _txt_for(role_key)
    if text is None:
        assert system == bs_role.system.strip() + "\n" + bs_roles.OUTPUT_CONTRACT, (
            f"无源角色 {role_key} 的 system 两侧应当逐字相同——A 只换有源的那四个")
        return
    head = text.strip()[:40]
    assert head, f"{role_key} 的 txt 前 40 字取空了，规则断言会空转"
    assert system.startswith(head), (
        f"{role_key}：WB 的 system 不是以真源 txt 开头。开头 40 字实得 {system[:40]!r}")
    assert system.rstrip().endswith(contract), (
        f"{role_key}：输出契约不在最末尾。这是硬要求——txt 原文里有「打印每个阶段完整输出」"
        "「输出 ReAct 执行日志」这类指令，契约就是来压住它们的（`roles.py:52-56`）")
    assert system.index(head) < system.rindex(contract[:40]), (
        f"{role_key}：契约跑到了 txt 前面，顺序被调了")
    for marker in EXTRA_MARKERS:
        assert marker not in system, (
            f"{role_key}：附加提示词默认关着却已经接进 system（{marker}）")


def assert_sequences_match_plan_a(wb_requests, bs_requests, cfg: AppConfig,
                                 role_key: str, *, label: str = "") -> list[int]:
    """除 system 正文外**逐维度要求一致**，system 两侧各按自己的构造规则断言。

    返回两侧 system 的长度对照（写进差异清单用，不硬编码）。
    """
    tag = f"{label} " if label else ""
    wb = _normalized(wb_requests, cfg)
    bs = _normalized(bs_requests, cfg)
    assert len(wb) == len(bs), (
        f"{tag}调用次数不一致：WB {len(wb)} / bs {len(bs)}。方案 A 换的是提示词正文，"
        "不改轮次——这条红了就是有人在某一边多插了一次调用")
    bs_role = bs_roles.get_role(role_key)
    for index, (w, b) in enumerate(zip(wb, bs), start=1):
        for field in ("json_mode", "temperature", "max_tokens"):
            assert w[field] == b[field], (
                f"{tag}第 {index} 次请求的 {field} 不一致：WB {w[field]!r} / "
                f"bs {b[field]!r}（A 只允许 system 正文不同）")
        assert [m["role"] for m in w["messages"]] == [m["role"] for m in b["messages"]], (
            f"{tag}第 {index} 次请求的消息角色序列不一致")
        for offset, (wm, bm) in enumerate(
                zip(w["messages"][1:], b["messages"][1:]), start=1):
            assert wm["content"] == bm["content"], (
                f"{tag}第 {index} 次请求第 {offset} 条**非 system** 消息不一致——"
                "用户素材、草稿、批判清单、本次要求都必须一字不差")
        if b["json_mode"]:
            # 生成与修订这两轮的 system 才是「角色本体 + 契约」。
            assert b["messages"][0]["content"] == (
                bs_role.system.strip() + "\n" + bs_roles.OUTPUT_CONTRACT), (
                f"{tag}bs 侧第 {index} 次生成请求的 system 不是「压缩版 + 契约」："
                "bs 一个字没改，它要是变了说明有人动了原件")
            _assert_wb_system_by_rule(w["messages"][0]["content"], role_key, bs_role)
        else:
            # 自我批判那一轮用的是角色自己的批判提示词，**A 不碰它**：两侧必须逐字相同。
            assert w["messages"][0]["content"] == b["messages"][0]["content"] == bs_role.critique, (
                f"{tag}第 {index} 次（自我批判）的 system 两侧不等同 critique 原文——"
                "方案 A 只替换角色本体，批判提示词换了就是越界")
    return [len(b["messages"][0]["content"]) for b in bs], [
        len(w["messages"][0]["content"]) for w in wb]


# ============================================================ A 组 副本 vs 原件


@pytest.mark.parametrize("role_key", SHARED_ROLE_KEYS)
def test_copy_and_original_agree_on_every_shared_role(role_key, default_env):
    """7 个共有角色逐个：`Summary.to_dict()` 相等，请求序列按方案 A 的口径相等。

    产出（`to_dict()`）必须仍然**逐字相等**：A 换的是提示词正文，不是输出形状。
    请求序列从「元组逐字相等」换成 `assert_sequences_match_plan_a`——理由写在下面。
    """
    wb = _wb_summarizer()
    bs_llm = FakeLLM(three_pass_script())
    wb_llm = FakeLLM(three_pass_script())
    cfg = make_cfg()

    bs_out = bs_summarizer.summarize(make_bundle(SHORT_BODY), bs_llm, cfg,
                                     role_key=role_key)
    wb_out = wb.summarize(make_bundle(SHORT_BODY), wb_llm, cfg, role_key=role_key)

    assert wb_out.to_dict() == bs_out.to_dict()
    # 口径变更（方案 A，2026-11-03 拍板）：这里原来是一条
    # `assert _requests_of(wb_llm) == _requests_of(bs_llm)`，逐字比 messages。
    # A 之后有源角色的 system 正文**必然**不同（WB 吃包内 txt 全文，bs 继续吃 roles.py
    # 压缩版），继续用元组相等就会把「唯一的有意差异」和「真的漂了」混成同一种红，
    # 而放宽成「不等就通过」又会盖掉阶段顺序与用户素材的漂移。
    # 所以换成逐维度比较：除 system 正文外全部要求一致，system 两侧各按构造规则断言。
    # 无源三角色（shensi/xueshu/renhua）在 helper 里仍走「两侧逐字相同」那条路。
    assert_sequences_match_plan_a(wb_llm.requests, bs_llm.requests, cfg, role_key,
                                  label="副本 vs 原件")

    # 钉住 bs 侧的真实期望：两侧一起跑偏时这一段要红。
    assert bs_out.role == role_key
    assert bs_out.one_line == PAYLOAD["one_line"]
    assert bs_out.summary_md == PAYLOAD["summary_md"]
    assert bs_out.key_points == PAYLOAD["key_points"]
    assert bs_out.model == "parity-fake"
    assert bs_out.chunks == 1
    assert len(bs_llm.requests) == 3, "7 个角色都是 passes=3，必须三次调用"


def test_copy_exports_the_contracted_surface():
    """权威层必须真的导出合同点名的东西（缺一个就是接口没对齐）。"""
    wb = _wb_package()
    for name in ("DEFAULT_ROLE", "RETIRED_ROLES", "ALL_ROLES", "ROLE_KEYS",
                 "describe_roles", "UnknownRole", "get_role", "role_label",
                 "summarize", "prepare_material", "Summary", "PreparedMaterial",
                 "Refinement", "SummarizeError"):
        assert hasattr(wb, name), f"video_workbench.core.summary 缺 {name}"
    # 这些名字在副本模块上也得在——漂移测试按字节锁的就是这份文件。
    module = _wb_summarizer()
    for name in ("diff_fields", "build_previous_view", "_coerce_timeline"):
        assert hasattr(module, name), f"副本 summarizer 缺 {name}"


# ================================================= B 组 适配层 vs 原件（严格档＝默认档）


# ================================================= B 组 适配层 vs 原件（严格档＝默认档）
#
# 这里原来是 `_effective(requests, cfg)`：把每个请求归一成四元素**元组**。
# task-18 把它删了，换成上面显式列字的 `_normalized()`（键 = `EFFECTIVE_FIELDS`）
# 和逐维度比较的 `assert_sequences_match_plan_a`。两个 owner 比一个 owner 危险：
# 元组少写一项是静默通过，字典少一个键会直接被
# `test_the_normalization_covers_every_listed_field` 抓住。


def test_strict_mode_adapter_requests_are_identical_to_bs(default_env):
    """A3 的正主：**默认档（未设＝严格）**下，适配层发出的请求与 bs 逐维度一致。

    「一字不差」在方案 A 之后只对**除 system 正文以外**的每一维成立：WB 的角色提示词
    来自包内 txt 全文，bs 继续用 roles.py 压缩版，这是用户拍板的差异（见
    `assert_sequences_match_plan_a` 与 `PLAN_A_NOTE`）。次数、阶段指纹、非 system 消息、
    temperature、max_tokens 仍然一个字都不能差。

    档位归位：这条原来钉在显式 `=1` 上，那是「默认＝宽松」时代的写法——那时不显式设
    就等于没打开对账。现在拍板反过来了，日常档就是严格，所以钉在**未设**上才是用户
    实际拿到的东西；显式 `=1` 与未设等价这件事由 I 组的取值表单独守。

    这是 lead 判据第 ① 条的直接实现。适配层自己建 Bundle、自己包 LLM，中间还有三层
    加固——默认档把它们关掉之后，链路必须塌回与 bs 完全相同的请求序列。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    project = {"title": "对账素材", "up_name": "UP对账", "bvid": "BV1paritycheck",
               "duration_sec": 600, "desc": "固定简介"}
    segments = [{"start_ms": 0, "text": "开场"}, {"start_ms": 5000, "text": "讲第一件"}]

    # 适配层内部会自己 build_bundle；先证明它是确定的，再拿同一份喂 bs。
    bundle = adapter.build_bundle(project, segments)
    assert adapter.build_bundle(project, segments) == bundle, "build_bundle 不确定"

    bs_llm = FakeLLM(three_pass_script())
    bs_out = bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="wengao")

    client = FakeLLM(three_pass_script())
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="wengao")

    assert result["summary"] == bs_out.to_dict()
    # 口径变更（方案 A）：原来是 `_effective(client.requests, cfg) == _effective(bs_llm.requests, cfg)`，
    # 归一化后的元组里含 `messages`，所以有源角色的 system 正文必然不等 → 红。
    # 换成逐维度比较：次数/阶段指纹/temperature/max_tokens/非 system 消息仍要求一致，
    # system 两侧各按构造规则断言（bs = 压缩版+契约，WB = txt(+extra)+契约且契约在最末）。
    # 期望值全部从传进来的 `cfg` 现读，不写死数字。
    assert_sequences_match_plan_a(client.requests, bs_llm.requests, cfg, "wengao",
                                  label="适配层 vs bs")


def test_strict_mode_adapter_does_not_retry_malformed_json(default_env):
    """**默认档**下模型给坏 JSON：两侧都必须**一次就失败**，不许自愈。

    档位归位：原来钉在显式 `=1`，现在「未设」才是用户的常态，所以钉在未设——
    万一有人把默认翻回宽松，显式 `=1` 那批用例会照常绿，而用户已经失去这条行为。
    `=1` 与未设同档这件事由 I 组的档位取值表单独守。

    lead 明确点过这一条：WB 不能在这里靠 jsonfix 兜住，否则「一致」就变成「WB 更宽容」。
    适配层把 `SummarizeError` 翻译成 `WBError(LLM_BAD_RESPONSE)`，那是它既有的错误出口，
    失败语义与 bs 抛 SummarizeError 对应；**关键是只发了 1 次请求**——修复/续写/拆分/
    重问那条阶梯完全没跑（阶梯本身在宽松档由 H 组守）。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    project = {"title": "对账素材", "up_name": "UP对账", "bvid": "BV1paritycheck",
               "duration_sec": 60}
    segments = [{"start_ms": 0, "text": "内容"}]

    bs_llm = FakeLLM(["这不是 JSON"])
    with pytest.raises(bs_summarizer.SummarizeError):
        bs_summarizer.summarize(make_bundle(SHORT_BODY), bs_llm, cfg, role_key="wengao")

    client = FakeLLM(["这不是 JSON"])
    with pytest.raises(WBError) as exc:
        adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                    role="wengao")
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert len(client.requests) == 1, f"严格模式还发了 {len(client.requests)} 次，加固没关干净"
    assert len(bs_llm.requests) == 1


# ============================================================ C 组 预读两条路


def test_prepare_material_short_path_skips_pre_reading_on_both_sides():
    """短素材：不触发预读，两侧 body/chunks 相等且**一次请求都不发**。"""
    wb = _wb_summarizer()
    cfg = make_cfg()

    bs_llm = FakeLLM([])
    wb_llm = FakeLLM([])
    bs_out = bs_summarizer.prepare_material(make_bundle(SHORT_BODY), bs_llm, cfg)
    wb_out = wb.prepare_material(make_bundle(SHORT_BODY), wb_llm, cfg)

    assert bs_out.body == wb_out.body == SHORT_BODY
    assert bs_out.chunks == wb_out.chunks == 1
    assert (bs_llm.requests, wb_llm.requests) == ([], [])

    # 前提自证：这条用例真的在测「短素材」那条分支。
    assert len(SHORT_BODY) <= cfg.llm.max_input_chars


def test_prepare_material_long_path_chunks_identically_on_both_sides():
    """长素材：两侧的分段数、合并后的笔记正文、以及每一次分段请求都相等。"""
    wb = _wb_summarizer()
    cfg = make_cfg()
    script = [f"第 {i} 段笔记" for i in range(1, EXPECTED_LONG_CHUNKS + 1)]

    bs_llm = FakeLLM(list(script))
    wb_llm = FakeLLM(list(script))
    bundle = make_bundle(LONG_BODY)
    bs_out = bs_summarizer.prepare_material(bundle, bs_llm, cfg)
    wb_out = wb.prepare_material(bundle, wb_llm, cfg)

    assert bs_out.chunks == wb_out.chunks == EXPECTED_LONG_CHUNKS
    assert bs_out.body == wb_out.body
    assert _requests_of(wb_llm) == _requests_of(bs_llm)

    # 前提自证： fixture 必须落在「超限」那一侧，否则这条会静默退化成短素材测试。
    assert len(LONG_BODY) > cfg.llm.max_input_chars
    assert len(bs_llm.requests) == EXPECTED_LONG_CHUNKS, "分段预读次数应等于段数"
    assert all(not r["json_mode"] for r in bs_llm.requests), "预读是纯文本笔记，不该开 JSON 模式"


# ============================================================ D 组 三轮回门


@pytest.mark.parametrize("role_key", SHARED_ROLE_KEYS)
def test_three_pass_gate_runs_generate_critique_revise_in_order(role_key):
    """`passes >= 3` 的角色必须走「生成 → 自我批判 → 修订」三次调用，两侧次数与顺序相等。

    阶段顺序的可执行指纹 = 三次请求的 `json_mode` 序列 `(True, False, True)`
    （生成要结构化、批判是散文清单、修订又要结构化）。这个形状来自
    `bili_scribe/core/summarizer.py:585-620`，比「数一数是不是三次」强：三次调用的顺序
    被写反时，次数照样是 3，但指纹会变。
    """
    from bili_scribe.core import roles as bs_roles

    wb = _wb_summarizer()
    cfg = make_cfg()
    bs_llm = FakeLLM(three_pass_script())
    wb_llm = FakeLLM(three_pass_script())

    bs_summarizer.summarize(make_bundle(SHORT_BODY), bs_llm, cfg, role_key=role_key)
    wb.summarize(make_bundle(SHORT_BODY), wb_llm, cfg, role_key=role_key)

    assert [r["json_mode"] for r in bs_llm.requests] == [True, False, True]
    assert [r["json_mode"] for r in wb_llm.requests] == [True, False, True]
    assert len(wb_llm.requests) == len(bs_llm.requests) == 3
    # 口径变更（方案 A）：原来这里逐字比 messages，有源角色的 system 正文必然不等。
    # 换成逐维度比较——阶段顺序、json_mode、非 system 消息、temperature/max_tokens 全部
    # 继续要求一致；生成两轮的 system 按两侧各自的构造规则断言，批判那轮两侧都必须是
    # 角色自己的 critique 原文（A 不碰它）。
    assert_sequences_match_plan_a(wb_llm.requests, bs_llm.requests, cfg, role_key,
                                  label="三轮回")

    # 三个阶段的 system 提示词各自是谁——顺序写反了这里必红。
    role = bs_roles.get_role(role_key)
    expected_generate = role.system.strip() + "\n" + bs_roles.OUTPUT_CONTRACT
    assert bs_llm.requests[0]["messages"][0]["content"] == expected_generate
    assert bs_llm.requests[1]["messages"][0]["content"] == role.critique
    assert bs_llm.requests[2]["messages"][0]["content"] == expected_generate


# ============================================================ E 组 迭代打磨


def test_refinement_path_matches_on_both_sides():
    """带 `Refinement(requirement, previous)` 时：产出与请求序列都相等。"""
    wb = _wb_summarizer()
    cfg = make_cfg()
    previous = json.dumps({**PAYLOAD, "revise_note": "上一版的说明"}, ensure_ascii=False)
    requirement = "只改开头那句，其余一个字都别动"
    revised = {**PAYLOAD, "revise_note": "把要点压到两条"}

    bs_llm = FakeLLM([PAYLOAD, CRITIQUE_NOTE, revised])
    wb_llm = FakeLLM([PAYLOAD, CRITIQUE_NOTE, revised])
    bs_out = bs_summarizer.summarize(
        make_bundle(SHORT_BODY), bs_llm, cfg, role_key="wengao",
        refinement=bs_summarizer.Refinement(requirement=requirement, previous=previous))
    wb_out = wb.summarize(
        make_bundle(SHORT_BODY), wb_llm, cfg, role_key="wengao",
        refinement=wb.Refinement(requirement=requirement, previous=previous))

    assert wb_out.to_dict() == bs_out.to_dict()
    # 口径变更（方案 A）：打磨路径同样只有 system 正文允许不同；本次要求（requirement）
    # 出现在三轮的每一轮里，那条由下面的逐字断言守着，不是靠放宽这条换来的。
    assert_sequences_match_plan_a(wb_llm.requests, bs_llm.requests, cfg, "wengao",
                                  label="打磨路径")
    # 三个阶段的请求里都必须有本次要求——批判与修订两轮也要知道「这次只改什么」，
    # 否则角色自己的清单会把使用者已经认可的部分一起重写（见 REFINE_CONSTRAINT 的注释）。
    assert requirement in bs_llm.requests[0]["messages"][1]["content"]
    assert requirement in bs_llm.requests[1]["messages"][1]["content"]
    assert requirement in bs_llm.requests[2]["messages"][1]["content"]
    # 修订路径才填 revise_note，填的是模型在最终那一版里写的说明。
    assert bs_out.revise_note == "把要点压到两条"


def test_revise_note_is_forced_empty_on_plain_generation():
    """`revise_note` 只在打磨路径填：初次生成时模型就算写了也要清空。

    理由是 bs 自己的注释：「初次生成时这个字段没有意义——那时没有上一版可比，
    模型填什么都是废话」。这条留在对账文件里，是因为它是一次**两侧都要成立**的语义，
    光靠字节相等测不出「哪天有人把条件写反」。
    """
    wb = _wb_summarizer()
    cfg = make_cfg()
    with_note = {**PAYLOAD, "revise_note": "没有上一版却硬写了说明"}

    bs_out = bs_summarizer.summarize(make_bundle(SHORT_BODY),
                                     FakeLLM([with_note, CRITIQUE_NOTE, with_note]),
                                     cfg, role_key="wengao")
    wb_out = wb.summarize(make_bundle(SHORT_BODY),
                          FakeLLM([with_note, CRITIQUE_NOTE, with_note]),
                          cfg, role_key="wengao")

    assert bs_out.revise_note == ""
    assert wb_out.to_dict() == bs_out.to_dict()


def test_diff_fields_and_previous_view_match_and_are_pinned():
    """`diff_fields` / `build_previous_view` 的产物两侧相等，并且等于钉死的期望值。

    只比「两侧相等」不够：这两个函数是**给使用者看的**（改动清单 + 喂回模型的上一版），
    两侧一起改坏了照样「相等」。所以同时钉期望：
      · `build_previous_view` 必须剥掉 `revise_note`（不剥就会每版照抄上一版的说明——
        这一点是活体验收时发现的，见函数 docstring）；
      · `diff_fields` 必须只报内容字段、把 `chunks`/`model`/`role` 这类元信息排除在外。
    """
    wb = _wb_summarizer()
    previous = {**PAYLOAD, "revise_note": "上一版说过什么", "chunks": 1, "model": "a",
                "role": "wengao", "frames": 0}
    current = {
        **PAYLOAD,
        "one_line": "完全不同的新结论",
        "key_points": ["第一件", "改过的第二件", "第三件"],
        "revise_note": "这一版的说明",
        "chunks": 2, "model": "b", "role": "wengao", "frames": 3,
    }

    view_bs = bs_summarizer.build_previous_view(previous)
    view_wb = wb.build_previous_view(previous)
    assert view_wb == view_bs
    assert "revise_note" not in view_bs, "上一版视图必须剥掉 revise_note"
    assert "上一版说过什么" not in view_bs
    assert PAYLOAD["one_line"] in view_bs, "其余字段要原样带给模型"

    diff_bs = bs_summarizer.diff_fields(previous, current)
    diff_wb = wb.diff_fields(previous, current)
    assert diff_wb == diff_bs
    changed = [item["field"] for item in diff_bs]
    assert changed == ["one_line", "key_points"], f"只该报这两个内容字段变了，实际 {changed}"
    for meta in ("chunks", "model", "frames", "revise_note"):
        assert meta not in changed, f"{meta} 是元信息，混进改动清单等于没有信号"


# ============================================================ F 组 失败路径

#: 会让 `_extract_json` / 空正文检查失败的回复形状。`{}` 是唯一「能解析但内容为空」的，
#: 它的失败发生在 summarize 末尾而不是解析层——两种失败都要覆盖，否则只测了解析层。
FAILING_REPLIES = (
    pytest.param("这不是 JSON", id="not-json"),
    pytest.param("", id="empty-reply"),
    pytest.param("42", id="bare-number"),
    pytest.param("[1, 2, 3]", id="top-level-array"),
    pytest.param("{}", id="empty-object"),
)


@pytest.mark.parametrize("reply", FAILING_REPLIES)
def test_failure_paths_raise_the_same_summarize_error_on_both_sides(reply):
    """非 JSON / 顶层非对象 / 空正文：两侧都抛 `SummarizeError`，**消息文本也相等**。

    消息相等不是吹毛求疵：这句话最后会被适配层拼进 `WBError` 的文案给用户看，两侧给出
    不同的解释就意味着行为已经分叉。

    这里刻意用 `standard`（`passes=1`）而不是三轮回角色：本条要探的是 `_extract_json`
    在**第一次**拿到坏回复时的行为，套上三轮回只会让脚本长度变成噪声，失败时也分不清
    是解析层坏了还是阶段编排坏了。

    实测钉住的分布（按 lead 指令，写 bs 的真实行为而不是「应该」的行为）：
      · `这不是 JSON` / `` / `42` / `[1, 2, 3]` → 「模型没有返回可解析的 JSON」；
      · `{}` → 能解析，但正文为空 → 「模型返回的总结为空，请重试或换个模型」。
    """
    wb = _wb_summarizer()
    cfg = make_cfg()

    with pytest.raises(bs_summarizer.SummarizeError) as bs_exc:
        bs_summarizer.summarize(make_bundle(SHORT_BODY), FakeLLM([reply]), cfg,
                                role_key="standard")
    with pytest.raises(wb.SummarizeError) as wb_exc:
        wb.summarize(make_bundle(SHORT_BODY), FakeLLM([reply]), cfg, role_key="standard")

    assert str(wb_exc.value) == str(bs_exc.value)

    expected = ("模型返回的总结为空，请重试或换个模型" if reply == "{}"
                else "模型没有返回可解析的 JSON")
    assert str(bs_exc.value) == expected, (
        f"bili_scribe 对这个输入的实际说法变了：{bs_exc.value}")


def test_copy_accepts_standard_while_the_package_refuses_it():
    """退役发生在**权威层**，不是发生在副本里——这个分工要钉住。

    副本 `summary/roles.py` 与原件逐字节相同，所以它的 `get_role("standard")` 照样取得到；
    WB 拒绝 standard 是 `__init__` 那层做的。这条值得单独写：如果有人嫌 standard 碍事，
    最直接的做法是去删副本的 roles.py——字节锁会红，这是对的；但更隐蔽的做法是「副本不动、
    summarize 里偷偷加一句拒绝」，那会让副本从此不再是原件的拷贝而**没有任何测试变红**。
    把「副本仍然认得 standard」钉成断言，这条路就没有藏身之处。
    """
    from bili_scribe.core import roles as bs_roles

    wb_package = _wb_package()
    module = _wb_summarizer()

    assert bs_roles.get_role("standard") is bs_roles.STANDARD
    assert module.roles.get_role("standard") is module.roles.STANDARD
    with pytest.raises(wb_package.UnknownRole):
        wb_package.get_role("standard")

    # standard 是 passes=1：一次调用就出结果，两侧都一样。
    bs_llm = FakeLLM([PAYLOAD])
    out_bs = module.summarize(make_bundle(SHORT_BODY), bs_llm, make_cfg(),
                              role_key="standard")
    assert len(bs_llm.requests) == 1
    assert out_bs.role == "standard"
    assert out_bs.summary_md == PAYLOAD["summary_md"]


def test_top_level_array_around_an_object_is_unwrapped_by_both_sides():
    """`[{...}]` 被两侧一致地「剥壳接受」——这是 bs 的既有行为，不是 WB 的宽容。

    `_extract_json` 用 `find("{") .. rfind("}")` 取片段，所以顶层数组里的**唯一一个对象**
    会被当成合法结果接受。直觉上「顶层非对象应该报错」，实测不是。按 lead 的指令：把 bs
    的真实行为写进用例。这个行为好不好是另一件事（已单独报给 lead），但它必须被**锁住**，
    不能让副本在无意间把它改掉——那会让「同一份素材两侧结果不同」重新变成可能。
    """
    wb = _wb_summarizer()
    cfg = make_cfg()
    wrapped = json.dumps([PAYLOAD], ensure_ascii=False)

    bs_out = bs_summarizer.summarize(make_bundle(SHORT_BODY), FakeLLM([wrapped]), cfg,
                                     role_key="standard")
    wb_out = wb.summarize(make_bundle(SHORT_BODY), FakeLLM([wrapped]), cfg,
                          role_key="standard")

    assert bs_out.one_line == PAYLOAD["one_line"], "bs 实测会接受剥壳后的对象"
    assert wb_out.to_dict() == bs_out.to_dict()


def test_the_top_level_not_object_branch_is_unreachable_on_both_sides():
    """`summarizer.py:132`「顶层不是对象」这条分支从任何输入都到不了——两侧都是死码。

    论证：`start = find("{")`、`end = rfind("}")`，切出来的片段一定以 `{` 开头、以 `}`
    结尾；能解析成合法 JSON 且以 `{` 开头的值必然是对象，于是 `isinstance(payload, dict)`
    恒真。要触发这条分支只能靠「带花括号的非对象」，而那种串本身解析就失败。

    这条用例把「它现在到不了」钉住：哪天有人改了 `_extract_json`（比如改成整体
    `json.loads`）让这条分支活过来，这里会红，逼他重新确认两侧行为——而不是让副本
    悄悄和原件分叉。
    """
    module = _wb_summarizer()
    probe = "模型返回的 JSON 顶层不是对象"
    cfg = make_cfg()

    for reply in ("[{\"a\": 1}, {\"b\": 2}]", "{\"a\":1} {\"b\":2}", "true", "null",
                  "[1, 2]", "一段没有花括号的散文"):
        for target, error_type in ((bs_summarizer, bs_summarizer.SummarizeError),
                                   (module, module.SummarizeError)):
            with pytest.raises(error_type) as exc:
                target.summarize(make_bundle(SHORT_BODY), FakeLLM([reply]), cfg,
                                 role_key="standard")
            assert probe not in str(exc.value), (
                f"死分支被激活了（输入 {reply!r} → {exc.value}）：_extract_json 被改过，"
                "需要重新确认两侧是否仍然一致")


def test_extract_json_boundary_is_pinned_on_the_bs_side():
    """把 `_extract_json` 的真实接受/拒绝边界直接钉在 bs 上（对账基准本身不许悄悄变）。

    上面几条都以 bili_scribe 为基准。基准要是自己动了，「两侧一致」就跟着一起动、测试
    还是绿的——所以这里不比较两侧，只断言 bs 的原话。三个实测结论：

    1. 散文、`true`/`null`/数字、纯数组 → 「模型没有返回可解析的 JSON」；
    2. **数组里包着一个对象会被剥壳接受**（`find("{")..rfind("}")` 的必然结果）；
    3. 两个并排的对象 → 「解析失败：Extra data」，而不是「顶层不是对象」。
    """
    assert bs_summarizer._extract_json('{"one_line": "x"}') == {"one_line": "x"}
    assert bs_summarizer._extract_json('[{"one_line": "x"}]') == {"one_line": "x"}
    assert bs_summarizer._extract_json(
        "  ```\n{\"one_line\": \"x\"}\n```  ") == {"one_line": "x"}

    for reply in ("散文", "true", "null", "42", "[1, 2]"):
        with pytest.raises(bs_summarizer.SummarizeError) as exc:
            bs_summarizer._extract_json(reply)
        assert str(exc.value) == "模型没有返回可解析的 JSON", f"{reply!r} 的实际说法变了"

    with pytest.raises(bs_summarizer.SummarizeError) as exc:
        bs_summarizer._extract_json("{\"a\":1} {\"b\":2}")
    assert "Extra data" in str(exc.value), f"并排对象的实际说法变了：{exc.value}"



# ============================================================ G 组 timeline 上限


def test_timeline_is_capped_at_twenty_on_both_sides():
    """模型给 25 条 timeline 时，两侧 `Summary.timeline` 都必须是 20 条。

    这是 bs 的硬上限（`_coerce_timeline` 结尾 `return out[:20]`）。对账模式 = 走 bs 的
    方法论本体，所以 WB 在这条路径上**不得多于 20**；默认模式的放宽是适配层的既有加固，
    单独在 H 组按差异清单登记。
    """
    wb = _wb_summarizer()
    cfg = make_cfg()
    rows = [{"t": f"00:{i:02d}", "text": f"第 {i} 条"} for i in range(1, 26)]
    payload = {**PAYLOAD, "timeline": rows}

    bs_llm = FakeLLM(three_pass_script(payload))
    wb_llm = FakeLLM(three_pass_script(payload))
    bs_out = bs_summarizer.summarize(make_bundle(SHORT_BODY), bs_llm, cfg,
                                     role_key="jinglian")
    wb_out = wb.summarize(make_bundle(SHORT_BODY), wb_llm, cfg, role_key="jinglian")

    assert len(bs_out.timeline) == TIMELINE_CAP
    assert len(wb_out.timeline) == TIMELINE_CAP
    assert wb_out.timeline == bs_out.timeline
    assert wb_out.timeline[:2] == [{"t": "00:01", "text": "第 1 条"},
                                   {"t": "00:02", "text": "第 2 条"}], "截的是尾部，不是头部"


def test_timeline_is_emptied_when_the_material_has_no_timestamps():
    """素材没有时间戳时两侧都必须把 timeline 清空——这是 bs 的反幻觉硬约束。

    `has_timestamps` 只看 `bundle.source_kind`，与模型给了什么无关。放在这里是因为它
    和上一条构成一对：20 条上限只在**允许写时间线**时才有意义；否则模型编出来的时间戳
    会被当成合规产出，而这东西是要发到别人评论区的。
    """
    wb = _wb_summarizer()
    cfg = make_cfg()
    payload = {**PAYLOAD, "timeline": [{"t": "00:01", "text": "编出来的"}]}

    bs_out = bs_summarizer.summarize(make_bundle(SHORT_BODY, source_kind="meta"),
                                     FakeLLM([payload]), cfg, role_key="standard")
    wb_out = wb.summarize(make_bundle(SHORT_BODY, source_kind="meta"),
                          FakeLLM([payload]), cfg, role_key="standard")

    assert bs_out.timeline == [] == wb_out.timeline
    assert bs_out.to_dict() == wb_out.to_dict()


# =========================================== H 组 宽松档（显式 =0）的差异清单


#: 允许差异清单（lead 2026-11-03 指令：写「只允许这三处」的逐条实测值，不要笼统 assert 不等）
#: **宽松档**（显式 `WB_SUMMARY_PARITY_STRICT=0`）允许与 bs 不同的三处。
#: task-9 之前这张表挂在「默认模式」名下，那是错的：默认档现在是严格档，
#: 这三处只有在有人显式设 0 时才成立。下面两条 H 组用例就是这张表的实测证据，
#: 每条都钉在 `loose_env` 上。默认档的一致性由 B 组与 I 组负责。
ALLOWED_DIFFERENCES = (
    "1) timeline 长度：bs 硬截 20 条，宽松档下 WB 的 `_apply_dense` 会把模型给的更长时间线"
    "覆盖回去（默认档由 task-11 的只读捕获承担同一职责）；"
    "2) max_tokens：bs 不写这个参数（由客户端按 cfg.llm.max_tokens 走），"
    "WB 的 `_json_budget` 在修复/续写/拆分各步抬到 max(cfg*2, 12288) 封顶 16384；"
    "3) 额外请求：WB 的 jsonfix→续写→拆分→重问阶梯会多发请求，bs 一次失败就抛。",
)


def test_loose_mode_timeline_difference_is_the_only_content_delta(loose_env):
    """**宽松档**（显式 `=0`）下模型表现良好：与 bs 的差别**只有** timeline 长度。

    档位归位（原名 `test_default_mode_timeline_difference_is_the_only_content_delta`，
    task-9 连名字一起改）：这条断言的现象在默认档**已经不成立**——默认档现在停在 20 条。
    它守的是「有人显式设 `=0` 时，①必须把 25 条都留下、且除 timeline 外不许有别的差异」。

    实测：模型给 25 条 → bs 20 条、WB 25 条；其余字段逐字相同、调用次数相同、
    max_tokens 相同。这就是「放宽 20 条上限」这项加固的完整代价，没有别的。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    rows = [{"t": f"00:{i:02d}", "text": f"第 {i} 条"} for i in range(1, 26)]
    script = three_pass_script({**PAYLOAD, "timeline": rows})
    project = {"title": "对账素材", "up_name": "UP对账", "bvid": "BV1paritycheck",
               "duration_sec": 600, "desc": "固定简介"}
    segments = [{"start_ms": 0, "text": "开场"}, {"start_ms": 5000, "text": "讲第一件"}]

    bundle = adapter.build_bundle(project, segments)
    bs_llm = FakeLLM(list(script))
    bs_out = bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="wengao")

    client = FakeLLM(list(script))
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="wengao")
    wb_summary = result["summary"]

    # 差异只有一处：timeline 条数。
    assert len(bs_out.timeline) == TIMELINE_CAP
    assert len(wb_summary["timeline"]) == 25, "WB 宽松档应该把 25 条都留下"

    # 其余字段必须逐字相同（含 chunk 数、模型名、角色名）。
    bs_dict = bs_out.to_dict()
    assert set(wb_summary) == set(bs_dict), "字段集合变了：加固改的不该是 schema"
    for field, value in bs_dict.items():
        if field == "timeline":
            continue
        assert wb_summary[field] == value, f"默认模式下 {field} 出现了未登记的差异"

    # 调用次数与预算：模型听话时，这处加固不花额外钱。
    assert len(client.requests) == len(bs_llm.requests) == 3
    assert [r["json_mode"] for r in client.requests] == [True, False, True]


def test_loose_mode_repair_ladder_costs_extra_requests_and_budget(loose_env):
    """**宽松档**（显式 `=0`）：模型输出被截断时登记第 2、3 条差异——多一次续写 + 预算 12288。

    档位归位（原名 `test_default_mode_repair_ladder_costs_extra_requests_and_budget`，
    task-9 连名字一起改）：③在默认档整条不走（一次就抛，见 B 组与 I 组），阶梯只能在
    显式 `=0` 上测。留成「未设」就是 C 条点名的写法——永远绿，但什么都没守。
    预算公式的另外两头（翻倍、封顶 16384）与拆分/重问都在宽松档，见
    `test_summary_truncate.py` / `test_summary_split.py` / `test_jsonfix.py` 里钉了
    `loose_env` 的那几条。

    实测：同一份「JSON 断在半路」的回复——
      · bs：1 次请求，然后 `SummarizeError`（它没有修复阶梯）；
      · WB：2 次请求（第 2 次是「接着写」），拿到完整 JSON 并正常产出。
    第 2 次请求的 `max_tokens` 实测是 `MIN_JSON_TOKENS=12288`，而 bs 那一次的有效上限
    是配置的 4096。这两处差异是**宽松档的产品加固**（长素材一次装不下是真实现场，见
    `_continue` 的注释），不是 bug；所以写成显式期望值，将来谁再抬预算、
    谁再多问一轮，这里都会红，逼他重新登记。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    head = ('{"one_line":"三件事","summary_md":"## 开头\\n这里讲到一半')
    tail = ('被截住了","key_points":["第一件"],"timeline":[],"caution":"",'
            '"comment_short":"短版"}')
    project = {"title": "对账素材", "up_name": "UP对账", "bvid": "BV1paritycheck",
               "duration_sec": 60, "desc": "固定简介"}
    segments = [{"start_ms": 0, "text": "内容"}]
    bundle = adapter.build_bundle(project, segments)

    # bs：一次请求就抛——它没有修复阶梯。
    bs_llm = FakeLLM([head])
    with pytest.raises(bs_summarizer.SummarizeError):
        bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="jinglian")
    assert len(bs_llm.requests) == 1, "bs 的失败形状变了，需要重新登记差异清单"

    # WB：3 次方法论请求 + 1 次续写 = 4。脚本顺序 = 断掉的头 / 接上的尾 / 批判 / 修订。
    client = FakeLLM([head, tail, CRITIQUE_NOTE,
                      {**PAYLOAD, "summary_md": "## 开头\n这里讲到一半被截住了"}])
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="jinglian")

    # 差异 3（额外请求）：多出来的那一次就是「接着写」。
    assert len(client.requests) == 4, (
        f"修复阶梯的请求数变了（实测 {len(client.requests)}），要重新登记差异清单")
    assert len(client.requests) == len(bs_llm.requests) + 3

    # 差异 2（输出预算被抬）：续写那次按 MIN_JSON_TOKENS 要预算，第一次仍按调用方原样。
    assert client.requests[0]["max_tokens"] is None
    assert client.requests[1]["max_tokens"] == adapter.MIN_JSON_TOKENS == 12288
    assert bs_llm.requests[0]["max_tokens"] is None, "bs 一直不写这个参数"

    # 除清单里那两处之外，产出本身仍要与 bs 的契约同构：字段集合一致、正文来自修好的 JSON。
    assert set(result["summary"]) == set(bs_summarizer.Summary().to_dict())
    assert result["summary"]["summary_md"] == "## 开头\n这里讲到一半被截住了"
    assert any("截断" in note for note in result["notes"]), (
        f"加固必须留下可查的说明，实际 notes={result['notes']}")


# =============== I 组 默认档翻正后的新契约（task-9 的 B1/B3/B4/B5/B6 与 D 的口径）

#: 这里原来有一条 `CONFIG_MAX_TOKENS = 4096`（抄自 `config.yaml:21` 的当时实配），
#: task-18 删掉：把配置值抄进用例当期望值，等于「自己考自己」——夹具把
#: `cfg.llm.max_tokens` 设成 4096，断言再拿 4096 比它，恒真；而工作区配置很可能
#: 马上改成 16384（真模型实测：推理型 auto 在 4096 下 100% 把预算烧在思考上）。
#: 现在 B4 那条**现读 `cfg.llm.max_tokens`**，并用 4096/16384 两个值各跑一遍，
#: 证明这个数是从配置流过去的，不是用例里写死的。
#: 上游 `bili_scribe/infra/llm.py:259` 是 `max_tokens or self.cfg.max_tokens`，
#: 所以 bs 每次请求实际带的就是 cfg 里那个数；默认档要求两侧同值才算一致。

#: 模型给出超过 bs 硬上限的时间线条数（`_coerce_timeline` 的 `out[:20]` 会咬到）。
OVER_CAP_ROWS = 25
#: 没超上限的条数。历史语料 p90=12 / max=19，18 条属于「常见且不该被咬到」那一类。
UNDER_CAP_ROWS = 18

WAIT_FOR_TASK11 = (
    "这条按 task-11 的合同写（严格档 `chat()` 返回后做**只读捕获**：`len(rows) > 20` 才记、"
    "零额外请求、解析失败什么都不做；`core/summarizer.py:265-291`）。"
    "它红就是下面三种之一：①捕获又退化（默认档把模型给的 25 条截成 20）、"
    "②有人改用「多问一轮补密」换条数（那是加请求，不是零成本）、"
    "③有人把 `_apply_dense` 重新包进 `if not _parity_strict()`（拍板是默认档也保留①）。"
    "归属 vendor-engineer / task-11 后续，不要放宽断言，也不许把 WB 削到 20 来骗绿。"
)


def _dense_project():
    return {"title": "对账素材", "up_name": "UP对账", "bvid": "BV1paritycheck",
            "duration_sec": 600, "desc": "固定简介"}


def _dense_segments():
    return [{"start_ms": 0, "text": "开场"}, {"start_ms": 5000, "text": "讲第一件"}]


def _rows(count: int) -> list[dict]:
    return [{"t": f"00:{i:02d}", "text": f"第 {i} 条"} for i in range(1, count + 1)]


def test_default_tier_keeps_a_timeline_longer_than_the_cap(default_env):
    """B1：**默认档**下模型自己给 25 条时间线 → WB 必须留 25 条，bs 同一响应仍是 20 条。

    这条从生产入口（`adapter.run`）走，不手工喂 `dense_timeline`：手工构造只证明覆盖函数
    自己会覆盖（那条路径 task-4 就在了），守不住「拍板保留①」这条命令。

    20 vs 25 是**已登记的有意差异**（`bili_scribe` 的 `out[:20]` 不改，用户命令是「保留①」），
    所以不许当复刻失败；更不许为了让两边相等把 WB 削到 20——那是拿删除断言换绿。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    script = three_pass_script({**PAYLOAD, "timeline": _rows(OVER_CAP_ROWS)})
    project, segments = _dense_project(), _dense_segments()

    bundle = adapter.build_bundle(project, segments)
    bs_llm = FakeLLM(list(script))
    bs_out = bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="wengao")

    client = FakeLLM(list(script))
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="wengao")

    assert len(bs_out.timeline) == TIMELINE_CAP, (
        f"bs 侧 20 条硬上限是登记差异的另一半，它变了就得重新登记：{len(bs_out.timeline)}")
    assert len(result["summary"]["timeline"]) == OVER_CAP_ROWS, (
        f"{WAIT_FOR_TASK11}\n默认档下 WB 应把模型给的 {OVER_CAP_ROWS} 条都留下，"
        f"实际只剩 {len(result['summary']['timeline'])} 条")

    # 「零额外请求」的唯一证据：请求次数与 calls 都必须和不补①时一样（3 次）。
    # 少这一句，将来有人为了「让①有内容」去多问一轮，上面两条断言照样绿。
    assert len(client.requests) == len(bs_llm.requests) == 3, (
        f"①必须是只读捕获，不许多发请求：WB {len(client.requests)} 次 / bs "
        f"{len(bs_llm.requests)} 次")
    assert result["calls"] == 3, f"calls 多出一次就不是零成本了：{result['calls']}"
    assert [r["json_mode"] for r in client.requests] == [True, False, True], (
        "请求形状也变了，说明捕获插进了方法论序列里")

    # 除登记在册的那一处，其余字段必须与 bs 逐字相同。
    bs_dict = bs_out.to_dict()
    for field, value in bs_dict.items():
        if field == "timeline":
            continue
        assert result["summary"][field] == value, f"默认档冒出未登记差异：{field}"


def test_default_tier_under_the_cap_is_byte_identical_and_stays_silent(default_env):
    """B6 之一：模型给 18 条（没超上限）→ 默认档必须与副本**逐字节全等**，捕获不动声色。

    钉 task-11 的另一半：只读捕获不许把「根本没被截断」那条路改坏。两层各管一层：
      · 端到端：WB 的 summary 与 bs 的 `to_dict()` 逐字节相同（18 条一条不多一条不少）；
      · 单元：直接走 `SummaryLLM.chat()` 拿一份 18 条的合法 JSON，`dense_timeline` 必须
        仍是空——捕获要么不记，记了也不许改变产出。
    这条现在就绿，task-11 落地后必须继续绿；它红了就是「修①顺手碰坏了不截断的路径」。
    """
    from video_workbench.core import summarizer as adapter
    from video_workbench.core.summarizer import SummaryLLM

    cfg = make_cfg()
    payload = {**PAYLOAD, "timeline": _rows(UNDER_CAP_ROWS)}
    script = three_pass_script(payload)
    project, segments = _dense_project(), _dense_segments()

    bundle = adapter.build_bundle(project, segments)
    bs_llm = FakeLLM(list(script))
    bs_out = bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="wengao")

    client = FakeLLM(list(script))
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="wengao")

    assert len(bs_out.timeline) == UNDER_CAP_ROWS, "bs 侧这份不该被 20 条上限咬到"
    assert json.dumps(result["summary"], ensure_ascii=False, sort_keys=True) == \
        json.dumps(bs_out.to_dict(), ensure_ascii=False, sort_keys=True), (
        "默认档下没超上限的产出必须与副本逐字节相同（捕获不许动它）")
    assert len(client.requests) == len(bs_llm.requests) == 3

    llm = SummaryLLM(FakeLLM([json.dumps(payload, ensure_ascii=False)]), model="stub-summary")
    llm.chat([{"role": "user", "content": "写一份总结"}], json_mode=True)
    assert list(getattr(llm, "dense_timeline", None) or []) == [], (
        "没超上限时捕获必须什么都不记；记了就会被当成「被截断」，把一条好路拖进差异里")


def test_default_tier_half_written_timeline_fails_without_an_extra_ask(default_env):
    """B6 之二：回复断在半截 `{"timeline": [{"t"` → 默认档不许多问，报错仍带开关提示。

    三个点各有分工：
      · **静默**：解析失败不许触发任何补救，只发 1 次请求；
      · **不多问**：这是「只读捕获不许变成第二次请求」最尖锐的反例——断在半截正是
        最容易诱使人「再要一次」的形状；
      · **报错仍是** `LLM_BAD_RESPONSE` 且末尾含那句开关。
    bs 侧同一份回复也必须是 1 次请求后 `SummarizeError`（登记它的真实行为，别照 WB 写）。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    half = '{"timeline": [{"t"'

    bs_llm = FakeLLM([half])
    with pytest.raises(bs_summarizer.SummarizeError):
        bs_summarizer.summarize(make_bundle(SHORT_BODY), bs_llm, cfg, role_key="wengao")
    assert len(bs_llm.requests) == 1, "bs 的失败形状变了，得重新登记"

    client = FakeLLM([half])
    with pytest.raises(WBError) as exc:
        adapter.run(_dense_project(), _dense_segments(), client=client, cfg=cfg,
                    ctx=QuietCtx(), role="wengao")
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert PARITY_ENV_NAME in str(exc.value.message), (
        f"半截 JSON 也要把开关念出来：{exc.value.message!r}")
    assert len(client.requests) == 1, (
        f"默认档多问了 {len(client.requests)} 次——捕获或解析失败路径偷偷补救了")


def test_default_tier_malformed_json_names_the_switch_and_keeps_the_old_prefix(default_env):
    """B3：默认档坏 JSON → 前半句逐字不变，末尾追加那句开关。

    前半句 `模型没给出可用的总结 JSON：` 是既有用例钉着的文案（`test_jsonfix.py` 里钉的是
    **阶梯用尽之后**的另一句，两者不许混用），一个字不许改；开关只准追加在末尾。
    这条钉「降级可见」：默认档不抢救是用户自己拍的板，报错必须替他把开关说出来——
    否则现场只有一句「模型没有返回可解析的 JSON」，没人知道是档位把它拦住了。
    实测一天的日志（`wb-20261101.log`：重问 7 / 续写 5 / 分两次 1 / 仍失败 1）说明
    这不是假想成本，那句话每天都要被读到十几次。
    **分工（lead 判卷结论）**：本文件与 B 组钉失败**形状**（异常类型、`code`、前半句、末尾
    开关句）；「请求次数随不随档位变」在 `test_summary_parity_teeth.py` ⑦/⑧。合起来才是
    完整判据，别在任一边补另一边的主张。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    client = FakeLLM(["这不是 JSON"])
    with pytest.raises(WBError) as exc:
        adapter.run(_dense_project(), _dense_segments(), client=client, cfg=cfg,
                    ctx=QuietCtx(), role="wengao")

    message = str(exc.value.message)
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert message.startswith("模型没给出可用的总结 JSON："), (
        f"前半句是既有用例钉着的，不许改：{message!r}")
    assert message.rstrip().endswith(f"{PARITY_ENV_NAME}=0 后重试。"), (
        f"开关提示必须在末尾，用户才知道下一步动手的是什么：{message!r}")


def test_loose_tier_failure_does_not_name_the_switch(loose_env):
    """反面：宽松档的失败**不许**提开关。

    宽松档里报那句是误导——那时阶梯已经全部用尽，设 `=0` 并不会多一点帮助。
    没有这条，「到处追加提示」也能让上面那条 B3 一直绿。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    client = FakeLLM(["这不是 JSON"] * 8)
    with pytest.raises(WBError) as exc:
        adapter.run(_dense_project(), _dense_segments(), client=client, cfg=cfg,
                    ctx=QuietCtx(), role="wengao")
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert PARITY_ENV_NAME not in str(exc.value.message), (
        f"宽松档不该提对账开关：{exc.value.message!r}")
    assert len(client.requests) > 1, "宽松档必须真的走过阶梯，否则这条只是没提开关而已"


def test_default_tier_first_round_budget_equals_the_configured_value(default_env):
    """B4：默认档第一轮 `max_tokens` 两侧同值，且等于**当前 cfg 里那个数**。

    `max_tokens` 是唯一一处「看原始 kwargs 会看错」的参数：上游根本不写它
    （`bili_scribe/infra/llm.py:259` 由客户端补配置值），而 WB 的 `ChatClient` 在 `None`
    时干脆不发这个字段——所以判据必须是**还原后的有效值**相等（`_normalized()`），
    默认档由 `_parity_max_tokens()` 把配置值显式补上。

    **期望值现读 `cfg.llm.max_tokens`，不写死 4096**（lead 2026-11-03 指令）。理由是真模型
    数据：推理型 `auto/hy4-preview` 在 4096 下 100% 把预算烧在思考上（实测
    `reasoning_tokens=4096`、正文 0），16384 下才出合法 JSON（121.7s）；非推理 flash 在
    4096 下 9.5s 出完整 JSON。工作区 `config.yaml` 很可能马上改成 16384——把 4096 写进
    断言，那次改动会让这条**假红**（产品没错，是用例把配置值当成合同抄了一遍）。
    下面第二遍用 16384 跑同一套断言，证明这个数是**跟着配置流过去**的。
    """
    from video_workbench.core import summarizer as adapter

    script = three_pass_script()
    project, segments = _dense_project(), _dense_segments()
    bundle = adapter.build_bundle(project, segments)

    for configured in (4096, 16384):
        cfg = make_cfg(max_tokens=configured)
        bs_llm = FakeLLM(list(script))
        bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="wengao")
        client = FakeLLM(list(script))
        adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                    role="wengao")

        assert cfg.llm.max_tokens == configured, "夹具没把配置值设进去"
        assert bs_llm.requests[0]["max_tokens"] is None, "bs 一直不写这个参数"
        assert client.requests[0]["max_tokens"] == configured, (
            f"默认档必须按配置原样补上 {configured}，"
            f"实际 {client.requests[0]['max_tokens']!r}")
        for index, item in enumerate(_normalized(client.requests, cfg)):
            assert item["max_tokens"] == configured, (
                f"默认档（cfg={configured}）第 {index + 1} 次的有效预算偏离配置值："
                f"{item['max_tokens']}（两侧同值才算一致）")
        for index, item in enumerate(_normalized(bs_llm.requests, cfg)):
            assert item["max_tokens"] == configured, (
                f"bs 侧第 {index + 1} 次也必须是配置值 {configured}：{item['max_tokens']}")


def test_the_normalization_covers_every_listed_field():
    """归一化的字段集合必须显式等于 `EFFECTIVE_FIELDS`（防「少比一个字段」静默通过）。

    这条针对的是我自己这套对账机制：以前判据是「两个元组相等」，谁把元组里少写一项
    （比如不再比 `temperature`），所有比较会**一起变绿**，没人看得见。现在字段名
    列成清单，少一项这里就红。（vendor 在 task-17 收尾里承认没逐行读完 `_effective`
    归一化了哪些字段——这个缺口就在这里补。）
    """
    cfg = make_cfg()
    probe = FakeLLM(three_pass_script())
    bs_summarizer.summarize(make_bundle(SHORT_BODY), probe, cfg, role_key="wengao")
    rows = _normalized(probe.requests, cfg)
    assert rows, "假 LLM 一次请求都没记下来，这条会变成空断言"
    assert set(rows[0]) == set(EFFECTIVE_FIELDS), (
        f"归一化字段集合漂了：实得 {sorted(rows[0])}，合同清单 {sorted(EFFECTIVE_FIELDS)}")
    # 每一项都得是真的量到了东西，不是恒为 None 的占位。
    assert rows[0]["messages"] and rows[0]["json_mode"] is not None
    assert rows[0]["temperature"] == cfg.llm.temperature
    assert rows[0]["max_tokens"] == cfg.llm.max_tokens


def test_plan_a_changes_the_system_of_exactly_the_sourced_roles(default_env):
    """A 的**边界**：只有 `prompt_source.MAP` 里那些角色的 system 变了，其余照旧。

    这条不放宽任何维度，它做的是把「允许不同的范围」本身钉死：
    - 有源角色（与 bs 共有的那 4 个）：WB 的 system 与 bs 的**必然不同**，且不同之处只在
      正文——两侧都以 OUTPUT_CONTRACT 结尾（`_assert_wb_system_by_rule` 已钉，这里再钉一次
      「确实不同」，免得哪天绑定失效、两边又变一样而用例还在绿）；
    - 无源的共有角色：两侧逐字相同；
    - `shipin` 是**权威层自造**的角色，bs 侧根本没有它 ⇒ 不能拿 bs 比，改成钉「它吃到了
      `视频总结.txt`，而且那份内容没漏进任何别的角色」。

    分类口径（task-25 写死在这里，别再用「MAP 有 4 项」当事实）：
      * 共有 = `SHARED_ROLE_KEYS`（bs 也认识的那几个），A 的比较只在这一层做；
      * WB-only = `ROLE_KEYS` 里不在 bs 的那些（现在是 `shipin`），它天然没有对侧；
      * 两段合起来必须等于 `ROLE_KEYS`，出现第三段就说明有人加了角色却没决定它吃不吃 txt。
    """
    wb = _wb_summarizer()
    pkg = _wb_package()
    cfg = make_cfg()
    bs_keys = {role.key for role in bs_roles.ALL_ROLES}
    sourced_shared = tuple(key for key in SHARED_ROLE_KEYS if key in ps.MAP)
    unsourced_shared = tuple(key for key in SHARED_ROLE_KEYS if key not in ps.MAP)
    wb_only = tuple(key for key in pkg.ROLE_KEYS if key not in bs_keys)
    assert set(ps.MAP) == set(sourced_shared) | set(wb_only), (
        f"MAP 里的角色既不在共有段也不在 WB-only 段：{sorted(ps.MAP)}")
    assert len(unsourced_shared) == 3, (
        f"无源的共有角色应当还是那 3 个，实得 {unsourced_shared}")

    for role_key in SHARED_ROLE_KEYS:
        wb_llm = FakeLLM(three_pass_script())
        bs_llm = FakeLLM(three_pass_script())
        bs_summarizer.summarize(make_bundle(SHORT_BODY), bs_llm, cfg, role_key=role_key)
        wb.summarize(make_bundle(SHORT_BODY), wb_llm, cfg, role_key=role_key)
        wb_system = wb_llm.requests[0]["messages"][0]["content"]
        bs_system = bs_llm.requests[0]["messages"][0]["content"]
        if role_key in ps.MAP:
            assert wb_system != bs_system, (
                f"{role_key} 绑了 txt 但两侧 system 仍然相同——绑定没生效")
            assert len(wb_system) > len(bs_system), (
                f"{role_key}：txt 全文应当比压缩版长（实测 {len(wb_system)} vs "
                f"{len(bs_system)}），现在反了")
        else:
            assert wb_system == bs_system, (
                f"无源角色 {role_key} 的 system 被改了——A 不该碰它")

    video_summary_text = ps.load("视频总结.txt").strip()
    assert video_summary_text, "视频总结.txt 读不到，下面两条断言都会空转"
    head = video_summary_text[:40]
    # 它现在绑给 shipin：shipin 的请求里必须吃得到（这条是「绑定确实发生」的正证）。
    assert "shipin" in wb_only, (
        f"前置不成立：shipin 应当在 WB-only 段里，实得 {wb_only}")
    shipin_llm = FakeLLM(three_pass_script())
    wb.summarize(make_bundle(SHORT_BODY), shipin_llm, cfg, role_key="shipin")
    shipin_system = shipin_llm.requests[0]["messages"][0]["content"]
    assert head in shipin_system, (
        f"视频总结.txt 没接进 shipin 的 system——MAP 里那一项失效了：{shipin_system[:80]!r}")
    # 反向：那份内容不许出现在任何**别的**角色里（一条 txt 喂多个角色＝方法论串味）。
    for role_key in [key for key in pkg.ROLE_KEYS if key != "shipin"]:
        other_llm = FakeLLM(three_pass_script())
        wb.summarize(make_bundle(SHORT_BODY), other_llm, cfg, role_key=role_key)
        joined = "\n".join(str(m["content"]) for m in other_llm.requests[0]["messages"])
        assert head not in joined, (
            f"视频总结.txt 内容串进了 {role_key} 的提示词——一份 txt 喂了多个角色")


def test_plan_a_costs_only_the_two_structured_rounds(default_env):
    """A 的成本口径（写注释用，别照 vendor 那句错的抄）：**固定两轮**，不随分段数放大。

    实测形状：长素材走预读时，每一次预读请求的 system 都是 bs 的 `MAP_SYSTEM`，
    **两侧相同**、都不含角色提示词——所以角色换成 txt 全文不会按分段数乘一遍。
    适配层的 `with prompt_source.use_extra_prompt(...)` 也只包住归约那一段，
    `prepare_material` 在 with 外面（`video_workbench/core/summarizer.py:876` 与
    `:884`/`:962` 与 `:968`），这正是「预读不吃开关」的实现依据。
    成本 = 生成与修订两轮各加一段 txt 正文，实测每轮约 +3.5K 字符
    （三轮 system 长度形如 [5008, 219, 5008]，219 是 critique 自己那句）。
    """
    wb = _wb_summarizer()
    cfg = make_cfg()
    # 预读每一段吃一条**纯文本**笔记（不是 JSON）。脚本长度按分段数给，另留富余：
    # 真实调用次数若超过分段数，会先被下面的 `len(pre_reads)` 抓住。
    notes = [f"第 {i} 段的小记" for i in range(1, EXPECTED_LONG_CHUNKS + 4)]
    wb_llm = FakeLLM(list(notes))
    bs_llm = FakeLLM(list(notes))
    bundle = make_bundle(LONG_BODY)

    wb_material = wb.prepare_material(bundle, wb_llm, cfg)
    bs_material = bs_summarizer.prepare_material(bundle, bs_llm, cfg)
    assert wb_material.chunks == bs_material.chunks == EXPECTED_LONG_CHUNKS

    map_system = bs_summarizer.MAP_SYSTEM
    for side, llm in (("WB", wb_llm), ("bs", bs_llm)):
        pre_reads = [r for r in llm.requests
                     if str(r["messages"][0]["content"]).strip().startswith(
                         map_system.strip()[:10])]
        assert len(pre_reads) == EXPECTED_LONG_CHUNKS, (
            f"{side} 预读次数不对：{len(pre_reads)}")
        for record in pre_reads:
            assert record["messages"][0]["content"] == map_system, (
                f"{side} 的预读请求带了别的东西——预读不该吃角色提示词")
    assert wb_llm.requests == bs_llm.requests, (
        "长素材预读两侧必须完全一致：A 不影响预读那一段")



def test_loose_tier_first_round_is_left_unwritten(loose_env):
    """宽松档第一轮仍是 `None`：②的抬升只发生在续写/拆分/重问那几步。

    与上一条配对，把「抬预算」的界线钉住：默认档全程 4096；宽松档第一轮按调用方原样，
    只有阶梯那几步才抬（具体数值由 `test_summary_truncate.py` 里钉了 `loose_env` 的
    那几条守）。少了这条，「宽松档也把第一轮抬到 12288」这种改动不会被发现——
    而它正好破坏与 bs 的第一轮可比性。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    # 用 25 条时间线那份 payload：时间线够密，宽松档的 `_finalize` 不会为补密度多问一轮，
    # 请求序列才纯是「方法论三步」，第一轮才是干净的观察点（实测三步＝3 次）。
    script = three_pass_script({**PAYLOAD, "timeline": _rows(OVER_CAP_ROWS)})
    client = FakeLLM(list(script))
    adapter.run(_dense_project(), _dense_segments(), client=client, cfg=cfg,
                ctx=QuietCtx(), role="wengao")
    assert len(client.requests) == 3, (
        f"宽松档这份素材应当只走方法论三步，实际 {len(client.requests)} 次——"
        "多出来的是补密度那一轮，那就不是「第一轮可比」的场景了")
    assert client.requests[0]["max_tokens"] is None, (
        f"宽松档不该改第一轮（那是 ②的活，只在阶梯步生效）：{client.requests[0]!r}")


def test_sparse_timeline_costs_an_extra_request_only_in_the_loose_tier(default_env):
    """补密度那一轮：默认档**不许**多问（与 bs 一致），宽松档会多问一次。

    实测（同一份只给 1 条时间线的 payload）：
      · 宽松档 4 次：①生成 → ②「只输出时间线」补密一轮（那次 `max_tokens=4096`）
        → ③批判 → ④修订；
      · 默认档 3 次，与 bs 一字不差。
    这条是 B1 那句「①必须零额外请求」的对照组：它证明**加一轮请求**是宽松档的老办法，
    task-11 给默认档选的是只读捕获。将来有人把那轮补密搬进默认档，这条立刻红——
    那时用户看到的是「同样一份总结，多花一次模型钱」。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    sparse = three_pass_script({**PAYLOAD, "timeline": _rows(1)})
    project, segments = _dense_project(), _dense_segments()

    client = FakeLLM(list(sparse))
    adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(), role="wengao")
    assert len(client.requests) == 3, (
        f"{WAIT_FOR_TASK11}\n默认档多问了 {len(client.requests)} 次——补密那一轮是宽松档的"
        "老办法，默认档要靠只读捕获，不许加请求")
    assert [r["json_mode"] for r in client.requests] == [True, False, True], (
        f"默认档请求形状偏离 bs：{[r['json_mode'] for r in client.requests]}")

    bs_llm = FakeLLM(list(sparse))
    bs_out = bs_summarizer.summarize(adapter.build_bundle(project, segments), bs_llm, cfg,
                                     role_key="wengao")
    assert len(bs_llm.requests) == 3, "bs 一直是三步，多出来的是 WB 的动作"
    assert len(bs_out.timeline) == 1, "稀疏那份在 bs 侧就是一条，没被补过"


def test_dense_rows_left_by_the_cap_override_stay_renderable(default_env):
    """①覆盖之后，条目必须仍是前端认得的样子——否则「留下 25 条」等于「留下 25 行空白」。

    这条是写这批用例时**发现并已被修掉**的真问题，留在这里当回归锁。
    发现过程：副本的 `_coerce_timeline()`（`bili_scribe/core/summarizer.py:168-183`）会把
    模型爱写的别名叫回来（`text or content`、`t or time`，裸字符串补成 `{"t":"","text":…}`），
    而当时的 `_apply_dense()` 是 `summary.timeline = dense` **原样覆盖**，不过这道清洗；
    前端 `video_workbench/web/js/pages/summary.js` 只读 `item.t` 与 `item.text`。
    三者接起来：模型给 `{"time":…, "content":…}` 且超过 20 条时，界面时间线**条目数没丢、
    文字全丢**。vendor 当时把这代价写成「形状不如清洗过的整齐」，实际后果是整块空白。

    修法（task-11 后续，`core/summarizer.py:643-668` 的 `_clean_dense()`）：覆盖前先洗成与
    副本同构，规则逐条对齐 `_coerce_timeline` 只去掉 `[:20]`，文本取值走副本的
    `_coerce_text()`——不另抄一份，免得又长出第二套真相。

    断言写成「形状必须可渲染」而不是「必须等于某个具体值」：清洗规则本身归副本，
    我不在这里另定一套规范。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    rows = [{"time": f"00:{i:02d}", "content": f"第 {i} 条"} for i in range(1, 26)]
    script = three_pass_script({**PAYLOAD, "timeline": rows})
    project, segments = _dense_project(), _dense_segments()

    bundle = adapter.build_bundle(project, segments)
    bs_llm = FakeLLM(list(script))
    bs_out = bs_summarizer.summarize(bundle, bs_llm, cfg, role_key="wengao")
    # 先证明副本确实会清洗别名：bs 那 20 条是 t/text，而且文字没丢。
    assert all(set(row) == {"t", "text"} for row in bs_out.timeline), bs_out.timeline[:2]
    assert any(row["text"] for row in bs_out.timeline), "bs 侧的清洗是这条用例的参照系"

    client = FakeLLM(list(script))
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="wengao")
    wb_rows = result["summary"]["timeline"]
    assert len(wb_rows) == OVER_CAP_ROWS, (
        f"{WAIT_FOR_TASK11}\n先确认①真的留住了 25 条，再谈形状：实际 {len(wb_rows)} 条")

    bad = [row for row in wb_rows if not isinstance(row, dict)
           or set(row) != {"t", "text"} or not row["text"]]
    assert not bad, (
        f"①覆盖过来的条目前端渲染不出来（summary.js:943-944 只读 item.t / item.text）：{bad[:2]}"
        "……覆盖前必须过 `_clean_dense()` 的别名归一，不必删①、也不必改拍板")
    # 别名归一之后文字还得对得上，不能只是键名换了。
    assert [row["text"] for row in wb_rows[:3]] == ["第 1 条", "第 2 条", "第 3 条"], wb_rows[:3]
    assert [row["t"] for row in wb_rows[:3]] == ["00:01", "00:02", "00:03"], wb_rows[:3]


def test_blank_dense_rows_are_dropped_instead_of_padded(default_env):
    """洗不掉的条目要**丢掉**，不许为了保住条数把空白行塞进时间线。

    `_apply_dense` 的顺序是「先洗、洗完再比条数」（`core/summarizer.py:691-693` 写明了）：
    模型给 25 条但其中 8 条 `content` 是空的，洗完只剩 17 条 ≤ 已有 17 条，就该原样返回；
    反过来如果洗完仍有 21 条，那 21 条才值得覆盖。
    这条守的是「① 只保条数」与「界面不许出现读不出内容的行」之间的取舍——
    写成断言而不是注释，因为把未清洗条目塞进来能同时让 B1 的条数断言变绿。
    """
    from video_workbench.core import summarizer as adapter

    cfg = make_cfg()
    rows: list[dict] = []
    for i in range(1, 26):
        rows.append({"time": f"00:{i:02d}",
                     "content": "" if i % 3 == 0 else f"第 {i} 条"})   # 每三条空一条
    keep = [row for row in rows if row["content"]]
    assert len(rows) == OVER_CAP_ROWS and len(keep) == 17, len(keep)

    script = three_pass_script({**PAYLOAD, "timeline": rows})
    project, segments = _dense_project(), _dense_segments()
    bs_llm = FakeLLM(list(script))
    bs_out = bs_summarizer.summarize(adapter.build_bundle(project, segments), bs_llm, cfg,
                                     role_key="wengao")
    client = FakeLLM(list(script))
    result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                         role="wengao")

    wb_rows = result["summary"]["timeline"]
    assert all(isinstance(row, dict) and row.get("text") for row in wb_rows), (
        f"时间线里出现了没有内容的条目：{[r for r in wb_rows if not r.get('text')][:2]}")
    assert len(wb_rows) <= len(keep), (
        f"覆盖过来的条目比洗得出来的还多（{len(wb_rows)} > {len(keep)}）＝塞了垃圾")
    assert len(wb_rows) == len(bs_out.timeline), (
        f"洗完不比已有的多就该原样返回（副本 20 条上限在这里是 {len(bs_out.timeline)} 条）："
        f"实际 {len(wb_rows)} 条")


def test_explicit_one_and_unset_are_the_same_tier(default_env, strict_env, monkeypatch):
    """`=1` 与「未设」必须给出一模一样的产出与请求序列（B5 的等价性那一半）。

    取值表只证明 `_parity_strict()` 的返回值一致；那还不够——真正要防的是有人拿
    `os.environ.get(PARITY_ENV)` 的**原始字符串**去做别的事（比如拼日志、选分支），
    那时函数说「同档」，链路却按两种写法走两条路。所以这里同一份脚本各跑一遍，
    逐字节比 summary、比请求序列（含 `max_tokens` 原值）、比 notes 数量。

    两个 fixture 叠着用不冲突：`default_env` 先 delenv，`strict_env` 后设 `=1`；
    本用例内部自己再切一次，切换顺序与 fixture 无关。
    """
    from video_workbench.core import summarizer as adapter
    from wb_parity_tiers import env_name

    cfg = make_cfg()
    script = three_pass_script({**PAYLOAD, "timeline": _rows(OVER_CAP_ROWS)})
    project, segments = _dense_project(), _dense_segments()

    runs = []
    for value in (None, "1"):
        if value is None:
            monkeypatch.delenv(env_name(), raising=False)
        else:
            monkeypatch.setenv(env_name(), value)
        client = FakeLLM(list(script))
        result = adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                             role="wengao")
        runs.append((json.dumps(result["summary"], ensure_ascii=False, sort_keys=True),
                     [(r["messages"], r["json_mode"], r["temperature"], r["max_tokens"])
                      for r in client.requests]))

    assert runs[0] == runs[1], (
        "未设与显式 `=1` 走了两条路：档位不能只看 `_parity_strict()` 的返回值，"
        "任何按原始字符串分支的地方都会在对账里留后门")


#: 档位取值表：未设与非 OFF 值一律严格，只有这三种写法宽松。
TIER_TABLE = (
    (None, True, "未设＝默认严格（用户的常态）"),
    ("", True, "空串也算严格：设了个空值不等于关掉对账"),
    ("1", True, "显式开"),
    ("true", True, "小写 true"),
    ("TRUE", True, "大写不许被当成未知值掉到宽松"),
    ("2", True, "不在表里的值不许变成宽松"),
    ("yes", True, "`yes` 不是 OFF 写法——想关掉得写 0/false/off"),
    ("0", False, "关掉：数字零"),
    ("false", False, "关掉：英文 false"),
    ("FALSE", False, "关掉：大写写法"),
    ("off", False, "关掉：英文 off"),
    (" 0 ", False, "带空格的 0 也要认（strip 之后比较）"),
)


@pytest.mark.parametrize("value,expect_strict,why", TIER_TABLE)
def test_parity_tier_value_table(monkeypatch, value, expect_strict, why):
    """B5 之一：档位取值表（未设=严格；`0`/`false`/`off`=宽松；其它一律严格）。

    OFF 集合从产品侧读（`wb_parity_tiers.off_values()`），不在测试里另抄一份——
    自己抄一份，产品哪天加个新写法（比如 `no`），测试会按老表继续判它「严格」，
    而真实行为已经变了，这就是静默分叉。
    """
    from wb_parity_tiers import env_name, is_strict, off_values

    if value is None:
        monkeypatch.delenv(env_name(), raising=False)
    else:
        monkeypatch.setenv(env_name(), value)
    assert is_strict() is expect_strict, f"{why}（值={value!r}）"
    if value is not None and str(value).strip().lower() in off_values():
        assert expect_strict is False, "本表的期望与产品侧 OFF 集合矛盾，表要重登"


def test_parity_tier_switches_within_one_process(monkeypatch):
    """B5 之二：同进程内来回切必须立即生效——钉「没有 import 期缓存」。

    只看 `_parity_strict()` 的返回值不够：真正要防的是模块 import 时把档位算成常量
    （`STRICT = _parity_strict()` 那种写法），那样函数本身仍然「对」，而走阶梯的
    `chat()` 却永远按旧档跑。所以来回切三遍，每遍都**实际跑一次坏 JSON**，
    用请求次数（严格 1 次 / 宽松 >1 次）当可观测证据。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer as adapter
    from wb_parity_tiers import env_name

    cfg = make_cfg()
    project, segments = _dense_project(), _dense_segments()
    seen: list[int] = []

    for value in (None, "0", None, "false", None):
        if value is None:
            monkeypatch.delenv(env_name(), raising=False)
        else:
            monkeypatch.setenv(env_name(), value)
        client = FakeLLM(["这不是 JSON"] * 8)
        with pytest.raises(WBError):
            adapter.run(project, segments, client=client, cfg=cfg, ctx=QuietCtx(),
                        role="wengao")
        seen.append(len(client.requests))

    assert seen[0] == seen[2] == seen[4] == 1, f"严格档必须每次都是 1 次请求：{seen}"
    assert seen[1] > 1 and seen[3] > 1, (
        f"宽松档必须真的走阶梯，否则这条只在量严格档：{seen}")


#: ①被档位关掉的旧形状（task-8 之前那段代码）。
_DENSE_GATE_RE = re.compile(
    r"if\s+not\s+_parity_strict\(\)\s*:\s*\n\s*summary\s*=\s*_apply_dense\(")


def test_dense_override_is_not_gated_by_the_tier_in_source():
    """形状检查：`_apply_dense` 的调用点不许再被档位开关包住（①与档位无关）。

    行为层那条（默认档留 25 条）现在红、在等 task-11；这条现在就绿，守的是「别再装回去」：
    门一旦重新出现，①就退回到「只有宽松档才有」，而拍板是默认档也保留。
    """
    src = ADAPTER_PY.read_text(encoding="utf-8")
    assert _DENSE_GATE_RE.search(src) is None, (
        "①又被档位关掉了：拍板是「默认严格但保留①」，`_apply_dense` 必须无条件调用")
    assert src.count("summary = _apply_dense(summary, llm)") >= 2, (
        "run() 与 refine() 两处补密都必须在，少一处就是只修了一半")


def test_dense_gate_detector_recognises_the_old_shape():
    """上一条的灵敏度自证：旧形状必须能被识别出来，合规写法不许误报。

    正则若写错，`search(...) is None` 会永远成立，那条形状检查就成了装饰品。
    """
    old = "    if not _parity_strict():\n        summary = _apply_dense(summary, llm)\n"
    assert _DENSE_GATE_RE.search(old), "检测器写坏了：它认不出当初那段代码"
    new = "    summary = _apply_dense(summary, llm)\n"
    assert not _DENSE_GATE_RE.search(new), "合规写法被误判"
