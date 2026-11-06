"""二次打磨不重跑预读 —— task-10 那两层复用的常驻回归。

## 这个文件守的是哪三句产品承诺

`video_workbench/core/summarizer.py` 里预读复用有**两层**，两层堵的是同一笔钱：

1. **同一项目再改一版，不重跑预读**（内存层，钥匙是 `task_key`＝project_id，
   `:932` 的 `get_prepared(task_key)` 命中后连 `prepare_material` 都不进）。
   对应 bs `run_refine` 的「不重新采集、不重新预读——素材笔记直接从 ctx 拿」。
2. **同一个视频被重开成第二个项目，直接命中磁盘**（磁盘层
   `video_workbench/infra/notecache.py`，按内容寻址、跨任务跨进程）。
   成本依据是 bs `core/summarizer.py:392-394` 的实测：4 小时视频 7 段预读占一次
   总结 **38%** 的输入（32,663 tokens），第二遍再付就是白付。
3. **冷缓存必须真的付钱**。这条不是凑数：没有它，前两条可以靠「一个永远返回命中的
   缓存」实现——那是假绿。lead 实测的第四行（新 task_key + 空磁盘目录 → 预读 5 次）
   就是这条的证据，所以本文件把它也钉成常驻用例。

## 分类方法（这条最容易测出自拍假数）

预读请求的判据是 **system 首行指纹**，不是 `json_mode`：

    messages[0].content 的 strip 前缀 == bili_scribe.core.summarizer.MAP_SYSTEM

**不许用 `json_mode` 分预读/生成。** 角色三轮回里 critique 那一次是
`json_mode=False`，按它分桶会把 critique 误计成一次「预读」，于是暖路径看起来还剩
1 次预读、结论变成「缓存复用没生效」——lead 第一版就是这么错的，他原话：「我第一版
把 wengao 的 critique 误计成一次预读，差点得出『复用没生效』的错误结论」。
分类依据本身可审计这件事由
`test_the_request_kinds_are_identified_from_authoritative_prompts` 钉住：map 的
system 必须**整段等于** bs 的 `MAP_SYSTEM`，critique 的 system 必须**整段等于**
`roles.get_role("wengao").critique`（`bili_scribe/core/roles.py:119`）。
顺带记一条实测结构事实：**生成与修订共用同一份 system**（bs `:602` 传的还是
`system_prompt`），修订的阶段身份在 **user 消息**里——它必须以 `role.revise` 中
`{critique}` 占位符之前那段固定文字开头。所以「读 system 认阶段」在三轮回里只
认得出批判，认不出修订。

## 分段数不钉死

素材长度决定 `_split` 的段数（lead 第一版 32,649 字 → 6 段，第二版 27,249 字 → 5 段）。
所以用例只钉「冷路径 map >= 2 且暖路径 map == 0」，并且钉一条**次数守恒**：
`冷总次数 == 暖总次数 + 冷预读次数`。守恒式跟着素材长度走，不会因改素材而假红。

## 三个已知坑（lead 踩过，写在这里省下一个人的时间）

- `ad.refine` 的签名是 `refine(project, segments, previous, requirement, *, ...)`，
  **previous 在 requirement 前面**；写反了报 `'dict' object has no attribute 'strip'`。
- 假 LLM 的脚本**别用平铺列表**：预读只吃掉前几格，剩下的会喂给生成阶段，于是报
  「模型没返回可解析的 JSON」——那是探针自己的 bug 不是产品的。本文件的 `ReuseLLM`
  按请求内容现编返回，`script` 永远是空的。
- `requirement` 少于 4 字会被 `VALIDATION` 挡（「说一下你想怎么改（至少几个字）」）。

## 为什么不另造假对象

`ReuseLLM` **继承** `test_summary_parity.FakeLLM`，只替换「怎么决定回吐什么」，
记录用的 `requests` 字段与 parity 完全一致（多一个 `kind` 是分类派生量）。两个假对象
的差异会被读成产品不一致——这一点是 parity 文件头就定下的规矩。

## 档位

跑在**默认档**（`wb_parity_tiers.default_env`＝未设＝严格）。复用与档位无关，但档位
不干净会让宽松档的补救阶梯多插请求，次数守恒就量不准了。

## 一个实测出来的结构事实：两层互为兜底

牙齿证明（三个变体，输出贴在交付里）显出这件事：

- 只把内存层短路（`context = get_prepared(task_key)` → `context = None`）→ **只有**
  `test_generation_then_refine_pays_no_second_pre_reading` 一条红，请求次数那几条
  全绿——因为磁盘层接住了，`map` 仍是 0。
- 只把磁盘层短路（两处 `cache=cache` → `cache=None`）→ 只有跨任务那条红。
- 两层一起短路 → 4 条红，包括次数守恒。

所以「暖路径 0 预读」这个计数**分不清是哪层生效**，它只守得住「总共没重付」。
要分清，只有两个只有该层才会动的证据：磁盘的 `session_hits` 计数，和那句
「复用已存的预读笔记」（它只由 `prepare_material` 在磁盘命中时发）。这两样都钉在
主断言里，不是装饰——把它们删了，单层失效就变成看不见的退化。
# 已知产品债（lead 裁定归 vendor 另开单，本文件**不修也不写会红的用例**，只指位置）：
# bs 侧 `bili_scribe/infra/notecache.py` 的版本判定写死 `CACHE_VERSION=1`，而盘上条目是 v2，
# 于是那些历史条目永远读不到、`summary.notes.stats` 报的 files/bytes 数字真但意义为零。
# WB 侧自己那份 `video_workbench/infra/notecache.py:43` 是 2，实测 `~/.bili-scribe/notes-cache`
# 8 个条目全是 v2、读得到——所以这条只影响 bs 原件那套缓存，别把它当「复用没生效」。

"""

