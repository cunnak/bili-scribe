## 这个版本解决什么问题

**总结以前看不见画面，现在看得见；而失败时那句报错不再把人往错的方向支。**

三件事：

1. 「设置 → 采集 → 截帧」这个开关在 v0.9.2 及以前是**假的**。开关注册在配置文件里（`collect.frames.enabled`），但 WB 的总结链路一个调用点都没有——关掉它什么都不省，界面上却写着「关掉可明显提速」。这一版把它真接上了：每次生成与修订都从**已下载到本地的源视频**等距抽一组帧，附给模型。
2. 抽帧接上之后，坏 JSON 的报错一开始写得像「是帧把输出带偏了，关掉截帧能躲过」。同形对照实测**无帧也坏**，锅在端点不遵守 `response_format=json_object` 的转义。这一版把那句文案改对了，行为一字不动（不偏离 bili-scribe）。
3. `1100×720` 这个窗口下限第一次被真窗口量到（此前没有任何判据、也没人验过 WebView2 会不会遵守）。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/core/frames.py`（新） | 没有本地抽帧腿 | 按 `duration_ms` 等距算偏移、逐张 `ffmpeg -ss … -vf scale=480:-2`，复用 bili-scribe 的 `Frame/FrameSet/FrameError/as_content_blocks` 数据形状；临时目录 `frames/<独占子目录>`，抽完只删自己那一个（同目录还住着封面 `thumb.jpg`） |
| `video_workbench/core/summarizer.py` | `run()`/`refine()` 不接帧 | 各加可选 `frameset`，只在**归约那一发**附上；宽松档的补救腿先 `_text_only` 降回纯文本（一轮三轮，12 帧白烧四遍） |
| `video_workbench/app/service.py` | 0 个调用点 | `_summary_frameset()` 每次生成与修订现抽一组；三条降级（开关关掉 / 没有源视频 / 抽帧失败）各自留一行日志后返回 None，**总结照跑，不整条失败** |
| 坏 JSON 的报错文案 | 「…不抢救坏 JSON；需要抢救请设 WB_SUMMARY_PARITY_STRICT=0」 | 中间插一句「这与画面帧无关：无帧同形对照实测同样失败，关掉「设置 → 采集 → 截帧」不解决。」；开关那句仍留在末尾——用户最后看到的必须是下一步动手的事 |
| `web/js/pages/settings.js` 三句文案 | 「给总结配图用；关掉可明显提速。」「省流量就把截帧关掉。」「帧会作为图片发给模型，越多越贵」 | 换成实测站得住的一句，方向也纠正（帧从本地文件抽，不走网络，不存在省流量）；相邻两行重复讲同一件事的删掉 |
| `scripts/sync_mirror.py` | 主仓删掉的文件不传播到发布镜像；`.gitignore` 被改脏照样同步；同步期间主仓前进也照提交 | 删除按窄面（`video_workbench/`、`tests/wb/`）传播；先验后写；主仓前进则拒绝提交（重跑即恢复） |
| `scripts/accept_window_min_size.py`（新） | 窗口下限只活在临时探针里 | 常驻验收：起产品自己的 `create_window`，只认自己新建的那扇窗，四条判据按 DPI 折算 |
| `scripts/accept_frozen_package.py`（新） | 打包验收只能人工 | 九条机械判据一条命令跑完，读数落临时区 |

## 验证

- 常驻判据：`tests/wb/test_summary_frames.py` 13 条（采集腿 5 / 适配层 3 / 服务层 4 / 界面 1）、`tests/wb/test_summary_bad_json_copy.py` 5 条（含「有帧/无帧」孪生对照）、`tests/wb/test_window_min_size.py` 2 条（断「传下去的就是模块那个元组」，不钉 1100/720 字面量）、`tests/wb/test_settings_frames_help.py` 4 条（两条源码级 + 两条真 chromium）。
- 真模型一发（预算账目）：`calls=2`。带帧一发 prompt **12967** / completion **2708** / 52.4s；无帧同形对照 prompt **5149** / completion **1534** / 61.8s。差值 7818 prompt token ÷ 12 帧 ≈ **651 token/帧**——这是本机这个端点按 base64 长度计费的口径；bili-scribe 文档里那个 122 image_tokens/帧是 magpie 端点的另一把尺，两个数不能混着报。
- 画面真的供了料：源视频 58 分钟而字幕只覆盖前 10:47，六个幻灯片标题在「字幕正文 / 无帧基线输出 / 带帧输出」三处的命中是 **False / False / True ×6**，另有四张帧图人工开图亲验（480×270）。带帧那次的 `caution` 里模型自己写了「实际视频时长信息存在矛盾（元数据称10:47但帧时间戳到53:42）」——这条信息纯文字路径拿不到。
- 坏 JSON 的可抢救性：同一份 raw 用 `video_workbench/infra/jsonfix.parse_json` 零成本还原出 `one_line / summary_md / key_points(5) / timeline(14) / caution / comment_short` 六个键。失败是策略（默认档不抢救），不是数据坏了。
- 窗口下限（100% 缩放）：`scripts/accept_window_min_size.py` 四条全 PASS（`min_track == MIN_SIZE`、往小拽有闸、往大放行）。
- 冻结包：`scripts/accept_frozen_package.py` **9/9 PASS**——exe 在、六个提示词字节一致、包内 settings.js 是改了文案的那一代、`video_workbench.core.frames` 在 PYZ toc 里、`--check`/`--roles` 退出码 0、包内自显示版本 == 仓库版本、三份真实配置验收前后一字未动。
- 同步入口常驻哨兵：`scripts/sync_mirror_sentinel.py` 40 腿 `ok=40 red=0`（拿上一版实现做 A/B 会红在那条自证腿上，说明新腿有牙）。
- 全量回归：`.venv/Scripts/python.exe -m pytest tests/wb -q` → **1722 passed, 1 skipped in 794.66s (0:13:14)**，退出码 0（上一版基线 1717，这一版多 5 条坏 JSON 判据）。

## 验证到什么程度（未测/未做的，说清）

- **125%/150% 缩放下窗口下限仍未验**：本机改成 150% 后探针读回还是 `dpi=96`（缩放没落到这个进程）。脚本已按 DPI 折算，改完缩放重跑同一条命令即可自证。
- **390 宽手机视口不是支持范围**：实测那一档 `label.field__label` 解析得到 6 个、一个都不可见；产品只有一条 `@media (max-width: 1200px)`。「要不要做手机宽度」是待决产品问题，本版本不动布局。
- **带帧那次是单发**，只走完归约一发，三轮批判/修订没跑（那是另外三次调用）。坏 JSON 的结论由孪生对照与 5 条常驻判据站住，不是靠那一次。
- **端点转义这件事我们治不了**，只把文案改对。还欠一个「设置页里能点的抢救开关」——今天抢救 JSON 要用户自己设环境变量，那不是产品该有的样子。
- 服务端抽帧接口（`aid` 那条腿）没做：`wb_project.aid` 现有 32 行全是空的，而 bili-scribe 那个下载器跟重定向、不设 host 白名单、不限字节，WB 不继承它。
- 帧没有磁盘缓存（采完即删，只在内存里发一次），也没有按「截帧保留天数」清理这一步。

## 兼容性与装机

- 只影响 Windows 桌面端。行为变化：默认开着截帧，素材有源视频时**每次生成多附 12 张图**（≈651 token/帧，本机端点口径）。不想花这个就用「设置 → 采集 → 截帧」关掉——这一版开始，关掉真的会关掉。
- 打包版资产 `VideoWorkbench-0.2.2.zip` 解压后直接是 `VideoWorkbench/`（上一版外面多包了一层 `dist/`），双击 `VideoWorkbench.exe` 即可，无需安装。
- 升级不动数据：配置仍在 `~/.video-workbench/`，媒体与库的位置都没变。

