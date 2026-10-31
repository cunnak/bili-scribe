"""统一错误模型与错误码表。

**为什么错误必须带码，而不是直接抛异常。** 前端拿到一句「操作失败」时无法判断
下一步：该重试、该改配置、还是该去网页端登录。把失败分成可判定的码之后，
界面才能给出可执行的提示（"先去 B站删掉旧笔记" / "请先安装 FFmpeg"）。

所有对外失败的形状只有一个出口（``WBError.to_payload``），RPC 层不再二次翻译。
"""

from __future__ import annotations

from typing import Any

# 方案书 6.4 的错误码表。新增错误码必须同时补进这张表，
# 否则前端拿到一个自己不认识的码就只能走兜底文案。
ERROR_CODES: dict[str, str] = {
    "VALIDATION": "参数不合法",
    "NOT_FOUND": "对象不存在",
    "CONFLICT": "状态冲突",
    "AUTH_REQUIRED": "需要登录或授权",
    "DEPENDENCY_MISSING": "缺少外部依赖",
    "PROCESS_FAILED": "外部进程执行失败",
    "CANCELED": "任务已取消",
    "LLM_ERROR": "模型调用失败",
    "LLM_FORBIDDEN": "被网关拒绝（403）",
    "LLM_EMPTY": "模型没有返回正文",
    "LLM_BAD_RESPONSE": "模型返回的内容不合规范",
    "LLM_NOT_CONFIGURED": "还没有配置大模型",
    # 下面这几个是 infra/llm.py 真的会抛的码（按 HTTP 状态分）。之前它们**不在表里**：
    # 模型一返 401/429/400/5xx 就会在构造异常时抛 ValueError("未知错误码")，
    # 用户看到的是"内部错误"，而真正的原因（key 不对 / 被限流 / 模型名写错）全丢了。
    "LLM_AUTH": "大模型鉴权失败",
    "LLM_RATE_LIMIT": "大模型限流，稍后再试",
    "LLM_BAD_REQUEST": "大模型拒绝了这次请求",
    "LLM_UPSTREAM": "大模型服务端错误",
    "LLM_TIMEOUT": "大模型响应超时",
    "LLM_FAILED": "大模型调用失败",
    "BILI_ERROR": "B站接口返回错误",
    "GEO_BLOCKED": "地区限制，当前网络不可用",
    "RATE_LIMITED": "触发限流，稍后再试",
    "NETWORK": "网络不通或超时",
    "DISK_FULL": "磁盘空间不足",
    # 术语表 CSV、双语字幕文件这类落盘/读盘失败。跟"参数不合法"分开：
    # 用户看到的是"写不出去"，多半是路径权限或磁盘，不是自己填错了。
    "IO": "文件读写失败",
    "ALL_FAILED": "批量任务全部失败",
    # 上次进程没退干净留下的任务。跟 INTERNAL 分开：用户看到"内部错误"只会以为
    # 程序坏了，看到"上次退出时中断了"才知道点重试就行。
    "INTERRUPTED": "上次退出时中断了（可以重试）",
    "UNSUPPORTED": "当前环境不支持",
    "INTERNAL": "内部错误",
}

# 这些码重试有意义；权限类重试只是浪费一次等待。
RETRYABLE_CODES = {
    "PROCESS_FAILED",
    "LLM_ERROR",
    "LLM_RATE_LIMIT",
    "LLM_TIMEOUT",
    "LLM_UPSTREAM",
    "BILI_ERROR",
    "INTERNAL",
    "NETWORK",
    "RATE_LIMITED",
    "INTERRUPTED",
}


class WBError(Exception):
    """带错误码的业务异常。

    ``detail`` 是给人排错用的原文（进程 stderr、接口响应体）。
    它可能很长，因此**只写日志和"复制日志"，不直接铺在界面主文案里**。
    """

    def __init__(
        self,
        code: str,
        message: str,
        detail: str = "",
        retryable: bool | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            # 拼错错误码会让前端掉进兜底分支，而这种错误最难查——当场炸出来。
            raise ValueError(f"未知错误码：{code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = RETRYABLE_CODES.__contains__(code) if retryable is None else retryable

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "retryable": bool(self.retryable),
        }

    def __str__(self) -> str:  # pragma: no cover - 只为日志可读
        return f"[{self.code}] {self.message}"


def canceled(message: str = "任务已取消") -> WBError:
    return WBError("CANCELED", message)


def validation(message: str, detail: str = "") -> WBError:
    return WBError("VALIDATION", message, detail)


def not_found(message: str) -> WBError:
    return WBError("NOT_FOUND", message)


def unsupported(message: str, detail: str = "") -> WBError:
    return WBError("UNSUPPORTED", message, detail)


def dependency(message: str, detail: str = "") -> WBError:
    return WBError("DEPENDENCY_MISSING", message, detail)
