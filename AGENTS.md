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

## 三点五、代码格式与静态检查（ruff 已装，但**一笔格式化都还没还**）

跑法（`.venv/Scripts/ruff.exe`，版本钉在 `requirements-dev.txt` 的 `ruff==0.17.3`；`pip` 只装包，不需要额外下载任何东西）：

```bash
.venv/Scripts/ruff.exe check .                    # 找问题，不改文件
.venv/Scripts/ruff.exe format --check --diff .     # 只报不改 —— 交差用这一条
.venv/Scripts/ruff.exe format .                    # 真改文件（先看清楚上面那条的 diff）
```

- 规则范围与 `exclude` 全在仓库根 `ruff.toml`，**基线数记在那份文件的注释里**：装上当天下默认集 `Found 1272 errors`（567 fixable）、`format --diff` = `288 files would be reformatted, 68 already formatted`；按收窄面复跑 `video_workbench` = **28 条 check / 43 个文件待 format**、`tests/wb` = 204 条、`scripts` = 19 条。
- **锁定面与参考侧本体在 `exclude` 里**（五份 vendored `.py` + 六份 txt + `bili_scribe`）：正常跑碰不到它们；**不许为了让某条规则过而去改 `exclude` 或加 `# noqa`** —— 那等于用格式闸改锁定面，会撞第三节的字节锁。
- **现状登记（task-71 已还完，2026-11-06 现读）**：`ruff format` 已分三笔落地——`video_workbench`（58 files already formatted）、`scripts`（54 files already formatted）、`tests/wb`（109 files already formatted）；`ruff check` 三面 `All checks passed!`，唯一例外是 `scripts/verify_cover_fix.py:20-21` 两条**有意保留**的 E402（`sys.path.insert` 之后的 import，挪了会真炸）。check 面另两笔：重导出改成 `X as X` 显式写法（产品侧 `Summary`/`role_label`/`DanmakuConfig`/`FramesConfig`，tests 侧 pytest 夹具同名复用），`target-version` 从 `py314` 改回 `py310`（README 写 3.10+，py314 会让 format 产出 3.14 才合法的 `except A, B:`）。任何人写「格式闸过了」之前，先想清楚自己跑的是哪条命令、哪个范围。
- `ruff.toml` 的 `[lint.per-file-ignores]` 逐文件点名根下那批一次性脚本（`dump_sub*.py`、`refine-*.py`、`db_*.py`、`poll*.py`、`_*.py` 等）。**那份名单故意不写成通配**：文件删掉之后 `ruff check` 会因为路径找不到报 `E902` —— 那就是提醒回来清名单的牙。别把它"优化"成 `dump_*` 一条通配，那等于把牙磨掉。

**判据**：交付里出现「format / lint / ruff 过关」这句话时，必须同时给出**命令原文 + 范围 + 剩余条数**三个数。

## 四、起真 GUI / 真后端的三条硬知识

1. **隔离夹具只准重定向 `HOME` + `USERPROFILE`，必须保留真实 `APPDATA`/`LOCALAPPDATA`。**
   连 `APPDATA` 一起指到 Temp，pythonnet 的 netfx 宿主会起不来：
   `RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize … pythonnet\runtime\Python.Runtime.dll`；
   试 `PYTHONNET_RUNTIME=coreclr` 更糟：`FileNotFoundException: Could not load assembly 'System.Windows.Forms'`。
   这个报错长得像产品坏了，其实是夹具过界。路径差异靠改写配置副本里的绝对路径解决。
