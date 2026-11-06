"""转录编排：音频抽取 → 子进程 ASR → transcript.json / transcript.srt / 入库（P2-01 / P2-02）。

**四件必须在这里定下来的事。**

**1. 缓存键是 (音频内容, 模型, 语言, VAD)。** 方案书 §5.4 的原话。用音频的 sha256
而不是视频的：改了音轨指纹必须变。命中就直接复用磁盘上的 transcript.json——
同一集重跑一次要几十秒到几分钟，命中缓存是"再点一次"能不能忍受的唯一区别。

**2. 音频用 wav 落地而不是管道喂。** quicker-whisper 类实现都支持自己解码 mp4，
但那样就没法算缓存键，也没法在第二次转录时省掉解码。``audio.wav`` 16k 单声道
的体积约每分钟 2MB，按 ``retention.audio_days`` 清理，代价可接受。

**3. 子进程只负责出字幕，不负责入库。** 数据库连接不能跨进程共享，而且一旦
worker 里写库，取消时就可能出现"库里有记录、文件没写完"的半状态。父进程等
worker 正常退出后再写库，是原子性最简单可靠的保证。

**4. 取消靠杀进程树，不靠标志位。** 详见 ``workers/asr_worker.py`` 的注释。
这里只保证一件事：杀完之后 ``audio.wav`` 保留（下次不用重新解码），
半截的 ``transcript.json`` 删掉（不能让它伪装成"转过了"）。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

from .. import __version__
from ..app.errors import WBError
from ..app.jobs import JobContext
from ..core.downloader import kill_tree
from ..infra import languages
from ..infra import models as models_mod
from ..infra.db import now_iso
from ..infra.ffmpeg import FFmpeg
from .layout import find_source

TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_SRT = "transcript.srt"
AUDIO_NAME = "audio.wav"
TASK_NAME = "transcript.task.json"
CACHE_NAME = "transcript.wb.json"

#: 已经走过转录之后的阶段。重转录只更新字幕，不把这些阶段往回退。
_DOWNSTREAM_STAGES = frozenset({"clipped", "merged", "published", "summarized"})

CHUNK = 1 << 20


class Runner(Protocol):
    """跑一个 ASR 子进程。测试里换成假的，不用真装模型。"""

    def __call__(self, spec: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
                 cancel: Callable[[], bool]) -> int: ...


# ----------------------------------------------------------------- 工具


def audio_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / AUDIO_NAME


def cache_key(audio: str | os.PathLike[str], *, model: str, language: str, vad: bool,
              word_timestamps: bool = True) -> str:
    """音频内容 + 影响输出的全部参数。参数进键的理由：换了模型结果就是另一份，
    拿旧结果冒充新的比重新算一遍更糟。"""
    digest = hashlib.sha256()
    with open(audio, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    parts = [digest.hexdigest(), f"model={model}", f"lang={language or 'auto'}",
             f"vad={int(bool(vad))}", f"words={int(bool(word_timestamps))}"]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def read_cache(root: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(root) / CACHE_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(root: str | os.PathLike[str], payload: dict[str, Any]) -> None:
    path = Path(root) / CACHE_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def format_srt(segments: list[dict[str, Any]]) -> str:
    """SRT 格式化。和 worker 里那份是同一个规则，但父进程要在"缓存命中"时
    自己写一遍——那时没有 worker 可用。"""
    lines: list[str] = []
    index = 0
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        index += 1
        lines.append(str(index))
        lines.append(f"{timestamp(seg.get('start_ms'))} --> {timestamp(seg.get('end_ms'))}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")


def timestamp(value: Any) -> str:
    total = max(0, int(value or 0))
    hours, rest = divmod(total, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WBError("PROCESS_FAILED", "转录结果读不出来", str(exc)) from exc
    if not isinstance(data, dict):
        raise WBError("PROCESS_FAILED", "转录结果格式不对", str(path))
    return data


def _source_video(project: dict[str, Any], repo: Any) -> Path:
    root = Path(str(project.get("root_dir") or ""))
    if not str(root):
        raise WBError("NOT_FOUND", "这个项目没有目录记录")
    # DB 优先，兜底规则只在 layout.find_source 一处。
    # 早先是反过来写的（先猜 root/"source.mp4" 再查库）：源文件名跟着视频标题走之后，
    # 那个硬编码猜测对任何新下载都不成立，还会把"猜中了旧文件"当成正确结果。
    prefer = ""
    for asset in repo.asset_list(project["id"]):
        if str(asset.get("kind") or "") != "source":
            continue
        path = str(asset.get("path") or "")
        if path and Path(path).is_file():
            prefer = path
            break
    found = find_source(root, prefer=prefer)
    if found is not None:
        return found
    raise WBError("NOT_FOUND", "找不到原始视频文件", str(root))


def _worker_command(spec_path: Path, python: str | None = None) -> list[str]:
    """拼出让 ASR worker 跑起来的命令行。

    开发态是 ``python -m video_workbench.workers.asr_worker <spec>``；
    **打包态没有 python.exe**，``sys.executable`` 是我们自己的 exe，所以改成
    ``VideoWorkbench.exe --asr-worker <spec>``——由 ``desktop.main`` 在开窗前
    分派。这条分支不写，打出来的包"能下载不能转录"，而且报错是
    ``No module named video_workbench``，看起来像环境问题（方案书 §12 已点过名）。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--asr-worker", str(spec_path)]
    return [python or sys.executable, "-m", "video_workbench.workers.asr_worker", str(spec_path)]


