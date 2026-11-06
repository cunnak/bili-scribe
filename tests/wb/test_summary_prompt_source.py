"""角色提示词真源（方案 A）的常驻用例：包内 txt 全文 + 按请求生效的附加开关。

配套产品：`video_workbench/core/prompt_source.py`（task-17，接口已冻结）与
`video_workbench/core/summary/__init__.py` 的导入时 `capture_baseline()` + `bind()` +
`_roles.get_role = _bound_get_role`。对账口径的变更在 `test_summary_parity.py`（那边管
「WB 与 bs 除了 system 正文以外必须逐维度一致」，这个文件管「system 正文这一处到底
是谁、怎么拼、缺了怎么办、开关怎么切」）。

## 为什么这一层值得单独一套用例

用户手写的提示词是**产品定义的方法论真源**，而 `core/summary/roles.py` 是 bili_scribe
的逐字节副本（`test_summary_drift.py` 按字节钉死，一个字不许改），里面的 system 是
省 token 的压缩版。两边同时成立只有一个办法：副本不动，绑定发生在权威层。
这意味着「绑定」这件事本身成了新的失败面，而且它的失败方式大多是**静默**的：

* 某个 txt 读不到 → 如果实现图省事留个空串，副本 `summarizer.py:517`
  （`if role.system.strip(): system_prompt = ... else: SYSTEM_PROMPT`）会走**无角色
  路径**，等于悄悄把方法论换成通用编辑，界面上看着一切正常。这是最容易出事故的一条，
  所以 P4 要断的是「回落的东西 == 压缩版原文」而不是「非空」。
* 附加提示词（`大概率管用 .txt`，对抗性双角色 `[lbk]Guardian[rbk]`/`[lbk]Maverick[rbk]`）
  与 JSON 输出契约正面冲突，默认关。要是有人把它在 `bind()` 时烘进 `role.system`，
  开关就永久失效而没人看得出来 —— P6 的第二条就是抓这个的。
* 契约必须在**最末尾**。txt 原文里写着「必须打印出每个阶段的完整输出」「输出 ReAct
  执行日志」这类指令，`roles.py:52-56` 的 OUTPUT_CONTRACT 正是来压住它们的；顺序调反，
  模型就把思考打进正文，`_extract_json` 当场坏（副本 `summarizer.py:513-518` 的注释
  也明说了这一点）。

## 口径来自哪里（不自己发明）

`Role.prompt_source` 是**动态属性**（`Role` 是 dataclass，但 roles.py 被逐字节锁住、
加不了字段），值就是相对包内目录的 txt 文件名**逐字**（含 `大概率管用 .txt` 那个空格与
`首席思想精炼师：……。.txt` 的全角标点）；没有真源的角色必须是 `None`——**空串算红**，
因为空串在界面上会被读成「有文件，只是文件名丢了」。
`bind()` 的返回形状固定为 `{"bound": [...], "missing_source": [...], "kept_compressed": [...]}`
三个键，P4 只断这个返回值和角色对象状态，**日志文本不作判据**。
缺文件的场景走产品自己的环境变量覆盖口 `WB_PROMPTS_DIR`（每次现读，与 `WB_WEB_ROOT`
同风格），在 Temp 里造一个缺文件的目录做**真实**回落，不许退化成 monkeypatch 函数。

## 牙齿（合同 rev3 那五条，改坏必须红）

* **V-1 改一个 txt 字节**：往任一被绑 txt 末尾追加一个字符 →
  `test_all_six_prompt_files_are_present_with_their_recorded_hashes` 红（sha/字节数）。
  注意红的是**登记表**而不是「role.system == txt 逐字」那条：pytest 每次是全新进程，
  `bind()` 在读到改过的 txt 之后已经把 role 也换成改过的内容，两边照样相等——
  所以「txt 被悄悄改一个字」这件事**只有 sha 登记表能发现**。这条就是 P1 存在的理由。
* **V-2 从 MAP 删一个绑定**（删掉 `"fenxi": "分析报告.txt",` 那一行）→
  `test_the_source_map_covers_exactly_four_roles_and_names_files_verbatim`、
  `test_bind_returns_the_three_buckets_and_is_byte_identical_when_repeated`、
  `test_manifest_reports_every_named_file_including_the_lost_ones`、以及 parity 的
  `test_plan_a_changes_the_system_of_exactly_the_sourced_roles` 一起红。
* **V-3 悄悄把无源角色的 system 设空**（`bind()` 的无源分支里加 `role.system = ""`）→
  `test_the_unsourced_roles_still_read_the_compressed_version_when_off` 红——那条就是为它写的。
* **V-4 附加提示词默认改开**（`_extra_on` 的 default 翻 True）→
  `test_the_extra_prompt_is_absent_by_default` 与 parity 里「默认关不许出现特征串」的断言红。
* **V-5 第三处写 `role.system =`**（在适配层 `core/summarizer.py` 加一行自赋值）→
  `test_only_the_prompt_source_layer_writes_role_system` 红，且报错点名新文件的行号。

V-1/V-3/V-4/V-5 要临时改产品文件（lead 的显式例外）：开工前把每个要碰的文件读成字节做
**sha256 快照清单**，跑完按快照逐字节覆盖回写并复验 sha 一致。**不许用 `git checkout`**——
`video_workbench/core/summary/`、6 个 txt、`prompt_source.py` 都还是未跟踪或带 vendor
未提交改动的状态，checkout 会把他刚落地的东西抹掉（原合同的「git diff 前后一致」在这条
任务上结构上不成立，rev3 已换成 sha 快照口径）。

### 实测输出（脚本 `C:/Users/b1596/AppData/Local/Temp/teeth_task18_rev3.py`）

控制组（未改坏）`92 passed`；五条逐个改坏的红数：
**V-1 红 3**（`…recorded_hashes` / `manifest…` / `…extra_prompt_is_absent_by_default`）、
**V-2 红 6**（MAP 等式、bind 三桶、单文件回落、manifest、默认关、parity 的
`plan_a_changes_the_system_of_exactly_the_four_sourced_roles`）、
**V-3 红 9**（为它写的 `…unsourced_roles_still_read_the_compressed_version_when_off` +
parity 里无源三角色的 `copy_and_original_agree…`/`three_pass_gate…` 六条 +
`…extra_applies_to_every_role…` + parity 的 A 边界那条）、
**V-4 红 24**（默认档对账整体炸掉：16 条 parity 角色对账 + 本文件 8 条开关语义）、
**V-5 红 1**（`test_only_the_prompt_source_layer_writes_role_system`，报错点名新写入点行号）。

还原后三个文件的 sha256 与开工前**逐字节一致**（各 64 位全值，取前 16 位示意）：
`prompt_source.py f7dfb7180bb9043d…`、`core/summarizer.py 43a35b4d0e831692…`、
`resources/prompts/分析报告.txt 7837f4da54d2937f…`；复跑 `92 passed`。
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import shutil
import threading
from pathlib import Path

import pytest
from test_summary_parity import (  # 同目录、无 __init__.py，直接复用夹具与假 LLM
    SHORT_BODY,
    FakeLLM,
    QuietCtx,
    make_bundle,
    make_cfg,
    three_pass_script,
)
from wb_parity_tiers import default_env as default_env  # noqa: F401  fixture，P9 那组显式钉默认档
from wb_prompt_sizes import TXT_SHORTER_THAN_COMPRESSED  # 长度方向登记的唯一 owner

import video_workbench.core.summary as wb_summary
from bili_scribe.core import roles as bs_roles
from bili_scribe.core import summarizer as bs_summarizer
from video_workbench.core import prompt_source as ps

MAP_SYSTEM = bs_summarizer.MAP_SYSTEM

#: 副本 `summarizer.py:518` 拼接 system 与 OUTPUT_CONTRACT 用的那个换行。
#: 定成常量是因为「手敲一个转义符」在对齐字面量时最容易看错（它要么是 `\n` 要么是 `\\n`）。
NL = chr(10)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "video_workbench" / "resources" / "prompts"

# ------------------------------------------------------------------ P1：六篇 txt 的身份

#: 用户手写的原文，随包发布。sha 变了就意味着真源被改过——那必须是一次有意的、
#: 登记过的改动（更新登记表并在交付里点名），不是「顺手润色」。
#: 元组 = (文件名, 字节数, sha256 全串)。
PROMPT_FILES: tuple[tuple[str, int, str], ...] = (
    ("写作专家.txt", 6941, "6b079cf4e338cc43259fd61188350d68883655bd72ceb9e9441b404d5c824b35"),
    ("分析报告.txt", 4366, "7837f4da54d2937f7e1d6db267b5ac523a94efdbf624f2d3bfc95d6fc17e6d85"),
    ("大概率管用 .txt", 2223, "4beb577f7438e243e24bae18f9f2dec5b9c69747739476f11ca0eda6711288be"),
    ("文稿编辑.txt", 8479, "9dae7c1075e5cb816a2553f02bfdb97e1c822553e390c6b5211c3c834c4873aa"),
    ("视频总结.txt", 1381, "26966b72b9238c9d7060d76d4e4f93c46c11167666141148ec8e6a78fc9de6e5"),
    (
        "首席思想精炼师：对于各种文本都处理得较为简洁的总结提示词。.txt",
        3224,
        "c02e4eef650d6123efe74b337a9bfc905bf901e2ccfdc7f9124e0352dfc94404",
    ),
)

#: 用户桌面上的原始目录（`视频总结.txt` 尚未绑给任何角色，等拍去向）。
#: 这个目录在不在、文件全不全**都不是产品缺陷**，所以只 skip，不判红。
DESK_SOURCE = Path(r"C:\Users\b1596\Desktop\提示词")

#: 附加提示词的特征串（对抗性双角色的两个角色名与分隔标记）。默认关时都不许出现。
EXTRA_MARKERS = ("奇美拉", "Guardian", "[lbk]")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_txt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8", errors="replace")


def _bs_known_role_keys() -> set:
    """bs 角色表里真实存在的 key 集合。

    有个坑专门用它来躲：bs 的 `get_role()` 对**未知 key 静默回落到 standard**，
    而 `standard.system` 是空串。task-24 之后 WB 有了 bs 没有的角色（`shipin`），
    任何「WB 的角色应当等于 bs 的压缩版」这类断言，对 WB-only 角色都会变成
    「断言它等于空串」——回落保护反过来被写成回落要求。凡拿 bs 当基准的比较，
    都先用这个集合把范围限定到共有角色。
    """
    return {role.key for role in bs_roles.ALL_ROLES}


def test_all_six_prompt_files_are_present_with_their_recorded_hashes():
    """P1：包内 6 个 txt **存在**，且文件名与 sha256 逐字对得上登记表。

    写成「每个点名的文件都必须找到、并且 sha 与字节数都对上」的存在性检查（lead 要求），
    而不是「先 glob 再逐个比」——后者会把「文件丢了」显示成「没有这个文件」，
    清单少一行也没人喊。目录里多出未登记的文件同样要红：那意味着有人往真源里塞东西。
    """
    assert PROMPT_DIR.is_dir(), f"提示词目录不在：{PROMPT_DIR}"
    for name, size, digest in PROMPT_FILES:
        path = PROMPT_DIR / name
        assert path.is_file(), f"真源缺文件：{name}"
        assert path.stat().st_size == size, (
            f"{name} 字节数不是登记的 {size}，实得 {path.stat().st_size}"
        )
        assert _sha256(path) == digest, (
            f"{name} 的 sha256 变了——用户手写的提示词被改过。要么是有意的（那就更新"
            f"登记表并在交付里点名），要么是意外（那必须回滚）"
        )
    found = sorted(p.name for p in PROMPT_DIR.glob("*.txt") if p.is_file())
    assert found == sorted(n for n, _s, _d in PROMPT_FILES), (
        f"目录里的 txt 与登记表不一致：实得 {found}"
    )
    # 目录里另有 7 个 `.md`（提示词笔记），不参与绑定，所以计数只限 *.txt。


def test_the_desk_source_folder_still_matches_the_packaged_prompts():
    """源侧同步锁：桌面 `提示词` 里的同名文件与包内发布件**逐字节一致**。

    这条防的是「用户改了原文，发布件忘了同步」。它**不是**「必须存在」的检查：
    那台机器不在、目录被搬走、某篇被删，都属正常环境差异 → `pytest.skip` 并写清
    没验到什么（skip 不是通过）。反过来，只要目录在而内容不一致，就必须红。
    """
    if not DESK_SOURCE.is_dir():
        pytest.skip(f"源侧目录不在这台机器上（{DESK_SOURCE}），同步锁没跑（不是通过）")
    checked = 0
    for name, _size, digest in PROMPT_FILES:
        desk = DESK_SOURCE / name
        if not desk.is_file():
            pytest.skip(f"源侧缺 {name}（可能已改名或被清理），这一篇没比对")
        assert _sha256(desk) == digest, (
            f"{name}：桌面原文与包内发布件不是同一份——发布件落后了，或包内被改过"
        )
        checked += 1
    assert checked == len(PROMPT_FILES), "一条都没比对成功，这条就成了空断言"


# ------------------------------------------------------------------ P2：覆盖度与 prompt_source


def test_the_source_map_covers_the_public_roles_and_names_files_verbatim():
    """P2 之一：MAP 的绑定表逐字钉死（文件名不许被「顺手规范化」）。

    `大概率管用 .txt` 的 `.txt` 前有一个空格、`首席……。.txt` 带全角冒号与句号：
    任何一次「清理文件名」都会让绑定静默失效。**这张表是「谁绑哪篇」的唯一 owner**
    （对账那边 `test_summary_parity.py` 的 plan A 只要求「一篇 txt 恰好有一个公开持有者」，
    不重抄归属名单——两处各写一遍就是第二个 owner，本仓库栽过）。

    task-31 的形状变化：`shipin` 合并进 `renhua`，所以 `视频总结.txt` 现在绑的是一个
    **bs 也认识的共有角色**。这张表因此从 4 项变 5 项，而「WB 自造段」那半**整段删掉**
    ——不是放宽，是它没有主语了；改成一条负向断言：MAP 里不许再出现 bs 不认的键
    （谁把 `shipin` 造回来并顺手绑一篇 txt，就在这里红，逼他做显式决定）。

    再加三条覆盖不变量，它们比「等于某个字面量 dict」更能拦住未来的错法：
    每个公开角色要么有源要么明确无源（不许第三种状态）、`UNBOUND` 必须归零
    （`视频总结.txt` 已有归属，再冒出来就是包里落了没人认领的 txt）、
    附加提示词不许同时是某个角色的真源（否则它双份进请求）。
    """
    bs_keys = {role.key for role in bs_roles.ALL_ROLES}
    shared_bound = {key: name for key, name in ps.MAP.items() if key in bs_keys}
    assert shared_bound == {
        "renhua": "视频总结.txt",
        "wengao": "文稿编辑.txt",
        "xiezuo": "写作专家.txt",
        "fenxi": "分析报告.txt",
        "jinglian": JINGLIAN_FILE,
    }, "共有角色绑的文件变了：谁绑哪篇是真源问题，改它必须是有意的并同步这条"
    # 权威层已无自造角色：MAP 的键必须全是 bs 也认识的。
    assert not (set(ps.MAP) - bs_keys), (
        f"MAP 里出现了 bs 不认的角色 {sorted(set(ps.MAP) - bs_keys)}："
        f"重新自造角色要先决定它的回落基准与漂移锁，不是加一行绑定"
    )

    public = set(wb_summary.ROLE_KEYS)
    assert set(ps.MAP) | set(ps.NO_SOURCE) == public, (
        f"既不在 MAP 也不在 NO_SOURCE 的公共角色：{sorted(public - set(ps.MAP) - set(ps.NO_SOURCE))}"
        f"；名单外的绑定：{sorted((set(ps.MAP) | set(ps.NO_SOURCE)) - public)}"
    )
    assert not (set(ps.MAP) & set(ps.NO_SOURCE)), "同一个角色既算有源又算无源：绑定与压缩版会互相盖"
    assert tuple(ps.UNBOUND) == (), f"又出现没人认领的 txt：{tuple(ps.UNBOUND)}"
    # 无源名单**不再写死**（task-31 之后它只剩 `shensi` 一个，写死名单每次角色增删都要红一轮）。
    # 上面两条不变量已经把 NO_SOURCE 唯一确定：MAP ∪ NO_SOURCE == 公开面 且 两者不相交
    # ⇒ NO_SOURCE == 公开面 − MAP。这里只补一条防空转：无源段必须还有人，
    # 否则 P3「无源角色两侧逐字相同」那条比较就失去主语。
    assert ps.NO_SOURCE, "无源段空了：公开面全都有源，P3 那条「与 bs 逐字相同」的对照会静默空转"
    assert ps.EXTRA_FILE == "大概率管用 .txt"
    assert ps.EXTRA_FILE not in set(ps.MAP.values()), (
        "附加提示词被当成某个角色的真源：它会双份进请求"
    )
    for name in list(ps.MAP.values()) + [ps.EXTRA_FILE, *ps.UNBOUND]:
        assert (PROMPT_DIR / name).is_file(), f"点名的文件不在包里：{name!r}"


JINGLIAN_FILE = "首席思想精炼师：对于各种文本都处理得较为简洁的总结提示词。.txt"


def test_every_role_carries_its_source_filename_or_none_never_empty_string():
    """P2 之二：有源角色 `prompt_source` == MAP 里那个文件名；无源三角色必须是 `None`。

    **空串算红**：界面上 `""` 会被读成「有文件、名字丢了」，而 `None` 才是「这个角色
    没有 txt 真源、吃的是压缩版」。判据用 `is not None`（lead 定的口径），不用真值判断。
    另附一条防漂移：`Role` 是 dataclass 而 roles.py 被逐字节锁死，所以 prompt_source
    只能是**动态属性**——这条顺便确认它真挂在对象上，而不是只存在于某个 dict 里。
    """
    for role in wb_summary.ALL_ROLES:
        expected = ps.MAP.get(role.key)
        got = getattr(role, "prompt_source", "<没有这个属性>")
        assert got != "", f"{role.key}：prompt_source 是空串，界面上会被读成「文件名叫丢了」"
        if expected is None:
            assert got is None, f"{role.key}：无源角色的 prompt_source 必须是 None，实得 {got!r}"
        else:
            assert got == expected, f"{role.key}：应绑 {expected!r}，实得 {got!r}"
    # describe_roles 走的是同一份 info_for，界面读的是它——两边不许分叉。
    rows = {row["key"]: row for row in wb_summary.describe_roles()}
    for role in wb_summary.ALL_ROLES:
        assert rows[role.key]["prompt_source"] == (ps.MAP.get(role.key) or None)


def test_a_bound_role_system_is_the_txt_bytes_not_the_compressed_copy():
    """绑定确实发生过：`role.system` == txt **逐字原文**（不清洗），且 != 压缩版。

    `prompt_source.load()` 的文档写明「除调用方按副本 :518 的既有形状做的 `.strip()`
    以外不做任何清洗」——清洗一次，用户写的提示词就不再是他写的那一份。
    这条同时是「`__init__.py` 的导入时 bind 被删掉」的最早报警：那时 system 会退回
    压缩版，而两侧对账（`test_summary_parity.py` 的 A 组）也才刚好看出来。
    """
    for key, name in ps.MAP.items():
        role = wb_summary._roles.get_role(key)  # 权威层对象本体（未过 view_for）
        raw = _read_txt(name)
        assert role.system == raw, f"{key}：system 不是 txt 逐字原文（被清洗过或没绑上）"
        if key in _bs_known_role_keys():
            # 只对有对侧的角色比。WB 自造的角色在 bs 侧是「未知 key 静默回落 standard」，
            # 拿空串当参照做不等比较是平凡真——那半句「绑过之后两侧必须不同」对它没意义。
            assert role.system != bs_roles.get_role(key).system, (
                f"{key}：绑过一次之后 WB 与 bs 的 system 居然相同——绑定失效"
            )


# ------------------------------------------------------------------ P3：bind() 的返回形状与幂等


def test_bind_returns_the_three_buckets_and_is_byte_identical_when_repeated():
    """P3：`bind()` 返回三个固定键；绑两次与绑一次**逐字节相同**（赋值不是拼接）。

    幂等这条为什么值得钉：绑定发生在导入时，而 job 重放、测试夹具、P4 的回落恢复都会
    再调一次 `bind()`。如果实现写成 `role.system += txt`，第二次就出现两份全文，
    token 直接翻倍而输出仍「像对的」——只有逐字节比才抓得住。
    """
    first = ps.bind(wb_summary._roles)
    assert set(first) == {"bound", "missing_source", "kept_compressed"}, (
        f"bind() 的返回键不是合同那三个：{sorted(first)}"
    )
    after_first = {r.key: r.system for r in wb_summary.ALL_ROLES}
    second = ps.bind(wb_summary._roles)
    after_second = {r.key: r.system for r in wb_summary.ALL_ROLES}
    assert second == first, f"两次 bind 的清单不同：{first} vs {second}"
    assert after_second == after_first, "两次 bind 之后某个角色的 system 变了——不是幂等"
    assert sorted(first["bound"]) == sorted(ps.MAP), (
        f"bound 桶应当是四个有源角色，实得 {first['bound']}"
    )
    assert sorted(first["kept_compressed"]) == sorted(ps.NO_SOURCE)
    assert first["missing_source"] == [], "包内文件齐全时不该有回落，有就是丢文件了"


# ------------------------------------------------------------------ P4：缺文件的真实回落（最易出事故）


def test_missing_prompt_files_fall_back_to_the_compressed_version_not_to_empty(
    monkeypatch, tmp_path
):
    """P4：`WB_PROMPTS_DIR` 指向空目录 → 四个有源角色**回到压缩版**，绝不许留空串。

    空 system 的代价不是「少一句提示词」：副本 `summarizer.py:517` 对空 system 走
    `SYSTEM_PROMPT` 那条**无角色**路径，等于悄悄把方法论换成通用编辑，用户以为选了
    「文稿编辑」，实际吃的根本不是它。这是 A 最容易出事故的一条。

    回落走产品自己的覆盖口（每次现读），不是 monkeypatch 函数——测的就是「换目录」
    这个真实动作会发生什么。结束时撤掉环境变量并用 snapshot/restore 回写，
    不给别的用例留脏状态。
    """
    empty = tmp_path / "prompts-missing"
    empty.mkdir()
    before = ps.snapshot(wb_summary._roles)
    try:
        monkeypatch.setenv(ps.PROMPTS_DIR_ENV, str(empty))
        assert ps.prompt_dir() == empty, "WB_PROMPTS_DIR 没被现读（import 期缓存了？）"
        assert ps.load("文稿编辑.txt") == "", "缺文件时 load() 必须返回空串（不许抛）"
        result = ps.bind(wb_summary._roles)
        assert sorted(result["missing_source"]) == sorted(ps.MAP), (
            f"四个有源角色都该报缺文件，实得 {result['missing_source']}"
        )
        assert result["bound"] == []
        assert sorted(result["kept_compressed"]) == sorted(wb_summary.ROLE_KEYS), (
            "回落的角色必须同时算「仍在用压缩版」，界面才看得出没生效"
        )
        for key in ps.MAP:
            role = wb_summary._roles.get_role(key)
            assert role.system, f"{key}：缺文件回落成了**空 system**——副本会走无角色路径"
            assert role.prompt_source is None, f"{key}：缺文件却还挂着真源文件名"
            if key not in _bs_known_role_keys():
                # 这条分支原本管的是 **WB 自造的角色**（`shipin`）：bs 的 `get_role("shipin")`
                # 会静默回落到 `standard`，而 `standard.system` 是空串——照共有角色那样比，
                # 这条就成了「断言回落成空 system」，恰好是它要防的那件事。
                # task-31 合并之后 MAP 里没有这样的键了，分支不再被走到。留着一个永远
                # 不执行的分支等于留一条假绿，所以改成**触发线**：谁再造一个带真源的
                # 自造角色，必须先回来决定它的回落基准（权威层注册的压缩底线
                # `COMMON_BOTTOM_LINE`，见 `core/summary/__init__.py` 的注册注释
                # 「不能留空串」，空串会让副本走无角色的 `SYSTEM_PROMPT` 路径）。
                pytest.fail(
                    f"MAP 里出现 bs 不认的角色 {key}：WB 已无自造角色，这条回落判据的 "
                    f"WB-only 分支失去主语。要恢复自造角色，先在这里补它的回落基准。"
                )
            assert role.system == bs_roles.get_role(key).system, (
                f"{key}：回落的东西不是压缩版原文（方法论被换了）"
            )
        # 回落之后仍然要能正常生成，并且请求里带的是「压缩版 + 契约」，不是 SYSTEM_PROMPT。
        cfg = make_cfg()
        # 原来这里只验 `wengao` 一个，另外单独给 WB 自造的 `shipin` 补了一条「底线 + 契约」。
        # 自造角色没了之后那条没有主语，改成**每个有源角色都验一遍**：覆盖面比原来宽，
        # 而不是少了一个角色就少一类形状。
        for key in ps.MAP:
            llm = FakeLLM(three_pass_script())
            wb_summary.summarize(make_bundle(SHORT_BODY), llm, cfg, role_key=key)
            expected = bs_roles.get_role(key).system.strip() + "\n" + bs_roles.OUTPUT_CONTRACT
            assert llm.requests[0]["messages"][0]["content"] == expected, (
                f"{key} 缺文件后的请求不是「压缩版 + 契约」——它走了无角色路径"
            )
    finally:
        monkeypatch.delenv(ps.PROMPTS_DIR_ENV, raising=False)
        ps.restore(wb_summary._roles, before)
    # 还原自检：快照没写回就等于给整个测试会话留了脏状态。
    for key, name in ps.MAP.items():
        role = wb_summary._roles.get_role(key)
        assert role.system == _read_txt(name), f"{key} 没还原回 txt 全文"
        assert role.prompt_source == name, f"{key} 的 prompt_source 没还原"


def test_a_single_missing_file_only_drops_that_one_role(monkeypatch, tmp_path):
    """P4 的另一半：只少一篇时，**只有那个角色**回落，其余三个照绑。

    这条防的是「一个文件读不到就把整批清空」那种一刀切实现——用户会莫名其妙发现
    另外三个角色的方法论也没了。
    """
    staging = tmp_path / "prompts-partial"
    staging.mkdir()
    missing_key, missing_name = "fenxi", ps.MAP["fenxi"]
    for name, _size, _digest in PROMPT_FILES:
        if name == missing_name:
            continue
        shutil.copy2(PROMPT_DIR / name, staging / name)
    before = ps.snapshot(wb_summary._roles)
    try:
        monkeypatch.setenv(ps.PROMPTS_DIR_ENV, str(staging))
        result = ps.bind(wb_summary._roles)
        assert result["missing_source"] == [missing_key], (
            f"只该 {missing_key} 回落，实得 {result['missing_source']}"
        )
        assert sorted(result["bound"]) == sorted(set(ps.MAP) - {missing_key})
        for key in set(ps.MAP) - {missing_key}:
            role = wb_summary._roles.get_role(key)
            assert role.prompt_source == ps.MAP[key]
            assert role.system == _read_txt(ps.MAP[key])
        role = wb_summary._roles.get_role(missing_key)
        assert role.system == bs_roles.get_role(missing_key).system
        assert role.prompt_source is None
    finally:
        monkeypatch.delenv(ps.PROMPTS_DIR_ENV, raising=False)
        ps.restore(wb_summary._roles, before)


# ------------------------------------------------------------------ P5：拼接顺序（定死，不许调）


def test_the_extra_applies_to_every_role_with_a_non_empty_system_including_unsourced():
    """附加提示词的作用面 = **所有 system 非空的角色**，无源的那些也一样吃；只有 standard 不吃。

    这是按实测登记（开与关各差 993 字，有源/无源都一样；standard 的 base 是空串，
    被 `system_for` 里的 `base.strip()` 挡掉）。为什么值得钉：
    「附加提示词」是**方法论之外的通用增益**，与有没有 txt 真源无关；而 standard 是唯一
    「没有方法论」的角色，给它接附加等于凭空造出一个方法论。
    遍历域现读自 `ROLE_KEYS`（task-31 把 `xueshu` 退役之后它自动退出——这条原来写死四个
    名字，里面就有那个已停用的学术角色）。
    如果 vendor 认为无源角色不该吃附加，要改的就是这一条——改之前得先回答
    「那 `llm.extra_prompt` 对「人话」「深思」到底生不生效」。
    """
    off = {}
    # 遍历域现读 = 公开面里 system 非空的角色（task-31 之后 6 个，`xueshu` 退役就自动退出）。
    # 原来写死 ("shensi","xueshu","renhua","wengao")，那份名单把退役角色当公开角色遍历，
    # 于是「已停用」的键越积越多、名单越来越假。
    subjects = [key for key in wb_summary.ROLE_KEYS if wb_summary._roles.get_role(key).system]
    assert len(subjects) >= 2, (
        f"公开面里 system 非空的角色只剩 {subjects}：这条比较会空转，先确认是不是绑定被清空了"
    )
    with ps.use_extra_prompt(False):
        for key in subjects:
            off[key] = ps.system_for(key, wb_summary._roles.get_role(key).system)
    with ps.use_extra_prompt(True):
        for key in subjects:
            on = ps.system_for(key, wb_summary._roles.get_role(key).system)
            assert on != off[key], f"{key}：开着附加却没生效"
            assert on.startswith(off[key][:40]), f"{key}：附加不该换掉开头的方法论本体"
            assert any(marker in on for marker in EXTRA_MARKERS), f"{key}：没接进附加"
        standard_base = bs_roles.get_role("standard").system
        assert ps.system_for("standard", standard_base) == standard_base, (
            "standard 是唯一没有方法论的角色，给它接附加等于凭空造方法论"
        )


def test_extra_prompt_sits_between_the_txt_and_the_output_contract():
    """P5：开关**开**时的顺序 = txt 全文 → 附加提示词 → 输出契约（契约永远最后）。

    三条规则各断一次，不写「等于某段拼好的字符串」：拼出来的字面串会跟着 txt 内容
    漂移，规则不会。契约在末尾的理由写在文件头（txt 里有「打印完整输出」「输出
    ReAct 执行日志」这类指令，契约就是来压住它们的）。
    """
    key = "wengao"
    txt = ps.load(ps.MAP[key]).strip()
    extra = ps.extra_text().strip()
    assert txt and extra, "txt 或附加提示词读空了，下面的顺序断言会空转"
    with ps.use_extra_prompt(True):
        assert ps.extra_enabled() is True
        composed = ps.system_for(key, wb_summary._roles.get_role(key).system)
    assert composed.startswith(txt[:40]), "开关开着，开头就不再是真源 txt 了"
    assert extra in composed, f"开着开关却没接进附加提示词：{len(composed)} 字"
    assert composed.index(txt[:40]) < composed.index(extra[:40]), (
        "附加提示词跑到 txt 前面了——顺序是定死的"
    )
    full = composed.strip() + "\n" + bs_roles.OUTPUT_CONTRACT
    assert full.rstrip().endswith(bs_roles.OUTPUT_CONTRACT.strip()), (
        "契约不在最末尾：模型会照 txt 的要求把思考过程打进正文，_extract_json 当场坏"
    )
    tail = full[full.rindex(bs_roles.OUTPUT_CONTRACT.strip()[:20]) :]
    assert extra[:40] not in tail, "附加提示词被塞到契约后面了——压不住它"


def test_the_extra_prompt_is_absent_by_default():
    """P5/P6 的默认面：不进 with 块就是**关**，关着的 system 里没有那几个特征串。"""
    key = "fenxi"
    base = wb_summary._roles.get_role(key).system
    assert ps.extra_enabled() is False, "默认值不是关——对抗性双角色会拖垮产出（文档明说）"
    composed = ps.system_for(key, base)
    assert composed == base.strip(), "默认关着却接了东西，或者多做了一次清洗"
    for marker in EXTRA_MARKERS:
        assert marker not in composed, f"附加提示词的特征串 {marker} 出现在默认 system 里"


# ------------------------------------------------------------------ P6：开关语义（两条真判据）


def test_the_switch_nests_back_to_the_enclosing_value_not_to_the_default():
    """P6 之一：嵌套退出回到**外层的值**，不是回到默认值。

    `use_extra_prompt` 用 contextvar + token 复位（`prompt_source.py:203-216`）。
    写成「退出后等于默认 False」会漏掉真正的 bug：内层 `finally` 里直接
    `set(False)` 而不是 `reset(token)`——外层开着附加的那个 job 会被静默关掉。
    所以两个方向都测：外开→内关→回开；外关→内开→回关。
    """
    seen: list[tuple[str, bool]] = []
    with ps.use_extra_prompt(True):
        seen.append(("outer-on", ps.extra_enabled()))
        with ps.use_extra_prompt(False):
            seen.append(("inner-off", ps.extra_enabled()))
        seen.append(("after-inner", ps.extra_enabled()))
    seen.append(("after-outer", ps.extra_enabled()))
    assert seen == [
        ("outer-on", True),
        ("inner-off", False),
        ("after-inner", True),
        ("after-outer", False),
    ], f"嵌套还原不对：{seen}——退出内层必须回外层值（token 复位），不是回默认"


def test_get_role_returns_a_different_system_as_the_switch_moves():
    """P6 之二：`get_role()` 必须**随开关**返回不同 system（抓「在 bind 时烘死」）。

    vendor 如果把 extra 在 `bind()` 时就拼进 `role.system`，那么开关怎么切都拿到同一份
    ——这条必须红。判据取可观测的东西：权威层 `wb_summary.get_role` 与副本内部
    `_roles.get_role` 两条路，在开与关两种状态下各自一致，且两种状态互不相同。
    """
    key = "jinglian"
    off_authority = wb_summary.get_role(key).system
    off_copy = wb_summary._roles.get_role(key).system
    assert off_authority == off_copy, (
        "两条取角色路径给出的 system 不一致（一个过了 view_for 一个没过）"
    )
    with ps.use_extra_prompt(True):
        on_authority = wb_summary.get_role(key).system
        on_copy = wb_summary._roles.get_role(key).system
    assert on_authority == on_copy, "开着时两条路径又不一致了"
    assert on_authority != off_authority, (
        "开关切了而 system 没变——附加提示词被烘死了，配置项 `llm.extra_prompt` 是摆设"
    )
    assert off_authority.startswith(ps.load(ps.MAP[key]).strip()[:40])


def test_the_view_is_the_same_object_when_off_and_a_copy_when_on():
    """开关关着时 `view_for` **原对象照回**（不复制）；开着时回浅拷贝，本体不被污染。

    这条守的是并发正确性：batch_worker 同时跑「开附加」和「不开附加」两个 job 时，
    开着那份必须是拷贝，否则一边写 `view.system` 就把共享的 Role 改了，另一边
    读到带附加提示词的 system（contextvar 的默认面就破了）。
    """
    role = wb_summary._roles.get_role("xiezuo")
    with ps.use_extra_prompt(False):
        assert ps.view_for(role) is role, "关着时不该复制（也不该造出第二份状态）"
    with ps.use_extra_prompt(True):
        view = ps.view_for(role)
        assert view is not role, "开着时必须返回拷贝，否则会写脏共享的 Role"
        assert view.system != role.system
        assert role.system == _read_txt(ps.MAP["xiezuo"]), "本体被改写了——并发时另一边会读到脏值"


def test_the_cache_is_keyed_by_the_switch_not_just_the_role():
    """缓存必须按 (角色, 开关, 目录) 分键：一开一关两个请求不许互相端错菜。"""
    key = "wengao"
    base = wb_summary._roles.get_role(key).system
    with ps.use_extra_prompt(True):
        on_first = ps.system_for(key, base)
    off_first = ps.system_for(key, base)
    with ps.use_extra_prompt(True):
        on_second = ps.system_for(key, base)
    assert on_first == on_second, "同状态两次取到不同东西——缓存键串了"
    assert off_first != on_first, "开着与关着拿到同一份——缓存没按开关分键"
    assert ps.extra_enabled() is False, "离开 with 块后开关应当复位"


def test_two_threads_one_on_one_off_do_not_contaminate_each_other():
    """P6 合同第二条：**并发跑一个开一个关的两个 job，两边的 system 各自正确**。

    这条与「同线程嵌套」不是同一件事：嵌套测的是 token 复位；并发测的是开关**不能是
    模块级布尔**。实现如果写成 `_extra_global = True`，线程 A 一开，线程 B 立刻被污染，
    而这在单线程里永远看不出来。合同还给了具体数字：开 4557 / 关 3563，差值正好是
    附加提示词的长度（+1 是中间那个换行）。

    不钉那两个字面数（它们随 txt 内容漂）：钉的是**差值 == len(extra)+1** 与
    **两条线程各自看到的长度**，这两样合起来才是「互不污染」。
    """
    results: dict[str, list[int]] = {}
    barrier = threading.Barrier(2)
    key = "wengao"
    extra_len = len(ps.extra_text().strip())

    def worker(label: str, enabled: bool) -> None:
        lengths: list[int] = []
        with ps.use_extra_prompt(enabled):
            barrier.wait(timeout=5)  # 两边都进 with 之后再开始，制造真并发
            for _ in range(24):
                lengths.append(len(ps.system_for(key, wb_summary._roles.get_role(key).system)))
        results[label] = lengths

    threads = [
        threading.Thread(target=worker, args=("on", True)),
        threading.Thread(target=worker, args=("off", False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert set(results) == {"on", "off"}, f"有线程没跑完：{sorted(results)}"
    assert len(set(results["on"])) == 1 and len(set(results["off"])) == 1, (
        "同一线程内长度会跳——并发时被另一个线程改写了"
    )
    on_len, off_len = results["on"][0], results["off"][0]
    assert on_len > off_len, f"开着的反而不比关着的长：on={on_len} off={off_len}"
    assert on_len - off_len == extra_len + 1, (
        f"开与关的差值应当正好是附加提示词（{extra_len}）加一个换行，实得 {on_len - off_len}"
    )


def test_the_unsourced_roles_still_read_the_compressed_version_when_off():
    """P3：无源的三个角色（深思/学术/人话）在**开关关**时 == bs 的压缩版，逐字节。

    必须显式钉在关的条件下：extra 对无源角色同样生效（lead 已裁定），开着比就会红。
    这条同时挡掉两种事故：把无源角色「顺手」也接进 MAP（那样 system 变了而 prompt_source
    仍是 None），和把无源角色的 system 清空（副本 :517 立刻走无角色路径）。
    """
    with ps.use_extra_prompt(False):
        for key in ps.NO_SOURCE:
            wb_role = wb_summary._roles.get_role(key)
            bs_role = bs_roles.get_role(key)
            assert wb_role.system, f"{key}：无源角色的 system 被清空了"
            assert wb_role.system == bs_role.system, (
                f"{key}：无源角色的 system 与 bs 压缩版不再逐字相同——"
                f"WB {len(wb_role.system)} 字 vs bs {len(bs_role.system)} 字"
            )
            assert getattr(wb_role, "prompt_source", None) is None


# ------------------------------------------------------------------ P7：绑定入口唯一

#: 合同（task-18 rev3）允许的写入文件集合：真源层 + 权威层两处口子。
#: lead 实测当前 3 处写入全在 `prompt_source.py:300/:304/:330`（bind 两条 + restore 一条），
#: 但白名单按合同给，不要把自己写得更窄的版本当成合同——那会让将来在权威层做一次合法
#: 归一化的改动变成假红。
ALLOWED_SYSTEM_WRITERS = frozenset(
    {
        "video_workbench/core/prompt_source.py",
        "video_workbench/core/summary/__init__.py",
    }
)

#: **绝不允许**出现写入点的地方：逐字节锁死的副本区。角色对象的 system 只能在权威层绑，
#: 副本一旦自己改，界面上显示的 sha 与实际发出去的就从此两份真源。
FORBIDDEN_SYSTEM_WRITERS = (
    "video_workbench/core/summary/roles.py",
    "video_workbench/core/summary/summarizer.py",
)


def _system_assignment_sites() -> list[str]:
    """AST 扫 `video_workbench/**/*.py` 里所有「往对象属性 system 赋值」的位置。

    用 AST 不用 grep：`grep "role.system ="` 会漏掉 `r.system =`、`setattr(x, "system", …)`、
    跨行赋值，也会被注释与字符串里的同样子串骗到（lead 明确要求别退化成 grep）。
    """
    sites: list[str] = []
    for path in sorted((REPO_ROOT / "video_workbench").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - 产品码不该有语法错
            pytest.fail(f"{path} 解析失败")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = [node.target] if isinstance(node, ast.AugAssign) else list(node.targets)
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "system":
                        sites.append(f"{rel}:{node.lineno}")
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "setattr"
                and len(node.value.args) >= 3
                and isinstance(node.value.args[1], ast.Constant)
                and node.value.args[1].value == "system"
            ):
                sites.append(f"{rel}:{node.lineno}(setattr)")
    return sites


def test_only_the_prompt_source_layer_writes_role_system():
    """P7：`role.system` 的写入点只许落在合同那两个文件里（真源层 + 权威层）。

    多一个 owner 就多一处「界面显示的 sha 与实际发出去的提示词不是同一份」的可能，
    而且这类分叉不报错，只让用户觉得「改了没生效」。
    """
    sites = _system_assignment_sites()
    assert sites, "一条写入点都没扫到——这个检测器坏了（下面那条自证会一起红）"
    files = {site.split(":")[0] for site in sites}
    assert files <= set(ALLOWED_SYSTEM_WRITERS), (
        f"出现合同之外的 role.system 写入点：{sorted(sites)}。绑定只能发生在 "
        f"`prompt_source.bind/restore`（必要时权威层）；要加新位置先说清为什么不能在那里做"
    )


def test_the_system_writer_detector_is_not_blind():
    """自证：扫描器真能看见合同里那三处写入，并且盯得住副本区。

    没有这条，「files <= 白名单」在白名单被改成全集、或扫描器根路径写错而扫不到任何文件时
    都会绿。这里正向断言 `prompt_source.py` 至少两处（bind 的两条赋值分支），并单独钉
    **副本区一条都不许有**——那才是逐字节锁与 sha 清单能同时成立的前提。
    """
    sites = _system_assignment_sites()
    writer_sites = [s for s in sites if s.startswith("video_workbench/core/prompt_source.py")]
    assert len(writer_sites) >= 2, (
        f"`prompt_source.py` 里应当至少两处 system 写入（绑全文、缺文件回落），实得 {writer_sites}"
    )
    banned = [s for s in sites if any(s.startswith(f) for f in FORBIDDEN_SYSTEM_WRITERS)]
    assert not banned, f"逐字节锁的副本区出现 system 写入点 {banned}——真源就变成两份了"


def test_a_missing_extra_file_makes_the_switch_a_no_op(monkeypatch, tmp_path):
    """附加那篇文件不在时，**开关开着也等于关**（不许拼出「txt + 空行」这种半截 system）。

    合同把这条写死了：extra 缺失是「少一份增益」，不是「换一套方法论」，界面与日志都不
    该出现任何接进去的痕迹。
    """
    staging = tmp_path / "no-extra"
    staging.mkdir()
    for name, _size, _digest in PROMPT_FILES:
        if name != ps.EXTRA_FILE:
            shutil.copy2(PROMPT_DIR / name, staging / name)
    monkeypatch.setenv(ps.PROMPTS_DIR_ENV, str(staging))
    base = wb_summary._roles.get_role("wengao").system
    assert ps.extra_text() == "", "文件不在时 extra_text() 必须空串（不许抛）"
    with ps.use_extra_prompt(True):
        composed = ps.system_for("wengao", base)
    assert composed == base.strip(), (
        f"extra 缺失时开关开着仍改了 system（{len(composed)} vs {len(base.strip())}）"
    )
    assert not composed.endswith("\n"), "拼了个空段落在末尾——那是「半截 system」的形状"


# ------------------------------------------------------------------ P8：清单与界面读的字段


def test_manifest_reports_every_named_file_including_the_lost_ones(monkeypatch, tmp_path):
    """P8：`manifest()` 列出**当前目录里实际有什么**，外加被点名却缺失的文件。

    「文件丢了」必须显示成一条 bytes=0 / sha 空的行，而不是从清单上凭空少一行——
    设置页读的就是这份清单（`core/summary/__init__.py` 的 `describe_roles` 用同一个
    `info_for`）。
    """
    rows = ps.manifest()
    assert len(rows) == len(PROMPT_FILES), (
        f"清单行数与登记表不符：登记 {len(PROMPT_FILES)} 篇，实得 {len(rows)} 行"
    )
    by_name = {row["name"]: row for row in rows}
    for name, size, digest in PROMPT_FILES:
        row = by_name[name]
        assert row["bytes"] == size and row["sha256"] == digest, f"{name} 的身份不对"
    assert by_name["文稿编辑.txt"]["bound_role"] == "wengao"
    assert by_name["写作专家.txt"]["bound_role"] == "xiezuo"
    assert by_name["分析报告.txt"]["bound_role"] == "fenxi"
    assert by_name[JINGLIAN_FILE]["bound_role"] == "jinglian"
    assert by_name["大概率管用 .txt"]["is_extra"] is True
    # task-24 之前这里是 `bound_role == ""` / `in_map is False`（那篇没人认领）；
    # task-24 把它绑给权威层自造的 `shipin`；task-31 合并 `shipin` → `renhua`，
    # 归属跟着换成 `renhua`。三次变化每一半都翻过来钉：绑定关系变了必须是有意的，
    # 而「文件还在、归属没了」这种静默退化正是这两行会抓的。
    # 「谁绑哪篇」的名单 owner 是 P2 那张逐字表；这里钉的是**文件视角**的投影
    # （manifest 是设置页读的那份，角色视角对了、文件视角没跟着对，界面就会说谎）。
    assert by_name["视频总结.txt"]["bound_role"] == "renhua", (
        f"视频总结.txt 的归属又变成 {by_name['视频总结.txt']['bound_role']!r} 了"
    )
    assert by_name["视频总结.txt"]["in_map"] is True, (
        "已绑定的文件被算成 in_map=False：设置页会说它没接进任何角色"
    )

    empty = tmp_path / "gone"
    empty.mkdir()
    monkeypatch.setenv(ps.PROMPTS_DIR_ENV, str(empty))
    try:
        missing_rows = ps.manifest()
        names = {row["name"] for row in missing_rows}
        for name in list(ps.MAP.values()) + [ps.EXTRA_FILE]:
            assert name in names, f"目录空了就不点名 {name}：丢文件会被显示成「没这个文件」"
        lost = {row["name"]: row for row in missing_rows}
        assert lost[ps.MAP["wengao"]]["bytes"] == 0
        assert lost[ps.MAP["wengao"]]["sha256"] == ""
    finally:
        monkeypatch.delenv(ps.PROMPTS_DIR_ENV, raising=False)


def test_the_info_for_shape_is_stable_for_sourced_and_unsourced_roles():
    """`info_for` 的两副面孔必须形状一致（界面与测试都按同一组键读）。"""
    sourced = ps.info_for("wengao")
    unsourced = ps.info_for("shensi")
    assert (
        set(sourced)
        == set(unsourced)
        == {"prompt_source", "prompt_bytes", "prompt_sha256", "prompt_sha8"}
    )
    assert sourced["prompt_source"] == "文稿编辑.txt"
    assert sourced["prompt_bytes"] == 8479
    assert sourced["prompt_sha8"] == sourced["prompt_sha256"][:8]
    assert unsourced["prompt_source"] is None and unsourced["prompt_bytes"] == 0
    assert unsourced["prompt_sha256"] == ""


# ------------------------------------------------------------------ P8：bs 侧没被顺手带走

#: bili_scribe 的角色表源码（A 的边界：一个字都不许动它）。
BS_ROLES_PY = REPO_ROOT / "bili_scribe" / "core" / "roles.py"


def test_bili_scribe_still_ships_the_compressed_roles():
    """P8：`bili_scribe/core/roles.py` **仍是压缩版**，没被方案 A 顺手带走。

    A 的成立前提是「bs 一个字不改」：它继续用省 token 的压缩版，WB 才谈得上「有意差异」。
    两侧形状一起看才算数——
      * bs 的 `get_role("wengao").system` 不许等于 txt 全文（否则 bs 也被绑了）；
      * bs 的源码里不许出现包内提示词目录（`resources/prompts`）或 `prompt_source`
        这类 WB 侧才有的东西；
      * bs 的压缩版必须**短于** txt 全文（差异方向本身也要钉）。
    字节级的漂移锁在 `test_summary_drift.py`（14 条）；那条管「副本 == 原件」，
    这条管「原件没被换成真源全文」——两个方向各守一面，缺一条就漏。
    """
    assert BS_ROLES_PY.is_file(), f"bs 角色表不在：{BS_ROLES_PY}"
    source = BS_ROLES_PY.read_text(encoding="utf-8", errors="replace")
    assert "resources/prompts" not in source, "bs 开始读包内 txt 了——A 的前提被推翻"
    assert "prompt_source" not in source, "bs 引用了 WB 的绑定层（依赖方向反了）"
    bs_keys = _bs_known_role_keys()
    # bs 的 8 个键**逐字钉死**（这是上游事实的警报，不是 WB 的合同值： bili_scribe/** 谁都不许改，
    # 哪天 bs 自己动角色表，漂移锁会红，而这条会先告诉我们是「加了/删了/改了名字」里的哪一种）。
    assert set(bs_keys) == {
        "standard",
        "xueshu",
        "renhua",
        "shensi",
        "wengao",
        "xiezuo",
        "fenxi",
        "jinglian",
    }, f"bs 的角色表不再是这 8 个：{sorted(bs_keys)}"
    # WB 已无自造角色（task-31 合并）。这条从「防越界」收紧成「防越界 + 承认现状」：
    # MAP 的键必须全部落在 bs 表里，任何 bs 不认的键出现就是有人把自造角色造回来了，
    # 那需要显式决定（并同步 P2 那张逐字绑定表），**不为了让它绿去造一个 wb_only**。
    # 原来这里断 `wb_only` 非空，是因为 `shipin` 在 bs 侧走「未知 key 静默回落 standard」，
    # `standard.system` 是空串：三条比较（!= txt、不以 txt 开头、比 txt 短）对空串全部
    # 平凡成立——所以必须靠「bs 确实没有它」这条正向断言撑着，现在靠的是反向那两条。
    assert not (set(ps.MAP) - set(bs_keys)), (
        f"MAP 里出现了 bs 不认的角色 {sorted(set(ps.MAP) - set(bs_keys))}"
    )
    compared = 0
    for key, name in ps.MAP.items():
        bs_system = bs_roles.get_role(key).system
        txt = _read_txt(name)
        assert bs_system != txt, f"bs 的 {key} 被换成 txt 全文了"
        assert not bs_system.startswith(txt.strip()[:40]), (
            f"bs 的 {key} 开头已是 txt 全文——绑定泄漏到 bs 侧"
        )
        # 长度方向按登记（唯一 owner：`wb_prompt_sizes.TXT_SHORTER_THAN_COMPRESSED`）。
        # `视频总结.txt` 全文 511 字**短于** bs `renhua` 压缩版 1509 字——那是 task-31
        # 把 `shipin` 合并进 `renhua` 的直接后果：人话角色的方法论正文缩到约三分之一。
        # 原来这里一刀切断「压缩版必然更短」，合并那天就红了：不是绑定错、不是 bs 被改，
        # 是代理指标覆盖了真实事实。改成登记之后，两种方向各有一条会红的期望。
        if name in TXT_SHORTER_THAN_COMPRESSED:
            assert len(bs_system) > len(txt), (
                f"bs 的 {key} 压缩版不再比 {name} 长（{len(bs_system)} vs {len(txt)}）："
                f"要么 bs 被改短、要么那篇 txt 被补长了——补过之后请把名字从登记里删掉，"
                f"别把这条改成永远成立"
            )
        else:
            assert len(bs_system) < len(txt), (
                f"bs 的 {key} 不再比 txt 短（{len(bs_system)} vs {len(txt)}），压缩版形同虚设"
            )
        compared += 1
    assert compared, (
        "共有角色一个都没比到（MAP 与 bs 名单的交集空了）——这条就退化成"
        "「只读源码字符串」，下面三条比较全在原地空转"
    )


# ------------------------------------------------------------------ P9：用户勾的那个盒子到哪去了


#: 适配层的入口形状（`video_workbench/core/summarizer.py:838` 起）：
#: `run(project, segments, *, client, cfg, ctx, role=..., extra_prompt=None)`。
#: 这里刻意不经过 RPC：走 RPC 要建项目、建库、起 worker，而这条承诺的本质是
#: 「配置里那个布尔值最后有没有变成发出去的 system」，用假 LLM 直接打适配器最省。
def _tiny_project() -> dict:
    return {
        "bvid": "BV1promptwiring",
        "aid": 3,
        "cid": 4,
        "page": 1,
        "page_count": 1,
        "title": "提示词接线素材",
        "desc": "固定简介",
        "up_name": "UP接线",
        "up_mid": 5,
        "duration_sec": 60,
        "tname": "知识",
        "tags": ["接线", "固定"],
    }


def _tiny_segments() -> list[dict]:
    return [
        {"start_ms": index * 5000, "text": f"第{index}件固定的事，内容长度稳定。"}
        for index in range(1, 9)
    ]


#: 长素材：900 段 × 每段约 40 字 ≈ 27,000 字 > `max_input_chars`（24,000），
#: 因此一定走分段预读（task-15 实测的段数与字数就是这个形状）。
LONG_SEG_TAIL = "这是一段固定长度的口语内容，用来把素材推过分段预读的阈值，字数稳定不漂移。"


def _long_segments(count: int = 900) -> list[dict]:
    return [
        {"start_ms": index * 4000, "text": f"第{index}段{LONG_SEG_TAIL}"}
        for index in range(1, count + 1)
    ]


def _markers_in(requests) -> list[bool]:
    """每一次请求的 system 里是否出现了附加提示词的特征串。"""
    out = []
    for req in requests:
        system = str(req["messages"][0]["content"])
        out.append(any(marker in system for marker in EXTRA_MARKERS))
    return out


def test_the_configured_switch_reaches_the_model_through_run(default_env):
    """P9 之一：配置里勾上 `llm.extra_prompt` → 发出去的 system 真的接进了附加提示词。

    这条是「用户看得见的那个开关」的端到端判据：勾了没生效 与 没勾却生效 都是事故，
    而两边都可能静默（提示词长一点短一点，输出照样像对的）。
    顺带钉住范围：附加只接在**生成与修订**（json_mode 那两轮），自我批判那轮不吃它
    （A 不动 `role.critique`，见 `test_summary_parity.py` 的 A 组）。
    """
    ad = importlib.import_module("video_workbench.core.summarizer")
    off = FakeLLM(three_pass_script())
    ad.run(
        _tiny_project(), _tiny_segments(), client=off, cfg=make_cfg(), ctx=QuietCtx(), role="wengao"
    )
    assert _markers_in(off.requests) == [False, False, False], (
        f"配置没勾却在 system 里出现附加提示词：{_markers_in(off.requests)}"
    )

    on = FakeLLM(three_pass_script())
    ad.run(
        _tiny_project(),
        _tiny_segments(),
        client=on,
        cfg=make_cfg(extra_prompt=True),
        ctx=QuietCtx(),
        role="wengao",
    )
    flags = _markers_in(on.requests)
    assert flags == [True, False, True], (
        f"勾上之后应当「生成与修订带附加、自我批判不带」，实得 {flags}；"
        f"三轮的 json_mode 指纹={[r['json_mode'] for r in on.requests]}"
    )
    assert len(on.requests) == len(off.requests), "开关不该改变调用次数——它只改 system 正文"


def test_the_explicit_argument_overrides_the_configuration(default_env):
    """P9 之二：显式入参**优先于**配置（`extra_prompt_on` 的既定顺序）。

    两个方向都要：cfg 关 + 显式开 → 开；cfg 开 + 显式关 → 关。只测一个方向的话，
    实现把顺序写反（配置永远赢）照样绿，而 job 重放与「本次先试试」就分不开。
    """
    ad = importlib.import_module("video_workbench.core.summarizer")
    llm_cfg_off = FakeLLM(three_pass_script())
    ad.run(
        _tiny_project(),
        _tiny_segments(),
        client=llm_cfg_off,
        cfg=make_cfg(),
        ctx=QuietCtx(),
        role="wengao",
        extra_prompt=True,
    )
    assert any(_markers_in(llm_cfg_off.requests)), "显式开了却没接进去（配置把入参盖掉了）"

    llm_cfg_on = FakeLLM(three_pass_script())
    ad.run(
        _tiny_project(),
        _tiny_segments(),
        client=llm_cfg_on,
        cfg=make_cfg(extra_prompt=True),
        ctx=QuietCtx(),
        role="wengao",
        extra_prompt=False,
    )
    assert not any(_markers_in(llm_cfg_on.requests)), (
        "显式关掉了却还带着附加提示词（配置把入参盖掉了）"
    )


def test_refine_honours_the_same_switch(default_env):
    """P9 之三：「再改一版」这条路（`refine()`）也吃同一个开关，行为与 `run()` 一致。

    产品事实：`refine()` 在 `:925` 收 `extra_prompt`、`:968` 用同一个 `with` 包法。
    这条单独立着，是因为 run 与 refine 是两处包点——只接对一处是很容易犯的分叉。
    """
    ad = importlib.import_module("video_workbench.core.summarizer")
    previous = {
        "one_line": "一句话",
        "summary_md": "正文",
        "key_points": ["要点"],
        "timeline": [{"t": "00:00", "text": "开场"}],
        "caution": "",
        "comment_short": "短版",
        "revise_note": "",
    }
    off = FakeLLM(three_pass_script())
    ad.refine(
        _tiny_project(),
        _tiny_segments(),
        previous,
        "把语气收紧一点",
        client=off,
        cfg=make_cfg(),
        ctx=QuietCtx(),
        role="wengao",
    )
    assert not any(_markers_in(off.requests)), (
        f"改稿路径没勾却带了附加提示词：{_markers_in(off.requests)}"
    )

    on = FakeLLM(three_pass_script())
    ad.refine(
        _tiny_project(),
        _tiny_segments(),
        previous,
        "把语气收紧一点",
        client=on,
        cfg=make_cfg(extra_prompt=True),
        ctx=QuietCtx(),
        role="wengao",
    )
    assert any(_markers_in(on.requests)), "改稿路径勾了却没接进去"


class _NoteThenPassLLM(FakeLLM):
    """预读那几次要**纯文本小记**，归约那三步要 JSON——按 system 自己分辨该给什么。

    为什么不数分段数：脚本顺序如果写死成「N 条小记 + 三步」，那 N 就必须等于实际分段数；
    素材一改，生成那一步就会拿到一条小记，报「模型没返回可解析的 JSON」——测出来的是
    夹具的簿记问题，不是产品问题。分辨依据用 `MAP_SYSTEM` 本身（预读的唯一指纹）。
    """

    def chat(self, messages, *args, **kwargs):
        system = str(messages[0]["content"]) if messages else ""
        if system.strip().startswith(MAP_SYSTEM.strip()[:12]):
            self.requests.append(
                {
                    "messages": [dict(item) for item in messages],
                    "json_mode": bool(kwargs.get("json_mode", False)),
                    "temperature": kwargs.get("temperature"),
                    "max_tokens": kwargs.get("max_tokens"),
                }
            )
            return f"第 {len(self.requests)} 段的口语小记，固定一句。"
        return super().chat(messages, *args, **kwargs)


def test_the_switch_never_touches_the_pre_read_requests(default_env):
    """P9 之四：长素材的**分段预读**永远不吃附加提示词，开关怎么切都一样。

    这是成本口径的可执行形式（lead 现读钉正过：A 的固定成本只加在生成与修订两轮，
    不随分段数放大）。预读发的是固定的 `MAP_SYSTEM`，`with` 也只包住在预读之后
    （`core/summarizer.py:876` 的 prepare 在 `:884` 的 with 外面）。
    实现若把整段 `_summary_job` 包进 with，这条立刻红。
    """
    ad = importlib.import_module("video_workbench.core.summarizer")
    llm = _NoteThenPassLLM(three_pass_script())
    ad.run(
        _tiny_project(),
        _long_segments(),
        client=llm,
        cfg=make_cfg(extra_prompt=True),
        ctx=QuietCtx(),
        role="wengao",
    )
    systems = [str(req["messages"][0]["content"]) for req in llm.requests]
    pre_reads = [s for s in systems if s.strip().startswith(MAP_SYSTEM.strip()[:12])]
    assert len(pre_reads) >= 2, f"没量到预读请求（共 {len(systems)} 次），这条就空转了"
    for text in pre_reads:
        assert text.strip() == MAP_SYSTEM.strip(), (
            "预读的 system 被动过——它吃的是固定的 MAP_SYSTEM，与角色与开关都无关"
        )
        assert not any(marker in text for marker in EXTRA_MARKERS), (
            "附加提示词混进预读了：A 的成本不该随分段数放大"
        )
    reduced = [s for s in systems if not s.strip().startswith(MAP_SYSTEM.strip()[:12])]
    assert any(any(marker in s for marker in EXTRA_MARKERS) for s in reduced), (
        "预读干净、归约也没带上附加提示词——那 with 到底包住了什么？"
    )


def test_the_settings_save_path_can_actually_turn_the_switch_on():
    """P9 之五：设置页那个勾选**存得进去**——`cfg.apply({"llm": {"extra_prompt": true}})` 生效。

    为什么单独钉这一条：`settings_put` 没有白名单（`app/service.py:958` 直接把 values 交给
    `cfg.apply`），而 `LlmConfig.extra_prompt` 是 dataclass 字段（`config.py:461`）。
    两边任一处掉了字段，勾选就变成装饰——界面能勾、发出去的 system 永远不带附加，
    而且没有任何报错。这条只验「配置这一跳」，system 那一跳由上面几条管。
    """
    from video_workbench.config import WorkbenchConfig

    cfg = WorkbenchConfig.__new__(WorkbenchConfig)
    # 用真实默认构造，别手搭：手搭的 cfg 会绕过 dataclass 默认值这个真源。
    cfg.__init__()  # type: ignore[misc]
    assert cfg.llm.extra_prompt is False, "附加提示词必须默认关（开了会拖垮 JSON 产出）"
    cfg.apply({"llm": {"extra_prompt": True}})
    assert cfg.llm.extra_prompt is True, "设置存不进去：UI 勾了、PUT 发了、cfg 里还是 False"
    ad = importlib.import_module("video_workbench.core.summarizer")
    assert ad.extra_prompt_on(cfg, None) is True, (
        "cfg 存进去了但 `extra_prompt_on` 没读它——with 那层接错线"
    )
    assert ad.extra_prompt_on(cfg, False) is False, "显式入参必须还能压过配置"


# ---------------------------------------------------------------------------
# task-41 判据 4：导入期对账的两个打断实验（lead 要求「要真断言，不要 print」）
#
# 三条对账住在 `video_workbench/core/summary/__init__.py:127 _reconcile_role_lists()`，
# 导入时跑一次：`known(RETIRED ∪ ROLE_KEYS) == owned(_NEVER_BIND ∪ MAP ∪ NO_SOURCE)`、
# `NO_SOURCE == ROLE_KEYS − MAP`、`MAP ∩ RETIRED == ∅`。产品码里的注释自称「少写一键
# 就必须红」，但**这句话本身此前没有任何用例验过**。下面三条就是把它验一遍：
# 每条都在内存里把一份名单改坏（盘上零写入），再直接重跑那个函数，断它必须抛、
# 且异常文本点得出是哪一个键坏、坏在哪一侧；每条都带「原样不抛」的控制腿，
# 免得将来有人把断言改成「怎么都抛」——那等于什么都没测。
# ---------------------------------------------------------------------------


def _reconcile_now():
    """直接重跑导入期那三条对账（它是普通函数，不必重新 import 整个包）。"""
    import video_workbench.core.summary as authority

    authority._reconcile_role_lists()


def test_the_reconciliation_passes_on_the_shipped_lists():
    """控制腿：盘上现在这三份名单是自洽的——否则下面三条「必须抛」全是空响。"""
    _reconcile_now()  # 不抛即通过；抛了就是产品当前状态坏了


def test_a_sourced_role_pushed_into_no_source_is_caught(tmp_path):
    """打断实验 1：把一个**有源**角色塞进 `NO_SOURCE`，对账必须炸，不许静默通过。

    为什么这种坏法危险：`NO_SOURCE` 的语义是「这个角色没有 txt 真源，用内置压缩版」。
    若 `wengao` 同时出现在 `MAP` 和 `NO_SOURCE` 里，绑定与回落两条声明就互相盖——
    `bind()` 按 MAP 走、`describe_roles()` 那列显示按 NO_SOURCE 走，界面与请求不一致，
    而**导入不会报错**，全量测试也可能照样绿（这正是 task-31 那次的形状）。
    """
    import video_workbench.core.prompt_source as ps
    import video_workbench.core.summary as authority

    sourced = sorted(ps.MAP)[0]
    original = ps.NO_SOURCE
    ps.NO_SOURCE = tuple(dict.fromkeys(original + (sourced,)))
    try:
        with pytest.raises(RuntimeError) as exc:
            authority._reconcile_role_lists()
    finally:
        ps.NO_SOURCE = original
    message = str(exc.value)
    assert sourced in message, f"炸了但没点出是哪个键：{message!r}"
    assert "多写" in message, f"没走到『NO_SOURCE 多写』那一条对账：{message!r}"
    # 还原后必须重新自洽——否则这条的「红」是被前一条污染的。
    _reconcile_now()


def test_a_retired_key_missing_from_never_bind_is_caught(tmp_path):
    """打断实验 2：`_NEVER_BIND` 少一个退役键，对账必须炸。

    产品注释写得很清楚（`prompt_source.py:85-88`）：漏一个键的后果本来是**静默**的——
    被漏掉的退役角色会落进 `kept_compressed`（`bind()` 里 MAP 查不到就走那条），
    于是它既不在界面上（清单已过滤）、又出现在给测试看的绑定结果里，第三种不报错
    也不留白的状态。所以这里断两件事：①对账拦住；②拦住的是「只在名单侧」那一半
    （`RETIRED_ROLES` 有它、真源三桶没它），不是别的噪声。
    """
    import video_workbench.core.prompt_source as ps
    import video_workbench.core.summary as authority

    dropped = sorted(authority.RETIRED_ROLES)[0]
    original = ps._NEVER_BIND
    ps._NEVER_BIND = frozenset(set(original) - {dropped})
    try:
        with pytest.raises(RuntimeError) as exc:
            authority._reconcile_role_lists()
    finally:
        ps._NEVER_BIND = original
    message = str(exc.value)
    assert dropped in message, f"炸了但没点出是哪个键：{message!r}"
    assert "只在名单侧" in message, f"没走到『known ⊃ owned』那一条对账：{message!r}"
    _reconcile_now()


def test_a_txt_bound_to_a_retired_role_is_caught(tmp_path):
    """附赠第三条（同一把对账，历史事故的那一种）：MAP 把 txt 绑到退役角色上必须炸。

    这是 task-31 记下的形状——「txt 挂在退役角色上、用户命令一字没落地而全量测试照样绿」。
    `bind()` 会跳过 `_NEVER_BIND`，所以这种绑定等于没绑：那份 txt 永远不会生效，
    而 manifest、设置页、请求序列都看不出异常。对账第三条专门拦它。
    """
    import video_workbench.core.prompt_source as ps
    import video_workbench.core.summary as authority

    retired = sorted(authority.RETIRED_ROLES)[0]
    original_map = dict(ps.MAP)
    ps.MAP = {**original_map, retired: ps.EXTRA_FILE}
    try:
        with pytest.raises(RuntimeError) as exc:
            authority._reconcile_role_lists()
    finally:
        ps.MAP = original_map
    message = str(exc.value)
    assert retired in message, f"炸了但没点出是哪个退役键被绑：{message!r}"
    assert "不会生效" in message or "bind()" in message, (
        f"没走到『MAP ∩ RETIRED』那一条对账：{message!r}"
    )
    _reconcile_now()
