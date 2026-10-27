"""活体验证「超强思考模式总结」：真模型、真采集，但**不碰使用者的库**。

为什么必须跑这一遍：这个角色的价值全在**文体与结构**上，而这两样离线测不出来
——「四问有没有真的成为骨架」「套话有没有被删掉」「归纳出来的问题有没有标明依据」
只能看真产出。上一版的发布说明里就写了：这一条无法自动化验证。

做法：库用快照副本（凭据在里面，所以能真采集），端口另开，跑完不影响使用者的数据。
只跑**一次生成**（三轮回），不做迭代——先把第一版看清楚。

    python role-shensi-live.py
"""

from __future__ import annotations

import json
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

from bili_scribe.app.main import create_app  # noqa: E402
from bili_scribe.config import AppConfig  # noqa: E402

# 视频号走命令行：脚本要进公开仓库，写死一个真实 bvid 等于公开某人的观看记录。
VIDEO = sys.argv[1] if len(sys.argv) > 1 else ""
if not VIDEO:
    sys.exit("用法：python role-shensi-live.py <BV号>")
PORT = 8823
BASE = f"http://127.0.0.1:{PORT}"
REAL_DB = Path.home() / ".bili-scribe" / "bili-scribe.db"

# 快照真实库：凭据在里面，所以能真采集；跑的是副本，使用者的库不受影响
tmp = Path(tempfile.mkdtemp(prefix="bs-shensi-"))
src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
dst = sqlite3.connect(str(tmp / REAL_DB.name))
src.backup(dst)
dst.close()
src.close()

cfg = AppConfig.load(PROJECT / "config.yaml")
cfg.storage.db_path = str(tmp / REAL_DB.name)
cfg.storage.key_path = str(Path.home() / ".bili-scribe" / "key.bin")
cfg.server.host = "127.0.0.1"
cfg.server.port = PORT
cfg.collect.frames.enabled = False   # 这一遍只看文体，不掺画面

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
    resp = probe.get(BASE + path, timeout=30)
    assert resp.status_code == 200, f"GET {path} → {resp.status_code} {resp.text[:160]}"
    return resp.json()


print(f"提交 {VIDEO}，角色＝超强思考模式总结（真人真模型，只跑一次）")
code = probe.post(BASE + "/api/task", json={"raw_input": VIDEO, "role": "shensi"}, timeout=30)
assert code.status_code == 200, code.text[:200]
task_id = code.json()["task_id"]
print(f"任务 {task_id}")

deadline = time.time() + 600
trail: list[str] = []
last = ""
while time.time() < deadline:
    data = get(f"/api/task/{task_id}")
    if data["status"] != last:
        trail.append(data["status"])
        last = data["status"]
    if data["status"] in ("ready", "failed"):
        break
    time.sleep(2)
print(f"轨迹：{' → '.join(trail)}（{len(trail)} 个状态）")
assert data["status"] == "ready", data.get("error")

doc = get(f"/api/task/{task_id}/versions/1")
summary = doc["summary_json"]
usage = get(f"/api/task/{task_id}/versions")["total"]

out = Path(__file__).resolve().parent / "role-shensi-live.txt"
lines: list[str] = []
lines.append("=" * 74)
lines.append("超强思考模式总结 · 活体验证（真模型 / 真采集 / 库用快照）")
lines.append("=" * 74)
lines.append(f"视频：{VIDEO}    角色：{data.get('role', 'shensi')}    消耗：{usage['calls']} 次调用 / {usage['total_tokens']} tokens")
lines.append(f"素材：{data.get('source_label')}    时间：{summary.get('created_at') or ''}")
lines.append("")
lines.append(f"—— one_line ——\n{summary['one_line']}")
lines.append("")
lines.append(f"—— summary_md（正文）——\n{summary['summary_md']}")
lines.append("")
lines.append(f"—— key_points ——\n{json.dumps(summary.get('key_points'), ensure_ascii=False, indent=1)}")
lines.append("")
lines.append(f"—— caution ——\n{summary.get('caution')}")
lines.append("")
lines.append(f"—— comment_short（会发到评论区的那段，{len(summary.get('comment_short') or '')} 字）——\n{summary.get('comment_short')}")
lines.append("")
lines.append(f"—— revise_note ——\n{summary.get('revise_note')}")
text = "\n".join(lines)
out.write_text(text, encoding="utf-8")
print(text)
print(f"\n已写入 {out.name}")

# 机械检查：四问骨架与套话形态
md = summary.get("summary_md") or ""
print("\n=== 机械检查（只查得动形状的那些）===")
for section in ("## 作者提出的问题", "## 内容的实质", "## 他给出的解法", "## 他发现了什么"):
    print(f"  {'有' if section in md else '缺！'}  {section}")
banned = ["接下来我们", "值得注意的是", "综上所述", "深刻地揭示了", "令人深思",
          "不仅仅是", "发人深省", "值得反思", "在这个信息爆炸"]
hit = [b for b in banned if b in md]
print(f"  套话形态命中：{hit or '无 ✓'}")
print(f"  正文 {len(md)} 字 / 要点 {len(summary.get('key_points') or [])} 条")
server.should_exit = True
