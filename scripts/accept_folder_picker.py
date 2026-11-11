"""真窗口验收：桌面端的原生文件/文件夹对话框（走产品自己的 picker）。

为什么需要它：`video_workbench/desktop.py` 的 `_make_picker()` 把
kind 映射到 pywebview 的 OPEN_DIALOG / FOLDER_DIALOG，单测里注入的是假 picker，
验不到「真弹出来的是不是那个框」。历史上这里出过两个真缺陷：

1. 合并页「添加本地文件」传 kind="files"（复数），旧代码只认 "file"，
   于是它弹的是**选择文件夹**——用户选完文件夹，合并页把它当视频文件，
   预检显示「文件不在了」。
2. allow_multiple 恒 False 且只取 result[0]，前端却按 "|" 拆多选结果，
   多选从未实现。

本脚本起一扇真窗，在线程里调产品 picker，另一个线程操作那个 #32770 原生窗
（填编辑框、点按钮），再把返回值与期望比对。读数落 %TEMP%，不落仓库根。

跑法：
    .venv/Scripts/python.exe scripts/accept_folder_picker.py
"""

from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import webview

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

user32 = ctypes.windll.user32

RESULT = Path(tempfile.gettempdir()) / "wb-picker-accept.json"
WORK = Path(tempfile.gettempdir()) / "wb-picker-accept-fixture"
TITLES = ("选择文件夹", "打开", "Select Folder", "Open")


def text_of(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def children(hwnd: int) -> list[int]:
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(child, _lparam):
        out.append(child)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def find_dialog(timeout: float) -> tuple[int, str]:
    """等一个可见的 #32770 且标题在 TITLES 里。"""
    end = time.time() + timeout
    while time.time() < end:
        hits: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and class_of(hwnd) == "#32770":
                hits.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        for h in hits:
            title = text_of(h)
            if any(t in title for t in TITLES):
                return h, title
        time.sleep(0.1)
    return 0, ""


def drive(dialog: int, text: str, accept: str) -> dict:
    """把 text 写进对话框的编辑框，再点 accept 那个按钮。

    编辑框只认类名恰好是 "Edit" 的：地址栏是 ToolbarWindow32，搜索框类名是
    SearchEditBoxWrapperClass——后者也含 "Edit" 字样，按子串匹配会填错地方
    （实测：填完读回来是自己写的值，但对话框的实际选中项没变，返回的还是上次的目录）。
    """
    report: dict = {"edit_class": None, "button": None, "children": []}
    edits: list[int] = []
    buttons: list[tuple[int, str]] = []
    for child in children(dialog):
        cls = class_of(child)
        label = text_of(child)
        if cls == "Edit":
            edits.append(child)
        if cls == "Button":
            buttons.append((child, label))
        if label or cls in ("Edit", "Button"):
            report["children"].append([cls, label])

    target_button = 0
    for h, label in buttons:
        if label == accept:
            target_button = h
            break
    if not target_button:
        for h, label in buttons:
            if label in ("确定", "打开", "OK"):
                target_button = h
                break
    report["button"] = text_of(target_button) if target_button else None

    edit = edits[0] if edits else 0
    report["edit_class"] = class_of(edit) if edit else None
    if edit:
        user32.SendMessageW(edit, 0x000C, 0, text)  # WM_SETTEXT
        time.sleep(0.2)
        report["edit_text_after_set"] = text_of(edit)

    if target_button:
        user32.SendMessageW(target_button, 0x00F5, 0, 0)  # BM_CLICK
    else:
        user32.SendMessageW(dialog, 0x0111, 1, 0)  # WM_COMMAND / IDOK
    return report


def build_fixture() -> dict:
    """两个真视频文件 + 一个目录，都用产品 picker 去选。"""
    WORK.mkdir(parents=True, exist_ok=True)
    out_dir = WORK / "out"
    out_dir.mkdir(exist_ok=True)
    files = []
    for name in ("a.mp4", "b.mp4"):
        path = WORK / name
        if not path.exists():
            path.write_bytes(b"\x00" * 16)
        files.append(path)
    return {"dir": out_dir, "file": files[0], "files": files}


def main() -> int:
    fixture = build_fixture()
    out: dict = {"fixture": {k: str(v) for k, v in fixture.items()}, "legs": []}
    from video_workbench.desktop import _make_picker

    pick = _make_picker()
    window = webview.create_window(
        "WBPickerAccept",
        html="<html><body style='font:14px sans-serif'>picker probe</body></html>",
        width=720,
        height=520,
    )

    def leg(name: str, kind: str, fill: str, accept: str, want) -> None:
        picked: dict = {}

        def ask():
            try:
                picked["value"] = pick(kind)
            except Exception as exc:  # noqa: BLE001 - 探针要把异常记下来
                picked["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        dialog, title = find_dialog(20.0)
        fill_report = drive(dialog, fill, accept) if dialog else None
        thread.join(timeout=25.0)
        value = picked.get("value")
        out["legs"].append(
            {
                "name": name,
                "kind": kind,
                "dialog_title": title,
                "fill": fill_report,
                "picked": value,
                "want": want,
                "ok": value == want,
                "error": picked.get("error"),
            }
        )
        time.sleep(0.6)

    def worker(_window):
        time.sleep(1.2)
        try:
            leg("dir", "dir", str(fixture["dir"]), "选择文件夹", str(fixture["dir"]))
            leg("file", "file", str(fixture["file"]), "打开", str(fixture["file"]))
            # 多选：OpenFileDialog 认引号包裹的多个文件名
            leg(
                "files",
                "files",
                " ".join(f'"{p}"' for p in (fixture["file"], fixture["files"][1])),
                "打开",
                "|".join(str(p) for p in (fixture["file"], fixture["files"][1])),
            )
            out["ok"] = all(item["ok"] for item in out["legs"])
        except Exception as exc:  # noqa: BLE001 - 探针不能静默失败
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            RESULT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            _window.destroy()

    webview.start(worker, window)
    if not RESULT.exists():
        RESULT.write_text(
            json.dumps({"ok": False, "error": "没有读数"}, ensure_ascii=False), encoding="utf-8"
        )
    report = json.loads(RESULT.read_text(encoding="utf-8"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
