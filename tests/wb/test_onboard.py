"""P5-05 首次启动向导：体检五项 + 汇总 + 记录"看过了"。

这一组测试的重点不是"注册表读得到吗"（那取决于机器），而是三条**决策**：

1. 状态是三态（ok / todo / missing），不是两态——"模型没下"（能一键解决）
   和"FFmpeg 没装"（得用户自己装）必须能分辨。
2. `ready` 只在这几件都齐时为真；没登录、没配 key 不阻塞（还能本地导入 +
   规则兜底），否则用户会被挡在门外。
3. 跳过也要落盘：`ui.onboarded` 写过一次就不再弹。

体检函数依赖机器环境（WebView2 装没装、PATH 里有没有 ffmpeg），所以每个用例都
把不关心的几项替身掉，只留被测项走真逻辑。
"""

from __future__ import annotations

import pytest

from video_workbench.config import read_ext, write_ext
from video_workbench.core import onboard


class FakeCfg:
    """够 `report()` 用的最小配置：只带它真读的字段。"""

    def __init__(
        self, *, api_key="", model="deepseek-chat", ffmpeg_dir="", models_root=None, onboarded=False
    ):
        self.llm = type("Llm", (), {"api_key": api_key, "model": model})()
        self.storage = type("Storage", (), {"models_root": str(models_root or "")})()
        self.download = type("Dl", (), {"ffmpeg_dir": ffmpeg_dir})()
        self.ui = type("Ui", (), {"onboarded": onboarded})()

    # app.settings.put / save 这类不在这里测，但 report 之后 service 会用到 apply
    def apply(self, values):
        if values.get("ui"):
            for key, value in values["ui"].items():
                setattr(self.ui, key, value)

    def save(self):
        return ""


class FakeGateway:
    def __init__(self, *, logged_in=False, who=""):
        self._logged_in = logged_in
        self._who = who

    def is_logged_in(self):
        return self._logged_in

    def identity(self):
        return {"name": self._who} if self._who else {}


@pytest.fixture
def clean_env(monkeypatch):
    """把"跟机器有关"的三项钉死，避免开发机装了 ffmpeg 就红。"""
    monkeypatch.setattr(onboard, "webview2_version", lambda: "120.0.2210.144")
    monkeypatch.setattr(onboard, "ffmpeg_path", lambda cfg: r"C:\ffmpeg\bin\ffmpeg.exe")
    monkeypatch.setattr(
        onboard,
        "model_state",
        lambda cfg: {"name": "large-v3-turbo", "downloaded": True, "bytes": 1717986918},
    )
    monkeypatch.setattr(onboard, "cached_models", lambda cfg: [], raising=False)


def _ids(report):
    return [step["id"] for step in report["steps"]]


def test_report_has_five_steps_in_fixed_order(clean_env):
    report = onboard.report(
        cfg=FakeCfg(api_key="sk-x"), gateway=FakeGateway(logged_in=True, who="小村")
    )
    assert _ids(report) == ["webview2", "ffmpeg", "model", "bili", "llm"]
    assert report["total"] == 5


def test_all_ready_when_everything_present(clean_env):
    report = onboard.report(
        cfg=FakeCfg(api_key="sk-x"), gateway=FakeGateway(logged_in=True, who="小村")
    )
    assert report["ok_count"] == 5
    assert report["ready"] is True
    assert report["blocking"] == []
    assert report["missing"] == []
    # 每项都带 detail，界面直接渲染；none of them 空着
    assert all(step["detail"] for step in report["steps"])


def test_model_missing_is_todo_with_download_action(clean_env, monkeypatch):
    """模型没下 = todo（能一键解决），且 action 指向真实的下载 RPC。"""
    monkeypatch.setattr(
        onboard,
        "model_state",
        lambda cfg: {"name": "large-v3-turbo", "downloaded": False, "bytes": 0},
    )
    report = onboard.report(cfg=FakeCfg(api_key="sk-x"), gateway=FakeGateway(logged_in=True))
    step = {s["id"]: s for s in report["steps"]}["model"]
    assert step["status"] == "todo"
    assert step["action"]["kind"] == "rpc"
    assert step["action"]["method"] == "transcribe.model.download"
    assert step["action"]["params"]["name"] == "large-v3-turbo"
    # 缺模型就干不了活：算阻塞
    assert "model" in report["blocking"]
    assert report["ready"] is False


def test_ffmpeg_missing_is_blocking_missing_with_url(clean_env, monkeypatch):
    monkeypatch.setattr(onboard, "ffmpeg_path", lambda cfg: "")
    report = onboard.report(cfg=FakeCfg(api_key="sk-x"), gateway=FakeGateway(logged_in=True))
    step = {s["id"]: s for s in report["steps"]}["ffmpeg"]
    assert step["status"] == "missing"
    assert step["action"]["kind"] == "url"
    assert "ffmpeg" in step["action"]["url"]
    assert "ffmpeg" in report["blocking"]
    assert report["ready"] is False


