"""P3-07 验收：字幕样式（真库 + 真 ffmpeg 渲染 + 带样式硬烧）。

假 ffmpeg 只能证明"参数拼对了"，证明不了"样式真的生效"。这里做两件真事：

1. `style.render` 用 FFmpeg 把 ASS 渲成 PNG —— 字号改大，图的字节必须变；
2. `clip.export(hard, style_id)` 先按样式**重生成** ASS 再烧，烧完的文件里
   Style 行必须是我们给的那套（而不是上一次导出的旧样式）。

跑法：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/accept_p3_styles.py \
        --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

from video_workbench.app.service import WorkbenchService  # noqa: E402

results: list[tuple[bool, str, str]] = []


class _SkipExport(RuntimeError):
    """没有带字幕的项目时，提前收工（已记一条"跳过"，不算失败）。"""


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, str(detail)))


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


def wait_job(service, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job and job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    return service.jobs.get(job_id) or {}


def pick_project(service) -> dict:
    """优先挑**已经有字幕**的项目：硬烧这一节要真烧一条字幕上去。

    库里的样片项目有时只有源码没有字幕资产（转录被清过），这时退到"有源视频"
    的项目并把硬烧整节标成跳过——比造一份假字幕覆盖用户的真字幕好。
    """
    items = call(service, "library.list")["items"]
    usable = [
        item
        for item in items
        if "source" in (item.get("kinds") or []) and float(item.get("duration_sec") or 0) > 1
    ]
    for item in usable:
        if "subtitle" in (item.get("kinds") or []):
            return item
    if usable:
        return usable[0]
    raise SystemExit("[fatal] 库里没有带源视频且有片长的项目，先跑 accept_p1_download.py")


def style_line(ass_path: Path) -> str:
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Style:"):
            return line
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args()

    service = WorkbenchService(config_path=str(PROJ / args.config))
    created: list[str] = []
    try:
        ffmpeg, _probe = service._ffmpeg.require()
        check("ffmpeg 可用", bool(ffmpeg), ffmpeg)

        # ---------------------------------------------------- 预设
        rows = call(service, "style.list")
        by_id = {row["id"]: row for row in rows}
        check("内置预设两个", {"default", "rounded-default"} <= set(by_id), ",".join(sorted(by_id)))
        check(
            "内置预设只读",
            bool(by_id["default"]["builtin"]) and bool(by_id["rounded-default"]["builtin"]),
        )
        check("默认预设是描边模式", by_id["default"]["mode"] == "ass", by_id["default"]["mode"])
        check(
            "圆角预设是圆角条模式",
            by_id["rounded-default"]["mode"] == "rounded",
            by_id["rounded-default"]["mode"],
        )
        check(
            "列表带字号与字体（界面要显示）",
            by_id["default"]["size"] >= 12 and bool(by_id["default"]["font"]),
            f"{by_id['default']['size']} / {by_id['default']['font']}",
        )

        got = call(service, "style.get", {"id": "圆角默认"})
        check("按名字能取到内置预设", got["style"]["id"] == "rounded-default", got["style"]["id"])
        check("样式带画布尺寸", got["canvas"] == {"width": 1280, "height": 720}, str(got["canvas"]))
        check(
            "内置样式 file 指向 styles 目录",
            got["file"].endswith("rounded-default.json"),
            got["file"],
        )

        # ---------------------------------------------------- 校验
        bad = error_of(
            service, "style.save", {"name": "坏样式", "config": {"primary": {"size": 400}}}
        )
        check("字号越界报 VALIDATION", bad.get("code") == "VALIDATION", bad.get("message", ""))
        check(
            "校验文案点出字段", "主字幕字号" in str(bad.get("message", "")), bad.get("message", "")
        )
        conflict = error_of(
            service,
            "style.save",
            {"id": "default", "name": "改内置", "config": {"primary": {"size": 60}}},
        )
        check("改内置报 CONFLICT", conflict.get("code") == "CONFLICT", conflict.get("message", ""))

        # ---------------------------------------------------- 保存 / 删除
        saved = call(
            service,
            "style.save",
            {
                "name": "验收样式-大字号",
                "config": {
                    "primary": {
                        "size": 72,
                        "color": service._style_resolve("default")[0]["primary"]["color"],
                    },
                    "secondary": {"size": 48},
                },
            },
        )
        style_id = saved["style"]["id"]
        created.append(style_id)
        check("新建样式拿到 sty id", style_id.startswith("sty"), style_id)
        check("保存写了 styles/*.json", Path(saved["file"]).is_file(), saved["file"])
        check(
            "样式文件与返回一致",
            '"size": 72' in Path(saved["file"]).read_text(encoding="utf-8"),
            Path(saved["file"]).read_text(encoding="utf-8")[:80],
        )
        listed = {row["id"]: row for row in call(service, "style.list")}
        check(
            "新样式出现在列表且非内置",
            style_id in listed and listed[style_id]["builtin"] is False,
            style_id,
        )

        # ---------------------------------------------------- 真渲染
        small = call(service, "style.render", {"id": "default", "text": "渲染测试"})
        big = call(service, "style.render", {"id": style_id, "text": "渲染测试"})
        check(
            "真渲染出 PNG（默认）",
            Path(small["path"]).is_file() and Path(small["path"]).stat().st_size > 1000,
            small["path"],
        )
        check(
            "真渲染分辨率 1280×720",
            (small["width"], small["height"]) == (1280, 720),
            f"{small['width']}x{small['height']}",
        )
        check(
            "改大字号后图的字节变了（样式真的生效）",
            Path(big["path"]).stat().st_size != Path(small["path"]).stat().st_size,
            f"{Path(small['path']).stat().st_size} → {Path(big['path']).stat().st_size}",
        )
        check(
            "渲染的 ASS 里字号是 72",
            ",72," in style_line(Path(big["ass"])),
            style_line(Path(big["ass"]))[:60],
        )
        rounded = call(service, "style.render", {"id": "rounded-default", "text": "圆角测试"})
        parts = style_line(Path(rounded["ass"])).split(",")
        check(
            "圆角样式走 BorderStyle=3",
            parts[15].strip() == "3",
            style_line(Path(rounded["ass"]))[:60],
        )

        # ---------------------------------------------------- 导出 / 导入
        out_path = Path(str(service.cfg.storage.media_root)) / "styles" / "accept-export.json"
        exported = call(service, "style.export", {"id": style_id, "path": str(out_path)})
        check("导出 JSON 落盘", Path(exported["path"]).is_file(), exported["path"])
        imported = call(service, "style.import", {"path": str(out_path)})
        created.append(imported["style"]["id"])
        check(
            "导入生成新 id（不覆盖原样式）",
            imported["style"]["id"] != style_id,
            f"{style_id} → {imported['style']['id']}",
        )
        check(
            "导入保留字号",
            int(imported["style"]["primary"]["size"]) == 72,
            str(imported["style"]["primary"]["size"]),
        )
        outside = error_of(
            service, "style.export", {"id": style_id, "path": "C:/Windows/evil.json"}
        )
        check("导出到系统目录被拒", outside.get("code") == "VALIDATION", outside.get("message", ""))

        # ---------------------------------------------------- 带样式硬烧
        project = pick_project(service)
        project_id = project["id"]
        clips = call(service, "clip.candidates", {"id": project_id})
        if not clips.get("candidates"):
            call(
                service,
                "clip.create",
                {
                    "project_id": project_id,
                    "kind": "highlight",
                    "start_ms": 6000,
                    "end_ms": 16000,
                    "title": "样式验收切片",
                },
            )
            clips = call(service, "clip.candidates", {"id": project_id})
        candidate = clips["candidates"][0]
        subs = [a for a in service.repo.asset_list(project_id) if a.get("kind") == "subtitle"]
        if not subs:
            results.append((True, "带样式硬烧（跳过）", "库里没有带字幕的项目，先转录再跑本节"))
            raise _SkipExport()
        check("项目已有字幕资产（硬烧前提）", True, f"{len(subs)} 个")

        job = call(
            service,
            "clip.export",
            {
                "project_id": project_id,
                "candidate_id": candidate["id"],
                "mode": "precise",
                "subtitle_mode": "hard",
                "style_id": style_id,
            },
        )["job"]
        done = wait_job(service, job["id"])
        result = done.get("result") or {}
        output = Path(str(result.get("path") or ""))
        check(
            "带样式硬烧成功",
            done.get("status") == "succeeded",
            f"{done.get('status')} {done.get('error_message') or ''}",
        )
        check(
            "成片文件存在且非空",
            output.is_file() and output.stat().st_size > 10_000,
            f"{output.name} {output.stat().st_size if output.is_file() else 0}",
        )
        style_ass = (
            Path(str(service.repo.project_get(project_id)["root_dir"]))
            / "export"
            / f"subtitle.{style_id}.ass"
        )
        check("硬烧用的是按样式重生成的 ASS", style_ass.is_file(), str(style_ass))
        check("重生成的 ASS 字号是 72", ",72," in style_line(style_ass), style_line(style_ass)[:60])
        check(
            "成片时长与切点一致（误差 ≤1 帧）",
            abs(
                int(result.get("duration_ms") or 0)
                - (int(candidate["end_ms"]) - int(candidate["start_ms"]))
            )
            <= 40,
            f"{result.get('duration_ms')} vs {int(candidate['end_ms']) - int(candidate['start_ms'])}",
        )

        # 不带样式的软字幕路径不该被样式影响
        plain = call(
            service,
            "clip.export",
            {
                "project_id": project_id,
                "candidate_id": candidate["id"],
                "mode": "precise",
                "subtitle_mode": "soft",
            },
        )["job"]
        plain_done = wait_job(service, plain["id"])
        check(
            "不带样式时软字幕照常成功",
            plain_done.get("status") == "succeeded",
            str(plain_done.get("error_message") or ""),
        )

    except _SkipExport:
        pass
    finally:
        for style_id in created:
            try:
                service.repo.style_delete(style_id)
            except Exception:  # noqa: BLE001 —— 清场失败不该盖住验收结论
                pass
        service.close()

    passed = sum(1 for flag, _n, _d in results if flag)
    for flag, name, detail in results:
        mark = "ok  " if flag else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    print(f"\n{passed}/{len(results)} 项通过")
    if passed != len(results):
        print("P3-07 字幕样式验收：存在失败项")
        return 1
    print("P3-07 字幕样式验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
