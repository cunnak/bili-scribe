import urllib.request, json, io, time
B='http://127.0.0.1:8818'; TID='2c7ff315bcdb'
for i in range(150):
    d=json.load(urllib.request.urlopen(f'{B}/api/task/{TID}', timeout=60))
    st=d.get('status'); vc=d.get('version_count')
    print(i, st, 'v', vc, 'refining', d.get('refining'))
    if st in ('ready','failed') and not d.get('refining') and vc and vc>=2:
        io.open('task_v2.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1))
        print('DONE v', vc); break
    if st=='failed':
        io.open('task_v2.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1)); print('FAILED', d.get('error')); break
    time.sleep(5)
