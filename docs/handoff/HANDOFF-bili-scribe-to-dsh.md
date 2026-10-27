# bili-scribe → dsh 移交文档

> **读者**：dsh（DeepSeek Harness）里的 agent。
> **用途**：接手 bili-scribe 这个项目，在 dsh 里继续开发。
> **不该被当成**：用户手册。用户手册是 `bili-scribe/README.md`，那份讲怎么用；这份讲**为什么这么做、哪些不能动、接下来怎么走**。

## 0. 放进 dsh 的哪里

三种放法，选一个：

| 放法 | 路径 | 效果 |
|---|---|---|
| 项目指令（推荐） | `<dsh 项目根>/AGENTS.md` | 每次会话整份注入。适合"在这个项目里干活" |
| 技能 | `~/.dsh/skills/bili-scribe/SKILL.md`（补 frontmatter 的 `name` / `description`） | 只在相关任务时按需加载。适合"偶尔用一下" |
| 外部档案 | 项目根任意位置 + 在 AGENTS.md 里写一句"先读 HANDOFF.md" | 折中，省 token |

推荐第一种。下面这些不变量你每次动手前都该看见，尤其是第 6、7、12 节。

---

## 1. 它现在在哪

| 项 | 值 |
|---|---|
| 本地路径 | `C:/Users/b1596/Desktop/视频内容总结`（2026-10-27 从 WorkBuddy 工作区迁入，venv 已按新路径重建） |
| 版本 | `0.5.0`（唯一来源：`bili_scribe/__init__.py::__version__`） |
| 仓库 | `https://github.com/cunnak/bili-scribe`（公开） |
| 代码量 | 7,835 行 Python + 2,208 行单页 HTML + 4,222 行测试 |
| 测试 | 37 离线 + 35 HTTP + 100 前端 = **172 项**，另有 `probe_http.js`（源码版端到端）与 `probe_exe.py`（打包产物实机验收） |
| 交付形态 | 单文件 exe（`dist/bili-scribe.exe`，约 28 MB，PyInstaller）+ 可跑源码（HTTP 版） |
| 用户数据 | `~/.bili-scribe/`（`bili-scribe.db` + `key.bin` + `frames/` + `notes-cache/`） |
| 技术栈 | Python 3.14 / pywebview(WebView2) 桌面壳 / FastAPI（可选 HTTP 壳）/ SQLite(WAL) / 原生 JS 单页 / 无构建步骤 |

它是**单人自用**的工具，不是 SaaS。没有多用户、没有鉴权、没有部署脚本——HTTP 版默认只监听 `127.0.0.1`，桌面版**根本不开端口**。

---

## 2. 它做什么

给一个 B站 视频（链接 / BV号 / 分享文本），把内容读一遍，写成总结，发到那个视频的评论区。

完整闭环：

```
输入解析 → 内容采集（降级链）→ 大模型总结（可多轮）→ 人工确认 → 发表评论
                ↑                                    ↓
                └──────── 迭代打磨（版本史）←─────────┘
```

**它不是什么**（别扩这几条）：

- 不是批量工具。一次一条，人工确认后才发。
- 不是爬虫。只读 B站 页面上公开的内容，不绕风控、不伪造设备指纹。
- 不是"自动涨粉/引流"工具。评论是给人看的，不是给算法喂的。
- 不评价 UP 主。总结只谈内容，不谈人。

---

## 3. 代码地图

