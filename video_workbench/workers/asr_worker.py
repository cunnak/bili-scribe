"""ASR 子进程入口（P2-01）。

**为什么转录必须单开一个进程。** 三个理由，任何一个都足够：

1. 显存和内存只有在进程退出时才真正还回去。CTranslate2 加载 1.6GB 模型之后，
   即便把对象置空，Python 侧也不一定释放——转录十个视频之后主进程就废了。
2. 取消要能杀掉。方案书 §5.4 写得很直白："不能只设标志位"。faster-whisper 在
   解码一段长音频时不会检查任何标志，唯一可靠的中断是 taskkill。
3. 崩了不能把应用带走。缺 cuDNN、显存不足、模型文件损坏，这些都该是一条错误
   事件，不是整个桌面端闪退。

**进程间协议：stdout 上一行一个 JSON。** 只走 stdout，不碰 stderr（faster-whisper
自己和 tqdm 都会往 stderr 写东西，混进来解析就废了）。父进程按行读、按 ``event``
分派。

**GPU 失败要自动退回 CPU。** 本机装了 CUDA 但 cuDNN 版本不匹配是极常见的状态，
表现是一句 ``Could not locate cudnn_ops64_9.dll``。这时候直接失败太粗暴——CPU
跑 int8 慢一点，但能出结果。所以这里先试 GPU，失败就记一条 log 事件再退 CPU。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterable

# 允许 `python -m video_workbench.workers.asr_worker` 在任意 cwd 下跑
if __package__ in (None, ""):  # pragma: no cover - 直接被当脚本执行时
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def emit(payload: dict[str, Any]) -> None:
    """一行一个 JSON，立刻 flush——父进程是逐行读的，缓冲会把它卡死。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _pick_device(requested: str, compute: str) -> tuple[str, str]:
    """把 ``auto`` 落成具体值。CPU 用 int8，GPU 用 float16。"""
    device = (requested or "auto").lower()
    compute = (compute or "auto").lower()
    if device == "auto":
        device = "cpu"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                device = "cuda"
        except Exception:  # noqa: BLE001 —— 探不到就当没有
            device = "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _ms(seconds: float | int | None) -> int:
    return int(round(float(seconds or 0) * 1000))


def _probe_path(models_root: str) -> Path | None:
    root = (models_root or "").strip()
    return Path(root) / ".asr_device.json" if root else None


def _read_probe(path: Path | None) -> str:
    """读过就返回失败原因；没读过或读坏了都返回空串（缓存坏了不该影响转录）。"""
    if path is None or not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("cuda_error") or "") if isinstance(data, dict) else ""


