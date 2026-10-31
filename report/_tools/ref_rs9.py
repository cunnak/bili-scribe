# -*- coding: utf-8 -*-
"""探美术资源【子页】= postimg 图集页：版式、网格、取色"""
import asyncio, json, subprocess, os, time, urllib.request, base64
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9366
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
URL = "https://postimg.cc/gallery/SCWBf0Y"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs9", "--window-size=1440,1000",
    "--hide-scrollbars", "--no-proxy-server",
    r"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "--no-first-run", "--no-default-browser-check", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1); break
    except Exception:
        time.sleep(0.5)

async def main():
    lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
    page = [t for t in lst if t["type"] == "page"][0]
    mid = [0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=400 * 1024 * 1024) as ws:
        async def cmd(m, **kw):
            mid[0] += 1; i = mid[0]
            await ws.send(json.dumps({"id": i, "method": m, "params": kw}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == i:
                    if "error" in r: raise RuntimeError(json.dumps(r["error"]))
                    return r.get("result", {})
        async def ev(expr):
            r = await cmd("Runtime.evaluate", expression=expr, returnByValue=True)
            if "exceptionDetails" in r:
                return {"__err": json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400]}
            return r.get("result", {}).get("value")
        async def shot(name):
            r = await cmd("Page.captureScreenshot", format="png")
            open(os.path.join(OUT, name), "wb").write(base64.b64decode(r["data"]))

        await cmd("Page.enable"); await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await cmd("Page.navigate", url=URL)
        await asyncio.sleep(14)
        await shot("gal1.png")
        info = await ev(r"""(()=>{
          const body=document.body, cs=getComputedStyle(body);
          const imgs=[...document.querySelectorAll('img')].map(i=>({src:(i.currentSrc||i.src||'').slice(0,100),w:i.naturalWidth,h:i.naturalHeight}));
          const big=imgs.filter(x=>x.w>=200&&x.h>=200);
          // 找网格容器
          const grids=[...document.querySelectorAll('*')].filter(e=>{
            const c=getComputedStyle(e); return c.display.includes('grid')||c.display.includes('flex');
          }).slice(0,12).map(e=>({tag:e.tagName,cls:(e.className||'').toString().slice(0,60),
            disp:getComputedStyle(e).display,cols:getComputedStyle(e).gridTemplateColumns,
            gap:getComputedStyle(e).gap, n:e.children.length}));
          return JSON.stringify({title:document.title,url:location.href,
            bg:cs.backgroundColor, color:cs.color, ff:cs.fontFamily, fs:cs.fontSize,
            H:document.body.scrollHeight, nImg:imgs.length, big:big.slice(0,30), grids})})()""")
        open(os.path.join(OUT, "gallery.json"), "w", encoding="utf-8").write(str(info))
        print("ok", (info or "")[:200])
asyncio.run(main())
p.terminate()
