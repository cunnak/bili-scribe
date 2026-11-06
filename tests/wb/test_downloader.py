"""P1-02 下载器测试。

一条原则：**这些测试不联网、不装 yt-dlp、不跑 ffmpeg**。所有断言都打在
"命令拼得对不对""进度行认不认得""失败分不分类""取消清不干净"这四件事上，
因为真站行为随时会变，而我们的边界不该跟着变。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import downloader as D

# --------------------------------------------------------------------- 夹具


def fake_runner(lines, *, code=0, produce=None, calls=None):
    """造一个假 runner。

    ``lines`` 会被逐行喂给 ``on_line``（模拟 yt-dlp 的 stdout）；
    ``produce`` 是下载成功后要写到目录里的文件名，由测试决定。
    """

    def run(cmd, cwd, on_line, cancel):
        if calls is not None:
            calls.append({"cmd": cmd, "cwd": cwd})
        for line in lines:
            on_line(line)
        if produce:
            target = Path(cwd) / produce
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-mp4")
            return code, "\n".join(lines) + f"\nwb-meta:1080|137+140|{target}"
        return code, "\n".join(lines)

    return run


def progress_line(**kw) -> str:
    payload = {"status": "downloading", "downloaded_bytes": 512, "total_bytes": 1024, **kw}
    return "download:" + json.dumps(payload)


# --------------------------------------------------------------------- 格式与命令


def test_quality_formats_are_bilibili_friendly():
    # 720P/1080P 要 avc1，剪辑软件对 hevc 支持参差
    assert "avc1" in D.format_selector("720P")
    assert "avc1" in D.format_selector("1080P")
    assert "avc1" not in D.format_selector("1080P+")
    assert "2160" in D.format_selector("4K")


def test_unknown_quality_falls_back_to_default():
    assert D.format_selector("9999P") == D.QUALITY_FORMATS[D.DEFAULT_QUALITY]
    assert D.format_selector("") == D.QUALITY_FORMATS[D.DEFAULT_QUALITY]


def test_build_command_carries_paths_and_cookies(tmp_path):
    cmd = D.build_command(
        yt_dlp_bin="yt-dlp",
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        home=tmp_path / "ep",
        quality="720P",
        cookie_file=tmp_path / "cookies.txt",
        ffmpeg_dir=r"C:\tools",
    )
    home = Path(tmp_path / "ep")
    assert cmd[-1] == "https://www.bilibili.com/video/BV1xx411c7mD", (
        "URL 必须在最后，前面参数不能吃掉它"
    )
    assert cmd[cmd.index("-f") + 1] == D.QUALITY_FORMATS["720P"]
    assert f"home:{home}" in cmd and f"temp:{home / '.tmp'}" in cmd
    assert "source.%(ext)s" in cmd
    assert "--cookies" in cmd and cmd[cmd.index("--cookies") + 1].endswith("cookies.txt")
    assert cmd[cmd.index("--ffmpeg-location") + 1] == r"C:\tools"
    assert "--merge-output-format" in cmd and "mp4" in cmd


def test_single_video_suppresses_playlist(tmp_path):
    single = D.build_command(yt_dlp_bin="yt-dlp", url="u", home=tmp_path, single=True)
    many = D.build_command(yt_dlp_bin="yt-dlp", url="u", home=tmp_path, single=False)
    assert "--no-playlist" in single
    assert "--no-playlist" not in many


# --------------------------------------------------------------------- 进度解析


def test_parse_progress_json():
    event = D.parse_line(progress_line())
    assert event["type"] == "progress"
    assert event["bytes"] == 512 and event["total"] == 1024
    assert event["percent"] == 50.0


def test_parse_progress_uses_estimate_when_total_missing():
    line = "download:" + json.dumps({"downloaded_bytes": 250, "total_bytes_estimate": 1000})
    assert D.parse_line(line)["percent"] == 25.0


def test_parse_progress_tolerates_missing_total():
    # 直播回放/flv 拿不到总大小，不能除零，也不能抛
    line = "download:" + json.dumps({"downloaded_bytes": 4096})
    assert D.parse_line(line)["percent"] == 0.0


def test_parse_meta_line():
    event = D.parse_line("wb-meta:1080|137+140|D:/media/x/source.mp4")
    assert event == {
        "type": "finished",
        "height": 1080,
        "format_id": "137+140",
        "path": "D:/media/x/source.mp4",
    }


def test_parse_merger_line():
    assert D.parse_line('[Merger] Merging formats into "source.mp4"')["type"] == "merging"


def test_parse_ignores_noise():
    for line in ("", "   ", "[info] Downloading 1 format(s)", "download:not-json", "[debug] 乱码"):
        assert D.parse_line(line) is None


# --------------------------------------------------------------------- 失败分类


@pytest.mark.parametrize(
    "text,code",
    [
        ("ERROR: This video requires premium membership", "AUTH_REQUIRED"),
        ("ERROR: 403 Forbidden, login required", "AUTH_REQUIRED"),
        ("ERROR: video is not available in your country", "GEO_BLOCKED"),
        ("ERROR: 稿件不存在或已删除", "NOT_FOUND"),
        ("ERROR: HTTP Error 412: Precondition Failed", "RATE_LIMITED"),
        ("[Errno 28] No space left on device", "DISK_FULL"),
        ("ERROR: unable to download: timed out", "NETWORK"),
        ("something nobody predicted", "PROCESS_FAILED"),
    ],
)
def test_classify_failure(text, code):
    assert D.classify_failure(text).code == code


def test_retryable_flags():
    assert D.classify_failure("timed out").retryable is True
    assert D.classify_failure("HTTP Error 429").retryable is True
    assert D.classify_failure("requires premium membership").retryable is False


def test_classify_keeps_detail_for_diagnostics():
    err = D.classify_failure("download failed\n" + "x" * 5000)
    assert err.code == "PROCESS_FAILED"
    assert len(err.detail) <= 5000, "detail 要截断，否则 SSE 一帧能塞进几 MB"


# --------------------------------------------------------------------- 下载主流程


def test_download_success_reports_actual_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    events: list[dict] = []
    result = D.download(
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        home=tmp_path / "ep",
        quality="1080P",
        on_event=events.append,
        runner=fake_runner(
            [progress_line(), progress_line(downloaded_bytes=1024)], produce="source.mp4"
        ),
    )
    assert result.path.name == "source.mp4"
    assert result.height == 1080
    assert result.format_id == "137+140"
    assert result.quality_actual == "1080P"
    assert result.quality_requested == "1080P"
    assert [e["percent"] for e in events if e["type"] == "progress"][-1] == 100.0


def test_download_falls_back_when_no_meta_line(tmp_path, monkeypatch):
    """老版本 yt-dlp 不给 --print 输出时，不能因为"没拿到元信息"就判失败。"""
    monkeypatch.setattr(D.shutil, "which", lambda name: "yt-dlp")

    def run(cmd, cwd, on_line, cancel):
        (Path(cwd) / "source.mp4").write_bytes(b"x" * 64)
        (Path(cwd) / "source.webm").write_bytes(b"x" * 8)
        return 0, ""

    result = D.download(url="u", home=tmp_path, runner=run)
    assert result.path.name == "source.mp4", "应挑体积最大的产物"
    assert result.quality_actual == ""


def test_download_raises_classified_error(tmp_path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda name: "yt-dlp")
    with pytest.raises(WBError) as info:
        D.download(
            url="u",
            home=tmp_path,
            runner=fake_runner(["ERROR: This video requires premium membership"], code=1),
        )
    assert info.value.code == "AUTH_REQUIRED"


def test_download_cancel_kills_tree_and_cleans_partials(tmp_path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda name: "yt-dlp")
    home = tmp_path / "ep"

    def run(cmd, cwd, on_line, cancel):
        (Path(cwd) / "source.mp4.part").write_bytes(b"partial")
        (Path(cwd) / ".tmp").mkdir(exist_ok=True)
        (Path(cwd) / ".tmp" / "frag.mp4").write_bytes(b"frag")
        return -2, ""

    with pytest.raises(WBError) as info:
        D.download(url="u", home=home, runner=run, cancel=lambda: True)
    assert info.value.code == "CANCELED"
    assert not list(home.rglob("*.part")), "取消后不能留 .part"
    assert not (home / ".tmp" / "frag.mp4").exists()


def test_default_runner_kills_on_cancel(monkeypatch, tmp_path):
    """_default_runner 的取消分支：读到一行就发现 cancel 已经置位。"""

    class FakeStdout:
        def __iter__(self):
            yield "line-1\n"
            yield "line-2\n"

    class FakeProc:
        pid = 4242

        def __init__(self):
            self.stdout = FakeStdout()

        def wait(self, timeout=None):
            return -1

    monkeypatch.setattr(D.subprocess, "Popen", lambda *a, **k: FakeProc())
    killed: list[int] = []
    monkeypatch.setattr(D, "kill_tree", lambda pid: killed.append(pid))
    code, out = D._default_runner(["yt-dlp", "u"], str(tmp_path), lambda _l: None, lambda: True)
    assert code == -2
    assert killed == [4242]
    assert "line-1" in out


def test_download_requires_yt_dlp(tmp_path, monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda name: None)
    monkeypatch.setattr(D, "_module_available", lambda name: False)
    with pytest.raises(WBError) as info:
        D.download(url="u", home=tmp_path)
    assert info.value.code == "DEPENDENCY_MISSING"


# --------------------------------------------------------------------- 守卫与工具


def test_ensure_disk_blocks_when_short(tmp_path, monkeypatch):
    class Usage:
        free = 100

    monkeypatch.setattr(D.shutil, "disk_usage", lambda _p: Usage())
    D.ensure_disk(tmp_path, 50)
    with pytest.raises(WBError) as info:
        D.ensure_disk(tmp_path, 10_000)
    assert info.value.code == "DISK_FULL"


def test_cleanup_partials_is_idempotent(tmp_path):
    assert D.cleanup_partials(tmp_path) == []
    (tmp_path / "a.part").write_bytes(b"1")
    (tmp_path / "b.ytdl").write_bytes(b"1")
    removed = D.cleanup_partials(tmp_path)
    assert {p.name for p in removed} == {"a.part", "b.ytdl"}
    assert D.cleanup_partials(tmp_path) == []


def test_quality_actual_tolerates_odd_heights():
    assert D.DownloadResult(path=Path("x"), height=1078).quality_actual == "1080P"
    assert D.DownloadResult(path=Path("x"), height=2160).quality_actual == "4K"
    assert D.DownloadResult(path=Path("x"), height=1234).quality_actual == "1234P"


def test_frozen_build_uses_self_ytdlp_entry(tmp_path, monkeypatch):
    """打包态不许用 `sys.executable -m yt_dlp`。

    真机教训（2026-10-31 用户报「下载任务失败了」）：exe 里 `sys.executable`
    就是 VideoWorkbench.exe 自己，`-m yt_dlp` 会把主程序再拉起来一遍，
    yt-dlp 压根没跑，用户只看到「yt-dlp 执行失败」。
    """
    monkeypatch.setattr(D.shutil, "which", lambda name: "")
    monkeypatch.setattr(D, "_module_available", lambda name: True)
    monkeypatch.setattr(D.sys, "frozen", True, raising=False)
    monkeypatch.setattr(D.sys, "executable", r"C:\Apps\Video Workbench\VideoWorkbench.exe")
    calls = []
    D.download(
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        home=tmp_path / "ep",
        runner=fake_runner([], produce="source.mp4", calls=calls),
    )
    cmd = calls[0]["cmd"]
    assert cmd[0] == r"C:\Apps\Video Workbench\VideoWorkbench.exe"
    assert cmd[1] == "--ytdlp", "打包态必须走 --ytdlp 入口"
    # 路径里有空格也不能被拆开
    assert "-m" not in cmd[:3]
    assert "--newline" in cmd
