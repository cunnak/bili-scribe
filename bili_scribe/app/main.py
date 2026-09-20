"""FastAPI 应用：把领域层能力暴露成一组接口，并托管单页前端。"""

from __future__ import annotations

import base64
import io
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import NEEDS_RESTART, AppConfig, LlmConfig, apply_settings, save_config
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
    # 单次任务的角色覆盖。不传就用设置里的默认角色。
    role: str | None = None


class CookieRequest(BaseModel):
    sessdata: str
    bili_jct: str
    dedeuserid: str = ""


class PublishRequest(BaseModel):
    task_id: str
    text: str
    confirm: bool = False
    # auto：长文走笔记、短文走普通评论；也允许显式指定
    mode: Literal["auto", "comment", "note"] = "auto"


class WithdrawRequest(BaseModel):
    publish_id: int


# --------------------------------------------------------------- 设置接口
#
# 这里用 Pydantic 把取值范围卡死，而不是在前端做校验：前端的校验是给人看的，
# 服务端的校验才拦得住写坏配置。范围都留了足够余量，只挡明显不合理的值。


class ServerPatch(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)


class LlmPatch(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    model: str = Field(min_length=1, max_length=200)
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1, le=200000)
    max_input_chars: int = Field(ge=1000, le=2000000)
    chunk_size: int = Field(ge=200, le=200000)
    chunk_overlap: int = Field(ge=0, le=100000)
    # 用 Literal 而不是自由字符串：写错的 thinking 会被静默当成 auto，
    # 而它的错值恰好属于"看着没问题但行为不对"那一类，必须当场报出来。
    thinking: Literal["auto", "off", "on"] = "auto"
    timeout_seconds: int = Field(ge=5, le=3600)
    # 用 str + 显式校验而不是 Literal：角色清单定义在 core/roles.py 里，
    # 双份维护迟早会漂移。校验失败时把可选清单一起返回，比 422 有用。
    role: str = "standard"


class DanmakuPatch(BaseModel):
    enabled: bool
    also_with_subtitle: bool
    max_items: int = Field(ge=1, le=50000)
    hot_items: int = Field(ge=1, le=20000)
    sample_items: int = Field(ge=1, le=20000)


class CollectPatch(BaseModel):
    prefer_subtitle_lang: list[str] = Field(min_length=1, max_length=20)
    include_platform_summary: bool
    danmaku: DanmakuPatch


class PublishPatch(BaseModel):
    require_confirm: bool
    min_interval_seconds: int = Field(ge=0, le=3600)
    max_comment_chars: int = Field(ge=50, le=1000)
    disclose_ai: bool
    disclosure_text: str = Field(max_length=200)


class StoragePatch(BaseModel):
    db_path: str = Field(min_length=1, max_length=1000)
    key_path: str = Field(min_length=1, max_length=1000)


class SettingsPayload(BaseModel):
    server: ServerPatch
    llm: LlmPatch
    collect: CollectPatch
    publish: PublishPatch
    storage: StoragePatch
    # 关闭人工确认闸门时必须显式置真。这个校验在服务端做，不依赖前端弹窗——
    # 前端弹窗能拦住手滑，拦不住一个直接构造的请求。
    confirm_disable_gate: bool = False


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


