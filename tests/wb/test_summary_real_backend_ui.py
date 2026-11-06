"""真后端 + 真 Chromium：设置页那行提示词来源是不是**后端算出来的**（task-21 判据 1–4）。

## 为什么要有这个文件

`test_settings_role_warning_ui.py` 那批走的是 `?mock=1` + stub 注入，所以它测不到
「后端真的这么算吗」。lead 的判据 1–4 要的就是这一层：真 `WorkbenchService`、真注册表、
真 `prompt_source` 计算，前端只当读者。

## 接缝（都现读过，不是猜的）

- `video_workbench/desktop.py:142` `bridge.rpc = self._service.dispatch(method, params, request_id)`
  ⇒ 业务入口不依赖窗口。
- `video_workbench/desktop.py:254 _check()` 里 `WorkbenchService(config_path=...)` 是产品自带的
  「不开窗口只装配」路径，CI 用它，所以本文件照它装配。
- `dispatch()` 返回信封 `{ok, data, request_id}`（失败时多一个 `error`），前端 `rpc.js` 取的是
  `data`——本文件的回环把这层信封原样透给页面，别让测试自己拼形状。

## 隔离（lead 要求落进用例，不是写注释）

- 配置：从仓库 `config.yaml` 复制一份到 Temp，只把 `storage.db_path` / `storage.key_path`
  改成 Temp 里的文件；**notes-cache 跟着 db 走**（`config.py:650 notes_dir = db 同级/notes-cache`），
  所以磁盘缓存也不会碰到用户的。
- 证据：用例真的拿后端值比，判据 1 那条若前端改成硬编码就会红（牙齿见 test_a_poisoned_backend_sha...）。

## 判据 3 的可观测面（与合同措辞的偏差，报过 lead）

合同写「`summary.roles` 的 system 长度随之变化」。实测 `summary.roles` 的每一行只有
`key/name/tagline/passes/prompt_source/prompt_bytes/prompt_sha8` 七个字段，**不返回 system 正文**
（`core/summarizer.py:1094-1108 roles_view`）。所以这条改成钉三个真能观测的量：
页面点保存 → 后端值翻转 → Temp 目录里的配置文件真变了 → `extra_prompt_on(cfg, None)` 变 True
→ `system_for` 的开/关差值正好等于附加提示词全文 + 1 个换行。

## 页面这一侧怎么接（这里偏离了合同字面，理由写在下面）

合同原话是「测试进程里起一个只绑 127.0.0.1 的回环 HTTP 服务，页面 `fetch /rpc`」。实测走不通，
而且不通的原因是产品自己的安全策略：`video_workbench/web/index.html:13-14` 的 CSP 写着
`connect-src 'none'`，`:9` 的注释就写明「页面**不做任何 fetch/XHR**，所有网络动作都在 Python 侧」。
浏览器会拒掉任何 fetch（同源也一样），报 `violates the following Content Security Policy directive:
"connect-src 'none'"`。⇒ 要么放宽产品 CSP（那是改产品安全边界，不在我写范围，也不该为测试开），
要么照产品本来的运输形状来。

产品本来的形状是 pywebview 注入的 JS↔Python 桥：`desktop.py:142` `bridge.rpc = self._service.dispatch(...)`。
本文件用 Playwright 的 `expose_binding` 注入一个同样把调用直接交给 `dispatch` 的桥函数，
其余一字不改：真 service、真注册表、真 `prompt_source` 计算、真 web 目录、真 Chromium。
`test_the_product_page_forbids_its_own_http_calls` 常驻钉着「盘上与浏览器里的 CSP 都还是 'none'」，
所以这条替代不是绕过，是复刻。

首屏仍用 `?mock=1` 起来（避开 11 处 `var WB = (window.WB = window.WB || {})` 反复重装陷阱，
原因写在 `test_settings_role_warning_ui.py` 文件头），起来之后把 `WB.rpc` 整包换成桥。
**只有与判据相关的四个方法**（`summary.roles` / `summary.prompt.list` / `app.settings.get` /
`app.settings.put`）走真 service，其余留在 mock——否则首屏别的页面会因缺 pywebview 桥报错，
而那条报错与本合同无关。这个取舍写清楚：**本文件测的是设置页的数据来源，不是整站连通性。**
"""

