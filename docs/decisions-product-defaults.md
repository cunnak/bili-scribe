# 产品默认值与节奏：用户拍板记录

> 原名 `decisions-narrow-window.md`（只有窄窗一条时起的名字）。它现在装四条决定，
> 名字跟不上内容就是下一处过期口径，所以改名并把这一节摆在最前面。
> 四条：**窄窗支持性** / **镜像同步与它的节奏** / **失败详情写不写日志** / **画面帧默认值**。
> 读法：每条都带「用户原话 + 当场读数 + 我错在哪」，没有只写结论的条目。

## 窄窗这条：产品其实早就设了最小尺寸

> 立档时间 2026-11-05 23:5x。触发：用户在决策卡上点「窄窗：设最小尺寸」（动作 id `narrow-min`）。
> 这份文档的存在本身就是一条证据链——结论是被**现读推翻**的，不是被设计出来的。

## 已确定

1. **产品码里已经有最小窗口尺寸**，逐字现读自 `video_workbench/desktop.py`：

   ```
   52: WINDOW_SIZE = (1360, 860)
   53: MIN_SIZE = (1100, 720)
   ...
   357:        min_size=MIN_SIZE,
   ```

   `create_window` 单独抽出来的那行注释写着是为了让测试能不 `webview.start()` 就检查参数——**但全仓没有一条测试真去检查它**：
   `grep -rn "MIN_SIZE|WINDOW_SIZE|min_size" tests/wb/*.py` → **0 命中**。

2. ⇒ 前面几轮拿 **360 / 375 / 440** 三个视口取证得到的「正文只剩 92px」那种窗口，
   **在真产品里开不出来**（除非 `min_size` 没被 WebView2 后端真正遵守）。
   那三个数字来自 Playwright 直接 `set_viewport_size`，绕过了宿主窗口约束。

3. 布局本身也不给窄窗留余地（现读 `video_workbench/web/css/app.css`）：
   - 根布局 `68: grid-template-columns: var(--rail-w) 1fr`；折叠态 `79: var(--rail-w-collapsed)`。
   - 侧栏/列表列大量写死像素：`609: 168px minmax(0, 1fr)`、`672: 190px …`、`1293: 168px …`、`2055: minmax(0,1fr) 322px`。
   - 全仓 CSS **只有一条** `@media`：`app.css:498 @media (max-width: 1200px)`（还是给卡片折列用的，不是窄窗适配）。

4. `#skin-picker`（`app.css:702`）造成的 4px 横向溢出（`docSW 364 > 360`）**同样只在 360 视口上成立**，
   在 1100 最小宽度下不构成用户能看到的问题。⇒ 那条「越界面 `app.css`」的待办**降级为不处理**，
   除非第 5 条的验证结果相反。

## 暂未确定（这是唯一的真缺口）

5. **`min_size` 到底被不被 WebView2 后端强制**，我没验过。
   pywebview 各后端对这个参数的落实程度不一样；本机桌面壳是 pywebview + WebView2。
   判据很便宜：起真窗，UIA 读目标窗口的 `NativeWindowHandle`，量它的最小可得宽度
   （`mcp__wincu__windows_computer_use_*` 现成能做），或者直接把窗拖到极限再读一次窗口矩形宽。
   - 若**遵守** ⇒ 360/375/440 那批取证只在「开发期用浏览器开页面」时才有意义；
     task-68 的提示条修复仍然有价值（防长句在正常宽度里压住下一条），但**不需要再动 `app.css`**。
   - 若**不遵守** ⇒ 用户真能把窗收到 92px 正文，那 task-68 的修复是唯一防线，
     并且应当再加一条布局下限（`min-width` 或折叠侧栏），那才需要越界面 `app.css`。

## 下一步

- 开一张验证单（不碰产品码，只起真窗读数）：**`min_size` 在 WebView2 上是否被强制**。
  读数要求：命令原文 + 窗口矩形宽序列 + 结论一句。
- 把 `MIN_SIZE` 钉成常驻判据（现在 0 条测试守着它）：断言「建窗参数里带着非零下限」，
  **不钉死 1100/720 这两个字面量**（那是布局决定，会随第 5 条结果改；钉死数字＝下一个过期口径）。
