"""ffmpeg / ffprobe 的唯一入口（P1-06）。

**为什么所有调用都收口在这里。** ffmpeg 的参数顺序很敏感（``-ss`` 放 ``-i`` 前面是
快速定位，放后面是逐帧解码），命令行拼错的表现往往不是报错，而是"导出的片子前
三秒是黑的"。把参数拼装集中在一个文件里，错一次就只错一处。

**为什么优先用配置目录里的二进制。** 用户机器上 ``PATH`` 里的 ffmpeg 可能是别的
软件（OBS、剪映）自带的老版本，行为不一致；``ffmpeg_dir`` 里那份是我们认的版本。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..app.errors import WBError

_WIN = os.name == "nt"


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 的一小撮字段。只要这些——其余字段用不上还占内存。"""

    duration_ms: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    has_video: bool = False
    has_audio: bool = False
    bytes: int = 0
    # 合并预检要判「能不能直接 copy」：像素格式与音频采样率/声道数不同就得转码
    pix_fmt: str = ""
    sample_rate: int = 0
    channels: int = 0

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0


def find_binary(name: str, prefer_dir: str | None = None) -> str | None:
    """先找配置目录，再找 PATH。找不到返回 None（不要抛——调用方要能优雅降级）。"""
    stem = f"{name}.exe" if _WIN else name
    if prefer_dir:
        candidate = Path(prefer_dir) / stem
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def _ratio(text: Any) -> float:
    """ffprobe 的帧率是 ``"30000/1001"`` 这种分数，不是小数。"""
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_probe(payload: dict[str, Any], size: int = 0) -> MediaInfo:
    """把 ffprobe 的 JSON 摊平。缺字段不抛，全部按 0/"" 处理。"""
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format") or {}
    duration = fmt.get("duration") or (video or {}).get("duration") or 0
    try:
        duration_ms = int(round(float(duration) * 1000))
    except (TypeError, ValueError):
        duration_ms = 0
    return MediaInfo(
        duration_ms=duration_ms,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=_ratio((video or {}).get("r_frame_rate") or (video or {}).get("avg_frame_rate")),
        vcodec=str((video or {}).get("codec_name") or ""),
        acodec=str((audio or {}).get("codec_name") or ""),
        has_video=video is not None,
        has_audio=audio is not None,
        bytes=size,
        pix_fmt=str((video or {}).get("pix_fmt") or ""),
        sample_rate=int((audio or {}).get("sample_rate") or 0),
        channels=int((audio or {}).get("channels") or 0),
    )


