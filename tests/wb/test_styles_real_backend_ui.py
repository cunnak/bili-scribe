# -*- coding: utf-8 -*-
"""真后端 + 真 Chromium：字幕样式系统的作用域、预览、导出全流程（任务 7）。

## 为什么要有这个文件

`tests/wb/frontend_test.js` 那批跑的是 `?mock=1`，它证明得了「控件连上了、草稿会变」，
证明不了「后端真的按作用域取样式」。这一层要的是：真 `WorkbenchService`、真 SQLite、
真 `effective_style`、真 `format_ass`，前端只当读者与操作者。

## 接缝（现读，不是猜的）

- `desktop.py` 的 `bridge.rpc = service.dispatch(method, params, request_id)` 是产品自带的
  业务入口，不依赖窗口；`WorkbenchService(config_path=...)` 是不开窗的装配路径。
- 页面**不许 fetch**：`web/index.html` 的 CSP 是 connect-src none，所以走
  `page.expose_binding` 把 `dispatch` 挂成桥，和桌面版同一形状。**不为测试放宽产品 CSP。**
- 首屏仍用 `?mock=1` 起来（避开 `var WB = window.WB || {}` 反复重装的陷阱），起来后把
  `WB.rpc` 里与本文件相关的方法换成真后端，其余留在 mock。

## 隔离

- 配置复制到 Temp，只把 `storage.db_path` / `storage.key_path` 改到 Temp；
  **`APPDATA` / `LOCALAPPDATA` 一律不动**（AGENTS.md 第四节：动了 pythonnet 起不来）。
- 每个用例结束 `close()` 真 service。
"""

from __future__ import annotations

from pathlib import Path

import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="没装 playwright，真后端界面用例没跑（不是通过）。修复："
    ".venv/Scripts/python.exe -m pip install playwright==1.64.1 "
    "&& .venv/Scripts/python.exe -m playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from test_settings_role_warning_ui import browser as browser  # noqa: E402,F401
from test_settings_role_warning_ui import web_base_url as web_base_url  # noqa: E402,F401

from video_workbench.app.service import WorkbenchService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
#: 这些方法接真后端，其余留 mock。样式系统的全部读写入口都在这里。
REAL_METHODS = frozenset(
    {
        "style.list",
        "style.get",
        "style.save",
        "style.delete",
        "style.effective.get",
        "style.effective.put",
        "style.effective.reset",
        "style.render",
        "style.fonts",
        "app.settings.get",
        "app.settings.put",
        "library.projects",
    }
)

#: `WB.rpc` 换成外置桥；返回 `res["data"]`，与前端 rpc.js 取的那一层一致。
#: 失败时把 code / data 一并带回，页面才能显示全部字段错误（有效样式那条 VALIDATION）。
BRIDGE_JS = """
(spec) => {
  const real = new Set(spec.methods);
  const legacy = WB.rpc;
  window.__realCalls = [];
  WB.rpc = function (name, params, opts) {
    if (!real.has(name)) { return legacy.call(WB, name, params, opts); }
    window.__realCalls.push(name);
    return Promise.resolve(window.__wbRpc({method: name, params: params || {}})).then(res => {
      if (res && res.ok) { return res.data; }
      const err = (res && res.error) || {};
      const e = new Error(err.message || ("backend failed: " + name));
      if (err.code) { e.code = err.code; }
      if (err.data) { e.data = err.data; }
      throw e;
    });
  };
}
"""


