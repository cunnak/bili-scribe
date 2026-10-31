#!/usr/bin/env python
"""合集总览验收（任务清单 P2-08）。

为什么用假模型：这一阶段要证明的是**接线**——三个子项的单集总结能拼成一份总览、
总览不碰子项、合集靠公共父目录落文件、失败可重试且不破坏已有版本。这些与模型质量无关。

夹具怎么来：合集分组的真实入口是"同一个 B 站合集下载下来的多集"，走真站下载既慢又不可控。
所以这里用 import.local 建三条项目，再直接把 collection_title 写成同一个值——
分组逻辑（repo.collection_titles）与服务层读子项的逻辑都照常被真实执行，
只有"标题从哪来"这一步被夹具替代。

本脚本验收：

    1. 三条同合集项目 → collection.list 报 1 个合集、3 集、ready
    2. 子项都没总结时 CONFLICT；只有 1 集的合集也 CONFLICT
    3. 生成总览：job 成功、collection_overview.md/json/.v1.json 落在公共父目录
    4. 总览正文含 theme / 3 条以上结论 / 每集一条 timeline / watch_order 覆盖全部子项
    5. 标题对不上的野条目被丢掉并记录 notes（不让模型编的标题混进正文）
    6. 子项总结一字未动（版本数、正文都不变）
    7. 换角色重出＝第 2 版，两版共存
    8. 模型乱写时任务失败、错误码 LLM_BAD_RESPONSE、已有版本与文件不受影响
    9. summary.collection.get 用标题和 col_ id 都能读

跑法::

    python scripts/accept_p2_collection.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
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

COLLECTION = "深度学习导论 · 验收合集"

CHILD_NAMES = [
    "第一讲 注意力到底是什么",
    "第二讲 加权求和的三种写法",
    "第三讲 残差连接为什么好用",
]

SUMMARY_JSON: dict[str, Any] = {
    "one_line": "把注意力的直觉推到能写出来的公式",
    "summary_md": (
        "## 这一集在讲什么\n"
        "从翻译时该看哪里出发，说明注意力就是按相关度做加权求和。\n\n"
        "## 怎么论证\n"
        "- 先给一个反例，说明直觉不可靠\n"
        "- 再把 Q/K/V 拆开逐个说明\n"
        "- 最后写出可运行的公式"
    ),
    "key_points": ["注意力是加权求和", "权重来自相似度", "缩放是为了让方差稳定"],
    "timeline": [
        {"t": "00:20", "text": "从翻译例子引出该看哪里"},
        {"t": "03:40", "text": "写出加权求和"},
    ],
    "caution": "素材来自 AI 转录字幕，专有名词可能有误。",
    "comment_short": "这一集把注意力从直觉推到公式，重点在为什么要缩放。",
}

CRITIQUE_TEXT = "1. 公式那段缺素材原话。\n2. 时间线少了结尾。"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-col")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def error_of(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-col-err")
    if result.get("ok"):
        raise SystemExit(f"{method} 本该失败却成功了")
    return result.get("error") or {}


# ---------------------------------------------------------------- 假模型


class StubModel:
    """两件事：写单集总结（JSON）、写合集总览（JSON，标题照抄素材）。"""

    mode = GOOD
    calls = 0
    overview_prompts: list[str] = []

    def titles_from(self, user: str) -> list[str]:
        """从 prompt 里把子项标题抠出来——模型在真实场景里也是照抄这几行。"""
        titles = re.findall(r"^\d+\.\s+(.+?)(?:（约\s*\d+\s*分钟）)?$", user, flags=re.M)
        return [item.strip() for item in titles if item.strip()]

    def payload(self, body: dict) -> dict:
        StubModel.calls += 1
        messages = body.get("messages") or []
        system = "".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        user = "".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")

        if self.mode == BAD:
            reply = "这个合集我不想按格式回话。"
        elif "合集" in user and "watch_order" in user:
            StubModel.overview_prompts.append(user)
            titles = self.titles_from(user) or CHILD_NAMES
            content = {
                "theme": "三讲连起来是一条「先证伪直觉、再给公式」的主线",
                "conclusions": [
                    "三讲共用同一套论证骨架：反例 → 拆变量 → 给公式。",
                    "第二讲把第一讲的直觉换成了可计算的写法。",
                    "第三讲的结论部分推翻第二讲：残差不是更好，而是让深层网络还能训。",
                    "重复最多的提醒是「别信直觉」。",
                ],
                "timeline": [
                    {"title": titles[0], "one_line": "建立词汇", "url": "https://example.invalid/1"},
                    {"title": titles[1], "one_line": "给出写法", "url": "https://example.invalid/2"},
                    {"title": titles[2], "one_line": "指出前两讲的边界", "url": "https://example.invalid/3"},
                    # 故意的野条目：标题在素材里不存在，必须被丢掉
                    {"title": "第四讲 不存在的补课", "one_line": "编的", "url": ""},
                ],
                "overlap": [{"kind": "重复", "text": "第一讲与第二讲都在举加权求和的例子。"}],
                "watch_order": [
                    {"title": titles[0], "reason": "先建立词汇"},
                    {"title": titles[2], "reason": "再看能照做的结论"},
                    {"title": titles[1], "reason": "最后补推导"},
                ],
            }
            reply = json.dumps(content, ensure_ascii=False)
        elif "待修订文本" in user:
            reply = json.dumps({**SUMMARY_JSON, "revise_note": "按你的要求改过"}, ensure_ascii=False)
        elif "one_line" in user and "请输出 JSON" in user:
            reply = json.dumps(SUMMARY_JSON, ensure_ascii=False)
        elif "批判" in system or "批判" in user:
            reply = CRITIQUE_TEXT
        else:
            reply = "（本段笔记）00:20 引出问题；03:40 写公式。"

        return {
            "id": "chatcmpl-accept-col",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "stub-collection",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100},
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


def wait_job(service, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.25)
    raise SystemExit(f"任务 {job_id} 超时未结束")


def fake_segments(offset: int) -> list[dict]:
    return [
        {"start_ms": 1000 + offset, "end_ms": 4200 + offset, "text": f"第 {offset} 段：注意力是加权求和。"},
        {"start_ms": 4600 + offset, "end_ms": 8100 + offset, "text": f"第 {offset} 段：缩放是为了让方差稳定。"},
    ]


def build_fixture(service, router, source: Path) -> list[str]:
    """三条同合集项目 + 假字幕。返回项目 id。"""
    tmp = Path(tempfile.mkdtemp(prefix="wb-coll-"))
    paths: list[str] = []
    for index, name in enumerate(CHILD_NAMES, start=1):
        target = tmp / f"{name}.mp4"
        shutil.copy2(source, target)
        paths.append(str(target))
    imported = call(router, "import.local", {"paths": paths, "recursive": False})
    ids = [row["project_id"] for row in imported.get("imported") or []]
    if not ids:
        raise SystemExit(f"导入没返回项目：{json.dumps(imported, ensure_ascii=False)[:300]}")
    for index, pid in enumerate(ids, start=1):
        call(router, "subtitle.save", {"project_id": pid, "segments": fake_segments(index)})
        service.repo.project_update(pid, collection_title=COLLECTION)
    return ids


def versions_of(router, pid: str) -> list[dict]:
    return call(router, "summary.versions", {"id": pid})["versions"]


def digest(router, pid: str) -> str:
    content = (call(router, "summary.get", {"id": pid})["summary"] or {}).get("content") or {}
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--source", default="build/accept/clips/short_60s.mp4")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}")
    source = (PROJ / args.source).resolve()
    if not source.is_file():
        raise SystemExit(f"缺少测试素材：{source}")

    server, base_url = start_stub()
    service = WorkbenchService(config_path=str(config_path), picker=None)
    service.cfg.override_llm(base_url=base_url, api_key="sk-accept", model="stub-collection")
    router = service.router

    try:
        # 清掉上一次验收留下的同名合集，避免版本号越滚越大
        for pid in [row["id"] for row in service.repo.project_list_by_collection(COLLECTION)]:
            service.repo.project_delete(pid)
        ids = build_fixture(service, router, source)
        check("建了三条同合集项目", len(ids) == 3, "、".join(ids))

        # 反复跑要能从头数版本：上一次的合集版本行与文件先清掉。
        # 合集 id 是标题的哈希，稳定不变——不清的话第二次跑就从第 3 版开始了。
        collection_id = service.repo.collection_id_for(COLLECTION)
        for row in service.repo.collection_summary_list(collection_id, limit=200):
            service.repo.summary_delete(row["id"])
        fresh_root = Path(service.repo.project_get(ids[0])["root_dir"]).parent
        for stale in fresh_root.glob("collection_overview*.md"):
            stale.unlink()
        for stale in fresh_root.glob("collection_overview*.json"):
            stale.unlink()

        box = call(router, "collection.list", {})["collections"]
        hit = next((row for row in box if row["title"] == COLLECTION), None)
        check("collection.list 认得这个合集", hit is not None, f"{len(box)} 个合集")
        check("合集有 3 集", bool(hit) and hit["items"] == 3, str(hit and hit["items"]))
        check("还没总结时 summarized=0", bool(hit) and hit["summarized"] == 0, str(hit and hit["summarized"]))
        check("还没总结时 ready=False", bool(hit) and hit["ready"] is False)
        check("还没总览时 has_overview=False", bool(hit) and hit["has_overview"] is False)

        err = error_of(router, "summary.collection.overview", {"collection": COLLECTION})
        check("子项没总结时拦住", err.get("code") == "CONFLICT", json.dumps(err, ensure_ascii=False)[:120])

        # 单集也要拦：另立一个只有一集的合集
        lonely = service.repo.project_list_by_collection(COLLECTION)[0]["id"]
        service.repo.project_update(lonely, collection_title="只有一集的合集 · 验收")
        err2 = error_of(router, "summary.collection.overview", {"collection": "只有一集的合集 · 验收"})
        check("只有一集时拦住", err2.get("code") == "CONFLICT", json.dumps(err2, ensure_ascii=False)[:120])
        service.repo.project_update(lonely, collection_title=COLLECTION)

        # 三条子项各自总结
        for pid in ids:
            job = wait_job(service, call(router, "summary.start", {"project_id": pid, "role": "standard"})["job"]["id"])
            if job.get("status") != "succeeded":
                raise SystemExit(f"子项 {pid} 总结失败：{job.get('error_message')}")
        check("三条子项都有总结", all(versions_of(router, pid) for pid in ids))
        before = {pid: digest(router, pid) for pid in ids}
        before_versions = {pid: len(versions_of(router, pid)) for pid in ids}

        # 生成总览
        calls_before = StubModel.calls
        submitted = call(router, "summary.collection.overview", {"collection": COLLECTION, "role": "standard"})
        check("提交返回了任务", bool(submitted.get("job", {}).get("id")), str(submitted.get("job", {}).get("type")))
        job = wait_job(service, submitted["job"]["id"])
        check("总览任务成功", job.get("status") == "succeeded", str(job.get("error_message") or ""))
        result = job.get("result") or {}
        check("任务回报第 1 版", int(result.get("version") or 0) == 1, str(result.get("version")))
        check("只发了一次请求", StubModel.calls - calls_before == 1, f"{StubModel.calls - calls_before} 次")

        view = call(router, "summary.collection.get", {"id": COLLECTION})
        check("读得回总览", bool(view.get("found")))
        content = (view.get("overview") or {}).get("content") or {}
        check("有 theme", bool(content.get("theme")), str(content.get("theme"))[:30])
        check("结论至少 3 条", len(content.get("conclusions") or []) >= 3, str(len(content.get("conclusions") or [])))
        items = content.get("items") or []
        check("timeline 只剩 3 条（野标题被丢）", len(items) == 3, str(len(items)))
        check(
            "timeline 标题都是真子项",
            all(row.get("title") in CHILD_NAMES for row in items),
            "、".join(row.get("title", "") for row in items),
        )
        check("野条目记进了 notes", len(content.get("notes") or []) >= 1, str(content.get("notes")))
        check("watch_order 覆盖 3 集", len(content.get("watch_order") or []) == 3, str(len(content.get("watch_order") or [])))
        check("有 overlap", len(content.get("overlap") or []) >= 1)
        check("子项带了链接", all(row.get("bvid") is not None for row in items))

        root = Path(service.repo.project_get(ids[0])["root_dir"]).parent
        check("collection_overview.md 落盘", (root / "collection_overview.md").is_file(), str(root))
        check("collection_overview.json 落盘", (root / "collection_overview.json").is_file())
        check("版本快照 .v1.json 落盘", (root / "collection_overview.v1.json").is_file())

        after = {pid: digest(router, pid) for pid in ids}
        check("子项总结一字未动", after == before)
        check("子项版本数没变", {pid: len(versions_of(router, pid)) for pid in ids} == before_versions)

        # 换角色重出
        second = call(router, "summary.collection.overview", {"collection": COLLECTION, "role": "xueshu"})
        job2 = wait_job(service, second["job"]["id"])
        check("换角色也能出", job2.get("status") == "succeeded", str(job2.get("error_message") or ""))
        view2 = call(router, "summary.collection.get", {"id": COLLECTION})
        check("两版共存", len(view2.get("versions") or []) == 2, str(len(view2.get("versions") or [])))
        check("新版本记了角色", (view2.get("overview") or {}).get("role") == "xueshu", str((view2.get("overview") or {}).get("role")))

        by_id = call(router, "summary.collection.get", {"id": (view2.get("collection") or {}).get("id") or ""})
        check("用 col_ id 也能读", bool(by_id.get("found")) or bool(by_id.get("children")), str(by_id.get("collection")))

        # 模型乱写
        md_before = (root / "collection_overview.md").read_bytes()
        StubModel.mode = BAD
        third = call(router, "summary.collection.overview", {"collection": COLLECTION, "role": "standard"})
        job3 = wait_job(service, third["job"]["id"])
        check("乱写时任务失败", job3.get("status") == "failed", str(job3.get("status")))
        check("错误码说得清", job3.get("error_code") == "LLM_BAD_RESPONSE", str(job3.get("error_code")))
        view3 = call(router, "summary.collection.get", {"id": COLLECTION})
        check("失败没加版本", len(view3.get("versions") or []) == 2, str(len(view3.get("versions") or [])))
        check("失败没改文件", (root / "collection_overview.md").read_bytes() == md_before)
        StubModel.mode = GOOD

        unknown = call(router, "summary.collection.get", {"id": "没有这个合集"})
        check("读不存在的合集不炸", unknown.get("found") is False and unknown.get("children") == [])
    finally:
        server.shutdown()
        service.close()

    print()
    if FAILS:
        print(f"P2-08 合集总览验收：{len(FAILS)} 项失败 —— " + "；".join(FAILS))
        return 1
    print("P2-08 合集总览验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