2. **浏览器探针别用 fetch/XHR。** `video_workbench/web/index.html:13` 的 CSP 含 `connect-src 'none'`（`:9` 注释：页面不做任何网络动作，所有网络都在 Python 侧）。桌面 RPC 走 pywebview 桥（`desktop.py:142`），所以正确做法是 Playwright `page.expose_binding` 把进程内 `WorkbenchService().dispatch` 挂成 `window.pywebview.api.rpc`。现成范例：`tests/wb/test_summary_real_backend_ui.py:225`。**不要为了让探针方便而放宽产品 CSP。**
3. **真 GUI 验收前后各记一次三个真实配置文件的 sha256 并逐字比对**（`~/.bili-scribe/config.yaml`、`~/.bili-scribe/workbench.json`、`~/.video-workbench/*`）。窗口可能在你看不见的时候写盘。
4. **夹具想让总结页肯跑任务，必须写真文件，不能只 upsert 库里的字幕行。** `app/service.py:1110` 逐字 `row["has_transcript"] = os.path.isfile(os.path.join(root, "transcript.json"))` —— 读的是磁盘文件，不是 `wb_transcript` 表；页面在 `summary.js:359` 按 `target.has_transcript === false` 拦下，job 根本不启动。只改库的夹具会呈现成「我点没反应」，很容易被误判成界面缺陷。
5. **起真窗口先关掉首屏引导层。** `#onb-overlay` 盖住整页，按产品自己的按钮 `#onb-skip` 关（`onboard.js:142`）——那是真用户路径；用 JS 硬移除节点等于绕过了产品行为。

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
- **落盘的判据只认 `git diff --numstat -- <路径>` 非空（或 bash 侧读到磁盘原文），同侧回显不算证据。** 本轮真实经过：成员报「edit 落了又被快照还原」→ 我据此定量否证（磁盘/索引/HEAD 三份**归一化后** sha 相同，差值 278/1258 恰等于行数＝CR 数，是 `.gitattributes` 的 `* text=auto eol=lf`）；成员随后改口「文件工具与 bash 是两套后端」，又被他自己下一步否证（绝对路径与相对路径读到同一 `revision` 句柄 `cc4b5454ecf07adb…`、278 行、5937 字节 ⇒ 只有一棵树）。**成因至今未定**（`git checkout -- <path>` 不留 reflog，这条无法证实也无法证伪，我不把它写成结论）；能确定的只有一句：**那三笔改动不在工作区、不在索引、不在 HEAD、不在 stash**，而 anyone 拿 `Applied` 加同族工具回显当「已落盘」报出来，就必然在这种地方连错两次。⇒ 写完必须在同一步跑 `git diff --numstat -- <路径>`（非空＝落盘）或 bash 侧 `grep -n <你写的符号> <文件>`；`git status --porcelain` 落文件数行数为 0 就按「没落地」处理。
- **改大文件只用字面替换（`edit`），禁止「`read` 全文 + `write` 整写」**。`read` 单次返回会被 token 上限截断，**返回行数 ≠ 文件行数**（`totalLines` 才是文件行数，但它也只在返回头部出现一次）。事故实录（2026-11-06，task-70）：`tests/wb/test_summary_parity.py` 有 **2290 行 / 124173 B**，`read` 只回了 960 行，我拿那份「全文」整写回去，工作区被截成 **51314 B / 960 行**，现场是一条语法错 `SyntaxError: unterminated triple-quoted string literal (detected at line 960)`——**后面 1300 行用例连同末尾的形状锁一起没了**，而且写入本身是成功的、不报任何错。重建走 `git show HEAD:路径` 取原文 + 在原文上做定点替换 + `write_bytes`，并在同一条命令里贴 字节数 / 行数 / `py_compile` 三行读数。**判据**：任何整写之后必须 `git diff --numstat -- <路径>`，行数与预期改动规模对不上就当场停。
- **我（lead）自己在这条上连错两次，写成永久提醒**：一次拿 `sha256sum < 工作区` 比 `git show HEAD:文件 | sha256sum`（eol 归一化造成的假差异）去定罪别人的工作被抹；一次把别人**已收回**的「两套后端」理论写进本文件当事实，下一轮才被推翻。⇒ 写进 AGENTS.md 的每一条，必须是**我自己现读且 theory-neutral** 的读数；别人收回的推断，不许我顺手固化。
- **派活前确认写者真的存在且可达。** 本轮 lead 起了一个「全新子代理」接 task-68，随后 `list_agents` 里查不到它、`send_message` 报 `target not found`、板面 revision 仍 3 / owner 空 —— 也就是说那张卡从头到尾**没有写者**，而停手待命的成员因此多等了一整轮。⇒ 交接必须先做一次「能不能给它发消息 / 它会不会去领卡」的存在性检查；查不到就不算交接成功，改回原成员名下。
- **上一条只说对了一半，完整版是：`subagent` 与 team teammate 是两套寻址，派活前要确认的是这张卡「只有一个写者」，不是「有没有写者」。** 事故实录（同一张 task-68，2026-11-05）：那个 subagent `157d3e28-3262-45f1-817c-9c81b201793f` **其实一直存在并且干完了整张卡**，只是 `subagent` 不出现于 `list_agents`、也没有板面工具（它自己报 `ops_discover_tools` 查 team_task → `No allowed capability matched`）。lead 因为「`list_agents` 查不到那个 id」就判定「这张卡没有写者」，把同一张卡用 `reassign` 派给 teammate ⇒ **一个任务两个写者改同两个文件**：teammate 18:2x 的三笔改动确实落过盘，随后被实现代理按实测替换掉，现场读起来像「编辑被静默还原」，lead 差点把「文件工具与 bash 是两套后端」这个已被否证的理论写进本文件。⇒ 四条硬规矩：①`subagent` 与 teammate 的寻址不同，`list_agents` 查不到**不等于**它没在干活；②派活前先读板面的 `owner` 与 `status`，非 pending／owner 非空就**不许**再派第二个人；③同一张卡若已被 `reassign`，原写者立刻停手（本条 teammate 主动来问是对的做法）；④**「唯一写者＝谁」必须落在板面上，不许只活在对话里**（事故实录：lead 已对 vendor 说「等我一句再起跑」，同一时间另一只 subagent 仍去 `claim` 了同一张 task-63，两边都零落盘才没造成损失）。落地写法：派活时把「唯一写者 = <名字>」写进卡的 `description` 首行，起跑的人先 `team_task_get` 核这一行是不是自己，不是就不动 —— `claim` 成功不等于拿到写权，板面 `ownerName` 只有一个值而两个执行体能各拿一次同 revision 的 claim 返回，这就是认领与写权在板面上是两套东西。
- **判"落地了没有"只读工作区路径本身，并且必须在同一条命令里同时打印 工作区 / 索引 / HEAD 三份 sha256 + grep 命中，写明引用的是哪一份。** 我（lead）用 `git show :path` 读到旧版，判成"工作区被打回旧版"，给两位成员各开了一张重建单——这种单子会让人真去重写本来好好的代码，比误删文件更贵。`M` 只代表工作区≠索引；落地未 commit 时索引=HEAD=旧版是**正常状态**，不是回退。
- **`team_task_update(action=edit)` 的 `description` 是整份替换，没有追加语义。** 想补证据必须先 `team_task_get` 拿全文，在原文后面接，再整份 edit 回去；直接写新内容会**静默吃掉前人写的判据与合同正文**。本轮 vendor 想用「追加」方式补牙齿证据，一次就把 task-42 的合同正文 + 交付证据全删了（返回值里 `description` 只剩他那段），靠他自己上下文重建才恢复到 rev6。这条比「别人报的行号要现读」更狠：那毁的是读数，这毁的是合同。
  - lead 自己也被这条咬过一次：task-38 结板时我用 edit 写了「判通过」正文，把原始四条判据压成了摘要——原文只活在提交信息 `4fa5aa6` 与消息里，板上不再逐字存在。
