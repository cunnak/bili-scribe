# -*- coding: utf-8 -*-
"""合并预检（P4-01）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

import pytest  # noqa: E402

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.core import merger  # noqa: E402
from video_workbench.infra.ffmpeg import MediaInfo  # noqa: E402


class FakeFFmpeg:
    """按路径回不同规格的假探针。文件名里带关键字就改规格，测试里好读。"""

    def __init__(self, specs: dict[str, dict] | None = None, errors: dict[str, str] | None = None):
        self.specs = specs or {}
        self.errors = errors or {}
        self.seen: list[str] = []

    def probe(self, path):
        target = str(path)
        self.seen.append(target)
        if target in self.errors:
            raise WBError("PROCESS_FAILED", self.errors[target], "假失败")
        base = dict(
            duration_ms=10_000,
            width=1920,
            height=1080,
            fps=30.0,
            vcodec="h264",
            acodec="aac",
            has_video=True,
            has_audio=True,
            bytes=1_000_000,
            pix_fmt="yuv420p",
            sample_rate=48000,
            channels=2,
        )
        base.update(self.specs.get(target, {}))
        return MediaInfo(**base)


@pytest.fixture()
def files(tmp_path: Path):
    made = []
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        path = tmp_path / name
        path.write_bytes(b"x" * 16)
        made.append(str(path))
    return made


def test_probe_item_missing_file_is_reported_not_raised(tmp_path: Path):
    item = merger.probe_item(FakeFFmpeg(), tmp_path / "nope.mp4")
    assert item["exists"] is False
    assert item["ok"] is False
    assert item["error"] == "文件不在了"


def test_probe_item_says_folder_instead_of_file_missing(tmp_path: Path):
    # 用户用文件夹选择框选错时，列表里必须说"这是文件夹"，不能谎报文件丢了。
    folder = tmp_path / "raw"
    folder.mkdir()
    item = merger.probe_item(FakeFFmpeg(), folder)
    assert item["exists"] is False
    assert item["ok"] is False
    assert item["error"] == "这是一个文件夹，不是视频文件"


def test_probe_item_carries_spec_fields(files):
    item = merger.probe_item(FakeFFmpeg(), files[0], origin="clip")
    assert item["ok"] is True
    assert item["pix_fmt"] == "yuv420p"
    assert item["sample_rate"] == 48000
    assert item["channels"] == 2
    assert item["origin"] == "clip"
    assert item["fps"] == 30.0


def test_probe_item_maps_probe_failure_to_error_text(files):
    fake = FakeFFmpeg(errors={files[0]: "moov atom not found"})
    item = merger.probe_item(fake, files[0])
    assert item["ok"] is False
    assert "moov atom not found" in item["error"]


def test_probe_item_rejects_file_without_video_stream(files):
    fake = FakeFFmpeg(specs={files[0]: {"has_video": False}})
    item = merger.probe_item(fake, files[0])
    assert item["ok"] is False
    assert item["error"] == "这个文件没有视频流"


def test_differences_empty_when_specs_match(files):
    result = merger.preflight(FakeFFmpeg(), files[:2])
    assert result["differences"] == []
    assert result["consistent"] is True
    assert result["can_quick"] is True


def test_differences_list_only_mismatched_fields(files):
    fake = FakeFFmpeg(specs={files[1]: {"fps": 25.0}})
    result = merger.preflight(fake, files[:2])
    assert [d["field"] for d in result["differences"]] == ["fps"]
    assert result["differences"][0]["label"] == "帧率"
    assert result["differences"][0]["values"][1]["value"] == "25 fps"
    assert result["can_quick"] is False
    assert result["consistent"] is False


def test_preflight_needs_two_files(files):
    result = merger.preflight(FakeFFmpeg(), files[:1])
    assert result["count"] == 1
    assert result["can_quick"] is False
    assert "至少要两个片段才能合并" in result["warnings"][0]


def test_preflight_reports_missing_files(tmp_path: Path, files):
    missing = str(tmp_path / "gone.mp4")
    result = merger.preflight(FakeFFmpeg(), [files[0], files[1], missing])
    assert [m["name"] for m in result["missing"]] == ["gone.mp4"]
    assert result["can_quick"] is False
    assert any("不在了" in w for w in result["warnings"])


def test_preflight_target_follows_first_and_can_be_overridden(files):
    follow = merger.preflight(FakeFFmpeg(), files[:2])
    assert follow["target"]["width"] == 1920
    assert follow["target"]["fps"] == 30.0

    forced = merger.preflight(FakeFFmpeg(), files[:2], width=1280, height=720, fps=25)
    assert forced["target"] == {
        "width": 1280,
        "height": 720,
        "fps": 25.0,
        "sample_rate": 48000,
        "channels": 2,
    }


def test_preflight_totals(files):
    result = merger.preflight(FakeFFmpeg(), files)
    assert result["total_ms"] == 30_000
    assert result["total_text"] == "0:30"
    assert result["total_bytes"] == 3_000_000
    assert result["count"] == 3


def test_estimate_quick_is_sum_of_parts_and_precise_is_smaller(files):
    result = merger.preflight(FakeFFmpeg(), files[:2])
    quick = merger.estimate(result, "quick")
    precise = merger.estimate(result, "precise")
    assert quick["size_bytes"] == result["total_bytes"]
    assert precise["size_bytes"] > 0
    assert quick["seconds"] < precise["seconds"]
    assert quick["size_text"].endswith("MB")


def test_estimate_precise_scales_with_target_pixels(files):
    result = merger.preflight(FakeFFmpeg(), files[:2])
    big = merger.estimate(
        {**result, "target": {**result["target"], "width": 3840, "height": 2160}}, "precise"
    )
    small = merger.estimate(
        {**result, "target": {**result["target"], "width": 1280, "height": 720}}, "precise"
    )
    assert big["size_bytes"] > small["size_bytes"]


def test_output_name_mentions_count_and_is_mp4(files):
    items = [merger.probe_item(FakeFFmpeg(), p) for p in files]
    name = merger.output_name(items)
    assert name.startswith("合并_3段_")
    assert name.endswith(".mp4")
    assert merger.output_name(items, suffix="_fast").endswith("_fast.mp4")


def test_output_name_has_no_forbidden_chars(files):
    items = [merger.probe_item(FakeFFmpeg(), p) for p in files]
    name = merger.output_name(items)
    for ch in '<>:"/\\|?*':
        assert ch not in name


def test_default_output_dir_prefers_project_root(tmp_path: Path):
    assert merger.default_output_dir(str(tmp_path), "C:/media") == tmp_path / "merge"


def test_default_output_dir_without_any_root_raises():
    with pytest.raises(WBError) as err:
        merger.default_output_dir("")
    assert err.value.code == "VALIDATION"


# ------------------------------------------------------------ 服务层


@pytest.fixture()
def service(cfg_path):
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=str(cfg_path))
    yield svc
    svc.close()


def _call(service, method: str, params: dict | None = None):
    return service.dispatch(method, params or {})


def _data(reply):
    assert reply["ok"] is True, reply
    return reply["data"]


def _error(reply):
    assert reply["ok"] is False, reply
    return reply["error"]


def test_merge_sources_lists_exports_and_sources(service, tmp_path: Path):
    project = service.repo.project_create(
        title="合并来源", source_kind="local", root_dir=str(tmp_path / "proj")
    )
    pid = project["id"]
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 32)
    exported = tmp_path / "clip.mp4"
    exported.write_bytes(b"y" * 32)
    service.repo.asset_upsert(pid, "source", str(source), bytes=32)
    service.repo.asset_upsert(pid, "export", str(exported), bytes=32)

    data = _data(_call(service, "merge.sources", {}))
    names = {row["name"] for row in data["clips"]}
    lib = {row["name"] for row in data["library"]}
    assert "clip.mp4" in names
    assert "source.mp4" in lib
    assert data["total"] == 2


def test_merge_sources_filters_by_query(service, tmp_path: Path):
    project = service.repo.project_create(
        title="注意力机制", source_kind="local", root_dir=str(tmp_path / "proj2")
    )
    pid = project["id"]
    a = tmp_path / "first.mp4"
    b = tmp_path / "second.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    service.repo.asset_upsert(pid, "export", str(a), bytes=1)
    service.repo.asset_upsert(pid, "export", str(b), bytes=1)
    data = _data(_call(service, "merge.sources", {"query": "注意力"}))
    assert len(data["clips"]) == 2
    data2 = _data(_call(service, "merge.sources", {"query": "first"}))
    assert [row["name"] for row in data2["clips"]] == ["first.mp4"]


def test_merge_preflight_requires_paths(service):
    error = _error(_call(service, "merge.preflight", {"paths": []}))
    assert error["code"] == "VALIDATION"
    assert "先往列表里加片段" in error["message"]


def test_merge_preflight_returns_estimate_and_output(service, tmp_path: Path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x" * 8)
    b.write_bytes(b"y" * 8)
    data = _data(_call(service, "merge.preflight", {"paths": [str(a), str(b)]}))
    # 两个假 mp4 探不出流：进 broken、计数为 0，但不该把预检打崩
    assert data["count"] == 0
    assert len(data["broken"]) == 2
    assert all(row["error"] for row in data["broken"])
    assert data["can_quick"] is False
    # 没有可用文件时输出目录退回 media_root/merge（而不是抛错）
    assert data["output"]["dir"] == str(Path(service.cfg.media_root) / "merge")
    assert data["output"]["name"].endswith(".mp4")
    assert set(data["estimate"]) == {"quick", "precise"}
