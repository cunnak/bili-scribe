"""任务队列：提交、进度、取消、重试（方案书 5.4.1 / 6.3）。

**为什么用线程池而不是 asyncio。** 真正的重活全在外部进程里（yt-dlp、ffmpeg、
faster-whisper），我们做的只是"起进程 + 读 stderr + 汇报进度"——阻塞的 ``readline``
比事件循环更直白。用 asyncio 反而要靠 ``run_in_executor`` 绕一圈回来。

**并发上限按类型分，不按全局。** 下载可以 2 路并行（网络 IO），
转录必须串行（显存只有一份）。用一个全局池会让"两路下载 + 一路转录"
互相排队，明明互不争用。

**取消是协作式的。** 这里只置一个 ``Event``，由 handler 在合适的检查点自己退出。
强行 kill 线程在 Python 里做不到（也没有线程能被安全强杀），
而 ``Popen.terminate()`` 由 handler 负责——它才知道自己起了哪些子进程。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable

from video_workbench.app.errors import WBError
from video_workbench.app.events import EventBus
from video_workbench.infra.db import now_iso
from video_workbench.infra.repo import Repo, RepoClosed, new_id

Handler = Callable[["JobContext", dict[str, Any]], Any]

_LOGGER = logging.getLogger(__name__)

#: 写进 ``wb_job.error_message`` 那一列的文本怎么拼。**只有一个出处**。
#:
#: 为什么要拼在这一列：``wb_job`` 只有 ``error_code`` / ``error_message`` 两格，
#: 没有 detail 格（`pragma table_info(wb_job)` 现读），而 detail 里恰恰放着唯一能自修
#: 的那句原文（``finish_reason=length，思考用了 16384 tokens``）。以前只存 message ⇒
#: 总结页读错键、其他页只看见一句「生成失败」，用户连原因都拿不到。拼进这一列
#: = 零 schema 迁移、零事件订阅（``job.finished`` 那条不算：后台跑完时页面可能没开着）。
ERROR_DETAIL_JOINER = " —— "
ERROR_MESSAGE_MAX_CHARS = 600

#: 失败/取消收尾那一刻要不要往日志文件补一行「为什么」。
#:
#: **默认关**。第 1 步量到的事实（都带命令，写在交付里）决定了这个默认值：
#: `error_message` 那一列**今天已经在库里、界面上已经逐字显示它**，而那一列**不过 `redact`**
#: —— 直接把库文件按字节扫一遍，`sk-` 开头的哨兵原文命中、`***` 不命中。所以「接」并不新增
#: 明文落盘的性质，但也不换来任何新东西：多出来的一份只是同一句话的第二次抄写，
#: 只是这一份反倒过了码。要不要在日志里留一份打过码的收尾原因，是产品决定，
#: 现在由这个开关决定；关掉 = 现状，一个字都不写。
#:
#: 开关每次调用现读环境变量，不缓存到 import（`_parity_strict` 同族理由）。
FAILURE_LOG_ENV = "WB_JOB_FAILURE_LOG"


def failure_log_enabled() -> bool:
    """`WB_JOB_FAILURE_LOG` 设成 1/true/on 才写那一行；没设 = 关。"""
    return str(os.environ.get(FAILURE_LOG_ENV, "")).strip().lower() in {"1", "true", "on"}


def job_log_prefix(job_id: str) -> str:
    """一行任务日志的头。**只有一个出处**，`_log` 与 `_finish` 都用它。

    为什么单拎出来：`log_line` 的前缀是 `%(name)s`（打出来是 `video_workbench.job`），
    **不带 job_id**；id 是 `_log` 那句 `f"[{job_id}] {text}"` 自己拼的。收尾那行要是再手打
    一遍同样的字面量，就等于给「一行任务日志长什么样」立了第二个 owner——两处格式一旦分叉，
    按 `[job_` grep 日志的人只会拿到两种形状。所以这里放一个函数，两处调它，
    想改形状只能改这一处。
    """
    return f"[{job_id}] "


def _detail_tail(detail: Any) -> str:
    """把 detail 收成一行。

    多行的那种是 ``traceback.format_exc()``（``INTERNAL`` 那条），整段留在这里会把
    主文案撑成一屏；栈的最后一条非空行才是「炸在哪」，正文日志里另有全文明细。
    单行的（LLM 那几条都是）原样回去，一个字不改——那句是要给人照着改配置的。
    """
    text = str(detail or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if len(lines) > 1 else text


def error_message_for_row(error: dict[str, Any] | None) -> str | None:
    """``to_payload()`` 里的 message + detail 收成落库那一句。

    detail 为空时**逐字返回 message**——界面上那八处 ``job.error_message`` 的消费者
    （clips/merge/subtitles/cli/batch）都按这一句显示，取消、恢复这类没有 detail 的
    消息不该被拼出一个尾缀分隔符。
    """
    message = str((error or {}).get("message") or "")
    detail = _detail_tail((error or {}).get("detail"))
    if not detail:
        return message or None
    if not message:
        return detail[:ERROR_MESSAGE_MAX_CHARS]
    room = ERROR_MESSAGE_MAX_CHARS - len(message) - len(ERROR_DETAIL_JOINER)
    if room <= 0:
        return message[:ERROR_MESSAGE_MAX_CHARS]
    return message + ERROR_DETAIL_JOINER + detail[:room]


class JobContext:
    """handler 能看到的一切。刻意只暴露这四件事：

    ``progress`` / ``log`` / ``should_cancel`` / ``payload``。

    不把 Repo、EventBus 递进去，是为了让 handler 无法绕过任务状态机
    直接改自己的状态——那样重试和取消的语义会被 handler 悄悄破坏。
    """

    def __init__(
        self,
        manager: "JobManager",
        job_id: str,
        job_type: str,
        project_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.job_id = job_id
        self.job_type = job_type
        self.project_id = project_id
        self.payload = payload
        self._manager = manager

    def progress(
        self,
        value: float | None = None,
        stage: str | None = None,
        speed: str | None = None,
        eta: str | None = None,
    ) -> None:
        """``value`` 是 0–1 的比例。传 None 表示"只更新阶段文案"。"""
        self._manager._report(self.job_id, value, stage, speed, eta)

    def log(self, line: str) -> None:
        self._manager._log(self.job_id, line)

    def should_cancel(self) -> bool:
        return self._manager._is_canceled(self.job_id)

    def check_cancel(self) -> None:
        """在检查点调用；已取消就抛 CANCELED 让任务正常收尾。"""
        if self.should_cancel():
            raise WBError("CANCELED", "任务已取消")

    # -- 子进程登记 ---------------------------------------------------------
    #
    # handler 把刚起的 Popen 交给这里。取消时由 JobManager 杀整棵进程树：
    # 有些 handler 卡在 `readline`（等 stderr 最后一行）上，光靠检查点根本
    # 走不到——只有从外面把进程杀掉，那条 readline 才会返回。

    def register_process(self, proc: Any) -> None:
        self._manager._register_process(self.job_id, proc)

    def guard_kwargs(self) -> dict[str, Any]:
        """直接扔给 `FFmpeg.run/export_clip/merge_copy` 的两个关键字。"""
        return {"should_cancel": self.should_cancel, "on_proc": self.register_process}


class JobManager:
    def __init__(
        self,
        repo: Repo,
        events: EventBus,
        limits: dict[str, int] | None = None,
        default_limit: int = 1,
        tmp_root: str | Path | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.repo = repo
        self.events = events
        self._handlers: dict[str, Handler] = {}
        self._limits: dict[str, int] = dict(limits or {})
        self._default_limit = default_limit
        self._sems: dict[str, threading.BoundedSemaphore] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._procs: dict[str, set[Any]] = {}
        self._logs: dict[str, deque[str]] = {}
        self._lock = threading.Lock()
        self._closed = False
        # 每个任务的临时目录（`cache/tmp/<job_id>`）：任务收尾时统一删。
        # 传空字符串表示不管（测试里不需要碰真实缓存目录）。
        self.tmp_root = Path(tmp_root) if tmp_root else None
        # 上次进程异常退出留下的 running 任务，开机就标失败，否则会永远卡着
        # 先记下「收拾了几个」，再交给启动恢复去汇总——否则统计永远是 0：
        # 等恢复跑到那一步，库里已经没有 running 的活了。
        self.interrupted = self.repo.job_mark_interrupted()
        # 任务日志同时落盘：界面上关掉抽屉，日志还得留得住（用户回头要看"为什么失败"）
        self._logger = logger

    # ------------------------------------------------------------- 注册

    def register(self, job_type: str, handler: Handler, limit: int | None = None) -> None:
        """一个类型一个 handler。重复注册覆盖——同名类型有两个实现是设计错误，不是特性。"""
        self._handlers[job_type] = handler
        if limit is not None:
            self._limits[job_type] = max(1, int(limit))
            self._sems.pop(job_type, None)

    def _sem(self, job_type: str) -> threading.BoundedSemaphore:
        # 延迟创建：注册顺序不影响并发上限
        with self._lock:
            sem = self._sems.get(job_type)
            if sem is None:
                sem = threading.BoundedSemaphore(self._limits.get(job_type, self._default_limit))
                self._sems[job_type] = sem
            return sem

    # ------------------------------------------------------------- 提交

    def submit(
        self,
        job_type: str,
        project_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if job_type not in self._handlers:
            raise WBError("UNSUPPORTED", f"未知任务类型：{job_type}")
        if self._closed:
            raise WBError("CONFLICT", "程序正在退出，不再接受新任务")

        job_id = new_id("job")
        body = payload or {}
        try:
            job = self.repo.job_create(job_id, job_type, project_id, body)
        except RepoClosed:
            # 与上面那个 _closed 检查是同一条竞态的另一半：检查通过之后、写库之前
            # 连接被关掉。对界面来说两种情况都是「程序正在退出」。
            raise WBError("CONFLICT", "程序正在退出，不再接受新任务") from None
        self._cancel[job_id] = threading.Event()
        self.events.emit_now("job.created", {"job": job})

        thread = threading.Thread(
            target=self._run,
            args=(job_id, job_type, project_id, body),
            name=f"wb-job-{job_type}-{job_id[-4:]}",
            daemon=True,
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return job

    def submit_batch(
        self,
        job_type: str,
        project_id: str | None = None,
        payloads: list[dict[str, Any]] | None = None,
        *,
        batch_id: str = "",
    ) -> list[dict[str, Any]]:
        """一次提交一串同类型任务，返回按提交顺序排的 job 列表。

        并发由该类型的信号量管（导出是 2）：这里**不**做聚合进度，也不做"全成功才算
        成功"——批量导出里一条失败不该把另外两条标成失败，界面按行看状态才对。
        需要把一批认出来，就给 ``batch_id``：它会并进每个 payload，``job.list`` 里能筛。
        """
        if job_type not in self._handlers:
            raise WBError("UNSUPPORTED", f"未知任务类型：{job_type}")
        jobs: list[dict[str, Any]] = []
        for payload in payloads or []:
            body = dict(payload or {})
            if batch_id:
                body["batch_id"] = batch_id
            jobs.append(self.submit(job_type, project_id, body))
        return jobs

    # ------------------------------------------------------------- 执行

    def _run(
        self, job_id: str, job_type: str, project_id: str | None, payload: dict[str, Any]
    ) -> None:
        sem = self._sem(job_type)
        # 等信号量期间也允许取消：排队中被取消，不该等到轮到它才响应
        while not sem.acquire(timeout=0.2):
            if self._is_canceled(job_id) or self._closed:
                self._finish(job_id, "canceled", {"code": "CANCELED", "message": "排队期间被取消"})
                return

        ctx = JobContext(self, job_id, job_type, project_id, payload)
        with self._lock:
            self._logs.setdefault(job_id, deque(maxlen=200))
        try:
            if self._is_canceled(job_id):
                self._finish(job_id, "canceled", {"code": "CANCELED", "message": "任务已取消"})
                return

            try:
                self.repo.job_update(
                    job_id, status="running", started_at=now_iso(), error_code=None
                )
            except RepoClosed:
                # 连接已关（退出竞态）：这条任务不会再跑了，收尾那一腿自己会跳过写库。
                self._finish(
                    job_id,
                    "canceled",
                    {"code": "CANCELED", "message": "程序正在退出，任务没有开始"},
                )
                return
            self.events.emit_now(
                "job.updated",
                {"id": job_id, "status": "running", "stage": None, "progress": 0},
            )

            handler = self._handlers[job_type]
            result = handler(ctx, payload)
            if self._is_canceled(job_id):
                # handler 没在检查点退出就返回了：按取消处理，别把半成品当成功
                self._finish(job_id, "canceled", {"code": "CANCELED", "message": "任务已取消"})
            else:
                self._finish(job_id, "succeeded", None, result=result)
        except WBError as exc:
            status = "canceled" if exc.code == "CANCELED" else "failed"
            self._finish(job_id, status, exc.to_payload())
        except Exception as exc:  # noqa: BLE001 - 任何未捕获异常都必须落到任务状态上
            detail = traceback.format_exc()
            self._finish(
                job_id,
                "failed",
                {
                    "code": "INTERNAL",
                    "message": str(exc) or exc.__class__.__name__,
                    "detail": detail,
                },
            )
        finally:
            sem.release()
            with self._lock:
                self._threads.pop(job_id, None)
                self._cancel.pop(job_id, None)

    def _finish(
        self,
        job_id: str,
        status: str,
        error: dict[str, Any] | None,
        result: Any = None,
    ) -> None:
        # 失败/取消时把日志尾巴留在库里：程序关掉之后日志就没了，
        # 而用户回头要看的恰恰是"为什么失败"。成功不必存（占地方、也没人看）。
        with self._lock:
            lines = list(self._logs.pop(job_id, ()) or ())
            self._procs.pop(job_id, None)
        if status != "succeeded" and lines:
            # 先把攒着的日志发出去，避免"任务已失败但日志还差最后几行"
            self.events.flush_logs()
            merged = dict(result) if isinstance(result, dict) else {}
            merged["log"] = lines
            result = merged

        # 失败/取消那一刻，把「为什么」补进行日志文件。默认关，见 `FAILURE_LOG_ENV` 那段。
        #
        # 这一行刻意不新增任何 owner：句子从 `error_message_for_row` 拿（多行的 detail
        # 已经由 `_detail_tail` 收成最后一条非空行 ⇒ **恒一个物理行**，不是我手塞 traceback
        # 原文拼出来的），脱敏从 `log_line` 体内的 `redact` 拿，前缀从 `job_log_prefix` 拿。
        # 在这三处再各写一遍，就是第二个 owner——那份明细本来就在 `error_message` 列里。
        if status != "succeeded" and self._logger is not None and failure_log_enabled():
            reason = error_message_for_row(error) or "（无错误文案）"
            code = (error or {}).get("code") or "-"
            try:
                self._logger(f"{job_log_prefix(job_id)}任务收尾 {status} code={code} → {reason}")
            except Exception:  # 与 `_log` 同一口径：日志不能反过来把任务搞挂
                pass

        payload = {
            "status": status,
            "finished_at": now_iso(),
            "result": result,
            "error_code": (error or {}).get("code"),
            "error_message": error_message_for_row(error),
        }
        if status == "succeeded":
            payload["progress"] = 1.0
        # 退出竞态：shutdown() 的 join 有超时，超时后连接可能已经被关。此时写库必然抛
        # sqlite3.ProgrammingError: Cannot operate on a closed database，而那个异常会把整个
        # 任务线程带着 traceback 打死（全量回归里表现为一条 PytestUnhandledThreadException
        # Warning，真正的损失是「失败原因没写进库」）。跳过写库，把没写成的那件事写进日志。
        # 库里那一行留在 running，下次开机由 job_mark_interrupted 标成 INTERRUPTED——
        # 那本来就是「上次退出时没跑完」的既定语义，不需要第二条 owner。
        job: dict[str, Any] = {}
        try:
            self.repo.job_update(job_id, **payload)
            job = self.repo.job_get(job_id) or {}
        except RepoClosed:
            if self._logger is not None:
                try:
                    self._logger(
                        f"{job_log_prefix(job_id)}收尾没写进库（程序正在退出）："
                        f"status={status} code={(error or {}).get('code') or '-'}"
                    )
                except Exception:  # noqa: BLE001 - 日志不能反过来把退出路径搞挂
                    pass
            _LOGGER.warning(
                "任务 %s 收尾时数据库已关闭，状态没写进库（status=%s code=%s）",
                job_id,
                status,
                (error or {}).get("code"),
            )
        # 完成是状态跃迁，必须立即发，不节流
        self._clean_tmp(job_id)
        self.events.emit_now(
            "job.finished",
            {"id": job_id, "status": status, "result": result, "error": error, "job": job},
        )

    def _clean_tmp(self, job_id: str) -> None:
        """删这个任务自己的临时目录。

        `cache/tmp/<job_id>` 里是拼接清单、中间音频这类东西——留在盘上没人清，
        用户过几个月发现缓存目录几个 G。删失败不算任务失败（可能有句柄没释放），
        但下一次启动的 sweep 还会再删一次。
        """
        if self.tmp_root is None:
            return
        target = self.tmp_root / job_id
        try:
            shutil.rmtree(target, ignore_errors=True)
        except Exception:  # noqa: BLE001 - 清理是尽力而为
            pass

    # ------------------------------------------------------------- 进度

    def _report(
        self,
        job_id: str,
        value: float | None,
        stage: str | None,
        speed: str | None = None,
        eta: str | None = None,
    ) -> None:
        progress = None if value is None else max(0.0, min(1.0, float(value)))
        fields: dict[str, Any] = {}
        if progress is not None:
            fields["progress"] = progress
        if stage is not None:
            fields["stage"] = stage
        if fields:
            try:
                self.repo.job_update(job_id, **fields)
            except RepoClosed:
                # 进度是可丢的（真值在事件里），不值得为一个正在退出的进程抛出去；
                # 抛出去会变成 handler 的 INTERNAL 失败，比丢一条进度更误导。
                return
        self.events.emit(
            "job.updated",
            {
                "id": job_id,
                "status": "running",
                "stage": stage,
                "progress": progress,
                "speed": speed,
                "eta": eta,
            },
            key=job_id,
        )

    def _log(self, job_id: str, line: str) -> None:
        # 日志每条都要到（用户会复制它排错），所以不节流；靠 handler 自己控制频率
        text = str(line)
        with self._lock:
            bucket = self._logs.get(job_id)
            if bucket is not None:
                bucket.append(text)
        # 攒批（默认 200ms 一条事件）：一次任务每秒几十行 stderr，
        # 逐行推等于把 UI 线程耗在序列化上。见 `events.EventBus.log`。
        self.events.log(job_id, text)
        if self._logger is not None:
            try:
                self._logger(f"{job_log_prefix(job_id)}{text}")
            except Exception:  # 日志不能反过来把任务搞挂
                pass

    # ------------------------------------------------------------- 控制

    def _is_canceled(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        return bool(event and event.is_set())

    def _register_process(self, job_id: str, proc: Any) -> None:
        with self._lock:
            self._procs.setdefault(job_id, set()).add(proc)

    def _kill_processes(self, job_id: str) -> int:
        """杀掉这个任务还活着的子进程（整棵树）。返回杀了几个。

        只置 Event 是不够的：handler 可能正卡在 `for line in proc.stdout` 里，
        要等子进程出口才醒——而子进程正是那个要跑十分钟的东西。
        """
        from video_workbench.core.downloader import kill_tree

        with self._lock:
            procs = list(self._procs.get(job_id, ()))
        killed = 0
        for proc in procs:
            try:
                if proc.poll() is not None:
                    continue
                kill_tree(int(proc.pid))
                killed += 1
            except Exception:  # noqa: BLE001 - 进程可能刚好自己退了
                continue
        return killed

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.repo.job_get(job_id)
        if not job:
            raise WBError("NOT_FOUND", "任务不存在")
        if job["status"] in ("succeeded", "failed", "canceled"):
            return job  # 已结束：幂等返回，不报错
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()
        self._kill_processes(job_id)
        self.repo.job_update(job_id, cancel_requested=True)
        return self.repo.job_get(job_id) or job

    def retry(self, job_id: str) -> dict[str, Any]:
        """重试 = 用同样的类型和 payload 起一个新任务。

        不复活原记录：历史失败记录本身有价值（同一素材反复失败说明素材有问题），
        覆盖掉就看不到这个信号了。
        """
        job = self.repo.job_get(job_id)
        if not job:
            raise WBError("NOT_FOUND", "任务不存在")
        if job["status"] in ("queued", "running"):
            raise WBError("CONFLICT", "任务正在进行中，不能重试")
        new_job = self.submit(job["type"], job.get("project_id"), job.get("payload") or {})
        self.repo.job_update(new_job["id"], attempts=int(job.get("attempts") or 0) + 1)
        # 回读再返回：submit 的快照是"加 attempt 之前"的，直接把旧字典交出去，
        # 界面会显示"第 0 次重试"，而库里其实是第 1 次。
        return self.repo.job_get(new_job["id"]) or new_job

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.repo.job_get(job_id)
        if not job:
            raise WBError("NOT_FOUND", "任务不存在")
        return job

    def list(self, statuses: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.repo.job_list(statuses, limit)

    def shutdown(self) -> None:
        """停止接受新任务，并给在跑的任务一点时间收尾。

        **光置 cancel event 是不够的**：handler 常卡在 `for line in proc.stdout` 上，
        要等子进程出口才醒——而子进程正是那个要跑十分钟的东西。所以这里和 `cancel()`
        一样要把子进程杀掉，否则下面那个 join 每次都会超时，连接随后被关掉，
        收尾那一腿就撞上「closed database」。
        """
        self._closed = True
        with self._lock:
            for event in self._cancel.values():
                event.set()
            threads = list(self._threads.values())
            job_ids = list(self._threads)
        for job_id in job_ids:
            self._kill_processes(job_id)
        # 给 handler 一点时间走完 except/finally，避免退出时把库写坏
        for thread in threads:
            thread.join(timeout=2.0)
