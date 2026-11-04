"""一键打包 VideoWorkbench（Windows, one-folder）。

    ./.venv/Scripts/python.exe scripts/build_wb.py            # 完整构建
    ./.venv/Scripts/python.exe scripts/build_wb.py --check     # 只核对前置条件

产物：`dist/VideoWorkbench/VideoWorkbench.exe`（同目录即绿色版，拷走就能跑）。

为什么先校验再构建：打包一次要几分钟，而最常见的失败原因是"图标没生成"、
"资源目录缺东西"这种一眼能看出的问题——先用 2 秒查掉，别让人等 5 分钟拿报错。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
PKG = PROJ / "video_workbench"
SPEC = PROJ / "video_workbench.spec"
DIST = PROJ / "dist" / "VideoWorkbench"
EXE = DIST / "VideoWorkbench.exe"

#: 六份提示词真源 txt 的**逐字**文件名（含全角冒号、末尾句号、`.txt` 前那个空格）。
#: 这里是唯一的出处，preflight 与 verify 共用——写死名字是刻意的：这份清单是"用户手写的
#: 那几篇原文必须进包"的构建契约，不跟着 prompt_source.MAP 走，改了 MAP 也不该让自检跟着松。
#: 事故背景：spec 用整目录收 `resources`，文件名带空格与全角时最容易"收进去但收错一份/半份"，
#: 只查存在会绿着骗过去，所以 verify 还要比字节（见 verify()）。
PROMPT_TXT_NAMES = (
    "文稿编辑.txt",
    "写作专家.txt",
    "分析报告.txt",
    "首席思想精炼师：对于各种文本都处理得较为简洁的总结提示词。.txt",
    "大概率管用 .txt",
    "视频总结.txt",
)


def _need(path: Path, what: str, problems: list[str], *, wait_s: float = 0.0) -> None:
    """存在性检查。

    ``wait_s`` 是给构建后自检用的：PyInstaller 报"完成"那一刻，刚写好的 2000+ 个文件
    还可能被杀软实时扫描挡在可见性后面，直接查会假报"缺包内资源"（实测同一份产物：
    构建时 4 条 ✗，稍后 --verify-only 全过）。等满还不存在才算真缺。
    """
    deadline = time.time() + wait_s
    while not path.exists():
        if time.time() >= deadline:
            problems.append(f"缺{what}：{path}")
            return
        time.sleep(0.5)


def preflight() -> list[str]:
    problems: list[str] = []
    _need(SPEC, "打包定义", problems)
    _need(PKG / "desktop.py", "入口", problems)
    _need(PKG / "web" / "index.html", "窗口首页", problems)
    _need(PKG / "resources" / "icon.ico", "图标（先跑 scripts/make_wb_icon.py）", problems)
    _need(PKG / "resources" / "prompts", "提示词目录", problems)
    # 六份 txt 逐字点名——少任何一份都算前置条件不齐，别白等几分钟构建再报缺。
    # 构建出来的包若少了它们，用户双击 exe 只会跑内置压缩版，界面还显示"无 txt 真源"，
    # 根本看不出问题（这正是 task-28 要堵的洞）。
    for name in PROMPT_TXT_NAMES:
        _need(PKG / "resources" / "prompts" / name, "提示词真源 txt（逐字文件名）", problems)
    for mod in ("PyInstaller", "webview"):
        try:
            __import__(mod)
        except Exception:
            problems.append(f"当前解释器没装 {mod}（用 ./.venv/Scripts/python.exe 跑）")
    return problems


def build(*, clean: bool, noconfirm: bool = True) -> int:
    if clean:
        for path in (PROJ / "build" / "VideoWorkbench", DIST):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                print(f"[build] 清理 {path}")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC)]
    if noconfirm:
        cmd.append("--noconfirm")
    cmd += ["--distpath", str(PROJ / "dist"), "--workpath", str(PROJ / "build")]
    print("[build] " + " ".join(cmd))
    started = time.time()
    rc = subprocess.call(cmd, cwd=str(PROJ))
    print(f"[build] PyInstaller 退出码 {rc}，耗时 {time.time() - started:.1f}s")
    return rc


def verify() -> int:
    """构建后自检：exe 在、能跑 --check、网页资源真的落进包里。

    **注意 stdout**：窗口程序（`console=False`）里 `sys.stdout` 不是控制台，桌面入口
    会把输出接到 exe 同目录的 `video-workbench.log`（见 `desktop._ensure_streams`）。
    所以这里不能只读子进程 stdout——第一次就是这么判成了"没报 ok"。
    """
    problems: list[str] = []
    if not EXE.exists():
        problems.append(f"没产出 exe：{EXE}")
    for rel in ("_internal/video_workbench/web/index.html",
                "_internal/video_workbench/web/js/app.js",
                "_internal/video_workbench/web/css/tokens.css",
                "_internal/video_workbench/resources/icon.ico"):
        _need(DIST / rel, "包内资源", problems, wait_s=20)

    # 六份 txt 真源：不止查存在，还查字节——文件名带空格与全角，历史上最容易
    # "收进去但收错一份/半份"，只查存在会绿着骗过去。字节数**现读比对**，
    # 不写死数字：仓库里改了，包没跟上就得红。_need 用 wait_s=20 是因为杀软实时扫描
    # 会让刚写出的文件短暂不可见（别改成裸 exists()）。
    repo_prompts = PKG / "resources" / "prompts"
    for name in PROMPT_TXT_NAMES:
        packed = DIST / "_internal/video_workbench/resources/prompts" / name
        _need(packed, "包内 txt 真源", problems, wait_s=20)
        if not packed.is_file():
            continue  # _need 已登记缺失，比不了字节
        repo_file = repo_prompts / name
        if not repo_file.is_file():
            problems.append(f"仓库侧缺 txt 真源，无法比对字节：{repo_file}")
            continue
        packed_bytes = packed.stat().st_size
        repo_bytes = repo_file.stat().st_size
        if packed_bytes != repo_bytes:
            problems.append(
                f"包内 txt 字节数与仓库不一致：{name} 包内={packed_bytes} 仓库={repo_bytes}"
            )
        else:
            print(f"[verify] ✓ {name} 包内 {packed_bytes}B == 仓库 {repo_bytes}B")

    if problems:
        for item in problems:
            print(f"[verify] ✗ {item}")
        return 1

    log_file = DIST / "video-workbench.log"
    if log_file.exists():
        try:
            log_file.unlink()  # 只认这一次的输出，别拿上一轮的 ok 蒙混
        except PermissionError:
            # 打包版还开着一个实例时日志被独占。此时不删就无法区分新旧输出，
            # 继续跑会把上一轮的 ok 当成本轮结果——直接报清楚该做什么，别抛栈。
            print(f"[verify] ✗ {log_file.name} 被占用：先关掉正在运行的 VideoWorkbench 再自检")
            return 1

    print("[verify] 跑 --check（不建窗口，只验证 RPC/DB/依赖探测）")
    proc = subprocess.run([str(EXE), "--check"], capture_output=True, text=True, timeout=240)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out and log_file.exists():
        out = log_file.read_text(encoding="utf-8", errors="replace")
    print(out.strip()[:2000])
    if proc.returncode != 0:
        print(f"[verify] ✗ --check 退出码 {proc.returncode}")
        return 1
    flat = "".join(out.split())
    if '"ok":true' not in flat:
        print("[verify] ✗ --check 没报 ok")
        return 1

    size = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file())
    print(f"[verify] 目录体积 {size / 1024 / 1024:.1f} MB，文件 {len(list(DIST.rglob('*')))} 个")
    print("[verify] ✓ 打包结果可用")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 VideoWorkbench")
    parser.add_argument("--check", action="store_true", help="只核对前置条件，不构建")
    parser.add_argument("--no-clean", action="store_true", help="保留上次的 build/dist")
    parser.add_argument("--verify-only", action="store_true", help="不构建，只跑产物自检")
    args = parser.parse_args(argv)

    problems = preflight()
    if problems:
        for item in problems:
            print(f"[preflight] ✗ {item}")
        return 2
    print("[preflight] ✓ 前置条件齐")
    if args.check:
        return 0
    if args.verify_only:
        return verify()

    rc = build(clean=not args.no_clean)
    if rc != 0:
        return rc
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