```
bili_scribe/
├── config.py                 配置读写（yaml + 环境变量 + 界面设置三处合一）
├── desktop.py                桌面外壳：pywebview 窗口 + 进程内 API 桥（**无端口**）
├── app/
│   ├── service.py            业务层：Service + AppState。桌面与 HTTP 共用这一份
│   ├── main.py               FastAPI 薄壳：26 个路由，只做传输，不含业务
│   └── static/index.html     单页前端（原生 JS，无框架、无构建，直调 api 桥）
├── core/                     领域层：业务规则，不发 HTTP 以外的事
│   ├── resolver.py           输入 → 稿件 + 分P（链接/BV/分享文本/短链）
│   ├── collector.py          降级链采集
│   ├── srt.py                用户上传 srt：编码探测、解析、错配校验
│   ├── frames.py             全片抽帧：挑帧、裁格、缓存（每次采集独占目录）
│   ├── roles.py              8 个角色的提示词（**改提示词只改这里**）
│   ├── summarizer.py         提示词编排、多轮回流水线、结构化输出、diff_fields
│   ├── composer.py           结构化字段 → 评论正文
│   └── publisher.py          发表 / 撤回
└── infra/                    基础设施：会出错的那一层
    ├── bili_client.py        B站 客户端（Cookie / 二维码登录 / 风控退避）
    ├── endpoints.py          接口地址集中定义
    ├── wbi.py               WBI 签名（B站 的接口签名）
    ├── llm.py               OpenAI 兼容客户端（流式、重试、计量）
    ├── store.py             SQLite：凭据加密、任务、发布、去重、版本史
    ├── notecache.py         预读笔记的内容寻址缓存（**跨任务、跨重启复用**）
    └── uploads.py           srt 临存区（token 校验、用完即删）
```

**单向依赖：`传输层 → app/service → core → infra`。** 领域层不直接发 HTTP，所以换传输方式不影响业务规则。这条别破——它是这个项目唯一的结构性保险，也是 0.5.0 能把桌面版加上去的原因：`desktop.py` 与 `main.py` 只是 `service.py` 的两个壳，业务一行没重写。

**别把业务逻辑再写回路由里。** 0.4.2 之前 `main.py` 是 1,301 行，业务语句和 `HTTPException` 搅在一起，想加第二种传输方式只能复制一份。现在 `HTTPException` 全部收在 `main.py` 的异常映射里（`ServiceError.status` → `HTTPException`），`service.py` 只抛 `ServiceError`。

---

## 4. 数据

### 4.1 三张表

```sql
task              一条总结任务。status 是状态机，progress 是给人看的进度文字
publish           一条发布记录。status: ok / failed / withdrawn
summary_version   版本史。UNIQUE(task_id, version_no)
kv / credential   键值 + 加密后的 B站 凭据（单行，id 强制为 1）
```

`summary_version` 的字段值得记：`source` 只有三种值 —— `initial`（初版）、`refine`（按要求改）、`restore`（以旧版为底稿）。`metrics` 是一整块 JSON（usage / chunks / frames / model / elapsed），**故意不拆列**：它只是给人看的账，不参与查询，拆了以后每次加字段都要迁移。

### 4.2 一个目录

`~/.bili-scribe/frames/<aid>-<随机>/` —— 抽帧缓存。

**每次采集建一个独占目录，采集函数自己建、自己删。** 别再改回按 aid 命名，理由见第 7 节第一条。`purge_stale` 只兜崩溃残骸（超 2 小时），且不碰不属于自己的目录。

### 4.3 什么东西可以不落盘

判据只有一条：**丢了能不能重建**。

| 东西 | 在哪 | 理由 |
|---|---|---|
| 素材笔记（长素材的预读结果） | **磁盘**（`~/.bili-scribe/notes-cache/`，内容寻址、LRU 300 条 / 192 MB） | 见下，0.5.0 从内存改成了磁盘 |
| 版本史每一版的全文 | **必须落库** | 一段已经生成、被人逐条改过的文字，重跑模型不会给出同一个结果。那是丢劳动，不是丢缓存 |
| 抽帧缓存 | 磁盘，任务结束即删 | 第三方内容实体，没理由跨任务留存 |
| 上传的 srt | 磁盘，用完即删 | 同上 |

**笔记为什么改成了磁盘（0.5.0 的改动，别改回去）。** 0.4.2 时它在 `AppState.prepared` 里，注释写的理由是"只在同一任务内供打磨复用，进程退出即丢无所谓"。真实的账本否掉了这个判断：`BV1hBnC6ZEug` 被重开成两个独立任务，**每个任务都各付了一次完整预读**——130,865 与 133,452 prompt tokens，合计约 264k，其中第二遍完全是白花。同一 task 内的复用一直是有效的（v02–v07 六次打磨都是约 55k，不是 130k），漏的是**跨任务/跨重启**那一段。

