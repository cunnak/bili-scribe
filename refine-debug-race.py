"""把第一程 B 的流程跑很多遍，抓那个「1.5 秒就失败」的 FileNotFoundError。

为什么要这个：活体验证里那次失败只在库里留下一句没有出处的
`FileNotFoundError(2, 'No such file or directory')`，而它是**偶发**的——
不能靠"再跑一次真实模型"去找它（一次两块多钱、还要等两分钟，而且不一定复现）。

所以把模型换成假的（不花钱），其余一模一样：真的读库、真的抓 B站 字幕与抽帧、
真的事件循环、真的后台线程、真的落版本。跑 5 遍，只要有一次版本数不对，
就把任务里的 error 与整条栈打出来。

一切都在临时副本里跑：库、帧缓存都不碰用户的真实数据。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# 脚本就在项目根：以前它们在工作区根、项目在子目录 bili-scribe/ 里，
# 迁移之后那个子目录没了，再拼一次会指向不存在的路径。
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests  # noqa: E402
import uvicorn  # noqa: E402

from bili_scribe.app.main import AppState, create_app  # noqa: E402
from bili_scribe.config import AppConfig  # noqa: E402
from bili_scribe.infra.llm import Usage  # noqa: E402

PORT = 8821
BASE = f"http://127.0.0.1:{PORT}"
# 视频号走命令行：脚本要进公开仓库，写死一个真实 bvid 等于公开某人的观看记录。
VIDEO = sys.argv[1] if len(sys.argv) > 1 else ""
if not VIDEO:
    sys.exit("用法：python refine-debug-race.py <BV号> [轮数]")
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"

tmp = Path(tempfile.mkdtemp(prefix="bs-race-"))
# 快照真实库。**不要直接拷 .db/-wal/-shm**：WAL 模式下三个文件互相关联，
# 分开拷出来的副本会被 SQLite 判成"只读库"（中途报 attempt to write a readonly
# database），而且最近的写入还可能丢在 -wal 里。备份 API 一次拿到一致的快照。
_src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
_dst = sqlite3.connect(str(tmp / REAL_DB.name))
_src.backup(_dst)
_dst.close()
_src.close()
print(f"快照真实库 → {tmp / REAL_DB.name}")


class FakeLlm:
    """假模型：让流程真的走完，但不产生任何费用。"""

    counter = 0

    def __init__(self) -> None:
        self.cfg = type("C", (), {"model": "fake-model"})()
        self.usage = Usage()

    def chat(self, messages, **_kw):
        self.usage.calls += 1
        FakeLlm.counter += 1
        return json.dumps(
            {
                "one_line": f"假模型第 {FakeLlm.counter} 次输出",
                "summary_md": "## 假正文\n这是复现用的假内容。",
                "key_points": ["甲", "乙"],
                "timeline": [],
                "caution": "",
                "comment_short": "假的短评",
                "revise_note": "只改了开头那句",
            },
            ensure_ascii=False,
        )


# 类级打桩：任务与迭代都走同一个假模型
AppState.new_llm = lambda self: FakeLlm()  # type: ignore[method-assign]

# ---- 追踪谁在动帧缓存 ----------------------------------------------------
# 已经不猜了：直接记录每次「收帧 / 清帧」的时间、线程和 aid，失败时对齐时间线即可。
import bili_scribe.core.frames as F  # noqa: E402

_t0 = time.time()


def _stamp() -> str:
    return f"t+{time.time() - _t0:6.2f}s [{threading.current_thread().name}]"


_orig_clear = F.clear_cache
_orig_collect = F.collect_frames


def traced_clear(db_path, aid):
    root = Path(db_path).parent / "frames"
    target = root / str(aid)
    before = f"目标目录存在={target.exists()}"
    n = _orig_clear(db_path, aid)
    print(f"      {_stamp()} clear_cache(aid={aid}) 删了 {n} 个文件（删前{before}）")
    return n


def traced_collect(client, frames_cfg, aid, db_path, progress=None):
    print(f"      {_stamp()} collect_frames(aid={aid}) 开始")
    try:
        out = _orig_collect(client, frames_cfg, aid, db_path, progress)
    finally:
        exists = (Path(db_path).parent / "frames" / str(aid)).exists()
        print(f"      {_stamp()} collect_frames(aid={aid}) 结束（目录还在={exists}）")
    return out


F.clear_cache = traced_clear        # type: ignore[assignment]
F.collect_frames = traced_collect   # type: ignore[assignment]

cfg = AppConfig.load(PROJECT / "config.yaml")
cfg.storage.db_path = str(tmp / "bili-scribe.db")
cfg.storage.key_path = str(Path.home() / ".bili-scribe" / "key.bin")
cfg.server.port = PORT

app = create_app(cfg)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.2)
assert server.started, "服务没起来"

probe = requests.Session()
probe.trust_env = False


def get(path: str) -> dict:
    resp = probe.get(BASE + path, timeout=60)
    assert resp.status_code == 200, f"GET {path} → {resp.status_code} {resp.text[:200]}"
    return resp.json()


def post(path: str, body: dict) -> tuple[int, dict]:
    resp = probe.post(BASE + path, json=body, timeout=60)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"raw": resp.text[:200]}


def wait_done(task_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    trail = []
    while time.time() < deadline:
        data = get(f"/api/task/{task_id}")
        status = data["status"]
        if not trail or trail[-1] != status:
            trail.append(status)
        if status in ("ready", "failed") and not data.get("refining"):
            data["_trail"] = trail
            return data
        time.sleep(0.2)
    data = get(f"/api/task/{task_id}")
    data["_trail"] = trail + ["（超时）"]
    return data


print(f"临时库：{tmp}")
print(f"跑 {ROUNDS} 轮：每轮 = 一次真实采集+假总结，再迭代一版\n")

failures = 0
for i in range(1, ROUNDS + 1):
    code, body = post("/api/task", {"raw_input": VIDEO})
    assert code == 200, body
    task_id = body["task_id"]
    ready = wait_done(task_id)
    v_after_first = get(f"/api/task/{task_id}/versions")
    print(f"[{i}] 首次生成 {'→'.join(ready['_trail'])}｜版本 {len(v_after_first['versions'])}")

    code, body = post(
        f"/api/task/{task_id}/refine",
        {"requirement": "把 one_line 改得更平实，其余字段一个字都不要动", "base_version": 1},
    )
    assert code == 200, body
    done = wait_done(task_id)
    versions = get(f"/api/task/{task_id}/versions")
    nums = [v["version_no"] for v in versions["versions"]]
    ok = nums == [1, 2]
    print(f"     迭代 {'→'.join(done['_trail'])}｜版本 {nums}{'' if ok else '  ✗ 不对'}")
    if not ok:
        failures += 1
        print(f"     status   = {done['status']}")
        print(f"     progress = {done['progress']}")
        print(f"     error    = {done['error']}")
        break
    # 换一个视频对象再来：同一 aid 反复跑能覆盖「清缓存 / 重建缓存」的先后顺序
    time.sleep(0.5)

print(f"\n{ROUNDS - failures}/{ROUNDS} 轮正常；失败 {failures} 次")
server.should_exit = True
time.sleep(0.5)
