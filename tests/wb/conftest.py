"""P0 测试夹具。

两条铁律，写在这里免得后面忘了：

1. **不碰用户的真目录**。每个测试用 ``tmp_path`` 造一份 config，把 storage 全部
   指到临时目录里。跑测试绝不能在 ``%APPDATA%`` 下留垃圾——那是最难擦的那种。
2. **不依赖网络/ffmpeg/模型**。P0 的验收面是"界面能起、配置能存、任务状态机
   能转"，这些都该在没有 GPU、没有 ffmpeg 的机器上跑绿。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    """写一份最小可用的 config.yaml，路径全部落在 tmp_path 内。"""
    data = {
        "ui": {"skin": "ark"},
        "storage": {
            "db_path": str(tmp_path / "data" / "wb.sqlite"),
            "key_path": str(tmp_path / "data" / "keys.json"),
            "media_root": str(tmp_path / "media"),
            "cache_root": str(tmp_path / "cache"),
            "models_root": str(tmp_path / "models"),
            "ffmpeg_dir": str(tmp_path / "tools"),
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for sub in ("data", "media", "cache", "models", "tools"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def service(cfg_path: Path):
    """起一个真后端（无窗口），用例结束关掉事件线程与数据库连接。"""
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=str(cfg_path), picker=None)
    try:
        yield svc
    finally:
        svc.close()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """清掉可能污染配置的环境变量，避免本机 shell 里的值影响断言。"""
    for name in ("WB_LLM_API_KEY", "VIDEO_WORKBENCH_CONFIG", "APPDATA_WB"):
        monkeypatch.delenv(name, raising=False)
    yield


def has_pywebview() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


requires_webview = pytest.mark.skipif(not has_pywebview(), reason="本机没装 pywebview（桌面外壳）")
windows_only = pytest.mark.skipif(os.name != "nt", reason="路径行为只在 Windows 上成立")
