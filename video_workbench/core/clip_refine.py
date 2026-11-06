"""切片候选的视觉精析（P6-01）。

前面几层（P3-01/02）只看得见字幕：位置对不对、句读吸附准不准、分数怎么算，
全是从文字和弹幕热度推出来的。它有个天然盲区——**文字漂亮的窗口可能是空镜**。

这一层补的就是这个：给 Top N 候选各抽 3 帧画面（开头/中间/结尾）和一小段音频，
交给 vision 模型复核，回来只改三样东西：

- ``score``：更准的分数（画面撑不撑得起）
- ``title``：更具体的标题
- ``reason``：说清加减分依据

**刻意不改 ``start_ms`` / ``end_ms``**。理由有两条：

1. 起止点是 P3-02 用句/词边界 + 2 秒容差吸附出来的，还过了 1 帧对齐校验。
   让模型顺手改时间，等于把它辛苦对齐的东西交给一个只会看图的模型重排。
2. ``wb_candidate`` 里 ``locked`` 的行代表"人已经确认过"，精析只允许改分和文案，
   边界一动，"锁定"就名不副实。

没配 vision 模型时整层跳过（``notes`` 里写清楚），不阻断候选生成——
功能不能被配置页锁住。
"""

from __future__ import annotations

import base64
import json
import math
import re
import wave
from pathlib import Path
from typing import Any, Callable, Sequence

from video_workbench.app.errors import WBError
from video_workbench.infra.jsonfix import parse_json

PROMPT_PATH = Path(__file__).resolve().parent.parent / "resources" / "prompts" / "clip_refine.md"

#: 每个候选抽几帧。3 帧（头/中/尾）是性价比拐点：再多，模型也只是在看同一张幻灯片。
FRAME_COUNT = 3
#: 抽帧宽度。vision 模型通常会把长边压到 768 以内，给 480 足够认画面类型。
FRAME_WIDTH = 480
#: ffmpeg `-q:v`，2 最好 31 最差。6 大概是"能看清是 PPT 还是人"的档位。
FRAME_QUALITY = 6
#: 单次最多精析几个候选。按分数取 Top N——低分候选本来就要被删，不值得再花一次请求。
VISION_TOPN = 6
#: 标题上限，与 prompt 里说的 20 字一致。
TITLE_MAX = 20
#: 单帧 base64 超过这个大小就丢弃（真实 480px JPEG 约 30–60KB，1.5MB 是异常值）。
MAX_IMAGE_BYTES = 1_500_000
#: 音频片段：单声道 16k，最多截 20 秒，只用来算响度/静音比例。
AUDIO_SAMPLE_RATE = 16_000
AUDIO_MAX_SECONDS = 20.0
#: 低于这个 RMS 当作静音（dBFS）。
SILENCE_DBFS = -45.0

SYSTEM = "你是严谨的短视频编导，只输出 JSON。"


# ----------------------------------------------------------------- 小工具


def load_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 文件缺失属于装坏了
        raise WBError("DEPENDENCY_MISSING", f"缺少提示词文件：{PROMPT_PATH.name}") from exc


def stamp(ms: int) -> str:
    """毫秒 → `mm:ss.s`。"""
    total = max(0, int(ms)) / 1000.0
    return f"{int(total // 60):02d}:{total % 60:04.1f}"


def frame_offsets(start_ms: int, end_ms: int, count: int = FRAME_COUNT) -> list[int]:
    """在窗口内均匀取 ``count`` 个时间点（不含端点）。

    不含端点是有意的：切点两侧各差半帧，抽到的可能是上一段或下一段的画面。
    取 ``(i+1)/(count+1)`` 的位置，头尾各留一点余量。
    """
    start = max(0, int(start_ms))
    end = max(start + 1, int(end_ms))
    n = max(1, int(count))
    return [int(start + (end - start) * (i + 1) / (n + 1)) for i in range(n)]


def _strip_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _first_json(text: str) -> str:
    raw = _strip_fence(text)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise WBError("LLM_BAD_RESPONSE", "模型没有返回 JSON 对象")
    return raw[start : end + 1]


# ----------------------------------------------------------------- 抽帧


