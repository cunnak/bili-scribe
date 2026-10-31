"""P1-08 服务层：后端探测、后端选择透传、失败项换后端重试。

只关心"选哪个后端"和"有没有把选择传下去"——真下载在 test_download_bbdown.py 里
用注入的 runner 验过，这里不重复。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.app.errors import WBError


class SubmitRecorder:
    """替掉 jobs.submit，只记参数不真跑任务（真跑会去连 B 站）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict]] = []

    def __call__(self, job_type, project_id=None, payload=None, **kwargs):
        self.calls.append((str(job_type), project_id, dict(payload or {})))
        return {"id": f"job_{len(self.calls)}", "type": job_type, "status": "queued"}


@pytest.fixture()
def recorder(service, monkeypatch):
    rec = SubmitRecorder()
    monkeypatch.setattr(service.jobs, "submit", rec)
    return rec


def make_batch_job(service, *, bvid: str = "BV1xx411c7mD", backend: str = "") -> str:
    job_id = "job_batch_1"
    service.repo.job_create(
        job_id,
        "download.batch",
        None,
        {"quality": "1080P", "up_name": "某UP", "collection_title": "某合集", "backend": backend},
    )
    service.repo.job_update(
        job_id,
        status="failed",
        result={
            "total": 2,
            "ok": 1,
            "failed": 1,
            "items": [
                {"bvid": bvid, "title": "合集第 1 集", "page": 1, "status": "failed"},
                {"bvid": "BV1yy411c7mE", "title": "合集第 2 集", "page": 2, "status": "succeeded"},
            ],
        },
    )
    return job_id


def call(service, method: str, params: dict | None = None):
    out = service.router.dispatch(method, params or {})
    assert out["ok"] is True, out
    return out["data"]


def err(service, method: str, params: dict | None = None) -> dict:
    out = service.router.dispatch(method, params or {})
    assert out["ok"] is False, out
    return out["error"]


# --------------------------------------------------------------- 探测


def test_backends_reports_ytdlp_and_bbdown(service):
    data = call(service, "download.backends")
    assert data["default"] == "ytdlp"
    assert isinstance(data["ytdlp"]["found"], bool)
    assert data["bbdown"]["found"] is False
    assert "BBDown" in data["bbdown"]["hint"]


def test_backends_finds_configured_bbdown(service, tmp_path):
    binary = tmp_path / "BBDown.exe"
    binary.write_bytes(b"MZ")
    service.cfg.download.bbdown_path = str(binary)
    data = call(service, "download.backends")
    assert data["bbdown"]["found"] is True
    assert data["bbdown"]["name"] == "BBDown.exe"


def test_backends_follows_configured_default(service):
    service.cfg.download.backend = "bbdown"
    assert call(service, "download.backends")["default"] == "bbdown"


# --------------------------------------------------------------- 后端透传


def test_download_start_passes_default_backend(service, recorder):
    call(service, "download.start", {"url": "https://b23.tv/xx", "items": [{"bvid": "BV1"}], "backend": "bbdown"})
    assert recorder.calls[0][0] == "download.batch"
    assert recorder.calls[0][2]["backend"] == "bbdown"


def test_download_start_rejects_unknown_backend(service):
    error = err(service, "download.start", {"url": "u", "items": [{"bvid": "BV1"}], "backend": "aria2"})
    assert error["code"] == "VALIDATION"


def test_download_start_omits_backend_by_default(service, recorder):
    call(service, "download.start", {"url": "https://b23.tv/xx", "items": [{"bvid": "BV1"}]})
    assert recorder.calls[0][2]["backend"] == ""


# --------------------------------------------------------------- 失败项重试


def test_retry_failed_defaults_to_original_backend(service, recorder, tmp_path):
    job_id = make_batch_job(service, backend="")
    call(service, "download.retry_failed", {"id": job_id})
    payload = recorder.calls[0][2]
    assert [i["bvid"] for i in payload["items"]] == ["BV1xx411c7mD"]
    assert payload["backend"] == ""


def test_retry_failed_with_bbdown_sets_backend(service, recorder):
    job_id = make_batch_job(service)
    call(service, "download.retry_failed", {"id": job_id, "backend": "bbdown"})
    assert recorder.calls[0][2]["backend"] == "bbdown"


def test_retry_failed_keeps_parent_backend_when_not_given(service, recorder):
    job_id = make_batch_job(service, backend="bbdown")
    call(service, "download.retry_failed", {"id": job_id})
    assert recorder.calls[0][2]["backend"] == "bbdown"


def test_retry_failed_rejects_unknown_backend(service):
    job_id = make_batch_job(service)
    assert err(service, "download.retry_failed", {"id": job_id, "backend": "aria2"})["code"] == "VALIDATION"


def test_retry_failed_without_failures_is_conflict(service):
    job_id = "job_batch_ok"
    service.repo.job_create(job_id, "download.batch", None, {"quality": "1080P"})
    service.repo.job_update(job_id, status="succeeded", result={"total": 1, "items": [{"bvid": "BV1", "status": "succeeded"}]})
    assert err(service, "download.retry_failed", {"id": job_id})["code"] == "CONFLICT"


# --------------------------------------------------------------- worker 透传


def test_worker_forwards_backend_to_downloader(tmp_path):
    """worker 必须把 payload 里的后端一路传给 downloader，否则按钮就是摆设。"""
    from video_workbench.workers.download_worker import make_download_handler

    seen: list[dict] = []

    def fake_download(**kwargs):
        seen.append(dict(kwargs))
        target = Path(kwargs["home"]) / "source.mp4"
        target.write_bytes(b"v" * 200_000)
        from video_workbench.core.downloader import DownloadResult

        return DownloadResult(path=target, height=1080, format_id="", quality_requested="1080P", elapsed_s=0.1)

    handler = make_download_handler(
        repo=_Repo(),
        media_root=tmp_path,
        downloader=fake_download,
        backend="ytdlp",
    )

    class Ctx:
        job_id = "j1"
        job_type = "download.item"

        def log(self, *a, **k):
            return None

        def progress(self, *a, **k):
            return None

        def check_cancel(self):
            return None

        def should_cancel(self):
            return False

    handler(Ctx(), {"item": {"bvid": "BV1xx411c7mD", "title": "某视频", "page": 1}, "backend": "bbdown", "quality": "720P"})
    assert seen and seen[0]["backend"] == "bbdown"
    assert seen[0]["bbdown_bin"] == ""


class _Repo:
    """最小 repo：worker 会 upsert 项目/资产，这里只吞掉。"""

    def __init__(self) -> None:
        self.assets: list[tuple] = []

    def project_find_by_bvid(self, bvid, cid=None):
        return None

    def project_create(self, **kwargs):
        return {"id": "pj_test", **kwargs}

    def project_update(self, project_id, **kwargs):
        return None

    def asset_upsert(self, project_id, kind, path, **kwargs):
        self.assets.append((project_id, kind, path))
        return {"id": "as_test"}

    def asset_list(self, project_id=None, kind=None):
        return []


_ = WBError
