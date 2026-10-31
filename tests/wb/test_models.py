"""模型注册表与下载（P2-01 的依赖面）。

下载本身不联网：``_snapshot_download`` 被替换成"假装慢慢写文件"的函数，
这样取消、进度、残留判定才可测。真下载在验收脚本里跑一次。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.infra import models as m


def fake_files(root: Path, name: str, *, size_mb: int = 21, tokenizer: bool = True) -> Path:
    path = root / "faster-whisper" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text("{}", encoding="utf-8")
    (path / "model.bin").write_bytes(b"0" * (size_mb * 1024 * 1024))
    if tokenizer:
        (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    return path


def test_spec_known_and_alias():
    assert m.spec("large-v3-turbo").repo == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert m.spec("turbo").name == "large-v3-turbo"
    assert m.spec("LARGE").name == "large-v3"
    with pytest.raises(WBError) as info:
        m.spec("gpt-4")
    assert info.value.code == "UNSUPPORTED"


def test_is_downloaded_requires_files_and_size(tmp_path: Path):
    assert m.is_downloaded(tmp_path, "tiny") is False
    path = fake_files(tmp_path, "tiny")
    assert m.is_downloaded(tmp_path, "tiny") is True

    # 半截文件（下到一半被打断）不能算已下载——这是最容易骗过用户的状态
    (path / "model.bin").write_bytes(b"0" * 1024)
    assert m.is_downloaded(tmp_path, "tiny") is False

    (path / "model.bin").write_bytes(b"0" * (21 * 1024 * 1024))
    (path / "tokenizer.json").unlink()
    assert m.is_downloaded(tmp_path, "tiny") is False, "缺 tokenizer/vocabulary 不算完整"


def test_status_and_list(tmp_path: Path):
    fake_files(tmp_path, "base")
    rows = m.list_status(tmp_path)
    assert len(rows) == len(m.SPECS)
    base = next(r for r in rows if r["name"] == "base")
    assert base["downloaded"] is True and base["bytes_on_disk"] > 0
    turbo = next(r for r in rows if r["name"] == "large-v3-turbo")
    assert turbo["downloaded"] is False and turbo["approx_label"].endswith("GB")


def test_remove_reports_freed(tmp_path: Path):
    fake_files(tmp_path, "tiny")
    out = m.remove(tmp_path, "tiny")
    assert out["removed"] is True and out["bytes_freed"] > 0
    assert not (tmp_path / "faster-whisper").exists(), "空壳父目录要一起收掉"
    again = m.remove(tmp_path, "tiny")
    assert again["removed"] is False


def test_download_can_be_canceled(tmp_path: Path, monkeypatch):
    release = threading.Event()

    def never_finishes(repo, target, timeout):  # 卡住不产出
        release.wait(2.0)

    monkeypatch.setattr(m, "_snapshot_download", never_finishes)
    flag = {"cancel": False}

    def cancel() -> bool:
        return flag["cancel"]

    def progress(ratio, text):
        flag["cancel"] = True  # 第一次回调就叫停

    with pytest.raises(WBError) as info:
        m.download(tmp_path, "tiny", progress=progress, cancel=cancel, poll_seconds=0.05)
    assert info.value.code == "CANCELED"
    release.set()


def test_download_writes_and_reports(tmp_path: Path, monkeypatch):
    def writes(repo, target, timeout):
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.json").write_text("{}", encoding="utf-8")
        (target / "model.bin").write_bytes(b"0" * (21 * 1024 * 1024))
        (target / "tokenizer.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(m, "_snapshot_download", writes)
    seen: list[float] = []
    out = m.download(tmp_path, "tiny", progress=lambda r, t: seen.append(r), poll_seconds=0.05)
    assert out["downloaded"] is True and out["cached"] is False
    assert seen[-1] == 1.0
    # 已经下好了再点一次：不该再走下载
    again = m.download(tmp_path, "tiny")
    assert again["cached"] is True


def test_download_incomplete_reports_network_error(tmp_path: Path, monkeypatch):
    def writes_partial(repo, target, timeout):
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.json").write_text("{}", encoding="utf-8")
        (target / "model.bin").write_bytes(b"0" * 1024)

    monkeypatch.setattr(m, "_snapshot_download", writes_partial)
    with pytest.raises(WBError) as info:
        m.download(tmp_path, "tiny", poll_seconds=0.05)
    assert info.value.code == "NETWORK"


def test_resolve_for_use_blocks_when_missing(tmp_path: Path):
    with pytest.raises(WBError) as info:
        m.resolve_for_use(tmp_path, "small")
    assert info.value.code == "DEPENDENCY_MISSING"
    fake_files(tmp_path, "small")
    out = m.resolve_for_use(tmp_path, "small")
    assert out["source"] == "local" and Path(out["path"]).is_dir()


def test_dir_size_handles_missing(tmp_path: Path):
    assert m.dir_size(tmp_path / "nope") == 0
    _ = time.time()
