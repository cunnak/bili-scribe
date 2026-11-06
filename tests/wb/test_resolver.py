"""P1-01 解析器测试。全部离线：用假网关喂固定 JSON。

为什么要为解析写这么多用例：解析是整个下载链路的入口，它错一次，后面
下载、转录、切片、总结全部白干。而且这些分支（单稿/多P/合集/番剧）在真实
接口里长得完全不一样，靠手点是点不全的。
"""

from __future__ import annotations

import pytest

from bili_scribe.infra.bili_client import BiliError
from video_workbench.app.errors import WBError
from video_workbench.core.bili import BiliGateway, TtlCache
from video_workbench.core.resolver import (
    KIND_BANGUMI,
    KIND_COLLECTION,
    KIND_MULTI_P,
    KIND_SINGLE,
    PGC_SEASON,
    VIDEO_VIEW,
    Resolver,
    identify,
)

# --------------------------------------------------------------------- 假网关


class FakeClient:
    """只实现解析会用到的两个方法。记录每次调用的 URL，用来验证缓存。"""

    def __init__(self, responses: dict[str, object], expand_to: str = ""):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.expand_to = expand_to

    def get_data(self, url: str, params: dict | None = None):
        self.calls.append((url, dict(params or {})))
        value = self.responses.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"测试没有准备 {url} 的返回")
        return value

    def expand_short_link(self, url: str) -> str:
        self.calls.append(("expand", {"url": url}))
        return self.expand_to or "https://www.bilibili.com/video/BV1xx411c7mD"

    def is_logged_in(self) -> bool:
        return True


def make_resolver(
    responses: dict[str, object], *, expand_to: str = "", cache: TtlCache | None = None
):
    client = FakeClient(responses, expand_to=expand_to)
    # 注意用 is not None：TtlCache 定义了 __len__，空缓存在布尔语境里是 False，
    # 写成 `cache or TtlCache()` 会把注入的缓存悄悄换掉（这条注释是被这个坑咬过写的）。
    gw = BiliGateway(lambda: client, cache=cache if cache is not None else TtlCache())
    return Resolver(gw), client, gw


SINGLE_VIEW = {
    "bvid": "BV1xx411c7mD",
    "aid": 170001,
    "cid": 279786,
    "title": "线性代数没速通（第一讲）",
    "duration": 2701,
    "pic": "https://i0.hdslb.com/cover.jpg",
    "owner": {"mid": 946974, "name": "示例UP"},
    "pages": [{"cid": 279786, "page": 1, "part": "第一讲", "duration": 2701}],
}

MULTI_VIEW = {
    "bvid": "BV1yy411c7mE",
    "aid": 170002,
    "title": "三十分钟讲透注意力机制",
    "pic": "",
    "owner": {"mid": 1, "name": "示例UP"},
    "pages": [
        {"cid": 11, "page": 1, "part": "注意力是什么", "duration": 600},
        {"cid": 12, "page": 2, "part": "自注意力的矩阵形状", "duration": 900},
        {"cid": 13, "page": 3, "part": "多头到底多在哪", "duration": 300},
    ],
}

COLLECTION_VIEW = {
    "bvid": "BV1aa411c7mF",
    "aid": 170003,
    "title": "合集中的某一集",
    "pic": "",
    "owner": {"mid": 2, "name": "示例UP"},
    "pages": [{"cid": 21, "page": 1, "part": "", "duration": 500}],
    "ugc_season": {
        "id": 999,
        "title": "从零手搓一个视频工作台",
        "sections": [
            {
                "episodes": [
                    {
                        "bvid": "BV1aa411c7mF",
                        "aid": 170003,
                        "cid": 21,
                        "title": "1",
                        "duration": 500,
                        "arc": {"title": "第一集 结构", "pic": "", "duration": 500, "aid": 170003},
                    },
                    {
                        "bvid": "BV1bb411c7mG",
                        "aid": 170004,
                        "cid": 22,
                        "title": "2",
                        "duration": 700,
                        "arc": {"title": "第二集 通道", "pic": "", "duration": 700, "aid": 170004},
                    },
                ]
            }
        ],
    },
}

