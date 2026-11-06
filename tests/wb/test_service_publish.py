"""发布草稿的服务层测试（P2-09）。

盯着三件界面上会直接暴露的事：

1. **没总结就没草稿**——报 CONFLICT 说清楚，而不是存一份空正文；
2. **改过的标题/正文不会被重新生成打回**——这是"编辑"这个动作的全部意义；
3. **同一项目只有一份草稿**——重复保存是更新，不是堆积一堆看起来一样的草稿。
"""

from __future__ import annotations

from pathlib import Path

SUMMARY_CONTENT = {
    "one_line": "这期把注意力机制拆成能照做的步骤",
    "summary_md": "## 这期在讲什么\n讲三件事。",
    "key_points": ["先证伪直觉", "把变量拆开"],
    "timeline": [{"t": "00:12", "text": "抛出反例"}],
    "caution": "数字建议回视频核对",
    "comment_short": "一句话：把注意力拆成能照做的三步。",
    "model": "deepseek-chat",
    # 保留 standard：直接写库/历史行的字面量，是「退役前数据仍可读」的证据（分类标准见 test_summary.py 文件头）
    "role": "standard",
}


def make_project(service, tmp_path: Path, *, title="标题", bvid="BV1xx", aid=1234):
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    return service.repo.project_create(
        title=title, up_name="UP", root_dir=str(root), bvid=bvid, aid=aid, stage="summarized"
    )


def with_summary(service, project, content=None):
    return service.repo.summary_insert(
        # 保留 standard：直接写库/历史行的字面量，是「退役前数据仍可读」的证据（分类标准见 test_summary.py 文件头）
        project["id"],
        content=content or SUMMARY_CONTENT,
        content_md="# 总结",
        role="standard",
    )


def call(service, method, params=None):
    return service.router.dispatch(method, params or {}, "req-1")


# ------------------------------------------------------------------ preview


def test_preview_without_summary_is_conflict(service, tmp_path):
    project = make_project(service, tmp_path)
    result = call(service, "publish.preview", {"project_id": project["id"]})
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFLICT"
    assert "还没有总结" in result["error"]["message"]


def test_preview_builds_body_from_latest_summary(service, tmp_path):
    project = make_project(service, tmp_path)
    record = with_summary(service, project)
    data = call(service, "publish.preview", {"project_id": project["id"]})["data"]
    assert data["title"] == "标题"
    assert data["summary"] == SUMMARY_CONTENT["comment_short"]
    assert "## 要点" in data["markdown"] and "时间轴" in data["markdown"]
    assert data["chars"] > 20
    # 摘要上限 2026-11-02 从 200 上调到 400（config.publish.max_summary_chars 默认值）
    assert data["limits"] == {"title": 80, "summary": 400}
    assert data["warnings"] == []
    assert data["summary_id"] == record["id"] and data["version"] == 1
    # 还没存过草稿
    assert data["note"] is None


