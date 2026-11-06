"""P4-03 验收：精确合并（filter_complex concat）、规格统一、进度、取消清理。

验四件事：
1. 三份**规格互不相同**的素材（720p25 / 1080p30 / 无音轨 540p）能精确合并成一条，
   输出规格跟随第一份，音轨补齐（无音轨那份补静音）；
2. 同样的素材走 quick 在提交前就被拦住，并指出是哪几项不一致；
3. 进度按「已完成片段数 + 当前片段百分比」上报，最终到 1.0；
4. 长任务取消后不留半成品、不留临时目录。

素材现场生成：从 `build/accept/clips/short_60s.mp4` 切并转成三种规格，跑多少遍都一样。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_precise.py \\
        --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import subprocess
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
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


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


def wait_job(service, job_id: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    return service.jobs.get(job_id) or {}


def make_clip(
    ffmpeg: str,
    src: Path,
    dest: Path,
    *,
    start: str,
    dur: str,
    size: str,
    fps: str,
    audio: bool = True,
) -> Path:
    """切一段并转成指定规格。`audio=False` 用来造「没有音轨」的素材。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-ss",
        start,
        "-t",
        dur,
        "-i",
        str(src),
        "-vf",
        f"scale={size}:force_original_aspect_ratio=increase,crop={size}",
        "-r",
        fps,
        "-c:v",
        "libx264",
        "-crf",
        "26",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
    ]
    if audio:
        args += ["-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "1"]
    else:
        args += ["-an"]
    args.append(str(dest))
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"[fatal] 生成素材失败：{(proc.stderr or '')[-600:]}")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4-03 精确合并验收")
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args(argv)

    service = WorkbenchService(config_path=str(args.config), picker=None)
    try:
        ffmpeg, _ = service._ffmpeg.require()
        source = PROJ / "build" / "accept" / "clips" / "short_60s.mp4"
        if not source.is_file():
            raise SystemExit(f"[fatal] 缺样本 {source}")
        work = PROJ / "build" / "accept" / "merge_precise"
        if work.is_dir():
            for old in work.rglob("*"):
                if old.is_file():
                    old.unlink()

        a = make_clip(
            ffmpeg, source, work / "01.mp4", start="0", dur="4", size="1280:720", fps="25"
        )
        b = make_clip(
            ffmpeg, source, work / "02.mp4", start="12", dur="4", size="1920:1080", fps="30"
        )
        c = make_clip(
            ffmpeg,
            source,
            work / "03.mp4",
            start="24",
            dur="3",
            size="960:540",
            fps="24",
            audio=False,
        )
        paths = [str(a), str(b), str(c)]

        # 1. 预检：认得出三份可用、且确实不一致
        pre = call(service, "merge.preflight", {"paths": paths})
        check("预检认得出三份可用", pre.get("count") == 3, f"count={pre.get('count')}")
        check(
            "确实规格不一致",
            bool(pre.get("differences")),
            f"{len(pre.get('differences') or [])} 项",
        )
        check("预检因此禁止快速拼接", pre.get("can_quick") is False, str(pre.get("can_quick")))
        fields = {row.get("field") for row in pre.get("differences") or []}
        check(
            "差异点名到帧率与分辨率",
            {"width", "height", "fps"} <= fields,
            str(sorted(fields)),
        )
        check(
            "总时长约 11 秒",
            10_000 <= int(pre.get("total_ms") or 0) <= 12_500,
            f"{pre.get('total_ms')}ms",
        )

        # 2. 同样素材走 quick 被拦下
        blocked = error_of(service, "merge.start", {"paths": paths, "mode": "quick"})
        check("quick 被拦下", blocked.get("code") == "CONFLICT", str(blocked.get("code")))
        check(
            "拦下的理由是规格不一致",
            "规格不一致" in str(blocked.get("message") or ""),
            str(blocked.get("message")),
        )

        # 3. 精确合并成功
        started = call(
            service,
            "merge.start",
            {"paths": paths, "mode": "precise", "out_dir": str(work)},
        )
        target = Path(started["output"])
        job = wait_job(service, started["job"]["id"])
        check(
            "精确合并任务成功",
            job.get("status") == "succeeded",
            str(job.get("status")) + " " + str(job.get("error_message") or ""),
        )
        check("任务进度走到 1.0", float(job.get("progress") or 0) >= 1.0, str(job.get("progress")))
        check("成片存在", target.is_file(), str(target))
        result = job.get("result") or {}
        check("结果标成 precise", result.get("mode") == "precise", str(result.get("mode")))
        check(
            "没有走过回退", result.get("fallback_used") is False, str(result.get("fallback_used"))
        )

        info = service._ffmpeg.probe(target)
        check(
            "分辨率跟随第一份素材",
            (info.width, info.height) == (1280, 720),
            f"{info.width}x{info.height}",
        )
        check("帧率统一到 25", abs(float(info.fps or 0) - 25.0) < 0.2, f"{info.fps}")
        check("无音轨那份补出了音轨", bool(info.has_audio), f"acodec={info.acodec}")
        expected = int(started.get("expected_ms") or 0)
        delta = abs(int(info.duration_ms or 0) - expected)
        check(
            "总时长等于各段之和（误差 < 1000ms）",
            delta < 1000,
            f"实际 {info.duration_ms}ms / 期望 {expected}ms / 差 {delta}ms",
        )
        check("成片有体积", target.stat().st_size > 0, f"{target.stat().st_size} B")

        row = service.repo.merge_get(started["merge_id"])
        check(
            "记录状态 succeeded",
            (row or {}).get("status") == "succeeded",
            str((row or {}).get("status")),
        )
        check(
            "记录里存了三段素材",
            len((row or {}).get("items") or []) == 3,
            str(len((row or {}).get("items") or [])),
        )

        # 4. 取消：造一个 90 秒的 1080p 任务，跑起来立刻取消
        long_clips = [
            str(
                make_clip(
                    ffmpeg,
                    source,
                    work / f"long{i}.mp4",
                    start=str(4 * i),
                    dur="20",
                    size="1920:1080",
                    fps="30",
                )
            )
            for i in range(3)
        ]
        cancel_req = call(
            service,
            "merge.start",
            {"paths": long_clips, "mode": "precise", "out_dir": str(work)},
        )
        cancel_target = Path(cancel_req["output"])
        time.sleep(2.5)
        service.jobs.cancel(cancel_req["job"]["id"])
        canceled = wait_job(service, cancel_req["job"]["id"], timeout=90)
        check(
            "取消后任务状态是 canceled",
            canceled.get("status") == "canceled",
            str(canceled.get("status")),
        )
        check("取消不留半成品", not cancel_target.is_file(), str(cancel_target))
        check(
            "取消后记录标记 canceled",
            (service.repo.merge_get(cancel_req["merge_id"]) or {}).get("status") == "canceled",
            str((service.repo.merge_get(cancel_req["merge_id"]) or {}).get("status")),
        )
        tmp_root = Path(str(service.cfg.cache_root)) / "tmp"
        leftover = [p.name for p in tmp_root.iterdir()] if tmp_root.is_dir() else []
        check(
            "临时目录已清理",
            cancel_req["job"]["id"] not in leftover and started["job"]["id"] not in leftover,
            str(leftover[:4]),
        )

        # 5. 一致性回归：同规格素材走 quick 仍然成功（没有被精确的实现带坏）
        same = [
            str(
                make_clip(
                    ffmpeg,
                    source,
                    work / f"same{i}.mp4",
                    start=str(40 + 4 * i),
                    dur="3",
                    size="1280:720",
                    fps="25",
                )
            )
            for i in range(2)
        ]
        quick_req = call(
            service, "merge.start", {"paths": same, "mode": "quick", "out_dir": str(work)}
        )
        quick_job = wait_job(service, quick_req["job"]["id"])
        check(
            "同规格走 quick 仍然成功",
            quick_job.get("status") == "succeeded",
            str(quick_job.get("status")),
        )
        check(
            "quick 没有被误标成回退",
            (quick_job.get("result") or {}).get("fallback_used") is False,
            str((quick_job.get("result") or {}).get("fallback_used")),
        )
    except WBError as exc:
        check(f"抛出未预期的错误 {exc.code}", False, exc.message)
    finally:
        service.close()

    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    print()
    for ok, name, detail in results:
        if not ok:
            print(f"[FAIL] {name} — {detail}")
    print(f"P4-03 精确合并验收：{'全部通过' if passed == total else '有失败'}（{passed}/{total}）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
