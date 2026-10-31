import asyncio, json, base64, subprocess, os, time, urllib.request
import websockets
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9355
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
os.makedirs(OUT, exist_ok=True)

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-ref", "--window-size=1440,1000",
    "--hide-scrollbars", "--no-proxy-server", r"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "--no-first-run", "--no-default-browser-check", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1); break
    except Exception: time.sleep(0.5)

async def go():
    lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
    page = [t for t in lst if t["type"] == "page"][0]
    mid=[0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=300*1024*1024) as ws:
        async def cmd(m, **kw):
            mid[0]+=1; i=mid[0]
            await ws.send(json.dumps({"id":i,"method":m,"params":kw}))
            while True:
                r=json.loads(await ws.recv())
                if r.get("id")==i:
                    if "error" in r: raise RuntimeError(str(r["error"]))
                    return r.get("result",{})
        await cmd("Page.enable"); await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await cmd("Page.navigate", url="https://www.ignoredone.space/")
        await asyncio.sleep(9)
        # 滚到中间模块再截
        for y,name in [(0,"ig1"),(1000,"ig2"),(2000,"ig3"),(3200,"ig4")]:
            await cmd("Runtime.evaluate", expression=f"window.scrollTo(0,{y})")
            await asyncio.sleep(1.6)
            r = await cmd("Page.captureScreenshot", format="png")
            open(os.path.join(OUT,name+".png"),"wb").write(base64.b64decode(r["data"]))
            print("saved",name)
        # 抓计算样式
        js = """(()=>{const g=e=>{const c=getComputedStyle(e);return{bg:c.backgroundColor,color:c.color,ff:c.fontFamily,fs:c.fontSize,fw:c.fontWeight,ls:c.letterSpacing,lh:c.lineHeight,br:c.borderRadius,sh:c.boxShadow}};
const out={};
out.body=g(document.body);
out.h1=g(document.querySelector('h1')||document.body);
out.h2=g(document.querySelector('h2')||document.body);
const a=document.querySelector('a'); out.a=g(a||document.body);
const btns=[...document.querySelectorAll('a,button')].filter(e=>getComputedStyle(e).borderRadius!=='0px').slice(0,4).map(g);
out.btns=btns;
out.fonts=[...new Set([...document.querySelectorAll('*')].slice(0,3000).map(e=>getComputedStyle(e).fontFamily))].slice(0,8);
return JSON.stringify(out)})()"""
        v = await cmd("Runtime.evaluate", expression=js, returnByValue=True)
        open(os.path.join(OUT,"ig_style.json"),"w",encoding="utf-8").write(str(v["result"]["value"]))
        print("style saved")
asyncio.run(go()); p.terminate()
