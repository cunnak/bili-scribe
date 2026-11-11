# P2 文本阶段 · 验收证据

日期：2026-10-29
范围：P2-01（ASR worker 与模型管理）、P2-02（转录数据与进度）、P2-03（字幕编辑器）、
P2-04（字幕 LLM 优化，见第五节）、P2-05（翻译与术语表，见第六节）、
P2-06（字幕导出，见第七节）、P2-07（总结适配器，见第八节）
不包含：P2-08 起（合集总览、发布草稿）

## 一、结论

三条任务全部达标，缺陷在验收过程中修掉 4 个（见第三节）。P2-01/P2-02 用**真模型**跑过真音频；
P2-03 的编辑器除了人工看截图，还有「1000 段渲染」的可量化基线。

| 门 | 命令 | 结果 |
| --- | --- | --- |
| 单元/装配测试 | `./.venv/Scripts/python.exe -m pytest tests/wb -q` | 269 passed, 1 skipped（skip=本机无 ffprobe 那条探测用例，验收机有 ffmpeg 手跑过） |
| 前端契约测试（全站） | `node tests/frontend_test.js` | 225 通过 / 0 失败 |
| 前端契约测试（全站，含总结页） | `node tests/wb/frontend_test.js` | 30 项全过（虚拟滚动、撤销重做、质检徽标、波形降级、翻译、导出、总结版本） |
| 颜色红线 | `./.venv/Scripts/python.exe scripts/check_colors.py` | OK（除 tokens.css 外无硬编码颜色） |
| 真模型转录 | `python scripts/accept_p2_transcribe.py --model tiny --source build/accept/clips/short_60s.mp4` | PASS（16 段、SRT 64 行、二次跑缓存命中 0.2s） |
| 编辑器真机验收 | `python scripts/accept_p2_editor.py --config build/accept/p2.yaml` | PASS（15 项全绿，含 split 后落盘与还原） |
| 渲染基线 | `python scripts/bench_p2_editor.py` | 1000 段：首屏 0.5ms / 单帧均值 6.5ms / 最差 21.8ms / DOM 26 行（阈值 400ms 与 16.7ms，PASS） |
| 总结适配器验收 | `python scripts/accept_p2_summary.py --config build/accept/p2.yaml` | PASS（32 项，见第八节） |

真机转录用的是 `tiny`（快、离线可下）；`large-v3-turbo` 也跑过一版：`--model large-v3-turbo` 首次下载 1.6GB（进度事件可见），转录 60s 音频约 74s（CPU int8），
段数与 tiny 一致。说明模型切换链路是通的，不是只对 tiny 特调。

## 二、逐条对账

### P2-01 ASR worker 与模型管理

| 验收项 | 证据 | 结论 |
| --- | --- | --- |
| 60s 测试片转录成功 | `accept_p2_transcribe.py` 输出 `[result] PASS`；`transcript.srt` 首行 `00:00:00,860 --> 00:00:05,000` 起 64 行 | ✅ |
| 子进程可取消 | `pytest tests/wb/test_transcriber.py::test_runner_kills_child_without_waiting_for_next_line`：子进程吐一行后静默，取消在 0.3s 轮询内变成 `kill_tree`，返回 -2；Windows 走 `taskkill /T /F` | ✅ |
| 模型状态正确 | `test_models.py`：未下载→`missing`、下载中→`downloading`、就绪→`ready`、删除后回 `missing`；`models.list` 带 `size_bytes`/`cached` | ✅ |
| GPU/CPU 自动 | `test_transcriber.py::test_pick_device_*`；本机无 CUDA 时落到 `cpu/int8` 并在 `notes` 里写 `device=cpu` | ✅ |

模型清单收在 `video_workbench/infra/models.py`，6 个尺寸 + `large-v3-turbo` 默认；下载走
`huggingface_hub.snapshot_download`，进度按字节回调到 job。

### P2-02 转录数据与进度

| 验收项 | 证据 | 结论 |
| --- | --- | --- |
| JSON 字段完整 | `test_transcriber.py::test_handler_writes_transcript_and_advances_stage` 断言 `language/segments/words/cache_key/model` | ✅ |
| SRT 可播放 | 真机产物 `build/accept/media/本地导入/short_60s/transcript.srt`，行数、时间轴连续（见脚本 `[check] SRT`） | ✅ |
| 重复转录命中缓存 | `[cache] 第二次 0.2s cached=True`；worker 侧 `test_second_run_hits_cache_and_skips_worker` 断言不再起子进程 | ✅ |
| 项目阶段更新 | `stage: new → transcribed`，并且 `library.list` 的 `has_transcript` 为真 | ✅ |

