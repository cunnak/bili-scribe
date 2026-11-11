# P4 合并与发布 · 验收记录

范围：P4-01 合并页 UI（来源列表 / 有序列表 / 输出设置 / 预检表）。
证据：`ark-merge.png`、`ark-merge-diff.png`（exhibition 同名的两张）。

## 一、P4-01 合并页 UI

### 跑法

```bash
node tests/wb/frontend_test.js
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/wb/test_merger.py -q
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/shoot_wb.py --skin ark --out docs/acceptance/p4 --only merge,merge-diff
```

### 断言表（39 项前端测试中的第 39 项 + 20 项后端单测）

| # | 断言 | 实测 |
|---|---|---|
| 1 | 空列表时有空状态、`开始合并` 不可点 | `.merge-empty` 存在，button.disabled=true |
| 2 | 来源列表同时给切片成片与素材库源片 | 5 个来源（切片 2 + 源片 3） |
| 3 | 点来源加入有序列表，序号从 1 开始 | 加两条 → 两行，序号 1/2 |
| 4 | 重复点击不重复加入并提示 | 「已经在列表里了」 |
| 5 | 上移一格把当前条与上一条互换 | names1[0]==names0[1] 且 names1[1]==names0[0] |
| 6 | 下移一格换回原位 | 顺序复原 |
| 7 | 反转把整个顺序倒过来 | 逆序，行数不变 |
| 8 | 删除单行后序号重排 | 剩 2 行，序号 1/2 |
| 9 | 清空回空状态且按钮禁用 | 0 行 + `.merge-empty` |
| 10 | 同规格时预检写「可以：参数一致」 | 「可以：参数一致，只封装不重编码」 |
| 11 | 混入 720p 源片出现差异表 | `.merge-diff` 含「帧率」「高度」 |
| 12 | 差异出现后快速拼接 radio 禁用并写原因 | disabled=true，说明「规格不一致或文件不够，快速拼接不可用」 |
| 13 | 移除差异文件后恢复可快速拼接 | 回到 can_quick |
| 14 | 预检表五项：文件数/总时长/总素材体积/统一规格/能否快速拼接 | 2 个可用 / 0:40 / 4.6 MB / 1920×1080 · 30fps / 可以 |
| 15 | 输出目录默认落在 merge/，文件名带段数与时间戳 | `…/export/merge`、`合并_2段_20261030-170000.mp4` |
| 16 | 分辨率/帧率默认「跟随第一个」 | 两个 select 均为空值选项 |
| 17 | 后端 `merge.sources` 列出两类来源 | clips 39 / library 7（真库 `build/accept/p2.yaml`） |
| 18 | 后端 `merge.preflight` 两条同规格 → can_quick | count=2、total 2:00、无差异 |
| 19 | 空列表 preflight 报 VALIDATION | 「先往列表里加片段」 |
| 20 | 预检尺寸/帧率参数覆盖默认 | 显式 1920×1080/30 时 target 随之变 |

### 本轮修掉的问题

1. **上移断言写反**：原来断言「上移后第一条不变」，实际应互换——测试先记 `names0`，
   移完比 `names1[0] === names0[1]`，再下移复位。
2. **mock 里规格差异不出现**：`mergeSpecOf()` 只认 `short_60s`，而素材库行是
   `source.mp4`（720p 源片）。改成「文件名含 short_60s 或以后 10 个字符是 source.mp4」，
   并用 `slice(-10)` 判断而不是正则——bash heredoc 会把 `[\/]` 里的反斜杠吃掉。
3. **`repo.project_create` 必须给 `root_dir`**：单测里漏传 → `KeyError: 'root_dir'`。

### 仍未验证

1. 真实素材库里「同规格」的样本只有 2 条（短样片）；多段长片拼接的预检耗时未测。
2. `merge.sources` 一次列 500 个项目内的资产，项目更多时的分页没做。
3. 输出目录默认取「第一个可用文件的父目录」，没做磁盘剩余空间检查（守卫在 P4-03 合并
   提交时才算）。
