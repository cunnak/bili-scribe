# -*- coding: utf-8 -*-
"""抓 ignoredone 美术资源页：分屏截图 + 计算样式 + 取色"""
import asyncio, json, base64, subprocess, os, time, urllib.request, sys
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9356
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.ignoredone.space/index.php/resource/"
TAG = sys.argv[2] if len(sys.argv) > 2 else "rs"
os.makedirs(OUT, exist_ok=True)

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs", "--window-size=1440,1000",
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

        v = await cmd("Runtime.evaluate", expression="document.documentElement.scrollHeight", returnByValue=True)
        H = int(v["result"]["value"])
        print("docH", H)

        ys = list(range(0, min(H, 9000), 900))
        for idx, y in enumerate(ys):
            await cmd("Runtime.evaluate", expression=f"window.scrollTo(0,{y})")
            await asyncio.sleep(1.5)
            r = await cmd("Page.captureScreenshot", format="png")
            name = f"{TAG}{idx+1:02d}"
            open(os.path.join(OUT, name + ".png"), "wb").write(base64.b64decode(r["data"]))
            print("saved", name, "y=", y)

        js = r"""(()=>{
const g=e=>{if(!e)return null;const c=getComputedStyle(e);const b=e.getBoundingClientRect();
return{tag:e.tagName,txt:(e.textContent||'').trim().slice(0,60),
bg:c.backgroundColor,color:c.color,ff:c.fontFamily,fs:c.fontSize,fw:c.fontWeight,
ls:c.letterSpacing,lh:c.lineHeight,br:c.borderRadius,sh:c.boxShadow.slice(0,180),
pad:c.padding,mar:c.margin,bi:c.backgroundImage.slice(0,200),
w:Math.round(b.width),h:Math.round(b.height)}};
const out={meta:{title:document.title,url:location.href,H:document.documentElement.scrollHeight,
bodyBg:getComputedStyle(document.body).backgroundColor}};
const sels=['body','header','.site-header','h1','h2','h3','h4','p','a','main','article',
'.entry-content','.wp-block-group','.wp-block-columns','.wp-block-image','.wp-block-cover',
'.wp-block-button__link','button','figure','img','nav','footer','.wp-block-media-text',
'.wp-block-gallery','.wp-block-heading','.has-text-align-center','.wp-block-spacer'];
for(const s of sels){const v=g(document.querySelector(s)); if(v) out[s]=v;}
out.allFonts=[...new Set([...document.querySelectorAll('h1,h2,h3,p,a,div,span,li')].slice(0,4000).map(e=>getComputedStyle(e).fontFamily))].slice(0,10);
out.blobs=[...document.querySelectorAll('img,div')].filter(e=>{const b=e.getBoundingClientRect();return b.width>250&&b.height>250}).slice(0,12).map(g);
return JSON.stringify(out)})()"""
        v = await cmd("Runtime.evaluate", expression=js, returnByValue=True)
        open(os.path.join(OUT, f"{TAG}_style.json"), "w", encoding="utf-8").write(str(v["result"]["value"]))
        print("style saved")

asyncio.run(go())
p.terminate()
