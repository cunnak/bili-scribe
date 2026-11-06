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
- 契约 3（``5cc6abf`` 翻正）：「load 一次就把老形状就地重写成新形状」这条**已经不成立**，
  而且不是回归——撤掉自愈写盘是有意的：只读的诊断口不许重写用户配置。现在的口径是
  ``test_legacy_yaml_is_read_but_never_rewritten``：脏值照样读得到，文件一个字节都不动，
  扩展键的清理归 bili-scribe 自己（它读到脏键照样 ``ValueError``）。
  「搬家」（旧家→新家）那条仍要求旧家零写入、新家落 sidecar，见 ``test_config_home_split.py``
  与本文件的 ``test_migration_keeps_providers``；两者都不许被当成「load 可以随便写盘」放宽。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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


def test_legacy_yaml_is_read_but_never_rewritten(tmp_path):
    """老文件（扩展键写在 yaml 里）：值读得到，但 `load()` 一个字节都不改它。

    以前为什么绿：`load()` 带「自愈写盘」——读到扩展键就顺手把整份 yaml 重写成新形状
    并落 sidecar，所以旧断言能写 `set(raw) == BILI_SECTIONS`，还能写「bili-scribe 又读得动了」。
    现在绿在哪（`5cc6abf` 的契约变更，不是回归）：只读诊断口不许重写用户配置。这份文件
    按现在的形状钉四件事，一条都不许退回去：
    1. 脏值照样读得到（`ui.onboarded` / `ui.rail_collapsed` / `download.quality` / `llm.tasks`），
       并且被登记进 `cfg.foreign_keys`——搬家提示文案与 `test_config_migration_notice` 都吃这份清单；
    2. 被读的那份文件字节前后一致（原始字节比原始字节，不许拿 `git cat-file -s` 那种
       已归一化的数当指纹）；
    3. 不顺手造 sidecar、不造 `.bak`（读就是读，目录里只剩它自己）；
    4. bili-scribe 读它**照样报错**——扩展键的清理责任在 bili 那一侧，WB 不替它改文件。
       「WB 自己写出的文件 bili 读得动」由 `test_bili_scribe_can_load_shared_file` 钉，
       两件事别混成一条。
    """
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
    before = target.read_bytes()

    cfg = WorkbenchConfig.load(target)

    # 1) 值读得到，不许因为「不认识的键」就丢
    assert cfg.ui.rail_collapsed is True
    assert cfg.ui.onboarded is True
    assert cfg.download.quality == "720p"
    assert cfg.llm.model == "deepseek-chat"
    assert cfg.llm.tasks.summary == ""
    assert set(cfg.foreign_keys) == {
        "ui.rail_collapsed",
        "ui.onboarded",
        "llm.tasks",
        "download",
        "storage.media_root",
    }, f"扩展键清单变了，搬家提示的文案要跟着重评：{cfg.foreign_keys}"
    assert cfg.foreign_keys == sorted(cfg.foreign_keys), "清单顺序应当稳定（sorted）"

    # 2) 文件一个字节都没动
    assert target.read_bytes() == before, "load() 又开始的自愈写盘：只读诊断口不许改用户配置"

    # 3) 读不产生副作用：没有 sidecar、没有 .bak，目录里只有它自己
    assert ext_path(target).exists() is False, "load() 不许顺手造 sidecar"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml"], "读盘不该留下任何文件"

    # 4) 脏键仍然烫到 bili——清理归它自己，WB 不替它改
    with pytest.raises(ValueError) as exc:
        bc.AppConfig.load(target)
    assert "未知配置项" in str(exc.value), (
        f"bili 的报错形状变了，先看是不是换了校验方式：{exc.value}"
    )


def test_migration_keeps_providers(tmp_path, monkeypatch):
    """真搬家那趟必须把服务商落到新家 sidecar，且旧家一个字节都不写。

    以前为什么红：夹具传了**显式路径**（`load(target)`）。`WorkbenchConfig.load(path)` 按设计
    绝不触发搬家（`config.py` 里的条件是 `path is None`，注释逐字「指到哪读到哪是用户的意图」），
    所以 sidecar 永远不落，读它就 `FileNotFoundError`。这是夹具错，不是产品错。
    现在绿在哪：改走真搬家分支——新家不存在 + 旧家有文件，调 `load()` 不带路径。
    家目录常量是 **import 期**用 `Path.home()` 算的，改 `HOME`/`USERPROFILE` 对已经 import 的
    常量无效，所以直接 monkeypatch 模块属性（`WorkbenchConfig.load` 在调用时现取这些名字，
    patch 有效；别的测试也不看常量本身，只看函数行为）。
    这条同时钉住旧契约里仍然成立的两半：搬家不许把服务商弄空、旧家零写入。
    """
    import video_workbench.config as cfg_mod

    home = tmp_path / "home"
    new_home = home / ".video-workbench"
    old_home = home / ".bili-scribe"
    old_home.mkdir(parents=True)
    legacy_yaml = old_home / "config.yaml"
    legacy_yaml.write_text(
        "\n".join(
            [
                "llm:",
                "  base_url: 'https://api.deepseek.com/v1'",
                "  api_key: 'sk-legacy'",
                "  model: deepseek-chat",
                "storage:",
                f"  db_path: '{(old_home / 'bs.db').as_posix()}'",
                f"  media_root: '{(old_home / 'media').as_posix()}'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    legacy_before = legacy_yaml.read_bytes()

    monkeypatch.setattr(cfg_mod, "DEFAULT_HOME", new_home)
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", new_home / "config.yaml")
    monkeypatch.setattr(cfg_mod, "LEGACY_HOME", old_home)
    monkeypatch.setattr(cfg_mod, "LEGACY_CONFIG_PATH", legacy_yaml)

    cfg = WorkbenchConfig.load()

    assert cfg.migrated_from is not None, "这条测的就是真搬家分支，migrated_from 必须指回旧家"
    assert Path(cfg.migrated_from) == legacy_yaml, f"搬家的来源不对：{cfg.migrated_from}"

    sidecar = ext_path(new_home / "config.yaml")
    assert sidecar.exists(), "搬家必须把扩展段落到新家 sidecar，不能只搬 yaml"
    ext = json.loads(sidecar.read_text(encoding="utf-8"))
    assert ext["llm"]["providers"], "搬家把服务商列表写空了（sync_providers 得在写 sidecar 之前）"
    assert ext["llm"]["providers"][0]["base_url"] == "https://api.deepseek.com/v1"
    assert ext["llm"]["providers"][0]["api_key"] == "sk-legacy"

    # 旧家是 bili-scribe 的文件：读可以，写不行，连 .bak 都不许留
    assert legacy_yaml.read_bytes() == legacy_before, "搬家不许写旧家一个字节"
    assert sorted(p.name for p in old_home.iterdir()) == ["config.yaml"], (
        "旧家不许多出 .bak/sidecar"
    )


def test_sidecar_survives_missing(tmp_path):
    """sidecar 被别人删了也不能拦启动：退回单服务商默认。"""
    target = tmp_path / "config.yaml"
    _cfg(tmp_path).save(target)
    ext_path(target).unlink()

    cfg = WorkbenchConfig.load(target)
    assert cfg.llm.providers, "没有 sidecar 时应该由扁平字段合成一张卡"
