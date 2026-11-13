# v0.9.12 / VideoWorkbench 0.2.11（打包版）

## 这个版本解决什么问题

轻量总结输入一个属于合集的 BV 号时，解析器会按下载器需要把整个合集的条目都返回；轻量层原来直接取 `items[0]`，于是用合集第一集的 `cid` 去取字幕，却把用户输入的那条 BV 当成标题，出现「提交的是第二集、总结的是第一集」。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/app/service.py` `_light_probe` | `first = items[0]`，`bvid/aid/cid/title/duration` 全取第一集 | 用 `resolver.identify(raw)` 得到用户输入的 BV/av，在 `res.items` 里定位对应条目；字幕、标题、时长、合集序号都取这一条 |
| `video_workbench/app/service.py` `_light_prepare` | 建项目时 `collection_index=None`、`part_title=""` | 把选中条目的 `collection_index` 与 `part_title` 传给项目 |
| `tests/wb/test_summary_light.py` | 只有单条视频 fixture | 新增合集 fixture，断言输入第二集时 probe 与 prepare 都取第二集 |

下载链路 `Resolver` 仍然返回整个合集，下载合集的行为不变；本次只修轻量总结的选择层。

## 验证

- 服务与路由回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary.py tests/wb/test_summary_usage.py tests/wb/test_summary_light.py tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py tests/wb/test_version_owner.py -q` → **97 passed in 5.61s**
- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（112 项）**
- 解析器基线：`.venv/Scripts/python.exe -m pytest tests/wb/test_resolver.py -q` → **30 passed in 0.20s**
- 静态检查：`.venv/Scripts/ruff.exe check video_workbench/app/service.py tests/wb/test_summary_light.py` → **All checks passed!**；`format --check` → **2 files already formatted**
- 冻结包验收：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**
- 发布资产：`dist/VideoWorkbench-0.2.11.zip`，**1959 个文件 / 1959 个条目**，**177,910,734 B**，SHA256 `85C7C41E6C2D9477496E39352674444BD07C3859A816A133257504F95E2B900B`，`ZipFile.testzip() = None`

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.2.11`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → 退出码 0
- 包内自检：`dist/VideoWorkbench/wb-check.json` → `info.version = 0.2.11`，`ok=true`
- commit：`160a2c0 fix(summary): 轻量总结按输入BV选择合集条目并抬到 0.2.11`
- release：`gh release create v0.9.12 dist/VideoWorkbench-0.2.11.zip --title "VideoWorkbench 0.2.11（打包版）" --notes-file docs/handoff/release-v0.9.12.md`，发布页 <https://github.com/cunnak/bili-scribe/releases/tag/v0.9.12>