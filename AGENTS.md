# AGENTS.md — video_workbench / bili_scribe 仓库作业规则

> 本文件沉淀**这个仓库特有的坑**。都是踩出来的，不是设计偏好。改代码前先读第三、四节。

## 一、环境（错一次就白跑一轮）

- Python 一律用 **`.venv/Scripts/python.exe`**。裸 `python` 是 WindowsApps 占位 alias，跑了等于没跑。
- 中文探针/输出先 `export PYTHONIOENCODING=utf-8`，否则 GBK 控制台会乱码或抛 `UnicodeEncodeError`。
- 计数类命令**不要接 `head`/`tail`**：管道会让中途退出的 `[exit code]` 变成 0，把失败洗成成功。
- 全量回归：`.venv/Scripts/python.exe -m pytest tests/wb -q`（离线，约 85–95s）。

## 二、两套包，别当一回事

- `bili_scribe/`（bs）= 原程序，`video_workbench/`（WB）= 复刻方。根 `config.yaml` 是两边共用的实配。
- WB 的总结核心是**逐字节锁定的 vendored 副本**：`video_workbench/core/summary/roles.py`、`video_workbench/core/summary/summarizer.py` + `video_workbench/infra/notecache.py`。除 import 段外必须与 bs 原件逐字节一致，`tests/wb/test_summary_drift.py` 14 条守着（现读该文件：`:234` 比 `core/summary/roles.py` 对 `bili_scribe/core/roles.py`、`:284` 比 `core/summary/summarizer.py` —— **`__init__.py` 不在这两条比对里，也不在字节锁清单里**）。
- **`core/summary/__init__.py` 不是副本，是 WB 自有的权威层**（`RETIRED_ROLES`、`ALL_ROLES` 过滤、`_WB_ONLY` 自造角色、`role_label` 都在这里），改它合法、不需要 drift 翻正。要删/藏/加角色，只改这里；不要去动 `roles.py`。（本条 2026-11-04 更正：我原先在这行把 `__init__.py` 也写成"逐字节锁定的副本"，那是错的口径，会让人以为改权威层要过 drift 门。）
- **角色名单对账只在权威层首次 import 时跑一次；import 之后再改坏 `NO_SOURCE`/`_NEVER_BIND` 是静默的**（parity 实测：干净 import 后改坏不抛，显式重跑 `_reconcile_role_lists()` 才抛）。⇒ 这类锁的牙齿必须**赶在 collection 之前** patch 权威层模块属性；而且判"红没红"时 `rc ∉ (0, 1)`（例如 collection 失败 `rc=2`、内部错 `rc=3`）必须显式打出来，不能读成"没红"。同一族形状：套件级牙齿可能整文件收集失败（`Interrupted: 1 error during collection`），这时"只红它该红的那条"根本不成立。
- `tests/smoke_test.py` 里的 `xueshu` 全部是测 **bili-scribe** 的（bs summarizer、bs 的 set_role、bs `passes==3`）。删 WB 的角色不影响它，别顺手"修"它。
- **比提示词长度前，先确认你量的是哪个对象**（本仓库为此反复吵过三轮）。`role.system` 存的是 **txt 原文**；真正进请求的是副本 `core/summary/summarizer.py:518` 拼出来的 `system.strip() + "\n" + OUTPUT_CONTRACT`。实测：txt stripped **511** + 换行 1 + `OUTPUT_CONTRACT` **450** = 进请求 **962**。⇒ 断言 `len(role.system)==txt` 与断言请求内容，是两件不同的事，报数时必须写明是哪个；同一条读数换个字段就变了。

## 三、字节锁 × git 规范化（这条最容易静默炸别人机器）

`.gitattributes:5` 是 `* text=auto eol=lf`。它会**静默改写被 sha/字节锁住的文件**：工作区 CRLF 的文件入库变 LF，字节数就变了。

已发生的：`文稿编辑.txt` 工作区 8479B、入库 blob 一度只有 **8273B**（少 206 个 CR）。别人 `git clone` 拿到的是 LF 版，于是「真源 sha 登记表」和打包期字节比对在**别人机器上**红成"真源被人改过"。

修法与核查法（三比，缺一个都可能漏）：