class FFmpeg:
    """命令行封装。每个方法都只做一件事，方便单独替换/跳过。"""

    def __init__(self, prefer_dir: str | None = None, timeout: float = 600.0) -> None:
        self.prefer_dir = prefer_dir
        self.timeout = timeout

    # ------------------------------------------------------------ 可用性

    @property
    def ffmpeg(self) -> str | None:
        return find_binary("ffmpeg", self.prefer_dir)

    @property
    def ffprobe(self) -> str | None:
        return find_binary("ffprobe", self.prefer_dir)

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def require(self) -> tuple[str, str]:
        if not self.available:
            raise WBError("DEPENDENCY_MISSING", "缺少 ffmpeg / ffprobe，请放到工具目录或加进 PATH")
        return self.ffmpeg or "", self.ffprobe or ""

    # ------------------------------------------------------------ 执行

    def run(
        self,
        args: Sequence[str],
        timeout: float | None = None,
        *,
        should_cancel: Any = None,
        on_proc: Any = None,
    ) -> str:
        """跑一条命令，返回 stdout。非 0 一律抛 PROCESS_FAILED 并带上末尾输出。

        **为什么要自己 Popen**：导出、mux、烧字幕、快速拼接都是几十秒到几分钟的命令。
        用 `subprocess.run` 的话，"取消"只能等它跑完才生效——用户点了取消，进度条
        还在动，只能去任务管理器杀。这里每 0.2 秒看一次：取消就杀掉整棵进程树并抛
        CANCELED，超时就杀掉并抛 PROCESS_FAILED。

        `on_proc` 拿到刚起的 Popen：`JobManager` 用它登记，取消时兜底杀——
        有些 handler 会卡在 `readline` 上，光靠检查点是退不出来的。
        """
        from video_workbench.core.downloader import kill_tree

        limit = float(timeout if timeout is not None else self.timeout)
        proc = subprocess.Popen(  # noqa: S603 —— 参数为列表，不经 shell
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if on_proc is not None:
            try:
                on_proc(proc)
            except Exception:  # noqa: BLE001 - 登记失败不该让命令跑不起来
                pass
        deadline = time.monotonic() + limit if limit > 0 else None
        stdout = ""
        stderr = ""
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                canceled = False
                if should_cancel is not None:
                    try:
                        canceled = bool(should_cancel())
                    except Exception:  # noqa: BLE001 - 判取消出岔子就当没取消
                        canceled = False
                if canceled:
                    kill_tree(proc.pid)
                    self._reap(proc)
                    raise WBError("CANCELED", "任务已取消")
                if deadline is not None and time.monotonic() > deadline:
                    kill_tree(proc.pid)
                    self._reap(proc)
                    raise WBError(
                        "PROCESS_FAILED",
                        f"{Path(args[0]).name} 超时（{int(limit)} 秒）",
                        retryable=True,
                    )
        if proc.returncode != 0:
            tail = (stderr or stdout or "").strip().splitlines()[-15:]
            raise WBError(
                "PROCESS_FAILED",
                f"{Path(args[0]).name} 执行失败（exit {proc.returncode}）",
                "\n".join(tail),
                retryable=True,
            )
        return stdout

    def probe(self, path: str | Path) -> MediaInfo:
        _, ffprobe = self.require()
        target = Path(path)
        if not target.is_file():
            raise WBError("NOT_FOUND", "文件不存在", str(target))
        out = self.run(
            [
                ffprobe,
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(target),
            ]
        )
        try:
            payload = json.loads(out or "{}")
        except ValueError as exc:
            raise WBError("PROCESS_FAILED", "ffprobe 输出不是 JSON", out[-800:]) from exc
        return parse_probe(payload, size=target.stat().st_size)

    # ------------------------------------------------------------ 抽帧

    def thumbnail(
        self,
        src: str | Path,
        dest: str | Path,
        *,
        at: float = 1.0,
        width: int = 480,
        quality: int = 4,
    ) -> Path:
        """抽一张缩略图。

        ``-ss`` 放在 ``-i`` 之前是"快速定位"（只解到关键帧附近），比放后面快一个数量级。
        代价是可能不准，但缩略图不需要准。
        """
        ffmpeg, _ = self.require()
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                ffmpeg,
                "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{max(0.0, float(at)):.3f}",
                "-i", str(src),
                "-frames:v", "1",
                "-vf", f"scale={int(width)}:-2",
                "-q:v", str(int(quality)),
                str(out),
            ],
            timeout=120,
        )
        if not out.is_file() or out.stat().st_size == 0:
            raise WBError("PROCESS_FAILED", "抽帧没有产生文件", str(out))
        return out

    def extract_audio(
        self,
        src: str | Path,
        dest: str | Path,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> Path:
        """转录前的音频规整：16k 单声道 wav（faster-whisper 的标准输入）。"""
        ffmpeg, _ = self.require()
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                ffmpeg,
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-vn", "-ac", str(int(channels)), "-ar", str(int(sample_rate)),
                "-c:a", "pcm_s16le",
                str(out),
            ]
        )
        return out

    # ------------------------------------------------------------ 切片导出

    def export_clip(
        self,
        src: str | Path,
        dest: str | Path,
        *,
        start_ms: int,
        end_ms: int,
        mode: str = "precise",
        subtitle: str | Path = "",
        subtitle_mode: str = "none",
        crf: int = 18,
        preset: str = "medium",
        aspect: str = "16x9",
        vertical_mode: str = "center",
        crop_x: float = 0.5,
        focus: list[dict] | None = None,
        duration_ms: int = 0,
        src_crop: dict[str, int] | None = None,
        focus_cuts: list[int] | None = None,
        bgm: str | Path = "",
        bgm_volume: float = 0.25,
        bgm_duck: bool = True,
        fade_ms: int = 0,
        should_cancel: Any = None,
        on_proc: Any = None,
    ) -> Path:
        """按切点导出一段（P3-05）。

        精确模式重编码（切点准，慢）；快速模式 `-c copy`（快，切点吸到关键帧）。
        命令拼装放在 `core/exporter.build_args`，这里只负责跑与建目录。
        竖屏参数（aspect/vertical_mode/crop_x/focus）透传下去，本层不解释。

        P6-03 的 BGM/淡入淡出要先知道源片有没有音轨：`afade`/`asplit` 指向
        `[0:a]` 时，无音轨素材会直接报 "matches no streams"，所以只有在真要动
        音频时才多花一次 ffprobe。
        """
        from ..core.exporter import build_args

        ffmpeg, _ = self.require()
        output = Path(dest)
        source_audio = True
        if bgm or fade_ms:
            try:
                source_audio = bool(self.probe(src).has_audio)
            except WBError:
                source_audio = False
        output.parent.mkdir(parents=True, exist_ok=True)
        args = build_args(
            ffmpeg,
            src=str(src),
            dest=str(output),
            start_ms=start_ms,
            end_ms=end_ms,
            mode=mode,
            subtitle=str(subtitle),
            subtitle_mode=subtitle_mode,
            crf=crf,
            preset=preset,
            aspect=aspect,
            vertical_mode=vertical_mode,
            crop_x=crop_x,
            focus=focus,
            duration_ms=duration_ms,
            src_crop=src_crop,
            focus_cuts=focus_cuts,
            bgm=str(bgm or ""),
            bgm_volume=bgm_volume,
            bgm_duck=bgm_duck,
            fade_ms=fade_ms,
            source_audio=source_audio,
        )
        self.run(args, should_cancel=should_cancel, on_proc=on_proc)
        if not output.is_file() or output.stat().st_size == 0:
            raise WBError("PROCESS_FAILED", "导出没有产生文件", str(output))
        return output

    # ---------------------------------------------------- 字幕 mux / 烧录

    def streams(self, path: str | Path) -> list[dict[str, Any]]:
        """读流清单。判断「软字幕有没有真的进去」只能看这里，命令退出码看不出这事。"""
        _, ffprobe = self.require()
        target = Path(path)
        if not target.is_file():
            raise WBError("NOT_FOUND", "文件不存在", str(target))
        out = self.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", str(target)]
        )
        try:
            payload = json.loads(out or "{}")
        except ValueError as exc:
            raise WBError("PROCESS_FAILED", "ffprobe 输出不是 JSON", out[-800:]) from exc
        return list(payload.get("streams") or [])

    def subtitle_streams(self, path: str | Path) -> list[dict[str, Any]]:
        return [s for s in self.streams(path) if s.get("codec_type") == "subtitle"]

    def merge_copy(
        self,
        list_path: str | Path,
        dest: str | Path,
        *,
        should_cancel: Any = None,
        on_proc: Any = None,
    ) -> Path:
        """快速拼接（`-f concat -c copy`）。规格一致才能用，判据在 `core/merger`。

        命令由 `merger.quick_command()` 拼——放在那边是为了单测能直接断言参数顺序，
        而不用起 ffmpeg。这里只负责建目录、跑、验产物。
        """
        from video_workbench.core.merger import quick_command

        ffmpeg, _ = self.require()
        listing = Path(list_path)
        if not listing.is_file():
            raise WBError("NOT_FOUND", "拼接清单不存在", str(listing))
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            quick_command(ffmpeg, listing, target),
            should_cancel=should_cancel,
            on_proc=on_proc,
        )
        if not target.is_file() or target.stat().st_size <= 0:
            raise WBError(
                "PROCESS_FAILED", "拼接没有产生文件", str(target), retryable=True
            )
        return target

    def _reap(self, proc: subprocess.Popen, grace: float = 5.0) -> None:
        """等被杀的子进程真的走掉；不走就硬杀一次。

        `proc.wait()` 在没有超时的时候会一直等下去：taskkill 失败（pid 传错、进程已
        被别的东西接管）时，界面上的"取消"就成了摆设——点完之后进度条还在动，用户只
        能去任务管理器里杀。所以取消路径不走 `wait()`，先轮询 `poll()`，超时就 kill。
        """
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except OSError:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass

    def merge_precise(
        self,
        *,
        inputs: Sequence[str | Path],
        dest: str | Path,
        plan: dict[str, Any],
        segments_ms: Sequence[int] = (),
        expected_ms: int = 0,
        crf: int = 18,
        preset: str = "medium",
        on_progress: Any = None,
        should_cancel: Any = None,
        timeout: float | None = None,
    ) -> Path:
        """精确合并（`filter_complex concat`），带进度与取消。

        **为什么不用 `self.run()`。** 那是一次性 `subprocess.run`：要么等十分钟没
        进度，要么超时被杀拿不到中间状态。精确合并必须能报进度、能被取消，所以这里
        自己 `Popen`，stdout 读 `-progress pipe:1` 的流，stderr 交给一个线程收尾
        （只保留最后若干行，成功时用不上、失败时全在尾巴上）。

        取消时要**杀进程树**：ffmpeg 在 Windows 上会派生编码子进程，只 kill 父进程
        会留一个还在写文件的孤儿，输出目录里就会出现"半成品 + 文件被占用"。
        """
        from video_workbench.core import merger as merger_core
        from video_workbench.core.downloader import kill_tree

        ffmpeg, _ = self.require()
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        args = merger_core.precise_command(
            ffmpeg, inputs=inputs, dest=target, plan=plan, crf=crf, preset=preset
        )

        proc = subprocess.Popen(  # noqa: S603 —— 参数为列表，不经 shell
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        tail: list[str] = []

        def drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                tail.append(line.rstrip("\n"))
                del tail[:-40]

        reader = threading.Thread(target=drain, name="wb-merge-stderr", daemon=True)
        reader.start()

        canceled = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                fields = merger_core.parse_progress_line(line)
                stamp = fields.get("out_time_us") or fields.get("out_time_ms")
                if stamp is not None and on_progress:
                    try:
                        micros = int(stamp)
                    except ValueError:
                        micros = 0
                    on_progress(
                        merger_core.merge_progress(
                            micros, segments_ms, expected_ms=expected_ms
                        )
                    )
                if should_cancel and should_cancel():
                    canceled = True
                    # kill_tree 收的是 **pid**，不是 Popen 对象。传对象时 taskkill 会拿到
                    # "<subprocess.Popen object at 0x...>" 然后静默失败（check=False），
                    # 后面的 wait 就会一直等下去——取消点了没反应，正是这么来的。
                    kill_tree(proc.pid)
                    break
            if canceled:
                self._reap(proc)
            else:
                proc.wait(timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            self._reap(proc)
            raise WBError(
                "PROCESS_FAILED", "合并超时，已中止", f"{Path(ffmpeg).name} 超过 {timeout or self.timeout}s",
                retryable=True,
            ) from None
        finally:
            reader.join(timeout=2)

        if canceled:
            # 取消时不留半成品：用户点"取消"就是不想再看到这个文件
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise WBError("CANCELED", "已取消合并")

        if proc.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            detail = "\n".join(tail[-15:]) or f"exit {proc.returncode}"
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise WBError(
                "PROCESS_FAILED", "精确合并失败", detail, retryable=True
            )
        return target

    def mux_subtitle(
        self,
        src: str | Path,
        subtitle: str | Path,
        dest: str | Path,
        *,
        language: str = "chi",
        title: str = "",
        should_cancel: Any = None,
        on_proc: Any = None,
    ) -> Path:
        """软字幕：视频/音频一律 ``-c copy``，只把字幕编成 ``mov_text``。

        MP4 里能放的字幕编码只有 ``mov_text``，样式（字体/字号/描边）**全部丢失**——
        这是容器限制，不是偷懒；界面必须说清楚，别让人以为软字幕能保住样式。
        """
        ffmpeg, _ = self.require()
        output = Path(dest)
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-i", str(subtitle),
            "-map", "0:v:0", "-map", "0:a?",
            "-map", "1:0",
            "-c", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", f"language={language}",
        ]
        if title:
            args += ["-metadata:s:s:0", f"title={title}"]
        args += [str(output)]
        self.run(args, should_cancel=should_cancel, on_proc=on_proc)
        return output

    def burn_subtitle(
        self,
        src: str | Path,
        subtitle: str | Path,
        dest: str | Path,
        *,
        crf: int = 20,
        preset: str = "veryfast",
        should_cancel: Any = None,
        on_proc: Any = None,
    ) -> Path:
        """硬字幕：``ass`` 滤镜把样式画进画面，必须重编码视频；音频照抄。"""
        ffmpeg, _ = self.require()
        output = Path(dest)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-vf", subtitle_filter(subtitle),
                "-c:v", "libx264", "-crf", str(int(crf)), "-preset", preset,
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(output),
            ],
            should_cancel=should_cancel,
            on_proc=on_proc,
        )
        return output


