"""P4-06 单测：发布记录查询、错误码文案、限流余量。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from video_workbench.core import publisher as core


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def make_project(repo, *, bvid: str = "BV1rec0000001") -> str:
    project = repo.project_create(
        title="记录样例",
        root_dir="C:/tmp/rec",
        bvid=bvid,
        aid=1234,
        up_name="示例UP",
    )
    return project["id"]


def seed(repo, project_id: str, *, status: str, title: str = "标题") -> str:
    row = repo.publish_note_upsert(
        project_id,
        bvid="BV1rec0000001",
        title=title,
        summary="摘要",
        content='[{"insert":"正文"}]',
    )
    note_pk = row["id"]
    if status == "published":
        repo.publish_set_status(note_pk, "published", note_id="note_1")
    elif status == "withdrawn":
        repo.publish_set_status(note_pk, "published", note_id="note_2")
        repo.publish_set_status(note_pk, "withdrawn")
    elif status == "failed":
        repo.publish_set_status(note_pk, "failed", error="[12015] 验证码")
    return note_pk


# ------------------------------------------------------------------ 记录查询


def test_records_include_published_withdrawn_failed_but_not_drafts(service):
    repo = service.repo
    project_id = make_project(repo)
    seed(repo, project_id, status="published", title="发过的")
    seed(repo, project_id, status="withdrawn", title="撤过的")
    seed(repo, project_id, status="failed", title="失败了")
    seed(repo, project_id, status="draft", title="还是草稿")

    rows = repo.publish_records()
    titles = [row["title"] for row in rows]
    assert "还是草稿" not in titles, "草稿不该混进发布记录"
    assert set(titles) == {"发过的", "撤过的", "失败了"}


def test_records_join_project_title_and_up(service):
    repo = service.repo
    project_id = make_project(repo)
    seed(repo, project_id, status="published")
    row = repo.publish_records()[0]
    assert row["project_title"] == "记录样例"
    assert row["up_name"] == "示例UP"


def test_records_order_by_last_action(service):
    repo = service.repo
    """撤回过的要按撤回时间排，否则上周发、今天撤的那条会沉下去。"""
    project_id = make_project(repo)
    old = seed(repo, project_id, status="published", title="上周发的")
    repo._exec("UPDATE wb_publish_note SET published_at=? WHERE id=?", (iso(60 * 24), old))
    fresh = seed(repo, project_id, status="withdrawn", title="刚撤的")
    repo._exec(
        "UPDATE wb_publish_note SET published_at=?, withdrawn_at=? WHERE id=?",
        (iso(60 * 24 * 7), iso(1), fresh),
    )
    rows = repo.publish_records()
    assert rows[0]["title"] == "刚撤的"


def test_records_respect_limit(service):
    repo = service.repo
    project_id = make_project(repo)
    for index in range(5):
        seed(repo, project_id, status="published", title=f"第 {index} 条")
    assert len(repo.publish_records(limit=2)) == 2


# ---------------------------------------------------------------- 记录视图


def test_record_view_drops_body_and_adds_url():
    row = {
        "id": "n1",
        "bvid": "BV1abc",
        "title": "标题",
        "status": "published",
        "content": '[{"insert":"很长很长的正文"}]',
        "published_at": iso(),
    }
    view = core.record_view(row)
    assert "content" not in view, "列表不需要正文，别把整篇文本塞进列表"
    assert view["url"] == "https://www.bilibili.com/video/BV1abc"
    assert view["status_text"] == "已发布"


def test_record_view_without_bvid_has_no_url():
    view = core.record_view({"id": "n1", "title": "t", "status": "failed"})
    assert view["url"] == "", "没有 BV 号就别给半截链接"
    assert view["status_text"] == "发布失败"


def test_record_view_none_is_none():
    assert core.record_view(None) is None


# ---------------------------------------------------------------- 错误码文案


def test_duplicate_note_code_says_go_delete_old():
    exc = type("E", (Exception,), {"code": 79508, "message": "该视频已存在你创建的笔记"})()
    text = core.note_error_text(exc)
    assert "79508" in text, "原始码要留着，用户要拿去搜索"
    assert "删掉旧的那条" in text
    assert "不会覆盖" in text, "必须说清不会自动覆盖"


def test_note_unsupported_code_mentions_comment():
    exc = type("E", (Exception,), {"code": 79513, "message": "该视频不支持笔记"})()
    text = core.note_error_text(exc)
    assert "不支持笔记" in text
    assert "评论" in text


def test_auth_codes_hint_login():
    for code in (-111, -101):
        exc = type("E", (Exception,), {"code": code, "message": "csrf 校验失败"})()
        text = core.note_error_text(exc)
        assert "登录" in text, f"{code} 应该提示重新登录：{text}"


# ---------------------------------------------------------------- 限流余量


def _draft(**over):
    base = {
        "title": "标题",
        "summary": "摘要",
        "content_text": "正文",  # 门禁读的是纯文本字段，不是 Delta 的 content
        "bvid": "BV1x",
    }
    base.update(over)
    return base


def test_wait_seconds_reported_when_rate_limited():
    last = datetime.now(timezone.utc) - timedelta(seconds=20)
    report = core.gate_report(
        _draft(),
        project={"bvid": "BV1x"},
        note={"bvid": "BV1x"},
        published_note=None,
        last_publish_at=last,
        confirmed=True,
        logged_in=True,
    )
    assert report["ready"] is False
    assert 35 <= report["wait_seconds"] <= 40, report["wait_seconds"]
    assert report["blockers"] == ["pace"]
    pace = [g for g in report["gates"] if g["id"] == "pace"][0]
    assert "还要等" in pace["reason"]


def test_wait_seconds_zero_when_not_limited():
    report = core.gate_report(
        _draft(),
        project={"bvid": "BV1x"},
        note={"bvid": "BV1x"},
        published_note=None,
        last_publish_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        confirmed=True,
        logged_in=True,
    )
    assert report["ready"] is True
    assert report["wait_seconds"] == 0
    assert report["blockers"] == []


def test_blockers_list_every_blocked_gate_in_order():
    report = core.gate_report(
        _draft(content_text="   "),
        project={"bvid": ""},
        note={},
        published_note=None,
        last_publish_at=None,
        confirmed=False,
        logged_in=False,
    )
    assert report["blockers"] == ["content", "account", "pace"]


# ---------------------------------------------------------------- 服务层


def test_publish_records_rpc_counts_and_shapes(service):
    project = service.repo.project_create(
        title="记录 RPC", root_dir="C:/tmp/rec-rpc", bvid="BV1rpc000001", aid=99
    )
    service.repo.summary_insert(
        project["id"],
        content={
            "one_line": "一句话",
            "summary_md": "## 小节\n正文",
            "key_points": ["要点"],
            "timeline": [],
            "caution": "",
            "comment_short": "短评",
        },
        content_md="正文",
    )
    saved = service.router.dispatch("publish.note", {"project_id": project["id"]}, "t")
    assert saved["ok"] is True, saved
    note_id = saved["data"]["note"]["id"]
    service.repo.publish_set_status(note_id, "published", note_id="note_x")

    out = service.router.dispatch("publish.records", {"limit": 10}, "t")["data"]
    assert out["total"] == 1
    assert out["counts"]["published"] == 1
    row = out["records"][0]
    assert row["status_text"] == "已发布"
    assert row["note_id"] == "note_x"
    assert "content" not in row
    assert row["url"].endswith("BV1rpc000001")


def test_publish_records_rpc_clamps_limit(service):
    out = service.router.dispatch("publish.records", {"limit": 9999}, "t")
    assert out["ok"] is True, "limit 超上限要夹住，不能报错"


def test_open_url_rejects_non_http(service):
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "C:/x.txt", ""):
        res = service.router.dispatch("app.open_url", {"url": bad}, "t")
        assert res["ok"] is False, bad
        assert res["error"]["code"] == "VALIDATION", bad


def test_open_url_accepts_https_without_opening(service, monkeypatch):
    opened: list[str] = []
    import os as os_mod

    monkeypatch.setattr(os_mod, "startfile", opened.append, raising=False)
    res = service.router.dispatch(
        "app.open_url", {"url": "https://www.bilibili.com/video/BV1x"}, "t"
    )
    assert res["ok"] is True
    assert opened and opened[0].startswith("https://")
