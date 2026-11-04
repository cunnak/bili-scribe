r"""task-13：设置页「默认角色」那行红字 + 下拉框无 standard —— 常驻界面验收。

## 为什么必须有这个文件
这行红字是本轮迁移里**用户直接看得见**的新东西：配置里的默认角色是退役值（`standard`）
或拼错的脏值时，设置→模型→「上下文与长素材」里那个下拉框下面会出现一行 danger 色的说明，
文案来自后端 `summary.roles` 的 `configured_hint`（判定只在后端一处：
`video_workbench/core/summarizer.py` 的 `resolve_role`）。下拉框本身也去掉了 standard。

lead 先前是**一次性手工**验的（复制 web 到 Temp、改副本 mock.js、起 http.server、真浏览器点进去）。
那种做法下次谁改 `settings.js` 或 `app.css` 都不会报错。本文件把它变成能重跑、会红的东西。

## 三条被钉住的判据（对应 settings.js 的三段真实代码）
1. **脏态**：`.warn-line` 可见、文本逐字等于后端给的 hint、用的是主题里的 danger 色与
   3px 左竖条；`<details class="adv">` 被自动展开（`expandRoleGroup`，
   不点任何地方就能看见）；下拉框选项**逐字等于 `ROLE_KEYS`**（个数现读，不写死）、
   不含任何退役角色。
2. **净态**：`.warn-line` 仍在 DOM 但不可见（`display:none`、`offsetParent` 为 null），
   折叠组**不许**被自动展开（干净时不抢用户的折叠习惯），下拉照样是 `ROLE_KEYS` 那几项。
3. **后端读不到时主功能不因此打不开**：`summary.roles` 失败（这里直接 reject），
   红字什么都不显示，页面**不出现**「读不到设置」——那是 `refreshRoleIssue()` 里
   `applyRoleIssue(null)` 的兜底语义，钉它是因为它一旦退化成「角色接口挂了就整页空白」，
   用户就没法在这页把脏值改回来了。

## 档位/形状的分工与边界
- stub 只注入 `configured_unrecognized` / `configured_hint` 这两个**会变的判定结果**；
  角色列表本身仍走真实 `WB.rpc` 的返回（在包装里 merge），不写死进 `mock.js`
  ——那是 vendor 定的判据：mock 只承载形状。仓库里的 `video_workbench/web/js/mock.js`
  一个字没动，脏值是在页面加载完成后包一层 `WB.rpc` 注入的（`add_init_script` 的属性陷阱试过，被 11 处 `window.WB = window.WB || {}` 反复重装冲掉，见 PATCH_JS 上方注释）。
- 两条**别再现读现猜**的事实（lead 裁定，我先前各说错一次）：
  ① `mock.js:1530-1536` 七个角色**都带** `prompt_source: null`（连同空的 prompt_bytes/prompt_sha8），
     不是缺字段——`:1523-1526` 注释写明故意只放键的形状，值由后端 `core/prompt_source.py` 现算，
     写死进 mock 等于第二份角色↔txt 映射表。所以演示态那行显示「无 txt 真源」是 null 走「无源」分支的
     自洽结果，**不许给 mock 补真值**，也不许为那行文案写用例（改文案归 vendor，另开任务）。
  ② 真后端渲染这些用例**测不到**：`rpc.js:61` 只认 pywebview 桥，WB 本体（`video_workbench/desktop.py`）
     没有 HTTP 服务模式（`run.py` 是 bili-scribe 的 uvicorn 启动器），要看真后端只能起桌面窗口。
     所以这里的绿是「真实形状 + 注入的判定结果」，永远不等于「连上真后端也这样」。
- 服务用 `ThreadingHTTPServer` 直接挂在真实 `video_workbench/web` 上，只读、端口 0 随机、
  fixture 结束 `shutdown()`+`server_close()`；不落临时副本，不留垃圾。
- 浏览器装不上就**明确 skip**（`importorskip` + launch 失败两条路都会给可执行的修复命令），
  绝不用「断言写了但没跑」冒充绿。

## 怎么跑
    .venv/Scripts/python.exe -m pytest tests/wb/test_settings_role_warning_ui.py -q
截图落在 `build/accept/wb_settings_role_pw_dirty.png` / `..._clean.png`
（名字带 pw，与 lead 手工基准 `wb_settings_role_dirty.png` / `..._clean.png` 并存，方便比对）。

## 本轮实测出来的两件事（都不是我断言的对象，但要说清楚）
- **一次进设置会发两遍 `summary.roles` 与 `app.settings.get`**：点左栏「设置」和代码里
  `router.go` 都一样；而页内切分区一次都不发。重复挂载属于浪费，不在这条红字的合同里，
  所以用例只钉「切分区不重发」，没有锁进页次数。已作为发现报给 lead。
- 复现不需要副本：`?mock=1` 起真实 `video_workbench/web` 就是这条路径。

## 牙齿证明怎么重跑（不碰产品文件）
牙齿证明要「把 `configured_unrecognized` 的消费改坏一次」，但仓库里那份
`video_workbench/web/js/pages/settings.js` 正被 vendor 改（未提交 100+ 行），
覆盖回写的窗口里他一落盘就会被我的备份抹掉。所以牙齿证明跑在 **Temp 副本**上：
`WB_WEB_ROOT` 只决定「把哪一份 web 目录交给浏览器」，默认就是仓库里那份产品树，
这一点由 `test_serves_the_repository_web_dir_by_default` 钉住（副本跑时用它自己的
`-k not serves_the_repository_web_dir` 排除）。

PowerShell（Windows 本机实测过的形式）：
    $t = Join-Path $env:TEMP "wbweb_teeth"
    Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
    Copy-Item -Recurse video_workbench\web $t
    # 改坏一处：把 `roleIssueText = dirty ? ((data && data.configured_hint) || "") : "";`
    # 换成 `roleIssueText = (data && data.configured_hint) || "";`（只认话、不认脏值）
    # 注意这个文件是 CRLF，锚点别带行尾，否则一次都命中不了
    $env:WB_WEB_ROOT = $t
    .venv\Scripts\python.exe -m pytest tests/wb/test_settings_role_warning_ui.py -q `
        -k "not serves_the_repository_web_dir"     # → 必须红
    Remove-Item Env:WB_WEB_ROOT                     # 再跑一次默认树 → 必须绿
git-bash 形式同理（`WB_WEB_ROOT=$t .venv/Scripts/python.exe -m pytest ...`）。

实测过的四个变体（都是红，且各红在不同用例上，说明四类断言都有牙）：
  control（原样副本）→ 绿；hint_only（只认 hint）→ `..._reads_the_verdict_from_the_backend_only` 红；
  no_expand（删 `roleAdvNode.open = true`）→ 脏态那条红；back_to_8（下拉框塞回 standard）→ 三条红；
  css_danger（拆掉 `.warn-line` 的 color 与 3px 左竖条）→ 脏态那条红。

## 双皮肤（task-21）

`SKINS = ("ark", "exhibition")` + `apply_skin(page, skin)`：点顶栏那个
`[data-skin-chip=…]` 按钮（`index.html:52/:55`），再断言 `document.documentElement.dataset.skin`
真的换过去了。**不用 `#skin=` 哈希**：`app.js:176` 是 `applySkin(skinFromHash || settings.skin)`，
真后端还会拿设置再覆盖一次，两条路在不同模式下行为不一样；点 chip 顺带把「用户切得过去吗」也验了。
打了 parametrize 的是四条**可见性/几何**判据：脏态红字、N1（收起态真隐藏）、N2（展开态滚得到）、
N3（来源行不溢出）。静态那几条（角色清单、settings.js 复制表、默认目录）与皮肤无关，不打。

## 已知事实，写给下一个读的人
`settings.js:1502-1533` 把这行红字挂在字段行里，而 `llm.role` 属于
`group: "advanced"`，默认收在「上下文与长素材」折叠组内——**没有脏值时它本来就不可见**，
到达率天然受限。vendor 的处置是「只在有结论时把这一组替用户展开」
（`expandRoleGroup()`，被 `applyRoleIssue` 与 `renderAdvanced` 各调一次），
所以本文件第 1 条同时钉住「自动展开」这件事：红字挂上了但组没展开，等于没挂。

## task-20 的判据：可见性断言必须自己把前置造出来（lead 批准，原样入档）

**断言「用户看得见 X」只许两种写法：**
1. 先把 X 所在的折叠组**显式打开并断言它确实打开**（`require_expanded`，真点 summary），
   再断言可见；
2. 这条测的就是**收起态**时，只许断言**不可见**，并同时断言 `open is None`
   （`require_collapsed`）。

**禁止第三种**：在收起态用 `is_visible()` / `inner_text()` / 点击去「证明可见」。

机制（task-19 修复前在这台机器上量的，不是推测）：旧 CSS 把 `.adv__body{display:flex}`
无条件写上，盖掉了 Chromium 对「未 open 的 `<details>` 里非 summary 子元素」的 UA 隐藏，
于是收起态下内容盒高 **1357px**、「附加提示词」那一行被绘制在 **bottom=1287**，而 `.page` 的
`scrollHeight` 只有 **1054**（`clientHeight` 721）⇒ 最多滚 **333px**，那一行永远滚不进来：
**画得出来、滚不到、点不到**。vendor 选 A 修成 `.adv__body{display:none}` +
`.adv[open] .adv__body{…display:flex…}`，收起时内容盒高 **0**、那一行高 **0**。

还有一条更阴的：Playwright 的 `has_text` 走 **textContent**，不看可见性——收起态下含
「默认角色」的 `.field` 照样定位得到（实测 `roleRowByHasText=1`）。所以**「读得到」不等于
「看得见」**：`option.value`、`textContent`、`input_value()` 证明的只是 DOM 形状，不许拿它们
替代可见性判据；反过来 `inner_text()` 对隐藏子树返回**空串**，而 `is_visible()` 在
「收起但被绘制」时返回 **True**——这正是把「看不见」测成「看得见」的那条路。

本文件的分工因此是：
- **N1 是这条缺陷的牙**（`test_collapsed_group_is_really_hidden_not_just_painted`）：区分
  「真隐藏」与「画出来但滚不到」。CSS 还原旧写法必红。
- **N2 是常驻回归**（`test_expanded_group_reaches_the_switch_and_the_line_by_scrolling`）：
  钉「展开之后用户真点得到」，含真点击翻 `aria-checked`。**如实说**：它在旧 CSS 下**不会**红
  （展开态两版布局相同，实测数字一致），它的牙在另一类回归上——变体二（限高）能量到它红。
- 既有 4 条「靠脏态隐式自动展开」的用例改成显式 `require_expanded`；收起态那条补
  `require_collapsed`；reject 那条的析取空断言（`count()==0 or not is_visible()`，
  本仓库第三次犯）拆成单一语义。

已知事实（lead 与 vendor 复核）：全仓**只有一个** `<details class="adv">`
（`settings.js:1194`），所以「取错 `.adv__body`」这个坑在本页不存在（探针在别的页面踩过）；
开关控件是 `<button class="switch" role="switch" aria-checked>`（`settings.js:1356-1367`），
**没有 input**，要读状态只能读 `aria-checked`。带 `pw_` 的三张截图是**每次跑都被覆盖**的过程图，
别当证据图；要留存的证据用 `wb_settings_role_t20_*.png`。

## 牙齿配方（task-20 三个变体，都跑在 Temp 副本上，产品文件一个字节不动）

PowerShell：
    $t = Join-Path $env:TEMP "t20web"
    Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
    Copy-Item -Recurse video_workbench\\web $t
    # 变体一（旧 CSS，**N1 的牙**）：把 `.adv__body{display:none}` 与 `.adv[open] .adv__body{…}`
    #   两条合并回一条无条件 flex（带 padding）；
    # 变体二（限高裁剪，**N2 的牙**）：给 `.adv[open] .adv__body` 加 max-height:0;overflow:hidden；
    # 变体三（强制不换行，**N3 的牙**）：在 css 末尾追加 `.field__help{white-space:nowrap}`。
    $env:WB_WEB_ROOT = $t
    .venv\\Scripts\\python.exe -m pytest tests/wb/test_settings_role_warning_ui.py -q `
        -k "not serves_the_repository_web_dir"     # → 对应变体必须红
    Remove-Item Env:WB_WEB_ROOT                     # 再跑默认树 → 必须绿

`app.css` 是 **LF**（2378 行、0 个 CRLF），锚点别带 `\\r`；`.adv__body` 在 css 里出现 **2 次**
（`.adv[open] .adv__summary` 那条规则不含它），所以还原旧写法必须**整两条规则一起换**才唯一命中。

实测（`C:/Users/b1596/AppData/Local/Temp/t20_teeth.py`，2026-11-03 23:31）：控制组 13 passed；
`t20_oldcss` → 红 `test_collapsed_group_is_really_hidden_not_just_painted`（N1）；
`t20_maxh` → 红 `test_expanded_group_reaches_the_switch_and_the_line_by_scrolling`（N2）；
`t20_nowrap` → 红 `test_expanded_group_keeps_the_source_line_inside_its_width`（N3）；
`app.css` sha256 前后都是 `83563fd418996f94…`（没动过），四个副本跑完都删。
**变体二这一条是补出来的**：第一版 N2 只查行的 rect 与 `click()`，在 `max-height:0` 下**照样绿**
（被裁剪的子元素 rect 有高度、Playwright 的可点击判定不含裁剪检查），属于「绿着但什么都没守」。
现在 N2 同时要求「内容盒高度 > 0」且「行落在内容盒之内」，并且用 `elementFromPoint` 命中测试
确认中心点真的落在那个开关上——三者缺一都会被相应变体糊过去。

## task-16 补的三道门（为什么是这个形状）

1. **选项表漂移门**（`test_the_option_table_in_settings_js_is_still_a_copy_of_the_backend_list`）
   ：`settings.js:60` 那张 role 选项表是后端 `ROLE_KEYS` 的第二份拷贝。**保留它是决定**
   ——`summary.roles` reject 时页面必须照常可用（`test_roles_endpoint_failure_does_not_break_the_settings_page`
   已经把这条需求编码成断言）；保留的代价是它会漂，所以加静态门，两边一起改才绿。
2. **空 default 已知边界**（`..._falls_back_to_the_first_option_and_that_is_a_declared_boundary`）
   ：vendor 说「读不到 default 就不选任何项」**不成立**——`roleSelectValue` 走
   `return want`，want 不在选项里时 DOM 的 `selectedIndex` 就是 0（证据是 vendor 自己的
   before 树）。后端现在恒给非空 default，所以这是假想边界，不为它改产品码，
   但要把它钉成**已声明的行为**，不许变成没人知道的意外。
3. **点保存的落盘实测**（`test_saving_an_unrelated_change_does_not_write_the_role_at_all`
   与 `test_confirming_the_displayed_default_and_saving_writes_that_default`）
   ：vendor 明确只读 DOM 没点按钮。两条合起来才是那句承诺的完整覆盖——
   **没碰角色就不该写角色**（`buildPatch` 只收标脏的 path，payload 是
   `{values: {llm: {role: …}}}` 的嵌套形状，`:223`），**碰了就要写后端 default**
   （不能是停用的 `standard`，也不能是 DOM 第一项 `xueshu`）。

   实测发现（没钉成规格，只记在这里）：脏配置但用户一个字段都没改时，底部条是
   「没有改动」、保存按钮 `disabled: true`（`settings.js:354`/`:382`）——所以
   「顺手保存」**必须**先弄脏某个字段才发生。3a/3b 都因此显式制造一次改动，
   并断言真的发出了 `app.settings.put`（`len(sends) == 1` + payload 带上我改的字段），
   否则「payload 里没有 role」会因为压根没保存而空转。
   两处都用 `_config_fingerprints()` 钉住**真实配置文件前后没变**（不存在的文件记
   `MISSING`，凭空造出一个也算改动）——离线 mock 不该有任何落盘副作用，这句话要用
   指纹证明，不是靠「反正没有后端」默认。

## 关于「一次进设置发两遍 summary.roles / app.settings.get」

保持原样：那是待修的浪费，**不是**这条红字的合同，所以**不加断言**（加了就是把浪费
固化成规格）。用例只钉「页内切分区为 0 次重发」。详见 `PATCH_JS` 上方注释里那两个
实测计数。
"""

