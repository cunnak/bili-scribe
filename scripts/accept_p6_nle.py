#!/usr/bin/env python
"""P6-04 三通道交付的真机验收（任务清单 P6-04）。

三条验收标准，逐条对上：

1. **剪映草稿可打开** —— 真造一版草稿，字段按真草稿的骨架写（`version=360000`、
   `platform.app_version`、materials 的 48 个槽位都在），并且**真的往本机剪映草稿箱里
   放一份**，把路径打出来给人打开确认。
2. **OTIO 可导入达芬奇** —— `.otio` 必须是 `Timeline.1 → Stack.1 → Track.1 → Clip.1`，
   帧数按 30fps 换算，媒体引用是 `file:///`。
3. **降级包含素材 + 说明** —— 片子在、字幕在、`导入说明.txt` 写清怎么进剪映/达芬奇、
   丢了什么（字幕样式不跟随）。

另外把边界也跑一遍：非法通道 VALIDATION、候选不存在 NOT_FOUND、没源视频 CONFLICT。

跑法::

    python scripts/accept_p6_nle.py            # 跑完会告诉你去剪映里看哪个草稿
    python scripts/accept_p6_nle.py --keep     # 保留 build/accept/p6_nle 里的产物
    python scripts/accept_p6_nle.py --no-app   # 不往真剪映草稿箱写（只写项目目录）
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
from video_workbench.core import nle  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def note(text: str) -> None:
    print(f"[note] {text}")


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p6-nle")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def error_of(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p6-nle")
    if result.get("ok"):
        raise SystemExit(f"{method} 竟然成功了，预期要报错")
    return result["error"]


def wait_job(service, job_id: str, timeout: float = 180.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.1)
    raise SystemExit(f"任务 {job_id} 超时未结束")


# ---------------------------------------------------------------- 素材


def make_source(ffmpeg: str, dest: Path) -> None:
    """6 秒彩条 + 正弦音轨：草稿要引用真文件，太短会让片段挤在一起不好看。"""
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
            "testsrc2=s=640x360:r=30:d=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            "6",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )


def make_project(service, source: Path, work: Path) -> tuple[str, str, Path]:
    root = work / "media" / "P6-04" / "三通道"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="三通道交付用例",
        up_name="UP",
        source_kind="local",
        root_dir=str(root),
        stage="analyzed",
    )
    service.repo.asset_upsert(
        project["id"], "source", str(source), bytes=source.stat().st_size, duration_ms=6000
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=[
            {"start_ms": 1200, "end_ms": 2600, "text": "第一句：先证伪直觉"},
            {"start_ms": 3200, "end_ms": 4600, "text": "第二句：再拆变量"},
        ],
        language="zh",
        model="tiny",
    )
    candidate = service.repo.candidate_insert(
        project["id"],
        kind="highlight",
        start_ms=1000,
        end_ms=5000,
        score=88,
        title="先证伪直觉",
        source="ai",
    )
    service.repo.candidate_update(
        candidate["id"],
        copy={
            "title": "先证伪直觉再给方法",
            "description": "三分钟讲清一件事：先证伪直觉，再拆变量。",
            "tags": ["方法论", "认知"],
        },
    )
    return project["id"], candidate["id"], root


def deliver(service, router, pid: str, cid: str, **params) -> dict:
    out = call(router, "clip.deliver", {"project_id": pid, "candidate_id": cid, **params})
    job = wait_job(service, out["job"]["id"])
    if job.get("status") != "succeeded":
        raise SystemExit(f"交付失败：{job.get('error_code')} {job.get('error_message')}")
    return job["result"]


def main() -> int:
    # GBK 控制台打不出 "−"、替换字符会把 print 打断成 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python / 重定向流没有 reconfigure
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--no-app", action="store_true", help="不往本机剪映草稿箱写")
    args = parser.parse_args()

    work = PROJ / "build" / "accept" / "p6_nle"
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
        make_source(ffmpeg, source)
        check(
            "造出测试素材",
            source.is_file() and source.stat().st_size > 10_000,
            f"{source.stat().st_size / 1024:.0f} KB",
        )

        # --------------------------------------------------------- 0. 通道与本机软件
        nle_info = call(router, "nle.info", {})
        keys = [item["key"] for item in nle_info["channels"]]
        check("三个通道齐全", keys == ["jianying", "otio", "bundle"], str(keys))
        detected = nle.detect_apps()
        check(
            "本机软件探测与 nle.info 一致",
            bool(detected["jianying"]["found"]) == bool(nle_info["apps"]["jianying"]["found"]),
            f"剪映 {detected['jianying']['found']} · 达芬奇 {detected['resolve']['found']}",
        )

        pid, cid, root = make_project(service, source, work)

        # ---------------------------------------------------------- 1. 剪映草稿
        draft = deliver(service, router, pid, cid, channel="jianying")
        folder = Path(draft["path"])
        check(
            "草稿落在项目 export/jianying 下",
            folder.parent == root / "export" / "jianying",
            str(folder),
        )
        content_file = folder / "draft_content.json"
        meta_file = folder / "draft_meta_info.json"
        check("草稿两件套都在", content_file.is_file() and meta_file.is_file())

        doc = json.loads(content_file.read_text(encoding="utf-8"))
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        check(
            "保留剪映自己写的骨架（version/app_version）",
            doc.get("version") == 360000 and "app_version" in (doc.get("platform") or {}),
            f"version={doc.get('version')} app={doc.get('platform', {}).get('app_version')}",
        )
        check(
            "materials 的槽位没被砍掉",
            all(k in doc.get("materials", {}) for k in ("videos", "audios", "texts", "speeds")),
            str(len(doc.get("materials") or {})) + " 个槽位",
        )
        check(
            "片段按候选区间裁剪（4 秒 = 4000000 微秒）",
            doc.get("duration") == 4_000_000,
            str(doc.get("duration")),
        )
        kinds = [track["type"] for track in doc.get("tracks") or []]
        check("视频轨 + 字幕轨都在", kinds == ["video", "text"], str(kinds))
        if kinds == ["video", "text"]:
            first = doc["tracks"][1]["segments"][0]
            check(
                "字幕时间转成相对片头（1200ms − 1000ms = 200000 微秒）",
                first["target_timerange"]["start"] == 200_000,
                str(first["target_timerange"]),
            )
        video = doc["tracks"][0]["segments"][0]
        check(
            "画面从源片 1 秒处开始取",
            video["source_timerange"]["start"] == 1_000_000,
            str(video["source_timerange"]),
        )
        check(
            "meta 里草稿路径是正斜杠、ID 是 GUID 形态",
            "\\" not in str(meta.get("draft_fold_path"))
            and len(str(meta.get("draft_id") or "").replace("-", "")) == 32,
            str(meta.get("draft_id")),
        )
        check(
            "草稿说明里写明字幕样式不跟随",
            any("字幕样式不跟随" in n for n in draft["notes"]),
            str(draft["notes"][:2]),
        )

        # 真往剪映草稿箱放一份：验收原文要求"实际导入一次"
        root_jy = nle.find_jianying_root()
        if root_jy and not args.no_app:
            placed = deliver(service, router, pid, cid, channel="jianying", place_in_app=True)
            placed_dir = Path(placed["path"])
            check(
                "勾了放进草稿箱就必须真写进剪映草稿箱",
                placed["placed_in_app"] is True and placed_dir.parent == root_jy,
                str(placed_dir),
            )
            check(
                "放进草稿箱的草稿同样两件齐",
                (placed_dir / "draft_content.json").is_file()
                and (placed_dir / "draft_meta_info.json").is_file(),
            )
            note("★ 打开剪映，草稿箱里找这一版（打开确认后可以删）：")
            note(f"   {placed_dir}")
            note(f"   名字：{placed_dir.name}")
        else:
            note("没检测到剪映草稿箱（或用了 --no-app），跳过「放进草稿箱」这一段")

        # ------------------------------------------------------------ 2. OTIO
        otio = deliver(service, router, pid, cid, channel="otio")
        otio_path = Path(otio["path"])
        check(
            "OTIO 落在 export/nle 且后缀是 .otio",
            otio_path.suffix == ".otio" and otio_path.parent == root / "export" / "nle",
            str(otio_path),
        )
        otio_doc = json.loads(otio_path.read_text(encoding="utf-8"))
        check(
            "OTIO 根是 Timeline.1",
            otio_doc.get("OTIO_SCHEMA") == "Timeline.1",
            str(otio_doc.get("OTIO_SCHEMA")),
        )
        stack = (otio_doc.get("tracks") or {}).get("children") or []
        track = (stack[0] if stack else {}).get("children") or []
        clip = track[0] if track else {}
        check(
            "V1 轨里有 1 个 Clip.1",
            clip.get("OTIO_SCHEMA") == "Clip.1",
            str(clip.get("OTIO_SCHEMA")),
        )
        duration = int(((clip.get("source_range") or {}).get("duration") or {}).get("value") or 0)
        check("4 秒 @30fps = 120 帧", duration == 120, str(duration))
        ref = str(((clip.get("media_reference") or {}).get("target_url")) or "")
        check("媒体引用是 file:/// 绝对路径", ref.startswith("file:///"), ref)
        note(f"   达芬奇：File → Import → Timeline，选 {otio_path.name}（在 {otio_path.parent}）")

        # -------------------------------------------------------- 3. 降级包
        bundle = deliver(service, router, pid, cid, channel="bundle")
        bundle_dir = Path(bundle["path"])
        names = sorted(Path(p).name for p in bundle["files"])
        notes_file = bundle_dir / nle.BUNDLE_NOTE
        check(
            "降级包含片段 + 字幕 + 导入说明",
            "片段.mp4" in names and nle.BUNDLE_SUBTITLE in names and nle.BUNDLE_NOTE in names,
            str(names),
        )
        check("降级包带上文案", nle.BUNDLE_COPY in names, str(names))
        text = notes_file.read_text(encoding="utf-8") if notes_file.is_file() else ""
        check(
            "导入说明写清剪映与达芬奇两条路", "剪映" in text and "达芬奇" in text, f"{len(text)} 字"
        )
        check("导入说明交代丢了什么（字幕样式不跟随）", "字幕样式不跟随" in text)
        check(
            "包里的视频不是空文件",
            (bundle_dir / "片段.mp4").is_file()
            and (bundle_dir / "片段.mp4").stat().st_size > 10_000,
            f"{(bundle_dir / '片段.mp4').stat().st_size / 1024:.0f} KB",
        )

        # ---------------------------------------------------------- 4. 边界
        bad = error_of(
            router, "clip.deliver", {"project_id": pid, "candidate_id": cid, "channel": "premiere"}
        )
        check("非法通道报 VALIDATION", bad.get("code") == "VALIDATION", str(bad.get("message")))
        missing = error_of(router, "clip.deliver", {"project_id": pid, "candidate_id": "cand_nope"})
        check(
            "候选不存在报 NOT_FOUND",
            missing.get("code") == "NOT_FOUND",
            str(missing.get("message")),
        )

        blank = service.repo.project_create(
            title="没有源视频",
            source_kind="local",
            root_dir=str(work / "media" / "P6-04" / "空项目"),
            stage="new",
        )
        blank_cand = service.repo.candidate_insert(
            blank["id"],
            kind="highlight",
            start_ms=0,
            end_ms=3000,
            score=50,
            title="空",
            source="manual",
        )
        out = call(
            router, "clip.deliver", {"project_id": blank["id"], "candidate_id": blank_cand["id"]}
        )
        job = wait_job(service, out["job"]["id"])
        check(
            "没有源视频时任务失败并报 CONFLICT",
            job.get("status") == "failed" and job.get("error_code") == "CONFLICT",
            f"{job.get('status')} / {job.get('error_code')}",
        )
    finally:
        service.close()

    print()
    print(f"产物在 {work}")
    if FAILS:
        print(f"P6-04 三通道交付验收：{len(FAILS)} 项未通过 → {FAILS}")
        return 1
    print("P6-04 三通道交付验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
