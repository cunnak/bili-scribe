"""接口常量与错误码释义。

这些是 B站 Web 端在用的非公开接口，不在官方开放平台的能力清单里，也没有
稳定性承诺。改动平台容易失效——所有调用点都必须检查返回值而不是 HTTP 状态码，
因为真正的错误信息在 JSON 的 `code` 字段里。

关于合规：2026-01-28，长期作为社区接口依据的 SocialSisterYi/bilibili-API-collect
收到 B站 委托律所律师函后永久关停下线，指控理由是"系统性收集、整理非公开 API
并向不特定公众传播"。所以本项目把这些路径视为本地自用实现细节：不要打包分发、
不要公开成教程、不要扩散到超出自己账号的范围。
"""

from __future__ import annotations

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"

# —— 登录 ——
QR_GENERATE = f"{PASSPORT}/x/passport-login/web/qrcode/generate"
QR_POLL = f"{PASSPORT}/x/passport-login/web/qrcode/poll"
NAV = f"{API}/x/web-interface/nav"
FINGER_SPI = f"{API}/x/frontend/finger/spi"

# —— 内容采集 ——
VIDEO_VIEW = f"{API}/x/web-interface/view"
PLAYER_V2 = f"{API}/x/player/wbi/v2"
ARCHIVE_TAGS = f"{API}/x/tag/archive/tags"
DANMAKU_XML = f"{API}/x/v1/dm/list.so"
DANMAKU_SEG = f"{API}/x/v2/dm/web/seg.so"
PLATFORM_SUMMARY = f"{API}/x/web-interface/view/conclusion/get"
REPLY_MAIN = f"{API}/x/v2/reply/wbi/main"

# —— 评论操作 ——
REPLY_ADD = f"{API}/x/v2/reply/add"
REPLY_DEL = f"{API}/x/v2/reply/del"
# 视频笔记。auto_comment=1 时笔记摘要会自动发成评论区评论，
# 这是突破普通评论 1000 字上限的唯一正规通道。
NOTE_ADD = f"{API}/x/note/add"
NOTE_DEL = f"{API}/x/note/del"

# 评论区的资源类型代码，视频稿件固定为 1
COMMENT_TYPE_VIDEO = 1

# 扫码轮询状态码
QR_WAITING = 86101
QR_SCANNED = 86090
QR_EXPIRED = 86038
QR_SUCCESS = 0

QR_STATUS_TEXT = {
    QR_SUCCESS: "登录成功",
    QR_WAITING: "等待扫码",
    QR_SCANNED: "已扫码，请在手机上确认",
    QR_EXPIRED: "二维码已失效，请刷新",
}

# 错误码 → 处置建议。这张表本身就是一张风控地图：
# 它告诉我们平台在哪些环节会卡住自动化行为。
ERROR_HINTS: dict[int, str] = {
    -1: "接口未返回 JSON，通常是被风控页拦截",
    -101: "账号未登录，请重新扫码",
    -102: "账号已被封停",
    -111: "csrf 校验失败（bili_jct 缺失或与 SESSDATA 不匹配），请重新登录",
    -352: "触发风控：多半是缺少 WBI 签名或 buvid3",
    -400: "请求参数错误",
    -403: "访问权限不足，该接口需要登录",
    -404: "资源不存在",
    -509: "请求过于频繁，已被限流",
    12002: "评论区已关闭",
    12015: "需要评论验证码：请先在网页端手动发一条评论解除。本工具不实现验证码绕过",
    12016: "内容包含敏感信息，已被平台拦截",
    12025: "评论字数超出上限",
    12035: "该账号被 UP 主列入评论黑名单",
    12045: "该稿件需要购买后才能评论",
    12051: "重复评论：相同内容不能重复发送",
    12052: "评论区已关闭",
}

# 明确不可重试、必须停下来人工处理的错误码
FATAL_COMMENT_CODES = {12015, 12016, 12025, 12035, 12045, 12002, 12052, -102}
# 可以通过退避重试缓解的错误码
RETRYABLE_CODES = {-509}