- 本轮已交付的 `task-68`（一行收起 + 真按钮展开）**不回退**：它治的是「长文本压在下一条上」，
  这个形状在 1100 宽度里同样会发生（600 字符那条提示是产品自己拼出来的，与窗口宽无关）。

## 我在这件事上错在哪（写下来防重犯）

- 我把取证视口（360/375/440）当成了产品视口，据此推导「要越界面改 `app.css`」，
  还把它写进了给用户的决策卡。**根布局的最小宽度约束一直就在 `desktop.py:53`，我三轮没去读它。**
- 同族的错本轮已经犯过三次（把旧快照当现状、把别人的收回理论当事实、把算出来的封顶当产品值），
  共同形状是：**没读那一个真正决定行为的文件，就去判断行为**。
  ⇒ 判「这个场景用户能不能遇到」之前，先读入口参数，再读取证手段是不是绕过了它。

## 补记（2026-11-06 00:0x）：镜像同步入口把自己锁在外面了

用户随后拍了「日志：接全句」（动作 id `log-full`），我去把发布镜像追平主仓时撞上第二条：

- `scripts/sync_mirror.py` 的 `--commit` **要求另给 `-m`**，而它在缺 `-m` 时**已经先把文件复制完、
  也先把 gitlink 对齐了**，最后才打印 `[x] --commit 需要 -m`（源码：`if not args.message.strip()` 在
  `apply_sync` 之后的提交分支里）。
- ⇒ 镜像停在「5 个文件已按 HEAD 写好 + gitlink 已动，但一笔都没 commit」的脏状态。
- **再跑一次入口，它自己的 `preflight()` 拒绝同步**：「镜像工作区不干净（6 行），先弄清是谁的在途」。
  也就是说：**这个工具能进入一个它自己不接受的状态**，而且没有任何恢复入口——
  只能靠人在镜像里手工 commit，而 AGENTS.md 第六节写的恰恰是「同步只准走这个入口」。

当场处置（都是入口本该自己做的事，我手工替它做完）：
- 手工核对镜像那 6 行**全部等于主仓 HEAD 版**（逐字节 `cmp`：`requirements-dev.txt`、
  `scripts/sync_mirror.py`、`tests/wb/test_summary_parity.py`、`tests/wb/test_summary_error_display.py`
  回显逐行 SAME-HEAD：`AGENTS.md`、`requirements-dev.txt`、`scripts/sync_mirror.py`、
  `tests/wb/test_summary_error_display.py`、`tests/wb/test_summary_parity.py` 五份；
  gitlink 现场是 `+78ca5bb` 而镜像 HEAD 仍记 `4756382`），
  然后按显式 pathspec 提交、抬 submodule 指针、`push`。

要修的两件事（归 lead，已落 task-73）：
1. **参数校验前移**：`--commit` 缺 `-m` 必须在**任何写入之前**失败，不许先复制再报错。
2. **给自己一个恢复入口**：当脏文件恰好等于「主仓 HEAD 版 + 已对齐的 gitlink」时，
   这就是上一次中断留下的现场，应当认出它、报 `[resume]` 并允许提交，而不是把人堵死。

顺带记两条同轮现读：
- 镜像 `bili_scribe/config.py:57` 经 junction 读出 `max_tokens: int = 32768` ⇒ 上一轮「装镜像的人拿到的
  默认值还是 4096」那个 bug，这次是真的对齐了（gitlink 从 `d823ee9` 一路抬到 `78ca5bb`）。
- 镜像形状锁在镜像里实测 **6 passed in 2.73s**（`tests/wb/test_junction_content_not_tracked.py`）。
## 再补一条（2026-11-06 00:1x）：镜像节奏由用户定了——等 task-63 一起走

用户动作 id `sync-later`，原文「等 task-63 落地一起走」。

**先认一句**：我在他点这个按钮**之前**已经把镜像追平了一次（`3dbcf7a`，两边 `0 0`，远端回显在上一条里）。
所以这不是「照指令去做」，是「他已经抢先做完了」——写清楚，免得下一轮有人以为镜像还旧着。

这条决定真正改变的是**下一次同步的口径**：

- task-63 落地的那一笔会同时带 `video_workbench/app/jobs.py` 与新判据
  `tests/wb/test_job_failure_log.py`。在那之前**不再跑 `sync_mirror.py --apply`**，
  免得镜像出现「有锁没有码」或「有码没有锁」的半截状态——本仓库的 `tests/wb` 是要
  原样搬到镜像里跑的（今天还在镜像里实测过 6 passed），半截搬过去就是让门面跑一套
  主仓没跑过的组合。
