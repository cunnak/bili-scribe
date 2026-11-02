"""P1-03 目录模板与侧车测试。

这些断言对应方案书 5.3.3 的每一个"必须"：空段省略、单稿不建 P 层、
非法字符替换、重名不覆盖、同集复用、侧车字段完整。
"""

from __future__ import annotations

import json

import pytest

from video_workbench.core.layout import (
    SIDECAR,
    SIDECAR_KEYS,
    allocate,
    build_sidecar,
    finalize_source,
    read_sidecar,
    source_stem,
    write_sidecar,
)


def test_no_collection_means_no_extra_level(tmp_path):
    layout = allocate(tmp_path, up_name="示例UP", title="第一讲", bvid="BV1xx411c7mD", cid=1)
    assert layout.root.parent.name == "示例UP"
    assert layout.root.name == "第一讲"
    assert layout.root.parent.parent == tmp_path


def test_collection_adds_a_level(tmp_path):
    layout = allocate(
        tmp_path,
        up_name="示例UP",
        title="第一集 结构",
        collection_title="从零手搓",
        bvid="BV1aa411c7mF",
        cid=21,
    )
    assert layout.root.parts[-3:] == ("示例UP", "从零手搓", "第一集 结构")


def test_single_video_has_no_part_level(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="单稿", part_index=None, bvid="BV1", cid=1)
    assert "P01" not in str(layout.root)


def test_multi_p_gets_two_digit_prefix(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="多P稿", part_index=3, part_title="第三讲", bvid="BV2", cid=33)
    assert layout.root.name == "P03_第三讲"


def test_multi_p_without_title_keeps_bare_prefix(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="多P稿", part_index=7, bvid="BV2", cid=77)
    assert layout.root.name == "P07"


def test_illegal_characters_are_replaced(tmp_path):
    layout = allocate(tmp_path, up_name='UP<>:"/\\|?*', title="标题|带?非法*字符", bvid="BV3", cid=1)
    for seg in layout.root.parts[len(tmp_path.parts):]:
        assert not set(seg) & set('<>:"/\\|?*')


def test_trailing_dot_and_space_are_stripped(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="结尾有点和空格...  ", bvid="BV4", cid=1)
    assert layout.root.name == "结尾有点和空格"


def test_blank_up_falls_back(tmp_path):
    layout = allocate(tmp_path, up_name="", title="无UP", bvid="BV5", cid=1)
    assert layout.root.parent.name == "未命名UP"


def test_same_item_reuses_existing_dir(tmp_path):
    first = allocate(tmp_path, up_name="UP", title="同一集", bvid="BV6", cid=66)
    first.ensure()
    write_sidecar(first, build_sidecar(bvid="BV6", cid=66, title="同一集"))
    again = allocate(tmp_path, up_name="UP", title="同一集", bvid="BV6", cid=66)
    assert again.root == first.root


def test_different_item_with_same_name_gets_suffix(tmp_path):
    first = allocate(tmp_path, up_name="UP", title="重名", bvid="BV7", cid=1)
    first.ensure()
    write_sidecar(first, build_sidecar(bvid="BV7", cid=1, title="重名"))

    second = allocate(tmp_path, up_name="UP", title="重名", bvid="BV8", cid=2)
    assert second.root.name == "重名 (2)"
    assert second.root != first.root

    # 第一次那份的内容不能被覆盖
    assert read_sidecar(first.sidecar)["bvid"] == "BV7"


def test_dir_without_sidecar_is_not_reused(tmp_path):
    """目录存在但没有侧车：可能是用户手工放的文件，不能直接写进去。"""
    first = allocate(tmp_path, up_name="UP", title="手工目录", bvid="BV9", cid=1)
    first.root.mkdir(parents=True)
    (first.root / "用户自己的文件.txt").write_text("别动我", encoding="utf-8")

    second = allocate(tmp_path, up_name="UP", title="手工目录", bvid="BV9", cid=1)
    assert second.root != first.root
    assert (first.root / "用户自己的文件.txt").read_text(encoding="utf-8") == "别动我"


def test_ensure_creates_standard_subdirs(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="建目录", bvid="BV10", cid=1).ensure()
    for name in ("frames", "clips", "export", "logs"):
        assert (layout.root / name).is_dir()
    assert layout.sidecar.parent == layout.root
    # 源文件不再有固定名：它落在哪个目录由 finalize_source 决定，这里断言"落进项目根目录"。
    staged = layout.root / "source.mp4"
    staged.write_bytes(b"x" * 10)
    assert finalize_source(layout.root, staged, stem="建目录").parent == layout.root


def test_layout_paths_point_into_root(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="路径", bvid="BV11", cid=1)
    for path in (layout.sidecar, layout.audio, layout.transcript_json,
                 layout.transcript_srt, layout.subtitle_zh_ass, layout.summary_json):
        assert path.parent == layout.root


def test_sidecar_roundtrip_has_all_fields(tmp_path):
    layout = allocate(tmp_path, up_name="示例UP", title="侧车", bvid="BV12", cid=12).ensure()
    data = build_sidecar(
        bvid="BV12", aid=999, cid=12, page=1, title="侧车", up="示例UP",
        collection="", quality_requested="1080P", quality_actual="720P",
        url="https://www.bilibili.com/video/BV12", downloaded_at="2026-10-29T22:00:00+08:00",
        tool_version="0.1.0",
    )
    write_sidecar(layout, data)
    disk = json.loads((layout.root / SIDECAR).read_text(encoding="utf-8"))
    for key in SIDECAR_KEYS:
        assert key in disk, f"侧车缺 {key}"
    assert disk["quality_actual"] == "720P"
    assert disk["tool_version"] == "0.1.0"


def test_sidecar_rejects_missing_fields(tmp_path):
    layout = allocate(tmp_path, up_name="UP", title="缺字段", bvid="BV13", cid=1)
    with pytest.raises(ValueError) as exc:
        write_sidecar(layout, {"bvid": "BV13"})
    assert "sidecar" in str(exc.value).lower() or "缺字段" in str(exc.value)


def test_quality_downgrade_is_recorded_not_hidden(tmp_path):
    """方案书 5.3.2：不允许静默降级，实际画质必须落盘。"""
    layout = allocate(tmp_path, up_name="UP", title="降级", bvid="BV14", cid=1).ensure()
    data = build_sidecar(
        bvid="BV14", cid=1, title="降级", up="UP",
        quality_requested="1080P+", quality_actual="1080P",
    )
    write_sidecar(layout, data)
    assert read_sidecar(layout.sidecar)["quality_requested"] == "1080P+"
    assert read_sidecar(layout.sidecar)["quality_actual"] == "1080P"
