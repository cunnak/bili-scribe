"""字幕导出（P2-06）：格式、缩放、样式映射、视频两种模式。

这一层最容易被"看起来对"骗过去的地方有三处，所以每条都钉死：

1. **字号缩放**：720P 是基准，1080P 要 ×1.5。写反或写死 48，
   1080P 上的字幕会小到看不清——而这条没有任何界面能提前发现。
2. **ASS 是 A/BGR**：``#FF0000`` 必须变成 ``&H000000FF``，直接拼 ``&H00FF0000``
   出来是蓝色。白字恰好两都"对"，所以只测白字等于没测。
3. **软字幕必须真有字幕流**：ffmpeg 退出码 0 也可能是 ``-map`` 写错，
   容器里根本没有字幕轨，用户打开发现"没字幕"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.core import subtitles
from video_workbench.core.subtitles import normalize

# ---------------------------------------------------------------- 纯格式层


def test_vtt_timestamps_use_three_digit_millis():
    body = subtitles.format_vtt(
        [
            {"start_ms": 0, "end_ms": 1500, "text": "第一句"},
            {"start_ms": 3_723_456, "end_ms": 3_724_000, "text": "一小时后"},
        ]
    )
    assert body.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in body
    assert "01:02:03.456 --> 01:02:04.000" in body


def test_vtt_header_becomes_note_and_arrow_in_body_is_escaped():
    body = subtitles.format_vtt(
        [{"start_ms": 0, "end_ms": 1000, "text": "A --> B"}], header="源：中文"
    )
    assert "NOTE 源：中文" in body
    # 正文里的 --> 会被当成新的定时行，必须换掉，否则成品在浏览器里错位
    assert "A → B" in body
    assert "A --> B" not in body


def test_vtt_bilingual_lines_are_two_physical_lines():
    body = subtitles.format_vtt(
        [
            {"start_ms": 0, "end_ms": 1000, "text": "原文", "lines": ["原文", "source"]},
        ]
    )
    rows = body.splitlines()
    start = rows.index("00:00:00.000 --> 00:00:01.000")
    assert rows[start + 1] == "原文"
    assert rows[start + 2] == "source"


def test_vtt_skips_empty_segments():
    body = subtitles.format_vtt(
        [
            {"start_ms": 0, "end_ms": 1000, "text": "有字"},
            {"start_ms": 1000, "end_ms": 2000, "text": ""},
        ]
    )
    assert body.count(" --> ") == 1


def test_ass_time_is_centiseconds_not_millis():
    # 写错一位就差 10 倍：1.234s 应出 01.23，不是 01.234 也不是 12.34
    assert subtitles.ass_time(1234) == "0:00:01.23"
    assert subtitles.ass_time(3_723_456) == "1:02:03.45"
    assert subtitles.ass_time(-5) == "0:00:00.00"


def test_ass_color_is_bgr_and_alpha_is_inverted():
    assert subtitles.ass_color("#FFFFFF") == "&H00FFFFFF"
    # 纯红：CSS FF0000 → ASS &H000000FF。拼成 &H00FF0000 就成了蓝色
    assert subtitles.ass_color("#FF0000") == "&H000000FF"
    assert subtitles.ass_color("#0000FF") == "&H00FF0000"
    assert subtitles.ass_color("") == "&H00FFFFFF"


def test_style_to_ass_maps_field_aliases():
    conf = subtitles.style_to_ass(
        {
            "font_name": "思源黑体",
            "font_size": 40,
            "outline_width": 3,
            "margin_bottom": 24,
            "text_color": "#FFCC00",
        }
    )
    assert conf["font"] == "思源黑体"
    assert conf["size"] == 40
    assert conf["outline"] == 3
    assert conf["margin_v"] == 24
    assert conf["primary"] == "#FFCC00"


def test_scale_style_uses_720p_as_base():
    base = {"font_size": 48, "outline_width": 2, "margin_v": 40, "shadow": 1}
    at720 = subtitles.scale_style(base, 720)
    assert (at720["size"], at720["outline"], at720["margin_v"]) == (48, 2, 40)
    at1080 = subtitles.scale_style(base, 1080)
    assert (at1080["size"], at1080["outline"], at1080["margin_v"]) == (72, 3, 60)
    # 4K 的字号不能小到看不见，也不能撑出画面：按比例就是 4 倍
    at2160 = subtitles.scale_style(base, 2160)
    assert at2160["size"] == 144
    # 高度传 0 时退回基准，而不是缩成 1/4
    assert subtitles.scale_style(base, 0)["size"] == 48


def test_format_ass_writes_scaled_style_into_header():
    body = subtitles.format_ass(
        [{"start_ms": 0, "end_ms": 1000, "text": "你好"}],
        style={"font_size": 48, "outline_width": 2, "margin_v": 40, "primary_color": "#FF0000"},
        height=1080,
    )
    head = body.split("Dialogue:")[0]
    assert "Style: Default," in head
    assert ",72," in head  # 48 × 1.5
    assert "&H000000FF" in head  # 红字，BGR 顺序
    assert "PlayResX: 1280" in head and "PlayResY: 720" in head


def test_format_ass_explicit_numbers_beat_style():
    body = subtitles.format_ass(
        [{"start_ms": 0, "end_ms": 1000, "text": "x"}], style={"font_size": 48}, size=30, height=720
    )
    assert ",30," in body


def test_format_ass_bilingual_uses_hard_line_break():
    body = subtitles.format_ass(
        [
            {"start_ms": 0, "end_ms": 1000, "text": "原文", "lines": ["原文", "source"]},
        ]
    )
    dialogue = [line for line in body.splitlines() if line.startswith("Dialogue:")][0]
    assert dialogue.endswith("原文\\Nsource")


def test_format_ass_escapes_braces_and_drops_empty():
    body = subtitles.format_ass(
        [
            {"start_ms": 0, "end_ms": 1000, "text": "{注释}留字"},
            {"start_ms": 1000, "end_ms": 2000, "text": "   "},
        ]
    )
    dialogues = [line for line in body.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 1
    # 花括号在 ASS 里是覆盖标签的语法，不换掉整行会消失
    assert "（注释）留字" in dialogues[0]
    assert "{" not in dialogues[0]


# ---------------------------------------------------------------- 服务层


class FakeFFmpeg:
    """只实现导出用得到的四个方法，行为由测试指定。"""

    def __init__(self, *, height=1080, streams=("sub",)):
        self.height = height
        self.streams = list(streams)
        self.available = True
        self.muxed: list[tuple] = []
        self.burned: list[tuple] = []
        self.probed: list[str] = []

    def require(self):
        return ("ffmpeg", "fake")

    def probe(self, path):
        self.probed.append(str(path))

        class Info:
            pass

        info = Info()
        info.height = self.height
        info.width = int(self.height * 16 / 9)
        info.duration_ms = 213_000
        info.bytes = Path(path).stat().st_size if Path(path).exists() else 1024
        return info

    def subtitle_streams(self, path):
        return [{"index": 2, "language": "chi"}] if self.streams else []

    def mux_subtitle(self, source, subtitle, output, **kwargs):
        self.muxed.append((str(source), str(subtitle), str(output), kwargs))
        Path(output).write_bytes(b"fake-mp4")
        return str(output)

    def burn_subtitle(self, source, subtitle, output, **kwargs):
        self.burned.append((str(source), str(subtitle), str(output), kwargs))
        Path(output).write_bytes(b"fake-burned")
        return str(output)


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]


def make_project(service, tmp_path: Path, *, texts=None, with_source=True) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="标题", up_name="UP", root_dir=str(root), stage="transcribed"
    )
    items = normalize(
        [
            {"start_ms": i * 2000, "end_ms": i * 2000 + 1800, "text": t}
            for i, t in enumerate(texts or ["今天讲三件事", "第一件事是定价", "第二件事是渠道"])
        ]
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=items,
        language="zh",
        model="tiny",
        srt_path=str(root / "transcript.srt"),
    )
    if with_source:
        source = root / "source.mp4"
        source.write_bytes(b"x" * 2048)
        service.repo.asset_upsert(
            project["id"], "source", str(source), bytes=2048, duration_ms=213_000
        )
    return project


def use_ffmpeg(service, monkeypatch, fake):
    monkeypatch.setattr(service, "_ffmpeg", fake)
    return fake


class FakeCtx:
    def __init__(self, project_id=""):
        self.project_id = project_id
        self.steps: list[tuple] = []
        self.logs: list[str] = []

    def guard_kwargs(self):
        """与真 JobContext 同形状：交给 FFmpeg 的 should_cancel / on_proc。"""
        return {"should_cancel": lambda: False, "on_proc": lambda proc: None}

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.steps.append((value, stage))

    def log(self, line):
        self.logs.append(line)

    def check_cancel(self):
        return None

    def should_cancel(self):
        return False


def test_export_rejects_unknown_mode(service, tmp_path):
    project = make_project(service, tmp_path)
    assert (
        err(service, "subtitle.export", {"project_id": project["id"], "mode": "magic"})["code"]
        == "VALIDATION"
    )


def test_export_needs_at_least_one_format_when_muxing_is_off(service, tmp_path):
    project = make_project(service, tmp_path)
    assert (
        err(service, "subtitle.export", {"project_id": project["id"], "formats": []})["code"]
        == "VALIDATION"
    )


def test_export_without_transcript_is_conflict(service, tmp_path):
    root = tmp_path / "media" / "UP" / "空"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(title="空", up_name="UP", root_dir=str(root))
    assert (
        err(service, "subtitle.export", {"project_id": project["id"], "formats": ["srt"]})["code"]
        == "CONFLICT"
    )


def test_export_writes_three_formats_and_registers_assets(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(height=1080))
    data = rpc(
        service, "subtitle.export", {"project_id": project["id"], "formats": ["srt", "ass", "vtt"]}
    )
    assert data["mode"] == "none"
    assert data["job_id"] == ""
    assert [f["format"] for f in data["files"]] == ["srt", "ass", "vtt"]
    root = Path(project["root_dir"])
    for name in ("subtitle.orig.srt", "subtitle.orig.ass", "subtitle.orig.vtt"):
        path = root / "export" / name
        assert path.is_file(), name
        assert path.stat().st_size > 0
    # SRT 是逗号毫秒 + 序号，ASS 是 Dialogue，VTT 是 WEBVTT 头
    assert "-->" in (root / "export" / "subtitle.orig.srt").read_text(encoding="utf-8")
    assert "Dialogue:" in (root / "export" / "subtitle.orig.ass").read_text(encoding="utf-8")
    assert (root / "export" / "subtitle.orig.vtt").read_text(encoding="utf-8").startswith("WEBVTT")
    kinds = [a["kind"] for a in service.repo.asset_list(project["id"])]
    assert kinds.count("subtitle") >= 3


def test_export_uses_source_height_when_height_is_auto(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg(height=1080))
    data = rpc(service, "subtitle.export", {"project_id": project["id"], "formats": ["ass"]})
    assert data["height"] == 1080
    assert fake.probed, "没探源视频就决定了基准高度"
    ass = (Path(project["root_dir"]) / "export" / "subtitle.orig.ass").read_text(encoding="utf-8")
    # 720 基准的默认字号 54，1080P 要放大到 81（不跟样式库一致就会"预览与导出差一截"）
    want = int(round(subtitles.ASS_DEFAULT["size"] * 1.5))
    assert f",{want}," in ass.split("Dialogue:")[0]


def test_export_falls_back_to_720_when_probe_fails(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)

    class Broken(FakeFFmpeg):
        def probe(self, path):
            from video_workbench.app.errors import WBError

            raise WBError("PROCESS_FAILED", "探不了")

    use_ffmpeg(service, monkeypatch, Broken())
    data = rpc(service, "subtitle.export", {"project_id": project["id"], "formats": ["ass"]})
    assert data["height"] == subtitles.ASS_BASE_HEIGHT


def test_export_dedupes_and_drops_unknown_formats(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    data = rpc(
        service, "subtitle.export", {"project_id": project["id"], "formats": ["SRT", "srt", "scc"]}
    )
    assert [f["format"] for f in data["files"]] == ["srt"]


def test_soft_export_adds_ass_and_submits_job(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg(height=1080))
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["srt"], "mode": "soft"},
    )
    # 软字幕也要 ASS 当输入：没选也得给你生成
    assert [f["format"] for f in data["files"]] == ["srt", "ass"]
    assert data["job_id"]
    assert data["output"].endswith("source.1080p.soft.mp4")
    assert fake.muxed == [], "提交阶段不该直接跑 ffmpeg"
    job = rpc(service, "job.get", {"id": data["job_id"]})
    assert job["type"] == "video.export"


def test_soft_export_without_source_is_not_found(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path, with_source=False)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    assert (
        err(
            service,
            "subtitle.export",
            {"project_id": project["id"], "formats": ["ass"], "mode": "soft"},
        )["code"]
        == "NOT_FOUND"
    )


def test_export_with_language_uses_translation_draft(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(height=720))
    service.repo.subtitle_upsert(
        project["id"],
        kind="translation",
        language="en",
        segments=normalize(
            [
                {
                    "start_ms": 0,
                    "end_ms": 1800,
                    "text": "今天讲三件事",
                    "translated_text": "Three things today",
                },
                {
                    "start_ms": 2000,
                    "end_ms": 3800,
                    "text": "第一件事是定价",
                    "translated_text": "First is pricing",
                },
            ]
        ),
        status="confirmed",
    )
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["srt", "ass"], "language": "en"},
    )
    assert [f["format"] for f in data["files"]] == ["srt", "ass"]
    srt = (Path(project["root_dir"]) / "export" / "subtitle.en.srt").read_text(encoding="utf-8")
    assert "Three things today" in srt
    assert "今天讲三件事" not in srt
    ass = (Path(project["root_dir"]) / "export" / "subtitle.en.ass").read_text(encoding="utf-8")
    # 默认版式原文在上：两行都在
    assert "今天讲三件事\\NThree things today" in ass


def test_export_with_missing_language_is_not_found(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    error = err(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["srt"], "language": "ja"},
    )
    assert error["code"] == "NOT_FOUND"


def test_export_job_soft_muxes_then_probes(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg(height=1080))
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["ass"], "mode": "soft"},
    )
    ctx = FakeCtx(project["id"])
    payload = {
        "source": str(Path(project["root_dir"]) / "source.mp4"),
        "subtitle": str(Path(project["root_dir"]) / "export" / "subtitle.orig.ass"),
        "output": data["output"],
        "mode": "soft",
        "height": 1080,
        "language": "orig",
    }
    result = service._export_job(ctx, payload)
    assert fake.muxed and not fake.burned
    assert result["output"] == data["output"]
    assert result["mode"] == "soft"
    assert Path(data["output"]).is_file()
    kinds = [a["kind"] for a in service.repo.asset_list(project["id"])]
    assert "export" in kinds
    assert ctx.steps[-1][0] == 1.0


def test_export_job_fails_when_soft_subtitle_not_in_container(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    use_ffmpeg(service, monkeypatch, FakeFFmpeg(streams=()))
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["ass"], "mode": "soft"},
    )
    payload = {
        "source": str(Path(project["root_dir"]) / "source.mp4"),
        "subtitle": str(Path(project["root_dir"]) / "export" / "subtitle.orig.ass"),
        "output": data["output"],
        "mode": "soft",
        "height": 1080,
    }
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        service._export_job(FakeCtx(project["id"]), payload)
    # 退出码 0 但没字幕流的产物必须报错，不能当成功
    assert exc.value.code == "PROCESS_FAILED"


def test_export_job_hard_uses_burn_and_skips_stream_check(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    fake = use_ffmpeg(service, monkeypatch, FakeFFmpeg(streams=()))
    data = rpc(
        service,
        "subtitle.export",
        {"project_id": project["id"], "formats": ["ass"], "mode": "hard"},
    )
    payload = {
        "source": str(Path(project["root_dir"]) / "source.mp4"),
        "subtitle": str(Path(project["root_dir"]) / "export" / "subtitle.orig.ass"),
        "output": data["output"],
        "mode": "hard",
        "height": 1080,
    }
    result = service._export_job(FakeCtx(project["id"]), payload)
    assert fake.burned and not fake.muxed
    assert result["mode"] == "hard"
    assert data["output"].endswith(".hard.mp4")


def test_export_job_missing_input_is_not_found(service, tmp_path, monkeypatch):
    use_ffmpeg(service, monkeypatch, FakeFFmpeg())
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        service._export_job(
            FakeCtx(),
            {
                "source": str(tmp_path / "nope.mp4"),
                "subtitle": str(tmp_path / "nope.ass"),
                "output": str(tmp_path / "out.mp4"),
            },
        )
    assert exc.value.code == "NOT_FOUND"
