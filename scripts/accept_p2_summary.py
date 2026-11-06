#!/usr/bin/env python
"""总结适配器验收（任务清单 P2-07）。

为什么用假模型：这一阶段要证明的是**接线**——字幕喂进 bili-scribe 的素材形状、
进度翻成人话、每次生成落一版且历史不丢、角色真的换了提示词、一句话打磨能修订、
模型乱写时错误码说得清。这些跟模型有多聪明无关，假模型反而更能逼出边界
（真模型很少恰好返回非 JSON）。

本脚本验收：

    1. 角色清单里有 standard，且有走三轮回的角色
    2. 提交成功 → job 成功 → summary.md / summary.json / summary.v1.json 落盘并登记资产
    3. stage 推进到 summarized，且没有动字幕原文
    4. 换角色重出＝新增一版（不覆盖），角色字段跟着变，提示词真的换了
    5. 一句话打磨＝新增一版且带本次改动说明
    6. 恢复旧版＝再新增一版（不是回滚覆盖）
    7. 模型乱写时任务失败、错误码 LLM_BAD_RESPONSE、旧版本与文件不受影响

跑法::

    python scripts/accept_p2_summary.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []

GOOD = "good"
BAD = "bad"

SUMMARY_JSON: dict[str, Any] = {
    "one_line": "用三十分钟把注意力机制从直觉讲到公式，最后落在一段可跑的最小实现上",
    "summary_md": (
        "## 这段讲的是什么\n"
        "作者从一个翻译任务里「这个词该看哪里」的问题出发，说明注意力就是在做加权求和。\n\n"
        "## 怎么推到公式\n"
        "先给出 Q/K/V 的直觉，再把缩放点积注意力写全，解释为什么要除以根号 d_k。\n\n"
        "## 最后落到实现\n"
        "用十几行 PyTorch 跑通一个单头注意力，并对比加不加 mask 的差异。"
    ),
    "key_points": [
        "注意力是加权求和，权重来自相似度",
        "除以根号 d_k 是为了让点积方差稳定",
        "mask 决定哪些位置不参与加权",
    ],
    "timeline": [
        {"t": "02:10", "text": "从翻译例子引出该看哪里的问题"},
        {"t": "12:40", "text": "写出缩放点积注意力公式"},
        {"t": "24:05", "text": "十几行实现并对比 mask 效果"},
    ],
    "caution": "素材是机器转录字幕，专有名词可能有误；具体数值需回视频核对。",
    "comment_short": (
        "这个视频用一个翻译的例子把注意力讲明白了：它本质就是按相关度加权求和。"
        "作者先把 Q/K/V 的直觉讲清楚，再补上缩放点积的公式，并解释了为什么要除以根号 d_k"
        "——不除的话点积随维度变大，softmax 会变得极端。最后给了一段十几行的实现，"
        "顺手对比了有 mask 和没 mask 的差别。适合已经知道 Transformer 结构但没搞懂内部在算什么的人。"
    ),
}

CRITIQUE_TEXT = (
    "1. 第二段为什么一定要除以根号 d_k，没给素材原话，属于我的推断。\n"
    "2. 时间线漏了 24:05 那个 mask 对比节点。\n"
    "3. 短版总结偏长，超过 280 字。"
)

REVISE_NOTE = "把公式那段补上素材原话；时间线加上 mask 对比；短版压到 260 字。"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-sum")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def error_of(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-sum-err")
    if result.get("ok"):
        raise SystemExit(f"{method} 本该失败却成功了")
    return result.get("error") or {}


# ---------------------------------------------------------------- 假模型服务


class StubModel:
    """OpenAI 兼容端点。

    三种回法：总结要 JSON、自我批判要正文、修订又要 JSON。
    靠 prompt 里的特征词分流——比按调用次序分流稳，改了实现也不会错位。
    """

    mode = GOOD
    calls = 0
    systems: list[str] = []
    users: list[str] = []

    def payload(self, body: dict) -> dict:
        StubModel.calls += 1
        messages = body.get("messages") or []
        system = "".join(
            str(item.get("content") or "") for item in messages if item.get("role") == "system"
        )
        user = "".join(
            str(item.get("content") or "") for item in messages if item.get("role") == "user"
        )
        StubModel.systems.append(system)
        StubModel.users.append(user)

        if self.mode == BAD:
            reply = "这次我不想按你给的格式回话，我们随便聊聊吧。"
        elif "待修订文本" in user:
            reply = json.dumps({**SUMMARY_JSON, "revise_note": REVISE_NOTE}, ensure_ascii=False)
        elif "one_line" in user and "请输出 JSON" in user:
            reply = json.dumps(SUMMARY_JSON, ensure_ascii=False)
        elif "批判" in system or "批判" in user:
            reply = CRITIQUE_TEXT
        else:
            # 分段预读：正文，保留时间戳
            reply = "（本段笔记）02:10 引出问题；12:40 写公式；24:05 对比 mask。"

        return {
            "id": "chatcmpl-accept",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "stub-summary",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
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


# ---------------------------------------------------------------- 辅助


def wait_job(service, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.3)
    raise SystemExit(f"任务 {job_id} 超时未结束")


def texts_of(service, project_id: str) -> list[str]:
    row = service.repo.transcript_get(project_id) or {}
    return [str(seg.get("text") or "") for seg in (row.get("segments") or [])]


def pick_project(router) -> dict:
    listing = call(router, "library.list", {"query": "", "limit": 200, "offset": 0})
    for item in listing["items"]:
        if "subtitle" in (item.get("kinds") or []):
            return item
    raise SystemExit("没找到带字幕的项目，先跑 scripts/accept_p2_transcribe.py")


def versions_of(router, project_id: str) -> list[dict]:
    return call(router, "summary.versions", {"id": project_id})["versions"]


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
    # 指到本地假服务：只在内存里改，不写用户的 config.yaml
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-summary")
    router = service.router

    try:
        item = (
            call(router, "library.get", {"id": args.project})["project"]
            if args.project
            else pick_project(router)
        )
        pid = item["id"]
        root = Path(item["root_dir"])
        before_text = texts_of(service, pid)
        check("找到带字幕的项目", bool(before_text), f"{pid} · {len(before_text)} 段")

        roles = call(router, "summary.roles", {})
        keys = [row["key"] for row in roles["roles"]]
        check("角色清单含 standard", "standard" in keys, "、".join(keys))
        three = [row["key"] for row in roles["roles"] if int(row.get("passes") or 0) >= 3]
        check("有走三轮回的角色", bool(three), "、".join(three) or "无")

        probe = call(router, "llm.test", {})
        check("llm.test 连上假模型", probe.get("ok") is True, f"{probe.get('latency_ms')}ms")

        # 旧版本先清掉，验收从零开始数
        for row in list(versions_of(router, pid)):
            service.repo.summary_delete(row["id"])
        for stale in (root / "summary.md", root / "summary.json"):
            if stale.is_file():
                stale.unlink()

        submitted = call(router, "summary.start", {"project_id": pid, "role": "standard"})
        job = wait_job(service, submitted["job"]["id"])
        check(
            "standard 生成成功",
            job.get("status") == "succeeded",
            str(job.get("error_message") or ""),
        )
        result = job.get("result") or {}
        check("任务回报了版本号", int(result.get("version") or 0) == 1, str(result.get("version")))

        view = call(router, "summary.get", {"id": pid})
        content = view["summary"].get("content") or {}
        check(
            "产出有结论与正文",
            bool(content.get("one_line")) and bool(content.get("summary_md")),
            str(content.get("one_line"))[:24],
        )
        check(
            "时间线条目带时间戳",
            len(content.get("timeline") or []) >= 1,
            str((content.get("timeline") or [{}])[0].get("t")),
        )
        check(
            "summary.md / summary.json 落盘",
            (root / "summary.md").is_file() and (root / "summary.json").is_file(),
        )
        check("版本快照 summary.v1.json 落盘", (root / "summary.v1.json").is_file())
        detail = call(router, "library.get", {"id": pid})["project"]
        kinds = [asset.get("kind") for asset in (detail.get("assets") or [])]
        check("总结登记为资产", "summary" in kinds, "、".join(sorted({str(k) for k in kinds})))
        stage = detail.get("stage")
        check("stage 推进到 summarized", stage == "summarized", stage)
        check("字幕原文没被改动", texts_of(service, pid) == before_text)
        check("版本列表 1 条", len(versions_of(router, pid)) == 1)

        # 换角色：换的是提示词，落的是新版本
        role2 = three[0] if three else next((k for k in keys if k != "standard"), "standard")
        system_before = StubModel.systems[-1]
        calls1 = StubModel.calls
        submitted2 = call(router, "summary.start", {"project_id": pid, "role": role2})
        job2 = wait_job(service, submitted2["job"]["id"])
        check(
            f"角色 {role2} 生成成功",
            job2.get("status") == "succeeded",
            str(job2.get("error_message") or ""),
        )
        view2 = call(router, "summary.get", {"id": pid})
        check("换角色＝新增一版", len(view2["versions"]) == 2, str(len(view2["versions"])))
        check(
            "新版本记了角色",
            view2["summary"].get("role") == role2,
            str(view2["summary"].get("role")),
        )
        new_systems = StubModel.systems[calls1:]
        check("提示词真的换了", bool(new_systems) and new_systems[0] != system_before)
        check(
            "三轮回跑满（批判＋修订）",
            StubModel.calls - calls1 >= 3,
            f"{StubModel.calls - calls1} 次调用",
        )
        first_note = str((view2["summary"].get("content") or {}).get("revise_note") or "")
        check("首次生成（含三轮）不写改动说明", first_note == "", first_note[:20] or "空")

        # 一句话打磨
        submitted3 = call(
            router,
            "summary.refine",
            {"project_id": pid, "requirement": "把语气改得更口语，短版再压一些"},
        )
        job3 = wait_job(service, submitted3["job"]["id"])
        check(
            "打磨任务成功", job3.get("status") == "succeeded", str(job3.get("error_message") or "")
        )
        view3 = call(router, "summary.get", {"id": pid})
        check("打磨＝再加一版", len(view3["versions"]) == 3, str(len(view3["versions"])))
        note = str((view3["summary"].get("content") or {}).get("revise_note") or "")
        check("打磨版记了改动说明", bool(note), note[:30])

        # 恢复旧版
        oldest = view3["versions"][-1]
        restored = call(router, "summary.restore", {"project_id": pid, "summary_id": oldest["id"]})
        view4 = call(router, "summary.get", {"id": pid})
        check("恢复旧版＝再加一版", len(view4["versions"]) == 4, str(len(view4["versions"])))
        check(
            "恢复版指向来源版本",
            restored.get("restored_from") == oldest.get("version"),
            f"来自第 {restored.get('restored_from')} 版",
        )
        newest_line = str((view4["summary"].get("content") or {}).get("one_line") or "")
        old_line = str(oldest.get("one_line") or "")
        same = newest_line == old_line
        check(
            "恢复的内容与旧版一致",
            same,
            str((view4["summary"].get("content") or {}).get("one_line"))[:24],
        )

        # 模型乱写
        StubModel.mode = BAD
        keep = json.dumps(view4["summary"].get("content"), ensure_ascii=False, sort_keys=True)
        bad = call(router, "summary.start", {"project_id": pid, "role": "standard"})
        bad_job = wait_job(service, bad["job"]["id"])
        StubModel.mode = GOOD
        code = str(bad_job.get("error_code") or "")
        check("乱写时任务失败", bad_job.get("status") == "failed", str(bad_job.get("status")))
        check("错误码是 LLM_BAD_RESPONSE", code == "LLM_BAD_RESPONSE", code)
        view5 = call(router, "summary.get", {"id": pid})
        check("失败不产生新版本", len(view5["versions"]) == 4, str(len(view5["versions"])))
        after = json.dumps(view5["summary"].get("content"), ensure_ascii=False, sort_keys=True)
        check("失败不动已有版本", after == keep)
        check("失败不动 summary.md", (root / "summary.md").is_file())

        # 没字幕的项目要拦在提交前。
        # 自己造一个空项目，而不是"到库里找一个无字幕的"——库里恰好没有时会静默跳过，
        # 而"跳过"在验收里等于没验（2026-10-30 P5-06 矩阵跑出过：库里的无字幕项目
        # 后来被别的脚本补上了字幕，这条断言就变成了断言"成功也是对的"）。
        blank_id = f"pj_accept_blank_{int(time.time() * 1000) % 10_000_000}"
        service.repo.project_create(
            id=blank_id,
            title="验收：没有字幕的空项目",
            root_dir=str(PROJ / "build" / "accept" / "blank"),
        )
        try:
            err = error_of(router, "summary.start", {"project_id": blank_id})
            check("没字幕的项目被拦住", str(err.get("code")) == "CONFLICT", str(err.get("code")))
        finally:
            service.repo.project_delete(blank_id)
    finally:
        server.shutdown()

    print()
    if FAILS:
        print(f"P2-07 总结适配器验收：{len(FAILS)} 项未通过")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("P2-07 总结适配器验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
