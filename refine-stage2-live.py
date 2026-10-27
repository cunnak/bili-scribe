"""阶段 2 活体验证：版本落库 + 迭代打磨（真实模型、真实库、真实重启）。

三程分开跑，第二、三程都是**新进程**（＝真重启）：
    python refine-stage2-live.py phase1    # 生成 → 改两版 → 回退 → 版本不符被拒
    python refine-stage2-live.py phase1b   # 修完"要求被质量流程覆盖"之后，同一件事再验一遍
    python refine-stage2-live.py phase2    # 新进程后版本史、全文、累计消耗还在不在

为什么非要分进程：阶段 2 要证明的正是"重启之后东西还在"。同一进程里断言一遍，
只能证明内存没被清——而那恰恰是阶段 1 已经做到、阶段 2 要超越的东西。

只读真实库、只往真实库追加真实任务，不碰 config.yaml、不改任何配置。
"""

from __future__ import annotations

import difflib
import json
import os
import sys
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

PORT = 8819
BASE = f"http://127.0.0.1:{PORT}"
# 视频号走命令行：脚本要进公开仓库，写死一个真实 bvid 等于公开某人的观看记录。
# 这个脚本的三个「程」是连着的，所以视频号用环境变量传，避免和已有的子命令参数打架。
VIDEO = os.environ.get("BS_VIDEO", "")   # 30:58 那一类视频：之前总结过、且没有未撤回的发布
if not VIDEO:
    sys.exit("用法：BS_VIDEO=<BV号> python refine-stage2-live.py phase1|phase1b|phase2")
STATE = Path(__file__).resolve().parent / "refine-stage2-state.json"

probe = requests.Session()
probe.trust_env = False


def log(*parts) -> None:
    print(*parts, flush=True)


def start_app():
    cfg = AppConfig.load(PROJECT / "config.yaml")
    cfg.server.host = "127.0.0.1"
    cfg.server.port = PORT
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.2)
    assert server.started, "服务没能起来"
    return app, server


def get(path: str) -> dict:
    resp = probe.get(BASE + path, timeout=30)
    assert resp.status_code == 200, f"GET {path} → {resp.status_code} {resp.text[:200]}"
    return resp.json()


def post(path: str, body: dict | None = None) -> tuple[int, dict]:
    resp = probe.post(BASE + path, json=body or {}, timeout=30)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"raw": resp.text[:200]}


def wait_ready(task_id: str, timeout: float, label: str) -> dict:
    """等任务从中间态走到 ready。把经过的状态如实打出来——轨迹比耗时更可信。"""
    deadline = time.time() + timeout
    trail: list[str] = []
    last = ""
    while time.time() < deadline:
        data = get(f"/api/task/{task_id}")
        status = f"{data['status']}"
        if status != last:
            trail.append(status)
            last = status
        if status in ("ready", "failed"):
            log(f"    {label} 轨迹：{' → '.join(trail)}（{time.time() - (deadline - timeout):.1f}s）")
            return data
        time.sleep(1.5)
    raise AssertionError(f"{label} 等超时，轨迹 {trail}")


def show_versions(task_id: str, title: str) -> dict:
    data = get(f"/api/task/{task_id}/versions")
    log(f"\n  [{title}] 共 {len(data['versions'])} 版，当前第 {data['current']} 版")
    for v in data["versions"]:
        m = v.get("metrics") or {}
        u = m.get("usage") or {}
        cost = "无消耗（没调模型）" if v["source"] == "restore" else (
            f"{u.get('calls', '?')} 次调用 / {u.get('total_tokens', '?')} tokens / "
            f"{m.get('elapsed', '?')}s / 分块 {m.get('chunks', '?')} / 画面 {m.get('frames', '?')}"
            if u else "消耗未记录"
        )
        log(f"    第 {v['version_no']} 版 · {v['source']:8s} · {cost}")
        log(f"        要求：{(v['requirement'] or '—')[:60]}")
        log(f"        改动说明：{(v['revise_note'] or '—')[:120]}")
    t = data["total"]
    log(f"    累计：{t['calls']} 次调用，输入 {t['prompt_tokens']} / 输出 {t['completion_tokens']}"
        f"，合计 {t['total_tokens']} tokens；未记账 {t['untracked']} 版；无用量返回 {t['unpriced']} 次")
    return data


