"""事件推送。

**为什么需要节流层。** 下载任务每秒能报几十次进度，而 pywebview 的
``evaluate_js`` 每次都要序列化一次参数、过一遍 JS 引擎。不节流的话，
界面线程的时间几乎全花在渲染"38.1% → 38.4%"上，真正的重绘反而卡住。
方案书要求"每个 job 的进度事件节流到 4 次/秒"，这里是唯一实现点。

**节流不等于丢最后一条。** 只做"太快就丢弃"会让进度条永远停在 97%：
最后那一条恰恰是最关键的。所以被压制的值留在 ``_pending`` 里，
由后台线程在窗口过后补发最新值。

**日志是另一回事：攒批。** 一个下载任务能在一秒里吐几十行
``stdout`` 摘要，逐行推等于"每行都过一次 JS 引擎"。方案书要求"日志批量 200ms"，
所以 ``log()`` 把行先攒在内存里，200ms 到点合成一条 ``job.log`` 事件
（``lines`` 数组）发出去。攒批**不丢行**：窗口到点全发，关窗时 flush。

**永不抛异常。** 事件是旁路：推送失败（窗口已关、JS 报错）不该让
正在跑的任务跟着失败。所有 sink 调用都用 try/except 包住。但"不抛"不等于
"不留痕"：关窗（``close()``）之后事件只能丢掉，第一条丢弃记一条 warning，之后只
累加计数（逐条记会把日志刷爆——关窗后每个事件都会走这条路）。还没装 sink 的总线
（CLI / 无界面跑 / 测试）事件本来就没人要，那不算丢，不留痕。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

Sink = Callable[[str, str], None]

_LOGGER = logging.getLogger(__name__)


class EventBus:
    """线程安全的事件总线。"""

    def __init__(
        self,
        throttle_seconds: float = 0.25,
        tick_seconds: float = 0.05,
        batch_seconds: float = 0.2,
    ) -> None:
        # 0.25s ≈ 4 次/秒，与方案书一致
        self._throttle = throttle_seconds
        self._tick = tick_seconds
        # 200ms 攒批窗口，与方案书"日志批量 200ms"一致
        self._batch = batch_seconds
        self._logs: dict[str, list[str]] = {}
        self._log_due: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sink: Sink | None = None
        self._last: dict[str, float] = {}
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}
        # 关窗后没有 sink 时事件只能丢；记一条 warning 和累计丢弃数，不逐条刷屏
        self._drop_warned = False
        self._dropped = 0
        self._closed = False
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- 装配

    def set_sink(self, sink: Sink | None) -> None:
        self._sink = sink
        if sink is not None:
            self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="wb-events", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._closed:
            time.sleep(self._tick)
            self._flush_due()
            self._flush_logs()

    def _flush_due(self) -> None:
        now = time.monotonic()
        due: list[tuple[str, str, dict[str, Any]]] = []
        with self._lock:
            for key in list(self._pending):
                name, payload = self._pending[key]
                if now - self._last.get(key, 0.0) >= self._throttle:
                    due.append((key, name, payload))
                    del self._pending[key]
                    self._last[key] = now
        for _key, name, payload in due:
            self._deliver(name, payload)

    # ---------------------------------------------------------- 发送

    def _note_drop(self, name: str) -> None:
        """事件没人接时只能丢，但**不能连一条痕迹都不留**。

        只在关窗之后（``close()`` 把 ``_closed`` 置真）记：那时每个事件都会走到这里，
        逐条记会把日志刷爆（一次任务收尾几十条），所以只记第一条并说明后续不再逐条记，
        条数继续累计在 ``_dropped`` 里。
        还没装 sink 的总线（CLI / 无界面跑 / 测试）事件本来就没人要，那不算丢、不留痕
        ——否则无界面跑一次任务就能把界面日志撑起来。
        """
        with self._lock:
            self._dropped += 1
            first = self._closed and not self._drop_warned
            if first:
                self._drop_warned = True
        if first:
            _LOGGER.warning("事件没有 sink 可送，丢弃: %s（之后只累计条数，不再逐条记）", name)

    def _deliver(self, name: str, payload: dict[str, Any]) -> None:
        sink = self._sink
        if sink is None:
            self._note_drop(name)
            return
        body = json.dumps(payload, ensure_ascii=False)
        for attempt in (1, 2):
            try:
                sink(name, body)
                return
            except Exception as exc:  # noqa: BLE001
                # 窗口关掉之后 evaluate_js 会抛，这不该影响后台任务；但**不能连日志都不留**：
                # 2026-11-01 那次「任务跑完了界面还停在 97%」就是靠这条线索才查到事件丢了。
                if attempt == 2:
                    _LOGGER.warning("事件没有送到界面: %s (%s: %s)", name, type(exc).__name__, exc)
                    return
                time.sleep(0.05)

    def emit_now(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """不节流，立即发。用于状态跃迁（创建/完成/失败）——那些一条都不能少。"""
        self._deliver(name, payload or {})

    def emit(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> None:
        """节流发。``key`` 相同的连续事件会被合并成最新的一条。"""
        body = payload or {}
        if key is None:
            self.emit_now(name, body)
            return
        token = f"{name}:{key}"
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(token, 0.0) < self._throttle:
                self._pending[token] = (name, body)
                return
            self._last[token] = now
        self._deliver(name, body)

    def log(self, job_id: str, line: str) -> None:
        """攒一行任务日志。窗口到点（默认 200ms）合成一条事件发出去。

        **不丢行**：同一窗口里的行按到达顺序拼进 ``lines`` 数组；窗口到了就发。
        这样一次下载任务每秒几十行 stderr，只过一个事件、一次序列化。
        """
        text = str(line)
        if not text:
            return
        now = time.monotonic()
        with self._lock:
            self._logs.setdefault(job_id, []).append(text)
            self._log_due.setdefault(job_id, now + self._batch)
            due = now >= self._log_due[job_id]
            if due:
                lines = self._logs.pop(job_id, [])
                self._log_due.pop(job_id, None)
        if due and lines:
            self._deliver("job.log", {"id": job_id, "lines": lines})

    def _flush_logs(self) -> None:
        now = time.monotonic()
        ready: list[tuple[str, list[str]]] = []
        with self._lock:
            for job_id in list(self._log_due):
                if now < self._log_due[job_id]:
                    continue
                lines = self._logs.pop(job_id, [])
                self._log_due.pop(job_id, None)
                if lines:
                    ready.append((job_id, lines))
        for job_id, lines in ready:
            self._deliver("job.log", {"id": job_id, "lines": lines})

    def flush_logs(self) -> None:
        """立刻把攒着的日志发出去（任务收尾、关窗时用）。"""
        with self._lock:
            keys = list(self._logs)
            del_keys: dict[str, list[str]] = {}
            for job_id in keys:
                lines = self._logs.pop(job_id, [])
                self._log_due.pop(job_id, None)
                if lines:
                    del_keys[job_id] = lines
        for job_id, lines in del_keys.items():
            self._deliver("job.log", {"id": job_id, "lines": lines})

    # ---------------------------------------------------------- 便捷

    def toast(self, message: str, level: str = "info") -> None:
        """全局提示。``level``: info | success | warning | error。"""
        self.emit_now("app.toast", {"level": level, "message": message})

    def close(self) -> None:
        # 关之前把攒着的日志发掉：最后那几十行往往正是失败原因
        try:
            self.flush_logs()
        except Exception:
            pass
        # 再把节流攒下的最后一批事件送出去：_flush_due 只在 _loop 里跑，
        # 而 _loop 见到 _closed 就退，所以不在这里排空，最后那条永远送不到
        # （2026-11-01「任务跑完了界面还停在 97%」就是这么丢的）。
        # 关窗时不再节流：攒着的每一条都要送到。
        try:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for name, payload in pending:
                self._deliver(name, payload)
        except Exception:
            pass
        self._closed = True
        self._sink = None
