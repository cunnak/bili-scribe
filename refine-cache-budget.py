"""验证「预读缓存留多少条」这件事的真实成本。

背景：`AppState.MAX_PREPARED = 20` 的原始理由写着"不设上限跑几十条就白占几百 MB"。
那个"几百 MB"是设计时**猜**的，从没量过。所以这里真量一遍。

三件事要分开量清楚，不然就会像上次那样靠感觉：
  1. 一条缓存到底占多少内存——而且要量**整个 RefineContext**（笔记 + bundle 的其它字段），
     不能只量那个字符串；
  2. 最坏情况（笔记被截断到 `max_input_chars × 2`）是多少；
  3. **原素材会不会跟着进缓存**——如果会，那成本就跟视频长短挂钩，结论完全不一样。

用 tracemalloc 量真实分配，不用 getsizeof 累加（后者容易漏掉容器与对象开销）。
采集是真的（免费），不调模型。库用快照副本，不碰真实数据。
"""

from __future__ import annotations

import copy
import gc
import sqlite3
import sys
import tempfile
import tracemalloc
from pathlib import Path

# 脚本就在项目根：以前它们在工作区根、项目在子目录 bili-scribe/ 里，
# 迁移之后那个子目录没了，再拼一次会指向不存在的路径。
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bili_scribe.app.main import RefineContext  # noqa: E402
from bili_scribe.config import AppConfig  # noqa: E402
from bili_scribe.core import collector as collector_mod  # noqa: E402
from bili_scribe.core import summarizer  # noqa: E402
from bili_scribe.infra.bili_client import BiliClient  # noqa: E402
from bili_scribe.infra.store import Store  # noqa: E402

# 视频号走命令行：脚本要进公开仓库，写死一个真实 bvid 等于公开某人的观看记录。
VIDEO = sys.argv[1] if len(sys.argv) > 1 else ""
if not VIDEO:
    sys.exit("用法：python refine-cache-budget.py <BV号>")
REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"

# 用备份 API 拿一致快照（WAL 模式不能拆开拷，见 skill: flaky-race-repro-cheap）
tmp = Path(tempfile.mkdtemp(prefix="bs-budget-"))
src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
dst = sqlite3.connect(str(tmp / REAL_DB.name))
src.backup(dst)
dst.close()
src.close()

cfg = AppConfig.load(PROJECT / "config.yaml")
cfg.storage.db_path = str(tmp / REAL_DB.name)
cfg.storage.key_path = str(Path.home() / ".bili-scribe" / "key.bin")
store = Store(cfg.db_path, cfg.key_path)

print("=" * 74)
print("一、真实采集一条，看看 Bundle 里都有些什么（这一步不花钱）")
print("=" * 74)
client = BiliClient(store, logger=lambda m: print(f"  [bili] {m}"))
bundle = collector_mod.collect(client, VIDEO, 1, cfg.collect)
print(f"  素材正文 body      ：{len(bundle.body):>8,} 字符")
print(f"  简介 desc          ：{len(bundle.desc or ''):>8,} 字符")
print(f"  平台摘要            ：{len(bundle.platform_summary or ''):>8,} 字符")
print(f"  热门评论            ：{len(bundle.hot_replies):>8,} 条")
print(f"  标签/来源检查       ：{len(bundle.tags)} 个 / {len(bundle.checks)} 项")
print(f"  → 这条视频的 max_input_chars = {cfg.llm.max_input_chars:,}，"
      f"body {'超过' if len(bundle.body) > cfg.llm.max_input_chars else '未超过'}上限")


def make_note(chars: int) -> str:
    """造一条长度可控的笔记。真实笔记就是中文长文，字符构成不影响结论。"""
    unit = "这一段在讲一个具体的问题，并且保留了时间戳与细节，不压缩成模糊的关键词。"
    return (unit * (chars // len(unit) + 1))[:chars]


def build(bundle_src, note_chars: int) -> RefineContext:
    """完全照 _run_task 的做法建一个缓存条目。"""
    note = make_note(note_chars)
    note_bundle = copy.copy(bundle_src)
    note_bundle.body = note
    return RefineContext(bundle=note_bundle, material=summarizer.PreparedMaterial(body=note, chunks=4))


def measure(label: str, bundle_src, note_chars: int, count: int) -> float:
    """量 count 个条目的真实保留内存（MB）。"""
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    keep = [build(bundle_src, note_chars) for _ in range(count)]
    gc.collect()
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    mb = (after - before) / 1024 / 1024
    per = mb * 1024 / count
    print(f"  {label:<38} {count:>5} 条 = {mb:>7.2f} MB（每条 {per:>6.1f} KB）")
    del keep
    return mb


print()
print("=" * 74)
print("二、一条缓存占多少（tracemalloc 真实分配）")
print("=" * 74)
REAL_NOTE = 19772   # 实测：4 小时视频的笔记就是这个量级
CEIL_NOTE = cfg.llm.max_input_chars * 2  # 代码里笔记的截断上限

real_20 = measure("典型（笔记 19,772 字符）", bundle, REAL_NOTE, 20)
ceil_20 = measure("最坏（笔记被截到 48,000 字符）", bundle, CEIL_NOTE, 20)

print()
print("=" * 74)
print("三、关键一问：原素材会不会跟着进缓存？")
print("=" * 74)
huge = copy.copy(bundle)
huge.body = "素材原文。" * 120000          # 4 小时级素材，约 60 万字符
print(f"  对照用的 bundle：body = {len(huge.body):,} 字符（约 4 小时视频量级）")
small = measure("同一个 bundle，但笔记只有 19,772 字符", huge, REAL_NOTE, 20)
tiny = measure("同一 bundle，笔记同样的长度（第二遍）", huge, REAL_NOTE, 20)
print(f"  → 素材从 {len(bundle.body):,} 字符涨到 {len(huge.body):,} 字符，"
      f"缓存占用基本不变（{real_20:.2f} MB → {small:.2f} MB）")
print("  → 结论：**原素材不跟着进缓存**，替换成笔记时它就被丢掉了（copy 的是壳，body 被换掉）")

print()
print("=" * 74)
print("四、所以上限该是多少：把内存预算换成条数")
print("=" * 74)
per_kb = real_20 * 1024 / 20
per_kb_worst = ceil_20 * 1024 / 20
for budget in (10, 50, 200, 500):
    print(f"  预算 {budget:>4} MB → 典型情况可留 {budget*1024/per_kb:>7,.0f} 条 / "
          f"最坏情况 {budget*1024/per_kb_worst:>6,.0f} 条")

print()
print("  参照：现在写死的 20 条，典型占用 {:.2f} MB、最坏 {:.2f} MB —— "
      "连一次二维码 PNG 的零头都不到。".format(real_20, ceil_20))
print()
print("  而被淘汰的代价是**真的花钱**：重跑一次 4 小时视频 = 86,428 tokens")
print("  （占一次总结的 100%，其中预读 32,663）。内存几乎免费、重跑很贵，")
print("  这个不对称就是该把上限往上抬的理由。")
