"""`filter_path` / `subtitle_filter`：把字幕塞进滤镜字符串的正确姿势。

这里全是真跑出来的坑，不是猜的：

- 冒号不转义 → ``Unable to parse "original_size" option value``（看不出是路径问题）；
- `.srt` 喂给 ``ass=`` → ``Could not create a libass track`` + exit -22；
- 文件名里有 `'` → ffmpeg 报"打不开输出文件"，不给文件名，写 `\\'` 或 shell 的
  `'\\''` 都不接，只能把文件镜像到干净名字的临时路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.infra.ffmpeg import (
    FILTER_UNSAFE_CHARS,
    filter_path,
    mirror_for_filter,
    subtitle_filter,
)


def unwrap(quoted: str) -> str:
    """把 filter_path 的结果还原成真实路径（去外层引号、把 \\: 变回 :）。"""
    return quoted.strip("'").replace("\\:", ":")


def test_filter_path_escapes_colon_and_wraps_quotes(tmp_path):
    quoted = filter_path(tmp_path / "x.ass")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "\\:" in quoted
    # 反斜杠统一成正斜杠，除了 \: 不该再有别的反斜杠
    assert "\\" not in quoted.replace("\\:", "")
    assert unwrap(quoted) == (tmp_path / "x.ass").resolve().as_posix()


def test_subtitle_filter_chooses_by_extension():
    assert subtitle_filter("a/b/transcript.srt").startswith("subtitles=")
    assert subtitle_filter("a/b/transcript.vtt").startswith("subtitles=")
    assert subtitle_filter("x/y.ass").startswith("ass=")
    assert subtitle_filter("x/y.SSA").startswith("ass=")


def test_subtitle_filter_empty_returns_empty():
    assert subtitle_filter("") == ""
    assert subtitle_filter(None) == ""
    assert subtitle_filter("   ") == ""


@pytest.mark.parametrize("name", ["a'b.srt", "a,b.ass", "x[y].ass", "s;t.ass"])
def test_unsafe_filenames_are_mirrored_to_a_clean_path(tmp_path, name):
    src = tmp_path / name
    src.write_text("1\n00:00:00.500 --> 00:00:01.500\nhi\n", encoding="utf-8")
    out = filter_path(src)
    real = unwrap(out)
    assert "'" not in real, out                # 引号只剩最外层包裹
    for ch in FILTER_UNSAFE_CHARS:
        assert ch not in Path(real).name
    assert Path(real).is_file()                # 真的复制出来了


def test_mirror_reuses_and_refreshes(tmp_path):
    src = tmp_path / "q'uote.srt"
    src.write_text("x", encoding="utf-8")
    first = mirror_for_filter(src)
    again = mirror_for_filter(src)
    assert first == again
    assert first.is_file()
    src.write_text("x" * 5, encoding="utf-8")
    third = mirror_for_filter(src)
    assert third.stat().st_size == 5           # 源变新了要重新拷


def test_clean_path_is_not_mirrored(tmp_path):
    src = tmp_path / "normal.srt"
    src.write_text("x", encoding="utf-8")
    assert unwrap(filter_path(src)) == src.resolve().as_posix()
