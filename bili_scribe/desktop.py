"""桌面外壳：一个窗口，背后是同一个 Python 进程。

    python desktop.py                 用 config.yaml 启动
    python desktop.py --config my.yaml
    python desktop.py --debug         开 WebView 的开发者工具

和 `run.py` 的区别是**没有 HTTP 服务**。界面直接调这个进程里的 Python 函数
（`js_api`），进度从后台线程 `evaluate_js` 主动推给页面。于是：

- 不用绑端口 → 不会撞上"端口被占用"，也不会有人在局域网里扫到你的服务；
- 不用轮询 → 原来 1.2 秒一次的轮询（一个 110 秒任务约 70–90 次请求）彻底消失。

这不是"把网页塞进一个窗口"——那样端口和轮询都会原样留着。真正的区别在于
**桥接是进程内调用**，一次 `api.get_task(id)` 就是一次普通函数调用，
不进序列化、不过 TCP。

`run.py` 仍然保留并继续可用：调试接口用 curl 比用开发者工具控制台方便，
而且 69 项后端测试是直接打 HTTP 的。
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, get_args, get_type_hints

import webview
from pydantic import BaseModel, ValidationError

from bili_scribe import __version__
from bili_scribe.app.service import Service, ServiceError
from bili_scribe.config import DEFAULT_HOME, AppConfig

WINDOW_TITLE = f"bili-scribe {__version__}"
# 初始尺寸按"单工作面"排：左边正文、右边版本与打磨，1360 够放下而不显得空。
# 最小尺寸卡在 1100×720：再窄右栏就会被压成一条缝，那还不如让窗口拒绝缩小。
WINDOW_SIZE = (1360, 860)
MIN_SIZE = (1100, 720)
# 窗口背景色，同时也是标题栏的颜色。必须和界面的 `--bg`（`#0D0F10`）一致：
# 窗口出现到页面画完之间有一段空白，底色对不上就会闪一下；标题栏也一样。
WINDOW_BG = "#0D0F10"


def _frozen() -> bool:
    """是不是在打包好的 exe 里跑。

    PyInstaller 启动时会设 `sys.frozen`，并把随包数据解到 `sys._MEIPASS`。
    下面两个路径函数都要按这个分叉：开发态和打包态的"文件在哪"是两回事，
    不分开就会出现在源码里跑得好好的、打出来的 exe 一起来就报"找不到界面文件"。
    """
    return bool(getattr(sys, "frozen", False))


def _ui_index() -> Path:
    """界面文件的位置。

    开发态直接用仓库里的 `static/index.html`（改了刷新就生效）；打包态在
    `_MEIPASS` 下，打包脚本按同一个相对路径把文件放进去。
    """
    if _frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "bili_scribe" / "app" / "static" / "index.html"
    return Path(__file__).resolve().parent / "app" / "static" / "index.html"


def _config_candidates() -> list[Path]:
    """配置文件的查找顺序（前者优先）。

    **为什么不是"只看一个位置"。** 这个程序有两种存在形态，同一个目录约定套不到
    两边：

    - **便携版**：exe 复制到哪儿，配置就跟着在哪儿，整个目录拷走换台机器还能用。
    - **装机版**：exe 放在 `dist/`、`Program Files\\` 或者某个临时目录里跑，
      配置不该跟着 exe 走——它属于"这个用户"，不属于"这个程序文件"。

    只看 exe 旁边，便携版对了、装机版会双击出一个没配置的程序；只看用户目录，
    反过来。所以两个都查，exe 旁边的优先（它更具体，想覆盖的人手边就有）。

    用户目录跟 `db_path` / `key_path` / 笔记缓存是同一个（`~/.bili-scribe/`）：
    凭据、库、配置本来就该待在一个地方，不然"备份一下我的数据"要翻三个目录。
    """
    if _frozen():
        local = Path(sys.executable).resolve().parent / "config.yaml"
    else:
        # 开发态用当前目录，和 run.py 保持一致：`python run.py` 与
        # `python -m bili_scribe.desktop` 读到的必须是同一份。
        local = Path("config.yaml")
    return [local, Path(DEFAULT_HOME) / "config.yaml"]


def _default_config() -> Path:
    """配置文件位置：取第一个存在的；都不存在则取首选位置作为"以后保存到哪里"。

    返回的路径会写进 `cfg.source_path`，设置页保存时就写它。所以"现在没有配置"
    这个状态是安全的——首次在设置页点保存，文件就创建在首选位置。
    """
    candidates = _config_candidates()
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _dark_title_bar(window: webview.Window) -> None:
    """把系统标题栏刷成深色，让窗口顶端和界面连成一片。

    **不能让标题栏跟着系统走。** pywebview 的 winforms 后端会读
    `AppsUseLightTheme` 决定标题栏深浅（winforms.py: `is_dark_theme`），
    所以系统用浅色主题的人装这个程序，会得到一条**白色标题栏压在近黑界面上**：
    截图里那一截白边看起来就像程序没渲染完。这个界面从头到尾只有深色一套，
    标题栏没理由跟随系统。

    `DWMWA_USE_IMMERSIVE_DARK_MODE` 的属性号在 20H1 之前是 19、之后是 20，
    两个都设一遍最省事（设错的那个返回非 0，无副作用）。

    光是转深色还不够：系统给的深色标题栏是**纯黑 `#000000`**，而这个界面的底
    是 `#0D0F10`。两个深色挨在一起，那点差值反而更显眼——一眼能看出来顶上那条
    不是界面的一部分。所以再用 `DWMWA_CAPTION_COLOR`（35）直接指定成界面底色，
    颜色从 `WINDOW_BG` 一个来源取，改主题不会漏掉这处。

    失败就算了：这只是配色，标题栏白一点也不该让程序起不来。
    """
    try:
        import ctypes

        # 优先从窗口对象拿 HWND。按标题去 FindWindow 不可靠——实测本机
        # 就有一个浏览器标签页标题里含 "bili-scribe"，会匹配错窗口。
        hwnd = 0
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is not None:
            hwnd = handle.ToInt32()
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        for attr in (20, 19):
            value = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
        # COLORREF 是 0x00BBGGRR，和 CSS 的 #RRGGBB 反着来。
        r = int(WINDOW_BG[1:3], 16)
        g = int(WINDOW_BG[3:5], 16)
        b = int(WINDOW_BG[5:7], 16)
        color = ctypes.c_int(r | (g << 8) | (b << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        pass


def _apply_shell_settings(settings: Any) -> list[str]:
    """把「像应用、但输入框还能正常用」这件事所需的 WebView2 开关一次设好。

    这里**只碰三个开关**，其余一律不动（尤其 `AreDevToolsEnabled` 保持关闭：
    打开右键菜单和快捷键不应连带放出开发者工具）。

    为什么是这三个：

    - `AreDefaultContextMenusEnabled` —— 右键菜单（复制 / 粘贴 / 全选）。
    - `AreBrowserAcceleratorKeysEnabled` —— **Ctrl+V / Ctrl+C / Ctrl+A 的总开关**。
      这一条是最容易漏的：它和右键菜单在 pywebview 里是**两行**，都写
      `= _state['debug']`（`webview/platforms/edgechromium.py:287-288`）。只把
      右键菜单打开、漏掉这条，现象就是「右键弹不出菜单，Ctrl+V 也毫无反应，
      输入框像是坏的」——而输入框本身完全正常。
    - `IsStatusBarEnabled` —— 保持关闭。留着它就会在左下角浮出
      `javascript:void(0)` 之类的链接地址条，那不是应用该有的东西。

    单独拿出来是为了能**不启动 GUI 就测**：传一个记下赋值的假对象即可，
    不必真的开窗口再看现象。

    返回设过的项名，方便调用方打日志。
    """

    settings.AreDefaultContextMenusEnabled = True
    settings.AreBrowserAcceleratorKeysEnabled = True
    settings.IsStatusBarEnabled = False
    return ["AreDefaultContextMenusEnabled", "AreBrowserAcceleratorKeysEnabled"]


def _enable_context_menu(window: webview.Window) -> None:
    """打开 WebView2 的原生右键菜单与浏览器快捷键（复制 / 粘贴 / 全选）。

    **pywebview 默认把这两样都关掉了。** `.venv/Lib/site-packages/webview/
    platforms/edgechromium.py:287-288`：

        settings.AreBrowserAcceleratorKeysEnabled = _state['debug']
        settings.AreDefaultContextMenusEnabled = _state['debug']

    只有带 `--debug` 启动才有。从"这是个应用"的角度看禁掉是合理的（不是网页，
    不需要"查看源代码"）；但**输入框里粘贴是刚需**：从别处复制一个 BV 号，
    右键想粘、Ctrl+V 想粘，两样都不响应，会以为程序卡了或输入框坏了。

    曾经只设了 `AreDefaultContextMenusEnabled` 一条，用户反馈"还是粘不进去"——
    因为真正的拦路虎是另一条：`AreBrowserAcceleratorKeysEnabled` 是 Ctrl+V 的
    总开关。**两个都要设**，缺一个就还是不能用。

    **`shown` 事件触发时 `CoreWebView2` 还不存在。** 实测那一刻
    `native.webview.CoreWebView2` 是 None，大约 **1.2 秒之后**才拿到实例
    (`on_webview_ready` 回调是异步的)。所以这里轮询等待，而不是设一次就完事。

    轮询放在独立线程里跑，因为 `shown` 回调是同步执行的——在里面 sleep 会把
    窗口初始化流程一起堵住。失败就算了，少了这些不影响主流程。
    """

    def wait_and_enable() -> None:
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                form = getattr(window, "native", None)
            except Exception as exc:
                print(f"[ctx] 拿不到窗口对象：{exc!r}", flush=True)
                return
            if form is None:
                time.sleep(0.15)
                continue

            # **必须把活送回 UI 线程做。** BrowserForm 是 WinForms 控件，任何
            # 属性读取都要在该控件自己的线程上；从后台线程直接摸
            # `form.webview.CoreWebView2` 会**永久阻塞**（不是抛异常，是卡死）。
            # 这个坑实测踩过两次：日志里只有"已挂上 shown 事件"，后面既没有
            # 成功那行、也没有 15 秒超时那行，线程就那么僵在那儿——看起来像
            # "开关设了但没效果"，实际是那行代码根本没跑到。
            #
            # winforms 后端自己也是这么干的（`webview/platforms/winforms.py:398`
            # 起 `self.Invoke(Func[Type](_shutdown))`），照它的做法来。
            outcome: list[Any] = []

            def on_ui_thread() -> None:
                try:
                    webview2 = getattr(form, "webview", None)
                    core = getattr(webview2, "CoreWebView2", None)
                    if core is None:
                        outcome.append(None)  # 还没初始化好，下一轮再来
                        return
                    outcome.append(_apply_shell_settings(core.Settings))
                except Exception as exc:  # noqa: BLE001 —— 记下来，不吞
                    outcome.append(exc)

            try:
                from System import Action  # 只有加载了 CLR 才有

                form.Invoke(Action(on_ui_thread))
            except Exception as exc:  # noqa: BLE001
                print(f"[ctx] Invoke 到 UI 线程失败：{exc!r}", flush=True)
                return

            if not outcome:
                time.sleep(0.15)
                continue
            got = outcome[0]
            if got is None:
                time.sleep(0.15)
                continue
            if isinstance(got, Exception):
                print(f"[ctx] 设开关时出错：{got!r}", flush=True)
                return
            # 打一行日志：打包成 exe 之后没有控制台，验证「到底开没开」
            # 只能靠 exe 旁边那份 bili-scribe.log。少了这行，用户报
            # 「还是粘不进去」时无从判断是没走到这儿、还是走到了但
            # WebView2 没理。
            print(f"[ctx] 已打开：{' / '.join(got)}", flush=True)
            return
        print("[ctx] 等了 15 秒还没拿到 CoreWebView2，这次没打开", flush=True)

    threading.Thread(target=wait_and_enable, daemon=True).start()


def _model_of(annotation: Any) -> type[BaseModel] | None:
    """从类型注解里取出 Pydantic 模型类，`X | None` 这类联合类型也能取到。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for item in get_args(annotation):
        if isinstance(item, type) and issubclass(item, BaseModel):
            return item
    return None


