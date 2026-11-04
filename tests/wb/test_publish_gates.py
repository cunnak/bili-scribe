"""P4-04：发布中心草稿 —— 门禁状态、重新生成、列表字段。

这一层验的是"能不能把话说清楚"：发布按钮什么时候该灰、灰的理由是什么、
用户改完标题再点「重新生成」会不会把他改的东西打回原形。

真发布（含 79508/79513/12015 的错误码映射）在 P4-05，本文件不碰网络。
"""

from __future__ import annotations

import json

import pytest

from video_workbench.core import publisher as publisher_core


# ---------------------------------------------------------------- 纯函数

def _draft(**over):
    base = {
        "title": "一个标题",
        "summary": "一句话摘要",
        "content_text": "正文正文正文",
        "title_chars": 4,
        "summary_chars": 5,
        "chars": 6,
    }
    base.update(over)
    return base


def _by_id(report):
    return {gate["id"]: gate for gate in report["gates"]}


def test_gate_report_has_three_named_gates():
    report = publisher_core.gate_report(_draft(), project={"bvid": "BV1xx"})
    assert [gate["id"] for gate in report["gates"]] == ["content", "account", "pace"]
    assert [gate["label"] for gate in report["gates"]] == ["内容确认", "账号与视频", "频率与重复"]


def test_content_gate_is_todo_until_confirmed():
    report = publisher_core.gate_report(_draft(), project={"bvid": "BV1xx"})
    assert _by_id(report)["content"]["status"] == "todo"
    assert "确认" in _by_id(report)["content"]["reason"]


def test_content_gate_blocks_on_empty_body_even_if_confirmed():
    report = publisher_core.gate_report(
        _draft(content_text="   "), project={"bvid": "BV1xx"}, confirmed=True
    )
    assert _by_id(report)["content"]["status"] == "blocked"
    assert "正文" in _by_id(report)["content"]["reason"]


def test_content_gate_blocks_when_title_over_limit():
    long_title = "标" * (publisher_core.TITLE_MAX + 3)
    report = publisher_core.gate_report(
        _draft(title=long_title), project={"bvid": "BV1xx"}, confirmed=True
    )
    gate = _by_id(report)["content"]
    assert gate["status"] == "blocked"
    assert "80" in gate["reason"]


def test_account_gate_blocks_without_bvid():
    report = publisher_core.gate_report(_draft(), project={})
    gate = _by_id(report)["account"]
    assert gate["status"] == "blocked"
    assert "BV" in gate["reason"]


def test_account_gate_blocks_when_note_bvid_differs():
    report = publisher_core.gate_report(
        _draft(), project={"bvid": "BV1aa"}, note={"bvid": "BV1bb"}
    )
    assert _by_id(report)["account"]["status"] == "blocked"


def test_account_gate_is_todo_when_login_unknown_and_ok_when_logged_in():
    unknown = _by_id(publisher_core.gate_report(_draft(), project={"bvid": "BV1aa"}))["account"]
    assert unknown["status"] == "todo"
    logged = _by_id(
        publisher_core.gate_report(_draft(), project={"bvid": "BV1aa"}, logged_in=True)
    )["account"]
    assert logged["status"] == "ok"


def test_account_gate_blocks_when_logged_out():
    gate = _by_id(publisher_core.gate_report(_draft(), project={"bvid": "BV1aa"}, logged_in=False))["account"]
    assert gate["status"] == "blocked"


def test_pace_gate_blocks_duplicate_note():
    report = publisher_core.gate_report(
        _draft(),
        project={"bvid": "BV1aa"},
        published_note={"status": "published", "note_id": "note_1"},
    )
    gate = _by_id(report)["pace"]
    assert gate["status"] == "blocked"
    assert "已经发过" in gate["reason"]
    assert any("note_1" in item["text"] for item in gate["items"])


def test_pace_gate_blocks_inside_interval_and_reports_remaining_seconds():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 30, 12, 0, 30, tzinfo=timezone.utc)
    report = publisher_core.gate_report(
        _draft(),
        project={"bvid": "BV1aa"},
        last_publish_at="2026-10-30T12:00:00+00:00",
        min_interval_seconds=60,
        now=now,
    )
    gate = _by_id(report)["pace"]
    assert gate["status"] == "blocked"
    assert "30 秒" in gate["reason"]


def test_pace_gate_ok_after_interval():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 30, 12, 5, 0, tzinfo=timezone.utc)
    report = publisher_core.gate_report(
        _draft(),
        project={"bvid": "BV1aa"},
        last_publish_at="2026-10-30T12:00:00+00:00",
        min_interval_seconds=60,
        now=now,
    )
    assert _by_id(report)["pace"]["status"] == "ok"


def test_ready_requires_every_gate_ok():
    report = publisher_core.gate_report(
        _draft(), project={"bvid": "BV1aa"}, confirmed=True, logged_in=True
    )
    assert report["ready"] is True
    partial = publisher_core.gate_report(_draft(), project={"bvid": "BV1aa"}, confirmed=True)
    assert partial["ready"] is False


