"""把主仓同步到发布镜像 videoworkbench（只跟产品码 + 文档），并挡住我踩过的那次覆盖。

为什么要有这个脚本：镜像（github.com/cunnak/videoworkbench）只当发布镜像，测试只在
主仓写（两库同步口径 ③）。同步以前是我手工 `cp -p` + `git add -A`，**手工没有前置
检查**。2026-11-05 出过一次险情：`cp -p` 把主仓的 `.gitignore` 覆盖到镜像上时，连镜像
特有的两行（注释 + `/bili_scribe`，排除那个目录联接）一起盖掉了，于是 `git add -A` 当场
把 junction 后面 24 个 bili_scribe 源文件暂存成待新增。当时靠我肉眼在 commit 前发现；
`tests/wb/test_junction_content_not_tracked.py` 那条锁拦的是**已入库**的形状，拦不住
「覆盖发生但那一次没 commit」。这条前置检查落在唯一会制造覆盖的入口上。

同步范围只有一种：**两仓共同跟踪、且内容不同**的路径。
- 主仓独有（我工作树里的临时物、镜像故意不带的文件）→ **永不为它新建文件**（第一版就是
  这么数出 614 个「待复制」的，方向错）。只有「镜像以前跟踪过、现在没了」才算分叉并拦。
- 镜像独有的非 `bili_scribe/`、非 `third_party/` 路径 → 报成漂移，交人工判，不自动删。
- `.gitignore` 从可写集里硬排除，并要求镜像那份必须含 `/bili_scribe` 行。

不用 `git diff A..B`：镜像与主仓是两条分叉的提交线，跨线 rev range 直接
`fatal: Invalid revision range`（我第一版死在这）。这里按**字节**比对两边索引。

用法（控制台是 GBK，**必须带 PYTHONIOENCODING=utf-8**，否则中文乱码）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/sync_mirror.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/sync_mirror.py --apply
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/sync_mirror.py --apply --commit -m "chore: 同步主仓 <sha>"

镜像根默认 `%TEMP%/videoworkbench-new`，可用 `WB_MIRROR_ROOT` 覆盖（与 `WB_*` 扫描域开关同族）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
List = list                      # 只给上面两个新函数当注解用，不引入 typing 依赖

#: 镜像特有、且必须活过任何一次同步的行——今天被覆盖掉的正是这两行。
JUNCTION_MARKER = "/bili_scribe"

#: 任何情况下都不由同步写入的路径（镜像自己维护）。
NEVER_SYNC = {".gitignore"}

#: 比对时跳过的主仓顶层前缀：bili_scribe 在镜像里是 junction + submodule。
SKIP_PREFIXES = ("bili_scribe/", "third_party/")

#: 逐字节 vendored 副本 + 6 个真源 txt——同步脚本永远不许删、不许覆盖。
#: 这六个是「主仓已删」这一栏第一次就误报的东西（见 git() 里 core.quotepath=false 那条注释）。
LOCKED_PREFIXES = ("video_workbench/core/summary/", "video_workbench/infra/notecache.py",
                   "video_workbench/resources/prompts/")

#: 「镜像该有、但没有」的新文件只从这几条前缀里自动认领。其余主仓独有路径（我工作树里的
#: 临时物 `_tiku/`、`_conv_doc.ps1`、根目录那堆一次性脚本）一律不新建 —— 第一版把它当待复制，
#: 数出 614 项「待同步」，方向整个错了。
PRODUCT_PREFIXES = ("video_workbench/", "scripts/", "tests/wb/")
#: docs/ 刻意不在这里：镜像从第一笔起就是「tests/wb + WB 验收脚本 + 决策记录」的发布子集
#: （首条 commit 原文），docs 只同步**两边原本都跟踪**的那 3 份决策记录。若把 docs 放进来，
#: 一次同步会把 236 份验收截图/设计稿全搬进发布仓（2026-11-06 实测 275→525 个文件）。

#: 认领 tests/wb 时顺手解析 import，只认 **tests/wb 同目录的兄弟模块**。
#: 已知边界（如实写，不装作完备）：`wb_parity_tiers.py` 这种共享夹具能跟着判据一起进；
#: 跨目录的 `from tests.wb.x import` 与动态 import 不解析，那种要靠「搬进去能 collect」那条腿兜。
_IMPORT_RE = re.compile(r"^[ ]*(?:from|import)[ ]+([A-Za-z_][A-Za-z0-9_.]*)", re.M)


def mirror_root() -> Path:
    override = os.environ.get("WB_MIRROR_ROOT")
    if override:
        return Path(override)
    return Path(os.environ.get("TEMP", "/tmp")) / "videoworkbench-new"


def git(cwd: Path, *args: str) -> str:
    """跑 git。**必须带 core.quotepath=false**：这台机没设，`git ls-files` 会把六个中文
    文件名输出成 `"video_workbench/resources/prompts/\\345\\206\\231..."` 这种八进制转义串，
    拿它去 `Path(...).is_file()` 恒 False ⇒ 六个真源 txt 全被误判成「主仓已删」，
    `--apply` 就会去删镜像里那六份锁定的字节副本。这是一次真险情的形状，靠参数挡掉。"""
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args],
                       cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"[x] git {' '.join(args)} 在 {cwd} 失败 rc={r.returncode}\n{r.stderr}")
    return r.stdout


def tracked_lines(cwd: Path) -> list[str]:
    """**HEAD** 里的跟踪清单（不是索引）。仓库没有 commit 时返回空（不是错误）。

    为什么不用 `git ls-files`（=索引）：2026-11-05 实录 —— 成员被要求「落地就
    `git add`」，那份 `add` 只是给 lead 的 pathspec 提交用的暂存，**还没进 HEAD**。
    入口若按索引取交集，就会把「已 staged、未 commit」的那截当合法内容搬进
    给人下载的镜像。⇒ 面与门都只认 HEAD：谁 staged 了是主仓内部的事。
    """
    r = subprocess.run(["git", "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "HEAD"],
                       cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return []
    return [ln for ln in r.stdout.splitlines() if ln]


def junction_shape(mirror: Path) -> tuple[str, list[str]]:
    """返回 (形状, 阻塞项)。形状 ∈ {junction, dir, missing, other}。"""
    link = mirror / "bili_scribe"
    tracked = [ln for ln in tracked_lines(mirror) if ln.startswith("bili_scribe/")]
    if link.is_junction() or link.is_symlink():
        if tracked:
            return "link", [f"bili_scribe 是联接，但索引里有 {len(tracked)} 个文件：{tracked[:2]}"
                            "——先 `git restore --staged bili_scribe` 并查 .gitignore 的排除行"]
        return "link", []
    if link.is_dir():
        if not tracked:
            return "dir", ["bili_scribe 是真目录却一个文件都没跟踪——镜像不该长这样"]
        return "dir", []
    if not link.exists():
        return "missing", [f"{link} 不存在——镜像里先跑 scripts/link_bili_scribe.py"]
    return "other", [f"bili_scribe 是第三种形状，不敢替你判断：exists={link.exists()} "
                     f"is_dir={link.is_dir()} is_symlink={link.is_symlink()}"]


def _expand_imports(main: Path, mirror: Path, seeds: List) -> List:
    """把 tests/wb 判据的**同目录共享模块**（夹具、档位表）跟着一起认领。

    为什么不按前缀整批搬：`tests/wb/**` 整批搬会把一次性与未收口的东西也带进门面；
    只解析 seeds 自己 import 的那些，才能「缺了就整代搬」而范围不失控。
    现场：`tests/wb/test_summary_parity.py:13` 逐字 import wb_parity_tiers as TIERS
    （`test_summary_truncate.py` 同形），只搬判据不搬 `wb_parity_tiers.py`
    ⇒ 镜像里 pytest 收集中断，门面**制造**一条 ERROR。
    """
    out: List = []
    queue = list(seeds)
    seen: set = set()
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
        if not rel.startswith("tests/wb/") or not rel.endswith(".py"):
            continue
        blob = head_bytes(main, rel)
        if blob is None:
            continue
        for name in _IMPORT_RE.findall(blob.decode("utf-8", "replace")):
            sib = "tests/wb/" + name + ".py"
            if (head_bytes(main, sib) is not None and head_bytes(mirror, sib) is None
                    and sib not in seen):
                queue.append(sib)
    return out

def _new_product_targets(main: Path, mirror: Path, a_set: List, b_all: set) -> List:
    """主仓 HEAD 里有、镜像从未跟踪过的产品/判据文件 ⇒ 该搬，不是打一行 note 就跳过。

    这是本入口第三个自锁（2026-11-06 实测）：第一次同步报「共同跟踪 272 项，内容不同 1 项」，
    只搬了 `video_workbench/app/jobs.py`，随后镜像里 `tests/wb/test_job_failure_log.py`
    → No such file；`comm -3` 同时列出 `test_job_failure_log.py` 与 `test_version_owner.py`
    ⇒ **镜像带着产品改动、却没有守它的判据，而 pytest tests/wb 在镜像里照样全绿（根本收集不到）**。
    原实现把这一整类归进「主仓独有」只打一行 note —— 那是把「门面缺牙」实现成设计。
    锁定面继续硬挡：新增的副本/真源 txt 不许自动进门面。
    """
    fresh = [p for p in a_set
             if p not in b_all and p.startswith(PRODUCT_PREFIXES) and p not in NEVER_SYNC]
    seeds = [p for p in fresh if p.startswith("tests/wb/")]
    rest = [p for p in fresh if not p.startswith("tests/wb/")]
    out: List = []
    for p in _expand_imports(main, mirror, seeds) + rest:
        if p.startswith(LOCKED_PREFIXES):
            print("[x] 镜像缺 " + p + "，但它在逐字节锁定面里 —— 不自动搬，人工判")
        elif p not in out:
            out.append(p)
    return sorted(out)

def preflight(main: Path, mirror: Path, commit_ok: bool = False,
              applying: bool = False) -> tuple:
    """返回 (blockers, notes)。blockers 非空即拒绝同步，此时不写任何文件。"""
    blockers: list[str] = []
    notes: list[str] = []

    if not (mirror / ".git").exists():
        return ([f"镜像根不是一个 git 仓库：{mirror}"], notes)

    shape, why = junction_shape(mirror)
    notes.append(f"镜像 bili_scribe 形状 = {shape}")
    blockers += why
    if shape == "missing":
        blockers.append("镜像根下没有 bili_scribe 入口：产品码 import 它，同步过去也跑不起来")

    m_gi, a_gi = mirror / ".gitignore", main / ".gitignore"
    if not m_gi.is_file():
        blockers.append("镜像没有 .gitignore")
    else:
        lines = m_gi.read_text(encoding="utf-8", errors="replace").splitlines()
        if JUNCTION_MARKER not in lines:
            blockers.append(
                f"镜像 .gitignore 缺 {JUNCTION_MARKER!r} 行——此刻覆盖它（或直接 add -A）"
                "就会让 junction 后面的源文件现身索引")
        if a_gi.is_file() and a_gi.read_bytes() != m_gi.read_bytes():
            notes.append(f"两侧 .gitignore 不同（预期镜像多出 junction 排除行）；它已从可写集硬排除")

    # 脏面分三类，**不许一刀切放行也不许一律拦**：
    #   (a) gitlink 自己：上次同步对齐了 checkout 却忘了 commit，本身就是这一笔要提交的东西。
    #   (b) 其余脏文件：若**逐字节等于主仓 HEAD 那一版**，那是「上一次 --apply 把文件写完了、
    #       最后才发现缺 -m」留下的中断现场（第①件修之前天天发生）—— 认识它、打 [resume]、
    #       允许带 -m 补提交；有任何一个不等于 HEAD 版就仍然拒绝。
    #   (c) submodule 内部脏：单独报，绝不与 (a) 混成一句「放行」。
    dirty = [ln for ln in git(mirror, "status", "--porcelain").splitlines() if ln]

    # porcelain v1 是**两列**状态（X=索引侧、Y=工作区侧）。旧版写 startswith("M ")
    # 只看工作区那一位：`MM`（索引与工作区都改过）、`A `、` D` 全都不匹配，
    # 于是 gitlink 掉进 rest 被当「不等于 HEAD 的文件」，报错信息完全误导。
    # 实测形状（本轮哨兵）：M  / M M / MM / A  /  D 五种都要认得。
    def _split(ln: str) -> tuple[str, str]:
        return (ln[:2], ln[3:])

    gitlink = [ln for ln in dirty if _split(ln)[1] == "third_party/bili-scribe"]
    rest = [ln for ln in dirty if ln not in gitlink]
    if gitlink:
        sm_dirty = [ln for ln in git(mirror / "third_party" / "bili-scribe",
                                     "status", "--porcelain").splitlines() if ln]
        if sm_dirty:
            blockers.append(f"gitlink 变了且 submodule 内部也有 {len(sm_dirty)} 行未提交："
                            + chr(10) + "      ".join(sm_dirty[:5]))
        else:
            notes.append(f"镜像只有 gitlink 待提交（{len(gitlink)} 行）——视为本笔要修的东西")
    if rest:
        resumable, not_head = [], []
        for ln in rest:
            code, body = _split(ln)
            if "->" in body:                       # rename：形状不认识，不敢替你判断
                not_head.append(body + "（rename，不认识）")
                continue
            path = body.strip().strip(chr(34))
            if path.endswith("/"):
                # 未跟踪目录（`?? dir/`）：里面是什么我们不知道，不认作中断现场。
                not_head.append(path + "（未跟踪目录，不认识）")
                continue
            want = head_bytes(main, path)
            work = (mirror / path).read_bytes() if (mirror / path).is_file() else None
            # 这里**只比工作区**，不比索引。理由是本入口在提交前一定会
            # `git add -A -- <本次 paths>`：add 会把工作区内容写进索引，所以
            # 「工作区 == 主仓 HEAD 版」才是这一笔最终会提交进去的内容。
            # 我第一版在这加了索引比较，哨兵 L8 当场把它证伪 —— 一个完全合法的
            # resume 现场（工作区已按 HEAD 写好、索引还是旧的）被读成
            # 「索引不等于 HEAD 版」而拒掉。索引是否真的对齐，改在 add **之后**
            # 校验（见 main 的 `idx_check`），那里才是真正决定提交内容的时刻。
            if want is None:
                not_head.append(path + "（主仓 HEAD 里没有，是删除或未入库）")
            elif work != want:
                not_head.append(path + f"（工作区不等于主仓 HEAD 版，状态 {code}）")
            else:
                resumable.append(path)
        if not not_head and commit_ok:
            notes.append(f"[resume] 认出上一次同步留下的中断现场：{len(resumable)} 个文件在工作区与索引里"
                         "都已按主仓 HEAD 写好 —— 带 -m 即可把这一笔补提交完")
        elif not not_head and applying:
            blockers.append(
                f"镜像脏 {len(resumable)} 行且都等于主仓 HEAD 版，但这次不是提交（缺 -m）："
                "要么带 --commit -m 补完这一笔，要么先弄清这是谁的在途")
        elif not not_head:
            # 纯 dry-run：这不是「拒绝同步」，是「要提交才能清掉」。报成 note，
            # 否则在全部 resumable 的镜像上跑无 --apply 会被挡在 preflight 里，
            # 看起来像工具死锁（评审 B-08）。
            notes.append(
                f"[resume] 镜像有 {len(resumable)} 个文件已按主仓 HEAD 写好但未提交："
                "带 --apply --commit -m 可补完这一笔；这次只看计划，不写入")
        else:
            blockers.append(
                f"镜像工作区不干净（{len(rest)} 行），其中 {len(not_head)} 行**不等于**主仓 HEAD 版"
                "——那是别人的在途或删除，不认作中断现场：" + chr(10) + "      ".join(not_head[:5]))
    return blockers, notes


def head_bytes(repo: Path, rel: str) -> bytes | None:
    """取 **HEAD 里**那一版（不是索引、更不是工作区）。None = HEAD 里没有这个路径。

    为什么不用 `git cat-file -e` 判在不在，也不用读 stderr 文案（两轮哨兵各推翻一版）：
      1) rc 语义：`git cat-file -e "HEAD:"` 空路径 **rc=0**（指向根 tree）⇒ 用法错会被读成「在」。
      2) 文案白名单：旧版只认 `exists on disk`，实测漏了第三种 128 文案
         `fatal: path 'x' does not exist in 'HEAD'`（路径既不在 HEAD、磁盘上也没有）
         ⇒ 直接抛 RuntimeError，哨兵 L6b 当场炸。
      3) 更深一层：`git show HEAD:docs`（目录）**rc=0 并吐 tree 内容**、`git show HEAD:`（空）
         同样 rc=0 ⇒ 「能读出字节」根本不等价于「是个 blob」。所以这里改用
         `git cat-file --batch-check` 直接问**对象类型**：blob 才给字节，tree/commit/没有 都给 None。
    空路径与目录在入口先拦（保留显式报错），其余一律按类型判，不再依赖任何英文文案。
    """
    if not rel or rel.endswith("/"):
        raise RuntimeError(f"--from-head/--adopt 要的是文件路径，不是目录或空路径：{rel!r}")
    # 用 `-t` 而不是 `--batch-check`：后者要求对象走 stdin，命令行传参会直接
    # `fatal: batch modes take no arguments`（rc=129）—— 我上一版就死在这。
    chk = subprocess.run(["git", "-c", "core.quotepath=false", "cat-file", "-t",
                          f"HEAD:{rel}"], cwd=str(repo), capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    out = (chk.stdout or "").strip()
    if out == "blob":
        res = subprocess.run(["git", "-c", "core.quotepath=false", "show", f"HEAD:{rel}"],
                             cwd=str(repo), capture_output=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"HEAD:{rel} 的 batch-check 说是 blob，git show 却 rc={res.returncode}："
                f"{(res.stderr or b'').decode('utf-8', 'replace')[:200]} —— 不一致，不当成「没有」读")
        return res.stdout
    err = chk.stderr or ""
    # 第三种 128 文案（L4 实测）：路径在磁盘上、但不在 HEAD ——
    #   fatal: path 'new.py' exists on disk, but not in 'HEAD'
    # 只认前两种会把「未入库的 adopt」读成「读不懂的形状」并抛 RuntimeError（rc=1），
    # 而契约是 rc=2 + 零写入。
    known_missing = (("not a valid object name" in err) or ("does not exist" in err)
                     or ("but not in" in err))
    if chk.returncode != 0 and not known_missing:
        raise RuntimeError(
            "git cat-file -t HEAD:" + rel + " rc=" + str(chk.returncode) +
            " out=" + out[:200] + " err=" + err[:200] +
            " —— 读不懂的形状不当成「HEAD 里没有」读")
    return None


def index_bytes(repo: Path, rel: str) -> bytes | None:
    """取**索引里**那一版（暂存区）。None = 索引里没有这个路径。

    为什么 resume 判据必须读索引而不只读工作区（本轮评审 B-01 实测形状）：
    `git commit -- <path>` 提交的是**索引内容**，不是工作区内容。若镜像里那个文件
    处于 `M `（工作区改了、索引还是旧的）或 `MM`（两边都改过），只比工作区字节
    会看到「等于主仓 HEAD 版」于是放行，而实际提交进去的是索引里的旧版 ——
    这一笔提交自己就造出一个新的对不上账现场。
    """
    if not rel or rel.endswith("/"):
        raise RuntimeError(f"索引读取要的是文件路径，不是目录或空路径：{rel!r}")
    chk = subprocess.run(["git", "-c", "core.quotepath=false", "cat-file", "-t", f":{rel}"],
                         cwd=str(repo), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    out = (chk.stdout or "").strip()
    if out == "blob":
        res = subprocess.run(["git", "-c", "core.quotepath=false", "show", f":{rel}"],
                             cwd=str(repo), capture_output=True)
        if res.returncode != 0:
            raise RuntimeError(
                f":{rel} 的 cat-file 说是 blob，git show 却 rc={res.returncode}："
                f"{(res.stderr or b'').decode('utf-8', 'replace')[:200]} —— 不一致，不当成「索引里没有」读")
        return res.stdout
    if chk.returncode != 0 and ("not a valid object name" in (chk.stderr or "")
                                or "does not exist" in (chk.stderr or "")
                                or "but not in" in (chk.stderr or "")):
        return None
    if chk.returncode != 0:
        raise RuntimeError(
            f"git cat-file -t :{rel} rc={chk.returncode} out={out[:200]} "
            f"err={(chk.stderr or '')[:200]} —— 读不懂的形状不当成「索引里没有」读")
    return None


def content_targets(main: Path, mirror: Path) -> dict:
    """按两边 **HEAD** 取交集，逐条比 HEAD 字节。工作区与索引都不参与判断。"""
    a_set = tracked_lines(main)
    b_set = tracked_lines(mirror)
    b_all = set(b_set)
    common = [p for p in a_set if p in b_all and not p.startswith(SKIP_PREFIXES)]
    targets = [p for p in common
               if p not in NEVER_SYNC
               and head_bytes(main, p) is not None
               and head_bytes(main, p) != head_bytes(mirror, p)]
    # 主仓独有：只报数，不新建。
    a_only = [p for p in a_set if p not in b_all and not p.startswith(SKIP_PREFIXES)]
    # 镜像独有（非 junction / 非 submodule 挂载点）：算漂移，人工判。
    b_only = [p for p in b_set if p not in set(a_set) and not p.startswith(SKIP_PREFIXES)]
    # 两边 HEAD 都列过、但主仓 HEAD 已删掉（staged 删除不算）→ 同步删除。
    gone = [p for p in common if head_bytes(main, p) is None]
    # 镜像从未跟踪、但主仓 HEAD 已入库的产品/判据文件：以前这里只打一行 note，
    # 于是「同步成功」与「门面缺牙」同时成立（见 _new_product_targets 的实录）。
    new_targets = _new_product_targets(main, mirror, a_set, b_all)
    targets = targets + [p for p in new_targets if p not in targets]
    a_only = [p for p in a_only if p not in new_targets]
    return {"targets": targets, "new_targets": new_targets, "main_only": a_only, "mirror_only": b_only, "deleted": gone,
            "common_n": len(common)}


def _narrow(cats: dict, exclude: list[str], only: list[str]) -> dict:
    """按 --exclude / --only 过滤「内容不同」与「同步删」两栏。

    为什么要有这个参数（2026-11-05 实录）：入口原本没有挑选能力，任何一次
    `--apply` 都会把「未入库的在途改动」和「已入库的改动」混成一笔账搬进
    给人下载的发布镜像。过滤只作用于**要写的面**；上方报数仍打全量，
    让人看见被挑掉了几项 —— 报数与写入分叉，就是这里的记账口径。
    """
    def keep(p: str) -> bool:
        if only and p not in only:
            return False
        if any(p == e or p.startswith(e.rstrip("/") + "/") for e in exclude):
            return False
        return True

    cats["dropped_from_sync"] = [p for p in cats["targets"] + cats.get("new_targets", [])
                                  + cats["deleted"] if not keep(p)]
    cats["targets"] = [p for p in cats["targets"] if keep(p)]
    cats["new_targets"] = [p for p in cats.get("new_targets", []) if keep(p)]
    cats["deleted"] = [p for p in cats["deleted"] if keep(p)]
    return cats


def rel_exists_in_head(main: Path, rel: str) -> bool:
    """「这个**文件**在不在 HEAD 里」。判据只走 `head_bytes`，不用 `git cat-file -e`。

    事故实录（本轮 L6c，我自己造的）：我第一版拿 `cat-file -e HEAD:<路径>` 的 rc 当
    「在/不在」用，**rc 语义在盘上被否证** —— 我这一棵树上现跑：

        $ git cat-file -e "HEAD:"            → rc=0    # 根 tree，语法合法
        $ git cat-file -e "HEAD:docs"        → rc=0    # **目录也算「在」**
        $ git cat-file -e "HEAD:AGENTS.md"   → rc=0    # 真文件
        $ git cat-file -e "HEAD:不存在的文件"  → rc=128

    ⇒ 空路径与目录都会返回 0，「在」不等于「是个可搬的文件」，写成 rc==0 就判在
    是把用法错读成结论（AGENTS 第一节那条 `rc∉(0,1)` 的老坑换了个 rc 刻度重演）。
    `head_bytes` 的判据是「**能不能读出字节**」：空路径/以 `/` 结尾在入口就抛，
    目录走 `git show` 那条可解释的 128 读成 None —— 与 `--adopt` 分支同一个读者，
    不留两个函数对同一问题各给答案。
    """
    if not rel or rel.endswith("/"):
        raise RuntimeError(f"--from-head/--adopt 要的是文件路径，不是目录或空路径：{rel!r}")
    return head_bytes(main, rel) is not None


def apply_sync(main: Path, mirror: Path, targets: list[str], deleted: list[str]) -> list[str]:
    done: list[str] = []
    for rel in targets + deleted:
        if rel.startswith(LOCKED_PREFIXES):
            raise RuntimeError(f"拒绝碰逐字节锁定的路径：{rel}（副本与 6 个真源 txt 不归同步管）")
    for rel in targets:
        blob = head_bytes(main, rel)
        if blob is None:
            raise RuntimeError(f"主仓 HEAD 里没有 {rel}，不该进 targets")
        (mirror / rel).parent.mkdir(parents=True, exist_ok=True)
        (mirror / rel).write_bytes(blob)      # 写 **HEAD 版**，不是工作区版、不是索引版
        done.append(rel)
    for rel in deleted:
        dst = mirror / rel
        if dst.exists():
            dst.unlink()
            done.append(f"del {rel}")
    return done


def align_gitlink(mirror: Path, main_head: str) -> tuple[bool, str]:
    sm = mirror / "third_party" / "bili-scribe"
    if not (sm / ".git").exists():
        return True, "镜像没挂 submodule，跳过 gitlink"
    def local_type() -> str:
        r = subprocess.run(["git", "cat-file", "-t", main_head], cwd=str(sm),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return (r.stdout or "").strip()

    # 先问本地有没有这个 commit：有就不 fetch。
    # 实录（2026-11-06）：真镜像同步卡死 120s，进程停在 git fetch -q origin ——
    # 目标 commit 5a8373a 早就在 submodule 本地（就在 HEAD 上），这次网络往返纯属多余。
    # 发布镜像经常在离网/代理不通的机器上同步，fetch 必须有超时且只在本地确实缺对象时才做。
    if local_type() != "commit":
        try:
            subprocess.run(["git", "fetch", "-q", "origin"], cwd=str(sm),
                           capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return False, (f"镜像的 bili-scribe 里没有 {main_head[:8]}，"
                           "fetch origin 60s 超时——先在能联网的机器上把主仓 push 并同步 submodule")
    if local_type() != "commit":
        return False, f"镜像的 bili-scribe remote 里找不到主仓 HEAD {main_head[:8]}——先把主仓 push"
    git(sm, "checkout", "-q", main_head)
    return True, f"gitlink -> {main_head[:8]}"


def norm_rel(p: str) -> str:
    """命令行路径归一化成仓库相对 posix 路径（单/双反斜杠同一口径）。"""
    return p.replace(chr(92), "/").lstrip("./")


def early_arg_check(args) -> list:
    """在任何写入之前把参数层面的错收干净。返回错误清单，空 = 通过。

    为什么单独一个函数（2026-11-06 实测）：旧版把 -m 检查写在文件复制与 gitlink 对齐**之后**，
    于是 `--apply --commit` 忘带 -m 会把镜像写成「5 个文件已按主仓 HEAD 写好 + gitlink 已动、
    一笔未 commit」，再跑一次它自己的 preflight 又把这种脏态拒在外头 ——
    **工具能进入一个它自己不接受的状态、且没有恢复入口**。AGENTS 第六节写死「同步只准走这个入口」，
    那时只能退回手工 `git add` + `commit`，正是这个入口要消灭的写法。
    """
    errs: list = []
    if (args.adopt or args.from_head) and not args.apply:
        # 实测形状：这两个参数只在 --apply 分支里被处理；不带 --apply 时它们**不生效**，
        # 而输出会写「adopt … → 镜像」骗人。
        errs.append("--adopt/--from-head 必须配 --apply（不带 --apply 不写入）")
    if args.commit and not args.apply:
        errs.append("--commit 必须配 --apply（只提交不写入会留下一笔对不上账的 commit）")
    if args.commit and not args.message.strip():
        errs.append("--commit 需要 -m 给镜像那一笔的 message —— 现在拒绝，还没写任何文件")

    for p in list(args.adopt) + list(args.from_head):
        p = norm_rel(p)
        if not p or p.endswith("/"):
            errs.append("路径要的是文件，不是目录或空值：" + repr(p))
            continue
        if p in NEVER_SYNC or p.startswith(SKIP_PREFIXES):
            errs.append("拒绝 " + p + "（镜像自己维护 / junction 范围）")
            continue
        if p.startswith(LOCKED_PREFIXES):
            errs.append("拒绝 " + p + "（逐字节锁定面：副本与真源 txt 不归同步管）")
            continue
        if not (ROOT / p).is_file():
            errs.append(p + " 在主仓工作区不存在")
            continue
        if head_bytes(ROOT, p) is None:
            errs.append(p + " 还没进主仓 HEAD —— 本入口只搬已入库内容，未 commit 的一律不进门面"
                        "（要它进镜像请先在主仓 commit）")
    return errs

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="主仓 → 发布镜像的同步入口（带前置检查）")
    ap.add_argument("--apply", action="store_true", help="真的复制文件并对齐 gitlink")
    ap.add_argument("--adopt", action="append", default=[], metavar="PATH",
                    help="显式认领某个「主仓独有」路径进镜像（镜像从未跟踪它；"
                         "同步本身只走交集，所以这类要人工点名，不自动新建）")
    ap.add_argument("--commit", action="store_true", help="同步后用显式 pathspec 提交（须配 --apply）")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATH",
                    help="这次同步**整体跳过**某个共同跟踪路径（可重复）。注意：跳过 = 镜像"
                         "保持它**现在这一版**，不是保持主仓已入库那一版 —— 若那个文件在"
                         "被跳过期间于主仓又入库过改动，镜像就会静默落后一个 commit（本轮"
                         "真发生过）。只想挡在途改动、又要镜像拿到已入库那版，用 --from-head")
    ap.add_argument("--from-head", action="append", default=[], metavar="PATH",
                    help="把这个路径的**主仓 HEAD 版**（已入库、不含工作区未提交改动）写进"
                         "镜像。用在「某人有在途未入库改动，但镜像里那份已经旧了一个 commit」"
                         "这种现场 —— 这才是 --exclude 该配的搭档，光排除会把旧版留在门面里")
    ap.add_argument("--only", action="append", default=[], metavar="PATH",
                    help="白名单：只同步点名的路径（可重复），其余全部跳过。与 --exclude "
                         "同时给时先 only 再 exclude")
    ap.add_argument("-m", "--message", default="", help="镜像那一笔的 commit message")
    args = ap.parse_args()

    mirror = mirror_root()
    # 第①件：参数校验必须在 preflight 与任何写入之前 —— 先验后写，拒绝即零写入。
    bad = early_arg_check(args)
    if bad:
        for b in bad:
            print("[x] " + b)
        return 2
    blockers, notes = preflight(ROOT, mirror, commit_ok=bool(args.commit),
                                applying=bool(args.apply))
    for n in notes:
        print(f"[note] {n}")
    if blockers:
        print("[x] 前置检查不过，拒绝同步：")
        for b in blockers:
            print(f"    - {b}")
        return 1

    t = content_targets(ROOT, mirror)
    if args.exclude or args.only:
        t = _narrow(t, args.exclude, args.only)
        if t["dropped_from_sync"]:
            print(f"[note] 按 --exclude/--only 跳过 {len(t['dropped_from_sync'])} 项（不写入镜像）："
                  + "、".join(t["dropped_from_sync"]))
    print(f"[ok] 前置检查通过：共同跟踪 {t['common_n']} 项，其中内容不同 **{len(t['targets'])}** 项")
    print(f"     主仓独有（不新建）{len(t['main_only'])} / 镜像独有（漂移，人工判）{len(t['mirror_only'])} "
          f"/ 主仓已删（同步删）{len(t['deleted'])}")
    for p in t["targets"][:25]:
        print(f"    sync {p}")
    if len(t["targets"]) > 25:
        print(f"    …（共 {len(t['targets'])}）")
    for p in t["mirror_only"][:10]:
        print(f"    ONLY-IN-MIRROR {p}")
    if t["main_only"]:
        # 「主仓有、镜像没跟踪」本身**不是**危险：同步只走两边交集，镜像少一个文件不会
        # 被覆盖或写坏（我第一版把它当 rc=3 硬拦，被哨兵夹具证伪：一个只装 3 个文件的
        # 小仓不该被判成产品树落后）。真正危险的那一半是：**这文件镜像以前跟踪过、后来
        # 被删**——那才意味着两边产品树正在分叉。所以只把「曾经跟踪过」的那批报成阻断。
        ever = set(git(mirror, "log", "--diff-filter=D", "--name-only", "--pretty=format:",
                       "--", "video_workbench", "docs", "tests").splitlines())
        regressed = sorted(p for p in t["main_only"] if p in ever)
        if regressed:
            print(f"[x] 主仓有 {len(regressed)} 个路径**镜像以前跟踪、现在没了**（两边产品树正在分叉）："
                  f"{regressed[:5]}")
            return 3
        print(f"[note] 主仓独有 {len(t['main_only'])} 个路径镜像从未跟踪（同步只走交集，不新建）")

    if not args.apply:
        print("[dry] 未写入。要同步加 --apply。")
        return 0

    adopted = []
    for p in args.adopt:
        p = norm_rel(p)
        if p in NEVER_SYNC or p.startswith(SKIP_PREFIXES):
            print(f"[x] --adopt 拒绝 {p}（镜像自己维护 / junction 范围）")
            return 2
        if not (ROOT / p).is_file():
            print(f"[x] --adopt 的 {p} 在主仓不存在")
            return 2
        blob = head_bytes(ROOT, p)
        if blob is None:
            print(f"[x] --adopt 的 {p} 还没进主仓 HEAD —— 本入口只搬已入库内容，"
                  f"未 commit 的东西一律不进门面（要它进镜像请先在主仓 commit）")
            return 2
        dst = mirror / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
        adopted.append(p)
        print(f"[ok] adopt {p} → 镜像（HEAD 版）")

    heads = []
    for p in args.from_head:
        p = norm_rel(p)
        if p in NEVER_SYNC or p.startswith(SKIP_PREFIXES):
            print(f"[x] --from-head 拒绝 {p}（镜像自己维护 / junction 范围）")
            return 2
        if rel_exists_in_head(ROOT, p):
            blob = head_bytes(ROOT, p)
            (mirror / p).parent.mkdir(parents=True, exist_ok=True)
            (mirror / p).write_bytes(blob)
            heads.append(p)
            print(f"[ok] from-head {p} → 镜像（HEAD 版，不含工作区/索引在途改动）")
        else:
            print(f"[x] --from-head 的 {p} 不在主仓 HEAD 里（未入库的文件只能走 --adopt）")
            return 2

    done = apply_sync(ROOT, mirror, t["targets"], t["deleted"]) + \
        [f"adopt {p}" for p in adopted] + heads
    print(f"[ok] 处理 {len(done)} 项")
    main_head = git(ROOT, "rev-parse", "HEAD").strip()
    ok, link_msg = align_gitlink(mirror, main_head)
    print(f"[{'ok' if ok else 'x'}] {link_msg}")
    if not ok:
        return 2

    if args.commit:
        rels = []
        for d in done:
            if d.startswith("del "):
                rels.append(d[4:])
            elif d.startswith("adopt "):
                rels.append(d[6:])
            else:
                rels.append(d)
        # **删除也必须进 pathspec**。旧版用 `(mirror/p).exists() or (ROOT/p).exists()` 过滤：
        # 同步删除的文件两处都不存在 ⇒ 被过滤掉 ⇒ `git add` 不含它 ⇒ 提交里没有这次删除，
        # 镜像工作区留下一个 D，下一次 preflight 就把自己永久挡在外面（评审 R73-1）。
        # 判据改成「本次 done 里出现过这个路径」——done 是这一笔真的动过的东西，不靠猜存在性。
        touched = set(rels)
        paths = sorted(touched |
                       {p for p in ("third_party/bili-scribe", ".gitmodules") if (mirror / p).exists()})
        if not paths:
            print("[ok] 没有需要提交的东西（镜像已与主仓一致）")
            return 0
        git(mirror, "add", "-A", "--", *paths)     # 显式 pathspec，绝不裸 -A
        # **只提交这一笔动过的路径**，不提交整个暂存面。旧版拿
        # `git diff --cached --name-only` 的全仓暂存列表去 commit，会把镜像里别人
        # 预先 staged 的文件一起吸进这笔提交（评审 R73-2）。这里以 paths 为准，
        # 并且发现 bili_scribe/ 条目时**只退它自己**，不再 `restore --staged bili_scribe`
        # 把别人的暂存一起退掉。
        staged_all = [ln for ln in git(mirror, "diff", "--cached", "--name-only").splitlines() if ln]
        ours = [p for p in paths if p in set(staged_all)]
        # add 之后、commit 之前，**再核一次索引内容**：`git commit -- <path>` 提交的是
        # 索引那一版，而 add 刚把它写成工作区版。若此时索引仍不等于主仓 HEAD 版
        # （路径被 .gitignore 挡住、或工作区在 add 与 commit 之间被改过），
        # 提交进去的就是一份对不上账的内容 —— 那正是本入口要消灭的现场。
        # 这一步放在 add 之后，是因为在 add 之前索引本来就可能旧（合法的 resume 现场），
        # 那时比索引会误判（哨兵 L8 已证伪我第一版写法）。
        # ⚠️ 顺序：`ours` 必须先算出来，这段才能用 —— 我第一版把它写在 `ours` 赋值之前，
        # 直接 NameError，于是 `--commit` 块整个没跑，哨兵 L2b/L8/L11b 三处一起红。
        idx_bad = []
        for rel in ours:
            want = head_bytes(ROOT, rel)
            if want is None:
                continue                    # 本次删除：索引里没有才是对的
            if index_bytes(mirror, rel) != want:
                idx_bad.append(rel)
        if idx_bad:
            print(f"[x] 暂存后索引仍不等于主仓 HEAD 版：{'、'.join(idx_bad[:5])}"
                  "（多半被 .gitignore 挡住或 add 后被改动）——不提交")
            return 2
        under = [p for p in ours if p.startswith("bili_scribe/")]
        if under:
            git(mirror, "restore", "--staged", "--", *under)
            print(f"[x] 本次要提交的路径里出现 bili_scribe/ 下 {len(under)} 个文件，已退回它们，不提交")
            return 2
        foreign = [p for p in staged_all if p not in set(paths)]
        if foreign:
            print(f"[note] 镜像暂存面另有 {len(foreign)} 个文件不属于本次同步，保持原样（不提交、不退）")
        if not ours:
            print("[ok] 没有需要提交的东西（镜像已与主仓一致）")
            return 0
        git(mirror, "commit", "-q", "-m", args.message.strip(), "--", *ours)
        print(f"[ok] 镜像 commit {git(mirror, 'rev-parse', '--short', 'HEAD').strip()}，含 {len(ours)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
