"""字节锁路径不许被 git 规范化 —— `AGENTS.md` 第三节「三比」的自动闸。

为什么必须有这道闸（事故，不是假想）
================================================
`.gitattributes:5` 是 `* text=auto eol=lf`，本机 `core.autocrlf` 现读是 `true`。
这个组合会**静默改写**被 sha/字节锁住的文件：工作区是 CRLF 的文件，入库 blob 变 LF。
真发生过的：`文稿编辑.txt` 工作区 8479 字节、入库 blob 一度只有 **8273 字节**（少 206
个 CR）。别人 `git clone` 拿到 LF 版，于是「真源 sha256 登记表」（P1：六个文件的
文件名+字节数+sha256）与打包期的字节比对（`scripts/build_wb.py` 的 `verify()`）在
**别人的机器上**红成「真源被人改过」，而这台机器上一切正常。

第三节的核查法是「三比」：① 工作区字节数、② `git cat-file -s :路径`（索引 blob）、
③ `git archive HEAD | tar -x` 后逐字节 `cmp`。它原本只写在文档里、靠人自觉，规则表那
行写的是「（人工，暂无自动闸）」——**靠自觉的闸等于没闸**，下一个新增 sha 锁的人不会
记得跑。本文件就是那道闸。

四道判据（各拦一种坏法，互不替代）
================================================
J1 **清单现读 + 合同下限**。txt 名单现读自 `scripts/build_wb.py` 的
   `PROMPT_TXT_NAMES`（AST 读字面量，不执行脚本）；`.py` 名单现读自漂移闸
   `test_summary_drift.py` 的模块级 `Path` 常量，再并入合同点名的五个锁路径当下限。
   双向断言：盘上每篇 txt 都得在名单里（拦「加了第七篇忘了登记」），清单里每个名字
   都得在盘上（拦「登记了个不存在的文件，于是比对一路 skip 到绿」）。
J2 **入库不改字节**：工作区原始字节数 == 索引 blob 字节数（`:p`），未跟踪直接算红。
J3 **属性或无 CR，二者必居其一**：`git check-attr text` 现读为 `unset`（显式
   `-text`/`binary`）就放行；否则该文件的**原始字节**必须不含 `0x0D`。六篇 txt 走前半
   （它们字节里真有 CR），vendored 副本走后半（它们是 LF）。判据从 git 现读，不写死
   `.gitattributes` 的那行文本。
J4 **模拟 clone**：`git archive HEAD` 抽出锁路径逐字节 `cmp` —— 比 J2 更靠近「同事拿
   到什么」。

为什么 J2 比索引、J4 才比 HEAD
------------------------------------------------
本仓的常态是**落地未 commit**：索引与 HEAD 都是旧版，而工作区才是落地版
（`AGENTS.md` 第六节最后两条，2026-11-04 定的口径）。拿 HEAD 与工作区比会把「还没
提交」误报成「被规范化」——每次落地都假红。所以 J2 比 `:p`（下一次 commit 会带进去
的那一版）；J4 比 HEAD，但**只对相对索引干净的路径**要求一致，未提交的显式列出并跳过，
不假装测过。

比字节一律用原始字节
------------------------------------------------
`Path.read_text()` 会把 `\r\n` 折成 `\n` 再编码，同一文件因此有两种 sha：本仓
`settings.js` 是 CRLF（`sha256sum` 给 `18a9fc3a…`，`read_text()` 给 `4339f77f…`），
`prompt_source.py`/`mock.js` 是 LF 所以两种读法一致。这道闸专门拦 CRLF 规范化，自己
绝不能用文本读法算字节 —— 本文件所有字节比较都是 `read_bytes()` / `cat-file` /
`hash-object`，读源码给 AST 也是先 `read_bytes()` 再 decode。

牙齿（只准 `%TEMP%` 副本；`WB_LOCK_REPO` 只为牙齿存在）
------------------------------------------------
仓库根经 `_repo_root()` 取：默认是真仓库，`WB_LOCK_REPO` 可指到一份 Temp 副本。两条
坏法都只在副本里做：① 从副本的 `scripts/build_wb.py` 名单里摘掉一篇 txt；② 删掉副本
`.gitattributes` 里的 `-text` 行。默认路径下测的就是真仓库，这个口子不是放宽。
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tarfile
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import pytest

REAL_REPO = Path(__file__).resolve().parents[2]
PROMPT_REL = "video_workbench/resources/prompts"

#: 合同（task-33/37 判据 1）点名的 .py 锁路径下限。现读负责「将来多一个副本会不会
#: 漏进闸」，这份下限负责「现读那条链路万一读空了，闸不许变成零对象」。
PY_LOCK_FLOOR = (
    "video_workbench/core/summary/roles.py",
    "video_workbench/core/summary/summarizer.py",
    "video_workbench/core/summary/__init__.py",
    "video_workbench/infra/notecache.py",
    "video_workbench/core/prompt_source.py",
)


def _repo_root() -> Path:
    raw = os.environ.get("WB_LOCK_REPO", "").strip()
    return Path(raw).resolve() if raw else REAL_REPO


ROOT = _repo_root()


def _git(*args: str, binary: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(ROOT), input=binary, capture_output=True, check=False,
    )


def _git_out(*args: str, binary: bytes | None = None) -> str:
    proc = _git(*args, binary=binary)
    assert proc.returncode == 0, (
        f"git {' '.join(args)} 失败：{proc.stderr.decode('utf-8', 'replace')}"
    )
    return proc.stdout.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------- J1 清单现读
def manifest_txt_names() -> tuple[str, ...]:
    """`scripts/build_wb.py` 里 `PROMPT_TXT_NAMES` 的字面值（AST，不执行脚本）。

    不 import 是因为那脚本一加载就会去摸 PyInstaller 与 `dist/`；这里要的事实是「名单
    文件里写了什么」本身。
    """
    script = ROOT / "scripts" / "build_wb.py"
    assert script.is_file(), f"清单的真源不在了：{script}"
    tree = ast.parse(script.read_bytes().decode("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PROMPT_TXT_NAMES"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple) and value, "PROMPT_TXT_NAMES 必须是逐字文件名的非空元组"
            return tuple(value)
    raise AssertionError(
        "scripts/build_wb.py 里找不到 PROMPT_TXT_NAMES —— 名单换了 owner，请同步本闸"
    )


def prompts_dir() -> Path:
    directory = ROOT / PROMPT_REL
    assert directory.is_dir(), f"txt 真源目录不在盘上：{directory}"
    return directory


def drift_copy_locks() -> tuple[str, ...]:
    """.py 侧现读自漂移闸：import 它，收集它模块级的 `Path` 常量。

    不在这里重抄路径 —— 抄一次就有两个 owner，下次 vendor 加锁就会漏。
    """
    tests_dir = str(ROOT / "tests" / "wb")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    module = __import__("test_summary_drift")
    found: list[str] = []
    for value in vars(module).values():
        if isinstance(value, Path):
            try:
                rel = value.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                continue
            if rel.startswith("video_workbench/") and rel.endswith(".py"):
                if rel not in found:
                    found.append(rel)
    return tuple(found)


def _locked_or_broken() -> list[str]:
    """参数化列表：清单读失败时**不许**退化成零条，塞一个假路径让每条用例自己红。"""
    try:
        paths = sorted({f"{PROMPT_REL}/{n}" for n in manifest_txt_names()}
                       | set(drift_copy_locks()) | set(PY_LOCK_FLOOR))
        if paths:
            return paths
    except Exception as exc:  # noqa: BLE001 —— 这里就是要把它变成用例里的红
        print(f"清单读取失败：{exc}")
    return ["<锁清单读取失败>"]


LOCKED = _locked_or_broken()


def txt_locks() -> tuple[str, ...]:
    return tuple(f"{PROMPT_REL}/{n}" for n in manifest_txt_names())


# ---------------------------------------------------------------------- 属性现读
def attr_rules() -> list[str]:
    """.gitattributes 的有效行（去注释/空行），原样留着让调用方判 token。"""
    path = ROOT / ".gitattributes"
    assert path.is_file(), ".gitattributes 不在了：字节锁闸失去判据来源"
    return [ln.strip() for ln in path.read_bytes().decode("utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _rule_matches(rule: str, rel: str) -> bool:
    pattern = rule.split()[0]
    return fnmatch(rel, pattern) or fnmatch(PurePosixPath(rel).name, pattern)


def rules_for(rel: str) -> list[str]:
    return [rule for rule in attr_rules() if _rule_matches(rule, rel)]


def text_attr(rel: str) -> str:
    """git 自己算出来的 `text` 属性；`unset` = 显式关掉了转换（`-text` / `binary`）。"""
    return _git_out("check-attr", "text", "--", rel).split(":")[-1].strip()


def cr_count(rel: str) -> int:
    return (ROOT / rel).read_bytes().count(b"\r")


# ------------------------------------------------------------------ J1 的清单用例
def test_the_txt_manifest_is_read_live_and_is_not_empty():
    """J1 入口：名单现读得到、非空，且每一项都是逐字文件名（不是通配）。"""
    names = manifest_txt_names()
    assert names, "PROMPT_TXT_NAMES 空了 —— 这道闸的 txt 侧当场没有对象"
    assert all(name.endswith(".txt") for name in names), f"名单里有不是 .txt 的项：{names}"
    assert not any("*" in name or "?" in name for name in names), "名单必须逐字，通配会漏文件"


def test_every_prompt_file_on_disk_is_registered():
    """盘上每一篇 txt 都必须在名单里 —— 拦「加了第七篇忘了登记」。"""
    registered = set(manifest_txt_names())
    on_disk = {p.name for p in prompts_dir().glob("*.txt")}
    missing = sorted(on_disk - registered)
    assert not missing, (
        f"{PROMPT_REL} 里有未登记的真源：{missing}。它们不进名单就不会被打包期 verify() "
        "比字节，也不会被本闸覆盖 —— 这正是 sha 登记表最容易被绕开的形状"
    )


def test_every_registered_name_exists_on_disk():
    """反向：名单里不许留着盘上没有的名字（那种条目会让比对一路跳过去还看着是绿）。"""
    directory = prompts_dir()
    absent = [n for n in manifest_txt_names() if not (directory / n).is_file()]
    assert not absent, f"PROMPT_TXT_NAMES 里有盘上不存在的文件名：{absent}"


def test_the_python_lock_list_covers_the_contract_floor_and_what_drift_locks():
    """J1 的 .py 侧：合同下限五个必须在清单里，且漂移闸现读到的副本也都被覆盖。"""
    live = set(drift_copy_locks())
    assert live, "test_summary_drift.py 里没有 video_workbench 的 Path 常量了？"
    covered = set(LOCKED)
    missing_floor = [rel for rel in PY_LOCK_FLOOR if rel not in covered]
    assert not missing_floor, f"本闸漏了合同点名的锁路径：{missing_floor}"
    missing_live = sorted(live - covered)
    assert not missing_live, f"漂移闸锁着、本闸却没管的路径：{missing_live}"
    for rel in PY_LOCK_FLOOR:
        assert (ROOT / rel).is_file(), f"锁路径不在盘上：{rel}"


def test_the_runtime_prompt_dir_is_the_locked_directory():
    """产品运行时读的提示词目录必须就是被锁的那个目录（没设 `WB_PROMPTS_DIR` 时）。

    否则会出现「闸锁 A 目录、产品实际吃 B 目录」，那六篇的字节锁就成了摆设。
    """
    if os.environ.get("WB_PROMPTS_DIR", "").strip():
        pytest.skip("环境里设了 WB_PROMPTS_DIR：产品目录被显式改指，这条不适用（牙齿场景）")
    from video_workbench.core import prompt_source

    assert Path(str(prompt_source.prompt_dir())).resolve() == prompts_dir().resolve(), (
        "prompt_source.prompt_dir() 与字节锁清单的目录不是同一个"
    )


# ---------------------------------------------------------------------- J2 字节相等
@pytest.mark.parametrize("rel", LOCKED)
def test_worktree_bytes_equal_the_blob_git_would_commit(rel):
    """J2：工作区原始字节数 == 索引 blob 字节数；未跟踪也算红。

    一个没被 git 跟踪的「字节锁」对别人机器上的版本没有任何约束力 —— 本闸自己就是
    因为未跟踪而消失过一次。
    """
    path = ROOT / rel
    assert path.is_file(), f"锁路径不在盘上：{rel}"
    work = path.stat().st_size
    assert work > 0, f"{rel} 是 0 字节：空文件也能骗过「逐字节相等」，闸不能拿它当证据"
    proc = _git("cat-file", "-s", f":{rel}")
    if proc.returncode != 0:
        pytest.fail(
            f"{rel} 没有被 git 跟踪（`git cat-file -s :{rel}` 失败）。它现在是字节锁的对象"
            "却进不了仓库：别人 clone 拿不到，登记表与打包比对形同虚设 ⇒ 先 `git add`"
        )
    index = int(proc.stdout.decode())
    assert index == work, (
        f"{rel}：工作区 {work}B、入库会是 {index}B（差 {work - index}）。多半是 CRLF 被 "
        "`* text=auto eol=lf` + core.autocrlf 吃掉 CR —— 文稿编辑.txt 少 206 个 CR 就是这么来的"
    )


# ------------------------------------------------------------------ J3 属性 / 无 CR
@pytest.mark.parametrize("rel", LOCKED)
def test_each_locked_path_is_minus_text_or_cr_free(rel):
    """J3：要么 `text` 属性现读为 `unset`，要么原始字节不含 CR —— 二者必居其一。

    这就是「autocrlf=true + text=auto 的组合必须被挡住」的实际执行处：组合在场时
    （本机现读为 true，见下一条），只有这两种状态安全。
    """
    if text_attr(rel) == "unset":
        return
    cr = cr_count(rel)
    assert cr == 0, (
        f"{rel}：text 属性没有 unset，而工作区里有 {cr} 个 CR。commit 时 git 会改写它的"
        "字节，登记表里的 sha256 在别人机器上就对不上"
    )


def test_the_autocrlf_and_text_auto_combination_is_on_record():
    """把「危险组合此刻确实成立」钉成读数，防止整套判据静默空转。

    本机 `core.autocrlf` 现读 true、`.gitattributes` 里有 `* text=auto eol=lf`。若有人
    改了其中之一，本条会红着要求同步重评口径 —— 而不是让 J3 变成永真。
    """
    autocrlf = _git("config", "--bool", "--get", "core.autocrlf")
    on = autocrlf.returncode == 0 and autocrlf.stdout.strip() == b"true"
    has_auto = any("text=auto" in rule for rule in attr_rules())
    assert on and has_auto, (
        f"core.autocrlf={autocrlf.stdout.decode().strip() or '未设置'}、存在 "
        f"'* text=auto'={has_auto}：触发本闸的那个组合已不在场，请把本文件与 AGENTS.md "
        "第三节一起重评，别留着空转"
    )


def test_the_six_txt_are_protected_by_a_rule_not_by_luck():
    """六篇真源的 CR 是真的存在，保护必须来自显式规则，而不是「恰好没有 CR」。

    这条同时是 J3 的灵敏度证据：哪天有人删掉 `-text` 行又没改这六篇的字节，「不含
    CR」的兜底不会救它们 —— 它们本来就是 CRLF。
    """
    protected = 0
    for rel in txt_locks():
        rules = rules_for(rel)
        assert any("-text" in r or "binary" in r for r in rules), (
            f"{rel} 没有显式关掉转换（命中规则：{rules}）"
        )
        if cr_count(rel) > 0:
            protected += 1
    assert protected > 0, (
        "六篇 txt 现在一个 CR 都没有：J3 的 txt 侧已经退化成靠兜底，请核对真源与登记表"
    )


def test_the_clean_filter_actually_rewrites_an_unprotected_path():
    """灵敏度证据（一次真 git 动作，不碰工作区）：同一串 CRLF 字节换个路径问属性，结果不同。

    今天 J2/J3 全绿可能只是因为「大家都合规」。这条用 `git hash-object --path --stdin`
    现场证明规范化机制确实在工作：受 `-text` 保护的路径保住 CR，不受保护的路径丢掉 CR。
    比较的是 blob 内容哈希，不是文件大小文本。
    """
    payload = b"a\r\nb\r\n"
    guarded_rel = txt_locks()[0]
    naked_rel = "video_workbench/core/summary/roles.py"
    raw = _git_out("hash-object", "--no-filters", "--stdin", binary=payload)
    guarded = _git_out("hash-object", "--path", guarded_rel, "--stdin", binary=payload)
    naked = _git_out("hash-object", "--path", naked_rel, "--stdin", binary=payload)
    assert guarded == raw, (
        f"{guarded_rel} 的 clean 滤镜改掉了 CR（-text 规则没生效？）：J3 的判据来源已变"
    )
    assert naked != raw, (
        "属性为 text=auto 的路径竟然没丢 CR —— 说明这台机器的 core.autocrlf / "
        ".gitattributes 组合变了，J3/J4 的口径要重评（本条与上一条互为对照）"
    )


# ---------------------------------------------------------------------- J4 模拟 clone
def test_git_archive_of_head_matches_the_worktree_for_committed_paths():
    """J4（模拟 clone）：`git archive HEAD` 抽出来逐字节 cmp。

    只对「相对索引干净」的路径要求 clone == 工作区；未提交的改动**显式列出并跳过**，
    不假装测过 —— 本仓的常态就是落地未 commit。
    """
    head = set(_git_out("ls-tree", "-r", "--name-only", "HEAD").splitlines())
    committed = [rel for rel in LOCKED if rel in head]
    assert committed, "HEAD 里一个锁路径都没有 —— 这条已经空转"
    pending = [rel for rel in LOCKED if rel not in head]
    if pending:
        print("尚未进 HEAD（本轮跳过 clone 比对）：" + "、".join(pending))

    proc = _git("archive", "HEAD", "--", *committed)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    bad: list[str] = []
    # extractfile 是 TarFile 上的方法，不是 TarInfo 上的（`member.extractfile()` 会
    # AttributeError）。这里曾经一直抛自己的错、根本没比过字节 —— 修好之前这条判据
    # 是空的，别把它当成已经验证过。
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        names = {m.name.rstrip("/"): m for m in tar.getmembers() if m.isfile()}
        for rel in committed:
            if _git("diff", "--quiet", "--", rel).returncode != 0:
                continue  # 工作区有未提交改动：clone 版必然不同，那不是规范化
            member = names.get(rel)
            if member is None:
                bad.append(f"{rel}: archive 里没有这个文件")
                continue
            handle = tar.extractfile(member)
            extracted = handle.read() if handle else b""
            work = (ROOT / rel).read_bytes()
            if extracted != work:
                # bytes([13]) 而不是 b"\r"：这条闸的全部意义就是数 CR，写错一个字面量
                # 会让报错信息里的 CR 数永远是 0，看起来像「内容不同但不是规范化」。
                clone_cr = extracted.count(bytes([13]))
                work_cr = work.count(bytes([13]))
                bad.append(
                    f"{rel}: clone 版 {len(extracted)}B（CR {clone_cr}）"
                    f" != 工作区 {len(work)}B（CR {work_cr}）"
                )
    assert not bad, "别人 clone 到的是另一版：\n" + "\n".join(bad)


def test_the_index_blob_matches_the_worktree_when_the_path_is_staged_clean():
    """J2 的补充：干净路径连**内容**都要逐字节相同，不只字节数相同。

    同长度、不同内容（比如 CR 换成别的空白）不会被 J2 抓到，所以这里直接比
    `git cat-file blob :rel` 与 `read_bytes()`。
    """
    checked = 0
    for rel in LOCKED:
        if _git("diff", "--quiet", "--", rel).returncode != 0:
            continue
        proc = _git("cat-file", "blob", f":{rel}")
        assert proc.returncode == 0, f"{rel} 取不到索引 blob（没被跟踪？）"
        assert proc.stdout == (ROOT / rel).read_bytes(), f"{rel}：索引 blob 内容 != 工作区内容"
        checked += 1
    assert checked > 0, "所有锁路径都带着未提交改动：这条退化成空转，请在干净状态下重跑"


# ------------------------------------------------------- 文档一致 + 闸自己必须被跟踪
def test_the_agents_md_rule_table_names_this_gate_and_a_real_file():
    """AGENTS.md 第七节那行的拦截手段必须是真实存在的测试名，不许再写「人工，暂无自动闸」。

    文档指一个不存在的文件，比没文档更糟：下一个人会以为已经有闸，于是连三比都不跑。
    """
    text = (ROOT / "AGENTS.md").read_bytes().decode("utf-8")
    rows = [ln for ln in text.splitlines() if "不许被 git 规范化" in ln]
    assert rows, "AGENTS.md 里找不到那条字节锁规则（第七节规则表被改了？）"
    row = rows[0]
    assert "暂无自动闸" not in row, f"文档还写着没有自动闸，而本文件就是那道闸：{row}"
    assert Path(__file__).name in row, f"那一行没点名本闸的文件名：{row}"
    assert (ROOT / "tests" / "wb" / Path(__file__).name).is_file(), "文档点名的文件不在盘上"


def test_this_gate_file_is_tracked_by_git():
    """闸自己必须被跟踪 —— 直接教训。

    上一版这道闸是**未跟踪文件**，在一次「按开工前快照覆盖回写」的还原里被当垃圾删掉，
    一夜之间从全量里消失，而文档还在引用它。`git diff` 对未跟踪文件恒返回空且**不报错**
    （AGENTS.md 第六节），所以这里必须用 `git ls-files --error-unmatch` 显式判。
    """
    rel = f"tests/wb/{Path(__file__).name}"
    proc = _git("ls-files", "--error-unmatch", "--", rel)
    assert proc.returncode == 0, (
        f"本闸没被 git 跟踪（{proc.stderr.decode().strip()}）：它随时会在一次误删/换机/快照"
        f"还原里永久消失 ⇒ `git add {rel}`（只 add，不 commit）"
    )
