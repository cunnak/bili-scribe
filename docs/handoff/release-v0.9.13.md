# v0.9.13 / VideoWorkbench 0.3.0（打包版）

## 这个版本解决什么问题

字幕样式以前只是设置里几个字段：改字号只能看到「大一点小一点」，没有预览、没有分场景覆盖，
改完也不知道成片长什么样。想给某个视频单独设样式，只能连累所有视频。

0.3.0 把它做成一套完整系统：全局默认 + 单视频整份覆盖，快速预览与精确预览并存，
样式一次配置同时驱动 ASS 导出与硬字幕烧录。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `core/styles.py` | schema v1，字号按 720 基准 | schema v2，横屏 1920x1080 / 竖屏 1080x1920 参考画布，25 字段 face，`FONT_FALLBACK` |
| `core/subtitles.py` | 单条 `Style:` 行，`PlayResY` 停在 720 | `ass_style_records()` 唯一映射 owner，多 `Style:` 行，`PlayResX/PlayResY` 写目标画布 |
| `infra/db.py` / `repo.py` | 无项目级样式列 | schema v7 加 `wb_project.subtitle_style_json`；`project_style_get/put/clear`（NULL = 继承） |
| `app/service.py` | 各调用点自己拼优先级 | `effective_style()` 单一入口；`style.effective.get/put/reset` 三处返回同构 |
| `web/js/pages/styles.js` | 简单表单 | 整页重写：400ms 防抖自动保存、请求串行、seq 丢弃旧响应、失败回填全部字段错误 |
| `web/js/components/subtitle_style_form.js` | 无 | 新建：纯表单组件，无 RPC |
| `web/js/components/subtitle_style_preview.js` | 无 | 新建：画布近似 + 真 FFmpeg 精确预览 |

### 顺手修掉的三处真实缺陷

1. **`skip_exported` 失效**：签名里加了 `style_hash`，跳过逻辑没跟上，同样的参数永远匹配不上，
   批量导出每次都重导。收成 `_style_digest()` 单一 owner。
2. **任务快照被覆盖**：导出任务直接吃共用的 `subtitle.orig.ass`，第二次导出会改写它，
   已启动任务「记录说 A、画面是 B」。改为 `subtitle.orig@<hash>.ass` 且**落盘即冻结**。
3. **`%APPDATA%` 未展开污染仓库**：7 处读 `cfg.storage.*` 原始串，把 PNG / ASS / 样式 JSON /
   日志全写进仓库根下一个叫 `%APPDATA%` 的字面目录。全部改为已展开的 property，
   并补常驻判据（扫产品码 + 环境变量重定向验红）。

### 字号基准的语义变化（会改变既有输出）

v1 是「字号按目标高度缩放，而 `PlayResY` 停在 720」，libass 又缩一次，1080 成片比预期大 50%。
v2 直接让 `PlayResX/PlayResY` 等于目标画布，缩放只发生一次。
往返不变量：legacy v1 size 48 @720 → v2 72 @1080 → `to_ass_style(conf, height=720)['size'] == 48`。

## 验证

- 全量回归：`.venv/Scripts/python.exe -m pytest tests/wb -q` → **6 failed, 2013 passed, 1 skipped in 837.73s**
- 前端回归：`node tests/wb/frontend_test.js` → **全部通过（113 项）**
- 版本 owner：`.venv/Scripts/python.exe -m pytest tests/wb/test_version_owner.py -q` → **6 passed**
- 样式专项：`pytest tests/wb/test_style_effective.py tests/wb/test_styles_real_backend_ui.py -q` → **25 passed**
- 静态检查：`.venv/Scripts/ruff.exe check video_workbench tests/wb scripts` → 3 条既有项（`verify_cover_fix.py` 两条有意保留的 E402、`cli.py:307` UP032）
- 回退验证：改回未展开读法 → 精确点名那一行失败；恢复 → 通过。改坏作用域标签 → 项目作用域用例失败。

### 那 6 条失败不是本次引入

用 `git archive HEAD` 取改动前的 web 副本，`WB_WEB_ROOT` 指过去跑同批用例，同样失败
（7 failed，多出的那条是 `test_serves_the_repository_web_dir_by_default`，它本来就该因覆盖而失败）：

- `test_settings_role_warning_ui.py` 5 条：折叠组展开状态
- `test_summary_error_display.py` 1 条：`summary.js` 里有 `detail` 字面量（该文件本次未改动）

## 发布执行记录

- 版本：`video_workbench/__init__.py` → `0.3.0`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → 退出码 0
- 包内自检：`dist/VideoWorkbench/wb-check.json` → `info.version = 0.3.0`，`ok=true`
- commit：`343107e feat(subtitle): 字幕样式系统 v2（作用域 / 快照 / 预设 / 双预览）并抬到 0.3.0`
