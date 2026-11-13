# 轻量模式版本卡修复验收（0.2.9）

## 问题

版本列表把模型写的 `revise_note` 一律标成「实际改动」。恢复操作写入的
`revise_note` 是「恢复自第 N 版」，于是恢复来源被显示成内容改动；真实字段差异
反而没有展示。

## 修复

- `video_workbench/app/service.py`：`_version_rows` / `_version_row` 按相邻版本
  计算 `changed_fields`，复用 `video_workbench.core.summarizer.diff_fields`；
  `summary_restore` 记录 `restored_version`。
- `video_workbench/web/js/pages/summary.js`：版本行拆成 `恢复自` / `它说` /
  `实际改动` 三个独立标签；只有真实字段差异才显示「实际改动」。
- 第一版和恢复版的 `changed_fields` 为 `None`，不伪造前后差异。

## 证据

- 服务与路由回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary.py tests/wb/test_summary_usage.py tests/wb/test_summary_light.py tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py -q` → `89 passed in 5.59s`
- 前端回归：`node tests/wb/frontend_test.js` → `全部通过（112 项）`
- 静态检查：`.venv/Scripts/ruff.exe check ...` → `All checks passed!`；`format --check` → `4 files already formatted`
- 浏览器验收截图：`fixed-light-version-card.png`；修复前标签为 `['它说']`，恢复后为 `['恢复自', '它说']`，控制台无报错。