# ---------------------------------------------------------------- 服务层

def _make_project(service, *, bvid="BV1gate", title="门禁样例"):
    from pathlib import Path

    root = Path(service.cfg.storage.media_root) / "门禁样例"
    root.mkdir(parents=True, exist_ok=True)
    return service.repo.project_create(
        title=title,
        bvid=bvid,
        up_name="测试UP",
        root_dir=str(root),
        stage="summarized",
        status="ready",
    )


def _seed_summary(service, project_id):
    service.repo.summary_insert(
        project_id,
        content={
            "one_line": "一句话",
            "summary_md": "正文第一段。",
            "key_points": ["要点一"],
            "timeline": [],
            "caution": "",
            "comment_short": "短评",
        },
        content_md="# 标题\n\n正文第一段。",
        # 保留 standard：直接写库/历史行的字面量，是「退役前数据仍可读」的证据（分类标准见 test_summary.py 文件头）
        role="standard",
        model="stub",
    )


def _call(service, method, params):
    return service.router.dispatch(method, params, "req")


def _data(service, method, params):
    payload = _call(service, method, params)
    assert payload.get("ok") is True, payload
    return payload["data"]


def _error(service, method, params):
    payload = _call(service, method, params)
    assert payload.get("ok") is False, payload
    return payload["error"]


def test_publish_get_returns_gates(service):
    project = _make_project(service)
    _seed_summary(service, project["id"])
    draft = _data(service, "publish.preview", {"project_id": project["id"]})
    saved = _data(
        service,
        "publish.note",
        {
            "project_id": project["id"],
            "title": draft["title"],
            "summary": draft["summary"],
        },
    )
    detail = _data(service, "publish.get", {"id": saved["note"]["id"]})
    assert [gate["id"] for gate in detail["gates"]["gates"]] == ["content", "account", "pace"]
    assert detail["gates"]["ready"] is False
    assert detail["note"]["title_chars"] > 0


def test_publish_list_carries_up_name_and_updated_at(service):
    project = _make_project(service)
    _seed_summary(service, project["id"])
    _data(
        service,
        "publish.note",
        {"project_id": project["id"], "title": "标题", "summary": "摘要"},
    )
    rows = _data(service, "publish.list", {})["notes"]
    assert rows and rows[0]["up_name"] == "测试UP"
    assert rows[0]["updated_at"], "列表要显示更新时间，updated_at 不能是空的"


def test_regenerate_summary_keeps_edited_title(service):
    project = _make_project(service)
    _seed_summary(service, project["id"])
    view = _data(service, "publish.preview", {"project_id": project["id"]})
    saved = _data(
        service,
        "publish.note",
        {"project_id": project["id"], "title": "我改过的标题", "summary": "我改过的摘要"},
    )
    note_id = saved["note"]["id"]
    again = _data(service, "publish.regenerate", {"id": note_id, "part": "summary"})
    assert again["draft"]["title"] == "我改过的标题", "只换摘要时标题不该被打回原样"
    assert again["draft"]["summary"] == view["summary"]
    assert again["draft"]["content_text"], "正文要保留"


def test_regenerate_all_takes_fresh_title_and_summary(service):
    project = _make_project(service)
    _seed_summary(service, project["id"])
    view = _data(service, "publish.preview", {"project_id": project["id"]})
    saved = _data(
        service,
        "publish.note",
        {"project_id": project["id"], "title": "旧标题", "summary": "旧摘要"},
    )
    again = _data(service, "publish.regenerate", {"id": saved["note"]["id"], "part": "all"})
    assert again["draft"]["title"] == view["title"]
    assert again["draft"]["summary"] == view["summary"]


def test_regenerate_on_missing_draft_is_not_found(service):
    error = _error(service, "publish.regenerate", {"id": "draft_nope"})
    assert error["code"] == "NOT_FOUND"


def test_gates_block_duplicate_after_publish_row(service):
    """同一 bvid 已有一份 published 记录时，新草稿的第三关要红。"""
    project = _make_project(service, bvid="BV1dup")
    _seed_summary(service, project["id"])
    first = _data(
        service,
        "publish.note",
        {"project_id": project["id"], "title": "标题", "summary": "摘要"},
    )
    # 直接改状态模拟"已经发出去了"，P4-05 会用真接口
    service.repo._exec(
        "UPDATE wb_publish_note SET status='published', note_id='note_x',"
        " published_at='2026-01-01T00:00:00Z' WHERE id=?",
        (first["note"]["id"],),
    )
    second = _data(
        service,
        "publish.note",
        {"project_id": project["id"], "title": "标题二", "summary": "摘要二"},
    )
    detail = _data(service, "publish.get", {"id": second["note"]["id"]})
    pace = [gate for gate in detail["gates"]["gates"] if gate["id"] == "pace"][0]
    assert pace["status"] == "blocked"
    assert "已经发过" in pace["reason"]


def test_schema_has_updated_at_column(service):
    rows = service.repo.conn.execute("PRAGMA table_info(wb_publish_note)").fetchall()
    names = [row[1] for row in rows]
    assert "updated_at" in names
