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


def _need(path: Path, what: str, problems: list[str]) -> None:
    if not path.exists():
        problems.append(f"缺{what}：{path}")


def preflight() -> list[str]:
    problems: list[str] = []
    _need(SPEC, "打包定义", problems)
    _need(PKG / "desktop.py", "入口", problems)
    _need(PKG / "web" / "index.html", "窗口首页", problems)
    _need(PKG / "resources" / "icon.ico", "图标（先跑 scripts/make_wb_icon.py）", problems)
    _need(PKG / "resources" / "prompts", "提示词目录", problems)
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
        _need(DIST / rel, "包内资源", problems)
    if problems:
        for item in problems:
            print(f"[verify] ✗ {item}")
        return 1

    log_file = DIST / "video-workbench.log"
    if log_file.exists():
        log_file.unlink()  # 只认这一次的输出，别拿上一轮的 ok 蒙混

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
