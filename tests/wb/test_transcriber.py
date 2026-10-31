"""转录编排的测试（P2-01 / P2-02）。

测的是**编排与落库语义**，不是 faster-whisper 本身：
缓存键、命中缓存不重跑、取消后不留半截结果、空结果不推进阶段。
真跑一次 ASR 需要 1.6GB 模型和几十秒，那是验收脚本（scripts/accept_p2_transcribe.py）
的事，不该塞进单测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import transcriber as t
from video_workbench.infra.db import open_and_migrate
from video_workbench.infra.ffmpeg import MediaInfo
from video_workbench.infra.repo import Repo


# ----------------------------------------------------------------- 夹具


class FakeCtx:
    """JobContext 的最小替身：只记录，不连数据库。"""

    def __init__(self, cancel_after: int | None = None):
        self.progress_calls: list[tuple[float | None, str | None]] = []
        self.logs: list[str] = []
        self._tick = 0
        self._cancel_after = cancel_after

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.progress_calls.append((value, stage))

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        self._tick += 1
        return self._cancel_after is not None and self._tick > self._cancel_after

    def check_cancel(self):
        if self.should_cancel():
            raise WBError("CANCELED", "任务已取消")


class FakeFF:
    """不做真转码：写一个假 wav，顺便记住调用次数。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def extract_audio(self, src, dest, **_kw):
        self.calls += 1
        if self.fail:
            raise WBError("PROCESS_FAILED", "没有音轨")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"RIFF0000WAVE" + b"x" * 1024)
        return Path(dest)

    def probe(self, path):  # handler 不用，但注入的是同一对象
        return MediaInfo()


def make_repo(tmp_path: Path) -> Repo:
    return Repo(open_and_migrate(str(tmp_path / "wb.db")))


def make_project(repo: Repo, tmp_path: Path, *, duration_ms: int = 60_000) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    (root / "source.mp4").write_bytes(b"fake-mp4-bytes")
    project = repo.project_create(
        title="标题",
        up_name="UP",
        root_dir=str(root),
        stage="downloaded",
        status="ready",
    )
    repo.asset_upsert(project["id"], "source", str(root / "source.mp4"), bytes=13,
                      duration_ms=duration_ms, status="ready")
    return project


def make_model(models_root: Path, name: str = "tiny") -> None:
    path = models_root / "faster-whisper" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text("{}", encoding="utf-8")
    (path / "model.bin").write_bytes(b"0" * (21 * 1024 * 1024))
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")


