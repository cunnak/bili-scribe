import asyncio, json, base64, subprocess, os, time, urllib.request, sys
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

async def js(ws, expr, awaitp=False):
    r = await cmd(ws, "Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                  returnByValue=True, userGesture=True)
    if r.get("exceptionDetails"):
        raise RuntimeError(str(r["exceptionDetails"].get("exception", {}).get("description")))
    return r.get("result", {}).get("value")

async def shot(ws, name, clip=None):
    params = {"format": "png", "captureBeyondViewport": False}
    if clip: params["clip"] = clip
    r = await cmd(ws, "Page.captureScreenshot", **params)
    open(os.path.join(OUT, name + ".png"), "wb").write(base64.b64decode(r["data"]))
    print("saved", name)

async def main():
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=200*1024*1024) as ws:
        await cmd(ws, "Page.enable"); await cmd(ws, "Runtime.enable")
        await cmd(ws, "Emulation.setDeviceMetricsOverride", width=1440, height=900,
                  deviceScaleFactor=2, mobile=False)
        await cmd(ws, "Page.navigate", url="http://127.0.0.1:8818/")
        await asyncio.sleep(4.0)
        # 打开最新一条任务
        r = await js(ws, """(()=>{const b=document.querySelector('#chip-history'); if(b) b.click(); return !!b})()""")
        print("history opened", r)
        await asyncio.sleep(2.0)
        r = await js(ws, """(()=>{const it=document.querySelector('#hist-list2 .hi')||document.querySelector('#hist-list2>*');
          if(it) it.click(); return it? it.innerText.slice(0,120):'none'})()""")
        print("first item", r)
        await asyncio.sleep(2.0)
        await shot(ws, "n00-probe")
        # 若有"在工作区打开"按钮则点
        r = await js(ws, """(()=>{const bs=[...document.querySelectorAll('button')];
          const t=bs.find(b=>b.innerText.includes('在工作区打开')||b.innerText.includes('打开'));
          if(t){t.click(); return t.innerText} return 'no-open-btn'})()""")
        print("open", r)
        await asyncio.sleep(3.0)
        await shot(ws, "n01-workspace")
        title = await js(ws, "document.querySelector('.left h1, .left .title, #pv-title')?.innerText || document.title")
        print("title now", title)

asyncio.run(main())
p.terminate()
