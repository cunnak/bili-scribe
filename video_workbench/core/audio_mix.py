"""切片音轨：BGM 混音、人声闪避、淡入淡出（P6-03）。

三件事都在这里拼 FFmpeg 滤镜串，不放 `exporter.py`——导出那个文件已经有一百多行
画幅/字幕分支，再塞音频会变成两百行 if。这里只做纯函数：给定参数返回滤镜链与
输出标签，不碰进程、不碰文件，测试可以直接断言字符串。

口径（对应任务清单 P6-03）：

- **闪避**：用 `sidechaincompress` 把原声当侧链，人声一出来就把 BGM 压下去。
  不用"从字幕算语音区间再打关键帧"——字幕有断句和延迟，真人说话的起音比字幕早，
  用侧链是音频层面的事实判断，也不需要先跑转录。
- **淡入淡出**：音频 `afade` + 画面 `fade` 同时做，长度同一个参数。两端各一次，
  所以参数上限受片长约束（见 `clamp_fade_ms`），0 就是不淡。
"""

from __future__ import annotations

from typing import Any

#: 淡入/淡出单侧上限：再长就不是"防爆音"而是转场了
FADE_MAX_MS = 3000
#: 两侧加起来最多占片长的这个比例，避免 3 秒的切片被淡掉 2 秒
FADE_SPAN_RATIO = 0.3
#: BGM 默认音量（相对原声，1.0 = 不衰减）
BGM_VOLUME = 0.25
#: 侧链参数：阈值 -30dB 左右起压、比例 8:1、20ms 起音、300ms 释放
DUCK_THRESHOLD = 0.03
DUCK_RATIO = 8
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 300


def clamp_fade_ms(span_ms: int, fade_ms: Any) -> int:
    """把用户填的淡入淡出长度夹到合理范围。

    `span_ms` 是这一段成片的时长；两侧各淡一次，所以单侧不超过片长的 30%，
    也不超过 `FADE_MAX_MS`。负数或非数字一律当 0（不淡）。
    """
    try:
        value = int(fade_ms or 0)
    except (TypeError, ValueError):
        return 0
    span = max(0, int(span_ms or 0))
    if value <= 0 or span <= 0:
        return 0
    return max(0, min(value, FADE_MAX_MS, int(span * FADE_SPAN_RATIO)))


def clamp_volume(value: Any, default: float = BGM_VOLUME) -> float:
    """BGM 音量夹到 0–1。前端滑杆给的是 0–100，换成 0–1 再传。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number < 0:  # -1 是调用方「按配置来」的哨兵，别夹成 0（那就静音了）
        return float(default)
    if number > 1:  # 传了 0–100 的百分数
        number = number / 100.0
    return max(0.0, min(1.0, number))


def fade_filters(span_ms: int, fade_ms: Any, *, audio: bool) -> list[str]:
    """淡入淡出滤镜串。`audio=True` 给 `afade`，否则给画面 `fade`。"""
    fade = clamp_fade_ms(span_ms, fade_ms)
    if fade <= 0:
        return []
    duration = max(0.001, int(span_ms) / 1000.0)
    length = fade / 1000.0
    start = max(0.0, duration - length)
    name = "afade" if audio else "fade"
    return [f"{name}=t=in:st=0:d={length:.3f}", f"{name}=t=out:st={start:.3f}:d={length:.3f}"]


def duck_filters() -> str:
    """人声闪避的压缩器参数串（`sidechaincompress` 的选项部分）。"""
    return (
        f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}"
        f":attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}"
    )


def build_audio_graph(
    *,
    span_ms: int,
    bgm: bool = False,
    volume: Any = BGM_VOLUME,
    duck: bool = True,
    fade_ms: Any = 0,
    bgm_index: int = 1,
    source_audio: bool = True,
) -> tuple[list[str], str]:
    """拼音频滤镜链，返回 `(链列表, 输出标签)`。

    - 没 BGM 也没淡入淡出 → `([], "")`，调用方按"音频直通"处理
    - 只有淡入淡出 → 一条链，标签 `[aout]`
    - 有 BGM → `asplit` 出侧链键 + `volume` 压 BGM + `sidechaincompress` + `amix`
    - 源片没音轨（`source_audio=False`）→ 直接用 BGM，不闪避（没有可用侧链）
    """
    chains: list[str] = []
    label = ""
    if bgm and not source_audio:
        chains.append(f"[{bgm_index}:a]volume={clamp_volume(volume):.4f}[abgm]")
        label = "[abgm]"
    elif bgm:
        chains.append(f"[{bgm_index}:a]volume={clamp_volume(volume):.4f}[abgm]")
        if duck:
            chains.append("[0:a]asplit=2[amain][akey]")
            chains.append(f"[abgm][akey]sidechaincompress={duck_filters()}[abgmduck]")
            chains.append(
                "[amain][abgmduck]amix=inputs=2:duration=first:dropout_transition=0"
                ":normalize=0[amix]"
            )
        else:
            chains.append(
                "[0:a][abgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amix]"
            )
        label = "[amix]"

    # 源片没有音轨、又没混 BGM 时，[0:a] 这个流根本不存在，afade 会把 ffmpeg
    # 打成 "matches no streams"；此时只做画面淡入淡出。
    if not label and not source_audio:
        return chains, label
    fades = fade_filters(span_ms, fade_ms, audio=True)
    if not fades:
        return chains, label
    source = label or "[0:a]"
    chains.append(f"{source}{','.join(fades)}[aout]")
    return chains, "[aout]"


def describe(
    *,
    bgm_name: str = "",
    bgm: bool = False,
    volume: Any = BGM_VOLUME,
    duck: bool = True,
    fade_ms: Any = 0,
    span_ms: int = 0,
) -> str:
    """给人看的一句说明，写进导出结果与界面，别让人猜"到底混没混进去"。"""
    bits: list[str] = []
    if bgm and bgm_name:
        percent = round(clamp_volume(volume) * 100)
        bits.append(f"BGM：{bgm_name}（{percent}%{'，人声出现时自动压低' if duck else ''}）")
    elif bgm:
        bits.append("BGM 已混入")
    fade = clamp_fade_ms(span_ms, fade_ms)
    if fade:
        bits.append(f"淡入淡出 {fade} ms")
    return "；".join(bits)