- task-73（修同步入口自锁）归我，它**改的是入口自身**：在它落地前，同步仍然只能用
  现在这个「缺 `-m` 会先写完才报错」的版本 ⇒ 每次跑必须**带 `-m`**，别再把镜像停成脏态。
  这条临时口径写在这里，等 task-73 落地即作废。

## 再补一条（2026-11-06 00:2x）：画面帧——默认**保持开**

用户动作 id `frames-on`，原文「默认保持开」。

这条决定的字面含义是**不改任何默认值**，这一点必须写清楚，否则下一轮会有人去"实现"一个
本来就不存在的改动：

- 代码默认：`bili_scribe/config.py:107 enabled: bool = True`（`max_frames = 12`、`quality = 85`）。
- 三家实配现状（我 00:1x 现读）：`~/.video-workbench/config.yaml:68`、`~/.bili-scribe/config.yaml:64`、
  仓库根 `config.yaml:68` 全部 `enabled: true` / `max_frames: 12` / `quality: 85`。
- ⇒ **task-70 的落地不含"翻默认值"这一步**；它要做的是把 `collect_frames` 从 bs 那条路
  **接进 WB 的总结链路**（今天 WB 侧 0 个调用点），也就是**兑现一个已经写在这三份配置里、
  但一直没人执行的开关**。

三个连带后果，现在就得记账，不要等实现完再发现：

1. **成本对所有人生效**：一次总结多约 1,464 image tokens（这个数出自
   `bili_scribe/config.py:95 class FramesConfig` 自己的注释「12 帧 ≈ 1,464 image_tokens，
   约为一条 4 小时视频总输入的 1.7%」）。不是只影响新用户——三家实配已经是 `true`，
   接上那天起老用户同样开始付。
2. **对账锁一定会红，这是好事不是回归**。parity 已现读确认：
   `tests/wb/test_summary_parity.py:406 EFFECTIVE_FIELDS = ("json_mode","temperature","max_tokens","messages")`
   与 `:499 assert wm["content"] == bm["content"]` 是**逐字**比 ⇒ 一侧接帧（`content` 从 `str`
   变 `list`）当场红。他另外在 `:411-419 PLAN_A_NOTE` 旁加了一条**前提锁**，钉
   「适配层记录的每条请求 `content` 都是 `str`」（`grep -rn "frameset\|collect_frames" tests/wb/*.py` 之前是 0）。
   ⇒ 接上那天由 **parity** 摘锁并显式扩口径（候选甲），或者帧走独立字段不进比对面（候选乙）；
   **这条只能我拍，实现层不许自己选**。
3. **界面那句 help 会从谎话变成真话**：`video_workbench/web/js/pages/settings.js:150` 现在写着
   「给总结配图用；关掉可明显提速。」——在 WB 里这个开关今天**没有任何消费者**，那句"提速"
   是给用户看的空承诺。接上之后它才第一次成立。⇒ 这条不构成新的改动，但**验收时要贴一次
   关掉开关的实测对比**（帧确实不发、正文确实变短），证明那个旋钮现在是活的。

不许动的东西（写死在这里防止"顺手改"）：

- 副本 `video_workbench/core/summary/summarizer.py:550`
  （`frame_blocks = frames_mod.as_content_blocks(frameset) if frameset else []`）**一行不许改**——
  撞漂移锁；接帧只能接在适配层 `video_workbench/core/summarizer.py`（`:906-908` run、
  `:990-998` refine 这两处现在都不写 `frameset=`）。
- 六份提示词真源 txt 与三份 vendored `.py` 的字节；`bili_scribe/**`（含 `config.py:107` 那个默认值，
  用户选的是"保持"，不是"去 bs 里改成 False"）。
- `Pillow 13.0.0` 已在场，抽帧不需要新依赖；真模型调用在 task-70 阶段 1 **禁止**（`calls=0`）。

### 落地记录（2026-11-06 17:4x，lead 现读）：阶段 1 已接完，走的是**本地 ffmpeg 腿**

上面那三条「等用户拍」的口子，实现按下面这样落了；**如果用户要的是另一条腿，改动面只在服务层那一个函数**（见「还没验的」）。

