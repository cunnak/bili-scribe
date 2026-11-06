#!/usr/bin/env python
"""P6-03 BGM 闪避与淡入淡出的真机验收（任务清单 P6-03）。

两条验收标准，逐条对上：

1. **人声段落 BGM 降低** —— 造一段"每 3 秒说话 1 秒"的素材（300 Hz 说人声）叠一条
   3000 Hz 的 BGM，导出两条（闪避开 / 关），用 `highpass=f=2000` 只量 BGM 频段：
   说话窗里闪避版必须明显更低，静音窗里两者必须几乎一样（只压说话段，不整体变小）。
2. **淡入淡出不爆音** —— 开场 80 ms 的电平必须远低于中段，且全程 `max_volume < 0 dBFS`
   （削波在 volumedetect 里会以 0 dB 峰值出现）。

另外整条链路走一遍服务层：参数下传、BGM 不存在要报 NOT_FOUND、`skip_exported`
换配乐后不能把新片子当成"已经导过"、源片没音轨时淡入淡出不能把 ffmpeg 打成
"matches no streams"。

跑法::

    python scripts/accept_p6_bgm.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
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
    result = router.dispatch(method, params, request_id="accept-p6-bgm")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def error_of(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p6-bgm")
    if result.get("ok"):
        raise SystemExit(f"{method} 竟然成功了，预期要报错")
    return result["error"]


def wait_job(service, job_id: str, timeout: float = 300.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    raise SystemExit(f"任务 {job_id} 超时未结束")


# ---------------------------------------------------------------- 素材


def make_source(ffmpeg: str, dest: Path, *, with_audio: bool = True) -> None:
    """6 秒黑场 + 每 3 秒说 1 秒（300 Hz），够短够稳。"""
    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=25:d=6",
    ]
    if with_audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.6*sin(2*PI*300*t)*between(mod(t\,3)\,0\,1):s=48000:d=6",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-t", "6", str(dest)]
    subprocess.run(args, check=True, capture_output=True)


def make_bgm(ffmpeg: str, dest: Path) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=3000:sample_rate=48000:duration=10",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )


def band_level(ffmpeg: str, path: Path, *, at: float, span: float) -> dict[str, float]:
    """只量 >2 kHz 的 BGM 频段（说人声是 300 Hz，被 highpass 挡掉）。"""
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-ss",
            str(at),
            "-t",
            str(span),
            "-i",
            str(path),
            "-af",
            "highpass=f=2000,volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    levels: dict[str, float] = {}
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            levels["mean"] = float(line.split("mean_volume:")[1].replace("dB", "").strip())
        if "max_volume:" in line:
            levels["max"] = float(line.split("max_volume:")[1].replace("dB", "").strip())
    return levels


# ---------------------------------------------------------------- 服务辅助


def make_project(service, source: Path, work: Path) -> tuple[str, str]:
    root = work / "media" / "P6-03"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="P6-03 音轨用例", source_kind="local", root_dir=str(root), stage="analyzed"
    )
    service.repo.asset_upsert(
        project["id"], "source", str(source), bytes=source.stat().st_size, duration_ms=6000
    )
    candidate = service.repo.candidate_insert(
        project["id"],
        kind="highlight",
        start_ms=0,
        end_ms=6000,
        score=80,
        title="整段",
        source="manual",
    )
    return project["id"], candidate["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    work = PROJ / "build" / "accept" / "p6_bgm"
    if work.exists() and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    cfg = {
        "ui": {"skin": "ark"},
        "storage": {
            "db_path": str(work / "wb.db"),
            "media_root": str(work / "media"),
            "cache_root": str(work / "cache"),
            "models_root": str(work / "models"),
            "ffmpeg_dir": str(work / "bin"),
        },
    }
    cfg_path = work / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    service = WorkbenchService(config_path=str(cfg_path), picker=None)
    router = service.router
    try:
        info = call(router, "app.info", {})
        check("FFmpeg 可用", bool(info["deps"]["ffmpeg"]), str(info["deps"]))
        ffmpeg = service._ffmpeg.ffmpeg
        if not ffmpeg:
            print("没有 ffmpeg，无法继续")
            return 1

        source = work / "src.mp4"
        muted = work / "src_noaudio.mp4"
        bgm = work / "bgm.wav"
        make_source(ffmpeg, source)
        make_source(ffmpeg, muted, with_audio=False)
        make_bgm(ffmpeg, bgm)
        check("造出测试素材", source.is_file() and bgm.is_file())

        # ------------------------------------------------ 1. 闪避：说话窗更低、静音窗一样
        duck_out = work / "out_duck.mp4"
        plain_out = work / "out_noduck.mp4"
        service._ffmpeg.export_clip(
            source,
            duck_out,
            start_ms=0,
            end_ms=6000,
            mode="precise",
            bgm=bgm,
            bgm_volume=0.25,
            bgm_duck=True,
        )
        service._ffmpeg.export_clip(
            source,
            plain_out,
            start_ms=0,
            end_ms=6000,
            mode="precise",
            bgm=bgm,
            bgm_volume=0.25,
            bgm_duck=False,
        )
        check("闪避版与不闪避版都导出了", duck_out.is_file() and plain_out.is_file())

        speech_duck = band_level(ffmpeg, duck_out, at=0.2, span=0.6)["mean"]
        speech_plain = band_level(ffmpeg, plain_out, at=0.2, span=0.6)["mean"]
        quiet_duck = band_level(ffmpeg, duck_out, at=1.6, span=0.6)["mean"]
        quiet_plain = band_level(ffmpeg, plain_out, at=1.6, span=0.6)["mean"]
        drop = speech_plain - speech_duck
        check(
            "说话时 BGM 被压下去（≥6 dB）",
            drop >= 6.0,
            f"{speech_plain:.1f} dB → {speech_duck:.1f} dB（低 {drop:.1f} dB）",
        )
        check(
            "静音时 BGM 原样（差值 ≤1.5 dB，说明只压说话段）",
            abs(quiet_plain - quiet_duck) <= 1.5,
            f"{quiet_duck:.1f} dB vs {quiet_plain:.1f} dB",
        )

        # ------------------------------------------------ 2. 淡入淡出：开场更低、不削波
        fade_out = work / "out_fade.mp4"
        service._ffmpeg.export_clip(
            source,
            fade_out,
            start_ms=0,
            end_ms=6000,
            mode="precise",
            fade_ms=200,
        )
        head = band_level(ffmpeg, fade_out, at=0.0, span=0.08)
        middle = band_level(ffmpeg, fade_out, at=0.4, span=0.2)
        full = band_level(ffmpeg, fade_out, at=0.0, span=6.0)
        check(
            "开场 80 ms 比中段明显轻（淡入生效）",
            head["mean"] <= middle["mean"] - 6.0,
            f"{head['mean']:.1f} dB vs {middle['mean']:.1f} dB",
        )
        check(
            "全程没有削波（max ≤ -0.1 dBFS）",
            full["max"] <= -0.1,
            f"max {full['max']:.1f} dB",
        )

        # ------------------------------------------------------ 3. 源片没音轨也能淡
        mute_out = work / "out_mute.mp4"
        try:
            service._ffmpeg.export_clip(
                muted,
                mute_out,
                start_ms=0,
                end_ms=4000,
                mode="precise",
                fade_ms=200,
            )
            ok_mute = mute_out.is_file() and mute_out.stat().st_size > 0
            detail = ""
        except Exception as exc:  # noqa: BLE001 - 把报错原文写进证据
            ok_mute = False
            detail = str(exc)[:200]
        check("源片没音轨时淡入淡出不炸（不会 matches no streams）", ok_mute, detail)

        # ------------------------------------------------------------ 4. 服务层链路
        pid, cid = make_project(service, source, work)
        job = wait_job(
            service,
            call(
                router,
                "clip.export",
                {
                    "project_id": pid,
                    "candidate_id": cid,
                    "bgm": str(bgm),
                    "bgm_volume": 30,
                    "bgm_duck": True,
                    "fade_ms": 200,
                },
            )["job"]["id"],
        )
        check(
            "带 BGM 的导出任务成功",
            job.get("status") == "succeeded",
            str(job.get("error_message") or ""),
        )
        result = job.get("result") or {}
        audio_text = str(result.get("audio") or "")
        check("结果里写了配套说明", "bgm.wav" in audio_text and "30%" in audio_text, audio_text)
        check("结果里写了淡入淡出", "200 ms" in audio_text, audio_text)
        payload = job.get("payload") or {}
        check(
            "任务载荷是解析后的绝对路径",
            Path(str(payload.get("bgm"))).is_absolute(),
            str(payload.get("bgm")),
        )

        missing = error_of(
            router,
            "clip.export",
            {
                "project_id": pid,
                "candidate_id": cid,
                "bgm": str(work / "没有这首.mp3"),
            },
        )
        check(
            "BGM 文件不存在时报 NOT_FOUND",
            missing.get("code") == "NOT_FOUND",
            str(missing.get("message")),
        )

        # 换配乐重导：不能被 skip_exported 当成"已经导过"
        bgm2 = work / "bgm2.wav"
        make_bgm(ffmpeg, bgm2)
        again = call(
            router,
            "clip.export.batch",
            {
                "project_id": pid,
                "candidate_ids": [cid],
                "bgm": str(bgm2),
                "skip_exported": True,
            },
        )
        check(
            "换一首 BGM 后 skip_exported 仍会重新导",
            int(again.get("submitted") or 0) == 1,
            str(again.get("skipped")),
        )
        # 必须等这批真的导完，"已经导过"的判定看的是成功任务 + 文件还在
        for item in again.get("jobs") or []:
            wait_job(service, item["job_id"])

        # 同一首再导一次：这次该被判成"已经导过"
        third = call(
            router,
            "clip.export.batch",
            {
                "project_id": pid,
                "candidate_ids": [cid],
                "bgm": str(bgm2),
                "skip_exported": True,
            },
        )
        check(
            "同参数重复导出会被跳过",
            int(third.get("submitted") or 0) == 0,
            str(third.get("skipped")),
        )
    finally:
        service.close()

    print()
    if FAILS:
        print(f"P6-03 BGM 与淡入淡出验收：{len(FAILS)} 项未通过 → {FAILS}")
        return 1
    print("P6-03 BGM 与淡入淡出验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
