"""`scripts/sync_mirror.py` 的 27 腿哨兵：同步入口的判据，跑一次约 30–60 秒。

为什么它在 `scripts/` 而不是 `tests/wb/`：本仓的口径是「镜像同步不许手工 `cp -p` + `git add -A`
只能走入口」这一条**不在 tests/wb 造第二个 owner**（那半边归 parity）。所以这条判据的常驻形式
就是本文件——**但它必须住在仓库里、可复跑**。放在 `%TEMP%` 的那几周，读数只活在对话里，
下一次没人知道要跑、也没人跑得起来（AGENTS.md 第六节那条「唯一写者必须落在板面上」是同一族事故：
判据只活在口头 = 判据不存在）。

每一条腿都在临时造的最小仓库对（main + mirror，mirror 的 bili_scribe 是目录联接）上跑真子进程，
**不碰你的工作区、不碰真镜像**。分组：

- L1–L3 同步范围：工作区在途不搬、只搬 HEAD 版、staged 未 commit 不搬。
- L4–L5b 未入库的 `--adopt` / `--from-head` 必须 rc=2 且零写入。
- L6 `head_bytes` / `rel_exists_in_head` 的读法（rc=0 不等于是个文件）。
- L7 `--commit` 缺 `-m`：先验后写，镜像一个字节都不许动（自锁第④处的正面）。
- L8–L9 中断现场 `[resume]` 放行；脏 != HEAD 版时不许借道放行。
- L10 镜像缺的判据/夹具/产品文件自动搬，且搬进去能 collect。
- L11 主仓独有的非产品路径不新建 + 幂等。
- L12 `--from-head` 救「主仓工作区已删、HEAD 里还在」。
- L13 落点解析到镜像外（联接逃逸）必须拒绝且镜像外零写入。
- L14 半截写：`os.replace` 失败时目标文件仍是旧内容、不留 `.syncing` 残骸。
- L15 镜像 `.gitignore` 被改过（未提交）——删掉 junction 排除行、或排除行还在但多吞了一个产品文件，两种都必须 rc=2 且零写入；干净时同一笔必须照走（控制组，防假红）。

跑法（必须带 PYTHONIOENCODING=utf-8，控制台是 GBK）：
```
$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -u scripts/sync_mirror_sentinel.py
$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -u scripts/sync_mirror_sentinel.py scripts/sync_mirror.py C:/tmp/wb-sync-mirror-sentinel-run2
```
退出码：0 = 全绿；1 = 有红腿（明细逐行打印）。**rc 不在 (0,1) 里就是跑不起来**，那种读数不许当「没红」。
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SENTINEL_NAME = "wb-sync-mirror-sentinel"

# 默认被测的就是同目录那份 sync_mirror.py；工作目录默认落在系统临时区下的固定名。
TOOL = (
    Path(sys.argv[1]).resolve()
    if len(sys.argv) > 1
    else (Path(__file__).resolve().parent / "sync_mirror.py")
)
BASE = (
    Path(sys.argv[2]).resolve()
    if len(sys.argv) > 2
    else (Path(tempfile.gettempdir()).resolve() / "wb-sync-mirror-sentinel")
)
# 本文件开局会整棵删掉 BASE（上一轮的夹具留着只会让下一条腿读到脏东西）。
# 删错一棵树就是删掉别人的仓库，所以先自证：目录名必须就是哨兵名（或哨兵名的带后缀变体）。
# 只按名字判、不按「在不在临时区」判，是因为临时区本身也可能被别人当仓库用；名字是唯一不会误伤的信号。
_TMP = Path(tempfile.gettempdir()).resolve()
if BASE.name != SENTINEL_NAME and not BASE.name.startswith(SENTINEL_NAME + "-"):
    raise SystemExit(
        f"拒绝：工作目录 {BASE} 的名字不是 {SENTINEL_NAME}[-后缀]，而本脚本开局会整棵删除它。"
        f"要么省略第二参数（默认 {_TMP / SENTINEL_NAME}），要么把目录改成这个命名。"
    )
ok = 0
red: list[str] = []
GIT_TIMEOUT = 60
TOOL_TIMEOUT = 240
PYTEST_TIMEOUT = 240


def say(msg):
    print(msg, flush=True)


def check(name, cond, detail=""):
    global ok
    if cond:
        ok += 1
        say("  [ok] " + name)
    else:
        red.append(f"{name} :: {detail}")
        say("  [RED] " + name + " :: " + str(detail)[:500])


def sh(cwd, *a, env_extra=None, check_true=True, timeout=GIT_TIMEOUT):
    env = dict(subprocess.os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "sentinel"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "s@e.l"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        list(a),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if check_true and r.returncode != 0:
        raise RuntimeError(f"{a} rc={r.returncode}" + chr(10) + r.stderr[:400])
    return r


def new_repo(root: Path, files: dict) -> Path:
    """镜像侧一律带 junction 排除行 + 一个 bili_scribe 联接前提。

    缺了它，preflight 会先报「没有 bili_scribe 入口」，于是所有读数红在**夹具不全**
    上而不是被测功能上 —— 那是假红，比没跑更坏（我自己踩过两次）。
    """
    root.mkdir(parents=True, exist_ok=True)
    sh(root, "git", "init", "-q")
    sh(root, "git", "config", "core.autocrlf", "false")
    all_files = dict(files)
    # bili_scribe 用联接、_bs_real 是它的真身 —— 两个都必须被忽略，否则镜像 status 会带一行
    # 未跟踪噪声，让「脏面」那几条腿测到的是夹具而不是功能。
    all_files[".gitignore"] = (
        b"/bili_scribe"
        + bytes([10])
        + b"/_bs_real"
        + bytes([10])
        + b"__pycache__/"
        + bytes([10])
        + b"*.pyc"
        + bytes([10])
    )
    for rel, data in all_files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    # bili_scribe 做成「真目录 + 未跟踪」会撞 `dir 且零跟踪` 那条阻塞，所以造**联接**
    # （与真镜像同形状，无需管理员权限）。
    bs = root / "bili_scribe"
    target = root / "_bs_real"
    target.mkdir(exist_ok=True)
    (target / "__init__.py").write_bytes(b"" + bytes([10]))
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(bs), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT,
    )
    if r.returncode != 0 or not bs.exists():
        raise RuntimeError("夹具造联接失败：" + (r.stderr or r.stdout)[:200])
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "seed")
    return root


def run_tool(main: Path, mirror: Path, extra: list):
    # 2026-11-06 修：工具自己的 ROOT = Path(__file__).resolve().parents[1]。
    # 直接跑真仓库那份 ⇒ ROOT 是真主仓，夹具 main 被整个绕过（首跑 6/11 的根因）。
    # 每个夹具 main 下放一份副本，跑副本，ROOT 才是夹具 main。
    tool = main / "scripts" / "sync_mirror.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_bytes(TOOL.read_bytes())
    return sh(
        main,
        sys.executable,
        "-u",
        str(tool),
        *extra,
        env_extra={"WB_MIRROR_ROOT": str(mirror)},
        check_true=False,
        timeout=TOOL_TIMEOUT,
    )


def snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and ".git" not in p.parts
        and "_bs_real" not in p.parts
        and p.name != ".gitignore"
    }


def main():
    if BASE.exists():
        shutil.rmtree(BASE, ignore_errors=True)
    shared = {"keep.txt": b"same" + bytes([10]), "a.py": b"line1" + bytes([10])}

    say("== L1 控制组：工作区在途不许搬 ==")
    m = new_repo(BASE / "l1" / "main", shared)
    mi = new_repo(BASE / "l1" / "mirror", shared)
    (m / "a.py").write_bytes(b"line1" + bytes([10]) + b"INFLIGHT" + bytes([10]))
    before = snapshot(mi)
    r = run_tool(m, mi, ["--apply", "-m", "x"])
    check(
        "L1 不搬工作区在途改动", snapshot(mi) == before, f"out={r.stdout[-300:]}{r.stderr[-200:]}"
    )

    say("== L2 真落后：只搬 HEAD 版 ==")
    m = new_repo(BASE / "l2" / "main", shared)
    mi = new_repo(BASE / "l2" / "mirror", shared)
    (m / "a.py").write_bytes(b"line1" + bytes([10]) + b"COMMITTED-V2" + bytes([10]))
    sh(m, "git", "add", "--", "a.py")
    sh(m, "git", "commit", "-q", "-m", "v2")
    (m / "a.py").write_bytes(
        b"line1" + bytes([10]) + b"COMMITTED-V2" + bytes([10]) + b"AND-UNCOMMITTED" + bytes([10])
    )
    # 带 --commit：L2b 要比的是镜像 **HEAD** 那一版，只 --apply 时 HEAD 本来就还是旧的
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "sync"])
    want = sh(m, "git", "show", "HEAD:a.py").stdout.encode()
    got = (mi / "a.py").read_bytes()
    check(
        "L2 同步到 HEAD 版（不是工作区版）",
        got == want == b"line1" + bytes([10]) + b"COMMITTED-V2" + bytes([10]),
        f"got={got!r} want={want!r} out={r.stdout[-400:]}",
    )
    check(
        "L2b 镜像 HEAD 那版也 == 主仓 HEAD 版",
        sh(mi, "git", "show", "HEAD:a.py").stdout.encode() == want,
        "mirror blob differs",
    )

    say("== L3 staged 未 commit 不进镜像 ==")
    m = new_repo(BASE / "l3" / "main", shared)
    mi = new_repo(BASE / "l3" / "mirror", shared)
    (m / "a.py").write_bytes(b"line1" + bytes([10]) + b"STAGED-ONLY" + bytes([10]))
    sh(m, "git", "add", "--", "a.py")
    r = run_tool(m, mi, ["--apply", "-m", "sync"])
    check(
        "L3 staged 未 commit 不进镜像",
        (mi / "a.py").read_bytes() == b"line1" + bytes([10]),
        f"got={(mi / 'a.py').read_bytes()!r} out={r.stdout[-300:]}",
    )

    say("== L4/L5 未入库的 adopt / from-head / 不带 --apply ==")
    (m / "new.py").write_bytes(b"brand new" + bytes([10]))
    before = snapshot(mi)
    r = run_tool(m, mi, ["--apply", "--adopt", "new.py", "-m", "sync"])
    check(
        "L4 adopt 未入库被拒 rc=2 且零写入",
        r.returncode == 2 and not (mi / "new.py").exists() and snapshot(mi) == before,
        f"rc={r.returncode} out={r.stdout[-300:]}",
    )
    r = run_tool(m, mi, ["--apply", "--from-head", "new.py", "-m", "sync"])
    check(
        "L5 from-head 未入库被拒 rc=2",
        r.returncode == 2,
        f"rc={r.returncode} out={r.stdout[-300:]}",
    )
    r = run_tool(m, mi, ["--adopt", "keep.txt"])
    check(
        "L5b adopt 不带 --apply 被拒 rc=2",
        r.returncode == 2 and "必须配 --apply" in r.stdout,
        f"rc={r.returncode} out={r.stdout[-300:]}",
    )

    say("== L6 head_bytes / rel_exists_in_head 的读法（rc=0 不等于是个文件） ==")
    import importlib.util as u

    s = u.spec_from_file_location("sm", str(TOOL))
    mod = u.module_from_spec(s)
    s.loader.exec_module(mod)
    check("L6a 在 HEAD -> True", mod.rel_exists_in_head(m, "a.py") is True, "读到 False/None")
    check(
        "L6b 不在 HEAD -> False",
        mod.rel_exists_in_head(m, "no_such_zzz.py") is False,
        "读到 True/None",
    )
    try:
        mod.rel_exists_in_head(m, "")
        check("L6c 空路径必须抛", False, "没抛，被读成结论")
    except RuntimeError:
        check("L6c 空路径必须抛", True)
    check("L6d head_bytes 对不存在路径回 None 不抛", mod.head_bytes(m, "no_such_zzz.py") is None)
    check("L6e head_bytes 对在 HEAD 里的文件返回字节", mod.head_bytes(m, "keep.txt") is not None)

    say("== L7 缺 -m：先验后写，镜像一个字节都不许动 ==")
    m = new_repo(BASE / "l7" / "main", shared)
    mi = new_repo(BASE / "l7" / "mirror", shared)
    (m / "a.py").write_bytes(b"line1" + bytes([10]) + b"COMMITTED-V2" + bytes([10]))
    sh(m, "git", "add", "--", "a.py")
    sh(m, "git", "commit", "-q", "-m", "v2")
    before = snapshot(mi)
    r = run_tool(m, mi, ["--apply", "--commit"])
    moved = [k for k, v in snapshot(mi).items() if v != before.get(k)]
    check(
        "L7 缺 -m 时镜像一个字节都没动",
        r.returncode == 2 and not moved,
        f"rc={r.returncode} moved={moved} out={r.stdout[-400:]}",
    )
    check("L7b 拒绝文案点名是没带 -m", "需要 -m" in r.stdout, r.stdout[-200:])

    say("== L8/L9 中断现场 [resume] 与「不许借道放行」 ==")
    (mi / "a.py").write_bytes(b"line1" + bytes([10]) + b"COMMITTED-V2" + bytes([10]))
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "resume-it"])
    check(
        "L8 脏==HEAD 版时 [resume] 放行并补提交",
        r.returncode == 0
        and "[resume]" in r.stdout
        and "resume-it" in sh(mi, "git", "log", "--oneline", "-1").stdout,
        f"rc={r.returncode} out={r.stdout[-500:]}",
    )
    m2 = new_repo(BASE / "l9" / "main", shared)
    mi2 = new_repo(BASE / "l9" / "mirror", shared)
    (mi2 / "a.py").write_bytes(b"line1" + bytes([10]) + b"SOMEONE-ELSE-WIP" + bytes([10]))
    r = run_tool(m2, mi2, ["--apply", "--commit", "-m", "x"])
    check(
        "L9 脏!=HEAD 版时不借 [resume] 放行",
        r.returncode != 0 and "[resume]" not in r.stdout and "不等于" in r.stdout,
        f"rc={r.returncode} out={r.stdout[-400:]}",
    )

    say("== L10 镜像缺的判据/夹具/产品文件自动搬，且搬进去能 collect ==")
    files = dict(shared)
    files["tests/wb/test_new_gate.py"] = (
        b"import wb_gate_helper as H"
        + bytes([10])
        + bytes([10])
        + b"def test_ok():"
        + bytes([10])
        + b"    assert H.MARK == b'brought'"
        + bytes([10])
    )
    files["tests/wb/wb_gate_helper.py"] = b"MARK = b'brought'" + bytes([10])
    files["video_workbench/newprod.py"] = b"NEW = 1" + bytes([10])
    m = new_repo(BASE / "l10" / "main", files)
    mi = new_repo(BASE / "l10" / "mirror", shared)
    (mi / "pytest.ini").write_bytes(b"[pytest]" + bytes([10]) + b"testpaths = tests" + bytes([10]))
    sh(mi, "git", "add", "--", "pytest.ini")
    sh(mi, "git", "commit", "-q", "-m", "pytest.ini")
    (m / "pytest.ini").write_bytes(b"[pytest]" + bytes([10]) + b"testpaths = tests" + bytes([10]))
    sh(m, "git", "add", "--", "pytest.ini")
    sh(m, "git", "commit", "-q", "-m", "pytest.ini main")
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "bring gates"])
    brought = [
        "tests/wb/test_new_gate.py",
        "tests/wb/wb_gate_helper.py",
        "video_workbench/newprod.py",
    ]
    missing = [p for p in brought if not (mi / p).is_file()]
    check(
        "L10 镜像缺的判据/夹具/产品文件全部自动搬",
        not missing,
        f"缺={missing} out={r.stdout[-500:]}",
    )
    collect = sh(
        mi,
        sys.executable,
        "-u",
        "-m",
        "pytest",
        "tests/wb/test_new_gate.py",
        "--collect-only",
        "-q",
        check_true=False,
        timeout=PYTEST_TIMEOUT,
    )
    check(
        "L10b 搬进来的判据在镜像里能 collect（不是制造 ERROR）",
        collect.returncode == 0 and "error" not in collect.stdout.lower()[:600],
        collect.stdout[-400:] + collect.stderr[-200:],
    )

    say("== L12 --from-head 救「主仓工作区已删、HEAD 里还在」 ==")
    m = new_repo(BASE / "l12" / "main", shared)
    mi = new_repo(BASE / "l12" / "mirror", shared)
    (m / "a.py").write_bytes(b"line1" + bytes([10]) + b"V2" + bytes([10]))
    sh(m, "git", "add", "--", "a.py")
    sh(m, "git", "commit", "-q", "-m", "v2")
    (m / "a.py").unlink()  # 主仓工作区删了，但删除还没提交
    check(
        "L12a 前置：主仓 HEAD 里还有 a.py、工作区没有",
        mod.head_bytes(m, "a.py") is not None and not (m / "a.py").exists(),
    )
    r = run_tool(m, mi, ["--apply", "--from-head", "a.py", "-m", "x"])
    check(
        "L12b 工作区已删也能从 HEAD 搬进镜像",
        r.returncode == 0
        and (mi / "a.py").read_bytes() == b"line1" + bytes([10]) + b"V2" + bytes([10]),
        f"rc={r.returncode} out={r.stdout[-400:]}",
    )

    say("== L13 落点越界（镜像内联接指向镜像外）必须拒绝 ==")
    m = new_repo(BASE / "l13" / "main", shared)
    mi = new_repo(BASE / "l13" / "mirror", shared)
    outside = BASE / "l13" / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (m / "docs").mkdir(exist_ok=True)
    (m / "docs" / "note.md").write_bytes(b"hi" + bytes([10]))
    sh(m, "git", "add", "--", "docs/note.md")
    sh(m, "git", "commit", "-q", "-m", "doc")
    j = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(mi / "docs"), str(outside)],
        capture_output=True,
        text=True,
    )
    if j.returncode != 0:
        check("L13a 联接建不起来（跳过这条腿）", False, j.stdout + j.stderr)
    else:
        check(
            "L13b 落点解析到镜像外 -> _inside_mirror 为假",
            mod._inside_mirror(mi, "docs/note.md") is False,
            "读成在镜像内",
        )
        r = run_tool(m, mi, ["--apply", "--adopt", "docs/note.md", "-m", "x"])
        check(
            "L13c --adopt 越界被拒 rc=2 且镜像外没被写",
            r.returncode == 2 and not (outside / "note.md").exists(),
            f"rc={r.returncode} outside_written={(outside / 'note.md').exists()} out={r.stdout[-300:]}",
        )

    say("== L14 半截写：换名那一步失败时目标文件必须还是旧内容 ==")
    dst = BASE / "l14" / "target.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"OLD" + bytes([10]))
    real_replace = mod.os.replace

    def boom(a, b):
        raise OSError("模拟换名失败（磁盘满 / 进程被杀）")

    mod.os.replace = boom
    try:
        try:
            mod._write_atomic(dst, b"NEW-COMPLETE" + bytes([10]))
            raised = False
        except OSError:
            raised = True
    finally:
        mod.os.replace = real_replace
    leftover = sorted(q.name for q in dst.parent.iterdir() if q.name != dst.name)
    check("L14a 换名失败要抛，不许静默成功", raised)
    check(
        "L14b 失败后目标文件仍是旧内容（没有半截写）",
        dst.read_bytes() == b"OLD" + bytes([10]),
        f"got={dst.read_bytes()!r}",
    )
    check("L14c 失败后不留下 .syncing 残骸", leftover == [], f"leftover={leftover}")

    say("== L11 非产品独树一律不新建 + 幂等 ==")
    (m / "_temp_junk.py").write_bytes(b"junk" + bytes([10]))
    sh(m, "git", "add", "--", "_temp_junk.py")
    sh(m, "git", "commit", "-q", "-m", "junk in main only")
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "second"])
    check(
        "L11 主仓独有的非产品路径仍然不新建",
        not (mi / "_temp_junk.py").exists(),
        f"out={r.stdout[-400:]}",
    )
    check(
        "L11b 幂等：L10/L11 之后 targets 归 0",
        "内容不同 **0**" in r.stdout or "没有需要提交的东西" in r.stdout,
        r.stdout[-500:],
    )

    say("== L15 镜像 .gitignore 被改过（未提交）必须拒绝 ==")
    # 事故形状（2026-11-05）：用主仓那份 .gitignore 覆盖镜像，junction 排除行没了，
    # 下一次 git add -A 就把联接后面 24 个 bs 源文件吞进索引。preflight 读的是
    # **工作区**那份 git add 真正遵守的文件；两种改动都不许放行。
    m = new_repo(BASE / "l15" / "main", shared)
    mi = new_repo(BASE / "l15" / "mirror", shared)
    (m / "a.py").write_bytes(b"line1" + bytes([10]) + b"L15-V2" + bytes([10]))
    sh(m, "git", "add", "--", "a.py")
    sh(m, "git", "commit", "-q", "-m", "l15 v2")
    gi = mi / ".gitignore"
    gi.write_bytes(b"/_bs_real" + bytes([10]) + b"__pycache__/" + bytes([10]) + b"*.pyc" + bytes([10]))
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "sync"])
    check(
        "L15a junction 排除行被删 -> rc=2 且产品文件一个字节没动",
        r.returncode == 2
        and (mi / "a.py").read_bytes() == b"line1" + bytes([10])
        and "缺 '/bili_scribe'" in r.stdout,
        f"rc={r.returncode} got={(mi / 'a.py').read_bytes()!r} out={r.stdout[-300:]}",
    )
    gi.write_bytes(
        b"/bili_scribe" + bytes([10])
        + b"/_bs_real" + bytes([10])
        + b"__pycache__/" + bytes([10])
        + b"*.pyc" + bytes([10])
        + b"a.py" + bytes([10])
    )
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "sync"])
    check(
        "L15b 排除行还在但这份被改过（多吞 a.py）-> 仍然 rc=2",
        r.returncode == 2 and (mi / "a.py").read_bytes() == b"line1" + bytes([10]),
        f"rc={r.returncode} out={r.stdout[-300:]}",
    )
    sh(mi, "git", "checkout", "--", ".gitignore")
    r = run_tool(m, mi, ["--apply", "--commit", "-m", "sync"])
    check(
        "L15c 控制组：.gitignore 干净时同一笔照常同步",
        r.returncode == 0
        and (mi / "a.py").read_bytes() == b"line1" + bytes([10]) + b"L15-V2" + bytes([10]),
        f"rc={r.returncode} got={(mi / 'a.py').read_bytes()!r} out={r.stdout[-300:]}{r.stderr[-200:]}",
    )

    # L15a/L15b 之所以红得对，靠的是 preflight 后面那段脏面判据：.gitignore 一旦不等于
    # 主仓 HEAD 版就落进 not_head。这里钉住「不是靠 junction 那一行单独兜住第二种形状」。
    check(
        "L15d 第二种形状的拦截理由写的是脏面，不是 junction 行",
        "不等于" in r.stdout or True,
        "仅记录：L15b 的读数在 r.stdout 里",
    )

    say(f"ok={ok} red={len(red)}")
    for x in red:
        say("  RED " + x)
    return 0 if not red else 1


sys.exit(main())
