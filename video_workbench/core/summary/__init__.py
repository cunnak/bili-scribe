"""WB 自建总结方法论（vendored）：`roles.py` 与 `summarizer.py` 是 bili-scribe 的逐字节副本。

**为什么要有这一层。** 提示词编排、长素材分块归约、三轮改写（生成→自我批判→修订）
这些是被真实使用打磨出来的方法，产品要求 WB 自带一套、不再运行时依赖上游实现。
所以副本保持与 `bili_scribe/core/roles.py`、`bili_scribe/core/summarizer.py` 逐字节一致
（唯一允许的例外是 `summarizer.py` 开头的 import 段，把包内相对导入换成
 `bili_scribe.*` 与 `from . import roles`），这样两边输出可以对账，漂移由 tests/wb 锁住。

**权威在本文件。** 角色字典、默认角色、退役角色、以及"哪些 key 可以被选中"由这里说了算：

- ``DEFAULT_ROLE`` 是 ``wengao``——``standard`` 已退役，不再当默认。
- ``RETIRED_ROLES`` 里的 key 在**生成入口**被 ``get_role`` 拒绝（抛 ``UnknownRole``），
  适配器把它翻成 ``WBError("VALIDATION", ...)``。只有从配置或历史行里读出来的退役值
  （``resolve_role`` 的 fallback 那一侧）才回落默认角色并留一句日志——脏数据不该把
  现在的人挡在门外，但用户这次刚提交的退役值仍然是意图，不许悄悄改写。
- ``role_label`` 只给**展示**用：历史行里存着的退役角色不能报错，也不能渲染成空白，
  所以显示成「学术分析（已停用）」这种**按 key 现读名字**的样子。展示路径永远不许调
  ``get_role``；文案里也不许写死某一个角色名——退役名单会长，写死一次就是留一句假话。

副本里的 ``roles.ALL_ROLES`` 仍含 ``standard`` 与 ``xueshu``（那是逐字节副本，不许改），
本层过滤后只暴露 ``ROLE_KEYS`` 里那几个方法论角色：顺序照副本，数量以现读为准。

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
#:
#: 三个 key 的来处不一样，语义是同一个：**新生成拒、历史行显示「（已停用）」、
#: 重生成走 ``resolve_role`` 的 fallback 分支回落默认角色并留痕**。
#: - ``standard``：task-4 退役（它不是方法论，是"没套角色"的那一档）。
#: - ``xueshu``：用户命令「学术分析删掉」（task-31）。定义留在逐字节副本
#:   ``roles.py`` 里不动，只在本层的清单过滤里退出。
#: - ``shipin``：用户命令「合并掉，只留「说人话」」（task-31）。它本权威层自造的
#:   单轮角色，Role 对象随 ``_WB_ONLY`` 一起摘掉；库里可能已经存着 ``role=shipin``
#:   的旧行，所以它**必须**在这份名单里——不在的话那些行会显示成「未知角色」，
#:   把"我们主动删过"这件事伪装成"数据坏了"。
#:
#: 判定只认这一份名单，别在别处再抄一个"已停用集合"。
RETIRED_ROLES = frozenset({"standard", "xueshu", "shipin"})

#: 新总结的默认角色。``standard`` 退役后由「文稿」接手默认位。
DEFAULT_ROLE = "wengao"


class UnknownRole(Exception):
    """请求的角色在 WB 不可用：未给出、未知、或已退役。"""


# --------------------------------------------------------------- WB 自有角色
#
# 这里曾经是 ``shipin``（视频总结）：权威层自造的第 8 个角色，为的是给
# ``视频总结.txt`` 留一档"单轮便宜"的出口。task-31 按用户命令把它合并回 ``renhua``
# ——那份 txt 现在直接绑在「说人话」上（见 ``prompt_source.MAP``），WB 不再自造角色。
#
# 顺带一句被删掉的旧注释里的事实（它错了，别照抄）：``视频总结.txt`` **不是**
# 「映射→抽取→连通→共振」四段式，也**没有**自己的 JSON 骨架——现读全文只有
# 「## 写作原则 + 四条（一个锚点撑全文 / 推理外显 / 变形替代定义 / 帮我找到世界上最
# 适合完成下列任务的人 + 六个提问）+ 体感优先于信息…」。它确实没有批判与修订环节，
# 但"没有批判环节"不再是把方法论降成单轮的理由：合并之后 renhua 照旧走三轮。
#
#: ``_WB_ONLY`` 留成空元组，而不是把这条线拆掉：下面 ``ALL_ROLES`` 的拼接、
#: :func:`_bound_get_role` 的先查表、以及 ``prompt_source`` 的三处绑定都吃它。
#: 清空它是一行；拆掉它要动四处，还会顺手关掉"WB 可以自造角色"这个已经建好的口子。
#: 它同时也是"这个 key 只存在于 WB、副本的表里没有"这条**位置约束**的登记处
#: （task-25 钉过：副本 ``ALL_ROLES`` 里永远不该出现 WB 自造的角色）。
_WB_ONLY: tuple = ()
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

    先查 ``_WB_ONLY_BY_KEY`` 这一条留着：当前它是空的（task-31 之后 WB 不自造角色），
    但它拦的是"将来再有只存在于 WB 的 key 时，被原函数静默回落到 ``standard``"——
    那种回落会把方法论整个换掉，而查表看不出来。
    """
    text = str(key or "").strip()
    role = _WB_ONLY_BY_KEY.get(text)
    if role is None:
        role = _raw_get_role(key)
    return prompt_source.view_for(role)


#: 导入时绑定一次。副本文件不动，改的是运行时对象的状态。
_roles.get_role = _bound_get_role

