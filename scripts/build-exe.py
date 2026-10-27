"""把桌面版打成单个 exe。

    python scripts/build-exe.py               出 dist/bili-scribe.exe
    python scripts/build-exe.py --console     带控制台（排障用）
    python scripts/build-exe.py --onedir      出一个目录而不是单文件

和 `scripts/package.py` 的区别：那个打的是**源码 zip**（给已经装了 Python 的人
用，改一行就能跑）；这个打的是**能直接双击的窗口程序**（给没装 Python 的人用，
代价是改代码要重新打包）。

三条硬约束，每一条都是"不遵守就会打出一个看起来正常、双击就崩"的东西：

1. **`--windowed` 会让 `sys.stdout` 变成 `None`**。项目里 `store` / `notes` /
   `bili` 三层都把日志接到 `print` 上，不补流就是 `AttributeError: 'NoneType'
   object has no attribute 'write'`。`desktop.py:_ensure_streams()` 负责补，
   它在 `main()` 第一行就执行。
2. **界面文件必须显式塞进去**。`index.html` 不是 Python 模块，PyInstaller 的
   静态分析看不见它，不写进 `--add-data` 打出来的 exe 会报"找不到界面文件"。
3. **`webview/lib` 下的 WebView2 DLL 也必须显式塞**。那是 .NET 侧的运行库，
   `import webview` 不碰它；缺了它 pythonnet 起来后找不到 WebView2，窗口白屏
   或直接抛 COM 异常。`webview` 自带的 hook 会收一部分，但这里再显式收一次
   不亏——重复的条目 PyInstaller 会自己去重。

产物不收 `config.yaml`：里面有 api_key。exe 跟源码一样，配置放在**exe 旁边**
（由 `desktop.py:_default_config()` 决定），首次启动用内置默认值。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUILD_DIR = ROOT / "build" / "exe"
DIST_DIR = ROOT / "dist"
SPEC_DIR = ROOT / "build" / "spec"

# 这些模块只有 HTTP 那条路（`run.py` + `app/main.py`）才用。桌面版的入口是
# `desktop.py`，它只 import `app.service`，所以正常也收不进来；显式排除是给
# 体积上保险——uvicorn 会顺带拖进 h11/click/anyio，白占几 MB。
EXCLUDES = [
    "fastapi",
    "starlette",
    "uvicorn",
    "anyio",
    "h11",
    "click",
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "pytest",
    "PIL.ImageQt",
    "PIL.ImageTk",
]


def _pyinstaller() -> list[str]:
    """PyInstaller 的调用前缀。

    用 `python -m PyInstaller` 而不是直接叫 `pyinstaller`：PATH 里那个可能是
    别的环境的，而这里必须用当前 venv 里那个（它和 venv 的 Python 版本、已装
    模块一一对应）。
    """
    return [sys.executable, "-m", "PyInstaller"]


def _clean() -> None:
    """清掉上一次的中间产物。

    不清 build 目录会让 PyInstaller 复用旧的分析缓存——改了依赖却打出旧包，
    这种"明明改了怎么没生效"最难查。dist 里只删同名 exe，不动历史 zip。
    """
    for path in (BUILD_DIR, SPEC_DIR):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def _collect_args() -> list[str]:
    """把所有需要显式带上的一次性参数攒起来。"""
    args = [
        # ---- 打包进来但不是 Python 模块的东西 ----
        # 界面：整个 static 目录，按源码里的相对路径落位，`_ui_index()` 在
        # 打包态就是从 `_MEIPASS` 下按这个路径找。
        "--add-data",
        f"{ROOT / 'bili_scribe' / 'app' / 'static'}{_sep()}bili_scribe/app/static",
        # WebView2 的 .NET 运行库。见文件头第 3 条。
        "--collect-data",
        "webview",
        "--collect-binaries",
        "webview",
        # pythonnet / clr_loader 的元数据与 DLL。`clr_loader` 要在运行时按模块
        # 路径定位 `Python.Runtime.dll`，被压进单文件后路径要靠 hook 重建；
        # 这里连数据一起收，避免"开发能跑、打包报 couldn't load CLR"。
        "--collect-all",
        "pythonnet",
        "--collect-all",
        "clr_loader",
        # PyInstaller 的静态分析看不见"函数体里才 import"的东西。
        # `qrcode` / `PIL.Image` 就属于这种（`service.qr_png_data_url` 里才 import）。
        "--hidden-import",
        "qrcode",
        "--hidden-import",
        "PIL.Image",
        "--hidden-import",
        "webview.platforms.winforms",
    ]
    for name in EXCLUDES:
        args += ["--exclude-module", name]
    return args


def _sep() -> str:
    """`--add-data` 的分隔符。

    Python 3.14 的 PyInstaller 两种都认（`os.pathsep` 在 Windows 上是 `;`），
    但用 `;` 更明确：`:` 在 `C:\\...` 这样的路径里会提前截断。
    """
    return ";"


def _icon() -> Path | None:
    """找图标。没有就返回 None，PyInstaller 会用默认的。

    不在这里生成图标：打包脚本的职责是打包，顺手画图会让人下次想换图标时
    不知道该改哪儿。
    """
    for name in ("bili-scribe.ico", "app.ico"):
        path = ROOT / "assets" / name
        if path.exists():
            return path
    return None


def _copy_config() -> str:
    """把项目根的 config.yaml 复制到 exe 旁边，返回一句给人看的结果。

    **为什么这一步要做。** exe 找配置的顺序是"exe 旁边 → `~/.bili-scribe/`"
    （见 `desktop.py:_config_candidates`）。`dist/` 里原本没有 config.yaml，
    于是双击出来的程序是"未配置大模型"的状态——功能都在，但你得重新填一遍
    key。既然这台机器上已经有一份配好的，直接放到它能找到的位置才算"双击即用"。

    **为什么不担心泄露**：`dist/` 在 `.gitignore` 里，不会进仓库；而
    `scripts/package.py` 打的源码 zip 显式排除了 config.yaml。这两条是各管各的，
    这里只影响本机的 dist 目录。

    复制而不是移动：源码目录那份还要给 `python run.py` 用。
    """
    src = ROOT / "config.yaml"
    if not src.exists():
        return "项目根没有 config.yaml，exe 首次启动会用内置默认值（在设置页里填）"
    dst = DIST_DIR / "config.yaml"
    if dst.resolve() == src.resolve():
        return "config.yaml 已就位"
    shutil.copy2(src, dst)
    return f"已把 config.yaml 复制到 exe 旁边（{dst.name}，含 api_key，仅本机）"


def build(*, console: bool, onedir: bool) -> int:
    _clean()
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    cmd = _pyinstaller() + [
        str(ROOT / "bili_scribe" / "desktop.py"),
        "--name",
        "bili-scribe",
        "--onedir" if onedir else "--onefile",
        "--console" if console else "--windowed",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(SPEC_DIR),
    ]
    cmd += _collect_args()

    icon = _icon()
    if icon is not None:
        cmd += ["--icon", str(icon)]

    print("=" * 70)
    print(" 打包 " + ("目录版" if onedir else "单文件") + " · " + ("带控制台" if console else "窗口程序"))
    print("=" * 70)
    print(f"  入口     {ROOT / 'bili_scribe' / 'desktop.py'}")
    print(f"  产物     {DIST_DIR / ('bili-scribe' if onedir else 'bili-scribe.exe')}")
    if icon is None:
        print("  图标     未找到 assets/bili-scribe.ico，使用默认图标")
    print("=" * 70)

    started = time.time()
    result = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = time.time() - started

    if result.returncode != 0:
        print(f"\n[x] 打包失败，退出码 {result.returncode}（{elapsed:.1f}s）")
        return result.returncode

    target = DIST_DIR / ("bili-scribe" if onedir else "bili-scribe.exe")
    if not target.exists():
        print(f"\n[x] 打包进程返回 0，但没找到产物：{target}")
        return 1

    if onedir:
        total = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    else:
        total = target.stat().st_size
    print(f"\n[v] 完成，用时 {elapsed:.1f}s")
    print(f"    产物   {target}")
    print(f"    体积   {total / 1048576:.1f} MB")
    print(f"    配置   {_copy_config() if not onedir else '（目录版请手动放 config.yaml）'}")
    print()
    print("    直接双击即可运行。")
    print("    配置查找顺序：exe 旁边 → ~/.bili-scribe/config.yaml")
    print("    数据（数据库、凭据、笔记缓存）在 ~/.bili-scribe/。")
    return 0


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[x] 没有装 PyInstaller。先执行：")
        print(f"    {sys.executable} -m pip install pyinstaller")
        return 2

    parser = argparse.ArgumentParser(description="把桌面版打成 exe")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排障用）")
    parser.add_argument("--onedir", action="store_true", help="出目录而不是单文件（启动更快）")
    args = parser.parse_args()
    return build(console=args.console, onedir=args.onedir)


if __name__ == "__main__":
    sys.exit(main())
