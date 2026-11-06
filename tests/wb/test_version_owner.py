"""包内自显示版本号的唯一 owner（task-69 连带三）。

本文件钉三件事，全部沿用仓库既有口径，不新造概念：

1. **显示面只有一个真源**：状态条 / `app.info` / `app.bootstrap` / 桥接 `ping` /
   CLI `--json` 信封报的都是 `video_workbench.__version__` 这一个符号，没有第二份数。
2. **不许把数字写死**——AGENTS.md 第六节原话：「『单一 owner』这类规则，值相等的断言
   拦不住取法退化，必须写行为锁」。本文件对**能调的**（`app.info`/`app.bootstrap`/`ping`）
   走真调用，对**不好调的**（`cli.emit`、启动打印）走取法文本锁；第三条是机械闸：
   产品码非注释行里版本号字面量**只准出现在 `video_workbench/__init__.py` 那一行**。
3. **现状如实登记**（不粉饰）：五处取法全是 `from video_workbench import __version__`
   的 **import 期别名**，别名在 import 时就绑死 ⇒ patch 源模块不跟着变。出厂那一刻
   它们都等于 `__version__`（第 1 条钉住了），所以这**不是缺陷**，是**下次抬版本时的
   漂移面**。最后一条用例把这个形状钉成事实，并写明翻正它的正解；谁真改成经模块属性
   现取，那条用例的期望必须跟着翻，规则表那行也从「登记」升级成真行为锁。

跑法：``.venv/Scripts/python.exe -m pytest tests/wb/test_version_owner.py -q -p no:randomly``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import video_workbench
from video_workbench import cli as cli_mod
from video_workbench import desktop as desktop_mod
from video_workbench.core import transcriber as transcriber_mod
from video_workbench.workers import download_worker as download_worker_mod

REPO = Path(__file__).resolve().parents[2]

#: 五个读这个符号的模块（现读 `grep -n "__version__"` 的集合，别凭记忆加人）。
#: 用途不是枚举美学：新加一处显示版本就得往这张表加一行，加不进去就红第 3 条。
ALIAS_MODULES = (
    ("app/service", __import__("video_workbench.app.service", fromlist=["__version__"])),
    ("cli", cli_mod),
    ("desktop", desktop_mod),
    ("core/transcriber", transcriber_mod),
    ("workers/download_worker", download_worker_mod),
)


def _code_lines(text: str) -> str:
    """剥掉整行注释与行尾注释 —— AGENTS 口径：注释可以有、代码行不行。"""
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split("#", 1)[0])
    return "\n".join(out)


def test_the_live_surfaces_report_the_module_attribute(service):
    """三个能真调的显示面 == 现读的 `video_workbench.__version__`。"""
    live = video_workbench.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", live), f"版本号形状不对：{live!r}"

    info = service.dispatch("app.info")["data"]
    assert info["version"] == live, info

    boot = service.dispatch("app.bootstrap")["data"]
    assert boot["info"]["version"] == live, boot["info"]

    # 桥接探活：页面 boot() 用它确认 js_api 真的挂上了（DesktopApi.ping 不碰 self）
    api = object.__new__(desktop_mod.DesktopApi)
    pong = api.ping()
    assert pong["version"] == live, pong
    assert isinstance(pong["pong"], float)


APP_PLACEHOLDER = "{APP_NAME} {__version__}"


def test_the_two_surfaces_that_are_hard_to_call_still_read_the_symbol():
    """`cli.emit` 与桌面启动打印：取法文本锁（它们读符号，不读字面量）。

    为什么不建真 Cli：`Cli.__init__(argv: argparse.Namespace)` 要吃整个 arg namespace，
    把它拼起来测一行 `"version": __version__` 的成本远大于收益。文本锁能拦住的真问题
    是「有人在这里补了个写死的数」——那件事第 3 条也会红，这条是第一现场。
    """
    cli_src = (REPO / "video_workbench" / "cli.py").read_text(encoding="utf-8")
    assert '"version": __version__' in cli_src, "cli 的 --json 信封不再读符号了"

    desktop_src = (REPO / "video_workbench" / "desktop.py").read_text(encoding="utf-8")
    assert APP_PLACEHOLDER in desktop_src, "桌面启动打印不再读符号了"


def test_no_second_version_literal_in_product_code():
    """产品码非注释行里的版本号字面量，全仓只准有 owner 那一行。

    历史档案（`docs/`）里的旧版本号是**已发生的事实**，不在扫描域内——扫进来会在下次
    抬版本时逼人去改历史记录，那是假红。测试夹具（`tests/`）也不扫：夹具里造两个版本
    号做对照是正当用法，让它当闸只会逼人绕开判据。
    """
    hits: list[str] = []
    for path in sorted(REPO.glob("video_workbench/**/*.py")):
        rel = path.relative_to(REPO).as_posix()
        for n, line in enumerate(_code_lines(path.read_text(encoding="utf-8")).splitlines(), 1):
            if re.search(r"""['"]\d+\.\d+\.\d+['"]""", line):
                hits.append(f"{rel}:{n}")
    assert hits == ["video_workbench/__init__.py:14"], (
        f"版本号出现在不止 owner 那一行：{hits} —— 那就是第二个版本 owner"
    )


