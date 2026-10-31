# -*- coding: utf-8 -*-
"""探 ignoredone 美术资源【子页】（点进去后的详情页）的设计语言"""
import asyncio, json, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9361
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
INDEX = "https://www.ignoredone.space/index.php/resource/"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs5", "--window-size=1440,1000",
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

        # 1. 进索引页，把所有链接捞出来
        await cmd("Page.navigate", url=INDEX)
        await asyncio.sleep(9)
        links = await cmd("Runtime.evaluate", returnByValue=True, expression=r"""(()=>{
          const out=[];
          document.querySelectorAll('a').forEach(a=>{
            const h=a.getAttribute('href')||''; const t=(a.innerText||'').trim().replace(/\s+/g,' ');
            if(!h||h.startsWith('#')) return;
            out.push({t:t.slice(0,40),h});
          });
          return JSON.stringify(out)})()""")
        L = json.loads(links["result"]["value"])
        cand = [x for x in L if "resource" in x["h"] and x["t"]]
        open(os.path.join(OUT, "rs_links.json"), "w", encoding="utf-8").write(json.dumps(cand, ensure_ascii=False, indent=1))
        print("links:", len(cand))
        for x in cand[:40]:
            print("  ", x["t"], "->", x["h"])
        # 2. 截图索引页底部（药丸区）
        h = await cmd("Runtime.evaluate", return_value=True, expression="document.body.scrollHeight")
        print("index H =", h["result"]["value"])
asyncio.run(main())
p.terminate()