def writing_runner(segments=None, *, code: int = 0, error: dict | None = None, on_call=None):
    """假的 ASR 子进程：按 spec 里给的路径写 transcript.json / srt，再回报事件。"""

    def run(spec, on_event, cancel):
        if on_call:
            on_call(spec)
        on_event({"event": "progress", "progress": 0.5, "stage": "转录中 1 段"})
        if error:
            on_event({"event": "error", **error})
            return code or 1
        if code != 0:
            return code
        payload = {
            "language": "zh",
            "duration_ms": 60_000,
            "model": spec.get("model"),
            "segments": segments
            if segments is not None
            else [
                {"id": "s1", "start_ms": 0, "end_ms": 1200, "text": "第一句",
                 "words": [{"start_ms": 0, "end_ms": 600, "text": "第一", "seq": 1}]},
                {"id": "s2", "start_ms": 1200, "end_ms": 2600, "text": "第二句", "words": []},
            ],
        }
        Path(spec["out_json"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        Path(spec["out_srt"]).write_text(t.format_srt(payload["segments"]), encoding="utf-8")
        on_event({"event": "progress", "progress": 1.0, "stage": "完成"})
        return 0

    return run


# ----------------------------------------------------------------- 纯函数


def test_cache_key_changes_with_every_parameter(tmp_path: Path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"abc" * 1000)
    base = t.cache_key(audio, model="tiny", language="zh", vad=True)
    assert base == t.cache_key(audio, model="tiny", language="zh", vad=True)
    assert base != t.cache_key(audio, model="small", language="zh", vad=True)
    assert base != t.cache_key(audio, model="tiny", language="en", vad=True)
    assert base != t.cache_key(audio, model="tiny", language="zh", vad=False)
    assert base != t.cache_key(audio, model="tiny", language="zh", vad=True, word_timestamps=False)

    audio.write_bytes(b"abc" * 999 + b"z")
    assert t.cache_key(audio, model="tiny", language="zh", vad=True) != base, "音频变了键必须变"


def test_srt_format_is_standard():
    text = t.format_srt([
        {"start_ms": 0, "end_ms": 1200, "text": "第一句"},
        {"start_ms": 3_600_000, "end_ms": 3_601_500, "text": "一小时后"},
        {"start_ms": 5000, "end_ms": 6000, "text": "   "},
    ])
    lines = text.strip().splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:01,200"
    # 段与段之间有一个空行（SRT 规范），所以第 2 段从第 5 行开始
    assert lines[4] == "2"
    assert lines[5] == "01:00:00,000 --> 01:00:01,500"
    assert len(lines) == 7, "空文本段要跳过，序号要连续"


# ----------------------------------------------------------------- handler


def test_handler_writes_transcript_and_advances_stage(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models)
    ff = FakeFF()
    handler = t.make_transcribe_handler(
        repo=repo, models_root=models, cfg=type("C", (), {"model": "tiny", "language": "auto",
                                                         "vad": True, "word_timestamps": True,
                                                         "device": "cpu", "compute_type": "int8"})(),
        ff=ff, runner=writing_runner(),
    )

    result = handler(FakeCtx(), {"project_id": project["id"]})

    assert result["cached"] is False
    assert result["segments"] == 2
    root = Path(project["root_dir"])
    assert (root / "transcript.json").is_file()
    srt = (root / "transcript.srt").read_text(encoding="utf-8")
    assert "第一句" in srt and "-->" in srt
    assert ff.calls == 1, "音频要抽一次"

    row = repo.transcript_get(project["id"])
    assert row and row["language"] == "zh" and len(row["segments"]) == 2
    assert repo.project_get(project["id"])["stage"] == "transcribed"

    # 字幕文件必须登记成资产：素材库的"有字幕"筛选和详情页资产表都看这个，
    # 只写 wb_transcript 的话界面上会一直显示"待转录"。
    assets = {a["kind"]: a for a in repo.asset_list(project["id"])}
    subtitle = assets.get("subtitle")
    assert subtitle, f"没有 subtitle 资产：{sorted(assets)}"
    assert subtitle["path"].endswith("transcript.srt")
    assert subtitle["status"] == "ready"
    assert subtitle["bytes"] > 0
    assert row["asset_id"] == subtitle["id"], "transcript 行要指回资产"
    cached = t.read_cache(root)
    assert cached["cache_key"] and cached["model"] == "tiny"


def test_second_run_hits_cache_and_skips_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models)
    calls: list[dict] = []

    def runner(spec, on_event, cancel):
        calls.append(spec)
        return writing_runner()(spec, on_event, cancel)

    handler = t.make_transcribe_handler(
        repo=repo, models_root=models,
        cfg=type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "cpu", "compute_type": "int8"})(),
        ff=FakeFF(), runner=runner,
    )
    handler(FakeCtx(), {"project_id": project["id"]})
    second = handler(FakeCtx(), {"project_id": project["id"]})

    assert second["cached"] is True
    assert len(calls) == 1, "命中缓存不该再起子进程"
    assert repo.transcript_get(project["id"])["segments"], "命中缓存也要保证库里有记录"


def test_cache_miss_when_model_changes(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models, "tiny")
    make_model(models, "base")
    calls: list[str] = []

    def runner(spec, on_event, cancel):
        calls.append(spec["model"])
        return writing_runner()(spec, on_event, cancel)

    cfg = type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                         "word_timestamps": True, "device": "cpu", "compute_type": "int8"})()
    handler = t.make_transcribe_handler(repo=repo, models_root=models, cfg=cfg,
                                        ff=FakeFF(), runner=runner)
    handler(FakeCtx(), {"project_id": project["id"]})
    handler(FakeCtx(), {"project_id": project["id"], "model": "base"})
    assert calls == ["tiny", "base"], "换模型必须重跑"


def test_cancel_kills_and_leaves_no_half_result(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models)

    def runner(spec, on_event, cancel):
        # 先写出半截结果，再"被杀"——父进程必须把它清掉
        Path(spec["out_json"]).write_text('{"segments": [{"id": "s1"}]}', encoding="utf-8")
        return -2

    handler = t.make_transcribe_handler(
        repo=repo, models_root=models,
        cfg=type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "cpu", "compute_type": "int8"})(),
        ff=FakeFF(), runner=runner,
    )
    with pytest.raises(WBError) as info:
        handler(FakeCtx(), {"project_id": project["id"]})
    assert info.value.code == "CANCELED"
    root = Path(project["root_dir"])
    assert not (root / "transcript.json").exists(), "取消后不能留下半截转录"
    assert repo.transcript_get(project["id"]) is None
    assert (root / "audio.wav").exists(), "音频要留着，下次不用重新解码"


