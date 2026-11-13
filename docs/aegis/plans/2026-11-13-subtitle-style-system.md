# VideoWorkbench 完整字幕样式系统任务规划书

## 1. 目标与依据

把现有「字幕样式」扩展为与 VideoCaptioner 功能对等的完整系统：统一驱动快速预览、精确预览、ASS 导出和硬字幕烧录，支持全局默认、单视频覆盖、任务快照及预设管理。

需求基线见 docs/decisions-subtitle-style.md。Q23 最终确定：MP4 软字幕继续使用 mov_text、明确不保留样式；完整样式只承诺 ASS、精确预览与硬字幕。

只读调研依据：core/styles.py、core/subtitles.py、app/service.py、config.py、infra/db.py、infra/repo.py、infra/ffmpeg.py、core/exporter.py，以及 styles.js、subtitles.js、settings.js、mock.js 和相关测试。当前工作区未找到独立 VideoCaptioner 源码，功能对照依据用户截图与现有实现对齐说明，不声称参考源码级逐字段核验。

本任务同时改变持久化 schema、RPC、导出契约和可见交互，必须固定唯一 owner、兼容边界和逐层验收，避免预览与导出各自演化。

## 2. 范围与成功标准

### 功能范围

- 四种排布；主副字幕独立样式和垂直间距。
- 全局默认 + 单视频整份覆盖；无覆盖跟随全局，有覆盖独立，可恢复继承。
- 预设选择、新建、重命名、删除、复制、打开目录；保留 JSON 导入导出。预设仅作模板。
- 字段自动保存；无效输入留在编辑态并显示全部错误，不覆盖上一份有效配置。
- 快速近似预览 + 手动精确预览；真实帧/真实字幕与演示图/示例文本并存。
- Windows 系统字体；缺字体使用确定后备并提示实际字体。
- ASS 完整样式；SRT/VTT 纯文本；MP4 软字幕 mov_text 不保留样式；硬字幕完整烧录。
- 导出或烧录启动时固化完整样式快照，后续编辑不影响已启动任务。
- 升级保持现有视觉结果；只验收 Windows。

### 成功标准

1. ASS 与硬字幕使用相同字段、颜色、透明度、布局、参考分辨率和后备字体。
2. 全局/单视频优先级、恢复继承和预设模板语义均通过 RPC 与真实浏览器验证。
3. 已启动任务的 ASS 与样式哈希不被后续编辑覆盖。
4. 每个公开高级字段有 ASS 序列化断言，关键组合有真实 FFmpeg 图片证据。
5. 现有默认样式、旧平铺配置、内置圆角预设和现有 style_id 调用保持兼容。

## 3. 架构与数据契约

### 唯一 owner

- core/styles.py：样式 schema、默认值、旧版归一化、字段校验、预览样本。
- core/subtitles.py：ASS 字段映射、颜色/透明度、分辨率缩放、主副样式记录和 Dialogue 序列化。styles.to_ass_style 保留为兼容委托，不维护第二套转换逻辑。
- app/service.py：有效样式解析、RPC、任务快照、真实渲染、导出编排。
- infra/db.py / infra/repo.py：schema v7 与项目覆盖读写。
- web/js/pages/styles.js：页面状态、作用域、自动保存、预设操作和路由；subtitles.js 只提供项目入口。
- infra/ffmpeg.py：继续作为 libass/FFmpeg 滤镜入口。

### 样式模型

新增 schema_version=2；top-level 包含 layout、vertical_gap、primary、secondary、preview、rounded、base_preset_id。rounded/mode 保留。

primary 与 secondary 均支持：

- 基础：font、size、spacing（小数）、color、opacity、outline_color、outline_opacity、outline_width。
- 字形：bold、italic、underline、strikeout、scale_x、scale_y、angle、blur。
- 位置：alignment、margin_l、margin_r、margin_v。
- 背景与阴影：border_style、background_color、background_opacity、shadow_color、shadow_opacity、shadow_distance。

styles.validate 集中拒绝 NaN/inf、非法颜色、未知枚举和越界，一次返回全部错误。字号 12–120；spacing -20–20、步长 0.1；opacity 0–1；scale 10–1000%；angle -360–360；outline/shadow/blur 0–100；边距与 vertical_gap 0–1000。

### 分辨率与迁移

- v2 以横屏 1920×1080、竖屏 1080×1920 为参考尺寸。
- 字号、描边、阴影、模糊、字距、边距、背景扩边和双字幕间距按目标高度/参考高度缩放；缩放百分比、角度、颜色、透明度不重复缩放。
- 无 schema_version 的旧样式按 720 基准读取，归一为 v2 时相关尺寸乘 1.5；旧 default 在 720 输出仍为 48，在 1080 输出仍为 72。
- 内置 default 只补新字段默认值，不改变既有观感。

