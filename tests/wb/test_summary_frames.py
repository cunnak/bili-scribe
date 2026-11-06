"""画面帧接进总结链路（task-70）的判据。

三件事必须钉住，缺一条这个功能就是"接上了但没人知道它接上了"：

1. **本地抽帧腿产出的是副本吃得下的形状**：FrameSet + as_content_blocks 的块序列，
   帧宽/时刻/notes 都对得上。WB 不调 bs 的 collect_frames（那条腿要 aid，而库里
   32 个项目 aid 全空），所以"形状同源"这件事必须自己证明一次。
2. **帧只进最终归约那一次请求**：副本的 frameset 接缝本来就是这个语义
   （core/summary/summarizer.py 里 as_content_blocks 上面那段注释），但接缝在副本里、
   入口在适配层里——**接线的人可能把它接到每一条腿上去**。所以这里从适配层入口
   （run / refine）往下看，逐条请求断 content 的类型。
3. **补救腿不带图**：_json_or_retry / _split_reply / _continue / _timeline_reply
   全是 list(messages) + [...]，照原样发会把同一批图再传一遍（12 帧≈1,464 image_tokens
   翻倍）。_text_only 就是为这条存在的，它没有自己的用例就等于没有牙。

另有一条**降级**口径（服务层）：抽不出来 / 没源视频 / 开关关着，总结照跑、日志留话，
不许让"画面是加分项"变成"画面失败整条任务失败"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_summary import GOOD, StubClient, make_project, run_job
from test_summary_parity import FakeLLM, QuietCtx, make_cfg, three_pass_script
from wb_parity_tiers import loose_env as loose_env  # noqa: F401  档位 fixture 由 pytest 注入

from bili_scribe.core import frames as bs_frames
from video_workbench.config import FramesConfig
from video_workbench.core import frames as frames_core


class RecordingFFmpeg:
    """记下每一次 thumbnail 的入参，并按需真写一个字节。

    为什么不复用 test_clip_refine 的 FakeFFmpeg：那边不记 width/quality，而本文件要钉
    "帧宽 480、quality 经 quality_to_qv 换算后传下去"。自己带一个记录版，比去改那边
    （它守的是精析 3 帧）安全。
    """

    def __init__(self, *, can_thumb=True, duration_ms=60_000):
        self.available = True
        self.can_thumb = can_thumb
        self.duration_ms = duration_ms
        self.calls: list[dict] = []
        self.thumbs = 0
        self.widths: list[int] = []

    def require(self):
        return "ffmpeg", "ffprobe"

    def probe(self, src):
        return type("Info", (), {"duration_ms": self.duration_ms})()

    def thumbnail(self, src, dest, *, at=1.0, width=480, quality=4):
        self.calls.append({"at": at, "width": width, "quality": quality})
        self.widths.append(width)
        if not self.can_thumb:
            raise RuntimeError("抽帧失败")
        self.thumbs += 1
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\xff\xd8fakejpeg\xff\xd9")
        return out


def make_frameset(count: int = 2) -> bs_frames.FrameSet:
    """造一组最小帧（真 data URL，形状与本地腿产出的完全一致）。"""
    return bs_frames.FrameSet(
        frames=[
            bs_frames.Frame(
                order=index + 1,
                timestamp=index * 60,
                data_url="data:image/jpeg;base64,QUJD",
                size=3,
            )
            for index in range(count)
        ],
        total_cells=count,
        sprites=0,
        has_timestamps=True,
        notes=["以下画面由本地 ffmpeg 从源视频等距抽取。"],
    )


PROJECT = {
    "title": "画面素材",
    "up_name": "UP画面",
    "bvid": "BV1framescheck",
    "duration_sec": 600,
    "desc": "固定简介",
}
SEGMENTS = [{"start_ms": 0, "text": "开场"}]


def adapter_run(client, cfg, frameset):
    from video_workbench.core import summarizer as adapter

    return adapter.run(
        dict(PROJECT),
        list(SEGMENTS),
        client=client,
        cfg=cfg,
        ctx=QuietCtx(),
        role="wengao",
        frameset=frameset,
    )


def adapter_refine(client, cfg, frameset):
    from video_workbench.core import summarizer as adapter

    previous = {
        "headline": "旧标题",
        "synopsis": "旧梗概",
        "key_points": ["旧要点"],
        "highlights": [],
        "conclusions": [],
        "faq": [],
        "timeline": [],
        "word": 100,
        "role_key": "wengao",
        "version_id": 1,
    }
    return adapter.refine(
        dict(PROJECT),
        list(SEGMENTS),
        previous,
        "把语气改得更口语一点",
        client=client,
        cfg=cfg,
        ctx=QuietCtx(),
        role="wengao",
        frameset=frameset,
    )


def last_content(messages):
    return list(messages)[-1]["content"]


# --------------------------------------------------------------- 本地抽帧腿


def touch_source(tmp_path: Path) -> Path:
    """collect 会先查 src.is_file()（与真实链路一致），所以夹具得真造出这个文件。"""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not a real video, the fake ffmpeg does not care")
    return source


def test_collect_produces_a_frameset_the_copy_can_consume(tmp_path: Path):
    """形状锁：产出的 FrameSet 交给副本那句 as_content_blocks 必须真的变成块序列。"""
    cfg = FramesConfig(enabled=True, max_frames=12, quality=85)
    fake = RecordingFFmpeg()
    frameset = frames_core.collect(
        fake, touch_source(tmp_path), cfg, out_dir=tmp_path / "frames", duration_ms=60_000
    )

    assert fake.thumbs == 12, f"计划 12 帧就该调 12 次 thumbnail，实得 {fake.thumbs}"
    assert len(frameset.frames) == 12
    assert frameset.has_timestamps is True
    assert frameset.total_bytes > 0
    for frame in frameset.frames:
        assert frame.data_url.startswith("data:image/jpeg;base64,"), frame.data_url[:40]
    stamps = [frame.timestamp for frame in frameset.frames]
    assert stamps == sorted(stamps) and len(set(stamps)) == 12, f"时刻必须严格递增：{stamps}"
    assert stamps[0] > 0 and stamps[-1] < 60, f"首尾端点必须被避开：{stamps}"

    blocks = bs_frames.as_content_blocks(frameset)
    assert len(blocks) == 1 + 2 * 12, f"块序列应为 1 段说明 + 每帧 2 块，实得 {len(blocks)}"
    assert blocks[0]["type"] == "text" and "参考材料" in blocks[0]["text"]
    assert blocks[1]["type"] == "text" and blocks[2]["type"] == "image_url"
    # 帧宽与画质必须真的传下去了：480 与 bs 原生单格一致，quality 走 quality_to_qv 换算。
    assert fake.widths == [frames_core.FRAME_WIDTH] * 12
    assert [call["quality"] for call in fake.calls] == [frames_core.quality_to_qv(85)] * 12


def test_collect_cleans_its_own_subdir_but_keeps_the_cover_thumb(tmp_path: Path):
    """谁建谁删，而且只删自己建的那个：frames/thumb.jpg（封面）必须活下来。"""
    out_dir = tmp_path / "frames"
    out_dir.mkdir(parents=True)
    thumb = out_dir / "thumb.jpg"
    thumb.write_bytes(b"\xff\xd8cover\xff\xd9")

    cfg = FramesConfig(enabled=True, max_frames=3, quality=85)
    frames_core.collect(
        RecordingFFmpeg(), touch_source(tmp_path), cfg, out_dir=out_dir, duration_ms=30_000
    )

    assert thumb.is_file(), "封面缩略图被抽帧的清理顺手删了——整目录 rmtree 就是这个后果"
    leftovers = [path.name for path in out_dir.iterdir() if path.name.startswith("summary-")]
    assert leftovers == [], f"临时子目录没收干净：{leftovers}"


def test_collect_raises_frame_error_when_nothing_comes_out(tmp_path: Path):
    """全失败抛 FrameError（调用方据此降级），不是返回空 FrameSet 混过去。"""
    cfg = FramesConfig(enabled=True, max_frames=3, quality=85)
    with pytest.raises(bs_frames.FrameError):
        frames_core.collect(
            RecordingFFmpeg(can_thumb=False),
            touch_source(tmp_path),
            cfg,
            out_dir=tmp_path / "frames",
            duration_ms=30_000,
        )


def test_collect_refuses_before_touching_the_disk_when_switched_off(tmp_path: Path):
    """开关关着时连目录都不该建——"关掉可明显提速"这句话得有个能验的实现。"""
    cfg = FramesConfig(enabled=False, max_frames=12, quality=85)
    out_dir = tmp_path / "frames"
    with pytest.raises(bs_frames.FrameError, match="未启用"):
        frames_core.collect(
            RecordingFFmpeg(), touch_source(tmp_path), cfg, out_dir=out_dir, duration_ms=60_000
        )
    assert not out_dir.exists(), "开关关着还建了 frames/ 目录"


def test_quality_maps_the_bili_scribe_scale_onto_ffmpeg_qv():
    """配置沿用 bs 的 Pillow 口径（1–100，100 最好），ffmpeg 的 -q:v 是 2 最好。

    不换算就是"用户调高画质、结果更糊"。这里钉两端与默认值，中间线性由公式本身保证。
    """
    assert frames_core.quality_to_qv(100) == frames_core.QV_BEST
    assert frames_core.quality_to_qv(1) == frames_core.QV_WORST
    assert frames_core.quality_to_qv(85) == 6, "85 是 bs 的默认档，落在 -q:v 6"
    # 0/None 当"没填"（回落到 85），越界向上夹紧；配置层已经拦过一次，这里防的是别处直接调它。
    assert frames_core.quality_to_qv(0) == frames_core.quality_to_qv(85), "0 应当当没填处理"
    assert frames_core.quality_to_qv(999) == frames_core.QV_BEST


# ------------------------------------------------------- 适配层：帧进哪一条请求


def test_the_adapter_sends_frames_only_on_the_reduction_request():
    """run 带帧时，只有最终归约那一次是块列表；批判与修订两轮必须仍是纯文本。

    这条守的是**接线位置**：副本的接缝只有一处，有人把它接到每一条腿上的时候，
    这条会红在"第 2/3 次请求带图"上。
    """
    cfg = make_cfg()
    client = FakeLLM(three_pass_script())
    adapter_run(client, cfg, frameset=make_frameset(2))
    assert len(client.requests) == 3, f"wengao 三轮应发 3 次，实得 {len(client.requests)}"
    assert isinstance(last_content(client.requests[0]["messages"]), list), "归约那一次没带画面块"
    for index in (1, 2):
        assert isinstance(last_content(client.requests[index]["messages"]), str), (
            f"第 {index + 1} 次请求也带上了画面块——帧只该进最终归约那一次"
        )


def test_refine_also_carries_frames():
    """修订版必须重新带帧：只给文字，上一版里只有画面才有的信息会悄悄丢掉。"""
    cfg = make_cfg()
    client = FakeLLM(three_pass_script())
    adapter_refine(client, cfg, frameset=make_frameset(2))
    assert isinstance(last_content(client.requests[0]["messages"]), list), (
        "refine 的归约那一次没带画面块"
    )


def test_the_rescue_legs_drop_the_frames(loose_env):
    """补救腿不许把同一批图再传一遍（宽松档才会走到补救腿）。

    现场：主请求带帧、模型回了一段没法修的垃圾 ⇒ 分批正文 + 补时间线两条腿各发一次，
    之后批判与修订再各发一次。那四条全是 list(messages) + [...]，不降级就会把帧
    再传四遍（12 帧≈1,464 image_tokens，四遍就是 5.8k 白烧）。
    """
    cfg = make_cfg()
    # 30 条时间线：_timeline_target 对 10 分钟片长要 20 条，给够了才不会被 _finalize 再要一轮。
    timeline = {
        "timeline": [{"t": f"{hour:02d}:00", "text": f"第{hour}段"} for hour in range(1, 31)]
    }
    body = {
        "one_line": "一行",
        "summary_md": "## 正文\n分批拿到的那一份。",
        "key_points": ["一", "二"],
        "caution": "",
        "comment_short": "一句话。",
    }
    final = {**body, "timeline": timeline["timeline"]}
    client = FakeLLM(["这不是 JSON", body, timeline, "批判意见", final])
    adapter_run(client, cfg, frameset=make_frameset(3))

    assert len(client.requests) >= 5, (
        f"补救腿没跑起来（只发了 {len(client.requests)} 次），这条会变空断言"
    )
    assert isinstance(last_content(client.requests[0]["messages"]), list), (
        "主请求没带画面块，前提不成立"
    )
    for index, request in enumerate(client.requests[1:], start=2):
        for offset, message in enumerate(request["messages"], start=1):
            assert isinstance(message["content"], str), (
                f"第 {index} 次请求（补救腿/批判/修订）第 {offset} 条消息仍是块列表——"
                "同一批图被再传了一遍，见 video_workbench/core/summarizer.py 的 _text_only"
            )


# --------------------------------------------------------- 服务层：抽不到就降级


def attach_source(service, project, tmp_path: Path) -> Path:
    source = Path(project["root_dir"]) / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"not a real video, the fake ffmpeg does not care")
    service.repo.asset_upsert_singleton(
        project["id"], "source", str(source), bytes=2048, duration_ms=120_000
    )
    return source


def summary_content(service, result):
    row = service.repo.summary_get(result["summary_id"]) or {}
    return dict(row.get("content") or {})


def test_the_service_attaches_frames_and_reports_them(service, monkeypatch, tmp_path: Path):
    """开关开着 + 源视频在盘 ⇒ 帧真的进了请求，Summary.frames 记着张数。"""
    project = make_project(service, tmp_path)
    attach_source(service, project, tmp_path)
    fake = RecordingFFmpeg(duration_ms=120_000)
    monkeypatch.setattr(service, "_ffmpeg", fake)

    client = StubClient([GOOD, "批判意见", GOOD])
    result, ctx = run_job(service, monkeypatch, client, {"kind": "run"}, project)

    content = summary_content(service, result)
    assert content.get("frames") == 12, f"帧数没进总结：{content.get('frames')}"
    assert fake.thumbs == 12, f"抽帧次数不对：{fake.thumbs}"
    assert isinstance(last_content(client.calls[0]), list), "归约那次请求没带画面块"
    for index in (1, 2):
        assert isinstance(last_content(client.calls[index]), str), (
            f"第 {index + 1} 次请求（批判/修订）也带了画面块"
        )
    assert any("画面就绪" in line for line in ctx.logs), f"没有进度话术：{ctx.logs}"
    assert any(stage == "抽画面帧" for _, stage in ctx.steps), f"没有抽帧阶段：{ctx.steps}"


def test_the_service_degrades_when_there_is_no_source_video(service, monkeypatch, tmp_path: Path):
    """没有源视频：总结照跑、日志留一句，不许抛。"""
    project = make_project(service, tmp_path)
    client = StubClient([GOOD, "批判意见", GOOD])
    result, ctx = run_job(service, monkeypatch, client, {"kind": "run"}, project)

    assert summary_content(service, result).get("frames") == 0
    assert any("未附画面" in line and "源视频" in line for line in ctx.logs), ctx.logs
    assert not any("封面" in line for line in ctx.logs), (
        f"把封面那句文案漏进总结了，用户会以为总结因为封面失败：{ctx.logs}"
    )


def test_the_service_degrades_when_the_frame_switch_is_off(service, monkeypatch, tmp_path: Path):
    """开关关着：不抽帧，但总结照跑。"""
    project = make_project(service, tmp_path)
    attach_source(service, project, tmp_path)
    service.cfg.collect.frames.enabled = False
    fake = RecordingFFmpeg(duration_ms=120_000)
    monkeypatch.setattr(service, "_ffmpeg", fake)

    client = StubClient([GOOD, "批判意见", GOOD])
    result, ctx = run_job(service, monkeypatch, client, {"kind": "run"}, project)

    assert fake.thumbs == 0, "开关关着还调了 thumbnail"
    assert summary_content(service, result).get("frames") == 0
    assert any("画面帧已关" in line for line in ctx.logs), ctx.logs


def test_the_service_degrades_when_the_local_ffmpeg_gives_up(service, monkeypatch, tmp_path: Path):
    """抽帧全失败（FrameError）：降级成无画面，不许让整条总结失败。"""
    project = make_project(service, tmp_path)
    attach_source(service, project, tmp_path)
    monkeypatch.setattr(service, "_ffmpeg", RecordingFFmpeg(can_thumb=False, duration_ms=120_000))

    client = StubClient([GOOD, "批判意见", GOOD])
    result, ctx = run_job(service, monkeypatch, client, {"kind": "run"}, project)

    assert summary_content(service, result).get("frames") == 0
    assert any("未附画面" in line for line in ctx.logs), ctx.logs


# ------------------------------------------------------------------ 设置页文案


def test_the_settings_help_no_longer_promises_a_speedup_that_never_happened():
    """settings.js 那句"关掉可明显提速"是接帧之前的空头支票，必须换掉。

    接上之后这句话要么成真、要么改写——留着它而代码里没有消费者，就是界面在骗人。
    这条只钉"不再骗人"与"说清了代价"，不钉具体措辞（措辞会随文案调整变）。
    """
    settings_js = (
        Path(__file__).resolve().parents[2]
        / "video_workbench"
        / "web"
        / "js"
        / "pages"
        / "settings.js"
    )
    text = settings_js.read_text(encoding="utf-8")
    assert "关掉可明显提速" not in text, "假开关的旧文案还在，用户会以为关掉只是变快"
    assert "图片 token" in text or "image token" in text, "没告诉用户关掉省的是什么"
    assert "collect.frames.enabled" in text
