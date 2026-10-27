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

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from ..config import LlmConfig

THINKING_PARAM = "enable_thinking"
DEFAULT_TIMEOUT = 300
# 端点不认识某个附加参数时会用这些状态码拒绝
PARAM_REJECT_CODES = (400, 404, 415, 422)

# 瞬时断链的重试策略。**刻意做成模块常量而不是配置项**：它不是使用者的偏好，
# 而是"一次网络抖动该不该让一整版报废"这个正确性问题。放进设置页会多出
# 前端字段、请求体字段、回写模板三处要同步维护，而且没人知道该填几。
# 要改就改这里，一处生效。
#
# **只重试传输层没拿到响应的错误**（TLS 被掐、代理隧道 EOF、连接被重置），
# 不重试超时、也不重试 HTTP 状态码错误，理由分别是：
#   - 超时：它已经等满了一整轮 timeout_seconds。再等两轮只是把 5 分钟变成
#     15 分钟，而没有任何证据表明下一轮会更快。慢的模型就该按慢处理。
#   - HTTP 4xx/5xx：响应体里已经写明了原因，交给上层给人看；盲目重试会在
#     "请求本身就错"的情况下白烧三次额度。
#
# 代价要说清楚：发生在"请求可能已经到达服务端"的时刻（比如 SSL EOF 出现在
# 请求体发出之后），重试会让同一次调用被计费两遍。实测这种抖动时长只有
# 0.1–0.3 秒、且失败率能到 6/6，比起"一整版素材重跑"（几万 token）来，
# 重复计费一次是更便宜的代价。
RETRY_ATTEMPTS = 3
# 每次重试前等多久（秒）。第一次短一点：抖动通常就是几百毫秒的事，
# 等太久反而让界面看起来像卡住了。
RETRY_BACKOFF = (0.8, 2.0)

# 判定"这次失败值不值得重试"时要在里面找的关键词。
# 只看异常类型不够：`ProxyError` 既可能是"代理端口根本没开"（重试无用，
# 应该立刻让人去改配置），也可能是"代理收下连接又把它掐断"（正是要重试的那种）。
# 观测到的那条真实错误是后者：
#   ProxyError('Unable to connect to proxy',
#              RemoteEndClosed('Remote end closed connection without response'))
# 两者的区别只在 `reason` 里，所以必须把整条因果链摊成文本再判断。
TRANSIENT_MARKS = (
    "remote end closed",
    "remotedisconnected",
    "connection reset",
    "connection aborted",
    "remote disconnected",
    "broken pipe",
    "eof occurred",
    "unexpected_eof",
    "protocolerror",
)


def _error_blob(exc: BaseException) -> str:
    """把一条异常的完整因果链压成小写文本，供关键词判定。"""
    seen: list[str] = []
    node: BaseException | None = exc
    depth = 0
    while node is not None and depth < 6:
        seen.append(f"{type(node).__name__} {node!r} {node}")
        inner = getattr(node, "reason", None)  # urllib3 把根因挂在 reason 上
        if isinstance(inner, BaseException):
            seen.append(f"{type(inner).__name__} {inner!r} {inner}")
            inner = None
        node = node.__cause__ or node.__context__
        depth += 1
    return " ".join(seen).lower()


