"""角色提示词的真源：包内 txt 全文 + 一个按请求生效的附加开关。

## 为什么要有这一层

产品定的方法论真源是 `resources/prompts/` 下那几篇 txt 原文（用户手写的提示词），
而 `video_workbench/core/summary/roles.py` 是 bili-scribe 的**逐字节副本**，被
`tests/wb/test_summary_drift.py` 按字节钉死，一个字都不能改。副本里的 system 是
压缩版——省 token，但不是用户写的那一份。

两边要同时成立只有一个办法：**副本不动，绑定发生在权威层**。
- `bind()` 把 txt 全文就地写进 `role.system`（Role 是可变 dataclass，字节没变，
  变的是运行时的对象状态）；
- `video_workbench/core/summary/__init__.py` 把 `_roles.get_role` 指向
  `view_for`，副本 `summarizer.py:512` 拿到的就是绑定后的角色。

## 拼接顺序（定死，不许调）

    txt.strip() + "\\n" + extra.strip() + "\\n"   （extra 仅当附加开关为真）
    ——然后由副本 summarizer.py:518 接上 roles.OUTPUT_CONTRACT

输出契约**永远在最后**。理由不是审美：txt 原文里写着「必须打印出每个阶段的完整输出」
「输出 ReAct 执行日志」这类指令，而 `roles.py:52-56` 那段 OUTPUT_CONTRACT 就是专门
写来压住它们的（副本 `summarizer.py:513-518` 的注释也明说了这一点）。契约放前面，
模型就照 txt 的要求把思考过程打进正文，`_extract_json` 当场坏掉。
所以这一层**不拼契约**，只交 system 的本体，顺序由副本那两行决定。

## 附加提示词为什么默认关

`大概率管用 .txt` 是对抗性双角色提示词（要求 `[lbk]Guardian[rbk]` /
`[lbk]Maverick[rbk]` 两段必出），和总结要的 JSON 契约正面冲突。默认开着会拖垮产出，
所以开关交给人：配置 `llm.extra_prompt`（默认 False）。
"""

from __future__ import annotations

import copy as _copy
import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Sequence

#: 覆盖提示词目录的环境变量。与 WB_WEB_ROOT 同风格：**每次现读，不许 import 期缓存**
#: ——测试要在同一个进程里在「包内目录」与「临时目录」之间来回切。
PROMPTS_DIR_ENV = "WB_PROMPTS_DIR"

#: 包内默认目录。形状与 clipper.py:28 / overview.py:31 / translator.py:33 一致，
#: 全仓只有这一个解析器，`WB_PROMPTS_DIR` 是它唯一的覆盖口。
PROMPT_DIR = Path(__file__).resolve().parents[1] / "resources" / "prompts"

#: 角色 key → txt 文件名（逐字，含空格与全角标点，别"顺手规范化"）。
MAP: dict[str, str] = {
    "wengao": "文稿编辑.txt",
    "xiezuo": "写作专家.txt",
    "fenxi": "分析报告.txt",
    "jinglian": "首席思想精炼师：对于各种文本都处理得较为简洁的总结提示词。.txt",
    # WB 权威层自造的角色（副本 roles.py 里没有这一个）：唯一吃 txt 的单轮角色。
    "shipin": "视频总结.txt",
}

#: 没有 txt 真源的角色：继续用副本 roles.py 的压缩版，界面上要能看出来。
NO_SOURCE: tuple[str, ...] = ("shensi", "xueshu", "renhua")

#: 已经在包里、但**暂未绑给任何角色**的文件（等用户拍去向）。
#: 留在清单里是为了让设置页显示"这个文件在，但没接进任何角色"，而不是凭空少一篇。
#: 2026-11-04 起为空：六份 txt 都有了归宿（四份绑方法论角色、一份绑 shipin、
#: 一份是可选附加）。清单与 manifest 的"没接进任何角色"这条通道保留——
#: 以后往 resources/prompts 里再塞一份还没接的，它照样会被列出来。
UNBOUND: tuple[str, ...] = ()

#: 可选附加提示词。文件名 `.txt` 前有一个空格，逐字保留。
EXTRA_FILE = "大概率管用 .txt"

#: 退役角色不绑：它压根不该再生成（副本里它只是历史数据的标签）。
_NEVER_BIND: frozenset[str] = frozenset({"standard"})