4. 截图里输出目录看着像双反斜杠——**已用 CDP 实测排除**：`JSON.stringify` 得到
   `"C:\mock\export\merge"` 即运行时单反斜杠，是等宽字体对 `\` 的渲染视觉，
   不是数据 bug。

## 一之二、输出目录可改 + 打开输出目录（2026-11-07 新增）

范围：合并页右栏「输出目录」从只读输入框改成按钮；旁边新增「打开输出目录」。

### 跑法

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/wb/test_merge_output_dir.py -q
node tests/wb/frontend_test.js
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -u Temp/accept_merge_outdir.py
```

### 断言表（后端 15 项 + 前端 6 项 + 真 ffmpeg 1 项）

| # | 断言 | 实测 |
|---|---|---|
| 1 | 选文件夹会记住这个目录 | `fs.pick {kind:"dir"}` 之后 `_picked_dirs` 有它 |
| 2 | 选文件不记（那是素材，不是写盘目标） | `kind:"file"` 之后名单不变 |
| 3 | 名单最多 32 个，超出丢最旧 | 第 33 个进来后长度 32、头部是第 2 个 |
| 4 | 手填工作区外的目录被拒 | `VALIDATION` + 「选择输出目录」 |
| 5 | 对话框选过的外部目录可以用 | `merge.start` 提交成功 |
| 6 | 把文件当目录用被拒 | 「指向的是一个文件，不是文件夹」 |
| 7 | 不存在的盘符报 VALIDATION 而不是 INTERNAL | `Z:\definitely\missing` → `VALIDATION` |
| 8 | 相对路径被拒 | 「输出目录要给绝对路径」 |
| 9 | 默认目录（没手选）会被建出来 | `<media>/merge/` 存在 |
| 10 | 打开输出目录按 merge_id 定位到成片父目录 | `{"path": ...}` == 成片 `.parent` |
| 11 | 未知 merge_id 报 NOT_FOUND | 「找不到这次合并记录」 |
| 12 | 空 merge_id 报 VALIDATION | 「要先有一次合并」 |
| 13 | 接口不收路径（`fs.reveal` 白名单不放开） | dispatch 传多余 `path` 键被忽略 |
| 14 | 成片和父目录都不在时报 NOT_FOUND | 「成片和它所在的目录都不在了」 |
| 15 | `merge.reveal_output` 已注册到路由 | `app.bootstrap` 的 rpc 名单里有 |
| 16 | 路径按钮是 BUTTON、空态写「选择输出目录」 | `tagName=BUTTON`、文案一致 |
| 17 | 点按钮按 `kind:"dir"` 要路径，选完显示完整路径 | `fs.pick` 收到 `{kind:"dir"}`、`title` == 路径 |
| 18 | 手选之后预检不再把目录盖回默认 | 再加一段，仍显示手选目录 |
| 19 | 取消选择不改原值、不报错 | 仍是原目录，错误条为空 |
| 20 | 合并成功前「打开输出目录」禁用，成功后可用且只传 merge_id | 参数里没有 `path` 键 |
| 21 | 打开失败有可见错误，不静默吞 | `.merge-flash--error` 含「不在了」 |
| 22 | 清空列表把输出目录与打开按钮一起复位 | 回占位文案、`outDirManual=false`、按钮禁用 |
| 23 | 切页再回来手选目录还在（会话内保留） | 去 library 再回 merge 仍是原路径 |
| 24 | 真 ffmpeg：手选外部目录能出片，reveal 指到它 | 12,318,915 B / 30.700s / 720×1280 20fps 48kHz 2ch |

第 24 项现场读数（`Temp/accept_merge_outdir.py`，2 个真实 flv）：

```
picked_dirs ['c:\\未明子录播\\wb_out_test']
start ok, job_942e6c0643b9, merge_id mg_54b7d4c5754f
job status succeeded
result path C:\未明子录播\wb_out_test\outdir_probe.mp4, bytes 12318915, duration_ms 30700
reveal {"path": "C:\\未明子录播\\wb_out_test"}
```

