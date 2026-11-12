"""前端三态（loading / empty / error）与交互规格的契约。

三态改造的核心不是"多画两个分支"，而是**每种状态说真话**：

1. **加载中不许冒充空态。** 数据还没到就画「还没有任务 / 还没有候选 / 还没有样式」，
   用户会以为东西丢了，于是重新生成一遍——那是白烧一次模型调用。
2. **错误必须带出口。** 报错后唯一的出路如果只剩"再点一次原按钮、而它可能已不在视野里"，
   用户就被困住了。要么给重试，要么在文案里写清该改哪儿。
3. **空态给动作，不给指路。** 「点右下角跑一个演示任务」这类指路，指的地方没有按钮。

字面量判据一律先过 `code_only`：本仓库的注释习惯是把"为什么"写下来，
于是解释「以前这里调 WB.fs.pickFile」的注释里字面上就含 WB.fs。
拿整份文件做子串判断，会把"解释为什么不能这么写"判成"又这么写了"。
"""

from __future__ import annotations

import pytest
from _js_source import block_of, code_only, read_js, web_js_root

# ---------------------------------------------------------------- ui.js 契约


def test_ui_exposes_the_three_state_helpers() -> None:
    source = code_only(read_js("components/ui.js"))
    for name in ("ui.loading", "ui.errorBlock", "ui.errorFrom"):
        assert f"{name} = function" in source, f"ui.js 没有 {name}"


def test_loading_is_announced_to_screen_readers() -> None:
    """`ui.loading` 必须带 role=status + aria-live：转圈对读屏用户是完全静音的。"""
    body = code_only(block_of(read_js("components/ui.js"), "ui.loading = function"))
    assert 'role: "status"' in body, "ui.loading 没有 role=status"
    assert '"aria-live": "polite"' in body, "ui.loading 没有 aria-live=polite"


def test_error_block_is_announced_assertively() -> None:
    """错误块必须 role=alert：它是"此刻需要你处理"的信息，不能排队等。"""
    body = code_only(block_of(read_js("components/ui.js"), "ui.errorBlock = function"))
    assert 'role: "alert"' in body, "ui.errorBlock 没有 role=alert"


def test_only_one_place_decides_whether_to_offer_a_retry() -> None:
    """`retryable` 的判定只有一个**消费者**：`ui.errorFrom`。

    后端 errors.py 的 RETRYABLE_CODES 已经判过"重试有没有意义"。前端要是自己再猜一遍
    （按 code 前缀、按 message 里有没有"超时"），两处判断迟早不一致：后端说不可重试、
    前端给了按钮，用户点一次白等一轮。

    三个豁免各有理由：ui.js 就是那个唯一 owner（本条判据守的就是"别处不许再有"），
    rpc.js 把这个字段**造出来**（RpcError 的构造与解包），mock.js **模拟后端产出**。
    判据盯的是"除 owner 之外，谁还拿它做决定"。
    """
    allowed = {"rpc.js", "mock.js", "ui.js"}
    offenders = []
    web_js = web_js_root()
    for path in sorted(web_js.rglob("*.js")):
        if path.name in allowed:
            continue
        for number, line in enumerate(
            code_only(path.read_text(encoding="utf-8")).split(chr(10)), 1
        ):
            if ".retryable" in line:
                offenders.append(f"{path.relative_to(web_js)}:{number}: {line.strip()}")
    assert not offenders, (
        "retryable 的判定漏到了 ui.js 之外（除了造字段的 rpc.js / 模拟后端的 mock.js）：\n"
        + "\n".join(offenders)
    )


def test_retry_button_appears_only_for_retryable_errors() -> None:
    body = code_only(block_of(read_js("components/ui.js"), "ui.errorFrom = function"))
    assert "retryable" in body, "ui.errorFrom 没有看 retryable —— 不可重试的错误也会长出重试按钮"


# ---------------------------------------------------------------- 页面三态


