import urllib.request, json, io, ssl
cfg = io.open('config.yaml',encoding='utf-8').read()
import re
key = re.search(r'api_key:\s*"([^"]+)"', cfg).group(1)
base = re.search(r'base_url:\s*"([^"]+)"', cfg).group(1)
model = re.search(r'model:\s*"([^"]+)"', cfg).group(1)
print('base', base, 'model', model)
body = json.dumps({"model":model,"messages":[{"role":"user","content":"ping, reply with OK only"}],"max_tokens":10}).encode()
for tag, handler in [('direct', urllib.request.ProxyHandler({})),
                     ('proxy7897', urllib.request.ProxyHandler({'https':'http://127.0.0.1:7897','http':'http://127.0.0.1:7897'}))]:
    try:
        op = urllib.request.build_opener(handler)
        r = urllib.request.Request(base.rstrip('/')+'/chat/completions', data=body,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
        resp = op.open(r, timeout=45)
        d = json.load(resp)
        print(tag, '-> OK', d['choices'][0]['message']['content'][:40])
    except Exception as e:
        print(tag, '-> ERR', type(e).__name__, str(e)[:120])
