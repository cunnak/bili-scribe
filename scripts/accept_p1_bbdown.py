#!/usr/bin/env python
"""P1-08 BBDown 兜底的验收（任务清单 P1-08）。

两条验收标准逐条对上：

1. **设置页显示 BBDown 状态** —— `download.backends` 必须真探测：没装时 `found=False`
   且给出安装提示；把文件放到配置路径上，立刻 `found=True` 并报出文件名。
2. **失败任务有「用 BBDown 重试」按钮** —— 按钮背后是 `download.retry_failed(backend="bbdown")`。
   这里不点像素，而是造一个**真实的失败批次任务行**，调那个 RPC，然后从库里读回新任务，
   断言它的 `payload.backend` 真的是 `bbdown`、且只带失败那一集。用户不点就不会变——
   换句话说，"不自动切换"这件事也在这里钉住：默认重试必须还是 yt-dlp。

另外在 downloader 层把兜底的后半段跑通：yt-dlp 报"需要大会员"→ 归成 AUTH_REQUIRED；
换后端后产物必须归一到 `source.mp4`、进度必须能从 BBDown 的百分比行里读出来。

跑法::

    python scripts/accept_p1_bbdown.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from video_workbench.app.service import WorkbenchService  # noqa: E402
from video_workbench.core import downloader as dl  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def call(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p1-bbdown")
    if not result.get("ok"):
        raise SystemExit(f"{method} 失败：{json.dumps(result.get('error'), ensure_ascii=False)}")
    return result["data"]


def error_of(router, method: str, params: dict) -> dict:
    result = router.dispatch(method, params, request_id="accept-p1-bbdown")
    if result.get("ok"):
        raise SystemExit(f"{method} 竟然成功了，预期要报错")
    return result["error"]


class FakeRunner:
    """假的进程 runner：按后端的样子往 work-dir 里放文件，并按需失败。"""

    def __init__(self, *, product: str = "【标题】某视频.mp4", code: int = 0, out: str = ""):
        self.calls: list[list[str]] = []
        self.product = product
        self.code = code
        self.out = out

    def __call__(self, argv, cwd, on_line, cancel):
        self.calls.append(list(argv))
        if self.product and self.code == 0:
            Path(cwd, self.product).write_bytes(b"fake video" * 2000)
        for line in (self.out or "").splitlines():
            on_line(line)
        return self.code, self.out


def failed_batch(service, *, backend: str = "", bvid: str = "BV1xx411c7mD") -> str:
    """造一个真的失败批次：两条成功、一条失败。"""
    job_id = f"job_accept_{bvid[-4:].lower()}_{backend or 'ytdlp'}"
    service.repo.job_create(
        job_id,
        "download.batch",
        None,
        {
            "quality": "1080P",
            "up_name": "P1-08 用例",
            "collection_title": "兜底用例",
            "backend": backend,
        },
    )
    service.repo.job_update(
        job_id,
        status="failed",
        result={
            "total": 3,
            "ok": 2,
            "failed": 1,
            "items": [
                {"bvid": bvid, "title": "第 1 集", "page": 1, "status": "succeeded"},
                {"bvid": bvid, "title": "第 2 集", "page": 2, "status": "succeeded"},
                {"bvid": bvid, "title": "第 3 集（大会员）", "page": 3, "status": "failed"},
            ],
        },
    )
    return job_id


def main() -> int:
    # GBK 控制台会把中文和 − 打成乱码，脚本自己的输出先兜住
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    work = PROJ / "build" / "accept" / "p1_bbdown"
    if work.exists() and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
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
    }
    cfg_path = work / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    service = WorkbenchService(config_path=str(cfg_path), picker=None)
    router = service.router
    try:
        # ------------------------------------------------ 1. 探测：没装时说清楚
        data = call(router, "app.info", {})
        check("app.info 可调用", bool(data.get("name")), str(data.get("version")))
        backends = call(router, "download.backends", {})
        check(
            "后端接口给出默认后端", backends.get("default") == "ytdlp", str(backends.get("default"))
        )
        check(
            "yt-dlp 探测到（本机有）",
            bool(backends["ytdlp"]["found"]),
            json.dumps(backends["ytdlp"], ensure_ascii=False),
        )
        check("没装 BBDown 时 found=False", backends["bbdown"]["found"] is False)
        check(
            "没装 BBDown 时给安装提示",
            "BBDown" in str(backends["bbdown"].get("hint", "")),
            str(backends["bbdown"].get("hint", "")),
        )

        # 把文件放到配置路径上 → 立刻被认出来
        fake_bbdown = work / "tools" / "BBDown.exe"
        fake_bbdown.parent.mkdir(parents=True, exist_ok=True)
        fake_bbdown.write_bytes(b"MZ fake")
        service.cfg.download.bbdown_path = str(fake_bbdown)
        detected = call(router, "download.backends", {})["bbdown"]
        check(
            "配上路径后 BBDown 被检出",
            detected["found"] is True,
            json.dumps(detected, ensure_ascii=False),
        )
        check(
            "检出的名字是 BBDown.exe", detected["name"] == "BBDown.exe", str(detected.get("name"))
        )
        service.cfg.download.bbdown_path = ""

        # ------------------------------------------------ 2. downloader：失败归类与产物归一
        ep = work / "ep"
        fail_runner = FakeRunner(product="", code=1, out="ERROR: 需要大会员才能观看")
        try:
            dl.download(url="https://b23.tv/accept", home=ep, backend="ytdlp", runner=fail_runner)
            raise SystemExit("yt-dlp 失败样例竟然成功了")
        except dl.WBError as exc:  # type: ignore[attr-defined]
            check("yt-dlp 的权限失败归成 AUTH_REQUIRED", exc.code == "AUTH_REQUIRED", exc.message)
        check(
            "yt-dlp 这条路仍走 yt_dlp（没被悄悄换成 BBDown）",
            "BBDown" not in fail_runner.calls[0][0] and "-m" in fail_runner.calls[0],
            " ".join(fail_runner.calls[0])[:120],
        )

        events: list[dict] = []
        ok_runner = FakeRunner(out="[进度] 10%\n[进度] 50%\n[进度] 100%")
        result = dl.download(
            url="https://b23.tv/accept",
            home=ep,
            quality="720P",
            backend="bbdown",
            bbdown_bin=str(fake_bbdown),
            on_event=events.append,
            runner=ok_runner,
        )
        check(
            "兜底后端用的是配置里的 BBDown",
            ok_runner.calls[0][0] == str(fake_bbdown),
            ok_runner.calls[0][0],
        )
        check("BBDown 收到 --work-dir", "--work-dir" in ok_runner.calls[0])
        check("产物归一到 source.mp4", Path(result.path).name == "source.mp4", str(result.path))
        check("产物文件真的存在", Path(result.path).is_file(), str(result.path))
        percents = [e["percent"] for e in events if e.get("type") == "progress"]
        check("从 BBDown 百分比行读出进度", 50.0 in percents, str(percents))

        # 认不出的后端要报错，不能悄悄换成别的
        bad = error_of(
            router, "download.start", {"url": "u", "items": [{"bvid": "BV1"}], "backend": "aria2"}
        )
        check("未知后端 → VALIDATION", bad["code"] == "VALIDATION", bad.get("message", ""))

        # ------------------------------------------------ 3. 失败项重试：换后端要显式
        job_id = failed_batch(service)
        retried = call(router, "download.retry_failed", {"id": job_id, "backend": "bbdown"})
        new_id = str((retried.get("job") or {}).get("id") or "")
        check("重试返回了新任务", bool(new_id), new_id)

        payload = service.repo.job_get(new_id).get("payload") if new_id else None
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = payload or {}
        check(
            "新任务的 backend 是 bbdown",
            payload.get("backend") == "bbdown",
            str(payload.get("backend")),
        )
        items = payload.get("items") or []
        check("只带失败那一集", len(items) == 1, f"{len(items)} 条")
        check(
            "带的是第 3 集（大会员那条）",
            items and int(items[0].get("page") or 0) == 3,
            str(items[0] if items else {}),
        )

        # 默认重试仍走 yt-dlp：兜底绝不能变成自动行为
        plain = call(router, "download.retry_failed", {"id": job_id})
        plain_id = str((plain.get("job") or {}).get("id") or "")
        plain_payload = service.repo.job_get(plain_id).get("payload") if plain_id else None
        if isinstance(plain_payload, str):
            plain_payload = json.loads(plain_payload)
        plain_payload = plain_payload or {}
        check(
            "默认重试不带 bbdown",
            plain_payload.get("backend") == "",
            str(plain_payload.get("backend")),
        )

        # 父任务本身用的是 bbdown 时，默认重试要沿用
        bb_parent = failed_batch(service, backend="bbdown", bvid="BV1yy411c7mE")
        kept = call(router, "download.retry_failed", {"id": bb_parent})
        kept_payload = service.repo.job_get(str((kept.get("job") or {}).get("id"))).get("payload")
        if isinstance(kept_payload, str):
            kept_payload = json.loads(kept_payload)
        check(
            "沿用父任务的后端",
            (kept_payload or {}).get("backend") == "bbdown",
            str((kept_payload or {}).get("backend")),
        )

        no_fail = "job_accept_all_ok"
        service.repo.job_create(no_fail, "download.batch", None, {"quality": "1080P"})
        service.repo.job_update(
            no_fail,
            status="succeeded",
            result={"total": 1, "items": [{"bvid": "BV1", "status": "succeeded"}]},
        )
        conflict = error_of(router, "download.retry_failed", {"id": no_fail})
        check("没有失败项 → CONFLICT", conflict["code"] == "CONFLICT", conflict.get("message", ""))
    finally:
        service.close()

    print()
    if FAILS:
        print(f"P1-08 BBDown 兜底验收：{len(FAILS)} 项失败")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("P1-08 BBDown 兜底验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
