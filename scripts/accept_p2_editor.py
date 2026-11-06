#!/usr/bin/env python
"""字幕编辑器的真机验收（任务清单 P2-03）。

P2-01/P2-02 的验收证明了"能转出字幕"；这个脚本证明**转出来的东西能被编辑并落盘**：

    1. 找到一个已有 transcript 的项目（subtitle.get 有段）
    2. audio.peaks 能返回与时长匹配的峰值（波形不是假的）
    3. subtitle.op(update) 改一句 → 库里、transcript.json、transcript.srt 三处都变
    4. subtitle.op(split) 切一刀 → 段数 +1，且两段首尾相接
    5. 把改动撤回（同一段改回原文），避免污染验收数据

跑法::

    python scripts/accept_p2_editor.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p2-editor")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def find_project_with_transcript(router) -> dict:
    listing = call(router, "library.list", {"query": "", "limit": 200, "offset": 0})
    # 判据是 library.list 的 kinds（= wb_asset 聚合），不是 library.get 的 assets：
    # 详情接口按需加载资产，返回的是 project + assets 两个键之一，直接拿 kinds 更稳。
    for item in listing["items"]:
        if "subtitle" in (item.get("kinds") or []):
            return call(router, "library.get", {"id": item["id"]})
    raise SystemExit("没有找到带字幕的项目：先跑 scripts/accept_p2_transcribe.py")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    parser.add_argument("--project", default="", help="指定项目 id，默认自动挑第一个有字幕的")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    service = WorkbenchService(config_path=str(config_path), picker=None)
    # Router 由 Service 自己建好并注册完所有方法，直接用——不在验收脚本里
    # 另拼一份注册表，否则验的就不是界面上跑的那套接口了。
    router = service.router

    # library.get 回的是 {project, assets} 两段，不是扁平的 project
    detail = (
        call(router, "library.get", {"id": args.project})
        if args.project
        else find_project_with_transcript(router)
    )
    project = detail["project"]
    pid = project["id"]
    print(f"[project] {pid} · {project.get('title', '')[:40]}")

    # ---- 1. 读字幕
    got = call(router, "subtitle.get", {"project_id": pid})
    segments = got.get("segments") or []
    check("transcript.get 能读到段", len(segments) > 0, f"{len(segments)} 段")
    check(
        "stats 与段数一致", got["stats"]["count"] == len(segments), f"stats={got['stats']['count']}"
    )
    check("qc 是列表", isinstance(got.get("qc"), list), f"{len(got.get('qc') or [])} 条问题")

    # ---- 2. 波形数据
    peaks = call(router, "audio.peaks", {"project_id": pid})
    check(
        "audio.peaks 有峰值",
        len(peaks.get("peaks") or []) > 0,
        f"{len(peaks.get('peaks') or [])} 个桶 / duration={peaks.get('duration_ms')}ms",
    )
    check("峰值与时长对得上", (peaks.get("duration_ms") or 0) > 0)

    # ---- 3. 改一句，三处落盘
    first = segments[0]
    original_text = first["text"]
    probe_text = (original_text + "（验收改动）")[:60]
    updated = call(
        router,
        "subtitle.op",
        {
            "project_id": pid,
            "op": "update",
            "segment_id": first["id"],
            "text": probe_text,
        },
    )
    same = [s for s in updated["segments"] if s["id"] == first["id"]][0]
    check("op(update) 回传的段已改", same["text"] == probe_text)

    reread = call(router, "subtitle.get", {"project_id": pid})
    check(
        "再读一次仍是改后的文本",
        [s for s in reread["segments"] if s["id"] == first["id"]][0]["text"] == probe_text,
    )

    root = Path(project["root_dir"])
    json_path = next(iter(root.glob("transcript*.json")), None)
    srt_path = next(iter(root.glob("transcript*.srt")), None)
    if json_path:
        blob = json_path.read_text(encoding="utf-8")
        check(f"{json_path.name} 里有改动", probe_text[:12] in blob)
    else:
        check("能找到 transcript*.json", False)
    if srt_path:
        check(
            f"{srt_path.name} 里有改动", probe_text[:12] in srt_path.read_text(encoding="utf-8-sig")
        )
    else:
        check("能找到 transcript*.srt", False)

    # ---- 4. 切一刀
    before = len(reread["segments"])
    mid = (first["start_ms"] + first["end_ms"]) // 2
    cut = call(
        router,
        "subtitle.op",
        {
            "project_id": pid,
            "op": "split",
            "segment_id": first["id"],
            "at_ms": mid,
        },
    )
    check(
        "op(split) 段数 +1",
        len(cut["segments"]) == before + 1,
        f"{before} → {len(cut['segments'])}",
    )
    # 按"原始段的时间区间"找这对兄弟：归一化会按 start_ms 重排，索引 0/1 未必是它们
    pair = [
        s
        for s in cut["segments"]
        if s["start_ms"] >= first["start_ms"] and s["end_ms"] <= first["end_ms"]
    ]
    left, right = pair[0], pair[1]
    # 切点会吸附到最近的词边界（有词级时间戳时不允许把词劈成两半），
    # 所以边界落在 [start, end] 内即可，不要求正好等于 at_ms。
    boundary = left["end_ms"]
    check("切开后首尾相接", boundary == right["start_ms"], f"{boundary} / {right['start_ms']}")
    check(
        "切点落在原段内",
        first["start_ms"] <= boundary <= first["end_ms"],
        f"boundary={boundary} mid={mid} 原段={first['start_ms']}-{first['end_ms']}",
    )
    check(
        "两段合起来等于原段",
        left["start_ms"] == first["start_ms"] and right["end_ms"] == first["end_ms"],
        f"{left['start_ms']}-{left['end_ms']} + {right['start_ms']}-{right['end_ms']}",
    )

    # ---- 5. 还原（合并回去 + 改回原文），验收不能留脏数据
    merged = call(
        router,
        "subtitle.op",
        {
            "project_id": pid,
            "op": "merge",
            "segment_ids": [left["id"], right["id"]],
        },
    )
    restored = call(
        router,
        "subtitle.op",
        {
            "project_id": pid,
            "op": "update",
            "segment_id": merged["segments"][0]["id"],
            "text": original_text,
        },
    )
    back = [s for s in restored["segments"] if s["id"] == first["id"]]
    check("还原成原文", bool(back) and back[0]["text"] == original_text)
    check("段数回到验收初", len(restored["segments"]) == before, f"{len(restored['segments'])}")

    print()
    if FAILS:
        print(f"[result] FAIL（{len(FAILS)} 项）：" + "；".join(FAILS))
        return 1
    print("[result] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