### 配置与 schema v7

- subtitle.default_style 保留，继续表示旧版默认预设 id。
- 新增 subtitle.default_style_config：空 dict 沿用 default_style；非空保存完整全局工作配置与 base_preset_id。
- wb_project 新增 subtitle_style_json TEXT；NULL 表示继承，非 NULL 保存完整项目覆盖与 base_preset_id。
- SCHEMA_VERSION 升至 7，登记 _V7_COLUMNS 并追加 _ALL_COLUMN_BUCKETS；沿用备份与幂等补列。
- repo 增加项目样式专用 get/put/clear；clear 写 NULL，不依赖通用 project_update 白名单。

有效样式优先级：显式 style_id > 项目覆盖 > 全局 default_style_config > 全局 default_style > 内置 default。

### RPC 与任务契约

保留 style.list/get/save/delete/import/export/fonts，新增：

- style.effective.get {project_id?}：返回 config、scope、inherited、base_preset_id、warnings、actual_fonts。
- style.effective.put {project_id?, config, base_preset_id?, revision?}：保存当前作用域；revision 防旧覆盖新。
- style.effective.reset {project_id?}：项目清除覆盖；全局恢复内置 default。

style.render 兼容原参数，新增 project_id、time_ms；有项目时抽真实帧并优先使用该时刻真实字幕，无项目或无可用字幕时使用演示内容。

subtitle.export 继续接受 style_id，新增 style_hash 返回字段。视频任务 payload 增加 style_snapshot、style_hash 和实际字体；渲染读取任务启动时生成的独立 ASS 文件。

## 4. 实施任务

### 任务 0：基线落盘

- 写入本决策记录和规划书，记录 git status 与计划修改文件。
- 新 tests/wb 文件落地后立即 git add 并登记 sha256 快照清单。
- 实施前调用 hindsight_capture_initiative；不创建 commit/push。
- 记录测试基线，既有红与新增红分开报告。

### 任务 1：样式模型与兼容归一

修改 core/styles.py、config.py、tests/wb/test_styles.py。

实现 schema v2、默认值、校验、旧平铺/720 配置归一、参考尺寸与预设元数据。保留 default/rounded-default、JSON 导入导出和圆角字段。验证旧 default 输出不变、新字段往返、非法值一次列全、横竖屏缩放正确。

### 任务 2：ASS 序列化与双字幕布局

修改 core/subtitles.py、core/styles.py、tests/wb/test_export.py、tests/wb/test_translator.py。

生成主/副两个 ASS 样式记录，保留 Default 兼容入口；用样式覆盖或等效结构表达主副不同样式。四种 layout 明确选择、排序或省略文本并应用 vertical_gap。映射全部高级字段，处理 BGR/反向 alpha、换行和特殊字符。styles.to_ass_style 改为唯一映射 owner 的委托。

### 任务 3：全局/项目配置与自动保存 RPC

修改 infra/db.py、infra/repo.py、app/service.py、config.py；扩展 tests/wb/test_db.py、test_library_migration_checks.py、test_styles.py，并更新 test_publish_local_video.py 的版本预期。

完成 v7 迁移、项目覆盖读写/清除和有效样式解析，实现 style.effective.* 与 revision 防旧写。缺字体采用大小写不敏感匹配；优先 Microsoft YaHei，其次系统列表首项，最后使用内置确定值。验证迁移、继承、覆盖独立、恢复继承和幂等备份。

### 任务 4：任务快照接入字幕与切片导出

修改 app/service.py、core/exporter.py（仅在消费契约需要时）、tests/wb/test_subtitle_export_rpc.py、test_clip_export.py、test_export.py。

在导出计划阶段只解析一次有效样式，保存完整快照、实际字体与内容哈希。ASS 文件名加入样式哈希，切片名再包含时间窗。硬字幕继续 subtitle_filter；软字幕继续 mov_text 并提示。style_hash 纳入切片 _export_signature，避免内容变化后被 skip_exported 错跳过。验证后续编辑不改变已启动任务 ASS，SRT/VTT 不含样式，显式 style_id 仍优先。

### 任务 5：样式页与两个入口

修改 web/js/pages/styles.js、subtitles.js、settings.js、web/js/mock.js、web/index.html、web/css/styles.css；新增：

- web/js/components/subtitle_style_form.js：字段分组、控件与完整错误展示。
- web/js/components/subtitle_style_preview.js：快速画布、演示/真实帧状态和精确预览结果。