1. **采集腿 = 本地 ffmpeg**，新文件 `video_workbench/core/frames.py`（`collect` / `frame_offsets` /
   `quality_to_qv` / `FRAME_WIDTH = 480`）。选它不是偏好，是实测：`wb_project.aid` 32 行全空
   （`bvid` 只有 24 行有值）⇒ bs 那条 `x/player/videoshot` 服务端腿**当前 0/32 可用**，要先补一遍
   bvid→aid；而 `bili_scribe/infra/bili_client.py` 的 `get_bytes(url)` 是裸 `session.get`——跟重定向、
   无 host 白名单、无大小上限，url 又来自外部接口，WB 不该继承这个面。本地腿实测 24/32 条源视频仍在盘。
   **数据类仍复用 bs**（`Frame` / `FrameSet` / `FrameError` / `as_content_blocks` 全是纯数据），
   所以副本那句 `as_content_blocks(frameset)` 一个字都不用改。
2. **接线位置**：适配层 `video_workbench/core/summarizer.py` 的 `run`/`refine` 各加一个可选 `frameset=None`
   并透传给副本；服务层 `WorkbenchService._summary_frameset()` 每次生成与修订都现抽一组（refine 不复用
   上一版那次采集，否则只有画面才有的信息会悄悄丢）。三条降级路径（开关关 / 无源视频 / `FrameError`）
   全部是「log 一句 + 返回 None」，**总结照跑**，与 bs 同口径。
3. **对账锁没被摘、口径没被扩**（候选乙成立）：`EFFECTIVE_FIELDS` 仍是
   `(json_mode, temperature, max_tokens, messages)`，`_normalized()` 里那条 `isinstance(content, str)`
   守卫原样保留。`tests/wb/test_summary_parity.py` 只把 `FRAMES_PRECONDITION_NOTE` 的**说明**从
   「今天 0 个调用点」改成「本文件的用例一律不传 `frameset`；带帧那条路的判据在
   `tests/wb/test_summary_frames.py`」。⇒ 一句话：**没传帧时两侧仍逐请求一致，传了帧由另一个文件管**。
4. **补救腿单独降回纯文本**（`_text_only`）：四条补救腿（重问 / 续写 / 分批 / 补时间线）都是
   `list(messages) + [...]` 的形状，不降就会把同一批图再传一遍（12 帧 ≈ 1,464 image_tokens，四遍 ≈ 5.8k 白烧）。
   画面只对「这次讲了什么」有用，补救腿要的是 JSON 形状——所以主请求带帧、补救腿不带。
5. **界面那句 help 改掉了**：`settings.js` 里「给总结配图用；关掉可明显提速。」换成
   「总结时附上画面：PPT、图表、代码里的字能进正文。关掉省图片 token，也少一次本地抽帧。」，
   `max_frames` 也补了一句「帧会作为图片发给模型，越多越贵」。
6. **判据**：`tests/wb/test_summary_frames.py` 13 条（本地腿 5 / 适配层 3 / 服务层 4 / 界面 1），控制组
   `13 passed`。牙齿 8 个变体全部**有牙**（脚本 `C:/Users/b1596/AppData/Local/Temp/wb_teeth_t70.py`，
   产物同目录 `wb_teeth_t70.json`；只在 Temp 副本里改坏，工作区零覆盖回写）：T1 run 不透帧、T2 refine 不透帧、
   T3 `_text_only` 退化、T4a/b/c 三条降级各自改成抛、T5 文案退回旧话、T6 清理删父目录。
   真模型调用 `calls=0`——全部走桩。

还没验的（等拍板或等放行）：

- 真实一次带帧总结的 token 差与正文质量变化。预估：`wengao` 三轮 + 12 帧 ⇒ 现有输入 +约 1.7%，
  在 `max_tokens=32768` 预算内；按预算闸这条要 lead 拿去问用户才烧。
- **服务端 aid 腿要不要补**：现在完全没接。要接，入口就是 `_summary_frameset` 一个函数，
  本地腿那 5 条判据按同形状再加一组服务端用例；补之前得先解决 bvid→aid 与 `get_bytes` 的白名单。
- 帧的磁盘缓存与 `purge_stale` 生命周期（今天采完即删，只留内存里的 data URL）。
