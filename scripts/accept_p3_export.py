"""P3-05 验收：原比例导出（真 ffmpeg + 真库项目）。

验的是"导出的片子能不能播、时长对不对、快速模式的偏移说没说清"——这些只有真跑
ffmpeg 才知道，假 ffmpeg 只能证明我们拼对了参数（那部分在
`tests/wb/test_clip_export.py` 里已经覆盖 27 项）。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p3_export.py \
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

from video_workbench.app.service import WorkbenchService  # noqa: E402

FRAME_MS = 40
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


def wait_job(service, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    return service.jobs.get(job_id) or {}


def pick_project(service) -> dict:
    # library.list 回的是 {items,total,limit,offset}（早期写成 projects 会 KeyError）
    for item in call(service, "library.list")["items"]:
        # library.list 不返回 has_source，能导出的判据是"有 source 资产 + 有片长"
        if "source" in (item.get("kinds") or []) and int(item.get("duration_sec") or 0) > 0:
            if "short_60s" in str(item.get("title") or ""):
                return item
    raise SystemExit("[fatal] 库里没有 short_60s 项目，先跑 accept_p1_download.py")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args()

    service = WorkbenchService(config_path=Path(args.config))
    try:
        project = pick_project(service)
        pid = project["id"]
        root = Path(str(project["root_dir"]))
        print(f"[info] 项目 {pid} · {project.get('title')} · {project.get('duration_sec')}s")

        # 上一轮的成片留着会让"文件名带模式"这些断言读到 (2)(3)，先清干净
        export_dir = root / "export"
        if export_dir.is_dir():
            for old in export_dir.glob("*验收切片*"):
                old.unlink()
        # 用固定的手动候选：时长、起点都可控，脚本跑几遍结果一样
        for row in service.repo.candidate_list(pid):
            if str(row.get("title") or "") == "验收切片":
                service.repo.candidate_delete(row["id"])
        created = call(
            service,
            "clip.create",
            {"project_id": pid, "start_ms": 6_000, "end_ms": 26_040, "title": "验收切片"},
        )
        cand = created["candidate"]
        expected = int(cand["end_ms"]) - int(cand["start_ms"])
        print(f"[info] 候选 {cand['id']} · {cand['start_ms']}→{cand['end_ms']}（{expected}ms）")

        # ---- 玩法与参数门禁
        check(
            "未知导出模式被拦",
            (
                error_of(
                    service,
                    "clip.export",
                    {"project_id": pid, "candidate_id": cand["id"], "mode": "turbo"},
                )
                or {}
            ).get("code")
            == "VALIDATION",
        )
        # 竖屏在 P3-06 做了（本次矩阵跑出来这条老断言已过时）：改为断言
        # 「画幅/竖屏模式的非法值被拦」，仍然守住"参数校验不会静默放过"。
        check(
            "非法竖屏模式被拦",
            (
                error_of(
                    service,
                    "clip.export",
                    {
                        "project_id": pid,
                        "candidate_id": cand["id"],
                        "aspect": "9x16",
                        "vertical_mode": "swirl",
                    },
                )
                or {}
            ).get("code")
            == "VALIDATION",
        )
        check(
            "不存在的候选报 NOT_FOUND",
            (
                error_of(service, "clip.export", {"project_id": pid, "candidate_id": "cand_nope"})
                or {}
            ).get("code")
            == "NOT_FOUND",
        )

        # ---- 精确模式
        precise = call(
            service,
            "clip.export",
            {
                "project_id": pid,
                "candidate_id": cand["id"],
                "mode": "precise",
            },
        )
        job = wait_job(service, precise["job"]["id"])
        check(
            "精确导出任务成功",
            job.get("status") == "succeeded",
            f"{job.get('status')} {job.get('error_message') or ''}",
        )
        result = job.get("result") or {}
        out = Path(str(result.get("path") or ""))
        check(
            "精确模式产出了文件",
            out.is_file() and out.stat().st_size > 0,
            f"{out.name if out.name else '(空)'} {out.stat().st_size if out.is_file() else 0} B",
        )
        info = service._ffmpeg.probe(out) if out.is_file() else None
        actual = int(getattr(info, "duration_ms", 0) or 0)
        check(
            "精确模式时长误差不超过 1 帧",
            abs(actual - expected) <= FRAME_MS,
            f"期望 {expected}ms 实际 {actual}ms 差 {actual - expected}ms",
        )
        streams = service._ffmpeg.streams(out) if out.is_file() else []
        vcodecs = [s.get("codec_name") for s in streams if s.get("codec_type") == "video"]
        check("成片里有视频流（能播）", vcodecs == ["h264"], str(vcodecs))
        check(
            "精确模式不写偏移说明",
            int(result.get("offset_ms") or 0) <= FRAME_MS and not result.get("note"),
            f"offset={result.get('offset_ms')} note={result.get('note')!r}",
        )
        check(
            "文件名带项目名与切点",
            "accept" in out.name.lower() or "short_60s" in out.name,
            out.name,
        )
        check("文件名不含半角冒号", ":" not in out.name, out.name)
        check("文件名带模式后缀", "precise" in out.name, out.name)
        check("文件落在项目 export 目录下", out.parent == root / "export", str(out.parent))

        # ---- 快速模式
        fast = call(
            service,
            "clip.export",
            {
                "project_id": pid,
                "candidate_id": cand["id"],
                "mode": "fast",
            },
        )
        job2 = wait_job(service, fast["job"]["id"])
        check(
            "快速导出任务成功",
            job2.get("status") == "succeeded",
            f"{job2.get('status')} {job2.get('error_message') or ''}",
        )
        result2 = job2.get("result") or {}
        out2 = Path(str(result2.get("path") or ""))
        check("快速模式产出了文件", out2.is_file() and out2.stat().st_size > 0, out2.name)
        delta = int(result2.get("delta_ms") or 0)
        offset = int(result2.get("offset_ms") or 0)
        check(
            "快速模式报了时长偏差（正负都算）",
            offset == abs(delta),
            f"delta={delta}ms offset={offset}ms",
        )
        note = str(result2.get("note") or "")
        if delta:
            check("偏差写进了说明文字", str(abs(delta)) in note, note)
        else:
            check("偏差为 0 时也说明了原因", "关键帧" in note, note or "(空)")
        info2 = service._ffmpeg.probe(out2) if out2.is_file() else None
        actual2 = int(getattr(info2, "duration_ms", 0) or 0)
        check(
            "快速模式时长偏差与 ffprobe 一致",
            actual2 - expected == delta,
            f"probe {actual2} expected {expected} delta {delta}",
        )

        # ---- 硬字幕（项目里得有字幕文件）
        subs = [a for a in service.repo.asset_list(pid) if a.get("kind") == "subtitle"]
        if subs:
            hard = call(
                service,
                "clip.export",
                {
                    "project_id": pid,
                    "candidate_id": cand["id"],
                    "subtitle_mode": "hard",
                },
            )
            job3 = wait_job(service, hard["job"]["id"])
            check(
                "硬烧字幕任务成功",
                job3.get("status") == "succeeded",
                f"{job3.get('status')} {job3.get('error_message') or ''}",
            )
            out3 = Path(str((job3.get("result") or {}).get("path") or ""))
            check("硬烧成片存在", out3.is_file() and out3.stat().st_size > 0, out3.name)
            check(
                "硬烧与精确模式不同名（不覆盖）",
                out3.name != out.name,
                f"{out3.name} vs {out.name}",
            )
            info3 = service._ffmpeg.probe(out3) if out3.is_file() else None
            check(
                "硬烧成片时长也对（多一道滤镜不该改时长）",
                abs(int(getattr(info3, "duration_ms", 0) or 0) - expected) <= FRAME_MS,
                f"{getattr(info3, 'duration_ms', 0)} vs {expected}",
            )
        else:
            check("项目没有字幕文件时硬烧被拦", False, "库里这个项目没有 subtitle 资产")

        # ---- 队列
        rows = call(service, "clip.exports", {"project_id": pid})["exports"]
        mine = [r for r in rows if r.get("candidate_id") == cand["id"]]
        check("导出队列记下了这几次导出", len(mine) >= 3, f"{len(mine)} 条")
        check(
            "队列里的文件都在",
            all(r.get("exists") for r in mine),
            str([r.get("name") for r in mine if not r.get("exists")]),
        )
        check(
            "队列区分了模式",
            {r.get("mode") for r in mine} >= {"precise", "fast"},
            str(sorted({r.get("mode") for r in mine})),
        )
        succeeded = [r for r in mine if r.get("status") == "succeeded"]
        check(
            "成功的行有时长",
            all(int(r.get("duration_ms") or 0) > 0 for r in succeeded),
            str([r.get("duration_ms") for r in succeeded]),
        )

        # ---- 任务失败不留半截成功
        check("导出任务类型正确", job.get("type") == "clip.export", str(job.get("type")))
    finally:
        service.close()

    passed = sum(1 for ok, _, _ in results if ok)
    for ok, name, detail in results:
        mark = "ok  " if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    print()
    if passed == len(results):
        print("P3-05 切片导出验收：全部通过")
        return 0
    print(f"P3-05 切片导出验收：{len(results) - passed} 项失败 / 共 {len(results)} 项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
