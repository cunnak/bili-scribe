"""task-21 第 5 节：预读缓存的**地址语义**（5b/5c/5d/5e）。

前七条（`test_summary_prepare_reuse.py`）钉的是「复用有没有生效」，这个文件钉的是
**凭什么复用**。判据方向按 lead 在 m02437 重写的那版——他原来要求「键里必须含 role
与 extra」是**原则性写反**，已作废：预读发的是固定的 `MAP_SYSTEM`（副本
`core/summary/summarizer.py:431`），角色 system 与附加提示词**根本不参与预读**，
所以它们不许进地址；否则用户每换一个角色就白付一次预读（bs 实测 4 小时视频 7 段
= 一次总结 38% 输入、32,663 tokens）。

四条口径与对应的实测事实：
- **5b**（核心）换角色 / 开附加提示词 → 地址**不变**、仍然复用、不重新预读。
- **5c** 反向：素材真变、recipe 变、`MAP_SYSTEM` 变 → 地址**必须变**、必须重新预读。
- **5d** `cache.put` 只在**真跑过预读**那一次发生（副本 `:451-454`）：冷路径 1 次、
  磁盘命中 0 次、内存命中连 `get` 都不发生。lead rev4 那句「复用路径 put 1 次、
  冷盘路径 0 次」与代码相反，这里按实测钉并把纠正报出去。
- **5e** 每 job 独立：不同 task_key 不许互相顶素材。

**5b 为什么不能写成 `make_key(a) == make_key(a)`**：`make_key` 的形参里根本没有角色，
那样比是恒真、自己考自己。下面的 `RecordingCache` 记录的是**产品实际传进来的实参**，
所以「角色没混进地址」是对行为的观察，不是对签名的复述；将来有人往 recipe 或
map_prompt 里塞角色提示词，`_assert_no_role_material_in_address` 会点名。
"""

from __future__ import annotations

import json

import pytest

from bili_scribe.core import roles as bs_roles
from bili_scribe.core import summarizer as bs_summarizer
from video_workbench.core import prompt_source as ps
from video_workbench.core import summarizer as ad
from video_workbench.infra.notecache import NoteCache

from wb_parity_tiers import default_env  # noqa: F401  （pytest fixture，不是未使用导入）
from test_summary_parity import make_cfg
from test_summary_prepare_reuse import (  # 复用同一套假 LLM 与素材，不再造第二个假对象
    MAP_PREFIX,
    RAW_MARK,
    THREE_PASS_REQUESTS,
    ReuseLLM,
    _fresh_key,
    _project,
    _segments,
)

#: 附加提示词全文里的特征串。从 `test_summary_parity` 引同一份，不在这里另抄一遍。
from test_summary_parity import EXTRA_MARKERS

#: 第二份素材独有的标记句。5c/5e 要靠它证明「换了素材」而不是「换了个键」。
OTHER_MARK = "另一份素材的标记句，长度足够触发分段预读。"

#: 每段约 44 字；700 段 ≈ 3.1 万字 > max_input_chars(24000)，必然触发分段预读。
#: 素材短于阈值时 `prepare_material` 直接原样返回（bs `:404-406` 那个早退），
#: 「地址变了、重新预读了」就永远量不到——上一版我在这里踩过假绿，见文件头注释。
OTHER_SEGMENT_COUNT = 700


def _other_segments(count: int = OTHER_SEGMENT_COUNT) -> list[dict]:
    """与 `_segments` 长度同量级、**内容不同**的第二份素材。"""
    return [{"start_ms": index * 5000, "text": f"另说第{index}段{OTHER_MARK}"}
            for index in range(1, count + 1)]


class _Quiet:
    """进度与日志的丢弃槽：本文件的判据是**请求与地址**，不是文案。"""

    def progress(self, *args, **kwargs):
        return None

    def log(self, *args, **kwargs):
        return None


class RecordingCache(NoteCache):
    """只加记录、不改行为：三个方法都原样委托给 `NoteCache`。"""

    def __init__(self, root) -> None:
        super().__init__(root)
        self.make_key_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.put_calls: list[str] = []

    def make_key(self, body, source_label, recipe, map_prompt):
        key = NoteCache.make_key(body, source_label, recipe, map_prompt)
        self.make_key_calls.append({
            "key": key,
            "body_chars": len(body),
            "body_head": body[:24],
            "source_label": source_label,
            "recipe": dict(recipe),
            "map_prompt": map_prompt,
        })
        return key

    def get(self, key):
        self.get_calls.append(key)
        return super().get(key)

    def put(self, key, body, chunks, meta=None):
        self.put_calls.append(key)
        return super().put(key, body, chunks, meta=meta)


