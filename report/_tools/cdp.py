import asyncio, json, base64, subprocess, sys, time, os, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"

def launch():
    prof = r"C:\Users\b1596\AppData\Local\Temp\cdp-prof"
    os.makedirs(prof, exist_ok=True)
    p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={prof}", "--window-size=1440,900", "--hide-scrollbars",
        "--force-device-scale-factor=2", "--no-first-run", "--no-default-browser-check",
        "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            return p
        except Exception:
            time.sleep(0.5)
    raise SystemExit("chrome did not start")

class S:
    def __init__(self, ws, mid=[0]):
        self.ws = ws; self.mid = mid
    async def cmd(self, method, **params):
        self.mid[0] += 1; i = self.mid[0]
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == i:
                if "error" in m: raise RuntimeError(method + ": " + str(m["error"]))
                return m.get("result", {})
    async def js(self, expr, awaitp=False):
        r = await self.cmd("Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                           returnByValue=True, userGesture=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"].get("exception", {}).get("description")))
        return r.get("result", {}).get("value")
    async def shot(self, name, clip=None):
        params = {"format": "png", "captureBeyondViewport": False}
        if clip: params["clip"] = clip
        r = await self.cmd("Page.captureScreenshot", **params)
        path = os.path.join(OUT, name + ".png")
        open(path, "wb").write(base64.b64decode(r["data"]))
        print("saved", name, len(r["data"]))

async def main(steps):
    p = launch()
    try:
        tgt = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/new?about:blank")) if False else None
        lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
        page = [t for t in lst if t["type"] == "page"][0]
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=200*1024*1024) as ws:
            s = S(ws)
            await s.cmd("Page.enable"); await s.cmd("Runtime.enable")
            await s.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                        deviceScaleFactor=2, mobile=False)
            await s.cmd("Page.navigate", url="http://127.0.0.1:8818/")
            await asyncio.sleep(3.5)
            src = open(steps, encoding="utf-8").read()
            g = {"s": s, "asyncio": asyncio, "os": os, "OUT": OUT}
            code = "async def _steps():\n" + "\n".join(
                ("    " + ln) if ln.strip() else "" for ln in src.splitlines())
            exec(compile(code, "steps", "exec"), g)
            await g["_steps"]()
    finally:
        p.terminate()

asyncio.run(main(sys.argv[1]))
