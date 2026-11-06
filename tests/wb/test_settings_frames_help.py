"""设置页「采集」分区那两句话的判据（task-70 的界面腿）。

为什么单独一个文件：task-70 把画面帧真接进总结链路之后，`collect.frames.enabled`
不再是假开关；但界面上留着两句**实测里没有的收益**——

- `settings.js` 旧 help 写「关掉可明显提速」：今天关掉的只有「12 次本地 ffmpeg 抽帧」
  与「约 1,464 image_tokens」，整条链路的时间大头在模型侧，**提速没有实测**。
- `settings.js:144` 旧 hint 写「省流量就把截帧关掉」：帧是从**已经下载到本地**的源视频抽的，
  不产生任何网络流量，这句连方向都是错的。

两句都在讲同一件事（截帧的代价），按删除测试只留一句：help 说清代价，hint 退回分区清单。
所以本文件钉的是「不许再出现这两句空口的收益承诺」，不是钉新文案的每一个字——
措辞再改不该红，旧口径回来必须红。

截图走真 chromium（与 `test_settings_role_warning_ui` 同一套夹具），两张：桌面 1440×960
与**产品窗口下限**（视口取 `desktop.MIN_SIZE`，窄态没有别的合法形状），落 `build/accept/`。
**浏览器起不来是 skip，不是通过。**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_settings_role_warning_ui import (
    ACCEPT_DIR,  # noqa: F401  截图落点（仓库内，可复跑）
    PATCH_JS,  # noqa: F401  load 之后包 WB.rpc 的注入脚本
    base_url,  # noqa: F401  去掉路由片段的基础 URL
)
from test_settings_role_warning_ui import browser as browser  # noqa: F401,E402  （pytest fixture）
from test_settings_role_warning_ui import web_base_url as web_base_url  # noqa: F401,E402  （pytest fixture）

from video_workbench.desktop import MIN_SIZE  # 窄态视口的唯一来源，不写死数字

SETTINGS_JS = (
    Path(__file__).resolve().parents[2] / "video_workbench" / "web" / "js" / "pages" / "settings.js"
)

DESKTOP = {"width": 1440, "height": 960}

#: 窄的那一张不按手机尺寸测，按**产品自己的窗口下限**测：
#: `video_workbench.desktop.MIN_SIZE`（task-72 已实测 WebView2 会强制这个外层下限，
#: 用户拖不到比它更窄），所以「窄态」= 这个值减去边框，而不是 390×844。
#: 390 宽下实测 `label.field__label` 解析得到但不可见——那是产品没有手机布局，
#: 属「要不要支持手机宽度」这条待定决策的证据，不在这儿当缺陷判。
NARROW = {"width": MIN_SIZE[0], "height": MIN_SIZE[1]}


def open_collect(browser, web_base_url: str, viewport: dict):
    """进「设置 → 采集」，返回 (page, 控制台错误列表)。"""
    page = browser.new_page(viewport=viewport)
    errors: list[str] = []

    def on_console(message) -> None:
        if message.type != "error":
            return
        text = message.text
        if "favicon" in text.lower():  # 仓库没有 favicon.ico，那条 404 与产品无关
            return
        errors.append(f"[console] {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
    page.goto(base_url(web_base_url), wait_until="load")
    page.wait_for_function("() => window.WB && typeof window.WB.rpc === 'function'", timeout=15000)
    page.evaluate(PATCH_JS, {})  # 空 stub：一个字段都不盖，全走 mock 的真实载荷
    page.evaluate('() => window.WB.router.go("settings", {section: "collect"})')
    page.wait_for_selector("label.field__label", timeout=15000)
    return page, errors


def frames_row(page):
    """「截帧」那一行。必须**全等**筛 label：同一分区还有一条「每稿最多截帧」，
    子串筛会先命中它，那时测的就是数字行而不是开关行。"""
    label = page.locator("label.field__label").filter(has_text=re.compile(r"^截帧$")).first
    assert label.count() == 1, "找不到唯一的「截帧」label——分区字段表变了，本文件的靶子也变了"
    return label.locator(
        "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' field ')][1]"
    )


def help_text(row) -> str:
    help_node = row.locator(".field__help")
    assert help_node.count() == 1, "「截帧」行只该有一条 help，现在不是 1 条"
    return help_node.inner_text().strip()


# ---------------------------------------------------------------- 代码层：旧口径不许回来


def test_the_frames_help_no_longer_promises_an_unmeasured_speedup():
    """源码级：两句空口收益不许回来（不看浏览器，改文案的人第一时间就该被拦）。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    assert "可明显提速" not in src, "「关掉可明显提速」回来了：这个收益今天没有实测"
    assert "省流量就把截帧关掉" not in src, (
        "「省流量就把截帧关掉」回来了：帧从本地源视频抽，不省流量"
    )
    # 新那句必须还在，否则上面两条会退化成「两句都被删了」的假绿
    assert "关掉省图片 token" in src, "help 被整句删掉了：开关的代价现在没人说"


def test_the_section_hint_does_not_repeat_the_help():
    """hint 与 help 是两句话讲同一件事——删除测试：留 help，hint 退回分区清单。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    hint_line = next(line for line in src.splitlines() if "字幕、弹幕、截帧" in line)
    assert "截帧" in hint_line, f"hint 行形状变了：{hint_line}"
    assert "关" not in hint_line, f"hint 又开始教用户关开关了：{hint_line}"


# ---------------------------------------------------------------- 浏览器层：真渲染 + 截图


@pytest.mark.parametrize(
    ("name", "viewport"),
    [("desktop", DESKTOP), ("narrow", NARROW)],
    ids=["desktop", "window-floor"],
)
def test_the_frames_help_renders_in_the_collect_section(browser, web_base_url, name, viewport):
    page, errors = open_collect(browser, web_base_url, viewport)
    try:
        row = frames_row(page)
        row.scroll_into_view_if_needed()
        text = help_text(row)

        assert "图片 token" in text, f"开关的代价没说出来（界面实得）：{text!r}"
        assert "提速" not in text and "省流量" not in text, f"空口收益被渲染出来了：{text!r}"
        # help 必须真看得见，不是在 display:none 里躺着（同族规则：界面字段要可达）
        box = row.locator(".field__help").evaluate(
            "el => ({h: Math.round(el.getBoundingClientRect().height)})"
        )
        assert box["h"] > 0, f"help 高度为 0，用户看不见：{box}"

        assert errors == [], f"采集分区渲染时报错：{errors}"

        ACCEPT_DIR.mkdir(parents=True, exist_ok=True)
        shot = ACCEPT_DIR / f"settings-collect-{name}.png"
        page.screenshot(path=str(shot), full_page=False)
        assert shot.is_file() and shot.stat().st_size > 5_000, f"截图没落盘或太小：{shot}"
    finally:
        page.close()
