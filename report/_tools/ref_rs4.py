# -*- coding: utf-8 -*-
"""抓 ignoredone 资源页的 @font-face 真实地址与字重"""
import asyncio, json, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9359
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
URL = "https://www.ignoredone.space/index.php/resource/"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs4", "--window-size=1440,1000",
    "--hide-scrollbars", "--no-proxy-server",
    r"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "--no-first-run", "--no-default-browser-check", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1); break
    except Exception:
        time.sleep(0.5)

JS = r"""(()=>{
const faces=[];
for(const ss of document.styleSheets){
  let rules; try{rules=ss.cssRules}catch(e){continue}
  if(!rules) continue;
  for(const r of rules){
    if(r.type===5){ // FONT_FACE_RULE
      faces.push({family:r.style.getPropertyValue('font-family'),
        src:r.style.getPropertyValue('src'),
        weight:r.style.getPropertyValue('font-weight'),
        style:r.style.getPropertyValue('font-style'),
        display:r.style.getPropertyValue('font-display'),
        unicode:r.style.getPropertyValue('unicode-range').slice(0,80),
        href:ss.href});
    }
  }
}
// 也看看 hero 大字的 background-clip
const h=document.querySelector('h1, .hero, [class*=hero], [class*=title]');
const heroInfo=h?{cls:(h.className||'').toString(),txt:(h.innerText||'').slice(0,40),
  bgc:getComputedStyle(h).backgroundClip, bgi:getComputedStyle(h).backgroundImage.slice(0,160),
  fs:getComputedStyle(h).fontSize,fw:getComputedStyle(h).fontWeight,ls:getComputedStyle(h).letterSpacing,
  ff:getComputedStyle(h).fontFamily}:null;
// 网格线：伪元素或 1px 元素
const grid=[...document.querySelectorAll('*')].filter(e=>{
  const c=getComputedStyle(e); const b=e.getBoundingClientRect();
  const isThin=(b.width<=2&&b.height>200)||(b.height<=2&&b.width>200);
  return isThin && (c.backgroundColor!=='rgba(0, 0, 0, 0)'||parseFloat(c.borderLeftWidth)>0||parseFloat(c.borderTopWidth)>0);
}).slice(0,20).map(e=>({tag:e.tagName,cls:(e.className||'').toString().slice(0,50),
  w:Math.round(e.getBoundingClientRect().width),h:Math.round(e.getBoundingClientRect().height),
  bg:getComputedStyle(e).backgroundColor,bdL:getComputedStyle(e).borderLeft,
  bdT:getComputedStyle(e).borderTop}));
return JSON.stringify({faces,heroInfo,grid})})()"""

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
        v = await cmd("Runtime.evaluate", expression=JS, returnByValue=True)
        open(os.path.join(OUT, "rs_fonts.json"), "w", encoding="utf-8").write(str(v["result"]["value"]))
        print("saved")
asyncio.run(go())
p.terminate()
