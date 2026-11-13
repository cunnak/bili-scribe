# v0.9.10 / VideoWorkbench 0.2.9（打包版）

## 这个版本解决什么问题

0.2.8 的轻量模式版本卡把模型写的 `revise_note` 统一标成「实际改动」；恢复操作写入的
`revise_note` 是「恢复自第 N 版」，于是恢复来源被误读成内容变化，真实字段差异反而
没有展示。0.2.9 把版本列表拆成「恢复自 / 它说 / 实际改动」三个独立标签。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/app/service.py` | 版本行只带 `revise_note`，没有字段差异 | `_version_rows` / `_version_row` 按相邻版本计算 `changed_fields`；恢复时记录 `restored_version` |
| `video_workbench/web/js/pages/summary.js` | 一律用「实际改动」显示 `revise_note` | 显示 `恢复自` / `它说` / `实际改动` 三个独立标签；只有真实字段差异才出现「实际改动」 |
| `video_workbench/web/css/app.css` | 多行说明没有行距 | `.ver-row__note-line + .ver-row__note-line` 增加间距 |

## 验证

- 服务与路由回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary.py tests/wb/test_summary_usage.py tests/wb/test_summary_light.py tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py tests/wb/test_version_owner.py -q` → **95 passed in 5.67s**
- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（112 项）**
- 静态检查：`.venv/Scripts/ruff.exe check ...` → `All checks passed!`；`format --check ...` → `5 files already formatted`
- 色彩闸：`.venv/Scripts/python.exe scripts/check_colors.py` → **OK**
- 版本 owner：包含在上述 95 条中

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.2.9`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → 退出码 0（PyInstaller 日志完成于 64.9s）
- 包内自检：`dist/VideoWorkbench/wb-check.json` → `info.version = 0.2.9`，`ok=true`；包目录 413.5 MB / 2164 个文件
- 冻结包验收：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**
- 发布资产：`dist/VideoWorkbench-0.2.9.zip`，**1959 个文件 / 1959 个条目 / 177,906,311 B**，SHA256 `4A8475936B873B57B49B646F070D854F2B6A577C6A7525AEDFD1A91C4E725B7A`，`ZipFile.testzip() = None`
- 包内核验：`_internal/video_workbench/web/js/pages/summary.js` 含 `versionNotes` / `restored_version` / `changed_fields`；包内与仓库该文件 SHA256 均为 `934AAF6D5C3F9F2A28DB5FD8CA8F292295F77DB36FC38EE6193D298F2B8EB85C`
- commit：`79c2850 fix(summary): 版本列表区分模型自述、恢复来源与实际改动并抬到 0.2.9`
- tag：`v0.9.10`
- release：`VideoWorkbench 0.2.9（打包版）`
