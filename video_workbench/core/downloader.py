"""yt-dlp 下载器（方案书 5.3.4）。

设计上只有三个决定，其余都是执行细节：

1. **子进程 + JSON 行进度**。不 ``import yt_dlp``（那是把它当库用，一旦它阻塞或
   崩掉，整个界面跟着假死）。父进程只读 stdout，一行一个事件。
2. **取消要杀进程树**。yt-dlp 会 fork 出 ffmpeg 做合并；只 kill 父进程会留下
   一个占着文件句柄的 ffmpeg，于是 ``.part`` 删不掉、目录被锁。Windows 上用
   ``taskkill /T /F``。
3. **失败要分类**。用户看到"下载失败"和看到"需要大会员"是完全两种反应。
   分类只做一件事：把 yt-dlp 的英文 stderr 映射到我们自己的错误码。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..app.errors import WBError

# 画质 → 格式选择器（方案书 5.3.4）。
# 1080P/720P 明确要 avc1，因为剪辑软件对 hevc 支持参差；4K/1080P+ 才放开编码，
# 但导出前会统一转 H.264（Phase 3 的事）。
QUALITY_FORMATS: dict[str, str] = {
    "720P": "bv*[height<=720][vcodec^=avc1]+ba/b[height<=720]",
    "1080P": "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080]",
    "1080P+": "bv*[height<=1080]+ba/b[height<=1080]",
    "4K": "bv*[height<=2160]+ba/b[height<=2160]",
}
DEFAULT_QUALITY = "1080P"

_PROGRESS_PREFIX = "download:"
_PRINT_PREFIX = "wb-meta:"

# stderr 关键词 → 错误码。顺序有意义：先判权限，再判地区，最后才是网络。
_FAILURE_PATTERNS: list[tuple[str, str, str]] = [
    (r"大会员|premium|vip", "AUTH_REQUIRED", "这个视频需要大会员或更高权限"),
    (r"login|登录|cookies|SESSDATA", "AUTH_REQUIRED", "需要登录后才能下载"),
    (r"geo|地区|region|not available in your country", "GEO_BLOCKED", "该视频在当前地区不可用"),
    (r"不存在|已删除|404|not found", "NOT_FOUND", "视频不存在或已删除"),
    (r"412|429|rate.?limit|too many requests|风控", "RATE_LIMITED", "触发限流，请稍后再试"),
    (r"no space|磁盘|Errno 28", "DISK_FULL", "磁盘空间不足"),
    (r"timed? ?out|timeout|connection|dns|proxy|网络|SSL", "NETWORK", "网络不通或超时"),
]

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")


# --------------------------------------------------------------------- 命令构造


def format_selector(quality: str) -> str:
    return QUALITY_FORMATS.get((quality or "").upper(), QUALITY_FORMATS[DEFAULT_QUALITY])


def ffmpeg_location() -> str | None:
    """ffmpeg 所在目录（yt-dlp 的 ``--ffmpeg-location`` 要目录不要文件）。"""
    exe = shutil.which("ffmpeg")
    return str(Path(exe).parent) if exe else None


def build_command(
    *,
    yt_dlp_bin: str,
    url: str,
    home: str | Path,
    quality: str = DEFAULT_QUALITY,
    cookie_file: str | Path | None = None,
    ffmpeg_dir: str | None = None,
    single: bool = True,
) -> list[str]:
    """拼 yt-dlp 命令行。纯函数，测试直接对参数断言。"""
    cmd: list[str] = [
        yt_dlp_bin,
        "--newline",
        "--no-colors",
        "--no-warnings",
        # 进度用 JSON 打出来：自己解析 "[download] 12.3%" 这种人类输出迟早被改版打断
        "--progress-template",
        f"{_PROGRESS_PREFIX}%(progress)j",
        # 下完打一行元信息，用来回填"实际画质"
        "--print",
        f"after_move:{_PRINT_PREFIX}%(height)s|%(format_id)s|%(filepath)s",
        "--merge-output-format",
        "mp4",
        "-f",
        format_selector(quality),
        "--paths",
        f"home:{Path(home)}",
        "--paths",
        f"temp:{Path(home) / '.tmp'}",
        "-o",
        "source.%(ext)s",
    ]
    if single:
        cmd.append("--no-playlist")
    if cookie_file:
        cmd += ["--cookies", str(cookie_file)]
    if ffmpeg_dir:
        cmd += ["--ffmpeg-location", ffmpeg_dir]
    cmd.append(url)
    return cmd


# --------------------------------------------------------------------- 进度解析


def parse_line(line: str) -> dict[str, Any] | None:
    """把一行 stdout 变成事件。不认识的返回 None（原样进日志）。"""
    text = (line or "").strip()
    if not text:
        return None

    if text.startswith(_PROGRESS_PREFIX):
        payload = text[len(_PROGRESS_PREFIX):]
        try:
            raw = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        total = raw.get("total_bytes") or raw.get("total_bytes_estimate") or 0
        done = raw.get("downloaded_bytes") or 0
        percent = (done / total * 100.0) if total else 0.0
        return {
            "type": "progress",
            "status": raw.get("status") or "downloading",
            "bytes": int(done),
            "total": int(total),
            "percent": round(min(100.0, max(0.0, percent)), 2),
            "speed": raw.get("speed"),
            "eta": raw.get("eta"),
            "fragment": raw.get("fragment_index"),
            "fragment_total": raw.get("fragment_count"),
        }

    if text.startswith(_PRINT_PREFIX):
        body = text[len(_PRINT_PREFIX):]
        height, _, rest = body.partition("|")
        fmt_id, _, filepath = rest.partition("|")
        try:
            h = int(height)
        except ValueError:
            h = 0
        return {
            "type": "finished",
            "height": h,
            "format_id": fmt_id,
            "path": filepath,
        }

    # FFmpeg 合并阶段会打这种行，保留一个粗粒度进度，免得 100% 之后界面像卡死
    if "[Merger]" in text or "Merging formats" in text:
        return {"type": "merging"}
    match = _PERCENT.search(text)
    if match and "download" in text.lower():
        return {"type": "progress", "percent": float(match.group(1))}
    return None


def classify_failure(text: str) -> WBError:
    """把 yt-dlp 的失败输出翻成带码的错误。认不出的就是 PROCESS_FAILED。"""
    detail = (text or "").strip()
    tail = "\n".join(detail.splitlines()[-50:])
    for pattern, code, message in _FAILURE_PATTERNS:
        if re.search(pattern, tail, re.IGNORECASE):
            return WBError(code, message, tail[-4000:], retryable=code in {"NETWORK", "RATE_LIMITED", "PROCESS_FAILED"})
    return WBError("PROCESS_FAILED", "yt-dlp 执行失败", tail[-4000:], retryable=True)


# --------------------------------------------------------------------- 进程管理


def kill_tree(pid: int) -> None:
    """杀进程树。Windows 用 taskkill /T；其他平台退化成 killpg。

    单独抽出来是为了能被测试替换掉——否则测"取消"就得真的起一个进程。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), 15)
    except OSError:
        pass


