"""窗口最小尺寸：**参数真的传下去了**，且判据不钉 1100/720 字面量。

为什么这条必须存在（2026-11-06 评审 D-02 / R72-3）：
`desktop.py` 里 `MIN_SIZE = (1100, 720)` 传给 `webview.create_window(min_size=...)`，
pywebview 6.2.1 的 winforms 后端把它按 DPI 缩放后赋给 WinForms `Form.MinimumSize`
（物理像素、外层尺寸含边框）。但全仓此前**一条测试都没有**守这条链路：
`grep -rn "MIN_SIZE|WINDOW_SIZE|min_size" tests/wb/*.py` 是 0 命中。
于是「重构时把 min_size 漏传」「把 MIN_SIZE 改成 (0,0)」这两种退化都不会有任何红灯。

这条用例只钉**链路**，不钉数值：期望值现读 `desktop.MIN_SIZE`，所以常量改了判据自动
跟着走；它同时要求两个分量都 > 0（那才是「最小尺寸还有意义」），并断言宽度不小于
某个下限之外的东西一概不管 —— 真窗口的实际钳制由人工验收（WM_GETMINMAXINFO +
GetWindowRect，见 docs/decisions-product-defaults.md 的窄窗节）负责，
机器只能证明「参数传到了 pywebview」。
"""

from __future__ import annotations

import pytest

from video_workbench import desktop


def test_create_window_passes_the_module_min_size_untouched(monkeypatch):
    """`create_window` 必须把 `desktop.MIN_SIZE` 原样交给 `webview.create_window`。

    牙齿：把 `min_size=MIN_SIZE` 这一行删掉（或改成 `(0, 0)`）⇒ 本用例必红。
    """
    captured: dict = {}

    class _EventHook:
        """模拟 pywebview 的 `window.events.shown`：产品侧用 `+=` 往上挂回调。"""

        def __init__(self) -> None:
            self.handlers: list = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    class _FakeWindow:
        def __init__(self) -> None:
            self.evaluated: list[str] = []
            self.events = type("E", (), {"shown": _EventHook()})()

        def evaluate_js(self, script: str) -> None:   # pragma: no cover - 不真跑
            self.evaluated.append(script)

    def fake_create_window(title, **kwargs):
        captured["title"] = title
        captured.update(kwargs)
        return _FakeWindow()

    monkeypatch.setattr(desktop.webview, "create_window", fake_create_window)
    # 界面文件必须真存在，否则 `create_window` 在建窗前就抛 FileNotFoundError。
    monkeypatch.setattr(desktop, "_ui_index", lambda: desktop.Path(__file__).resolve())

    class _Service:
        """只为 `attach()` 那一步：它要 `service.events.set_sink` 与 `service.cfg.ui.skin`。"""

        class events:
            @staticmethod
            def set_sink(_sink) -> None:
                return None

        cfg = type("C", (), {"ui": type("U", (), {"skin": "ink"})()})()

    api = desktop.DesktopApi(_Service())
    service = _Service()

    desktop.create_window(service, api)

    assert "min_size" in captured, "create_window 没有把 min_size 传给 pywebview"
    assert tuple(captured["min_size"]) == tuple(desktop.MIN_SIZE), (
        "传下去的 min_size 与 desktop.MIN_SIZE 不一致："
        f"{captured['min_size']!r} vs {desktop.MIN_SIZE!r}")
    assert captured["width"] == desktop.WINDOW_SIZE[0]
    assert captured["height"] == desktop.WINDOW_SIZE[1]


def test_min_size_still_constrains_something():
    """常量本身还得是「一个真正的最小尺寸」：两个分量都 > 0，且不小于 320×240。

    这条与上一条分开，是因为它们守的是两件不同的事：
    上一条守「传下去」，这一条守「传下去的值还有意义」。
    写成 `MIN_SIZE = (0, 0)` 时上一条照样绿（两边都是 (0,0)），只有这一条会红。
    """
    width, height = desktop.MIN_SIZE
    assert width > 0 and height > 0, f"MIN_SIZE 有非正分量：{desktop.MIN_SIZE!r}"
    assert width >= 320 and height >= 240, (
        f"MIN_SIZE 小到不构成约束：{desktop.MIN_SIZE!r}（界面在 1200px 以下会走窄版布局）")
