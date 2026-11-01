"""`--check` 在两种形态下都要能交出结果。

打包成 windowed exe 后没有 stdout，`print` 静默丢失——自检只剩退出码，
脚本没法核「几条 RPC、依赖齐不齐」。这条测试钉住「没有 stdout 就落文件」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_workbench import desktop  # noqa: E402


def test_emit_json_prints_when_stdout_exists(capsys):
    path = desktop._emit_json({"ok": True, "n": 1})
    assert path == ""
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"ok": True, "n": 1}


def test_emit_json_falls_back_to_file_without_stdout(tmp_path, monkeypatch):
    fake_exe = tmp_path / "VideoWorkbench.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "stdout", None)
    path = desktop._emit_json({"ok": True, "rpc": ["a", "b"]})
    assert path == str((tmp_path / "wb-check.json").resolve()) or path.endswith("wb-check.json")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert len(data["rpc"]) == 2


def test_emit_json_writes_file_even_when_frozen_stdout_works(tmp_path, monkeypatch):
    """打包态 stdout 会接进日志：打印「成功」但脚本拿不到，所以必须同时落文件。"""
    fake_exe = tmp_path / "VideoWorkbench.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    path = desktop._emit_json({"ok": True, "rpc": ["a"]})
    assert path.endswith("wb-check.json")
    assert json.loads(Path(path).read_text(encoding="utf-8"))["rpc"] == ["a"]
