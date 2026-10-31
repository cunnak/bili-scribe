# Phase 5 验收记录（加固与打包）

样张与断言都在这里。Phase 5 不新增界面，所以没有新截图；证据以命令输出为主。

跑法统一为：

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p5_xxx.py --config build/accept/p2.yaml
```

## 一、P5-01 取消 / 重试 / 进程清理（真 ffmpeg + 真进程）

`scripts/accept_p5_cancel.py` —— **故意跑真 ffmpeg**：取消是不是真生效，只有看进程表才算数。

| # | 断言 | 实测 |
|---|---|---|
| A1 | 真 ffmpeg 命令能被取消 | 1.8 秒返回，`CANCELED` |
| A2 | 取消在命令自然结束前返回 | 1.8 秒（命令本身要 60 秒） |
| A3 | 取消后那个进程真的没了 | `tasklist` 里查不到 pid |
| A4 | 没有留下孤儿 ffmpeg | 取消前后 ffmpeg 进程集合无新增 |
| B1 | 导出任务能提交 | `job_26c860c5fdad` |
| B2 | 取消请求被接受 | ok |
| B3 | 任务落到 canceled | `canceled` |
| B4 | 取消也保存了日志 | 1 行 |
| B5 | 取消的导出没被记成成功 | 0 条 succeeded |
| B6 | 取消没在导出目录留半成品 | 新增 `[]` |
| C1 | 重试起了新任务 | `job_e65e4231bdb3` ≠ 原任务 |
| C2 | 重试成功 | `succeeded` |
| C3 | 成片时长与期望相符 | delta 0 ms |
| C4 | 成片在盘上且非空 | 17,339,363 B |
| D1 | 会失败的任务能提交 | `job_ea052e82c111` |
| D2 | 失败任务状态是 failed | `failed` |
| D3 | 失败任务带日志 | 1 行 |
| D4 | 失败原因可读 | 「精确合并失败」 |
| E1 | 合并任务能提交 | `job_76a7bf815c74` |
| E2 | 合并能取消 | `canceled` |
| E3 | 任务临时目录已清 | `cache/tmp/<job_id>` 不存在 |
| E4 | 合并记录没被标成功 | `canceled` |

单测 `tests/wb/test_cancel.py`（8 项）额外覆盖：`FFmpeg.run` 正常返回 / 非 0 带尾巴 /
超时被杀 / 取消杀进程树；`JobManager` 取消时杀掉 handler 登记的子进程（handler 故意
卡在 `communicate()` 上）、失败任务把日志尾巴写进 `result.log`、成功任务不背日志、
任务收尾删自己的 `tmp/<job_id>`。

修掉的问题：

1. **`FFmpeg.run` 根本不能取消**：它是 `subprocess.run`，导出、mux、烧字幕、快速拼接
   全走它。用户点取消，进度条继续动到命令自己跑完为止——这不是"慢"，是假按钮。
   改成 Popen + 0.2 秒轮询：取消杀整棵进程树并抛 `CANCELED`，超时也杀并抛 `PROCESS_FAILED`。
2. **handler 卡在 `readline` 上时取消退不出来**：只置 Event 没用，要等子进程出口才醒。
   加了 `JobContext.register_process()`：`JobManager.cancel()` 现在会直接杀登记过的进程树。
3. **取消/失败会留下半截 mp4**：被杀掉的 ffmpeg 已经写了一部分字节，文件能看见、打不开、
   体积还不小。切片导出与字幕导出都在异常路径删掉目标文件。
4. **临时目录只有合并自己清**：其它任务类型漏写 finally 就留垃圾。改成 `JobManager`
   统一在 `_finish` 删 `cache/tmp/<job_id>`（启动时的 sweep 仍然保留，兜住强杀）。
5. **失败日志只活在事件里**：窗口关掉就没了，而用户回头要看的恰恰是"为什么失败"。
   现在失败/取消时把最近 200 行日志写进 `job.result.log`；成功不存（占地方没人看）。

仍未验证：

1. 真实 yt-dlp / faster-whisper 进程在取消后是否也没残留（本轮杀的是 ffmpeg；
   下载与转录走的是同一套 `kill_tree`，但没在真站上复验）。
2. Windows 之外的行为（`kill_tree` 是 taskkill，Linux/macOS 未验）。
3. 一亿字节级的超长导出在取消瞬间磁盘残留（B6 只覆盖了单段 60 秒）。

## 二、P5-02 重启恢复（真强杀一个进程）

`scripts/accept_p5_recovery.py` —— 起一个**真子进程**（`scripts/p502_child.py`），
让它提交一次真导出任务，然后用 `kill_tree` 把整棵进程树杀掉，再重新打开后端。

| # | 断言 | 实测 |
|---|---|---|
| S1 | 子进程提交了导出任务 | `job_3a90c6cc73ec` |
| S2 | 子进程被强行结束 | rc=1 |
| S3 | 强杀没有留下孤儿 ffmpeg | 新增 `[]` |
| R1 | `app.info` 带出 recovery 统计 | `{'jobs': 1, 'merges': 0, 'projects': 0}` |
| R2 | 至少收尾了一个中断任务 | jobs=1 |
| R3 | 那个任务不再是 running | `failed` |
| R4 | 错误码是 INTERRUPTED | `INTERRUPTED` |
| R5 | 文案说清是上次退出导致 | 「上次退出时这个任务还没跑完（可以重试）」 |
| E1 | 产物齐全的项目保持 summarized | `summarized` |
| E2 | 缺产物的项目被回退 | `new` |
| E3 | 回退写明了原因 | 「启动检查：summarized 的产物不见了，已退回 new」 |
| E4 | 回退记录里能看到这个项目 | rolled 里有该 project_id |
| T1 | 中断的任务可以重试 | `job_30b83ccbb678` |
| T2 | 重试跑成功了 | `succeeded` |
| T3 | 重试真的产出了文件 | 17,339,363 B |

单测 `tests/wb/test_recovery.py`（14 项）：running/queued 任务标 `INTERRUPTED` 且可重试、
succeeded 任务不动、running 合并记录收尾；项目阶段回退（summarized 缺 md → new、
transcribed 缺转录 → downloaded、clipped 缺候选 → new、published 缺记录 → merged）、
证据齐全的项目保持原阶段、未知阶段不猜、恢复幂等（第二次 0 条）、`app.info` 暴露统计。

修掉的问题：

1. **任务永远停在 running**：`wb_job` 里上次被杀的任务没有任何地方会结束它，界面上
   就是一排进度条卡在 30%，也没有重试入口。现在启动时统一标 `failed` + `INTERRUPTED`。
2. **中断被报成"内部错误"**：原来复用 `INTERNAL`，用户看到"内部错误"只会以为程序坏了，
   而真实答案只是"点重试"。新增 `INTERRUPTED` 码（可重试），文案直说"上次退出时中断了"。
3. **合并记录卡在 running**：合并页显示"合并中"，既等不到结果也点不了重试。
   新增 `merge_mark_interrupted()`，启动时把它们标失败并写清理由。
4. **项目阶段会撒谎**：`stage=summarized` 但 `summary.md` 写一半被杀，点开是"有总结但没内容"。
   现在启动时逐级检查"这一步的证据还在不在"，不在了就退回上一个证据齐全的阶段，
   并把原因写进 `project.error`。
5. **恢复统计永远是 0**：`JobManager` 构造时就把残留任务标掉了，等恢复流程再去查已经没人可查。
   改成 `JobManager` 记住数量（`self.interrupted`）并交给 `recover(jobs=...)`。

仍未验证：

1. 断电（非 kill）场景：Windows 上没模拟真实掉电，只做了进程树强杀。
2. 写入一半的 `transcript.json`（既有行又有文件，但 JSON 被截断）——本轮按"文件在就算有"处理，
   没有对内容做校验。
3. 多个后端实例同时启动时的恢复竞争（正常使用只有一个窗口）。

---

## 三、P5-03 错误分类与日志（真库验收）

跑法：

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p5_diagnostics.py --config build/accept/p2.yaml
```

