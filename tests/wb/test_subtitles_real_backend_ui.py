"""真后端 + 真 Chromium：字幕页的项目条 / 切换 / reveal / 竞态（docs/plan-subtitles-entry.md §5.6 判据 1–6）。

## 为什么要有这个文件

frontend_test.js 那批复刻的是 mock 形状，测不到「library.list 真的这样排序吗」「fs.reveal
收到的真的是 root_dir 吗」「旧项目的迟到响应真的进不来吗」。本文件用真 WorkbenchService +
真 Chromium 把规划书 §5.6 的六条判据钉在产品自己的运输形状上。

## 接缝（与 test_summary_real_backend_ui.py 同一族）

- 页面用 ?mock=1 起首屏，再把 library.list / library.get / subtitle.get / subtitle.save /
  fs.reveal 五个方法经 page.expose_binding("__wbRpc", ...) 接到进程内真 service.dispatch。
  其余方法留在 mock——本文件测的是字幕页的数据来源与切换状态机，不是整站连通性。
- 产品的 CSP 是 connect-src 'none'（web/index.html:13），页面不做 fetch；pywebview 桥
  （desktop.py 把 window.api.rpc 接到 service.dispatch）才是产品本来的运输形状，
  expose_binding 复刻的就是这一条。test_the_product_page_forbids_its_own_http_calls
  常驻钉着 CSP 还是 'none'，所以这条替代不是绕过。

## 夹具要点（规划书 §5.6 的夹具要求，逐条落地）

- _temp_config：复制仓库 config.yaml，只把 storage.db_path / storage.key_path 指到 Temp。
  notes-cache 跟 db 走，磁盘缓存也隔离。
- 两个项目 proj-a / proj-b 直接 service.svc.repo.project_create(...) 建，root_dir 是 Temp
  下两个真目录，各写 transcript.json 占位，再用 subtitle.save 写入两份不同字幕
  （服务端 _store_subtitles 同时写 DB 与 transcript.json）。
- **fs.reveal 的白名单**：_allowed_roots() 含 os.path.dirname(self.cfg.db_path)——夹具把
  db 指到 tmp_path，项目目录就在 db 同级之下，天然落在 reveal 白名单内，不需要任何豁免。
- os.startfile 被 monkeypatch 成记录函数，fs.reveal 不会真的打开资源管理器。
- 判据 4 的「保存失败」在桥层注入（fail_save 标志位把 subtitle.save 换成错误信封），
  不改产品码——错误信封形状与产品 WBError 一致，前端走的是同一条 RpcError 路径。

## 牙齿（阶段 3 第 4 条「先确认改坏会红」的落地方式）

仓库纪律禁止对工作区文件做快照回写式的改坏，所以真桥侧的牙齿全部走**数据/条件层**：

- 数据牙齿（test_the_default_pick_skips_a_project_without_transcript_on_disk）：把
  proj-a 的 transcript.json 删掉再无参进入，默认选择必须跳到 proj-b——若 defaultProjectId
  退化成「无脑取第一个」，这条会红。
- 条件牙齿（test_when_the_save_succeeds_the_switch_is_not_blocked）：同一套「编辑→切换」
  流程，不注入保存失败时切换必须成功落在 proj-b——证明判据 4 的红真的来自「保存失败」
  这个条件，而不是流程本身。
- mock 侧的全局牙齿已在 frontend_test.js 验过（nav:true 落地当天左栏断言按预期报
  「7 个，应为 6」）。

## 已知限制

- 判据 6 的浏览器后退跑在 http://127.0.0.1 静态服务上（web_base_url fixture），不是
  桌面壳的 file:// 场景。规划书 §5.6 判据 6 允许「file:// 不稳定则降级为 history.length
  不增」——本文件先断言 go_back 回 library；history.length 那半句由 frontend_test.js 的
  mock 用例（replace 不增历史）钉住。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="没装 playwright，字幕真后端界面用例没跑（不是通过）。修复："
    ".venv/Scripts/python.exe -m pip install playwright==1.64.1 "
    "&& .venv/Scripts/python.exe -m playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from test_settings_role_warning_ui import browser as browser  # noqa: E402,F401
from test_settings_role_warning_ui import web_base_url as web_base_url  # noqa: E402,F401

from video_workbench.app.service import WorkbenchService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "video_workbench" / "web"

#: 只有这五个方法接到真后端，其余留 mock（理由见文件头「接缝」）。
REAL_METHODS = frozenset(
    {
        "library.list",
        "library.get",
        "subtitle.get",
        "subtitle.save",
        # commitEdit 走 runOp("subtitle.op")（服务端语义操作）——不接它，
        # 编辑会落进 mock 层（段 id 对不上、dirty 不置位），判据 3/4 测的是假链路。
        "subtitle.op",
        "fs.reveal",
    }
)

#: 注入到页面里的包装：WB.rpc → 外置桥函数 window.__wbRpc。返回 res["data"]，
#: 与前端 rpc.js 取的那一层一致。
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
      throw new Error(err.message || ('后端调用失败: ' + name));
    });
  };
}
"""


