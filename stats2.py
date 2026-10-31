import sqlite3, json, io, os, statistics as st
db=os.path.expanduser('~/.bili-scribe/bili-scribe.db')
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
q=lambda s,*a: [dict(r) for r in c.execute(s,a)]
out={}
out['tasks']=q("select count(*) n from task")[0]['n']
out['by_status']=q("select status,count(*) n from task group by status")
out['videos']=q("select count(distinct bvid) n from task where bvid is not null")[0]['n']
out['source']=q("select source_kind,count(*) n from task group by source_kind")
out['versions']=q("select count(*) n from summary_version")[0]['n']
out['ver_role']=q("select role,count(*) n from summary_version group by role")
out['pub']=q("select status,count(*) n from publish group by status")
out['pub_cols']=[r[1] for r in c.execute("PRAGMA table_info(publish)")]
# token totals from version metrics
tot={'prompt':0,'completion':0,'total':0,'calls':0}; el=[]
for r in c.execute("select metrics from summary_version"):
    m=json.loads(r[0]) if r[0] else {}
    u=m.get('usage') or {}
    tot['prompt']+=u.get('prompt_tokens',0); tot['completion']+=u.get('completion_tokens',0)
    tot['total']+=u.get('total_tokens',0); tot['calls']+=u.get('calls',0)
    if m.get('elapsed'): el.append(m['elapsed'])
out['tokens']=tot
out['elapsed']={'n':len(el),'median':round(st.median(el),1) if el else None,
   'min':round(min(el),1) if el else None,'max':round(max(el),1) if el else None}
out['date_range']=q("select min(created_at) a, max(created_at) b from task")[0]
io.open('stats2.json','w',encoding='utf-8').write(json.dumps(out,ensure_ascii=False,indent=1,default=str))
print('ok')
