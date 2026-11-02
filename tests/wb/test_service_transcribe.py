"""转录相关 RPC 的装配测试（P2-01 的接口面）。

只验接线：参数放不放行、任务交给谁、模型状态怎么报。真转录在
``scripts/accept_p2_transcribe.py`` 里跑一次真模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.infra import models as models_mod


def err_code(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]["code"]


@pytest.fixture
def spied(service, monkeypatch):
    calls: list[tuple[str, str | None, dict]] = []

    def fake_submit(job_type, project_id=None, payload=None):
        calls.append((job_type, project_id, payload or {}))
        return {"id": f"job_{len(calls)}", "type": job_type, "status": "queued", "payload": payload}

    monkeypatch.setattr(service.jobs, "submit", fake_submit)
    return calls


def make_project(service, tmp_path: Path, *, stage: str = "downloaded", status: str = "ready") -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    (root / "source.mp4").write_bytes(b"fake")
    project = service.repo.project_create(title="标题", up_name="UP", root_dir=str(root),
                                          stage=stage, status=status)
    return project


def test_transcribe_start_creates_one_job_per_project(service, spied, tmp_path):
    first = make_project(service, tmp_path)
    second = make_project(service, tmp_path / "b")
    data = service.dispatch("transcribe.start", {"project_ids": [first["id"], second["id"]],
                                                 "model": "tiny"})["data"]
    assert data["count"] == 2
    assert [c[0] for c in spied] == ["transcribe", "transcribe"]
    assert spied[0][1] == first["id"], "job 要带 project_id，重试和取消都靠它"
    assert spied[0][2]["model"] == "tiny"
    assert spied[0][2]["project_id"] == first["id"]


def test_transcribe_start_rejects_empty(service, spied):
    assert err_code(service, "transcribe.start", {"project_ids": []}) == "VALIDATION"
    assert spied == []


def test_transcribe_start_skips_downloading_project(service, spied, tmp_path):
    project = make_project(service, tmp_path, status="downloading")
    assert err_code(service, "transcribe.start", {"project_ids": [project["id"]]}) == "CONFLICT"
    assert spied == []


def test_transcribe_start_skips_missing_project(service, spied):
    assert err_code(service, "transcribe.start", {"project_ids": ["pj_nope"]}) == "CONFLICT"


def test_transcribe_models_reports_six_with_current(service):
    data = service.dispatch("transcribe.models", {})["data"]
    names = [i["name"] for i in data["items"]]
    assert names == ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
    assert data["current"] == "large-v3-turbo"
    assert data["engine"] == "faster-whisper"
    assert all(i["downloaded"] is False for i in data["items"])


def test_model_download_skips_when_present(service, spied, tmp_path):
    root = Path(service.cfg.models_root)
    path = root / "faster-whisper" / "tiny"
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text("{}", encoding="utf-8")
    (path / "model.bin").write_bytes(b"0" * (21 * 1024 * 1024))
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")

    data = service.dispatch("transcribe.model.download", {"name": "tiny"})["data"]
    assert data["downloaded"] is True and data["job"] is None
    assert spied == [], "已下载就不该建任务"


def test_model_download_submits_job(service, spied):
    data = service.dispatch("transcribe.model.download", {"name": "small"})["data"]
    assert data["downloaded"] is False
    assert spied and spied[0][0] == "model.download"
    assert spied[0][2]["name"] == "small"


def test_model_download_rejects_unknown(service, spied):
    assert err_code(service, "transcribe.model.download", {"name": "whisper-9000"}) == "UNSUPPORTED"


def test_model_remove_requires_name(service):
    assert err_code(service, "transcribe.model.remove", {"name": ""}) == "VALIDATION"


def test_transcribe_cancel_delegates(service, monkeypatch):
    seen: list[str] = []

    def fake_cancel(job_id):
        seen.append(job_id)
        return {"id": job_id, "status": "canceled"}

    monkeypatch.setattr(service.jobs, "cancel", fake_cancel)
    data = service.dispatch("transcribe.cancel", {"id": "job_1"})["data"]
    assert data["status"] == "canceled" and seen == ["job_1"]


def test_library_get_exposes_transcript_meta(service, tmp_path):
    project = make_project(service, tmp_path)
    data = service.dispatch("library.get", {"id": project["id"]})["data"]
    assert data["project"]["transcript"] is None
    assert data["project"]["has_transcript"] is False

    service.repo.transcript_upsert(project["id"], segments=[{"id": "s1", "text": "hi"}],
                                   language="zh", model="tiny", srt_path="x.srt")
    data = service.dispatch("library.get", {"id": project["id"]})["data"]
    meta = data["project"]["transcript"]
    assert meta["segments"] == 1 and meta["language"] == "zh"
    assert "segments_json" not in meta, "列表接口不该把整段字幕吐给前端"


def test_app_info_reports_faster_whisper(service):
    data = service.dispatch("app.info", {})["data"]
    assert "faster_whisper" in data["deps"]
    assert models_mod.DEFAULT_MODEL == "large-v3-turbo"


# ----------------------------------------------------------------- 源语言


def test_transcribe_start_rejects_unknown_language(service, spied, tmp_path):
    project = make_project(service, tmp_path)
    code = err_code(service, "transcribe.start",
                    {"project_ids": [project["id"]], "language": "zh-CN"})
    assert code == "VALIDATION"
    assert spied == [], "校验必须发生在提交任务之前，否则队列里会多一条注定失败的 job"


def test_transcribe_start_normalizes_language_into_payload(service, spied, tmp_path):
    project = make_project(service, tmp_path)
    service.dispatch("transcribe.start", {"project_ids": [project["id"]], "language": " ZH "})
    assert spied[0][2]["language"] == "zh", "大小写和空格要在入口吃掉，下游只认裸码"


def test_transcribe_start_keeps_auto_empty(service, spied, tmp_path):
    project = make_project(service, tmp_path)
    service.dispatch("transcribe.start", {"project_ids": [project["id"]], "language": "auto"})
    assert spied[0][2]["language"] == "", "auto 归一成空串，worker 靠它切换成 None 去探测"


def test_transcribe_languages_lists_common_and_all(service):
    from video_workbench.infra import languages

    data = service.dispatch("transcribe.languages", {})["data"]
    assert data["common"][0]["code"] == ""
    assert len(data["common"]) == len(languages.COMMON)
    assert data["count"] == len(languages.CODES)
    assert len(data["all"]) > 50, "长尾要给全，用户在弹窗里可能就要那一个"