def cleanup_partials(directory: str | Path) -> list[Path]:
    """删掉 ``.part`` / ``.ytdl`` 残留。取消后目录必须看起来像没下过。"""
    root = Path(directory)
    removed: list[Path] = []
    if not root.exists():
        return removed
    for pattern in ("*.part", "*.ytdl", "*.part-Frag*"):
        for path in root.rglob(pattern):
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    tmp = root / ".tmp"
    if tmp.is_dir():
        for path in tmp.rglob("*"):
            if path.is_file():
                try:
                    path.unlink()
                    removed.append(path)
                except OSError:
                    pass
    return removed


def ensure_disk(path: str | Path, need_bytes: int) -> None:
    """磁盘守卫（P1-06）。实现收口在 ``infra/files.ensure_free_space``。"""
    if not need_bytes:
        return
    from ..infra.files import ensure_free_space

    ensure_free_space(path, need_bytes)


# --------------------------------------------------------------------- 下载


@dataclass
class DownloadResult:
    path: Path
    height: int = 0
    format_id: str = ""
    quality_requested: str = DEFAULT_QUALITY
    elapsed_s: float = 0.0
    events: int = 0
    stderr_tail: str = ""

    @property
    def quality_actual(self) -> str:
        """把实际高度翻回人话。yt-dlp 给的是数字，界面要的是 720P/1080P。"""
        if not self.height:
            return ""
        for label, nominal in (("4K", 2160), ("1440P", 1440), ("1080P", 1080), ("720P", 720), ("480P", 480), ("360P", 360)):
            # B 站偶尔给 1078/1035 这种数，允许 ±3% 的邻近误差，但 1234 不能被说成 1080P
            if abs(self.height - nominal) <= max(8, nominal * 0.03):
                return label
        return f"{self.height}P"