词级时间戳在 `<segment>.words` 里，`at_ms` 切分靠它吸附（见第四节第 4 条）。

### P2-03 字幕编辑器

| 验收项 | 证据 | 结论 |
| --- | --- | --- |
| 1000 条不卡 | `bench_p2_editor.py`：只渲染可视区（实测 DOM 26 行），首屏 0.5ms、单帧均值 6.5ms | ✅ |
| 编辑后保存 | `accept_p2_editor.py`：`subtitle.op(update)` → 回读文本已改 → `transcript.json` 与 `transcript.srt` 同时更新 | ✅ |
| 撤销/重做正确 | `tests/wb/frontend_test.js`（22 项）含「改文本→撤销回原文→重做再改」；栈上限 100 步 | ✅ |
| 重叠/负时长标黄 | `test_subtitle_qc.py` 8 个用例（空文本、负时长、过短/过长、超长字符、重叠、大间隔、乱序归一）+ 编辑器行内徽标 | ✅ |
| 分割/合并 | `accept_p2_editor.py`：split 段数 16→17、两段 860-3320 / 3320-5000 首尾相接；merge 回到 16 | ✅ |
| 波形与时间轴 | `audio.peaks` 1200 桶、`duration=60000ms`；画布点击定位、`at_ms` 与播放头对齐 | ✅ |

截图（两套皮肤各 7 张，`?mock=1`）：

- `ark/ark-subtitles.png`、`ark/ark-subtitles-qc.png`、`exhibition/exhibition-subtitles.png`、`exhibition/exhibition-subtitles-qc.png`
- 素材库/新建任务同批重拍，确认 P1 页面被 P2 改动波及后仍然正确

## 三、验收时修掉的真实缺陷

1. **字幕没登记成资产**：转录只写 `wb_transcript` + 文件，没写 `wb_asset(kind=subtitle)`。
   后果是素材库的「有字幕」筛选永远筛不到、详情页资产表缺一行、`has_transcript` 与 `kinds` 打架。
   现在 worker 在写完 SRT 后 `asset_upsert(kind="subtitle")`，并把 `transcript.asset_id` 指回去。
2. **`audio.peaks` 直接 500**：`service.py` 里写成 `self.ffmpeg.ffmpeg`，真实属性是 `self._ffmpeg`。
   接口一直没被真调用过，单测只测了 `waveform.get_peaks` 本身。现在有 RPC 层用例覆盖。
3. **本地导入不带探测与抽帧**：`import_local()` 调 `import_videos()` 时没传 `prober`/`thumbs`，
   于是本地导入的项目 `duration_sec` 恒为 0、没有缩略图——素材库显示 00:00，编辑器时间轴与波形全按 0 算。
   时长真源是 `wb_asset.duration_ms`（`wb_project` 没有这一列），`_decorate()` 聚合出来给前端。
4. **取消转录要等下一行输出才生效**：`_default_runner` 原来在主线程里 `for line in proc.stdout`，
   解码长音频时子进程几分钟不吐一行，取消标志就一直没机会被读到。改成"读线程 + 队列，
   主线程 0.3s 轮询一次"，取消在半秒内落成 `kill_tree`。有回归用例（子进程静默后必须能杀掉）。
5. **切分点吸附**：`subtitle.op(split)` 有词级时间戳时会把 `at_ms` 吸到最近的词边界，避免把词劈成两半。
   第一版验收脚本断言「边界 == 请求的 at_ms」，于是被自己的正确行为判红——脚本已改成断言
   「两段首尾相接且落在原段区间内」，并把吸附后的实际值打出来。

## 四、已知偏差与未验证项

- **只验了 CPU**：这台机器没有可用 CUDA，`device=auto` 的 GPU 分支（`float16`）只在单测里用假对象走过，
  没有真显卡证据。装到有 N 卡的机器上要重跑一次 `accept_p2_transcribe.py --device cuda`。
- **长音频没压测**：最长只跑了 60s 素材。2 小时播客的内存与断点续传策略尚未验证（VAD 分段是流式的，
  理论上按 30s 窗口滚动，但没有实测数据支撑）。
