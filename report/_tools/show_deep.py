# -*- coding: utf-8 -*-
import json, io, sys
p = r"C:\Users\b1596\Desktop\视频内容总结\report\shots\rs_deep.json"
d = json.load(open(p, encoding="utf-8"))
out = io.StringIO()
w = out.write

w("=== doc ===\n")
w(json.dumps(d["doc"], ensure_ascii=False, indent=1) + "\n\n")

w("=== hero (>=40px 大字) ===\n")
for h in d["hero"]:
    w(f'{h["tag"]}.{h["cls"]}  "{h["txt"]}"  rect={h["r"]}\n')
    w(f'   fs={h["s"]["fs"]} fw={h["s"]["fw"]} ls={h["s"]["ls"]} lh={h["s"]["lh"]} ff={h["s"]["ff"][:40]} color={h["s"]["color"]}\n')

w("\n=== cards (图卡) ===\n")
for c in d["cards"]:
    w(f'{c["tag"]}.{c["cls"]}  rect={c["r"]}\n')
    w(f'   br={c["s"]["br"]} sh={c["s"]["sh"]} pad={c["s"]["pad"]} bg={c["s"]["bg"]} of={c["s"]["of"]}\n')
    if c["img"]:
        w(f'   img {c["img"]}\n')
    w(f'   txt="{c["txt"][:60]}"\n')

w("\n=== pills ===\n")
for q in d["pills"]:
    w(f'{q["tag"]}.{q["cls"]} "{q["txt"]}" rect={q["r"]}  br={q["s"]["br"]} fs={q["s"]["fs"]} fw={q["s"]["fw"]} pad={q["s"]["pad"]} bg={q["s"]["bg"]} sh={q["s"]["sh"]}\n')

w("\n=== lines (可见细线) ===\n")
for l in d["lines"]:
    w(f'{l["tag"]}.{l["cls"]} rect={l["r"]} bdL={l["s"]["bdL"]} bdT={l["s"]["bdT"]} bg={l["s"]["bg"]}\n')

w("\n=== topSizes ===\n")
for t in d["topSizes"]:
    w(f'{t["k"]}  x{t["n"]}\n')

w("\n=== fixed bars ===\n")
for b in d["bars"]:
    w(f'{b["tag"]}.{b["cls"]} "{b["txt"]}" rect={b["r"]} bg={b["s"]["bg"]} br={b["s"]["br"]} sh={b["s"]["sh"]} pad={b["s"]["pad"]}\n')

s = out.getvalue()
open(r"C:\Users\b1596\Desktop\视频内容总结\report\shots\rs_deep.txt", "w", encoding="utf-8").write(s)
print(s)