Runner = Callable[..., tuple[int, str]]


# ------------------------------------------------------------- BBDown 兜底（P1-08）


BBDOWN_NAMES = ("BBDown.exe", "BBDown")
BACKENDS = ("ytdlp", "bbdown")


def _bbdown_candidates(configured: str | Path | None = None) -> list[Path]:
    """BBDown 的可能位置，按可信度排序。

    配置里写的路径排第一（用户显式指定就该优先），然后是 PATH、工具目录、当前目录。
    只找文件，不看它能不能跑——能不能跑由真起进程时决定。
    """
    out: list[Path] = []
    seen: set[str] = set()

    def push(value: str | Path | None) -> None:
        if not value:
            return
        path = Path(value).expanduser()
        key = str(path).lower()
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    push(configured)
    for name in BBDOWN_NAMES:
        found = shutil.which(name)
        if found:
            push(found)
    tools = Path(__file__).resolve().parent.parent.parent / "tools"
    for name in BBDOWN_NAMES:
        push(tools / name)
        push(Path.cwd() / name)
    return out


def ytdlp_info(configured: str | Path | None = None) -> dict[str, Any]:
    """yt-dlp 的探测结果，形状与 :func:`bbdown_info` 对齐（设置页并排显示）。"""
    path = str(configured or "")
    if not path:
        path = shutil.which("yt-dlp") or shutil.which("yt_dlp") or ""
    module = _module_available("yt_dlp")
    found = bool(path) or module
    return {
        "found": found,
        "path": path,
        "name": Path(path).name if path else ("yt_dlp（模块）" if module else ""),
        "module": module,
        "configured": str(configured or ""),
        "hint": "" if found else "缺少 yt-dlp：pip install yt-dlp，或把 yt-dlp.exe 放进 tools/ 目录",
    }


def find_bbdown(configured: str | Path | None = None) -> str:
    """找到可用的 BBDown，找不到返回空串。**不抛异常**——探测不该让界面报错。"""
    for path in _bbdown_candidates(configured):
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return ""


def bbdown_info(configured: str | Path | None = None) -> dict[str, Any]:
    """给设置页和 ``download.backends`` 用的探测结果。"""
    path = find_bbdown(configured)
    return {
        "found": bool(path),
        "path": path,
        "name": Path(path).name if path else "",
        "configured": str(configured or ""),
        "hint": ""
        if path
        else "没检测到 BBDown：把 BBDown.exe 放进 tools/ 目录，或在设置里填它的绝对路径",
    }


def build_bbdown_command(
    *,
    binary: str,
    url: str,
    home: str | Path,
    quality: str = DEFAULT_QUALITY,
    cookie_file: str | Path | None = None,
    extra_args: str = "",
) -> list[str]:
    """组装 BBDown 命令行。

    BBDown 与 yt-dlp 的参数名不同：``--work-dir`` 指定落盘目录，画质用 ``-q`` 传它自己的
    优先级串。只传"确定存在"的几个参数，其余交给用户用 ``download.bbdown_args`` 追加
    ——不猜不认识的旗标，猜错的表现是"下载成功但文件在别处"。
    """
    argv: list[str] = [binary, url, "--work-dir", str(Path(home))]
    quality_key = str(quality or "").strip().upper()
    if quality_key in {"720P", "1080P", "1080P+", "4K"}:
        argv += ["-q", quality_key]
    if cookie_file:
        # BBDown 认浏览器导出的 cookie 文本，我们手上的 key.bin 已导成 Netscape 格式。
        argv += ["--cookie", str(cookie_file)]
    extra = str(extra_args or "").strip()
    if extra:
        argv += shlex.split(extra, posix=False)
    return argv


def parse_bbdown_line(raw: str) -> dict[str, Any] | None:
    """从 BBDown 的一行输出里抽进度。

    BBDown 的进度长这样：``[进度] 12.5% (1.2 MB/9.4 MB)``，而且刷新很密。
    只认带百分号的下载行，其余当天日志丢掉——免得把"正在解析"读成 0%。
    """
    text = (raw or "").strip()
    if not text or "%" not in text:
        return None
    match = _PERCENT.search(text)
    if not match:
        return None
    try:
        percent = float(match.group(1))
    except ValueError:
        return None
    return {"type": "progress", "percent": max(0.0, min(100.0, percent)), "raw": text}


