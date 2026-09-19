"""FastAPI 应用：把领域层能力暴露成一组接口，并托管单页前端。"""

from __future__ import annotations

import base64
import io
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import AppConfig
from ..core import collector as collector_mod
from ..core import composer, resolver, summarizer
from ..core.collector import CollectError
from ..core.publisher import Publisher
from ..core.resolver import ResolveError
from ..core.summarizer import SummarizeError
from ..infra.bili_client import BiliClient, BiliError
from ..infra.endpoints import QR_SUCCESS
from ..infra.llm import LlmClient, LlmError
from ..infra.store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"


class QrUnavailable(RuntimeError):
    """二维码依赖缺失或渲染失败。

    单独成一个异常类型，是为了让接口能返回一句"照着做就行"的提示，
    而不是一个没有任何线索的 500。
    """


def _qr_available() -> bool:
    try:
        import qrcode  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


# ------------------------------------------------------------------- 请求体


class TaskRequest(BaseModel):
    raw_input: str = Field(min_length=1, max_length=2000)


class CookieRequest(BaseModel):
    sessdata: str
    bili_jct: str
    dedeuserid: str = ""


class PublishRequest(BaseModel):
    task_id: str
    text: str
    confirm: bool = False


class WithdrawRequest(BaseModel):
    publish_id: int


# --------------------------------------------------------------------- 状态


class AppState:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.store = Store(cfg.db_path, cfg.key_path, logger=lambda m: print(f"[store] {m}"))
        self.llm = LlmClient(cfg.llm)

    def new_client(self) -> BiliClient:
        """每个请求用独立会话。Cookie 统一从 SQLite 恢复，不用跨线程共享连接。"""
        return BiliClient(self.store, logger=lambda m: print(f"[bili] {m}"))


# --------------------------------------------------------------------- 工具


def _qr_png_data_url(content: str) -> str:
    """把扫码 URL 渲染成 PNG data URL。

    qrcode 和 Pillow 只是"扫码登录"这一条通道的依赖。缺了它们，应用照样能启动，
    手工填 Cookie 那条兜底通道也照样能用——所以这里绝不能退化成裸 ImportError，
    那会让界面上只剩一句没有信息量的 "HTTP 500"。
    """
    try:
        import qrcode
    except ImportError as exc:
        raise QrUnavailable(
            '缺少二维码依赖，无法生成扫码图。请执行：pip install "qrcode[pil]"'
            "（不影响下方的手动 Cookie 登录）"
        ) from exc

    try:
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(content)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#111111", back_color="#ffffff")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    except Exception as exc:  # noqa: BLE001 - 渲染失败的原因要原样带出去
        raise QrUnavailable(
            f"二维码渲染失败（{type(exc).__name__}: {exc}）。"
            '通常是 Pillow 没装上，请执行：pip install "qrcode[pil]"'
        ) from exc

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _run_task(state: AppState, task_id: str, raw_input: str) -> None:
    """后台跑完整条链：解析 → 采集 → 总结 → 组装。失败就落错误，不吞异常。"""
    store = state.store

    def progress(stage: str, message: str) -> None:
        store.update_task(task_id, status=stage, progress=message)

    try:
        client = state.new_client()

        progress("resolving", "解析输入")
        target = resolver.resolve(raw_input, client)
        store.update_task(task_id, bvid=target.bvid, page=target.page)

        bundle = collector_mod.collect(
            client, target.bvid, target.page, state.cfg.collect, progress=progress
        )
        store.update_task(
            task_id,
            aid=bundle.aid,
            cid=bundle.cid,
            title=bundle.title,
            up_name=bundle.up_name,
            up_mid=bundle.up_mid,
            source_kind=bundle.source_kind,
            source_label=bundle.source_label,
            source_trust=bundle.source_trust,
            warnings=bundle.warnings,
            candidates=bundle.checks_as_dicts(),
            platform_summary=bundle.platform_summary,
        )

        summary = summarizer.summarize(bundle, state.llm, state.cfg, progress=progress)
        comment_text = composer.build_comment(summary, bundle, state.cfg.publish)
        preview_md = composer.build_preview(bundle, summary, state.cfg.publish)

        store.update_task(
            task_id,
            status="ready",
            progress="总结已生成，等待你确认",
            summary_json=summary.to_dict(),
            comment_text=comment_text,
            preview_md=preview_md,
        )
    except (ResolveError, CollectError, SummarizeError, BiliError, LlmError) as exc:
        store.update_task(task_id, status="failed", progress="失败", error=str(exc))
    except Exception as exc:  # 兜底：任何未预期异常都不能让任务永远停在中间态
        store.update_task(
            task_id, status="failed", progress="失败", error=f"未预期的错误：{exc!r}"
        )


