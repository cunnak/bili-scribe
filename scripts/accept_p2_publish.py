"""P2-09 发布草稿的验收（本地跑，不需要模型、不需要网络）。

为什么不用假模型：草稿的内容全部来自**已有的总结行**，这一层一个请求都不发。
需要证明的只有四件事：

1. 正文拼对了（分节、披露、内部嘱咐不进正文）；
2. 字数门禁真的拦得住，而且**不偷偷截断**；
3. 草稿落进 ``wb_publish_note(status=draft)``，重启还在；
4. 发布/撤回如实说"Phase 5 才接"，不假装能用。

用法::

    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p2_publish.py \\
        --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_workbench.app.service import WorkbenchService  # noqa: E402

CONTENT = {
    "one_line": "这期把注意力机制拆成能照做的步骤",
    "summary_md": "## 这期在讲什么\n先给结论，再补细节。",
    "key_points": ["先证伪直觉", "把变量拆开", "给出操作顺序"],
    "timeline": [{"t": "00:12", "text": "抛出反例"}, {"t": "01:05", "text": "拆变量做对比"}],
    "caution": "数字与专有名词建议回视频核对",
    "comment_short": "一句话：把注意力拆成能照做的三步。",
    "chunks": 1,
    "model": "fake-model",
    "role": "standard",
}

FAILS: list[str] = []
count = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global count
    count += 1
    if ok:
        print(f"[ok  ] {name}")
    else:
        print(f"[FAIL] {name} — {detail}")
        FAILS.append(name)


def call(service, method: str, params: dict | None = None) -> dict:
    return service.router.dispatch(method, params or {}, "accept")


def data_of(service, method: str, params: dict | None = None) -> dict:
    result = call(service, method, params)
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{result.get('error')}")
    return result["data"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    service = WorkbenchService(config_path=args.config, picker=None)
    try:
        root = Path(service.cfg.storage.media_root) / "验收" / "发布草稿"
        root.mkdir(parents=True, exist_ok=True)
        project = service.repo.project_create(
            title="【硬核】三十分钟讲透注意力机制",
            up_name="验收UP",
            root_dir=str(root),
            bvid="BV1accept0001",
            aid=778899,
            stage="summarized",
        )
        empty_project = service.repo.project_create(
            title="还没总结的项目", up_name="验收UP", root_dir=str(root / "empty"), stage="transcribed"
        )
        # 反复跑：先清掉上一次的草稿，否则"只有一份草稿"这类断言会假红
        for row in service.repo.publish_list(project["id"]):
            service.repo.publish_delete(row["id"])

        # ---- 1) 没有总结就没有草稿 ----
        bad = call(service, "publish.preview", {"project_id": empty_project["id"]})
        check(
            "没总结的项目被拦下",
            bad.get("ok") is False and bad["error"]["code"] == "CONFLICT",
            json.dumps(bad.get("error"), ensure_ascii=False),
        )

        record = service.repo.summary_insert(
            project["id"], content=CONTENT, content_md="# 总结", role="standard", model="fake-model"
        )

        # ---- 2) 预览 ----
        draft = data_of(service, "publish.preview", {"project_id": project["id"]})
        check("预览取到最新一版总结", draft["summary_id"] == record["id"] and draft["version"] == 1,
              f"{draft.get('summary_id')} v{draft.get('version')}")
        check("标题默认取视频标题", draft["title"] == project["title"], draft["title"])
        check("摘要默认取短评", draft["summary"] == CONTENT["comment_short"], draft["summary"])
        check("字数门禁与平台一致", draft["limits"] == {"title": 80, "summary": 200},
              json.dumps(draft["limits"]))
        body = draft["markdown"]
        check("正文有要点分节", "## 要点" in body, body[:80])
        check("正文有时间轴分节", "## 时间轴" in body and "`00:12`" in body)
        check("正文带注意提示", "注意：数字与专有名词建议回视频核对" in body)
        check("正文带 AI 披露", "本总结由 AI 依据视频字幕生成" in body)
        check("内部嘱咐没混进正文", "素材局限" not in body and "采集提示" not in body)
        check("Delta 里没有 Markdown 标记",
              "**" not in draft["content_text"] and "## " not in draft["content_text"],
              draft["content_text"][:80])
        manual = len("".join(draft["content_text"].split()))
        check("字数与手算一致", draft["chars"] == manual, f"{draft['chars']} vs {manual}")
        check("预览不带 warning", draft["warnings"] == [], json.dumps(draft["warnings"], ensure_ascii=False))

        # ---- 3) 保存草稿 ----
        saved = data_of(service, "publish.note", {"project_id": project["id"]})
        note = saved["note"]
        check("草稿状态是 draft", note["status"] == "draft", note["status"])
        check("草稿带上了 bvid/aid", note["bvid"] == "BV1accept0001" and note["aid"] == 778899,
              f"{note.get('bvid')}/{note.get('aid')}")
        check("草稿未标记发布/撤回时间", note["published_at"] is None and note["withdrawn_at"] is None)
        row = service.repo.publish_for_project(project["id"])
        check("wb_publish_note 里真有这一行", bool(row) and row["status"] == "draft")

        # ---- 4) 列表 ----
        listed = data_of(service, "publish.list", {})["notes"]
        mine = [item for item in listed if item["project_id"] == project["id"]]
        check("列表里能看到这份草稿", len(mine) == 1, str(len(mine)))
        check("列表带项目标题与 UP", mine[0]["project_title"] == project["title"] and mine[0]["up_name"] == "验收UP")
        check("列表带字数", mine[0]["chars"] == note["chars"], f"{mine[0]['chars']} vs {note['chars']}")

        # ---- 5) 编辑：改标题与摘要，同一条更新 ----
        edited = data_of(service, "publish.note", {
            "project_id": project["id"],
            "title": "我自己改的标题",
            "summary": "我自己改的摘要",
        })["note"]
        check("改标题落在同一行", edited["id"] == note["id"], f"{edited['id']} vs {note['id']}")
        check("改标题生效", edited["title"] == "我自己改的标题", edited["title"])
        check("改摘要生效", edited["summary"] == "我自己改的摘要", edited["summary"])
        again = data_of(service, "publish.list", {"project_id": project["id"]})["notes"]
        check("重复保存不会堆积草稿", len(again) == 1, str(len(again)))

        # ---- 6) 手改 Delta 以用户为准 ----
        custom = json.dumps([{"insert": "我自己写的正文\n"}], ensure_ascii=False)
        manual_row = data_of(service, "publish.note", {
            "project_id": project["id"], "content": custom
        })["note"]
        check("手改正文以用户为准", manual_row["content"] == custom, manual_row["content"][:40])
        check("手改正文的字数重算", manual_row["content_text"].startswith("我自己写的正文"))

        # ---- 7) 门禁：不截断，直接拒 ----
        long_title = call(service, "publish.note", {"project_id": project["id"], "title": "标" * 100})
        check("超长标题被拒", long_title.get("ok") is False and long_title["error"]["code"] == "VALIDATION")
        check("超长标题的报错说得清", "标题超过 80 字" in long_title["error"]["message"],
              long_title["error"]["message"])
        long_summary = call(service, "publish.note", {
            "project_id": project["id"], "title": "正常标题", "summary": "长" * 260
        })
        check("超长摘要被拒", long_summary.get("ok") is False and "摘要超过 200 字" in long_summary["error"]["message"])
        empty_body = call(service, "publish.note", {"project_id": project["id"], "content": "[]"})
        check("空正文被拒", empty_body.get("ok") is False and empty_body["error"]["code"] == "VALIDATION")
        still = service.repo.publish_for_project(project["id"])
        check("被拒的三次都没改库", still["id"] == note["id"], still["id"])

        # ---- 8) 关掉 AI 披露 ----
        service.cfg.publish.disclose_ai = False
        no_disclose = data_of(service, "publish.preview", {"project_id": project["id"]})
        check("设置里关掉披露后正文不再带它",
              "本总结由 AI 依据视频字幕生成" not in no_disclose["markdown"])
        service.cfg.publish.disclose_ai = True

        # ---- 9) 持久化：换一个服务实例还能读到 ----
        note_id = note["id"]
        service.close()
        service = WorkbenchService(config_path=args.config, picker=None)
        survived = data_of(service, "publish.get", {"id": note_id})["note"]
        check("重启后草稿还在", survived["id"] == note_id and survived["status"] == "draft")

        # ---- 10) 撤回如实说 Phase 5 ----
        withdraw = call(service, "publish.withdraw", {"id": note_id})
        check("撤回明确说 Phase 5 才接",
              withdraw.get("ok") is False and withdraw["error"]["code"] == "UNSUPPORTED"
              and "Phase 5" in withdraw["error"]["message"],
              json.dumps(withdraw.get("error"), ensure_ascii=False))

        # ---- 11) 删除 ----
        data_of(service, "publish.delete", {"id": note_id})
        gone = call(service, "publish.get", {"id": note_id})
        check("删除后读不到", gone.get("ok") is False and gone["error"]["code"] == "NOT_FOUND")
        twice = call(service, "publish.delete", {"id": note_id})
        check("重复删除报 NOT_FOUND", twice.get("ok") is False and twice["error"]["code"] == "NOT_FOUND")
        check("删除后列表空了", data_of(service, "publish.list", {"project_id": project["id"]})["notes"] == [])
    finally:
        try:
            service.close()
        except Exception:
            pass

    total = count
    if FAILS:
        print(f"\n{len(FAILS)} / {total} 项失败：")
        for name in FAILS:
            print("  - " + name)
        return 1
    print(f"\nP2-09 发布草稿验收：全部通过（{total} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
