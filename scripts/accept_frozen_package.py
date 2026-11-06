r"""打包版（onedir）交付验收：把「这个包里到底有没有今天那些改动」变成一条可复跑的命令。

    .venv/Scripts/python.exe scripts/accept_frozen_package.py [包目录]

默认包目录 `dist/VideoWorkbench`；发版前如果 `dist` 被正在运行的 exe 占着
（实测 `PermissionError: [WinError 5] ... charset_normalizer\cd.cp314-win_amd64.pyd`），
就指到 PyInstaller 的 staging 目录去（`build/stage-0.2.2/VideoWorkbench`）。

为什么要有这个脚本：冻结态没有 stdout（`console=False`、exe `Subsystem=2`），自检结果只能
落成 exe 旁边的 json；而「PyInstaller 退出码 0」根本不能证明**内容**是新一代的 —— 今天
`dist` 里那份 exe 就比仓库晚了两笔提交，靠肉眼在 `_internal` 里搜字符串才发现。

五类检查（任何一条 FAIL 就 rc=1）：

1. exe 在；
2. 六个提示词真源 txt 字节数与仓库一致（名单现读自 `scripts/build_wb.PROMPT_TXT_NAMES`，
   不抄第二份名单 —— 抄了就有两个 owner，`test_git_no_normalization_on_locked_paths.py`
   那把闸就是靠「锁清单现读自 build_wb」才只有一份真源）；
3. 包内 `settings.js` 有今天的截帧 help，旧的两句假文案不许回来；
4. 新模块在不在包里，判据是 PyInstaller 的 `PYZ-00.toc`，**不是**对二进制裸串 grep：
   函数名在压缩 marshal 里，裸串永远搜不到（今天就在这一条上误判过一次「包是旧的」）；
5. `--check` / `--roles` 各跑一次：退出码 0、包内自显示版本 == 仓库 `__version__`
   （不钉字面量，钉的是 owner 那条）、六把角色 + 三把退役仍在；
6. 验收前后三个真实配置文件的 sha256 逐字相同 —— 窗口可能在你看不见的时候写盘。

读数一律落到系统临时区（`wb-frozen-accept.json`），不落仓库根：这份脚本会被
`sync_mirror` 搬进发布仓，产物不落仓才不会在下一次同步里被当成漂移。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 名单只有一个 owner：现读自 build_wb，不在此处另抄六个文件名。
from scripts.build_wb import PROMPT_TXT_NAMES  # noqa: E402

OUT = Path(tempfile.gettempdir()).resolve() / "wb-frozen-accept.json"

# 今天（task-70 界面腿）钉住的文案：新句必须在、旧两句不许回来。
HELP_MUST = "总结时附上画面：PPT、图表、代码里的字能进正文。"
HELP_MUST_NOT = ("给总结配图用；关掉可明显提速。", "省流量就把截帧关掉。")

RETIRED = ["shipin", "standard", "xueshu"]

CONFIG_FILES = (
    Path.home() / ".video-workbench" / "config.yaml",
    Path.home() / ".bili-scribe" / "config.yaml",
    Path.home() / ".bili-scribe" / "workbench.json",
)


def sha256(path: Path) -> str:
    if not path.is_file():
        return "ABSENT"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def configs() -> dict:
    return {str(p): sha256(p) for p in CONFIG_FILES}


def run_switch(exe: Path, flag: str, name: str) -> tuple[int, dict]:
    """冻结态没有 stdout，只能读 exe 旁边落的那份 json；先删旧的，别拿上一轮的读数交差。"""
    payload = exe.parent / name
    if payload.exists():
        payload.unlink()
    proc = subprocess.run(
        [str(exe), flag], cwd=str(exe.parent), capture_output=True, text=True, timeout=300
    )
    data = json.loads(payload.read_text(encoding="utf-8")) if payload.is_file() else {}
    return proc.returncode, data


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "dist" / "VideoWorkbench"
    exe = root / "VideoWorkbench.exe"
    checks: list[dict] = []
    before = configs()

    def check(name: str, ok: bool, note: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "note": note})
        print(("PASS " if ok else "FAIL ") + name + ("  " + note if note else ""), flush=True)

    if not exe.is_file():
        check("exe 存在", False, str(exe))
        OUT.write_text(
            json.dumps({"root": str(root), "checks": checks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 1
    check("exe 存在", True, f"{exe.stat().st_size} B @ {exe}")

    internal = root / "_internal"
    prompts_dir = internal / "video_workbench" / "resources" / "prompts"
    bad = []
    for name in PROMPT_TXT_NAMES:
        packed = prompts_dir / name
        source = REPO / "video_workbench" / "resources" / "prompts" / name
        if not packed.is_file():
            bad.append(f"{name} 不在包里")
        elif packed.stat().st_size != source.stat().st_size:
            bad.append(f"{name} 包内 {packed.stat().st_size} != 仓库 {source.stat().st_size}")
    check(
        "六个提示词真源字节数与仓库一致（名单现读自 build_wb.PROMPT_TXT_NAMES）",
        not bad,
        "; ".join(bad),
    )

    settings = internal / "video_workbench" / "web" / "js" / "pages" / "settings.js"
    if settings.is_file():
        text = settings.read_bytes().decode("utf-8", errors="replace")
        missing = HELP_MUST not in text
        revived = [s for s in HELP_MUST_NOT if s in text]
        check(
            "包内 settings.js 是改了文案的那一代",
            (not missing) and (not revived),
            ("缺新句 " if missing else "")
            + ("旧句回来了 " + "、".join(revived) if revived else ""),
        )
    else:
        check("包内 settings.js 存在", False, str(settings))

    tocs = list((REPO / "build").rglob("PYZ-00.toc"))
    hit = None
    for toc in tocs:
        try:
            if "video_workbench.core.frames" in toc.read_text(encoding="utf-8", errors="replace"):
                hit = toc
                break
        except OSError:
            continue
    check(
        "新模块 video_workbench/core/frames.py 进了包（判据 = PYZ-00.toc）",
        hit is not None,
        str(hit) if hit else f"扫了 {len(tocs)} 份 toc 都没命中",
    )

    from video_workbench import __version__ as repo_version

    rc, report = run_switch(exe, "--check", "wb-check.json")
    check(
        "--check 退出码 0 且报 ok",
        rc == 0 and bool(report.get("ok")),
        f"rc={rc} ok={report.get('ok')}",
    )
    check(
        "包内自显示版本 == 仓库 __version__（不钉字面量）",
        # 版本在 --check 载荷的 info 里（app.info 那一层），不在顶层；第一版按顶层读，
        # 读出 None 就误判成「包是旧的」—— 与「裸串搜不到函数名」是同一族读法错误。
        str((report.get("info") or {}).get("version")) == repo_version,
        f"包内 {(report.get('info') or {}).get('version')} / 仓库 {repo_version}",
    )

    rc, roles = run_switch(exe, "--roles", "wb-roles.json")
    keys = [r.get("key") for r in (roles.get("roles") or [])]
    check(
        "--roles 退出码 0 且六把角色",
        rc == 0 and roles.get("count") == 6,
        f"rc={rc} count={roles.get('count')} keys={keys}",
    )
    check(
        "三把退役角色仍在名单里",
        sorted(roles.get("retired") or []) == sorted(RETIRED),
        str(roles.get("retired")),
    )

    after = configs()
    check("验收前后三份真实配置一字未动", after == before, f"前 {before} 后 {after}")

    OUT.write_text(
        json.dumps(
            {
                "root": str(root),
                "configs_before": before,
                "configs_after": after,
                "check": report,
                "roles": keys,
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    total = len(checks)
    passed = sum(1 for c in checks if c["ok"])
    print(f"读数产物：{OUT}", flush=True)
    print(f"合计：{passed}/{total} PASS", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