from __future__ import annotations

import http.server
import json
import os
import re
import threading
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="没装 playwright，界面验收跑不了。修：`.venv/Scripts/python.exe -m pip install "
           "playwright && .venv/Scripts/python.exe -m playwright install chromium`",
)

from playwright.sync_api import sync_playwright  # noqa: E402

from video_workbench.core.summary import (  # noqa: E402
    DEFAULT_ROLE,
    RETIRED_ROLES,
    ROLE_KEYS,
)

ROOT = Path(__file__).resolve().parents[2]
#: 牙齿证明用：只决定「把哪一份 web 目录交给浏览器」，默认=仓库产品树（见文件头）。
WEB_DIR = Path(os.environ.get("WB_WEB_ROOT") or ROOT / "video_workbench" / "web")
ACCEPT_DIR = ROOT / "build" / "accept"

#: 脏值本身与整句话案都来自后端；这里用一句可识别的话，逐字比对才有效。
HINT = "标准（standard）角色已停用，请选一个方法论角色"
DIRTY_VALUE = "standard"
CLEAN_VALUE = ""

#: 注入点：**等页面 load 完再包 `WB.rpc`**，不用 `add_init_script` 里的属性陷阱。
#: 原因实测过：`video_workbench/web/js` 下有 11 个文件各自写
#: `var WB = (window.WB = window.WB || {})`，每一次赋值都会把陷阱重装一遍、
#: 把已经存下来的真实 `rpc` 丢掉，于是启动阶段页面抛
#: `Cannot read properties of undefined (reading 'apply')`，整页渲染不出来。
#: load 之后所有模块都跑完了，这时包一次就稳定。`__rolesStubCalls` 计数用来钉
#: 「页内切分区不重新要结论」。注意：**一次进设置实测发了 2 次** `summary.roles`
#: （`app.settings.get` 同样 2 次，路由挂载路径重复，点左栏和 `router.go` 都一样）——
#: 那是待修的浪费，不是这条红字的合同，所以用例只断言切分区为 0 次，没把 2 次说成正确。
PATCH_JS = """
(stub) => {
  const real = window.WB && window.WB.rpc;
  if (typeof real !== "function") {
    throw new Error("注入点不对：页面加载完还是没有 WB.rpc");
  }
  window.__rolesStubCalls = 0;
  window.__rpcSends = [];
  window.WB.rpc = function (method, params, opts) {
    const argv = Array.prototype.slice.call(arguments);
    if (method === "app.settings.get" && stub && stub.settings_role !== undefined) {
      // 配置里真的写着退役角色：这才是用户日常遇到的形态（后端两个字段就是这么来的）。
      return real.apply(this, argv).then(function (data) {
        const merged = JSON.parse(JSON.stringify(data || {}));
        merged.llm = merged.llm || {};
        merged.llm.role = stub.settings_role;
        return merged;
      });
    }
    if (method === "summary.roles" && stub) {
      window.__rolesStubCalls += 1;
      if (stub.reject) {
        return Promise.reject(new Error("stub: summary.roles 读不到"));
      }
      // 角色列表本身仍走真实实现，stub 只盖这两个会变的判定字段。
      return real.apply(this, argv).then(function (data) {
        const merged = JSON.parse(JSON.stringify(data || {}));
        merged.configured_unrecognized = stub.configured_unrecognized || "";
        merged.configured_hint = stub.configured_hint || "";
        // `default` 也允许盖：它是 `roleSelectValue` 的第二个输入，而「后端 default
        // 为空」这条已知边界正常永远进不去（`describe_roles()` 恒给非空值），只能盖它。
        if (stub.default !== undefined) merged.default = stub.default;
        return merged;
      });
    }
    if (method === "app.settings.put") {
      // 「点保存到底落盘了什么」必须可观测：先记 payload，再交给真实实现。
      // mock 只改内存里的 SETTINGS（`mock.js:1214` `merge(SETTINGS, p.values)`），
      // 不碰任何磁盘文件；这条用例全程没有后端进程，落盘可能性见 `CONFIG_PATHS` 指纹检查。
      window.__rpcSends.push(JSON.parse(JSON.stringify(params || null)));
    }
    return real.apply(this, argv);
  };
}
"""