def _write_probe(path: Path | None, reason: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cuda_error": reason[:400], "at": int(time.time())},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_audio(path: str) -> Any:
    """读 16k 单声道 wav，返回 float32 numpy 数组。

    **为什么不直接把路径交给 faster-whisper。** 因为它内部用 PyAV 解码
    （``av.open(..., metadata_errors="ignore")``），而 PyAV 17+ 删掉了这个参数：
    faster-whisper 1.2.1 配 PyAV 19 会当场 ``TypeError``。而我们的音频本来就是
    ffmpeg 规整过的 16k 单声道 PCM——用标准库 ``wave`` 读它是零依赖、零风险，
    顺便省掉一次容器解析。

    只有读不出来（比如外部塞了 flac/mp3）才退回让 faster-whisper 自己解。
    """
    import wave

    import numpy as np

    try:
        with wave.open(path, "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        if width != 2 or channels != 1 or rate != 16000:
            return path
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return samples
    except Exception:  # noqa: BLE001 —— 不是标准 wav 就交给上游解码
        return path


def transcribe(spec: dict[str, Any]) -> dict[str, Any]:
    """真正干活的地方。返回写入的摘要，异常交给 main 转成 error 事件。"""
    from faster_whisper import WhisperModel

    audio = str(spec["audio"])
    model_path = str(spec["model_path"])
    language = str(spec.get("language") or "auto").strip()
    language = "" if language.lower() in ("auto", "") else language
    device, compute = _pick_device(str(spec.get("device") or "auto"), str(spec.get("compute_type") or "auto"))
    vad = bool(spec.get("vad", True))
    want_words = bool(spec.get("word_timestamps", True))
    duration_ms = int(spec.get("duration_ms") or 0)

    def load(dev: str, cpt: str) -> Any:
        return WhisperModel(model_path, device=dev, compute_type=cpt)

    models_root = str(spec.get("models_root") or "")
    probe_path = _probe_path(models_root)
    reason = _read_probe(probe_path)
    auto_device = str(spec.get("device") or "auto").lower() in ("", "auto")
    if auto_device and device == "cuda" and reason:
        emit({"event": "log", "message": f"上次 GPU 运行失败（{reason}），这次直接用 CPU"})
        device, compute = "cpu", "int8"

    state: dict[str, Any] = {"model": None, "device": device, "compute": compute}

    def ensure(dev: str, cpt: str) -> Any:
        """按设备懒加载。换设备才重载——CPU 与 GPU 的权重不能混用。"""
        if state["model"] is None or (state["device"], state["compute"]) != (dev, cpt):
            state["model"] = WhisperModel(model_path, device=dev, compute_type=cpt)
            state["device"], state["compute"] = dev, cpt
        return state["model"]

    emit({"event": "log", "message": f"加载模型 device={device} compute={compute}"})

    emit({"event": "progress", "progress": 0.02, "stage": "解码音频"})
    audio_input = load_audio(audio)
    if not isinstance(audio_input, str):
        emit({"event": "log", "message": f"音频已载入内存 {len(audio_input) / 16000:.1f}s"})

    def consume(use_vad: bool) -> tuple[list[dict[str, Any]], Any, int]:
        model = ensure(str(state["device"]), str(state["compute"]))
        segments_iter, info = model.transcribe(
            audio_input,
            language=language or None,
            vad_filter=use_vad,
            word_timestamps=want_words,
            beam_size=int(spec.get("beam_size") or 5),
            condition_on_previous_text=False,  # 长视频上它会放大重复幻觉
        )
        total = duration_ms or _ms(getattr(info, "duration", 0))
        out: list[dict[str, Any]] = []
        last = 0.0
        for index, seg in enumerate(segments_iter, start=1):
            words: list[dict[str, Any]] = []
            for word in getattr(seg, "words", None) or []:
                text = str(getattr(word, "word", "") or "")
                if not text.strip():
                    continue
                words.append({"start_ms": _ms(word.start), "end_ms": _ms(word.end), "text": text})
            out.append(
                {
                    "id": f"s{index}",
                    "start_ms": _ms(seg.start),
                    "end_ms": _ms(seg.end),
                    "text": str(seg.text or "").strip(),
                    "words": words,
                }
            )
            now = time.time()
            # 每 2 秒最多推一次：方案书 §5.4 的原话，太密会把 RPC 通道刷满
            if now - last >= 2.0 and total > 0:
                last = now
                ratio = min(0.98, _ms(seg.end) / total)
                emit(
                    {
                        "event": "progress",
                        "progress": round(0.05 + ratio * 0.9, 3),
                        "stage": f"转录中 {len(out)} 段",
                        "text": str(seg.text or "").strip()[:80],
                    }
                )
        return out, info, total

    started = time.time()
    notes: list[str] = []
    try:
        segments, info, total_ms = consume(vad)
    except Exception as exc:  # noqa: BLE001 —— GPU 的坑在 encode 阶段才暴露
        # 参数错（语言码非法、模型路径不对）在任何设备上都该原样抛出，
        # 不能被当成"GPU 跑不动"。否则一次手误（比如传了 zh-CN）就会把
        # models_root/.asr_device.json 写成失败标记，之后**每一次**转录都被
        # 强制降到 CPU——性能损失是永久的，而用户根本不知道是自己拼错了。
        if isinstance(exc, (ValueError, TypeError, KeyError, LookupError)):
            raise
        if state["device"] != "cuda":
            raise
        emit({"event": "log", "message": f"GPU 运行失败（{type(exc).__name__}: {exc}），改用 CPU"})
        _write_probe(probe_path, f"{type(exc).__name__}: {exc}")
        notes.append("cpu_fallback")
        # 只改 device 不够：ensure() 认为"模型还在、设备也一致"就会复用那个跑不了的
        # GPU 模型。必须把模型置空，逼它重新加载成 CPU int8。
        state["model"] = None
        state["device"], state["compute"] = "cpu", "int8"
        segments, info, total_ms = consume(vad)

    if not segments and vad:
        # silero 是"说话"检测器，唱歌、说唱、强背景音乐常被判成非语音。整段 0 段
        # 直接告诉用户"没识别到语音"是错的——那会让音乐类素材永远转不出东西。
        emit({"event": "log", "message": "VAD 判断整段没有语音，关闭 VAD 重跑一次"})
        notes.append("vad_fallback")
        segments, info, total_ms = consume(False)

    if not segments:
        emit({"event": "progress", "progress": 1.0, "stage": "未识别到语音"})
        return {
            "segments": [],
            "language": getattr(info, "language", language or ""),
            "duration_ms": total_ms,
            "elapsed_s": round(time.time() - started, 1),
            "device": state["device"],
            "compute_type": state["compute"],
            "notes": notes,
        }

    seq = 0
    for seg in segments:
        for word in seg["words"]:
            seq += 1
            word["seq"] = seq

    return {
        "segments": segments,
        "language": str(getattr(info, "language", language or "")),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
        "duration_ms": total_ms,
        "elapsed_s": round(time.time() - started, 1),
        "device": state["device"],
        "compute_type": state["compute"],
        "notes": notes,
    }


def format_srt(segments: Iterable[dict[str, Any]]) -> str:
    """标准 SRT。时间码用逗号，且必须 ``HH:MM:SS,mmm``——写成点会被不少播放器拒。"""
    lines: list[str] = []
    for index, seg in enumerate(segments, start=1):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{_ts(seg.get('start_ms'))} --> {_ts(seg.get('end_ms'))}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")


def _ts(value: Any) -> str:
    total = max(0, int(value or 0))
    hours, rest = divmod(total, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        emit({"event": "error", "code": "VALIDATION", "message": "缺少任务描述文件"})
        return 2
    try:
        spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "code": "VALIDATION", "message": f"任务描述读不出来：{exc}"})
        return 2

    try:
        result = transcribe(spec)
    except BaseException as exc:  # noqa: BLE001 —— 任何异常都要变成事件离开
        emit(
            {
                "event": "error",
                "code": "PROCESS_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
                "detail": traceback.format_exc()[-4000:],
            }
        )
        return 1

    segments = result.get("segments") or []
    out_json = spec.get("out_json")
    out_srt = spec.get("out_srt")
    if out_json:
        payload = {
            "language": result.get("language"),
            "language_probability": result.get("language_probability", 0.0),
            "duration_ms": result.get("duration_ms", 0),
            "model": spec.get("model"),
            "device": result.get("device"),
            "compute_type": result.get("compute_type"),
            "notes": result.get("notes") or [],
            "segments": segments,
        }
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if out_srt:
        Path(out_srt).parent.mkdir(parents=True, exist_ok=True)
        Path(out_srt).write_text(format_srt(segments), encoding="utf-8")

    emit({"event": "progress", "progress": 1.0, "stage": "完成"})
    emit({"event": "done", "result": {k: v for k, v in result.items() if k != "segments"} | {"segment_count": len(segments)}})
    return 0


if __name__ == "__main__":  # pragma: no cover
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
