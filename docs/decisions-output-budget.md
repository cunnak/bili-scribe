# 决策记录：长素材输出预算（A + C），以及不做历史重写

> 归档方式说明：本会话的 Hindsight 记忆写入仍然失败——
> `POST https://api.hindsight.vectorize.io/v1/default/banks/coding-agent%3A%3A视频内容总结/memories -> 401 {"detail":"Authentication failed: API key required"}`
> 所以按纪律落成本文件，**没有记忆节点 id**。修好 key 后整篇 ingest 即可。

## 一、用户拍板原话

> 「A + C 一起做，不做 filter-repo」

对应上一轮我摆的三条路（A 抬默认值 / B 按轮分配 / C 只改指引）+ 两件遗留（filter-repo、真窗）。真窗那条已作废（`tasklist` 实测 0 进程）。

## 二、CCG 六要素

**功能名**：长素材输出预算与配置指引修正（task-50 取证 → task-52/51 落地）

**生效条件**：素材走分段预读（bundle > `max_input_chars=24000`）+ 角色 `passes=3`（renhua/wengao/…）+ 端点为 magpie/workbuddy 路由（思考 token 计入 `max_tokens`，`enable_thinking` 被忽略）。

**子功能**
1. A：`bili_scribe/config.py` 的 `LlmConfig.max_tokens` 默认 `4096` → `32768`；`config.wb.example.yaml`、`config.example.yaml` 模板同步。实配三份（`~/.video-workbench/`、`~/.bili-scribe/`、仓库根 `config.yaml`）**不动**。
2. C：WB 侧 `render_yaml` 不再继承「这段推理不吃 max_tokens」这句已被实测推翻的指引；`bili_scribe/**` 的那两处注释**保持原样**（用户只授权了默认值那一行）。
3. B 被否决：按轮分配预算要求适配器显式写 `max_tokens`，会主动破「与 bili_scribe 逐请求一致」——那正是本项目最初的主命令。
4. D 不成立：`workbuddy/deepseek-v4.1-flash` 在长素材 16384 下同样死；`workbuddy/glm-5.3-flash` 16384 能过但预读方差大（第 3 段回 20320 字）、总 809s。默认模型写死等于绑端点。

**执行**（实测依据，命令原文在 task-50 的板面 output 与 `Temp/wb_t50_*.json`）
- 三轮输入字符：generate 49504 / critique 50674 / **revise 55013**；三轮都不写 `max_tokens`，严格档 `_parity_max_tokens` 原样补配置值 ⇒ **每轮预算恒等于 `cfg.llm.max_tokens`**；`_json_budget` / `MIN_JSON_TOKENS=12288` 那套阶梯一次都没参与。
- 16384 下失败点逐字：`finish_reason=length，思考用了 16384 tokens。把 max_tokens 调大，或换一个非推理模型，再试。`
- 32768 下：11/11 全过，revise 回 4253 字，总 541s。
- 端点不遵守 `enable_thinking`：同一小题三发 `reasoning_tokens` = auto 110 / off(发 false) **140** / on 106 ⇒ bs 的「空正文自动关思考重试」自愈腿在 magpie 上**空转**，同 payload 下 bs 也会走到 `bili_scribe/infra/llm.py:452 raise LlmError`。**所以这不是 WB/bs 一致性破口，不开那条修复单。**

**验证方式**
- V1 三处默认值/模板现读都是 32768，且 `bili_scribe/config.py` 除那一行外无其他改动。
- V2（决定性）**改完默认值后走默认值路径真跑一次长素材端到端**，拿 11/11 与 revise 正文读数。默认值改了却没在长素材上验过＝没改。
- V3 报错文案与 detail 两处 `except ChatError as exc: raise WBError(exc.code, exc.message, exc.detail)` 不许动；前端那一跳渲染的是 `message` 还是 `app/errors.py:26` 的码表条目，由 vendor 现读回报（我先前那句「文案没说谎」是从后端推的，前端没读）。
- 判据侧：写死 4096 当期望值的一律改成现读 `cfg.llm.max_tokens`；新增一条常驻判据钉住「三轮预算恒等于配置值、不按轮抬」，让将来任何按轮分配都必须显式翻判据并留偏离登记。

**不适用条件**
- 短素材（180 段 / 1892 字）在 16384 下本就能过（134.3s，timeline 16，caution 如实指出素材缺陷）。
- 非思考端点（直连 DeepSeek 等）预算语义不同：`max_tokens` 是上限不是实付，抬默认值对它们是上限抬高而非多付。
- bili-scribe 自身用户会跟着拿到新默认值——用户已授权这一行，属于有意共享。

## 三、不做 filter-repo 的残留（明确记账，不是遗忘）

- 公开仓库 `cunnak/bili-scribe` 历史里 commit `2a17d38` 曾带真实 LLM api_key（`report/` 下 7 个 yaml 的第 18 行）。
- 已做的：`1fa3af0` 脱敏文件内容；用户吊销换发该 key（我复验 401 `invalid key: A56702`）。
- 仍在的：历史那一笔里的原文，公开可取。**不做 filter-repo** ⇒ 不重写 hash、不强推、tag/release 全部保持现状。
- 附带：`report/relcheck/bili-scribe.exe` 与 `bili-scribe-0.7.0.zip` 等 48 个被跟踪二进制继续留在库里（体积 97.54 MB）。

## 四、本轮新增的结构事实（会影响后续所有测试落地）

