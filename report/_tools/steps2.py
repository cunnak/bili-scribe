async def click(sel):
    await s.js(f"document.querySelector({sel!r}).click()")
    await asyncio.sleep(1.2)

# 02 历史浮层
await click("#chip-history")
await asyncio.sleep(1.5)
# 选中第一条预览
await s.js("""(()=>{const it=document.querySelector('#hist-list2 .hi')||document.querySelector('#hist-list2>*');
if(it) it.click(); return !!it})()""")
await asyncio.sleep(1.5)
await s.shot("02-history")

# 03 角色浮层
await click("#hist-pop-x")
await asyncio.sleep(0.8)
await click("#chip-role")
await asyncio.sleep(1.0)
await s.shot("03-role")

# 04 设置抽屉
await click("#chip-role")
await asyncio.sleep(0.5)
await click("#chip-settings")
await asyncio.sleep(2.0)
await s.shot("04-settings")
print(await s.js("document.querySelector('#d-role')?document.querySelector('#d-role').value:'na'"))
