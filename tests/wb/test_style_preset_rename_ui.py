# -*- coding: utf-8 -*-
"""真浏览器：预设重命名 / 另存为必须**当场问名字**（bug: 预设无法重命名）。

## 这条守什么

原实现 `renamePreset()` 直接提交 `state.draft.name`。用户点「重命名」时不会先去
「基本」区块改那个输入框，于是提交的是原名 —— 后端保存成功、列表一字未变，
用户看到的是「点了重命名没反应」。`saveAsPreset()` 同族：另存出来的名字与当前
预设一模一样，看着像凭空多了一份副本。

## 判据的形状

不钉「弹窗长什么样」，只钉**用户能走通**：点重命名 → 出现输入框 → 改名字 → 确定
→ 列表里出现新名字、旧名字消失。名字没变时不许提交（否则又是一次「保存成功但没变」）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="没装 playwright，界面判据没跑（不是通过）",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402

from test_settings_role_warning_ui import browser as browser  # noqa: E402,F401
from test_settings_role_warning_ui import web_base_url as web_base_url  # noqa: E402,F401

from video_workbench.app.service import WorkbenchService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

REAL_METHODS = frozenset(
    {
        "style.list",
        # style.get 必须走真后端：pickPreset 靠它把预设内容读进 draft。
        # 漏了它就会落到 mock，选中预设后 draft 还是 mock 那一份，
        # 名字不变、id 也对不上，于是「重命名」永远认不出当前是哪个预设。
        "style.get",
        "style.save",
        "style.delete",
        "style.effective.get",
        "style.effective.put",
        "style.effective.reset",
        "style.fonts",
    }
)

BRIDGE_JS = """
(spec) => {
  const real = new Set(spec.methods);
  const legacy = WB.rpc;
  WB.rpc = function (name, params, opts) {
    if (!real.has(name)) { return legacy.call(WB, name, params, opts); }
    return Promise.resolve(window.__wbRpc({method: name, params: params || {}})).then(res => {
      if (res && res.ok) { return res.data; }
      const err = (res && res.error) || {};
      const e = new Error(err.message || ("backend failed: " + name));
      if (err.code) { e.code = err.code; }
      throw e;
    });
  };
}
"""

def _temp_config(tmp_path: Path) -> Path:
    src = ROOT / "config.yaml"
    lines = []
    for line in src.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("db_path:"):
            lines.append('  db_path: "%s"' % (tmp_path / "p.db").as_posix())
        elif s.startswith("key_path:"):
            lines.append('  key_path: "%s"' % (tmp_path / "p.key").as_posix())
        elif s.startswith("media_root:"):
            lines.append('  media_root: "%s"' % (tmp_path / "media").as_posix())
        elif s.startswith("cache_root:"):
            lines.append('  cache_root: "%s"' % (tmp_path / "cache").as_posix())
        else:
            lines.append(line)
    target = tmp_path / "config.yaml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class _Service:
    def __init__(self, svc: WorkbenchService):
        self.svc = svc

    def dispatch(self, method: str, params=None) -> dict:
        return self.svc.dispatch(method, params or {})

    def call(self, method: str, params=None):
        env = self.dispatch(method, params)
        assert env.get("ok"), f"{method} 失败：{env.get('error')}"
        return env["data"]

    def close(self) -> None:
        self.svc.close()


@pytest.fixture()
def real_service(tmp_path):
    cfg = _temp_config(tmp_path)
    handle = _Service(WorkbenchService(config_path=str(cfg)))
    try:
        yield handle
    finally:
        handle.close()


def _open_styles(browser_ctx, web_base: str, service: _Service):
    page = browser_ctx.new_page(viewport={"width": 1440, "height": 960})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

    def on_console(msg) -> None:
        if msg.type != "error":
            return
        text = msg.text or ""
        if "favicon" in text.lower():
            return
        errors.append(f"console: {text}")

    page.on("console", on_console)

    def _bridge(source, payload):
        method = (payload or {}).get("method") or ""
        try:
            return service.dispatch(method, (payload or {}).get("params") or {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "request_id": None, "error": {"message": str(exc)}}

    page.expose_binding("__wbRpc", _bridge)
    page.goto(f"{web_base}/index.html?mock=1", wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    page.evaluate(BRIDGE_JS, {"methods": sorted(REAL_METHODS)})
    page.evaluate("() => window.WB.router.go('styles', {})")
    page.wait_for_selector("#styles-preview", timeout=15000)
    return page, errors


@pytest.fixture()
def styles_page(browser, web_base_url, real_service):
    try:
        page, errors = _open_styles(browser, web_base_url, real_service)
    except PlaywrightError as exc:
        if "chromium" not in str(exc).lower():
            raise
        pytest.skip(f"Chromium 起不来，界面判据没跑（不是通过）：{exc}")
    try:
        yield page, errors, real_service
    finally:
        page.close()



# ------------------------------------------------------------ 判据
def _preset_names(page) -> list[str]:
    return page.eval_on_selector_all(
        "#styles-list .styles__item-name", "els => els.map(e => e.textContent.trim())"
    )


def _make_and_select(page, backend, name: str) -> str:
    """在后端建一个自建预设，再让页面选中它。返回预设 id。"""
    made = backend.call("style.save", {"name": name, "config": {"primary": {"size": 70}}})
    preset_id = made["style"]["id"]
    page.wait_for_selector("#styles-list", timeout=15000)
    # 路由已经在 styles 上，再 go("styles") 不会重建页面（router 只在换页时 render），
    # 得直接调页面自己的 loadList 把新预设读进来。
    page.evaluate("() => window.WB.pages.styles._internals.loadList()")
    item = page.locator("#styles-list [data-preset-id]").filter(has_text=name).first
    item.wait_for(state="visible", timeout=15000)
    item.click()
    # 套用预设会先 flush 再落 draft，等名字框真的变成这个预设的名字
    page.wait_for_function(
        "(want) => { var el = document.querySelector('#styles-edit [data-field=name]');"
        " return el && el.value === want; }",
        arg=name,
        timeout=15000,
    )
    return preset_id


def test_renaming_a_preset_asks_for_the_new_name_and_applies_it(styles_page):
    """点「重命名」必须当场要新名字；改名后列表里是新名字、旧名字没了。"""
    page, errors, backend = styles_page
    preset_id = _make_and_select(page, backend, "待改的名字")

    page.click("#styles-rename")
    try:
        page.wait_for_selector("#modal-root input[data-field=prompt]", timeout=5000)
    except PlaywrightTimeoutError:
        raise AssertionError(
            "点「重命名」没有出现输入框 —— 用户没有地方输入新名字（这就是「预设无法重命名」）"
        ) from None

    field = page.locator("#modal-root input[data-field=prompt]")
    # 输入框预填当前名字，用户改一下就能提交
    assert field.input_value() == "待改的名字", field.input_value()
    field.fill("改好了")
    page.click("#modal-root .btn--primary")

    # 等后端真改掉
    got = None
    for _ in range(60):
        rows = backend.call("style.list")
        hit = [r for r in rows if r["id"] == preset_id]
        if hit and hit[0]["name"] == "改好了":
            got = hit[0]["name"]
            break
        page.wait_for_timeout(100)
    assert got == "改好了", f"后端名字没改成：{got}"
    page.wait_for_timeout(600)
    names = _preset_names(page)
    assert "改好了" in names, names
    assert "待改的名字" not in names, names
    assert errors == [], errors


def test_renaming_to_the_same_name_is_refused_in_the_dialog(styles_page):
    """名字没变时不许提交 —— 否则又是一次「保存成功但列表一字未变」。"""
    page, errors, backend = styles_page
    _make_and_select(page, backend, "原名")
    page.click("#styles-rename")
    page.wait_for_selector("#modal-root input[data-field=prompt]", timeout=5000)
    # 不动名字直接点确定：弹窗必须还在，并给出一句为什么
    page.click("#modal-root .btn--primary")
    page.wait_for_timeout(300)
    assert page.locator("#modal-root input[data-field=prompt]").count() == 1, (
        "名字没变却关掉了弹窗 —— 用户会以为改成功了"
    )
    assert errors == [], errors


def test_the_name_field_of_a_builtin_preset_is_editable_not_a_dead_box(styles_page):
    """内置预设的名字框不许 disabled。

    原来 builtin 时把名字输入框 disabled 了，用户看得见却打不进字；
    而「重命名」按钮又直接提交 draft.name —— 两处合起来就是「预设无法重命名」。
    """
    page, errors, backend = styles_page
    item = page.locator("#styles-list [data-preset-id]").filter(has_text="默认").first
    item.wait_for(state="visible", timeout=15000)
    item.click()
    page.wait_for_selector("#styles-edit [data-field=name]", timeout=15000)
    page.wait_for_timeout(500)
    box = page.locator("#styles-edit [data-field=name]")
    assert box.is_enabled(), "内置预设的名字框被 disabled 了 —— 用户没法输入新名字"
    assert not box.get_attribute("disabled"), box.get_attribute("disabled")
    assert errors == [], errors


def test_renaming_a_builtin_preset_offers_to_copy_it_first(styles_page):
    """点内置预设的重命名，必须当场问「复制一份再改」，而不是甩一行红字。"""
    page, errors, backend = styles_page
    item = page.locator("#styles-list [data-preset-id]").filter(has_text="默认").first
    item.wait_for(state="visible", timeout=15000)
    item.click()
    page.wait_for_selector("#styles-edit [data-field=name]", timeout=15000)
    page.wait_for_timeout(500)
    page.click("#styles-rename")
    # 先出现的是「内置预设不能改名」的确认框，而不是输入框
    page.wait_for_selector("#modal-root .modal__panel", timeout=5000)
    head = page.eval_on_selector("#modal-root .modal__head", "e => e.textContent")
    assert "内置" in head, head
    # 点「复制一份再改」→ 出现另存为的输入框
    page.click("#modal-root .btn--primary")
    page.wait_for_selector("#modal-root input[data-field=prompt]", timeout=5000)
    assert errors == [], errors
