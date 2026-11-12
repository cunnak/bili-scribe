"""素材库信息架构重构（v5）的常驻判据。

覆盖三组东西：

* 列表契约：73 条全量、facets 求和、排序稳定、分页可达、limit 夹取；
* 迁移与牙：v4 旧库升 v5 两列落地；删掉 V5 列桶后旧库必须补不上列；
* 安全闸：排序注入、批量删除上限/跳过、确认框总数、CSP、innerHTML、阶段词表。

这些用例只动测试侧：D3 的"把 V5 桶删掉"用 monkeypatch 模块属性实现，不改产品文件。
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_JS = ROOT / "video_workbench" / "web" / "js"

#: A2 用的"像真库那样"的分布：8 个规范阶段 + 历史遗留的 analyzed 3 条。
_REAL_LIKE_STAGES = [
    ("downloaded", 55),
    ("new", 5),
    ("subtitle", 3),
    ("transcribed", 2),
    ("summarized", 3),
    ("clipped", 2),
    ("merged", 0),
    ("published", 0),
    ("analyzed", 3),
]

#: 三种界面排序。对齐 T1-3 的 SORTS 与 T1-4 的 title 分支。
_UI_SORTS = ("created_desc", "title", "collection_index")


# --------------------------------------------------------------------------- helpers


def _rpc(service, method: str, params: dict | None = None) -> dict:
    return service.dispatch(method, dict(params or {}))


def _ok(service, method: str, params: dict | None = None) -> dict:
    payload = _rpc(service, method, params)
    assert payload.get("ok") is True, payload.get("error")
    return payload["data"]


def _project(service, tmp_path: Path, title: str, stage: str = "new", **fields):
    root = tmp_path / "media" / re.sub(r"[^\w.-]+", "_", title)
    root.mkdir(parents=True, exist_ok=True)
    return service.repo.project_create(title=title, stage=stage, root_dir=str(root), **fields)


def _set(service, project_id: str, **columns) -> None:
    """直接改测试夹具的时间/集号列；不是产品调用面。"""
    assignments = ", ".join(f"{key}=?" for key in columns)
    service.repo._exec(
        f"UPDATE wb_project SET {assignments} WHERE id=?",
        [*columns.values(), project_id],
    )


def _bulk(service, tmp_path: Path, count: int, stage: str = "downloaded", prefix: str = "P"):
    return [_project(service, tmp_path, f"{prefix}{i:04d}", stage=stage) for i in range(count)]


def _seed_73(service, tmp_path: Path) -> list[dict]:
    projects: list[dict] = []
    serial = 0
    for stage, count in _REAL_LIKE_STAGES:
        for _ in range(count):
            projects.append(_project(service, tmp_path, f"项目{serial:03d}", stage=stage))
            serial += 1
    assert len(projects) == 73
    return projects


def _strip_js_comments(text: str) -> str:
    # 保留原行数：块注释里有换行时用等量换行占位，报错行号才对得上文件。
    text = re.sub(
        r"/\*.*?\*/",
        lambda match: "\n" * match.group(0).count("\n"),
        text,
        flags=re.S,
    )
    return re.sub(r"(?m)//.*$", "", text)


def _make_v4_db(tmp_path: Path) -> Path:
    """手工造一份 v4 库（3 行项目 + 3 条资产），不带 v5 的两列。"""
    path = tmp_path / "data" / "workbench.db"
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
        for i in range(3):
            pid = f"pj_old_{i}"
            conn.execute(
                "INSERT INTO wb_project"
                " (id, title, source_kind, root_dir, stage, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    pid,
                    f"旧项目{i}",
                    "bilibili",
                    str(tmp_path / "media" / pid),
                    "downloaded",
                    "idle",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )
            conn.execute(
                "INSERT INTO wb_asset"
                " (id, project_id, kind, path, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    f"as_{i}",
                    pid,
                    "source",
                    str(tmp_path / "media" / f"{pid}.mp4"),
                    "ready",
                    "2026-01-01T00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------- A 正常路径


def test_a1_seventy_three_fixture_lists_all_items_and_total_73(service, tmp_path):
    _seed_73(service, tmp_path)
    data = _ok(service, "library.list", {"limit": 200})
    assert data["total"] == 73
    assert len(data["items"]) == 73
    assert len({row["id"] for row in data["items"]}) == 73


def test_a2_facets_sum_to_total_including_analyzed_and_summarized_rows(service, tmp_path):
    _seed_73(service, tmp_path)
    data = _ok(service, "library.list", {"limit": 200})
    facets = data.get("facets")
    assert isinstance(facets, list) and facets, "library.list 必须返回 facets"
    counts = {str(f["key"]): int(f["count"]) for f in facets}
    assert data["total"] == 73
    assert sum(counts.values()) == data["total"], (
        f"sum(facets) 必须等于 total；analyzed/summarized 这类行不许落在桶外：{counts}"
    )


def test_a3_collection_index_sort_is_ascending_within_the_collection(service, tmp_path):
    projects = [
        _project(
            service,
            tmp_path,
            f"合集第{i:02d}讲",
            stage="downloaded",
            collection_title="2026正大杯备赛",
        )
        for i in range(60)
    ]
    order = list(range(60))
    random.Random(7).shuffle(order)
    for i, project in enumerate(projects):
        # created_at 与集号顺序相反：若实现退回按时间排，行序会整体反过来。
        _set(
            service,
            project["id"],
            collection_index=order[i],
            created_at=f"2026-01-01T00:00:{59 - i:02d}",
        )
    data = _ok(service, "library.list", {"limit": 200, "sort": "collection_index"})
    assert data["total"] == 60
    by_index = {order[i]: projects[i]["id"] for i in range(60)}
    assert [row["id"] for row in data["items"]] == [by_index[i] for i in range(60)]


def test_a4b_default_created_desc_keeps_insertion_order_for_tied_timestamps(service, tmp_path):
    """兼容红线：默认排序不能在同秒创建的两条上翻面。

    字幕页验收（test_subtitles_real_backend_ui）用两个同秒项目，旧默认
    `ORDER BY created_at DESC` 的并列顺序是插入序；给 SQL 加 `id DESC`
    会把第一项换掉，整条字幕切换链路因此红过。
    """
    first = _project(service, tmp_path, "先建")
    second = _project(service, tmp_path, "后建")
    stamp = "2026-03-01T00:00:00"
    _set(service, first["id"], created_at=stamp)
    _set(service, second["id"], created_at=stamp)
    data = _ok(service, "library.list", {"limit": 200})
    assert [row["id"] for row in data["items"]] == [first["id"], second["id"]]


def test_a4_three_sorts_are_stable_and_keep_every_row_with_duplicate_timestamps(service, tmp_path):
    projects = [_project(service, tmp_path, f"第{i + 1}讲", stage="downloaded") for i in range(20)]
    for i, project in enumerate(projects[5:]):
        _set(
            service,
            project["id"],
            created_at=f"2026-02-01T00:00:{i:02d}",
            updated_at=f"2026-02-01T00:00:{i:02d}",
        )
    for project in projects[:5]:
        _set(
            service,
            project["id"],
            created_at="2026-02-01T00:00:30",
            updated_at="2026-02-01T00:00:30",
        )
    all_ids = {project["id"] for project in projects}
    for sort in _UI_SORTS:
        first = _ok(service, "library.list", {"limit": 200, "sort": sort})["items"]
        second = _ok(service, "library.list", {"limit": 200, "sort": sort})["items"]
        assert len(first) == 20, f"{sort}: 同时间戳的行不许被丢掉"
        assert {row["id"] for row in first} == all_ids
        assert [row["id"] for row in first] == [row["id"] for row in second], (
            f"{sort}: 同一状态下两次调用必须同序"
        )


# --------------------------------------------------------------------------- C 边界 / 规模


@pytest.mark.parametrize("size", [200, 201, 1000])
def test_c1_the_201st_row_is_reachable_by_offset_pagination(service, tmp_path, size):
    _bulk(service, tmp_path, size)
    seen: list[str] = []
    offset = 0
    total = None
    while True:
        page = _ok(service, "library.list", {"limit": 200, "offset": offset})
        total = page["total"]
        items = page["items"]
        if not items:
            break
        seen.extend(row["id"] for row in items)
        offset += 200
    assert total == size
    assert len(seen) == size
    assert len(set(seen)) == size
    if size > 200:
        second = _ok(service, "library.list", {"limit": 200, "offset": 200})
        assert len(second["items"]) == min(200, size - 200)


def test_c2_limit_zero_1000_and_negative_are_clamped_to_1_200_1(service, tmp_path):
    _bulk(service, tmp_path, 3)
    for raw, clamped in ((0, 1), (1000, 200), (-5, 1)):
        data = _ok(service, "library.list", {"limit": raw})
        assert data["limit"] == clamped, f"limit={raw} 应夹到 {clamped}"
        assert len(data["items"]) == min(3, clamped)


def test_c3_offset_past_the_end_returns_no_items_and_keeps_total(service, tmp_path):
    _seed_73(service, tmp_path)
    data = _ok(service, "library.list", {"limit": 200, "offset": 1000})
    assert data["items"] == []
    assert data["total"] == 73


# --------------------------------------------------------------------------- D 兼容 / 迁移


def test_d1_v4_database_migrates_and_gains_archived_at_and_collection_index(tmp_path):
    from video_workbench.infra import db as wbdb

    path = _make_v4_db(tmp_path)
    conn = wbdb.open_and_migrate(path)
    try:
        assert wbdb.schema_version(conn) == wbdb.SCHEMA_VERSION
        assert wbdb.SCHEMA_VERSION >= 5
        cols = {row[1] for row in conn.execute("PRAGMA table_info(wb_project)")}
        assert {"archived_at", "collection_index"} <= cols
        rows = conn.execute(
            "SELECT id, archived_at, collection_index FROM wb_project ORDER BY id"
        ).fetchall()
        assert [row["id"] for row in rows] == ["pj_old_0", "pj_old_1", "pj_old_2"]
        assert all(row["archived_at"] is None and row["collection_index"] is None for row in rows)
        assert conn.execute("SELECT COUNT(*) FROM wb_asset").fetchone()[0] == 3
    finally:
        conn.close()
    backups = list((tmp_path / "data").glob("workbench.db.bak-*"))
    assert backups, "v4→v5 迁移前必须自动备份（T0-1）"


def test_d3_removing_the_v5_bucket_makes_the_old_database_miss_both_columns(monkeypatch, tmp_path):
    """T0-2 的牙：把 V5 列桶从登记表里删掉，旧库就补不出列，用的时候必然 no such column。"""
    from video_workbench.infra import db as wbdb

    v5 = getattr(wbdb, "_V5_COLUMNS", None)
    assert v5 is not None, "T0-2 要求模块级 _V5_COLUMNS 登记桶"
    buckets = getattr(wbdb, "_ALL_COLUMN_BUCKETS", None)
    assert buckets is not None, "T0-2 要求 _ALL_COLUMN_BUCKETS 作为唯一登记表"
    assert v5 in buckets, "V5 桶必须在 _ALL_COLUMN_BUCKETS 里登记"
    monkeypatch.setattr(wbdb, "_ALL_COLUMN_BUCKETS", tuple(b for b in buckets if b is not v5))

    path = _make_v4_db(tmp_path)
    conn = wbdb.connect(path)
    try:
        wbdb.migrate(conn, path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(wb_project)")}
        assert "archived_at" not in cols and "collection_index" not in cols
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            conn.execute("SELECT archived_at FROM wb_project").fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- F 安全闸


def test_f1_sort_injection_returns_validation_and_keeps_the_row_count(service, tmp_path):
    _bulk(service, tmp_path, 5)
    valid = _ok(service, "library.list", {"sort": "title"})
    assert len(valid["items"]) == 5
    before = service.repo.project_count()
    payload = _rpc(
        service,
        "library.list",
        {"sort": "created_at; DROP TABLE wb_project--"},
    )
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "VALIDATION", payload["error"]
    assert service.repo.project_count() == before == 5


def test_f2_delete_many_rejects_1001_ids_with_validation_and_deletes_nothing(service, tmp_path):
    projects = _bulk(service, tmp_path, 2)
    ids = [project["id"] for project in projects] + [f"pj_missing_{i}" for i in range(999)]
    assert len(ids) == 1001
    payload = _rpc(service, "library.delete_many", {"ids": ids, "expect_total": 1001})
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "VALIDATION", payload["error"]
    assert service.repo.project_count() == 2


def test_f3_delete_many_ten_ids_with_one_missing_removes_nine_and_skips_one(service, tmp_path):
    projects = _bulk(service, tmp_path, 12)
    # 严格按验收清单：10 个 id 里混 1 个不存在的，删除 9 条、跳过 1 条。
    ids = [project["id"] for project in projects[:9]] + ["pj_does_not_exist"]
    assert len(ids) == 10
    preview = _ok(service, "library.delete_preview", {"ids": ids})
    assert preview["total"] == 9
    result = _ok(
        service,
        "library.delete_many",
        {"ids": ids, "expect_total": preview["total"]},
    )
    assert result["removed"] == 9
    assert result["skipped"] == [{"id": "pj_does_not_exist", "reason": "项目不存在"}]
    snapshot = Path(result["snapshot_path"])
    assert snapshot.is_file(), "删除前必须落 json 快照"
    assert isinstance(json.loads(snapshot.read_text(encoding="utf-8")), dict)
    assert service.repo.project_count() == 3


def test_f4_delete_preview_reports_filter_total_1000_not_page_size_200(service, tmp_path):
    _bulk(service, tmp_path, 1000, stage="downloaded")
    listing = _ok(service, "library.list", {"limit": 200, "stage": "downloaded"})
    assert listing["total"] == 1000
    assert len(listing["items"]) == 200
    preview = _ok(service, "library.delete_preview", {"query": "", "stage": "downloaded"})
    assert preview["total"] == 1000, "确认框数量必须来自筛选总数，不是当前页 200 条"


def test_f5_index_html_keeps_connect_src_none_and_the_package_starts_no_http_server():
    html = (ROOT / "video_workbench" / "web" / "index.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in html, "CSP 的 connect-src 'none' 是产品契约"
    desktop = (ROOT / "video_workbench" / "desktop.py").read_text(encoding="utf-8")
    assert "http_server=False" in desktop, "pywebview 不许起自带 HTTP 服务"

    pattern = re.compile(
        r"(?m)^\s*(?:import|from)\s+http\.server\b"
        r"|HTTPServer\s*\("
        r"|TCPServer\s*\("
        r"|uvicorn\.run\s*\("
        r"|FastAPI\s*\("
    )
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "video_workbench").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"WB 包内不许起 HTTP server：{offenders}"


def test_f6_innerhtml_only_accepts_empty_string_and_ui_svg():
    violations: list[str] = []
    sink_files: list[str] = []
    assign = re.compile(r"innerHTML\s*\+?=\s*([^;\n]+)")
    for path in sorted(WEB_JS.rglob("*.js")):
        text = _strip_js_comments(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(ROOT))
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in assign.finditer(line):
                rhs = match.group(1).strip()
                if rhs in ('""', "''"):
                    continue
                if rhs.startswith("ui.svg("):
                    continue
                if path.name == "ui.js" and rhs == "value":
                    sink_files.append(f"{rel}:{lineno}")
                    continue
                violations.append(f"{rel}:{lineno}: innerHTML = {rhs}")
    assert violations == [], f"innerHTML 只允许空串或 ui.svg：{violations}"
    assert len(sink_files) == 1, f"只允许 ui.js 的 h() 保留唯一 HTML sink：{sink_files}"


# --------------------------------------------------------------------------- T4-2 阶段词表


def test_t4_2_stage_literals_in_web_js_are_subset_of_stage_order():
    from video_workbench.core.stages import STAGE_ORDER

    allowed = set(STAGE_ORDER)
    violations: list[str] = []
    quoted = re.compile(r"""["']([a-z][a-z0-9_]*)["']""")
    # 只看"给 stage 赋/比较一个字符串"的位置；span/div 这类标签名和中文文案不是阶段值。
    stage_value = re.compile(
        r"""(?:\bstage\b|["']data-stage["'])\s*(?:===|!==|==|!=|=|:)\s*["']([a-z][a-z0-9_]*)["']"""
    )
    for path in sorted(WEB_JS.rglob("*.js")):
        text = _strip_js_comments(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(ROOT))
        for lineno, line in enumerate(text.splitlines(), 1):
            for token in stage_value.findall(line):
                if token not in allowed:
                    violations.append(f"{rel}:{lineno}: {token!r}")
        for block_name in ("STAGES", "STAGE_FILTERS"):
            match = re.search(rf"var\s+{block_name}\s*=\s*[\{{[](.*?)\n\s*[\}}\]];", text, re.S)
            if not match:
                continue
            for token in quoted.findall(match.group(1)):
                if token not in allowed:
                    violations.append(f"{rel}: <{block_name}>: {token!r}")
    assert violations == [], (
        f"web/js 里的 stage 字面量必须全部属于 core.stages.STAGE_ORDER；越界项：{violations}"
    )


# --------------------------------------------------------------------------- A5 / B4 / C4-C6 / D5 / E1-E2 / F7 / T1


def test_a5_collection_group_count_matches_the_same_list_call(service, tmp_path):
    for i in range(60):
        _project(
            service,
            tmp_path,
            f"合集第{i:02d}讲",
            stage="downloaded",
            collection_title="2026正大杯备赛",
        )
    data = _ok(service, "library.list", {"stage": "", "limit": 200})
    rows = [row for row in data["items"] if row.get("collection_title") == "2026正大杯备赛"]
    assert data["total"] == 60
    assert len(rows) == data["total"] == 60, (
        "组头数字必须来自同一个 library.list(stage='') 的过滤结果"
    )


def test_a6_collection_filter_narrows_rows_and_facets_to_the_source(service, tmp_path):
    wanted = [
        _project(
            service, tmp_path, "合集A-下载1", stage="downloaded", collection_title="2026正大杯备赛"
        ),
        _project(
            service, tmp_path, "合集A-下载2", stage="downloaded", collection_title="2026正大杯备赛"
        ),
        _project(
            service, tmp_path, "合集A-总结", stage="summarized", collection_title="2026正大杯备赛"
        ),
        _project(
            service, tmp_path, "合集A-旧阶段", stage="analyzed", collection_title="2026正大杯备赛"
        ),
        _project(
            service, tmp_path, "合集B-下载", stage="downloaded", collection_title="另一个合集"
        ),
        _project(service, tmp_path, "单稿-下载", stage="downloaded"),
    ]
    data = _ok(service, "library.list", {"collection": "2026正大杯备赛", "limit": 200})
    assert data["total"] == 4
    assert {row["id"] for row in data["items"]} == {row["id"] for row in wanted[:4]}
    assert all(row["collection_title"] == "2026正大杯备赛" for row in data["items"])
    assert sum(int(facet["count"]) for facet in data["facets"]) == data["total"]
    counts = {facet["key"]: int(facet["count"]) for facet in data["facets"]}
    assert counts["downloaded"] == 2
    assert counts["summarized"] == 1
    assert counts["analyzed"] == 1

    empty = _ok(service, "library.list", {"collection": "不存在的合集", "limit": 200})
    assert empty["items"] == []
    assert empty["total"] == 0
    assert sum(int(facet["count"]) for facet in empty["facets"]) == 0


def test_a7_source_group_filters_match_tree_buckets(service, tmp_path):
    local = _project(service, tmp_path, "本地导入", source_kind="local")
    unassigned = _project(service, tmp_path, "有BV无合集", bvid="BV1aa411c7mF")
    other = _project(service, tmp_path, "无BV无合集")
    in_collection = _project(service, tmp_path, "合集内", collection_title="合集C")

    def ids(group: str) -> set[str]:
        data = _ok(service, "library.list", {"group": group, "limit": 200})
        assert sum(int(facet["count"]) for facet in data["facets"]) == data["total"]
        return {row["id"] for row in data["items"]}

    assert ids("local") == {local["id"]}
    assert ids("unassigned") == {unassigned["id"]}
    assert ids("other") == {other["id"]}
    assert in_collection["id"] not in ids("local") | ids("unassigned") | ids("other")

    payload = _rpc(service, "library.list", {"group": "not-a-group"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION"


def test_a8_collection_filter_combines_with_stage_and_archived_total(service, tmp_path):
    a_downloaded = [
        _project(service, tmp_path, f"A下载{i}", stage="downloaded", collection_title="合集D")
        for i in range(3)
    ]
    a_summarized = _project(
        service, tmp_path, "A总结", stage="summarized", collection_title="合集D"
    )
    _project(service, tmp_path, "B下载", stage="downloaded", collection_title="合集E")

    staged = _ok(
        service, "library.list", {"collection": "合集D", "stage": "downloaded", "limit": 200}
    )
    assert staged["total"] == 3
    assert {row["id"] for row in staged["items"]} == {row["id"] for row in a_downloaded}
    assert sum(int(facet["count"]) for facet in staged["facets"]) == 4, (
        "facets 必须覆盖来源内全部阶段，不能因为选了 downloaded 就只剩一个桶"
    )

    service.repo.project_archive([a_summarized["id"]], True)
    visible = _ok(service, "library.list", {"collection": "合集D", "limit": 200})
    assert visible["total"] == 3
    assert visible["archived_total"] == 1
    assert all(row["id"] != a_summarized["id"] for row in visible["items"])


def test_a9_delete_preview_and_delete_many_respect_collection_filter(service, tmp_path):
    for i in range(3):
        _project(service, tmp_path, f"待删A{i}", stage="downloaded", collection_title="待删合集A")
    for i in range(2):
        _project(service, tmp_path, f"保留B{i}", stage="downloaded", collection_title="保留合集B")

    preview = _ok(service, "library.delete_preview", {"collection": "待删合集A"})
    assert preview["total"] == 3

    removed = _ok(
        service,
        "library.delete_many",
        {"collection": "待删合集A", "expect_total": 3},
    )
    assert removed["removed"] == 3

    assert _ok(service, "library.list", {"collection": "待删合集A"})["total"] == 0
    assert _ok(service, "library.list", {"collection": "保留合集B"})["total"] == 2

    bad = _rpc(service, "library.delete_preview", {"group": "not-a-group"})
    assert bad["ok"] is False, "批量删除入口不能绕过来源分组校验"
    assert bad["error"]["code"] == "VALIDATION"


def test_b4_library_get_missing_project_returns_not_found_after_delete(service, tmp_path):
    project = _project(service, tmp_path, "待删项目", stage="downloaded")
    project_id = project["id"]

    deleted = _ok(service, "library.delete", {"id": project_id})
    assert deleted["removed"] is True

    payload = _rpc(service, "library.get", {"id": project_id})
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "NOT_FOUND", payload["error"]

    library_js = _strip_js_comments((WEB_JS / "pages" / "library.js").read_text(encoding="utf-8"))
    assert 'state.detailNotice = "项目不存在或已被移除"' in library_js
    assert 'err && err.code === "NOT_FOUND"' in library_js
    assert "select(next.id, { keepNotice: true, force: true })" in library_js
    assert "rows[Math.min(index < 0 ? 0 : index, rows.length - 1)]" in library_js


def test_c4_zero_stage_facet_and_empty_list(service):
    from video_workbench.core.stages import STAGE_ORDER

    zero_stage = next(stage for stage in STAGE_ORDER if stage == "published")
    data = _ok(service, "library.list", {"stage": zero_stage, "limit": 200})
    assert data["items"] == []
    assert data["total"] == 0
    facet = next(item for item in data["facets"] if item["key"] == zero_stage)
    assert int(facet["count"]) == 0

    library_js = _strip_js_comments((WEB_JS / "pages" / "library.js").read_text(encoding="utf-8"))
    assert "var zero = !facet.count" in library_js
    assert "is-zero" in library_js
    assert "disabled: zero && !active" in library_js


def test_c5_thumbnail_path_keeps_special_characters_and_tofileurl_escapes(service, tmp_path):
    # Windows 文件名不允许 "?"；标题里可以有，路径里用其余四个保留字符。
    root = tmp_path / "media" / "合集 #1 & +"
    (root / "frames").mkdir(parents=True, exist_ok=True)
    thumb = root / "frames" / "thumb.jpg"
    thumb.write_bytes(b"not-a-real-jpeg-but-a-real-file")
    project = service.repo.project_create(
        title="合集 #1? & +",
        stage="downloaded",
        root_dir=str(root),
    )

    data = _ok(service, "library.list", {"limit": 200})
    row = next(item for item in data["items"] if item["id"] == project["id"])
    assert row["thumb"] == str(thumb)
    assert row["has_thumb"] is True

    rpc_js = (WEB_JS / "rpc.js").read_text(encoding="utf-8")
    assert "encodeURI(text)" in rpc_js
    for escaped in ('"%23"', '"%3F"', '"%26"', '"%2B"'):
        assert escaped in rpc_js, f"toFileUrl 缺少 {escaped} 转义"
    assert "toFileUrl(text)" in rpc_js


def test_c6_thousand_row_wal_library_list_cold_and_hot(service, tmp_path):
    projects = _bulk(service, tmp_path, 1000)
    media = tmp_path / "media"
    for i, project in enumerate(projects):
        service.repo.asset_upsert_singleton(
            project["id"],
            "source",
            str(media / f"{i}.mp4"),
        )

    mode = service.repo._one("PRAGMA journal_mode")
    assert mode is not None
    assert str(mode["journal_mode"]).lower() == "wal", mode

    started = time.perf_counter()
    first = _ok(service, "library.list", {"limit": 200, "offset": 0})
    cold_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    second = _ok(service, "library.list", {"limit": 200, "offset": 0})
    hot_ms = (time.perf_counter() - started) * 1000
    print(f"[C6] rows={first['total']} cold={cold_ms:.1f}ms hot={hot_ms:.1f}ms")
    assert first["total"] == second["total"] == 1000
    assert len(first["items"]) == 200


def test_d5_collection_title_search_hits(service, tmp_path):
    project = _project(
        service,
        tmp_path,
        "普通标题",
        stage="downloaded",
        collection_title="深度学习合集",
    )
    data = _ok(service, "library.list", {"query": "深度学习", "limit": 200})
    assert data["total"] == 1, f"collection_title 应参与搜索，实际 total={data['total']}"
    assert [row["id"] for row in data["items"]] == [project["id"]]


def test_e1_e2_v5_backup_opens_and_new_values_read_back(tmp_path):
    from video_workbench.infra import db as wbdb

    path = _make_v4_db(tmp_path)
    conn = wbdb.open_and_migrate(path)
    try:
        old_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, title, stage, created_at, updated_at FROM wb_project ORDER BY id"
            )
        ]
        conn.execute(
            "UPDATE wb_project SET archived_at=?, collection_index=? WHERE id=?",
            ("2026-02-02T00:00:00", 7, "pj_old_0"),
        )
        conn.commit()
    finally:
        conn.close()

    backups = sorted((tmp_path / "data").glob("workbench.db.bak-*"))
    assert backups, "v5 迁移必须先留备份"
    backup = backups[0]
    backup_conn = sqlite3.connect(str(backup))
    backup_conn.row_factory = sqlite3.Row
    try:
        cols = {row[1] for row in backup_conn.execute("PRAGMA table_info(wb_project)")}
        assert "archived_at" not in cols and "collection_index" not in cols
        version = backup_conn.execute(
            "SELECT value FROM wb_meta WHERE key='schema_version'"
        ).fetchone()
        assert version["value"] == "4"
        assert backup_conn.execute("SELECT COUNT(*) FROM wb_project").fetchone()[0] == 3
        backup_rows = [
            dict(row)
            for row in backup_conn.execute(
                "SELECT id, title, stage, created_at, updated_at FROM wb_project ORDER BY id"
            )
        ]
        assert backup_rows == old_rows, "备份里的旧数据必须逐行完整"
    finally:
        backup_conn.close()

    readback = sqlite3.connect(str(path))
    readback.row_factory = sqlite3.Row
    try:
        rows = readback.execute(
            "SELECT id, archived_at, collection_index FROM wb_project ORDER BY id"
        ).fetchall()
        assert [(row["id"], row["archived_at"], row["collection_index"]) for row in rows] == [
            ("pj_old_0", "2026-02-02T00:00:00", 7),
            ("pj_old_1", None, None),
            ("pj_old_2", None, None),
        ]
    finally:
        readback.close()


def test_f7_reveal_outside_allowed_roots_is_validation(service, tmp_path):
    # tmp_path 本身在 source_path 的父目录里（配置就在 tmp_path），所以往外挪一层。
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside media root", encoding="utf-8")
    roots = service._allowed_roots()
    assert all(not outside.resolve().is_relative_to(Path(root).resolve()) for root in roots)

    payload = _rpc(service, "fs.reveal", {"path": str(outside)})
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "VALIDATION", payload["error"]


def test_t1_9_resolver_collection_index_is_zero_based_sequence():
    from test_resolver import COLLECTION_VIEW, VIDEO_VIEW, make_resolver

    view = deepcopy(COLLECTION_VIEW)
    episodes = []
    for i in range(39):
        episodes.append(
            {
                "bvid": f"BV1aa411c7m{i:02d}",
                "aid": 170003 + i,
                "cid": 21 + i,
                "title": str(i + 1),
                "duration": 500,
                "arc": {
                    "title": f"第{i + 1}集",
                    "pic": "",
                    "duration": 500,
                    "aid": 170003 + i,
                },
            }
        )
    view["ugc_season"]["sections"] = [{"episodes": episodes}]

    resolver, _, _ = make_resolver({VIDEO_VIEW: view})
    result = resolver.resolve("BV1aa411c7mF")
    assert len(result.items) == 39
    assert [item.collection_index for item in result.items] == list(range(39))


def test_t1_10_asset_project_query_uses_index(service):
    plan = service.repo._all(
        "EXPLAIN QUERY PLAN SELECT id, path FROM wb_asset WHERE project_id=?",
        ("pj_probe",),
    )
    details = " ".join(str(row["detail"]) for row in plan)
    assert "idx_wb_asset_project" in details.lower(), details
    assert "SCAN wb_asset" not in details.upper(), details
