"""真浏览器验收：三态与交互规格在 Chromium 里真的成立。

为什么要有这一层：jsdom 没有布局、没有真实焦点、没有 offsetParent。三态里最关键的
几条恰恰依赖这些 ——「加载中的转圈真的在转」「错误块的重试按钮真的能点到」
「弹窗打开后 Tab 真的出不去」「磁盘槽真的显示数字而不是破折号」。
jsdom 里全绿不代表用户看得见。

做法与既有约定一致：起一个只读服务指向真实 video_workbench/web，
用 ?mock=1 启动（不需要 pywebview 桥），**不动产品 CSP**。

Chromium 起不来时 pytest.skip 并说明原因 —— **skip 不是 pass**，交差时报 skipped 数。
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "video_workbench" / "web"

pytest.importorskip("playwright.sync_api", reason="没装 playwright，真浏览器验收没跑")

from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture(scope="module")
def web_url():
    Handler = type(
        "QuietHandler",
        (http.server.SimpleHTTPRequestHandler,),
        {"log_message": lambda self, *args: None},
    )

    def factory(*args, **kwargs):
        return Handler(*args, **kwargs, directory=str(WEB_DIR))

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), factory) as httpd:
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
        except Exception as exc:
            pytest.skip(
                "chromium 起不来，真浏览器验收没跑（不是通过，是没跑）。"
                f"原因：{type(exc).__name__}: {str(exc)[:200]}；"
                "修：.venv/Scripts/python.exe -m playwright install chromium"
            )
        try:
            yield launched
        finally:
            launched.close()


def _open(browser, web_url: str):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(web_url, wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.router && window.WB.router.current()")
    return page, errors


def test_the_disk_slot_shows_a_number_not_a_dash(browser, web_url: str) -> None:
    """状态条那一格必须显示真实大小。它今天恒显示「磁盘 —」是因为没人给数。"""
    page, errors = _open(browser, web_url)
    try:
        text = page.inner_text("#status-disk")
        assert text.strip() != "磁盘 —", f"磁盘槽还是占位符：{text!r}"
        assert text.startswith("磁盘"), f"磁盘槽文案不对：{text!r}"
        assert any(ch.isdigit() for ch in text), f"磁盘槽里没有数字：{text!r}"
        visible = page.evaluate(
            "() => { var n = document.getElementById('status-disk');"
            " return !!n && n.getBoundingClientRect().height > 0; }"
        )
        assert visible, "磁盘槽没有可见高度（被裁掉了）"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_the_loading_state_actually_spins(browser, web_url: str) -> None:
    """加载态的转圈必须真的在动：静态半圆读起来像「卡住」。"""
    page, errors = _open(browser, web_url)
    try:
        playing = page.evaluate(
            """() => {
              var host = document.createElement("div");
              document.body.appendChild(host);
              host.appendChild(window.WB.ui.loading("正在读取…"));
              var svg = host.querySelector(".empty--loading svg");
              if (!svg) return { ok: false, why: "没有 svg" };
              var name = getComputedStyle(svg).animationName;
              var box = svg.getBoundingClientRect();
              return { ok: !!name && name !== "none", name: name, w: box.width, h: box.height };
            }"""
        )
        assert playing["ok"], f"加载态的 svg 没有动画：{playing}"
        assert playing["w"] > 0 and playing["h"] > 0, f"转圈没有可见尺寸：{playing}"
        role = page.get_attribute(".empty--loading", "role")
        assert role == "status", f"加载态没有 role=status：{role!r}"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_the_retry_button_is_really_clickable(browser, web_url: str) -> None:
    """错误块的重试按钮必须能被点到（可见、有尺寸、命中测试通过）。

    只断言「节点存在」是不够的：它可能被 display:none 的祖先藏起来，
    或者被别的元素盖住 —— 用户点了没反应，而测试全绿。
    """
    page, errors = _open(browser, web_url)
    try:
        result = page.evaluate(
            """() => {
              var host = document.createElement("div");
              // 挂在页面顶端并固定定位：探测块如果被推到视口外，
              // getBoundingClientRect 是视口坐标、elementFromPoint 会返回 null，
              // 那条断言就变成在测「它在不在屏内」而不是「它能不能被点到」。
              host.style.cssText = "position:fixed;left:20px;top:20px;z-index:9999;background:#fff";
              document.body.appendChild(host);
              var hits = 0;
              host.appendChild(window.WB.ui.errorFrom(
                { message: "网络断了", retryable: true },
                function () { hits += 1; },
                "读不到任务"
              ));
              var btn = host.querySelector("button");
              if (!btn) return { ok: false, why: "没有重试按钮" };
              var box = btn.getBoundingClientRect();
              var hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
              btn.click();
              return {
                ok: true, w: box.width, h: box.height,
                hit: hit === btn || (!!hit && btn.contains(hit)),
                hits: hits,
                role: host.querySelector(".empty").getAttribute("role")
              };
            }"""
        )
        assert result["ok"], result
        assert result["w"] > 0 and result["h"] > 0, f"重试按钮没有可见尺寸：{result}"
        assert result["hit"], f"重试按钮被别的东西盖住了：{result}"
        assert result["hits"] == 1, f"重试按钮点了没反应：{result}"
        assert result["role"] == "alert", f"错误块没有 role=alert：{result}"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_a_fatal_error_offers_no_retry_button(browser, web_url: str) -> None:
    """不可重试的错误不许长按钮：点了也白点，还会让人以为是网络问题。"""
    page, errors = _open(browser, web_url)
    try:
        count = page.evaluate(
            """() => {
              var host = document.createElement("div");
              document.body.appendChild(host);
              host.appendChild(window.WB.ui.errorFrom(
                { message: "接口地址没填", retryable: false },
                function () {},
                "读不到任务"
              ));
              return host.querySelectorAll("button").length;
            }"""
        )
        assert count == 0, f"不可重试的错误也给了 {count} 个按钮"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_tab_cannot_escape_the_modal(browser, web_url: str) -> None:
    """弹窗打开后 Tab 必须出不去。这是 aria-modal="true" 唯一的实际内容。

    jsdom 里 offsetParent 恒为 null，测不出这一条 —— 只有在真浏览器里，
    焦点循环才有意义。
    """
    page, errors = _open(browser, web_url)
    try:
        page.evaluate(
            """() => {
              window.__dlg = window.WB.ui.modal({
                title: "焦点自检",
                body: window.WB.ui.h("p", { text: "三个按钮" }),
                footer: [
                  window.WB.ui.h("button", { class: "btn", type: "button", text: "甲" }),
                  window.WB.ui.h("button", { class: "btn", type: "button", text: "乙" }),
                  window.WB.ui.h("button", { class: "btn", type: "button", text: "丙" })
                ]
              });
            }"""
        )
        page.wait_for_selector(".modal__panel")
        inside = []
        for _ in range(8):
            page.keyboard.press("Tab")
            inside.append(
                page.evaluate(
                    """() => {
                      var panel = document.querySelector(".modal__panel");
                      var a = document.activeElement;
                      return !!panel && (a === panel || panel.contains(a));
                    }"""
                )
            )
        assert all(inside), f"Tab 把焦点带出了弹窗：{inside}"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_focus_returns_to_the_opener(browser, web_url: str) -> None:
    """关闭弹窗后焦点必须回到打开它的那个按钮。

    否则键盘用户得从页首重新 Tab 一遍才能回到刚才的位置。
    """
    page, errors = _open(browser, web_url)
    try:
        page.evaluate(
            """() => {
              var opener = document.getElementById("status-jobs");
              opener.focus();
              window.__dlg2 = window.WB.ui.modal({
                title: "还焦点自检",
                body: window.WB.ui.h("p", { text: "关掉之后焦点要回来" }),
                footer: [window.WB.ui.h("button", { class: "btn", type: "button", text: "好" })]
              });
            }"""
        )
        page.wait_for_selector(".modal__panel")
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal__panel", state="detached")
        landed = page.evaluate("() => document.activeElement && document.activeElement.id")
        assert landed == "status-jobs", f"关闭后焦点落在 {landed!r}，没回到打开它的按钮"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_the_empty_jobs_modal_is_not_blank(browser, web_url: str) -> None:
    """没有任何任务时，任务中心必须画出空态，不能是一整块空白。

    事故形状：sameSet 在空集合上判成「没变」⇒ 直接 return ⇒ 空态那段永远不执行。
    """
    page, errors = _open(browser, web_url)
    try:
        page.evaluate(
            """() => {
              window.WB.rpc = (function (real) {
                return function (method, params, opts) {
                  if (method === "job.list") return Promise.resolve([]);
                  return real.call(window.WB, method, params, opts);
                };
              })(window.WB.rpc);
              document.getElementById("status-jobs").click();
            }"""
        )
        page.wait_for_selector(".modal__panel")
        page.wait_for_function(
            """() => {
              var body = document.querySelector(".modal__body");
              return !!body && body.textContent.indexOf("还没有任务") >= 0;
            }""",
            timeout=5000,
        )
        text = page.inner_text(".modal__body")
        assert "右下角" not in text, f"空态还在让人去点右下角（那里没有按钮）：{text}"
        btn = page.query_selector(".modal__body button")
        assert btn is not None, "空态里没有动作按钮"
        box = btn.bounding_box()
        assert box and box["width"] > 0 and box["height"] > 0, f"空态按钮不可见：{box}"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_the_rail_has_no_empty_counter_badge(browser, web_url: str) -> None:
    """左栏那排空白小方块（.rail__count）必须没了。

    它是个从没被填过的占位符：每个入口右侧都挂一个空 span，看着像加载失败。
    """
    page, errors = _open(browser, web_url)
    try:
        count = page.evaluate("() => document.querySelectorAll('.rail__count').length")
        assert count == 0, f"左栏还有 {count} 个空的计数占位符"
        assert not errors, f"控制台报错：{errors}"
    finally:
        page.close()


def test_no_page_renders_a_blank_error_shell(browser, web_url: str) -> None:
    """每个页面在演示数据下都不许出现「空标题 + 空正文」的错误块。

    三态改造最容易留下的尾巴：错误块建出来了、但没有文案，用户看到一块空白。
    """
    page, errors = _open(browser, web_url)
    try:
        routes = ["library", "subtitles", "summary", "clipper", "merge", "settings", "newtask"]

        # file:// 媒体路径在 http 下必然被浏览器拒绝（mock 数据里写的是本地绝对路径）。
        # 那是演示数据与本测试传输方式的不匹配，不是三态缺陷；把这类噪声排除，
        # 剩下的任何控制台错误仍然算失败。
        def real_error(text: str) -> bool:
            return "Not allowed to load local resource" not in text

        for route in routes:
            page.evaluate("(r) => window.WB.router.go(r)", route)
            page.wait_for_timeout(700)
            blanks = page.evaluate(
                """() => {
                  var bad = [];
                  document.querySelectorAll('[role="alert"], .empty').forEach(function (n) {
                    var text = (n.textContent || "").trim();
                    var box = n.getBoundingClientRect();
                    if (box.height > 0 && text.length === 0) bad.push(n.className);
                  });
                  return bad;
                }"""
            )
            assert not blanks, f"{route} 页出现空白的错误块/空态：{blanks}"
        serious = [e for e in errors if real_error(e)]
        assert not serious, f"控制台报错：{serious}"
    finally:
        page.close()
