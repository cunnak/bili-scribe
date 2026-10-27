"""「字段级 diff 该不该做」——先用库里全部真实迭代量一量频率。

为什么先量：这个功能的价值完全取决于「模型顺手改了你没要求的地方」发生得有多频繁。
如果五次里一次，那它值得占一行界面；如果二十次里一次，那它只是噪音。

⚠️ 样本会很小（这台机器上真实迭代就那么几次）。所以结论要按"小样本"报，
不装成统计显著。另外这里有两个**启发式**，结果只能当线索，不能当判据：
  · "要求里提没提到这个字段"——用字段名是否出现在要求文本里判断；
  · "改动说明里提没提到"——同理。
模型完全可能描述了改动却不点字段名。所以正文里两个数分开列，不合并成一个判决。

只读真实库。零模型调用。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"

CONTENT_FIELDS = ("one_line", "summary_md", "key_points", "timeline", "caution", "comment_short")
SHORT = {
    "one_line": "one_line", "summary_md": "summary_md", "key_points": "key_points",
    "timeline": "timeline", "caution": "caution", "comment_short": "comment_short",
}

conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT task_id, version_no, source, requirement, summary_json, metrics "
    "FROM summary_version ORDER BY task_id, version_no"
).fetchall()

# 按任务归组
by_task: dict[str, list[dict]] = {}
for r in rows:
    try:
        payload = json.loads(r["summary_json"] or "{}")
    except ValueError:
        payload = {}
    by_task.setdefault(r["task_id"], []).append(
        {**dict(r), "json": payload}
    )

refines = []
restores = 0
for task_id, versions in by_task.items():
    for old, new in zip(versions, versions[1:]):
        if new["source"] == "restore":
            restores += 1
            continue
        if new["source"] != "refine":
            continue
        changed = [
            f for f in CONTENT_FIELDS if old["json"].get(f) != new["json"].get(f)
        ]
        refines.append(
            {
                "task": task_id,
                "no": new["version_no"],
                "requirement": new["requirement"] or "",
                "note": (new["json"].get("revise_note") or ""),
                "changed": changed,
            }
        )

print("=" * 76)
print("库里一共有多少次「真实迭代」")
print("=" * 76)
print(f"  有版本史的任务：{len(by_task)} 条")
print(f"  迭代（refine）：{len(refines)} 次     回退（restore）：{restores} 次")
print("  ⚠️ 样本就这么点，下面的百分比只能当方向看，不能当统计结论。")

print()
print("=" * 76)
print("每次迭代实际动了几个内容字段")
print("=" * 76)
dist: dict[int, int] = {}
for item in refines:
    dist[len(item["changed"])] = dist.get(len(item["changed"]), 0) + 1
for n in sorted(dist):
    print(f"  动了 {n} 个字段：{dist[n]} 次")

multi = [i for i in refines if len(i["changed"]) >= 2]
print(f"\n  → 动了 ≥2 个字段的：{len(multi)}/{len(refines)} 次"
      f"（{100 * len(multi) / max(len(refines), 1):.0f}%）")

print()
print("=" * 76)
print("逐次明细：要求提到的字段 / 说明提到的字段 / 实际动的字段")
print("=" * 76)
for item in refines:
    req, note, changed = item["requirement"], item["note"], item["changed"]
    silent = [f for f in changed if f not in note]
    print(f"\n  {item['task']} 第 {item['no']} 版")
    print(f"    要求：{req[:60]}")
    print(f"    实际动了：{'、'.join(changed) or '（无）'}")
    if silent:
        print(f"    ⚠️ 改动说明里没点名、但实际动了的：{'、'.join(silent)}")

silent_total = sum(
    1 for i in refines if any(f not in i["note"] for f in i["changed"])
)
print()
print("=" * 76)
print("结论素材")
print("=" * 76)
print(f"  迭代总次数：{len(refines)}")
print(f"  动过 ≥2 个字段：{len(multi)} 次")
print(f"  说明里至少漏点一个字段名：{silent_total} 次")
print("  （后两个都是启发式：模型可以描述了改动而不点字段名，那会被算成「漏点」）")
