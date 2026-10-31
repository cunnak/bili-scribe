import sys, zipfile, re, os, html

SRC = r"C:\Users\b1596\Desktop\【9】知识赛网考题库参考"
OUT = r"C:\Users\b1596\Desktop\视频内容总结\_tiku"
os.makedirs(OUT, exist_ok=True)

def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
    xml = re.sub(r'<w:br[^>]*/>', '\n', xml)
    txt = re.sub(r'<[^>]+>', '', xml)
    txt = html.unescape(txt)
    txt = re.sub(r'[ \t]+', ' ', txt)
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    return txt

for name in sorted(os.listdir(SRC)):
    p = os.path.join(SRC, name)
    if not name.lower().endswith('.docx'):
        continue
    try:
        t = extract_docx(p)
        base = os.path.splitext(name)[0]
        op = os.path.join(OUT, base + '.txt')
        with open(op, 'w', encoding='utf-8') as f:
            f.write(t)
        print('OK', name, len(t), '->', op)
    except Exception as e:
        print('FAIL', name, repr(e))
