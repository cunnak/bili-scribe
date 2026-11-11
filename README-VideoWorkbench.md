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
| 配置 | `~/.bili-scribe/config.yaml`（**只读**，WB 不再写它） | 自己的家 `~/.video-workbench/`：`config.yaml` 只写 bili-scribe 认识的六段、扩展设置全在同目录 `workbench.json`；首次启动若旧家还在，会被**逐字段原样**搬运一次到新家 |

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

窗口 1360×860（最小 1100×720）。左栏六项：新建任务 / 素材库 / 字幕 / 总结 / 切片台 / 合并 / 发布，
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

## 配置（`~/.video-workbench/config.yaml` + `workbench.json`，或 `config.wb.example.yaml` 作模板）

> 2026-11-04 起 WB 的配置**从 bili-scribe 的家里搬出去了**：以前两边共用 `~/.bili-scribe/config.yaml`，WB 往里面写 `role` / `max_tokens` 会连带改掉 bili-scribe 的行为。现在 WB 只读那一份（首次启动搬运一次），写只写自己的 `~/.video-workbench/`。**数据文件没搬**：库与 B 站凭据仍在 `~/.bili-scribe/`，那是刻意复用（已有项目与总结历史都在里面）。

> **模板只认 `config.wb.example.yaml`。** 仓库根的 `config.example.yaml` 是 **bili-scribe 那份**（文件头逐字「# bili-scribe 配置示例」，`README.md:67` 那句 `copy config.example.yaml config.yaml` 是 bili-scribe 的装法），拿它当 VideoWorkbench 的模板会抄到一份已过期的 `llm` 段注释：里面写着「思考模型那段推理**不吃 `max_tokens`** ……实测设 4096 却跑了 5125 tokens」，而实测结果是——`finish_reason=length` 时**正文为空**、`reasoning_tokens` 正好等于 `max_tokens`，也就是**吃的**。WB 自己渲染配置那一段已经按实测改过（`video_workbench/config.py`，commit `695cd83`），那句错指引留在 bili-scribe 的模板文件里没被改，是因为它归 bili-scribe、本轮明确不许越界动 `bili_scribe/**`（用户 m06291）。**要修得在 bili-scribe 仓库自己那侧做**；在 WB 这边，绕开它就行。

两个文件分工明确：

- `config.yaml`：只放 bili-scribe 也认的六段（`server` / `llm` / `collect` / `publish` / `storage` / `ui`），
  形状由 bili-scribe 自己的模板渲染。搬新家之后往这里加键**不再会打挂对方**（它不读这份了），但仍然守这条：一份配置只留一个地方负责，而且这份文件保持可被 bili-scribe 读。
- `workbench.json`：Workbench 的 11 段全量配置（服务商列表、任务模型、目录、切片参数、保留策略……）。

读取顺序：`config.yaml` 与 `workbench.json` 合并 → 共享键以 yaml 为准 → 环境变量 → 默认值。
新家不存在而旧家 (`~/.bili-scribe/config.yaml` / `workbench.json`) 还在 → 首次 `load()` **只读**搬一次过去：逐字段忠实（旧家那份就算写着已停用的 `standard`、4096 也照搬，修值是另一件事），`source_path` 指向新家并立刻落盘；旧家此后一个字节都不再被写（连 `.bak` 都不产生），本次迁移记在 `cfg.migrated_from` 上（一次性，第二次 load 为空）。
老版本把扩展键写在 yaml 里的，首次 `load()` 会自动迁移（值搬到 sidecar，yaml 重写成合法形状）。

### 搬家之后 bili-scribe 打不开？那是旧家留下的键

搬家只把配置**复制**到新家，旧家 `~/.bili-scribe/config.yaml` 一个字节都不改（那是 bili-scribe 的文件，
WB 没有权限制去动它）。但老版本的 WB 曾经往那份文件里写过 bili-scribe 不认识的东西——
`llm.tasks`、`ui.onboarded`、整段 `download:` 等等。bili-scribe 的加载器对未知键是**报错**而不是忽略，
所以这些键会让它直接起不来。搬家之前 WB 会顺手把它们重写干净，搬家之后不会了：**代价留给了旧家**。

首次搬运时如果现读到这些键，WB 会：

1. 把事实记进 sidecar 的 `ui.migration_notice`（新家 `config.yaml` 里查不到这个字段——它是扩展键）：
   只有**键名**清单与两个路径，**绝不带值**（值里可能有 `api_key`）；
2. 日志里同一件事留一行（同样只报键名）；
3. 第一次打开界面弹一次窗，说清三件事：配置搬到新家了、旧家那份我没动、旧家里这几个键会让
   bili-scribe 报「未知配置项」而起不来——**要清得你自己在 bili-scribe 那边删，WB 不会替你删**。
   点「知道了」/ 按 ESC / 点遮罩都算看过一次，之后不再提示（标记存在 `ui.migration_notice.dismissed`，
   熬得过后续任何一次保存）。