- **词级时间戳精度**：`vad_fallback` 音频（这段 MV 全程人声混音乐）会出现 1.1s 级别的偏移，
  靠 `notes` 字段暴露而不是假装准确。
- **编辑器撤销栈**是内存态，刷新即失效；跨会话撤销不在本期范围（任务清单也未要求）。
- **`tests/frontend_test.js` 仍是脚本式断言**：新加的 22 项沿用同一套 `check()`，pytest 收集不到，
  只能靠 `npm test` 跑。这是 P0 定下的既有结构，不为它引入新框架。

---

## 五、P2-04 字幕 LLM 优化（假模型验收）

真 API key 不在本机，所以这一项**不验文笔，只验链路与防线**：起一个只做两件事的假 OpenAI 兼容
服务（把编号行清洗后回 JSON / 故意扩写），让真实的服务层、任务轮询、校验、diff、落盘、回滚全程跑一遍。

```
python scripts/accept_p2_optimize.py --config build/accept/p2.yaml
→ 13 项全 ok，最后一行「P2-04 字幕优化验收：全部通过」
```

断言清单（`scripts/accept_p2_optimize.py`）：

| 断言 | 说明 |
| --- | --- |
| `llm.test` 连上 | 证明配置读取、HTTP 客户端、超时与错误映射都通 |
| 产出 diff | 19 段有改动，且每条都只动 `text`，`start_ms/end_ms` 原样 |
| **优化阶段不写库** | 预览就是预览，没点「应用」前数据库一个字都没变 |
| 只应用勾选的 | `applied=9 accepted=9`，未勾选的段保持原文 |
| 写了快照 | `transcript.prev.json` 落在项目目录，回滚靠它 |
| 三处一致 | 库 / `transcript.json` / `transcript.srt` 同一份文本 |
| 时间轴不变 | 应用前后 `start_ms` 逐段比对 |
| 回滚 | 回到优化前文本，19 段原样 |
| 模型胡写 | 整批失败、错误信息可读（「所有批次都失败了，字幕没有改动」）、原文一字未改 |

样张：`ark-` / `exhibition-subtitles-optimize.png`（diff 面板）、`*-settings-llm.png`（测试连接按钮）。

这一轮顺手修掉的界面缺陷：

- **演示模式状态条是假的**：`app.js` 在 `?mock=1` 时 `finishBoot(null)` 提前返回，
  版本号一直显示 `v—`、依赖一直卡在「依赖检查中…」。现在 mock 分支也拉一次 `app.bootstrap` 再画。
  前端测试加了回归断言（25 项）。
- **mock 数据自称缺依赖**：样张里出现「缺少 yt-dlp / faster-whisper」，那是 mock 自带的假状态；
  改成按「装好了」给，缺依赖是状态提示，不该混进样张里干扰判断。

### 仍未验证

- **真模型的文笔与稳定性**：只验了协议与防线。真 key 到位后跑
  `python scripts/accept_p2_optimize.py --config <真配置>` 并人工抽看 diff 质量即可，代码无需改。
- **长字幕分批的边界**：验收用 19 段（单批）。分批逻辑由 24 个单测覆盖（含尾批、`size=0`、
  部分批失败），但没有上千段的真任务数据。

---

## 六、P2-05 翻译与术语表（假模型验收）

同 P2-04 的理由：真 key 不在本机，**验链路和防线，不验文笔**。假模型按编号行回译、
把术语表里的目标词原样带出来、还有个「不想按格式回话」的坏模式。

```
python scripts/accept_p2_translate.py --config build/accept/p2.yaml
→ 24 项全 ok，最后一行「P2-05 翻译与术语表验收：全部通过」
```

断言清单（`scripts/accept_p2_translate.py`，真库真文件）：

