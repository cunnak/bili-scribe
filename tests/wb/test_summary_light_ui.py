"""轻量总结的真浏览器验收（mock 后端，真实产品树）。

jsdom 能证明"元素在、点了有反应"，证明不了**它真的看得见**：``display:none``、
零高度、被别的层盖住，jsdom 一律判通过。这个功能最容易出的错恰好是这一类——
来源切换做出来了但没人看得见，或者切过去之后面板是空的。

两条判据：

1. **来源切换看得见、点得动、切得过去**：三个条件同快照取（内容盒高 > 0、
   行在视口盒内、``elementFromPoint`` 命中的就是它），和设置页 N1 同一把尺子。
2. **切过去之后轻量面板真的在**：BV 输入框、解析按钮、选文件按钮、开始按钮
   四样都在且可见；点「提取字幕并总结」不给任何输入时，界面上要出现一句人话。

不测 B站 网络行为——那是 ``tests/wb/test_summary_light.py` 的活。
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "video_workbench" / "web"

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - 没装 playwright 时整份跳过
    sync_playwright = None

pytestmark = pytest.mark.skipif(sync_playwright is None, reason="没装 playwright")


@pytest.fixture(scope="module")
def web_base_url():
    """只读服务真实 ``video_workbench/web``，端口 0，用完关掉。"""
    Handler = type(
        "QuietHandler",
        (http.server.SimpleHTTPRequestHandler,),
        {"log_message": lambda self, *args: None},
    )

    def handler_factory(*args, **kwargs):
        return Handler(*args, **kwargs, directory=str(WEB_DIR))

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/index.html?mock=1"
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            launched = pw.chromium.launch()
        except Exception as exc:  # 浏览器没下载/无权限起进程——明确 skip，不假绿
            pytest.skip(
                "chromium 起不来，界面验收没跑（不是通过，是没跑）。"
                f"原因：{type(exc).__name__}: {str(exc)[:200]}；"
                "修：`.venv/Scripts/python.exe -m playwright install chromium`"
            )
        try:
            yield launched
        finally:
            launched.close()


def open_summary(browser, web_base_url: str) -> tuple:
    """开一页总结页，返回 (page, 控制台错误列表)。"""
    page = browser.new_page(viewport={"width": 1384, "height": 900})
    problems: list[str] = []
    page.on("console", lambda msg: problems.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: problems.append(str(err)))
    page.goto(web_base_url, wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.router")
    page.evaluate("() => window.WB.router.go('summary', {})")
    page.wait_for_selector("#sum-head", timeout=8000)
    return page, problems


def assert_visible(page, selector: str) -> None:
    """三条件同快照：内容盒高 > 0、行在视口盒内、命中测试指向它自己。

    只看 ``is_visible()`` 是不够的：一个被上层浮层盖住的按钮照样 is_visible。
    """
    box = page.evaluate(
        """(sel) => {
            const node = document.querySelector(sel);
            if (!node) return null;
            const rect = node.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return {ok: false, why: 'zero-box'};
            const inside = rect.top >= 0 && rect.bottom <= window.innerHeight + 1;
            const hit = document.elementFromPoint(
                Math.round(rect.left + rect.width / 2),
                Math.round(rect.top + rect.height / 2)
            );
            return {ok: inside && (hit === node || node.contains(hit)), why: inside ? 'hit' : 'offscreen'};
        }""",
        selector,
    )
    assert box is not None, f"找不到 {selector}"
    assert box["ok"], f"{selector} 看不见或点不到：{box['why']}"


def test_the_source_switch_is_visible_and_clickable(browser, web_base_url):
    page, problems = open_summary(browser, web_base_url)
    try:
        # 上一段可能是合集模式（state 是模块级的），先切回单条
        page.evaluate(
            """() => {
                const tabs = document.querySelectorAll('.sum-tab');
                if (tabs.length && !tabs[0].classList.contains('is-active')) tabs[0].click();
            }"""
        )
        page.wait_for_timeout(300)

        assert_visible(page, "#sum-src-local")
        assert_visible(page, "#sum-src-light")
        assert page.eval_on_selector("#sum-src-local", "n => n.classList.contains('is-active')")
        assert page.eval_on_selector("#sum-project", "n => !!n"), "本地模式下该有项目下拉"

        page.click("#sum-src-light")
        page.wait_for_selector("#sum-light-url", timeout=4000)
        assert page.eval_on_selector("#sum-src-light", "n => n.classList.contains('is-active')")
        assert page.eval_on_selector_all("#sum-project", "ns => ns.length") == 0, (
            "切到轻量后不该还挂着本地项目下拉"
        )
        assert problems == [], f"控制台报错：{problems}"
    finally:
        page.close()


def test_the_light_panel_is_usable_and_says_something_on_empty_input(browser, web_base_url):
    page, problems = open_summary(browser, web_base_url)
    try:
        page.evaluate(
            """() => {
                const tabs = document.querySelectorAll('.sum-tab');
                if (tabs.length && !tabs[0].classList.contains('is-active')) tabs[0].click();
            }"""
        )
        page.wait_for_timeout(300)
        page.click("#sum-src-light")
        page.wait_for_selector("#sum-light-url", timeout=4000)

        for selector in ("#sum-light-url", "#sum-light-probe", "#sum-light-file", "#sum-light-go"):
            assert_visible(page, selector)

        # 什么都不填就点：界面必须给一句话，而不是静默
        page.click("#sum-light-go")
        page.wait_for_selector("#sum-light-error", timeout=4000)
        text = page.inner_text("#sum-light-error")
        assert text.strip(), "空输入点下去没有任何提示"
        assert "av" in text.lower() or "字幕" in text, f"提示看不出该干什么：{text}"

        # 输一个 BV 号再解析：mock 会回两条字幕轨
        page.fill("#sum-light-url", "BV1xx411c7mD")
        page.click("#sum-light-probe")
        page.wait_for_selector("#sum-light-subs", timeout=4000)
        options = page.eval_on_selector_all(
            "#sum-light-subs option", "ns => ns.map(n => n.textContent)"
        )
        assert len(options) == 2, f"字幕轨数量不对：{options}"
        assert any("AI" in item for item in options), f"AI 字幕没标出来：{options}"

        assert problems == [], f"控制台报错：{problems}"
    finally:
        page.close()
