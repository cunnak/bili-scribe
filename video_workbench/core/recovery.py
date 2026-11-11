"""启动恢复：把上一次"非正常退出"留下的烂摊子收拾干净。

**为什么必须做这件事。** 用户强制关窗、任务管理器结束进程、断电——这三种情况
在 Windows 上一点都不罕见。事后重启，如果什么都不做：

- `wb_job` 里那些 `running` 的行永远不会结束（界面上一排进度条卡在 30%）；
- `wb_merge` 里那次合并停在 `running`，用户点不了重试；
- 项目的 `stage` 已经跑到 `summarized`，可 `summary.md` 因为写到一半被杀了，
  点开就是"有总结但没有内容"。

所以启动时做三件事，每件都只依赖库里的状态，不依赖任何"崩溃前的记忆"：

1. 运行中的任务 → `failed` + 码 `INTERRUPTED`（可重试）；
2. 运行中的合并记录 → `failed`，理由写清楚；
3. 项目的进度位往回收：拿着它现在声称的阶段，逐级检查"这一步的证据还在不在"，
   不在了就退回上一个证据齐全的阶段，并把原因写进 `project.error`——
   宁可让用户看到"回退到已下载"，也不要让他看到"已总结"却打不开内容。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .stages import STAGE_ORDER

# 阶段顺序的唯一 owner 在 `core/stages.py`：这里**只 import，不复制第二份**。
# 保留本模块的同名属性是因为 `recover()` / `recover_stage()` 一直从模块全局取它，
# 而且测试会 patch `recovery.STAGE_ORDER` 来模拟"阶段表变了"——名字换了那些用例就失效。

#: 每个阶段成立所必需的证据。键是阶段，值是"检查函数名"。
#: 只检查本地能验证的东西（文件在不在、库里有不有行），不联网、不探测平台。
_STAGE_CHECKS = (
    "new",
    "downloaded",
    "transcribed",
    "summarized",
    "clipped",
    "merged",
    "published",
)


def _exists(path: str) -> bool:
    return bool(path) and Path(path).is_file()


def _assets_by_kind(assets: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        out.setdefault(str(asset.get("kind") or ""), []).append(asset)
    return out


def _has_source(kinds: dict[str, list[dict[str, Any]]]) -> bool:
    return any(_exists(str(a.get("path") or "")) for a in kinds.get("source", []))


def _has_subtitle_file(kinds: dict[str, list[dict[str, Any]]]) -> bool:
    return any(_exists(str(a.get("path") or "")) for a in kinds.get("subtitle", []))


def _has_transcript(repo: Any, project_id: str, kinds: dict[str, list[dict[str, Any]]]) -> bool:
    if repo.transcript_get(project_id):
        return True
    return _has_subtitle_file(kinds)


def _has_summary(repo: Any, project_id: str, kinds: dict[str, list[dict[str, Any]]]) -> bool:
    if repo.summary_latest(project_id) is None:
        return False
    # 库里有行还不够：总结的正文同时落了一份 summary.md，被杀了就是"有行没文件"
    return any(_exists(str(a.get("path") or "")) for a in kinds.get("summary", []))


def _has_candidates(repo: Any, project_id: str) -> bool:
    return bool(repo.candidate_list(project_id))


def _has_exports(repo: Any, project_id: str, kinds: dict[str, list[dict[str, Any]]]) -> bool:
    return any(_exists(str(a.get("path") or "")) for a in kinds.get("export", []))


def _has_publish(repo: Any, project_id: str) -> bool:
    row = repo.publish_for_project(project_id)
    return bool(row) and str(row.get("status") or "") in {"published", "withdrawn"}


def stage_ok(repo: Any, project: dict[str, Any], assets: list[dict[str, Any]]) -> bool:
    """这个项目现在声称的阶段，证据齐全吗。"""
    stage = str(project.get("stage") or "new")
    pid = str(project.get("id") or "")
    kinds = _assets_by_kind(assets)
    if stage == "new":
        return True
    if stage == "downloaded":
        return _has_source(kinds) or _has_subtitle_file(kinds)
    if stage in {"subtitle", "transcribed"}:
        return _has_transcript(repo, pid, kinds)
    if stage == "summarized":
        return _has_summary(repo, pid, kinds)
    if stage == "clipped":
        return _has_candidates(repo, pid)
    if stage == "merged":
        return _has_exports(repo, pid, kinds)
    if stage == "published":
        return _has_publish(repo, pid)
    # 不认识的阶段：不猜、不动它，交给人和文档去对
    return True


def recover_stage(
    repo: Any, project: dict[str, Any], assets: list[dict[str, Any]]
) -> tuple[str, str]:
    """算出应该退回到哪个阶段。

    返回 ``(stage, reason)``；不需要回退时 stage 与原值相同、reason 为空串。
    """
    stage = str(project.get("stage") or "new")
    if stage not in STAGE_ORDER:
        return stage, ""
    if stage_ok(repo, project, assets):
        return stage, ""
    # 从当前阶段往前找第一个"证据齐全"的阶段。找不到就退到 new（至少能重新下载/导入）。
    index = STAGE_ORDER.index(stage)
    for prev in reversed(STAGE_ORDER[:index]):
        probe = dict(project)
        probe["stage"] = prev
        if prev == "new" or stage_ok(repo, probe, assets):
            return prev, f"启动检查：{stage} 的产物不见了，已退回 {prev}"
    return "new", f"启动检查：{stage} 的产物不见了，已退回 new"


def recover(repo: Any, *, log: Any = None, jobs: int | None = None) -> dict[str, Any]:
    """跑完整的恢复流程，返回做了什么（数字要能被界面和验收脚本读到）。

    ``jobs`` 允许调用方把"已经收尾的任务数"喂进来：``JobManager`` 在构造时就会
    把残留任务标掉，等这里再查就只剩 0 了——数字要靠它带过来。
    """
    if jobs is None:
        jobs = repo.job_mark_interrupted()
    merges = repo.merge_mark_interrupted()
    rolled: list[dict[str, str]] = []

    for project in repo.project_list(limit=1000):
        pid = str(project.get("id") or "")
        assets = repo.asset_list(pid)
        stage, reason = recover_stage(repo, project, assets)
        if not reason:
            continue
        repo.project_update(pid, stage=stage, error=reason)
        rolled.append(
            {
                "project_id": pid,
                "from": str(project.get("stage") or ""),
                "to": stage,
                "reason": reason,
            }
        )
        if log is not None:
            log(f"{project.get('title') or pid}：{reason}")

    return {"jobs": jobs, "merges": merges, "projects": len(rolled), "rolled": rolled}
