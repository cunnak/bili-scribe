# -*- coding: utf-8 -*-
"""深挖 ignoredone 美术资源库的版式结构：网格线、图卡几何、胶囊、底栏、字阶"""
import asyncio, json, base64, subprocess, os, time, urllib.request
import websockets

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9357
OUT = r"C:\Users\b1596\Desktop\视频内容总结\report\shots"
URL = "https://www.ignoredone.space/index.php/resource/"

p = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=C:\Users\b1596\AppData\Local\Temp\cdp-rs2", "--window-size=1440,1000",
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
const R=e=>{const b=e.getBoundingClientRect();return{x:Math.round(b.x),y:Math.round(b.y+scrollY),w:Math.round(b.width),h:Math.round(b.height)}};
const S=e=>{const c=getComputedStyle(e);return{bg:c.backgroundColor,color:c.color,ff:c.fontFamily,fs:c.fontSize,fw:c.fontWeight,ls:c.letterSpacing,lh:c.lineHeight,br:c.borderRadius,sh:c.boxShadow.slice(0,120),pad:c.padding,mar:c.margin,bd:c.border,bdL:c.borderLeft,bdT:c.borderTop,bi:c.backgroundImage.slice(0,120),pos:c.position,of:c.overflow}};
const all=[...document.querySelectorAll('*')];

// 1) 竖线：左边框/右边框为可见细线的元素
const lines=all.filter(e=>{const c=getComputedStyle(e);
  const hasL=parseFloat(c.borderLeftWidth)>0 && c.borderLeftStyle!=='none';
  const hasT=parseFloat(c.borderTopWidth)>0 && c.borderTopStyle!=='none';
  const hasB=parseFloat(c.borderBottomWidth)>0 && c.borderBottomStyle!=='none';
  return (hasL||hasT||hasB) && e.getBoundingClientRect().width>0;
}).slice(0,40).map(e=>({cls:e.className&&e.className.toString().slice(0,60),tag:e.tagName,r:R(e),s:S(e)}));

// 2) 大图卡（含图片且宽度>400）
const cards=all.filter(e=>{const b=e.getBoundingClientRect();
  return b.width>400 && b.height>300 && (e.tagName==='FIGURE'||e.tagName==='IMG'||e.querySelector('img'))&&e.tagName!=='IMG';
}).slice(0,14).map(e=>({tag:e.tagName,cls:(e.className||'').toString().slice(0,70),r:R(e),s:S(e),
  img:(()=>{const i=e.querySelector('img');if(!i)return null;const b=i.getBoundingClientRect();
    const c=getComputedStyle(i);return{src:(i.currentSrc||i.src||'').slice(-60),nat:i.naturalWidth+'x'+i.naturalHeight,
    w:Math.round(b.width),h:Math.round(b.height),fit:c.objectFit,br:c.borderRadius}})(),
  txt:(e.innerText||'').trim().slice(0,80)}));

// 3) 胶囊
const pills=all.filter(e=>{const c=getComputedStyle(e);const br=parseFloat(c.borderRadius);
  const b=e.getBoundingClientRect();
  return br>=30 && b.width>50 && b.width<420 && b.height>18 && b.height<70;
}).slice(0,14).map(e=>({tag:e.tagName,cls:(e.className||'').toString().slice(0,60),txt:(e.innerText||'').trim().slice(0,30),r:R(e),s:S(e)}));

// 4) 字阶：所有字号统计
const sizes={};
all.forEach(e=>{const t=(e.innerText||'').trim();if(!t||e.children.length>0)return;
  const c=getComputedStyle(e);const k=c.fontSize+'/'+c.fontWeight+'/'+c.fontFamily.split(',')[0];
  sizes[k]=(sizes[k]||0)+1});
const topSizes=Object.entries(sizes).sort((a,b)=>b[1]-a[1]).slice(0,18).map(([k,v])=>({k,n:v}));

// 5) 页头大字
const hero=[...document.querySelectorAll('h1,h2,.wp-block-heading,div,span')].filter(e=>{
  const b=e.getBoundingClientRect();const c=getComputedStyle(e);
  return b.width>500 && parseFloat(c.fontSize)>=40 && (e.innerText||'').trim().length<40;
}).slice(0,8).map(e=>({tag:e.tagName,cls:(e.className||'').toString().slice(0,60),txt:(e.innerText||'').trim().slice(0,50),r:R(e),s:S(e)}));

// 6) 底栏
const bars=all.filter(e=>{const b=e.getBoundingClientRect();const c=getComputedStyle(e);
  return c.position==='fixed'&&b.width>300}).slice(0,10).map(e=>({tag:e.tagName,cls:(e.className||'').toString().slice(0,60),txt:(e.innerText||'').trim().slice(0,60),r:R(e),s:S(e)}));

return JSON.stringify({lines,cards,pills,topSizes,hero,bars,
  doc:{H:document.documentElement.scrollHeight,W:document.documentElement.scrollWidth,
  mainBg:getComputedStyle(document.querySelector('main')||document.body).backgroundColor,
  bodyBg:getComputedStyle(document.body).backgroundColor,
  rootFont:getComputedStyle(document.documentElement).fontSize}})})()"""

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
        # 滚到底触发懒加载，再回顶
        for y in (5000, 15000, 30000):
            await cmd("Runtime.evaluate", expression=f"window.scrollTo(0,{y})")
            await asyncio.sleep(1.2)
        await cmd("Runtime.evaluate", expression="window.scrollTo(0,0)")
        await asyncio.sleep(1.5)
        v = await cmd("Runtime.evaluate", expression=JS, returnByValue=True)
        open(os.path.join(OUT, "rs_deep.json"), "w", encoding="utf-8").write(str(v["result"]["value"]))
        print("saved rs_deep.json")
asyncio.run(go())
p.terminate()