def test_preview_rejects_summary_from_other_project(service, tmp_path):
    project = make_project(service, tmp_path)
    other = make_project(service, tmp_path, title="别的")
    record = with_summary(service, other)
    result = call(
        service, "publish.preview", {"project_id": project["id"], "summary_id": record["id"]}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION"


def test_preview_shows_existing_draft(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)
    call(service, "publish.note", {"project_id": project["id"], "title": "存过的标题"})
    data = call(service, "publish.preview", {"project_id": project["id"]})["data"]
    assert data["note"]["title"] == "存过的标题"
    assert data["note"]["status"] == "draft"


# ------------------------------------------------------------------ 保存


def test_note_is_saved_as_draft(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)
    data = call(service, "publish.note", {"project_id": project["id"]})["data"]
    note = data["note"]
    assert note["status"] == "draft"
    assert note["bvid"] == "BV1xx" and note["aid"] == 1234
    assert note["published_at"] is None and note["withdrawn_at"] is None
    assert note["chars"] > 20
    # 库里真的是这一行
    row = service.repo.publish_for_project(project["id"])
    assert row["id"] == note["id"]


def test_edited_title_and_summary_are_kept(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)
    data = call(
        service,
        "publish.note",
        {
            "project_id": project["id"],
            "title": "我自己改的标题",
            "summary": "我自己改的摘要",
        },
    )["data"]
    assert data["note"]["title"] == "我自己改的标题"
    assert data["note"]["summary"] == "我自己改的摘要"
    # 再预览一次，草稿仍在（不会被重新生成打回原位）
    again = call(service, "publish.preview", {"project_id": project["id"]})["data"]
    assert again["note"]["title"] == "我自己改的标题"


def test_edited_delta_wins_over_regenerated_markdown(service, tmp_path):
    """用户手改过正文时，正文以他的为准。"""
    project = make_project(service, tmp_path)
    with_summary(service, project)
    custom = '[{"insert": "我自己写的正文\\n"}]'
    data = call(service, "publish.note", {"project_id": project["id"], "content": custom})["data"]
    assert data["note"]["content"] == custom
    assert data["note"]["content_text"] == "我自己写的正文\n"


def test_second_save_updates_same_row(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)
    first = call(service, "publish.note", {"project_id": project["id"], "title": "第一次"})["data"]
    second = call(service, "publish.note", {"project_id": project["id"], "title": "第二次"})["data"]
    assert first["note"]["id"] == second["note"]["id"]
    rows = call(service, "publish.list", {"project_id": project["id"]})["data"]["notes"]
    assert len(rows) == 1 and rows[0]["title"] == "第二次"


def test_over_limit_title_is_refused_not_truncated(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)
    result = call(service, "publish.note", {"project_id": project["id"], "title": "标" * 100})
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION"
    assert "标题超过 80 字" in result["error"]["message"]
    assert service.repo.publish_for_project(project["id"]) is None


def test_empty_body_is_refused(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project, content={"one_line": "只有一句话"})
    result = call(service, "publish.note", {"project_id": project["id"], "content": "[]"})
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION"


def test_note_on_missing_project(service, tmp_path):
    result = call(service, "publish.note", {"project_id": "pj_nope"})
    assert result["ok"] is False and result["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------------ 列表与删除


def test_list_carries_project_title_and_counts(service, tmp_path):
    project = make_project(service, tmp_path, title="被讲的视频")
    with_summary(service, project)
    call(service, "publish.note", {"project_id": project["id"]})
    notes = call(service, "publish.list", {})["data"]["notes"]
    assert len(notes) == 1
    assert notes[0]["project_title"] == "被讲的视频"
    assert notes[0]["up_name"] == "UP"
    assert notes[0]["chars"] > 20


def test_get_and_delete(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)
    note = call(service, "publish.note", {"project_id": project["id"]})["data"]["note"]
    got = call(service, "publish.get", {"id": note["id"]})["data"]["note"]
    assert got["id"] == note["id"] and got["project_title"] == "标题"

    call(service, "publish.delete", {"id": note["id"]})
    again = call(service, "publish.get", {"id": note["id"]})
    assert again["ok"] is False and again["error"]["code"] == "NOT_FOUND"
    twice = call(service, "publish.delete", {"id": note["id"]})
    assert twice["ok"] is False and twice["error"]["code"] == "NOT_FOUND"


def test_withdraw_rejects_unknown_and_unpublished(service, tmp_path):
    """P4-05 之后撤回是真的了，但对「不存在」和「还没发」的草稿必须说实话。"""
    missing = call(service, "publish.withdraw", {"id": "draft_x"})
    assert missing["ok"] is False and missing["error"]["code"] == "NOT_FOUND"

    project = make_project(service, tmp_path)
    with_summary(service, project)
    note = call(service, "publish.note", {"project_id": project["id"]})["data"]["note"]
    draft = call(service, "publish.withdraw", {"id": note["id"]})
    assert draft["ok"] is False
    assert draft["error"]["code"] == "CONFLICT"
    assert "已发布" in draft["error"]["message"]


def test_publish_event_fires(service, tmp_path):
    seen = []
    service.events.set_sink(lambda name, payload: seen.append((name, payload)))
    project = make_project(service, tmp_path)
    with_summary(service, project)
    call(service, "publish.note", {"project_id": project["id"]})
    assert any(name == "publish.updated" for name, _ in seen)


# ------------------------------------------------------------------ 并发与越界


def test_note_cannot_overwrite_draft_of_another_project(service, tmp_path):
    """note_pk 指向别的项目的草稿时，拒绝覆盖（D1）。

    草稿是按行 id 更新的。id 若属于另一个视频，一次「保存」会把那个项目的
    标题/摘要/正文整份改掉——用户看到的是"我明明在这个视频下点的保存"。
    """
    first = make_project(service, tmp_path)
    with_summary(service, first)
    saved = call(service, "publish.note", {"project_id": first["id"]})["data"]
    draft_id = saved["note"]["id"]

    second = make_project(service, tmp_path, title="另一个视频", bvid="BV1yy", aid=5678)
    with_summary(service, second)

    result = call(service, "publish.note", {"project_id": second["id"], "note_pk": draft_id})
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFLICT"
    assert "另一个视频" in result["error"]["message"]

    # 原来那份草稿一个字都没被动过
    still = call(service, "publish.get", {"id": draft_id})["data"]["note"]
    assert still["project_id"] == first["id"]


def test_publish_rejects_second_call_while_one_is_in_flight(service, tmp_path):
    """发布是 check-then-act，第二个请求必须拿不到锁（D2）。

    pywebview 每个 js_api 调用一个线程；两次点击会各自读到 status=draft、
    各自 POST 一次，平台侧就是两条重复笔记。
    """
    project = make_project(service, tmp_path)
    with_summary(service, project)
    saved = call(service, "publish.note", {"project_id": project["id"]})["data"]
    draft_id = saved["note"]["id"]

    assert service._publish_lock.acquire(blocking=False) is True
    try:
        result = call(service, "publish.publish", {"id": draft_id, "confirmed": True})
    finally:
        service._publish_lock.release()

    assert result["ok"] is False
    assert result["error"]["code"] == "CONFLICT"
    assert "还没返回" in result["error"]["message"]
