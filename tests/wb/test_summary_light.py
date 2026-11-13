"""轻量总结（只输 av/BV 取字幕，或选一份 srt）的判据。

这个功能的价值全在**不下载视频也能总结**，所以判据也围着这条走：

1. **素材来源说得准**：B站 AI 字幕 / 平台人工字幕 / 用户 srt / 本机 ASR 四种，
   提示词里那句「素材类型 + 可信度」必须跟着真实来源变。把平台字幕说成本机 ASR
   不会让总结更差，但会让「素材未覆盖」这句话整体失真。
2. **登录与没字幕必须分得开**：字幕接口对匿名请求恒定返回空列表，和「UP 主没传」
   长得一模一样。混在一起，用户会得到一句「这个视频没有字幕」然后去别处找。
3. **不覆盖已有本地项目**：拿平台字幕顶掉一份已经花过 ASR 时间的转录是破坏性操作。
4. **同一条素材只有一个项目**：重复点两次不许长出两份字幕。

全部离线：假网关喂固定 JSON，不联网、不碰真目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.core import bili_subs, summary_source
from video_workbench.core.resolver import Item, Resolution

# --------------------------------------------------------------------- 假网关


class FakeClient:
    """只实现字幕这条路用到的两个方法。"""

    def __init__(self, payload: dict | None = None):
        self.payload = payload if payload is not None else {}
        self.urls: list[str] = []

    def get_data(self, url: str, params: dict | None = None, signed: bool = False):
        self.urls.append(url)
        value = self.payload.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"测试没有准备 {url} 的返回")
        return value

    def get_json_url(self, url: str):
        self.urls.append(url)
        value = self.payload.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"测试没有准备 {url} 的返回")
        return value

    def is_logged_in(self) -> bool:
        return True


class FakeGateway:
    """形状对齐 core/bili.py 的 BiliGateway：登录态 + get_data + client()。"""

    def __init__(self, payload: dict | None = None, *, logged_in: bool = True):
        self._client = FakeClient(payload)
        self._logged_in = logged_in
        self.calls: list[tuple] = []

    def client(self):
        return self._client

    def is_logged_in(self) -> bool:
        return self._logged_in

    def get_data(self, url, params=None, action="请求", *, signed=False):
        self.calls.append((url, dict(params or {}), action, signed))
        return self._client.get_data(url, params, signed=signed)


def player_payload(*tracks: dict) -> dict:
    return {"subtitle": {"subtitles": list(tracks)}}


def track(lan: str, url: str, doc: str = "") -> dict:
    return {"lan": lan, "lan_doc": doc or lan, "subtitle_url": url}


def json3(*pairs: tuple[float, str]) -> dict:
    return {"body": [{"from": start, "to": start + 1.5, "content": text} for start, text in pairs]}


PLAYER = "https://api.bilibili.com/x/player/wbi/v2"


# ------------------------------------------------------------------ 来源词表


def test_the_four_origins_and_the_default():
    assert summary_source.ORIGINS == ("asr", "bili_ai", "bili_human", "user_srt")
    assert summary_source.DEFAULT_ORIGIN == "asr"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bili_ai", "bili_ai"),
        ("  BILI_AI  ", "bili_ai"),
        ("user_srt", "user_srt"),
        ("", "asr"),
        (None, "asr"),
        ("something-else", "asr"),
    ],
)
def test_normalize_origin_falls_back_to_the_default(raw, expected):
    assert summary_source.normalize_origin(raw) == expected


def test_read_origin_survives_a_missing_or_broken_file(tmp_path: Path):
    assert summary_source.read_origin(tmp_path) == "asr", "目录里没有 transcript.json"
    (tmp_path / "transcript.json").write_text("{ 这不是 json", encoding="utf-8")
    assert summary_source.read_origin(tmp_path) == "asr", "坏文件不该让总结跑不动"
    (tmp_path / "transcript.json").write_text(
        json.dumps({"origin": "bili_human"}), encoding="utf-8"
    )
    assert summary_source.read_origin(tmp_path) == "bili_human"
    assert summary_source.origin_of({"root_dir": str(tmp_path)}) == "bili_human"
    assert summary_source.origin_of(None) == "asr"


def test_bundle_meta_tells_the_four_origins_apart():
    kinds = {}
    for origin in summary_source.ORIGINS:
        kind, label, trust, detail = summary_source.bundle_meta(origin, segments=12)
        kinds[origin] = kind
        assert "12 段" in detail, (origin, detail)
        assert "时间戳" in detail, (origin, detail)
        assert label and trust
    assert kinds["asr"] == "subtitle_ai"
    assert kinds["bili_ai"] == "subtitle_ai"
    assert kinds["bili_human"] == "subtitle_human"
    assert kinds["user_srt"] == "subtitle_user"


def test_only_the_local_asr_label_avoids_claiming_platform_origin():
    _, asr_label, _, asr_detail = summary_source.bundle_meta("asr", segments=3)
    _, ai_label, _, ai_detail = summary_source.bundle_meta("bili_ai", segments=3)
    assert "本机" in asr_detail
    assert "B站" not in asr_label, f"本机 ASR 不许自称平台字幕：{asr_label}"
    assert "B站" in ai_detail


def test_danmaku_is_mentioned_only_when_there_is_some():
    _, _, _, without = summary_source.bundle_meta("bili_ai", segments=3)
    _, _, _, with_hot = summary_source.bundle_meta("bili_ai", segments=3, danmaku=7)
    assert "弹幕" not in without, "没有弹幕时不许说「另附弹幕 0 条」"
    assert "7 条" in with_hot


# ------------------------------------------------------------------ srt 那条路


SRT = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:03,200 --> 00:00:05,000
第二句
"""


