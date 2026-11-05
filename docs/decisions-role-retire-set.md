# 决策：WB 角色表收口走 A 方案（退役三键同集合）

> 2026-11-04 19:0x。用户命令原文：「合并掉，只留「说人话」，「学术分析」删掉，「超强思考模式总结」保留」。
> 参与：vendor-engineer 摆证据，lead 拍板，parity-test-engineer 负责测试面（task-32）。

## 结论

**A：`RETIRED_ROLES = frozenset({"standard", "xueshu", "shipin"})`**——三键同集合。
**B（另设 `_RETIRED_BY_KEY` 只给显示用）正式否决。**

公开角色最终 6 个：`renhua` / `shensi` / `wengao` / `xiezuo` / `fenxi` / `jinglian`；`视频总结.txt` 从绑 `shipin` 改绑 `renhua`。

## 理由（三条，均盘上现读）

1. **B 是零实现**：`grep -rn "_RETIRED_BY_KEY" video_workbench | wc -l` = 0。
2. **B 要越界**：回落与显示两处判定都只认 `RETIRED_ROLES`（`video_workbench/core/summarizer.py:136`、`:1072`），走 B 得改 `core/summarizer.py`，而它不在写范围内。
3. **B 有可感知退化**：`xueshu` 不进 `RETIRED_ROLES`，老配置残留的 `role: xueshu` 会从「回落默认 + 日志提示」变成 **VALIDATION 硬拒**。

A 之下 `:136`/`:1072` 一行不改，「回落 + 历史行（已停用）」自动成立。

## lead 自我更正（记录在案）

我 18:2x 说过「`:1072` 已改读 `_RETIRED_BY_KEY`」——**错的**：那个标识符全仓不存在，是我没现读就转述成员的话。「引用别人给的标识符前自己读一遍」这条规矩就是为这种时刻立的。

## 顺带确认的四处（vendor 提，lead 认）

| 位置 | 现状（盘上现读） | 改法与理由 |
|---|---|---|
| `core/summary/__init__.py:159` | `f"标准（{text}）角色已停用…"` 写死「标准」 | 改 `f"{name}（{key}）角色已停用，请选一个方法论角色"`。三键下 `get_role("xueshu")` 抛「标准（xueshu）…」= 假话。standard 输出逐字不变 ⇒ 钉死该句的三处常量（`test_summary_role_resolution.py:77`、`test_settings_role_warning_ui.py:227`、`test_summary_drift.py:478`）必须仍绿 |
| `core/summary/__init__.py:178` | `role_label` 返回写死 `"标准（已停用）"` | 改 `f"{name}（已停用）"`；`grep -rn "标准（已停用）" tests/wb/*.py` = 0 命中，无用例钉它；`core/summarizer.py:1067` 与 `__init__.py:15/:171` 的注释一起按名字重写 |
| `__init__.py` 新增退役名字典（`shipin` 专用） | `_WB_ONLY = ()` 后 Role 对象消失，bs 表里从来也没有 `shipin` | 放一个键到名字的字面量映射（`shipin` → 「视频总结」，取自 `视频总结.txt` 文件名）并加注释说明它只用于显示退役行 ⇒ `role_label("shipin")` = 「视频总结（已停用）」，界面上 retired 与 unknown 分得开 |
| `prompt_source.py:47 _NEVER_BIND` | 现为 `frozenset({"standard"})`，别处不消费 | **必须补 `xueshu`/`shipin`**。他现读 `bind()`：`:16` 命中 `_NEVER_BIND` 就 continue、`:22` 无 MAP 则落 `kept_compressed`，而 `_targets()` 遍历副本 `ALL_ROLES`（8 个含 `xueshu`）⇒ 不补就会让 `xueshu` 落进 `kept_compressed`，直接撞 `test_summary_prompt_source.py:321`（`kept_compressed == NO_SOURCE`） |

## `NO_SOURCE` 的正确口径（我先前写反过一次，以此为准）

