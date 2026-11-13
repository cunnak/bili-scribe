# v0.9.14 / VideoWorkbench 0.3.1（打包版）

## 这个版本解决什么问题

字幕样式页的「重命名」按钮点了没反应。

用户看到的是三个预设（圆角默认 / 默认 / 默认 2），点中一个、点「重命名」，界面什么都不变。
复现后是三层原因叠在一起，每一层单独都能让这个按钮死掉。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `web/js/components/ui.js` | 只有 `confirm` / `pick`，没有「问一句话」的原语 | 新增 `ui.prompt(opts)`（+98 行）：当场问新名字，`validate` 拦空名与「名字没变」，× / ESC / 遮罩 / `close()` 四条关闭路径都 resolve `null`，不会挂住 |
| `web/js/pages/styles.js` | `renamePreset()` 把 `state.draft.name` 原样提交 | 改用 `ui.prompt` 问名，再把新名字真的写进 `style.save` |
| `web/js/pages/styles.js` | `saveAsPreset()` 也是拿当前名直接存 | 改用 `ui.prompt`，预填「原名 副本」 |
| `web/js/pages/styles.js` | 内置预设时名字框 `disabled` | `nameLocked: false`，名字框永远可编辑 |
| `web/js/pages/styles.js` | 内置预设点重命名只贴一行红字 | 先问「复制一份再改」，确认后走另存 |
| `web/js/pages/styles.js` | 四个按钮没有 id | 补 `styles-save-as` / `styles-rename` / `styles-duplicate` / `styles-delete` |

### 三层根因

1. **提交的是原名。** 用户没在「基本」里改过名字，`renamePreset()` 提交的就是 `state.draft.name`。
   后端返回 `ok=True`（名字没变化，也不算错），列表一字未变 —— 现场表现就是「点了没反应」。
2. **名字框是死的。** `subtitle_style_form.js` 里 `if (opts.nameLocked) name.disabled = true;`，
   内置预设传 `nameLocked: builtin`，用户根本打不进字，也就没有别的路可走。
3. **内置预设没有出路。** 后端对内置预设改名返回 `ok=False`（`内置样式不可修改，请先复制一份`），
   而界面只是把这句话贴出来，没给「复制一份」这个动作。

## 验证

- 新增判据：`tests/wb/test_style_preset_rename_ui.py`（4 条，真 Chromium + 真 `WorkbenchService`，
  `page.expose_binding` 挂 RPC，没有放宽产品 CSP）→ **4 passed in 5.90s**
- 合并验收：`test_style_preset_rename_ui + test_styles_real_backend_ui + test_web_tristate
  + test_style_effective + test_styles` → **79 passed in 12.89s**
- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（113 项）**
- 静态检查：`.venv/Scripts/ruff.exe check video_workbench/web/js` → **All checks passed!**
- 全量回归：`tests/wb` 分 6 块跑完 → **2016 passed / 1 skipped / 7 failed**

### 牙齿（改坏验红）

| 改坏什么 | 红了哪条 | 恢复 |
|---|---|---|
| `nameLocked: false` → `builtin` | 只红 `test_the_name_field_of_a_builtin_preset_is_editable_not_a_dead_box` | sha256 一致 |
| `name: next` → `state.draft.name` | 只红 `test_renaming_a_preset_asks_for_the_new_name_and_applies_it` | sha256 一致 |

### 那 7 条失败不是本次引入

6 条既有：`test_settings_role_warning_ui.py` 5 条（折叠组展开状态）
+ `test_summary_error_display.py::test_the_page_does_not_rebuild_the_sentence_the_backend_already_joined`。
上一轮已用 `git archive HEAD` 取改动前的 web 副本、`WB_WEB_ROOT` 指过去跑同批用例，同样失败。

第 7 条 `test_build_preflight_live_process.py::test_a_live_instance_is_caught_and_released` 是
上一轮真 GUI 验收留下的 `VideoWorkbench.exe`（PID 8068，11-13 22:26 起）占着 `dist/`。
杀掉后单独复跑 → **4 passed in 0.41s**。真实失败是既有的 6 条。

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.3.1`
- 构建前预检：`.venv/Scripts/python.exe scripts/build_wb.py --check` → `[preflight] ✓ 前置条件齐`，`RC=0`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → `RC=0`，83.8s，目录 413.5 MB / 2166 个文件
- 包内自检：`dist/VideoWorkbench/wb-check.json` → `info.version = 0.3.1`，`ok=true`
- 冻结包验收：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**，`RC=0`
  （含「包内自显示版本 == 仓库 `__version__`」0.3.1 == 0.3.1、「验收前后三份真实配置一字未动」）
- 包内资源核对：`_internal/video_workbench/web/js/components/ui.js` 命中 `ui.prompt` 1 处；
  `.../pages/styles.js` 命中「重命名预设」1 处、`nameLocked: false` 1 处
- 提交与发布：见下方

## 没验证到的

- 没在打包版 exe 里手动点过这个按钮（只核对了包内资源字节与 `--check` 载荷）。
- 既有那 6 条失败没修。