### 顺带修掉的问题

1. **`merge.start` 收任意 `out_dir` 不校验**（改动前是 `Path(out_dir)` 直接用）。
   现在四条判据：绝对路径 / 非设备路径 / 是已存在目录 / 在工作区内或被对话框选过。
2. **默认目录没人建**。`ensure_free_space` 会往上找已存在祖先，但 ffmpeg 自己不会建父目录，
   快速拼接要到收尾才失败。现在 `merge_start` 里 `mkdir(parents=True, exist_ok=True)`。
3. **盘符不存在时抛 INTERNAL**。`shutil.disk_usage` 的 `FileNotFoundError` 现在转成 `IO`。
4. **`fs_pick` 的 kind 未归一化**：`"DIR"` / 带空格会走进非 dir 分支。现在 `strip().lower()`。

### 第 25 项：真窗口里的原生对话框（2026-11-07 补）

上面 24 项里，前端第 17 项用的是注入的假 picker——它证明「点按钮会去要路径」，
证明不了「弹出来的是文件夹框而不是文件框」。历史缺陷正好出在这一层（合并页
`kind:"files"` 掉进 FOLDER_DIALOG 分支），所以补一条真窗口腿。

常驻脚本 `scripts/accept_folder_picker.py`：起一扇真 pywebview 窗，在线程里调
产品自己的 `_make_picker()`，另一个线程操作那个 `#32770` 原生窗（`WM_SETTEXT`
填编辑框、`BM_CLICK` 点按钮），再比对返回值。读数落 `%TEMP%/wb-picker-accept.json`。

现读读数（`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -u scripts/accept_folder_picker.py`，exit 0）：

| 腿 | 传的 kind | 弹出窗口标题 | 返回值 |
|---|---|---|---|
| dir | `dir` | 选择文件夹 | `…\wb-picker-accept-fixture\out` |
| file | `file` | 打开 | `…\wb-picker-accept-fixture\a.mp4` |
| files | `files` | 打开 | `…\a.mp4` + `|` + `…\b.mp4` |

三条 `ok` 全 true。顺带钉住一个坑：对话框里含 "Edit" 字样的子窗有两个——地址栏是
`ToolbarWindow32`、搜索框是 `SearchEditBoxWrapperClass`，**只有类名恰好 `Edit` 的那个**
才是「文件夹:」栏。按子串匹配会填进搜索框：填完读回来是自己写的值，但对话框实际选中项没变，
返回的仍是上一次的目录（第一版探针就是这么假绿的）。

### 仍未验证

- 输出目录在网络盘 / 只读盘上的表现。
- 一次合并写多个目录（不支持，一次只有一个输出目录）。
- 真窗口里「添加本地文件」多选之后合并页的顺序表内容（第 25 项验到 picker 的返回字符串
  形状为止，页面侧由前端第 1 条用例覆盖）。

## 二、P4-02 预检与快速拼接（真 ffmpeg 验收）

