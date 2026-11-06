"""P6-04 服务层：三通道交付（`clip.deliver` / `nle.info`）。

约定与其它服务层用例一致：错误看信封（`dispatch` 不抛）、不真跑 ffprobe
（假 mp4 探不出来会把断言引到错地方，真素材在 `scripts/accept_p6_nle.py` 里跑）。

这里最要紧的一条：**"放进剪映草稿箱"必须是显式动作**。默认往项目 `export/jianying`
写，只有用户勾了 `place_in_app`、且本机真检测到草稿箱，才动人家的草稿目录。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from video_workbench.core import nle


def make_project(
    service,
    tmp_path: Path,
    *,
    with_source=True,
    with_subtitle=False,
    with_candidate=True,
    with_copy=False,
) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="analyzed"
    )
    if with_source:
        source = root / "source.mp4"
        source.write_bytes(b"x" * 2048)
        service.repo.asset_upsert(
            project["id"], "source", str(source), bytes=2048, duration_ms=60_000
        )
    if with_subtitle:
        sub = root / "transcript.srt"
        sub.write_text(
            "1\n00:00:12,600 --> 00:00:14,000\n先证伪直觉\n"
            "2\n00:00:34,700 --> 00:00:36,000\n再拆变量\n",
            encoding="utf-8",
        )
        service.repo.asset_upsert(
            project["id"], "subtitle", str(sub), bytes=sub.stat().st_size, language="zh"
        )
        # 草稿里的字幕轨取自转录正文（不是 srt 文件），所以两条都要有
        service.repo.transcript_upsert(
            project["id"],
            segments=[
                {"start_ms": 12_600, "end_ms": 14_000, "text": "先证伪直觉"},
                {"start_ms": 34_700, "end_ms": 36_000, "text": "再拆变量"},
            ],
            language="zh",
            model="tiny",
            srt_path=str(sub),
        )
    candidate = {}
    if with_candidate:
        candidate = service.repo.candidate_insert(
            project["id"],
            kind="highlight",
            start_ms=12_400,
            end_ms=34_800,
            score=86,
            title="先证伪直觉",
            source="ai",
        )
        if with_copy:
            service.repo.candidate_update(
                candidate["id"],
                copy={
                    "title": "先证伪直觉",
                    "description": "三分钟讲清一件事",
                    "tags": ["方法论", "认知"],
                },
            )
            candidate = service.repo.candidate_get(candidate["id"]) or candidate
    return {"project": project, "root": root, "candidate": candidate}


class FakeFFmpeg:
    """只要 probe：交付通道不转码，但草稿要写画布尺寸与源时长。"""

    def __init__(self, *, duration_ms=60_000, width=1920, height=1080, fps=30.0):
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.duration_ms = duration_ms
        self.width = width
        self.height = height
        self.fps = fps
        self.probed = []

    def probe(self, path):
        from video_workbench.infra.ffmpeg import MediaInfo

        self.probed.append(str(path))
        return MediaInfo(
            duration_ms=self.duration_ms,
            width=self.width,
            height=self.height,
            fps=self.fps,
            vcodec="h264",
            acodec="aac",
            has_video=True,
            has_audio=True,
            bytes=2048,
        )


def use_ffmpeg(service, monkeypatch, fake=None):
    fake = fake or FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    return fake


def call(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok"), out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert not out.get("ok"), out
    return out["error"]


def wait_job(service, job_id, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"任务没在 {timeout}s 内结束：{service.jobs.get(job_id)}")


def deliver(service, project_id, candidate_id, **params):
    out = call(
        service,
        "clip.deliver",
        {"project_id": project_id, "candidate_id": candidate_id, **params},
    )
    job = wait_job(service, out["job"]["id"])
    return out, job


def test_nle_info_lists_three_channels(service, monkeypatch):
    monkeypatch.setattr(
        nle,
        "detect_apps",
        lambda home=None: {
            "jianying": {"found": True, "root": "C:/d"},
            "resolve": {"found": False, "root": ""},
        },
    )
    data = call(service, "nle.info")
    keys = [item["key"] for item in data["channels"]]
    assert keys == ["jianying", "otio", "bundle"]
    labels = {item["key"]: item["label"] for item in data["channels"]}
    assert labels["jianying"] == "剪映草稿" and labels["otio"] == "达芬奇 OTIO"
    # 检测到剪映、没检测到达芬奇 → 说明文案要跟着变
    notes = {item["key"]: item["note"] for item in data["channels"]}
    assert "已检测到剪映草稿箱" in notes["jianying"]
    assert "没检测到达芬奇" in notes["otio"]
    assert data["apps"]["jianying"]["found"] is True


def test_deliver_rejects_unknown_candidate(service):
    error = err(service, "clip.deliver", {"project_id": "pj_x", "candidate_id": "cand_x"})
    assert error["code"] == "NOT_FOUND"


def test_deliver_rejects_candidate_of_another_project(service, tmp_path):
    made = make_project(service, tmp_path)
    other = service.repo.project_create(
        title="别的", source_kind="local", root_dir=str(tmp_path / "other")
    )
    error = err(
        service,
        "clip.deliver",
        {"project_id": other["id"], "candidate_id": made["candidate"]["id"]},
    )
    assert error["code"] == "VALIDATION"


def test_deliver_rejects_unknown_channel(service, tmp_path):
    made = make_project(service, tmp_path)
    error = err(
        service,
        "clip.deliver",
        {
            "project_id": made["project"]["id"],
            "candidate_id": made["candidate"]["id"],
            "channel": "premiere",
        },
    )
    assert error["code"] == "VALIDATION"


def test_jianying_draft_goes_to_project_export_by_default(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_subtitle=True)
    use_ffmpeg(service, monkeypatch)
    monkeypatch.setattr(nle, "find_jianying_root", lambda home=None: Path("C:/draftbox"))
    out, job = deliver(service, made["project"]["id"], made["candidate"]["id"])
    assert out["channel"] == "jianying"
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["placed_in_app"] is False
    path = Path(result["path"])
    assert path.parent == made["root"] / "export" / "jianying"
    assert (path / "draft_content.json").is_file()
    assert (path / "draft_meta_info.json").is_file()
    # 没勾"放进剪映"就不能碰人家的草稿箱
    assert "C:/draftbox" not in str(path)
    assert any("草稿放在本项目的 export/jianying" in n for n in result["notes"])
    assert result["subtitle_style_followed"] is False


def test_jianying_subtitles_become_text_track(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_subtitle=True)
    use_ffmpeg(service, monkeypatch)
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"])
    folder = Path(job["result"]["path"])
    doc = json.loads((folder / "draft_content.json").read_text(encoding="utf-8"))
    kinds = [track["type"] for track in doc["tracks"]]
    assert kinds == ["video", "text"]
    text = doc["tracks"][1]["segments"]
    # 12.6s 那句在候选区间（12.4–34.8s）内 → 相对片段起点 200ms
    assert text[0]["target_timerange"]["start"] == 200_000
    # 片段里应当是 22.4 秒（34800-12400）
    assert doc["duration"] == 22_400_000


def test_jianying_places_draft_when_asked_and_detected(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch)
    box = tmp_path / "draftbox"
    box.mkdir()
    monkeypatch.setattr(nle, "find_jianying_root", lambda home=None: box)
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"], place_in_app=True)
    result = job["result"]
    assert result["placed_in_app"] is True
    assert Path(result["path"]).parent == box
    assert any("已放进剪映草稿箱" in n for n in result["notes"])


def test_jianying_falls_back_when_draftbox_missing(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch)
    monkeypatch.setattr(nle, "find_jianying_root", lambda home=None: None)
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"], place_in_app=True)
    result = job["result"]
    assert result["placed_in_app"] is False
    assert Path(result["path"]).parent == made["root"] / "export" / "jianying"
    assert any("没检测到剪映草稿箱" in n for n in result["notes"])


def test_otio_lands_in_export_nle(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(width=1280, height=720))
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"], channel="otio")
    result = job["result"]
    path = Path(result["path"])
    assert path.suffix == ".otio"
    assert path.parent == made["root"] / "export" / "nle"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    clip = doc["tracks"]["children"][0]["children"][0]
    # 22.4s @30fps = 672 帧
    assert clip["source_range"]["duration"]["value"] == 672.0
    assert any("字幕样式不跟随" in n for n in result["notes"])


def test_bundle_has_files_notes_and_copy(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_subtitle=True, with_copy=True)
    use_ffmpeg(service, monkeypatch)
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"], channel="bundle")
    result = job["result"]
    folder = Path(result["path"])
    names = sorted(Path(p).name for p in result["files"])
    assert nle.BUNDLE_NOTE in names
    assert nle.BUNDLE_COPY in names
    assert "片段.mp4" in names and nle.BUNDLE_SUBTITLE in names
    assert "先证伪直觉" in (folder / nle.BUNDLE_COPY).read_text(encoding="utf-8")
    # 没导出过成片 → 包里放源视频，且必须说出来
    assert result["source_is_export"] is False
    assert any("整段源视频" in n for n in result["notes"])


def test_bundle_prefers_exported_clip(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch)
    clip = made["root"] / "clips" / "成片.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"y" * 4096)
    job_id = service.repo.new_job_id() if hasattr(service.repo, "new_job_id") else "job_exp_1"
    service.repo.job_create(
        job_id,
        "clip.export",
        made["project"]["id"],
        {"candidate_id": made["candidate"]["id"], "mode": "precise"},
    )
    service.repo.job_update(
        job_id, status="succeeded", result={"path": str(clip)}, finished_at="2026-01-01T00:00:00Z"
    )
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"], channel="bundle")
    result = job["result"]
    assert result["source_is_export"] is True
    assert Path(result["path"]).joinpath("片段.mp4").is_file()
    assert not any("整段源视频" in n for n in result["notes"])


def test_deliver_without_source_is_conflict(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_source=False)
    use_ffmpeg(service, monkeypatch)
    out = call(
        service,
        "clip.deliver",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job = wait_job(service, out["job"]["id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "CONFLICT"


def test_deliver_registers_export_asset(service, tmp_path, monkeypatch):
    """交付物要进素材库，否则用户下次找不到自己刚生成的草稿。"""
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch)
    _out, job = deliver(service, made["project"]["id"], made["candidate"]["id"])
    exports = [
        a for a in service.repo.asset_list(made["project"]["id"]) if a.get("kind") == "export"
    ]
    assert any(str(e["path"]) == job["result"]["path"] for e in exports)
