"""设置提交的契约：点号键必须被明确拒绝，而不是静默成功。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from video_workbench.app.service import WorkbenchService  # noqa: E402


@pytest.fixture()
def service(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "storage:\n  db_path: " + str(tmp_path / "wb.db").replace("\\", "/") + "\n",
        encoding="utf-8",
    )
    svc = WorkbenchService(config_path=cfg)
    try:
        yield svc
    finally:
        svc.close()


def test_dotted_key_is_rejected(service):
    res = service.dispatch("app.settings.put", {"values": {"llm.timeout_seconds": 900}})
    assert res["ok"] is False
    assert res["error"]["code"] == "VALIDATION"
    assert "llm.timeout_seconds" in res["error"]["message"]


def test_dotted_key_nested_one_level_is_rejected(service):
    res = service.dispatch("app.settings.put", {"values": {"llm.tasks": {"summary": "a/b"}}})
    assert res["ok"] is False
    assert res["error"]["code"] == "VALIDATION"


def test_nested_key_still_works(service):
    res = service.dispatch("app.settings.put", {"values": {"llm": {"timeout_seconds": 777}}})
    assert res["ok"] is True
    assert service.cfg.llm.timeout_seconds == 777
