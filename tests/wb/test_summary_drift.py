"""迁入副本的**漂移锁定**（task-5，验收项 A2 / A4 的静态那一半）。

## 这组用例在防什么

`video_workbench/core/summary/{roles.py,summarizer.py}` 是 `bili_scribe/core/` 同名文件的
副本。副本一旦可以自由编辑，「一比一复刻」这句话会在第三次改动之后失效，而且失效得
毫无声响：两侧都还跑得通，只是产出不一样了。所以这里把「允许哪里不一样」钉成**行号**，
其余任何一处改动都必须让测试变红。

## 判定标准（写死，免得后来人猜哪边是对的）

1. **`roles.py` —— 逐字节相等，零例外。**
   它是提示词文本与角色数据的载体，而提示词就是这次复刻的唯一标的。副本里没有任何
   一行有理由与原件不同；出现差异就说明有人开始分叉了。

2. **`summarizer.py` —— 除 import 段外逐字节相等；import 段的行号合同钉在第 9–23 行。**
   这一对文件唯一的正当差异是「从哪里 import」：原件用相对导入指向 `bili_scribe` 自己的
   `config / infra.llm / frames / roles / collector / resolver`，副本必须指向 WB 侧的对应物。
   这条合同由**两条**用例分别守，因为 vendor 实际的写法与合同的字面行号不符：
     * `test_summarizer_body_is_identical_apart_from_the_import_block` —— 按**锚点**定位
       import 段，锁住它之外的每一个字节。**这条是绿的**，正文改一个字都会红。
     * `test_summarizer_copy_keeps_the_pinned_import_block_range` —— 按**行号**（第 9–23 行）
       的合同验收。**这条现在是红的**：副本把 import 段里的两个空行压掉了（实测 13 行），
       内容层面与原件仍逐字节相同，只是正文整体错位 2 行。要 vendor 或 lead 处置，
       不由测试单方面认下来。
   第 25 行起（`if TYPE_CHECKING:` 块）属于**正文**，改它必须红。

3. **`DEFAULT_ROLE` —— 登记过的有意差异，不是 bug。**
   WB = `"wengao"`，bs = `"standard"`（真源 `bili_scribe/config.py:85`）。这一条存在的理由
   是两侧默认值**本来就该不同**；哪天有人把它「对齐」了才是事故。所以断言写成两边的
   显式期望值，而不是含糊的 `assert a != b`。

## EOL 的处理

byte 级相等对换行符敏感，而仓库里有 `.gitattributes`。断言保持严格（照合同办），
但**不等时给诊断**：首个差异字节偏移、所属行号、以及「归一 CRLF/LF 之后是否就相等」。
这样真漂移与「只是换行符不一样」在报错里一眼可分，不需要二次排查。

## 副本还没落地时怎么办

本文件所有触及副本的用例**必须是红的**，不许 skip：skip 会被读成「这条不用管」。
所以取副本的入口在未落地时统一 `pytest.fail` 并点名在等谁（task-4 / vendor-engineer）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

BS_ROLES = ROOT / "bili_scribe" / "core" / "roles.py"
BS_SUMMARIZER = ROOT / "bili_scribe" / "core" / "summarizer.py"
WB_SUMMARY_DIR = ROOT / "video_workbench" / "core" / "summary"
WB_ROLES = WB_SUMMARY_DIR / "roles.py"
WB_SUMMARIZER = WB_SUMMARY_DIR / "summarizer.py"

#: summarizer 副本唯一允许与原件不同的行号区间（1-based，含端点）。合同值，改动要同步
#: `_assert_import_block_anchors` 的锚。
IMPORT_BLOCK_FIRST, IMPORT_BLOCK_LAST = 9, 23

#: 副本尚未落地时的提示。
WAIT_FOR_VENDOR = (
    "`video_workbench/core/summary/` 还没有落地。这组用例在等 vendor-engineer 的 task-4"
    "（把 bili_scribe 的 roles.py / summarizer.py 逐字节迁入并退役 standard 角色）。"
    "产品侧完成之前，本文件这些断言**应该**是红的——这不是测试写坏了，别放宽断言。"
)


#: 两侧共有的角色 = bs 认得、WB 权威层也没退役的那几个，按 bs 原有顺序。
#:
#: task-31 之前这里是写死的 7 元组（含 `xueshu`），并且下面那条公开面用例还额外要求
#: 「后段非空」——因为 `shipin` 是权威层自造的第一个角色。task-31 把 `xueshu` 退役、
#: 把 `shipin` 合并进 `renhua` 之后，那两处都成了过期的合同值：名单少了一个、自造段
#: 空了。所以**名单改成现读**，不再留第四份写死的角色表（本仓库已经因为写死名单在每
#: 一次角色增删时红一整片：task-24 红过 5 条、task-31 又红 15 条）。
#:
#: 现读不等于没牙：共有段的锚点是 `bili_scribe.core.roles.ALL_ROLES`——那是**逐字节锁定
#: 的原件**（漂移锁本身守着它），所以「退役集合少配一键」会让共有段变长而公开面没变短，
#: 这条立刻红。退役集合**等于哪三个键**这个身份信息不在本文件钉（唯一 owner 是
#: `tests/wb/test_summary_retired_roles.py`），本文件只管「漂移」这一维。
def _shared_role_keys(wb_package) -> tuple:
    """现读：bs 的键序去掉权威层退役的那些，就是两侧共有段。"""
    from bili_scribe.core import roles as bs_roles

    retired = set(wb_package.RETIRED_ROLES)
    return tuple(role.key for role in bs_roles.ALL_ROLES if role.key not in retired)


class _QuietCtx:
    """适配层要的进度对象，这里不关心内容。"""

    def progress(self, value=None, stage=None, speed=None, eta=None):
        return None

    def log(self, line):
        return None

    def should_cancel(self):
        return False

    def check_cancel(self):
        return None


# ------------------------------------------------------------------ 取文件 / 取模块


def _read_required(path: Path) -> bytes:
    if not path.is_file():
        pytest.fail(f"{WAIT_FOR_VENDOR}\n（缺文件：{path.relative_to(ROOT)}）")
    return path.read_bytes()


def _wb_package():
    """`video_workbench.core.summary` 权威层。"""
    try:
        return importlib.import_module("video_workbench.core.summary")
    except ImportError as exc:
        pytest.fail(f"{WAIT_FOR_VENDOR}\n（ImportError：{exc}）")


def _wb_summarizer_module():
    """`video_workbench.core.summary.summarizer` 副本本体（diff_fields 等只在这里有）。"""
    try:
        return importlib.import_module("video_workbench.core.summary.summarizer")
    except ImportError as exc:
        pytest.fail(f"{WAIT_FOR_VENDOR}\n（ImportError：{exc}）")


# ------------------------------------------------------------------------ 比较器


def _normalise_eol(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _first_diff_offset(a: bytes, b: bytes) -> int:
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def _line_no(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _split_block(
    data: bytes, first: int, last: int
) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """按 1-based 闭区间 [first, last] 切成 (前段行, 允许差异行, 后段行)。"""
    lines = data.split(b"\n")
    if len(lines) < last:
        pytest.fail(f"文件只有 {len(lines)} 行，撑不起第 {last} 行的允许区间")
    return lines[: first - 1], lines[first - 1 : last], lines[last:]


def _assert_bytes_equal(a: bytes, b: bytes, label: str) -> None:
    """逐字节相等；不等时给「首个差异行 + 是否只是 EOL 问题」的诊断。"""
    if a == b:
        return
    offset = _first_diff_offset(a, b)
    hint = (
        "归一 CRLF/LF 之后就相等 → 只是换行符不一致（检出方式 / .gitattributes），不是内容漂移"
        if _normalise_eol(a) == _normalise_eol(b)
        else "归一换行符之后仍然不等 → 这是真的内容漂移"
    )
    pytest.fail(
        f"{label} 漂移：第 {_line_no(a, offset)} 行（首个差异字节偏移 {offset}，"
        f"全长 {len(a)} vs {len(b)}）。{hint}"
    )


def _split_at_import_block(data: bytes) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """按**内容锚点**把 summarizer 切成 (文档串, import 段, 其余正文)。

    为什么还要一条按锚点定位的版本：合同把 import 段钉成「第 9–23 行」，而副本实际把
    import 段里的两个空行压掉了（实测 13 行，9–21）。行号合同由
    `test_summarizer_copy_keeps_the_pinned_import_block_range` 守着、**继续红**；但
    「改正文一行必须红」这个真正的保护不该因为两个空行而失效，所以正文用锚点定位来锁。
    两条测试合起来才是合同的完整意思：**行号要照合同，正文一律不许动。**
    """
    lines = data.split(b"\n")
    anchor = b"from __future__ import annotations"
    if anchor not in lines:
        pytest.fail("找不到 `from __future__ import annotations` 锚点")
    start = lines.index(anchor)
    try:
        tc = next(i for i, ln in enumerate(lines) if ln.startswith(b"if TYPE_CHECKING"))
    except StopIteration:
        pytest.fail("找不到 `if TYPE_CHECKING` 锚点")
    if lines[tc - 1] != b"":
        pytest.fail("import 段与 if TYPE_CHECKING 之间必须有且仅有一个空行")
    return lines[:start], lines[start : tc - 1], lines[tc - 1 :]


def _assert_equal_outside_block(a: bytes, b: bytes, label: str, first: int, last: int) -> None:
    """第 [first, last] 行允许不同，**其余行**必须逐行相等（行号合同的原话）。"""
    pre_a, mid_a, post_a = _split_block(a, first, last)
    pre_b, mid_b, post_b = _split_block(b, first, last)
    _assert_bytes_equal(b"\n".join(pre_a), b"\n".join(pre_b), f"{label}（第 1–{first - 1} 行）")
    _assert_bytes_equal(b"\n".join(post_a), b"\n".join(post_b), f"{label}（第 {last + 1} 行往后）")


def _assert_body_equal(a: bytes, b: bytes, label: str) -> None:
    """按锚点定位 import 段，比较它**之外**的全部字节。"""
    pre_a, _block_a, post_a = _split_at_import_block(a)
    pre_b, _block_b, post_b = _split_at_import_block(b)
    _assert_bytes_equal(b"\n".join(pre_a), b"\n".join(pre_b), f"{label}（文档串）")
    _assert_bytes_equal(
        b"\n".join(post_a), b"\n".join(post_b), f"{label}（import 段之后的全部正文）"
    )


# ---------------------------------------------------------------- 1. roles.py 逐字节


def test_roles_original_is_a_full_module_not_an_empty_file():
    """先钉住「原件是完整的」，否则下一条会退化成「两个空文件相等」。

    逐字节相等这种断言最坏的失败模式是假绿：两侧都被截断成空文件时它照样通过。
    bs 的 roles.py 由 git 追踪、不可能为空，所以把它的规模钉死就足以封掉这条路。
    数值取实测下限（当前 666 行 / 24 KB 量级），留一半余量，不约束上游正常精简。
    """
    data = BS_ROLES.read_bytes()
    assert len(data) > 15_000, "bili_scribe/core/roles.py 字节数低于下限"
    assert data.count(b"\n") > 400, "bili_scribe/core/roles.py 行数低于下限"


def test_roles_copy_is_byte_identical_to_bili_scribe():
    """判定标准 1：roles.py 逐字节相等，零例外。"""
    _assert_bytes_equal(
        _read_required(BS_ROLES),
        _read_required(WB_ROLES),
        "core/summary/roles.py（对 bili_scribe/core/roles.py）",
    )


def test_summarizer_original_is_a_full_module_not_an_empty_file():
    data = BS_SUMMARIZER.read_bytes()
    assert len(data) > 15_000, f"bili_scribe/core/summarizer.py 只有 {len(data)} 字节"
    assert data.count(b"\n") > 400


# ------------------------------------------------------- 2. summarizer.py 除 import 段


def _assert_import_block_anchors() -> None:
    """钉住「第 9–23 行确实就是 import 段」——用原件实测的行做锚。

    为什么锚也要写成断言：只写死行号的话，某天原件在文档字符串里多插一行，允许区间
    就会悄悄套在正文头上，把真正的漂移盖住——那时这套测试反而成了掩护。锚断了必须红，
    并且要人去**同步这里的行号常量**，而不是把断言删掉。
    """
    lines = BS_SUMMARIZER.read_bytes().split(b"\n")
    assert lines[IMPORT_BLOCK_FIRST - 2] == b"", "第 8 行不是空行（文档串结束处）"
    assert lines[IMPORT_BLOCK_FIRST - 1] == b"from __future__ import annotations", (
        f"原件第 {IMPORT_BLOCK_FIRST} 行不再是 __future__ 导入：允许区间需要跟着改"
    )
    assert lines[IMPORT_BLOCK_LAST - 1] == b"from .resolver import format_duration", (
        f"原件第 {IMPORT_BLOCK_LAST} 行不是 import 段末行：允许区间需要跟着改"
    )
    assert lines[IMPORT_BLOCK_LAST] == b"", "import 段后一行不是空行"
    assert lines[IMPORT_BLOCK_LAST + 1].startswith(b"if TYPE_CHECKING"), (
        "第 25 行不是 TYPE_CHECKING 块：允许区间正在吃正文"
    )


def test_summarizer_import_block_range_is_pinned_against_the_original():
    """允许区间先对原件自证：锚点对得上，切分可逆。"""
    _assert_import_block_anchors()
    data = BS_SUMMARIZER.read_bytes()
    head, mid, tail = _split_block(data, IMPORT_BLOCK_FIRST, IMPORT_BLOCK_LAST)
    assert b"\n".join(head + mid + tail) == data, "区间切分不可逆，行号锚已失效"
    assert head[-1] == b"" and head[6] == b'"""', "前段不是「文档串 + 空行」"
    assert mid[0] == b"from __future__ import annotations"
    assert mid[-1] == b"from .resolver import format_duration"
    assert tail[0] == b"" and tail[1].startswith(b"if TYPE_CHECKING"), "区间后与锚不符"


