#!/usr/bin/env python
"""检查 web/ 下有没有硬编码颜色（方案书 3.3 / 任务清单 P0-04 验收）。

规则只有一条，但必须自动化：

    除 ``web/css/tokens.css`` 外，``web/`` 里不允许出现 ``#RRGGBB`` / ``#RGB``
    或 ``rgb(...)`` / ``rgba(...)`` 字面量。

为什么较真：皮肤是"换一个 CSS 变量值"实现的。只要有一处写死颜色，
切到 exhibition 就会留下一块"永远深色"的补丁，而且这种问题在单页截图里
根本看不出来——它只在另一套皮肤下显形。人眼审不出来，就让脚本审。

用法::

    python scripts/check_colors.py            # 扫 video_workbench/web
    python scripts/check_colors.py --root X   # 换根目录（测试用）
    python scripts/check_colors.py --list     # 额外列出每处命中

退出码：0 干净，1 有命中。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# 允许出现颜色字面量的文件（相对 root）
ALLOW_FILES = {"css/tokens.css"}

# 只匹配 CSS 值里真正会被浏览器解析的颜色写法。
# - `#abc` / `#aabbcc` / `#aabbccdd`
# - `rgb(...)` / `rgba(...)`
# - `hsl(...)` / `hsla(...)`（同样绕过 token，一并禁）
# 不匹配 URL 里的 `#fragment`：前面必须是 `:` 或 `(` 或空白 + `#`，太容易误伤，
# 所以这里用"十六进制长度"来约束，并要求前面不是字母数字。
HEX_RE = re.compile(r"(?<![0-9A-Za-z_-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
FUNC_RE = re.compile(r"\b(?:rgba?|hsla?)\s*\(", re.IGNORECASE)

# 这些扩展名才看；别去扫 png/woff
SCAN_EXT = {".css", ".html", ".js", ".svg", ".json"}


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in {"node_modules", ".git", "__pycache__"})
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if os.path.splitext(name)[1].lower() not in SCAN_EXT:
                continue
            yield path


def check(root: str) -> list[tuple[str, int, str, str]]:
    """返回 [(相对路径, 行号, 行内容, 命中的字面量)]。"""
    hits: list[tuple[str, int, str, str]] = []
    for path in iter_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel in ALLOW_FILES:
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                # 注释行里允许写例子（比如"不要写 #ff0000"），否则规则没法自我说明
                stripped = line.strip()
                if stripped.startswith(("*", "//", "/*", "<!--")):
                    continue
                for match in list(HEX_RE.finditer(line)) + list(FUNC_RE.finditer(line)):
                    hits.append((rel, lineno, stripped[:120], match.group(0)))
    return hits


def main(argv: list[str] | None = None) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="扫描 web/ 下的硬编码颜色")
    parser.add_argument("--root", default=os.path.join(here, "video_workbench", "web"), help="要扫描的目录")
    parser.add_argument("--list", action="store_true", help="列出每一处命中")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"[check_colors] 目录不存在：{root}", file=sys.stderr)
        return 2

    hits = check(root)
    if not hits:
        print(f"[check_colors] OK：{root} 没有硬编码颜色（tokens.css 除外）")
        return 0

    print(f"[check_colors] 发现 {len(hits)} 处硬编码颜色：", file=sys.stderr)
    shown = hits if args.list else hits[:40]
    for rel, lineno, line, literal in shown:
        print(f"  {rel}:{lineno}  {literal!r}  <- {line}", file=sys.stderr)
    if not args.list and len(hits) > 40:
        print(f"  …还有 {len(hits) - 40} 处（--list 看全部）", file=sys.stderr)
    print("[check_colors] 颜色只能来自 css/tokens.css 的变量", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