### 跑法

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_merge.py --config build/accept/p2.yaml
```

脚本现场从 `build/accept/clips/short_60s.mp4` 用**重编码**切三段（1280×720 / 30fps，
每段 4 秒），再复制一份并改成 1920×1080 当「规格不一致」的对照组——用 `-c copy`
切会让关键帧对齐漂移，段长就不准了。

### 断言表（21 项，实测值见括号）

| # | 断言 | 实测 |
| --- | --- | --- |
| 1 | 预检认得出三条可用 | `count=3` |
| 2 | 同规格可以快速拼接 | `can_quick=True`，`differences=[]` |
| 3 | 总时长约 12 秒 | `12000ms` |
| 4 | 提交后拿到任务 | `job_...` |
| 5 | 任务成功 | `succeeded` |
| 6 | 成片存在 | 路径在 `export/` 下 |
| 7 | 成片有体积 | > 0 B |
| 8 | 成片时长等于各段之和（误差 < 500ms） | 12021ms vs 12000ms，差 21ms |
| 9 | 成片分辨率跟随源 | `1280x720` |
| 10 | 成片保留音轨 | `aac` |
| 11 | 合并记录已入库 | 有行 |
| 12 | 记录状态 succeeded | `succeeded` |
| 13 | 记录里存了三条素材 | 3 |
| 14 | 不同规格被拦下 | `CONFLICT` |
| 15 | 理由说清是规格不一致 | 「规格不一致…」 |
| 16 | 差异表点名具体字段 | `['height','width']` |
| 17 | 一条时拒绝并说清要两条 | `CONFLICT` + 「至少」 |
| 18 | 文件不在能点名 | 「…不在了」 |
| 19 | 空列表报 VALIDATION | `VALIDATION` |
| 20 | 精确模式诚实地说还没做 | `UNSUPPORTED` + 「P4-03」 |
| 21 | 同名再合并不会覆盖上一条 | `… (2).mp4` |

单测另计：`tests/wb/test_merge_quick.py` 19 项（转义、清单、命令顺序、
`block_reason` 三分支、服务层 11 项）、`tests/wb/test_merger.py` 20 项、
`tests/wb/test_repo_concurrency.py` 1 项。

### 本轮修掉的问题

1. **`escape_concat_path` 的单引号转义被吃掉**：FFmpeg concat 清单里 `'` 要写成
   `'\''`，直接写字面量经 bash heredoc + Python 字符串两层转义后变成 `'''`。
   改成 `quote_fix = "'" + chr(92) + "''"` 拼。
2. **mock 里 `merge.start` 调了不存在的 `TS()`** → `ReferenceError`，合并页在
   mock 下点「开始合并」直接报错。改成 `mergeStamp()` 自己拼时间戳。
3. **预检回到一致时不切回快速**：原来只做「不一致 → 切精确」单向，用户删掉那条
   不合规的之后仍停在精确，点开始只会得到「精确重编码还没做」。改成双向同步。
4. **`wb_job` 读取有并发窗口**（flake 根因，详见偏差记录 P4-02 节的第 1 条）：
   `Repo._one` 原来 `self._exec(sql).fetchone()`——锁在 `_exec` 里，取回游标时已经
   出锁，别的线程在同一条连接上执行语句就把游标顶掉。现象是刚建的任务立刻查不到
   （`test_clip_batch.py` 约 1/5 跑次报 `[NOT_FOUND] 任务不存在`），压测还能压出
   `sqlite3.InterfaceError: bad parameter or other API misuse`。

### 仍未验证

- 上千段素材的合并（FFmpeg concat 清单与内存占用）。
- 不同帧率（如 25 与 29.97）混拼被拦下后的用户路径——现在只做到「拦下并说明」，
  精确重编码要等 P4-03。
- 长素材（> 1 小时）快速拼接的实际耗时与失败率。
- Windows 长路径（> 260 字符）下的 concat 清单与输出文件。
## 三、P4-03 精确合并与取消（真 ffmpeg 验收）

