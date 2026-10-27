"""打包后的 exe 到底能不能用 —— 只有跑起来、拍下来，才算数。

    python tests/probe_exe.py

它做四件事，顺序即因果：

1. 启动 `dist/bili-scribe.exe`（不带 --debug，模拟真实双击）；
2. 等窗口真的出现（按标题找，不靠 sleep 猜）；
3. **把窗口内容截图存盘**——这是唯一能证明"界面渲染出来了"的证据。
   进程活着但白屏，用进程状态是看不出来的；
4. 检查 exe 旁边有没有意外冒出来的文件（配置文件该只在保存时才创建）。

退出码 0 = 通过。任何一步失败都打印具体到"卡在哪"，不靠人看截图猜。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "bili-scribe.exe"
SHOT = ROOT / "docs" / "design" / "app-exe.png"
TITLE = "bili-scribe"

user32 = ctypes.windll.user32

# **必须声明 DPI 感知。** 不声明的话，系统会对本进程做坐标虚拟化：屏幕是 150%
# 缩放时窗口矩形报的是"逻辑坐标"，而 `WindowFromPoint` / `ImageGrab` 用的是物理
# 坐标，两者差一个缩放倍数——采样点全都落到窗口外面，判成"到处被遮挡"。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 2 = PER_MONITOR_AWARE
except Exception:  # 老系统没有 shcore，或已经设过
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def find_window(title_part: str, timeout: float = 25.0) -> int:
    """按「标题 + 窗口类」找窗口，返回 hwnd（0 = 没找到）。

    两个坑必须同时避开，否则会认错窗口并且看起来"通过了"：

    - **不能按 pid 找**：PyInstaller 单文件模式先起一个引导进程，再由它拉起真正
      的主进程，PID 对不上。
    - **不能只按标题找**：浏览器标签页标题里完全可能含有同一个词（开发这个项目
      时，Harness 自己的页面标题里就有 "bili-scribe"），于是会截到 Chrome 上去，
      截出一张"内容很丰富"的图，检查全部通过——但那根本不是我们的窗口。

    所以类名必须是 `WindowsForms10.*`（winforms 后端），标题必须**以**项目名开头
    且**不是**后面跟着别的浏览器尾巴。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def each(hwnd, _lparam):  # noqa: ANN001 - ctypes 回调签名固定
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if not title.startswith(title_part):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if not cls.value.startswith("WindowsForms10"):
                return True
            found.append(hwnd)
            return False  # 找到了就停

        user32.EnumWindows(each, 0)
        if found:
            return found[0]
        time.sleep(0.4)
    return 0


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def raise_to_top(hwnd: int) -> None:
    """把窗口提到最前面，好让截屏只截到它。

    **`SetForegroundWindow` 单独用是不够的。** Windows 不允许后台进程随便抢
    前台焦点，这个进程是脚本起的、没有用户输入，调用通常**静默失败**（返回
    False，但它返回什么不重要，重要的是窗口没上来）。实测后果：截图里整个
    右半边是另一个正在跑的模拟器窗口，压在我们的界面上——而颜色数高达 6 万多，
    "有内容"这条判据完全通过，看起来一切正常。

    可靠的做法是先置顶（`HWND_TOPMOST`）、再取消置顶，一轮下来窗口必然被提到
    最前。配合 `ShowWindow(SW_RESTORE)` 处理最小化。
    """
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW

    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.6)


def visible_rect(hwnd: int) -> "wt.RECT | None":
    """窗口**看得见**的那块矩形。

    **别用 `GetWindowRect` 截屏。** 它返回的是窗口矩形，在现代 Windows 上比
    实际可见范围大一圈：从 Vista 起窗口边框由 DWM 合成，那圈不可见缩放边框
    仍算在窗口矩形里（左右各 8px、底部 8px 上下）。直接按它截，会把窗口背后
    的桌面内容一起截进来——表现为右侧和底部多出一条"碎片"，看起来像界面溢出
    到窗口外了，实际上是截图取景框取大了。实测：`GetWindowRect` 得 1360x860，
    真正可见 1344x828，差值就是那圈边框。

    `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` 拿的是合成器认为的
    可见边界。老系统或接口失败时退回 `GetWindowRect`（截大了总比截不到强）。
    """
    try:
        dwmapi = ctypes.windll.dwmapi
        drect = wt.RECT()
        # DWMWA_EXTENDED_FRAME_BOUNDS = 9
        if dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(9),
            ctypes.byref(drect), ctypes.sizeof(drect),
        ) == 0 and drect.right - drect.left > 0:
            return drect
    except Exception:
        pass
    rect = wt.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect
    return None


