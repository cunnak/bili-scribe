async def click(sel, wait=1.2):
    await s.js(f"document.querySelector({sel!r}).click()")
    await asyncio.sleep(wait)

# 05 发布块
await s.js("""(()=>{const l=document.querySelector('.left'); const p=document.querySelector('#publish-btn');
if(p){p.scrollIntoView({block:'center'});} return l.scrollTop})()""")
await asyncio.sleep(1.8)
await s.shot("05-publish")

# 06 发布记录 + 底部
await s.js("""(()=>{const l=document.querySelector('.left'); l.scrollTop=l.scrollHeight; return l.scrollTop})()""")
await asyncio.sleep(1.8)
await s.shot("06-publish-log")

# 07 设置抽屉下半（发布/采集）
await click("#chip-settings", 2.0)
await s.js("""(()=>{const d=document.querySelector('.db'); d.scrollTop=d.scrollHeight; return d.scrollTop})()""")
await asyncio.sleep(1.5)
await s.shot("07-settings-2")
print("ok")
