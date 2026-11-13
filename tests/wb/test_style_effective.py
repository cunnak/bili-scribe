# -*- coding: utf-8 -*-
"""有效样式 RPC（style.effective.*）：作用域、覆盖、恢复继承与旧写保护。

这一层的契约只有一条：**取有效样式只走一个入口**。预览、导出、烧录三处若各自
拼一遍优先级，迟早有一处顺序写反，表现为「预览对、成片不对」——那种 bug 在成片
上才发现，代价最高。所以这里把优先级、覆盖的独立性和「查看不创建覆盖」都钉成判据。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from video_workbench.core import styles

ROOT = Path(__file__).resolve().parents[2]


def make_project(service, tmp_path: Path, name: str = "标题") -> dict:
    root = tmp_path / "media" / "UP" / name
    root.mkdir(parents=True, exist_ok=True)
    return service.repo.project_create(title=name, up_name="UP", root_dir=str(root))


def rpc(service, method, params=None):
    out = service.router.dispatch(method, params or {}, "test")
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.router.dispatch(method, params or {}, "test")
    assert out.get("ok") is False, out
    return out["error"]


# ------------------------------------------------------------------ 读
def test_global_scope_returns_the_builtin_default(service):
    data = rpc(service, "style.effective.get", {})
    assert data["scope"] == "global"
    assert data["inherited"] is True
    assert data["config"]["schema_version"] == 2
    # 内置 default 的主字号就是参考画布（1080P）下的 72，不是 720 时代的 54
    assert data["config"]["primary"]["size"] == 72
    # 两个画布别混：canvas 是预览缩略图，reference 才是字号基准
    assert data["canvas"] == {"width": 1280, "height": 720}
    assert data["reference"] == {"width": 1920, "height": 1080}


def test_viewing_a_project_does_not_create_an_override(service, tmp_path):
    project = make_project(service, tmp_path)
    data = rpc(service, "style.effective.get", {"project_id": project["id"]})
    assert data["scope"] == "global"
    assert data["inherited"] is True
    # 只读不该写库：NULL 表示继承，查看一次就落下覆盖会让「我什么都没改」变成谎话
    assert service.repo.project_style_get(project["id"]) is None


def test_unknown_project_is_not_found(service):
    assert err(service, "style.effective.get", {"project_id": "pj_nope"})["code"] == "NOT_FOUND"


# ------------------------------------------------------------------ 写
def test_project_override_is_independent_from_global(service, tmp_path):
    project = make_project(service, tmp_path)
    put = rpc(
        service,
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 90}}},
    )
    # 返回的是**保存后**的作用域：在项目页第一次保存前一刻还是继承，保存完就是覆盖
    assert put["scope"] == "project"
    assert put["previous_scope"] == "global"
    assert put["config"]["primary"]["size"] == 90

    after = rpc(service, "style.effective.get", {"project_id": project["id"]})
    assert after["scope"] == "project"
    assert after["inherited"] is False
    assert after["config"]["primary"]["size"] == 90

    # 全局没被项目覆盖带跑
    assert rpc(service, "style.effective.get", {})["config"]["primary"]["size"] == 72


def test_global_save_does_not_touch_an_existing_override(service, tmp_path):
    project = make_project(service, tmp_path)
    rpc(
        service,
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 90}}},
    )
    rpc(service, "style.effective.put", {"config": {"primary": {"size": 60}}})
    assert rpc(service, "style.effective.get", {})["config"]["primary"]["size"] == 60
    assert (
        rpc(service, "style.effective.get", {"project_id": project["id"]})["config"]["primary"][
            "size"
        ]
        == 90
    )


def test_explicit_style_id_beats_every_scope(service, tmp_path):
    project = make_project(service, tmp_path)
    rpc(
        service,
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 90}}},
    )
    style, scope = service.effective_style(project_id=project["id"], style_id="default")
    assert scope == "style_id"
    assert style["primary"]["size"] == 72


def test_reset_restores_inheritance(service, tmp_path):
    project = make_project(service, tmp_path)
    rpc(
        service,
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 90}}},
    )
    reset = rpc(service, "style.effective.reset", {"project_id": project["id"]})
    assert reset["scope"] == "global"
    assert reset["config"]["primary"]["size"] == 72
    assert service.repo.project_style_get(project["id"]) is None


def test_global_reset_drops_the_working_config(service):
    rpc(service, "style.effective.put", {"config": {"primary": {"size": 60}}})
    assert service.cfg.subtitle.default_style_config
    reset = rpc(service, "style.effective.reset", {})
    assert reset["config"]["primary"]["size"] == 72
    assert not service.cfg.subtitle.default_style_config


# ------------------------------------------------------------------ 冲突与校验
def test_a_stale_revision_is_rejected(service, tmp_path):
    project = make_project(service, tmp_path)
    current = rpc(service, "style.effective.get", {"project_id": project["id"]})
    error = err(
        service,
        "style.effective.put",
        {
            "project_id": project["id"],
            "config": {"primary": {"size": 90}},
            "revision": "deadbeefdeadbeef",
        },
    )
    assert error["code"] == "CONFLICT"
    # 被拒的那一次不落盘
    assert service.repo.project_style_get(project["id"]) is None
    assert current["revision"] != "deadbeefdeadbeef"


def test_a_matching_revision_goes_through(service, tmp_path):
    project = make_project(service, tmp_path)
    current = rpc(service, "style.effective.get", {"project_id": project["id"]})
    put = rpc(
        service,
        "style.effective.put",
        {
            "project_id": project["id"],
            "config": {"primary": {"size": 90}},
            "revision": current["revision"],
        },
    )
    assert put["saved"] is True


def test_invalid_input_is_rejected_whole_and_keeps_the_previous_config(service, tmp_path):
    project = make_project(service, tmp_path)
    rpc(
        service,
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 90}}},
    )
    error = err(
        service,
        "style.effective.put",
        {"project_id": project["id"], "config": {"primary": {"size": 9999, "color": "zzz"}}},
    )
    assert error["code"] == "VALIDATION"
    # 一次列全，不是报第一个就停——用户改一个点保存又被下一句拦下是最烦的
    assert "字号" in error["message"] and "文字色" in error["message"]
    assert error["message"].count("；") == 1, error["message"]
    # 上一份有效配置原样保留
    assert (
        rpc(service, "style.effective.get", {"project_id": project["id"]})["config"]["primary"][
            "size"
        ]
        == 90
    )


# ------------------------------------------------------------------ 指纹与字体
def test_style_hash_changes_only_when_the_style_changes(service):
    base = rpc(service, "style.effective.get", {})["config"]
    first = service.style_hash(base)
    assert first == service.style_hash(styles.normalize(base))
    changed = styles.normalize({"primary": {"size": 100}}, base=base)
    assert service.style_hash(changed) != first


def test_actual_fonts_names_the_fallback_when_a_font_is_missing(service):
    conf = styles.normalize({"primary": {"font": "这个字体肯定不存在"}})
    fonts = service._actual_fonts(conf)
    assert fonts["fonts"]["primary"] == styles.FONT_FALLBACK
    assert "这个字体肯定不存在" in fonts["missing"]
    assert styles.FONT_FALLBACK in fonts["note"]


def test_actual_fonts_is_quiet_when_the_font_exists(service):
    conf = styles.normalize({})
    fonts = service._actual_fonts(conf)
    assert fonts["missing"] == []
    assert fonts["note"] == ""


# ------------------------------------------------------- 任务快照的独立性
def test_editing_the_style_afterwards_does_not_change_a_started_job(service, tmp_path):
    """已启动的任务吃的是自己那份 ASS，后续改样式不能改到它头上。

    这条守的是一个真实的坑：任务 payload 里记的 `style_snapshot` 说「按 A 样式烧」，
    而 ASS 若还是那份共用的 `subtitle.orig.ass`，第二次导出就会把它覆盖成 B 样式。
    于是同一条任务「记录说 A、画面是 B」，事后没有任何东西对得上。
    """
    project = make_project(service, tmp_path, "快照")
    out = tmp_path / "export"
    out.mkdir(parents=True, exist_ok=True)
    ass = out / "subtitle.orig.ass"
    ass.write_text("[Script Info]\nTitle: A\n", encoding="utf-8")
    digest_a = "aaaaaaaaaaaaaaaa"
    job_ass = service._style_snapshot_ass(ass, digest_a)
    assert job_ass != ass
    assert job_ass.is_file()
    assert digest_a in job_ass.name
    # 第二次导出把共用那份改成 B 样式：任务那份必须纹丝不动
    ass.write_text("[Script Info]\nTitle: B\n", encoding="utf-8")
    assert "Title: A" in job_ass.read_text(encoding="utf-8")
    # 同样的样式再导一次：复用同一份，不堆垃圾
    again = service._style_snapshot_ass(ass, digest_a)
    assert again == job_ass
    # 换了样式就是另一份，谁也不覆盖谁
    other = service._style_snapshot_ass(ass, "bbbbbbbbbbbbbbbb")
    assert other != job_ass
    assert "Title: B" in other.read_text(encoding="utf-8")
    assert "Title: A" in job_ass.read_text(encoding="utf-8")
    assert project["id"]


# ------------------------------------------------------- 路径必须已展开
def test_every_storage_path_the_service_uses_is_already_expanded(tmp_path):
    """服务内部取路径必须走**已展开**的 property，不许读原始 storage 字段。

    这条守的是一个已经真发生过的污染：`style.render` 用 `cfg.storage.media_root`
    这个原始串拼路径，`%APPDATA%` 没被展开，于是 PNG / ASS / 样式 JSON 全写进了
    仓库根下一个叫 `%APPDATA%` 的字面目录（连日志都在里面）。同一处注释早就写着
    「用 cfg.cache_root，不是 cfg.storage.cache_root」，但别的调用点没跟。

    判据是形状而不是逐个点名：把 storage 里每个路径字段都写成带 `%APPDATA%` 的串，
    起真 service，然后**扫产品码**确认没有 `cfg.storage.<路径字段>` 这种读法。
    """
    raw = {"media_root": "%APPDATA%/wb-check/media", "cache_root": "%APPDATA%/wb-check/cache"}
    data = {
        "storage": {
            "db_path": str(tmp_path / "data" / "wb.sqlite"),
            "key_path": str(tmp_path / "data" / "keys.json"),
            "media_root": raw["media_root"],
            "cache_root": raw["cache_root"],
            "models_root": "%APPDATA%/wb-check/models",
            "ffmpeg_dir": "%APPDATA%/wb-check/bin",
        }
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=str(cfg_file), picker=None)
    try:
        for name in ("media_root", "cache_root", "models_root", "ffmpeg_dir"):
            got = getattr(svc.cfg, name)
            assert "%APPDATA%" not in got, f"{name} 没展开：{got}"
            assert Path(got).is_absolute(), f"{name} 不是绝对路径：{got}"
    finally:
        svc.close()
    # 形状锁：产品码里不许再出现 cfg.storage.<路径字段> 这种读法（注释不算）。
    # 扫描域走环境变量重定向（照 WB_WEB_ROOT / WB_CLI_SOURCE_OVERRIDE 的先例）：
    # 牙齿要能在 %TEMP% 副本上验红，不许改工作区文件来造红。
    scan = os.environ.get("WB_SERVICE_SOURCE", "").strip()
    source_path = Path(scan) if scan else ROOT / "video_workbench" / "app" / "service.py"
    assert source_path.is_file(), f"扫描目标不存在：{source_path}"
    source = source_path.read_text(encoding="utf-8")
    code_lines = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    offenders = [
        line.strip()
        for line in code_lines
        if re.search(
            r"cfg\.storage\.(media_root|cache_root|models_root|ffmpeg_dir|key_path|db_path)", line
        )
    ]
    assert offenders == [], (
        "这些地方读的是未展开的原始串，会把 %APPDATA% 当字面目录：\n" + "\n".join(offenders)
    )
