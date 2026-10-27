"""把「字段级 diff」真的跑给你看：用库里真实的两版做逐字段比对。

动机：`revise_note`（改动说明）是模型自己写的，实测两次与实际不符
（一次说"保持原样"却改了条数；一次要求"只改一个字段"，四个字段确实没动，
但它顺手改了第五个，说明里倒承认了）。既然自述不可靠，就直接比——
相邻两版的 summary_json 都在库里，逐字段比一遍是纯粹的本地计算，零成本。

只读真实库。输出的格式就是界面上该长成的样子。
"""

from __future__ import annotations

import difflib
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"

# 用户总结里，真正会被看到的内容字段（其余是元信息：model / role / frames / chunks）
CONTENT_FIELDS = ("one_line", "summary_md", "key_points", "timeline", "caution", "comment_short")
LABEL = {
    "one_line": "one_line（一句话结论）",
    "summary_md": "summary_md（正文）",
    "key_points": "key_points（要点）",
    "timeline": "timeline（时间轴）",
    "caution": "caution（局限说明）",
    "comment_short": "comment_short（短版）",
}

conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def versions_of(task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT version_no, source, requirement, summary_json FROM summary_version "
        "WHERE task_id = ? ORDER BY version_no",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["summary_json"] or "{}")
        except ValueError:
            payload = {}
        out.append({**dict(r), "json": payload})
    return out


def first_diff(a: str, b: str) -> str:
    """两段文字的第一处差异，截短给人看。界面上的"展开"就该显示这个。"""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "equal":
            old = (a[i1:i2] or "（空）")[:40]
            new = (b[j1:j2] or "（空）")[:40]
            return f"「{old}」→「{new}」"
    return ""


def compare(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """逐字段比。返回 (字段, 状态, 细节)。

    列表要单独处理：`key_points` 常常"项数没变、内容变了"（实测抓到过——
    模型说"其余字段保持原样"，实际 6 项里有 2 项被换掉了）。
    只报项数会漏掉这一类，所以要点出"有几项不同"，并给一个例子。
    """
    rows = []
    for field in CONTENT_FIELDS:
        a, b = old.get(field), new.get(field)
        if a == b:
            rows.append((field, "未变", ""))
            continue
        if isinstance(a, str) or isinstance(b, str):
            a, b = a or "", b or ""
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            detail = f"相似度 {ratio:.2f}，{len(a)} → {len(b)} 字"
            if ratio < 0.999:
                detail += f"，首处差异 {first_diff(a, b)}"
            rows.append((field, "改了", detail))
        else:
            a, b = a or [], b or []
            common = sum(1 for x, y in zip(a, b) if x == y)
            moved = max(len(a), len(b)) - common
            detail = f"{len(a)} 项 → {len(b)} 项，其中 {moved} 项内容不同"
            example = next((f"「{x[:36]}」→「{y[:36]}」" for x, y in zip(a, b) if x != y), "")
            if example:
                detail += f"，例：{example}"
            rows.append((field, "改了", detail))
    return rows


def show(task_id: str, title: str) -> None:
    vs = versions_of(task_id)
    if len(vs) < 2:
        print(f"（{task_id} 只有 {len(vs)} 版，跳过）")
        return
    print("=" * 78)
    print(f"{title}    任务 {task_id}，共 {len(vs)} 版")
    print("=" * 78)
    for old, new in zip(vs, vs[1:]):
        print(f"\n第 {old['version_no']} 版 → 第 {new['version_no']} 版"
              f"（{new['source']}）")
        print(f"  当时的要求：{new['requirement'] or '—'}")
        note = (new["json"].get("revise_note") or "").strip()
        print(f"  模型自己的说明：{note[:150] or '—'}")
        print("  ── 逐字段比对（本地算的，不是模型说的）──")
        touched, untouched = [], []
        for field, state, detail in compare(old["json"], new["json"]):
            mark = "改了" if state == "改了" else "未变"
            print(f"    {LABEL[field]:<26} {mark}   {detail}")
            (touched if state == "改了" else untouched).append(field)
        print(f"  → 实际动了 {len(touched)} 个字段：{'、'.join(touched) or '（无）'}")
        if note:
            # 模型提到过哪些字段名？没提到却动了的，就是"顺手改的"
            silent = [f for f in touched if f not in note]
            if silent:
                print(f"  ⚠️ 说明里没提到、但实际改了的字段：{'、'.join(silent)}"
                      "（它自己都没说，你只能靠比对发现）")


# 任务 id 走命令行。原版写死了两个本地库里的 id——那是某个人自己的观看记录，
# 对别人既没意义、也不该出现在公开仓库里。不给参数就自动挑库里版本最多的几条。
wanted = sys.argv[1:]
if wanted:
    for tid in wanted:
        show(tid, f"任务 {tid}")
else:
    rows = conn.execute(
        "SELECT task_id, COUNT(*) AS n FROM summary_version GROUP BY task_id"
        " HAVING n >= 2 ORDER BY n DESC LIMIT 3"
    ).fetchall()
    if not rows:
        print("库里没有迭代过两次以上的任务，先用真实任务跑一遍再来。")
    for tid, n in rows:
        show(tid, f"迭代最多的一条（{n} 版）")
        print()
