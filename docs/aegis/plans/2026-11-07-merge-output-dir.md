# 合并页「输出目录」按钮与「打开输出目录」实施规划

- 规划日期：2026-11-07（Asia/Shanghai）
- 状态：已评审，待执行
- 批准来源：用户 m00915 / m00919；专家评审 m00789–m00914
- 执行方式：inline，单写者按 T1 → T5 顺序执行；不需要再拆子代理
- 计划路径：`docs/aegis/plans/2026-11-07-merge-output-dir.md`

> Aegis Visibility：本单同时改前端状态机、后端 RPC 契约与输出路径校验边界，且要交给另一个新对话执行，因此需要一份可独立落地的持久规划。

## 1. 目标

把「合并」页的输出目录从只读输入框改成可操作控件：

1. 输出目录路径本身是按钮，点击打开系统「选择文件夹」对话框；显示完整路径，过长省略，鼠标悬停 `title` 看全文。
2. 输出目录右侧增加「打开输出目录」按钮；合并完成后点击，在资源管理器打开这次成片所在目录。
3. 清空列表时输出目录恢复默认；从合并页切到别的页面再回来，当前会话内保留手选目录；重启应用恢复默认；不写配置文件、不写数据库。

## 2. 已定产品决策（不可重新设计）

| 决策 | 结论 |
|---|---|
| 输出目录控件 | 路径本身作为按钮，不要只读输入框 |
| 路径显示 | 完整路径；容器过窄时 CSS 省略；`title` 永久放完整路径 |
| 打开按钮 | 位于路径按钮旁边，文案「打开输出目录」 |
| 清空列表 | 顺序表、预检、输出目录、上次成片引用一起回到新任务初态 |
| 离开合并页再回来 | 当前进程内保留手选目录和上次成片引用 |
| 重启应用 | 输出目录、手选状态、上次成片引用全部恢复默认 |
| 持久化 | 不写配置、不写数据库、不写 localStorage |

## 3. 基线（接手先读，不要覆盖）

仓库：`C:\Users\b1596\Desktop\视频内容总结`，分支 `main`，当前 HEAD `1c2e0da`。

工作区已有上一轮「合并模块添加本地文件」的未提交改动，不是本单要重写的内容：

- `M docs/用户手册.md`
- `M tests/wb/frontend_test.js`
- `A tests/wb/test_desktop_picker.py`（已 staged）
- `M tests/wb/test_merge_quick.py`
- `M tests/wb/test_merger.py`
- `M tests/wb/test_rpc.py`
- `M video_workbench/app/service.py`
- `M video_workbench/core/merger.py`
- `M video_workbench/desktop.py`
- `M video_workbench/web/js/pages/clips.js`
- `M video_workbench/web/js/pages/merge.js`
- `?? Temp/`

执行前先跑 `git status --porcelain` 现读。若上一轮改动已经提交，则以新的 HEAD 为准；无论哪种情况，都不得用 `git checkout --`、`git reset --hard` 或整文件覆盖回退这些改动。新增改动只加自己的 pathspec，禁止 `git add -A`。

环境纪律：

- Python 必须用 `.venv/Scripts/python.exe`。
- 中文输出前 `export PYTHONIOENCODING=utf-8`。
- 跑全量 pytest 前把 Git for Windows 放到 PATH 最前：`export PATH="/c/Program Files/Git/cmd:$PATH"`；否则 `test_git_no_normalization_on_locked_paths.py` 的 autocrlf 用例会在 niugit 下假红。
- 改大文件只用 `edit` 字面替换，禁止 `read` 全文后 `write` 整写。

## 4. 现有实现与根因

### 4.1 前端

- `video_workbench/web/js/pages/merge.js` 的 `state` 没有 `outDirManual`；`refreshPreflight()` 里 `state.outDir = data.output.dir || state.outDir` 无条件覆盖用户选择。
- 输出目录目前是 `ui.h("input", { class: "input mono", readonly: true })`，无法点击。
- `pollMerge()` 成功只写 `WB.revealLast = result.path`，全前端没有消费者；没有保存 `merge_id`，所以无法安全地按合并记录打开目录。
- `collect()` 总是把 `state.outDir` 传给 `merge.start`；默认路径也会被当成显式路径。
- `clearAll()` 只清 `order` 与 `preflight`，不清 `outDir`。

