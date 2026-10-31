"""本地导入测试（P1-05）。

验收面：**识别视频、认出同名字幕、重复导入不重复建项目、单个坏文件不拖垮整批。**
不碰 ffmpeg（prober/thumbs 都注入假的），所以这台机器上装没装都不影响结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import importer
from video_workbench.core.importer import import_videos, resolve_inputs, scan_folder


class Info:
    duration_ms = 90_000
    width = 1920
    height = 1080
    fps = 30.0
    vcodec = "h264"
    acodec = "aac"


def touch(path: Path, size: int = 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_scan_folder_only_videos(tmp_path):
    touch(tmp_path / "a.mp4")
    touch(tmp_path / "b.mkv")
    touch(tmp_path / "c.txt")
    touch(tmp_path / "sub" / "d.flv")
    plain = scan_folder(tmp_path, recursive=False)
    assert [p.name for p in plain] == ["a.mp4", "b.mkv"]
    deep = scan_folder(tmp_path, recursive=True)
    assert {p.name for p in deep} == {"a.mp4", "b.mkv", "d.flv"}


def test_scan_folder_missing_path(tmp_path):
    with pytest.raises(WBError) as info:
        scan_folder(tmp_path / "nope")
    assert info.value.code == "NOT_FOUND"


def test_resolve_inputs_dedupes(tmp_path):
    a = touch(tmp_path / "a.mp4")
    resolve = resolve_inputs([a, str(a), tmp_path])
    assert [p.name for p in resolve] == ["a.mp4"], "同一文件给两次只算一次"


def test_import_creates_project_and_asset(service, tmp_path):
    video = touch(tmp_path / "素材" / "第01讲.mp4")
    result = import_videos(
        [video], media_root=tmp_path / "media", repo=service.repo, prober=lambda _p: Info(), make_thumbs=False
    )
    assert result["total"] == 1 and len(result["imported"]) == 1
    project = service.repo.project_get(result["imported"][0]["project_id"])
    assert project["source_kind"] == "local"
    assert project["stage"] == "new", "没有字幕就该等转录"
    assets = service.repo.asset_list(project["id"])
    assert len(assets) == 1
    assert assets[0]["path"].endswith("第01讲.mp4")
    assert assets[0]["duration_ms"] == 90_000
    assert Path(project["root_dir"]).is_dir(), "衍生品目录必须建出来"
    assert assets[0]["path"] != str(Path(project["root_dir"]) / "source.mp4"), "导入不搬文件"


def test_import_recognises_sibling_subtitle(service, tmp_path):
    video = touch(tmp_path / "第02讲.mp4")
    (tmp_path / "第02讲.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
    result = import_videos([video], media_root=tmp_path / "media", repo=service.repo, make_thumbs=False)
    project = service.repo.project_get(result["imported"][0]["project_id"])
    assert project["stage"] == "subtitle", "有字幕就不该再排转录"
    kinds = {a["kind"] for a in service.repo.asset_list(project["id"])}
    assert kinds == {"source", "subtitle"}


def test_import_twice_is_idempotent(service, tmp_path):
    video = touch(tmp_path / "第03讲.mp4")
    first = import_videos([video], media_root=tmp_path / "media", repo=service.repo, make_thumbs=False)
    second = import_videos([video], media_root=tmp_path / "media", repo=service.repo, make_thumbs=False)
    assert len(first["imported"]) == 1
    assert second["imported"] == []
    assert second["skipped"] and second["skipped"][0]["reason"] == "已导入"
    assert service.repo.project_count("") == 1


def test_bad_file_does_not_break_batch(service, tmp_path, monkeypatch):
    good = touch(tmp_path / "好.mp4")
    bad = touch(tmp_path / "坏.mp4")
    real_upsert = service.repo.asset_upsert

    def flaky(project_id, kind, path, **fields):
        if path.endswith("坏.mp4"):
            raise RuntimeError("磁盘错误")
        return real_upsert(project_id, kind, path, **fields)

    monkeypatch.setattr(service.repo, "asset_upsert", flaky)
    result = import_videos([good, bad], media_root=tmp_path / "media", repo=service.repo, make_thumbs=False)
    assert len(result["imported"]) == 1
    assert len(result["failed"]) == 1 and result["failed"][0]["code"] == "INTERNAL"


def test_empty_input_is_validation_error(service, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WBError) as info:
        import_videos([empty], media_root=tmp_path / "media", repo=service.repo)
    assert info.value.code == "VALIDATION"


def test_thumbnail_failure_is_tolerated(service, tmp_path):
    video = touch(tmp_path / "第04讲.mp4")

    def boom(*_a, **_k):
        raise RuntimeError("抽帧炸了")

    result = import_videos(
        [video], media_root=tmp_path / "media", repo=service.repo, thumbs=boom, make_thumbs=True
    )
    assert len(result["imported"]) == 1
    assert result["imported"][0]["thumb"] == ""


def test_thumbnail_success_is_recorded(service, tmp_path):
    video = touch(tmp_path / "第05讲.mp4")
    marker = tmp_path / "media" / "本地导入" / "第05讲" / "frames" / "thumb.jpg"

    def fake_thumb(_src, _root):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"jpg")
        return marker

    result = import_videos(
        [video], media_root=tmp_path / "media", repo=service.repo, thumbs=fake_thumb, make_thumbs=True
    )
    assert result["imported"][0]["thumb"] == str(marker)