def _run(cache, task_key: str, role: str, *, cfg=None, extra_prompt=None,
         segments=None):
    llm = ReuseLLM()
    out = ad.run(_project(), segments if segments is not None else _segments(),
                 client=llm, cfg=cfg or make_cfg(), ctx=_Quiet(), role=role,
                 cache=cache, task_key=task_key, extra_prompt=extra_prompt)
    return llm, out


def _refine(cache, task_key: str, previous, role: str, requirement: str, *,
            cfg=None, extra_prompt=None, segments=None):
    llm = ReuseLLM()
    out = ad.refine(_project(), segments if segments is not None else _segments(),
                    previous, requirement, client=llm, cfg=cfg or make_cfg(),
                    ctx=_Quiet(), role=role, cache=cache, task_key=task_key,
                    extra_prompt=extra_prompt)
    return llm, out


def _previous(tag: str) -> dict:
    """手搭一份「上一次总结」，只为让 refine 过 `previous` 那道门（CONFLICT）。"""
    return {"one_line": f"前次总结 {tag}", "summary_md": f"正文 {tag}",
            "key_points": ["点一", "点二"], "timeline": [], "quotes": [],
            "chapters": [], "tags": ["固定"], "role": "wengao",
            "model": "reuse-fake"}


def _assert_no_role_material_in_address(call: dict, label: str) -> None:
    """地址实参里不许出现角色本体：既不许有 txt 全文，也不许有压缩版 system。"""
    blob = json.dumps(call, ensure_ascii=False)
    for role_key in ("wengao", "xiezuo", "fenxi", "jinglian"):
        name = ps.MAP.get(role_key)
        if name:
            text = ps.load(name).strip()
            assert text[:40] not in blob, (
                f"{label}：预读地址的实参里出现了 {role_key} 的 txt 真源正文——"
                "角色不参与预读，进地址等于每换一个角色白付一次预读")
        compressed = bs_roles.get_role(role_key).system
        if compressed:
            assert compressed[:40] not in blob, (
                f"{label}：地址实参里出现了 {role_key} 的压缩版 system（同上）")


# ============================================================ 5b 换角色不换地址


def test_switching_the_role_does_not_change_the_pre_read_address(default_env, tmp_path):
    """wengao 预读过的素材，换成 xiezuo 应当**直接复用**，一次预读都不发。

    守两句承诺：①「同一视频不要白付两遍钱」（换角色是最常见动作）；②地址只覆盖
    真正影响笔记的输入。第二句的正面证据是 `map_prompt == MAP_SYSTEM`——角色 system
    要是混进预读，这里先红，而不是等命中率掉了再猜。
    """
    cache = RecordingCache(tmp_path / "notes")
    key_wengao = _fresh_key("role-wengao")
    key_xiezuo = _fresh_key("role-xiezuo")

    cold, produced = _run(cache, key_wengao, "wengao")
    assert cold.count("map") >= 2, "前置不成立：首角色没真的分段预读"
    assert len(cache.make_key_calls) == 1, (
        f"一次生成应当只算一次地址，实得 {len(cache.make_key_calls)} 次")
    first = cache.make_key_calls[0]
    assert first["map_prompt"] == bs_summarizer.MAP_SYSTEM, (
        "预读地址里的 map_prompt 不是 MAP_SYSTEM——角色 system 混进预读了")
    _assert_no_role_material_in_address(first, "首角色")

    warm, _ = _refine(cache, key_xiezuo, produced["summary"], "xiezuo",
                      "换个角色重出一版，保留三个论点")
    assert warm.count("map") == 0, (
        f"换角色后仍发了 {warm.count('map')} 次预读——地址里混进了角色")
    assert warm.total() == THREE_PASS_REQUESTS
    assert len(cache.make_key_calls) == 2, "第二轮应当只多算一次地址"
    second = cache.make_key_calls[-1]
    assert second["key"] == first["key"], (
        f"同一素材换角色后地址变了：{first['key'][:12]}… → {second['key'][:12]}…")
    assert second["recipe"] == first["recipe"], "recipe 漂了但 key 没变"
    assert second["map_prompt"] == first["map_prompt"], "map_prompt 漂了但 key 没变"
    _assert_no_role_material_in_address(second, "换角色后")
    assert cache.get_calls and cache.get_calls[-1] == second["key"], (
        "复用必须是拿同一个地址去查磁盘")


