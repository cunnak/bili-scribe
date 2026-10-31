"""请求头：默认 UA、额外请求头、opencode 的会话头。

这三条都是「密钥没问题、网关就是不认」的真实故障，各钉一条。
"""
from __future__ import annotations

import json

from video_workbench.config import LlmConfig, _fill, validate
from video_workbench.infra.llm import DEFAULT_USER_AGENT, ChatClient


def client(**kw):
    return ChatClient(**{"base_url": "https://api.example.com/v1", "api_key": "k", "model": "m", **kw})


def test_default_user_agent_looks_like_a_browser() -> None:
    """默认 UA 必须是浏览器 UA。

    Cloudflare 的 1010 规则按客户端指纹拦人：同一个密钥、同一个地址，
    ``VideoWorkbench/0.1`` 一律 403，浏览器 UA 就 200。所以「程序名」不能当默认值。
    """
    assert "Mozilla/5.0" in DEFAULT_USER_AGENT
    assert "Chrome/" in DEFAULT_USER_AGENT
    assert "urllib" not in DEFAULT_USER_AGENT.lower()


def test_default_user_agent_is_sent() -> None:
    seen: dict = {}

    def transport(url, body, headers, timeout):
        seen.update(headers)
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]})

    client(transport=transport).chat([{"role": "user", "content": "hi"}])
    assert seen["User-Agent"] == DEFAULT_USER_AGENT


def test_opencode_gets_session_header_on_both_calls() -> None:
    """opencode zen 的对话接口缺 ``x-opencode-session`` 会回 400 MissingSessionID。

    这个头要同时出现在「列模型」和「对话」两条路径上，且同一个客户端内保持稳定
    （每次换值会把上游的会话打散）。
    """
    seen: list[dict] = []

    def transport(url, body, headers, timeout):
        seen.append(dict(headers))
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]})

    def getter(url, headers, timeout):
        seen.append(dict(headers))
        return 200, json.dumps({"data": [{"id": "deepseek-v4.1-flash"}]})

    c = ChatClient(
        base_url="https://opencode.ai/zen/go/v1",
        api_key="k",
        model="deepseek-v4.1-flash",
        transport=transport,
        getter=getter,
    )
    c.chat([{"role": "user", "content": "hi"}])
    c.list_models()
    c.chat([{"role": "user", "content": "again"}])

    assert len(seen) == 3
    sessions = {h.get("x-opencode-session") for h in seen}
    assert sessions and None not in sessions, "两条路径都必须带会话头"
    assert len(sessions) == 1, f"会话头应当稳定，实际拿到 {sessions}"


def test_other_hosts_do_not_get_opencode_header() -> None:
    seen: dict = {}

    def transport(url, body, headers, timeout):
        seen.update(headers)
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]})

    client(transport=transport).chat([{"role": "user", "content": "hi"}])
    assert "x-opencode-session" not in {k.lower() for k in seen}


def test_extra_headers_are_merged_and_can_override() -> None:
    seen: dict = {}

    def transport(url, body, headers, timeout):
        seen.update(headers)
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]})

    c = client(transport=transport, extra_headers={"api-version": "2024-02-01", "User-Agent": "custom/1"})
    c.chat([{"role": "user", "content": "hi"}])
    assert seen["api-version"] == "2024-02-01"
    assert seen["User-Agent"] == "custom/1"


def test_config_keeps_extra_headers_key_types() -> None:
    llm = _fill(LlmConfig, {"extra_headers": {"api-version": 2024, "X-Trace": "1"}})
    # 头值只能是字符串，填了数字也要转过来，否则发请求时 httplib 直接报错
    assert llm.extra_headers == {"api-version": "2024", "X-Trace": "1"}


def test_config_rejects_header_with_newline() -> None:
    from video_workbench.app.errors import WBError
    from video_workbench.config import WorkbenchConfig

    cfg = WorkbenchConfig.load()
    cfg.llm.extra_headers = {"X-Bad": "a" + chr(13) + "b"}
    try:
        validate(cfg)
    except WBError as exc:
        assert exc.code == "VALIDATION"
        assert "extra_headers" in exc.message
    else:  # pragma: no cover
        raise AssertionError("带换行的头必须被拦下（否则可以注入其他头）")
