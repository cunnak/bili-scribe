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
import uuid
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any, Callable, Iterable, Mapping, Protocol

from ..app.errors import WBError

# 传输层签名：(url, payload_bytes, headers, timeout) -> (status, body_text)
Transport = Callable[[str, bytes, Mapping[str, str], float], tuple[int, str]]
# GET 用另一条签名（没有 body）。分开而不是给 Transport 加 method 参数：
# 加参数就得让已有的假传输层全部多接一个位置参数，改动面比新增一条大。
Getter = Callable[[str, Mapping[str, str], float], tuple[int, str]]

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

# 默认 UA。为什么不能用 urllib 自带的那个：
# `Python-urllib/3.x` 命中 Cloudflare 防火墙的 "bad browser signature"（Error 1010），
# 会在**密钥完全正确**的情况下直接 403——用户看到的就是「别处能用，这里不能用」。
# 默认 UA 必须长得像浏览器：Cloudflare 的 1010 规则按「客户端指纹」放行或拦截。
# 实测同一把密钥、同一个地址：VideoWorkbench/0.1 一律 403，换成下面这串就 200。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

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
        opener = _opener_for(proxy, url)
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


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", ""}


def _is_local_url(url: str) -> bool:
    """判断目标是不是本机/内网地址（这类请求永远不该走代理）。"""
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 127 or a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
        if a == 169 and b == 254:
            return True
    return False