from __future__ import annotations

import hashlib
import json
import tempfile
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

# 皮肤切换、折叠组定位、几何测量这些判据与 helper 都从 mock 那批复用，
# 两处对「看得见」的定义必须只有一份（否则两边各写一套、各错各的）。
from test_settings_role_warning_ui import (  # noqa: E402
    ACCEPT_DIR,
    SKINS,
    adv_group,
    apply_skin,
    open_settings,  # noqa: F401  （mock 版入口，本文件不用，只借 helper）
    require_expanded,  # 读折叠组里的文字之前必须先展开（收起态 inner_text 恒空）
    role_row,
    warn_line,
)
from test_settings_role_warning_ui import browser as browser  # noqa: E402,F401  （pytest fixture）
from test_settings_role_warning_ui import web_base_url as web_base_url  # noqa: E402,F401  （pytest fixture：静态服务真实 web 目录）

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import prompt_source as ps  # noqa: E402
from video_workbench.core import summarizer as ad  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "video_workbench" / "web"

#: 只有这四个方法接到真后端，其余留 mock（理由见文件头「页面这一侧怎么接」）。
REAL_METHODS = frozenset(
    {
        "summary.roles",
        "summary.prompt.list",
        "app.settings.get",
        "app.settings.put",
    }
)

#: 注入到页面里的包装：`WB.rpc` → 外置桥函数 `window.__wbRpc`。返回 `res["data"]`，
#: 与前端 `rpc.js:108` 取的那一层一致。
#:
#: **为什么不是 fetch 打回环 HTTP**：产品的 `index.html:13-14` 写了
#: `connect-src 'none'`（`:9` 的注释就是「页面不做任何 fetch/XHR，所有网络动作都在 Python 侧」），
#: 浏览器会拒掉任何端口上的请求（同源的也一样）。真桌面里走的是 pywebview 注入的
#: `window.api.rpc`（`desktop.py:142` 把它接到 `service.dispatch`），`expose_binding` 注入的
#: 桥与它是同一种形状——所以这里不是「绕过产品约束」，是照产品的运输方式复刻一条。
#:
#: 不改 `WB.isMock()`：legacy 内部自己记着 mock 开关，未接的四个方法照旧走 mock，
#: 把 isMock 翻成 false 只会让别的页面分支走到我没验过的路上（最小干预）。
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


def _source_help(page):
    """那一行真源清单节点（`settings.js:328-343 describeRoleSources` 写进去的那个）。

    `llm.role` 这一行里其实有**两个** `.field__help`：字段自己的说明（静态文案）和真源清单。
    DOM 顺序是 label / control / field.help / src / warn（`settings.js:1603-1609`），所以清单是
    **最后一个**。取 `.first` 会读到静态说明——实测报错长这样：期望 `xiezuo ← 写作专家.txt ·
    6941 B · 6b079cf4`，拿到「全部角色走三轮改写（生成→自我批判→修订）……」。
    下面 count==2 那条是钉住「这行确实只有这两个 help，最后一个就是清单」；将来产品加了第三个
    help，这条会红着提醒重新对位，而不是静默读到别的节点。
    """
    helps = role_row(page).locator(".field__help")
    assert helps.count() == 2, (
        f"`llm.role` 那行的 .field__help 数量变了（现在 {helps.count()} 个），"
        "真源清单的位置需要重新核对，别继续用 .last 猜"
    )
    return helps.last