def _temp_config(tmp_path: Path) -> Path:
    """复制仓库配置到 Temp，只把 db / key 指到 Temp。

    不用最小配置：真实配置里的 provider / apiKey 与判据无关，但「用户实际那份配置
    被改了吗」这类断言只有拿真配置才成立。
    """
    src = ROOT / "config.yaml"
    text = src.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("db_path:"):
            lines.append('  db_path: "%s"' % (tmp_path / "wb-styles.db").as_posix())
        elif stripped.startswith("key_path:"):
            lines.append('  key_path: "%s"' % (tmp_path / "wb-styles.key").as_posix())
        else:
            lines.append(line)
    target = tmp_path / "config.yaml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class _Service:
    """真 `WorkbenchService` 的薄壳：记桥收到的方法名，失败当场红。"""

    def __init__(self, svc: WorkbenchService, config_path: Path):
        self.svc = svc
        self.config_path = config_path
        self.bridge_calls: list[str] = []

    def dispatch(self, method: str, params=None) -> dict:
        return self.svc.dispatch(method, params or {})

    def call(self, method: str, params=None):
        envelope = self.dispatch(method, params)
        if not envelope.get("ok"):
            raise AssertionError(f"{method} 失败：{envelope.get('error')}")
        return envelope["data"]

    def close(self) -> None:
        self.svc.close()


@pytest.fixture()
def real_service(tmp_path):
    """真 service + Temp 配置；teardown 一定 close()。"""
    cfg_path = _temp_config(tmp_path)
    handle = _Service(WorkbenchService(config_path=str(cfg_path)), cfg_path)
    try:
        yield handle
    finally:
        handle.close()


