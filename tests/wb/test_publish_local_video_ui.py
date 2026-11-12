"""真后端 + 真 Chromium：本地项目在发布卡填 BV 号后「账号与视频」放行。

这是「本地导入项目无法发布笔记」的验收面：不是测 mock 文案，而是让真
WorkbenchService.dispatch 通过 pywebview 同形状的 bridge 接到页面，点「从总结生成」
→ 填 #pb-bvid → 保存草稿 → 后端 wb_project.publish_bvid 真的落库，门禁卡不再说
「这个项目没有 BV 号」。CSP 的 connect-src 'none' 决定了不能用 fetch，理由同
tests/wb/test_summary_real_backend_ui.py 文件头。
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

from test_settings_role_warning_ui import ACCEPT_DIR  # noqa: E402
from test_settings_role_warning_ui import browser as browser  # noqa: E402,F401
from test_settings_role_warning_ui import web_base_url as web_base_url  # noqa: E402,F401

SUMMARY_CONTENT = {
    "one_line": "本地视频也能发笔记",
    "summary_md": "## 正文\n这一期讲本地视频发布。",
    "key_points": ["填视频号", "保存草稿"],
    "timeline": [{"t": "00:08", "text": "开场"}],
    "model": "deepseek-chat",
    "role": "standard",
}

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

REAL_METHODS = frozenset(
    {
        "library.list",
        "publish.preview",
        "publish.note",
        "publish.list",
        "publish.get",
        "publish.records",
    }
)


class _Item:
    def __init__(self, bvid: str, aid: int):
        self.bvid = bvid
        self.aid = aid


class _Resolution:
    def __init__(self, bvid: str, aid: int):
        self.items = [_Item(bvid, aid)]


class _Resolver:
    def resolve(self, raw: str):
        text = str(raw or "").strip()
        if text in ("BV1xx411c7mD", "av778899"):
            return _Resolution("BV1xx411c7mD", 778899)
        raise AssertionError(f"unexpected resolver input: {text!r}")


class _Gateway:
    def is_logged_in(self):
        return True

    def client(self):
        return None


def _make_project(service, tmp_path: Path):
    root = tmp_path / "media" / "本地素材"
    root.mkdir(parents=True, exist_ok=True)
    (root / "transcript.json").write_text("[]", encoding="utf-8")
    project = service.repo.project_create(
        title="本地素材：讲座录像.mp4",
        source_kind="local",
        bvid=None,
        aid=None,
        root_dir=str(root),
        stage="summarized",
    )
    service.repo.summary_insert(
        project["id"],
        content=SUMMARY_CONTENT,
        content_md="# 总结",
        role="standard",
    )
    return project


def _open_publish_card(browser, web_base_url: str, service):
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

    def bridge(source, payload):
        method = payload.get("method") or ""
        try:
            return service.dispatch(method, payload.get("params") or {})
        except Exception as exc:  # noqa: BLE001 - 桥不能把页面拖死
            return {
                "ok": False,
                "request_id": None,
                "error": {"message": f"{type(exc).__name__}: {exc}"},
            }

    page.expose_binding("__wbRpc", bridge)
    page.goto(web_base_url.split("#")[0], wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    page.evaluate(BRIDGE_JS, {"methods": sorted(REAL_METHODS)})
    page.evaluate(
        """() => {
          const main = document.createElement('div');
          main.id = 'publish-test-main';
          main.style.width = '860px';
          document.body.appendChild(main);
          const rail = document.createElement('div');
          rail.id = 'publish-test-rail';
          rail.style.width = '320px';
          document.body.appendChild(rail);
          return WB.pages.publish.mount({main: main, rail: rail});
        }"""
    )
    page.wait_for_selector("#pb-project", timeout=15000)
    return page, errors


def test_local_project_publish_card_takes_bv_and_unblocks_account(
    browser, web_base_url, service, tmp_path
):
    service._gateway = _Gateway()
    service._resolver = _Resolver()
    project = _make_project(service, tmp_path)

    page, errors = _open_publish_card(browser, web_base_url, service)
    try:
        page.get_by_role("button", name="从总结生成").click()
        page.wait_for_selector("#pb-bvid", timeout=15000)
        assert page.locator("#pb-aid").is_visible(), "AV 输入栏没有渲染"

        page.fill("#pb-bvid", "BV1xx411c7mD")
        page.get_by_role("button", name="保存草稿").click()
        page.wait_for_selector("#pb-note", timeout=15000)

        row = service.repo.project_get(project["id"])
        assert row["publish_bvid"] == "BV1xx411c7mD", row
        assert row["bvid"] is None and row["aid"] is None, "不能把发布视频号写进下载身份列"
        assert page.locator(".pb-gate").inner_text().find("这个项目没有 BV 号") == -1
        assert "视频 BV 号 BV1xx411c7mD" in page.locator(".pb-gate").inner_text()
        assert not errors, "页面报错了：\n" + "\n".join(errors[:10])
        ACCEPT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ACCEPT_DIR / "wb_publish_local_video.png"), full_page=True)
    finally:
        page.close()
