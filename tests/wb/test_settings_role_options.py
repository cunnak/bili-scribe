"""判据 4a/4b/4c：设置页角色下拉框的两份名单各自守什么（口径为什么换掉，写在下面）。

## 原口径被撤销的原因（lead 定，不是我挑的）

原合同要求「`settings.js` 里不得出现角色 key 的字面量列表」，即把兜底表删干净、
下拉框完全由后端注入。vendor 的牙齿证明那样做会打挂另一件事：后端 reject 时
`options: []`，设置页角色那一栏空转，用户连「换个角色试试」都点不到 ——
那不是更干净，是少了一条退路。所以换成三条**能分别红**的判据：

* **4a 正常路径用的是后端返回值。** 注入一份与兜底表不同的名单
  （改顺序 + 换一个后端真实不存在的 key），界面必须跟着那份走。
  这条红了 = 有人把兜底表接回了正常渲染路径（两个 owner 各说各话）。
* **4b 后端 reject 时兜底表可用。** `summary.roles` 失败 → 选项非空、表单照常、零 error。
  这条红了 = 兜底表被删了，或者 reject 时整页塌掉。
* **4c 兜底表 ⊆ 后端 `ROLE_KEYS`，且相对顺序一致，且不含退役键。**
  这条是 task-24 那次漏更的常驻门：后端加了 `shipin`、前端表没跟，
  用户在设置页就选不到新角色 —— 靠人记得同步不可靠，靠这条可靠。

三条都只读产品源码、只改我这一侧的桥返回值，**不改任何产品文件**。
读法都用真后端（`WorkbenchService`）而不是 mock：`settings.js:308` 那句
`roleOptionsOf = roleOptionList || field.options` 里，`roleOptionList` 只有真后端
答回来才会有值，mock 模式下永远非空 —— 用 mock 测 4b 会测成「mock 的兜底」，
和产品的兜底不是一回事。

产品侧的三个锚点（现读，行号是 2026-11-04 的）：
`settings.js:308 roleOptionsOf()` / `:1536 roleOptionsFrom()`（空列表返回 `null` = 触发兜底；
标签按 `passes>=3` 打「（三轮）/（单轮）」）/ `:55-70` `llm.role` 字段里那张 `options:` 兜底表。

## 牙齿配方（本轮全部跑过，输出贴在任务板 acceptanceResults 第 3 条）

坏法只两种载体：**运行时 pytest 插件**（`-p`，改模块属性）与 **`%TEMP%` 下我自己建的
哨兵目录里的 web 副本**（`WB_WEB_ROOT` 指过去）。产品文件 sha256 开工前后一字不差，
控制组（原样副本）必须绿 —— 否则红可能来自「换了个目录」而不是坏法。
驱动器与五个插件留在 `%TEMP%/parity-t25d/`（`t25_teeth.py` + `t25_s1/s2/s3/s4/s6.py`），
被清掉的话照下面五行重建：

| 变体 | 坏法（一处最小改动） | 该红的名字 |
|---|---|---|
| 4a | JS：`return roleOptionList || (field && field.options) || [];` → 两段顺序互换 | `follow_the_backend` |
| 4b | JS：`llm.role` 那张 `options: [[…活角色若干项…]],` → `options: [],` | `keeps_the_page_usable`（连带 `ordered_subset`） |
| 4c | JS：兜底表**前两项互换**（集合不变、只漂顺序；当前前两项是 `renhua`/`shensi`，task-31 之前是 `xueshu`/`renhua`） | `ordered_subset` |
| OWNERS | JS：`describeRoleSources` 里 `if (item.prompt_source) {` → `if (false) {` | `exactly_one_owner` |
| 控制组 | JS：原样副本，不改 | —— 必须一条都不红 |

跑法：`WB_WEB_ROOT=<Temp 副本> .venv/Scripts/python.exe -m pytest tests/wb/test_settings_role_options.py -q`。
4c 的坏法专门挑「集合相同、顺序不同」这一形，因为 4b 那种删表是合同点名的坏法，
同类的另一种坏法（顺序漂）如果测不到，就说明我只防了一种形状。
"""

from __future__ import annotations

import importlib
import json
import re

from test_summary_real_backend_ui import (
    browser as browser,  # pytest fixture（透传给 real_page）
)

# 桥、fixture 与「看得见」的定义都从真后端那份复用：两处对同一件事只许有一套读法。
from test_summary_real_backend_ui import (  # noqa: F401
    real_page as real_page,  # pytest fixture：真 service + 已走进 设置→模型 的页面
)
from test_summary_real_backend_ui import (
    real_service as real_service,  # pytest fixture（透传给 real_page）
)
from test_summary_real_backend_ui import (
    web_base_url as web_base_url,  # pytest fixture（透传给 real_page）
)