def _bbdown_output(home: Path) -> Path | None:
    """BBDown 落的文件名取决于视频标题，取最新的那个视频文件。"""
    best: Path | None = None
    best_time = -1.0
    for pattern in ("*.mp4", "*.mkv", "*.flv"):
        for path in home.glob(pattern):
            if path.suffix == ".part":
                continue
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if stamp > best_time:
                best, best_time = path, stamp
    return best


def _normalize_bbdown_output(home: Path, produced: Path | None) -> Path | None:
    """把 BBDown 的产物统一成 ``home/source.<ext>``——两个后端共用的**暂存名**。

    改名只在这里做一次，失败就原样返回，不因为一个文件名判下载失败。

    下游真正按这个名字找文件的只有四处：``layout.SOURCE_STAGING``、本文件的 ``-o``、
    ``_bbdown_output`` 与这里的兜底 glob。真实消费者读的是数据库里的路径，所以源文件
    随后被 ``layout.finalize_source`` 改成视频标题不会波及它们。
    """
    if produced is None or not produced.exists():
        return produced
    if produced.stem == "source" and produced.parent == home:
        return produced
    target = home / f"source{produced.suffix.lower()}"
    try:
        if target.exists():
            target.unlink()
        produced.replace(target)
    except OSError:
        return produced
    return target


def _resolve_bbdown(bbdown_bin: str | Path | None) -> str:
    binary = find_bbdown(bbdown_bin)
    if not binary:
        raise WBError(
            "DEPENDENCY_MISSING",
            "没检测到 BBDown：把 BBDown.exe 放进 tools/ 目录，或在设置里填它的绝对路径",
        )
    return binary


def _default_runner(cmd: list[str], cwd: str | None, on_line: Callable[[str], None], cancel: Callable[[], bool]) -> tuple[int, str]:
    """起子进程、逐行回调、可取消。返回 ``(returncode, 全部输出)``。"""
    started = time.time()
    proc = subprocess.Popen(  # noqa: S603 —— 参数是列表，不经 shell
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=0x00000200 if os.name == "nt" else 0,  # CREATE_NEW_PROCESS_GROUP
    )
    lines: list[str] = []
    canceled = False
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip("\n"))
        on_line(line.rstrip("\n"))
        if cancel() and not canceled:
            canceled = True
            kill_tree(proc.pid)
            break
    try:
        code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        code = -1
    if canceled:
        code = -2
    _ = started
    return code, "\n".join(lines)


