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
    files = sorted(js_dir.rglob("*.js"))
    assert files, f"没找到任何 js，这条空转了：{js_dir}"
    hits = [(f.relative_to(js_dir), ln)
            for f in files
            for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            if "retired_labels" in line]
    assert hits == [], (
        "前端开始吃 retired_labels —— 那是 CLI 的字段，"
        f"换成对象数组会让 indexOf 恒 -1：{hits}")
    # 控制腿：前端必须**还在**吃裸 key 那份，否则上一条的「0 命中」可能只是因为
    # 整个退役显示被人删了。
    page = (js_dir / "pages" / "summary.js").read_text(encoding="utf-8")
    assert "data.retired" in page, (
        "summary.js 不再读 `retired` —— 退役显示整块没了，这条锁要重新设计")


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
