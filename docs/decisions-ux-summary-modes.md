# 决策：总结页与轻量总结拆成两条独立界面

- 日期：2026-11-13
- 范围：`video_workbench/web/js/pages/summary.js`、`summary.css`、`app.css`、`publish.js`

## 背景

轻量总结原本是 `summary.js` 里 `state.source` 的一个取值，和本地项目共用同一个 `state`、同一个 `#sum-head`、同一批项目/正文渲染。结果是：

- 从本地项目切到轻量后，顶部显示轻量 BV 信息，正文却仍渲染上一个本地项目；
- 轻量解析出的 `pj_light_demo` 会改写本地页的 `state.projectId`；
- 轻量工具栏没有角色/模型选择，只能继承本地页上一次的选择；
- 长视频标题与 AV/BV 输入框同排时，固定 `min-width: 220px` 的输入框会冲出 `.sum-fld`（1525 宽实测溢出 113px）。

## 决策

1. **`#/summary` 只负责本地项目总结，`#/light` 只负责轻量总结。**
   `summary.js` 用 `createSummaryPage(kind)` 创建两个独立实例（`WB.pages.summary` / `WB.pages.summaryLight`），各自持有 `state`、`pollToken`、`errorOpenText`、项目与结果。左栏新增「轻量总结」一级入口，顺序在「总结」之后。
2. **轻量页不读本地项目列表。** 首次渲染只加载 `summary.roles` / `summary.models`；没有 `state.projectId` 时不挂本地正文（`docHead`）、不挂发布卡、不挂本地总结的版本/打磨/用量侧栏，也不显示「还没选素材」这类本地项目文案。
3. **轻量页补齐角色/模型栏。** `#sum-light-role` 与 `#sum-light-model` 分别来自 `summary.roles`、`summary.models`；点「提取字幕并总结」时 `summary.start` 显式携带本页选择的 `role` / `model`。
4. **轻量工具栏的版式合同：**
   - AV/BV、字幕轨、角色、模型可换行，但每个 `.sum-fld` 与其中 `.input` 必须能压缩，输入框不再设固定 `min-width`；
   - 视频标题/UP、解析提示、错误、已选文件名放进 `#sum-light-meta`，独占一行，长标题不参与字段行宽度竞争；
   - 全屏/窗口最大化按 viewport 变宽处理，验收覆盖 1100×720、1525×900、2306×1225、3840×1600 四档，要求输入框 `right <= .sum-fld.right` 且无横向滚动。
5. **发布卡跟随页面项目。** `publish.mount({ projectId })` 记录 `filterProjectId`，`publish.list` 按它过滤草稿；轻量页没有项目时不挂载，离开页面时 `unmount()` 清空过滤。这样两页的发布状态不会串。

## 非目标

- 不改 `summary.start` / `summary.light.*` 后端契约；
- 不改 `bili_scribe/`，不碰 `core/summary` 字节锁文件；
- 不重做总结正文的视觉设计。

## 验收

- `node tests/wb/frontend_test.js`：8 个左栏入口、两条独立路由、轻量 role/model 参数、两页 state 互不覆盖；
- `tests/wb/test_summary_light_ui.py`：真浏览器可见性 + 四档视口布局回归；
- `tests/wb/test_web_router_lifecycle.py`：两页 `destroy` 都释放轮询并 `publish.unmount`。
