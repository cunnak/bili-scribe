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

from bili_scribe.infra.bili_client import BiliError
from bili_scribe.infra.endpoints import NOTE_ADD, NOTE_DEL
from video_workbench.app.errors import WBError
from video_workbench.core import publisher as pub


# --------------------------------------------------------------------- 替身

class FakeClient:
    """只实现 post_form：记下每次请求，按脚本决定成功还是抛 BiliError。"""

    def __init__(self, *, script=None, note_id="9001"):
        self.calls: list[tuple[str, dict]] = []
        self.script = list(script or [])
        self.note_id = note_id

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


def make_note(service, *, title="一篇笔记", summary="摘要", body=None, status="draft"):
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
        {"aid": 42, "title": "标题", "summary": "摘要", "content": '[{"insert":"x\\n"}]'},
    )
    url, payload = client.calls[0]
    assert url == NOTE_ADD
    assert payload["oid"] == 42 and payload["oid_type"] == 0
    assert payload["title"] == "标题" and payload["summary"] == "摘要"
    assert payload["auto_comment"] == 1 and payload["publish"] == 1
    assert out["note_id"] == "9001" and out["attempts"] == 1


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
