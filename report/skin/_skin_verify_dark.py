# -*- coding: utf-8 -*-
"""机械核对：把新旧两边的 CSS 变量**都展开**，再逐字比对。

深色皮肤（默认皮肤）的每个像素值都必须与改动前完全一致。
只要出现一处差异，就说明收敛手法改变了视觉 —— 那是 bug，不是"微调"。

新皮肤 `[data-skin="exhibition"]` 是纯新增块，比对时剥掉。
"""
import difflib
import re
import sys
from pathlib import Path

NEW = Path("bili_scribe/app/static/index.html")
OLD = Path("report/skin/_index.orig.html")


def css(s: str) -> str:
    L = s.split("\n")
    st = next(i for i, l in enumerate(L) if l.strip() == "<style>")
    en = next(i for i, l in enumerate(L) if l.strip() == "</style>")
    return "\n".join(L[st + 1 : en])


def block_at(c: str, start: int) -> str:
    """从 c[start] 处的 '{' 起找到配对的 '}'，返回整个块（含花括号）。"""
    d = 0
    for i, ch in enumerate(c[start:]):
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
            if d == 0:
                return c[start : start + i + 1]
    raise ValueError("unbalanced block")


def strip_var_blocks(c: str) -> str:
    """剥掉所有只定义变量的块：`:root{...}` 与 `[data-skin="exhibition"]{...}`。"""
    for sel in (":root{", '[data-skin="exhibition"]{'):
        while sel in c:
            start = c.index(sel)
            brace = start + len(sel) - 1      # sel 以 '{' 结尾，这里就是块的起点
            block = block_at(c, brace)
            c = c[:start] + c[brace + len(block) :]
    return c


def strip_skin_rules(c: str) -> str:
    """剥掉 `.sk` 系列规则（皮肤选择器自己的样式）。

    这组规则是本次**纯新增**的界面元素，不属于任何一套皮肤的配色，
    因此不参与"深色皮肤是否被改动"的比对。

    定位方式：`.sk` 后面紧跟的不是标识符字符（排除 `.sky` 这类误伤），
    就是一条规则的开头；再往后第一个 `{` 是它的规则体起点，`block_at` 找到配对的 `}`。
    选择器可能是 `.sk{` / `.sk:hover{` / `.sk.on .tx b{`，都能覆盖。

    必须从后往前删，否则前面的删除会让后面记录的偏移全部错位。
    """
    starts = [m.start() for m in re.finditer(r"\.sk(?=[^\w-])", c)]
    for pos in reversed(starts):
        brace = c.index("{", pos)
        block = block_at(c, brace)
        c = c[:pos] + c[brace + len(block) :]
    return c


def vars_of_all(c: str, selectors: tuple[str, ...] = (":root",)) -> dict[str, str]:
    """收集指定变量块里的定义（按出现顺序，后面的覆盖前面的）。"""
    out: dict[str, str] = {}
    for sel in selectors:
        pat = re.escape(sel) + r"\{([^{}]*)\}"
        for m in re.finditer(pat, c):
            for v in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1)):
                out[v.group(1)] = v.group(2).strip()
    return out


def expand(text: str, vars_: dict[str, str], depth: int = 0) -> str:
    if depth > 12:
        return text

    def rep(m):
        v = vars_.get(m.group(1))
        return m.group(0) if v is None else expand(v, vars_, depth + 1)

    return re.sub(r"var\((--[\w-]+)\)", rep, text)


def canon_hex(c: str) -> str:
    def canon(m):
        h = m.group(1).upper()
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        return "#" + h

    return re.sub(
        r"#([0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})(?![0-9A-Za-z])", canon, c
    )


def normalize(c: str, vars_: dict[str, str]) -> str:
    """只留真正的样式声明：剥变量块、剥新增的 .sk 规则、剥注释、展开变量、压空白。"""
    c = strip_var_blocks(c)
    c = strip_skin_rules(c)
    c = re.sub(r"/\*.*?\*/", "", c, flags=re.S)
    c = expand(c, vars_)
    c = re.sub(r"\s+", " ", c)
    c = re.sub(r"\s*([{};:,])\s*", r"\1", c)
    return canon_hex(c).strip()


old_css = css(OLD.read_text(encoding="utf-8"))
new_css = css(NEW.read_text(encoding="utf-8"))

# 只读 `:root` —— 那才是**默认（深色）皮肤**的取值。
# 浅色皮肤的覆盖块 `[data-skin="exhibition"]` 是新增内容，不能参与这次比对。
old_vars = vars_of_all(old_css)
new_vars = vars_of_all(new_css)
for k, v in new_vars.items():
    old_vars.setdefault(k, v)

a = normalize(old_css, old_vars)
b = normalize(new_css, new_vars)

print(f"old vars: {len(old_vars)}   new vars: {len(new_vars)}")
print(f"normalized lengths: old={len(a)} new={len(b)}")

if a == b:
    print("RESULT: IDENTICAL - 深色皮肤逐像素不变")
    sys.exit(0)

sm = difflib.SequenceMatcher(None, a, b)
print(f"RESULT: DIFFERS ratio={sm.ratio():.5f}")
shown = total = 0
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
        continue
    total += 1
    if shown < 40:
        shown += 1
        print(f"  {tag:8s} OLD[{a[i1:i2]}]  ->  NEW[{b[j1:j2]}]")
print(f"total diff hunks: {total}")
sys.exit(1)
