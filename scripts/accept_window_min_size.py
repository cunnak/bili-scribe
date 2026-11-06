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
shcore = ctypes.windll.shcore
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


SW_HIDE, SW_SHOWMINIMIZED = 0, 2

#: 建窗之前桌面上已有的同名窗口；它们一个都不许当被测对象。
KNOWN: list = []


class PLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def titled_hwnds() -> list:
    """枚举全局标题完全等于 TITLE 的顶层窗口（FindWindowW 只回第一个，不可靠）。"""
    found: list = []

    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == TITLE:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def snapshot() -> None:
    """记下开机前桌面上已有的同名窗口。

    这里**故意不枚举全桌面**：枚举本身在 UI 线程里会把 pywebview 炸穿（见 own_hwnd 的
    事故记录），而主线程还没进消息环时枚举是安全的 —— 所以快照在 create_window 之前做。
    """
    del KNOWN[:]
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        KNOWN.append(int(hwnd))


def _is_ours_normal(hwnd: int) -> bool:
    if not user32.IsWindowVisible(hwnd):
        return False
    wp = PLACEMENT()
    wp.length = ctypes.sizeof(wp)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return False
    return wp.showCmd not in (SW_HIDE, SW_SHOWMINIMIZED)


def monitor_dpi_of(hwnd: int) -> int:
    """窗口所在显示器的有效 DPI —— 与 GetDpiForWindow 不一致就说明缩放没落到这个窗口上。"""
    try:
        hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return 0
        x = ctypes.c_uint()
        y = ctypes.c_uint()
        shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y))  # MDT_EFFECTIVE_DPI
        return int(x.value)
    except OSError:
        return 0


def registry_scale():
    """Win10/11 存显示器缩放的地方：HKCU:\\Software\\Microsoft\\Scaling 的 Scale（百分数）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Scaling") as k:
            return winreg.QueryValueEx(k, "Scale")[0]
    except OSError:
        return None


IDENTITY = [""]


def own_hwnd(window) -> int:
    """拿**本次创建**的那扇窗的句柄。

    两条路的教训都在里面：
    1) FindWindowW(None, TITLE) 会返回桌面上任意一个同名窗口 —— 上一轮它返回了一扇
       最小化的残留窗（160x28 @ -32000），三条尺寸判据全在别人窗上测，红绿都不作数。
    2) 在 UI 线程里 EnumWindows + GetWindowTextW 枚举全桌面会把 pywebview 炸穿
       （现场：window.native.AccessibilityObject.Bounds.Empty.Empty... 递归到
       maximum recursion depth exceeded，2026-11-06 21:1x）。所以优先走 pywebview
       自己的对象图拿句柄，枚举只作为兜底，且兜底只查同名窗口里「不是开机前就有的」那一个。
    """
    for attr in ("native", "_native", "form"):
        obj = getattr(window, attr, None)
        handle = getattr(obj, "Handle", None) if obj is not None else None
        if handle is None:
            continue
        try:
            value = int(handle.ToInt32())
        except Exception:
            continue
        if value:
            IDENTITY[0] = "window." + attr + ".Handle"
            return value
    IDENTITY[0] = "find_hwnd 兜底（按开机前快照排除别人的窗）"
    return find_hwnd()


def find_hwnd() -> int:
    """只挑「本次新出现、可见、未最小化」的那个窗口。

    事故实录（2026-11-06 21:0x）：旧版写 FindWindowW(None, TITLE)，桌面上只要有一个同名的
    最小化窗口（上一轮残留、或用户自己开着的实例），它就把那扇窗当被测对象返回 ——
    读数变成 160x28 @ (-32000,-32000)（最小化窗口的经典读数），dpi 还读成 96。
    三条改尺寸的判据全在**别人创建的窗**上测，红绿都不作数。
    """
    for _ in range(200):
        fresh = [h for h in sorted(set(titled_hwnds()) - set(KNOWN)) if _is_ours_normal(h)]
        if fresh:
            return fresh[0]
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
    hwnd = own_hwnd(window)
    result["hwnd"] = hwnd
    result["identity"] = IDENTITY[0]
    result["known_before"] = sorted(KNOWN)
    if not hwnd:
        result["error"] = "窗口没起来（没有新出现的可见未最小化同名窗口）"
    else:
        dpi = int(user32.GetDpiForWindow(hwnd))
        scale = dpi / 96.0
        result["monitor_dpi"] = monitor_dpi_of(hwnd)
        result["registry_scale"] = registry_scale()
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
    snapshot()  # 先记下别人开着的同名窗口，否则 find_hwnd 会测到别人的窗
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
        f"dpi={data['dpi']} scale={data['scale']} 显示器 DPI={data.get('monitor_dpi')}"
        f" 注册表 Scale={data.get('registry_scale')}"
        f" MIN_SIZE={tuple(MIN_SIZE)} -> 物理下限 {tuple(data['expected_min_physical'])}"
    )
    if data.get("monitor_dpi") and data["monitor_dpi"] != data["dpi"]:
        print(
            f"注意：显示器有效 DPI={data['monitor_dpi']}（{data['monitor_dpi'] / 96:.2f}x）"
            f"而这个窗口读到 {data['dpi']} —— 缩放没落到这个进程上，"
            "本次读数不能当 125%/150% 的验收。"
        )
    print("读数：", OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