def is_unobstructed(hwnd: int, rect: "wt.RECT") -> bool:
    """窗口这块区域，屏幕上真的显示的是它自己的画面吗？

    提出这个检查是因为**截图会骗人**：`ImageGrab` 截的是屏幕，谁的窗口在最上面
    就截到谁。实测两次：一次右半边整个是另一个正在跑的模拟器窗口，一次右下角
    弹出个输入法候选框——两次"颜色数够多"的判据都通过了，因为盖上去的东西也很花。

    做法很直接：在窗口矩形里取几个采样点，问系统那个坐标上最顶层的窗口是谁
    （`WindowFromPoint`），比对它是不是我们窗口或它的子孙。只采点而不逐像素，
    因为浮层通常覆盖一大片，几个点就够；漏判的代价只是回退到 `PrintWindow`。
    """
    # 采样点**必须避开四角**：Windows 11 的窗口是圆角的，圆角那一小块是透明的，
    # 那个坐标上本来就该是别的东西。第一版取了 `(right-8, bottom-8)`，于是永远
    # 判成"被遮挡"，然后一直等、一直等到超时——看起来像探针坏了，实际是判据错了。
    #
    # 也不能只取五个点：实测一次输入法候选框从右下角伸进来，五个点全落在它旁边，
    # 判成"没被遮挡"，截出来的图右下角是一块键盘。所以取**网格**——浮层是成片的，
    # 网格一定能撞上。
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    inset = 24
    cols, rows = 8, 5
    pts = []
    for r in range(rows):
        for c in range(cols):
            pts.append((
                rect.left + inset + (w - 2 * inset) * c // (cols - 1),
                rect.top + inset + (h - 2 * inset) * r // (rows - 1),
            ))
    for x, y in pts:
        hit = user32.WindowFromPoint(wt.POINT(x, y))
        if not hit:
            return False
        # 命中的可能是子窗口（WebView2 是独立的子 HWND），往上找到根再看。
        root = user32.GetAncestor(hit, 2)  # GA_ROOT = 2
        if root != hwnd:
            return False
    return True


def print_window(hwnd: int):
    """让窗口**自己画一份**到内存位图，绕开屏幕上的一切遮挡。返回 (img, w, h)。

    `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 是现代版：让窗口把完整内容
    （含被 DWM 合成的部分）画到我们给的画布上，不经过屏幕。实测 1346x853 拿到
    5000+ 种颜色，和真实界面一致。

    **它的代价是拿不到标题栏**：标题栏由 DWM 单独合成，不在客户区里。所以只在
    窗口被挡住时才用它——平时用屏幕截图，那张才有标题栏（图标、版本号都在上面，
    是验收的一部分）。

    另一个用途更重要：**判断"页面画出来了没有"只能靠它**。屏幕截图在窗口被挡
    时截到的是别人的画面，颜色数照样几万——那条判据就白等了。这个函数不受影响。
    """
    rect = visible_rect(hwnd)
    if rect is None:
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)

    # 这一步就是整个函数的全部意义，**忘了它画布永远是空的**。
    # 漏掉时的表现极具误导性：位图是新建的、尺寸全对、颜色数恒为 1（全黑），
    # 看起来像"窗口没渲染"，实际是没人往里画。第一版就漏了，
    # 于是探针每次都在等一张永远不会被填满的画布，15 秒超时。
    PW_RENDERFULLCONTENT = 0x00000002
    if not user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
        # 老系统不认这个 flag，退回 0（只画客户区，够判断有没有内容）
        user32.PrintWindow(hwnd, mem, 0)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    info = BITMAPINFOHEADER()
    info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.biWidth = w
    info.biHeight = -h  # 负数 = 从上到下，省一次翻转
    info.biPlanes = 1
    info.biBitCount = 32
    info.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return (img, w, h)


