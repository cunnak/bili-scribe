"""音频波形峰值（P2-03 编辑器用）。

**为什么不放前端。** 浏览器解码整段音频要先把几十兆字节塞进内存再算，一小时
的视频基本必崩；后端用 ffmpeg 解成 8kHz 单声道 s16le 边读边算，峰值只占 1200
个字节。这里返回的是"给画布看的包络"，不是精确波形——精确波形对剪辑没有意义，
肉眼只需要看到"哪里在说话"。

缓存写 ``<项目目录>/peaks.json``，用源文件的 size+mtime 做失效判定。峰值算一次
几百毫秒，但编辑器每次进页面都算一遍会很烦，所以缓存一定要有。
"""

from __future__ import annotations

import array
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: 固定 1200 个桶。编辑器画布宽度最多约 1400px，再多也没地方画。
BUCKETS = 1200
#: 8kHz 单声道：16KB/s，一小时 57MB 流式读完，不落盘。
SAMPLE_RATE = 8000
CACHE_NAME = "peaks.json"
CACHE_VERSION = 1


def _cache_path(root: Path) -> Path:
    return root / CACHE_NAME


def _stat_key(media: Path) -> list[int]:
    try:
        st = media.stat()
    except OSError:
        return [0, 0]
    return [int(st.st_size), int(st.st_mtime)]


def bucketize(samples: array.array, buckets: int = BUCKETS) -> list[int]:
    """把一串 16 位采样压成 ``buckets`` 个 0..255 的峰值。

    桶数比采样数还多时填充 0 而不是抛错——极短的片段（比如 200ms）也会走到这里，
    这种情况下波形没什么可看的，画一条平线比报错友好。
    """
    total = len(samples)
    out = [0] * max(1, buckets)
    if total == 0:
        return out
    size = max(1, total // buckets)
    for index in range(buckets):
        start = index * size
        if start >= total:
            break
        chunk = samples[start:min(total, start + size)]
        peak = 0
        for value in chunk:
            magnitude = value if value >= 0 else -value
            if magnitude > peak:
                peak = magnitude
        out[index] = min(255, peak * 255 // 32768)
    return out


def _ffmpeg_binary(prefer_dir: str | None = None) -> str | None:
    """复用 infra.ffmpeg 的查找逻辑，但只取可执行文件路径。"""
    from video_workbench.infra.ffmpeg import find_binary

    return find_binary("ffmpeg", prefer_dir)


def _spawn(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """跑一个外部进程，返回 (returncode, stdout, stderr)。

    单独抽出来是为了测试能换掉它——真解码要几十兆音频，单测里不该跑真 ffmpeg。
    """
    creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=creation)
    except OSError as exc:
        raise RuntimeError(f"找不到 ffmpeg，无法生成波形：{exc}") from exc
    try:
        raw, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("生成波形超时") from None
    return proc.returncode, raw, err


def read_peaks(media: str | Path, *, ffmpeg_bin: str | None = None,
               buckets: int = BUCKETS, timeout: float = 300.0) -> list[int]:
    """解码音频算峰值。ffmpeg 不在或解码失败时抛 ``RuntimeError``。"""
    binary = ffmpeg_bin or _ffmpeg_binary()
    if not binary:
        raise RuntimeError("找不到 ffmpeg，无法生成波形")
    args = [binary, "-v", "error", "-i", str(media), "-vn", "-ac", "1",
            "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    code, raw, err = _spawn(args, timeout)
    if code != 0:
        message = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg 解码失败：{message[-1] if message else code}")

    samples = array.array("h")
    samples.frombytes(raw[:len(raw) - (len(raw) % 2)])
    if sys.byteorder == "big":  # pragma: no cover - Windows/Linux x86 都是小端
        samples.byteswap()
    return bucketize(samples, buckets)


def get_peaks(root: str | Path, media: str | Path, *, ffmpeg_bin: str | None = None,
              buckets: int = BUCKETS, refresh: bool = False,
              duration_ms: int | None = None) -> dict[str, Any]:
    """给界面用的入口：先看缓存，没有才解码。

    返回 ``{"peaks": [...], "buckets": 1200, "duration_ms": int, "cached": bool}``。
    解码失败不抛：前端拿不到波形只是少一条参考线，不该让整个编辑器打不开。
    """
    media_path = Path(media)
    root_path = Path(root)
    cache_file = _cache_path(root_path)
    key = _stat_key(media_path)

    if not refresh and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if (cached and cached.get("version") == CACHE_VERSION
                and cached.get("buckets") == buckets
                and cached.get("stat") == key
                and isinstance(cached.get("peaks"), list)):
            return {"peaks": cached["peaks"], "buckets": buckets,
                    "duration_ms": int(cached.get("duration_ms") or 0), "cached": True}

    try:
        peaks = read_peaks(media_path, ffmpeg_bin=ffmpeg_bin, buckets=buckets)
    except (RuntimeError, OSError) as exc:
        return {"peaks": [], "buckets": buckets, "duration_ms": int(duration_ms or 0),
                "cached": False, "error": str(exc)}

    payload = {"version": CACHE_VERSION, "buckets": buckets, "stat": key,
               "duration_ms": int(duration_ms or 0), "peaks": peaks}
    try:
        root_path.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache_file)
    except OSError:  # pragma: no cover - 权限/磁盘问题
        pass
    return {"peaks": peaks, "buckets": buckets, "duration_ms": int(duration_ms or 0),
            "cached": False}
