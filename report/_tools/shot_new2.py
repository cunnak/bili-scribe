import asyncio, json, base64, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9344
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
TITLE_KEY = "新闻周刊"

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

async def shot(ws, name):
    r = await cmd(ws, "Page.captureScreenshot", format="png", captureBeyondViewport=False)
    open(os.path.join(OUT, name + ".png"), "wb").write(base64.b64decode(r["data"]))
    print("saved", name)

async def click(ws, sel, wait=1.4):
    ok = await js(ws, f"(()=>{{const e=document.querySelector({sel!r}); if(e){{e.click(); return true}} return false}})()")
    await asyncio.sleep(wait)
    return ok

async def main():
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=200*1024*1024) as ws:
        await cmd(ws, "Page.enable"); await cmd(ws, "Runtime.enable")
        await cmd(ws, "Emulation.setDeviceMetricsOverride", width=1440, height=900,
                  deviceScaleFactor=2, mobile=False)
        await cmd(ws, "Page.navigate", url="http://127.0.0.1:8818/")
        await asyncio.sleep(5.0)
        t = await js(ws, "document.querySelector('#pv-title,.left h1')?.innerText || ''")
        print("loaded title:", t)
        if TITLE_KEY not in (t or ""):
            await click(ws, "#chip-history", 2.0)
            await js(ws, """(()=>{const it=document.querySelector('#hist-list2 .hi')||document.querySelector('#hist-list2>*'); if(it) it.click()})()""")
            await asyncio.sleep(2.0)
            await js(ws, """(()=>{const bs=[...document.querySelectorAll('button')];
              const t=bs.find(b=>b.innerText.includes('在工作区打开')); if(t) t.click()})()""")
            await asyncio.sleep(3.0)
        vc = await js(ws, "document.querySelectorAll('#ver-list .v, .ver-item').length")
        print("version nodes:", vc)
        # 01 工作区（顶部）
        await js(ws, "document.querySelector('.left') && (document.querySelector('.left').scrollTop=0)")
        await asyncio.sleep(1.2)
        await shot(ws, "n01-home")
        # 02 版本面板特写：右栏
        r = await js(ws, """(()=>{const p=document.querySelector('.right')||document.querySelector('#side');
          return p? {w:Math.round(p.getBoundingClientRect().width),x:Math.round(p.getBoundingClientRect().left)} : null})()""")
        print("right panel:", r)
        await click(ws, "#chip-history", 2.0)
        await js(ws, """(()=>{const it=document.querySelector('#hist-list2 .hi')||document.querySelector('#hist-list2>*'); if(it) it.click()})()""")
        await asyncio.sleep(2.0)
        await shot(ws, "n02-history")
        await click(ws, "#hist-pop-x", 1.0)
        await click(ws, "#chip-role", 1.6)
        await shot(ws, "n03-role")
        await click(ws, "#chip-role", 0.8)
        await click(ws, "#chip-settings", 2.2)
        await shot(ws, "n04-settings")
        await js(ws, "(()=>{const d=document.querySelector('.db'); if(d){d.scrollTop=d.scrollHeight} return 1})()")
        await asyncio.sleep(1.4)
        await shot(ws, "n07-settings-2")
        await click(ws, "#chip-settings", 1.2)
        # 05 发布区
        await js(ws, "(()=>{const l=document.querySelector('.left'); if(l) l.scrollTop=l.scrollHeight; return 1})()")
        await asyncio.sleep(2.0)
        await shot(ws, "n05-publish")
        # 06 正文中部（时间轴/素材局限）
        await js(ws, """(()=>{const l=document.querySelector('.left'); const h=[...document.querySelectorAll('.left h3,.left .sec')];
          const t=h.find(e=>e.innerText.includes('时间轴')); if(t&&l){l.scrollTop=t.offsetTop-140} return 1})()""")
        await asyncio.sleep(1.6)
        await shot(ws, "n06-body")
        print("done")

asyncio.run(main())
p.terminate()