### 跑法

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_precise.py --config build/accept/p2.yaml
```

脚本现场造三份**规格互不相同**的素材：1280×720 / 25fps（有声）、1920×1080 / 30fps
（有声）、960×540 / 24fps（**故意不带音轨**）。三者各 3–4 秒，靠"短"让精确重编码
跑得完，同时把六项差异（分辨率、帧率、音频编码、采样率、声道、像素格式）全带出来。

### 断言表（25 项，实测值见括号）

| # | 断言 | 实测 |
| --- | --- | --- |
| 1 | 预检认得出三条可用 | `count=3` |
| 2 | 规格差异列全 | 6 项：`acodec/channels/fps/height/sample_rate/width` |
| 3 | 不一致时快速拼接不可用 | `can_quick=False` |
| 4 | 总时长约 11 秒 | `11083ms` |
| 5 | quick 被拦住 | `CONFLICT`，理由点出每个字段各文件的值 |
| 6 | precise 能提交 | `job=job_...` |
| 7 | precise 成功 | `succeeded` |
| 8 | 进度跑到 1.0 | `1.0` |
| 9 | 成片分辨率取目标规格 | `1280×720` |
| 10 | 成片帧率取目标规格 | `25fps` |
| 11 | 无音轨素材补出静音轨 | `acodec=aac` 存在 |
| 12 | 时长与期望差 < 1 秒 | 实际 `11083ms`，期望 `11083ms`，差 **0ms** |
| 13 | 成片有体积 | `3.36MB` |
| 14 | 合并记录 status | `succeeded` |
| 15 | 合并记录 items 条数 | `3` |
| 16 | 记录里的成片路径与磁盘一致 | 存在且非空 |
| 17 | 长任务能取消 | 90 秒 1080p 任务，2.5 秒后 cancel |
| 18 | 取消后状态 | `canceled` |
| 19 | 取消不留半成品 | `export/` 里没有该文件名 |
| 20 | 记录状态同步 | `canceled` |
| 21 | 临时目录已清 | `cache/tmp/<job>` 不存在 |
| 22 | 同规格素材仍走 quick | `mode=quick` |
| 23 | 没有回退就不谎称回退 | `fallback_used=False` |
| 24 | quick 失败会自己切精确 | 用 `PATH` 里放坏 ffmpeg 的假外壳模拟，见下 |
| 25 | 回退只发生一次 | 精确再失败就是 failed，不再重试 |

### 本轮修掉的问题

1. **`kill_tree(proc)` 传错参数**（真缺陷，取消一开始完全失效）：`kill_tree` 收的是
   **pid**，传 `Popen` 对象会得到 `"<subprocess.Popen object at 0x...>"` 交给
   `taskkill`，`check=False` 让失败静默通过，随后 `proc.wait()` 永久阻塞——用户点
   「取消」只会看到任务一直转圈，20 秒后才莫名其妙报 `PROCESS_FAILED`。
   修：`kill_tree(proc.pid)`，另加 `_reap(proc, grace=5.0)`（轮询 `poll()`，超时
   `proc.kill()` 兜底）。这条是靠验收脚本第 17–21 项从红变绿才敢说修好了。
2. **只勾一条时谎称"规格不一致"**：`can_quick=False` 有两种原因（不够两条 / 规格不
   同），界面统一说成后者。改成只有 `differences` 非空才自动切精确，并加
   `state.modeManual`：用户手选过模式后，预检不再替他改。
3. **输出文件名停在「合并_1段」而列表已两段**：文件名原来只在为空时写一次。加
   `state.outNameManual`（用户手改过才锁定），其余时候始终跟服务端建议名。
4. **预检表在规格不一致时仍写「统一规格 1920×1080·30fps」**：这不是统一规格，是
   精确重编码的目标值。改成 `consistent ? "统一规格" : "目标规格"`。
5. **已加入列表的源项在浅色皮肤下几乎读不出**（`opacity: 0.55`）：改成
   `border-color: var(--accent) + background: var(--active-wash) + color: var(--muted)`。
6. **mock 对 precise 一律回「还没做（P4-03）」**：P4-03 做完后这句话成了假拒绝，
   样张里直接印在横幅上。mock 改为与后端同语义（只有"不够两条"和"quick 但不一致"
   才是 CONFLICT），`result` 补 `mode_used/requested_mode/fallback_used/note`。
7. **截图里加错行**：源行改带 `data-origin`，脚本按选择器定位而不是猜文案
   （`textContent.indexOf("素材库")` 匹配不到行，之前只加进 1 条）。

### 仍未验证

- 20 条以上素材的 `filter_complex` 命令行长度与内存（`-filter_complex` 是单参数字符串）。
- 代理帧率素材（VFR）合并后的音画对齐。
- 取消发生在"刚好写完 moov"那一刻的残留文件（当前按 `_reap` 后统一删）。
- 混合 HDR / SDR 素材合并的色彩空间（本阶段统一 `format=yuv420p`）。

---

## 四、P4-04 发布中心草稿与门禁状态（真库验收，不打模型）

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_publish.py     --config build/accept/p2.yaml
```

