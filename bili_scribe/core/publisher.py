"""评论发布：这是整个应用里唯一不可撤销的动作，所以它是受约束最多的模块。

三道闸门，缺一不可：

1. **人工确认**。`require_confirm` 为真时，没有显式确认一律不发。
2. **本地去重**。发请求之前先查本地记录。B站 对相同内容重复发送会返回
   12051，与其等平台来拒绝，不如自己先拦住。
3. **频率限制**。两次发布之间强制间隔，避免踩到 -509 限流。

另外，遇到需要验证码（12015）、敏感内容（12016）、被 UP 主拉黑（12035）这类
错误，一律硬停并把原因原样交回给人处理。这里不实现任何绕过手段——能绕过验证码
的工具，本质上是在替平台做安全决策，那不是这个应用该干的事。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..config import PublishConfig
from ..infra.bili_client import BiliClient, BiliError
from ..infra.endpoints import (
    COMMENT_TYPE_VIDEO,
    FATAL_COMMENT_CODES,
    REPLY_ADD,
    REPLY_DEL,
    RETRYABLE_CODES,
)
from ..infra.store import Store

STATUS_PUBLISHED = "published"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_WITHDRAWN = "withdrawn"


@dataclass
class PublishResult:
    status: str
    message: str
    rpid: str | None = None
    publish_id: int | None = None
    withdrawable: bool = False
    raw_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "rpid": self.rpid,
            "publish_id": self.publish_id,
            "withdrawable": self.withdrawable,
            "raw_error": self.raw_error,
        }


def _parse_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _extract_rpid(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    rpid = data.get("rpid_str") or data.get("rpid")
    if rpid in (None, 0, "0"):
        reply = data.get("reply") or {}
        rpid = reply.get("rpid_str") or reply.get("rpid")
    return str(rpid) if rpid not in (None, 0, "0") else None


class Publisher:
    def __init__(
        self,
        client: BiliClient,
        store: Store,
        cfg: PublishConfig,
        logger: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.store = store
        self.cfg = cfg
        self.log = logger or (lambda _msg: None)

    # ------------------------------------------------------------ 前置检查

    def preflight(self, bvid: str) -> tuple[bool, str]:
        if not self.client.is_logged_in:
            return False, "未登录，无法发表评论"

        existing = self.store.find_live_publish(bvid)
        if existing:
            return False, (
                f"这个视频你已经发过总结（rpid={existing.get('rpid')}，"
                f"时间 {existing.get('created_at')}）。为避免平台判定「重复评论」，本次已拦截。"
                "如需重发，请先在下方撤回那条评论。"
            )

        last = self.store.last_publish_at()
        if last:
            elapsed = time.time() - _parse_timestamp(last)
            remain = self.cfg.min_interval_seconds - elapsed
            if remain > 0:
                return False, (
                    f"距上次发布不足 {self.cfg.min_interval_seconds} 秒，"
                    f"请等 {int(remain) + 1} 秒后再试（防止触发限流 -509）"
                )

        return True, ""

    # -------------------------------------------------------------- 发布

    def publish(
        self,
        task_id: str,
        bvid: str,
        aid: int | None,
        text: str,
        confirmed: bool = False,
    ) -> PublishResult:
        if not text.strip():
            return PublishResult(STATUS_BLOCKED, "评论内容为空")
        if aid is None:
            return PublishResult(STATUS_FAILED, "缺少 aid，无法定位评论区")

        if self.cfg.require_confirm and not confirmed:
            return PublishResult(
                STATUS_BLOCKED,
                "这条内容还没有被确认。配置里 require_confirm 为真时，必须显式确认才会发布。",
            )

        allowed, reason = self.preflight(bvid)
        if not allowed:
            self.store.add_publish(task_id, bvid, aid, None, text, STATUS_BLOCKED, reason)
            return PublishResult(STATUS_BLOCKED, reason)

        payload = {
            "oid": aid,
            "type": COMMENT_TYPE_VIDEO,
            "message": text,
            "plat": 1,
        }

        last_error: BiliError | None = None
        for attempt in range(3):
            try:
                data = self.client.post_form(REPLY_ADD, payload)
            except BiliError as exc:
                last_error = exc
                if exc.code in RETRYABLE_CODES and attempt < 2:
                    wait = 3 * (attempt + 1)
                    self.log(f"发布被限流（{exc.code}），{wait} 秒后重试")
                    time.sleep(wait)
                    continue
                break
            else:
                rpid = _extract_rpid(data)
                publish_id = self.store.add_publish(
                    task_id, bvid, aid, rpid, text, STATUS_PUBLISHED, "发布成功"
                )
                return PublishResult(
                    STATUS_PUBLISHED,
                    "已发布到评论区",
                    rpid=rpid,
                    publish_id=publish_id,
                    withdrawable=bool(rpid),
                )

        if last_error is None:
            return PublishResult(STATUS_FAILED, "发布失败，且没有捕获到具体错误")

        message = self._explain(last_error)
        self.store.add_publish(
            task_id, bvid, aid, None, text, STATUS_FAILED, message
        )
        return PublishResult(STATUS_FAILED, message, raw_error=str(last_error))

    @staticmethod
    def _explain(error: BiliError) -> str:
        code = error.code
        if code == 12015:
            hint = (
                "平台要求评论验证码。请先在网页端手动发一条评论解除验证，再回来重试。"
                "本工具不实现验证码绕过。"
            )
        elif code == 12051:
            hint = (
                "平台判定为重复评论——风控已经介入。请换一段内容，或先撤回旧评论。"
            )
        elif code == 12035:
            hint = "你被这位 UP 主加入了评论黑名单，在该视频下无法评论。"
        elif code == 12016:
            hint = "内容被判定为敏感信息。请修改措辞后重试。"
        elif code == -102:
            hint = "账号处于封停状态，请先处理账号问题。"
        elif code in FATAL_COMMENT_CODES:
            hint = "这类问题需要人工处理，本工具不会尝试绕过。"
        else:
            hint = "可稍后重试；若反复失败，请检查登录态是否失效。"
        return f"{error}｜{hint}"

    # -------------------------------------------------------------- 撤回

    def withdraw(self, publish_id: int) -> PublishResult:
        record = self.store.get_publish(publish_id)
        if record is None:
            return PublishResult(STATUS_FAILED, "找不到这条发布记录")

        if record.get("deleted_at"):
            return PublishResult(STATUS_BLOCKED, "这条评论已经撤回过了")

        rpid = record.get("rpid")
        if not rpid:
            return PublishResult(STATUS_FAILED, "该记录没有 rpid，无法撤回")

        try:
            self.client.post_form(
                REPLY_DEL,
                {
                    "oid": record.get("aid"),
                    "type": COMMENT_TYPE_VIDEO,
                    "rpid": rpid,
                },
            )
        except BiliError as exc:
            return PublishResult(
                STATUS_FAILED, f"撤回失败：{exc}", rpid=rpid, raw_error=str(exc)
            )

        self.store.mark_publish_deleted(publish_id, "已撤回")
        return PublishResult(
            STATUS_WITHDRAWN,
            "已撤回评论区的那条总结",
            rpid=rpid,
            publish_id=publish_id,
        )

    # -------------------------------------------------------------- 预览

    def preview_comment(self, text: str) -> dict[str, Any]:
        """发布前让前端能自查字数与是否超限。"""
        limit = self.cfg.max_comment_chars
        return {
            "length": len(text),
            "limit": limit,
            "over_limit": len(text) > limit,
            "platform_limit": 1000,
        }