所以 `infra/notecache.py` 用**内容寻址**做 key：`sha256(recipe + source_label + body)`，`recipe` 含 `max_input_chars / chunk_size / chunk_overlap` 三个会影响产出的参数。参数一改，key 就变，不会命中过期笔记。**只在 `chunks > 1` 时写缓存**——短素材根本不预读，存进去只是占地方。

判据没变，改的是结论：笔记**确实能重建，但重建要花真钱**，所以它是"宁可留着的缓存"，不是"随手可丢的中间产物"。这条从"可以不落盘"里搬出来了。

---

## 5. 八个角色

角色 = 一套提示词方法论。全部定义在 `core/roles.py`，改提示词只改那一处。

| key | 名字 | 轮次 | 方法论 |
|---|---|---|---|
| `standard` | 标准 | 1 | 不套方法论，用流水线原始提示词 |
| `xueshu` | 学术分析 | 3 | 论证结构、证据等级、概念界定 |
| `renhua` | 说人话 | 3 | 一个锚点撑全文、推理外显、变形替代定义 |
| `shensi` | 超强思考模式总结 | 3 | 四问骨架（问题/实质/解法/发现）+ 删套话纪律 |
| `wengao` | 文稿编辑 | 3 | 扩写型，产出最长 |
| `xiezuo` | 写作专家 | 3 | 结构与节奏 |
| `fenxi` | 分析报告 | 3 | 结论先行、分层论证 |
| `jinglian` | 首席思想精炼师 | 3 | 极限压缩，留判断不留修饰 |

多轮 = 生成 → 自我批判 → 修订。`revise` 里必须有 `{critique}` 占位符，否则批判那轮的结果进不去。

### 5.1 角色不能覆盖底线

每个角色的提示词后面都会**原样拼上**输出契约与硬约束：

- 只用素材里的信息
- 素材没覆盖的写「素材未覆盖」，不许补脑
- 时间戳必须来自素材
- 不评价 UP 主

这条别动。角色只管风格，不管事实来源。曾经有个角色想"主动舍弃信息换可读性"，做法是要求它在 `caution` 字段里交代自己舍弃了什么——而不是放开底线。

### 5.2 提示词怎么写才有效（实测结论，样本小）

- **具体例子比原则管用**。写"删掉套话"没用，写"删掉『不仅仅是……而是……』『这深刻地揭示了』"才有用。
- **禁令是枚举式，盖不全**。实测：禁了"不仅仅是……而是……"，模型转手用了同族的"并非……而是……"。所以批判轮里放了一条**检验**而非清单："删掉之后信息量一点不变的就是套话"。
- **要求能被听进去，但是强约束不是硬保证**。实测要求写"只改 one_line，其余四个字段一个字都不要动"：那四个确实逐字未变，但它顺手改了第五个字段。
- **别让批判轮管太宽**。为了保住用户指定的四节结构，曾把批判轮限定在"只盯文体与立论"，结果 `key_points` 退化成截断的原话碎片，没有任何关口拦它。**收窄检查范围，等于给被排除的部分开绿灯。**

---

## 6. 七条不要动的设计决策

这些看起来"可以优化"，但每一条都是为了堵一个具体的洞。

**1. 迭代不是"重跑一次 + 加个提示词"。**
三个理由：会掩盖真实原因（你分不清是要求生效了还是模型这次恰好写对了）；全量重跑意味着全部重花；会把你满意的部分也重写掉。实际形态是「素材 + 上一版 + 本次要求」。

**2. 回退是复制，不是跳转。**
点回退会生成一个**新的版本号**（`source=restore`），内容等于目标版本。这样历史是线性的，任何时候都能回答"第 N 版是什么"，不会出现分支树。

**3. 回退不花一分钱。**
零模型调用，实测 0.03 秒。因为它只是复制已有记录。

**4. 字段级 diff 只输出客观事实。**
不去问模型"你改了什么"（那是自述，会错），而是把相邻两版的 `summary_json` 逐字段比一遍，本地算。**不做**"它没说自己动了这个字段"的标记——那是启发式，两头都会误报，而一个看起来权威的误报比不报更糟。

