"""生成应用图标 `assets/bili-scribe.ico`。

    .venv/Scripts/python.exe scripts/make-icon.py

**为什么单独一个脚本、而不是塞进 build-exe.py。** 打包脚本的职责是打包；
顺手画图会让人下次想换图标时不知道该改哪儿（这条理由写在 `build-exe.py:_icon`
的 docstring 里，两边一致）。这里的产物是**源文件**，应该进仓库；打包脚本只负责
把它交给 PyInstaller。

图标画的是界面的角标：近黑底 + 酸性黄绿的斜切块 + 黑色斜纹。和 `index.html`
里 `.top::before` / `.top::after` 同源——一排斜纹斜切块，正是这个界面最容易
被认出来的那一点。颜色也从界面取值（`--bg` `#0D0F10`、`--acid` `#D8F24B`），
不另立一套。

多尺寸是必须的：Windows 会在标题栏（16px）、任务栏（32px）、资源管理器大图标
（256px）三种尺寸下分别取用。只给一张 256 的话，小尺寸会由系统缩放，斜纹糊成
一片灰。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "bili-scribe.ico"

BG = (13, 15, 16)        # --bg  #0D0F10
ACID = (216, 242, 75)    # --acid #D8F24B
INK = (8, 9, 10)         # --void #08090A

SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> Image.Image:
    """画一张 `size`x`size` 的图。

    按比例算坐标，不写死像素：16px 下画死 34px 的块会整张糊掉。斜切角度保持
    一致（顶边宽、底边窄，左侧竖直），斜纹间距在小尺寸下放大——16px 里塞 3px
    的条纹只会得到噪点。
    """
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # 斜切块：占左侧约 58% 宽，顶边到右边、底边收窄，形状同 .top::before。
    top = size * 0.14
    bottom = size * 0.86
    x0 = size * 0.16
    x1 = size * 0.62          # 顶边右端
    x2 = size * 0.44          # 底边右端（收窄）
    d.polygon([(x0, top), (x1, top), (x2, bottom), (x0, bottom)], fill=ACID)

    # 斜纹：115° 左右，用细平行四边形切黑色。小尺寸少画几条、画粗一点。
    #
    # 16px 那档**不画斜纹**：条纹间距只有一两个像素，缩下去全是噪点，看着像
    # 图标坏了而不是有纹理。小尺寸留纯色块更能认（实测对比过）。
    if size <= 24:
        pass
    else:
        stripes = 3 if size <= 32 else 4
        gap = (bottom - top) / (stripes * 2)
        skew = (x1 - x2) * 0.55   # 让条纹跟着块的斜度走
        for i in range(stripes):
            y = top + gap * (i * 2 + 0.5)
            d.polygon(
                [
                    (x0, y),
                    (x2 + (x1 - x2) * 0.35, y),
                    (x2 + (x1 - x2) * 0.35 - skew, y + gap * 0.9),
                    (x0, y + gap * 0.9),
                ],
                fill=INK,
            )

    # 右下角一个小方块，呼应界面里状态 chip 前面的方点。
    # 16px 不放：那个尺寸下它抢到的像素和斜切块一样多，看起来像两个色块。
    if size >= 32:
        s = size * 0.11
        d.rectangle([size - s - size * 0.14, size - s - size * 0.14,
                     size - size * 0.14, size - size * 0.14], fill=ACID)

    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(n) for n in SIZES]
    # **必须把每一档都当 append_images 传进去。** 只给一张 256 加 `sizes=` 的话，
    # PIL 会用 256 那张 LANCZOS 缩出小尺寸——按尺寸分别画的工作全白费，
    # 16px 上依然会是糊掉的斜纹（实测：改完 `draw()` 后文件字节数一个都没变，
    # 就是这个原因）。PIL 只在 `provided_im.size` 恰好等于目标尺寸时才用现成的。
    big = frames[-1]
    big.save(
        OUT,
        format="ICO",
        sizes=[(n, n) for n in SIZES],
        append_images=frames[:-1],
    )
    print(f"已写入 {OUT}（{OUT.stat().st_size} 字节，{len(SIZES)} 种尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