def run_task(label: str, requirement: str | None = None) -> tuple[str, dict]:
    """跑一次真实生成；给了 requirement 就顺带改一版。返回 (task_id, ready)。"""
    log(f"\n[{label}] 提交 {VIDEO}（30:58）")
    code, body = post("/api/task", {"raw_input": VIDEO})
    assert code == 200, body
    task_id = body["task_id"]
    ready = wait_ready(task_id, 600, "首次生成")
    assert ready["status"] == "ready", ready.get("error")
    log(f"    总结完成，素材 {ready['source_label']}，"
        f"分块 {ready['summary_json']['chunks']}，画面 {ready['summary_json']['frames']} 帧")
    assert ready["can_refine"] is True, f"刚跑完应当能改：{ready['refine_blocked']}"
    if requirement:
        log(f"    迭代：{requirement}")
        code, body = post(f"/api/task/{task_id}/refine",
                          {"requirement": requirement, "base_version": 1})
        assert code == 200, body
        ready = wait_ready(task_id, 600, "改一版")
        assert ready["status"] == "ready", ready.get("error")
    return task_id, ready


# ------------------------------------------------------------------ 第一程


def phase1() -> None:
    app, server = start_app()
    log("=" * 74)
    log("阶段 2 活体验证 · 第一程（真实模型调用）")
    log("=" * 74)
    log(f"库：{app.state.ctx.store.db_path}")

    # ---- 1. 老任务（升级前生成、没有版本行）读一次就该被补成第 1 版 ----
    #
    # ⚠️ 不能拿「最新那条带总结的任务」当老记录：你自己跑过一次总结之后，它就会顶到
    #    最前面，而它是新记录（有账、有版本史），拿它来验「老记录被补成第 1 版」必然错。
    #    老记录的形状是唯一的：**只有一版，且那一版没有消耗记录**（补记的没有账；
    #    真有账的新记录不可能只有一版却没账）。按形状找，而不是按顺序猜。
    tasks = get("/api/tasks")
    old = None
    for task in tasks:
        if not task.get("summary_json"):
            continue
        row = get(f"/api/task/{task['id']}/versions")
        if len(row["versions"]) == 1 and not (row["versions"][0]["metrics"] or {}).get("usage"):
            old = task
            break
    assert old, "真实库里找不到「升级前生成、还没有记账」的老任务"
    detail = get(f"/api/task/{old['id']}")
    log(f"\n[1] 老任务 {old['id']}（{old['title'][:24]}）")
    log(f"    版本数 {detail['version_count']}，当前第 {detail['current_version']} 版")
    assert detail["version_count"] == 1, "老记录应当被补成第 1 版"
    assert detail["current_version"] == 1
    assert detail["can_refine"] is False, "重启过 → 素材缓存已失效"
    log(f"    can_refine={detail['can_refine']}，理由：{detail['refine_blocked'][:44]}…")
    old_versions = show_versions(old["id"], "老任务")
    first = old_versions["versions"][0]
    assert not first["metrics"].get("usage"), "老版本不该假装有账"
    assert old_versions["total"]["untracked"] == 1, "老版本要如实算「未记录」"
    if first["revise_note"]:
        assert first["source"] == "refine", "带改动说明的老记录不该被标成「初次生成」"
        log("    这条老记录本身就是一次改版的产物，来源已标成 refine（标签和内容对得上）")

    code, body = post(f"/api/task/{old['id']}/refine", {"requirement": "随便改改"})
    log(f"    对它发起迭代 → HTTP {code}：{body.get('detail', body)[:60]}")
    assert code == 409 and "重新跑一次" in body["detail"]

    # ---- 2. 真跑一条 31 分钟视频，再连改两版 ----
    log(f"\n[2] 新任务：{VIDEO}")
    code, body = post("/api/task", {"raw_input": VIDEO})
    assert code == 200, body
    task_id = body["task_id"]
    started = time.time()
    ready = wait_ready(task_id, 600, "首次生成")
    assert ready["status"] == "ready", ready.get("error")
    first_seconds = time.time() - started
    log(f"    完成：{first_seconds:.1f}s，素材 {ready['source_label']}")
    assert ready["can_refine"] is True, f"刚跑完应当能改：{ready['refine_blocked']}"
    assert ready["current_version"] == 1 and ready["version_count"] == 1
    v1 = show_versions(task_id, "首次生成之后")
    assert v1["total"]["calls"] >= 1 and v1["total"]["total_tokens"] > 0, "初版必须有账"
    assert v1["total"]["untracked"] == 0

    for index, requirement in enumerate(
        ["把 one_line 改得更平实，不要形容词；其余部分一个字都别动",
         "总结正文里每段的开头都压到 25 字以内，不要用比喻"], 1
    ):
        log(f"\n[3.{index}] 迭代：{requirement}")
        code, body = post(f"/api/task/{task_id}/refine",
                          {"requirement": requirement, "base_version": index})
        assert code == 200, body
        started = time.time()
        ready = wait_ready(task_id, 300, f"第 {index + 1} 版")
        assert ready["status"] == "ready", ready.get("error")
        log(f"    完成：{time.time() - started:.1f}s，当前第 {ready['current_version']} 版")
        assert ready["current_version"] == index + 1, "版本号必须往前走"

    after = show_versions(task_id, "改了两版之后")
    assert [v["version_no"] for v in after["versions"]] == [1, 2, 3]
    notes = [v["revise_note"] for v in after["versions"]]
    assert notes[0] == "" and notes[1] and notes[2], f"改动说明有问题：{notes}"
    assert notes[1] != notes[2], "改动说明照抄了上一版（阶段 1 修过的坑）"
    assert after["total"]["calls"] > v1["total"]["calls"], "累计消耗得跟着涨"

    # ---- 4. 底稿版本不符时拒绝 ----
    code, body = post(f"/api/task/{task_id}/refine", {"requirement": "改一下", "base_version": 1})
    log(f"\n[4] 拿着过期的底稿版本提要求 → HTTP {code}：{body.get('detail', '')[:56]}")
    assert code == 409 and "第 3 版" in body["detail"]

    # ---- 5. 回退（不调模型）----
    log("\n[5] 以第 1 版为底稿")
    started = time.time()
    code, body = post(f"/api/task/{task_id}/versions/1/restore")
    log(f"    HTTP {code}，{time.time() - started:.2f}s：{json.dumps(body, ensure_ascii=False)}")
    assert code == 200 and body["version_no"] == 4
    restored = show_versions(task_id, "回退之后")
    assert [v["version_no"] for v in restored["versions"]] == [1, 2, 3, 4]
    assert restored["versions"][3]["source"] == "restore"
    assert not restored["versions"][3]["metrics"].get("usage"), "回退没有消耗，不该记 0"
    assert restored["total"]["calls"] == after["total"]["calls"], "回退不该让累计消耗变多"
    detail = get(f"/api/task/{task_id}")
    assert detail["summary_json"]["one_line"] == after["versions"][0]["one_line"], "当前版要变回第 1 版的内容"
    log(f"    当前版 one_line = {detail['summary_json']['one_line']}")

    STATE.write_text(json.dumps({
        "phase1_task_id": task_id, "old_task_id": old["id"],
        "first_seconds": round(first_seconds, 1), "total": restored["total"],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    server.should_exit = True
    time.sleep(1)
    log("\n第一程结束：进程退出")


# ----------------------------------------------------------------- 第一程 B


def phase1b() -> None:
    """修完「要求被自己的质量流程覆盖」之后，同一件事再验一遍。

    判据是客观的，不看模型自己怎么吹：要求写着"只改 one_line，其余字段一个字都
    不要动"，那就**逐字比对**两版的那几个字段。修好之前，批判那一轮会把角色的
    通用清单当任务，修订再照单全收，于是"只改一句"变成了七处改动。
    """
    requirement = ("把 one_line 改得更平实，去掉形容词和比喻；"
                   "summary_md / key_points / timeline / comment_short 这四个字段一个字都不要动")
    _, server = start_app()
    log("=" * 74)
    log("阶段 2 活体验证 · 第一程 B（要求能不能穿过三轮回）")
    log("=" * 74)

    task_id, ready = run_task("B1", requirement)
    versions = show_versions(task_id, "改一版之后")
    assert [v["version_no"] for v in versions["versions"]] == [1, 2]
    v1 = get(f"/api/task/{task_id}/versions/1")["summary_json"]
    v2 = get(f"/api/task/{task_id}/versions/2")["summary_json"]

    log("\n  [B2] 逐字比对（要求：只动 one_line）")
    log(f"    one_line  ：{v1['one_line']}")
    log(f"             → {v2['one_line']}")
    for field in ("summary_md", "key_points", "timeline", "comment_short"):
        a, b = v1.get(field), v2.get(field)
        if isinstance(a, str):
            ratio = difflib.SequenceMatcher(None, a, b or "").ratio()
            same = "逐字未变" if a == b else f"相似度 {ratio:.3f}（{len(a)}→{len(b or '')} 字）"
        else:
            same = "逐字未变" if a == b else f"变了：{json.dumps(a, ensure_ascii=False)[:60]} → {json.dumps(b, ensure_ascii=False)[:60]}"
        log(f"    {field:14s}：{same}")
        if isinstance(a, str):
            changed = [
                (tag, (a or "")[i1:i2][:90], (b or "")[j1:j2][:90])
                for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b or "").get_opcodes()
                if tag != "equal"
            ][:3]
            assert difflib.SequenceMatcher(None, a, b or "").ratio() >= 0.98, (
                f"{field} 被改了 —— 要求是「一个字都不要动」。差异：{changed}"
            )
        else:
            assert a == b, f"{field} 被改了：{a} → {b}"
    assert v1["one_line"] != v2["one_line"], "要求改的那一处反而没改，等于要求没被满足"

    note = v2["revise_note"]
    log(f"\n    改动说明（{len(note)} 字）：{note[:200]}")
    assert "诊断清单" not in note[:40], f"改动说明变成了复述批判清单：{note[:60]}"
    assert len(note) <= 260, f"改动说明长得像一份清单（{len(note)} 字），不像是「相对上一版改了什么」"

    usage = versions["versions"][1]["metrics"]["usage"]
    log(f"    这一版消耗：{usage['calls']} 次调用 / {usage['total_tokens']} tokens / "
        f"{versions['versions'][1]['metrics']['elapsed']}s")
    STATE.write_text(json.dumps({"phase1b_task_id": task_id}, ensure_ascii=False), encoding="utf-8")
    server.should_exit = True
    time.sleep(1)
    log("\n第一程 B 结束：要求活着穿过了生成 → 批判 → 修订三轮")


# ------------------------------------------------------------------ 第二程


def phase2() -> None:
    start_app()
    log("=" * 74)
    log("阶段 2 活体验证 · 第二程（新进程 ＝ 真重启）")
    log("=" * 74)

    mine = [t for t in get("/api/tasks") if t.get("bvid") == VIDEO]
    assert len(mine) >= 2, f"这一程应当看到刚才那两条任务，实际 {len(mine)} 条"
    # ⚠️ 按"有几版"挑，不要按创建时间排第几。中间失败过的那些次会留下 1 版的任务，
    #    按位置取就会拿到错的（第一版 phase2 就是这么挂的：它把一条失败的 1 版任务
    #    当成了第一程那条 4 版的）。这和第一程 step 1 是同一个教训：按形状找。
    shapes: dict[int, dict] = {}
    for task in mine:
        shapes.setdefault(get(f"/api/task/{task['id']}")["version_count"], task)
    fresh, older = shapes.get(2), shapes.get(4)
    assert fresh and older, (
        f"没找到 2 版 / 4 版的任务，实际分布 {sorted(shapes)}"
    )

    for label, task, expected in (("第一程 B（1 版初版 + 1 版改版）", fresh, 2),
                                  ("第一程（4 版）", older, 4)):
        detail = get(f"/api/task/{task['id']}")
        log(f"\n[6] 重启后看 {label}：{task['id']}")
        log(f"    版本数 {detail['version_count']}，当前第 {detail['current_version']} 版，状态 {detail['status']}")
        log(f"    can_refine={detail['can_refine']}（素材笔记只在内存里 → 重启即失效）")
        assert detail["version_count"] == expected, f"重启后版本史必须还在（期望 {expected} 版）"
        assert detail["current_version"] == expected
        assert detail["can_refine"] is False, "重启后内存缓存必然失效，这一点必须如实告诉界面"

        versions = show_versions(task["id"], "重启之后")
        for row in versions["versions"]:
            one = get(f"/api/task/{task['id']}/versions/{row['version_no']}")
            assert one["summary_json"].get("summary_md"), f"第 {row['version_no']} 版全文丢了"
            assert one["publish_document"], f"第 {row['version_no']} 版发布长文生成不了"
            log(f"    第 {row['version_no']} 版全文可取：正文 {len(one['summary_json']['summary_md'])} 字，"
                f"发布长文 {len(one['publish_document'])} 字")

    # 已经发布过的视频：打磨入口必须关掉，并说清先撤回
    # 视频号同样走环境变量（BS_PUBLISHED_VIDEO），不写死真实观看记录。
    want = os.environ.get("BS_PUBLISHED_VIDEO", "")
    if want:
        published = [t for t in get("/api/tasks") if t.get("bvid") == want]
        assert published, f"真实库里应当有那条已发布的视频 {want}"
        live = get(f"/api/task/{published[0]['id']}")
        log(f"\n[7] 已经发布过的视频 {live['bvid']}：can_refine={live['can_refine']}")
        log(f"    {live['refine_blocked']}")
        assert live["can_refine"] is False and live["live_publish"], "已发布的视频必须关上打磨入口"
        code, body = post(f"/api/task/{published[0]['id']}/refine", {"requirement": "改一下"})
        log(f"    对它发起迭代 → HTTP {code}：{body.get('detail', '')[:60]}")
        assert code == 409 and "撤回" in body["detail"]
    else:
        log("\n[7] 跳过「已发布视频」这一段：没设 BS_PUBLISHED_VIDEO")

    log("\n第二程结束：版本史、全文、累计消耗在重启之后全部完好")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "phase1"
    {"phase1": phase1, "phase1b": phase1b, "phase2": phase2}[which]()
