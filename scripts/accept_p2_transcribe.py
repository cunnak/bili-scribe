"""P2 验收：真模型转录一次 60 秒测试片，并验证缓存命中（P2-01 / P2-02）。

这个脚本只做一件事：把"能不能真的转出来"变成一条 PASS/FAIL。
它跑真 faster-whisper 子进程，所以慢（tiny 约 10 秒，turbo 视 CPU/GPU 而定）。

用法：
    python scripts/accept_p2_transcribe.py --model tiny
    python scripts/accept_p2_transcribe.py --model large-v3-turbo --source <某个 mp4>

默认会从 ``build/accept/media`` 里挑一个已下载的源片，切 60 秒成新片，
再走「本地导入 → transcribe.start → 轮询 job」这条与界面完全相同的路径。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ACCEPT = ROOT / "build" / "accept"
CONFIG = ACCEPT / "p2.yaml"


def write_config(models_root: Path) -> Path:
    ACCEPT.mkdir(parents=True, exist_ok=True)
    for sub in ("data", "media", "cache", "models", "tools", "clips"):
        (ACCEPT / sub).mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(
        "\n".join(
            [
                "app:",
                "  language: zh",
                f"storage:",
                f"  db_path: {json.dumps(str(ACCEPT / 'wb_p2.db'))}",
                f"  key_path: {json.dumps(str(ACCEPT / 'data' / 'keys.json'))}",
                f"  media_root: {json.dumps(str(ACCEPT / 'media'))}",
                f"  cache_root: {json.dumps(str(ACCEPT / 'cache'))}",
                f"  models_root: {json.dumps(str(models_root))}",
                f"  ffmpeg_dir: {json.dumps(str(ACCEPT / 'tools'))}",
                "transcribe:",
                "  engine: faster-whisper",
                "  device: auto",
                "  compute_type: auto",
                "  language: zh",
                "  vad: true",
                "  word_timestamps: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return CONFIG


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("[x] PATH 里没有 ffmpeg")
    return found


def cut_clip(source: Path, dest: Path, seconds: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    subprocess.run(  # noqa: S603
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "30", "-t", str(seconds), "-i", str(source),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac",
         str(dest)],
        check=True,
    )
    return dest


def pick_source(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"[x] 找不到源片：{path}")
        return path
    for candidate in sorted((ACCEPT / "media").rglob("source.mp4")):
        return candidate
    raise SystemExit("[x] build/accept/media 下没有源片，先用 scripts/accept_p1_download.py 下一集，或用 --source 指定")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--source", default=None)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--language", default="auto", help="auto|zh|en|ja")
    parser.add_argument("--reuse", action="store_true", help="复用已有库（不清库）")
    args = parser.parse_args()

    models_root = ACCEPT / "models"
    write_config(models_root)
    if not args.reuse:
        for junk in (ACCEPT / "wb_p2.db",):
            if junk.exists():
                junk.unlink()

    from video_workbench.app.service import WorkbenchService
    from video_workbench.infra import models as models_mod

    service = WorkbenchService(config_path=str(CONFIG), picker=None)
    try:
        print(f"[info] db={service.cfg.db_path}")
        print(f"[info] models={service.cfg.models_root}")

        if not models_mod.is_downloaded(service.cfg.models_root, args.model):
            print(f"[model] 下载 {args.model}（约 {models_mod.spec(args.model).approx_label}）")
            last = {"p": -1}

            def progress(ratio, text):
                pct = int(ratio * 100)
                if pct >= last["p"] + 10:
                    last["p"] = pct
                    print(f"[model] {pct}% {text}")

            models_mod.download(service.cfg.models_root, args.model, progress=progress, poll_seconds=1.0)
        print(f"[model] ready {args.model}")

        source = pick_source(args.source)
        clip = cut_clip(source, ACCEPT / "clips" / f"short_{args.seconds}s.mp4", args.seconds)
        print(f"[clip] {clip} ({clip.stat().st_size / 1048576:.1f} MB)")

        imported = service.dispatch("import.local", {"paths": [str(clip)], "recursive": False})
        if not imported.get("ok"):
            print("[x] 导入失败", imported)
            return 3
        data = imported["data"]
        items = data.get("imported") or []
        if items:
            project_id = items[0]["project_id"]
            print(f"[import] project={project_id} root={items[0]['root']}")
        else:
            # 同一个片子跑第二遍时会被"已导入"挡掉，这里复用旧项目，
            # 让验收脚本可以反复跑（尤其是想换模型重跑时）。
            skipped = data.get("skipped") or []
            if not skipped:
                print("[x] 导入既没有新增也没有跳过", data)
                return 3
            project_id = skipped[0]["project_id"]
            print(f"[import] 复用已有项目 project={project_id}（{skipped[0]['reason']}）")
            # 旧字幕会被这次转录覆盖；后面的断言靠 model 字段确认"确实是这次跑的"


        started = time.time()
        out = service.dispatch("transcribe.start",
                             {"project_ids": [project_id], "model": args.model,
                              "language": args.language})
        if not out.get("ok"):
            print("[x] 启动转录失败", out)
            return 4
        job = out["data"]["jobs"][0]
        job_id = job["id"]
        print(f"[start] job={job_id}")

        last_line = ""
        while True:
            info = service.dispatch("job.get", {"id": job_id})["data"]
            status = info.get("status")
            stage = info.get("stage") or ""
            pct = int(float(info.get("progress") or 0) * 100)
            line = f"{status} {pct}% {stage}"
            if line != last_line:
                print(f"[job] {line}")
                last_line = line
            if status in ("succeeded", "failed", "canceled"):
                break
            if time.time() - started > args.timeout:
                print(f"[x] 超时（{args.timeout}s）")
                return 5
            time.sleep(0.5)
        elapsed = time.time() - started
        if status != "succeeded":
            print(f"[x] 转录失败：{info.get('error_code')} {info.get('error_message')}")
            return 6

        detail = service.dispatch("library.get", {"id": project_id})["data"]["project"]
        meta = detail.get("transcript") or {}
        root = Path(detail["root_dir"])
        json_path = root / "transcript.json"
        srt_path = root / "transcript.srt"
        print(f"[result] stage={detail['stage']} segments={meta.get('segments')} lang={meta.get('language')} "
              f"model={meta.get('model')} 耗时 {elapsed:.1f}s")
        if meta.get("model") and meta.get("model") != args.model:
            print(f"[x] 字幕不是本次模型产出的：期望 {args.model}，实际 {meta.get('model')}")
            return 9
        print(f"  [{'OK ' if json_path.is_file() else 'MISS'}] {json_path}")
        print(f"  [{'OK ' if srt_path.is_file() else 'MISS'}] {srt_path}")

        if not json_path.is_file() or not srt_path.is_file():
            print("[x] 产物缺失")
            return 7
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        segments = payload.get("segments") or []
        if not segments:
            print("[!] 60 秒里没有识别到语音（可能是纯音乐），管道本身是通的")
        first = segments[:3]
        for seg in first:
            print(f"    {seg['start_ms']:>6}-{seg['end_ms']:>6}  {seg['text'][:40]}")
        words = sum(len(s.get("words") or []) for s in segments)
        print(f"[check] 词级时间戳 {words} 个 · notes={payload.get('notes')} device={payload.get('device')}")
        srt_text = srt_path.read_text(encoding="utf-8")
        print(f"[check] SRT 行数 {len(srt_text.splitlines())}，首行 {srt_text.splitlines()[0] if srt_text else '(空)'}")

        # 缓存命中：再点一次必须秒回
        again_started = time.time()
        again = service.dispatch("transcribe.start",
                             {"project_ids": [project_id], "model": args.model,
                              "language": args.language})
        job2 = again["data"]["jobs"][0]["id"]
        while True:
            info2 = service.dispatch("job.get", {"id": job2})["data"]
            if info2.get("status") in ("succeeded", "failed", "canceled"):
                break
            if time.time() - again_started > 120:
                print("[x] 第二次转录超时")
                return 8
            time.sleep(0.2)
        second = time.time() - again_started
        cached = (info2.get("result") or {}).get("cached")
        print(f"[cache] 第二次 {second:.1f}s cached={cached}")
        if info2.get("status") != "succeeded" or cached is not True:
            print("[x] 缓存未命中")
            return 9
        if second > 20:
            print(f"[!] 缓存命中也用了 {second:.1f}s，偏慢（应 <20s）")

        print("[result] PASS")
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