FILTER_UNSAFE_CHARS = "'`,;[]{}"

#: 镜像目录名：同一份字幕反复导出不要来回复制
_FILTER_MIRROR = "subs"


def mirror_for_filter(path: Path) -> Path:
    """把字幕文件复制到一个"滤镜安全"的临时路径，返回新路径。

    ffmpeg 的 filtergraph 有三层转义，文件名里一个 `'` 就能把引号提前关掉，
    报出来的是 `Error initializing filters / No such file or directory`——
    连文件名都不告诉你。与其跟解析器较劲，不如把文件放到我们自己起的安全名字下：
    只留字母数字和 `._-`，并且带上原路径哈希，不同项目同名文件不会互相覆盖。
    """
    import hashlib
    import os
    import shutil
    import tempfile

    stem = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in path.stem)[:60] or "subtitle"
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    name = f"{stem}-{digest}{path.suffix.lower()}"
    roots = [Path(tempfile.gettempdir()), Path(os.environ.get("SystemDrive", "C:")) / "wb-temp"]
    for root in roots:
        if any(ch in str(root) for ch in FILTER_UNSAFE_CHARS):
            continue
        dest = root / "video_workbench" / _FILTER_MIRROR / name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if (not dest.is_file()
                    or dest.stat().st_size != path.stat().st_size
                    or dest.stat().st_mtime_ns < path.stat().st_mtime_ns):
                shutil.copyfile(path, dest)
        except OSError:
            continue
        return dest
    return path


