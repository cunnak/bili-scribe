"""ChatClient 的解析与空正文守卫。"""
from __future__ import annotations

import json

import pytest

from video_workbench.infra.llm import ChatClient


def make(transport):
    return ChatClient(
        base_url="http://127.0.0.1:3425/v1",
        api_key="magpie",
        model="auto/hy4-preview",
        transport=transport,
    )


def reply(content: str, **extra) -> str:
    data = {
        "id": "x",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": extra.pop("finish", "stop"),
                "message": {"role": "assistant", "content": content, **extra},
            }
        ],
        "usage": {},
    }
    return json.dumps(data, ensure_ascii=False)


def test_plain_content() -> None:
    client = make(lambda url, body, headers, timeout: (200, reply("收到")))
    assert client.chat([{"role": "user", "content": "hi"}]) == "收到"


def test_segmented_content_joined() -> None:
    body = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": [{"type": "text", "text": "前半"}, {"type": "text", "text": "后半"}]
                    },
                }
            ]
        },
        ensure_ascii=False,
    )
    client = make(lambda url, b, headers, timeout: (200, body))
    assert client.chat([{"role": "user", "content": "hi"}]) == "前半后半"


def test_empty_content_from_reasoning_model_is_an_error() -> None:
    """magpie 的 auto/* 在 max_tokens 太小时：content=null、全给 reasoning_content。

    以前这里返回空串，上层（总结、翻译）会把它当成功结果写进库里。
    """
    body = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "We need answer user ...",
                    },
                }
            ],
            "usage": {
                "completion_tokens": 32,
                "completion_tokens_details": {"reasoning_tokens": 32},
            },
        },
        ensure_ascii=False,
    )
    client = make(lambda url, b, headers, timeout: (200, body))
    with pytest.raises(Exception) as excinfo:
        client.chat([{"role": "user", "content": "hi"}], max_tokens=32)
    err = excinfo.value
    assert getattr(err, "code", "") == "LLM_EMPTY"
    assert "思考" in str(err)
    assert "max_tokens" in str(getattr(err, "detail", ""))


def test_empty_content_without_reasoning_is_an_error() -> None:
    body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
    client = make(lambda url, b, headers, timeout: (200, body))
    with pytest.raises(Exception) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert getattr(excinfo.value, "code", "") == "LLM_EMPTY"


def test_retries_server_error_then_succeeds() -> None:
    calls = {"n": 0}

    def transport(url, body, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return 503, "busy"
        return 200, reply("第二次成功")

    client = ChatClient(
        base_url="http://x/v1", api_key="k", model="m", transport=transport, sleep=lambda s: None
    )
    assert client.chat([{"role": "user", "content": "hi"}]) == "第二次成功"
    assert calls["n"] == 2
