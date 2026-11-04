#!/usr/bin/env python
"""VideoWorkbench 的命令行入口（P6-05 批量自动化）。

设计上的两条硬约束：

1. **只走 RPC**。命令行不直接动 `Repo`/`jobs`/`ffmpeg`，全部通过 `service.router.dispatch`
   走界面同一条链路——同一套校验、同一套错误码。这样"界面能做的命令行能做"，
   也不会出现两套规则各自漂移。
2. **机器可读优先**。`--json` 时 stdout 只有一行 JSON，日志走 stderr；退出码固定：

   ======  ==========================================
   0       成功
   1       任务本身失败（PROCESS_FAILED / LLM_ERROR …）
   2       参数或前置条件不对（VALIDATION / NOT_FOUND / CONFLICT …）
   3       被取消
   130     中断（Ctrl+C）
   ======  ==========================================

子命令覆盖任务清单里的五件事：``download`` / ``transcribe`` / ``summarize`` / ``clip`` / ``merge``，
外加把单稿闭环串起来的 ``run``，和看状态的 ``info`` / ``jobs``。

跑法::

    python -m video_workbench.cli run --url https://www.bilibili.com/video/BV1xx411c7mD
    python -m video_workbench.cli --json info
    python -m video_workbench.cli clip clip_ab12cd34ef56 --count 6 --export 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from video_workbench import APP_NAME, __version__
from video_workbench.app.errors import WBError, RETRYABLE_CODES
from video_workbench.app.service import WorkbenchService, roles_payload
from video_workbench.config import WorkbenchConfig

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_BAD_INPUT = 2
EXIT_CANCELED = 3
EXIT_INTERRUPTED = 130

# 这些错误码属于"输入/前置条件",不是"跑挂了"
_BAD_INPUT_CODES = {
    "VALIDATION",
    "NOT_FOUND",
    "CONFLICT",
    "DEPENDENCY_MISSING",
    "UNSUPPORTED",
    "DISK_FULL",
    "AUTH_REQUIRED",
    "BILI_ERROR",
    # 「还没配大模型」不是跑挂了，是脚本写错地方/环境没准备好
    "LLM_NOT_CONFIGURED",
}
_DONE = ("succeeded", "failed", "canceled")

HELP_EPILOG = """\
例子:
  video_workbench.cli info
  video_workbench.cli download --url BV1xx411c7mD --quality 1080P
  video_workbench.cli run --url BV1xx411c7mD --clips 4 --export 2
  video_workbench.cli merge a.mp4 b.mp4 -o 成片.mp4 --mode precise
  video_workbench.cli --json jobs