def test_no_login_does_not_block(clean_env):
    """没登录不该挡人：本地文件照样能导入。"""
    report = onboard.report(cfg=FakeCfg(api_key="sk-x"), gateway=FakeGateway(logged_in=False))
    step = {s["id"]: s for s in report["steps"]}["bili"]
    assert step["status"] == "missing"
    assert "bili" not in report["blocking"]
    assert report["ready"] is True


def test_no_llm_key_does_not_block_and_points_to_settings(clean_env):
    report = onboard.report(cfg=FakeCfg(api_key=""), gateway=FakeGateway(logged_in=True))
    step = {s["id"]: s for s in report["steps"]}["llm"]
    assert step["status"] == "todo"
    assert step["action"]["kind"] == "route"
    assert step["action"]["route"] == "settings"
    assert step["action"]["params"]["section"] == "llm"
    assert report["ready"] is True


def test_missing_webview2_blocks_and_offers_url(clean_env, monkeypatch):
    monkeypatch.setattr(onboard, "webview2_version", lambda: "")
    report = onboard.report(cfg=FakeCfg(api_key="sk-x"), gateway=FakeGateway(logged_in=True))
    step = {s["id"]: s for s in report["steps"]}["webview2"]
    assert step["status"] == "missing"
    assert step["action"]["kind"] == "url"
    assert "webview2" in report["blocking"]


def test_platform_line_mentions_source_or_frozen(clean_env):
    report = onboard.report(cfg=FakeCfg(), gateway=FakeGateway(), command_line="VideoWorkbench.exe")
    assert report["command_line"] == "VideoWorkbench.exe"
    assert "python" in report["platform"]


def test_summary_counts_match_statuses(clean_env, monkeypatch):
    """ok_count / missing 必须是算出来的，不是写死的。"""
    monkeypatch.setattr(onboard, "ffmpeg_path", lambda cfg: "")
    report = onboard.report(cfg=FakeCfg(api_key=""), gateway=FakeGateway(logged_in=False))
    statuses = [s["status"] for s in report["steps"]]
    assert report["ok_count"] == statuses.count("ok")
    assert report["missing"] == [s["id"] for s in report["steps"] if s["status"] == "missing"]


# ------------------------------------------------------------------ 服务层


def test_service_onboard_and_done_persist(tmp_path):
    """`app.onboard.done` 必须真写进配置——否则每次启动都弹，等于没记。"""
    from video_workbench.app.service import WorkbenchService

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "storage:\n"
        f"  media_root: '{tmp_path.as_posix()}/media'\n"
        f"  cache_root: '{tmp_path.as_posix()}/cache'\n"
        f"  models_root: '{tmp_path.as_posix()}/models'\n"
        f"  db_path: '{tmp_path.as_posix()}/wb.db'\n",
        encoding="utf-8",
    )
    service = WorkbenchService(config_path=str(cfg_path))
    try:
        assert service.cfg.ui.onboarded is False
        report = service.app_onboard()
        assert report["total"] == 5
        assert report["done"] is False

        service.app_onboard_done(skipped=True)
    finally:
        service.close()

    # 扩展键不进共享 yaml（bili-scribe 遇到不认识的键会报错），落 sidecar
    saved = cfg_path.read_text(encoding="utf-8")
    assert "onboarded" not in saved, "ui.onboarded 不该写进共享的 config.yaml"
    assert read_ext(cfg_path)["ui"]["onboarded"] is True

    service2 = WorkbenchService(config_path=str(cfg_path))
    try:
        again = service2.app_onboard()
        assert again["done"] is True, "重启后没记住「看过了」"
    finally:
        service2.close()


def test_service_onboard_is_read_only(tmp_path):
    """体检只读：跑一次不该改配置、不该建库以外的文件。"""
    from video_workbench.app.service import WorkbenchService

    cfg_path = tmp_path / "config.yaml"
    # 干净的共享文件（只有 bili-scribe 认识的键）+ 扩展配置在 sidecar：
    # 这样体检跑完不该有任何重写动作。
    cfg_path.write_text(
        "server:\n"
        "  host: '127.0.0.1'\n"
        "  port: 8765\n"
        "storage:\n"
        f"  db_path: '{tmp_path.as_posix()}/wb.db'\n",
        encoding="utf-8",
    )
    write_ext(
        cfg_path,
        {
            "storage": {
                "db_path": f"{tmp_path.as_posix()}/wb.db",
                "media_root": f"{tmp_path.as_posix()}/media",
                "cache_root": f"{tmp_path.as_posix()}/cache",
                "models_root": f"{tmp_path.as_posix()}/models",
            }
        },
    )
    before = cfg_path.read_text(encoding="utf-8")
    service = WorkbenchService(config_path=str(cfg_path))
    try:
        service.app_onboard()
        service.app_onboard()
    finally:
        service.close()
    assert cfg_path.read_text(encoding="utf-8") == before, "体检改动了配置文件"
    assert cfg_path.read_text(encoding="utf-8").count("onboarded") == 0
