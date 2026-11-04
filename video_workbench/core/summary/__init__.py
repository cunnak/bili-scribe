"""WB 自建总结方法论（vendored）：`roles.py` 与 `summarizer.py` 是 bili-scribe 的逐字节副本。

**为什么要有这一层。** 提示词编排、长素材分块归约、三轮改写（生成→自我批判→修订）
这些是被真实使用打磨出来的方法，产品要求 WB 自带一套、不再运行时依赖上游实现。
所以副本保持与 `bili_scribe/core/roles.py`、`bili_scribe/core/summarizer.py` 逐字节一致
（唯一允许的例外是 `summarizer.py` 开头的 import 段，把包内相对导入换成
 `bili_scribe.*` 与 `from . import roles`），这样两边输出可以对账，漂移由 tests/wb 锁住。

**权威在本文件。** 角色字典、默认角色、退役角色、以及"哪些 key 可以被选中"由这里说了算：

- ``DEFAULT_ROLE`` 是 ``wengao``——``standard`` 已退役，不再当默认。
- ``RETIRED_ROLES`` 里的 key 在**生成入口**被 ``get_role`` 拒绝（抛 ``UnknownRole``），
  适配器把它翻成 ``WBError("VALIDATION", ...)``。
- ``role_label`` 只给**展示**用：历史行里存着的退役角色不能报错，也不能渲染成空白，
  所以要显示成「标准（已停用）」。展示路径永远不许调 ``get_role``。

副本里的 ``roles.ALL_ROLES`` 仍含 ``standard``（那是逐字节副本，不许改），
本层过滤后暴露 7 个方法论角色，顺序与副本一致。

**提示词真源在 txt，不在副本。** ``roles.py`` 里的 system 是为省 token 写的压缩版，
用户手写的 ``resources/prompts/*.txt`` 全文才是定稿的方法论。副本一个字节都不许改，
所以绑定发生在本文件：导入时 ``prompt_source.bind(_roles)`` 把 txt 全文就地写进
角色对象，再把 ``_roles.get_role`` 指向 :func:`_bound_get_role`——副本
``summarizer.py:512`` 调的就是这个被换掉的入口，于是它拿到的 system 是 txt 全文
（外加可选的附加提示词），而 ``:518`` 依旧把 ``OUTPUT_CONTRACT`` 接在最后。
拼接顺序与附加开关的规矩都写在 ``video_workbench/core/prompt_source.py``。
"""

from __future__ import annotations

from typing import Any

from video_workbench.core import prompt_source

from . import roles as _roles
from .summarizer import (
    PreparedMaterial,
    Refinement,
    SummarizeError,
    Summary,
    prepare_material,
    summarize,
)

#: 退役角色：仍然能在历史行里读到，但不可再被选中生成。
RETIRED_ROLES = frozenset({"standard"})

#: 新总结的默认角色。``standard`` 退役后由「文稿」接手默认位。
DEFAULT_ROLE = "wengao"


class UnknownRole(Exception):
    """请求的角色在 WB 不可用：未给出、未知、或已退役。"""


# --------------------------------------------------------------- WB 自有角色
#
# ``shipin``（视频总结）是**权威层自造**的角色，副本 ``roles.py`` 里没有它，
# 副本的 ``ALL_ROLES`` 也永远不该出现它——那是 task-25 的位置约束。
#
# 为什么要它：``standard`` 退役之后，WB 一个单轮角色都不剩，用户每一次总结都要付
# 三轮（生成→自我批判→修订，约 3 倍耗时）。``视频总结.txt`` 那篇本身就没有批判与
# 修订环节（它是「映射→抽取→连通→共振」四段式 + 一份自己的 JSON 骨架），所以把它
# 做成 ``passes=1`` 既不编造方法论，又把"便宜的那档"还给用户。
#
# ``system`` 起手给的是副本自带的 ``COMMON_BOTTOM_LINE``：它是 ``prompt_source``
# 读不到 ``视频总结.txt`` 时的回落位（见 bind 的 missing_source 桶）。**不能留空串**——
# 副本 ``summarizer.py:517`` 见空 system 会切到 ``SYSTEM_PROMPT`` 那条"不套角色方法论"
# 的路，等于悄悄把用户选的方法论换掉了。
SHIPIN = _roles.Role(
    key="shipin",
    name="视频总结",
    tagline="写作四则：锚点撑全文·推理外显·变形替代定义·体感优先（不套自我批判）",
    system=_roles.COMMON_BOTTOM_LINE.strip(),
    critique="",
    revise="",
    note="单轮最快：txt 原文里没有批判/修订环节，这里就不替用户编两轮。",
    passes=1,
)

#: 只在权威层存在的角色。绑定与查表都要认它，副本的表里没有。
_WB_ONLY: tuple = (SHIPIN,)
_WB_ONLY_BY_KEY = {role.key: role for role in _WB_ONLY}

