# -*- coding: utf-8 -*-
"""把用户题干与题库全文做 10-gram 投票定位，输出最佳匹配区段原文。"""
import os, sys, re, json

TIKU = r"C:\Users\b1596\Desktop\视频内容总结\_tiku"

def norm_chars(s):
    """返回 (归一化字符串, 每个归一化字符在原文中的下标)"""
    out = []
    idx = []
    for i, ch in enumerate(s):
        if ch.isspace():
            continue
        if re.match(r'[（）()【】\[\]{}“”"\'’，,。.、；;：:？?！!—\-_…·*]', ch):
            continue
        out.append(ch)
        idx.append(i)
    return ''.join(out), idx

def load_texts():
    texts = {}
    for name in sorted(os.listdir(TIKU)):
        if name.endswith('.txt') and not name.startswith('_'):
            texts[name] = open(os.path.join(TIKU, name), encoding='utf-8', errors='ignore').read()
    return texts

def build_index(texts):
    """每文件一个归一化串 + 索引"""
    idxs = {}
    for name, t in texts.items():
        n, m = norm_chars(t)
        idxs[name] = (n, m, t)
    return idxs

def best_match(idxs, q, gram=10):
    qn, _ = norm_chars(q)
    if len(qn) < gram:
        gram = max(4, len(qn))
    grams = [qn[i:i+gram] for i in range(0, max(1, len(qn)-gram+1))]
    grams = list(dict.fromkeys(grams))[:60]
    best = (0, None, None, None)
    for name, (n, m, t) in idxs.items():
        score = {}
        for g in grams:
            start = 0
            while True:
                p = n.find(g, start)
                if p < 0:
                    break
                # 以命中位置为中心做区间投票
                key = p // 200
                score[key] = score.get(key, 0) + 1
                start = p + 1
        for key, sc in score.items():
            if sc > best[0]:
                best = (sc, name, key, n, m, t)
    return best, grams

def extract_ctx(t, m, nstart, nend, pad_lines=3):
    """把归一化区间映射回原文，按行取上下文"""
    i0 = m[nstart] if nstart < len(m) else 0
    i1 = m[min(nend, len(m)-1)]
    ls = t.rfind('\n', 0, i0)
    le = t.find('\n', i1)
    if ls < 0: ls = 0
    if le < 0: le = len(t)
    # 扩展到若干行
    for _ in range(pad_lines):
        ls2 = t.rfind('\n', 0, ls)
        if ls2 < 0: break
        ls = ls2
    for _ in range(pad_lines+6):
        le2 = t.find('\n', le+1)
        if le2 < 0: break
        le = le2
    return t[ls:le]

def main():
    qfile = sys.argv[1]
    outfile = sys.argv[2]
    texts = load_texts()
    idxs = build_index(texts)
    out = []
    for line in open(qfile, encoding='utf-8'):
        line = line.rstrip('\n')
        if not line.strip():
            continue
        num, _, q = line.partition('|')
        best, grams = best_match(idxs, q)
        out.append('=' * 72)
        out.append('### 题 %s | 查询=%s' % (num, q))
        if not best or best[0] == 0:
            out.append('  <NO MATCH>')
            continue
        sc, name, key, n, m, t = best
        # 在最佳窗口内精确定位
        p = key * 200
        nstart = max(0, p - 30)
        nend = min(len(n), p + 400)
        # 用第一个命中 gram 精确定位
        pos = n.find(grams[0], max(0, p - 400))
        if pos >= 0:
            nstart = pos
            nend = min(len(n), pos + 500)
        out.append('  [%s] score=%d' % (name[:10], sc))
        out.append(extract_ctx(t, m, nstart, nend))
    open(outfile, 'w', encoding='utf-8').write('\n'.join(out))
    print('written', outfile)

main()
