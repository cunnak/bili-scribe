W, H = 696, 1052
await s.cmd("Emulation.setEmulatedMedia", media="print")
await s.cmd("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
await s.cmd("Page.navigate", url="file:///C:/Users/b1596/Desktop/%E8%A7%86%E9%A2%91%E5%86%85%E5%AE%B9%E6%80%BB%E7%BB%93/report/%E5%AE%9E%E9%AA%8C%E6%8A%A5%E5%91%8A.html")
await asyncio.sleep(2.5)
print(await s.js("""(() => {
  const b=getComputedStyle(document.body), h=document.documentElement;
  const t=document.querySelector('header');
  const w=document.querySelector('.wrap')||document.querySelector('main');
  const r=(w||t).getBoundingClientRect();
  const p=document.querySelector('p');
  return JSON.stringify({
    bodyPad:[b.paddingLeft,b.paddingRight,b.marginLeft],
    docScrollW:h.scrollWidth, docClientW:h.clientWidth,
    wrapTag:(w||t).tagName, wrapCls:(w||t).className, wrapRect:[r.left,r.right,r.width],
    wrapPad:[(w||t)?getComputedStyle(w||t).paddingLeft:'-',(w||t)?getComputedStyle(w||t).paddingRight:'-'],
    firstP:p?[p.getBoundingClientRect().left,p.getBoundingClientRect().right]:null
  },null,1);
})()"""))