真后端、真配置、真日志文件，不模拟任何一环。脚本故意在内存里塞一个假 key
（`sk-accept5diagnostics0001`），包里只要出现它就是事故。

| # | 断言 | 实测 |
|---|---|---|
| 1 | `app.info` 报日志位置 | `…/build/accept/logs` |
| 2 | 日志文件真的在盘上 | `wb-20261030.log` |
| 3 | 日志开头记了会话信息 | `==== 会话开始 pid=35264 python=3.14.0 …` |
| 4 | 日志目录名是 `logs` | 是 |
| 5 | demo 任务跑完 | `succeeded` |
| 6 | 任务日志落盘（按 job id 可查） | `job_7b8d55451e76` 出现在文件里 |
| 7 | 诊断包已生成 | `media/diagnostics/diagnostics-20261030T132603.zip` |
| 8 | 诊断包落在媒体目录下 | 是 |
| 9 | 包里有报告/脱敏配置/日志三样 | `report.txt,config.redacted.yaml,log.txt` |
| 10 | 报告带数据库版本 | `schema_version: 4` |
| 11 | 报告带错误码表 | `VALIDATION: 参数不合法` |
| 12 | 报告带依赖状态 | `ffmpeg: …\WinGet\Links\ffmpeg.EXE` |
| 13 | 报告带日志目录 | 是 |
| 14 | 配置里的 api key 没泄露 | 假 key 不在包里 |
| 15 | 脱敏后仍能看出是哪个字段 | `api_key: "***"` |
| 16 | 日志尾巴里有本次会话 | 1218 字符 |
| 17 | 日志尾巴里没有 api key | 是 |
| 18 | 包不允许写到白名单外 | `C:/Windows/Temp/…` → `VALIDATION` |
| 19 | 指定媒体目录内的路径可以 | `media/diag-accept.zip` |
| 20 | `app.log_tail` 返回文本 | 15 行 |
| 21 | `log_tail` 里的凭据也被抹掉 | 是 |
| 22 | `log_tail` 列出日志文件 | `["wb-20261030.log"]` |
| 23 | 每个错误码都有可读中文文案 | 26 个全过 |
| 24 | 错误码表够用（≥24） | 26 |

