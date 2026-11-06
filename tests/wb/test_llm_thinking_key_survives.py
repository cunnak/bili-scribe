"""判据 6/7/8：「深度思考」控件撤掉了，但 `llm.thinking` 这个键必须活着。

两半方向相反，任何一半退化都必须红：

* **判据 6（界面上不该再有它）**：`video_workbench/web/js/pages/settings.js:90-93` 的注释
  把理由写得很清楚 —— WB 这条路径上没有任何出网口读这个键，而 magpie 实测不透传
  `enable_thinking`（带与不带都烧满 reasoning tokens）。留着那个按钮等于给用户一个
  会说谎的旋钮：他以为选了「关」就不思考了。所以 task-26 把控件撤了。
* **判据 7（文件里不许丢它）**：`bili_scribe/infra/llm.py` 真会按它发参数，
  `~/.bili-scribe` 时代存下的配置也带着它。搬家（task-23）之后如果 WB 的 `save()`
  把它抹掉或重置成 `auto`，用户旧文件里那句 `off` 就无声消失了 ——
  那不是撤一个控件，那是改了用户的配置。

## 为什么判据 6 不能照合同字面写

合同给的锚点是 `[data-path="llm.thinking"]`。**这个属性在产品里根本不存在**：
`grep -n "data-path" video_workbench/web/js/pages/settings.js` 零命中 —— `field.path`
只用于 `current[field.path]` / `markDirty(field.path, …)` 这些 JS 侧的键（`:421/:495/:1232`），
从不写进 DOM；DOM 里带 `dataset` 的只有左侧分区导航（`:562 dataset: {key: section.key}`）。
所以那条选择器在**任何**页面上都计数 0，照抄进用例就是一条永远为真的断言
（我上一轮真的写出了这种用例，靠跑了一遍才发现它绿得没有理由）。
可观测的锚点换成字段 label：`label.field__label` 是 `row()` 给每个字段渲染的文本，
「深度思考」如果回到界面上，一定会出现在这里。同一批 label 里必须有「温度」——
那是正对照，证明扫描真在扫，而不是页面压根没渲染。
源码侧则钉 `SECTIONS` 的 llm 段（`:25` 起，字段定义的唯一来源）。

## 本文件两条判据的牙齿（实跑输出在任务板 acceptanceResults）

* **S-5 把控件加回来**：Temp web 副本的字段表里，在 `{ path: "llm.temperature", label: "温度"`
  之前插一行 `{ path: "llm.thinking", label: "深度思考", type: "select", … }` →
  源码侧与页面侧**两条都红**（`exit=1 红了 2 条`）。
  副本目录先做**控制组**（原样不改）：0 红 —— 证明红来自这一行插入，不来自换了个目录。
* **S-6 让保存吞掉键**：运行时插件替换 `video_workbench.config.render_yaml`，
  把含 `thinking` 的行全部滤掉（`config.py:903` 是 `fh.write(render_yaml(self))`，
  按模块全局名解析，所以替换名字就精确复现了「模板不认识这个键」）→
  只红 1 条：`test_an_unrelated_save_carries_the_thinking_value_verbatim`。

坏法与恢复都只在 `%TEMP%/parity-t25d/` 下我自己建的哨兵目录里做；
8 个产品文件（含 `settings.js`、`mock.js`、`config.py`）sha256 开工前后一字不差。
判据 8 那条「`mock.js` 里不许删掉 thinking 形状」是 S-5 的同族：谁把它从 mock 里删了，
`test_the_key_still_lives_in_the_config_class_and_the_written_file` 第三条红。
"""

from __future__ import annotations

import importlib
import json
import re

import pytest
from test_settings_role_warning_ui import WEB_DIR, require_expanded  # noqa: E402

# fixture 与桥的搭法都从真后端那份复用；页面侧「读得到」的定义从 mock 那份复用。
from test_summary_real_backend_ui import (  # noqa: F401
    browser as browser,
)
from test_summary_real_backend_ui import (
    real_page as real_page,
)
from test_summary_real_backend_ui import (
    real_service as real_service,
)
from test_summary_real_backend_ui import (
    web_base_url as web_base_url,
)

NL = chr(10)
THINKING_PATH = "llm.thinking"
THINKING_LABEL = "深度思考"


