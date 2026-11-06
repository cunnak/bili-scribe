"""P5-03 验收：错误分类与日志 / 诊断包。

真后端、真配置、真日志文件——不模拟任何一环：

1. 启动后**日志文件真的存在**，里面有本次会话的开头一行；
2. 跑一个真任务，任务的每一行日志都同时落进文件（界面抽屉关掉也查得到）；
3. `app.diagnostics` 产出的 zip 真的能打开，里面有三样东西：报告、脱敏配置、日志尾巴；
4. 配置里的 api key **不出现在包里**（我们故意在内存里塞一个假 key）；
5. 诊断包不允许写到白名单之外；
6. `app.log_tail` 返回的文本同样看不到凭据。

跑法：`./.venv/Scripts/python.exe scripts/accept_p5_diagnostics.py --config build/accept/p2.yaml`
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
for path in (PROJ, PROJ / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

OK = 0
FAIL = 0
ROWS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        ROWS.append((True, name, detail))
        print(f"[ok]   {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        ROWS.append((False, name, detail))
        print(f"[FAIL] {name} —— {detail}")


def call(service, method: str, params: dict | None = None) -> tuple[bool, dict]:
    reply = service.dispatch(method, params or {})
    if reply.get("ok"):
        return True, reply.get("data") or {}
    return False, reply.get("error") or {}


def wait_job(service, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        ok, data = call(service, "job.get", {"id": job_id})
        if ok:
            row = data.get("job", data)
            if row.get("status") in ("succeeded", "failed", "canceled"):
                return row
        time.sleep(0.05)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args()

    from video_workbench.app.service import WorkbenchService

    service = WorkbenchService(args.config)
    # 故意塞一个可识别的假 key：如果它出现在诊断包里，说明脱敏有漏洞
    FAKE_KEY = "sk-accept5diagnostics0001"
    service.cfg.llm.api_key = FAKE_KEY
    try:
        info_ok, info = call(service, "app.info")
        check(
            "app.info 报日志位置",
            info_ok and bool(info.get("log_dir")) and bool(info.get("log_file")),
            f"{info.get('log_dir')}",
        )
        log_file = Path(str(info.get("log_file") or ""))
        check("日志文件真的在盘上", log_file.is_file(), str(log_file))
        head = (
            log_file.read_text(encoding="utf-8", errors="replace")[:800]
            if log_file.is_file()
            else ""
        )
        check("日志开头记了会话信息", "会话开始" in head, head.splitlines()[:1][0] if head else "")
        check(
            "日志目录名是 logs",
            Path(str(info.get("log_dir"))).name == "logs",
            str(info.get("log_dir")),
        )

        # 跑一个真任务，看它的日志有没有落盘
        job_ok, job_data = call(service, "job.demo.submit", {"steps": 5})
        job = job_data.get("job", job_data)
        job_id = str(job.get("id") or "")
        row = wait_job(service, job_id) if job_id else {}
        check("任务跑完了（demo）", row.get("status") == "succeeded", str(row.get("status")))
        for handler in __import__("logging").getLogger("video_workbench").handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
        check("任务日志落盘（按 job id 可查）", bool(job_id) and job_id in text, job_id)

        # 诊断包
        zip_ok, bundle = call(service, "app.diagnostics", {"log_lines": 200})
        target = Path(str(bundle.get("path") or ""))
        check("诊断包已生成", zip_ok and target.is_file(), str(target))
        check("诊断包落在媒体目录下", "diagnostics" in target.as_posix(), target.as_posix())
        entries: list[str] = []
        report = config_text = log_text = ""
        if target.is_file():
            with zipfile.ZipFile(target) as archive:
                entries = archive.namelist()
                report = archive.read("report.txt").decode("utf-8", "replace")
                if "config.redacted.yaml" in entries:
                    config_text = archive.read("config.redacted.yaml").decode("utf-8", "replace")
                if "log.txt" in entries:
                    log_text = archive.read("log.txt").decode("utf-8", "replace")
        check(
            "包里有报告/脱敏配置/日志三样",
            {"report.txt", "config.redacted.yaml", "log.txt"} <= set(entries),
            ",".join(entries),
        )
        check("报告带数据库版本", "schema_version" in report, _line(report, "schema_version"))
        check(
            "报告带错误码表",
            "VALIDATION" in report and "CONFLICT" in report,
            _line(report, "VALIDATION"),
        )
        check("报告带依赖状态", "ffmpeg:" in report or "ffmpeg" in report, _line(report, "ffmpeg"))
        check("报告带日志目录", "logs" in report, _line(report, "logs"))
        check("配置里的 api key 没泄露", FAKE_KEY not in config_text and FAKE_KEY not in report, "")
        check(
            "脱敏后仍能看出是哪个字段",
            "api_key" in config_text or "llm:" in config_text,
            _line(config_text, "api_key") or _line(config_text, "llm"),
        )
        check("日志尾巴里有本次会话", "会话开始" in log_text, str(len(log_text)))
        check("日志尾巴里没有 api key", FAKE_KEY not in log_text, "")

        # 白名单
        bad_ok, bad = call(service, "app.diagnostics", {"path": "C:/Windows/Temp/wb-diag.zip"})
        check(
            "包不允许写到白名单外",
            (not bad_ok) and bad.get("code") == "VALIDATION",
            json.dumps(bad, ensure_ascii=False),
        )
        inside = Path(str(service.cfg.media_root)) / "diag-accept.zip"
        good_ok, good = call(service, "app.diagnostics", {"path": str(inside)})
        check("指定媒体目录内的路径可以", good_ok and inside.is_file(), str(inside))

        # 日志尾巴 RPC
        tail_ok, tail = call(service, "app.log_tail", {"lines": 120})
        check(
            "app.log_tail 返回文本", tail_ok and bool(tail.get("text")), f"{tail.get('lines')} 行"
        )
        check("log_tail 里的凭据也被抹掉", FAKE_KEY not in str(tail.get("text")), "")
        check(
            "log_tail 列出日志文件",
            bool(tail.get("files")),
            json.dumps([row.get("name") for row in tail.get("files") or []], ensure_ascii=False),
        )

        # 错误码可读性：直接从表里取，逐条念一遍
        from video_workbench.app.errors import ERROR_CODES

        bad_rows = [
            code
            for code, text in ERROR_CODES.items()
            if not text.strip() or text.strip().endswith("。")
        ]
        check("每个错误码都有可读中文文案", not bad_rows, ",".join(bad_rows))
        check("错误码表够用（≥24 个）", len(ERROR_CODES) >= 24, str(len(ERROR_CODES)))
    finally:
        service.close()

    print()
    print(
        f"P5-03 错误分类与日志验收：{'全部通过' if FAIL == 0 else f'{FAIL} 项失败'}（{OK}/{OK + FAIL}）"
    )
    if FAIL:
        print("\n失败项：")
        for ok, name, detail in ROWS:
            if not ok:
                print(f"  - {name} —— {detail}")
    return 0 if FAIL == 0 else 1


def _line(text: str, needle: str) -> str:
    for line in str(text).splitlines():
        if needle in line:
            return line.strip()[:120]
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
