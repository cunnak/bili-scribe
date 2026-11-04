"""多服务商（模型设置照 DSH 重做）的后端契约测试。

覆盖三层，每层都对应一个"界面能不能信"的问题：

1. **归一**：老配置（只有扁平 base_url/api_key/model）不能变成空列表；
   providers 与扁平字段必须只有一个真相，否则切了服务商还在打上一家。
2. **取模型**：``llm.tasks.*`` 的两种写法（``模型名`` 与 ``服务商/模型``）都要能解，
   而且 OpenRouter 那种自带斜杠的模型 id 不能被拆坏。
3. **RPC**：``llm.test`` 指名某张卡时，必须真的用那家的地址与密钥——
   借当前生效服务商的配置去测，会把"新加一家还没填 key"测成通过。
"""

from __future__ import annotations

import json

import pytest
import yaml

from video_workbench.config import (
    LlmProviderConfig,
    WorkbenchConfig,
    ext_path,
    find_provider,
    parse_model_spec,
    provider_id,
    render_yaml,
    resolve_task_model,
    validate,
)


def write_cfg(tmp_path, llm: dict) -> WorkbenchConfig:
    """把一份 llm 段落写成 config.yaml 再读回来（走真实 load 链路，不手搓对象）。"""
    data = {
        "ui": {"skin": "ark"},
        "llm": llm,
        "storage": {
            "db_path": str(tmp_path / "data" / "wb.sqlite"),
            "media_root": str(tmp_path / "media"),
            "cache_root": str(tmp_path / "cache"),
            "models_root": str(tmp_path / "models"),
            "ffmpeg_dir": str(tmp_path / "tools"),
        },
    }
    path = tmp_path / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return WorkbenchConfig.load(path)


# ── 1. provider_id：id 要人能看懂 ───────────────────────────────────────────

def test_provider_id_takes_brand_not_generic_prefix():
    assert provider_id("DeepSeek 官方", "https://api.deepseek.com/v1") == "deepseek"
    assert provider_id("Kimi", "https://api.moonshot.cn/v1") == "moonshot"
    assert provider_id("OpenRouter", "https://openrouter.ai/api/v1") == "openrouter"


def test_provider_id_falls_back_to_hash_when_host_is_useless():
    a = provider_id("", "http://192.168.1.20:11434/v1")
    b = provider_id("", "")
    assert a.startswith("p_") and len(a) == 10
    assert a != b, "不同地址必须给不同 id，否则两张卡会撞"


# ── 2. sync_providers：谁是真相 ─────────────────────────────────────────────

def test_legacy_flat_config_becomes_one_card(tmp_path):
    cfg = write_cfg(tmp_path, {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-old",
        "model": "deepseek-chat",
    })
    cards = cfg.llm.providers
    assert len(cards) == 1, "老配置读进来不能是空列表，否则设置页一开就把密钥弄丢"
    assert cards[0].base_url == "https://api.deepseek.com/v1"
    assert cards[0].api_key == "sk-old"
    assert cards[0].models == ["deepseek-chat"]
    assert cfg.llm.active_provider == cards[0].id
    # 扁平字段保持原值：bili_scribe 那边直接读它。
    assert cfg.llm.api_key == "sk-old" and cfg.llm.model == "deepseek-chat"


