"""本地导入项目的发布视频号绑定（BV / AV）。

发布链路原来只认 wb_project.bvid：本地项目这一列永远是空的，所以「账号与视频」
第二关永远 blocked。这里用真 SQLite + 真 Repo 验：手填 BV/AV 落到 publish_*
两列、不污染下载身份列、只填一个也能工作、错的输入不写库。
"""

from __future__ import annotations

from pathlib import Path

SUMMARY_CONTENT = {
    "one_line": "这一期讲清楚本地视频怎么发布",
    "summary_md": "## 正文\n本地视频也能发笔记。",
    "key_points": ["先填视频号", "再保存草稿"],
    "timeline": [{"t": "00:10", "text": "开场"}],
    "model": "deepseek-chat",
    "role": "standard",
}


class FakeItem:
    def __init__(self, bvid: str, aid: int):
        self.bvid = bvid
        self.aid = aid


class FakeResolution:
    def __init__(self, bvid: str, aid: int):
        self.items = [FakeItem(bvid, aid)]


class FakeResolver:
    """足够覆盖 BV/AV 两条解析路径；不联网。"""

    def __init__(self, *, bvid: str = "BV1av411c7mD", aid: int = 778899):
        self.bvid = bvid
        self.aid = aid
        self.calls: list[str] = []

    def resolve(self, raw: str):
        text = str(raw or "").strip()
        self.calls.append(text)
        if text == f"av{self.aid}" or text == self.bvid:
            return FakeResolution(self.bvid, self.aid)
        if text == "BV1xx411c7mD":
            return FakeResolution(text, 778899)
        raise AssertionError(f"unexpected resolver input: {text!r}")


class FakeGateway:
    def is_logged_in(self):
        return True

    def client(self):
        return None


def make_project(service, tmp_path: Path, *, bvid=None, aid=None, source_kind="local"):
    root = tmp_path / "media" / "本地素材"
    root.mkdir(parents=True, exist_ok=True)
    (root / "transcript.json").write_text("[]", encoding="utf-8")
    return service.repo.project_create(
        title="本地素材",
        source_kind=source_kind,
        bvid=bvid,
        aid=aid,
        root_dir=str(root),
        stage="summarized",
    )


def with_summary(service, project):
    return service.repo.summary_insert(
        project["id"],
        content=SUMMARY_CONTENT,
        content_md="# 总结",
        role="standard",
    )


def call(service, method, params=None):
    return service.dispatch(method, params or {})


def ok(service, method, params=None):
    result = call(service, method, params)
    assert result["ok"] is True, result.get("error")
    return result["data"]


def fail(service, method, params=None):
    result = call(service, method, params)
    assert result["ok"] is False, result.get("data")
    return result["error"]


def account_gate(gates):
    return next(gate for gate in gates["gates"] if gate["id"] == "account")


def test_publish_binding_columns_exist_after_migration(service):
    from video_workbench.infra import db as db_mod

    names = {row[1] for row in service.repo.conn.execute("PRAGMA table_info(wb_project)")}
    assert {"publish_bvid", "publish_aid"} <= names
    assert db_mod.SCHEMA_VERSION == 6


def test_local_project_preview_exposes_empty_video_and_blocks_account(service, tmp_path):
    project = make_project(service, tmp_path)
    with_summary(service, project)

    data = ok(service, "publish.preview", {"project_id": project["id"]})
    assert data["video"] == {
        "bvid": "",
        "aid": 0,
        "source_kind": "local",
        "bound": False,
    }
    gate = account_gate(data["gates"])
    assert gate["status"] == "blocked"
    assert "视频号" in gate["reason"]


def test_saving_bv_binds_project_and_note(service, tmp_path):
    service._gateway = FakeGateway()
    service._resolver = FakeResolver()
    project = make_project(service, tmp_path)
    with_summary(service, project)

    data = ok(service, "publish.note", {"project_id": project["id"], "bvid": "bv1xx411c7mD"})
    assert data["note"]["bvid"] == "BV1xx411c7mD"
    assert data["note"]["aid"] == 778899
    row = service.repo.project_get(project["id"])
    assert row["publish_bvid"] == "BV1xx411c7mD"
    assert row["bvid"] is None, "发布绑定不能写进下载身份列"
    assert account_gate(data["gates"])["status"] != "blocked"


def test_saving_av_resolves_to_bv_and_aid(service, tmp_path):
    service._gateway = FakeGateway()
    service._resolver = FakeResolver()
    project = make_project(service, tmp_path)
    with_summary(service, project)

    data = ok(service, "publish.note", {"project_id": project["id"], "aid": "av778899"})
    assert data["note"]["bvid"] == "BV1av411c7mD"
    assert data["note"]["aid"] == 778899
    row = service.repo.project_get(project["id"])
    assert row["publish_bvid"] == "BV1av411c7mD"
    assert row["publish_aid"] == 778899
    assert row["bvid"] is None and row["aid"] is None


def test_invalid_bv_or_av_is_rejected_without_writing(service, tmp_path):
    service._gateway = FakeGateway()
    project = make_project(service, tmp_path)
    with_summary(service, project)

    for params in ({"bvid": "not-a-bv"}, {"aid": "avabc"}):
        error = fail(
            service,
            "publish.note",
            {"project_id": project["id"], **params},
        )
        assert error["code"] == "VALIDATION"
        row = service.repo.project_get(project["id"])
        assert row["publish_bvid"] is None and row["publish_aid"] is None
        assert service.repo.publish_for_project(project["id"]) is None


def test_bv_and_av_mismatch_is_rejected(service, tmp_path):
    service._gateway = FakeGateway()
    service._resolver = FakeResolver(bvid="BV1av411c7mD", aid=778899)
    project = make_project(service, tmp_path)
    with_summary(service, project)

    error = fail(
        service,
        "publish.note",
        {"project_id": project["id"], "bvid": "BV1av411c7mD", "aid": "av999999"},
    )
    assert error["code"] == "VALIDATION"
    row = service.repo.project_get(project["id"])
    assert row["publish_bvid"] is None and row["publish_aid"] is None


def test_blank_video_fields_do_not_clear_binding(service, tmp_path):
    service._gateway = FakeGateway()
    service._resolver = FakeResolver()
    project = make_project(service, tmp_path)
    with_summary(service, project)

    ok(service, "publish.note", {"project_id": project["id"], "bvid": "BV1xx411c7mD"})
    ok(service, "publish.note", {"project_id": project["id"], "bvid": "", "aid": ""})
    row = service.repo.project_get(project["id"])
    assert row["publish_bvid"] == "BV1xx411c7mD"


def test_downloaded_project_keeps_using_source_bvid(service, tmp_path):
    service._gateway = FakeGateway()
    project = make_project(
        service,
        tmp_path,
        bvid="BV1down411c7",
        aid=1234,
        source_kind="bilibili",
    )
    with_summary(service, project)

    data = ok(service, "publish.note", {"project_id": project["id"]})
    assert data["note"]["bvid"] == "BV1down411c7"
    assert data["note"]["aid"] == 1234
    row = service.repo.project_get(project["id"])
    assert row["publish_bvid"] is None and row["publish_aid"] is None