- **补键不补值的「同形」判据，必须说清哪些键是惰性的。** 本轮给 `mock.js` 的 `summary.roles` 补齐七个键后，牙齿证明：`retired` 抄真值 ⇒ `state.roleRetired.length` 会变（有消费者）；`extra_prompt` 抄 `true` ⇒ 页面读数**全不动**（前端只从 `settings.get` 读它，`summary.roles` 里那个键没人消费，见 `settings.js:79`/`:338` 对 `app/service.py:694`）。⇒ 形状同源是**维护性规则**（防"真载荷加一列没人跟"），不是行为规则；写判据时行为断言只挂在真被消费的键上，别给惰性键编一条"页面会变"的假判据。
- **"单一 owner"这类规则，值相等的断言拦不住取法退化，必须写行为锁。** 本轮实例：`core/summarizer.py` 用 `from … import role_label` 别名取退役显示名，别名在导入时就绑死函数对象 ⇒ 改权威层输出下游纹丝不动，而"值相等"那条断言照样绿。改成经模块属性现取 `summary_pkg.role_label(...)` 后行为锁才成立。写判据时：patch **权威层的模块属性**（不是适配层别名、不是 subprocess），并同测 `resolve_role` 的两条 notice。
- **引用 commit sha 必须附上产生它的那条命令回显（`git log` / `git cat-file -t`），否则一律改用 subject 或文件路径指代。** 本轮实录：我报给用户的三笔 `e4858f0` / `12d85e5` / `d15d3a8` 在同一棵树上 `git cat-file -t` 全部 `Not a valid object name`，而同主题提交真实存在但号不同（抬 0.2.1 与版本锁实际是 `2e47868`、发布记录是 `f0e3828`）；vendor 那六笔（`1140e3b`/`29080b4`/`e8c72f6`/`d3b21d1`/`33f8903`/`b7c4e91`）他自己在同一棵树上复跑也一样不存在，`git fsck --unreachable` 与 `git reflog --all` 都没有。**形状＝七位 sha 在压缩/蒸馏通道里被写坏**：包、tag、资产都是真的，只有号不可复现。对照物还在：AGENTS.md 本节之外，第七节规则表里引用的 `b2471b8` / `5c4d956` / `f0e3828` 三条我当场 `cat-file` 全真——**写进本文件的号要当场验，凭记忆和凭折叠记录都不算**。附带一条：验 tag 要解引用（`git cat-file -t v0.9.2` = `tag`，`v0.9.2^{commit}` 才是 `commit`），我自己第一版就把这条腿写红过。
- **引用代码位置一律用符号名，不要给行号。** 行号会漂：vendor 在 task-44 里多加一行注释，就把他上一轮报给我的全部行号（`:131`→`:132`、`:273`→`:274` 等）整体推走一位，我自己按旧号去读会读错位置。要定位就 `grep -n "^def role_label\|^def _bound_get_role\|text = \"\" if key is None"` 现取，或在注释/文档里写「`role_label` 里的判空」这种可 grep 的说法。
- **字面量禁令（`grep -n "已停用\|str(key or"` 这类）必须只看非注释行，否则会把"解释为什么没有这张表"判成违规。** 本轮两次实测命中：task-36 的中文退役名、task-44 的 `str(key or` —— 两处命中都在注释里，是实现层写的理由。正确口径是**注释可以有、代码行不行**；写判据时把扫描域收成"非注释行"，同时保留那段解释（删解释等于删掉下一个人的护栏）。
- **源码级判据的「扫描域」必须由环境变量重定向，不许改工作区文件来验红。** 本轮立起来的形状：`WB_CLI_SOURCE_OVERRIDE`（tests 侧 `_cli_function_lines`）与既有 `WB_PROMPTS_DIR`、`WB_WEB_ROOT` 同一族——**牙齿从此有合法入口**：以前「牙齿只准动 Temp 副本」没给测试侧留门，大家只能临时改工作区再改回来，而那正是历史上两次抹掉 vendor 改动的手法。新写源码级/字面量判据时：① 扫描路径走这种开关，别自己造新名；② 必须配一条常驻用例证明**开关只换文件、不换判据**（样板：`test_the_source_override_switches_the_file_not_the_verdict`）；③ 判据覆盖面要说清盯的是哪个函数/哪一段，别把「今天这个形状不复发」写成「整类禁令已成立」。
- **牙齿打补丁的时机分两面，搞反就红不成或红一片**：① 要改**产品侧模块属性**（权威层 `role_label`、`_NEVER_BIND` 这类）必须赶在 **collection 之前**，因为测试模块 `from … import` 的别名在 import 期就绑死了（`import test_xxx as M` 还会 `ModuleNotFoundError`、rc=3）；② 要**红得精准**（只红该红的那一条）就把补丁放进 `pytest_runtest_setup` 按 `item.name` 命中，放 `pytest_configure` 会红一片或根本红不到。
- **同一族三个未决口径，全部属于「偏离 bili-scribe 或收紧契约」，需用户拍板，实现层与测试层都不许自己动**（现状各由一条用例钉住，拍板那天这些用例必须红）：
  ① 副本入口 `_bound_get_role` 对未知/坏输入**静默回落到一把退役键 `standard`**（权威层 `get_role` 是正确抛 `UnknownRole`）—— 回落语义来自 `bili_scribe/core/roles.py:612-613`，由 `test_a_broken_input_currently_falls_back_to_a_retired_key_in_the_copy_path` 钉住；
  ② 带空白的键两条路结果不同：`get_role(" wengao ")` → `'wengao'`（清洗侧），`_bound_get_role(" wengao ")` → `'standard'`（原样交给 bs 查表）—— 由 `test_the_copy_lookup_still_matches_bili_scribe_for_a_padded_key` 拿 bs 原件当对照物钉住；
  ③ 「非字符串按 `str()` 查表」意味着 `__str__` 返回键名的对象会被当合法键 —— 现状登记，没有收紧。
  另有一条**已确认的契约边界**（不是缺陷）：`__str__` 自己抛异常的对象在 `str(key)` 这一步就抛 `ValueError`，三个入口都抛，由 `test_an_input_whose_dunder_str_raises_currently_breaks_the_never_raises_contract` 钉成「期望值就是抛」。
