import urllib.request, json, io, time
B='http://127.0.0.1:8818'
def post(p, obj):
    r=urllib.request.Request(B+p, data=json.dumps(obj).encode(), headers={'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(r, timeout=120))
def get(p):
    return json.load(urllib.request.urlopen(B+p, timeout=60))

t = post('/api/task', {'raw_input':'https://www.bilibili.com/video/BV1eAHG6LE4Y/','role':'wengao'})
io.open('task_tmp.json','w',encoding='utf-8').write(json.dumps(t,ensure_ascii=False,indent=1))
print('task id:', t.get('task_id') or t.get('id'))
print('status:', t.get('status'))