#: 可选择的方法论角色（副本的 ALL_ROLES 去掉退役项，其余顺序不变，最后接权威层自造的）。
ALL_ROLES = tuple(role for role in _roles.ALL_ROLES if role.key not in RETIRED_ROLES) + _WB_ONLY

#: 角色 key 的顺序清单，界面下拉与服务层校验都用它。
ROLE_KEYS = tuple(role.key for role in ALL_ROLES)

#: 副本原本的取角色函数（未知 key 静默回落 standard）。换掉之前先攥住它。
_raw_get_role = _roles.get_role


def _bound_get_role(key: str | None) -> Any:
    """``roles.get_role`` 的替换实现：查表语义不变，system 按当前开关现算。

    副本 ``summarizer.py:512`` 调的就是这个名字（``roles.get_role``），所以注入点只有
    这一个：把 txt 全文与可选附加提示词接上，方法论的编排逻辑仍然一个字都没改。
    查表行为（含"未知回落 standard"）与原函数逐字一致，对账测试才不会被这件事影响。

    唯一的新增：先认权威层自造的 key（``shipin``）——副本的表里没有它，
    不先查就会被原函数静默回落到 ``standard``（退役角色，方法论直接换掉）。
    """
    text = str(key or "").strip()
    role = _WB_ONLY_BY_KEY.get(text)
    if role is None:
        role = _raw_get_role(key)
    return prompt_source.view_for(role)


#: 导入时绑定一次。副本文件不动，改的是运行时对象的状态。
_roles.get_role = _bound_get_role

#: 绑之前先把压缩版记下来：缺文件时要回落的就是它，而绑过之后再抓就抓不到了。
prompt_source.register_wb_only_roles(_WB_ONLY)
prompt_source.capture_baseline(_roles, extra=_WB_ONLY)

#: 绑定结果（哪些角色吃到了 txt 真源、哪些因为缺文件回落），给测试与设置页看。
PROMPT_BIND = prompt_source.bind(_roles, extra=_WB_ONLY)


def describe_roles() -> list[dict[str, Any]]:
    """角色清单（过滤掉退役项）；字段与副本 ``roles.describe_roles()`` 对齐，
    另加三个真源字段：``prompt_source``（文件名或 ``None``）/``prompt_bytes``/``prompt_sha8``。

    ``prompt_source is None`` 表示这个角色**没有 txt 真源**（或文件缺失回落），
    界面必须能看出来——否则用户以为改过的那份提示词其实没生效。
    """
    rows: list[dict[str, Any]] = []
    for role in ALL_ROLES:
        info = prompt_source.info_for(role.key)
        rows.append(
            {
                "key": role.key,
                "name": role.name,
                "tagline": role.tagline,
                "note": role.note,
                "passes": role.passes,
                "prompt_source": info["prompt_source"],
                "prompt_bytes": info["prompt_bytes"],
                "prompt_sha8": info["prompt_sha8"],
            }
        )
    return rows


def get_role(key: str | None) -> Any:
    """取一个**可用**角色；未给出、未知或已退役都抛 ``UnknownRole``。

    上游 ``roles.get_role`` 对未知 key 是静默回落到 ``standard``，这条底线在 WB
    不适用：退役角色一旦能被静默选中，界面就再也回不到三轮改写，所以这里必须硬拒。
    """
    if key is None or not str(key).strip():
        raise UnknownRole(f"没有给出总结角色，请选一个方法论角色（默认 {DEFAULT_ROLE}）")
    text = str(key).strip()
    if text in RETIRED_ROLES:
        raise UnknownRole(f"标准（{text}）角色已停用，请选一个方法论角色")
    for role in ALL_ROLES:
        if role.key == text:
            # 与 _roles.get_role 走同一个 view_for：两条路拿到的 system 必须一致，
            # 否则「适配器看到的角色」和「副本内部看到的角色」会不是同一份提示词。
            return prompt_source.view_for(role)
    raise UnknownRole(f"未知角色：{text}，请选一个方法论角色")


def role_label(key: Any) -> str:
    """角色名，只给展示用，**任何输入都不抛**。

    历史行里存着 ``standard`` 是常态（版本列表、总览、导出都要显示），
    那些路径不能因为退役就报错或留白：退役显示「标准（已停用）」，
    未知显示「未知角色（key）」，空值显示空串。
    """
    text = str(key or "").strip()
    if not text:
        return ""
    if text in RETIRED_ROLES:
        return "标准（已停用）"
    for role in ALL_ROLES:
        if role.key == text:
            return role.name
    return f"未知角色（{text}）"


__all__ = [
    "ALL_ROLES",
    "DEFAULT_ROLE",
    "PreparedMaterial",
    "RETIRED_ROLES",
    "ROLE_KEYS",
    "Refinement",
    "SummarizeError",
    "Summary",
    "UnknownRole",
    "describe_roles",
    "get_role",
    "prepare_material",
    "role_label",
    "summarize",
]