def test_summarizer_body_is_identical_apart_from_the_import_block():
    """判定标准 2 的**正文锁**：import 段之外的每个字节都必须与原件相同。

    按锚点定位（文档串 | import 段 | 其余正文），所以这条不受副本压掉两个空行影响，
    现在就是绿的、并且真的能在有人改正文时变红。行号合同另见
    `test_summarizer_copy_keeps_the_pinned_import_block_range`。
    """
    _assert_body_equal(
        _read_required(BS_SUMMARIZER), _read_required(WB_SUMMARIZER), "core/summary/summarizer.py"
    )


def test_copy_import_block_contains_nothing_but_imports():
    """允许段不许变成逃生舱：那里只准出现 import 与注释/空行。

    「第 9–23 行随便改」这条豁免的真实风险，是有人把兼容 shim（比如
    `sys.modules[...] = ...`、或一段 try/except 的可选依赖探测）塞进 import 段——
    它在豁免区里，正文锁永远看不见，但它改变的是模块的运行时行为。
    """
    _pre, block, _post = _split_at_import_block(_read_required(WB_SUMMARIZER))
    allowed = (b"import ", b"from ", b"#")
    for offset, line in enumerate(block, 1):
        text = line.strip()
        if not text or text.startswith(allowed):
            continue
        pytest.fail(
            f"副本 import 段第 {offset} 行不是导入语句：{line[:60]!r}。"
            "允许差异的段落只给导入语句用，任何可执行代码都属于正文。"
        )