from __future__ import annotations

import json
import uuid

import pytest
from test_summary_parity import (
    CRITIQUE_NOTE,
    MAX_INPUT_CHARS,
    PAYLOAD,
    FakeLLM,
    QuietCtx,
    make_cfg,
)

# 档位 fixture 的唯一去处；假 LLM 与配置也从对账那边复用（见文件头「为什么不另造假对象」）。
from wb_parity_tiers import default_env as default_env  # noqa: F401  （pytest fixture，不是未使用导入）

from bili_scribe.core import roles as bs_roles
from bili_scribe.core import summarizer as bs_summarizer
from video_workbench.core import summarizer as ad
from video_workbench.infra.notecache import NoteCache

#: 预读请求的指纹。从 bs 权威常量取，不在测试里抄提示词正文。
MAP_PREFIX = bs_summarizer.MAP_SYSTEM.strip()[:10]

#: 素材正文里独有的一句话。笔记由假 LLM 产出，绝不会出现它——所以「复用传的是笔记
#: 而不是原文」这件事可以用它直接证明（见 test_reuse_carries_the_notes…）。
SEG_TAIL = "原文标记句只出现在素材正文里，用来把长度推过分段预读阈值。"
RAW_MARK = "原文标记句只出现在素材正文里"

#: 每段约 40 字，900 段 → 正文约 3.6 万字 > max_input_chars(24000)，必然触发分段预读。
SEGMENT_COUNT = 900

#: wengao 是三轮回角色：生成 → 自我批判 → 修订。复用命中时**只剩**这三次。
THREE_PASS_REQUESTS = 3

NOTE_TEXT = "这一段讲了三个论点，例子是复现用的固定句子。"


# ------------------------------------------------------------------------ 假 LLM


class ReuseLLM(FakeLLM):
    """`FakeLLM` 的「按请求现编」版：记录逻辑完全继承，脚本永远为空。

    要测的量就是请求次数，脚本长度事先不可知，所以不能预铺列表（见文件头坑 2）。
    """

    def __init__(self, model: str = "reuse-fake") -> None:
        super().__init__([], model=model)

    def chat(self, messages, temperature=None, max_tokens=None, json_mode=False, note=None, **kw):
        system = str(messages[0].get("content") or "")
        if system.strip().startswith(MAP_PREFIX):
            kind = "map"
        elif json_mode:
            kind = "json"
        else:
            kind = "critique"
        self.requests.append(
            {
                "messages": [dict(item) for item in messages],
                "json_mode": bool(json_mode),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "kind": kind,
            }
        )
        if kind == "map":
            return NOTE_TEXT
        if kind == "json":
            return json.dumps(PAYLOAD, ensure_ascii=False)
        return CRITIQUE_NOTE

    # --- 分类后的可观测计数（复用与否的唯一证据） -------------------------
    def count(self, kind: str) -> int:
        return sum(1 for item in self.requests if item["kind"] == kind)

    def total(self) -> int:
        return len(self.requests)

    def system_of(self, index: int) -> str:
        return str(self.requests[index]["messages"][0]["content"])

    def all_text(self) -> str:
        return "\n".join(
            str(item.get("content") or "")
            for record in self.requests
            for item in record["messages"]
        )