styles.js 保留页面级状态与 RPC owner。设置页进入全局作用域；字幕页携带 project_id 进入项目作用域。持续显示「全局默认 / 项目覆盖 / 正在继承」，项目可恢复继承。

普通字段 400ms 防抖自动保存，请求串行化并用序号/revision 防旧响应；切作用域或离页前 flush，失败保留草稿。选择预设只复制到当前配置；预设覆盖、另存、重命名、删除、复制均显式操作，内置预设不直接修改。打开目录复用 fs.reveal。控件可键盘操作，1100×720 下不横向溢出。

### 任务 6：真实帧精确预览

修改 app/service.py、core/styles.py、subtitle_style_preview.js、styles.js 和 mock.js。

快速预览明确标注近似。精确预览复用有效样式到 ASS 的唯一链路；有 project_id 时按 time_ms 抽源帧并烧录样本字幕，无项目时生成演示背景。处理渲染中、失败重试、缺源视频、无字幕、缺字体和旧响应失效。返回 PNG、ASS、style_hash、尺寸、实际字体与警告；失败不伪报成功。

### 任务 7：文档、回归与真实验收

- 更新 docs/用户手册.md、README-VideoWorkbench.md，说明作用域、模板、排布、各导出格式样式边界和缺字体行为。
- 扩展 tests/wb/frontend_test.js；新增 tests/wb/test_styles_real_backend_ui.py，使用真 WorkbenchService + Chromium + expose_binding，不放宽 CSP。
- 真实浏览器走完：全局编辑→项目覆盖→全局再改→项目不变→恢复继承→预设操作→两种预览→ASS/SRT/硬字幕导出。控制台任何错误算失败。
- 保存全局、项目覆盖、四种排布、高级字段组合、缺字体、渲染失败和 1100×720 截图，并做视觉复核。
- 按 prove-it 汇报命令原文、通过/失败数、截图和未验证项；mock 结果不冒充真渲染。

## 5. 验证命令与证据

以下为计划命令，规划阶段尚未执行，不能预写通过数：

```bash
node tests/wb/frontend_test.js
.venv/Scripts/python.exe -m pytest tests/wb/test_styles.py tests/wb/test_export.py tests/wb/test_translator.py tests/wb/test_subtitle_export_rpc.py tests/wb/test_clip_export.py tests/wb/test_db.py tests/wb/test_library_migration_checks.py -q
.venv/Scripts/python.exe -m pytest tests/wb/test_styles_real_backend_ui.py -q
.venv/Scripts/python.exe -m pytest tests/wb -q
.venv/Scripts/ruff.exe check <本次实际改动的 Python 文件与测试>
.venv/Scripts/ruff.exe format --check --diff <同一范围>
```

全量 pytest 前把 Git for Windows 放到 PATH 最前。真 GUI 使用隔离 HOME/USERPROFILE、保留真实 APPDATA/LOCALAPPDATA，按 #onb-skip 走真实路径，前后记录真实配置文件 sha256。

最终证据链：模型/迁移/RPC 测试、ASS 字段断言、任务快照回归、真实 FFmpeg PNG、硬字幕抽帧、真实浏览器流程与控制台结果、默认及 MIN_SIZE 截图、全量与 ruff 读数。

## 6. 风险与处理

- 高级字段假支持：字段进入 ASS + 真实 FFmpeg 组合渲染双重验收。
- 主副样式串扰：四种布局和不同颜色/字号/透明度分别断言，检查单路缺失与空文本。
- 预览与成片漂移：精确预览和烧录共用 format_ass 与 subtitle_filter；快速预览标注近似。
- 任务输入被覆盖：内容哈希文件名 + payload 快照 + 后续编辑回归。
- 自动保存竞态：串行写入、revision、离页 flush、失败保留草稿。
- 旧默认外观变化：720 到 1080 等效转换，以旧输出数值与真实图片对照。
- 缺字体不可见：载荷返回实际字体和警告，不依赖 libass 静默替换。
- 共享数据库：只改 wb_*，v7 先备份，不触碰 bili_scribe 表。
- 在途工作：六张 library-ia 截图保持原样；不大文件整写，不用工作区快照恢复做牙齿。

## 7. 明确不做

- 不兼容 VideoCaptioner 文件，不打包或导入字体。
- 不做批量套用或一键清除全部覆盖。
- 不承诺 macOS/Linux。
- 不把 MP4 mov_text 描述成保留样式，不新增 MKV 分支。
- 不改 NLE「字幕样式不跟随」边界，不改 CSP，不新增第二套样式真源。
- 不创建 git commit/push，除非用户另行明确要求。

执行方式：按任务 0→7 顺序 inline 推进；共享核心模型和页面状态，不制造多写者。每个任务完成后立即验证，再进入下一项。