`publish.preview` 的草稿来自库里已有总结，不进模型；门禁状态由
`publisher.gate_report()` 算，只读、不联网（登录态读本地凭证）。

### 断言

| # | 断言 | 实测 |
|---|---|---|
| 1 | 预览给出标题与正文 | 15 字 / 132 字 |
| 2 | 摘要不再出现在预览里 | `""`（字段已下线） |
| 3 | 三道门禁都在，顺序固定 | `content, account, pace` |
| 4 | 没勾确认时内容关是 todo 不是 ok | todo ·「发布前要勾一下」 |
| 5 | 三关没全绿时 `ready=False` | False |
| 6 | 生成草稿成功 | `draft_c0c384843606` |
| 7 | 改标题后保存成功 | 标题 = 手改值 |
| 8 | 保存后 `updated_at` 变了 | `10:29:41Z → 10:29:42Z` |
| 9 | 标题字数按码点算 | 7 = 7 |
| 10 | 列表带 UP 名 | 验收UP |
| 11 | 列表带更新时间 | 同上 |
| 12 | 列表带状态 | draft |
| 13 | 只重算标题时标题被换掉 | 换成最新一版总结的标题 |
| 14 | 只重算标题时正文不被清空 | 132 字 |
| 15 | 只重算标题时正文就是库里那一份 | 逐字相同 |
| 16 | 重算结果带门禁状态 | gates 有值 |
| 17 | 重算不写库 | `publish.get` 标题未变 |
| 18 | 勾确认且已登录时三关全绿 | `ok/ok/ok`，`ready=True` |
| 19 | 前两关状态都是 ok | `["ok","ok"]` |
| 20 | 删除草稿成功 | `removed=1` |
| 21 | 删完列表里没了 | 剩 0 条 |
| 22 | 删掉的草稿再读报 NOT_FOUND | `NOT_FOUND` |

### 这一轮修掉的问题

1. **`publish_note_upsert` 会改到已发布的那一行**：`note_pk` 为空时原来无条件复用
   `publish_for_project()` 的最新行；那行若是 `published`，编辑草稿会**改掉已发布记录**，
   并让"同视频不能重复发"永远判不出来。改为只有 `draft` 才复用，否则新建一行。
2. **门禁永远停在 todo**：`_publish_gates()` 没往 `gate_report()` 传 `logged_in`，
   账号关永远"发布时再确认"→ 三关永远不可能全绿、发布按钮永远不可用。
   改为默认读本地登录态（`self._gateway.is_logged_in()`，不发请求），并允许显式传入。
3. **`is_logged_in` 两种形态**：bili-scribe 那边这个成员有时是方法、有时是属性，
   `bool(client.is_logged_in())` 在属性形态下抛 `'bool' object is not callable`
   （被 RPC 包成 VALIDATION「调用参数不匹配」）。改为先取值、`callable` 再调。
4. **待确认写成了红字**：`todo` 用 `--danger` 渲染，看着像出错。按状态分色，
   `todo` 走 `--muted`。
5. **列表缺更新时间**：`wb_publish_note` 只有 `created_at`，改草稿后时间不动 →
   库升到 v4 加 `updated_at`，`upsert` 的 UPDATE 分支也写它。

### 仍未验证

- 真发一条笔记（P4-05 才有 `note_id` 与 79508 之类的错误码）。
- 三道门禁在"同一视频已有未撤回笔记"时的界面观感（数据是造的，只在单测里覆盖）。
- 一分钟限流的时间漂移（用 `published_at` 比较，没做时钟偏移校正）。

---

## 四、P4-05 发布门禁与笔记发布（真库 + 假 B 站客户端）

