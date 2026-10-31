"""P6-04 交付：剪映草稿 / OTIO / 降级包的纯函数层（`core/nle.py`）。

这里只测"拼出来的结构对不对"，不碰真剪映：能打开是
`scripts/accept_p6_nle.py` 的活（它会把草稿放进真草稿箱，由人打开确认）。

三个必须钉住的点：

1. **时间单位**：剪映草稿里是微秒，OTIO 是 `RationalTime`（帧）。
   混了单位不会报错，只会让 5 秒的片段变成 3 微秒。
2. **草稿骨架不能精简**：`check_flag`/`clip`/`hdr_settings` 这些看着没用的字段是
   剪映自己写的，模板里少了就打不开，所以断言模板"关键字段都在"。
3. **降级包必须有说明**：光有文件没有说明，用户不知道该拖到哪条轨上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import nle


def _doc(**kwargs):
    template = nle.load_template(nle.DRAFT_CONTENT_TEMPLATE)
    params = {
        "name": "测试草稿",
        "video_path": "C:/media/source.mp4",
        "source_duration_ms": 120_000,
        "width": 1280,
        "height": 720,
        "start_ms": 30_000,
        "end_ms": 45_000,
    }
    params.update(kwargs)
    return nle.jianying_document(template, **params)


def test_us_converts_milliseconds_to_microseconds():
    assert nle.us(1) == 1000
    assert nle.us(1500) == 1_500_000
    assert nle.us(0) == 0


def test_template_keeps_fields_jianying_writes_itself():
    """骨架里的"废话字段"是剪映的，删了就打不开。"""
    tpl = nle.load_template(nle.DRAFT_CONTENT_TEMPLATE)
    for key in ("canvas_config", "config", "keyframes", "materials", "platform"):
        assert key in tpl, key
    assert tpl["version"] == 360000
    assert tpl["platform"]["app_version"]
    # 素材槽位一个都不能少：剪映按 key 找列表，缺了当损坏
    for key in ("videos", "audios", "texts", "speeds"):
        assert key in tpl["materials"], key


def test_draft_duration_and_canvas_follow_the_clip():
    doc = _doc()
    assert doc["duration"] == 15_000_000  # 15 秒 → 微秒
    assert doc["canvas_config"] == {"width": 1280, "height": 720, "ratio": "original"}
    assert doc["fps"] == 30.0
    assert doc["name"] == "测试草稿"


def test_video_segment_is_trimmed_to_the_clip_range():
    doc = _doc()
    video_track = doc["tracks"][0]
    assert video_track["type"] == "video"
    seg = video_track["segments"][0]
    assert seg["source_timerange"] == {"start": 30_000_000, "duration": 15_000_000}
    assert seg["target_timerange"] == {"start": 0, "duration": 15_000_000}
    # 素材要指到源文件，且素材总长不能比片段短
    material = doc["materials"]["videos"][0]
    assert material["path"] == "C:/media/source.mp4"
    assert material["duration"] == 120_000_000
    assert material["id"] == seg["material_id"]


def test_subtitles_become_text_track_relative_to_clip():
    doc = _doc(
        subtitles=[
            {"start_ms": 31_000, "end_ms": 33_000, "text": "第一句"},
            {"start_ms": 44_000, "end_ms": 46_000, "text": "第二句"},  # 尾巴超出去要截
            {"start_ms": 20_000, "end_ms": 21_000, "text": "区间外"},  # 不该进来
        ]
    )
    text_tracks = [t for t in doc["tracks"] if t["type"] == "text"]
    assert len(text_tracks) == 1
    segs = text_tracks[0]["segments"]
    assert len(segs) == 2
    assert segs[0]["target_timerange"] == {"start": 1_000_000, "duration": 2_000_000}
    # 超出片段末尾的那句被截到片段长度（15000ms）
    assert segs[1]["target_timerange"]["start"] == 14_000_000
    assert segs[1]["target_timerange"]["duration"] == 1_000_000
    body = json.loads(doc["materials"]["texts"][0]["content"])
    assert body["text"] == "第一句"
    # 中性样式：白字、不加粗、无描边
    style = body["styles"][0]
    assert style["fill"]["content"]["solid"]["color"] == [1.0, 1.0, 1.0]
    assert style["strokes"] == []


def test_no_subtitle_means_no_text_track():
    doc = _doc(subtitles=[])
    assert [t["type"] for t in doc["tracks"]] == ["video"]


def test_bgm_adds_audio_track():
    doc = _doc(bgm_path="C:/media/bgm.mp3")
    kinds = [t["type"] for t in doc["tracks"]]
    assert kinds == ["video", "audio"]
    audio = doc["materials"]["audios"][0]
    assert audio["path"] == "C:/media/bgm.mp3"
    assert audio["name"] == "bgm.mp3"


def test_meta_points_at_the_draft_folder():
    meta_tpl = nle.load_template(nle.DRAFT_META_TEMPLATE)
    folder = Path("C:/Users/x/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft/测试")
    meta = nle.jianying_meta(meta_tpl, name="测试", folder=folder, created_ms=1_700_000_000_000)
    assert meta["draft_name"] == "测试"
    # 剪映存的是正斜杠
    assert meta["draft_fold_path"].endswith("/测试")
    assert "\\" not in meta["draft_fold_path"]
    assert meta["draft_root_path"].endswith("com.lveditor.draft")
    assert meta["tm_draft_create"] == 1_700_000_000


def test_write_jianying_creates_both_files(tmp_path: Path):
    doc = _doc()
    meta = nle.jianying_meta(
        nle.load_template(nle.DRAFT_META_TEMPLATE), name="t", folder=tmp_path / "t"
    )
    out = nle.write_jianying(tmp_path / "t", doc, meta)
    assert len(out["files"]) == 2
    assert (tmp_path / "t" / "draft_content.json").is_file()
    assert (tmp_path / "t" / "draft_meta_info.json").is_file()
    written = json.loads((tmp_path / "t" / "draft_content.json").read_text(encoding="utf-8"))
    assert written["duration"] == 15_000_000


def test_otio_timeline_shape():
    doc = nle.otio_document(
        name="切片",
        video_path="C:/media/source.mp4",
        start_ms=2000,
        end_ms=5000,
        width=1280,
        height=720,
        source_duration_ms=60_000,
    )
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    track = doc["tracks"]["children"][0]
    assert track["OTIO_SCHEMA"] == "Track.1" and track["kind"] == "Video"
    clip = track["children"][0]
    assert clip["OTIO_SCHEMA"] == "Clip.1"
    ref = clip["media_reference"]
    assert ref["OTIO_SCHEMA"] == "ExternalReference.1"
    assert ref["target_url"].startswith("file:///")
    assert ref["target_url"].endswith("source.mp4")
    # 3 秒 @30fps = 90 帧
    assert clip["source_range"]["duration"]["value"] == 90.0
    assert clip["source_range"]["start_time"]["value"] == 60.0
    assert doc["metadata"]["video_workbench"]["note"].startswith("字幕样式不跟随")


def test_write_otio(tmp_path: Path):
    doc = nle.otio_document(
        name="a", video_path="C:/m/s.mp4", start_ms=0, end_ms=1000
    )
    out = nle.write_otio(tmp_path / "a.otio", doc)
    data = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    assert data["OTIO_SCHEMA"] == "Timeline.1"
    assert out["bytes"] > 0


def test_bundle_notes_say_what_is_lost():
    text = nle.bundle_notes(
        project_title="项目",
        clip_title="切片",
        start_ms=0,
        end_ms=61_000,
        files=["片段.mp4", "字幕.srt"],
    )
    assert "片段.mp4" in text and "字幕.srt" in text
    assert "字幕样式不跟随" in text
    assert "01:01.0" in text  # 61000ms 的人话时间
    assert "剪映" in text and "达芬奇" in text


def test_write_bundle_copies_files_and_writes_notes(tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1024)
    sub = tmp_path / "s.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    out = nle.write_bundle(
        tmp_path / "pkg",
        files=[(str(video), "片段.mp4"), (str(sub), "字幕.srt")],
        notes="说明正文",
        copy_text="标题：x",
    )
    names = sorted(Path(p).name for p in out["files"])
    assert names == sorted(["片段.mp4", "字幕.srt", nle.BUNDLE_NOTE, nle.BUNDLE_COPY])
    assert (tmp_path / "pkg" / nle.BUNDLE_NOTE).read_text(encoding="utf-8") == "说明正文"


def test_write_bundle_reports_missing_source(tmp_path: Path):
    with pytest.raises(WBError) as exc:
        nle.write_bundle(
            tmp_path / "pkg", files=[(str(tmp_path / "nope.mp4"), "片段.mp4")], notes="x"
        )
    assert exc.value.code == "NOT_FOUND"


def test_channel_validation():
    assert nle.check_channel("JianYing") == "jianying"
    for bad in ("", "premiere", "剪映"):
        with pytest.raises(WBError) as exc:
            nle.check_channel(bad)
        assert exc.value.code == "VALIDATION"


def test_safe_draft_name_strips_illegal_chars():
    # sanitize_name 是"删掉"而不是"替换"：剪映拿文件夹名当草稿名，少几个字符不碍事
    assert nle.safe_draft_name("a/b:c*?") == "abc"
    assert nle.safe_draft_name("   ") == "VideoWorkbench"
    assert nle.safe_draft_name("", fallback="兜底") == "兜底"


def test_detect_apps_reports_both(tmp_path: Path):
    local = tmp_path / "Local"
    (local / "JianyingPro/User Data/Projects/com.lveditor.draft").mkdir(parents=True)
    (local / "DaVinci Resolve Welcome").mkdir(parents=True)
    apps = nle.detect_apps(home=local)
    assert apps["jianying"]["found"] and apps["resolve"]["found"]
    empty = nle.detect_apps(home=tmp_path / "none")
    assert not empty["jianying"]["found"] and not empty["resolve"]["found"]
