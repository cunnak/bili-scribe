"""WBI 签名。

B站 对部分接口要求带 `w_rid` / `wts` 两个参数，算法是公开可复现的：

1. 取 `x/web-interface/nav` 返回的 `wbi_img.img_url` 与 `sub_url`，抠出文件名主干
   得到 img_key 与 sub_key；
2. 把两个 key 拼起来，按固定 64 位重排表打乱，取前 32 位当作 mixin_key；
3. 把业务参数补上 `wts`（当前 unix 秒），按 key 排序、剔除 `!'()*` 字符后
   urlencode，得到规范化查询串；
4. `w_rid = md5(规范化查询串 + mixin_key)`。

签名本身不涉密，密钥是从公开接口取的。它的作用更像访问控制而非加密——这也就是
为什么这些接口被归为"非公开"。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable
from urllib.parse import urlencode

from .endpoints import NAV

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 官方算法里需要从参数值中剔除的字符
_FILTER_CHARS = "!'()*"
_KEY_TTL_SECONDS = 1800


class WbiSigner:
    """把签名逻辑与 HTTP 传输解耦，便于单独测试。"""

    def __init__(self, fetch_json: Callable[..., dict[str, Any]], ttl: int = _KEY_TTL_SECONDS):
        self._fetch_json = fetch_json
        self._ttl = ttl
        self._key: str | None = None
        self._fetched_at: float = 0.0

    @staticmethod
    def mixin_key(img_key: str, sub_key: str) -> str:
        raw = img_key + sub_key
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    def ensure_key(self, force: bool = False) -> str:
        fresh = self._key is not None and (time.time() - self._fetched_at) < self._ttl
        if fresh and not force:
            return self._key  # type: ignore[return-value]

        payload = self._fetch_json(NAV)
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url") or ""
        sub_url = wbi_img.get("sub_url") or ""
        if not img_url or not sub_url:
            raise RuntimeError("无法获取 WBI 密钥，nav 接口返回异常")
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._key = self.mixin_key(img_key, sub_key)
        self._fetched_at = time.time()
        return self._key

    def sign(self, params: dict[str, Any]) -> dict[str, str]:
        key = self.ensure_key()
        merged: dict[str, Any] = {k: v for k, v in params.items() if v is not None}
        merged["wts"] = int(time.time())

        cleaned: dict[str, str] = {}
        for name in sorted(merged):
            value = str(merged[name])
            cleaned[name] = "".join(ch for ch in value if ch not in _FILTER_CHARS)

        query = urlencode(cleaned)
        cleaned["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
        return cleaned
