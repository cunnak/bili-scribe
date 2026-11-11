#!/usr/bin/env python
"""按合集顺序回填 wb_project.collection_index（T3-1）。

背景：合集里的项目以前只记了 collection_title，没有"第几集"。列表想按合集
顺序排就只能靠标题，遇到「第2讲 / 第10讲」这种命名会排错。这个脚本对每个
collection_title 取一个代表 bvid 走一次 Resolver（和下载入口同一条解析
链路），拿回该合集完整的 episodes 顺序，再按 bvid 把序号写回项目。

默认 dry-run：只打印"将写哪些行"，不碰数据库。加 --apply 才写，
且只填 collection_index IS NULL 的行——已有值不覆盖，值不一致的单独列成
conflict 供人工确认（自动改可能把人工排过的顺序冲掉）。

用法::

    .venv/Scripts/python.exe scripts/backfill_collection_index.py
    .venv/Scripts/python.exe scripts/backfill_collection_index.py --apply
    .venv/Scripts/python.exe scripts/backfill_collection_index.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_workbench.config import WorkbenchConfig  # noqa: E402
from video_workbench.core.bili import default_gateway  # noqa: E402
from video_workbench.core.resolver import Resolver  # noqa: E402
from video_workbench.infra.db import now_iso  # noqa: E402


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, bvid, collection_title, collection_index FROM wb_project "
        "WHERE COALESCE(collection_title, '') <> '' "
        "ORDER BY collection_title, created_at, id"
    ).fetchall()


def _resolve_indexes(resolver: Resolver, title: str, bvid: str) -> dict[str, int]:
    """解析一个合集，返回 {bvid: collection_index}。"""
    if not bvid:
        raise ValueError(f"合集「{title}」没有可用的代表 bvid")
    resolution = resolver.resolve(bvid)
    mapping: dict[str, int] = {}
    for item in resolution.items:
        if item.bvid and item.collection_index is not None:
            mapping[item.bvid] = int(item.collection_index)
    if not mapping:
        raise ValueError(
            f"解析「{title}」得到 {resolution.kind}，但没有 collection_index（合集结构可能变了）"
        )
    return mapping


def run(*, apply: bool, as_json: bool) -> int:
    cfg = WorkbenchConfig.load()
    db_path = cfg.db_path
    if not Path(db_path).is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2

    conn = _connect(db_path)
    try:
        rows = _rows(conn)
    finally:
        conn.close()

    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[str(row["collection_title"])].append(row)

    resolver = Resolver(default_gateway(cfg.db_path, cfg.storage.key_path))
    plan: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for title, group in sorted(groups.items()):
        try:
            mapping = _resolve_indexes(resolver, title, str(group[0]["bvid"] or ""))
        except Exception as exc:  # noqa: BLE001 —— 单个合集解析失败不拖垮其余
            failures.append({"collection_title": title, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for row in group:
            bvid = str(row["bvid"] or "")
            want = mapping.get(bvid)
            if want is None:
                conflicts.append(
                    {
                        "id": row["id"],
                        "title": title,
                        "bvid": bvid,
                        "reason": "合集解析结果里没有这个 bvid",
                    }
                )
                continue
            current = row["collection_index"]
            if current is None:
                plan.append({"id": row["id"], "collection_title": title, "bvid": bvid, "index": want})
            elif int(current) != want:
                conflicts.append(
                    {
                        "id": row["id"],
                        "title": title,
                        "bvid": bvid,
                        "current": int(current),
                        "expected": want,
                        "reason": "已有值与解析结果不一致（未覆盖）",
                    }
                )

    removed = 0
    if apply and plan:
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            stamp = now_iso()
            for row in plan:
                cur = conn.execute(
                    "UPDATE wb_project SET collection_index=?, updated_at=? "
                    "WHERE id=? AND collection_index IS NULL",
                    (row["index"], stamp, row["id"]),
                )
                removed += int(cur.rowcount or 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    payload = {
        "db_path": db_path,
        "mode": "apply" if apply else "dry-run",
        "collections": len(groups),
        "projects": len(rows),
        "planned": len(plan),
        "written": removed,
        "conflicts": conflicts,
        "failures": failures,
        "plan": plan,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    else:
        print(f"数据库：{db_path}")
        print(f"模式：{'APPLY（已写）' if apply else 'dry-run（不写盘）'}")
        print(f"合集 {len(groups)} 个 / 有合集名的项目 {len(rows)} 条 / 待回填 {len(plan)} 条")
        for item in plan[:50]:
            print(f"  {item['id']}  [{item['index']:>2}]  {item['collection_title']}  {item['bvid']}")
        if len(plan) > 50:
            print(f"  … 其余 {len(plan) - 50} 条省略")
        if conflicts:
            print(f"冲突 {len(conflicts)} 条（未覆盖）：")
            for item in conflicts[:20]:
                print(f"  {item['id']}  {item['reason']}  current={item.get('current')} expected={item.get('expected')}")
        if failures:
            print(f"解析失败 {len(failures)} 个合集：")
            for item in failures:
                print(f"  {item['collection_title']}: {item['error']}")
        if apply:
            print(f"实际写入 {removed} 条")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="回填合集内序号（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只预览）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    return run(apply=bool(args.apply), as_json=bool(args.json))


if __name__ == "__main__":
    sys.exit(main())
