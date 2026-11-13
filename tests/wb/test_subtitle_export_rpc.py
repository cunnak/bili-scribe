# -*- coding: utf-8 -*-
"""字幕导出 RPC（P2-06）：三种格式落盘 + 内嵌字幕任务。

这一版是重写的。上一版没跑起来的原因就两条，写在这里防止再犯：
  1. 我以为有 ``subtitles.parse_srt`` / ``format_ass(lines=..., font=...)``——
     实际 ``format_srt`` 在 ``core/transcriber.py``，``format_ass`` 收 segments 列表；
  2. 我断言「软字幕走 FFmpeg.run、硬字幕走 Popen」——实际两条都在
     ``infra/ffmpeg.py`` 的 ``subtitle_filter`` 之后统一交给一个 ``video.export`` 任务，
     RPC 这层只看得到提交的 payload。
"""

from __future__ import annotations

from pathlib import Path

from video_workbench.core.subtitles import format_ass, normalize

SEGS = [
    {"start_ms": 0, "end_ms": 2000, "text": "第一句"},
    {"start_ms": 2500, "end_ms": 5000, "text": "第二句"},
    {"start_ms": 6000, "end_ms": 9000, "text": "第三句"},
]


def make_project(service, tmp_path: Path) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    items = normalize(SEGS)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="transcribed"
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=items,
        language="zh",
        model="tiny",
        srt_path=str(root / "transcript.srt"),
    )
    return project


def add_source(service, project: dict, tmp_path: Path) -> Path:
    src = tmp_path / "media" / "UP" / "标题" / "source.mp4"
    src.write_bytes(b"x" * 1024)  # 只为让「源文件在」成立，不真跑 ffmpeg
    service.repo.asset_upsert(project["id"], "source", str(src), size=1024)
    return src


def rpc(service, method, params=None):
    out = service.router.dispatch(method, params or {}, "test")
    assert out.get("ok") is True, out
    return out["data"]


def style_field(text: str, index: int) -> int:
    line = next(raw for raw in text.splitlines() if raw.startswith("Style:"))
    return int(line.split(",")[index])


# ------------------------------------------------------------------ 文本格式
def test_export_writes_srt_vtt_ass(service, tmp_path):
    project = make_project(service, tmp_path)
    data = rpc(
        service, "subtitle.export", {"project_id": project["id"], "formats": ["srt", "vtt", "ass"]}
    )
    by_fmt = {f["format"]: Path(f["path"]) for f in data["files"]}
    assert set(by_fmt) == {"srt", "vtt", "ass"}, sorted(by_fmt)
    assert by_fmt["srt"].read_text(encoding="utf-8").count("-->") == 3
    assert "WEBVTT" in by_fmt["vtt"].read_text(encoding="utf-8")
    assert by_fmt["ass"].read_text(encoding="utf-8").count("Dialogue:") == 3
    # 落在项目 export/ 下，文件名带语言标记（无译文时是 orig）
    assert by_fmt["srt"].parent.name == "export"
    assert by_fmt["srt"].name == "subtitle.orig.srt"


def test_mode_none_does_not_create_a_job(service, tmp_path):
    project = make_project(service, tmp_path)
    data = rpc(service, "subtitle.export", {"project_id": project["id"], "formats": ["srt"]})
    assert data["mode"] == "none" and data["job_id"] == ""


def test_ass_font_scales_with_height(service, tmp_path):
    """720 是基准：1080 的字号必须是 1.5 倍，否则大屏上字幕会小成一坨。"""
    assert style_field(format_ass(normalize(SEGS), height=720), 2) == 54
    assert style_field(format_ass(normalize(SEGS), height=1080), 2) == 81
    project = make_project(service, tmp_path)
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["ass"], "height": 1080},
    )
    text = Path(data["files"][0]["path"]).read_text(encoding="utf-8")
    # v2 的参考画布是 1080P，内置 default 主字号就是 72，1080 下缩放系数 1.0
    assert style_field(text, 2) == 72
    assert data["height"] == 1080


def test_export_always_produces_ass_for_embed(service, tmp_path):
    """软/硬字幕必须有 ASS 源；只勾 srt 时后端会补一份 ass，不让人对着黑屏猜。"""
    project = make_project(service, tmp_path)
    add_source(service, project, tmp_path)
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["srt"], "mode": "soft"},
    )
    assert {f["format"] for f in data["files"]} == {"srt", "ass"}, data["files"]


def test_export_without_transcript_is_conflict(service, tmp_path):
    project = service.repo.project_create(
        title="空", root_dir=str(tmp_path / "none"), stage="downloaded"
    )
    out = service.router.dispatch(
        "subtitle.export", {"project_id": project["id"], "formats": ["srt"]}, "test"
    )
    assert out["ok"] is False and out["error"]["code"] == "CONFLICT", out


def test_export_unknown_project_is_not_found(service):
    out = service.router.dispatch(
        "subtitle.export", {"project_id": "pj_不存在", "formats": ["srt"]}, "test"
    )
    assert out["ok"] is False and out["error"]["code"] == "NOT_FOUND", out


def test_export_needs_a_form(service):
    out = service.router.dispatch("subtitle.export", {"project_id": "pj_x", "formats": []}, "test")
    assert out["ok"] is False and out["error"]["code"] == "VALIDATION", out


# -------------------------------------------------------------------- 内嵌
def test_soft_subtitle_submits_mov_text_job(service, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        service.jobs, "submit", lambda *a, **k: seen.update(args=a) or {"id": "job_soft"}
    )
    project = make_project(service, tmp_path)
    src = add_source(service, project, tmp_path)
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["ass"], "mode": "soft"},
    )
    assert data["job"]["id"] == "job_soft"
    job_type, project_arg, payload = seen["args"][:3]
    assert job_type == "video.export" and project_arg == project["id"]
    assert payload["mode"] == "soft"
    assert Path(payload["subtitle"]).suffix == ".ass"
    assert payload["source"] == str(src)
    assert Path(payload["output"]).parent == src.parent / "export"


def test_hard_subtitle_submits_same_job_with_hard_mode(service, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        service.jobs, "submit", lambda *a, **k: seen.update(args=a) or {"id": "job_hard"}
    )
    project = make_project(service, tmp_path)
    add_source(service, project, tmp_path)
    rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["ass"], "mode": "hard"},
    )
    payload = seen["args"][2]
    assert payload["mode"] == "hard", payload
    # 硬字幕的滤镜选型归 subtitle_filter（.ass→ass=，其余→subtitles=），这层只交文件
    assert Path(payload["subtitle"]).is_file()


def test_embed_without_source_video_is_not_found(service, tmp_path):
    project = make_project(service, tmp_path)
    out = service.router.dispatch(
        "subtitle.export", {"project_id": project["id"], "formats": ["ass"], "mode": "soft"}, "test"
    )
    assert out["ok"] is False and out["error"]["code"] == "NOT_FOUND", out
    assert "源视频" in out["error"]["message"], out["error"]