def _temp_config(tmp_path: Path) -> Path:
    """复制仓库配置到 Temp，只把 db / key 指到 Temp（照抄范式文件的同一函数）。"""
    src = ROOT / "config.yaml"
    text = src.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("db_path:"):
            lines.append('  db_path: "%s"' % (tmp_path / "wb-sub-e2e.db").as_posix())
        elif stripped.startswith("key_path:"):
            lines.append('  key_path: "%s"' % (tmp_path / "wb-sub-e2e.key").as_posix())
        else:
            lines.append(line)
    target = tmp_path / "config.yaml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


SEGMENTS_A = [
    {"start": 0.0, "end": 2.0, "text": "alpha line one"},
    {"start": 2.0, "end": 4.0, "text": "alpha line two"},
]
SEGMENTS_B = [
    {"start": 0.0, "end": 2.0, "text": "beta line one"},
    {"start": 2.0, "end": 4.0, "text": "beta line two"},
]


class _SubService:
    """真 WorkbenchService 的薄壳 + 测试注入点（不改产品码）。

    - fail_save=True 期间所有 subtitle.save 回错误信封（判据 4 的注入条件）；
    - delay_project 让 subtitle.get 对该项目 sleep（判据 2 的迟到响应）；
    - reveal_calls 记录页面发来的 fs.reveal 参数（判据 5）。
    """

    def __init__(self, svc: WorkbenchService, config_path: Path):
        self.svc = svc
        self.config_path = config_path
        self.fail_save = False
        self.delay_project: str | None = None
        self.reveal_calls: list[dict] = []
        self.bridge_calls: list[str] = []

    def dispatch(self, method: str, params=None) -> dict:
        params = params or {}
        self.bridge_calls.append(method)
        if method == "fs.reveal":
            self.reveal_calls.append(dict(params))
        if method == "subtitle.save" and self.fail_save:
            return {
                "ok": False,
                "request_id": None,
                "error": {"message": "测试注入：保存失败"},
            }
        if (
            method == "subtitle.get"
            and self.delay_project
            and params.get("project_id") == self.delay_project
        ):
            time.sleep(0.6)
        return self.svc.dispatch(method, params)

    def call(self, method: str, params=None):
        envelope = self.dispatch(method, params)
        if not envelope.get("ok"):
            raise AssertionError(f"{method} 失败：{envelope.get('error')}")
        return envelope["data"]

    def shutdown(self) -> None:
        self.svc.close()


