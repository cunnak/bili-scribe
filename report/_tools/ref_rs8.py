# -*- coding: utf-8 -*-
"""看一张美术资源的「子页」（postimg 图集）到底是什么版式"""
import asyncio, json, subprocess, os, time, urllib.request, base64
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9364
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
URL = "https://postimg.cc/gallery/SCWBf0Y"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs8", "--window-size=1440,1000",
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
                    if "error" in r: raise RuntimeError(str(r["error"]))
                    return r.get("result", {})
        await cmd("Page.enable"); await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await cmd("Page.navigate", url=URL)
        await asyncio.sleep(12)
        info = await cmd("Runtime.evaluate", returnByValue=True, expression=r"""(()=>{
          const imgs=[...document.querySelectorAll('img')].map(i=>({src:(i.currentSrc||i.src||'').slice(0,90),w:i.naturalWidth,h:i.naturalHeight}));
          return JSON.stringify({title:document.title,url:location.href,
            imgs:imgs.filter(x=>x.w>80).slice(0,24), n:imgs.length,
            bodyBg:getComputedStyle(document.body).backgroundColor})})()""")
        open(os.path.join(OUT, "gallery.json"), "w", encoding="utf-8").write(info["result"]["value"])
        r = await cmd("Page.captureScreenshot", format="png")
        open(os.path.join(OUT, "gal1.png"), "wb").write(base64.b64decode(r["data"]))
        print("ok")
asyncio.run(main())
p.terminate()
