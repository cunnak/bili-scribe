"""P3-07 字幕样式：预设形状、校验、ASS 转换、RPC 与真实渲染。

两条断言取向：

- **校验必须一次列全**。只报第一条错，用户改一轮跑一轮，改到第五遍才发现
  "副字号也不能一样大"。
- **样式改动要能看见**。渲染出来的 ASS 里，字号/边框样式/BGR 颜色都得跟着变；
  只比较"函数没抛异常"等于没验。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.core import styles


def call(svc, method: str, params: dict | None = None):
    from video_workbench.app.rpc import Router  # noqa: F401 —— 仅为说明调用形态

    return svc.router.dispatch(method, params or {}, "t")


def ok(svc, method: str, params: dict | None = None):
    res = call(svc, method, params)
    assert res.get("ok") is True, res
    return res.get("data")


def err(svc, method: str, params: dict | None = None) -> dict:
    res = call(svc, method, params)
    assert res.get("ok") is False, res
    return res.get("error") or {}


# ---------------------------------------------------------------- 预设与归一


def test_builtin_presets_have_both_modes():
    presets = {p["id"]: p for p in styles.builtin_presets()}
    assert set(presets) == {"default", "rounded-default"}
    assert presets["default"]["mode"] == "ass"
    assert presets["rounded-default"]["mode"] == "rounded"
    # 主副字号有层级，不然圆角条和普通字幕看起来一样
    assert presets["default"]["primary"]["size"] > presets["default"]["secondary"]["size"]


def test_normalize_fills_defaults_and_drops_unknown_keys():
    conf = styles.normalize(
        {"id": "x", "name": "X", "primary": {"size": 60, "bogus": 1}, "nope": True}
    )
    assert conf["primary"]["size"] == 60
    assert "bogus" not in conf["primary"]
    assert "nope" not in conf
    assert conf["secondary"]["size"] == 36  # 没给的取默认


def test_normalize_base_reads_legacy_flat_row():
    """老库里 default 是平铺的（font_size/primary_color），不能因为改结构就丢值。"""
    row = {
        "id": "old",
        "name": "旧",
        "mode": "ass",
        "config": {
            "font": "SimHei",
            "font_size": 66,
            "primary_color": "#FF0000",
            "outline_width": 4,
        },
    }
    conf = styles.normalize_base(row)
    assert conf["id"] == "old"
    assert conf["primary"]["size"] == 66
    assert conf["primary"]["color"] == "#FF0000"
    assert conf["primary"]["font"] == "SimHei"


# ---------------------------------------------------------------- 校验


def test_validate_lists_all_problems_at_once():
    bad = styles.normalize(
        {"primary": {"size": 200, "outline_width": 40}, "secondary": {"size": 200}}
    )
    with pytest.raises(Exception) as exc:
        styles.validate(bad)
    message = str(getattr(exc.value, "message", exc.value))
    assert "主字幕字号" in message
    assert "主字幕描边" in message
    assert "字号至少差" in message


def test_validate_rejects_unknown_mode_and_layout():
    with pytest.raises(Exception) as exc:
        styles.validate(styles.normalize({"mode": "fancy"}))
    assert "样ass" not in str(exc.value)  # 别把英文枚举拼进中文句子
    with pytest.raises(Exception):
        styles.validate(styles.normalize({"layout": "top"}))


def test_validate_warns_when_bar_too_wide():
    wide = styles.normalize(
        {
            "mode": "rounded",
            "preview": {"text_length": "long"},
            "rounded": {"size": 60, "padding_h": 40},
        },
        base=styles.rounded_preset(),
    )
    _style, warnings = styles.validate(wide)
    assert warnings, "长文本 + 大字号应该给出顶边警告"
    assert "超过" in warnings[0]


def test_validate_passes_clean_style_without_warnings():
    style, warnings = styles.validate(styles.default_preset())
    assert warnings == []
    assert style["mode"] == "ass"


# ---------------------------------------------------------------- ASS 转换


def test_ass_style_scales_with_height():
    conf720 = styles.to_ass_style(styles.default_preset(), height=720)
    conf1080 = styles.to_ass_style(styles.default_preset(), height=1080)
    assert conf720["size"] == 48
    assert conf1080["size"] == 72  # 1.5 倍
    assert conf1080["outline"] > conf720["outline"]


def test_ass_text_carries_style_numbers():
    ass = styles.preview_ass(styles.default_preset(), height=720, texts=["测试一句"])
    line = next(raw for raw in ass.splitlines() if raw.startswith("Style:"))
    parts = [p.strip() for p in line.split(",")]
    assert parts[1] == "Microsoft YaHei"
    assert parts[2] == "48"  # Fontsize
    assert parts[15] == "1"  # BorderStyle=1 描边
    assert parts[3] == "&H00FFFFFF"  # 白色（BGR）
    assert "测试一句" in ass


def test_rounded_style_uses_box_and_alpha():
    ass = styles.preview_ass(styles.rounded_preset(), height=720, texts=["圆角条"])
    line = next(raw for raw in ass.splitlines() if raw.startswith("Style:"))
    parts = [p.strip() for p in line.split(",")]
    assert parts[15] == "3"  # BorderStyle=3 → 实心框
    assert parts[6].startswith("&H99")  # 60% 不透明 → alpha 0x99
    assert parts[17] == "0"  # 阴影关掉，否则框外一圈黑边


def test_canvas_follows_orientation():
    assert styles.canvas_size(styles.default_preset()) == {"width": 1280, "height": 720}
    portrait = styles.normalize({"preview": {"orientation": "portrait"}})
    assert styles.canvas_size(portrait) == {"width": 720, "height": 1280}


def test_bar_ratio_grows_with_text_length():
    short = styles.normalize({"preview": {"text_length": "short"}}, base=styles.rounded_preset())
    long = styles.normalize({"preview": {"text_length": "long"}}, base=styles.rounded_preset())
    assert styles.bar_ratio(long) > styles.bar_ratio(short)


# ---------------------------------------------------------------- RPC


def test_style_list_has_two_builtins(service):
    rows = ok(service, "style.list")
    by_id = {row["id"]: row for row in rows}
    assert {"default", "rounded-default"} <= set(by_id)
    assert by_id["default"]["builtin"] is True
    assert by_id["default"]["config"]["primary"]["size"] == 48


def test_style_get_falls_back_to_preset_by_name(service):
    data = ok(service, "style.get", {"id": "圆角默认"})
    assert data["style"]["mode"] == "rounded"
    assert data["builtin"] is True
    assert data["canvas"] == {"width": 1280, "height": 720}


def test_style_get_unknown_is_not_found(service):
    assert err(service, "style.get", {"id": "nope"})["code"] == "NOT_FOUND"


def test_style_save_creates_row_and_file(service):
    data = ok(
        service,
        "style.save",
        {
            "name": "我的样式",
            "config": {"primary": {"size": 60, "color": "#FFEE00"}, "secondary": {"size": 40}},
        },
    )
    style_id = data["style"]["id"]
    assert style_id.startswith("sty")
    assert data["style"]["primary"]["size"] == 60
    saved = Path(data["file"])
    assert saved.is_file()
    on_disk = json.loads(saved.read_text(encoding="utf-8"))
    assert on_disk["primary"]["color"] == "#FFEE00"
    listed = {row["id"]: row for row in ok(service, "style.list")}
    assert listed[style_id]["builtin"] is False


def test_style_save_refuses_builtin(service):
    error = err(
        service,
        "style.save",
        {"id": "default", "name": "改内置", "config": {"primary": {"size": 60}}},
    )
    assert error["code"] == "CONFLICT"


def test_style_save_validates(service):
    error = err(service, "style.save", {"name": "坏样式", "config": {"primary": {"size": 999}}})
    assert error["code"] == "VALIDATION"


def test_style_update_keeps_id(service):
    created = ok(service, "style.save", {"name": "甲", "config": {}})
    style_id = created["style"]["id"]
    updated = ok(
        service, "style.save", {"id": style_id, "name": "甲2", "config": {"primary": {"size": 52}}}
    )
    assert updated["style"]["id"] == style_id
    assert updated["style"]["name"] == "甲2"
    assert updated["style"]["primary"]["size"] == 52


def test_style_save_keeps_names_unique(service):
    """重名不能让 sqlite 的 UNIQUE 直接冒成 INTERNAL：自动加序号，并告知改过名。"""
    first = ok(service, "style.save", {"name": "重名", "config": {}})
    second = ok(service, "style.save", {"name": "重名", "config": {}})
    third = ok(service, "style.save", {"name": "重名", "config": {}})
    assert first["style"]["name"] == "重名"
    assert second["style"]["name"] == "重名 2"
    assert third["style"]["name"] == "重名 3"
    assert second["renamed"] is True and first["renamed"] is False
    # 更新自己不算重名
    again = ok(service, "style.save", {"id": first["style"]["id"], "name": "重名", "config": {}})
    assert again["style"]["name"] == "重名"


def test_style_import_with_same_name_is_renamed(service, tmp_path):
    source = tmp_path / "dup.json"
    source.write_text(
        json.dumps({"name": "同一份", "primary": {"size": 60}, "secondary": {"size": 40}}),
        encoding="utf-8",
    )
    one = ok(service, "style.import", {"path": str(source)})
    two = ok(service, "style.import", {"path": str(source)})
    assert one["style"]["id"] != two["style"]["id"]
    assert (one["style"]["name"], two["style"]["name"]) == ("同一份", "同一份 2")


def test_style_delete_removes_row_and_file(service):
    created = ok(service, "style.save", {"name": "待删", "config": {}})
    style_id = created["style"]["id"]
    assert Path(created["file"]).is_file()
    ok(service, "style.delete", {"id": style_id})
    assert err(service, "style.get", {"id": style_id})["code"] == "NOT_FOUND"
    assert not Path(created["file"]).exists()


def test_style_delete_refuses_builtin(service):
    assert err(service, "style.delete", {"id": "default"})["code"] == "CONFLICT"


def test_style_import_creates_new_id(service, tmp_path):
    source = tmp_path / "my.json"
    source.write_text(
        json.dumps(
            {
                "id": "default",
                "name": "导入的",
                "mode": "ass",
                "primary": {"size": 58},
                "secondary": {"size": 40},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    data = ok(service, "style.import", {"path": str(source)})
    assert data["style"]["id"] != "default"  # 不覆盖内置
    assert data["style"]["name"] == "导入的"
    assert data["style"]["primary"]["size"] == 58


def test_style_import_bad_json(service, tmp_path):
    source = tmp_path / "bad.json"
    source.write_text("{oops", encoding="utf-8")
    assert err(service, "style.import", {"path": str(source)})["code"] == "VALIDATION"
    assert (
        err(service, "style.import", {"path": str(tmp_path / "none.json")})["code"] == "NOT_FOUND"
    )


def test_style_export_writes_json(service, tmp_path):
    target = Path(str(service.cfg.storage.media_root)) / "out" / "copy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = ok(service, "style.export", {"id": "default", "path": str(target)})
    assert Path(data["path"]).is_file()
    body = json.loads(Path(data["path"]).read_text(encoding="utf-8"))
    assert body["id"] == "default"
    assert body["mode"] == "ass"


def test_style_export_outside_roots_is_rejected(service):
    assert (
        err(service, "style.export", {"id": "default", "path": "C:/Windows/evil.json"})["code"]
        == "VALIDATION"
    )


def test_style_fonts_lists_something_or_says_why(service):
    data = ok(service, "style.fonts")
    assert isinstance(data["fonts"], list)
    if data["count"] == 0:
        assert data["note"], "读不到字体要给出原因，不能静默空列表"


def test_style_render_makes_png(service):
    pytest.importorskip("shutil")
    if service._ffmpeg.ffmpeg is None:
        pytest.skip("本机没有 ffmpeg，真渲染跳过（画布预览仍可用）")
    data = ok(service, "style.render", {"id": "rounded-default", "text": "渲染测试"})
    png = Path(data["path"])
    assert png.is_file() and png.stat().st_size > 0
    assert (data["width"], data["height"]) == (1280, 720)
    ass = Path(data["ass"]).read_text(encoding="utf-8")
    style_line = next(raw for raw in ass.splitlines() if raw.startswith("Style:"))
    assert style_line.split(",")[15].strip() == "3"  # 圆角预设走 BorderStyle=3
    assert "渲染测试" in ass


def test_style_render_rejects_bad_config(service):
    assert (
        err(service, "style.render", {"config": {"primary": {"size": 1}}})["code"] == "VALIDATION"
    )