def test_the_authority_behind_these_expectations():
    """前提检查：本文件的期望值全挂在后端权威上，权威一变就要在这里红，别在四处默默失配。

    - `standard` 必须在 `RETIRED_ROLES` 里：脏态用例注入的就是这个值，它要是被恢复成
      可用角色，「红字」根本没有触发条件，后面几条断言全是空转。
    - 下拉框的**选项数与顺序**由 `ROLE_KEYS` 决定，所以这里钉的是它的**结构**而不是个数：
      共有的那一段（= bs 存活角色，顺序一致）在前，WB 权威层自造的排在后面。
      task-24 加 `shipin` 时本文件红过一轮，原因就是把「7 个」当成了合同值——
      个数会随角色增删移动（`shipin` 与 `renhua` 方法论同源，随时可能合并），
      结构不会。写死个数的用例等于给下一次角色变更预置一次返工。
    """
    from bili_scribe.core import roles as bs_roles

    assert DIRTY_VALUE in RETIRED_ROLES, (
        f"退役名单里没有 {DIRTY_VALUE!r}，脏态用例的前提没了：{sorted(RETIRED_ROLES)}")
    assert len(ROLE_KEYS) == len(set(ROLE_KEYS)), f"ROLE_KEYS 里有重复：{ROLE_KEYS}"
    survivors = tuple(role.key for role in bs_roles.ALL_ROLES
                      if role.key not in set(RETIRED_ROLES))
    assert ROLE_KEYS[:len(survivors)] == survivors, (
        f"共有角色那一段漂了：期望 {survivors} 开头，实得 {ROLE_KEYS}")
    extras = ROLE_KEYS[len(survivors):]
    bs_keys = {role.key for role in bs_roles.ALL_ROLES}
    assert all(key not in bs_keys for key in extras), (
        f"后段 {extras} 里有 bs 已存在的角色：说明前段被删短了，不是新角色进来了")
    assert set(ROLE_KEYS).isdisjoint(set(RETIRED_ROLES)), (
        "退役角色还挂在 ROLE_KEYS 上，下拉框会把它渲染成一个可选项")


def test_serves_the_repository_web_dir_by_default():
        """默认必须跑真实产品树：`WB_WEB_ROOT` 一旦被谁常驻设上，用例就在测一份副本，
        产品改了也不会红——这条把「测的是仓库里那份」钉死。"""
        assert WEB_DIR == ROOT / "video_workbench" / "web", (
            f"被测目录被换走了：{WEB_DIR}")
        assert (WEB_DIR / "js" / "pages" / "settings.js").is_file(), WEB_DIR
        assert (WEB_DIR / "css" / "app.css").is_file(), WEB_DIR


@pytest.fixture(scope="module")
def web_base_url():
    """只读服务真实 `video_workbench/web`，端口 0 随机，用完关掉。"""
    Handler = type(
        "QuietHandler",
        (http.server.SimpleHTTPRequestHandler,),
        {"log_message": lambda self, *args: None},
    )

    def handler_factory(*args, **kwargs):
        return Handler(*args, **kwargs, directory=str(WEB_DIR))

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/index.html?mock=1#/settings?section=llm"
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            launched = pw.chromium.launch()
        except Exception as exc:  # 浏览器没下载/无权限起进程——明确 skip，不假绿
            pytest.skip(
                "chromium 起不来，界面验收没跑（不是通过，是没跑）。"
                f"原因：{type(exc).__name__}: {str(exc)[:200]}；"
                "修：`.venv/Scripts/python.exe -m playwright install chromium`"
            )
        try:
            yield launched
        finally:
            launched.close()


def base_url(web_base_url: str) -> str:
    """去掉路由片段，只留 `index.html?mock=1`；进设置页改用 `router.go`。"""
    return web_base_url.split("#")[0]


