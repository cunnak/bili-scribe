"""P6-03：BGM 混音与淡入淡出的纯函数测试。"""

from __future__ import annotations

import pytest

from video_workbench.core import audio_mix, exporter


# ---------------------------------------------------------------- 钳位


def test_clamp_fade_ms_keeps_value_when_reasonable():
    assert audio_mix.clamp_fade_ms(30_000, 200) == 200


def test_clamp_fade_ms_never_exceeds_half_second_limit_and_span_ratio():
    # 上限 3000ms，且不超过片段时长的 30%
    assert audio_mix.clamp_fade_ms(60_000, 9_999) == audio_mix.FADE_MAX_MS
    assert audio_mix.clamp_fade_ms(1_000, 500) == 300


def test_clamp_fade_ms_treats_garbage_as_zero():
    assert audio_mix.clamp_fade_ms(10_000, None) == 0
    assert audio_mix.clamp_fade_ms(10_000, -5) == 0
    assert audio_mix.clamp_fade_ms(10_000, "abc") == 0


def test_clamp_volume_accepts_percent_and_clamps():
    assert audio_mix.clamp_volume(0.4) == pytest.approx(0.4)
    assert audio_mix.clamp_volume(25) == pytest.approx(0.25)  # 百分数
    assert audio_mix.clamp_volume(9) == pytest.approx(0.09)
    assert audio_mix.clamp_volume(-1) == pytest.approx(audio_mix.BGM_VOLUME)
    assert audio_mix.clamp_volume(120) == pytest.approx(1.0)


# ---------------------------------------------------------------- 滤镜


def test_fade_filters_use_span_and_stay_inside():
    filters = audio_mix.fade_filters(6_000, 200, audio=False)
    assert filters[0] == "fade=t=in:st=0:d=0.200"
    assert filters[1] == "fade=t=out:st=5.800:d=0.200"

    audio_filters = audio_mix.fade_filters(6_000, 200, audio=True)
    assert audio_filters[0].startswith("afade=")


def test_fade_filters_disabled_when_zero():
    assert audio_mix.fade_filters(6_000, 0, audio=True) == []


def test_build_audio_graph_without_bgm_returns_nothing():
    chains, label = audio_mix.build_audio_graph(span_ms=6_000, fade_ms=0)
    assert chains == []
    assert label == ""


def test_build_audio_graph_ducks_with_sidechain():
    chains, label = audio_mix.build_audio_graph(
        span_ms=6_000, bgm=True, volume=0.25, duck=True, bgm_index=1
    )
    joined = ";".join(chains)
    assert "[1:a]volume=0.2500[abgm]" in joined
    assert "[0:a]asplit=2[amain][akey]" in joined
    assert "sidechaincompress=threshold=" in joined
    assert "amix=inputs=2" in joined
    assert label == "[amix]"


def test_build_audio_graph_without_duck_does_not_split():
    chains, label = audio_mix.build_audio_graph(
        span_ms=6_000, bgm=True, volume=0.5, duck=False, bgm_index=2
    )
    joined = ";".join(chains)
    assert "asplit" not in joined
    assert "sidechaincompress" not in joined
    assert "[2:a]volume=0.5000[abgm]" in joined
    assert label == "[amix]"


def test_build_audio_graph_without_source_audio_uses_bgm_only():
    chains, label = audio_mix.build_audio_graph(
        span_ms=6_000, bgm=True, volume=0.3, duck=True, bgm_index=1, source_audio=False
    )
    joined = ";".join(chains)
    assert "[0:a]" not in joined
    assert "amix" not in joined
    assert label == "[abgm]"


def test_build_audio_graph_appends_fade_to_final_label():
    chains, label = audio_mix.build_audio_graph(
        span_ms=6_000, bgm=True, volume=0.25, duck=True, fade_ms=200, bgm_index=1
    )
    assert label == "[aout]"
    assert any("afade=t=in" in chain for chain in chains)
    # 淡入淡出必须作用在混完的那一路，否则只会淡掉 BGM 或原声其中一个
    assert "[amix]afade=t=in:st=0:d=0.200,afade=t=out:st=5.800:d=0.200[aout]" in chains


def test_describe_says_what_happened():
    text = audio_mix.describe(
        bgm_name="bgm.mp3", bgm=True, volume=0.25, duck=True, fade_ms=200, span_ms=6_000
    )
    assert "bgm.mp3" in text
    assert "25%" in text
    assert "压低" in text
    assert "200 ms" in text

    plain = audio_mix.describe(
        bgm_name="", bgm=False, volume=0.25, duck=True, fade_ms=0, span_ms=6_000
    )
    assert "背景音乐" in plain or plain == ""


# ---------------------------------------------------------------- 导出参数


def _args(**kwargs):
    params = dict(
        src="src.mp4",
        dest="out.mp4",
        start_ms=0,
        end_ms=6_000,
        mode="precise",
    )
    params.update(kwargs)
    return exporter.build_args("ffmpeg", **params)


def test_export_args_add_bgm_input_and_audio_filter():
    args = _args(bgm="bgm.mp3", bgm_volume=0.3, bgm_duck=True)
    assert "-stream_loop" in args
    assert "bgm.mp3" in args
    joined = " ".join(args)
    assert "sidechaincompress" in joined
    assert "-map" in args


def test_export_args_without_audio_keep_legacy_shape():
    args = _args()
    joined = " ".join(args)
    assert "sidechaincompress" not in joined
    assert "afade" not in joined


def test_export_args_fade_adds_video_and_audio_fades():
    args = _args(fade_ms=200)
    joined = " ".join(args)
    assert "fade=t=in:st=0:d=0.200" in joined
    assert "afade=t=in:st=0:d=0.200" in joined


def test_export_args_fade_without_source_audio_skips_audio_fade():
    args = _args(fade_ms=200, source_audio=False)
    joined = " ".join(args)
    assert "fade=t=in:st=0:d=0.200" in joined
    assert "afade" not in joined
