import sqlite3, json, io

DB = r"C:\Users\b1596\.bili-scribe\bili-scribe.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()
out = []
def q(sql, *a): return cur.execute(sql, a).fetchall()

# ---- 新任务 ----
r = dict(q("select * from task where id=?", "2c7ff315bcdb")[0])
out.append("## TASK 2c7ff315bcdb")
for k in ["id","bvid","aid","cid","page","title","up_name","status","source_kind","source_label",
          "source_trust","created_at","updated_at","progress","error"]:
    out.append(f"{k}: {r.get(k)}")
out.append("warnings: " + str(r.get("warnings")))
sj = json.loads(r.get("summary_json") or "{}")
out.append("summary_json keys: " + str(list(sj.keys())))
out.append("chunks: " + str(sj.get("chunks")) + " frames: " + str(sj.get("frames")) + " role: " + str(sj.get("role")))
out.append("preview_md len: " + str(len(r.get("preview_md") or "")))
out.append("comment_text len: " + str(len(r.get("comment_text") or "")))
tm = sj.get("timeline") or []
out.append("timeline items: " + str(len(tm)))
kp = sj.get("key_points") or []
out.append("key_points: " + str(len(kp)))
out.append("summary_md len: " + str(len(sj.get("summary_md") or "")))

# ---- 版本 ----
out.append("\n## VERSIONS of new task")
for v in q("select version_no,source,requirement,role,metrics,created_at from summary_version where task_id=? order by version_no","2c7ff315bcdb"):
    m = json.loads(v["metrics"] or "{}")
    u = m.get("usage") or {}
    out.append(f"v{v['version_no']} src={v['source']} role={v['role']} req={v['requirement']!r} "
               f"calls={u.get('calls')} in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
               f"elapsed={m.get('elapsed')} chunks={m.get('chunks')} frames={m.get('frames')} at={v['created_at']}")

# ---- 全局 ----
out.append("\n## GLOBAL")
out.append("task total: %d" % q("select count(*) c from task")[0]["c"])
for row in q("select status,count(*) c from task group by status order by c desc"):
    out.append(f"  status {row['status']}: {row['c']}")
out.append("distinct bvid: %d" % q("select count(distinct bvid) c from task")[0]["c"])
out.append("versions: %d" % q("select count(*) c from summary_version")[0]["c"])
out.append("publish rows: %d" % q("select count(*) c from publish")[0]["c"])
for row in q("select status,count(*) c from publish group by status"):
    out.append(f"  publish {row['status']}: {row['c']}")
for row in q("select source_kind,count(*) c from task group by source_kind order by c desc"):
    out.append(f"  src {row['source_kind']}: {row['c']}")
for row in q("select role,count(*) c from summary_version group by role order by c desc"):
    out.append(f"  role {row['role']}: {row['c']}")
r = q("select min(created_at) a,max(created_at) b from task")[0]
out.append(f"date range: {r['a']} .. {r['b']}")

tot_in=tot_out=0; calls=0; el=[]
for v in q("select metrics from summary_version"):
    try: m=json.loads(v["metrics"] or "{}")
    except: continue
    u=m.get("usage") or {}
    tot_in+=u.get("prompt_tokens",0) or 0
    tot_out+=u.get("completion_tokens",0) or 0
    calls+=u.get("calls",0) or 0
    e=m.get("elapsed")
    if isinstance(e,(int,float)): el.append(e)
el.sort()
out.append(f"tokens in={tot_in:,} out={tot_out:,} total={tot_in+tot_out:,} calls={calls}")
if el:
    n=len(el)
    out.append(f"elapsed n={n} median={el[n//2]:.1f}s min={el[0]:.1f} max={el[-1]:.1f} p25={el[n//4]:.1f} p75={el[n*3//4]:.1f}")

io.open("db_stats.txt","w",encoding="utf-8").write("\n".join(out))
print("ok", len(out))