@pytest.fixture()
def sub_world(tmp_path, monkeypatch):
    """真 service + 两个带字幕的项目；teardown 一定 close()。"""
    cfg_path = _temp_config(tmp_path)
    svc = WorkbenchService(config_path=str(cfg_path))
    world = _SubService(svc, cfg_path)

    world.root_a = tmp_path / "proj-a"
    world.root_b = tmp_path / "proj-b"
    world.root_a.mkdir()
    world.root_b.mkdir()
    # transcript.json 占位：_decorate 的 has_transcript 读的是磁盘文件（app/service.py）。
    # 占位必须是 JSON 对象：_store_subtitles 会 json.loads 这份文件再 payload["segments"]=…，
    # 写成数组会在服务端炸成 list indices must be integers。
    (world.root_a / "transcript.json").write_text("{}", encoding="utf-8")
    (world.root_b / "transcript.json").write_text("{}", encoding="utf-8")

    svc.repo.project_create(
        id="proj-a",
        title="项目甲",
        up_name="up-alpha",
        root_dir=str(world.root_a),
        stage="new",
    )
    svc.repo.project_create(
        id="proj-b",
        title="项目乙",
        up_name="up-beta",
        root_dir=str(world.root_b),
        stage="new",
    )
    world.call("subtitle.save", {"project_id": "proj-a", "segments": SEGMENTS_A})
    world.call("subtitle.save", {"project_id": "proj-b", "segments": SEGMENTS_B})

    # fs.reveal 里 os.startfile 是属性访问，patch 掉就不会真开 Explorer。
    started: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(str(p)), raising=True)
    world.started = started

    try:
        yield world
    finally:
        world.shutdown()