`changed_fields` 三种取值语义严格分开，别合并：
- `null` = 没比过（第 1 版、回退版）
- `[]` = 比过，逐字相同
- 非空 = 逐字段变了

界面上把"没比过"读成"没改动"，等于替模型圆谎。

**5. 发布必须人工确认。**
`publish.require_confirm` 默认 `true`。这是唯一会对外产生副作用的动作，不该由模型自己决定。界面上改了正文会让"已确认"作废——内容变了，确认就不作数。

**6. 降级链必须如实报告。**
采集有五个来源（用户 srt / 人工字幕 / AI 字幕 / 弹幕 / 仅元信息），每个带 `source_trust`。用了低可信来源就是要让使用者看见，而不是悄悄降级。

**7. 长素材先分段预读再归约。**
超过 `max_input_chars` 就分段，每段出结构化笔记，再拿笔记归约。分块上限 8 段，避免一个长视频把成本炸开。迭代打磨能省下的正是这一段——实测占一次 4 小时视频输入的 **32,663 tokens / 38%**，轨迹里迭代时**没有 `mapping` 那一步**（168.2s → 36.1s）。

---

## 7. 踩过的坑（每条都有证据，别重犯）

### 7.1 帧缓存竞争（v0.4.0 修）

偶发 `FileNotFoundError(2, 'No such file or directory')`，1.5 秒就失败，长得非常像"偶发网络问题"。复现率约 1/6。打桩抓到的时间线：

```
t+2.43s [task-A]   采集完成（目录还在=True）
t+2.66s [refine-A] 采集开始
t+2.96s [refine-A] 采集失败（目录还在=False）
t+3.00s [task-A]   清理删了 13 个文件   ← 迟到的收尾
```

根因**不是"删得太早"**，是两次采集共用了按 aid 命名的同一个目录——清理者不可能知道自己删的是不是别人正在写的东西。修法不是把窗口调小，是**取消共用物**：每次采集独占目录，谁建谁删。

### 7.2 `urllib` 默认读环境代理

本机有 Clash（`127.0.0.1:7897`），会话环境里带 `HTTP_PROXY`。`urllib` 默认会用它，于是打本机端口会被送去代理转发回来，得到 **502**，而应用日志里根本看不到这次请求。

**所有验证脚本都要 `ProxyHandler({})` 或显式 `NO_PROXY`。** 这和测试套件里的规矩是同一条。

### 7.3 `MAX_PREPARED` 的注释错了三个数量级

原注释写"不设上限跑几十条就白占几百 MB"。实测：一条 4 小时视频的笔记（19,772 字）在 CPython 里只占 **39 KB**，20 条 = 0.76 MB。而且成本**与视频长短无关**（`copy.copy` 的是壳，原文被换成笔记后即丢弃）。

上限已改成 200（典型 7.7 MB / 最坏 18 MB）。理由是不对称：内存几乎免费，淘汰的代价是真花钱——重跑一条 4 小时视频 86,428 tokens。

这件事的教训比数字重要：**那个 20 是凭猜测定的数，然后被写进注释、又被写进 README，开始冒充实测结论。**

### 7.4 报错丢出处

原来错误详情只有 `{exc!r}`，`FileNotFoundError` 的 `repr` 里可能连文件名都没有。现在 `_error_detail()` 会带出最后两帧的「文件:行号 函数」+ 完整栈。**这一条把"手工复现"变成了"看一眼栈"。**

### 7.5 heredoc 与反斜杠

一次性脚本用 `python - <<'PY'`，不要 `python -c "…"`——嵌中文引号会**静默不输出**，退出码还是 0。

另外 **heredoc 里不要嵌 Windows 反斜杠路径**：MSYS 会把 `C:\Users\x` 改成斜杠，测试样本被污染，检查看着没命中其实是样本错了。要拼反斜杠用 `chr(92)`。

### 7.6 四个"看着通过、其实没查"的检查

这个项目里反复出现同一类病，最后一次抓出两个：

- 打包复核的「本机路径」正则要求**双**反斜杠，真实路径只有一个 → 永远匹配不上，一直显示"0 处 ✓"。
- 修好后它命中了**它自己的注释**（注释里写了个真实路径样子）。