# ------------------------------------------------------------------- 素材与夹具


def _project(bvid: str = "BV1preparecheck") -> dict:
    return {
        "bvid": bvid,
        "aid": 7,
        "cid": 8,
        "page": 1,
        "page_count": 1,
        "title": "预读复用素材",
        "desc": "固定简介",
        "up_name": "UP复用",
        "up_mid": 9,
        "duration_sec": 3600,
        "tname": "知识",
        "tags": ["复用", "固定"],
    }


def _segments(count: int = SEGMENT_COUNT) -> list[dict]:
    return [
        {"start_ms": index * 4000, "text": f"第{index}段{SEG_TAIL}"}
        for index in range(1, count + 1)
    ]


def _fresh_key(label: str) -> str:
    """每个用例一对唯一 task_key。

    内存层是模块级字典（`_prepared`），跨用例会串。用唯一键隔离比去清内部字典好：
    后者是把测试绑到实现细节上，将来换成别的容器就红得没道理。
    """
    return f"reuse-{label}-{uuid.uuid4().hex[:8]}"


def _body_is_long_enough_to_need_pre_reading() -> int:
    """夹具自检：素材必须真的越过阈值。

    这条不是形式主义——素材一旦掉到阈值以下，`prepare_material` 直接原样返回、
    预读恒为 0，于是「暖路径 0 预读」永远绿却什么都没守（bs:404-406 那个早退）。
    """
    body = ad.segments_text(_segments())
    assert len(body) > MAX_INPUT_CHARS, (
        f"夹具失效：素材只有 {len(body)} 字，不超过 max_input_chars="
        f"{MAX_INPUT_CHARS}，分段预读根本不会触发，本文件的断言会全部变成空断言"
    )
    return len(body)


@pytest.fixture(autouse=True)
def _fixture_guard():
    _body_is_long_enough_to_need_pre_reading()
    yield


def _run(task_key: str, cache, label: str = "run"):
    llm = ReuseLLM()
    ctx = QuietCtx()
    out = ad.run(
        _project(),
        _segments(),
        client=llm,
        cfg=make_cfg(),
        ctx=ctx,
        role="wengao",
        cache=cache,
        task_key=task_key,
    )
    assert out["role"] == "wengao", f"{label}：角色不该被复用逻辑改写"
    return llm, ctx, out


def _refine(
    task_key: str,
    cache,
    previous,
    label: str = "refine",
    requirement: str = "把时间线再压缩一点，保留三个论点",
):
    llm = ReuseLLM()
    ctx = QuietCtx()
    out = ad.refine(
        _project(),
        _segments(),
        previous,
        requirement,
        client=llm,
        cfg=make_cfg(),
        ctx=ctx,
        role="wengao",
        cache=cache,
        task_key=task_key,
    )
    assert out["requirement"] == requirement.strip(), f"{label}：要求被改写了"
    return llm, ctx, out


# ============================================================ 主断言 1：内存层


