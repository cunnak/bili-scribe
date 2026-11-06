"""P5-05 打包与首次启动验收：把"打出来的东西"当被测对象，而不是看构建日志。

    ./.venv/Scripts/python.exe scripts/accept_p5_package.py
    ./.venv/Scripts/python.exe scripts/accept_p5_package.py --dist dist/VideoWorkbench

断言分四组：

1. **产物结构**：exe 在、网页与提示词落进 `_internal/`、图标在。
2. **不该有的东西没被打进去**：FFmpeg、语音模型、字体——方案书 §0 的硬约束，
   体积与许可都不允许随包发。这条最容易被"顺手 copy 一下"破坏，所以要断言。
3. **跑得起来**：`exe --check` 在**干净配置**（临时目录）下 ok，且体检五项都在。
4. **不碰用户数据**：干净配置跑完只在自己的目录里建库，不去动 `~/.bili-scribe`。

全程不建窗口：`--check` 是桌面入口里"只装配服务"的分支，验收机（CI/无桌面）
也能跑。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
ROOT = PROJ
RESULTS: list[tuple[bool, str, str]] = []
_user_db_size = -1


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def call(fn):
    return fn()


# ---------------------------------------------------------------- 结构


def check_structure(dist: Path) -> None:
    exe = dist / "VideoWorkbench.exe"
    check("exe 存在", exe.exists(), str(exe))
    internal = dist / "_internal"
    for rel in (
        "video_workbench/web/index.html",
        "video_workbench/web/js/pages/onboard.js",
        "video_workbench/web/css/onboard.css",
        "video_workbench/web/css/tokens.css",
        "video_workbench/resources/prompts",
        "video_workbench/resources/icon.ico",
    ):
        path = internal / rel
        check(f"包内资源 {rel}", path.exists(), str(path))


def check_no_forbidden_payload(dist: Path) -> None:
    """方案书 §0：不打包 FFmpeg / 字体 / 语音模型。"""
    files = [p for p in dist.rglob("*") if p.is_file()]
    names = {p.name.lower() for p in files}
    check(
        "没有把 ffmpeg 打进去",
        "ffmpeg.exe" not in names and "ffprobe.exe" not in names,
        "、".join(sorted(n for n in names if n.startswith("ffm"))[:3]),
    )

    model_hits = [p.name for p in files if p.suffix.lower() in {".bin", ".pt", ".onnx"}]
    # onnxruntime 自带的小算子库不算"语音模型"；whisper 的 CTranslate2 权重叫 model.bin
    heavy = [name for name in model_hits if name.lower() in {"model.bin", "model.onnx"}]
    check("没有把语音模型打进去", not heavy, "、".join(heavy[:3]))

    font_hits = [p.name for p in files if p.suffix.lower() in {".ttf", ".ttc", ".otf"}]
    check("没有把字体打进去", not font_hits, "、".join(font_hits[:3]))

    size_mb = sum(p.stat().st_size for p in files) / 1024 / 1024
    # 396 MB 是"带 ASR 依赖、不带模型"的量级；超过 900 MB 基本等于把模型塞进去了
    check("体积在合理区间（< 900 MB）", size_mb < 900, f"{size_mb:.1f} MB")


# ---------------------------------------------------------------- 跑起来


def run_check(dist: Path, cfg: Path, log: Path) -> tuple[int, str]:
    if log.exists():
        log.unlink()
    proc = subprocess.run(
        [str(dist / "VideoWorkbench.exe"), "--check", "--config", str(cfg)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out and log.exists():
        out = log.read_text(encoding="utf-8", errors="replace")
    return proc.returncode, out


def check_runs(dist: Path, tmp: Path) -> None:
    media = tmp / "media"
    cache = tmp / "cache"
    models = tmp / "models"
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "app:\n  language: zh-CN\n"
        "storage:\n"
        f"  media_root: '{(media.as_posix())}'\n"
        f"  cache_root: '{(cache.as_posix())}'\n"
        f"  models_root: '{(models.as_posix())}'\n"
        f"  db_path: '{(tmp / 'wb.db').as_posix()}'\n",
        encoding="utf-8",
    )
    log = dist / "video-workbench.log"
    global _user_db_size
    user_db = Path.home() / ".bili-scribe" / "bili-scribe.db"
    _user_db_size = user_db.stat().st_size if user_db.exists() else -1
    rc, out = run_check(dist, cfg, log)
    check("exe --check 退出码 0", rc == 0, f"rc={rc}")
    check("--check 报 ok", '"ok":true' in "".join(out.split()), out[:200])

    payload = None
    m = re.search(r"\{[\s\S]*\}\s*$", out)
    if m:
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            payload = None
    if payload:
        rpc_names = payload.get("rpc") or []
        check(
            "RPC 表非空且包含关键命名",
            len(rpc_names) > 60 and "app.onboard" in rpc_names,
            f"{len(rpc_names)} 个",
        )
        onboard = payload.get("onboard") or {}
        check("体检五项齐全", onboard.get("total") == 5, str(onboard.get("steps")))
        check(
            "体检带得出 ready 结论",
            isinstance(onboard.get("ready"), bool),
            str(onboard.get("ready")),
        )
    else:
        check("--check 输出可解析为 JSON", False, out[:160])

    check("在干净配置下建了自己的库", (tmp / "wb.db").exists(), str(tmp / "wb.db"))
    user_db = Path.home() / ".bili-scribe" / "bili-scribe.db"
    untouched = (not user_db.exists()) or user_db.stat().st_size == _user_db_size
    check(
        "没去动用户默认库", untouched, "用户库大小未变" if untouched else f"用户库被动过：{user_db}"
    )

    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        check(
            "启动日志没有 Traceback",
            "Traceback" not in text,
            text[-200:] if "Traceback" in text else "",
        )
    else:
        check("启动日志已生成", False, "找不到 video-workbench.log")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P5-05 打包与首次启动验收")
    parser.add_argument("--dist", default=str(PROJ / "dist" / "VideoWorkbench"))
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    if not dist.exists():
        print(f"找不到打包产物：{dist}\n先跑 ./.venv/Scripts/python.exe scripts/build_wb.py")
        return 2

    print(f"[accept] 被测产物：{dist}")
    check_structure(dist)
    check_no_forbidden_payload(dist)

    tmp = Path(tempfile.mkdtemp(prefix="wb-accept-p505-"))
    try:
        check_runs(dist, tmp)
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [item for item in RESULTS if not item[0]]
    print()
    if failed:
        print(
            f"P5-05 打包验收：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过，{len(failed)} 项失败"
        )
        for _, name, detail in failed:
            print(f"  - {name}：{detail}")
        return 1
    print(f"P5-05 打包与首次启动验收：全部通过（{len(RESULTS)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
