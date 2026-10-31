"""P3-05：切片导出（文件名、命令拼装、偏移、任务）。

用假 FFmpeg 验的是"我们怎么指挥它"；真 ffmpeg 的成片验收在
`scripts/accept_p3_export.py`（时长、可播、偏移）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.service import WorkbenchService
from video_workbench.core import exporter
from video_workbench.core.subtitles import normalize


# ---------------------------------------------------------------- 纯函数

def test_filename_keeps_readable_parts_and_timestamps():
    name = exporter.clip_filename(
        project_title="讲透注意力机制：从直觉到实现",
        candidate_title="先证伪直觉，再给方法",
        start_ms=12_400,
        end_ms=34_800,
    )
    # 全角冒号在 Windows 文件名里合法，保留；半角非法字符才洗
    assert name.startswith("讲透注意力机制：从直觉到实现_先证伪直觉，再给方法_")
    assert name.endswith("_16x9.mp4")
    assert "00m12.400-00m34.800" in name


def test_filename_washes_illegal_characters():
    name = exporter.clip_filename(
        project_title='a/b\\c:d*e?f"g<h>i|j',
        candidate_title="   ",
        start_ms=0,
        end_ms=1_000,
    )
    for bad in '/\\*?"<>|':
        assert bad not in name
    assert "_切片_" in name  # 候选标题空白时给兜底名


def test_filename_has_no_colon_in_timecode():
    """时间码不能写 `00:12.400`——Windows 文件名里不许有半角冒号。"""
    name = exporter.clip_filename(
        project_title="p", candidate_title="c", start_ms=3_723_456, end_ms=3_725_000
    )
    assert ":" not in name
    assert "62m03.456" in name


def test_long_titles_are_trimmed():
    name = exporter.clip_filename(
        project_title="长" * 200, candidate_title="候选" * 100, start_ms=0, end_ms=1_000
    )
    stem = name[: -len(".mp4")]
    assert len(stem) < 140


def test_build_args_precise_reencodes_with_crf18():
    args = exporter.build_args(
        "ffmpeg", src="s.mp4", dest="d.mp4", start_ms=12_400, end_ms=34_800
    )
    assert args[:1] == ["ffmpeg"]
    # -ss 必须在 -i 之前，否则精确定位要解码到切点，慢得多
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-ss") + 1] == "12.400"
    assert args[args.index("-t") + 1] == "22.400"
    assert "libx264" in args and args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "medium"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-b:a") + 1] == "192k"
    assert args[args.index("-ar") + 1] == "48000"
    assert "+faststart" in args
    assert "copy" not in args


def test_build_args_fast_copies_and_avoids_negative_ts():
    args = exporter.build_args(
        "ffmpeg", src="s.mp4", dest="d.mp4", start_ms=1_000, end_ms=9_000, mode="fast"
    )
    assert args[args.index("-c") + 1] == "copy"
    assert args[args.index("-avoid_negative_ts") + 1] == "make_zero"
    assert "libx264" not in args


def test_build_args_hard_subtitle_picks_filter_by_extension():
    """真链路抓出来的：把 srt 喂给 ass= 滤镜，libass 直接建不出 track。

    报错是 `Could not create a libass track when reading file '…'` 接着
    `Error opening output files: Invalid argument`（-22），以前这条测试
    断言的正是那个错行为——换任何机器都跑不通，只是在自证通过。
    """
    args = exporter.build_args(
        "ffmpeg", src="s.mp4", dest="d.mp4", start_ms=0, end_ms=5_000,
        subtitle="new.srt", subtitle_mode="hard",
    )
    vf = args[args.index("-vf") + 1]
    assert vf.startswith("subtitles=")
    assert "ass=" not in vf
    assert "libx264" in args  # 烧字幕必须重编码


def test_build_args_hard_subtitle_keeps_ass_filter_for_ass(tmp_path):
    ass = tmp_path / "subtitle.ass"
    ass.write_text("[Script Info]\n", encoding="utf-8")
    args = exporter.build_args(
        "ffmpeg", src="s.mp4", dest="d.mp4", start_ms=0, end_ms=5_000,
        subtitle=str(ass), subtitle_mode="hard",
    )
    vf = args[args.index("-vf") + 1]
    # ASS 走 ass=（样式可控），并且冒号要转义，否则被当参数分隔符
    assert vf.startswith("ass=") and "\\:" in vf


def test_build_args_soft_subtitle_maps_second_input():
    args = exporter.build_args(
        "ffmpeg", src="s.mp4", dest="d.mp4", start_ms=0, end_ms=5_000,
        subtitle="new.srt", subtitle_mode="soft",
    )
    assert args.count("-i") == 2
    assert args[args.index("-map") + 1] == "0:v:0"
    assert "1:0" in args
    assert args[args.index("-c:s") + 1] == "mov_text"


def test_build_args_rejects_unknown_modes():
    with pytest.raises(ValueError):
        exporter.build_args("ffmpeg", src="a", dest="b", start_ms=0, end_ms=1, mode="turbo")
    with pytest.raises(ValueError):
        exporter.build_args(
            "ffmpeg", src="a", dest="b", start_ms=0, end_ms=1, subtitle_mode="karaoke"
        )


def test_build_args_requires_subtitle_file_when_mode_set():
    with pytest.raises(ValueError):
        exporter.build_args(
            "ffmpeg", src="a", dest="b", start_ms=0, end_ms=1, subtitle_mode="soft"
        )


def test_span_is_at_least_one_millisecond():
    args = exporter.build_args("ffmpeg", src="a", dest="b", start_ms=5_000, end_ms=5_000)
    assert args[args.index("-t") + 1] == "0.001"


def test_delta_keeps_sign():
    """快速模式成片可能更长，也可能更短，两种都要能被说清楚。"""
    assert exporter.duration_delta_ms(22_400, 21_900) == -500
    assert exporter.duration_delta_ms(20_040, 21_491) == 1_451
    assert exporter.duration_delta_ms(1_000, 1_000) == 0


def test_offset_note_says_what_happened():
    assert exporter.offset_note("precise", 1_451) == ""
    assert "正好落在关键帧" in exporter.offset_note("fast", 0)
    # 变长：开头多带了（关键帧在切点之前）——第一版这里错报成"正好"
    assert "长 1451 ms" in exporter.offset_note("fast", 1_451)
    # 变短：片尾吸到关键帧
    assert "短 500 ms" in exporter.offset_note("fast", -500)
    assert "精确模式" not in exporter.offset_note("fast", -500)


# ---------------------------------------------------------------- 服务层

class FakeFFmpeg:
    """假 ffmpeg：记录调用，写出一个固定大小的文件。"""

    def __init__(self, *, out_duration_ms=22_400, fail=False):
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.out_duration_ms = out_duration_ms
        self.fail = fail
        self.calls = []
        self.probed = []

    def export_clip(self, src, dest, **kwargs):
        self.calls.append((str(src), str(dest), kwargs))
        if self.fail:
            raise WBError("PROCESS_FAILED", "ffmpeg 执行失败（exit 1）", "boom", retryable=True)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x" * 4096)
        return Path(dest)

    def probe(self, path):
        self.probed.append(str(path))

        class Info:
            pass

        info = Info()
        info.duration_ms = self.out_duration_ms
        info.height = 1080
        info.width = 1920
        info.bytes = Path(path).stat().st_size if Path(path).exists() else 0
        return info


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]


def wait_job(service, job_id, timeout=10.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.02)
    return service.jobs.get(job_id)


def make_project(service, tmp_path: Path, *, with_source=True, with_subtitle=False,
                 with_candidate=True) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="analyzed"
    )
    segments = normalize(
        [
            # 刻意把第一条压在候选起点（12400）之前，测"跨起点的截到 0"
            {"start_ms": 12_000, "end_ms": 14_000, "text": "今天讲三件事"},
            {"start_ms": 20_000, "end_ms": 22_000, "text": "第一件事是定价"},
            # 整条在窗口之后，应该被丢掉
            {"start_ms": 40_000, "end_ms": 42_000, "text": "窗口外的话"},
        ]
    )
    service.repo.transcript_upsert(
        project["id"], segments=segments, language="zh", model="tiny",
        srt_path=str(root / "transcript.srt"),
    )
    if with_source:
        source = root / "source.mp4"
        source.write_bytes(b"x" * 2048)
        service.repo.asset_upsert(project["id"], "source", str(source), bytes=2048,
                                  duration_ms=60_000)
    if with_subtitle:
        sub = root / "subtitle.zh.srt"
        sub.write_text("1\n00:00:00,000 --> 00:00:02,000\n今天讲三件事\n", encoding="utf-8")
        service.repo.asset_upsert(project["id"], "subtitle", str(sub), bytes=sub.stat().st_size,
                                  language="zh")
    candidate = {}
    if with_candidate:
        candidate = service.repo.candidate_insert(
            project["id"], kind="highlight", start_ms=12_400, end_ms=34_800,
            score=86, title="先证伪直觉", source="ai",
        )
    return {"project": project, "root": root, "candidate": candidate}


def use_ffmpeg(service, monkeypatch, fake):
    monkeypatch.setattr(service, "_ffmpeg", fake)
    return fake


def test_export_rejects_unknown_candidate(service):
    error = err(service, "clip.export", {"project_id": "pj_x", "candidate_id": "cand_x"})
    assert error["code"] == "NOT_FOUND"


def test_export_rejects_unknown_mode(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "mode": "turbo"},
    )
    assert error["code"] == "VALIDATION"


def test_export_accepts_9x16_and_checks_vertical_mode(service, tmp_path, monkeypatch):
    """P3-06 之后 9x16 是合法画幅；不合法的是竖屏模式。"""
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "aspect": "9x16", "vertical_mode": "blur"},
    )
    assert out["job"]["payload"]["aspect"] == "9x16"
    assert out["job"]["payload"]["vertical_mode"] == "blur"
    error = err(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "aspect": "9x16", "vertical_mode": "swirl"},
    )
    assert error["code"] == "VALIDATION"


def test_export_rejects_unknown_aspect(service, tmp_path, monkeypatch):
    """用户手打 9:16（方案书和界面文案都是这么写的）必须能用；真正错的写法才拦。"""
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "aspect": "9:16", "vertical_mode": "blur"},
    )
    assert out["job"]["payload"]["aspect"] == "9x16"
    error = err(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "aspect": "4:3"},
    )
    assert error["code"] == "VALIDATION"
    assert "9x16" in error["message"] and "9:16" in error["message"]


def test_normalize_aspect_accepts_both_spellings():
    assert exporter.normalize_aspect("9:16") == "9x16"
    assert exporter.normalize_aspect(" 16:9 ") == "16x9"
    assert exporter.normalize_aspect("竖屏") == "9x16"
    for bad in ("", "4:3", "square"):
        with pytest.raises(ValueError):
            exporter.normalize_aspect(bad)


def test_export_without_source_is_conflict(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_source=False)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    assert error["code"] == "CONFLICT"


def test_export_without_subtitle_file_is_conflict(service, tmp_path, monkeypatch):
    """硬烧要真有内容可烧：既没转录文件、库里也没转录正文，才拦下来。

    以前拦的是"没有 srt 资产"——但真链路里转录完成后 DB 有正文、磁盘上未必要有
    srt，那时应该现生成 ASS 而不是报错（P6 实测踩到：喂 srt 给 ass= 滤镜会炸）。
    """
    made = make_project(service, tmp_path, with_subtitle=False)
    pid = made["project"]["id"]
    service.repo.transcript_clear(pid)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service, "clip.export",
        {"project_id": pid, "candidate_id": made["candidate"]["id"],
         "subtitle_mode": "hard"},
    )
    assert error["code"] == "CONFLICT"


def test_export_hard_generates_ass_from_transcript(service, tmp_path, monkeypatch):
    """没导出过 srt 也能硬烧：按当前字幕 + 默认样式现生成 ASS。"""
    made = make_project(service, tmp_path, with_subtitle=False)
    pid = made["project"]["id"]
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service, "clip.export",
        {"project_id": pid, "candidate_id": made["candidate"]["id"],
         "subtitle_mode": "hard"},
    )
    payload = out["job"]["payload"]
    assert payload["subtitle"].lower().endswith(".ass"), payload["subtitle"]
    assert payload["subtitle"].endswith("12400ms.ass"), payload["subtitle"]
    body = Path(payload["subtitle"]).read_text(encoding="utf-8")
    assert "[Script Info]" in body

    def to_ms(text):
        h, m, rest = text.split(":")
        s, cs = rest.split(".")
        return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(cs) * 10

    # ASS 的事件行是 `Dialogue: 0,<start>,<end>,Style,...`，没有 Start:/End: 标签
    events = re.findall(
        r"Dialogue:\s*\d+,(\d+:\d+:\d+\.\d\d),(\d+:\d+:\d+\.\d\d)", body)
    assert len(events) == 2, body          # 窗口外那条不该进来
    assert [to_ms(a) for a, _ in events] == [0, 7_600], events
    assert [to_ms(b) for _, b in events] == [1_600, 9_600], events


def test_export_soft_writes_srt_from_transcript(service, tmp_path, monkeypatch):
    """没导出过 srt 也能挂软字幕：从转录现写一份，不该反过来要求用户先导字幕。"""
    made = make_project(service, tmp_path, with_subtitle=False)
    pid = made["project"]["id"]
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service, "clip.export",
        {"project_id": pid, "candidate_id": made["candidate"]["id"],
         "subtitle_mode": "soft"},
    )
    subtitle = out["job"]["payload"]["subtitle"]
    assert subtitle.endswith(".srt"), subtitle
    assert "@12400ms" in subtitle, subtitle
    body = Path(subtitle).read_text(encoding="utf-8")
    # 切片从 12400 起：软字幕轨靠 -itsoffset 对齐，文件里仍是源时间，
    # 但窗口外的第三条（40000ms）不该进来
    assert "窗口外的话" not in body
    assert body.count("-->") == 2


def test_export_soft_without_any_subtitle_is_conflict(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_subtitle=False)
    pid = made["project"]["id"]
    service.repo.transcript_clear(pid)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service, "clip.export",
        {"project_id": pid, "candidate_id": made["candidate"]["id"],
         "subtitle_mode": "soft"},
    )
    assert error["code"] == "CONFLICT"
    assert "软字幕" in error["message"]


def test_export_hard_falls_back_to_srt_when_no_transcript(service, tmp_path, monkeypatch):
    """只有 srt、库里没转录正文（导入的外部字幕）：交原始文件，由 subtitles= 滤镜烧。"""
    made = make_project(service, tmp_path, with_subtitle=True)
    pid = made["project"]["id"]
    service.repo.transcript_clear(pid)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    out = rpc(
        service, "clip.export",
        {"project_id": pid, "candidate_id": made["candidate"]["id"],
         "subtitle_mode": "hard"},
    )
    assert out["job"]["payload"]["subtitle"].lower().endswith(".srt")


def test_export_precise_writes_file_and_asset(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", job
    assert fake.calls[0][2]["mode"] == "precise"
    out = Path(job["result"]["path"])
    assert out.is_file()
    assert out.parent == made["root"] / "export"
    assert job["result"]["duration_ms"] == 22_400
    assert job["result"]["offset_ms"] == 0
    assets = [a for a in service.repo.asset_list(made["project"]["id"]) if a["kind"] == "export"]
    assert [Path(a["path"]).name for a in assets] == [out.name]


def test_export_fast_reports_offset(service, tmp_path, monkeypatch):
    """快速模式实际切在关键帧上，时长比期望短 → 起点提前，要把偏移写清楚。"""
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(out_duration_ms=21_900))
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "mode": "fast"},
    )
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["mode"] == "fast"
    # FakeFFmpeg 出的成片比期望短 500ms → delta 为负，说明写"短 500 ms"
    assert job["result"]["delta_ms"] == -500
    assert job["result"]["offset_ms"] == 500
    assert "短 500 ms" in job["result"]["note"]


def test_export_precise_has_no_offset_note(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(out_duration_ms=22_380))
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job = wait_job(service, data["job"]["id"])
    assert job["result"]["delta_ms"] == -20  # 精确模式也会差几毫秒（容器取整）
    assert job["result"]["offset_ms"] == 20
    assert job["result"]["note"] == ""


def test_export_soft_passes_subtitle_path(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_subtitle=True)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "subtitle_mode": "soft"},
    )
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", job
    kwargs = fake.calls[0][2]
    assert kwargs["subtitle_mode"] == "soft"
    assert kwargs["subtitle"].endswith("subtitle.zh.srt")


def test_export_failure_keeps_job_failed(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(fail=True))
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "PROCESS_FAILED"


def test_exports_lists_queue_with_states(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(out_duration_ms=21_900))
    good = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "mode": "fast"},
    )
    wait_job(service, good["job"]["id"])
    rows = rpc(service, "clip.exports", {"project_id": made["project"]["id"]})["exports"]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "succeeded"
    assert row["mode"] == "fast"
    assert row["offset_ms"] == 500
    assert row["exists"] is True
    assert row["candidate_id"] == made["candidate"]["id"]


def test_exports_flags_missing_file(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job = wait_job(service, data["job"]["id"])
    Path(job["result"]["path"]).unlink()
    rows = rpc(service, "clip.exports", {"project_id": made["project"]["id"]})["exports"]
    assert rows[0]["exists"] is False


def test_exports_requires_project(service):
    assert err(service, "clip.exports", {"project_id": "pj_x"})["code"] == "NOT_FOUND"


def test_mode_suffix_distinguishes_modes():
    """三种模式切同一段时输出必须不同名，否则互相覆盖。"""
    names = {
        exporter.mode_suffix("precise"),
        exporter.mode_suffix("fast"),
        exporter.mode_suffix("precise", "hard"),
    }
    assert names == {"precise", "fast", "precise-hard"}
    with pytest.raises(ValueError):
        exporter.mode_suffix("turbo")


def test_filename_carries_suffix():
    name = exporter.clip_filename(
        project_title="p", candidate_title="c", start_ms=0, end_ms=1_000, suffix="fast"
    )
    assert name.endswith("_16x9_fast.mp4")


def test_three_modes_write_three_files(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path, with_subtitle=True)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    paths = set()
    for mode, sub in (("precise", "none"), ("fast", "none"), ("precise", "hard")):
        data = rpc(
            service, "clip.export",
            {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
             "mode": mode, "subtitle_mode": sub},
        )
        job = wait_job(service, data["job"]["id"])
        assert job["status"] == "succeeded", job
        paths.add(job["result"]["path"])
    assert len(paths) == 3, f"三种模式只产出 {len(paths)} 个文件：{sorted(paths)}"


def test_repeat_export_does_not_overwrite(service, tmp_path, monkeypatch):
    """同模式再导一次：加 " (2)"，不能把上一次的成片盖掉。"""
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    first = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job1 = wait_job(service, first["job"]["id"])
    second = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    job2 = wait_job(service, second["job"]["id"])
    assert job1["result"]["path"] != job2["result"]["path"]
    assert Path(job1["result"]["path"]).is_file() and Path(job2["result"]["path"]).is_file()


def test_manual_candidate_exports_like_ai_one(service, tmp_path, monkeypatch):
    """P3-04 的验收句"手动切片可导出"——手动候选走同一条导出路径。"""
    made = make_project(service, tmp_path, with_candidate=False)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    created = rpc(
        service, "clip.create",
        {"project_id": made["project"]["id"], "start_ms": 6_000, "end_ms": 26_040,
         "title": "手动切片"},
    )
    data = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": created["candidate"]["id"]},
    )
    job = wait_job(service, data["job"]["id"])
    assert job["status"] == "succeeded", job
    assert fake.calls[0][2]["start_ms"] == 6_000
    assert "手动切片" in Path(job["result"]["path"]).name


# ---------------------------------------------------------------- P6-03 音轨

def test_export_rejects_missing_bgm(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "bgm": str(tmp_path / "没有这个文件.mp3")},
    )
    assert error["code"] == "NOT_FOUND"
    assert "背景音乐" in error["message"]


def test_export_passes_bgm_volume_and_fade_to_job(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"fake audio")
    out = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"],
         "bgm": str(bgm), "bgm_volume": 40, "bgm_duck": False, "fade_ms": 200},
    )
    payload = out["job"]["payload"]
    assert payload["bgm"] == str(bgm.resolve())
    assert payload["bgm_volume"] == pytest.approx(0.4)  # 40% 换 0.4
    assert payload["bgm_duck"] is False
    assert payload["fade_ms"] == 200


def test_export_fade_defaults_to_config_and_never_exceeds_span(service, tmp_path, monkeypatch):
    made = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    first = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    # 配置默认 0（不淡），老片子导出的结果不该被静默改掉
    assert first["job"]["payload"]["fade_ms"] == service.cfg.clip.fade_ms == 0
    assert first["job"]["payload"]["bgm"] == ""

    service.cfg.clip.fade_ms = 900
    second = rpc(
        service, "clip.export",
        {"project_id": made["project"]["id"], "candidate_id": made["candidate"]["id"]},
    )
    # 候选 12.4s–34.8s = 22.4s，上限 3000 与 30% 都不会砍到 900
    assert second["job"]["payload"]["fade_ms"] == 900


def test_export_signature_counts_bgm_and_fade():
    """换配乐/改淡入淡出 = 另一版片子，不能被 skip_exported 当"已经导过"。"""
    base = {"candidate_id": "c1", "mode": "precise", "subtitle_mode": "none",
            "aspect": "16x9", "vertical_mode": "center"}
    plain = WorkbenchService._export_signature({"payload": dict(base)})
    with_bgm = WorkbenchService._export_signature(
        {"payload": {**base, "bgm": "D:/a.mp3", "fade_ms": 200}}
    )
    with_other_bgm = WorkbenchService._export_signature(
        {"payload": {**base, "bgm": "D:/b.mp3", "fade_ms": 200}}
    )
    assert plain != with_bgm
    assert with_bgm != with_other_bgm