### 4.2 后端

- `video_workbench/app/service.py` 的 `fs_reveal()` 只允许 `_allowed_roots()` 内的已存在路径；默认输出目录常在源文件父目录下的 `merge`，可能在白名单外，所以不能直接复用 `fs.reveal`。
- `fs_pick(kind="dir")` 已由 `_make_picker()` 支持 FOLDER_DIALOG；取消返回 `{path: None}`，这条能力已经存在。
- `merge_start()` 直接 `Path(out_dir)`，没有校验绝对路径、存在性、目录类型；`ensure_free_space()` 遇到缺失盘符会抛 `FileNotFoundError`，经 RPC 变成 INTERNAL。
- `merge_start()` 返回 `merge_id`；`_merge_job` 的结果含 `merge_id` 与 `path`，快速拼接失败转精确时 `path` 会改成 `_precise.mp4` 并更新 `wb_merge.output_path`。
- `repo.merge_get(merge_id)` 可取 `output_path`/`status`。
- `video_workbench/web/js/mock.js` 的 `mergePreflight` 约在 2257 行，`merge.start` case 约在 2343 行，`merge.list` 约在 2400 行；新增 RPC 必须补 mock。

### 4.3 为什么必须改代码

只改文案或只改前端不够：`fs_reveal` 的授权边界是故意收紧的，不能为了「打开输出目录」放开任意路径；必须在服务端增加一个只接受 `merge_id`、以 `wb_merge.output_path` 为唯一授权的窄接口。输出目录选择也必须让后端知道「这是用户从系统对话框选的目录」，否则 `merge_start` 无法安全接受外部路径。

## 5. 接口与行为契约

### 5.1 `fs.pick`

- 请求：`{ kind: "dir" }`
- 返回：`{ path: string | null }`，取消时 `path` 为 `null`。
- 服务端在成功选中非空目录后，把规范化后的绝对路径记入进程内 `_picked_dirs`（最多 32 个，超出丢最旧），仅当前进程有效。

### 5.2 `merge.start`

- `out_dir` 为空字符串：使用 `merger.default_output_dir(...)` 的默认值，行为与当前一致；默认目录允许自动创建。
- `out_dir` 非空：必须是绝对路径、存在且是目录；同时必须落在 `_allowed_roots()` 内，或等于本次进程内 `fs.pick(kind="dir")` 选过的目录。否则抛 `VALIDATION`，错误文案要能指导用户重新选择。
- 传给 `Path` 前先做 `expandvars` / `expanduser` / `abspath`；不要接受文件路径、缺失盘符、UNC/设备名伪路径。
- `ensure_free_space` 的 `OSError` 转成 `WBError("IO", "输出目录不可写", str(exc))`，不要把 `FileNotFoundError` 漏成 INTERNAL。

### 5.3 `merge.reveal_output`

- 请求：`{ merge_id: string }`。
- 返回：`{ path: string }`，`path` 为实际定位到的目录。
- 只按 `merge_id` 查 `repo.merge_get()` 的 `output_path`；不接受页面传任意路径。
- 输出文件存在：打开它的父目录；输出文件不存在但父目录存在：打开父目录并在返回路径里说明；两者都不存在：`NOT_FOUND`。
- `os.startfile` 失败转 `PROCESS_FAILED`；非 Windows 分支沿用现有 `fs_reveal` 的平台处理方式。
- 不修改 `fs_reveal` 的白名单，不新增 `fs.reveal` 的任意路径入口。

## 6. 任务分解

### T1 先写红测试（契约锁定）

**文件**：
- 新建 `tests/wb/test_merge_output_dir.py`
- 修改 `tests/wb/test_merge_quick.py`（把现有直接传 `out_dir=tmp_path/"out"` 的用例改为显式创建 `tmp_path/"media"/"out"`，因为 `media_root` 是允许根）
- 修改 `tests/wb/frontend_test.js`（新增合并页输出目录用例）

