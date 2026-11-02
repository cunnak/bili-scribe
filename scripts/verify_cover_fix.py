"""端到端验证封面修复：走真实 B站接口 + 真实生产出口，不启动 GUI。

链路就是用户点「解析」时走的那一条：
  WorkbenchService.download_resolve → Resolver.resolve → Resolution.to_payload → normalize_cover

断言三件事：
1. 出口给的 cover 是 https（页面 CSP 的 img-src 不含 http:，明文必被拦）
2. 那个 https 地址能真取回完整 JPEG（证明拦它的是 CSP，不是图床）
3. 同一地址换成明文取到的字节数完全一致（协议无关，升级是安全的）
"""

import io
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = r"C:\Users\b1596\Desktop\视频内容总结"
sys.path.insert(0, ROOT)

from video_workbench.app.service import WorkbenchService
from video_workbench.core.resolver import normalize_cover

URL = "https://www.bilibili.com/video/BV1xGxW6kEQZ/?vd_source=20f9b01ac1b31cd36420348795850256"

svc = WorkbenchService(config_path=ROOT + r"\config.yaml", picker=None)
try:
    payload = svc.download_resolve(URL)
finally:
    svc.close()

cover = str(payload.get("cover") or "")
print("kind        :", payload.get("kind"))
print("title       :", payload.get("title"))
print("items       :", len(payload.get("items") or []))
print("cover(出口) :", cover)

assert cover, "出口封面为空"
assert cover.startswith("https://"), f"出口封面不是 https，页面 CSP 会拦掉：{cover}"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        assert resp.status == 200, resp.status
        return resp.read()


https_bytes = get(cover)
print("https 字节  :", len(https_bytes), "magic:", https_bytes[:3].hex())
assert https_bytes[:3] == b"\xff\xd8\xff", "返回的不是 JPEG"
assert len(https_bytes) > 1024, "字节数异常，像是错误页"

plain = cover.replace("https://", "http://", 1)
http_bytes = get(plain)
print("http  字节  :", len(http_bytes))
assert http_bytes == https_bytes, "同一资源两种协议字节不一致，升级协议不安全"
print("normalize_cover(明文) →", normalize_cover(plain))
print("\nOK：真实接口 → 出口 https → 可取回完整 JPEG，且与明文字节一致")
