## 这个版本解决什么问题

**三个「已经写在配置里、但产品里没人消费」的东西，现在真的动了；外加一条只在全量回归里露过一次的退出竞态。**

上一版（v0.9.3）交付时留了三条明账：服务端抽帧那条腿没做、帧没有磁盘缓存也没有按天数清理、抢救坏 JSON 要用户自己设环境变量。这一版把这三条收掉，并把改动推到了本机打包版与发布仓。

1. **坏 JSON 的抢救开关搬进设置页**。以前想让模型「修一下 JSON」得先 `set WB_SUMMARY_PARITY_STRICT=0` 再重启——那不是产品该有的样子，而且那个变量连输出预算一起放行，用户想要的往往只是前者。
2. **服务端抽帧腿接上**。以前只有本地 ffmpeg 一条腿：源视频没下载到本地就一张画面都没有。现在走 B 站的 `videoshot` 接口按 aid 取拼图、在 WB 侧裁格。
3. **帧有了磁盘缓存，并按「截帧保留天数」清理**。以前采完即删，同一个项目改一次稿就重抽一次；`storage.retention.frames_days` 这个键一直存在、界面也有输入框，但**产品里零消费者**。
4. **退出竞态**：连接关掉之后还来写库，不再把任务线程带着 traceback 打死。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/config.py` `LlmConfig` | 没有这个键 | 新增 `rescue_json: bool = False`；默认关 = 上一版的行为。它是 bili-scribe 不认识的扩展键，只落 `workbench.json`，共用 `config.yaml` 不被写脏 |
| `video_workbench/core/summarizer.py` `chat()` 严格档分支 | 单请求，坏 JSON 直接失败 | `_rescue_json_on(cfg)` 每次调用现读；开着且拿不到 JSON 对象才进 `_json_or_retry(..., rescue=True)`。**预算闸不跟着开** |
| 同上 `_json_or_retry` | 只服务宽松档 | 加 `rescue` 形参 + **重入护栏**：`if rescue and self.calls > 1` 就停手报错。离线实测没有这条护栏时同一份坏输出会滚到 **14 发** |
| 同上 `_strict_hint` | 单参，只教环境变量 | 收 `cfg`，三态：开关开着就不再教用户打开它；开关关着同时念出设置页路径与环境变量，**末句仍是 `WB_SUMMARY_PARITY_STRICT=0 后重试。`** |
| `video_workbench/core/frames.py` | 只有本地 ffmpeg 腿 | 新增服务端腿 `collect_from_api` + `fetch_sprite`（只放行 https、域名白名单 `hdslb.com`/`biliimg.com` 及其子域、不许 userinfo/端口/结尾点、不跟重定向、双重大小上限）+ 磁盘缓存 + `purge_stale_frames` |
| 同上 `_atomic_write_bytes` | 裸 `os.replace` | `_replace_into_place`：Windows 上两线程同时 replace 同一格会 `WinError 5`（实测 2/5），重试 8 次，仍失败且目标位已是等价完整内容就按成功返回 |
| `video_workbench/app/service.py` `_summary_frameset` | 只有本地腿 | 本地腿优先（源在盘时**零出网**）→ 本地失败降级服务端 → 两腿都坏 `return None` 只记日志，总结照跑 |
| 同上 开机段 | 只清 `cache/tmp` | 紧跟 `tmp_swept` 后接 `frames_swept = purge_stale_frames(cache_root/frames, frames_days)` |
| `video_workbench/infra/repo.py` | 关掉之后写库抛底层 `sqlite3.ProgrammingError` | 新增 `RepoClosed` / `close()` / `closed` / `_guard()`，三个读写口在锁内先问 |
| `video_workbench/app/jobs.py` | `_finish` 写库、`shutdown` 只置 cancel event | `_finish` 吞下 `RepoClosed` 并把「收尾没写进库」写进任务日志；`_report` 进度丢掉算了；`shutdown` 在 join 之前**杀掉在跑任务的子进程** |
| `web/js/pages/settings.js` / `mock.js` | 无 | llm 段新增 switch「坏 JSON 抢救」，绑 `llm.rescue_json`；mock 默认值与后端同形状 |
| `config.wb.example.yaml` | 只讲环境变量那条路 | 补 `rescue_json: false` 与六行说明，写清它与环境变量的唯一区别 |

## 验证

- 新增常驻判据 **53 条**：`tests/wb/test_summary_rescue_switch.py` 15、`test_frames_server_leg.py` 21、`test_frames_cache.py` 10、`test_repo_closed_on_exit.py` 7。现读 `... -m pytest 这四个文件 -q` → **53 passed**。
- 牙齿（每一条都在 `%TEMP%` 的插件副本里做，不碰工作区）：`_rescue_json_on` 强制 False ⇒ 6 failed / 9 passed；强制 True ⇒ 5 failed / 10 passed；`_validate_sprite_url` 恒等 ⇒ 7 个参数全红；`purge_stale_frames` 恒返回 0 ⇒ 2 failed；days 写死 7 ⇒ 2 failed；本地腿强制失败 ⇒ 那条「不出网」红；`_cached_picks` 恒 None ⇒ 缓存命中两条红；`WB_WEB_ROOT` 指向改坏的 settings.js ⇒ 2 failed；拆 `Repo._guard` ⇒ 4 failed；换 `RepoClosed` 类 ⇒ 3 failed；去掉 `_kill_processes` ⇒ 只红 shutdown 那条。
- 修掉一条**没有牙的既有判据**：`test_fetch_sprite_rejects_untrusted_urls` 原来只断「抛了 FrameError」，而 `fetch_sprite` 把网络层异常也包成 FrameError ⇒ 白名单被拆掉时照样绿。改成记账断言（一处请求都不许发出去）后才红。这是判据自身缺陷，不是产品缺陷。
- `ruff`：`check video_workbench tests/wb` → **All checks passed!**；`format --check video_workbench` → **59 files already formatted**；`format --check tests/wb` → **115 files already formatted**；`check scripts` → **Found 2 errors**（`scripts/verify_cover_fix.py` 两条有意的 E402）。
- 全量回归：`export PATH="/c/Program Files/Git/cmd:$PATH"; PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/wb -q` → **1775 passed, 1 skipped in 784.84s (0:13:04)**，0 failed，**上一版那条间歇性 warning 也消失了**（上一版基线 1768）。
- 冻结包：`scripts/build_wb.py` 重建 → `dist/VideoWorkbench/VideoWorkbench.exe` 24802723 B @ 2026-11-07 03:43，日志逐字 `[verify] 目录体积 413.3 MB，文件 2164 个` / `[verify] ✓ 打包结果可用`；`scripts/accept_frozen_package.py` → **合计 9/9 PASS**。
- 发布资产：`dist/VideoWorkbench-0.2.3.zip` **173159094 B / 1959 个文件**，`testzip` 无坏块，包内 exe 字节数与磁盘一致，凭据/数据库零泄漏。

## 验证到什么程度（未测/未做的，说清）

- **服务端腿对真接口跑过一次（发布后补，见下节），但只有一次**。六帧、一张拼图、100 格全部对得上，缓存命中也真的 0 次出网；不过样本是单条视频，没有覆盖「多张拼图」「无时间戳」「拼图尺寸非常规」这些分支——那些仍只有离线判据。
- **`fetch_sprite` 的大小上限与不跟重定向是自证式判据**：验的是我们自己的守卫代码，不是真服务器行为。
- **`rescue_json` 打开后的抢救阶梯没有对真模型跑过**。桩验的是「发了几次、带了什么参数」，不是「真模型被救回来」。
- **重入护栏的上限是「单轮」，不是「单卡」**：`self.calls > 1` 只拦同一次请求内的重入，跨请求仍各算一次。
- **开机清理只在夹具里跑过**：临时 `cache_root` 起了一次，读数 `frames_swept == 1`。真实用户目录下的第一次开机清理没有观测。
- **125%/150% 缩放下窗口下限仍未验，而且今天连 150% 都不在场**。`scripts/accept_window_min_size.py` 在本机现跑：四条全 PASS（`min_track == MIN_SIZE`、SetWindowPos 往小拽停在 1100×720、MoveWindow 同样、往大放行），落 `dpi=96 scale=1.0 显示器 DPI=96 注册表 Scale=None`。同日另跑两读数确认机器此刻就是 100%：`GetDpiForSystem()` = 96、`GetDpiForMonitor()` = [96,96]、注册表 `PerMonitorSettings` 里两块屏 `DpiValue` = `4294967294`（即 -2「未设置」）与 `0`。⇒ 早先「改成 150%」那次没落盘到系统，现在的状态是 100%。要验这一支只能真改显示器缩放后重跑同一条命令，脚本已按 DPI 折算（`scale = GetDpiForWindow/96`），不需要改代码。
- **`dist/` 不进 git**（`.gitignore:34`），本机包没有版本控制痕迹；要复原只能重跑 `scripts/build_wb.py`。

## 真接口探针（2026-11-07 04:0x，发布后补跑）

上一节写的「服务端腿只有离线判据」是本版交付时的实话。发布之后补了一次真跑，因为发现手头就有现成的 bvid 与登录态，成本只是几次出网请求（**不花模型调用**）。

探针 `<Temp>/wb_real_frames_probe.py`，产物 `<Temp>/wb_real_frames_probe.json`：

| 步骤 | 读数 |
|---|---|
| bvid → aid（走现有 Resolver） | `BV1VrGu6JEqk` → `aid=117017155733333`，`kind=collection` |
| `collect_from_api` | `frames=6`、`total_cells=100`、`sprites=1`、`has_timestamps=true`、`total_bytes=67533`、首帧 `timestamp=0` |
| 缓存落盘 | `<tmp>/frames 目录内`：`0.jpg 4.jpg 8.jpg 12.jpg 16.jpg 20.jpg meta.json`（格号与 `pick_cells` 一致） |
| 第二次同参采集 | `frames=6`、**`network_calls=0`**（缓存命中，一张图都没重下） |

⇒ 「按 aid 取拼图能拿到真图」这句话现在有端到端读数支撑了。

### 分支覆盖也补了一轮（同一天，三条视频）

| bvid / aid | 读数 | 覆盖到的分支 |
|---|---|---|
| `BV1TDGT6vEqC` / 117017155733333（191s） | 12 帧 / 100 格 / 1 张拼图 / 480×270 | 主路径 |
| `BV1dQtx66E9K` / 117187729688485（169s） | 12 帧 / 100 格 / 1 张拼图 / 480×270 | 主路径（换一条片子复现） |
| `BV1xx411c7mD` / **2**（2055s） | 12 帧 / **200 格 / 2 张拼图** / **160×90** | **多拼图 + 非常规格子尺寸** |

多拼图那条进一步核了格号跨拼图的落点：缓存里出现 `108 122 135 149` 这类 >100 的格号，说明第二张拼图真的被取用了（不是只裁第一张）；`orders` 1..12 连续、`timestamps` `0 165 325 514 704 904 1101 1310 1498 1723 1889 2054` 单调递增。

**仍没覆盖到的只剩一条**：`index` 为空（无时间戳）的分支。真接口今天给的两条片子 `index` 都非空（21 / 150 项），要构造它得找一条 videoshot 不返回时间表的稿件，成本比收益高，留在离线判据里。

### 抢救开关也补了真模型读数（同一天，6 次真实调用）

上一版留下的另一条「没验到的」是「抢救阶梯没对真模型跑过，桩只验次数与参数」。补跑用的是实配那条路（`WorkbenchConfig.load()` 读真家配置，`workbuddy/deepseek-v4.1-flash`、`max_tokens=32768`、`base_url=http://127.0.0.1:3425/v1` 当场 `/v1/models` = 200）。探针 `<Temp>/wb_real_rescue_probe.py` 与 `<Temp>/wb_off_probe.py`。