def test_turning_the_extra_prompt_on_does_not_change_the_address(default_env, tmp_path):
    """附加提示词开关也**不许**进预读地址：它只接在生成与修订两轮上。

    产品事实：适配层 `with prompt_source.use_extra_prompt(...)` 从 `:884`/`:968`
    才开始包，`prepare_material` 在 `:876`/`:962`（with 外面）。开关进地址的代价与
    角色进地址完全一样：每切一次重付一次预读。
    """
    cache = RecordingCache(tmp_path / "notes")
    key_off = _fresh_key("extra-off")

    cold_off, produced = _run(cache, key_off, "wengao", extra_prompt=False)
    assert cold_off.count("map") >= 2
    key_before = cache.make_key_calls[-1]["key"]

    warm_on, _ = _refine(cache, _fresh_key("extra-on"), produced["summary"],
                         "wengao", "加上附加提示词再出一版", extra_prompt=True)
    assert warm_on.count("map") == 0, (
        f"打开附加提示词后重付了 {warm_on.count('map')} 次预读——开关进了地址")
    assert cache.make_key_calls[-1]["key"] == key_before, "附加提示词不该改变预读地址"

    # 开关确实生效的那一处：归约两轮的 system 必须含 extra 全文特征串。
    # 不钉这个就是假绿——「没进地址」和「根本没接进产品」会长得一模一样。
    hit = [marker for marker in EXTRA_MARKERS if marker in warm_on.system_of(0)]
    assert hit, (
        "开着附加提示词，生成那轮的 system 里却没有它——开关根本没接到模型")


def test_the_extra_prompt_is_still_absent_when_the_switch_is_off(default_env, tmp_path):
    """上一条的反面：同一个 job 显式关掉，特征串必须真的不在 system 里。

    只测「开着有」不测「关着没有」，那么把 extra 无条件烘进 system 的实现也能过前一条。
    这条与前一条一起才把开关本身钉住（地址不变是它们共同的推论）。
    """
    cache = RecordingCache(tmp_path / "notes")
    llm, _ = _run(cache, _fresh_key("extra-off-only"), "wengao", extra_prompt=False)
    blob = llm.all_text()
    for marker in EXTRA_MARKERS:
        assert marker not in blob, (
            f"显式关掉附加提示词，{marker} 却出现在请求里——开关是装饰")
    assert llm.count("map") >= 2, "前置不成立：这条素材没触发分段预读"


# ============================================================ 5c 该变的一定要变


def test_different_material_changes_the_address_and_pays_again(default_env, tmp_path):
    """素材正文变了 → 地址必须变、必须重新预读。

    否则用户重录/改完字幕，拿到的还是旧笔记——这种错不报错、只是内容不对，
    是最贵的一类。反向对照（什么都不改 → 地址必须相同）由 5b 那条承担。
    """
    cache = RecordingCache(tmp_path / "notes")
    key_a = _fresh_key("mat-a")
    cold, _ = _run(cache, key_a, "wengao")
    assert cold.count("map") >= 2, "前置不成立：第一份素材没触发分段预读"
    key_first = cache.make_key_calls[-1]["key"]
    chars_first = cache.make_key_calls[-1]["body_chars"]

    shorter = _other_segments()        # 同样够长、但内容不同的第二份素材
    again, _ = _run(cache, _fresh_key("mat-b"), "wengao", segments=shorter)
    assert again.count("map") >= 2, (
        "第二份素材没触发分段预读——夹具长度不够，下面的地址对照就是空跑")
    changed = cache.make_key_calls[-1]
    assert changed["key"] != key_first, (
        f"素材正文不同而地址相同：{key_first[:12]}…——旧笔记会被误用")
    assert changed["body_chars"] != chars_first, (
        "两份不同素材量出来的 body_chars 相同，夹具失效")
    assert OTHER_MARK in again.all_text(), "第二次预读吃的不是新素材，是旧笔记"