**后端用例至少覆盖**：
1. `test_fs_pick_dir_remembers_the_selected_folder`：假 picker 返回外部目录，`fs.pick(kind="dir")` 后该目录可用于 `merge_start`。
2. `test_merge_start_rejects_manual_out_dir_outside_allowed_roots`：未选过的外部目录 → `VALIDATION`，不派 job、不插 merge 记录。
3. `test_merge_start_accepts_a_folder_picked_from_the_dialog`：先 `fs.pick` 再 `merge_start` → 成功，`job.payload.dest` 的父目录等于所选目录。
4. `test_merge_start_rejects_a_file_as_out_dir`：`out_dir` 指向文件 → `VALIDATION`。
5. `test_merge_start_turns_missing_drive_into_validation_not_internal`：`out_dir` 指向不存在盘符/目录 → `VALIDATION`，不是 `INTERNAL`。
6. `test_merge_reveal_output_opens_parent_of_the_recorded_output`：monkeypatch `os.startfile`，断言收到的是输出文件的父目录。
7. `test_merge_reveal_output_rejects_unknown_merge_id`：未知 id → `NOT_FOUND`。
8. `test_merge_reveal_output_does_not_accept_an_arbitrary_path`：接口参数只有 `merge_id`；传 `path` 不生效或被忽略，不能用来打开任意目录。

**前端用例至少覆盖**：
1. 初始显示默认输出目录；点击路径按钮发起 `fs.pick {kind:"dir"}`；返回目录后按钮文案更新。
2. 再触发一次 `merge.preflight`，手选目录不被 `data.output.dir` 覆盖。
3. 取消选择不改原值，不显示错误。
4. 合并成功后「打开输出目录」从禁用变可点；点击调用 `merge.reveal_output`，参数是上次 `merge_id`。
5. 打开失败时错误可见，不 silent catch。
6. 清空列表后输出目录回默认、打开按钮禁用；再加片段预检后恢复默认路径。
7. 从合并页切到其他页再切回来，手选目录仍保留。

**红测试预期**：T1 的新用例在实现前应因缺少 `merge.reveal_output` / 缺少状态字段 / 仍为 input 而失败；记录失败原因。不要把缺少实现误报成产品已有缺陷。

### T2 后端实现（`video_workbench/app/service.py`）

1. 在 `MergeStartParams` 附近新增 `MergeRevealOutputParams`：`merge_id: str = ""`。
2. `WorkbenchService.__init__` 增加 `self._picked_dirs: list[str] = []`。
3. 增加私有方法 `_remember_picked_dir(path)`：`expandvars`/`expanduser`/`abspath`/`realpath` 后去重、保留最近 32 个。
4. `fs_pick`：只在 `str(kind).lower() == "dir"` 且 `picked` 非空时记录；返回结构不变。
5. 增加 `_validate_manual_out_dir(out_dir)`：绝对路径、`is_dir()`、`is_within` 任一允许根或 `_picked_dirs` 命中；失败抛 `VALIDATION`。
6. `merge_start`：`raw_out = str(out_dir or "").strip()`；为空走默认目录并 `mkdir(parents=True, exist_ok=True)`；非空先调用校验，再检查 `ensure_free_space`；捕获 `OSError` 转 `WBError("IO", ...)`。
7. 新增 `merge_reveal_output(self, merge_id: str = "")`：查 `repo.merge_get(merge_id)`；取 `output_path`；按 5.3 行为打开；失败按上述错误码抛。
8. 在 `_register_routes` 的 `merge.list` 附近 `r.add("merge.reveal_output", self.merge_reveal_output, MergeRevealOutputParams)`。

**边界**：不修改 `fs_reveal` 的 `_allowed_roots` 和白名单；不改变 `merge_list` 的输出形状；不把 `merge_reveal_output` 做成接受 `path` 的通用 opener。

### T3 前端实现（`video_workbench/web/js/pages/merge.js`）