def test_generation_then_refine_pays_no_second_pre_reading(default_env, tmp_path):
    """承诺 1：生成之后同一项目「再改一版」，一个预读请求都不发。

    这是用户最常走的路径：先点生成、不满意再改。`run()` 把预读结果连同**笔记版**
    bundle 存进内存（`:863`），`refine()` 命中它（`:932`）就根本不进
    `prepare_material`。所以这里除了 map==0，还钉「磁盘计数没动」——那是内存命中
    与磁盘命中的唯一区别，两层不是一回事。
    """
    cache = NoteCache(tmp_path / "notes")
    key = _fresh_key("gen-then-refine")

    cold, cold_ctx, first = _run(key, cache, label="首次生成")
    assert cold.count("map") >= 2, (
        f"前置不成立：首次生成只发了 {cold.count('map')} 次预读，素材没真的分段，"
        "后面的「0 预读」就没有意义"
    )
    assert "分段预读 1/" in "\n".join(cold_ctx.logs), "首次生成的日志里看不到分段预读进度"

    before = cache.stats()
    warm, warm_ctx, second = _refine(key, cache, first["summary"], label="再改一版")

    assert warm.count("map") == 0, (
        f"同一 task_key 二次打磨仍发了 {warm.count('map')} 次预读——内存层没接上"
    )
    assert warm.total() == THREE_PASS_REQUESTS, (
        f"复用命中后应当只剩三轮回那 {THREE_PASS_REQUESTS} 次，实发 {warm.total()} 次"
    )
    assert warm.count("json") == 2 and warm.count("critique") == 1, (
        "三轮回的形状变了：生成/批判/修订应是 json、非 json、json"
    )

    after = cache.stats()
    assert after["session_hits"] == before["session_hits"], "内存命中不该去查磁盘"
    assert after["session_misses"] == before["session_misses"], "内存命中不该去查磁盘"
    assert "复用已存的预读笔记" not in "\n".join(warm_ctx.logs), (
        "这句话只由 prepare_material 在**磁盘**命中时发出；它出现说明走的是磁盘层，内存层没生效"
    )
    # 复用不该改变产出：假 LLM 每次回的都是同一份 PAYLOAD，所以这里量的是
    # 「走笔记版 bundle 也能正常出稿」，不是内容比较。
    assert second["summary"]["one_line"] == PAYLOAD["one_line"]


def test_second_refine_on_the_same_task_key_matches_the_measured_baseline(default_env, tmp_path):
    """承诺 1 的 lead 原表复现：① 冷 refine 8 次 / ② 暖 refine 3 次，且次数守恒。

    守恒式 `冷总 == 暖总 + 冷预读` 是「复用省下的正好是预读那些次」的唯一算术证据；
    钉死 8/3 会随素材长度假红，所以只把 3（三轮回）钉死、把 8 留给守恒式。
    """
    cache = NoteCache(tmp_path / "notes")
    key = _fresh_key("refine-twice")

    first, _, produced = _refine(key, cache, dict(PAYLOAD), label="首次打磨")
    assert first.count("map") >= 2, "前置不成立：首次打磨没触发分段预读"
    cold_total = first.total()

    second, _, again = _refine(
        key, cache, produced["summary"], label="第二次打磨", requirement="再顺一遍措辞，别动结构"
    )
    assert second.count("map") == 0, f"第二次打磨仍发了 {second.count('map')} 次预读"
    assert second.total() == THREE_PASS_REQUESTS
    assert cold_total == second.total() + first.count("map"), (
        f"次数不守恒：冷 {cold_total} ≠ 暖 {second.total()} + 预读 {first.count('map')}"
    )
    # 省下的只有预读那几次；三轮回一次不少，产出照常出稿。
    assert again["summary"]["one_line"] == PAYLOAD["one_line"]


# ============================================================ 主断言 2：磁盘层


def test_a_reopened_project_hits_the_disk_and_sends_no_pre_reading(default_env, tmp_path):
    """承诺 2：同一个视频重开成第二个项目，直接命中磁盘、一次模型调用都不发。

    故意**新建一个 NoteCache 实例**指向同一个目录——这就是「进程重启」的等价物：
    内存层（模块级字典）换了新 task_key 必然未命中，能救它的只有磁盘。
    同时钉那句「复用已存的预读笔记」：它是 bs `:419` 在磁盘命中时发的进度文案，
    用户看得见，也是与内存层区分开的唯一正面证据。
    """
    root = tmp_path / "notes"
    first_key = _fresh_key("disk-first")
    second_key = _fresh_key("disk-second")

    cold, cold_ctx, produced = _run(first_key, NoteCache(root), label="项目一")
    assert cold.count("map") >= 2, "前置不成立：第一次没真的预读"
    entries_after_first = NoteCache(root).stats()["entries"]
    assert entries_after_first >= 1, "跑过预读却没写进磁盘缓存——那第二个项目根本无从命中"

    reopened = NoteCache(root)
    before = reopened.stats()
    warm, warm_ctx, again = _refine(
        second_key, reopened, produced["summary"], label="项目二（同磁盘目录）"
    )

    assert warm.count("map") == 0, (
        f"跨任务未命中磁盘：新 task_key 仍发了 {warm.count('map')} 次预读"
    )
    assert warm.total() == THREE_PASS_REQUESTS
    after = reopened.stats()
    assert after["session_hits"] == before["session_hits"] + 1, (
        "磁盘命中必须体现在计数上，否则「0 预读」可能是别的东西被短路了"
    )
    assert after["session_misses"] == before["session_misses"]
    logs = "\n".join(warm_ctx.logs)
    assert "复用已存的预读笔记" in logs, f"磁盘命中却没念出那句进度：{logs!r}"


