r"""真窗口验收：create_window 的 min_size 到底被不被 WebView2/WinForms 强制。

为什么住 scripts/：这条判据的证据只能在**真窗口**上取。tests/wb/test_window_min_size.py
那两条钉的是「接线」——create_window 是否把模块常量原样传下去、常量本身是否非零；
「传下去之后 Windows 认不认」那一半机器测不到，只能起窗读数。以前这份探针只活在
%TEMP%\wb_real_window_min_size.py，读数写在对话里，换台机器就没人能跑（AGENTS.md 第六节
「常驻判据必须住进仓库」点的就是这类事故）。

期望值按 DPI 折算，不钉 1100/720 字面量：pywebview 的 winforms 宿主是
    MinimumSize = Size(int(min_size[0] * scale), int(min_size[1] * scale))
    scale = GetDpiForWindow(hwnd) / 96
设的还是**外层窗口**（含边框标题栏）。所以 100% 缩放下应读到 MIN_SIZE 本身，150% 下应读到
1.5 倍。判据写成折算式，同一份脚本在任意缩放下都能自证。

跑法（会真弹一个窗口，约 6 秒）：
    $env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -u scripts/accept_window_min_size.py
退出码：0 = 四条判据全过；1 = 有不符（明细逐行打印，读数落 wb-min-size-accept.json）；
其他 = 跑不起来，那种读数不许当「过了」。

读数 JSON 落在系统临时区（wb-min-size-accept.json），**故意不落仓库根**：常驻的是这份脚本，
不是某一次的窗口读数。

要测 125%/150%：改显示器缩放（设置 → 显示 → 缩放）后重跑本脚本。本脚本不碰系统设置，
也不写任何用户配置文件——只用 stub 装配窗口，不建库、不碰 ~/.video-workbench。
"""

from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import webview  # noqa: E402  sys.path 前置之后才能 import 本仓

from video_workbench.desktop import (  # noqa: E402  同上
    MIN_SIZE,
    WINDOW_SIZE,
    create_window,
)

TITLE = "VideoWorkbench"
OUT = Path(tempfile.gettempdir()).resolve() / "wb-min-size-accept.json"
user32 = ctypes.windll.user32
try:
    user32.SetProcessDPIAware()
except OSError:
    pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", POINT),
        ("ptMaxSize", POINT),
        ("ptMaxPosition", POINT),
        ("ptMinTrackSize", POINT),
        ("ptMaxTrackSize", POINT),
    ]


def rect_of(hwnd: int) -> dict:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {
        "width": r.right - r.left,
        "height": r.bottom - r.top,
        "left": r.left,
        "top": r.top,
    }


def min_track_of(hwnd: int) -> list[int]:
    mmi = MINMAXINFO()
    user32.SendMessageW(hwnd, 0x0024, 0, ctypes.byref(mmi))
    return [mmi.ptMinTrackSize.x, mmi.ptMinTrackSize.y]


class ApiStub:
    """create_window 只需要一个能 attach 的 api。"""

    def __init__(self) -> None:
        self.window = None

    def attach(self, window) -> None:
        self.window = window


class ServiceStub:
    cfg = SimpleNamespace(ui=SimpleNamespace(skin="ark"))


def find_hwnd() -> int:
    for _ in range(200):
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            return int(hwnd)
        time.sleep(0.05)
    return 0


def _pair(value) -> list[int]:
    return [int(value[0]), int(value[1])]


def worker(window) -> None:
    result: dict = {
        "screen": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)],
        "min_size_logical": list(MIN_SIZE),
        "window_size_logical": list(WINDOW_SIZE),
        "checks": [],
    }
    hwnd = find_hwnd()
    result["hwnd"] = hwnd
    if not hwnd:
        result["error"] = "窗口没起来（FindWindowW 没找到标题）"
    else:
        dpi = int(user32.GetDpiForWindow(hwnd))
        scale = dpi / 96.0
        expect = _pair([MIN_SIZE[0] * scale, MIN_SIZE[1] * scale])

        def check(name: str, got, want) -> None:
            result["checks"].append(
                {
                    "name": name,
                    "got": _pair(got),
                    "want": _pair(want),
                    "ok": _pair(got) == _pair(want),
                }
            )

        result["dpi"] = dpi
        result["scale"] = round(scale, 4)
        result["expected_min_physical"] = expect
        result["initial"] = rect_of(hwnd)
        result["min_track"] = min_track_of(hwnd)
        check("min_track_equals_scaled_min_size", result["min_track"], expect)

        # 往小里改：两条 API 都必须被顶回期望下限（0x0002|0x0004 = NOMOVE/NOSIZE 关掉）
        user32.SetWindowPos(hwnd, 0, 0, 0, 600, 400, 0x0002 | 0x0004 | 0x0010)
        time.sleep(0.8)
        got = rect_of(hwnd)
        result["after_setwindowpos_600x400"] = got
        check("setwindowpos_shrink_stops_at_floor", (got["width"], got["height"]), expect)

        user32.MoveWindow(hwnd, 100, 100, 500, 300, True)
        time.sleep(0.8)
        got = rect_of(hwnd)
        result["after_movewindow_500x300"] = got
        check("movewindow_shrink_stops_at_floor", (got["width"], got["height"]), expect)

        # 往大里改必须放行，否则这条闸退化成上限
        big = [expect[0] + 300, expect[1] + 200]
        user32.SetWindowPos(hwnd, 0, 0, 0, big[0], big[1], 0x0002 | 0x0004 | 0x0010)
        time.sleep(0.8)
        got = rect_of(hwnd)
        result["after_grow"] = got
        check("grow_still_allowed", (got["width"], got["height"]), big)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    window.destroy()


def main() -> int:
    window = create_window(ServiceStub(), ApiStub())
    webview.start(worker, (window,))
    data = json.loads(OUT.read_text(encoding="utf-8"))
    if data.get("error"):
        print("FAIL", data["error"])
        return 1
    bad = [c for c in data["checks"] if not c["ok"]]
    for c in data["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"], "got", c["got"], "want", c["want"])
    print(
        f"dpi={data['dpi']} scale={data['scale']} MIN_SIZE={tuple(MIN_SIZE)}"
        f" -> 物理下限 {tuple(data['expected_min_physical'])}"
    )
    print("读数：", OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
