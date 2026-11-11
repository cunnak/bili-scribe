"""T3 两个离线脚本的冒烟（backfill / dirty_report）。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_wb_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_v5_db(path: Path) -> Path:
    from video_workbench.infra import db as wbdb

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = wbdb.connect(path)
    try:
        wbdb.migrate(conn, path)
    finally:
        conn.close()
    return path


def _insert_project(
    conn: sqlite3.Connection,
    project_id: str,
    title: str,
    root_dir: Path,
    *,
    bvid: str | None = None,
    collection_title: str | None = None,
    collection_index: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO wb_project"
        " (id, title, source_kind, bvid, collection_title, collection_index,"
        "  root_dir, stage, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            title,
            "bilibili",
            bvid,
            collection_title,
            collection_index,
            str(root_dir),
            "new",
            "idle",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )


def test_backfill_dry_run_then_apply_only_null_rows(tmp_path, monkeypatch, capsys):
    mod = _load_script("backfill_collection_index")
    db = _make_v5_db(tmp_path / "data" / "wb.sqlite")
    conn = sqlite3.connect(str(db))
    try:
        _insert_project(conn, "p1", "第1讲", tmp_path / "media" / "p1", bvid="BV1", collection_title="C1")
        _insert_project(conn, "p2", "第2讲", tmp_path / "media" / "p2", bvid="BV2", collection_title="C1")
        _insert_project(
            conn, "p3", "第3讲", tmp_path / "media" / "p3", bvid="BV3", collection_title="C1",
            collection_index=99,
        )
        conn.commit()
    finally:
        conn.close()

    cfg = types.SimpleNamespace(
        db_path=str(db),
        storage=types.SimpleNamespace(key_path=str(tmp_path / "data" / "keys.json")),
    )
    monkeypatch.setattr(mod.WorkbenchConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(mod, "default_gateway", lambda *args, **kwargs: None)

    class FakeResolver:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, bvid: str):
            return types.SimpleNamespace(
                kind="collection",
                items=[
                    types.SimpleNamespace(bvid="BV1", collection_index=0),
                    types.SimpleNamespace(bvid="BV2", collection_index=1),
                    types.SimpleNamespace(bvid="BV3", collection_index=2),
                ],
            )

    monkeypatch.setattr(mod, "Resolver", FakeResolver)

    rc = mod.run(apply=False, as_json=True)
    assert rc == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run" and dry["planned"] == 2 and dry["written"] == 0
    with sqlite3.connect(str(db)) as check:
        values = check.execute(
            "SELECT id, collection_index FROM wb_project ORDER BY id"
        ).fetchall()
    assert values == [("p1", None), ("p2", None), ("p3", 99)]

    rc = mod.run(apply=True, as_json=True)
    assert rc == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply" and applied["planned"] == 2 and applied["written"] == 2
    assert applied["conflicts"] and applied["conflicts"][0]["id"] == "p3"
    with sqlite3.connect(str(db)) as check:
        values = check.execute(
            "SELECT id, collection_index FROM wb_project ORDER BY id"
        ).fetchall()
    assert values == [("p1", 0), ("p2", 1), ("p3", 99)]


def test_dirty_report_collect_three_classes_and_is_read_only(tmp_path):
    mod = _load_script("dirty_report")
    db = _make_v5_db(tmp_path / "data" / "wb.sqlite")
    temp_root = Path(tempfile.gettempdir()) / "wb-dirty-probe"
    normal = Path(tempfile.gettempdir()).parent / "wb-dirty-normal"
    conn = sqlite3.connect(str(db))
    try:
        # 1) build/accept/p6 两条
        _insert_project(conn, "b1", "烧字幕", tmp_path / "build" / "accept" / "p6-1")
        _insert_project(conn, "b2", "白底烧字幕", tmp_path / "build" / "accept" / "p6-2")
        # 2) 系统临时目录两条
        _insert_project(conn, "t1", "a", temp_root / "a")
        _insert_project(conn, "t2", "第2讲", temp_root / "b")
        # 3) 同名两组：a 与 第2讲
        _insert_project(conn, "d1", "a", normal / "a1")
        _insert_project(conn, "d2", "第2讲", normal / "b1")
        conn.commit()
    finally:
        conn.close()

    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    data = mod.collect(str(db))
    assert data["total_projects"] == 6
    assert len(data["build_accept_p6"]) == 2, data
    assert len(data["temp_root"]) == 2, data
    assert len(data["duplicate_groups"]) == 2, data
    titles = {group["title"] for group in data["duplicate_groups"]}
    assert titles == {"a", "第2讲"}
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