def download(
    *,
    url: str,
    home: str | Path,
    quality: str = DEFAULT_QUALITY,
    yt_dlp_bin: str | None = None,
    cookie_file: str | Path | None = None,
    ffmpeg_dir: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel: Callable[[], bool] = lambda: False,
    need_bytes: int = 0,
    runner: Runner | None = None,
    backend: str = "ytdlp",
    bbdown_bin: str | None = None,
    bbdown_args: str = "",
) -> DownloadResult:
    """下载一集到 ``home/source.<ext>``——这是**暂存名**，不是最终文件名。

    调用方（``workers/download_worker``）拿到返回路径后交给
    ``layout.finalize_source`` 改成视频标题名。这里固定用暂存名是刻意的：
    断点续传的 ``.part`` 文件名必须稳定，而 yt-dlp 自己的清洗规则和
    ``sanitize_name`` 不一致，让它按标题写会留下两个名字对不上的现场。

    ``backend`` 两种：``ytdlp``（默认）与 ``bbdown``（兜底，P1-08）。兜底**只在调用方
    明确指定时**才走——不在这里做"失败了自动换一个"的静默切换。

    取消时：杀进程树 → 删 ``.part`` → 抛 ``CANCELED``。**不会**删已经下完的文件。
    """
    home_path = Path(home)
    home_path.mkdir(parents=True, exist_ok=True)

    chosen = str(backend or "ytdlp").strip().lower()
    if chosen not in BACKENDS:
        raise WBError("VALIDATION", "下载后端只能是 ytdlp 或 bbdown", chosen)

    ensure_disk(home_path, need_bytes)

    if chosen == "bbdown":
        # 兜底后端：只在用户明确要求时走这里，绝不自动切换（P1-08 的验收口径）。
        bbdown = _resolve_bbdown(bbdown_bin)
        argv = build_bbdown_command(
            binary=bbdown,
            url=url,
            home=home_path,
            quality=quality,
            cookie_file=cookie_file,
            extra_args=bbdown_args,
        )
    else:
        prefix: list[str] = []
        binary = yt_dlp_bin or shutil.which("yt-dlp") or shutil.which("yt_dlp")
        if not binary:
            # 用模块方式跑，省得用户还要单独装 CLI
            if getattr(sys, "frozen", False):
                # 打包态没有 python.exe：`sys.executable -m yt_dlp` 会把主程序
                # 自己再拉起来一次（窗口一闪、yt-dlp 压根没跑），改走 `--ytdlp` 入口。
                prefix = [sys.executable, "--ytdlp"]
                binary = prefix[0]
            elif _module_available("yt_dlp"):
                binary = f"{sys.executable} -m yt_dlp"
            else:
                raise WBError(
                    "DEPENDENCY_MISSING",
                    "缺少 yt-dlp：装一下再来（pip install yt-dlp，或把 yt-dlp.exe 放到工具目录）",
                )
        cmd = build_command(
            yt_dlp_bin=binary,
            url=url,
            home=home_path,
            quality=quality,
            cookie_file=cookie_file,
            ffmpeg_dir=ffmpeg_dir or ffmpeg_location(),
        )
        if prefix:
            # 打包态：[exe, "--ytdlp"] + yt-dlp 自己的参数（不能按空格拆 sys.executable，
            # 路径里一旦带空格就会拆坏）
            argv = list(prefix) + cmd[1:]
        else:
            # "python -m yt_dlp" 这种带空格的二进制要拆开，否则 Popen 会去找名字里有空格的 exe
            argv = cmd[0].split() + cmd[1:] if " " in cmd[0] else cmd

    emit = on_event or (lambda _e: None)
    started = time.time()
    events = 0
    finished: dict[str, Any] | None = None

    def handle(line: str) -> None:
        nonlocal events, finished
        if chosen == "bbdown":
            progress = parse_bbdown_line(line)
            if progress is None:
                return
            events += 1
            emit(progress)
            return
        event = parse_line(line)
        if event is None:
            return
        events += 1
        if event.get("type") == "finished":
            finished = event
            return
        if event.get("type") == "progress":
            emit(event)

    run = runner or _default_runner
    code, output = run(argv, str(home_path), handle, cancel)

    if code == -2 or (cancel() and code != 0):
        cleanup_partials(home_path)
        raise WBError("CANCELED", "下载已取消", str(home_path))

    if code != 0:
        raise classify_failure(output)

    if finished is None:
        # 注入的 runner / 日志重放只给整段文本，那就从文本里捞最后一条元信息
        for raw in reversed((output or "").splitlines()):
            event = parse_line(raw)
            if event and event.get("type") == "finished":
                finished = event
                break

    produced = Path(finished["path"]) if finished and finished.get("path") else None
    if chosen == "bbdown":
        if produced is None or not produced.exists():
            produced = _bbdown_output(home_path)
        produced = _normalize_bbdown_output(home_path, produced)
    if produced is None or not produced.exists():
        # yt-dlp 有时不给 print（老版本），退化成"目录里最大的 mp4"
        candidates = sorted(home_path.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
        produced = candidates[0] if candidates else None
    if produced is None or not produced.exists():
        raise WBError("PROCESS_FAILED", "下载结束但没找到产物文件", output[-2000:])

    emit({"type": "progress", "percent": 100.0, "status": "finished"})
    return DownloadResult(
        path=produced,
        height=int(finished.get("height") or 0) if finished else 0,
        format_id=str(finished.get("format_id") or "") if finished else "",
        quality_requested=quality,
        elapsed_s=round(time.time() - started, 2),
        events=events,
        stderr_tail=output[-4000:],
    )


def _module_available(name: str) -> bool:
    from importlib.util import find_spec

    return find_spec(name) is not None


def iter_events(result: DownloadResult) -> Iterable[dict[str, Any]]:
    """给 job 层用的小工具：把结果摊成事件序列（便于统一落日志）。"""
    yield {"type": "progress", "percent": 100.0}
    yield {
        "type": "done",
        "path": str(result.path),
        "quality_actual": result.quality_actual,
        "quality_requested": result.quality_requested,
        "format_id": result.format_id,
        "elapsed_s": result.elapsed_s,
    }
