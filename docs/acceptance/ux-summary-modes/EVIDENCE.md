# EVIDENCE — 总结页 / 轻量总结三处修复

日期：2026-11-13
范围：`video_workbench/web/js/pages/summary.js`、`publish.js`、`video_workbench/web/css/summary.css`、`app.css`、`tests/wb/*`。

## 事实与判据

| # | 类型 | 事实 | 确认方式 |
|---|---|---|---|
| E1 | FACT | 原实现把轻量作为 `state.source` 的一个取值；从本地项目切过去会复用 `state.projectId`，正文仍渲染上一个项目。 | ✅ 可命令：修复前真浏览器截图与源码 `switchSource` 已删除；新实现见 `summary.js` 的 `createSummaryPage(kind)`。 |
| E2 | FACT | AV/BV 输入框原来固定 `min-width:220px`，1525px 下实测溢出 `.sum-fld` 113px。 | ✅ 可命令：修复前 Playwright 几何读数；修复后 `light_1100/1525/2306/3840` 实测输入右边缘始终 `<=` 字段右边缘且 `scrollWidth == clientWidth`。 |
| E3 | FACT | 轻量页原先没有独立的角色/模型控件。 | ✅ 可命令：`tests/wb/test_summary_light_ui.py` 断言 `#sum-light-role` / `#sum-light-model` 可操作且选项数 `>=2`；`frontend_test.js` 断言 `summary.start` 携带两值。 |
| E4 | FACT | 本地页与轻量页现在注册为两个路由、两个 `createSummaryPage` 实例，state 不共用。 | ✅ 可命令：`node tests/wb/frontend_test.js` 的「总结页与轻量总结是两条独立路由」「本地项目与轻量页的 state 互不覆盖」；`test_web_router_lifecycle.py` 的 destroy 断言。 |
| E5 | FACT | 轻量页没有项目时不挂本地项目的发布卡与版本/打磨/用量侧栏。 | ✅ 可命令：`summary.js` 的 `paint()` 分支；真浏览器截图 `docs/acceptance/ux-summary-modes/light_1525.png`。 |
| E6 | CONVENTION | 仓库要求：全量回归用 `.venv/Scripts/python.exe -m pytest tests/wb -q`；格式/静态用 `ruff`；新测试文件需 `git add`（本笔未新增 `tests/wb/*.py`）。 | ✅ 可命令：`AGENTS.md` 第三点五节、第六节。 |
| E7 | AMBIGUITY | 全量回归第一次跑到 `test_build_preflight_live_process.py` 时，机器上有一个 9:09 起运行的 `dist\VideoWorkbench\VideoWorkbench.exe`，该用例按设计要求先关闭旧实例。 | ✅ 可命令：`Get-Process -Id 54548` 现读；同一命令输出 `StartTime 2026/11/13 9:09:47`。 |
| E8 | EDGE | 轻量页长标题会进入 `#sum-light-meta`，不参与字段行宽度竞争；四档视口 meta 顶边都高于字段底边至少 12px。 | ✅ 可命令：截图脚本输出 `metaTop - fieldsBottom = 12`（1100/1525/2306/3840）。 |

## 复跑命令

```powershell
node tests/wb/frontend_test.js
$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -m pytest tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py tests/wb/test_summary_light.py -q
$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -m pytest tests/wb/test_publish_local_video_ui.py tests/wb/test_publish_gates.py -q
$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -m pytest tests/wb/test_summary_error_display.py -q -k 'not untouched_copy and not burn_when'
.venv/Scripts/ruff.exe check tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py
.venv/Scripts/ruff.exe format --check tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py
```

## 截图

- `docs/acceptance/ux-summary-modes/light_1100.png`：1100×720 窄窗口，字段换行但仍不出白框。
- `docs/acceptance/ux-summary-modes/light_1525.png`：1525×900 常用窗口，角色/模型/AV-BV 同排。
- `docs/acceptance/ux-summary-modes/light_3840.png`：3840×1600 全屏，输入框右边缘仍收在字段内。
- `docs/acceptance/ux-summary-modes/local_1525.png`：本地项目页对照，仍保留项目/角色/模型栏。

## 已知限制

- 全量 pytest 第一次跑到 `test_build_preflight_live_process.py` 时，被机器上已运行的旧 `dist\VideoWorkbench\VideoWorkbench.exe`（PID 54548，9:09 起）阻断；这是该用例设计要求的独占总线前置条件，不是产品代码失败。
- 宽口径回归（排除 build preflight 与 summary error display 两个文件）：`1949 passed, 1 skipped, 5 failed in 144.96s`。5 条都在 `test_settings_role_warning_ui.py`（折叠组 `open` 被意外展开），该文件单独跑仍为 `5 failed, 13 passed in 8.61s`；本笔没有改 `settings.js`/settings 相关 CSS，这是既有基线问题，未计入本笔验收。
- 本笔相关测试分组全部通过：frontend 112、light/lifecycle/light 48、publish 23、summary error display 26（6 deselected）。
