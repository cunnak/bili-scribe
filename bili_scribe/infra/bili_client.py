"""B站 HTTP 客户端：统一会话、Cookie、WBI 签名、错误码映射与重试。

设计上刻意把"错误"分两层：网络层异常（超时、连不上）与业务层错误（JSON 里的
`code`）。B站 大量接口在 HTTP 200 的情况下返回失败，所以只看状态码一定会漏判。
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from .endpoints import (
    ERROR_HINTS,
    FINGER_SPI,
    NAV,
    QR_GENERATE,
    QR_POLL,
    QR_STATUS_TEXT,
)
from .store import Store
from .wbi import WbiSigner

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 需要持久化的 Cookie。SESSDATA 是登录态，bili_jct 是 csrf，
# buvid3/buvid4 是设备指纹——缺指纹会被风控直接判 -352。
PERSISTED_COOKIES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
)

NETWORK_ERROR_CODE = -1


class BiliError(RuntimeError):
    """B站业务层错误。code 来自响应 JSON，不是 HTTP 状态码。"""

    def __init__(self, code: int, message: str, url: str = ""):
        self.code = code
        self.raw_message = message
        self.url = url
        text = f"[{code}] {message or '未知错误'}"
        hint = ERROR_HINTS.get(code)
        if hint and hint not in (message or ""):
            text += f" —— {hint}"
        super().__init__(text)


class BiliClient:
    def __init__(self, store: Store, timeout: int = 20, logger: Callable[[str], None] | None = None):
        self.store = store
        self.timeout = timeout
        self.log = logger or (lambda _msg: None)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        # 签名器只需要一个"不带签名的取数函数"，避免自引用死循环
        self.wbi = WbiSigner(self._fetch_unsigned)
        self._restore()

    # ------------------------------------------------------------ 凭据管理

    def _restore(self) -> None:
        credential = self.store.get_credential()
        if credential:
            self._apply_cookies(credential["cookies"])
        self.ensure_buvid()

    def _apply_cookies(self, mapping: dict[str, str]) -> None:
        for name, value in mapping.items():
            if value:
                self.session.cookies.set(name, value, domain=".bilibili.com", path="/")

    def _snapshot(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.session.cookies}

    @property
    def cookies(self) -> dict[str, str]:
        return self._snapshot()

    @property
    def csrf(self) -> str:
        return self._snapshot().get("bili_jct", "")

    @property
    def is_logged_in(self) -> bool:
        snapshot = self._snapshot()
        return bool(snapshot.get("SESSDATA") and snapshot.get("bili_jct"))

    def identity(self) -> dict[str, Any] | None:
        """返回当前登录身份；未登录返回 None。"""
        try:
            data = self.get_data(NAV)
        except BiliError:
            return None
        if not isinstance(data, dict) or not data.get("isLogin"):
            return None
        return {
            "mid": data.get("mid"),
            "uname": data.get("uname"),
            "vip": (data.get("vipStatus") == 1),
            "level": data.get("level_info", {}).get("current_level"),
        }

    def ensure_buvid(self) -> str:
        """确保有设备指纹。没有它，带签名的接口会被判风控。"""
        cached = self.store.kv_get("buvid3")
        if cached:
            self._apply_cookies({"buvid3": cached})
            return cached
        try:
            body = self._request_json("GET", FINGER_SPI)
        except BiliError:
            return ""
        data = body.get("data") or {}
        b3 = data.get("b_3") or ""
        b4 = data.get("b_4") or ""
        if b3:
            self.store.kv_set("buvid3", b3)
            self._apply_cookies({"buvid3": b3})
        if b4:
            self._apply_cookies({"buvid4": b4})
        return b3

    def save_session(self) -> dict[str, Any]:
        """把当前会话里的登录凭据持久化。扫码成功或手工填 Cookie 后调用。"""
        snapshot = self._snapshot()
        keep = {k: v for k, v in snapshot.items() if k in PERSISTED_COOKIES and v}
        if not keep.get("SESSDATA"):
            raise BiliError(-101, "登录响应中没有 SESSDATA，无法保存登录态")
        if not keep.get("bili_jct"):
            raise BiliError(-111, "登录响应中没有 bili_jct，后续无法发表评论")

        ident = self.identity() or {}
        self.store.save_credential(
            keep, mid=str(ident.get("mid") or ""), uname=str(ident.get("uname") or "")
        )
        if keep.get("buvid3"):
            self.store.kv_set("buvid3", keep["buvid3"])
        return {"cookies": sorted(keep), "identity": ident}

    def login_with_cookies(self, sessdata: str, bili_jct: str, dedeuserid: str = "") -> dict[str, Any]:
        """手工填 Cookie 的兜底通道。"""
        payload = {"SESSDATA": sessdata.strip(), "bili_jct": bili_jct.strip()}
        if dedeuserid.strip():
            payload["DedeUserID"] = dedeuserid.strip()
        if not all(payload.values()):
            raise BiliError(-400, "SESSDATA 与 bili_jct 都必须填写")

        probe = BiliClient(store=_EphemeralStore(), timeout=self.timeout, logger=self.log)
        probe._apply_cookies(payload)
        ident = probe.identity()
        if not ident:
            raise BiliError(-101, "这组 Cookie 无效或已过期，nav 接口未识别到登录态")

        self._apply_cookies(payload)
        return self.save_session()

    def logout(self) -> None:
        self.session.cookies.clear()
        self.store.clear_credential()

    # ------------------------------------------------------------ 扫码登录

    def qr_generate(self) -> dict[str, Any]:
        body = self._request_json("GET", QR_GENERATE)
        data = body.get("data") or {}
        if not data.get("url"):
            raise BiliError(-1, "二维码生成失败，接口未返回 url")
        return {"url": data["url"], "key": data.get("qrcode_key", "")}

    def qr_poll(self, key: str) -> dict[str, Any]:
        """轮询扫码状态。注意状态码在 data.code 里，外层 code 恒为 0。"""
        try:
            resp = self.session.get(
                QR_POLL, params={"qrcode_key": key}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise BiliError(NETWORK_ERROR_CODE, f"轮询失败：{exc}", QR_POLL) from exc
        try:
            body = resp.json()
        except ValueError:
            raise BiliError(NETWORK_ERROR_CODE, "轮询返回非 JSON", QR_POLL)

        data = body.get("data") or {}
        status = data.get("code")
        # 扫码成功的 Set-Cookie 已经自动落进 session，这里只需读状态
        return {
            "status": status,
            "message": data.get("message") or "",
            "status_text": QR_STATUS_TEXT.get(status, "未知状态"),
        }

    # ------------------------------------------------------------ 请求底层

    def _fetch_unsigned(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", url, params=params)

    def _request_json(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        attempts = 2 if signed else 1
        last_error: BiliError | None = None

        for attempt in range(attempts):
            query = self.wbi.sign(params or {}) if signed else dict(params or {})
            try:
                resp = self.session.request(
                    method, url, params=query, data=data, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise BiliError(NETWORK_ERROR_CODE, f"网络请求失败：{exc}", url) from exc

            if resp.status_code >= 400:
                raise BiliError(
                    NETWORK_ERROR_CODE,
                    f"HTTP {resp.status_code}：{resp.text[:200]}",
                    url,
                )

            try:
                body = resp.json()
            except ValueError:
                raise BiliError(
                    NETWORK_ERROR_CODE,
                    "接口未返回 JSON（通常是被风控页拦截或该接口已下线）",
                    url,
                )

            code = body.get("code")
            if code == 0:
                return body

            error = BiliError(code, str(body.get("message", "")), url)
            # -352 多半是签名密钥过期，强制刷新后重试一次
            if code == -352 and signed and attempt + 1 < attempts:
                self.log("WBI 签名被拒（-352），刷新密钥后重试")
                self.wbi.ensure_key(force=True)
                last_error = error
                continue
            raise error

        raise last_error or BiliError(-352, "签名重试后仍然失败", url)

    def get_raw(self, url: str, params: dict[str, Any] | None = None, signed: bool = False) -> dict[str, Any]:
        return self._request_json("GET", url, params=params, signed=signed)

    def get_data(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        default: Any = None,
    ) -> Any:
        body = self._request_json("GET", url, params=params, signed=signed)
        data = body.get("data")
        return default if data is None else data

    def post_form(
        self,
        url: str,
        data: dict[str, Any],
        signed: bool = False,
        with_csrf: bool = True,
    ) -> Any:
        payload = dict(data)
        if with_csrf and "csrf" not in payload:
            if not self.csrf:
                raise BiliError(-111, "缺少 bili_jct，无法提交带 csrf 的请求")
            payload["csrf"] = self.csrf
        body = self._request_json("POST", url, data=payload, signed=signed)
        return body.get("data")

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BiliError(NETWORK_ERROR_CODE, f"下载失败：{exc}", url) from exc
        if resp.status_code >= 400:
            raise BiliError(NETWORK_ERROR_CODE, f"HTTP {resp.status_code}", url)
        return resp.content

    def get_json_url(self, url: str) -> Any:
        """下载一个绝对 URL 的 JSON（例如字幕文件，托管在 CDN 上不需要凭据）。"""
        if url.startswith("//"):
            url = "https:" + url
        try:
            resp = self.session.get(
                url,
                timeout=self.timeout,
                headers={"Referer": "https://www.bilibili.com/"},
            )
        except requests.RequestException as exc:
            raise BiliError(NETWORK_ERROR_CODE, f"字幕文件下载失败：{exc}", url) from exc
        try:
            return resp.json()
        except ValueError:
            raise BiliError(NETWORK_ERROR_CODE, "字幕文件不是 JSON", url)

    def expand_short_link(self, url: str) -> str:
        """展开 b23.tv 短链，返回最终落地地址。"""
        try:
            resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BiliError(NETWORK_ERROR_CODE, f"短链展开失败：{exc}", url) from exc
        return resp.url


class _EphemeralStore(Store):
    """用于校验外部 Cookie 的临时存储：只借 KV，不落盘真实凭据。"""

    def __init__(self) -> None:
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp(prefix="bili-scribe-probe-"))
        super().__init__(str(tmp / "probe.db"), str(tmp / "probe.key"), logger=lambda _m: None)

    def save_credential(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None
