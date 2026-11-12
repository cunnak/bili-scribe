"""配置装载、应用、回写（方案书 6.6）。

**与 bili-scribe 的关系。** 这里不是另写一套配置，而是**继承**它的 dataclass：
``LlmConfig`` / ``CollectConfig`` / ``PublishConfig`` / ``StorageConfig`` / ``UiConfig``
全部来自 ``bili_scribe.config``，本文件只在子类上补新字段。理由很实在——
bili-scribe 的模型客户端、发布器读的是这些属性名，一旦另写一份，
上游哪天改一个字段名，这边会静默读到默认值，表现为"配置填了但没生效"。

**两个文件。** ``config.yaml`` 是两边共用的，只放 bili-scribe 认识的键；Workbench
多出来的配置（五整段 + ui/storage/llm 里那几个键）存在同目录的 ``workbench.json``。
原因是它的 ``section()`` 对未知键**报错**：把 ``llm.tasks`` 写进共享文件，bili-scribe
下次启动就是 ``ValueError``。所以本文件自带一个语义相同的 ``_fill()``（未知键报错、
缺字段落默认值）来读合并后的 dict，写 yaml 时则投影成它的 ``AppConfig`` 再交给它的
模板渲染——形状、注释风格都由上游说了算。
校验规则复用 bili-scribe 导出的常量（``SKIN_KEYS`` / ``THINKING_MODES`` /
``PROXY_SCHEMES``），保证两边判据一致。

**读取顺序**：``config.yaml`` + ``workbench.json`` → 环境变量（仅密钥）→ 内置默认。
**写入**：先写 ``.tmp`` 再 ``os.replace`` 原子替换，替换前备份 ``config.yaml.bak``。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, ClassVar, get_args, get_type_hints

import yaml

from bili_scribe.config import (
    PROXY_SCHEMES,
    SKIN_KEYS,
    THINKING_MODES,
    CollectConfig,
    ServerConfig,
)
from bili_scribe.config import (
    # 这两个在本模块里没有直接消费者，是**有意保留的重导出**：WB 侧代码要读弹幕/画面帧
    # 配置时统一从 video_workbench.config 取，不各自去 import bili_scribe（那会让
    # 「改参考侧」变成一条看不见的耦合）。写成 `X as X` 是 ruff 认的「显式重导出」写法，
    # 不加 noqa、不删名字 —— 删了会让 `from video_workbench.config import FramesConfig`
    # 这种取法在别处断掉（task-71 的 F401 不许用 auto-fix 清这两条）。
    DanmakuConfig as DanmakuConfig,
)
from bili_scribe.config import (
    FramesConfig as FramesConfig,
)
from bili_scribe.config import (
    LlmConfig as _LlmConfig,
)
from bili_scribe.config import (
    PublishConfig as _PublishConfig,
)
from bili_scribe.config import (
    StorageConfig as _StorageConfig,
)
from bili_scribe.config import (
    UiConfig as _UiConfig,
)
from video_workbench.app.errors import WBError

DEFAULT_HOME = Path.home() / ".video-workbench"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.yaml"

# 旧家 = Workbench 曾经和 bili-scribe 同住一个目录、共用同一个 config.yaml 的地方。
# 搬家之后这两个常量**只用来找迁移来源**，不是生效配置：旧家目录下的字节从今往后
# 一个都不许被写（连 `.bak` 都不行）——那是对方程序的配置，写进去等于替用户改了
# bili-scribe 的行为。`~/.bili-scribe` 仍然留着：数据库与 B 站登录凭据在那儿，
# 那是刻意复用，不是没搬干净（见 ``StorageConfig`` 的默认值）。
LEGACY_HOME = Path.home() / ".bili-scribe"
LEGACY_CONFIG_PATH = LEGACY_HOME / "config.yaml"

# Workbench 的配置比 bili-scribe 多得多：``app`` / ``download`` / ``transcribe`` /
# ``subtitle`` / ``clip`` 是整整五段新段，另外 ``ui`` 里加了 ``rail_collapsed`` /
# ``onboarded``、``storage`` 里加了四个目录与 ``retention``、``llm`` 里加了
# ``providers`` / ``active_provider`` / ``tasks``。
# 以前共用一份 config.yaml，而 bili-scribe 的 ``AppConfig.load`` 遇到不认识的键
# **直接报错**而不是忽略：实测只要文件里出现 ``llm.tasks``，它就是
# ``ValueError: LlmConfig 存在未知配置项：tasks`` —— 用户打开 bili-scribe 只会看到
# 一句"起不来"，压根想不到是配置里多了个键。
# 搬家之后这份 yaml 归 Workbench 自己，「怕打挂 bili-scribe」这个理由不再成立，
# 但这次**只搬家、不改存储格式**，所以那条限制原样保留，换成两条还站得住的理由：
#   1) 形状继续由 bili-scribe 的 ``render_config_yaml`` 生成，不用维护两份模板；
#   2) 这份文件仍可被 bili-scribe 的加载器直接读（实测），搬家后不依赖它，
#      但也没必要主动把它变成一个读不了的形状。
# 两边互不影响靠的是**目录不同**，不再靠键名单。
# 两个文件各管一摊：
#   config.yaml    —— 只写 bili-scribe 认识的键，形状由它自己的 render_config_yaml 决定；
#   workbench.json —— 同目录 sidecar，存 Workbench 的全部配置（含扩展键）。
# 读的时候合并：共享键以 yaml 为准（用户可能直接改文件），扩展键只有 sidecar 有。
EXT_NAME = "workbench.json"
LEGACY_LLM_EXT_NAME = "workbench.llm.json"  # v0.8.0 短暂用过的旧 sidecar，只读兼容


def ext_path(config_path: str | os.PathLike[str]) -> Path:
    """sidecar 路径：与 config.yaml 同目录，文件名固定。"""
    raw = os.path.abspath(os.path.expanduser(str(config_path)))
    return Path(raw).with_name(EXT_NAME)


def _read_json(path: Path) -> dict[str, Any]:
    """坏文件当空处理。扩展配置丢了顶多退回单服务商，不该拦启动。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_ext(config_path: str | os.PathLike[str]) -> dict[str, Any]:
    """读 sidecar。新文件优先；只有旧文件时把它的 llm 三键包成 ``llm`` 段。"""
    data = _read_json(ext_path(config_path))
    if data:
        return data
    raw = os.path.abspath(os.path.expanduser(str(config_path)))
    legacy = _read_json(Path(raw).with_name(LEGACY_LLM_EXT_NAME))
    if legacy:
        keys = ("providers", "active_provider", "tasks")
        return {"llm": {k: legacy[k] for k in keys if legacy.get(k) is not None}}
    return {}


def write_ext(config_path: str | os.PathLike[str], data: dict[str, Any]) -> Path:
    """原子写 sidecar（先 .tmp 再 replace，和 config.yaml 一个规矩）。"""
    p = ext_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def _bili_app_cls() -> Any:
    """bili-scribe 的 AppConfig。延迟导入，方便测试替身。"""
    import bili_scribe.config as bc

    return bc.AppConfig


def _fields(cls: Any) -> dict[str, Any]:
    """字段名 → 真实类型。

    bili-scribe 开了 ``from __future__ import annotations``，``f.type`` 是字符串，
    直接 ``is_dataclass(f.type)`` 永远为假，得走 ``get_type_hints`` 才拿到类对象。
    """
    hints = get_type_hints(cls)
    return {f.name: hints.get(f.name, f.type) for f in dataclasses.fields(cls)}


def _project(value: Any, cls: Any) -> Any:
    """投影成 ``cls``：只保留它认识的字段，同名 dataclass 递归投影。

    不写死键名清单——bili-scribe 哪天加字段，这里自动跟上，省得维护两份。
    """
    kwargs: dict[str, Any] = {}
    for name, ftype in _fields(cls).items():
        if not hasattr(value, name):
            continue
        item = getattr(value, name)
        if dataclasses.is_dataclass(item) and dataclasses.is_dataclass(ftype):
            item = _project(item, ftype)
        kwargs[name] = item
    return cls(**kwargs)


