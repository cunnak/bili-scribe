#!/usr/bin/env python
"""字幕编辑器的性能基线（任务清单 P2-03 验收："1000 条不卡"）。

为什么要有这个脚本：虚拟列表写错时的典型症状是"看起来能用，滚起来粘手"，
而这在截图里完全看不出来。这里用真 Chrome 量两个数：

    first   —— 一次性把 1000 段塞进 DOM 的耗时（首屏）
    scroll  —— 连滚 20 帧，每帧从 scroll 事件到下一帧的平均/最差耗时

判定线（60fps 的一帧 = 16.7ms）：
    first  < 400ms     —— 首屏允许一帧半的抖动，但不应超过 0.4s
    scroll < 16.7ms    —— 滚动必须还在 60fps 预算内

跑法::

    python scripts/bench_p2_editor.py
    python scripts/bench_p2_editor.py --count 3000 --json build/bench.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import websockets  # noqa: E402
from shoot_wb import (  # noqa: E402
    PROJ,
    Cdp,  # noqa: E402
    close_tab,
    find_chrome,
    free_port,
    new_tab,
)

INDEX = PROJ / "video_workbench" / "web" / "index.html"

SCRIPT = """
(async () => {
  const count = %(count)d;
  const page = WB.pages.subtitles;
  const segments = [];
  for (let i = 0; i < count; i += 1) {
    segments.push({ id: 'bench' + i, start_ms: i * 1600, end_ms: i * 1600 + 1500,
                    text: '第 ' + i + ' 句，一行大概这么长', words: [] });
  }
  page.state.segments = segments;
  page.state.qc = {};

  const t0 = performance.now();
  page._internals.renderRows(true);
  const first = performance.now() - t0;

  const scroller = document.getElementById('sub-scroll');
  const frames = [];
  for (let k = 0; k < 20; k += 1) {
    const begin = performance.now();
    scroller.scrollTop = k * 400;
    scroller.dispatchEvent(new Event('scroll'));
    await new Promise((r) => requestAnimationFrame(r));
    frames.push(performance.now() - begin);
  }

  const rendered = document.querySelectorAll('.sub-row').length;
  const total = document.querySelectorAll('.sub-row').length;
  return JSON.stringify({
    count: count,
    first: Math.round(first * 10) / 10,
    scrollAvg: Math.round((frames.reduce((a, b) => a + b, 0) / frames.length) * 10) / 10,
    scrollMax: Math.round(Math.max.apply(null, frames) * 10) / 10,
    rendered: total
  });
})()
"""


async def measure(count: int, timeout: float) -> dict:
    chrome = find_chrome()
    port = free_port()
    profile = PROJ / "build" / f"cdp-bench-{port}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1360,860",
            "--force-device-scale-factor=1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        tabs = []
        while asyncio.get_event_loop().time() < deadline:
            try:
                import urllib.request

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as resp:
                    resp.read()
                break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            raise SystemExit("Chrome 没起来")

        # 只带 ?mock=1，路由在页面就绪后用 router.go 走一次：
        # 写死 #/subtitles 会被 app.js 的 boot 规范回素材库（路由表还没注册完）。
        url = INDEX.as_uri() + "?mock=1"
        tab = new_tab(port, url)
        tabs.append(tab["id"])
        async with websockets.connect(
            tab["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024
        ) as sock:
            cdp = Cdp(sock)
            await cdp.cmd("Runtime.enable")
            await cdp.cmd("Page.enable")
            ready = await cdp.js(
                """
                (async () => {
                  for (let i = 0; i < 100; i++) {
                    if (window.WB && WB.router && WB.pages && WB.pages.subtitles) return true;
                    await new Promise(r => setTimeout(r, 100));
                  }
                  return false;
                })()
                """
            )
            if not ready:
                raise SystemExit("页面没起来（WB 命名空间不齐）")
            await cdp.js("window.WB.router.go('subtitles', {project: 'p_demo_1'})")
            await asyncio.sleep(1.0)
            raw = await cdp.js(SCRIPT % {"count": count})
        for tab_id in tabs:
            close_tab(port, tab_id)
        if not raw:
            raise SystemExit("页面没返回测量结果（编辑器没就绪）")
        return json.loads(raw)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    data = asyncio.run(measure(args.count, args.timeout))
    print(
        f"[bench] {data['count']} 段：首屏 {data['first']}ms，"
        f"滚动均值 {data['scrollAvg']}ms，最差 {data['scrollMax']}ms，"
        f"DOM 中实际行数 {data['rendered']}"
    )

    ok = data["first"] < 400 and data["scrollAvg"] < 16.7 and data["rendered"] <= 60
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "[bench] " + ("PASS" if ok else "FAIL") + "（首屏 <400ms，滚动均值 <16.7ms，DOM 行数 ≤60）"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
