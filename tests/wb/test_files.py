"""路径与文件名的验收（P0-03 / RPC 边界）。

这些函数看着琐碎，但它们站在 RPC 边界上：前端传来的每一段路径都要从这里过。
``resolve_under`` 一旦松了口，页面就能用 ``../..`` 把文件写到工作区外面去。
所以这里测的是"拒绝"而不是"通过"。
"""

from __future__ import annotations

import pytest

from video_workbench.app.errors import WBError
from video_workbench.infra.files import (
    human_bytes,
    is_within,
    resolve_under,
    sanitize_name,
    sanitize_relpath,
    unique_path,
)


# --------------------------------------------------------------- 文件名


def test_sanitize_replaces_illegal_chars():
    got = sanitize_name('a<b>c:d"e/f\\g|h?i*j')
    for bad in '<>:"/\\|?*':
        assert bad not in got
    assert got  # 不能因为全是非法字符就变成空串


def test_sanitize_strips_trailing_dots_and_spaces():
    """Windows 会把结尾的点和空格吃掉，导致"名字看起来对、其实打不开"。"""
    assert not sanitize_name("标题... ").endswith((".", " "))


def test_sanitize_avoids_reserved_names():
    for name in ("CON", "prn", "aux", "nul", "com1", "lpt9"):
        got = sanitize_name(name)
        assert got.upper() != name.upper(), name


def test_sanitize_truncates_long_names():
    got = sanitize_name("长" * 400, max_len=40)
    assert len(got) <= 41  # 允许一个用来避让的后缀字符


def test_sanitize_relpath_keeps_levels():
    got = sanitize_relpath("字幕/v1/乱:名?.srt")
    parts = [p for p in got.replace("\\", "/").split("/") if p]
    assert len(parts) == 3, got          # 层级不能被压平
    assert ":" not in got and "?" not in got


# --------------------------------------------------------------- 越界


@pytest.mark.parametrize(
    "rel",
    ["", "   ", "../outside.txt", "a/../../b", "C:/Windows/system32", "/etc/passwd", ".", ".."],
)
def test_resolve_under_rejects_escape(tmp_path, rel: str):
    with pytest.raises(WBError) as info:
        resolve_under(tmp_path, rel)
    assert info.value.code == "VALIDATION"


def test_resolve_under_accepts_normal_relative(tmp_path):
    got = resolve_under(tmp_path, "字幕/第一版.srt")
    assert got == tmp_path / "字幕" / "第一版.srt"
    assert is_within(tmp_path, got)


def test_is_within_is_not_a_string_prefix_check(tmp_path):
    """``C:\\a`` 与 ``C:\\ab`` 只差一个字母，前缀比较会误判成"在内部"。"""
    root = tmp_path / "a"
    sibling = tmp_path / "ab"
    assert is_within(root, root / "x.txt") is True
    assert is_within(root, sibling / "x.txt") is False


# --------------------------------------------------------------- 其他


def test_unique_path_never_overwrites(tmp_path):
    target = tmp_path / "切片.mp4"
    target.write_text("x", encoding="utf-8")
    second = unique_path(target)
    assert second != target and not second.exists()
    second.write_text("y", encoding="utf-8")
    third = unique_path(target)
    assert third not in (target, second) and not third.exists()


def test_human_bytes():
    # 0 和 None 都渲染成占位符：界面上"0 B"和"没算出来"是两回事，混在一起会误导
    assert human_bytes(0) == "—"
    assert human_bytes(None) == "—"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(1024 * 1024 * 3) == "3.0 MB"