单测：`tests/wb/test_errors.py` 24 项——错误码表完整性（含**扫全仓源码里
`WBError("XXX"` / `ChatError("XXX"` 的字面量**，新增码忘进表会让用户看到"内部错误"）、
脱敏五种形态、`setup_logging` 幂等、按天清理、诊断包三件、服务层 RPC 与白名单。

### 这一轮修掉的问题

1. **JSON 形态的凭据漏抹**：`{"token": "abcd…"}` 里引号挡在值前面，`[^\s,;"']`
   一个字符都匹配不到 → 一字未抹。改成允许「键后 / 值前」各有可选引号。
2. **说明文字和三个按钮挤在一行**：第一版「诊断包 = 环境 + …」和三个按钮同处
   `.settings__row-actions`，窄窗口下换行成两截。拆成三行（说明 / 按钮 / 结果）。
3. **成功提示没有强调条**：`WB.toast("…", "ok")` —— 样式表里只有
   `toast--success/warning/error`，`ok` 掉进无样式分支。
4. **`"\n".join(...)` 被写成了真换行**：heredoc 里一层 Python 转义 + 一层 shell，
   文件里落下的是断开的字符串 → `SyntaxError: unterminated string literal`。
   统一改用 `chr(10).join(...)`。

### 仍未验证

- 真实登录态下 cookie 的**全部**字段名（现在覆盖 `SESSDATA/bili_jct/buvid3/DedeUserID`，
  但 B 站还有 `buvid_fp`、`_uuid` 之类，靠 `Cookie:` 整行替换兜底）。
