import base64, os
W, H = 696, 1052          # A4 减去 0.51in 左右边距 / 0.55+0.63in 上下边距后的可打印区
await s.cmd("Emulation.setEmulatedMedia", media="print")
await s.cmd("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1.9, mobile=False)
await s.cmd("Page.navigate", url="file:///C:/Users/b1596/Desktop/%E8%A7%86%E9%A2%91%E5%86%85%E5%AE%B9%E6%80%BB%E7%BB%93/report/%E5%AE%9E%E9%AA%8C%E6%8A%A5%E5%91%8A.html")
await asyncio.sleep(3.0)
h = await s.js("document.body.scrollHeight")
w = await s.js("document.documentElement.scrollWidth")
print("print height", h, "width", w, "pages", round(h / H, 2))
# 横向溢出检测
ov = await s.js("""(() => {
  const bad = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && (r.right > 700 || r.left < -4)) {
      bad.push(el.tagName + '.' + (el.className || '').toString().slice(0,40) + ' → ' + Math.round(r.left) + '..' + Math.round(r.right));
    }
  });
  return bad.slice(0, 25).join(' ~ ') || 'none';
})()""")
print("overflow:", ov)
n = int(h / H) + 1
for i in range(n):
    clip = {"x": 0, "y": i * H, "width": W, "height": H, "scale": 1}
    r = await s.cmd("Page.captureScreenshot", format="png", clip=clip, captureBeyondViewport=True)
    open(os.path.join(OUT, f"p{i+1}.png"), "wb").write(base64.b64decode(r["data"]))
print("pages written", n)
