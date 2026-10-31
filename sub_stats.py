import urllib.request, json, io, sqlite3, os
# 从数据库或缓存找字幕原文
db=os.path.expanduser('~/.bili-scribe/bili-scribe.db')
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
cols=[r[1] for r in c.execute("PRAGMA table_info(task)")]
print('task cols:', cols)
r=c.execute("select * from task where id='2c7ff315bcdb'").fetchone()
d=dict(r)
for k,v in d.items():
    sv=str(v) if v is not None else ''
    print(k, '->', len(sv), repr(sv[:80]))