```bash
wc -c < 工作区文件                          # ① 桌面真源
git cat-file -s ":路径"                    # ② 入库 blob
git archive HEAD | tar -x -C /tmp/xx       # ③ 模拟 clone，逐字节 cmp
```

任何被 sha/字节锁住的路径（**6 个 `resources/prompts/*.txt`**，以及将来新增的二进制/字节敏感件）必须在 `.gitattributes` 里显式 `-text`。改完用 `git add --renormalize` 重灌，再用 ③ 证明 clone==桌面。

**判据**：新增任何 sha/字节锁时，同一笔改动里就要跑一遍 ①②③ 并贴出三行读数。

## 四、起真 GUI / 真后端的三条硬知识

1. **隔离夹具只准重定向 `HOME` + `USERPROFILE`，必须保留真实 `APPDATA`/`LOCALAPPDATA`。**
   连 `APPDATA` 一起指到 Temp，pythonnet 的 netfx 宿主会起不来：
   `RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize … pythonnet\runtime\Python.Runtime.dll`；
   试 `PYTHONNET_RUNTIME=coreclr` 更糟：`FileNotFoundException: Could not load assembly 'System.Windows.Forms'`。
   这个报错长得像产品坏了，其实是夹具过界。路径差异靠改写配置副本里的绝对路径解决。
2. **浏览器探针别用 fetch/XHR。** `video_workbench/web/index.html:13` 的 CSP 含 `connect-src 'none'`（`:9` 注释：页面不做任何网络动作，所有网络都在 Python 侧）。桌面 RPC 走 pywebview 桥（`desktop.py:142`），所以正确做法是 Playwright `page.expose_binding` 把进程内 `WorkbenchService().dispatch` 挂成 `window.pywebview.api.rpc`。现成范例：`tests/wb/test_summary_real_backend_ui.py:225`。**不要为了让探针方便而放宽产品 CSP。**
3. **真 GUI 验收前后各记一次三个真实配置文件的 sha256 并逐字比对**（`~/.bili-scribe/config.yaml`、`~/.bili-scribe/workbench.json`、`~/.video-workbench/*`）。窗口可能在你看不见的时候写盘。

WB 没有 HTTP 服务模式：`run.py` 是 **bili-scribe** 的 uvicorn + webbrowser 启动器，不是 WB 的。WB 入口只有 `video_workbench/desktop.py:main(argv)`（`--config/--debug/--check`）。

### 只读入口的写盘边界（2026-11-04 定，task-34）

- `load()` 里**除「搬家落新家」与「显式保存」外不写盘**。曾经有一条自愈：`if foreign: cfg.save(target)` 原地重写用户的配置文件——已撤，改成只把键名记进 ClassVar `cfg.foreign_keys`（不进 `asdict`/设置页/落盘），装配处 `logger.warning` 报**键名**。**那个重写修不了它想修的病**：bili-scribe 读的是 `~/.bili-scribe/`，不是新家那一份；脏键的清理是 bili-scribe 自己的事。
- 取只读名单**不许装配服务**。`roles_payload(cfg)` 是模块级唯一 owner（`app/service.py:650`），`WorkbenchService.summary_roles()`（`:2591`）只是它的一层委托；`cli.cmd_roles`（`:238`）与 `desktop._roles`（`:313`）直接吃 `roles_payload`。`cli.open()` 已改成懒装配（只在 `rpc()`/`wait()` 里装）。
- **已知残留（有意保留，不是缺陷）**：`info` 与 `--check` **仍会建库、建 `logs/`、落 DB 备份**——它们的语义就是体检「配置写得进去、数据目录可写」。**别为了"统一"把它们也改成不装配**，那会让 `--check` 变成 no-op；要改得先让 `app.onboard` 不摸网关，那是另一笔改动。
- 复现这条要用**含 foreign 键的夹具**（`storage.cache_root` 一个就够）。真实两家现在 `foreign_keys()` 都是 `[]`，**拿真实配置当夹具复现不出来 —— 那不等于缺陷不存在**（本轮 lead 就在这上面错过一次，并据此否掉过一张真单）。

### 长素材的输出预算边界，以及 bs 那条自愈腿为什么救不了（2026-11-05 定，task-50）