| 断言 | 说明 |
| --- | --- |
| `llm.test` 连上假模型 | 配置读取、HTTP、错误映射一次过 |
| 每段都拿到译文 | `translated=19 / total=19` |
| `auto` 在提交时定死 | 回给界面的 `target=zh` 是具体语言，不是 `auto` |
| **翻译不写 transcript** | 原文一字未改，`wb_transcript` 没被动过 |
| **草稿不落文件** | 没有 `subtitle.zh.srt` / `subtitle.bilingual.ass`——确认之后才有 |
| 草稿进 `wb_subtitle` | `kind=translation`、`status=draft`、19 段 |
| 术语表进了 prompt | 断言抓的是假模型收到的 prompt 全文，不是内存里的配置 |
| 译文带出术语目标词 | 术语表不是只写在 prompt 里就算数，落定文本里要能看见 |
| 两处文件写出来 | `subtitle.zh.srt` + `subtitle.bilingual.ass` |
| 纯译文 SRT 段数一致 | 19 条 ` --> ` |
| 双语 ASS 每段一条 Dialogue | 19 条，且每条都是 `原文\N译文` 双行（`original_top`） |
| 草稿转 `confirmed` | 确认动作有落库，不是只改内存 |
| 两个文件登记成资产 | `language=zh` 与 `language=bilingual` 各一条 |
| 模型乱写时任务失败 | `status=failed`，错误信息「所有批次都失败了，没有译文」 |
| 失败不碰已确认文件 | ASS 字节级对比未变 |
| 失败不打回 draft | 草稿仍是 `confirmed`（这条是验收抓出来的真缺陷） |
| 失败不改原文 | 19 段原样 |
| 只重试指定段 | 提交 `segment_ids=[s1,s2]`，其余 17 段译文仍在（19/19 有译文） |
| 重译后退回 draft | 改了译文就得重新确认，这是有意的 |

样张：`ark-` / `exhibition-subtitles-translate.png`（翻译面板 + 失败段占位）、
`*-settings-glossary.png`（术语表编辑 + 导入/导出 CSV）。

### 这一轮修掉的真实缺陷

**`translated` 计的是「有译文的段数」，不是「本次译出来的段数」。**
重试两段时库里还留着上次 19 段译文，于是「整批全失败」被数成 19 段成功：
任务报 succeeded，草稿被重写成 `draft`——用户看到的是「重试了一下，确认状态没了」，
而这次其实一条都没译出来。修法：

- `translator.translate()` 拆两个计数：`translated`（本次真写进去的，用 `fresh` 集合标）
  与 `filled`（全量有译文，界面显示「已译 19/19」用它）。
- 服务层判「全批失败」用 `translated`，`filled` 只回给界面。
- 回归：`test_retry_counts_only_this_run_not_leftovers`、
  `test_retry_that_fails_every_batch_reports_zero_translated`、
  `test_handler_failed_retry_keeps_confirmed_draft`。

### 仍未验证

- **真模型译文质量与术语遵循度**：prompt 里要求「术语表强制」，字符串层面只能保证
  「模型把源词原样带出来时替换掉」；模型自己换写法（`AI 芯片` → `AI chip`）管不住，那是 prompt 的活。
  真 key 到位后人工抽看。
- **日文 / 英文目标的排版**：验收只跑了中文字幕，`zh/en/ja` 三种目标的长度预算（中文 22 字 / 英文 80 字符）
  由单测覆盖，没有真素材样张。
- **ASS 在剪辑软件里的实际渲染**：文件结构与时间码有断言，但没有导入 PR / 达芬奇的截图证据。
- **术语表上千条时的 prompt 体积**：没有压测；当前是全量塞进每次请求。

## 七、P2-06 字幕导出（真机 ffmpeg 验收）

这一节不假任何东西：真的 ffmpeg、真的 1080P 成片、真的 ffprobe。

```bash
./.venv/Scripts/python.exe scripts/accept_p2_export.py --config build/accept/p2.yaml
```

项目 `pj_80184d99f6ee`（19 段，源 `short_60s.mp4` 8.0 MB），**16 项全通过**，末行
`P2-06 字幕导出验收：全部通过`。

| 断言 | 实际值 |
| --- | --- |
| 三种格式都生成 | `ass, srt, vtt` |
| 三个文件非空 | `subtitle.orig.srt 1291B` / `.ass 2171B` / `.vtt 1232B` |
| **三种格式段数都等于转录段数** | `srt=19 ass=19 vtt=19`，期望 19 |
| VTT 有 `WEBVTT` 头 | 是 |
| SRT 用逗号毫秒 | `00:00:01,466 --> ...` |
| **1080P 字号是 720P 的 1.5 倍** | `720P=54` / `1080P=81` |
| 软字幕任务成功 | `status=succeeded` |
| **软字幕成片里有字幕流** | ffprobe 看到 1 条 |
| 编解码器是 `mov_text` | 是（MP4 软字幕的唯一通用选择） |
| 成片名带分辨率与模式 | `short_60s.1080p.soft.mp4` |
| 硬字幕任务成功 | `status=succeeded` |
| **硬字幕画面与原片不同** | `1.46s=22.4dB 6.60s=21.5dB 10.64s=23.8dB 13.70s=23.5dB 16.08s=23.5dB`（有限 PSNR 就是"画进去了"） |
| 硬字幕成片有视频流 | 是 |
| 源视频没被改动 | `8379404B`，字节不变 |
| 导出产物登记成资产 | `export=3`、`subtitle=6` |