def test_summarizer_copy_keeps_the_pinned_import_block_range():
    """行号合同：副本的 import 段必须正好占满第 9–23 行。**当前是红的，而且应该红。**

    红的原因不是正文漂移（正文锁上一条已证明相同），而是副本把 import 段里的两个空行
    压掉了，于是 import 段是 13 行、正文整体从第 22 行开始而不是第 24 行。合同写的是
    「第 9–23 行」，这个偏差要由产品侧或 lead 处置，不该由测试单方面认下来——但也别把它
    当成「复刻失败」：内容层面它是逐字节一致的。
    """
    _assert_import_block_anchors()
    wb = _read_required(WB_SUMMARIZER)
    pre_w, block_w, _post_w = _split_at_import_block(wb)
    actual_first = len(pre_w) + 1
    actual_last = actual_first + len(block_w) - 1
    assert (actual_first, actual_last) == (IMPORT_BLOCK_FIRST, IMPORT_BLOCK_LAST), (
        f"合同钉死副本 import 段 = 第 {IMPORT_BLOCK_FIRST}–{IMPORT_BLOCK_LAST} 行"
        f"（{IMPORT_BLOCK_LAST - IMPORT_BLOCK_FIRST + 1} 行），"
        f"实测是第 {actual_first}–{actual_last} 行（{len(block_w)} 行）。"
        "副本把原件 import 段里第 10 行与第 18 行的空行压掉了，正文因此整体错位 "
        f"{IMPORT_BLOCK_LAST - actual_last} 行。\n"
        "两个处置方式（选一个，别删断言）：① 产品侧把那两个空行补回来，行号即与合同一致；"
        "② lead 明确把合同改成「import 段按锚点定位、行数不限」，那时本用例连同 "
        "IMPORT_BLOCK_FIRST/LAST 一起删除，并在文件头的判定标准里登记这个改动。"
    )