def test_recipe_change_invalidates_the_address(default_env, tmp_path):
    """分块参数变了 → 分段边界就变了，旧笔记不可比，地址必须变。"""
    cache = RecordingCache(tmp_path / "notes")
    _run(cache, _fresh_key("recipe-a"), "wengao")
    key_a = cache.make_key_calls[-1]["key"]
    recipe_a = cache.make_key_calls[-1]["recipe"]

    cfg = make_cfg(chunk_size=int(recipe_a["chunk_size"]) + 1200)
    llm, _ = _run(cache, _fresh_key("recipe-b"), "wengao", cfg=cfg)
    assert llm.count("map") >= 2, "改了分块参数却没重新预读"
    recipe_b = cache.make_key_calls[-1]["recipe"]
    assert cache.make_key_calls[-1]["key"] != key_a, (
        f"recipe 变了地址却没变：{recipe_a} vs {recipe_b}")


def test_map_prompt_change_invalidates_the_address(default_env, tmp_path, monkeypatch):
    """预读指令本身变了 → 产出不可比，地址必须变。

    两个坑都在这里：
    1) 要打的是**副本**那份 `MAP_SYSTEM`（`video_workbench/core/summary/summarizer.py:74`），
       适配层调的是副本的 `prepare_material`，打原件那份模块全局不会生效——我第一版
       就是打错了模块，结果第二次直接磁盘命中（`map == 0`），报得像「产品没反应」，
       实际是我的探针没改到东西。
    2) 只动指纹前 10 字**之后**的内容，保证 `ReuseLLM` 仍认得出预读请求（分类靠
       `MAP_PREFIX`，见 `test_summary_prepare_reuse.py` 文件头）——否则「重新预读」
       是记账错而不是产品行为。
    """
    from video_workbench.core.summary import summarizer as copy_summarizer

    assert len(MAP_PREFIX) == 10
    cache = RecordingCache(tmp_path / "notes")
    _run(cache, _fresh_key("prompt-a"), "wengao")
    key_a = cache.make_key_calls[-1]["key"]

    patched = copy_summarizer.MAP_SYSTEM + "\n（测试追加的一句预读要求）"
    monkeypatch.setattr(copy_summarizer, "MAP_SYSTEM", patched)
    llm, _ = _run(cache, _fresh_key("prompt-b"), "wengao")
    assert llm.count("map") >= 2, (
        "预读指令变了却没重新预读——要么地址含的 map_prompt 不是这一份，要么夹具没生效")
    last = cache.make_key_calls[-1]
    assert last["key"] != key_a, "map_prompt 变了地址却没变"
    assert last["map_prompt"] == patched, (
        "产品送进地址的不是被打断的那份预读指令——那它送的是哪一份？")


# ============================================================ 5d put 的真实形状


def test_put_runs_once_on_the_cold_path_and_zero_times_when_reusing(default_env, tmp_path):
    """按实测形状钉 `put`：冷路径 1 次、磁盘命中 0 次、内存命中连 `get` 都不发生。

    只钉**次数**，不钉写进去的内容（钉内容就是照着实现抄答案）。三层形状不同正是
    区分「哪层生效」的唯一办法：内存命中连磁盘都不碰，磁盘命中只读不写。
    """
    root = tmp_path / "notes"
    cold_cache = RecordingCache(root)
    key_cold = _fresh_key("put-cold")

    cold, produced = _run(cold_cache, key_cold, "wengao")
    assert cold.count("map") >= 2, "前置不成立：冷路径没真的预读"
    assert len(cold_cache.put_calls) == 1, (
        f"冷路径应当写一次盘，实得 {len(cold_cache.put_calls)} 次")
    assert len(cold_cache.get_calls) == 1, "冷路径应当先查一次盘（未命中）"

    # 同一 task_key 第二轮：内存命中，根本不进 prepare_material → get/put 都不动。
    gets_before = len(cold_cache.get_calls)
    puts_before = len(cold_cache.put_calls)
    warm, produced2 = _refine(cold_cache, key_cold, produced["summary"], "wengao",
                             "同一任务再改一版")
    assert warm.count("map") == 0, "内存命中失败，后面两段的对照就失去意义"
    assert len(cold_cache.get_calls) == gets_before, "内存命中却去查了磁盘"
    assert len(cold_cache.put_calls) == puts_before, "内存命中却回写了磁盘"

    # 新 task_key + 同目录：磁盘命中 → get 1 次、put 0 次。
    reopened = RecordingCache(root)
    hits_before = reopened.hits
    disk_warm, _ = _refine(reopened, _fresh_key("put-disk"), produced2["summary"],
                          "wengao", "重开项目再改一版")
    assert disk_warm.count("map") == 0, "磁盘命中失败"
    assert reopened.hits == hits_before + 1, "命中没体现在计数上"
    assert len(reopened.get_calls) == 1, (
        f"磁盘命中应当只查一次，实得 {len(reopened.get_calls)} 次")
    assert reopened.put_calls == [], "磁盘命中却回写了——put 只应发生在真跑过预读之后"


