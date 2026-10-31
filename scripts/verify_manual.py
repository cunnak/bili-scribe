#!/usr/bin/env python
"""按 `docs/用户手册.md` 从零跑一遍单稿闭环（任务清单 P5-07 的验证要求）。

为什么要有这个脚本：

「新人按文档能跑通」不能靠人嘴说。这个脚本把手册里那条路的**每一步都用文档上写的
方法**走一遍——从空配置、空库开始，不碰 `build/accept/` 里任何已有数据：

    1. 命令行自检        `python -m video_workbench.desktop --check --config ...`
    2. 导入本地          `import.local`（手册 §二 加本地文件）
    3. 转录              `transcribe.start`（手册 §二 开始转录）
    4. 改一行字幕        `subtitle.get` / `subtitle.save`（手册 §三 双击改文本）
    5. 导出字幕          `subtitle.export`（手册 §三 导出字幕）
    6. 生成总结          `summary.start`（手册 §四，模型用本地假端点）
    7. 生成切片候选      `clip.analyze`（手册 §五）
    8. 导出切片          `clip.export`（手册 §五，精确模式）
    9. 合并两条          `merge.preflight` / `merge.start`（手册 §六）
   10. 生成发布草稿      `publish.preview` / `publish.note`（手册 §七）

第 10 步**只生成草稿、看三关**，不发出去——发布不可撤销，自动化里不该碰。

跑法::

    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/verify_manual.py
    ... --model tiny --skip-transcribe     # 转录太慢时跳过，其余照跑

产物留在 `build/manual/`（配置、库、媒体、导出），下次跑会先清掉。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

HOME = PROJ / "build" / "manual"
CONFIG = HOME / "config.yaml"

FAILS: list[str] = []
PY = str(PROJ / ".venv" / "Scripts" / "python.exe")


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def call(service, method: str, params: dict | None = None) -> dict:
    result = service.dispatch(method, params or {})
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def wait_job(service, job_id: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.3)
    raise SystemExit(f"任务 {job_id} 超时未结束")


# ---------------------------------------------------------------- 假模型

SUMMARY_JSON = {
    "one_line": "这一条用来验证文档里的闭环能不能跑通。",
    "summary_md": "## 这条在讲什么\n\n按手册走完下载之外的每一步。\n\n## 怎么论证的\n\n1. 先自检\n2. 再一步步跑",
    "key_points": ["自检能过", "转录能出段", "切片能导出"],
    "timeline": [{"t": "00:00", "text": "开头"}],
    "caution": "这条是脚本生成的，不是模型真的读懂了内容。",
    "comment_short": "验收用草稿",
}


class Stub:
    """OpenAI 兼容端点：总结请求回结构化 JSON，别的请求回一句空话。"""

    def payload(self, body: dict) -> dict:
        prompt = "\n".join(
            str(m.get("content") or "") for m in (body.get("messages") or [])
        )
        if "one_line" in prompt or "key_points" in prompt or "总结" in prompt:
            text = json.dumps(SUMMARY_JSON, ensure_ascii=False)
        else:
            text = "收到。"
        return {
            "id": "chatcmpl-manual",
            "object": "chat.completion",
            "model": "stub-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    stub = Stub()

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

        def log_message(self, *_a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1"


# ---------------------------------------------------------------- 准备

def fresh_home(base_url: str, model: str) -> None:
    """造一个「新人第一次打开」的磁盘状态：只有一份配置，库还不存在。"""
    if HOME.exists():
        shutil.rmtree(HOME, ignore_errors=True)
    for sub in ("media", "cache", "models", "tools", "exports"):
        (HOME / sub).mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    lines = [
        "# 由 scripts/verify_manual.py 生成，模拟手册里「第一次打开」的配置",
        "app:",
        "  language: zh-CN",
        "ui:",
        "  skin: ark",
        "llm:",
        f"  base_url: {json.dumps(base_url)}",
        '  api_key: "sk-manual"',
        '  model: "stub-model"',
        '  proxy: ""',
        "storage:",
        f"  db_path: {json.dumps(str(HOME / 'wb.db'))}",
        f"  media_root: {json.dumps(str(HOME / 'media'))}",
        f"  cache_root: {json.dumps(str(HOME / 'cache'))}",
        f"  models_root: {json.dumps(str(HOME / 'models'))}",
        f"  ffmpeg_dir: {json.dumps(str(HOME / 'tools'))}",
        "transcribe:",
        "  engine: faster-whisper",
        f"  model: {json.dumps(model)}",
        "  device: cpu",
        "  compute_type: int8",
        "  language: en",
        "  vad: true",
        "  word_timestamps: true",
    ]
    CONFIG.write_text(nl.join(lines) + nl, encoding="utf-8")


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("[x] PATH 里没有 ffmpeg")
    return found


def make_clip(dest: Path, seconds: int = 25) -> Path:
    """一段带人声的测试片：正弦音不算人声，所以从既有验收素材里借一段真语音。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for src in sorted((PROJ / "build" / "accept" / "media").rglob("source.mp4")):
        subprocess.run(  # noqa: S603
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "20", "-t", str(seconds), "-i", str(src),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac",
             str(dest)],
            check=True,
        )
        return dest
    raise SystemExit("[x] build/accept/media 里没有源片：先跑 scripts/accept_p1_download.py 下一集")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--skip-transcribe", action="store_true")
    args = parser.parse_args()

    print("=" * 72)
    print("按 docs/用户手册.md 从零跑一遍单稿闭环")
    print("=" * 72)

    server, base_url = start_stub()
    try:
        fresh_home(base_url, args.model)
        print(f"[info] 新家目录 {HOME}（库还不存在：{not (HOME / 'wb.db').exists()}）")

        # ── 1. 手册 §起：命令行自检
        out = subprocess.run(  # noqa: S603
            [PY, "-m", "video_workbench.desktop", "--check", "--config", str(CONFIG)],
            cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=False,
        )
        info = {}
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                info = json.loads(line)
        check("手册 §起：`--check` 装配自检通过", info.get("ok") is True,
              f"ok={info.get('ok')} rpc={len(info.get('rpc') or [])} 条")
        check("手册 §首次启动：库由程序自己建出来", (HOME / "wb.db").is_file(),
              str(HOME / "wb.db"))

        from video_workbench.app.service import WorkbenchService
        from video_workbench.infra import models as models_mod

        service = WorkbenchService(config_path=str(CONFIG))
        try:
            # ── 2. 手册 §二：导入本地
            clip = make_clip(HOME / "incoming" / "manual_clip.mp4")
            imported = call(service, "import.local", {"paths": [str(clip)], "recursive": False})
            items = imported.get("imported") or []
            check("手册 §二：导入本地文件进了素材库", bool(items),
                  f"{len(items)} 个 / skipped={len(imported.get('skipped') or [])} / failed={len(imported.get('failed') or [])}")
            pid = (items[0] if items else {}).get("project_id") or ""
            if not pid:
                raise SystemExit("[x] 导入没给出 project_id")
            print(f"[info] 项目 {pid}")

            # ── 3. 手册 §二：开始转录
            if args.skip_transcribe:
                # 后面的总结/切片/合并全部要字幕，跳了转录就没有东西可验。
                # 与其继续跑到一堆 CONFLICT 假红，不如说到哪为止。
                print("[skip] 转录及其之后的步骤（--skip-transcribe 只验到导入）")
            else:
                if not models_mod.is_downloaded(service.cfg.models_root, args.model):
                    print(f"[model] 下 {args.model}（{models_mod.spec(args.model).approx_label}）")
                    models_mod.download(service.cfg.models_root, args.model, poll_seconds=1.0)
                started = call(service, "transcribe.start", {"project_ids": [pid]})
                jobs = started.get("jobs") or []
                job = wait_job(service, (jobs[0] if jobs else {}).get("id") or "")
                check("手册 §二：转录跑完并成功", job.get("status") == "succeeded",
                      f"{job.get('status')} {job.get('error_message') or ''}")
                sub = call(service, "subtitle.get", {"project_id": pid})
                segs = sub.get("segments") or []
                check("手册 §三：字幕编辑器读到了段", bool(segs), f"{len(segs)} 段")
                if not segs:
                    raise SystemExit("[x] 没有字幕段，后面的编辑/导出没法验")

                # ── 4. 手册 §三：双击改一行
                first = segs[0]
                call(service, "subtitle.save", {
                    "project_id": pid,
                    "segments": [{"id": first["id"], "start_ms": first["start_ms"],
                                  "end_ms": first["end_ms"],
                                  "text": (first.get("text") or "") + " (manual)"}],
                })
                again = call(service, "subtitle.get", {"project_id": pid}).get("segments") or []
                check("手册 §三：改文本落盘了",
                      bool(again) and "(manual)" in str(again[0].get("text")),
                      str((again[0] if again else {}).get("text"))[:40])

                # ── 5. 手册 §三：导出字幕
                exported = call(service, "subtitle.export", {"project_id": pid, "formats": ["srt"]})
                files = exported.get("files") or []
                srt = Path(files[0]["path"]) if files else None
                check("手册 §三：导出的 SRT 在磁盘上", bool(srt and srt.is_file()),
                      str(srt))

            if args.skip_transcribe:
                print("-" * 72)
                print("只验到导入：转录跳过了，总结/切片/合并都没有素材可验（这条不算验过）")
                return 0

            # ── 6. 手册 §四：生成总结
            sjob = call(service, "summary.start", {"project_id": pid})
            done = wait_job(service, (sjob.get("job") or {}).get("id") or "")
            check("手册 §四：总结生成成功", done.get("status") == "succeeded",
                  f"{done.get('status')} {done.get('error_message') or ''}")
            # summary.get / clip.candidates 的入参名都是 ``id``（不是 project_id）
            got = call(service, "summary.get", {"id": pid})
            body = (got.get("summary") or {}).get("content") or {}
            md = body.get("summary_md") if isinstance(body, dict) else ""
            detail = f"found={got.get('found')}"
            detail += f" {len(md)} 字" if md else " 没有正文"
            check("手册 §四：总结读得有正文", bool(got.get("found")) and bool(md), detail)

            # ── 7. 手册 §五：生成候选
            ajob = call(service, "clip.analyze", {"project_id": pid,
                                                  "kind": "highlight", "count": 4})
            await_ = wait_job(service, (ajob.get("job") or {}).get("id") or "", timeout=300)
            cands = call(service, "clip.candidates", {"id": pid}).get("candidates") or []
            check("手册 §五：生成候选成功且非空",
                  await_.get("status") == "succeeded" and bool(cands),
                  f"{len(cands)} 条")
            if not cands:
                raise SystemExit("[x] 没有候选，后面的导出/合并没法验")

            # ── 8. 手册 §五：导出切片（精确）
            made = call(service, "clip.create", {
                "project_id": pid, "candidate_id": cands[0]["id"],
                "start_ms": cands[0]["start_ms"], "end_ms": cands[0]["end_ms"],
            })
            cid = (made.get("clip") or {}).get("id") or ""
            e1 = call(service, "clip.export", {"project_id": pid, "candidate_id": cands[0]["id"],
                                               "clip_id": cid, "mode": "precise"})
            j1 = wait_job(service, (e1.get("job") or {}).get("id") or "", timeout=600)
            clip1 = (j1.get("result") or {}).get("path") or (e1.get("path") or "")
            check("手册 §五：精确导出产出文件",
                  j1.get("status") == "succeeded" and bool(clip1) and Path(clip1).is_file(),
                  str(clip1))

            # 第二条（合并要两条）
            second = cands[1] if len(cands) > 1 else cands[0]
            made2 = call(service, "clip.create", {
                "project_id": pid, "candidate_id": second["id"],
                "start_ms": second["start_ms"], "end_ms": second["end_ms"],
            })
            cid2 = (made2.get("clip") or {}).get("id") or ""
            e2 = call(service, "clip.export", {"project_id": pid, "candidate_id": second["id"],
                                               "clip_id": cid2, "mode": "precise"})
            j2 = wait_job(service, (e2.get("job") or {}).get("id") or "", timeout=600)
            clip2 = (j2.get("result") or {}).get("path") or (e2.get("path") or "")
            check("手册 §五：第二条也导出了", bool(clip2) and Path(clip2).is_file(), str(clip2))

            # ── 9. 手册 §六：合并
            if clip1 and clip2:
                pre = call(service, "merge.preflight", {"paths": [clip1, clip2]})
                check("手册 §六：预检给出每个来源的规格",
                      len(pre.get("items") or []) == 2,
                      f"differences={pre.get('differences')}")
                mjob = call(service, "merge.start", {"paths": [clip1, clip2],
                                                     "mode": "quick", "out_name": "manual_merge"})
                mdone = wait_job(service, (mjob.get("job") or {}).get("id") or "", timeout=600)
                mpath = (mdone.get("result") or {}).get("path") or (mjob.get("path") or "")
                check("手册 §六：合并产出文件",
                      mdone.get("status") == "succeeded" and bool(mpath) and Path(mpath).is_file(),
                      f"{mpath} fallback={ (mdone.get('result') or {}).get('fallback_used') }")

            # ── 10. 手册 §七：草稿 + 三关（不发）
            draft = call(service, "publish.preview", {"project_id": pid})
            body = draft.get("draft") or draft
            check("手册 §七：草稿有标题与正文",
                  bool(body.get("title")) and bool(body.get("summary") or body.get("body")),
                  str(body.get("title"))[:30])
            note = call(service, "publish.note", {"project_id": pid}).get("note") or {}
            gates = service._publish_gates(
                note, project=service.repo.project_get(pid),
                note=service.repo.publish_get(note.get("id")), confirmed=False, logged_in=False,
            )
            names = [g["id"] for g in (gates.get("gates") or [])]
            check("手册 §七：三关都在（内容/账号/频率）",
                  names == ["content", "account", "pace"], str(names))
            check("手册 §七：没勾确认就不让发", gates.get("ready") is not True,
                  f"blockers={gates.get('blockers')}")
            statuses = {g["id"]: g["status"] for g in (gates.get("gates") or [])}
            check("手册 §七：内容关在勾确认前是 todo", statuses.get("content") == "todo",
                  str(statuses))
        finally:
            service.close()
    finally:
        server.shutdown()

    print("-" * 72)
    if FAILS:
        print(f"按手册跑：{len(FAILS)} 项未通过 → {', '.join(FAILS)}")
        return 1
    print("按手册从零跑通单稿闭环：全部通过")
    print(f"产物：{HOME}（库 wb.db / 媒体 media / 导出 exports）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
