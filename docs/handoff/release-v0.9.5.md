# v0.9.5 / VideoWorkbench 0.2.4（打包版）

## 这个版本解决什么问题

本地导入的项目没有 `bvid` / `aid`，总结页发布卡一直卡在「这个项目没有 BV 号，发不了笔记」。
这一版在发布卡加了「视频号」两个输入栏：BV 号或 av 号任填一个，保存草稿后写入
`wb_project.publish_bvid` / `publish_aid`，不碰下载去重用的 `bvid` / `aid`，发布门禁随之放行。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/infra/db.py` | `wb_project` 只有下载身份列 | schema v6 新增 `publish_bvid` / `publish_aid` |
| `video_workbench/app/service.py` | 本地项目没有视频号，预览/保存/发布都拿不到 BV | 保存草稿时校验并绑定手填 BV/AV；预览、读取、重新生成、发布、撤回都使用绑定值 |
| `video_workbench/core/publisher.py` | 缺 BV 的门禁只说「发不了」 | 文案指向发布卡「视频号」输入栏，保存草稿后生效 |
| `video_workbench/web/js/pages/publish.js` | 没有输入栏 | 新增 `#pb-bvid` / `#pb-aid`，支持脏检查和保存后刷新 |
| `video_workbench/web/js/mock.js`、`docs/用户手册.md` | 没有演示与说明 | mock 同形状返回 `video`，手册补操作步骤 |

## 验证

- 目标回归：`pytest tests/wb/test_publish_local_video.py tests/wb/test_publish_local_video_ui.py tests/wb/test_publish_gates.py tests/wb/test_publish_do.py tests/wb/test_service_publish.py tests/wb/test_db.py tests/wb/test_library_migration_checks.py -q` → `85 passed in 19.29s`
- 字节锁与 git 归一化：`pytest tests/wb/test_summary_drift.py tests/wb/test_git_no_normalization_on_locked_paths.py -q` → `52 passed in 4.40s`
- 冻结包：`scripts/accept_frozen_package.py` → `16/16 PASS`（含包内 `publish.js` 有本地项目视频号输入栏）
- 发布资产：`dist/VideoWorkbench-0.2.4.zip`，1959 个文件，`testzip` 无坏块。

## 验证到什么程度

- 真实 B 站发帖没有实际打网络：验收覆盖「绑号 → 落库 → 门禁放行」，真实登录态下的发布接口未调用。
- 打包版验收里的 `--check` / `--roles` 跑的是冻结 exe，配置前后 sha256 已核对未变。

## 发布执行记录

- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → PyInstaller 退出码 0，`--check` 报版本 `0.2.4`。
- tag：`v0.9.5`（指向本版提交）。
- release：`gh release create v0.9.5 "dist/VideoWorkbench-0.2.4.zip" --title "VideoWorkbench 0.2.4（打包版）"`。
