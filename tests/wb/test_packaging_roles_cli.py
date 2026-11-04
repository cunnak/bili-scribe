"""打包自检的两个出口：`--check` 落 wb-check.json，`--roles` 落 wb-roles.json。

为什么这两条要有常驻用例：包内那 6 篇 txt 是**运行期**从 `_internal/.../prompts`
读的，构建脚本比的是"字节进包了"，证明不了"冻结态真读到了、绑对了角色"；而窗口
子系统没有 stdout，结论只能靠 exe 旁边的 json 文件。这两条一旦坏，验收会变成
"看了退出码 0 就以为过了"——正是 task-28 要堵的洞。
"""

from __future__ import annotations

import json
import sys

import pytest

from video_workbench import desktop


@pytest.fixture()
def tmp_config(tmp_path):
    """一份指向 Temp 的完整配置：库与密钥都不碰真实目录。

    ``_roles`` 会真的装配 ``WorkbenchService``（它要读 DB、跑恢复探测），所以
    ``storage.db_path`` 必须指到 Temp——沿用 ``test_config_shared.py:65`` 的写法。
    """
    from video_workbench.config import WorkbenchConfig

    cfg = WorkbenchConfig()
    cfg.storage.db_path = str(tmp_path / "wb-roles-test.db")
    cfg.storage.key_path = str(tmp_path / "wb-roles-test.key")
    path = tmp_path / "config.yaml"
    cfg.save(path)
    return path


@pytest.fixture()
def frozen(tmp_path, monkeypatch):
    """模拟冻结态：`_emit_json` 只有在 sys.frozen 为真时才落文件。"""
    exe = tmp_path / "VideoWorkbench.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    return tmp_path


def test_roles_writes_a_payload_a_script_can_grep(frozen, tmp_config):
    rc = desktop._roles(str(tmp_config))
    assert rc == 0
    payload = json.loads((frozen / "wb-roles.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True and payload["what"] == "roles"
    keys = [row["key"] for row in payload["roles"]]
    assert payload["count"] == len(keys)
    # 默认角色必须在这份名单里（前端拿它做默认选项）
    assert payload["default"] in keys
    # 有真源的角色必须点名 txt 并给出字节与 sha8；无源的必须是 None 而不是空串
    sourced = {row["key"]: row for row in payload["roles"] if row["prompt_source"]}
    plain = [row for row in payload["roles"] if not row["prompt_source"]]
    assert sourced, "至少要有一个角色绑着 txt 真源，否则这条用例没在测东西"
    for row in sourced.values():
        assert row["prompt_source"].endswith(".txt")
        assert row["prompt_bytes"] > 0
        assert len(row["prompt_sha8"]) == 8
    for row in plain:
        assert row["prompt_source"] is None
        assert row["prompt_bytes"] == 0


def test_check_still_writes_its_own_file(frozen):
    """`--roles` 加了 name 参数后，`--check` 的落盘文件名不能被改掉。"""
    path = desktop._emit_json({"ok": True, "probe": 1})
    assert path.endswith("wb-check.json")
    assert json.loads((frozen / "wb-check.json").read_text(encoding="utf-8"))["probe"] == 1
    assert not (frozen / "wb-roles.json").exists()


def test_main_dispatches_roles_before_opening_a_window(monkeypatch, tmp_config):
    """`--roles` 必须**真的接到** `_roles`：加了旗标却没派发，冻结态会直接开窗。

    这条不是我凭空写的：task-28 第一次验收时，`cli.py` 有了 `--roles`、exe 却在
    `desktop.py` 的解析器上，结果是 `error: unrecognized arguments: --roles`。
    所以这里既钉派发、也钉「不许掉进建窗分支」——真掉进去是弹一个窗口而不是报错，
    人工验收很容易看成"起来了就是好的"。
    """
    seen: dict[str, str] = {}

    def fake_roles(config_path=None):
        seen["cfg"] = config_path
        return 0

    def boom(*_a, **_k):
        raise AssertionError("--roles 不该建窗：命令行的出口必须在 create_window 之前")

    monkeypatch.setattr(desktop, "_roles", fake_roles)
    monkeypatch.setattr(desktop, "create_window", boom)
    assert desktop.main(["--roles", "--config", str(tmp_config)]) == 0
    assert seen["cfg"] == str(tmp_config)
