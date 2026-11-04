"""`shipin`（视频总结）这个 WB 自造角色的常驻判据：单轮成本、契约优先、位置约束。

配套产品：`video_workbench/core/summary/__init__.py:70-86`（`SHIPIN` 与 `_WB_ONLY`）与
`video_workbench/core/prompt_source.py` 的 `MAP`（task-24 起多了一项
`shipin → 视频总结.txt`）。对账维度（WB 与 bs 除 system 正文外逐维度一致）在
`test_summary_parity.py`，真源绑定与回落机制在 `test_summary_prompt_source.py`，
**这个文件只管「多了一个单轮角色」这件事本身值不值、以及它有没有越界**。

## 为什么这个角色值得单独一套用例

`standard` 退役之后，WB 一个单轮角色都不剩：每次总结都要付三轮
（生成→自我批判→修订，约 3 倍耗时）。`shipin` 的意义就是把「便宜的那档」还给用户，
所以它的**成本**就是它存在的理由——一旦它悄悄变成三轮，这个角色就白加了，
而界面上一切照旧。判据 1 因此钉的是**实际发出的请求次数**，不是 `role.passes` 那个字段：
三轮那道门是 `core/summary/summarizer.py:572` 的
`if role.passes >= 3 and role.critique and role.revise:`，
字段对、门没生效是最可能的坏法。

## 三处口径与合同措辞的偏差（都实测过，不是猜的）

1. **「system 必须以 OUTPUT_CONTRACT 结尾」不能按字面写在 `role.system` 上**。
   实测五个有源角色的 `role.system` 都**不以契约结尾**：`视频总结.txt` 归一化（CRLF→LF）
   后 511 字，`get_role("shipin").system` 正好是这 511 字。契约是**构建请求时**才接上去的
   （副本 `summarizer.py:518`：先 `role.system.strip()`，再接 `roles.OUTPUT_CONTRACT`）。
   所以这条判据落在「请求里的 system」这一层（`FakeLLM` 记录的那份）——
   那才是用户实际发给模型的东西。照字面写会永远红。
2. **WB 自造的角色不能拿 bs 当基准**。bs 的 `get_role()` 对未知 key **静默回落到
   `standard`**，而 `standard.system` 是空串——任何「WB 的 system 应当等于 bs 的压缩版」
   这类断言对 `shipin` 都会变成「断言它是空串」，恰好是要防的那件事。
   凡是拿 bs 当参照的比较，都先用 `bs_roles.ALL_ROLES` 的 key 集合限定范围。
3. **角色总数一律现读**。`视频总结.txt` 的写作四则与 `renhua` 的 tagline 同源
   （`一个锚点撑全文 / 推理外显 / 变形替代定义 / 体感优先`），用户随时可能拍板合并。
   本文件不出现「7 个」「8 个」这种字面量：共有段从 bs 名单推，
   WB 自造段从 `ROLE_KEYS - bs 名单` 推。

## 牙齿（六条，各自只红它该红的那条）

全在 `%TEMP%` 下自己带哨兵名的目录里做：一个一次性 pytest 插件（`-p`，运行时改模块属性），
或一份 Temp web 副本。**禁止对仓库工作区文件按快照覆盖回写**——
上一轮就是「按快照回写」把 vendor 刚落地的改动抹过一次。

* **S-1 把 `shipin` 偷改成 `passes=3`**（并补上 `critique`/`revise` 让它真走三轮）→
  只红 1 条：`test_shipin_sends_exactly_one_model_call`。
  `Role` 是普通 `@dataclass`（`bili_scribe/core/roles.py:36`，实测 `frozen=False`），
  改 `pkg._WB_ONLY_BY_KEY["shipin"]` 那一个对象的三个字段就等于改了 `:572` 那道门。
* **S-2 让真源静默不进请求**（把 `shipin` 的 `system` 换成压缩版底线
  `_copy_roles.COMMON_BOTTOM_LINE.strip()`，等价于 `bind()` 少绑这一个角色）→
  只红 1 条：`test_shipin_request_puts_the_contract_last_after_the_full_txt`。
  刻意不打 `role.system == txt` 那种「两边同步变化」的断言 —— 那种断言在这一形下不红。
* **S-3 改掉副本的 `OUTPUT_CONTRACT`**（`覆盖上面任何与此冲突的要求` → `与上面的要求并列参考`
  再加一条尾巴）→ 红 2 条：上面那条请求侧 + `test_output_contract_is_byte_identical_…`
  （两侧不再相等、登记 sha 也对不上）。bs 侧不动 —— 两侧可以各自漂，所以要靠登记值。
* **S-4 把 `shipin` 塞进副本的角色表**（运行时 `pkg._roles.ALL_ROLES += (SHIPIN,)`）→
  只红 1 条：`test_the_new_role_lives_only_in_the_authority_layer`。
  **盘上那一半**（副本源文本里出现 `shipin` 字面量）由 drift 的逐字节锁负责，
  其灵敏度由 `tests/wb/test_summary_drift.py::test_drift_guard_is_not_vacuous` 的四变体
  **常驻**自证 —— 我不为了一次牙齿去写 vendored 文件（那是产品面，
  而且 b57 那次「按快照回写抹掉 vendor 改动」的事故就是这么发生的）。
* **S-5 把「深度思考」控件加回设置页**（Temp web 副本的字段表里插一行
  `{ path: "llm.thinking", label: "深度思考", … }`）→ 红 2 条：
  `test_the_retired_thinking_control_is_not_in_the_field_table`（源码侧）+
  `test_the_rendered_page_has_no_thinking_control`（页面侧）。
  见 `tests/wb/test_llm_thinking_key_survives.py`。
* **S-6 让写盘模板丢掉 `thinking`**（运行时替换 `video_workbench.config.render_yaml`，
  把含 `thinking` 的行全滤掉；`config.py:903` 是按模块全局名调用的）→
  只红 1 条：`test_an_unrelated_save_carries_the_thinking_value_verbatim`。

本轮实跑（驱动器 `%TEMP%/parity-t25d/t25_teeth.py`，11 个变体含控制组）：
**S-1…S-6 全部红在该红的名字上，控制组（原样 Temp 副本）0 红，
8 个产品文件 sha256 开工前后一字不差。**

"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest
from bili_scribe.core import roles as bs_roles
from video_workbench.core import prompt_source as ps

from test_summary_parity import (  # 同目录、无 __init__.py：直接复用夹具与假 LLM
    SHORT_BODY,
    FakeLLM,
    make_bundle,
    make_cfg,
    single_pass_script,
    three_pass_script,
)
from wb_parity_tiers import default_env  # noqa: F401  fixture：成本断言在默认档下跑

NL = chr(10)

REPO_ROOT = Path(__file__).resolve().parents[2]
WB_SUMMARY_DIR = REPO_ROOT / "video_workbench" / "core" / "summary"
PROMPT_DIR = REPO_ROOT / "video_workbench" / "resources" / "prompts"

#: 逐字节副本那三份文件。漂移锁管**内容**，这里管「这一趟跑完有没有被改过」。
#: 快照必须在 **import 期**取：同一次调用里算两遍再比是恒真，什么都拦不住。
COPY_FILES = (
    WB_SUMMARY_DIR / "roles.py",
    WB_SUMMARY_DIR / "summarizer.py",
    WB_SUMMARY_DIR / "__init__.py",
)
COPY_SHA_AT_IMPORT = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in COPY_FILES if path.is_file()
}

#: 输出契约的身份（现读 `bili_scribe/core/roles.py:51` 后登记）。
#: 这段文本是全产品唯一一处「压住角色原文里那些打印执行日志」的防线：
#: `shipin` 的价值建立在「单轮也要出合法 JSON」上，靠的就是它。
#: 改它必须是有意的——更新这一行并在交付里点名。
REGISTERED_CONTRACT_SHA256 = (
    "e904f5eb2cf0cc835f2a6f4eaa7c7b38e5e3ff6877caac88977a5e546dc9c637")

#: 附加提示词的特征串（与 `test_summary_prompt_source.py` 同一组，那边管开关本身）。
EXTRA_MARKERS = ("奇美拉", "Guardian", "[lbk]")


def _pkg():
    return importlib.import_module("video_workbench.core.summary")


def _other_role_keys() -> tuple:
    """除单轮角色之外的全部角色。**从 ROLE_KEYS 现读**，不写死名单。"""
    return tuple(key for key in _pkg().ROLE_KEYS if key != "shipin")


def _normalized(name: str) -> str:
    """包内 txt 的**现读**内容，只把 CRLF 归一（副本 `:518` 的 `.strip()` 在那之后）。

    判据要求「以 txt 全文开头」必须读文件本身，不许把提示词正文抄进用例——
    抄进用例就成了「改 txt 的同时改测试」，锁就没了。
    """
    return (PROMPT_DIR / name).read_text(encoding="utf-8").replace("\r\n", NL)


# ------------------------------------------------ 判据 1：单轮成本（这个角色的全部意义）


def test_shipin_sends_exactly_one_model_call(default_env):
    """`shipin` 只发一次 generate：没有自我批判、没有修订。

    钉的是**实际请求次数**，不是 `passes` 字段——三轮那道门是
    `core/summary/summarizer.py:572` 的
    `if role.passes >= 3 and role.critique and role.revise:`，
    三个条件里任何一个出错都会让「便宜的档」变贵，而 `passes` 显示的还是 1。
    三条各查一次：次数、字段形状、以及那唯一一次确实是结构化生成轮。
    """
    pkg = _pkg()
    cfg = make_cfg()
    llm = FakeLLM(single_pass_script())
    summary = pkg.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key="shipin")
    assert len(llm.requests) == 1, (
        f"shipin 发了 {len(llm.requests)} 次请求，单轮角色的成本优势没了："
        f"{[r['json_mode'] for r in llm.requests]}")
    role = pkg.get_role("shipin")
    assert role.passes == 1 and not role.critique and not role.revise, (
        f"字段形状与请求次数不符：passes={role.passes} "
        f"critique={bool(role.critique)} revise={bool(role.revise)}")
    assert llm.requests[0]["json_mode"] is True, "唯一那一次不是结构化生成轮"
    assert summary.to_dict()["one_line"], "单轮跑出来的东西是空的"


@pytest.mark.parametrize("role_key", _other_role_keys())
def test_every_other_role_still_pays_three_calls(role_key, default_env):
    """其余角色必须仍然真发三轮——拦住「顺手把别人也降成单轮」。

    参数表从 `ROLE_KEYS` 现读（不是写死的名单）。这条是判据 1 的另一半：
    只钉「shipin 便宜」会鼓励人把便宜档越开越宽，最后没人付得起深度改写。
    """
    pkg = _pkg()
    cfg = make_cfg()
    llm = FakeLLM(three_pass_script())
    pkg.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key=role_key)
    role = pkg.get_role(role_key)
    assert role.passes == 3, f"{role_key} 的 passes 掉到 {role.passes}：三轮档被降了"
    assert len(llm.requests) == 3, (
        f"{role_key} 实际只发 {len(llm.requests)} 次，字段写着 3 轮但门没生效："
        f"{[r['json_mode'] for r in llm.requests]}")


# ------------------------------------- 判据 2：契约优先（落在请求层，见文件头偏差第 1 条）


def test_shipin_request_puts_the_contract_last_after_the_full_txt(default_env):
    """发给模型的那份 system：**txt 全文在前、OUTPUT_CONTRACT 在最后**。

    两个方向都要钉：
      * 契约必须在**最末尾**——角色原文里那些「打印每个阶段的完整输出」会击穿 JSON 解析，
        靠的就是这句覆盖性声明压住它（副本 `:518` 上方的注释也这么写）。顺序调反，
        `_extract_json` 当场坏，而界面上只是「这次效果不好」。
      * txt 全文必须真在里面。只断「结尾是契约」的话，
        有人把 system 换成压缩版再加契约也能过——那方法论就悄悄换了。
    """
    pkg = _pkg()
    cfg = make_cfg()
    llm = FakeLLM(single_pass_script())
    pkg.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key="shipin")
    system = llm.requests[0]["messages"][0]["content"]
    txt = _normalized(ps.MAP["shipin"]).strip()
    assert txt and len(txt) > 200, f"现读的 txt 太短，开头的比较没有意义：{len(txt)}"
    assert system.startswith(txt), (
        f"请求里的 system 不是以 视频总结.txt 全文开头（前 40 字实得 {system[:40]!r}）")
    contract = bs_roles.OUTPUT_CONTRACT.strip()
    assert system.rstrip().endswith(contract), "输出契约不在最末尾"
    assert system.index(txt[:40]) < system.index(contract[:20]), (
        "契约在 txt 之前——同一份文本里的先后顺序就是覆盖关系的先后")


def test_output_contract_is_byte_identical_on_both_sides_and_unchanged():
    """`OUTPUT_CONTRACT` 一个字都没改：先比两侧，再对登记表，再看内容形状。

    三种坏法各拦一层：
      * 只改副本 → 两侧比较红（漂移锁也会红，但那管整份文件）；
      * 两侧同时改成同一份新文本 → 漂移锁与两侧比较都不红，**只有 sha 登记红**；
      * 润色式重写（长度差不多、意思像）→ 前两条可能都看不出，形状那几条红。
    """
    pkg = _pkg()
    assert pkg._roles.OUTPUT_CONTRACT == bs_roles.OUTPUT_CONTRACT, "两侧的契约文本分叉了"
    digest = hashlib.sha256(bs_roles.OUTPUT_CONTRACT.encode("utf-8")).hexdigest()
    assert digest == REGISTERED_CONTRACT_SHA256, (
        f"OUTPUT_CONTRACT 被改过（现读 sha256 {digest}，登记的是 "
        f"{REGISTERED_CONTRACT_SHA256}）。改它必须是有意的：更新登记并在交付里点名——"
        "这段是全产品唯一的「压住角色原文里打印日志类指令」的防线。")
    for phrase in ("只输出一个合法 JSON 对象", "禁止输出任何思考过程",
                   "覆盖上面任何与此冲突的要求"):
        assert phrase in bs_roles.OUTPUT_CONTRACT, f"契约里那句 {phrase!r} 没了"


# ------------------------------------------------- 判据 3：新增角色的位置约束


def test_the_new_role_lives_only_in_the_authority_layer():
    """`shipin` 只许存在于权威层；逐字节副本必须还是 bs 那一份，不含它。

    这条拦的是最省事也最致命的做法：直接往副本 `roles.py` 里加角色。
    那样漂移锁会红，但**在红之前** `ALL_ROLES` 的形状已经和 bs 分了家——
    两侧对账会开始比较两个不同的角色集，那种红会被误读成「对账用例坏了」。
    三层各查一遍：副本运行期对象的 key 集合、两侧源文件里有没有 `shipin` 字面量、
    以及权威层是不是走 `_WB_ONLY` 拼接（那样 WB 自造的角色必然排在清单末尾）。
    """
    pkg = _pkg()
    copy_keys = tuple(role.key for role in pkg._roles.ALL_ROLES)
    bs_keys = tuple(role.key for role in bs_roles.ALL_ROLES)
    assert copy_keys == bs_keys, (
        f"逐字节副本的角色集与 bs 不同：副本 {copy_keys} vs bs {bs_keys}")
    assert "shipin" not in copy_keys, "shipin 进了副本的 ALL_ROLES——塞进 vendored 文件了"
    copy_source = (WB_SUMMARY_DIR / "roles.py").read_text(encoding="utf-8")
    assert "shipin" not in copy_source, "副本 roles.py 源码里出现 shipin"
    bs_source = (REPO_ROOT / "bili_scribe" / "core" / "roles.py").read_text(encoding="utf-8")
    assert "shipin" not in bs_source, "bs 的 roles.py 里出现 shipin（越界到上游了）"

    wb_only = tuple(key for key in pkg.ROLE_KEYS if key not in set(bs_keys))
    assert "shipin" in wb_only, f"权威层自造的那段里没有 shipin：{wb_only}"
    tail = tuple(role.key for role in pkg.ALL_ROLES)[-len(wb_only):]
    assert set(tail) == set(wb_only), (
        f"WB 自造的角色没排在公开清单末尾（末 {len(wb_only)} 个是 {tail}）——"
        "插入位置会改下拉顺序，历史版本对比跟着错位")


def test_the_vendored_files_are_untouched_for_the_whole_run():
    """副本三文件在本趟测试期间 sha 不变（开工前后一致，现取现比）。

    快照在 **import 期**取（文件头 `COPY_SHA_AT_IMPORT`）。同一趟里读两遍再比是恒真，
    什么都拦不住；这条的存在理由就是「跑着跑着有人往 vendored 文件里塞东西」。
    """
    assert COPY_SHA_AT_IMPORT, "import 期没取到任何副本文件，这条会空转"
    for path in COPY_FILES:
        assert path.is_file(), f"副本文件没了：{path}"
        now = hashlib.sha256(path.read_bytes()).hexdigest()
        registered = COPY_SHA_AT_IMPORT.get(path.name)
        assert registered is not None, (
            f"{path.name} 不在 import 期快照里——新文件不进清单就是这条的盲区")
        assert now == registered, (
            f"{path.name} 在这趟测试期间被改过：import 期 {registered[:16]}… "
            f"现在 {now[:16]}…")


# ------------------------------------------------ 判据 5：附加提示词对它同样生效


def test_the_extra_prompt_wraps_shipin_too(default_env):
    """`shipin` 吃不吃「大概率管用 .txt」必须与别的角色**一致**。

    有源角色里它是最晚进 MAP 的那个，最容易被漏在拼接表里——
    漏了不会报错，只是别人开开关时这个角色仍按旧方法论走。
    成本按**不变量**钉（差值 == 附加全文 + 1 个换行），不抄字面长度：
    txt 与附加文件都是用户手写的，字数会漂，差值关系不会。
    """
    pkg = _pkg()
    cfg = make_cfg()
    off_llm = FakeLLM(single_pass_script())
    pkg.summarize(make_bundle(SHORT_BODY), off_llm, cfg, role_key="shipin")
    off = off_llm.requests[0]["messages"][0]["content"]

    on_llm = FakeLLM(single_pass_script())
    with ps.use_extra_prompt(True):
        pkg.summarize(make_bundle(SHORT_BODY), on_llm, cfg, role_key="shipin")
    on = on_llm.requests[0]["messages"][0]["content"]

    extra = ps.extra_text().strip()
    assert extra, "附加提示词读不到（大概率管用 .txt 不在包里？），差值断言会空转"
    assert all(marker in on for marker in EXTRA_MARKERS), (
        "开关开着，shipin 的 system 里没有附加提示词的特征串")
    assert not any(marker in off for marker in EXTRA_MARKERS), (
        "默认档就把附加提示词烘进了 shipin 的 system——开关对它失效")
    assert len(on) - len(off) == len(extra) + 1, (
        f"差值不是「附加全文 + 1 个换行」：{len(on)} - {len(off)} vs {len(extra)} + 1")
    assert on.rstrip().endswith(bs_roles.OUTPUT_CONTRACT.strip()), (
        "开了附加提示词之后契约不在末尾了——那份是对抗性双角色，正要靠契约压住")