**判据**：守门脚本必须能对一个已知的脏样本失败。**跑不挂的检查等于没写。**

---

## 8. 怎么才算"没弄坏"

### 8.1 三层测试

```bash
cd bili-scribe
.venv/Scripts/python.exe tests/smoke_test.py     # 36 离线 + 35 HTTP
<node> tests/frontend_test.js                    # 87 前端（jsdom 真跑 DOM，不起浏览器）
.venv/Scripts/python.exe run.py --check          # 配置与凭据自检
.venv/Scripts/python.exe tests/probe_exe.py      # 打包出来的 exe 真的能开窗（先跑 build-exe.py）
```

测试数基线：**37 + 35 + 100 = 172**，外加 `probe_http.js` 与 `probe_exe.py` 两项探针。改了行为就同步改 README 里的数字，别让文档撒谎。

**前端测试是 0.5.0 全部重写的。** 旧的那 134 项做的是"读 `index.html` → 用正则抠出 `<script>` → 在 DOM 桩里求值 → 断言源码里出现过某个字符串"（例如 `静态 11` 查 `<textarea[^>]+id="refine-req"[^>]+oninput="updateRefineButton\(\)"`）。它一直在报 PASS，测的却是拼写。重写后换成 jsdom 真 DOM + 真事件 + 真 await，断言的是行为。旧用例没有保留价值，也没有做兼容层——那是花钱骗自己。

### 8.2 新写守护用例的规矩

**必须先在旧实现上跑一遍、确认它会红。** 已有先例：帧缓存竞争、LRU 淘汰顺序、报错带出处、打包复核脚本 —— 四条都是这么验的。

有一个反面教材值得记住：`_error_detail` 的守护用例第一版是手工构造 `FileNotFoundError(2, msg, "路径")`，文件名会进参数、`repr()` 里也有它，于是**这条用例在旧实现上照样通过**。改成真的 `open` 一个不存在的路径之后才有效。

### 8.3 活体验收（离线测不了的那一半）

模型行为只能真跑。现成的三个脚本在工作区根（**不在仓库里**，见第 11 节）：

- `refine-stage2-live.py phase1 / phase1b / phase2` —— 版本落库、要求穿透三轮、重启后持久化
- `role-shensi-live.py` —— 新角色跑一遍，看四节骨架和要点质量
- `refine-field-diff-stats.py` —— 拿真实版本史统计"模型顺手改"的频率

一次真跑 2–3 块钱。**改动提示词后必须真跑一遍再声称改好了**——提示词改动无法离线验证，这是这个项目最大的验证盲区。

### 8.4 打包与发布

```bash
.venv/Scripts/python.exe scripts/package.py         # → dist/bili-scribe-<版本>.zip
.venv/Scripts/python.exe scripts/verify-package.py  # 独立复核，退出码非 0 就不要发
```

`verify-package.py` 换一套判据查：敏感文件名、凭据、本机路径、真实视频号、包内版本与工作区一致、必备文件齐。

发版顺序（踩过坑）：
1. 改版本号（**只有 `__init__.py` 一处**，先 `git grep` 扫有没有第二处）
2. 测试全绿
3. **打包**（必须在最后一次改动之后——先打包后加文件，包就是旧的，而 zip 一重打 sha256 就变）
4. 复核
5. commit + tag + push
6. Release **两步走**：`gh release create` 先建壳，再 `GODEBUG=http2client=0 gh release upload` 传附件（一步式附件失败会把 Release 一起带走）
7. 校验：tag_name / draft=false / prerelease=false / 附件 state=uploaded / **本地 sha256 与远端 digest 逐字比对**

Windows 上的两条环境事实：代理是 `127.0.0.1:7897`；`gh api` 要传全 `os.environ`（只传 `PATH` 会让它读不到 `%APPDATA%\GitHub CLI\hosts.yml`，表现为**字段全是 None**，看起来像"没发布成功"）。

---

## 9. 在 dsh 里怎么接

三条通路，按代价从低到高。**建议按 9.1 → 9.2 → 9.3 顺序走，不要跳过。**

### 9.1 第一层：MCP 桥（把 Python 变成工具）