**总真实调用 = 6 次**（`calls=1` + `calls=5`），token 合计 `prompt 661 / completion 359`——刻意把 `max_tokens` 压到 60 / 40 制造截断，所以每次都短。

| 档位 | 读数 |
|---|---|
| 抢救关（默认） | `calls=1`，一次请求，正文原样交出。另一次把预算压到 40 时模型回了 `{"a":Void}`——读不出 dict、也**没有**续写或重问，因为默认档一次调用只发一次请求 |
| 抢救开 | `calls=5`，阶梯真跑：`输出被截断了，正在接着写` → `一次装不下，改成正文与时间线分两次要` → `模型输出不是合法 JSON，已按更严格的要求重问一次` → 再分批 → 最终 `LLM_BAD_RESPONSE`（`prompt 629 / completion 299`） |

两条结论都能落地：**默认档的「零额外请求」在真端点上成立**；**开关打开后阶梯真的会连发，且会正常收敛到失败而不是无限重试**（这次 5 发是单次 `chat()` 内四腿加主请求，没有跨轮重入）。

## 发布执行记录（2026-11-07 03:5x，lead 现跑）

| 环节 | 命令原文 | 读数 |
|---|---|---|
| 主仓 push | `git -c http.proxy= push origin main` | `ef4a313..8cb0afe  main -> main`；随后 `3c663c9`；`rev-list --left-right --count origin/main...HEAD` = `0 0` |
| tag | `git tag -a v0.9.4 -m "…"` | `git cat-file -t v0.9.4` = `tag`；`git rev-parse "v0.9.4^{commit}"` = `3c663c94e834d23eb260c97280f40d11b5185dab`（解引用这一腿不能省） |
| tag 修正 | 先打了指向 8cb0afe 的 tag，抬版本后 `git tag -d v0.9.4` 重打；远端 `push origin :refs/tags/v0.9.4` 后重推 | `ls-remote --tags origin` 现读 `refs/tags/v0.9.4` = `558ed11…`、`^{}` = `3c663c9…` |
| release | `gh release create v0.9.4 "dist/VideoWorkbench-0.2.3.zip" --title "VideoWorkbench 0.2.3（打包版）" --notes-file <Temp>/release_v094_body.md` | `REL_RC=0` → https://github.com/cunnak/bili-scribe/releases/tag/v0.9.4 ；`gh release list --limit 3` 首行 `VideoWorkbench 0.2.3（打包版） / Latest / v0.9.4` |

**本轮踩到的一条网络坑（写在这里给下一个人）**：本机 `.git/config` 里钉着 `http.proxy = http://127.0.0.1:7897`，而那个代理此刻 `curl` 读回 `000`（握手失败），于是 `git push` / `gh` 全部报 `schannel: failed to receive handshake`。同一时刻**不带代理直连 `api.github.com` 是 200**。解法是 `git -c http.proxy= -c https.proxy=` 与 `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy` 一起用（空值覆盖仓库级配置，环境变量则要显式 unset 因为空字符串仍会被读成代理地址）。别看到 handshake 失败就判「GitHub 挂了」。