- 日志长到几十 MB 时诊断包的体积（现在只取尾部 N 行，理论上没问题，没实测）。
- 用户把日志目录指到只读盘：`setup_logging` 会抛 `OSError`，目前没有"降级到内存日志"的分支。

---

## 四、P5-04 性能与事件节流（真事件总线 + 真 Chrome）

```bash
./.venv/Scripts/python.exe scripts/accept_p5_perf.py --config build/accept/p2.yaml
```

21 项断言，全过。前 17 项打真后端（真事件总线、真任务），后 4 项由
`scripts/bench_p5_perf.py` 在真 Chrome 里量（切页 / 1000 段字幕）。

| # | 断言 | 实测 |
|---|---|---|
| 1 | 任务跑完了（demo） | `succeeded` |
| 2 | 进度事件被节流到 4 次/秒以内 | `42 条 / 10.19s`（上限 44） |
| 3 | 进度事件没有一条都不发 | 42 条 |
| 4 | 最后一次进度补发到 100% | `progress=1.0` |
| 5 | 200 次突发只送达个位数 | 2 条 |
| 6 | 突发后带着最新值（不是被压成旧值） | `progress=0.995` |
| 7 | 突发没有污染别的任务的事件 | 是 |
| 8 | 任务跑完了（40 步） | `succeeded` |
| 9 | 日志事件带 `lines` 数组 | 11 条事件 |
| 10 | 攒批后的行数 ≥ 事件数（真攒了） | 11 行 / 11 条 |
| 11 | 日志一行都没丢 | 11 行 |
| 12 | 日志里有 `[demo]` 前缀 | `['[demo] 0s / 10s', …]` |
| 13 | 没有旧的单行字段（契约已改） | 是 |
| 14 | 200ms 窗口到点就发（≤0.4s） | 206ms |
| 15 | 窗口内的三行合成一条事件 | `['a', 'b', 'c']` |
| 16 | 25 行同窗口只发 1 条事件 | 1 条 |
| 17 | 25 行按顺序全在 | 25 行 |
| 18 | 前端基线脚本跑通 | `exit=0` |
| 19 | 切页均值 <200ms | 10.1ms |
| 20 | 切页最差 <400ms | 62.2ms |
| 21 | 1000 段首屏 <400ms / 滚动均值 <16.7ms / DOM ≤60 行 | 0.3ms / 4.3ms / 26 行 |

`bench_p5_perf.py` 逐页实测（均值/最差，毫秒）：

| 页面 | 实测 |
|---|---|
| 新建任务 | 25.4 / 62.2 |
| 素材库 | 6.2 / 7.2 |
| 总结 | 6.2 / 7.5 |
| 切片台 | 16.8 / 24.3 |
| 合并 | 6.1 / 6.6 |
| 发布 | 6.2 / 7.7 |
| 设置 | 6.3 / 6.4 |
| 字幕（带 1000 段） | 7.3 / 9.6 |

单测：`tests/wb/test_events.py` 9 项——节流到 4/s、按 key 各自保留最新值、
`emit_now` 不受节流、30 行同窗口只发 1~2 条、跨窗口分批、`flush_logs` 立即发、
`close()` 补发（关窗不吞最后几行）、空行不发、sink 抛异常不外泄。

### 这一轮改了什么

1. **日志从"一行一推"改成 200ms 攒批**：`EventBus.log(job_id, line)` 先把行攒在
   内存，窗口到点合成一条 `job.log` 事件（`lines` 数组）。契约变了——
   `tests/wb/test_jobs.py` 的断言跟着改成读 `lines`。
2. **任务收尾强制 `flush_logs()`**：任务失败时最后几行往往就是失败原因，
   等下一个窗口再发没意义（而且进程可能马上就没了）。
3. **`close()` 也 flush**：关窗把攒着的日志发掉，不让它随进程消失。

### 仍未验证