from video_workbench.core import prompt_source as ps

NL = chr(10)
RETIRED_VALUE = "standard"


def _pkg():
    return importlib.import_module("video_workbench.core.summary")


def _role_select(page):
    return page.locator(".field", has_text="默认角色").locator("select").first


def _option_keys(page) -> list[str]:
    return [
        k for k in _role_select(page).locator("option").evaluate_all("els => els.map(e => e.value)")
    ]


def _reopen_settings(page, backend):
    """让设置页**重新读一次后端**，并证明这次读的是真后端。

    走的是用户路径（离开→再回来），不是刷新：
    `settings.js` 的 `load()` 每次进页都会 `Promise.all([app.settings.get, refreshRoles()])`，
    所以换路由一轮就能让 `summary.roles` 重新过一次我已经改好的桥。
    为什么不 `page.reload()`：刷新会把 fixture 打好的那层 `WB.rpc` 包装冲掉
    （`expose_binding` 的 `__wbRpc` 活下来，包装却没了），页面掉回 mock ——
    上一轮我就是这么错的：4a 红在「界面用的是兜底表」，真正的原因是我把它掉回 mock 了。

    返回桥收到的 `summary.roles` 次数增量；调用方断言它 **> 0**，
    否则整条用例比的是 mock 名单，绿灯毫无意义。
    """
    before = backend.bridge_calls.count("summary.roles")
    page.evaluate('() => window.WB.router.go("library")')
    page.wait_for_timeout(150)
    page.evaluate('() => window.WB.router.go("settings", {section: "llm"})')
    page.wait_for_selector("label.field__label", timeout=15000)
    page.wait_for_timeout(250)  # 等这一次 summary.roles 的回包把选项画完
    return backend.bridge_calls.count("summary.roles") - before


def _refusing_factory(backend):
    """把 `summary.roles` 换成 reject，其余方法照走真后端。"""
    original = backend.dispatch

    def refusing(method, params=None):
        if method == "summary.roles":
            return {
                "ok": False,
                "request_id": None,
                "error": {"code": "TEST", "message": "这条用例故意让后端不答"},
            }
        return original(method, params)

    return original, refusing


# ==================================================== 4a：正常路径吃的是后端返回值


def test_the_role_options_follow_the_backend_when_it_answers(real_page):
    """4a：后端返回什么名单，下拉框就是什么名单（**顺序也跟**）。

    注入的名单与兜底表有两处不同：顺序打乱、换成一个后端表里根本没有的 key
    （`zzINJECTED`）。只比集合不够 —— 兜底表和后端表集合相同、顺序不同时，
    只有顺序断言能分辨界面用的是哪一份。
    标签也必须来自后端（`roleOptionsFrom` 按 `passes` 打「三轮/单轮」），
    所以顺手钉住注入项的显示文案，证明不是只把 value 换了。
    """
    page, errors, backend, _ = real_page
    original, refusing = _refusing_factory(backend)

    # 注入项现读：一个不可能的键（zzINJECTED，故意的未知值）+ 退役集合里的两个键。
    # 原来这里写死 `["zzINJECTED", "shipin", "xueshu"]`：那两个键**当时**是「退役但仍在
    # 历史行里」的样本，今天仍然成立，但写死名单会让下一次退役又红一轮。
    # 「退役集合等于哪三个键」由 `test_summary_retired_roles.py` 钉一份，这里只取它的投影。
    from video_workbench.core.summary import RETIRED_ROLES as _RETIRED

    injected = ["zzINJECTED", *sorted(_RETIRED)[:2]]
    assert len(injected) == 3, f"注入项不足三个（当前退役集只有 {sorted(_RETIRED)}）"

    def patched(method, params=None):
        envelope = original(method, params)
        if method == "summary.roles" and envelope.get("ok"):
            rows = envelope["data"]["roles"]
            envelope["data"]["roles"] = [
                dict(row, key=key, name=key, passes=1) for key, row in zip(injected, rows)
            ]
        return envelope

    backend.dispatch = patched
    try:
        delta = _reopen_settings(page, backend)
        assert delta > 0, (
            "改完桥之后页面一次都没重新读 summary.roles —— 下面的比较是在拿旧渲染结果"
            "或 mock 名单说话，没有意义"
        )
        keys = _option_keys(page)
        assert keys == injected, (
            f"后端可用时下拉框没有逐位照抄后端的名单（得到 {keys}，注入的是 {injected}）——"
            "兜底表混进了正常渲染路径，或者顺序被本地重排了"
        )
        labels = (
            _role_select(page).locator("option").evaluate_all("els => els.map(e => e.textContent)")
        )
        assert all(label.endswith("（单轮）") for label in labels), (
            f"注入项 passes=1，标签却没按后端给的轮数打：{labels}"
        )
    finally:
        backend.dispatch = original
    assert errors == [], f"控制台有 error：{errors}"