#: N2 必须用的视口高度：vendor 与 lead 都在 797px 下复现过死区；960 会盖住这个缺陷。
DEAD_ZONE_VIEWPORT = {"width": 1384, "height": 797}


def open_settings(browser, web_base_url, stub: dict, viewport: dict | None = None) -> tuple:
    """开一页「设置→模型」，返回 (page, 控制台/页面错误列表)。

    默认视口 1440×960；测可达性的用例必须显式换成 `DEAD_ZONE_VIEWPORT`，
    否则「滚不到」在 960 的高度下有可能刚好被盖住。
    """
    page = browser.new_page(viewport=viewport or {"width": 1440, "height": 960})
    errors: list[str] = []

    def on_console(message) -> None:
        if message.type != "error":
            return
        text = message.text
        # 浏览器自己会请求 /favicon.ico，本仓库没有这个文件——那条 404 与产品无关。
        if "favicon" in text.lower():
            return
        errors.append(f"[console] {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
    page.goto(base_url(web_base_url), wait_until="load")
    page.wait_for_function("() => window.WB && typeof window.WB.rpc === 'function'",
                           timeout=15000)
    page.evaluate(PATCH_JS, stub)
    page.evaluate('() => window.WB.router.go("settings", {section: "llm"})')
    # 等真实渲染：字段行出现即说明 app.settings.get 与 paint 都走完了。
    page.wait_for_selector("label.field__label", timeout=15000)
    return page, errors


def role_row(page):
    """「默认角色」那一行（label + select + help + warn-line 都在同一个 .field 里）。"""
    return page.locator(".field").filter(has_text="默认角色").first


def warn_line(page):
    return role_row(page).locator(".warn-line")


def role_values(page) -> list[str]:
    return role_row(page).locator("select option").evaluate_all(
        "nodes => nodes.map(n => n.value)")


# ============================================================ 折叠组前置（task-20 判据）

#: 「上下文与长素材」那个折叠组。全仓只有一个 `details.adv`（`settings.js:1194`），
#: 但这里仍然按标题筛，而不是裸 `details.adv`：万一以后别的分区也加了折叠组，
#: 筛标题会比「取第一个」先红在这里，而不是悄悄测错元素。
def adv_group(page):
    return page.locator("details.adv").filter(has_text="上下文与长素材").first


def _box(locator) -> dict:
    """元素的几何：rect 高度 + computed display。**只量，不判可见性**。"""
    return locator.evaluate(
        """el => ({
             height: Math.round(el.getBoundingClientRect().height),
             top: Math.round(el.getBoundingClientRect().top),
             bottom: Math.round(el.getBoundingClientRect().bottom),
             display: getComputedStyle(el).display,
           })""")


def require_expanded(page, reason: str):
    """判据写法 1：**真点 summary** 打开折叠组，并断言它确实打开，然后才许断言可见。

    为什么不直接 `el.open = true`：那会绕过 summary 的点击路径，而用户唯一能做的就是点
    summary；点不动（被别的样式盖住、标题不在视口内）本身就是可达性缺陷。
    为什么脏态还要点：脏配置下 `expandRoleGroup()` 已经把它打开了，`if open is None` 才点
    一次——点两下会把它**合上**，所以必须先读状态再决定点不点。
    """
    group = adv_group(page)
    if group.get_attribute("open") is None:
        group.locator("summary").click()
        page.wait_for_timeout(120)
    assert group.get_attribute("open") is not None, (
        f"前置失败：折叠组打不开（{reason}）——后面每条可见性断言都建立在它打开之上")
    return group


def require_collapsed(page, reason: str):
    """判据写法 2：这条测的就是收起态，那就把「收起」钉成断言，别再拿可见性当证据。"""
    group = adv_group(page)
    assert group.get_attribute("open") is None, (
        f"前置失败：折叠组本该是收起的（{reason}），现在却被展开了，"
        "这条测的就不是收起态了")
    return group


def switch_row(page, label_text: str):
    """折叠组里某一个字段行（按 label 全文找，再上溯到 `.field`）。"""
    label = page.locator("label.field__label").filter(has_text=label_text).first
    return label.locator(
        "xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' field ')][1]")


#: 两套皮肤。ark 是深色默认，exhibition 是浅色（`app.js:75-98` 皮肤表）。
SKINS = ("ark", "exhibition")


def apply_skin(page, skin: str) -> None:
    """切皮肤：点顶栏那个 chip（`index.html:52/:55`），**不用 URL 哈希**。

    为什么不用 `#skin=…`：`app.js:176 load()` 里 `applySkin(skinFromHash || settings.skin)`
    之后，真后端还会再覆盖一次，哈希那条路在 mock 与真后端下行为不同；chip 是用户
    实际用的入口，点它等于把「用户能不能切过去」一起验了。
    """
    chip = page.locator(f'[data-skin-chip="{skin}"]').first
    assert chip.count() == 1, f"找不到皮肤 chip {skin!r}——皮肤名表或顶栏改了"
    chip.click()
    page.wait_for_timeout(120)
    now = page.evaluate("() => document.documentElement.dataset.skin")
    assert now == skin, f"点完 chip 皮肤没切过去：期望 {skin}，实得 {now}"
    pressed = chip.get_attribute("aria-pressed")
    assert pressed == "true", f"皮肤 chip 的 aria-pressed 没跟上：{pressed!r}"


@pytest.mark.parametrize("skin", SKINS)
def test_dirty_config_shows_the_danger_line_and_auto_expands_the_group(
        browser, web_base_url, skin):
    """脏态：红字可见、文案逐字、danger style_info在、折叠组自动展开、下拉框 7 项无 standard。

    双皮肤（task-21）：`--danger` 在 exhibition 是 `#b42318`、ark 是 `#ff6b6b`，
    判据比的是「同一时刻解析出来的 var(--danger)」，所以两套都必须过。
    """
    page, errors = open_settings(browser, web_base_url, {
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
    })
    try:
        apply_skin(page, skin)
        line = warn_line(page)
        assert line.count() == 1, f"`llm.role` 那行应该挂且只挂一条 .warn-line：{line.count()}"
        assert line.is_visible(), ".warn-line 在 DOM 但看不见——红字等于没挂"
        assert line.inner_text() == HINT, f"文案不是后端那句原话：{line.inner_text()!r}"

        # 折叠组必须被自动展开：红字挂上但组是合上的，用户照样看不见（到达率问题）。
        group = page.locator("details.adv").filter(has_text="上下文与长素材").first
        assert group.get_attribute("open") is not None, (
            "脏态没有自动展开「上下文与长素材」——这行红字藏在折叠组里，"
            "不合上就没人在不多点一次的情况下看见它")

        style_info = line.evaluate(
            """el => {
                 const cs = getComputedStyle(el);
                 const probe = document.createElement('span');
                 probe.style.color = 'var(--danger)';
                 el.appendChild(probe);
                 const danger = getComputedStyle(probe).color;
                 probe.remove();
                 return {color: cs.color, danger: danger,
                         borderLeft: cs.borderLeftWidth, display: cs.display,
                         offset: el.offsetParent !== null};
               }""")
        assert style_info["display"] != "none" and style_info["offset"] is True, style_info
        assert style_info["color"] == style_info["danger"], (
            f"红字不再是主题里的 danger 色：{style_info['color']} vs {style_info['danger']}")
        assert style_info["borderLeft"] == "3px", f"左竖条不是 3px：{style_info['borderLeft']}"

        # 切分区不该把结论弄丢、也不该重新去后端要一遍：
        # 实测点「常规」再点回「模型」是纯前端重绘（`showSection`），零 RPC。
        before = page.evaluate("() => window.__rolesStubCalls")
        page.click('.settings__nav-item[data-key="general"]')
        page.click('.settings__nav-item[data-key="llm"]')
        page.wait_for_timeout(250)
        assert page.evaluate("() => window.__rolesStubCalls") == before, (
            "页内切分区又去后端要了一次结论——这条提示只在进页面时读一次就够")
        line_after = warn_line(page)
        assert line_after.is_visible(), "切一趟分区，红字就没了"
        assert line_after.inner_text() == HINT, f"切完分区文案变了：{line_after.inner_text()!r}"
        assert group.get_attribute("open") is not None, "切完分区折叠组又合上了"

        # lead 的权威判据：提示行必须挂在那个折叠组里（挂错地方＝看不见或语义错），
        # 下拉框当前值必须等于配置里的角色（不是被前端偷偷改成默认值）。
        owner = line.evaluate("el => { const d = el.closest('details'); "
                              "return d ? d.querySelector('.adv__title').textContent : null; }")
        assert owner == "上下文与长素材", f".warn-line 不在「上下文与长素材」组里，而在：{owner!r}"
        configured = page.evaluate("() => window.WB.rpc('app.settings.get')"
                                   ".then(s => s.llm && s.llm.role)")
        shown = role_row(page).locator("select").input_value()
        assert shown == configured, (
            f"下拉框显示的角色与配置不一致：界面 {shown!r} vs 配置 {configured!r}")

        values = role_values(page)
        assert values == list(ROLE_KEYS), f"下拉框选项与 ROLE_KEYS 不一致：{values}"
        assert DIRTY_VALUE not in values, "下拉框里又出现退役角色了"
        assert not (set(values) & set(RETIRED_ROLES)), f"退役角色漏进下拉框：{values}"

        ACCEPT_DIR.mkdir(parents=True, exist_ok=True)
        line.scroll_into_view_if_needed()
        page.screenshot(path=str(ACCEPT_DIR / f"wb_settings_role_pw_dirty-{skin}.png"))

        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()


def test_clean_config_keeps_the_line_hidden_and_the_group_collapsed(browser, web_base_url):
    """净态：`.warn-line` 在 DOM 但不可见、折叠组不被抢着展开，页面不出现「读不到设置」。"""
    page, errors = open_settings(browser, web_base_url, {
        "configured_unrecognized": CLEAN_VALUE,
        "configured_hint": "",
    })
    try:
        line = warn_line(page)
        assert line.count() == 1, "净态这行节点应该仍然挂在 DOM 里（只藏不删）"
        assert not line.is_visible(), "配置是干净的，红字却显示出来了"
        state = line.evaluate(
            "el => ({display: getComputedStyle(el).display, "
            "offset: el.offsetParent !== null, text: el.textContent})")
        assert state["display"] == "none", state
        assert state["offset"] is False, state
        assert state["text"] == "", state

        group = page.locator("details.adv").filter(has_text="上下文与长素材").first
        assert group.get_attribute("open") is None, (
            "干净配置也把折叠组展开了——注释写明不许抢用户的折叠习惯")

        assert page.locator("text=读不到设置").count() == 0, "净态页面上出现了「读不到设置」"
        assert role_values(page) == list(ROLE_KEYS)

        ACCEPT_DIR.mkdir(parents=True, exist_ok=True)
        group.scroll_into_view_if_needed()
        page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_pw_clean.png"))

        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()


def test_roles_endpoint_failure_does_not_break_the_settings_page(browser, web_base_url):
    """`summary.roles` 读不到时：什么都不显示，但设置页照常可用（不是「读不到设置」）。

    钉的是 `refreshRoleIssue()` 里那句 `applyRoleIssue(null)`——角色接口挂了不能让整页挂掉，
    因为用户恰恰要在这页把脏值改回来。
    """
    page, errors = open_settings(browser, web_base_url, {"reject": True})
    try:
        # 单一语义（原来写的是 `count()==0 or not is_visible()`——析取在「节点在但藏着」时
        # 恒真，等于没断言，这是我在这个仓库上第三次犯同类空断言，删掉）。
        # 拆成两条，各自都能红：
        #   ① 形状：字段行照常渲染，`.warn-line` 这个节点必须还在（只是藏着）；
        #   ② 内容：后端没给结论，就不许凭空挂出文案。
        # ⚠️ 这两条**都不是可见性判据**（`.field` 在收起态也能被 has_text 命中，实测
        # roleRowByHasText=1）。「用户看不见红字」由 require_collapsed + N1 负责，
        # 这条只管「读不到结论时页面形状不变」。
        line = warn_line(page)
        assert line.count() == 1, (
            f"reject 时字段行照常渲染，`.warn-line` 节点应该还在（只藏不删），实得 {line.count()} 个")
        assert line.evaluate("el => el.textContent") == "", (
            "后端读不到结论，红字却有文案——那是前端在自己编判定")
        # 收起态前置要钉住：这条断言的是「页面照常可用」，而不是「展开后能看见」
        require_collapsed(page, "reject 读不到结论时不该抢着展开折叠组")
        assert page.locator("text=读不到设置").count() == 0, (
            "角色接口失败把整个设置页拖进了空状态")
        assert role_values(page) == list(ROLE_KEYS), "表单没渲染出来，主功能受影响"
        ACCEPT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_pw_readfail.png"))
        # 这一条里 console error 是**预期**的：rpc.js 对 silent 调用也会 console.warn，
        # 而 reject 走的是异常路径。warn 不算 error，所以仍然要求列表为空。
        assert errors == [], f"读不到角色时不该有 console error：{errors}"
    finally:
        page.close()


def test_the_ui_still_reads_the_verdict_from_the_backend_only(browser, web_base_url):
    """前端不许自己列退役名单：只带 hint、不带脏值时，**什么都不显示**。

    `settings.js:277-278` 要求 `configured_unrecognized` 与 `configured_hint` 同时非空才挂行。
    这条钉的是「判定归后端一处」：哪天有人在前端补一句 `if (text.includes('standard'))`，
    旧后端只给 hint 时就会挂出一行没有对象的提示，这条会红。
    """
    page, errors = open_settings(browser, web_base_url, {
        "configured_unrecognized": "",
        "configured_hint": HINT,     # 只给话，不给脏值
    })
    try:
        line = warn_line(page)
        # 这条测的就是「收起态下红字不该出现」，所以按判据写法 2：钉住收起，再断言不可见。
        # 不钉 open 的话，将来谁把 expandRoleGroup 改成无条件展开，这条会从「证明看不见」
        # 悄悄变成「证明存在」——断言还是绿的，语义已经换了。
        require_collapsed(page, "只给 hint 不给脏值时不该展开折叠组")
        assert not line.is_visible(), "只有 hint、没有脏值时不该挂红字（前端不该自己判定）"
        assert line.evaluate("el => el.textContent") == ""
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()


def test_config_actually_holding_a_retired_role_still_offers_only_live_roles(browser, web_base_url):
    """配置里**真的**写着 `standard` 时（不是只有后端两个字段在说话）用户看见什么。

    脏态用例里 mock 的配置本身是干净的，脏值只来自 `configured_unrecognized`——那是
    lead 一次性验过的形态。这条补的是另一半：`app.settings.get` 回的 `llm.role` 就是
    `standard`。此时必须同时成立：
      - 红字照样挂出来（提示不依赖配置值本身，只读后端结论）；
      - 下拉框里**绝不出现** `standard` 这个选项，且当前选中值**等于后端给的 default**。

    断言强度变更（不是放宽）：task-13 那版这里只敢写「选中的是七个活角色之一」，因为
    当时产品就是靠 DOM 副作用退到第一项，把它钉成 `== xueshu` 等于把缺陷固化成规格。
    task-14 落地后合同变成「必须等于后端 default」（`settings.js:271-280 roleSelectValue`），
    所以这里收紧成逐值相等——顺带说明那条旧写法为什么当时是**对的**写法。
    """
    page, errors = open_settings(browser, web_base_url, {
        "settings_role": DIRTY_VALUE,
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
    })
    try:
        # 判据写法 1：可见性断言之前，先把折叠组显式打开并断言它打开。
        # 原来这条靠「脏态自动展开」隐式成立——自动展开哪天被改掉，它会红，但红得
        # 说不清是「红字没挂上」还是「组没打开」，所以前置必须自己造出来。
        require_expanded(page, "本条要读 .warn-line 的可见性与文案")
        line = warn_line(page)
        assert line.is_visible(), "配置里写着退役角色，红字却没挂出来"
        assert line.inner_text() == HINT, f"文案不是后端那句原话：{line.inner_text()!r}"

        values = role_values(page)
        assert DIRTY_VALUE not in values, f"退役角色又进下拉框了：{values}"
        shown = role_row(page).locator("select").input_value()
        assert shown != DIRTY_VALUE, "下拉框把退役值当成当前选中项"
        assert shown == DEFAULT_ROLE, (
            f"配置里的脏值不可用时，下拉框应选后端 default（{DEFAULT_ROLE!r}），"
            f"实得 {shown!r}——退到第一项是 DOM 副作用，不是设计")
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()



# ================================================== task-16 补的三道门

#: `settings.js:60` 那张 role 选项表是后端 `ROLE_KEYS` 的第二份拷贝（vendor 上报的
#: 第二个 owner）。它**保留**是需求：`summary.roles` reject 时页面必须还能用，
#: 见 `test_roles_endpoint_failure_does_not_break_the_settings_page`。保留的代价就是
#: 会漂，所以这里加一道漂移门。
SETTINGS_JS = ROOT / "video_workbench" / "web" / "js" / "pages" / "settings.js"

#: 真实配置落盘的几处位置。界面用例绝不许碰它们——不是「反正不会写」就算了，
#: 而是把「没写」变成可核对的事实。
#:
#: task-23 搬家前后的差别（原来钉什么 → 现在钉什么 → 为什么）：
#: 原来只有两处——仓库根那份（测试/显式 ``--config`` 用的）与 ``~/.bili-scribe/config.yaml``
#: （WB 与 bili-scribe **共用**的默认家）。搬家之后默认家换成 ``~/.video-workbench``，
#: 旧家仍然存在并且是 bili-scribe 自己的配置。所以这个清单必须**同时**盯三处：
#: 只盯旧家的话，「误写新家」这件事根本看不见；只盯新家的话，「又开始写对方文件」
#: 也看不见。新家连 sidecar 一起列——``workbench.json`` 里是全量配置（含每把 api_key）。
CONFIG_PATHS = [
    ROOT / "config.yaml",
    ROOT / "workbench.json",
    Path.home() / ".bili-scribe" / "config.yaml",
    Path.home() / ".bili-scribe" / "workbench.json",
    Path.home() / ".video-workbench" / "config.yaml",
    Path.home() / ".video-workbench" / "workbench.json",
]


def _config_fingerprints() -> dict[str, str]:
    """配置文件指纹：存在记 sha256，不存在记 `MISSING`。

    不存在的文件也必须进表——一次误写会把 `MISSING` 变成 sha256，而这正是这条用例
    最怕的事；只比「已存在的文件没变」会漏掉「凭空造出一个配置」。
    """
    import hashlib

    out: dict[str, str] = {}
    for path in CONFIG_PATHS:
        try:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            out[str(path)] = "MISSING"
    return out


def _settings_js_role_option_keys() -> list[str]:
    """从 `settings.js` 读出 llm.role 那行 `options` 里每个二元组的第一项（key）。

    做括号配平而不是正则到行尾：那张表全在一行上，但下次有人把它折成多行时，
    「读到行尾」会静默少几项，漂移门就变成假绿。
    """
    source = SETTINGS_JS.read_text(encoding="utf-8")
    anchor = 'path: "llm.role"'
    hits = source.count(anchor)
    assert hits == 1, (
        f"`{anchor}` 在 settings.js 里出现 {hits} 次，解析锚点不唯一，"
        "读出来的选项表不可信")
    start = source.index("options:", source.index(anchor))
    depth = 0
    end = start
    while end < len(source):
        char = source[end]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        end += 1
    assert depth == 0, "options 数组括号没配平，解析器读到了错误位置"
    keys = re.findall(r'\[\s*"([^"]+)"\s*,', source[start:end + 1])
    assert keys, "解析器在 settings.js 里一个 role 选项都没读到——是解析器坏了，不是产品漂了"
    return keys


def _bar_button(page, name: str):
    """底部常驻保存条上的按钮（`settings.js:353`/`:361`）。"""
    return page.locator(".settings__bar").get_by_role("button", name=name, exact=True)


def _number_row(page, label_text: str):
    """按 label **全文**相等找到的数字字段行（用 exact 找 label，再上溯到 .field）。"""
    label = page.locator("label.field__label").filter(has_text=label_text).first
    assert label.inner_text().strip() == label_text, (
        f"「{label_text}」这一行没按预期找到，实得 {label.inner_text()!r}")
    return label.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' field ')][1]")