def test_srt_becomes_segments_without_the_15_second_merge():
    segments, warnings, encoding = summary_source.segments_from_srt(SRT.encode("utf-8"))
    assert encoding.lower().startswith("utf")
    assert [s["text"] for s in segments] == ["第一句", "第二句"], "用户 srt 的断句不许再合并"
    assert segments[0]["start_ms"] == 1000
    assert segments[0]["end_ms"] == 3200, "结束时间取下一句的起点"
    assert segments[-1]["end_ms"] > segments[-1]["start_ms"]
    assert warnings == []


def test_srt_that_is_out_of_order_is_sorted_and_says_so():
    raw = """1
00:00:09,000 --> 00:00:10,000
后面那句

2
00:00:01,000 --> 00:00:02,000
前面那句
"""
    segments, warnings, _ = summary_source.segments_from_srt(raw.encode("utf-8"))
    assert [s["text"] for s in segments] == ["前面那句", "后面那句"]
    assert any("排序" in w for w in warnings), warnings


def test_a_broken_srt_file_reports_validation_not_a_traceback():
    with pytest.raises(WBError) as box:
        summary_source.segments_from_srt(b"\xff\xfe\xe4\xb8\x8d")
    assert box.value.code == "VALIDATION"


def test_srt_whose_timeline_is_longer_than_the_video_gets_a_warning():
    segments, warnings, _ = summary_source.segments_from_srt(SRT.encode("utf-8"), duration_ms=2000)
    assert segments
    assert warnings, "字幕比视频还长时必须提醒：多半是选错了文件"


# -------------------------------------------------------------- B站字幕那条路


def test_listing_tracks_without_login_says_it_needs_login_not_no_subtitles():
    tracks, note = bili_subs.list_tracks(FakeGateway({}, logged_in=False), 1, 2)
    assert tracks == []
    assert "登录" in note
    assert "没有字幕" not in note, "未登录不许说成「这个视频没字幕」"


def test_listing_tracks_reports_what_the_video_has():
    gateway = FakeGateway({PLAYER: player_payload(track("ai-zh", "https://x/ai.json"))})
    tracks, note = bili_subs.list_tracks(gateway, 170001, 33)
    assert note == ""
    assert tracks == [{"lan": "ai-zh", "lan_doc": "ai-zh", "is_ai": True}]
    _, params, _, signed = gateway.calls[0]
    assert params["aid"] == 170001 and params["cid"] == 33
    assert signed is True, "字幕接口必须走 WBI 签名，否则稳定返回空列表"


def test_a_video_without_any_track_says_so_plainly():
    tracks, note = bili_subs.list_tracks(FakeGateway({PLAYER: player_payload()}), 1, 2)
    assert tracks == []
    assert "没有字幕" in note


def test_fetching_an_ai_track_marks_the_origin_as_platform_ai():
    gateway = FakeGateway(
        {
            PLAYER: player_payload(track("ai-zh", "https://x/ai.json", "中文（AI生成）")),
            "https://x/ai.json": json3((0.0, "开头"), (2.0, "中间"), (30.0, "很后面")),
        }
    )
    got = bili_subs.fetch(gateway, 1, 2, ["zh-CN", "ai-zh"])
    assert got["origin"] == "bili_ai"
    assert got["is_ai"] is True
    assert got["lang_doc"] == "中文（AI生成）"
    assert got["raw_segments"] == 3
    assert got["segments"][0]["text"].startswith("开头")