def _opener_for(proxy: str, url: str) -> urllib.request.OpenerDirector:
    """按「目标地址 + llm.proxy」装配 opener，**不让环境变量偷偷决定走向**。

    Windows 上 ``urllib`` 的两半是分裂的：``getproxies()`` 认 ``HTTP_PROXY`` 环境变量，
    而 ``proxy_bypass()`` 读的是 IE/注册表设置、**不认 ``NO_PROXY``**。于是
    「发了 ``HTTP_PROXY=127.0.0.1:7897``、又写了 ``NO_PROXY=127.0.0.1``」这种最常见的配置，
    发往本机网关的请求仍会被塞进代理，最后拿回一个 Cloudflare 403 ——
    报的是「CDN 判定你是机器人」，跟真实原因（请求根本没到本机）隔着一整条链路。

    所以：本机/内网地址一律 ``ProxyHandler({})`` 直连；对端是公网时才按 ``llm.proxy`` 走，
    没配就沿用环境变量（那是用户唯一能表达「我要走代理」的地方）。
    """
    proxy = (proxy or "").strip()
    if _is_local_url(url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _urllib_get(proxy: str = "") -> Getter:
    """默认 GET：列模型用。和 ``_urllib_transport`` 共用代理规则（只认 llm.proxy）。"""

    def send(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, str]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        opener = _opener_for(proxy, url)
        try:
            with opener.open(request, timeout=timeout) as resp:
                return int(resp.status), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            return int(exc.code), body

    return send


def normalize_models(payload: Any) -> list[dict[str, str]]:
    """把各家 ``GET /models`` 的返回归一成 ``[{"id","owned_by"}]``。

    见过的三种形状（都不是猜测，是真跑出来的）：

    * 标准 OpenAI：``{"object":"list","data":[{"id":"gpt-4o","owned_by":"openai"}]}``
    * 网关常用：``{"models":["deepseek-chat", {...}]}``
    * 直给数组：``["qwen-max", "qwen-plus"]``

    统一按出现顺序去重（网关有时会重复列出同一个 id）。
    """
    raw: Any
    if isinstance(payload, dict):
        raw = payload.get("data")
        if raw is None:
            raw = payload.get("models")
        if raw is None:
            # 有的实现把列表塞在别的键里，只剩一个 list 值的情况也认
            lists = [v for v in payload.values() if isinstance(v, list)]
            raw = lists[0] if len(lists) == 1 else []
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            mid, owner = entry.strip(), ""
        elif isinstance(entry, dict):
            mid = str(entry.get("id") or entry.get("name") or entry.get("model") or "").strip()
            owner = str(entry.get("owned_by") or entry.get("owner") or "")
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "owned_by": owner})
    return out


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
        getter: Getter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        provider_name: str = "",
        user_agent: str = "",
        extra_headers: Mapping[str, str] | None = None,
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
        self.user_agent = (user_agent or "").strip() or DEFAULT_USER_AGENT
        self.extra_headers = {
            str(k): str(v) for k, v in dict(extra_headers or {}).items() if str(k).strip()
        }
        self._session_id = ""
        self._getter = getter or _urllib_get(self.proxy)
        self._sleep = sleep

    # ------------------------------------------------------------- 列模型

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        """按网关要求拼请求头。

        ``extra_headers`` 是给「需要私有请求头」的网关留的通用旋钮。
        ``opencode.ai`` 那家另外有一个固定的坑：``chat/completions`` 必须带
        ``x-opencode-session``（任意字符串都行，缺了回 400 MissingSessionID），
        这里给它补一个进程内稳定的值，用户不必自己去读文档。
        """
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        headers.update(self.extra_headers)
        lowered = {k.lower() for k in headers}
        if "opencode.ai" in self.base_url.lower() and "x-opencode-session" not in lowered:
            if not self._session_id:
                self._session_id = "wb-" + uuid.uuid4().hex[:16]
            headers["x-opencode-session"] = self._session_id
        return headers

    def list_models(self) -> list[dict[str, str]]:
        """``GET {base_url}/models`` —— 这家密钥能用的模型有哪些。

        为什么要单独一个方法而不是塞进 ``chat``：这条路径**不需要 model**，
        用户正是在「还不知道有哪些模型」的时候点它。所以只校验地址与密钥，
        不走 ``require()``（那会把"没填模型"也算作没配置）。

        失败一律翻成 ``ChatError``，码和 ``chat`` 同一套：界面上 401 是
        「密钥不对」、连不上是 NETWORK，不需要另写一套文案。
        """
        missing = [
            name
            for name, value in (("base_url", self.base_url), ("api_key", self.api_key))
            if not value
        ]
        if missing:
            who = f"：{self.provider_name}" if self.provider_name else ""
            raise ChatError(
                "LLM_NOT_CONFIGURED",
                f"服务商还没有配置完整{who}",
                "缺：" + "、".join(missing),
            )
        url = f"{self.base_url}/models"

        headers = self._headers(json_body=False)
        try:
            status, text = self._getter(url, headers, self.timeout)
        except TimeoutError as exc:
            raise ChatError("LLM_TIMEOUT", "获取模型列表超时", str(exc)) from exc
        except OSError as exc:
            raise ChatError("NETWORK", "连不上大模型服务", str(exc)) from exc
        if not (200 <= status < 300):
            raise self._error_for(status, text)
        try:
            payload = json.loads(text or "null")
        except ValueError as exc:
            raise ChatError("LLM_BAD_RESPONSE", "模型列表返回的不是 JSON", (text or "")[:400]) from exc
        models = normalize_models(payload)
        if not models:
            # 200 但空列表：多半是网关不实现 /models（有的只认 chat 路径）。
            # 不当成成功——否则界面上会弹出「0 个模型」的空窗，用户以为坏了。
            raise ChatError(
                "LLM_BAD_RESPONSE",
                "这家没有返回可用模型列表",
                "该地址可能不支持 /models，手动填模型名即可",
            )
        return models

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
        picked = None
        if provider:
            from video_workbench.config import find_provider

            picked = find_provider(llm, provider)
            # 指名了一家不存在的服务商时**不要**造一个空客户端：那样报出来的是
            # 「这家还没填接口地址和密钥」，指的是一个根本不存在的服务商，把排查方向
            # 引到填表上去了。这里安静退到「任务/默认」那条路（与 resolve_task_model
            # 的「服务商写错就退下一条」一致），provider_name 仍带上用户写的名字，
            # 报错文案里能看出是哪一次点名没对上。
        if picked is not None:
            model = str(getattr(llm, "model", "") or "")
            if picked.models:
                model = model if model in picked.models else picked.models[0]
            return cls(
                base_url=picked.base_url,
                api_key=picked.api_key,
                model=model,
                timeout=float(getattr(llm, "timeout_seconds", 120) or 120),
                proxy=str(getattr(llm, "proxy", "") or ""),
                transport=transport,
                provider_name=picked.name,
                user_agent=str(getattr(llm, "user_agent", "") or ""),
                extra_headers=dict(getattr(llm, "extra_headers", {}) or {}),
            )
        provider_obj, model = resolve_task_model(llm, task)
        return cls(
            base_url=str(getattr(provider_obj, "base_url", "") or getattr(llm, "base_url", "") or ""),
            api_key=str(getattr(provider_obj, "api_key", "") or getattr(llm, "api_key", "") or ""),
            model=str(model or ""),
            timeout=float(getattr(llm, "timeout_seconds", 120) or 120),
            proxy=str(getattr(llm, "proxy", "") or ""),
            transport=transport,
            provider_name=str(getattr(provider_obj, "name", "") or "") or (provider or ""),
            user_agent=str(getattr(llm, "user_agent", "") or ""),
            extra_headers=dict(getattr(llm, "extra_headers", {}) or {}),
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

        headers = self._headers(json_body=True)

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
        if status == 401:
            return ChatError("LLM_AUTH", "大模型鉴权失败（api_key 不对或没有权限）", snippet, status)
        if status == 403:
            # 403 ≠ 密钥错。最常见是 CDN 按客户端指纹/UA 拦人（Cloudflare 1010）。
            # 文案必须把这条说清，否则用户会一直怀疑自己的 key。
            low = snippet.lower()
            if "cloudflare" in low or "1010" in low or "blocked access" in low:
                why = "被服务端前面的 CDN 拦了（Cloudflare 1010：按客户端指纹/UA 判定为机器人）"
                tip = "密钥本身没问题。可在设置里填代理再试；或在「上下文与长素材」填一个自己的 llm.user_agent"
            else:
                why = "服务端拒绝了这次请求（403，不是密钥格式问题）"
                tip = "该 key 可能没有这个接口或模型的权限"
            return ChatError("LLM_FORBIDDEN", why, (tip + " | " + snippet)[:600], status)
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
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数实现返回分段结构
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        out = str(content or "").strip()
        if out:
            return out

        # 空正文不能当成功返回。推理型模型（magpie 的 auto/*、deepseek-reasoner 等）
        # 在 max_tokens 不够时会把预算全烧在 reasoning 上，content 给 null、
        # finish_reason 是 length——上层拿到空串只会以为「模型不想说话」。
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        details = (data.get("usage") or {}).get("completion_tokens_details") or {}
        spent = details.get("reasoning_tokens")
        if reasoning or spent:
            raise ChatError(
                "LLM_EMPTY",
                "模型这次只输出了思考过程，正文是空的（预算被思考吃掉了）",
                (
                    "finish_reason=%s，思考用了 %s tokens。把 max_tokens 调大，"
                    "或换一个非推理模型，再试。" % (choice.get("finish_reason") or "?", spent or "?")
                ),
            )
        raise ChatError(
            "LLM_EMPTY",
            "模型没有返回正文",
            json.dumps(data, ensure_ascii=False)[:400],
        )