SEASON_DATA = {
    "season_id": 4321,
    "title": "某部番",
    "cover": "https://i0.hdslb.com/bangumi.jpg",
    "up_info": {"uname": "番剧出品方"},
    "episodes": [
        {
            "id": 1001,
            "bvid": "BV1ep411c7m1",
            "aid": 2001,
            "cid": 3001,
            "title": "第1话",
            "long_title": "起风了",
            "duration": 1440,
        },
        {
            "id": 1002,
            "bvid": "BV1ep411c7m2",
            "aid": 2002,
            "cid": 3002,
            "title": "第2话",
            "long_title": "下雨了",
            "duration": 1440,
        },
    ],
}


# --------------------------------------------------------------------- identify


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("BV1xx411c7mD", ("bv", "BV1xx411c7mD", 1)),
        ("bv1xx411c7mD", ("bv", "BV1xx411c7mD", 1)),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD/?p=3&spm_id_from=x",
            ("bv", "BV1xx411c7mD", 3),
        ),
        ("av170001", ("av", "170001", 1)),
        ("https://www.bilibili.com/video/av170001", ("av", "170001", 1)),
        ("ep1001", ("ep", "1001", 1)),
        ("https://www.bilibili.com/bangumi/play/ep1001", ("ep", "1001", 1)),
        ("https://www.bilibili.com/bangumi/play/ss4321", ("ss", "4321", 1)),
        ("https://www.bilibili.com/bangumi/media/md2823", ("md", "2823", 1)),
        ("md2823", ("md", "2823", 1)),
    ],
)
def test_identify_kinds(raw, expect):
    assert identify(raw) == expect


@pytest.mark.parametrize("raw", ["", "   ", "随便一段话", "https://example.com/a/b"])
def test_identify_rejects_garbage(raw):
    with pytest.raises(WBError) as exc:
        identify(raw)
    assert exc.value.code == "VALIDATION"


# --------------------------------------------------------------------- 单稿 / 多P


def test_single_video():
    res, client, _ = make_resolver({VIDEO_VIEW: SINGLE_VIEW})
    out = res.resolve("BV1xx411c7mD")
    assert out.kind == KIND_SINGLE
    assert out.title == "线性代数没速通（第一讲）"
    assert out.up_name == "示例UP"
    assert out.items[0].cid == 279786
    assert out.items[0].duration_ms == 2701000
    assert client.calls == [(VIDEO_VIEW, {"bvid": "BV1xx411c7mD"})]


def test_av_number_resolves_through_aid():
    res, client, _ = make_resolver({VIDEO_VIEW: SINGLE_VIEW})
    out = res.resolve("av170001")
    assert out.kind == KIND_SINGLE
    assert client.calls[0][1] == {"aid": 170001}


def test_multi_p_lists_every_page():
    res, _, _ = make_resolver({VIDEO_VIEW: MULTI_VIEW})
    out = res.resolve("BV1yy411c7mE")
    assert out.kind == KIND_MULTI_P
    assert [it.page_no for it in out.items] == [1, 2, 3]
    assert [it.cid for it in out.items] == [11, 12, 13]
    assert out.items[1].display_title.endswith("自注意力的矩阵形状")
    assert "3 个分P" in out.note


def test_multi_p_with_page_param_takes_only_that_page():
    res, _, _ = make_resolver({VIDEO_VIEW: MULTI_VIEW})
    out = res.resolve("https://www.bilibili.com/video/BV1yy411c7mE?p=2")
    assert out.kind == KIND_MULTI_P
    assert len(out.items) == 1
    assert out.items[0].cid == 12
    assert out.items[0].page_no == 2


def test_multi_page_items_carry_own_page_count(tmp_path):
    """分P 数带在每条上：目录的 P01_ 前缀只在真有多 P 时出现，合集不该有。"""
    res, _, _ = make_resolver({VIDEO_VIEW: MULTI_VIEW})
    out = res.resolve("BV1yy411c7mE")
    assert [it.page_count for it in out.items] == [3, 3, 3]
    from video_workbench.core.layout import _segments

    paths = ["/".join(_segments("UP", it.title, "", it.page_no, it.part_title)) for it in out.items]
    assert paths == [
        "UP/三十分钟讲透注意力机制/P01_注意力是什么",
        "UP/三十分钟讲透注意力机制/P02_自注意力的矩阵形状",
        "UP/三十分钟讲透注意力机制/P03_多头到底多在哪",
    ]
    res2, _, _ = make_resolver({VIDEO_VIEW: COLLECTION_VIEW})
    coll = res2.resolve("BV1aa411c7mF")
    assert [
        "/".join(_segments("UP", it.title, coll.collection_title, None, it.part_title))
        for it in coll.items
    ] == ["UP/从零手搓一个视频工作台/第一集 结构", "UP/从零手搓一个视频工作台/第二集 通道"]


