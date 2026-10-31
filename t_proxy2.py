import urllib.request, json, io, re, time
cfg = io.open('config.yaml',encoding='utf-8').read()
key = re.search(r'api_key:\s*"([^"]+)"', cfg).group(1)
base = re.search(r'base_url:\s*"([^"]+)"', cfg).group(1)
model = re.search(r'model:\s*"([^"]+)"', cfg).group(1)
body = json.dumps({"model":model,"messages":[{"role":"user","content":"say OK"}],"max_tokens":10}).encode()
op = urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7897','http':'http://127.0.0.1:7897'}))
for i in range(4):
    t0=time.time()
    try:
        r = op.open(urllib.request.Request(base.rstrip('/')+'/chat/completions', data=body,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+key}), timeout=90)
        d=json.load(r); print(i,'OK', round(time.time()-t0,1),'s', d['choices'][0]['message']['content'][:30])
        break
    except Exception as e:
        print(i,'ERR', round(time.time()-t0,1),'s', type(e).__name__, str(e)[:90])
        time.sleep(3)
