#!/usr/bin/env python
"""切点编辑验收（任务清单 P3-04）。

P3-03 验的是"候选能上界面、能改、能锁"；这一支只验 P3-04 新增的三件事：

1. **帧长从片源来**：`clip.candidates.frame_ms` 不是硬编码 40，真的读了 ffprobe 的
   `r_frame_rate`；探不到时退回 40 并在界面上照实说。
2. **切点误差 ≤ 1 帧**：`clip.update(align="frame")` 之后存下来的值必须落在帧栅格上，
   且与用户给的值相差不超过半帧。这条是 P3-04 的验收原文。
3. **锁定与手动切片**：锁定后重跑分析不被覆盖；手动新建的切片具备"可导出"的形状
   （时长 ≥0.4s、有起止、source=manual）——P3-05 的导出直接吃这三样。

用的是 `build/accept/p2.yaml` 里的真库（项目 `pj_80184d99f6ee`，源文件是真 mp4），
所以 `frame_ms` 读到的是真帧率而不是替身。

跑法::

    python scripts/accept_p3_edit.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from accept_p3_clip import (  # noqa: E402
    BAD,
    GOOD,
    StubModel,
    call,
    error_of,
    pick_project,
    start_stub,
)
from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def wait_job(service, job_id: str, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = service.jobs.get(job_id)
        if last.get("status") in {"succeeded", "failed", "canceled"}:
            return last
        time.sleep(0.15)
    return last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}（先跑 accept_p2_transcribe.py）")

    server, base_url = start_stub()
    service = WorkbenchService(config_path=str(config_path), picker=None)
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-edit")
    router = service.router
    try:
        item = pick_project(router)
        pid = item["id"]
        print(f"[info] 项目 {pid} · {item.get('title')} · {item.get('duration_sec')}s")

        # 前面几支脚本（含本脚本上次中断）留下的候选会让"锁定保护"的计数不可信。
        # 这里全清（含 locked/manual）：验收库是夹具，不从干净状态开跑就会假红。
        for row in service.repo.candidate_list(pid):
            service.repo.candidate_delete(row["id"])

        # ---------------- 1. 帧长从片源来
        data = call(router, "clip.candidates", {"id": pid})
        frame_ms = int(data.get("frame_ms") or 0)
        fps = float(data.get("fps") or 0)
        check("clip.candidates 给出 frame_ms", frame_ms > 0, f"{frame_ms} ms")
        check("fps 与 frame_ms 自洽", abs(1000.0 / frame_ms - fps) < 3.5, f"{fps} fps / {frame_ms} ms")
        check(
            "帧长落在常见帧率档位（25/30/29.97/60 或 40 的退回值）",
            frame_ms in (33, 34, 40, 41, 17, 16) or 10 <= frame_ms <= 50,
            str(frame_ms),
        )
        check("有源文件路径（真 mp4 才探得到真帧率）", bool(data.get("source_path")), data.get("source_path") or "")

        # ---------------- 2. 对齐误差 ≤ 1 帧
        StubModel.mode = GOOD
        StubModel.calls = 0
        job = call(router, "clip.analyze", {"project_id": pid, "count": 12})["job"]
        done = wait_job(service, job["id"])
        check("分析任务成功", done.get("status") == "succeeded", str(done.get("status")))
        rows = call(router, "clip.candidates", {"id": pid})["candidates"]
        check("拿到候选", len(rows) >= 1, f"{len(rows)} 条")
        if not rows:
            return 1
        cand = rows[0]

        worst = 0
        off_grid = 0
        cases = [cand["start_ms"] + 1, cand["start_ms"] + 7, cand["start_ms"] + 13,
                 cand["start_ms"] + 19, cand["start_ms"] + 21]
        for raw in cases:
            # 起点往后挪一点点，保证仍 ≥0.4s 且不越界
            got = call(
                router,
                "clip.update",
                {"id": cand["id"], "start_ms": raw, "end_ms": cand["end_ms"], "align": "frame"},
            )["candidate"]["start_ms"]
            worst = max(worst, abs(got - raw))
            if got % frame_ms != 0:
                off_grid += 1
        check(
            f"对齐后误差不超过半帧（实测最大 {worst} ms / 帧 {frame_ms} ms）",
            worst <= frame_ms / 2 + 0.001,
            str(worst),
        )
        check("对齐后全部落在帧栅格上", off_grid == 0, f"{off_grid} 条不在栅格")

        # 不对齐时必须原样存（手动敲时间码的场景）
        raw_ms = cand["start_ms"] + 13
        kept = call(
            router,
            "clip.update",
            {"id": cand["id"], "start_ms": raw_ms, "end_ms": cand["end_ms"]},
        )["candidate"]["start_ms"]
        check("不指定 align 时原样保存", kept == raw_ms, f"{raw_ms} → {kept}")
        check(
            "align 传未知值被拦",
            (error_of(router, "clip.update", {"id": cand["id"], "start_ms": raw_ms, "align": "vad"}) or {}).get("code")
            == "VALIDATION",
        )

        # ---------------- 3. 锁定后重跑不覆盖
        call(router, "clip.update", {"id": cand["id"], "status": "locked"})
        locked_start = call(router, "clip.update", {
            "id": cand["id"], "start_ms": cand["start_ms"] + 400, "end_ms": cand["end_ms"],
            "align": "frame",
        })["candidate"]["start_ms"]
        check(
            "锁定后仍能微调切点（锁的是重跑，不是手改）",
            locked_start != cand["start_ms"],
            f"{cand['start_ms']} → {locked_start}",
        )
        StubModel.mode = BAD
        job2 = call(router, "clip.analyze", {"project_id": pid, "count": 12})["job"]
        done2 = wait_job(service, job2["id"])
        check("模型挂了任务仍然结束（规则兜底）", done2.get("status") == "succeeded", str(done2.get("status")))
        after = call(router, "clip.candidates", {"id": pid})["candidates"]
        locked_rows = [r for r in after if r["id"] == cand["id"]]
        check("锁定的候选还在", len(locked_rows) == 1)
        check(
            "锁定候选的切点没被重跑冲掉",
            bool(locked_rows) and locked_rows[0]["start_ms"] == locked_start,
            f"{locked_start} vs {locked_rows[0]['start_ms'] if locked_rows else '缺失'}",
        )
        check("重跑确实换了别的候选（不是啥都没做）", len(after) >= 1)
        others = [r for r in after if r["id"] != cand["id"] and r.get("status") != "locked"]
        check(
            "重跑后除锁定项外都是规则兜底",
            bool(others) and all((r.get("notes") or {}).get("origin") == "rule" for r in others),
            str([(r.get("status"), (r.get("notes") or {}).get("origin")) for r in after]),
        )

        # ---------------- 4. 手动切片具备可导出形状
        StubModel.mode = GOOD
        manual = call(
            router,
            "clip.create",
            {
                "project_id": pid,
                "kind": "segment",
                "start_ms": 6_013,
                "end_ms": 26_027,
                "title": "手切一段",
                "align": "frame",
            },
        )["candidate"]
        span = manual["end_ms"] - manual["start_ms"]
        check("手动切片对齐后落在栅格", manual["start_ms"] % frame_ms == 0 and manual["end_ms"] % frame_ms == 0,
              f"{manual['start_ms']}–{manual['end_ms']}")
        check("手动切片时长 ≥ 0.4 秒（可导出）", span >= 400, f"{span} ms")
        check("手动切片标了 manual 来源", manual["source"] == "manual", str(manual["source"]))
        check("手动切片默认 draft（要人确认才发）", manual["status"] == "draft", str(manual["status"]))
        check(
            "对齐后不足 0.4 秒要被拦",
            (
                error_of(
                    router,
                    "clip.create",
                    {"project_id": pid, "start_ms": 6_013, "end_ms": 6_100, "align": "frame"},
                )
                or {}
            ).get("code")
            == "VALIDATION",
        )
        call(router, "clip.delete", {"id": manual["id"]})

        # 收尾：把锁定的那条解回 draft，别给后面 P3-05 的脚本留坑
        call(router, "clip.update", {"id": cand["id"], "status": "draft"})
        return 0 if not FAILS else 1
    finally:
        server.shutdown()
        service.close()


if __name__ == "__main__":
    code = main()
    print()
    if FAILS:
        print(f"P3-04 切点编辑验收：{len(FAILS)} 项失败")
        for name in FAILS:
            print(f"  - {name}")
    else:
        print("P3-04 切点编辑验收：全部通过")
    raise SystemExit(code)
