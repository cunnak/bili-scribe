"""P4-05 发布门禁与笔记发布 · 真库验收（假 B 站客户端）。

为什么用假客户端：发布是对真实账号的不可撤销动作，验收脚本绝不能真发一条笔记。
这里把 `publisher_core.post_note/withdraw_note` 换成替身，其余走真服务层、真数据库、
真门禁计算——能验的是「门禁怎么判、失败怎么保留草稿、成功怎么落 note_id、撤回怎么写」。

跑法：
  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_publish_do.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import publisher as publisher_core  # noqa: E402

OK = []
BAD = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        OK.append(name)
        print(f"[ok]   {name}" + (f" —— {detail}" if detail else ""))
    else:
        BAD.append(name)
        print(f"[FAIL] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def call(service: WorkbenchService, method: str, params: dict | None = None) -> dict:
    return service.router.dispatch(method, params or {}, "acc")


def error_of(res: dict) -> dict:
    return res.get("error") or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(PROJ / "build" / "accept" / "p2.yaml"))
    args = parser.parse_args(argv)

    service = WorkbenchService(config_path=args.config)

    # --- 替身：B 站客户端与笔记接口（绝不真发）
    calls: list[dict] = []
    withdraws: list[dict] = []
    state = {"fail": None, "note_seq": 0}

    class _Client:
        def __init__(self) -> None:
            self.cookies = {}
            self.me = {"mid": 0, "uname": "accept"}

    service._gateway.client = lambda: _Client()  # type: ignore[assignment]
    service._gateway.is_logged_in = lambda: True  # type: ignore[assignment]

    def fake_post(client, draft, *, attempts=3, sleep=None, on_retry=None):  # noqa: ANN001
        calls.append(dict(draft))
        if state["fail"]:
            raise WBError("BILI_ERROR", str(state["fail"]), retryable=False)
        state["note_seq"] += 1
        return {"note_id": f"note_acc_{state['note_seq']}", "attempts": 1, "raw": {}}

    def fake_withdraw(client, *, aid, note_id):  # noqa: ANN001
        withdraws.append({"aid": aid, "note_id": note_id})
        return {"code": 0}

    publisher_core.post_note = fake_post  # type: ignore[assignment]
    publisher_core.withdraw_note = fake_withdraw  # type: ignore[assignment]

    # --- 挑一个带 bvid + aid 的项目
    listing = call(service, "library.list", {"limit": 200})["data"]["items"]
    project = None
    for item in listing:
        if item.get("bvid") and item.get("aid"):
            project = item
            break
    if not project:
        print("没有带 aid/bvid 的项目，先跑一次真站下载再验收")
        return 2

    # 自清扫：本脚本会真的把草稿置成 published，第二次跑起来频率关就会说
    # "这个视频已经发过笔记了"。开跑前清场，保证可重复跑。
    #
    # 注意 `publish.list` 自 P4-06 起只回草稿，用它找"已发布"永远找不到——
    # 必须走 repo（第一版就是这么写的，于是清理静默失效）。
    for row in call(service, "publish.list", {"project_id": project["id"]}).get("notes") or []:
        call(service, "publish.delete", {"id": row["id"]})
    # 自清扫：本组脚本会真的产出 published 行（或上一轮留下的），而 `publish.list`
    # 现在只回 draft —— 用 repo 直接查「已发布且未撤回」，先撤回再断言，
    # 否则频率关会说「这个视频已经发过笔记了」，与脚本要验的东西无关。
    for stale in service.repo.publish_published(project["bvid"]):
        service.repo.publish_set_status(stale["id"], "withdrawn")

    pid = project["id"]
    print(f"[ctx] 项目 {pid} · {project.get('title')} · bvid={project.get('bvid')}")

    for row in call(service, "publish.list", {})["data"]["notes"]:
        if row.get("project_id") == pid:
            call(service, "publish.delete", {"id": row["id"]})

    # --- 1. 生成草稿
    preview = call(service, "publish.preview", {"project_id": pid})
    if not check("publish.preview 成功", preview.get("ok") is True, str(error_of(preview))):
        service.close()
        return 1
    data = preview["data"]
    draft_title, draft_summary = data["title"], data["summary"]
    check("预览带门禁", "gates" in data)
    check(
        "三关顺序固定 content/account/pace",
        [g["id"] for g in data["gates"]["gates"]] == ["content", "account", "pace"],
    )
    content_gate = [g for g in data["gates"]["gates"] if g["id"] == "content"][0]
    check("未勾确认时内容关是 todo", content_gate["status"] == "todo", content_gate["reason"])

    saved = call(
        service,
        "publish.note",
        {
            "id": data.get("id") or "",
            "project_id": pid,
            "title": draft_title,
            "summary": draft_summary,
        },
    )
    if not saved.get("ok"):
        saved = call(
            service,
            "publish.note",
            {"project_id": pid, "title": draft_title, "summary": draft_summary},
        )
    if not check("存草稿成功", saved.get("ok") is True, str(error_of(saved))):
        service.close()
        return 1
    note_id = saved["data"]["note"]["id"]
    check(
        "草稿状态是 draft",
        saved["data"]["note"]["status"] == "draft",
        str(saved["data"]["note"].get("status")),
    )
    check(
        "列表能查到这一条",
        any(r["id"] == note_id for r in call(service, "publish.list", {})["data"]["notes"]),
    )

    # --- 2. 没勾确认 → 发不出去
    blocked = call(service, "publish.publish", {"id": note_id, "confirmed": False})
    check("未勾确认被拒", blocked.get("ok") is False)
    check(
        "拒绝文案说清原因",
        "我已阅读并确认" in error_of(blocked).get("message", ""),
        error_of(blocked).get("message", ""),
    )
    check("拒绝时一次请求都没发", len(calls) == 0, f"{len(calls)} 次")

    # --- 3. 勾了确认 → 三关全绿
    ready = call(service, "publish.get", {"id": note_id, "confirmed": True})["data"]
    statuses = [g["status"] for g in ready["gates"]["gates"]]
    check("勾确认后三关全绿", statuses == ["ok", "ok", "ok"], str(statuses))
    check("门禁 ready=true", ready["gates"]["ready"] is True)

    # --- 4. 真发（替身）
    done = call(service, "publish.publish", {"id": note_id, "confirmed": True})
    if not check("发布成功", done.get("ok") is True, str(error_of(done))):
        service.close()
        return 1
    check("返回 note_id", bool(done["data"].get("note_id")), done["data"].get("note_id", ""))
    check(
        "note_id 入库",
        call(service, "publish.get", {"id": note_id})["data"]["note"]["note_id"]
        == done["data"]["note_id"],
    )
    check(
        "状态变 published",
        call(service, "publish.get", {"id": note_id})["data"]["note"]["status"] == "published",
    )
    check("只发了一次请求", len(calls) == 1, f"{len(calls)} 次")
    sent = calls[0] if calls else {}
    check("发出去的标题与草稿一致", sent.get("title") == draft_title)
    check("发出去带 aid", bool(sent.get("aid")), str(sent.get("aid")))
    check("发出去带正文", bool(str(sent.get("content") or "").strip()))

    # --- 5. 重复发布被拦
    again = call(service, "publish.publish", {"id": note_id, "confirmed": True})
    check("同一条不能重复发", again.get("ok") is False)
    check(
        "重复拒绝带 note_id",
        "note_id" in error_of(again).get("message", ""),
        error_of(again).get("message", ""),
    )

    # --- 6. 撤回
    out = call(service, "publish.withdraw", {"id": note_id})
    if not check("撤回成功", out.get("ok") is True, str(error_of(out))):
        service.close()
        return 1
    check(
        "撤回请求带 aid 与 note_id",
        bool(withdraws) and withdraws[-1].get("note_id") == done["data"]["note_id"],
        str(withdraws[-1] if withdraws else {}),
    )
    check(
        "状态变 withdrawn",
        call(service, "publish.get", {"id": note_id})["data"]["note"]["status"] == "withdrawn",
    )

    # --- 7. 撤回后新草稿的第三关应该放行
    fresh = call(
        service, "publish.note", {"project_id": pid, "title": draft_title, "summary": draft_summary}
    )
    fresh_id = fresh["data"]["note"]["id"] if fresh.get("ok") else ""
    if fresh_id:
        gate = call(service, "publish.get", {"id": fresh_id, "confirmed": True})["data"]["gates"]
        pace = [g for g in gate["gates"] if g["id"] == "pace"][0]
        check("撤回后新草稿第三关放行", pace["status"] == "ok", pace["reason"])

    # --- 8. 失败路径：B 站报错要保留草稿
    state["fail"] = "[12015] 验证码校验失败 —— 需要人机验证，换一下网络重试"
    bad = call(service, "publish.publish", {"id": fresh_id, "confirmed": True})
    check("B 站报错时发布失败", bad.get("ok") is False)
    check("错误码是 BILI_ERROR", error_of(bad).get("code") == "BILI_ERROR", str(error_of(bad)))
    row = call(service, "publish.get", {"id": fresh_id})["data"]["note"]
    check("失败后草稿还在（不删记录）", bool(row))
    check("失败后正文没丢", bool(str(row.get("content") or "").strip()))
    check("失败后状态是 failed", row.get("status") == "failed", str(row.get("status")))
    check("失败原因写进记录", "验证码" in str(row.get("error") or ""), str(row.get("error"))[:40])

    # --- 9. 恢复后再发能成
    state["fail"] = None
    state["note_seq"] = 10
    assert fresh_id
    retry = call(service, "publish.publish", {"id": fresh_id, "confirmed": True})
    check("失败后重发能成功", retry.get("ok") is True, str(error_of(retry)))
    check(
        "重发后状态回 published",
        call(service, "publish.get", {"id": fresh_id})["data"]["note"]["status"] == "published",
    )

    # --- 10. 超长标题不回传到 B 站
    before = len(calls)
    over = call(
        service,
        "publish.note",
        {"id": fresh_id, "project_id": pid, "title": "长" * 90, "summary": draft_summary},
    )
    if over.get("ok"):
        # 保存没拦（编辑器允许先存着），那就必须由门禁拦在网络之前
        long_res = call(service, "publish.publish", {"id": fresh_id, "confirmed": True})
        check("超长标题发布被拦", long_res.get("ok") is False, str(error_of(long_res))[:60])
        check("超长标题一次请求都没发", len(calls) == before, f"{len(calls) - before} 次")
    else:
        check("超长标题保存就被拦", error_of(over).get("code") == "VALIDATION", str(error_of(over)))

    # --- 11. 不存在的草稿
    missing = call(service, "publish.publish", {"id": "pn_nope", "confirmed": True})
    check("未知草稿 NOT_FOUND", error_of(missing).get("code") == "NOT_FOUND")

    service.close()

    print()
    print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
    if BAD:
        print("失败项：" + "；".join(BAD))
        return 1
    print("P4-05 发布门禁与笔记发布验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
