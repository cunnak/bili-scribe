# -*- coding: utf-8 -*-
"""定点截图：抓 ignoredone 资源页的文字/目录区（y 38000-50000）"""
import asyncio, json, base64, subprocess, os, time, urllib.request, sys
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9358
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
URL = "https://www.ignoredone.space/index.php/resource/"
Y0 = int(sys.argv[1]) if len(sys.argv) > 1 else 38000
N = int(sys.argv[2]) if len(sys.argv) > 2 else 4
TAG = sys.argv[3] if len(sys.argv) > 3 else "rt"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rt", "--window-size=1440,1000",
    "--hide-scrollbars", "--no-proxy-server",
    r"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "--no-first-run", "--no-default-browser-check", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1); break
    except Exception:
        time.sleep(0.5)

async def go():
    lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
    page = [t for t in lst if t["type"] == "page"][0]
    mid = [0]
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=300 * 1024 * 1024) as ws:
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
        await asyncio.sleep(10)
        for y in (5000, 15000, 30000):
            await cmd("Runtime.evaluate", expression=f"window.scrollTo(0,{y})")
            await asyncio.sleep(1.0)
        for k in range(N):
            y = Y0 + k * 1000
            await cmd("Runtime.evaluate", expression=f"window.scrollTo(0,{y})")
            await asyncio.sleep(1.4)
            r = await cmd("Page.captureScreenshot", format="png")
            name = f"{TAG}{k+1:02d}"
            open(os.path.join(OUT, name + ".png"), "wb").write(base64.b64decode(r["data"]))
            print("saved", name, "y=", y)
asyncio.run(go())
p.terminate()
