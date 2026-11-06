"""P4-06 发布记录与限流 · 真库验收（不联网、绝不真发）。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_records.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import publisher as core  # noqa: E402

TITLE = "P4-06 记录验收"


def iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="build/accept/p2.yaml")
    args = ap.parse_args()

    service = WorkbenchService(config_path=args.config)
    # 门禁第二关要读登录态：验收不登录也不想让它永远停在 todo，
    # 就在服务层替一个"已登录"（只影响这一关的判断，不发任何请求）。
    service._gateway.is_logged_in = lambda: True  # type: ignore[assignment]
    # publish.publish 会先取 client 再发请求；验收不联网，给它一个空壳，
    # 真正的请求被下面 core.post_note 的替身拦住。
    service._gateway.client = lambda: object()  # type: ignore[assignment]
    repo = service.repo
    checks: list[tuple[str, bool, str]] = []

    def want(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def call(method: str, params: dict | None = None) -> dict:
        return service.router.dispatch(method, params or {}, "acc")

    project = repo.project_create(
        title=TITLE,
        root_dir=str(Path(service.cfg.media_root) / "_accept_p406"),
        bvid="BV1recaccept1",
        aid=556677,
        up_name="验收UP",
    )
    pid = project["id"]

    # 三份记录：发过 / 撤过 / 失败
    published = repo.publish_note_upsert(
        pid, bvid="BV1recaccept1", aid=556677, title="发出去的那条", summary="摘要", content="[]"
    )
    repo.publish_set_status(published["id"], "published", note_id="note_acc_p")
    repo._exec(
        "UPDATE wb_publish_note SET published_at=? WHERE id=?", (iso(minutes=30), published["id"])
    )

    withdrawn = repo.publish_note_upsert(
        pid, bvid="BV1recaccept1", aid=556677, title="撤回来的那条", summary="摘要", content="[]"
    )
    repo.publish_set_status(withdrawn["id"], "published", note_id="note_acc_w")
    repo.publish_set_status(withdrawn["id"], "withdrawn")
    repo._exec(
        "UPDATE wb_publish_note SET published_at=?, withdrawn_at=? WHERE id=?",
        (iso(days=3), iso(seconds=1), withdrawn["id"]),
    )

    failed = repo.publish_note_upsert(
        pid, bvid="BV1recaccept1", aid=556677, title="失败的那条", summary="摘要", content="[]"
    )
    repo.publish_set_status(failed["id"], "failed", error="[79508] 该视频已存在你创建的笔记")
    repo._exec("UPDATE wb_publish_note SET updated_at=? WHERE id=?", (iso(hours=2), failed["id"]))

    draft = repo.publish_note_upsert(
        pid,
        bvid="BV1recaccept1",
        aid=556677,
        title="还没发的那条",
        summary="摘要",
        content='[{"insert":"这一段的正文够长，内容关要能过。"},{"insert":"\\n"}]',
    )

    try:
        # ---------------------------------------------------- 记录列表
        out = call("publish.records", {"limit": 20})
        want("publish.records 成功", out.get("ok") is True, out.get("error", {}).get("message", ""))
        data = out.get("data") or {}
        rows = data.get("records") or []
        mine = [row for row in rows if row.get("project_id") == pid]
        want("记录里三条都在（草稿不算）", len(mine) == 3, f"实际 {len(mine)} 条")
        want(
            "计数分开给（发过/撤过/失败）",
            data.get("counts", {}).get("published", 0) >= 1
            and data.get("counts", {}).get("withdrawn", 0) >= 1
            and data.get("counts", {}).get("failed", 0) >= 1,
            json.dumps(data.get("counts"), ensure_ascii=False),
        )
        want(
            "刚撤回的排在最前（按最后一次动作）",
            mine[0].get("title") == "撤回来的那条",
            str(mine[0].get("title") if mine else ""),
        )
        want("记录里没有正文（列表不该塞整篇）", all("content" not in row for row in mine), "")
        want(
            "记录带视频链接",
            any(str(row.get("url", "")).endswith("BV1recaccept1") for row in mine),
            "",
        )
        want(
            "状态有中文文案",
            {row.get("status_text") for row in mine} >= {"已发布", "已撤回", "发布失败"},
            "",
        )

        # ---------------------------------------------------- 限流：刚发过
        repo._exec(
            "UPDATE wb_publish_note SET published_at=?, bvid=? WHERE id=?",
            (iso(seconds=15), "BV1recother99", published["id"]),
        )
        gate = call("publish.get", {"id": draft["id"], "confirmed": True}).get("data") or {}
        report = gate.get("gates") or {}
        want("刚发过 → 不能发", report.get("ready") is False, json.dumps(report.get("blockers")))
        want("被卡的是频率关", report.get("blockers") == ["pace"], str(report.get("blockers")))
        wait = int(report.get("wait_seconds") or 0)
        want("给出了剩余等待秒数", 30 <= wait <= 46, f"wait_seconds={wait}")
        pace = [g for g in report.get("gates", []) if g.get("id") == "pace"][0]
        want("理由写明还要等几秒", "还要等" in pace.get("reason", ""), pace.get("reason", ""))

        # ---------------------------------------------------- 时间够了但同视频已有笔记
        repo._exec(
            "UPDATE wb_publish_note SET published_at=?, bvid=? WHERE id=?",
            (iso(minutes=10), "BV1recaccept1", published["id"]),
        )
        gate2 = call("publish.get", {"id": draft["id"], "confirmed": True}).get("data") or {}
        report2 = gate2.get("gates") or {}
        pace2 = [g for g in report2.get("gates", []) if g.get("id") == "pace"][0]
        want("同视频已有笔记 → 仍然不能发", report2.get("ready") is False, "")
        want(
            "理由指向重复而不是限流",
            "已经发过" in pace2.get("reason", ""),
            pace2.get("reason", ""),
        )

        # ---------------------------------------------------- 撤回之后可以再发
        repo._exec(
            "UPDATE wb_publish_note SET status='withdrawn', withdrawn_at=? WHERE id=?",
            (iso(seconds=1), published["id"]),
        )
        gate3 = call("publish.get", {"id": draft["id"], "confirmed": True}).get("data") or {}
        want("撤回后三关放行", (gate3.get("gates") or {}).get("ready") is True, "")

        # ---------------------------------------------------- 79508 文案 + 失败留记录
        exc = type("E", (Exception,), {"code": 79508, "message": "该视频已存在你创建的笔记"})()
        text = core.note_error_text(exc)
        want("79508 说明去删旧笔记", "删掉旧的那条" in text and "不会覆盖" in text, text)
        want("79508 保留原始码", "79508" in text, text)

        def boom(*_a, **_k):
            # post_note 的真实现会把 BiliError 翻成 WBError；替身也得走同一条路，
            # 否则服务层的"失败保留草稿"分支根本不会被执行到。
            raise WBError("BILI_ERROR", core.note_error_text(exc), retryable=False)

        original = core.post_note
        core.post_note = boom  # type: ignore[assignment]
        try:
            sent = call("publish.publish", {"id": draft["id"], "confirmed": True})
        finally:
            core.post_note = original  # type: ignore[assignment]
        want("79508 发布失败", sent.get("ok") is False, "")
        row = repo.publish_get(draft["id"]) or {}
        want("失败后状态是 failed", row.get("status") == "failed", str(row.get("status")))
        want(
            "失败原因入库且是人话",
            "删掉旧的那条" in str(row.get("error", "")),
            str(row.get("error", "")),
        )
        want("失败后草稿正文没丢", str(row.get("content", "")) != "", "")

        # ---------------------------------------------------- 边界
        want(
            "limit 夹到上限（不报错）",
            call("publish.records", {"limit": 9999}).get("ok") is True,
            "",
        )
        bad = call("app.open_url", {"url": "file:///C:/Windows/win.ini"})
        want(
            "open_url 拒掉非 http",
            bad.get("ok") is False and bad["error"]["code"] == "VALIDATION",
            "",
        )

    finally:
        for note_pk in (
            published["id"],
            withdrawn["id"],
            failed["id"],
            draft["id"],
        ):
            repo.publish_delete(note_pk)
        repo._exec("DELETE FROM wb_project WHERE id=?", (pid,))
        service.close()

    print("=" * 72)
    bad = 0
    for name, ok, detail in checks:
        print(
            ("[ok]   " if ok else "[FAIL] ") + name + ("" if ok or not detail else f" —— {detail}")
        )
        if not ok:
            bad += 1
    print("-" * 72)
    print(f"{len(checks) - bad}/{len(checks)} 项通过")
    print("P4-06 发布记录与限流验收：" + ("全部通过" if not bad else f"{bad} 项未通过"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
