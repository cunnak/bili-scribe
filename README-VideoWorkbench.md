# VideoWorkbench

一个人的视频流水线：**下载 → 转录 → 字幕 → 总结 → 切片 → 合并 → 发布**，全在一个桌面窗口里。

> 仓库根目录的 `README.md` 属于上游 **bili-scribe**（同一仓库、共用登录态与数据库）。
> 本文只讲 VideoWorkbench 这个桌面工作台。

## 它和 bili-scribe 是什么关系

| | bili-scribe | VideoWorkbench |
|---|---|---|
| 形态 | 命令行 + 本地 HTTP 服务 | `pywebview` 桌面窗口（不起 HTTP） |
| 代码位置 | `bili_scribe/` | `video_workbench/` |
| 数据库 | `~/.bili-scribe/bili-scribe.db` | **同一个库**，只写 `wb_*` 表；读它的 task/publish/summary_version 表 |
| B 站登录 | `~/.bili-scribe/key.bin` | **复用**，不需要再登一次 |
| 配置 | `~/.bili-scribe/config.yaml` | **同一个文件**，`video_workbench/config.py` 在它的 dataclass 上继承扩展 |

原则：**不改 `bili_scribe/` 的任何业务代码**。需要的能力（下载、摘要、B 站客户端）按函数
导入复用；新功能一律写在 `video_workbench/`。