- **一份 `tests/wb` 要在多种仓库形状下跑时，判据必须按形状分支，且两半各留一条独立的牙。** 本仓库就是这个形状：原库根下 `bili_scribe` 是**真目录**（24 个文件该跟踪），镜像库根下它是**目录联接**（跟踪面必须为空）。写成「`git ls-files bili_scribe` 必须为空」在原库天生红；写成「必须非空」在镜像天生红；两种写法都能骗过一半的人。正解（`tests/wb/test_junction_content_not_tracked.py` 的形状）：联接⇒必空、真目录⇒必非空、第三种形状一律红；再加一条已知真目录的对照锁，拦住「拿查不到当没泄漏」。**机制那一半（`git check-ignore` / `git diff --cached` 自己起进程数）不许复用形状锁用的那个 helper**——否则 git 语义变了和本仓判据坏了会红在同一条上，两半互相兜底＝两半都没牙。事故出处：lead 2026-11-05 同步镜像时用主仓 `.gitignore`（没有 `/bili_scribe` 那两行）覆盖，`git add -A` 当场把 junction 后面 24 个 bs 源文件暂存成待新增，靠 staged 列表里出现 `A bili_scribe/core/roles.py` 才发现。
- **主仓 → 发布镜像的同步只能走 `scripts/sync_mirror.py`，禁止手工 `cp -p` + `git add -A`。** 上面那条形状锁拦的是**已入库**的泄漏，拦不住「覆盖发生了、但那一次没 commit」——今天就是这一种，靠肉眼在 commit 前发现。这个入口把肉眼换成判据：镜像的 `.gitignore` 必须含 `/bili_scribe` 行（**且 `.gitignore` 本身从可写集里硬排除**，两边那 3 行差异是设计，不是漂移）；形状与索引面必须自洽；工作区不干净只放行一种——整行就是 `M third_party/bili-scribe` 且 submodule 内部干净（那是「上次对齐了 checkout 忘了 commit」，今天真撞上）。同步范围只取**两边共同跟踪且字节不同**的路径，主仓独有的永不新建（只有 `--adopt` 显式点名才放行）；提交用显式 pathspec，暂存后若出现 `bili_scribe/` 条目立即 `restore --staged` 并拒绝提交。**另有一条 Windows 硬前提：所有 `git ls-files` 必须带 `-c core.quotepath=false`** —— 不带时六个中文文件名输出成八进制转义串，`Path(...).is_file()` 恒 False，它们会被误判成「主仓已删」，`--apply` 就会去删镜像里那六份**字节锁定的真源 txt**（我第一版真写出来了，被哨兵夹具的 `deleted` 那一栏抓到）。
- **主仓的删除也要传播，而且「删除面」必须比「认领面」窄。** 评审 R73-1 指出旧写法是**死分支**：
  `gone = [p for p in common if head_bytes(main, p) is None]`，而 `common` 来自
  `tracked_lines(main)` = `git ls-tree -r HEAD` —— 主仓 HEAD 已经删掉的路径**根本不在 a_set 里**，
  所以这一支永远取不到东西，主仓删的文件在镜像里永久留着（决定档改名那一次就是这么漏的）。
  正解反过来取「镜像 HEAD 有、主仓 HEAD 没有」，但**只限 `DELETE_PREFIXES = ("video_workbench/", "tests/wb/")`**
  两面：`scripts/` 里住着发布仓自有的 `link_bili_scribe.py`，`docs/` 整体不在产品面上，把它们算进删除面，
  第一次同步就会去删发布仓自己的工具（那是 `_new_product_targets` 那笔事故的反向版本，同一个错误方向）。
  非删除面的「镜像有、主仓没有」继续按 `mirror_only` 报给人判，绝不动手。
