"""领域服务层：所有业务动作的实现，**不依赖 HTTP**。

这一层存在的理由只有一个：桌面应用和 HTTP 服务必须跑同一份逻辑。

原来的结构是把业务代码直接写在 FastAPI 的路由函数里——26 个路由里嵌着校验、
状态机、线程调度和异常翻译。那样一来，第二个入口（pywebview 的 `js_api` 桥）
就只能二选一：要么复制一遍逻辑（两份迟早漂移，而且漂移的表现是"桌面上能发、
网页上不能发"这种最难查的 bug），要么让桌面端也起一个 HTTP 服务（把端口和
轮询一起带回来，那正是要干掉的东西）。

所以这里把边界划在"业务"和"传输"之间：

- 这一层（service.py）只认 Python 值和 `ServiceError(status, detail)`；
- 上层（main.py 的 FastAPI 路由、desktop.py 的 js_api）只做传输转换：
  把 `ServiceError` 翻成 HTTPException 或 `{"error": ...}`。

**没有引入新的抽象**：`AppState`、`_run_task`、`_run_refine` 这些原样搬过来，
行为一字未改。唯一新增的是 `Service` 这个薄包装，它的每个方法对应原来路由
函数体里的那十几行。
"""

from __future__ import annotations

import base64
import copy
import io
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .. import __version__
from ..config import (
    NEEDS_RESTART,
    PROXY_SCHEMES,
    AppConfig,
    LlmConfig,
    apply_settings,
    save_config,
)
from ..core import collector as collector_mod
from ..core import composer, resolver, summarizer
from ..core import frames as frames_mod
from ..core import srt as srt_mod
from ..core.collector import Bundle, CollectError
from ..core.publisher import Publisher
from ..core.resolver import ResolveError
from ..core.summarizer import SummarizeError
from ..infra import uploads
from ..infra.bili_client import BiliClient, BiliError
from ..infra.endpoints import QR_SUCCESS
from ..infra.llm import LlmClient, LlmError
from ..infra.notecache import NoteCache
from ..infra.store import Store


class ServiceError(RuntimeError):
    """业务层拒绝了一个请求。`status` 沿用 HTTP 语义，方便两层各自翻译。

    为什么不直接抛 HTTPException：那样这一层就得 import fastapi，而它要能被
    一个没有 fastapi 进程的桌面桥调用（打包时也可以不带 fastapi）。用数字状态码
    而不是自定义枚举，是因为上层要的就是"该回 409 还是 400"，多一层映射没有收益。
    """

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class QrUnavailable(RuntimeError):
    """二维码依赖缺失或渲染失败。

    单独成一个异常类型，是为了让接口能返回一句"照着做就行"的提示，
    而不是一个没有任何线索的 500。
    """


def _api(func: Callable[..., Any]) -> Callable[..., Any]:
    """把领域异常翻成 ServiceError。只包公开方法，不改任何业务行为。

    BiliError → 502、QrUnavailable → 503：这两个状态码原来由 FastAPI 的
    `exception_handler` 给，桌面端没有那层处理器，所以搬到这里统一翻译。
    其余异常一律放行——未预期的错误必须让上层看到原始类型，别在这里吞掉。
    """

    import functools

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ServiceError:
            raise
        except QrUnavailable as exc:
            raise ServiceError(503, str(exc)) from exc
        except BiliError as exc:
            raise ServiceError(502, str(exc)) from exc

    return wrapper


def qr_available() -> bool:
    try:
        import qrcode  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


# 保留旧名字：main.py 与测试都在用它。
_qr_available = qr_available


# ------------------------------------------------------------------- 请求体
#
# 这些模型留在这一层而不是 main.py：桌面桥拿到的也是同样的 dict，
# 必须用同一套规则校验。字段级约束交给 Pydantic，跨字段的语义校验在方法里手写。


class TaskRequest(BaseModel):
    raw_input: str = Field(min_length=1, max_length=2000)
    # 单次任务的角色覆盖。不传就用设置里的默认角色。
    role: str | None = None
    # 使用者上传的 srt 的临存 token（由 parse_subtitle 签发）。
    # 传了就**优先用这份字幕**，平台字幕让位。
    subtitle_token: str | None = None


class RefineRequest(BaseModel):
    """一次迭代打磨的输入。"""

    # 本次想怎么改。这是唯一必填的东西——链接、素材、角色都沿用原任务。
    requirement: str = Field(min_length=1, max_length=2000)
    # 允许换角色，但那语义上等于「重开一版」：不继承上一版，从素材重新起步。
    # 不传就沿用原任务那次用的角色。
    role: str | None = None
    # 你是在看着第几版提这个要求的。服务端会核对它是否仍是当前版——不符就拒绝，
    # 免得要求落在别的底稿上（比如另一个标签页刚改过一版）。不传则不核对。
    base_version: int | None = None


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
    # 空 = 直连（且不读环境变量代理）。非空必须是带协议的完整地址。
    proxy: str = Field(default="", max_length=500)
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


class RoleRequest(BaseModel):
    """切换默认角色。只带一个字段，见 `Service.set_role` 的说明。"""

    role: str = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------- 状态


@dataclass
class RefineContext:
    """一次迭代打磨所需的上下文。

    `bundle.body` **已经被换成分段笔记**，不是素材原文——所以内存里躺着的始终是
    模型写出来的摘要，第三方原文既不落库也不留在内存里。这和画面帧、上传件
    「用完即删」是同一条原则。

    元数据（标题、UP 主、时长、标签、来源）照常带着：归约时的提示词要用它们，
    而且这些都是很小的、不敏感的字段。
    """

    bundle: Bundle
    material: summarizer.PreparedMaterial


