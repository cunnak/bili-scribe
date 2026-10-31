"""批量下载：父批次 + 子项（P1-07）。

一次「下载」可能是一集，也可能是合集里的 40 集、分P 的 12 集。界面上那一条进度条
必须代表整件事，而库里每一集又各自有自己的项目与状态。所以拆成两层：

- **父任务** ``download.batch``：不碰磁盘、不碰网络，只负责派活、汇总、汇报。
- **子任务** ``download.item``：真正下载一集（``download_worker`` 里那套）。

三个决定：

**1. 并发靠信号量，不靠 worker 自己排队。** 子任务类型的 limit 设成 2，
   一次性把 40 个子任务全提交进去，JobManager 自己会只放 2 个进临界区。
   好处是队列在任务中心里可见——用户能看到"排队中"，而不是"没反应"。

**2. 子项失败不拖垮父批次。** 40 集里有 1 集要大会员，剩下 39 集该正常下完。
   父任务只在**全部**子项都失败时才判失败，否则成功并附上失败清单。

**3. 取消是传染的。** 父任务被取消，必须把子任务也取消掉再收尾；否则界面上
   父任务已消失、后台还在偷偷下 40 集。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..app.errors import WBError
from ..app.jobs import JobContext

CHILD_TYPE = "download.item"
BATCH_TYPE = "download.batch"
POLL_SECONDS = 0.2
TERMINAL = ("succeeded", "failed", "canceled")


def make_batch_handler(
    *,
    manager: Any,
    child_type: str = CHILD_TYPE,
    poll: float = POLL_SECONDS,
    submit: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> Callable[[JobContext, dict[str, Any]], dict[str, Any]]:
    """装配批量 handler。``manager`` 只需要 ``submit/get/cancel`` 三个方法。"""

    def _submit(item_payload: dict[str, Any]) -> dict[str, Any]:
        if submit is not None:
            return submit(child_type, item_payload)
        return manager.submit(child_type, None, item_payload)

    def handler(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        items = [dict(i) for i in (payload.get("items") or []) if isinstance(i, dict)]
        if not items:
            raise WBError("VALIDATION", "没有可下载的条目")

        quality = str(payload.get("quality") or "1080P").upper()
        shared = {
            "quality": quality,
            "up_name": str(payload.get("up_name") or ""),
            "collection_title": str(payload.get("collection_title") or ""),
            "page_count": int(payload.get("page_count") or len(items)),
        }

        ctx.progress(0.0, stage=f"排队 0/{len(items)}")
        children: list[dict[str, Any]] = []
        for item in items:
            ctx.check_cancel()
            body = {**shared, "item": item}
            job = _submit(body)
            children.append({"item": item, "job_id": str(job.get("id") or ""), "status": "queued"})

        total = len(children)
        done = 0
        # 轮询而不是 join：子任务也可能被用户在任务中心单独取消，轮询能看见。
        while True:
            if ctx.should_cancel():
                for child in children:
                    if child["status"] not in TERMINAL:
                        try:
                            manager.cancel(child["job_id"])
                        except WBError:
                            pass
                raise WBError("CANCELED", "任务已取消")

            live = 0
            done = 0
            for child in children:
                try:
                    job = manager.get(child["job_id"])
                except WBError:
                    child["status"] = "failed"
                    continue
                child["status"] = str(job.get("status") or "queued")
                child["project_id"] = job.get("project_id")
                child["error"] = job.get("error_message") or ""
                if child["status"] in TERMINAL:
                    done += 1
                else:
                    live += 1

            ratio = done / total if total else 0.0
            # 子项进行中的那部分也算进来，进度条才不会卡在整数格上
            for child in children:
                if child["status"] == "running":
                    try:
                        job = manager.get(child["job_id"])
                    except WBError:
                        continue
                    ratio += (float(job.get("progress") or 0) / total) if total else 0
                    break
            ctx.progress(min(0.99, ratio), stage=f"下载中 {done}/{total}")
            if live == 0:
                break
            time.sleep(max(0.05, poll))

        ok = [c for c in children if c["status"] == "succeeded"]
        failed = [c for c in children if c["status"] != "succeeded"]
        result = {
            "total": total,
            "ok": len(ok),
            "failed": len(failed),
            "quality": quality,
            "items": [
                {
                    "bvid": c["item"].get("bvid", ""),
                    "title": c["item"].get("title", ""),
                    "page": c["item"].get("page", 1),
                    "job_id": c["job_id"],
                    "project_id": c.get("project_id"),
                    "status": c["status"],
                    "error": c.get("error", ""),
                }
                for c in children
            ],
        }
        ctx.progress(1.0, stage=f"完成 {len(ok)}/{total}")
        if not ok:
            # 全灭才判失败：失败清单在 result 里，重试只跑这些
            raise WBError("ALL_FAILED", f"{total} 个条目全部失败", result)
        return result

    return handler


def failed_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """从父任务的 result 里捞出失败项，供"只重试失败项"使用。"""
    out = []
    for row in (result or {}).get("items") or []:
        if isinstance(row, dict) and row.get("status") != "succeeded":
            out.append(
                {
                    "bvid": row.get("bvid", ""),
                    "title": row.get("title", ""),
                    "page": row.get("page", 1),
                    "page_count": (result or {}).get("total", 0),
                }
            )
    return out