def test_the_option_table_in_settings_js_is_still_a_copy_of_the_backend_list():
    """门 1：`settings.js:60` 的选项 key 序列必须**逐项等于**后端 `ROLE_KEYS`。

    这条守的是 vendor 自己上报的那件事：那张表是第二个 owner，下次退役角色如果只改
    后端、忘了改前端，界面就会继续提供已经退役的角色（用户选了它，落到后端才被
    `VALIDATION` 挡回来——一个只有提交才会发现的坑）。

    不合并两张表是**决定**，不是遗漏：`summary.roles` 读不到时页面必须照常可用
    （那条需求已经编码在 `test_roles_endpoint_failure_does_not_break_the_settings_page`
    里，它断言 reject 时 `role_values == list(ROLE_KEYS)`）。既然保留第二份拷贝，
    就必须让它漂不动，而不是靠人记得。

    牙齿证明见交付：临时在后端多退役一个角色（改 `video_workbench/core/summary/__init__.py:36`
    的 `RETIRED_ROLES`），这条必须红；用开工前的原字节覆盖回写后必须绿。
    """
    keys = _settings_js_role_option_keys()
    assert keys == list(ROLE_KEYS), (
        f"settings.js 的角色选项表与后端 ROLE_KEYS 漂了：\n"
        f"  前端: {keys}\n"
        f"  后端: {list(ROLE_KEYS)}\n"
        "要么两边一起改，要么把这张表改成后端注入——但后者会撞 reject 那条需求，"
        "所以先照两边一起改办")


