"""config.py 的验收：首启可用、坏值早报、保存可回读。

对应任务清单 P0-02。这里刻意只测"出错的那一类"——枚举拼错、未知键、坏 YAML、
代理漏协议。它们的共同点是：不测的话不会当场炸，而是延后到跑任务时才炸，
而且现场信息离真原因很远。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from video_workbench.app.errors import WBError
from video_workbench.config import SKIN_KEYS, WorkbenchConfig


def _write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


# --------------------------------------------------------------- 首启


def test_missing_file_falls_back_to_defaults(tmp_path: Path):
    cfg = WorkbenchConfig.load(tmp_path / "nope.yaml")
    assert cfg.ui.skin in SKIN_KEYS
    # 默认库沿用 bili-scribe 的库（方案书要求两个应用共用一份数据），
    # 但实例化时必须给出**绝对**路径，不能让 cwd 决定数据落哪儿。
    assert cfg.db_path.lower().endswith("bili-scribe.db")
    # source_path 仍然要记住"本来想读哪个文件"：save() 靠它决定写哪儿
    assert str(cfg.source_path).endswith("nope.yaml")


def test_defaults_are_valid_and_absolute(tmp_path: Path):
    cfg = WorkbenchConfig.load(tmp_path / "nope.yaml")
    for value in (cfg.db_path, cfg.media_root, cfg.cache_root, cfg.models_root):
        assert Path(value).is_absolute(), value


def test_yaml_roundtrip(cfg_path: Path):
    cfg = WorkbenchConfig.load(cfg_path)
    cfg.download.concurrency = 3
    cfg.ui.skin = "exhibition"
    first = cfg.save()
    assert first["bytes"] > 0
    # 夹具里 config.yaml 本来就在，所以第一保存也会留 .bak —— 覆盖前必须能回退
    assert first["backup"] and Path(first["backup"]).exists()

    again = WorkbenchConfig.load(cfg_path)
    assert again.download.concurrency == 3
    assert again.ui.skin == "exhibition"

    second = cfg.save()
    assert second["backup"] and Path(second["backup"]).exists()


# --------------------------------------------------------------- 坏值


def test_bad_yaml_reports_clearly(tmp_path: Path):
    path = _write(
        tmp_path / "bad.yaml",
        """
        app:
          - 这不是映射
        ui: {
    """,
    )
    with pytest.raises(WBError) as info:
        WorkbenchConfig.load(path)
    assert info.value.code == "VALIDATION"
    assert "解析失败" in info.value.message


def test_unknown_key_is_rejected_not_ignored(cfg_path: Path):
    """未知键必须报错。静默忽略会让用户以为改生效了。"""
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["download"] = {"max_frame": 40}  # 少一个 s
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(WBError) as info:
        WorkbenchConfig.load(cfg_path)
    assert info.value.code == "VALIDATION"
    blob = f"{info.value.message} {info.value.detail or ''}"
    assert "max_frame" in blob


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"ui": {"skin": "nope"}}, "ui.skin"),
        ({"llm": {"proxy": "127.0.0.1:7897"}}, "llm.proxy"),
        ({"download": {"concurrency": 99}}, "download.concurrency"),
        (
            {"clip": {"highlight_min_seconds": 90, "highlight_max_seconds": 20}},
            "highlight_min_seconds",
        ),
    ],
)
def test_bad_values_rejected(cfg_path: Path, patch: dict, needle: str):
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    for section, values in patch.items():
        raw.setdefault(section, {}).update(values)
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    with pytest.raises(WBError) as info:
        WorkbenchConfig.load(cfg_path)
    assert info.value.code == "VALIDATION"
    assert needle in f"{info.value.message} {info.value.detail or ''}"


# --------------------------------------------------------------- 提交


def test_apply_is_all_or_nothing(cfg_path: Path):
    """一段坏值不能让别的段"改了一半"。界面最怕这种半成品状态。"""
    cfg = WorkbenchConfig.load(cfg_path)
    before_skin = cfg.ui.skin
    before_conc = cfg.download.concurrency

    with pytest.raises(WBError):
        cfg.apply({"download": {"concurrency": 2}, "ui": {"skin": "没有这个皮肤"}})

    assert cfg.ui.skin == before_skin
    assert cfg.download.concurrency == before_conc


def test_apply_marks_server_for_restart(cfg_path: Path):
    cfg = WorkbenchConfig.load(cfg_path)
    restart = cfg.apply({"server": {"port": 8811}})
    assert "server" in restart
    assert cfg.server.port == 8811


def test_views_hide_api_key_but_settings_show_it(cfg_path: Path):
    cfg = WorkbenchConfig.load(cfg_path)
    cfg.llm.api_key = "sk-secret"
    assert "api_key" not in cfg.public_view()["llm"]
    assert cfg.public_view()["llm"]["configured"] is True
    assert cfg.settings_view()["llm"]["api_key"] == "sk-secret"
