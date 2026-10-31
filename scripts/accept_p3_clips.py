#!/usr/bin/env python
"""切片台 RPC 验收（任务清单 P3-03）。

上一支 `accept_p3_clip.py` 验的是 `clipper.generate` 这个纯函数；这一支验的是
**界面真正会走的那条路**：`clip.analyze` 提交任务 → 候选落库 → `clip.candidates`
读回 → `clip.update` 改切点 → 锁定后重跑不被冲掉 → 手动新建/删除。

为什么要跑真任务而不是直接调 handler：`clip.analyze` 会 `candidate_clear()`
再 `candidate_insert()`，并 emit `clip.updated`；如果只在内存里算一遍，
"重跑冲掉了锁定的候选"这种事故一条都测不出来。

假模型复用 `accept_p3_clip.StubModel`（三种回法：正常 / 500 / 纯文字），
所以两支脚本对"模型给了什么"的口径永远一致。

跑法::

    python scripts/accept_p3_clips.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}（先跑 accept_p2_transcribe.py）")

    server, base_url = start_stub()
    service = WorkbenchService(config_path=str(config_path), picker=None)
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-clip")
    router = service.router

    try:
        item = (
            call(router, "library.get", {"id": args.project})["project"]
            if args.project
            else pick_project(router)
        )
        pid = item["id"]

        # 上一支脚本可能留下候选：先清空，保证每次从同一起点开跑
        for row in service.repo.candidate_list(pid):
            service.repo.candidate_delete(row["id"])

        # ---------------- 1. 空库时的读接口
        first = call(router, "clip.candidates", {"id": pid})
        duration = int(first["duration_ms"])
        check(
            "读到项目与片长",
            bool(first["project"]["id"]) and duration > 0,
            f"{pid} · {duration} ms",
        )
        check("没有候选时返回空列表而不是报错", first["candidates"] == [], f"{len(first['candidates'])} 条")
        check("磁吸边界来自字幕", len(first["boundaries"]) >= 4, f"{len(first['boundaries'])} 个切点")
        check("带字幕标记为真", first["has_transcript"] is True)
        check("给了源文件路径（界面要喂 <video>）", bool(first["source_path"]), first["source_path"])

        # ---------------- 2. 跑一次分析任务
        StubModel.mode = GOOD
        StubModel.calls = 0
        StubModel.users.clear()
        submitted = call(router, "clip.analyze", {"project_id": pid, "count": 12})
        job = submitted["job"]
        check("clip.analyze 返回任务", job.get("type") == "clip.analyze", str(job.get("id")))
        done = wait_job(service, job["id"])
        check("任务成功结束", done.get("status") == "succeeded", str(done.get("status")))
        result = done.get("result") or {}
        check("模型这条路走通了（source=llm）", result.get("source") == "llm", str(result.get("source")))

        listing = call(router, "clip.candidates", {"id": pid})
        rows = listing["candidates"]
        # 60 秒片 + 每条 20 秒高光 + 50% 重叠去重 → 只剩 1 条是**正确**结果
        # （P3-02 已确认过的取舍）。所以这里不硬凑 3–8 条，而是验：
        # 去重真的发生了、留下的这批两两不超 50% 重叠。
        check("候选至少留下 1 条", len(rows) >= 1, f"{len(rows)} 条")
        check("模型给的多条被去重（dropped>0）", int(result.get("dropped") or 0) > 0, str(result.get("dropped")))
        check(
            "留下的候选两两重叠不超过 50%",
            all(
                min(a["end_ms"], b["end_ms"]) - max(a["start_ms"], b["start_ms"]) <= 0
                or (min(a["end_ms"], b["end_ms"]) - max(a["start_ms"], b["start_ms"]))
                / min(a["end_ms"] - a["start_ms"], b["end_ms"] - b["start_ms"]) < 0.5
                for i, a in enumerate(rows)
                for b in rows[i + 1:]
            ),
            str([(r["start_ms"], r["end_ms"]) for r in rows]),
        )
        check(
            "分数降序",
            [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True),
            str([r["score"] for r in rows]),
        )
        check(
            "每条都有起止且落在片长内",
            all(0 <= r["start_ms"] < r["end_ms"] <= duration for r in rows),
            str([(r["start_ms"], r["end_ms"]) for r in rows]),
        )
        check("每条都标了来源 origin=llm", all((r.get("notes") or {}).get("origin") == "llm" for r in rows))
        check(
            "每条都留着模型原值（raw_start_ms）",
            all("raw_start_ms" in (r.get("notes") or {}) for r in rows),
        )
        check(
            "切点要么落在磁吸边界上，要么写明为什么没吸上",
            all(
                (r["start_ms"] in listing["boundaries"] and r["end_ms"] in listing["boundaries"])
                or bool((r.get("notes") or {}).get("snap_notes"))
                for r in rows
            ),
            str([
                (r["start_ms"] in listing["boundaries"], r["end_ms"] in listing["boundaries"],
                 (r.get("notes") or {}).get("snap_notes"))
                for r in rows
            ]),
        )

        # ---------------- 3. 改切点
        target = rows[0]
        new_start = int(target["start_ms"]) + 1_000
        updated = call(
            router,
            "clip.update",
            {"id": target["id"], "start_ms": new_start, "end_ms": target["end_ms"]},
        )["candidate"]
        check("改起点后回读一致", updated["start_ms"] == new_start, f"{updated['start_ms']}")
        check("改过切点会留痕 edited_ms", (updated.get("notes") or {}).get("edited_ms") is True)
        check(
            "原值仍保留，事后还能对照模型意见",
            (updated.get("notes") or {}).get("raw_start_ms") == target["start_ms"],
            str((updated.get("notes") or {}).get("raw_start_ms")),
        )

        too_short = error_of(
            router, "clip.update", {"id": target["id"], "start_ms": new_start, "end_ms": new_start + 100}
        )
        check("切点太短被拦", too_short.get("code") == "VALIDATION", str(too_short.get("message")))

        over = call(
            router,
            "clip.update",
            {"id": target["id"], "start_ms": new_start, "end_ms": duration + 60_000},
        )["candidate"]
        check("终点越界被夹回片长", over["end_ms"] <= duration, f"{over['end_ms']} <= {duration}")

        bad_status = error_of(router, "clip.update", {"id": target["id"], "status": "published"})
        check("非法状态被拦", bad_status.get("code") == "VALIDATION", str(bad_status.get("message")))

        # ---------------- 4. 锁定后重跑不被冲掉
        call(router, "clip.update", {"id": target["id"], "status": "locked"})
        StubModel.mode = BAD  # 让模型挂掉，走规则兜底：顺带验 origin=rule
        StubModel.calls = 0
        job2 = call(router, "clip.analyze", {"project_id": pid})["job"]
        done2 = wait_job(service, job2["id"])
        check("模型挂了任务仍成功（规则兜底）", done2.get("status") == "succeeded", str(done2.get("status")))
        after = call(router, "clip.candidates", {"id": pid})["candidates"]
        locked = [r for r in after if r["id"] == target["id"]]
        check("锁定的候选活下来了", len(locked) == 1 and locked[0]["status"] == "locked")
        check(
            "锁定的切点没被重跑改成别的值",
            bool(locked) and locked[0]["start_ms"] == new_start,
            f"{locked[0]['start_ms'] if locked else '—'}",
        )
        others = [r for r in after if r["id"] != target["id"]]
        check("其余候选被替换成规则结果", len(others) >= 1 and all((r.get("notes") or {}).get("origin") == "rule" for r in others))
        check("规则兜底的候选不再假装有分数含义", all(int(r["score"]) == 0 or r["score"] < 60 for r in others), str([r["score"] for r in others]))

        # ---------------- 5. 手动新建与删除
        manual = call(
            router,
            "clip.create",
            {"project_id": pid, "kind": "highlight", "start_ms": 1_000, "end_ms": 21_000, "title": "手动切片"},
        )["candidate"]
        check("手动新建落库", manual.get("source") == "manual", str(manual.get("id")))
        check("手动候选有默认状态 draft", manual.get("status") == "draft")
        again = call(router, "clip.candidates", {"id": pid})["candidates"]
        check("手动候选出现在列表里", any(r["id"] == manual["id"] for r in again))

        removed = call(router, "clip.delete", {"id": manual["id"]})
        check("删除成功", removed.get("deleted") == manual["id"], json.dumps(removed, ensure_ascii=False))
        rest = call(router, "clip.candidates", {"id": pid})["candidates"]
        check("删除后列表里没了", all(r["id"] != manual["id"] for r in rest))

        # ---------------- 6. 错误路径
        missing = error_of(router, "clip.candidates", {"id": "pj_does_not_exist"})
        check("未知项目 NOT_FOUND", missing.get("code") == "NOT_FOUND", str(missing.get("message")))
        ghost = error_of(router, "clip.update", {"id": "cand_deadbeefdead", "status": "draft"})
        check("未知候选 NOT_FOUND", ghost.get("code") == "NOT_FOUND", str(ghost.get("message")))
    finally:
        service.close()
        server.shutdown()

    print()
    if FAILS:
        print(f"P3-03 切片台验收：{len(FAILS)} 项失败 → {FAILS}")
        return 1
    print("P3-03 切片台验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
