"""P3-06 竖屏导出验收：三种竖屏模式真跑 ffmpeg，量分辨率、量黑边、看跟随回退。

只走 RPC 层（和界面用的是同一套接口），不看内部函数：
    1. 9:16 居中 → 1080×1920
    2. 9:16 模糊背景 → 1080×1920，且左右没有黑柱（削掉素材自带黑边）
    3. 9:16 人脸跟随但还没有轨迹 → 能出片，退回居中，结果里写清用了 0 个人脸点
    4. 存两个手工关键帧 → clip.focus 读回来带 manual 标记 → 再导出，用了 2 个点
    5. 非法竖屏模式 → VALIDATION

用法：
    ./.venv/Scripts/python.exe scripts/accept_p3_vertical.py --config build/accept/p2.yaml
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
for extra in (PROJ, PROJ / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from accept_p3_clip import call, error_of  # noqa: E402  （复用信封解析）

from video_workbench.app.service import WorkbenchService  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


def wait_job(service: WorkbenchService, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        row = call(service, "job.get", {"id": job_id}) or {}
        if row.get("status") in ("succeeded", "failed", "canceled"):
            return row
        time.sleep(0.4)
    return row


def probe_ffmpeg(ffmpeg: str, path: str) -> dict:
    """用 ffprobe 量宽高与时长；ffprobe 不在 PATH 时退回 ffmpeg -i 的 stderr。"""
    ffprobe = (
        str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg.lower().endswith(".exe") else "ffprobe"
    )
    if not Path(ffprobe).is_file():
        ffprobe = "ffprobe"
    out = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    stream = (data.get("streams") or [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration_ms": int(float((data.get("format") or {}).get("duration") or 0) * 1000),
    }


def column_span(ffmpeg: str, video: str, out_png: Path, at_ms: int = 1500) -> tuple[int, int]:
    """抽一帧，量"非黑列"的首尾（判断有没有黑柱）。

    黑柱就是成片左右两边纯黑：模糊背景铺不满时最容易出现，肉眼一看就知道，
    所以这里也用像素量出来，别只靠眼看。
    """
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-ss",
            f"{at_ms / 1000:.3f}",
            "-i",
            video,
            "-frames:v",
            "1",
            str(out_png),
        ],
        capture_output=True,
        text=True,
    )
    if not out_png.is_file():
        return (0, 0)
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - 环境缺 Pillow 时上层判失败
        return (0, 0)
    image = Image.open(out_png).convert("L")
    width, height = image.size
    pixels = image.load()
    first, last = -1, -1
    step = max(1, height // 40)
    for x in range(width):
        bright = 0
        for y in range(0, height, step):
            if pixels[x, y] > 12:
                bright += 1
        if bright > max(1, (height // step) // 8):
            if first < 0:
                first = x
            last = x
    return (first, last)


def pick_project(service: WorkbenchService) -> dict:
    listing = call(service, "library.list", {"limit": 50}) or {}
    for row in listing.get("items") or []:
        kinds = row.get("kinds") or []
        if "source" in kinds and float(row.get("duration_sec") or 0) >= 20:
            return row
    raise SystemExit("没有可用的样本项目（需要 source 资产且时长 ≥ 20 秒）")


def ensure_candidate(service: WorkbenchService, project_id: str) -> dict:
    rows = (call(service, "clip.candidates", {"id": project_id}) or {}).get("candidates") or []
    for row in rows:
        if row.get("title") == "验收切片":
            return row
    made = call(
        service,
        "clip.create",
        {
            "project_id": project_id,
            "kind": "highlight",
            "start_ms": 6000,
            "end_ms": 15040,
            "title": "验收切片",
        },
    )
    return (made or {}).get("candidate") or {}


def export(service: WorkbenchService, project_id: str, candidate_id: str, **extra) -> dict:
    params = {
        "project_id": project_id,
        "candidate_id": candidate_id,
        "mode": "precise",
        "subtitle_mode": "none",
        "aspect": "9x16",
    }
    params.update(extra)
    data = call(service, "clip.export", params)
    job = (data or {}).get("job") or {}
    if not job.get("id"):
        return {"error": data}
    return wait_job(service, job["id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="build/accept/p2.yaml")
    args = parser.parse_args()

    service = WorkbenchService(config_path=args.config)
    ffmpeg = service._ffmpeg.ffmpeg
    check(bool(ffmpeg), "ffmpeg 可用", str(ffmpeg or "没找到 ffmpeg"))
    if not ffmpeg:
        return report()

    project = pick_project(service)
    project_id = str(project.get("id") or project.get("project_id") or "")
    check(bool(project_id), "找到样本项目", project_id)
    candidate = ensure_candidate(service, project_id)
    candidate_id = str(candidate.get("id") or "")
    check(bool(candidate_id), "有可导出的候选", str(candidate.get("title") or ""))

    tmp = PROJ / "build" / "accept" / "vertical"
    tmp.mkdir(parents=True, exist_ok=True)

    # ---- 1. 居中
    job = export(service, project_id, candidate_id, vertical_mode="center", crop_x=0.5)
    result = job.get("result") or {}
    check(job.get("status") == "succeeded", "居中导出成功", str(job.get("error_message") or ""))
    center_path = str(result.get("path") or "")
    info = probe_ffmpeg(ffmpeg, center_path) if center_path else {}
    check(
        info.get("width") == 1080 and info.get("height") == 1920,
        "居中成片是 1080×1920",
        f"{info.get('width')}×{info.get('height')}",
    )
    check(
        result.get("vertical_mode") == "center",
        "结果记下竖屏模式",
        str(result.get("vertical_mode")),
    )
    check(
        str(result.get("name") or "").find("_9x16") >= 0,
        "文件名带竖屏标记",
        str(result.get("name")),
    )
    check(
        bool(result.get("src_crop")), "记录了素材黑边裁切", json.dumps(result.get("src_crop") or {})
    )

    # ---- 2. 模糊背景（削边后不该有黑柱）
    job = export(service, project_id, candidate_id, vertical_mode="blur")
    result = job.get("result") or {}
    check(job.get("status") == "succeeded", "模糊背景导出成功", str(job.get("error_message") or ""))
    blur_path = str(result.get("path") or "")
    info = probe_ffmpeg(ffmpeg, blur_path) if blur_path else {}
    check(
        info.get("width") == 1080 and info.get("height") == 1920,
        "模糊背景成片是 1080×1920",
        f"{info.get('width')}×{info.get('height')}",
    )
    if blur_path:
        first, last = column_span(ffmpeg, blur_path, tmp / "blur-frame.png")
        check(
            first <= 2 and last >= 1077,
            "模糊背景左右没黑柱",
            f"内容列 {first}–{last}（应覆盖 0–1079）",
        )

    # ---- 3. 跟随但没有轨迹 → 退回居中
    call(service, "clip.focus.save", {"project_id": project_id, "clear": True})
    job = export(service, project_id, candidate_id, vertical_mode="follow")
    result = job.get("result") or {}
    check(
        job.get("status") == "succeeded",
        "无轨迹的跟随也能出片",
        str(job.get("error_message") or ""),
    )
    check(
        result.get("vertical_mode") == "center",
        "无轨迹时退回居中",
        str(result.get("vertical_mode")),
    )
    check(
        int(result.get("focus_points") or 0) == 0,
        "无轨迹时人脸点数为 0",
        str(result.get("focus_points")),
    )

    # ---- 4. 手工关键帧可见可改，再导出就用上了
    saved = call(
        service,
        "clip.focus.save",
        {
            "project_id": project_id,
            "focus": [
                {"t_ms": 6000, "x": 0.3, "manual": True},
                {"t_ms": 12000, "x": 0.7, "manual": True},
            ],
        },
    )
    focus = (saved or {}).get("focus") or []
    check(len(focus) == 2, "两个手工关键帧存进去了", str(len(focus)))
    check(
        all(point.get("manual") for point in focus),
        "手工点带 manual 标记",
        json.dumps(focus, ensure_ascii=False),
    )

    read_back = call(service, "clip.focus", {"project_id": project_id}) or {}
    rows = read_back.get("focus") or []
    check(len(rows) == 2, "clip.focus 能读回轨迹", str(len(rows)))
    check(
        [round(float(point.get("x") or 0), 2) for point in rows] == [0.3, 0.7],
        "轨迹按时间排好且值不变",
        json.dumps([point.get("x") for point in rows]),
    )
    check(
        (read_back.get("safe_area") or {}).get("top") == 120,
        "返回安全区坐标",
        json.dumps(read_back.get("safe_area") or {}),
    )

    job = export(service, project_id, candidate_id, vertical_mode="follow")
    result = job.get("result") or {}
    check(
        job.get("status") == "succeeded",
        "有人工轨迹时跟随能出片",
        str(job.get("error_message") or ""),
    )
    check(
        result.get("vertical_mode") == "follow",
        "有轨迹时保持跟随模式",
        str(result.get("vertical_mode")),
    )
    check(
        int(result.get("focus_points") or 0) == 2,
        "导出用了 2 个人脸点",
        str(result.get("focus_points")),
    )
    follow_path = str(result.get("path") or "")
    info = probe_ffmpeg(ffmpeg, follow_path) if follow_path else {}
    check(
        info.get("width") == 1080 and info.get("height") == 1920,
        "跟随成片是 1080×1920",
        f"{info.get('width')}×{info.get('height')}",
    )

    # ---- 5. 非法竖屏模式
    # 注意：error_of 自己会 dispatch，别把 call 的结果塞给它（call 失败就直接退出进程了）
    bad = error_of(
        service,
        "clip.export",
        {
            "project_id": project_id,
            "candidate_id": candidate_id,
            "mode": "precise",
            "subtitle_mode": "none",
            "aspect": "9x16",
            "vertical_mode": "swirl",
        },
    )
    check(
        (bad or {}).get("code") == "VALIDATION",
        "非法竖屏模式被拦",
        json.dumps(bad or {}, ensure_ascii=False),
    )

    # ---- 6. 队列能分清竖屏
    exports = (call(service, "clip.exports", {"project_id": project_id}) or {}).get("exports") or []
    vertical_rows = [row for row in exports if row.get("aspect") == "9x16"]
    check(len(vertical_rows) >= 3, "队列里有多条竖屏导出", str(len(vertical_rows)))
    check(
        all(row.get("vertical_mode") in ("center", "blur", "follow") for row in vertical_rows),
        "队列行带竖屏模式",
        json.dumps([row.get("vertical_mode") for row in vertical_rows], ensure_ascii=False),
    )

    # ---- 7. 清掉验收轨迹（别把手工点留在样本项目里）
    call(service, "clip.focus.save", {"project_id": project_id, "clear": True})
    service.close()
    return report()


def report() -> int:
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"[FAIL] {name} — {detail}")
        else:
            print(f"[ok]   {name}" + (f" — {detail}" if detail else ""))
    print()
    if passed == len(RESULTS):
        print(f"P3-06 竖屏导出验收：全部通过（{passed} 项）")
        return 0
    print(f"P3-06 竖屏导出验收：{len(RESULTS) - passed} 项未通过")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
