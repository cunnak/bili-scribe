# HANDOFF — bili-scribe

## 交付范围

- 仓库只保留 BiliScribe 代码、测试、文档与 v0.7.3 及更早 release。
- 旧产品目录及其构建产物已从远端索引移除；本地副本不提交。

## 本机运行

- Python：`.venv/Scripts/python.exe`
- 启动：`python run.py`（需先配好 `config.yaml`，模板见 `config.example.yaml`）
- 烟测：`.venv/Scripts/python.exe -m pytest tests/smoke_test.py -q`
- 前端：`node tests/frontend_test.js`

## 发布

- 代理：`HTTP_PROXY=http://127.0.0.1:7897`
- tag/release 只在 GitHub 上维护；删除其他产品线版本后只留 BiliScribe 版本线。

## 交接注意

- 不要 `git add -A`；提交必须带显式 pathspec。
- 截图 `docs/screenshots/**` 是二进制基准，禁止覆盖。