class AppState:
    # 预读缓存最多留几条。
    #
    # 这个数字是**量出来的**，不是估的（脚本见项目根的 refine-cache-budget.py）：
    #   · 一条缓存 = 39 KB（典型：4 小时视频的笔记 19,772 字）
    #              = 94 KB（最坏：笔记被截断到 max_input_chars×2 = 48,000 字）
    #   · 200 条 → 典型 7.7 MB / 最坏 18 MB
    #   · **原素材不跟着进缓存**：body 换成分段笔记时就被丢掉了。实测把素材从
    #     11,703 字符换成 600,000 字符，20 条缓存占用 0.77 MB 纹丝不动。
    #
    # ⚠️ 它原来是 20，理由是"不设上限跑几十条就白占几百 MB"——**错了约三个数量级**
    # （20 条实际只有 0.77 MB）。20 条的代价是使用者被莫名挡住：总结满 20 条之后，
    # 早先那条视频想再改就得整条重跑（4 小时视频 = 86,428 tokens，是真花钱），
    # 而省下来的是不到 1 MB 内存。
    #
    # 内存几乎免费、重跑很贵——这个不对称就是取 200 的理由：一个服务活到总结 200 条
    # 视频之前几乎碰不到上限，最坏也不过 18 MB。（要再往上抬也行，1000 条约 40-95 MB。）
    MAX_PREPARED = 200

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.store = Store(cfg.db_path, cfg.key_path, logger=lambda m: print(f"[store] {m}"))
        self.llm = LlmClient(cfg.llm)
        # 迭代打磨用的预读缓存：task_id → RefineContext。
        #
        # **这一层只放内存，但它已经不是唯一的缓存了。** 原注释写的是「刻意只放内存，
        # 不落库」，理由是笔记是第三方内容的衍生品、丢了重新预读就能重建。那条判断在
        # 「单任务内反复打磨」的场景下成立，但账本暴露了它漏掉的场景：同一个 bvid 被
        # 重开成两个任务时，两次各自付了一次完整预读。
        # 实测 `BV1hBnC6ZEug`：task 29bf75337d59 与 07ef9bf7f66c 各付约 13 万 prompt
        # tokens 做 7 段预读，第二遍的 13 万完全是白付的。
        #
        # 所以现在多了一层按内容寻址的磁盘缓存（infra/notecache.py），跨任务、跨进程
        # 复用。这一层内存字典仍保留：同一任务内的打磨命中它更快，且不碰磁盘。
        self.prepared: dict[str, RefineContext] = {}
        self.prepared_lock = threading.Lock()
        self.notes = NoteCache(cfg.notes_dir, logger=lambda m: print(f"[notes] {m}"))
        # 正在迭代打磨的任务。存在的理由是"同一版被改两遍"：两次请求会拿到同一份
        # 上一版，然后一前一后各写一条版本记录，而两条都声称自己基于第 N 版。
        # 界面上按钮会禁用，但按钮不是防线。
        self.refining: set[str] = set()
        self.refine_lock = threading.Lock()
        # 任务状态变化的推送出口。HTTP 模式下是 None（前端自己轮询）；
        # 桌面模式下由 desktop.py 挂上，把更新直接 evaluate_js 推给页面，
        # 于是 1.2 秒一次的轮询彻底消失。
        # 签名是 (task_id: str) -> None，实现方自己去库里读最新状态。
        self.on_task_change: Callable[[str], None] | None = None

    def new_client(self) -> BiliClient:
        """每个请求用独立会话。Cookie 统一从 SQLite 恢复，不用跨线程共享连接。"""
        return BiliClient(self.store, logger=lambda m: print(f"[bili] {m}"))

    def new_llm(self) -> LlmClient:
        """每次任务／每轮迭代单独建一个模型客户端。

        三个理由，按重要性排：
        1. **消耗记账不能串**。累计 token 挂在客户端实例上，共用一个实例时，
           两个并发任务会把彼此的账算到一起——而"这一版花了多少"恰恰是迭代
           打磨这个功能的核心信息，算错就等于这个功能没做。
        2. 配置改动后新任务本就该用新配置，不必等重建共享实例。
        3. 代价近似为零：它只是一个 requests.Session。
        """
        return LlmClient(self.cfg.llm)

    def begin_refine(self, task_id: str) -> bool:
        """尝试占住这条视频的迭代名额。返回 False 表示已经有一版在改了。"""
        with self.refine_lock:
            if task_id in self.refining:
                return False
            self.refining.add(task_id)
            return True

    def end_refine(self, task_id: str) -> None:
        with self.refine_lock:
            self.refining.discard(task_id)

    def is_refining(self, task_id: str) -> bool:
        with self.refine_lock:
            return task_id in self.refining

    def remember_prepared(self, task_id: str, material) -> None:
        """存下这次任务的预读结果，供后续迭代复用。超出上限就淘汰最早的一条。"""
        with self.prepared_lock:
            self.prepared.pop(task_id, None)  # 先删再插，让它排到队尾
            self.prepared[task_id] = material
            while len(self.prepared) > self.MAX_PREPARED:
                self.prepared.pop(next(iter(self.prepared)))

    def get_prepared(self, task_id: str):
        """读预读结果。

        叫 get 不叫 take：**它不删除**——打磨是反复的，同一份笔记要支撑很多轮修订，
        每轮都取走的话第二次就没了。
        """
        with self.prepared_lock:
            return self.prepared.get(task_id)

    def emit(self, task_id: str) -> None:
        """把"这条任务变了"推出去。没有订阅者时是空操作，所以 HTTP 路径零影响。"""
        sink = self.on_task_change
        if sink is None:
            return
        try:
            sink(task_id)
        except Exception:  # 推送失败绝不能反过来把任务搞挂
            traceback.print_exc()


# --------------------------------------------------------------------- 工具


def qr_png_data_url(content: str) -> str:
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


# 旧名字，测试与 main.py 都在用。
_qr_png_data_url = qr_png_data_url


def metrics_of(llm, started: float, **extra: Any) -> dict[str, Any]:
    """一次生成／一轮迭代的账：花了多少 token、跑了多久、这次的分块与画面。

    时间也要记：它是使用者唯一能直接感知的代价，而"这一版 36 秒、上一版 168 秒"
    正是"迭代跳过了预读"这件事最直观的证据（详见 README 的两条实测轨迹）。
    """
    return {
        "usage": llm.usage.to_dict(),
        "elapsed": round(time.monotonic() - started, 1),
        **extra,
    }


_metrics_of = metrics_of