合并后 `NO_SOURCE` 只能剩 `("shensi",)`。决定性不变量：`:242 set(MAP)|set(NO_SOURCE)==public`、`:244` 名单外绑定为空、`:245 not (MAP & NO_SOURCE)`、`:321 kept_compressed == NO_SOURCE`。
⇒ `renhua` 有源必须离开 `NO_SOURCE`（否则撞 `:245`）；`xueshu` 退役后不在 public（留在 `NO_SOURCE` 会撞 `:242`/`:244`）。
**写注释说清口径**：「不在清单」的正确理由是**退役、不在 public**，不是「没源」——两义混在一个元组里，将来退役一个有源角色会同时撞两条不变量。

## 写死的名单（归 task-32 改现读，不许换成新的写死值）

- `test_summary_prompt_source.py:248`：`tuple(sorted(ps.NO_SOURCE)) == ("renhua","shensi","xueshu")`
- `:238`：`wb_only_bound.get("shipin") == "视频总结.txt"`

## 两条待用户拍（不许替用户决定）

1. 合并后 `renhua` 走**原文 + 三轮**（system 511 字符 + `OUTPUT_CONTRACT` 450 = 请求里 962，`core/summary/summarizer.py:518` 现读）。要不要让 `renhua` 保留内置压缩版（1509）当 system、原文只作参考？
2. 真库里 `standard` 有 1 条历史行；`xueshu`/`shipin` **没有任何真行** ⇒ 「（已停用）」显示只能用 Temp 夹具造行验证，交付里不许写成「真库已验」。

## 顺手记一条产品债（parity 用 `xfail(strict=True)` 常驻钉住）

> **本段已过期，销账如下（2026-11-05 现读）。** 下面提到的那条 `xfail(strict=True)` **早就不存在了**——`grep -rn "pytest.mark.xfail" tests/` = 0 命中。这条债在 task-34 修好、task-48/49 补完判据，现在由两条**常驻用例**守着，不是记债标记：`tests/wb/test_config_shared.py:147 test_legacy_yaml_is_read_but_never_rewritten` 与 `tests/wb/test_config_migration_notice.py:488 test_the_legacy_files_are_read_but_never_written`。这段留着只因为下面「写点已更正」那节的取证过程仍然有用（复现必须用含 foreign 键的夹具那一条）。

只读路由 `roles` / `--roles` / `info` 在服务装配阶段就把用户配置整份重写。已钉的是「真实两家零写入 + 第二次运行幂等」；真正修好后那条 xfail 会 XPASS 变红，提醒删掉它。

**写点已更正（vendor 用 `trace_save2.py` 调用栈取证，本节原写 `app/service.py:987/:1008/:1014` 是错的）**：真实链路是
`cli.py:677 main → cli.py:113 open → app/service.py:662 __init__ → video_workbench/config.py:874 load → config.py:947 save → write_ext`，
即 **`config.py:874` 那条 `elif foreign and target != LEGACY_CONFIG_PATH:` 自愈分支**（task-22 加的）。夹具须含 foreign 键（`storage.cache_root` 即可触发）；真实两家 `foreign_keys()` 都是 `[]`，所以拿真实配置当夹具复现不出来 —— 这点曾让 lead 误判「缺陷不存在」。修法口径：`load()` 里除「搬家落新家」与「显式保存」外不写盘，自愈只报告、不重写用户文件（task-34）。

## 补充决策 2（用户 2026-11-04 20:32 拍）

**`shensi（超强思考模式总结）不补写 txt，保持内置压缩版。** 它是退役合并后**唯一的无源角色**（`NO_SOURCE = ("shensi",)`）。

- 理由：无源不是缺陷，是这套设计的另一半——`prompt_source.MAP` 只管"哪些角色吃 txt 原文"，没进 MAP 的角色走内置压缩版 + 同一份 `OUTPUT_CONTRACT`，行为完全确定、有 `tests/wb/test_summary_prompt_source.py` P3 钉着（"无源角色的 system 必须等于 bs 内置压缩版"）。`shensi` 的内置版 2593 字符本来就比多数 txt 原文更厚，没有"补一篇才算完整"的必要。
- 代价（已认）：它的提示词**不受真源文件保护**——改它要改代码（`roles.py` 是逐字节锁定的副本，唯一合法差异点是 `core/summary/__init__.py`），不能像那六个角色一样改 txt 就生效。将来若要给它接 txt，必须同时动 `MAP`、`NO_SOURCE`（现读推导）与 `AGENTS.md` 第二节的口径。
- 待定：无。
