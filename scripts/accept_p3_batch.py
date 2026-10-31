"""P3-09 验收：批量导出（真 ffmpeg + 真库项目）。

验的是"多选能不能一起出去、一条失败会不会拖垮别人、重试能不能补上"。
为了让失败**可控**（不能靠碰运气），脚本在进程内把 `service._ffmpeg.export_clip`
包一层：命中指定候选的输出名就抛 `PROCESS_FAILED`，其余照常交给真 ffmpeg。
这不是给产品开后门——验收结束就还原，真正出片的仍是 ffmpeg。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p3_batch.py \
        --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.app.service import WorkbenchService  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, str(detail)))


def call(service, method: str, params: dict | None = None) -> dict:
    out = service.dispatch(method, params or {})
    if not out.get("ok"):
        raise SystemExit(f"[fatal] {method} 失败：{out.get('error')}")
    return out["data"]


def error_of(service, method: str, params: dict | None = None) -> dict:
    out = service.dispatch(method, params or {})
    if out.get("ok"):
        raise SystemExit(f"[fatal] {method} 本该失败却成功了")
    return out.get("error") or {}


def wait_job(service, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    return service.jobs.get(job_id) or {}


def pick_project(service) -> dict:
    for item in call(service, "library.list")["items"]:
        if "source" in (item.get("kinds") or []) and int(item.get("duration_sec") or 0) > 0:
            if "short_60s" in str(item.get("title") or ""):
                return item
    raise SystemExit("[fatal] 库里没有 short_60s 项目，先跑 accept_p1_download.py")


def make_candidates(service, pid: str, titles: list[str]) -> list[dict]:
    made = []
    for index, title in enumerate(titles):
        start = 4_000 + index * 6_000
        row = call(
            service,
            "clip.create",
            {"project_id": pid, "kind": "highlight", "start_ms": start,
             "end_ms": start + 5_000, "title": title},
        )["candidate"]
        made.append(row)
    return made


class FlakyFFmpeg:
    """真 ffmpeg 外面套一层"指定名字必失败"，用来验部分失败语义。"""

    def __init__(self, inner, bad_titles: tuple[str, ...]):
        self._inner = inner
        self._bad = bad_titles

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def export_clip(self, src, dest, **kwargs):
        name = Path(str(dest)).name
        if any(bad and bad in name for bad in self._bad):
            raise WBError("PROCESS_FAILED", "ffmpeg 执行失败（exit 1）", "验收用的受控失败",
                          retryable=True)
        return self._inner.export_clip(src, dest, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--batch", type=int, default=3)
    args = parser.parse_args()

    service = WorkbenchService(config_path=Path(args.config))
    try:
        project = pick_project(service)
        pid = project["id"]
        root = Path(str(project["root_dir"]))
        export_dir = root / "export"
        print(f"[info] 项目 {pid} · {project.get('title')} · {project.get('duration_sec')}s")

        # 清场：上一轮的验收切片与成片都删掉，否则文件名会带 (2)(3)，断言分不清谁是谁
        for row in call(service, "clip.candidates", {"id": pid})["candidates"]:
            if str(row.get("title") or "").startswith("批量验收"):
                call(service, "clip.delete", {"id": row["id"]})
        if export_dir.is_dir():
            for path in export_dir.glob("*批量验收*"):
                path.unlink(missing_ok=True)

        titles = [f"批量验收{index + 1}" for index in range(args.batch)]
        made = make_candidates(service, pid, titles)
        ids = [row["id"] for row in made]
        check("造出待导出的切片", len(made) == args.batch, f"{len(made)} 条")

        # ---- 1. 空选：挡在提交前
        blank = error_of(service, "clip.export.batch", {"project_id": pid, "candidate_ids": []})
        check("空选被拦", blank.get("code") == "VALIDATION", blank.get("code", ""))

        # ---- 2. 正常批量：三条一起出，共用 batch_id
        first = call(service, "clip.export.batch",
                     {"project_id": pid, "candidate_ids": ids, "mode": "precise"})
        check("三条一起提交", first["submitted"] == 3, str(first["submitted"]))
        check("共用一个 batch_id", str(first["batch_id"]).startswith("batch_"), first["batch_id"])
        jobs = {row["candidate_id"]: row["job_id"] for row in first["jobs"]}
        payloads = [service.jobs.get(job_id)["payload"] for job_id in jobs.values()]
        check("每条都带 batch_id",
              {p.get("batch_id") for p in payloads} == {first["batch_id"]},
              str(len(payloads)))

        settled = {cid: wait_job(service, job_id) for cid, job_id in jobs.items()}
        check("三条全部导出成功",
              all(job.get("status") == "succeeded" for job in settled.values()),
              str([job.get("status") for job in settled.values()]))
        paths = {cid: Path(str((job.get("result") or {}).get("path") or ""))
                 for cid, job in settled.items()}
        check("三条成片都在盘上", all(p.is_file() and p.stat().st_size > 0 for p in paths.values()),
              str([p.name for p in paths.values()]))
        check("文件名互不相同", len({p.name for p in paths.values()}) == 3,
              str(sorted({p.name for p in paths.values()})))

        # ffprobe 复核成片时长（精确模式误差 ≤1 帧）
        probe = service._ffmpeg
        durations = []
        for candidate_id, path in paths.items():
            info = probe.probe(str(path))
            cand = service.repo.candidate_get(candidate_id)
            want = int(cand["end_ms"]) - int(cand["start_ms"])
            durations.append((want, int(info.duration_ms)))
        worst = max(abs(want - got) for want, got in durations)
        check("精确模式时长误差 ≤ 1 帧", worst <= 40, f"最大差 {worst} ms")

        # ---- 3. 部分失败：第二条受控失败，其余照出
        for path in export_dir.glob("*批量验收*"):
            path.unlink(missing_ok=True)
        inner = service._ffmpeg
        service._ffmpeg = FlakyFFmpeg(inner, ("批量验收2",))
        try:
            second = call(service, "clip.export.batch",
                          {"project_id": pid, "candidate_ids": ids, "mode": "fast"})
            jobs2 = {row["candidate_id"]: row["job_id"] for row in second["jobs"]}
            settled2 = {cid: wait_job(service, job_id) for cid, job_id in jobs2.items()}
        finally:
            service._ffmpeg = inner
        statuses = {cid: job.get("status") for cid, job in settled2.items()}
        failed_ids = [cid for cid, status in statuses.items() if status == "failed"]
        check("恰好一条失败", len(failed_ids) == 1, str(statuses))
        check("失败的是指定那条", failed_ids == [ids[1]],
              f"失败 {failed_ids} / 期望 {[ids[1]]}")
        check("其余两条不受影响",
              sum(1 for s in statuses.values() if s == "succeeded") == 2, str(statuses))

        rows = call(service, "clip.exports", {"project_id": pid})["exports"]
        mine = [row for row in rows if row.get("batch_id") == second["batch_id"]]
        check("队列能看到这一批", len(mine) == 3, f"{len(mine)} 行")
        check("失败行带错误说明",
              any(row["status"] == "failed" and row["error"] for row in mine),
              str([row.get("error") for row in mine if row["status"] == "failed"]))
        check("成功行的文件真的在",
              sum(1 for row in mine if row["status"] == "succeeded" and row["exists"]) == 2,
              str([row.get("exists") for row in mine]))

        # ---- 4. 重试失败那条（新建任务，失败历史留着）
        retried = call(service, "job.retry", {"id": jobs2[ids[1]]})
        job3 = (retried or {}).get("job") or retried
        done = wait_job(service, job3["id"])
        check("重试成功", done.get("status") == "succeeded", str(done.get("status")))
        rows = call(service, "clip.exports", {"project_id": pid})["exports"]
        mine = [row for row in rows if row.get("batch_id") == second["batch_id"]]
        check("重试后这批三条都成功",
              sum(1 for row in mine if row["status"] == "succeeded") == 3,
              str([row["status"] for row in mine]))
        check("失败的旧记录还在（历史不删）",
              sum(1 for row in rows if row["status"] == "failed") >= 1,
              str([row["status"] for row in rows].count("failed")))

        # ---- 5. skip_exported：同参数不重复导；换模式算新的一份
        skipped = call(service, "clip.export.batch",
                       {"project_id": pid, "candidate_ids": ids, "mode": "fast",
                        "skip_exported": True})
        check("同参数跳过已导出的", skipped["submitted"] == 0, str(skipped["submitted"]))
        check("跳过原因写清了",
              {row["reason"] for row in skipped["skipped"]} == {"这条已经导出过（同参数）"},
              str([row["reason"] for row in skipped["skipped"]]))
        other = call(service, "clip.export.batch",
                     {"project_id": pid, "candidate_ids": ids, "mode": "precise",
                      "skip_exported": True})
        check("换模式仍会重新导", other["submitted"] == 3, str(other["submitted"]))
        for row in other["jobs"]:
            wait_job(service, row["job_id"])

        # ---- 6. 未知候选被跳过而不是整批崩
        mixed = call(service, "clip.export.batch",
                     {"project_id": pid, "candidate_ids": [ids[0], "cand_nope"],
                      "mode": "precise", "skip_exported": False})
        check("未知候选只跳过自己", mixed["submitted"] == 1, str(mixed["submitted"]))
        check("未知候选有原因",
              mixed["skipped"] and mixed["skipped"][0]["reason"] == "候选不存在",
              str(mixed["skipped"]))
        wait_job(service, mixed["jobs"][0]["job_id"])

        # 清场：验收用的切片与成片不留
        for row in made:
            call(service, "clip.delete", {"id": row["id"]})
        for path in export_dir.glob("*批量验收*"):
            path.unlink(missing_ok=True)
    finally:
        service.close()

    ok = sum(1 for flag, _, _ in results if flag)
    print("\n[结果]")
    for flag, name, detail in results:
        print(f"  {'ok  ' if flag else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    print(f"\n{ok}/{len(results)} 项通过")
    if ok != len(results):
        print("P3-09 批量导出验收：有失败项")
        return 1
    print("P3-09 批量导出验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
