"""P6-02 多主体 / 镜头切换 / 关闭增强（`video_workbench/core/reframe.py`）。

Haar 级联没法凭空造出"两个人同框"的画面，所以聚类、切点、打分这些逻辑全部通过
注入 `sampler` 来测：采样器返回什么，检测层就得照着做什么。真实路径（真抽帧）由
`scripts/accept_p6_focus.py` 在真视频上验。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.core import reframe

WIDTH = 1920
HEIGHT = 1080
FACE = 200


def face_at(center_x: float, center_y: float = 300.0, size: int = FACE) -> list[float]:
    return [center_x - size / 2, center_y - size / 2, size, size]


def samples_two_people(*, a_frames: int = 10, b_frames: int = 6, cut_at: int = -1, total: int = 10):
    rows = []
    for index in range(total):
        t_ms = index * reframe.DETECT_INTERVAL_MS
        faces = [face_at(420.0)]
        if index < b_frames:
            faces.append(face_at(1500.0))
        cut = 0.62 if t_ms == cut_at else 0.02
        rows.append({"t_ms": t_ms, "faces": faces, "cut": cut})
    return rows


def sampler_of(samples, *, width=WIDTH, height=HEIGHT, detector="haar", thumbs=()):
    def _sampler(source, *, start_ms, span_ms, interval_ms, models_root):  # noqa: ARG001
        return {
            "samples": samples,
            "detector": detector,
            "notes": [],
            "frames": len(samples),
            "width": width,
            "height": height,
            "thumbs": list(thumbs),
        }

    return _sampler


# ---------------------------------------------------------------- 聚类


def test_cluster_keeps_two_people_apart():
    subjects = reframe.cluster_faces(samples_two_people(), width=WIDTH, height=HEIGHT)
    assert len(subjects) == 2
    left, right = sorted(subjects, key=lambda item: item["avg_x"])
    assert left["frames"] == 10
    assert right["frames"] == 6
    assert left["avg_x"] < 0.2 < 0.8 < right["avg_x"]


def test_cluster_does_not_merge_two_faces_of_one_frame():
    """同一帧里的两张脸一定是两个人：允许它们落进同一个簇就会把两个人合成一个。"""
    rows = [{"t_ms": 0, "faces": [face_at(500.0), face_at(900.0)], "cut": 0.0}]
    subjects = reframe.cluster_faces(rows, width=WIDTH, height=HEIGHT)
    assert len(subjects) == 2


def test_cluster_carries_track_over_gap():
    """中间几帧只有一个人：位置能接上，不该因为断了几帧就新开一个主体。"""
    rows = [
        {"t_ms": 0, "faces": [face_at(600.0)]},
        {"t_ms": 500, "faces": [face_at(610.0)]},
        {"t_ms": 1000, "faces": []},
        {"t_ms": 1500, "faces": [face_at(620.0)]},
    ]
    subjects = reframe.cluster_faces(rows, width=WIDTH, height=HEIGHT)
    assert len(subjects) == 1
    assert subjects[0]["frames"] == 3


def test_labels_use_positions_for_two_people():
    subjects = reframe.cluster_faces(samples_two_people(), width=WIDTH, height=HEIGHT)
    labels = sorted(item["label"] for item in subjects)
    assert labels == sorted([reframe.POSITION_LABELS[0], reframe.POSITION_LABELS[1]])


def test_max_subjects_caps_output():
    rows = [{"t_ms": 0, "faces": [face_at(400.0), face_at(900.0), face_at(1400.0), face_at(1750.0)]}]
    subjects = reframe.cluster_faces(rows, width=WIDTH, height=HEIGHT, max_subjects=2)
    assert len(subjects) == 2


# ---------------------------------------------------------------- 打分


def test_longer_presence_scores_higher():
    subjects = reframe.cluster_faces(samples_two_people(), width=WIDTH, height=HEIGHT)
    ranked = reframe.rank_subjects(subjects, span_ms=5000, width=WIDTH)
    assert ranked[0]["frames"] == 10
    assert ranked[0]["score"] > ranked[1]["score"]
    assert reframe.pick_subject(ranked) == ranked[0]["id"]


def test_score_is_bounded():
    rows = [{"t_ms": index * 500, "faces": [face_at(960.0, size=900)]} for index in range(20)]
    subjects = reframe.cluster_faces(rows, width=WIDTH, height=HEIGHT)
    ranked = reframe.rank_subjects(subjects, span_ms=10_000, width=WIDTH)
    assert 0 <= ranked[0]["score"] <= 100


# ---------------------------------------------------------------- 镜头切换


def test_cut_times_picks_only_big_changes():
    rows = samples_two_people(cut_at=2000)
    assert reframe.cut_times(rows) == [2000]
    assert reframe.cut_times(rows, threshold=0.9) == []


def test_smooth_track_does_not_limit_across_cut():
    """跨切点不限速：否则 0.2→0.9 的换位会被拉成几秒的横移。"""
    points = [
        {"t_ms": 0, "x": 0.2},
        {"t_ms": 500, "x": 0.2},
        {"t_ms": 1000, "x": 0.2},
        {"t_ms": 1500, "x": 0.2},
        {"t_ms": 2000, "x": 0.9},
        {"t_ms": 2500, "x": 0.9},
        {"t_ms": 3000, "x": 0.9},
    ]
    limited = reframe.smooth_track(points, frame_width=WIDTH, clip_span_ms=3000)
    at_cut = next(row for row in limited if row["t_ms"] == 2000)
    assert at_cut["x"] < 0.6, "没有切点信息时应该被限速拉住"

    jumped = reframe.smooth_track(points, frame_width=WIDTH, clip_span_ms=3000, cuts=[2000])
    at_cut = next(row for row in jumped if row["t_ms"] == 2000)
    assert at_cut["x"] == pytest.approx(0.9, abs=1e-6)


def test_jump_pairs_insert_instant_switch():
    points = [(0.0, 0.2), (4.0, 0.8)]
    pairs = reframe.jump_pairs(points, [2000])
    assert (1.98, 0.2) in pairs
    assert (2.0, 0.8) in pairs


def test_crop_expression_uses_real_interval_at_cut():
    """切点那对关键帧只隔 20ms，分母必须是 0.020，不能按 1 秒算。"""
    expr = reframe.crop_expression(
        [{"t_ms": 0, "x": 0.2}, {"t_ms": 4000, "x": 0.8}], cuts=[2000]
    )
    assert "/0.020" in expr
    assert expr.count("(iw-ih*9/16)") == 1


# ---------------------------------------------------------------- 检测层


def test_detect_subjects_from_sampler_picks_main_subject():
    got = reframe.detect_subjects(
        "a.mp4",
        start_ms=0,
        end_ms=5000,
        models_root="/nowhere",
        sampler=sampler_of(samples_two_people(cut_at=2000)),
    )
    assert len(got["subjects"]) == 2
    assert got["cuts"] == [2000]
    assert got["subject"] == got["subjects"][0]["id"]
    assert got["focus"], "选定主体要有轨迹"
    assert got["detector"] == "haar"
    assert set(got["tracks"]) == {item["id"] for item in got["subjects"]}


def test_detect_subjects_honours_requested_subject():
    rows = samples_two_people()
    subjects = reframe.rank_subjects(
        reframe.cluster_faces(rows, width=WIDTH, height=HEIGHT), span_ms=5000, width=WIDTH
    )
    wanted = subjects[1]["id"]
    got = reframe.detect_subjects(
        "a.mp4", start_ms=0, end_ms=5000, models_root="/nowhere",
        sampler=sampler_of(rows), subject=wanted,
    )
    assert got["subject"] == wanted


def test_detect_subjects_reports_unavailable():
    got = reframe.detect_subjects(
        "a.mp4", start_ms=0, end_ms=1000, models_root="/nowhere",
        sampler=sampler_of([], detector="unavailable"),
    )
    assert got["detector"] == "unavailable"
    assert got["subjects"] == []


def test_detect_focus_enhance_off_returns_legacy_shape(monkeypatch):
    monkeypatch.setattr(reframe, "_load_cv2", lambda: None)
    legacy = reframe.detect_focus(
        "a.mp4", start_ms=0, end_ms=2000, models_root="/nowhere", enhance=False
    )
    assert legacy["detector"] == "unavailable"
    assert legacy["enhance"] is False
    assert "subjects" not in legacy


def test_detect_focus_enhance_on_adds_subjects():
    got = reframe.detect_focus(
        "a.mp4", start_ms=0, end_ms=5000, models_root="/nowhere",
        sampler=sampler_of(samples_two_people(cut_at=2000)),
    )
    assert got["enhance"] is True
    assert len(got["subjects"]) == 2
    assert got["cuts"] == [2000]


def test_rebuild_focus_switches_subject():
    rows = samples_two_people()
    subjects = reframe.rank_subjects(
        reframe.cluster_faces(rows, width=WIDTH, height=HEIGHT), span_ms=5000, width=WIDTH
    )
    first = reframe.rebuild_focus(subjects, subject_id=subjects[0]["id"], span_ms=5000, frame_width=WIDTH)
    second = reframe.rebuild_focus(subjects, subject_id=subjects[1]["id"], span_ms=5000, frame_width=WIDTH)
    assert first and second
    assert first[0]["x"] != second[0]["x"]


# ---------------------------------------------------------------- vision 选主体


class FakeClient:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def chat(self, messages, *, temperature=0.3, max_tokens=1000, model="", json_mode=False):  # noqa: ARG002
        self.calls.append({"messages": messages, "model": model})
        return self.text


def shot_payload():
    return [{"jpeg": b"\xff\xd8not-a-real-jpeg", "faces": [face_at(420.0), face_at(1500.0)]}]


def test_parse_subject_reply_accepts_id_and_number():
    subjects = [{"id": "sub_a"}, {"id": "sub_b"}]
    assert reframe.parse_subject_reply('{"subject": "sub_b", "reason": "在说话"}', subjects) == {
        "subject": "sub_b",
        "reason": "在说话",
    }
    assert reframe.parse_subject_reply('```json\n{"subject": 2}\n```', subjects)["subject"] == "sub_b"


def test_parse_subject_reply_rejects_garbage():
    subjects = [{"id": "sub_a"}]
    assert reframe.parse_subject_reply("我觉得左边那位更好", subjects) == {}
    assert reframe.parse_subject_reply('{"subject": 9}', subjects) == {}


def test_vision_pick_subject_maps_number_to_id():
    subjects = reframe.label_subjects(
        [{"id": "sub_a", "avg_x": 0.1, "track": [], "frames": 1, "area": 0.01},
         {"id": "sub_b", "avg_x": 0.9, "track": [], "frames": 1, "area": 0.01}]
    )
    client = FakeClient('{"subject": 2, "reason": "右边那位在说话"}')
    got = reframe.vision_pick_subject(
        client, shots=shot_payload(), subjects=subjects, model="vl", prompt="挑一个"
    )
    assert got["subject"] == "sub_b"
    assert got["model"] == "vl"
    assert client.calls and client.calls[0]["model"] == "vl"
    content = client.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[-1]["type"] == "image_url"
    assert content[-1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_vision_pick_subject_skips_single_subject():
    subjects = [{"id": "sub_a", "label": "主体 A"}]
    client = FakeClient('{"subject": 1}')
    assert reframe.vision_pick_subject(client, shots=shot_payload(), subjects=subjects) == {}
    assert client.calls == []


def test_vision_pick_subject_survives_broken_image():
    """图坏了也要能发出去（只是没框），不能让一次画图失败打断精析。"""
    subjects = reframe.label_subjects(
        [{"id": "sub_a", "avg_x": 0.1}, {"id": "sub_b", "avg_x": 0.9}]
    )
    client = FakeClient('{"subject": "sub_a"}')
    got = reframe.vision_pick_subject(client, shots=shot_payload(), subjects=subjects)
    assert got["subject"] == "sub_a"


def test_load_subject_prompt_has_json_contract():
    prompt = reframe.load_subject_prompt()
    assert "JSON" in prompt and "subject" in prompt


# ---------------------------------------------------------------- 状态文件


def test_focus_meta_keeps_cuts(tmp_path: Path):
    path = tmp_path / "focus.json"
    reframe.save_focus(
        path,
        [{"t_ms": 0, "x": 0.3}, {"t_ms": 2000, "x": 0.7}],
        meta={"cuts": [2000], "subject": "sub_b", "enhance": True},
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["cuts"] == [2000]
    assert raw["subject"] == "sub_b"
    assert reframe.load_focus(path) == [
        {"t_ms": 0, "x": 0.3, "manual": False},
        {"t_ms": 2000, "x": 0.7, "manual": False},
    ]


# ---------------------------------------------------------------- 模型路径镜像
#
# OpenCV 5 的 dnn 在 Windows 上按字节开文件，模型路径带中文会读不出来
# （工作目录叫「视频内容总结」时必然踩到），所以 ascii_model_path 要把模型
# 镜像到纯 ASCII 目录再喂给 OpenCV。


def test_ascii_model_path_mirrors_non_ascii(tmp_path: Path):
    nested = tmp_path / "视频内容总结" / "models"
    nested.mkdir(parents=True)
    model = nested / "yunet.onnx"
    model.write_bytes(b"onnx-bytes")
    mirror = reframe.ascii_model_path(model)
    assert str(mirror).isascii()
    assert mirror != model
    assert mirror.read_bytes() == b"onnx-bytes"
    # 再调一次不重复拷贝，直接复用
    assert reframe.ascii_model_path(model) == mirror


def test_ascii_model_path_keeps_ascii_path(tmp_path: Path):
    model = tmp_path / "models" / "yunet.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x")
    assert reframe.ascii_model_path(model) == model
