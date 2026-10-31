import sqlite3, json, io, difflib, re

DB = r"C:\Users\b1596\.bili-scribe\bili-scribe.db"
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
cur = con.cursor()
out = []

rows = cur.execute("select version_no,summary_json,preview_md,comment_text from summary_version "
                   "where task_id=? order by version_no", ("2c7ff315bcdb",)).fetchall()

def norm(s):
    return re.sub(r"\s+", "", s or "")

def cjk(s):
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))

sj1 = json.loads(rows[0]["summary_json"]); sj2 = json.loads(rows[1]["summary_json"])

def sim(a, b):
    a, b = norm(a), norm(b)
    if not a and not b: return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)

out.append("## FIELD-LEVEL DIFF v1 -> v2")
for key, label in [("summary_md","正文"),("one_line","一句话"),("comment_short","短版"),("preview_md","预览全文")]:
    a = rows[0][key] if key == "preview_md" else sj1.get(key)
    b = rows[1][key] if key == "preview_md" else sj2.get(key)
    out.append(f"{label}: 字数 {cjk(a)} -> {cjk(b)} · 字符 {len(a or '')} -> {len(b or '')} · 相似度 {sim(a,b)}")

kp1, kp2 = sj1.get("key_points") or [], sj2.get("key_points") or []
same = sum(1 for x in kp1 if x in kp2)
out.append(f"要点: {len(kp1)} 项 -> {len(kp2)} 项 · 完全相同 {same} 项 · 内容不同 {max(len(kp1),len(kp2))-same} 项")

tl1, tl2 = sj1.get("timeline") or [], sj2.get("timeline") or []
out.append(f"时间轴: {len(tl1)} 条 -> {len(tl2)} 条")
out.append("  v1 时间点: " + ", ".join(str(x.get("t")) for x in tl1))
out.append("  v2 时间点: " + ", ".join(str(x.get("t")) for x in tl2))
new = [x for x in tl2 if x not in tl1]
out.append(f"  v2 新增: {json.dumps(new, ensure_ascii=False)}")

out.append(f"\nrevise_note(v2): {sj2.get('revise_note')!r}")
out.append(f"comment_text v1 len={len(rows[0]['comment_text'] or '')} v2 len={len(rows[1]['comment_text'] or '')}")

# 字幕统计：从 task 原始数据没有字幕；改从采集日志/接口。这里先输出 preview 结构
out.append("\n## PREVIEW STRUCTURE v2")
pm = rows[1]["preview_md"] or ""
for ln in pm.split("\n"):
    if ln.startswith("#") or ln.startswith("**") or ln.startswith(">"):
        out.append(ln[:150])

io.open("diff_stats.txt","w",encoding="utf-8").write("\n".join(out))
print("ok")