def to_bili_config(cfg: "WorkbenchConfig") -> Any:
    """投影成 bili-scribe 的 AppConfig —— 写 config.yaml 时只写这个形状。"""
    return _project(cfg, _bili_app_cls())


def _merge_raw(raw: dict[str, Any], ext: dict[str, Any]) -> dict[str, Any]:
    """yaml 与 sidecar 合并成一份 dict。

    sidecar 是全量快照，yaml 里只有共享键（用户可以手改）——所以共享键以 yaml 为准，
    扩展键只有 sidecar 有。只认 ``_SECTION_NAMES`` 里的段，别的一律丢掉。
    """
    out: dict[str, Any] = {}
    for name in _SECTION_NAMES:
        side = ext.get(name)
        base = dict(side) if isinstance(side, dict) else {}
        top = raw.get(name)
        out[name] = _deep_merge(base, top) if isinstance(top, dict) else base
    return out


def foreign_keys(raw: Any) -> list[str]:
    """``raw`` 里 bili-scribe 认不出的键（段名或段内子键）。空列表 = 形状合法。"""
    if not isinstance(raw, dict):
        return []
    fields = _fields(_bili_app_cls())
    bad: list[str] = []
    for name, value in raw.items():
        ftype = fields.get(name)
        if ftype is None:
            bad.append(str(name))
            continue
        if isinstance(value, dict) and dataclasses.is_dataclass(ftype):
            known = set(_fields(ftype))
            bad += [f"{name}.{k}" for k in sorted(set(value) - known)]
    return bad