def _open_styles(browser_ctx, web_base: str, service: _Service, params: dict | None = None):
    """开样式页并把 WB.rpc 换成真后端桥。返回 (page, errors)。"""
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
        service.bridge_calls.append(method)
        try:
            return service.dispatch(method, (payload or {}).get("params") or {})
        except Exception as exc:  # noqa: BLE001 - 桥不能把页面拖死，按产品信封回错
            return {
                "ok": False,
                "request_id": None,
                "error": {"message": f"{type(exc).__name__}: {exc}"},
            }

    page.expose_binding("__wbRpc", _bridge)
    page.goto(f"{web_base}/index.html?mock=1", wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    page.evaluate(BRIDGE_JS, {"methods": sorted(REAL_METHODS)})
    page.evaluate("(p) => window.WB.router.go('styles', p)", params or {})
    try:
        page.wait_for_selector("#styles-preview", timeout=15000)
    except PlaywrightTimeoutError as exc:
        raise AssertionError(
            f"样式页没渲染出预览区：{exc}\n页面期间收集到的错误：\n"
            + "\n".join(errors[:12])
            + "\n桥收到的方法："
            + repr(service.bridge_calls[:12])
        ) from None
    return page, errors


@pytest.fixture()
def styles_page(browser, web_base_url, real_service):
    """(page, errors, backend) —— 页面已经走进全局样式页（无 project_id）。"""
    try:
        page, errors = _open_styles(browser, web_base_url, real_service)
    except PlaywrightError as exc:
        if "chromium" not in str(exc).lower():
            raise
        pytest.skip(f"Chromium 起不来，界面验收没跑（不是通过，是没跑）：{exc}")
    try:
        yield page, errors, real_service
    finally:
        page.close()


# ------------------------------------------------------------ 全局作用域
def test_the_global_page_reads_the_backend_default(styles_page):
    """全局页显示的就是后端那份内置 default，不是前端自己拼的数。"""
    page, errors, backend = styles_page
    conf = backend.call("style.effective.get", {})
    assert conf["scope"] == "global"
    assert conf["inherited"] is True
    # 读 input 的 value 属性（ui.h 走 node.value，没有 value 特性），
    # 用 get_attribute 会永远拿到 None —— 那条断言看着在比数，其实在比空。
    size = page.input_value('#styles-edit [data-face="primary"][data-field="size"]')
    assert str(size) == str(conf["config"]["primary"]["size"]), (
        f"页面显示的主字号 {size} 与后端 {conf['config']['primary']['size']} 不一致"
    )
    assert "style.effective.get" in backend.bridge_calls, "这条断言没有读真后端"
    assert errors == [], errors


def test_editing_the_global_size_saves_to_the_backend(styles_page):
    """改字号 → 自动保存 → 后端真变了。防「只改了内存里的草稿」。"""
    page, errors, backend = styles_page
    before = backend.call("style.effective.get", {})["config"]["primary"]["size"]
    target = 88 if int(before) != 88 else 76
    field = '#styles-edit [data-face="primary"][data-field="size"]'
    page.fill(field, str(target))
    page.dispatch_event(field, "change")
    # 400ms 防抖 + 一次往返：等的是**后端读数**，不是页面文案
    got = None
    for _ in range(80):
        got = backend.call("style.effective.get", {})["config"]["primary"]["size"]
        if str(got) == str(target):
            break
        page.wait_for_timeout(100)
    assert str(got) == str(target), f"后端没存住：期望 {target}，实得 {got}"
    assert errors == [], errors


# ------------------------------------------------------------ 项目作用域
def _make_project(backend: _Service, tmp_path: Path) -> dict:
    root = tmp_path / "media" / "UP" / "样式项目"
    root.mkdir(parents=True, exist_ok=True)
    return backend.svc.repo.project_create(title="样式项目", up_name="UP", root_dir=str(root))


def test_a_project_page_starts_inheriting_the_global(styles_page, tmp_path):
    """项目页第一次打开：显示「正在继承全局」，且**不许**顺手建覆盖。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    page.evaluate("(p) => window.WB.router.go('styles', p)", {"project_id": project["id"]})
    # 等的是页面真把作用域标签画出来（不是等一个恒真条件）
    page.wait_for_function(
        "() => { var el = document.querySelector('#styles-scope-pill');"
        " return el && el.textContent.indexOf('继承') >= 0; }",
        timeout=15000,
    )
    data = backend.call("style.effective.get", {"project_id": project["id"]})
    assert data["scope"] == "global", data["scope"]
    assert data["inherited"] is True
    # 查看不建覆盖：库里那一列还是 NULL
    row = backend.svc.repo.project_get(project["id"])
    assert row.get("subtitle_style_json") in (None, ""), row.get("subtitle_style_json")
    assert errors == [], errors


def test_the_project_override_creates_its_own_copy_and_keeps_global(styles_page, tmp_path):
    """项目里改字号：建独立覆盖，全局那份**一点不动**。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    global_before = backend.call("style.effective.get", {})["config"]["primary"]["size"]
    page.evaluate("(p) => window.WB.router.go('styles', p)", {"project_id": project["id"]})
    page.wait_for_function(
        "() => { var el = document.querySelector('#styles-scope-pill');"
        " return el && el.textContent.indexOf('继承') >= 0; }",
        timeout=15000,
    )
    field = '#styles-edit [data-face="primary"][data-field="size"]'
    page.fill(field, "96")
    page.dispatch_event(field, "change")
    got = None
    for _ in range(80):
        got = backend.call("style.effective.get", {"project_id": project["id"]})
        if got["scope"] == "project":
            break
        page.wait_for_timeout(100)
    assert got["scope"] == "project", f"没建立项目覆盖：{got['scope']}"
    assert got["inherited"] is False
    assert str(got["config"]["primary"]["size"]) == "96"
    # 全局不受影响：项目覆盖是整份拷贝，不是共享引用
    after = backend.call("style.effective.get", {})["config"]["primary"]["size"]
    assert str(after) == str(global_before), f"全局被项目覆盖带跑了：{global_before} → {after}"
    assert errors == [], errors


def test_reset_brings_the_project_back_to_inheriting(styles_page, tmp_path):
    """恢复继承之后，项目读到的又是全局那份。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    backend.call(
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 100}}},
    )
    assert backend.call("style.effective.get", {"project_id": project["id"]})["scope"] == "project"
    data = backend.call("style.effective.reset", {"project_id": project["id"]})
    assert data["scope"] == "global", data["scope"]
    assert data["inherited"] is True
    row = backend.svc.repo.project_get(project["id"])
    assert row.get("subtitle_style_json") in (None, ""), "reset 之后库里还留着覆盖"
    assert errors == [], errors


# ------------------------------------------------------- 导出格式的样式边界
def test_srt_and_vtt_carry_no_style_but_ass_does(styles_page, tmp_path):
    """SRT / VTT 不带样式（格式本身没有），ASS 才带——三种格式各归各的。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    backend.call(
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 91, "color": "#12AB34"}}},
    )
    root = Path(backend.svc.repo.project_get(project["id"])["root_dir"])
    (root / "transcript.json").write_text(
        '{"segments": [{"start": 0.0, "end": 1.5, "text": "第一句"}]}', encoding="utf-8"
    )
    backend.svc.repo.transcript_upsert(
        project["id"],
        segments=[{"start_ms": 0, "end_ms": 1500, "text": "第一句"}],
    )
    data = backend.call(
        "subtitle.export",
        {"project_id": project["id"], "formats": ["srt", "vtt", "ass"], "mode": "none"},
    )
    assert data["style_scope"] == "project", data["style_scope"]
    by_fmt = {f["format"]: Path(f["path"]) for f in data["files"]}
    assert set(by_fmt) == {"srt", "vtt", "ass"}
    srt = by_fmt["srt"].read_text(encoding="utf-8")
    vtt = by_fmt["vtt"].read_text(encoding="utf-8")
    ass = by_fmt["ass"].read_text(encoding="utf-8")
    # SRT/VTT 只承载文本与时间，样式参数一个都不该出现
    for body in (srt, vtt):
        assert "Style:" not in body
        assert "PlayRes" not in body
        assert "第一句" in body
    # ASS 才带样式，而且带的是项目覆盖那一份（字号 91 / 颜色 BGR 反序）
    assert "Style:" in ass
    assert "PlayResY: 1080" in ass
    assert ",91," in ass, ass[:400]
    assert "&H0034AB12" in ass.upper(), ass[:400]
    assert errors == [], errors