def _settings_source() -> str:
    return (WEB_DIR / "js" / "pages" / "settings.js").read_text(encoding="utf-8")


def _llm_field_paths(source: str) -> list[str]:
    """抠出 `SECTIONS` 里 `key: "llm"` 那一段的全部字段路径（按源码顺序）。

    两张表都不是正则能一行搞定的：`SECTIONS`（`:25`）里每个分区自带 `fields: [...]`，
    字段里又嵌着 `options:` 数组和长 help 文本，所以按括号配平来取，
    不拿「找到第一个 `];`」当结束 —— 那会在第一个嵌套中括号处截半。

    **没有 `LLM_FIELDS` 这个名字**（我上一轮的笔记记错了，实测 grep 无命中），
    表就叫 `SECTIONS`，字段路径也**从不写进 DOM**（见下面那条页面侧用例的注释）。
    """
    at = source.index('key: "llm"')
    opener = source.index("fields: [", at) + len("fields: ")
    depth = 0
    for i in range(opener, len(source)):
        if source[i] == "[":
            depth += 1
        elif source[i] == "]":
            depth -= 1
            if depth == 0:
                return re.findall(r'path:\s*"([^"]+)"', source[opener : i + 1])
    raise AssertionError("SECTIONS 的 llm 段没有闭合，抠取规则已经不适用")


# ============================================== 判据 6：控件不许自己长回来（源码侧）


def test_the_retired_thinking_control_is_not_in_the_field_table():
    """`SECTIONS` 的 llm 段里不许有 `llm.thinking`；`llm.temperature` 必须在（自证规则没空转）。

    这条红了 = 有人把 task-26 撤掉的旋钮加回来了。那种改动不会报错、不会挂测试，
    只会让用户再次得到一个说谎的开关，所以它需要一条常驻门。
    """
    paths = _llm_field_paths(_settings_source())
    assert THINKING_PATH not in paths, (
        f"「深度思考」控件回到设置页字段表里了（现有 {paths}）——"
        "WB 的总结路径没有出网口读它，magpie 也不透传，这个旋钮只会说谎"
    )
    assert "llm.temperature" in paths, (
        f"抠取规则失效：连 llm.temperature 都没读到（实得 {paths}），上一条断言因此空转"
    )


# =============================================== 判据 6：真页面上那条 CSS 选择器


def test_the_rendered_page_has_no_thinking_control(real_page):
    """页面侧：字段 label 集合里不许出现「深度思考」，且必须有「温度」（正对照）。

    合同那条 `[data-path="llm.thinking"]` 在这里**不能用**，理由见文件头：
    那个属性在本产品里不存在，任何页面都会返回 0，是一条恒真断言。
    改成读 `label.field__label` 的文本集合：控件若从别的表长回来，label 一定出现。

    顺带钉住「键确实还在后端载荷里」——所以这条红了不是「配置被删了」，
    而是「控件自己长回来了」；两种红的修法完全不同，必须能在报错里分辨。
    """
    page, errors, backend, _ = real_page
    labels = page.locator("label.field__label").evaluate_all(
        "els => els.map(e => e.textContent.trim())"
    )
    assert labels, "设置页一个字段 label 都没读到：页面没渲染，下面的断言全是空转"
    assert THINKING_LABEL not in labels, (
        f"「{THINKING_LABEL}」回到设置页了（现有 {labels}）——"
        "WB 的总结路径没有出网口读它，magpie 也不透传，这个旋钮只会说谎"
    )
    assert "温度" in labels, f"正对照失效：连「温度」都没读到（实得 {labels}），上面那条因此不可信"
    payload = json.dumps(backend.call("app.settings.get"), ensure_ascii=False)
    assert '"thinking"' in payload, (
        "后端载荷里 thinking 键没了 —— 那不是撤控件，是把用户的配置抹了，"
        "判据 7/8 的另一半现在就该红"
    )
    assert errors == [], f"控制台有 error：{errors}"


# ========================================== 判据 8：键还在配置类 / 写盘 / mock 三处


