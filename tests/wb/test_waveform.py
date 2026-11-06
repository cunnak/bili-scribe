"""波形与音频峰值的测试（P2-03）。

``bucketize`` 是纯函数，测边界；``get_peaks`` 测缓存行为——用一个假的 ffmpeg
可执行文件冒充解码器，这样测试不依赖本机装了 ffmpeg，也不会因为真解码几十兆
音频而变慢。
"""

from __future__ import annotations

import array
import json
from pathlib import Path

from video_workbench.core import waveform


def make_samples(values: list[int]) -> array.array:
    items = array.array("h", values)
    return items


def test_bucketize_keeps_peak_of_each_bucket():
    samples = make_samples([0, 100, -200, 300, 0, 0, -32768, 10])
    peaks = waveform.bucketize(samples, buckets=4)
    assert len(peaks) == 4
    # 8 个采样 4 个桶 → 每桶 2 个。第 2 桶取 [200,300] 的 300，第 4 桶是满幅负值
    assert peaks[0] == 100 * 255 // 32768
    assert peaks[1] == 300 * 255 // 32768
    assert peaks[3] == 255


def test_bucketize_empty_is_flat_line():
    assert waveform.bucketize(make_samples([]), buckets=10) == [0] * 10


def test_bucketize_more_buckets_than_samples():
    peaks = waveform.bucketize(make_samples([1000]), buckets=5)
    assert len(peaks) == 5
    assert sum(peaks) > 0
    assert peaks.count(0) >= 4


def fake_ffmpeg(tmp_path: Path, samples: list[int], monkeypatch) -> str:
    """把外部进程换成"直接吐 pcm"的假实现，单测里不跑真 ffmpeg。"""
    payload = array.array("h", samples).tobytes()
    seen: list[list[str]] = []

    def spawn(args, timeout):
        seen.append(list(args))
        return 0, payload, b""

    monkeypatch.setattr(waveform, "_spawn", spawn)
    path = tmp_path / "ffmpeg-fake.exe"
    path.write_bytes(b"")
    return str(path)


def test_get_peaks_caches_result(tmp_path, monkeypatch):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 32)
    binary = fake_ffmpeg(tmp_path, [100, -2000, 30000], monkeypatch)

    first = waveform.get_peaks(tmp_path, media, ffmpeg_bin=binary, buckets=8)
    assert first["cached"] is False
    assert len(first["peaks"]) == 8
    assert max(first["peaks"]) == 30000 * 255 // 32768

    second = waveform.get_peaks(tmp_path, media, ffmpeg_bin=binary, buckets=8)
    assert second["cached"] is True
    assert second["peaks"] == first["peaks"]

    # 源文件变了（size 变化）缓存必须失效
    media.write_bytes(b"x" * 64)
    third = waveform.get_peaks(tmp_path, media, ffmpeg_bin=binary, buckets=8)
    assert third["cached"] is False


def test_get_peaks_error_is_reported_not_raised(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x")
    result = waveform.get_peaks(tmp_path, media, ffmpeg_bin=str(tmp_path / "nope"))
    assert result["peaks"] == []
    assert "ffmpeg" in (result.get("error") or "")


def test_cache_file_is_plain_json(tmp_path, monkeypatch):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x")
    binary = fake_ffmpeg(tmp_path, [1, 2, 3], monkeypatch)
    waveform.get_peaks(tmp_path, media, ffmpeg_bin=binary, buckets=4)
    payload = json.loads((tmp_path / "peaks.json").read_text(encoding="utf-8"))
    assert payload["version"] == waveform.CACHE_VERSION
    assert payload["buckets"] == 4
    assert isinstance(payload["peaks"], list)
