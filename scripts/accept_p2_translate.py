#!/usr/bin/env python
"""翻译与术语表的真机验收（任务清单 P2-05）。

为什么还是假模型：这一阶段要证明的是链路和防线——提交任务 → 分批带上下文 →
解析 → 校验 → **草稿不动原文** → 人工确认才落盘 → 双语 ASS 可读 → 失败可重试。
文笔不是自动化能判的，能判的是「模型胡写时不许污染已经确认的字幕」。

八段验收：

    1. llm.test 连上假模型
    2. subtitle.translate 出草稿，且**没有**写 transcript、没有落 srt
    3. 术语表进了 prompt（词表真的被 e2e 传下去），译文中带出术语
    4. subtitle.translation.apply 落两个文件：subtitle.zh.srt + subtitle.bilingual.ass
    5. ASS 里每段一条 Dialogue，双语版式用 \\N 分行；草稿状态转 confirmed
    6. 模型胡写时整批失败，报错清楚
    7. 已确认的字幕与文件一字未改（失败不破坏成果）
    8. 只重试失败的段：其它段的译文还在，不用整篇重来

跑法::

    python scripts/accept_p2_translate.py --config build/accept/p2.yaml
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

GOOD = "good"
BAD = "bad"

LINE_RE = re.compile(r"^(s\d+)\t\[(\d+)-(\d+)\]\t(.*)$")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-tr")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


# ---------------------------------------------------------------- 假模型服务


class StubModel:
    """OpenAI 兼容端点：按编号行回译；术语表里的词原样带出来；坏模式乱写。"""

    mode = GOOD
    last_prompt = ""

    def payload(self, body: dict) -> dict:
        prompt = ""
        for msg in body.get("messages") or []:
            if msg.get("role") == "user":
                prompt = str(msg.get("content") or "")
        StubModel.last_prompt = prompt
        # 术语表长这样："- 源词 → 目标词"
        term = ""
        for line in prompt.splitlines():
            if "→" in line:
                term = line.split("→", 1)[1].strip()
                break
        rows = []
        for line in prompt.splitlines():
            m = LINE_RE.match(line.strip())
            if m:
                rows.append((m.group(1), m.group(4)))
        if self.mode == BAD:
            text = "我这次不想按格式回话，随便说两句。"
        else:
            out = []
            for sid, src in rows:
                head = f"{term} " if term else "译 "
                out.append({"id": sid, "text": head + src.strip()})
            text = json.dumps(out, ensure_ascii=False)
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

        def log_message(self, *_args):
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


def draft_of(service, project_id: str, target: str) -> dict:
    rows = [r for r in service.repo.subtitle_list(project_id) if r.get("kind") == "translation"]
    for row in rows:
        if row.get("language") == target:
            return row
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--project", default="")
    parser.add_argument("--target", default="zh")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}（先跑 accept_p2_transcribe.py 生成）")

    server, base_url = start_stub()
    service = WorkbenchService(config_path=str(config_path), picker=None)
    # 指到本地假服务：只改内存里的配置，不写用户的 config.yaml
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-model")
    target = args.target
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

        # 术语表：拿第一段里的第一个词当源词，目标词用一个不可能撞车的串
        first_word = ""
        for token in re.split(r"[\s，。,.!?！？、]+", original[0] if original else ""):
            if len(token) >= 2:
                first_word = token
                break
        TERM = "XTERM-九号"
        service.cfg.subtitle.translate.glossary = [f"{first_word}={TERM}"] if first_word else []
        check(
            "准备好术语表",
            bool(first_word),
            service.cfg.subtitle.translate.glossary[0] if first_word else "无可用词",
        )

        probe = call(router, "llm.test", {})
        check("llm.test 连上假模型", probe.get("ok") is True, f"{probe.get('latency_ms')}ms")

        srt_path = root / f"subtitle.{target}.srt"
        ass_path = root / "subtitle.bilingual.ass"
        for stale in (srt_path, ass_path):
            if stale.is_file():
                stale.unlink()

        submitted = call(
            router, "subtitle.translate", {"project_id": pid, "target": target, "batch_size": 4}
        )
        job = wait_job(service, submitted["job"]["id"])
        check(
            "翻译任务成功结束",
            job.get("status") == "succeeded",
            str(job.get("error_message") or ""),
        )
        result = job.get("result") or {}
        check(
            "每段都拿到了译文",
            result.get("translated") == result.get("total") == len(original),
            f"translated={result.get('translated')} / total={result.get('total')}",
        )
        check(
            "auto 在提交时就定死成具体语言",
            submitted.get("target") == target,
            str(submitted.get("target")),
        )
        check("翻译不写 transcript（原文未动）", texts_of(service, pid) == original)
        check("草稿不落文件（没有 srt / ass）", not srt_path.exists() and not ass_path.exists())

        draft = draft_of(service, pid, target)
        check(
            "草稿进了 wb_subtitle",
            draft.get("status") == "draft",
            f"{draft.get('id')} · {len(draft.get('segments') or [])} 段",
        )
        check("术语表真的进了 prompt", TERM in StubModel.last_prompt, TERM)
        segs = draft.get("segments") or []
        check(
            "译文带出了术语目标词",
            bool(segs) and TERM in str(segs[0].get("translated_text") or ""),
            str(segs[0].get("translated_text") or "")[:40] if segs else "",
        )

        applied = call(
            router,
            "subtitle.translation.apply",
            {"project_id": pid, "target": target, "layout": "original_top"},
        )
        check(
            "两处文件都写出来了",
            srt_path.is_file() and ass_path.is_file(),
            f"{applied.get('files')}",
        )
        srt_text = srt_path.read_text(encoding="utf-8")
        ass_text = ass_path.read_text(encoding="utf-8")
        check(
            "纯译文 SRT 段数一致",
            srt_text.count(" --> ") == len(original),
            f"{srt_text.count(' --> ')} 条",
        )
        dialogues = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]
        check("双语 ASS 每段一条 Dialogue", len(dialogues) == len(original), f"{len(dialogues)} 条")
        check(
            "ASS 是原文在上＋译文在下的双行",
            all("\\N" in ln for ln in dialogues) and ass_text.startswith("[Script Info]"),
        )
        check(
            "确认后草稿状态转 confirmed",
            draft_of(service, pid, target).get("status") == "confirmed",
        )
        assets = [
            a
            for a in (call(router, "library.get", {"id": pid})["project"].get("assets") or [])
            if a.get("kind") == "subtitle" and str(a.get("path", "")).endswith((".srt", ".ass"))
        ]
        langs = {str(a.get("language") or "") for a in assets}
        check("两个文件都登记成了资产", {target, "bilingual"} <= langs, str(sorted(langs)))

        # 失败路径：模型乱写，只提交前两段
        StubModel.mode = BAD
        two = [str(s.get("id")) for s in (draft.get("segments") or [])[:2]]
        before_ass = ass_text
        bad = call(
            router, "subtitle.translate", {"project_id": pid, "target": target, "segment_ids": two}
        )
        job_bad = wait_job(service, bad["job"]["id"])
        msg = str(job_bad.get("error_message") or "")
        check(
            "模型乱写时任务失败并给出原因",
            job_bad.get("status") == "failed" and "所有批次都失败了" in msg,
            f"status={job_bad.get('status')} msg={msg[:60]}",
        )
        check("失败不碰已确认的字幕文件", ass_path.read_text(encoding="utf-8") == before_ass)
        check("失败不把草稿打回 draft", draft_of(service, pid, target).get("status") == "confirmed")
        check("失败不改原文", texts_of(service, pid) == original)

        # 只重试那两段：其它段译文必须还在
        StubModel.mode = GOOD
        retry = call(
            router, "subtitle.translate", {"project_id": pid, "target": target, "segment_ids": two}
        )
        job_retry = wait_job(service, retry["job"]["id"])
        check(
            "重试成功",
            job_retry.get("status") == "succeeded",
            str(job_retry.get("error_message") or ""),
        )
        after = draft_of(service, pid, target)
        filled = [s for s in (after.get("segments") or []) if str(s.get("translated_text") or "")]
        check(
            "只重跑指定段，其它段译文没丢",
            len(filled) == len(original),
            f"{len(filled)} / {len(original)} 段有译文",
        )
        check(
            "重译后退回草稿状态（需要重新确认）",
            after.get("status") == "draft",
            str(after.get("status")),
        )
    finally:
        service.close()
        server.shutdown()

    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("P2-05 翻译与术语表验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