# --------------------------------------------------------------------- 应用


def create_app(cfg: AppConfig) -> FastAPI:
    app = FastAPI(title="bili-scribe", version="0.1.0")
    state = AppState(cfg)
    app.state.ctx = state

    if not _qr_available():
        print(
            "[!] 缺少 qrcode / Pillow，扫码登录不可用；"
            '请执行 pip install "qrcode[pil]"（手动填 Cookie 不受影响）'
        )

    # ---------------------------------------------------------- 元信息

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        payload = state.cfg.public_view()
        # 把能力探测结果交给前端，让界面能提前把不可用的入口关掉，
        # 而不是等用户点了才报错。
        payload["features"] = {"qr_login": _qr_available()}
        return payload

    @app.get("/api/llm/check")
    def api_llm_check() -> dict[str, Any]:
        ok, detail = state.llm.health_check()
        return {"ok": ok, "detail": detail, "model": state.llm.describe()}

    # ------------------------------------------------------------ 登录

    @app.get("/api/auth/status")
    def api_auth_status() -> dict[str, Any]:
        client = state.new_client()
        credential = state.store.get_credential()
        identity = client.identity() if client.is_logged_in else None
        return {
            "logged_in": bool(identity),
            "has_credential": bool(credential),
            "identity": identity,
            "updated_at": credential.get("updated_at") if credential else None,
        }

    @app.get("/api/auth/qrcode")
    def api_auth_qrcode() -> dict[str, Any]:
        client = state.new_client()
        try:
            info = client.qr_generate()
        except BiliError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        try:
            image = _qr_png_data_url(info["url"])
        except QrUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        return {"key": info["key"], "url": info["url"], "image": image}

    @app.get("/api/auth/poll")
    def api_auth_poll(key: str = Query(min_length=8)) -> dict[str, Any]:
        client = state.new_client()
        try:
            status = client.qr_poll(key)
        except BiliError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        if status.get("status") != QR_SUCCESS:
            return status
        try:
            saved = client.save_session()
        except BiliError as exc:
            return {**status, "saved": None, "error": str(exc)}
        return {**status, "saved": saved}

    @app.post("/api/auth/cookie")
    def api_auth_cookie(payload: CookieRequest) -> dict[str, Any]:
        client = state.new_client()
        try:
            saved = client.login_with_cookies(
                payload.sessdata, payload.bili_jct, payload.dedeuserid
            )
        except BiliError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "saved": saved}

    @app.post("/api/auth/logout")
    def api_auth_logout() -> dict[str, Any]:
        client = state.new_client()
        client.logout()
        return {"ok": True}

    # ------------------------------------------------------------ 任务

    @app.post("/api/task")
    def api_create_task(payload: TaskRequest) -> dict[str, Any]:
        task_id = state.store.create_task(payload.raw_input)
        threading.Thread(
            target=_run_task,
            args=(state, task_id, payload.raw_input),
            daemon=True,
            name=f"task-{task_id}",
        ).start()
        return {"task_id": task_id}

    @app.get("/api/task/{task_id}")
    def api_get_task(task_id: str) -> dict[str, Any]:
        task = state.store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @app.get("/api/tasks")
    def api_list_tasks(limit: int = 20) -> list[dict[str, Any]]:
        return state.store.list_tasks(limit=max(1, min(limit, 100)))

    # ------------------------------------------------------------ 发布

    @app.post("/api/publish")
    def api_publish(payload: PublishRequest) -> dict[str, Any]:
        task = state.store.get_task(payload.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.get("status") != "ready":
            raise HTTPException(
                status_code=400,
                detail=f"任务当前状态是「{task.get('status')}」，还不能发布",
            )

        client = state.new_client()
        publisher = Publisher(client, state.store, state.cfg.publish)
        result = publisher.publish(
            payload.task_id,
            task["bvid"],
            task.get("aid"),
            payload.text,
            confirmed=payload.confirm,
        )
        return result.to_dict()

    @app.post("/api/publish/withdraw")
    def api_withdraw(payload: WithdrawRequest) -> dict[str, Any]:
        client = state.new_client()
        publisher = Publisher(client, state.store, state.cfg.publish)
        return publisher.withdraw(payload.publish_id).to_dict()

    @app.get("/api/publishes")
    def api_list_publishes(limit: int = 20) -> list[dict[str, Any]]:
        return state.store.list_publishes(limit=max(1, min(limit, 100)))

    # ------------------------------------------------------------ 静态页

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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app