## 装

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows；Linux/macOS 用 .venv/bin/activate
pip install -r requirements.txt        # 上游 bili-scribe 的依赖
pip install -r requirements-wb.txt     # 工作台额外依赖（pywebview / faster-whisper / yt-dlp / pyinstaller …）
```

FFmpeg **不随包分发**：首次启动的「运行环境体检」会告诉你去哪儿下，也可以在设置页
「下载 FFmpeg」自动落到 `%APPDATA%\VideoWorkbench\bin`。语音模型同理，用体检里的
「开始下载」或 `transcribe.model.download` 拉（默认 `large-v3-turbo`，约 1.6 GB，
走 `hf-mirror.com`）。

## 起

```bash
python -m video_workbench.desktop                 # 正常启动
python -m video_workbench.desktop --check         # 只体检，打一行 JSON 就退出（给脚本用）
python -m video_workbench.desktop --config D:\x\config.yaml   # 指定配置文件
```

窗口 1360×860（最小 1100×720）。左栏五项：新建任务 / 素材库 / 总结 / 切片台 / 合并 / 发布，
底部「设置」；左栏可折叠成 56px 图标条，状态记在 `ui.rail_collapsed`。

## 首次启动会体检五件事

| 项 | 缺了会怎样 |
|---|---|
| WebView2 | 窗口起不来（Win11 自带；老系统需装） |
| FFmpeg | 不能下载 / 转录 / 导出 / 合并 |
| 语音模型 | 不能转录，其余照常 |
| B 站登录 | 不能下需要登录的画质，本地视频照常 |
| 大模型 | 总结、字幕优化、翻译、文案、画面精析全退成规则兜底 |

体检可以「先这样，进去试试」跳过，之后再从设置页「关于 → 运行环境体检」打开。
跳过这件事记在 `ui.onboarded`，下次不再弹。

## 配置（`~/.bili-scribe/config.yaml`，或 `config.wb.example.yaml` 作模板）

| 段 | 关键项 |
|---|---|
| `app` | `language`（zh-CN）、`media_server`（auto/file/http） |
| `ui` | `skin`（`ark` 暗 / `exhibition` 亮）、`rail_collapsed`、`onboarded` |
| `server` | 仅 `run.py` 调试用，桌面主路径不起 HTTP |
| `llm` | `providers[]`（`id`/`name`/`protocol`/`base_url`/`api_key`/`models[]`）、`active_provider`、`base_url`/`api_key`/`model`（**当前服务商的镜像，别手改**）、`proxy`、`temperature`、`max_tokens`、`thinking`、`timeout_seconds`、`tasks.{summary,collection_overview,translate,clip_analyze,copy,refine,vision}`、`max_input_chars`、`chunk_size`、`chunk_overlap` |
| `storage` | `db_path`、`media_root`、`cache_root`、`models_root`、`ffmpeg_dir`、`retention.{source,audio,frames,subtitle,export}` |
| `collect` | 采集开关（沿用上游） |
| `download` | `quality`（4k/1080p/720p/480p）、`codec`（h264/hevc/av1）、`concurrency`（1–8）、`cookies_from`、`dir_template`、`backend`（`ytdlp`/`bbdown`）、`bbdown_path`、`bbdown_args` |
| `transcribe` | `engine`（faster-whisper）、`model`、`device`（auto/cuda/cpu）、`language`、`vad`、`word_timestamps` |
| `subtitle` | `default_style`、`optimize`、`translate.{target,bilingual,glossary}` |
| `clip` | `highlight_min/max`、`segment_min/max`、`candidate_count`、`snap_tolerance_ms`、`vertical_mode`、`follow_enhance`、`bgm_volume`、`bgm_duck`、`fade_ms` |
| `publish` | `require_confirm`、`min_interval_seconds`、`max_summary_chars`、`max_comment_chars`、`disclose_ai`、`auto_comment` |

`llm.tasks.<任务>` 里既能写 `服务商/模型`（`kimi/moonshot-v1-8k`），也能只写模型名
（`deepseek-reasoner`，谁的 `models` 里有它就用谁）；解析只有 `config.resolve_task_model` 一个入口。
模型 id 自带斜杠时（如 `qwen/qwen3-vl-plus`）不会被拆开——前缀不是已知服务商就整串当模型名。

`tasks.vision` 留空会自动回落到 `llm.model`——**只有真能收图的多模态模型**才配这一项，
否则画面精析会自动跳过（跳过写进任务备注，不会假装跑过）。

`download.backend` 默认 `ytdlp`。`bbdown` 只是**手动兜底**：yt-dlp 失败后，在新建任务页
「最近下载」里点「用 BBDown 重试」才走它，不会自动切。`bbdown_path` 留空则按
`tools/BBDown.exe` → `PATH` → 当前目录找，找不到就不显示那个按钮。

## 跑一单（最短路径）

1. **新建任务**：贴 BV / av / ep / ss / 合集链接，或「选本地文件」导入；
2. **素材库**：选一条 →「转录」（首次要下模型）→「字幕」进去改文本、跑质检；
3. **总结**：生成，挑一版「按这个改」微调；
4. **切片台**：生成候选 → 改切点（2 秒磁吸、±1 帧）→ 预览 → 导出（可 9:16 竖屏、可烧字幕、
   可配 BGM 与人声闪避）→ 「交付」成剪映草稿 / 达芬奇 OTIO / 降级包；
5. **合并**：把几条成片拼成一条（预检规格不一致会自动转精确重编码）；
6. **发布**：从总结生成草稿 → 三关检查（内容 / 账号 / 节奏）→ 勾确认 → 发笔记；
   **只有发布不可撤销**，其余每一步都能改。

详细操作见 [`docs/用户手册.md`](docs/用户手册.md)。

## 命令行（不想开窗口 / 要脚本化）

```bash
python -m video_workbench.cli info                       # 版本、路径、依赖、体检
python -m video_workbench.cli jobs                       # 最近任务
python -m video_workbench.cli run --url BV1xx411c7mD --clips 2 --export 1 --mode fast
python -m video_workbench.cli run --project-id pj_abc --clips 2 --export 1 --mode fast
python -m video_workbench.cli download --url BV1xx411c7mD --quality 720P --backend bbdown
python -m video_workbench.cli transcribe pj_abc
python -m video_workbench.cli summarize pj_abc --role hardcore
python -m video_workbench.cli clip pj_abc --count 3 --export 1 --mode fast
python -m video_workbench.cli merge a.mp4 b.mp4 -o 合并成片.mp4
```

`--json` 让 stdout 只有一行 JSON（进度与日志走 stderr），写在子命令前后都行（`cli --json info`
与 `cli info --json` 等价），`--timeout` 到点只是不再等。
退出码：`0` 成功 / `1` 任务失败（可重试）/ `2` 参数或环境不对（重试没用）/ `3` 取消 / `130` 中断。

## 质量门（四条命令 + 一条矩阵）

```bash
./.venv/Scripts/python.exe -m pytest tests/wb -q                 # 后端
node tests/wb/frontend_test.js                                   # 前端（jsdom，无需打包）
./.venv/Scripts/python.exe scripts/check_colors.py               # 皮肤：只有 tokens.css 允许写颜色
./.venv/Scripts/python.exe -m video_workbench.desktop --check    # 启动体检（一行 JSON）
./.venv/Scripts/python.exe scripts/accept_p5_matrix.py           # 全量验收矩阵
```

每条验收脚本都用自己的隔离库（`build/accept/isolated/<script>/wb.db`），互不污染。

## 打包

```bash
pyinstaller video_workbench.spec --noconfirm
# → dist/VideoWorkbench/VideoWorkbench.exe（one-folder）
```

**不夹带** FFmpeg、字体、语音模型：那是几十 MB 到 2 GB 的外部资源，装到用户目录更合适。
`video_workbench.spec` 里 `_WANT` 显式列了 `yt_dlp`（动态导入，PyInstaller 扫不到）。

## 已知问题

| 现象 | 原因 / 办法 |
|---|---|
| 窗口白屏 | WebView2 没装；体检里会标出来 |
| 转录报 `cublas64_12.dll is not found` | cuBLAS 缺；程序自动退回 CPU/int8，日志里有 `notes: gpu_fallback` |
| 唱歌/强背景音乐段落转录为空 | VAD 把音乐判成非语音；自动关 VAD 重跑一次，`notes: vad_fallback` |
| 人脸检测一直「按居中裁切」 | OpenCV 5 没有 Haar 了，改用 YuNet；下载要走代理（`llm.proxy`） |
| 草稿在剪映里字幕样式不是自己设的 | 设计如此：**字幕样式不跟随草稿**，要样式就导出硬字幕 |
| 发布提示「这个视频已经发过笔记了」 | 60 秒节奏按**账号**算，换视频不算换账号 |
| 数据库被占用 | bili-scribe 与工作台共用 SQLite；关掉另一个再来 |
| 大模型只说「内部错误」 | 多半是鉴权 / 限流；设置页「测试连接」会给出具体错误码 |

## 边界（有意不做）

- 不做剪映 / 达芬奇的完整工程复刻，草稿只保证「能打开、时间轴对」；
- 不自动发布：AI 产物一律是草稿，没有「关掉确认直接发」的开关；
- 不托管模型与 FFmpeg，不分发字体；
- 前端没有 Node 构建链：原生 ES Modules + CSS 变量，第三方库放 `web/vendor/`。

## 目录

```text
video_workbench/
  desktop.py            # pywebview 外壳：窗口、js_api、--check
  config.py             # 继承 bili_scribe 配置，扩展 download/transcribe/subtitle/clip/publish
  app/                  # service.py（RPC 总入口）、rpc.py、jobs.py、events.py、errors.py
  core/                 # resolver/downloader/transcriber/subtitles/summarizer/clipper/
                        # exporter/reframe/merger/publisher/styles/copywriter/cover/
                        # audio_mix/nle/overview/recovery/subtitle_llm/translator
  infra/                # db/schema、repo、ffmpeg、llm、models、log、files、cookies
  workers/              # 子进程 worker：asr_worker、download_worker、batch_worker
  resources/prompts/    # 提示词（总结、优化、翻译、切片、精析、选主体…）
  resources/export_templates/jianying/   # 剪映草稿骨架（从真草稿剥出来的）
  web/                  # index.html、css/（tokens + 分页样式）、js/（router/pages/components）
tests/wb/               # pytest 与前端 jsdom 测试
scripts/                # 验收脚本、截图、矩阵、色值检查
docs/                   # 用户手册、验收记录、截图
```
