"""`renhua`（人话）吃 `视频总结.txt` 之后的常驻判据：三轮成本、契约优先、位置约束。

## 文件名保留 `shipin` 的原因（改写不删，task-32 合同第 4 组）

`shipin` 是 task-24 那个「WB 权威层自造的单轮角色」。task-31 按用户决定把它**合并进
`renhua`** 并退役（`RETIRED_ROLES` 现在含 `shipin`），这套判据的主语随之从「新加的单轮角色」
变成「吃到 `视频总结.txt` 的那个三轮角色」。合同要求**改写不删**，我就地重写并保留文件名：
删一个已被 git 跟踪的文件，会在别人的工作树里留下 `D`，而还原逻辑对「开工前存在、现在不在」
的处理方式不是我一个人能定的（本仓库真踩过按快照回写抹掉同事改动的事故）。
改名要等 lead 统一处理，不在测试交付里顺手做。

配套产品：`video_workbench/core/summary/__init__.py`（权威层角色注册与 `RETIRED_ROLES`）、
`video_workbench/core/prompt_source.py` 的 `MAP`（现读：`renhua → 视频总结.txt`）。
对账维度（WB 与 bs 除 system 正文外逐维度一致）在 `test_summary_parity.py`，
真源绑定与回落机制在 `test_summary_prompt_source.py`，
退役键的历史行降级在 `test_summary_retired_roles.py`。
**这个文件只管三件事：`renhua` 的成本形状、契约的位置、副本不许被顺手改。**

## 判据随合并发生的变化（不是放宽，是主语换了）

1. **删掉「单轮成本」那组**。它唯一存在的理由是「`shipin` 是便宜的档」；合并后没有单轮角色，
   再钉「恰好一次请求」就是在要求产品保留一个已被用户决定退役的档。
   取而代之钉 `renhua.passes == 3` **并且真发三次**（门在
   `core/summary/summarizer.py:572` 的 `if role.passes >= 3 and role.critique and role.revise:`，
   字段对、门没生效是最可能的坏法）。
2. **一篇 txt 换角色的成本被单独量出来**：`视频总结.txt` 正文 511 字，而 `renhua` 原来内置的
   压缩版正文 1508 字 ⇒ 合并后「人话」这一档的方法论篇幅缩到约三分之一。
   这条登记在 `wb_prompt_sizes.py`，本文件用 `test_renhua_now_ships_the_shorter_txt_prompt`
   把它钉成**会红的断言**：哪天有人往那篇 txt 补内容补过 1508 字，或者反过来把压缩版塞回去，
   这条都要说话，不许静静跟着变。
3. **「system 必须以 OUTPUT_CONTRACT 结尾」仍不能按字面写在 `role.system` 上**：
   实测有源角色的 `role.system` 就是那篇 txt 全文，契约是**构建请求时**才接上去的
   （副本 `summarizer.py:518`：先 `role.system.strip()`，再接 `roles.OUTPUT_CONTRACT`）。
   所以判据落在「请求里的 system」这一层（`FakeLLM` 记录的那份）——那才是发给模型的东西。
4. **角色总数一律现读**。本文件不出现「6 个」「7 个」「8 个」这种字面量：
   公开面从 `ROLE_KEYS` 读，副本那 8 个从 bs 的 `ALL_ROLES` 读，两处一比就红。

## 牙齿（四条，各自只红它该红的那条）

全在 `%TEMP%` 下自己带哨兵名的目录里做：一个一次性 pytest 插件（`-p`，运行时改模块属性）。
**禁止对仓库工作区文件按快照覆盖回写**——上一轮就是「按快照回写」把 vendor 刚落地的改动抹过一次。

* **R-1 把 `renhua` 的 `passes` 偷改成 1**（并清掉 `critique`/`revise`）→
  只红 `test_renhua_pays_three_real_calls_and_every_public_role_still_does`。
  `Role` 是普通 `@dataclass`（`bili_scribe/core/roles.py:36`，实测 `frozen=False`），
  改权威层那个对象的字段就等于改了 `:572` 那道门。
* **R-2 让真源静默不进请求**（把 `renhua` 的 `system` 换成压缩版底线，等价于 `bind()` 少绑这一个）→
  红 `test_renhua_request_puts_the_contract_last_after_the_full_txt`（开头不再是 txt）。
  刻意不打 `role.system == txt` 那种「两边同步变化」的断言——那种断言在这一形下不红。
* **R-3 从副本 `roles.py` 里删掉 `xueshu` 的定义**（「顺手清理已停用角色」）→
  红 `test_retired_keys_stay_defined_in_the_copy_and_absent_from_the_public_surface`
  以及漂移锁。**这条是本文件存在的另一半理由**：删掉它，下一个人在副本里删 `xueshu`
  时漂移锁会红，但红的信息是「副本 != 原件」，没人会说出「这是已停用角色，本来就不该删」。
* **R-4 把 `OUTPUT_CONTRACT` 改一个字**（`覆盖上面任何与此冲突的要求` → `与上面的要求并列参考`）→
  红 2 条：`test_output_contract_is_byte_identical_on_both_sides_and_unchanged`（sha 登记）+
  请求侧那条（末尾不再是登记过的契约）。两侧一起改也照样红在 sha 上——
  漂移锁与「两侧相等」都盖不住同漂，只有登记值能。

本轮实跑：见交付里的命令原文与变体读数。
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
    three_pass_script,
)
from wb_parity_tiers import default_env  # noqa: F401  fixture：成本断言在默认档下跑
from wb_prompt_sizes import TXT_SHORTER_THAN_COMPRESSED

NL = chr(10)

REPO_ROOT = Path(__file__).resolve().parents[2]
WB_SUMMARY_DIR = REPO_ROOT / "video_workbench" / "core" / "summary"
PROMPT_DIR = REPO_ROOT / "video_workbench" / "resources" / "prompts"

#: 这套判据的主角。现读自 `ps.MAP`，不写死：`视频总结.txt` 的持有者是谁由
#: `test_summary_prompt_source.py` 的 P2 那张逐字表决定，本文件跟着它走，
#: 两处各写一遍归属名单就是第二个 owner。
def _holder_of_video_summary() -> str:
    pkg = _pkg()
    holders = [key for key in pkg.ROLE_KEYS if ps.MAP.get(key) == "视频总结.txt"]
    assert len(holders) == 1, (
        f"视频总结.txt 的公开持有者不是恰好一个，实得 {holders}："
        f"先看 P2 的绑定表，再决定这套判据的主语")
    return holders[0]


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
#: `renhua` 从此吃 `视频总结.txt`，而它原文里有「打印完整输出」这类指令，
#: 单轮还是三轮都靠这句把 JSON 形状保住。
#: 改它必须是有意的——更新这一行并在交付里点名。
REGISTERED_CONTRACT_SHA256 = (
    "e904f5eb2cf0cc835f2a6f4eaa7c7b38e5e3ff6877caac88977a5e546dc9c637")

#: 附加提示词的特征串（与 `test_summary_prompt_source.py` 同一组，那边管开关本身）。
EXTRA_MARKERS = ("奇美拉", "Guardian", "[lbk]")


def _pkg():
    return importlib.import_module("video_workbench.core.summary")


def _normalized(name: str) -> str:
    """包内 txt 的**现读**内容，只把 CRLF 归一（副本 `:518` 的 `.strip()` 在那之后）。

    判据要求「以 txt 全文开头」必须读文件本身，不许把提示词正文抄进用例——
    抄进用例就成了「改 txt 的同时改测试」，锁就没了。
    """
    return (PROMPT_DIR / name).read_text(encoding="utf-8").replace("\r\n", NL)


def _first_system(llm: FakeLLM) -> str:
    assert llm.requests, "假 LLM 一次请求都没记下来，这条会变成空断言"
    return llm.requests[0]["messages"][0]["content"]


# ------------------------------------------ 判据 1：成本形状（合并之后它是三轮角色）


def test_renhua_pays_three_real_calls_and_every_public_role_still_does(default_env):
    """`renhua` 与公开面里每个角色都必须真发三轮（生成→自我批判→修订）。

    钉的是**实际请求次数**，不是 `passes` 字段——三轮那道门是
    `core/summary/summarizer.py:572` 的
    `if role.passes >= 3 and role.critique and role.revise:`，
    三个条件里任何一个被「顺手」改掉，成本就变了而字段还写着 3。

    合并 `shipin` 之后公开面**一个单轮角色都不剩**：这就是「每次都付三轮」这个决定的代价，
    所以这条把整个公开面逐个跑一遍，而不是只跑 renhua——
    只跑主角会漏掉「别人被顺手降成单轮」那一半。
    阶段顺序的可执行指纹 = 三次请求的 `json_mode` 序列 `(True, False, True)`。
    """
    pkg = _pkg()
    cfg = make_cfg()
    subject = _holder_of_video_summary()
    assert subject in pkg.ROLE_KEYS, f"{subject} 不在公开面：它被退役了？"

    runs = []
    for role_key in pkg.ROLE_KEYS:
        llm = FakeLLM(three_pass_script())
        summary = pkg.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key=role_key)
        role = pkg.get_role(role_key)
        assert role.passes == 3, (
            f"{role_key} 的 passes 掉到 {role.passes}：三轮档被降了"
            f"（用户选了它却要等三轮，或反过来以为只花一轮）")
        assert len(llm.requests) == 3, (
            f"{role_key} 实际只发 {len(llm.requests)} 次，字段写着 3 轮但门没生效："
            f"{[r['json_mode'] for r in llm.requests]}")
        assert [r["json_mode"] for r in llm.requests] == [True, False, True], (
            f"{role_key} 的三轮顺序/形状变了：阶段被换或某一轮没开 JSON")
        assert summary.to_dict()["one_line"], f"{role_key}：三轮跑出来的东西是空的"
        runs.append(role_key)
    assert subject in runs and len(runs) == len(tuple(pkg.ROLE_KEYS)), (
        f"公开面没有全跑一遍：跑了 {runs}，清单 {tuple(pkg.ROLE_KEYS)}")


def test_renhua_now_ships_the_shorter_txt_prompt(default_env):
    """合并的**成本可见形式**：`renhua` 现在吃的 txt 全文比它原来的压缩版短（511 vs 1508 正文）。

    这条不是「为了绿而写的形状断言」，它钉的是用户看得见的一件事：
    task-31 之后「人话」这一档的方法论正文缩到约三分之一。
    登记表在 `wb_prompt_sizes.TXT_SHORTER_THAN_COMPRESSED`（唯一 owner，plan A 也用它）。
    两种改动都会让这条红：
      * 有人往 `视频总结.txt` 补内容补过压缩版 ⇒ 登记该删掉，红着提醒；
      * 有人把 `renhua` 换回内置压缩版 ⇒ 「txt 全文在前」那条先红，这条跟着红。
    参照物**不取本文件里另一个量**：长度两边都现读（txt 文件本身 / bs 原件的角色表），
    任何一侧被改，比较的另一侧不会跟着变——这就是它不是自己考自己的原因。
    """
    name = "视频总结.txt"
    assert name in TXT_SHORTER_THAN_COMPRESSED, (
        "登记表被删了：renhua 这条比较失去方向，请连同 P8/plan A 一起改")
    txt = _normalized(name).strip()
    assert txt, f"{name} 读不到，下面三条断言都会空转"

    pkg = _pkg()
    subject = _holder_of_video_summary()
    cfg = make_cfg()
    llm = FakeLLM(three_pass_script())
    pkg.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key=subject)
    system = _first_system(llm)
    bs_compressed = bs_roles.get_role(subject).system

    assert len(system) < len(bs_compressed.strip() + NL + bs_roles.OUTPUT_CONTRACT), (
        f"{subject} 的请求侧 system 不再比 bs 那份请求侧短（{len(system)} vs "
        f"{len(bs_compressed.strip() + NL + bs_roles.OUTPUT_CONTRACT)}）："
        f"要么 {name} 被补长了（那要同步删登记），要么绑定被换回了压缩版")
    assert len(txt) < len(bs_compressed.strip()), (
        f"正文层面也一样：txt {len(txt)} vs 压缩版 {len(bs_compressed.strip())}")
    # 参照物的独立自检：bs 那位角色的 system 必须仍是内置文本，不是那篇 txt。
    assert bs_compressed.strip() != txt, (
        "bs 原件的 renhua 被换成了 txt 全文——A 的前提（bs 继续发压缩版）被推翻")


# ---------------------------- 判据 2：契约优先（落在请求层，见文件头偏差第 3 条）


def test_renhua_request_puts_the_contract_last_after_the_full_txt(default_env):
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
    subject = _holder_of_video_summary()
    llm = FakeLLM(three_pass_script())
    pkg.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key=subject)
    system = _first_system(llm)
    txt = _normalized(ps.MAP[subject]).strip()
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


# ------------------------------------------------- 判据 3：位置约束（副本不许被顺手改）


def test_retired_keys_stay_defined_in_the_copy_and_absent_from_the_public_surface():
    """已停用的键**必须继续定义在副本里**，同时不许出现在公开面。

    这条拦的是最省事也最容易被当成「清理」的做法：往逐字节副本 `roles.py` 里删角色。
    直觉上「WB 都停用 xueshu 了，副本里留着它干嘛」，但那正是漂移锁的前提——
    副本必须永远等于 bs 原件。少了这条，下一个人删掉 `xueshu` 时只会看到
    「副本 != 原件」这种红，没人说出「这是已停用角色，本来就该留在 vendored 里」。

    四层各查一遍：
      1. 副本运行期对象的 key 集合 == bs 的（含 `xueshu`，且不含 `shipin`）；
      2. 副本源文件里 `xueshu` 的定义仍在、`shipin` 字面量仍没有；
      3. bs 原件源文件里同样没有 `shipin`（合并是权威层的事，不许回写上源）；
      4. 权威层的公开面 `ROLE_KEYS` 里没有 `xueshu`，且自造段为空。
    """
    pkg = _pkg()
    copy_keys = tuple(role.key for role in pkg._roles.ALL_ROLES)
    bs_keys = tuple(role.key for role in bs_roles.ALL_ROLES)
    assert copy_keys == bs_keys, (
        f"逐字节副本的角色集与 bs 不同：副本 {copy_keys} vs bs {bs_keys}")
    assert "xueshu" in copy_keys, (
        f"副本的 ALL_ROLES 里没有 xueshu：已停用角色被从 vendored 文件里删了 {copy_keys}")
    assert "shipin" not in copy_keys, "shipin 进了副本的 ALL_ROLES——塞进 vendored 文件了"

    copy_source = (WB_SUMMARY_DIR / "roles.py").read_text(encoding="utf-8")
    assert '"xueshu"' in copy_source, "副本 roles.py 源码里 xueshu 的定义没了"
    assert "shipin" not in copy_source, "副本 roles.py 源码里出现 shipin"
    bs_source = (REPO_ROOT / "bili_scribe" / "core" / "roles.py").read_text(encoding="utf-8")
    assert "shipin" not in bs_source, "bs 的 roles.py 里出现 shipin（越界到上游了）"

    assert "xueshu" not in tuple(pkg.ROLE_KEYS), (
        "已停用的 xueshu 还在公开面：用户选了它就拿不到总结")
    # 权威层自造段现在**必须为空**（task-31 把唯一的自造角色合并掉了）。
    # 留着这条不是为了纪念：谁再造一个角色，必须先来决定它吃不吃 txt、
    # 以及 `test_summary_prompt_source.py` 那张绑定表怎么改。
    wb_only = tuple(key for key in pkg.ROLE_KEYS if key not in set(bs_keys))
    assert wb_only == (), (
        f"权威层又出现 bs 不认的角色 {wb_only}：合并已完成，重新自造要有显式决定")


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


# ------------------------------------------------ 判据 4：附加提示词对它同样生效


def test_the_extra_prompt_wraps_the_txt_holder_too(default_env):
    """`视频总结.txt` 的持有者吃不吃「大概率管用 .txt」必须与别的角色**一致**。

    合并之后 `renhua` 成了「最晚拿到真源的那位」，最容易被漏在拼接表里——
    漏了不会报错，只是别人开开关时这个角色仍按旧方法论走。
    成本按**不变量**钉（差值 == 附加全文 + 1 个换行），不抄字面长度：
    txt 与附加文件都是用户手写的，字数会漂，差值关系不会。
    """
    pkg = _pkg()
    cfg = make_cfg()
    subject = _holder_of_video_summary()

    off_llm = FakeLLM(three_pass_script())
    pkg.summarize(make_bundle(SHORT_BODY), off_llm, cfg, role_key=subject)
    off = _first_system(off_llm)

    on_llm = FakeLLM(three_pass_script())
    with ps.use_extra_prompt(True):
        pkg.summarize(make_bundle(SHORT_BODY), on_llm, cfg, role_key=subject)
    on = _first_system(on_llm)

    extra = ps.extra_text().strip()
    assert extra, "附加提示词读不到（大概率管用 .txt 不在包里？），差值断言会空转"
    assert all(marker in on for marker in EXTRA_MARKERS), (
        f"开关开着，{subject} 的 system 里没有附加提示词的特征串")
    assert not any(marker in off for marker in EXTRA_MARKERS), (
        f"默认档就把附加提示词烘进了 {subject} 的 system——开关对它失效")
    assert len(on) - len(off) == len(extra) + 1, (
        f"差值不是「附加全文 + 1 个换行」：{len(on)} - {len(off)} vs {len(extra)} + 1")
    assert on.rstrip().endswith(bs_roles.OUTPUT_CONTRACT.strip()), (
        "开了附加提示词之后契约不在末尾了——那份是对抗性双角色，正要靠契约压住")
