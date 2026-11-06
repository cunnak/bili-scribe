"""P3-09：批量导出（多选 → 每条一个 job → 共用 batch_id → 一条失败不影响其他 → 重试）。

这一层的验收面是"调度"，不是"画面"：真 ffmpeg 出片在 `scripts/accept_p3_batch.py`。
假 FFmpeg 会按输出文件名故意让某一条失败——批量导出的关键语义就是**部分失败**，
用"全都成功"的夹具测不出这条。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.jobs import JobManager
from video_workbench.core.subtitles import normalize

# ---------------------------------------------------------------- 夹具


class FakeFFmpeg:
    """假 ffmpeg：正常写文件；`bad_titles` 里出现过的输出名一律失败。"""

    def __init__(self, *, out_duration_ms=22_400, bad_titles=(), raise_disk=False):
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.out_duration_ms = out_duration_ms
        self.bad_titles = tuple(bad_titles)
        self.raise_disk = raise_disk
        self.calls = []

    def export_clip(self, src, dest, **kwargs):
        self.calls.append((str(src), str(dest), kwargs))
        name = Path(dest).name
        if any(bad and bad in name for bad in self.bad_titles):
            raise WBError("PROCESS_FAILED", "ffmpeg 执行失败（exit 1）", "boom", retryable=True)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x" * 4096)
        return Path(dest)

    def probe(self, path):
        class Info:
            pass

        info = Info()
        info.duration_ms = self.out_duration_ms
        info.height = 1080
        info.width = 1920
        info.bytes = Path(path).stat().st_size if Path(path).exists() else 0
        return info


@pytest.fixture(autouse=True)
def no_disk_guard(monkeypatch):
    """磁盘守卫在单测里换成空实现：跑测试的机器剩多少空间不该影响断言。"""
    monkeypatch.setattr(
        "video_workbench.app.service.ensure_free_space",
        lambda path, need: {"free": 10**12, "need": int(need)},
    )


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]


def wait_job(service, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    return service.jobs.get(job_id)


def make_project(service, tmp_path: Path, *, titles=("第一条", "第二条", "第三条")) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="analyzed"
    )
    segments = normalize(
        [
            {"start_ms": 0, "end_ms": 2000, "text": "今天讲三件事"},
            {"start_ms": 2000, "end_ms": 4000, "text": "第一件事是定价"},
        ]
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=segments,
        language="zh",
        model="tiny",
        srt_path=str(root / "transcript.srt"),
    )
    source = root / "source.mp4"
    source.write_bytes(b"x" * 2_048_000)  # 2MB / 60s，估体积时有用
    service.repo.asset_upsert(
        project["id"], "source", str(source), bytes=2_048_000, duration_ms=60_000
    )
    candidates = []
    for index, title in enumerate(titles):
        start = 6_000 + index * 5_000
        candidates.append(
            service.repo.candidate_insert(
                project["id"],
                kind="highlight",
                start_ms=start,
                end_ms=start + 4_000,
                score=90 - index,
                title=title,
                source="ai",
            )
        )
    return {"project": project, "root": root, "candidates": candidates}


def use_ffmpeg(service, monkeypatch, fake):
    monkeypatch.setattr(service, "_ffmpeg", fake)
    return fake


# ---------------------------------------------------------------- 提交语义


def test_batch_submits_one_job_per_candidate_with_shared_batch_id(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    ids = [c["id"] for c in made["candidates"]]
    out = rpc(
        service, "clip.export.batch", {"project_id": made["project"]["id"], "candidate_ids": ids}
    )
    assert out["submitted"] == 3
    assert len(out["jobs"]) == 3
    assert out["batch_id"].startswith("batch_")
    batch_ids = set()
    for row in out["jobs"]:
        job = service.jobs.get(row["job_id"])
        assert job["status"] in ("queued", "running", "succeeded")
        assert job["payload"]["candidate_id"] == row["candidate_id"]
        assert job["payload"]["batch_id"] == out["batch_id"]
        batch_ids.add(job["payload"]["batch_id"])
    assert batch_ids == {out["batch_id"]}
    assert out["estimate_bytes"] > 0


def test_batch_dedupes_repeated_ids(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, titles=("甲", "乙"))
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    ids = [c["id"] for c in made["candidates"]]
    out = rpc(
        service,
        "clip.export.batch",
        {"project_id": made["project"]["id"], "candidate_ids": [ids[0], ids[0], ids[1]]},
    )
    assert out["submitted"] == 2


def test_batch_without_selection_is_validation(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service, "clip.export.batch", {"project_id": made["project"]["id"], "candidate_ids": []}
    )
    assert error["code"] == "VALIDATION"
    assert "勾" in error["message"]


def test_batch_skips_unknown_and_foreign_candidates(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    other = service.repo.project_create(
        title="别的项目", up_name="UP", root_dir=str(tmp_path / "media" / "别的"), stage="analyzed"
    )
    foreign = service.repo.candidate_insert(
        other["id"], kind="highlight", start_ms=0, end_ms=4_000, title="别处的", source="ai"
    )
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service,
        "clip.export.batch",
        {
            "project_id": made["project"]["id"],
            "candidate_ids": [made["candidates"][0]["id"], "cand_missing", foreign["id"]],
        },
    )
    assert out["submitted"] == 1
    reasons = {row["candidate_id"]: row["reason"] for row in out["skipped"]}
    assert reasons["cand_missing"] == "候选不存在"
    assert reasons[foreign["id"]] == "候选不属于这个项目"


def test_batch_rejects_unknown_mode_before_submitting(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    before = len(service.repo.job_list(limit=500, project_id=made["project"]["id"]))
    error = err(
        service,
        "clip.export.batch",
        {
            "project_id": made["project"]["id"],
            "candidate_ids": [made["candidates"][0]["id"]],
            "mode": "turbo",
        },
    )
    assert error["code"] == "VALIDATION"
    after = len(service.repo.job_list(limit=500, project_id=made["project"]["id"]))
    assert after == before  # 一条都不许钻进去


def test_batch_carries_vertical_settings(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, titles=("竖屏",))
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service,
        "clip.export.batch",
        {
            "project_id": made["project"]["id"],
            "candidate_ids": [made["candidates"][0]["id"]],
            "aspect": "9x16",
            "vertical_mode": "blur",
            "crop_x": 0.3,
        },
    )
    payload = service.jobs.get(out["jobs"][0]["job_id"])["payload"]
    assert payload["aspect"] == "9x16"
    assert payload["vertical_mode"] == "blur"
    assert abs(payload["crop_x"] - 0.3) < 1e-6


def test_batch_disk_guard_blocks_start(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())

    def boom(path, need):
        raise WBError("DISK_FULL", "磁盘空间不足：需要 8 GB，可用 1 GB")

    monkeypatch.setattr("video_workbench.app.service.ensure_free_space", boom)
    error = err(
        service,
        "clip.export.batch",
        {
            "project_id": made["project"]["id"],
            "candidate_ids": [c["id"] for c in made["candidates"]],
        },
    )
    assert error["code"] == "DISK_FULL"
    rows = [
        j
        for j in service.repo.job_list(limit=500, project_id=made["project"]["id"])
        if j.get("type") == "clip.export"
    ]
    assert rows == []


# ---------------------------------------------------------------- 部分失败与重试


def test_one_failure_does_not_stop_the_others(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    # 让第二条固定失败：批量导出的核心语义就是"部分成功"
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(bad_titles=("第二条",)))
    out = rpc(
        service,
        "clip.export.batch",
        {
            "project_id": made["project"]["id"],
            "candidate_ids": [c["id"] for c in made["candidates"]],
        },
    )
    statuses = {}
    for row in out["jobs"]:
        job = wait_job(service, row["job_id"])
        statuses[job["payload"]["candidate_id"]] = job["status"]
    ids = [c["id"] for c in made["candidates"]]
    assert statuses[ids[0]] == "succeeded"
    assert statuses[ids[1]] == "failed"
    assert statuses[ids[2]] == "succeeded"

    rows = rpc(service, "clip.exports", {"project_id": made["project"]["id"]})["exports"]
    failed = [r for r in rows if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["candidate_id"] == ids[1]
    assert failed[0]["batch_id"] == out["batch_id"]
    # 成功的两条真有文件，失败那条没有
    assert len([r for r in rows if r["exists"]]) == 2


def test_retry_after_partial_failure_succeeds(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    ids = [c["id"] for c in made["candidates"]]
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(bad_titles=("第二条",)))
    out = rpc(
        service, "clip.export.batch", {"project_id": made["project"]["id"], "candidate_ids": ids}
    )
    jobs = {row["candidate_id"]: row["job_id"] for row in out["jobs"]}
    failed = wait_job(service, jobs[ids[1]])
    assert failed["status"] == "failed"

    # 重试 = 换一台"能出片"的 ffmpeg 再单独提交那一条（保留失败历史）
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    again = rpc(
        service, "clip.export", {"project_id": made["project"]["id"], "candidate_id": ids[1]}
    )
    job = wait_job(service, again["job"]["id"])
    assert job["status"] == "succeeded"
    rows = rpc(service, "clip.exports", {"project_id": made["project"]["id"]})["exports"]
    assert len([r for r in rows if r["status"] == "succeeded"]) == 3
    assert len([r for r in rows if r["status"] == "failed"]) == 1  # 历史留着


def test_skip_exported_avoids_duplicates(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, titles=("甲", "乙"))
    ids = [c["id"] for c in made["candidates"]]
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    first = rpc(
        service, "clip.export.batch", {"project_id": made["project"]["id"], "candidate_ids": ids}
    )
    for row in first["jobs"]:
        assert wait_job(service, row["job_id"])["status"] == "succeeded"

    second = rpc(
        service,
        "clip.export.batch",
        {"project_id": made["project"]["id"], "candidate_ids": ids, "skip_exported": True},
    )
    assert second["submitted"] == 0
    assert {row["reason"] for row in second["skipped"]} == {"这条已经导出过（同参数）"}

    # 换参数（fast）不算"已经导出过"：同一段片还能再出一份快切的
    third = rpc(
        service,
        "clip.export.batch",
        {
            "project_id": made["project"]["id"],
            "candidate_ids": ids,
            "skip_exported": True,
            "mode": "fast",
        },
    )
    assert third["submitted"] == 2


def test_batch_reports_skipped_when_nothing_left(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, titles=("甲",))
    ids = [c["id"] for c in made["candidates"]]
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    first = rpc(
        service, "clip.export.batch", {"project_id": made["project"]["id"], "candidate_ids": ids}
    )
    wait_job(service, first["jobs"][0]["job_id"])
    again = rpc(
        service,
        "clip.export.batch",
        {"project_id": made["project"]["id"], "candidate_ids": ids, "skip_exported": True},
    )
    assert again["batch_id"] == ""
    assert again["jobs"] == []
    assert again["submitted"] == 0


# ---------------------------------------------------------------- JobManager


def test_submit_batch_into_unknown_type_is_unsupported(service):
    manager: JobManager = service.jobs
    with pytest.raises(WBError) as caught:
        manager.submit_batch("clip.nope", None, [{"a": 1}])
    assert caught.value.code == "UNSUPPORTED"


def test_submit_batch_stamps_batch_id_on_every_payload(service):
    manager: JobManager = service.jobs
    jobs = manager.submit_batch("dev.demo", None, [{"i": 1}, {"i": 2}], batch_id="batch_x")
    assert [j["payload"]["batch_id"] for j in jobs] == ["batch_x", "batch_x"]
    assert [j["payload"]["i"] for j in jobs] == [1, 2]