def test_the_key_still_lives_in_the_config_class_and_the_written_file(tmp_path):
    """`thinking` 必须同时活在 `LlmConfig` 的字段里、`render_yaml` 的输出里、mock 的形状里。

    三处是三个不同的丢失方式：
      * 从 dataclass 删字段 → 搬家与旧文件直接读不进来（task-23 的逐字段搬运会少一列）；
      * `render_yaml` 不再写它 → 保存一次就静默丢；
      * mock 里没有 → 界面用例的形状与真后端不一致（合同要求 mock 保持原样，
        所以这一半只是「别把它从 mock 里删掉」的守门）。
    """
    from dataclasses import fields

    from video_workbench.config import LlmConfig, WorkbenchConfig, render_yaml

    names = {f.name for f in fields(LlmConfig)}
    assert "thinking" in names, (
        f"LlmConfig 里已经没有 thinking 字段（现有 {sorted(names)}）——"
        "那等于把用户配置里的这一列删了，不是撤控件"
    )

    # 从 Temp 的那份副本装配，绝不碰真实家目录：`WorkbenchConfig.load()` 不带路径
    # 会读（并按需创建）~/.video-workbench，那是用户的东西，不是我这条用例的现场。
    from test_summary_real_backend_ui import _temp_config

    cfg = WorkbenchConfig.load(_temp_config(tmp_path))
    rendered = render_yaml(cfg)
    assert re.search(r"^\s*thinking\s*:", rendered, re.M), (
        f"render_yaml 的输出里没有 thinking —— 保存一次这个键就没了：{rendered!r}"
    )

    mock_text = (WEB_DIR / "js" / "mock.js").read_text(encoding="utf-8")
    assert re.search(r"\bthinking\s*:", mock_text), (
        "mock.js 里的 thinking 形状被删了：界面用例的载荷形状与真后端不再同源"
    )


# =========================== 判据 6 的另一半：撤的是控件，不是那个键的校验


def test_the_enum_still_rejects_an_unknown_thinking_value(tmp_path):
    """控件撤了，**枚举校验不许跟着撤**：`thinking: sometimes` 必须仍然被拒。

    为什么这条重要：撤控件之后这个键没有任何 UI 入口，它是**只从文件进来**的。
    文件是唯一入口 = 唯一入口没有校验，用户手改或者旧文件里带的坏值就会一路走到
    `bili_scribe/infra/llm.py` 那个真会发 `enable_thinking` 的地方。
    断言不依赖任一侧措辞：只要求异常消息里同时能看到字段名与 `thinking`
    （两侧消息文本本来就不同：`[VALIDATION] llm.thinking 只能取 auto|off|on：sometimes`
    与 `[VALIDATION] 配置项不合法：llm.thinking='sometimes'（可选值：auto/off/on）`；
    「两侧都拒 + 都能看到字段名和可选值」才是判据，逐字对账归 `test_config_home_split`）。
    """
    from test_summary_real_backend_ui import _temp_config

    from video_workbench.config import WorkbenchConfig

    cfg_path = _temp_config(tmp_path)
    text = cfg_path.read_text(encoding="utf-8")
    broken = re.sub(r"^(\s*)thinking:.*$", r'\1thinking: "sometimes"', text, count=1, flags=re.M)
    assert broken != text, (
        f"没能把 thinking 改成 sometimes（现读 {[line for line in text.split(NL) if 'thinking' in line]}）"
        "—— 夹具不成立，这条会空转"
    )
    cfg_path.write_text(broken, encoding="utf-8")
    with pytest.raises(Exception) as caught:
        WorkbenchConfig.load(cfg_path)
    message = str(caught.value)
    assert "VALIDATION" in message and "thinking" in message, (
        f"未知 thinking 值没被枚举挡住，或者报错看不出是谁拒的：{message!r}"
    )
    for choice in ("auto", "off", "on"):
        assert choice in message, (
            f"报错里看不到可选值 {choice}：用户手改错了却不知道能填什么 —— {message!r}"
        )


# ==================================== 判据 7：一次无关保存必须把它逐字带回去


