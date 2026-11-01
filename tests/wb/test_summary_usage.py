"""总结用量账：计数器、单次 usage、这条视频累计。

两小时视频打磨七八版，用户真正想知道的是「一共花了多少」，
所以累计必须按全部版本求和，而不是只看最后一版。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from video_workbench.app.service import (  # noqa: E402
    WorkbenchService,
    _usage_totals,
    _version_row,
)
from video_workbench.core.summarizer import _usage_of  # noqa: E402
from video_workbench.infra.llm import ChatClient  # noqa: E402


def _reply(prompt: int, completion: int) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }
    )


def test_chat_accumulates_usage_across_calls():
    replies = [_reply(100, 20), _reply(30, 7)]
    client = ChatClient(
        "http://x", "k", "m", transport=lambda *a: (200, replies.pop(0))
    )
    client.chat([{"role": "user", "content": "a"}])
    client.chat([{"role": "user", "content": "b"}])
    assert client.calls == 2
    assert client.prompt_tokens == 130
    assert client.completion_tokens == 27
    assert client.total_tokens == 157


def test_chat_counts_call_even_when_gateway_returns_no_usage():
    body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
    client = ChatClient("http://x", "k", "m", transport=lambda *a: (200, body))
    client.chat([{"role": "user", "content": "a"}])
    # 没有 usage 就不猜：调用次数照记，token 留 0。
    assert client.calls == 1
    assert client.prompt_tokens == 0
    assert client.total_tokens == 0


class _FakeLLM:
    def __init__(self, client, calls=3, started=None):
        self.client = client
        self.calls = calls
        self.started_at = started


class _FakeSummary:
    def __init__(self, chunks):
        self.chunks = chunks


def test_usage_of_carries_tokens_seconds_and_chunks():
    client = ChatClient("http://x", "k", "m")
    client.prompt_tokens = 240_000
    client.completion_tokens = 30_000
    client.total_tokens = 0  # 网关没给 total，要用输入+输出兜
    usage = _usage_of(_FakeLLM(client), _FakeSummary([1, 2, 3, 4]))
    assert usage["calls"] == 3
    assert usage["prompt_tokens"] == 240_000
    assert usage["completion_tokens"] == 30_000
    assert usage["total_tokens"] == 270_000
    assert usage["chunks"] == 4
    assert usage["seconds"] >= 0


def test_usage_totals_sums_all_versions():
    rows = [
        {"usage": {"calls": 9, "prompt_tokens": 120_000,
                   "completion_tokens": 18_000, "total_tokens": 138_000,
                   "seconds": 1089}},
        {"usage": {"calls": 12, "prompt_tokens": 121_242,
                   "completion_tokens": 16_879, "total_tokens": 138_121,
                   "seconds": 1092}},
    ]
    totals = _usage_totals(rows)
    assert totals["versions"] == 2
    assert totals["calls"] == 21
    assert totals["prompt_tokens"] == 241_242
    assert totals["completion_tokens"] == 34_879
    assert totals["total_tokens"] == 276_121
    assert totals["seconds"] == pytest.approx(2181.0)


def test_usage_totals_falls_back_to_sum_of_parts_and_tolerates_junk():
    rows = [
        {"usage": {"calls": 2, "prompt_tokens": 100, "completion_tokens": 40}},
        {"usage": {"calls": "3", "prompt_tokens": "bad"}},
        {"usage": None},
        {},
    ]
    totals = _usage_totals(rows)
    assert totals["versions"] == 4
    assert totals["calls"] == 5
    assert totals["prompt_tokens"] == 100
    assert totals["total_tokens"] == 140  # 只有第一条能凑出总量


def test_version_row_exposes_usage():
    row = {
        "id": "sum_x", "version": 3, "role": "standard", "model": "m",
        "chars": 10, "created_at": "2026-11-01T00:00:00Z", "status": "ready",
        "content": {"one_line": "一句话"},
        "usage": {"calls": 12, "prompt_tokens": 1},
    }
    out = _version_row(row)
    assert out["usage"]["calls"] == 12
    assert out["usage"]["prompt_tokens"] == 1


@pytest.fixture()
def service(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "storage:\n  db_path: " + str(tmp_path / "wb.db").replace("\\", "/") + "\n",
        encoding="utf-8",
    )
    svc = WorkbenchService(config_path=cfg)
    try:
        yield svc, tmp_path
    finally:
        svc.close()


def test_summary_get_returns_totals_for_whole_video(service):
    svc, tmp_path = service
    project = svc.repo.project_create(title="两小时的片子", root_dir=str(tmp_path / "m"))
    for calls, prompt, completion in ((9, 120_000, 18_000), (12, 121_242, 16_879)):
        svc.repo.summary_insert(
            project["id"],
            content={"one_line": "一句话"},
            content_md="## 摘要\n正文",
            role="standard",
            model="m",
            usage={"calls": calls, "prompt_tokens": prompt,
                   "completion_tokens": completion,
                   "total_tokens": prompt + completion, "seconds": 1092},
            status="ready",
        )
    res = svc.dispatch("summary.get", {"id": project["id"]})
    assert res["ok"] is True
    totals = res["data"]["totals"]
    assert totals["calls"] == 21
    assert totals["prompt_tokens"] == 241_242
    assert totals["total_tokens"] == 276_121
    assert res["data"]["summary"]["usage"]["calls"] == 12
    assert res["data"]["versions"][0]["usage"]["prompt_tokens"] == 121_242


def test_summary_get_without_versions_has_zero_totals(service):
    svc, tmp_path = service
    project = svc.repo.project_create(title="新片", root_dir=str(tmp_path / "m2"))
    res = svc.dispatch("summary.get", {"id": project["id"]})
    assert res["ok"] is True
    assert res["data"]["totals"]["calls"] == 0
    assert res["data"]["totals"]["total_tokens"] == 0