def test_active_card_mirrors_into_flat_fields(tmp_path):
    cfg = write_cfg(tmp_path, {
        "base_url": "https://OLD.example/v1",
        "api_key": "sk-old",
        "model": "old-model",
        "active_provider": "kimi",
        "providers": [
            {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-ds", "models": ["deepseek-chat"]},
            {"id": "kimi", "name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
             "api_key": "sk-kimi", "models": ["moonshot-v1-8k", "moonshot-v1-32k"]},
        ],
    })
    assert cfg.llm.base_url == "https://api.moonshot.cn/v1"
    assert cfg.llm.api_key == "sk-kimi"
    assert cfg.llm.model == "moonshot-v1-8k", "默认模型必须落在当前这家列表里"

    # 换 active 再归一一次：扁平字段跟着走（切服务商要立刻生效）。
    cfg.llm.active_provider = cfg.llm.providers[0].id
    cfg.sync_providers()
    assert cfg.llm.base_url == "https://api.deepseek.com/v1"
    assert cfg.llm.model == "deepseek-chat"


def test_unknown_active_falls_back_to_first_card(tmp_path):
    cfg = write_cfg(tmp_path, {
        "active_provider": "不存在的一家",
        "providers": [
            {"id": "a", "name": "A", "base_url": "https://a.example/v1",
             "api_key": "k", "models": ["m1"]},
            {"id": "b", "name": "B", "base_url": "https://b.example/v1",
             "api_key": "k", "models": ["m2"]},
        ],
    })
    assert cfg.llm.active_provider == "a"
    assert cfg.llm.model == "m1"


def test_ids_are_filled_and_deduped(tmp_path):
    cfg = write_cfg(tmp_path, {
        "providers": [
            {"name": "同一地址", "base_url": "https://api.deepseek.com/v1",
             "api_key": "k1", "models": ["m"]},
            {"name": "同一地址", "base_url": "https://api.deepseek.com/v1",
             "api_key": "k2", "models": ["m"]},
        ],
    })
    ids = [p.id for p in cfg.llm.providers]
    assert len(set(ids)) == 2, "重复 id 会让界面点一张卡改到另一张"


def test_env_key_overrides_active_card_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BILI_SCRIBE_API_KEY", "sk-from-env")
    cfg = write_cfg(tmp_path, {
        "active_provider": "b",
        "providers": [
            {"id": "a", "name": "A", "base_url": "https://a.example/v1",
             "api_key": "k-a", "models": ["m"]},
            {"id": "b", "name": "B", "base_url": "https://b.example/v1",
             "api_key": "k-b", "models": ["m"]},
        ],
    })
    assert cfg.llm.api_key == "sk-from-env"
    by_id = {p.id: p for p in cfg.llm.providers}
    assert by_id["b"].api_key == "sk-from-env", "生效卡也要一起换，否则界面显示的还是文件里那把"
    assert by_id["a"].api_key == "k-a", "别家的密钥不能被环境变量覆盖"


# ── 3. 任务 → 服务商/模型 ───────────────────────────────────────────────────

def test_parse_model_spec_both_spellings():
    assert parse_model_spec("deepseek/deepseek-chat") == ("deepseek", "deepseek-chat")
    assert parse_model_spec("deepseek-chat") == ("", "deepseek-chat")
    assert parse_model_spec("") == ("", "")


def two_cards_llm():
    a = LlmProviderConfig(id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com/v1",
                          api_key="k", models=["deepseek-chat", "deepseek-reasoner"])
    b = LlmProviderConfig(id="kimi", name="Kimi", base_url="https://api.moonshot.cn/v1",
                          api_key="k", models=["moonshot-v1-8k"])
    return _FakeLlm([a, b], active="deepseek", model="deepseek-chat")


class _FakeLlm:
    """只带 resolve_task_model 真正会 getattr 的字段。"""

    def __init__(self, providers, active="", model="", tasks=None):
        self.providers = providers
        self.active_provider = active
        self.model = model
        self.tasks = tasks


def test_resolve_named_provider_beats_active():
    from video_workbench.config import LlmTasksConfig

    llm = two_cards_llm()
    llm.tasks = LlmTasksConfig(summary="kimi/moonshot-v1-8k")
    card, model = resolve_task_model(llm, "summary")
    assert card.id == "kimi" and model == "moonshot-v1-8k"


def test_resolve_bare_model_finds_its_home():
    from video_workbench.config import LlmTasksConfig

    llm = two_cards_llm()
    llm.tasks = LlmTasksConfig(summary="deepseek-reasoner")
    card, model = resolve_task_model(llm, "summary")
    assert card.id == "deepseek" and model == "deepseek-reasoner"


def test_resolve_typo_prefix_still_finds_the_model():
    from video_workbench.config import LlmTasksConfig

    llm = two_cards_llm()
    llm.tasks = LlmTasksConfig(summary="deepseek/deepseek-reasoner")
    card, model = resolve_task_model(llm, "summary")
    assert card.id == "deepseek" and model == "deepseek-reasoner"

    # 前缀打错（kimi 没有这个模型，deepseek 才有）：靠模型名找回正确的一家。
    llm.tasks = LlmTasksConfig(summary="kimi/deepseek-chat")
    card, _ = resolve_task_model(llm, "summary")
    assert card.id == "kimi", "前缀对不上但 kimi 是存在的卡，按写的那家走"


def test_resolve_slash_model_id_is_not_cut_in_half():
    """OpenRouter 式模型 id 自带斜杠：前缀不是服务商时，整串必须原样当模型名。

    这条就是防"把 qwen/qwen3-vl-plus 拆成 qwen3-vl-plus"——请求发给网关是个不存在的
    模型，症状是总结报错，跟配置写法隔着一整条调用链，最难查。
    """
    from video_workbench.config import LlmTasksConfig

    llm = two_cards_llm()
    llm.tasks = LlmTasksConfig(summary="qwen/qwen3-vl-plus")
    card, model = resolve_task_model(llm, "summary")
    assert model == "qwen/qwen3-vl-plus"
    assert card.id == "deepseek", "没指名服务商就用当前生效那家"


def test_resolve_without_task_returns_active_and_default_model():
    card, model = resolve_task_model(two_cards_llm(), "")
    assert card.id == "deepseek" and model == "deepseek-chat"


def test_resolve_survives_bili_scribe_flat_config():
    """bili-scribe 的 LlmConfig 没有 providers/tasks —— 必须安静退化，不能抛。"""

    class Flat:
        base_url = "https://api.deepseek.com/v1"
        api_key = "k"
        model = "deepseek-chat"

    card, model = resolve_task_model(Flat(), "summary")
    assert model == "deepseek-chat"
    assert card.base_url == "https://api.deepseek.com/v1"


# ── 4. 校验 / 往返 / 对外形状 ───────────────────────────────────────────────

def test_validate_rejects_cards_that_would_hit_the_wrong_door(tmp_path):
    """只拦"配了但会静默打错门"的：地址缺协议、协议枚举写错。"""
    cases = [
        # 少了 https:// —— urllib 会当相对地址，症状是"连不上"，跟真原因隔一层
        {"providers": [{"name": "A", "base_url": "api.deepseek.com/v1", "models": ["m"]}]},
        {"providers": [{"name": "A", "base_url": "https://a.example/v1", "models": ["m"],
                        "protocol": "anthropic"}]},
    ]
    for i, llm in enumerate(cases):
        with pytest.raises(Exception) as ei:
            write_cfg(tmp_path / f"bad{i}", llm)
        assert getattr(ei.value, "code", "") == "VALIDATION", ei.value


def test_validate_allows_model_ids_with_slashes(tmp_path):
    """模型 id 自带斜杠是常态，不是"写错了"。

    magpie 的 ``auto/hy4-preview``、OpenRouter 的 ``anthropic/claude-3.5``、
    HF 的 ``Qwen/Qwen3-VL-Plus`` 都带斜杠。早先这里按"斜杠等于服务商前缀"一刀切
    拦掉，结果真网关的模型根本存不下来（实测：接入 magpie 时首存就报 VALIDATION）。
    前缀消歧交给 ``parse_model_spec``：只有前缀命中已知服务商 id 才当服务商用。
    """
    cfg = write_cfg(tmp_path, {
        "providers": [{"name": "本机网关", "base_url": "http://127.0.0.1:3425/v1",
                       "api_key": "k", "models": ["auto/hy4-preview", "Qwen/Qwen3-VL-Plus"]}],
    })
    assert cfg.llm.providers[0].models == ["auto/hy4-preview", "Qwen/Qwen3-VL-Plus"]
    # 本机地址给的是可读 id，不是 sha1
    assert cfg.llm.providers[0].id == "local"


def test_unknown_provider_ref_falls_back_instead_of_empty_client():
    """点名一家不存在的服务商时，不许造空客户端报"还没填地址"。

    那会把排查方向引到填表上——明明填了，报的却是"缺 base_url、api_key"。
    """
    from video_workbench.infra.llm import ChatClient

    class Cfg:
        class llm:  # noqa: N801
            base_url = "https://api.deepseek.com/v1"
            api_key = "k"
            model = "deepseek-chat"
            timeout_seconds = 30
            proxy = ""
            user_agent = ""
            providers = []
            active_provider = ""

    client = ChatClient.from_config(Cfg(), provider="typo-here")
    assert client.base_url == "https://api.deepseek.com/v1"
    assert client.api_key == "k"
    assert client.configured


def test_validate_allows_empty_cards_on_a_fresh_install(tmp_path):
    """地址与模型留空是"还没配"，不是"配错了"——首启就该是这个形状，不许拦。"""
    cfg = write_cfg(tmp_path, {"model": "keepme",
                               "providers": [{"name": "还没配的一家", "base_url": "",
                                              "api_key": "", "models": []}]})
    assert cfg.llm.providers[0].id, "空卡也要有 id，否则界面删不掉它"
    # 空卡不许把已有的扁平 model 清空：老配置可能只填了 model，界面一开就丢是最坏结果。
    # （列表非空的卡才会把默认模型拉回自己名下，见 test_active_card_mirrors_into_flat_fields）
    assert cfg.llm.model == "keepme"


def _two_cards(tmp_path):
    """两张服务商卡的真实起点（走 `write_cfg` 那条真 load 链路，不手搓对象）。"""
    return write_cfg(tmp_path, {
        "active_provider": "kimi",
        "model": "moonshot-v1-8k",
        "providers": [
            {"id": "deepseek", "name": "DeepSeek 官方", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-ds", "models": ["deepseek-chat"]},
            {"id": "kimi", "name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
             "api_key": "sk-kimi", "models": ["moonshot-v1-8k"]},
        ],
    })


def test_save_roundtrip_keeps_cards(tmp_path):
    """`save()` 往返必须带得住两张卡——这才是用户真正走的那条路（两个文件一起落）。

    以前这条测的是「`render_yaml` 单独往返也能保住两张卡」，而它绿是靠 `load()` 的自愈写盘：
    load 顺手把 sidecar 补了出来，第二张卡才从那里读回来。`5cc6abf` 撤掉自愈写以后就露了底
    （实得 ['moonshot']，那是用扁平 base_url 推的一张，id 来自域名）。翻成正方向：
    `save()` 会同时写 yaml 与 `workbench.json`（先 `sync_providers()` 再写），所以卡不丢。
    """
    cfg = _two_cards(tmp_path)
    saved_dir = tmp_path / "saved"
    saved_dir.mkdir()
    target = saved_dir / "config.yaml"

    cfg.save(target)

    assert ext_path(target).exists(), "save() 必须一起落 sidecar，服务商就住在那儿"
    back = WorkbenchConfig.load(target)
    assert [p.id for p in back.llm.providers] == ["deepseek", "kimi"]
    assert back.llm.active_provider == "kimi"
    assert back.llm.providers[1].api_key == "sk-kimi"
    assert back.llm.model == "moonshot-v1-8k"


def test_render_yaml_alone_drops_the_cards(tmp_path):
    """说明性一条：`render_yaml` 只渲染 bili-scribe 认识的键，服务商不在 yaml 里。

    这不是缺陷，是形状：`providers` / `active_provider` 属 WB 扩展段，住在 `workbench.json`。
    所以「把 `render_yaml` 的文本单独写成一份 yaml、旁边不放 sidecar 就去 load」**必然**只剩
    一张由扁平字段合成的卡。这条把它钉住，是为了拦下另一种走法：有人为了让旧断言重新绿，
    把 providers 塞进 `render_yaml`（那会让 bili-scribe 读到不认识的键，直接 ValueError）。
    期望值用现读：扁平 base_url 是 active 那张（kimi → moonshot 域名），推出来的 id 由
    `provider_id()` 决定，所以这里断「只剩一张 + 那张的 base_url 是 active 的地址」，
    不把 id 的字面量钉死。

    旧假绿的机制（实测，不是推测）：`foreign_keys()` 把 `llm.providers` / `llm.active_provider`
    算成 bili-scribe 不认识的扩展键，而 `5cc6abf` 之前 `load()` 见到扩展键就 `cfg.save(target)`
    ——于是 `write_cfg` 那次 load 把 sidecar 落在了 **config.yaml 所在的同一目录**；sidecar 是
    按目录共用的（见 `test_the_sidecar_is_scoped_per_directory`），所以同目录那份 `again.yaml`
    读得回两张卡。今天的夹具必须把「单独 yaml」放进**另一个目录**才测得到真实形状。
    """
    cfg = _two_cards(tmp_path)
    text = render_yaml(cfg)

    assert "providers" not in text, "render_yaml 不该渲染 WB 扩展键，bili 读到会报错"
    assert "active_provider" not in text

    solo_dir = tmp_path / "solo"
    solo_dir.mkdir()
    solo = solo_dir / "config.yaml"
    solo.write_text(text, encoding="utf-8")

    assert ext_path(solo).exists() is False, "这份夹具旁边不该有 sidecar（否则就测不到单 yaml 的形状）"
    back = WorkbenchConfig.load(solo)
    assert len(back.llm.providers) == 1, f"单 yaml 往返只剩扁平推出的那一张：{[p.id for p in back.llm.providers]}"
    assert back.llm.providers[0].base_url == "https://api.moonshot.cn/v1", "扁平字段是 active 那张的地址"
    assert back.llm.model == "moonshot-v1-8k", "model 是共享键，单 yaml 往返带得住"


def test_the_sidecar_is_scoped_per_directory(tmp_path):
    """`workbench.json` 按**目录**共用，不按文件名——本轮夹具各占一个目录就是这个原因。

    这条钉形状，不钉功能：同一个目录里放两份不同名的 config，`ext_path()` 给的是同一个
    sidecar；换目录才换 sidecar。它解释了上面那条旧用例为什么能假绿（`5cc6abf` 之前
    `load()` 见到 `llm.providers` / `llm.active_provider` 这类扩展键就 `cfg.save(target)`，
    把两张卡写进**同目录**的 `workbench.json`，随后同目录那份 `again.yaml` 再 load 就
    「什么都保住了」），也是 `test_save_roundtrip_keeps_cards` 与
    `test_render_yaml_alone_drops_the_cards` 各自 `mkdir()` 一个子目录的理由。
    哪天 sidecar 改成按文件名走，这条会红：那不是回归，是提醒这一族用例的独立性靠的是目录。
    """
    same_dir = tmp_path / "same"
    other_dir = tmp_path / "other"
    same_dir.mkdir()
    other_dir.mkdir()

    first = same_dir / "config.yaml"
    second_in_same_dir = same_dir / "another.yaml"
    third = other_dir / "config.yaml"
    for p in (first, second_in_same_dir, third):
        p.write_text(render_yaml(write_cfg(same_dir, {
            "base_url": "https://api.deepseek.com/v1", "api_key": "sk-one", "model": "deepseek-chat",
        })), encoding="utf-8")

    assert ext_path(first).name == "workbench.json"
    assert ext_path(first) == ext_path(second_in_same_dir), "同目录两份 config 现在共用一份 sidecar"
    assert ext_path(first) != ext_path(third), "换了目录就该换 sidecar"


def test_public_view_hides_keys_settings_view_keeps(tmp_path):
    cfg = write_cfg(tmp_path, {
        "providers": [
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-abcdef123455", "models": ["deepseek-chat"]},
        ],
    })
    pub = cfg.public_view()
    card = pub["llm"]["providers"][0]
    # 契约：键还在（设置页要按形状回填），但值必须是空串——明文一格都不许外泄。
    assert card["api_key"] == "", "公开视图（首屏/事件）绝不能带明文密钥"
    assert card["key_tail"] == "3455", "尾号取后 4 位：够界面认，不够拼出密钥"
    assert pub["llm"].get("api_key") is None, "扁平 api_key 也要抹掉"
    assert pub["llm"]["configured"] is True
    assert "sk-abcdef123455" not in json.dumps(pub, ensure_ascii=False)
    priv = cfg.settings_view()
    assert priv["llm"]["providers"][0]["api_key"] == "sk-abcdef123455"


# ── 5. apply（设置页保存）与 RPC 指名 ───────────────────────────────────────

def test_apply_replaces_whole_provider_list(tmp_path):
    cfg = write_cfg(tmp_path, {
        "providers": [
            {"id": "a", "name": "A", "base_url": "https://a.example/v1",
             "api_key": "k", "models": ["m"]},
        ],
    })
    cfg.apply({"llm": {
        "providers": [
            {"id": "a", "name": "A", "base_url": "https://a.example/v1",
             "api_key": "k", "models": ["m"]},
            {"id": "b", "name": "B", "base_url": "https://b.example/v1",
             "api_key": "k2", "models": ["x"]},
        ],
        "active_provider": "b",
    }})
    assert len(cfg.llm.providers) == 2
    assert cfg.llm.base_url == "https://b.example/v1"


def test_apply_rollback_on_invalid_card(tmp_path):
    cfg = write_cfg(tmp_path, {
        "providers": [
            {"id": "a", "name": "A", "base_url": "https://a.example/v1",
             "api_key": "k", "models": ["m"]},
        ],
    })
    before = cfg.llm.base_url
    with pytest.raises(Exception):
        cfg.apply({"llm": {"providers": [
            {"id": "a", "name": "A", "base_url": "https://a.example/v1", "api_key": "k", "models": ["m"]},
            {"id": "b", "name": "B", "base_url": "not-a-url", "api_key": "k", "models": ["x"]},
        ]}})
    assert cfg.llm.base_url == before, "任一张卡不合法必须整体回滚，不能改一半"


class RecordingClient:
    """替身：记录 from_config 收到哪个 provider，并按那张卡构造可测状态。"""

    instances: list = []

    def __init__(self, cfg, provider=""):
        self.cfg = cfg
        self.provider = provider
        card = find_provider(cfg.llm, provider) if provider else None
        self.base_url = card.base_url if card is not None else cfg.llm.base_url
        self.api_key = card.api_key if card is not None else cfg.llm.api_key
        self.model = (card.models[0] if card and card.models else cfg.llm.model)
        self.provider_name = card.name if card is not None else "默认服务商"
        self.chat_called = 0
        RecordingClient.instances.append(self)

    @classmethod
    def from_config(cls, cfg, *, transport=None, task="", provider=""):
        """签名跟真 ChatClient 一致（关键字参数）。

        服务里两条路都走这个工厂：设置页传 ``provider`` 指名某张卡，
        任务侧 ``_llm_client(task)`` 传 ``task`` 走 ``llm.tasks.*`` 映射。
        """
        if not provider:
            from video_workbench.config import resolve_task_model

            card, model = resolve_task_model(cfg.llm, task)
            c = cls(cfg, getattr(card, "id", ""))
            c.model = model or c.model
            return c
        return cls(cfg, provider)

    @property
    def configured(self):
        return bool(self.base_url and self.api_key and self.model)

    def chat(self, messages, **kwargs):
        self.chat_called += 1
        return "OK"


def test_llm_rpc_tests_the_named_card(service, monkeypatch):
    from video_workbench import app as app_pkg
    import video_workbench.app.service as svc_mod

    service.cfg.apply({"llm": {
        "providers": [
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-ds", "models": ["deepseek-chat"]},
            {"id": "kimi", "name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
             "api_key": "", "models": ["moonshot-v1-8k"]},
        ],
        "active_provider": "deepseek",
    }})
    RecordingClient.instances.clear()
    monkeypatch.setattr(svc_mod, "ChatClient", RecordingClient)

    out = service.dispatch("llm.test", {"provider": "deepseek"})
    assert out["ok"] is True, out
    assert out["data"]["provider"] == "DeepSeek"
    assert out["data"]["base_url"] == "https://api.deepseek.com/v1"

    # 未配置密钥的那张卡必须报"没配完整"，而且不能借当前生效服务商混过去。
    bad = service.dispatch("llm.test", {"provider": "kimi"})
    assert bad["ok"] is False
    assert bad["error"]["code"] == "LLM_NOT_CONFIGURED"
    assert "Kimi" in bad["error"]["message"]
    assert all(c.api_key != "sk-ds" for c in RecordingClient.instances if c.provider == "kimi"), \
        "测 kimi 时不许拿 deepseek 的密钥"

    # 不传 provider：测当前生效那家（走 _llm_client → for_task）。
    plain = service.dispatch("llm.test", {})
    assert plain["ok"] is True, plain
    assert plain["data"]["base_url"] == service.cfg.llm.base_url
    assert plain["data"]["provider"] == "DeepSeek"


def test_stub_client_without_provider_name_still_works(service, monkeypatch):
    """注入的客户端不一定是 ChatClient（测试桩/未来实现），取属性不许直接点。"""

    class Bare(RecordingClient):
        @property
        def configured(self):
            return True

        def chat(self, messages, **kwargs):
            return "OK"

    class BareFactory:
        @staticmethod
        def _strip(c):
            # provider_name 是实例属性，这里把它摘掉——模拟一个"没有这个字段"的客户端。
            c.__dict__.pop("provider_name", None)
            return c

        @classmethod
        def from_config(cls, cfg, *, transport=None, task="", provider=""):
            if not provider:
                from video_workbench.config import resolve_task_model

                card, model = resolve_task_model(cfg.llm, task)
                c = Bare(cfg, getattr(card, "id", ""))
                c.model = model or c.model
                return BareFactory._strip(c)
            return BareFactory._strip(Bare(cfg, provider))

    import video_workbench.app.service as svc_mod
    monkeypatch.setattr(svc_mod, "ChatClient", BareFactory)
    out = service.dispatch("llm.test", {"provider": ""})
    assert out["ok"] is True, out
    assert out["data"]["provider"] == ""
