# -*- coding: utf-8 -*-
"""P4-02：快速拼接（concat 清单、命令、提交前后的把关）。

用假 FFmpeg 验「我们怎么指挥它」与「什么情况下不指挥」；真 ffmpeg 的成片与
时长验收在 `scripts/accept_p4_merge.py`。
"""

from __future__ import annotations

import json
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
    """按路径回固定规格；文件名里带 `narrow` 就变 720p，用来制造不一致。"""

    def __init__(self, *, duration_ms: int = 6000) -> None:
        self.duration_ms = duration_ms
        self.calls: list[tuple[str, Path, Path]] = []
        self.probed: list[str] = []
        self.listings: list[str] = []

    def probe(self, path):
        target = str(path)
        self.probed.append(target)
        narrow = "narrow" in Path(target).name
        return MediaInfo(
            duration_ms=self.duration_ms,
            width=1280 if narrow else 1920,
            height=720 if narrow else 1080,
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

    def merge_copy(self, list_path, dest, **kwargs):
        """假装拼接成功：产物照 expected 时长写点字节，并把清单内容抄下来。"""
        self.calls.append(("merge_copy", Path(list_path), Path(dest)))
        # 任务收尾会删掉 cache/tmp/<job>，所以清单内容必须此刻抄走
        self.listings.append(Path(list_path).read_text(encoding="utf-8"))
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-mp4")
        return dest


def make_files(tmp_path: Path, names: list[str]) -> list[str]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x" * 128)
        paths.append(str(path))
    return paths


def wait_job(service, job_id, timeout=8.0):
    """轮询到终态。后台线程写状态，断言前必须等，否则读到 running 就假红。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    return service.jobs.get(job_id)


# ---------------------------------------------------------------- 清单与命令


def test_concat_list_uses_forward_slashes_and_escapes_quote():
    text = merger.escape_concat_path("C:\\a b\\it's.mp4")
    # Windows 反斜杠在 demuxer 里是转义符，统一转正斜杠；单引号按 ffmpeg 的规则收尾
    assert text == "file 'C:/a b/it'\\''s.mp4'"


def test_concat_list_text_ends_with_newline_and_keeps_order():
    text = merger.concat_list_text(["a.mp4", "b.mp4"])
    assert text == "file 'a.mp4'\nfile 'b.mp4'\n"


def test_quick_command_is_copy_not_reencode():
    args = merger.quick_command("ffmpeg", "list.txt", "out.mp4")
    assert args[0] == "ffmpeg"
    assert args[1:4] == ["-hide_banner", "-y", "-f"]
    assert args[4] == "concat"
    assert ["-safe", "0"] == args[5:7]
    assert args[7:9] == ["-i", "list.txt"]
    assert ["-c", "copy"] == args[9:11]
    assert args[-1] == "out.mp4"
    assert "-vf" not in args
    assert "libx264" not in args


# ---------------------------------------------------------------- 为什么不能拼


def test_block_reason_needs_two_items():
    result = {"count": 1, "items": [{"ok": True}]}
    assert merger.block_reason(result) == "至少要两个可用片段才能拼接"


def test_block_reason_reports_missing_before_broken():
    result = {
        "count": 3,
        "missing": [{"name": "gone.mp4"}],
        "broken": [{"name": "a"}, {"name": "b"}],
    }
    assert "不在了" in merger.block_reason(result)


def test_block_reason_reports_differences_with_values():
    result = {
        "count": 2,
        "differences": [
            {"field": "fps", "label": "帧率", "values": [{"name": "a.mp4", "value": "30 fps"}]}
        ],
    }
    reason = merger.block_reason(result)
    assert reason.startswith("规格不一致：帧率")
    assert "a.mp4=30 fps" in reason


def test_block_reason_empty_for_precise_mode():
    assert merger.block_reason({"count": 0}, "precise") == ""


def test_difference_text_empty_when_consistent():
    assert merger.difference_text({"differences": []}) == ""


# ---------------------------------------------------------------- 服务层


def test_start_rejects_empty_list(service):
    with pytest.raises(WBError) as exc:
        service.merge_start(paths=[])
    assert exc.value.code == "VALIDATION"
    assert "先往列表里加片段" in exc.value.message


def test_start_rejects_unknown_mode(service, tmp_path):
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, mode="magic")
    assert exc.value.code == "VALIDATION"


def test_precise_mode_is_submitted_with_its_own_payload(service, tmp_path, monkeypatch):
    """P4-03 起精确合并可用：这里只验"派活得出去、模式没被改写成 quick"。

    真正把不同规格拼成一条在 `scripts/accept_p4_precise.py`（真 ffmpeg）。
    """
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)

    def fake_precise(**kw):
        # 真实现会建目录，假实现也得建——否则等于在测"文件系统会不会自己长目录"
        target = Path(kw["dest"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
        return target

    monkeypatch.setattr(fake, "merge_precise", fake_precise, raising=False)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    data = service.merge_start(paths=paths, mode="precise", out_dir=str(tmp_path / "out"))
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", json.dumps(job, ensure_ascii=False)
    assert job["payload"]["mode"] == "precise"
    assert job["result"]["mode"] == "precise"
    assert fake.calls == []  # 精确模式不该走 -c copy


def test_inconsistent_specs_block_before_any_job(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "narrow.mp4"])

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, out_dir=str(tmp_path / "out"))

    assert exc.value.code == "CONFLICT"
    assert "规格不一致" in exc.value.message
    # detail 里带结构化差异，界面能直接渲染
    detail = json.loads(exc.value.detail)
    assert detail and detail[0]["field"] in ("width", "height")
    # 一条命令都没跑，库里也没有半条合并记录
    assert fake.calls == []
    assert service.repo.merge_list() == []


def test_quick_merge_runs_and_records(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg(duration_ms=6000)
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    out_dir = tmp_path / "out"

    data = service.merge_start(paths=paths, out_dir=str(out_dir))
    job = data["job"]
    final = wait_job(service, job["id"])

    assert final["status"] == "succeeded", final
    result = final["result"]
    assert result["count"] == 2
    assert result["duration_ms"] == 6000
    assert Path(result["path"]).is_file()

    # 清单写过、写的是可用的两条；跑完之后这个临时目录必须已经清掉
    list_path = fake.calls[0][1]
    assert list_path == Path(service.cfg.cache_root) / "tmp" / job["id"] / "concat.txt"
    assert fake.listings[0].count("file '") == 2
    assert not list_path.exists(), "临时清单应该随任务收尾一起删掉"

    rows = service.repo.merge_list()
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["items"] == paths
    assert rows[0]["mode"] == "quick"
    assert rows[0]["fallback_used"] is False


def test_merge_list_reports_exists_after_file_removed(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    data = service.merge_start(paths=paths, out_dir=str(tmp_path / "out"))
    wait_job(service, data["job"]["id"])

    row = service.merge_list()["items"][0]
    assert row["exists"] is True
    Path(row["output_path"]).unlink()
    assert service.merge_list()["items"][0]["exists"] is False


def test_second_merge_with_same_name_does_not_overwrite(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    out_dir = tmp_path / "out"

    first = service.merge_start(paths=paths, out_dir=str(out_dir), out_name="同一条")
    wait_job(service, first["job"]["id"])
    second = service.merge_start(paths=paths, out_dir=str(out_dir), out_name="同一条")

    assert first["name"] == "同一条.mp4"
    assert second["name"] == "同一条 (2).mp4"


def test_disk_guard_blocks_before_creating_record(service, tmp_path, monkeypatch):
    from video_workbench.app import service as service_module

    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])

    def boom(path, need, what=""):
        raise WBError("DISK_FULL", "磁盘空间不够", f"需要 {need}")

    monkeypatch.setattr(service_module, "ensure_free_space", boom)

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, out_dir=str(tmp_path / "out"))
    assert exc.value.code == "DISK_FULL"
    assert service.repo.merge_list() == []


def test_missing_file_is_listed_not_merged(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    good = make_files(tmp_path, ["a.mp4", "b.mp4"])
    gone = str(tmp_path / "gone.mp4")

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=good + [gone], out_dir=str(tmp_path / "out"))
    assert exc.value.code == "CONFLICT"
    assert "不在了" in exc.value.message
    assert fake.calls == []


def test_failed_merge_marks_record_failed(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)

    def boom(list_path, dest, **kwargs):
        # **kwargs：service 会把 should_cancel / on_proc 透传下来（P5-01），
        # 替身不收就会变成 TypeError，而 TypeError 不是 WBError——
        # 记录会停在 running，看起来像"失败没被记下来"。
        raise WBError("PROCESS_FAILED", "拼接失败：moov atom not found")

    def boom_precise(**kwargs):
        raise WBError("PROCESS_FAILED", "重编码失败：moov atom not found")

    monkeypatch.setattr(fake, "merge_copy", boom)
    # P4-03 起快速失败会自动切精确，所以两条路都要断才算真失败
    monkeypatch.setattr(fake, "merge_precise", boom_precise, raising=False)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    data = service.merge_start(paths=paths, out_dir=str(tmp_path / "out"))
    wait_job(service, data["job"]["id"])

    final = service.jobs.get(data["job"]["id"])
    assert final["status"] == "failed"
    row = service.repo.merge_list()[0]
    assert row["status"] == "failed"
    assert "moov atom" in row["error"]


def test_merge_update_rejects_unknown_field(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    data = service.merge_start(paths=paths, out_dir=str(tmp_path / "out"))
    row = service.repo.merge_update(data["merge_id"], nope="x")
    assert "nope" not in row
    assert row["id"] == data["merge_id"]
