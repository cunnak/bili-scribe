"""P5-01 真机验收：取消要真把进程杀掉、临时文件要清、失败要留日志。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p5_cancel.py --config build/accept/p2.yaml

和别的验收脚本不同，这里**故意跑真 ffmpeg**：取消是不是真的生效，只有看进程表才算数。
用替身的话，"取消了但 ffmpeg 还在写文件"这种情况永远测不出来。
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

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.infra.ffmpeg import FFmpeg  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))


def call(service, method: str, params: dict | None = None) -> tuple[bool, dict]:
    env = service.dispatch(method, params or {})
    return bool(env.get("ok")), env.get("data") if env.get("ok") else env.get("error")


def alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return str(pid) in (out.stdout or "")


def ffmpeg_pids() -> set[int]:
    """当前所有 ffmpeg 进程的 pid。取消之后不该多出新的。"""
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    pids: set[int] = set()
    for line in (out.stdout or "").splitlines():
        parts = [p for p in line.split() if p]
        if len(parts) >= 2 and parts[0].lower().startswith("ffmpeg"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def wait_job(service, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        ok, data = call(service, "job.get", {"id": job_id})
        if ok:
            row = data
            if row.get("status") in ("succeeded", "failed", "canceled"):
                return row
        time.sleep(0.05)
    return row


def pick_project(service) -> dict:
    ok, data = call(service, "library.list", {"limit": 200})
    rows = data.get("items") if ok else []
    for row in rows or []:
        if "source" in (row.get("kinds") or []) and float(row.get("duration_sec") or 0) > 3:
            return row
    raise SystemExit("没有可用项目（先跑 accept_p2_*）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(PROJ / "build/accept/p2.yaml"))
    args = ap.parse_args()
    os.environ.setdefault("BILI_SCRIBE_CONFIG", args.config)

    service = WorkbenchService(config_path=args.config)
    try:
        # ------------------------------------------------ A. FFmpeg.run 取消
        before = ffmpeg_pids()
        ff = FFmpeg(timeout=120)
        source = PROJ / "build/accept/clips/short_60s.mp4"
        if not source.is_file():
            raise SystemExit(f"缺少样片 {source}")
        slow = [
            ff.ffmpeg or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-re",
            "-i",
            str(source),
            "-t",
            "60",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-an",
            str(PROJ / "build/accept/clips/_cancel_probe.mp4"),
        ]
        started: list[int] = []
        t0 = time.monotonic()

        def should_cancel() -> bool:
            return time.monotonic() - t0 > 1.2

        canceled = False
        try:
            ff.run(slow, should_cancel=should_cancel, on_proc=lambda p: started.append(p.pid))
        except WBError as exc:
            canceled = exc.code == "CANCELED"
        spent = time.monotonic() - t0
        check("A1 真 ffmpeg 命令能取消", canceled, f"{spent:.1f} 秒返回")
        check("A2 取消在命令自然结束前返回", spent < 20, f"{spent:.1f} 秒")
        time.sleep(0.6)
        check(
            "A3 取消后进程真的没了",
            bool(started) and not alive(started[0]),
            f"pid={started[0] if started else '—'}",
        )
        leaked = ffmpeg_pids() - before
        check("A4 没有留下孤儿 ffmpeg", not leaked, f"新增 {sorted(leaked)}")
        probe = PROJ / "build/accept/clips/_cancel_probe.mp4"
        if probe.exists():
            probe.unlink(missing_ok=True)

        # ------------------------------------------------ B. 导出取消 + 半成品
        project = pick_project(service)
        pid = project["id"]
        ok, created = call(
            service,
            "clip.create",
            {
                "project_id": pid,
                "kind": "highlight",
                "start_ms": 0,
                "end_ms": int(min(60000, float(project.get("duration_sec") or 60) * 1000)),
                "title": "P5-01 取消验收",
            },
        )
        candidate_id = (created or {}).get("id") or (created or {}).get("candidate", {}).get("id")
        check("B0 造出一条候选", bool(candidate_id), str(candidate_id))

        ok, project_detail = call(service, "library.get", {"id": pid})
        root = Path(str((project_detail or {}).get("project", {}).get("root_dir") or ""))
        export_dir = root / "export"
        before_files = {f.name for f in export_dir.glob("*.mp4")} if export_dir.is_dir() else set()

        ok, sub = call(
            service,
            "clip.export",
            {
                "project_id": pid,
                "candidate_id": candidate_id,
                "mode": "precise",
            },
        )
        job_id = (sub or {}).get("job", {}).get("id", "")
        check("B1 导出任务已提交", bool(job_id), job_id)
        time.sleep(0.8)
        ok, canceled_row = call(service, "job.cancel", {"id": job_id})
        check("B2 取消请求被接受", ok, "")
        row = wait_job(service, job_id, timeout=45)
        check("B3 任务落到 canceled", row.get("status") == "canceled", row.get("status", ""))
        check(
            "B4 取消也保存了日志",
            bool((row.get("result") or {}).get("log")),
            f"{len((row.get('result') or {}).get('log') or [])} 行",
        )
        ok, exports = call(service, "clip.exports", {"project_id": pid})
        rows = [
            r for r in (exports or {}).get("items", []) if r.get("candidate_id") == candidate_id
        ]
        leftover = [r for r in rows if r.get("status") == "succeeded"]
        check("B5 取消的导出没被记成成功", not leftover, f"{len(leftover)} 条 succeeded")
        after_files = {f.name for f in export_dir.glob("*.mp4")} if export_dir.is_dir() else set()
        new_files = sorted(after_files - before_files)
        check("B6 取消没在导出目录留下半成品", not new_files, f"新增 {new_files}")

        # ------------------------------------------------ C. 取消后重试
        ok, retried = call(service, "job.retry", {"id": job_id})
        new_id = (retried or {}).get("id", "")
        check("C1 重试起了新任务", bool(new_id) and new_id != job_id, new_id)
        done = wait_job(service, new_id, timeout=180)
        check("C2 重试成功", done.get("status") == "succeeded", done.get("status", ""))
        result = done.get("result") or {}
        check(
            "C3 成片时长与期望相符",
            abs(int(result.get("delta_ms") or 0)) <= 400,
            f"delta={result.get('delta_ms')}",
        )
        out = Path(str(result.get("path") or ""))
        check(
            "C4 成片在盘上且非空",
            out.is_file() and out.stat().st_size > 0,
            f"{out.name} {out.stat().st_size if out.is_file() else 0} B",
        )

        # ------------------------------------------------ D. 失败保留日志
        # 造一次"提交之后文件被移走"的失败：这是真会发生的事（用户手动删了成片），
        # 而且失败发生在 handler 里——它才会写日志、才会落 failed。
        scratch = Path(service.cfg.storage.cache_root) / "tmp" / "p501"
        scratch.mkdir(parents=True, exist_ok=True)
        copy_a = scratch / "a.mp4"
        copy_b = scratch / "b.mp4"
        data = Path(str(result.get("path") or "")).read_bytes()
        copy_a.write_bytes(data)
        copy_b.write_bytes(data)
        ok, sub2 = call(
            service, "merge.start", {"paths": [str(copy_a), str(copy_b)], "mode": "precise"}
        )
        job2 = (sub2 or {}).get("job", {}).get("id", "")
        check("D1 会失败的任务已提交", bool(job2), job2)
        copy_b.unlink(missing_ok=True)  # 提交后把第二个片段移走
        row2 = wait_job(service, job2, timeout=120)
        check("D2 失败任务状态是 failed", row2.get("status") == "failed", row2.get("status", ""))
        logs = (row2.get("result") or {}).get("log") or []
        check("D3 失败任务带日志", bool(logs), f"{len(logs)} 行")
        check(
            "D4 失败原因可读",
            "b.mp4" in str(row2.get("error_message") or "")
            or "失败" in str(row2.get("error_message") or ""),
            str(row2.get("error_message"))[:60],
        )
        for leftover in (copy_a, copy_b):
            leftover.unlink(missing_ok=True)
        try:
            scratch.rmdir()
        except OSError:
            pass

        # ------------------------------------------------ E. 合并取消 + tmp 清理
        ok, pre = call(
            service,
            "merge.preflight",
            {
                "paths": [str(result.get("path") or ""), str(result.get("path") or "")],
            },
        )
        same = (pre or {}).get("can_quick")
        check("E0 同一文件两段可快速拼接", bool(same), str((pre or {}).get("consistent")))
        ok, started_merge = call(
            service,
            "merge.start",
            {
                "paths": [str(result.get("path") or "")] * 2,
                "mode": "precise",
            },
        )
        merge_job = (started_merge or {}).get("job", {}).get("id", "")
        check("E1 合并任务已提交", bool(merge_job), merge_job)
        time.sleep(0.6)
        call(service, "job.cancel", {"id": merge_job})
        mrow = wait_job(service, merge_job, timeout=60)
        check("E2 合并能取消", mrow.get("status") == "canceled", mrow.get("status", ""))
        tmp_root = Path(service.cfg.storage.cache_root) / "tmp" / merge_job
        check("E3 任务临时目录已清", not tmp_root.exists(), str(tmp_root))
        ok, merges = call(service, "merge.list")
        failed = [
            m
            for m in (merges or {}).get("items", [])
            if m.get("id") == (started_merge or {}).get("merge_id")
        ]
        check(
            "E4 合并记录没被标成功",
            not failed or failed[0].get("status") != "succeeded",
            failed[0].get("status") if failed else "无记录",
        )
    finally:
        service.close()

    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    print("-" * 72)
    print(f"{passed}/{len(CHECKS)} 项通过")
    if passed == len(CHECKS):
        print("P5-01 取消/重试/进程清理验收：全部通过")
        return 0
    print("P5-01 取消/重试/进程清理验收：有失败项")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
