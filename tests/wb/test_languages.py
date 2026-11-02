"""源语言码表：必须与 faster-whisper 逐字一致，且默认拒绝而不是猜。

这张表是"选源语言"功能的地基。它一旦和 faster-whisper 漂移，用户选的码会在
VAD 之后（也就是几十秒之后）才炸，报错还是一句英文 ValueError——用户既看不出
是自己选错了，也没法从报错里知道该选什么。
"""

from __future__ import annotations

import pytest

from video_workbench.app.errors import WBError
from video_workbench.infra import languages


def test_codes_match_faster_whisper_exactly():
    tokenizer = pytest.importorskip("faster_whisper.tokenizer")
    assert set(languages.CODES) == set(tokenizer._LANGUAGE_CODES), (
        "码表与 faster-whisper 不一致。它升级时这里先红，好过用户转录几十秒后才炸"
    )


def test_every_code_has_a_chinese_label():
    missing = sorted(set(languages.CODES) - set(languages.NAMES))
    assert not missing, f"这些码没有中文名，下拉里会露出裸码：{missing}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("zh", "zh"),
        (" ZH ", "zh"),
        ("Zh", "zh"),
        ("yue", "yue"),
        ("", ""),
        ("auto", ""),
        ("AUTO", ""),
        ("自动检测", ""),
        ("none", ""),
    ],
)
def test_normalize_accepts_and_folds(raw, expected):
    assert languages.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["zh-CN", "en-US", "中文", "chi", "zz", "x"])
def test_normalize_rejects_unknown(raw):
    with pytest.raises(WBError) as info:
        languages.normalize(raw)
    assert info.value.code == "VALIDATION"
    assert raw in str(info.value), "报错要回显用户填的原值，否则他不知道错在哪"

    # faster-whisper 自己的报错长这样：'<v>' is not a valid language code。
    # 用户看到这句既不知道是自己选错了，也不知道该改成什么，所以必须在入口拦掉。


def test_catalog_auto_first_and_fully_partitioned():
    cat = languages.catalog()
    assert cat["common"][0]["code"] == "", "第一项必须是自动检测（保留它，但不预选）"
    assert len(cat["common"]) == 8, "常用项就是用户拍板的 8 个"

    common = {c["code"] for c in cat["common"]}
    tail = {c["code"] for c in cat["all"]}
    assert not (common - {""}) & tail, "常用项不该在长尾里重复出现"
    # "" 是「自动检测」这个选项，不是 faster-whisper 的码，算覆盖时要先摘掉。
    assert len((common - {""}) | tail) == len(languages.CODES), "常用 + 长尾要刚好覆盖全部码"