dsh 自带 `@deepseek-ai/dsh-mcp-client`，工具会以 `mcp__<serverName>__<rawName>` 出现在 agent 手里。

配置（`serverName` 必须匹配 `[A-Za-z0-9_-]{1,32}`）：

```ts
{
  transport: 'stdio',
  serverName: 'bili-scribe',
  command: 'C:/Users/b1596/Desktop/视频内容总结/.venv/Scripts/python.exe',
  args: ['scripts/mcp_server.py'],
  env: { NO_PROXY: '127.0.0.1,localhost' },   // 见 7.2，否则打本机端口会被代理拦成 502
  cwd: 'C:/Users/b1596/Desktop/视频内容总结',
  toolCallTimeoutMs: 600000,                  // 一条 4 小时视频总结要 90s+，别用默认值
  failOnStartupError: true,
}
```

要写的 `scripts/mcp_server.py` 是一个**薄壳**：它只做路由，所有逻辑调用现有 `core` 层，不重写任何业务规则。需要官方 Python SDK（`pip install mcp`），用 `mcp.server.fastmcp.FastMCP` 起 stdio server 就够。

上面那段是 `@deepseek-ai/dsh-mcp-client` 的 **Config 字段**，不是某个 JSON 文件的格式——它落在你的 cordis 组合里（`cordis.yml` 或 bundle 的 patch 行），具体位置取决于组合怎么装配。字段语义照抄即可。

首版建议暴露 6 个工具（不要一次全暴露，工具太多会污染 agent 的选择）：

| 工具 | 对应现有能力 | 注意 |
|---|---|---|
| `summarize_video` | `POST /api/task` + 轮询 | 长任务，要么异步返回 task_id，要么把 timeout 拉到 10 分钟 |
| `get_task` | `GET /api/task/{id}` | 含进度、来源、警告 |
| `refine_summary` | `POST /api/task/{id}/refine` | **必填 requirement**，缓存失效时如实报原因 |
| `list_versions` / `get_version` | 版本列表与详情 | 带上 `changed_fields` |
| `publish_comment` | `POST /api/publish` | **必须先人工确认**，见第 12 节 |
| `withdraw_comment` | `POST /api/publish/withdraw` | |

三个坑：

1. **不要用 Streamable HTTP 传输**。bili-scribe 的 HTTP 服务需要另外起进程，而宿主环境**不允许 agent 起长驻进程**（2026-10 实测：`DETACHED_PROCESS` 起的进程会被清掉，WMI 创建进程被策略拒，从 Bash 调 `cmd`/`schtasks` 也被拒）。stdio 传输由 dsh 自己 spawn，是唯一稳的路。
2. **进程内直接调 core 层，不要 curl 自己的 HTTP 接口**。多一层网络就多一个 502 的机会，而且拿不到异常栈。
3. **SQLite 用 WAL，多进程共享同一个库**。dsh spawn 的 MCP 进程和原来的网页服务会同时开 `~/.bili-scribe/bili-scribe.db`——这是安全的（WAL 支持），但**删除或备份时不要拆开拷 .db/-wal/-shm**，用 `sqlite3.Connection.backup`。

### 9.2 第二层：把重复的编排收进 MCP

跑通第一层之后，会发现 agent 每次都在重复同样的三步（提交 → 轮询 → 取结果）。这时候再考虑：

- 把「轮询」收进工具内部（`summarize_video` 直接返回最终结果，只在超时时返回 task_id）
- 加 `list_recent_tasks` 让 agent 能回答"我上次总结的那条呢"
- 加一个 `diff_versions` 工具（现在 `changed_fields` 已经在接口里，但"看具体差异"还是要在页面上悬停）

**判断标准**：只有当某个编排在对话里被重复了三次以上，才值得变成一个工具。

### 9.3 第三层：client 面板（重建 UI）

用户明确要的是原生面板。代价得说清楚：**它比前两层贵一个量级。**

dsh 的 client 插件是 cordis 包，命名 `@deepseek-ai/dsh-client-<name>`，必须遵守一整套纪律：

