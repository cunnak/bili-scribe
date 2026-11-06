#!/usr/bin/env python
"""字幕 LLM 优化的真机验收（任务清单 P2-04）。

为什么要起一个假模型服务，而不是直接调真 API：

- 这个阶段要证明的是**链路和防线**——RPC → 任务 → 分批 → 解析 → 校验 → diff →
  应用 → 回滚。模型的文笔好不好，不是自动化能判的。
- 假服务返回的内容是可控的，所以"模型胡写"也必须能被断言：脚本里专门喂一次
  会扩写的回复，要求整批被拒、原文不动。

四段验收：

    1. llm.test 能连上（证明配置读取与 HTTP 客户端没问题）
    2. subtitle.optimize 产出 diff，且**数据库没变**
    3. subtitle.optimize.apply 落三处（库 / transcript.json / transcript.srt）并留快照
    4. subtitle.rollback 把文本退回原样
    5. 模型胡写时整批失败、原文不动

跑法::

    python scripts/accept_p2_optimize.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []

GOOD_REPLY = "good"
BAD_REPLY = "bad"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-opt")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


# ---------------------------------------------------------------- 假模型服务


class StubModel:
    """一个只会做两件事的 OpenAI 兼容端点：清洗编号行 / 故意乱写。"""

    mode = GOOD_REPLY

    def payload(self, body: dict) -> dict:
        prompt = ""
        for msg in body.get("messages") or []:
            if msg.get("role") == "user":
                prompt = str(msg.get("content") or "")
        ids = []
        for line in prompt.splitlines():
            head = line.split("\t")[0].strip()
            if head.startswith("s") and head[1:].isdigit():
                ids.append((head, line.split("\t", 2)[-1].strip()))
        if self.mode == BAD_REPLY:
            # 一段"看起来像 JSON、其实在扩写"的回复：长度校验必须拦住它
            text = json.dumps(
                [{"id": i, "text": t + "。" + "而且这句话被模型自己加长了" * 3} for i, t in ids],
                ensure_ascii=False,
            )
        else:
            text = json.dumps(
                [{"id": i, "text": t.strip("嗯啊 ") + "。"} for i, t in ids], ensure_ascii=False
            )
        return {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "model": "stub-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    stub = StubModel()

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

        def log_message(self, *_args):  # 静音：验收输出里只要结果
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1"


# ---------------------------------------------------------------- 主流程


def first_project_with_transcript(router) -> dict:
    listing = call(router, "library.list", {"query": "", "limit": 200, "offset": 0})
    for item in listing["items"]:
        if "subtitle" in (item.get("kinds") or []):
            return item
    raise SystemExit("没有找到带字幕的项目：先跑 scripts/accept_p2_transcribe.py")


def wait_job(service, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.3)
    raise SystemExit(f"任务 {job_id} 超时未结束")


def texts_of(service, project_id: str) -> list[str]:
    row = service.repo.transcript_get(project_id) or {}
    return [str(s.get("text") or "") for s in (row.get("segments") or [])]


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
    # 把模型指到本地假服务：改的是内存里的配置，不写用户的 config.yaml
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-model")
    router = service.router

    try:
        item = (
            call(router, "library.get", {"id": args.project})["project"]
            if args.project
            else first_project_with_transcript(router)
        )
        pid = item["id"]
        root = Path(item["root_dir"])
        original = texts_of(service, pid)
        check("找到一个带字幕的项目", bool(original), f"{pid} · {len(original)} 段")

        probe = call(router, "llm.test", {})
        check(
            "llm.test 连上假模型",
            probe.get("ok") is True and probe.get("model") == "stub-model",
            f"{probe.get('latency_ms')}ms",
        )

        submitted = call(router, "subtitle.optimize", {"project_id": pid, "batch_size": 30})
        job = wait_job(service, submitted["job"]["id"])
        check(
            "优化任务成功结束",
            job.get("status") == "succeeded",
            str(job.get("error_message") or ""),
        )
        result = job.get("result") or {}
        changes = result.get("changes") or []
        check("产出了 diff", len(changes) >= 1, f"{len(changes)} 段有改动")
        check(
            "diff 只改文本不改时间",
            all(c["before"] != c["after"] and {"start_ms", "end_ms"} <= set(c) for c in changes),
        )
        check("优化阶段没有写库（原文未动）", texts_of(service, pid) == original)

        accepted = [c["id"] for c in changes[: max(1, len(changes) // 2)]]
        applied = call(
            router,
            "subtitle.optimize.apply",
            {"project_id": pid, "changes": changes, "accepted_ids": accepted},
        )
        after = texts_of(service, pid)
        check(
            "只应用了勾选的那些段",
            applied.get("applied") == len(accepted),
            f"applied={applied.get('applied')} accepted={len(accepted)}",
        )
        snapshot = root / "transcript.prev.json"
        check(
            "写了优化前快照", snapshot.is_file() and snapshot.stat().st_size > 0, str(snapshot.name)
        )
        srt = (root / "transcript.srt").read_text(encoding="utf-8")
        js = (root / "transcript.json").read_text(encoding="utf-8")
        changed_texts = [s["text"] for s in applied["segments"]]
        check(
            "三处都写到了（库 / json / srt）",
            after == changed_texts and changed_texts[0][:6] in js and changed_texts[0][:6] in srt,
        )
        check(
            "时间轴没被改动",
            [s["start_ms"] for s in applied["segments"]]
            == [s.get("start_ms") for s in service.repo.transcript_get(pid)["segments"]],
        )

        back = call(router, "subtitle.rollback", {"id": pid})
        check(
            "回滚回到优化前",
            texts_of(service, pid) == original,
            f"{len(back.get('segments') or [])} 段",
        )

        # 第四步：模型乱写时必须整批失败、原文不动
        StubModel.mode = BAD_REPLY
        before_bad = texts_of(service, pid)
        sub2 = call(router, "subtitle.optimize", {"project_id": pid, "batch_size": 30})
        job2 = wait_job(service, sub2["job"]["id"])
        ok2 = job2.get("status") == "failed" and "所有批次都失败了" in str(
            job2.get("error_message") or ""
        )
        check("模型扩写时整批失败并报错", ok2, str(job2.get("error_message") or "")[:60])
        check("失败时原文一字未改", texts_of(service, pid) == before_bad)
        StubModel.mode = GOOD_REPLY
    finally:
        service.close()
        server.shutdown()

    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("P2-04 字幕优化验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