### 这一轮修掉的真实缺陷

1. **纯译文 SRT 里原文和译文挤在一起。** 带 `language` 导出时，SRT 用了
   `merge_bilingual()` 的结果——它的 `text` 是「原文\n译文」两行拼接，于是
   `subtitle.en.srt` 每条都是两行。SRT/VTT 是单层格式，塞两行播放器就叠在一起。
   修法：导出时准备两份载荷，`plain_items`（只取 `translated_text`）给 SRT/VTT，
   `payload_items`（带 `lines`）只给 ASS 用。回归：`test_export_with_language_uses_translation_draft`
   现在断言 SRT 里**没有**原文、ASS 里两行都在。
2. **全空白的行也写进 ASS。** `format_ass()` 原来只跳过 `""`，`"   "` 会生成一条
   「什么都不显示但占时间」的 Dialogue，导出后行数对不上、用户以为丢了一句又找不到。
   改成 `body.strip()`。回归：`test_format_ass_escapes_braces_and_drops_empty`（2 段进、1 条出）。
3. **样式默认字号与样式库对齐。** `ASS_DEFAULT["size"]` 是 54（与 `wb_style` 内置 default
   同源），测试里手写 48 会导致「文档写 54、导出成 48」。测试改成按 `ASS_DEFAULT` 推算。

### 验收脚本自己踩的两个坑（写给下一个写脚本的人）

- **PSNR 不能用 `-ss` 快进**：`-ss` 放在 `-i` 前面是快速定位到关键帧，两路各自 seek，
  取到的可能都不是目标帧，于是「字明明烧进去了」也判 PSNR=inf。改成 `trim` 精确切一帧。
- **ffmpeg 不接受中文相对路径**：`build/accept/media/本地导入/...` 直接报
  `No such file or directory`（它的 argv 不走宽字符）。绝对路径 + `Path.resolve()` 才行。

### 仍未验证

- **样式库里的自定义样式在成片上的观感**：接了 `style_to_ass()`，但验收用的是内置
  default；不同字体/描边在 4K 下的可读性没有人工确认。
- **超长队列下的软字幕并发**：`video.export` 并发限 1，队列里排 5 个没测过等待体验。
- **ASS 在剪辑软件里的实际渲染**：文件结构与时间码有断言，没有导入 PR / 达芬奇的截图。
- **硬字幕重编码的耗时/画质取舍**：固定 `libx264 crf 20 veryfast`，没有在长视频上比对过。

## 八、P2-07 总结适配器（假模型验收）

日期：2026-10-30
跑法：`./.venv/Scripts/python.exe scripts/accept_p2_summary.py --config build/accept/p2.yaml`
环境：本地 `ThreadingHTTPServer` 假 OpenAI 端点（`("127.0.0.1", 0)` 随机端口），
项目 `pj_80184d99f6ee`（19 段字幕），脚本**只在内存里改 `service.cfg.llm.*`，不写用户 config.yaml**。

为什么用假模型：这一阶段要证的是**接线**——字幕喂成 bili-scribe 认的 `Bundle`、
四个 stage 翻成人话、每次生成落一版且历史只增不减、角色真的换提示词、一句话打磨能修订、
模型乱写时错误码说得清。这些跟模型多聪明无关；假模型反而更容易逼出边界（真模型很少
恰好返回非 JSON）。

### 断言与证据

