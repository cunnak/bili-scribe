"""大模型适配器。只依赖 OpenAI 兼容的 /chat/completions，不绑定具体厂商。"""

from __future__ import annotations

import requests

from ..config import LlmConfig


class LlmError(RuntimeError):
    pass


class LlmHttpError(LlmError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"模型接口返回 HTTP {status_code}：{body[:300]}")


class LlmClient:
    def __init__(self, cfg: LlmConfig, timeout: int = 180):
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.cfg.api_key and self.cfg.base_url and self.cfg.model)

    def describe(self) -> str:
        return f"{self.cfg.model} @ {self.cfg.base_url}"

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        if not self.configured:
            raise LlmError("尚未配置大模型 api_key，请在 config.yaml 的 llm 段填写")

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload: dict = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            return self._post(url, payload)
        except LlmHttpError as exc:
            # 不少兼容端点不支持 response_format，降级重试一次而不是直接失败
            if json_mode and exc.status_code in (400, 404, 422):
                payload.pop("response_format", None)
                return self._post(url, payload)
            raise

    def _post(self, url: str, payload: dict) -> str:
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
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
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            raise LlmError("模型返回内容为空")
        return content

    def health_check(self) -> tuple[bool, str]:
        """给前端一个"模型通不通"的快速探针。"""
        if not self.configured:
            return False, "未配置 api_key"
        try:
            reply = self.chat(
                [{"role": "user", "content": "回复两个字：可用"}],
                max_tokens=16,
                temperature=0,
            )
        except LlmError as exc:
            return False, str(exc)
        return True, reply.strip()[:40]
