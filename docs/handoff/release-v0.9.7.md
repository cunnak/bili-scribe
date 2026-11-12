# v0.9.7 / VideoWorkbench 0.2.6（打包版）

## 这个版本解决什么问题

之前发布笔记时，`x/note/add` 的 `auto_comment` 被写死为 0，笔记不会同步到视频评论区。用户在发布笔记时看到 B 站的「审核通过后，将同步至动态及评论区」，但 VideoWorkbench 发出去的笔记没有评论卡。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/core/publisher.py` | `auto_comment: 0`，发布后评论区没有笔记卡 | `auto_comment: 1`，平台把 `summary` 预览作为评论卡同步到视频评论区 |
| `video_workbench/core/publisher.py` | `summary` 只作为必填占位 | 仍从正文开头取前 80 字；作为评论卡预览，不读取老草稿里已下线的 summary |
| `video_workbench/app/service.py` | 服务层没有传递评论同步开关 | 发布时读取 `PublishConfig.auto_comment`，默认开启 |
| `video_workbench/web/js/pages/publish.js` | 发布确认、成功、撤回提示不提评论区 | 统一说明「同步至动态及评论区」「撤回后动态与评论区里的笔记卡消失」 |
| `video_workbench/config.py` | `auto_comment` 字段未参与发布链路 | 接入服务层发布调用，默认值 `True` |
| `tests/wb/test_publish_do.py` | 只断言请求能发出 | 断言 payload 默认 `auto_comment=1`、显式关闭为 0、服务层发布也带 1 |
| `docs/用户手册.md` | 只写发布笔记，不提同步位置 | 写明保存草稿后发布，笔记同步到视频评论区 |

## 验证

- 发布链路回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_publish_do.py tests/wb/test_publish_draft.py tests/wb/test_service_publish.py tests/wb/test_publish_gates.py -q -p no:randomly` → **95 passed in 12.42s**
- 版本 owner：`.venv/Scripts/python.exe -m pytest tests/wb/test_version_owner.py -q -p no:randomly` → **6 passed in 1.07s**
- 前端回归：`node tests/wb/frontend_test.js` → **107 项全过**
- 真浏览器发布卡：`.venv/Scripts/python.exe -m pytest tests/wb/test_publish_local_video_ui.py -q -p no:randomly` → **1 passed in 2.32s**
- Ruff：`.venv/Scripts/ruff.exe check ...` / `ruff.exe format --check ...`（5 个 Python 文件）→ **All checks passed，5 files already formatted**
- 真实草稿 payload：`draft_ac84a4c5c755` → `auto_comment=1`，`summary_chars=80`，`body_bytes_with_csrf=17622`，`cont_len=2136`；显式关闭时 `auto_comment=0`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → PyInstaller 退出码 0，耗时 160.0s，`--check` 报版本 `0.2.6`
- 冻结包：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**
- 发布资产：`dist/VideoWorkbench-0.2.6.zip`，1989 个 ZIP 条目（1959 个文件），174,048,739 B，`testzip` 无坏块

## 验证到什么程度

- 真实 B 站发帖没有实际打网络：本地用真实数据库做了 payload 验证，真实登录态下的 `x/note/add` 未调用，评论区评论卡的实际渲染与审核同步效果仍需人工复验。
- 打包版验收证明冻结包包含本次构建、版本与模块符号；不替代真实账号发布。

## 发布执行记录

- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → PyInstaller 退出码 0，`--check` 报版本 `0.2.6`。
- tag：`v0.9.7`（指向本版提交）。
- release：`gh release create v0.9.7 "dist/VideoWorkbench-0.2.6.zip" --title "VideoWorkbench 0.2.6（打包版）"`。