| 验收项 | 证据 |
| --- | --- |
| 角色清单含 standard | `standard、xueshu、renhua、shensi、wengao、xiezuo、fenxi、jinglian` |
| 有走三轮回的角色 | 除 standard 外 7 个都是（`passes>=3`） |
| llm.test 连上假模型 | 18ms |
| standard 生成成功 | `status=succeeded`，`result.version=1` |
| 产出有结论与正文 | `one_line=用三十分钟把注意力机制…`、`summary_md` 三个二级标题 |
| 时间线条目带时间戳 | `02:10 / 12:40 / 24:05`（素材里的时间戳被原样带出） |
| 落盘 | `summary.md`、`summary.json`、`summary.v1.json` 都在 |
| 登记资产 | `kinds` 里出现 `summary`（同目录还有 audio/export/source/subtitle） |
| stage 推进 | `transcribed → summarized` |
| **字幕原文没被改动** | 19 段文本逐条相等 |
| 版本列表 | 1 条 |
| 换角色（xueshu） | 新增第 2 版，`role=xueshu`，system 提示词与上次不同，共 3 次调用（批判＋修订） |
| 首次生成不写改动说明 | `revise_note` 为空（bili-scribe 只在修订版里记，见偏差记录） |
| 一句话打磨 | 新增第 3 版，`revise_note` 非空 |
| 恢复旧版 | 新增第 4 版，`restored_from=1`，正文与第 1 版一致 |
| 模型乱写 | 任务 `failed`，`error_code=LLM_BAD_RESPONSE`，版本数不变、正文不变、`summary.md` 还在 |
| 没字幕的项目 | 本库无此项目，**跳过**（单测 `test_summary_*` 覆盖 CONFLICT） |

### 这一轮修掉的真实缺陷

1. **`ChatError` 的六个错误码不在 `ERROR_CODES` 里**（P2-07 开工时发现）：
   `LLM_AUTH / LLM_RATE_LIMIT / LLM_BAD_REQUEST / LLM_UPSTREAM / LLM_TIMEOUT / LLM_FAILED`
   由 `infra/llm.py` 真的会抛，但表里没有 → 构造异常时先抛 `ValueError("未知错误码")`，
   用户看到的是「内部错误」，真正原因（key 不对/被限流/模型名写错）全丢。
   回归：`test_every_llm_error_code_is_registered` 用正则扫源码，漏一个就红。
2. **mock 项目缺 `has_transcript`**：真后端 `_decorate` 一定返回它，mock 的 PROJECTS 没有 →
   总结页把「不知道」当「没字幕」，直接拦下报「先去转录」。修法：三个 mock 项目补该字段；
   前端判断也从 `!has_transcript` 改成 `has_transcript === false`（缺字段不算没字幕）。
3. **mock 的总结正文写死了一段 MV 文案**：截图里左边项目叫「讲透注意力机制」、右边在讲
   Rick Astley——审图的人第一眼就会怀疑数据造假。改成按项目标题生成正文
   （`summaryContentFor(projectId)`）。

### 仍未验证

- **真模型的总结质量**：提示词是 bili-scribe 调过的，但中文长视频上的实际可用度、
  时间线是否真的只引用素材里出现过的时间戳，没有真模型抽检。
- **长素材分块**：`max_input_chars=24000` 的分段预读只在单测里用小阈值触发过，
  真·两小时视频的预读耗时与笔记质量没跑。
- **弹幕进素材**：`summary.start` 收了 `danmaku` 参数并截断到 400 条，但界面还没做入口，
  也没有把它拼进 `Bundle` 的实测。
- **并发**：`summary.run` 并发限 3，三个任务同时打同一家模型服务的限流表现没测。

## 九、P2-08 合集总览（假模型验收）

跑法（配置与前面几节共用）：::

    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p2_collection.py \
        --config build/accept/p2.yaml

末行 **`P2-08 合集总览验收：全部通过`**，共 36 项。夹具说明：合集分组的真实入口是
「同一个 B 站合集下载下来的多集」，走真站既慢又不可控，所以脚本用 `import.local`
建三条项目再直接把 `collection_title` 写成同一个值——分组逻辑（`repo.collection_titles`）
与服务层读子项的逻辑照常被真实执行，只有"标题从哪来"这一步被夹具替代。

### 断言与证据