def test_drift_guard_is_not_vacuous():
    """灵敏度自证（A2「故意改一行必须红」现在就能跑的那一半）。

    两个比较器都要自证，因为它们守的是合同的两半：

    **锚点版（`_assert_body_equal`，正文锁）**
      ① 正文改一个字符 → 必须红；
      ② 文档串改一个字 → 必须红；
      ③ import 段原地改一行 → 必须**不**红（这正是合同给的豁免）。

    **行号版（`_assert_equal_outside_block`，行号合同）**
      ④ import 段原地改一行、行数不变 → 必须不红；
      ⑤ import 段多塞一行 → 必须红，且报错落在「第 24 行往后」。

    ⑤ 是本文件第一版实现真实踩过的坑：当时按**字节区间**跳过 import 段，副本 import
    段长度一变，后段就整体错位，正文明明逐字相同却假红；反过来把区间放宽又能盖住真漂移。
    没有 ③/④ 的话，把允许区设成空集也能通过全部正向断言——所以必须成对测。
    本条守的是比较器本身，不涉及 summarizer 的任何业务语义。
    """
    bs = BS_SUMMARIZER.read_bytes()
    block = (IMPORT_BLOCK_FIRST, IMPORT_BLOCK_LAST)

    body_anchor = b"MAX_CHUNKS = 8"
    assert body_anchor in bs, "正文锚点没了，灵敏度无从验证"
    mutated_body = bs.replace(body_anchor, b"MAX_CHUNKS = 9", 1)

    doc_anchor = "这个模块最重要的设计".encode("utf-8")
    assert doc_anchor in bs, "文档串锚点没了"
    mutated_doc = bs.replace(doc_anchor, "这个模块最不重要的设计".encode("utf-8"), 1)

    import_anchor = b"import math\n"
    assert import_anchor in bs, "import 段锚点没了"
    mutated_imports = bs.replace(import_anchor, b"import math  # noqa\n", 1)
    grown_block = bs.replace(import_anchor, b"import math\nimport os\n", 1)

    # ① ②：锚点版必须抓到正文与文档串的改动
    with pytest.raises(pytest.fail.Exception) as exc:
        _assert_body_equal(bs, mutated_body, "正文变体")
    assert "漂移" in str(exc.value)
    with pytest.raises(pytest.fail.Exception) as exc:
        _assert_body_equal(bs, mutated_doc, "文档串变体")
    assert "漂移" in str(exc.value)
    # ③：锚点版必须放得下 import 段的正当改动（否则豁免就是空的）
    _assert_body_equal(bs, mutated_imports, "import 段变体")

    # ④：行号版同样放行原地改 import
    _assert_equal_outside_block(bs, mutated_imports, "import 段变体", *block)
    # ⑤：行号版必须抓住「import 段挤进正文」
    with pytest.raises(pytest.fail.Exception) as exc:
        _assert_equal_outside_block(bs, grown_block, "import 段变长变体", *block)
    assert f"第 {IMPORT_BLOCK_LAST + 1} 行往后" in str(exc.value), (
        f"多出来的 import 行没有落进正文区被抓到，报错不符合预期：{exc.value}"
    )
    # 行号版也必须抓到正文改动（它和锚点版守的是同一半，不能比锚点版松）
    with pytest.raises(pytest.fail.Exception):
        _assert_equal_outside_block(bs, mutated_body, "正文变体", *block)


