# 字幕模块一级入口与项目选择 · 施工登记（2026-11-07）

> 对应规划书：`docs/plan-subtitles-entry.md`（v1.0，第 2 节四决策已拍板）。执行：lead 委派唯一写者子代理承接「软件架构师」职责，lead 复核。未 commit / 未 push。

## sha256 快照清单登记（AGENTS.md 第六节）

新建 tests/wb/*.py 登记在此，防止还原逻辑把「开工前不存在」的文件当垃圾删掉：

- `A tests/wb/test_subtitles_real_backend_ui.py`（已 staged，未 commit；497 行，原始字节 sha256 = `cb5ddfe912f23f1396d4f582cc5685a8fb760e409e52b284e091b04e648113d5`）

## 开工基线（2026-11-07 17:0x）

工作区已有上一轮「合并模块输出目录 / 桌面选文件夹」的未提交在途改动（`M` service.py / core/merger.py / desktop.py / merge.js / merge.css / mock.js / clips.js / test_merge_quick.py / test_merger.py / test_rpc.py / frontend_test.js 内 +321 行 / docs/用户手册.md / docs/acceptance/p4/README.md；`A` test_desktop_picker.py、scripts/accept_folder_picker.py）。本单只做叠加，未还原任何在途内容。

## 交付读数（每数字附产生它的命令）

- `node tests/wb/frontend_test.js` → rc=1，唯一 FAIL 为既有用例「服务商卡片：增删、设为当前、折叠区与保存」（开工基线即红，与本单改动面零交集）；`ok 左栏渲染出 7 个入口且顺序稳定`、无参数默认项目、离页 flush、播放器不消失等新增用例全绿（lead 复跑）。
- `.venv/Scripts/python.exe -m pytest tests/wb/test_subtitles_real_backend_ui.py -q` → 8 passed in 6.42s（lead 复跑，补齐 USERPROFILE 后；首次 rc=2 系 run_code 子进程缺 HOME/USERPROFILE，按「rc∉(0,1) 不读成红」处理）。
- `.venv/Scripts/python.exe -m pytest tests/wb -q`（全量）→ 1809 passed, 2 failed, 1 skipped，约 13:18（子代理跑）。两失败均非本单：① test_git_no_normalization_on_locked_paths::autocrlf —— 命令未前置 Git for Windows 的环境假红（AGENTS.md 第三节五预告该行为），前置后复跑该文件 38 passed；② test_summary_error_display 全量下漂移红（开工基线红 untouched_copy、终跑红 anywhere），单跑前置 Git 后 32 passed 全绿。
- `ruff check` + `format --check`（范围=新测试文件）→ All checks passed / 1 file already formatted。
- router.js `replace` 定义于 :99（history.replaceState，:108-114 有 location.replace 兜底）；renderRail 同路由 no-op 在场。
- 截图：`Temp/subtitles-1360x860.png`、`Temp/subtitles-1100x720.png`（脚本 `Temp/wb_sub_shots.py`）；AC 探针 `Temp/wb_sub_ac_probe.js`（AC-04/10/13 PASS）。

## 规划外既有缺陷（未修，待用户拍板）

`video_workbench/web/js/pages/subtitles.js:1666-1681`：行 `onmousedown` 末尾**无条件** `renderRows(true)`（:1680），真实指针序列中 mousedown 目标被同步重建 detach ⇒ Chromium 不合成 click/dblclick ⇒ 桌面壳里双击编辑失效；jsdom 合成事件掩盖了它（:1674 注释「放行给双击编辑」的意图恰好被这行拆台）。探针证据：`Temp/wb_sub_dblclick_probe2.py`（真实 chromium 只收到 mousedown+mouseup，零 click/dblclick）。属规划 §10 之外，一行修复（非拖拽路径不重建）但未动手。
