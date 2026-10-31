# -*- coding: utf-8 -*-
"""抓 ignoredone 美术资源页的真实视觉：截图 + 计算样式 + 取色统计"""
import base64, json, os, subprocess, sys, time, urllib.request
import websockets.sync.client as ws

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(OUT, "shots")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9346
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://www.ignoredone.space/index.php/resource/"
TAG = sys.argv[2] if len(sys.argv) > 2 else "ig5"

proc = subprocess.Popen([
    CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
    r"--user-data-dir=%TEMP%\cdp-ig5", "--window-size=1440,2400",
    "--hide-scrollbars", "--force-device-scale-factor=1",
    "--no-first-run", "--no-default-browser-check", "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

for _ in range(60):
    try:
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
        if tabs: break
    except Exception:
        pass
    time.sleep(0.3)

url = tabs[0]["webSocketDebuggerUrl"]
mid = [0]

def call(ws_, method, params=None):
    mid[0] += 1
    ws_.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws_.recv())
        if msg.get("id") == mid[0]:
            return msg.get("result", {})

with ws.connect(url, max_size=None) as w:
    call(w, "Page.enable")
    call(w, "Runtime.enable")
    call(w, "Emulation.setDeviceMetricsOverride",
         {"width": 1440, "height": 2400, "deviceScaleFactor": 1, "mobile": False})
    call(w, "Page.navigate", {"url": TARGET})
    time.sleep(6)

    # 计算样式采样
    js = r"""
    (() => {
      const pick = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return {
          sel, tag: el.tagName, text: (el.textContent||'').trim().slice(0,80),
          color: s.color, background: s.backgroundColor, bgImage: s.backgroundImage.slice(0,160),
          fontFamily: s.fontFamily, fontSize: s.fontSize, fontWeight: s.fontWeight,
          lineHeight: s.lineHeight, letterSpacing: s.letterSpacing,
          borderRadius: s.borderRadius, boxShadow: s.boxShadow.slice(0,200),
          padding: s.padding, margin: s.margin,
          w: Math.round(r.width), h: Math.round(r.height),
        };
      };
      const sels = ['body','h1','h2','h3','p','a','.wp-block-button__link','button',
        'header','.site-header','main','article','figure','img','nav','footer',
        '.entry-content','.wp-block-group','.wp-block-columns','.wp-block-image',
        '.wp-block-cover','.wp-block-media-text','h1.wp-block-heading','.has-text-align-center'];
      const out = {};
      for (const s of sels) { const v = pick(s); if (v) out[s] = v; }
      out._meta = {title: document.title, url: location.href,
        docH: document.documentElement.scrollHeight,
        bodyBg: getComputedStyle(document.body).backgroundColor};
      return JSON.stringify(out);
    })()
    """
    r = call(w, "Runtime.evaluate", {"expression": js, "returnByValue": True})
    style = json.loads(r["result"]["value"])
    with open(os.path.join(SHOTS, f"{TAG}_style.json"), "w", encoding="utf-8") as f:
        json.dump(style, f, ensure_ascii=False, indent=2)

    # 全页截图
    m = call(w, "Page.getLayoutMetrics")
    ch = m["cssContentSize"]
    call(w, "Emulation.setDeviceMetricsOverride",
         {"width": 1440, "height": min(int(ch["height"]), 12000),
          "deviceScaleFactor": 1, "mobile": False})
    time.sleep(1.5)
    shot = call(w, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    path = os.path.join(SHOTS, f"{TAG}_full.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(shot["data"]))
    print("saved", path, os.path.getsize(path))
    print("docH", ch["height"])
    print(json.dumps(style.get("_meta", {}), ensure_ascii=False))

proc.terminate()