def test_a_cold_disk_cache_must_pay_for_pre_reading_again(default_env, tmp_path):
    """承诺 3：冷缓存必须真的重付——这条是本文件的防假绿底座。

    没有它，前两条可以靠「一个永远返回命中的缓存」实现。lead 实测的第四行
    （新 task_key + 空磁盘目录 → 预读 5 次）就是这条。同一份素材在两个独立目录
    各付一次，而且两次付的**次数相同**，顺带钉住分段数只由素材与 cfg 决定。
    """
    cold_a, _, produced_a = _run(_fresh_key("cold-a"), NoteCache(tmp_path / "a"), label="冷目录 A")
    cold_b, _, produced_b = _run(_fresh_key("cold-b"), NoteCache(tmp_path / "b"), label="冷目录 B")

    assert cold_a.count("map") >= 2, "冷路径 A 没触发预读"
    assert cold_b.count("map") == cold_a.count("map"), (
        f"同一份素材在两个空目录分别预读 {cold_b.count('map')} / "
        f"{cold_a.count('map')} 次——分段数不该跟着缓存目录变"
    )
    assert produced_a["summary"] == produced_b["summary"], (
        "冷启动与冷启动的产出不一致：预读结果受缓存状态影响"
    )


# ============================================================ 支撑断言


def test_reuse_carries_the_notes_not_the_raw_transcript(default_env, tmp_path):
    """复用带过去的是**分段笔记**，不是素材原文。

    `RefineContext` 的注释（`:783-786`）写死了这个约定：bundle.body 已被换成
    笔记，第三方原文既不落库也不留着。这条用两个方向同时钉：
    - 暖路径的请求里必须出现「【第 1 段笔记】」（笔记确实在提示词里）；
    - 必须不出现 `RAW_MARK`（只存在于素材正文的标记句）。
    并且笔记段数 == 冷路径的预读次数——两个数不是一条断言的两个说法，而是
    「每次预读都留下一段笔记」这件事本身。
    """
    cache = NoteCache(tmp_path / "notes")
    key = _fresh_key("notes-not-raw")

    cold, _, produced = _run(key, cache, label="首次生成")
    map_calls = cold.count("map")
    assert "【第 1 段笔记】" in cold.all_text(), "冷路径的生成请求里就该带笔记"
    # 反空断言：标记句必须**确实**在冷路径出现过，否则「暖路径没有它」什么都证明不了
    # （一个从不出现的字符串永远「不在」任何文本里）。
    assert RAW_MARK in cold.all_text(), (
        "素材标记句连冷路径的预读请求里都没有——夹具失效，下面的反向断言会是空断言"
    )

    warm, _, _ = _refine(key, cache, produced["summary"], label="再改一版")
    assert RAW_MARK not in warm.all_text(), (
        "暖路径的请求里出现了素材原文标记句：复用的是原文，不是笔记"
    )
    # 笔记段数只跟**生成那一次**比：三轮回的批判与修订都各自带一份完整笔记
    # （bs `:590`/`:604` 的「原始素材」就是它），拿全路径的总出现次数比会得到 3 倍。
    generate = next(r for r in warm.requests if r["kind"] == "json")
    generate_text = "\n".join(str(item.get("content") or "") for item in generate["messages"])
    assert "【第 1 段笔记】" in generate_text, (
        "复用命中后生成请求里却没有笔记——带过去的不是笔记版 bundle"
    )
    assert generate_text.count("【第 ") == map_calls, (
        f"生成请求里的笔记段数 {generate_text.count('【第 ')} 与预读次数 {map_calls} "
        "对不上：每次预读应当留下一段笔记"
    )


