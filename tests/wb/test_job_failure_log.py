"""失败/取消收尾那一行日志（`app/jobs.py` 的 `_finish` + 开关 `WB_JOB_FAILURE_LOG`）。

这张文件守的是 task-63 第 2 步落地的东西。第 1 步量到的现状（三条腿各走 `_finish` 恰好一次、
收尾写 0 行）写在卡正文里，这里不复述。三条设计约束都由第 1 步的读数决定，不是偏好：

1. **默认关**。开关一开就永久写盘，而 detail 唯一来源是 LLM 网关的响应体片段
   （`infra/llm.py` 的 `_error_for`：`snippet = (text or "").strip()[:400]`）——那是服务端可控文本。
   默认关让「新增永久写盘」这件事由人显式点一次。
2. **那一行只有一个句子 owner**：收尾行抄的是 `error_message_for_row` 已经算好的整句
   （`message —— _detail_tail(detail)`，再按 600 字符封顶）。库里那一列与前端（task-55）吃的
   也是同一句。在这儿重拼一遍就是第二个 owner，两处一分叉，界面与日志会给出两种原因。
3. **那一行恒一个物理行**：`_detail_tail` 把多行 traceback 收成最后一条非空行。
   第 1 步实测一次失败 ≈0.22 KB（真文案）到 ≈1.86 KB（detail 打满 600 字符、全中文），
   而 `prune(keep_days=30)` 只按文件 mtime 删整天文件、日内没有轮转上限，
   所以写多行等于把一个偶发事件变成每天几百 KB。

牙齿（lead 指定两条，都打在 %TEMP% 哨兵副本上）：摘掉开关判断（无条件写）⇒ 本文件第 1 条必红；
把收尾行绕过 `log_line` 直写 handler（不过 `redact`）⇒ 第 5 条必红。

本文件所有任务都用桩 handler，不发任何真请求：`calls=0`。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from video_workbench.app.events import EventBus
from video_workbench.app.jobs import JobManager, job_log_prefix
from video_workbench.infra import log as log_mod
from video_workbench.infra.db import open_and_migrate
from video_workbench.infra.repo import Repo

#: 哨兵都是自假值，不是真凭据；形状照 `infra/log.py` 里 `redact` 认的那几种来。
SENTINEL_KEY = "sk-LEAKTEST-ABCDEFGHIJKLMNOP"
SENTINEL_BEARER = "Bearer LEAKTEST-TOKEN-VALUE-0123456789"

SWITCH = "WB_JOB_FAILURE_LOG"


@pytest.fixture
def logged(tmp_path: Path, monkeypatch) -> Iterator[dict]:
    """真日志文件 + 真 `JobManager`，`logger` 就接产品自己那条腿（`log_line`）。

    结束把 `setup_logging` 挂上的 handler 摘掉：它挂在 `video_workbench` 这个**进程级** logger 上，
    不摘就带着本用例的临时目录活到整轮测试后面，别的用例写的日志会掉进一个已删除的文件里。
    """
    info = log_mod.setup_logging(tmp_path / "logs", console=False)
    logger = logging.getLogger(log_mod.LOG_NAME)
    before = (list(logger.handlers), logger.level, logger.propagate)
    monkeypatch.delenv(SWITCH, raising=False)

    conn = open_and_migrate(tmp_path / "wb.db")
    events = EventBus()
    manager = JobManager(
        Repo(conn),
        events,
        default_limit=1,
        tmp_root=tmp_path / "tmp",
        logger=log_mod.log_line,
    )
    try:
        yield {"manager": manager, "file": Path(info["file"]), "tmp": tmp_path}
    finally:
        manager.shutdown()
        conn.close()
        for handler in list(logger.handlers):
            if getattr(handler, "_wb_owned", False):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        logger.handlers, logger.level, logger.propagate = before


def _run_to_end(logged: dict, job_type: str, handler) -> str:
    """注册桩 handler、起任务、等终态，返回 job_id。"""
    manager = logged["manager"]
    manager.register(job_type, handler, limit=1)
    job = manager.submit(job_type, None, {})
    job_id = job["id"]
    deadline = time.time() + 12
    while time.time() < deadline:
        if manager.get(job_id)["status"] in ("failed", "canceled", "succeeded"):
            return job_id
        time.sleep(0.05)
    raise AssertionError(f"任务没在 12 秒内落到终态：{job_id}")


def _lines(logged: dict) -> list[str]:
    for handler in logging.getLogger(log_mod.LOG_NAME).handlers:
        handler.flush()
    return log_mod.tail_lines(logged["file"], 400)


def _closing(logged: dict) -> list[str]:
    return [ln for ln in _lines(logged) if "任务收尾" in ln]


# ---------------------------------------------------------------- 默认关


def test_the_switch_is_off_by_default_and_a_failure_writes_nothing(logged: dict, monkeypatch) -> None:
    """**默认关**：不设环境变量时，失败任务一行都不写。

    这条就是「摘掉开关判断（无条件写）」那颗牙齿咬的对象。
    handler **故意不调 `ctx.log`**：不然文件里本来就会多一行任务自己写的日志，
    「文件不增长」这条判据就变成在测 handler 的嘴，而不是测收尾那行。
    """
    monkeypatch.delenv(SWITCH, raising=False)
    before = len(_lines(logged))

    def handler(ctx, payload):
        raise RuntimeError("网关把这次请求打回来了")

    job_id = _run_to_end(logged, "t63.off", handler)
    assert logged["manager"].get(job_id)["status"] == "failed"
    assert _closing(logged) == [], "默认关却写了收尾行"
    assert len(_lines(logged)) == before, "默认关时日志文件不该增长"


def test_the_switch_accepts_only_1_true_on_and_ignores_junk(logged: dict, monkeypatch) -> None:
    """开关认 `1/true/on`（含大小写与空白），其余值一律当关。

    这条防的是把「设过没有」当成「开没开」：`WB_JOB_FAILURE_LOG=0` 必须是关。
    """
    for value, expect_on in (("1", True), ("TRUE", True), (" on ", True), ("0", False), ("false", False), ("", False)):
        monkeypatch.setenv(SWITCH, value)
        from video_workbench.app.jobs import failure_log_enabled

        assert failure_log_enabled() is expect_on, value


# ---------------------------------------------------------------- 开起来之后


def test_an_enabled_failure_writes_one_line_carrying_the_id_and_the_reason(
    logged: dict, monkeypatch
) -> None:
    """开关开 + 失败 ⇒ 日志里出现**恰好一行**，里面有 job_id、状态、错误码与原因片段。"""
    monkeypatch.setenv(SWITCH, "1")

    def handler(ctx, payload):
        ctx.log("开始拉字幕")
        raise RuntimeError("连不上那个地址，检查网络或 base_url。")

    job_id = _run_to_end(logged, "t63.on", handler)
    closing = _closing(logged)
    assert len(closing) == 1, f"收尾行应当恰好一条，实际 {len(closing)} 条：{closing}"
    line = closing[0]
    assert job_id in line, f"收尾行没有 job_id，grep 不到是哪个任务：{line}"
    assert "failed" in line, line
    assert "INTERNAL" in line, f"错误码没进收尾行：{line}"
    assert "连不上那个地址" in line, f"原因片段没进收尾行：{line}"


def test_a_multi_line_traceback_detail_still_lands_as_one_physical_line(
    logged: dict, monkeypatch
) -> None:
    """多行那种（`traceback.format_exc()`）也必须只占**一个物理行**。

    这条钉「不许把原始 traceback 整段塞进 `log_line`」：收尾行抄的是 `error_message_for_row`，
    它内部已经过 `_detail_tail`（最后一条非空行）。第 1 步我那个「+6 物理行」的对照实验
    就是绕过 `_detail_tail` 自己拼原文才得到的，那种写法不收。
    """
    monkeypatch.setenv(SWITCH, "true")

    def handler(ctx, payload):
        def inner():
            raise ValueError("这里炸在最深一层")

        inner()

    job_id = _run_to_end(logged, "t63.multiline", handler)
    closing = _closing(logged)
    assert len(closing) == 1, closing
    assert job_id in closing[0]
    assert "ValueError: 这里炸在最深一层" in closing[0], closing[0]
    body = _lines(logged)
    # traceback 的中间那些行不许单独成行：整段只该有那一条收尾
    assert not [ln for ln in body if ln.lstrip().startswith('File "')], "traceback 中间行单独落盘了"
    assert not [ln for ln in body if ln.startswith("Traceback (most recent call")], "原始 traceback 头落盘了"


def test_a_success_writes_nothing_even_with_the_switch_on(logged: dict, monkeypatch) -> None:
    """成功任务一行都不写 —— 开关只管失败/取消，不是给每个任务加一行噪声。

    同上一条的理由，handler 不调 `ctx.log`：这一条要量的是「收尾那一行有没有出现」，
    不是「任务自己有没有写日志」。
    """
    monkeypatch.setenv(SWITCH, "on")
    before = len(_lines(logged))

    def handler(ctx, payload):
        return {"ok": True}

    job_id = _run_to_end(logged, "t63.ok", handler)
    assert logged["manager"].get(job_id)["status"] == "succeeded"
    assert _closing(logged) == []
    assert len(_lines(logged)) == before, "成功任务不该让日志增长"


def test_the_written_line_is_redacted(logged: dict, monkeypatch) -> None:
    """**硬判据**：写进文件的那一行必须已经过 `redact`。

    这条是「绕过 `log_line` 直写 handler」那颗牙齿咬的对象。哨兵放在异常消息里，经 `INTERNAL`
    那条腿进 detail、再进收尾行；实现若改成直接对 handler 发包裹，明文就会出现在文件里。
    """
    monkeypatch.setenv(SWITCH, "1")

    def handler(ctx, payload):
        raise RuntimeError(f"上游说：api_key={SENTINEL_KEY} 以及 {SENTINEL_BEARER}")

    job_id = _run_to_end(logged, "t63.redact", handler)
    closing = _closing(logged)
    assert len(closing) == 1, closing
    text = "\n".join(_lines(logged))
    assert SENTINEL_KEY not in text, f"凭据原值落进了日志文件：{closing[0]}"
    assert "LEAKTEST-TOKEN-VALUE" not in text, f"Bearer 原值落进了日志文件：{closing[0]}"
    assert "***" in closing[0], f"收尾行里看不到打码痕迹：{closing[0]}"
    assert job_id in closing[0]


def test_the_cancelling_path_is_logged_too(logged: dict, monkeypatch) -> None:
    """取消也写：现状是 canceled 那条腿连一句「被取消」都没有（第 1 步实测收尾 0 行）。

    这里走真取消：`manager.cancel()` 置位，handler 自己轮询 `ctx.should_cancel()` 再抛
    `WBError("CANCELED", …)` —— `_run` 里那条 `status = "canceled" if exc.code == "CANCELED"`
    就是它的落点。不这么写的话任务会一路跑成功，测不到取消这条腿。
    """
    monkeypatch.setenv(SWITCH, "1")

    def handler(ctx, payload):
        for _ in range(400):
            if ctx.should_cancel():
                from video_workbench.app.errors import WBError

                raise WBError("CANCELED", "任务已取消")
            time.sleep(0.01)
        return {"ok": True}

    manager = logged["manager"]
    manager.register("t63.cancel", handler, limit=1)
    job = manager.submit("t63.cancel", None, {})
    job_id = job["id"]
    time.sleep(0.3)
    manager.cancel(job_id)
    deadline = time.time() + 12
    status = ""
    while time.time() < deadline:
        status = manager.get(job_id)["status"]
        if status in ("canceled", "failed", "succeeded"):
            break
        time.sleep(0.05)
    assert status == "canceled", f"没走成取消这条腿：{status}"
    closing = _closing(logged)
    assert len(closing) == 1, closing
    assert "canceled" in closing[0], closing[0]
    assert job_id in closing[0], closing[0]


# ---------------------------------------------------------------- 单一 owner


def test_the_job_id_prefix_has_a_single_owner(logged: dict, monkeypatch) -> None:
    """`[job_id] ` 这个形状只有一个出处：`job_log_prefix`。

    行为锁（不是值相等锁）：收尾那行必须**由 `job_log_prefix` 现取**，并且同一行里这个前缀
    只出现一次（`log_line` 的名字前缀不带 id，谁要是把已带 `[job_]` 的串再拼一次，这里就红）。
    手打一遍 `f"[{job_id}] …"` 的那种分叉由**下一条** `test_the_prefix_switch_moves_both_lines_together`
    拦：它把 owner 换成 `#id=` 形状，手打的那行不会跟着变 ⇒ 那条红。（牙齿实测：V5 变体只红那一条，
    本条在 V5 下仍绿——因为手打出来的串与 owner 的返回值逐字相同，本条的值锁抓不到它。别把「立刻红」
    写在本条注释里。）
    """
    monkeypatch.setenv(SWITCH, "1")
    seen: list[str] = []

    def spy(message: str) -> None:
        seen.append(str(message))
        log_mod.log_line(message)

    logged["manager"]._logger = spy

    def handler(ctx, payload):
        ctx.log("中途一行")
        raise RuntimeError("故意失败")

    job_id = _run_to_end(logged, "t63.owner", handler)
    body_lines = [s for s in seen if "中途一行" in s]
    closing_lines = [s for s in seen if "任务收尾" in s]
    assert body_lines and closing_lines, seen
    expected = job_log_prefix(job_id)
    assert body_lines[0].startswith(expected), body_lines[0]
    assert closing_lines[0].startswith(expected), closing_lines[0]
    # 前缀只有一个出处 ⇒ 同一行里不可能出现两遍
    assert closing_lines[0].count(expected) == 1, closing_lines[0]


def test_the_prefix_switch_moves_both_lines_together(logged: dict, monkeypatch) -> None:
    """上一条评论版的「换掉 owner」那一半：patch `jobs.job_log_prefix` 之后两行都跟着换形状。

    收尾行与 `_log` 都从这一个函数取前缀；把它替成 `#id=` 形状，两边都得是新形状。
    """
    monkeypatch.setenv(SWITCH, "1")
    seen: list[str] = []

    def spy(message: str) -> None:
        seen.append(str(message))

    logged["manager"]._logger = spy
    monkeypatch.setattr("video_workbench.app.jobs.job_log_prefix", lambda job_id: f"#{job_id}=")

    def handler(ctx, payload):
        ctx.log("中途一行二")
        raise RuntimeError("故意失败")

    _run_to_end(logged, "t63.prefix2", handler)
    assert any(s.startswith("#") and "中途一行二" in s for s in seen), seen
    assert any(s.startswith("#") and "任务收尾" in s for s in seen), seen
