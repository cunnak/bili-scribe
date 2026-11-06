"""P5-02 重启恢复验收：真强杀一个进程，再看重启后的状态。

为什么必须真杀：这条需求本身就是"进程非正常死亡之后怎么办"，
用替身或直接改库都测不到真实现场——库里的 `running` 行、写在半截的 summary.md、
还有可能残留的子进程，只有真杀一次才齐。

跑法::

    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p5_recovery.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core.downloader import kill_tree  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""), flush=True)


def call(service, method, params=None):
    env = service.router.dispatch(method, params or {}, "accept")
    return env["data"] if env.get("ok") else None, env.get("error") or {}


def ffmpeg_pids() -> set[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p for p in line.split() if p.strip()]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def seed_stage_rows(service, media_root: Path) -> tuple[str, str]:
    """造两个"停在 summarized"的项目：一个产物在、一个产物没了。"""
    repo = service.repo
    keep_root = media_root / "恢复" / "有总结"
    lost_root = media_root / "恢复" / "缺总结"
    for root in (keep_root, lost_root):
        root.mkdir(parents=True, exist_ok=True)

    keep = repo.project_create(title="P5-02 有总结", root_dir=str(keep_root))
    repo.project_update(keep["id"], stage="summarized")
    repo.summary_insert(keep["id"], content={"one_line": "在"}, content_md="# 在", role="standard")
    md = keep_root / "summary.md"
    md.write_text("# 在\n", encoding="utf-8")
    repo.asset_upsert(keep["id"], "summary", str(md))

    lost = repo.project_create(title="P5-02 缺总结", root_dir=str(lost_root))
    repo.project_update(lost["id"], stage="summarized")
    repo.summary_insert(
        lost["id"], content={"one_line": "没了"}, content_md="# 没了", role="standard"
    )
    # 不写 summary.md：模拟"库里有行、文件没写出来"
    return keep["id"], lost["id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="build/accept/p2.yaml")
    args = ap.parse_args()

    before_ffmpeg = ffmpeg_pids()

    # ---------------------------------------------------------------- 1. 起子进程、提交任务、强杀
    child = subprocess.Popen(
        [sys.executable, str(PROJ / "scripts" / "p502_child.py"), "--config", args.config],
        cwd=str(PROJ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    job_id = ""
    deadline = time.time() + 120
    while time.time() < deadline:
        line = child.stdout.readline()
        if not line:
            if child.poll() is not None:
                break
            continue
        line = line.strip()
        if line.startswith("JOB "):
            job_id = line[4:].strip()
        if line == "READY":
            break
    check("S1 子进程提交了导出任务", bool(job_id), job_id)

    if not job_id:
        child.kill()
        return 2

    time.sleep(1.5)  # 让 ffmpeg 真的开始跑
    kill_tree(child.pid)
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
    time.sleep(1.0)
    check("S2 子进程已被强行结束", child.returncode is not None, f"rc={child.returncode}")

    after_ffmpeg = ffmpeg_pids()
    left = after_ffmpeg - before_ffmpeg
    check("S3 强杀没有留下孤儿 ffmpeg", not left, f"新增 {sorted(left)}")

    # ---------------------------------------------------------------- 2. 重新打开：恢复跑完
    service = WorkbenchService(config_path=args.config)
    try:
        info = service.router.dispatch("app.info", {}, "accept")["data"]
        stats = info.get("recovery") or {}
        check(
            "R1 app.info 带出 recovery 统计",
            "jobs" in stats,
            str({k: stats.get(k) for k in ("jobs", "merges", "projects")}),
        )
        check(
            "R2 至少收尾了一个中断任务",
            int(stats.get("jobs") or 0) >= 1,
            f"jobs={stats.get('jobs')}",
        )

        job = service.repo.job_get(job_id)
        check("R3 那个任务不再是 running", job.get("status") == "failed", str(job.get("status")))
        check(
            "R4 错误码是 INTERRUPTED",
            job.get("error_code") == "INTERRUPTED",
            str(job.get("error_code")),
        )
        check(
            "R5 文案说清了是上次退出导致",
            "上次退出" in (job.get("error_message") or ""),
            str(job.get("error_message"))[:40],
        )

        # ------------------------------------------------------------ 3. 阶段回退
        keep_id, lost_id = seed_stage_rows(service, Path(service.cfg.media_root))
        again = WorkbenchService(config_path=args.config)
        try:
            rolled = (
                again.router.dispatch("app.info", {}, "accept")["data"].get("recovery") or {}
            ).get("rolled") or []
            from_ids = {row.get("project_id") for row in rolled}
            keep_row = again.repo.project_get(keep_id)
            lost_row = again.repo.project_get(lost_id)
            check(
                "E1 产物齐全的项目保持 summarized",
                keep_row["stage"] == "summarized",
                keep_row["stage"],
            )
            check("E2 缺产物的项目被回退", lost_row["stage"] != "summarized", lost_row["stage"])
            check(
                "E3 回退写明了原因",
                "启动检查" in (lost_row.get("error") or ""),
                str(lost_row.get("error"))[:40],
            )
            check("E4 回退记录里能看到这个项目", lost_id in from_ids, str(sorted(from_ids))[:80])
        finally:
            again.close()

        # ------------------------------------------------------------ 4. 重试
        ok, err = call(service, "job.retry", {"id": job_id})
        # job.retry 直接把新任务交出来（不是 {job: ...}）
        retry_job = ((ok or {}).get("job") or ok or {}).get("id", "")
        check("T1 中断的任务可以重试", bool(retry_job), retry_job or str(err)[:60])
        if retry_job:
            final = None
            deadline = time.time() + 300
            while time.time() < deadline:
                final = service.repo.job_get(retry_job)
                if final and final.get("status") in {"succeeded", "failed", "canceled"}:
                    break
                time.sleep(0.5)
            check(
                "T2 重试跑成功了",
                (final or {}).get("status") == "succeeded",
                str((final or {}).get("status")),
            )
            result = (final or {}).get("result") or {}
            path = Path(str(result.get("path") or ""))
            check(
                "T3 重试真的产出了文件",
                path.is_file() and path.stat().st_size > 0,
                f"{path.name} {path.stat().st_size if path.is_file() else 0} B",
            )
    finally:
        service.close()

    total = len(RESULTS)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("-" * 72)
    print(f"{passed}/{total} 项通过")
    if passed != total:
        print("P5-02 重启恢复验收：有失败项")
        return 1
    print("P5-02 重启恢复验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