def _temp_config(tmp_path: Path) -> Path:
    """复制仓库配置到 Temp，只把 db / key 指到 Temp。

    不直接写一份最小配置：真实用户的配置里有 provider/apiKey 之类，判据 2 要看的是
    「用户实际那份配置翻转了吗」，用最小程序跑出来的绿灯不代表真实路径。
    """
    src = ROOT / "config.yaml"
    text = src.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("db_path:"):
            lines.append('  db_path: "%s"' % (tmp_path / "wb-e2e.db").as_posix())
        elif stripped.startswith("key_path:"):
            lines.append('  key_path: "%s"' % (tmp_path / "wb-e2e.key").as_posix())
        else:
            lines.append(line)
    target = tmp_path / "config.yaml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class _Service:
    """真 `WorkbenchService` 的薄壳：`call()` 拆信封并**让失败当场红**。

    不走 HTTP 回环（理由见 `BRIDGE_JS` 上方注释），直接把 `dispatch` 当桥函数暴露给页面，
    和桌面版把 `window.api.rpc` 接到同一个函数是同一形状。
    """

    def __init__(self, svc: WorkbenchService, config_path: Path):
        self.svc = svc
        self.config_path = config_path
        #: 页面经桥打过来的方法名，用来证明「这条断言确实读了后端而不是读了 mock」。
        self.bridge_calls: list[str] = []

    def dispatch(self, method: str, params=None) -> dict:
        return self.svc.dispatch(method, params or {})

    def call(self, method: str, params=None):
        envelope = self.dispatch(method, params)
        if not envelope.get("ok"):
            raise AssertionError(f"{method} 失败：{envelope.get('error')}")
        return envelope["data"]

    def shutdown(self) -> None:
        self.svc.close()


@pytest.fixture()
def real_service(tmp_path):
    """真 service + Temp 配置；teardown 一定 `close()`（lead 硬要求）。"""
    cfg_path = _temp_config(tmp_path)
    handle = _Service(WorkbenchService(config_path=str(cfg_path)), cfg_path)
    try:
        yield handle
    finally:
        handle.shutdown()


