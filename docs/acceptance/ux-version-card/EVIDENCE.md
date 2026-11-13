# 版本卡恢复语义修复验收（0.2.10）

## 问题

0.2.9 的版本卡虽然分开了「恢复自 / 它说 / 实际改动」标签，但点击恢复仍然会插入一条新的
`wb_summary` 版本；用户看到版本数继续增长，恢复并没有真正回到旧版作为当前版。

## 修复

- `video_workbench/infra/repo.py`：新增当前版本指针（存 `wb_meta`），
  `summary_latest` 优先读指针；新生成版本会把指针移到新版本。
- `video_workbench/app/service.py`：`summary_restore` 只移动当前指针，不新增版本、
  不复制正文；历史版本全部保留。
- `video_workbench/web/js/pages/summary.js` / `mock.js`：恢复提示改为
  「已恢复第 N 版」；mock 同步为不新增版本。
- 旧版本行仍可显示模型自述 `它说` 与真实字段差异 `实际改动`；恢复不再写入
  「恢复自第 N 版」的新行。

## 证据

- 服务与路由回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary.py tests/wb/test_summary_usage.py tests/wb/test_summary_light.py tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py tests/wb/test_version_owner.py -q` → `95 passed in 5.59s`
- 前端回归：`node tests/wb/frontend_test.js` → `全部通过（112 项）`
- 轻量浏览器验收：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary_light_ui.py -q` → `4 passed in 3.72s`
- 静态检查：`.venv/Scripts/ruff.exe check ...` → `All checks passed!`
- 验收截图：`fixed-light-version-card.png`（版本列表不再新增恢复行，当前标记落到被恢复版本）
