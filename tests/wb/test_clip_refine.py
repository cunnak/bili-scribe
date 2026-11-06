"""P6-01 画面精析的单元测试。

这一层最容易犯的错不是"模型答得不好"，而是**我们自己把它做成必需品**：
没配 vision、没源文件、抽帧失败、模型返回半截 JSON——四种情况都必须退化成
"候选照旧可用"，而不是整批候选失败。下面每条都钉一个这样的分支。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import clip_refine as cr

SEGS = [
    {"start_ms": 0, "end_ms": 3000, "text": "第一句开场"},
    {"start_ms": 3000, "end_ms": 7000, "text": "第二句讲机制"},
    {"start_ms": 7000, "end_ms": 12000, "text": "第三句给例子"},
]


def _cand(start=0, end=7000, score=60, title="旧标题", kind="highlight"):
    return {
        "kind": kind,
        "start_ms": start,
        "end_ms": end,
        "score": score,
        "title": title,
        "reason": "旧理由",
    }


class FakeClient:
    def __init__(self, reply: str = "", error: Exception | None = None, reject_temperature=False):
        self.reply = reply
        self.error = error
        self.reject_temperature = reject_temperature
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        if self.reject_temperature and "temperature" in kwargs:
            raise TypeError("unexpected keyword")
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply


class FakeFFmpeg:
    """只实现 `available` / `require` / `thumbnail` / `run`。

    `thumbnail` 真写一个字节，因为下游 `data_url` 会检查文件大小——
    假的空文件会让"抽帧成功"变成假绿。
    """

    def __init__(self, *, can_thumb=True, can_audio=True):
        self.available = True
        self.can_thumb = can_thumb
        self.can_audio = can_audio
        self.thumbs = 0

    def require(self):
        return "ffmpeg", "ffprobe"

    def thumbnail(self, src, dest, *, at=1.0, width=480, quality=4):
        if not self.can_thumb:
            raise WBError("PROCESS_FAILED", "抽帧失败")
        self.thumbs += 1
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\xff\xd8fakejpeg\xff\xd9")
        return out

    def run(self, args, timeout=None, **kwargs):
        if not self.can_audio:
            raise WBError("PROCESS_FAILED", "没有音轨")
        dest = Path(args[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 440Hz 正弦的 16bit 单声道 wav，让 audio_features 能真算出数字
        import math
        import struct
        import wave

        rate = 16000
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate // 4)
        )
        with wave.open(str(dest), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(frames)
        return ""


# ---------------------------------------------------------------- 纯函数


def test_frame_offsets_avoid_both_ends():
    points = cr.frame_offsets(0, 9000, 3)
    assert points == [2250, 4500, 6750], "三帧应该均匀落在窗口内部，不贴两端"


def test_frame_offsets_survives_zero_length_window():
    assert (
        cr.frame_offsets(5000, 5000, 2) == [5000, 5000] or len(cr.frame_offsets(5000, 5000, 2)) == 2
    )


def test_window_text_keeps_partial_overlap():
    text = cr.window_text(SEGS, 2500, 8000)
    assert "第一句开场" in text, "和窗口有重叠就该带进来，半句也算上下文"
    assert "第三句给例子" in text


def test_window_text_says_so_when_empty():
    assert "没有字幕" in cr.window_text(SEGS, 90000, 95000)


def test_item_block_carries_audio_numbers():
    block = cr.item_block(_cand(), SEGS, {"rms_dbfs": -21.5, "silence_ratio": 0.25})
    assert "-21.5 dBFS" in block and "25%" in block


def test_build_messages_puts_text_first_then_images():
    messages = cr.build_messages(
        _cand(), ["data:image/jpeg;base64,AAA", "data:image/jpeg;base64,BBB"], SEGS
    )
    assert messages[0]["role"] == "system"
    parts = messages[1]["content"]
    assert parts[0]["type"] == "text"
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]


def test_parse_reply_accepts_fenced_json():
    got = cr.parse_reply(
        '```json\n{"score": 88, "title": "这条能单独发", "reason": "话讲完了"}\n```'
    )
    assert got == {"score": 88, "title": "这条能单独发", "reason": "话讲完了"}


def test_parse_reply_keeps_partial_fields():
    assert cr.parse_reply('{"score": 120}')["score"] == 100, "分数要夹到 0–100"


def test_parse_reply_rejects_empty_object():
    with pytest.raises(WBError) as exc:
        cr.parse_reply("{}")
    assert exc.value.code == "LLM_BAD_RESPONSE"


def test_parse_reply_truncates_long_title():
    got = cr.parse_reply(json.dumps({"title": "标" * 40}, ensure_ascii=False))
    assert len(got["title"]) == cr.TITLE_MAX


def test_apply_sorts_by_new_score_and_counts_changes():
    rows = [_cand(score=80, title="A"), _cand(start=8000, end=15000, score=40, title="B")]
    packed = cr.apply(rows, [{"score": 30}, {"score": 95, "title": "B 新标题"}])
    assert [r["title"] for r in packed["candidates"]] == ["B 新标题", "A"]
    assert packed["changed"] == 2
    assert packed["candidates"][0]["refined"] is True


def test_apply_never_touches_boundaries():
    rows = [_cand(start=1000, end=6000)]
    packed = cr.apply(rows, [{"score": 99, "start_ms": 0, "end_ms": 99999}])
    row = packed["candidates"][0]
    assert (row["start_ms"], row["end_ms"]) == (1000, 6000), "精析只改分和文案，不许动时间"


def test_apply_skips_unchanged_items():
    packed = cr.apply([_cand(score=60, title="旧标题")], [{"score": 60, "title": "旧标题"}])
    assert packed["changed"] == 0
    assert "refined" not in packed["candidates"][0]


# ---------------------------------------------------------------- 编排分支


def _refine(cands, **kwargs):
    base = dict(
        client=FakeClient('{"score": 90, "title": "新标题", "reason": "画面有信息"}'),
        segments=SEGS,
        ffmpeg=FakeFFmpeg(),
        source_path=__file__,
        work_dir=str(Path(__file__).parent / "_refine_tmp"),
    )
    base.update(kwargs)
    return cr.refine_candidates(cands, **base)


def test_refine_happy_path_rewrites_score_and_title(tmp_path):
    out = _refine([_cand()], work_dir=str(tmp_path))
    assert out["refined"] == 1
    assert out["candidates"][0]["score"] == 90
    assert out["candidates"][0]["title"] == "新标题"


def test_refine_skips_whole_layer_without_vision_client(tmp_path):
    out = _refine([_cand()], client=None, work_dir=str(tmp_path))
    assert out["refined"] == 0
    assert any("没配 vision" in n for n in out["notes"])
    assert out["candidates"][0]["score"] == 60, "跳过时要原样返回，不能顺手抹掉分数"


def test_refine_skips_when_source_missing(tmp_path):
    out = _refine([_cand()], source_path=str(tmp_path / "nope.mp4"), work_dir=str(tmp_path))
    assert out["refined"] == 0
    assert any("找不到源视频" in n for n in out["notes"])


def test_refine_skips_when_ffmpeg_missing(tmp_path):
    out = _refine([_cand()], ffmpeg=None, work_dir=str(tmp_path))
    assert any("FFmpeg 不可用" in n for n in out["notes"])


def test_refine_survives_thumbnail_failure(tmp_path):
    out = _refine([_cand()], ffmpeg=FakeFFmpeg(can_thumb=False), work_dir=str(tmp_path))
    assert out["refined"] == 0
    assert out["failed"] == 1
    assert any("抽不到画面" in n for n in out["notes"])


def test_refine_survives_model_error(tmp_path):
    out = _refine([_cand()], client=FakeClient(error=RuntimeError("boom")), work_dir=str(tmp_path))
    assert out["refined"] == 0
    assert any("精析请求失败" in n for n in out["notes"])


def test_refine_retries_without_temperature(tmp_path):
    client = FakeClient('{"score": 70}', reject_temperature=True)
    out = _refine([_cand()], client=client, work_dir=str(tmp_path))
    assert out["refined"] == 1, "不认 temperature 的客户端要退化成裸调用"


def test_refine_reports_bad_json_per_candidate(tmp_path):
    out = _refine(
        [_cand(), _cand(start=8000, end=15000)],
        client=FakeClient("我觉得还行"),
        work_dir=str(tmp_path),
    )
    assert out["failed"] == 2
    assert out["refined"] == 0
    assert len(out["notes"]) >= 2


def test_refine_only_touches_top_n(tmp_path):
    rows = [_cand(score=90 - i * 5, start=i * 1000, end=i * 1000 + 6000) for i in range(4)]
    out = _refine(rows, topn=2, work_dir=str(tmp_path))
    assert out["refined"] == 2, "只精析 Top N，低分候选不值得再花一次请求"


def test_refine_records_audio_features(tmp_path):
    out = _refine([_cand()], work_dir=str(tmp_path))
    assert out["audio"] == 1, "有音轨时应该真的算一次响度"


def test_refine_honours_cancel(tmp_path):
    with pytest.raises(WBError) as exc:
        _refine([_cand()], should_cancel=lambda: True, work_dir=str(tmp_path))
    assert exc.value.code == "CANCELED"
