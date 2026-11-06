"""合集总览那条路曾经不传 cfg ⇒ 补救预算与配置无关（task-59）。

这条锁比「测总览那一处」值钱的点在于：它扫的是**产品码里每一个 ``SummaryLLM(` 构造点**，
所以将来任何人新增一条走补救链的路、忘了传 cfg，当场就红，不用等某个用户的
``llm.max_tokens`` 悄悄不起作用。

判据用 AST，不用 grep：``cfg=cfg`` 这种写法在源码文本里出现一次和出现三次长得一样，
而构造点是不是真的带上了这个关键字，只有语法树说得上话。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 扫描根。默认是仓库里的 ``video_workbench`` 包；牙齿把它指向 ``%TEMP%`` 的副本树
#: （**禁止对仓库文件按快照覆盖回写**，AGENTS.md 第六节），换的只是文件、不是判据。
DEFAULT_SCAN_ROOT = ROOT / "video_workbench"
SCAN_ROOT_ENV = "WB_SUMMARY_LLM_SCAN_ROOT"


def _scan_root() -> Path:
    raw = os.environ.get(SCAN_ROOT_ENV, "").strip()
    root = Path(raw) if raw else DEFAULT_SCAN_ROOT
    assert root.is_dir(), (
        f"{SCAN_ROOT_ENV}={raw!r} 指向的目录不存在：{root}。"
        "判据不许在扫不到东西时静默通过——扫不到就等于没锁。"
    )
    return root


def _construction_sites(root: Path) -> list[tuple[str, int, bool]]:
    """返回产品码里每个 ``SummaryLLM(...)`` 构造点的 (文件, 行号, 是否传了 cfg)。"""
    found: list[tuple[str, int, bool]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # 解析不了就是坏了，不许跳过
            raise AssertionError(f"{path} 解析失败：{exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "SummaryLLM":
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            found.append((str(path.relative_to(root)), node.lineno, "cfg" in kwargs))
    return found


def test_the_product_code_has_summary_llm_construction_sites_at_all():
    """控制判据：扫描真的命中了构造点。0 个的话后面几条都是空转。"""
    sites = _construction_sites(_scan_root())
    assert sites, (
        f"在 {_scan_root()} 里一个 ``SummaryLLM(`` 构造点都没扫到。"
        "要么扫描根错了，要么构造写法变了——两种情况这条锁都失效，必须红。"
    )


def test_every_summary_llm_construction_in_product_code_passes_cfg():
    """产品码里每个构造点都必须传 cfg：不传 ⇒ 坏 JSON 的补救预算恒等于写死的下限。"""
    missing = [
        f"{file}:{line}" for file, line, has_cfg in _construction_sites(_scan_root()) if not has_cfg
    ]
    assert not missing, (
        "这些 ``SummaryLLM(`` 构造点没有传 cfg，走的是「补救预算与配置无关」的老行为："
        + "、".join(missing)
        + "。要修的是把调用方手里的配置传进去（``SummaryLLM(..., cfg=cfg)``），"
        "不是把这条判据放宽。"
    )


def test_the_collection_overview_path_is_one_of_the_covered_sites():
    """task-59 的具体病灶：``core/overview.py`` 里那个构造点必须在被扫到的清单里、
    且已传 cfg。点名它是因为这条路是**唯一一个曾经漏掉的**——将来谁把它挪走或改名，
    这条判据会先响，而不是静默少锁一条路径。"""
    sites = {file: (line, has_cfg) for file, line, has_cfg in _construction_sites(_scan_root())}
    overview = next(
        (
            value
            for file, value in sites.items()
            if file.endswith(os.path.join("core", "overview.py"))
        ),
        None,
    )
    assert overview is not None, (
        f"扫描结果里没有 ``core{os.sep}overview.py`` 的构造点（现有：{sorted(sites)}）。"
        "要么文件被移走，要么构造写法变了——task-59 那条路就不再被锁住了。"
    )
    line, has_cfg = overview
    assert has_cfg, f"core/overview.py:{line} 的 ``SummaryLLM(`` 又回到不传 cfg 的写法了。"


def test_overview_run_accepts_cfg_and_defaults_to_none():
    """``overview.run()`` 的 cfg 形参必须存在且默认 None：默认值保向后兼容（老调用方
    不传就是老行为），存在才让 ``app/service.py`` 那条真调用能把它接进去。"""
    from video_workbench.core import overview

    params = overview.run.__defaults__ or ()
    kwonly = list(getattr(overview.run, "__kwdefaults__", {}) or {})
    assert "cfg" in kwonly, f"overview.run 的的关键字形参里没有 cfg：{kwonly}"
    assert overview.run.__kwdefaults__.get("cfg", "?") is None, (
        "cfg 的默认值必须是 None——非 None 的默认值等于让「忘了传」和「传了默认」"
        "两种写法看起来一样，那正是 task-59 要防的事。"
    )
    del params  # 位置默认值与本判据无关，只是别让它被误读成断言对象


@pytest.mark.parametrize(
    "snippet,expect_has_cfg",
    [
        ("llm = SummaryLLM(client, model=m, cfg=cfg)", True),
        ("llm = SummaryLLM(client, model=m)", False),
        ("llm = SummaryLLM(client, cfg=cfg)", True),
        ("llm = A.SummaryLLM(good, model='stub')", False),
    ],
)
def test_the_scanner_actually_reads_the_kwarg_rather_than_grepping_text(snippet, expect_has_cfg):
    """扫描器本身的判据：同一段文本里 ``cfg=`` 出现与否要能被 AST 正确分辨。
    没有这条，上面那条锁可能只是「恰好因为文本形状」而绿。"""
    sites = []
    node = ast.parse(snippet)
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "SummaryLLM":
                sites.append({kw.arg for kw in call.keywords} >= {"cfg"})
    assert sites and all(s is expect_has_cfg for s in sites), (
        f"扫描逻辑对 ``{snippet}`` 判错了（期望 cfg={expect_has_cfg}，实得 {sites}）"
    )