| # | 断言 | 证据 |
| --- | --- | --- |
| 1 | 建了三条同合集项目 | 三个 project_id |
| 2 | `collection.list` 认得这个合集 | 1 个合集 |
| 3 | 合集有 3 集 | `items=3` |
| 4 | 还没总结时 `summarized=0` | — |
| 5 | 还没总结时 `ready=False` | — |
| 6 | 还没总览时 `has_overview=False` | — |
| 7 | 子项没总结时拦住 | `CONFLICT`「有 3 集还没有总结，先给它们各出一版」 |
| 8 | 只有一集时拦住 | `CONFLICT`「合集至少要有 2 集才能出总览」 |
| 9 | 三条子项各自总结成功 | 各 1 版 |
| 10 | 提交返回 `summary.collection` 任务 | — |
| 11 | 总览任务成功 | job status `succeeded` |
| 12 | 任务是第 1 版 | `version=1` |
| 13 | 只发一次请求 | `calls=1`（三轮回不套在合集上） |
| 14 | 读得回总览 | `found=true` |
| 15 | 有 `theme` | 40 字内的主题句 |
| 16 | 结论至少 3 条 | 4 条 |
| 17 | timeline 只剩 3 条 | 模型给的 4 条里有 1 条野标题被丢 |
| 18 | timeline 标题都是真子项 | 三条都在 `CHILD_NAMES` 里 |
| 19 | 野条目记进 `content.notes` | 「模型给的 1 条时间线对不上任何子项，已丢弃」 |
| 20 | `watch_order` 覆盖 3 集 | — |
| 21 | 有 `overlap` | — |
| 22 | 子项带了 `bvid`/链接 | — |
| 23–25 | `collection_overview.md` / `.json` / `.v1.json` 落盘 | 公共父目录 `build/accept/media/本地导入/` |
| 26 | 子项总结一字未动 | 三条 `content` 的 JSON 摘要逐字相等 |
| 27 | 子项版本数没变 | 前后都是 1 |
| 28 | 换角色也能出（xueshu） | job 成功 |
| 29 | 两版共存 | `versions=2` |
| 30 | 新版本记了角色 | `role=xueshu` |
| 31 | 用 `col_` id 也能读 | `summary.collection.get` 两种 id 都通 |
| 32 | 模型乱写时任务失败 | status `failed` |
| 33 | 错误码说得清 | `error_code=LLM_BAD_RESPONSE` |
| 34 | 失败没加版本 | 仍是 2 版 |
| 35 | 失败没改文件 | `collection_overview.md` 字节相等 |
| 36 | 读不存在的合集不炸 | `found=false`、`children=[]` |

### 这一轮修掉的真实缺陷

1. **首屏按钮永远是灰的**：`summary.js` 的 `head()` 在数据回来之前就建好了 DOM，
   而 `paint()` 只重画 `#summary-host`，不重画头部。后果：第一次进总结页
   「生成总结」点不动、项目下拉显示「（素材库还是空的）」；合集模式更彻底——
   切过去时 `state.collection` 还是空串，`#sum-coll-start` 建出来就是 disabled，
   之后再没人改它。修法：把 `head()` 包进 `#sum-head`，每次 `paint()` 用
   `swapHead()` 重建这一块。回归：前端测试断言首屏 `#sum-start` /
   `#sum-coll-start` 的 `disabled === false`。
2. **总览的 `notes` 没落进 `content`**：`core/overview.py` 的 `run()` 把 notes 放在
   返回值上，而落库的是 `content`、界面读的是 `content.notes`。后果：模型给了 4 条
   timeline、界面只显示 3 条，用户看不到任何解释——"是不是吞了我的内容？"
   修法：`content["notes"] = notes + llm.notes`。回归：`test_run_stores_notes_inside_content`。
3. **分集行把标题念两遍**：模型给的 `one_line` 常以标题开头，界面又拼了一次
   「标题：」，于是「【硬核】三十分钟讲透注意力机制：【硬核】三十分钟讲透注意力机制：
   把抽象结论拆成能照做的步骤」。修法：渲染前若 `one_line` 以 `title` 开头就切掉前缀
   并去掉紧跟的冒号/逗号。
4. **子项行借用了 stage 文案**：素材库里的 stage 还停在「已转录」，但这一集明明已有总结，
   左右两处说法打架（左边"已转录"、右边总览引用了它的总结）。改成有总结就写「有总结」。

### 仍未验证

- **真模型的合集视角**：提示词要求「跨集找重复、分歧、缺口」，假模型只会照抄标题，
  真实长合集里它是否真能看出「第二讲被第三讲推翻」没有抽检。
- **跨目录的合集**：`os.path.commonpath` 取公共父目录；子项分散在不同磁盘时
  （Windows 上 `commonpath` 会抛 `ValueError`）会退到第一个子项目录，这条分支只有单测覆盖。
- **很多集的合集**：20 集以上时 `MAX_CHARS=16000` 会触发压缩模式（要点截 2 条），
  压缩后总览质量是否还能用没测。
- **合集与任务的并发**：`summary.collection` 限 1 且与 `summary.run` 分开限流，
  子项正在重出时点总览的行为（会读到旧版还是拒绝）没测。

