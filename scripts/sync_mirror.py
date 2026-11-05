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
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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
    """git ls-files；仓库没有 commit 时返回空（不是错误）。走 git() 所以带 quotepath=false。"""
    r = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"],
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


def preflight(main: Path, mirror: Path) -> tuple[list[str], list[str]]:
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

    dirty = [ln for ln in git(mirror, "status", "--porcelain").splitlines() if ln]
    # 只放行一种脏：整行就是 submodule gitlink（` M third_party/bili-scribe`），且 submodule
    # 自己的内容是干净的。那正是「上次同步对齐了 checkout 却忘了 commit」的现场（今天真撞上），
    # 它本身就是这一笔要提交的东西，不是别人的在途改动。
    only_gitlink = dirty and all(ln.strip() == "M third_party/bili-scribe" or
                                 ln.strip().startswith("M third_party/bili-scribe (")
                                 for ln in dirty)
    if only_gitlink:
        inside = [ln for ln in git(mirror, "status", "--porcelain", "--",
                                   "third_party/bili-scribe").splitlines() if ln]
        # 上面那行只能证明父仓看见 gitlink 变了；再看 submodule 内部有没有未提交内容。
        sm_dirty = [ln for ln in git(mirror / "third_party" / "bili-scribe",
                                     "status", "--porcelain").splitlines() if ln]
        if sm_dirty:
            blockers.append(f"gitlink 变了且 submodule 内部也有 {len(sm_dirty)} 行未提交："
                            "\n      " + "\n      ".join(sm_dirty[:5]))
        else:
            notes.append(f"镜像工作区只有 gitlink 待提交（{len(dirty)} 行）——视为本笔要修的东西，放行")
    elif dirty:
        blockers.append(f"镜像工作区不干净（{len(dirty)} 行），先弄清是谁的在途：\n      " +
                        "\n      ".join(dirty[:5]))
    return blockers, notes


def content_targets(main: Path, mirror: Path) -> dict:
    """按两边**索引**取交集，逐条比字节。"""
    a_set = tracked_lines(main)
    b_set = tracked_lines(mirror)
    b_all = set(b_set)
    common = [p for p in a_set if p in b_all and not p.startswith(SKIP_PREFIXES)]
    targets = [p for p in common
               if p not in NEVER_SYNC
               and (main / p).is_file() and (mirror / p).is_file()
               and (main / p).read_bytes() != (mirror / p).read_bytes()]
    # 主仓独有：只报数，不新建。
    a_only = [p for p in a_set if p not in b_all and not p.startswith(SKIP_PREFIXES)]
    # 镜像独有（非 junction / 非 submodule 挂载点）：算漂移，人工判。
    b_only = [p for p in b_set if p not in set(a_set) and not p.startswith(SKIP_PREFIXES)]
    # 两边都跟踪、但主仓已从工作树删除 → 同步删除。
    gone = [p for p in common if not (main / p).is_file()]
    return {"targets": targets, "main_only": a_only, "mirror_only": b_only, "deleted": gone,
            "common_n": len(common)}


def apply_sync(main: Path, mirror: Path, targets: list[str], deleted: list[str]) -> list[str]:
    done: list[str] = []
    for rel in targets + deleted:
        if rel.startswith(LOCKED_PREFIXES):
            raise RuntimeError(f"拒绝碰逐字节锁定的路径：{rel}（副本与 6 个真源 txt 不归同步管）")
    for rel in targets:
        subprocess.run(["cp", "-p", str(main / rel), str(mirror / rel)], check=True)
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
    git(sm, "fetch", "-q", "origin")
    r = subprocess.run(["git", "cat-file", "-t", main_head], cwd=str(sm),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if (r.stdout or "").strip() != "commit":
        return False, f"镜像的 bili-scribe remote 里找不到主仓 HEAD {main_head[:8]}——先把主仓 push"
    git(sm, "checkout", "-q", main_head)
    return True, f"gitlink -> {main_head[:8]}"


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="主仓 → 发布镜像的同步入口（带前置检查）")
    ap.add_argument("--apply", action="store_true", help="真的复制文件并对齐 gitlink")
    ap.add_argument("--adopt", action="append", default=[], metavar="PATH",
                    help="显式认领某个「主仓独有」路径进镜像（镜像从未跟踪它；"
                         "同步本身只走交集，所以这类要人工点名，不自动新建）")
    ap.add_argument("--commit", action="store_true", help="同步后用显式 pathspec 提交（须配 --apply）")
    ap.add_argument("-m", "--message", default="", help="镜像那一笔的 commit message")
    args = ap.parse_args()

    mirror = mirror_root()
    blockers, notes = preflight(ROOT, mirror)
    for n in notes:
        print(f"[note] {n}")
    if blockers:
        print("[x] 前置检查不过，拒绝同步：")
        for b in blockers:
            print(f"    - {b}")
        return 1

    t = content_targets(ROOT, mirror)
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
        p = p.replace("\\", "/").lstrip("./")
        if p in NEVER_SYNC or p.startswith(SKIP_PREFIXES):
            print(f"[x] --adopt 拒绝 {p}（镜像自己维护 / junction 范围）")
            return 2
        if not (ROOT / p).is_file():
            print(f"[x] --adopt 的 {p} 在主仓不存在")
            return 2
        dst = mirror / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-p", str(ROOT / p), str(dst)], check=True)
        adopted.append(p)
        print(f"[ok] adopt {p} → 镜像")

    done = apply_sync(ROOT, mirror, t["targets"], t["deleted"]) + [f"adopt {p}" for p in adopted]
    print(f"[ok] 处理 {len(done)} 项")
    main_head = git(ROOT, "rev-parse", "HEAD").strip()
    ok, link_msg = align_gitlink(mirror, main_head)
    print(f"[{'ok' if ok else 'x'}] {link_msg}")
    if not ok:
        return 2

    if args.commit:
        if not args.message.strip():
            print("[x] --commit 需要 -m 给镜像那一笔的 message")
            return 2
        rels = []
        for d in done:
            if d.startswith("del "):
                rels.append(d[4:])
            elif d.startswith("adopt "):
                rels.append(d[6:])
            else:
                rels.append(d)
        paths = sorted({p for p in rels if (mirror / p).exists() or (ROOT / p).exists()} |
                       {p for p in ("third_party/bili-scribe", ".gitmodules") if (mirror / p).exists()})
        if not paths:
            print("[ok] 没有需要提交的东西（镜像已与主仓一致）")
            return 0
        git(mirror, "add", "--", *paths)          # 显式 pathspec，绝不 -A
        staged = [ln for ln in git(mirror, "diff", "--cached", "--name-only").splitlines() if ln]
        under = [p for p in staged if p.startswith("bili_scribe/")]
        if under:
            git(mirror, "restore", "--staged", "bili_scribe")
            print(f"[x] 暂存面出现 bili_scribe/ 下 {len(under)} 个文件，已退回，不提交")
            return 2
        if not staged:
            print("[ok] 没有需要提交的东西（镜像已与主仓一致）")
            return 0
        git(mirror, "commit", "-q", "-m", args.message.strip(), "--", *staged)
        print(f"[ok] 镜像 commit {git(mirror, 'rev-parse', '--short', 'HEAD').strip()}，含 {len(staged)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
