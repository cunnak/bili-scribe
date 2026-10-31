"""P1 真站验收：解析 → 下载 → 落库 → 侧车文件 → 缩略图。

为什么要有这么一条脚本，而不是只跑 pytest：
    pytest 里的下载器对着的是**假的** B站 响应。它能证明"代码按我理解的协议工作"，
    但证明不了"真实接口今天还是这个形状"。这条脚本用一个真 BV 号跑一遍真的 yt-dlp，
    把"下载器能下、文件真的在盘上、DB 里那一行真的指着它"一起钉住。

它刻意不碰用户的正式库：配置和媒体目录全落在 `build/accept/` 下，
跑完想清就删，不会让素材库里多出一条奇怪的记录。

用法：
    python scripts/accept_p1_download.py --url https://www.bilibili.com/video/BV1GJ411x7h7
    python scripts/accept_p1_download.py --url BV1GJ411x7h7 --quality 720P --timeout 600
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

ACCEPT = PROJ / "build" / "accept"


def write_config() -> Path:
    ACCEPT.mkdir(parents=True, exist_ok=True)
    (ACCEPT / "media").mkdir(parents=True, exist_ok=True)
    path = ACCEPT / "p1.yaml"
    path.write_text(
        "\n".join(
            [
                "# 只为验收存在的最小配置：把库和媒体都圈在 build/accept 里，",
                "# 跑完删掉整个目录即可，不会污染正式素材库。",
                "storage:",
                f'  db_path: "{ACCEPT.as_posix()}/wb.db"',
                f'  key_path: "{ACCEPT.as_posix()}/key.bin"',
                f'  media_root: "{ACCEPT.as_posix()}/media"',
                f'  cache_root: "{ACCEPT.as_posix()}/cache"',
                f'  models_root: "{ACCEPT.as_posix()}/models"',
                "download:",
                "  quality: \"720p\"",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 真站下载验收")
    parser.add_argument("--url", default="https://www.bilibili.com/video/BV1GJ411x7h7")
    parser.add_argument("--quality", default="720P")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7897")
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")

    from video_workbench.app.service import WorkbenchService

    cfg = write_config()
    service = WorkbenchService(config_path=str(cfg))
    info = service.app_info()
    print(f"[info] db={info['db_path']}")
    print(f"[info] media={info['media_root']}")
    if not info["deps"].get("yt_dlp"):
        print("[x] 没装 yt-dlp，先 pip install yt-dlp")
        service.close()
        return 2

    try:
        resolved = service.dispatch("download.resolve", {"url": args.url})["data"]
        items = resolved["items"]
        print(f"[resolve] kind={resolved['kind']} title={resolved['title']!r} items={len(items)}")
        picked = items[:1]  # 只下第一集：验收要的是"链路通"，不是把整个合集搬回家
        started = service.dispatch(
            "download.start",
            {
                "url": args.url,
                "quality": args.quality,
                "items": picked,
                "up_name": resolved.get("up_name") or "",
                "collection_title": resolved.get("collection_title") or "",
            },
        )["data"]
        print(f"[start] {json.dumps(started, ensure_ascii=False)}")
        # download.start 回的是**一个批次 job**（project_id 为 null，子项才有），
        # 所以这里盯批次 id，别指望从 project_id 反查——早先按 project_id 过滤，
        # 结果整段轮询一次都没命中，四分钟空等到超时。
        job_id = (started.get("job") or {}).get("id") or ""
        if not job_id:
            print("[x] 没拿到任务 id")
            return 3

        deadline = time.time() + args.timeout
        last = ""
        status = ""
        while time.time() < deadline:
            job = service.dispatch("job.get", {"id": job_id})["data"]
            line = f"{job['status']} {job.get('progress')} {job.get('stage') or ''}"
            if line != last:
                print(f"[job] {line}")
                last = line
            status = str(job["status"])
            if status in ("succeeded", "failed", "canceled"):
                break
            time.sleep(1.5)
        else:
            print("[x] 超时未结束")
            return 3
        if status != "succeeded":
            print(f"[x] 任务未成功：{status}")
            return 4

        # 项目 id 从子项任务里取。批次任务的 project_id 是 null（一个批次对多集），
        # 只有 download.item 才带 project_id——这一点必须看真数据才知道。
        time.sleep(0.5)
        project_ids: list[str] = []
        for item_job in service.dispatch("job.list")["data"]:
            if item_job.get("type") != "download.item":
                continue
            if item_job.get("status") != "succeeded":
                continue
            pid = item_job.get("project_id")
            if pid and pid not in project_ids:
                project_ids.append(pid)
        # 兜底：万一 job 表被清理，就按标题回查库里最新的几条。
        if not project_ids:
            for row in service.dispatch("library.list", {"limit": 5})["data"]["items"]:
                if row.get("title") == resolved["title"]:
                    project_ids.append(row["id"])
        if not project_ids:
            print("[x] 批次成功但没找到子项项目")
            return 4

        # 关键一步：不信 job 状态，信磁盘。
        ok = True
        for pid in project_ids:
            detail = service.dispatch("library.get", {"id": pid})["data"]["project"]
            print(f"[project] {detail['id']} stage={detail['stage']} missing={detail['missing']}")
            print(f"[project] root={detail['root_dir']}")
            assets = detail.get("assets") or []
            for asset in assets:
                exists = "OK " if asset["exists"] else "GONE"
                print(f"  [{exists}] {asset['kind']:<10} {asset['path']}")
            if detail["missing"]:
                ok = False
            kinds = {a["kind"] for a in assets if a["exists"]}
            if "source" not in kinds:
                print("[x] 没有源文件")
                ok = False
            if "thumb" not in kinds:
                print("[!] 没有缩略图（缩略图失败不该挡住下载，但要看一眼为什么）")
        print("[result]", "PASS" if ok else "FAIL")
        return 0 if ok else 4
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
