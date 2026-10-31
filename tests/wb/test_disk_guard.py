"""磁盘守卫测试（P1-06）。

守卫的验收标准不是"能拦住"，而是**拦得准确**：差一点点的时候不能误报，
真不够的时候必须拦住，而且要说清差多少。
"""

from __future__ import annotations

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import downloader
from video_workbench.infra import files


class Usage:
    def __init__(self, free: int):
        self.total = free * 4
        self.used = free * 3
        self.free = free


def test_guard_passes_when_enough(monkeypatch, tmp_path):
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _p: Usage(10_000))
    assert files.ensure_free_space(tmp_path, 9_000) == {"free": 10_000, "need": 9_000}


def test_guard_raises_when_short(monkeypatch, tmp_path):
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _p: Usage(10_000))
    with pytest.raises(WBError) as info:
        files.ensure_free_space(tmp_path, 10_001)
    assert info.value.code == "DISK_FULL"
    assert "需要" in info.value.message and "可用" in info.value.message, "提示必须报数字"


def test_guard_zero_need_never_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _p: Usage(0))
    assert files.ensure_free_space(tmp_path, 0)["free"] == 0


def test_guard_falls_back_to_existing_parent(monkeypatch, tmp_path):
    """目标目录还没建出来（首次下载）时，要往上找到存在的父目录再量。"""
    seen: list[str] = []

    def fake(_p):
        seen.append(str(_p))
        return Usage(1_000)

    monkeypatch.setattr(files.shutil, "disk_usage", fake)
    files.ensure_free_space(tmp_path / "not-created" / "ep", 10)
    assert Path(seen[0]).exists()


def test_has_free_space_is_boolean(monkeypatch, tmp_path):
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _p: Usage(100))
    assert files.has_free_space(tmp_path, 50) is True
    assert files.has_free_space(tmp_path, 500) is False


def test_downloader_delegates_to_guard(monkeypatch, tmp_path):
    """下载器的 ensure_disk 必须和导出走同一套判断，否则两处阈值迟早对不上。"""
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _p: Usage(10))
    downloader.ensure_disk(tmp_path, 0)  # 不检查
    with pytest.raises(WBError) as info:
        downloader.ensure_disk(tmp_path, 999)
    assert info.value.code == "DISK_FULL"


from pathlib import Path  # noqa: E402  —— 放在末尾是因为上面的用例只用到 tmp_path