- UI 只能通过 `ctx.slots.register({ name, children?, store?, inject? }, Component)` 组合，slot 名形如 `<domain>.<entry>.<hole>`。渲染未声明的 slot 会在加载时失败。
- 组件只能拿到"四份共享"：owner props / `PropsRenderSlots` / `PropsStore` / inject face。**组件看不到 ctx**，也不能自己造 hook。
- 响应式数据只有三条通道：父级已知 → owner props；只有组件自己知道 → local state；跨入口或跨重挂载 → register 时声明的 store。
- 写操作只能走 `props.actions.*`。
- 不许 runtime-import 另一个 feature 插件的值。

这意味着 1,836 行原生 JS 不能直接搬——它不是"移植"，是**按 dsh 的组件模型重写一遍**。

**折中路线（推荐先做这个，一两天能出效果）**：

dsh 有 `present` 工具。让 agent 把版本史渲染成一份 HTML（三栏：版本列表 / 本版全文 / 相邻两版的字段 diff），present 出来。用户在 dsh 的预览面板里看，点"发布"再走对话。这**不是**原生面板，但拿到了 80% 的价值，代价是几百行模板，不是几千行 React。

**然后**再决定要不要原生面板。真要做的顺序：

1. 在 `packages/client/` 下建包，`package.json` 声明 `dsh.client`（`inject: ["@deepseek-ai/dsh-client-runtime", "@deepseek-ai/dsh-client-ui-slots"]`、`platform: "web"`），导出 `./client`，用 `packages/client/tsdown.client.ts` preset。
2. 先做**只读**面板：版本列表 + 正文预览 + diff 展开。数据从 MCP 工具或直接读现有 HTTP 接口。
3. 再做写操作（发布、回退），且**发布必须二次确认**。

---

## 10. 迁移路线（阶段表）

| 阶段 | 做什么 | 验收 |
|---|---|---|
| **S0 接手** | 读这份文档 + `README.md` + `roles.py`；跑一遍 172 项测试 + `probe_http.js` + `probe_exe.py`；确认桌面版能开窗 | 测试全绿，底栏版本号 = `0.5.0` |
| **S1 MCP 桥** | 写 `scripts/mcp_server.py`（6 个工具），配 `dsh-mcp-client` | agent 能完成"总结一条视频 → 改一版 → 取版本"三段，不用手工拼 URL |
| **S2 会话化** | 把轮询、重试、进度播报收进工具；加 `list_recent_tasks` | 一条 4 小时视频在对话里跑完，中途不超时、不丢进度 |
| **S3 面板（轻）** | 版本史 → HTML → `present` | 用户能在预览里看清"这一版实际改了哪些字段、改成什么" |
| **S4 面板（原生）** | client 插件包，只读版 → 写操作版 | 在 dsh web UI 里完成"看版本 → 回退 → 发布"全流程 |
| **S5 补洞** | 见第 11 节 | 每条有守护用例 |

S1 之前别动 `core/`。**先把桥搭通，再谈重构**——不然你会同时改两个变量，出问题分不清是哪边的。

---

## 11. 现有缺陷清单（接手起点）

按"修起来多便宜 / 不修多糟"排序。

**1. 发布记录不记版本号（便宜，但越晚补越补不回来）**
`publish` 表存了发出去的文本，但**没有 `version_no`**。评论正文在发布前可以手改，手改之后既不是任何一版、也查不出对应哪一版。所以"我发的是哪一版"在现有数据里**可能根本没有答案**。
修法：`publish` 加 `version_no` + 一个"与模型版是否一致"的布尔。历史发布补不回来。

**2. 没有缩水检测（便宜，接住最常见的一类坏）**
"丢了素材"最常见的形状是缩水。目前没有任何东西在提示"这一版比上一版少了 30% 内容"。纯本地比较字数/要点数就能做。

**3. 保真校验只有模型自己（贵，但这是最深的洞）**
现在的"批判断言素材有没有被编造"用的是**同一个模型**。同一个模型的自我报告，不能既当运动员又当裁判。这和 `revise_note` 不可信是同一个问题的放大版。

