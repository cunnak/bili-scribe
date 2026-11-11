#!/usr/bin/env python
r"""脏数据清单（T3-2）：只输出，不自动归档、不删除。

现在库里有三类"不该留在正式素材库里"的项目，这个脚本把它们列出来交给用户
决定（archived 还是 delete_many）：

1. 测试产物：root_dir 落在 build\accept\p6* 下；
2. 临时目录产物：root_dir 落在系统 %TEMP% 下；
3. 同名项目：title 完全相同的一组（本地导入/重复下载留下的）。

用法::

    .venv/Scripts/python.exe scripts/dirty_report.py
    .venv/Scripts/python.exe scripts/dirty_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_workbench.config import WorkbenchConfig  # noqa: E402


def _is_build_accept_p6(root: str) -> bool:
    parts = [part.casefold() for part in Path(root).parts]
    for index in range(len(parts) - 2):
        if parts[index] == "build" and parts[index + 1] == "accept" and parts[index + 2].startswith("p6"):
            return True
    return False


def _is_under_temp(root: str) -> bool:
    try:
        candidate = Path(root).resolve()
        base = Path(tempfile.gettempdir()).resolve()
    except (OSError, ValueError):
        return False
    return candidate == base or base in candidate.parents


def collect(db_path: str) -> dict[str, Any]:
    conn = sqlite3.connect("file:" + Path(db_path).resolve().as_posix() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, root_dir, collection_title, stage FROM wb_project "
            "ORDER BY created_at, id"
        ).fetchall()
    finally:
        conn.close()

    build_rows: list[dict[str, Any]] = []
    temp_rows: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        root = str(item.get("root_dir") or "")
        if _is_build_accept_p6(root):
            build_rows.append(item)
        elif _is_under_temp(root):
            temp_rows.append(item)

    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_title[str(row["title"] or "")].append(dict(row))
    duplicate_groups = [
        {"title": title, "items": items}
        for title, items in sorted(by_title.items())
        if title and len(items) > 1
    ]

    return {
        "db_path": db_path,
        "total_projects": len(rows),
        "build_accept_p6": build_rows,
        "temp_root": temp_rows,
        "duplicate_groups": duplicate_groups,
    }


def run(*, as_json: bool) -> int:
    cfg = WorkbenchConfig.load()
    db_path = cfg.db_path
    if not Path(db_path).is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2
    data = collect(db_path)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
        return 0

    print(f"脏数据清单（只读，共 {data['total_projects']} 个项目）")
    print(f"数据库：{db_path}")

    def dump(title: str, items: list[dict[str, Any]]) -> None:
        print(f"\n{title}：{len(items)} 条")
        for item in items:
            print(f"  {item['id']}  {item['title']}  [{item.get('stage')}]")
            print(f"      {item.get('root_dir')}")

    dump(r"build\accept\p6 测试产物", data["build_accept_p6"])
    dump("%TEMP% 下临时产物", data["temp_root"])
    print(f"\n同名项目：{len(data['duplicate_groups'])} 组")
    for group in data["duplicate_groups"]:
        print(f"  {group['title']}：{len(group['items'])} 条")
        for item in group["items"]:
            print(f"    {item['id']}  {item.get('root_dir')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="脏数据清单（只输出，不处理）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    return run(as_json=bool(args.json))


if __name__ == "__main__":
    sys.exit(main())
