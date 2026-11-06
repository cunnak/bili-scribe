#!/usr/bin/env python
"""P5-04 前端性能基线：页面切换 + 1000 段字幕不卡。

为什么单独一支脚本：性能问题在截图里看不出来，在 pytest 里也量不出来
（jsdom 没有布局与合成）。这里用真 Chrome + CDP 量两件用户能感觉到的事：

    switch   切页耗时（点左栏到新页面画完一帧）
    editor   1000 段字幕的首屏耗时与滚动耗时

判定线：

    switchAvg < 200ms    —— 一帧 16.7ms，切页允许慢 12 帧（含 RPC mock 往返）
    switchMax < 400ms    —— 任何一次都不该慢到"以为没点上"
    first     < 400ms    —— 1000 段首屏
    scrollAvg < 16.7ms   —— 滚动必须还在 60fps 预算内
    rendered  <= 60      —— DOM 里只留视口附近的几十行（虚拟列表没退化成全量）

跑法::

    ./.venv/Scripts/python.exe scripts/bench_p5_perf.py
    ./.venv/Scripts/python.exe scripts/bench_p5_perf.py --json build/bench-p5.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import websockets  # noqa: E402
from shoot_wb import PROJ, Cdp, close_tab, find_chrome, free_port, new_tab  # noqa: E402

INDEX = PROJ / "video_workbench" / "web" / "index.html"

# 左栏的六个入口 + 字幕页（从素材库进，不在左栏）
ROUTES: list[tuple[str, dict]] = [
    ("newtask", {}),
    ("library", {}),
    ("summary", {}),
    ("clipper", {}),
    ("merge", {}),
    ("publish", {}),
    ("settings", {}),
    # 字幕页不在左栏：从素材库带 project 进去
    ("subtitles", {"project": "p_demo_1"}),
]

SWITCH_SCRIPT = """
(async () => {
  const routes = %(routes)s;
  const measureOne = async (route) => {
    const args = %(args)s;
    const t0 = performance.now();
    WB.router.go(route, args[route] || {});
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    return performance.now() - t0;
  };
  // 预热：首轮要下载/编译脚本、建 DOM，不该算进用户感知的切页时间
  for (const [route] of routes) await measureOne(route);

  const rows = [];
  for (const [route] of routes) {
    const samples = [];
    for (let i = 0; i < 3; i += 1) samples.push(await measureOne(route));
    const round = (v) => Math.round(v * 10) / 10;
    rows.push({
      route: route,
      avg: round(samples.reduce((a, b) => a + b, 0) / samples.length),
      max: round(Math.max.apply(null, samples))
    });
  }
  return JSON.stringify(rows);
})()
"""

EDITOR_SCRIPT = """
(async () => {
  const count = %(count)d;
  const page = WB.pages.subtitles;
  WB.router.go('subtitles', { project: 'p_demo_1' });
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  const segments = [];
  for (let i = 0; i < count; i += 1) {
    segments.push({
      id: 'bench' + i,
      start_ms: i * 1600,
      end_ms: i * 1600 + 1500,
      text: '第 ' + i + ' 句，一行大概这么长',
      words: []
    });
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

  const round = (v) => Math.round(v * 10) / 10;
  return JSON.stringify({
    count: count,
    first: round(first),
    scrollAvg: round(frames.reduce((a, b) => a + b, 0) / frames.length),
    scrollMax: round(Math.max.apply(null, frames)),
    rendered: document.querySelectorAll('.sub-row').length
  });
})()
"""


def _spawn_chrome(port: int) -> subprocess.Popen:
    chrome = find_chrome()
    profile = PROJ / "build" / f"cdp-perf-{port}"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
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


async def measure(count: int, timeout: float) -> dict:
    port = free_port()
    proc = _spawn_chrome(port)
    tabs: list[str] = []
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as resp:
                    resp.read()
                break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            raise SystemExit("Chrome 没起来")

        # 只带 ?mock=1：写死 #/subtitles 会被 boot 规范回素材库
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
                  for (let i = 0; i < 120; i++) {
                    if (window.WB && WB.router && WB.pages && WB.pages.subtitles) return true;
                    await new Promise(r => setTimeout(r, 100));
                  }
                  return false;
                })()
                """
            )
            if not ready:
                raise SystemExit("页面没起来（WB 命名空间不齐）")
            await asyncio.sleep(0.6)

            routes = [[name] for name, _ in ROUTES]
            args = {name: payload for name, payload in ROUTES}
            raw = await cdp.js(
                SWITCH_SCRIPT % {"routes": json.dumps(routes), "args": json.dumps(args)}
            )
            switches = json.loads(raw or "[]")

            raw = await cdp.js(EDITOR_SCRIPT % {"count": count})
            editor = json.loads(raw or "{}")

        for tab_id in tabs:
            close_tab(port, tab_id)
        return {"switch": switches, "editor": editor}
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
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    data = asyncio.run(measure(args.count, args.timeout))
    switches = data["switch"]
    editor = data["editor"]

    avg_all = round(sum(row["avg"] for row in switches) / max(1, len(switches)), 1)
    max_all = round(max((row["max"] for row in switches), default=0), 1)
    print(
        "[bench] 切页："
        + "，".join(f"{row['route']} {row['avg']}/{row['max']}ms" for row in switches)
    )
    print(f"[bench] 切页总览：均值 {avg_all}ms，最差 {max_all}ms")
    print(
        f"[bench] 字幕 {editor.get('count')} 段：首屏 {editor.get('first')}ms，"
        f"滚动均值 {editor.get('scrollAvg')}ms，最差 {editor.get('scrollMax')}ms，"
        f"DOM 行数 {editor.get('rendered')}"
    )

    ok = (
        avg_all < 200
        and max_all < 400
        and float(editor.get("first") or 0) < 400
        and float(editor.get("scrollAvg") or 0) < 16.7
        and int(editor.get("rendered") or 0) <= 60
    )
    payload = {
        "switch": switches,
        "switchAvg": avg_all,
        "switchMax": max_all,
        "editor": editor,
        "ok": ok,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "[bench] "
        + ("PASS" if ok else "FAIL")
        + "（切页均值 <200ms、最差 <400ms、首屏 <400ms、滚动 <16.7ms、DOM ≤60 行）"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
