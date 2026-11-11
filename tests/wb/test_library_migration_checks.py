"""T0 迁移检查的常驻判据（v5）。

覆盖三件事：

* T0-2：补列桶登记表被删一个桶后，显式调用 `_assert_column_buckets()` 必须抛错；
* T0-1：v4 库迁移前自动备份，且备份是能打开、能读到旧数据的 sqlite 文件；
* T0-3：`--migrate-check` 只读，打印 JSON 的待补列，跑前跑后库文件字节与 mtime 不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _make_v4_db(path: Path) -> Path:
    """造一份带 wb_meta=4 与真 wb_project/wb_asset 表的 v4 库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE wb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE wb_project (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              source_kind TEXT NOT NULL,
              bvid TEXT, aid INTEGER, cid INTEGER, page_no INTEGER,
              up_mid INTEGER, up_name TEXT,
              collection_id TEXT,
              collection_title TEXT,
              root_dir TEXT NOT NULL,
              stage TEXT NOT NULL DEFAULT 'new',
              status TEXT NOT NULL DEFAULT 'idle',
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE wb_asset (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              bytes INTEGER,
              duration_ms INTEGER,
              width INTEGER, height INTEGER,
              fps REAL,
              vcodec TEXT, acodec TEXT,
              language TEXT,
              status TEXT NOT NULL DEFAULT 'ready',
              created_at TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO wb_meta(key, value) VALUES ('schema_version', '4')")
        conn.execute(
            "INSERT INTO wb_project"
            " (id, title, source_kind, root_dir, stage, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("pj_v4_1", "旧项目", "bilibili", str(path.parent / "media"), "downloaded", "idle",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO wb_asset"
            " (id, project_id, kind, path, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("as_v4_1", "pj_v4_1", "source", str(path.parent / "media" / "a.mp4"), "ready",
             "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_t0_2_column_bucket_registration_has_an_explicit_teeth_function(monkeypatch):
    from video_workbench.infra import db as wbdb

    assert wbdb._assert_column_buckets() is None
    buckets = getattr(wbdb, "_ALL_COLUMN_BUCKETS")
    v5 = getattr(wbdb, "_V5_COLUMNS")
    monkeypatch.setattr(
        wbdb, "_ALL_COLUMN_BUCKETS", tuple(bucket for bucket in buckets if bucket is not v5)
    )
    with pytest.raises(RuntimeError, match="必须往 _ALL_COLUMN_BUCKETS"):
        wbdb._assert_column_buckets()


def test_t0_1_migrate_backs_up_v4_database_and_backup_is_readable(tmp_path):
    from video_workbench.infra import db as wbdb

    path = _make_v4_db(tmp_path / "data" / "workbench.db")
    conn = wbdb.connect(path)
    try:
        result = wbdb.migrate(conn, path)
        assert result["from"] == 4
        assert result["to"] == wbdb.SCHEMA_VERSION
        backup = Path(result["backup"] or "")
        assert backup.is_file(), "migrate 返回值必须指向真实备份文件"
        assert ".bak-" in backup.name
        assert backup.parent == path.parent
        with sqlite3.connect(str(backup)) as old:
            assert old.execute("SELECT value FROM wb_meta WHERE key='schema_version'").fetchone()[0] == "4"
            assert old.execute("SELECT COUNT(*) FROM wb_project").fetchone()[0] == 1
    finally:
        conn.close()


def test_t0_3_migrate_check_is_read_only_and_reports_pending_columns(cfg_path):
    from video_workbench.config import WorkbenchConfig

    cfg = WorkbenchConfig.load(str(cfg_path))
    db_path = Path(cfg.db_path)
    _make_v4_db(db_path)
    before_bytes = db_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = db_path.stat().st_mtime_ns

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "video_workbench.cli",
            "--migrate-check",
            "--config",
            str(cfg_path),
            "--json",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    data = payload.get("data", payload)
    assert data["migrate_needed"] is True
    assert "wb_project.archived_at" in data["pending_columns"]
    assert "wb_project.collection_index" in data["pending_columns"]
    assert Path(data["db_path"]) == db_path
    assert db_path.read_bytes() == before_bytes
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash
    assert db_path.stat().st_mtime_ns == before_mtime
    assert not list(db_path.parent.glob("workbench.db.bak-*"))