# ------------------------------------------- 3. DEFAULT_ROLE：登记过的有意差异


def test_default_role_difference_is_the_registered_one():
    """判定标准 3：WB=wengao、bs=standard，是**有意差异**，不许对齐。

    bs 侧一行没动（`bili_scribe/config.py:85` 仍是 `role: str = "standard"`），所以两侧
    默认角色必然不同。把两边的值都写成显式期望值：只断言「不等」的话，有人把 bs 也
    改成 wengao 时它照样通过，而那正是这次要防的事故。
    """
    from bili_scribe.config import LlmConfig

    wb = _wb_package()
    assert LlmConfig().role == "standard", "bs 的默认角色被动过——它不该改"
    assert wb.DEFAULT_ROLE == "wengao"
    assert wb.DEFAULT_ROLE != LlmConfig().role


def test_unknown_role_fallback_asymmetry_is_intentional():
    """bs 静默回落 / WB 严格抛错——同一合同的两面，标为**有意差异**（lead 指令）。

    bs：`get_role("bogus")` 退回 `STANDARD`，原件注释写着「不要让一个拼错的配置把总结
    卡死」——那是本地配置文件，卡死比降级更糟。
    WB：standard 已退役，而角色清单是界面下拉的数据源，悄悄降级会让用户以为自己选了
    某个角色、实际拿到另一个。所以这里**必须**抛 `UnknownRole`。
    这个不对称要写成用例并留档，别当 bug 报，也别把它「修成一致」。
    """
    from bili_scribe.core import roles as bs_roles

    wb = _wb_package()
    assert bs_roles.get_role("bogus") is bs_roles.STANDARD
    assert bs_roles.get_role(None) is bs_roles.STANDARD
    with pytest.raises(wb.UnknownRole):
        wb.get_role("bogus")
    with pytest.raises(wb.UnknownRole):
        wb.get_role(None)