def shoot(hwnd: int, out: Path) -> tuple[int, int] | None:
    """把窗口区域截下来。返回 (宽, 高)，失败返回 None。

    **首选屏幕截图，因为它带标题栏**；但只在确认窗口没被遮挡时才用——被遮挡时
    截到的是别人的画面，而且"颜色数够多"这类判据照样通过。所以先问一句
    `is_unobstructed`，不通过就退回 `print_window`。
    """
    rect = visible_rect(hwnd)
    if rect is None:
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    try:
        from PIL import ImageGrab
    except ImportError:
        print("[!] 没有 Pillow，跳过截图")
        return (w, h)

    raise_to_top(hwnd)
    if not is_unobstructed(hwnd, rect):
        print("[i] 窗口被别的程序挡住了，改用 PrintWindow 让窗口自己画（不含标题栏）")
        got = print_window(hwnd)
        if got is None:
            return None
        img, w, h = got
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return (w, h)

    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    img = img.convert("RGB")

    # 截下来的图里有多少"不是我们界面"的东西？判据很粗但够用：
    # 界面本身是近黑 + 酸性黄绿的极窄色域，被别的窗口压住时会突然冒出大片
    # 中间调。数一数近黑像素占比——正常界面绝大多数像素落在最暗的一档。
    hist = img.convert("L").histogram()
    total = sum(hist) or 1
    dark = sum(hist[:48]) / total
    if dark < 0.55:
        print(f"[!] 只有 {dark:.0%} 的像素是暗底，窗口可能被别的程序盖住了")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return (w, h)


def kill_all() -> int:
    """杀掉所有 bili-scribe.exe 进程，返回杀掉的个数。

    **光 `proc.kill()` 不够。** PyInstaller 单文件模式的进程结构是
    「引导进程 → 真正的应用进程」两层：`proc.kill()` 杀的是外面那一层，
    里面那层还活着。它活着就会一直占着 exe 文件，下一次打包报
    `PermissionError: [WinError 5] 拒绝访问: dist\bili-scribe.exe`——
    看起来像磁盘或权限问题，实际是上一次没清干净。

    所以这里按镜像名全清一遍。用 `taskkill` 而不是 psutil：不引入新依赖。
    """
    result = subprocess.run(
        ["taskkill", "/F", "/IM", "bili-scribe.exe"],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
    )
    if result.returncode == 0:
        return sum(1 for line in (result.stdout or "").splitlines() if "PID" in line)
    return 0


def cleanup() -> None:
    killed = kill_all()
    if killed:
        print(f"[i] 清掉了 {killed} 个残留进程")
    time.sleep(1.0)  # 等文件句柄真的释放，不然紧接着打包照样失败


def wait_painted(hwnd: int, timeout: float = 15.0, need: int = 200) -> int:
    """等到窗口真的画出东西，返回看到的颜色数；超时返回 0。

    判据是"颜色数"而不是"有没有变化"：深色主题的底本来就接近纯黑，
    白屏和黑屏在"非白即黑"这个层面完全一样，只有颜色**种类**能把它们分开。
    实测正常渲出来的界面有 5000+ 种颜色，纯底色的空白窗口不到 10 种。

    **颜色数不区分"我们的内容"和"盖在上面的窗口"。** 一开始没意识到这点：
    另一个程序压在上面时颜色数照样几万，这条判据心安理得地通过。所以**用
    `PrintWindow` 数**——它让窗口自己画，屏幕上是别人的画面也不影响。

    曾经试过"被遮挡就跳过这一轮、等它露出来"，结果探针三次全部超时：挡住它的
    是输入法候选框，它不会自己消失。判据必须能在"被遮挡"这个状态下给出答案，
    而不是指望环境变好。
    """
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return -1  # 没有 Pillow，跳过这个判据（返回非 0，不让它误判失败）

    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        vr = visible_rect(hwnd)
        if vr is None:
            return 0
        if vr.right - vr.left <= 0:
            time.sleep(0.3)
            continue
        got = print_window(hwnd)
        if got is not None:
            img, _w, _h = got
            last = len(img.getcolors(maxcolors=1_000_000) or [])
        if last >= need:
            return last
        time.sleep(0.5)
    return 0


