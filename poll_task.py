import urllib.request, json, io, time, sys
B='http://127.0.0.1:8818'; TID='2c7ff315bcdb'
for i in range(120):
    d=json.load(urllib.request.urlopen(f'{B}/api/task/{TID}', timeout=60))
    st=d.get('status'); prog=d.get('progress')
    print(i, st, prog, d.get('stage') or d.get('message') or '')
    if st in ('done','failed','error','succeeded'): 
        io.open('task_final.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1))
        print('FINAL saved')
        break
    time.sleep(5)