- **合成帧率下的滚动**：headless Chrome 的 rAF 接近 60fps，但真机上 120Hz 屏 +
  高 DPI 缩放时的绘制耗时会更高。数字好看不等于绝对不卡。
- **十万段字幕**：现在验证到 1000 段（方案书要求的口径）。虚拟列表理论上与总量无关，
  但 `indexQc` 与搜索那两条路径是按全量算的，十万段没测。
- **多任务并发时的 UI**：三个任务同时刷进度（下载/转录/导出）时状态条与任务卡的
  重绘频率只做了节流层的验证，没有肉眼观察过。

## 五、P5-05 打包与首次启动（真 PyInstaller 产物 + 真窗口）

跑法：

```bash
./.venv/Scripts/python.exe scripts/build_wb.py          # 出 dist/VideoWorkbench
./.venv/Scripts/python.exe scripts/accept_p5_package.py # 对产物做断言
```

`--check` 分支不开窗口，验收机也能跑；`accept_p5_package.py` 额外用**临时配置**跑一遍，
确认干净的机器上第一屏该看到什么。

| # | 断言 | 实测 |
|---|---|---|
| 1 | exe 存在 | `dist/VideoWorkbench/VideoWorkbench.exe` |
| 2 | 网页与提示词落进 `_internal/` | index.html / onboard.js / onboard.css / tokens.css / prompts / icon.ico 七项齐 |
| 3 | 没把 FFmpeg 打进去 | 目录里没有 `ffmpeg.exe` / `ffprobe.exe` |
| 4 | 没把语音模型打进去 | 没有 `model.bin` / `model.onnx` 这类权重 |
| 5 | 没把字体打进去 | 没有 `.ttf/.ttc/.otf` |
| 6 | 体积合理 | 412.9 MB / 2151 文件（< 900 MB 视为没夹带模型） |
| 7 | `exe --check` 退出码 0 | rc=0 |
| 8 | `--check` 报 ok | `"ok":true` |
| 9 | RPC 表非空且含关键命名 | 93 条，含 `app.onboard` |
| 10 | 体检五项齐全 | `['webview2','ffmpeg','model','bili','llm']` |
| 11 | 体检给得出 ready 结论 | `ready=False`（本机缺模型） |
| 12 | 干净配置下建了自己的库 | 库落在临时目录 |
| 13 | 没去动用户默认库 | 用户库大小未变 |
| 14 | 启动日志没有 Traceback | 干净 |

真窗口那一段（人工 + 截图，`docs/acceptance/p5/ark-frozen-*.png`）：

| # | 步骤 | 结果 |
|---|---|---|
| 15 | 双击 exe（带临时配置） | 窗口 1360×860 起来，标题 `VideoWorkbench` |
| 16 | 首屏 | 浮出「开始之前，先看五件事」，五项都是**本机真实数据**（WebView2 156.0.4314.50、FFmpeg 的 WinGet 路径、模型未下、登录态缺失、LLM 未配） |
| 17 | 汇总行 | 「还差：语音模型。其余可以先跳过，用能用的部分干活。」+「2/5 就绪」 |
| 18 | 点「跳过，别再提示」 | 配置里 `ui.onboarded: true` 落盘；窗口回到素材库 |
| 19 | 状态条依赖 | 修完后为「依赖齐全」（修前是「缺少 yt-dlp」） |

### 这一轮修掉的问题

1. **`--check --config X` 静默忽略 X**：`desktop.py` 的 `_check()` 第一版写死
   `WorkbenchService()`，于是「隔离验证」变成「开用户的库」——验收脚本第一次跑就把
   用户真实库的 WAL 打开了。`_check(config_path)` 一路传参修好。
2. **打包漏了 yt-dlp**：`video_workbench.spec` 的 `_WANT` 里没有它，装出来的 exe 状态条
   诚实写「缺少 yt-dlp」，下载按钮点了必失败。补进 collect_all 后重打，条上变「依赖齐全」。
