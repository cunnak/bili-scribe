"""P4-05 发布门禁与笔记发布。

这一层是全应用唯一不可撤销的动作，所以测试的重点不是"能发出去"，而是：

1. **发不出去的那些路**：没勾确认、门禁没过、已经发过、缺 aid——每一条都要在
   真正发请求之前挡住，并且说清为什么。
2. **失败要留得住草稿**：平台拒了（重复/敏感/验证码）之后，草稿还在，
   用户改两个字就能重发，不用从总结重来。
3. **只有 -509 重试**：验证码/敏感/重复重试一百次也一样，重试只会让风控更认定这是脚本。
"""

from __future__ import annotations

import json

import pytest
import requests

from bili_scribe.infra.bili_client import BiliError
from bili_scribe.infra.endpoints import NOTE_ADD, NOTE_DEL
from video_workbench.app.errors import WBError
from video_workbench.core import publisher as pub
from video_workbench.core.layout import SIDECAR

# --------------------------------------------------------------------- 替身


class FakeClient:
    """只实现 post_form：记下每次请求，按脚本决定成功还是抛 BiliError。"""

    def __init__(self, *, script=None, note_id="9001"):
        self.calls: list[tuple[str, dict]] = []
        self.script = list(script or [])
        self.note_id = note_id
        # BiliClient 真正发请求时会把 bili_jct 作为 csrf 补进表单；这里模拟同样的 32 字符。
        self.csrf = "a" * 32

    def post_form(self, url, payload):
        self.calls.append((url, payload))
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
        # BiliClient.post_form 已经把信封拆过，返回的就是 data 本体
        return {"note_id": self.note_id}


class FakeGateway:
    def __init__(self, client, *, logged_in=True):
        self._client = client
        self._logged_in = logged_in

    def client(self):
        return self._client

    def is_logged_in(self):
        return self._logged_in


def make_note(service, *, title="一篇笔记", summary="", body=None, status="draft"):
    """造一条草稿行（用真 repo，别用假表——门禁要看真字段）。"""
    project = service.repo.project_create(
        title="测试视频",
        bvid="BV1xx411c7mD",
        aid=778899,
        root_dir="",
        up_name="验收UP",
    )
    note = service.repo.publish_note_upsert(
        project["id"],
        bvid="BV1xx411c7mD",
        aid=778899,
        title=title,
        summary=summary,
        content=body if body is not None else json.dumps([{"insert": "正文一段话\n"}]),
    )
    if status != "draft":
        service.repo.publish_set_status(note["id"], status, note_id="9000")
    return project, service.repo.publish_get(note["id"])


def use_client(service, client, *, logged_in=True):
    service._gateway = FakeGateway(client, logged_in=logged_in)
    return client


def err(service, method, params):
    out = service.dispatch(method, params)
    assert out["ok"] is False, f"{method} 本该失败却成功了：{out.get('data')}"
    return out["error"]


def ok(service, method, params):
    out = service.dispatch(method, params)
    assert out["ok"] is True, f"{method} 失败：{out.get('error')}"
    return out["data"]


# ------------------------------------------------------------------ 错误翻译


@pytest.mark.parametrize(
    "code,expect",
    [
        (12015, "验证码"),
        (12016, "敏感"),
        (12051, "重复"),
        (-509, "限流"),
        (12035, "黑名单"),
        (-102, "封停"),
        (79501, "20000"),
    ],
)
def test_note_error_text_has_actionable_hint(code, expect):
    text = pub.note_error_text(BiliError(code, "出错"))
    assert str(code) in text and expect in text


def test_note_error_text_keeps_unknown_codes_and_says_retry():
    text = pub.note_error_text(BiliError(12345, "怪错误"))
    assert "[12345]" in text and "稍后重试" in text


# ------------------------------------------------------------------ post_note


def test_post_note_builds_bilibili_payload():
    client = FakeClient()
    out = pub.post_note(
        client,
        {"aid": 42, "title": "标题", "content": '[{"insert":"x\\n"}]'},
    )
    url, payload = client.calls[0]
    assert url == NOTE_ADD
    assert payload["oid"] == 42 and payload["oid_type"] == 0
    assert payload["title"] == "标题"
    assert payload["cont_len"] == len(pub.plain_text(payload["content"]))
    # 摘要字段已下线（2026-11-07）：界面上没有这个输入框，库里也不认老值。
    # 但接口把它标成"必要"，所以这个位置发的是**正文开头**（不是空串，空串在必填位
    # 上可能直接被平台 -400 —— 那正是"点了发布发不出去"的一种死法）。
    # auto_comment=1：平台把摘要预览作为评论卡同步到视频评论区。
    assert payload["summary"] == "x"
    assert payload["auto_comment"] == 1
    assert payload["publish"] == 1
    assert out["note_id"] == "9001" and out["attempts"] == 1