def _run_task(state: AppState, task_id: str, raw_input: str, role: str | None = None) -> None:
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

        summary = summarizer.summarize(
            bundle, state.llm, state.cfg, progress=progress, role_key=role
        )
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
    app = FastAPI(title="bili-scribe", version=__version__)
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

    @app.get("/api/roles")
    def api_roles() -> dict[str, Any]:
        """可选角色清单。前端拿它渲染选择器，不用把清单抄一份到 JS 里。"""
        return {
            "roles": summarizer.roles.describe_roles(),
            "current": state.cfg.llm.role,
        }

    # ---------------------------------------------------------- 设置页面

    @app.get("/api/settings")
    def api_get_settings() -> dict[str, Any]:
        cfg = state.cfg
        path = cfg.source_path
        return {
            "values": cfg.settings_view(),
            "path": path,
            "config_exists": bool(path and Path(path).exists()),
            "gate_disabled": not cfg.publish.require_confirm,
            "restart_needed_for": list(NEEDS_RESTART),
            "platform_comment_max": 1000,
        }

    @app.put("/api/settings")
    def api_put_settings(payload: SettingsPayload) -> dict[str, Any]:
        cfg = state.cfg
        if not cfg.source_path:
            raise HTTPException(
                status_code=500,
                detail="配置没有来源路径，无法回写。请通过 run.py 启动，而不是直接起 uvicorn。",
            )

        # 跨字段校验：Pydantic 管不到字段之间的关系，这几条得自己查
        if payload.llm.chunk_overlap >= payload.llm.chunk_size:
            raise HTTPException(
                status_code=400,
                detail="chunk_overlap 必须小于 chunk_size，否则分块窗口不会向前推进。",
            )
        if payload.publish.max_comment_chars > 1000:
            raise HTTPException(
                status_code=400,
                detail="评论上限不能超过 1000，这是平台的硬限制。",
            )
        if payload.llm.role not in summarizer.roles.ROLE_KEYS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"未知的总结角色：{payload.llm.role}。可选："
                    + "、".join(summarizer.roles.ROLE_KEYS)
                ),
            )

        turning_off_gate = (
            cfg.publish.require_confirm and not payload.publish.require_confirm
        )
        if turning_off_gate and not payload.confirm_disable_gate:
            raise HTTPException(
                status_code=400,
                detail=(
                    "关闭人工确认闸门需要显式确认。关闭后总结生成会直接发到视频评论区，"
                    "没有确认环节；误发了只能自己去评论区删。"
                ),
            )

        restart_sections = apply_settings(cfg, payload.model_dump())

        try:
            written = save_config(cfg, cfg.source_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"配置写入失败：{exc}")

        # 热生效：LlmClient 在启动时构造一次，不重建的话换了 key/model 也不会生效，
        # 表现为"保存成功但测试连接还是老结果"。
        state.llm = LlmClient(cfg.llm)

        return {
            "ok": True,
            "written": written,
            "restart_required": bool(restart_sections),
            "restart_sections": restart_sections,
            "gate_disabled": not cfg.publish.require_confirm,
        }

    @app.post("/api/settings/test-llm")
    def api_test_llm(payload: LlmPatch | None = None) -> dict[str, Any]:
        """测试模型连通性。

        可以带上还没保存的值，这样能"先测再存"——不必为了试一个 key 先写盘再改回来。
        不传就用当前生效的配置。
        """
        llm_cfg = LlmConfig(**payload.model_dump()) if payload is not None else state.cfg.llm
        ok, detail = LlmClient(llm_cfg).health_check()
        return {
            "ok": ok,
            "detail": detail,
            "model": llm_cfg.model,
            "base_url": llm_cfg.base_url,
            "saved": payload is None,
        }

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
            args=(state, task_id, payload.raw_input, payload.role),
            daemon=True,
            name=f"task-{task_id}",
        ).start()
        return {"task_id": task_id, "role": summarizer.roles.get_role(payload.role or state.cfg.llm.role).key}

    @app.get("/api/task/{task_id}")
    def api_get_task(task_id: str) -> dict[str, Any]:
        task = state.store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        # 发布版长文（不是概括版）。在前端展示"将要发出去的东西"，
        # 而且让确认闸门对着真实内容生效——人对着概括版点确认，等于没确认。
        summary_json = task.get("summary_json")
        if summary_json:
            try:
                task["publish_document"] = composer.build_publish_document(
                    summarizer.Summary(**summary_json)
                )
            except (TypeError, ValueError):
                task["publish_document"] = ""
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

        # 长文走笔记、短文走普通评论。用户明确要求发完整版而不是概括版，
        # 而完整版普遍超过普通评论 1000 字符的硬上限（12025），
        # 所以默认策略是"超长即笔记"，而不是逼用户二选一。
        document = composer.build_publish_document(
            summarizer.Summary(**(task.get("summary_json") or {}))
        )
        use_note = payload.mode == "note" or (
            payload.mode == "auto" and len(payload.text or document) > 1000
        )

        if use_note:
            result = publisher.publish_note(
                payload.task_id,
                task["bvid"],
                task.get("aid"),
                title=task.get("title") or "视频笔记",
                # 评论卡展示的是 summary；官方建议前 120 字符是提炼性内容，
                # 所以这里用一句话结论，而不是正文开头
                summary_text=composer.to_plain_text(
                    (task.get("summary_json") or {}).get("one_line") or payload.text
                ),
                content_delta=composer.markdown_to_delta(payload.text or document),
                confirmed=payload.confirm,
            )
        else:
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
