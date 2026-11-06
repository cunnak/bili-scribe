"""文案生成与封面（P3-08）。

这一层只管两件事：模型回的东西能不能直接拿去投稿、封面文件有没有真落到盘上。
字数/标签这些规则由平台定，所以断言都写死具体数字——改规则就该改测试。
"""

from __future__ import annotations

import json
from pathlib import Path

from video_workbench.core import copywriter, cover


class Reply:
    def __init__(self, text):
        self.text = text


class Stub:
    """按序回吐。抛异常就是"请求失败"。"""

    def __init__(self, *replies, model="stub-model"):
        self.replies = list(replies)
        self.calls = []
        self.model = model

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.replies:
            raise RuntimeError("没有更多回复了")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


GOOD = {
    "title": "他为什么坚持不做低价",
    "description": "同一款产品他把价格翻了三倍，这段讲清定价不是成本加法。",
    "tags": ["定价策略", "商业判断", "产品思维"],
}


# ------------------------------------------------------------------ 纯函数


def test_normalize_truncates_long_title_and_says_so():
    notes = []
    copy = copywriter.normalize_copy(
        {"title": "这是一个明显超过三十个字的标题" * 3, "description": "短", "tags": []},
        notes=notes,
    )
    assert len(copy["title"]) <= copywriter.TITLE_MAX
    assert any("标题超长" in item for item in notes)


def test_normalize_truncates_long_description():
    notes = []
    copy = copywriter.normalize_copy(
        {"title": "标题还在", "description": "描" * 300, "tags": ["a", "b", "c"]}, notes=notes
    )
    assert len(copy["description"]) <= copywriter.DESC_MAX
    assert any("简介超长" in item for item in notes)


def test_tags_strip_hash_and_dedupe_and_cap():
    tags = copywriter.normalize_tags(["#定价", "定价", "  ", "产品  思维", "a", "b", "c", "d"])
    assert "#" not in "".join(tags)
    assert len(tags) <= copywriter.TAGS_MAX
    assert tags.count("定价") == 1
    assert "产品思维" in tags  # 中间空格被压掉


def test_tag_string_input_is_split():
    tags = copywriter.normalize_tags("定价，产品、思维 商业")
    assert tags[:3] == ["定价", "产品", "思维"]


def test_empty_title_falls_back_to_candidate_title():
    notes = []
    copy = copywriter.normalize_copy(
        {"title": "精彩片段", "description": "讲了定价", "tags": ["定价", "商业", "产品"]},
        candidate={"title": "定价不是成本加法"},
        notes=notes,
    )
    assert copy["title"] == "定价不是成本加法"
    assert any("太笼统" in item for item in notes)


def test_missing_description_uses_quote():
    notes = []
    copy = copywriter.normalize_copy(
        {"title": "定价不是成本加法", "tags": ["定价", "商业", "产品"]},
        candidate={"quote": "你卖得便宜，是因为你不敢定价。"},
        notes=notes,
    )
    assert "不敢定价" in copy["description"]
    assert any("用切片里的原句" in item for item in notes)


def test_short_tags_are_padded_from_text():
    notes = []
    copy = copywriter.normalize_copy(
        {"title": "定价不是成本加法", "description": "讲定价策略和商业判断", "tags": ["定价"]},
        notes=notes,
    )
    assert len(copy["tags"]) >= copywriter.TAGS_MIN
    assert any("已补到" in item for item in notes)


def test_parse_reply_tolerates_fence_and_chatter():
    raw = '好的，这是文案：\n```json\n{"title":"x","description":"y","tags":["a","b","c"]}\n```\n希望有用'
    assert copywriter.parse_reply(raw)["title"] == "x"


def test_parse_reply_rejects_non_json():
    try:
        copywriter.parse_reply("我不想按格式回话")
    except copywriter.CopyError as exc:
        assert exc.code == "LLM_BAD_RESPONSE"
    else:  # pragma: no cover
        raise AssertionError("应该报 LLM_BAD_RESPONSE")


def test_build_prompt_replaces_every_placeholder():
    template = copywriter.load_prompt()
    prompt = copywriter.build_prompt(
        {"kind": "highlight", "start_ms": 0, "end_ms": 12_400, "title": "标题", "reason": "理由"},
        [{"text": "第一句"}, {"text": "第二句"}],
        project_title="项目名",
        template=template,
    )
    assert "$" not in prompt.replace("$title_max", "").replace("$desc_max", "")
    assert "项目名" in prompt and "12.4 秒" in prompt and "第一句" in prompt


def test_publish_block_joins_parts():
    text = copywriter.publish_block(GOOD)
    assert text.splitlines()[0] == GOOD["title"]
    assert "#定价策略" in text


def test_rule_copy_uses_candidate_only():
    copy = copywriter.rule_copy(
        {"title": "片段标题", "quote": "原话一句", "reason": "理由一句", "tags": ["甲"]},
        project_title="项目",
    )
    assert copy["title"] == "片段标题"
    assert "原话一句" in copy["description"]
    assert copy["tags"][0] == "甲"


# ------------------------------------------------------------------ generate


def test_generate_uses_model_and_marks_source():
    stub = Stub(json.dumps(GOOD, ensure_ascii=False))
    out = copywriter.generate(
        {"id": "c1", "start_ms": 0, "end_ms": 30_000, "title": "t"}, [{"text": "x"}], client=stub
    )
    assert out["source"] == "llm"
    assert out["copy"]["title"] == GOOD["title"]
    assert out["model"] == "stub-model"
    assert stub.calls[0]["kwargs"].get("json_mode") is True