def test_auto_comment_can_be_explicitly_disabled():
    """默认同步到评论区；只有显式传 False 才关闭评论卡。"""
    payload = pub.note_payload(
        {"aid": 1, "content": '[{"insert":"x\\n"}]'},
        auto_comment=False,
    )
    assert payload["auto_comment"] == 0


def test_a_legacy_summary_in_the_row_is_not_sent():
    """老草稿行里还留着下线前算好的摘要：发出去时**不许**认它。

    实录：draft_8dfe01d36c95 的 summary 列是 268 字（与 comment_short 逐字同源），
    用户界面上早就看不到这个字段了；如果认它，就会把用户删掉的摘要作为评论卡
    同步到评论区。库里的老行没有清零路径，只能在出口这一处不认。

    现在那个位置发的是**正文开头**：正文是用户确认过的那一份，前 80 字天然属于
    它，既不会空着（必填位），也不会多出用户没见过的话；auto_comment=1 让它
    作为评论卡同步到视频评论区。
    """
    client = FakeClient()
    pub.post_note(
        client,
        {
            "aid": 7,
            "summary": "旧草稿里的 268 字",
            "content": '[{"insert":"正文第一句。\\n第二句。\\n"}]',
        },
    )
    payload = client.calls[0][1]
    assert "旧草稿" not in payload["summary"]
    assert payload["summary"] == "正文第一句。 第二句。"
    assert payload["auto_comment"] == 1


def test_the_summary_slot_is_filled_from_the_body_and_capped():
    """必填位不许空着，但也不许超过预览口径。"""
    body = "".join(f"第{i}段。" for i in range(60))
    payload = pub.note_payload({"aid": 1, "content": json.dumps([{"insert": body + "\\n"}])})
    assert payload["summary"], "必填位被发成了空串"
    assert len(payload["summary"]) == pub.NOTE_PREVIEW_CHARS == 80
    assert body.startswith(payload["summary"])


def _multiline_delta(lines: int = 100, line_chars: int = 20) -> str:
    """合成一份约 2,000 字、逐行一个 op 的 Delta（不依赖真实草稿内容）。"""
    ops = []
    for index in range(lines):
        text = (f"第{index}段" + "内容" * 20)[:line_chars]
        ops.append({"insert": text + "\n"})
    return json.dumps(ops, ensure_ascii=False)


def test_compact_delta_merges_adjacent_same_attribute_ops():
    raw = json.dumps(
        [
            {"insert": "a"},
            {"insert": "b"},
            {"insert": "\n", "attributes": {"bold": True}},
            {"insert": "c"},
        ],
        ensure_ascii=False,
    )
    assert json.loads(pub.compact_delta(raw)) == [
        {"insert": "ab"},
        {"insert": "\n", "attributes": {"bold": True}},
        {"insert": "c"},
    ]


def test_form_body_bytes_matches_requests_encoding():
    payload = {
        "oid": 42,
        "title": "标题",
        "summary": "预览",
        "content": '[{"insert":"正文"}]',
        "csrf": "a" * 32,
    }
    expected = len(requests.Request("POST", NOTE_ADD, data=payload).prepare().body)
    assert pub.form_body_bytes(payload) == expected


def test_note_payload_compacts_delta_and_sends_cont_len():
    raw = json.dumps([{"insert": "第一行\n"}, {"insert": "第二行\n"}], ensure_ascii=False)
    payload = pub.note_payload({"aid": 1, "title": "标题", "content": raw})
    assert json.loads(payload["content"]) == [{"insert": "第一行\n第二行\n"}]
    assert payload["cont_len"] == len(pub.plain_text(payload["content"]))


def test_a_two_thousand_char_multiline_note_fits_after_compaction():
    """2,000 字逐行 op 的 Delta：未压缩会过 20,000 B，压缩后必须回到安全线内。"""
    raw = _multiline_delta()
    raw_payload = {
        "oid": 1,
        "oid_type": 0,
        "title": "标题",
        "summary": "预览",
        "content": raw,
        "cls": 1,
        "from": "save",
        "platform": "web",
        "publish": 1,
        "auto_comment": 1,
    }
    assert pub.form_body_bytes(raw_payload, csrf="a" * 32) > 20_000
    payload = pub.note_payload({"aid": 1, "title": "标题", "content": raw})
    assert pub.form_body_bytes(payload, csrf="a" * 32) <= pub.NOTE_BODY_MAX_BYTES


