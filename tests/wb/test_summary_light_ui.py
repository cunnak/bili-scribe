"""轻量总结的真浏览器验收（mock 后端，真实产品树）。

jsdom 能证明"元素在、点了有反应"，证明不了**它真的看得见**：``display:none``、
零高度、被别的层盖住，jsdom 一律判通过。这个功能的判据分三层：

1. **两条路由真的独立**：#/summary 没有来源切换、有本地项目下拉；
   #/light 没有单条/合集标签和本地项目下拉，有自己的轻量工具栏。
2. **轻量工具栏真的可用**：AV/BV、角色、模型、解析、选文件、开始都在且可见；
   空输入点下去要出现一句人话，解析后能看见两条字幕轨。
3. **宽屏/窗口下限都不溢出**：1100×720、1525×900、2306×1225、3840×1600
   四档视口里，AV/BV 输入框始终落在自己的 .sum-fld 内，长标题独占一行，
   不产生横向滚动。

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


def open_page(browser, web_base_url: str, route: str = "summary", width: int = 1384) -> tuple:
    """开一页指定路由，返回 (page, 控制台错误列表)。"""
    page = browser.new_page(viewport={"width": width, "height": 900})
    problems: list[str] = []
    page.on("console", lambda msg: problems.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda err: problems.append(str(err)))
    page.goto(web_base_url, wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.router")
    page.evaluate("(route) => window.WB.router.go(route, {})", route)
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


def test_summary_and_light_are_independent_routes(browser, web_base_url):
    summary_page, summary_problems = open_page(browser, web_base_url, "summary")
    try:
        assert_visible(summary_page, "#sum-project")
        assert summary_page.eval_on_selector_all(".sum-src", "ns => ns.length") == 0, (
            "本地项目页不该再有来源切换"
        )
        assert summary_page.eval_on_selector_all(".sum-tab", "ns => ns.length") == 2
        assert summary_page.eval_on_selector_all("#sum-light-url", "ns => ns.length") == 0
        assert summary_problems == [], f"控制台报错：{summary_problems}"
    finally:
        summary_page.close()

    light_page, light_problems = open_page(browser, web_base_url, "light")
    try:
        assert_visible(light_page, "#sum-light-title")
        for selector in (
            "#sum-light-url",
            "#sum-light-role",
            "#sum-light-model",
            "#sum-light-probe",
            "#sum-light-file",
            "#sum-light-go",
        ):
            assert_visible(light_page, selector)
        assert light_page.eval_on_selector_all(".sum-tab", "ns => ns.length") == 0, (
            "轻量页不该有单条/合集标签"
        )
        assert light_page.eval_on_selector_all("#sum-project", "ns => ns.length") == 0, (
            "轻量页不该挂着本地项目下拉"
        )
        assert light_problems == [], f"控制台报错：{light_problems}"
    finally:
        light_page.close()


def test_the_light_panel_is_usable_and_says_something_on_empty_input(browser, web_base_url):
    page, problems = open_page(browser, web_base_url, "light")
    try:
        for selector in (
            "#sum-light-url",
            "#sum-light-role",
            "#sum-light-model",
            "#sum-light-probe",
            "#sum-light-file",
            "#sum-light-go",
        ):
            assert_visible(page, selector)

        # 角色/模型选择栏必须真的有选项，不是两个空壳
        role_count = page.eval_on_selector_all("#sum-light-role option", "ns => ns.length")
        model_count = page.eval_on_selector_all("#sum-light-model option", "ns => ns.length")
        assert role_count >= 2, f"角色下拉只有 {role_count} 个选项"
        assert model_count >= 2, f"模型下拉只有 {model_count} 个选项"

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


def test_light_toolbar_does_not_overflow_at_wide_and_min_widths(browser, web_base_url):
    page, problems = open_page(browser, web_base_url, "light")
    try:
        long_title = "AI撞上美国“新世界”：一条超长视频标题用来验证输入框不会被挤出白框"
        page.evaluate(
            """(title) => {
                const s = window.WB.pages.summaryLight;
                s.state.lightInfo = {title: title, up_name: '演示UP'};
                window.WB.router.go('light', {});
            }""",
            long_title,
        )
        page.wait_for_selector("#sum-light-info", timeout=4000)
        for width, height in ((1100, 720), (1525, 900), (2306, 1225), (3840, 1600)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(120)
            box = page.evaluate(
                """() => {
                    const input = document.querySelector('#sum-light-url');
                    const field = input && input.closest('.sum-fld');
                    const fields = document.querySelector('#sum-light-fields');
                    const meta = document.querySelector('#sum-light-meta');
                    if (!input || !field || !fields || !meta) return null;
                    const ir = input.getBoundingClientRect();
                    const fr = field.getBoundingClientRect();
                    const mr = meta.getBoundingClientRect();
                    const fsr = fields.getBoundingClientRect();
                    return {
                        inputLeft: ir.left, inputRight: ir.right,
                        fieldLeft: fr.left, fieldRight: fr.right,
                        metaTop: mr.top, fieldsBottom: fsr.bottom,
                        scrollWidth: document.documentElement.scrollWidth,
                        clientWidth: document.documentElement.clientWidth
                    };
                }"""
            )
            assert box is not None, f"{width}×{height} 下轻量工具栏结构不完整"
            assert box["inputLeft"] >= box["fieldLeft"] - 1, f"{width}px 下输入框左边缘出框：{box}"
            assert box["inputRight"] <= box["fieldRight"] + 1, (
                f"{width}px 下输入框右边缘出框：{box}"
            )
            assert box["metaTop"] >= box["fieldsBottom"] - 1, f"{width}px 下长标题没换行：{box}"
            assert box["scrollWidth"] <= box["clientWidth"] + 1, f"{width}px 下出现横向滚动：{box}"

        assert problems == [], f"控制台报错：{problems}"
    finally:
        page.close()
