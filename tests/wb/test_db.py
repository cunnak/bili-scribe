"""数据库与仓储的验收（P0-03）。

重点不是"表建出来了"，而是三件会在真实使用中咬人的事：

* 迁移是**幂等**的——每次启动都会跑一遍，第二遍不能把内置样式插成两份。
* 上次进程被杀时留下的 ``running`` 任务必须被改成失败，否则界面上永远转圈。
* 查询能按 bvid 找回同一条目录，这是"素材库不重复下载"的地基。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from video_workbench.infra import db as wbdb
from video_workbench.infra.repo import Repo


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def test_migrate_creates_schema_and_styles(tmp_path: Path):
    conn = wbdb.open_and_migrate(tmp_path / "wb.sqlite")
    try:
        names = _tables(conn)
        assert {"wb_project", "wb_job", "wb_style"} <= names
        assert wbdb.schema_version(conn) == wbdb.SCHEMA_VERSION

        repo = Repo(conn)
        styles = repo.style_list()
        # P3-07 起两个内置预设：default（描边）+ rounded-default（圆角条）。
        # 列表按 builtin DESC, name 排，所以别断言顺序。
        assert {row["id"] for row in styles} == {"default", "rounded-default"}
        assert all(row["builtin"] for row in styles)
        # 内置样式已经迁到嵌套结构（primary/secondary）；还停在平铺字段说明
        # 「样式页看到 48、导出得到 54」那个坑又回来了
        assert styles[0]["config"]["primary"]["size"] >= 12
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path):
    path = tmp_path / "wb.sqlite"
    conn = wbdb.open_and_migrate(path)
    first = len(Repo(conn).style_list())
    wbdb.migrate(conn, path)
    wbdb.migrate(conn, path)
    assert len(Repo(conn).style_list()) == first
    conn.close()


def test_project_roundtrip_and_bvid_lookup(tmp_path: Path):
    conn = wbdb.open_and_migrate(tmp_path / "wb.sqlite")
    repo = Repo(conn)
    try:
        made = repo.project_create(title="某期视频", bvid="BV1xx411c7mD", root_dir=str(tmp_path / "p1"))
        assert made["id"].startswith("pj")
        assert made["stage"] == "new" and made["status"] == "idle"

        got = repo.project_get(made["id"])
        assert got and got["title"] == "某期视频"

        found = repo.project_find_by_bvid("BV1xx411c7mD")
        assert found and found["id"] == made["id"]

        # 第二个项目不干扰计数
        repo.project_create(title="另一个", bvid="BV1yy411c7mE", root_dir=str(tmp_path / "p2"))
        assert repo.project_count() == 2
        assert repo.project_count("某期") == 1
        assert len(repo.project_list(limit=1)) == 1
        assert len(repo.project_list(limit=10, offset=1)) == 1

        repo.project_update(made["id"], stage="transcribed", status="done")
        assert repo.project_get(made["id"])["stage"] == "transcribed"
    finally:
        conn.close()


def test_interrupted_jobs_become_failed(tmp_path: Path):
    """进程被强杀后重启：上次的 running 任务不能永远停在 running。"""
    conn = wbdb.open_and_migrate(tmp_path / "wb.sqlite")
    repo = Repo(conn)
    try:
        job = repo.job_create("job_x1", "dev.demo", None, {"seconds": 1})
        assert job["status"] == "queued"
        repo.job_update("job_x1", status="running")

        changed = repo.job_mark_interrupted()
        assert changed == 1

        after = repo.job_get("job_x1")
        assert after["status"] == "failed"
        assert (after.get("error_code") or "") != ""
        assert [row["id"] for row in repo.job_list(statuses=["failed"])] == ["job_x1"]
    finally:
        conn.close()


def test_job_payload_survives_roundtrip(tmp_path: Path):
    conn = wbdb.open_and_migrate(tmp_path / "wb.sqlite")
    repo = Repo(conn)
    try:
        project = repo.project_create(title="带任务的素材", root_dir=str(tmp_path / "p1"))
        repo.job_create("job_y1", "dev.demo", project["id"], {"seconds": 2, "nested": {"k": [1, 2]}})
        row = repo.job_get("job_y1")
        assert row["payload"] == {"seconds": 2, "nested": {"k": [1, 2]}}
        assert row["project_id"] == project["id"]
        assert row["result"] is None
    finally:
        conn.close()