- WB 已拆成独立仓库 **`cunnak/videoworkbench`**（PRIVATE，4 笔 commit）：`video_workbench/` + `tests/wb/` + 49 个 WB 验收脚本 + 决策记录 + `AGENTS.md`/`README-VideoWorkbench.md`/`config.wb.example.yaml`/`requirements-wb.txt`/`requirements-dev.txt`/`video_workbench.spec`/`package*.json`/`LICENSE`/`.gitattributes`。
- `bili_scribe` 以 **submodule** 挂在 `third_party/bili-scribe`，钉 `v0.9.1`（= `d823ee9`），**不复制源码**；仓库根的 `bili_scribe` 是**目录联接**，由 `scripts/link_bili_scribe.py` 生成、已进 `.gitignore`。原因：submodule 根是整个 bili-scribe 仓库，包在里面一层，直接挂会让 `import bili_scribe.config` 失败。
- 新库现跑四把锁：`pytest tests/wb/test_summary_drift.py tests/wb/test_git_no_normalization_on_locked_paths.py tests/wb/test_summary_prompt_source.py tests/wb/test_cli_roles_route.py -q -rf -p no:randomly` → **102 passed in 15.11s**。
- **未决（我下一轮拍）**：同一套 `tests/wb/` 现在有两个仓库要同步。三个候选：① 新库只跟产品码、tests 从原库生成；② 两边都提 + 加一条「两库 `tests/wb` 逐字节一致」的闸；③ 新库只当发布镜像，测试继续只在原库写。本轮成员仍在原库落地，不动这个决定。
- release：**`v0.9.1`** 已发，资产 `wb_dist_0.2.0.zip` 177,810,844 B（onedir 整目录，单独 exe 起不来）。
- release：**`v0.9.2`** 已发，资产 `VideoWorkbench-0.2.1.zip`（onedir 整目录，1959 个文件入包、zip 177,801,097 B）。v0.9.1 那版**不含**这三件事：默认输出预算抬到 32768、失败原因把 `message —— detail` 拼给界面（task-55）、窄窗错误提示条一行收起 + 真按钮展开（task-68）；包内自显示版本也从 0.2.0 抬到 0.2.1。**v0.9.1 的资产不替换**，老链接保持可下载（用户 m06761 拍「发 v0.9.2」）。
- 发布镜像的 gitlink 同步抬过两次：`c8b3376`（默认值那一行）→ `df305ad`。在此之前镜像钉的是 `d823ee9`，junction 读出来 `bili_scribe/config.py:57 = 4096` —— **从 `c2a1215` 起任何 clone 镜像的人拿到的默认值都与主仓不一致**，这条是真缺陷、已修。同步入口后来立成 `scripts/sync_mirror.py`（禁手工 `cp -p` + `git add -A`，见 `AGENTS.md` 第六节）。

## 附：本轮结案但**仍等用户拍**的三条

1. **WB 的总结链路永不附画面帧，bs 默认附最多 12 帧**（lead 独立现读）：`grep -rn "collect_frames" video_workbench/ --include=*.py` = **0 命中**；适配层 `video_workbench/core/summarizer.py:469` 有 `frameset: frames_mod.FrameSet | None = None` 形参但**没有任何调用点传它**（`:906-908` run、`:990-998` refine 两处都没有 `frameset=`）⇒ 副本 `core/summary/summarizer.py:550` 归约那一次 `frame_blocks = []`、`:626 frames=0`、`:555` 提示词里那句「已附 N 帧画面」永不出现。对照 `bili_scribe/app/service.py:638`/`:770` 真调 `collect_frames` 并在 `:686`/`:786` 传 `frameset=`，默认 `FramesConfig.enabled=True`、`max_frames=12`、`quality=85`。
   **不是「本来就不可能」**：真库 `wb_project` 32 行里 `aid` **全为 NULL**，但 `bvid` 24 行有值（8 行为 NULL）⇒ bvid 可解析出 aid，抽帧在数据上成立。所以这是**未迁的功能**，不是环境限制。
   ⇒ 影响本轮反复写的那句「两边输出内容一致」：在有画面可抽的素材上，两边**输入不同**（bs 最多多 12 帧图）。要拍的是：要不要补这条输入（约 1,464 image_tokens / 一条 4 小时视频总输入的 1.7%，出处是 bs 那段 docstring，**这台机器没实测过**），以及在此之前「一致」这句话该怎么写才不夸大。归用户。
2. **WB 要不要支持窄窗**：360 档主区只剩 92px 是布局造成的（侧栏 `#rail-nav` 实测 203px、`app.css` 的 `grid-template-columns: 168px minmax(0,1fr)`；全仓 `@media` 只有 `app.css:498 (max-width:1200px)` 一条），与文案长短无关。若产品从不需要窄窗，task-68 是在给不存在的场景做适配，真正该做的可能只是设窗口最小尺寸。归用户。
3. **要不要把 ruff 装进 venv**：`requirements-dev.txt` 全文只有 `playwright==1.64.1`，`grep -rn ruff pyproject.toml ruff.toml setup.cfg pytest.ini tox.ini` 零命中，`.venv` 里无 ruff ⇒「ruff format --check 过关」这句话**从来不是本仓库的闸**，谁都验不了。装它属于新增依赖。归用户。

## 附：本轮流程上的一次事故（记在这里防止重演）

**同一个任务卡两个写者**。lead 因为 `list_agents` 查不到某个 `subagent` 的 id（subagent 与 teammate 是两套寻址、也没有板面工具），判定那张卡「没有写者」，把它 `reassign` 给 teammate —— 实际那个 subagent 一直在干活。结果 teammate 的三笔改动落过盘、又被 subagent 按实测替换掉，现场读起来像「编辑被静默还原」，lead 一度把「文件工具与 bash 是两套后端」这个已被否证的理论写进 `AGENTS.md`。
正解与硬规矩写在 `AGENTS.md` 第六节：**派活前确认的是这张卡「只有一个写者」，不是「有没有写者」**；派活前先读板面 `owner`/`status`，非 pending 或 owner 非空就不许再派第二个人。