def test_post_note_rejects_oversize_body_before_any_request():
    client = FakeClient()
    with pytest.raises(WBError) as info:
        pub.post_note(
            client,
            {"aid": 1, "title": "标题", "content": json.dumps([{"insert": "中" * 12_000}])},
        )
    assert info.value.code == "VALIDATION"
    assert "20000" in info.value.message
    assert client.calls == []


def test_post_note_rejects_empty_content_before_any_request():
    client = FakeClient()
    with pytest.raises(WBError) as info:
        pub.post_note(client, {"aid": 1, "content": "[]"})
    assert info.value.code == "VALIDATION"
    assert client.calls == []


def test_post_note_retries_only_rate_limit():
    client = FakeClient(script=[BiliError(-509, "请求过于频繁")])
    sleeps: list[float] = []
    notes: list[str] = []
    out = pub.post_note(
        client,
        {"aid": 1, "content": '[{"insert":"x\\n"}]'},
        sleep=lambda s: sleeps.append(s),
        on_retry=notes.append,
    )
    assert out["attempts"] == 2 and sleeps == [3]
    assert notes and "-509" in notes[0]


def test_post_note_does_not_retry_captcha():
    client = FakeClient(script=[BiliError(12015, "验证码")])
    with pytest.raises(WBError) as info:
        pub.post_note(client, {"aid": 1, "content": '[{"insert":"x\\n"}]'}, sleep=lambda _s: None)
    assert info.value.code == "BILI_ERROR"
    assert len(client.calls) == 1, "验证码不该重试"


# ------------------------------------------------------------------- 发布门禁


def test_publish_blocked_when_not_confirmed(service):
    _, note = make_note(service)
    client = use_client(service, FakeClient())

    error = err(service, "publish.publish", {"id": note["id"]})
    assert error["code"] == "CONFLICT"
    assert "确认" in error["message"]
    assert client.calls == [], "没勾确认就一个请求都不该发"


