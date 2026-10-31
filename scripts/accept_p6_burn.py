#!/usr/bin/env python
"""P6 真链路实测撞出来的坑：烧字幕到底烧上去没有。

`scripts/accept_p3_export.py` 验的是「时长对、能解码、切点准」——字幕没烧上
也照样全绿。这里补上两个真实失败：

1. libass 只吃 ASS/SSA。代码把项目的 `.srt` 直接喂给 `ass=` 滤镜，ffmpeg 报
   `Could not create a libass track` 然后 exit -22（P3/P6 的验收脚本都不传
   `subtitle`，所以一路全绿）。
2. 切片的 `-ss` 只作用于视频输入，成片时间轴从 0 开始；字幕文件里还是源时间
   （比如 3000 秒）。结果任务显示"烧字幕"成功，画面上一个字都没有。软字幕轨
   同理，要 `-itsoffset` 把字幕轨一起挪回来。

素材是合成的纯黑底：画面里唯一的亮部就是字幕，"有没有字"直接量平均亮度，
不用人眼看也不用 OCR。

跑法：``python scripts/accept_p6_burn.py --config config.yaml``
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import subtitles  # noqa: E402
from video_workbench.infra.ffmpeg import FFmpeg, subtitle_filter  # noqa: E402

FAILS: list[str] = []


def ok(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


def call(service, method: str, params: dict) -> dict:
    out = service.dispatch(method, params)
    if not out.get("ok"):
        raise AssertionError(f"{method} → {out.get('error')}")
    return out["data"]


def wait_job(service, job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.3)
    service.jobs.cancel(job_id)
    raise SystemExit(f"任务超时：job={job_id}")


def bright_at(ffmpeg_bin: str, video: Path, at_s: float) -> int:
    """某一秒画面上"亮像素"的个数。

    素材是纯黑底，唯一的亮部就是字。平均亮度会被整帧摊平（几个汉字只剩 0.2，
    看不出差别），数像素才对：有字几千个、没字接近 0。
    """
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-ss", f"{at_s:.3f}",
         "-i", str(video), "-frames:v", "1", "-vf", "format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return -1
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    return int((arr > 60).sum())


def gray_frame(ffmpeg_bin: str, video: Path, at_s: float):
    """取一帧灰度原始像素，返回 (数组, 高)。失败返回 (None, 0)。"""
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-ss", f"{at_s:.3f}",
         "-i", str(video), "-frames:v", "1", "-vf", "format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None, 0
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    size = len(arr)
    for height in (1920, 720, 360):          # 素材/成片两种画幅都覆盖
        width = size // height
        if width * height == size:
            return arr.reshape(height, width), height
    return None, 0


def changed_at(ffmpeg_bin: str, a: Path, b: Path, at_s: float,
               rows=slice(None)) -> int:
    """同一时刻两帧的差异像素数（阈值 24，避开编码抖动）。

    这是亮度法的替代品：白底画面上「亮像素」恒定很多，看不出有没有字；
    差分只问一件事——这块区域的像素有没有被字幕改掉。
    """
    fa, _ = gray_frame(ffmpeg_bin, a, at_s)
    fb, _ = gray_frame(ffmpeg_bin, b, at_s)
    if fa is None or fb is None or fa.shape != fb.shape:
        return -1
    diff = np.abs(fa.astype(np.int16) - fb.astype(np.int16))
    return int((diff[rows] > 24).sum())

def ass_times(body: str) -> list[tuple[int, int]]:
    out = []
    for m in re.finditer(r"Dialogue:\s*\d+,(\d+:\d+:\d+\.\d\d),(\d+:\d+:\d+\.\d\d)", body):
        out.append((_to_ms(m.group(1)), _to_ms(m.group(2))))
    return out


def _to_ms(text: str) -> int:
    h, m, rest = text.split(":")
    s, cs = rest.split(".")
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(cs) * 10


def first_srt_start_ms(text: str) -> int:
    for line in text.splitlines():
        if "-->" in line:
            h, m, rest = line.split("-->")[0].strip().split(":")
            s, ms = rest.replace(",", ".").split(".")
            return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)
    return -1


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 字幕上屏验收")
    parser.add_argument("--config", required=True)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    home = ROOT / "build" / "accept" / "p6_burn"
    if home.exists() and not args.keep:
        shutil.rmtree(home, ignore_errors=True)
    (home / "media").mkdir(parents=True, exist_ok=True)

    ffmpeg_obj = FFmpeg()
    if not ffmpeg_obj.available:
        print("[skip] 没找到 ffmpeg，这条验不了（不算失败）")
        return 0
    tools = ffmpeg_obj.ffmpeg

    # ---- 1) 滤镜按扩展名选，特殊字符按 libass 规则转义
    srt_filter = subtitle_filter("a/b/transcript.srt")
    ok("srt 走 subtitles=（走 ass= 会 exit -22）", srt_filter.startswith("subtitles="),
       srt_filter)
    ok("ass/ssa 走 ass=", subtitle_filter("x.ass").startswith("ass=")
       and subtitle_filter("x.SSA").startswith("ass="))
    ok("空路径返回空串", subtitle_filter("") == "" and subtitle_filter(None) == "")
    quoted = home / "a'b.srt"
    quoted.write_text("1\n00:00:01,000 --> 00:00:02,000\n引号路径\n", encoding="utf-8")

    # ---- 2) 底层直调：原始 srt 真的能烧（修之前这里直接 -22）
    src = home / "src.mp4"
    subprocess.run(
        [tools, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=10",
         "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100",
         "-t", "10", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(src)],
        capture_output=True, check=True)
    raw = home / "manual.srt"
    raw.write_text("1\n00:00:01,000 --> 00:00:03,000\n第一句\n\n"
                   "2\n00:00:06,000 --> 00:00:08,000\n第二句\n", encoding="utf-8")
    direct = home / "direct.mp4"
    res = subprocess.run(
        [tools, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", subtitle_filter(raw), "-map", "0:v", "-c:v", "libx264",
         "-preset", "veryfast", str(direct)], capture_output=True)
    ok("原始 .srt 直接喂滤镜也能烧（不再 -22）", res.returncode == 0,
       res.stderr.decode("utf-8", "replace")[-120:])
    lit = bright_at(tools, direct, 2.0)
    dark = bright_at(tools, direct, 4.5)
    ok("第 2 秒有字、第 4.5 秒没字", lit > 200 and 0 <= dark < 20,
       f"有字 {lit} px / 空档 {dark} px")
    quoted_out = home / "quoted.mp4"
    qres = subprocess.run(
        [tools, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", subtitle_filter(quoted), "-map", "0:v", "-c:v", "libx264",
         "-preset", "veryfast", str(quoted_out)], capture_output=True)
    ok("路径里带单引号也能烧（滤镜参数要转义）", qres.returncode == 0,
       qres.stderr.decode("utf-8", "replace")[-120:])

    # ---- 3) 走服务：切片字幕要平移回 0，否则成片一行字都没有
    service = WorkbenchService(config_path=args.config)
    try:
        root = home / "media" / "UP" / "烧字幕"
        root.mkdir(parents=True, exist_ok=True)
        project = service.repo.project_create(title="烧字幕", up_name="UP",
                                              root_dir=str(root), stage="analyzed")
        pid = str(project["id"])
        # 字幕在源视频里是 2–4s / 8–10s；切片从 2s 起 → 成片里应是 0–2s / 6–8s
        service.repo.transcript_upsert(
            pid, segments=subtitles.normalize([
                {"start_ms": 2_000, "end_ms": 4_000, "text": "切片里的第一句"},
                {"start_ms": 8_000, "end_ms": 10_000, "text": "切片里的第二句"},
            ]), language="zh", model="tiny", srt_path=str(root / "transcript.srt"))
        source = root / "source.mp4"
        shutil.copyfile(src, source)
        service.repo.asset_upsert(pid, "source", str(source),
                                  bytes=source.stat().st_size, duration_ms=10_000,
                                  height=360)
        candidate = service.repo.candidate_insert(pid, kind="highlight", start_ms=2_000,
                                                 end_ms=10_000, score=80,
                                                 title="烧字幕", source="manual")
        cid = str(candidate["id"])

        job = wait_job(service, call(service, "clip.export", {
            "project_id": pid, "candidate_id": cid, "mode": "precise",
            "subtitle_mode": "hard", "aspect": "16x9"})["job"]["id"])
        ok("烧字幕任务成功", job["status"] == "succeeded",
           f"{job['status']}｜{job.get('error_message') or ''}"[:160])
        payload = job["payload"]
        ass_path = str(payload["subtitle"])
        ok("切片烧的是 ASS，不是 srt", ass_path.lower().endswith(".ass"), ass_path)
        ok("ASS 文件名带切片起点，不同切片不互相覆盖",
           f"@{int(payload['start_ms'])}ms" in Path(ass_path).name, Path(ass_path).name)
        events = ass_times(Path(ass_path).read_text(encoding="utf-8"))
        ok("只留窗口内两条，且时间从 0 起算",
           len(events) == 2 and events[0][0] == 0 and events[1][0] == 6_000, str(events))

        clip = Path(str(job["result"]["path"]))
        band = bright_at(tools, clip, 1.0)
        gap = bright_at(tools, clip, 4.0)
        band2 = bright_at(tools, clip, 7.0)
        ok("成片第 1 秒有字幕（平移对了）", band > 200, f"{band} px")
        ok("成片第 7 秒也有（第二条仍在窗口内）", band2 > 200, f"{band2} px")
        ok("空档确实干净", 0 <= gap < 20, f"{gap} px")

        job_none = wait_job(service, call(service, "clip.export", {
            "project_id": pid, "candidate_id": cid, "mode": "precise",
            "subtitle_mode": "none", "aspect": "16x9"})["job"]["id"])
        clean = bright_at(tools, Path(str(job_none["result"]["path"])), 1.0)
        ok("不烧字幕的对照片全程黑底", 0 <= clean < 20, f"{clean} px")

        # ---- 4) 白底画面：数亮像素在这儿会骗人，必须同帧差分
        #         （真链路 BV1VZWc6dECG 大量白底幻灯片，第一版复验因此给出
        #          17 万 vs 13 万这种没法判的数字，字幕有没有上都算“通过”）
        white_src = home / "white.mp4"
        subprocess.run(
            [tools, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=white:s=640x360:r=25:d=10",
             "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100",
             "-t", "10", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(white_src)],
            capture_output=True, check=True)
        wroot = home / "media" / "UP" / "白底烧字幕"
        wroot.mkdir(parents=True, exist_ok=True)
        wproj = service.repo.project_create(title="白底烧字幕", up_name="UP",
                                            root_dir=str(wroot), stage="analyzed")
        wpid = str(wproj["id"])
        service.repo.transcript_upsert(
            wpid, segments=subtitles.normalize([
                {"start_ms": 2_000, "end_ms": 4_000, "text": "白底上的第一句"},
                {"start_ms": 8_000, "end_ms": 10_000, "text": "白底上的第二句"},
            ]), language="zh", model="tiny", srt_path=str(wroot / "transcript.srt"))
        wsource = wroot / "source.mp4"
        shutil.copyfile(white_src, wsource)
        service.repo.asset_upsert(wpid, "source", str(wsource),
                                  bytes=wsource.stat().st_size, duration_ms=10_000,
                                  height=360)
        wcand = service.repo.candidate_insert(wpid, kind="highlight", start_ms=2_000,
                                              end_ms=10_000, score=80,
                                              title="白底烧字幕", source="manual")
        wid = str(wcand["id"])
        w_hard = wait_job(service, call(service, "clip.export", {
            "project_id": wpid, "candidate_id": wid, "mode": "precise",
            "subtitle_mode": "hard", "aspect": "9x16",
            "vertical_mode": "center"})["job"]["id"])
        w_plain = wait_job(service, call(service, "clip.export", {
            "project_id": wpid, "candidate_id": wid, "mode": "precise",
            "subtitle_mode": "none", "aspect": "9x16",
            "vertical_mode": "center"})["job"]["id"])
        hp = Path(str(w_hard["result"]["path"]))
        pp = Path(str(w_plain["result"]["path"]))
        _, fh = gray_frame(tools, hp, 1.0)
        band = slice(int(fh * 0.72), fh) if fh else slice(None)
        lit_px = bright_at(tools, hp, 1.0)
        gap_px = bright_at(tools, hp, 4.0)
        ok("白底上亮度法确实失效（有字/空档都过阈值）",
           lit_px > 200 and gap_px > 200, f"有字 {lit_px} / 空档 {gap_px} px")
        band_hit = changed_at(tools, hp, pp, 1.0, band)
        band_gap = changed_at(tools, hp, pp, 4.0, band)
        ok("白底用同帧差分能看出字幕（带内改变 > 3000）", band_hit > 3000,
           f"{band_hit} px / 帧高 {fh}")
        ok("空档帧差分归零（字幕会随时间消失，不是常驻水印）", band_gap <= 300,
           f"{band_gap} px")

        job_soft = wait_job(service, call(service, "clip.export", {
            "project_id": pid, "candidate_id": cid, "mode": "precise",
            "subtitle_mode": "soft", "aspect": "16x9"})["job"]["id"])
        ok("软字幕导出成功", job_soft["status"] == "succeeded",
           f"{job_soft['status']}｜{job_soft.get('error_message') or ''}"[:160])
        extracted = home / "track.srt"
        subprocess.run([tools, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(job_soft["result"]["path"]), "-map", "0:s:0",
                        str(extracted)], capture_output=True)
        first = first_srt_start_ms(extracted.read_text(encoding="utf-8", errors="replace"))
        ok("软字幕轨按成片时间走（不是源时间）", 0 <= first <= 1_500, f"首句 {first}ms")
    finally:
        service.close()

    if not args.keep:
        shutil.rmtree(home, ignore_errors=True)

    print()
    if FAILS:
        print("P6 字幕上屏验收：FAILED")
        for name in FAILS:
            print(f"  · {name}")
        return 1
    print("P6 字幕上屏验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
