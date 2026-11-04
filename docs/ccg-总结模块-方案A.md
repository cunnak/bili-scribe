# CCG 间歇归档 · VideoWorkbench 总结模块（方案 A：txt 为真源）

> 归档时间：2026-11-04 09:0x（第 10 轮交互触发的间歇归档）
>
> 落盘位置说明：本应写入 Hindsight 长期记忆（`content_kind=text`），该服务返回
> `401 {"detail":"Authentication failed: API key required"}`（bank
> `coding-agent::视频内容总结`，endpoint `api.hindsight.vectorize.io`），本会话也没有
> `cg` 工具（`ops_discover_tools` 报 "No allowed, implemented capability matched"）。
> 因此**这条归档没有进入记忆系统**，先以文件形式持久化。修好 Hindsight 的 API key 后
> 可直接把本文整篇 ingest 一次。

---

## 1. 功能名

VideoWorkbench 总结模块 = `bili_scribe` 总结方法的**逐字节锁定副本**（vendored copy）
+ 提示词真源改指 `*.txt`（方案 A）+ `standard` 角色退役。

## 2. 生效条件

| 条件 | 位置 | 由什么守住 |
|---|---|---|
| 副本与原件逐字节一致，唯一允许差异 = import 段（行号合同 9–23） | `video_workbench/core/summary/{summarizer.py,roles.py}` vs `bili_scribe/core/` | `tests/wb/test_summary_drift.py`（14 条） |
| 只有 `prompt_source` 读写 txt | `video_workbench/core/prompt_source.py` | AST 扫 `role.system =` 写入点，允许文件集合 `{core/prompt_source.py, core/summary/__init__.py}` |
| 绑定只在装配期跑一次 ⇒ **改 txt 要重启才生效** | `core/summary/__init__.py:83 PROMPT_BIND = prompt_source.bind(_roles)` | 已量到，未钉成"应当热加载" |
| 唯一路径覆盖口 = 环境变量 `WB_PROMPTS_DIR`（每次现读） | `prompt_source.prompt_dir()` | `test_summary_prompt_source.py` P4 走真实回落，不退化 monkeypatch |
| 拼接顺序定死：`txt + (extra 仅当开关开) + OUTPUT_CONTRACT`，契约永远最后 | `prompt_source.system_for()` | 同上 P5 |
| 角色白名单 7 个；`standard` 退役 | `core/summary/__init__.py`（`RETIRED_ROLES`、`get_role`） | `test_summary_role_resolution.py`（14 条） |
| 默认角色 = `wengao` | `video_workbench/config.py` `LlmConfig.role` 覆写 + `config.yaml:39` | `test_summary_drift.py` 配置默认值那条 |
| 严格对账档默认开 | `core/summarizer.py:_parity_strict()` | `test_summary_parity.py`（37 条）+ `test_summary_parity_teeth.py`（9 条） |
| 附加提示词 `llm.extra_prompt` 默认关 | `config.py`（只落 sidecar `workbench.json`，不进 `config.yaml`） | `test_summary_prompt_source.py` P9 五档 |

**一致性口径（重要）**：有源四角色（wengao/xiezuo/fenxi/jinglian）**不再与 bs 逐字比
system**——这是用户拍板方案 A 的必然结果，已登记为有意差异；仍一比一的维度是
调用次数、三轮阶段顺序、`Summary.to_dict()` 字段集合、timeline 上限、错误码。
无源三角色（shensi/xueshu/renhua）继续逐字比。

## 3. 子功能

- **prompt_source**：`prompt_dir / load / bind / capture_baseline / snapshot / restore /
  use_extra_prompt / extra_enabled / manifest`；`Role.prompt_source` 存**逐字文件名**
  （无源必须 `None`，不许空串）。
- **预读磁盘缓存**：`video_workbench/infra/notecache.py`（`CACHE_VERSION = 2`，与
  `bili_scribe/infra/notecache.py:43` 同值；文件名是 **64 位 sha256**，不是 `v`+20）；
  地址 = `make_key(body, source_label, recipe, MAP_SYSTEM)`，**不吃角色、不吃 extra**
  （预读发的是 `MAP_SYSTEM`，角色 system 与附加提示词与它无关）。
  生产接线：`app/service.py:5009` / `:5022` 递 `cache=self.notes, task_key=project_id`；
  `self.notes` 构造在 `service.py:681`。
- **角色取值单一 owner**：`core/summarizer.py:66 resolve_role(explicit, fallback)`；
  service 六处收敛 `:2492/:2560/:2625/:2770/:2831/:4934`；回落说明走 payload 键
  `role_notice`；只有"列清单"那一处允许 `except WBError → DEFAULT_ROLE`（设置页必须
  能把错值改回，生成路径照旧硬拒）。