def _open_real(browser_ctx_factory, web_base: str, service: _Service):
    """开页面：外置桥 → mock 起首屏 → 把四个方法换成真 service → 进 设置→模型。"""
    page = browser_ctx_factory.new_page(viewport={"width": 1440, "height": 960})
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
        service.bridge_calls.append(method)
        try:
            return service.dispatch(method, payload.get("params") or {})
        except Exception as exc:  # noqa: BLE001 - 桥不能把页面拖死，错误照产品的信封回
            return {
                "ok": False,
                "request_id": None,
                "error": {"message": f"{type(exc).__name__}: {exc}"},
            }

    page.expose_binding("__wbRpc", _bridge)
    page.goto(f"{web_base}/index.html?mock=1", wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    page.evaluate(BRIDGE_JS, {"methods": sorted(REAL_METHODS)})
    page.evaluate("() => window.WB.router.go('settings', {section: 'llm'})")
    try:
        page.wait_for_selector("label.field__label", timeout=15000)
    except PlaywrightTimeoutError as exc:
        # 不带上页面自己报的错，这条超时看起来就只是「界面慢」，谁也分不清是没渲染、
        # 还是运输被拒（CSP / CORS 那两次就是靠这里攒下的报错定位的）。
        raise AssertionError(
            f"设置页没渲染出字段：{exc}\n页面期间收集到的错误：\n"
            + "\n".join(errors[:12])
            + "\n桥收到的方法："
            + repr(service.bridge_calls[:12])
        ) from None
    return page, errors


@pytest.fixture()
def real_page(browser, web_base_url, real_service):
    """(page, errors, backend, cfg_path) —— 页面已经走进 设置→模型。"""
    try:
        page, errors = _open_real(browser, web_base_url, real_service)
    except PlaywrightError as exc:
        if "chromium" not in str(exc).lower():
            raise
        pytest.skip(f"Chromium 起不来，界面验收没跑（不是通过，是没跑）：{exc}")
    try:
        yield page, errors, real_service, real_service.config_path
    finally:
        page.close()


# ================================================================ 判据 1：来源清单


def test_the_source_line_shows_what_the_backend_computed(real_page):
    """判据 1：设置页那行真源显示的文件名/字节/sha8 **逐字**等于后端 `summary.prompt.list`。

    这条同时否定两种假实现：前端自己算 sha（后端改了不跟着变）、mock 写死 null
    （`mock.js:1530-1536` 只带键的形状，值全空）。真读到后端的证据是下面把 sha 改错那条。
    """
    page, errors, backend, _ = real_page
    listed = backend.call("summary.prompt.list")["prompts"]
    rows = {row["bound_role"]: row for row in listed if row.get("bound_role")}
    # 名单**从 `prompt_source.MAP` 现读**，不写死：task-24 把 `shipin` 绑上了
    # `视频总结.txt`，写死四个到那天就会红——而红的是我的常量过期，不是产品坏。
    # 后端返回的绑定集合必须**正好等于** MAP：多一个说明有人绕过 MAP 接了文件，
    # 少一个说明 MAP 里那一项在清单层丢了（设置页就不会显示它）。
    assert set(rows) == set(ps.MAP), (
        f"后端认的绑定角色集合与 MAP 不一致：清单 {sorted(rows)} vs MAP {sorted(ps.MAP)}"
    )

    require_expanded(page, "真源清单在折叠组里，收起态读到的 inner_text 是空串")
    help_text = _source_help(page).inner_text()
    for key, row in rows.items():
        expected = f"{key} ← {row['name']} · {row['bytes']} B · {row['sha256'][:8]}"
        assert expected in help_text, (
            f"来源清单里没有后端的这一行 {expected!r}；页面显示的是：{help_text!r}"
        )
    # 无源角色必须说人话，不许留空
    for key in ps.NO_SOURCE:
        assert f"{key} ←" in help_text, f"无源角色 {key} 在清单里消失了：{help_text!r}"
    assert errors == [], f"控制台有 error：{errors}"
    page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_t21_sources.png"))


def test_a_poisoned_backend_sha_shows_up_in_the_page(real_page):
    """**会红的证据**（lead 要求）：后端 sha 被改错时页面跟着变——证明它真读后端。

    poison 打在哪一层是现读出来的，不是猜的：`summary.roles` → `summarizer.roles_view()`
    迭代的是 **import 期算好的 `ROLES` 快照**（`core/summarizer.py:1094-1108` +
    `core/summary/__init__.py:86-105 describe_roles`），所以 patch `prompt_source.info_for`
    对它没影响（第一版就打错了层，报「后端这层就没吃到 poison」）。这里直接改 `ROLES` 里
    wengao 那一行，测完恢复——改的是测试进程里的对象，产品文件一字节未动。

    顺带记下这个形状带给用户的后果：**运行中改 txt，界面那行 sha 不会跟着变**，要重启才更新
    （`prompt_source` 的 `_VIEW_CACHE` 与 `bind()` 同样只在装配时跑一次）。这是产品事实，
    本条不把它钉成「应该实时更新」，也不钉成缺陷——只报给 lead。

    若前端是硬编码或自己算 sha，这条会拿不到 poison 值而红；那时红的是前端，不是产品坏。
    """
    page, errors, backend, _ = real_page
    row = [r for r in ad.ROLES if r["key"] == "wengao"][0]
    original = dict(row)
    assert original["prompt_source"], "前置不成立：wengao 应当有 txt 真源"
    row["prompt_sha8"] = "deadbeef"
    row["prompt_bytes"] = int(original["prompt_bytes"]) + 7
    try:
        again = backend.call("summary.roles")["roles"]
        wengao = [r for r in again if r["key"] == "wengao"][0]
        assert wengao["prompt_sha8"] == "deadbeef", "后端这层就没吃到 poison，测试无效"

        page.evaluate("() => window.WB.router.go('settings', {section: 'general'})")
        page.evaluate("() => window.WB.router.go('settings', {section: 'llm'})")
        page.wait_for_timeout(250)
        require_expanded(page, "真源清单在折叠组里，收起态读到的 inner_text 是空串")
        help_text = _source_help(page).inner_text()
        assert "deadbeef" in help_text, (
            f"后端 sha 改了、页面却还是旧值——前端不是读者：{help_text!r}"
        )
        assert "wengao ← 文稿编辑.txt · %d B · deadbeef" % wengao["prompt_bytes"] in help_text, (
            f"字节数没跟着动：{help_text!r}"
        )
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        row.clear()
        row.update(original)
        assert row["prompt_sha8"] == original["prompt_sha8"], "poison 没恢复干净"


def test_every_prompt_file_the_backend_lists_has_exactly_one_owner(real_page):
    """判据 4（翻正）：清单里**每一篇** txt 都必须有且只有一个归属，不许「既没绑也没说明」。

    原合同这条钉的是「`视频总结.txt` 未绑定时的显示形态」，`UNBOUND` 当时是它一个。
    task-24 把它绑给了 `shipin`，`UNBOUND` 现在是空 —— 按字面留着「未绑文件应有 1 篇」
    就等于把一条**已过期的常量**当合同。这里保留这条用例真正要守的不变量，
    并让它对下一篇新 txt 依然有效：

    1. **归属穷尽**：每篇文件必须满足「绑给某个角色」/「是附加提示词」/「明确在未绑清单里」
       三者之一。出现第四种（后端列了个没人认领也没说不认的文件）就红——那正是当初
       合同禁止的形态：用户以为改了它有用，其实没生效。
    2. **归属互斥**：同一篇不许既算 `is_extra` 又算成某角色的真源（那会双份进请求）。
    3. **`in_map` 与生产者语义一致**：`in_map=True` 当且仅当「它绑在 `MAP` 的某个角色上
       **或**它就是附加提示词」（`prompt_source.py:219` 那一行的字面语义）。
       设置页读的就是这个布尔值，它撒谎界面就说谎 —— 但**语义要以那行代码为准**，
       不是以我对字段名的猜测为准（上一轮就猜错过一次，见下面那条断言的注释）。
    4. **页面与归属必须同真相（正反两半）**：MAP 里每篇真源都要在页面上以「← 文件名」出现；
       而「某某 没绑定」这句只许用在真的躺在 `UNBOUND` 里的文件上。
       正向那一半同时钉住了后端的 `bound_role` 字段 —— 它撒谎时 `describeRoleSources()`
       会把那行写成「无 txt 真源」，页面立刻对不上 MAP。
    """
    page, errors, backend, _ = real_page
    listed = backend.call("summary.prompt.list")["prompts"]
    assert listed, "后端一篇都没列，下面的归属检查会空转"

    unbound_names = set(ps.UNBOUND)
    for row in listed:
        name = row["name"]
        bound = bool(row.get("bound_role"))
        extra = bool(row.get("is_extra"))
        assert bound or extra or name in unbound_names, (
            f"{name} 既没绑给角色、也不是附加提示词、又不在未绑清单里——"
            f"用户改它不会有任何效果，界面也没地方说明它"
        )
        assert not (bound and extra), f"{name} 同时是某角色的真源和附加提示词：会双份进请求"
        # `in_map` 的语义以**生产者**为准（`prompt_source.py:219`）：
        # `"in_map": name in bound_by or name == EXTRA_FILE` —— 附加提示词也算「在映射里」，
        # 因为它确实会进请求。我上一轮把它写成「in_map 当且仅当它在 MAP 的 values 里」，
        # 那是**按猜测定义别人的字段**，于是 `大概率管用 .txt` 红了一条我自己写错的期望值。
        # 这条不是放宽：它把断言挪回去对准那行代码真正表达的关系。
        expected_in_map = name in set(ps.MAP.values()) or name == ps.EXTRA_FILE
        assert row["in_map"] is expected_in_map, (
            f"{name} 的 in_map={row['in_map']} 与生产者语义不符"
            f"（绑给角色：{bool(row.get('bound_role'))}；是附加提示词：{extra}；"
            f"EXTRA_FILE={ps.EXTRA_FILE!r}）"
        )
        if bound:
            assert ps.MAP.get(row["bound_role"]) == name, (
                f"{name} 被算成 {row['bound_role']} 的真源，MAP 里那角色绑的是别篇"
            )

    require_expanded(page, "真源清单在折叠组里，收起态读到的 inner_text 是空串")
    help_text = _source_help(page).inner_text()
    # 正向那一半：MAP 里每一篇真源都必须在页面上以「← 文件名」露出来。
    # 这一半同时把后端 `bound_role` 那个字段钉住了 —— 它要是撒谎（把有主的说成没主），
    # `describeRoleSources()` 就会把那行写成「无 txt 真源」，这里立刻红。
    for name in set(ps.MAP.values()):
        assert f"← {name}" in help_text, (
            f"页面上看不到 {name} 的真源行（MAP 说它有人认领）：{help_text!r}"
        )
    # 反向那一半：只有真的在 `UNBOUND` 里的文件才许被写成「没绑定」。
    # `UNBOUND` 现在是空的（task-24 把最后一篇绑给了 shipin），所以这一半平时沉默，
    # 但**不是恒真**：谁把有主的文件塞进未绑文案，它马上红。
    # 原合同那句「未绑文件应有 1 篇」已经把那个 1 用掉了，我不再重钉已过期的常量。
    for row in listed:
        if f"{row['name']} 没绑定" in help_text:
            assert row["name"] in unbound_names, (
                f"页面把 {row['name']} 说成没绑定，后端却认它有归属 —— "
                "用户改了它以为不生效，其实生效：{!r}".format(help_text)
            )
    assert errors == [], f"控制台有 error：{errors}"
    page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_t25_owners.png"))


# ============================================================ 判据 2/3：附加提示词开关


def test_the_switch_initial_state_comes_from_the_backend(real_page):
    """判据 2：开关初始态 == 后端 `app.settings.get` 的 `llm.extra_prompt`。

    反面对照也要：后端是 False 时页面必须是「关」，否则初始态是从 mock 或 DOM 默认来的。
    """
    page, errors, backend, _ = real_page
    value = backend.call("app.settings.get")["llm"]["extra_prompt"]
    assert value is False, f"本用例的前置：配置应当是关，实得 {value!r}"
    switch = (
        page.locator("details.adv button.switch").filter(has=page.locator("text=附加提示词")).first
    )
    if switch.count() == 0:
        switch = page.locator("button.switch").first
    state = switch.get_attribute("aria-checked")
    assert state == ("true" if value else "false"), (
        f"开关初始态与后端不一致：后端 {value}，aria-checked={state!r}——"
        "页面自己决定默认值 = 用户看到的和存的不是一回事"
    )
    assert errors == [], f"控制台有 error：{errors}"


def test_clicking_and_saving_flips_the_backend_value_and_the_effective_system(real_page):
    """判据 3：点开关并保存 → Temp 配置真的翻转，且**生效的 system 长度随之变**。

    三段都要，少一段就是假绿：
    1. 页面点开关 + 点保存（走真 `app.settings.put`）；
    2. Temp 配置文件里 `llm.extra_prompt` 变 True（真落盘，不是只在内存）；
    3. `extra_prompt_on(cfg, None)` 从 False 变 True，于是 `system_for` 长出来的那份
       正好等于 extra 全文长度 + 1 个换行——把「开关到模型」这条链钉死。
    """
    page, errors, backend, cfg_path = real_page
    before = backend.call("app.settings.get")["llm"]["extra_prompt"]
    assert before is False

    # 这一组在配置干净时是**收起**的（脏值才会被自动展开）。不展开就点，Playwright 会一直
    # 等「元素可见」直到超时——报错长得像「开关点不动」，实际是没展开。task-20 的判据在这里同样成立。
    require_expanded(page, "附加提示词开关在折叠组里，收起态点不到")
    row = page.locator("label.field__label").filter(has_text="附加提示词").first
    field = row.locator(
        "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' field ')][1]"
    )
    switch = field.locator("button.switch").first
    switch.click()
    assert switch.get_attribute("aria-checked") == "true", "点完开关 aria-checked 没翻"

    save = page.locator("button").filter(has_text="保存").last
    save.click()
    page.wait_for_timeout(400)

    after = backend.call("app.settings.get")["llm"]["extra_prompt"]
    assert after is True, f"页面保存后后端值没翻转：{after!r}"
    written = _value_in_files(cfg_path.parent)
    assert written is True, f"配置文件里读不到 extra_prompt=True（实际值 {written!r}）——落盘缺口"

    assert ad.extra_prompt_on(backend.svc.cfg, None) is True, "配置翻转了但取值函数仍关"
    base = ps.load(ps.MAP["wengao"]).strip()
    off_len = len(ps.system_for("wengao", base, with_extra=False))
    on_len = len(ps.system_for("wengao", base, with_extra=True))
    assert on_len > off_len, f"开关打开后 system 长度反而没变：{off_len} → {on_len}"
    assert on_len - off_len == len(ps.extra_text().strip()) + 1, (
        f"多出来的不是整份附加提示词：差 {on_len - off_len}，"
        f"extra 正文 {len(ps.extra_text().strip())} 字"
    )
    assert errors == [], f"控制台有 error：{errors}"
    page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_t21_switch_saved.png"))


def _value_in_files(config_dir: Path):
    """配置真写到了盘上哪一份都行（yaml 或 workbench.json），但值必须是 True。"""
    for name in ("config.yaml", "workbench.json"):
        candidate = config_dir / name
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8")
        if name.endswith(".json"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if "extra_prompt" in json.dumps(data):
                return bool(data.get("llm", {}).get("extra_prompt"))
        else:
            for line in text.splitlines():
                if line.strip().startswith("extra_prompt:"):
                    return line.split(":", 1)[1].strip().lower() in ("true", "yes", "1")
    return None


# ================================================================ 双皮肤（真后端下）


@pytest.mark.parametrize("skin", SKINS)
def test_the_danger_line_is_readable_on_both_skins_with_a_real_backend(
    browser, web_base_url, real_service, skin
):
    """双皮肤 × 真后端：脏值那行红字在 ark 与 exhibition 都必须看得见、滚得到。

    后端给的 hint/脏值这次是**真的** `configured_unrecognized`——配置里写 standard。
    判据沿用 task-20 的两条写法（先展开再断言可见），这里额外断言颜色就是主题自己的
    danger（两套皮肤两个值），不硬钉 rgb。
    """
    # 真配置里写一个退役角色：走产品自己的保存路径，不碰文件解析
    flipped = real_service.dispatch("app.settings.put", {"values": {"llm": {"role": "standard"}}})
    assert flipped.get("ok"), f"前置不成立：写脏值失败 {flipped.get('error')}"

    page, errors = _open_real(browser, web_base_url, real_service)
    try:
        apply_skin(page, skin)
        page.evaluate("() => window.WB.router.go('settings', {section: 'general'})")
        page.evaluate("() => window.WB.router.go('settings', {section: 'llm'})")
        page.wait_for_timeout(300)

        info = real_service.call("summary.roles")
        assert info["configured_unrecognized"] == "standard", info["configured_unrecognized"]
        line = warn_line(page)
        assert line.count() == 1 and line.is_visible(), (
            f"{skin} 皮肤下脏值红字没挂出来或看不见：{info['configured_unrecognized']!r}"
        )
        assert line.inner_text() == info["configured_hint"], (
            f"页面文案不是后端那句原话：{line.inner_text()!r} vs {info['configured_hint']!r}"
        )
        group = adv_group(page)
        assert group.get_attribute("open") is not None, f"{skin} 下折叠组没自动展开"
        style = line.evaluate(
            """el => {
                 const cs = getComputedStyle(el);
                 const probe = document.createElement('span');
                 probe.style.color = 'var(--danger)';
                 el.appendChild(probe);
                 const danger = getComputedStyle(probe).color;
                 probe.remove();
                 return {color: cs.color, danger: danger, border: cs.borderLeftWidth};
               }"""
        )
        assert style["color"] == style["danger"], (skin, style)
        assert style["border"] == "3px", (skin, style)
        options = role_row(page).locator("option").evaluate_all("els => els.map(e => e.value)")
        assert "standard" not in options, options
        assert errors == [], f"控制台有 error：{errors}"
        page.screenshot(path=str(ACCEPT_DIR / f"wb_settings_role_t21_dirty_{skin}.png"))
    finally:
        page.close()


def test_the_product_page_forbids_its_own_http_calls(real_page):
    """守门：产品自己的 CSP 仍然写着 `connect-src 'none'`，本文件**没有**为了让桥通而放宽它。

    这条不是装饰：运输方式一旦有人改（把 'none' 放开、或给测试特判一个白名单），页面就多了
    一条产品没设计过的出站路径。同时它也解释了为什么桥必须是注入的绑定函数：
    实测页面里任何 fetch（含同源）都会被这份 CSP 直接拒（报错见 task-21 交付记录）。
    """
    page, _errors, _backend, _cfg = real_page
    declared = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "connect-src 'none'" in declared, "产品页面的 connect-src 不再是 none——出站口被开了"
    in_dom = page.evaluate(
        "() => [...document.querySelectorAll('meta[http-equiv]')].map(m => m.getAttribute('content') || '').join('|')"
    )
    assert "connect-src 'none'" in in_dom, (
        "浏览器里读到的 CSP 与盘上不一致（有人注入了别的 meta？）"
    )


def test_the_real_user_database_and_config_stay_untouched(real_page):
    """隔离硬要求**落进用例**：跑完整条真后端界面验收，用户自己的库与配置一个字节都没变。

    `WorkbenchService(config_path=Temp)` 只是把「读哪份配置」指到 Temp；产品照常会在装配时
    打开它自己那套路径，所以这条不能靠注释声明，必须现场比。比的是两样：

    1. 用户库 `~/.bili-scribe/bili-scribe.db` 的 sha256 + mtime + size（本文件全程不碰它，
       Temp 配置把 `storage.db_path` 指到 Temp 的 `wb-e2e.db`）；
    2. 工作区里的 `config.yaml` 与 `workbench.json`（判据 3 点过「保存」，写只能是 Temp 那份）。

    前置不满足（用户库不存在）时明确 skip，不许把「没东西可比」当成「比过了」。
    """
    user_db = Path.home() / ".bili-scribe" / "bili-scribe.db"
    if not user_db.exists():
        pytest.skip(f"用户库不在盘上（{user_db}），这条隔离证明没法跑——不是通过，是没跑")
    watched = [user_db, ROOT / "config.yaml", ROOT / "workbench.json"]
    before = {_stat_of(p): p for p in watched if p.exists()}
    assert before, "一个可观测文件都没有，前置不成立"

    # 走一遍真的读写路径：读后端清单 + 点开关并保存（判据 3 已证它真会落盘），
    # 目的是让「没写用户文件」这条结论覆盖到有写动作的场景，而不是只测只读。
    page, errors, backend, cfg_path = real_page
    backend.call("summary.prompt.list")
    backend.call("app.settings.get")
    require_expanded(page, "开关在折叠组里")
    backend.dispatch("app.settings.put", {"values": {"llm": {"extra_prompt": True}}})
    page.wait_for_timeout(150)

    after = {_stat_of(p): p for p in watched if p.exists()}
    assert set(after) == set(before), f"观测集合变了：{sorted(set(after) ^ set(before))}"
    for stat, path in before.items():
        assert stat in after, f"{path} 的 sha/mtime/size 变了——真后端用例写到用户文件了"
    # 服务装配用的配置必须在系统 Temp 下面，而不是工作区那份（写得含糊就等于没钉）。
    assert Path(tempfile.gettempdir()).resolve() in cfg_path.resolve().parents, (
        f"服务用的配置不在系统 Temp 里：{cfg_path}"
    )
    assert cfg_path.resolve() != (ROOT / "config.yaml").resolve(), cfg_path
    assert errors == [], f"控制台有 error：{errors}"


def _stat_of(path: Path) -> tuple:
    data = path.read_bytes()
    stat = path.stat()
    return (str(path), hashlib.sha256(data).hexdigest(), stat.st_mtime_ns, stat.st_size)


def test_the_repository_web_dir_is_what_the_page_serves(real_page):
    """守门：这个文件测的必须是仓库里的产品 web 目录（同 mock 那批的理由）。"""
    page, _errors, _backend, _cfg = real_page
    origin = page.evaluate("() => window.location.origin")
    assert WEB_DIR.is_dir(), WEB_DIR
    assert origin.startswith("http://127.0.0.1:"), origin
