# -*- coding: utf-8 -*-
import os, sys, json, re

TIKU = r"C:\Users\b1596\Desktop\视频内容总结\_tiku"
OUT = r"C:\Users\b1596\Desktop\视频内容总结\_tiku\_search_out.txt"

def load():
    texts = {}
    for name in sorted(os.listdir(TIKU)):
        if name.endswith('.txt') and not name.startswith('_'):
            texts[name] = open(os.path.join(TIKU, name), encoding='utf-8', errors='ignore').read().split('\n')
    return texts

def search_lines(texts, kw, before=2, after=14, maxhit=3):
    blocks = []
    for name, lines in texts.items():
        for i, ln in enumerate(lines):
            if kw in ln:
                s = max(0, i - before); e = min(len(lines), i + after)
                seg = '\n'.join(x for x in lines[s:e] if x.strip())
                blocks.append((name[:6], i, seg))
                if len(blocks) >= maxhit:
                    return blocks
    return blocks

def main():
    kwfile = sys.argv[1]
    kws = [l.strip() for l in open(kwfile, encoding='utf-8') if l.strip()]
    texts = load()
    out = []
    for kw in kws:
        num, _, key = kw.partition('|')
        out.append('=' * 70)
        out.append('### 题 %s  KEY=[%s]' % (num, key))
        bl = search_lines(texts, key)
        if not bl:
            out.append('  <NO HIT>')
        for name, i, seg in bl:
            out.append('--- %s L%d' % (name, i))
            out.append(seg)
    open(OUT, 'w', encoding='utf-8').write('\n'.join(out))
    print('written', OUT, len(out))

main()
