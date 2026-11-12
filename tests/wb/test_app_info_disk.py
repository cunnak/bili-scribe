"""app.info 的 disk 槽：状态条那一格今天恒显示「磁盘 —」，因为从来没人给过数。

判据守三件事：

1. **真的算了**：`app_info()["disk"]` 是 `{"free_bytes","total_bytes"}` 两个 int，
   不是 None、不是字符串、不是 shutil 的 namedtuple 原样透传（那个在 JSON 里会变成数组）。
2. **探测失败不拖垮 app.info**：盘拔了 / 网络盘断线 / 目录没权限时给 `None`，
   而不是让整个 `app.info` 抛 —— 界面连"哪里出错"都显示不出来那种。
3. **演示态与真后端同形**：mock.js 的 INFO 也得有这两个键，否则截图与真机长得不一样，
   那一格会被读成缺陷。

第 1、2 条的牙齿：把 `disk_usage` 换成"永远返回 None"，第 1 条必红；
换成"永远抛异常"，第 2 条必红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.infra import files as files_mod

ROOT = Path(__file__).resolve().parents[2]


def test_disk_usage_reports_two_integers(tmp_path: Path) -> None:
    """正常目录：两个非负 int，且 free <= total。"""
    usage = files_mod.disk_usage(tmp_path)
    assert isinstance(usage, dict), f"disk_usage 返回的不是 dict：{usage!r}"
    assert set(usage) == {"free_bytes", "total_bytes"}, f"键不对：{sorted(usage)}"
    for key, value in usage.items():
        assert isinstance(value, int) and not isinstance(value, bool), f"{key} 不是 int：{value!r}"
        assert value >= 0, f"{key} 是负数：{value}"
    assert usage["free_bytes"] <= usage["total_bytes"], (
        f"剩余大于总量：{usage} —— 大概率把 free/total 两列接反了"
    )


def test_disk_usage_walks_up_to_an_existing_ancestor(tmp_path: Path) -> None:
    """素材盘目录还没建出来（首启、刚改配置）时必须往上找，不能返回 None。

    这条与 ensure_free_space 共用同一套探测逻辑：写盘守卫能过的地方，
    读数也必须能过 —— 否则会出现"能下载但状态条说读不到盘"这种自相矛盾的画面。
    """
    missing = tmp_path / "not-created-yet" / "deeper"
    assert not missing.exists()
    usage = files_mod.disk_usage(missing)
    assert usage is not None, "目录还不存在时应该往上找已存在的祖先，而不是放弃"


def test_disk_usage_returns_none_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测失败一律 None。抛出来会让整个 app.info 挂掉。

    两种真实失败各一条腿：
    - **盘符不存在**：走上层祖先走到根还是不存在，最终 shutil 抛 OSError；
    - **系统调用本身失败**（网络盘断线、权限被改）：直接把 shutil.disk_usage 换掉，
      这才是能确定复现的形状。造一个"碰巧会抛"的假路径不算数 —— 实测 Windows 上
      `Path("")` 的 `exists()` 是 True（指向当前目录），`Path("\x00").exists()`
      也只是 False，两者都不会抛，拿它们当"必然抛"的夹具是我想当然。
    """
    assert files_mod.disk_usage("Z:\\definitely-no-such-drive-\\x") is None, (
        "不存在的盘符应该给 None，而不是把 OSError 抛出去"
    )

    def boom(_path):
        raise OSError("网络盘断线")

    monkeypatch.setattr(files_mod.shutil, "disk_usage", boom)
    assert files_mod.disk_usage(".") is None, "shutil 抛 OSError 时 disk_usage 应该吃掉并给 None"

    def boom_value(_path):
        raise ValueError("非法盘符")

    monkeypatch.setattr(files_mod.shutil, "disk_usage", boom_value)
    assert files_mod.disk_usage(".") is None, "shutil 抛 ValueError 时 disk_usage 应该吃掉并给 None"