def test_an_empty_backend_default_falls_back_to_the_first_option_and_that_is_a_declared_boundary(
        browser, web_base_url):
    """门 2：后端 `default` 为空时下拉框退到第一项——**已声明的已知边界，不是理想行为**。

    vendor 交回的说法是「`default` 读不到时 `roleSelectValue` 退回配置值本身（不选任何
    项）」，**这句不成立**：`settings.js:279` 在这种情况下 `return want`，而 want
    （配置里那个脏值）不在选项里；`<select>` 收到一个不存在的 value 时，DOM 的行为就是
    `selectedIndex = 0`。证据是 vendor 自己的 before 树数据里就是 `selectedIndex=0`。

    现在后端 `describe_roles()` 永远带非空 `default`，所以这是假想边界，不为它改产品码。
    但也不许它变成「没人知道的意外」：这条把它钉成**已声明的行为**，同时留好可观测点
    （`selectedIndex == 0`）——将来谁把 default 改成可空、或者给下拉加了占位项，
    这条会红，那时才是做产品决策的时刻。真要改成「明确不选」需要加一个占位 option，
    属产品决策，不在此用例范围。
    """
    page, errors = open_settings(browser, web_base_url, {
        "settings_role": DIRTY_VALUE,
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
        "default": "",          # 假想边界：后端没给结论
    })
    try:
        # 判据写法 1：下面既读红字可见性、又要 select 可交互，前置必须自己造。
        require_expanded(page, "空 default 这条要读红字文案与 select 状态")
        select = role_row(page).locator("select")
        state = select.evaluate(
            "el => ({index: el.selectedIndex, value: el.value, "
            "options: [...el.options].map(o => o.value)})")
        # 下面三条各自独立成立，不写成 `A or B`——那种析取在 index 恒为 0 的现实下
        # 会永远为真，等于没断言（我在这个仓库上已经犯过一次）。
        assert state["index"] == 0, (
            f"空 default 时 DOM 应当退到第一项（selectedIndex==0），实得 {state}——"
            "如果这条红了，说明产品改成别的行为（比如加了占位项），需要重新定规格")
        assert state["options"] == list(ROLE_KEYS), (
            f"选项表本身被 default 影响到了：{state['options']}")
        assert state["value"] == ROLE_KEYS[0], (
            f"第一项应当是 {ROLE_KEYS[0]!r}，实得 {state['value']!r}")
        # 至少文字说明还在：下拉框亮着用户没选过的角色时，红字是唯一的解释来源。
        line = warn_line(page)
        assert line.is_visible(), "空 default 时红字不见了——用户将毫无线索"
        assert line.inner_text() == HINT
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()


