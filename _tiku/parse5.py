# -*- coding: utf-8 -*-
"""解析规整格式题库（N.题干 / A.选项 / 【答案】X）为结构化 JSON"""
import re, json, sys, os

def parse(path):
    with open(path, encoding='utf-8', errors='ignore') as f:
        raw = f.read()
    lines = [l.replace('\r', '') for l in raw.split('\n')]
    recs, cur = [], None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        # 答案行
        ma = re.match(r'^[【\[]\s*答案\s*[】\]]\s*[:：]?\s*(.+)$', s)
        if ma and cur is not None:
            cur['answer'] = ma.group(1).strip()
            continue
        # 新题号行：数字 + . 或 、，且不是选项
        mq = re.match(r'^(\d{1,4})\s*[\.、．]\s*(.*)$', s)
        if mq and not re.match(r'^[A-Ea-e]\s*[\.、．]', s):
            if cur is not None and (cur['stem'] or cur['options']):
                recs.append(cur)
            cur = {'num': int(mq.group(1)), 'stem': mq.group(2), 'options': [], 'answer': ''}
            continue
        if cur is None:
            continue
        # 选项行
        mo = re.match(r'^([A-Ea-e])\s*[\.、．\)）]\s*(.*)$', s)
        if mo:
            cur['options'].append(mo.group(2).strip())
            continue
        # 续行
        if cur['options']:
            cur['options'][-1] = (cur['options'][-1] + s).strip()
        else:
            cur['stem'] = (cur['stem'] + s).strip()
    if cur is not None and (cur['stem'] or cur['options']):
        recs.append(cur)
    return recs

if __name__ == '__main__':
    base = os.path.dirname(os.path.abspath(__file__))
    files = sys.argv[1:] or ['市调大赛知识赛网考题库及答案5（428页）.txt',
                             '题库调研、统计知识练习题汇总.txt']
    allrecs = []
    for fn in files:
        p = os.path.join(base, fn)
        if not os.path.exists(p):
            print('MISSING', fn); continue
        r = parse(p)
        for x in r:
            x['src'] = fn[:12]
        allrecs.append((fn, r))
        print(fn, '->', len(r), 'recs, with answer:', sum(1 for x in r if x['answer']))
    flat = [x for _, r in allrecs for x in r]
    out = os.path.join(base, '_recs5.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(flat, f, ensure_ascii=False)
    print('total', len(flat), '->', out)
