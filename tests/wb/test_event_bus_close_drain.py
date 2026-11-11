"""事件总线关窗时的排空契约（E1–E5）。

**这条锁守的是什么。** 2026-11-01 那次「任务跑完了界面还停在 97%」：
`EventBus.emit()` 把节流窗口内的值留在 `_pending` 里，靠后台线程
`_loop` 在窗口过后补发；而 `close()` 一进来就置 `_closed`，`_loop`
见到它就退 —— 最后那条（往往正是 100%）永远送不到。同一族还有两个方向：
关窗之后事件既送不到界面、又不留任何痕迹（事后排查时日志一片干净），
以及反过来「每条丢弃都记一行」把关窗瞬间几十条事件刷成几百行日志。

于是五条契约（与实现方拿到的是同一份）：

* E1 `close()` 之后 `_pending` 必须空 —— 攒着的东西不许留着过夜。
* E2 被节流压住的那一条，必须**在 close() 期间**真的送到 sink。
* E3 关窗之后的事件送不出去，但**不能一条痕迹都不留**：至少一条 warning 点名事件。
* E4 但也不能逐条记：1000 条关窗后事件产生的日志行数必须有界。
* E5 `close()` 幂等：第二次调用不抛，也不把已经送到的事件再送一遍。

**为什么断言落在这一层。** E1/E5 盯的是 `_pending` 与「送了几次」这两个
契约里点名的量，不是内部实现细节；E2/E3/E4 全部走真实 `Sink` 与真实
`_LOGGER`（`video_workbench.app.events`），不给被守住的那一段上替身 ——
替身掉 sink 就等于把「送没送到」这件事本身换成「我调了一个函数」，
那样真实现不排空也照样绿。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from video_workbench.app.events import EventBus

LOGGER_NAME = "video_workbench.app.events"

# 节流窗口要足够长：这些用例里没有任何 sleep，全部在毫秒级跑完，
# 2 秒的窗口保证「第二条一定落在 _pending 里」，而不会被后台线程提前补发。
# 也不能短到让 `now - _last.get(token, 0.0)` 的初值（0.0）落进窗口内 ——
# 那样第一条就进 _pending 了，E2 的前置断言会假红。
THROTTLE = 2.0


class _RecordingSink:
    """记账用的 sink：只记「收到了什么」，不替身化任何投递逻辑。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, name: str, body: str) -> None:
        with self._lock:
            self.calls.append((name, body))

    @property
    def names(self) -> list[str]:
        with self._lock:
            return [name for name, _body in self.calls]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(body) for got, body in self.calls if got == name]


def _bus(**kwargs: Any) -> EventBus:
    # tick 调小是为了让后台线程在 close() 之后尽快退出，不拖慢整个套件；
    # batch 调大是为了让 log() 的行一定还攒在内存里，由 close() 去 flush。
    params: dict[str, Any] = {
        "throttle_seconds": THROTTLE,
        "tick_seconds": 0.01,
        "batch_seconds": 30.0,
    }
    params.update(kwargs)
    return EventBus(**params)


# ---- 第一层：节流攒下的最后一条不能丢（E1 / E2）


def test_the_throttled_tail_event_reaches_the_sink_when_the_window_closes() -> None:
    """E2：同一个 name+key 的第二次 emit 落在节流窗口内，close() 必须把它送到。"""
    bus = _bus()
    sink = _RecordingSink()
    bus.set_sink(sink)
    try:
        bus.emit("job.progress", {"id": "job_1", "percent": 10}, key="job_1")
        bus.emit("job.progress", {"id": "job_1", "percent": 97}, key="job_1")
        # 前置：第二条确实被节流压住了（只有第一条送到）。这一句红了说明
        # 用例根本没造出「攒着一条」的现场，后面的断言就无从谈起。
        assert sink.names == ["job.progress"]
        assert sink.payloads("job.progress")[0]["percent"] == 10

        bus.close()

        assert sink.names == ["job.progress", "job.progress"]
        assert sink.payloads("job.progress")[-1]["percent"] == 97
    finally:
        bus.close()


def test_close_leaves_nothing_pending_and_counts_nothing_as_dropped() -> None:
    """E1：close() 之后 _pending 空；排空是「送到了」，不是「记一次丢弃」。"""
    bus = _bus()
    sink = _RecordingSink()
    bus.set_sink(sink)
    try:
        bus.emit("job.progress", {"id": "job_1", "percent": 10}, key="job_1")
        bus.emit("job.progress", {"id": "job_1", "percent": 55}, key="job_1")
        bus.emit("job.progress", {"id": "job_1", "percent": 97}, key="job_1")
        assert bus._pending, "三条 emit 之后 _pending 不该是空的（用例没造出现场）"

        bus.close()

        assert bus._pending == {}
        # 排空走的是投递路径：sink 当时还在，所以一条都不该被记成丢弃。
        assert bus._dropped == 0
        assert len(sink.payloads("job.progress")) == 2
        assert sink.payloads("job.progress")[-1]["percent"] == 97
    finally:
        bus.close()