def test_worker_error_surfaces_message(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models)
    handler = t.make_transcribe_handler(
        repo=repo, models_root=models,
        cfg=type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "cpu", "compute_type": "int8"})(),
        ff=FakeFF(),
        runner=writing_runner(code=1, error={"code": "PROCESS_FAILED",
                                             "message": "Could not locate cudnn_ops64_9.dll"}),
    )
    with pytest.raises(WBError) as info:
        handler(FakeCtx(), {"project_id": project["id"]})
    assert "cudnn" in str(info.value)
    assert info.value.code == "PROCESS_FAILED"


def test_empty_transcript_does_not_advance_stage(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models)
    handler = t.make_transcribe_handler(
        repo=repo, models_root=models,
        cfg=type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "cpu", "compute_type": "int8"})(),
        ff=FakeFF(), runner=writing_runner(segments=[]),
    )
    result = handler(FakeCtx(), {"project_id": project["id"]})
    assert result["segments"] == 0
    row = repo.project_get(project["id"])
    assert row["stage"] == "downloaded", "没识别到语音不该推进到已转录"
    assert "未识别到语音" in str(row.get("error") or "")


def test_missing_model_reports_dependency(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    handler = t.make_transcribe_handler(
        repo=repo, models_root=tmp_path / "models",
        cfg=type("C", (), {"model": "large-v3-turbo", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "auto", "compute_type": "auto"})(),
        ff=FakeFF(), runner=writing_runner(),
    )
    with pytest.raises(WBError) as info:
        handler(FakeCtx(), {"project_id": project["id"]})
    assert info.value.code == "DEPENDENCY_MISSING"
    assert "large-v3-turbo" in str(info.value)


def test_no_audio_track_is_unsupported(tmp_path: Path):
    repo = make_repo(tmp_path)
    project = make_project(repo, tmp_path)
    models = tmp_path / "models"
    make_model(models)
    handler = t.make_transcribe_handler(
        repo=repo, models_root=models,
        cfg=type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "cpu", "compute_type": "int8"})(),
        ff=FakeFF(fail=True), runner=writing_runner(),
    )
    with pytest.raises(WBError) as info:
        handler(FakeCtx(), {"project_id": project["id"]})
    assert info.value.code == "UNSUPPORTED"


def test_missing_project_reports_not_found(tmp_path: Path):
    repo = make_repo(tmp_path)
    handler = t.make_transcribe_handler(
        repo=repo, models_root=tmp_path / "models",
        cfg=type("C", (), {"model": "tiny", "language": "auto", "vad": True,
                           "word_timestamps": True, "device": "cpu", "compute_type": "int8"})(),
        ff=FakeFF(), runner=writing_runner(),
    )
    with pytest.raises(WBError) as info:
        handler(FakeCtx(), {"project_id": "pj_missing"})
    assert info.value.code == "NOT_FOUND"


def test_runner_kills_child_without_waiting_for_next_line(monkeypatch, tmp_path):
    """取消要在半秒内落成 taskkill，而不是等下一行输出。

    真实场景：解码一段 5 分钟音频时子进程一句都不吐。早先只在"读到一行之后"
    才看取消标志，于是用户点了取消，进程还要跑到当前段落结束才死。
    """
    import threading

    from video_workbench.core import transcriber as tr

    killed: list[int] = []
    gate = threading.Event()

    class FakeStdout:
        def __iter__(self):
            yield '{"event": "progress", "done": 0}' + chr(10)
            gate.wait(5)  # 之后一直"没输出"
            return

    class FakeProc:
        pid = 424242

        def __init__(self):
            self.stdout = FakeStdout()
            self.stderr = None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(tr.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(tr, "kill_tree", lambda pid: killed.append(pid))

    seen: list[dict] = []
    spec = {"_spec_path": str(tmp_path / "spec.json"), "_cwd": str(tmp_path)}
    code = tr._default_runner("python")(spec, seen.append, lambda: bool(seen))
    gate.set()

    assert code == -2, "取消要回 -2，调用方才好标 canceled"
    assert killed == [424242], f"没杀子进程：{killed}"
    assert seen and seen[0]["event"] == "progress", "第一行事件仍要派发"