跑法：

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_publish_do.py --config build/accept/p2.yaml
```

**为什么用假客户端**：发布是对真实账号不可撤销的动作，验收脚本不能真发一条笔记。
脚本只替换 `publisher_core.post_note` / `withdraw_note` 两个函数，
其余全是真服务层、真数据库、真门禁计算。真站那一次的观感留给手工验收。

### 断言（36 项，全过）

| # | 断言 | 实测 |
|---|---|---|
| 1 | `publish.preview` 成功 | 项目 `pj_2933d8db7e0f`（`BV1accept0001` / aid 778899）|
| 2 | 预览带门禁 | `gates` 在返回里 |
| 3 | 三关顺序固定 | `content / account / pace` |
| 4 | 未勾确认时内容关是 todo | reason「发布前要勾一下「我已阅读并确认」」|
| 5 | 存草稿成功 | `draft_bcc31806db05` |
| 6 | 草稿状态 draft | draft |
| 7 | 列表能查到 | `publish.list` → `notes[]` |
| 8 | 未勾确认被拒 | CONFLICT |
| 9 | 拒绝文案说清原因 | 「还没勾「我已阅读并确认」，这条不发布」|
| 10 | 拒绝时一次请求都没发 | 0 次 |
| 11 | 勾确认后三关全绿 | `['ok','ok','ok']` |
| 12 | `ready=true` | true |
| 13 | 发布成功 | note_id `note_acc_1` |
| 14 | note_id 入库 | 读回一致 |
| 15 | 状态变 published | published |
| 16 | 只发一次请求 | 1 次 |
| 17 | 标题与草稿一致 | 一致 |
| 18 | 请求带 aid | 778899 |
| 19 | 请求带正文 | 非空 |
| 20 | 同一条不能重复发 | CONFLICT |
| 21 | 重复拒绝带 note_id | 「这条已经发过了（note_id=note_acc_1）」|
| 22–25 | 撤回：成功 / 带 aid+note_id / 状态 withdrawn | `{'aid': 778899, 'note_id': 'note_acc_1'}` |
| 26 | 撤回后新草稿第三关放行 | pace=ok |
| 27–33 | B 站报错：失败 / BILI_ERROR / 草稿还在 / 正文没丢 / 状态 failed / 原因写进记录 / 重发能成 | `[12015] 验证码校验失败` |
| 34–35 | 超长标题（90 字）保存就被拦 | VALIDATION「标题超过 80 字（现在 90 字）」|
| 36 | 未知草稿 NOT_FOUND | NOT_FOUND |

### 这一阶段修掉的问题

1. **`ui.modal` 关不掉**（真实缺陷，影响全应用）：`close()` 写的是
   `root.removeChild(panel)`，但 `panel` 的父节点是 `overlay` → 抛
   `NotFoundError: The node to be removed is not a child of this node`，
   弹窗关不掉、`resolve(true)` 永远走不到 → 用户看到「点了发布没反应」。
   改成摘 `overlay`。
2. **`publishRow` 在未保存草稿时整块不出现**：门禁卡写着「还不能发」，编辑器下面
   却没有任何发布入口，用户不知道该干什么。补一条
   「先点「保存草稿」：发布会用库里这一版的标题与正文」。
3. **mock 的门禁永远停在「发布时再确认」**：mock 没有登录流程，账号关恒 todo →
   `ready` 恒 false，界面里发布按钮永远点不动（截图里就成了假缺陷）。
   mock 加 `MOCK_LOGGED_IN = true` 并在三处调用点传入。
4. **P2-09 的旧断言过期**：`test_withdraw_is_honest_about_phase5` 断言撤回返回
   `UNSUPPORTED「Phase 5」`；P4-05 之后撤回是真的，改写成「未知草稿 NOT_FOUND +
   未发布草稿 CONFLICT」。
5. **`error_of()` 只认 `error.code`**：脚本里 `pick_project` / `error_of` 的返回形态
   与预期不同，导致三条断言假红（详见偏差记录）。

### 仍未验证

- 真发一条笔记（真账号、真限流、真错误码文案）。
- 笔记在 B 站评论区里的渲染（换行、加粗、超长段落的折叠）。
- 撤回后服务端的延迟一致性（撤回请求成功但列表刷新有时间差）。

## 五、P4-06 发布记录与限流（真库验收，绝不真发）

跑法：

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_records.py --config build/accept/p2.yaml
```