def main() -> int:
    if not EXE.exists():
        print(f"[x] 找不到 {EXE}，先跑 python scripts/build-exe.py")
        return 2

    # 开跑前先清一次：上一次探针异常退出会留下占着 exe 的进程。
    cleanup()

    before = {p.name for p in EXE.parent.iterdir()}
    print(f"启动 {EXE.name} ...")
    # 关键：cwd 故意设成系统临时目录，不是 exe 目录。
    # 双击快捷方式时的工作目录不一定是 exe 旁边，这样才测出真实情况。
    proc = subprocess.Popen(
        [str(EXE)],
        cwd="C:\\Windows\\Temp",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    hwnd = find_window(TITLE)
    if not hwnd:
        print("[x] 25 秒内没等到窗口。")
        cleanup()
        out, err = proc.communicate(timeout=10)
        for label, blob in (("stdout", out), ("stderr", err)):
            text = (blob or b"").decode("utf-8", "replace").strip()
            if text:
                print(f"--- {label} ---\n{text[-3000:]}")
        log = EXE.parent / "bili-scribe.log"
        if log.exists():
            print(f"--- {log.name} ---\n{log.read_text(encoding='utf-8', errors='replace')[-3000:]}")
        return 1

    print(f"[v] 窗口出现：{window_text(hwnd)!r}")

    # **找到窗口不等于画完了。** WebView2 是独立的渲染进程，窗口句柄先出现、
    # 内容后到；这时截图会得到一张纯黑底（背景色 #0A0B0D），而"黑"恰好是
    # 深色主题的一部分，人眼很容易把它当成"设计就是这样"放过。
    # 所以这里必须等到画面真的有内容，再截、再判。
    painted = wait_painted(hwnd)
    if not painted:
        print("[x] 15 秒内窗口一直没画出内容（一直近乎纯色）。")
        cleanup()
        return 1
    print(f"[v] 画面已渲染（{painted} 种颜色）")

    # 渲染稳定后再截一张：刚出内容的那一帧可能还缺字体/图片。
    time.sleep(1.0)
    size = shoot(hwnd, SHOT)
    if size:
        print(f"[v] 截图 {size[0]}x{size[1]} → {SHOT}")
    else:
        print("[!] 截图失败")

    if SHOT.exists():
        try:
            from PIL import Image

            img = Image.open(SHOT).convert("RGB")
            colors = img.getcolors(maxcolors=1_000_000) or []
            uniq = len(colors)
            print(f"[i] 截图里有 {uniq} 种颜色", end="")
            if uniq < 200:
                print(" —— 偏少，界面可能没画全")
            else:
                print(" —— 有内容")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 数颜色失败：{exc}")

    # 收尾必须清干净：两层进程，只 kill 外层会留下一个占着 exe 的孤儿。
    cleanup()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass

    after = {p.name for p in EXE.parent.iterdir()}
    # bili-scribe.log 是设计上要写的（打包态没有控制台，日志是唯一的现场），
    # 其余任何新文件都算意外。
    new = sorted(after - before - {"bili-scribe.log"})
    if new:
        print(f"[i] 运行后新增文件：{new}")
        if "config.yaml" in new:
            # 只是启动不该写配置——那说明有人在启动路径上调了 save_config。
            print("[!] 启动就写了 config.yaml，设置页的\"保存了却没生效\"会从这里开始")
    else:
        print("[v] 启动没有在 exe 旁边乱写文件（日志除外）")

    print("\n[v] exe 能启动、能开窗、能渲染。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
