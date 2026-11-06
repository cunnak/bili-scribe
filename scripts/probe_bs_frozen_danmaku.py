"""从 bili_scribe 打包产物里读出界面文件，验证弹幕配额控件真的进了包。

用法：

    .venv/Scripts/python.exe scripts/probe_bs_frozen_danmaku.py [EXE 路径]

默认读 ``dist/bili-scribe.exe``。不启动 GUI、不写任何文件：直接读单文件 exe 的
CArchive，取出 ``bili_scribe/app/static/index.html``，核对 ``DanmakuPatch`` 的
五个必填字段是否都有对应的 ``data-path`` 控件。

背景：设置页只遍历 ``[data-path]`` 组载荷，控件缺席会让 ``PUT /api/settings``
被 Pydantic 判 422（``collect.danmaku.hot_items/sample_items: Field required``）。
源码侧的回归测试在 ``tests/frontend_test.js``；这里守的是**打包版**那一层，
因为界面文件靠 ``scripts/build-exe.py`` 的 ``--add-data`` 塞进包，改了源码却
打了旧包是最难发现的一类回归。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]

# 与 bili_scribe/app/service.py 的 DanmakuPatch 五个必填字段逐一对齐。
REQUIRED = {
    "enabled",
    "also_with_subtitle",
    "max_items",
    "hot_items",
    "sample_items",
}
HTML_ENTRY = "bili_scribe/app/static/index.html"


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "bili-scribe.exe"
    if not exe.exists():
        print(f"✗ 没有产物：{exe}")
        return 1

    car = CArchiveReader(str(exe))
    names = [n for n in car.toc if n.replace("\\", "/").endswith("index.html")]
    target = next(
        (n for n in names if HTML_ENTRY in n.replace("\\", "/")),
        None,
    )
    print(f"EXE={exe}")
    print(f"HTML_ENTRIES={names}")
    if target is None:
        print(f"✗ CArchive 里没有 {HTML_ENTRY}")
        return 1

    raw = car.extract(target)
    if isinstance(raw, tuple):  # 旧版 API 返回 (ispkg, data)
        raw = raw[-1]
    text = raw.decode("utf-8", "replace")
    found = set(re.findall(r'data-path="collect\.danmaku\.([^"]+)"', text))

    print(f"HTML_BYTES={len(raw)}")
    print(f"REQUIRED={sorted(REQUIRED)}")
    print(f"IN_EXE   ={sorted(found)}")
    print(f"MISSING  ={sorted(REQUIRED - found)}")

    # `--add-data` 是原样拷贝，包里的界面文件应当和工作区源码逐字节一致。
    # 不一致 = 改了源码却没重新打包（或打进了旧版缓存），这时上面的控件检查
    # 可能碰巧还是绿的，但用户拿到的不是当前源码。
    source = ROOT / "bili_scribe" / "app" / "static" / "index.html"
    source_bytes = source.read_bytes() if source.exists() else b""
    same = bool(source_bytes) and raw == source_bytes
    print(f"SOURCE_BYTES={len(source_bytes)}")
    print(f"BYTE_IDENTICAL={same}")

    ok = REQUIRED <= found and same
    print("→ 打包版含全部弹幕配额控件，且与源码逐字节一致" if ok else "→ 打包版与源码不符")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
