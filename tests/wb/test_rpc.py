"""RPC 层的验收（P0-05）。

整个应用对外的唯一入口就是 ``Router.dispatch``，所以这里要钉住四件事：

1. 信封形状固定为 ``{ok, data | error, request_id}``；前端 ``rpc.js`` 只解析 ``data``。
2. 未知方法返回错误码而不是抛异常——一次手滑不该把桥打断。
3. 参数不合法是 VALIDATION，且能说清哪个字段。
4. ``request_id`` 原样回显，否则前端没法把响应配对到请求。
"""

from __future__ import annotations

import time

import pytest


def _ok(response: dict) -> dict:
    assert response["ok"] is True, response
    assert "data" in response, response
    return response["data"]


def _err(response: dict, code: str) -> dict:
    assert response["ok"] is False, response
    assert response["error"]["code"] == code, response
    assert response["error"]["message"]
    return response["error"]


def test_unknown_method_reports_error_not_crash(service):
    err = _err(service.dispatch("nope.method"), "UNSUPPORTED")
    assert "nope.method" in err["message"]


def test_request_id_echoed(service):
    assert service.dispatch("app.info", {}, request_id="r-1")["request_id"] == "r-1"
    assert service.dispatch("nope.method", {}, request_id="r-2")["request_id"] == "r-2"


def test_app_info_shape(service):
    info = _ok(service.dispatch("app.info"))
    assert set(info) >= {"name", "version", "skin", "deps", "db_path", "uptime"}
    assert info["name"] == "VideoWorkbench"
    assert set(info["deps"]) >= {"ffmpeg", "yt_dlp", "faster_whisper"}


def test_bootstrap_carries_everything_the_shell_needs(service):
    boot = _ok(service.dispatch("app.bootstrap"))
    assert set(boot) >= {"info", "settings", "library", "jobs", "styles"}
    assert boot["library"]["items"] == []
    assert boot["library"]["total"] == 0
    assert set(boot["library"]) >= {"items", "total", "limit", "offset"}
    assert isinstance(boot["jobs"], list)
    # P0 至少要有内置样式：空表会让后续 FFmpeg 渲染拿到空参数
    assert any(row["id"] == "default" and row["builtin"] is True for row in boot["styles"])


def test_settings_get_exposes_api_key_but_bootstrap_does_not(service):
    service.cfg.llm.api_key = "sk-test"
    stored = _ok(service.dispatch("app.settings.get"))
    assert stored["llm"]["api_key"] == "sk-test"

    boot = _ok(service.dispatch("app.bootstrap"))
    assert "api_key" not in boot["settings"]["llm"]


@pytest.mark.parametrize(
    "method, params",
    [
        ("app.skin.set", {}),
        ("app.skin.set", {"skin": 3}),
        ("app.skin.set", {"skin": "没有这个皮肤"}),
        ("app.rail.set", {"collapsed": "maybe"}),
        ("app.settings.put", {"values": "不是对象"}),
        ("library.get", {}),
    ],
)
def test_bad_params_are_validation_errors(service, method: str, params: dict):
    _err(service.dispatch(method, params), "VALIDATION")


def test_settings_put_persists_and_reports_restart(service):
    result = _ok(service.dispatch("app.settings.put", {"values": {"ui": {"skin": "exhibition"}}}))
    assert result["settings"]["ui"]["skin"] == "exhibition"
    assert isinstance(result["restart"], list)
    assert result["saved"]  # 写盘路径

    from video_workbench.config import WorkbenchConfig

    assert WorkbenchConfig.load(service.cfg.source_path).ui.skin == "exhibition"


def test_settings_put_rejects_unknown_key(service):
    _err(service.dispatch("app.settings.put", {"values": {"ui": {"skni": "exhibition"}}}), "VALIDATION")


def test_skin_set_changes_state(service):
    _ok(service.dispatch("app.skin.set", {"skin": "exhibition"}))
    assert service.cfg.ui.skin == "exhibition"
    assert _ok(service.dispatch("app.info"))["skin"] == "exhibition"


def test_library_get_missing_project(service):
    _err(service.dispatch("library.get", {"id": "pj_nope"}), "NOT_FOUND")


def test_fs_pick_without_picker_is_unsupported(service):
    _err(service.dispatch("fs.pick", {"kind": "dir"}), "UNSUPPORTED")


def test_fs_reveal_rejects_outside_path(service):
    _err(service.dispatch("fs.reveal", {"path": "C:/Windows/notepad.exe"}), "VALIDATION")


def test_events_reach_the_sink(service):
    """换皮肤要让所有窗口同步，所以服务端必须真的往桥上发一条事件。"""
    import json

    seen: list[tuple[str, str]] = []
    service.events.set_sink(lambda name, payload: seen.append((name, payload)))
    service.events.emit("skin.changed", {"skin": "exhibition"})
    deadline = time.time() + 3
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    assert seen, "3s 内没有收到事件（节流线程可能没起来）"
    # sink 的第二段必须是**已经序列化好的 JSON 字符串**：
    # 它会被直接拼进 evaluate_js，传 dict 过去会变成 Python repr。
    assert seen[0][0] == "skin.changed"
    assert json.loads(seen[0][1])["skin"] == "exhibition"


def test_duplicate_registration_is_a_programming_error(service):
    with pytest.raises(ValueError):
        service.router.add("app.info", lambda: {})
