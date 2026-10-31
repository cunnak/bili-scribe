import urllib.request, json, io
B='http://127.0.0.1:8818'; TID='2c7ff315bcdb'
d=json.load(urllib.request.urlopen(f'{B}/api/task/{TID}', timeout=90))
io.open('task_final.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1))
print('status', d.get('status'), 'versions', d.get('version_count'), 'title', d.get('title'))
print('source', d.get('source_kind'), d.get('source_label'), d.get('source_trust'))
print('warnings', d.get('warnings'))
print('progress', d.get('progress'))
