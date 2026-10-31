#!/usr/bin/env python
"""字幕导出的真机验收（任务清单 P2-06）。

这一阶段最容易"看着成功、其实废片"的地方，验收脚本必须自己去量：

    1. 三个文本格式真的写出来了，且段数对得上（不是空文件）
    2. ASS 字号按 720P 基准放大——1080P 的成片字号必须比 720P 的大
    3. 软字幕：ffprobe 能**看见**字幕流，编解码器是 mov_text
    4. 硬字幕：画面真的变了（PSNR 有限），不是"烧了个寂寞"
    5. 源视频一个字节没动（导出永远不许改原片）

跑法::

    python scripts/accept_p2_export.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-exp")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def wait_job(service, job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = call(service.router, "job.get", {"id": job_id})
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.5)
    raise SystemExit("job 超时")


def pick_project(service, want_id: str) -> dict:
    if want_id:
        return call(service.router, "library.get", {"id": want_id})["project"]
    listing = call(service.router, "library.list", {"limit": 200})
    for item in listing["items"]:
        if "subtitle" not in (item.get("kinds") or []):
            continue
        got = call(service.router, "library.get", {"id": item["id"]})["project"]
        kinds = {x["kind"] for x in got.get("assets") or []}
        if "source" in kinds:
            return got
    raise SystemExit("没有带源视频与字幕的项目，先跑 accept_p2_transcribe.py")


def ass_style_size(ass_text: str) -> int:
    head = ass_text.split("Dialogue:")[0]
    for line in head.splitlines():
        if line.startswith("Style: Default,"):
            return int(line.split(",")[2])
    return 0


def psnr_between(ffmpeg, a: str, b: str, at: float) -> float:
    """同一时刻两段视频的 PSNR。相同 = inf，不同 = 一个有限数。

    两个坑都踩过：
    - ``-ss`` 放在 ``-i`` 前面是快进，两路各自 seek 到关键帧，取到的可能
      都不是目标帧；这里用 ``trim`` 精确切一帧再比。
    - 路径必须给绝对路径：ffmpeg 的主函数不按 Unicode 处理相对路径，
      中文目录直接报 "No such file"。
    """
    span = f"[0:v]trim={at:.3f}:{at + 0.05:.3f},setpts=PTS-STARTPTS[a];"            f"[1:v]trim={at:.3f}:{at + 0.05:.3f},setpts=PTS-STARTPTS[b];[a][b]psnr"
    proc = __import__("subprocess").run(
        [ffmpeg.require()[0], "-hide_banner", "-nostats",
         "-i", str(Path(a).resolve()), "-i", str(Path(b).resolve()),
         "-filter_complex", span, "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r"average:([0-9.]+|inf)", proc.stderr or "")
    if not match:
        raise SystemExit(f"PSNR 没算出来：{(proc.stderr or '')[-300:]}")
    return float(match.group(1)) if match.group(1) != "inf" else float("inf")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}")

    service = WorkbenchService(config_path=str(config_path), picker=None)
    ffmpeg = service._ffmpeg
    if not ffmpeg.available:
        raise SystemExit("这台机器没有 ffmpeg，导出验收跑不了")
    router = service.router

    project = pick_project(service, args.project)
    pid = project["id"]
    root = Path(project["root_dir"])
    assets = call(router, "library.get", {"id": pid})["project"].get("assets") or []
    source = next(a["path"] for a in assets if a["kind"] == "source")
    seg_count = len(call(router, "subtitle.get", {"project_id": pid})["segments"])
    source_bytes = Path(source).stat().st_size
    print(f"项目 {pid} · {seg_count} 段 · 源 {Path(source).name} "
          f"({source_bytes / 1024 / 1024:.1f} MB)")

    out_dir = root / "export"
    if out_dir.is_dir():
        for old in out_dir.iterdir():
            if old.is_file():
                old.unlink()

    # ---- 1. 三种文本格式
    data = call(router, "subtitle.export", {"project_id": pid,
                                            "formats": ["srt", "ass", "vtt"],
                                            "height": 1080})
    files = {f["format"]: Path(f["path"]) for f in data["files"]}
    check("三种格式都生成了", set(files) == {"srt", "ass", "vtt"}, ", ".join(sorted(files)))
    check("三个文件都在盘上且有内容",
          all(p.is_file() and p.stat().st_size > 0 for p in files.values()),
          " / ".join(f"{p.name} {p.stat().st_size}B" for p in files.values() if p.is_file()))

    srt_text = files["srt"].read_text(encoding="utf-8")
    ass_text = files["ass"].read_text(encoding="utf-8")
    vtt_text = files["vtt"].read_text(encoding="utf-8")
    srt_rows = len(re.findall(r"^\d+$", srt_text, re.MULTILINE))
    ass_rows = ass_text.count("\nDialogue:")
    vtt_rows = len(re.findall(r" --> ", vtt_text))
    check("三种格式的段数都等于转录段数",
          srt_rows == ass_rows == vtt_rows == seg_count,
          f"srt={srt_rows} ass={ass_rows} vtt={vtt_rows} 期望={seg_count}")
    check("VTT 有 WEBVTT 头", vtt_text.startswith("WEBVTT"))
    check("SRT 用逗号毫秒", re.search(r"\d{2}:\d{2}:\d{2},\d{3} --> ", srt_text) is not None)

    # ---- 2. 字号随高度缩放
    size_1080 = ass_style_size(ass_text)
    data720 = call(router, "subtitle.export",
                   {"project_id": pid, "formats": ["ass"], "height": 720})
    size_720 = ass_style_size(Path(data720["files"][0]["path"]).read_text(encoding="utf-8"))
    check("1080P 字号是 720P 的 1.5 倍",
          size_720 > 0 and abs(size_1080 / size_720 - 1.5) < 0.06,
          f"720P={size_720} 1080P={size_1080}")

    # ---- 3. 软字幕
    soft = call(router, "subtitle.export",
                {"project_id": pid, "formats": ["ass"], "mode": "soft", "height": 1080})
    soft_job = wait_job(service, soft["job_id"])
    check("软字幕任务成功", soft_job["status"] == "succeeded",
          str(soft_job.get("error_message") or ""))
    soft_path = Path(soft["output"])
    streams = ffmpeg.subtitle_streams(str(soft_path))
    check("软字幕成片里有字幕流", bool(streams), str([s.get("codec_name") for s in streams]))
    check("字幕编解码器是 mov_text",
          any(s.get("codec_name") == "mov_text" for s in streams),
          str(streams[0].get("codec_name") if streams else "无"))
    check("成片文件名带分辨率与模式",
          soft_path.name.endswith(".1080p.soft.mp4"), soft_path.name)

    # ---- 4. 硬字幕：画面必须真的变了
    hard = call(router, "subtitle.export",
                {"project_id": pid, "formats": ["ass"], "mode": "hard", "height": 1080})
    hard_job = wait_job(service, hard["job_id"])
    check("硬字幕任务成功", hard_job["status"] == "succeeded",
          str(hard_job.get("error_message") or ""))
    hard_path = Path(hard["output"])
    # 单帧取样会撞上「那一下字幕刚好没字」的时刻，所以多取几段的前半段，
    # 只要有一处画面与原片不同，就证明字幕真的被画进去了。
    segments = call(router, "subtitle.get", {"project_id": pid})["segments"]
    samples = []
    for seg in segments[:5]:
        mid = (seg["start_ms"] + min(seg["end_ms"], seg["start_ms"] + 1200)) / 2000.0
        samples.append((mid, psnr_between(ffmpeg, source, str(hard_path), mid)))
    burned = [s for s in samples if s[1] != float("inf")]
    detail = " ".join(f"{t:.2f}s={'inf' if v == float('inf') else f'{v:.1f}dB'}"
                      for t, v in samples)
    check("硬字幕画面与原片不同（不是烧了个寂寞）", bool(burned), detail)
    check("硬字幕成片有视频流",
          any(s.get("codec_type") == "video" for s in ffmpeg.streams(str(hard_path))))

    # ---- 5. 原片与资产
    check("源视频没被改动", Path(source).stat().st_size == source_bytes,
          f"{Path(source).stat().st_size}B")
    kinds = [a["kind"] for a in call(router, "library.get", {"id": pid})["project"]["assets"]]
    check("导出产物登记成资产", kinds.count("export") >= 2 and kinds.count("subtitle") >= 3,
          f"export={kinds.count('export')} subtitle={kinds.count('subtitle')}")

    print()
    if FAILS:
        print(f"P2-06 字幕导出验收：{len(FAILS)} 项失败 —— " + "；".join(FAILS))
        return 1
    print("P2-06 字幕导出验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