# --------------------------------------------------- 4. 退役角色的可达性（A4）


def test_retired_standard_role_is_absent_from_the_public_surface():
    """退役角色不在公开面，且公开面 = 「两侧共有的那段（顺序不变）+ 权威层自造的尾巴」。

    **退役判据一个字没松**（`standard` 既不在清单里、又必须在 `RETIRED_ROLES` 里），
    变的是「清单等于什么」。task-24 时后段必须非空（`shipin` 是权威层自造的第一个角色）；
    task-31 把 `shipin` 合并进 `renhua`、把 `xueshu` 退役之后，**后段合法地变成空**，
    所以那句 `assert extras` 删掉了。这不是放宽：它当初拦的是「新角色没进清单」，
    而今天没有新角色要进清单；留着它等于要求产品永远得有一个自造角色。

    剩下的判据各管各的：

    1. **前段**必须逐字等于 `_shared_role_keys(wb)`（bs 键序去掉权威层退役的那些）。
       这段是两侧共有的，漂了就说明副本的角色顺序/内容被动过；而「退役集合少配一键」
       会让这段变长、公开面却没跟着变长 ⇒ 同样红。锚点是逐字节锁定的 bs 原件，
       所以现读不自证。
    2. **后段**只许是 bs 没有的角色（权威层自造的），并且必须排在后面。拦住两种坏法：
       把自造角色插到中间（前端下拉顺序变了，历史版本对比会错位），以及把 bs 的角色
       悄悄删掉（那样前段就短了）。
    3. `ROLE_KEYS` 与 `describe_roles()` 的顺序不许分叉（一个给机器、一个给界面）。
    """
    from bili_scribe.core import roles as bs_roles

    wb = _wb_package()
    keys = [item["key"] for item in wb.describe_roles()]
    bs_keys = {role.key for role in bs_roles.ALL_ROLES}

    shared = _shared_role_keys(wb)
    assert keys[: len(shared)] == list(shared), (
        f"共有角色那一段漂了（顺序或内容）：前 {len(shared)} 个期望 {list(shared)}，"
        f"实得 {keys[: len(shared)]}"
    )
    tail = keys[len(shared) :]
    assert all(key not in bs_keys for key in tail), (
        f"后段出现了 bs 已有的角色 {tail}：说明前段被删短了，不是 WB 自造的角色进来了"
    )

    assert "standard" not in keys
    assert "standard" in tuple(wb.RETIRED_ROLES)
    assert tuple(wb.ROLE_KEYS) == tuple(keys), "ROLE_KEYS 与 describe_roles() 的顺序分叉"