@pytest.fixture()
def thinking_off_page(browser, web_base_url, tmp_path):
    """真后端 + 一份把 `llm.thinking` 写成 `off` 的 Temp 配置，并已走进 设置→模型。

    不用默认值 `auto` 当现场：值与默认相同时，「被重置成 auto」和「原本就是 auto」
    长得一样，判据 7 最想拦的那一种坏法就看不见了。
    """
    from test_summary_real_backend_ui import _open_real, _Service, _temp_config

    from video_workbench.app.service import WorkbenchService

    cfg_path = _temp_config(tmp_path)
    text = cfg_path.read_text(encoding="utf-8")
    if re.search(r"^\s*thinking:", text, re.M):
        text = re.sub(r"^(\s*)thinking:.*$", r'\1thinking: "off"', text, count=1, flags=re.M)
    else:
        lines = []
        for line in text.split(NL):
            lines.append(line)
            if line.rstrip() == "llm:":
                lines.append('  thinking: "off"')
        text = NL.join(lines)
    cfg_path.write_text(text, encoding="utf-8")

    handle = _Service(WorkbenchService(config_path=str(cfg_path)), cfg_path)
    page = errors = None
    try:
        page, errors = _open_real(browser, web_base_url, handle)
        yield page, errors, handle, cfg_path
    finally:
        if page is not None:
            page.close()
        handle.shutdown()


def test_an_unrelated_save_carries_the_thinking_value_verbatim(thinking_off_page):
    """判据 7：界面上没有这个旋钮，保存别的内容时它必须**逐字留在文件里**。

    现场：Temp 配置先写 `thinking: "off"` → 真页面上只改「温度」→ 点保存 →
    从磁盘文本、sidecar、重新装配三份读回来。
    三个失败面分开钉，因为坏法不同：
      * 整个键没了 → 第一条红（写盘模板不认识它 / 保存路径重建了对象）；
      * 被重置成 `auto` → 第二条红（有人拿默认值新建了 LlmConfig）；
      * yaml 与 `workbench.json` 不一致 → 第三条红（两份真相，搬家读的是 yaml）。
    页面只改温度，**不碰角色、不点默认角色**，所以这里不会牵动 task-23 的 C 组。
    """
    page, errors, backend, cfg_path = thinking_off_page
    seed = cfg_path.read_text(encoding="utf-8")
    assert re.search(r'^\s{2}thinking:\s*"off"\s*$', seed, re.M), (
        f"夹具没能把 thinking 写成 off（现读 {[line for line in seed.split(NL) if 'thinking' in line]}）"
        "—— 这一步不成立，下面三条都在测别的东西"
    )

    require_expanded(page, "温度在折叠组「上下文与长素材」里，收起态点不到保存按钮")
    # 温度那一行：按 label 定位（本产品没有 data-path 属性，理由见文件头），
    # 再取该行里的 number 输入框。
    row = page.locator(".field", has_text="温度")
    assert row.count() == 1, f"「温度」那一行应当唯一，实得 {row.count()}"
    box = row.first.locator('input[type="number"]')
    assert box.count() == 1, f"那一行里应当只有一个数字框，实得 {box.count()}"
    box.fill("0.37")
    page.get_by_role("button", name="保存").first.click()
    page.wait_for_timeout(600)

    saved = cfg_path.read_text(encoding="utf-8")
    kept = [ln for ln in saved.split(NL) if "thinking" in ln]
    assert kept and re.search(r'^\s{2}thinking:\s*"off"\s*$', saved, re.M), (
        f"保存别的字段之后，文件里的 llm.thinking 不见了或被改了：{kept}；"
        "界面上没有控件时，这个键唯一的活路就是随文件往返"
    )
    sidecar = cfg_path.parent / "workbench.json"
    if sidecar.exists():
        ext = json.loads(sidecar.read_text(encoding="utf-8"))
        assert ext.get("llm", {}).get("thinking") == "off", (
            f"sidecar 与 yaml 不一致：{ext.get('llm')}"
        )
    reloaded = importlib.import_module("video_workbench.config").WorkbenchConfig.load(cfg_path)
    assert reloaded.llm.thinking == "off", (
        f"重新装配读到 {reloaded.llm.thinking!r}，不是磁盘上那句 off"
    )
    assert float(reloaded.llm.temperature) == 0.37, (
        f"温度没保存上（{reloaded.llm.temperature}）：那这次保存压根没发生，上面三条是在原地打转"
    )
    assert errors == [], f"控制台有 error：{errors}"