def test_app_info_carries_the_disk_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`app.info` 的载荷里必须有 disk，而且是可 JSON 序列化的两个 int。

    接成 namedtuple 是这里最容易犯的错：Python 侧读 `usage.free` 一切正常，
    过了 JSON 变成 `[free, total]` 数组，前端 `disk.free_bytes` 恒 undefined，
    状态条永远显示「磁盘 …」。所以这条判据走一遍 `json.dumps`/`loads`。
    """
    from video_workbench.app.service import WorkbenchService

    service = WorkbenchService()
    info = service.app_info()
    assert "disk" in info, f"app.info 没有 disk 槽：{sorted(info)}"
    disk = info["disk"]
    assert disk is None or isinstance(disk, dict), f"disk 形状不对：{disk!r}"
    if disk is not None:
        assert set(disk) == {"free_bytes", "total_bytes"}, f"disk 键不对：{sorted(disk)}"
        round_tripped = json.loads(json.dumps(disk))
        assert round_tripped == disk, (
            f"disk 过了 JSON 就变形了：{disk!r} -> {round_tripped!r} "
            f"（多半是把 shutil 的 namedtuple 原样塞进去了）"
        )


def test_a_dead_drive_does_not_take_down_app_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """素材盘读不到时 app.info 仍然要能返回，disk 给 None。

    牙齿打在**系统调用**这一层（`files.shutil.disk_usage`），不是打在
    `service.disk_usage` 这个名字上：后者只是 import 进来的一份引用，
    把它换成一个会抛的函数，测的就是"service 有没有第二道 try" ——
    而"绝不抛"这件事只有一个 owner（`infra/files.py:disk_usage`）。
    两处各写一道 try 才是真的隐患：改了其中一处，另一处永远不知道。
    """
    from video_workbench.app import service as service_mod

    def boom(_path):
        raise OSError("盘符不存在")

    monkeypatch.setattr(files_mod.shutil, "disk_usage", boom)
    info = service_mod.WorkbenchService().app_info()
    assert info["disk"] is None, "读盘失败时 disk 应该是 None"
    assert "version" in info, "app.info 因为读盘失败少了一半字段"


def test_the_mock_payload_has_the_same_disk_shape() -> None:
    """演示态与真后端同形：mock.js 的 INFO 里也要有两个 int。"""
    source = (ROOT / "video_workbench" / "web" / "js" / "mock.js").read_text(encoding="utf-8")
    assert "disk: { free_bytes:" in source, "mock.js 的 INFO 没有 disk"
    assert "total_bytes:" in source, "mock.js 的 disk 缺 total_bytes"


def test_the_status_bar_reads_the_payload_and_never_hardcodes_a_dash() -> None:
    """状态条必须从载荷读，不许写死一句「磁盘 —」。"""
    source = (ROOT / "video_workbench" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    assert "function paintDisk(" in source, "app.js 没有 paintDisk"
    assert "disk.free_bytes" in source, "paintDisk 没有读 free_bytes"
    assert '"磁盘 —"' not in source, "app.js 里还留着写死的「磁盘 —」"


def test_the_three_disk_states_are_visually_distinct() -> None:
    """有数 / 读不到 / 还没到，三种输入必须画出三种不同的字。

    合成一句话（都画成「磁盘 —」）就分不出"没算"和"读不到" ——
    而这两件事该做的动作完全不同：一个是等一下，一个是去查盘。
    """
    source = (ROOT / "video_workbench" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    start = source.find("function paintDisk(")
    end = source.find("\n  }", start)
    body = source[start:end]
    assert '"磁盘 "' in body or "'磁盘 '" in body, "有数时没有拼「磁盘 + 大小」"
    assert '"磁盘 ?"' in body, "读不到时没有单独的「磁盘 ?」"
    assert '"磁盘 …"' in body, "还没到数时没有单独的「磁盘 …」"
