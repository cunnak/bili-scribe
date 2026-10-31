# -*- coding: utf-8 -*-
"""看 card 内部结构与是否存在站内子页；并截取一张卡片的详情"""
import asyncio, json, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9363
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
INDEX = "https://www.ignoredone.space/index.php/resource/"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs7", "--window-size=1440,1000",
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
        async def shot(path, full=False):
            r = await cmd("Page.captureScreenshot", format="png",
                          captureBeyondViewport=full,
                          clip={"x": 0, "y": 0, "width": 1440, "height": 1000, "scale": 1} if not full else None)
            import base64
            open(path, "wb").write(base64.b64decode(r["data"]))

        await cmd("Page.enable"); await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
        await cmd("Page.navigate", url=INDEX)
        await asyncio.sleep(9)

        # 一张 card 的完整 outerHTML 结构（只看前 3000 字）
        r = await cmd("Runtime.evaluate", returnByValue=True, expression=r"""(()=>{
          const c=document.querySelector('.card');
          if(!c) return 'NO CARD';
          const t=c.outerHTML.replace(/>\s+</g,'><');
          // 找所有内部 a href
          const as=[...c.querySelectorAll('a')].map(a=>({h:a.getAttribute('href'),t:(a.innerText||'').trim().slice(0,20)}));
          return JSON.stringify({len:t.length, head:t.slice(0,2200), as})})()""")
        open(os.path.join(OUT, "rs_card.json"), "w", encoding="utf-8").write(r["result"]["value"])
        print("card saved")

        # 站内所有 /resource/ 开头的 href（含子页）
        r2 = await cmd("Runtime.evaluate", returnByValue=True, expression=r"""(()=>{
          const s=new Set();
          document.querySelectorAll('a').forEach(a=>{const h=a.getAttribute('href')||'';
            if(h.includes('/resource/')) s.add(h);});
          return JSON.stringify([...s])})()""")
        open(os.path.join(OUT, "rs_subpages.json"), "w", encoding="utf-8").write(r2["result"]["value"])
        print("subpages saved")

        # 截一张卡片区域特写：滚到第一个 card 位置
        await cmd("Runtime.evaluate", expression="window.scrollTo(0,900)")
        await asyncio.sleep(1.2)
        await shot(os.path.join(OUT, "rs_card.png"))
        print("card shot ok")
asyncio.run(main())
p.terminate()