_extra_on: ContextVar[bool] = ContextVar("wb_summary_extra_prompt", default=False)

#: (role_key, extra_on, 目录) → 拼好的 system。缓存必须按 extra_on 分键，
#: 否则一开一关两个请求会互相端错菜；`bind()` / `restore()` 会整体作废它。
_VIEW_CACHE: dict[tuple[str, bool, str], str] = {}

#: 副本 roles.py 里每个角色的**压缩版**原文，在第一次 bind 之前抓下来。
#: 缺文件时要回落的就是它——不抓的话，绑过一次之后就没有"压缩版"可回了。
_BASELINE: dict[str, str] = {}

#: 权威层登记的「只存在于 WB 的角色」（task-24 的 shipin：副本的 ALL_ROLES 里不许有它，
#: 位置约束由 task-25 钉住）。登记一次，下面四个函数默认都带上它们。
#: 为什么非要登记而不是靠调用方传 `extra`：少传这一个参数就静默漏掉一个**有 txt 真源**
#: 的角色——它的 system 停在旧值上、missing_source 也不报它，`bound != MAP` 这种不一致
#: 谁都看不见，直到某天真模型端出旧提示词。参数留着当显式入口，登记是兜底。
_WB_ONLY: dict[str, Any] = {}


def register_wb_only_roles(roles: Sequence[Any]) -> None:
    """权威层导入时登记自己造的角色；重复登记按 key 覆盖，不追加。"""
    for role in roles or ():
        key = str(getattr(role, "key", "") or "")
        if key:
            _WB_ONLY[key] = role


