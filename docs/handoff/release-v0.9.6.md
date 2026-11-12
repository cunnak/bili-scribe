# v0.9.6 / VideoWorkbench 0.2.5（打包版）

## 这个版本解决什么问题

B 站笔记发布返回 `[79501] 最多输入20000字哦` 时，真正撞到的不是正文字符数，而是
`x/note/add` 的 **form-urlencoded 请求体字节数**：中文按 URL 编码后约 9 字节/字，逐行
Delta 还会把格式开销一起放大。旧版把长文原样提交，正文看似只有几千字，表单却可能超过
20,000 字节。

## 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `video_workbench/core/publisher.py` | 逐行 Delta 原样编码；发布前不量表单体 | `compact_delta()` 合并相邻同属性文本操作；新增 `NOTE_BODY_MAX_BYTES = 19_800` 与 `form_body_bytes()`，超限在发请求前拦截 |
| `video_workbench/core/publisher.py` | 不发送 `cont_len`，摘要上限 200 字 | 发送 `cont_len = len(plain_text(content))`，摘要上限改为 80 字（对齐真实客户端） |
| `video_workbench/core/publisher.py` | `79501` 只显示平台原文 | 错误提示直接说明「约 20000 字节表单体上限，删减正文再发，重试不会变好」 |
| `video_workbench/core/publisher.py` | 内容门禁只报正文字数 | 门禁同时报正文字数 / 表单编码字节数，超限时阻断并显示安全线 |
| `tests/wb/test_publish_do.py`、`tests/wb/test_publish_gates.py` | 没有字节预算与压缩用例 | 新增 Delta 合并、表单编码字节、超限预检、`cont_len`、2000 字多行正文等回归 |

## 验证

- 发布链路回归：`.venv/Scripts/python.exe -m pytest tests/wb/test_publish_do.py tests/wb/test_publish_gates.py tests/wb/test_publish_draft.py tests/wb/test_service_publish.py -q -p no:randomly` → **94 passed in 28.72s**
- 版本 owner：`.venv/Scripts/python.exe -m pytest tests/wb/test_version_owner.py -q -p no:randomly` → **6 passed in 0.93s**
- 冻结包：`.venv/Scripts/python.exe scripts/accept_frozen_package.py` → **16/16 PASS**；包内 `video_workbench.core.publisher` 的 code object 含 `compact_delta` / `form_body_bytes` / `NOTE_BODY_MAX_BYTES`
- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → PyInstaller 退出码 0，耗时 92.6s，`--check` 报版本 `0.2.5`
- 发布资产：`dist/VideoWorkbench-0.2.5.zip`，1989 个 ZIP 条目（1959 个文件），174,048,629 B，`testzip` 无坏块

## 验证到什么程度

- 真实 B 站发帖没有实际打网络：本地用真实数据库做了 payload 预算验证（17,622B / 16,286B / 18,367B 可通过，66,804B 被门禁拦截），但真实登录态下的 `x/note/add` 未调用。
- 打包版验收只证明冻结包包含本次构建与模块符号；真实账号发布仍需人工用一条长笔记复验。

## 发布执行记录

- 构建：`.venv/Scripts/python.exe scripts/build_wb.py` → PyInstaller 退出码 0，`--check` 报版本 `0.2.5`。
- tag：`v0.9.6`（指向本版提交）。
- release：`gh release create v0.9.6 "dist/VideoWorkbench-0.2.5.zip" --title "VideoWorkbench 0.2.5（打包版）"`。
