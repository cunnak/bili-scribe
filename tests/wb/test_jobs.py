"""任务状态机的验收（P0-05）。

任务系统是这个应用里唯一"异步、多线程、会失败重试"的东西，因此验收标准不是
"能跑完"，而是：状态一定落库、进度在 0..1、取消一定生效、重试一定是新的一条记录。
"""

from __future__ import annotations

import time

import pytest

from video_workbench.app.errors import WBError


def _wait(service, job_id: str, statuses: set[str], timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        row = service.dispatch("job.get", {"id": job_id})["data"]
        if row["status"] in statuses:
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"任务 {job_id} 在 {timeout}s 内没有进入 {statuses}，最后状态 {row.get('status')}"
    )


def test_demo_job_runs_to_success(service):
    job = service.dispatch("job.demo.submit", {"seconds": 0.5})["data"]
    assert job["status"] in ("queued", "running")
    assert job["progress"] == 0

    done = _wait(service, job["id"], {"succeeded", "failed", "canceled"})
    assert done["status"] == "succeeded", done
    assert done["progress"] == 1.0
    assert done["result"] == {"seconds": 0.5}
    assert done["started_at"] and done["finished_at"]

    listed = service.dispatch("job.list")["data"]
    assert job["id"] in [row["id"] for row in listed]


def test_cancel_stops_running_job(service):
    job = service.dispatch("job.demo.submit", {"seconds": 5})["data"]
    time.sleep(0.2)
    service.dispatch("job.cancel", {"id": job["id"]})

    done = _wait(service, job["id"], {"succeeded", "failed", "canceled"}, timeout=10)
    assert done["status"] == "canceled", done
    assert done["error_code"] == "CANCELED"


def test_cancel_completed_job_is_idempotent(service):
    job = service.dispatch("job.demo.submit", {"seconds": 0.5})["data"]
    _wait(service, job["id"], {"succeeded"})
    again = service.dispatch("job.cancel", {"id": job["id"]})
    assert again["ok"] is True
    assert again["data"]["status"] == "succeeded"


def test_retry_creates_new_record_with_attempt_count(service):
    job = service.dispatch("job.demo.submit", {"seconds": 0.5})["data"]
    _wait(service, job["id"], {"succeeded"})

    again = service.dispatch("job.retry", {"id": job["id"]})["data"]
    assert again["id"] != job["id"], "重试必须是新记录，覆盖旧记录会丢掉失败历史"
    assert again["attempts"] == 1
    assert again["payload"] == {"seconds": 0.5}
    _wait(service, again["id"], {"succeeded"})


def test_retry_while_running_is_conflict(service):
    job = service.dispatch("job.demo.submit", {"seconds": 5})["data"]
    time.sleep(0.2)
    try:
        resp = service.dispatch("job.retry", {"id": job["id"]})
        assert resp["ok"] is False
        assert resp["error"]["code"] == "CONFLICT"
    finally:
        service.dispatch("job.cancel", {"id": job["id"]})
        _wait(service, job["id"], {"canceled", "succeeded", "failed"}, timeout=10)


def test_unknown_job_type_is_unsupported(service):
    with pytest.raises(WBError) as info:
        service.jobs.submit("no.such.job")
    assert info.value.code == "UNSUPPORTED"


def test_cancel_unknown_job(service):
    resp = service.dispatch("job.cancel", {"id": "job_nope"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "NOT_FOUND"


def test_progress_and_log_events_are_emitted(service):
    """界面靠事件推进度条；只落库不发事件的话进度条会一直是 0。"""
    import json

    updates: list[dict] = []
    logs: list[dict] = []

    def sink(name: str, payload_json: str) -> None:
        payload = json.loads(payload_json)
        (updates if name == "job.updated" else logs).append(payload)

    service.events.set_sink(sink)
    job = service.dispatch("job.demo.submit", {"seconds": 1.0})["data"]
    _wait(service, job["id"], {"succeeded"})
    time.sleep(0.4)  # 节流线程 flush

    assert any(item.get("id") == job["id"] and item.get("progress") is not None for item in updates)
    # 日志按 200ms 攒批发出：一条事件里带 `lines` 数组（见 events.py）
    flat = [line for item in logs for line in (item.get("lines") or [])]
    assert any("[demo]" in str(line) for line in flat), f"没收到任务日志：{logs}"
