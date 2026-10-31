# -*- coding: utf-8 -*-
import os, re, json, sys

TIKU = r"C:\Users\b1596\Desktop\视频内容总结\_tiku"

def load_all():
    recs = []
    for name in sorted(os.listdir(TIKU)):
        if not name.lower().endswith('.txt'):
            continue
        p = os.path.join(TIKU, name)
        raw = open(p, encoding='utf-8', errors='ignore').read()
        # 统一：按 "题目：" 切块
        parts = re.split(r'(?=题目[：:])', raw)
        for blk in parts:
            if not blk.startswith('题目'):
                continue
            m = re.match(r'题目[：:](.*?)\[题型[：:](.*?)\]\s*\[答案[：:](.*?)\]', blk, re.S)
            if not m:
                continue
            stem = m.group(1).strip()
            qtype = m.group(2).strip()
            ans = m.group(3).strip()
            rest = blk[m.end():]
            # 选项
            rest = re.sub(r'^\s*\d*-?选项[：:]', '', rest)
            opts = [l.strip() for l in rest.split('\n') if l.strip()]
            recs.append(dict(file=name, stem=stem, qtype=qtype, ans=ans, opts=opts))
    return recs

def norm(s):
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[（）()【】\[\]{}“”"\'’，,。.、；;：:？?！!—\-_…·]', '', s)
    return s

if __name__ == '__main__':
    recs = load_all()
    print('total records:', len(recs))
    from collections import Counter
    print('types:', Counter(r['qtype'] for r in recs).most_common(12))
    print('files:', Counter(r['file'] for r in recs).most_common())
    json.dump(recs, open(os.path.join(TIKU, '_recs.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    for r in recs[:3]:
        print('---', r['stem'][:60], '|', r['qtype'], '|', r['ans'], '|', r['opts'][:5])
