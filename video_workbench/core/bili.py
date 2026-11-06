"""复用 bili-scribe 的登录态与 HTTP 客户端。

**为什么不自己重写一套请求层。** wbi 签名、csrf、cookie 刷新这些坑，bili-scribe 已经
踩完并修完了。工作台要做的是"调它"，不是"再踩一遍"。硬约束也明确：不改 ``bili_scribe/``
的业务代码——所以这里只 ``import`` 它的 ``Store`` 与 ``BiliClient``，一个字节都不动。

**为什么客户端要懒建且单例。** ``BiliClient`` 初始化会读加密凭据文件（要解密 + 建表），
几十毫秒起步；而首屏的 ``app.bootstrap`` 根本不需要 B站。等到真的要解析链接了再建，
没登录的用户也不会因为"读不到 key.bin"而打不开程序。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from bili_scribe.infra.bili_client import BiliClient, BiliError
from bili_scribe.infra.store import Store

from ..app.errors import WBError

# B站接口的负数码里有几个是有明确含义的，其余一律当 BILI_ERROR。
_NOT_FOUND_CODES = {-404, 62002, 62004}  # 稿件不存在 / 已删除 / 审核中不可见
_AUTH_CODES = {-101, -403, -352, 87007, 87008}  # 未登录 / 权限不足 / 风控


def map_bili_error(exc: Exception, action: str = "请求") -> WBError:
    """把 bili-scribe 的异常翻成工作台的错误码。

    分不清的就不猜：看不清原因的统一落到 ``BILI_ERROR`` 并保留原文，比假装知道
    "是网络问题"更有用——用户拿着原文去搜索，比照着我们编的原因试要快。
    """
    if isinstance(exc, WBError):
        return exc
    code = getattr(exc, "code", None)
    detail = str(exc)
    if code in _NOT_FOUND_CODES:
        return WBError("NOT_FOUND", f"{action}失败：稿件不存在或已删除", detail)
    if code in _AUTH_CODES:
        return WBError("AUTH_REQUIRED", f"{action}需要登录：{detail}", detail)
    if isinstance(exc, BiliError):
        return WBError("BILI_ERROR", f"{action}失败：{detail}", detail, retryable=True)
    if isinstance(exc, TimeoutError) or "timed out" in detail.lower():
        return WBError("BILI_ERROR", f"{action}超时：{detail}", detail, retryable=True)
    return WBError("BILI_ERROR", f"{action}失败：{detail}", detail, retryable=True)


class TtlCache:
    """极小的 TTL 缓存（只给解析结果用，不需要淘汰策略之外的任何东西）。

    方案书 7.1 要求解析结果缓存 10 分钟：用户把同一个链接粘两次、或者从合集里
    点进分P再返回，都不该重复打接口。带 ``now`` 注入是为了测试能不睡觉。
    """

    def __init__(self, ttl_s: float = 600.0, now: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_s
        self._now = now
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
        if not hit:
            return None
        stamp, value = hit
        if self._now() - stamp > self.ttl_s:
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._now(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class BiliGateway:
    """工作台唯一的 B站入口：给一个客户端，外加一层错误翻译与缓存。

    测试里把 ``client_factory`` 换成假的就能完全离线跑——这一层存在的意义之一
    就是"让上层代码不需要知道 BiliClient 怎么造"。
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        cache: TtlCache | None = None,
    ) -> None:
        self._factory = client_factory
        self._client: Any | None = None
        self._lock = threading.Lock()
        self.cache = cache if cache is not None else TtlCache()

    # ------------------------------------------------------------- 客户端

    def client(self) -> Any:
        """懒建单例。首次调用会读凭据；失败包成 WBError 而不是把栈抛给前端。"""
        with self._lock:
            if self._client is None:
                try:
                    self._client = self._factory()
                except Exception as exc:  # noqa: BLE001 —— 凭据文件坏了也要能提示
                    raise map_bili_error(exc, "初始化B站客户端") from exc
            return self._client

    @property
    def started(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------- 调用

    def get_data(
        self, url: str, params: dict[str, Any] | None = None, action: str = "请求"
    ) -> dict[str, Any]:
        try:
            data = self.client().get_data(url, params or {})
        except Exception as exc:  # noqa: BLE001
            raise map_bili_error(exc, action) from exc
        if not isinstance(data, dict):
            raise WBError("BILI_ERROR", f"{action}返回结构异常", str(type(data)))
        return data

    def is_logged_in(self) -> bool:
        """bili-scribe 那边 `is_logged_in` 有时是方法、有时是属性，两种都认。"""
        try:
            flag = self.client().is_logged_in
        except WBError:
            return False
        try:
            return bool(flag() if callable(flag) else flag)
        except WBError:
            return False

    def identity(self) -> dict[str, Any] | None:
        try:
            return self.client().identity()
        except WBError:
            return None


def default_gateway(db_path: str, key_path: str, cache: TtlCache | None = None) -> BiliGateway:
    """按 bili-scribe 的库与密钥路径造网关（共用登录态，不另存一份）。"""

    def factory() -> BiliClient:
        store = Store(db_path, key_path)
        return BiliClient(store)

    return BiliGateway(factory, cache=cache)
