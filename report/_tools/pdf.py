import asyncio, json, base64, subprocess, sys, time, os, urllib.request
import websockets
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9344
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\实验报告.pdf"
URL = "file:///C:/Users/b1596/Desktop/%E8%A7%86%E9%A2%91%E5%86%85%E5%AE%B9%E6%80%BB%E7%BB%93/report/%E5%AE%9E%E9%AA%8C%E6%8A%A5%E5%91%8A.html"
prof = r"C:\Users\b1596\AppData\Local\Temp\cdp-pdf"
os.makedirs(prof, exist_ok=True)
p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1); break
    except Exception: time.sleep(0.5)

async def go():
    lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
    page = [t for t in lst if t["type"] == "page"][0]
    mid = [0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=400*1024*1024) as ws:
        async def cmd(method, **params):
            mid[0] += 1; i = mid[0]
            await ws.send(json.dumps({"id": i, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == i:
                    if "error" in m: raise RuntimeError(str(m["error"]))
                    return m.get("result", {})
        await cmd("Page.enable")
        await cmd("Page.navigate", url=URL)
        await asyncio.sleep(4)
        await cmd("Runtime.evaluate", expression="document.fonts.ready.then(()=>1)", awaitPromise=True)
        await asyncio.sleep(1.5)
        r = await cmd("Page.printToPDF", printBackground=True, paperWidth=8.27, paperHeight=11.69,
                      marginTop=0, marginBottom=0, marginLeft=0, marginRight=0,
                      preferCSSPageSize=True, displayHeaderFooter=True,
                      headerTemplate='<div style="width:100%;font-family:monospace;font-size:7px;color:#6f746f;padding:0 13mm;text-align:right">第 8 组 &nbsp;/&nbsp; bili-scribe · experimental report</div>',
                      footerTemplate='<div style="width:100%;box-sizing:border-box;padding:0 13mm 7mm;font-family:monospace;font-size:7px;color:#6f746f;display:flex;justify-content:space-between"><span>第 8 组 · bili-scribe · 视频内容总结 · 实验报告</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
        open(OUT, "wb").write(base64.b64decode(r["data"]))
        print("pdf bytes", os.path.getsize(OUT))
asyncio.run(go())
p.terminate()