def _default_runner(python: str | None = None) -> Runner:
    """起 ``python -m video_workbench.workers.asr_worker <spec>``，逐行读事件。"""

    def run(spec: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
            cancel: Callable[[], bool]) -> int:
        spec_path = Path(spec["_spec_path"])
        spec_path.write_text(json.dumps({k: v for k, v in spec.items() if not k.startswith("_")},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = _worker_command(spec_path, python)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.Popen(  # noqa: S603 —— 参数是列表，不经 shell
            cmd,
            cwd=str(Path(spec["_cwd"])),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            # CREATE_NEW_PROCESS_GROUP（可 Ctrl-Break 取消）+ CREATE_NO_WINDOW
            # （打包态 windowed 程序没有控制台，不加会每转录一次闪一个黑框）
            creationflags=(0x00000200 | 0x08000000) if os.name == "nt" else 0,
        )
        canceled = False
        assert proc.stdout is not None
        # 读线程 + 队列：解码一段长音频时子进程几分钟不吐一行，
        # 若只在"收到一行之后"看取消标志，用户点了取消要等到段落结束才生效。
        # 这里让主线程每 0.3s 醒一次，取消能在半秒内落成 taskkill。
        lines: "queue.Queue[str | None]" = queue.Queue()

        def _pump() -> None:
            try:
                for raw in proc.stdout:  # type: ignore[union-attr]
                    lines.put(raw)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        reader = threading.Thread(target=_pump, name="asr-stdout", daemon=True)
        reader.start()
        try:
            while True:
                if cancel() and not canceled:
                    canceled = True
                    kill_tree(proc.pid)
                    break
                try:
                    line = lines.get(timeout=0.3)
                except queue.Empty:
                    continue
                if line is None:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    on_event(json.loads(line))
                except ValueError:
                    continue
        finally:
            try:
                code = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                code = -1
            if canceled:
                code = -2
            if code != 0 and not canceled:
                tail = ""
                if proc.stderr is not None:
                    try:
                        tail = proc.stderr.read()[-1500:]
                    except (OSError, ValueError):
                        tail = ""
                if tail.strip():
                    on_event({"event": "log", "message": tail.strip()[-600:]})
        return code

    return run


# ----------------------------------------------------------------- 主流程


def make_transcribe_handler(
    *,
    repo: Any,
    models_root: str | os.PathLike[str],
    cfg: Any,
    ff: FFmpeg | None = None,
    runner: Runner | None = None,
    tool_version: str = __version__,
) -> Callable[[JobContext, dict[str, Any]], dict[str, Any]]:
    """装配 handler。所有外部依赖都从这里进来，测试时换成假的即可。"""
    ffmpeg = ff or FFmpeg()
    run_worker = runner or _default_runner()

    def handler(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        project = repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        root = Path(str(project.get("root_dir") or ""))
        if not root.is_dir():
            raise WBError("NOT_FOUND", "项目目录不存在", str(root))

        model_name = str(payload.get("model") or getattr(cfg, "model", models_mod.DEFAULT_MODEL))
        # 这里再做一次归一化：jobs.retry() 会把库里的旧 payload 原样重新提交，
        # 那些记录写于加校验之前。纵深防御，不指望上游一定干净。
        language = languages.normalize(
            payload.get("language") or getattr(cfg, "language", "auto")
        )
        vad = bool(payload.get("vad", getattr(cfg, "vad", True)))
        want_words = bool(payload.get("word_timestamps", getattr(cfg, "word_timestamps", True)))
        device = str(payload.get("device") or getattr(cfg, "device", "auto"))
        compute = str(payload.get("compute_type") or getattr(cfg, "compute_type", "auto"))

        ctx.check_cancel()
        ctx.progress(0.02, stage="准备音频")
        video = _source_video(project, repo)
        audio = audio_path(root)
        if not audio.is_file() or audio.stat().st_size == 0:
            try:
                ffmpeg.extract_audio(video, audio)
            except WBError:
                # 抽不出声轨是常见情况（无声视频、纯音乐封面稿）。这不是"转录失败"，
                # 是"这集没话说"，需要让用户看到明确的原因。
                raise WBError("UNSUPPORTED", "这条视频没有可用的音轨", str(video))
            repo.asset_upsert(project_id, "audio", str(audio),
                              bytes=audio.stat().st_size, status="ready")

        ctx.progress(0.05, stage="检查缓存")
        key = cache_key(audio, model=model_name, language=language, vad=vad,
                        word_timestamps=want_words)
        cached = read_cache(root)
        json_path = root / TRANSCRIPT_JSON
        if cached.get("cache_key") == key and json_path.is_file():
            data = _load_json(json_path)
            segments = data.get("segments") or []
            srt_path = root / TRANSCRIPT_SRT
            if not srt_path.is_file():
                srt_path.write_text(format_srt(segments), encoding="utf-8")
            ctx.progress(0.98, stage="命中缓存")
            _finish(repo, project_id, segments=segments, language=str(data.get("language") or ""),
                    model=model_name, srt_path=srt_path, audio=audio, stage="transcribed")
            return {"project_id": project_id, "cached": True, "segments": len(segments),
                    "language": data.get("language"), "srt": str(srt_path)}

        resolved = models_mod.resolve_for_use(models_root, model_name)
        ctx.progress(0.08, stage="加载模型")
        # worker 先写这两个临时名，父进程确认成功后才 os.replace 到正式名。
        # 直接让 worker 覆盖正式文件的话，中途失败会把上一版还能用的字幕一起
        # 带走——而"选错语言 → 重转 → 失败"恰恰是这个功能最容易出现的路径。
        tmp_json = root / (TRANSCRIPT_JSON + ".tmp")
        tmp_srt = root / (TRANSCRIPT_SRT + ".tmp")
        spec = {
            "audio": str(audio),
            "model_path": resolved["path"],
            "models_root": str(models_root),
            "model": resolved["model"],
            "language": language,
            "device": device,
            "compute_type": compute,
            "vad": vad,
            "word_timestamps": want_words,
            "duration_ms": int(project.get("duration_ms") or 0),
            "out_json": str(tmp_json),
            "out_srt": str(tmp_srt),
            "_spec_path": str(root / TASK_NAME),
            "_cwd": str(Path(__file__).resolve().parents[2]),
        }

        errors: list[dict[str, Any]] = []

        def on_event(event: dict[str, Any]) -> None:
            kind = event.get("event")
            if kind == "progress":
                ratio = float(event.get("progress") or 0.0)
                stage = str(event.get("stage") or "转录中")
                preview = str(event.get("text") or "").strip()
                if preview:
                    # JobContext 只有 stage 一个自由文案位；把识别到的当前句接在
                    # 阶段名后面，界面上就是"转录中 12 段 · 今天讲的是…"。
                    stage = f"{stage} · {preview[:40]}"
                ctx.progress(0.08 + max(0.0, min(1.0, ratio)) * 0.88, stage=stage)
            elif kind == "log":
                ctx.log(str(event.get("message") or ""))
            elif kind == "error":
                errors.append(event)

        code = run_worker(spec, on_event, ctx.should_cancel)
        try:
            Path(spec["_spec_path"]).unlink(missing_ok=True)
        except OSError:
            pass

        if code == -2 or ctx.should_cancel():
            # 只清临时文件。上一次的正式字幕是用户唯一的退路，取消不该把它带走。
            _wipe_outputs(root, tmp_only=True)
            raise WBError("CANCELED", "已取消转录")
        if code != 0 or errors:
            _wipe_outputs(root, tmp_only=True)
            first = errors[0] if errors else {}
            raise WBError(
                "PROCESS_FAILED",
                str(first.get("message") or f"转录进程退出码 {code}"),
                str(first.get("detail") or "")[:2000],
            )
        if not tmp_json.is_file():
            raise WBError(
                "PROCESS_FAILED", "转录进程结束但没有产出 transcript.json", str(tmp_json)
            )

        ctx.progress(0.97, stage="写入字幕")
        # 到这一步才动正式文件。os.replace 在同分区上是原子的：不存在
        # "transcript.json 写了一半"这种中间态，也不会留下下次会被误判成
        # 缓存命中的半截结果。
        os.replace(tmp_json, json_path)
        data = _load_json(json_path)
        segments = data.get("segments") or []
        detected = str(data.get("language") or language or "")
        srt_path = root / TRANSCRIPT_SRT
        if tmp_srt.is_file():
            os.replace(tmp_srt, srt_path)
        else:  # pragma: no cover - worker 总会写 srt，这里只是不让流程断
            srt_path.write_text(format_srt(segments), encoding="utf-8")
        write_cache(root, {"cache_key": key, "model": model_name, "language": detected,
                           "vad": bool(vad), "word_timestamps": bool(want_words),
                           "created_at": now_iso(), "tool_version": tool_version,
                           "segments": len(segments)})
        _finish(repo, project_id, segments=segments, language=detected, model=model_name,
                srt_path=srt_path, audio=audio, stage="transcribed")
        return {"project_id": project_id, "cached": False, "segments": len(segments),
                "language": detected, "srt": str(srt_path)}

    return handler


def _wipe_outputs(root: Path, *, tmp_only: bool = False) -> None:
    """清转录产物。

    ``tmp_only=True`` 是默认工作路径：worker 写的是 ``*.tmp``，失败或取消时
    只把半截的临时文件删掉，正式字幕原封不动。用户选错语言重转一次失败，
    不该连上一版能用的字幕一起没了。
    """
    names = [TRANSCRIPT_JSON + ".tmp", TRANSCRIPT_SRT + ".tmp"]
    if not tmp_only:
        names += [TRANSCRIPT_JSON, TRANSCRIPT_SRT]
    for name in names:
        try:
            (root / name).unlink(missing_ok=True)
        except OSError:
            pass


def _finish(repo: Any, project_id: str, *, segments: list[dict[str, Any]], language: str,
            model: str, srt_path: Path, audio: Path, stage: str) -> None:
    """入库 + 阶段推进。缓存命中也走这里，保证两条路径的落库行为完全一致。"""
    # 字幕文件要作为资产登记：素材库的"有字幕"筛选、详情页的资产表都按
    # wb_asset.kind == "subtitle" 判断。只写 wb_transcript 的话，转完了界面
    # 仍然显示"待转录"——踩过，见实现偏差记录 P2。
    asset = repo.asset_upsert(
        project_id,
        "subtitle",
        str(srt_path),
        language=language,
        model=model,
        bytes=srt_path.stat().st_size if srt_path.is_file() else 0,
        status="ready" if segments else "empty",
    )
    repo.transcript_upsert(
        project_id,
        segments=segments,
        language=language,
        model=model,
        srt_path=str(srt_path),
        asset_id=asset.get("id") if asset else None,
        status="ready" if segments else "empty",
    )
    # 重转录不该把已经切片/合并/发布/总结过的项目打回 transcribed：下游产物
    # 仍然有效，阶段一倒退，界面就会提示"还没切片"，用户得手动再走一遍流水线。
    # 与总结任务的处理保持一致（service.py 的摘要任务有同类保护）。
    current = str((repo.project_get(project_id) or {}).get("stage") or "")
    keep = current in _DOWNSTREAM_STAGES
    if segments:
        repo.project_update(project_id, stage=current if keep else stage,
                            status="ready", error=None)
    else:
        # 没识别到语音是"结果为空"，不是失败：方案书 §7 的约定，不自动总结。
        repo.project_update(project_id, stage=current if keep else "downloaded",
                            status="ready", error="未识别到语音")
    _ = audio
