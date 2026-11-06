"""ASR 子进程的执行阶梯（P2-01 的可靠性部分）。

这些测试不加载真模型——真模型在 ``scripts/accept_p2_transcribe.py`` 里跑。
这里要钉住的是三条回退：GPU→CPU、VAD→无 VAD、以及"失败原因要落盘别重复踩"。
"""

from __future__ import annotations

import json
import sys
import types
import wave
from pathlib import Path

import pytest

from video_workbench.workers import asr_worker as W


def make_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x10" * int(rate * seconds))
    return path


class Seg:
    def __init__(self, text: str, start: float, end: float, words: list | None = None):
        self.text = text
        self.start = start
        self.end = end
        self.words = words or []


class Word:
    def __init__(self, word: str, start: float, end: float):
        self.word = word
        self.start = start
        self.end = end


class Info:
    def __init__(self, language: str = "en", probability: float = 0.9, duration: float = 1.0):
        self.language = language
        self.language_probability = probability
        self.duration = duration


class FakeModel:
    """按 vad_filter 决定产出的假模型，并把调用记下来。"""

    calls: list[dict] = []
    devices: list[str] = []
    plan: list = []  # 每次 transcribe 返回的段数
    fail_on: tuple[str, ...] = ()  # 构造时就失败（缺 cuDNN 之类）
    fail_call: tuple[str, ...] = ()  # 编码时才失败（缺 cublas64_12.dll 的真实形状）

    def __init__(self, path, device, compute_type):
        FakeModel.devices.append(device)
        if device in self.fail_on:
            raise RuntimeError(f"cublas64_12.dll is not found ({device})")
        self.device = device
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"device": self.device, **kwargs, "audio_type": type(audio).__name__})
        FakeModel.calls = self.calls
        if self.device in self.fail_call:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        count = 1
        if FakeModel.plan:
            count = FakeModel.plan.pop(0)
        segs = [
            Seg(f"句子{i}", i * 1.0, i * 1.0 + 0.9, [Word("词", i * 1.0, i * 1.0 + 0.4)])
            for i in range(count)
        ]
        return iter(segs), Info(duration=1.0)


@pytest.fixture(autouse=True)
def fake_asr(monkeypatch):
    FakeModel.calls = []
    FakeModel.devices = []
    FakeModel.plan = []
    FakeModel.fail_on = ()
    FakeModel.fail_call = ()
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    yield
    FakeModel.plan = []


def spec_for(tmp_path: Path, **over) -> dict:
    audio = make_wav(tmp_path / "audio.wav")
    spec = {
        "audio": str(audio),
        "model_path": str(tmp_path / "model"),
        "models_root": str(tmp_path),
        "model": "tiny",
        "language": "auto",
        "device": "cpu",
        "compute_type": "int8",
        "vad": True,
        "word_timestamps": True,
        "duration_ms": 1000,
    }
    spec.update(over)
    return spec


def test_load_audio_reads_wav_into_float32(tmp_path):
    audio = make_wav(tmp_path / "a.wav", seconds=0.5)
    out = W.load_audio(str(audio))
    assert not isinstance(out, str)
    assert out.dtype.name == "float32" and out.shape[0] == 8000


def test_load_audio_falls_back_to_path(tmp_path):
    weird = tmp_path / "a.mp3"
    weird.write_bytes(b"not a wav")
    assert W.load_audio(str(weird)) == str(weird)
    assert W.load_audio(str(tmp_path / "missing.wav")).endswith("missing.wav")


def test_plain_transcription_counts_words(tmp_path):
    result = W.transcribe(spec_for(tmp_path))
    assert len(result["segments"]) == 1
    words = result["segments"][0]["words"]
    assert words and words[0]["seq"] == 1
    assert result["notes"] == []


def test_vad_fallback_when_no_speech_detected(tmp_path):
    FakeModel.plan = [0, 3]  # 第一次（VAD 开）整段静音，第二次（VAD 关）出三段
    result = W.transcribe(spec_for(tmp_path))
    assert "vad_fallback" in result["notes"]
    assert len(result["segments"]) == 3
    assert [c["vad_filter"] for c in FakeModel.calls] == [True, False]
    assert FakeModel.calls[0]["audio_type"] == "ndarray", "音频应该走内存数组，不经 PyAV"


def test_vad_off_never_retries(tmp_path):
    FakeModel.plan = [0]
    result = W.transcribe(spec_for(tmp_path, vad=False))
    assert result["segments"] == [] and result["notes"] == []
    assert len(FakeModel.calls) == 1


def test_cuda_failure_retries_on_cpu_and_is_remembered(tmp_path):
    FakeModel.fail_call = ("cuda",)
    result = W.transcribe(spec_for(tmp_path, device="auto", compute_type="auto"))
    assert "cpu_fallback" in result["notes"]
    assert result["device"] == "cpu"
    assert FakeModel.devices == ["cuda", "cpu"]
    probe = json.loads((tmp_path / ".asr_device.json").read_text(encoding="utf-8"))
    assert "cublas" in probe["cuda_error"]

    # 第二次：auto 直接走 CPU，不再浪费一次加载
    FakeModel.devices = []
    FakeModel.calls = []
    again = W.transcribe(spec_for(tmp_path, device="auto", compute_type="auto"))
    assert FakeModel.devices == ["cpu"]
    assert again["notes"] == []


def test_explicit_cuda_ignores_the_memo(tmp_path):
    (tmp_path / ".asr_device.json").write_text(json.dumps({"cuda_error": "old"}), encoding="utf-8")
    result = W.transcribe(spec_for(tmp_path, device="cuda", compute_type="float16"))
    assert FakeModel.devices == ["cuda"], "用户显式点 cuda 就要真试，不能被缓存劝退"
    assert result["device"] == "cuda"


def test_cuda_load_failure_retries_on_cpu(tmp_path):
    FakeModel.fail_on = ("cuda",)
    result = W.transcribe(spec_for(tmp_path, device="auto", compute_type="auto"))
    assert FakeModel.devices == ["cuda", "cpu"]
    assert "cpu_fallback" in result["notes"]


def test_corrupt_probe_file_does_not_break(tmp_path):
    (tmp_path / ".asr_device.json").write_text("{not json", encoding="utf-8")
    result = W.transcribe(spec_for(tmp_path, device="auto", compute_type="auto"))
    assert result["segments"], "缓存坏了只该重新探测，不该失败"


def test_srt_format_and_timestamps():
    srt = W.format_srt(
        [
            {"text": "你好", "start_ms": 0, "end_ms": 1500},
            {"text": " ", "start_ms": 1500, "end_ms": 2000},
            {"text": "世界", "start_ms": 3_723_456, "end_ms": 3_725_000},
        ]
    )
    assert "00:00:00,000 --> 00:00:01,500" in srt
    assert "01:02:03,456 --> 01:02:05,000" in srt
    assert "2\n" not in srt.split("\n")[4], "空文本段不占编号"
