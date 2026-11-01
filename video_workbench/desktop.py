"""VideoWorkbench 桌面入口。

**为什么不起 HTTP 服务。** `bili_scribe` 的 HTTP 模式是为了「浏览器里也能用」，
但它意味着一个监听端口、一份 CORS 策略、一套 cookie/session 语义，以及"别人也能
访问到你的库"这个可能性。桌面版不需要其中任何一样：pywebview 的 `js_api` 是进程内
调用，页面用 `file://` 直接加载（`webview.start(http_server=False)`），从头到尾
一个端口都不占，也就没有被扫描、被跨站、被代理的风险面。

**桥接只有一个入口。** 页面侧永远只调 `pywebview.api.rpc(method, params)`，
方法白名单在 `app/service.py` 的 `_register_rpc` 里显式列出。这里不额外暴露任何
"顺手"接口（除了 `ping` 和生命周期用的两个），因为 pywebview 会把 js_api 对象的
每个公开方法都送到页面里——少一个公开方法就少一处被页面（或注入进来的脚本）
摸到的东西。

**公开方法一律下划线前缀以外的东西要克制。** `attach` / `mark_ready` / `rpc` /
`ping` 是全部。`self._service` 这类以下划线开头的属性，pywebview 的
`util.get_functions()` 会直接跳过（见 `bili_scribe/desktop.py` 里那段注释）——
这一点很关键：`self._window` 是 .NET 的 Window 对象，公开它会让递归遍历撞上
`native.AccessibilityObject.Bounds.Empty.Empty...` 自引用链，最终 `RecursionError`，
表现为前端永远等不到 `window.pywebview.api`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import webview

# 标题栏配色、右键菜单/快捷键开关：这三件事在 bili-scribe 里是三轮实测换来的
# （详见 bili_scribe/desktop.py 的注释）。复用它而不是抄一份，因为一旦 pywebview
# 升级、`on_webview_ready` 的签名或 sender 语义变了，要改的只有一处。
from bili_scribe.desktop import (
    _dark_title_bar,
    _enable_context_menu,
    _patch_webview_settings_hook,
    skin_window_bg,
)

from video_workbench import APP_NAME, __version__
from video_workbench.app.service import WorkbenchService

WINDOW_TITLE = APP_NAME
WINDOW_SIZE = (1360, 860)
MIN_SIZE = (1100, 720)


# ============================================================ 路径


def _frozen() -> bool:
    """是不是在打包好的 exe 里跑（PyInstaller 会设 `sys.frozen`）。"""
    return bool(getattr(sys, "frozen", False))


def _ui_index() -> Path:
    """界面入口位置。开发态用仓库里的 `web/index.html`，打包态在 `_MEIPASS` 下。"""
    if _frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "video_workbench" / "web" / "index.html"
    return Path(__file__).resolve().parent / "web" / "index.html"


# ============================================================ 桥


class DesktopApi:
    """暴露给页面的全部接口面（4 个公开方法）。"""

    def __init__(self, service: WorkbenchService) -> None:
        self._service = service
        self._window: webview.Window | None = None
        self._ready = threading.Event()
        # 页面就绪之前的事件不能丢：`evaluate_js` 在文档就绪前调用会抛
        # JavascriptException，而"服务已启动、界面还在解析 HTML"是必然出现的一瞬。
        # 用有界队列兜住这一瞬，就绪时补发。
        self._pending: list[tuple[str, str]] = []
        self._pending_lock = threading.Lock()

    # ---------------------------------------------------------- 生命周期

    def attach(self, window: webview.Window) -> None:
        self._window = window
        # 标题栏与右键菜单都要等 HWND / CoreWebView2 就绪，`shown` 是那个时刻。
        window.events.shown += _dark_title_bar
        window.events.shown += _enable_context_menu
        # 必须在 `webview.start()` 之前打补丁：窗口一建，`on_webview_ready` 就在跑了。
        _patch_webview_settings_hook()
        # 事件出口。EventBus 的 sink 签名是 (name, payload_json)。
        self._service.events.set_sink(self._on_event)

    def mark_ready(self) -> dict[str, Any]:
        """由前端在 DOM 就绪后主动调一次。

        **不依赖 `pywebviewready` 事件**：它只说明桥接挂上了，不代表 app.js
        已经跑完、`window.__wbEvent` 已经存在。前端自己说"我好了"是唯一可靠信号。
        """
        self._ready.set()
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
        for name, payload in pending:
            self._eval(name, payload)
        return {"ready": True, "replayed": len(pending)}

    def _on_event(self, name: str, payload_json: str) -> None:
        """EventBus 的 sink。可能在任意后台线程被调用。"""
        if not self._ready.is_set():
            with self._pending_lock:
                if len(self._pending) < 512:
                    self._pending.append((name, payload_json))
            return
        self._eval(name, payload_json)

    def _eval(self, name: str, payload_json: str) -> None:
        """把一条事件送进页面。永不抛：事件是旁路，不该拖垮后台任务。"""
        window = self._window
        if window is None:
            return
        try:
            # payload 已经是 JSON 文本，这里再用 json.dumps 包一层字符串，
            # 让页面拿到字符串后自己 JSON.parse —— 比把它塞进 JS 字面量安全，
            # 也不必担心 payload 里的引号/换行破坏表达式。
            window.evaluate_js(
                f"window.__wbEvent && window.__wbEvent("
                f"{json.dumps(name)}, {json.dumps(payload_json)})"
            )
        except Exception:  # noqa: BLE001 - 窗口已关 / 文档未就绪
            pass

    # ---------------------------------------------------------- 调用面

    def rpc(self, method: str, params: Any = None, request_id: str | None = None):
        """**唯一业务入口。** 参数校验、白名单、错误映射都在 service/router 里。"""
        return self._service.dispatch(method, params, request_id)

    def ping(self) -> dict[str, Any]:
        """桥接探活。页面 `boot()` 用它确认 js_api 真的挂上了。"""
        return {"pong": time.time(), "version": __version__}


# ============================================================ 系统对话框


def _make_picker() -> Callable[[str], str | None]:
    """注入给 service 的目录/文件选择器。

    为什么放在桌面层：`create_file_dialog` 是窗口的方法，service 不该知道窗口存在。
    调试模式下 service.picker 是 None，`fs.pick` 会明确报 UNSUPPORTED，而不是
    抛一个 AttributeError 让前端猜。
    """

    def pick(kind: str = "dir") -> str | None:
        windows = getattr(webview, "windows", None) or []
        if not windows:
            return None
        window = windows[0]
        if str(kind) == "file":
            dialog = getattr(webview, "OPEN_DIALOG", None) or getattr(webview, "FileDialog", None)
            result = window.create_file_dialog(dialog, allow_multiple=False)
        else:
            dialog = getattr(webview, "FOLDER_DIALOG", None) or getattr(webview, "FileDialog", None)
            result = window.create_file_dialog(dialog)
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

    return pick


# ============================================================ 启动


def _ensure_streams() -> None:
    """打包态把 stdout/stderr 接到日志文件。

    窗口程序（`--windowed`）没有控制台，PyInstaller 给的可能是 `None`，也可能是
    一个"收下但不显示"的流。后者更坏：判 `is None` 会放行，日志文件永远不生成，
    而恰恰是打包态最需要它。所以判据是**打包态就接管**。
    """
    if not _frozen() and sys.stdout is not None and sys.stderr is not None:
        return
    try:
        base = Path(sys.executable).resolve().parent if _frozen() else Path.cwd()
        log = open(base / "video-workbench.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        import tempfile

        log = open(Path(tempfile.gettempdir()) / "video-workbench.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or _frozen():
        sys.stdout = log
    if sys.stderr is None or _frozen():
        sys.stderr = log
    print(f"\n=== {APP_NAME} 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)


def _fatal(message: str) -> None:
    """报错并尽量弹窗——双击启动时没有控制台。"""
    print(f"[x] {message}", file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, f"{APP_NAME} 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


def _check(config_path: str | None = None) -> int:
    """`--check`：不开窗口，只验证"服务能不能装配起来、命令表有几条"。

    这条路径是给 CI 和"打不开时先自查"用的，所以它连窗口都不建，任何 WebView
    环境问题都不该让它失败。

    ``config_path`` 必须一路传进来：验收要能用临时配置跑（不碰用户真实库）。
    第一版这里写死 ``WorkbenchService()``，`--check --config X` 会静默忽略 X 去开
    用户的库——把"隔离验证"变成"动生产数据"。
    """
    try:
        service = WorkbenchService(config_path=config_path)
        info = service.app_info()
        # 打包后最想知道的两件事：窗口能不能起来（info/rpc）、干净机器上第一屏
        # 会看到什么（onboard 五件事）。`onboard` 是纯只读的，放这里不下载不联网。
        try:
            onboard = service.app_onboard()
        except Exception as exc:  # noqa: BLE001 - 体检挂了不该让自检整体失败
            onboard = {"error": str(exc)}
        payload = {
            "ok": True,
            "info": info,
            "rpc": service.router.names(),
            "jobs": [j.get("id") for j in service.jobs.list(limit=5)],
            "onboard": {
                "total": onboard.get("total"),
                "ok_count": onboard.get("ok_count"),
                "ready": onboard.get("ready"),
                "steps": [s.get("id") for s in (onboard.get("steps") or [])],
            },
        }
        service.close()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc), "trace": traceback.format_exc()}, ensure_ascii=False))
        return 1
    # 一行 JSON：`--check` 是给脚本和人 grep 用的，多行反而难消费。
    # README / 用户手册写的都是「打印一行 JSON」，这里跟文档对齐。
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def create_window(service: WorkbenchService, api: DesktopApi) -> webview.Window:
    """建窗并挂桥。单独抽出来是为了让测试能在没有 `webview.start()` 的情况下检查参数。"""
    index = _ui_index()
    if not index.exists():
        raise FileNotFoundError(f"找不到界面文件：{index}")
    # 皮肤走 **片段** 而不是查询串：`file://...index.html?skin=ark` 里的 `?skin=ark`
    # 会被 WebView2 当成文件名的一部分，窗口里直接 ERR_FILE_NOT_FOUND。
    # （bili-scribe 上踩过这个坑，probe 只数颜色数，把报错页当成渲染成功放过去了。）
    from urllib.parse import quote

    url = index.as_uri() + "#skin=" + quote(service.cfg.ui.skin, safe="")
    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        js_api=api,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        # 单窗口应用：关窗即退出，不要"确定关闭？"那一次多余确认。
        confirm_close=False,
        # 和界面当前皮肤底色一致，否则窗口出现一瞬间会闪一下白/黑。
        background_color=skin_window_bg(service.cfg.ui.skin),
        text_select=True,
    )
    api.attach(window)
    return window


def _run_asr_worker(argv: list[str]) -> int:
    """`--asr-worker <spec>`：打包态下 ASR 子进程的入口。

    不开窗、不建服务、不读配置——worker 只认 spec 文件。走这里而不是
    另做一个 exe，是为了只打一个产物、只维护一套依赖。
    """
    if not argv:
        print("--asr-worker 需要 spec 文件路径", file=sys.stderr)
        return 2
    from video_workbench.workers import asr_worker

    return asr_worker.main(argv)


def _run_ytdlp(argv: list[str]) -> int:
    """`--ytdlp <args…>`：打包态下 yt-dlp 子进程的入口。

    打包后没有独立的 python.exe：`sys.executable -m yt_dlp` 会把主程序自己
    再拉起来一遍（窗口一闪、yt-dlp 压根没跑），所以下载子进程走这里。
    不开窗、不建服务、不读配置——跟 `--asr-worker` 一个路子。
    """
    try:
        from yt_dlp import main as ytdlp_main
    except Exception as exc:  # noqa: BLE001 - 缺依赖要当场说清楚
        print(f"yt-dlp 不可用：{exc}", file=sys.stderr or sys.stdout)
        return 2
    return int(ytdlp_main(argv) or 0)

# Windows 上「没有控制台」的父进程去起「控制台子系统」的子进程（ffmpeg.exe 就是），
# 系统会给子进程新开一个黑窗口。下载时那两个一闪而过的终端就是这么来的：
# yt-dlp 合并音视频要调 ffmpeg。CREATE_NO_WINDOW 让子进程照常跑、不建窗口。
_CREATE_NO_WINDOW = 0x08000000


def _hide_console_children() -> None:
    """打包态统一给子进程加 CREATE_NO_WINDOW（源码态不动，方便调试看输出）。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    import subprocess

    if getattr(subprocess.Popen, "_wb_no_window", False):
        return
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        flags = int(kwargs.get("creationflags") or 0)
        kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched  # type: ignore[method-assign]
    subprocess.Popen._wb_no_window = True  # type: ignore[attr-defined]

