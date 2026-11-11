# -*- coding: utf-8 -*-
"""合并页的输出目录：手选、校验、打开成片目录。

三条产品决策在这里落成判据：

1. 输出目录可以换成工作区外的目录，但**只有用户在系统对话框里亲手选过**才算数
   （`fs.pick(kind="dir")` 记一笔，`merge.start` 才敢收）；
2. 页面递来的任意字符串不能变成「往任意目录写文件」的原语，所以文件路径、不存在
   的目录、没选过的外部目录一律 `VALIDATION`；
3. 「打开输出目录」只认 `merge_id`，路径来自库里的 `wb_merge.output_path`，
   不放开 `fs.reveal` 的白名单。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

import pytest  # noqa: E402

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.infra.ffmpeg import MediaInfo  # noqa: E402


class FakeFFmpeg:
    """按路径回同一份规格；只为了让 merge_start 走到派活那一步。"""

    def __init__(self, *, duration_ms: int = 6000) -> None:
        self.duration_ms = duration_ms
        self.calls: list[tuple[str, Path, Path]] = []

    def probe(self, path):
        return MediaInfo(
            duration_ms=self.duration_ms,
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

    def merge_copy(self, list_path, dest, **kwargs):
        self.calls.append(("merge_copy", Path(list_path), Path(dest)))
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
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    return service.jobs.get(job_id)


@pytest.fixture
def picked_service(cfg_path, tmp_path):
    """带一个假 picker 的服务：picker 记下被问的 kind，并回一个外部目录。"""
    from video_workbench.app.service import WorkbenchService

    outside = tmp_path.parent / "outside_out"
    outside.mkdir(parents=True, exist_ok=True)
    asked: list[str] = []

    def picker(kind):
        asked.append(str(kind))
        return str(outside)

    svc = WorkbenchService(config_path=str(cfg_path), picker=picker)
    svc._test_outside = str(outside)  # 只给用例读，产品代码不碰这个名字
    svc._test_asked = asked
    try:
        yield svc
    finally:
        svc.close()


# ---------------------------------------------------------------- fs.pick


def test_fs_pick_dir_remembers_the_selected_folder(picked_service):
    data = picked_service.fs_pick(kind="dir")
    assert data["path"] == picked_service._test_outside
    assert picked_service._test_asked == ["dir"]
    assert picked_service._picked_dirs == [
        __import__("os").path.normcase(__import__("os").path.abspath(picked_service._test_outside))
    ]


def test_fs_pick_file_does_not_remember_a_write_target(cfg_path, tmp_path):
    """选文件不记：素材路径不该被当成「可以往这里写」的授权。"""
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=str(cfg_path), picker=lambda kind: str(tmp_path / "a.mp4"))
    try:
        svc.fs_pick(kind="files")
        assert svc._picked_dirs == []
    finally:
        svc.close()


def test_picked_dirs_keeps_at_most_32_and_drops_the_oldest(tmp_path):
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService.__new__(WorkbenchService)
    svc._picked_dirs = []
    for index in range(40):
        folder = tmp_path / f"d{index:02d}"
        folder.mkdir()
        svc._remember_picked_dir(str(folder))
    assert len(svc._picked_dirs) == 32
    assert "d07" not in "".join(svc._picked_dirs)
    assert "d39" in "".join(svc._picked_dirs)


# ---------------------------------------------------------------- merge.start 校验


def test_merge_start_rejects_manual_out_dir_outside_allowed_roots(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    outside = tmp_path.parent / "not_picked_out"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, out_dir=str(outside))

    assert exc.value.code == "VALIDATION"
    assert "选择输出目录" in exc.value.message
    assert fake.calls == []
    assert service.repo.merge_list() == []


def test_merge_start_accepts_a_folder_picked_from_the_dialog(picked_service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(picked_service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])

    picked_service.fs_pick(kind="dir")
    data = picked_service.merge_start(paths=paths, out_dir=picked_service._test_outside)
    job = wait_job(picked_service, data["job"]["id"])

    assert job["status"] == "succeeded", job
    assert Path(data["output"]).parent == Path(picked_service._test_outside)


def test_merge_start_rejects_a_file_as_out_dir(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    a_file = tmp_path / "not_a_folder.mp4"
    a_file.write_bytes(b"x")

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, out_dir=str(a_file))

    assert exc.value.code == "VALIDATION"
    assert "文件" in exc.value.message
    assert fake.calls == []


def test_merge_start_turns_missing_drive_into_validation_not_internal(
    service, tmp_path, monkeypatch
):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, out_dir="Z:\\definitely\\missing")

    assert exc.value.code == "VALIDATION"
    assert exc.value.code != "INTERNAL"


def test_merge_start_rejects_a_relative_out_dir(service, tmp_path, monkeypatch):
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])

    with pytest.raises(WBError) as exc:
        service.merge_start(paths=paths, out_dir="相对目录")

    assert exc.value.code == "VALIDATION"
    assert "绝对路径" in exc.value.message


def test_merge_start_creates_the_default_output_dir(service, tmp_path, monkeypatch):
    """留空走默认目录：默认目录可能还不存在，得由我们建出来。"""
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])

    data = service.merge_start(paths=paths)
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", job
    assert Path(data["output"]).parent.is_dir()


# ---------------------------------------------------------------- merge.reveal_output


def _succeed_once(service, tmp_path, monkeypatch) -> dict:
    fake = FakeFFmpeg()
    monkeypatch.setattr(service, "_ffmpeg", fake)
    paths = make_files(tmp_path, ["a.mp4", "b.mp4"])
    data = service.merge_start(paths=paths)
    wait_job(service, data["job"]["id"])
    return data


def test_merge_reveal_output_opens_parent_of_the_recorded_output(service, tmp_path, monkeypatch):
    data = _succeed_once(service, tmp_path, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: opened.append(str(p)), raising=False)

    result = service.merge_reveal_output(merge_id=data["merge_id"])

    assert result["path"] == str(Path(data["output"]).parent)
    assert opened == [str(Path(data["output"]).parent)]


def test_merge_reveal_output_rejects_unknown_merge_id(service):
    with pytest.raises(WBError) as exc:
        service.merge_reveal_output(merge_id="mg_does_not_exist")
    assert exc.value.code == "NOT_FOUND"


def test_merge_reveal_output_requires_a_merge_id(service):
    with pytest.raises(WBError) as exc:
        service.merge_reveal_output(merge_id="")
    assert exc.value.code == "VALIDATION"


def test_merge_reveal_output_does_not_accept_an_arbitrary_path(service, tmp_path, monkeypatch):
    """参数只有 merge_id：塞一个 path 进去不生效，也开不了别处。"""
    data = _succeed_once(service, tmp_path, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: opened.append(str(p)), raising=False)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = service.dispatch(
        "merge.reveal_output", {"merge_id": data["merge_id"], "path": str(elsewhere)}
    )

    assert out["ok"] is True, out
    assert opened == [str(Path(data["output"]).parent)]
    assert str(elsewhere) not in opened


def test_merge_reveal_output_reports_missing_folder(service, tmp_path, monkeypatch):
    """成片和目录都没了：给 NOT_FOUND，而不是一个打不开的空路径。"""
    data = _succeed_once(service, tmp_path, monkeypatch)
    target = Path(data["output"])
    target.unlink()
    target.parent.rmdir()

    with pytest.raises(WBError) as exc:
        service.merge_reveal_output(merge_id=data["merge_id"])
    assert exc.value.code == "NOT_FOUND"


def test_merge_reveal_output_is_registered_on_the_router(service):
    assert "merge.reveal_output" in service.router.names()
