"""大模型适配器。只依赖 OpenAI 兼容的 /chat/completions，不绑定具体厂商。

这个模块里有一条非显然的规则，值得先读：

**思考模型的输出预算陷阱。** 部分模型（Qwen3 系列等）会先产出一大段推理再作答，
而这段推理**不受 `max_tokens` 约束**。实测：`max_tokens=4096`，实际
`completion_tokens=5125`，其中 5120 是思考，正文只有一个 `[]`。
后果是两个都不好查的故障——
  1) 耗时失控，撞上读超时（表现是 ReadTimeout，看起来像网络问题）；
  2) 预算被思考吃光，正文为空，但 HTTP 200、`finish_reason=stop`，
     从响应表面完全看不出异常。
对策是三层：允许用 `enable_thinking` 直接关掉；发现"空正文 + 有思考 token"
时自动关掉重试一次；把超时错误写成可照做的建议而不是裸的 ReadTimeout。

还有一条同样非显然的规则：**代理只能来自 `llm.proxy`，绝不来自环境变量。**
`requests` 默认 `trust_env=True`，会读 `HTTP_PROXY` / `HTTPS_PROXY`，于是
"走不走代理"实际由**启动程序的那个 shell** 决定。踩过的坑是：从一个设了代理的
会话里启动，请求被送进一个连不到目标站点的代理，界面报
`ProxyError / Tunnel connection failed: 502`——看起来像模型服务挂了，
其实请求压根没出去。所以这里 `trust_env = False`，并在代理失败时把
"这不是模型服务的问题"直接写进错误信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import requests

from ..config import LlmConfig

THINKING_PARAM = "enable_thinking"
DEFAULT_TIMEOUT = 300
# 端点不认识某个附加参数时会用这些状态码拒绝
PARAM_REJECT_CODES = (400, 404, 415, 422)


class LlmError(RuntimeError):
    pass


class LlmHttpError(LlmError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"模型接口返回 HTTP {status_code}：{body[:300]}")


@dataclass
class ChatResult:
    """一次调用的结果，连同 usage 里的诊断信息。

    `reasoning_tokens` 是关键：它是唯一能举证"输出预算被思考吃光"的证据。
    """

    content: str
    completion_tokens: int = 0
    reasoning_tokens: int = 0


class LlmClient:
    def __init__(self, cfg: LlmConfig, timeout: int | None = None):
        self.cfg = cfg
        configured_timeout = getattr(cfg, "timeout_seconds", None) or DEFAULT_TIMEOUT
        self.timeout = int(timeout or configured_timeout)
        self.session = requests.Session()
        # 关掉 trust_env：requests 默认会去读 HTTP_PROXY / HTTPS_PROXY，
        # 那会让"走不走代理"由**启动程序的 shell**决定，而不是由配置决定。
        # 实测踩过：从一个设了代理的会话里启动，请求被塞进一个连不到目标站点的
        # 代理，界面报 "ProxyError / Tunnel connection failed: 502"，
        # 看起来像模型服务挂了，其实是本机代理路由错误。
        # 代理只能来自 llm.proxy，绝不来自环境。
        self.session.trust_env = False
        proxy = (getattr(cfg, "proxy", "") or "").strip()
        self.proxies: dict[str, str] | None = (
            {"http": proxy, "https": proxy} if proxy else None
        )
        # 最近一次调用的过程说明（例如"已自动关闭思考重试"），供界面展示。
        # 这些是"发生过但没失败"的事情，不记下来用户就不知道模型被降级过。
        self.notes: list[str] = []

    @property
    def configured(self) -> bool:
        return bool(self.cfg.api_key and self.cfg.base_url and self.cfg.model)

    def describe(self) -> str:
        route = self.proxies["https"] if self.proxies else "直连"
        return f"{self.cfg.model} @ {self.cfg.base_url}（{route}）"

    # ------------------------------------------------------------ 载荷

    def _payload(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
        thinking: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if thinking == "off":
            payload[THINKING_PARAM] = False
        elif thinking == "on":
            payload[THINKING_PARAM] = True
        return payload

    def _note(self, sink: Callable[[str], None] | None, message: str) -> None:
        self.notes.append(message)
        if sink:
            sink(message)

    # ------------------------------------------------------------ 收发

    def _post(self, url: str, payload: dict[str, Any]) -> ChatResult:
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
                proxies=self.proxies,
            )
        except requests.exceptions.ProxyError as exc:
            # 代理失败长得非常像"模型服务挂了"（502 Bad Gateway / Unable to connect
            # to proxy），实际上请求压根没出去。这里把话说明白。
            where = self.proxies["https"] if self.proxies else "（未配置代理）"
            raise LlmError(
                f"代理连接失败：{exc}。当前 llm.proxy = {where}。"
                "这通常不是模型服务的问题——先确认该代理能连上 "
                f"{self.cfg.base_url}，连不上就换一个代理，或者留空直连。"
            ) from exc
        except requests.exceptions.ConnectTimeout as exc:
            # 必须排在 requests.Timeout 前面：ConnectTimeout 是它的子类，
            # 被它先接住的话，连不上站点的场景会得到"把 thinking 改成 off"
            # 这种完全不相干的建议，反而把人带偏。
            route = self.proxies["https"] if self.proxies else "直连"
            raise LlmError(
                f"连不上 {self.cfg.base_url}（{route}）：{exc}。"
                "如果这个站点直连一直超时、而别的网站正常，多半是域名解析被污染"
                "——在设置页的 llm.proxy 里填一个能连上它的代理再试。"
            ) from exc
        except requests.Timeout as exc:
            raise LlmError(
                f"模型请求超时：{self.timeout} 秒内没有拿到响应。"
                "思考模型最容易撞这个——把 llm.thinking 设为 off 通常能根治；"
                "也可以调大 llm.timeout_seconds，或调小 llm.max_input_chars。"
            ) from exc
        except requests.RequestException as exc:
            raise LlmError(f"模型请求失败：{exc}") from exc

        if resp.status_code >= 400:
            raise LlmHttpError(resp.status_code, resp.text)

        try:
            body = resp.json()
        except ValueError:
            raise LlmError(f"模型返回非 JSON：{resp.text[:300]}")

        choices = body.get("choices") or []
        if not choices:
            raise LlmError(f"模型未返回 choices：{str(body)[:300]}")

        message = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return ChatResult(
            content=str(message.get("content") or "").strip(),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        )

    # ------------------------------------------------------------ 对外

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        note: Callable[[str], None] | None = None,
    ) -> str:
        if not self.configured:
            raise LlmError("尚未配置大模型 api_key，请在 config.yaml 的 llm 段填写")

        self.notes = []
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        thinking = getattr(self.cfg, "thinking", "auto") or "auto"

        try:
            result = self._post(
                url, self._payload(messages, temperature, max_tokens, json_mode, thinking)
            )
        except LlmHttpError as exc:
            # 两条降级路径，彼此独立：
            #   1) 端点不认 response_format（不少兼容端点如此）
            #   2) 端点不认 enable_thinking（非 Qwen 端点、或非思考模型）
            # 逐个摘掉重试，而不是直接失败。
            if exc.status_code not in PARAM_REJECT_CODES:
                raise
            payload = self._payload(messages, temperature, max_tokens, json_mode, thinking)
            dropped: list[str] = []
            if json_mode and "response_format" in payload:
                payload.pop("response_format", None)
                dropped.append("response_format")
            if thinking != "auto" and THINKING_PARAM in payload:
                payload.pop(THINKING_PARAM, None)
                dropped.append(THINKING_PARAM)
            if not dropped:
                raise
            self._note(note, "端点不接受 " + "、".join(dropped) + "，已去掉该参数重试")
            result = self._post(url, payload)

        # 正文为空、却烧掉了思考 token：思考把输出预算吃光了。
        # 这种失败在 HTTP 层完全看不出来（200 + finish_reason=stop + 正文空），
        # 只有 usage 里的 reasoning_tokens 能举证。
        if not result.content and result.reasoning_tokens > 0 and thinking != "off":
            self._note(
                note,
                f"模型把 {result.reasoning_tokens} 个 token 全用在思考上、正文返回为空，"
                "已自动关闭思考重试",
            )
            result = self._post(
                url, self._payload(messages, temperature, max_tokens, json_mode, "off")
            )

        if not result.content:
            raise LlmError(
                "模型返回内容为空。若用的是思考模型，请把 llm.thinking 设为 off；"
                "否则检查模型名是否被该端点支持。"
            )
        return result.content

    def health_check(self) -> tuple[bool, str]:
        """给前端一个"模型通不通"的快速探针。

        max_tokens 给 64 而不是更小：思考模型连"回复两个字"都可能先想掉几十个 token，
        预算太小会走一次没必要的关思考重试。
        """
        if not self.configured:
            return False, "未配置 api_key"
        try:
            reply = self.chat(
                [{"role": "user", "content": "回复两个字：可用"}],
                max_tokens=64,
                temperature=0,
            )
        except LlmError as exc:
            return False, str(exc)
        extra = "；" + "；".join(self.notes) if self.notes else ""
        return True, reply.strip()[:40] + extra
