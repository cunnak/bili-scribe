"""仓储的跨线程读写：任务提交与后台线程同时动一条连接。

回归背景（P4-02 收尾时抓到的 flake）：`Repo._one` 原来是
``self._exec(sql).fetchone()``——锁在 ``_exec`` 里，**取回游标时已经出锁**，
别的线程在同一条连接上执行语句就把这个游标顶掉。现象是刚 ``job_create``
的行立刻 ``job_get`` 查不到（``WBError [NOT_FOUND] 任务不存在``，测试里约
1/5 跑次复现），压测还能压出 ``sqlite3.InterfaceError: bad parameter or other
API misuse``。修法：执行与取回整段握在 ``DB_LOCK`` 里。
"""

from __future__ import annotations

import threading

from video_workbench.infra.repo import new_id


def test_read_right_after_write_survives_concurrent_threads(service):
    repo = service.repo
    stop = threading.Event()
    misses: list[str] = []
    errors: list[str] = []

    def writer() -> None:
        while not stop.is_set():
            repo.job_create(new_id("job"), "dev.demo", None, {"x": 1})

    def reader() -> None:
        while not stop.is_set():
            job_id = new_id("job")
            repo.job_create(job_id, "dev.demo", None, {"x": 2})
            try:
                row = repo.job_get(job_id)
            except Exception as exc:  # noqa: BLE001 - 记下来断言更有说服力
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if row is None:
                misses.append(job_id)

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
    threads.append(threading.Thread(target=reader, daemon=True))
    for thread in threads:
        thread.start()
    try:
        stop_wait = threading.Event()
        stop_wait.wait(1.2)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)

    assert errors == []
    assert misses == []