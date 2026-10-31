# -*- coding: utf-8 -*-
"""取药丸按钮的 href（美术资源各自的子页入口）"""
import asyncio, json, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9362
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
INDEX = "https://www.ignoredone.space/index.php/resource/"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs6", "--window-size=1440,1000",
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
        await cmd("Page.navigate", url=INDEX)
        await asyncio.sleep(9)
        r = await cmd("Runtime.evaluate", returnByValue=True, expression=r"""(()=>{
          const out=[];
          document.querySelectorAll('.wp-block-button__link, a.wp-block-button__link, .card a, .card-cover-btn').forEach(a=>{
            out.push({t:(a.innerText||'').trim().slice(0,30), h:a.getAttribute('href')||'', tag:a.tagName, cls:(a.className||'').toString().slice(0,60)});
          });
          return JSON.stringify(out)})()""")
        L = json.loads(r["result"]["value"])
        open(os.path.join(OUT, "rs_links.json"), "w", encoding="utf-8").write(json.dumps(L, ensure_ascii=False, indent=1))
        print("count:", len(L))
        for x in L[:30]:
            print(json.dumps(x, ensure_ascii=False))
asyncio.run(main())
p.terminate()