PAGES_WITH_LOADING = {
    "pages/library.js": "正在读取素材库",
    "pages/merge.js": "正在读取可用来源",
    "pages/summary.js": "正在读取这一条的总结",
    "pages/newtask.js": "正在解析这个链接",
    "pages/styles.js": "正在读取样式列表",
}


@pytest.mark.parametrize("page", sorted(PAGES_WITH_LOADING))
def test_the_page_has_a_real_loading_state(page: str) -> None:
    """每个"首次加载"的页面都必须走 ui.loading，而不是先画空态。"""
    source = code_only(read_js(page))
    assert "ui.loading(" in source, f"{page} 没有加载态"
    assert PAGES_WITH_LOADING[page] in source, (
        f"{page} 的加载文案不是「{PAGES_WITH_LOADING[page]}…」—— 加载态要写清在做什么"
    )


def test_library_no_longer_double_reports_the_same_error() -> None:
    """同一个错误只走一条通道。

    以前 library 的 catch 里既写 `state.error`（画成常驻错误块）又 `ui.toast`（3 秒飘走），
    同一句话在屏幕上出现两次、其中一个还先消失。留块：块带重试按钮，toast 不带。
    """
    source = code_only(read_js("pages/library.js"))
    marker = 'state.error = (err && err.message) || "读不到素材库"'
    start = source.find(marker)
    assert start >= 0, "找不到 library 的错误分支"
    tail = source[start : start + 260]
    assert "ui.toast" not in tail, "library 的读取失败仍然同时 toast 一遍（两条通道说同一句话）"


def test_jobs_empty_state_offers_the_action_instead_of_pointing_at_it() -> None:
    """任务中心空态必须给按钮，不能让人「点右下角」——那里是 toast 区，没有按钮。"""
    source = code_only(read_js("app.js"))
    assert "右下角" not in source, "app.js 的代码里还有「点右下角」这种指错路的文案"
    start = source.find("还没有任务")
    assert start >= 0, "找不到任务中心的空态"
    window = source[start : start + 700]
    assert "跑一个演示任务" in window, "空态里没有那个动作按钮"
    assert "onclick" in window, "空态里的按钮没有绑定动作"


def test_an_empty_job_list_is_not_treated_as_unchanged() -> None:
    """空集合不许被判成「集合没变」。

    事故形状：`sameSet` 写成「任务数 == rows 数 && 每个 job 都有 refs」。
    任务数为 0、rows 也为空时两边都是 0、`every` 在空数组上恒真 ⇒ 判成"没变"
    ⇒ 直接 return ⇒ **首次打开任务中心（一个任务都没有）是一整块空白**。
    空态文案与按钮都在下面那段永远执行不到的分支里，用户以为面板坏了。

    这条判据盯的是机制而不是文案：`sameSet` 必须显式排除空集合，
    或者显式承认"屏幕上已经画着空态"这一种情况。
    """
    source = code_only(read_js("app.js"))
    start = source.find("var sameSet")
    assert start >= 0, "找不到 applyJobs 里的 sameSet 判定"
    body = source[start : start + 420]
    assert "jobs.length > 0" in body or "showingEmpty" in body, (
        "sameSet 在空集合上会判成「没变」—— 空任务列表时任务中心整块空白"
    )


def test_the_disk_slot_has_three_distinct_states() -> None:
    """磁盘那一格：有数 / 读不到 / 还没到，三种输入三种字。

    合成一句（都画成「磁盘 —」）就分不出"没算"和"读不到"——这两件事该做的动作不同。
    """
    body = code_only(block_of(read_js("app.js"), "function paintDisk("))
    assert '"磁盘 ?"' in body, "读不到时没有单独的「磁盘 ?」"
    assert '"磁盘 …"' in body, "还没到数时没有单独的「磁盘 …」"


