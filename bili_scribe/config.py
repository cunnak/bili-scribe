"""配置装载、应用与回写。

配置有三个流向，各自的约束不同：

1. **装载**：YAML → dataclass。字段缺失退回默认值；出现未知键直接报错，避免
   拼错键名后配置静默失效。
2. **应用**：设置页面提交的值 → 原地修改 dataclass。必须原地改，因为
   `AppState`、采集器、发布器都持有同一个对象引用，替换对象会让它们继续用旧值。
3. **回写**：dataclass → YAML。用带完整注释的模板重新渲染，而不是 PyYAML 的
   `dump`——dump 会把注释全部丢掉，配置文件退化成机器输出，以后没人看得懂。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_HOME = Path.home() / ".bili-scribe"

# 必须在模块层定义：dataclass 的 class 属性访问拿到的是 default_factory
# 函数本身，不是列表，直接引用会踩坑。
DEFAULT_SUBTITLE_LANGS = ["zh-CN", "zh-Hans", "zh-Hant", "ai-zh"]

# 改了之后需要重启才生效的段落
NEEDS_RESTART = ("server",)


def _expand(path: str) -> str:
    return os.path.expanduser(str(path))


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8818


@dataclass
class LlmConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int = 4096
    max_input_chars: int = 24000
    chunk_size: int = 6000
    chunk_overlap: int = 400
    # 思考模型（Qwen3 系列等）会先生成一大段推理再作答，而这段推理
    # **不计入 max_tokens** —— 实测设 4096 却跑了 5125 tokens，其中 5120 是思考。
    # 后果有两个：耗时不随素材长度线性而是失控（撞上读超时），
    # 或者输出预算被思考吃光、正文返回空数组。
    #   auto = 不发送任何相关参数（对非思考模型最兼容）
    #   off  = 显式发 enable_thinking: false
    #   on   = 显式发 enable_thinking: true
    # 即使设为 auto / on，客户端发现"正文为空但烧了思考 token"时会自动关思考重试一次。
    thinking: str = "auto"
    timeout_seconds: int = 300
    # 调用模型时走的 HTTP 代理。空 = 直连，不读环境变量。
    #
    # 这一项存在的原因：`requests` 默认 trust_env=True，会去读 HTTP_PROXY /
    # HTTPS_PROXY。于是"程序走不走代理"实际由**启动它的那个 shell**决定——
    # 从带代理的环境里启动，所有模型请求就被塞进那个代理。
    # 实测踩过：从某个设了 HTTP_PROXY 的会话里启动，请求被送进一个连不到
    # 目标站点的代理，界面报的是 "ProxyError / Tunnel connection failed: 502"，
    # 看起来像"模型服务挂了"，其实是本机代理路由错误。
    # 所以这里显式配置、并让客户端关掉 trust_env：行为可预期，不随 shell 变。
    # 例：http://127.0.0.1:7897
    proxy: str = ""
    # 总结角色。每个角色是一套提示词方法论，三轮回的会走
    # 「生成 → 自我批判 → 修订」三次独立调用（见 core/roles.py）。
    # standard 表示不套用任何角色方法论，行为与不设这一项时完全一致。
    role: str = "standard"


THINKING_MODES = ("auto", "off", "on")
# requests 支持的代理协议。写错的话错误会延后到真正调用模型时才出现，
# 所以在这里就挡掉（见 AppConfig.from_raw）。
PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


@dataclass
class DanmakuConfig:
    enabled: bool = True
    also_with_subtitle: bool = False
    max_items: int = 800
    hot_items: int = 200
    sample_items: int = 200


@dataclass
class CollectConfig:
    prefer_subtitle_lang: list[str] = field(
        default_factory=lambda: list(DEFAULT_SUBTITLE_LANGS)
    )
    include_platform_summary: bool = True
    danmaku: DanmakuConfig = field(default_factory=DanmakuConfig)


@dataclass
class PublishConfig:
    require_confirm: bool = True
    min_interval_seconds: int = 60
    max_comment_chars: int = 900
    disclose_ai: bool = True
    disclosure_text: str = "（本总结由 AI 依据视频字幕／弹幕生成）"


@dataclass
class StorageConfig:
    db_path: str = str(DEFAULT_HOME / "bili-scribe.db")
    key_path: str = str(DEFAULT_HOME / "key.bin")


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    # 配置文件的实际路径。不属于配置内容，由 run.py 装载后写入，
    # 供设置页面知道该回写到哪。故意不参与 asdict()/比较。
    source_path: str | None = field(default=None, repr=False, compare=False)

    @property
    def db_path(self) -> str:
        return _expand(self.storage.db_path)

    @property
    def key_path(self) -> str:
        return _expand(self.storage.key_path)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "AppConfig":
        """从 YAML 装载配置。字段缺失时退回 dataclass 默认值。

        这里刻意写死嵌套关系而不用反射：模块开了
        `from __future__ import annotations`，dataclass 的 `f.type` 是字符串，
        反射判断不出嵌套类型，会把用户的整段配置静默替换成默认值。
        """

        def section(node: type, values: Any):
            if not isinstance(values, dict):
                return node()
            known = {f.name for f in dataclass_fields(node)}
            unknown = set(values) - known
            if unknown:
                raise ValueError(
                    f"{node.__name__} 存在未知配置项：{', '.join(sorted(unknown))}"
                )
            return node(**values)

        raw: dict[str, Any] = {}
        if path is not None:
            cfg_path = Path(_expand(str(path)))
            if not cfg_path.exists():
                # 显式指定了路径却不存在，属于配置错误，不该静默吞掉
                raise FileNotFoundError(f"配置文件不存在：{cfg_path}")
            with open(cfg_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            if not isinstance(raw, dict):
                raise ValueError("config.yaml 顶层必须是映射结构")

        collect_raw = raw.get("collect") or {}
        if not isinstance(collect_raw, dict):
            raise ValueError("collect 段必须是映射结构")
        danmaku_raw = collect_raw.get("danmaku")

        llm_cfg = section(LlmConfig, raw.get("llm"))
        # thinking 是枚举，写错的话会被静默当成 auto 用，
        # 而它的错误值恰恰是"看起来没配错但行为不对"那一类，所以显式挡掉。
        if llm_cfg.thinking not in THINKING_MODES:
            raise ValueError(
                f"llm.thinking 只能是 {' / '.join(THINKING_MODES)}，收到：{llm_cfg.thinking!r}"
            )
        # 代理写错（比如漏了 scheme）时，requests 会抛一个很难读的
        # MissingSchema，而且是在真正调用模型时才炸。这里提前挡掉。
        if llm_cfg.proxy:
            scheme = llm_cfg.proxy.split("://", 1)[0].lower()
            if "://" not in llm_cfg.proxy or scheme not in PROXY_SCHEMES:
                raise ValueError(
                    f"llm.proxy 必须是带协议的完整地址（如 http://127.0.0.1:7897），"
                    f"收到：{llm_cfg.proxy!r}"
                )

        return cls(
            server=section(ServerConfig, raw.get("server")),
            llm=llm_cfg,
            collect=CollectConfig(
                prefer_subtitle_lang=list(
                    collect_raw.get("prefer_subtitle_lang", DEFAULT_SUBTITLE_LANGS)
                ),
                include_platform_summary=bool(
                    collect_raw.get("include_platform_summary", True)
                ),
                danmaku=section(DanmakuConfig, danmaku_raw),
            ),
            publish=section(PublishConfig, raw.get("publish")),
            storage=section(StorageConfig, raw.get("storage")),
        )

    def public_view(self) -> dict[str, Any]:
        """暴露给前端的子集。绝不包含 api_key 等机密。

        设置页面需要看到明文密钥时走独立接口，不要把它塞进这个"随手就会被调用"
        的响应里——这个响应主页面每次加载都会打。
        """
        data = asdict(self)
        data.pop("source_path", None)
        data["llm"].pop("api_key", None)
        data["llm"]["configured"] = bool(self.llm.api_key)
        return data

    def settings_view(self) -> dict[str, Any]:
        """设置页面用的完整视图，含明文 api_key。

        单独一个方法而不是复用 public_view：调用点不同、暴露面不同，
        混在一起迟早会有人把密钥带到不该去的地方。
        """
        data = asdict(self)
        data.pop("source_path", None)
        return data


# ----------------------------------------------------------------- 应用


def apply_settings(cfg: AppConfig, values: dict[str, Any]) -> list[str]:
    """把设置页面提交的值原地写进 cfg。

    返回"需要重启才生效"的段落名。必须原地修改：`AppState`、采集器、发布器都
    持有同一个 cfg 引用，替换对象会让它们继续读旧值——表现为"保存了但没生效"。

    嵌套的 dataclass（如 collect.danmaku）递归下去改，不整段替换。
    """
    restarted: list[str] = []
    for section_name in ("server", "llm", "collect", "publish", "storage"):
        incoming = values.get(section_name)
        if not isinstance(incoming, dict):
            continue
        if section_name in NEEDS_RESTART:
            restarted.append(section_name)
        section = getattr(cfg, section_name)
        for key, value in incoming.items():
            if not hasattr(section, key):
                continue
            current = getattr(section, key)
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if hasattr(current, sub_key):
                        setattr(current, sub_key, sub_value)
            else:
                setattr(section, key, value)
    return restarted


# ----------------------------------------------------------------- 回写


def render_config_yaml(cfg: AppConfig) -> str:
    """把配置渲染成带完整注释的 YAML 文本。

    刻意不用 `yaml.dump`：它会把所有注释丢掉。这个文件是给人看的，
    注释本身就是文档，丢了以后只剩一堆裸键值。
    """
    def q(value: Any) -> str:
        # JSON 字符串转义是 YAML 双引号标量的子集，直接借来用，
        # 省得自己处理中文、引号、反斜杠的转义。
        return json.dumps(value, ensure_ascii=False)

    def flag(value: bool) -> str:
        return "true" if value else "false"

    return "\n".join(
        [
            "# bili-scribe 配置",
            "#",
            "# 这个文件由网页上的设置页面生成。手动修改同样有效，",
            "# 但下次在页面上点保存时会被整体覆盖（覆盖前会备份为 config.yaml.bak）。",
            "# 带完整说明的原始示例在 config.example.yaml。",
            "#",
            "# 本文件已被 .gitignore 排除，不会随仓库外传。",
            "",
            "server:",
            "  # 只监听本机。不要改成 0.0.0.0 —— 这个进程持有你的 B站 登录凭据。",
            "  # 改这两项需要重启程序才生效。",
            f"  host: {q(cfg.server.host)}",
            f"  port: {cfg.server.port}",
            "",
            "llm:",
            "  # 任意 OpenAI 兼容端点。默认 DeepSeek。",
            f"  base_url: {q(cfg.llm.base_url)}",
            f"  api_key: {q(cfg.llm.api_key)}",
            f"  model: {q(cfg.llm.model)}",
            f"  temperature: {cfg.llm.temperature}",
            f"  max_tokens: {cfg.llm.max_tokens}",
            "  # 思考模型（Qwen3 系列等）先产出一大段推理再作答，而这段推理不吃 max_tokens。",
            "  # 后果是耗时失控撞上读超时，或者预算被思考吃光、正文返回空。",
            "  # auto=不发相关参数 / off=显式关闭思考（思考模型建议用这个）/ on=显式开启。",
            "  # 即使填 auto 或 on，客户端发现「空正文 + 有思考 token」时也会自动关掉重试一次。",
            f"  thinking: {q(cfg.llm.thinking)}",
            "  # 单次请求的读超时（秒）。思考模型开着的话调大它只是缓解，根治靠关思考。",
            f"  timeout_seconds: {cfg.llm.timeout_seconds}",
            "  # 调用模型时走的 HTTP 代理。留空 = 直连，并且不读 HTTP_PROXY/HTTPS_PROXY 环境变量。",
            "  # 有些站点（DNS 被污染的那些）必须走代理才能连上，填上你的代理地址，例如：",
            "  #   proxy: \"http://127.0.0.1:7897\"",
            f"  proxy: {q(cfg.llm.proxy)}",
            "  # 总结角色。可选：standard / xueshu（学术分析）/ wengao（文稿编辑）/",
            "  # xiezuo（写作专家）/ fenxi（分析报告）/ jinglian（首席思想精炼师）。",
            "  # 除 standard 外都是三轮回（生成→自我批判→修订），调用数与耗时约为三倍。",
            f"  role: {q(cfg.llm.role)}",
            "  # 素材超过这个长度就走分块归约",
            f"  max_input_chars: {cfg.llm.max_input_chars}",
            f"  chunk_size: {cfg.llm.chunk_size}",
            f"  chunk_overlap: {cfg.llm.chunk_overlap}",
            "",
            "collect:",
            "  # 字幕语言优先级。ai-zh 是平台语音识别，误差高于人工字幕。",
            f"  prefer_subtitle_lang: {q(cfg.collect.prefer_subtitle_lang)}",
            "  # 平台自带的 AI 摘要，仅作对照展示，不参与生成",
            f"  include_platform_summary: {flag(cfg.collect.include_platform_summary)}",
            "  danmaku:",
            f"    enabled: {flag(cfg.collect.danmaku.enabled)}",
            "    # 有字幕时是否也抓弹幕。开启会显著增加 token 消耗。",
            f"    also_with_subtitle: {flag(cfg.collect.danmaku.also_with_subtitle)}",
            f"    max_items: {cfg.collect.danmaku.max_items}",
            "    # 取高频弹幕条数（反映观众情绪高点）",
            f"    hot_items: {cfg.collect.danmaku.hot_items}",
            "    # 沿时间轴均匀采样条数（保留叙事推进）",
            f"    sample_items: {cfg.collect.danmaku.sample_items}",
            "",
            "publish:",
            "  # 人工闸门。关掉等于放弃最后一道防线：",
            "  # 总结生成后会直接发到视频评论区，没有确认环节。",
            f"  require_confirm: {flag(cfg.publish.require_confirm)}",
            "  # 两次发布之间的最短间隔，防止触发 -509 限流",
            f"  min_interval_seconds: {cfg.publish.min_interval_seconds}",
            "  # 平台评论上限 1000 字符，这里默认留出余量",
            f"  max_comment_chars: {cfg.publish.max_comment_chars}",
            "  # 是否在评论里注明 AI 生成",
            f"  disclose_ai: {flag(cfg.publish.disclose_ai)}",
            f"  disclosure_text: {q(cfg.publish.disclosure_text)}",
            "",
            "storage:",
            f"  db_path: {q(cfg.storage.db_path)}",
            "  # 凭据加密密钥，单独存放",
            f"  key_path: {q(cfg.storage.key_path)}",
            "",
        ]
    )


def save_config(cfg: AppConfig, path: str | os.PathLike | None = None) -> dict[str, Any]:
    """把配置回写到 YAML，返回路径与备份信息。

    先写临时文件再原子替换：中途失败不至于把原配置截断成半截文件。
    """
    target_raw = path or cfg.source_path
    if not target_raw:
        raise ValueError("不知道要写到哪里：配置没有 source_path")
    target = Path(_expand(str(target_raw)))
    target.parent.mkdir(parents=True, exist_ok=True)

    backup: Path | None = None
    if target.exists():
        backup = target.with_name(target.name + ".bak")
        shutil.copy2(target, backup)

    content = render_config_yaml(cfg)
    tmp = target.with_name(target.name + ".tmp")
    # 显式 newline="\n"：默认的 newline=None 在 Windows 上会把 \n 翻译成 \r\n，
    # 于是"返回的字节数"和"磁盘上的字节数"对不上，行尾也和随包附带的
    # config.example.yaml（纯 LF）不一致。
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp, target)

    return {
        "path": str(target),
        "backup": str(backup) if backup else None,
        "bytes": target.stat().st_size,
    }
