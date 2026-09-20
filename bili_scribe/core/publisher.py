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
    NOTE_ADD,
    NOTE_DEL,
    REPLY_ADD,
    REPLY_DEL,
    RETRYABLE_CODES,
)
from ..infra.store import Store

STATUS_PUBLISHED = "published"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_WITHDRAWN = "withdrawn"

# 笔记的 note_id 存进发布记录的 rpid 字段时带这个前缀。
# 不改表结构就能区分"这条记录是评论还是笔记"，撤回时据此选接口。
NOTE_RPID_PREFIX = "note:"


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

    # ---------------------------------------------------------- 笔记发布

    def publish_note(
        self,
        task_id: str,
        bvid: str,
        aid: int | None,
        title: str,
        summary_text: str,
        content_delta: str,
        confirmed: bool = False,
    ) -> PublishResult:
        """把长文发布为视频笔记，并让它自动出现在评论区。

        普通评论有 1000 字符的硬上限（超出报 12025），长文只能走这条通道：
        `auto_comment=1` 时，笔记摘要会以【评论卡】形态出现在评论区。
        三道闸门与普通评论完全一致——不可撤销这一点上，笔记比评论更麻烦，
        因为它没有现成的"撤回评论"按钮。
        """
        if not (content_delta or "").strip() or content_delta.strip() == "[]":
            return PublishResult(STATUS_BLOCKED, "笔记内容为空")
        if aid is None:
            return PublishResult(STATUS_FAILED, "缺少 aid，无法定位视频")

        if self.cfg.require_confirm and not confirmed:
            return PublishResult(
                STATUS_BLOCKED,
                "这条内容还没有被确认。配置里 require_confirm 为真时，必须显式确认才会发布。",
            )

        allowed, reason = self.preflight(bvid)
        if not allowed:
            self.store.add_publish(task_id, bvid, aid, None, summary_text, STATUS_BLOCKED, reason)
            return PublishResult(STATUS_BLOCKED, reason)

        payload = {
            "oid": aid,
            "oid_type": 0,
            "title": (title or "视频笔记")[:80],
            # 官方建议：笔记前 120 字符说明关键提炼性内容，评论卡展示的就是它
            "summary": (summary_text or "").strip()[:200],
            "content": content_delta,
            "cls": 1,
            "from": "save",
            "platform": "web",
            "publish": 1,
            "auto_comment": 1,
        }

        last_error: BiliError | None = None
        for attempt in range(3):
            try:
                data = self.client.post_form(NOTE_ADD, payload)
            except BiliError as exc:
                last_error = exc
                if exc.code in RETRYABLE_CODES and attempt < 2:
                    wait = 3 * (attempt + 1)
                    self.log(f"发布被限流（{exc.code}），{wait} 秒后重试")
                    time.sleep(wait)
                    continue
                break
            else:
                note_id = ""
                if isinstance(data, dict):
                    note_id = str(data.get("note_id") or "")
                stored = f"{NOTE_RPID_PREFIX}{note_id}" if note_id else None
                publish_id = self.store.add_publish(
                    task_id, bvid, aid, stored, summary_text, STATUS_PUBLISHED,
                    f"已发布为笔记并同步到评论区（note_id={note_id or '未知'}）",
                )
                return PublishResult(
                    STATUS_PUBLISHED,
                    f"已发布为笔记并同步到评论区（note_id={note_id or '未知'}）",
                    rpid=stored,
                    publish_id=publish_id,
                    withdrawable=bool(note_id),
                )

        if last_error is None:
            return PublishResult(STATUS_FAILED, "发布失败，且没有捕获到具体错误")

        message = self._explain_note(last_error)
        self.store.add_publish(task_id, bvid, aid, None, summary_text, STATUS_FAILED, message)
        return PublishResult(STATUS_FAILED, message, raw_error=str(last_error))

    @staticmethod
    def _explain_note(error: BiliError) -> str:
        code = error.code
        if code == 79508:
            hint = (
                "这个视频已经存在一篇你创建的笔记，平台不允许重复新增。"
                "先到 B站 删除旧笔记再试。"
            )
        elif code == 79513:
            hint = "这个视频不支持笔记功能（不是所有稿件都开放）。只能改用普通评论。"
        elif code == 12015:
            hint = "平台要求验证码。请先在网页端手动操作解除。本工具不实现验证码绕过。"
        elif code == -111:
            hint = "csrf 校验失败，登录态可能已过期，请重新扫码。"
        elif code == -101:
            hint = "账号未登录，请重新扫码。"
        else:
            hint = "可稍后重试；若反复失败，请检查登录态是否失效。"
        return f"{error}｜{hint}"

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

        # 笔记和评论的撤回接口不同：笔记按 note_id 删，评论按 rpid 删。
        # note_id 存进 rpid 字段时带了 "note:" 前缀，据此分流。
        if str(rpid).startswith(NOTE_RPID_PREFIX):
            note_id = str(rpid)[len(NOTE_RPID_PREFIX) :]
            try:
                self.client.post_form(
                    NOTE_DEL,
                    {"oid": record.get("aid"), "note_id": note_id},
                )
            except BiliError as exc:
                return PublishResult(
                    STATUS_FAILED, f"撤回笔记失败：{exc}", rpid=rpid, raw_error=str(exc)
                )
            self.store.mark_publish_deleted(publish_id, "已撤回笔记")
            return PublishResult(
                STATUS_WITHDRAWN,
                "已删除那篇笔记（评论区里的笔记卡会随之消失）",
                rpid=rpid,
                publish_id=publish_id,
            )

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
