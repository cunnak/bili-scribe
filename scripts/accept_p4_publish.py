"""P4-04 验收：发布中心草稿与门禁状态（真库，不打模型）。

验五件事：
1. 草稿能从总结生成，标题 / 摘要 / 正文都在，字数与内容对得上；
2. 三道门禁都亮出来，且**没有勾确认时不能发**（内容关是 todo 而不是 ok）；
3. 手改标题后保存，`updated_at` 会变，列表带 UP 与状态；
4. 重新生成只换指定的那一部分（`summary` 不碰标题，`title` 不碰摘要）；
5. 删草稿后列表里真的没了；勾了确认、已登录时三关全绿。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p4_publish.py \
        --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from video_workbench.app.errors import WBError  # noqa: E402
from video_workbench.app.service import WorkbenchService  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, str(detail)))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def call(service, method: str, params: dict | None = None) -> dict:
    out = service.dispatch(method, params or {})
    if not out.get("ok"):
        raise SystemExit(f"[fatal] {method} 失败：{out.get('error')}")
    return out["data"]


def error_of(service, method: str, params: dict | None = None) -> dict:
    out = service.dispatch(method, params or {})
    if out.get("ok"):
        raise SystemExit(f"[fatal] {method} 本该失败却成功了")
    return out.get("error") or {}


def pick_project(service) -> dict:
    """挑一个**有 BV 号**且已经有总结的项目。

    两个条件都不能省：没总结就没有草稿可谈；没 BV 号则账号关天然 blocked
    （"这个项目没有 BV 号，发不了笔记"），"三关全绿"这条断言会因为挑到导入的
    本地视频而假红——2026-10-30 跑 P5-06 矩阵时就红在这里。
    """
    rows = call(service, "library.list", {"limit": 100}).get("items") or []
    for row in rows:
        if not row.get("bvid"):
            continue
        got = service.dispatch("summary.get", {"id": row["id"]})
        if got.get("ok") and got["data"].get("found"):
            return row
    raise SystemExit("[fatal] 库里没有「有 BV 号 + 有总结」的项目，先跑 P2-07 验收")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="build/accept/p2.yaml")
    args = ap.parse_args()

    service = WorkbenchService(config_path=args.config)
    project = pick_project(service)
    pid = project["id"]
    print(f"[info] 项目 {pid} · {project.get('title', '')[:40]}")

    for row in call(service, "publish.list", {}).get("notes") or []:
        if row.get("project_id") == pid:
            call(service, "publish.delete", {"id": row["id"]})
    bvid = str(project.get("bvid") or "")
    # 自清扫：本组脚本会真的产出 published 行（或上一轮留下的），而 `publish.list`
    # 现在只回 draft —— 用 repo 直接查「已发布且未撤回」，先撤回再断言，
    # 否则频率关会说「这个视频已经发过笔记了」，与脚本要验的东西无关。
    for stale in service.repo.publish_published(bvid):
        service.repo.publish_set_status(stale["id"], "withdrawn")

    # ── 1. 预览：字段齐、字数对
    preview = call(service, "publish.preview", {"project_id": pid})
    draft = preview.get("draft") or preview  # preview 直接返回草稿本体
    check(
        "预览给出标题/摘要/正文",
        bool(draft.get("title"))
        and bool(draft.get("summary"))
        and bool((draft.get("content_text") or "").strip()),
        f"title={len(draft.get('title') or '')}字 body={len((draft.get('content_text') or ''))}字",
    )
    check(
        "摘要不超过 200 字",
        len(draft.get("summary") or "") <= 200,
        f"{len(draft.get('summary') or '')} 字",
    )

    # ── 2. 门禁三关都在，未确认时不能发
    gates = preview.get("gates") or {}
    rows = gates.get("gates") or []
    check(
        "三道门禁都在",
        [g.get("id") for g in rows] == ["content", "account", "pace"],
        json.dumps([g.get("id") for g in rows], ensure_ascii=False),
    )
    content = next((g for g in rows if g.get("id") == "content"), {})
    check(
        "没勾确认时内容关是待确认（不是通过）",
        content.get("status") == "todo",
        f"status={content.get('status')} reason={content.get('reason') or '—'}",
    )
    check("三关没全绿时 ready=False", gates.get("ready") is False, f"ready={gates.get('ready')}")

    # ── 3. 生成草稿 → 改标题 → 保存，updated_at 会动
    note = call(service, "publish.note", {"project_id": pid}).get("note") or {}
    check("生成草稿成功", bool(note.get("id")), f"id={note.get('id')}")
    first_updated = str(note.get("updated_at") or "")
    time.sleep(1.1)
    mine = "验收改过的标题"
    saved = (
        call(
            service,
            "publish.note",
            {
                "project_id": pid,
                "note_pk": note.get("id"),
                "title": mine,
            },
        ).get("note")
        or {}
    )
    check("改标题后保存成功", saved.get("title") == mine, f"title={saved.get('title')}")
    check(
        "保存后 updated_at 变了",
        str(saved.get("updated_at") or "") != first_updated,
        f"{first_updated} → {saved.get('updated_at')}",
    )
    check(
        "标题字数按码点算",
        saved.get("title_chars") == len(mine),
        f"{saved.get('title_chars')} vs {len(mine)}",
    )

    listing = call(service, "publish.list", {"project_id": pid}).get("notes") or []
    hit = next((r for r in listing if r.get("id") == note.get("id")), {})
    check("列表带 UP 名", "up_name" in hit, f"up_name={hit.get('up_name') or '（空）'}")
    check("列表带更新时间", bool(hit.get("updated_at")), f"updated_at={hit.get('updated_at')}")
    check("列表带状态", hit.get("status") == "draft", f"status={hit.get('status')}")

    # ── 4. 重新生成只换指定部分
    before_summary = str(saved.get("summary") or "")
    regen = call(service, "publish.regenerate", {"id": note.get("id"), "part": "summary"})
    fresh = regen.get("draft") or {}
    check("只重算摘要时标题保留手改值", fresh.get("title") == mine, f"title={fresh.get('title')}")
    check(
        "只重算摘要时正文不被清空",
        bool((fresh.get("content_text") or "").strip()),
        f"body={len(fresh.get('content_text') or '')}字",
    )
    check("重算摘要带门禁状态", bool((regen.get("gates") or {}).get("gates")), "gates 有值")
    check(
        "重算摘要不写库（草稿仍是旧摘要）",
        (call(service, "publish.get", {"id": note.get("id")}).get("note") or {}).get("summary")
        == before_summary,
        "get 回来的摘要未变",
    )
    regen_title = call(service, "publish.regenerate", {"id": note.get("id"), "part": "title"})
    check(
        "只重算标题时摘要保留",
        (regen_title.get("draft") or {}).get("summary") == before_summary,
        "摘要未变",
    )

    # ── 5. 勾了确认 + 已登录：三关全绿
    got = call(service, "publish.get", {"id": note.get("id")})
    gate_ok = service._publish_gates(
        got.get("note") or {},
        project=service.repo.project_get(pid),
        note=service.repo.publish_get(note.get("id")),
        confirmed=True,
        logged_in=True,
    )
    check(
        "勾确认且已登录时三关全绿",
        gate_ok.get("ready") is True,
        json.dumps([(g["id"], g["status"]) for g in gate_ok["gates"]], ensure_ascii=False),
    )
    check(
        "三关全绿时 content/account 都是 ok",
        [g["status"] for g in gate_ok["gates"]][:2] == ["ok", "ok"],
        json.dumps([g["status"] for g in gate_ok["gates"]], ensure_ascii=False),
    )

    # ── 6. 删掉
    check(
        "删除草稿成功",
        (call(service, "publish.delete", {"id": note.get("id")}) or {}).get("removed") == 1,
    )
    left = call(service, "publish.list", {"project_id": pid}).get("notes") or []
    check("删完列表里没了", all(r.get("id") != note.get("id") for r in left), f"剩 {len(left)} 条")
    err = error_of(service, "publish.get", {"id": note.get("id")})
    check("删掉的草稿再读报 NOT_FOUND", err.get("code") == "NOT_FOUND", f"code={err.get('code')}")

    service.close()

    bad = [name for ok, name, _ in results if not ok]
    print()
    if bad:
        print(
            f"P4-04 发布草稿验收：{len(results) - len(bad)}/{len(results)} 通过，失败 {len(bad)} 项"
        )
        for name in bad:
            print(f"  - {name}")
        return 1
    print(f"P4-04 发布草稿验收：全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WBError as exc:  # pragma: no cover
        print(f"[fatal] {exc.code}: {exc.message}")
        raise SystemExit(2)
