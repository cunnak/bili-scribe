# 决策记录：配置搬家不改 `max_tokens`（用户拍板 C）

> 记录时间：2026-11-04 12:5x ｜ 记录人：lead（技术方案评审团 Team Lead）
> 触发：真桌面首启验收实测出的「新装用户首启即挂」风险，摆 A/B/C 三选后用户答「C 什么都不做」。

## 结论

**搬家保持「逐字段原样搬运」，不改任何配置值。** 具体三条禁令，对所有成员生效：

1. 不许在搬家分支里对 `llm.max_tokens` 做下限抬升、钳制或改写。
2. 不许在 task-29 的提示文案里夹带「我们帮你把预算调大了」这类没做的事，也不许夹带自动修复动作。
3. 不许为这条风险新增任何"顺手兜底"的代码路径（例如检测到 4096 就静默改配置）。

## 理由

- 搬家那行日志的口径是产品对用户的承诺：
  `首次启动：配置已从旧家…搬到新家…（逐字段原样搬运，没有改任何值）；旧家是 bili-scribe 的文件，此后只读、不再被写入`
  一旦我们"好心"改值，这句承诺就变成假话，而它正是这套搬家设计唯一的安全边界来源（旧家只读、新家忠实副本，用户敢信）。
- 代价有界，且**不是静默失败**。`video_workbench/infra/llm.py:505-510` 在「空正文 + 有 reasoning」时抛：
  - 主句：`模型这次只输出了思考过程，正文是空的（预算被思考吃掉了）`
  - 详情：`finish_reason=%s，思考用了 %s tokens。把 max_tokens 调大，或换一个非推理模型，再试。`
  ⇒ 用户看到的是可执行建议，不是"不知道为什么挂"。我上一轮把 C 的代价写成「而且不知道原因」**说过头了**，在此更正。
- `app/service.py:623` 把 `LLM_EMPTY` 列进 `keep` 集合，这条错误码不会被降级吞掉。

## 已知事实（本轮实测，非推断）

| 项 | 读数 |
|---|---|
| 首启搬家是否发生 | 真桌面窗口（pid 47728，`VideoWorkbench` 1360×860，pywebview 桥 `desktop.py:142`）实测发生 |
| 搬家产物 | `~/.video-workbench/config.yaml` 4502B + `workbench.json` 4186B（12:28） |
| 旧家是否被写 | 未写（mtime 停在夹具生成的 12:27；真实旧家 sha `d4576e6cb0432a4c` / `0afac4091c905abc` 全程未变） |
| 搬进新家的值 | `model: workbuddy/deepseek-v4.1-flash`、`max_tokens: 4096`、`role: standard` |
| 4096 下真实行为 | flash 也在思考；完整链路第 3 轮 `LLM_EMPTY`（抬到 16384 后成功，97.1s / 3 轮） |
| 密钥是否泄漏 | 哨兵 `sk-LEAKTEST-Do-Never-Show-7C11` 在日志目录 `grep -r` 0 命中 |
| 本机真实旧家是否有脏键 | **没有**：`foreign_keys = []`、`AppConfig.load` 不抛 ⇒ 这台机不会弹搬家提示（不误报） |

## 待定（不在本决策范围内，仍等用户）

- `role` / `max_tokens` 是否从与 bili-scribe 共用的 `config.yaml` 彻底拆出（新家已经拆了配置文件，但 `storage.db_path` / `notes_dir` 仍与 bili 共用同一个库）。
- 无源的三个角色（`xueshu` / `renhua` / `shensi`）要不要补写 txt 真源。
- 设置页「深度思考」空控件的处置（task-26 已排：WB 界面撤控件，键留着，bs 侧不动）。

## 证据归档

- 截图：`build/accept/wb_firstlaunch_desktop_real_library.png`
- 首启日志副本：`C:/Users/b1596/AppData/Local/Temp/lead/evidence/wb_firstlaunch.log`
- 相关任务：task-22（搬家）、task-23（搬家回归锁）、task-29（一次性提示，rev4）、task-30（提示用例）
- 记忆系统归档**未成功**：`hindsight_ingest_document` 返回
  `POST https://api.hindsight.vectorize.io/v1/default/banks/coding-agent%3A%3A视频内容总结/memories -> 401 {"detail":"Authentication failed: API key required"}`；
  本会话亦无 `cg` 写入工具（`ops_discover_tools` 只选中 `ops_write_file`）。故本文件是唯一归档载体，**没有节点 id 可给**。