- **前端**：`web/js/pages/settings.js` 角色下拉下面一行真源清单（文本全来自后端
  `summary.roles`）；附加提示词开关（`class="switch"`，**无 id**，自动化只能按文本定位）；
  `web/js/mock.js` 只补键形状、判定值留 `null`（判据原文写在 `mock.js:1521`）。
- **RPC 新增**：`summary.prompt.list`、`summary.notes.stats`、`summary.notes.clear`。

## 4. 执行（谁做了什么）

- 团队：lead + vendor-engineer（产品）+ parity-test-engineer（测试）；
  共享任务板 `task-1 … task-22` 全部 completed。
- lead 亲跑的实测：
  - 全量 `.venv/Scripts/python.exe -m pytest tests/wb -q` = **1461 passed / 1 skipped / 0 failed**；
  - `git status --porcelain -- bili_scribe` = **0 行**；
  - 真模型 9 用例：`workbuddy/deepseek-v4.1-flash` 在 `max_tokens=4096` 下 **9.5s**
    返回六字段齐全 JSON（system = `文稿编辑.txt` 全文 4012 字符）；
    `auto/hy4-preview` 在 4096 下正文恒空（思考吃光预算），16384 才可用（121.7s）。
  - 缓存真相：真实磁盘条目 `~/.bili-scribe/notes-cache` 8 个，内部 `v` 全是 2，
    `NoteCache.get(真实文件名当地址)` → **命中**（`session_hits=1`），
    且 bs 与 WB 的 `make_key` **同址**。
- 用户可见缺陷修复：设置页折叠组收起时内容**被绘制但不计入 scrollHeight**（HEAD 起
  就有，12+ 个字段看得见滚不到）→ `web/css/app.css` 改 `.adv__body{display:none}` +
  `.adv[open]` 才 flex；`tests/wb/test_settings_role_warning_ui.py` 加 N1/N2/N3。

## 5. 验证方式

| 要验的 | 命令/判据 |
|---|---|
| 逐字节锁 | `git diff --no-index --numstat bili_scribe/core/summary_summarizer wb副本`（roles.py 应 0 行；summarizer.py 只剩 `@@ -15,12 +15,12 @@`） |
| 写点唯一 | AST 扫 `role.system =`（当前 3 处，全在 `prompt_source.py:300/304/330`） |
| **真源被悄悄改一个字能否发现** | **只有 P1 的 sha256 登记表能发现**——pytest 每进程全新导入，bind 现读会让 role 与 txt 同步变化，"逐字相等"那条不红。P1 是这套锁唯一的牙，不许删 |
| 界面"真隐藏"而非"绘制不计高" | 同一次 evaluate 内取三值：内容盒高>0 + 行落在内容盒内 + `elementFromPoint` 中心命中元素本身。**跨时刻 rect 不可比**（本仓库界面用例标准写法） |
| 牙齿（用例能红） | 变体脚本只动 Temp 副本；产品文件恢复核对用 **sha256 快照清单**（不能用 git diff——`core/summary/` 与 txt 全是 `??`）；牙齿只准动自己带哨兵名的目录 |
| 安全边界没被测试带走 | `index.html:14` 的 `connect-src 'none'` 有常驻守门用例（"不许为测试放宽 CSP"）；真后端 UI 用 Playwright 注入桥（与 `desktop.py:142` 同形状） |

## 6. 不适用条件

- **不适用 `bili_scribe` 侧**：bs 一行未改。`tests/smoke_test.py` 的 8 角色与
  `get_role("bogus") → standard` 在 bs 成立；WB 侧该回退**刻意不成立**（有意差异）。
- 本归档**不描述未落地项**：无源三角色是否补写 txt、`视频总结.txt` 归属、
  `config.yaml` 与 bs 共用是否拆分、设置页"深度思考"空控件处置、模型与 `max_tokens`
  的最终选择 —— 全部待用户拍。
- **未 commit**（用户从未要求）；`core/summary/`、`core/prompt_source.py`、
  `infra/notecache.py`、`resources/prompts/*.txt`、十余个新测试文件、
  `requirements-dev.txt` 全是 `??` **untracked**，误删不可恢复。
- 传输层实现不在一比一范围内（WB 走自己 `infra/llm.py` 的 `ChatClient`），但
  **`trust_env=False` 必须保留**——它挡住系统代理劫持内网端点，是真实故障的修复。
