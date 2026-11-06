<!-- 已被取代：本文件是 2026-11-06 00:47 那次会话交接，读数只对那一刻成立。
     后续进度以 docs/HANDOFF-2026-11-06.md 与其末尾「收尾」一节为准。
     保留入库只为留痕，不要按这里的 HEAD / 计数当现状引用。 -->
# 会话交接（2026-11-06 00:47 · 用户要求开新对话）

> 用途：新会话不必重读整条历史就能接上。**每条都带当场回显来源**，不写「应该已经」。
> 本文件不属于任何一张卡；下一轮谁改完自己那件事，请把对应行删掉或改成「已落」。

## 一、盘面（命令原文见括注）

- 主仓 `HEAD = 5a8373a`（task-63 那笔），与远端 `0 0`（`git rev-list --left-right --count origin/main...HEAD`）。
  提交链现读前 12 笔：`5a8373a` → `34369ac` → `4378340` → `b1b4e3b` → `9f524e5` → `78ca5bb` → `bf51852` →
  `8a2ef74`(parity) → `40b1762` → `5ea0f23` → `4756382` → `cfd7923`。
- 工作区**未提交**（01:2x 现读）：`M scripts/sync_mirror.py`（我的 task-73 在途）、`?? HANDOFF.md`。
  ⇒ **本文件不引用任何在途状态**：上一版写「`AM tests/wb/test_job_failure_log.py` 还没入库」时它早已随
  `5a8373a` 入库（`git log -1 --format=%h -- 该文件` → `5a8373a`）。要写状态就写「查 HEAD」，别写「现在脏着」。
- 镜像 `%TEMP%/videoworkbench-new` `HEAD = 74bd473`、远端 `0 0`、脏 **1 行**（未查明是谁的）。
- 已发布：**v0.9.2 / tag 解引用 → `f0e3828`**（`git cat-file -t v0.9.2` = `tag`），
  资产 `VideoWorkbench-0.2.1.zip` **177,801,097 B**（v0.9.1 是 177,810,844 B，别混报）。
- 全量（用户撤回「不跑测试」之后我亲跑）：第二次 **`1689 passed, 1 skipped in 785.26s`** 红名单空；
  第一次红 1 条 `test_summary_real_backend_ui.py::test_a_poisoned_backend_sha_shows_up_in_the_page`
  （UI 折叠组 timeout，单跑 1 passed ⇒ 负载互污）。vendor 最新一轮全量红 1 条
  `test_config_migration_notice.py::test_every_close_path_records_the_dismissal[x]`（隔离连跑 3 次全绿）
  ⇒ **同一族「只在整跑里偶发」，两次不是同一条**，没人定性过。

## 二、三条今天新撞出来的事实（还没修）

1. **镜像缺两条判据，而且 `pytest tests/wb` 在镜像里照样全绿**（＝门面缺牙）：
   ```
   $ comm -3 <(git ls-files tests/wb | sed 's#^tests/wb/##' | sort) \
             <(git -C <镜像> ls-files tests/wb | sed 's#^tests/wb/##' | sort)
   test_job_failure_log.py
   test_version_owner.py
   ```
   根因：`scripts/sync_mirror.py` 对「主仓有、镜像从未跟踪」只打一行
   `[note] 主仓独有 638 个路径镜像从未跟踪（同步只走交集，不新建）` 就跳过。
   ⇒ 已发布 **v0.9.2 的镜像里没有版本 owner 锁**（它有 `0.2.1` 那行，没有守它的用例）；
   刚同步那次也没带进 task-63 的新判据（我手工 `--adopt` 过 `test_job_failure_log.py` 一次，
   `cmp` SAME / 14361 B / 镜像里 9 passed —— 但**第二次 comm 仍列出它**，说明那次 adopt 进的是
   工作区/一笔未推的东西或被后续覆盖，**现状就是缺**，别当我修好了）。
2. **`WB_JOB_FAILURE_LOG` 不在已发布的包里**：
   `git log -1 --format='%h %s' v0.9.2 -- video_workbench/app/jobs.py` → **`b2471b8`**，
   带开关的 `5a8373a` 在其后。⇒ 谁问「用户怎么打开这个开关」，答案只有源码路径能用，
   **发布包要重打才有**，不许说「已发布」。
3. **task-73 已从「两处自锁」扩成四处**（01:2x 现读定死第 ④ 处的机理）：
   ① 缺 `-m` 先写完才报错 ② 中断现场没有 `[resume]` 出口
   ③ **新判据被静默漏掉**（上面第 1 条）④ **搬进镜像却不提交 ⇒ 反手把自己锁死**：
   `test_job_failure_log.py` 在镜像**工作区有、`cmp` 与主仓字节 SAME、索引未跟踪** ⇒ 下一次跑前置检查
   会因这行未跟踪直接拒绝同步。第 ④ 处是第 1 条的成因，之前只标了「未查明」。
   哨兵要加 ⑦⑧⑨⑩⑪；夹具 `%TEMP%/lead-sync-head-sentinel` **别删，它是对照实验的现成基线**。
   ⚠️ **11 腿首跑只有 6/11 成立，红的全是夹具缺环境**：`ROOT = Path(__file__).resolve().parents[1]`
   ⇒ 夹具里不放一份 `scripts/sync_mirror.py` 副本，工具就会拿**真主仓**当主仓（实跑回显
   `[ok] 处理 494 项`、夹具镜像因此多出未跟踪 `a.py`/`keep.txt` ⇒ L2/L8/L10 假红）。
   修法：`new_repo(main)` 里放工具副本、`TOOL` 随腿走。**这条不定性成「功能坏」。**

