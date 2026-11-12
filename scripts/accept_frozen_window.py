import ctypes
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

OUT = Path("build/accept")
OUT.mkdir(parents=True, exist_ok=True)
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    user32.SetProcessDPIAware()
except OSError:
    pass

TITLE = "VideoWorkbench"
hwnd = user32.FindWindowW(None, TITLE)
print("hwnd", hwnd)
if not hwnd:
    raise SystemExit("no window")

user32.ShowWindow(hwnd, 9)  # SW_RESTORE
user32.SetForegroundWindow(hwnd)
time.sleep(1.2)

rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
w, h = rect.right - rect.left, rect.bottom - rect.top
print("size", w, h)

hdc = user32.GetWindowDC(hwnd)
mem = gdi32.CreateCompatibleDC(hdc)
bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
gdi32.SelectObject(mem, bmp)
# PW_RENDERFULLCONTENT = 0x2
ok = user32.PrintWindow(hwnd, mem, 2)
print("PrintWindow", ok)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


bi = BITMAPINFOHEADER()
bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
bi.biWidth = w
bi.biHeight = -h
bi.biPlanes = 1
bi.biBitCount = 32
bi.biCompression = 0
stride = w * 4
buf = ctypes.create_string_buffer(stride * h)
got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
print("GetDIBits", got)

img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", stride, 1)
path = OUT / "frozen-01-window.png"
img.convert("RGB").save(path)
print("saved", path, path.stat().st_size)

gdi32.DeleteObject(bmp)
gdi32.DeleteDC(mem)
user32.ReleaseDC(hwnd, hdc)
