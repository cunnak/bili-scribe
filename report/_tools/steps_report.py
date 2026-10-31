await s.cmd("Page.navigate", url="file:///C:/Users/b1596/Desktop/%E8%A7%86%E9%A2%91%E5%86%85%E5%AE%B9%E6%80%BB%E7%BB%93/report/%E5%AE%9E%E9%AA%8C%E6%8A%A5%E5%91%8A.html")
await asyncio.sleep(2.5)
h = await s.js("document.body.scrollHeight")
print("page height", h)
for i, y in enumerate([0, 900, 1800, 2700]):
    await s.js(f"window.scrollTo(0,{y})")
    await asyncio.sleep(0.6)
    await s.shot(f"r{i+1}")