- **A/B 用「上一版实现」跑常驻判据时，崩掉的那一条必须记成 RED，不能留成 traceback。**
  实录（2026-11-06，R73-3）：给哨兵加了 L18 三条腿（进程内 `mod.main()` + 把 `current_main_head` 换成
  「第一次给真 sha、之后给假 sha」以**确定地**复现「规划之后主仓又前进」），拿 `git show HEAD:scripts/sync_mirror.py`
  做 A/B 时整棵驱动死在 `AttributeError: module 'sm' has no attribute 'current_main_head'`、`AB_EXIT=1`、
  日志里连 `ok=` 那行都没有 —— 这个读数和第二天没人重跑的「没红」长得一模一样。
  正解：整段包 `try/except AttributeError`，except 里 `check(那条, False, "AttributeError: … —— 这支闸不在这份实现里")`。
  现读两条读数：工作树版 `ok=40 red=0`（`EXIT=0`），HEAD 版 `ok=37 red=1` 且红的正是那条自证腿。
  **同类口径**：判据脚本的退出码 `∉ (0,1)`（崩、收集失败、内部错）一律不许读成「没红」——这是第二节那条的加强版：
  断言写对了但**跑崩**，仍然是假绿的一种。
- **`sync_mirror.py` 的退出码有两种，腿位必须按真的那一种断。** 现读 `main()`：
  **`rc=2` = `early_arg_check` 在 preflight 之前把参数问倒**（未入库的 `--adopt`/`--from-head`、缺 `-m`、落点越界）；
  **`rc=1` = `preflight` 返回 blockers**（junction 形状不对、镜像 `.gitignore` 缺排除行或被改脏、镜像工作区不等于 HEAD 版）。
  两类都必须零写入。**事故实录**：L15 那三条腿我先前写成 `rc=2` 并记成「全绿」，实际跑出来是 `rc=1`——
  三条腿连着红了一轮才被重新 A/B（HEAD 版与工作树版各跑一次）抓到。⇒ 凡是断退出码的腿，
  先 `git show HEAD:脚本 > 副本` 跑一次 A/B，再信「绿了」。
