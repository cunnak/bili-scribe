"""配置装载。所有可调参数集中在 config.yaml，代码里不出现魔法数字。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_HOME = Path.home() / ".bili-scribe"

# 必须在模块层定义：dataclass 的 class 属性访问拿到的是 default_factory
# 函数本身，不是列表，直接引用会踩坑。
DEFAULT_SUBTITLE_LANGS = ["zh-CN", "zh-Hans", "zh-Hant", "ai-zh"]


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

        return cls(
            server=section(ServerConfig, raw.get("server")),
            llm=section(LlmConfig, raw.get("llm")),
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
        """暴露给前端的子集。绝不包含 api_key 等机密。"""
        data = asdict(self)
        data["llm"].pop("api_key", None)
        data["llm"]["configured"] = bool(self.llm.api_key)
        return data
