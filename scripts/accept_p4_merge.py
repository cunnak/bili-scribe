"""P4-02 验收：快速拼接（真 ffmpeg）。

验三件事：
1. 同规格的三段能拼成一条，时长等于各段之和（误差 < 0.5 秒）；
2. 混进一个不同规格的文件时**在提交前**就被拦住，并且理由说得出是哪一项不一致；
3. 文件不在、只有一条时给出的拒绝理由与处置办法对应。

素材现场生成：从 `build/accept/clips/short_60s.mp4` 切三段并重编码成同一规格，
另做一个 720p 的版本用来制造真实差异。这样不依赖库里已有成片，跑多少遍都一样。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_merge.py \
        --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
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


def wait_job(service, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    return service.jobs.get(job_id) or {}


def make_clip(ffmpeg: str, src: Path, dest: Path, *, start: str, dur: str, scale: str) -> Path:
    """切一段并统一到目标规格（重编码，保证三段参数完全一致）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg, "-hide_banner", "-y",
        "-ss", start, "-t", dur, "-i", str(src),
        "-vf", f"scale={scale}:force_original_aspect_ratio=increase,crop={scale}",
        "-r", "30", "-c:v", "libx264", "-crf", "24", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        str(dest),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"[fatal] 生成素材失败：{(proc.stderr or '')[-600:]}")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4-02 快速拼接验收")
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args(argv)

    service = WorkbenchService(config_path=str(args.config), picker=None)
    try:
        ffmpeg, _ = service._ffmpeg.require()
        source = PROJ / "build" / "accept" / "clips" / "short_60s.mp4"
        if not source.is_file():
            raise SystemExit(f"[fatal] 缺样本 {source}")
        work = PROJ / "build" / "accept" / "merge"
        # 清掉上轮产物，免得 old 文件混进断言
        for old in work.glob("*.mp4"):
            old.unlink()

        a = make_clip(ffmpeg, source, work / "01.mp4", start="0", dur="4", scale="1280:720")
        b = make_clip(ffmpeg, source, work / "02.mp4", start="10", dur="4", scale="1280:720")
        c = make_clip(ffmpeg, source, work / "03.mp4", start="20", dur="4", scale="1280:720")
        odd = make_clip(ffmpeg, source, work / "hd.mp4", start="30", dur="4", scale="1920:1080")
        paths = [str(a), str(b), str(c)]

        # 1) 预检：三条同规格
        pre = call(service, "merge.preflight", {"paths": paths})
        check("预检认得出三条可用", pre.get("count") == 3, f"count={pre.get('count')}")
        check("同规格可以快速拼接", pre.get("can_quick") is True, f"differences={pre.get('differences')}")
        check("总时长约 12 秒", 11_000 <= int(pre.get("total_ms") or 0) <= 13_500, f"{pre.get('total_ms')}ms")

        # 2) 提交快速拼接
        started = call(service, "merge.start", {"paths": paths, "mode": "quick"})
        job = started.get("job") or {}
        check("提交后拿到任务", bool(job.get("id")), str(job.get("id")))
        final = wait_job(service, job.get("id") or "")
        check("任务成功", final.get("status") == "succeeded", str(final.get("status")) + " " + str(final.get("error_message") or ""))
        result = final.get("result") or {}
        out = Path(str(result.get("path") or ""))
        check("成片存在", out.is_file(), str(out))
        check("成片有体积", int(result.get("bytes") or 0) > 0, f"{result.get('bytes')} B")

        info = service._ffmpeg.probe(out)
        expected = int(pre.get("total_ms") or 0)
        delta = int(info.duration_ms or 0) - expected
        check("成片时长等于各段之和（误差 < 500ms）", abs(delta) < 500, f"实际 {info.duration_ms}ms / 期望 {expected}ms / 差 {delta}ms")
        check("成片分辨率跟随源", (info.width, info.height) == (1280, 720), f"{info.width}x{info.height}")
        check("成片保留音轨", bool(info.has_audio), f"acodec={info.acodec}")

        # 3) 合并记录
        rows = call(service, "merge.list")["items"]
        hit = [r for r in rows if r.get("id") == started.get("merge_id")]
        check("合并记录已入库", bool(hit), f"共有 {len(rows)} 条")
        check("记录状态 succeeded", bool(hit) and hit[0].get("status") == "succeeded", str(hit[0].get("status") if hit else ""))
        check("记录里存了三条素材", bool(hit) and len(hit[0].get("items") or []) == 3, str(len(hit[0].get("items") or []) if hit else 0))

        # 4) 规格不一致：提交前拦住
        err = error_of(service, "merge.start", {"paths": paths + [str(odd)], "mode": "quick"})
        check("不同规格被拦下", err.get("code") == "CONFLICT", str(err.get("code")))
        check("理由说清是规格不一致", "规格不一致" in str(err.get("message") or ""), str(err.get("message")))
        detail = json.loads(err.get("detail") or "[]")
        fields = {row.get("field") for row in detail}
        check("差异表点名具体字段", bool(fields & {"width", "height", "fps"}), str(sorted(fields)))

        # 5) 边界：只有一条 / 文件不在 / 空列表
        err = error_of(service, "merge.start", {"paths": [str(a)], "mode": "quick"})
        check("一条时拒绝并说清要两条", err.get("code") == "CONFLICT" and "至少" in str(err.get("message") or ""), str(err.get("message")))
        gone = work / "not-here.mp4"
        err = error_of(service, "merge.start", {"paths": [str(a), str(b), str(gone)], "mode": "quick"})
        check("文件不在能点名", "不在了" in str(err.get("message") or ""), str(err.get("message")))
        err = error_of(service, "merge.start", {"paths": [], "mode": "quick"})
        check("空列表报 VALIDATION", err.get("code") == "VALIDATION", str(err.get("code")))
        # P4-03 把精确合并做完了，这条老断言（曾是 UNSUPPORTED + "P4-03"）已过时：
        # 现在应当能正常提交。诚实性由 accept_p4_precise.py 断言（那里会真跑 ffmpeg）。
        submitted = call(service, "merge.start", {"paths": paths, "mode": "precise"})
        check("精确模式可以提交", bool((submitted.get("job") or {}).get("id")),
              str(submitted.get("job") or {}))
        wait_job(service, (submitted.get("job") or {}).get("id") or "", timeout=180)

        # 6) 同一批素材反复合并不会互相覆盖
        again = call(service, "merge.start", {"paths": paths, "mode": "quick", "out_name": out.stem})
        wait_job(service, again.get("job", {}).get("id") or "")
        check("同名再合并不会覆盖上一条", again.get("name") != out.name, f"{again.get('name')} vs {out.name}")

    finally:
        service.close()

    bad = [r for r in results if not r[0]]
    print()
    if bad:
        print(f"P4-02 快速拼接验收：{len(bad)} 项失败 / 共 {len(results)} 项")
        for _, name, detail in bad:
            print(f"  - {name}：{detail}")
        return 1
    print(f"P4-02 快速拼接验收：全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())