def test_saving_an_unrelated_change_does_not_write_the_role_at_all(browser, web_base_url):
    """门 3a：脏配置下改**别的字段**再保存，落盘的东西里根本不该有 `llm.role`。

    这条才是「用户顺手保存不会把 standard 改成他没选过的角色」这句承诺的正面证据：
    `settings.js:223 buildPatch` 只把标了脏的 path 放进 payload（`:390` 的脏判定是
    `JSON.stringify(value) !== JSON.stringify(getPath(base, path))`），而 `roleSelectValue`
    只改**显示**、不改 `current`，所以显示着 wengao、配置里还是 standard、脏表里根本没有
    llm.role —— 落盘保持原值，既不变成 xueshu 也不悄悄变成 wengao。

    反空断言：必须确确实实发出了一次 `app.settings.put` 并且 payload 里带了我改的那个
    字段，否则「payload 里没有 llm.role」会因为「压根没保存」而空转。
    """
    before_files = _config_fingerprints()
    page, errors = open_settings(browser, web_base_url, {
        "settings_role": DIRTY_VALUE,
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
    })
    try:
        # 判据写法 1：「温度」这一栏在折叠组**里面**，`_number_row` 用 inner_text 比对标签，
        # 收起态下 inner_text 是空串（隐藏子树），fill 也要求可交互——所以这里必须先把组
        # 显式打开并断言打开。原来靠「脏态自动展开」隐式成立，自动展开一失效就变成
        # 说不清原因的红。
        require_expanded(page, "要操作折叠组内的「温度」输入框")
        row = _number_row(page, "温度")
        row.locator("input").fill("0.33")
        save = _bar_button(page, "保存")
        expect_enabled = page.locator(".settings__bar .muted").first
        assert "待保存" in expect_enabled.inner_text(), (
            f"改了温度却没进脏表：{expect_enabled.inner_text()!r}")
        assert not save.is_disabled(), "改了字段但保存按钮还是灰的"
        save.click()
        page.wait_for_function("() => window.__rpcSends.length > 0", timeout=10000)

        sends = page.evaluate("() => window.__rpcSends")
        assert len(sends) == 1, f"应当恰好发出一次 app.settings.put，实得 {len(sends)} 次"
        values = (sends[0] or {}).get("values") or {}
        assert values.get("llm", {}).get("temperature") == 0.33, (
            f"payload 里没带上我改的温度，这条断言会空转：{values}")
        assert "role" not in (values.get("llm") or {}), (
            f"只改了温度，落盘却动了角色：{values}——"
            "脏配置会被顺手改掉，用户选都没选过它")
        assert _config_fingerprints() == before_files, (
            "界面用例写了真实配置文件——离线 mock 不该有落盘副作用")
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()


def test_confirming_the_displayed_default_and_saving_writes_that_default(browser, web_base_url):
    """门 3b：用户真的去**确认**了那个显示出来的默认角色再保存，落盘就是后端 default。

    3a 管的是「没碰它就不该写它」，这条管的是「碰了就要写对」：用户看见框里亮着
    「文稿编辑」，动一下下拉（选中的正是显示值），保存下去必须是后端 default——
    既不能是配置里那个已经停用的 `standard`，也不能是 DOM 副作用第一项 `xueshu`。
    这就是 lead 给用户那句承诺的另一半。

    `DEFAULT_ROLE` 从后端权威层 import，不在测试里写字面量 `"wengao"`：前端不许有第二个
    owner，测试也不该有。
    """
    before_files = _config_fingerprints()
    page, errors = open_settings(browser, web_base_url, {
        "settings_role": DIRTY_VALUE,
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
    })
    try:
        # 判据写法 1：`select_option` 要做可交互性检查，收起态（真隐藏）下会直接超时，
        # 所以这条的前置也得自己造出来并断言住。
        require_expanded(page, "要真的去点下拉框确认显示值")
        select = role_row(page).locator("select")
        shown = select.input_value()
        assert shown == DEFAULT_ROLE, (
            f"前置不成立：显示的不是后端 default（{DEFAULT_ROLE!r}），实得 {shown!r}")
        assert shown != ROLE_KEYS[0], (
            "前置不成立：显示值就是第一项，那这条测不出「顺手保存改成第一项」这个坑")

        select.select_option(value=DEFAULT_ROLE)   # 用户顺手确认一下显示的默认角色
        save = _bar_button(page, "保存")
        assert not save.is_disabled(), "选中显示值后保存按钮仍灰着"
        save.click()
        page.wait_for_function("() => window.__rpcSends.length > 0", timeout=10000)

        sends = page.evaluate("() => window.__rpcSends")
        assert len(sends) == 1, f"应当恰好发出一次 app.settings.put，实得 {len(sends)} 次"
        values = (sends[0] or {}).get("values") or {}
        written = (values.get("llm") or {}).get("role")
        assert written == DEFAULT_ROLE, (
            f"确认后保存的角色应为后端 default {DEFAULT_ROLE!r}，实得 {written!r}")
        assert written != DIRTY_VALUE, "落盘把已停用的退役角色又写回去了"
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()


# ==================================================== N1 / N2 / N3：折叠态可达性（task-20 的牙）


def _page_band(page) -> tuple[int, int]:
    """.page 这个滚动容器的可视带（视口坐标）。可达性一律相对它判，不相对屏幕。"""
    band = page.evaluate(
        """() => {
             const p = document.querySelector(".page");
             const r = p.getBoundingClientRect();
             return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                     scrollHeight: p.scrollHeight, clientHeight: p.clientHeight};
           }""")
    return band


@pytest.mark.parametrize("skin", SKINS)
def test_collapsed_group_is_really_hidden_not_just_painted(browser, web_base_url, skin):
    """N1（**这条才是 task-19 那个缺陷的牙**）：收起态必须真隐藏，不许「画出来但滚不到」。

    task-19 之前的旧 CSS 把 `.adv__body{display:flex}` 无条件写上，盖掉了 UA 对未 open
    `<details>` 内容区的隐藏。实测（1384×797）：内容盒高 **1357px**、「附加提示词」那一行被
    绘制在 bottom=**1287**，而 `.page` 的 scrollHeight 只有 **1054**、clientHeight 721 ⇒
    最多滚 333px，那一行**画得出来、滚不到、点不到**。修复后两者都是 **0**。

    为什么既有 11 条用例对这个缺陷**免疫**（我实测过、lead 也承认他预测反了）：
    Playwright 的 `has_text` 走 textContent，收起态照样定位得到；而 `is_visible()` 在
    「被绘制」时返回 True——所以旧断言既不依赖它、也没守住它。能区分的只有几何 + display
    这一对，`is_visible()`/`inner_text()` 都不行（见文件头判据段）。
    """
    page, errors = open_settings(browser, web_base_url, {
        "configured_unrecognized": CLEAN_VALUE,
        "settings_role": CLEAN_VALUE,
    }, viewport=DEAD_ZONE_VIEWPORT)
    try:
        apply_skin(page, skin)
        require_collapsed(page, "N1 测的就是收起态")
        body = _box(page.locator("details.adv .adv__body").first)
        assert body["display"] == "none", (
            f"收起态内容盒的 computed display 必须是 none，实得 {body['display']!r}——"
            "只要它不是 none，内容就会被绘制出来却不计入滚动高度")
        assert body["height"] == 0, (
            f"收起态内容盒必须有 0 高度（旧 CSS 是 1357），实得 {body['height']}")
        # 组内「附加提示词」那一行（A 新加的开关，797px 下点不到的就是它）
        row = switch_row(page, "附加提示词")
        row_box = _box(row.first)
        assert row_box["height"] == 0, (
            f"收起态里「附加提示词」那一行仍有 {row_box['height']}px 高的盒子"
            "（旧 CSS 实测 65）——它被绘制了，但落在 .page 可滚范围之外")
        # 反向对照：内容不许「画在滚得到的范围之外」。这条把上面两个数与滚动容器绑起来，
        # 谁将来给 .adv__body 加 height:0 但 overflow:visible 之类的写法，这条会红。
        band = _page_band(page)
        assert body["bottom"] <= band["bottom"] + 1 or body["height"] == 0, (
            f"内容盒底边 {body['bottom']} 超出 .page 可视带 {band}，且高度不为 0 ⇒ 死区")
        assert errors == [], f"控制台有 error：{errors}"
        page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_t20_collapsed.png"))
    finally:
        page.close()


