"""VideoWorkbench —— 录播与视频切片工作台。

与 bili-scribe 的边界（方案书 0.1 / 6.2）：

- ``bili_scribe/`` 是上游库，本包只 import，不改它的业务代码。
- 桌面主链路**不起 HTTP**：靠 pywebview 的 ``js_api`` 进程内调用，
  后台线程用 ``evaluate_js`` 推事件。FastAPI 只留在 ``run.py`` 调试模式。
- 数据落在 ``~/.bili-scribe/bili-scribe.db`` 里新增的 ``wb_*`` 表，
  不修改 bili-scribe 自己的表结构、不替它跑迁移。
"""

from __future__ import annotations

__version__ = "0.2.11"
APP_NAME = "VideoWorkbench"

__all__ = ["__version__", "APP_NAME"]