1. `state` 增加 `outDirManual: false`、`lastMergeId: ""`、`lastOutput: ""`。
2. `refreshPreflight()`：只在 `!state.outDirManual` 时用 `data.output.dir` 更新 `state.outDir`；文件名仍按原逻辑跟随段数。
3. `collect()`：`out_dir: state.outDirManual ? state.outDir : ""`，让默认目录由后端计算。
4. `build()`：把 `els.outDir` 从 `input` 改成 `button`（建议 `class: "btn btn--ghost merge-outdir-path"`，`type: "button"`，初始文案「选择输出目录」）；新增 `els.openDir` 按钮（`btn btn--ghost`，文案「打开输出目录」），两者放进 `.merge-outdir-row` 容器。
5. 新增 `pickOutDir()`：`state.busy` 防重入；`WB.rpc("fs.pick", {kind:"dir"}, {silent:true})`；有 `path` 时 `state.outDir = String(path).trim(); state.outDirManual = true; state.error=""; state.note="";`；取消不改；异常写入 `state.error`；`finally` 恢复 `busy` 并 `paintAll()`。
6. 新增 `openOutputDir()`：无 `lastMergeId` 直接返回；`WB.rpc("merge.reveal_output", {merge_id: state.lastMergeId}, {silent:true})`；异常写入 `state.error`，不要静默 `catch`。
7. `paintOutput()`：路径按钮 `textContent = state.outDir || "选择输出目录"`，`title = state.outDir || "点击选择输出目录"`；打开按钮 disabled 条件为 `!state.lastMergeId || state.running || state.busy`；运行中 title 写「合并完成后可打开」。
8. `clearAll()`：清 `order`、`preflight`，并重置 `outDir=""`、`outDirManual=false`、`lastMergeId=""`、`lastOutput=""`；调用 `paintAll()`。
9. `pollMerge()` 成功分支：`state.lastMergeId = result.merge_id || ""`，`state.lastOutput = result.path || ""`；`WB.revealLast` 保留兼容但不再作为唯一来源。
10. 在 `render()` 生命周期内不要重置上述字段；页面切换依赖模块级 `state` 保留。

### T4 样式、mock 与文档

- `video_workbench/web/css/merge.css`：新增 `.merge-outdir-row`（`display:flex; gap:6px; align-items:center; min-width:0`）、`.merge-outdir-path`（`flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:left`）；颜色全部用现有 token，禁止字面量。
- `video_workbench/web/js/mock.js`：`fs.pick` 按 `kind==="dir"` 返回一个模拟目录；新增 `merge.reveal_output` case，返回 `{path: ...}`，确保 `?mock=1` 下按钮可点。
- `docs/用户手册.md` 合并章节：在步骤 3 增加「输出目录：点路径按钮选择文件夹；默认是第一个可用源文件父目录下的 `merge`」，步骤 4 后增加「完成后点『打开输出目录』定位成片」。不要写没有实测支撑的速度/收益承诺。

### T5 测试、真实 GUI 与打包

1. 目标 pytest：

```bash
export PYTHONIOENCODING=utf-8
.venv/Scripts/python.exe -m pytest tests/wb/test_merge_output_dir.py tests/wb/test_merge_quick.py tests/wb/test_merger.py tests/wb/test_rpc.py -q
```

2. 前端：`node tests/wb/frontend_test.js`；本次新增的合并页用例必须全 `ok`。若全量仍有与本次无关的既有失败，单独记录原始用例名，不要当成新改动通过或失败。

3. 全量回归：

```bash
export PATH="/c/Program Files/Git/cmd:$PATH"
export PYTHONIOENCODING=utf-8
.venv/Scripts/python.exe -m pytest tests/wb -q
```

4. ruff（命令原文 + 范围 + 剩余条数三样都要写进交付）：

```bash
.venv/Scripts/ruff.exe check video_workbench/app/service.py video_workbench/web/js/pages/merge.js tests/wb/test_merge_output_dir.py tests/wb/test_merge_quick.py
.venv/Scripts/ruff.exe format --check --diff video_workbench/app/service.py tests/wb/test_merge_output_dir.py tests/wb/test_merge_quick.py
```

5. 真实 GUI 验收（手动的部分必须标成手动，不能写成自动化通过）：

