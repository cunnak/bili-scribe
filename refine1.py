import urllib.request, json, io, time
B='http://127.0.0.1:8818'; TID='2c7ff315bcdb'
req = {
 'requirement':'第二个小标题「AI造假维权之困与法律博弈」现在有三段，读起来太密。把这段压成两段，法律条文那半段只留最高法的两条明确规则（可识别标准、惩罚性赔偿），其余展开删掉。另外时间轴现在只有六条，从 25:29 直接跳到 26:33，补一条萨维在伦敦马拉松破二的那个瞬间。',
 'base_version':1
}
r = urllib.request.Request(B+f'/api/task/{TID}/refine', data=json.dumps(req).encode(),
    headers={'Content-Type':'application/json'})
try:
    d = json.load(urllib.request.urlopen(r, timeout=600))
    io.open('refine1.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1))
    print('ok', {k:d.get(k) for k in ('version_no','status','ok')})
except Exception as e:
    print('ERR', type(e).__name__, str(e)[:400])
    try:
        print(e.read().decode('utf-8','ignore')[:600])
    except Exception: pass