def test_the_lines_still_batched_in_memory_are_flushed_at_close() -> None:
    """同一族的日志腿：攒批窗口没到就关窗，那些行必须跟着 close() 出去。"""
    bus = _bus()
    sink = _RecordingSink()
    bus.set_sink(sink)
    try:
        bus.log("job_1", "第 1 行")
        bus.log("job_1", "第 2 行")
        assert sink.names == []  # 攒批窗口 30s，此刻还没发

        bus.close()

        assert sink.names == ["job.log"]
        assert sink.payloads("job.log")[0] == {"id": "job_1", "lines": ["第 1 行", "第 2 行"]}
    finally:
        bus.close()


# ---- 第二层：关窗之后丢弃要留痕，但不能刷屏（E3 / E4）


def test_an_event_emitted_after_close_is_dropped_with_one_warning(caplog: Any) -> None:
    """E3：close() 之后送不出去，但不许一声不响地消失。"""
    bus = _bus()
    bus.set_sink(_RecordingSink())
    bus.close()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        bus.emit_now("job.finished", {"id": "job_1", "status": "succeeded"})
        bus.toast("窗口已经关了")

    warnings = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert len(warnings) >= 1, "关窗后的事件丢得一声不响 —— 事后无法排查"
    assert any("job.finished" in r.getMessage() for r in warnings), (
        f"warning 没有点名是哪个事件：{[r.getMessage() for r in warnings]}"
    )
    assert bus._dropped == 2
    bus.close()


def test_a_thousand_events_after_close_do_not_grow_the_log_line_by_line(
    caplog: Any,
) -> None:
    """E4：条数继续累计，日志行数必须有界（逐条记会把关窗瞬间刷成几百行）。"""
    bus = _bus()
    bus.close()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for i in range(1000):
            bus.emit_now("job.progress", {"id": "job_1", "percent": i})

    warnings = [r for r in caplog.records if r.name == LOGGER_NAME]
    # 上界放宽到 5 而不是钉成 1：契约是「有界」，不是「恰好一条」。
    assert 1 <= len(warnings) <= 5, f"1000 条丢弃产生了 {len(warnings)} 行日志"
    # 但计数不能丢：条数是排查时唯一还剩下的信息。
    assert bus._dropped == 1000
    bus.close()


def test_a_bus_that_never_had_a_sink_leaves_no_trace(caplog: Any) -> None:
    """E3 的边界：还没装过 sink 的总线（CLI / 无界面跑）不算丢、不留痕。

    没有这一条，E3 会被写成「任何丢弃都 warning」，而无界面跑一次任务
    就能把界面日志撑起来 —— 那是同一段注释（`_note_drop` 的 docstring）
    明确排除的形状。
    """
    bus = _bus()
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            bus.emit_now("job.progress", {"id": "job_1", "percent": 42})
        assert [r for r in caplog.records if r.name == LOGGER_NAME] == []
        assert bus._dropped == 1
    finally:
        bus.close()


# ---- 第三层：close() 幂等，且不许重复投递（E5）


def test_close_is_idempotent_and_does_not_deliver_twice() -> None:
    """E5：两次 close() 不抛，第二次也不把已经送到的事件再送一遍。"""
    bus = _bus()
    sink = _RecordingSink()
    bus.set_sink(sink)
    bus.emit_now("job.created", {"id": "job_1"})
    bus.close()
    delivered = len(sink.calls)
    assert delivered == 1

    bus.close()  # 不许抛

    assert len(sink.calls) == delivered


def test_a_sink_that_raises_does_not_break_the_caller_but_leaves_a_line(
    caplog: Any,
) -> None:
    """旁路契约：sink 抛异常不许冒到调用方（后台任务会跟着失败），但必须留一行。

    2026-11-01 那次事件丢失就是靠「事件没有送到界面」这条线索查到的；
    把它退化成静默 return，下一次同类故障就又只剩「界面不动」这一条现象。
    """

    def _broken(name: str, body: str) -> None:
        raise RuntimeError("evaluate_js: 窗口已销毁")

    bus = _bus()
    bus.set_sink(_broken)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            bus.emit_now("job.progress", {"id": "job_1", "percent": 97})
        warnings = [r for r in caplog.records if r.name == LOGGER_NAME]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        message = warnings[0].getMessage()
        assert "job.progress" in message
        assert "RuntimeError" in message
    finally:
        bus.close()