- 走分段预读的长素材（`pj_0c24bce5cf5c` 4126 段 / 39938 字），**输入最大的不是 generate、也不是 critique，而是第三轮 revise**：副本 `core/summary/summarizer.py:597`，user 内容 = 素材正文 + 待评稿 + 批判意见，实测 **55013 字符**（critique 50674、generate 49504）。报「哪一轮最大」之前必须三轮都读过。
- 三轮调用**都不写 `max_tokens`**；严格档（默认）下适配器 `_parity_max_tokens(None)` 只是**把配置值原样补上**，不抬不封 ⇒ **每一轮的输出预算恒等于 `cfg.llm.max_tokens`**。`_json_budget` / `MIN_JSON_TOKENS=12288` 那套阶梯只在 `max_tokens` 显式传入时生效，严格档实测 11 次调用 `max_tokens_arg` 全 None ⇒ **一次都没参与**。别拿它当"预算兜底"。
- 因此 `max_tokens=16384` + 推理型模型（`workbuddy/deepseek-v4.1-flash` 等）在长素材上**必然**死在 revise：detail 逐字 `finish_reason=length，思考用了 16384 tokens`。实测两条对症解法（都不改产品码）：**抬到 32768**（同模型同素材 11/11 全过，总 541s）；或**换非推理模型**（`workbuddy/glm-5.3-flash` + 16384 也 11/11 全过，总 809s，预读方差大）。
- **`bili_scribe` 那条"空正文自动关思考重问"的自愈腿在这台机器上是空转**，别照搬进 WB 当修复：`_payload` 在 `thinking=="auto"` 时根本不发 `enable_thinking`（bs 配置默认就是 auto），所以它的第一次请求与 WB 严格档逐字同形；它多做的只有"带 `enable_thinking:false` 重问一次"。实测该端点**不遵守这个参数**——同一小题三发 `reasoning_tokens` = auto 110 / off(**false**) **140** / on 106，全都照样思考。⇒ 同 payload 下 bs 也会走到 `bili_scribe/infra/llm.py:452 raise LlmError`，只是换一句文案。**这不是"WB 与 bs 输出不一致"的破口，是端点能力**；判"两边行为不同"之前先用这种几秒的小请求把布尔问题答掉，别喂 10 分钟原 payload。
- 端到端探针的零改动量法（副本是字节锁的，**别去加 print**）：进程内包一层 `video_workbench.core.summarizer.SummaryLLM.chat`，打印 `sum(len(str(m["content"])))`、`kw["max_tokens"]`、`_parity_strict()`，挂在 `ad.run(...)` 之前。现成脚本 `Temp/wb_t50_probe.py`，三份读数 `Temp/wb_t50_probe{,_mt32768,_flash}.json`。
- **旧探针 `Temp/wb_real_e2e.py` 没有 try/except**，异常直接冒到 stderr ⇒ 从不落 JSON 产物，事后只能靠已被消费的 job 输出。**端到端脚本必须自己捕异常并把 `ChatError.detail` 写进产物**，否则失败证据活不过一轮。

## 五、打包版（`dist/VideoWorkbench`）

- 构建：`.venv/Scripts/python.exe scripts/build_wb.py`（`SPEC` 指向**仓库根**的 `video_workbench.spec`，不在 `scripts/` 下）。
- `verify()` 会逐个比 `resources/prompts/*.txt` 的**字节数**（`PROMPT_TXT_NAMES`）。文件名里有全角冒号、末尾句号、`.txt` 前一个空格（`大概率管用 .txt`）——**逐字抄，别手敲**。
- 冻结态 **没有 stdout**（`console=False`，exe `Subsystem=2`）。CLI 自检结果只能落成 exe 旁边的 json：`--check` → `wb-check.json`，`--roles` → `wb-roles.json`。
- **exe 的入口是 `desktop.py` 的 `main()`：只往 `cli.py` 加路由，冻结态收不到**（当年实测报 `VideoWorkbench.exe: error: unrecognized arguments: --roles`）。这类开关必须挂在 `desktop.py` 的 `main()` 上，且派发要在 `create_window` **之前**，否则会先弹窗、什么都不返回。
  现状（2026-11-04 更正本条口径）：**两个入口都存在，但只有一个数据 owner**。`desktop.py:294 _roles()` 是打包验收路径（落 `wb-roles.json`）；`cli.py:215 cmd_roles()` 是**开发态额外入口**（`python -m video_workbench.cli --roles`，内部走 `self.rpc("summary.roles")` ⇒ 同一份载荷，不是第二份真源）。别因为"cli 里已经有了"就以为冻结态能跑。
  **判据的实际写法**（parity 现读纠正，我复核过：`grep -rln "inspect.getsource" tests/wb/` → **零命中**，所以拦它的不是读源码文本）：`tests/wb/test_packaging_roles_cli.py:76 test_main_dispatches_roles_before_opening_a_window` —— 用 monkeypatch 把 `desktop._roles` 换成记账函数、把 `desktop.create_window` 换成 `raise AssertionError("--roles 不该建窗…")`，再断 `desktop.main(["--roles","--config",…]) == 0`。**建窗即炸**才是那条机械证据；掉进建窗分支的现场是"弹窗起来了"，人工验收极易误判成通过。同文件 `:46` 钉 `wb-roles.json` 载荷形状、`:68` 钉 `--check` 的文件名没被 `name` 参数带跑。

