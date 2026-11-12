"""素材库信息架构（T4-3）真浏览器验收：真 service + 真 73 条库副本。

跑法：
    HOME=C:/Users/b1596 .venv/Scripts/python.exe -m pytest tests/wb/test_library_ia_ui.py -q

夹具只读复制真实库到 Temp，不改用户库；页面走产品自己的 pywebview 桥形状
（Playwright expose_binding），不 fetch、不放宽 CSP。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="没装 playwright，T4-3 真浏览器验收没跑（不是通过）。",
)
from playwright.sync_api import sync_playwright  # noqa: E402

from video_workbench.app.service import WorkbenchService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "docs" / "screenshots" / "library-ia"
REAL_METHODS = [
    "library.list",
    "library.get",
    "library.archive",
    "library.delete_preview",
    "library.delete_many",
    "transcribe.languages",
]


def _copy_real_db(dest: Path) -> bool:
    src = Path(os.path.expanduser("~/.bili-scribe/bili-scribe.db"))
    if not src.is_file():
        return False
    source = sqlite3.connect("file:" + src.resolve().as_posix() + "?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return True


def _temp_config(tmp_path: Path) -> Path:
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("db_path:"):
            lines.append('  db_path: "%s"' % (tmp_path / "wb-ui.db").as_posix())
        elif stripped.startswith("key_path:"):
            lines.append('  key_path: "%s"' % (tmp_path / "wb-ui.key").as_posix())
        else:
            lines.append(line)
    target = tmp_path / "config.yaml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wb-library-ui")
    db = tmp_path / "wb-ui.db"
    if not _copy_real_db(db):
        pytest.skip("真实库 ~/.bili-scribe/bili-scribe.db 不存在，T4-3 没跑")
    cfg = _temp_config(tmp_path)
    svc = WorkbenchService(config_path=str(cfg))
    try:
        yield svc
    finally:
        svc.close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        launched = pw.chromium.launch(channel="chrome")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture(scope="module")
def web_base_url():
    # 产品是 file:// 加载（desktop.py:http_server=False），真窗口里的缩略图也是 file://；
    # 用 http 起页面会让 file:// 缩略图被浏览器安全策略拒载，测出来的红是夹具的，不是产品的。
    return (ROOT / "video_workbench" / "web").as_uri()


def test_library_ia_real_browser_flow(browser, web_base_url, service):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors = []
    page.on("pageerror", lambda exc: errors.append("pageerror: %s" % exc))
    page.on(
        "console",
        lambda msg: (
            errors.append("console: %s" % msg.text)
            if msg.type == "error" and "favicon" not in (msg.text or "")
            else None
        ),
    )

    def bridge(source, payload):
        method = (payload or {}).get("method") or ""
        try:
            return service.dispatch(method, (payload or {}).get("params") or {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"message": "%s: %s" % (type(exc).__name__, exc)}}

    page.expose_binding("__wbRpc", bridge)
    # 走产品自己的 pywebview 桥形状：不请求 ?mock=1，页面启动时就能看到桥，
    # WB.isMock() 必须为 false。这样截图里的数字只能来自 service，不可能来自 mock.js。
    page.add_init_script(
        "window.pywebview = { api: { rpc: function (method, params) {"
        " return window.__wbRpc({method: method, params: params || {}}); } } };"
    )
    page.goto(web_base_url + "/index.html", wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    assert page.evaluate("() => window.WB.isMock()") is False, (
        "页面降级成了 mock，截图不算真 service"
    )
    # 真实 service 首次启动会弹引导层；按产品自己的「跳过」按钮关掉（AGENTS.md 第四节）。
    if page.locator("#onb-overlay").count():
        page.locator("#onb-skip").click()
        page.wait_for_selector("#onb-overlay", state="hidden", timeout=5000)
    page.evaluate("() => window.WB.router.go('library')")
    page.wait_for_selector(".lib-tree", timeout=15000)
    page.wait_for_selector(".lib-list", timeout=15000)

    # 1) 两级树：点第一个合集组 = 只筛这个合集，并把组展开
    groups = page.locator(".lib-tree__group")
    assert groups.count() >= 1, "左栏没有合集组"
    groups_before = groups.count()
    preferred = page.locator('.lib-tree__group[data-collection="2026正大杯备赛"]')
    first_group = preferred.first if preferred.count() else groups.first
    collection_title = first_group.get_attribute("data-collection")
    assert collection_title, "第一个组不是合集组，无法验收来源筛选"
    collection_data = (
        service.dispatch("library.list", {"collection": collection_title, "limit": 200}).get("data")
        or {}
    )
    collection_total = int(collection_data.get("total") or 0)
    assert collection_total > 0, "真实库副本里这个合集是空的，无法断言"
    first_group.click()
    page.wait_for_selector(".lib-tree__children", timeout=5000)
    assert "is-active" in (first_group.get_attribute("class") or ""), "点击合集后组头没有高亮"
    assert "collection=" in page.url, "来源筛选没有写进 URL：%s" % page.url
    head = page.locator(".list__head").inner_text()
    assert ("%d / %d" % (collection_total, collection_total)) in head, (
        "合集筛选后的列表计数不对：%s" % head
    )
    all_chip = page.locator(".lib-chip", has_text="全部").first
    assert str(collection_total) in all_chip.inner_text(), (
        "来源筛选后「全部」chip 的总数没有跟着收窄：%s" % all_chip.inner_text()
    )
    page.screenshot(path=str(SHOTS / "01-tree-expanded.png"), full_page=True)

    # 1b) 来源内再点一个非零阶段 chip：数字必须来自该来源的 facets
    collection_facets = collection_data.get("facets") or []
    scoped_facet = next(
        (facet for facet in collection_facets if int(facet.get("count") or 0) > 0),
        None,
    )
    assert scoped_facet is not None, "合集内没有非零阶段，无法验收来源内阶段筛选"
    page.locator(".lib-chip", has_text=scoped_facet["label"]).first.click()
    page.wait_for_timeout(350)
    head = page.locator(".list__head").inner_text()
    want = "%d / %d" % (int(scoped_facet["count"]), int(scoped_facet["count"]))
    assert want in head, "来源内阶段筛选后的计数不对：期望 %s，实际 %s" % (want, head)
    page.screenshot(path=str(SHOTS / "02-source-stage-filter.png"), full_page=True)
    page.locator(".lib-chip", has_text="全部").first.click()
    page.wait_for_timeout(250)

    # 1c) 全部项目 = 清除来源，列表回到全库
    page.locator(".tree__row", has_text="全部项目").first.click()
    page.wait_for_timeout(350)
    assert "is-active" not in (first_group.get_attribute("class") or ""), (
        "点「全部项目」后合集组头还保持高亮"
    )
    assert "collection=" not in page.url, "清来源后 URL 还挂着 collection：%s" % page.url
    assert groups.count() == groups_before, "清来源后左栏树被裁掉了"
    global_data = service.dispatch("library.list", {"limit": 200}).get("data") or {}
    global_total = int(global_data.get("total") or 0)
    head = page.locator(".list__head").inner_text()
    assert ("%d / %d" % (global_total, global_total)) in head, (
        "点「全部项目」后没有回到全库：%s" % head
    )

    # 2) chips：点「已总结」，断言列表数量来自后端 facets
    # 期望值不许写死：夹具打的是真实库副本，行数会漂，写死就是本文件原先那条红。
    # facets 不带 stage 过滤（见 service._library_facets 的 docstring），所以「已总结」
    # 这一格的 count 既是 chips 上的数字，也是 stage 过滤后列表表头 pill 的分母。
    envelope = service.dispatch("library.list", {})
    facets = (envelope.get("data") or {}).get("facets") or []
    expected = next((f.get("count", 0) for f in facets if f.get("key") == "summarized"), 0)
    if not expected:
        pytest.skip("真实库里没有已总结的项目，chips 过滤无从断言")
    page.locator(".lib-chip", has_text="已总结").first.click()
    page.wait_for_timeout(350)
    head = page.locator(".list__head").inner_text()
    want = "%d / %d" % (expected, expected)
    assert want in head, "期望 %s，实际：%s" % (want, head)
    assert groups.count() == groups_before, "stage 过滤把左栏树裁掉了"
    page.screenshot(path=str(SHOTS / "02-chips-summarized.png"), full_page=True)

    # 3) 排序：切回全部后按 B 站合集顺序
    page.locator(".lib-chip", has_text="全部").first.click()
    page.wait_for_timeout(250)
    page.select_option(".lib-sort", "collection_index")
    page.wait_for_timeout(350)
    assert page.locator(".lib-sort").input_value() == "collection_index"
    page.screenshot(path=str(SHOTS / "03-sort-collection-index.png"), full_page=True)

    # 4) 多选：勾前三行，操作条出现「已选 3 项」
    boxes = page.locator(".lib-check__box[data-id]")
    assert boxes.count() >= 3, "列表不足三行，无法演示多选"
    for i in range(3):
        boxes.nth(i).check()
    assert "已选 3 项" in page.locator("#lib-bulkbar").inner_text()
    page.screenshot(path=str(SHOTS / "04-multi-select.png"), full_page=True)

    # 5) 删除预览：必须显示 total 与未发布草稿数，取消后不删
    page.locator("#lib-bulk-remove").click()
    page.wait_for_selector(".modal", timeout=5000)
    modal_text = page.locator(".modal__body").inner_text()
    assert "将移除 3 项" in modal_text, modal_text
    assert "未发布草稿" in modal_text, modal_text
    page.screenshot(path=str(SHOTS / "05-delete-preview.png"), full_page=True)
    page.locator(".modal .btn", has_text="取消").click()

    assert errors == [], "控制台有 error：%r" % errors
    page.close()
