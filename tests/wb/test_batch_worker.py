"""批量下载测试（P1-07）。

这一层最容易出的问题不是"下不下来"，而是**一个坏的拖垮全部**、
**父任务取消了子任务还在跑**、以及**重试把已经成功的重下一遍**。
所以三个用例分别盯这三件事。
"""

from __future__ import annotations

import pytest

from video_workbench.app.errors import WBError
from video_workbench.workers import batch_worker


class Ctx:
    """最小 JobContext 替身：只记进度、只答"取消了吗"。"""

    job_id = "job_parent"

    def __init__(self, cancel_after: int | None = None):
        self.steps: list[tuple[float, str]] = []
        self.polls = 0
        self.cancel_after = cancel_after

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.steps.append((value if value is not None else -1, stage or ""))

    def log(self, line):
        pass

    def should_cancel(self):
        self.polls += 1
        return self.cancel_after is not None and self.polls > self.cancel_after

    def check_cancel(self):
        if self.should_cancel():
            raise WBError("CANCELED", "任务已取消")


class FakeManager:
    """按 script 顺序回报子任务状态：script[i] 是第 i 次 get 的返回值列表。"""

    def __init__(self, final: list[str], fail_titles: set[str] | None = None):
        self.submitted: list[dict] = []
        self.canceled: list[str] = []
        self.final = final
        self.fail_titles = fail_titles or set()
        self._polls = 0

    def submit(self, job_type, project_id=None, payload=None):
        self.submitted.append({"type": job_type, "payload": payload})
        index = len(self.submitted) - 1
        return {"id": f"job_{index}"}

    def get(self, job_id):
        self._polls += 1
        index = int(job_id.split("_")[1])
        total = len(self.final)
        # 第一轮全部 running，第二轮起按 final 定稿
        status = "running" if self._polls <= total else self.final[index]
        title = self.submitted[index]["payload"]["item"].get("title", "")
        return {
            "id": job_id,
            "status": "failed" if title in self.fail_titles and status != "running" else status,
            "progress": 0.5 if status == "running" else 1.0,
            "project_id": f"p{index}",
            "error_message": "大会员专享" if title in self.fail_titles else "",
        }

    def cancel(self, job_id):
        self.canceled.append(job_id)
        return {"id": job_id, "status": "canceled"}


def items(*titles):
    return [{"bvid": f"BV{t}", "title": t, "page": 1} for t in titles]


def run(manager, ctx=None, payload=None):
    handler = batch_worker.make_batch_handler(manager=manager, poll=0.001)
    return handler(ctx or Ctx(), payload or {"items": items("a", "b", "c")})


def test_partial_failure_keeps_parent_successful():
    manager = FakeManager(final=["succeeded", "failed", "succeeded"], fail_titles={"b"})
    result = run(manager)
    assert result["ok"] == 2 and result["failed"] == 1
    assert result["items"][1]["status"] == "failed"
    assert result["items"][1]["error"] == "大会员专享"


def test_all_failed_marks_parent_failed():
    manager = FakeManager(final=["failed", "failed", "failed"], fail_titles={"a", "b", "c"})
    with pytest.raises(WBError) as info:
        run(manager)
    assert info.value.code == "ALL_FAILED"
    assert info.value.detail["failed"] == 3


def test_all_children_are_submitted_up_front():
    """一次性全提交，并发由 semaphore 管；否则队列在界面上不可见。"""
    manager = FakeManager(final=["succeeded"] * 12)
    run(manager, payload={"items": items(*[f"p{i}" for i in range(12)]), "quality": "720P"})
    assert len(manager.submitted) == 12
    assert all(m["type"] == batch_worker.CHILD_TYPE for m in manager.submitted)
    assert manager.submitted[0]["payload"]["quality"] == "720P"


def test_cancel_propagates_to_children():
    manager = FakeManager(final=["succeeded"] * 3)
    ctx = Ctx(cancel_after=0)  # 第一次检查就取消
    with pytest.raises(WBError) as info:
        run(manager, ctx=ctx)
    assert info.value.code == "CANCELED"
    assert manager.canceled == [], "取消发生在派活之前，不该有子任务"


def test_cancel_after_dispatch_kills_children():
    manager = FakeManager(final=["succeeded"] * 3)
    ctx = Ctx(cancel_after=3)  # 派活后（每项 check_cancel 一次）再取消
    with pytest.raises(WBError) as info:
        run(manager, ctx=ctx)
    assert info.value.code == "CANCELED"
    assert len(manager.canceled) == 3, "父任务取消后子任务必须一起停"


def test_progress_reaches_one_and_reports_counts():
    manager = FakeManager(final=["succeeded", "succeeded"])
    ctx = Ctx()
    run(manager, ctx=ctx, payload={"items": items("a", "b")})
    assert ctx.steps[-1][0] == 1.0
    assert "2/2" in ctx.steps[-1][1]


def test_empty_items_is_validation_error():
    with pytest.raises(WBError) as info:
        run(FakeManager(final=[]), payload={"items": []})
    assert info.value.code == "VALIDATION"


def test_failed_items_extracts_only_failures():
    result = {
        "total": 2,
        "items": [
            {"bvid": "BV1", "title": "a", "page": 1, "status": "succeeded"},
            {"bvid": "BV2", "title": "b", "page": 3, "status": "failed"},
        ],
    }
    todo = batch_worker.failed_items(result)
    assert len(todo) == 1 and todo[0]["bvid"] == "BV2" and todo[0]["page"] == 3