## 六、协作与交付纪律（本仓库踩过事故）

- **牙齿（故意改坏验红）只准在 `%TEMP%` 下自己带哨兵名的副本里做。禁止对仓库工作区文件按快照覆盖回写。** 已发生两次：用"开工前原字节"还原，把同一文件上别人已落地的改动一起抹了（settings.js 的角色下拉注入、已 accepted 的删控件都被抹过）。
- **新建 `tests/wb/*.py` 要先把路径登记进 sha256 快照清单。** 否则还原逻辑会把"开工前不存在"的文件当垃圾删掉。
- **交付里每个数字后面必须附产生它的那条命令原文。** 编不出命令原文，就等于没跑过。
- **`git cat-file -s :路径` 打的是「索引 blob 的字节数」，索引内容已经过 eol 归一化 —— 它不等于工作区字节数。** CRLF 文件两者之差正好等于 CR 个数（实测 `tests/wb/test_summary_retired_roles.py`：索引 44592 / 工作区 45370 / 差 778 = 行数 = CR 数）。我（lead）23:0x 就拿这两个数当"内容一致"的证据，配上自己编的 grep 模式，误报了一次"在途工作被快照还原抹掉"。**判内容是否一致，只有两种正确读法**：① 原始字节直接比（`工作区.read_bytes().replace(b"\r\n", b"\n") == git show :路径`）；② 同一条命令里打三份**归一化后**的 sha 并写明引用哪一份。字节数差不是证据，行尾差异会伪装成内容差异。
- **grep 模式必须来自对方交付里的真名，禁止自己"按语义猜一个函数名"再拿 0 命中定罪。** 我那次用 `never_raises|same_key_set|形状同源|惰性` 扫 parity 的文件得 0 命中，他的真名是 `test_the_mock_payload_has_every_key_the_real_service_sends`、`test_the_error_envelope_currently_names_no_broken_role_key`、`test_a_retired_key_missing_from_never_bind_is_caught`。同一族错误本轮我犯过三次（`'"display"'` 带引号当字面量、`>= 4` 行号凭记忆、这次猜函数名），前两次只是读错数，这次是拿错钥匙去开一扇有东西的门，然后宣布房间是空的。
- **`git commit` 必须带 pathspec。** 我 `git add AGENTS.md` 之后不带路径 `git commit`，把 parity 刚 staged 的两个测试文件一起吸收进 `5683253`（`git show --stat` 三个文件），一笔提交里混了两个作者的改动 —— 回滚、cherry-pick、按作者复盘全都不干净。lead 替成员收 staged 文件是常态（成员被要求"落地就 add"），所以 pathspec 不是形式主义。
- **判别人「报了没跑过的东西」之前，先隔 3–5 分钟重读同一批文件。** 单点读数可能落在失真窗口里。本轮我（lead）就用一次失真窗口的读数判过 vendor 造假，随后被他的只读命令原文推翻——规矩对我也生效。
- **任何对仓库文件的快照恢复必须先打印 sha256（恢复前/恢复后），禁止静默 `shutil.copy2` / `cp -p` 回盖。** 保留 mtime 的覆盖会让「文件没变」的假象与内容漂移同时成立：本轮 `prompt_source.py` 的 mtime 停在 19:06，内容却在 20:01 读到 HEAD 版、20:04 读到落地版，排查成本极高。
- **`git diff` 对未跟踪文件恒返回空且不报错。** 判断"有没有人动过"必须 `git status --porcelain` + `git ls-files`（或 `git cat-file -s :path`）。只看 `git diff` 会把"没被跟踪"读成"没被改动"——`tests/wb/test_git_no_normalization_on_locked_paths.py` 那 20 条字节锁闸就是这样被静默删掉、一夜从全量里消失的。
- **新建 `tests/wb/*.py` 落地后必须马上 `git add`（只 add，不 commit）。** 未跟踪的文件在任何一次误删/换机/快照还原里都会永久消失，已经消失过一次。
- **跨工具比 sha 必须比原始字节，禁止用 `read_text()` 归一化后的内容算 sha。** `video_workbench/web/js/pages/settings.js` 是 **CRLF**，而 `prompt_source.py`/`mock.js` 是 LF：`sha256sum` 打磁盘字节得 `18a9fc3a…`，Python `Path.read_text()` 把 `\r\n` 折成 `\n` 再 encode 得 `4339f77f…`。本轮「同一文件两次 sha 不同」被当成并发写/静默还原来排查，实际全是读法差异。⇒ 快照登记、字节锁闸、牙齿的"恢复后一致"核对，三处都必须 `open(p,'rb').read()` 或 `sha256sum` / `git cat-file -s :path`。
- **判"落地了没有"只读工作区路径本身，并且必须在同一条命令里同时打印 工作区 / 索引 / HEAD 三份 sha256 + grep 命中，写明引用的是哪一份。** 我（lead）用 `git show :path` 读到旧版，判成"工作区被打回旧版"，给两位成员各开了一张重建单——这种单子会让人真去重写本来好好的代码，比误删文件更贵。`M` 只代表工作区≠索引；落地未 commit 时索引=HEAD=旧版是**正常状态**，不是回退。
- **`team_task_update(action=edit)` 的 `description` 是整份替换，没有追加语义。** 想补证据必须先 `team_task_get` 拿全文，在原文后面接，再整份 edit 回去；直接写新内容会**静默吃掉前人写的判据与合同正文**。本轮 vendor 想用「追加」方式补牙齿证据，一次就把 task-42 的合同正文 + 交付证据全删了（返回值里 `description` 只剩他那段），靠他自己上下文重建才恢复到 rev6。这条比「别人报的行号要现读」更狠：那毁的是读数，这毁的是合同。
  - lead 自己也被这条咬过一次：task-38 结板时我用 edit 写了「判通过」正文，把原始四条判据压成了摘要——原文只活在提交信息 `4fa5aa6` 与消息里，板上不再逐字存在。
