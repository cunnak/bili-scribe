from PIL import Image
import os, base64, json
S = "shots"; O = "img"
os.makedirs(O, exist_ok=True)
plan = [("01-home","01-home",1600),("02-history","02-history",1600),
        ("03-role","03-role",1600),("04-settings","04-settings",1600),
        ("05-publish","05-publish",1600),("07-settings-2","07-settings-2",1600)]
meta={}
for src,dst,w in plan:
    im = Image.open(os.path.join(S,src+".png")).convert("RGB")
    r = w / float(im.width)  # 源为 2x
    im2 = im.resize((int(im.width*r), int(im.height*r)), Image.LANCZOS)
    p = os.path.join(O,dst+".jpg"); im2.save(p,"JPEG",quality=88,optimize=True,progressive=True)
    meta[dst]=os.path.getsize(p)
# 版本面板特写：从 2x 原图裁右侧栏
im = Image.open(os.path.join(S,"01-home.png")).convert("RGB")
box=(2246,130,2880,1230)
im.crop(box).save(os.path.join(O,"06-version-diff.jpg"),"JPEG",quality=90,optimize=True)
meta["06-version-diff"]=os.path.getsize(os.path.join(O,"06-version-diff.jpg"))
print(json.dumps(meta,indent=0))
