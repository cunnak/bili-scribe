"""config.yaml + workbench.json 这一对的**形状契约**（方案书 6.6 / 偏差记录）。

## task-23 搬家之后：这份文件还钉什么，为什么还钉

原来这条边界是「共享边界」：``config.yaml`` 两边共用，bili-scribe 的 ``AppConfig.load``
遇到不认识的键**直接报错**，所以 WB 把 ``ui.onboarded``、``llm.tasks`` 写进去就等于
让用户打开 bili-scribe 时看到 ``ValueError: LlmConfig 存在未知配置项：tasks``。

搬家（``DEFAULT_HOME`` 从 ``~/.bili-scribe`` 换成 ``~/.video-workbench``）之后，
**「共用同一个文件」这个前提没了**：默认情况下各读各的，WB 写脏也不会打到对方。
但形状契约**一条都不删**，因为 vendor 选的是「只搬家、不改存储格式」
（``video_workbench/config.py`` 搬家后的注释给了两条还站得住的理由）：

1. yaml 的形状继续由 bili-scribe 自己的 ``render_config_yaml`` 生成——省得两份模板各说一套；
2. 这份文件仍然**被 bili-scribe 读得动**（实测），搬家后不依赖它，但也没必要主动
   把它变成一个读不了的形状。

于是本文件与 ``tests/wb/test_config_home_split.py`` 的分工是：

- 本文件：一份**指定路径**的 yaml + sidecar 的形状（写出保守、读入合并、老形状就地重写）；
- ``test_config_home_split.py``：**默认家在哪、迁移读谁写谁、旧家零写入、库仍共用**。

三条契约的前后口径（逐条，写给下一个读的人）：

- 契约 1 ``test_save_keeps_yaml_bili_only``：原来钉「不能出现 bili 不认识的键，因为
  bili 会读这份文件并报错」；现在钉的是同样的形状，理由换成「形状由它说了算 +
  搬家换来的自由是目录自由、不是随便写」。断言一个字没放宽。
- 契约 2 ``test_yaml_wins_for_shared_keys`` / ``test_save_puts_extensions_in_sidecar``：
  原来「共享键以 yaml 为准」的前提是两边都会改同一份文件；搬家后只剩 WB 自己改，
  但**用户仍会手改 yaml**，所以「手改的共享键必须生效」这条照旧成立，保留。
- 契约 3 ``test_load_migrates_legacy_yaml``：这里的「迁移」是**同一份文件内的形状迁移**
  （老版本把扩展键写进 yaml，load 一次重写成新形状），跟「搬家」是两件事。
  搬家那条要求旧家零写入，见 ``test_config_home_split.py``；本条用的是显式路径，
  写回自己那份文件是合法的，不许被当成搬家语义放宽。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from video_workbench.config import WorkbenchConfig, ext_path, read_ext

BILI_SECTIONS = {"server", "llm", "collect", "publish", "storage", "ui"}
BILI_LLM_KEYS = {
    "base_url",
    "api_key",
    "model",
    "temperature",
    "max_tokens",
    "max_input_chars",
    "chunk_size",
    "chunk_overlap",
    "thinking",
    "timeout_seconds",
    "proxy",
    "role",
}


def _cfg(tmp_path: Path, **kw) -> WorkbenchConfig:
    cfg = WorkbenchConfig(**(kw or {}))
    cfg.storage.db_path = str(tmp_path / "wb.db")
    cfg.storage.media_root = str(tmp_path / "media")
    cfg.storage.cache_root = str(tmp_path / "cache")
    cfg.storage.models_root = str(tmp_path / "models")
    cfg.ui.rail_collapsed = True
    cfg.ui.onboarded = True
    cfg.download.quality = "720p"
    cfg.transcribe.model = "small"
    cfg.llm.tasks.summary = "kimi/moonshot-v1-8k"
    return cfg


def test_save_keeps_yaml_bili_only(tmp_path):
    """写盘的 yaml 里不能出现任何 bili-scribe 不认识的键。"""
    target = tmp_path / "config.yaml"
    cfg = _cfg(tmp_path)
    cfg.save(target)

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert set(raw) == BILI_SECTIONS, f"多出来的段：{sorted(set(raw) - BILI_SECTIONS)}"
    assert set(raw["llm"]) == BILI_LLM_KEYS, "llm 段冒出扩展键"
    assert set(raw["ui"]) == {"skin"}, "ui 段冒出扩展键"
    assert set(raw["storage"]) == {"db_path", "key_path"}, "storage 段冒出扩展键"


def test_save_puts_extensions_in_sidecar(tmp_path):
    """扩展配置一条不落地进 sidecar —— 否则「保存了重启就没了」。"""
    target = tmp_path / "config.yaml"
    cfg = _cfg(tmp_path)
    cfg.save(target)

    ext = read_ext(target)
    assert ext["ui"]["rail_collapsed"] is True
    assert ext["ui"]["onboarded"] is True
    assert ext["download"]["quality"] == "720p"
    assert ext["transcribe"]["model"] == "small"
    assert ext["llm"]["tasks"]["summary"] == "kimi/moonshot-v1-8k"
    assert ext["llm"]["providers"], "服务商列表必须落盘"


def test_bili_scribe_can_load_shared_file(tmp_path):
    """真正的验收断言：bili-scribe 自己的加载器能读这个文件。"""
    bc = __import__("bili_scribe.config", fromlist=["AppConfig"])

    target = tmp_path / "config.yaml"
    _cfg(tmp_path).save(target)

    theirs = bc.AppConfig.load(target)  # 有扩展键的话这里就 ValueError
    assert theirs.ui.skin in {"ark", "exhibition"}


def test_round_trip(tmp_path):
    """存了再读，扩展配置一个都不能少。"""
    target = tmp_path / "config.yaml"
    before = _cfg(tmp_path)
    before.save(target)

    after = WorkbenchConfig.load(target)
    assert after.ui.rail_collapsed is True
    assert after.ui.onboarded is True
    assert after.download.quality == "720p"
    assert after.transcribe.model == "small"
    assert after.llm.tasks.summary == "kimi/moonshot-v1-8k"
    assert [p.id for p in after.llm.providers] == [p.id for p in before.llm.providers]


def test_yaml_wins_for_shared_keys(tmp_path):
    """用户手改 yaml 的共享键要生效（bili-scribe 那边也会改同一个文件）。"""
    target = tmp_path / "config.yaml"
    cfg = _cfg(tmp_path)
    cfg.save(target)

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    raw["llm"]["temperature"] = 0.9
    target.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    assert WorkbenchConfig.load(target).llm.temperature == 0.9


def test_load_migrates_legacy_yaml(tmp_path):
    """老文件（扩展键写在 yaml 里）load 一次就地迁移，并让 bili-scribe 重新读得动。"""
    bc = __import__("bili_scribe.config", fromlist=["AppConfig"])

    target = tmp_path / "config.yaml"
    target.write_text(
        "\n".join(
            [
                "ui:",
                "  skin: ark",
                "  rail_collapsed: true",
                "  onboarded: true",
                "llm:",
                "  model: deepseek-chat",
                "  tasks:",
                "    summary: ''",
                "download:",
                "  quality: 720p",
                "storage:",
                f"  db_path: '{tmp_path.as_posix()}/wb.db'",
                f"  media_root: '{tmp_path.as_posix()}/media'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = WorkbenchConfig.load(target)
    assert cfg.ui.rail_collapsed is True
    assert cfg.download.quality == "720p"

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert set(raw) == BILI_SECTIONS, "迁移后 yaml 里还剩扩展段"
    assert "onboarded" not in raw["ui"], "迁移后 ui 段还剩扩展键"
    assert "tasks" not in raw["llm"], "迁移后 llm 段还剩扩展键"
    bc.AppConfig.load(target)  # 不抛 = bili-scribe 又能启动了

    ext = read_ext(target)
    assert ext["ui"]["onboarded"] is True, "迁移要把值搬过去，不能直接丢"
    assert ext["download"]["quality"] == "720p"


def test_migration_keeps_providers(tmp_path):
    """迁移写 sidecar 时要写在「服务商归一」之后，否则用户的服务商会被写成空列表。"""
    target = tmp_path / "config.yaml"
    target.write_text(
        "\n".join(
            [
                "llm:",
                "  base_url: 'https://api.deepseek.com/v1'",
                "  api_key: 'sk-legacy'",
                "  model: deepseek-chat",
                "storage:",
                f"  db_path: '{tmp_path.as_posix()}/wb.db'",
                f"  media_root: '{tmp_path.as_posix()}/media'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    WorkbenchConfig.load(target)
    ext = json.loads(ext_path(target).read_text(encoding="utf-8"))
    assert ext["llm"]["providers"], "迁移把服务商列表写空了"
    assert ext["llm"]["providers"][0]["base_url"] == "https://api.deepseek.com/v1"


def test_sidecar_survives_missing(tmp_path):
    """sidecar 被别人删了也不能拦启动：退回单服务商默认。"""
    target = tmp_path / "config.yaml"
    _cfg(tmp_path).save(target)
    ext_path(target).unlink()

    cfg = WorkbenchConfig.load(target)
    assert cfg.llm.providers, "没有 sidecar 时应该由扁平字段合成一张卡"