自家机器如果早于这个功能搬完（新家已经有文件），那条提示不会凭空出现——下次读到旧家仍有未知键时
才会记下来。想直接确认，看 `~/.video-workbench/workbench.json` 里的 `ui.migration_notice.keys`，
照着那份名单删 `~/.bili-scribe/config.yaml` 里对应的行即可。

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
| `publish` | `require_confirm`、`min_interval_seconds`、`max_comment_chars`、`disclose_ai`、`auto_comment`（`max_summary_chars` 已随摘要字段下线，只剩兼容位） |

`llm.tasks.<任务>` 里既能写 `服务商/模型`（`kimi/moonshot-v1-8k`），也能只写模型名
（`deepseek-reasoner`，谁的 `models` 里有它就用谁）；解析只有 `config.resolve_task_model` 一个入口。
模型 id 自带斜杠时（如 `qwen/qwen3-vl-plus`）不会被拆开——前缀不是已知服务商就整串当模型名。

RPC 里跟模型相关的有三个：`llm.test`（发一条最小对话验证这家能不能用）、
模型输出不是严格 JSON 时（例如 `summary_md` 里带裸换行），`video_workbench/infra/jsonfix.py`
会先 `repair_json()` 修（`strict=False` + 去尾逗号 + 剥围栏），修不了就**把上次输出回灌重问一次**；
两次都不行才抛 `LLM_BAD_RESPONSE`，并在 detail 里提示是「截断」还是「格式」。
这条兜底同时挂在候选解析（`core/clipper.py`）与精析解析（`core/clip_refine.py`）上。
`llm.models`（`GET {base_url}/models` 列这家账号能用的模型，供界面「获取可用模型」用；
不需要 `model` 已配好，缺地址/密钥回 `LLM_NOT_CONFIGURED`，地址不支持该接口回 `LLM_BAD_RESPONSE`），
以及 `summary.models`（总结页下拉的数据源：把所有服务商卡展开成 `{spec, provider, provider_name, model}`，
外加 `default`＝`resolve_task_model(llm, "summary")` 的结果）。

`summary.start` / `summary.refine` / `summary.collection.overview` 都收一个可选 `model`：
空串＝跟随设置；`服务商id/模型名` 会走 `parse_model_spec`（斜杠分隔），指名的服务商不存在回 `VALIDATION`；
只写模型名则换模型不换服务商。`summary.refine` 不传时**沿用这一版的模型**，不悄悄换。

`llm.user_agent` 留空用**内置浏览器 UA**（`Mozilla/5.0 … Chrome/141 … Safari/537.36`）。
之所以这么设：Cloudflare 的 1010 规则按客户端指纹判机器人。同一把 key、同一个地址实测——
UA 写 `python-urllib/3.x` 或写 `VideoWorkbench/0.1 …` 一律 403（`blocked access based on your browser signature`），
换成浏览器 UA 就是 200。**把自己程序的名字当 UA 发出去等于自报脚本**，所以默认值不能是程序名。
仍被拦时只剩两条：`llm.proxy` 走代理出口，或换一个更新的浏览器 UA。改完重启生效。
403 与 401 已经分开：401 是 `LLM_AUTH`（key 不对），403 是 `LLM_FORBIDDEN`（网关/权限拒绝）。

`llm.extra_headers`（`dict[str, str]`）给需要私有请求头的网关用：Azure 的 `api-version`、
自建网关的 `x-tenant` 之类，界面上是「上下文与长素材 → 额外请求头」，一行一个 `名字: 值`。
优先级：`extra_headers` > 自动补的会话头 > 内置 UA（所以填 `User-Agent` 也能覆盖 UA）。
`x-opencode-session` 不用手填——host 含 `opencode.ai` 时程序自动补一个进程内稳定的
`wb-<uuid4 前 16 位>`（这一家缺它直接回 400 `MissingSessionID`，不补就根本用不了）。
头名与值都不许含 CR/LF（`validate()` 会拒），防头注入。

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

### 本机网关（magpie）

设置页「模型」分区底部有「接入本机 magpie」：一键建卡并拉模型清单
（`base_url=http://127.0.0.1:3425/v1`、`api_key=magpie`、`id=magpie`）。
环回地址的服务商 id 统一是 `local`（不是 sha1），所以 `llm.tasks.*` 里可以写 `local/auto/hy4-preview`。

模型 id 自带斜杠是允许的（`auto/hy4-preview`、`Qwen/Qwen3-VL-Plus`）：
`parse_model_spec()` 只在斜杠前缀**命中已知服务商 id** 时才把它当服务商前缀。

推理型模型在小 `max_tokens` 下只产出 `reasoning_content`、`content=null`。
这种响应会造成 `LLM_EMPTY`（「模型没有返回正文」/「只输出了思考过程」），
不会被当成成功结果写入 `wb_summary` / `wb_subtitle`。`llm.test` 的探测预算固定 512 token。
