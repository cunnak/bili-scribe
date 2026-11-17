# AGENTS.md — bili-scribe 仓库作业规则

> 本仓库只交付 BiliScribe。旧产品的本地副本已从 Git 索引移除；它在本地磁盘上的副本属于用户私有文件，不提交、不清理、不推送。

## 一、环境

- Python 一律用 `.venv/Scripts/python.exe`，裸 `python` 是 WindowsApps 占位 alias。
- 中文输出先设 `PYTHONIOENCODING=utf-8`。
- git/gh 只认环境变量代理：`HTTP_PROXY=http://127.0.0.1:7897`、`HTTPS_PROXY=http://127.0.0.1:7897`。
- 中文路径计数/列表必须带 `git -c core.quotepath=false`，否则输出成八进制转义串。

## 二、代码面

- `bili_scribe/` 是唯一产品包；`run.py` 是 uvicorn + webbrowser 启动器。
- 根 `config.yaml` 是本机实配，禁止提交；模板是 `config.example.yaml`。
- `docs/screenshots/**` 是验收截图基准，二进制，禁止覆盖或重编码。
- `dist/bili-scribe-*` 与 `dist/bili-scribe.exe` 是 BiliScribe 发布产物，保留。

## 三、验证

- 后端烟测：`.venv/Scripts/python.exe -m pytest tests/smoke_test.py -q`
- 前端测试：`node tests/frontend_test.js`
- Lint：`.venv/Scripts/ruff.exe check bili_scribe tests scripts run.py`
- 格式：`.venv/Scripts/ruff.exe format --check --diff bili_scribe tests scripts run.py`
- 交差必须贴命令原文 + 通过/失败计数；“过了”三个字不算证据。

## 四、Git 与发布

- `git commit` 必须带 pathspec，且 `-m` 写在 `--` 之前。
- 提交后用 `git log --oneline -1` 现读；不要用 status 变干净当提交成功。
- 推送走代理；`remote rejected ... Internal Server Error` 是 GitHub 瞬时 500，退避重试。
- 发布只保留 BiliScribe 的 v0.7.3 及更早版本；其他产品线版本不由本仓库发布。

## 五、规则表

| 规则 | 由什么拦住 |
|---|---|
| 不提交真实配置 | `.gitignore` 的 `config.yaml*` |
| 不提交本地旧产品副本 | `.gitignore` 的 `/video_*/` 等条目 |
| 截图基准不被覆盖 | `git status` + SHA-256 现读 |
| 暂存提交不混作者改动 | `git commit -m ... -- <pathspec>` + 提交后 `git log` |