## 十、P2-09 发布草稿（本地验收，不用模型）

草稿的内容全部来自**已经存好的总结行**，这一层一个请求都不发，所以不需要假模型：

    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p2_publish.py \
        --config build/accept/p2.yaml

末行 **`P2-09 发布草稿验收：全部通过（37 项）`**。

### 断言与证据

| 组 | 断言 | 证据 |
| --- | --- | --- |
| 素材 | 没总结的项目被拦下 | `CONFLICT`「这个项目还没有总结，先去「总结」页出一版」 |
| 素材 | 预览取到最新一版 | `summary_id`/`version` 与插入的那版一致 |
| 正文 | 标题默认取视频标题；摘要字段已下线（恒空串） | 逐字相等 |
| 正文 | 有 `## 要点`、`## 时间轴`（带 `` `00:12` ``）、AI 披露 | 正文逐段比对 |
| 正文 | **「注意：素材为机器转录…」不进正文** | 该串不出现 |
| 正文 | **内部嘱咐不进正文** | 「素材局限」「采集提示」不出现 |
| 正文 | Delta 转纯文本后没有 `**` / `## ` | — |
| 字数 | 门禁常数只剩标题 | `limits == {title:80}` |
| 字数 | 字数与手算一致 | `len("".join(text.split()))` |
| 存库 | 存成 `status=draft`，带 `bvid`/`aid`，未填发布/撤回时间 | 行字段 |
| 存库 | 库里真有这一行 | `repo.publish_for_project` |
| 列表 | 能看到草稿，带项目标题/UP/字数 | `publish.list` |
| 编辑 | 改标题生效、改摘要不再生效（字段已下线），且**落在同一行** | 行 id 不变 |
| 编辑 | 重复保存不堆积 | 列表恒为 1 |
| 编辑 | 手改 Delta 以用户为准，并重算字数 | 正文逐字相等 |
| 门禁 | 100 字标题被拒 | `VALIDATION`「标题超过 80 字」 |
| 门禁 | 260 字摘要参数被忽略而不是被拒 | 存下来仍是空串 |
| 门禁 | 空正文（`[]`）被拒 | `VALIDATION` |
| 门禁 | 被拒的三次**都没改库** | 行 id 没变 |
| 设置 | 关掉 `disclose_ai` 后正文不再带披露 | — |
| 持久化 | 换一个服务实例仍读得到草稿 | 重开 `WorkbenchService` |
| 边界 | 草稿撤回说清楚只有已发布的才能撤 | `CONFLICT`「只有已发布的笔记才能撤回」 |
| 删除 | 删后读不到、重复删除 `NOT_FOUND`、列表为空 | — |

### 这一轮修掉的问题（都是我自己写的 bug，被测试抓住）

1. **标题被静默截断**：`default_title()` 原来 `[:80]`。
   后果是"超长"这件事从界面上消失了——用户看到一个刚好 80 字的标题，
   发出去却被平台拒，而平台拒的理由他永远看不到。修法：不截断，
   超长如实保留并报 warning，`check_draft()` 在存库前拦下。
   回归：`test_draft_warns_instead_of_silently_truncating_over_limit`。
   （摘要那半条已随字段下线一起退场，对应用例改名成
   `test_an_over_long_summary_no_longer_warns`。）
2. **mock 缺 `projectById`**：新加的分支调了一个不存在的函数，前端测试直接崩在
   `state.projects.map`——这类"mock 自己写的代码没有 mock"只有真跑一遍才发现。
3. **列表行元信息折行**：`.pb-row__meta` 没有 `nowrap`，窄栏里「351 字」被拆成两行，
   行高忽高忽低。截图看出来的。

### 仍未验证

- **真站发布**：`publisher.publish_note` 一次都没调过。P2-09 只做草稿，
  门禁（内容确认 / 账号与视频 / 频率与重复）与 `79508 / 79513 / 12015 / -111`
  这几个平台错误码要等 Phase 5 才有对象可测。
- **笔记正文的渲染效果**：Delta 只用了 `bold`，B站 笔记编辑器里标题会不会显得很平、
  列表符号 `- ` 会不会别扭，只有真发一次才知道。
- **超长正文**：方案书没给笔记正文的长度上限，本层不设限；真接口如果有上限，
  现在的实现会在发布时被拒而不是提前拦。