3. **向导第一版不注册路由**：写成 `WB.pages.onboard = {...}`，本工程用
   `WB.router.define()`，`#/onboard` 渲染不出来 → 改成 define。
4. **mock 的 `app.open_url` 不记 url**：前端测试无法断定「链接交给系统了」，顺手把第 45 项
   也带红；补 `window.__wbOpenedUrls` 后一起绿。

### 仍未验证

- **干净 Windows 虚拟机**：本机有 WebView2、有 FFmpeg、有 Python 3.14。上面 19 条都是在
  「装过开发机的机器」上跑的；真·全新系统的 WebView2 缺失路径只有代码与文案层面的保证。
- **一次完整业务链路在打包版里跑通**（下载→转录→导出）：打包版只验到「窗口起来 + 首屏 +
  体检 + 跳过落盘」；转录要 1.6 GB 模型，没在这台机器上重下。
- **安装包/分发形态**：现在交付的是 one-folder 目录（可绿色运行），没有做 installer、没有签名。

---

## 六、P5-06 全量验收矩阵

跑一遍：

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p5_matrix.py
```

可加 `--skip-visual`（不拍截图）或 `--skip-network`（跳过真站下载）。

### 结果

| 项 | 数量 | 结果 |
|---|---|---|
| 验收脚本（§8.1–8.7 + 闭环回放） | 27 | **27/27 全过** |
| 视觉矩阵（两套皮肤 × 全部页面） | 33 + 33 | 全部渲染成功（130.9s / 131.4s） |

明细写在仓库根的 `验收记录.md`（由矩阵脚本生成，逐条标准对到脚本，带每次运行的实测尾行）。
样张在 `docs/acceptance/matrix/`。

### 抓到的两类问题（共 7 项红，都不是产品 bug）

**A. 跨阶段遗留的过时断言（2 项，真缺陷 → 已改）**

1. `accept_p3_export.py` 里还断言「9:16 导出必须是 UNSUPPORTED」——P3-06 做完竖屏后这条永远假红。
   改为断言「非法竖屏模式被拦（VALIDATION）」，保留「参数不会静默放过」的原意。
2. `accept_p4_merge.py` 里还断言「精确模式必须报 UNSUPPORTED 且提到 P4-03」——P4-03 做完后同样的假红。
   改为「精确模式可以提交并跑完」，诚实性交给 `accept_p4_precise.py`（那里真跑 ffmpeg）。

**B. 脚本互相污染验收库（5 项红 → 用隔离根治）**

所有脚本共用 `build/accept/wb_p2.db`。`accept_p4_publish_do.py` 会把草稿置成 published，
紧接着跑的 `accept_p4_records.py`（断言「刚发过不能发」）与 `accept_p4_publish.py`（断言「三关全绿」）
就被上一条记录卡住，报出与自己无关的红。另外两个具体坑：

3. `accept_p4_*` 挑项目只要求「有总结」，挑到导入的本地视频（无 BV 号）→ 账号关天然 blocked，
   断言「三关全绿」假红。改为「有 BV 号 + 有总结」。
4. 通知清理用的是 `publish.list`，而它自 P4-06 起只回草稿 → 清不到已发布行，清理静默失效。
   改用 `repo.publish_published(bvid)` 撤掉。
5. `accept_p2_summary.py` 靠「到库里找无字幕项目」验拦截；那个项目后来被别的脚本补上字幕，
   这条断言就变成断言「成功也是对的」。改为**自己造一个空项目**（`project_create` + 断言 CONFLICT + 删掉）。

根治法：`scripts/accept_p5_matrix.py` 给每个脚本**复制一份独立验收库**（整库 copy + 只改 `storage.db_path`，
媒体/缓存/模型目录保持原样——库里存的是绝对路径）。改完重跑 27/27 全过。

### 未验证

- 干净 Windows 虚拟机（同 P5-05）。
- 打包版跑完整业务链路（下载→转录→导出）。
- 真实发布到 B 站（全程替身客户端）。
