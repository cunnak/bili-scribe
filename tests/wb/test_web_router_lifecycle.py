"""路由生命周期：每个会订阅全局事件 / 起定时器 / 挂 window 监听的页面，
离开时都必须有 destroy（或 unmount）把它们收掉。

为什么值得一条常驻判据：这三类东西**不挂在 DOM 上**，页面被 `view.textContent = ""`
清空时它们一个都不会消失。表现是离开页面之后：

- `job.any` 的订阅还在收事件，继续往已经不在文档里的节点写；
- `setInterval` 还在每 800ms 打一次 `job.get`，跑到 600 次再往空节点写一句错误；
- `window.resize` 上的回调还在算一个已经不存在的画布。

用户看到的是「切走之后风扇还在转」「回来时进度条是别人的」。这几条都不报错，
所以只能靠"这个页面注册了什么，就必须注销什么"的机械对账来守。

判据读源码（页面模块都是 IIFE，没有可导入的对象），因此**不启动浏览器**，
在离线全量里也能跑。
"""

from __future__ import annotations

import re

import pytest
from _js_source import read_js


def _page_source(name: str) -> str:
    return read_js(f"pages/{name}.js")


def _route_block(source: str, route: str) -> str:
    """取 `WB.router.define("<route>", { ... });` 那一段的原文。

    用括号配平而不是正则：对象体里嵌着函数、数组、对象字面量，
    正则截出来的东西经常少半截，而"少半截"在读 destroy 时恰好会读成"没有 destroy"。
    """
    marker = f'WB.router.define("{route}"'
    start = source.find(marker)
    assert start >= 0, f"找不到路由定义：{route}"
    brace = source.find("{", start)
    assert brace >= 0, f"路由定义没有对象体：{route}"
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"路由定义的对象体没有闭合：{route}")


# 页面 → 它注册到全局的、离开时必须收掉的东西。名单来自逐页读源码，不是猜的。
GLOBAL_REGISTRATIONS = {
    "clips": [
        ("setInterval", "轮询计时器"),
        ('window.addEventListener("resize"', "window resize 监听"),
    ],
    "merge": [
        ('WB.on("job.any"', "job.any 事件订阅"),
    ],
    "summary": [
        ("setTimeout(tick", "轮询计时器"),
    ],
}


@pytest.mark.parametrize("page", sorted(GLOBAL_REGISTRATIONS))
def test_a_page_that_registers_globals_has_a_destroy_hook(page: str) -> None:
    """注册了全局东西的页面，路由定义里必须有 destroy。"""
    block = _route_block(_page_source(page), "clipper" if page == "clips" else page)
    assert re.search(r"\bdestroy\s*:", block), (
        f"{page}.js 注册了全局对象却没有任何 destroy —— "
        f"离开页面后它们会继续跑：{GLOBAL_REGISTRATIONS[page]}"
    )


@pytest.mark.parametrize("page", sorted(GLOBAL_REGISTRATIONS))
def test_the_destroy_hook_actually_releases_what_was_registered(page: str) -> None:
    """destroy 里必须逐条出现「注销那一件事」的调用，不能只是个空函数。

    只查"有没有 destroy" 挡不住 `destroy: function () {}` —— 那是最容易写出来的
    假交付：路由会照常调它，看起来"已经处理了"，实际什么都没收。
    """
    source = _page_source(page)
    block = _route_block(source, "clipper" if page == "clips" else page)
    destroy_at = block.find("destroy")
    assert destroy_at >= 0
    body = block[destroy_at:]

    expected = {
        "clips": ["stopPolling", "removeEventListener"],
        "merge": ["unwatchJob"],
        "summary": ["pollToken"],
    }[page]
    for token in expected:
        assert token in body, (
            f"{page}.js 的 destroy 里没有 {token} —— 那一项没有被真的收掉，只是写了个空钩子"
        )


def test_publish_has_an_unmount_because_it_mounts_into_another_page() -> None:
    """publish 不是独立页，它挂进总结页的两个容器，所以它需要的是 unmount 而不是 destroy。

    没有它：`state.panel` 指着已经随页面丢掉的节点、`state.loaded` 还记着
    "这批数据已经读过" ⇒ 下次进总结页直接 paint 到脱离文档的节点上，屏幕上什么都不变。
    """
    source = _page_source("publish")
    assert re.search(r"function\s+unmount\s*\(", source), "publish.js 没有 unmount"
    assert re.search(r"\bunmount\s*:\s*unmount\b", source), (
        "unmount 没有从 WB.pages.publish 暴露出去"
    )
    assert "state.panel = null" in source, "unmount 没有把 panel 清空"
    assert "state.loaded = false" in source, (
        "unmount 没有把 loaded 复位，下次进页面会拿到上一条视频的缓存"
    )


def test_summary_calls_publish_unmount_when_it_leaves() -> None:
    """总结页离开时必须替 publish 调一次 unmount：publish 自己不挂在路由上。"""
    block = _route_block(_page_source("summary"), "summary")
    assert "unmount" in block, "summary 的 destroy 没有调 publish.unmount"


def test_the_router_calls_destroy_before_clearing_the_view() -> None:
    """路由必须**先**调上一页的 destroy，再清空视图。

    顺序反了的话，destroy 里的代码想读一下自己页面的节点（例如"把滚动位置存起来"）
    就只能读到已经清空的东西 —— 那类 bug 不报错，只是悄悄丢了状态。
    """
    source = read_js("router.js")
    destroy_at = source.find("previous.destroy()")
    clear_at = source.find('view.textContent = ""')
    assert destroy_at >= 0, "router.js 没有调用上一页的 destroy"
    assert clear_at >= 0, "router.js 没有清空视图"
    assert destroy_at < clear_at, "router.js 先清空视图再调 destroy —— 顺序反了"


def test_destroy_failure_does_not_block_navigation() -> None:
    """destroy 抛异常只能 warn，不能把用户卡在旧页面上。

    任何一页的清理代码都可能踩到"节点已经不在"（第三方组件、老 WebView 的 API 差异），
    那时如果异常往上冒，整个路由就断在半路 —— 用户点左栏没有任何反应。
    """
    source = read_js("router.js")
    destroy_at = source.find("previous.destroy()")
    window = source[max(0, destroy_at - 260) : destroy_at + 400]
    assert "try {" in window, "router.js 调 destroy 时没有 try"
    assert "catch" in window, "router.js 调 destroy 时没有 catch"
    assert "console.warn" in window, "destroy 失败时没有留下任何痕迹，排查时无从下手"