def sum_usage(versions: list[dict[str, Any]]) -> dict[str, int]:
    """把各版本的消耗加起来。

    `restore` 不参与累计也不计入 untracked：那一版只是把旧版全文重新设为当前版，
    **根本没调用模型**，消耗是确定的 0，而不是"不知道"。把它当成"未记录"会让人
    以为账丢了。
    """
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "reasoning_tokens": 0, "total_tokens": 0, "unpriced": 0}
    untracked = 0
    for version in versions:
        if version.get("source") == "restore":
            continue
        usage = (version.get("metrics") or {}).get("usage")
        if not usage:
            # 升级前生成的老版本：当时根本没有记账这一回事。如实说"未记录"，
            # 不要用 0 冒名顶替——那会让人以为这条视频真的没花钱。
            untracked += 1
            continue
        for name in total:
            total[name] += int(usage.get(name) or 0)
    total["untracked"] = untracked
    return total


_sum_usage = sum_usage


def version_brief(
    version: dict[str, Any],
    current: int | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """版本列表用的一行。**不含全文**——列表要轻，全文由详情接口按需取。

    `changed_fields` 是「相对上一版，实际改了哪些字段」，本地比对算出来的（见
    summarizer.diff_fields）。三种取值，语义完全不同，所以不能混：

    - `None`：**没比**（第 1 版没有上一版；回退版没有"改"这回事）
    - `[]`：比过了，**一个字都没变**——这本身就是有用信息（说明要求没被满足）
    - 非空：逐字段列出改动

    界面上这三种说法必须不一样，否则"没比过"会被读成"没改动"。
    """
    summary = version.get("summary_json") or {}
    changed: list[dict[str, Any]] | None = None
    if previous is not None and version.get("source") != "restore":
        changed = summarizer.diff_fields(previous.get("summary_json") or {}, summary)
    return {
        "version_no": version["version_no"],
        "source": version["source"],
        "requirement": version.get("requirement") or "",
        "role": version.get("role") or "",
        "created_at": version.get("created_at") or "",
        "one_line": summary.get("one_line") or "",
        # 「这一版改了什么」是使用者判断"我的要求被听进去了没有"的唯一依据，
        # 所以它必须出现在列表里，而不是藏在详情里。
        "revise_note": summary.get("revise_note") or "",
        "changed_fields": changed,
        "chars": len(version.get("comment_text") or ""),
        # 消耗记录。**这一层统一归一成对象**：库里没有记录是 NULL，接口发出去是 {}，
        # 消费方永远可以安全地 .get("usage")，不必到处判空。
        # 「空对象」本身不区分"没记账"和"没消耗"——那由 source 区分：
        # restore 是真的没调模型（确定的 0），其余为空就是当时还没有记账这回事。
        # ⚠️ 别把这里"修"成 None：那样 `metrics.get(...)` 这类写法会直接崩，
        # 而且三个消费方（界面、离线用例、活体验证脚本）都按对象写的。
        "metrics": version.get("metrics") or {},
        "current": version["version_no"] == current,
    }


_version_brief = version_brief


def publish_document_of(summary_json: dict[str, Any] | None) -> str:
    """把一版的 summary_json 渲染成发布版长文。解析不了就返回空串。

    单独抽出来是因为它有四个调用点（任务当前版、某一版详情、发布、以及设置页的
    预览），而每一处都要吞掉"旧数据结构缺字段"这类异常——不吞的话，一条老记录
    能让整个任务详情接口 500。
    """
    if not summary_json:
        return ""
    try:
        return composer.build_publish_document(summarizer.Summary(**summary_json))
    except (TypeError, ValueError):
        return ""


_publish_document_of = publish_document_of


def refine_gate(state: AppState, task: dict[str, Any]) -> tuple[bool, str]:
    """能不能在这条任务上再改一版？不能的话，理由是什么。

    **界面和接口共用这一个函数**，这是刻意的：如果界面用一套判断、接口用另一套，
    迟早会出现"按钮是亮的、点了报错"或者"按钮是灰的、其实能改"。把理由也一并
    返回，界面就能把话说在点击之前。
    """
    task_id = task["id"]
    if not task.get("summary_json"):
        return False, "这条还没有生成过总结，先跑一次总结，之后就能在它上面反复打磨。"
    if task.get("bvid"):
        live = state.store.find_live_publish(task["bvid"])
        if live:
            # 这是"迭代入口只在发布前"那条决定的落点：评论发出去就不能编辑了，
            # 这时候再改只能是为了撤回重发，性质变了，所以要先把话说清楚。
            return False, (
                "这条视频的总结已经发出去了（撤回前不能再改）。B站 的评论发出去不能编辑，"
                "要改得先到左边「历史发布」里把那条撤回，这里就会重新可用。"
            )
    if state.is_refining(task_id):
        return False, "正在改这一版，等它出结果再提新要求。"
    if state.get_prepared(task_id) is None:
        # 两个原因都会走到这里，而且**都得说**：程序重启过；或者这一轮总结的条数
        # 超过了缓存上限，这条被挤出去了。只说"重启过"会在第二种情况下变成一句假话
        # ——对着一个没重启过程序的人说"你重启过"，比不说更糟。
        # 数字从 MAX_PREPARED 取，不写死：否则调了上限、界面上还在说旧数。
        return False, (
            "这条视频的素材缓存已经不在内存里了（程序重启过，或者中间总结的视频"
            f"超过了 {state.MAX_PREPARED} 条、把它挤了出去）。重新跑一次这条视频"
            "就能继续打磨——重跑会顺带把缓存建回来，而你之前改过的版本都还在。"
        )
    return True, ""


_refine_gate = refine_gate


def error_detail(exc: BaseException) -> str:
    """把异常写成「能查」的样子：消息 + 出错的代码位置。

    **这一条是踩出来的。** 迭代那一版曾经在 1.5 秒内失败，库里只留下
    `FileNotFoundError(2, 'No such file or directory')`——连文件名都没有，
    因为 `{exc!r}` 只打参数、不打 `.filename`（文件名在 `str(exc)` 里，而 `repr`
    比 `str` 更像"技术细节"，当初就顺手用了 repr）。结果是：报错进了库、界面上也
    显示了，但**没有任何线索指向哪一行**，只能靠一步步手工复现去找。
    所以这里把最后两帧的文件、行号、函数名一起带上——它不影响正常使用，
    但把一个"无从下手"的报错变成一个"看一眼就知道"的报错。
    """
    frames = traceback.extract_tb(exc.__traceback__)[-2:]
    where = " ← ".join(
        f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in frames
    )
    text = f"{type(exc).__name__}: {exc}" if str(exc) else repr(exc)
    return f"{text}（{where}）" if where else text


_error_detail = error_detail


def run_task(
    state: AppState,
    task_id: str,
    raw_input: str,
    role: str | None = None,
    subtitle_token: str | None = None,
) -> None:
    """后台跑完整条链：解析 → 采集 → 画面 → 总结 → 组装。失败就落错误，不吞异常。"""
    store = state.store
    started = time.monotonic()
    # 这次任务专用的模型客户端：消耗要归到这条任务、而不是所有任务的总和。
    llm = state.new_llm()

    # 顺手收掉上次异常退出留下的画面缓存，以及"传了文件却没提交任务"的上传件。
    # 放这里而不是只在启动时做，是因为这个应用可能连着跑好几天不重启，
    # 而残留恰恰是"那一次异常"留下的——只在启动时清，等于指望使用者去重启它。
    # 清理失败不能影响任务，所以单独吞掉。
    for purge in (frames_mod.purge_stale, uploads.purge_stale):
        try:
            purge(state.cfg.db_path)
        except OSError:
            pass

    def progress(stage: str, message: str) -> None:
        store.update_task(task_id, status=stage, progress=message)
        state.emit(task_id)

    try:
        client = state.new_client()

        progress("resolving", "解析输入")
        target = resolver.resolve(raw_input, client)
        store.update_task(task_id, bvid=target.bvid, page=target.page)

        # 上传件在创建任务时已校验过存在性。真读不到（极端情况下被兜底清理捡走了）
        # 就按"没有上传件"继续——采集层会降级回平台字幕，不会让任务失败。
        user_srt = (
            uploads.load(state.cfg.db_path, subtitle_token) if subtitle_token else None
        )

        bundle = collector_mod.collect(
            client,
            target.bvid,
            target.page,
            state.cfg.collect,
            user_srt=user_srt,
            progress=progress,
        )

        # 画面是加分项，不是必需品：拿不到就如实记一条，绝不让整条任务失败。
        # 主动关掉时（enabled=False）不记——那是使用者的选择，不是异常。
        frameset = None
        warnings = list(bundle.warnings)
        if state.cfg.collect.frames.enabled:
            try:
                frameset = frames_mod.collect_frames(
                    client,
                    state.cfg.collect.frames,
                    bundle.aid,
                    state.cfg.db_path,
                    progress=progress,
                )
            except frames_mod.FrameError as exc:
                warnings.append(f"未附画面：{exc}")
            else:
                warnings.extend(f"画面：{note}" for note in frameset.notes)

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
            warnings=warnings,
            candidates=bundle.checks_as_dicts(),
            platform_summary=bundle.platform_summary,
        )

        # 预读单独跑一次，结果连同一份「笔记版 bundle」留在内存里——后续
        # 「再改一版」直接复用，省掉整段预读（4 小时视频 32,663 tokens，占 38%）。
        #
        # 缓存的是**笔记版**：body 已被替换成分段笔记，素材原文不进来。
        # 传 state.notes 是为了跨任务复用：同一个 bvid 被重开成第二个任务时，
        # 这一整段预读直接命中磁盘、一次模型调用都不发。
        material = summarizer.prepare_material(
            bundle, llm, state.cfg, progress=progress, cache=state.notes
        )
        note_bundle = copy.copy(bundle)
        note_bundle.body = material.body
        state.remember_prepared(
            task_id, RefineContext(bundle=note_bundle, material=material)
        )

        summary = summarizer.summarize(
            bundle,
            llm,
            state.cfg,
            progress=progress,
            role_key=role,
            frameset=frameset,
            prepared=material,
        )
        comment_text = composer.build_comment(summary, bundle, state.cfg.publish)
        preview_md = composer.build_preview(bundle, summary, state.cfg.publish)

        # 初版也走版本落库，而不是只写 task 表：这样「版本史」从第 1 版起就是完整的，
        # 后面每次迭代只是往后追加，界面不需要为"还没有历史"再写一套分支。
        store.record_version(
            task_id,
            source="initial",
            summary_json=summary.to_dict(),
            preview_md=preview_md,
            comment_text=comment_text,
            role=summary.role,
            metrics=metrics_of(
                llm,
                started,
                chunks=material.chunks,
                frames=summary.frames,
                cached_material=material.cached,
            ),
            status="ready",
            progress="总结已生成，等待你确认",
            error=None,
        )
    except (ResolveError, CollectError, SummarizeError, BiliError, LlmError) as exc:
        store.update_task(task_id, status="failed", progress="失败", error=str(exc))
    except Exception as exc:  # 兜底：任何未预期异常都不能让任务永远停在中间态
        # 未预期 = 代码有毛病，所以除了写进库，还把整条栈打到控制台（那个窗口
        # 就是服务日志）。写进库的那份只带最后两帧，免得界面被栈刷屏。
        traceback.print_exc()
        store.update_task(
            task_id, status="failed", progress="失败", error=f"未预期的错误：{error_detail(exc)}"
        )
    finally:
        # 上传件用完即删。画面帧**不在这里删**——它在 collect_frames 内部就收掉了
        # （谁建谁删），因为按任务收尾会踩到"迭代正在用同一个目录"的竞争，
        # 详见 core/frames.py 的 _new_cache_dir。
        if subtitle_token:
            try:
                uploads.discard(state.cfg.db_path, subtitle_token)
            except OSError:
                pass
        state.emit(task_id)