- **补键不补值的「同形」判据，必须说清哪些键是惰性的。** 本轮给 `mock.js` 的 `summary.roles` 补齐七个键后，牙齿证明：`retired` 抄真值 ⇒ `state.roleRetired.length` 会变（有消费者）；`extra_prompt` 抄 `true` ⇒ 页面读数**全不动**（前端只从 `settings.get` 读它，`summary.roles` 里那个键没人消费，见 `settings.js:79`/`:338` 对 `app/service.py:694`）。⇒ 形状同源是**维护性规则**（防"真载荷加一列没人跟"），不是行为规则；写判据时行为断言只挂在真被消费的键上，别给惰性键编一条"页面会变"的假判据。
- **"单一 owner"这类规则，值相等的断言拦不住取法退化，必须写行为锁。** 本轮实例：`core/summarizer.py` 用 `from … import role_label` 别名取退役显示名，别名在导入时就绑死函数对象 ⇒ 改权威层输出下游纹丝不动，而"值相等"那条断言照样绿。改成经模块属性现取 `summary_pkg.role_label(...)` 后行为锁才成立。写判据时：patch **权威层的模块属性**（不是适配层别名、不是 subprocess），并同测 `resolve_role` 的两条 notice。
- **引用代码位置一律用符号名，不要给行号。** 行号会漂：vendor 在 task-44 里多加一行注释，就把他上一轮报给我的全部行号（`:131`→`:132`、`:273`→`:274` 等）整体推走一位，我自己按旧号去读会读错位置。要定位就 `grep -n "^def role_label\|^def _bound_get_role\|text = \"\" if key is None"` 现取，或在注释/文档里写「`role_label` 里的判空」这种可 grep 的说法。
- **字面量禁令（`grep -n "已停用\|str(key or"` 这类）必须只看非注释行，否则会把"解释为什么没有这张表"判成违规。** 本轮两次实测命中：task-36 的中文退役名、task-44 的 `str(key or` —— 两处命中都在注释里，是实现层写的理由。正确口径是**注释可以有、代码行不行**；写判据时把扫描域收成"非注释行"，同时保留那段解释（删解释等于删掉下一个人的护栏）。
- **源码级判据的「扫描域」必须由环境变量重定向，不许改工作区文件来验红。** 本轮立起来的形状：`WB_CLI_SOURCE_OVERRIDE`（tests 侧 `_cli_function_lines`）与既有 `WB_PROMPTS_DIR`、`WB_WEB_ROOT` 同一族——**牙齿从此有合法入口**：以前「牙齿只准动 Temp 副本」没给测试侧留门，大家只能临时改工作区再改回来，而那正是历史上两次抹掉 vendor 改动的手法。新写源码级/字面量判据时：① 扫描路径走这种开关，别自己造新名；② 必须配一条常驻用例证明**开关只换文件、不换判据**（样板：`test_the_source_override_switches_the_file_not_the_verdict`）；③ 判据覆盖面要说清盯的是哪个函数/哪一段，别把「今天这个形状不复发」写成「整类禁令已成立」。
- **牙齿打补丁的时机分两面，搞反就红不成或红一片**：① 要改**产品侧模块属性**（权威层 `role_label`、`_NEVER_BIND` 这类）必须赶在 **collection 之前**，因为测试模块 `from … import` 的别名在 import 期就绑死了（`import test_xxx as M` 还会 `ModuleNotFoundError`、rc=3）；② 要**红得精准**（只红该红的那一条）就把补丁放进 `pytest_runtest_setup` 按 `item.name` 命中，放 `pytest_configure` 会红一片或根本红不到。
- **同一族三个未决口径，全部属于「偏离 bili-scribe 或收紧契约」，需用户拍板，实现层与测试层都不许自己动**（现状各由一条用例钉住，拍板那天这些用例必须红）：
  ① 副本入口 `_bound_get_role` 对未知/坏输入**静默回落到一把退役键 `standard`**（权威层 `get_role` 是正确抛 `UnknownRole`）—— 回落语义来自 `bili_scribe/core/roles.py:612-613`，由 `test_a_broken_input_currently_falls_back_to_a_retired_key_in_the_copy_path` 钉住；
  ② 带空白的键两条路结果不同：`get_role(" wengao ")` → `'wengao'`（清洗侧），`_bound_get_role(" wengao ")` → `'standard'`（原样交给 bs 查表）—— 由 `test_the_copy_lookup_still_matches_bili_scribe_for_a_padded_key` 拿 bs 原件当对照物钉住；
  ③ 「非字符串按 `str()` 查表」意味着 `__str__` 返回键名的对象会被当合法键 —— 现状登记，没有收紧。
  另有一条**已确认的契约边界**（不是缺陷）：`__str__` 自己抛异常的对象在 `str(key)` 这一步就抛 `ValueError`，三个入口都抛，由 `test_an_input_whose_dunder_str_raises_currently_breaks_the_never_raises_contract` 钉成「期望值就是抛」。