def extract_frames(
    ffmpeg: Any,
    source: str | Path,
    *,
    start_ms: int,
    end_ms: int,
    out_dir: str | Path,
    stem: str,
    count: int = FRAME_COUNT,
) -> list[Path]:
    """按窗口抽帧。抽不到就返回已抽到的部分（不抛），由调用方决定要不要继续。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, at_ms in enumerate(frame_offsets(start_ms, end_ms, count)):
        dest = out / f"{stem}_{index}.jpg"
        try:
            ffmpeg.thumbnail(
                source,
                dest,
                at=at_ms / 1000.0,
                width=FRAME_WIDTH,
                quality=FRAME_QUALITY,
            )
        except Exception:  # noqa: BLE001 - 单帧失败不该毁掉整次精析
            continue
        if dest.is_file() and dest.stat().st_size:
            paths.append(dest)
    return paths


def data_url(path: str | Path) -> str:
    """读成 `data:image/jpeg;base64,...`。太大的一律丢掉（返回空串）。"""
    file = Path(path)
    size = file.stat().st_size if file.is_file() else 0
    if not size or size > MAX_IMAGE_BYTES:
        return ""
    raw = base64.b64encode(file.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{raw}"


# ----------------------------------------------------------------- 音频片段


def extract_audio_snippet(
    ffmpeg: Any,
    source: str | Path,
    dest: str | Path,
    *,
    start_ms: int,
    end_ms: int,
    seconds: float = AUDIO_MAX_SECONDS,
) -> Path | None:
    """截一小段 wav（单声道 16k）。只为了算响度/静音比例，不做识别。"""
    ffmpeg_bin, _ = ffmpeg.require()
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    span = min(max(0.2, float(seconds)), max(0.2, (int(end_ms) - int(start_ms)) / 1000.0))
    try:
        ffmpeg.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{max(0, int(start_ms)) / 1000.0:.3f}",
                "-t",
                f"{span:.3f}",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-f",
                "wav",
                str(out),
            ],
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - 没音轨/解码失败都只是"没有响度信息"
        return None
    return out if out.is_file() and out.stat().st_size else None


def audio_features(wav_path: str | Path) -> dict[str, float]:
    """从 wav 算峰值/RMS/静音比例。

    没有 numpy 或文件读不了时返回空 dict——上游据此少一句描述，而不是报错。
    """
    try:
        import numpy as np  # noqa: PLC0415 - 只在真要看音频时才需要
    except Exception:  # noqa: BLE001
        return {}
    file = Path(wav_path)
    if not file.is_file():
        return {}
    try:
        with wave.open(str(file), "rb") as fh:
            frames = fh.readframes(fh.getnframes())
            width = fh.getsampwidth()
        if width != 2 or not frames:
            return {}
        data = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    except Exception:  # noqa: BLE001
        return {}
    if data.size == 0:
        return {}
    peak = float(np.max(np.abs(data)))
    rms = float(math.sqrt(float(np.mean(np.square(data)))))
    # 每 50ms 一格看静音比例：整段都静音和"中间有停顿"是两件事
    step = max(1, int(AUDIO_SAMPLE_RATE * 0.05))
    chunks = [data[i : i + step] for i in range(0, data.size, step)]
    quiet = 0
    for chunk in chunks:
        if chunk.size == 0:
            continue
        level = math.sqrt(float(np.mean(np.square(chunk))))
        if 20 * math.log10(max(level, 1e-9)) < SILENCE_DBFS:
            quiet += 1
    return {
        "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 1),
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-9)), 1),
        "silence_ratio": round(quiet / max(1, len(chunks)), 3),
    }


# ----------------------------------------------------------------- 组装请求


def window_text(
    segments: Sequence[dict[str, Any]], start_ms: int, end_ms: int, *, limit: int = 24
) -> str:
    """窗口内的字幕原文。冲突时以"和窗口有重叠"为准，不完全包含的也带进来。"""
    picked: list[dict[str, Any]] = []
    for seg in segments or []:
        start = int(seg.get("start_ms") or 0)
        end = int(seg.get("end_ms") or 0)
        if end <= start_ms or start >= end_ms:
            continue
        picked.append(seg)
    if not picked:
        return "（这一段没有字幕）"
    lines = []
    for seg in picked[:limit]:
        text = str(seg.get("text") or "").strip()
        if text:
            lines.append(f"[{stamp(int(seg['start_ms']))}] {text}")
    return "\n".join(lines) or "（这一段没有字幕）"


def item_block(
    candidate: dict[str, Any],
    segments: Sequence[dict[str, Any]],
    audio: dict[str, float] | None = None,
) -> str:
    start = int(candidate.get("start_ms") or 0)
    end = int(candidate.get("end_ms") or 0)
    lines = [
        f"时间范围：{stamp(start)} – {stamp(end)}（{(end - start) / 1000.0:.1f} 秒）",
        f"类型：{candidate.get('kind') or 'highlight'}",
        f"当前分数：{candidate.get('score')}",
        f"当前标题：{candidate.get('title') or '（无）'}",
    ]
    if audio:
        lines.append(
            "音频：RMS {rms} dBFS，静音占比 {s:.0%}".format(
                rms=audio.get("rms_dbfs"), s=float(audio.get("silence_ratio") or 0)
            )
        )
    lines.append("字幕原文：")
    lines.append(window_text(segments, start, end))
    return "\n".join(lines)


def build_messages(
    candidate: dict[str, Any],
    images: Sequence[str],
    segments: Sequence[dict[str, Any]],
    *,
    prompt_template: str | None = None,
    audio: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """一个候选一条 user 消息：文字在前、图在后。

    每候选单独请求，而不是把 N 个候选塞进一条消息：模型对"哪三张图属于哪个窗口"
    的对齐能力很差，混在一起会拿 A 的画面给 B 打分。
    """
    template = prompt_template if prompt_template is not None else load_prompt()
    text = template.replace("$item", item_block(candidate, segments, audio))
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for url in images:
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": parts},
    ]


# ----------------------------------------------------------------- 解析与应用


def parse_reply(raw: str) -> dict[str, Any]:
    """把模型的一句话收拾成 ``{"score","title","reason"}``。

    三个字段都可缺省：缺的按"没改"处理，不因为模型少写一个字段就把整条丢掉。
    """
    try:
        payload = json.loads(_first_json(raw))
    except WBError:
        raise
    except json.JSONDecodeError as exc:
        payload = parse_json(raw)
        if payload is None:
            raise WBError("LLM_BAD_RESPONSE", "精析返回的不是合法 JSON", str(exc)) from exc
    if not isinstance(payload, dict):
        raise WBError("LLM_BAD_RESPONSE", "精析返回的不是对象")
    out: dict[str, Any] = {}
    raw_score = payload.get("score")
    try:
        score = float(raw_score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        score = None
    if score is not None and not math.isnan(score):
        out["score"] = int(round(max(0.0, min(100.0, score))))
    title = str(payload.get("title") or "").strip()
    if title:
        out["title"] = title[:TITLE_MAX]
    reason = str(payload.get("reason") or "").strip()
    if reason:
        out["reason"] = reason[:200]
    if not out:
        raise WBError("LLM_BAD_RESPONSE", "精析没给出 score/title/reason 中的任何一项")
    return out


def apply(
    candidates: Sequence[dict[str, Any]],
    refined: Sequence[dict[str, Any] | None],
) -> dict[str, Any]:
    """把精析结果贴回去并按新分数重排。

    ``refined[i]`` 是第 i 个候选的结果，``None`` 表示这个候选没精析成功（原样保留）。
    返回的 ``changed`` 只数真正改动了分数或文案的条数——验收脚本和界面都用它。
    """
    rows: list[dict[str, Any]] = []
    changed = 0
    for index, item in enumerate(candidates or []):
        row = dict(item)
        patch = refined[index] if index < len(refined or []) else None
        if patch:
            touched = False
            if "score" in patch and int(patch["score"]) != int(row.get("score") or 0):
                row["score"] = int(patch["score"])
                touched = True
            if patch.get("title") and str(patch["title"]) != str(row.get("title") or ""):
                row["title"] = str(patch["title"])
                touched = True
            if patch.get("reason") and str(patch["reason"]) != str(row.get("reason") or ""):
                row["reason"] = str(patch["reason"])
                touched = True
            if touched:
                row["refined"] = True
                changed += 1
        rows.append(row)
    rows.sort(key=lambda r: (-int(r.get("score") or 0), int(r.get("start_ms") or 0)))
    return {"candidates": rows, "changed": changed}


# ----------------------------------------------------------------- 编排


def refine_candidates(
    candidates: Sequence[dict[str, Any]],
    *,
    client: Any,
    segments: Sequence[dict[str, Any]] = (),
    ffmpeg: Any = None,
    source_path: str | Path = "",
    work_dir: str | Path = "",
    model: str = "",
    prompt_template: str | None = None,
    topn: int = VISION_TOPN,
    temperature: float = 0.2,
    on_progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """对 Top N 候选做一次视觉精析。

    这个函数**不抛**（除了被取消）：抽帧失败、模型抽风、没有源文件，都退化成
    "这条没精析"，把原因写进 ``notes``。理由是一条候选的图看不清，不该让整批
    候选作废——用户要的是能用的候选，不是一次完美调用。

    返回 ``{"candidates", "refined", "failed", "notes", "model", "audio"}``。
    """
    rows = [dict(item) for item in (candidates or [])]
    notes: list[str] = []
    if not rows:
        return {
            "candidates": [],
            "refined": 0,
            "failed": 0,
            "notes": notes,
            "model": model,
            "audio": 0,
        }
    if client is None:
        notes.append("没配 vision 模型，跳过画面精析（分数仍按字幕与弹幕热度算）")
        return {
            "candidates": rows,
            "refined": 0,
            "failed": 0,
            "notes": notes,
            "model": "",
            "audio": 0,
        }
    if not str(source_path or "").strip() or not Path(str(source_path)).is_file():
        notes.append("找不到源视频文件，跳过画面精析")
        return {
            "candidates": rows,
            "refined": 0,
            "failed": 0,
            "notes": notes,
            "model": model,
            "audio": 0,
        }
    if ffmpeg is None or not getattr(ffmpeg, "available", False):
        notes.append("FFmpeg 不可用，抽不出画面，跳过精析")
        return {
            "candidates": rows,
            "refined": 0,
            "failed": 0,
            "notes": notes,
            "model": model,
            "audio": 0,
        }

    root = Path(work_dir or ".") / "refine"
    picked = min(len(rows), max(1, int(topn)))
    results: list[dict[str, Any] | None] = [None] * len(rows)
    failed = 0
    audio_used = 0

    for index in range(picked):
        if should_cancel is not None and should_cancel():
            raise WBError("CANCELED", "任务已取消")
        item = rows[index]
        start = int(item.get("start_ms") or 0)
        end = int(item.get("end_ms") or 0)
        stem = f"c{index:02d}"
        if on_progress:
            on_progress(f"精析第 {index + 1}/{picked} 个候选：{stamp(start)}–{stamp(end)}")
        frames = extract_frames(
            ffmpeg,
            source_path,
            start_ms=start,
            end_ms=end,
            out_dir=root,
            stem=stem,
        )
        images = [url for url in (data_url(path) for path in frames) if url]
        audio: dict[str, float] = {}
        wav = extract_audio_snippet(
            ffmpeg, source_path, root / f"{stem}.wav", start_ms=start, end_ms=end
        )
        if wav is not None:
            audio = audio_features(wav)
            if audio:
                audio_used += 1
        if not images:
            failed += 1
            notes.append(f"「{item.get('title') or stem}」抽不到画面，这条保持原样")
            continue
        messages = build_messages(
            item, images, segments, prompt_template=prompt_template, audio=audio
        )
        try:
            reply = client.chat(messages, temperature=temperature)
        except TypeError:
            # 客户端不认 temperature（早期桩）→ 裸调用一次
            try:
                reply = client.chat(messages)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                notes.append(f"「{item.get('title') or stem}」精析请求失败：{type(exc).__name__}")
                continue
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权
            failed += 1
            notes.append(f"「{item.get('title') or stem}」精析请求失败：{type(exc).__name__}")
            continue
        try:
            results[index] = parse_reply(reply if isinstance(reply, str) else str(reply))
        except WBError as exc:
            failed += 1
            notes.append(f"「{item.get('title') or stem}」{exc.message}")

    packed = apply(rows, results)
    if not packed["changed"]:
        notes.append("精析没有改动任何候选（模型给的分和标题与现状一致）")
    return {
        "candidates": packed["candidates"],
        "refined": packed["changed"],
        "failed": failed,
        "notes": notes,
        "model": model,
        "audio": audio_used,
    }