def filter_path(path: str | Path) -> str:
    """把路径塞进 ``-vf`` 的 filtergraph，返回**带引号**的转义结果。

    filtergraph 里 ``:`` 是参数分隔符、``\\`` 是转义符，Windows 路径
    ``C:\\x\\y.ass`` 直接拼进去会被当作「第二个参数」——实测报的是
    ``Unable to parse "original_size" option value "/Users/.../x.ass"``，
    完全看不出问题是路径。

    实测结论（同一台机器逐一跑过）：

    - ``ass=C:/x.ass``、``ass='C:/x.ass'`` 都失败——引号挡不住冒号；
    - ``ass='C\\:/x.ass'`` 成功。

    所以：整体加单引号 + 冒号转义。反斜杠统一成正斜杠，免得被当成转义前缀。

    名字里带 `'`、`,`、`[`、`]`、`;` 的走另一条路：先镜像到临时目录再引用
    （见 `mirror_for_filter`）。这些字符在 filtergraph 里各有含义，而引号内
    不认反斜杠转义——`\\'` 会把引号提前关掉，`'\\''` 这种 shell 写法 ffmpeg
    也不接（都实测过），与其猜解析器不如把文件名洗干净。
    """
    real = Path(path).resolve()
    if any(ch in str(real) for ch in FILTER_UNSAFE_CHARS):
        real = mirror_for_filter(real)
    text = real.as_posix().replace(":", "\\:")
    return "'" + text + "'"


