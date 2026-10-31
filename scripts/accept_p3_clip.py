#!/usr/bin/env python
"""切片候选生成 + 切点吸附验收（任务清单 P3-01 / P3-02）。

为什么用假模型：这一阶段要证明的是**契约**——字幕喂进去、模型给的 JSON 被
校验成"能存库的形状"、越界与缺字段被拦、模型挂了规则兜底还能切出可用的位置、
失败时说清是谁的锅。这些跟模型有多会挑高光无关；假模型反而更好逼边界
（真模型很少恰好给你一个 kind 都不认识的候选）。

本脚本验收：

    1. 60 秒测试片在 LLM 模式下出 3–8 个候选，分数降序，时长落在 15–90 秒
    2. 每条 quote 都能在字幕原文里找到（模型编原句 = 假引用，必须对得上）
    3. 越界候选被夹回片长内，kind 不认识的进 dropped 而不是硬塞
    4. 弹幕时间轴进 prompt，且热度真的加了分
    5. 模型乱写 / 模型报错 时退回规则候选，并说明"标题和分数没有内容含义"
    6. 长文分段模式能切出 120–480 秒的整章
    7. 没有字幕时明确拒绝（CONFLICT），不产出空列表
    8. 默认开吸附/去重后：同类两两重叠低于 50%、每条都留着模型原值、
       切点落在句边界或标出原因（P3-02）

跑法::

    python scripts/accept_p3_clip.py --config build/accept/p2.yaml
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
from typing import Any

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import clipper  # noqa: E402

FAILS: list[str] = []

GOOD = "good"
BAD = "bad"
GARBO = "garbo"

LINE_RE = re.compile(r"^\[(\d+):(\d+)\.(\d+)-(\d+):(\d+)\.(\d+)\]\s*(.+)$")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p3-clip")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def error_of(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p3-clip-err")
    if result.get("ok"):
        raise SystemExit(f"{method} 本该失败却成功了")
    return result.get("error") or {}


# ---------------------------------------------------------------- 假模型服务


def parse_lines(user: str) -> list[tuple[int, int, str]]:
    """把 prompt 里的字幕行读回 [(start_ms, end_ms, text)]。"""
    out: list[tuple[int, int, str]] = []
    for line in user.splitlines():
        match = LINE_RE.match(line.strip())
        if not match:
            continue
        sm, ss, sms, em, es, ems, text = match.groups()
        start = int(sm) * 60_000 + int(ss) * 1_000 + int(sms)
        end = int(em) * 60_000 + int(es) * 1_000 + int(ems)
        out.append((start, end, text.strip()))
    return out


class StubModel:
    """OpenAI 兼容端点。三种回法：正常候选 / 报错 / 纯文字乱写。"""

    mode = GOOD
    calls = 0
    users: list[str] = []

    def payload(self, body: dict) -> dict:
        StubModel.calls += 1
        messages = body.get("messages") or []
        user = "".join(
            str(item.get("content") or "") for item in messages if item.get("role") == "user"
        )
        StubModel.users.append(user)

        if self.mode == GARBO:
            reply = "这段视频挺好看的，前半段铺垫，后半段爆发，建议自己看一遍。"
        elif self.mode == BAD:
            reply = ""
        else:
            rows = parse_lines(user)
            picks = rows[:6]
            # 模板正文里两种类型都写着，只能看「想要的类型」那一行
            kind_line = next(
                (ln for ln in user.splitlines() if ln.startswith("- 想要的类型：")), ""
            )
            want_segment = "长文分段" in kind_line
            candidates: list[dict[str, Any]] = []
            for index, (start, end, text) in enumerate(picks[:4]):
                if want_segment:
                    span_start = start
                    cand = {
                        "kind": "segment",
                        "title": f"第 {index + 1} 章",
                        "start_hint_ms": span_start,
                        "end_hint_ms": span_start + 150_000,
                        "score": 70 - index * 5,
                        "reason": "这一章把问题讲完了",
                        "quote": text,
                    }
                else:
                    cand = {
                        "kind": "highlight",
                        "title": f"高光 {index + 1}",
                        "start_hint_ms": start,
                        "end_hint_ms": start + 20_000,
                        "score": 88 - index * 6,
                        "reason": "有结论也有情绪",
                        "quote": text,
                    }
                candidates.append(cand)
            # 一条 kind 不认识的、一条越界的：用来验证校验真的在干活
            candidates.append(
                {
                    "kind": "trailer",
                    "title": "预告",
                    "start_hint_ms": 0,
                    "end_hint_ms": 10_000,
                    "score": 99,
                    "reason": "类型不认识",
                }
            )
            candidates.append(
                {
                    "kind": "segment" if want_segment else "highlight",
                    "title": "越界",
                    "start_hint_ms": 0,
                    "end_hint_ms": 10 * 3_600_000,
                    "score": 61,
                    "reason": "结束时间超出片长",
                    "quote": picks[0][2] if picks else "",
                }
            )
            reply = "```json\n" + json.dumps({"candidates": candidates}, ensure_ascii=False) + "\n```"

        if self.mode == BAD:
            return self._error("stub 模型 500")
        return {
            "id": "chatcmpl-accept-clip",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "stub-clip",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 420, "total_tokens": 1320},
        }

    @staticmethod
    def _error(message: str) -> dict:
        return {
            "error": {
                "message": message,
                "type": "server_error",
                "code": "internal_error",
            }
        }


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    stub = StubModel()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            payload = stub.payload(body)
            status = 500 if StubModel.mode == BAD else 200
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
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


# ---------------------------------------------------------------- 辅助


def pick_project(router) -> dict:
    listing = call(router, "library.list", {"query": "", "limit": 200, "offset": 0})
    for item in listing["items"]:
        if "subtitle" in (item.get("kinds") or []):
            return item
    raise SystemExit("没找到带字幕的项目，先跑 scripts/accept_p2_transcribe.py")


def synthetic_segments(total_ms: int = 660_000) -> list[dict[str, Any]]:
    """10 分钟假字幕：长文分段只在长片上才切得出来，测试片只有 60 秒。"""
    rows = []
    step = 8_000
    index = 0
    for start in range(0, total_ms - step, step):
        index += 1
        rows.append(
            {
                "id": f"s{index}",
                "start_ms": start,
                "end_ms": start + step - 200,
                "text": f"第 {index} 段：这里讲的是把方法拆成可以复现的几个步骤。",
            }
        )
    return rows


def durations_of(rows: list[dict[str, Any]]) -> list[int]:
    return [int(row["end_ms"]) - int(row["start_ms"]) for row in rows]


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
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-clip")
    router = service.router

    try:
        item = (
            call(router, "library.get", {"id": args.project})["project"]
            if args.project
            else pick_project(router)
        )
        pid = item["id"]
        transcript = service.repo.transcript_get(pid) or {}
        segments = list(transcript.get("segments") or [])
        texts = [str(seg.get("text") or "") for seg in segments]
        duration = max(
            [int(seg.get("end_ms") or 0) for seg in segments] or [0]
        )
        check("找到带字幕的项目", len(segments) >= 5, f"{pid} · {len(segments)} 段 · {duration} ms")

        client = type(
            "Client",
            (),
            {
                "model": "stub-clip",
                "chat": lambda self, messages, **kwargs: _chat(service, messages, **kwargs),
            },
        )()

        # ---------------- 1. LLM 模式（60 秒真实测试片，只要高光）
        StubModel.mode = GOOD
        StubModel.calls = 0
        StubModel.users.clear()
        out = clipper.generate(
            segments,
            client=client,
            duration_ms=duration,
            kinds=("highlight",),
            count=6,
            danmaku=[{"time_ms": segments[0]["start_ms"] + 500}] * 20
            + [{"time_ms": segments[min(3, len(segments) - 1)]["start_ms"]}] * 5,
            model="stub-clip",
            snap=False,
            on_progress=lambda _msg: None,
        )
        rows = out["candidates"]
        check("LLM 模式产出", out["source"] == "llm", out["source"])
        check("一次请求就成", out["attempts"] == 1, str(out["attempts"]))
        check("候选数在 3–8 之间", 3 <= len(rows) <= 8, str(len(rows)))
        check(
            "全部是高光类型",
            all(row["kind"] == "highlight" for row in rows),
            "、".join(sorted({row["kind"] for row in rows})),
        )
        spans = durations_of(rows)
        check(
            "时长都在 15–90 秒",
            all(15_000 <= span <= 90_000 for span in spans),
            "、".join(f"{span / 1000:.0f}s" for span in spans),
        )
        check(
            "起止不越界",
            all(0 <= int(row["start_ms"]) and int(row["end_ms"]) <= duration for row in rows),
            f"片长 {duration} ms",
        )
        check(
            "分数降序",
            [row["score"] for row in rows] == sorted([row["score"] for row in rows], reverse=True),
            "、".join(str(row["score"]) for row in rows),
        )
        check(
            "分数在 0–100",
            all(0 <= int(row["score"]) <= 100 for row in rows),
            "",
        )
        check("模型候选里没有混进规则候选", out["source"] == "llm", out["source"])
        quotes = [row.get("quote") or "" for row in rows]
        check(
            "每条都有原句",
            all(quotes),
            f"{sum(1 for q in quotes if q)}/{len(rows)}",
        )
        joined = "\n".join(texts)
        check(
            "原句都能在字幕里找到",
            all(quote in joined for quote in quotes if quote),
            "编造的原句会被标出来",
        )
        check(
            "每条的标题与理由不为空",
            all(row.get("title") and row.get("reason") for row in rows),
            "",
        )
        check(
            "类型不认识的候选被丢掉",
            any("kind" in str(item.get("reason") or "") for item in out["dropped"]),
            f"丢掉 {len(out['dropped'])} 条",
        )
        check(
            "越界候选被夹回片长内",
            all(int(row["end_ms"]) <= duration for row in rows),
            "",
        )
        check("弹幕算出热度桶", out["heat_buckets"] >= 1, str(out["heat_buckets"]))
        prompt_used = StubModel.users[-1] if StubModel.users else ""
        check(
            "prompt 里带时间码",
            re.search(r"\[\d{2}:\d{2}\.\d{3}-\d{2}:\d{2}\.\d{3}\]", prompt_used) is not None,
            "",
        )
        check("prompt 里带弹幕热度", "弹幕" in prompt_used, "")
        check(
            "热度真的加到分上了",
            any(int(row.get("heat_bonus") or 0) > 0 for row in rows),
            "、".join(str(row.get("heat_bonus")) for row in rows),
        )

        # ---------------- 1b. 默认吸附 + 去重（P3-02 行为）
        snapped = clipper.generate(
            segments,
            client=client,
            duration_ms=duration,
            kinds=("highlight",),
            count=6,
            danmaku=[{"time_ms": segments[0]["start_ms"] + 500}] * 20,
            on_progress=lambda _msg: None,
        )
        picked = snapped["candidates"]
        check(
            "去重后同类互不重叠",
            all(
                min(int(a["end_ms"]), int(b["end_ms"])) - max(int(a["start_ms"]), int(b["start_ms"]))
                <= 0
                or (
                    min(int(a["end_ms"]), int(b["end_ms"])) - max(int(a["start_ms"]), int(b["start_ms"]))
                )
                / max(
                    1,
                    min(
                        int(a["end_ms"]) - int(a["start_ms"]),
                        int(b["end_ms"]) - int(b["start_ms"]),
                    ),
                )
                < clipper.OVERLAP_RATIO
                for i, a in enumerate(picked)
                for b in picked[i + 1 :]
            ),
            f"{len(rows)} → {len(picked)} 条",
        )
        check(
            "每个候选都留了模型原值",
            all("raw_start_ms" in row and "raw_end_ms" in row for row in picked),
            "",
        )
        check(
            "切点落在句边界或标了原因",
            all(row.get("boundary") or row.get("snap_notes") for row in picked),
            "、".join(row.get("boundary") or "无边界" for row in picked),
        )
        check(
            "吸附后仍合规",
            all(
                15_000 <= int(row["end_ms"]) - int(row["start_ms"]) <= 90_000
                and int(row["end_ms"]) <= duration
                for row in picked
            ),
            "、".join(f"{(row['end_ms'] - row['start_ms']) / 1000:.0f}s" for row in picked),
        )

        # ---------------- 2. 模型报错 → 规则兜底
        StubModel.mode = BAD
        StubModel.calls = 0
        fallback = clipper.generate(
            segments,
            client=client,
            duration_ms=duration,
            kinds=("highlight",),
            count=6,
            model="stub-clip",
            on_progress=lambda _msg: None,
        )
        check("模型报错时退回规则", fallback["source"] == "rule", fallback["source"])
        check(
            "规则候选也够用",
            1 <= len(fallback["candidates"]) <= 8,
            f"{len(fallback['candidates'])} 个（{duration // 1000} 秒片按 {clipper.RULE_WINDOW_MS // 1000} 秒窗口切）",
        )
        check(
            "规则候选明说没有内容含义",
            any("规则" in note for note in fallback["notes"]),
            "；".join(fallback["notes"])[:60],
        )
        check(
            "规则候选不假装有标题",
            all(
                re.fullmatch(r"第 \d+ 段(（规则候选）)?", str(row.get("title") or "")) is not None
                for row in fallback["candidates"]
            ),
            "、".join(str(row.get("title")) for row in fallback["candidates"][:3]),
        )
        check("模型报错过两次才兜底", fallback["attempts"] == 2, str(fallback["attempts"]))

        # ---------------- 3. 模型乱写（非 JSON）→ 也兜底
        StubModel.mode = GARBO
        StubModel.calls = 0
        garbo = clipper.generate(
            segments,
            client=client,
            duration_ms=duration,
            kinds=("highlight",),
            count=6,
            on_progress=lambda _msg: None,
        )
        check("乱写时也退回规则", garbo["source"] == "rule", garbo["source"])
        check(
            "乱写的原因写进 notes",
            any("JSON" in note or "解析" in note for note in garbo["notes"]),
            "；".join(garbo["notes"])[:80],
        )

        # ---------------- 4. 长文分段（合成 10 分钟字幕）
        StubModel.mode = GOOD
        long_rows = synthetic_segments()
        long_total = int(long_rows[-1]["end_ms"])
        seg_out = clipper.generate(
            long_rows,
            client=client,
            duration_ms=long_total,
            kinds=("segment",),
            count=3,
            model="stub-clip",
            on_progress=lambda _msg: None,
        )
        seg_spans = durations_of(seg_out["candidates"])
        check(
            "长文分段切出整章",
            bool(seg_spans) and all(120_000 <= span <= 480_000 for span in seg_spans),
            "、".join(f"{span / 1000:.0f}s" for span in seg_spans),
        )
        check(
            "分段模式只出 segment",
            all(row["kind"] == "segment" for row in seg_out["candidates"]),
            "、".join(sorted({row["kind"] for row in seg_out["candidates"]})),
        )

        # ---------------- 5. 没有字幕就明确拒绝
        missing_err = error_of(router, "library.get", {"id": "pj_does_not_exist"})
        check("不存在的项目报 NOT_FOUND", missing_err.get("code") == "NOT_FOUND", str(missing_err.get("code")))

        try:
            clipper.generate([], client=client)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "")
            check("空字幕明确拒绝", code == "CONFLICT", code or type(exc).__name__)
        else:
            check("空字幕明确拒绝", False, "没有报错")

        # ---------------- 6. 没有弹幕时热度为 0，不假造
        plain = clipper.generate(
            segments,
            client=client,
            duration_ms=duration,
            kinds=("highlight",),
            count=6,
            danmaku=[{"text": "哈哈哈哈"}, {"text": "太真实了"}],
            on_progress=lambda _msg: None,
        )
        check("没时间轴的弹幕不算热度", plain["heat_buckets"] == 0, str(plain["heat_buckets"]))
        check(
            "并说明热度没算进去",
            any("时间轴" in note for note in plain["notes"]),
            "；".join(plain["notes"])[:60],
        )
    finally:
        service.close()
        server.shutdown()

    print()
    if FAILS:
        print(f"P3-01/P3-02 切片候选验收：{len(FAILS)} 项失败 → " + "；".join(FAILS))
        return 1
    print("P3-01/P3-02 切片候选验收：全部通过")
    return 0


def _chat(service, messages, **_kwargs) -> str:
    """把 prompt 直接交给假端点，走真实 HTTP 通道（顺带验一遍解析层）。"""
    import urllib.request

    body = json.dumps(
        {"model": "stub-clip", "messages": messages, "temperature": 0.4},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        service.cfg.llm.base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-accept",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


if __name__ == "__main__":
    raise SystemExit(main())