- **常驻判据必须住进仓库，`%TEMP%` 里的哨兵等于不存在。** 事故实录：sync_mirror 的 27 腿哨兵在
  `%TEMP%\lead-sync-head-sentinel\driver.py` 里活了好几天，读数只在我的对话里 —— 交接文档里写成「%TEMP% 那份」，
  下一个人既不知道要跑也拿不到文件（评审 R73-9 点的就是这条）。现在它落成 `scripts/sync_mirror_sentinel.py`，
  默认自己定位被测的 `scripts/sync_mirror.py`，工作目录默认在系统临时区下的固定名。
  **开局会整棵删掉那个目录**（上一轮夹具留着会让下一条腿读到脏东西），所以有一条自证：目录名必须就是
  `wb-sync-mirror-sentinel[-后缀]`，否则 `rc=1` 拒跑。牙齿已验：指着一个放重要文件的非哨兵目录跑 →
  `RC=1`、`目录还在=True 文件还在=True`。同一轮补的 L15 三条腿钉住「镜像 `.gitignore` 被改过」这一类（现读读数：**都是 `rc=1`，不是 2**）：
  删掉 `/bili_scribe` 排除行会被 junction 那一行拦住；**排除行还在、但多吞了一个产品文件**（`a.py`）这种形状
  靠的是脏面判据（它不等于主仓 HEAD 版 ⇒ `not_head`），不是那一行 —— 所以控制组 L15c 必须证明干净时同一笔照常同步，
  否则这两条腿会一起退化成恒真。**同类判据（只活在对话或临时区的验证脚本）落地时就写进 `scripts/`，
  别等别人来问「那个哨兵呢」。**
- **中文关键词从 run_code 递给 pwsh 会被控制台编码弄乱，「0 命中」不等于「文件里没有」。** 事故实录（2026-11-06，task-70）：
  我用 `Select-String -Pattern "落地记录"` 扫 `docs/decisions-product-defaults.md` 得 0 命中，据此判定「上一轮写进决策档的
  落地记录是假交付」，还把这个结论写进了压缩摘要——`read` 现读才发现那一节好好在 157–195 行。同一次调用里三个 ASCII 模式
  （`MIN_SIZE`/`min_size`）都正常命中，只有中文那条空，报错信息里模式串显示成乱码 ⇒ **是传参被 GBK 折了一遍**，不是文件问题。
  **判据**：中文关键词的存在性判据要么用 `read`/`grep` 工具，要么先 `$OutputEncoding = [Text.UTF8Encoding]::new()` 再传；
  拿 pwsh 的中文 grep 0 命中去定罪别人的工作「没落盘」，和拿 `git diff` 判未跟踪文件是同一类错误。
