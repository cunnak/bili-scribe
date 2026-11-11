"""退出竞态：``JobManager.submit()`` 与 ``shutdown()`` 抢同一把锁的那一段。

为什么这一层必须有测试。2026-11-01 那次「任务跑完了界面还停在 97%」的现场是
``wb-job-clip.export-58b2`` 在关窗瞬间收尾：``repo.close()`` 先把库关了，任务线程还在
往里写；而退出路径上的 ``shutdown()`` 自己抛了异常，把 ``events.close()`` /
``repo.close()`` 一起跳过。三条缺陷落在同一个时间窗里，单独修任何一条都看不出变化，
所以这里钉的是五条互不重叠的契约：

1. ``shutdown()`` 返回之后没有活着的 ``wb-job-`` 线程——否则它会跑到 ``repo.close()``
   之后，``sqlite3.ProgrammingError: Cannot operate on a closed database`` 就是这么来的；
2. ``shutdown()`` 自己不抛，包括与 ``submit()`` 并发时——它一抛，后面两步清理全跳过；
3. 关窗瞬间进来的 ``submit()`` 只有两种合法结局：报 ``CONFLICT``，或者它的线程在
   ``shutdown()`` 拍的那份快照里（被杀、被 join）；
4. ``_cancel`` / ``_threads`` / ``_closed`` 三处写入全在 ``self._lock`` 里。这条只能是
   源码级判据：锁外写登记表在行为上只能靠运气撞出来，撞不到就是全绿；
5. ``shutdown()`` 幂等。

再加上同一条时间窗里的第四个缺陷：库关掉之后 ``cancel()`` / ``retry()`` / ``get()`` /
``list()`` 必须报 ``CONFLICT``（不可重试），而不是把 ``RepoClosed`` 漏成 ``INTERNAL``——
``INTERNAL`` 在 ``RETRYABLE_CODES`` 里，界面会真的去重试一条注定失败的调用。
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from video_workbench.app.errors import WBError
from video_workbench.app.events import EventBus
from video_workbench.app.jobs import JobManager
from video_workbench.infra.db import open_and_migrate
from video_workbench.infra.repo import Repo

# ---- 夹具 ----------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path):
    conn = open_and_migrate(tmp_path / "wb.db")
    r = Repo(conn)
    try:
        yield r
    finally:
        r.close()


def _manager(repo: Repo) -> JobManager:
    """一个只跑 1 路并发、不碰真实缓存目录的 JobManager。"""
    return JobManager(repo, EventBus(), default_limit=1)


def _job_threads() -> set[threading.Thread]:
    """当前活着的任务线程。按产品自己起的线程名前缀认，不读私有登记表。"""
    return {t for t in threading.enumerate() if t.name.startswith("wb-job-")}


def _blocking_handler(ctx, payload) -> dict[str, bool]:
    """会被取消信号叫停的 handler。

    不能写成死循环：``shutdown()`` 的 ``join(timeout=2.0)`` 到点就返回，线程还活着
    ⇒ I1 会红在测试自己身上。所以每 5ms 看一眼 ``should_cancel()``，被叫停就按产品
    约定抛 ``CANCELED`` 正常收尾（handler 侧唯一的退出方式）。
    """
    for _ in range(400):
        if ctx.should_cancel():
            raise WBError("CANCELED", "任务已取消")
        time.sleep(0.005)
    return {"ok": True}


# ---- I4：登记表只在锁里写（源码级） ---------------------------------------

_REGISTRY_NAMES = ("_cancel", "_threads", "_closed")


def _job_manager_methods() -> dict[str, ast.FunctionDef]:
    path = Path(__file__).resolve().parents[2] / "video_workbench" / "app" / "jobs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "JobManager":
            return {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}
    raise AssertionError("jobs.py 里找不到 JobManager 这个类")


def _is_self_lock(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "_lock"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "self"
    )


def _lock_guarded_node_ids(func: ast.FunctionDef) -> set[int]:
    """``with self._lock:`` 里所有节点的 id（含 with 自己的 body 语句节点）。

    用结构包含而不是行号：行号会随一行注释整体漂走，而 id 是同一棵 AST 里的对象身份。
    """
    guarded: set[int] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.With):
            continue
        if not any(_is_self_lock(item.context_expr) for item in node.items):
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                guarded.add(id(inner))
    return guarded


def _self_attribute_writes(node: ast.AST) -> set[str]:
    """一次赋值/删除碰到了哪些 ``self._xxx``。``self._cancel[id] = ...`` 也算。"""
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    else:
        return set()
    names: set[str] = set()
    for target in targets:
        for inner in ast.walk(target):
            if (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "self"
            ):
                names.add(inner.attr)
    return names


def test_the_three_registries_are_only_written_under_the_lock():
    """I4：``_cancel`` / ``_threads`` / ``_closed`` 的写入不许出现在锁外。

    为什么这条非得看源码：锁外写登记表的后果是「``shutdown()`` 拍快照时撞上
    ``dictionary changed size during iteration``」或者「捞到一条还没 start 的线程
    ⇒ ``join()`` 抛 ``cannot join thread before it is started``」。两者都是概率事件，
    行为测试撞不到就等于没测。
    """
    methods = _job_manager_methods()
    offenders: list[str] = []
    seen: set[str] = set()
    for method_name in ("submit", "shutdown"):
        func = methods.get(method_name)
        assert func is not None, f"JobManager 里没有 {method_name}()"
        guarded = _lock_guarded_node_ids(func)
        for node in ast.walk(func):
            names = _self_attribute_writes(node) & set(_REGISTRY_NAMES)
            if not names:
                continue
            seen |= names
            if id(node) not in guarded:
                offenders.append(f"{method_name}() 第 {node.lineno} 行写了 {sorted(names)}")
    assert not offenders, "锁外写登记表：" + "；".join(offenders)
    # 三个名字必须都还找得到。少了就说明被改名/搬走了，上面那条断言会退化成永真。
    assert seen == set(_REGISTRY_NAMES), (
        f"submit()/shutdown() 里只找到 {sorted(seen)} 的写入；"
        f"{sorted(set(_REGISTRY_NAMES) - seen)} 不见了，这条判据得跟着改，否则它只是在空转"
    )


# ---- I1：shutdown() 之后没有任务线程活着 ----------------------------------


def test_no_job_thread_outlives_shutdown(repo):
    """I1：``shutdown()`` 返回之后，任何 ``wb-job-`` 线程都不许还活着。

    活着的那个会继续往下写：``_finish()`` 的 ``repo.job_update``、``_report()`` 的
    进度写库，全在 ``repo.close()`` 之后 ⇒ 现场那句
    ``sqlite3.ProgrammingError: Cannot operate on a closed database``。
    """
    before = _job_threads()
    manager = _manager(repo)
    manager.register("slow", _blocking_handler, limit=1)
    job = manager.submit("slow")
    manager.shutdown()
    leaked = _job_threads() - before
    assert not leaked, f"shutdown() 返回后仍有任务线程在跑：{sorted(t.name for t in leaked)}"
    assert manager.get(job["id"])["status"] == "canceled"


# ---- I2：shutdown() 自己不抛 ---------------------------------------------


def test_shutdown_never_raises_while_a_submit_is_in_flight(repo):
    """I2：与 ``submit()`` 撞在一起时 ``shutdown()`` 不许抛。

    它一抛，调用方（``service.close()``）后面的 ``events.close()`` / ``repo.close()``
    就全跳过——那正是这条缺陷最初的形态：库没关、线程没等，进程退出时才炸。
    跑 10 轮：真正的交错窗口只有几十微秒，单轮撞不上不代表没缺陷。
    """
    for round_index in range(10):
        manager = _manager(repo)
        manager.register("slow", _blocking_handler, limit=1)
        gate = threading.Barrier(2)
        failures: list[BaseException] = []

        def submitter(m: JobManager = manager, b: threading.Barrier = gate) -> None:
            b.wait()
            try:
                m.submit("slow")
            except WBError:
                return  # 关窗后 submit 报 CONFLICT 是合法结局，不在这里断
            except BaseException as exc:  # noqa: BLE001 - 任何别的异常都是缺陷
                failures.append(exc)

        thread = threading.Thread(target=submitter, name=f"wb-test-race-{round_index}", daemon=True)
        thread.start()
        gate.wait()
        manager.shutdown()  # 这一句抛 = 缺陷仍在
        thread.join(timeout=10.0)
        assert not thread.is_alive(), f"第 {round_index} 轮：submitter 没能在 shutdown 后结束"
        assert not failures, f"第 {round_index} 轮：submit() 抛了非 WBError：{failures!r}"


# ---- I3：关窗瞬间的 submit() 只有两种合法结局 -----------------------------


def test_a_submit_that_races_shutdown_gets_conflict_or_is_joined(repo):
    """I3：关窗瞬间进来的 ``submit()``，要么报 ``CONFLICT``，要么线程进了快照。

    「进了快照」的可观测后果只有一个：它被 cancel + join 了，所以 ``shutdown()`` 之后
    不剩活线程。中间那种「起来了、没登记」最坏——那条线程从快照后面溜过去，
    没人 kill 没人 join，一路跑到 ``repo.close()`` 之后。
    """
    for round_index in range(10):
        before = _job_threads()
        manager = _manager(repo)
        manager.register("slow", _blocking_handler, limit=1)
        gate = threading.Barrier(2)
        outcome: list[str] = []

        def submitter(m: JobManager = manager, b: threading.Barrier = gate) -> None:
            b.wait()
            try:
                m.submit("slow")
            except WBError as exc:
                outcome.append(exc.code)
            else:
                outcome.append("accepted")

        thread = threading.Thread(
            target=submitter, name=f"wb-test-outcome-{round_index}", daemon=True
        )
        thread.start()
        gate.wait()
        manager.shutdown()
        thread.join(timeout=10.0)
        assert outcome, f"第 {round_index} 轮：submit() 既没返回也没抛"
        assert set(outcome) <= {"CONFLICT", "accepted"}, (
            f"第 {round_index} 轮：出现了第三种结局 {outcome!r}；"
            "关窗瞬间只有「报 CONFLICT」和「进快照被杀被 join」两种合法结果"
        )
        leaked = _job_threads() - before
        assert not leaked, (
            f"第 {round_index} 轮（结局 {outcome[0]}）：线程从 shutdown 的快照后面溜过去了，"
            f"现在还在跑：{sorted(t.name for t in leaked)}"
        )


# ---- I5：shutdown() 幂等 -------------------------------------------------


def test_shutdown_is_idempotent(repo):
    """I5：第二次 ``shutdown()`` 不许抛。

    退出路径上可能被调两次（窗口 close 事件 + `atexit`）。第二次要是抛，
    第一次做完的清理会被一个异常盖住，日志里只剩一句无关的 traceback。
    """
    manager = _manager(repo)
    manager.register("slow", _blocking_handler, limit=1)
    manager.submit("slow")
    manager.shutdown()
    manager.shutdown()


# ---- P4：库已关时四个入口都报 CONFLICT，不是 INTERNAL ---------------------


@pytest.mark.parametrize("entry", ["cancel", "retry", "get", "list"])
def test_a_management_entry_point_reports_the_exit_as_conflict_not_internal(repo, entry):
    """P4：``repo.close()`` 之后四个入口必须报 ``CONFLICT`` 且不可重试。

    漏成 ``INTERNAL`` 的代价是界面真的去重试：``INTERNAL`` 在 ``RETRYABLE_CODES`` 里
    ⇒ ``to_payload()["retryable"]`` 是 True ⇒ 「重试」按钮亮着，点了必然再失败一次。
    """
    manager = _manager(repo)
    job = repo.job_create("job_p4", "slow", None, {})
    # 先落到一个「能重试」的终态。否则 retry() 会因为 status=queued 报
    # CONFLICT「任务正在进行中」——那也是一条 CONFLICT，断言会假通过。
    repo.job_update(job["id"], status="failed", error_code="INTERNAL", error_message="炸了")
    repo.close()
    with pytest.raises(WBError) as caught:
        if entry == "list":
            manager.list()
        else:
            getattr(manager, entry)(job["id"])
    error = caught.value
    assert error.code == "CONFLICT", f"{entry}() 报的是 {error.code}，不是 CONFLICT"
    assert error.to_payload()["retryable"] is False, f"{entry}() 被标成了可重试"
    assert "正在退出" in error.message, f"{entry}() 的话术不是退出竞态那一句：{error.message!r}"
