# v0.9.9 / VideoWorkbench 0.2.8（打包版）

## 这个版本解决什么问题

0.2.7 的轻量总结已经能工作，但界面还存在三处会让用户误以为功能坏了的问题：全屏时 AV/BV 输入框会冲出字段容器；本地项目与轻量总结仍共用一条界面的状态和部分渲染路径；轻量页没有自己的角色、模型选择栏。0.2.8 把这三处收掉。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/web/js/pages/summary.js` | 轻量是 `state.source` 的取值，与本地项目共用 `state`、正文和侧栏 | `createSummaryPage(kind)` 创建 `WB.pages.summary` / `WB.pages.summaryLight` 两个独立实例，`#/summary` 与 `#/light` 两条路由 |
| `video_workbench/web/js/pages/summary.js` | 轻量页没有角色/模型控件 | 新增 `#sum-light-role`、`#sum-light-model`，`summary.start` 显式携带本页选择 |
| `video_workbench/web/css/summary.css`、`app.css` | AV/BV 输入框固定 `min-width:220px`，长标题/全屏会挤出白框 | 输入框改为可压缩，标题/UP/错误/文件名移入独占的 `#sum-light-meta`；四档视口无溢出 |
| `video_workbench/web/js/pages/publish.js` | 发布卡默认挑第一个本地项目，切换页面可能串项目 | `publish.mount({ projectId })` 记录 `filterProjectId`，草稿列表按页面项目过滤，离开时清理 |

## 验证

- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（112 项）**
- 轻量/路由/发布组合：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary_light_ui.py tests/wb/test_web_router_lifecycle.py tests/wb/test_summary_light.py tests/wb/test_publish_local_video_ui.py tests/wb/test_publish_gates.py -q` → **71 passed in 13.14s**
- 总结错误提示条：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary_error_display.py -q -k 'not untouched_copy and not burn_when'` → **26 passed, 6 deselected in 46.32s**
- 版本 owner：`.venv/Scripts/python.exe -m pytest tests/wb/test_version_owner.py -q` → **6 passed in 0.62s**
- 色彩闸：`python scripts/check_colors.py` → **OK**
- 四档浏览器截图：1100/1525/2306/3840，`inputRight <= fieldRight`、`scrollWidth == clientWidth`、`errors=[]`

### 既有失败

全量宽口径回归中的 `tests/wb/test_settings_role_warning_ui.py` 折叠组 5 条失败在 v0.9.8 记录中已确认为既有缺陷，本版未改 settings 相关文件；单独运行仍为 `5 failed, 13 passed`。

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.2.8`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → 退出码 0，约 100s
- 包内自检：`dist/VideoWorkbench/wb-check.json` → `info.version = 0.2.8`，`ok=true`；包目录 413.5 MB
- 冻结包验收：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**
- 发布资产：`dist/VideoWorkbench-0.2.8.zip`，**1959 个文件 / 1959 个条目 / 177,908,306 B**，SHA256 `4F7A72BA0BAB9133757A84EDE405C44727F17E64A938B8C83C1BE3552AEFBD8D`，`ZipFile.testzip() = None`
- 包内核验：`_internal/video_workbench/web/js/pages/summary.js` 含 `createSummaryPage("light")` / `sum-light-role` / `sum-light-model` / `sum-light-meta`；`publish.js` 含 `filterProjectId`
- commit：`69b71a8 fix(summary): 总结页/轻量总结三处修复并打包 0.2.8`
- tag：`v0.9.9`（`git cat-file -t v0.9.9` = `tag`，`v0.9.9^{commit}` = `69b71a8`；tag 对象 `588396c`）
- push：`git push origin main` → `6e0cc6e..69b71a8`；`git push origin v0.9.9` → `* [new tag] v0.9.9`
- release：`gh release create v0.9.9 dist/VideoWorkbench-0.2.8.zip --title "VideoWorkbench 0.2.8（打包版）" --notes-file <notes>` → rc=0，<https://github.com/cunnak/bili-scribe/releases/tag/v0.9.9>
- 资产回读：`gh release view v0.9.9 --json assets` → `VideoWorkbench-0.2.8.zip`、`state=uploaded`、`size=177908306`、`digest=sha256:4f7a72ba0bab9133757a84ede405c44727f17e64a938b8c83c1be3552aefbd8d`