def _bind_stdio() -> None:
    """把 stdout/stderr 接回真正的句柄 1/2，不接日志文件。

    子进程入口（`--ytdlp` / `--asr-worker`）的输出是给父进程读的管道。窗口程序在
    PyInstaller 下 `sys.stdout` 可能是 None；若走 `_ensure_streams()` 会把输出重定向进
    `video-workbench.log`，父进程的管道就永远收不到进度行（表现为「下载结束但没找到产物」）。
    """
    import io

    for name, fd in (("stdout", 1), ("stderr", 2)):
        try:
            stream = io.TextIOWrapper(
                io.FileIO(fd, "w", closefd=False), encoding="utf-8", errors="replace", line_buffering=True
            )
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, stream)

def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # 一进来就收拾子进程的窗口：GUI 主进程与 --ytdlp / --asr-worker 两个入口都要
    _hide_console_children()
    # 必须排在 argparse 与 _ensure_streams 之前：子进程要的是管道，不是日志文件
    if raw and raw[0] == "--asr-worker":
        _bind_stdio()
        return _run_asr_worker(raw[1:])
    if raw and raw[0] == "--ytdlp":
        _bind_stdio()
        return _run_ytdlp(raw[1:])

    _ensure_streams()

    parser = argparse.ArgumentParser(description=f"{APP_NAME} 桌面版")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 ~/.bili-scribe/config.yaml）")
    parser.add_argument("--debug", action="store_true", help="打开 WebView 开发者工具")
    parser.add_argument("--check", action="store_true", help="只做装配自检，不开窗口")
    args = parser.parse_args(argv)

    if args.check:
        return _check(args.config)

    try:
        service = WorkbenchService(config_path=args.config)
    except Exception as exc:  # noqa: BLE001 - 配置错必须当场说清楚
        _fatal(f"配置装载失败：\n{exc}")
        return 2

    api = DesktopApi(service)
    try:
        window = create_window(service, api)
    except FileNotFoundError as exc:
        service.close()
        _fatal(str(exc))
        return 2

    # 目录选择器需要窗口句柄，所以只能建窗之后注入。
    service.picker = _make_picker()

    info = service.app_info()
    print("=" * 62)
    print(f"  {APP_NAME} {__version__} —— 桌面版")
    print("=" * 62)
    print(f"  配置文件   {info['config_path']}")
    print(f"  数据库     {info['db_path']}")
    print(f"  素材根目录 {info['media_root']}")
    missing = [k for k, ok in info["deps"].items() if not ok]
    if missing:
        print(f"  [!] 缺少依赖：{', '.join(missing)}（相关步骤会在用到时失败）")
    print("  关闭这个窗口即退出程序。")
    print("=" * 62)

    try:
        # http_server=False：不起 pywebview 自带的静态服务，界面走 file://，
        # 这是"桌面版不占端口"的落点。
        webview.start(debug=args.debug, http_server=False)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