- 先关闭所有正在运行的 `VideoWorkbench.exe`。
- 启动 `dist/VideoWorkbench/VideoWorkbench.exe`。
- 合并页点「添加本地文件」，从 `C:\未明子录播\raw` 选两个 `.flv`。
- 点输出目录按钮，确认弹出 Windows「选择文件夹」；选一个预先创建的 `C:\未明子录播\merge_out`（允许工作区外路径）。
- 确认路径按钮显示完整路径、过窄窗口下省略且悬停 title 可读全文。
- 开始合并，等待成功；点「打开输出目录」，确认资源管理器打开 `C:\未明子录播\merge_out`。
- 清空列表，确认输出目录恢复默认；重新加入片段后确认默认可再次合并。
- 切到其他页面再回来，确认手选目录在当前进程内保留。
- 关闭应用重开，确认输出目录恢复默认、上次成片引用消失。

6. 打包：

```bash
.venv/Scripts/python.exe scripts/build_wb.py
.venv/Scripts/python.exe scripts/build_wb.py --verify-only
```

产物在 `dist/VideoWorkbench/`；不要新建 tag、不要发 release，除非用户明确要求。旧实例可能锁住 exe，先关闭再构建。

## 7. 验证矩阵（Definition of Done）

| 项目 | 通过标准 |
|---|---|
| 输出路径按钮 | 点击弹出文件夹选择框；选中后按钮显示所选路径；过长省略、title 完整 |
| 取消选择 | 路径保持原值，不新增错误 |
| 预检覆盖 | 手选目录后再次预检，目录不被默认值覆盖 |
| 默认目录 | 未手选时仍走第一个可用源文件父目录下的 `merge`；行为不回归 |
| 外部目录 | 工作区外、由系统对话框选中的已有目录可合并成功 |
| 显式路径校验 | 文件路径、缺失目录、未选过的外部路径被 `VALIDATION` 拦住 |
| 打开输出目录 | 合并成功后按钮可点；打开记录中成片的父目录；失败可读错误 |
| 清空 | 输出目录恢复默认，打开按钮禁用 |
| 页面切换 | 当前会话内保留手选目录 |
| 重启 | 恢复默认，无持久化副作用 |
| 回归 | 目标 pytest 全绿；全量 pytest 无新增失败；前端合并用例全绿；ruff 三样读数完整 |
| 打包 | `build_wb.py` 成功、`--verify-only` exit 0，dist 内含新前端 |

## 8. 风险与回滚

- 风险：直接改 `fs_reveal` 白名单会扩大任意目录打开能力，禁止。
- 风险：`merge_start` 校验过宽会允许任意写路径；过窄会误杀用户刚选的目录。用 `_picked_dirs` + 允许根双通道解决。
- 风险：默认输出目录保存在 `state.outDir` 但 `outDirManual=false`，`collect()` 必须传空，否则后端会把默认路径当手动路径校验。
- 风险：清空时若只清 `order` 不清 `lastMergeId`，会出现「当前默认目录」与「上次成片」错位；按决策一起清。
- 回滚：只回退本单新增改动；不得回退上一轮未提交的合并选择器修复。若需要临时保底，保留只读输出路径显示，但不要删除已有测试或把 `fs_reveal` 放开。

## 9. 交付格式

接手对话完成后按以下格式汇报：

```text
做了什么：一句话
测试：<命令原文> → 通过 N，失败 0
截图：<真实 GUI 截图路径>（输出目录按钮、选择后路径、合并成功后打开按钮）
打包：<build_wb.py 结果> / <--verify-only 结果>
没验证到的：哪些、为什么；没有就写「无」
```

每个数字都要附产生它的命令原文；不要把计划里的预期写成已经跑过的结果。

## 10. 给接手对话的第一条指令

```text
读 docs/aegis/plans/2026-11-07-merge-output-dir.md；先跑 git status --porcelain 和 git log -1 --oneline，确认上一轮未提交的合并选择器修复没有被覆盖。按 T1→T5 执行，禁止 git reset/checkout 整文件回退，禁止 git add -A；完成后按计划第 9 节交差。
```
