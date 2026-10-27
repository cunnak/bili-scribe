"""定位「迭代时 FileNotFoundError」用的最小复现（逐步版）。

背景：阶段 2 活体验证第一程 B 里，迭代那一版在 1.5 秒内就失败了，库里留下
`FileNotFoundError(2, 'No such file or directory')`——**连文件名都没有**，因为
`{exc!r}` 只打参数、不打 .filename。所以这里把 `_run_refine` 的每一步拆开跑一遍，
每步单独 try，把完整栈打出来。

只用假模型（不花钱），其余全是真的：真读库、真抓 B站 抽帧。
库和帧缓存都指向临时副本，不碰用户的真实数据。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# 脚本就在项目根：以前它们在工作区根、项目在子目录 bili-scribe/ 里，
# 迁移之后那个子目录没了，再拼一次会指向不存在的路径。
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bili_scribe.app.main import AppState, RefineContext  # noqa: E402
from bili_scribe.config import AppConfig  # noqa: E402
from bili_scribe.core import composer, frames as frames_mod, summarizer  # noqa: E402
from bili_scribe.core.collector import Bundle  # noqa: E402
from bili_scribe.infra.llm import Usage  # noqa: E402

TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "e01d93709aa7"
REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"

tmp = Path(tempfile.mkdtemp(prefix="bs-debug-"))
# ⚠️ 库是 WAL 模式：最近的写入还在 -wal 里，只拷 .db 会丢掉它们
# （第一次就是这么踩的：拷过去的库里压根没有那个任务）。三个文件一起拷。
for suffix in ("", "-wal", "-shm"):
    src = Path(str(REAL_DB) + suffix)
    if src.exists():
        shutil.copy2(src, tmp / (REAL_DB.name + suffix))
        print(f"拷了 {src.name}（{src.stat().st_size // 1024} KB）")

cfg = AppConfig.load(PROJECT / "config.yaml")
# db_path / key_path 是只读属性（从 storage 段推导），所以改 storage
cfg.storage.db_path = str(tmp / "bili-scribe.db")
cfg.storage.key_path = str(Path.home() / ".bili-scribe" / "key.bin")  # 只读，用来解密凭据
state = AppState(cfg)

task = state.store.get_task(TASK_ID)
assert task, f"库里没有 {TASK_ID}"
print(f"任务 {TASK_ID}：{(task['title'] or '')[:30]} / aid={task['aid']} / 状态 {task['status']}")
print(f"临时库：{tmp}")
print(f"帧缓存根：{Path(cfg.db_path).parent / 'frames'}（存在={ (Path(cfg.db_path).parent / 'frames').exists() }）")


def step(name, fn):
    print(f"\n--- {name} ---")
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 抛了 {type(exc).__name__}")
        print(f"  str()  = {exc}")
        print(f"  repr() = {exc!r}")
        print(f"  filename = {getattr(exc, 'filename', None)!r}")
        traceback.print_exc()
        return None
    print("✓ 通过")
    return out


class FakeLlm:
    def __init__(self) -> None:
        self.cfg = type("C", (), {"model": "fake-model"})()
        self.usage = Usage()

    def chat(self, messages, **_kw):
        self.usage.calls += 1
        return (
            '{"one_line":"改后的一句话","summary_md":"改后的正文","key_points":["甲"],'
            '"timeline":[],"caution":"","comment_short":"改后的短评","revise_note":"只改了开头"}'
        )


bundle = Bundle(
    bvid=task["bvid"] or "",
    aid=task["aid"] or 0,
    cid=task["cid"] or 0,
    page=task["page"] or 1,
    page_count=1,
    title=task["title"] or "",
    desc="",
    up_name=task["up_name"] or "",
    up_mid=task["up_mid"] or 0,
    duration=0,
    tname="",
    source_kind=task["source_kind"] or "subtitle_ai",
    source_label=task["source_label"] or "",
    source_trust=task["source_trust"] or "",
    source_detail="",
    body="（复现用的假素材，不会真的发给模型）",
)
ctx = RefineContext(bundle=bundle, material=summarizer.PreparedMaterial(body="假笔记", chunks=1))
llm = FakeLlm()

# 顺序与 _run_refine 严格一致：抽帧 → 归约 → 组装 → 落版本
frameset = step(
    "A 抽帧（与 _run_refine 同一行调用）",
    lambda: frames_mod.collect_frames(
        state.new_client(), cfg.collect.frames, bundle.aid, cfg.db_path,
        progress=lambda s, m: print(f"    [{s}] {m}"),
    ),
)
summary = step(
    "B 归约（假模型）",
    lambda: summarizer.summarize(
        bundle, llm, cfg, role_key="wengao", frameset=frameset,
        prepared=ctx.material,
        refinement=summarizer.Refinement(requirement="只改开头那句", previous='{"one_line":"原版"}'),
    ),
)
step("C 组装", lambda: (composer.build_comment(summary, bundle, cfg.publish),
                      composer.build_preview(summary and bundle, summary, cfg.publish)))
step(
    "D 落版本",
    lambda: state.store.record_version(
        TASK_ID, source="refine", requirement="只改开头那句",
        summary_json=summary.to_dict(), preview_md="P", comment_text="C", role="wengao",
        metrics={"usage": llm.usage.to_dict()}, status="ready", progress="ok",
    ),
)

print("\n--- E 清帧缓存（_run_refine 的 finally）---")
try:
    n = frames_mod.clear_cache(cfg.db_path, bundle.aid)
    still = (Path(cfg.db_path).parent / "frames").exists()
    print(f"clear_cache → 删了 {n} 个文件；帧缓存根还在吗：{still}")
except Exception:
    traceback.print_exc()