## 三、在途（新会话第一件事先看这两个）

- **task-70（vendor）阶段 1 已交板，等 lead 判**：他报 `ok=45 red=0`、`calls=0`、产品码 0 行。
  **等拍的两件**（他明确没自己选）：
  - 第 4 项 **甲 vs 乙**：甲＝接上那天由 parity 摘/改写画面帧前提锁并显式把「一致」扩到帧；
    乙＝帧走独立字段、不进 `messages` 比对面。**不许出现「先把判据放宽」的写法。**
  - 第 1 项的结论可能是「**做不成**」：若帧输入在 WB 侧不齐备（无本地视频文件 / 无 ffmpeg / 时长拿不到），
    那是方案的第一约束，**不要顺手去补输入链路**（新卡）。
- **task-72（vendor）**：实测 `desktop.py:53 MIN_SIZE = (1100, 720)` 在 WebView2 上是否真被强制 +
  把下限钉成常驻判据（**不钉 1100/720 字面量**）。它现在 `ready`，但和 task-70 同期开工会
  撞真窗口资源 ⇒ 串行。
- **task-71（vendor）**：ruff 债，`video_workbench` 面 28 条 check / **43 个文件待 format**，
  **`ruff format` 至今一次都没执行过**。硬边界：不许为过规则改 `ruff.toml` 的 `exclude`、不许加 `# noqa`。
- **parity** 名下待答：`test_summary_truncate.py` 两条旧字面量是否翻正（他会先现读 `git status` +
  `git log -1 -- 该文件` 再决定，若已被吸进 `5a8373a` 就停手）；夹具格文本长度四处打架 +
  `TEETH_EXPECT["anywhere"]` 的 cell id 漂移。

## 四、这个会话反复踩的坑（新会话请当默认前提，别重新发明）

- **环境**：`.venv/Scripts/python.exe`（裸 `python` 是 WindowsApps alias）；中文探针
  `export PYTHONIOENCODING=utf-8`；`subprocess.run(text=True)` 取中文要
  `encoding="utf-8", errors="replace"`；`%TEMP%` 残留 `video_workbench/` 会抢 `sys.path[0]`
  ⇒ Temp 脚本先 `sys.path.insert(0, REPO)`；比字节必须比**原始字节**（`core.autocrlf` 会伪装成内容差异）。
- **真库不在 `%APPDATA%`**：`C:\Users\b1596\.bili-scribe\bili-scribe.db`
  （`~/.video-workbench/config.yaml:87 db_path` 逐字指它；`%APPDATA%\bili-scribe` 不存在）。只读一律 `mode=ro`，跑前跑后核 sha。
- **报数三铁律**（本会话我用它们各抓出自己一次）：① 每个数字附产生它的那条命令原文；
  ② **七位 commit sha 会被压缩/蒸馏通道写坏** —— 只有当场 `git show --stat` / `git cat-file -t` 算证据
  （已实证：`e4858f0`/`12d85e5`/`d15d3a8` 三个号在这棵树根本不存在，真号 `2e47868`/`f0e3828`/`4756382`）；
  ③ job id / pid / 计数不许凭记忆，写「在跑」必须同条消息带 `job_list` 回显。
- **寻址**：`subagent` **不出现**在 `list_agents`、也没有板面工具 ⇒ 派活只发消息不算交接；
  写权来源是**卡面首行「唯一写者」**，`claim` 成功不等于拿到写权（本会话为此造过一次双写者）。
  给 teammate 发消息 `target` 必须是 `parity-test-engineer` / `vendor-engineer` 全名。
- **板面 API**：`team_task_create` 不认 `dependencies`/`inScope` ⇒ 用 `blocked_by`/`write_scopes`；
  `set_dependencies` 不设写范围；`edit` 的 description 是**整份替换**；
  `complete` 只能从 `in_progress` 走；终端结果不能再 complete，只能 `edit` 追加 `evidence_note`；
  追加前先 `team_task_get` 取 revision（用旧号报 `stale`）。
- **Git**：`commit` 必须带 pathspec 且 `-m` 在 `--` **之前**；`git diff` 对未跟踪文件恒返回空；
  反引号包行号会触发 bash 命令替换（commit message 用单引号）；代理只认 `HTTP_PROXY`
  （`HTTP_PROXY=http://127.0.0.1:7897 git push`），HTTPS 全站不通时先看 `netstat` 再看 TLS，
  `remote rejected … Internal Server Error` 是 GitHub 瞬时 500，退避重试即可。
- **用户偏好**（`~/.dsh/AGENTS.md`）：日常极简、要证据链、不替他决定、**假装做到比说做不到更让他生气**；
  每次问答结束列出本次调用的 skill。

## 五、还没验的东西（不许在报告里写成已验）

真实「下载 + 转录」全链路（整轮都用库里已有 ASR 转录）；`32768` 对非 magpie 端点的成本；
真 WebView2 下的观感（task-68 只跑过 Chromium）；冻结包里的 `bili_scribe.max_tokens` 默认值（在 PYZ 内没量到）；
`ruff format` 的实际影响面；镜像里 vendor 自己那批测试。
