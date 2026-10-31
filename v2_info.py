import urllib.request, json, io
B='http://127.0.0.1:8818'; TID='2c7ff315bcdb'
d=json.load(urllib.request.urlopen(f'{B}/api/task/{TID}/versions', timeout=60))
io.open('versions_list.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1))
print('count', len(d) if isinstance(d,list) else d)
v2=urllib.request.urlopen(f'{B}/api/task/{TID}/versions/2', timeout=60)
d2=json.load(v2)
io.open('v2.json','w',encoding='utf-8').write(json.dumps(d2,ensure_ascii=False,indent=1))
print('v2 keys', list(d2.keys()))
print('metrics', json.dumps(d2.get('metrics'),ensure_ascii=False))
diff=d2.get('diff') or {}
io.open('v2_diff.json','w',encoding='utf-8').write(json.dumps(diff,ensure_ascii=False,indent=1))
io.open('v2_note.txt','w',encoding='utf-8').write(str(d2.get('revise_note') or d2.get('note') or '')+'\n\n---SUMMARY---\n'+str(d2.get('preview_md') or '')[:2500])