def test_the_owner_line_is_the_only_place_a_bump_has_to_happen(service):
    """抬版本的**实际效果**：改 owner 那一行 ⇒ 三个活显示面一起变。

    这条是「一处改、处处跟」的正向证据（在 import 别名的形状下也成立，因为
    `app/service.py` 用的是同一个 import 期符号；它不能证明 patch 源模块会跟着变，
    那是下一条的职责）。用真服务现读，不造快照。
    """
    live = video_workbench.__version__
    info = service.dispatch("app.info")["data"]
    assert info["version"] == live
    # owner 那一行的文本必须还在，否则上面那条会退化成读一个没人维护的常量
    owner_line = (REPO / "video_workbench" / "__init__.py").read_text(encoding="utf-8")
    assert re.search(rf'^__version__ = "{re.escape(live)}"$', owner_line, re.M), (
        "video_workbench/__init__.py 里的 __version__ 行形状变了，本文件的判据要跟着翻"
    )


def test_the_alias_shape_is_registered_as_a_drift_risk_not_a_passing_lock():
    """现状登记：五处都是 import 期别名 ⇒ patch 源模块属性**不会**让它们跟着变。

    不许把它写成「行为锁已通过」。正解（若哪天要真做成一处 owner）：把五处改成
    经模块属性现取 `video_workbench.__version__`，然后本用例的断言翻成
    「一个都不漂」，并把 AGENTS.md 规则表那一行从登记升级成真行为锁。
    """
    live_before = video_workbench.__version__
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(video_workbench, "__version__", "9.9.9-probe")
        drifted = [
            name
            for name, mod in ALIAS_MODULES
            if getattr(mod, "__version__", None) != "9.9.9-probe"
        ]
        assert len(drifted) == len(ALIAS_MODULES), (
            f"取法变了（不再全漂：drifted={drifted}）—— 这条用例的期望要跟着翻正，"
            "并把 AGENTS.md 规则表那行从「登记」升级成行为锁"
        )
    finally:
        monkey.undo()
    assert video_workbench.__version__ == live_before, "patch 没还原，后面全脏"


def test_the_status_bar_reads_the_payload_not_a_literal():
    """前端只写 `"v" + info.version`，app.js 里不许出现写死的版本号。"""
    js = (REPO / "video_workbench" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    code = _code_lines(js)
    assert re.search(r'"v"\s*\+\s*\(?\s*info\.version', code), (
        '状态条不再是 `"v" + info.version`：显示层被换成写死的数、'
        "或者改成 `paintInfo` 之外的人也在写 `#status-version`"
    )
    assert not re.search(r"""['"]\d+\.\d+\.\d+['"]""", code), (
        "app.js 里出现了写死的版本号字面量：那是第二个版本 owner"
    )