def test_publish_blocked_by_gates_and_lists_reasons(service):
    # 标题 90 字 → 内容关过不去
    _, note = make_note(service, title="长" * 90)
    client = use_client(service, FakeClient())

    error = err(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert error["code"] == "CONFLICT" and "门禁没过" in error["message"]
    gates = json.loads(error["detail"])
    assert any(g["id"] == "content" and g["status"] == "blocked" for g in gates["gates"])
    assert client.calls == []


def test_publish_success_writes_note_id_and_status(service):
    _, note = make_note(service)
    client = use_client(service, FakeClient(note_id="123456"))

    data = ok(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert data["note_id"] == "123456"
    assert data["url"].endswith("/BV1xx411c7mD")
    row = service.repo.publish_get(note["id"])
    assert row["status"] == "published"
    assert row["note_id"] == "123456"
    assert row["published_at"]
    assert client.calls[0][0] == NOTE_ADD
    assert client.calls[0][1]["auto_comment"] == 1, "服务层发布也要同步到评论区"


def test_publish_twice_is_blocked(service):
    _, note = make_note(service)
    use_client(service, FakeClient())
    ok(service, "publish.publish", {"id": note["id"], "confirmed": True})

    error = err(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert error["code"] == "CONFLICT" and "发过了" in error["message"]


def test_publish_failure_keeps_draft_content(service):
    _, note = make_note(service)
    before = note["content"]
    use_client(service, FakeClient(script=[BiliError(12016, "敏感内容")]))

    error = err(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert error["code"] == "BILI_ERROR" and "敏感" in error["message"]
    row = service.repo.publish_get(note["id"])
    assert row["status"] == "failed"
    assert "敏感" in (row["error"] or "")
    assert row["content"] == before, "失败不该动草稿正文"


def test_publish_requires_login(service):
    _, note = make_note(service)
    client = use_client(service, FakeClient(), logged_in=False)

    error = err(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert error["code"] == "CONFLICT"
    assert client.calls == []


# --------------------------------------------------------------------- 撤回


def test_withdraw_sends_note_del_and_marks_row(service):
    _, note = make_note(service)
    use_client(service, FakeClient(note_id="777"))
    ok(service, "publish.publish", {"id": note["id"], "confirmed": True})

    client = service._gateway.client()
    data = ok(service, "publish.withdraw", {"id": note["id"]})
    assert data["note_id"] == "777"
    url, payload = client.calls[-1]
    assert url == NOTE_DEL and payload["note_id"] == "777" and payload["oid"] == 778899
    row = service.repo.publish_get(note["id"])
    assert row["status"] == "withdrawn" and row["withdrawn_at"]


def test_withdraw_needs_published_status(service):
    _, note = make_note(service)
    use_client(service, FakeClient())
    error = err(service, "publish.withdraw", {"id": note["id"]})
    assert error["code"] == "CONFLICT" and "已发布" in error["message"]


def test_withdraw_without_note_id_is_refused(service):
    _, note = make_note(service, status="published")
    service.repo.publish_set_status(note["id"], "published", note_id="")
    use_client(service, FakeClient())
    error = err(service, "publish.withdraw", {"id": note["id"]})
    assert error["code"] == "CONFLICT" and "note_id" in error["message"]


def test_withdraw_failure_keeps_published_status(service):
    _, note = make_note(service)
    client = use_client(service, FakeClient(note_id="888"))
    ok(service, "publish.publish", {"id": note["id"], "confirmed": True})

    client.script.append(BiliError(12016, "删不掉"))
    error = err(service, "publish.withdraw", {"id": note["id"]})
    assert error["code"] == "BILI_ERROR"
    assert service.repo.publish_get(note["id"])["status"] == "published"


# --------------------------------------------------------------- 门禁会随确认变


def test_get_with_confirmed_turns_content_gate_green(service):
    _, note = make_note(service)
    use_client(service, FakeClient())

    cold = ok(service, "publish.get", {"id": note["id"]})
    assert [g["status"] for g in cold["gates"]["gates"]][0] == "todo"

    warm = ok(service, "publish.get", {"id": note["id"], "confirmed": True})
    assert warm["gates"]["ready"] is True
    assert [g["status"] for g in warm["gates"]["gates"]] == ["ok", "ok", "ok"]


# --------------------------------------------------------- aid 三级兜底

# 这三条盯的是同一个事故的另一半：`wb_project.aid` 与 `wb_publish_note.aid`
# 历史上全是 NULL（下载 worker 把解析器给的 aid 丢了），于是发布必死在
# 「这条草稿没有 aid，定位不到视频」，而门禁只看 bvid、三道门全绿。
# 现在发布前按「库里 → 侧车 → 解析器」三级现取，取到就地回写草稿行。


class _Item:
    def __init__(self, aid):
        self.aid = aid


class FakeResolver:
    """只实现 resolve：解析器那一级兜底的替身，不碰网络。"""

    def __init__(self, aid=None, error=None):
        self.aid = aid
        self.error = error
        self.calls: list[str] = []

    def resolve(self, raw):
        self.calls.append(raw)
        if self.error:
            raise self.error
        return type("R", (), {"items": [_Item(self.aid)]})()


def make_note_without_aid(service, *, root_dir=""):
    """造一条 aid 为空的草稿（老数据的样子：项目行与草稿行都没 aid）。"""
    project = service.repo.project_create(
        title="测试视频", bvid="BV1xx411c7mD", root_dir=root_dir, up_name="验收UP"
    )
    note = service.repo.publish_note_upsert(
        project["id"],
        bvid="BV1xx411c7mD",
        title="一篇笔记",
        content=json.dumps([{"insert": "正文一段话\n"}]),
    )
    return project, service.repo.publish_get(note["id"])


def test_publish_fills_aid_from_the_sidecar_when_the_row_has_none(service, tmp_path):
    """项目目录里的 source.wb.json 写着 aid：就地补上，并且回写草稿行。"""
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    (root / SIDECAR).write_text(json.dumps({"aid": 555, "bvid": "BV1xx411c7mD"}), "utf-8")

    project, note = make_note_without_aid(service, root_dir=str(root))
    assert note["aid"] is None, "夹具得真的没有 aid，否则这条测的是另一件事"
    client = use_client(service, FakeClient())

    ok(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert client.calls[0][1]["oid"] == 555
    assert service.repo.publish_get(note["id"])["aid"] == 555, "补到的 aid 要回写，别每次都现找"


def test_publish_falls_back_to_the_resolver_when_there_is_no_sidecar(service):
    """连侧车都没有（本地导入/目录被搬走）：拿 bvid 现问一次解析器。"""
    project, note = make_note_without_aid(service)
    resolver = FakeResolver(aid=777)
    service._resolver = resolver
    client = use_client(service, FakeClient())

    ok(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert resolver.calls == ["BV1xx411c7mD"]
    assert client.calls[0][1]["oid"] == 777


def test_publish_says_why_when_no_aid_can_be_found(service):
    """三条腿都空：一个请求都不发，报错要说清「重新下载这一集会补上」。"""
    project, note = make_note_without_aid(service)
    service._resolver = FakeResolver(aid=None)
    client = use_client(service, FakeClient())

    error = err(service, "publish.publish", {"id": note["id"], "confirmed": True})
    assert error["code"] == "CONFLICT"
    assert "aid" in error["message"] and "重新下载" in error["message"]
    assert client.calls == [], "没有 aid 就一个请求都不该发"