def _coerce_args(func, args: tuple) -> tuple:
    """把 JS 传过来的 dict 按签名里的 Pydantic 模型校验成模型实例。

    **这一步不能省。** service 层的方法全都按模型在取属性（`payload.subtitle_token`、
    `payload.llm.chunk_overlap`……），而 pywebview 从 JS 收到的 JSON 对象就是原生
    dict——dict 没有属性，每个带请求体的桌面调用都会炸成
    `AttributeError: 'dict' object has no attribute '...'`，然后被 `_wrap` 兜成一句
    看不出原因的"未预期的错误"。

    HTTP 那边这件事是 FastAPI 替我们做的（它在调用路由前按注解校验并构造模型）。
    桌面端没有那一层，所以在这里补上同样的一次转换：校验规则、字段默认值、类型
    强转都还是 Pydantic 那一套，两边行为一致。校验失败抛 `ValidationError`，
    由 `_wrap` 翻成 422。
    """
    if not args:
        return args
    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 - 拿不到注解就原样传，别在传输层制造新故障
        return args
    if not hints:
        return args

    try:
        names = list(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        return args

    coerced = list(args)
    for index, value in enumerate(coerced):
        if index >= len(names) or not isinstance(value, dict):
            continue
        model = _model_of(hints.get(names[index]))
        if model is not None:
            coerced[index] = model.model_validate(value)
    return tuple(coerced)


def _validation_message(exc: ValidationError) -> str:
    """把 Pydantic 的报错压成一句人话。

    原始报错是多行英文结构，直接扔给界面等于没提示——使用者需要知道的是
    "哪个字段、错在哪"。字段路径用点连起来，正好和设置页上的层级对得上。
    """
    errors = exc.errors()
    parts = []
    for err in errors[:3]:
        loc = ".".join(str(item) for item in err.get("loc", ())) or "请求体"
        parts.append(f"{loc}: {err.get('msg', '不合法')}")
    suffix = "" if len(errors) <= 3 else f"（还有 {len(errors) - 3} 处）"
    return "请求内容不合法 —— " + "；".join(parts) + suffix


class DesktopApi:
    """暴露给 JavaScript 的接口面。

    每个方法都是"同步函数 + 返回可 JSON 序列化的值"。pywebview 会把它包成
    Promise，所以 JS 侧是 `await api.get_task(id)`。

    **这里不做业务**，只做两件传输层的事：
    1. 把 `ServiceError` 翻成 `{"ok": false, "error": ..., "status": ...}`
    2. 把结果包成 `{"ok": true, "data": ...}`

    为什么不直接抛异常让 pywebview 转成 rejected Promise：那样 JS 侧拿到的是一个
    被序列化成字符串的异常对象，`catch (e) { e.status }` 取不到状态码，
    于是"任务不存在"和"服务崩了"在前端看起来一模一样。显式约定一个返回结构，
    前端只需要判 `ok`。这个约定集中在前端的 `call()` 一处，改动成本很低。
    """

    def __init__(self, service: Service):
        # **这两个属性必须以下划线开头。** pywebview 的 `util.py:get_functions()` 会
        # 递归遍历 js_api 对象的每一个公开属性，把方法收集成 `pywebview.api.<点路径>`；
        # `self.window` 是 .NET 的 Window 对象，它有 `native`、`AccessibilityObject`、
        # `Bounds` 这些自引用链，递归会一路撞到 RecursionError（控制台里那串
        # `Error while processing window.native.AccessibilityObject.Bounds.Empty.Empty...`
        # 就是它）。更致命的后果是：递归不结束 → `generate_js_object()` 跑不到
        # `finish_script` → `window.pywebview.api` 永远不存在 → 前端 `boot()` 在
        # `mark_ready()` 上 await 到天荒地老，整个界面停在"桥接没有建立起来"。
        # 下划线开头的属性在 `get_functions()` 里第一行就被 `continue` 跳过。
        self._service = service
        self._window: webview.Window | None = None
        # 推送出去的进度事件可能很密（每步一次），但页面还没加载完时不能推——
        # `evaluate_js` 在文档就绪前调用会抛 JavascriptException。
        # 用一个标志记住"页面可以接收推送了"，比到处 try/except 更清楚。
        self._ready = threading.Event()
        # 页面就绪之前发生的变更要补推，否则用户会看到"已经开始跑了"但进度停在第一步。
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------- 生命周期

    def attach(self, window: webview.Window) -> None:
        self._window = window
        # 标题栏配色与右键菜单都必须在窗口出现之后改：HWND 与 CoreWebView2
        # 那时才存在（`shown` 事件里两者都就绪）。
        window.events.shown += _dark_title_bar
        window.events.shown += _enable_context_menu
        print("[ctx] 已挂上 shown 事件（标题栏配色 + 右键菜单）", flush=True)

    def mark_ready(self) -> None:
        """由前端在 DOM 就绪后显式调用一次。

        **不能靠 `pywebviewready` 事件自己判断**：那个事件只说明桥接挂上了，
        不代表 app.js 已经跑起来、`window.__onProgress` 已经存在。前端主动
        报告"我准备好了"是唯一可靠的信号。
        """
        self._ready.set()
        # 补推：就绪之前积压的变更
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
        for task_id in pending:
            self._push(task_id)

    def on_task_change(self, task_id: str) -> None:
        """Service 的状态机在每次变更后调这个。可能是任何一个后台线程。"""
        if not self._ready.is_set():
            with self._pending_lock:
                self._pending.add(task_id)
            return
        self._push(task_id)

    def _push(self, task_id: str) -> None:
        """把这条任务的最新状态推给页面。

        **推的是完整状态，但只有一份**：页面上不只进度条要用它（标题、版本数、
        能不能打磨都在里面），少推一个字段就要多一次往返，而这一次推送本来就是
        免费的。原来轮询模式下每次也是拿完整 JSON，这里只是从"页面来问"变成
        "服务端来说"，数据量一样。
        """
        if self._window is None:
            return
        try:
            payload = self._service.get_task(task_id)
        except ServiceError:
            # 任务在推送途中被删了之类。静默跳过：下一次变更还会推。
            return
        except Exception:  # noqa: BLE001 - 推送失败绝不能反过来把任务搞挂
            traceback.print_exc()
            return
        try:
            self._window.evaluate_js(
                f"window.__onTaskUpdate({json.dumps(payload, ensure_ascii=False)})"
            )
        except Exception:  # noqa: BLE001
            # 窗口已经关了，或者页面正在跳转。这两件事都不需要处理。
            pass

    # --------------------------------------------------------------- 业务桥
    #
    # 每个方法都只有两行：调 service、交给 _wrap。所以下面不再逐个写注释，
    # 业务语义全在 service.py 的方法上。

    def _wrap(self, func, *args, **kwargs) -> dict[str, Any]:
        try:
            return {"ok": True, "data": func(*_coerce_args(func, args))}
        except ServiceError as exc:
            return {"ok": False, "error": exc.detail, "status": exc.status}
        except ValidationError as exc:
            # 形状不对的请求体。这在 HTTP 下是 FastAPI 回的 422；桌面端没有那一层，
            # 不在这里翻的话前端只会看到一条 "未预期的错误：ValidationError"，
            # 而真正该告诉使用者的是「哪个字段写错了」。
            return {"ok": False, "error": _validation_message(exc), "status": 422}
        except Exception as exc:  # noqa: BLE001
            # 未预期异常：前端拿到的仍然是结构化结果，而栈打到控制台。
            # 两边都不丢线索——这是 ERROR_DETAIL 那一课的直接结论。
            traceback.print_exc()
            return {
                "ok": False,
                "error": f"未预期的错误：{type(exc).__name__}: {exc}",
                "status": 500,
            }

    # 元信息
    def get_config(self):
        return self._wrap(self._service.config)

    def check_llm(self):
        return self._wrap(self._service.llm_check)

    def get_roles(self):
        return self._wrap(self._service.roles)

    def set_role(self, payload):
        """顶栏的角色选择器走这里。

        桌面版没有 HTTP，前端 `call("set_role")` 直接落到这个方法上——少写一个，
        网页版能切角色、桌面版点了没反应，而且两边共用同一个 index.html，
        光看界面根本看不出差别。
        """
        return self._wrap(self._service.set_role, payload)

    # 设置
    def get_settings(self):
        return self._wrap(self._service.get_settings)

    def save_settings(self, payload):
        return self._wrap(self._service.put_settings, payload)

    def test_llm(self, payload=None):
        return self._wrap(self._service.test_llm, payload)

    # 登录
    def auth_status(self):
        return self._wrap(self._service.auth_status)

    def auth_qrcode(self):
        return self._wrap(self._service.auth_qrcode)

    def auth_poll(self, key):
        return self._wrap(self._service.auth_poll, key)

    def auth_cookie(self, payload):
        return self._wrap(self._service.auth_cookie, payload)

    def auth_logout(self):
        return self._wrap(self._service.auth_logout)

    # 字幕上传
    #
    # **这条路走文件对话框，不走 <input type=file>。** WebView 里的 file input
    # 拿到的 File 对象没法把字节交给 Python（pywebview 没有暴露读文件的能力），
    # 而把 5 MB 内容转成 base64 塞进 evaluate_js 会撑爆参数长度并让界面卡住。
    # 让 Python 自己弹系统对话框直接拿到路径，读字节这件事就完全留在这一侧。
    def pick_subtitle(self):
        """弹系统文件对话框，读进来解析暂存。返回和 HTTP 版一样的回执。

        线程上这里有个看起来该出事的地方：`webview.util` 对每次 js_api 调用都
        新起一个 Thread（`func = window._functions.get(...)` 之后 `Thread(...).start()`），
        而 winforms 后端 `create_file_dialog` 直接 `dialog.ShowDialog(...)`，
        没有 `Invoke` 回 UI 线程。实测结果是**能用**：对话框真的弹出来了，
        而且模态、能选中文件、中文路径原样返回。所以不额外套一层线程；
        但要记住这是「实测如此」，不是文档保证——哪天真出问题，症状会变成
        「点了字幕按钮没反应」，那时要把调用 `Invoke` 到窗口线程上。
        """

        def work() -> dict[str, Any]:
            paths = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=("字幕文件 (*.srt;*.txt)", "所有文件 (*.*)"),
            )
            if not paths:
                # 用户取消了。这不是错误，前端据此安静地什么都不做。
                return {"cancelled": True}
            path = Path(paths[0])
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ServiceError(400, f"读不了这个文件：{exc}") from exc
            payload = self._service.parse_subtitle(raw)
            payload["cancelled"] = False
            payload["name"] = path.name
            return payload

        return self._wrap(work)

    def discard_subtitle(self, token):
        return self._wrap(self._service.discard_subtitle, token)

    # 任务
    def create_task(self, payload):
        return self._wrap(self._service.create_task, payload)

    def get_task(self, task_id):
        return self._wrap(self._service.get_task, task_id)

    def list_versions(self, task_id):
        return self._wrap(self._service.list_versions, task_id)

    def get_version(self, task_id, version_no):
        return self._wrap(self._service.get_version, task_id, version_no)

    def restore_version(self, task_id, version_no):
        return self._wrap(self._service.restore_version, task_id, version_no)

    def refine_task(self, task_id, payload):
        return self._wrap(self._service.refine_task, task_id, payload)

    def list_tasks(self, limit=20):
        return self._wrap(self._service.list_tasks, limit)

    def list_history(self, limit=100):
        return self._wrap(self._service.list_history, limit)

    # 发布
    def publish(self, payload):
        return self._wrap(self._service.publish, payload)

    def withdraw(self, publish_id):
        return self._wrap(self._service.withdraw, publish_id)

    def list_publishes(self, limit=20):
        return self._wrap(self._service.list_publishes, limit)

    # 预读缓存
    def notes_stats(self):
        return self._wrap(self._service.notes_stats)

    def clear_notes(self):
        return self._wrap(self._service.clear_notes)

    # 杂项
    def open_data_dir(self):
        """在资源管理器里打开数据目录——那个目录里有数据库和日志，
        用户迟早会想去看一眼，而不是自己去拼 `%USERPROFILE%\\.bili-scribe`。"""

        def work() -> dict[str, Any]:
            path = Path(self._service.state.cfg.db_path).parent
            path.mkdir(parents=True, exist_ok=True)
            import os

            os.startfile(str(path))  # noqa: S606 - 打开自己的数据目录，路径不由外部输入
            return {"path": str(path)}

        return self._wrap(work)


