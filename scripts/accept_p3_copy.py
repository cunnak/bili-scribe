#!/usr/bin/env python
"""切片文案与封面验收（任务清单 P3-08）。

为什么用假模型：这一阶段要证明的是**契约**——模型回什么能被收成"能投稿的形状"、
超长/带井号/标签不足会被收拾到什么程度、已有文案不会白烧一次请求、
封面抽帧真的落在盘上且能换、模型 500 时还能给出可用的兜底文案。
文案本身好不好看不在验收范围里。

跑法::

    python scripts/accept_p3_copy.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from accept_p3_clip import BAD, GOOD, StubModel, call, error_of, pick_project, start_stub  # noqa: E402

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import copywriter  # noqa: E402

FAILS: list[str] = []

COPY_JSON = {
    "title": "他为什么坚持不做低价",
    "description": "同一款产品把价格翻了三倍，这段讲清定价靠的是判断而不是算账，听完能自己复盘一遍。",
    "tags": [
        "#定价策略",
        "定价策略",
        "商业判断",
        "产品思维",
        "视频",
        "很长的标签超过八个字的那种",
        "多余",
    ],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def wait_job(service: WorkbenchService, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job["status"] in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.05)
    raise SystemExit(f"任务没在 {timeout}s 内结束：{service.jobs.get(job_id)}")


# ---------------------------------------------------------------- 假模型接线


def _copy_reply() -> str:
    return json.dumps(COPY_JSON, ensure_ascii=False)


_orig_payload = StubModel.payload


def payload(self, body: dict) -> dict:  # noqa: ANN001
    """文案请求单独回一份"故意不规范"的 JSON，其余交给候选生成那套。"""
    messages = body.get("messages") or []
    user = "".join(
        str(item.get("content") or "") for item in messages if item.get("role") == "user"
    )
    if "切片文案（P3-08）" not in user:
        return _orig_payload(self, body)
    StubModel.calls += 1
    StubModel.users.append(user)
    if StubModel.mode == BAD:
        return StubModel._error("stub 文案模型 500")
    reply = _copy_reply() if StubModel.mode == GOOD else "这段挺好的，建议自己看。"
    return {
        "id": "chatcmpl-accept-copy",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "stub-copy",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 400, "completion_tokens": 120, "total_tokens": 520},
    }


StubModel.payload = payload  # type: ignore[method-assign]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}（先跑 accept_p2_transcribe.py 生成）")

    server, base_url = start_stub()
    service = WorkbenchService(config_path=str(config_path), picker=None)
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-copy")
    router = service.router

    try:
        item = (
            call(router, "library.get", {"id": args.project})["project"]
            if args.project
            else pick_project(router)
        )
        pid = item["id"]
        sources = [a for a in service.repo.asset_list(pid) if a.get("kind") == "source"]
        check(
            "找到带源文件与字幕的项目",
            bool(sources),
            f"{pid} · {sources[0]['path'] if sources else '无'}",
        )

        # ---------------- 0. 生成候选作为素材
        StubModel.mode = GOOD
        StubModel.calls = 0
        started = call(router, "clip.analyze", {"project_id": pid, "count": 6})
        wait_job(service, started["job"]["id"])
        rows = call(router, "clip.candidates", {"id": pid})["candidates"]
        rows = [row for row in rows if not (row.get("copy") or {}).get("title")]
        check("有可写文案的候选", len(rows) >= 2, f"{len(rows)} 条")
        target = rows[0]

        # ---------------- 1. 写文案：任务化 + 规范化
        StubModel.calls = 0
        out = call(router, "clip.copy", {"candidate_id": target["id"]})
        check(
            "clip.copy 返回任务而不是同步结果",
            bool(out.get("job", {}).get("id")),
            str(out.get("job", {}).get("id")),
        )
        job = wait_job(service, out["job"]["id"])
        check("任务成功", job["status"] == "succeeded", job.get("error_message") or "")
        row = service.repo.candidate_get(target["id"]) or {}
        copy = row.get("copy") or {}
        check("文案落库", bool(copy.get("title")), copy.get("title") or "")
        check(
            "标题不超过 30 字",
            len(copy.get("title") or "") <= copywriter.TITLE_MAX,
            f"{len(copy.get('title') or '')} 字",
        )
        check(
            "简介不超过 120 字且有内容",
            10 <= len(copy.get("description") or "") <= copywriter.DESC_MAX,
            f"{len(copy.get('description') or '')} 字",
        )
        tags = list(copy.get("tags") or [])
        check("标签 3–5 个", copywriter.TAGS_MIN <= len(tags) <= copywriter.TAGS_MAX, str(tags))
        check(
            "标签去掉井号且去重",
            all("#" not in tag for tag in tags) and len(set(tags)) == len(tags),
        )
        check("来源标成 llm", copy.get("source") == "llm", str(copy.get("source")))
        check("刚写完不算手改", copy.get("edited") is False, str(copy.get("edited")))
        check("模型只被问了一次", StubModel.calls == 1, f"{StubModel.calls} 次")
        prompt = StubModel.users[-1]
        quote = str(target.get("quote") or "")
        in_prompt = bool(quote) and quote[:6] in prompt
        check(
            "prompt 带上切片字幕原文",
            in_prompt or not quote,
            f"quote={quote[:12]!r} {'在' if in_prompt else '不在'} prompt 里",
        )

        # ---------------- 2. 封面：默认切点后 1 秒，文件真的在盘上
        cover = (service.repo.candidate_get(target["id"]) or {}).get("copy", {}).get("cover") or {}
        cover_path = Path(str(cover.get("path") or ""))
        check("自动抽了封面", bool(cover_path) and cover_path.is_file(), str(cover_path))
        check(
            "封面文件有字节",
            cover_path.is_file() and cover_path.stat().st_size > 1024,
            f"{cover_path.stat().st_size if cover_path.is_file() else 0} 字节",
        )
        expect_at = min(int(target["start_ms"]) + 1000, int(target["end_ms"]) - 1)
        check(
            "封面取切点后 1 秒",
            int(cover.get("at_ms") or -1) == expect_at,
            f"期望 {expect_at}，实际 {cover.get('at_ms')}",
        )
        first_cover = str(cover.get("path") or "")

        # ---------------- 3. 已有文案不重烧请求
        calls_before = StubModel.calls
        again = call(router, "clip.copy", {"candidate_id": target["id"]})
        check("已有文案直接复用", bool(again.get("reused")), str(again.get("reused")))
        check(
            "复用不花模型调用",
            StubModel.calls == calls_before,
            f"{StubModel.calls - calls_before} 次",
        )
        check("复用回的还是同一份标题", (again.get("copy") or {}).get("title") == copy.get("title"))

        # ---------------- 4. 重新生成：新增调用，但封面不丢
        regen = call(router, "clip.copy", {"candidate_id": target["id"], "regenerate": True})
        wait_job(service, regen["job"]["id"])
        check("重新生成真的问了模型", StubModel.calls == calls_before + 1, f"{StubModel.calls} 次")
        row2 = service.repo.candidate_get(target["id"]) or {}
        check(
            "重写后封面保留",
            str((row2.get("copy") or {}).get("cover", {}).get("path") or "") == first_cover,
            str((row2.get("copy") or {}).get("cover", {}).get("path")),
        )
        check("重写后文案仍在", bool((row2.get("copy") or {}).get("title")))

        # ---------------- 5. 手改：截断、去重、标记 edited
        saved = call(
            router,
            "clip.copy.save",
            {
                "candidate_id": target["id"],
                "title": "标" * 60,
                "description": "描" * 400,
                "tags": ["#甲", "甲", "乙", "丙", "丁", "戊", "己"],
            },
        )
        scopy = saved["copy"]
        check("手改后标记 edited", scopy.get("edited") is True)
        check(
            "超长标题被截断",
            len(scopy["title"]) <= copywriter.TITLE_MAX,
            f"{len(scopy['title'])} 字",
        )
        check(
            "超长简介被截断",
            len(scopy["description"]) <= copywriter.DESC_MAX,
            f"{len(scopy['description'])} 字",
        )
        check(
            "标签去重并封顶",
            len(scopy["tags"]) <= copywriter.TAGS_MAX and "甲" in scopy["tags"],
            str(scopy["tags"]),
        )
        check(
            "投稿文本包含标题、简介与话题词",
            scopy["title"] in copywriter.publish_block(scopy)
            and "#" in copywriter.publish_block(scopy),
        )

        # ---------------- 6. 换封面：指定时刻 / 抽帧候选 / 上传
        picked = call(
            router,
            "clip.cover",
            {"candidate_id": target["id"], "at_ms": int(target["start_ms"]) + 3000},
        )
        pcover = picked["cover"]
        check(
            "指定时刻换封面",
            int(pcover["at_ms"]) == int(target["start_ms"]) + 3000,
            str(pcover["at_ms"]),
        )
        check("换完文件在盘上", Path(str(pcover["path"])).is_file(), str(pcover.get("path")))

        frames = call(router, "clip.cover.frames", {"candidate_id": target["id"], "count": 4})
        check("抽帧候选 4 张", len(frames["frames"]) == 4, str(len(frames["frames"])))
        check(
            "抽帧候选都写了文件",
            all(Path(str(f["path"])).is_file() for f in frames["frames"]),
            str([f["path"] for f in frames["frames"]]),
        )

        upload = PROJ / "build" / "accept" / "upload-cover.png"
        upload.parent.mkdir(parents=True, exist_ok=True)
        if not upload.is_file():
            # 1x1 PNG：够让 PIL/ffmpeg 不崩，也够让"复制字节"这条路径跑通
            upload.write_bytes(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000"
                    "0049454e44ae426082"
                )
            )
        up = call(router, "clip.cover.upload", {"candidate_id": target["id"], "path": str(upload)})
        check("上传封面标成 upload", up["cover"]["source"] == "upload", str(up["cover"]["source"]))
        check(
            "上传封面落进项目目录",
            int(target["start_ms"]) >= 0 and Path(up["cover"]["path"]).is_file(),
            str(up["cover"]["path"]),
        )
        bad_upload = PROJ / "build" / "accept" / "upload-cover.txt"
        bad_upload.write_text("not an image", encoding="utf-8")
        err = error_of(
            router, "clip.cover.upload", {"candidate_id": target["id"], "path": str(bad_upload)}
        )
        check("非图片被拦", err.get("code") == "VALIDATION", str(err.get("code")))

        # ---------------- 7. 短片封面时刻被夹在区间内
        tiny = call(
            router,
            "clip.create",
            {
                "project_id": pid,
                "kind": "highlight",
                "start_ms": 6000,
                "end_ms": 6800,
                "title": "0.8 秒短片",
            },
        )["candidate"]
        tiny_cover = call(router, "clip.cover", {"candidate_id": tiny["id"], "at_ms": -1})["cover"]
        check(
            "0.8 秒短片的封面时刻被夹在片内",
            6000 <= int(tiny_cover["at_ms"]) <= 6799,
            str(tiny_cover["at_ms"]),
        )

        # ---------------- 8. 模型 500：兜底文案，任务仍成功
        blank = [row for row in rows if row["id"] != target["id"]][0]
        StubModel.mode = BAD
        bad = call(router, "clip.copy", {"candidate_id": blank["id"]})
        bad_job = wait_job(service, bad["job"]["id"])
        bcopy = (service.repo.candidate_get(blank["id"]) or {}).get("copy") or {}
        check(
            "模型挂了文案任务仍成功",
            bad_job["status"] == "succeeded",
            bad_job.get("error_message") or "",
        )
        check("兜底文案标成 rule", bcopy.get("source") == "rule", str(bcopy.get("source")))
        check(
            "兜底文案用候选标题", bcopy.get("title") == blank.get("title"), str(bcopy.get("title"))
        )
        check(
            "兜底写了原因",
            any("兜底" in str(item) for item in (bcopy.get("notes") or [])),
            str(bcopy.get("notes")),
        )

        # ---------------- 9. 边界：不存在的候选
        check(
            "未知候选写文案 → NOT_FOUND",
            error_of(router, "clip.copy", {"candidate_id": "cand_x"}).get("code") == "NOT_FOUND",
        )
        check(
            "未知候选保存文案 → NOT_FOUND",
            error_of(router, "clip.copy.save", {"candidate_id": "cand_x", "title": "x"}).get("code")
            == "NOT_FOUND",
        )
        check(
            "候选列表能读回文案",
            any(
                (row.get("copy") or {}).get("title")
                for row in call(router, "clip.candidates", {"id": pid})["candidates"]
            ),
        )
    finally:
        service.close()
        server.shutdown()

    print()
    if FAILS:
        print(f"P3-08 切片文案验收：{len(FAILS)} 项失败")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("P3-08 切片文案验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