def test_without_task_key_and_cache_nothing_is_reused(default_env, tmp_path):
    """不接线就不复用：老行为必须原样保留。

    `task_key=""` 时 `remember_prepared`/`get_prepared` 都直接返回（`:804`/`:816`），
    `cache=None` 时不碰磁盘。这条钉的是「复用是显式接线的」——否则某次改动把
    内存字典改成全局共享，跨项目会串素材，而上面几条用例全都看不出来。
    """
    first, _, produced = _run("", None, label="无键第一次")
    assert first.count("map") >= 2
    assert first.total() == first.count("map") + THREE_PASS_REQUESTS

    second, _, _ = _refine("", None, produced["summary"], label="无键第二次")
    assert second.count("map") >= 2, "不传 task_key 却复用了上一次的预读——内存层变成了隐式全局状态"


def test_the_request_kinds_are_identified_from_authoritative_prompts(default_env, tmp_path):
    """分类依据本身要可审计（防止「按指纹分桶」变成自证）。

    `map` / `critique` / `revise` 三个 kind 都对着 bs 权威提示词核一次：
    - map 的 system 必须**整段等于** `bili_scribe.core.summarizer.MAP_SYSTEM`
      （不只是前 10 字相同——副本与原件逐字节锁着，这里连正文一起对上）；
    - critique 的 system 必须**整段等于** `roles.get_role("wengao").critique`；
    - 修订那次只能从 **user 消息**认（生成与修订共用 system，见文件头）。
    这三条成立，`json_mode` 分桶为什么不行就不再是我的说法，而是可核对的事实：
    critique 也是 `json_mode=False`，按它分桶就会把 1 次批判数成 1 次预读。
    """
    cache = NoteCache(tmp_path / "notes")
    cold, _, produced = _run(_fresh_key("kinds"), cache, label="首次生成")

    map_records = [r for r in cold.requests if r["kind"] == "map"]
    assert map_records, "没有请求被认成预读"
    for record in map_records:
        assert str(record["messages"][0]["content"]) == bs_summarizer.MAP_SYSTEM, (
            "预读请求的 system 与 bs 的 MAP_SYSTEM 不逐字相同，分类指纹不可信"
        )
    assert all(not r["json_mode"] for r in map_records), "预读请求应当不是 JSON 模式"

    critique = [r for r in cold.requests if r["kind"] == "critique"]
    assert len(critique) == 1, f"三轮回角色应当恰好一次自我批判，实得 {len(critique)}"
    assert str(critique[0]["messages"][0]["content"]) == bs_roles.get_role("wengao").critique, (
        "非 JSON 那次请求的 system 不等于 wengao 的 critique 提示词——它就是被误计成"
        "「预读」的那一次：按 json_mode 分桶时批判与预读同为非 JSON"
    )
    assert critique[0]["json_mode"] is False

    json_records = [r for r in cold.requests if r["kind"] == "json"]
    assert len(json_records) == 2, "生成与修订两次 JSON 请求"
    # 生成与修订**共用同一个 system**（bs `:602` 用的还是 `system_prompt`），所以阶段
    # 身份只能从 user 消息读：修订那次以角色的 revise 提示词开头（`{critique}` 之前
    # 那段固定文字，从权威提示词里切出来，不在测试里抄正文）。
    assert str(json_records[0]["messages"][0]["content"]) == str(
        json_records[1]["messages"][0]["content"]
    ), "生成与修订的 system 应当是同一份角色提示词；不一样说明三轮回的结构变了"
    revise_head = bs_roles.get_role("wengao").revise.split("{", 1)[0].strip()
    assert revise_head, "wengao 的 revise 提示词开头就是占位符，取不到阶段指纹"
    revise_user = str(json_records[1]["messages"][1]["content"])
    assert revise_user.startswith(revise_head), (
        f"第二次 JSON 请求的 user 消息不是以 revise 开头：{revise_user[:40]!r}"
    )
    assert revise_head not in str(json_records[0]["messages"][1]["content"]), (
        "生成阶段不该带修订指令"
    )
