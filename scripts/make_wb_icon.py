"""生成 VideoWorkbench 的应用图标（多尺寸 .ico）。

为什么自己画而不是找一张图：图标要和界面同一套色（`--void` 墨黑 + `--acid`
酸性黄绿），随便找的图标会让安装目录里出现第三个配色体系。这里只用两个颜色、
一个几何符号（播放三角 + 切片横线），16px 下也能认出来。

用法：
    ./.venv/Scripts/python.exe scripts/make_wb_icon.py
输出：video_workbench/resources/icon.ico（16/24/32/48/64/128/256）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

PROJ = Path(__file__).resolve().parents[1]
OUT = PROJ / "video_workbench" / "resources" / "icon.ico"

VOID = (13, 15, 16, 255)      # --void #0D0F10
ACID = (216, 242, 75, 255)    # --acid #D8F24B
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw(size: int) -> Image.Image:
    """按目标尺寸画：小图不加圆角与细节，避免糊成一团。"""
    scale = 8  # 先大画再缩，边缘才不锯齿
    side = size * scale
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(side * 0.22)
    if size >= 32:
        d.rounded_rectangle([0, 0, side - 1, side - 1], radius=radius, fill=VOID)
    else:
        # 16/24 全铺满：圆角在这个尺寸只剩噪点
        d.rectangle([0, 0, side - 1, side - 1], fill=VOID)

    # 播放三角（重心略偏右，视觉上才居中）。上下留白不对称：下面要给切片横线让位，
    # 等分留白会让横线压在三角底边上（第一版就是这样糊在一起的）。
    left = side * 0.30
    top = side * 0.20
    bottom = side * 0.66
    tri = [
        (left, top),
        (left, bottom),
        (side * 0.76, (top + bottom) / 2),
    ]
    d.polygon(tri, fill=ACID)

    # 切片横线：暗示"把视频切成段"
    if size >= 32:
        line_w = max(2, int(side * 0.055))
        y1 = side * 0.73
        y2 = side * 0.84
        x0 = side * 0.26
        x1 = side * 0.74
        d.rounded_rectangle([x0, y1, x1, y1 + line_w], radius=line_w // 2, fill=ACID)
        d.rounded_rectangle([x0, y2, x0 + (x1 - x0) * 0.55, y2 + line_w],
                            radius=line_w // 2, fill=ACID)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    biggest = frames[-1]
    biggest.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"[icon] {OUT}  ({OUT.stat().st_size} B, {len(SIZES)} 个尺寸)")

    # 顺手留一张 png 给文档/商店页用（不进包）
    png = OUT.with_suffix(".png")
    biggest.save(png, format="PNG")
    print(f"[icon] {png}  ({png.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