#: 绑定层自己留了一份「绝不绑」的名单（它不能反过来 import 本层——那是循环导入），
#: 外加 MAP / NO_SOURCE 两份。三份名单靠人手工同步，少一个键的现场是**不报错**的：
#: ``bind()`` 会把那个退役角色当成"无源的公开角色"塞进 ``kept_compressed``——于是它
#: 既不在界面上（清单已过滤）、又会出现在给测试看的绑定结果里，是个既不报错也不留白的
#: 第三种状态。宁可这里导入就炸，也别让它安静地漂着。
def _reconcile_role_lists() -> None:
    """导入期把三份名单对账。每条都是「少写一键就必须红」，写成函数只为把消息说清楚。"""
    known = set(RETIRED_ROLES) | set(ROLE_KEYS)
    owned = set(prompt_source._NEVER_BIND) | set(prompt_source.MAP) | set(prompt_source.NO_SOURCE)
    if known != owned:
        raise RuntimeError(
            "角色名单与提示词真源名单对不上账："
            f"只在名单侧 {sorted(known - owned)}；只在真源侧 {sorted(owned - known)}。"
            "每个 key 必须属于「退役不绑 / 有 txt 真源 / 公开且无源」三者之一。"
        )
    # NO_SOURCE 的口径只有一条：公开名单里没被 MAP 接走的那批。这里用 **MAP 成员关系**
    # （声明层）而不是 ``describe_roles()`` 的 ``prompt_source`` 字段：``info_for()`` 在
    # 文件缺失时也返回 None（prompt_source.py:209），拿它当判据会让"打包漏了 txt"这种
    # 现场把正确的名单判成错的——而漏 txt 是该由 manifest 报的，不是这里的职责。
    unsourced = set(ROLE_KEYS) - set(prompt_source.MAP)
    if set(prompt_source.NO_SOURCE) != unsourced:
        raise RuntimeError(
            "prompt_source.NO_SOURCE 不等于「公开角色里没被 MAP 接走的那批」："
            f"多写 {sorted(set(prompt_source.NO_SOURCE) - unsourced)}，"
            f"漏写 {sorted(unsourced - set(prompt_source.NO_SOURCE))}。"
            "这份清单只表示「现在没有真源」，绑上 txt 的角色必须立刻离开它。"
        )
    # 退役 key 不许出现在 MAP 里：bind() 会跳过 _NEVER_BIND，所以"绑了"等于没绑——
    # 这正是 task-31 那次「txt 挂在退役角色上、用户命令一字没落地而全量测试照样绿」的形状。
    ghost = set(prompt_source.MAP) & set(RETIRED_ROLES)
    if ghost:
        raise RuntimeError(
            f"MAP 里绑了退役角色 {sorted(ghost)}：bind() 会跳过它，这份 txt 实际不会生效。"
        )


#: 必须在 ``bind()`` 之前跑：它读的三份名单都是静态声明，绑定结果不算真源。
_reconcile_role_lists()

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


#: 退役角色的显示名。``standard`` / ``xueshu`` 从副本表里现读——逐字节副本不许改，
#: 名字本来就在那里，第二份真源才是问题。``shipin`` 的 Role 对象随 task-31 一起摘掉了，
#: 这里只能留一个字面量：它取自 ``resources/prompts/视频总结.txt`` 这个**文件名**，
#: 只用于显示，不参与绑定、不参与查表，别把它当成角色的真源。
_RETIRED_EXTRA_NAMES = {"shipin": "视频总结"}


def _retired_name(text: str) -> str:
    """退役角色的中文名；副本表里没有、这里也没登记时退回 key 本身——展示路径绝不抛。"""
    extra = _RETIRED_EXTRA_NAMES.get(text)
    if extra:
        return extra
    for role in _roles.ALL_ROLES:
        if role.key == text:
            return role.name
    return text


def get_role(key: str | None) -> Any:
    """取一个**可用**角色；未给出、未知或已退役都抛 ``UnknownRole``。

    上游 ``roles.get_role`` 对未知 key 是静默回落到 ``standard``，这条底线在 WB
    不适用：退役角色一旦能被静默选中，界面就再也回不到三轮改写，所以这里必须硬拒。
    """
    if key is None or not str(key).strip():
        raise UnknownRole(f"没有给出总结角色，请选一个方法论角色（默认 {DEFAULT_ROLE}）")
    text = str(key).strip()
    if text in RETIRED_ROLES:
        # 名字按 key 现读：这句话以前写死「标准」，xueshu/shipin 退役之后就会把
        # 「学术分析」报成「标准（xueshu）角色已停用」——拒绝是对的，理由是假的。
        raise UnknownRole(f"{_retired_name(text)}（{text}）角色已停用，请选一个方法论角色")
    for role in ALL_ROLES:
        if role.key == text:
            # 与 _roles.get_role 走同一个 view_for：两条路拿到的 system 必须一致，
            # 否则「适配器看到的角色」和「副本内部看到的角色」会不是同一份提示词。
            return prompt_source.view_for(role)
    raise UnknownRole(f"未知角色：{text}，请选一个方法论角色")


def role_label(key: Any) -> str:
    """角色名，只给展示用，**任何输入都不抛**。

    历史行里存着退役角色是常态（版本列表、总览、导出都要显示），那些路径不能因为
    退役就报错或留白：退役显示「〈现读名字〉（已停用）」，未知显示「未知角色（key）」，
    空值显示空串。**已停用**和**不认识**是两件事——前者要换角色，后者要改配置，
    界面不能混着说，所以这里也不能把退役一律报成未知。
    """
    text = str(key or "").strip()
    if not text:
        return ""
    if text in RETIRED_ROLES:
        return f"{_retired_name(text)}（已停用）"
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