def test_fetching_a_human_track_marks_the_origin_as_platform_human():
    gateway = FakeGateway(
        {
            PLAYER: player_payload(
                track("zh-CN", "https://x/zh.json"), track("ai-zh", "https://x/a")
            ),
            "https://x/zh.json": json3((0.0, "人工字幕第一句")),
        }
    )
    got = bili_subs.fetch(gateway, 1, 2, ["zh-CN", "ai-zh"])
    assert got["origin"] == "bili_human"
    assert got["is_ai"] is False


def test_fetching_without_login_raises_auth_required():
    with pytest.raises(WBError) as box:
        bili_subs.fetch(FakeGateway({}, logged_in=False), 1, 2, [])
    assert box.value.code == "AUTH_REQUIRED"


def test_fetching_from_a_video_without_tracks_raises_conflict():
    with pytest.raises(WBError) as box:
        bili_subs.fetch(FakeGateway({PLAYER: player_payload()}), 1, 2, [])
    assert box.value.code == "CONFLICT"


# ------------------------------------------------------------- service 层（RPC）


class FakeResolver:
    """解析结果固定：一条 B站视频，bvid/cid/aid 都由用例给。"""

    def __init__(
        self, bvid="BV1light411c", aid=170001, cid=33, title="轻量样本", duration_ms=742000
    ):
        self.bvid = bvid
        self.aid = aid
        self.cid = cid
        self.title = title
        self.duration_ms = duration_ms
        self.raws: list[str] = []

    def resolve(self, raw: str) -> Resolution:
        self.raws.append(raw)
        return Resolution(
            kind="single",
            title=self.title,
            up_name="示例UP",
            up_mid=170001,
            collection_title="",
            raw=raw,
            items=[
                Item(
                    bvid=self.bvid,
                    aid=self.aid,
                    cid=self.cid,
                    page_no=1,
                    title=self.title,
                    duration_ms=self.duration_ms,
                )
            ],
        )


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]


def wire(service, *, logged_in=True, payload=None):
    service._resolver = FakeResolver()
    service._gateway = FakeGateway(
        payload
        if payload is not None
        else {
            PLAYER: player_payload(track("ai-zh", "https://x/ai.json", "中文（AI生成）")),
            "https://x/ai.json": json3((0.0, "第一段"), (20.0, "第二段")),
        },
        logged_in=logged_in,
    )
    return service


def test_probe_lists_tracks_and_does_not_create_a_project(service):
    wire(service)
    before = len(service.repo.project_list(limit=200))

    data = rpc(service, "summary.light.probe", {"url": "BV1light411c"})

    assert data["bvid"] == "BV1light411c"
    assert data["aid"] == 170001 and data["cid"] == 33
    assert data["logged_in"] is True
    assert [item["lan"] for item in data["subtitles"]] == ["ai-zh"]
    assert data["note"] == ""
    after = len(service.repo.project_list(limit=200))
    assert after == before, "解析这一步不许建项目"


def test_probe_without_login_still_answers(service):
    wire(service, logged_in=False)

    data = rpc(service, "summary.light.probe", {"url": "BV1light411c"})

    assert data["logged_in"] is False
    assert data["subtitles"] == []
    assert "登录" in data["note"]


def test_probe_without_a_url_is_a_validation_error(service):
    error = err(service, "summary.light.probe", {"url": "   "})
    assert error["code"] == "VALIDATION"


def test_prepare_writes_the_subtitles_and_marks_the_origin(service):
    wire(service)

    data = rpc(service, "summary.light.prepare", {"url": "BV1light411c"})

    assert data["origin"] == "bili_ai"
    assert data["segments"] >= 1
    project = service.repo.project_get(data["project_id"])
    assert project["source_kind"] == "bili_light"
    assert project["bvid"] == "BV1light411c"
    assert project["cid"] == 33
    assert project["stage"] == "transcribed"
    root = Path(project["root_dir"])
    assert (root / "transcript.json").is_file(), "页面靠这个文件判 has_transcript"
    assert (root / "transcript.srt").is_file()
    saved = json.loads((root / "transcript.json").read_text(encoding="utf-8"))
    assert saved["origin"] == "bili_ai"
    assert saved["segments"]
    assert (root / "source.wb.json").is_file(), "写了侧车，之后真下载才复用同一目录"


def test_prepare_twice_reuses_the_same_project(service):
    wire(service)

    first = rpc(service, "summary.light.prepare", {"url": "BV1light411c"})
    second = rpc(service, "summary.light.prepare", {"url": "BV1light411c"})

    assert second["project_id"] == first["project_id"], "同一条视频不许长出两个项目"
    assert second["reused"] is True
    same = [p for p in service.repo.project_list(limit=200) if p["bvid"] == "BV1light411c"]
    assert len(same) == 1, "同一条视频不许长出两个项目"


