"""「获取可用模型」（``llm.models``）的测试。

这条路径的价值在于**少打字**：模型名的大小写、日期后缀、网关前缀，是设置页最
容易填错的一栏。所以它必须对三种真实返回形状都认，且失败时说清是哪一步失败
（没配 / 密钥错 / 连不上 / 这家不支持 /models）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_workbench.infra.llm import ChatClient, ChatError, normalize_models, _urllib_get


# ---------------------------------------------------------------- 归一化


def test_normalize_openai_shape():
    payload = {
        "object": "list",
        "data": [
            {"id": "deepseek-chat", "owned_by": "deepseek"},
            {"id": "deepseek-reasoner", "owned_by": "deepseek"},
        ],
    }
    assert normalize_models(payload) == [
        {"id": "deepseek-chat", "owned_by": "deepseek"},
        {"id": "deepseek-reasoner", "owned_by": "deepseek"},
    ]


def test_normalize_gateway_shape_with_strings():
    assert [m["id"] for m in normalize_models({"models": ["qwen-max", {"name": "qwen-plus"}]})] == [
        "qwen-max",
        "qwen-plus",
    ]


def test_normalize_plain_list_and_dedupe():
    got = normalize_models(["a", "b", "a", "", {"id": "c"}, {"id": ""}])
    assert [m["id"] for m in got] == ["a", "b", "c"]


def test_normalize_unknown_shape_is_empty():
    assert normalize_models("nope") == []
    assert normalize_models({"error": "x"}) == []


def test_normalize_single_list_value_fallback():
    """有的端点把列表藏在自造键里：只有一个 list 值就认它。"""
    assert [m["id"] for m in normalize_models({"result": [{"id": "m1"}]})] == ["m1"]


# ---------------------------------------------------------------- list_models


class FakeGetter:
    def __init__(self, status=200, body='{"data":[{"id":"m1"}]}', exc=None):
        self.status = status
        self.body = body
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        if self.exc:
            raise self.exc
        return self.status, self.body


def client_with(getter, **kw):
    params = {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-x", "model": "", "getter": getter}
    params.update(kw)
    return ChatClient(**params)


def test_list_models_hits_models_path_with_bearer():
    getter = FakeGetter()
    models = client_with(getter).list_models()
    assert [m["id"] for m in models] == ["m1"]
    url, headers = getter.calls[0]
    assert url == "https://api.deepseek.com/v1/models"
    assert headers["Authorization"] == "Bearer sk-x"


def test_list_models_does_not_require_model_name():
    """点这个按钮的时候用户正是还不知道有哪些模型，所以不能要求 model。"""
    assert client_with(FakeGetter(), model="").list_models()


def test_list_models_missing_key_is_not_configured():
    with pytest.raises(ChatError) as exc:
        client_with(FakeGetter(), api_key="").list_models()
    assert exc.value.code == "LLM_NOT_CONFIGURED"
    assert "api_key" in (exc.value.detail or "")


def test_list_models_401_is_auth():
    with pytest.raises(ChatError) as exc:
        client_with(FakeGetter(status=401, body='{"error":"bad key"}')).list_models()
    assert exc.value.code == "LLM_AUTH"


def test_list_models_network_error():
    with pytest.raises(ChatError) as exc:
        client_with(FakeGetter(exc=OSError("connection refused"))).list_models()
    assert exc.value.code == "NETWORK"


def test_list_models_non_json():
    with pytest.raises(ChatError) as exc:
        client_with(FakeGetter(body="<html>not json</html>")).list_models()
    assert exc.value.code == "LLM_BAD_RESPONSE"


def test_list_models_empty_list_is_an_error():
    """200 但空列表：这家多半不实现 /models。不能说"成功"，否则界面弹个空窗。"""
    with pytest.raises(ChatError) as exc:
        client_with(FakeGetter(body='{"data":[]}')).list_models()
    assert exc.value.code == "LLM_BAD_RESPONSE"
    assert "手动" in (exc.value.detail or "")


# ---------------------------------------------------------------- 真发一次 HTTP


class _Handler(BaseHTTPRequestHandler):
    got: list[str] = []

    def do_GET(self):  # noqa: N802 - 标准库接口
        _Handler.got.append(self.path + "|" + (self.headers.get("Authorization") or ""))
        body = json.dumps({"data": [{"id": "real-1"}, {"id": "real-2"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音：测试输出里不要 HTTP 访问日志
        return


@pytest.fixture()
def fake_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_urllib_get_really_speaks_http(fake_endpoint):
    """连真 HTTP：证明默认 GET 传输层可用（方法、头、读响应都对）。"""
    _Handler.got.clear()
    client = ChatClient(base_url=fake_endpoint, api_key="sk-real", model="")
    models = client.list_models()
    assert [m["id"] for m in models] == ["real-1", "real-2"]
    assert _Handler.got == ["/v1/models|Bearer sk-real"]


def test_urllib_get_surfaces_status(fake_endpoint):
    """走真传输层时 401 也要翻成 LLM_AUTH，而不是抛 urllib 的异常。"""

    def boom(url, headers, timeout):
        return 401, '{"error":"invalid api key"}'

    client = ChatClient(base_url=fake_endpoint, api_key="sk-real", model="", getter=boom)
    with pytest.raises(ChatError) as exc:
        client.list_models()
    assert exc.value.code == "LLM_AUTH"


# ---------------------------------------------------------------- RPC


class StubModelsClient:
    provider_name = "默认服务商"
    base_url = "http://stub.local/v1"

    def __init__(self, models=None, error=None):
        self.models = models or [{"id": "m1", "owned_by": ""}]
        self.error = error

    def list_models(self):
        if self.error:
            raise self.error
        return self.models


def test_rpc_llm_models_returns_list(service, monkeypatch):
    stub = StubModelsClient([{"id": "m1", "owned_by": "x"}, {"id": "m2", "owned_by": ""}])
    monkeypatch.setattr(service, "_llm_client", lambda task="": stub)
    out = service.dispatch("llm.models", {})
    assert out["ok"] is True
    assert out["data"]["count"] == 2
    assert [m["id"] for m in out["data"]["models"]] == ["m1", "m2"]
    assert out["data"]["provider"] == "默认服务商"


def test_rpc_llm_models_auth_error_is_llm_error(service, monkeypatch):
    stub = StubModelsClient(error=ChatError("LLM_AUTH", "大模型鉴权失败", "bad key", 401))
    monkeypatch.setattr(service, "_llm_client", lambda task="": stub)
    out = service.dispatch("llm.models", {})
    assert out["ok"] is False
    assert out["error"]["code"] == "LLM_ERROR"
    assert "鉴权" in out["error"]["message"]


def test_rpc_llm_models_not_configured_keeps_its_code(service, monkeypatch):
    """界面靠这个码显示「先填地址和密钥」，折成 LLM_ERROR 就丢了提示。"""
    stub = StubModelsClient(error=ChatError("LLM_NOT_CONFIGURED", "服务商还没有配置完整", "缺：api_key"))
    monkeypatch.setattr(service, "_llm_client", lambda task="": stub)
    out = service.dispatch("llm.models", {})
    assert out["ok"] is False
    assert out["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_rpc_llm_models_provider_is_forwarded(service, monkeypatch):
    seen: dict = {}

    def fake_from_config(cfg, *, transport=None, task="", provider=""):
        seen["provider"] = provider
        return StubModelsClient()

    monkeypatch.setattr(
        "video_workbench.app.service.ChatClient.from_config", staticmethod(fake_from_config)
    )
    out = service.dispatch("llm.models", {"provider": "moonshot"})
    assert out["ok"] is True
    assert seen["provider"] == "moonshot"

def test_llm_models_uses_unsaved_values(service, monkeypatch) -> None:
    """设置页会先敲地址/密钥再点「获取可用模型」——那会儿还没保存。

    后端必须按传上来的值去请求，而不是拿磁盘上的旧配置。
    """
    seen: dict[str, object] = {}

    class Stub:
        provider_name = "默认服务商"
        base_url = "https://typed.example/v1"

        def list_models(self):
            return [{"id": "m-typed", "owned_by": ""}]

    def fake_from_config(cfg, **kwargs):
        seen["card_base"] = cfg.llm.providers[0].base_url
        seen["card_key"] = cfg.llm.providers[0].api_key
        return Stub()

    monkeypatch.setattr(
        "video_workbench.app.service.ChatClient.from_config", staticmethod(fake_from_config)
    )
    before_base = service.cfg.llm.providers[0].base_url
    before_key = service.cfg.llm.providers[0].api_key

    result = service.dispatch(
        "llm.models",
        {
            "provider": service.cfg.llm.providers[0].id,
            "base_url": "https://typed.example/v1",
            "api_key": "sk-typed",
        },
    )

    assert result["ok"] is True
    assert result["data"]["models"] == [{"id": "m-typed", "owned_by": ""}]
    assert seen["card_base"] == "https://typed.example/v1"
    assert seen["card_key"] == "sk-typed"
    # 关键：不能把 self.cfg 改脏（否则「放弃改动」之后内存与磁盘对不上）
    assert service.cfg.llm.providers[0].base_url == before_base
    assert service.cfg.llm.providers[0].api_key == before_key


def test_llm_test_uses_unsaved_values(service, monkeypatch) -> None:
    class Stub:
        provider_name = "默认服务商"
        base_url = "https://typed.example/v1"
        model = "m-typed"
        configured = True

        def chat(self, messages, **kwargs):
            return "OK"

    seen: dict[str, object] = {}

    def fake_from_config(cfg, **kwargs):
        seen["key"] = cfg.llm.providers[0].api_key
        return Stub()

    monkeypatch.setattr(
        "video_workbench.app.service.ChatClient.from_config", staticmethod(fake_from_config)
    )
    before = service.cfg.llm.providers[0].api_key

    result = service.dispatch(
        "llm.test",
        {
            "provider": service.cfg.llm.providers[0].id,
            "base_url": "https://typed.example/v1",
            "api_key": "sk-typed",
        },
    )

    assert result["ok"] is True
    assert result["data"]["ok"] is True
    assert seen["key"] == "sk-typed"
    assert service.cfg.llm.providers[0].api_key == before

def test_request_carries_user_agent() -> None:
    """不能让 urllib 的默认 UA 出去：那会被 Cloudflare 的 1010 直接 403。

    小村真实遇到过——同一个 base_url + key 在别处能用，在这里 403
    「blocked access based on your browser's signature」。
    """
    from video_workbench.infra.llm import DEFAULT_USER_AGENT, ChatClient

    seen: dict[str, str] = {}

    def transport(url, body, headers, timeout):
        seen.update(headers)
        return 200, '{"choices":[{"message":{"content":"ok"}}]}'

    def getter(url, headers, timeout):
        seen.update(headers)
        return 200, '{"data":[{"id":"m1"}]}'

    client = ChatClient(
        base_url="https://gw.example/v1",
        api_key="sk-x",
        model="m1",
        transport=transport,
        getter=getter,
    )
    client.chat([{"role": "user", "content": "hi"}])
    assert seen.get("User-Agent") == DEFAULT_USER_AGENT
    assert "urllib" not in DEFAULT_USER_AGENT.lower()

    seen.clear()
    client.list_models()
    assert seen.get("User-Agent") == DEFAULT_USER_AGENT


def test_user_agent_configurable() -> None:
    from video_workbench.infra.llm import ChatClient

    seen: dict[str, str] = {}

    def getter(url, headers, timeout):
        seen.update(headers)
        return 200, '{"data":[{"id":"m1"}]}'

    client = ChatClient(
        base_url="https://gw.example/v1",
        api_key="sk-x",
        getter=getter,
        user_agent="Mozilla/5.0 (custom)",
    )
    client.list_models()
    assert seen.get("User-Agent") == "Mozilla/5.0 (custom)"


def test_forbidden_is_not_auth_error() -> None:
    """403 曾被报成「api_key 不对」，把人带偏了一整天。"""
    from video_workbench.infra.llm import ChatClient

    client = ChatClient(base_url="https://gw.example/v1", api_key="sk-x", model="m1")
    cf = client._error_for(
        403,
        '{"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/'
        'cloudflare-1xxx-errors/error-1010/","title":"Error 1010: Access denied",'
        '"detail":"blocked access based on your browser signature"}',
    )
    assert cf.code == "LLM_FORBIDDEN"
    assert "CDN" in cf.message
    assert "user_agent" in cf.detail

    plain = client._error_for(403, "forbidden")
    assert plain.code == "LLM_FORBIDDEN"
    assert "403" in plain.message

    assert client._error_for(401, "x").code == "LLM_AUTH"