def test_connection_state_is_its_own_axis() -> None:
    """「后端未连接」不许写进依赖槽：那是两根轴，修的东西完全不同。

    写进依赖槽时用户读到的是「依赖没装」，会去重装 ffmpeg —— 而真实原因是桥没起来。
    """
    source = code_only(read_js("app.js"))
    assert 'getElementById("status-conn")' in source, "app.js 没有独立的连接状态槽"
    assert 'depsNode.textContent = "后端未连接"' not in source, (
        "「后端未连接」还写在依赖槽里 —— 用户会去重装 ffmpeg"
    )
    assert "依赖未知" in source, "连接断开时依赖槽没有自己的说法"


def test_search_shortcut_yields_to_pages_that_already_handled_it() -> None:
    """页面自己处理过的组合键，全局搜索不许再抢。

    字幕页把 Ctrl+F 定义成「批量替换」；两边都监听 document，谁后注册谁生效 ——
    结果是在字幕页按 Ctrl+F，焦点跳到左上角搜索框，替换框根本没开。
    """
    source = code_only(read_js("app.js"))
    start = source.find('document.addEventListener("keydown"')
    assert start >= 0
    body = source[start : start + 420]
    assert "event.defaultPrevented" in body, (
        "全局搜索的 keydown 没有先看 defaultPrevented —— 会抢走页面自己的 Ctrl+F"
    )


# ---------------------------------------------------------------- 交互规格


def test_the_clip_card_honours_its_button_role() -> None:
    """`role="button" tabindex="0"` 是承诺：回车/空格必须等价于点击。

    只写这两个属性、不写 keydown，Tab 到这里就成了"能聚焦但按什么都没反应"的死点 ——
    比不聚焦更糟，因为它先告诉用户"这里能操作"。
    """
    source = code_only(read_js("pages/clips.js"))
    assert 'role: "button"' in source, "候选卡不再声明 role=button"
    start = source.find('card.addEventListener("keydown"')
    assert start >= 0, "候选卡没有 keydown —— role=button 是空承诺"
    body = source[start : start + 420]
    assert '"Enter"' in body, "回车没有绑定"
    assert '" "' in body or "Spacebar" in body, "空格没有绑定"


def test_deleting_a_clip_candidate_asks_first() -> None:
    """删除没有撤销，必须先问一次。以前点了就删，一次误触丢一条调好时间的候选。"""
    source = code_only(read_js("pages/clips.js"))
    start = source.find("function removeCandidate(")
    assert start >= 0
    end = source.find(chr(10) + "  function ", start + 10)
    body = source[start:end]
    assert "ui.confirm(" in body, "removeCandidate 没有确认框"
    assert "clip.delete" not in body, "removeCandidate 里直接发了 clip.delete（确认前就删了）"


def test_the_modal_traps_and_restores_focus() -> None:
    """弹窗：打开移进焦点、Tab 循环、关闭还回原处。

    没有这一条时 `role="dialog" aria-modal="true"` 只是一句声明 ——
    Tab 照样能走到遮罩后面，键盘用户被困在"看不见的地方"。
    """
    source = code_only(read_js("components/ui.js"))
    assert "var trapTab = function" in source, "ui.js 没有 Tab 陷阱"
    assert "opener" in source, "ui.js 没有记录打开前的焦点"
    assert "opener.isConnected" in source, "关闭时没有判断原节点是否还挂在文档里"
    body = code_only(
        block_of(read_js("components/ui.js"), 'var panel = h("div", { class: "modal__panel"')
    )
    assert 'tabindex: "-1"' in body, "panel 没有 tabindex=-1，焦点落不进去"


def test_the_styles_import_uses_the_existing_file_picker_channel() -> None:
    """导入走产品既有的 `fs.pick` RPC，不是某个不存在的 `WB.fs`。

    以前调 `WB.fs.pickFile` —— 全仓没有 WB.fs 这个对象，于是「导入」在任何环境下
    都只弹一句"这个环境没有文件选择器"，用户以为是自己机器缺东西。
    """
    body = code_only(block_of(read_js("pages/styles.js"), "function importJson("))
    assert "WB.fs" not in body, "importJson 还在引用不存在的 WB.fs"
    assert 'WB.rpc("fs.pick"' in body, "importJson 没有走 fs.pick"
