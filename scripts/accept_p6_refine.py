#!/usr/bin/env python
"""P6-01 多模态精析的真机验收（任务清单 P6-01）。

要证明的三件事，一件都不能省：

1. **真的送到了画面**。假模型端点会把收到的 content 分段数组存下来，脚本断言
   里面确实有 3 张 data URL 的图，且文本块在前——"抽了帧"和"把帧发给了模型"
   不是同一件事。
2. **排序真的跟着画面变了**。构造两条候选：字幕层 A 分高，而"画面"说 B 更好。
   精析后 B 必须排到前面，且标题被改写。这样"更合理"就有了可断言的定义。
3. **没配 vision 时安静跳过**。清空 `llm.tasks.vision` 重跑，候选照常产出，
   假端点一次都不能被调用，且 notes 里要写明为什么没精析。

跑法::

    python scripts/accept_p6_refine.py --video build/accept/media/<某个>/source.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []
SEEN: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p6-refine")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def wait_job(service, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    raise SystemExit(f"任务 {job_id} 超时未结束")


# ---------------------------------------------------------------- 假 vision 端点


class VisionStub:
    """OpenAI 兼容端点，一具两用：

    - **带图的请求** = P6-01 的画面精析 → 按"看到了什么"给分。
    - **不带图的请求** = P3-02 的候选生成 → 回 candidates 数组。

    两者共用同一个模型名会互相污染（候选阶段拿不到 candidates 就退回规则，
    窗口全都一样长，排序断言就失去意义），所以这里按"有没有图"分流。

    体积分规则故意写成"看画面下菜"：窗口文本里出现「后段」给高分、出现「开场」
    给低分——脚本据此构造"字幕层认为 A 好、画面说 B 好"的对照。
    """

    def payload(self, body: dict) -> dict:
        messages = body.get("messages") or []
        content = messages[-1].get("content") if messages else None
        texts: list[str] = []
        images = 0
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
                    images += 1
        else:
            texts.append(str(content or ""))
        prompt = "\n".join(texts)
        SEEN.append({"images": images, "prompt": prompt, "model": body.get("model")})

        if images:
            title = "后段结论" if "后段" in prompt else "开场铺垫"
            text = json.dumps(
                {"score": 95 if "后段" in prompt else 25, "title": title,
                 "reason": f"看到 {images} 张画面"},
                ensure_ascii=False,
            )
        else:
            text = json.dumps(
                {
                    "candidates": [
                        {"kind": "highlight", "start_hint_ms": 0, "end_hint_ms": 20_000,
                         "title": "开场铺垫", "reason": "字幕层评分最高",
                         "quote": "开场先讲一件大家都知道的事", "score": 88},
                        {"kind": "highlight", "start_hint_ms": 30_000, "end_hint_ms": 50_000,
                         "title": "后段收束", "reason": "字幕层分低", "quote": "后段才是结论",
                         "score": 41},
                    ]
                },
                ensure_ascii=False,
            )
        return {
            "id": "chatcmpl-vl-stub",
            "object": "chat.completion",
            "model": body.get("model") or "vl-stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    stub = VisionStub()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            data = json.dumps(stub.payload(body), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1"


# ---------------------------------------------------------------- 造一个可控项目


def make_project(service, video: Path, duration_ms: int) -> str:
    """建项目 + 源文件资产 + 三句话的字幕，最后一段带「后段」。

    两段候选都要 ≥15 秒且不重叠——低于下限会被"补到 15 秒"的规则撑开，
    撑开之后两条重叠 ≥50%，去重会丢掉一条，排序断言就无从谈起。
    """
    root = Path(service.cfg.media_root) / "p6_refine"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="P6-01 精析用例",
        source_kind="local",
        root_dir=str(root),
        stage="transcribed",
    )
    service.repo.asset_upsert(
        project["id"], kind="source", path=str(video),
        size=video.stat().st_size if video.exists() else 0,
    )
    segments = [
        {"id": "s1", "start_ms": 0, "end_ms": 20_000, "text": "开场先讲一件大家都知道的事"},
        {"id": "s2", "start_ms": 20_000, "end_ms": 30_000, "text": "中间把过程拆开"},
        {"id": "s3", "start_ms": 30_000, "end_ms": 50_000, "text": "后段才是结论，落成能照做的步骤"},
    ]
    service.repo.transcript_upsert(
        project["id"], language="zh", model="accept", segments=segments
    )
    return project["id"]


def seed_candidates(service, pid: str, duration_ms: int) -> None:
    """两条候选：A 字幕更像"金句"所以分高，B 分低。画面若说 B 好，排序必须翻。"""
    service.repo.candidate_clear(pid)
    service.repo.candidate_insert(
        pid, kind="highlight", start_ms=0, end_ms=20_000, score=88,
        title="开场铺垫（字幕层评分最高）", reason="开场语境完整", status="draft",
    )
    service.repo.candidate_insert(
        pid, kind="highlight", start_ms=30_000, end_ms=50_000, score=41,
        title="后段收束", reason="结论段，字幕层分低", status="draft",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", default="")
    parser.add_argument("--keep", action="store_true", help="保留临时库便于排查")
    args = parser.parse_args()

    video = Path(args.video).resolve() if args.video else None
    if video is None:
        pool = sorted((PROJ / "build" / "accept" / "media").rglob("source.mp4"))
        video = pool[0] if pool else None
    if not video or not video.is_file():
        raise SystemExit("找不到测试视频：用 --video 指定一个 mp4")

    work = PROJ / "build" / "accept" / "p6_refine"
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
        "clip": {"candidate_count": 2},
    }
    cfg_path = work / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    server, base_url = start_stub()
    service = WorkbenchService(config_path=str(cfg_path), picker=None)
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-text")
    router = service.router

    try:
        info = call(router, "app.info", {})
        check("FFmpeg 可用", bool(info["deps"]["ffmpeg"]), str(info["deps"]))

        duration_ms = 50_000
        pid = make_project(service, video, duration_ms)
        seed_candidates(service, pid, duration_ms)

        # ---- 1. 配了 vision：精析必须真跑
        service.cfg.llm.tasks.vision = "vl-stub"
        service.cfg.llm.tasks.clip_analyze = "stub-text"
        SEEN.clear()
        job = wait_job(service, call(router, "clip.analyze",
                                     {"project_id": pid, "count": 2})["job"]["id"])
        check("精析任务成功", job.get("status") == "succeeded", str(job.get("error_message") or ""))
        result = job.get("result") or {}
        check("结果里带 refined 计数", result.get("refined") == 2, str(result.get("refined")))
        check("用了配置里的 vision 模型", result.get("vision_model") == "vl-stub")

        vision_calls = [s for s in SEEN if s["model"] == "vl-stub"]
        check("vision 端点被调用了", len(vision_calls) >= 2, f"{len(vision_calls)} 次")
        check("每次请求都带 3 张画面", all(s["images"] == 3 for s in vision_calls),
              str([s["images"] for s in vision_calls]))
        check("请求里有字幕窗口文本", any("开场先讲" in s["prompt"] for s in vision_calls))
        check("请求里带音频数字", any(re.search(r"dBFS", s["prompt"]) for s in vision_calls))

        rows = service.repo.candidate_list(pid)
        top = rows[0]
        check("排序被画面翻转（低分候选上位）", top["start_ms"] == 30_000,
              f"top={top['title']}@{top['start_ms']}")
        check("分数被改写成模型的判断", top["score"] >= 90, str(top["score"]))
        check("标题被重写", top["title"] == "后段结论", top["title"])
        check("起止时间没被模型改掉", (top["start_ms"], top["end_ms"]) == (30_000, 50_000))
        # 精析是"在字幕层结果上改分和文案"，不是"重新挑片段"，所以两条候选都该在
        check("候选条数没被精析改掉", len(rows) == 2, str([r["start_ms"] for r in rows]))

        # ---- 2. 清空 vision：必须安静跳过，且不再打端点
        service.cfg.llm.tasks.vision = ""
        SEEN.clear()
        job2 = wait_job(service, call(router, "clip.analyze",
                                      {"project_id": pid, "count": 2})["job"]["id"])
        res2 = job2.get("result") or {}
        vision2 = [s for s in SEEN if s["model"] == "vl-stub"]
        check("无 vision 时不调端点", not vision2, f"{len(vision2)} 次")
        check("无 vision 时 refined=0", res2.get("refined") == 0, str(res2.get("refined")))
        check("notes 说明了跳过原因",
              any("没配 vision" in n for n in (res2.get("notes") or [])),
              str(res2.get("notes"))[:80])
        check("跳过时候选照常入库", len(service.repo.candidate_list(pid)) >= 2)

        # ---- 3. 配了 vision 但这次点名不要：也不能跑
        service.cfg.llm.tasks.vision = "vl-stub"
        SEEN.clear()
        job3 = wait_job(service, call(router, "clip.analyze",
                                      {"project_id": pid, "count": 2, "refine": False})["job"]["id"])
        res3 = job3.get("result") or {}
        vision3 = [s for s in SEEN if s["model"] == "vl-stub"]
        check("refine=false 时不调端点", not vision3, f"{len(vision3)} 次")
        check("refine=false 时 refined=0", res3.get("refined") == 0)
        check("refine=false 时说明了原因",
              any("按本次请求跳过" in n for n in (res3.get("notes") or [])),
              str(res3.get("notes"))[:80])

        # ---- 4. 源文件缺失：跳过而不是整单失败
        # 用**新项目**而不是把已有项目的资产改坏：asset_upsert 会再插一条 source，
        # `next(kind=="source")` 仍然取到原来那条真文件，这条断言就变成假绿了。
        pid2 = make_project(service, work / "gone.mp4", duration_ms)
        service.repo.project_update(pid2, title="P6-01 源文件缺失用例")
        seed_candidates(service, pid2, duration_ms)
        job4 = wait_job(service, call(router, "clip.analyze",
                                      {"project_id": pid2, "count": 2})["job"]["id"])
        res4 = (job4.get("result") or {}).get("notes") or []
        check("源文件缺失时任务仍成功", job4.get("status") == "succeeded",
              str(job4.get("error_message") or ""))
        check("并说明原因", any("找不到源视频" in n for n in res4), str(res4)[:90])

    finally:
        service.close()
        server.shutdown()
        if not args.keep:
            for p in (work / "cache" / "tmp").glob("*"):
                if p.is_dir():
                    import shutil

                    shutil.rmtree(p, ignore_errors=True)

    print()
    if FAILS:
        print(f"P6-01 画面精析验收：{len(FAILS)} 项未通过 → {FAILS}")
        return 1
    print("P6-01 画面精析验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
