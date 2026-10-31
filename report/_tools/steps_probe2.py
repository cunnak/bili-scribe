W, H = 696, 1052
await s.cmd("Emulation.setEmulatedMedia", media="print")
await s.cmd("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
await s.cmd("Page.navigate", url="file:///C:/Users/b1596/Desktop/%E8%A7%86%E9%A2%91%E5%86%85%E5%AE%B9%E6%80%BB%E7%BB%93/report/%E5%AE%9E%E9%AA%8C%E6%8A%A5%E5%91%8A.html")
await asyncio.sleep(2.5)
print(await s.js("""(() => {
  const out=[];
  document.querySelectorAll('.flow,.flow *,.loop,.loop *').forEach(el=>{
    const r=el.getBoundingClientRect();
    if(r.width>0) out.push(el.tagName+'.'+String(el.className||'-').slice(0,22)+' L'+Math.round(r.left)+' R'+Math.round(r.right)+' W'+Math.round(r.width));
  });
  return out.slice(0,22).join('\u0001');
})()"""))
print('---- overflow-x containers ----')
print(await s.js("""(() => {
  const out=[];
  document.querySelectorAll('body *').forEach(el=>{
    if(el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0)
      out.push(el.tagName+'.'+String(el.className||'-').slice(0,20)+' sw'+el.scrollWidth+' cw'+el.clientWidth+' ovf'+getComputedStyle(el).overflowX);
  });
  return out.slice(0,15).join('\u0001') || 'none';
})()"""))