def _targets(roles_module: Any, extra: Sequence[Any] = ()) -> list[Any]:
    """这次要处理的角色清单：副本表里的 + 显式传进来的 + 登记过的（按 key 去重）。"""
    out: list[Any] = []
    seen: set[str] = set()
    pool = list(getattr(roles_module, "ALL_ROLES", ())) + list(extra or ()) + list(_WB_ONLY.values())
    for role in pool:
        key = str(getattr(role, "key", "") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(role)
    return out


def capture_baseline(roles_module: Any, extra: Sequence[Any] = ()) -> None:
    """记下各角色当前的 system（＝副本里的压缩版），只记第一次。

    必须在 `bind()` 之前调用：绑过以后 `role.system` 就是 txt 全文了，
    那时再抓会把全文当成"压缩版"存下来，缺文件回落就回错东西。

    `extra` 是**权威层自造的角色**（副本的 `ALL_ROLES` 里不该出现它们，task-25 的
    位置约束钉的就是这件事）：它们同样要有一份"读不到 txt 时回落什么"的底线，
    否则缺文件时 `role.system` 会剩空串，而副本 `summarizer.py:517` 见空 system
    就切到 `SYSTEM_PROMPT` 那条「不套角色方法论」的路——等于悄悄把方法论换了。
    传不传都行：登记过的角色（`register_wb_only_roles`）会自动带上。
    """
    for role in _targets(roles_module, extra):
        key = str(getattr(role, "key", "") or "")
        if key and key not in _BASELINE:
            _BASELINE[key] = str(getattr(role, "system", "") or "")


def baseline_of(role_key: str) -> str:
    """某个角色的压缩版原文（副本里写的那一份）。没抓到就空串。"""
    return _BASELINE.get(str(role_key or ""), "")


def prompt_dir() -> Path:
    """提示词根目录：`WB_PROMPTS_DIR` 优先，否则包内 `resources/prompts`。"""
    raw = str(os.environ.get(PROMPTS_DIR_ENV, "")).strip()
    return Path(raw) if raw else PROMPT_DIR


def path_for(name: str) -> Path:
    return prompt_dir() / name


def exists(name: str) -> bool:
    return path_for(name).is_file()


def load(name: str) -> str:
    """读某个 txt 的**逐字原文**；文件不在就返回空串（缺文件不许抛）。

    除调用方按副本 :518 的既有形状做的 `.strip()` 以外，这里不做任何清洗——
    清洗一次，用户写的提示词就不再是他写的那一份了。
    """
    path = path_for(name)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def sha256_of(name: str) -> str:
    path = path_for(name)
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def byte_size(name: str) -> int:
    path = path_for(name)
    if not path.is_file():
        return 0
    return path.stat().st_size


def source_info(name: str) -> dict[str, Any]:
    """给 RPC / 测试用的一份文件身份：文件名、字节数、sha256、前 8 位。"""
    return {
        "prompt_source": name if exists(name) else None,
        "prompt_bytes": byte_size(name),
        "prompt_sha256": sha256_of(name),
        "prompt_sha8": sha256_of(name)[:8],
    }


def info_for(role_key: str) -> dict[str, Any]:
    """某个角色当前的真源信息；无源角色给 None + 0 + 空哈希（不是空串文件名）。"""
    name = MAP.get(str(role_key or ""))
    if not name or not exists(name):
        return {"prompt_source": None, "prompt_bytes": 0, "prompt_sha256": "", "prompt_sha8": ""}
    return source_info(name)


def manifest() -> list[dict[str, Any]]:
    """包内 6 个 txt 的清单：字节数、sha256、被谁绑走、是不是附加。

    只报**当前目录里实际有什么**，映射关系由 MAP/EXTRA_FILE 决定，不在这里判断可用性
    ——设置页要能看见「这个文件在，但还没接进任何角色」。
    """
    base = prompt_dir()
    names = sorted(p.name for p in base.glob("*.txt") if p.is_file()) if base.is_dir() else []
    bound_by: dict[str, str] = {}
    for key, name in MAP.items():
        bound_by[name] = key
    rows: list[dict[str, Any]] = []
    for name in names:
        rows.append(
            {
                "name": name,
                "bytes": byte_size(name),
                "sha256": sha256_of(name),
                "bound_role": bound_by.get(name, ""),
                "is_extra": name == EXTRA_FILE,
                "in_map": name in bound_by or name == EXTRA_FILE,
            }
        )
    # MAP / EXTRA 点名了但目录里没有的，也要列出来——不然"文件丢了"会被显示成"没有这个文件"。
    for name in list(MAP.values()) + [EXTRA_FILE]:
        if name not in names:
            rows.append(
                {
                    "name": name,
                    "bytes": 0,
                    "sha256": "",
                    "bound_role": bound_by.get(name, ""),
                    "is_extra": name == EXTRA_FILE,
                    "in_map": True,
                }
            )
    return rows


def extra_enabled() -> bool:
    """当前这条执行路径是否要接附加提示词。"""
    return bool(_extra_on.get())


@contextmanager
def use_extra_prompt(enabled: bool) -> Iterator[bool]:
    """在 with 块里按请求打开/关掉附加提示词。

    必须是 contextvar 而不是模块级布尔：batch_worker 并发跑多个 job，一个开附加一个
    不开时两边的 system 都要正确。嵌套时退出内层回到**外层的值**（token 复位），
    不是回到默认值。
    """
    value = bool(enabled)
    token = _extra_on.set(value)
    try:
        yield value
    finally:
        _extra_on.reset(token)


def extra_text() -> str:
    """附加提示词全文；文件不在就空串（缺文件不影响主角色）。"""
    return load(EXTRA_FILE)


def system_for(role_key: str, base_system: str, *, with_extra: bool | None = None) -> str:
    """按当前开关拼出这个角色这一条的 system 本体（不含输出契约）。

    顺序定死：`txt.strip()` (+ ` "\\n" + extra.strip()` 仅当开关为真)。
    附加开关**不许烘死进 bind()**——每次调用现读 contextvar，缓存按
    `(role_key, extra_on, 目录)` 分键。
    """
    key = str(role_key or "")
    want_extra = extra_enabled() if with_extra is None else bool(with_extra)
    cache_key = (key, want_extra, str(prompt_dir()))
    hit = _VIEW_CACHE.get(cache_key)
    if hit is not None:
        return hit
    base = str(base_system or "")
    text = base
    if want_extra and key not in _NEVER_BIND:
        extra = extra_text().strip()
        # extra 自己不接自己；空 system 的角色（standard）保持原样，
        # 让副本 :517 的无角色路径继续走 SYSTEM_PROMPT，不在这里造出一个新方法论。
        if extra and base.strip() and key != "standard":
            text = base.rstrip() + "\n" + extra
    _VIEW_CACHE[cache_key] = text
    return text


def view_for(role: Any) -> Any:
    """把角色包成「当前开关下应该生效的那一份」。

    开关关着时**原对象照回**（不复制、不产生新对象），一条多余分配都没有；
    开关开着时回一份浅拷贝——改的是副本读到的值，不是共享的 Role 本身，
    所以并发的一开一关两个请求不会互相污染。
    """
    key = str(getattr(role, "key", "") or "")
    system_for(key, str(getattr(role, "system", "") or ""))  # 预热缓存，方便看命中率
    if not extra_enabled():
        return role
    composed = system_for(key, str(getattr(role, "system", "") or ""))
    if composed == str(getattr(role, "system", "") or ""):
        return role
    view = _copy.copy(role)
    view.system = composed
    return view


def bind(roles_module: Any, extra: Sequence[Any] = ()) -> dict[str, list[str]]:
    """把 txt 全文绑到角色对象上；返回可断言的三桶清单。

    - ``bound``：MAP 里有源且文件读得到，system 已被换成 txt 全文；
    - ``missing_source``：MAP 里点名了、但当前目录里没有（或读出来是空）——
      这些角色**保持压缩版**，绝不许留空 system（空 system 会让副本 :517 走
      SYSTEM_PROMPT 那条无角色路径，等于悄悄把方法论换掉了）；
    - ``kept_compressed``：无源的角色，加上因缺文件回落的角色。

    幂等：绑两次与绑一次的结果逐字节相同（赋值不是拼接），两个清单内容也相同。
    ``extra`` 同 ``capture_baseline``：权威层自造的角色，副本表里没有它们。
    """
    result: dict[str, list[str]] = {"bound": [], "missing_source": [], "kept_compressed": []}
    for role in _targets(roles_module, extra):
        key = str(getattr(role, "key", "") or "")
        if key in _NEVER_BIND:
            # 退役角色：不绑、也不算"回落"，它不该出现在任何生成路径里。
            if not hasattr(role, "prompt_source"):
                role.prompt_source = None
            continue
        name = MAP.get(key)
        if not name:
            role.prompt_source = None
            result["kept_compressed"].append(key)
            continue
        text = load(name)
        if not text.strip():
            # 缺文件 / 空文件：**回到压缩版**，绝不许留 txt 残留或空 system。
            # 空 system 会让副本 :517 走 SYSTEM_PROMPT 那条无角色路径，等于悄悄换方法论；
            # 而残留上一份 txt 更糟——用户以为回落了，其实吃的是别的目录里的旧文。
            role.prompt_source = None
            base = baseline_of(key)
            if base:
                role.system = base
            result["missing_source"].append(key)
            result["kept_compressed"].append(key)
            continue
        role.system = text
        role.prompt_source = name
        result["bound"].append(key)
    _VIEW_CACHE.clear()
    return result


def snapshot(roles_module: Any, extra: Sequence[Any] = ()) -> dict[str, dict[str, Any]]:
    """取当前每个角色的 system 全文与真源标记，给测试用完还原。"""
    out: dict[str, dict[str, Any]] = {}
    for role in _targets(roles_module, extra):
        key = str(getattr(role, "key", "") or "")
        out[key] = {
            "system": str(getattr(role, "system", "") or ""),
            "prompt_source": getattr(role, "prompt_source", None),
        }
    return out


def restore(roles_module: Any, snap: dict[str, dict[str, Any]], extra: Sequence[Any] = ()) -> None:
    """把 snapshot 拿回去的状态写回来（测试之间不许互相留脏）。"""
    for role in _targets(roles_module, extra):
        key = str(getattr(role, "key", "") or "")
        item = snap.get(key)
        if not item:
            continue
        role.system = item.get("system", "")
        role.prompt_source = item.get("prompt_source", None)
    _VIEW_CACHE.clear()


__all__ = [
    "EXTRA_FILE",
    "MAP",
    "NO_SOURCE",
    "PROMPTS_DIR_ENV",
    "UNBOUND",
    "bind",
    "byte_size",
    "exists",
    "extra_enabled",
    "extra_text",
    "info_for",
    "load",
    "manifest",
    "path_for",
    "prompt_dir",
    "restore",
    "sha256_of",
    "snapshot",
    "source_info",
    "system_for",
    "use_extra_prompt",
    "view_for",
]
