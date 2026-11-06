"""日志与诊断包。

三件事：

1. **日志要留得下来**。程序关掉之后内存里的东西就没了，而用户回头要看的
   恰恰是"为什么失败"。所以统一写到 ``<cache_root>/../logs/wb-YYYYMMDD.log``，
   一天一个文件，不做轮转（按 30 天清理，见 ``prune``）。
2. **日志里不能有凭据**。B站 cookie、LLM 的 api key、Authorization 头都可能
   被写进 stderr 或请求体里；落盘前一律过 ``redact``。
3. **诊断包要能发给别人看**。一个 zip：环境信息 + 脱敏后的配置 + 最近日志尾巴。
   不做成"一键上传"——那是要把用户数据发到我们服务器，本项目不发这种东西。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LOG_NAME = "video_workbench"
FILE_FMT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# 凭据字段名。键名命中就以整值替换——比按值猜可靠得多。
SECRET_KEYS = (
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "authorization",
    "password",
    "secret",
    "sessdata",
    "bili_jct",
    "buvid3",
    "dedeuserid",
)

# 文本里的凭据形态。顺序有意义：先打掉长 token，再打 bearer 头。
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]{4,}"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{6,}"), r"\1***"),
    (
        re.compile(r"(?i)\b(SESSDATA|bili_jct|buvid3|DedeUserID|access_key)=([^;&\s\"']{4,})"),
        r"\1=***",
    ),
    # JSON / YAML 两种写法：值前面可能还有一个引号
    (
        re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\"?\s*[:=]\s*\"?)([^\s,;\"'}]{6,})"),
        r"\1***",
    ),
    (re.compile(r"(?i)(cookie\s*:\s*)([^\r\n]{8,})"), r"\1***"),
)


# ------------------------------------------------------------------ 脱敏


def redact(text: str) -> str:
    """把文本里像凭据的片段抹掉。**宁可多抹，不可漏抹。**"""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_obj(value: Any, *, _depth: int = 0) -> Any:
    """递归脱敏：键名命中 ``SECRET_KEYS`` 的整值换成 ``***``，字符串过 ``redact``。"""
    if _depth > 8:  # 配置不该这么深；真有环就停下，别把栈打穿
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in SECRET_KEYS):
                out[key] = "***" if item not in (None, "") else ""
            else:
                out[key] = redact_obj(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_obj(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


# ------------------------------------------------------------------ 落盘


def log_dir_for(cache_root: str | Path) -> Path:
    """日志目录与缓存目录同级：``%APPDATA%/VideoWorkbench/logs``。

    跟着 cache_root 走而不是另配一个绝对路径——用户改了缓存目录，
    日志也应该跟着走（诊断包里"日志在哪"只有一个答案）。
    """
    # 传进来的可能是没展开的原始配置串（`%APPDATA%/...`），这里再兜一次底：
    # 展开顺序与 config._abs 一致——先环境变量，再家目录，最后转绝对路径。
    return Path(os.path.expanduser(os.path.expandvars(str(cache_root)))).resolve().parent / "logs"


def setup_logging(
    log_dir: str | Path,
    *,
    level: int = logging.INFO,
    console: bool = True,
    session_tag: str = "",
) -> dict[str, Any]:
    """挂上文件与（可选的）控制台输出；重复调用只换文件，不叠加 handler。"""
    directory = Path(log_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone()
    file_path = directory / f"wb-{stamp.strftime('%Y%m%d')}.log"

    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_wb_owned", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    formatter = logging.Formatter(FILE_FMT, DATE_FMT)

    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._wb_owned = True  # type: ignore[attr-defined]
    logger.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream._wb_owned = True  # type: ignore[attr-defined]
        logger.addHandler(stream)

    logger.info(
        "==== 会话开始 pid=%s python=%s platform=%s %s ====",
        os.getpid(),
        platform.python_version(),
        platform.platform(),
        session_tag,
    )
    return {"dir": str(directory), "file": str(file_path)}


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOG_NAME}.{name}" if name else LOG_NAME)


def log_line(message: str, *, level: int = logging.INFO, name: str = "job") -> None:
    """给 JobManager 之类的地方用的简易入口：一条字符串，落盘前脱敏。"""
    get_logger(name).log(level, redact(str(message)))


def tail_lines(path: str | Path, limit: int = 400) -> list[str]:
    """读日志尾巴。文件不在就回空——诊断包不该因为"今天还没日志"就失败。"""
    target = Path(path).expanduser()
    if not target.is_file():
        return []
    limit = max(1, int(limit))
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-limit:]]


def list_logs(log_dir: str | Path) -> list[dict[str, Any]]:
    directory = Path(log_dir).expanduser()
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for item in sorted(directory.glob("wb-*.log"), reverse=True):
        try:
            stat = item.stat()
        except OSError:
            continue
        rows.append(
            {"name": item.name, "path": str(item), "bytes": stat.st_size, "mtime": stat.st_mtime}
        )
    return rows


def prune(log_dir: str | Path, *, keep_days: int = 30) -> int:
    """删掉 ``keep_days`` 天前的日志。返回删了几个文件。"""
    directory = Path(log_dir).expanduser()
    if not directory.is_dir() or keep_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    removed = 0
    for item in directory.glob("wb-*.log"):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ------------------------------------------------------------------ 诊断包


def environment_lines() -> list[str]:
    return [
        f"python: {platform.python_version()} ({platform.python_implementation()})",
        f"platform: {platform.platform()}",
        f"machine: {platform.machine()} / {platform.processor() or '未知'}",
        f"executable: {sys.executable}",
        f"cwd: {os.getcwd()}",
        f"now: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
    ]


def write_bundle(
    out_path: str | Path,
    *,
    title: str,
    sections: Iterable[tuple[str, str]] = (),
    config_yaml: str = "",
    log_file: str | Path | None = None,
    log_lines: int = 400,
) -> dict[str, Any]:
    """写一个诊断 zip：``sections`` 是若干段纯文本（环境/数据库/错误码表…），
    ``config_yaml`` 会被脱敏后再放进去，日志放"最近 N 行"的尾巴。
    """
    target = Path(out_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    head = [f"# {title}", f"生成时间: {stamp}", ""]
    for name, body in sections:
        head.append(f"## {name}")
        head.append(redact(str(body).rstrip()) or "（空）")
        head.append("")
    report = "\n".join(head)

    entries: list[str] = ["report.txt"]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.txt", report)
        if config_yaml:
            entries.append("config.redacted.yaml")
            archive.writestr("config.redacted.yaml", redact(config_yaml))
        if log_file:
            lines = tail_lines(log_file, log_lines)
            if lines:
                entries.append("log.txt")
                archive.writestr("log.txt", redact("\n".join(lines)))

    size = target.stat().st_size if target.is_file() else 0
    return {
        "path": str(target),
        "bytes": size,
        "entries": entries,
        "log_lines": len(tail_lines(log_file, log_lines)) if log_file else 0,
    }
