"""钉住镜像库那次真事故：**目录联接（junction）背后的内容不许被当成自己的文件跟踪**。

## 来历（2026-11-05，lead 同步镜像时）

镜像库 `videoworkbench` 根下的 `bili_scribe` 不是真目录，是指向 `third_party/bili-scribe`
的 **junction**（`scripts/link_bili_scribe.py` 生成、不入库），靠镜像特有的 `.gitignore`
两行 `/bili_scribe` 排除。lead 按主仓那份 `.gitignore` 覆盖过去（主仓不需要那两行），
`git add -A` 立刻把 junction 后面的 **24 个 bs 源文件**暂存成待新增——差一点把 bs 源码当
镜像自己的文件提交出去。他在 staged 列表里看见 `A bili_scribe/core/roles.py` 才发现，
`git restore --staged bili_scribe` + 补回那两行，盘上零改动（junction 完好）。

现有闸都不覆盖这个形状：`test_secret_paths_ignored.py` 管密钥文件、
`test_git_no_normalization_on_locked_paths.py` J1–J4 管字节锁件被 git 规范化、
`test_summary_prompt_source.py` P1 管那 6 个 txt 的 sha 登记表。
⇒ 缺的就是这条：**联接一旦被跟踪，说明排除规则没了**（或有人绕过排除硬 add）。

## 为什么不能写成「`git ls-files bili_scribe` 必须为空」

那句话在**原库**里恒假——原库的 `bili_scribe` 是真目录，24 个文件本来就该被跟踪。
镜像与原库共用这一份 `tests/wb`，所以判据必须按**工作树形状**分支，不按仓库名字、
也不按写死的名单。合法形状只有两种，第三种一律红：

- 真目录 ⇒ 跟踪面必须**非空**（空 = `git ls-files` 指错了仓库或判据坏了，不是安全）；
- 联接 ⇒ 跟踪面必须**为空**（非空 = lead 那次事故）。

这两半合起来才挡住两种假绿：判据恒真，以及拿「查不到」当「没泄漏」。

## 机制那一半单独常驻，并且**不经过**形状锁用的那个 helper

形状锁成立的前提是「`.gitignore` 那两行真的挡得住 junction 内容」——不能靠信仰。
本文件在 `tmp_path` 里造两个**各自独立**的最小 git 仓库（同一个仓库连跑两次会被索引残留
污染：没有 commit 的仓库里 `git restore --staged .` 是失败的，我第一次就混出过假读数），
一个不带排除行、一个带。机制那条数的是 `git diff --cached --name-only`（自己起进程，
**不调 `_tracked_under`**），这样两边各有独立的牙：git 语义变了 → 机制条红；
本仓的查询/判据坏了 → 形状条红。

## 牙齿（只准 Temp 插件改模块属性，禁止动工作区）与各自的期望红名单

- V1 `_is_reparse_point` 恒 False ⇒ **只红前提条**（那条自己断「刚造出来的 junction 必须被认成
  联接」）。形状条与机制条都不红：形状条走的是「零跟踪 + 已知真目录非空」那一支，前提坏了它
  读不出联接 ⇒ 落到真目录支、要求非空、实得 24 条 ⇒ 仍绿；机制条在前提句拆出去之后不再调探针。
- V2 `_is_reparse_point` 恒 True ⇒ 红三条：前提条（真目录被认成联接）、对照条（已知真目录被
  读成联接）、形状条（本仓 `bili_scribe` 被读成联接 ⇒ 要求零跟踪、实得 24 条）。
  红因各不相同，逐条写死在驱动器 `HINT` 里。
- V3 `_tracked_under` 恒 [] ⇒ **只有**形状条与那条已知真目录的对照条红，
  机制条必须仍绿（它不经过这个 helper）——这条就是「两半各自独立」的证据。
- V4 `_is_reparse_point` 只把 `bili_scribe` 认成联接、其余如实 ⇒ 形状条走联接支、读得到
  24 条 ⇒ 红；Temp 那个 `link` 不被认出 ⇒ 前提条也红；机制条**不红**（它只数暂存面）。
  ⇒ 这颗牙专门证明「只认错一个名字的探针」不会被机制条兜住。
- V5 `_staged_under` 恒 [] ⇒ 机制条与变更条一起红（两条都数暂存面，正当连带）。
- V6 把邻居 `test_secret_paths_ignored.py` 的 `_ignored_by_git` 对备份形状返回 False ⇒
  邻居红 3 条、本文件 0 红 ⇒ 一颗牙只咬一个判据，两个文件之间没有互相依赖。

所有期望红名单**一律以实测为准**：v3 第一版我按纸面推导把 MECH 写进 V1/V2/V4 的期望，
实测三条都不含它（前提句独立成 PROBE 之后，机制条不再重复那句断言），已按读数收口。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Windows 的 FILE_ATTRIBUTE_REPARSE_POINT：junction / symlink / mount point 都带这一位。
_REPARSE = 0x400

#: 本判据盯的那棵树：原库＝真目录且必须被跟踪；镜像＝junction 且必须零跟踪。
LINKED_TREE = "bili_scribe"

#: 已知真目录，用来对照「git 查询本身没坏」（它在任何一本仓库里都必须有跟踪面）。
KNOWN_REAL_TREE = "video_workbench"


def _is_reparse_point(p: Path) -> bool:
    """这条路径**自己**是不是重解析点（联接）。不去跟它指哪儿。"""
    st = p.stat(follow_symlinks=False)
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _tracked_under(root: Path, name: str) -> list[str]:
    """该仓库索引里 `name/` 下面的文件（跟踪面，不是工作区列表）。"""
    r = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r.returncode == 0, f"git ls-files 失败 rc={r.returncode}：{r.stderr.strip()[:200]}"
    return [line for line in r.stdout.splitlines() if line.strip()]


def _staged_under(root: Path, name: str) -> list[str]:
    """`git add` 之后索引里 `name/` 的条目——**故意不走 `_tracked_under`**，见模块 docstring。"""
    r = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r.returncode == 0, f"git diff --cached 失败：{r.stderr.strip()[:200]}"
    return [line for line in r.stdout.splitlines() if line.startswith(name + "/")]


def _win32_only(what: str) -> None:
    if sys.platform != "win32":
        pytest.skip(
            f"{what}：这台机器造不出 junction ⇒ **机制没跑，不是通过**（形状锁与平台无关，照常跑）"
        )


def _make_repo(repo: Path, *, exclude: bool, keep: bool = True) -> Path:
    """最小仓库：真目录 `src/` + 指向它的 junction `link/`，然后 `git add -A`。

    每变体必须新建独立仓库：没有 commit 的仓库里 `git restore --staged .` 会失败
    （HEAD 不存在），复用目录会把上一次的索引残留混进读数。
    """
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("B = 2\n", encoding="utf-8")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    if exclude:
        (repo / ".gitignore").write_text("/link\n", encoding="utf-8")
    link = repo / "link"
    if link.exists():
        if not keep:
            return repo  # 变更场景：只改 .gitignore，工作树与索引一个字节都不动
        os.rmdir(link)  # junction 只能整体摘掉；递归删会咬进目标目录
    mk = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(repo / "src")],
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert mk.returncode == 0, (
        f"造 junction 失败：{(mk.stdout or '')[:160]}{(mk.stderr or '')[:160]}"
    )
    for args in (["git", "init", "-q"], ["git", "add", "-A"]):
        r = subprocess.run(args, cwd=str(repo), capture_output=True, text=True, errors="replace")
        assert r.returncode == 0, f"{args} 失败：{r.stderr.strip()[:200]}"
    return repo


# ------------------------------------------------------------ 形状锁（本仓库）


def test_the_linked_tree_is_either_tracked_or_clean_never_both() -> None:
    """`bili_scribe` 只允许两种合法形状；第三种就是 lead 那次事故，第四种就是判据坏了。"""
    target = ROOT / LINKED_TREE
    assert target.exists(), (
        f"{LINKED_TREE} 不在 {ROOT} 下 ⇒ 这份 tests/wb 被搬到一个两种合法形状都不是的仓库，"
        "判据失去对象。要改这里得先确认镜像/原库的形状约定，不许静默放过。"
    )
    linked = _is_reparse_point(target)
    tracked = _tracked_under(ROOT, LINKED_TREE)
    print(f"本仓 {LINKED_TREE}：形状={'联接' if linked else '真目录'}，跟踪面 {len(tracked)} 条")

    if linked:
        assert tracked == [], (
            f"`{LINKED_TREE}` 是目录联接，但索引里有 {len(tracked)} 个文件"
            f"（前 5：{', '.join(tracked[:5])}）⇒ 排除规则被覆盖或有人硬 add，"
            "联接背后的源码正被当成自己的文件提交。"
            f"补 `.gitignore` 里的 `/{LINKED_TREE}` 行 + `git restore --staged {LINKED_TREE}`。"
        )
    else:
        assert tracked, (
            f"`{LINKED_TREE}` 是真目录却零跟踪 ⇒ 要么 `git ls-files` 指错了仓库，"
            "要么这棵树被整体摘掉过。拿「查不到」当「没泄漏」是这条锁唯一能被绕过的方式。"
        )


def test_the_git_query_works_on_a_known_real_dir() -> None:
    """对照条：已知真目录上查询必须有结果，否则形状锁的「绿」只是查询坏了。

    形状锁允许「联接 + 零跟踪」这一支——所以镜像里它天生绿。这一条不分仓库形状，
    任何一次把 `git -C` 指错地方、或者在错误索引上跑的改动都会先在这里红。
    """
    assert not _is_reparse_point(ROOT / KNOWN_REAL_TREE), (
        f"{KNOWN_REAL_TREE} 被读成联接 ⇒ 这台机器上的仓库形状已经和我们假设的不一样，"
        "形状锁的两种分支都不成立了，必须人工重评，别让判据继续假装能判"
    )
    tracked = _tracked_under(ROOT, KNOWN_REAL_TREE)
    assert tracked, f"已知真目录 {KNOWN_REAL_TREE} 查询结果为空 ⇒ `git ls-files` 这条路坏了"
    assert all(p.startswith(KNOWN_REAL_TREE + "/") for p in tracked[:5]), (
        f"跟踪面里出现不以 `{KNOWN_REAL_TREE}/` 开头的路径：{tracked[:5]} "
        "⇒ ls-files 的 pathspec 没生效，形状锁读的是整个仓库，不是那棵树"
    )


# ---------------------------------------------------------- 机制自证（Temp 仓库）


def test_junction_content_is_staged_unless_the_repo_excludes_it(tmp_path: Path) -> None:
    """形状锁的前提：排除行真挡得住，junction 内容确实会被 `git add -A` 吞进去。"""
    _win32_only("本用例要在 Windows 上造 junction")
    blind = _make_repo(tmp_path / "no-exclude", exclude=False)
    guarded = _make_repo(tmp_path / "with-exclude", exclude=True)

    staged_blind = _staged_under(blind, "link")
    staged_guarded = _staged_under(guarded, "link")
    assert len(staged_blind) == 2, (
        "不带排除行时 junction 后面两个文件应当被 `git add -A` 收进索引（这就是 lead 事故的机制），"
        f"实得 {staged_blind} ⇒ 要么 git 语义变了，要么夹具没真造出 junction，形状锁的前提不成立"
    )
    assert staged_guarded == [], (
        f"带 `/link` 排除行后仍有 {staged_guarded} 被跟踪 ⇒ 镜像那两行 `/bili_scribe` 挡不住，"
        "形状锁的修法口径是假的"
    )


def test_the_probe_distinguishes_a_junction_from_a_real_dir(tmp_path: Path) -> None:
    """前提独立成条：`_is_reparse_point` 分得开刚造出来的 junction 与它的真目录目标。

    不塞进机制条里——`_staged_under(root, name)` 在一个 root 上只有「被排除/没被排除」
    一个语义，同一条里既断「无排除行 ⇒ 2 条」又断别的形状，会让两种坏法互相掩盖
    （牙齿 v2 的 V4 就是这么露馅的）。
    """
    _win32_only("本用例要在 Windows 上造 junction")
    repo = _make_repo(tmp_path / "probe", exclude=True)
    assert _is_reparse_point(repo / "link"), (
        "刚造出来的 junction 没被认成联接 ⇒ 形状锁会走错分支，机制条的归因也不成立"
    )
    assert not _is_reparse_point(repo / "src"), (
        "真目录被认成联接 ⇒ 探针反向，形状锁会把有跟踪的树判成事故"
    )


def test_the_exclusion_line_is_the_only_difference_between_the_two_repos(tmp_path: Path) -> None:
    """上一条的红/绿只能归因于那一行 `.gitignore`，否则归因不成立。"""
    _win32_only("本用例要在 Windows 上造 junction")
    a = _make_repo(tmp_path / "only-differs" / "no-exclude", exclude=False)
    b = _make_repo(tmp_path / "only-differs" / "with-exclude", exclude=True)
    names_a = sorted(p.name for p in a.iterdir() if p.name != ".gitignore")
    names_b = sorted(p.name for p in b.iterdir() if p.name != ".gitignore")
    assert names_a == names_b, f"两仓工作树内容不一致：{names_a} vs {names_b} ⇒ 差异不是那一行"
    assert not (a / ".gitignore").exists(), "A 仓不该有 .gitignore"
    assert (b / ".gitignore").read_text(encoding="utf-8").strip() == "/link"
    ci = subprocess.run(
        ["git", "-C", str(b), "check-ignore", "--no-index", "-v", "--", "link/a.py"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert ci.returncode == 0 and "/link" in ci.stdout, (
        f"排除行没被 git 认账：rc={ci.returncode} out={ci.stdout.strip()!r}"
    )


def test_deleting_the_exclusion_line_reveals_the_junction_contents(tmp_path: Path) -> None:
    """lead 事故的**变更**形状：仓库本来健康，覆盖掉 `.gitignore` 后 junction 内容才现身。

    同一个仓库、同一个工作树，只删那一行再 `git add -A`。这条只数 `git diff --cached`，
    不读形状锁用的 `_tracked_under` ⇒ 两半各有独立的牙。
    """
    _win32_only("本用例要在 Windows 上造 junction")
    repo = _make_repo(tmp_path / "mutate", exclude=True)
    assert _staged_under(repo, "link") == [], (
        f"带 `/link` 排除行的新仓库本该零跟踪 link/，实得 {_staged_under(repo, 'link')} "
        "⇒ 前提不成立，「删行就现身」这条无从谈起"
    )
    (repo / ".gitignore").write_text("", encoding="utf-8")
    r = subprocess.run(
        ["git", "add", "-A"], cwd=str(repo), capture_output=True, text=True, errors="replace"
    )
    assert r.returncode == 0, f"git add 失败：{r.stderr.strip()[:200]}"
    revealed = _staged_under(repo, "link")
    assert len(revealed) == 2, (
        f"删掉 `/link` 那行后应当有 2 个文件现身（lead 事故里的 24 个就是同一机制），"
        f"实得 {revealed} ⇒ 形状锁声称要拦的事其实不会发生，判据在防一个不存在的风险"
    )
