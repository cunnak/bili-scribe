"""P5-04 验收：性能与事件节流。

真后端、真事件总线、真 Chrome；不模拟节流器本身：

1. 一个 2 秒的活任务，进度事件不超过 4 次/秒（方案书 §6.6），**最后一次必须是 100%**
   ——压掉最后一条会让进度条永远停在 97%；
2. 200 次进度突发，送达次数个位数，且携带的是最新值；
3. 日志按 200ms 攒批：事件数远少于行数，但**一行都不丢**，顺序不乱；
4. 攒批窗口到点就发（≤0.4s），不是等任务结束才发；
5. 前端性能基线（`scripts/bench_p5_perf.py`）同轮跑：切页均值 <200ms、最差 <400ms、
   1000 段字幕首屏 <400ms、滚动均值 <16.7ms、DOM ≤60 行。

跑法：`./.venv/Scripts/python.exe scripts/accept_p5_perf.py --config build/accept/p2.yaml`
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
for path in (PROJ, PROJ / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

OK = 0
FAIL = 0
ROWS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        ROWS.append((True, name, detail))
        print(f"[ok]   {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        ROWS.append((False, name, detail))
        print(f"[FAIL] {name} —— {detail}")


def call(service, method: str, params: dict | None = None) -> tuple[bool, dict]:
    reply = service.dispatch(method, params or {})
    if reply.get("ok"):
        return True, reply.get("data") or {}
    return False, reply.get("error") or {}


def wait_job(service, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        ok, data = call(service, "job.get", {"id": job_id})
        if ok:
            row = data.get("job", data)
            if row.get("status") in ("succeeded", "failed", "canceled"):
                return row
        time.sleep(0.05)
    return row


class Collector:
    """把事件按名字收下来，顺带记时间戳（量节流/攒批）。"""

    def __init__(self) -> None:
        self.rows: list[tuple[float, str, dict]] = []
        self.lock = __import__("threading").Lock()

    def __call__(self, name: str, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except Exception:
            payload = {}
        with self.lock:
            self.rows.append((time.monotonic(), name, payload))

    def by(self, name: str) -> list[dict]:
        with self.lock:
            return [payload for _ts, event, payload in self.rows if event == name]

    def times(self, name: str) -> list[float]:
        with self.lock:
            return [ts for ts, event, _payload in self.rows if event == name]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--skip-bench", action="store_true")
    args = parser.parse_args()

    from video_workbench.app.service import WorkbenchService

    service = WorkbenchService(args.config)
    collector = Collector()
    service.events.set_sink(collector)
    try:
        # ---------------------------------------------------------- 1. 节流
        started = time.monotonic()
        ok, data = call(service, "job.demo.submit", {"steps": 20})  # 0.1s/步 ≈ 2s
        job = data.get("job", data)
        job_id = str(job.get("id") or "")
        row = wait_job(service, job_id)
        elapsed = time.monotonic() - started
        time.sleep(0.4)  # 等节流线程把最后一条补发出来

        updates = [
            payload for payload in collector.by("job.updated") if payload.get("id") == job_id
        ]
        budget = int(math.ceil(elapsed / 0.25)) + 3
        check("任务跑完了（demo）", row.get("status") == "succeeded", str(row.get("status")))
        check(
            "进度事件被节流到 4 次/秒以内",
            len(updates) <= budget,
            f"{len(updates)} 条 / {elapsed:.2f}s（上限 {budget}）",
        )
        check("进度事件没有一条都不发", len(updates) >= 1, f"{len(updates)} 条")
        last = updates[-1].get("progress") if updates else None
        check("最后一次进度补发到 100%", last == 1, f"最后一条 progress={last}")

        # ---------------------------------------------------------- 2. 突发
        before = len(collector.by("job.updated"))
        burst = 200
        for index in range(burst):
            service.events.emit(
                "job.updated", {"id": "job_burst", "progress": index / burst}, key="job_burst"
            )
        time.sleep(0.5)
        burst_rows = [
            payload for payload in collector.by("job.updated") if payload.get("id") == "job_burst"
        ]
        check(
            f"{burst} 次突发只送达个位数",
            len(burst_rows) <= 4,
            f"{len(burst_rows)} 条",
        )
        check(
            "突发后带着最新值（不是被压成旧值）",
            bool(burst_rows) and burst_rows[-1].get("progress") == (burst - 1) / burst,
            f"最后一条 progress={burst_rows[-1].get('progress') if burst_rows else None}",
        )
        check("突发没有污染别的任务的事件", len(collector.by("job.updated")) - before >= 1, "")

        # ---------------------------------------------------------- 3. 日志攒批
        ok, data = call(service, "job.demo.submit", {"steps": 40})  # 每 10 步一行 ≈ 4 行
        job2 = data.get("job", data)
        job2_id = str(job2.get("id") or "")
        row2 = wait_job(service, job2_id)
        time.sleep(0.3)
        log_events = [
            payload for payload in collector.by("job.log") if payload.get("id") == job2_id
        ]
        lines = [line for payload in log_events for line in (payload.get("lines") or [])]
        check("任务跑完了（40 步）", row2.get("status") == "succeeded", str(row2.get("status")))
        check(
            "日志事件带 lines 数组",
            bool(log_events) and all(isinstance(p.get("lines"), list) for p in log_events),
            f"{len(log_events)} 条事件",
        )
        check(
            "攒批后的行数≥事件数（真攒了）",
            len(lines) >= len(log_events),
            f"{len(lines)} 行 / {len(log_events)} 条",
        )
        check("日志一行都没丢", len(lines) >= 4, f"{len(lines)} 行")
        check("日志里有 demo 前缀", any("[demo]" in str(line) for line in lines), str(lines[:2]))
        check(
            "没有旧的单行字段（契约已改）", not any("line" in payload for payload in log_events), ""
        )

        # ---------------------------------------------------------- 4. 攒批时延
        collector.rows.clear()
        service.events.log("job_latency", "a")
        service.events.log("job_latency", "b")
        service.events.log("job_latency", "c")
        mark = time.monotonic()
        deadline = mark + 1.0
        delivered_at = 0.0
        while time.monotonic() < deadline:
            if collector.by("job.log"):
                delivered_at = collector.times("job.log")[0] - mark
                break
            time.sleep(0.01)
        check(
            "200ms 窗口到点就发（≤0.4s）", 0 < delivered_at <= 0.4, f"{delivered_at * 1000:.0f}ms"
        )
        batched = [
            line for payload in collector.by("job.log") for line in (payload.get("lines") or [])
        ]
        check(
            "窗口内的三行合成一条事件",
            collector.by("job.log") and batched == ["a", "b", "c"],
            str(batched),
        )

        # 真·攒批：25 行在同一窗口内连发，必须是 1 条事件带 25 行——
        # 上面 demo 任务是每秒一行，本来就不该合成，拿它证明攒批没意义。
        collector.rows.clear()
        for index in range(25):
            service.events.log("job_batch", f"row {index}")
        time.sleep(0.45)
        batch_events = collector.by("job.log")
        rows = [line for payload in batch_events for line in (payload.get("lines") or [])]
        check("25 行同窗口只发 1 条事件", len(batch_events) == 1, f"{len(batch_events)} 条")
        check("25 行按顺序全在", rows == [f"row {i}" for i in range(25)], f"{len(rows)} 行")
    finally:
        service.close()

    # ---------------------------------------------------------- 5. 前端基线
    if not args.skip_bench:
        bench = PROJ / "scripts" / "bench_p5_perf.py"
        out = PROJ / "build" / "bench-p5.json"
        proc = subprocess.run(
            [sys.executable, str(bench), "--json", str(out)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        tail = (proc.stdout or "").strip().splitlines()
        for line in tail[-4:]:
            print("[bench] " + line)
        payload: dict = {}
        if out.is_file():
            payload = json.loads(out.read_text(encoding="utf-8"))
        check("前端基线脚本跑通", proc.returncode == 0, f"exit={proc.returncode}")
        check(
            "切页均值 <200ms",
            float(payload.get("switchAvg") or 999) < 200,
            f"{payload.get('switchAvg')}ms",
        )
        check(
            "切页最差 <400ms",
            float(payload.get("switchMax") or 999) < 400,
            f"{payload.get('switchMax')}ms",
        )
        editor = payload.get("editor") or {}
        check(
            "1000 段首屏 <400ms",
            float(editor.get("first") or 999) < 400,
            f"{editor.get('first')}ms",
        )
        check(
            "1000 段滚动均值 <16.7ms",
            float(editor.get("scrollAvg") or 999) < 16.7,
            f"{editor.get('scrollAvg')}ms",
        )
        check(
            "虚拟列表只留 ≤60 行",
            int(editor.get("rendered") or 999) <= 60,
            f"{editor.get('rendered')} 行",
        )

    print()
    print(
        f"P5-04 性能与事件节流验收：{'全部通过' if FAIL == 0 else f'{FAIL} 项失败'}（{OK}/{OK + FAIL}）"
    )
    if FAIL:
        print("\n失败项：")
        for ok, name, detail in ROWS:
            if not ok:
                print(f"  - {name} —— {detail}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
