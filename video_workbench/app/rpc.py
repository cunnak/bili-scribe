"""RPC 分发：白名单 + 参数校验 + 错误映射。

**这是整个应用唯一对外的调用面。** pywebview 的 ``js_api`` 会把这里暴露的每个方法
交给页面里的 JS，所以白名单之外的一切都必须够不着——不做反射式 ``getattr(self, name)``，
因为那等于把 ``_finish`` / ``__dict__`` 这类东西也一起送出去。

**返回值形状固定**，前端只需要判断 ``ok``：

.. code-block:: json

   {"ok": true,  "data": ...,  "request_id": "..."}
   {"ok": false, "error": {"code","message","detail","retryable"}, "request_id": "..."}

**异常在这里收口。** 业务代码只管 ``raise WBError``；其余异常一律转 ``INTERNAL``
并把整段 traceback 放进 ``detail``（``detail`` 的三条真实去处写在 ``app/errors.py``
的 ``WBError`` docstring 里，那里是唯一口径，这里不重复一遍）。任务失败时界面上那句
原因是 ``app/jobs.py`` 的 ``error_message_for_row`` 拼的——整段 traceback 不会铺在
主文案里，拼接规则只有那一处。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from video_workbench.app.errors import WBError

Method = Callable[..., Any]


class _Invalid(BaseModel):
    """占位：没有参数模型的方法用不到它，保留类型别名可读性。"""


class Router:
    def __init__(self) -> None:
        self._table: dict[str, tuple[Method, type[BaseModel] | None]] = {}

    # ------------------------------------------------------------- 注册

    def add(
        self,
        name: str,
        fn: Method,
        model: type[BaseModel] | None = None,
    ) -> None:
        if name in self._table:
            raise ValueError(f"RPC 方法重复注册：{name}")
        self._table[name] = (fn, model)

    def names(self) -> list[str]:
        return sorted(self._table)

    # ------------------------------------------------------------- 分发

    def dispatch(
        self, method: str, params: Any = None, request_id: str | None = None
    ) -> dict[str, Any]:
        entry = self._table.get(str(method or ""))
        if entry is None:
            return self._error(WBError("UNSUPPORTED", f"未知接口：{method}"), request_id)
        fn, model = entry
        try:
            if model is not None:
                raw = params if isinstance(params, dict) else {}
                try:
                    payload = model.model_validate(raw)
                except ValidationError as exc:
                    # 只回"哪个字段错了"，不回整段参数：参数里可能带密钥
                    problems = "; ".join(
                        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                        for err in exc.errors()
                    )
                    raise WBError("VALIDATION", f"参数不合法：{problems}") from exc
                data = fn(**payload.model_dump())
            else:
                data = fn() if params in (None, {}) else fn(**params)

            return {"ok": True, "data": data, "request_id": request_id}
        except WBError as exc:
            return self._error(exc, request_id)
        except TypeError as exc:
            # 参数名写错是开发期错误，但落到用户手里会变成"点了没反应"，
            # 所以仍然按参数错误回报，并附上原文便于定位。
            return self._error(
                WBError("VALIDATION", f"调用参数不匹配：{exc}", traceback.format_exc()),
                request_id,
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(
                WBError("INTERNAL", str(exc) or exc.__class__.__name__, traceback.format_exc()),
                request_id,
            )

    @staticmethod
    def _error(exc: WBError, request_id: str | None) -> dict[str, Any]:
        return {"ok": False, "error": exc.to_payload(), "request_id": request_id}