- `roles` 载荷里 `retired`（裸 key，前端 `summary.js` 用 `indexOf`）与 `retired_labels`（`{key, display}`，CLI 用）**并存是有意差异，不许合并、不许前端改吃 `display`**。两字段必须**同序**——断言要写成 `list == list`，写成集合相等就白钉（两边各自 `sorted()`，同序本来只是巧合）。

## 七、规则表

| 规则 | 由什么拦住 |
|---|---|
| 副本与 bs 逐字节一致（除 import 段与 `__init__.py` 权威层） | `pytest tests/wb/test_summary_drift.py`（14 条） |
| 6 个 txt 真源不许被改一个字节 | `tests/wb/test_summary_prompt_source.py` P1 的 sha256 登记表 + 打包期字节比对 |
| 被字节锁住的路径不许被 git 规范化 | `tests/wb/test_git_no_normalization_on_locked_paths.py`（自动闸：锁清单现读自 `scripts/build_wb.py:PROMPT_TXT_NAMES` + `tests/wb/test_summary_drift.py`；四道判据 J1–J4，含 `core.autocrlf=true` 与 `* text=auto eol=lf` 组合在场的读数）+ `.gitattributes` 的 `-text` 行 |
| 界面字段必须真的可达可点 | `tests/wb/test_settings_role_warning_ui.py` N1/N2/N3（同快照三条件：内容盒高>0、行在盒内、`elementFromPoint` 命中） |
| 配置搬家不改任何值 | `tests/wb/test_*_migration*` 反向常驻用例（旧家 `max_tokens:4096` → 新家仍 4096） |
| 只读诊断口不许写用户配置、不许建库建 logs | `tests/wb/test_cli_roles_route.py` C4（首次运行即不写）+ C5（无 `workbench.json`/`cli.db`/`logs`）+ **正向对照**（`info` 真会落 `cli.db` 与 `logs/`，防四个 `not exists` 因路径写错变永真） |
| 退役显示名只有一个 owner，且必须**现取**（不许 import 别名） | `tests/wb/test_cli_roles_route.py` D 组两条行为锁：patch 权威层 `video_workbench.core.summary.role_label` 后，CLI 那句退役与 `resolve_role` 的两条 notice 必须跟着变。牙齿变体 T-6（把三处取法退回别名）→ 只红这两条 |
| `retired` 与 `retired_labels` 同源**同序**、前端只吃裸 key | `test_cli_roles_route.py` 判据 5（`[x["key"] for x in retired_labels] == retired`，list 相等）+ `test_summary_retired_roles.py` 钉 `grep retired_labels video_workbench/web/js/**` 为 0 |
| 字面量禁令只看代码行、注释可以有（退役名/旧写法这类禁令的扫描域必须是**非注释行**） | `tests/wb/test_cli_roles_route.py` 的 `test_the_cli_carries_no_retired_name_table_in_its_code_lines`（先剥注释再扫，保留同一行的代码部分）+ `test_the_comment_that_explains_why_there_is_no_such_table_is_still_there`（解释不许被删）+ `test_the_comment_stripper_keeps_the_code_part_of_a_line_with_trailing_comment`（扫描器自己的形状锁）+ `test_the_source_override_switches_the_file_not_the_verdict`（开关只换文件不换判据） |
| 交差必须贴出受影响文件的原始读数 | 见第六节「每个数字后面必须附产生它的那条命令原文」。**全量是否跑由用户当轮命令决定**（2026-11-04 20:33 起命令为「不跑测试」）⇒ 没跑全量就不许写「全量 0 failed / 已验证不回归」，必须落在「没验证到的」里 |
| 牙齿变体的注入计数器必须**按实例**记，不许挂在模块/插件对象上 | `tests/wb/test_summary_parity_teeth.py` 的 B4 变体用 `getattr(self, "calls", 0) == 3`（`SummaryLLM.chat()` 每条请求自增）。挂在共享对象上 ⇒ 整个 session 共用，翻倍落在最先到三轮的**别的**用例上，目标用例一次没被改动，读数会被写成「这条没牙」（task-54 实测踩过并自报） |
| 常驻灵敏度用例不许把「注入函数返回值 ×N」当期望 | 写成比「注入函数返回的值 == 请求里实际记下的值」。写死倍数 ⇒ 外部叠一次同样注入就变 4 倍，假红；这样既保住「管道断了照样红」的非自证性质，又对叠加免疫 |
| 新建 / 收紧 `tests/wb/*.py` 后，报数之前先贴一行 `git status --porcelain` | 事故实录：`test_git_no_normalization_on_locked_paths.py` 的 task-49 收紧（工作区 18 个用例 / HEAD 17 个，ghost 常驻锁 HEAD 里没有）拖了一整天没入库，双方都把「staged」当成「已交付」——那正是「未入库的闸被当垃圾删」的同族事故。已随 `5c4d956` 收掉 |
| 失败原因不许只到「生成失败」为止 | 归 `tests/wb`（task-55 落地时钉）：`video_workbench/web/js/pages/summary.js:307` 读 `job.error.message`，而 `wb_job` 实测只有 `error_code`/`error_message` ⇒ 界面写死兜底文案，`finish_reason=length，思考用了 16384 tokens…` 这句能自修的 detail 用户永远看不见。判据锁「总结页读的是真实存在的键」，牙齿＝改回 `job.error.message` 必红 |
