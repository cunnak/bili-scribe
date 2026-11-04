"""已退役角色键的可达性：生成入口、导出、界面、真库历史行。

配套产品：`video_workbench/core/summarizer.py:106-139`（`resolve_role` 的两支）、
`:1062-1073`（`to_markdown` 的角色那行）、`video_workbench/core/summary/__init__.py:205`
（`role_label`，给界面用）、`video_workbench/web/js/pages/summary.js:190-195`（`roleText`）。

## 这个文件是「退役集合等于哪三个键」的唯一 owner

`test_summary_drift.py` 与 `test_summary_parity.py` 都需要「共有段」这个概念，
它们**现读** `RETIRED_ROLES` 来推那段名单；身份信息（等于 `{standard, xueshu, shipin}`）
只在这里钉一份。这样退役一个角色时只改一处，其它文件跟着动——
本仓库已经因为「名单在四个文件里各写一份」在 task-24 红 5 条、task-31 红 15 条。

## 三种口径必须分开，它们是三件事

| 口径 | 谁负责 | 说什么 |
|---|---|---|
| 生成入口（显式提交） | `resolve_role` A 支 `:138` | 抛 `VALIDATION`：这是用户此刻的意图，悄悄改写＝替他改主意 |
| 生成入口（配置/历史兜底） | `resolve_role` B 支 `:136-137` | 回落默认角色 + 把原因写进日志：脏数据不是此刻的意图 |
| 展示 | `role_label`；页面 `adoptRole` 那句「注意」chip（`:661-666`）、`roleText`（`:190-195`） | 说人话：「学术分析（已停用）」 |
| 出处 | `to_markdown` | 按**存进库的 key** 对回那一行：`角色：xueshu（已停用）` |

**现读到的产品事实（决定界面腿钉在哪）**：单视频的版本行 meta 只有「日期 · 模型 · 字数」，
**不含角色**（`summary.js:783`）；`roleText` 只用在合集总览行（`:1034`）与总览脚注（`:1114`）。
所以界面上「历史行角色不可用」的可见形式是页头那颗「注意」chip——
句子由 `adoptRole`（`:200-219`）生成，同时带上原 key、「已停用/已经不在角色列表里」、
以及改用哪个角色。三条腿都钉（退役 / 未知 / 现役不该冒提示）。

「已停用」与「未知角色」不许并成一类：前者用户要做的是换个角色，后者是把配置里那一行改对。
所以每一层都有**两条**断言（退役键 + `zzz-not-a-role`），只写前者等于允许实现用同一个 `else` 兜住。

## 为什么历史行必须用 Temp 夹具造，而不是查真库

真库 `summary_version` 现存分布（现读）：`wengao 79 / shensi 4 / xiezuo 1 / standard 1 / fenxi 1`——
**没有 xueshu 也没有 shipin 的行**（那两个角色退役前用户没生成过）。
所以「退役键的历史行仍可读」这件事只能在 Temp 库里造行验证，交付里也如实写「夹具造」。
真库那 1 条 `standard` 真行留在 `test_the_real_database_row_for_standard_is_still_readable`
做**只读对照**（跑前跑后 sha 必须一致），它证明的是「真数据没被迁移脚本改写过」，不是「降级逻辑跑过」。

## 牙齿（三条，各自只红它该红的那条）

驱动器在 `%TEMP%` 下我自己带哨兵名的目录里做（禁碰仓库工作区文件）：

* **T-1 `RETIRED_ROLES` 改回只含 `standard`**（即 `xueshu`/`shipin` 不再登记退役）→
  必须红：本文件的身份集合那条、A/B 两支的样本角色（退役键变成现役键，行为整个翻掉）、
  以及 drift/parity 里两处**现读**共有段（现读不等于没牙：锚点是逐字节锁定的 bs 原件，
  退役少配一键会让共有段变长而公开面没变短）。
* **T-2 把 `to_markdown` 的 `if shown not in ROLE_KEYS` 改成 `if False`**（即导出永不标注）→
  只红导出的退役腿与未知腿两条；现役腿仍绿（它本来就不该标注）。
* **T-3 把 `to_markdown` 里的三元后缀并成一个 `（已停用）`**（丢掉「未知角色」那一类）→
  只红未知腿（现役腿与退役腿仍绿）。这条的存在理由就是文件头那句「两类不许并成一类」。
* **T-4 把页面 `adoptRole` 的 `why` 也并成一个「已停用」**（Temp web 副本，`WB_WEB_ROOT` 指过去）→
  只红界面那条未知腿；顺带证明界面腿读的确实是桥后面的真数据，不是 mock。
  （同族坏法还有第二种：`state.roleNotice` 只在 `state.roleRetired` 非空时才赋值——
  那会让未知键不冒提示，红的是未知腿，而退役腿仍绿。牙齿至少要有这两形，不能只坏合同点名的那一形。）

实跑读数见交付里的命令原文。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from video_workbench.core import summarizer as ad
from video_workbench.core.summary import role_label
from video_workbench.core.summary import DEFAULT_ROLE, RETIRED_ROLES, ROLE_KEYS
from video_workbench.core.summary import UnknownRole, get_role

from test_summary import GOOD, FakeCtx, StubClient, _cfg  # 同目录、无 __init__.py
from playwright._impl._errors import Error as PlaywrightError  # noqa: E402

from test_summary_real_backend_ui import (  # noqa: F401  两个 fixture 由 pytest 注入
    browser,
    web_base_url,
)
from test_summary_parity import SHORT_BODY  # noqa: F401  造历史行时复用同一份正文形状

#: 身份信息（**有意为之的决定**，不是从产品读出来的）：把这一行改成从
#: `RETIRED_ROLES` 现读，就等于让产品自己给自己出证明——退役少登记一个键，
#: 所有下游都跟着「一致地错」。所以这套用例里只有这一处允许写死三个键名。
EXPECTED_RETIRED = {"standard", "xueshu", "shipin"}

#: 未知键的样本。刻意用一个不可能被登记成退役的串，
#: 这样「它显示成（未知角色）」就只能来自 else 分支，而不是巧合。
UNKNOWN_KEY = "zzz-not-a-role"

REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"
# 仓库根：parents[0] 是 tests/wb，parents[1] 是 tests，parents[2] 才是根。
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_JS_DEFAULT = REPO_ROOT / "video_workbench" / "web" / "js"


def _retired_not_in_registry(key: str):
    """退役键在权威层必须是「认识但已停用」，不是「没听过」。"""
    with pytest.raises(UnknownRole) as exc:
        get_role(key)
    return str(exc.value)


# ------------------------------------------------------------- 1. 退役集合的身份


def test_the_retired_set_is_exactly_the_three_registered_keys():
    """`RETIRED_ROLES` 等于 `{standard, xueshu, shipin}`，权威层与适配层同一份。

    适配层不许自己攒第二份退役名单（`core/summarizer.py:45` 只 import `role_label`，
    退役判断也走 `get_role` 抛不抛）——两边数字对上，才说明真的只有一个 owner。
    """
    assert set(RETIRED_ROLES) == EXPECTED_RETIRED, (
        f"退役集合与登记不符：现读 {sorted(RETIRED_ROLES)}，登记 {sorted(EXPECTED_RETIRED)}。"
        f"改它必须是有意的：更新 EXPECTED_RETIRED 并在交付里点名。")
    assert set(ad.RETIRED_ROLES) == set(RETIRED_ROLES), (
        f"适配层的退役名单与权威层分叉：{sorted(ad.RETIRED_ROLES)}")
    for key in EXPECTED_RETIRED:
        assert key not in ROLE_KEYS, f"{key} 已退役却还在公开面：用户选了就拿不到总结"


def test_the_roles_rpc_carries_the_same_retired_list_the_authority_keeps(tmp_path):
    """`summary.roles` 的 `retired` 字段就是权威层那份，页面不许自己攒名单。"""
    from video_workbench.app.service import WorkbenchService

    from test_summary_real_backend_ui import _temp_config

    cfg_path = _temp_config(tmp_path)
    service = WorkbenchService(config_path=str(cfg_path))
    try:
        data = service.dispatch("summary.roles", {}, "t32")["data"]
    finally:
        service.close()
    assert sorted(data["retired"]) == sorted(EXPECTED_RETIRED), (
        f"RPC 的退役名单与登记不同：{data['retired']}")
    listed = [item["key"] for item in data["roles"]]
    assert not (set(listed) & EXPECTED_RETIRED), (
        f"RPC 清单里出现退役键：{sorted(set(listed) & EXPECTED_RETIRED)}")


# ------------------------------------------------------------- 2. 展示口径（人话）


@pytest.mark.parametrize("key", sorted(EXPECTED_RETIRED))
def test_role_label_names_a_retired_role_in_plain_words(key):
    """`role_label` 对退役键：不抛、含中文名、含「已停用」。

    这条是「历史行的角色那一格不能空、不能报错」的后端半边；
    界面半边在 `test_the_page_says_a_retired_history_role_is_stopped_and_what_it_switched_to`。
    刻意**不**要求它含 key 本身：给用户的说法是人话（「学术分析（已停用）」），
    出处那一行才要 key（见导出那组）。
    """
    label = role_label(key)
    assert label, f"{key}：role_label 返回空串——那一格会显示成空白"
    assert "已停用" in label, f"{key}：退役角色的标签里没有「已停用」：{label!r}"
    assert label != key, f"{key}：只回了个 key，用户看不懂：{label!r}"


def test_role_label_for_an_unknown_key_does_not_claim_it_was_retired():
    """未知键的标签不许说「已停用」：它从没存在过，用户要做的是改配置不是换角色。"""
    label = role_label(UNKNOWN_KEY)
    assert "已停用" not in label, f"未知键被说成已停用：{label!r}"


# ------------------------------------------------------------- 3. 出处口径（导出）


def _md(role_value: str) -> str:
    return ad.to_markdown({**GOOD, "role": role_value, "model": "m-test"})


def test_export_marks_a_retired_history_role_by_its_stored_key():
    """导出正文：`角色：xueshu（已停用）` —— 既不报错也不留白，但**保留存进库的 key**。

    这是「人话覆盖不许改 key」那半：导出是出处，历史行得能按 key 对回库里那一行。
    所以这一行同时要求三件事：key 在、标注在、没被换成中文名。
    """
    for key in ("xueshu", "shipin", "standard"):
        body = _md(key)
        assert f"角色：{key}（已停用）" in body, (
            f"退役键 {key} 的导出那行不对：{[l for l in body.splitlines() if '角色' in l]}")
        assert "（未知角色）" not in body, f"{key}：退役键被说成未知角色"
        assert role_label(key) not in body, (
            f"{key}：导出被改成说人话了——出处那一行必须按存进库的 key 对回那一行")


def test_export_marks_an_unknown_role_separately_from_a_retired_one():
    """未知键的导出必须是 `（未知角色）`，不许与「已停用」共用一个兜底。"""
    body = _md(UNKNOWN_KEY)
    assert f"角色：{UNKNOWN_KEY}（未知角色）" in body, (
        f"未知键那行不对：{[l for l in body.splitlines() if '角色' in l]}")
    assert "已停用" not in body, "两类被并成一类：退役要说「换角色」，未知要说「改配置」"


def test_export_of_a_live_role_stays_the_bare_key():
    """现役角色那行不许被加任何标注（正负对照的第三条腿）。"""
    for key in ROLE_KEYS:
        body = _md(key)
        assert f"角色：{key}" in body, f"{key}：现役角色的导出少了那一行"
        line = next(l for l in body.splitlines() if l.startswith("角色："))
        assert "（" not in line, f"{key}：现役角色被标注了：{line!r}"


# ------------------------------------------------------------- 4. 生成入口的两支


@pytest.mark.parametrize("key", sorted(EXPECTED_RETIRED))
def test_an_explicit_retired_role_at_the_generation_entry_is_a_validation_error(key):
    """A 支：用户这次显式提交退役键 → `WBError VALIDATION`，不许悄悄换角色。

    `_retired_not_in_registry` 先把「权威层认识它」钉住：
    如果哪天退役键从登记表里掉出去，`get_role` 的报错会从「已停用」变成「不认识」，
    那时这条红，提醒去看退役集合，而不是让 A/B 两支的判断跟着漂。
    """
    from video_workbench.app.errors import WBError

    message = _retired_not_in_registry(key)
    assert "已停用" in message, f"权威层原话没说「已停用」：{message!r}"
    with pytest.raises(WBError) as exc:
        ad.resolve_role(key)
    assert exc.value.code == "VALIDATION"
    assert "已停用" in str(exc.value.message)


@pytest.mark.parametrize("key", sorted(EXPECTED_RETIRED))
def test_a_retired_role_coming_from_the_config_falls_back_and_says_so(key):
    """B 支：退役值只来自 fallback（配置/历史行）→ 回落默认角色 + notice 说清原因。

    拿历史惩罚现在的人是不合理的：配置文件里躺着 `standard` 是脏数据，
    用户从没在本次点过它。但**必须**留 notice，否则用户以为文风变了是自己选的。
    """
    got, notice = ad.resolve_role(None, key)
    assert got == DEFAULT_ROLE, f"{key}：兜底没回落默认角色，实得 {got}"
    assert "已停用" in notice and "默认角色" in notice, (
        f"{key}：回落了却没说原因：{notice!r}")


def test_the_generation_run_with_a_dirty_config_role_succeeds_and_logs_the_switch():
    """B 支走完整条生成链：脏配置不许把总结挡在门外，且日志里必须有那句原因。

    这条是上一那条的**落地版**：`resolve_role` 对了但 `run()` 没把 notice 打出去，
    用户仍然会在「怎么突然换了文风」里摸不着头脑。
    """
    cfg = _cfg()
    cfg.llm.role = "standard"
    segments = [{"start_ms": index * 2000, "text": f"第 {index} 句字幕内容"}
                for index in range(40)]
    client = StubClient([json.dumps(GOOD)] * 3)
    ctx = FakeCtx()
    result = ad.run({"title": "t", "url": ""}, segments,
                    client=client, cfg=cfg, ctx=ctx)
    assert result["role"] == DEFAULT_ROLE, (
        f"脏配置角色没被回落成默认：{result['role']}")
    assert any("已停用" in line for line in ctx.logs), (
        f"生成过程没把回落原因写进日志：{ctx.logs}")


def test_an_unknown_role_from_the_config_still_blocks_the_run():
    """未知值不许被静默修正成默认角色：否则用户永远不知道自己那一行没生效。"""
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        ad.resolve_role(None, UNKNOWN_KEY)
    assert exc.value.code == "VALIDATION", (
        f"未知键的错误码不对：{exc.value.code} / {exc.value.message}")


# ------------------------------------------------------------- 5. 界面口径（真页面）


def _open_summary_page(browser, web_base: str, service):
    """开真后端页面并进「总结」页。

    运输方式与 `test_summary_real_backend_ui.py` 同一套：产品的 `index.html:13` 写了
    `connect-src 'none'`，页面不做任何 fetch/XHR，所以这里用 `expose_binding` 注入
    与桌面版 `window.api.rpc` 同形的桥，产品 CSP 一字不动。
    `web_base` 传进来的就是 fixture 那个完整 URL（带 `?mock=1#/settings…`），
    取掉片段再用作基址——上一版我把它当基址拼了 `/index.html`，路由掉进未知页，
    看起来像「版本行没渲染」，其实是页面根本没走到总结页。
    这里只取 **scheme://host:port**（`urlsplit`），把 path 和 query 都甩掉重新拼：
    用 `split("?")` 会把 `/index.html` 留在尾巴上，拼出 `index.html/index.html` → 404，
    页面永远等不到 `WB.router`（我在这上面白等了两次 15 秒）。
    """
    from urllib.parse import urlsplit

    from test_summary_real_backend_ui import BRIDGE_JS

    parts = urlsplit(web_base)
    origin = f"{parts.scheme}://{parts.netloc}"
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console: {msg.text}")
            if msg.type == "error" and "favicon" not in (msg.text or "") else None)

    def _bridge(source, payload):
        method = payload.get("method") or ""
        service.bridge_calls.append(method)
        try:
            return service.dispatch(method, payload.get("params") or {})
        except Exception as exc:  # noqa: BLE001 - 桥不能把页面拖死，错误照产品的信封回
            return {"ok": False, "request_id": None,
                    "error": {"message": f"{type(exc).__name__}: {exc}"}}

    page.expose_binding("__wbRpc", _bridge)
    page.goto(f"{origin}/index.html?mock=1", wait_until="load")
    page.wait_for_function("() => window.WB && window.WB.rpc && window.WB.router", timeout=15000)
    page.evaluate(BRIDGE_JS, {"methods": ["library.list", "summary.get",
                                          "summary.models", "summary.roles"]})
    page.evaluate("() => window.WB.router.go('summary')")
    # 等证据来自桥的真调用：mock 也有一条角色清单，读了 mock 就等于没测。
    page.wait_for_function(
        "() => (window.__realCalls || []).indexOf('summary.get') >= 0", timeout=20000)
    page.wait_for_selector("#sum-chips", timeout=20000)
    return page, errors


def _page_with_one_role(tmp_path, browser, web_base_url, role_value):
    """Temp 库里造**一个**项目 + **一条**该角色的版本行，并把页面开到总结页。

    历史行只能这么造（文件头写过原因）：真库里没有 xueshu/shipin 的行。
    写库走 `repo.summary_insert`，**不经过生成入口**——这正是分类标准第 2 类的形状。
    只造一条：页面上的提示（`roleNotice`）说的是**当前那一版**的角色，
    多条会让「当前版」取决于插入顺序，判据就不是我说的那个东西了。
    """
    from video_workbench.app.service import WorkbenchService

    from test_summary_real_backend_ui import _Service, _temp_config

    cfg_path = _temp_config(tmp_path)
    svc = WorkbenchService(config_path=str(cfg_path))
    handle = _Service(svc, cfg_path)
    root = tmp_path / "media" / "retired-row"
    root.mkdir(parents=True)
    project = svc.repo.project_create(title="退役行样本", root_dir=str(root))
    svc.repo.summary_insert(project["id"], content={**GOOD, "role": role_value},
                            content_md="正文", role=role_value, model="stub-summary")
    page, errors = _open_summary_page(browser, web_base_url, handle)
    return page, errors, handle


def _fallback_role_name(handle) -> str:
    """默认角色在**载荷里**的名字（页面用的就是它，不许我这边另拼中文名）。"""
    rows = handle.call("summary.roles", {})["roles"]
    return next(row["name"] for row in rows if row["key"] == DEFAULT_ROLE)


def _assert_notice_chip_visible(page) -> None:
    """那颗「注意」chip 必须**看得见**，不只是在 DOM 里。

    `#sum-chips` 在页头，不在 `<details>` 折叠组里，所以这里**不适用** `require_expanded`
    那套前置（b79 判据①的适用条件在这里不成立——写清楚，免得下一个人照抄展开步骤，
    也免得他以为我在用收起态的 `inner_text` 冒充可见）。
    """
    from playwright.sync_api import expect

    expect(page.locator("#sum-chips span", has_text="上一版的角色").first).to_be_visible(
        timeout=10000)


def test_the_page_says_a_retired_history_role_is_stopped_and_what_it_switched_to(
        tmp_path, browser, web_base_url):
    """历史行存着退役键时，界面必须说人话并且**保留那个 key**。

    落点是 `summary.js:661-666`：`#sum-chips` 里那颗「注意」chip，正文来自 `adoptRole`
    （`:218-219`）。句子同时说清三件事——原来哪个角色、为什么不能用、这次改用谁。
    产品事实一条：单视频的版本行 meta 只有「日期 · 模型 · 字数」，**不含角色**
    （`roleText` 用在合集总览行 `:1034` 与总览脚注 `:1114`）。
    所以这条钉的是「当前版角色不可用时的提示」，不去断一个界面上不存在的位置。
    """
    page, errors, handle = _page_with_one_role(tmp_path, browser, web_base_url, "xueshu")
    try:
        chips = page.locator("#sum-chips").inner_text()
        assert "已停用" in chips, f"界面没说「已停用」：{chips!r}"
        assert "「xueshu」" in chips, (
            f"界面把退役键藏起来了——用户得知道是哪一版的角色不能原样重做：{chips!r}")
        assert f"「{_fallback_role_name(handle)}」" in chips, (
            f"界面没说清改用哪个角色：{chips!r}")
        assert "未知角色" not in chips, f"退役键被说成未知键：{chips!r}"
        _assert_notice_chip_visible(page)
        assert errors == [], f"页面报错：{errors[:6]}"
    finally:
        page.close()


def test_the_page_keeps_an_unknown_history_role_a_different_message(tmp_path, browser,
                                                                   web_base_url):
    """未知键的历史行不许也说「已停用」：它从没存在过，用户要做的是改配置。

    这条与上一条是同一族的两侧——只写任一条，实现都可以用一个 `else` 把两类并成一类。
    """
    page, errors, _handle = _page_with_one_role(tmp_path, browser, web_base_url, UNKNOWN_KEY)
    try:
        chips = page.locator("#sum-chips").inner_text()
        assert UNKNOWN_KEY in chips, f"界面把未知键藏了：{chips!r}"
        assert "已停用" not in chips, f"未知键被说成已停用：{chips!r}"
        assert "不在角色列表" in chips or "不认识" in chips, (
            f"界面没说清这是不认识的键：{chips!r}")
        _assert_notice_chip_visible(page)
        assert errors == [], f"页面报错：{errors[:6]}"
    finally:
        page.close()


# ------------------------------------------------------------- 6. 真库只读对照


def test_a_live_history_role_produces_no_switch_notice(tmp_path, browser, web_base_url):
    """现役角色的历史行**不该**冒任何「注意」提示——否则这条降级逻辑就在乱报。

    三条腿（退役 / 未知 / 现役）缺一条都不算数：只写前两条，实现可以变成
    「只要版本行里有角色就提示」，界面看起来仍然"对"。
    顺带钉一个现读到的事实：现役角色那格显示的是**它自己的名字**（`renhua` → 「说人话」），
    不是回落后的默认角色——只有不可用的键才会被换掉并说明。
    """
    page, errors, handle = _page_with_one_role(tmp_path, browser, web_base_url, "renhua")
    try:
        chips = page.locator("#sum-chips").inner_text()
        assert "上一版的角色" not in chips, f"现役角色被提示成不可用：{chips!r}"
        assert page.locator("#sum-chips span", has_text="上一版的角色").count() == 0, (
            "「注意」那颗 chip 还在 DOM 里，只是文字空了——那是半条实现")
        rows = handle.call("summary.roles", {})["roles"]
        own = next(row["name"] for row in rows if row["key"] == "renhua")
        assert own in chips, f"界面上看不到这一版自己的角色名 {own!r}：{chips!r}"
        assert errors == [], f"页面报错：{errors[:6]}"
    finally:
        page.close()


def test_the_page_does_not_read_the_label_rows_the_cli_consumes():
    """判据 5 第三腿：`retired_labels` 是**给 CLI 的**，前端一个字都不许吃。

    前端那份是裸 key 数组 `retired`（`summary.js:234 state.roleRetired =
    (data && data.retired) || []`），判退役走 `indexOf(key)`。谁哪天「顺手统一一下」
    把它换成对象数组，`indexOf` 恒 -1 ⇒ 每条历史行都显示成「未知角色」，而且**不会报错**
    —— 这正是本仓库最贵的那类静默错。所以这条按源码扫，不按渲染扫：渲染只覆盖我
    当时造的那条数据，扫源码覆盖所有分支。

    目录可指：`WB_WEB_JS_DIR`（照 `WB_WEB_ROOT` 的先例），牙齿据此在 `%TEMP%` 副本上
    把 `summary.js` 改成吃 `retired_labels`，这条必须红；默认读仓库那份产品树。
    """
    js_dir = Path(os.environ.get("WB_WEB_JS_DIR") or WEB_JS_DEFAULT)
    # 扫描域＝**页面逻辑**，`mock.js` 除外。mock 是演示态的载荷夹具，task-42 要它的恰恰
    # 是「键要补齐、值留中性」，它里面出现 `retired_labels` 是**应该**的；把它算进「前端
    # 吃 CLI 字段」会把补形状判成事故（2026-11-04 vendor 落地那一版就是被这条判红的）。
    # 夹具那一半改由 `test_the_mock_payload_has_every_key_the_real_service_sends` 反向钉：
    # 逻辑不许吃、载荷必须有，两条合起来才是完整合同——只留前一条等于把后半删了。
    logic = [f for f in sorted(js_dir.rglob("*.js")) if f.name != "mock.js"]
    assert logic, f"没找到任何页面 js，这条空转了：{js_dir}"
    hits = [(f.relative_to(js_dir), ln)
            for f in logic
            for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            if "retired_labels" in line]
    assert hits == [], (
        "页面逻辑开始吃 retired_labels —— 那是 CLI 的字段，"
        f"换成对象数组会让 indexOf 恒 -1：{hits}")
    # 控制腿：前端必须**还在**吃裸 key 那份，否则上一条的「0 命中」可能只是因为
    # 整个退役显示被人删了。
    page = (js_dir / "pages" / "summary.js").read_text(encoding="utf-8")
    assert "data.retired" in page, (
        "summary.js 不再读 `retired` —— 退役显示整块没了，这条锁要重新设计")


def test_the_mock_payload_has_every_key_the_real_service_sends(browser, web_base_url,
                                                                tmp_path):
    """判据 5（task-41）：演示态载荷的**键集合**必须等于真服务那一份，两边都现读。

    为什么必须有这条：缺键与空值在界面上表现**完全一样**——`summary.js:234` 是
    `(data && data.retired) || []`，读不到就当空。于是 mock 少一个键不会报错、不会白屏，
    只会让演示态**永远走兜底支**；而演示态恰是最常被拿去截图、做验收的那一态。形状漂了
    没人拦得住，这一格此前无人认领（task-39 交板时我先发现，lead 现读比我说的更严重：
    真载荷七个键，mock 原来只有 `default` + `roles` 两个）。

    写法上有两条硬要求：
    1. **不许写死七个键的名单**——那是第三个 owner。两边各自现读：真侧走进程内
       `roles_payload()`，演示侧走页面里的 `WB.rpc("summary.roles")`（mock 模式经
       `rpc.js:85` 转 `WB.mock.call`，与页面真实消费路径同一条）。
    2. 断的是**键集合**，不是值。值按 task-42 的规定一律是中性值（空数组/false/空串），
       真侧是现算的，比对值必然假红。
    """
    from urllib.parse import urlsplit

    from video_workbench.app.service import roles_payload
    from video_workbench.config import WorkbenchConfig

    from test_summary_real_backend_ui import _temp_config

    cfg_path = _temp_config(tmp_path)
    real_keys = set(roles_payload(WorkbenchConfig.load(str(cfg_path))))
    assert real_keys, "真载荷空了：下面那条集合相等会退化成『空==空』的假绿"

    parts = urlsplit(web_base_url)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)[:220]))
    mock_keys = None
    rpc_failure = None
    try:
        page.goto(f"{parts.scheme}://{parts.netloc}/index.html?mock=1",
                  wait_until="load")
        # 两条谓词分开等：`WB.rpc` 存在是脚本加载完了，`WB.isMock()` 为真才是演示态。
        # 演示态没起来却拿真后端载荷比键，两边同源，这条就成了假绿。
        page.wait_for_function(
            "() => !!(window.WB && typeof WB.rpc === 'function')", timeout=15000)
        page.wait_for_function(
            "() => !!(window.WB && WB.isMock && WB.isMock())", timeout=15000)
        # `WB.rpc` **成功就返回 data**（`rpc.js:102` 注释与 `:112 return res.data`），
        # 失败抛 `RpcError`。前一版我按「它回信封」写判据，是把 `WB.raw` 的语义安到了
        # `WB.rpc` 上——那样 Object.keys(信封) 得到 ok/data/request_id，恒不等于真键集合，
        # 一条永远红的锁。信封形状要验就得显式用 WB.raw。
        mock_keys = page.evaluate("""async () => {
          const d = await WB.rpc('summary.roles', {}, {silent: true});
          return Object.keys(d || {});
        }""")
    except PlaywrightError as exc:
        rpc_failure = str(exc)[:400]
    finally:
        page.close()

    assert rpc_failure is None, (
        f"演示态取 `summary.roles` 没成功（{rpc_failure}）；页内错误：{page_errors}。"
        "页面没起来 ≠ 载荷形状不符，别把这一支读成判据红。")
    assert not page_errors, f"取完键页面仍报错，读数不可信：{page_errors}"
    assert mock_keys is not None, "上面两个断言与这条不可能同时不成立——走到这就是夹具坏了"
    assert set(mock_keys), "演示态载荷是空对象：集合相等会变成『空 == 空』的假绿"
    missing = sorted(real_keys - set(mock_keys))
    extra = sorted(set(mock_keys) - real_keys)
    assert not missing and not extra, (
        f"演示态载荷形状与真服务不一致：mock 缺 {missing}，多 {extra}。"
        "缺键与空值在界面上表现完全一样（`summary.js:234` 那句 `|| []`），所以演示态"
        "会永远走兜底支且不报错——这条就是拦这个的。")


def test_the_real_database_row_for_standard_is_still_readable():
    """真库那 1 条 `standard` 行：只读能列出来，且这一趟**没被写过**（跑前跑后 sha 一致）。

    这条证的不是降级逻辑（那靠 Temp 夹具），是**没有任何迁移脚本改写过历史数据**。
    本机没有这个库就明确 skip，并说「不是通过，是没跑」。
    """
    if not REAL_DB.is_file():
        pytest.skip(f"本机没有真库 {REAL_DB}，这条没跑（不是通过）")
    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    conn = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select role, count(*) from summary_version group by role").fetchall()
    finally:
        conn.close()
    counts = dict(rows)
    assert counts.get("standard", 0) >= 1, (
        f"真库里没有 standard 的历史行，这条的对照意义没了：{counts}")
    after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    assert after == before, "只读连接改了真库指纹——立刻停手查是谁写的"


# ============================================================== 真库导出（缺口 1）
# 取材分两栏，**不许混成「已验」**：
#   上面 `test_export_*` 那组是 **Temp 夹具造行** —— 能造 xueshu/shipin，因为真库里没有那种行；
#   这两条是 **真实库只读** —— 真数据里只有 1 条 `standard`，它是「用户盘上真存在过的退役行」
#   的唯一证据，补的是「真库的导出路径今天没被任何用例走过」这个洞（此前只被 GROUP BY 数过）。
# 现读事实：`summary_version` 列 = id/task_id/version_no/source/requirement/role/summary_json/
# preview_md/comment_text/metrics/created_at；`summary_json` 里**也存了 role**，两者一致才说明
# 没有迁移只改了一半。


def test_the_real_retired_row_still_exports_by_its_stored_key():
    """真库那行 `standard` 喂 `to_markdown`：按**存库的 key** 对回那一行 + 停用标注。

    实测读数（探针 2026-11-04 22:32）：导出那行是
    `角色：standard（已停用）　模型：qwen3.8-omni-flash　分段：3　本次改动：…`
    —— 三件事一起钉：key 在、标注在、**没被人话名替换**。
    """
    if not REAL_DB.is_file():
        pytest.skip(f"本机没有真库 {REAL_DB}，这条没跑（不是通过）")
    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    conn = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in conn.execute("pragma table_info(summary_version)")]
        row = conn.execute(
            "select * from summary_version where role = 'standard' limit 1").fetchone()
    finally:
        conn.close()
    if row is None:
        pytest.fail("真库里 standard 行没了 —— 这条的证据基础消失，改用夹具并在交付里写明")
    data = dict(zip(cols, row))
    body = json.loads(str(data["summary_json"]))
    assert body.get("role") == data["role"] == "standard", (
        f"列与 JSON 的角色不一致，「按 key 对回那一行」成了看运气："
        f"列={data['role']!r} JSON={body.get('role')!r}")

    text = ad.to_markdown(body)
    line = next((ln for ln in text.splitlines() if ln.startswith("角色：")), "")
    assert "standard" in line, f"导出里找不到存库 key：{line!r}"
    assert "已停用" in line, f"真库退役行导出没标注停用：{line!r}"
    assert "未知角色" not in line, f"真库退役行被并报成未知：{line!r}"
    # 导出是**出处**，人话归界面：这里出现 role_label() 的输出就是把两栏合成一栏了。
    assert "标准（已停用）" not in text, (
        "导出开始念人话名了 —— 按存库 key 对回那一行是导出的定义，不是显示偏好")

    after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    assert after == before, "只读连接改了真库指纹 —— 立刻停手查是谁写的"
    print(f"sha unchanged: {after == before}")


def test_the_real_database_only_holds_standard_among_retired_keys():
    """钉住「真库退役 key 只有 standard」这个事实 —— 它就是上一条只能用 standard 的原因。

    常驻而不是写进注释的理由：将来真库里多出 xueshu/shipin（或有人迁移造行）时，这条会红着
    提醒「取材基础变了，两栏分工要重排」，而不是让人以为夹具那组已经等价于真库验证。
    """
    if not REAL_DB.is_file():
        pytest.skip(f"本机没有真库 {REAL_DB}，这条没跑（不是通过）")
    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    conn = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        counts = dict(conn.execute(
            "select role, count(*) from summary_version group by role").fetchall())
    finally:
        conn.close()
    retired_in_db = sorted(set(counts) & EXPECTED_RETIRED)
    assert retired_in_db == ["standard"], (
        f"真库的退役 key 不再只有 standard：{counts} —— 上一条的取材范围要跟着扩")
    after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    assert after == before, "只读连接改了真库指纹 —— 立刻停手查是谁写的"


# ================================================== RPC 层的兜底支（缺口 2）
# vendor 在 task-36 自报：他只在 `cli.py` 做了「缺 display 退回裸 key」，RPC 这条路
# （`app/service.py:691` 经**模块属性** `summarizer.retired_row_labels()` 取数）没夹具。
# 我现读 + 探针复核过他那句「缺 display 会 UnknownRole 抛错」——**报得太宽，两种坏法要分开**：
#   · `role_label()`（`core/summary/__init__.py:233`）docstring 明写「任何输入都不抛」，
#     正常路径根本产不出「缺 display」；所以缺 display 只能是载荷层被改坏。
#   · 探针 Q2a：把 `retired_row_labels` 换成 key-only，RPC **照旧 ok=True**、`retired` 与
#     `roles` 都还在 ⇒ 分类靠裸 key 那把，display 只是念法，念法没了分类不能没。
#   · 探针 Q2b：真抛出去时，`app/rpc.py:78-91` 兜住 Exception 回 INTERNAL 信封，
#     **不会把桥挂死**（他担心的「页面卡死」不成立），成立的是另一件事：整页角色名单
#     跟着一起没，而且 message 里认不出是哪个键。⇒ 下面第二条是**变更探测器**，
#     钉的是现状，不是我认为的对。


def _rpc_roles_envelope(tmp_path) -> dict:
    """进程内真 service 取 `summary.roles` 的信封（接缝照 :121 那条，不新造入口）。

    teardown 必 `close()`：`WorkbenchService.__init__` 会建库建 logs，泄漏会污染 Temp。
    """
    from video_workbench.app.service import WorkbenchService

    from test_summary_real_backend_ui import _temp_config

    service = WorkbenchService(config_path=str(_temp_config(tmp_path)))
    try:
        return service.dispatch("summary.roles", {}, "t39")
    finally:
        service.close()


def test_the_roles_rpc_still_serves_retired_keys_when_a_row_has_no_display(tmp_path):
    """把 `retired_labels` 的 display 整列拿掉：RPC 必须照旧 ok，`retired` 一把不能少。

    拦的是「把退役判定搬到 display 上」这类退化。控制腿在同一条里先跑，
    防「patch 没生效也算绿」。
    """
    keys = sorted(EXPECTED_RETIRED)
    normal = _rpc_roles_envelope(tmp_path)
    assert normal["ok"] is True, f"控制腿：正常 RPC 就失败了 {normal.get('error')}"
    assert [r["key"] for r in normal["data"]["retired_labels"]] == \
        normal["data"]["retired"] == keys, (
        "控制腿不成立：正常载荷两栏就不同源同序，下面那条对照没有意义")

    original = ad.retired_row_labels          # `ad` 就是 core.summarizer（文件 :69 现读别名）
    ad.retired_row_labels = lambda: [{"key": k} for k in keys]
    try:
        envelope = _rpc_roles_envelope(tmp_path)
    finally:
        ad.retired_row_labels = original
    # 判据 1（task-41）：**不许**把这条写成「必须 ok=True」。合同是一句不变量——
    # 「无论响应长成哪一支，界面依赖不能塌」：
    #   A 支（今天这一支）＝整条照旧，只是 retired_labels 少一列 ⇒ 退役判定 `retired`
    #     必须三把不少，前端 `summary.js:234` 的 indexOf 才有得查；
    #   B 支（将来若改成逐键兜底）＝不许把整条打死，活角色清单 `roles` 必须仍在。
    # 两支任一成立即通过；两条都不成立＝界面塌了，那才是这条要拦的。
    retired_intact = (envelope["ok"] is True
                      and envelope["data"].get("retired") == keys)
    roles_intact = bool((envelope.get("data") or {}).get("roles"))
    assert retired_intact or roles_intact, (
        f"界面依赖塌了：既没有完整的退役名单（A 支），也没有活角色清单（B 支）。"
        f"实得 ok={envelope['ok']} error={envelope.get('error')}")
    # 下面这行不是上面那行的重复，是**变更探测器**：它钉「今天走的是 A 支」。哪天有人
    # 把兜底改成逐键降级（B 支），这条必须红，要求把上面 A 支那一段的具体断言（少一列
    # display 的形态）搬到 B 支重写——而不是让合同悄悄变成「两种都行、什么都不检查」。
    assert retired_intact, (
        "当前分支已从 A（少一列、整条照旧）变成 B（整条失败但 roles 还在）——"
        "请同步改写上面那段断言，并保留『不许打死接口』这条不变量")
    data = envelope["data"]
    assert [r.get("display") for r in data["retired_labels"]] == [None] * len(keys), (
        "兜底腿自己把 display 补出来了 —— 这一条只测「缺字段不许打死接口」，"
        "补谁的名字是另一个决定，不许顺手猜")


def test_one_unprintable_retired_key_currently_takes_the_whole_role_list_down(tmp_path):
    """变更探测器（**钉现状，不是钉我认为的对**）：某个退役键的显示名算不出来时，
    今天整条 `summary.roles` 一起失败（ok=False / INTERNAL），而不是只少那一行。

    这是 task-39 现读时发现的单点故障，**归 lead 判要不要修，我不改产品码**：
    `roles_payload()`（`app/service.py:691`）里 `retired_row_labels()` 一抛，整个载荷构造
    就没了 ⇒ 用户看到的是「设置页连活角色都列不出来」，代价与「一个退役键念不出来」不成比例。
    第二条证据更难受：错误信封的 `message` 只有异常原文、**不含键名**（键只出现在 detail 的
    traceback 里），现场连「哪个键坏了」都要翻栈。

    修好之后本条会**变红**：那时请改成期望 ok=True 并保留「其他键照常念得出」的断言，
    同时把上面这段说明删掉——别只把断言放宽成两种都行。

    **与上一条（缺 display）的分工**（task-41 判据 2，lead 定）：那条测的是「载荷少一列」
    ＝输入形状退化，接口不许塌；这条测的是「标签计算本身抛」＝整条名单一起没。两者不是
    同一件事，别合并成一条「都要优雅降级」。

    **为什么产品侧不该包 try/except**（lead 现读 `core/summary/__init__.py:233` 后定的）：
    `role_label()` 的 docstring 逐字写着「任何输入都不抛」，那是**契约**。在载荷层套一层
    try/except 是给**契约上不存在**的输入加分支，等于把「有人违反了 role_label 自己的契约」
    这个 bug 压成静默——真要抛就该修 `role_label` 本身，不在调用方盖症状。所以这条长期
    钉现状、不推动产品加分支；它同时是「role_label 违约」的报警器。
    """
    import video_workbench.core.summary as authority

    original = authority.role_label

    def boom(key):
        if str(key) == "xueshu":
            raise authority.UnknownRole("夹具：这一个键念不出来")
        return original(key)

    authority.role_label = boom
    try:
        envelope = _rpc_roles_envelope(tmp_path)
    finally:
        authority.role_label = original

    assert envelope["ok"] is False, (
        "单点故障已被修掉：坏一个键不再打死整张名单 —— 现在就把这条改成期望 ok=True，"
        "并加一条「其余键的 display 仍要正常」的断言")
    error = envelope.get("error") or {}
    assert error.get("code") == "INTERNAL", f"错误码不是现状登记的 INTERNAL：{error}"


def test_the_error_envelope_currently_names_no_broken_role_key(tmp_path):
    """判据 3（task-41，lead 判**先不修**）：错误信封的 `message` 现在**不含坏掉的键名**。

    现场难受的地方：`ok=False` 时用户和排障的人只看到异常原文（夹具那句
    「这一个键念不出来」），`xueshu` 这个键只出现在 detail 的 traceback 里。也就是说
    「设置页整张角色名单没了」这件事，日志与界面都指不出是哪个键坏的。

    lead 的处置：不修产品码，但把「现在不带键名」钉成常驻用例——将来谁把键名加进
    message（这是最省事的修法），这条会**变红**，要求那时把它抬成「必须带键名」并删掉
    本条的现状说明。别把断言写成「带不带都行」。
    """
    import video_workbench.core.summary as authority

    original = authority.role_label

    def boom(key):
        if str(key) == "xueshu":
            raise authority.UnknownRole("夹具：这一个键念不出来")
        return original(key)

    authority.role_label = boom
    try:
        envelope = _rpc_roles_envelope(tmp_path)
    finally:
        authority.role_label = original

    assert envelope["ok"] is False, "控制腿失效：整条接口今天没有失败"
    message = str((envelope.get("error") or {}).get("message", ""))
    assert "xueshu" not in message, (
        "错误信封已经开始带键名了（现状：只有异常原文）。这是好消息——请把这条改成"
        f"『message 必须含坏掉的键名』，并删掉上面那段现状说明。实得 message={message!r}")
    assert "这一个键念不出来" in message, (
        f"错误信封连异常原文都不回了，界面将完全没有线索：{message!r}")
# ---------------------------------------------------------------------------
# task-41 判据 1：把 docstring 那句「任何输入都不抛」从注释变成机械证据
#
# 分工（判据 2，刻意不合并成一条）：
#   · 下面这一组钉的是**函数本身对任何输入都不抛**——契约面。
#   · `test_one_unprintable_retired_key_currently_takes_the_whole_role_list_down`
#     钉的是**万一它抛了会牵连整张名单**——现状面（变更探测器）。
#   并成一条的话，「不抛」就会被「抛了也有错误信封」掩盖：将来 role_label 真的开始抛，
#   那一组照样绿。一条说「不许坏」，一条说「坏了会长什么样」，缺一不可。
#
# 期望值全部现读，**一个中文显示名都不写死**：写死就等于将来有人把「已停用」改成
# 「停用」，测试红得毫无信息量（还会诱导下一个人去改测试而不是改产品）。
# ---------------------------------------------------------------------------

# 契约的措辞是「任何输入」，所以用例集合要是它的像：类型形状 × 键空间两条轴都铺到。
_SHAPE_PROBE = (
    None, "", "   ", "\t\n", 123, 0, -1, 3.5, True, False, b"x",
    [], ["x"], {}, {"key": "x"}, (1,), frozenset({"x"}), set(),
    object(), Ellipsis, NotImplemented, range(2), (lambda: None), Exception("boom"),
)
# `NotImplemented` 与 `Ellipsis` 现在都在这一组里：task-43 把判空从真值判断改成
# `"" if key is None else str(key).strip()` 之后，builtin 哨兵和 `123`、`b'x'`、
# `object()` 走同一条未知键路径，不再单开分支。

# 真·未知字符串。注意 **不能**写 " standard"／"standard "：现读证明 `role_label`
# 会先去空白再查表，那两个会正常解析成退役标签（是宽容，不是 bug）。# 也**不能**写 "wengao"：那是现役键，会被正常解析成角色名，
# 拿它当未知键就会得到一条假红（第一版我就踩在这里）。
_UNKNOWN_STRINGS = (
    "zzz-not-a-role", "Standard", "STANDARD", "standard2",
    "标准", "已停用", "（已停用）",
)


def _authority_layer():
    import video_workbench.core.summary as authority

    return authority


def _live_base_name(key: str) -> str:
    """退役键的「基名」现读：vendored 副本表里有定义就用它，没有就用权威层补登的名。

    两个来源都是产品自己的表，不是我抄的第二份真源——我要量的只是
    「label 在基名之外还多加了什么」，基名本身由权威层决定。
    """
    import video_workbench.core.summary.roles as copy_roles

    for role in copy_roles.ALL_ROLES:
        if role.key == key:
            return role.name
    extra = getattr(_authority_layer(), "_RETIRED_EXTRA_NAMES", {})
    assert key in extra, (
        f"{key!r} 是退役键，但副本表里没有它、权威层也没补登名字——"
        "那它的显示名是从哪来的？请把这条改成读新的真源位置")
    return extra[key]


def _live_retired_suffix() -> str:
    """退役标签共用的那个后缀，从现读 label 减现读基名得到（不写中文字面量）。"""
    suffixes = set()
    for key in sorted(RETIRED_ROLES):
        label = role_label(key)
        base = _live_base_name(key)
        assert label != key, f"退役键 {key!r} 的标签退化成裸 key，界面等于没说话"
        assert label.startswith(base), (
            f"退役标签 {label!r} 不是以现读基名 {base!r} 开头——格式变了，"
            "这条判据的减法前提不成立，请改成新格式再判")
        suffixes.add(label[len(base):])
    assert len(suffixes) == 1, f"三把退役键的后缀不一致：{sorted(suffixes)}"
    suffix = suffixes.pop()
    assert suffix, "退役标签没有任何后缀，那「已停用」这件事在界面上就消失了"
    return suffix


def test_role_label_never_raises_and_always_returns_a_string():
    """判据 1 主面：把 `_SHAPE_PROBE` 里每个形状都喂进去，一个都不许抛、都必须回 str。"""
    offenders = []
    for value in _SHAPE_PROBE:
        tag = f"{type(value).__name__}({value!r})"[:60]
        try:
            out = role_label(value)
        except Exception as exc:                       # noqa: BLE001 - 契约面就是要抓任何异常
            offenders.append(f"{tag} → 抛 {type(exc).__name__}: {exc}")
            continue
        if not isinstance(out, str):
            offenders.append(f"{tag} → 返回 {type(out).__name__}，不是 str")
    assert len(_SHAPE_PROBE) >= 20, (
        f"形状集合只剩 {len(_SHAPE_PROBE)} 项了；契约写的是「任何输入」，"
        "用例集合缩到个位数就等于没测")
    assert not offenders, "「任何输入都不抛」被这些形状破了：\n  " + "\n  ".join(offenders)


def test_blank_inputs_get_an_empty_label_not_an_invented_name():
    """空值输入必须回空串。

    为什么单独立一条：回 `''` 和回「未知角色（None）」是两种完全不同的产品行为——
    后者会把 None 打印到用户的下拉框里。这条钉的是「不许编名字」，
    而上面那条只保证「不抛」，两条不重复。
    """
    for value in (None, "", "   ", "\t\n"):
        assert role_label(value) == "", (
            f"输入 {value!r} 拿到了 {role_label(value)!r}：空值不该有名字")


def test_retired_labels_share_one_suffix_built_on_the_live_base_name():
    """退役标签＝现读基名 + 一个三把共用的非空后缀；现役键不许带它（控制腿）。"""
    suffix = _live_retired_suffix()
    for key in ROLE_KEYS:
        label = role_label(key)
        assert not label.endswith(suffix), (
            f"现役角色 {key!r} 被打上退役后缀了：{label!r}")
        assert label == _live_base_name(key), (
            f"现役角色 {key!r} 的标签 {label!r} 不等于现读基名 "
            f"{_live_base_name(key)!r}——展示层开始加戏了")


def test_unknown_inputs_never_claim_a_retired_role_or_say_nothing():
    """未知键与非字符串输入：不许带退役后缀；字符串型未知键还必须把原键带回给用户。"""
    # 「冒充退役角色」的准确含义：标签正好等于某一把真退役键的标签。
    # 不用 endswith(后缀) 判——输入本身就是「已停用」这种字符串时，
    # `未知角色（已停用）` 会以该后缀结尾，那是正确行为，不是冒充（第一版红在这里）。
    retired_labels = {role_label(key) for key in sorted(RETIRED_ROLES)}
    silent, mislabeled = [], []
    for value in list(_UNKNOWN_STRINGS) + [123, 3.5, b"x", ["x"], {"key": "x"},
                                           frozenset({"x"}), object(), Ellipsis]:
        label = role_label(value)
        if label in retired_labels:
            mislabeled.append(f"{value!r} → {label!r}（正好撞上真退役键的标签）")
        if isinstance(value, str) and value.strip() and value not in label:
            silent.append(f"{value!r} → {label!r}（用户看不出是哪个键坏了）")
    assert retired_labels, "退役标签集合为空，这条就没有对照物了"
    assert not mislabeled, "这些输入被冒充成退役角色了：\n  " + "\n  ".join(mislabeled)
    assert not silent, "这些未知键的标签没带回原键名：\n  " + "\n  ".join(silent)



class _HostileBool:
    """`__bool__` 自己抛错的对象：与 `NotImplemented` 同一成因（真值判断）。"""

    def __bool__(self):
        raise RuntimeError("夹具：真值判断故意炸")

    def __repr__(self):
        return "<HostileBool>"


class _HostileRepr:
    """`__repr__` 自己抛错的对象：另一成因——标签把输入格式化进字符串。"""

    def __repr__(self):
        raise RuntimeError("夹具：repr 故意炸")


# 三支现读出来的形状，都不在主组里，各自钉一条「现在会抛」的探测器。
_TRUTHINESS_HAZARDS = (
    ("NotImplemented", NotImplemented),
    ("__bool__ 抛错的对象", _HostileBool()),
)
_REPR_HAZARD = ("__repr__ 抛错的对象", _HostileRepr())
# falsy 的非字符串：今天全部回 ''，靠的正是 `if not key`。
_FALSY_NON_STRINGS = (0, False, range(0), 0.0, -0.0, 0j, b"", (), frozenset(),
                      {}.keys())


def _live_role_labels() -> set:
    """全部真角色的现读标签（公开 + 退役），用来判「解析成功了」。不写中文名。"""
    return {role_label(key) for key in ROLE_KEYS} | {
        role_label(key) for key in sorted(RETIRED_ROLES)}


class _StrShadow:
    """`__str__` 正好返回某个真键的对象：非字符串里唯一可能被查表命中的形状。

    只改 `__str__`、`__repr__` 保持默认——这样它能把「按 str() 查表」这个洞和
    「按 repr 格式化」那个洞分开钉（后者见 `test_a_non_string_is_currently_looked_up_by_its_str_form`
    与 repr 探测器那条）。
    """

    def __init__(self, key: str):
        self._key = key

    def __str__(self):
        return self._key

    def __repr__(self):
        return f"<StrShadow {self._key}>"



def test_builtin_sentinels_take_the_unknown_key_path_like_anything_else():
    """正向常驻断言：builtin 哨兵不特殊，和 `123`、`b'x'` 走同一条未知键路径。

    这条原来是探测器（`NotImplemented` 会抛 TypeError）。task-43 落地后产品满足了
    契约，探测器必须翻成正向断言——留着它就是一条永远绿的假测试。
    期望值全部现读，不写中文：
      · 标签必须非空（哨兵不是「空值」，不许被吞成 ''）；
      · 标签必须把 `str(输入)` 带回给用户；
      · 标签不许等于任何一把真角色的标签（现役或退役都不行）；
      · 六把不同形状的输入，剥掉各自的 `str(value)` 之后剩下的外壳必须**逐字相同**
        ——这一条才真正拦住「给某个哨兵单开分支」。
    """
    live = _live_role_labels()
    assert live, "现读标签集合为空，这条没有对照物"
    shells = set()
    for value in (NotImplemented, Ellipsis, 123, b"x", ["x"], float("nan")):
        label = role_label(value)
        rendered = str(value)
        assert label, f"{type(value).__name__} 的标签不许是空串"
        assert label != rendered, f"标签只剩裸值，界面看不出这是未知键：{label!r}"
        assert rendered in label, f"标签没把原值带回给用户：{label!r}"
        assert label not in live, f"哨兵 {rendered!r} 被解析成真角色：{label!r}"
        shells.add(label.replace(rendered, "\x00", 1))
    assert len(shells) == 1, (
        f"这些非字符串的标签外壳分裂成多份（特例被加回来了）：{sorted(shells)}")


def test_truthiness_sensitive_inputs_land_on_the_unknown_key_path():
    """正向常驻断言：靠真值分流的那一类输入整类闭合（替代原探测器）。

    `NotImplemented` 与任何 `__bool__` 抛错的对象，过去都被 `if not key` 引爆。
    task-43 把判空改成 `key is None` + `str()` 之后，这一类**整类**安全落地——
    不是只治了 builtin 那一个实例，所以这里喂的还是那组 `_TRUTHINESS_HAZARDS`，
    只是判据从「必须抛」翻成「必须不抛且走未知键路径」。
    """
    live = _live_role_labels()
    offenders = []
    for tag, value in _TRUTHINESS_HAZARDS:
        try:
            out = role_label(value)
        except Exception as exc:                            # noqa: BLE001
            offenders.append(f"{tag} → 抛 {type(exc).__name__}: {exc}")
            continue
        if not isinstance(out, str) or not out:
            offenders.append(f"{tag} → {out!r}（非空字符串才算走通未知键路径）")
        elif out in live:
            offenders.append(f"{tag} → {out!r}（被解析成真角色）")
    assert not offenders, (
        "真值敏感输入又开始抛、或又被当成空值/真角色了。"
        "判空大概退回真值判断：\n  " + "\n  ".join(offenders))


def test_only_none_and_blank_input_collapse_to_an_empty_label():
    """正向常驻断言：只有 `None` 与纯空白算空；falsy 的非字符串必须带原值。

    这条原来钉的是「`0`/`False`/`range(0)` 今天也回 ''」——那正是真值判断顺带造成的
    行为，也是 `NotImplemented` 破口的同一个表达式。task-43 之后这一支**按合同翻面**：
    `''` → `未知角色（0）`。从界面角度这反而更有信息量（用户看得见配置里存了个怪值），
    所以断言写成「空值支只许有 `None` 与空白串」：
      · falsy 非字符串：标签非空、且带 `str(value)`；
      · 真空值（`None`/空白串）必须回 `''` —— 那一半由
        `test_blank_inputs_get_an_empty_label_not_an_invented_name` 钉，本条不动它。
    """
    offenders = []
    for value in _FALSY_NON_STRINGS:
        label = role_label(value)
        rendered = str(value).strip()
        if not rendered:
            continue                     # 自身就是空形态的（b''/()）交给空值支判
        if label == "" or rendered not in label:
            offenders.append(f"{type(value).__name__}({value!r}) → {label!r}")
    assert len(_FALSY_NON_STRINGS) >= 8, (
        f"falsy 形状集合只剩 {len(_FALSY_NON_STRINGS)} 项，钉不住整类")
    assert not offenders, (
        "这些 falsy 输入被当成空值吞掉了（界面留白，看不出配置里存了个怪东西）：\n  "
        + "\n  ".join(offenders))


def test_a_non_string_is_currently_looked_up_by_its_str_form():
    """现状登记（**不是我认为的对**）：查表吃的是 `str(输入)`，不是输入本身。

    现读 `core/summary/__init__.py`：`role_label` 与 `get_role` 都先 `str(key).strip()`
    再拿那个字符串比对键表。后果是一个 `__str__` 正好返回键名的对象会被当成合法键：
      `role_label(<str()=='wengao'>)` → 文稿编辑（现读）
      `get_role(<str()=='fenxi'>)`     → 返回那个 Role（现读）
    契约只声明「非字符串按 `str()` 呈现」，**没有**声明「非字符串不许当键用」，
    所以我不能把它写成规格、也不能擅自要求 vendor 加 `isinstance` 分支（那叫加特例）。
    这条钉当前语义，**已报 lead 判**：他若定「查表只认真 `str`」，这条会红，那时翻成
    「非字符串一律走未知键路径 / `get_role` 必须抛 `UnknownRole`」。

    今天没有真实入口踩得到：RPC 与配置里的 role 都是 `str`，所以这是语义洞、不是用户
    可见缺陷——但 `get_role` 是硬拒入口，值域被 `str()` 放宽这件事必须留痕。
    """
    live = _live_role_labels()
    assert live, "现读标签集合为空，这条没有对照物"

    retired = sorted(RETIRED_ROLES)[0]        # 现读一把退役键，不写死名字
    shadow = _StrShadow(retired)
    assert str(shadow) == retired, "夹具造错：__str__ 没返回键名"
    assert role_label(shadow) == role_label(retired), (
        "标签这一支已经不按 str() 查表了——请把这条翻成"
        "「非字符串必须走未知键路径」的正向断言")

    public = sorted(ROLE_KEYS)[0]
    got = get_role(_StrShadow(public))
    assert got.key == public, (
        "get_role 开始拒绝非字符串了——这条该翻成「必须抛 UnknownRole」")
    assert role_label(b"wengao") not in live, (
        "bytes 的 str() 形态不等于键名，不该解析成角色（对照腿：防整类被吞成空值）")


def test_an_input_with_a_broken_repr_currently_raises():
    """变更探测器（第二成因）：标签会把输入格式化进字符串，`__repr__` 抛错就整体抛。

    与真值那条是**两个不同的成因**，task-43 的修法（`str()` 判空）治不到这一支：
    对象没定义 `__str__` 时 `str(obj)` 会回落到 `__repr__`，所以照样抛。要治得给转换
    包一层，或改用 `type(value).__name__` 兜底。我不替产品决定该不该管——这种对象只有
    测试夹具造得出来，真实 RPC 载荷进不来；先钉现状，将来谁改谁看见。
    （已随探测器一起报 lead：契约那句「任何输入都不抛」在这一支**仍未闭合**。）
    """
    tag, value = _REPR_HAZARD
    with pytest.raises(RuntimeError) as exc:
        role_label(value)
    assert "repr" in str(exc.value), (
        f"{tag} 抛了，但不是 repr 那条路径（{exc.value}）——成因变了，请重读")


