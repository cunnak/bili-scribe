import sqlite3, json, io, os
db = os.path.expanduser('~/.bili-scribe/bili-scribe.db')
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
tid='2c7ff315bcdb'
out={}
for t in ('task','summary_version','publish','task_note','note'):
    try:
        cols=[r[1] for r in c.execute(f'PRAGMA table_info({t})')]
        out[t+'_cols']=cols
    except Exception as e:
        out[t+'_cols']='ERR '+str(e)
row = c.execute('SELECT * FROM task WHERE id=?',(tid,)).fetchone()
out['task']=dict(row) if row else None
vs = c.execute('SELECT * FROM summary_version WHERE task_id=? ORDER BY version_no',(tid,)).fetchall()
out['versions']=[dict(v) for v in vs]
io.open('db_task.json','w',encoding='utf-8').write(json.dumps(out,ensure_ascii=False,indent=1,default=str))
print('tables keys', list(out.keys()))
print('versions', len(out['versions']))
