r"""打包版（onedir）交付验收：把「这个包里到底有没有今天那些改动」变成一条可复跑的命令。

    .venv/Scripts/python.exe scripts/accept_frozen_package.py [包目录]

默认包目录 `dist/VideoWorkbench`；发版前如果 `dist` 被正在运行的 exe 占着
（实测 `PermissionError: [WinError 5] ... charset_normalizer\cd.cp314-win_amd64.pyd`），
就指到 PyInstaller 的 staging 目录去（`build/stage-0.2.2/VideoWorkbench`）。

为什么要有这个脚本：冻结态没有 stdout（`console=False`、exe `Subsystem=2`），自检结果只能
落成 exe 旁边的 json；而「PyInstaller 退出码 0」根本不能证明**内容**是新一代的 —— 今天
`dist` 里那份 exe 就比仓库晚了两笔提交，靠肉眼在 `_internal` 里搜字符串才发现。

六类检查（任何一条 FAIL 就 rc=1；健康包上共 9 条断言——exe/设置/toc 各自「不存在」那
三条只在坏包上才会跑到，所以这里的类数与读数里的分母不是同一个数）：

1. exe 在；
2. 六个提示词真源 txt 字节数与仓库一致（名单现读自 `scripts/build_wb.PROMPT_TXT_NAMES`，
   不抄第二份名单 —— 抄了就有两个 owner，`test_git_no_normalization_on_locked_paths.py`
   那把闸就是靠「锁清单现读自 build_wb」才只有一份真源）；
3. 包内 `settings.js` 有今天的截帧 help，旧的两句假文案不许回来；
4. **这份包是不是改完之后重建的**，判据是 PyInstaller 的 `PYZ-00.toc`，**不是**对二进制裸串
   grep：函数名在压缩 marshal 里，裸串永远搜不到（v0.9.3 就在这一条上误判过一次「包是旧的」）。
   只判「模块在不在」不够——`core/frames.py` 从 0.2.2 起就在包里，上一轮的旧包同样命中，
   于是「重建之后再验收」会绿在重建**之前**那份产物上。所以两件事一起判：只认本 spec 那一条
   toc（`build/<spec stem>/PYZ-00.toc`，不再 rglob 全树——全树里还躺着 bs 的
   `build/exe/bili-scribe/PYZ-00.toc`），且它的 mtime 不早于 `video_workbench/` 下最新的源文件；
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
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 名单只有一个 owner：现读自 build_wb，不在此处另抄六个文件名。
from scripts.build_wb import PROMPT_TXT_NAMES, SPEC  # noqa: E402

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

    # 判「某个模块在不在包里」只有 PYZ-00.toc 一种读法（exe 与 _internal 里的名字在压缩 marshal 里，
    # 裸串永远搜不到 —— AGENTS.md 记着 v0.9.3 那次差点据此判「包是旧的」）。但只判「在不在」不够：
    # frames.py 从 0.2.2 起就在包里，上一轮的旧包同样命中，于是「重建之后再验收」会绿在重建之前的
    # 那份产物上 —— 这条判据就退化成一句「历史上曾经打过包」。所以两件事一起判：
    #   ① 只认本 spec 那一条 toc。PyInstaller 的 workpath 子目录用 **spec 文件 stem** 命名
    #      （video_workbench.spec ⇒ build/video_workbench），路径可现算，不抄第二份；不再 rglob
    #      全树 —— 全树里还躺着 bs 的 build/exe/bili-scribe/PYZ-00.toc，谁先被扫到谁说了算。
    #   ② toc 的 mtime 必须不早于 video_workbench/ 下最新的源文件，否则这份包是改之前建的。
    toc = REPO / "build" / SPEC.stem / "PYZ-00.toc"
    newest, newest_at = 0.0, None
    for path in (REPO / "video_workbench").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        stamp = path.stat().st_mtime
        if stamp > newest:
            newest, newest_at = stamp, path
    if not toc.is_file():
        check("PYZ-00.toc 在（判据 = build/<spec stem>/PYZ-00.toc）", False, str(toc))
    else:
        text = toc.read_text(encoding="utf-8", errors="replace")
        has_module = "video_workbench.core.frames" in text
        toc_at = toc.stat().st_mtime
        fresh = toc_at >= newest
        note = (
            f"{toc.stat().st_size} B @ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(toc_at))}"
        )
        if newest_at is not None:
            note += (
                f"；最新源 {newest_at.relative_to(REPO)} @ "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest))}"
            )
        if not has_module:
            note += "；toc 里没有 video_workbench.core.frames"
        if not fresh:
            note += "；这份包比源文件旧 —— 先重建再验收"
        check(
            "包是改完之后重建的，且新模块 video_workbench/core/frames.py 在里面",
            has_module and fresh,
            note,
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

    # ---- 三态改造那一批（前端静态资源 + 后端 disk 字段）
    #
    # 为什么读「包内文件内容」而不是 PYZ-00.toc：三态是 js/css，PyInstaller 把
    # video_workbench/web 整份拷进 _internal/video_workbench/web（video_workbench.spec 的
    # datas），文件内容本身就是真凭据；toc 只登记 Python 模块，对 js/css 无话可说。
    #
    # 为什么既有「必须含」也有「必须不含」：ui.placeholder 与 .rail__count 是这次**删掉**的。
    # 只判「新的在」会绿在一份新旧都还在的包上 —— 那种包说明构建收的是半新半旧的树。
    web = internal / "video_workbench" / "web"

    def _packed(rel: str) -> str:
        path = web / rel
        if not path.is_file():
            return ""
        return path.read_bytes().decode("utf-8", errors="replace")

    ui_js = _packed("js/components/ui.js")
    tri_missing = [
        name
        for name in ("ui.loading = function", "ui.errorBlock = function", "ui.errorFrom = function")
        if name not in ui_js
    ]
    check(
        "包内 ui.js 有三个三态原语（loading / errorBlock / errorFrom）",
        bool(ui_js) and not tri_missing,
        ("缺 " + "、".join(tri_missing) if tri_missing else "") if ui_js else "包内没有 ui.js",
    )
    check(
        "包内 ui.placeholder 已删除（只判新的在会绿在半新半旧的包上）",
        bool(ui_js) and "ui.placeholder = function" not in ui_js,
        "" if ui_js else "包内没有 ui.js",
    )
    router_js = _packed("js/router.js")
    check(
        "包内 router.js 不再生成左栏空角标 .rail__count",
        bool(router_js) and "rail__count" not in router_js,
        "" if router_js else "包内没有 router.js",
    )
    app_js = _packed("js/app.js")
    disk_missing = [n for n in ("function paintDisk", "status-conn") if n not in app_js]
    check(
        "包内 app.js 有磁盘槽 paintDisk 与「后端连接」独立轴",
        bool(app_js) and not disk_missing,
        ("缺 " + "、".join(disk_missing) if disk_missing else "") if app_js else "包内没有 app.js",
    )
    app_css = _packed("css/app.css")
    check(
        "包内 app.css 有加载态转圈动画 wb-spin",
        bool(app_css) and "@keyframes wb-spin" in app_css,
        "" if app_css else "包内没有 app.css",
    )
    publish_js = _packed("js/pages/publish.js")
    publish_missing = [
        name for name in ("pb-bvid", "pb-aid", "本地导入的视频没有视频号") if name not in publish_js
    ]
    check(
        "包内 publish.js 有本地项目视频号输入栏",
        bool(publish_js) and not publish_missing,
        ("缺 " + "、".join(publish_missing) if publish_missing else "")
        if publish_js
        else "包内没有 publish.js",
    )
    disk = (report.get("info") or {}).get("disk")
    disk_ok = (
        isinstance(disk, dict)
        and isinstance(disk.get("free_bytes"), int)
        and isinstance(disk.get("total_bytes"), int)
        and 0 <= disk["free_bytes"] <= disk["total_bytes"]
    )
    check(
        "--check 载荷带 info.disk 且 free/total 都是整数（状态条磁盘槽的数据源）",
        disk_ok,
        f"disk={disk!r}",
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