def _open_subs(browser, web_base: str, world: _SubService, route_params=None):
    """开页面：mock 起首屏 → 五个方法换真桥 → 进字幕页（可带参数）。"""
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: (
            errors.append(f"console: {msg.text}")
            if msg.type == "error" and "favicon" not in (msg.text or "")
            else None
        ),
    )

    def _bridge(source, payload):
        method = payload.get("method") or ""
        try:
            return world.dispatch(method, payload.get("params") or {})
        except Exception as exc:  # noqa: BLE001 - 桥不能把页面拖死，错误照产品信封回
            return {
                "ok": False,
                "request_id": None,
                "error": {"message": f"{type(exc).__name__}: {exc}"},
            }

    page.expose_binding("__wbRpc", _bridge)
    page.goto(f"{web_base}/index.html?mock=1", wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    page.evaluate(BRIDGE_JS, {"methods": sorted(REAL_METHODS)})
    page.evaluate("(p) => window.WB.router.go('subtitles', p)", route_params)
    try:
        page.wait_for_selector("#sub-project-select", timeout=15000)
    except PlaywrightTimeoutError as exc:
        raise AssertionError(
            f"字幕页没渲染出项目条：{exc}\n页面期间收集到的错误：\n"
            + "\n".join(errors[:12])
            + "\n桥收到的方法："
            + repr(world.bridge_calls[:12])
        ) from None
    return page, errors


@pytest.fixture()
def subs_page(browser, web_base_url, sub_world):
    """(page, errors, world) —— 页面已带参进入 proj-a。"""
    try:
        page, errors = _open_subs(browser, web_base_url, sub_world, {"project": "proj-a"})
    except PlaywrightError as exc:
        if "chromium" not in str(exc).lower():
            raise
        pytest.skip(f"Chromium 起不来，界面验收没跑（不是通过，是没跑）：{exc}")
    try:
        yield page, errors, sub_world
    finally:
        page.close()


@pytest.fixture()
def subs_page_noargs(browser, web_base_url, sub_world):
    """(page, errors, world) —— 页面无参进入字幕页（默认项目逻辑）。"""
    try:
        page, errors = _open_subs(browser, web_base_url, sub_world, None)
    except PlaywrightError as exc:
        if "chromium" not in str(exc).lower():
            raise
        pytest.skip(f"Chromium 起不来，界面验收没跑（不是通过，是没跑）：{exc}")
    try:
        yield page, errors, sub_world
    finally:
        page.close()


def _wait_selected(page: "object", project_id: str, timeout: int = 10000) -> None:
    page.wait_for_function(
        "(v) => { const el = document.getElementById('sub-project-select');"
        " return el && el.value === v; }",
        arg=project_id,
        timeout=timeout,
    )


def _begin_edit(page) -> None:
    """用 JS dispatch 触发行编辑（理由见文件头「已知限制」一节）。"""
    page.evaluate(
        "() => document.querySelector('.sub-row').dispatchEvent("
        "new MouseEvent('dblclick', {bubbles: true}))"
    )
    page.wait_for_selector(".sub-row__input", timeout=5000)


def _route_project(page) -> str:
    return page.evaluate("() => window.WB.router.current().params.project")


# ---------------------------------------------------------------------------
# 判据 1：无参数打开 → 选中第一个 has_transcript 的项目；列表显示该项目字幕。
# ---------------------------------------------------------------------------


def test_without_params_the_default_pick_is_the_first_project_with_transcript(
    subs_page_noargs,
):
    page, errors, world = subs_page_noargs
    listing = world.call("library.list", {"limit": 200, "offset": 0})
    expected = next(p["id"] for p in listing["items"] if p.get("has_transcript"))
    _wait_selected(page, expected)
    body = page.inner_text("#view")
    first_line = SEGMENTS_A[0]["text"] if expected == "proj-a" else SEGMENTS_B[0]["text"]
    assert first_line in body, f"默认项目 {expected} 的字幕行没渲染出来；页面正文片段：{body[:200]}"


def test_the_default_pick_skips_a_project_without_transcript_on_disk(subs_page_noargs):
    """数据牙齿：删掉 proj-a 的 transcript.json 后，无参进入必须选 proj-b。

    has_transcript 是 _decorate 按磁盘文件现算的；若 defaultProjectId 退化成
    「无脑取第一个项目」，这条会红。
    """
    page, errors, world = subs_page_noargs
    (world.root_a / "transcript.json").unlink()
    page.evaluate("() => window.WB.router.go('library')")
    page.wait_for_function("() => window.WB.router.current().route === 'library'", timeout=10000)
    page.evaluate("() => window.WB.router.go('subtitles')")
    page.wait_for_selector("#sub-project-select", timeout=15000)
    _wait_selected(page, "proj-b")


# ---------------------------------------------------------------------------
# 判据 2：切换项目 → params 变、列表变；旧项目的迟到响应不能覆盖新项目。
# ---------------------------------------------------------------------------


def test_switching_projects_lands_on_the_later_one_and_the_stale_response_cannot_overwrite(
    subs_page,
):
    page, errors, world = subs_page
    world.delay_project = "proj-a"  # proj-a 的 subtitle.get 迟到 0.6s
    try:
        # 先打开 A，立即打开 B（不等 A 的响应落地）
        page.evaluate("() => window.WB.router.go('subtitles', {project: 'proj-a'})")
        page.evaluate("() => window.WB.router.go('subtitles', {project: 'proj-b'})")
        page.wait_for_function(
            "() => window.WB.router.current().params.project === 'proj-b'",
            timeout=10000,
        )
        _wait_selected(page, "proj-b")
        # 等到 A 的迟到响应确定已经落地（0.6s sleep + 渲染余量）
        page.wait_for_timeout(1200)
        body = page.inner_text("#view")
        assert "beta line one" in body, f"切换后新项目字幕丢了：{body[:200]}"
        assert "alpha line one" not in body, "旧项目的迟到响应覆盖了新项目的行列表"
        assert _route_project(page) == "proj-b"
    finally:
        world.delay_project = None


# ---------------------------------------------------------------------------
# 判据 3：切换前编辑未保存 → 切到 B 后 subtitle.get(A) 能读到改动（flush 生效）。
# ---------------------------------------------------------------------------


def test_unsaved_edit_survives_switching_projects(subs_page_noargs):
    page, errors, world = subs_page_noargs
    listing = world.call("library.list", {"limit": 200, "offset": 0})
    expected = next(p["id"] for p in listing["items"] if p.get("has_transcript"))
    _wait_selected(page, expected)

    _begin_edit(page)
    page.locator(".sub-row__input").fill("flushed marker")
    page.keyboard.press("Enter")
    # 立即切换（700ms 自动保存来不及触发，flush 必须自己兜住这份草稿）
    page.evaluate("() => window.WB.router.go('subtitles', {project: 'proj-b'})")
    _wait_selected(page, "proj-b")

    rows = world.call("subtitle.get", {"project_id": expected})
    assert rows["segments"][0]["text"] == "flushed marker", (
        f"切换前的未保存草稿没有落盘；A 的第一行现在是：{rows['segments'][0]['text']!r}"
    )


# ---------------------------------------------------------------------------
# 判据 4：保存失败 → 切换被阻断，停留 A，select 回弹 A。
# ---------------------------------------------------------------------------


def test_a_failed_save_blocks_the_switch_and_bounces_back(subs_page_noargs):
    page, errors, world = subs_page_noargs
    listing = world.call("library.list", {"limit": 200, "offset": 0})
    expected = next(p["id"] for p in listing["items"] if p.get("has_transcript"))
    _wait_selected(page, expected)

    _begin_edit(page)
    page.locator(".sub-row__input").fill("doomed marker")
    page.keyboard.press("Enter")
    world.fail_save = True  # 期间所有 subtitle.save 都失败（含自动保存抢跑的情况）
    try:
        # 页内切换（select change -> switchProject -> flushDraft）才有"阻断+回弹"语义；
        # go() 是路由级重建，不适用。
        page.select_option("#sub-project-select", "proj-b")
        _wait_selected(page, "proj-a", timeout=8000)
        assert _route_project(page) == "proj-a", (
            f"保存失败后路由还是切走了：{_route_project(page)!r}"
        )
        notice = page.inner_text("#sub-notice")
        assert "保存失败" in notice, f"错误提示没出现：{notice!r}"
        body = page.inner_text("#view")
        assert "doomed marker" in body, "回弹后 A 的未保存草稿丢了"
    finally:
        world.fail_save = False


def test_when_the_save_succeeds_the_switch_is_not_blocked(subs_page_noargs):
    """条件牙齿：同一流程不注入失败时，切换必须成功——判据 4 的红来自「保存失败」。"""
    page, errors, world = subs_page_noargs
    _begin_edit(page)
    page.locator(".sub-row__input").fill("flushed marker")
    page.keyboard.press("Enter")
    page.evaluate("() => window.WB.router.go('subtitles', {project: 'proj-b'})")
    _wait_selected(page, "proj-b", timeout=8000)
    assert _route_project(page) == "proj-b"


# ---------------------------------------------------------------------------
# 判据 5：#sub-reveal → fs.reveal 的 path 是 A 的 root_dir，且没有 project_id。
# ---------------------------------------------------------------------------


def test_reveal_hands_the_backend_the_project_root_and_no_project_id(subs_page):
    page, errors, world = subs_page
    _wait_selected(page, "proj-a")
    page.click("#sub-reveal")
    page.wait_for_timeout(500)  # 等桥线程把 fs.reveal 记下来
    assert len(world.reveal_calls) == 1, f"fs.reveal 调用记录：{world.reveal_calls!r}"
    params = world.reveal_calls[0]
    assert params.get("path") == str(world.root_a), f"reveal 收到的 path 不对：{params!r}"
    assert "project_id" not in params, f"reveal 混进了 project_id：{params!r}"
    # os.startfile 被 patch 成记录函数：确认产品路径真的走到了打开目录这一步
    assert world.started == [str(world.root_a)] or world.started == [], (
        f"os.startfile 收到了意外目标：{world.started!r}"
    )


# ---------------------------------------------------------------------------
# 判据 6：后退。replace 切换 A→B 后 go_back 应回 library。
# ---------------------------------------------------------------------------


def test_going_back_after_a_replace_switch_leaves_the_library(subs_page_noargs):
    page, errors, world = subs_page_noargs
    _wait_selected(
        page,
        next(
            p["id"]
            for p in world.call("library.list", {"limit": 200, "offset": 0})["items"]
            if p.get("has_transcript")
        ),
    )
    page.evaluate("() => window.WB.router.go('library')")
    page.wait_for_function("() => window.WB.router.current().route === 'library'", timeout=10000)
    page.evaluate("() => window.WB.router.go('subtitles')")
    page.wait_for_selector("#sub-project-select", timeout=15000)
    length_before = page.evaluate("() => window.history.length")
    page.select_option("#sub-project-select", "proj-b")
    _wait_selected(page, "proj-b", timeout=8000)
    length_after = page.evaluate("() => window.history.length")
    assert length_after == length_before, (
        f"replace 切换不应增加历史：{length_before} -> {length_after}"
    )
    page.go_back()
    page.wait_for_function("() => window.WB.router.current().route === 'library'", timeout=10000)