def test_get_role_rejects_retired_standard_but_role_label_does_not_raise():
    """取角色严格（拿不到就抛），贴标签宽松（界面要能显示历史行的角色名）。

    这两个函数的分野是真实需求，不是重复实现：
      - `run()` 拿到未知角色继续跑，会产出一版没人要求的总结，所以必须抛；
      - 版本列表要给**退役前留下的历史行**显示一个名字，抛异常等于把历史记录从
        界面上抹掉，所以 `role_label` 不许抛，且要写明「已停用」。
    """
    wb = _wb_package()
    with pytest.raises(wb.UnknownRole):
        wb.get_role("standard")

    label = wb.role_label("standard")
    assert isinstance(label, str) and label
    assert "已停用" in label, f"退役角色的标签要写明停用，实际是「{label}」"
    # 存活角色的标签就是它的名字——这条保证 role_label 不是只给退役角色打的补丁。
    assert wb.role_label("wengao") == wb.get_role("wengao").name


def test_configured_default_role_is_not_the_retired_one():
    """配置里的默认角色必须是**可用**角色——退役要退干净，包括默认值。

    这条现在是红的，而且是**用户可见的**红：`WorkbenchConfig.llm.role` 从 bili_scribe 的
    `LlmConfig` 继承，默认值仍是 `"standard"`；而 `summary_start`（service.py:2548）和
    `_summary_job` 都按 `payload.role or cfg.llm.role or DEFAULT_ROLE` 取值——cfg 里那个
    "standard" 排在 `DEFAULT_ROLE`（"wengao"）**前面**，于是没显式选角色的用户第一次点
    「生成总结」就会拿到「标准（standard）角色已停用，请选一个方法论角色」。

    修法两条（都在产品侧，本文件不代劳）：① WB 的 `LlmConfig` 覆写
    `role: str = "wengao"`；② service 在读到属于 `RETIRED_ROLES` 的配置值时回落
    `DEFAULT_ROLE`。第 ① 条更干净——留一个不可用的默认值在配置里，界面上的角色下拉
    和实际生成的角色会长期对不上。
    """
    from video_workbench.config import WorkbenchConfig

    wb = _wb_package()
    configured = WorkbenchConfig().llm.role
    assert configured not in wb.RETIRED_ROLES, (
        f"配置默认角色 {configured!r} 已退役：不选角色的用户会直接被拒，"
        "这是退役没退干净，不是测试写坏了"
    )
    assert configured in wb.ROLE_KEYS, f"配置默认角色 {configured!r} 不在可用清单里"


def test_adapter_rejects_retired_standard_with_a_validation_error():
    """适配层入口：`run(role="standard")` 必须是 `WBError(code="VALIDATION")`。

    A4 的运行时那一半——前面的用例锁数据面，这条锁「用户真的从界面选到它、或者老
    脚本带着旧角色名来调用」时会发生什么。client 一旦被调用就直接炸，所以这条同时
    守住「拒绝发生在发请求之前」，而不是「拒绝了但碰巧没跑到」。
    """
    from bili_scribe.config import AppConfig
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer

    class _NoCallClient:
        model = "stub"
        base_url = "http://stub.local/v1"
        configured = True

        def require(self):
            return None

        def chat(self, messages, **kwargs):
            raise AssertionError("退役角色不该走到模型请求")

    cfg = AppConfig()
    cfg.llm.role = "wengao"
    with pytest.raises(WBError) as exc:
        summarizer.run(
            {"title": "t"},
            [{"start_ms": 0, "text": "x"}],
            client=_NoCallClient(),
            cfg=cfg,
            ctx=_QuietCtx(),
            role="standard",
        )
    assert exc.value.code == "VALIDATION"
