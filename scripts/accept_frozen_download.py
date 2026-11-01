"""用打包出来的 exe 当 yt-dlp 宿主，真跑一次下载，验「打包态 argv + 管道解析」这条路。

手法：把本进程伪装成 frozen（sys.frozen=True、sys.executable=dist 里的 exe），
于是 downloader 会走 [exe, --ytdlp] 前缀——和打包版 GUI 里完全同一条命令。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXE = ROOT / "dist" / "VideoWorkbench" / "VideoWorkbench.exe"
URL = "https://www.bilibili.com/video/BV1GJ411x7h7"


def main() -> int:
    if not EXE.exists():
        print("没有打包产物：", EXE)
        return 2
    import video_workbench.core.downloader as D

    out = ROOT / "build" / "_probe_frozen"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    events: list[dict] = []

    real_exe, real_frozen = sys.executable, getattr(sys, "frozen", None)
    sys.frozen = True  # type: ignore[attr-defined]
    sys.executable = str(EXE)
    try:
        res = D.download(url=URL, home=out, quality="720P", on_event=events.append, need_bytes=0)
    finally:
        if real_frozen is None:
            try:
                del sys.frozen  # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            sys.frozen = real_frozen  # type: ignore[attr-defined]
        sys.executable = real_exe

    prog = [e for e in events if e.get("type") == "progress"]
    meta = [e for e in events if e.get("type") in {"meta", "wb-meta"}]
    print("[events] 共 %d 条，progress %d 条，meta %d 条" % (len(events), len(prog), len(meta)))
    if prog:
        print("[events] 进度抽样：", [round(p.get("percent", 0), 1) for p in prog[:5]], "…",
              [round(p.get("percent", 0), 1) for p in prog[-2:]])
    if meta:
        print("[events] meta 抽样：", meta[-1])
    print("[result] file=%s height=%s format=%s events=%d elapsed=%.1fs" % (
        Path(res.path).name if res.path else None, res.height, res.format_id, res.events, res.elapsed_s))
    if res.stderr_tail:
        print("[stderr 尾] ", res.stderr_tail[-300:])
    files = sorted(p.name for p in out.iterdir())
    print("[files] ", files)
    ok = bool(res.path) and Path(res.path).exists() and len(prog) > 0 and res.height > 0
    print("[verdict]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
