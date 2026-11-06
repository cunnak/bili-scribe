#!/usr/bin/env python
"""P6-05 命令行验收（任务清单 P6-05）。

验收原文两条：

1. **一条命令跑完单稿闭环** —— `cli run --project-id …` 一条命令里完成"转录 → 总结 → 选段 → 导出"，
   结束时打印项目 id，拿到的成片路径必须真实存在。
2. **退出码正确** —— 跑成功 0、任务失败 1、参数/环境不对 2。

为了不让验收依赖外网和真模型：素材用 `build/accept/media/` 里真下载过的片子截 25 秒，
总结走本地假 OpenAI 端点（只回固定结构的 JSON），转录用 tiny 模型（本机已下好）。

跑法::

    python scripts/accept_p6_cli.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

FAILS: list[str] = []

SUMMARY_JSON = {
    "one_line": "这段在讲先证伪直觉再动手",
    "summary_md": "## 这期在讲什么\n\n先证伪直觉，再给可复现的步骤。\n\n## 要点\n\n- 先证伪直觉\n- 再拆变量对比",
    "key_points": ["先证伪直觉", "再拆变量对比"],
    "timeline": [{"t": "00:00", "text": "开场反例"}],
    "caution": "字幕由 AI 转写，专有名词建议核对。",
    "comment_short": "先证伪直觉，再动手。",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


class ChatStub:
    """只回固定结构的假大模型。"""

    def __init__(self) -> None:
        self.calls = 0

    def payload(self, body: dict) -> dict:
        self.calls += 1
        text = json.dumps(SUMMARY_JSON, ensure_ascii=False)
        return {
            "id": "chatcmpl-cli-stub",
            "object": "chat.completion",
            "model": body.get("model") or "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
        }


def start_stub() -> tuple[ThreadingHTTPServer, str, ChatStub]:
    stub = ChatStub()

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
    return server, f"http://{host}:{port}/v1", stub


def run_cli(cfg: Path, *argv: str) -> tuple[int, str, str, dict | None]:
    """跑一次命令行，返回 (退出码, stdout, stderr, stdout 的 JSON)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "video_workbench.cli", "--config", str(cfg), *argv],
        cwd=str(PROJ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = None
    text = proc.stdout.strip()
    if text.startswith("{"):
        # 一行 JSON 的契约：整段 stdout 必须能整体 parse
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return proc.returncode, proc.stdout, proc.stderr, payload


def pick_source(work: Path) -> Path | None:
    """从已有验收素材里截 25 秒，避免依赖外网。"""
    candidates = sorted((PROJ / "build" / "accept" / "media").rglob("source.mp4"))
    if not candidates:
        return None
    ffmpeg = None
    from video_workbench.infra.ffmpeg import FFmpeg

    ff = FFmpeg(prefer_dir=str(work / "bin"))
    ffmpeg, _probe = ff.require()
    dest = work / "src.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(candidates[0]),
            "-t",
            "25",
            "-c",
            "copy",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest if dest.is_file() else None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    work = PROJ / "build" / "accept" / "p6_cli"
    if work.exists() and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    server, base_url, stub = start_stub()
    import yaml

    cfg = {
        "ui": {"skin": "ark"},
        "llm": {
            "base_url": base_url,
            "api_key": "sk-cli-accept",
            "model": "stub-chat",
            "timeout_seconds": 60,
        },
        "storage": {
            "db_path": str(work / "wb.db"),
            "media_root": str(work / "media"),
            "cache_root": str(work / "cache"),
            # 用共享的验收模型目录：tiny 早就在那儿，不用每次重下 75MB
            "models_root": str(PROJ / "build" / "accept" / "models"),
            "ffmpeg_dir": str(work / "bin"),
        },
        "transcribe": {"model": "tiny", "device": "cpu", "compute_type": "int8"},
    }
    cfg_path = work / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    from video_workbench.app.service import WorkbenchService

    service = WorkbenchService(config_path=str(cfg_path), picker=None)
    try:
        # ------------------------------------------------ 0. 环境与素材
        code, out, err, payload = run_cli(cfg_path, "--json", "info")
        check("info 退出码 0", code == 0, f"rc={code} err={err[-160:]}")
        check(
            "info 的 stdout 只有一行 JSON",
            payload is not None and len([line for line in out.splitlines() if line.strip()]) == 1,
        )
        check(
            "info 报出配置与依赖", bool((payload or {}).get("data", {}).get("info", {}).get("deps"))
        )

        source = pick_source(work)
        if not source:
            print("没找到 build/accept/media 里的素材，先跑 scripts/accept_p1_download.py")
            return 1
        check("截出 25 秒素材", source.is_file(), str(source))

        # 项目目录必须真存在：转录/导出都往这里写文件，光有记录是跑不动的
        root = work / "media" / "cli"
        root.mkdir(parents=True, exist_ok=True)
        project = service.repo.project_create(
            title="CLI 闭环用例", source_kind="local", root_dir=str(root), stage="new"
        )
        service.repo.asset_upsert(project["id"], "source", str(source), size=source.stat().st_size)
        check("项目与源文件已入库", bool(project.get("id")), str(project.get("id")))

        # ------------------------------------------------ 1. 一条命令跑完闭环
        code, out, err, payload = run_cli(
            cfg_path,
            "--json",
            "run",
            "--project-id",
            project["id"],
            "--clips",
            "2",
            "--export",
            "1",
            "--mode",
            "fast",
        )
        data = (payload or {}).get("data") or {}
        check("run 退出码 0", code == 0, f"rc={code} err={err[-200:]}")
        check("run 的 stdout 是一行 JSON", payload is not None)
        check(
            "run 报出项目 id", data.get("project_id") == project["id"], str(data.get("project_id"))
        )
        steps = [s.get("step") for s in data.get("steps") or []]
        check("闭环里含转录", "transcribe" in steps, str(steps))
        check("闭环里含总结", "summary" in steps, str(steps))
        check(
            "总结正文非空",
            all(
                (s.get("chars") or 0) > 0
                for s in data.get("steps") or []
                if s.get("step") == "summary"
            ),
        )
        exports = data.get("exports") or []
        check("导出了 1 条切片", len(exports) == 1, str(exports))
        if exports:
            path = Path(str(exports[0].get("path") or ""))
            check(
                "成片真的存在",
                path.is_file() and path.stat().st_size > 0,
                f"{path} {path.stat().st_size if path.exists() else 0}B",
            )
        check("假大模型被真的调用过", stub.calls >= 1, f"{stub.calls} 次")

        # 库里也要有账：命令行不是绕过 RPC 自己记账的
        jobs = service.repo.job_list(limit=20, project_id=project["id"])
        kinds = {str(j.get("type")) for j in jobs}
        check(
            "任务表里有转录/总结/选段/导出",
            {"transcribe", "summary.run", "clip.analyze", "clip.export"} <= kinds,
            str(sorted(kinds)),
        )

        # ------------------------------------------------ 2. 退出码
        code, _out, _err, payload = run_cli(cfg_path, "--json", "jobs")
        check("jobs 退出码 0", code == 0)
        check("jobs 的 data 是列表", isinstance((payload or {}).get("data"), list))

        code, _out, _err, payload = run_cli(cfg_path, "--json", "summarize", "pj_不存在")
        check("找不到项目 → 退出码 2", code == 2, f"rc={code}")
        check(
            "找不到项目 → 错误码 NOT_FOUND",
            ((payload or {}).get("error") or {}).get("code") == "NOT_FOUND",
        )

        # 坏文件在预检就被拦下：这属于"输入不对"，必须是 2，不能混成"跑挂了"
        bad_a = work / "bad_a.mp4"
        bad_b = work / "bad_b.mp4"
        bad_a.write_bytes(b"not a video")
        bad_b.write_bytes(b"not a video")
        code, _out, err, payload = run_cli(cfg_path, "--json", "merge", str(bad_a), str(bad_b))
        check("合并坏文件 → 退出码 2（预检拦下）", code == 2, f"rc={code} tail={err[-200:]}")
        check(
            "合并坏文件 → 错误码 CONFLICT",
            ((payload or {}).get("error") or {}).get("code") == "CONFLICT",
            str((payload or {}).get("error")),
        )

        # 真·任务失败（模型连不上）必须是 1：脚本据此决定"重试"还是"改参数"
        dead = dict(cfg)
        dead["llm"] = dict(cfg["llm"], base_url="http://127.0.0.1:9/v1", timeout_seconds=5)
        dead_cfg = work / "config_dead_llm.yaml"
        dead_cfg.write_text(
            yaml.safe_dump(dead, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        code, _out, err, payload = run_cli(dead_cfg, "--json", "summarize", project["id"])
        check("模型连不上 → 退出码 1（任务失败）", code == 1, f"rc={code} tail={err[-160:]}")
        check(
            "模型连不上 → 错误码是 LLM_* / NETWORK",
            str(((payload or {}).get("error") or {}).get("code", ""))
            in ("NETWORK", "LLM_ERROR", "LLM_TIMEOUT"),
            str((payload or {}).get("error")),
        )

        proc = subprocess.run(
            [sys.executable, "-m", "video_workbench.cli", "download"],
            cwd=str(PROJ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        check("缺必填参数 → 退出码 2（argparse）", proc.returncode == 2, f"rc={proc.returncode}")
    finally:
        service.close()
        server.shutdown()

    print()
    if FAILS:
        print(f"P6-05 命令行验收：{len(FAILS)} 项失败")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("P6-05 命令行验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
