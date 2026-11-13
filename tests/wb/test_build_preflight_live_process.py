"""打包前置检查：`dist/` 里还开着的旧实例必须在构建之前被拦住。

钉的是一条**真实踩过**的坑（2026-11-13，发布 0.2.7 时）：`dist/VideoWorkbench` 里躺着一个
跑了一整天的旧实例，`build(clean=True)` 的 `shutil.rmtree(DIST)` 撞上被锁的
`_internal/charset_normalizer/cd.cp314-win_amd64.pyd`，抛的是::

    PermissionError: [WinError 5] 拒绝访问。

那条报错指向 PyInstaller 的 `_make_clean_directory`——**真凶在自己的程序里，报错里一个字
都没有**。于是白等 81 秒才拿到一句读不懂的话。

四条判据，两两成对：

1. **结构锁**：`_running_from_dist` 存在，且 `preflight()` 的源码里真的调它。
   这条走 `WB_BUILD_SCRIPT_OVERRIDE`，所以牙齿能在 `%TEMP%` 副本里验红——
   验红只准换文件，不准改工作区（本仓库既有纪律，见 AGENTS.md 第六节）。
2. **行为锁**：真从 `dist/` 起一个进程 → 必须被点名；关掉 → 必须回到空。
   **两半都断**：只证明"会红"的闸，退化成一个恒真的 `problems.append(...)` 也照样过。

⚠️ 行为锁**故意不认** `WB_BUILD_SCRIPT_OVERRIDE`：脚本的 `PROJ` 由自身 `__file__` 算出，
加载副本会让 `DIST` 指到 `<副本目录>/dist`，那里没有正在跑的 exe —— 实测过，
那会让这条用例恒红在一个假前提上。源码可换、被观测的对象不可换。

跑法：``.venv/Scripts/python.exe -m pytest tests/wb/test_build_preflight_live_process.py -q``
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 真脚本。行为锁只认它。
SCRIPT = ROOT / "scripts" / "build_wb.py"
EXE = ROOT / "dist" / "VideoWorkbench" / "VideoWorkbench.exe"


def _source_path() -> Path:
    """结构锁读哪份源码。默认仓库那份；牙齿用 ``WB_BUILD_SCRIPT_OVERRIDE`` 指到副本。

    这是本仓库既有形状（``WB_PROMPTS_DIR`` / ``WB_WEB_ROOT`` / ``WB_CLI_SOURCE_OVERRIDE``
    同一族）：**验红只准换文件，不准改工作区**。
    """
    return Path(os.environ.get("WB_BUILD_SCRIPT_OVERRIDE") or SCRIPT)


def _load_build_module():
    """把真脚本当模块加载（import 期只定义常量，不构建）。**不认 override**，见文件头。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wb_build_wb_under_test", SCRIPT)
    assert spec and spec.loader, f"加载不了 {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_helper_exists_and_is_wired_into_preflight():
    """结构锁：`_running_from_dist` 存在，且 `preflight` 的源码里真的调它。

    这条拦的是"有人重构 preflight 时把那段顺手删了"——行为锁只在 exe 存在时能跑，
    而新克隆的机器上 `dist/` 常常是空的，光靠行为锁会静默失去覆盖。
    """
    source = _source_path()
    assert source.is_file(), f"被检源码不在了：{source}"
    tree = ast.parse(source.read_bytes().decode("utf-8"))
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "_running_from_dist" in names, (
        "scripts/build_wb.py 里没有 _running_from_dist —— 构建前置检查少了「旧实例还开着」这一条"
    )
    preflight = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "preflight"
    )
    called = {
        node.func.id
        for node in ast.walk(preflight)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_running_from_dist" in called, (
        "preflight() 没有调用 _running_from_dist() —— 函数还在但没接上，等于没有这条检查"
    )


def test_the_override_switches_the_file_not_the_verdict():
    """开关只换文件、不换判据：拿一份**完好副本**过 override，结构锁必须照样绿。

    没有这条，`WB_BUILD_SCRIPT_OVERRIDE` 可能被写成"只要设了就直接跳过"，
    那时牙齿全绿而判据已经没了。
    """
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="wb-preflight-override-") as tmp:
        copy = Path(tmp) / "build_wb.py"
        shutil.copyfile(SCRIPT, copy)
        assert copy.read_bytes() == SCRIPT.read_bytes(), "副本必须与原文件逐字节一致"
        os.environ["WB_BUILD_SCRIPT_OVERRIDE"] = str(copy)
        try:
            assert _source_path() == copy, "开关没生效"
            test_the_helper_exists_and_is_wired_into_preflight()
        finally:
            os.environ.pop("WB_BUILD_SCRIPT_OVERRIDE", None)


def test_a_process_outside_dist_is_not_reported():
    """不误报：pytest 自己（跑在仓库里，不在 dist 下）不许出现在结果里。"""
    module = _load_build_module()
    found = module._running_from_dist()
    assert all(str(ROOT / "dist").lower() in image.lower() for _pid, image in found), (
        f"报了 dist 之外的进程：{found}"
    )
    assert all(pid != 0 for pid, _image in found), "PID 0 是 Idle 进程，不该被算进来"


@pytest.mark.skipif(sys.platform != "win32", reason="文件锁行为是 Windows 专有")
@pytest.mark.skipif(
    not EXE.is_file(),
    reason="没构建过打包版（dist/VideoWorkbench/VideoWorkbench.exe 不存在）",
)
def test_a_live_instance_is_caught_and_released():
    """行为锁：真起一个进程 → 必须被点名；关掉 → 必须回到空。

    两半都断，缺一半这条闸就退化成恒真或恒假。
    """
    module = _load_build_module()
    before = module._running_from_dist()
    assert before == [], f"开工前 dist 里就已经有进程在跑：{before}；这条用例要独占，先关掉再跑"

    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent))
    try:
        deadline = time.time() + 30
        found: list[tuple[int, str]] = []
        while time.time() < deadline:
            found = module._running_from_dist()
            if any(pid == proc.pid for pid, _image in found):
                break
            time.sleep(0.5)
        assert any(pid == proc.pid for pid, _image in found), (
            f"起了 PID {proc.pid} 却看不见它；实读 {found}"
        )
        image = next(image for pid, image in found if pid == proc.pid)
        assert image.lower().startswith(str(module.DIST).lower()), (
            f"报出来的路径不是 dist 下的：{image}"
        )

        problems = module.preflight()
        assert any(str(proc.pid) in item for item in problems), (
            f"preflight 没有点名 PID {proc.pid}；实读 {problems}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)

    deadline = time.time() + 30
    while time.time() < deadline:
        if module._running_from_dist() == []:
            break
        time.sleep(0.5)
    assert module._running_from_dist() == [], "进程关掉之后仍然被报出来 —— 这条闸会永远红，等于没有"
    assert not any("打包产物还开着" in item for item in module.preflight()), (
        "进程关掉之后 preflight 仍在报「打包产物还开着」"
    )
