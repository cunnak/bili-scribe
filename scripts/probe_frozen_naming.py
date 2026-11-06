"""从打包产物里把冻结的字节码捞出来，验证命名/封面那套新代码真的进了包。

不启动 GUI、不下载视频：直接读 dist/VideoWorkbench/VideoWorkbench.exe 里的
CArchive → PYZ → 模块 code object，检查关键函数名与常量是否存在。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "VideoWorkbench" / "VideoWorkbench.exe"

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

KEYS = {
    "source_stem",
    "finalize_source",
    "find_source",
    "SOURCE_STAGING",
    "asset_upsert_singleton",
    "normalize_cover",
    "COVER_HOSTS",
    "source.mp4",
}

WANT = {
    "video_workbench.core.layout": {
        "source_stem",
        "finalize_source",
        "find_source",
        "SOURCE_STAGING",
    },
    "video_workbench.core.resolver": {"normalize_cover", "COVER_HOSTS"},
    "video_workbench.infra.repo": {"asset_upsert_singleton"},
    "video_workbench.workers.download_worker": {
        "source_stem",
        "finalize_source",
        "find_source",
        "asset_upsert_singleton",
    },
    "video_workbench.core.transcriber": {"find_source"},
}


def walk(code, out: set[str]) -> None:
    stack = [code]
    while stack:
        cur = stack.pop()
        for n in getattr(cur, "co_names", ()):
            if n in KEYS:
                out.add(n)
        for c in getattr(cur, "co_consts", ()):
            if hasattr(c, "co_names"):
                stack.append(c)
            elif isinstance(c, str) and c in KEYS:
                out.add(c)


def main() -> int:
    if not EXE.exists():
        print(f"✗ 没有产物：{EXE}")
        return 1
    car = CArchiveReader(str(EXE))
    pyz_names = [n for n in car.toc if n.endswith(".pyz")]
    if not pyz_names:
        print(f"✗ CArchive 里没找到 PYZ，现有条目示例：{list(car.toc)[:8]}")
        return 1
    pyz = car.open_embedded_archive(pyz_names[0])
    toc = getattr(pyz, "toc", None) or {}
    print(f"PYZ={pyz_names[0]}  模块数={len(toc)}")
    bad = 0
    for mod, required in WANT.items():
        if mod not in toc:
            print(f"✗ 缺模块 {mod}")
            bad += 1
            continue
        try:
            code = pyz.extract(mod)
            if isinstance(code, tuple):  # 旧版返回 (ispkg, code)
                code = code[-1]
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 解出 {mod} 失败：{exc}")
            bad += 1
            continue
        found: set[str] = set()
        walk(code, found)
        missing = required - found
        flag = "✓" if not missing else "✗"
        print(f"{flag} {mod} 命中={sorted(found)} 缺={sorted(missing)}")
        if missing:
            bad += 1
    print("→ 打包版含本次全部改动" if bad == 0 else f"→ 有 {bad} 项不符")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