#: libass 只吃 ASS/SSA；其余字幕格式必须先由 ffmpeg 的 ``subtitles`` 滤镜转一遍
ASS_LIKE_SUFFIXES = (".ass", ".ssa")


def subtitle_filter(path: str | Path) -> str:
    """按扩展名选出正确的烧录滤镜。

    实测：把 ``.srt`` 直接喂给 ``ass=`` 会得到
    ``Could not create a libass track when reading file '…'``，接着是
    ``Error opening output files: Invalid argument``（退出码 -22），
    而我们日志里只剩一句「ffmpeg 执行失败」——看不出是字幕格式配错了滤镜。

    没有字幕文件（`subtitle_mode="none"` 或用户没挂字幕）时返回空串，调用方
    据此跳过滤镜，而不是拼出一个 `subtitles=''` 让 ffmpeg 去开一个不存在的文件。
    """
    if not str(path or "").strip():
        return ""
    suffix = Path(str(path)).suffix.lower()
    name = "ass" if suffix in ASS_LIKE_SUFFIXES else "subtitles"
    return f"{name}={filter_path(path)}"


def best_thumb_time(duration_ms: int, offset_s: float = 1.0) -> float:
    """挑个抽帧时间点：太靠头可能还是黑场，取 10% 处但不超过 offset 之后的区间。"""
    total = max(0.0, duration_ms / 1000.0)
    if total <= 0:
        return offset_s
    return min(max(offset_s, total * 0.1), max(0.0, total - 0.2))