_run_task = run_task


def run_refine(
    state: AppState,
    task_id: str,
    ctx: RefineContext,
    requirement: str,
    role: str | None,
    previous: str,
) -> None:
    """后台跑一次「按使用者的要求修订」。

    **不重新采集、不重新预读**——素材笔记直接从 ctx 拿。这就是迭代打磨的全部
    意义：同一份素材改很多版，只付一次预读的钱（4 小时视频省下 32,663 tokens）。
    """
    store = state.store
    started = time.monotonic()
    llm = state.new_llm()

    def progress(_stage: str, message: str) -> None:
        # 状态统一记成 refining，不写流水线那套阶段名。理由：这一轮里
        # 「解析 → 采集 → 预读」一件都没发生，让界面把它们显示成一条走完的流水线，
        # 等于在告诉使用者它跑了一遍完整流程——而"跳过预读"正是这个功能的价值所在。
        store.update_task(task_id, status="refining", progress=message)
        state.emit(task_id)

    warnings: list[str] = []
    frameset = None
    aid = ctx.bundle.aid

    try:
        # 画面帧重新取一次，而不是「上次取过就算了」：上一版里那些只有画面才有的
        # 信息（PPT、图表上的字）如果不重新给，修订版会**悄悄丢掉它们**——
        # 产出看起来更贴合要求了，实际信息基础却退了一截。
        if state.cfg.collect.frames.enabled and aid:
            try:
                frameset = frames_mod.collect_frames(
                    state.new_client(),
                    state.cfg.collect.frames,
                    aid,
                    state.cfg.db_path,
                    progress=progress,
                )
            except frames_mod.FrameError as exc:
                warnings.append(f"本次未附画面：{exc}")

        summary = summarizer.summarize(
            ctx.bundle,
            llm,
            state.cfg,
            progress=progress,
            role_key=role,
            frameset=frameset,
            prepared=ctx.material,
            refinement=summarizer.Refinement(
                requirement=requirement, previous=previous
            ),
        )
        comment_text = composer.build_comment(summary, ctx.bundle, state.cfg.publish)
        preview_md = composer.build_preview(ctx.bundle, summary, state.cfg.publish)

        store.record_version(
            task_id,
            source="refine",
            requirement=requirement,
            summary_json=summary.to_dict(),
            preview_md=preview_md,
            comment_text=comment_text,
            role=summary.role,
            metrics=metrics_of(llm, started, chunks=summary.chunks, frames=summary.frames),
            status="ready",
            progress="已按你的要求改好一版，等待确认",
            warnings=(ctx.bundle.warnings or []) + warnings,
            error=None,
        )
    except (SummarizeError, LlmError) as exc:
        # ⚠️ 失败时把状态放回 ready，不是 failed：**上一版还在**，还能继续用、
        # 继续改。打成 failed 会把已有成果一起作废——那是最不该发生的事。
        # 也不写版本：没改成的那一版不是一版，记进去只会让版本史说谎。
        store.update_task(
            task_id, status="ready", progress=f"这一版没改成：{exc}", error=str(exc)
        )
    except Exception as exc:  # 兜底，理由同上
        traceback.print_exc()
        store.update_task(
            task_id,
            status="ready",
            progress=f"这一版没改成：{error_detail(exc)}",
            error=f"未预期的错误：{error_detail(exc)}",
        )
    finally:
        # 无论成败都要放开迭代名额，否则这一次失败会让这条视频永远改不动。
        # 画面帧不在这里删：collect_frames 自己建、自己收（见 core/frames.py）。
        state.end_refine(task_id)
        state.emit(task_id)


