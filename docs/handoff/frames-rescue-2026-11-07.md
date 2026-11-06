## 这一轮解决什么问题

**三件「已经写在配置里、但产品里没人消费」的事，和一件「以前只能靠环境变量」的事。**

上一版（v0.9.3）交付时留了三条明账：服务端抽帧那条腿没做、帧没有磁盘缓存也没有按天数清理、抢救坏 JSON 要用户自己设环境变量。这一轮把这三条收掉，并把改动推到了本机的打包版。

1. **坏 JSON 的抢救开关搬进设置页**。以前想让模型「修一下 JSON」得先 `set WB_SUMMARY_PARITY_STRICT=0` 再重启——那不是产品该有的样子，而且那个变量连输出预算一起放行，用户想要的往往只是前者。
2. **服务端抽帧腿接上**。以前只有本地 ffmpeg 一条腿：源视频没下载到本地就一张画面都没有。现在走 B 站的 `videoshot` 接口按 aid 取拼图、在 WB 侧裁格。
3. **帧有了磁盘缓存，并按「截帧保留天数」清理**。以前采完即删，同一个项目改一次稿就重抽一次；`storage.retention.frames_days` 这个键一直存在、界面也有输入框，但**产品里零消费者**。
4. **打包版重建并九条机械验收**。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/config.py` `LlmConfig` | 没有这个键 | 新增 `rescue_json: bool = False`；默认关 = 今天的对账档行为。它是 bili-scribe 不认识的扩展键，只落 `workbench.json`，共用 `config.yaml` 不被写脏 |
| `video_workbench/core/summarizer.py` `chat()` 严格档分支 | 单请求，坏 JSON 直接失败 | `_rescue_json_on(cfg)` 每次调用现读；开着且拿不到 JSON 对象才进 `_json_or_retry(..., rescue=True)`。**预算闸不跟着开**（`_json_budget` 严格档仍原样返回入参） |
| 同上 `_json_or_retry` | 只服务宽松档 | 加 `rescue` 形参 + **重入护栏**：`if rescue and self.calls > 1` 就停手报错。离线实测没有这条护栏时同一份坏输出会滚到 **14 发**（分批/续写回流到 `chat` 再进阶梯），击穿「单卡 3 次真实调用」的口径 |
| 同上 `_strict_hint` | 单参，只教环境变量 | 收 `cfg`，三态：开关开着就不再教用户打开它；开关关着同时念出设置页路径与环境变量，**末句仍是 `WB_SUMMARY_PARITY_STRICT=0 后重试。`**（既有闸钉死语序） |
| `video_workbench/core/frames.py` | 只有本地 ffmpeg 腿 | 新增服务端腿 `collect_from_api`（`videoshot` → 裁单格 → `Frame`）+ `fetch_sprite`（**只放行 https、域名白名单 `hdslb.com`/`biliimg.com` 及其子域、不许 userinfo/端口/结尾点、不跟重定向、Content-Length 与流式双重大小上限**）+ 磁盘缓存 `<cache_root>/frames/<bvid 或 aid>/`（`meta.json` + 逐格 jpg，命中要求 max_frames/quality 一致）+ `purge_stale_frames` |
| 同上 `_atomic_write_bytes` | 裸 `os.replace` | 新增 `_replace_into_place`：Windows 上两个线程同时 replace 同一格会 `WinError 5`（实测 2/5 概率），重试 8 次，仍失败且目标位已是等价完整内容就按成功返回 |
| `video_workbench/app/service.py` `_summary_frameset` | 只有本地腿 | 本地腿优先（**零回归：源在盘时不出网**）；本地失败才降级服务端；aid 从 bvid 走**现有 Resolver**，不新写解析；两腿都坏 `return None` 只记日志，总结照跑 |
| 同上 开机段 | 只清 `cache/tmp` | 紧跟 `tmp_swept` 后接 `frames_swept = purge_stale_frames(cache_root/frames, frames_days)`；`frames_days <= 0` = 不清理 |
| `web/js/pages/settings.js` / `mock.js` | 无 | llm 段新增 switch「坏 JSON 抢救」，绑 `llm.rescue_json`；mock 默认值与后端同形状、默认 `false` |
| `config.wb.example.yaml` | 只讲环境变量那条路 | 补 `rescue_json: false` 与六行说明，并写清它与环境变量的**唯一区别**：前者只放行修 JSON，后者连预算兜底一起放行 |

## 验证

- 新增常驻判据 **46 条**：`tests/wb/test_summary_rescue_switch.py` 15、`test_frames_server_leg.py` 21、`test_frames_cache.py` 10。现读命令 `... -m pytest tests/wb/test_summary_rescue_switch.py tests/wb/test_frames_server_leg.py tests/wb/test_frames_cache.py -q` → **46 passed in 1.60s**（分文件 15 / 21 / 10）。
- 牙齿（每一条都在 `%TEMP%` 的插件副本里做，不碰工作区）：
  - 把 `_rescue_json_on` 强制为 `False` ⇒ `test_summary_rescue_switch.py` **6 failed / 9 passed**。
  - 强制为 `True` ⇒ **5 failed / 10 passed**（默认关那几条站得住）。
  - 把 `_validate_sprite_url` 换成恒等函数 ⇒ `test_fetch_sprite_rejects_untrusted_urls` **7 个参数全红**。
  - 把 `purge_stale_frames` 换成 `lambda root, days: 0` ⇒ **2 failed**（含新加的接线那条）。
  - 把 `days` 写死成 7 ⇒ **2 failed**（`frames_days=0` 那两条）。
  - 强制本地腿失败 ⇒ `test_service_prefers_local_leg_and_never_touches_the_network` 红。
  - 让 `_cached_picks` 恒返回 `None` ⇒ 缓存命中两条红。
  - 用 `WB_WEB_ROOT` 指向改坏 `settings.js` 的副本（把 `llm.rescue_json` 换成别的键）⇒ **2 failed**。
- 修掉一条**没有牙的既有判据**：`test_fetch_sprite_rejects_untrusted_urls` 原来只断「抛了 FrameError」，而 `fetch_sprite` 把网络层异常也包成 FrameError ⇒ 白名单被拆掉时它照样绿（实测 21 passed）。改成记账断言（`calls == []`，一处请求都不许发出去），重跑牙齿才红。这条是**判据自身缺陷**，不是产品缺陷。
- `ruff`（task-71 口径）：`check video_workbench tests/wb` → **All checks passed!**；`format --check video_workbench` → **59 files already formatted**；`format --check tests/wb` → **115 files already formatted**；`check scripts` → **Found 2 errors**，即 `scripts/verify_cover_fix.py` 的两条 E402（`sys.path.insert` 之后的 import，挪了会真炸，有意保留）。
- 打包版：本机包用 `scripts/build_wb.py` 重建（`dist/VideoWorkbench/VideoWorkbench.exe` 2026-11-07 02:36:35、24800227 B；构建日志逐字 `[verify] 目录体积 413.3 MB，文件 2164 个` / `[verify] ✓ 打包结果可用`），随后 `scripts/accept_frozen_package.py` → **合计 9/9 PASS**（六个提示词真源字节一致；包内 settings.js 是带新开关那一代；`video_workbench.core.frames` 在 `PYZ-00.toc` 里；`--check` rc=0 ok=True；包内版本 == 仓库版本 0.2.2；`--roles` rc=0 六把角色；三把退役角色仍在名单；**验收前后三份真实配置 sha 一字未动**）。
- 全量回归：`export PATH="/c/Program Files/Git/cmd:$PATH"; PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/wb -q` → **1768 passed, 1 skipped in 782.75s (0:13:02)**，0 failed（上一版基线 1766，本轮净增 2 条接线判据）。**PATH 前置那条不能省**：`ops_bash` 默认的 git 是 niux 自带的 2.56.0 niugit，系统配置里没有 `core.autocrlf=true`，`test_git_no_normalization_on_locked_paths.py` 那条会正确地报红（实测 1 failed / 1765 passed），换成 Git for Windows 后同一条命令全绿。
- 示例配置可加载：把 `config.wb.example.yaml` 复制成临时家的 `config.yaml` 后 `WorkbenchConfig.load()` → `rescue_json=False`、`frames_days=7`，`foreign_keys` 里能看到 `llm.rescue_json` 与 `storage.retention`（说明它们确实只落 `workbench.json`）。

## 验证到什么程度（未测/未做的，说清）

- **服务端腿没有对真接口跑过**。全部判据都是离线的：`gateway` 是假对象、`fetch_bytes` 是假函数。真跑一次要登录态 + 真 aid + 真拼图，会花掉一次真实出网请求，本轮没有预算也没有账号。⇒ 「按 aid 取拼图能拿到真图」这件事**只有代码与形状判据支撑**，没有端到端读数。
- **`fetch_sprite` 的大小上限与不跟重定向是自证式判据**：判据用假 `requests` 模块，验的是我们自己的守卫代码，不是真服务器行为。
- **开机清理只在夹具里跑过**：`WorkbenchService` 用临时 `cache_root` 起了一次，读数 `frames_swept == 1`。真实用户目录下的第一次开机清理没有观测。
- **`rescue_json` 打开后的抢救阶梯没有对真模型跑过**。判据里 `client` 是记账桩，验的是「发了几次、带了什么参数」，不是「真模型被救回来」。上一版 v0.9.3 已记过同一份坏 raw 能被 `jsonfix` 零成本还原，但那不等于端点这一轮会照做。
- **重入护栏的上限是「单轮」，不是「单卡」**：`self.calls > 1` 只拦同一次请求内的重入。跨请求（下一轮生成/修订）仍然各算一次，与「单卡 3 次真实调用」口径的关系需要在真跑时重新核。
- **`dist/` 不进 git**（`.gitignore:34`），所以这一版的本机包没有任何版本控制痕迹；要复原只能重跑 `scripts/build_wb.py`。
- **没推 GitHub**：本轮用户只要求推到本机 `dist/VideoWorkbench/`，没有 tag、没有 release、没有镜像同步。`video_workbench/__init__.py` 仍是 `0.2.2`。
