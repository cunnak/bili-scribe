# P1 素材入库 · 验收证据

> 这一页只回答一个问题：**P1 的每条验收，我用什么证据说它过了。**
> 证据分三类：自动化断言（pytest / npm）、真站实跑、界面截图。
> 三类的分工是刻意的——pytest 守"协议没被改坏"，真站守"接口今天还长这样"，
> 截图守"人看到的东西对不对"。缺任何一类，另外两类都替不了它。

## 一、自动化断言

| 命令 | 结果 | 覆盖 |
|---|---|---|
| `python -m pytest tests/wb -q` | **198 passed / 1 skipped** | 解析、下载编排、目录模板、侧车、导入、缩略图、磁盘守卫、批量队列、RPC 契约 |
| `node tests/frontend_test.js` | **225 passed / 0 failed** | 三栏结构、阶段筛选、详情资产、接口契约与后端路由对账、两套皮肤对比度 |
| `python scripts/check_colors.py` | OK | 前端没有硬编码颜色（tokens.css 除外） |

跳过的那 1 条是 `ffmpeg 探测`：本机没有独立 `ffprobe` 可执行文件时自动 skip，不是失败。

## 二、真站实跑（不看假数据）

脚本：`scripts/accept_p1_download.py`（把库、媒体、密钥全圈在 `build/accept/`，
跑完删目录即可，不碰正式素材库）。

```bash
python scripts/accept_p1_download.py --url BV1GJ411x7h7 --quality 720P
```

它跑的是完整链路：**解析 → 建批次 → yt-dlp 子进程 → 目录落盘 → 侧车 → ffprobe → 抽帧 → 写库**。

实测输出（2026-10-29 23:0x）：

```
[resolve] kind=single title='【官方 MV】Never Gonna Give You Up - Rick Astley' items=1
[start]   {"job": {"id": "job_aeed57e64e93", "type": "download.batch", "status": "queued"}}
[job]     running 0.0  排队 0/1
[job]     running 0.05 下载中 0/1
[job]     succeeded 1.0 完成 1/1
[project] pj_98ac278669cf stage=downloaded missing=[]
  [OK ] source  ...\media\索尼音乐中国\【官方 MV】Never Gonna Give You Up - Rick Astley\source.mp4
  [OK ] thumb   ...\media\索尼音乐中国\【官方 MV】…\frames\thumb.jpg
[result] PASS
```

落盘目录同时验证了 P1-03 的模板：`{media_root}/{UP}/{标题}/`，单稿不建分P层，
`source.wb.json` 与 `source.mp4` 同级。

## 三、界面截图

两套皮肤 × 五个页面，由 `scripts/shoot_wb.py` 产出（headless Chrome，走 `?mock=1`，
路由用 `router.go` 而不是改 hash——理由见脚本注释）：

| 页面 | ark（深色） | exhibition（浅色） |
|---|---|---|
| 素材库（三栏 + 阶段树 + 缺失标红） | `ark-library.png` | `exhibition-library.png` |
| 素材库详情（元信息 + 资产表） | `ark-library-detail.png` | `exhibition-library-detail.png` |
| 新建任务（空态） | `ark-newtask.png` | `exhibition-newtask.png` |
| 新建任务（解析结果 + 逐集勾选） | `ark-newtask-parse.png` | `exhibition-newtask-parse.png` |
| 设置 | `ark-settings.png` | `exhibition-settings.png` |

```bash
python scripts/shoot_wb.py --skin ark        --out docs/acceptance/p1
python scripts/shoot_wb.py --skin exhibition --out docs/acceptance/p1
```

## 四、逐条对账

| 任务 | 目标 | 证据 | 状态 |
|---|---|---|---|
| P1-01 解析 | BV/av/ep/ss/md、单稿/分P/合集 | `test_resolver.py` 29 条；真站 BV1GJ411x7h7 解析出 cid / 时长 / 封面 | ✅ |
| P1-02 下载 | yt-dlp 子进程、进度、取消杀进程树 | `test_download_worker.py` 14 条 + 真站 720P 下载成功（源文件 49.6 MB，`missing=[]`） | ✅ |
| P1-03 目录与侧车 | 模板落盘、清洗、重名、`source.wb.json` | `test_paths.py`；真站目录树见上 | ✅ |
| P1-04 素材库 | 三栏、阶段 chips、搜索筛选、缺失标红 | 截图 ×2 皮肤；`frontend_test.js` 三栏与筛选断言 | ✅ |
| P1-05 本地导入 | 多选、ffprobe、同名 SRT | `test_importer.py`；`fs.pick` + `import.local` 已接界面 | ✅ |
| P1-06 缩略图与磁盘守卫 | 列表有缩略图；不足时 `DISK_FULL` | `test_disk_guard.py`；真站已产出 `frames/thumb.jpg` 并记为 thumb 资产 | ✅ |
| P1-07 队列/取消/重试/批量 | 父批次 + 子项、并发 2、失败隔离、重试失败项 | `test_batch_worker.py`、`test_jobs.py`；`download.retry_failed` 已接 | ✅ |
| P1-08 BBDown 兜底（可选） | 检测到才提示，绝不静默切换 | 未做——见下 | ⏸ |

### P1-08 为什么先放着

方案书把它标成"可选"，且它的价值取决于一个本机事实：**这台机器上没装 BBDown**。
先做出一个"检测不到就永远不显示"的按钮，等于写了一段没人会走到的分支；
等真遇到 yt-dlp 下不动的稿件（通常是章节/互动视频）再补，那时候才有可验证的失败样本。
这是有意的推迟，不是漏掉。