def test_generate_falls_back_when_model_raises():
    stub = Stub(RuntimeError("boom"), RuntimeError("boom"))
    out = copywriter.generate(
        {"id": "c1", "start_ms": 0, "end_ms": 30_000, "title": "兜底标题", "quote": "兜底原句"},
        [{"text": "x"}],
        client=stub,
    )
    assert out["source"] == "rule"
    assert out["copy"]["title"] == "兜底标题"
    assert any("兜底文案" in item for item in out["notes"])


def test_generate_retries_once_on_bad_json_then_succeeds():
    stub = Stub("不是 JSON", json.dumps(GOOD, ensure_ascii=False))
    out = copywriter.generate(
        {"id": "c1", "start_ms": 0, "end_ms": 30_000, "title": "t"}, [{"text": "x"}], client=stub
    )
    assert out["source"] == "llm"
    assert out["attempts"] == 2


def test_generate_tolerates_client_without_json_mode():
    class Bashful:
        model = "old"

        def __init__(self):
            self.calls = 0

        def chat(self, messages):
            self.calls += 1
            return json.dumps(GOOD, ensure_ascii=False)

    client = Bashful()
    out = copywriter.generate(
        {"id": "c1", "start_ms": 0, "end_ms": 30_000, "title": "t"}, [{"text": "x"}], client=client
    )
    assert out["source"] == "llm"
    assert client.calls >= 1


# ------------------------------------------------------------------ 封面


def test_default_at_is_start_plus_one_second():
    assert cover.default_at(10_000, 40_000) == 11_000


def test_default_at_is_clamped_inside_short_clip():
    # 0.8 秒的手动切片：+1 秒已经出界，必须夹在区间内
    assert cover.default_at(5_000, 5_800) == 5_799


def test_clamp_at_keeps_value_inside():
    assert cover.clamp_at(5_000, 9_000, 1_000) == 5_000
    assert cover.clamp_at(5_000, 9_000, 99_000) == 8_999


def test_cover_path_is_stable_per_candidate():
    root = Path("C:/tmp/root")
    assert cover.cover_path(root, "cand_1") == cover.cover_path(root, "cand_1")
    assert cover.cover_path(root, "cand_1").name == "cand_1.jpg"


def test_is_image_only_accepts_pictures():
    assert cover.is_image("a.PNG") and cover.is_image("b.webp")
    assert not cover.is_image("a.mp4")


class FakeFF:
    """假 ffmpeg：只写字节，不真抽帧。"""

    def __init__(self, *, write=True):
        self.calls = []
        self.write = write

    def thumbnail(self, src, dest, *, at=1.0, width=480, quality=4):
        self.calls.append({"src": str(src), "dest": str(dest), "at": at, "width": width})
        target = Path(dest)
        if self.write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"jpeg-bytes")
        return target


def test_grab_frame_writes_cover_and_reports_offset(tmp_path):
    ff = FakeFF()
    candidate = {"id": "cand_1", "start_ms": 4_000, "end_ms": 20_000}
    info = cover.grab_frame(ff, tmp_path / "src.mp4", candidate, tmp_path / "root")
    assert Path(info["path"]).is_file()
    assert info["at_ms"] == 5_000
    assert info["bytes"] > 0
    assert abs(ff.calls[0]["at"] - 5.0) < 1e-6
    assert ff.calls[0]["width"] == cover.COVER_WIDTH


def test_grab_frame_accepts_explicit_offset(tmp_path):
    ff = FakeFF()
    info = cover.grab_frame(
        ff,
        tmp_path / "src.mp4",
        {"id": "c", "start_ms": 0, "end_ms": 10_000},
        tmp_path,
        at_ms=7_000,
    )
    assert info["at_ms"] == 7_000


def test_grab_frame_raises_when_no_file_written(tmp_path):
    ff = FakeFF(write=False)
    try:
        cover.grab_frame(
            ff, tmp_path / "src.mp4", {"id": "c", "start_ms": 0, "end_ms": 9_000}, tmp_path
        )
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "PROCESS_FAILED"
    else:  # pragma: no cover
        raise AssertionError("没写出文件应该报 PROCESS_FAILED")


def test_frames_for_returns_one_pick_per_slot(tmp_path):
    ff = FakeFF()
    out = cover.frames_for(
        ff, tmp_path / "src.mp4", {"id": "c", "start_ms": 0, "end_ms": 10_000}, tmp_path, count=4
    )
    assert out["count"] == 4
    offsets = [item["at_ms"] for item in out["frames"]]
    assert offsets == sorted(offsets)
    assert all(0 <= value < 10_000 for value in offsets)


def test_frames_to_cleanup_lists_stale_files(tmp_path):
    keep_dir = cover.cover_dir(tmp_path) / "frames"
    keep_dir.mkdir(parents=True)
    (keep_dir / "a.jpg").write_bytes(b"1")
    (keep_dir / "b.jpg").write_bytes(b"1")
    stale = cover.frames_to_cleanup(tmp_path, [str(keep_dir / "a.jpg")])
    assert [Path(item).name for item in stale] == ["b.jpg"]


def test_copy_image_rejects_non_image(tmp_path):
    bad = tmp_path / "note.txt"
    bad.write_text("x", encoding="utf-8")
    try:
        cover.copy_image(bad, tmp_path / "cover.jpg")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "VALIDATION"
    else:  # pragma: no cover
        raise AssertionError("非图片应该被 VALIDATION 拦")


def test_copy_image_copies_bytes(tmp_path):
    src = tmp_path / "up.png"
    src.write_bytes(b"png-bytes")
    dest = cover.copy_image(src, tmp_path / "root" / "covers" / "c.jpg")
    assert dest.read_bytes() == b"png-bytes"
