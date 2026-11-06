"""坏 JSON 这条失败路径上的文案判据（task-70 的路线②：不偏离 bs，只把文案改对）。

钉住两件实测出来的事实：

1. **能救的那句必须点名真正的开关**（``WB_SUMMARY_PARITY_STRICT=0``）。这是既有文案，
   本文件不重复它的形状锁，只补第 2 条。
2. **不许把端点的转义问题推给「截帧」开关**。带帧那一发失败在 char 121、无帧同形对照
   失败在 char 141（同一端点、同一角色、同一发请求形状），两边都是中文引述里嵌裸 ASCII
   双引号 —— 关掉截帧不解决。用户手上唯一显眼的旋钮就是那个开关，所以这句必须出现在
   失败本身上，而不是只活在决策档里。

档位口径：本文件默认档（环境变量**未设**）跑；宽松档另有一条用例钉「不追加任何建议」。
判据全部走适配层 ``_strict_hint`` 与 ``summarizer.run``，不读副本源码。
"""

from __future__ import annotations

import pytest
from test_summary_frames import adapter_run, make_frameset
from test_summary_parity import FakeLLM, make_cfg
from wb_parity_tiers import loose_env as loose_env  # noqa: F401  档位 fixture 由 pytest 注入

BAD_JSON = "这不是 JSON"
SWITCH = "WB_SUMMARY_PARITY_STRICT"


def _hint(message: str) -> str:
    from video_workbench.core import summarizer as adapter

    return adapter._strict_hint(message)


def test_the_strict_copy_names_the_real_switch_and_denies_the_frames_one(monkeypatch):
    """严格档（默认）：既给出能救的开关，也明说截帧不是解决办法。"""
    monkeypatch.delenv(SWITCH, raising=False)
    hint = _hint("模型没给出可用的总结 JSON：Expecting ',' delimiter")

    assert f"{SWITCH}=0" in hint, f"没念抢救开关，用户无从自救：{hint}"
    assert "画面帧无关" in hint, f"没撇清画面帧，用户会去关截帧白折腾：{hint}"
    assert "不解决" in hint, f"只说无关不说不解决，等于没说：{hint}"


def test_the_sentence_mentioning_the_frames_switch_denies_it(monkeypatch):
    """形状锁：文案里凡是提到「截帧」的那一句，必须同时带有「不解决」。

    拦的是未来有人把这半句改回「关掉截帧试试」——那会把一次实测否证的归因重新写成产品承诺。
    """
    monkeypatch.delenv(SWITCH, raising=False)
    hint = _hint("前半句")
    for sentence in [s for s in hint.replace("；", "。").split("。") if "截帧" in s]:
        assert "不解决" in sentence, f"截帧被写成了可选办法：{sentence}"


def test_the_loose_copy_appends_no_advice_at_all(loose_env):
    """宽松档：失败是整套抢救阶梯都用尽之后的结果，再提开关就是误导。"""
    assert _hint("前半句") == "前半句"


@pytest.mark.parametrize("frameset", [None, make_frameset(2)], ids=["no-frames", "framed"])
def test_the_disclaimer_is_on_the_path_users_actually_hit(frameset):
    """端到端：坏 JSON 冒到适配层时，两条腿（有帧/无帧）都带这句。

    这条就是那次同形对照的离线孪生 —— 判据不依赖「这次有没有附帧」，
    所以文案也不许依赖：同一条错在两边给同一句解释。
    """
    from video_workbench.app.errors import WBError

    with pytest.raises(WBError) as exc:
        adapter_run(FakeLLM([BAD_JSON]), make_cfg(), frameset)

    assert exc.value.code == "LLM_BAD_RESPONSE"
    message = str(exc.value)
    assert f"{SWITCH}=0" in message, f"报错没念开关：{message}"
    assert "画面帧无关" in message, f"报错把锅留给截帧开关：{message}"
