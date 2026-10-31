import urllib.request, json, io
bv='BV1eAHG6LE4Y'
url=f'https://api.bilibili.com/x/web-interface/view?bvid={bv}'
req=urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'})
d=json.load(urllib.request.urlopen(req, timeout=20))
v=d['data']
out={'bvid':v['bvid'],'title':v['title'],'owner':v['owner']['name'],'duration':v['duration'],
     'pubdate':v['pubdate'],'view':v['stat']['view'],'reply':v['stat']['reply'],'desc':v['desc'][:400],
     'pages':[{'p':p['page'],'part':p['part'],'duration':p['duration']} for p in v['pages']]}
io.open('vinfo_tmp.json','w',encoding='utf-8').write(json.dumps(out,ensure_ascii=False,indent=1))
print('ok', out['duration'], len(out['pages']), out['title'])
