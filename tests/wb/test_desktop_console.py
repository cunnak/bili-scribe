"""打包态的两个「看得见」的坑：状态条计数、子进程黑窗口。

窗口那件事没法在 CI 里数窗口数量，这里只钉住「补丁真的把 CREATE_NO_WINDOW 加上了」——
真正的确认只能靠人看一眼（下载时不该再闪终端）。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from video_workbench import desktop


def test_hide_console_children_adds_flag_when_frozen(monkeypatch):
    seen: dict = {}
    original_init = subprocess.Popen.__init__

    def recorder(self, *args, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        raise RuntimeError("stop here")  # 不真起进程

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(subprocess.Popen, "__init__", recorder)
    if hasattr(subprocess.Popen, "_wb_no_window"):
        monkeypatch.delattr(subprocess.Popen, "_wb_no_window")
    try:
        desktop._hide_console_children()
        with pytest.raises(RuntimeError):
            subprocess.Popen(["cmd.exe", "/c", "echo", "hi"], creationflags=0x200)
    finally:
        monkeypatch.setattr(subprocess.Popen, "__init__", original_init)
        if hasattr(subprocess.Popen, "_wb_no_window"):
            delattr(subprocess.Popen, "_wb_no_window")

    flags = int(seen.get("creationflags") or 0)
    assert flags & desktop._CREATE_NO_WINDOW, "没加 CREATE_NO_WINDOW，子进程会开黑窗口"
    assert flags & 0x200, "原来的 CREATE_NEW_PROCESS_GROUP 不能被冲掉"


def test_hide_console_children_is_noop_when_not_frozen(monkeypatch):
    original_init = subprocess.Popen.__init__
    monkeypatch.delattr(sys, "frozen", raising=False)
    if hasattr(subprocess.Popen, "_wb_no_window"):
        monkeypatch.delattr(subprocess.Popen, "_wb_no_window")
    desktop._hide_console_children()
    assert not getattr(subprocess.Popen, "_wb_no_window", False), "源码态不该打补丁"
    assert subprocess.Popen.__init__ is original_init
