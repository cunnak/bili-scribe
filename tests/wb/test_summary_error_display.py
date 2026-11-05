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

import os
import re
import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.events import EventBus
from video_workbench.app.jobs import JobManager, error_message_for_row
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
