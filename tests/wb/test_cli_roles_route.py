"""CLI 的 `--roles` 路由：打包验收用的离线自证口（task-30 第 3 项）。

为什么要有这个口：打包版（`dist/VideoWorkbench`）里的 6 篇 txt 是不是**真进了包**、
装完之后 `prompt_source.bind()` 是不是**真读到了**，在没有浏览器的机器上本来只有一种
看法 —— 打开设置页看那行 `key ← 文件名 · N B · sha8`。exe 跑不了 Playwright，
所以 `video_workbench/cli.py` 加了 `roles` 子命令 / `--roles` 旗标（lead 在 m04018 扩的写范围）。

钉四件事，都是「打包后才会暴露」的形状：

* **C1 三种写法必须同结果**（`--roles` / `roles` / `--config X --roles`）。
  我第一版把命令名插到最前，`--roles --config X` 直接
  `error: unrecognized arguments: --config …` —— 旗标在打包验收里是唯一入口，
  少一种写法就少一种失败姿势。
* **C2 载荷与设置页同源**：`--json` 出来的东西必须逐字段等于 `svc.dispatch("summary.roles")`
  的 `data`。两边读的是同一个 owner，不许 CLI 自己拼一份「看起来一样」的名单。
* **C3 不泄密**：Temp 配置里故意写一把哨兵 key，输出里必须一个字符都见不到。
* **C4 不写家目录、也不写配置**：跑之前跑之后，真实 `~/.video-workbench` 与 `~/.bili-scribe`
  的每个文件 `(sha256, mtime_ns, size)` 必须一致（lead 的 V6 判据）；Temp 那份配置本身
  也不许被写 —— task-34 之后这条从「幂等」抬回「一次都不写」。
* **C5 连副作用都没有**：`roles` / `--roles` / `--roles --json` 三种写法都不许建库、
  不许建 `logs/`、不许落 sidecar。**范围只到这两个入口**：`info` 与 `--check` 的语义就是
  体检「写得进去」，仍会建库建 logs，那是 `AGENTS.md` 第四节记在案的有意边界，不是缺陷。

冲突那条（`info --roles`）钉的是**报错方式**：必须明确说「--roles 就是 roles 子命令」，
不许静默丢掉用户要的那份输出。

命令原文都记在任务板 commandsRun；本文件全部走**子进程**，与打包验收同形。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "sk-PROBE-ONLY-不许多一个字符"
#: 哨兵 key 只进 Temp 配置；真实家目录一个字节都不碰。
NOISE_KEYS = (SENTINEL,)


def _home_tree() -> dict:
    """真实两个家目录的 `(路径, sha256, mtime_ns, size)` 快照。

    import 期不做这件事 —— 这条要在用例执行的那一刻取，跑前跑后各一次才比得出东西。
    """
    out = {}
    for home in (Path.home() / ".video-workbench", Path.home() / ".bili-scribe"):
        if not home.exists():
            continue
        for f in sorted(home.glob("*")):
            if f.is_file():
                b = f.read_bytes()
                st = f.stat()
                out[str(f)] = (hashlib.sha256(b).hexdigest(), st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture
def temp_cfg(tmp_path):
    """一份只属于本次调用的 Temp 配置：库、密钥、日志、缓存全在 tmp 里。

    注意 `storage.db_path` **不跟配置家走**（两程序共库），必须显式指到 tmp，
    否则所谓沙盒会去读真实的 `~/.bili-scribe/bili-scribe.db`。
    """
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    text = (
        "llm:\n"
        f"  api_key: {json.dumps(SENTINEL)}\n"
        "  model: \"m\"\n"
        "  base_url: \"http://127.0.0.1:9/v1\"\n"
        "  role: \"wengao\"\n"
        "storage:\n"
        f"  db_path: {json.dumps((cfg_dir / 'cli.db').as_posix())}\n"
        f"  key_path: {json.dumps((cfg_dir / 'key.bin').as_posix())}\n"
        f"  cache_root: {json.dumps((tmp_path / 'cache').as_posix())}\n"
    )
    path = cfg_dir / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _cli(*argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "video_workbench.cli", *argv],
        cwd=str(ROOT), env=env, capture_output=True, encoding="utf-8", errors="replace",
        timeout=180)


def _roles_lines(stdout: str) -> list[str]:
    """从人读输出里抠角色行（两格缩进 + `｜` 分隔），顺带把表头那行留着自证。"""
    return [ln for ln in stdout.splitlines() if ln.startswith("  ") and "｜" in ln]


def test_the_three_spellings_all_reach_the_same_role_list(temp_cfg):
    """C1：`--roles`、`roles`、`--config X --roles` 必须给出**逐行相同**的输出。

    只钉一种写法是不够的：lead 验收时最自然的写法是 `exe --roles --config X`，
    而打包脚本可能写 `exe roles --config X` —— 两种都得通。
    """
    p = str(temp_cfg)
    runs = [
        _cli("--roles", "--config", p),
        _cli("roles", "--config", p),
        _cli("--config", p, "--roles"),
    ]
    for proc in runs:
        assert proc.returncode == 0, (
            f"写法没过：{proc.returncode}\nstdout={proc.stdout[-500:]}\n"
            f"stderr={proc.stderr[-500:]}")
    lines = [_roles_lines(r.stdout) for r in runs]
    assert lines[0], "一条角色行都没打出来，下面的比较会空转"
    assert lines[0] == lines[1] == lines[2], (
        "三种写法输出不同：\n" + "\n---\n".join(str(x) for x in lines))
    assert len(lines[0]) >= 6, f"角色行太少（{len(lines[0])}），不像完整名单"


def test_the_roles_header_names_the_default_and_the_retired_ones(temp_cfg):
    """表头那行必须把「默认是谁、退役了谁」说出来。

    打包验收时这是唯一一处能一眼看出装的是哪一版角色的地方 —— 光有 6 行名单，
    看不出默认角色被换过没有。期望值从权威层现读，不写死。

    **退役那三个用的是人话名，不是裸 key**（2026-11-04 vendor 把表头改成人读；
    我第一版按裸 key 断，红在这里）。改断 `role_label()` 的产物并不等于放宽：这条要守的
    行为是「退役集合被完整念出来」，键序、数量、缺一个都照样红；而「载荷里到底该带 key
    还是带 display」是 RPC/CLI 载荷的判据，归 task-36/task-38，不在表头这行上混着钉。
    """
    from video_workbench.core import summary as wb_summary

    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-500:]
    header = [ln for ln in proc.stdout.splitlines() if ln.startswith("角色 ")]
    assert len(header) == 1, f"表头应当只有一行，读到 {header}"
    assert str(len(wb_summary.ROLE_KEYS)) in header[0], header[0]
    assert wb_summary.DEFAULT_ROLE in header[0], header[0]
    for retired in sorted(wb_summary.RETIRED_ROLES):
        label = wb_summary.role_label(retired)
        assert label != retired, (
            f"{retired} 的停用名回落到裸 key 了：{label}（表头就再也读不出「谁退役了」）")
        assert label in header[0], (
            f"退役角色 {retired} 没在表头里被念出来（现读它的停用名是「{label}」）：{header[0]}")


def test_the_json_output_is_the_same_payload_the_page_reads(temp_cfg):
    """C2：`--json` 的载荷必须逐字段等于设置页读的那份 `summary.roles`。

    判据是「同一个 owner 的两张脸」，不是「两边看起来都是 8 个角色」——
    后者在 CLI 自己拼一份名单时照样绿。
    """
    from video_workbench.app.service import WorkbenchService

    proc = _cli("--roles", "--json", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-500:]
    payload = json.loads(proc.stdout)
    # 外层信封的键是 `cmd` 不是 `method`（我上一轮按 `method` 猜了一次，红在那儿）。
    # `dispatch` 的回包才有 `request_id` 那一套，CLI 的 `emit()` 是自己的一层，
    # 这里对准 emit 的真实形状，别把两边的字段名混起来。
    assert payload.get("cmd") == "roles", payload
    assert payload.get("ok") is True, payload
    data = payload["data"]

    svc = WorkbenchService(config_path=str(temp_cfg))
    try:
        expected = svc.dispatch("summary.roles", {}, "cli-compare")["data"]
    finally:
        svc.close()
    assert data == expected, (
        "CLI 与设置页读到的角色载荷不同 —— 第二个 owner 就是这么长出来的")
    # 载荷里必须有默认角色，且**不许有退役角色**。原来这条写死 `>= {"wengao","shipin"}`：
    # `shipin` 已按用户决定退役（合并进 `renhua`），写死的名字就成了过期合同值。
    # 换成现读的两半，方向仍然比原来硬：默认角色缺席要说，退役角色混进清单更要说——
    # 后者是用户选了就拿不到总结的那类事故。
    from video_workbench.core import summarizer as ad_summary

    listed = {r["key"] for r in data["roles"]}
    assert ad_summary.DEFAULT_ROLE in listed, (
        f"默认角色 {ad_summary.DEFAULT_ROLE!r} 不在 --roles 的清单里：用户按默认值找它，找不到")
    assert not (listed & set(ad_summary.RETIRED_ROLES)), (
        f"退役角色混进 --roles 清单：{sorted(listed & set(ad_summary.RETIRED_ROLES))}")


def test_a_retired_source_line_says_what_it_actually_uses(temp_cfg):
    """无 txt 真源的角色必须写明「内置压缩版」，不许留空让用户以为文件在。

    这一条是 C1 那套输出的**语义**腿：行数一样不代表话说对了。
    现读 `prompt_source.NO_SOURCE` 决定该看哪几行，将来某角色有了真源，
    它就从这条里退出 —— 不是我把名单写死在断言里。
    """
    from video_workbench.core import prompt_source as ps

    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-500:]
    lines = {ln.strip().split("｜")[0]: ln for ln in _roles_lines(proc.stdout)}
    assert set(lines) >= set(ps.NO_SOURCE), f"名单里没有该出现的无源角色：{sorted(lines)}"
    for key in ps.NO_SOURCE:
        if key not in lines:      # 角色可能被合并/退役（task-31 正在动这块），只查在的
            continue
        assert "内置压缩版" in lines[key], lines[key]
    for key, fname in ps.MAP.items():
        if key not in lines:
            continue
        assert fname in lines[key], f"{key} 那行没提它的真源文件 {fname}：{lines[key]}"
        sha8 = hashlib.sha256((ps.PROMPT_DIR / fname).read_bytes()).hexdigest()[:8]
        assert sha8 in lines[key], (
            f"{key} 那行没打 sha8（{sha8}）—— 用户没法确认生效的是不是这一份：{lines[key]}")


def test_the_key_never_leaves_the_config_file(temp_cfg):
    """C3：哨兵 api_key 不许出现在 stdout/stderr 的任何一处。

    故意用一把长得像真密钥、内容唯一的哨兵。**只查 stdout 不够**：
    `--quiet` 之外 CLI 的日志走 stderr，那里才是最容易顺手把配置打出来的地方。
    """
    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-300:]
    for label, text in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        for needle in NOISE_KEYS:
            assert needle not in text, f"{label} 里漏了 {needle}"
    assert "api_key" not in proc.stdout, "stdout 里出现了 api_key 字样（哪怕没带值）"


def test_the_command_writes_neither_the_real_homes_nor_the_config(
        temp_cfg):
    """C4：`--roles` 跑完之后，真实两家原封不动，Temp 配置**一个字节都没被写**。

    这是 lead 的 V6 判据的常驻形态。家目录那一半直接钉死；配置这一半以前钉的是
    「幂等」（第二次运行等于第一次之后的状态），因为首次运行就会把最小配置补成 91 行
    整份模板 —— 那是当时的既有缺陷，我用严格 xfail 单独记着、没有混进通过项。task-34
    把只读路由改成不装配服务之后（`app/service.py:650 roles_payload`），这里改回合同
    本意：**一次都不写**。幂等仍然顺带验（第二次也不许漂）。副作用面（不建库、不建
    logs、不落 sidecar）由下一条单独钉，别把两件事挤进一条断言里。
    """
    before = _home_tree()
    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-300:]
    after = _home_tree()
    changed = {k: (before[k], after[k]) for k in before if before.get(k) != after.get(k)}
    extra = sorted(set(after) - set(before))
    assert not changed, f"真实家目录被写了：{changed}"
    assert not extra, f"真实家目录多出文件：{extra}"

    original = temp_cfg.read_bytes()
    assert temp_cfg.read_bytes() == original, (
        "只读诊断口重写了用户配置（首次运行就落盘）")
    proc2 = _cli("--roles", "--config", str(temp_cfg))
    assert proc2.returncode == 0, proc2.stderr[-300:]
    assert temp_cfg.read_bytes() == original, (
        "同一个配置被第二次运行又改了一遍 —— 诊断口变成了改环境的操作")


def test_a_read_only_route_leaves_no_database_logs_or_sidecar(temp_cfg):
    """合同「不写任何配置」的完整版：**只针对 `roles` / `--roles` 这两个入口**。

    判据不是「配置字节不变」这一条 —— 一次「只是看看角色表」的运行如果在用户目录里
    留下 `cli.db`、`logs/`、`workbench.json`，用户看到的仍是「跑一下诊断口，环境被改了」。
    所以这里钉四件事：配置不变、不落 sidecar、不建库、不建 logs 目录。三种写法都验
    （`--roles`、`roles`、`--roles --json`），因为打包验收会用哪一种不该决定副作用。

    **`info` 与 `--check` 不在本条范围内**：它们的语义就是体检「写得进去」，现在仍会
    建库建 logs —— lead 已把这条边界写进 `AGENTS.md` 第四节与 release notes 的已知边界，
    别在这里顺手把它们钉成不建库（那是另一笔产品改动）。
    """
    original = temp_cfg.read_bytes()
    cfg_dir = temp_cfg.parent
    tmp_root = cfg_dir.parent
    for argv in (("--roles",), ("roles",), ("--roles", "--json")):
        proc = _cli(*argv, "--config", str(temp_cfg))
        assert proc.returncode == 0, f"{argv}：{proc.stderr[-300:]}"
    assert temp_cfg.read_bytes() == original, "配置被写过（见上一条的口径）"
    sidecar = cfg_dir / "workbench.json"
    assert not sidecar.exists(), (
        f"sidecar 落盘了：{sidecar}（配置里只有一个 yaml 应当是唯一事实文件）")
    db = cfg_dir / "cli.db"
    assert not db.exists(), f"只读路由建了库：{db}"
    logs = tmp_root / "logs"
    assert not logs.exists(), (
        f"只读路由建了日志目录：{logs}（cache_root 同级的 logs 是装配副作用）")


def test_a_writing_route_actually_lands_on_the_paths_c5_checks(temp_cfg):
    """上面那条 C5 的**正向对照**：`info` 确实会在 `cli.db` / `logs` 落东西。

    为什么必须有这条：C5 断的全是 `not exists`。路径一旦写错（比如日志其实落在别处），
    那四条就永远为真 —— 这正是本仓库反复在清的「自己考自己」。所以用一条**会写**的路由
    把同样的路径钉活：它建出来的东西必须落在 C5 检查的那几个位置上，C5 的绿才说明问题。

    这条不是要求 `info` 永远建库：`--check` 是 `desktop.py` 的旗标（CLI 里没有），而
    `info` 建库是 `AGENTS.md` 第四节记在案的有意边界 —— 体检语义本身就包含「写得进去」。
    万一将来它不写了，本条会红着说「对照失效」，提醒 C5 要重新设计，
    而不是安静地变成永真。
    """
    cfg_dir = temp_cfg.parent
    tmp_root = cfg_dir.parent
    proc = _cli("info", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-400:]
    built = sorted(str(p.relative_to(tmp_root).as_posix()) for p in tmp_root.rglob("*")
                   if p.is_file())
    db = cfg_dir / "cli.db"
    assert db.is_file(), (
        f"C5 检查的库路径 {db.as_posix()} 连 info 都不建 ⇒ 路径写错了、C5 的 "
        f"not-exists 是空的。本次实际建出：{built}")
    logs = tmp_root / "logs"
    assert logs.is_dir(), (
        f"C5 检查的日志目录 {logs.as_posix()} 连 info 都不建 ⇒ 同上。"
        f"本次实际建出：{built}")


def test_conflicting_spelling_errors_instead_of_silently_losing_output(temp_cfg):
    """`info --roles` 必须明确报错，不许静默按 info 跑（用户就少了 8 行东西）。

    报错文案钉两个词：得说出 `--roles`，得说出它就是 `roles` 子命令 ——
    只写「参数错误」的话，下一个人还是会在同一个地方卡住。
    """
    proc = _cli("info", "--roles", "--config", str(temp_cfg))
    assert proc.returncode != 0, (
        f"冲突写法居然成功了，用户拿到的其实是 info：\n{proc.stdout[:400]}")
    blob = proc.stdout + proc.stderr
    assert "--roles" in blob, blob[-400:]
    assert "roles" in blob and ("子命令" in blob or "别" in blob), blob[-400:]


    # ======================================================================= D 组
    # task-38：退役角色的**显示名**只有一个 owner（权威层 role_label），CLI 是消费者。
    #
    # 来历：`roles` 曾把三个已停用角色印成英文裸 key（`shipin、standard、xueshu`），
    # 同一件事在界面上打的却是整句人话（「视频总结（已停用）」…）——一个事实两个 owner，
    # 而 key 对用户没有信息量（那三个词从来没在界面上出现过）。task-36 之后载荷带
    # `retired_labels`（`{key, display}`），`cli.py:246-258` 消费 display、缺字段才回落 key。
    # 本组钉「改完之后不许再退化」，四条按 lead 21:3x 定稿 + 21:5x 补的形状落：
    #   1. stdout 的退役段 == 权威层 `role_label()` 的**现读序列**（整句、顺序、连接符一起钉）；
    #   2. 同进程取载荷：`retired_labels` 的 key 序列 == `retired` 序列（**list == list**，
    #      写成集合相等就白钉：两边现在都是 `sorted(RETIRED_ROLES)`，同序属**巧合级成立**，
    #      谁把一边改成权威层顺序就没人拦）；`display` 与 `role_label()` 逐字相等；
    #   3. **兜底支必须有夹具能打到**（缺 display ⇒ 印裸 key、不抛）；
    #   4. **行为锁**：换掉权威层 `role_label`，CLI 那句与两条 notice 必须跟着变。
    #      不用「grep cli.py 无 `已停用` 字面量」当判据 —— 它连注释一起命中，把「注释里
    #      解释为什么没有表」这种正当写法判成违规（vendor 第一版就栽在这上面）。
    #
    # ⚠️ 判据 2/3/4 必须**同进程**：取名发生在 CLI 模块内部，subprocess 起的是另一个解释器，
    # patch 传不进去（vendor 第一版 patch 了别的层 + subprocess，恒走正常路径＝假绿）。
    # 同进程只借 `Cli.cmd_roles()` 这一段：它从 `WorkbenchConfig.load(temp_cfg)` 开始、
    # 经 `roles_payload(cfg)` 取同一份载荷、不调 `open()`，所以不建服务、不建库；配置被
    # `temp_cfg` 整个关在 Temp 里（`storage.db_path` 不跟配置家走，夹具已显式指 tmp）。


def _retired_clause(stdout: str) -> str:
    """从表头那行抠出「已退役：」后面那一串（不含右括号）。

    抠不到就失败并贴出原输出 —— **这一步不许兜底**，否则判据 1 被自己糊掉。
    """
    head = next((ln for ln in stdout.splitlines() if "已退役：" in ln), "")
    assert head, f"表头里没有「已退役：」这一节：\n{stdout[:400]}"
    tail = head.split("已退役：", 1)[1].strip()
    # 只剥**一个**右括号（表头自己那对）。`rstrip("）")` 是错的：退役名以
    # 「…（已停用）」结尾，连着两个右括号会被一起剥掉，把每条断言都推向假红。
    return tail[:-1] if tail.endswith("）") else tail


def _authority_retired_sequence() -> list[tuple[str, str]]:
    """唯一 owner 的现读序列：`(key, role_label(key))`，顺序 == `sorted(RETIRED_ROLES)`。

    故意**不写死三个键**（lead 判据 2 明说）：以后退役到四个键，这里自动跟着长，
    而断言仍然要求 CLI 与载荷一一对应。
    """
    from video_workbench.core import summary as auth

    return [(key, auth.role_label(key)) for key in sorted(auth.RETIRED_ROLES)]


def _run_roles_in_process(temp_cfg, capsys, *, mutate_payload=None,
                          label_override=None):
    """跑一次真 `cmd_roles()`，返回 `(rc, stdout, 补丁生效时取到的载荷)`。

    `mutate_payload` 拿真载荷改形状（判据 3 剥 display 用）；`label_override` 直接
    patch **权威层模块属性**（判据 4 的行为锁）——不是 patch 适配层里的 import 别名，
    别名在 import 期就绑死了，换它什么也不会变（这正是本组要拦的那类退化）。
    """
    from video_workbench import cli as cli_mod
    from video_workbench.core import summary as auth

    real_payload = cli_mod.roles_payload      # 装补丁之前先抓真函数
    saved_label = auth.role_label
    if label_override is not None:
        auth.role_label = label_override
    try:
        # 顺序两件事都不可换：取名补丁要先生效（`display` 在载荷里现取），
        # 载荷要用真函数算（拿自己塞回去的 lambda 算，就得到一个空字典）。
        shown = dict(real_payload(cli_mod.WorkbenchConfig.load(str(temp_cfg))))
        if mutate_payload is not None:
            mutate_payload(shown)
        cli_mod.roles_payload = lambda cfg: shown
        argv = argparse.Namespace(config=str(temp_cfg), json=False, quiet=True,
                                  timeout=0, cmd="roles")
        rc = cli_mod.Cli(argv).cmd_roles()
    finally:
        cli_mod.roles_payload = real_payload
        auth.role_label = saved_label
    return rc, capsys.readouterr().out, shown


def test_the_roles_header_prints_the_retired_names_in_the_authority_order(temp_cfg):
    """判据 1：stdout 的退役段**逐序列**等于权威层 `role_label()` 现读结果。

    钉三件事，一件都不能少：条目**内容**（整句中文停用名）、条目**顺序**（与
    `sorted(RETIRED_ROLES)` 一致）、**连接符**（`、`）。比 `test_the_roles_header_names_…`
    那条更严：那条只断「每个名字都出现了」，顺序被打乱或混进第四个词它照样绿。
    另外单独钉「不许出现『未知角色』」：退役键被当成未知，就是 task-31 收口前那个症状。
    """
    proc = _cli("roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-300:]
    clause = _retired_clause(proc.stdout)
    seq = _authority_retired_sequence()
    assert seq, "权威层现读退役序列是空的 —— 这条会永远为真，先查 owner"
    assert clause == "、".join(display for _key, display in seq), (
        f"CLI 的退役段不是权威层序列：\n  CLI : {clause!r}\n  权威: "
        f"{'、'.join(d for _k, d in seq)!r}\n完整输出：\n{proc.stdout[:400]}")
    assert "未知角色" not in proc.stdout, proc.stdout[:400]
    # shipin 不在逐字节副本的角色表里（它是 WB 自造又退役的键），显示名取自文件名登记；
    # 它必须是「视频总结（已停用）」而不是裸 key —— 用户认识的只有这几个字。
    assert "视频总结（已停用）" in clause, clause
    for key, _display in seq:
        assert key not in clause, f"退役段里还混着裸 key {key!r}：{clause!r}"


def test_the_retired_labels_pair_the_key_list_in_order(temp_cfg):
    """判据 2 + 判据 5 前半：`retired` 与 `retired_labels` **同序同源**（list == list）。

    写成 `set(...) == set(...)` 就白钉了。两个字段现在各自 `sorted(RETIRED_ROLES)`，
    所以同序属**巧合级成立**：谁把 `retired` 换成权威层自己的顺序、`retired_labels`
    仍 `sorted()`，前端 `indexOf` 按 key 查不受影响，只有把两列当同一序列 zip 的消费方
    会错 —— 拦的就是那一步。

    **同进程**取载荷，理由见本节头：subprocess 咬不住运行时改顺序的牙齿。
    `--json` 的封皮与「CLI == 设置页载荷」由 C2 那条继续钉，两件事不重叠。
    """
    from video_workbench.app.service import roles_payload
    from video_workbench.config import WorkbenchConfig

    data = roles_payload(WorkbenchConfig.load(str(temp_cfg)))
    seq = _authority_retired_sequence()
    keys = [k for k, _d in seq]
    assert data["retired"] == keys, (
        f"`retired` 不是 sorted(RETIRED_ROLES)：{data['retired']!r} vs {keys!r}")
    rows = data["retired_labels"]
    assert [r["key"] for r in rows] == keys, (
        f"两列序列不相等（顺序也是契约）：{[r['key'] for r in rows]!r} vs {keys!r}")
    for row, (key, display) in zip(rows, seq):
        assert row["display"] == display, (
            f"{key} 的 display 与权威层不一致：{row['display']!r} vs {display!r}")
        assert row["display"] != key, (
            f"{key} 的 display 退化成裸 key，判据 1 会跟着空转：{row!r}")


def test_a_retired_row_without_display_prints_bare_keys_and_still_exits_zero(
        temp_cfg, capsys):
    """判据 3：退役项**缺 `display`** 时 CLI 退回裸 key、不抛 KeyError。

    这条以前只是代码里一个 `or key`，没有任何夹具打得到＝死代码；一旦有人把兜底写成
    「拼个（已停用）后缀」或干脆抛错，界面与脚本两条消费路都会当场炸。故意**同进程**跑，
    理由见本节头。断言里同时钉「不许出现停用名」：兜底只能少信息，不能造假信息。
    """
    def strip_display(payload):
        rows = payload.get("retired_labels")
        assert rows, "载荷里没有 retired_labels，判据 3 会空转"
        payload["retired_labels"] = [{"key": r["key"]} for r in rows]

    rc, out, _payload = _run_roles_in_process(temp_cfg, capsys,
                                              mutate_payload=strip_display)
    keys = [k for k, _d in _authority_retired_sequence()]
    clause = _retired_clause(out)
    assert rc == 0, f"缺 display 居然失败了（rc={rc}）：\n{out[:400]}"
    assert clause == "、".join(keys), (
        f"兜底应当逐字退回裸 key，实得 {clause!r}，期望 {'、'.join(keys)!r}")
    assert "已停用" not in clause, clause


def test_the_cli_retired_names_are_taken_from_role_label_not_a_local_table(temp_cfg,
                                                                          capsys):
    """判据 4（第一半）：换掉权威层 `role_label`，CLI 那句**必须跟着变**。

    这是「名字现取、不是本地表」唯一的机械证据：值相等（判据 2 那种比法）把取法退回
    import 别名照样绿，而别名在 import 期就绑死了，以后改权威层根本传不进来。
    落点在 `core/summarizer.py:1143`（载荷里的 `display`）。
    """
    sentinel = lambda key: "SENTINEL·" + str(key)  # noqa: E731
    rc, out, _payload = _run_roles_in_process(temp_cfg, capsys,
                                              label_override=sentinel)
    keys = [k for k, _d in _authority_retired_sequence()]
    assert rc == 0, out[:400]
    assert _retired_clause(out) == "、".join(sentinel(k) for k in keys), (
        f"改了权威层 CLI 却不跟着变 ⇒ 它自己存了一份表：{_retired_clause(out)!r}")


def test_the_two_role_notices_are_taken_from_the_same_authority_label():
    """判据 4（第二半）：`resolve_role` 的回落 notice 与设置页那句也必须是现取。

    同一规则的另外两个现取点：`core/summarizer.py:145`（配置/历史行里的退役值 →
    回落默认角色时那句「本次按默认角色「…」」）与 `:169`（`configured_role_issue`
    给设置页的「生成时会自动按「…」走」）。lead 点名这两处先前**没人钉**，和 `:1143`
    是同一条规则。两句断言都必须落在**补丁还活着的窗口里** —— 我上一版在 helper 跑完
    之后才断 notice（它的 `finally` 已把 `role_label` 还原回去），等于在补丁外测补丁，
    必然假红；拆成两条才是它本来的形状。

    A 支（用户显式提交退役值）抛的是权威层原话、**不含默认角色名**，所以不受本补丁
    影响 —— 那条由 `tests/wb/test_summary_role_resolution.py` 单独钉，别在这儿混。
    """
    import video_workbench.core.summarizer as ad
    from video_workbench.core import summary as auth
    from video_workbench.core.summary import DEFAULT_ROLE

    sentinel = lambda key: "SENTINEL·" + str(key)  # noqa: E731
    retired_key = sorted(auth.RETIRED_ROLES)[0]
    saved = auth.role_label
    auth.role_label = sentinel
    try:
        _key, notice = ad.resolve_role(None, fallback=retired_key)
        assert sentinel(DEFAULT_ROLE) in notice, (
            f"回落 notice 没走权威层取名（:145）：{notice!r}")
        dirty, message = ad.configured_role_issue(retired_key)
        assert dirty == retired_key, (
            f"退役值没被认定成脏配置，第二句根本走不到：{(dirty, message)!r}")
        assert sentinel(DEFAULT_ROLE) in message, (
            f"设置页那句没走权威层取名（:169）：{message!r}")
    finally:
        auth.role_label = saved
    # 控制腿：还原之后必须回到真名 —— 否则上面那两句可能一直为真（自己考自己）。
    _key2, notice2 = ad.resolve_role(None, fallback=retired_key)
    assert "SENTINEL" not in notice2, f"补丁没还原干净：{notice2!r}"
    assert auth.role_label(DEFAULT_ROLE) in notice2, (
        f"还原后回落 notice 里没有真名，这条锁本来就没内容：{notice2!r}")
