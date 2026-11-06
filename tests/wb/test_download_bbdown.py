"""P1-08：BBDown 兜底后端的单元测试。

这里只测"后端选择与参数拼装"这些纯逻辑——真起 BBDown 要看用户机器上有没有那个 exe，
所以下载流程本身用注入的 runner 走一遍，产物归一的规则也在这里钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import downloader as dl


def make_binary(tmp_path: Path, name: str = "BBDown.exe") -> Path:
    path = tmp_path / name
    path.write_bytes(b"MZ fake")
    return path


# --------------------------------------------------------------- 探测


def test_find_bbdown_prefers_configured_path(tmp_path):
    found = make_binary(tmp_path)
    assert dl.find_bbdown(found) == str(found)


def test_find_bbdown_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dl.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))
    assert dl.find_bbdown("") == ""


def test_bbdown_info_reports_hint_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dl.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))
    info = dl.bbdown_info("")
    assert info["found"] is False
    assert "BBDown" in info["hint"]


def test_bbdown_info_reports_name_when_found(tmp_path):
    found = make_binary(tmp_path)
    info = dl.bbdown_info(found)
    assert info == {
        "found": True,
        "path": str(found),
        "name": "BBDown.exe",
        "configured": str(found),
        "hint": "",
    }


def test_ytdlp_info_shape_matches_bbdown_info():
    info = dl.ytdlp_info()
    assert set(info) >= {"found", "path", "name", "hint"}
    assert isinstance(info["found"], bool)


# --------------------------------------------------------------- 命令行


def test_build_bbdown_command_sets_work_dir_and_quality(tmp_path):
    argv = dl.build_bbdown_command(
        binary="BBDown.exe",
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        home=tmp_path,
        quality="720p",
        cookie_file=None,
    )
    assert argv[0] == "BBDown.exe"
    assert argv[1].startswith("https://")
    assert argv[argv.index("--work-dir") + 1] == str(tmp_path)
    assert argv[argv.index("-q") + 1] == "720P"


def test_build_bbdown_command_skips_unknown_quality(tmp_path):
    argv = dl.build_bbdown_command(binary="b", url="u", home=tmp_path, quality="8k")
    assert "-q" not in argv


def test_build_bbdown_command_passes_cookie_and_extra_args(tmp_path):
    argv = dl.build_bbdown_command(
        binary="b",
        url="u",
        home=tmp_path,
        cookie_file=tmp_path / "cookies.txt",
        extra_args="--dfn 1080P",
    )
    assert argv[argv.index("--cookie") + 1].endswith("cookies.txt")
    assert argv[-2:] == ["--dfn", "1080P"]


# --------------------------------------------------------------- 进度解析


def test_parse_bbdown_line_reads_percent():
    event = dl.parse_bbdown_line("[进度] 12.5% (1.2 MB/9.4 MB)")
    assert event is not None
    assert event["percent"] == pytest.approx(12.5)


def test_parse_bbdown_line_ignores_noise():
    assert dl.parse_bbdown_line("开始解析视频") is None
    assert dl.parse_bbdown_line("") is None
    assert dl.parse_bbdown_line("100%")["percent"] == pytest.approx(100.0)


def test_parse_bbdown_line_clamps_over_100():
    assert dl.parse_bbdown_line("999%")["percent"] == pytest.approx(100.0)


# --------------------------------------------------------------- 产物归一


def test_normalize_bbdown_output_renames_to_source(tmp_path):
    produced = tmp_path / "【标题】某视频.mp4"
    produced.write_bytes(b"x" * 10)
    normalized = dl._normalize_bbdown_output(tmp_path, produced)
    assert normalized == tmp_path / "source.mp4"
    assert normalized.exists()
    assert not produced.exists()


def test_normalize_bbdown_output_keeps_source_name(tmp_path):
    produced = tmp_path / "source.mp4"
    produced.write_bytes(b"x")
    assert dl._normalize_bbdown_output(tmp_path, produced) == produced


def test_bbdown_output_picks_newest_video(tmp_path):
    old = tmp_path / "old.mp4"
    old.write_bytes(b"1")
    new = tmp_path / "new.mkv"
    new.write_bytes(b"22")
    # 显式把 old 的时间往前拨一天：同一毫秒内建两个文件时 mtime 可能相等，
    # 那样"最新的"就变成随机挑选，测试会间歇性红。
    stamp = new.stat().st_mtime - 86400
    import os

    os.utime(old, (stamp, stamp))
    assert dl._bbdown_output(tmp_path) == new


# --------------------------------------------------------------- download() 分支


class Recorder:
    """假的 runner：记下 argv，并按 BBDown 的样子往 work-dir 里放一个文件。"""

    def __init__(self, *, product: str = "【标题】某视频.mp4", code: int = 0, out: str = ""):
        self.calls: list[list[str]] = []
        self.product = product
        self.code = code
        self.out = out

    def __call__(self, argv, cwd, on_line, cancel):
        self.calls.append(list(argv))
        if self.product:
            Path(cwd, self.product).write_bytes(b"fake video" * 100)
        for line in (self.out or "[进度] 100% (9.4 MB/9.4 MB)").splitlines():
            on_line(line)
        return self.code, self.out


def test_download_routes_to_bbdown_and_normalizes(tmp_path):
    binary = make_binary(tmp_path)
    home = tmp_path / "ep"
    rec = Recorder()
    result = dl.download(
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        home=home,
        quality="720P",
        backend="bbdown",
        bbdown_bin=str(binary),
        runner=rec,
    )
    assert rec.calls and rec.calls[0][0] == str(binary)
    assert "--work-dir" in rec.calls[0]
    assert result.path == home / "source.mp4"
    assert result.path.exists()


def test_download_bbdown_missing_binary_raises_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(dl.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))
    with pytest.raises(WBError) as info:
        dl.download(
            url="u",
            home=tmp_path / "ep",
            backend="bbdown",
            bbdown_bin=str(tmp_path / "nope.exe"),
            runner=Recorder(),
        )
    assert info.value.code == "DEPENDENCY_MISSING"


def test_download_rejects_unknown_backend(tmp_path):
    with pytest.raises(WBError) as info:
        dl.download(url="u", home=tmp_path, backend="aria2", runner=Recorder())
    assert info.value.code == "VALIDATION"


def test_download_bbdown_failure_is_classified(tmp_path):
    binary = make_binary(tmp_path)
    rec = Recorder(product="", code=1, out="ERROR: 需要大会员才能观看")
    with pytest.raises(WBError) as info:
        dl.download(
            url="u", home=tmp_path / "ep", backend="bbdown", bbdown_bin=str(binary), runner=rec
        )
    assert info.value.code == "AUTH_REQUIRED"


def test_download_bbdown_emits_percent_events(tmp_path):
    binary = make_binary(tmp_path)
    events: list[dict] = []
    dl.download(
        url="u",
        home=tmp_path / "ep",
        backend="bbdown",
        bbdown_bin=str(binary),
        runner=Recorder(out="[进度] 10%\n[进度] 55%"),
        on_event=events.append,
    )
    percents = [e["percent"] for e in events if e.get("type") == "progress"]
    assert 10.0 in percents and 55.0 in percents


def test_download_bbdown_cancel_cleans_partials(tmp_path):
    """取消的口径是"目录里不留半成品"，不是"删掉已经下完的文件"。"""
    binary = make_binary(tmp_path)

    def runner(argv, cwd, on_line, cancel):
        Path(cwd, "x.mp4.part").write_bytes(b"1")
        return -2, ""

    with pytest.raises(WBError) as info:
        dl.download(
            url="u", home=tmp_path / "ep", backend="bbdown", bbdown_bin=str(binary), runner=runner
        )
    assert info.value.code == "CANCELED"
    assert not (tmp_path / "ep" / "x.mp4.part").exists()


def test_default_backend_keeps_ytdlp_path(tmp_path, monkeypatch):
    """不传 backend 时必须还走 yt-dlp——兜底不能变成默认。"""
    seen: list[list[str]] = []

    def runner(argv, cwd, on_line, cancel):
        seen.append(list(argv))
        Path(cwd, "source.mp4").write_bytes(b"v" * 10)
        on_line('wb-meta: {"type": "finished", "path": "%s"}' % (Path(cwd, "source.mp4")))
        return 0, ""

    monkeypatch.setattr(dl, "_module_available", lambda name: True)
    result = dl.download(url="https://b23.tv/x", home=tmp_path / "ep", runner=runner)
    assert seen and "bbdown" not in " ".join(seen[0]).lower()
    assert result.path.name == "source.mp4"


_ = sys  # 让 linters 知道 sys 是有意保留的（测试里留给未来的 platform 分支）
