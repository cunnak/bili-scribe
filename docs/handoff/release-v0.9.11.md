# v0.9.11 / VideoWorkbench 0.2.10（打包版）

## 这个版本解决什么问题

0.2.9 只把版本卡里的「恢复自第 N 版」从「实际改动」里拆了出来，但点击恢复仍然会插入一条
新的 `wb_summary` 版本——用户看到版本数继续增长，恢复并没有真正回到旧版作为当前版。
0.2.10 把恢复改成「只移动当前版本指针」：历史版本不删、不复制，版本列表不再新增一行。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/infra/repo.py` | `summary_latest` 只看最大 version；恢复与生成都靠插入新行 | 新增 `wb_meta` 当前版本指针；插入新版本推进指针，`summary_latest` 优先读指针；旧库无指针时回退到最大 version |
| `video_workbench/app/service.py` | `summary_restore` 调用 `summary_insert`，生成新版本并写 `restored_version` | `summary_restore` 只校验项目归属并调用 `summary_set_current`，不新增版本 |
| `video_workbench/web/js/pages/summary.js` | 提示「已恢复第 N 版（存成了新的一版，旧版都还在）」 | 提示「已恢复第 N 版」 |
| `video_workbench/web/js/mock.js` | 恢复会把旧版本复制到版本列表最前面 | 恢复只切换当前版本，历史列表保持原样 |
| 回归用例 | 断言恢复新增版本 | 断言恢复不新增版本、版本数不变、当前标记落到目标版本 |

## 验证

- 服务与路由回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary.py tests/wb/test_summary_usage.py tests/wb/test_summary_light.py tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py tests/wb/test_version_owner.py -q` → **95 passed in 5.59s**
- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（112 项）**
- 轻量浏览器验收：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary_light_ui.py -q` → **4 passed in 3.72s**
- 静态检查：`.venv/Scripts/ruff.exe check ...` → **All checks passed!**；本次格式化范围 `video_workbench/__init__.py`、`video_workbench/app/service.py`、`tests/wb/test_summary.py`、`tests/wb/test_summary_light_ui.py` → **4 files already formatted**
- 色彩闸：`.venv/Scripts/python.exe scripts/check_colors.py` → **OK**
- 冻结包验收：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**
- 发布资产：`dist/VideoWorkbench-0.2.10.zip`，**1959 个文件 / 1959 个条目**，**177,909,870 B**，SHA256 `631BB72EA61C8C52517BD0B67213D46D71820917A3A895E9895E05FD7E1FE1EF`，`ZipFile.testzip() = None`

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.2.10`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → 退出码 0
- 包内自检：`dist/VideoWorkbench/wb-check.json` → `info.version = 0.2.10`，`ok=true`
- commit：`6d20110 fix(summary): 恢复旧版只切换当前版本且不再新增一版并抬到 0.2.10`
- release：`gh release create v0.9.11 dist/VideoWorkbench-0.2.10.zip --title "VideoWorkbench 0.2.10（打包版）" --notes-file docs/handoff/release-v0.9.11.md`，发布页 <https://github.com/cunnak/bili-scribe/releases/tag/v0.9.11>
