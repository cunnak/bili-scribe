# -*- coding: utf-8 -*-
"""第一步（v2）：把 index.html 的 <style> 里硬编码颜色收敛成变量。

**核心手法：半透明变体保留 alpha，只把 RGB 三元组抽成变量。**
`rgba(216,242,75,.45)` → `rgba(var(--acid-rgb),.45)`。
这样深色皮肤的每一个像素值都与改动前**完全相等**（alpha 一位都没动），
换肤时只需要改三元组 —— 比如白叠层 `255,255,255` 换成 `0,0,0`，
34 处强调线自动跟着反色，不会出现"改了一处漏了三处"。

始终从 git HEAD 的干净副本 `_index.orig.html` 出发，保证可重复执行。
"""
import re
from pathlib import Path

SRC = Path("_index.orig.html")
P = Path("bili_scribe/app/static/index.html")

lines = SRC.read_text(encoding="utf-8").split("\n")

# 动态定位 style 块，不硬编码行号（index.html 随时会变长）
st = next(i for i, l in enumerate(lines) if l.strip() == "<style>")
en = next(i for i, l in enumerate(lines) if l.strip() == "</style>")
print(f"style block: lines {st+1}-{en+1}")

root_st = next(i for i, l in enumerate(lines) if l.strip() == ":root{")
root_en = next(i for i, l in enumerate(lines) if i > root_st and l.strip() == "}")

head = lines[:root_st]        # <style> 到 :root{ 之前（含注释头）
tail = lines[root_en + 1 :]   # :root 之后的所有内容（含 </style> 与 body）

body = "\n".join(tail)

# ------------------------------------------------- RGB 三元组（alpha 原样保留）
TRIPLES = [
    ("216,242,75",   "var(--acid-rgb)"),    # 酸性黄绿
    ("255,107,53",   "var(--ember-rgb)"),   # 工业橙红
    ("255,255,255",  "var(--ink-rgb)"),     # 白叠层（浅色皮肤反成黑叠层）
    ("4,5,6",        "var(--scrim-rgb)"),   # 抽屉遮罩
    ("0,0,0",        "var(--shade-rgb)"),   # 阴影 / 顶栏斜纹
]
for raw, var in TRIPLES:
    body = body.replace(f"rgba({raw},", f"rgba({var},")

# ------------------------------------------------- 纯色字面量
SOLIDS = [
    ("#C8CFCB", "var(--text-2)"),
    ("#0A0B08", "var(--on-acid)"),
    ("#E4FA6B", "var(--acid-hover)"),
    ("#0A0C0D", "var(--foot)"),
]
for old, new in SOLIDS:
    body = body.replace(old, new)

# box-shadow 整条提成变量：浅色下阴影要淡得多
body = body.replace(
    "box-shadow:0 18px 48px rgba(var(--shade-rgb),.62)",
    "box-shadow:var(--pop-shadow)",
)

# ------------------------------------------------- 文字色与细线走 --acid-tx
# 34 处 `color:var(--acid)` 是文字；浅色底上荧光黄绿只有 1.54:1，必须换深橄榄。
# negative lookbehind 避开 border-color / border-left-color / background-color。
body = re.sub(r"(?<![-\w])color:var\(--acid\)", "color:var(--acid-tx)", body)
body = body.replace("border-left-color:var(--acid)", "border-left-color:var(--acid-tx)")
# 2px 对角装饰线同理：深色下等同 acid，浅色下必须是深橄榄才看得见
body = body.replace("solid var(--acid)", "solid var(--acid-tx)")

# #fff：两处文字色必须走变量（浅色底上纯白字是隐形的），二维码底是语义色
# （必须白底才扫得出来），保留字面量。深色皮肤的 --strong 就是纯白，像素不变。
body = body.replace(
    "::selection{background:rgba(var(--acid-rgb),.28);color:#fff}",
    "::selection{background:rgba(var(--acid-rgb),.28);color:var(--strong)}",
)
body = body.replace(
    "strong{color:#fff;font-weight:700}", "strong{color:var(--strong);font-weight:700}"
)

ROOT_NEW = r"""  --void:#08090A;
  --bg:#0D0F10;
  --panel:#131617;
  --panel-2:#191D1E;
  --line:rgba(var(--ink-rgb),.10);
  --line-s:rgba(var(--ink-rgb),.055);

  --acid:#D8F24B;
  --acid-d:#8A9B22;
  --ember:#FF6B35;
  --paper:#E8E2D4;

  --text:#F2F4F2;
  --muted:#8B9490;
  --dim:#6B7672;

  /* ================= 换肤用的语义层 =================
     这一层存在的唯一理由是**同一个颜色在不同底色上必须换写法**。
     酸性黄绿在近黑底上是 15.3:1（当文字很舒服），在近白底上只有 1.54:1
     （当文字直接读不出来）。所以拆成三类：

       --acid       实心色块：顶栏斜块、主按钮、圆点、输入光标 —— 浅色皮肤照样用荧光色
       --acid-tx    文字与 1px 细线 —— 浅色皮肤换成深橄榄
       --acid-rgb   半透明强调的 RGB 三元组 —— alpha 由调用处决定，一位都不变

     `--ink-rgb` 是**叠层**：深色皮肤叠白（提亮），浅色皮肤必须叠黑（压暗）。
     若把它写死成 255,255,255，换肤后网格、刻度尺、行悬停会集体消失 ——
     那正是"只换了一半"的典型症状。

     `--shade-rgb` 给阴影与遮罩用；浅色底上 0.62 的黑影会脏成一片。 */
  --acid-rgb:216,242,75;
  --ember-rgb:255,107,53;
  --ink-rgb:255,255,255;
  --shade-rgb:0,0,0;
  --scrim-rgb:4,5,6;

  --acid-tx:#D8F24B;
  --acid-hover:#E4FA6B;
  --on-acid:#0A0B08;

  --strong:#FFFFFF;
  --text-2:#C8CFCB;
  --foot:#0A0C0D;
  --stripe:rgba(0,0,0,.9);
  --pop-shadow:0 18px 48px rgba(0,0,0,.62);

  --mono:'Cascadia Mono','Consolas','SF Mono',monospace;
  --sans:'Segoe UI','Microsoft YaHei',sans-serif;
  --cn:'Microsoft YaHei','PingFang SC',sans-serif;"""

out = "\n".join(head) + "\n:root{\n" + ROOT_NEW + "\n}\n" + body
P.write_text(out, encoding="utf-8")
print("step1 v2 done ->", P)
print("total lines:", out.count("\n") + 1)
