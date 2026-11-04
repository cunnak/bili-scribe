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
* **C4 不写家目录**：跑之前跑之后，真实 `~/.video-workbench` 与 `~/.bili-scribe`
  的每个文件 `(sha256, mtime_ns, size)` 必须一致（lead 的 V6 判据）；配置本身也不许被写。

冲突那条（`info --roles`）钉的是**报错方式**：必须明确说「--roles 就是 roles 子命令」，
不许静默丢掉用户要的那份输出。

命令原文都记在任务板 commandsRun；本文件全部走**子进程**，与打包验收同形。
"""

from __future__ import annotations

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

    打包验收时这是唯一一处能一眼看出装的是哪一版角色的地方 —— 光有 8 行名单，
    看不出默认角色被换过没有。期望值从权威层现读，不写死。
    """
    from video_workbench.core import summary as wb_summary

    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-500:]
    header = [ln for ln in proc.stdout.splitlines() if ln.startswith("角色 ")]
    assert len(header) == 1, f"表头应当只有一行，读到 {header}"
    assert str(len(wb_summary.ROLE_KEYS)) in header[0], header[0]
    assert wb_summary.DEFAULT_ROLE in header[0], header[0]
    for retired in sorted(wb_summary.RETIRED_ROLES):
        assert retired in header[0], f"退役角色 {retired} 没在表头里说出来：{header[0]}"


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


def test_the_command_writes_neither_the_real_homes_nor_a_second_time(
        temp_cfg):
    """C4：跑一次 `--roles` 之后，真实两家必须原封不动；而且**不许每跑一次写一次**。

    这是 lead 的 V6 判据的常驻形态。家目录那一半直接钉死；配置这一半钉的是**幂等**
    （第二次运行的字节必须等于第一次之后的字节），不是「一次都不写」——
    后者今天不成立，单独记在下一条严格 xfail 里，不偷偷混进来当已通过。
    """
    before = _home_tree()
    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-300:]
    after = _home_tree()
    changed = {k: (before[k], after[k]) for k in before if before.get(k) != after.get(k)}
    extra = sorted(set(after) - set(before))
    assert not changed, f"真实家目录被写了：{changed}"
    assert not extra, f"真实家目录多出文件：{extra}"

    once = temp_cfg.read_bytes()
    sidecar_once = (temp_cfg.parent / "workbench.json").read_bytes() \
        if (temp_cfg.parent / "workbench.json").exists() else b""
    proc2 = _cli("--roles", "--config", str(temp_cfg))
    assert proc2.returncode == 0, proc2.stderr[-300:]
    assert temp_cfg.read_bytes() == once, (
        "同一个配置被第二次运行又改了一遍 —— 诊断口变成了改环境的操作")
    sidecar_again = (temp_cfg.parent / "workbench.json").read_bytes() \
        if (temp_cfg.parent / "workbench.json").exists() else b""
    assert sidecar_again == sidecar_once, "sidecar 每次运行都在漂"


@pytest.mark.xfail(
    strict=True,
    reason="产品问题，等 vendor 定：只读诊断口第一次跑就会把用户配置整份重写。"
           "实测 `roles` 与 `info` **都一样**（88 行模板 + sidecar），"
           "写发生在服务装配那一步（`video_workbench/app/service.py` 里的 `self.cfg.save()`），"
           "不在我加的 `cmd_roles` 里 —— 我的写范围只有 `cli.py`，改不到那一层。"
           "vendor 修好这条会 XPASS 变红，那时把它删掉、把上面的幂等腿改回「一次都不写」。")
def test_a_read_only_route_should_not_rewrite_a_users_config_the_first_time(
        temp_cfg):
    """合同写的是「不写任何配置」，盘上现在做不到：首次运行就把最小配置补成整份模板。

    为什么单独立一条而不是并进上一条：这条**今天就是红的**，而红的是既有行为，
    不是我这轮加的路由。把它塞进通过的用例里，就等于用一句注释把违约说成合规。
    现读到的形状：跑前 8 行（只有 llm/storage 两段），跑后 91 行整份模板 + 落
    `workbench.json`；`grep -n "\\.save()" video_workbench/app/service.py` 六处命中，
    服务装配那条路径上就有。
    """
    original = temp_cfg.read_bytes()
    proc = _cli("--roles", "--config", str(temp_cfg))
    assert proc.returncode == 0, proc.stderr[-300:]
    assert temp_cfg.read_bytes() == original, (
        "只读的诊断口重写了用户配置（见上面的 reason，等 vendor）")


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