def _is_transient(exc: requests.RequestException) -> bool:
    """这次传输层失败，值不值得再试一遍。

    判断依据是"下一轮有没有可能不一样"，不是"错误看起来严不严重"：

    - **超时类一律不重试**（含 ConnectTimeout / ReadTimeout）。它已经等满了一整轮
      `timeout_seconds`；再等两轮只是把 5 分钟变成 15 分钟，而且没有任何证据
      表明下一轮会更快。慢的模型就该按慢处理。
    - **SSL 握手/读响应被断开**：要重试。这是实测里出现最多的形态
      （`SSLEOFError: EOF occurred in violation of protocol`），0.1–0.3 秒就断，
      重试成功率很高。
    - **代理错误**：分两种，靠因果链文本区分——端口没开（连接被拒）不重试，
      收下又掐断（remote closed / reset / EOF）要重试。
    - 其余 `ConnectionError`（不含上面两类）：按瞬时处理。
    """
    # 顺序有讲究：Timeout 和 SSLError、ProxyError 都是 ConnectionError 的子类，
    # 放后面会被 ConnectionError 一把兜走，分不出差别。
    if isinstance(exc, requests.exceptions.Timeout):
        return False
    if isinstance(exc, requests.exceptions.SSLError):
        return True
    blob = _error_blob(exc)
    if isinstance(exc, requests.exceptions.ProxyError):
        return any(mark in blob for mark in TRANSIENT_MARKS)
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    return False


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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass
class Usage:
    """一次任务（往往由很多次调用组成）的累计消耗。

    为什么要把账记在这个粒度上：迭代打磨卖的正是"同一份素材改很多版"，而使用者
    唯一能判断这件事值不值的依据，就是每一版各花了多少。没有这个数字，界面上
    只剩一个「再改一版」按钮，它的代价完全是隐形的。

    `unpriced` 是**未被计价的调用数**：不少兼容端点（尤其是中转）压根不返回
    usage，那时计数会是 0，看起来像"不要钱"。把"这几笔没记账"单独说出来，
    比给一个漂亮的 0 诚实。
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    unpriced: int = 0

    def add(self, result: "ChatResult") -> None:
        self.calls += 1
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.reasoning_tokens += result.reasoning_tokens
        self.total_tokens += result.total_tokens or (
            result.prompt_tokens + result.completion_tokens
        )
        if not (result.prompt_tokens or result.completion_tokens or result.total_tokens):
            self.unpriced += 1

    def merge(self, other: dict[str, Any] | None) -> None:
        """把一份 to_dict() 的结果加进来。汇总历史版本时用。"""
        if not other:
            return
        for name in ("calls", "prompt_tokens", "completion_tokens",
                     "reasoning_tokens", "total_tokens", "unpriced"):
            setattr(self, name, getattr(self, name) + int(other.get(name) or 0))

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "unpriced": self.unpriced,
        }

    def describe(self) -> str:
        parts = [f"{self.calls} 次调用"]
        if self.prompt_tokens or self.completion_tokens:
            parts.append(f"输入 {self.prompt_tokens} / 输出 {self.completion_tokens} tokens")
        if self.total_tokens:
            parts.append(f"合计 {self.total_tokens} tokens")
        if self.unpriced:
            parts.append(f"其中 {self.unpriced} 次端点未返回 usage，无法计价")
        return "，".join(parts)


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
        # 这个实例上的累计消耗。**刻意挂在实例上**：调用方按任务／按次迭代各建一个
        # 实例，账就不会在并发任务之间串起来（见 AppState.new_llm）。
        self.usage = Usage()
        # 重试提示要送到界面，但重试发生在 _post 里、拿不到 chat() 的 note 参数，
        # 所以每次 chat 开头把它挂上来（见 chat）。
        self._note_sink: Callable[[str], None] | None = None

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
        """发送并**在瞬时断链时重试**，耗尽次数后把原始异常翻成人话抛出。

        为什么重试要做在这一层而不是更上面：`chat()` 一次调用可能是"生成 →
        自我批判 → 修订"三轮里的任一轮，也可能属于分块归约的十几段之一。
        断在哪一轮都意味着整版素材白跑——而实测这条线路的抖动时长只有
        0.1–0.3 秒。在这里重试，代价是一次调用；不在这里重试，代价是整版重来。

        只接 `requests.RequestException`：HTTP 状态码错误和响应格式问题都是
        `LlmError`（由 `_post_once` 直接抛），不该被重试——它们重试也不会变。
        """
        last_exc: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return self._post_once(url, payload)
            except LlmError:
                # HTTP 4xx/5xx、返回非 JSON、没有 choices：重试是白烧额度。
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 >= RETRY_ATTEMPTS or not _is_transient(exc):
                    break
                # 第 n 次重试前等 RETRY_BACKOFF[n]。次数用尽时按最后一个值等，
                # 免得把 RETRY_BACKOFF 改短之后出现 IndexError。
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                reason = type(exc).__name__
                self._note(
                    self._note_sink,
                    f"网络中断（{reason}），{delay:g} 秒后重试"
                    f"（第 {attempt + 2}/{RETRY_ATTEMPTS} 次）",
                )
                time.sleep(delay)
        assert last_exc is not None  # RETRY_ATTEMPTS >= 1 时走不到别处
        self._fail(last_exc)
        raise AssertionError("unreachable")  # _fail 必抛，这里是给类型看的

    def _fail(self, exc: requests.RequestException) -> None:
        """把传输层异常翻成"用户能照着做"的错误。永不返回。"""
        if isinstance(exc, requests.exceptions.ProxyError):
            # 代理失败长得非常像"模型服务挂了"（502 Bad Gateway / Unable to connect
            # to proxy），实际上请求压根没出去。这里把话说明白。
            where = self.proxies["https"] if self.proxies else "（未配置代理）"
            raise LlmError(
                f"代理连接失败：{exc}。当前 llm.proxy = {where}。"
                "这通常不是模型服务的问题——先确认该代理能连上 "
                f"{self.cfg.base_url}，连不上就换一个代理，或者留空直连。"
            ) from exc
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            # 必须排在 requests.Timeout 前面：ConnectTimeout 是它的子类，
            # 被它先接住的话，连不上站点的场景会得到"把 thinking 改成 off"
            # 这种完全不相干的建议，反而把人带偏。
            route = self.proxies["https"] if self.proxies else "直连"
            raise LlmError(
                f"连不上 {self.cfg.base_url}（{route}）：{exc}。"
                "如果这个站点直连一直超时、而别的网站正常，多半是域名解析被污染"
                "——在设置页的 llm.proxy 里填一个能连上它的代理再试。"
            ) from exc
        if isinstance(exc, requests.Timeout):
            raise LlmError(
                f"模型请求超时：{self.timeout} 秒内没有拿到响应。"
                "思考模型最容易撞这个——把 llm.thinking 设为 off 通常能根治；"
                "也可以调大 llm.timeout_seconds，或调小 llm.max_input_chars。"
            ) from exc
        if isinstance(exc, requests.exceptions.SSLError):
            # 实测多发的形态：走代理时对端在 TLS 握手/读响应中断开，
            # 报 SSLEOFError "EOF occurred in violation of protocol"。
            where = self.proxies["https"] if self.proxies else "直连"
            raise LlmError(
                f"模型请求失败：建立加密连接时被对端断开（{where}）：{exc}。"
                f"已自动重试 {RETRY_ATTEMPTS} 次仍未成功——这种情况多半是代理线路"
                "在抖，稍后重试，或换一个代理。"
            ) from exc
        raise LlmError(f"模型请求失败：{exc}") from exc

    def _post_once(self, url: str, payload: dict[str, Any]) -> ChatResult:
        """发一次，不重试。传输层异常原样抛给 `_post`。"""
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        resp = self.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout,
            proxies=self.proxies,
        )
        return self._parse(resp)

    def _parse(self, resp: Any) -> ChatResult:
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
        result = ChatResult(
            content=str(message.get("content") or "").strip(),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
        # 记账点放这儿、而不是 chat() 的出口：钱是在这一次 HTTP 往返里花掉的，
        # 哪怕它的正文随后因为"思考吃光预算"被丢弃重试（见 chat），那一笔也花了。
        self.usage.add(result)
        return result

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
        # 重试发生在 _post 里，拿不到 chat 的 note 参数，所以把 sink 挂到实例上。
        # 不这么做的话"刚才重试了两次"只进 self.notes、不进界面进度——
        # 而用户此刻正盯着一动不动的一行字，最该看到的就是这句话。
        self._note_sink = note
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