# ============================================================ 5e 每 job 独立


def test_each_job_gets_its_own_pre_read_not_the_others(default_env):
    """两个 job 各自的 task_key：谁都不许吃对方的笔记（故意 cache=None）。

    排除磁盘层，只测内存隔离。内存字典是模块级的（`_prepared`），若有人把它缩成
    单槽或按素材内容共享，第二个 job 会拿到第一个 job 的笔记——**次数看起来仍是省的**，
    所以这条只能靠内容对照来钉：B 的请求里必须出现 B 的标记句、且不许出现 A 的。

    两份素材都必须够长（见 `_other_segments` 的注释）：短的会走
    `prepare_material` 的恒等早退，`map` 恒为 0，那时候「没重新预读」看着像复用成功，
    其实什么都没测。
    """
    segments_a = _segments()
    segments_b = _other_segments()

    llm_a, _ = _refine(None, _fresh_key("iso-a"), _previous("A"), "wengao",
                       "第一个 job 的要求", segments=segments_a)
    assert llm_a.count("map") >= 2, "前置不成立：第一个 job 没真的预读"
    assert RAW_MARK in llm_a.all_text(), (
        "第一个 job 的素材标记句没进请求——没有对照，下面的反向断言就是空断言")

    llm_b, _ = _refine(None, _fresh_key("iso-b"), _previous("B"), "wengao",
                       "第二个 job 的要求", segments=segments_b)
    assert llm_b.count("map") >= 2, "第二个 job 没重新预读——内存层把别人的笔记顶给了它"
    blob_b = llm_b.all_text()
    assert OTHER_MARK in blob_b, "第二个 job 的预读没吃自己的素材"
    assert RAW_MARK not in blob_b, (
        "第二个 job 的请求里出现了第一个 job 的素材标记句——内存层串了 job")

    # 反向对照：同一份素材、不同 task_key，也必须各自付（内存键是 task_key，不是内容）。
    llm_c, _ = _refine(None, _fresh_key("iso-c"), _previous("C"), "wengao",
                       "第三个 job", segments=segments_a)
    assert llm_c.count("map") >= 2, (
        "同素材不同 job 共用了内存笔记——地址式共享会把「谁在跑」这件事丢掉")


@pytest.mark.parametrize("role_key", ["wengao", "xiezuo"])
def test_the_address_is_the_same_for_both_roles_before_and_after_pre_reading(
        default_env, tmp_path, role_key):
    """5b 的可读版：把「两次地址相同」按角色参数化跑一遍，防止只有 wengao→xiezuo 单向成立。

    两个方向都跑：先 xiezuo 后 wengao、先 wengao 后 xiezuo。地址若含角色，
    这两个参数化里至少有一个会红——而不是只测「第一个角色冷、第二个角色热」这一条路。
    """
    order = ("wengao", "xiezuo") if role_key == "wengao" else ("xiezuo", "wengao")
    cache = RecordingCache(tmp_path / f"notes-{role_key}")
    cold, produced = _run(cache, _fresh_key(f"pair-{role_key}-1"), order[0])
    assert cold.count("map") >= 2, f"前置不成立：{order[0]} 没真的预读"
    warm, _ = _refine(cache, _fresh_key(f"pair-{role_key}-2"), produced["summary"],
                      order[1], "换角色再出一版，结构别动")
    assert warm.count("map") == 0, f"{order[0]} → {order[1]} 没复用"
    assert cache.make_key_calls[0]["key"] == cache.make_key_calls[-1]["key"]
