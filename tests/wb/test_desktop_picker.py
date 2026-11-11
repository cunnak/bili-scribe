# -*- coding: utf-8 -*-
"""桌面层文件选择器的 kind 语义。

这一层最容易坏的不是"对话框开不开"，而是"开哪一个"：`files` 一旦落进
文件夹框，用户选完目录会被当成一个视频文件，预检只会说"文件不在了"。
所以这里钉的是每次调用实际传下去的 dialog 类型、allow_multiple 和返回值形状。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from video_workbench import desktop  # noqa: E402


class FakeWindow:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def create_file_dialog(self, dialog_type, **kwargs):
        self.calls.append({"dialog_type": dialog_type, **kwargs})
        return self.result


def use_window(monkeypatch, result):
    window = FakeWindow(result)
    monkeypatch.setattr(desktop.webview, "windows", [window], raising=False)
    return window


def test_files_kind_opens_multi_file_dialog_and_joins_with_pipe(monkeypatch):
    window = use_window(monkeypatch, ["C:/a.mp4", "C:/b.mp4"])
    picked = desktop._make_picker()("files")
    assert picked == "C:/a.mp4|C:/b.mp4"
    assert window.calls == [{"dialog_type": desktop.webview.OPEN_DIALOG, "allow_multiple": True}]


def test_file_kind_opens_single_file_dialog(monkeypatch):
    window = use_window(monkeypatch, ["C:/cover.png"])
    picked = desktop._make_picker()("file")
    assert picked == "C:/cover.png"
    assert window.calls == [{"dialog_type": desktop.webview.OPEN_DIALOG, "allow_multiple": False}]


def test_audio_kind_also_opens_single_file_dialog(monkeypatch):
    window = use_window(monkeypatch, ["C:/bgm.mp3"])
    assert desktop._make_picker()("audio") == "C:/bgm.mp3"
    assert window.calls == [{"dialog_type": desktop.webview.OPEN_DIALOG, "allow_multiple": False}]


def test_dir_kind_opens_folder_dialog(monkeypatch):
    window = use_window(monkeypatch, ["C:/out"])
    assert desktop._make_picker()("dir") == "C:/out"
    assert window.calls == [{"dialog_type": desktop.webview.FOLDER_DIALOG, "allow_multiple": False}]


def test_unknown_kind_defaults_to_folder(monkeypatch):
    window = use_window(monkeypatch, ["C:/out"])
    assert desktop._make_picker()("whatever") == "C:/out"
    assert window.calls[0]["dialog_type"] == desktop.webview.FOLDER_DIALOG


def test_cancel_returns_none(monkeypatch):
    use_window(monkeypatch, None)
    assert desktop._make_picker()("files") is None


def test_empty_items_are_dropped_before_joining(monkeypatch):
    use_window(monkeypatch, ["C:/a.mp4", ""])
    assert desktop._make_picker()("files") == "C:/a.mp4"


def test_no_window_returns_none():
    assert desktop._make_picker()("files") is None