def test_a_missing_font_is_named_in_the_payload(styles_page, tmp_path):
    """缺字体不能只靠 libass 静默替换：载荷里必须点名用了哪个后备。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    data = backend.call(
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"font": "这个字体肯定不存在"}}},
    )
    fonts = backend.call("style.effective.get", {"project_id": project["id"]})["actual_fonts"]
    assert fonts["fonts"]["primary"] == "Microsoft YaHei", fonts
    assert "这个字体肯定不存在" in fonts["missing"], fonts
    assert fonts["note"], "缺字体必须有一句说明，不能只是静默替换"
    assert data["scope"] == "project"
    assert errors == [], errors


# ---------------------------------------------------------------- 预设管理
def test_save_as_preset_keeps_the_scope_and_appears_in_the_list(styles_page, tmp_path):
    """把项目覆盖另存成预设：预设进列表，项目覆盖**仍然是它自己那一份**。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    backend.call(
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 95}}},
    )
    made = backend.call(
        "style.save",
        {
            "name": "我的竖屏",
            "config": backend.call("style.effective.get", {"project_id": project["id"]})["config"],
        },
    )
    preset_id = made["style"]["id"]
    assert preset_id, made
    # style.list 返回的是**裸数组**，不是 {items: [...]}——按错形状读会静默得到空
    rows = backend.call("style.list")
    assert isinstance(rows, list), type(rows)
    ids = [row["id"] for row in rows]
    assert preset_id in ids, ids
    # 另存预设不改当前作用域：项目覆盖还在
    still = backend.call("style.effective.get", {"project_id": project["id"]})
    assert still["scope"] == "project"
    assert str(still["config"]["primary"]["size"]) == "95"
    assert errors == [], errors


def test_deleting_a_preset_that_a_project_overrides_from_leaves_it_alone(styles_page, tmp_path):
    """删预设不影响已经独立出来的项目覆盖——覆盖是整份拷贝，不是引用。"""
    page, errors, backend = styles_page
    project = _make_project(backend, tmp_path)
    preset = backend.call("style.save", {"name": "会被删的", "config": {"primary": {"size": 84}}})
    backend.call("style.effective.put", {"project_id": project["id"], "config": preset["style"]})
    backend.call("style.delete", {"id": preset["style"]["id"]})
    after = backend.call("style.effective.get", {"project_id": project["id"]})
    assert after["scope"] == "project", after["scope"]
    assert str(after["config"]["primary"]["size"]) == "84"
    assert errors == [], errors
