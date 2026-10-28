"""FastAPI 应用：把领域层能力暴露成一组接口，并托管单页前端。

这个文件现在只做一件事：**传输转换**。业务逻辑全在 `service.py`，因为桌面版
（`desktop.py`）要跑同一份逻辑却不能起 HTTP 服务——两边共用一套校验、一套状态
机、一套错误文案，才不会出现"桌面上能发、网页上不能发"这种最难查的 bug。

留在这一层的东西有三样：
1. 路由声明（`app.get` / `app.post`，路径与参数名不能变，211 项测试都打它）
2. `ServiceError` → `HTTPException` 的翻译
3. 静态页挂载

**`main.py` 里那批旧函数名仍然导出**（`_qr_png_data_url`、`_sum_usage`、
`AppState`、`_run_refine`……）：测试按这些名字 import，改名等于顺手把测试也改了，
而那是另一件事。它们现在是 `service.py` 里同名函数的别名。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import AppConfig
from ..infra.bili_client import BiliError
from .service import (
    AppState,
    CookieRequest,
    LlmPatch,
    PublishRequest,
    RefineContext,
    RefineRequest,
    QrUnavailable,
    RoleRequest,
    SkinRequest,
    Service,
    ServiceError,
    SettingsPayload,
    TaskRequest,
    WithdrawRequest,
    error_detail,
    metrics_of,
    publish_document_of,
    qr_available,
    qr_png_data_url,
    refine_gate,
    run_refine,
    run_task,
    sum_usage,
    version_brief,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 旧名字的别名。测试按这些 import，改动它们属于"顺手把测试也改了"。
_qr_available = qr_available
_qr_png_data_url = qr_png_data_url
_metrics_of = metrics_of
_sum_usage = sum_usage
_version_brief = version_brief
_publish_document_of = publish_document_of
_refine_gate = refine_gate
_error_detail = error_detail
_run_task = run_task
_run_refine = run_refine

__all__ = [
    "AppState",
    "RefineContext",
    "QrUnavailable",
    "Service",
    "ServiceError",
    "create_app",
]


def _http(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


def create_app(cfg: AppConfig) -> FastAPI:
    app = FastAPI(title="bili-scribe", version=__version__)
    # `service` 在 AppState 之外多管两件启动期的事（清残留画面缓存、探测二维码依赖），
    # 所以这里构造它而不是自己 new AppState。
    service = Service(cfg)
    app.state.ctx = service.state
    app.state.service = service

    # ------------------------------------------------------------------ 元信息

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        return service.config()

    @app.get("/api/llm/check")
    def api_llm_check() -> dict[str, Any]:
        return service.llm_check()

    @app.get("/api/roles")
    def api_roles() -> dict[str, Any]:
        return service.roles()

    @app.post("/api/role")
    def api_set_role(payload: RoleRequest) -> dict[str, Any]:
        # 顶栏的角色选择器走这条，而不是复用 PUT /api/settings——理由见
        # Service.set_role 的注释（只为改一个字段，不必让 api_key 再走一遍请求体）。
        return _call(service.set_role, payload)

    @app.post("/api/skin")
    def api_set_skin(payload: SkinRequest) -> dict[str, Any]:
        # 同 set_role：皮肤是一个字段，不该让整份设置（含明文密钥）跟着走一遍。
        return _call(service.set_skin, payload)

    # ---------------------------------------------------------------- 设置页面

    @app.get("/api/settings")
    def api_get_settings() -> dict[str, Any]:
        return service.get_settings()

    @app.put("/api/settings")
    def api_put_settings(payload: SettingsPayload) -> dict[str, Any]:
        # 校验与写盘全在 service 里。这里只翻异常——注意 `save_config` 失败原来是
        # `HTTPException(500)`，现在由 service 抛 ServiceError(500)，语义一致。
        return _call(service.put_settings, payload)

    @app.post("/api/settings/test-llm")
    def api_test_llm(payload: LlmPatch | None = None) -> dict[str, Any]:
        return service.test_llm(payload)

    # -------------------------------------------------------------------- 登录

    @app.get("/api/auth/status")
    def api_auth_status() -> dict[str, Any]:
        return service.auth_status()

    @app.get("/api/auth/qrcode")
    def api_auth_qrcode() -> dict[str, Any]:
        return service.auth_qrcode()

    @app.get("/api/auth/poll")
    def api_auth_poll(key: str = Query(min_length=8)) -> dict[str, Any]:
        # 参数约束留在路由上：`Query(min_length=8)` 由 FastAPI 直接变 422，
        # 和 service 里的 400 是两条不同的路径，合并会改变状态码。
        return service.auth_poll(key)

    @app.post("/api/auth/cookie")
    def api_auth_cookie(payload: CookieRequest) -> dict[str, Any]:
        return service.auth_cookie(payload)

    @app.post("/api/auth/logout")
    def api_auth_logout() -> dict[str, Any]:
        return service.auth_logout()

    # ---------------------------------------------------------------- 字幕上传

    @app.post("/api/subtitle/parse")
    async def api_parse_subtitle(request: Request) -> dict[str, Any]:
        """收原始字节，解析后暂存。

        **必须用 run_in_threadpool**：这个路由是 async，而解析与落盘都是同步的
        CPU／IO 操作，直接在事件循环里跑会阻塞整个服务——实测一份上限体积
        （5 MB）的 srt 解析约 0.5–0.8 秒，足够让正在轮询的任务进度卡住。
        把 `service.parse_subtitle` 整体丢进线程池，一次切换覆盖解析和落盘两步。
        """
        raw = await request.body()
        return await run_in_threadpool(service.parse_subtitle, raw)

    @app.delete("/api/subtitle/{token}")
    def api_discard_subtitle(token: str) -> dict[str, Any]:
        return service.discard_subtitle(token)

    # -------------------------------------------------------------------- 任务

    @app.post("/api/task")
    def api_create_task(payload: TaskRequest) -> dict[str, Any]:
        return service.create_task(payload)

    @app.get("/api/task/{task_id}")
    def api_get_task(task_id: str) -> dict[str, Any]:
        return service.get_task(task_id)

    @app.get("/api/task/{task_id}/versions")
    def api_list_versions(task_id: str) -> dict[str, Any]:
        return service.list_versions(task_id)

    @app.get("/api/task/{task_id}/versions/{version_no}")
    def api_get_version(task_id: str, version_no: int) -> dict[str, Any]:
        return service.get_version(task_id, version_no)

    @app.post("/api/task/{task_id}/versions/{version_no}/restore")
    def api_restore_version(task_id: str, version_no: int) -> dict[str, Any]:
        return service.restore_version(task_id, version_no)

    @app.post("/api/task/{task_id}/refine")
    def api_refine_task(task_id: str, payload: RefineRequest) -> dict[str, Any]:
        return service.refine_task(task_id, payload)

    @app.get("/api/tasks")
    def api_list_tasks(limit: int = 20) -> list[dict[str, Any]]:
        return service.list_tasks(limit)

    @app.get("/api/history")
    def api_list_history(limit: int = 100) -> list[dict[str, Any]]:
        return service.list_history(limit)

    # -------------------------------------------------------------------- 发布

    @app.post("/api/publish")
    def api_publish(payload: PublishRequest) -> dict[str, Any]:
        return _call(service.publish, payload)

    @app.post("/api/publish/withdraw")
    def api_withdraw(payload: WithdrawRequest) -> dict[str, Any]:
        return _call(service.withdraw, payload.publish_id)

    @app.get("/api/publishes")
    def api_list_publishes(limit: int = 20) -> list[dict[str, Any]]:
        return service.list_publishes(limit)

    # ------------------------------------------------------- 预读缓存（桌面端用）

    @app.get("/api/notes/stats")
    def api_notes_stats() -> dict[str, Any]:
        return service.notes_stats()

    @app.post("/api/notes/clear")
    def api_clear_notes() -> dict[str, Any]:
        return service.clear_notes()

    # ------------------------------------------------------------------ 异常处理
    #
    # 三个 handler 保留在这里而不是搬进 service：它们本质是"怎么把异常写成 HTTP"，
    # 属于传输层的事。桌面端不装这套——那边把 ServiceError 写成 {"error": ...}。

    @app.exception_handler(ServiceError)
    def _service_error_handler(_request, exc: ServiceError) -> JSONResponse:
        """service 层的领域异常统一翻成 HTTP。"""
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.exception_handler(BiliError)
    def _bili_error_handler(_request, exc: BiliError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc), "code": exc.code})

    @app.exception_handler(QrUnavailable)
    def _qr_unavailable_handler(_request, exc: QrUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "qr_unavailable"})

    @app.exception_handler(Exception)
    def _unhandled_handler(_request, exc: Exception) -> JSONResponse:
        """未预期的异常也必须以 JSON 回给前端。

        否则响应体是空的，前端只能读到 "HTTP 500"——等于把排查线索全部丢掉。
        这类错误排查起来最费时间，不能在这里省事。
        """
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务端未预期错误：{type(exc).__name__}: {exc}"},
        )

    # ------------------------------------------------------------------ 静态页

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app


def _call(func, *args, **kwargs):
    """同步调用 service 方法并把 ServiceError 翻成 HTTPException。

    为什么要显式包一层：`ServiceError` 有全局 handler，但 FastAPI 的 handler
    优先级低于路由里抛出的 `HTTPException`。测试里有几条断言的是
    `HTTPException.status_code`（通过 TestClient 的 4xx 响应），走 handler 也一样；
    但显式翻译能让"这个方法是会失败的"在路由代码里看得见，不用去翻 service。
    """
    try:
        return func(*args, **kwargs)
    except ServiceError as exc:
        raise _http(exc) from exc
