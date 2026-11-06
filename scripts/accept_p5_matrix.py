"""P5-06 全量验收矩阵：把已经写好的验收脚本按方案书 §8.1–8.7 分组跑一遍，
再把"两套皮肤 × 全部页面"的视觉矩阵跑一遍，最后写 `验收记录.md`。

    ./.venv/Scripts/python.exe scripts/accept_p5_matrix.py
    ./.venv/Scripts/python.exe scripts/accept_p5_matrix.py --skip-network   # 不跑真站下载
    ./.venv/Scripts/python.exe scripts/accept_p5_matrix.py --only 8.3 --only 8.5

为什么不把断言重写一遍：每一条断言都在对应脚本里有真实实测值（时长、字节数、
分辨率、note_id），把结论抄一遍反而更难核对。这里只做**编排 + 汇总 + 落账**，
失败时把脚本的最后几行原样贴进记录里，方便一眼看到红在哪。

视觉矩阵靠 `scripts/shoot_wb.py`（headless Chrome + `?mock=1`）。它每套皮肤拍
一整套页面，本脚本只断言"该在的文件都在"，像素审视仍在人工那一步——脚本能证明
页面渲染出来了，证明不了好不好看。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
PY = sys.executable
CFG = str(PROJ / "build" / "accept" / "p2.yaml")
RECORD = PROJ / "验收记录.md"
STATE = PROJ / "build" / "accept" / "matrix" / "state.json"


@dataclass
class Step:
    script: str
    args: list[str] = field(default_factory=list)
    note: str = ""
    network: bool = False
    outcome: str = ""  # passed / failed / skipped
    last: str = ""  # 脚本最后一行
    seconds: float = 0.0


@dataclass
class Group:
    key: str
    title: str
    spec: list[str]  # 方案书 §8.x 的条目（照抄，便于对照）
    steps: list[Step]
    extra: list[str] = field(default_factory=list)


def _s(script: str, *args: str, note: str = "", network: bool = False) -> Step:
    return Step(script=script, args=list(args), note=note, network=network)


GROUPS: list[Group] = [
    Group(
        key="8.1",
        title="新建任务",
        spec=[
            "粘贴 BV 链接能解析出标题/UP/封面/时长",
            "分P 能展开选择",
            "合集能列出子项",
            "仅总结不下载也能出总结（需平台字幕）",
            "完整处理能创建项目并跳转",
            "解析失败有明确错误，不是空白",
        ],
        steps=[
            _s(
                "accept_p1_download.py",
                "--url",
                "BV1GJ411x7h7",
                "--quality",
                "720P",
                note="真站解析 + 下载 + 缩略图 + 侧车（解析失败/权限不足的归因也在里面）",
                network=True,
            ),
            _s("accept_p2_summary.py", "--config", CFG, note="「仅总结」路径：有字幕即可出总结"),
            _s(
                "accept_p1_bbdown.py",
                note="yt-dlp 失败后的兜底：后端探测 + 失败项显式换 BBDown 重试（不自动切换）；自带隔离库",
            ),
        ],
    ),
    Group(
        key="8.2",
        title="素材库",
        spec=[
            "树/列表/详情三栏在 1100×720 不溢出",
            "100 个项目滚动不卡",
            "阶段 chips 与实际数据一致",
            "删除默认不删文件；勾选后才删",
            "缺失文件标红，能重新定位",
        ],
        steps=[
            _s("accept_p2_editor.py", "--config", CFG, note="库→字幕入口、阶段推进、缺失文件处理"),
            _s(
                "accept_p5_perf.py", "--skip-bench", note="100 段/1000 段渲染与滚动（不重跑 bench）"
            ),
        ],
        extra=["视觉矩阵 library / library-1100（两皮肤，见下）"],
    ),
    Group(
        key="8.3",
        title="切片台",
        spec=[
            "生成 5–12 个候选，卡片显示分数/时长/理由",
            "点卡片能 seek 并播放",
            "拖切点有磁吸线；改完能保存",
            "原比例导出成功，时长与切点一致（≤1 帧；快速模式除外）",
            "9:16 导出成功，分辨率 1080×1920",
            "硬字幕可见；软字幕可开关；不烧录无字幕",
            "取消导出不留半成品",
        ],
        steps=[
            _s("accept_p3_clip.py", "--config", CFG, note="候选生成 + 吸附去重"),
            _s("accept_p3_clips.py", "--config", CFG, note="RPC 层切片台：读库/增删改查"),
            _s("accept_p3_edit.py", "--config", CFG, note="切点编辑：帧对齐、锁定、吸附"),
            _s("accept_p3_export.py", "--config", CFG, note="原比例导出：精确 0ms / 快速带偏移"),
            _s("accept_p3_vertical.py", "--config", CFG, note="9:16 三模式 + 无黑边"),
            _s("accept_p3_styles.py", "--config", CFG, note="样式改完导出生效（硬烧对比）"),
            _s("accept_p3_copy.py", "--config", CFG, note="文案与封面"),
            _s("accept_p3_batch.py", "--config", CFG, note="批量导出 + 失败重试"),
        ],
    ),
    Group(
        key="8.4",
        title="合并",
        spec=[
            "3 个同规格片段快速拼接成功，总时长 = 各段之和（≤100ms）",
            "不同规格触发预检阻止快速拼接，精确模式成功",
            "取消不留半成品",
            "输出文件名不覆盖已有文件",
        ],
        steps=[
            _s("accept_p4_merge.py", "--config", CFG, note="快速拼接 + 预检拦截"),
            _s("accept_p4_precise.py", "--config", CFG, note="精确合并 + 取消 + 回退"),
        ],
    ),
    Group(
        key="8.5",
        title="发布",
        spec=[
            "三道门禁全绿才可点发布",
            "摘要 201 字被阻止，提示明确",
            "发布成功存 note_id，发布中心可见",
            "撤回后状态变 withdrawn，记录保留",
            "重复发布同一视频被阻止",
        ],
        steps=[
            _s("accept_p4_publish.py", "--config", CFG, note="草稿/门禁/字数/更新时间"),
            _s(
                "accept_p4_publish_do.py",
                "--config",
                CFG,
                note="勾确认→发布→撤回 全链路（替身客户端）",
            ),
            _s("accept_p4_records.py", "--config", CFG, note="发布记录 + 账号级限流"),
        ],
    ),
    Group(
        key="8.6",
        title="设置与皮肤",
        spec=[
            "两套皮肤在全部页面可切换，无残留硬编码颜色",
            "字幕样式改动实时预览，保存后导出生效",
            "模型测试连接成功/失败都有明确提示",
            "修改存储路径后，新项目写入新路径，旧项目仍可打开",
        ],
        steps=[
            _s("accept_p2_optimize.py", "--config", CFG, note="llm.test 成功/失败两态"),
            _s("check_colors.py", note="除 tokens.css 外禁硬编码颜色"),
        ],
        extra=[
            "视觉矩阵两套皮肤全套页面（见下）",
            "`accept_p3_styles.py` 已覆盖「样式保存后导出生效」",
        ],
    ),
    Group(
        key="8.7",
        title="任务与恢复",
        spec=[
            "下载/转录/导出/合并都能取消，进程被杀死",
            "失败任务能重试，不重复已完成步骤",
            "强制退出后重启，运行中任务标记 interrupted，可重试",
            "日志目录有本次会话日志",
        ],
        steps=[
            _s("accept_p5_cancel.py", "--config", CFG, note="取消/超时/杀进程树/半成品清理"),
            _s("accept_p5_recovery.py", "--config", CFG, note="强杀重启→INTERRUPTED→重试成功"),
            _s("accept_p5_diagnostics.py", "--config", CFG, note="日志落盘 + 诊断包脱敏"),
        ],
    ),
    Group(
        key="loop",
        title="闭环回放（P2 文本链）",
        spec=["总结→优化→翻译→导出→合集总览 一条链上的断言，逐个复跑"],
        steps=[
            _s(
                "accept_p2_transcribe.py",
                "--reuse",
                note="转录（复用已有产物，不重跑模型；该脚本自建配置，不收 --config）",
            ),
            _s("accept_p2_optimize.py", "--config", CFG, note="LLM 优化 diff/应用/回滚"),
            _s("accept_p2_translate.py", "--config", CFG, note="翻译草稿/术语/双语 ASS"),
            _s("accept_p2_export.py", "--config", CFG, note="SRT/ASS/VTT + 软硬字幕"),
            _s("accept_p2_collection.py", "--config", CFG, note="合集总览"),
        ],
    ),
    Group(
        key="p6",
        title="可选增强（Phase 6）",
        spec=[
            "画面精析把真帧送给了 vision 模型（不是只抽帧没发）",
            "精析后候选排序跟着画面变，起止时间不动",
            "无 vision 配置时自动跳过，端点零调用",
            "人声段落 BGM 自动压低、静音段原样；淡入淡出不削波",
            "一条命令跑完单稿闭环；退出码能区分成功/任务失败/参数错",
        ],
        steps=[
            _s(
                "accept_p6_refine.py",
                note="抽帧+响度→vision 重打分；未配置/点名关掉/源文件缺失三种跳过",
            ),
            _s(
                "accept_p6_focus.py",
                note="真视频走 FaceDetectorYN 出轨迹；假采样器验多主体/切点；关增强回退单脸",
            ),
            _s(
                "accept_p6_bgm.py",
                note="sidechaincompress 闪避按 dB 量；淡入淡出对比首段/中段；换配乐不被 skip 跳过",
            ),
            _s(
                "accept_p6_nle.py",
                note="剪映草稿真写进草稿箱并打路径；OTIO 帧数/媒体引用；降级包四件齐",
            ),
            _s(
                "accept_p6_cli.py",
                note="一条命令跑完单稿闭环（转录→总结→选段→导出）；退出码 0/1/2 三态",
            ),
            _s(
                "accept_p6_burn.py",
                "--config",
                CFG,
                note="黑底合成素材数亮像素（另有不烧对照）：srt 喂 subtitles=、切片字幕平移回 0、软字幕轨按成片时间走",
            ),
        ],
    ),
]

# 页面清单写在 shoot_wb.py 的 SHOTS 里（当前 33 个：含 1100×720 与几处交互态）；
# 这里只断言"张数够不够"，具体清单以那份为准，避免两处名单不同步。
SHOT_SETS = {
    "ark": 33,
    "exhibition": 33,
}


VERDICT_RE = re.compile(r"(验收[:：]|全部通过|全部 ok|\[FAIL\]|通过，\d+ 项失败|check_colors\])")


def _verdict(lines: list[str]) -> str:
    """挑一行"结论"，而不是最后一行日志。

    验收脚本的 stderr 里夹着 logging 输出（`INFO video_workbench.job …`），直接取
    最后一行会把日志当结论抄进记录——记录里全是时间戳，红在哪一行都看不出来。
    """
    for line in reversed(lines):
        if VERDICT_RE.search(line):
            return line
    return ""


def _isolate_cfg(tag: str) -> str | None:
    """给一个脚本拷一份独立的验收库，返回临时配置路径。

    为什么必须隔离：这些脚本共用同一个 `build/accept/wb_p2.db`，而它们会互相踩
    ——`accept_p4_publish_do` 发出一条 published 记录，紧接着跑的
    `accept_p4_records`（断言"刚发过不能发"）与 `accept_p4_publish`（断言"三关全绿"）
    就被上一条记录卡住，报出与自己无关的红。P5-06 第一次全量跑就是这么红的：
    9 个"失败"里 7 个是互相污染，只有 2 个是真缺陷（过时的断言）。

    隔离方式：整库复制 + 只换 `storage.db_path`，媒体/缓存/模型目录照旧指向同一处
    （数据库里存的是绝对路径，换了媒体根反而找不到文件）。
    """
    src_db = PROJ / "build" / "accept" / "wb_p2.db"
    if not src_db.exists():
        return None
    work = PROJ / "build" / "accept" / "isolated" / tag
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db = work / "wb.db"
    shutil.copy2(src_db, db)
    cfg = PROJ / "build" / "accept" / "p2.yaml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    # 只替换库路径那一行，其余（媒体根/模型/接口地址）保持与真验收一致
    target = re.sub(r"(?m)^(\s*db_path:\s*).*$", lambda m: f"{m.group(1)}'{db.as_posix()}'", text)
    if target == text:
        # 这份 yaml 可能压根没写 db_path（走默认库），那就补一段。
        # 换行用 chr(10) 拼：直接写在源码里会被两层转义吃成真换行，然后 SyntaxError。
        nl = chr(10)
        target = text + nl.join(["", "storage:", f"  db_path: '{db.as_posix()}'", ""])
    out = work / "config.yaml"
    out.write_text(target, encoding="utf-8")
    return str(out)


def run_step(step: Step, *, timeout: int = 900) -> None:
    script = PROJ / "scripts" / step.script
    if not script.exists():
        step.outcome, step.last = "skipped", "脚本不存在"
        return
    args = list(step.args)
    if "--config" in args:
        idx = args.index("--config") + 1
        iso = _isolate_cfg(f"{step.script.replace('.py', '')}-{abs(hash(step.script)) % 9973}")
        if iso:
            args[idx] = iso
    cmd = [PY, str(script)] + args
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJ),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        step.outcome, step.seconds = "failed", time.time() - started
        step.last = f"超时（>{timeout}s）"
        return
    step.seconds = time.time() - started
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    step.outcome = "passed" if proc.returncode == 0 else "failed"
    step.last = _verdict(lines) or (lines[-1] if lines else f"（无输出，rc={proc.returncode}）")
    step._out = out  # type: ignore[attr-defined]


def run_visual(*, skins: list[str], height: int = 1400) -> dict[str, dict]:
    """两套皮肤各拍一整套页面，断言"该在的文件都在"。"""
    result: dict[str, dict] = {}
    for skin in skins:
        out_dir = PROJ / "docs" / "acceptance" / "matrix"
        cmd = [
            PY,
            str(PROJ / "scripts" / "shoot_wb.py"),
            "--skin",
            skin,
            "--out",
            str(out_dir),
            "--height",
            str(height),
        ]
        started = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(PROJ),
            capture_output=True,
            text=True,
            timeout=1800,
            encoding="utf-8",
            errors="replace",
        )
        files = sorted(p.name for p in out_dir.glob(f"{skin}-*.png"))
        want = SHOT_SETS.get(skin, 25)
        ok = proc.returncode == 0 and len(files) >= want
        result[skin] = {
            "ok": ok,
            "files": len(files),
            "want": want,
            "seconds": round(time.time() - started, 1),
            "tail": [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()][
                -1:
            ],
        }
    return result


def load_state() -> dict:
    """上次分批跑留下的各组结果（--only 才用得上）。"""
    if not STATE.exists():
        return {}
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict, run_groups: list[Group], visual: dict[str, dict]) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for group in run_groups:
        state[group.key] = {
            "at": stamp,
            "title": group.title,
            "steps": {
                s.script: {
                    "outcome": s.outcome,
                    "seconds": round(s.seconds, 1),
                    "last": s.last,
                    "note": s.note,
                }
                for s in group.steps
            },
        }
    real = {k: v for k, v in visual.items() if not v.get("skipped")}
    if real:
        state["_visual"] = real
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def hydrate(groups: list[Group], state: dict, run_keys: set[str]) -> dict[str, str]:
    """没在这次跑里的组，用上次的结果填上；返回 {group_key: 上次时间}。"""
    origin: dict[str, str] = {}
    for group in groups:
        if group.key in run_keys:
            continue
        saved = state.get(group.key) or {}
        steps = saved.get("steps") or {}
        if not steps:
            continue
        for step in group.steps:
            got = steps.get(step.script)
            if not got:
                continue
            step.outcome = str(got.get("outcome") or "")
            step.seconds = float(got.get("seconds") or 0.0)
            step.last = str(got.get("last") or "")
            if not step.note and got.get("note"):
                step.note = str(got["note"])
        origin[group.key] = str(saved.get("at") or "")
    return origin


def write_record(
    groups: list[Group],
    visual: dict[str, dict],
    *,
    skipped_network: bool,
    origin: dict[str, str] | None = None,
) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    origin = origin or {}
    total = sum(len(g.steps) for g in groups)
    passed = sum(1 for g in groups for s in g.steps if s.outcome == "passed")
    failed = [(g, s) for g in groups for s in g.steps if s.outcome == "failed"]
    skipped = [(g, s) for g in groups for s in g.steps if s.outcome == "skipped"]

    lines: list[str] = []
    lines.append("# VideoWorkbench 验收记录")
    lines.append("")
    lines.append(f"- 生成时间：{stamp}")
    lines.append(
        f"- 生成方式：`./.venv/Scripts/python.exe scripts/accept_p5_matrix.py`"
        f"{'（--skip-network）' if skipped_network else ''}"
    )
    if origin:
        keys = "、".join(f"§{k}（{v or '上次'}）" for k, v in sorted(origin.items()))
        lines.append(
            f"- 分批运行：本次只跑了 {len(groups) - len(origin)} 组，其余沿用上次结果 —— {keys}"
        )
    lines.append(
        f"- 断言来源：{total} 个验收脚本，逐条实测写在各自脚本与本目录 `docs/acceptance/p*` 里"
    )
    lines.append(
        f"- 结论：**{passed}/{total} 个脚本全过**"
        + (f"，{len(skipped)} 个跳过" if skipped else "")
        + (f"，**{len(failed)} 个失败**" if failed else "")
    )
    lines.append("")
    lines.append("## 一、逐条对照（方案书 §8.1–8.7）")
    lines.append("")

    for group in groups:
        got = [s for s in group.steps if s.outcome == "passed"]
        bad = [s for s in group.steps if s.outcome == "failed"]
        mark = "✅" if not bad and got else ("⚠️" if bad else "—")
        when = f"（上次 {origin[group.key]}）" if group.key in origin else ""
        lines.append(f"### §{group.key} {group.title} {mark}{when}")
        lines.append("")
        lines.append("| 标准（方案书原文） | 断言落在哪 | 结果 | 实测 |")
        lines.append("|---|---|---|---|")
        for item in group.spec:
            steps = "、".join(f"`{s.script}`" for s in group.steps) or "（见视觉矩阵）"
            lines.append(f"| {item} | {steps} | {'✅' if not bad else '⚠️'} | 见下列脚本明细 |")
        lines.append("")
        for step in group.steps:
            icon = {"passed": "✅", "failed": "❌", "skipped": "⏭"}.get(step.outcome, "·")
            if group.key in origin and step.outcome:
                icon = {"passed": "✅", "failed": "❌", "skipped": "⏭"}[step.outcome] + "⏱"
            note = f"（{step.note}）" if step.note else ""
            lines.append(
                f"- {icon} `{step.script}` {note} — {step.seconds:.1f}s — `{step.last[:160]}`"
            )
        for extra in group.extra:
            lines.append(f"- • {extra}")
        lines.append("")

    lines.append("## 二、视觉矩阵（两套皮肤 × 全部页面）")
    lines.append("")
    lines.append("| 皮肤 | 页面数 | 期望 | 用时 | 结果 |")
    lines.append("|---|---|---|---|---|")
    for skin, info in visual.items():
        if info.get("skipped"):
            state = "⏭ 未重拍" if info["ok"] else "⏭ 跳过（无样张）"
        else:
            state = "✅" if info["ok"] else "❌"
        lines.append(
            f"| {skin} | {info['files']} | ≥{info['want']} | {info['seconds']}s | {state} |"
        )
    lines.append("")
    lines.append(
        "样张在 `docs/acceptance/matrix/`（`--height 1400`，一屏能看完整个页面）。"
        "脚本只能证明「页面渲染出来了」，好不好看仍需人看一眼——本轮人工看过的样张见 "
        "`docs/acceptance/p1..p5/`。"
    )
    lines.append("")

    lines.append("## 三、阻塞项与绕过方案")
    lines.append("")
    blocking = [
        (
            "ASR 模型需下载 1.6 GB（`large-v3-turbo`）",
            "转录类断言用 `--reuse` 复用已有 `transcript.json`；真要重跑模型时先 `transcribe.model.download`",
        ),
        (
            "B 站真实下载依赖网络与登录态",
            "`--skip-network` 可整组跳过；其余发布/合并/导出断言全部用替身客户端，不需要网络",
        ),
        (
            "真实发布到 B 站（`publish.publish`）未做联网实测",
            "全部走 `post_note/withdraw_note` 替身；真发一次需要账号，属于用户自己决定的操作",
        ),
        (
            "干净 Windows 虚拟机未验",
            "打包产物已验 `exe --check` + 真窗口首屏；缺 WebView2 的路径只有代码与文案保证",
        ),
    ]
    lines.append("| 阻塞项 | 绕过方案 |")
    lines.append("|---|---|")
    for item, workaround in blocking:
        lines.append(f"| {item} | {workaround} |")
    lines.append("")

    if failed:
        lines.append("## 四、失败明细")
        lines.append("")
        for group, step in failed:
            lines.append(f"### §{group.key} `{step.script}`")
            lines.append("")
            out = getattr(step, "_out", "") or ""
            tail = [line for line in out.splitlines() if line.strip()][-14:]
            lines.append("```")
            lines.extend(tail)
            lines.append("```")
            lines.append("")

    RECORD.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    # 子脚本会往 stdout 里写中文；Windows 控制台默认 GBK，遇到替换字符会直接抛
    # UnicodeEncodeError 把整轮矩阵打断——宁可乱码也不要断。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 老 Python / 重定向流
        pass
    parser = argparse.ArgumentParser(description="P5-06 全量验收矩阵")
    parser.add_argument(
        "--skip-network", action="store_true", help="跳过真站下载（无网络/不想下载时）"
    )
    parser.add_argument("--skip-visual", action="store_true", help="不重拍视觉矩阵（用已有样张）")
    parser.add_argument("--only", action="append", default=[], help="只跑这几组，如 --only 8.3")
    parser.add_argument("--height", type=int, default=1400)
    args = parser.parse_args(argv)

    run_groups = [g for g in GROUPS if not args.only or g.key in args.only]
    groups = list(GROUPS)  # 记录要完整：没跑的组用上次结果补
    run_keys = {g.key for g in run_groups}
    state = load_state() if args.only else {}
    origin = hydrate(groups, state, run_keys) if state else {}
    print(
        f"[matrix] 本次 {len(run_groups)} 组，共 {sum(len(g.steps) for g in run_groups)} 个脚本"
        + (f"；另 {len(origin)} 组沿用上次" if origin else "")
    )

    for group in run_groups:
        print(f"\n=== §{group.key} {group.title} ===")
        for step in group.steps:
            if step.network and args.skip_network:
                step.outcome, step.last = "skipped", "按 --skip-network 跳过"
                print(f"[skip] {step.script}")
                continue
            print(f"[run ] {step.script} …", flush=True)
            run_step(step)
            icon = {"passed": "ok  ", "failed": "FAIL", "skipped": "skip"}[step.outcome]
            print(f"[{icon}] {step.script} — {step.seconds:.1f}s — {step.last[:120]}")

    visual: dict[str, dict] = {}
    if args.skip_visual:
        for skin, want in SHOT_SETS.items():
            have = len(list((PROJ / "docs" / "acceptance" / "matrix").glob(f"{skin}-*.png")))
            visual[skin] = {
                "ok": have >= want,
                "files": have,
                "want": want,
                "seconds": 0.0,
                "skipped": True,
                "tail": ["按 --skip-visual 未重拍；上面的张数是已有样张"]
                if have
                else ["按 --skip-visual 跳过，且没有历史样张"],
            }
    elif args.only and state.get("_visual"):
        visual = {
            k: dict(v, skipped=True, tail=["本次分批不重拍，沿用上次样张"])
            for k, v in state["_visual"].items()
        }
        for skin, want in SHOT_SETS.items():
            visual.setdefault(
                skin,
                {
                    "ok": False,
                    "files": 0,
                    "want": want,
                    "seconds": 0.0,
                    "skipped": True,
                    "tail": ["没有历史样张"],
                },
            )
    else:
        print("\n=== 视觉矩阵（两套皮肤 × 全部页面）===")
        visual = run_visual(skins=list(SHOT_SETS), height=args.height)
        for skin, info in visual.items():
            print(
                f"[{'ok  ' if info['ok'] else 'FAIL'}] {skin}: {info['files']}/{info['want']} 张，"
                f"{info['seconds']}s"
            )

    if args.only:
        save_state(state, run_groups, visual)
    write_record(groups, visual, skipped_network=args.skip_network, origin=origin)

    failed = [s for g in run_groups for s in g.steps if s.outcome == "failed"]
    bad_visual = [
        skin for skin, info in visual.items() if not info["ok"] and not info.get("skipped")
    ]
    print(f"\n记录已写：{RECORD.relative_to(PROJ)}")
    if failed or bad_visual:
        print(f"P5-06 验收矩阵：{len(failed)} 个脚本失败 / 视觉异常 {bad_visual}")
        return 1
    print("P5-06 全量验收矩阵：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
