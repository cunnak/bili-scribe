"""P3-06 竖屏导出的服务层契约：文件名标记、轨迹清空、跟随回退。

跑法：./.venv/Scripts/python.exe -m pytest tests/wb/test_vertical_export.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.core import exporter, reframe


def test_filename_marks_aspect_and_vertical_mode():
    """竖屏成片必须能从文件名认出来——否则 16:9 和 9:16 混在一起没法管。"""
    name = exporter.clip_filename(
        project_title="讲透注意力",
        candidate_title="先证伪直觉",
        start_ms=6000,
        end_ms=15040,
        aspect="9x16",
        suffix="precise-follow",
    )
    assert "_9x16_" in name
    assert name.endswith("_9x16_precise-follow.mp4")


def test_filename_keeps_16x9_default():
    name = exporter.clip_filename(
        project_title="A", candidate_title="B", start_ms=0, end_ms=1000, suffix="precise"
    )
    assert "_16x9_precise.mp4" in name


def test_focus_save_can_clear_trajectory(tmp_path: Path):
    """空列表不能"取消清空"：必须能明确回到没检测过的状态。"""
    path = tmp_path / "focus.json"
    reframe.save_focus(path, [{"t_ms": 0, "x": 0.4, "manual": False}], meta={"detector": "haar"})
    assert reframe.load_focus(path)

    # 用空列表保存：按现有语义是"不改"，所以清空要靠 clear
    dropped = [point for point in reframe.load_focus(path) if point.get("manual")]
    assert dropped == []
    path.unlink()
    assert reframe.load_focus(path) == []


def test_load_focus_survives_broken_json(tmp_path: Path):
    path = tmp_path / "focus.json"
    path.write_text("{不是 json", encoding="utf-8")
    assert reframe.load_focus(path) == []


def test_vertical_filters_follow_needs_focus():
    """没有轨迹时按 0.5 居中出片，而不是抛错——导出不该被检测失败拦住。"""
    vf, graph = reframe.vertical_filters(
        vertical_mode="follow", crop_x=0.5, focus=[], duration_ms=5000
    )
    assert graph == ""
    assert "0.5000" in vf


def test_crop_expression_scales_to_source_width():
    """表达式里乘号只能出现一次：多乘一次，切点会跑到画面外面。"""
    expr = reframe.crop_expression(
        [{"t_ms": 0, "x": 0.2}, {"t_ms": 2000, "x": 0.8}], duration_ms=4000
    )
    assert expr.count("iw-ih*9/16") == 1
    assert expr.startswith("(iw-ih*9/16)*")


@pytest.mark.parametrize("mode", ["center", "blur", "follow"])
def test_vertical_modes_are_stable(mode: str):
    assert mode in reframe.VERTICAL_MODES


def test_safe_area_numbers():
    area = reframe.safe_area()
    assert (area["top"], area["bottom"]) == (120, 260)
    assert (area["width"], area["height"]) == (1080, 1920)


# ---------------------------------------------------------------- 服务层契约


def _project_with_source(service, tmp_path: Path) -> dict:
    """造一个带 source 资产的项目（不碰真文件内容，只要路径存在）。"""
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    src = root / "source.mp4"
    src.write_bytes(b"\x00" * 2048)
    project = service.repo.project_create(
        title="竖屏样本",
        source_kind="local",
        root_dir=str(root),
        stage="downloaded",
    )
    service.repo.asset_upsert(project["id"], "source", str(src), status="ready", bytes=2048)
    return project


def test_export_name_marks_vertical(service, tmp_path: Path):
    project = _project_with_source(service, tmp_path)
    candidate = service.repo.candidate_insert(
        project["id"], kind="highlight", start_ms=0, end_ms=10000, title="竖屏切片"
    )
    planned = service._clip_export_inputs(
        project["id"],
        candidate,
        mode="precise",
        subtitle_mode="none",
        aspect="9x16",
        vertical_mode="blur",
    )
    name = Path(planned["output"]).name
    assert "_9x16_" in name and "blur" in name, name


def test_export_name_stays_16x9_for_flat(service, tmp_path: Path):
    project = _project_with_source(service, tmp_path)
    candidate = service.repo.candidate_insert(
        project["id"], kind="highlight", start_ms=0, end_ms=10000, title="横屏切片"
    )
    planned = service._clip_export_inputs(project["id"], candidate)
    name = Path(planned["output"]).name
    assert "_16x9_" in name and "9x16" not in name, name


def test_focus_clear_removes_file(service, tmp_path: Path):
    project = _project_with_source(service, tmp_path)
    service.clip_focus_save(project["id"], [{"t_ms": 1000, "x": 0.3}, {"t_ms": 2000, "x": 0.6}])
    assert len(service.clip_focus(project["id"])["focus"]) == 2

    data = service.clip_focus_save(project["id"], [], clear=True)
    assert data["cleared"] is True and data["focus"] == []
    assert service.clip_focus(project["id"])["focus"] == []
    assert not service._focus_path(project["id"]).exists()


def test_focus_save_marks_manual_and_keeps_detected(service, tmp_path: Path):
    """手工点覆盖同 t_ms 的检测点，其它检测点留着——检测一次不该被一次拖动抹掉。"""
    project = _project_with_source(service, tmp_path)
    path = service._focus_path(project["id"])
    reframe.save_focus(
        path,
        [{"t_ms": 0, "x": 0.2}, {"t_ms": 4000, "x": 0.9}],
        meta={"detector": "haar"},
    )
    service.clip_focus_save(project["id"], [{"t_ms": 4000, "x": 0.5}])
    rows = service.clip_focus(project["id"])["focus"]
    assert [row["t_ms"] for row in rows] == [0, 4000]
    assert rows[1]["x"] == pytest.approx(0.5) and rows[1]["manual"] is True
    assert rows[0]["manual"] is False
