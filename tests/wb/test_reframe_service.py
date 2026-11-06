"""P6-02 服务层：主体检测落盘、切主体、关闭增强（`app/service.py`）。

两条约定与 P3 的切片用例一致：

1. **错误看信封**，`dispatch` 不抛异常。
2. **不真抽帧**：`reframe.detect_focus` 被换成一个固定返回，测的是"服务层怎么用检测
   结果"（写哪些文件、返回哪些字段、切主体时读哪份状态）。真抽帧在
   `scripts/accept_p6_focus.py` 里跑真视频。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from video_workbench.core import reframe


def make_project(service, title="跟随用例"):
    return service.repo.project_create(
        title=title,
        source_kind="local",
        root_dir=str(Path(service.cfg.media_root) / "focuscase"),
    )


def add_source(service, project_id, tmp_path: Path):
    video = tmp_path / "source.mp4"
    if not video.is_file():
        video.write_bytes(b"\x00" * 2048)
    service.repo.asset_upsert(project_id, kind="source", path=str(video), size=video.stat().st_size)
    return video


def stub_probe(monkeypatch, *, duration_ms=6000, width=1920, height=1080):
    """别真跑 ffprobe：这里的字节流不是合法视频，测的是服务层而不是 ffmpeg。"""
    from video_workbench.infra import ffmpeg as ffmpeg_mod

    def fake_probe(self, path):
        return ffmpeg_mod.MediaInfo(
            duration_ms=duration_ms,
            width=width,
            height=height,
            fps=30.0,
            vcodec="h264",
            acodec="aac",
            has_video=True,
            has_audio=True,
            bytes=2048,
        )

    monkeypatch.setattr(ffmpeg_mod.FFmpeg, "probe", fake_probe, raising=True)


@pytest.fixture(autouse=True)
def _no_ffprobe(monkeypatch):
    """本文件所有用例都别去碰真 ffprobe（假 mp4 探不出来，会把断言引到错地方）。"""
    stub_probe(monkeypatch)


def call(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok"), out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert not out.get("ok"), out
    return out["error"]["code"]


def wait_job(service, job_id, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务没在 {timeout}s 内结束：{service.jobs.get(job_id)}")


def fake_detect(calls, *, subjects=2, enhance_default=True):
    """造一份"两个主体 + 一处切换"的检测结果，形状与真检测一致。"""

    def _detect(
        source,
        *,
        start_ms,
        end_ms,
        models_root,
        interval_ms=500,
        proxy="",
        log=None,
        enhance=True,
        sampler=None,
        subject="",
    ):
        calls.append({"enhance": enhance, "subject": subject, "source": str(source)})
        if not enhance:
            return {
                "focus": [{"t_ms": 0, "x": 0.4}, {"t_ms": 3000, "x": 0.42}],
                "detector": "haar",
                "notes": [],
                "frames": 6,
                "hits": 3,
                "enhance": False,
            }
        rows = [
            {
                "id": "sub_a",
                "label": "左边那位",
                "track": [{"t_ms": 0, "x": 0.3}],
                "frames": 6,
                "hits": 6,
                "avg_x": 0.3,
                "area": 0.02,
                "box": [100.0, 100.0, 200.0, 200.0],
                "score": 78,
            },
            {
                "id": "sub_b",
                "label": "右边那位",
                "track": [{"t_ms": 0, "x": 0.8}],
                "frames": 3,
                "hits": 3,
                "avg_x": 0.8,
                "area": 0.02,
                "box": [900.0, 100.0, 200.0, 200.0],
                "score": 41,
            },
        ][:subjects]
        chosen = subject or rows[0]["id"]
        return {
            "focus": [{"t_ms": 0, "x": 0.3}, {"t_ms": 3000, "x": 0.32}],
            "detector": "haar",
            "notes": ["找到 2 个主体，可切换跟随对象"],
            "frames": 6,
            "hits": 6,
            "subjects": rows,
            "tracks": {row["id"]: row["track"] for row in rows},
            "cuts": [2000],
            "subject": chosen,
            "thumbs": [],
            "width": 1920,
            "height": 1080,
            "span_ms": max(1, int(end_ms) - int(start_ms)),
            "enhance": True,
        }

    return _detect


def analyze(service, project_id, **params):
    job = call(service, "clip.focus.analyze", {"project_id": project_id, **params})["job"]
    return wait_job(service, job["id"])


def test_analyze_writes_subject_sidecar(service, tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(reframe, "detect_focus", fake_detect(calls))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)

    job = analyze(service, project["id"])
    assert job["status"] == "succeeded", job.get("error_message")
    result = job["result"]
    assert result["subject"] == "sub_a"
    assert result["cuts"] == 1
    assert result["enhance"] is True
    assert calls and calls[0]["enhance"] is True

    state = json.loads(
        (Path(project["root_dir"]) / "focus.subjects.json").read_text(encoding="utf-8")
    )
    assert [row["id"] for row in state["subjects"]] == ["sub_a", "sub_b"]
    assert state["cuts"] == [2000]
    assert state["frame_width"] == 1920

    meta = json.loads((Path(project["root_dir"]) / "focus.json").read_text(encoding="utf-8"))
    assert meta["subject"] == "sub_a"
    assert meta["cuts"] == [2000]
    assert meta["enhance"] is True


def test_clip_focus_reports_subjects(service, tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(reframe, "detect_focus", fake_detect(calls))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)
    analyze(service, project["id"])

    data = call(service, "clip.focus", {"project_id": project["id"]})
    assert [row["id"] for row in data["subjects"]] == ["sub_a", "sub_b"]
    assert data["subject"] == "sub_a"
    assert data["cuts"] == [2000]
    assert data["enhance"] is True


def test_switch_subject_rebuilds_without_detecting(service, tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(reframe, "detect_focus", fake_detect(calls))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)
    analyze(service, project["id"])
    assert len(calls) == 1

    data = call(service, "clip.focus.subject", {"project_id": project["id"], "subject": "sub_b"})
    assert data["subject"] == "sub_b"
    assert data["label"] == "右边那位"
    assert len(calls) == 1, "切主体不许重跑检测"

    meta = json.loads((Path(project["root_dir"]) / "focus.json").read_text(encoding="utf-8"))
    assert meta["subject"] == "sub_b"
    assert meta["cuts"] == [2000]
    # sub_b 在假数据里只有 t=0 一个点，重建后轨迹从 0.8 起
    assert data["focus"][0]["x"] == 0.8


def test_switch_subject_unknown_id_is_not_found(service, tmp_path, monkeypatch):
    monkeypatch.setattr(reframe, "detect_focus", fake_detect([]))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)
    analyze(service, project["id"])
    assert (
        err(service, "clip.focus.subject", {"project_id": project["id"], "subject": "sub_z"})
        == "NOT_FOUND"
    )


def test_switch_subject_without_analysis_conflicts(service):
    project = make_project(service)
    assert (
        err(service, "clip.focus.subject", {"project_id": project["id"], "subject": "sub_a"})
        == "CONFLICT"
    )


def test_analyze_with_enhance_false_skips_subjects(service, tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(reframe, "detect_focus", fake_detect(calls))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)

    job = analyze(service, project["id"], enhance=False)
    assert job["status"] == "succeeded", job.get("error_message")
    assert calls[0]["enhance"] is False
    assert job["result"]["enhance"] is False
    assert job["result"]["subjects"] == []

    meta = json.loads((Path(project["root_dir"]) / "focus.json").read_text(encoding="utf-8"))
    assert meta["enhance"] is False
    assert meta["subject"] == ""
    # 关掉增强也要留下可用的轨迹：跟随本身不能因为它变空
    assert meta["focus"]
    assert (
        err(service, "clip.focus.subject", {"project_id": project["id"], "subject": "sub_a"})
        == "CONFLICT"
    )


def test_config_default_turns_enhance_on(service, tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(reframe, "detect_focus", fake_detect(calls))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)

    assert service.cfg.clip.follow_enhance is True
    analyze(service, project["id"], enhance=None)
    assert calls[0]["enhance"] is True

    service.cfg.clip.follow_enhance = False
    analyze(service, project["id"], enhance=None)
    assert calls[1]["enhance"] is False


def test_manual_points_keep_cuts(service, tmp_path, monkeypatch):
    monkeypatch.setattr(reframe, "detect_focus", fake_detect([]))
    project = make_project(service)
    add_source(service, project["id"], tmp_path)
    analyze(service, project["id"])

    call(
        service,
        "clip.focus.save",
        {"project_id": project["id"], "focus": [{"t_ms": 1000, "x": 0.6}]},
    )
    meta = json.loads((Path(project["root_dir"]) / "focus.json").read_text(encoding="utf-8"))
    assert meta["cuts"] == [2000], "存手工点不能把切点弄丢，否则导出会在切点插值"
    assert meta["subject"] == "sub_a"
    assert any(point.get("manual") for point in meta["focus"])


def test_analyze_without_source_conflicts(service):
    project = make_project(service, title="没有源文件")
    assert err(service, "clip.focus.analyze", {"project_id": project["id"]}) == "CONFLICT"


def test_export_passes_cuts(service, tmp_path, monkeypatch):
    """导出时把切点带进滤镜：这是"切换镜头不跳变"落到命令上的唯一出口。"""
    from video_workbench.infra import ffmpeg as ffmpeg_mod

    seen: list[dict] = []

    def fake_export(self, src, dest, **kwargs):
        seen.append(kwargs)
        Path(dest).write_bytes(b"x" * 32)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ffmpeg_mod.FFmpeg, "export_clip", fake_export, raising=True)
    project = make_project(service)
    add_source(service, project["id"], tmp_path)
    Path(project["root_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(project["root_dir"]) / "focus.json").write_text(
        json.dumps({"focus": [{"t_ms": 0, "x": 0.3}, {"t_ms": 4000, "x": 0.7}], "cuts": [2000]}),
        encoding="utf-8",
    )

    job = service.jobs.submit(
        "clip.export",
        project["id"],
        {
            "project_id": project["id"],
            "source": str(tmp_path / "source.mp4"),
            "output": str(tmp_path / "out.mp4"),
            "start_ms": 0,
            "end_ms": 5000,
            "aspect": "9x16",
            "vertical_mode": "follow",
            "mode": "precise",
        },
    )
    wait_job(service, job["id"])
    assert seen, "导出没有走到 export_clip"
    assert seen[0]["focus_cuts"] == [2000]
    assert seen[0]["vertical_mode"] == "follow"