def _legacy_foreign_keys() -> list[str]:
    """旧家那份 yaml 里 bili-scribe 不认识的键。读不到/不合法一律当「没有」。

    只读。这个函数存在的意义是：旧家那份文件从今以后没人清了，得有人知道它坏在哪。
    """
    if not LEGACY_CONFIG_PATH.exists():
        return []
    try:
        raw = yaml.safe_load(LEGACY_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # 坏 YAML 不是这次要报的事： bili 自己会报，别在这里抢话
        return []
    if not isinstance(raw, dict):
        return []
    return sorted(str(k) for k in foreign_keys(raw))


def _migration_notice(
    target: Path,
    origin: Path,
    migrated_from: "str | None",
    foreign: list[str],
    merged: dict[str, Any],
    *,
    explicit: bool,
):
    """这次要不要给用户记一条「搬家把 bili-scribe 的坑留在旧家了」的事实。

    为什么必须讲：搬家之前 ``load()`` 里那句 ``if foreign: cfg.save(target)`` 会顺手把
    共享 ``~/.bili-scribe/config.yaml`` 里 bili 不认识的脏键重写干净，老用户其实是靠这个
    才打得开 bili-scribe。搬家之后重写只作用于新家（这是「不许写对方文件」的合同要求，
    正确），于是旧家那份脏文件再没人清 —— 老用户的 bili-scribe 照样起不来，而且不知道
    为什么。代价落在用户身上，至少得让他看得见代价落在哪几个键上。

    两条记录路径：

    1) **本次真的搬了**（``migrated_from`` 非空）：直接用刚读到的 ``foreign``，零额外盘。
    2) **新家早就存在**（本机在这条提示写出来之前就搬完了）：旧家那份如果还留着未知键，
       照样得说 —— 不然产品主线那批用户永远看不到这条提示，而这个任务要修的就是他们。
       代价是每次 ``load()`` 多读一次旧家 yaml，所以只在没有「已确认」标记时才读：
       用户点过「知道了」以后，WB 连读都不读对方的文件。

    只抄键名（``foreign_keys`` 给什么就用什么）：``keys`` 是要印到屏幕上的，值里可能有
    ``api_key``。旧家干净 → 什么都不记，也不提示也不写日志。显式 ``--config`` 指到哪读到
    哪，不替用户发明一条提示。
    「根本没搬」与「搬了但旧家干净」靠 ``cfg.migrated_from`` 区分：那个字段只在真的搬运过
    一次的路径上挂值，而且刻意不持久化。
    """
    existing = (merged.get("ui") or {}).get("migration_notice")
    if isinstance(existing, dict) and existing.get("dismissed"):
        return None  # 说过了，不再重复打扰，也不再碰旧家
    if explicit or target == LEGACY_CONFIG_PATH:
        return None  # 显式指定 / 读的就是旧家：不发明提示
    if migrated_from is not None:
        keys = sorted(str(k) for k in foreign)
        src = str(LEGACY_CONFIG_PATH)
    else:
        keys = _legacy_foreign_keys()
        src = str(LEGACY_CONFIG_PATH)
    if not keys:
        return None
    return {"to": str(target), "from": src, "keys": keys, "dismissed": False}


# 仅密钥可以从环境变量覆盖。把整份配置开放给环境变量会让"到底读到了什么"
# 变得不可复现，而密钥恰恰是最不适合写进文件的那类值。
ENV_API_KEY = "BILI_SCRIBE_API_KEY"

# 服务商协议。现在只实现 OpenAI 兼容那一套（``POST {base_url}/chat/completions``）。
# 只留一个值不是偷懒，是不让界面出现「能选、选了却没接」的下拉——真接第二种协议时
# 再加进来，``validate`` 会自动开始挡别的写法。
PROVIDER_PROTOCOLS = ("openai",)


# =========================================================== 新增段落


@dataclass
class LlmTasksConfig:
    """分任务模型（方案书 6.6）。留空回退到 ``llm.model``。

    为什么按任务分模型而不是全局一个：切片分析要读长上下文、写文案要文风，
    两者的最优模型常常不是同一个。但**默认全部留空**——多一个必填项就多一次
    "新装用户为什么什么都没跑通"的排查。
    """

    summary: str = ""
    collection_overview: str = ""
    translate: str = ""
    clip_analyze: str = ""
    copy: str = ""
    refine: str = ""
    vision: str = ""  # Phase 6，留空回退 model


@dataclass
class LlmProviderConfig:
    """一个服务商 ＝ 一套 ``base_url`` + 一把 ``api_key`` + 它上面能用的模型名。

    为什么要单独一层而不是只有 ``llm.base_url``：文字模型、视觉模型、翻译模型常常
    不在同一家（DeepSeek 写稿 ＋ 别家读画面），而 ``llm.tasks.*`` 只能填模型名，
    打到哪个地址由 ``llm.base_url`` 说了算——多模型多密钥时必然打错门。

    ``protocol`` 现在只有 ``openai``（＝ ``POST {base_url}/chat/completions``）。
    界面上要显示「OpenAI 兼容」，也是将来接第二种协议的挂点；除 openai 外的值在
    ``validate()`` 里直接拒收，不装成"支持但其实没接"。
    """

    id: str = ""
    name: str = ""
    protocol: str = "openai"
    base_url: str = ""
    api_key: str = ""
    models: list[str] = field(default_factory=list)


# ------------------------------------------------------------- 服务商解析
#
# 这一段是「 providers 是唯一真相」的落地点：界面、任务映射、客户端装配都走这里，
# 不要在别处再写一遍 ``cfg.llm.base_url`` 的读法。


def provider_id(name: str, base_url: str) -> str:
    """给服务商生成一个**可读且稳定**的 id。

    优先取域名里的「商号」而不是第一段（``https://api.deepseek.com/v1`` →
    ``deepseek`` 而不是 ``api``）：这个 id 会写进 YAML、也会出现在 ``llm.tasks.*``
    的取值里（``deepseek/deepseek-chat``）。手改配置的人看得懂 ``deepseek``，
    看不懂的 ``a3f1c9`` 只会让人放弃手改。

    跳过 ``api``/``www`` 这类通用前缀和 ``com``/``cn`` 这类尾缀，取剩下第一段；
    整串是 IP、中文主机名、或压根没有地址时才退回名称哈希（哈希只保证稳定，不保证可读）。
    """
    url = (base_url or "").strip()
    host = ""
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    # 本机网关（llm 代理服务、Ollama、LM Studio）没有可读的商号。硬拿 sha1 当 id 的话，
    # ``llm.tasks.summary`` 里会写成 ``p_da8bcfd0/deepseek-chat``，用户看不懂也没法手改。
    if host.lower() in {"localhost", "127.0.0.1", "0.0.0.0"} or host.lower().endswith(".local"):
        return "local"
    generic = {"api", "www", "gateway", "prod", "v1", "open"}
    tails = {"com", "cn", "net", "org", "io", "ai", "dev", "co", "me", "cc", "xyz", "com.cn"}
    for label in (host or "").split("."):
        low = label.strip().lower()
        if not low or low in generic or low in tails:
            continue
        if (
            low.isascii()
            and any("a" <= ch <= "z" for ch in low)
            and low.replace("-", "").replace("_", "").isalnum()
        ):
            return low
        break
    seed = f"{(name or '').strip()}|{url}".encode("utf-8", "replace")
    return "p_" + hashlib.sha1(seed).hexdigest()[:8]


def parse_model_spec(spec: str) -> tuple[str, str]:
    """把 ``llm.tasks.*`` 的取值拆成 ``(服务商 id, 模型名)``。

    两种写法都要能吃，因为它们来自不同年代：
    - ``deepseek-chat``：老写法，只有模型名 → 服务商留空，由调用方按
      「哪家的模型列表里有它」或当前生效服务商决定。
    - ``deepseek/deepseek-chat``：新写法，前缀是服务商 id。

    **不能简单地按第一个 ``/`` 切**：HuggingFace 风格的模型名本身就带斜杠
    （``Qwen/Qwen3-VL-Plus``）。所以只有当前缀**命中已知服务商**时才当服务商用，
    否则整串都是模型名。
    """
    text = (spec or "").strip()
    if not text:
        return "", ""
    if "/" in text:
        head, tail = text.split("/", 1)
        if head and tail:
            return head.strip(), tail.strip()
    return "", text


def find_provider(llm: Any, ref: str) -> Any | None:
    """按 id 或名称找服务商（大小写不敏感，名称允许中文）。"""
    key = (ref or "").strip().lower()
    if not key:
        return None
    for p in getattr(llm, "providers", None) or []:
        if (
            str(getattr(p, "id", "")).lower() == key
            or (getattr(p, "name", "") or "").strip().lower() == key
        ):
            return p
    return None


def resolve_task_model(llm: Any, task: str = "") -> tuple[Any, str]:
    """任务 → ``(服务商, 模型名)``。这是所有 LLM 调用取参数的唯一入口。

    取法从具体到宽：
    1. ``llm.tasks.<task>`` 写了 ``服务商/模型`` → 就用那一家；服务商写错不报错，
       退到下一条（配置手改打错一个字母不该让整个功能红掉，界面另有提示）。
    2. 只写了模型名 → 谁的模型列表里有它就用谁；多家都有就用当前生效的那家。
    3. 什么都没写 → 当前生效服务商 + ``llm.model``。

    **全程 getattr，不直接点字段。** 调用方可能递来 bili-scribe 的 ``LlmConfig``
    （它没有 ``providers``/``active_provider``/``tasks``），那时这里必须安静退化成
    「扁平字段就是唯一服务商」，而不是抛 AttributeError 把总结链路打断——
    症状会是「点总结没反应」，跟真实原因隔着一整条调用链，最难查。
    """
    providers = list(getattr(llm, "providers", None) or [])
    active_ref = str(getattr(llm, "active_provider", "") or "").strip().lower()
    active = next(
        (p for p in providers if str(getattr(p, "id", "")).strip().lower() == active_ref), None
    )
    if active is None and providers:
        active = providers[0]
    default_model = str(getattr(llm, "model", "") or "")
    if active is None:
        active = LlmProviderConfig(
            id="default",
            name="默认服务商",
            base_url=str(getattr(llm, "base_url", "") or ""),
            api_key=str(getattr(llm, "api_key", "") or ""),
            models=[default_model] if default_model else [],
        )
    tasks = getattr(llm, "tasks", None)
    spec = str(getattr(tasks, task, "") or "").strip() if (task and tasks is not None) else ""
    pref, model = parse_model_spec(spec)
    if pref:
        hit = find_provider(llm, pref)
        if hit is not None:
            return hit, model or (hit.models[0] if hit.models else "")
        # 前缀对不上任何服务商。有两种完全不同的写法会长这样：
        # - 手改配置打错服务商名（``deepsekk/deepseek-chat``）→ 模型名还在别家列表里；
        # - OpenRouter / HF 风格的模型 id 自带斜杠（``qwen/qwen3-vl-plus``）→ 斜杠是名字的一部分。
        # 原实现直接退成「active + 斜杠后半段」，第二种就把模型名改坏了：请求发给
        # 网关是个不存在的 id，症状是「总结报错」，跟配置写法隔着一整条链，最难查。
        home = next((p for p in providers if model and model in (p.models or [])), None)
        if home is not None:
            return home, model
        return active, spec
    if model:
        home = next((p for p in providers if model in (p.models or [])), None)
        return (home or active), model
    return active, (default_model or (active.models[0] if active.models else ""))


@dataclass
class RetentionConfig:
    """保留天数。0 = 不自动清理（方案书 6.6）。"""

    source_days: int = 30
    audio_days: int = 3
    frames_days: int = 7
    subtitle_days: int = 0
    export_days: int = 0


@dataclass
class AppSectionConfig:
    language: str = "zh-CN"
    # auto | file | http：媒体给 WebView 的方式。
    # auto 让后端按体积和格式挑（小文件走 file://，大文件或浏览器不支持的编码走本地 http）。
    media_server: str = "auto"


@dataclass
class DownloadConfig:
    quality: str = "1080p"
    codec: str = "h264"
    concurrency: int = 2
    # 凭据来源：复用 bili-scribe 的登录态，而不是让用户再扫一次码。
    cookies_from: str = "bili_scribe"
    # 空段自动省略；单稿 page_title 为空。
    dir_template: str = "{up}/{collection}/{title}/{page_title}"
    # 下载后端：ytdlp 为主，bbdown 是手动兜底（不自动切换）。
    backend: str = "ytdlp"
    bbdown_path: str = ""
    bbdown_args: str = ""


@dataclass
class TranscribeConfig:
    engine: str = "faster-whisper"
    model: str = "large-v3-turbo"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = "auto"
    vad: bool = True
    word_timestamps: bool = True


@dataclass
class TranslateConfig:
    target: str = "auto"
    bilingual: bool = True
    # 双语版式（方案书 §5.11.2 的 layout 取值）。导出双语 ASS 时的默认排布。
    layout: str = "original_top"
    glossary: list[str] = field(default_factory=list)


# 翻译枚举定义在配置层，core/translator.py 反过来从这里取用：
# 配置要校验它们，而配置层不该反向依赖 core。
TRANSLATE_TARGETS = ("auto", "zh", "en", "ja")
BILINGUAL_MODES = ("original_top", "translated_top", "original_only", "translated_only")


@dataclass
class SubtitleConfig:
    default_style: str = "default"
    optimize: bool = True
    translate: TranslateConfig = field(default_factory=TranslateConfig)


@dataclass
class ClipConfig:
    highlight_min_seconds: int = 15
    highlight_max_seconds: int = 90
    segment_min_seconds: int = 120
    segment_max_seconds: int = 480
    candidate_count: int = 12
    # 边界向内/向外吸附的容差。太大会把切点拉到不相关的话上，太小则切在句中。
    snap_tolerance_ms: int = 2000
    vertical_mode: str = "center"
    # P6-02：跟随增强（多主体 + 镜头切换）。关掉就走 P3-06 的"只跟最大那张脸"。
    follow_enhance: bool = True
    # P6-03：BGM 混音默认值。fade_ms 默认 0（不淡），因为老片子的导出结果不该被静默改掉；
    # 界面导出面板预填 200 ms，「0.2 秒可配」在交互层仍然成立。
    bgm_volume: float = 0.25
    bgm_duck: bool = True
    fade_ms: int = 0


# =========================================================== 继承段落


@dataclass
class LlmConfig(_LlmConfig):
    # 总结角色。父类 bili_scribe/config.py:85 的默认值是 "standard"，而 standard
    # 在 Workbench 里已经退役（不套方法论的裸总结），所以这里必须覆写：service.py
    # 六处取角色的顺序都是 ``payload.role or self.cfg.llm.role or DEFAULT_ROLE``，
    # 配置里的 "standard" 排在 DEFAULT_ROLE 前面——没覆写的话，从没选过角色的用户
    # 第一次点「生成」就会被拒，报的还是一个他没见过的角色名。
    # 注意：config.yaml 是两边共用的同一份文件，文件里写了 role 就以文件为准。
    role: str = "wengao"
    # 附加提示词：把 `resources/prompts/大概率管用 .txt` 的全文再接进 system。
    # **默认关**——那篇是对抗性双角色提示词（要求 [lbk]Guardian[rbk] /
    # [lbk]Maverick[rbk] 两段必出），和总结要的 JSON 输出契约正面冲突，默认开着会拖垮
    # 产出。要不要吃它交给人决定；生效方式与拼接顺序见 core/prompt_source.py
    # （按请求走 contextvar，不在绑定时烘死，并发任务互不影响）。
    extra_prompt: bool = False
    # 坏 JSON 抢救：严格档（默认）里一次调用只发一次请求，模型给的东西读不懂就
    # 直接失败——那是与 bili-scribe 逐请求对账的形状，一个字都不能被动。所以这个开关
    # **默认关**，关着等于今天的线上行为。
    # 打开后只放行「修 JSON」这一件事：repair -> 截断续写 -> 正文/时间线分批 -> 严格重问。
    # 两道闸留着（见 core/summarizer.py 的 _json_budget 与 _json_or_retry 的重入护栏）：
    # 补救请求的输出预算仍按配置原样、不抬到 12288 也不翻倍；同一次打磨里阶梯已经
    # 发过请求就不再重入阶梯，所以真实请求数被钉在 1 主 + 若干补救，不会滚成十几发。
    rescue_json: bool = False
    tasks: LlmTasksConfig = field(default_factory=LlmTasksConfig)
    # 服务商列表。空列表在 load()/apply() 里会由扁平字段合成一条（见
    # ``WorkbenchConfig.sync_providers``），所以运行期永远至少有一条。
    # 有些网关（Cloudflare 前置）会按客户端指纹/UA 拦人。留空用内置默认 UA；
    # 填了就用填的（例如整段浏览器 UA），改完要重启才生效。
    user_agent: str = ""
    # 额外请求头。有些网关要求自己的私有头（例如 opencode zen 的
    # ``x-opencode-session``，缺了回 400 MissingSessionID），也有要 ``api-version``
    # 这类 Azure 风格的。放成键值对而不是写死进代码，换一家就不用改程序。
    extra_headers: dict[str, str] = field(default_factory=dict)
    providers: list[LlmProviderConfig] = field(default_factory=list)
    # 当前生效的服务商 id。空 ＝ 第一条。
    active_provider: str = ""


@dataclass
class StorageConfig(_StorageConfig):
    # ``db_path`` / ``key_path`` 刻意**不在这里重新声明**：搬家搬的是配置文件，不是数据
    # 文件。库与 B 站登录凭据继续住在 bili-scribe 的家里（它自己的 ``StorageConfig`` 默认值），
    # 用户已有的项目与 86 条总结历史都在那个库里 —— 顺手搬进新家等于让他的列表凭空变空。
    # 谁要"搬干净"，先回答这两个路径为什么不能跟着 ``DEFAULT_HOME`` 走。
    # 扩展字段全部带默认值：这样即使读到的是一份只有 db_path/key_path 的
    # 旧 bili-scribe 配置文件，也不会因为「缺字段」而装不起来。
    media_root: str = "%APPDATA%/VideoWorkbench/media"
    cache_root: str = "%APPDATA%/VideoWorkbench/cache"
    models_root: str = "%APPDATA%/VideoWorkbench/models"
    ffmpeg_dir: str = "%APPDATA%/VideoWorkbench/bin"
    retention: RetentionConfig = field(default_factory=RetentionConfig)


@dataclass
class PublishConfig(_PublishConfig):
    # 摘要字段已下线（2026-11-07）：笔记不再带摘要，界面上也没有这个输入框。
    # 这个键留着只为兼容老配置文件（删掉会让 _fill 对未知键抛 VALIDATION），
    # 不再参与任何计算，也不在设置页暴露。
    max_summary_chars: int = 400
    # 发布笔记时把摘要预览作为【评论卡】同步到视频评论区；默认开启。
    auto_comment: bool = True


@dataclass
class UiConfig(_UiConfig):
    rail_collapsed: bool = False
    # 首次启动向导看过没有。默认 False：新装的机器第一次开窗口就把五件事摆出来，
    # 用户跳过也算看过（否则每次启动都弹，会变成骚扰）。
    onboarded: bool = False
    # 搬家一次性提示。**默认 None 才是默认态**：没有要提示的事。
    # 里面只放四样事实——搬到哪（``to``）、从哪搬来（``from``）、旧家里 bili-scribe
    # 不认识的**键名**清单（``keys``）、以及看过没有（``dismissed``）。
    # **绝不放值**：那些键的值里可能有 ``api_key``，弹给用户看等于把密钥印在屏幕上。
    # 为什么声明成 dataclass 字段而不是往 sidecar 里塞一个裸键：``_fill`` 对未知键是
    # 直接抛 ``VALIDATION`` 而不是忽略（那是它该有的行为，``max_frame`` 少个 s 就靠它
    # 报错），塞一个没声明的键等于让所有用户的 WB 起不来。``rail_collapsed`` /
    # ``onboarded`` 就是这个先例的正确形态。
    # 它也不进 ``config.yaml``：``render_yaml`` 按 bili 的 ``AppConfig`` 投影，扩展键
    # 天生被滤掉 —— 所以 bili-scribe 永远不会因为这个键再报一次「未知配置项」。
    migration_notice: dict | None = None


# =========================================================== 根对象


@dataclass
class WorkbenchConfig:
    """工作台的完整配置。字段顺序即 YAML 渲染顺序，刻意把新增的放前面。"""

    app: AppSectionConfig = field(default_factory=AppSectionConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    subtitle: SubtitleConfig = field(default_factory=SubtitleConfig)
    clip: ClipConfig = field(default_factory=ClipConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)

    source_path: str | None = field(default=None, repr=False, compare=False)

    # 搬家标记：本次 load 是不是从旧家（``~/.bili-scribe/config.yaml``）搬过来的，
    # 是就记那份旧文件的绝对路径，否则 ``None``。刻意写成 ``ClassVar`` —— 它不是
    # dataclass 字段，所以 ``dataclasses.asdict``（= ``workbench.json`` 的内容）、
    # ``==`` 比较、设置页视图都不会带上它；它只是给日志和验收用例一个可断言的形式。
    # 类级默认 ``None`` 保证「不是通过 load() 造出来的配置对象」也能安全读这个属性。
    migrated_from: ClassVar[str | None] = None

    # 只报告、不修：本次 load 读到「bili-scribe 不认识的键」时，把这些**键名**记在这里，
    # 平时是空列表。和 ``migrated_from`` 同一个道理写成 ``ClassVar``：它不是字段，
    # ``asdict`` / ``workbench.json`` / ``==`` / 设置页视图都不带它，**只放键名不放值**
    # （那些键里就有 ``llm.api_key`` 这种，界面上打出来等于把密钥写进日志）。
    # 以前这条会顺手重写用户的配置文件，现在不了 —— 见 ``load()`` 里那段注释。
    foreign_keys: ClassVar[list[str]] = []

    # ------------------------------------------------------- 服务商归一

    def override_llm(self, *, base_url: str = "", api_key: str = "", model: str = "") -> None:
        """把「当前服务商」指向给定端点（只改内存，不写盘；给验收脚本与测试用）。

        多服务商之后 ``llm.base_url/api_key/model`` 只是当前卡的镜像：直接赋值
        改的是镜像，``resolve_task_model`` 读的还是卡里的老值——请求就会打到旧
        地址。这个方法同时改镜像与卡，两条路才一致。
        """
        llm = self.llm
        if base_url:
            llm.base_url = base_url
        if api_key:
            llm.api_key = api_key
        if model:
            llm.model = model
        card = find_provider(llm, llm.active_provider)
        if card is None:
            new_id = provider_id("accept", llm.base_url)
            llm.providers = list(llm.providers) + [
                LlmProviderConfig(
                    id=new_id,
                    name="验收端点",
                    protocol="openai",
                    base_url=llm.base_url,
                    api_key=llm.api_key,
                    models=[llm.model] if llm.model else [],
                )
            ]
            llm.active_provider = new_id
            return
        card.base_url = llm.base_url
        card.api_key = llm.api_key
        models = list(card.models or [])
        if llm.model and llm.model not in models:
            models.insert(0, llm.model)
        card.models = models

    def sync_providers(self) -> None:
        """把 ``llm.providers`` 与扁平的 ``base_url/api_key/model`` 对齐。

        **谁是真相：providers。** 扁平字段是它的一面镜像，存在的唯一理由是
        bili-scribe 的模型客户端、发布器读的就是 ``cfg.llm.base_url/api_key/model``
        （边界规则不许改它），而 ``config.yaml`` 是和它共用的同一份文件。

        三种情况：
        1. providers 为空（老配置、或 bili-scribe 自己写的配置）→ 用扁平字段合成
           一条「默认服务商」。不这么做的话设置页会显示空列表，用户一改就把
           原来能用的密钥丢掉。
        2. providers 有但 ``active_provider`` 对不上（手改配置、删了当前条）→ 落回第一条，
           不报错：让界面显示「用的是第 1 家」比启动就弹一个红字有用。
        3. providers 有且对得上 → 把这家地址与密钥抄回扁平字段；``model`` 不在这家的
           模型列表里时改用它的第一个模型（否则「切了服务商、还在打上一家的模型名」）。

        这个函数必须在 ``load()`` 与 ``apply()`` 里都跑，且跑在 ``validate()`` 之前：
        校验要看到的是已经对齐的结果，而不是两份可能互相矛盾的数据。
        """
        llm = self.llm
        providers = [p for p in (llm.providers or []) if isinstance(p, LlmProviderConfig)]
        if not providers:
            providers = [
                LlmProviderConfig(
                    id=provider_id("默认服务商", llm.base_url),
                    name="默认服务商",
                    base_url=llm.base_url,
                    api_key=llm.api_key,
                    models=[llm.model] if llm.model else [],
                )
            ]
        else:
            # 补 id：手写的 YAML 里通常没有 id，而界面要靠它定位卡片。
            seen: set[str] = set()
            for p in providers:
                if not p.id:
                    p.id = provider_id(p.name or "服务商", p.base_url)
                n = 1
                base = p.id
                while p.id in seen:
                    n += 1
                    p.id = f"{base}-{n}"
                seen.add(p.id)
        llm.providers = providers
        active = next((p for p in providers if p.id == llm.active_provider), None) or providers[0]
        llm.active_provider = active.id
        llm.base_url = active.base_url
        llm.api_key = active.api_key
        # 默认模型必须落在当前这家名下：否则界面切了服务商、实际请求还在打上一家的模型。
        # 这家一个模型都没填时保留原值（可能是老配置，不能凭空把模型清空）。
        if active.models and llm.model not in active.models:
            llm.model = active.models[0]

    # ------------------------------------------------------- 派生路径
    #
    # 所有路径字段统一「先 expandvars 再 expanduser 再转绝对」。
    # 顺序不能反：`%APPDATA%/x` 先 expanduser 不会有变化，但 `~/x` 先 expandvars
    # 在某些 shell 里会被误当变量；先拆环境变量再拆家目录是唯一稳的顺序。

    @staticmethod
    def _abs(raw: str) -> str:
        text = os.path.expandvars(str(raw))
        text = os.path.expanduser(text)
        return os.path.abspath(text)

    @property
    def db_path(self) -> str:
        return self._abs(self.storage.db_path)

    @property
    def key_path(self) -> str:
        return self._abs(self.storage.key_path)

    @property
    def media_root(self) -> str:
        return self._abs(self.storage.media_root)

    @property
    def cache_root(self) -> str:
        return self._abs(self.storage.cache_root)

    @property
    def models_root(self) -> str:
        return self._abs(self.storage.models_root)

    @property
    def ffmpeg_dir(self) -> str:
        return self._abs(self.storage.ffmpeg_dir)

    @property
    def notes_dir(self) -> str:
        """预读笔记缓存目录——与 bili-scribe 用同一位置，复用它的缓存。"""
        return str(Path(self.db_path).parent / "notes-cache")

    # ------------------------------------------------------- 视图

    def public_view(self) -> dict[str, Any]:
        """暴露给主界面的子集，不含密钥。主界面每次加载都会打这个接口。"""
        data = dataclasses.asdict(self)
        data.pop("source_path", None)
        data["llm"].pop("api_key", None)
        # 服务商列表里的密钥同样要抹：``llm.api_key`` 抹了而 ``providers[].api_key``
        # 没抹，等于白抹——主界面谁都拿得到 JSON。这里只留尾 4 位，够界面显示
        # 「尾号 2E55」又不够拼出完整密钥。
        data["llm"]["providers"] = [
            {**p, "api_key": "", "key_tail": (p.get("api_key") or "")[-4:]}
            for p in data["llm"].get("providers", [])
        ]
        data["llm"]["configured"] = bool(self.llm.api_key)
        return data

    def settings_view(self) -> dict[str, Any]:
        """设置页面用的完整视图，含明文 api_key。调用点只有一个，别扩大。"""
        data = dataclasses.asdict(self)
        data.pop("source_path", None)
        return data

    def to_app_config(self):
        """给 bili-scribe 用的 ``AppConfig``。

        传入的是**同一批对象引用**，不是拷贝：bili-scribe 那边一旦改配置，
        这边读到的就是新值，不会出现"两个配置对象各说各话"。
        """
        from bili_scribe.config import AppConfig as _AppConfig

        return _AppConfig(
            server=self.server,
            llm=self.llm,
            collect=self.collect,
            publish=self.publish,
            storage=self.storage,
            ui=self.ui,
            source_path=self.source_path,
        )

    # ------------------------------------------------------- 装载

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "WorkbenchConfig":
        """装载。文件不存在时用默认值（首次启动不该因为没配置而起不来）。"""
        target = Path(cls._abs(str(path))) if path is not None else DEFAULT_CONFIG_PATH
        # 搬家：新家还没落过文件、旧家有 → 读旧家那一对（config.yaml + workbench.json，
        # 里面再回落旧的 workbench.llm.json），然后落到新家。判「旧家有没有东西」要两个
        # 都看：只有 sidecar 没有 yaml 也是有人在用的配置，只认 yaml 就等于把他的服务商
        # 丢了。显式 --config（``path is not None``）**绝不触发迁移** —— 指到哪读到哪是
        # 用户的意图，不能被一条"帮你搬家"的逻辑改写。两边都没有 → 内置默认，照旧。
        origin = target
        migrated_from: str | None = None
        if (
            path is None
            and not target.exists()
            and (LEGACY_CONFIG_PATH.exists() or ext_path(LEGACY_CONFIG_PATH).exists())
        ):
            origin = LEGACY_CONFIG_PATH
            migrated_from = str(LEGACY_CONFIG_PATH)
        raw: dict[str, Any] = {}
        if origin.exists():
            try:
                with open(origin, "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
            except yaml.YAMLError as exc:
                # 坏 YAML 必须在这里停下来。放过去的话 PyYAML 的报错会在启动链路里
                # 变成一句英文栈信息，用户看到的是"程序打不开"，压根想不到是缩进问题。
                raise WBError(
                    "VALIDATION",
                    "config.yaml 解析失败：检查缩进与冒号（YAML 不吃 Tab）",
                    f"{origin}: {exc}",
                ) from exc
            if not isinstance(raw, dict):
                raise WBError("VALIDATION", "config.yaml 顶层必须是映射结构", str(origin))
        merged = _merge_raw(raw, read_ext(origin))
        # 老版本把扩展键写进了共享 yaml：记下来，构造完立刻按新形状重写一次。
        foreign = foreign_keys(raw)
        notice = _migration_notice(
            target, origin, migrated_from, foreign, merged, explicit=path is not None
        )
        if notice is not None:
            merged.setdefault("ui", {})["migration_notice"] = notice
        cfg = cls(
            app=_fill(AppSectionConfig, merged.get("app"), "app"),
            ui=_fill(UiConfig, merged.get("ui"), "ui"),
            server=_fill(ServerConfig, merged.get("server"), "server"),
            llm=_fill(LlmConfig, merged.get("llm"), "llm"),
            storage=_fill(StorageConfig, merged.get("storage"), "storage"),
            collect=_fill(CollectConfig, merged.get("collect"), "collect"),
            download=_fill(DownloadConfig, merged.get("download"), "download"),
            transcribe=_fill(TranscribeConfig, merged.get("transcribe"), "transcribe"),
            subtitle=_fill(SubtitleConfig, merged.get("subtitle"), "subtitle"),
            clip=_fill(ClipConfig, merged.get("clip"), "clip"),
            publish=_fill(PublishConfig, merged.get("publish"), "publish"),
            source_path=str(target),
        )
        # 服务商先归一，再谈环境变量：反过来的话 ``sync_providers`` 会把文件里那把密钥
        # 抄回扁平字段，环境变量覆盖就被吃掉了（表现为"设了 env 还是 401"）。
        cfg.sync_providers()
        # 迁移证据：本次真的搬了才挂，值就是那份旧文件的绝对路径；没搬就是 ``None``。
        # 刻意**不持久化** —— 新家一旦有文件就不再满足搬运条件，第二次 load 必须报
        # ``None``，否则日志与界面每次启动都会骗用户一次「刚从旧家搬过来」。
        # 挂在实例上而不是 dataclass 字段：``asdict`` / ``==`` / 落盘内容都不带它。
        cfg.migrated_from = migrated_from
        if migrated_from is not None:
            # 搬家落地：**只写新家**。旧家一个字节都不写，连 ``.bak`` 都不产生 ——
            # 那是 bili-scribe 的文件，替它备份一次就等于动了对方目录。
            # 逐字段忠实搬运：值一个都不改、不"顺手修"。旧家那份 ``role=standard`` /
            # ``max_tokens=4096`` 确实是坏的，但修值是另一件事，掺进迁移里
            # 迁移就不可预测了（用户/同事得能推出搬完之后是什么）。
            # 也不走下面的 foreign 重写：``render_yaml`` 只渲染 bili-scribe 认识的键，
            # 写出来的新家文件天生没有脏键，再重写一次纯属多一趟盘。
            try:
                cfg.save()
            except OSError:
                pass  # 新家暂时写不进去不影响本次启动，下次保存会补
        elif foreign and target != LEGACY_CONFIG_PATH:
            # 文件里还留着 bili-scribe 不认识的键（老版本写进去过）。
            # **这里以前会 `cfg.save(target)` 原地重写用户的配置文件**，2026-11-04 撤掉，
            # 理由不是风格问题，是两条实测：
            #   1. 一次纯读的角色查询（`-m video_workbench.cli roles --config …`）在用户
            #      没点任何保存的情况下，把 395 B 的 config.yaml 重写成 4519 B、留下
            #      `config.yaml.bak` 与 `workbench.json`。只读路由写用户的盘，出事时用户
            #      手里连"我原本那份"都没有。
            #   2. 这个重写**修不了它想修的那个病**：脏键来自上一版本 WB 自己的写入，
            #      bili-scribe 读的是 `~/.bili-scribe/`，不是这里；重写只是把脏键从
            #      WB 的文件里抹掉，用户真正会撞到的那一句 ValueError 在旧家。
            # 现在的规则：**除「搬家落新家」与「显式保存」以外，`load()` 不写盘。**
            # 这里只把事实记下来交给真正有日志的地方（`WorkbenchService` 启动时打一行、
            # `app.info` 里能看见），由人决定要不要在设置页里点保存。
            # ``target != LEGACY_CONFIG_PATH`` 这半仍然要留着：旧家是 bili-scribe 的文件，
            # 读可以、写不行，报告也一样不能替对方改盘 —— 只是这次连新家的写都省了。
            cfg.foreign_keys = sorted(str(key) for key in foreign)
        # 密钥允许被环境变量覆盖：这样容器/脚本里起服务不用把密钥落盘。
        # 生效服务商也要一起改，否则界面显示的还是文件里那把。
        if os.environ.get(ENV_API_KEY):
            key = os.environ[ENV_API_KEY]
            cfg.llm.api_key = key
            active = find_provider(cfg.llm, cfg.llm.active_provider)
            if active is not None:
                active.api_key = key
        validate(cfg)
        return cfg

    # ------------------------------------------------------- 应用

    def apply(self, values: dict[str, Any]) -> list[str]:
        """把设置页提交的值原地写回。返回「需要重启才生效」的段落名。

        原地改而不是替换对象：``AppState``、采集器、发布器都持有同一个引用，
        换成新对象会让它们继续读旧值——症状是"保存了但没生效"。
        这一步是**全有或全无**：任一段校验失败就整体回滚，否则界面会变成
        "报错说没改成、其实改了一半"。

        **先深合并再构造**：设置页只提交改过的那几项（``buildPatch`` 的粒度是叶子），
        而 ``_fill`` 对「没出现的键」一律落默认值。两者直接相碰的结果是
        「我明明只改了温度，api_key 却没了」——所以每个段落都要先拿旧值垫底。
        """
        snapshot = dataclasses.asdict(self)
        snapshot.pop("source_path", None)
        try:
            for name in _SECTION_NAMES:
                incoming = values.get(name)
                if not isinstance(incoming, dict):
                    continue
                current = getattr(self, name)
                merged = _deep_merge(snapshot.get(name) or {}, incoming)
                setattr(self, name, _fill(type(current), merged, name))
            self.sync_providers()
            validate(self)
        except Exception:
            for name in _SECTION_NAMES:
                setattr(
                    self, name, _fill(type(getattr(self, name)), snapshot.get(name) or {}, name)
                )
            raise
        return [n for n in ("server",) if isinstance(values.get(n), dict)]

    # ------------------------------------------------------- 回写

    def save(self, path: str | os.PathLike | None = None) -> dict[str, Any]:
        target_raw = path or self.source_path
        if not target_raw:
            raise WBError("VALIDATION", "不知道要写到哪里：配置没有 source_path")
        target = Path(self._abs(str(target_raw)))
        target.parent.mkdir(parents=True, exist_ok=True)
        # 写盘前归一一次：调用方可能只改了扁平字段（脚本、测试、命令行都这么干），
        # 不归一的话 sidecar 里那张「当前服务商」的卡会是空的，重启后密钥就丢了。
        self.sync_providers()

        backup: Path | None = None
        if target.exists():
            backup = target.with_name(target.name + ".bak")
            shutil.copy2(target, backup)

        tmp = target.with_name(target.name + ".tmp")
        # 显式 newline="\n"：Windows 默认会把 \n 翻成 \r\n，
        # 于是「写的字节数」和「磁盘上的字节数」对不上，行尾也和示例文件不一致。
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_yaml(self))
        os.replace(tmp, target)
        self.source_path = str(target)
        # 全量配置落 sidecar：yaml 那边只有 bili-scribe 认识的键。
        data = dataclasses.asdict(self)
        data.pop("source_path", None)
        ext = write_ext(target, data)
        return {
            "path": str(target),
            "ext": str(ext),
            "backup": str(backup) if backup else None,
            "bytes": target.stat().st_size,
        }


_SECTION_NAMES = (
    "app",
    "ui",
    "server",
    "llm",
    "storage",
    "collect",
    "download",
    "transcribe",
    "subtitle",
    "clip",
    "publish",
)


# =========================================================== 构造与校验


def _fill(cls: type, values: Any, path: str = ""):
    """按 dataclass 结构递归构造，**未知键报错**。

    未知键必须报错而不是忽略：``max_frame`` 少写一个 ``s`` 会被静默当成默认值，
    使用者看到的现象是"我把上限改成 40 了怎么还是 12 帧"——这类问题最难查。

    这里用 ``get_type_hints`` 而不是 ``f.type``：模块开了
    ``from __future__ import annotations``，``f.type`` 是字符串，
    反射拿不到真实的嵌套类型，会把整段配置静默退化成默认值。
    """
    label = path or cls.__name__
    if values is None:
        return cls()
    if not isinstance(values, dict):
        raise WBError("VALIDATION", f"{label} 必须是映射结构", repr(values))

    known = {f.name for f in dataclass_fields(cls)}
    unknown = set(values) - known
    if unknown:
        raise WBError(
            "VALIDATION",
            f"{label} 存在未知配置项：{', '.join(sorted(unknown))}",
            f"可用键：{', '.join(sorted(known))}",
        )

    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for key, value in values.items():
        hint = hints.get(key)
        child = f"{path}.{key}" if path else key
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            kwargs[key] = _fill(hint, value, child)
        elif hint is dict or str(hint).startswith("dict["):
            if value is None:
                kwargs[key] = {}
            elif isinstance(value, dict):
                # 头名/值都是自由文本：统一转 str，省掉「填了数字 5 就报类型错」。
                kwargs[key] = {str(k): str(v) for k, v in value.items()}
            else:
                raise WBError("VALIDATION", f"{child} 必须是映射结构", repr(value))
        elif hint is list or str(hint).startswith("list["):
            if value is None:
                kwargs[key] = []
            elif isinstance(value, (list, tuple)):
                # 列表元素是 dataclass 时必须逐个构造，不能原样塞 dict：
                # 塞进去的话 ``p.base_url`` 取不到（dict 没有属性），而
                # ``dataclasses.asdict`` 也不会把它转成界面要的形状——
                # 表现为「配置里写了服务商，设置页一条都不显示」。
                elem = next(iter(get_args(hint) or ()), None)
                if isinstance(elem, type) and dataclasses.is_dataclass(elem):
                    kwargs[key] = [
                        _fill(elem, item, f"{child}[{i}]") for i, item in enumerate(value)
                    ]
                else:
                    kwargs[key] = list(value)
            else:
                raise WBError("VALIDATION", f"{child} 必须是列表", repr(value))
        else:
            kwargs[key] = value
    return cls(**kwargs)


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个映射：**字典逐层合，列表整份换**。

    列表为什么不合：``llm.providers``、``transcribe.language`` 候选这类数组，
    「改第 2 条」在补丁里没法表达成``[1]``以外的键；界面本来就是拿整份数组回来覆盖，
    合并只会造出「删掉的条目又活了」这种更难查的现象。
    """
    out = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _looks_like_endpoint(url: str) -> bool:
    """服务商地址的形状检查：**协议 + 主机**有就行，路径随便。

    故意不做的事：不校验有没有 ``/v1``、不校验能不能连通。用户接的是自建网关、
    azure 风格路径（``/openai/deployments/xxx``）都合法；连通性是 ``llm.test``
    按钮该管的事，写在启动校验里只会把能用的配置拦在门外。
    """
    text = (url or "").strip()
    if "://" not in text:
        return False
    scheme, rest = text.split("://", 1)
    return scheme.lower() in ("http", "https") and bool(rest.split("/", 1)[0].strip())


def _enum(actual: Any, allowed: tuple[str, ...], label: str) -> None:
    if actual not in allowed:
        raise WBError(
            "VALIDATION",
            f"{label} 只能是 {' / '.join(allowed)}，收到：{actual!r}",
        )


def _range(actual: int | float, low: float, high: float | None, label: str) -> None:
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        raise WBError("VALIDATION", f"{label} 必须是数字，收到：{actual!r}")
    if actual < low or (high is not None and actual > high):
        bound = f"{low}–{high}" if high is not None else f"≥ {low}"
        raise WBError("VALIDATION", f"{label} 必须 {bound}，收到：{actual}")


def validate(cfg: WorkbenchConfig) -> None:
    """集中校验。

    **只挡"写错了但看起来正常"的那一类**：枚举拼错、范围越界、代理漏协议。
    这些的共同点是不会当场报错，而是延后到真正干活时才炸，且现场信息与真原因
    相距很远（"模型服务挂了"其实是代理写了 `127.0.0.1:7897` 少了协议）。
    """
    _enum(cfg.app.media_server, ("auto", "file", "http"), "app.media_server")
    _enum(cfg.ui.skin, SKIN_KEYS, "ui.skin")
    _enum(cfg.llm.thinking, THINKING_MODES, "llm.thinking")

    # 服务商：只挡"配了但一眼会打错门"的。地址留空不算错——新装的机器本来就没有，
    # 首次请求时由 ``ChatClient.require()`` 报 LLM_NOT_CONFIGURED，那才是合适的位置。
    # 额外请求头：不能含换行，否则等于允许注入别的头。
    for head_name, head_val in (getattr(cfg.llm, "extra_headers", None) or {}).items():
        for what, text in (("头名", head_name), ("值", head_val)):
            if chr(10) in str(text) or chr(13) in str(text):
                raise WBError("VALIDATION", f"llm.extra_headers 的{what}不能含换行", str(text))

    for i, p in enumerate(cfg.llm.providers or []):
        label = f"llm.providers[{i}]"
        _enum(p.protocol, PROVIDER_PROTOCOLS, f"{label}.protocol")
        if p.base_url and not _looks_like_endpoint(p.base_url):
            raise WBError(
                "VALIDATION",
                f"{label}.base_url 必须是 http(s) 地址（到 /v1 这一层，不含 /chat/completions）",
                f"收到：{p.base_url!r}",
            )
        # 这里**不校验模型名里的斜杠**。真实网关的模型 id 大量自带斜杠
        # （magpie 的 ``auto/hy4-preview``、OpenRouter 的 ``anthropic/claude-3.5``、
        # HF 的 ``Qwen/Qwen3-VL-Plus``），把它们判成「写错了」会让正常配置根本存不下来。
        # 前缀消歧由 ``parse_model_spec`` 负责：只有前缀命中已知服务商 id 才当服务商用。
    if cfg.llm.active_provider and find_provider(cfg.llm, cfg.llm.active_provider) is None:
        raise WBError(
            "VALIDATION",
            "llm.active_provider 指向一个不存在的服务商",
            f"收到：{cfg.llm.active_provider!r}；可用 id："
            f"{', '.join(p.id for p in cfg.llm.providers or []) or '（空）'}",
        )

    if cfg.llm.proxy:
        scheme = cfg.llm.proxy.split("://", 1)[0].lower()
        if "://" not in cfg.llm.proxy or scheme not in PROXY_SCHEMES:
            raise WBError(
                "VALIDATION",
                f"llm.proxy 必须是带协议的完整地址（如 http://127.0.0.1:7897），"
                f"收到：{cfg.llm.proxy!r}",
            )

    _range(cfg.collect.frames.max_frames, 0, None, "collect.frames.max_frames")
    _range(cfg.collect.frames.quality, 1, 100, "collect.frames.quality")

    _enum(cfg.download.quality, ("4k", "1080p", "720p", "480p"), "download.quality")
    _enum(cfg.download.codec, ("h264", "hevc", "av1"), "download.codec")
    _range(cfg.download.concurrency, 1, 8, "download.concurrency")
    _enum(cfg.download.backend, ("ytdlp", "bbdown"), "download.backend")

    # 翻译的两个枚举必须在这里挡：拼错 target 的后果是"界面显示已翻译、
    # 导出却是空文件"，拼错 layout 的后果是双语行序反了——都不报错，只出怪结果。
    _enum(cfg.subtitle.translate.target, TRANSLATE_TARGETS, "subtitle.translate.target")
    _enum(cfg.subtitle.translate.layout, BILINGUAL_MODES, "subtitle.translate.layout")

    _enum(
        cfg.transcribe.engine,
        ("faster-whisper",),
        "transcribe.engine",
    )
    _enum(
        cfg.transcribe.device,
        ("auto", "cuda", "cpu"),
        "transcribe.device",
    )
    _enum(
        cfg.transcribe.compute_type,
        ("auto", "float16", "int8_float16", "int8", "float32"),
        "transcribe.compute_type",
    )

    _range(cfg.clip.highlight_min_seconds, 1, None, "clip.highlight_min_seconds")
    _range(cfg.clip.highlight_max_seconds, 1, None, "clip.highlight_max_seconds")
    if cfg.clip.highlight_min_seconds > cfg.clip.highlight_max_seconds:
        raise WBError(
            "VALIDATION",
            "clip.highlight_min_seconds 不能大于 clip.highlight_max_seconds",
            f"{cfg.clip.highlight_min_seconds} > {cfg.clip.highlight_max_seconds}",
        )
    _range(cfg.clip.segment_min_seconds, 1, None, "clip.segment_min_seconds")
    _range(cfg.clip.segment_max_seconds, 1, None, "clip.segment_max_seconds")
    if cfg.clip.segment_min_seconds > cfg.clip.segment_max_seconds:
        raise WBError(
            "VALIDATION",
            "clip.segment_min_seconds 不能大于 clip.segment_max_seconds",
            f"{cfg.clip.segment_min_seconds} > {cfg.clip.segment_max_seconds}",
        )
    _range(cfg.clip.candidate_count, 1, 100, "clip.candidate_count")
    _range(cfg.clip.snap_tolerance_ms, 0, 10000, "clip.snap_tolerance_ms")
    _enum(cfg.clip.vertical_mode, ("center", "blur", "follow"), "clip.vertical_mode")
    if not isinstance(cfg.clip.follow_enhance, bool):
        raise WBError("VALIDATION", "clip.follow_enhance 只能是 true/false")
    _range(cfg.clip.bgm_volume, 0.0, 1.0, "clip.bgm_volume")
    if not isinstance(cfg.clip.bgm_duck, bool):
        raise WBError("VALIDATION", "clip.bgm_duck 只能是 true/false")
    _range(cfg.clip.fade_ms, 0, 3000, "clip.fade_ms")

    for name in ("source_days", "audio_days", "frames_days", "subtitle_days", "export_days"):
        _range(getattr(cfg.storage.retention, name), 0, None, f"storage.retention.{name}")

    _range(cfg.publish.max_comment_chars, 1, 1000, "publish.max_comment_chars")
    _range(cfg.publish.max_summary_chars, 1, 2000, "publish.max_summary_chars")


# =========================================================== 渲染


#: ``llm.max_tokens`` 下面那几行注释，WB 版的说法。之所以要接管它，是因为
#: bili-scribe 渲染的那两句在**本机这类端点上是错的指引**，而用户正好是在「总结返回空」
#: 的时候来翻这个文件。两句都按 2026-11-05 的实测写（证据见 AGENTS.md 第四节 task-50 小节）：
#:   * 「这段推理不吃 max_tokens」——分两类端点。本机 workbuddy/magpie 实测**算进去**
#:     （预算 16384 被思考打满、``finish_reason=length``、正文空）；另一类端点确实不算
#:     （bs 当年实测设 4096 仍跑了 5125 tokens，其中 5120 是思考）。所以两种都不许写成绝对，
#:     判据是报错里的 ``finish_reason`` 与思考 token 数，不是这个数的大小。
#:   * 「客户端发现空正文会自动关思考重试一次」——那条腿在 bili-scribe 的客户端里，
#:     Workbench 这条路没有；而且这个端点连 ``enable_thinking: false`` 都不遵守
#:     （实测关掉那发仍烧 140 思考 tokens）。「把 thinking 设成 off」不是有效解，
#:     不能照着 bs 那句把它写成根治办法。
WB_THINKING_NOTES = (
    "  # 思考模型（Qwen3 系列、QwQ、带 -thinking 的型号等）先产出一大段推理再作答。",
    "  # 这段推理算不算进 max_tokens，因端点而异，判据看报错本身：本机 workbuddy/magpie",
    "  # 实测**算**——预算被打满时 finish_reason=length、正文返回空、usage 里 reasoning_tokens",
    "  # 正好等于 max_tokens。所以正文空先看 finish_reason 和思考 token 数，别无脑抬这个值。",
    "  # auto=不发相关参数 / off=显式发 enable_thinking:false / on=显式发 true。",
    "  # 别指望「空正文会自动关思考重试」：Workbench 这条路没有那条腿，而且这个端点连",
    "  # enable_thinking:false 都不遵守（实测关掉仍烧 140 思考 tokens），关掉不是根治。",
)

#: ``llm.timeout_seconds`` 上面那行注释：bili-scribe 写的是「根治靠关思考」，按上面同一条
#: 实测就是错的（关不掉）。这里只留「调大是缓解」这半句，不给不存在的根治办法。
WB_TIMEOUT_NOTE = ("  # 单次请求的读超时（秒）。思考模型的耗时主要花在推理上，调大它只是缓解。",)


_SECTION_RE = re.compile(r"^[a-z_]+:[ \t]*$")


def _rewrite_llm_comments(body: str) -> str:
    """把 ``llm`` 段里那两处注释换成上面的 WB 版；键、值、缩进一个字节都不改。

    **按形状匹配，不按原文匹配**。按原文匹配（「找到含『不吃 max_tokens』的那行」）有个
    静默失效的坑：bili-scribe 哪天改个措辞，我们的替换就悄悄不生效，用户又看到那句错指引，
    而钉「WB 写出的文件里没有这句」的测试照样绿（新措辞里没那个子串）。形状匹配不看措辞，
    只看「紧跟在 ``llm.max_tokens`` 之后的那整块注释」和「``llm.timeout_seconds`` 前面的那行
    注释」，bs 怎么改措辞都照样被接管。段落判断用 ``^[a-z_]+:$``，所以只可能命中顶层段落名。
    """
    lines = body.split("\n")
    out: list[str] = []
    section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if _SECTION_RE.match(line):
            section = line.strip()[:-1]
        if section == "llm":
            if line.strip().startswith("max_tokens:"):
                out.append(line)
                index += 1
                while index < len(lines) and lines[index].lstrip().startswith("#"):
                    index += 1
                out.extend(WB_THINKING_NOTES)
                continue
            if (
                line.lstrip().startswith("#")
                and index + 1 < len(lines)
                and lines[index + 1].strip().startswith("timeout_seconds:")
            ):
                out.extend(WB_TIMEOUT_NOTE)
                index += 1
                continue
        out.append(line)
        index += 1
    return "\n".join(out)


def render_yaml(cfg: WorkbenchConfig) -> str:
    """渲染 config.yaml —— **只含 bili-scribe 认识的键**。

    这个限制从「共用一份文件、怕打挂 bili-scribe」时代留下来。搬家之后这份 yaml 归
    Workbench 自己（``~/.video-workbench/config.yaml``），那条理由已经不成立了，但格式
    照旧，理由换成两条：形状继续复用 bili-scribe 的 ``render_config_yaml``，不用维护
    两份模板各说一套；而且这份文件仍然能被 bili-scribe 的加载器直接读 —— 我们不依赖
    它了，也没必要主动把它变成一个读不了的形状。
    **新键进来的规则因此变了**：以前加一个 bili-scribe 不认识的键 = 对方起不来（硬事故），
    现在 = 只有 bili-scribe 万一又去读这份文件才会炸（自家事故）。仍然保守 —— 扩展键
    一律进 ``workbench.json``（由 ``save()`` 写），因为设置页、任务模型、下载/转录/切片
    那一大堆本来就该只有一个地方负责，别把同一份配置拆成两种形状去读。

    唯一一处**不照搬** bili-scribe 文本的地方是 ``llm`` 段那两段注释，见
    ``_rewrite_llm_comments``。
    """
    import bili_scribe.config as bc

    head = "\n".join(
        [
            "# VideoWorkbench 配置（本目录归 Workbench 自己，不是 bili-scribe 的配置）。",
            "# 只放 bili-scribe 也认识的键；Workbench 自己的设置（服务商列表、任务模型、",
            f"# 下载/转录/切片等）存在同目录的 {EXT_NAME}。新键请进那个文件，别加在这里 ——",
            "# bili-scribe 的加载器遇到不认识的键会直接报错，这份文件保持能被它读。",
            "",
        ]
    )
    body = _rewrite_llm_comments(bc.render_config_yaml(to_bili_config(cfg)))
    return head + (body if body.endswith("\n") else body + "\n")
