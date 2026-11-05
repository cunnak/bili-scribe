"""task-55：任务失败的**原因**必须到用户眼前，不能只剩一句「生成失败」。

现场（2026-11-05 现读，三条合起来才是这个缺陷的形状）：

* `wb_job` 只有 `error_code` / `error_message` 两列（`pragma table_info(wb_job)`
  实测 15 列里没有 `error`、也没有 `error_detail`）；
* 总结页读的却是 `job.error.message`——那是个**不存在的键**，恒 `undefined`，
  于是界面永远落到写死的「生成失败」；
* 而 `clips.js` / `merge.js` / `subtitles.js` 读的是 `job.error_message`，
  所以同样的失败在别的页能看到原因、在总结页看不到。

最贵的那种失败恰好只有 `detail` 里有线索：`finish_reason=length，思考用了
16384 tokens`。用户照着这句调 `llm.max_tokens` 就能自修；把它丢掉等于把缺陷
藏起来。lead 定的落法是「拼进 `error_message` 这一列」——零 schema 迁移、零事件
订阅（`job.finished` 不算：后台线程跑完时页面可能根本没开着，事件会丢，那是假修复）。

这里两条锁各自有牙，**都不靠 grep 全仓**：

1. 后端行为锁：真 `JobManager` + 真 `Repo` + 真库列，断「有 detail 时那句原文必须
   进到 `error_message`」与「没 detail 时逐字等于 message」（后者护住另外八个消费者
   和 `test_cancel.py::test_cancel_writes_error_message`，别让拼接把干净文案拖出尾缀）。
2. 前端源码锁：把 `pollJob` 失败分支里出现的每个 `job.<键>` 拿去和**真库列集合**比。
   还原成 `job.error.message` 时，`error` 不在列里 ⇒ 必须红。扫描域走 `WB_WEB_JS_DIR`
   （照 `test_summary_retired_roles.py` 的先例），牙齿据此在 `%TEMP%` 副本上跑。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.service import WorkbenchService
from video_workbench.app.events import EventBus
from video_workbench.app.jobs import (
    ERROR_DETAIL_JOINER, ERROR_MESSAGE_MAX_CHARS, JobManager, error_message_for_row)
from video_workbench.infra.db import open_and_migrate
from video_workbench.infra.repo import Repo

ROOT = Path(__file__).resolve().parents[2]
WEB_JS_DEFAULT = ROOT / "video_workbench" / "web" / "js"

# 与 task-52 实测同一条原文：预算被打满时端点返回的就是这两句。
LLM_EMPTY_MESSAGE = "模型这次只输出了思考过程，正文是空的（预算被思考吃掉了）"
LLM_EMPTY_DETAIL = "finish_reason=length，思考用了 16384 tokens。"


def _js_dir() -> Path:
    """扫描域。默认是仓库里那份产品树，`WB_WEB_JS_DIR` 让牙齿指向 `%TEMP%` 副本。"""
    return Path(os.environ.get("WB_WEB_JS_DIR") or WEB_JS_DEFAULT)


@pytest.fixture()
def manager(tmp_path):
    """真库 + 真 Repo + 真 JobManager——错误落哪一列必须由真 SQL 说了算。"""
    conn = open_and_migrate(tmp_path / "wb.db")
    repo = Repo(conn)
    events = EventBus()
    m = JobManager(repo, events, default_limit=2, tmp_root=tmp_path / "tmp")
    yield m, repo
    m.shutdown()
    events.close()
    conn.close()


def _run_failing(manager, error: WBError) -> dict:
    """跑一个真任务，让它抛 `error`，返回库里那一行。"""
    m, repo = manager

    def handler(ctx, payload):
        raise error

    m.register("probe-fail", handler)
    started = m.submit("probe-fail", None, {})
    deadline = time.time() + 10.0
    while time.time() < deadline:
        row = repo.job_get(started["id"]) or {}
        if row.get("status") in ("failed", "succeeded"):
            return row
        time.sleep(0.05)
    pytest.fail(f"任务十秒内没落到终态：{row.get('status')!r}")


# ------------------------------------------------------------------ 后端：那一列


def test_a_failure_with_detail_lands_the_detail_in_the_error_message_column(manager):
    """`detail` 必须进 `error_message`——它是唯一能自修的那句。"""
    row = _run_failing(
        manager, WBError("LLM_EMPTY", LLM_EMPTY_MESSAGE, LLM_EMPTY_DETAIL))
    assert row["status"] == "failed", row
    # 码不能因为拼文案而变——`_llm_code` 的白名单与重试逻辑都按码走。
    assert row["error_code"] == "LLM_EMPTY", row
    stored = row["error_message"] or ""
    assert LLM_EMPTY_MESSAGE in stored, stored
    assert LLM_EMPTY_DETAIL in stored, (
        "detail 被丢掉了：用户在界面上只会看到「模型这次只输出了思考过程」这半句，"
        f"拿不到 `finish_reason=length` 那个可自修的原文。实得 {stored!r}")


def test_a_failure_without_detail_stays_exactly_the_message(manager):
    """没 detail 的失败**逐字不变**。

    这条不是顺手加的：`error_message` 有八个消费者（`clips.js:221`、`merge.js:441`、
    `subtitles.js:480`、`cli.py:166`、`service.py:4312`、`batch_worker.py:94` 等），
    取消与恢复那两条的消息本来就没有 detail，拼出一个 ` —— ` 尾缀是把干净文案弄脏，
    而 `tests/wb/test_cancel.py` 正是按整句相等断的。
    """
    row = _run_failing(manager, WBError("PROCESS_FAILED", "故意失败"))
    assert row["error_message"] == "故意失败", row


def test_the_joiner_is_a_single_owner_in_the_backend():
    """拼接规则只在 `error_message_for_row` 一处：空 detail / 空 message / 超长各一条。"""
    assert error_message_for_row(None) is None
    assert error_message_for_row({"code": "X", "message": "甲"}) == "甲"
    assert error_message_for_row({"code": "X", "detail": "乙"}) == "乙"
    joined = error_message_for_row({"message": "甲", "detail": "乙"})
    assert joined == "甲 —— 乙", joined
    # 多行的 detail（INTERNAL 那条是 traceback.format_exc()）只留最后一行——
    # 全文明细在 `result_json` 的 log 与日志文件里，主文案不许撑成一屏。
    tail = error_message_for_row(
        {"message": "内部错误", "detail": "Traceback (most recent call last):\n  File x\nTypeError: 炸了"})
    assert tail == "内部错误 —— TypeError: 炸了", tail
    long = error_message_for_row({"message": "短", "detail": "啊" * 2000})
    assert len(long) <= 600, len(long)


# ------------------------------------------------------------------ 前端：读对键


def _strip_js_comments(source: str) -> str:
    """去掉 `//` 行注释与 `/* */` 块注释，只留代码。

    这条不是装饰：我写在 `summary.js` 里那些「以前读的是 `job.error.message`，
    那是个不存在的键」的解释性注释，**本身**就会被键扫描抓到。判据扫注释就等于
    禁止代码讲道理——task-36 已经为此把解释删掉过一次，那是判据反过来损害代码。
    """
    no_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(ln.split("//", 1)[0] for ln in no_block.splitlines())


def _poll_failure_branch(source: str) -> str:
    """取出 `pollJob` 里判 failed/canceled 的那一段源码（注释已剥）。

    只扫这一段，不扫整个文件：别的函数也有 `job.xxx`，全文件扫会把不相干的键
    一起判进来，将来加个 `job.attempts` 之类的正常读取都会被误红。
    """
    source = _strip_js_comments(source)
    marker = 'if (job.status === "failed" || job.status === "canceled")'
    start = source.find(marker)
    assert start >= 0, (
        "找不到 pollJob 的失败分支（那段文案或判断被改名了？）——这条锁现在空转，"
        "请改成新形状，别让它绿着骗人")
    depth = 0
    for idx in range(source.index("{", start), len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    pytest.fail("失败分支的花括号没闭合，提取器要重写")


def _job_row_columns(tmp_path) -> set[str]:
    conn = open_and_migrate(tmp_path / "cols.db")
    try:
        return {str(r[1]) for r in conn.execute("pragma table_info(wb_job)")}
    finally:
        conn.close()


def test_the_summary_page_reads_only_keys_that_exist_on_the_job_row(tmp_path):
    """总结页失败分支里出现的每个 `job.<键>` 都必须是真列。

    这条就是「读的是真实存在的键」那个判据的机械形式：`job.error.message` 里的
    `error` 不是列 ⇒ 还原成旧写法必红；反过来，加一列（比如将来真做 `error_detail`）
    也不会让这条误红，因为列集合是现读的。
    """
    branch = _poll_failure_branch(
        (_js_dir() / "pages" / "summary.js").read_text(encoding="utf-8"))
    keys = set(re.findall(r"\bjob\.([a-z_][a-z0-9_]*)", branch))
    assert keys, f"失败分支里一个 job.<键> 都没读到，这条空转了：{branch!r}"
    columns = _job_row_columns(tmp_path)
    unknown = sorted(keys - columns)
    assert unknown == [], (
        f"总结页在读不存在于 wb_job 的键 {unknown}——界面上它会恒 undefined，"
        f"失败原因被丢掉，只剩写死的「生成失败」。真列集合：{sorted(columns)}")


def test_the_summary_page_actually_uses_the_message_column():
    """控制腿：上一条不许是靠「分支里根本没有 job. 引用」蒙绿的。"""
    branch = _poll_failure_branch(
        (_js_dir() / "pages" / "summary.js").read_text(encoding="utf-8"))
    assert "job.error_message" in branch, (
        "失败分支不再读 error_message —— 要么换了键（那上一条得跟着重设计），"
        f"要么整句被删。实读分支：\n{branch}")


def test_the_page_does_not_rebuild_the_sentence_the_backend_already_joined(tmp_path):
    """detail 的措辞只有一个 owner：前端不许自己再拼一次「原因 —— 原文」。

    两处各拼一次 = 同一个事实两句话，将来后端改分隔符，界面就出现「甲 —— 乙 —— 乙」。
    """
    source = _strip_js_comments(
        (_js_dir() / "pages" / "summary.js").read_text(encoding="utf-8"))
    assert "detail" not in source.lower(), (
        "summary.js 开始自己处理 detail 了——拼接规则的唯一出处是 "
        "app/jobs.py 的 error_message_for_row，第二处 owner 会让两边措辞漂开")


def test_the_scan_domain_switch_changes_the_file_not_the_verdict(monkeypatch, tmp_path):
    """AGENTS.md 第六节：开关只改「扫哪份文件」，不改判据本身。

    把 `WB_WEB_JS_DIR` 指到仓库那份 js 目录的绝对路径，三条前端判据必须给出**同样的
    结论**（绿）。这条存在的原因是：如果哪天有人把开关实现成「设了就跳过检查」，
    牙齿跑起来永远是绿的，判据就白写了。
    """
    monkeypatch.setenv("WB_WEB_JS_DIR", str(WEB_JS_DEFAULT))
    assert _js_dir() == WEB_JS_DEFAULT
    branch = _poll_failure_branch(
        (_js_dir() / "pages" / "summary.js").read_text(encoding="utf-8"))
    columns = _job_row_columns(tmp_path)
    assert set(re.findall(r"\bjob\.([a-z_][a-z0-9_]*)", branch)) - columns == set()
    assert "job.error_message" in branch
    # 指到一份不存在的目录时，必须**炸**而不是静默通过。
    monkeypatch.setenv("WB_WEB_JS_DIR", str(tmp_path / "no-such-js"))
    with pytest.raises((FileNotFoundError, AssertionError)):
        _poll_failure_branch(
            (_js_dir() / "pages" / "summary.js").read_text(encoding="utf-8"))

# ═══════════════════════════════════════════════════════════════════════
# task-68（B 方案）：提示条 =「主文案一句 + 真展开」，窄窗不许横向撑破。
#
# 三面分开守，各用一侧、各有牙：
#   · A 组（静态面）盯 summary.js / summary.css 的结构形状，扫描域走**既有**
#     开关 `WB_WEB_JS_DIR`（CSS 取 `parent / "css"`，指到副本时自然跟着走，
#     不新增开关名）。
#   · B 组（渲染面）盯 chromium 里真画出来的几何，被测目录走**既有**
#     `WB_WEB_ROOT`（`test_settings_role_warning_ui.web_base_url` 就是从那一个
#     变量取的树）。
#   · C 组（牙）在 `%TEMP%\wb_t68_teeth\task68_teeth_*` 自带哨兵名的副本上把实现
#     退化成事故现场，**每个变体先声明期望红名单**：缺红与多红都算失败。
#
# 三列文本全部由**产品自己**算出来（`error_message_for_row`），不手搓长度。
# 实测列宽 23 / 140 / 600。合同里写的「62 / 506 / 600」不是这三个数：62 是上一轮
# t66 那档**真链路** NETWORK 那一列的长度，506 是那轮的**请求体**字符数，两个都不是
# 这一列的宽度 —— 按实测报，不为凑合同改数。端到端读数与九张图见交付表，
# 探针 `C:/Users/b1596/AppData/Local/Temp/wb_t68/t68_accept.py`。
#
# 扫描域只取提示条那一段（`_strip_body()`），因为 summary.js 别处有合法的
# `title:`（字段提示、发布卡跳转）与 `slice(`（markdown 渲染、序号补零），
# 整文件禁令会把别人的正常写法判成违规。
# ═══════════════════════════════════════════════════════════════════════

from test_settings_role_warning_ui import browser, web_base_url  # noqa: E402  既有夹具

#: 提示条正文与展开开关的节点 id（逐字来自 `summary.js` 的 setErrorBar）。
#: A 组用它取扫描域，B 组用它查 DOM —— 同一条字符串只写一次。
BODY_ID = "summary-error-body"
#: 开关的节点 id，同一族。
MORE_ID = "summary-error-more"
#: 提示条容器自己的 id（`render()` 里建的那一个）。
BAR_ID = "summary-error"

#: 开关文案（逐字来自 `summary.js:1183`；B 组用它判翻转，A 组用它盯改动面）。
LABEL_OPEN = "展开"
LABEL_CLOSE = "收起"

#: 两个 paint 路径共用的渲染口（`summary.js:1204` / `:1249` 各一次）。
TOGGLE_CALL_SITES = "setErrorBar(document.getElementById(\"summary-error\"))"

#: 四条判据的短名，渲染面与牙齿共用一份，别在两处各写一遍。
CHECKS = ("node_inside_parent", "no_clipped_content", "page_no_h_scroll", "no_sibling_overlap")


def _css_dir() -> Path:
    """CSS 与被扫的 js 同源，走同一个开关。"""
    return _js_dir().parent / "css"


def _css_rule(selector: str) -> str:
    """取某条规则（从选择器到它的 `}`），选择器不认识就红。"""
    css = (_css_dir() / "summary.css").read_text(encoding="utf-8")
    start = css.find(selector)
    assert start >= 0, f"summary.css 里找不到 {selector} —— 实现不在了"
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[start:end + 1]


def _js_source() -> str:
    return (_js_dir() / "pages" / "summary.js").read_text(encoding="utf-8")


def _strip_body() -> str:
    """提示条那一段源码（从 `var errorOpenText` 到 `function paintCollection`）。

    取这一段是因为 `title:` 与 `slice(` 在 summary.js 别处是**合法写法**（字段提示、
    markdown 渲染），整文件禁令会红得不讲道理。锚点之一认开关的 id、之二认下一个函数。
    """
    source = _strip_js_comments(_js_source())
    start = source.find("var errorOpenText")
    if start < 0:
        start = source.index(f'id: "{MORE_ID}"')
    end = source.find("function paintCollection", start)
    assert end > start, "summary.js 里取不到提示条那一段（paintCollection 位置变了）"
    return source[start:end]


def _node_line(marker: str, what: str) -> str:
    """取**唯一**一行建节点源码：命中数不是 1 就红，别静默读错行。"""
    lines = [line.strip() for line in _strip_js_comments(_js_source()).splitlines()
             if marker in line]
    assert len(lines) == 1, f"{what} 应当只有 1 行，实测 {len(lines)} 行：{lines}"
    return lines[0]


def _body_node_line() -> str:
    return _node_line(f'id: "{BODY_ID}"', "正文节点")


def _bar_node_line() -> str:
    return _node_line(f'id: "{BAR_ID}"', "提示条容器节点")


def _toggle_node_line() -> str:
    return _node_line(f'id: "{MORE_ID}"', "展开开关节点")


def _column(message: str, detail: str = "") -> str:
    """把一行失败形状交给**产品的拼接 owner**，返回真会出现的那一列。"""
    return error_message_for_row({"code": "INTERNAL", "message": message, "detail": detail})


# 撑破容器的东西是端点原话里那两串**不可断 ASCII**（invalid_request_error /
# messages[12].content），不是中文长度。上一轮实测：节点 275px 而父 .sum-bar 只有 92px。
_UNBREAKABLE_HEAD = ('模型返回的 JSON 无法解析，原始响应体片段：{"error":'
                     '{"type":"invalid_request_error","message":"')
_UNBREAKABLE_MID = "请检查 model、messages 条数、以及一段被截断的上下文"
_UNBREAKABLE_TAIL = '","param":"messages[12].content"}}'
#: 端点 403 的真实响应体一行（上一轮现场抄来的形状，里面有那两串不可断 ASCII）。
CF_BODY = ('{"error":{"type":"invalid_request_error","message":"'
           + _UNBREAKABLE_MID + '","param":"messages[12].content"}}')
_UNBREAKABLE = _UNBREAKABLE_HEAD + _UNBREAKABLE_MID + _UNBREAKABLE_TAIL

#: 短列：一行放得下（`detail` 为空 ⇒ 后端逐字返回 message，不拼分隔符）。
COLUMN_SHORT = _column("连不上那个地址，检查网络或 base_url。")
#: 真 403 那一形：message + 「 —— 」+ 端点响应体一行。
COLUMN_403 = error_message_for_row({"code": "LLM_FORBIDDEN",
                                    "message": "模型这次请求被端点拒了（403）。",
                                    "detail": CF_BODY})
#: 封顶列：正好 600（`ERROR_MESSAGE_MAX_CHARS` 现读 = 600）。
_CAPPED_MESSAGE = _UNBREAKABLE_HEAD + _UNBREAKABLE_MID * 8 + _UNBREAKABLE_TAIL
COLUMN_CAPPED = _column(_CAPPED_MESSAGE,
                        detail="Traceback (most recent call last):" + "\n"
                               + "  假的一行栈" + "\n"
                               + "ValueError: " + _CAPPED_MESSAGE)

# 三档视口：360/375/440 是合同点名的三档，高度只影响截图取景。
_VIEWPORTS = [(360, 740), (375, 812), (440, 800)]
#: 四个**失败形状**。每一形都跑真任务，那一列由后端拼接 owner 现算并落进真库，
#: 测试只从库里读回来 —— 不手搓长度。上一轮 t66 的真链路读数：NETWORK 那一列
#: len=62（所以叫 net62）；cf403 / cbig600 两形的长度由 B 组自己断言与打印。
_SHAPES = ("net62", "cf403", "cbig600")
_CELLS = [(vw, vh, shape) for (vw, vh) in _VIEWPORTS for shape in _SHAPES]
_CELL_IDS = [f"{vw}-{shape}" for (vw, _vh, shape) in _CELLS]
_VP_IDS = [f"{vw}-{vh}" for (vw, vh) in _VIEWPORTS]


\
#: 只有这七个方法走真后端，其余留在 mock（最小干预，同
#: `test_summary_real_backend_ui.py` 的 BRIDGE_JS 口径）。
REAL_METHODS = ["collection.list", "job.get", "library.list", "summary.get",
                "summary.models", "summary.roles", "summary.start"]

#: 夹具的落点。只写这一棵 Temp 树；真实 `~/.bili-scribe` 与 `~/.video-workbench`
#: 一字不动（AGENTS 第四节第 3 条的前后 sha 核对交给独立探针跑）。
SVC_HOME = Path(tempfile.gettempdir()) / "wb_t68_strip_fixture"

#: 端点 403 的响应体那一形，逐字抄自上一轮现场探针
#: `C:/Users/b1596/AppData/Local/Temp/wb_narrow/t66_round7.py` 的 CF_BODY。
_CF_BODY = ('{"error":{"type":"invalid_request_error","message":'
            + _UNBREAKABLE_MID + ',"param":"messages[12].content"}}')
#: 一条 INTERNAL 形状：message 里塞满不可断 ASCII，交给后端截到封顶值。
_BIG = _UNBREAKABLE_HEAD + _UNBREAKABLE_MID * 8 + _UNBREAKABLE_TAIL


def _chat_forbidden(self, *args, **kwargs):
    """chat 换成「端点回 403」：用产品自己的 `_error_for` 造错误，不自己编文案。"""
    raise self.client._error_for(403, _CF_BODY)


def _chat_huge(self, *args, **kwargs):
    """chat 换成「抛超长 ValueError」⇒ 未捕获异常 ⇒ INTERNAL ⇒ 那一列被截到封顶值。"""
    raise ValueError(_BIG)


def _chat_empty_detail(self, *args, **kwargs):
    """短列那一形：产品的 NETWORK 文案 + 空 detail ⇒ 后端逐字返回 message，不拼分隔符。"""
    from video_workbench.infra.llm import ChatError
    raise ChatError("NETWORK", "连不上大模型服务", "")


#: 形状 → 要换掉的 chat。net62 不换：真打 `http://127.0.0.1:9/v1` 被本地拒连，
#: 那一列完全由产品的错误管道与拼接 owner 产出（上一轮实测 len=62）。
SHAPE_PATCH = {"net62": None, "cf403": _chat_forbidden,
               "cbig600": _chat_huge, "short8": _chat_empty_detail}


def _write_cfg(home: Path, base_url: str) -> Path:
    """Temp 里的一份 config.yaml（CRLF，同上一轮探针写法），db/key/cache 全落 Temp。"""
    cfg_dir = home / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "llm:\n"
        '  api_key: "sk-SENTINEL-not-a-real-key"\n'
        f'  base_url: "{base_url}"\n'
        '  model: "workbuddy/deepseek-v4.1-flash"\n'
        '  role: "renhua"\n'
        "  max_tokens: 32768\n"
        "storage:\n"
        f'  db_path: "{(home / "wb.db").as_posix()}"\n'
        f'  key_path: "{(home / "key.bin").as_posix()}"\n'
        f'  cache_root: "{(home / "cache").as_posix()}"\n'
    ).replace("\n", "\r\n")
    (cfg_dir / "config.yaml").write_bytes(text.encode("utf-8"))
    return cfg_dir / "config.yaml"


def _seed_project(svc, title: str, home: Path) -> str:
    """夹具想让总结页肯跑任务必须**真写 transcript.json**（AGENTS 第四节第 4 条）。

    root 挂在**这一格自己的 home** 下：标题是给人看的短标签，不再兼职唯一性。
    """
    root = home / "roots" / title
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    made = svc.repo.project_create(title=title, bvid="", root_dir=str(root))
    pid = made if isinstance(made, str) else (made or {}).get("id", "")
    segs = [{"t": float(i * 8), "duration": 8.0, "text": f"第 {i} 句：用来把失败路径跑到。"}
            for i in range(1, 25)]
    svc.repo.transcript_upsert(pid, segments=segs)
    (root / "transcript.json").write_text(json.dumps({"segments": segs}, ensure_ascii=False),
                                          encoding="utf-8")
    return pid


def _failed_column(svc, pid: str) -> str:
    """从**真库**里读那一列（产品拼接 owner 的产物），不是我自己算的字符串。"""
    for _ in range(900):
        rows = [r for r in svc.repo.job_list(limit=50, project_id=pid)
                if r.get("status") == "failed"]
        if rows:
            return str(rows[0].get("error_message") or "")
        time.sleep(0.05)
    raise AssertionError(f"{pid} 等不到 failed 任务：那一列没落库")


@pytest.fixture()
def strip_driver(browser, web_base_url, monkeypatch):
    """返回 `open(viewport, shape) -> (page, column)`：真后端 + 真失败 + 真点击。

    为什么不写 `state.error` 再想办法刷一次：`render()` 第一行就是 `state.error = ""`
    （`summary.js` 现读），公开的 `reload` 是 `loadSummary`、它不画提示条，而 `paint`
    没导出 ⇒ 手工写状态要么被清、要么得我自己建节点。合同要的是「真用户路径」，
    所以这里跑**真任务**：`summary.start` → 真 JobManager → 真库列 → `pollJob` →
    `start()` 的 catch → 同一套 `paint()` → `setErrorBar`。模型一次没真调
    （net62 是本地拒连，其余三形在 `SummaryLLM.chat` 里直接抛），`calls=0`。

    chat 的换法走产品自有开关面（同 `AGENTS` 第五节「零改动量法」：包一层
    `SummaryLLM.chat`，不动字节锁的副本文件），并用 monkeypatch 在用例结束后还原。
    """
    from video_workbench.core import summarizer as wb_summarizer

    base = web_base_url.split("#")[0]
    services = []
    opened = []

    def open_strip(viewport, shape):
        vw, vh = viewport
        # 每格一份独立 home：同名的 shape-vw 会在同一次 module 里被开两次
        # （几何那格 + 可达那格），上一格的 sqlite 还压在库里，rmtree 静默失败、
        # mkdir 就抛 FileExistsError；独立库同时堵住「读到上一次运行的 failed 行」。
        SVC_HOME.mkdir(parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix=f"{shape}-{vw}-", dir=str(SVC_HOME)))
        cfg = _write_cfg(home, "http://127.0.0.1:9/v1")
        patch = SHAPE_PATCH[shape]
        if patch is not None:
            monkeypatch.setattr(wb_summarizer.SummaryLLM, "chat", patch)
        svc = WorkbenchService(config_path=str(cfg))
        services.append(svc)
        # 标题只做人能认出来的标签，别拼随机后缀：草稿卡会把这个标题当大标题
        # 画出来，长 ASCII 串把 `.sum-fields` 撑出列宽（实测 406px 对 140px 的列），
        # 整页横向溢出之后点「开始总结」会把提示条推出视口（实测 left = -47）。
        pid = _seed_project(svc, f"t68-{shape}-{vw}", home)

        page = browser.new_page(viewport={"width": vw, "height": vh})
        opened.append(page)
        problems = []
        page.on("pageerror", lambda exc: problems.append(str(exc)))
        page.on("console", lambda msg: problems.append(msg.text)
                if msg.type == "error" and "favicon" not in (msg.text or "") else None)
        page.expose_binding("__wbRpc",
                            lambda source, payload: _logged_dispatch(payload))
        bridge_calls = []

        def _logged_dispatch(payload):
            bridge_calls.append(str(payload.get("method") or ""))
            return svc.dispatch(payload.get("method"), payload.get("params") or {})

        page.goto(base, wait_until="domcontentloaded")
        skip = page.locator("#onb-skip")
        if skip.count() and skip.first.is_visible():
            skip.first.click()                      # 产品自己的关法，不用 JS 硬移除节点
        page.wait_for_function(
            "() => !document.getElementById('onb-overlay') || "
            "getComputedStyle(document.getElementById('onb-overlay')).display === 'none'",
            timeout=15000)
        page.evaluate("""(methods) => {
                          const real = new Set(methods);
                          const legacy = WB.rpc;
                          WB.rpc = function (name, params, opts) {
                            if (!real.has(name)) { return legacy.call(WB, name, params, opts); }
                            return Promise.resolve(window.__wbRpc({method: name,
                                                                   params: params || {}}))
                              .then((res) => {
                                if (res && res.ok) { return res.data; }
                                const err = (res && res.error) || {};
                                throw new Error(err.message || ('后端调用失败: ' + name));
                              });
                          };
                        }""", REAL_METHODS)
        page.evaluate("() => window.WB.router.go('summary')")
        page.wait_for_selector(f"#{BAR_ID}", state="attached", timeout=15000)
        page.wait_for_function(
            "(pid) => { const sel = document.getElementById('sum-project');"
            " return !!sel && Array.from(sel.options).some(o => o.value === pid); }",
            arg=pid, timeout=15000)
        page.select_option("#sum-project", pid)      # 真下拉，不是硬塞 state.projectId
        page.click("#sum-start")                     # 真按钮：产品的 start() 全链路
        page.wait_for_selector(f"#{BAR_ID}", state="visible", timeout=30000)
        page.wait_for_selector(f"#{BODY_ID}", state="attached", timeout=15000)
        assert "summary.start" in bridge_calls, (
            "真 summary.start 没发出去 ⇒ 这条提示栏不是失败列。产品 start() 在 "
            "target.has_transcript === false 时直接落界面提示并 return，"
            "要修的是夹具（字幕得真写到盘上），不是拿别的句子当失败列量几何。")
        column = _failed_column(svc, pid)
        assert problems == [], f"页面自己报了错：{problems}"
        page.wait_for_function(
            """
            (arg) => {
              const b = document.getElementById(arg.bodyId);
              return !!b && b.textContent === arg.column;
            }
            """,
            arg={"bodyId": BODY_ID, "column": column},
            timeout=30000,
        )
        return page, column

    yield open_strip
    for page in opened:
        try:
            page.close()
        except Exception:
            pass
    for svc in services:
        svc.close()


def _baseline_doc(page) -> int:
    """提示条隐藏时页面的 doc scrollWidth —— 既有溢出与本卡改动面的分界线。

    360 档页面本身就有 364 vs 360 的横向溢出，元凶是顶栏换肤控件 `#skin-picker` /
    `.skin-chip`（样式在 `video_workbench/web/css/app.css` —— 本卡的越界清单里，
    不许为凑那条判据去动它，也不许在提示条上硬加没用的规则）。基线在同一个页面里
    现量，不写死 364。
    """
    return page.evaluate(
        """(barId) => {
             const el = document.getElementById(barId);
             const prev = el.style.display;
             el.style.display = 'none';
             const w = document.documentElement.scrollWidth;
             el.style.display = prev;
             window.__t68BaselineDoc = w;
             return w;
           }""",
        BAR_ID)


def _geometry(page):
    """四个数一次取全（同一快照，避免中间重排把展开态混进收起态）。"""
    return page.evaluate(
        """([barId, bodyId, checks]) => {
             const bar = document.getElementById(barId);
             const body = document.getElementById(bodyId);
             if (!bar || !body) { return { missing: true, bar: !!bar, body: !!body }; }
             const rect = (e) => e.getBoundingClientRect();
             const rBar = rect(bar), rBody = rect(body);
             const rParent = rect(bar.parentElement);
             const rNote = bar.nextElementSibling ? rect(bar.nextElementSibling) : null;
             const rHost = bar.parentElement.nextElementSibling
                ? rect(bar.parentElement.nextElementSibling) : null;
             const inter = (a, b) => {
               if (!a || !b) { return 0; }
               const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
               const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
               return (w > 0 && h > 0) ? Math.round(w * h * 100) / 100 : 0;
             };
             const doc = document.documentElement.scrollWidth;
             const base = Number(window.__t68BaselineDoc || doc);
             const cs = getComputedStyle(bar);
             return {
               nodeWidth: Math.round(rBody.width * 10) / 10,
               nodeRight: Math.round(rBody.right * 10) / 10,
               // 合同那四条都是相对量：整列被推出视口时它们照样绿，
               // 所以补一条绝对量（左边不出左边界、右边不出右边界）。
               nodeLeft: Math.round(rBody.left * 10) / 10,
               nodeInViewport: (rBody.left >= 0 && rBody.right <= window.innerWidth + 0.5),
               nodeScrollWidth: body.scrollWidth,
               nodeClientWidth: body.clientWidth,
               nodeHeight: Math.round(rBody.height * 10) / 10,
               parentRight: Math.round(rParent.right * 10) / 10,
               barScrollWidth: bar.scrollWidth,
               barClientWidth: bar.clientWidth,
               docScrollWidth: doc,
               docBaseline: base,
               docOverBaseline: doc - base,
               innerWidth: window.innerWidth,
               overlapNote: inter(rBar, rNote),
               overlapHost: inter(rBar, rHost),
               overflowWrap: cs.overflowWrap,
               fails: checks.filter((name) => {
                 if (name === "node_inside_parent") { return rBody.right > rParent.right + 0.5; }
                 if (name === "no_clipped_content") { return body.scrollWidth > body.clientWidth + 0.5; }
                 if (name === "page_no_h_scroll") { return doc - base > 0; }
                 if (name === "no_sibling_overlap") { return inter(rBar, rNote) > 0 || inter(rBar, rHost) > 0; }
                 return true;
               }),
             };
           }""",
        [BAR_ID, BODY_ID, list(CHECKS)])


def _toggle(page):
    """开关的存在性 + 同快照三条件（高>0、落在提示条盒内、elementFromPoint 命中自己）。"""
    return page.evaluate(
        r"""([moreId, barId]) => {
             const more = document.getElementById(moreId);
             if (!more) { return { missing: true }; }
             if (getComputedStyle(more).display === "none") { return { hidden: true }; }
             const r = more.getBoundingClientRect();
             const bar = document.getElementById(barId).getBoundingClientRect();
             const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
             const hit = document.elementFromPoint(cx, cy);
            const label = (el) => el ? (el.tagName.toLowerCase()
                + (el.id ? "#" + el.id : "")
                + (el.className && typeof el.className === "string"
                   ? "." + el.className.trim().split(/\s+/).join(".") : "")) : null;
             return {
               tag: more.tagName.toLowerCase(),
               label: more.textContent,
               expanded: more.getAttribute("aria-expanded"),
               height: Math.round(r.height * 10) / 10,
               top: Math.round(r.top * 10) / 10,
               bottom: Math.round(r.bottom * 10) / 10,
               barTop: Math.round(bar.top * 10) / 10,
               barBottom: Math.round(bar.bottom * 10) / 10,
               hitSelf: !!hit && (hit === more || more.contains(hit)),
              // 红话里要能看出「被谁盖住」，不然 hitSelf: False 只能靠猜。
              hitStack: document.elementsFromPoint(cx, cy).slice(0, 3).map(label),
              docScroll: document.documentElement.scrollTop,
              pageScroll: (() => { const p = document.querySelector(".page");
                                   return p ? p.scrollTop : null; })(),
               title: more.getAttribute("title"),
             };
           }""",
        [MORE_ID, BAR_ID])


def _shown_text(page) -> str:
    """正文节点上**真画出来**的那一句（不靠测试自己算，也不回读后端列）。"""
    return page.evaluate(f"() => document.getElementById('{BODY_ID}').textContent")


def _force_open(page) -> None:
    """直接去掉收起态 —— 用来验「开关的出现条件是正文真被裁，不是文本长度」。"""
    page.evaluate(f"""() => document.getElementById('{BODY_ID}').classList.remove('is-clamped')""")


# ── A 组：静态形状（不启动浏览器，扫描域走 WB_WEB_JS_DIR）─────────────────


def test_the_three_columns_have_the_measured_widths():
    """三列文本先证明是**我要的那三形**：短 23 / 真 403 那一形 140 / 封顶 600。

    这条是给后面所有几何判据当地基的：列宽哪天被拼接 owner 改了，几何表里
    「哪一格是哪一形」的对应关系会静默错位，所以让它先红。数字全部现读自产品。
    """
    assert len(COLUMN_SHORT) == 23, COLUMN_SHORT
    assert COLUMN_SHORT == "连不上那个地址，检查网络或 base_url。"
    assert len(COLUMN_403) == 140, len(COLUMN_403)
    assert ERROR_DETAIL_JOINER in COLUMN_403
    assert len(COLUMN_CAPPED) == ERROR_MESSAGE_MAX_CHARS == 600, len(COLUMN_CAPPED)
    for token in ("invalid_request_error", "messages[12].content"):
        assert token in COLUMN_403 and token in COLUMN_CAPPED, token


def test_the_collapsed_strip_is_clamped_to_one_line_by_css():
    """「主文案一句」是 CSS 收成**第一视觉行**，不是前端截字符。

    四件一起才成立（`display:-webkit-box` + `-webkit-line-clamp` + `-webkit-box-orient`
    + `overflow`），少 `overflow: hidden` 就不裁；`line-clamp` 必须是 1，写成 2 就不止
    一句。写法与本文件已有的 `.coll-row__title` 同源。正文节点必须真挂上 `is-clamped`，
    否则收起态没人负责，600 字一屏全是红。
    """
    rule = _css_rule(".sum-bar .callout__body.is-clamped")
    for decl in ("display: -webkit-box", "-webkit-line-clamp: 1",
                 "-webkit-box-orient: vertical", "overflow: hidden"):
        assert decl in rule, f"{decl} 不在收起态那段里：\n{rule}"
    assert "is-clamped" in _body_node_line(), _body_node_line()
    strip = _strip_body()
    for cutter in (".split(", ".slice(", ".substring("):
        assert cutter not in strip, f"前端在裁字符（{cutter}）= 自己造第二份截断规则"


def test_the_strip_keeps_the_unbreakable_tokens_wrappable():
    """窄窗里撑破容器的元凶是**不可断 ASCII token**，所以 CSS 必须让它们能断行。

    实测（360/375/440 × 600 字封顶行）：`overflow-wrap: anywhere` 一条就同时拦住
    「提示条 right ≤ 父 right」「scrollWidth ≤ clientWidth」「与下一兄弟相交 = 0」；
    再补 `min-width:0` 三条读数一字不变，只补它反而把 scrollWidth 判据打成
    265 > 76（文字被裁掉看不见）。所以这里只钉实测承重的那一条，
    不写「两条都要加」（前一轮的推断已被实测推翻）。
    """
    rule = _css_rule(".sum-bar .callout")
    assert "overflow-wrap: anywhere" in rule, rule
    strip = _strip_body()
    assert f'id: "{BODY_ID}"' in strip, "正文节点不在提示条那一段里"


def test_the_strip_carries_the_whole_column_and_opens_it_verbatim():
    """正文节点建出来就带着**那一列全文**，展开态是同一句、一字不丢。

    前端不切分 = 不二次拥有「message + 分隔符 + 尾巴」这条拼接规则（唯一 owner 是
    `app/jobs.py` 的 `error_message_for_row`），也顺手护住上面那条常驻用例
    「页面不许重拼后端已经拼好的句子」。
    """
    body = _body_node_line()
    assert "text: text }" in body, f"正文没拿整列：{body}"
    assert "ui.h(\"div\"" in body, f"正文节点不是块级：{body}"
    strip = _strip_body()
    for cutter in (".split(", ".slice(", ".substring("):
        assert cutter not in strip, f"前端在切那一列（{cutter}）= 第二个拼接/截断 owner"


def test_the_error_bar_node_is_a_block_container():
    """提示条容器必须是 `div`：行内元素装不下「正文块 + 开关」两个子节点。

    它同时是 `.sum-bar` 的 flex 子项；实测把 B 方案挂在这个节点上，退回
    `ui.h("span", …)` 就等于把展开结构塞进行内盒。
    """
    line = _bar_node_line()
    assert line.startswith('ui.h("div"'), f"提示条节点不是 div：{line}"
    assert "callout--warn" in line, line


def test_the_toggle_is_a_real_button_and_nothing_uses_a_title_tooltip():
    """开关是真 `<button>`（键盘可达、aria-expanded 可查），**不是** title 悬浮。

    WebView2 不弹 `title=`，把详情藏进悬浮提示等于没有。`_strip_body()` 取的是
    注释剥掉之后的那一段，所以产品注释里解释「为什么不用悬浮」不会被这条判红。
    """
    toggle = _toggle_node_line()
    assert 'type: "button"' in toggle, f"开关那行没有 type=button：{toggle}"
    strip = _strip_body()
    assert 'ui.h("button"' in strip, "开关不是 <button>（真点击 + 键盘可达的前提）"
    assert "aria-expanded" in strip, "开关没有 aria-expanded，展开态无从判定"
    for line in strip.splitlines():
        assert "title:" not in line, f"提示条那一段出现悬浮写法：{line}"
    assert _js_source().count(TOGGLE_CALL_SITES) >= 2, \
        "两个 paint 路径必须都走同一个渲染口，否则收起/展开只修一半"


\
# ── B 组：几何（chromium，被测目录走既有开关 WB_WEB_ROOT）─────────────────

#: 截图目录。`build/` 在 `.gitignore` 里，先例同 `test_settings_role_warning_ui.py`
#: 的 `ACCEPT_DIR = ROOT/"build"/"accept"`。要换地方用 T68_SHOT_DIR，不改代码。
SHOT_DIR = Path(os.environ.get("T68_SHOT_DIR")
                or (ROOT / "build" / "accept" / "wb_summary_error_t68"))


def _shot(page, name: str, element: str = "") -> str:
    """整页 + 可选元素截图。牙齿子进程写到 SHOT_DIR/_teeth 子目录。

    牙齿跑的是**退化副本**，它的画面不能和验收图混在同一个目录里，否则
    「九张图」里混进坏画面，肉眼验收会拿它当好画面看（本轮真发生过）。
    元素截图是给窄档准备的：360/375 下 `.sum-bar` 被 `.sum-grid`（app.css，
    本卡改动面外）挤到 92/107px，整页图里提示条只有一条细缝，看不出内容。
    """
    target = SHOT_DIR
    if os.environ.get("T68_TEETH_CHILD") == "1":
        target = SHOT_DIR / "_teeth"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    if element:
        page.locator(element).screenshot(path=str(target / f"{name}__element.png"))
    return str(path)


def _lengths(page, column: str) -> None:
    """把「哪一格是哪一形、那一形多长」打进 stdout —— 交付表里的数就来自这里。"""
    print(f"[t68] {page.viewport_size['width']}px 列长={len(column)} "
          f"码点前 12={column[:12]!r}")


@pytest.mark.parametrize("vw,vh,shape", _CELLS, ids=_CELL_IDS)
def test_the_strip_stays_inside_its_parent_with_no_clipped_content(strip_driver,
                                                                   vw, vh, shape):
    """三档视口 × 三个真失败形状，收起态与展开态各交四个数（九宫格 18 组读数）。

    四条判据：① 正文盒 rect.right ≤ 父（`.sum-bar`）rect.right；② 正文盒
    scrollWidth ≤ clientWidth；③ `document.documentElement.scrollWidth` 不超过
    「提示条隐藏时的基线」（360 档那 4px 是顶栏换肤控件 `#skin-picker` 的既有病，
    样式在 app.css —— 本卡越界清单里，不为此动它，也不在提示条上硬加没用的规则）；
    ④ 与 `#sum-note`、`#summary-host` 的相交面积 = 0。
    """
    page, column = strip_driver((vw, vh), shape)
    _lengths(page, column)
    base = _baseline_doc(page)
    closed = _geometry(page)
    assert not closed.get("missing"), closed
    assert closed["fails"] == [], closed
    assert closed["nodeRight"] <= closed["parentRight"], closed
    assert closed["nodeScrollWidth"] <= closed["nodeClientWidth"], closed
    assert closed["overlapNote"] == 0 and closed["overlapHost"] == 0, closed
    assert closed["docOverBaseline"] <= 0, (base, closed)
    assert closed["nodeInViewport"], (closed["nodeLeft"], closed["nodeRight"], closed["innerWidth"])
    assert closed["overflowWrap"] == "anywhere", closed
    assert _shown_text(page) == column, (len(_shown_text(page)), len(column))
    _shot(page, f"{shape}_{vw}_collapsed", f"#{BAR_ID}")

    if shape == "cbig600":
        assert len(column) == ERROR_MESSAGE_MAX_CHARS, len(column)
    toggle = _toggle(page)
    if toggle.get("hidden") or toggle.get("missing"):
        # 一行放得下的那一形没有可展开的东西：四个数已交完，短形状另有专用用例。
        assert shape == "net62", (shape, closed["nodeHeight"], toggle)
        page.close()
        return
    _force_open(page)
    opened = _geometry(page)
    assert opened["fails"] == [], opened
    assert opened["nodeRight"] <= opened["parentRight"], opened
    assert opened["nodeScrollWidth"] <= opened["nodeClientWidth"], opened
    assert opened["overlapNote"] == 0 and opened["overlapHost"] == 0, opened
    assert opened["docOverBaseline"] <= 0, (base, opened)
    assert opened["nodeInViewport"], (opened["nodeLeft"], opened["nodeRight"], opened["innerWidth"])
    assert _shown_text(page) == column, len(_shown_text(page))
    _shot(page, f"{shape}_{vw}_expanded", f"#{BAR_ID}")
    page.close()


@pytest.mark.parametrize("vw,vh", _VIEWPORTS, ids=_VP_IDS)
def test_the_toggle_is_reachable_and_reveals_the_whole_column(strip_driver, vw, vh):
    """展开必须是**真用户路径**：同快照三条件成立 → 真 click → 文案与 aria 翻转 →
    正文等于那一列全文（封顶行一个字不丢）→ 再点收回。三档都用 600 封顶形状。
    """
    page, column = strip_driver((vw, vh), "cbig600")
    _lengths(page, column)
    assert len(column) == ERROR_MESSAGE_MAX_CHARS, \
        f"这一格的列应当正好封顶 {ERROR_MESSAGE_MAX_CHARS}，实测 {len(column)}"
    _baseline_doc(page)
    closed = _geometry(page)
    assert closed["fails"] == [], closed
    assert closed["nodeHeight"] < 120, \
        f"收起态应当只有一行，实测 {closed['nodeHeight']}px（line-clamp 没生效）"
    toggle = _toggle(page)
    assert not toggle.get("missing") and not toggle.get("hidden"), toggle
    assert toggle["tag"] == "button", toggle
    assert toggle["title"] is None, "开关上挂了悬浮提示（WebView2 里等于没有）"
    assert toggle["label"] == LABEL_OPEN, toggle
    assert toggle["expanded"] == "false", toggle
    assert toggle["height"] > 0, f"开关内容盒高 = 0：{toggle}"
    assert toggle["barTop"] <= toggle["top"] and toggle["bottom"] <= toggle["barBottom"] + 0.5, \
        f"开关不在提示条盒内：{toggle}"
    assert toggle["hitSelf"], f"elementFromPoint 没命中开关本身：{toggle}"

    page.click(f"#{MORE_ID}")
    opened = _geometry(page)
    assert opened["fails"] == [], opened
    assert opened["nodeHeight"] > closed["nodeHeight"], (closed, opened)
    after = _toggle(page)
    assert after["label"] == LABEL_CLOSE and after["expanded"] == "true", after
    assert after["hitSelf"], after
    assert _shown_text(page) == column, len(_shown_text(page))
    _shot(page, f"cbig600_{vw}_expanded_by_click", f"#{BAR_ID}")

    # 展开之后开关顶边可能贴着视口底（360x740 实测 moreTop=694/740，222 字那形实测
    # 740 整条出屏）。真用户会滚一下再点「收起」——这里就照真用户做，不用 sleep 糊。
    page.locator(f"#{MORE_ID}").scroll_into_view_if_needed()
    page.click(f"#{MORE_ID}")
    back = _geometry(page)
    assert abs(back["nodeHeight"] - closed["nodeHeight"]) < 1.0, (closed, back)
    assert _toggle(page)["label"] == LABEL_OPEN, "收回后开关文案没回到「展开」"
    page.close()


def _clip_truth(page):
    """独立量「正文真被裁」：临时摘掉 is-clamped，比摘前/摘后的 clientHeight。

    这里**不能**用 scrollWidth > clientWidth：line-clamp 的裁切是纵向的，而
    `overflow-wrap: anywhere` 已经把横向撑破治掉了（实测 sw == cw），横向判据
    恒为 False，拿它当对照会让用例走进「不该有开关」那一支——而产品给的开关
    是对的。本函数与产品自己的 `scrollHeight > clientHeight` 是两条不同的腿：
    它动 class，产品动的是自己那次读取，两边同时成立才算数。
    """
    return page.evaluate("""(arg) => {
      const body = document.getElementById(arg.bodyId);
      const more = document.getElementById(arg.moreId);
      if (!body) { return {missing: true}; }
      const clampedH = body.clientHeight;
      body.classList.remove('is-clamped');
      const freeH = body.clientHeight;
      body.classList.add('is-clamped');
      return {clampedH: clampedH, freeH: freeH,
              reallyClipped: freeH > clampedH + 1,
              toggleShown: !!more && getComputedStyle(more).display !== 'none',
              bodyWidth: Math.round(body.getBoundingClientRect().width)};
    }""", {"bodyId": BODY_ID, "moreId": MORE_ID})


def test_a_one_line_message_does_not_offer_a_toggle(strip_driver):
    """开关的出现条件 = 「正文真被裁」，与文本长短无关。

    同一条 8 字短列（真链路 `NETWORK` 且 `detail` 为空 ⇒ `error_message_for_row`
    逐字返回 message）在两档视口里对读，开关有没有必须等于 `_clip_truth()` 独立
    量出来的「摘掉 is-clamped 之后比摘前多高」：
      440 档 .sum-bar 有 172px，8 字一行放得下 ⇒ 没开关；
      360/375 档 .sum-bar 只有 92/107px（既有布局事实，见 SHOT 目录与交付），
      8 字被 line-clamp 裁成两行 ⇒ 开关必须出现。
    只测一档会被「压根没实现出现条件」那种写法糊过去（那种写法两档都绿）；
    只按文本长度当条件，也会在这里红——因为同一句话在窄档确实放不下。
    """
    page_wide, column_wide = strip_driver((440, 800), "short8")
    _lengths(page_wide, column_wide)
    assert column_wide == "连不上大模型服务", column_wide
    closed_wide = _geometry(page_wide)
    assert closed_wide["fails"] == [], closed_wide
    clip_wide = _clip_truth(page_wide)
    assert clip_wide["reallyClipped"] is False, clip_wide
    t_wide = _toggle(page_wide)
    assert t_wide.get("hidden") or t_wide.get("missing"), (clip_wide, t_wide)

    page_narrow, column_narrow = strip_driver((360, 740), "short8")
    _lengths(page_narrow, column_narrow)
    assert column_narrow == column_wide, "两档用了不同的列，对照不成立"
    closed_narrow = _geometry(page_narrow)
    assert closed_narrow["fails"] == [], closed_narrow
    clip_narrow = _clip_truth(page_narrow)
    assert clip_narrow["reallyClipped"] is True, clip_narrow
    t_narrow = _toggle(page_narrow)
    assert not (t_narrow.get("hidden") or t_narrow.get("missing")), (clip_narrow, t_narrow)
    assert closed_narrow["nodeHeight"] < 120, closed_narrow


# ── C 组：牙齿（%TEMP% 自带哨兵名的副本；仓库工作区一字节不动）────────────

TEETH_ROOT = Path(tempfile.gettempdir()) / "wb_t68_teeth"
SENTINEL = "task68_teeth_"

#: 常驻那 7 条：牙齿跑起来它们必须**始终绿**。
RESIDENT_TESTS = (
    "test_a_failure_with_detail_lands_the_detail_in_the_error_message_column",
    "test_a_failure_without_detail_stays_exactly_the_message",
    "test_the_joiner_is_a_single_owner_in_the_backend",
    "test_the_summary_page_reads_only_keys_that_exist_on_the_job_row",
    "test_the_summary_page_actually_uses_the_message_column",
    "test_the_page_does_not_rebuild_the_sentence_the_backend_already_joined",
    "test_the_scan_domain_switch_changes_the_file_not_the_verdict",
)

#: 每个变体**先声明期望红名单**（缺红与多红都算失败）。
#: expr 用 `-k` 圈定该变体该影响的用例；9 条几何只在 anywhere 变体里进名单，
#: 因为退化后三个视口 × 三列文本都应当撑破，逐格点名。
#: 牙齿选择集：**排除常驻 7 条与牙齿自身，其余全跑**。
#: 不逐变体圈小scope —— 圈小了就只能看见「我预期它会红的那几条」，
#: 看不见同一退化还打红了谁，多红就检测不出来（合同要求缺红与多红都算失败）。
#: 参数化格子的全名，期望红名单里用它拼 id —— 用例改名就会对不上，
#: 不会静默空跑（`_CELL_IDS` / `_VP_IDS` 由参数化自己现算）。
_GEOM_TEST = "test_the_strip_stays_inside_its_parent_with_no_clipped_content"
_REACH_TEST = "test_the_toggle_is_reachable_and_reveals_the_whole_column"


TEETH_EXPR = ("not a_failure_with_detail_lands and not a_failure_without_detail_stays"
              " and not joiner_is_a_single_owner and not reads_only_keys_that_exist"
              " and not actually_uses_the_message_column"
              " and not does_not_rebuild_the_sentence"
              " and not scan_domain_switch_changes and not burn_when")

#: 每个变体的期望红名单，逐字抄自现跑读数（`Temp/wb_t68/calib4_all.txt`，
#: 2026-11-05 20:39 起那一段；夹具改成每格独立 `mkdtemp` home 之后重测）。
#: 名单里凡是 `[{...}]` 的展开都由 `_CELL_IDS` / `_VP_IDS` 现算，
#: 参数化 id 一改就会对不上，不会静默空跑。
TEETH_EXPECT = {
    # -webkit-line-clamp: 1 -> 2：收起态不再是「一句」。静态那条红；
    # 窄档 8 字列不再被裁（实测 clampedH 39 == freeH 39）⇒ 「开关出现条件 =
    # 真被裁」的对照红。calib4 那轮还多红三档可达格，是夹具标题把整页推出
    # 视口造成的假红；短标题 + nodeInViewport 之后 calib5 实测只剩这 2 条。
    "clamp2": {"test_a_one_line_message_does_not_offer_a_toggle",
               "test_the_collapsed_strip_is_clamped_to_one_line_by_css"},
    # overflow-wrap: anywhere -> normal：不可断 ASCII 串重新撑破父矩形。
    # 静态横向锁 + 三档 × 三列九个几何格 + 三档可达格（实测 13 条）。
    "anywhere": {"test_the_strip_keeps_the_unbreakable_tokens_wrappable"}
                | {f"{_GEOM_TEST}[{i}]" for i in _CELL_IDS}
                | {f"{_REACH_TEST}[{i}]" for i in _VP_IDS},
    # 前端 text.split(" ")[0]：正文不再携带整列 = 自己造第二份截断规则。
    # 静态两条 + 夹具的对齐等待（body.textContent === 库里那一列）等不到
    # ⇒ 九个几何格与三个可达格一起红（实测 14 条，这一支最慢：每格等 30s）。
    "split": {"test_the_collapsed_strip_is_clamped_to_one_line_by_css",
              "test_the_strip_carries_the_whole_column_and_opens_it_verbatim"}
             | {f"{_GEOM_TEST}[{i}]" for i in _CELL_IDS}
             | {f"{_REACH_TEST}[{i}]" for i in _VP_IDS},
    # 提示条节点 div -> span：行内节点挂不住内滚 body 与开关。
    # 静态块容器那条。calib4 那轮的三档可达格红同样是标题造成的假红，
    # calib5 实测只剩 1 条 —— 行内节点在短标题下还点得着，
    # 所以拦住这退化的是块容器那条静态锁，不是可达格。
    "inline": {"test_the_error_bar_node_is_a_block_container"},
    # 开关挂 title=：悬浮写法在 WebView2 里等于没有，用户点不到也看不见。
    # 静态禁令 + 三档可达格（实测 4 条）。
    "title": {"test_the_toggle_is_a_real_button_and_nothing_uses_a_title_tooltip"}
             | {f"{_REACH_TEST}[{i}]" for i in _VP_IDS},
}


def _teeth_copy() -> Path:
    """整份 web 树复制进本次专属目录（目录名自带哨兵，仓库树不动）。

    必须连 `index.html` 一起拷：`_run_suite` 把 `WB_WEB_ROOT` 指到这个目录，
    浏览器夹具从它起静态服务。只拷 `js`+`css` 时 `/index.html` 404，
    页面里没有 `WB`，子进程 12 个浏览器格会在几秒内**无差别全红**
    （实测 `14 failed, 5 passed in 4.80s`，正常一跑 40s+）——
    那是取证管道断了，不是判据没牙，而且会把「多余的红」判成实现问题。
    """
    TEETH_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=SENTINEL, dir=str(TEETH_ROOT)))
    shutil.copytree(str(_js_dir().parent), str(work), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return work


def _patch_file(work: Path, rel: str, needle: str, replacement: str) -> None:
    """在副本上打一处退化；**没命中就直接红**。

    少了这句断言，「产品文件变了」会被读成「判据没牙」——两种都该红，但不能混。
    """
    target = work / rel
    data = target.read_text(encoding="utf-8")
    assert needle in data, f"退体化补丁没命中 {rel}：{needle!r}（产品文件变了，不是判据没牙）"
    target.write_text(data.replace(needle, replacement, 1), encoding="utf-8")


def _run_suite(work: Path, expr: str):
    """在改过的副本上重跑本文件那批用例，返回 (FAILED 集合, 输出, rc)。

    副本走 `WB_WEB_JS_DIR`（A 组的扫描域）与 `WB_WEB_ROOT`（B 组被测的静态目录），
    同一个退化面两侧都看得见。
    """
    env = dict(os.environ)
    env["WB_WEB_JS_DIR"] = str(work / "js")
    env["WB_WEB_ROOT"] = str(work)
    env["T68_TEETH_CHILD"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-q", "-rf", "-p", "no:randomly",
         "--tb=line", "-k", expr],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=900,
        # 这台机器的控制台编码是 GBK：不带 encoding 时子进程 stdout 会在读取线程里
        # 抛 UnicodeDecodeError，proc.stdout 直接变 None（实测 TypeError: expected
        # string or bytes-like object, got 'NoneType'）。取证管道断了不等于判据没牙。
        encoding="utf-8", errors="replace")
    failed = set(re.findall(r"^FAILED \S+::(\S+)", proc.stdout, flags=re.M))
    return failed, proc.stdout + proc.stderr, proc.returncode


@pytest.mark.parametrize("variant", sorted(TEETH_EXPECT))
def test_the_strip_checks_burn_when_the_fix_is_removed(variant):
    """把实现退化成事故现场，重跑，逐变体对期望红名单。

    副本在 `%TEMP%` 下的 `wb_t68_teeth` 目录（目录名自带 `task68_teeth_` 前缀）。常驻那 7 条不在任何期望名单里，
    一旦变红就落在「多余的红」这一支；子进程退出码 ∉ (0,1) 单独打出来，
    不能读成「没红」（AGENTS：collection 失败 rc=2 不是没红）。
    """
    if os.environ.get("T68_TEETH_CHILD") == "1":
        pytest.fail("牙齿子进程里又选中了牙齿 —— 递归，立刻停")
    expected = TEETH_EXPECT[variant]
    work = _teeth_copy()
    try:
        if variant == "clamp2":
            _patch_file(work, "css/summary.css", "-webkit-line-clamp: 1;",
                        "-webkit-line-clamp: 2;")
        elif variant == "anywhere":
            _patch_file(work, "css/summary.css", "overflow-wrap: anywhere;",
                        "overflow-wrap: normal;")
        elif variant == "split":
            _patch_file(work, "js/pages/summary.js",
                        'id: "summary-error-body", text: text',
                        'id: "summary-error-body", text: text.split(" ")[0]')
        elif variant == "inline":
            _patch_file(work, "js/pages/summary.js",
                        'ui.h("div", { class: "callout callout--warn", id: "summary-error"',
                        'ui.h("span", { class: "callout callout--warn", id: "summary-error"')
        elif variant == "title":
            _patch_file(work, "js/pages/summary.js", 'class: "callout__more"',
                        'class: "callout__more", title: text')
        else:
            pytest.fail(f"未知变体 {variant}")
        failed, dump, rc = _run_suite(work, TEETH_EXPR)
        if rc not in (0, 1):
            pytest.fail(f"子进程退出码 {rc} 不在 (0,1) —— 收集/内部错，不能读成「没红」：\n{dump[-1500:]}")
        hit_resident = [name for name in RESIDENT_TESTS if name in failed]
        assert not hit_resident, f"常驻用例被牙齿打红了：{hit_resident}\n{dump[-1200:]}"
        assert failed, f"{variant}：一个都没红 = 判据没牙（或补丁没打到东西）\n{dump[-1500:]}"
        missing, extra = expected - failed, failed - expected
        assert not missing, f"{variant}：期望变红但没有：{sorted(missing)}\n{dump[-1500:]}"
        assert not extra, f"{variant}：多余的红：{sorted(extra)}\n{dump[-1500:]}"
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
