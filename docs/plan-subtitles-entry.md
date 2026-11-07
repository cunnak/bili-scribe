# 字幕模块一级入口与项目选择 · 项目规划书

> 版本：v1.0（2026-11-07）
> 状态：**待执行**。用户已对本文第 2 节的四个决策拍板；执行者请在新对话里按本文件施工，不要重新开评审。
> 目标仓库：C:\Users\b1596\Desktop\视频内容总结（video_workbench / bili_scribe 复刻项目）
> 交付物：可运行的前端改动 + 测试 + 文档更新；交付前必须跑通对应测试并附截图。

---

## 0. 执行者第一页（硬约束，先读）

1. **只做本规划范围内的改动**。不碰 bili_scribe/ 业务代码、不碰 video_workbench/core/summary/roles.py 与 summarizer.py 的逐字节副本、不改 scripts/sync_mirror.py。
2. **后端零改动**。本规划全部通过现有 RPC 完成：library.list / library.get / subtitle.get / subtitle.save / fs.reveal。不给 RevealParams 加 project_id，不改 fs_reveal。
3. **前端是原生 JS**（无构建链、无框架）。沿用现有 h() / ui.svg() / WB.rpc() 写法，不引入新依赖。
4. **业务文本一律走 textContent 或 ui.h({text})**。项目标题、UP 名、错误信息都来自外部；禁止 innerHTML 拼这些值。ui.h({html}) 只允许固定文案/图标。
5. **不创建 git commit / 不 push**，除非用户当轮明确要求。
6. Python 一律 .venv/Scripts/python.exe；中文探针先 export PYTHONIOENCODING=utf-8。全量回归命令：.venv/Scripts/python.exe -m pytest tests/wb -q。
7. **每个数字后面附产生它的命令原文**；没跑过的检查写进「没验证到的」，不许写成通过。
8. 新建 tests/wb/*.py 要先把路径登记进 sha256 快照清单并 git add（只 add，不 commit），依据见仓库 AGENTS.md 第六节。
9. 改大文件只用字面替换（edit），禁止 read 全文 + write 整写；整写后必须 git diff --numstat -- <路径> 核对。
10. 界面文案不许写没有实测支撑的收益承诺；新增文案要能通过 prove-it 的删除测试。

---

## 1. 背景与目标

### 1.1 现状

- 字幕编辑器 video_workbench/web/js/pages/subtitles.js 在文件末尾这样注册：

~~~js
WB.router.define("subtitles", {
  title: "字幕",
  crumb: "转录文本",
  icon: "subtitles",
  nav: false,
  render: render
});
~~~

- nav:false 意味着它不在左栏出现，只能从素材库某条视频点「字幕」进入（#/subtitles?project=pj_xxx）。
- 直接打开 #/subtitles（无 project 参数）时，render(ctx) 把 state.projectId 设为空，并显示：

~~~
没指定项目。从素材库点开一条视频再进这里。
~~~

- 页面没有项目选择器；用户想换项目只能退回素材库再点另一条。
- 页面没有「在文件夹中显示」；总结页有类似按钮，但合集分支还错误地传 project_id（见 3.4，属于既有缺陷，本单不修）。

### 1.2 目标

1. 左栏增加「字幕」入口，位置在「素材库」与「总结」之间。
2. 字幕页顶部增加项目选择（可搜索、可切换）与「在文件夹中显示」按钮。
3. 无参数打开 #/subtitles 时自动进入一个可用项目；没有项目时显示可行动的空态。
4. 切换项目不能丢草稿、不能让旧请求污染新项目、不能出现播放器消失或状态串项目。
5. 浏览器后退键从字幕页回到上一个页面，而不是在项目之间倒退。

### 1.3 非目标（本单不做）

- 不重构路由为 History API；仍是 hash 路由。
- 不做字幕内容编辑功能本身的改动（分割/合并/替换/AI 优化/翻译/导出都不动）。
- 不做项目重命名/删除。
- 不修 summary.js 合集分支的 fs.reveal({project_id}) 错误（另开单）。
- 不修 infra/files.py 的 is_within() junction/symlink 问题（安全单）。
- 不保证「旧 media_root 项目」一定能打开目录：fs.reveal 的白名单只认当前配置的 media_root；旧项目根不在白名单时后端会报 VALIDATION，本单只在界面上把错误说清楚，不改后端白名单策略。

---

## 2. 已拍板决策（不可再改）

用户于 2026-11-07 明确拍板：

| # | 决策点 | 选择 | 实施含义 |
|---|---|---|---|
| 1 | 无参数打开字幕页 | **A + 空态兜底** | 默认选第一个 has_transcript === true 的项目；没有则选列表第一个；一个项目都没有则显示空态「还没有项目」+ 去素材库按钮。与现有总结页的默认规则一致。 |
| 2 | 切换项目时保存失败 | **A. 阻断切换并提示** | 先 flush 旧项目草稿；flush 失败就留在旧项目，选择器回弹到旧值，显示错误，不切换。绝不静默丢草稿。 |
| 3 | 「在文件夹中显示」 | **A. 打开项目根目录** | 调 fs.reveal({ path: project.root_dir })；不做 Explorer 选中具体文件，不改后端加 /select。 |
| 4 | 浏览器后退 | **B. 离开字幕页** | 项目切换用 router.replace（replaceState），不产生历史条目；Back 从字幕页回到进入前的页面。 |

补充约定：

- 左栏顺序固定为：新建任务 / 素材库 / **字幕** / 总结 / 切片台 / 合并 / 设置。字幕必须在素材库与总结之间（由 index.html 的脚本注册顺序决定：subtitles.js 在 library.js 与 summary.js 之间加载）。
- 设置按钮仍固定在左栏底部 .rail__foot，不属于 navItems()。

---

## 3. 现状证据（全部为静态只读核实）

> 行号会漂，本文用符号名指位置；执行时用 grep -n 现取。

### 3.1 路由

video_workbench/web/js/router.js

- router.define(name, definition) 直接存表；没有 destroy / beforeLeave 概念。
- router.navItems() 过滤 routes[name].nav；renderRail(host) 生成 button.rail__item[data-route]，点击一律 router.go(item.route)。
- router.go(route, params) 先拼 #/route?k=v，写 location.hash，再无条件 renderRoute({route, params})。
- renderRoute(parsed) 把 current 设为新路由，执行 view.textContent = ""，然后调用 definition.render(ctx)；**没有调用上一页的清理函数**。
- highlight(name) 给当前 .rail__item 设 aria-current="page"。

### 3.2 字幕页

video_workbench/web/js/pages/subtitles.js

- 模块级：state（projectId / project / segments / qc / issues / stats / selected / activeId / editingId / undo / redo / dirty / saving / savedAt / scrollTop / follow / loading / error / pending / optJob / opt / optBusy / transJob / trans / transBusy）、nodes、wave、video、saveTimer、rafPending。
- 常量：AUTOSAVE_MS = 700、UNDO_LIMIT = 40、ROW_HEIGHT = 38。
- render(ctx) 只重置 projectId / segments / selected / undo / redo / dirty / opt / trans / exp / error；**不重置** project / qc / issues / stats / activeId / editingId / savedAt / scrollTop / loading / saving / pending，也**不清理** saveTimer、video、wave、resize 监听。
- load()：Promise.all([subtitle.get({project_id}), library.get({id}).catch(()=>null)]) 后**无条件** applyPayload；没有请求代次。失败时 state.project 保留旧值。
- loadPeaks()：直接读 state.projectId 发 audio.peaks；没有守卫。
- save()：触发时才读 state.projectId / state.segments；回调不校验项目。
- runOp()：同样在回调里直接 applyPayload，没有项目代次。
- mountMedia()：模块级 video 非空时直接 return；host.innerHTML = "" 只在新建 video 时执行。因此页面 DOM 被路由清空后，再次进入会看到「没有播放器」。
- render() 的 setTimeout 里每次 window.addEventListener("resize", function(){ wave && wave.resize(); })，从不移除。
- 页面通过 WB.pages.subtitles 暴露 render / state / _internals（indexQc / renderRows / shortMs / ms / selectRange / rowHeight / doExport / startExport）。

### 3.3 图标

video_workbench/web/js/components/ui.js

- ICONS 有 plus / library / scissors / merge / send / gear / folder / spinner / check / alert / film / note / trash，没有 subtitles。
- ui.svg(name) 对未知 name 返回空字符串，所以即使 nav:true，左栏也会出现一个只有文字的条目。

### 3.4 总结页可复用范式（只读参考）

video_workbench/web/js/pages/summary.js

- loadProjects() 调 library.list({limit:200, offset:0})；默认选第一个 has_transcript，否则第一个。
- pickProject(id) 只改 state.projectId 然后 loadSummary()。
- field("sum-project","PROJECT","选一条素材，总结只用它自己的字幕", projectOptions, pickProject) 在 head() 的 .sum-fields 里，样式直接复用 .sum-fields / .sum-fld。
- 单视频页的 reveal() 正确：call("fs.reveal", {path: row.root_dir})。
- 合集按钮在 collHead() 仍错误调用 fs.reveal({project_id:first.id}) —— 这是既有缺陷，**本单不修**，但要避免把这个错误复制到字幕页。

### 3.5 后端接口契约

video_workbench/app/service.py

- RevealParams 只有 path: str（必填）。
- fs_reveal(path)：abspath / expandvars / expanduser 后过 _allowed_roots() 的 is_within 检查；允许 media_root / cache_root / models_root / db 目录 / logs / source_path 目录；越界 VALIDATION「只允许打开工作区内的路径」；不存在 NOT_FOUND；OSError PROCESS_FAILED。Windows 分支 os.startfile(os.path.dirname(target) if os.path.isfile(target) else target)，没有 /select。
- library_list(query, limit, offset)：limit clamp 1..200；返回 {items,total,limit,offset}；每行经 _decorate 带 root_dir / has_thumb / has_transcript / missing / assets。
- library_get(id)：项目不存在抛 NOT_FOUND「项目不存在」；返回 {project}，project 带 assets。
- subtitle_get(project_id)：项目不存在抛 NOT_FOUND；无字幕返回 has_transcript:false 与空 segments。
- subtitle_save(project_id, segments)：空 segments 抛 VALIDATION「字幕为空，保存会把已有字幕清掉」。
- subtitle_op(...)：服务端自己 _store_subtitles 持久化，返回全量 segments/qc/stats。

### 3.6 测试与文档

- tests/wb/frontend_test.js：ROUTES 已含 ["subtitles","字幕"]；左栏断言目前是 **6 项**，顺序 ["新建任务","素材库","总结","切片台","合并","设置"]；nav:true 后会红，应改 7 项并在「素材库」后加「字幕」。
- 现有字幕测试均用 WB.router.go("subtitles",{project:"p_demo_1"})；mock.js 有 p_demo_1 / p_demo_2 / p_demo_3 三个项目，其中 p_demo_1 有字幕，p_demo_2 无字幕、有源文件，p_demo_3 无字幕、缺文件。
- tests/wb/test_summary_real_backend_ui.py：真桥测试范式，用 Playwright page.expose_binding("__wbRpc", _bridge) 把进程内 WorkbenchService().dispatch 挂成 window.pywebview.api.rpc；页面用 ?mock=1 起首屏，再把相关方法换成真 service。
- README-VideoWorkbench.md 与 docs/用户手册.md 都还写着左栏「五项」，需要随入口增加更新。

---

## 4. 目标行为规范

### 4.1 左栏入口

- subtitles.js 的 WB.router.define 改 nav: true。
- ui.js 的 ICONS 增加 subtitles 图标；建议用 16×16、stroke="currentColor" 的简单图形（两条字幕线 + 外框），不要复用 film（语义是视频）。
- 左栏出现顺序必须是：新建任务 / 素材库 / **字幕** / 总结 / 切片台 / 合并 / 设置。
- 当前页高亮由 router.highlight 负责，沿用 aria-current="page"。
- 点击当前已激活的左栏项不重复渲染：renderRail 的 onclick 里先判断 current && current.route === item.route，是则 return。否则会重新渲染当前页、丢掉未保存草稿并重挂播放器。
- 键盘可达性：button 本身可 Tab 聚焦；不要用 div 替代。图标 aria-hidden="true"，文字在 .rail__label。

### 4.2 项目条

位置：page__head 之后、错误提示之前，独立一行，不塞进右上角 .sub-toolbar（工具按钮已有 11 个，塞进去会换行成两排）。

结构示意（class 名可微调，但元素 id 必须保留，测试要用）：

~~~html
<div class="sub-projects">
  <label class="sum-fld sub-projects__pick">
    <span class="k">PROJECT</span>
    <select id="sub-project-select"></select>
  </label>
  <label class="sum-fld sub-projects__search">
    <span class="k">搜索</span>
    <input id="sub-project-search" type="search" placeholder="标题 / UP / BV 号">
  </label>
  <button id="sub-reveal" class="sum-ghost" type="button">在文件夹中显示</button>
  <span id="sub-project-count" class="muted"></span>
</div>
~~~

行为：

- 首次加载：library.list({query:"", limit:50, offset:0})。
- 选项文本：title + 「（无字幕）」（当 has_transcript === false）；同名标题追加「 · 」+ 项目 id 后 4 位消歧。
- 项目总数 total > items.length 时，计数提示「共 N 个，输入关键词搜索」；否则「共 N 个项目」。
- 搜索：input 事件 250ms 防抖，调 library.list({query, limit:50, offset:0})；搜索只重建 select 的 options，不重建输入框（否则焦点丢失）。
- 当前项目不在返回页（项目很多或在搜索词外）：调 library.get({id:state.projectId})，成功则把该项目临时插到 options 首位，保持选中；失败则保留在 URL 里的 project 参数并显示错误，不静默换项目。
- 切换：change 事件调 switchProject(value)（见 4.5）。切换期间禁用 select / search / reveal，状态栏显示「保存中…」。
- sub-reveal：state.project && state.project.root_dir 为空时 disabled；点击调 WB.rpc("fs.reveal", { path: state.project.root_dir })。错误统一 toast（WB.rpc 默认会 toast，也可 silent:true 后自己 toast，但不要吞掉）。
- 所有项目标题/UP 名渲染走 textContent，禁止 HTML 拼接。

### 4.3 默认项目与空态

无 ctx.params.project 时：

1. 等 library.list 返回。
2. items.find(x => x.has_transcript === true)；没有则 items[0]。
3. 选中后调 WB.router.replace("subtitles", { project: id })，由路由重新渲染该项目的完整页面。
4. items.length === 0：渲染空态卡片：
   - 文案：「还没有项目。先去「新建任务」下载，或在「素材库」导入本地视频。」
   - 按钮「去素材库」调 WB.router.go("library")。
   - 不渲染 .sub-grid（没有项目时编辑区无意义），但保留项目条（select 禁用、reveal 禁用）。
5. library.list 失败：显示错误卡片「项目列表加载失败：<message>」+「重试」按钮；不自动进入任何项目。

带 project 参数时：

- 直接 library.get + subtitle.get 加载；项目不存在显示「项目不存在或已删除」+「回素材库」按钮。
- 项目没有字幕（has_transcript === false）：仍打开编辑器（可以手动新建/导入？现有页面显示「还没有字幕，先回素材库转录」），保持现有行为；项目选择器仍可用，方便换有字幕的项目。

### 4.4 「在文件夹中显示」

- 只传 path: state.project.root_dir。
- 不传 project_id；不给 RevealParams 加字段。
- 若 state.project.root_dir 为空：按钮 disabled，title 提示「这个项目没有记录根目录」。
- 若后端返回 VALIDATION（旧 media_root 项目不在白名单）：toast 显示后端原话；这是已知限制，不在本单扩白名单。
- fs_reveal 的 Windows 实现是打开目录（若 path 是文件则打开其父目录），所以传根目录正好。

### 4.5 切换项目状态机

switchProject(nextId)：

1. 若 nextId === state.projectId：把 select 回弹到当前值，return。
2. 记录 oldId = state.projectId；禁用项目条控件；状态栏「保存中…」。
3. flushDraft()：
   - 如果当前没有 dirty：resolve。
   - 如果有 dirty：立即保存（不等 700ms 自动保存），保存成功才 resolve。
   - 保存失败：reject，保持旧项目，state.error = "切换失败：字幕还没保存成功（<message>）"，select 回弹 oldId，重新启用控件。**不得继续切换。**
4. 成功后：WB.router.replace("subtitles", { project: nextId })。
5. 路由重新渲染新项目页面；旧页面的 destroy() 会再次尝试 flush（此时 dirty 已为 false，不会重复写）。

### 4.6 请求代次（防止旧响应污染）

必须同时有：

- pageToken（模块级）：每次 render() 自增；destroy() 也自增。所有异步回调先比对 token === pageToken。
- state.loadSeq：每次 load(pid) 自增；回调比对 seq === state.loadSeq && pid === state.projectId && token === pageToken。
- state.peaksSeq：同上，给 loadPeaks(pid) 用。
- state.saveSeq / state.savePromise：保存串行化；响应只允许最新一次应用到 UI（旧响应可忽略，但不要丢服务端已成功的写）。
- runOp / 轮询（pollOpt / pollTrans / pollExport）：进入时捕获 pid + token；回调不匹配就只清理自己的计时器/状态，不 applyPayload、不改当前页面。

### 4.7 播放器与波形生命周期

- 新增 disposeMedia()：暂停 video、清 src、load()、置 video = null、videoProjectId = ""、wave = null（波形没有 destroy API，靠 canvas 随 DOM 移除）。
- render() 开头调用 disposeMedia() 并移除上一次的 resize 监听（保存 handler 引用）。
- mountMedia() 里先 host.innerHTML = ""；只在 videoProjectId !== state.projectId 或没有 video 时创建新 video；创建后记录 videoProjectId。
- 同一项目同一页面内重复 mountMedia()（例如手动刷新）可复用现有 video；跨项目必须重建。
- 不要求跨页面保留播放位置；离开页面再回来从头开始是允许的，但**不能出现没有播放器**。

### 4.8 页面离开

- 路由新增可选 definition.destroy()，在 renderRoute 里替换视图之前调用；destroy 抛异常只 console.warn，不阻断路由。
- 字幕页 destroy()：
  1. pageToken += 1；
  2. clearTimeout(saveTimer)；
  3. 若 dirty && projectId，用当前快照发一次 subtitle.save（不等待、不更新已销毁的 UI；失败 toast「字幕保存失败：…」）；
  4. disposeMedia()；
  5. 移除 resize 监听。
- 注意：destroy() 不能依赖 nodes.page（视图即将被清空）；保存用快照与 projectId，不要调 save() 里会写 UI 的分支。

### 4.9 错误与空态清单

| 场景 | 表现 |
|---|---|
| 没有任何项目 | 空态卡片 + 「去素材库」按钮 |
| 项目列表加载失败 | 错误卡片 + 「重试」按钮 |
| URL 里的项目不存在 | 「项目不存在或已删除」+ 「回素材库」按钮 |
| 项目无字幕 | 编辑器仍打开，显示现有提示「还没有字幕，先回素材库转录」 |
| 切换时保存失败 | 阻断切换，留在旧项目，错误显示在页面顶部，select 回弹 |
| 自动保存失败 | 状态栏「有未保存的改动」+ 页面顶部错误；不静默丢 |
| reveal 越界/不存在 | toast 后端原话，不重试、不换路径 |

---

## 5. 技术设计（文件级）

### 5.1 video_workbench/web/js/router.js

1. **新增 destroy 支持**：在 renderRoute(parsed) 开头，取 previous = current && routes[current.route]；若 previous && typeof previous.destroy === "function"，在 try/catch 里调用 previous.destroy()。然后再 current = {route, params}、清空 view、调用新 render。
2. **新增 router.replace(route, params)**：与 go 拼同样的 hash；优先 history.replaceState(null, "", next)，失败时 location.replace(next)；然后 renderRoute({route, params, hash: next})。不要改 go 的现有语义（测试会依赖 go 同路由重渲染）。
3. **左栏同路由 no-op**：只改 renderRail 的 onclick：

~~~js
onclick: function () {
  if (current && current.route === item.route) return;
  router.go(item.route);
}
~~~

不要把这个判断放进 router.go（会破坏测试里用 go 强制重载当前页的用法）。

### 5.2 video_workbench/web/js/pages/subtitles.js

#### 5.2.1 新增 state / 模块变量

~~~js
// state 新增
projects: [],
projectTotal: 0,
projectQuery: "",
projectLoading: false,
loadSeq: 0,
peaksSeq: 0,
saveSeq: 0

// 模块级新增
var pageToken = 0;
var resizeHandler = null;
var videoProjectId = "";
var saveChain = Promise.resolve();
~~~

#### 5.2.2 新增/改造函数

- resetForProject()：重置所有项目级状态（project / segments / qc / issues / stats / selected / activeId / editingId / undo / redo / dirty / saving / savedAt / scrollTop / follow / loading / error / pending / opt* / trans* / exp*），但保留 projects / projectTotal / projectQuery（列表数据跨项目共用）。
- disposeMedia()：见 4.7。
- detachResize()：if (resizeHandler) window.removeEventListener("resize", resizeHandler); resizeHandler = null;
- loadProjectOptions(query)：调 library.list；处理当前项目不在列表时的 library.get 补位；只更新 state.projects / state.projectTotal / paintProjectBar()。
- defaultProjectId(items)：先找 has_transcript，再 items[0]。
- paintProjectBar()：重建 #sub-project-select 的 options；更新 #sub-project-count；设置 #sub-reveal disabled。
- switchProject(nextId)：见 4.5。
- flushDraft()：state.dirty 为真时调 save()；若 state.savePromise 在飞，先等它，再检查是否仍有 dirty。
- init(token)：加载项目列表 → 无参数时选默认项目并 router.replace；有参数时 load(token)；空列表显示空态。
- destroy()：见 4.8。

#### 5.2.3 改造 render(ctx)

- 进入时 pageToken += 1，保存 var token = pageToken。
- 调用 disposeMedia() / detachResize() / clearTimeout(saveTimer) / resetForProject()。
- 构建页面外壳（page head、项目条、错误容器、grid、底部状态、opt/trans/export 面板）。
- 无 state.projectId 时先不渲染 grid 内容；由 init(token) 决定是自动选项目（路由 replace）还是显示空态。
- setTimeout 绑定 DOM 后：
  - 创建波形并保存 resizeHandler；
  - 绑定列表滚动/键盘；
  - paint()；
  - init(token)。

#### 5.2.4 改造 load()

~~~js
function load(token) {
  var pid = state.projectId;
  var seq = ++state.loadSeq;
  state.loading = true;
  state.error = "";
  paint();
  Promise.all([
    call("subtitle.get", { project_id: pid }),
    call("library.get", { id: pid })
  ]).then(function (results) {
    if (token !== pageToken || seq !== state.loadSeq || pid !== state.projectId) return;
    applyPayload(results[0] || {});
    state.project = (results[1] && results[1].project) || state.project;
    state.loading = false;
    state.error = results[0] && results[0].has_transcript ? "" : "还没有字幕，先回素材库转录";
    paint();
    mountMedia();
    loadPeaks(pid, token);
    if (state.segments.length && !state.activeId) {
      setActive(state.segments[0].id, false);
      renderRows(true);
    }
  }).catch(function (err) {
    if (token !== pageToken || seq !== state.loadSeq || pid !== state.projectId) return;
    state.loading = false;
    state.error = (err && err.message) || String(err);
    paint();
  });
}
~~~

注意：library.get 不再 .catch(() => null) 静默吞错；项目不存在要让用户看见。若担心单个接口失败导致整页失败，可改 Promise.allSettled，但必须明确哪些字段可缺、哪些错误必须显示；本规划建议直接显示错误。

#### 5.2.5 改造 save() / flushDraft()

要求：

- 保存时捕获 pid / token / 快照 segments；请求发出后不改这些变量。
- 同一时刻最多一个 subtitle.save 在飞；所有保存走 saveChain 串行，避免旧快照后到覆盖新快照。
- 回调中 pid !== state.projectId || token !== pageToken 时：不更新 UI；如果失败，仍 toast 一次（用户在别的页面也要知道没保存上）。
- 保存成功：只有当前项目才更新 state.qc/issues/stats/savedAt、重绘行。
- 保存失败：当前项目设 state.dirty = true、state.error = "保存失败：<message>"；切换场景由 flushDraft() reject 给 switchProject。

#### 5.2.6 改造 runOp() / 轮询

- runOp 捕获 pid/token；返回时若已切项目，直接 return data，不 applyPayload、不改 state.selected/activeId。
- pollOpt(tries) / pollTrans(tries) / pollExport(tries) 捕获启动时的 pid/token；回调不匹配时清理自己的 timer / job 状态并 return，不写当前页面。
- 注意：服务端任务不会因切页停止；用户回到原项目时可能看不到正在跑的任务。这是允许的已知行为，但不要让它污染新项目。

#### 5.2.7 改造 mountMedia()

- 先 host.innerHTML = ""。
- 找不到 source 时显示「找不到源文件，只能编辑文本」。
- 若 video && videoProjectId === state.projectId，可直接复用（同页重挂）。
- 否则创建 video，设 videoProjectId = state.projectId；旧 video 由 disposeMedia() 清理。
- onloadedmetadata 里更新 nodes.note 与 mountTicks()。

#### 5.2.8 暴露给测试

在 WB.pages.subtitles._internals 增加：

- flushDraft
- switchProject
- defaultProjectId
- disposeMedia

这些只是测试把手，不改生产行为；不要暴露内部可变对象之外的额外能力。

### 5.3 video_workbench/web/js/components/ui.js

在 ICONS 增加：

~~~js
subtitles: '<rect x="2" y="3" width="12" height="10" rx="1.2"/><path d="M4.5 6.5h7M4.5 9.5h4.5" stroke-linecap="round"/>'
~~~

风格与现有图标一致：16×16、无填充、stroke="currentColor"、stroke-width="1.5"。不要用 emoji，不要手画复杂 SVG。

### 5.4 video_workbench/web/css/app.css

在字幕样式区新增（复用总结页的 .sum-fields / .sum-fld 视觉语言，不复制颜色值）：

~~~css
.sub-projects {
  display: flex;
  gap: var(--gap-2);
  align-items: center;
  flex-wrap: wrap;
}
.sub-projects__pick { flex: 1 1 320px; }
.sub-projects__search { flex: 0 1 260px; }
.sub-projects .sum-fld { height: 38px; }
.sub-projects__pick select { max-width: 100%; }
.sub-projects > .muted { font-size: 12px; }
~~~

要求：

- 在 1100×720（产品 MIN_SIZE）下不横向溢出；换行时按钮仍可点。
- 不覆盖 .sum-fld 的全局样式（只作用于 .sub-projects 内）。
- 不给项目条加渐变/发光/彩色竖条；保持现有黑白灰 + 酸绿色强调。

### 5.5 tests/wb/frontend_test.js

1. 更新左栏断言：

~~~js
assert.strictEqual(items.length, 7);
assert.deepStrictEqual(order, ["新建任务","素材库","字幕","总结","切片台","合并","设置"]);
~~~

2. 新增用例（mock 模式，按现有 check() 风格）：

- **左栏入口与默认项目**：WB.router.go("subtitles")（无参数）→ 等 200ms；断言 WB.router.current().params.project === "p_demo_1"（mock 第一个有字幕项目）；断言 #sub-project-select 存在且值 p_demo_1；断言行列表非空（strangers 文本出现）。
- **项目条切换用 replace**：进入 p_demo_1；记录 location.hash；选择 p_demo_2 并 dispatch change；等 300ms；断言 hash 含 project=p_demo_2；断言 history.length 没有增加（在 mock 初始页记录一次，切换后比一次；若 WebView2 行为不稳定，至少断言路由当前项目正确且没有重复历史条目）。
- **切换项目会先保存旧草稿**：进入 p_demo_1；双击第一行 → 输入「切换前保存测试」→ Enter；立即把 select 切到 p_demo_2（不等 700ms）；等 500ms；await WB.rpc("subtitle.get",{project_id:"p_demo_1"}) 断言文本是「切换前保存测试」；切回 p_demo_1，断言行列表出现该文本；测试结束恢复原始字幕（subtitle.save 写回 before.segments）。
- **保存失败时阻断切换**：进入 p_demo_1；临时包装 WB.rpc：当 method === "subtitle.save" 时 Promise.reject(new WB.RpcError("PROCESS_FAILED","模拟保存失败"))；改动第一行文本并切到 p_demo_2；等 300ms；断言仍在 p_demo_1，select 的值回到 p_demo_1，页面出现「切换失败」；恢复 WB.rpc，把字幕写回原值。
- **离开字幕页会 flush**：进入 p_demo_1，改第一行，不等自动保存，立即 WB.router.go("library")；等 300ms；subtitle.get(p_demo_1) 断言改动已保存；恢复原字幕。
- **重复进入播放器不消失**：WB.router.go("subtitles",{project:"p_demo_1"})；等 200ms；断言 #sub-video video 数量 1；WB.router.go("library") 再回 subtitles；断言 #sub-video video 数量仍为 1（旧实现会因为模块级 video 非空而跳过挂载，导致 0 个）。
- **reveal 传 root_dir 而不是 project_id**：包装 WB.rpc 记录 fs.reveal 的 params；点击 #sub-reveal；断言 params.path === p_demo_1.root_dir，且没有 project_id 键。

3. 兼容性注意：现有测试里 WB.router.go("subtitles",{project:"p_demo_1"}) 会触发新 destroy，如果上一条测试留下了 dirty 状态，会先保存。每个用例结束要复位 mock 字幕（现有测试已有部分复位，新增用例也要）。不要修改 WB.isMock() / ?mock=1 机制。

### 5.6 新增 tests/wb/test_subtitles_real_backend_ui.py

目的：用真 WorkbenchService + 真 Chromium 验证「项目选择 / 切换 / reveal / 竞态」不是 mock 的假象。参考 tests/wb/test_summary_real_backend_ui.py 的 _temp_config / _Service / page.expose_binding 结构。

夹具：

- _temp_config(tmp_path)：复制仓库 config.yaml，只改 storage.db_path / storage.key_path 到 Temp（照抄现有函数）。
- 建两个项目：直接 service.svc.repo.project_create(...)，字段至少 id/title/up_name/root_dir/stage；根目录用 Temp 下两个目录；每个目录写 transcript.json（也要写 frames/thumb.jpg 可省）。
- 用 service.call("subtitle.save",{project_id, segments:[...]}) 写两份不同的字幕（服务端会同时写 DB 和 transcript.json）。
- 页面只把 library.list / library.get / subtitle.get / subtitle.save / fs.reveal 接到真 service，其余走 mock；fs.reveal 用 monkeypatch 把 os.startfile 换成记录函数，避免真的开 Explorer。

判据（至少）：

1. #/subtitles 无参数打开 → #sub-project-select 值 = 第一个 has_transcript 的项目 id；列表显示该项目字幕。
2. 切换项目 → router.current().params.project 变新项目，列表文本变新项目文本；旧项目请求不会再覆盖（把 subtitle.get 包一层延迟，先打开 A 再立即打开 B，等延迟结束断言 B 的文本仍在）。
3. 切换前编辑未保存 → 切到 B 后 subtitle.get(A) 能读到改动（flush 生效）。
4. 保存失败（monkeypatch subtitle.save 返回错误）→ 切换被阻断，停留 A，select 回弹 A。
5. #sub-reveal 点击 → 记录到的 fs.reveal path 等于 A 的 root_dir，且没有 project_id。
6. 浏览器后退：从 library 进入 subtitles 并切换 A→B 后，page.go_back() 应回到 library（不是 A）。如果 Playwright 在 file:// 下不稳定，至少断言 history.length 不随 A→B 增加，并把该限制写清楚。

### 5.7 文档

- docs/用户手册.md：左栏「五项」改「六项」，加入「字幕」；在字幕章节写清「左栏入口 / 项目选择 / 在文件夹中显示 / 切换会先保存」。
- README-VideoWorkbench.md：左栏说明从「五项：…/发布」改为「六项：新建任务 / 素材库 / 字幕 / 总结 / 切片台 / 合并 / 发布」+ 底部设置（注意原文本身把「发布」列在五项里但实际是 6 个业务入口，按实际写）。
- 不修改 方案书.md（历史设计文档，不改写历史）。

---

## 6. 任务拆分与执行顺序

> 每一阶段完成后跑对应测试并记录命令原文；不要一次改完再统一验证。

### 阶段 0：基线（只读 + 记录）

1. npm run test:wb（或 node tests/wb/frontend_test.js）记录当前通过数与耗时。
2. .venv/Scripts/python.exe -m pytest tests/wb -q 记录当前全量结果（若用户要求不跑全量，按用户当轮指令记录「未跑」）。
3. 读 docs/用户手册.md 左栏段落、README-VideoWorkbench.md 左栏段落，记下需要改的句子。
4. 读 tests/wb/frontend_test.js 左栏断言与字幕测试，确认改动会红的位置。

**产出**：基线读数（命令 + 通过/失败数）。

### 阶段 1（P0）：路由生命周期 + 字幕页状态机

1. router.js：destroy 支持 + replace + 左栏同路由 no-op。
2. subtitles.js：pageToken / resetForProject / disposeMedia / detachResize / destroy；render 接入。
3. subtitles.js：load / loadPeaks / save / runOp / 轮询加请求代次。
4. subtitles.js：mountMedia 重建逻辑。

**验收**：

- 手动改 mock：连续 WB.router.go("subtitles",{project:"p_demo_1"}) → go("library") → 回字幕，#sub-video video 始终 1 个。
- 快速切换 project 时，旧响应不会出现在新项目（可用 5.6 第 2 条先做临时探针）。
- 未保存改动离开页面后，subtitle.get 能读到。

### 阶段 2（P1）：左栏入口 + 项目条 + reveal

1. ui.js 加 subtitles 图标。
2. subtitles.js 注册 nav:true。
3. 增加项目条 DOM、loadProjectOptions / paintProjectBar / switchProject / defaultProjectId / init。
4. app.css 加 .sub-projects 样式。
5. 空态 / 错误态 / reveal 按钮。

**验收**：

- 左栏 7 项、顺序正确、图标可见、当前页高亮。
- 无参数进入自动选 p_demo_1（mock）。
- 有参数进入指定项目；项目不存在显示错误。
- 切换项目先保存、失败阻断；成功后 hash 是 replace（不增加历史）。
- reveal 传 root_dir。

### 阶段 3（P1）：测试

1. 更新 frontend_test.js 左栏断言。
2. 增加 5.5 的 7 条 mock 用例。
3. 新增 tests/wb/test_subtitles_real_backend_ui.py（至少 1-6 条判据；按实际环境可先做 1/2/3/4/5，后退那条如 file:// 不稳定则记录限制并保留 mock 断言）。
4. 每条测试都先确认「改坏产品会红」（牙齿），再恢复。

**验收**：

- node tests/wb/frontend_test.js 全绿，输出实际条数。
- .venv/Scripts/python.exe -m pytest tests/wb/test_subtitles_real_backend_ui.py -q 全绿（或明确 skip 原因，不能写成通过）。

### 阶段 4（P2）：文档 + 全量验证

1. 更新 docs/用户手册.md 与 README-VideoWorkbench.md。
2. 跑 node tests/wb/frontend_test.js + .venv/Scripts/python.exe -m pytest tests/wb -q（按用户当轮要求）。
3. 真 GUI 截图（默认 1360×860 与最小 1100×720）：左栏字幕入口；字幕页项目条（含搜索、reveal）；空态；切换项目后的页面。
4. ruff（若改了 Python；本单原则上不改 Python，若改了测试文件则跑对应范围）按 AGENTS.md 第三点五节给出命令原文 + 范围 + 剩余条数。

---

## 7. 验收标准（AC）

> 每条都要有对应测试或截图；没有证据的条不算通过。

| ID | 验收标准 | 证据 |
|---|---|---|
| AC-01 | 左栏出现「字幕」，位于「素材库」与「总结」之间，图标非空 | frontend_test.js 断言 7 项 + 顺序；截图 |
| AC-02 | 点击左栏「字幕」打开字幕页，不要求先选项目 | WB.router.go("subtitles") 测试 |
| AC-03 | 无参数打开时默认选第一个 has_transcript 项目，否则第一个；没有项目显示空态 | mock + real backend 测试 |
| AC-04 | 项目下拉列出项目，显示标题 + 无字幕标记；支持关键词搜索（防抖 250ms） | mock 测试 + 截图 |
| AC-05 | 切换项目前自动保存旧项目草稿；切换后旧项目能读到改动 | real backend 测试 / mock 测试 |
| AC-06 | 保存失败时阻断切换，select 回弹，页面显示错误，旧项目不变 | real backend 测试 |
| AC-07 | 快速连续切换项目时，旧请求不覆盖新项目内容 | real backend 测试（延迟 subtitle.get） |
| AC-08 | 字幕页离开后再次进入，播放器仍然存在（不消失） | mock 测试 #sub-video video 数量 |
| AC-09 | 「在文件夹中显示」调用 fs.reveal({path: root_dir})，没有 project_id | mock 记录参数 + real backend 记录 path |
| AC-10 | 项目 root_dir 为空时按钮 disabled；reveal 失败 toast 后端原话 | mock 测试 / 手动 |
| AC-11 | 项目切换使用 replace，不增加浏览器历史；Back 回到进入字幕页前的页面 | mock 断言 + real Playwright（如可用） |
| AC-12 | 项目标题/UP 名等外部文本不经过 innerHTML | 代码审查 + 现有 h({text}) 规范 |
| AC-13 | 无项目 / 列表加载失败 / 项目不存在 三种空态都有可行动按钮 | mock 测试 + 截图 |
| AC-14 | 左栏 1100×720 下不溢出、项目条控件可见可点 | Playwright 截图 + getBoundingClientRect 断言 |
| AC-15 | 现有字幕功能（编辑/撤销/重做/AI 优化/翻译/导出）不回归 | 现有 frontend_test.js 全部字幕用例通过 |

---

## 8. 测试命令与证据格式

每条交付都按这个格式写：

~~~
做了什么：一句话
测试：<命令原文> → 通过 N，失败 0
截图：<路径>
没验证到的：哪些、为什么；没有就写「无」
~~~

本单相关命令：

~~~bash
# 前端 mock 测试
node tests/wb/frontend_test.js

# 真后端 UI 测试（新增）
.venv/Scripts/python.exe -m pytest tests/wb/test_subtitles_real_backend_ui.py -q

# 全量（按用户当轮要求；没跑就写未跑）
.venv/Scripts/python.exe -m pytest tests/wb -q

# ruff（若改 Python）
.venv/Scripts/ruff.exe check <范围>
.venv/Scripts/ruff.exe format --check --diff <范围>
~~~

真实 GUI 启动：

~~~bash
.venv/Scripts/python.exe -m video_workbench.desktop --config <Temp 配置路径>
~~~

隔离要求（照 AGENTS.md 第四节）：只重定向 HOME / USERPROFILE，保留真实 APPDATA / LOCALAPPDATA；跑前跑后记录三个真实配置文件的 sha256。

---

## 9. 风险与边界

| 风险 | 说明 | 处置 |
|---|---|---|
| R-01 生命周期 | 字幕页从二级页变一级页后，原模块级 video / wave / saveTimer / resize 监听会跨渲染残留 | 阶段 1 的 destroy + disposeMedia + detachResize；测试 AC-08 |
| R-02 旧响应污染 | load / loadPeaks / save / runOp / 轮询都无项目代次 | pageToken + loadSeq + saveSeq；测试 AC-07 |
| R-03 丢草稿 | autosave 700ms，切项目/离开页面时定时器被清 | flushDraft + destroy 补存；测试 AC-05 |
| R-04 保存竞态 | 两次 save 并发可能旧快照后到 | saveChain 串行；测试 AC-05 |
| R-05 旧 media_root | fs_reveal 白名单只认当前配置的 media_root，旧项目可能 VALIDATION | 不改后端；UI toast 原话；写入已知限制 |
| R-06 file:// history | history.replaceState 在旧 WebView2 可能失败 | try/catch + location.replace；测试 AC-11 如不稳定则记录限制 |
| R-07 大项目列表 | >200 项目时只加载前 50，搜索才查更多 | 计数提示「共 N 个，输入关键词搜索」；不一次拉全量 |
| R-08 CSP | 页面 connect-src 'none'，不能 fetch；所有调用走 pywebview 桥 | 测试沿用 expose_binding，不放宽 CSP |
| R-09 外部文本注入 | 项目标题/UP 名可能含 HTML | 只用 textContent / ui.h({text})；代码审查 AC-12 |

---

## 10. 交付边界（不要顺手做）

- 不修 summary.js 合集 fs.reveal({project_id})。
- 不修 infra/files.py 的 is_within junction/symlink。
- 不加 RevealParams.project_id。
- 不做 /select Explorer 选中文件。
- 不改 bili_scribe/。
- 不动 scripts/sync_mirror.py。
- 不创建 commit / push（除非用户明确要求）。

---

## 11. 执行完成后的交付话术

按仓库 prove-it 格式：

~~~
做了什么：字幕模块左栏入口 + 项目选择 + 在文件夹中显示
测试：node tests/wb/frontend_test.js → 通过 N，失败 0；.venv/Scripts/python.exe -m pytest tests/wb/test_subtitles_real_backend_ui.py -q → 通过 M，失败 0
截图：<默认宽度 + MIN_SIZE 两张路径>
没验证到的：<例如：旧 media_root 项目 reveal、file:// 下 history 行为；没有就写无>
~~~

同时报告：

- 改动文件清单；
- 新增/修改测试清单；
- 已知限制（R-05 / R-06）；
- 未运行的检查（如用户说「不跑测试」）。
