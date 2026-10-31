"""OpenAI 兼容的聊天接口客户端（P2-04 起用）。

**为什么自己写而不用 SDK。** 三个理由：

1. 只要 ``POST {base_url}/chat/completions`` 这一条路径。装一个 SDK 换来几十个用不到的
   依赖项，对"双击就能装"的桌面应用是负担。
2. 失败要能翻译成我们自己的错误码。SDK 抛的是 ``openai.APIStatusError``，到了界面上
   还是得翻译一遍，不如一开始就在边界处转。
3. 测试要能替换传输层。``ChatClient(transport=...)`` 把"怎么发 HTTP"抽出来，
   单测和本地假模型都不需要起服务器。

**超时与重试。** 只重试"重试有意义"的失败：429、5xx、连接错误、读超时。
401/403 立即失败（key 错了重试一百次也还是错）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Mapping, Protocol

from ..app.errors import WBError

# 传输层签名：(url, payload_bytes, headers, timeout) -> (status, body_text)
Transport = Callable[[str, bytes, Mapping[str, str], float], tuple[int, str]]

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.8, 2.4)


class ChatError(WBError):
    """带上原始状态码，日志里能直接看出是配额还是鉴权。"""

    def __init__(self, code: str, message: str, detail: str = "", status: int = 0) -> None:
        super().__init__(code, message, detail)
        self.status = int(status or 0)


def _urllib_transport(proxy: str = "") -> Transport:
    """默认传输：标准库 urllib。代理只认配置里的 llm.proxy（不走系统设置）。"""

    def send(url: str, payload: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, str]:
        request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")
        handlers: list[urllib.request.BaseHandler] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=timeout) as resp:
                return int(resp.status), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也带响应体，读出来才看得懂原因
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            return int(exc.code), body

    return send


class _SupportsChat(Protocol):  # pragma: no cover - 仅用于类型说明
    def chat(self, messages: Iterable[dict[str, str]], **kwargs: Any) -> str: ...


class ChatClient:
    """一次请求 = 一次 ``chat``。批量、分批、进度由调用方管（那是业务逻辑）。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        *,
        timeout: float = 120.0,
        proxy: str = "",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        provider_name: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        # 服务商名字只为报错文案与「测试连接」回显服务。它有值不代表能用，
        # 判据仍然走 configured/require。
        self.provider_name = provider_name or ""
        self.timeout = float(timeout or 120.0)
        self.proxy = proxy or ""
        self._transport = transport or _urllib_transport(self.proxy)
        self._sleep = sleep

    # ------------------------------------------------------------- 装配

    @classmethod
    def from_config(cls, cfg: Any, *, transport: Transport | None = None,
                    task: str = "", provider: str = "") -> "ChatClient":
        """按配置装配客户端。

        **传 ``task`` 才会走服务商映射**（``llm.tasks.<task>``），传 ``provider``
        则指名某一家（设置页「测试连接」用）。为什么不直接读 ``llm.base_url``：
        那是「当前生效服务商」的镜像，而任务可能指名另一家（文字走 DeepSeek、
        视觉走 GLM）。取参数只有 ``config.resolve_task_model`` 一个入口，
        界面、任务、报错文案都从它拿——避免出现「设置页显示 A 家、实际打 B 家」。
        """
        from video_workbench.config import resolve_task_model

        llm = getattr(cfg, "llm", cfg)
        if provider:
            from video_workbench.config import find_provider

            picked = find_provider(llm, provider)
            model = str(getattr(llm, "model", "") or "")
            if picked is not None and picked.models:
                model = model if model in picked.models else picked.models[0]
            return cls(
                base_url=picked.base_url if picked else "",
                api_key=picked.api_key if picked else "",
                model=model,
                timeout=float(getattr(llm, "timeout_seconds", 120) or 120),
                proxy=str(getattr(llm, "proxy", "") or ""),
                transport=transport,
                provider_name=picked.name if picked else provider,
            )
        provider_obj, model = resolve_task_model(llm, task)
        return cls(
            base_url=str(getattr(provider_obj, "base_url", "") or getattr(llm, "base_url", "") or ""),
            api_key=str(getattr(provider_obj, "api_key", "") or getattr(llm, "api_key", "") or ""),
            model=str(model or ""),
            timeout=float(getattr(llm, "timeout_seconds", 120) or 120),
            proxy=str(getattr(llm, "proxy", "") or ""),
            transport=transport,
            provider_name=str(getattr(provider_obj, "name", "") or ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def require(self) -> None:
        """没配置就别往下走，省得在第一批请求里炸出 401。"""
        missing = [
            name
            for name, value in (("base_url", self.base_url), ("api_key", self.api_key), ("model", self.model))
            if not value
        ]
        if missing:
            # 报错要说清是**哪一家**没配齐。多服务商之后，「还没有配置大模型」
            # 用户不知道该点哪张卡；带上卡名一句就定位了。
            who = f"：{self.provider_name}" if self.provider_name else ""
            raise ChatError(
                "LLM_NOT_CONFIGURED",
                f"服务商还没有配置完整{who}",
                "缺：" + "、".join(missing),
            )

    # ------------------------------------------------------------- 请求

    def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        json_mode: bool = False,
    ) -> str:
        self.require()
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            # 让服务端保证"只吐 JSON"。兼容端点不一定认这个字段（有的直接 400），
            # 所以由调用方在 LLM_BAD_REQUEST 时退回来重试一次；这里只负责该发就发。
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

        last: ChatError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                status, text = self._transport(url, body, headers, self.timeout)
            except TimeoutError as exc:
                last = ChatError("LLM_TIMEOUT", "大模型响应超时", str(exc))
            except OSError as exc:  # 连接被拒、DNS 失败、代理不通都归这里
                last = ChatError("NETWORK", "连不上大模型服务", str(exc))
            else:
                if 200 <= status < 300:
                    return self._extract(text)
                last = self._error_for(status, text)
                if status not in RETRY_STATUS:
                    raise last
            if attempt < MAX_ATTEMPTS - 1:
                self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        raise last or ChatError("INTERNAL", "大模型调用失败")

    # ------------------------------------------------------------- 内部

    def _error_for(self, status: int, text: str) -> ChatError:
        snippet = (text or "").strip()[:400]
        if status in (401, 403):
            return ChatError("LLM_AUTH", "大模型鉴权失败（api_key 不对或没有权限）", snippet, status)
        if status == 429:
            return ChatError("LLM_RATE_LIMIT", "大模型限流，稍后再试", snippet, status)
        if status in (400, 404, 422):
            return ChatError("LLM_BAD_REQUEST", "大模型拒绝了这次请求（模型名或参数不对）", snippet, status)
        if status >= 500:
            return ChatError("LLM_UPSTREAM", "大模型服务端错误", snippet, status)
        return ChatError("LLM_FAILED", f"大模型调用失败（HTTP {status}）", snippet, status)

    @staticmethod
    def _extract(text: str) -> str:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ChatError("LLM_BAD_RESPONSE", "大模型返回的不是 JSON", (text or "")[:400]) from exc
        choices = data.get("choices") or []
        if not choices:
            # 有些网关把错误塞进 200 的 body 里，这种情况也要说清楚
            raise ChatError("LLM_BAD_RESPONSE", "大模型没有返回内容", json.dumps(data, ensure_ascii=False)[:400])
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数实现返回分段结构
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return str(content or "").strip()
