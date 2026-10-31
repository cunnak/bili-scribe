"""P5-02 单测：启动恢复（任务标中断、合并收尾、项目阶段按产物回退）。

这几条的共同前提是"进程被强杀时库里的状态是对的，但产物可能只写了一半"，
所以断言的方向都是：**宁可退一步，也不要让界面声称一个打不开的状态**。
"""

from __future__ import annotations

from pathlib import Path

from video_workbench.core import recovery


def make_project(repo, tmp_path: Path, *, stage: str, title: str = "恢复样例") -> dict:
    root = tmp_path / "media" / title
    root.mkdir(parents=True, exist_ok=True)
    project = repo.project_create(title=title, root_dir=str(root))
    repo.project_update(project["id"], stage=stage)
    return repo.project_get(project["id"])


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


# ---------------------------------------------------------------- 任务与合并


def test_running_job_becomes_interrupted_retryable(service):
    job = service.repo.job_create("job_recover_1", "clip.export", None, {"a": 1})
    service.repo.job_update(job["id"], status="running", progress=0.4)

    stats = recovery.recover(service.repo)

    row = service.repo.job_get(job["id"])
    assert row["status"] == "failed"
    assert row["error_code"] == "INTERRUPTED"
    assert "可以重试" in row["error_message"]
    assert stats["jobs"] >= 1


def test_queued_job_also_counted(service):
    job = service.repo.job_create("job_recover_2", "merge.run", None, {})
    service.repo.job_update(job["id"], status="queued")

    recovery.recover(service.repo)

    assert service.repo.job_get(job["id"])["status"] == "failed"


def test_succeeded_job_is_untouched(service):
    job = service.repo.job_create("job_recover_3", "merge.run", None, {})
    service.repo.job_update(job["id"], status="succeeded")

    recovery.recover(service.repo)

    assert service.repo.job_get(job["id"])["status"] == "succeeded"


def test_running_merge_marked_failed(service):
    row = service.repo.merge_insert(items=[{"path": "a.mp4"}], mode="quick", status="running")

    stats = recovery.recover(service.repo)

    after = service.repo.merge_get(row["id"])
    assert after["status"] == "failed"
    assert "重新开始" in (after.get("error") or "")
    assert stats["merges"] >= 1


# ---------------------------------------------------------------- 项目阶段回退


def test_summarized_without_summary_file_rolls_back(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="summarized")
    # 库里有总结行，但 summary.md 不在（写一半被杀）
    service.repo.summary_insert(
        project["id"], content={"one_line": "一句话"}, content_md="# 一句话", role="standard"
    )

    stats = recovery.recover(service.repo)

    after = service.repo.project_get(project["id"])
    assert after["stage"] == "new"
    assert "启动检查" in (after.get("error") or "")
    assert stats["rolled"][0]["from"] == "summarized"


def test_summarized_with_file_kept(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="summarized")
    service.repo.summary_insert(
        project["id"], content={"one_line": "一句话"}, content_md="# 一句话", role="standard"
    )
    md = touch(Path(project["root_dir"]) / "summary.md")
    service.repo.asset_upsert(project["id"], "summary", str(md))

    recovery.recover(service.repo)

    assert service.repo.project_get(project["id"])["stage"] == "summarized"


def test_transcribed_without_transcript_rolls_back_to_downloaded(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="transcribed")
    source = touch(Path(project["root_dir"]) / "source.mp4")
    service.repo.asset_upsert(project["id"], "source", str(source))

    recovery.recover(service.repo)

    assert service.repo.project_get(project["id"])["stage"] == "downloaded"


def test_transcribed_with_transcript_kept(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="transcribed")
    source = touch(Path(project["root_dir"]) / "source.mp4")
    service.repo.asset_upsert(project["id"], "source", str(source))
    srt = touch(Path(project["root_dir"]) / "transcript.srt")
    asset = service.repo.asset_upsert(project["id"], "subtitle", str(srt))
    service.repo.transcript_upsert(
        project["id"],
        segments=[{"id": "s1", "start_ms": 0, "end_ms": 1000, "text": "你好"}],
        language="zh",
        model="tiny",
        asset_id=asset["id"],
    )

    recovery.recover(service.repo)

    assert service.repo.project_get(project["id"])["stage"] == "transcribed"


def test_clipped_without_candidates_rolls_back(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="clipped")

    recovery.recover(service.repo)

    assert service.repo.project_get(project["id"])["stage"] == "new"


def test_published_without_note_rolls_back(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="published")
    export = touch(Path(project["root_dir"]) / "export" / "a.mp4")
    service.repo.asset_upsert(project["id"], "export", str(export))

    recovery.recover(service.repo)

    # 有导出文件但没有发布记录：退到 merged（而不是 new）
    assert service.repo.project_get(project["id"])["stage"] == "merged"


def test_new_project_untouched(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="new")

    recovery.recover(service.repo)

    after = service.repo.project_get(project["id"])
    assert after["stage"] == "new"
    assert not after.get("error")


def test_unknown_stage_is_not_guessed(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="weird")
    service.repo.project_update(project["id"], stage="weird")

    recovery.recover(service.repo)

    assert service.repo.project_get(project["id"])["stage"] == "weird"


def test_recover_is_idempotent(service, tmp_path):
    project = make_project(service.repo, tmp_path, stage="summarized")

    first = recovery.recover(service.repo)
    second = recovery.recover(service.repo)

    assert first["projects"] >= 1
    assert second["projects"] == 0
    assert service.repo.project_get(project["id"])["stage"] == "new"


def test_app_info_exposes_recovery(service):
    data = service.router.dispatch("app.info", {}, "r")["data"]

    assert "recovery" in data
    assert set(data["recovery"]) >= {"jobs", "merges", "projects", "rolled"}
