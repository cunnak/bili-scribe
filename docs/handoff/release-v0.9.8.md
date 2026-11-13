# v0.9.8 / VideoWorkbench 0.2.7（打包版）

## 这个版本解决什么问题

总结模块此前只能对**已经下载到本地**的项目工作：想总结一条视频，得先把整段视频下下来（几百 MB 到几 GB），或者先跑一遍本机 ASR。用户只想快速看一眼"这条视频讲了什么"的时候，这个前置成本完全不成比例。

0.2.7 给总结模块加了**轻量模式**：输入 av / BV 号（或视频链接），不下载视频，二选一取素材——抓 B 站字幕（AI 字幕或 UP 主上传的人工字幕），或者直接选一份本地 srt 文件。取到素材之后走的是**完全同一条**总结链路（同一个任务、同一套版本、同一份成本账），不是第二套实现。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/web/js/pages/summary.js` | 只有"选一条已下载的项目" | 「单条视频」标签内多一条来源切换：**本地项目 / 轻量**；轻量面板含 AV/BV 输入、解析、字幕轨选择、选 srt 文件、提取并总结 |
| `video_workbench/app/service.py` | 总结只能从 `wb_transcript` 读素材 | 新增 `summary.light.probe`（只解析 + 列字幕轨，不建项目）与 `summary.light.prepare`（取素材、落项目、写三处文件）；`_summary_job` 把素材来源透传进 `run`/`refine` |
| `video_workbench/core/bili_subs.py` | 无此模块 | 列轨、选轨、抓字幕正文、转字幕段；未登录单独报 `AUTH_REQUIRED`，与"这个视频没字幕"分开 |
| `video_workbench/core/summary_source.py` | 无此模块 | 四种素材来源（本机 ASR / B 站 AI 字幕 / B 站人工字幕 / 用户 srt）的单一 owner；`origin` 存进 `transcript.json`，不加数据库列，`SCHEMA_VERSION` 仍是 6 |
| `video_workbench/core/summarizer.py` | `build_bundle` 把素材类型写死成"AI 转录字幕 / 本机 ASR" | `build_bundle`/`run`/`refine`/`prepare` 接 `origin`，提示词里那句"素材类型 + 可信度"跟着真实来源走 |
| `video_workbench/core/bili.py` | `get_data` 不支持 WBI 签名 | 加 `signed` 关键字；**只在需要时**把该 kwarg 传给底层客户端（测试假客户端签名不带它） |
| `video_workbench/core/layout.py` | `allocate` 是唯一入口 | 抽出 `base_root`（不避让、不追加序号），给"按目录去重"用 |
| `video_workbench/infra/repo.py` | 只能按 bvid 查项目 | 新增 `project_find_by_root`，让只给 srt、没有 bvid 的素材也能落到同一行 |

## 这一版修掉的三个真缺陷

1. **`srt.parse_bytes` 会漏出 `UnicodeDecodeError`。** 它声明只抛 `SrtError`，但编码探测在"有 UTF-16 BOM、内容却是残的"输入上直接把 `UnicodeDecodeError` 穿出来。用户选错字幕文件时看到的是整段 traceback，而不是"这个字幕文件读不了"。
2. **轻量总结会白跑一次网络。** 原实现先抓字幕、后判"这条视频是不是已经有本地项目"，于是已经花过 ASR 时间的项目也会被请求一次平台接口。顺序改成先判后抓。
3. **轻量面板的错误提示从来没渲染过。** `state.lightError` 只被赋值、界面上没有节点，空输入点「提取」看起来像按钮坏了。

## 验证

- 轻量总结判据：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary_light.py tests/wb/test_summary_light_ui.py -q` → **35 passed in 4.71s**
- 非浏览器全量：`.venv/Scripts/python.exe -m pytest tests/wb -q --ignore=<10 个浏览器文件>` → **1876 passed, 1 skipped in 105.20s**
- 浏览器文件单独跑：**102 passed, 5 failed in 707.82s**
- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（108 项）**
- 字节锁：`.venv/Scripts/python.exe -m pytest tests/wb/test_summary_drift.py -q` → **14 passed**
- 版本 owner：`.venv/Scripts/python.exe -m pytest tests/wb/test_version_owner.py -q` → **6 passed**
- Ruff：`.venv/Scripts/ruff.exe check` / `ruff.exe format --check`（本次改动的 8 个 Python 文件）→ **All checks passed / 8 files already formatted**
- `bili_scribe/` 零改动：`git status --porcelain -- bili_scribe` → **0 行**

### 那 5 条失败不是本次引入的

全部落在 `tests/wb/test_settings_role_warning_ui.py`，报的是设置页折叠组本该收起却被展开。A/B 判据：用 `git archive HEAD video_workbench/web` 导出上一版 web 目录、`WB_WEB_ROOT` 指过去跑同一批用例 → **同样这 5 条失败**（另加一条 `WB_WEB_ROOT` 自身守卫，属预期）。这是既有缺陷，单独处理。

## 验证到什么程度

- **B 站接口一次都没有真打。** 字幕接口要求 WBI 签名 + 登录态，仓库里没有可用凭据。`bili_subs` 的全部行为由假网关钉住（含 `signed=True` 这个 kwarg 有没有传下去），但"签名参数是否真被上游接受"只有登录状态下点一次才能确认。这是本版最大的未验证面。
- 全量 1984 条没有一条命令跑完过：浏览器那 10 个文件单独跑要 707s，与其余 1876 条拼在一起超出单次执行时限。两批分开跑，覆盖完整但不是一个进程里的读数。
- 打包版验收证明冻结包包含本次构建、版本与模块符号；不替代真实账号与真实接口的行为验收。

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.2.7`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py`
- 冻结包验收：`.venv/Scripts/python.exe scripts/accept_frozen_package.py`
- tag：`v0.9.8`
- release：`gh release create v0.9.8 dist/VideoWorkbench-0.2.7.zip --title "VideoWorkbench 0.2.7（打包版）"`