def test_collection_returns_all_episodes():
    res, _, _ = make_resolver({VIDEO_VIEW: COLLECTION_VIEW})
    out = res.resolve("BV1aa411c7mF")
    assert out.kind == KIND_COLLECTION
    assert out.collection_title == "从零手搓一个视频工作台"
    assert [it.bvid for it in out.items] == ["BV1aa411c7mF", "BV1bb411c7mG"]
    # 每条取自己的标题：以前全用父视频标题，素材库一排同名分不清谁是谁。
    assert [it.title for it in out.items] == ["第一集 结构", "第二集 通道"]
    assert [it.page_count for it in out.items] == [1, 1]
    assert out.title == "合集中的某一集"  # 批次标题仍是进来的那条


# --------------------------------------------------------------------- 番剧


def test_bangumi_season():
    res, client, _ = make_resolver({PGC_SEASON: SEASON_DATA})
    out = res.resolve("https://www.bilibili.com/bangumi/play/ss4321")
    assert out.kind == KIND_BANGUMI
    assert len(out.items) == 2
    assert out.season_id == 4321
    assert client.calls[0][1] == {"season_id": 4321}
    assert out.items[0].part_title == "第1话"
    assert out.items[0].title == "起风了"  # 本话长标题，不是整季同名


def test_bangumi_single_episode():
    res, _, _ = make_resolver({PGC_SEASON: SEASON_DATA})
    out = res.resolve("ep1002")
    assert len(out.items) == 1
    assert out.items[0].cid == 3002


def test_bangumi_without_episodes_is_not_found():
    res, _, _ = make_resolver({PGC_SEASON: {"title": "空番", "episodes": []}})
    with pytest.raises(WBError) as exc:
        res.resolve("ss1")
    assert exc.value.code == "NOT_FOUND"


# --------------------------------------------------------------------- 缓存 / 短链 / 错误


def test_second_resolve_hits_cache():
    res, client, gw = make_resolver({VIDEO_VIEW: SINGLE_VIEW})
    res.resolve("BV1xx411c7mD")
    res.resolve("bv1xx411c7mD")  # 大小写不同也要命中
    assert len(client.calls) == 1
    assert len(gw.cache) == 1


def test_cache_expires():
    now = [100.0]
    cache = TtlCache(ttl_s=600, now=lambda: now[0])
    res, client, _ = make_resolver({VIDEO_VIEW: SINGLE_VIEW}, cache=cache)
    res.resolve("BV1xx411c7mD")
    now[0] += 601
    res.resolve("BV1xx411c7mD")
    assert len(client.calls) == 2


def test_short_link_expands_first():
    res, client, _ = make_resolver({VIDEO_VIEW: SINGLE_VIEW})
    out = res.resolve("https://b23.tv/abc123")
    assert out.kind == KIND_SINGLE
    assert client.calls[0][0] == "expand"


@pytest.mark.parametrize(
    "err,code",
    [
        (BiliError(-404, "啥都木有"), "NOT_FOUND"),
        (BiliError(-101, "账号未登录"), "AUTH_REQUIRED"),
        (BiliError(-509, "请求过于频繁"), "BILI_ERROR"),
    ],
)
def test_bili_errors_map_to_codes(err, code):
    res, _, _ = make_resolver({VIDEO_VIEW: err})
    with pytest.raises(WBError) as exc:
        res.resolve("BV1xx411c7mD")
    assert exc.value.code == code


def test_payload_shape_is_json_ready():
    res, _, _ = make_resolver({VIDEO_VIEW: COLLECTION_VIEW})
    payload = res.resolve("BV1aa411c7mF").to_payload()
    assert payload["count"] == 2
    assert set(payload["items"][0]) == {
        "bvid",
        "aid",
        "cid",
        "page",
        "title",
        "part_title",
        "page_count",
        "duration_ms",
        "cover",
    }
