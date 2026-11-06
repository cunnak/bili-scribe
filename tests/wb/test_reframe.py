"""P3-06 竖屏：滤镜拼装、轨迹平滑、黑边裁剪、跟随检测的降级路径。

这些断言都对着方案书 §5.7.4 / §7.6 的硬要求写：三种模式都要出片、跟随可见可改、
检测失败回退居中、无黑边。滤镜字符串不靠肉眼比对整串，只断言"决定行为的那几段"，
免得调一个数字就全红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.core import reframe

# ---------------------------------------------------------------- 滤镜拼装


def test_center_chain_crops_ninth_sixteenth_then_scales():
    chain = reframe.crop_chain(0.5)
    assert "crop=ih*9/16:ih" in chain
    assert "scale=1080:1920" in chain
    # 0.5 是默认居中
    assert ":(iw-ih*9/16)*0.5000" in chain


def test_center_chain_moves_with_crop_x():
    left = reframe.crop_chain(0)
    right = reframe.crop_chain(1)
    assert ":(iw-ih*9/16)*0.0000" in left.split(",")[0]
    assert ":(iw-ih*9/16)*1.0000" in right.split(",")[0]


def test_crop_x_is_clamped_and_non_numeric_falls_back():
    assert reframe.clamp_x(5) == 1.0
    assert reframe.clamp_x(-2) == 0.0
    assert reframe.clamp_x("abc") == 0.5
    assert reframe.clamp_x(float("nan")) == 0.5


def test_blur_graph_has_background_and_foreground_branches():
    graph = reframe.blur_graph()
    assert "force_original_aspect_ratio=increase" in graph
    assert "boxblur=20:2" in graph
    assert "eq=brightness=-0.15" in graph
    assert "[bg][fg]overlay=" in graph
    assert graph.endswith("[vout]")


def test_blur_graph_puts_subtitle_after_overlay():
    graph = reframe.blur_graph(r"C:\tmp\a.ass")
    assert "ass=" in graph
    # 字幕必须在 [vblur] 之后烧，否则字幕也被模糊
    assert graph.index("overlay=") < graph.index("ass=")
    assert graph.endswith("[vout]")


def test_vertical_filters_picks_vf_or_graph():
    vf, graph = reframe.vertical_filters(vertical_mode="center")
    assert vf and not graph
    vf, graph = reframe.vertical_filters(vertical_mode="blur")
    assert graph and not vf
    vf, graph = reframe.vertical_filters(vertical_mode="follow", focus=[{"t_ms": 0, "x": 0.5}])
    assert vf.startswith("crop=") and not graph


def test_vertical_filters_rejects_unknown_mode():
    with pytest.raises(ValueError):
        reframe.vertical_filters(vertical_mode="swirl")


def test_src_crop_prefix_is_first_in_every_chain():
    """削黑边必须排在最前：先削再裁，坐标才对。"""
    rect = {"w": 928, "h": 718, "x": 176, "y": 0}
    center = reframe.crop_chain(0.5, src_crop=rect)
    follow = reframe.follow_chain([{"t_ms": 0, "x": 0.4}], duration_ms=1000, src_crop=rect)
    blur = reframe.blur_graph(src_crop=rect)
    assert center.startswith("crop=928:718:176:0,")
    assert follow.startswith("crop=928:718:176:0,")
    assert blur.startswith("[0:v]crop=928:718:176:0,")
    # 两路都要削，不然背景里还留着黑边
    assert blur.count("crop=928:718:176:0,") == 2


def test_crop_rect_accepts_dict_tuple_and_rejects_junk():
    assert reframe.crop_rect({"w": 10, "h": 20, "x": 1, "y": 2}) == {
        "w": 10,
        "h": 20,
        "x": 1,
        "y": 2,
    }
    assert reframe.crop_rect((10, 20, 1, 2)) == {"w": 10, "h": 20, "x": 1, "y": 2}
    assert reframe.crop_rect({"w": 0, "h": 20, "x": 0, "y": 0}) is None
    assert reframe.crop_rect({"w": 10, "h": 20, "x": -1, "y": 0}) is None
    assert reframe.crop_rect("nope") is None


def test_same_box_detects_full_frame():
    assert reframe.same_box((1280, 720), None) is True
    assert reframe.same_box((1280, 720), {"w": 1280, "h": 720, "x": 0, "y": 0}) is True
    assert reframe.same_box((1280, 720), {"w": 928, "h": 718, "x": 176, "y": 0}) is False


# ---------------------------------------------------------------- 黑边探测


def test_detect_crop_reads_most_common_rect(monkeypatch):
    text = "\n".join(
        [
            "[Parsed_cropdetect_0] x1:176 x2:1103 y1:0 y2:718 w:928 h:718 x:176 y:0",
            "crop=928:718:176:0",
            "crop=924:716:178:2",
            "crop=928:718:176:0",
        ]
    )

    class Proc:
        stderr = text

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Proc())
    rect = reframe.detect_crop("a.mp4", ffmpeg_bin="ffmpeg")
    assert rect == {"w": 928, "h": 718, "x": 176, "y": 0}


def test_detect_crop_returns_none_when_probe_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no ffmpeg")

    monkeypatch.setattr("subprocess.run", boom)
    assert reframe.detect_crop("a.mp4", ffmpeg_bin="ffmpeg") is None


def test_detect_crop_returns_none_without_any_crop_line(monkeypatch):
    class Proc:
        stderr = "nothing here"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Proc())
    assert reframe.detect_crop("a.mp4", ffmpeg_bin="ffmpeg") is None


# ---------------------------------------------------------------- 轨迹


def test_crop_expression_multiplies_available_width_once():
    """乘号只能出现一次：每层都乘会让量纲直接错（踩过一次）。"""
    expr = reframe.crop_expression(
        [{"t_ms": 0, "x": 0.3}, {"t_ms": 5000, "x": 0.7}], duration_ms=6000
    )
    assert expr.count("(iw-ih*9/16)") == 1
    assert expr.startswith("(iw-ih*9/16)*if(")
    assert "0.7000" in expr


def test_crop_expression_empty_falls_back_to_center():
    assert reframe.crop_expression([], duration_ms=1000) == "(iw-ih*9/16)*0.5000"


def test_crop_expression_is_simplified_for_long_clip():
    """480 秒片子按 0.5 秒采样有 960 个点，表达式必须被压短，否则命令行放不下。"""
    points = [{"t_ms": i * 500, "x": 0.5 + (i % 3) * 0.001} for i in range(960)]
    expr = reframe.crop_expression(points, duration_ms=480_000)
    assert expr.count("if(") < 120


def test_median_smooth_kills_single_frame_jitter():
    values = [0.5, 0.5, 0.9, 0.5, 0.5]
    out = reframe.median_smooth(values, window=5)
    assert out[2] < 0.9


def test_fill_gaps_keeps_last_position_then_resets():
    """连续没人脸 3 秒才回中心；中间保持上一位置，避免画面来回甩。"""
    points = [{"t_ms": 0, "x": 0.2}]
    filled = reframe.fill_gaps(points, interval_ms=500, span_ms=8000, reset_ms=3000)
    times = [p["t_ms"] for p in filled]
    assert times == sorted(times)
    early = [p for p in filled if p["t_ms"] <= 2000]
    assert all(abs(p["x"] - 0.2) < 1e-6 for p in early)
    late = [p for p in filled if p["t_ms"] >= 6000]
    assert all(abs(p["x"] - 0.5) < 1e-6 for p in late)


def test_smooth_track_limits_step_between_frames():
    raw = [{"t_ms": 0, "x": 0.1}, {"t_ms": 500, "x": 0.9}]
    out = reframe.smooth_track(raw, frame_width=1280, clip_span_ms=8000)
    assert len(out) >= 2
    first = out[0]["x"]
    second = out[1]["x"]
    assert abs(second - first) < 0.8  # 一步不能从最左跳到最右


def test_merge_manual_makes_manual_point_win():
    detected = [{"t_ms": 0, "x": 0.2}, {"t_ms": 1000, "x": 0.4}]
    manual = [{"t_ms": 1000, "x": 0.8, "manual": True}]
    merged = reframe.merge_manual(detected, manual, span_ms=2000)
    hit = [p for p in merged if p["t_ms"] == 1000]
    assert hit and abs(hit[0]["x"] - 0.8) < 1e-6


def test_save_and_load_focus_roundtrip(tmp_path: Path):
    path = tmp_path / "focus.json"
    points = [{"t_ms": 0, "x": 0.2}, {"t_ms": 1200, "x": 0.6, "manual": True}]
    reframe.save_focus(path, points, meta={"detector": "haar"})
    loaded = reframe.load_focus(path)
    assert [(p["t_ms"], p["x"]) for p in loaded] == [(p["t_ms"], p["x"]) for p in points]
    assert loaded[1]["manual"] is True
    assert loaded[0]["manual"] is False
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["detector"] == "haar"


def test_load_focus_tolerates_broken_file(tmp_path: Path):
    path = tmp_path / "focus.json"
    path.write_text("{not json", encoding="utf-8")
    assert reframe.load_focus(path) == []


# ---------------------------------------------------------------- 检测降级


def test_detect_focus_without_cv2_reports_unavailable(monkeypatch):
    """没装 cv2 / 没级联文件时必须返回 unavailable，而不是抛异常。"""
    monkeypatch.setattr(reframe, "_load_cv2", lambda: None)
    result = reframe.detect_focus("a.mp4", start_ms=0, end_ms=2000, models_root="/nowhere")
    assert result["detector"] == "unavailable"
    assert result["focus"] == []


def test_safe_area_matches_spec():
    area = reframe.safe_area()
    assert area == {"top": 120, "bottom": 260, "width": 1080, "height": 1920}