def _ensure_streams() -> None:
    """打包态强制把 stdout/stderr 接到日志文件。

    **窗口程序（`--windowed`）里没有控制台。** 这时 Python 会把 `sys.stdout`
    和 `sys.stderr` 设成 `None`，于是任何一处 `print()` 都会当场抛
    `AttributeError: 'NoneType' object has no attribute 'write'`。而这个项目
    print 得不算少（store / notes / bili 三层都把日志接到 print 上），所以
    不补这一步，打包出来第一件事就是崩，且崩在看起来毫不相干的地方。

    **只判断 `is None` 是不够的（踩过）。** PyInstaller 的 `--windowed` 在这个
    Python 上给的不是 None，而是一个"收下但不显示"的流；于是
    `if sys.stdout is not None: return` 直接放行，日志文件从来没有被创建过——
    表面上没崩，实际上"双击启动出了问题想查现场"的路断掉了，而恰恰是打包态
    最需要那份日志。所以判据改成**打包态就接管**，不看 stdio 是不是 None。

    开发态（跑源码）不接管，保留终端里的实时输出。
    """
    if not _frozen():
        # 跑源码：终端可见，保持原样。只在真的没有流时兜一下。
        if sys.stdout is not None and sys.stderr is not None:
            return
    try:
        base = Path(sys.executable).resolve().parent if _frozen() else Path.cwd()
        log = open(base / "bili-scribe.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        # 目录不可写（比如装在 Program Files 又没有权限）。退到系统临时目录，
        # 因为这里的唯一目的是"让 print 别崩"，不是保证日志一定在预期位置。
        import tempfile

        log = open(
            Path(tempfile.gettempdir()) / "bili-scribe.log",
            "a",
            encoding="utf-8",
            buffering=1,
        )
    if _frozen() or sys.stdout is None:
        sys.stdout = log
    if _frozen() or sys.stderr is None:
        sys.stderr = log
    print(f"\n=== bili-scribe 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)


def main() -> int:
    # 必须最先做。窗口程序里 stdout 是 None，之后任何一次 print 都会崩。
    _ensure_streams()

    parser = argparse.ArgumentParser(description="bili-scribe 桌面版")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认见 _default_config；不存在则用内置默认值）",
    )
    parser.add_argument("--debug", action="store_true", help="打开 WebView 开发者工具")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else _default_config()
    try:
        cfg = AppConfig.load(config_path if config_path.exists() else None)
    except (ValueError, FileNotFoundError) as exc:
        # 桌面模式下没有控制台可看（双击启动时），所以配置错必须弹窗说清楚。
        # 直接把窗口开起来显示错误是可行的，但那会让人以为"程序能开就行"；
        # 配置错了整个程序都不能用，得当场说。
        _fatal(f"配置装载失败：\n{exc}")
        return 2

    # 设置页面靠它知道回写到哪个文件。必须用具名路径，避免工作目录变化后写到别处。
    cfg.source_path = str(config_path.resolve())
    if not config_path.exists():
        # 文件不存在不阻塞启动（内置默认值可用），但要在日志里留一句，
        # 因为"设置保存了但下次启动又空"通常就是这个原因。
        print(f"[i] 未找到 {config_path}，使用内置默认值；保存设置时会创建它。")

    index = _ui_index()
    if not index.exists():
        _fatal(f"找不到界面文件：\n{index}\n\n项目文件不完整，请重新解压。")
        return 2

    service = Service(cfg)
    api = DesktopApi(service)
    # 把状态机变更接到推送出口上。这一行就是"轮询消失"的开关：
    # HTTP 模式下 on_task_change 是 None，所以 main.py 那边完全不受影响。
    service.state.on_task_change = api.on_task_change

    window = webview.create_window(
        WINDOW_TITLE,
        url=index.as_uri(),
        js_api=api,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        # 这窗口是应用本体，不是浏览器标签页。关掉就等于退出程序——
        # 默认的 confirm_close=True 会弹一次"确定关闭？"，对单窗口应用是纯噪音。
        confirm_close=False,
        # 背景色和界面底色一致。不设的话窗口出现的一瞬间是白的，
        # 深色界面会闪一下白——那种闪烁看起来像 bug。
        background_color=WINDOW_BG,
        text_select=True,
    )
    api.attach(window)

    print("=" * 62)
    print(f"  bili-scribe {__version__} —— 桌面版")
    print("=" * 62)
    print(f"  数据文件   {cfg.db_path}")
    print(f"  大模型     {cfg.llm.model} @ {cfg.llm.base_url}")
    if not cfg.llm.api_key:
        print("  [!] 未配置 llm.api_key，总结步骤会失败。请在设置里填写。")
    if not cfg.publish.require_confirm:
        print("  [!] require_confirm = false：总结生成后会直接发到评论区，没有确认环节。")
    print("  关闭这个窗口即退出程序。")
    print("=" * 62)

    # http_server=False 是这里最关键的一个参数：它关掉 pywebview 内部的
    # 静态文件服务。界面用 file:// 直接加载，桥接走进程内调用，
    # 整个过程一个端口都不占。
    webview.start(debug=args.debug, http_server=False)
    return 0


def _fatal(message: str) -> None:
    """报错并退出。双击启动时没有控制台，所以尽量弹窗。

    弹窗本身需要 WebView，而配置错的时候 WebView 未必能起来——所以先在控制台
    打一份，再尽力弹一个系统的消息框，两条路都试。
    """
    print(f"[x] {message}", file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "bili-scribe 启动失败", 0x10)
    except Exception:  # noqa: BLE001 - 弹不出来就只能靠上面那行 stderr
        pass


if __name__ == "__main__":
    sys.exit(main())