- **真模型调用要有预算闸，起跑前先报预估，单张任务卡上限 3 次真实调用。** 今天已经烧掉的两笔（写在这里当例子，不是抽象教条）：task-52 长素材 11 次调用 `prompt 154748 / completion 141260`（≈29.6 万 token，602s）；实配抬到 32768 之后走真配置又跑一遍同样的长素材（11 次、约 770s，出处 `%TEMP%/wb_live32768.log`，149 行）。**两张卡的正文都没写过「真模型能花多少」**，所以谁都不觉得自己在花钱。四句口径：
  ① **起跑前必须写一句预估**：模型名 × 预计调用次数 × 单次大概 token 形状。写不出预估 = 不许起跑。
  ② **单张卡真实调用上限 3 次**，超过要先拿到 lead 显式放行（lead 拿去问用户）。
  ③ **桩不算真调用**：像 task-59 那种只记「真发出去的 `max_tokens`」的 `StubClient`（不联网、不建库、不改 HOME 之外）可以放开跑；**不要为了让读数像端到端而给它接真网络**。
  ④ **真跑过必须报 `calls=N`**，不许只写「跑通了」。只报结论不报次数，下一轮没人在预算里。
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
| 引用 commit sha 必须附产生它的命令回显（`git log` / `git cat-file -t`），否则改用 subject 或文件路径指代 | 本轮实录：我报给用户的 `e4858f0`/`12d85e5`/`d15d3a8` 在同一棵树 `cat-file -t` 全部 `Not a valid object name`，同主题提交真实存在但号不同（抬 0.2.1 实为 `2e47868`、发布记录 `f0e3828`）；成员那六笔他复跑也不存在、`fsck --unreachable` 与 `reflog --all` 都无。⇒ 七位 sha 会被压缩/蒸馏通道写坏，**只有当场回显算证据**。另一条同族错：tag 要解引用（`cat-file -t v0.9.2` = `tag`，`v0.9.2^{commit}` 才是 `commit`），我自己第一版把这条腿写红过 |
| 牙齿变体的注入计数器必须**按实例**记，不许挂在模块/插件对象上 | `tests/wb/test_summary_parity_teeth.py` 的 B4 变体用 `getattr(self, "calls", 0) == 3`（`SummaryLLM.chat()` 每条请求自增）。挂在共享对象上 ⇒ 整个 session 共用，翻倍落在最先到三轮的**别的**用例上，目标用例一次没被改动，读数会被写成「这条没牙」（task-54 实测踩过并自报） |
| 常驻灵敏度用例不许把「注入函数返回值 ×N」当期望 | 写成比「注入函数返回的值 == 请求里实际记下的值」。写死倍数 ⇒ 外部叠一次同样注入就变 4 倍，假红；这样既保住「管道断了照样红」的非自证性质，又对叠加免疫 |
| 新建 / 收紧 `tests/wb/*.py` 后，报数之前先贴一行 `git status --porcelain` | 事故实录：`test_git_no_normalization_on_locked_paths.py` 的 task-49 收紧（工作区 18 个用例 / HEAD 17 个，ghost 常驻锁 HEAD 里没有）拖了一整天没入库，双方都把「staged」当成「已交付」——那正是「未入库的闸被当垃圾删」的同族事故。已随 `5c4d956` 收掉 |
| 真模型调用不许无预算：起跑前报预估，单卡上限 3 次，真跑必须报 `calls=N` | 见第六节那条（含今天两笔实际用量当例子：`prompt 154748 / completion 141260` 与 `%TEMP%/wb_live32768.log`）。**拦住它的是交付正文里必须出现的 `calls=N` 这一个数**——leader 核板时看见真跑却没报次数就退回；这是需要判断的规则，没有机器闸（桩与真调用的区别靠 `StubClient` 不联网的形状自证，不额外造锁） |
| 一份 `tests/wb` 跨两种仓库形状（原库真目录 / 镜像联接）不许写成恒真或恒假 | `tests/wb/test_junction_content_not_tracked.py`（6 条：形状锁按联接⇒跟踪必空 / 真目录⇒必非空分支、第三种形状红 + 已知真目录对照锁 + 机制锁在 `tmp_path` 两个各自独立的最小仓库里自己起 `git add -A` 数 `git diff --cached`，不调形状锁的 helper）+ 牙齿六变体 V1–V6 与 V7「用主仓 `.gitignore` 覆盖镜像」逐字复现事故。镜像里实测 `LinkType: Junction`、`git ls-files -- bili_scribe` = 0，走第一支 |
| 镜像同步不许手工 `cp -p` + `git add -A`，只能走入口 | `scripts/sync_mirror.py` 的 `preflight()` + 显式 pathspec（`.gitignore` 在 `NEVER_SYNC` 里、镜像那份必须含 `/bili_scribe`、形状与索引面自洽、工作区只放行 gitlink 那一种脏）。常驻判据是 `scripts/sync_mirror_sentinel.py`（37 腿，每条腿在临时最小仓库对里跑真子进程，不碰工作区也不碰真镜像）：`$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -u scripts/sync_mirror_sentinel.py` → 实测 `ok=37 red=0`、`EXIT=0 ELAPSED=31s`。腿位覆盖：L1–L3 同步范围（在途不搬／只搬 HEAD／staged 不搬）、L4–L5b 未入库 adopt/from-head rc=2 且零写入、L6 `head_bytes`/`rel_exists_in_head` 读法、L7 缺 `-m` 先验后写、L8–L9 `[resume]` 正反例、L10 判据自动搬且搬进去能 collect、L11 非产品独树不新建 + 幂等、L12 `--from-head` 救「工作区已删」、L13 落点越界（联接逃逸）、L14 半截写不留残骸、L15 镜像 `.gitignore` 被改脏（rc=1 那两条）、L16 主仓删除传播与「面只认 HEAD」正反两半。**它不住 `tests/wb`**：同步入口的判据归这个驱动，`tests/wb` 那半边归 parity，不在两处造第二个 owner。**旧的那份 %TEMP% 六组驱动（①④⑥ 绿／②③⑤ 红）已被这 37 腿取代。** |
| 失败原因不许只到「生成失败」为止 | **已落地（task-55，commit b2471b8）**：拼接 owner 是 video_workbench/app/jobs.py 的 error_message_for_row（同文件的常量 ERROR_DETAIL_JOINER 值为「 —— 」、常量 ERROR_MESSAGE_MAX_CHARS 为 600），在 to_payload() 里写进 error_message 那一列；前端在 summary.js 的失败分支里逐字写 new Error(job.error_message ‖「生成失败」)。判据归 tests/wb/test_summary_error_display.py（7 条，lead 复跑 7 passed in 0.22s）。牙齿＝把前端取法改回 job.error.message 必红。**别再把它写成「待落地」**（本轮 lead 就差点拿旧规则表当现状读） |
| 包内自显示的版本号只有一个 owner | **已落地（task-69 连带三，commit 2e47868；`git log --diff-filter=A -- tests/wb/test_version_owner.py` 现读）**：判据是 tests/wb/test_version_owner.py 六条（lead 现跑 6 passed in 1.28s），断言「载荷／关于显示的版本 == video_workbench.__version__」而**不钉字面量**；同文件里还有一条机械闸——产品码非注释行里版本号字面量只准 video_workbench/__init__.py 那一行出现（扫描前先剥整行与行尾注释，docs 与 tests 不扫，理由写在 docstring）。牙齿＝把那一行的字面量写回 0.2.0 而显示层不动 ⇒ 机械闸与 owner 形状锁必红。归 lead，不派 vendor，与 test_summary_error_display.py 不撞文件。**别再把它写成「待落」**（本轮这条规则表就差一点又变成过期口径） |
| 镜像 `.gitignore` 被人改过（哪怕 junction 排除行还在）不许照样同步 | `scripts/sync_mirror_sentinel.py` 的 L15a/L15b/L15c/L15d：删排除行 → `rc=1`（preflight blocker，不是参数错）+ 产品文件零改动 + 文案点名 `'/bili_scribe'`；排除行还在、但那份文件被改脏（多吞了一个同步范围内的产品文件——夹具里的主仓独有件）→ 同样 `rc=1`；L15d 拿 L15b 自己的读数比两半——必须有「不等于」那条、必须**没有** junction 那两条，这才证明第二种形状不是被排除行单独兜住的；`.gitignore` 干净 → `rc=0` 且真的搬进 HEAD 版（控制组，防三条腿变恒真） |
| 主仓 HEAD 删掉产品面文件，镜像必须跟着删；非删除面只报不动 | `scripts/sync_mirror_sentinel.py` 的 L16a–L16f（`ok=37 red=0`，命令 `$env:PYTHONIOENCODING='utf-8'; .venv/Scripts/python.exe -u scripts/sync_mirror_sentinel.py`）：L16a/L16b 删除真的落进那一笔提交（镜像 `ls-tree HEAD` 不再含它）；L16c/L16d 反向——`scripts/` 下镜像自有工具不许被误删，主仓真删它也只按 `mirror_only` 报出来；L16e/L16f 是同一族「面只认 HEAD」的正反两半：只 staged 的删除不许搬，commit 之后必须立刻搬 |
| 常驻判据（哨兵 / 验证驱动）必须住进仓库，`%TEMP%` 里的那份等于不存在 | 实录：sync_mirror 最早那 27 腿哨兵在 `%TEMP%` 活了好几天，只有我能跑，交接文档写成「%TEMP% 那份」就等于一句没有执行入口的话（评审 R73-9）。现已落成 `scripts/sync_mirror_sentinel.py`。**拦住这类事的是判断，不是机器闸**：结板时看见「判据 = 某个临时路径下的脚本」就退回，要求落到 `scripts/`（或 `tests/wb/`）并贴出可复跑的命令原文。落地时顺带给「开局会删工作目录」这类脚本加一条命名自证（哨兵实测：非哨兵名目录 → `RC=1` 且目录完好） |
| 改大文件不许「read 全文 + write 整写」（read 会被 token 上限截断，静默删掉尾部） | 第六节那条事故读数（2290 行只回 960 行 ⇒ 工作区 51314 B）+ 交付里必须附 `git diff --numstat` 与 `py_compile` 读数；需要整文件重写时从 `git show HEAD:路径` 取原文，不拿 `read` 的返回值当基线 |
| 中文关键词的「0 命中」不许当「文件里没有」——从 run_code 递给 pwsh 的中文模式串会被控制台编码折成乱码 | 实录见第六节那条（`落地记录` 那条假阴性把我自己的决策档判成了假交付）。**拦住它的是判断，不是机器闸**：中文存在性判据走 `read`/`grep` 工具，或在 pwsh 之前显式设 `$OutputEncoding`；ASCII 模式命中、中文模式空命中这一组合出现时，先怀疑传参编码再怀疑文件 |
| 「ruff format / check 过关」这句话不许空口说 | 命令口径见第三点五节。**拦住空口的不是机器闸，是「命令原文 + 范围 + 剩余条数」这三个数**：只写「过了」而给不出这三样，核板时按没跑退回。锁定面与 `bili_scribe` 必须同时进 `[format] exclude` 与 `[lint] exclude`（0.17.3 的顶层 `exclude` 只落到 formatter，`linter.exclude` 实测为空 ⇒ 一次 `check --fix` 就会改 vendored 字节；`[lint]` 不认 `extend-exclude`，写错所有 ruff 命令直接 rc=1）。**`--show-files` 不能当判据**（它只列解析器扫到的文件，不套 lint 排除）；正确的三条读数是 `--show-settings` 里 `linter.exclude` 含锁定面、`check --statistics` 的条数、`check --select F401,I001 --fix --diff` 的 diff 头不含 `video_workbench/core/summary/roles.py`/`video_workbench/core/summary/summarizer.py`。加 `# noqa` 只准用在**pytest 夹具重导出**与**有意的 `sys.path` 前置**两类形状上，且必须带一句为什么 |
