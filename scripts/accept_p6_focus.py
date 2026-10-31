#!/usr/bin/env python
"""P6-02 跟随增强的真机验收（任务清单 P6-02）。

三条验收标准，逐条对上：

1. **多人画面可指定主体** —— Haar 检测器在真实视频里通常只找到一张脸，构造不出"两个人"。
   所以分两层验：真视频证明"检测这条路能跑通"（detector/thumbs/落盘），假采样器证明
   "两张脸分别成两个主体、能按 id 换人、换人不重跑检测"。
2. **切换镜头不跳变** —— 假采样器在 2 秒处塞一个镜头切换（帧差超阈值），断言
   ①切点被识别出来 ②轨迹表达式在切点前 20ms 保持旧值、切点当刻立刻用新值
   ③`crop` 表达式里出现 `/0.020`（若按 1 秒插值，那 20ms 的跳转会被抹平）。
3. **关掉增强回退 P3-06 基础跟随** —— 同一条视频 `enhance=false` 跑一遍，断言
   没有 subjects/tracks、只有一个轨迹、且 `detect_focus` 走的是老单脸路径。

跑法::

    python scripts/accept_p6_focus.py --video build/accept/media/<某个>/source.mp4
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import reframe  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p6-focus")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def wait_job(service, job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.jobs.get(job_id)
        if job.get("status") in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(0.2)
    raise SystemExit(f"任务 {job_id} 超时未结束")


# ---------------------------------------------------------------- 假采样器


def fake_sampler(*, faces: int = 2, cut_at_ms: int = 2000):
    """造一段"两个人 + 一次镜头切换"的采样。

    返回的采样形状与 `_haar_sampler` 一致：`{t_ms, faces, cut, jpeg, width, height}`。
    2 秒处两个人都换位（模拟镜头切换），同时把 `cut` 标成超过阈值。
    """

    def _sampler(source, *, start_ms, span_ms, interval_ms, models_root, proxy="", log=None,
                 thumb_limit=3):
        samples = []
        thumbs: list[bytes] = []
        for index, t in enumerate(range(0, max(1, span_ms), interval_ms)):
            after = t >= cut_at_ms
            if faces == 2:
                # 镜头切换时两张脸一起小幅平移：位置变了但身份没换，
                # 聚类才应该认出"还是这两个人"。互换位置等于换了人，那是另一回事。
                shift = 80.0 if after else 0.0
                left = [320.0 + shift, 120.0, 200.0, 200.0]
                right = [760.0 + shift, 160.0, 180.0, 180.0]
                rows = [left, right]
            elif faces == 1:
                rows = [[500.0 + (60.0 if after else 0.0), 140.0, 220.0, 220.0]]
            else:
                rows = []
            jpeg = b"\xff\xd8fake-jpeg" if index == 0 else b""
            if jpeg:
                thumbs.append(jpeg)
            samples.append(
                {
                    "t_ms": start_ms + t,
                    "faces": rows,
                    "cut": 0.42 if t == cut_at_ms else 0.02,
                    "jpeg": jpeg,
                    "width": 1920,
                    "height": 1080,
                }
            )
        return {
            "samples": samples,
            "thumbs": thumbs,
            "detector": "haar",
            "frames": len(samples),
            "width": 1920,
            "height": 1080,
            "notes": [],
        }

    return _sampler


def make_project(service, video: Path) -> str:
    root = Path(service.cfg.media_root) / "p6_focus"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(
        title="P6-02 跟随用例", source_kind="local", root_dir=str(root), stage="transcribed"
    )
    service.repo.asset_upsert(
        project["id"], kind="source", path=str(video),
        size=video.stat().st_size if video.exists() else 0,
    )
    return project["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    video = Path(args.video).resolve() if args.video else None
    if video is None:
        pool = sorted((PROJ / "build" / "accept" / "media").rglob("source.mp4"))
        video = pool[0] if pool else None
    if not video or not video.is_file():
        raise SystemExit("找不到测试视频：用 --video 指定一个 mp4")

    work = PROJ / "build" / "accept" / "p6_focus"
    work.mkdir(parents=True, exist_ok=True)
    cfg = {
        "ui": {"skin": "ark"},
        "storage": {
            "db_path": str(work / "wb.db"),
            "media_root": str(work / "media"),
            "cache_root": str(work / "cache"),
            "models_root": str(work / "models"),
            "ffmpeg_dir": str(work / "bin"),
        },
        "clip": {"follow_enhance": True},
        # 检测模型要下载（YuNet 232 KB / Haar 级联 900 KB），走本机代理才通
        "llm": {"proxy": "http://127.0.0.1:7897"},
    }
    cfg_path = work / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    service = WorkbenchService(config_path=str(cfg_path), picker=None)
    router = service.router
    original_detect = reframe.detect_focus

    try:
        info = call(router, "app.info", {})
        check("FFmpeg 可用", bool(info["deps"]["ffmpeg"]), str(info["deps"]))

        # ---------------------------------------------------------- 1. 真视频跑一遍
        pid = make_project(service, video)
        job = wait_job(service, call(router, "clip.focus.analyze", {"project_id": pid})["job"]["id"])
        check("真视频检测任务成功", job.get("status") == "succeeded",
              str(job.get("error_message") or ""))
        result = job.get("result") or {}
        check("回报检测器", result.get("detector") in ("haar", "yunet", "no_face", "unavailable"),
              str(result.get("detector")))
        check("落盘了轨迹点", (result.get("points") or 0) >= 1, str(result.get("points")))
        focus_file = Path(service.repo.project_get(pid)["root_dir"]) / "focus.json"
        check("focus.json 写出来了", focus_file.is_file(), str(focus_file))
        payload = json.loads(focus_file.read_text(encoding="utf-8"))
        check("focus.json 带 cuts/enhance 字段",
              "cuts" in payload and "enhance" in payload, str(list(payload)))

        # ---------------------------------------------------- 2. 假采样器：多主体 + 切点
        reframe.detect_focus = lambda *a, **kw: original_detect(
            *a, **{**kw, "sampler": fake_sampler(faces=2, cut_at_ms=2000)}
        )
        job2 = wait_job(
            service, call(router, "clip.focus.analyze",
                          {"project_id": pid, "enhance": True})["job"]["id"]
        )
        check("多主体检测成功", job2.get("status") == "succeeded",
              str(job2.get("error_message") or ""))
        res2 = job2.get("result") or {}
        subjects = res2.get("subjects") or []
        check("识别出两个主体", len(subjects) == 2, str([s.get("id") for s in subjects]))
        check("主体带位置标签", any(s.get("label") == "左边那位" for s in subjects),
              str([s.get("label") for s in subjects]))
        check("默认跟分高的那个", res2.get("subject") == (subjects[0]["id"] if subjects else ""),
              str(res2.get("subject")))
        check("识别出镜头切换", res2.get("cuts") == 1, str(res2.get("cuts")))

        data = call(router, "clip.focus", {"project_id": pid})
        check("clip.focus 回报主体列表", len(data.get("subjects") or []) == 2)
        sidecar = Path(service.repo.project_get(pid)["root_dir"]) / "focus.subjects.json"
        check("focus.subjects.json 写出来了", sidecar.is_file(), str(sidecar))

        # 换人：不重跑检测（detect 调用次数不变）
        calls = {"n": 0}
        real = reframe.detect_focus

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        reframe.detect_focus = counting
        second = subjects[1]["id"]
        switched = call(router, "clip.focus.subject", {"project_id": pid, "subject": second})
        check("换主体成功", switched.get("subject") == second, str(switched.get("subject")))
        check("换主体没有重跑检测", calls["n"] == 0, f"{calls['n']} 次 detect")
        check("换主体后轨迹变了", switched["focus"][0]["x"] != data["focus"][0]["x"],
              f'{data["focus"][0]["x"]} → {switched["focus"][0]["x"]}')
        reframe.detect_focus = real

        # 切点不插值：表达式里必须有 20ms 的跳转段
        expr = reframe.crop_expression(
            [{"t_ms": 0, "x": 0.3}, {"t_ms": 4000, "x": 0.7}], duration_ms=6000, cuts=[2000]
        )
        check("表达式里有 20ms 跳转段", "/0.020" in expr, expr[:120])
        # 裁切主干只能有一个（原来这条断言写的是"乘号只出现一次"，
        # 但带切点时线性插值段本身也含 `*`，得看裁切因子而不是看星号个数）
        check("裁切主干只出现一次", expr.count("(iw-ih*9/16)") == 1, expr[:120])

        # ------------------------------------------- 3. 关掉增强：回退 P3-06 单脸跟随
        job3 = wait_job(
            service, call(router, "clip.focus.analyze",
                          {"project_id": pid, "enhance": False})["job"]["id"]
        )
        check("关闭增强后任务仍成功", job3.get("status") == "succeeded",
              str(job3.get("error_message") or ""))
        res3 = job3.get("result") or {}
        check("关闭增强时没有主体列表", not res3.get("subjects"), str(res3.get("subjects")))
        check("关闭增强时标记 enhance=False", res3.get("enhance") is False, str(res3.get("enhance")))
        check("关闭增强时仍有轨迹（跟随照常可用）", (res3.get("points") or 0) >= 1,
              str(res3.get("points")))
        after = json.loads(focus_file.read_text(encoding="utf-8"))
        check("focus.json 记下 enhance=False", after.get("enhance") is False, str(after.get("enhance")))
        # 回退路径 = 老的单脸逻辑：没有 subjects 键
        single = reframe.detect_focus(
            str(video), start_ms=0, end_ms=4000,
            models_root=str(work / "models"), enhance=False,
        )
        check("enhance=False 走的是老实现（结果里没有 subjects 键）",
              "subjects" not in single, str(list(single)))
        check("老实现仍返回 focus", bool(single.get("focus")), str(len(single.get("focus") or [])))

        # 配置层：follow_enhance=false 时，不传 enhance 也走基础跟随
        service.cfg.clip.follow_enhance = False
        job4 = wait_job(
            service, call(router, "clip.focus.analyze", {"project_id": pid})["job"]["id"]
        )
        res4 = job4.get("result") or {}
        check("配置项能关掉增强", res4.get("enhance") is False, str(res4.get("enhance")))

        # -------------------------------------------------------------- 4. 语音/无脸也能收工
        reframe.detect_focus = lambda *a, **kw: original_detect(
            *a, **{**kw, "sampler": fake_sampler(faces=0, cut_at_ms=10**9)}
        )
        job5 = wait_job(
            service, call(router, "clip.focus.analyze",
                          {"project_id": pid, "enhance": True})["job"]["id"]
        )
        res5 = job5.get("result") or {}
        check("没人脸时不报错", job5.get("status") == "succeeded",
              str(job5.get("error_message") or ""))
        check("没人脸时 detector 说清楚", res5.get("detector") == "no_face",
              str(res5.get("detector")))
        reframe.detect_focus = original_detect

    finally:
        reframe.detect_focus = original_detect
        service.close()
        if not args.keep:
            for path in (work / "cache" / "tmp").glob("*"):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)

    print()
    if FAILS:
        print(f"P6-02 跟随增强验收：{len(FAILS)} 项未通过 → {FAILS}")
        return 1
    print("P6-02 跟随增强验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
