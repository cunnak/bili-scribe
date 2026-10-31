import asyncio, json, base64, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9344
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-new", "--window-size=1440,900",
    "--hide-scrollbars", "--force-device-scale-factor=2", "--no-first-run",
    "--no-default-browser-check", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(80):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1); break
    except Exception: time.sleep(0.5)

lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
page = [t for t in lst if t["type"] == "page"][0]
mid = [0]

async def cmd(ws, m, **kw):
    mid[0] += 1; i = mid[0]
    await ws.send(json.dumps({"id": i, "method": m, "params": kw}))
    while True:
        r = json.loads(await ws.recv())
        if r.get("id") == i:
            if "error" in r: raise RuntimeError(m + ": " + str(r["error"]))
            return r.get("result", {})

async def js(ws, expr):
    r = await cmd(ws, "Runtime.evaluate", expression=expr, returnByValue=True, userGesture=True)
    if r.get("exceptionDetails"):
        raise RuntimeError(str(r["exceptionDetails"].get("exception", {}).get("description")))
    return r.get("result", {}).get("value")

async def shot(ws, name):
    r = await cmd(ws, "Page.captureScreenshot", format="png", captureBeyondViewport=False)
    open(os.path.join(OUT, name + ".png"), "wb").write(base64.b64decode(r["data"]))
    print("saved", name)

async def main():
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=200*1024*1024) as ws:
        await cmd(ws, "Page.enable"); await cmd(ws, "Runtime.enable")
        await cmd(ws, "Emulation.setDeviceMetricsOverride", width=1440, height=900,
                  deviceScaleFactor=2, mobile=False)
        await cmd(ws, "Page.navigate", url="http://127.0.0.1:8818/")
        await asyncio.sleep(5.0)
        # 找到发布区，滚动到那里
        info = await js(ws, """(()=>{
          const l=document.querySelector('.left');
          const anchors=[...document.querySelectorAll('.left *')].filter(e=>e.children.length===0 && e.innerText && e.innerText.trim().length<30);
          const t=anchors.find(e=>/将发到评论区|发布区|确认/.test(e.innerText));
          const pub=document.querySelector('#publish-btn');
          return {found: t? t.innerText.slice(0,20): 'none', hasPub: !!pub,
                  sh: l? l.scrollHeight : 0, ch: l? l.clientHeight : 0};
        })()""")
        print("info", info)
        # 滚到底部找发布块
        r = await js(ws, """(()=>{const l=document.querySelector('.left'); if(!l) return 'no-left';
          l.scrollTop = l.scrollHeight; return l.scrollTop})()""")
        print("scrollTop", r)
        await asyncio.sleep(2.0)
        await shot(ws, "n05-publish")
        # 再往上滚一屏，捕捉时间轴 + 素材局限 + 发布区开头
        r = await js(ws, """(()=>{const l=document.querySelector('.left'); l.scrollTop = l.scrollHeight - l.clientHeight*0.95; return l.scrollTop})()""")
        await asyncio.sleep(1.8)
        await shot(ws, "n06-body")
        print("done")

asyncio.run(main())
p.terminate()
