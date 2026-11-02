# 0001 · 源视频文件按标题命名，侧车名与下载暂存名保持固定

- 状态：已采纳
- 日期：2026-11-02
- 相关：方案书 §5.3.3、§7.2；`core/layout.py`、`core/downloader.py`、`workers/download_worker.py`、`infra/repo.py`

## 背景

用户诉求（原话）：

> bug修复：videoworkbench下载的视频源文件不要命名为"source"，将其命名为视频的原标题。

一个项目目录里躺着一个 `source.mp4`，下十几集之后在资源管理器里完全分不出谁是谁。
但"改个文件名"在这套代码里不是字符串替换：文件名同时被**资产身份键、复用判定、
产物探测**三处当作依据，直接改会静默丢数据。

## 决定：三个名字，三种职责，故意不对称

| 名字 | 取值 | 归谁管 | 能不能变 |
|---|---|---|---|
| **媒体文件名** | `{视频标题}.mp4`（多P 为 `主标题 - 分P标题.mp4`） | `layout.source_stem` + `layout.finalize_source` | 跟内容走，UP 改标题它就变 |
| **侧车文件名** | `source.wb.json` | `layout.SIDECAR` | **固定**，动了旧目录判不出是同一集 |
| **下载暂存名** | `source.<ext>` | `downloader` 的 `-o` | **固定**，断点续传的 `.part` 靠它稳定 |

改名只发生在**一个地方**：下载进程结束后，`finalize_source` 把暂存文件改成标题名。
读侧只认**数据库里记的路径**，兜底收口在 `find_source` 一个函数里。

## 为什么不能顺手把另外两个也改了

**侧车名不能动。** `allocate()` 要在**还不知道媒体文件叫什么**的时候判断
"这个目录是不是上次下的那一集"，依据是侧车里的 `bvid + cid`（`_same_item`）。
侧车名一旦跟着标题走，标题一变就找不到侧车 → 判定"不是同一集" → 新建重复项目目录，
同一条视频在盘上裂成两份。这个后果比"命名不统一"严重得多。

**暂存名不能动。** `-o "%(title)s.%(ext)s"` 有两个坑：

1. `.part` 续传文件名含标题，UP 改标题后续传直接失效，下到一半的重来。
2. yt-dlp 自己的清洗规则（只替换 `<>:"/\|?*`）和我们的 `sanitize_name`
   （还剥首尾点、处理 Win32 保留字、压空白、截断）**不一致** —— 同一个视频会出现两个名字，
   于是"改完名去库里找文件"这件事永远对不齐。而且 `unique_path` 的 ` (2)` 消歧
   在输出模板里根本表达不出来。

还有一条更隐蔽的：把标题当**模板片段**拼进 `-o`（`f"{title}.%(ext)s"`）会让标题里的
`%(id)s` 被 yt-dlp 真的展开——那是输出模板注入。标题只能当**值**传，而我们连当值都不传。

**`Layout` 不加 `title` 字段。** `Layout` 是 frozen、只带 `root` 的 dataclass。
加了可选的 title，`Layout.source` 的语义就从"文件叫什么"退化成"我期望它叫什么"，
任何只拿到 `root` 的重构路径都会静默回落到 `source.mp4`。所以我们**删掉**了
`Layout.source` 属性，而不是给它加参数：宁可不给一个会骗人的便利属性。
（`as_dict()`/`rel()` 全仓零调用者，一并处理。）

## 改名会引爆的那颗雷：资产身份键

`repo.asset_upsert` 的去重键是 `(project_id, kind, path)` —— **含 path**。
文件名一改，重下同集就会插出**第二行** `kind="source"`；而读取侧
`service.py` 的 `by_kind.setdefault(kind, asset)` 配上 `asset_list` 的
`ORDER BY created_at`（升序）意味着**最早那行胜出**：旧路径遮蔽新文件。
库里没有 `UNIQUE` 约束，也全仓没有任何删除资产行的代码。

后果不是"名字不好看"，是：素材库那行标红"文件不在了"、转录/剪辑/导出拿到一个
不存在的文件、`_clip_export_job` 抛 `NOT_FOUND`，而且**都不报错地发生在用户点导出之后**。

所以配套改成：

- 新增 `repo.asset_upsert_singleton(project_id, kind, path, **fields)`，身份键
  `(project_id, kind)` **不含 path**，找到行就**改写**（不删行——
  `wb_transcript.asset_id` 是 `ON DELETE SET NULL` 外键，删行会把转录的来源抹成 NULL）。
- 只用于**单例 kind**（`source`、`thumb`）。`subtitle` / `export` 是多值，继续走
  `asset_upsert`，一刀切会把多条字幕合成一条。
- `download_worker._existing_is_same` 不再自己拼 `source.mp4`，改走 `find_source`。
  它原来那份手拼**在改动之前就已经**对本地导入的项目失效了（导入写的 path
  从来不是 `source.mp4`），表现为"重下同集静默整集重下且不报错"。
- `transcriber._source_video` 从"先猜 `root/source.mp4` 再查库"倒过来：**DB 优先**。

## 兼容边界：读侧兼容，写侧不迁移

历史目录里叫 `source.mp4` 的文件**不改名、不搬动**。`find_source` 的查找顺序是：
调用方给的权威路径（DB/侧车）→ 标题名 → 遗留的 `source.*` → 目录里**唯一**那个视频文件。
两个以上候选且都对不上就返回 `None` 让上层重下。

这里刻意**不**保留"挑最大的文件"或"挑 mtime 最新的"这类启发式：文件名跟标题走之后，
这些兜底会从"偶尔救命"变成"主路径"，有把 B 视频记到 A 项目名下且不报错的风险。
`downloader` 里原有的 mtime 挑选与 `glob("source.*")` 取最大者都因此收窄。

## 明确没做的事

- **UP 改标题后旧目录会留在盘上**（一个视频占两份）。这是改动前后都存在的遗留问题
  （目录名本来就跟着标题走），删目录是破坏性操作，单独立项。
- **标题同时流向文件名、数据库、DOM、LLM 提示词**四条路径，注入面各不相同，
  本轮只处理文件名这一条。
- 封面**不做服务端抓取落盘**。本轮只把地址协议对齐（见下），落盘缓存会引入
  出网面、缓存不淘汰、第三套 HTTP 栈等一整套新风险。

## 顺带修的封面显示（同一次报障）

页面以 `file://` 加载，CSP 是 `img-src 'self' file: data: https:`（**不含 `http:`**）；
而 B站 `view` 接口返回的 `pic` 恰恰是明文 `http://i2.hdslb.com/...`。两者一撞，
浏览器按 CSP 拦掉，界面上就是破图。实测 https 变体返回**完全相同的字节数**，
不带 Referer 也 200（只有外站 Referer 才 403）——所以根因是协议不匹配，不是防盗链。

修在 `resolver.normalize_cover`：白名单域（`hdslb.com` / `biliimg.com` / `bilibili.com`）
的 `http://` 升 `https://`，协议相对 `//` 补 `https:`，非白名单明文地址直接丢弃走占位。
**不放宽 CSP**——那等于为了显示一张缩略图给页面开一条可被中间人替换的图片来源。
前端 `newtask.js` 补 `error` 兜底，`WB.mediaUrl` 在 `encodeURI` 之后补转义 `#?&+`
（标题里带 `#` 是常态，不转义会在素材库重演同一个破图）。
