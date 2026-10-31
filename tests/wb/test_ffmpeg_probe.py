"""ffprobe 解析与 ffmpeg 封装测试（P1-05 / P1-06）。

不打桩真实 ffmpeg（机器上装没装都不该影响 CI），只验三件事：
**JSON 摊平对不对、命令拼得对不对、失败翻不翻成 WBError。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core.probe import (
    THUMB_NAME,
    guess_aspect,
    is_video,
    make_thumbnail,
    sibling_subtitles,
    thumb_path,
)
from video_workbench.infra.ffmpeg import FFmpeg, best_thumb_time, find_binary, parse_probe


SAMPLE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
         "r_frame_rate": "30000/1001", "duration": "600.5"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"duration": "600.5"},
}


def test_parse_probe_flattens_streams():
    info = parse_probe(SAMPLE, size=1234)
    assert info.duration_ms == 600_500
    assert (info.width, info.height) == (1920, 1080)
    assert info.fps == pytest.approx(29.97, abs=0.01), "分数帧率要真算，不能当字符串"
    assert info.vcodec == "h264" and info.acodec == "aac"
    assert info.has_video and info.has_audio
    assert info.bytes == 1234


def test_parse_probe_handles_audio_only():
    info = parse_probe({"streams": [{"codec_type": "audio", "codec_name": "mp3"}], "format": {"duration": "3"}})
    assert info.has_video is False and info.has_audio is True
    assert info.duration_ms == 3000


def test_parse_probe_survives_garbage():
    info = parse_probe({"streams": [], "format": {"duration": "not-a-number"}})
    assert info.duration_ms == 0 and info.width == 0


def test_find_binary_prefers_config_dir(tmp_path):
    exe = tmp_path / ("ffmpeg.exe" if __import__("os").name == "nt" else "ffmpeg")
    exe.write_text("", encoding="utf-8")
    assert find_binary("ffmpeg", str(tmp_path)) == str(exe)


def test_missing_binaries_raise_dependency():
    ff = FFmpeg(prefer_dir=None)
    ff.prefer_dir = None
    if ff.available:  # 本机真装了 ffmpeg 就跳过这条（CI 上是必过的）
        pytest.skip("本机装了 ffmpeg")
    with pytest.raises(WBError) as info:
        ff.require()
    assert info.value.code == "DEPENDENCY_MISSING"


def test_thumbnail_command_puts_seek_before_input(tmp_path, monkeypatch):
    """``-ss`` 必须在 ``-i`` 之前，否则每次抽帧都要从头解码。"""
    calls: list[list[str]] = []
    ff = FFmpeg(prefer_dir=None)
    monkeypatch.setattr(FFmpeg, "available", property(lambda self: True))
    monkeypatch.setattr(FFmpeg, "require", lambda self: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(FFmpeg, "run", lambda self, args, timeout=None: calls.append(list(args)) or "")

    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    dest = tmp_path / "frames" / THUMB_NAME
    # thumbnail() 会检查产物是否落盘，所以假 run 得顺手把文件"写"出来
    real_run = FFmpeg.run
    monkeypatch.setattr(
        FFmpeg, "run", lambda self, args, timeout=None: (calls.append(list(args)), Path(args[-1]).write_bytes(b"jpg"), "")[2]
    )
    ff.thumbnail(src, dest, at=2.5, width=320)
    monkeypatch.setattr(FFmpeg, "run", real_run)
    args = calls[0]
    assert args.index("-ss") < args.index("-i"), f"-ss 位置错了：{args}"
    assert "-frames:v" in args and "1" in args
    assert "scale=320:-2" in args
    assert args[-1] == str(dest)


def test_thumbnail_without_output_raises(tmp_path, monkeypatch):
    ff = FFmpeg()
    monkeypatch.setattr(FFmpeg, "available", property(lambda self: True))
    monkeypatch.setattr(FFmpeg, "require", lambda self: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(FFmpeg, "run", lambda self, args, timeout=None: "")
    with pytest.raises(WBError):
        ff.thumbnail(tmp_path / "in.mp4", tmp_path / "t.jpg")


def test_extract_audio_uses_whisper_defaults(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    ff = FFmpeg()
    monkeypatch.setattr(FFmpeg, "available", property(lambda self: True))
    monkeypatch.setattr(FFmpeg, "require", lambda self: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(FFmpeg, "run", lambda self, args, timeout=None: calls.append(list(args)) or "")
    out = ff.extract_audio(tmp_path / "in.mp4", tmp_path / "audio.wav")
    args = calls[0]
    assert args[args.index("-ar") + 1] == "16000"
    assert args[args.index("-ac") + 1] == "1"
    assert out.name == "audio.wav"


# ------------------------------------------------------------------ probe 层


def test_is_video():
    assert is_video("a.MP4") and is_video("b.flv") and is_video("c.mkv")
    assert not is_video("a.srt") and not is_video("a.txt")


def test_sibling_subtitles_matches_stem_only(tmp_path):
    video = tmp_path / "第01讲.mp4"
    video.write_bytes(b"x")
    (tmp_path / "第01讲.srt").write_text("1", encoding="utf-8")
    (tmp_path / "第01讲.ass").write_text("1", encoding="utf-8")
    (tmp_path / "第02讲.srt").write_text("1", encoding="utf-8")
    found = sibling_subtitles(video)
    assert {p.suffix for p in found} == {".srt", ".ass"}
    assert all(p.stem == "第01讲" for p in found)


def test_thumb_path_is_fixed():
    assert thumb_path(Path("D:/media/x")).as_posix().endswith("frames/thumb.jpg")


def test_make_thumbnail_returns_none_without_ffmpeg(tmp_path, monkeypatch):
    ff = FFmpeg()
    monkeypatch.setattr(FFmpeg, "available", property(lambda self: False))
    assert make_thumbnail(tmp_path / "in.mp4", tmp_path / "root", ff=ff) is None


def test_make_thumbnail_reuses_existing(tmp_path, monkeypatch):
    root = tmp_path / "root"
    dest = thumb_path(root)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"jpg")
    monkeypatch.setattr(FFmpeg, "available", property(lambda self: True))
    assert make_thumbnail(tmp_path / "in.mp4", root) == dest


def test_best_thumb_time_avoids_head_and_tail():
    assert best_thumb_time(0) == 1.0
    assert best_thumb_time(600_000) == pytest.approx(60.0)
    assert best_thumb_time(2_000) == pytest.approx(1.0)


def test_guess_aspect():
    assert guess_aspect(1920, 1080) == "16:9"
    assert guess_aspect(1080, 1920) == "9:16"
    assert guess_aspect(1080, 1080) == "1:1"
    assert guess_aspect(0, 0) == ""
