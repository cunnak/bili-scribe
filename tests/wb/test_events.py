"""事件总线：节流 4/s、日志攒批 200ms、绝不丢最后一条。

这些测试都在验"界面为什么会卡"的反面：

* 进度一秒来几十次，推送端不该跟着来几十次（节流）；
* 被压制的最后一次必须补发——否则进度条永远停在 97%；
* 日志攒批但**一行都不能丢**；
* 关窗时攒着的日志要发掉（最后几行往往正是失败原因）。
"""

from __future__ import annotations

import json
import time

from video_workbench.app.events import EventBus


class Recorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def __call__(self, name: str, payload_json: str) -> None:
        self.rows.append((name, json.loads(payload_json)))

    def by(self, name: str) -> list[dict]:
        return [payload for event, payload in self.rows if event == name]


def _wait(seconds: float) -> None:
    time.sleep(seconds)


def test_progress_is_throttled_to_four_per_second() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02)
    rec = Recorder()
    bus.set_sink(rec)
    start = time.monotonic()
    for index in range(40):  # 40 次进度压进 40ms 内
        bus.emit("job.updated", {"id": "j1", "progress": index / 40}, key="j1")
        time.sleep(0.001)
    elapsed = time.monotonic() - start
    _wait(0.4)  # 等补发
    bus.close()

    delivered = rec.by("job.updated")
    assert delivered, "一条都没发"
    # 40ms 内最多 1 条；补发 1 条；总量远小于 40
    assert len(delivered) <= 2, f"节流失效：{len(delivered)} 条 / {elapsed:.3f}s"
    assert delivered[-1]["progress"] == 39 / 40, "最后一条没补发（进度会永远停在 97%）"


def test_throttle_keeps_latest_value_per_key() -> None:
    bus = EventBus(throttle_seconds=0.5, tick_seconds=0.02)
    rec = Recorder()
    bus.set_sink(rec)
    bus.emit("job.updated", {"id": "a", "progress": 0.1}, key="a")
    bus.emit("job.updated", {"id": "a", "progress": 0.5}, key="a")
    bus.emit("job.updated", {"id": "b", "progress": 0.2}, key="b")
    _wait(0.3)
    bus.close()
    # 不同 key 互不压制
    first = {payload["id"]: payload["progress"] for payload in rec.by("job.updated")}
    assert first.get("b") == 0.2


def test_emit_now_is_never_throttled() -> None:
    bus = EventBus(throttle_seconds=1.0, tick_seconds=0.02)
    rec = Recorder()
    bus.set_sink(rec)
    for index in range(5):
        bus.emit_now("job.finished", {"id": f"j{index}"})
    bus.close()
    assert len(rec.by("job.finished")) == 5


def test_logs_are_batched_within_window() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02, batch_seconds=0.2)
    rec = Recorder()
    bus.set_sink(rec)
    for line in range(30):
        bus.log("job_x", f"line {line}")
    assert rec.by("job.log") == [], "攒批窗口没到就发了"
    _wait(0.35)
    bus.close()

    events = rec.by("job.log")
    assert 1 <= len(events) <= 2, f"30 行日志发了 {len(events)} 条事件"
    flat = [line for payload in events for line in payload["lines"]]
    assert flat == [f"line {index}" for index in range(30)], "有行丢了或顺序乱了"


def test_log_batch_splits_across_windows() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02, batch_seconds=0.1)
    rec = Recorder()
    bus.set_sink(rec)
    bus.log("job_y", "first")
    _wait(0.18)
    bus.log("job_y", "second")
    _wait(0.18)
    bus.close()
    events = rec.by("job.log")
    assert len(events) >= 2, f"应该分成两批：{events}"
    flat = [line for payload in events for line in payload["lines"]]
    assert flat == ["first", "second"]


def test_flush_logs_is_immediate() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02, batch_seconds=5.0)
    rec = Recorder()
    bus.set_sink(rec)
    bus.log("job_z", "urgent")
    assert rec.by("job.log") == []
    bus.flush_logs()
    assert [line for payload in rec.by("job.log") for line in payload["lines"]] == ["urgent"]
    bus.close()


def test_close_flushes_pending_logs() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02, batch_seconds=5.0)
    rec = Recorder()
    bus.set_sink(rec)
    bus.log("job_w", "last words")
    bus.close()

    flat = [line for payload in rec.by("job.log") for line in payload["lines"]]
    assert flat == ["last words"], "关窗把没发的日志吞了"


def test_log_ignores_empty_lines() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02, batch_seconds=0.05)
    rec = Recorder()
    bus.set_sink(rec)
    bus.log("job_v", "")
    _wait(0.12)
    bus.close()
    assert rec.by("job.log") == []


def test_sink_errors_do_not_escape() -> None:
    bus = EventBus(throttle_seconds=0.25, tick_seconds=0.02, batch_seconds=0.05)

    def broken(name: str, payload: str) -> None:
        raise RuntimeError("窗口已经关了")

    bus.set_sink(broken)
    bus.log("job_u", "x")
    bus.emit_now("job.finished", {"id": "job_u"})
    _wait(0.1)
    bus.close()  # 不抛就算过


def test_failed_delivery_is_retried_then_logged(caplog):
    """投递失败要重试一次，仍然失败就留日志——静默吞掉让我们查了一下午。"""
    import logging

    bus = EventBus()
    seen: list[str] = []

    def flaky(name: str, body: str) -> None:
        seen.append(name)
        raise RuntimeError("窗口正忙")

    bus.set_sink(flaky)
    with caplog.at_level(logging.WARNING, logger="video_workbench.app.events"):
        bus.emit_now("job.finished", {"id": "job_x", "status": "succeeded"})
    bus.close()

    assert len(seen) == 2, "投递失败应该重试一次，实际尝试 %d 次" % len(seen)
    assert "事件没有送到界面" in caplog.text
    assert "job.finished" in caplog.text


def test_delivery_success_does_not_retry():
    bus = EventBus()
    seen: list[str] = []
    bus.set_sink(lambda name, body: seen.append(name))
    bus.emit_now("job.finished", {"id": "job_y"})
    bus.close()
    assert seen == ["job.finished"]