_run_refine = run_refine


# ------------------------------------------------------------------- 服务门面


class Service:
    """业务动作的入口。每个方法对应原来一个路由函数体。

    **刻意不持有线程池**：任务线程由 `run_task` 自己起，和原来一样。这里只负责
    校验与状态流转，把 HTTP 那层的意思表达成 Python 调用。
    """

    def __init__(self, cfg: AppConfig):
        self.state = AppState(cfg)
        # 兜底清一次画面缓存。正常路径下每个任务结束就删了，这里清的是上次
        # 异常退出（被杀／崩溃／断电）留下的残留——否则它们会一直躺在磁盘上。
        stale = frames_mod.purge_stale(cfg.db_path)
        if stale:
            print(f"[i] 清掉了 {stale} 个上次未清理的画面缓存目录")
        if not qr_available():
            print(
                "[!] 缺少 qrcode / Pillow，扫码登录不可用；"
                '请执行 pip install "qrcode[pil]"（手动填 Cookie 不受影响）'
            )

    # ---------------------------------------------------------- 元信息

    @_api
    def config(self) -> dict[str, Any]:
        payload = self.state.cfg.public_view()
        # 把能力探测结果交给前端，让界面能提前把不可用的入口关掉，
        # 而不是等用户点了才报错。
        payload["features"] = {"qr_login": qr_available()}
        # 版本号只有一处定义（`bili_scribe/__init__.py`），这里读出来交给界面显示。
        # **不要在前端另抄一份**：那样就会出现两个都会过期的数字，而界面上显示的
        # 往往是你最信的那个。
        payload["version"] = __version__
        return payload

    @_api
    def llm_check(self) -> dict[str, Any]:
        ok, detail = self.state.llm.health_check()
        return {"ok": ok, "detail": detail, "model": self.state.llm.describe()}

    @_api
    def roles(self) -> dict[str, Any]:
        """可选角色清单。前端拿它渲染选择器，不用把清单抄一份到 JS 里。"""
        return {
            "roles": summarizer.roles.describe_roles(),
            "current": self.state.cfg.llm.role,
        }

    @_api
    def set_role(self, payload: RoleRequest) -> dict[str, Any]:
        """只改「默认角色」这一项，并立刻写盘。

        单独开一个接口，而不是让顶栏去走 `put_settings`：那条路要求提交整份设置，
        而整份设置里含明文 api_key——为了切一个下拉框，让密钥在请求体里多走一遍，
        是白白多出来的暴露面；而且任何一个字段填错都会连带覆盖别的配置。

        这里只碰 `llm.role`。注意它影响的是**下一次**总结：`create_task` 在
        `payload.role` 为空时读的就是这个值。已经在跑的任务、以及「迭代打磨」
        （它沿用原任务那次用的角色）都不受影响。
        """
        role = payload.role
        if role not in summarizer.roles.ROLE_KEYS:
            raise ServiceError(
                400,
                f"未知的总结角色：{role}。可选："
                + "、".join(summarizer.roles.ROLE_KEYS),
            )

        cfg = self.state.cfg
        if not cfg.source_path:
            raise ServiceError(
                500,
                "配置没有来源路径，无法回写。请通过 run.py 启动，而不是直接起 uvicorn。",
            )

        previous = cfg.llm.role
        cfg.llm.role = role
        try:
            save_config(cfg, cfg.source_path)
        except OSError as exc:
            # 写盘失败就把内存里的值退回去。否则界面显示新角色、进程一重启又变回旧的，
            # 而这种"改了但没存上"最容易被当成角色不生效的玄学问题。
            cfg.llm.role = previous
            raise ServiceError(500, f"配置写入失败：{exc}") from exc

        info = summarizer.roles.get_role(role)
        return {
            "ok": True,
            "role": info.key,
            "name": info.name,
            "passes": info.passes,
            "path": cfg.source_path,
        }

    # ---------------------------------------------------------- 设置页面

    @_api
    def get_settings(self) -> dict[str, Any]:
        cfg = self.state.cfg
        path = cfg.source_path
        return {
            "values": cfg.settings_view(),
            "path": path,
            "config_exists": bool(path and Path(path).exists()),
            "gate_disabled": not cfg.publish.require_confirm,
            "restart_needed_for": list(NEEDS_RESTART),
            "platform_comment_max": 1000,
        }

    @_api
    def put_settings(self, payload: SettingsPayload) -> dict[str, Any]:
        cfg = self.state.cfg
        if not cfg.source_path:
            raise ServiceError(
                500,
                "配置没有来源路径，无法回写。请通过 run.py 启动，而不是直接起 uvicorn。",
            )

        # 跨字段校验：Pydantic 管不到字段之间的关系，这几条得自己查
        if payload.llm.chunk_overlap >= payload.llm.chunk_size:
            raise ServiceError(
                400,
                "chunk_overlap 必须小于 chunk_size，否则分块窗口不会向前推进。",
            )
        if payload.publish.max_comment_chars > 1000:
            raise ServiceError(
                400, "评论上限不能超过 1000，这是平台的硬限制。"
            )
        if payload.llm.role not in summarizer.roles.ROLE_KEYS:
            raise ServiceError(
                400,
                f"未知的总结角色：{payload.llm.role}。可选："
                + "、".join(summarizer.roles.ROLE_KEYS),
            )
        # 代理写错必须当场挡掉。它会被写进磁盘，而 AppConfig.from_raw 在加载
        # 时会校验协议前缀——放过去的话，保存那一刻不报错，**下次启动直接起不来**。
        proxy = (payload.llm.proxy or "").strip()
        if proxy:
            scheme = proxy.split("://", 1)[0].lower()
            if "://" not in proxy or scheme not in PROXY_SCHEMES:
                raise ServiceError(
                    400,
                    f"代理地址要带协议，例如 http://127.0.0.1:7897。收到：{proxy}",
                )
        payload.llm.proxy = proxy

        turning_off_gate = (
            cfg.publish.require_confirm and not payload.publish.require_confirm
        )
        if turning_off_gate and not payload.confirm_disable_gate:
            raise ServiceError(
                400,
                "关闭人工确认闸门需要显式确认。关闭后总结生成会直接发到视频评论区，"
                "没有确认环节；误发了只能自己去评论区删。",
            )

        restart_sections = apply_settings(cfg, payload.model_dump())

        try:
            written = save_config(cfg, cfg.source_path)
        except OSError as exc:
            raise ServiceError(500, f"配置写入失败：{exc}") from exc

        # 热生效：LlmClient 在构造时定住了配置，不重建的话换了 key/model 也不会生效，
        # 表现为"保存成功但测试连接还是老结果"。
        self.state.llm = LlmClient(cfg.llm)

        return {
            "ok": True,
            "written": written,
            "restart_required": bool(restart_sections),
            "restart_sections": restart_sections,
            "gate_disabled": not cfg.publish.require_confirm,
        }

    @_api
    def test_llm(self, payload: LlmPatch | None = None) -> dict[str, Any]:
        """测试模型连通性。

        可以带上还没保存的值，这样能"先测再存"——不必为了试一个 key 先写盘再改回来。
        不传就用当前生效的配置。
        """
        llm_cfg = (
            LlmConfig(**payload.model_dump()) if payload is not None else self.state.cfg.llm
        )
        ok, detail = LlmClient(llm_cfg).health_check()
        return {
            "ok": ok,
            "detail": detail,
            "model": llm_cfg.model,
            "base_url": llm_cfg.base_url,
            "saved": payload is None,
        }

    # ------------------------------------------------------------ 登录

    @_api
    def auth_status(self) -> dict[str, Any]:
        client = self.state.new_client()
        credential = self.state.store.get_credential()
        identity = client.identity() if client.is_logged_in else None
        return {
            "logged_in": bool(identity),
            "has_credential": bool(credential),
            "identity": identity,
            "updated_at": credential.get("updated_at") if credential else None,
        }

    @_api
    def auth_qrcode(self) -> dict[str, Any]:
        client = self.state.new_client()
        info = client.qr_generate()  # BiliError → 502，由 _api 翻
        image = qr_png_data_url(info["url"])  # QrUnavailable → 503
        return {"key": info["key"], "url": info["url"], "image": image}

    @_api
    def auth_poll(self, key: str) -> dict[str, Any]:
        if not isinstance(key, str) or len(key) < 8:
            raise ServiceError(400, "二维码 key 无效，请重新获取。")
        client = self.state.new_client()
        status = client.qr_poll(key)
        if status.get("status") != QR_SUCCESS:
            return status
        try:
            saved = client.save_session()
        except BiliError as exc:
            return {**status, "saved": None, "error": str(exc)}
        return {**status, "saved": saved}

    @_api
    def auth_cookie(self, payload: CookieRequest) -> dict[str, Any]:
        client = self.state.new_client()
        try:
            saved = client.login_with_cookies(
                payload.sessdata, payload.bili_jct, payload.dedeuserid
            )
        except BiliError as exc:
            raise ServiceError(400, str(exc)) from exc
        return {"ok": True, "saved": saved}

    @_api
    def auth_logout(self) -> dict[str, Any]:
        self.state.new_client().logout()
        return {"ok": True}

    # -------------------------------------------------------- 字幕上传

    @_api
    def parse_subtitle(self, raw: bytes) -> dict[str, Any]:
        """解析使用者上传的 srt 并暂存，返回一份回执。

        收的是**原始字节**而不是 multipart——理由见 infra/uploads.py 的模块说明
        （简单说：不为此引入 python-multipart 这个新依赖）。

        回执里带上识别到的编码：**出现乱码时它是唯一的线索**。

        这一层是同步的（CPU／IO 都阻塞）。HTTP 路由那边用 run_in_threadpool 包起来，
        因为它是 async 的、直接在事件循环里跑会**阻塞整个服务**：实测一份上限体积
        （5 MB）的 srt 解析约 0.5–0.8 秒——足够让正在轮询的任务进度卡住。
        桌面桥跑在自己的线程里，没有这个问题。
        """
        if not raw:
            raise ServiceError(400, "这个文件是空的。")
        if len(raw) > uploads.MAX_BYTES:
            raise ServiceError(
                400,
                f"文件太大了（{len(raw) / 1024 / 1024:.1f} MB），"
                f"上限 {uploads.MAX_BYTES // 1024 // 1024} MB。是不是选错文件了？",
            )
        try:
            items, encoding = srt_mod.parse_bytes(raw)
        except srt_mod.SrtError as exc:
            raise ServiceError(400, f"读不了这个文件：{exc}") from exc

        token = uploads.new_token()
        uploads.save(self.state.cfg.db_path, token, raw)
        return {
            "token": token,
            "segments": len(items),
            "encoding": encoding,
            "first": srt_mod.format_clock(min(start for start, _ in items)),
            "last": srt_mod.format_clock(max(start for start, _ in items)),
            "chars": sum(len(text) for _, text in items),
            "bytes": len(raw),
        }

    @_api
    def discard_subtitle(self, token: str) -> dict[str, Any]:
        """使用者点「清除」时立刻删掉，不等兜底清理。"""
        return {"removed": uploads.discard(self.state.cfg.db_path, token)}

    # ------------------------------------------------------------ 任务

    @_api
    def create_task(self, payload: TaskRequest) -> dict[str, Any]:
        # 上传件在这里就校验掉。放到任务里再查的话，使用者要等解析、采集都跑完
        # 才发现"文件过期了"，白烧一次调用。
        if payload.subtitle_token and not uploads.exists(
            self.state.cfg.db_path, payload.subtitle_token
        ):
            raise ServiceError(400, "你上传的字幕已过期或被清除，请重新选择文件。")
        task_id = self.state.store.create_task(payload.raw_input)
        threading.Thread(
            target=run_task,
            args=(
                self.state,
                task_id,
                payload.raw_input,
                payload.role,
                payload.subtitle_token,
            ),
            daemon=True,
            name=f"task-{task_id}",
        ).start()
        return {
            "task_id": task_id,
            "role": summarizer.roles.get_role(
                payload.role or self.state.cfg.llm.role
            ).key,
        }

    @_api
    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.state.store.get_task(task_id)
        if task is None:
            raise ServiceError(404, "任务不存在")
        # 发布版长文（不是概括版）。在前端展示"将要发出去的东西"，
        # 而且让确认闸门对着真实内容生效——人对着概括版点确认，等于没确认。
        task["publish_document"] = publish_document_of(task.get("summary_json"))

        # 告诉界面「现在能不能直接再改一版」，以及**不能的话是为什么**。
        # 理由必须查实而不是猜：素材缓存只在内存里，程序重启过就没了；
        # 而一个点了就报错的按钮，比一个写着原因的禁用按钮更让人困惑。
        can_refine, blocked = refine_gate(self.state, task)
        task["can_refine"] = can_refine
        task["refine_blocked"] = blocked
        task["refining"] = self.state.is_refining(task_id)

        # 版本史不在这里返回全文（可能好几版，每版几千字），只给"现在一共有几版、
        # 当前是第几版"，够界面显示头顶那一行；列表由 list_versions 单独取。
        self.state.store.ensure_initial_version(task_id)
        versions = self.state.store.list_versions(task_id)
        task["version_count"] = len(versions)
        task["current_version"] = versions[-1]["version_no"] if versions else None
        # 同一个视频发过、且还没撤回的总结。界面据此把打磨入口关掉并说明原因——
        # 已发出的评论改不了，再打磨只能是为了撤回重发。
        # 只取几个字段：发布记录里存着整段评论正文，原样带出来会让任务详情接口
        # 白白多背几 KB，而界面只需要知道"发过没有、是哪一条、什么时候"。
        live = (
            self.state.store.find_live_publish(task["bvid"]) if task.get("bvid") else None
        )
        task["live_publish"] = (
            {
                "publish_id": live["id"],
                "rpid": live.get("rpid"),
                "created_at": live.get("created_at"),
            }
            if live
            else None
        )
        return task

    @_api
    def list_versions(self, task_id: str) -> dict[str, Any]:
        """版本史（不含全文）。列表要轻，全文点开那一版再取。"""
        if self.state.store.get_task(task_id) is None:
            raise ServiceError(404, "任务不存在")
        self.state.store.ensure_initial_version(task_id)
        versions = self.state.store.list_versions(task_id)
        current = versions[-1]["version_no"] if versions else None
        briefs = []
        previous = None
        for version in versions:
            briefs.append(version_brief(version, current, previous))
            previous = version
        return {
            "current": current,
            "total": sum_usage(versions),
            "versions": briefs,
        }

    @_api
    def get_version(self, task_id: str, version_no: int) -> dict[str, Any]:
        version = self.state.store.get_version(task_id, version_no)
        if version is None:
            raise ServiceError(404, f"这条没有第 {version_no} 版")
        current = self.state.store.latest_version_no(task_id)
        # 详情页也给同一份"实际改了什么"，免得同一件事在两个地方口径不同
        previous = (
            self.state.store.get_version(task_id, version_no - 1)
            if version_no > 1
            else None
        )
        payload = version_brief(version, current, previous)
        payload["summary_json"] = version.get("summary_json") or {}
        payload["preview_md"] = version.get("preview_md") or ""
        payload["comment_text"] = version.get("comment_text") or ""
        payload["publish_document"] = publish_document_of(version.get("summary_json"))
        return payload

    @_api
    def restore_version(self, task_id: str, version_no: int) -> dict[str, Any]:
        """把某一版重新设为当前版（「以这一版为底稿」）。

        **不调用模型、不花一分钱、几毫秒返回**——它只是把那一版的全文抄成新的当前版。
        做成"追加一个版本"而不是"把指针挪回去"：版本史要能如实回答"当时实际用的是
        哪一版"，而挪指针会让中间那几版看起来从没存在过。这和 git revert 是同一种取舍。
        """
        store = self.state.store
        task = store.get_task(task_id)
        if task is None:
            raise ServiceError(404, "任务不存在")

        source = store.get_version(task_id, version_no)
        if source is None:
            raise ServiceError(404, f"这条没有第 {version_no} 版")
        if version_no == store.latest_version_no(task_id):
            raise ServiceError(400, f"第 {version_no} 版就是当前版，不用回退。")

        allowed, blocked = refine_gate(self.state, task)
        if not allowed:
            raise ServiceError(409, blocked)

        store.record_version(
            task_id,
            source="restore",
            requirement=f"以第 {version_no} 版为底稿",
            summary_json=source.get("summary_json") or {},
            preview_md=source.get("preview_md") or "",
            comment_text=source.get("comment_text") or "",
            role=source.get("role") or "",
            # 不记 metrics：它没有消耗。记成 0 也行，但"没有这一项"更准确。
            metrics=None,
            status="ready",
            progress=f"已回到第 {version_no} 版的内容，等待你确认",
            error=None,
        )
        # 回退之后素材笔记的缓存可能已经不在（程序重启过），这时 can_refine 会变成
        # false——界面上会看到"回退成功但不能再改"。这是实话，如实返回即可。
        can_refine, why = refine_gate(self.state, store.get_task(task_id) or task)
        return {
            "ok": True,
            "version_no": store.latest_version_no(task_id),
            "restored_from": version_no,
            "can_refine": can_refine,
            "refine_blocked": why,
        }

    @_api
    def refine_task(self, task_id: str, payload: RefineRequest) -> dict[str, Any]:
        """按使用者的要求再改一版：**不重新采集、不重新预读**。

        这是迭代打磨的入口。素材笔记从缓存取——所以同一份素材改多少版，
        预读都只付一次钱。
        """
        store = self.state.store
        task = store.get_task(task_id)
        if task is None:
            raise ServiceError(404, "任务不存在")

        summary_json = task.get("summary_json")
        if not summary_json:
            raise ServiceError(
                400, "这条任务还没有生成过总结，没有可改的底稿。先跑一次总结。"
            )

        allowed, blocked = refine_gate(self.state, task)
        if not allowed:
            # 三种拒绝（已发布 / 正在改 / 缓存失效）用同一段文案，和界面上的禁用
            # 理由来自同一个函数——界面说什么、接口就说什么，不会各说各话。
            # 缓存失效那条尤其重要：要说清「不是坏了，是重跑一次就行」。
            raise ServiceError(409, blocked)

        current = store.latest_version_no(task_id)
        if payload.base_version is not None and payload.base_version != current:
            # 界面让你在第 N 版的基础上提要求，那就必须真的基于第 N 版改。
            # 不查的话（比如另一个标签页刚改过一版），你按第 2 版提的要求会落到
            # 第 4 版上，而结果是看不出来的——它只会静静地不是你想要的。
            raise ServiceError(
                409,
                f"你看到的是第 {payload.base_version} 版，但当前已经是第 {current} 版"
                "（多半是另一个页面刚改过一版）。刷新一下再提要求，免得你的要求"
                "落在别的底稿上。",
            )

        # 不传 role 就沿用上一版的角色——迭代的语义是「同一个讲法越改越好」，
        # 而不是换个人重讲。想换角色就显式传，那等于重开一版。
        role = payload.role or summary_json.get("role") or None
        if role and role not in summarizer.roles.ROLE_KEYS:
            raise ServiceError(
                400,
                f"未知的总结角色：{role}。可选：" + "、".join(summarizer.roles.ROLE_KEYS),
            )

        # 先占名额再起线程：检查与占用之间如果隔了一行，两个几乎同时到达的请求
        # 会双双通过检查。占位失败说明已经有一版在改。
        if not self.state.begin_refine(task_id):
            raise ServiceError(
                409, "这条视频已经有一版在改了，等它出结果再提新要求。"
            )

        # 上一版给**完整 JSON**：模型这次要输出同构结构，让它看到上一版的每个字段，
        # 比只给 summary_md 更能避免「顺手把别的字段也重写了」。
        # 但必须去掉上一版的 revise_note——理由见 build_previous_view。
        previous = summarizer.build_previous_view(summary_json)
        ctx = self.state.get_prepared(task_id)

        # 同步把状态置成 refining，再起线程：否则前端提交后第一次轮询可能读到的
        # 还是上一轮的 "ready"，会以为"已经改完了"，就此停下不再轮询。
        store.update_task(task_id, status="refining", progress="已收到要求，准备改这一版")
        self.state.emit(task_id)
        try:
            threading.Thread(
                target=run_refine,
                args=(self.state, task_id, ctx, payload.requirement, role, previous),
                daemon=True,
                name=f"refine-{task_id}",
            ).start()
        except Exception as exc:  # 起不来线程就别把任务永远挂在 refining 上
            self.state.end_refine(task_id)
            store.update_task(
                task_id,
                status="ready",
                progress=f"没能开始改：{exc!r}",
                error=f"未预期的错误：{exc!r}",
            )
            raise ServiceError(500, f"没能开始改这一版：{exc}") from exc

        return {
            "task_id": task_id,
            "status": "refining",
            "base_version": current,
            "reused_prepared": True,
        }

    @_api
    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.state.store.list_tasks(limit=max(1, min(limit, 100)))

    # ------------------------------------------------------------ 发布

    @_api
    def publish(self, payload: PublishRequest) -> dict[str, Any]:
        task = self.state.store.get_task(payload.task_id)
        if task is None:
            raise ServiceError(404, "任务不存在")
        if task.get("status") != "ready":
            raise ServiceError(
                400, f"任务当前状态是「{task.get('status')}」，还不能发布"
            )

        client = self.state.new_client()
        publisher = Publisher(client, self.state.store, self.state.cfg.publish)

        # 长文走笔记、短文走普通评论。用户明确要求发完整版而不是概括版，
        # 而完整版普遍超过普通评论 1000 字符的硬上限，
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

    @_api
    def withdraw(self, publish_id: int) -> dict[str, Any]:
        client = self.state.new_client()
        publisher = Publisher(client, self.state.store, self.state.cfg.publish)
        return publisher.withdraw(publish_id).to_dict()

    @_api
    def list_publishes(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.state.store.list_publishes(limit=max(1, min(limit, 100)))

    # ------------------------------------------------------------ 缓存

    @_api
    def notes_stats(self) -> dict[str, Any]:
        """预读缓存的现状。桌面端设置抽屉里显示它，让"省了多少"看得见。"""
        return self.state.notes.stats()

    @_api
    def clear_notes(self) -> dict[str, Any]:
        return {"removed": self.state.notes.clear()}