**4. 模型行为的结论样本太小**
"改动说明不保证准确""要求能被三轮回听到""只改提到的部分是强约束"——这些全部来自 5 次迭代、同一个视频、同一个模型（`qwen3.8-omni-flash`）、同一个角色（`wengao`）。**换模型或换视频类型，这些结论可能就不成立。** 补起来很便宜（各两三块钱跑一轮），但没做。

**5. 仓库里有 4 处引用指向仓库外的脚本**
`main.py`、`core/frames.py`（两处）、`tests/smoke_test.py` 都写着"脚本见项目根的 `refine-debug-race.py` / `refine-cache-budget.py`"，而那 6 个脚本原本在工作区根、**从没进仓库**。公开仓库的读者照着找会找不到。
**2026-10-27 进展**：7 个脚本已随迁移落到项目根（`refine-*.py` / `role-shensi-live.py`），注释里的路径现在成立。但**尚未 `git add`**，未提交前对公开仓库的读者仍然不存在。剩余两件事：(a) 决定要不要提交进仓库，若要，先按下一句处理 bvid；(b) `refine-stage2-live.py` 第 35 行硬编码真实 bvid（`BVxxxxxxxxxx`，个人观看记录）、第 347 行硬编码 `BVyyyyyyyyyy`，公开前必须改成命令行参数。

**6. 没有 CHANGELOG**
已经发了 8 个版本，仓库里和发布包里都没有版本历史。拿到 `0.5.0` 的人无从知道它和手上那份差在哪——而 0.5.0 的跨度特别大（桌面化 + 分层 + 前端重写 + 笔记缓存），这份差距恰好最需要写清楚。`docs/handoff/release-v0.4.2.md` 是唯一一份还算完整的发布说明，`release-v0.5.0.md` 已补。

**7. `max_comment_chars` 与 `comment_short` 的规格不对齐**
角色提示词里写 `comment_short` 180–280 字，实测出过 317 字。提示词里的自述规格没有硬校验。

---

## 12. 红线

**1. 凭据永不入库、永不进日志、永不进包。**
B站 凭据在 `credential` 表里是加密的（`key.bin` 是本地密钥）。发版前必须跑 `verify-package.py`。日志里出现 `SESSDATA` 就是事故。

**2. 发布是唯一有外部副作用的动作。**
- 必须人工确认（`require_confirm` 默认 true）
- 只能在目标视频的评论区
- 不做批量、不做定时、不做"帮我给这 20 个视频都留个言"
- 撤回要能工作（`withdraw` 走的是删除接口）

**3. 只读公开内容。**
不绕风控、不伪造设备指纹、不并发轰炸接口。降级链的设计前提就是"拿不到就如实说拿不到"。

**4. 素材版权：不把第三方内容落盘留存。**
抽帧、上传的 srt 用完即删；素材笔记只在内存。唯一长期保留的是**自己生成的总结**和**用户自己的版本史**。

**5. 不评价 UP 主。**
总结谈内容，不谈人。这条写进了底线条款，不是风格偏好。

**6. 别把"检查通过"当成"没问题"。**
第 7.6 节那两次教训都发生在同一个地方：**检查本身坏了，而它显示通过**。任何新增的守门逻辑，先证明它能对已知的坏样本失败。

---

## 13. 交接清单

**必须保留（丢了就是丢劳动）**
- `~/.bili-scribe/bili-scribe.db`（尤其 `summary_version` 表）
- `~/.bili-scribe/key.bin`（丢了凭据解不开，要重新扫码登录）
- `core/roles.py` 的八套提示词（这是调了很多次的结果，不是随便写的）

**可以重建（丢了只是贵）**
- `~/.bili-scribe/frames/`（抽帧缓存）
- 内存里的素材笔记
- `dist/`（打包产物）

**可以直接丢**
- `__pycache__`、`.venv`（重新 `pip install -r requirements.txt`）

---

## 14. 一句话

这个项目的价值不在"能总结视频"，那部分谁都能做。它值钱的是**它知道自己哪儿不可靠**：改动说明是自述不是审计、帧缓存曾有过竞争、上限数字曾经是猜的、模型行为的结论只来自五次样本。这些在代码注释、README 和测试里都留了痕。

接手时先读那些写着"为什么不能这么做"的注释，再读实现。实现能看懂，判断只能从注释里拿。