脚本自建一个临时项目与四行记录（发过 / 撤过 / 失败 / 还没发），跑完把这几行删掉；
B站 请求用 `core.post_note` 的替身拦住，一次都不外发。

| # | 断言 | 实测 |
|---|---|---|
| 1 | `publish.records` 能调通 | ok |
| 2 | 记录里三条都在，草稿不算 | 3 条 |
| 3 | 计数分开给（已发/已撤/失败） | 各 1 |
| 4 | 按「最后一次动作」排序 | 刚撤回的排第一 |
| 5 | 记录行不带正文 | 无 `content` 字段 |
| 6 | 记录带视频链接 | `…/video/BV1recaccept1` |
| 7 | 状态有中文文案 | 已发布 / 已撤回 / 发布失败 |
| 8 | 刚发过 → 不能发 | `ready=false` |
| 9 | 被卡的是频率关 | `blockers=['pace']` |
| 10 | 给出剩余等待秒数 | 30–46 秒（实测 44） |
| 11 | 理由写明还要等几秒 | 「还要等 44 秒才能再发（限流 60 秒）」 |
| 12 | 同视频已有笔记 → 仍然不能发 | `ready=false` |
| 13 | 理由指向重复而不是限流 | 「已经发过了」 |
| 14 | 撤回之后三关放行 | `ready=true` |
| 15 | 79508 说明去删旧笔记、且不会覆盖 | 文案含「删掉旧的那条」「不会覆盖」 |
| 16 | 79508 保留原始码 | 文案以 `[79508]` 开头 |
| 17 | 79508 发布失败 | `ok=false` |
| 18 | 失败后状态是 `failed` | 库里 `status=failed` |
| 19 | 失败原因入库且是人话 | 含「删掉旧的那条」 |
| 20 | 失败后草稿正文没丢 | 正文仍在 |
| 21 | `limit` 超上限被夹住而不是报错 | ok |
| 22 | `app.open_url` 拒掉非 http | `VALIDATION` |

修掉的问题（都是这轮验收/看样张时暴露的）：

1. **限流只按视频算**：`_publish_gates` 原来用「同 bvid 的已发布记录」算上次发布时间，
   换个视频就绕过去了，60 秒限制等于没做。改成按账号（全部已发布记录取最大值），
   重复检查仍然只看同一个 bvid。
2. **草稿列表混进已发布/已撤回的行**：`publish.list` 直接返回 `repo.publish_list`，
   发过的笔记还挂在「草稿」里，点开还能改。改成只回 `status='draft'`。
3. **`_NOTE_HINTS` 少了方案书点名的码**：79508 / 79513 / -111 / -101 原来走兜底文案
   （「可稍后重试」），用户按这个提示重试只会反复失败。四个码补上专门文案。
4. **mock 种子记录的正文是数组**：`mockPlain` 解析失败 → 门禁判「正文是空的」，
   界面把一条发出去过的笔记说成空正文。种子改成 Delta JSON 字符串，时间改 ISO
   （本地化格式没法参与「距上次发布多少秒」的计算）。
5. **记录行按钮叫「查看草稿」，但那条早不是草稿了**：改叫「查看详情」，
   前端测试也改成断言右栏载入了这一条记录，而不是「草稿列表有一行高亮」。
6. **门禁卡底部还写着「发布与撤回在 P4-05 接上」**：P4-05 已经做完，这句成了过期承诺。
   改成「三关都过了才允许点发布；限流按账号算」。

仍未验证：

1. 真发一条笔记之后的记录回读（真 note_id、真 published_at）——本轮全程替身，一次都没外发。
2. 平台侧的 79508 / 79513 真实返回（本地按方案书文案实现的，没撞过真接口）。
3. 跨设备/跨账号的限流表现（本地只按本机库里的发布记录算）。