# ================================================== 4b：reject 时兜底表必须可用


def test_the_fallback_table_keeps_the_page_usable_when_the_backend_refuses(real_page):
    """4b：`summary.roles` 失败时，角色一栏**仍有选项**、表单不塌、零 error。

    `settings.js:59-63` 那张表被注释成「后端挂了也照常可用」，这条就是那句承诺的常驻证明。
    三个面分开钉，坏法不同：
      * 选项为空 → 表被删了（用户在这台机器上换不了角色）；
      * 选项里有后端不认识的 key → 选了发不出去（兜底表不许自己造角色）；
      * 「读不到设置」横幅出现 → 一个方法挂掉把整页带走了。
    顺带钉住选中值退回配置原值本身（`roleSelectValue`：读不到后端 `default` 时
    「宁可不选，也不自己编一个」）—— 编一个默认值等于替用户改配置。
    """
    page, errors, backend, config_path = real_page
    original, refusing = _refusing_factory(backend)
    configured = json.dumps(backend.call("app.settings.get"), ensure_ascii=False)
    backend.dispatch = refusing
    try:
        delta = _reopen_settings(page, backend)
        assert delta > 0, "页面没有重新读 summary.roles，reject 这条压根没被执行"
        keys = _option_keys(page)
        assert keys, (
            "后端 reject 后角色下拉框一个选项都没有：兜底表被删了 —— "
            "这正是原口径（删掉字面量表）会造成的后果"
        )
        live = set(_pkg().ROLE_KEYS)
        assert set(keys) <= live, f"兜底表里有后端不存在角色：{set(keys) - live} —— 选了也发不出去"
        assert not page.get_by_text("读不到设置").count(), (
            "只挂了一个方法，整页不许塌（summary.roles 的失败不能升级成页面级错误）"
        )
        selected = _role_select(page).evaluate("el => el.value")
        assert selected in keys, (
            f"reject 时选中值 {selected!r} 不在选项里（DOM 会静默退到第一项，"
            f"用户顺手一保存就被改了配置）；配置原文：{configured[:120]}"
        )
    finally:
        backend.dispatch = original
    assert errors == [], f"控制台有 error：{errors}"


# ============================== 4c：兜底表是后端名单的有序子集，且不含退役角色


def _fallback_table_keys() -> list[str]:
    """从产品源码里抠出 `llm.role` 那张 `options:` 表的 key 序列。

    为什么要抠源码而不是读 DOM：DOM 在正常路径下是**后端**名单，
    读 DOM 得到的是 4a 的答案，测不到这张表本身漂没漂。
    抠法限定在 `llm.role` 那一段里（`path: "llm.role"` 到下一个 `path: "`），
    不拿整个文件里所有 `["x","y"]` 形状去猜 —— 那样任何一个 select 加选项都会误报。
    """
    import test_settings_role_warning_ui as si

    source = (si.WEB_DIR / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
    at = source.index('path: "llm.role"')
    nxt = source.index('path: "', at + 1)
    block = source[at:nxt]
    pairs = re.findall(r'\[\s*"([A-Za-z_][\w-]*)"\s*,\s*"[^"]*"\s*\]', block)
    assert pairs, f"在 llm.role 那一段里抠不到 options（规则已不适用）：{block[:200]!r}"
    return pairs


def test_the_static_fallback_is_an_ordered_subset_without_retired_keys():
    """4c：兜底表 ⊆ `ROLE_KEYS`、相对顺序一致、不含退役键、无重复。

    顺序为什么要一致：下拉顺序同时是历史版本对比里角色排列的依据，
    两边各排各的会让「同一份总结在两个界面里角色位置不同」这种小事变成排查半天的事。
    允许真子集：后端可以比这张表多角色（新增时先改后端也能用），
    那种情况由 4a/4b 管可见性，不该在这里红 —— 在这里红等于惩罚渐进式发布。
    """
    table = _fallback_table_keys()
    live = list(_pkg().ROLE_KEYS)
    assert len(table) == len(set(table)), f"兜底表自己有重复项：{table}"
    for key in table:
        assert key in live, f"兜底表里的 {key!r} 不在后端 ROLE_KEYS：选了发不出去"
    assert table == [k for k in live if k in set(table)], (
        f"兜底表相对顺序与后端不一致：前端 {table} vs 后端 {[k for k in live if k in set(table)]}"
    )
    assert RETIRED_VALUE not in table, (
        f"兜底表里还留着已退役的 {RETIRED_VALUE}：reject 路径会把用户送回去"
    )
    # 附加提示词的角色名单也来自同一处：这张表不许把 extra 文件当角色列进来。
    assert ps.EXTRA_FILE not in table, "兜底表把附加提示词文件当成角色了"
