"""P5-02 验收用的"会被强杀的子进程"。

它做的事和真实使用一致：起一个真后端、挑一个项目、提交一次真导出任务，
把任务 id 打到 stdout，然后睡下去——父进程读走 id 之后直接 kill 掉它，
模拟「用户点了导出，然后任务管理器结束进程 / 断电 / 强行关窗」。

这个文件要留在仓里（不是临时脚本）：验收脚本要真的去起它。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 这个脚本用 `python scripts/p502_child.py` 起，sys.path[0] 是 scripts/，
# 因此要自己把仓库根塞进来才能 import 到 video_workbench。
_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402


def call(service, method, params):
    return service.router.dispatch(method, params, "child")["data"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="build/accept/p2.yaml")
    args = ap.parse_args()

    service = WorkbenchService(config_path=args.config)
    try:
        items = call(service, "library.list", {"limit": 100})["items"]
        pick = None
        for item in items:
            kinds = item.get("kinds") or []
            if "source" in kinds and (item.get("duration_sec") or 0) > 1:
                pick = item
                break
        if pick is None:
            print("NO_PROJECT", flush=True)
            return 2

        cand = call(
            service,
            "clip.create",
            {
                "project_id": pick["id"],
                "kind": "highlight",
                "start_ms": 0,
                "end_ms": int((pick.get("duration_sec") or 60) * 1000),
                "title": "P5-02 强杀验收",
            },
        )
        candidate_id = (cand.get("candidate") or cand).get("id")
        job = call(
            service,
            "clip.export",
            {"project_id": pick["id"], "candidate_id": candidate_id, "mode": "precise"},
        )["job"]
        print(f"JOB {job['id']}", flush=True)
        print(f"CAND {candidate_id}", flush=True)
        print("READY", flush=True)
        # 父进程此刻会杀掉整棵进程树。睡得够久，保证杀的时候任务还在跑。
        time.sleep(300)
        return 0
    finally:
        try:
            service.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