"""


class CliError(Exception):
    """命令行自己的错（参数、找不到任务），带上要用的退出码。"""

    def __init__(self, message: str, *, code: str = "VALIDATION", exit_code: int = EXIT_BAD_INPUT):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code


def _exit_for(error: dict[str, Any]) -> int:
    code = str(error.get("code") or "INTERNAL")
    if code == "CANCELED":
        return EXIT_CANCELED
    if code in _BAD_INPUT_CODES:
        return EXIT_BAD_INPUT
    return EXIT_JOB_FAILED


def _is_retryable(error: dict[str, Any]) -> bool:
    value = error.get("retryable")
    if isinstance(value, bool):
        return value
    return str(error.get("code") or "") in RETRYABLE_CODES


class Cli:
    """把 RPC 调用、等待、输出三件事收在一起，子命令只写自己的流程。"""

    def __init__(self, argv: argparse.Namespace):
        self.args = argv
        self.json = bool(getattr(argv, "json", False))
        self.quiet = bool(getattr(argv, "quiet", False))
        self.timeout = float(getattr(argv, "timeout", 0) or 0)
        self.service: WorkbenchService | None = None

    # ------------------------------------------------------------ 基础设施

    def open(self) -> WorkbenchService:
        config = getattr(self.args, "config", "") or None
        self.service = WorkbenchService(config_path=config, picker=None)
        return self.service

    def close(self) -> None:
        if self.service is not None:
            try:
                self.service.close()
            finally:
                self.service = None

    def say(self, text: str) -> None:
        """给人看的过程信息：走 stderr，免得污染 --json 的 stdout。"""
        if self.quiet:
            return
        print(text, file=sys.stderr, flush=True)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        # 装配是**懒的**：以前 `main()` 在建 handler 之后就无条件 `cli.open()`，于是连
        # `roles` 这种纯读查询也要先建库、建 logs/、写 sidecar（2026-11-04 实测一次
        # `roles --config` 造成 6 项盘上变化）。谁真要干活谁才装配。
        if self.service is None:
            self.open()
        assert self.service is not None
        result = self.service.router.dispatch(method, params or {}, request_id="cli")
        if result.get("ok"):
            return result.get("data")
        error = dict(result.get("error") or {})
        raise CliError(
            str(error.get("message") or "调用失败"),
            code=str(error.get("code") or "INTERNAL"),
            exit_code=_exit_for(error),
        )

    def wait(self, job_id: str, *, label: str = "") -> dict[str, Any]:
        """轮询任务到结束。失败时抛出带退出码的 CliError（消息里带错误码）。"""
        if self.service is None:
            self.open()
        assert self.service is not None
        started = time.time()
        last = ""
        while True:
            job = self.service.jobs.get(job_id)
            status = str(job.get("status") or "")
            stage = str(job.get("stage") or "")
            percent = int(round(float(job.get("progress") or 0) * 100))
            line = f"{label or job_id} {percent:3d}% {stage}".rstrip()
            if line != last:
                self.say(line)
                last = line
            if status in _DONE:
                if status != "succeeded":
                    code = str(job.get("error_code") or ("CANCELED" if status == "canceled" else "PROCESS_FAILED"))
                    message = str(job.get("error_message") or status)
                    raise CliError(
                        f"{label or job_id} {status}：{message}",
                        code=code,
                        exit_code=EXIT_CANCELED if status == "canceled" else _exit_for({"code": code}),
                    )
                self.say(f"{label or job_id} 完成（{time.time() - started:.1f}s）")
                return job
            if self.timeout and time.time() - started > self.timeout:
                raise CliError(
                    f"{label or job_id} 超过 {self.timeout:.0f}s 还没结束（任务还在后台跑，可用 jobs 查看）",
                    code="PROCESS_FAILED",
                    exit_code=EXIT_JOB_FAILED,
                )
            time.sleep(0.25)

    def emit(self, cmd: str, data: Any) -> None:
        """成功输出：--json 一行，否则人话由子命令自己补。"""
        if self.json:
            payload = {"ok": True, "cmd": cmd, "version": __version__, "data": data}
            print(json.dumps(payload, ensure_ascii=False), flush=True)

    def fail(self, cmd: str, error: CliError) -> int:
        if self.json:
            payload = {
                "ok": False,
                "cmd": cmd,
                "version": __version__,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": _is_retryable({"code": error.code}),
                },
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        else:
            print(f"[{error.code}] {error.message}", file=sys.stderr, flush=True)
        return error.exit_code

    # ------------------------------------------------------------ 子命令

    def cmd_info(self) -> int:
        info = self.rpc("app.info")
        onboard = self.rpc("app.onboard")
        data = {"info": info, "onboard": onboard}
        self.emit("info", data)
        if not self.json:
            print(f"{APP_NAME} {info.get('version')}（{info.get('skin')} 皮肤）")
            print(f"配置：{info.get('config_path')}")
            print(f"数据库：{info.get('db_path')}")
            print(f"媒体目录：{info.get('media_root')}")
            deps = info.get("deps") or {}
            print("依赖：" + "、".join(f"{k}={'有' if v else '无'}" for k, v in deps.items()))
            steps = onboard.get("steps") or []
            self.say("体检：" + "；".join(f"{s.get('title')}={s.get('status')}" for s in steps))
        return EXIT_OK

    def cmd_roles(self) -> int:
        """角色名单与各自真源：只读 `summary.roles` 的载荷，不联网、不写配置、不调 `save()`。

        走 `roles_payload` 而不是 `self.rpc("summary.roles")` —— 载荷本身仍然只有一个 owner
        （`WorkbenchService.summary_roles()` 就是对它的一层委托，设置页读的是同一份），
        但这条命令为了取一份只读名单**不该把整个服务装配起来**：装配会建库
        （`service.py` 的 `open_and_migrate`）、建 `logs/`（`setup_logging`）、连带写 sidecar。
        2026-11-04 实测：一次 `roles --config` 造成 6 项盘上变化，用户什么都没点。
        体检类命令（`info` / 打包的 `--check`）照旧必须装配 —— 它们的工作正是验
        "配置写得进去、数据目录可写"，摘掉装配就成了 no-op。

        这条命令是给**打好的 exe** 用的离线自证：不开界面、不配密钥、不发任何请求，
        也能证明包里那几篇 txt 真的被读到、绑在哪些角色上（`prompt_sha8` 就是包内那一份的哈希）。
        """
        cfg = WorkbenchConfig.load(getattr(self.args, "config", "") or None)
        data = roles_payload(cfg)
        self.emit("roles", data)
        if not self.json:
            rows = list(data.get("roles") or [])
            # 退役段念的是**后端给的中文名**，不是裸 key。以前这里是 `str(r) for r in retired`，
            # 于是 CLI 打「shipin、standard、xueshu」三个英文 key，界面同一件事打的是带状态
            # 后缀的中文名——一个事实两个 owner，而 key 对用户没有信息量（那三个词从来没在
            # 界面上出现过）。显示名的唯一 owner 是权威层 `role_label()`，
            # 经载荷的 `retired_labels` 送进来（`core/summarizer.retired_row_labels`）。
            # **这里不许出现 key→中文 的表**（也别在这里拼停用后缀）：那样下次退役就得改
            # 两个地方，而 task-31 之后退役是三个键，改漏一个就是现在这个 bug。
            # 缺 `display`（老后端/字段没带上）时退回裸 key：兼容支宁可信息少一点，
            # 也不能因为一个字段缺失就让整条命令抛 KeyError。
            labels = {
                str(row.get("key")): str(row.get("display") or "")
                for row in (data.get("retired_labels") or [])
                if isinstance(row, dict)
            }
            shown = [labels.get(str(key)) or str(key) for key in (data.get("retired") or [])]
            retired = "、".join(shown) or "无"
            print(f"角色 {len(rows)} 个（默认：{data.get('default')}；已退役：{retired}）")
            for row in rows:
                src = row.get("prompt_source") or "内置压缩版（无 txt 真源）"
                sha8 = str(row.get("prompt_sha8") or "")
                tail = f" · {row.get('prompt_bytes')} B · {sha8}" if sha8 else ""
                print(f"  {row.get('key')}｜{row.get('name')}｜{row.get('passes')} 轮｜{src}{tail}")
        return EXIT_OK

    def cmd_jobs(self) -> int:
        jobs = self.rpc("job.list")
        self.emit("jobs", jobs)
        if not self.json:
            if not jobs:
                print("没有任务")
            for job in jobs:
                percent = int(round(float(job.get("progress") or 0) * 100))
                print(
                    f"{job.get('id')}  {str(job.get('status')):9s} {percent:3d}%  "
                    f"{job.get('type') or job.get('kind') or '':20s} {job.get('stage') or ''}"
                )
        return EXIT_OK

    def cmd_download(self) -> int:
        url = str(self.args.url or "").strip()
        if not url:
            raise CliError("download 需要 --url")
        resolved = self.rpc("download.resolve", {"url": url})
        items = list(self.args.items or []) or None
        if items is None:
            items = [dict(i) for i in (resolved.get("items") or [])]
        self.say(f"解析到 {len(items)} 个条目：{resolved.get('title') or url}")
        data = self.rpc(
            "download.start",
            {
                "url": url,
                "quality": str(self.args.quality or "1080P"),
                "items": items,
                "up_name": resolved.get("up_name") or "",
                "collection_title": resolved.get("collection_title") or "",
                "backend": str(self.args.backend or ""),
            },
        )
        job = data.get("job") or {}
        job_id = str(job.get("id") or "")
        if self.args.no_wait:
            self.emit("download", {"job": job, "resolved": resolved})
            print(job_id)
            return EXIT_OK
        final = self.wait(job_id, label="下载")
        project_ids = [i.get("project_id") for i in (final.get("result") or {}).get("items") or []]
        project_ids = [pid for pid in project_ids if pid]
        out = {"job": final, "project_ids": project_ids}
        self.emit("download", out)
        if not self.json:
            print("\n".join(project_ids))
        return EXIT_OK

    def cmd_transcribe(self) -> int:
        project_ids = list(self.args.project_ids or [])
        if not project_ids:
            raise CliError("transcribe 需要至少一个项目 id")
        data = self.rpc(
            "transcribe.start",
            {
                "project_ids": project_ids,
                "model": str(self.args.model or ""),
                "language": str(self.args.language or ""),
            },
        )
        jobs = list(data.get("jobs") or [])
        if not jobs:
            raise CliError("没有可转录的项目（可能已经排过队）", code="CONFLICT")
        done = []
        for job in jobs:
            done.append(self.wait(str(job.get("id")), label="转录"))
        out = {"jobs": done, "skipped": data.get("skipped") or []}
        self.emit("transcribe", out)
        if not self.json:
            for job in done:
                result = job.get("result") or {}
                print(f"{result.get('project_id') or ''} {result.get('segments') or 0} 段"
                      f"  {result.get('language') or ''}  {result.get('srt') or ''}")
        return EXIT_OK

    def summary_text(self, project_id: str) -> str:
        """任务结果里只带 id，正文得回库里取——否则调用方会以为「跑完了却没内容」。"""
        try:
            data = self.rpc("summary.get", {"id": project_id})
        except CliError:
            return ""
        summary = (data or {}).get("summary") or {}
        content = summary.get("content") or {}
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return content
        if isinstance(content, dict):
            return str(content.get("summary_md") or "")
        return str(content)

    def cmd_summarize(self) -> int:
        project_id = str(self.args.project_id or "")
        if not project_id:
            raise CliError("summarize 需要项目 id")
        data = self.rpc("summary.start", {"project_id": project_id, "role": str(self.args.role or "")})
        job = data.get("job") or data
        final = self.wait(str(job.get("id") or ""), label="总结")
        text = self.summary_text(project_id)
        self.emit("summarize", {"job": final, "summary_md": text})
        if not self.json:
            print(text)
        return EXIT_OK

    def cmd_clip(self) -> int:
        project_id = str(self.args.project_id or "")
        if not project_id:
            raise CliError("clip 需要项目 id")
        analyze = self.rpc(
            "clip.analyze",
            {"project_id": project_id, "kinds": list(self.args.kinds or []), "count": int(self.args.count or 0)},
        )
        job = analyze.get("job") or analyze
        self.wait(str(job.get("id") or ""), label="选段")
        data = self.rpc("clip.candidates", {"id": project_id})
        candidates = list(data.get("candidates") or [])
        exported: list[dict[str, Any]] = []
        take = int(self.args.export or 0)
        for cand in candidates[:take]:
            out = self.rpc(
                "clip.export",
                {
                    "project_id": project_id,
                    "candidate_id": cand.get("id") or "",
                    "mode": str(self.args.mode or "precise"),
                    "subtitle_mode": str(self.args.subtitle or "none"),
                    "aspect": str(self.args.aspect or "16x9"),
                },
            )
            out_job = out.get("job") or out
            exported.append(self.wait(str(out_job.get("id") or ""), label="导出"))
        payload = {"candidates": candidates, "exports": exported}
        self.emit("clip", payload)
        if not self.json:
            for cand in candidates:
                print(f"{cand.get('id')}  {cand.get('score')}分  {cand.get('title')}")
            for job in exported:
                print(str((job.get("result") or {}).get("path") or ""))
        return EXIT_OK

    def cmd_merge(self) -> int:
        paths = [str(p) for p in (self.args.paths or [])]
        if len(paths) < 2:
            raise CliError("merge 需要至少两个文件")
        report = self.rpc("merge.preflight", {"paths": paths, "mode": str(self.args.mode or "quick")})
        if not report.get("ok", True) and report.get("blocking"):
            raise CliError(
                "预检没通过：" + "；".join(str(b) for b in report.get("blocking") or []),
                code="CONFLICT",
            )
        data = self.rpc(
            "merge.start",
            {
                "paths": paths,
                "mode": str(self.args.mode or "quick"),
                "out_dir": str(self.args.out_dir or ""),
                "out_name": str(self.args.out_name or ""),
            },
        )
        job = data.get("job") or data
        final = self.wait(str(job.get("id") or ""), label="合并")
        path = str((final.get("result") or {}).get("path") or "")
        self.emit("merge", {"job": final, "path": path})
        if not self.json:
            print(path)
        return EXIT_OK

    def cmd_run(self) -> int:
        """一条命令跑完单稿闭环：下载 → 转录 → 总结（可选再选段导出）。"""
        steps: list[dict[str, Any]] = []
        url = str(self.args.url or "").strip()
        project_id = str(self.args.project_id or "").strip()
        if not url and not project_id:
            raise CliError("run 需要 --url 或 --project-id")

        if not project_id:
            self.args.no_wait = False
            before = len(steps)
            self.cmd_download_into(steps)
            assert len(steps) > before
            project_id = str(steps[-1].get("project_id") or "")
            if not project_id:
                raise CliError("下载没有拿到项目 id（看 jobs 里的明细）", code="PROCESS_FAILED", exit_code=EXIT_JOB_FAILED)

        if self.args.skip_transcribe:
            self.say("跳过转录（--skip-transcribe）")
        else:
            self.args.project_ids = [project_id]
            # produce 以前漏传 language，整条流水线的转录永远走自动探测——
            # 单跑 transcribe 能指定，串起来反而不能，这是最容易踩的一个不一致。
            data = self.rpc(
                "transcribe.start",
                {
                    "project_ids": [project_id],
                    "model": str(getattr(self.args, "model", "") or ""),
                    "language": str(getattr(self.args, "language", "") or ""),
                },
            )
            for job in data.get("jobs") or []:
                final = self.wait(str(job.get("id")), label="转录")
                steps.append({"step": "transcribe", "job": final.get("id"),
                              "segments": (final.get("result") or {}).get("segments")})

        if self.args.skip_summary:
            self.say("跳过总结（--skip-summary）")
        else:
            data = self.rpc("summary.start", {"project_id": project_id, "role": str(self.args.role or "")})
            job = data.get("job") or data
            final = self.wait(str(job.get("id") or ""), label="总结")
            steps.append({"step": "summary", "job": final.get("id"),
                          "chars": len(self.summary_text(project_id))})

        exports: list[dict[str, Any]] = []
        if int(self.args.export or 0) > 0:
            data = self.rpc(
                "clip.analyze",
                {"project_id": project_id, "kinds": list(self.args.kinds or []), "count": int(self.args.clips or 0)},
            )
            job = data.get("job") or data
            self.wait(str(job.get("id") or ""), label="选段")
            cands = list(self.rpc("clip.candidates", {"id": project_id}).get("candidates") or [])
            for cand in cands[: int(self.args.export or 0)]:
                out = self.rpc(
                    "clip.export",
                    {"project_id": project_id, "candidate_id": cand.get("id") or "",
                     "mode": str(self.args.mode or "precise")},
                )
                out_job = out.get("job") or out
                final = self.wait(str(out_job.get("id") or ""), label="导出")
                exports.append({"candidate_id": cand.get("id"),
                                "path": (final.get("result") or {}).get("path")})

        payload = {"project_id": project_id, "steps": steps, "exports": exports}
        self.emit("run", payload)
        if not self.json:
            print(project_id)
        return EXIT_OK

    def cmd_download_into(self, steps: list[dict[str, Any]]) -> None:
        """`run` 里的下载：跟 cmd_download 同一条链路，但把结果塞进 steps。"""
        url = str(self.args.url or "").strip()
        resolved = self.rpc("download.resolve", {"url": url})
        items = [dict(i) for i in (resolved.get("items") or [])]
        self.say(f"解析到 {len(items)} 个条目：{resolved.get('title') or url}")
        data = self.rpc(
            "download.start",
            {
                "url": url,
                "quality": str(self.args.quality or "1080P"),
                "items": items,
                "up_name": resolved.get("up_name") or "",
                "collection_title": resolved.get("collection_title") or "",
            },
        )
        job = data.get("job") or {}
        final = self.wait(str(job.get("id") or ""), label="下载")
        result = final.get("result") or {}
        for item in result.get("items") or []:
            project_id = str(item.get("project_id") or "")
            steps.append({"step": "download", "project_id": project_id, "job": final.get("id")})
            if project_id:
                break


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video_workbench.cli",
        description=f"{APP_NAME} 命令行（{__version__}）：单稿闭环与批量操作",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="", help="配置文件路径（默认 ~/.video-workbench/config.yaml；旧家 ~/.bili-scribe/config.yaml 只读）")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON，日志走 stderr")
    parser.add_argument("--quiet", action="store_true", help="不打印过程信息")
    parser.add_argument("--timeout", type=float, default=0, help="等待任务的超时秒数（0 = 不限）")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--roles", action="store_true",
                        help="等价于 `roles` 子命令：离线打印角色名单与各自真源（打包验收用）")
    subs = parser.add_subparsers(dest="cmd", required=True)

    subs.add_parser("info", help="环境与依赖状态")
    subs.add_parser("jobs", help="最近的任务")
    subs.add_parser("roles", help="角色名单与各自真源（离线：不联网、不写配置）")

    p = subs.add_parser("download", help="解析并下载")
    p.add_argument("--url", required=True)
    p.add_argument("--quality", default="1080P")
    p.add_argument("--backend", default="", help="ytdlp（默认）或 bbdown（兜底）")
    p.add_argument("--items", nargs="*", default=None, help="只下这几集（JSON 片段，一般不用）")
    p.add_argument("--no-wait", action="store_true", help="只提交任务，立刻返回任务 id")

    p = subs.add_parser("transcribe", help="转录")
    p.add_argument("project_ids", nargs="+")
    p.add_argument("--model", default="")
    p.add_argument("--language", default="")

    p = subs.add_parser("summarize", help="生成总结")
    p.add_argument("project_id")
    p.add_argument("--role", default="")

    p = subs.add_parser("clip", help="选段并导出切片")
    p.add_argument("project_id")
    p.add_argument("--count", type=int, default=0, help="候选条数（0 = 按配置）")
    p.add_argument("--export", type=int, default=0, help="导出前几条（0 = 只选不导）")
    p.add_argument("--kinds", nargs="*", default=None, help="highlight / segment")
    p.add_argument("--mode", default="precise", choices=["precise", "fast"], help="导出模式")
    p.add_argument("--subtitle", default="none")
    p.add_argument("--aspect", default="16x9")

    p = subs.add_parser("merge", help="合并多段视频")
    p.add_argument("paths", nargs="+")
    p.add_argument("-o", "--out-name", dest="out_name", default="")
    p.add_argument("--out-dir", dest="out_dir", default="")
    p.add_argument("--mode", default="quick", choices=["quick", "precise"])

    p = subs.add_parser("run", help="一条命令跑完单稿闭环")
    p.add_argument("--url", default="")
    p.add_argument("--project-id", dest="project_id", default="")
    p.add_argument("--quality", default="1080P")
    p.add_argument("--model", default="")
    p.add_argument("--language", default="", help="源语言（zh/en/ja…）；留空 = 自动探测")
    p.add_argument("--role", default="")
    p.add_argument("--clips", type=int, default=0)
    p.add_argument("--export", type=int, default=0)
    p.add_argument("--kinds", nargs="*", default=None)
    p.add_argument("--mode", default="precise", choices=["precise", "fast"], help="导出模式")
    p.add_argument("--skip-transcribe", action="store_true")
    p.add_argument("--skip-summary", action="store_true")

    return parser


_GLOBAL_FLAGS = ("--json", "--quiet", "--version", "--roles")
_GLOBAL_VALUES = ("--config", "--timeout")


def _has_subcommand(tokens: list[str]) -> bool:
    """这串参数里**到底写没写子命令**。

    不能按「是不是横杠开头」猜：`--config` 的路径值也不带横杠，那样
    `--roles --config X`（打包验收最自然的写法）会被误判成「已经写了命令」，
    旗标就静默失效了。所以旗标值成对跳过，剩下的裸 token 才是子命令。
    """
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        name = token.split("=", 1)[0]
        if name in _GLOBAL_VALUES and "=" not in token:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return True
    return False


def hoist_globals(argv: list[str]) -> list[str]:
    """把写在子命令后面的全局参数挪到最前面。

    `cli info --json` 是人的直觉写法，但 argparse 只认 `cli --json info`：
    子解析器不认识父级的参数，会直接 usage 报错退出 2。与其在文档里叮嘱顺序，
    不如把顺序当实现细节消化掉——两种写法都给同一个结果。
    """
    args = list(argv)
    head: list[str] = []
    body: list[str] = []
    seen_command = False
    index = 0
    while index < len(args):
        token = args[index]
        if not seen_command and not token.startswith("-"):
            seen_command = True
            body.append(token)
            index += 1
            continue
        if seen_command:
            name = token.split("=", 1)[0]
            if name in _GLOBAL_FLAGS:
                head.append(token)
                index += 1
                continue
            if name in _GLOBAL_VALUES:
                if "=" in token:
                    head.append(token)
                    index += 1
                    continue
                head.append(token)
                if index + 1 < len(args):
                    head.append(args[index + 1])
                    index += 2
                else:
                    index += 1
                continue
        body.append(token)
        index += 1
    return head + body


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = build_parser()
    tokens = hoist_globals(list(sys.argv[1:] if argv is None else argv))
    # `--roles` 是打包验收用的旗标（`VideoWorkbench.exe --roles`），但 argparse 要求必须给
    # 子命令，所以这里把它翻译成 `roles`。**只在确实没写子命令时翻译** —— 判据要跟
    # 真实命令名比，不能拿「是不是横杠开头」猜：`--config <路径>` 的值也不带横杠，
    # 把它当命令名就会让 `--roles --config X` 这条最自然的写法报错。
    # `info --roles` 这种两个都写的自相矛盾用法下面直接报错，不静默丢掉用户要的那一份。
    if "--roles" in tokens and not _has_subcommand(tokens):
        # 命令名要插在**全局旗标之后**：argparse 只认 `--config X roles`，
        # 把 roles 放到最前就把 `--config` 挤到子解析器里去了
        # （`error: unrecognized arguments: --config …`，我第一版就是这么红的）。
        tokens = [t for t in tokens if t != "--roles"] + ["roles"]
    args = parser.parse_args(tokens)
    if args.roles and args.cmd != "roles":
        parser.error(f"--roles 就是 roles 子命令，别再和 {args.cmd} 一起写")
    cli = Cli(args)
    handler = {
        "info": cli.cmd_info,
        "jobs": cli.cmd_jobs,
        "roles": cli.cmd_roles,
        "download": cli.cmd_download,
        "transcribe": cli.cmd_transcribe,
        "summarize": cli.cmd_summarize,
        "clip": cli.cmd_clip,
        "merge": cli.cmd_merge,
        "run": cli.cmd_run,
    }.get(args.cmd)
    if handler is None:
        parser.error(f"未知子命令：{args.cmd}")
        return EXIT_BAD_INPUT

    try:
        # 这里**不再无条件 `cli.open()`**：装配服务会建库、建 logs/、连带写 sidecar，
        # 而 `roles` 这类纯读出口不该在用户没点任何保存的时候动他的盘。
        # 真要服务的命令由 `rpc()` / `wait()` 懒装配，行为对它们完全不变。
        return handler()
    except CliError as exc:
        return cli.fail(args.cmd, exc)
    except WBError as exc:  # 服务层直接抛上来的（理论上都被 dispatch 收成信封了）
        return cli.fail(args.cmd, CliError(exc.message, code=exc.code, exit_code=_exit_for({"code": exc.code})))
    except KeyboardInterrupt:
        print("[INTERRUPTED] 已中断", file=sys.stderr, flush=True)
        return EXIT_INTERRUPTED
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