def test_prepare_from_a_local_srt_never_touches_bilibili(service):
    wire(service, logged_in=False)
    srt_path = Path(service.cfg.media_root).parent / "sub.srt"
    srt_path.write_text(SRT, encoding="utf-8")

    data = rpc(service, "summary.light.prepare", {"url": "", "path": str(srt_path)})

    assert data["origin"] == "user_srt"
    assert service._gateway.calls == [], "选了本地文件就不该请求 B站"
    project = service.repo.project_get(data["project_id"])
    assert project["source_kind"] == "local"
    assert project["bvid"] in (None, ""), "没有 bvid 就如实留空"
    saved = json.loads((Path(project["root_dir"]) / "transcript.json").read_text(encoding="utf-8"))
    assert saved["origin"] == "user_srt"


def test_prepare_from_the_same_srt_twice_reuses_the_project(service):
    wire(service)
    srt_path = Path(service.cfg.media_root).parent / "sub2.srt"
    srt_path.write_text(SRT, encoding="utf-8")

    first = rpc(service, "summary.light.prepare", {"path": str(srt_path)})
    second = rpc(service, "summary.light.prepare", {"path": str(srt_path)})

    assert second["project_id"] == first["project_id"]
    assert second["reused"] is True


def test_prepare_from_a_missing_srt_reports_io(service):
    wire(service)
    error = err(
        service, "summary.light.prepare", {"path": str(Path(service.cfg.media_root) / "nope.srt")}
    )
    assert error["code"] == "IO"


def test_prepare_does_not_overwrite_an_existing_downloaded_project(service, tmp_path):
    """已有本地项目 + 已有字幕 ⇒ 不抓取、不覆盖，直接用它自己的。"""
    root = tmp_path / "media" / "示例UP" / "轻量样本"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="轻量样本",
        bvid="BV1light411c",
        aid=170001,
        cid=33,
        root_dir=str(root),
        stage="transcribed",
    )
    original = [{"id": "s1", "start_ms": 0, "end_ms": 900, "text": "本机转录的原句", "words": []}]
    service.repo.transcript_upsert(project["id"], segments=original, language="zh", model="tiny")
    (root / "transcript.json").write_text(
        json.dumps({"segments": original, "origin": "asr"}, ensure_ascii=False), encoding="utf-8"
    )
    wire(service)
    before_urls = list(service._gateway.client().urls)

    data = rpc(service, "summary.light.prepare", {"url": "BV1light411c"})

    assert data["reused"] is True
    assert data["origin"] == "asr"
    # 判据钉在**下载字幕正文**这一步，不是「一次网络都不许发」：probe 列轨是只读的
    # 轻请求（用户点「解析」时本来就会发一次），真正贵且会覆盖字幕的是 get_json_url。
    assert "https://x/ai.json" not in service._gateway.client().urls[before_urls.__len__() :], (
        "已有本地项目时不许下载平台字幕正文"
    )
    saved = json.loads((root / "transcript.json").read_text(encoding="utf-8"))
    assert saved["segments"] == original, "不许拿平台字幕顶掉已花过 ASR 时间的转录"
    assert saved["origin"] == "asr"


def test_the_summary_bundle_carries_the_light_origin(service):
    """来源一路透传到 Bundle：B站 AI 字幕的 detail 里必须是「B站 AI 字幕」。"""
    wire(service)
    data = rpc(service, "summary.light.prepare", {"url": "BV1light411c"})
    project = service.repo.project_get(data["project_id"])
    row = service.repo.transcript_get(project["id"])

    from video_workbench.core import summarizer as wb_summarizer

    bundle = wb_summarizer.build_bundle(
        project, row["segments"], origin=summary_source.origin_of(project)
    )

    assert bundle.source_kind == "subtitle_ai"
    assert "B站 AI 字幕" in bundle.source_detail
    assert "本机 ASR" not in bundle.source_detail


def test_storing_subtitles_without_an_origin_keeps_the_one_on_disk(service, tmp_path):
    """用户编辑/优化/翻译之后来源不变——把 bili_ai 写回 asr 会让可信度说明失真。"""
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(title="标题", root_dir=str(root), stage="transcribed")
    items = [{"id": "s1", "start_ms": 0, "end_ms": 900, "text": "第一句", "words": []}]
    service._store_subtitles(project["id"], items, origin="bili_human")

    edited = [{"id": "s1", "start_ms": 0, "end_ms": 900, "text": "改过的第一句", "words": []}]
    service._store_subtitles(project["id"], edited)

    saved = json.loads((root / "transcript.json").read_text(encoding="utf-8"))
    assert saved["origin"] == "bili_human", "没给 origin 时必须沿用盘上那份"
    assert saved["segments"][0]["text"] == "改过的第一句"