@pytest.mark.parametrize("skin", SKINS)
def test_expanded_group_reaches_the_switch_and_the_line_by_scrolling(browser, web_base_url, skin):
    """N2：展开态下，红字与组内那个新开关必须**真的滚得到、点得动**（视口钉 1384×797）。

    如实记一件事：这条在**旧 CSS 下不会红**——展开态两版布局相同（实测数字一致），
    旧 CSS 的病在收起态，归 N1。它的牙在另一类回归上：谁把 `.adv[open] .adv__body`
    改成受限高度 / `overflow:hidden`、或给 `.page` 加 `overflow:hidden`，这条立刻红
    （配方与实测见文件头「牙齿配方」，变体二）。

    开关控件是 `<button class="switch" role="switch" aria-checked>`（`settings.js:1356-1367`），
    **没有 input**，所以状态只能读 `aria-checked`——用 `input.is_checked()` 会超时，
    那是探针判据错，不是产品点不到（vendor 踩过）。
    """
    page, errors = open_settings(browser, web_base_url, {
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
        "settings_role": DIRTY_VALUE,
    }, viewport=DEAD_ZONE_VIEWPORT)
    try:
        apply_skin(page, skin)
        require_expanded(page, "N2 测展开态可达性")
        targets = [("红字", warn_line(page).first),
                   ("附加提示词那一行", switch_row(page, "附加提示词").first)]
        for label, target in targets:
            target.scroll_into_view_if_needed()
            # **同一次快照**里取行、内容盒、滚动容器三个 rect：上一条犯的错是先量内容盒、
            # 再 scroll、后量行——滚完之后拿旧坐标比，得到「行超出盒外」的假红
            # （实测 391 vs 989 就是这么来的）。跨时刻的坐标不可比。
            g = target.evaluate(
                """el => {
                     const r = el.getBoundingClientRect();
                     const body = document.querySelector('.adv__body');
                     const br = body.getBoundingClientRect();
                     const p = document.querySelector('.page');
                     const pr = p.getBoundingClientRect();
                     const cs = getComputedStyle(body);
                     return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                             height: Math.round(r.height),
                             bodyTop: Math.round(br.top), bodyBottom: Math.round(br.bottom),
                             bodyHeight: Math.round(br.height), bodyDisplay: cs.display,
                             bodyMaxHeight: cs.maxHeight, bodyOverflow: cs.overflow,
                             pageTop: Math.round(pr.top), pageBottom: Math.round(pr.bottom),
                             scrollHeight: p.scrollHeight, clientHeight: p.clientHeight};
                   }""")
            assert g["height"] > 0, f"{label} 展开态下高度为 0，等于没渲染"
            assert g["top"] >= g["pageTop"] - 1 and g["bottom"] <= g["pageBottom"] + 1, (
                f"{label} 滚完之后仍不在 .page 可视带内：{g}"
                f"（scrollHeight={g['scrollHeight']} clientHeight={g['clientHeight']}）")
            # 内容盒本身要有实际高度，且行必须落在盒内：牙齿变体二实测教训——只查行的 rect
            # 会被 `max-height:0;overflow:hidden` 糊过去（被裁剪的子元素 rect 照旧有高度、
            # 照旧落在 .page 带内，连 click() 都能过），补上这一对比才抓得住「被裁掉」。
            assert g["bodyHeight"] > 0, (
                f"展开态内容盒高度为 {g['bodyHeight']}（maxHeight={g['bodyMaxHeight']} "
                f"overflow={g['bodyOverflow']}）——组里所有字段都被裁掉了")
            assert g["top"] >= g["bodyTop"] - 1 and g["bottom"] <= g["bodyBottom"] + 1, (
                f"{label} 的 rect 超出了内容盒 [{g['bodyTop']}, {g['bodyBottom']}]："
                f"行在 {g['top']}–{g['bottom']}，说明它被祖先裁剪掉了，"
                "视觉上不存在（rect 有高度不等于被画出来）")
        # 真点一下之前，先做**命中测试**：元素中心点必须真的落在这个按钮（或它自己的内部件）上。
        # 为什么不能只靠 rect 落在可视带内 + `click()`：牙齿变体二实测证明——给
        # `.adv[open] .adv__body` 加 `max-height:0;overflow:hidden` 时，子元素 rect 照旧有高度、
        # 照旧落在 `.page` 带内，连 `click()` 都能过（Playwright 的可点击判定不含裁剪检查），
        # 但视觉上整组被裁掉了。那是一种「绿着但什么都没守」的写法，必须用 elementFromPoint
        # 把它抓住（task-19 预备时我用同样的探针在收起态抓到了 HEADER.topbar）。
        hit = switch_row(page, "附加提示词").locator("button.switch").first.evaluate(
            """el => {
                 const r = el.getBoundingClientRect();
                 const h = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                 return {tag: h && h.tagName, cls: h && String(h.className).slice(0, 40),
                         painted: !!h && (h === el || el.contains(h)),
                         clipped: getComputedStyle(el.parentElement || el).overflow};
               }""")
        assert hit["painted"], (
            f"「附加提示词」开关中心点命中的是 {hit['tag']}.{hit['cls']}，不是它自己——"
            f"它被祖先（overflow={hit['clipped']}）裁剪或遮挡，用户点不到")

        # 可达性的终判：点得动，并且状态真的翻了
        switch = switch_row(page, "附加提示词").locator("button.switch").first
        before = switch.get_attribute("aria-checked")
        switch.click()
        page.wait_for_timeout(200)
        after = switch_row(page, "附加提示词").locator("button.switch").first.get_attribute("aria-checked")
        assert after != before, (
            f"点了开关状态没翻（aria-checked {before} → {after}）——按钮不可达或没接上")
        assert after in ("true", "false"), f"aria-checked 不是布尔字面量：{after!r}"
        assert errors == [], f"控制台有 error：{errors}"
        page.screenshot(path=str(ACCEPT_DIR / "wb_settings_role_t20_expanded.png"))
    finally:
        page.close()


@pytest.mark.parametrize("skin", SKINS)
def test_expanded_group_keeps_the_source_line_inside_its_width(browser, web_base_url, skin):
    """N3（lead 标可选，我判断值得钉）：展开态下那行来源说明不许横向溢出。

    A 把提示词真源写进 `.field__help`（`settings.js:1588-1601`，`white-space:pre-line`），
    形如 `wengao ← 文稿编辑.txt · 8479 B · 9dae7c10`。文件名长 + 中文字宽，窄窗口下有溢出风险。
    判据只钉**不变量** `scrollWidth == clientWidth`，不钉 lead 实测的 500/858——那两个数跟
    视口绑，换个窗口就假红。

    只能在展开态钉：实测净态两行 help 都被折叠掉（`helpVisible=[]`），脏态才有 `[[0,1]]`；
    在收起态钉它就是判据写法 3（禁止的那种）。
    """
    page, errors = open_settings(browser, web_base_url, {
        "configured_unrecognized": DIRTY_VALUE,
        "configured_hint": HINT,
    }, viewport=DEAD_ZONE_VIEWPORT)
    try:
        apply_skin(page, skin)
        require_expanded(page, "来源说明只在展开态可见")
        rows = page.locator("details.adv .field__help")
        count = rows.count()
        assert count >= 1, "折叠组里一行 help 都没有——A 的来源说明没渲染"
        for i in range(count):
            box = rows.nth(i).evaluate(
                "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, "
                "text: el.textContent.slice(0, 40)})")
            assert box["scrollWidth"] <= box["clientWidth"], (
                f"第 {i} 行来源说明横向溢出：scrollWidth={box['scrollWidth']} > "
                f"clientWidth={box['clientWidth']}，内容 {box['text']!r}")
        assert errors == [], f"控制台有 error：{errors}"
    finally:
        page.close()
