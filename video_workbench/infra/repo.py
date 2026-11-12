"""仓储：``wb_*`` 表的读写。

只做**存取**，不写业务判断。业务规则留在 ``app/service.py``——
放在这里会让"查一条任务"和"这条任务能不能重试"混在一起，
后来做批量操作时根本分不清哪些是查询、哪些是状态迁移。

JSON 字段一律 ``json.dumps(ensure_ascii=False)``：中文内容存成 ``\\uXXXX``
后，用 sqlite3 CLI 直接查库时一句都读不懂，排障成本会翻倍。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from video_workbench.app.errors import WBError
from video_workbench.infra.db import DB_LOCK, now_iso


def new_id(prefix: str) -> str:
    """带前缀的短 ID。前缀让日志里一眼能看出这是项目还是任务。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any) -> Any:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # 库里出现非 JSON 时不要让整个列表接口 500：
        # 返回原文，前端至少还能显示出来
        return value


def _candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    row["tags"] = _loads(row.pop("tags_json", None)) or []
    row["notes"] = _loads(row.pop("notes_json", None)) or {}
    row["copy"] = _loads(row.pop("copy_json", None)) or {}
    return row


def _merge_row(row: Any) -> dict[str, Any]:
    """合并行：解 `items_json`/`preflight_json`，`fallback_used` 转 bool。"""
    data = dict(row)
    data["items"] = _loads(data.pop("items_json", None)) or []
    data["preflight"] = _loads(data.pop("preflight_json", None)) or {}
    data["fallback_used"] = bool(data.get("fallback_used"))
    return data


# 排序白名单。**必须是模块级常量**：SQL 的 ORDER BY 只能拼白名单里的字面量，
# 用户传的字符串永远不直接进 SQL（`sort="created_at; DROP TABLE ..."` 会被这里挡下）。
# title 的自然序（「第2讲」<「第10讲」）SQL 排不出来——真正的顺序由 service 层用
# natural_key 算，这里给一个稳定兜底序，保证直调 repo 时也可复现。
SORTS: dict[str, str] = {
    # 兼容红线：默认排序必须与改造前逐字段同序。老 SQL 是 `ORDER BY created_at DESC`，
    # SQLite 对同一时间戳按 rowid（插入序）稳定排列；加 `id DESC` 会把同秒创建的两条翻面，
    # 字幕页/旧调用方会看到第一项被换掉（test_subtitles_real_backend_ui 已踩到）。
    "created_desc": "created_at DESC",
    "created_asc": "created_at ASC, id ASC",
    "collection_index": "collection_index ASC, created_at ASC, id ASC",
    "title": "title COLLATE NOCASE ASC, created_at ASC, id ASC",
}


def _project_where(
    query: str = "",
    *,
    stage: str = "",
    include_archived: bool = False,
    collection: str = "",
    group: str = "",
) -> tuple[str, list[Any]]:
    """项目列表/计数/分组的公共 WHERE。

    搜索的 OR 组**必须整体加括号**：老写法 `WHERE title LIKE ? OR up_name LIKE ?` 再加
    `AND stage=?` 时，AND 的优先级高于 OR，条件会被切成
    `title LIKE ? OR (up_name LIKE ? AND stage=?)`——搜索词一命中标题就绕过了阶段过滤。
    `collection_title` 也进 LIKE：用户按合集名搜「线性代数」是该库最常用的入口。
    """
    clauses: list[str] = []
    params: list[Any] = []
    text = str(query or "").strip()
    if text:
        like = f"%{text}%"
        clauses.append(
            "(title LIKE ? OR up_name LIKE ? OR bvid LIKE ? OR collection_title LIKE ?)"
        )
        params.extend([like, like, like, like])
    stage_key = str(stage or "").strip()
    if stage_key:
        clauses.append("stage=?")
        params.append(stage_key)
    collection_title = str(collection or "")
    if collection_title:
        clauses.append("collection_title=?")
        params.append(collection_title)
    group_key = str(group or "").strip()
    if group_key == "local":
        clauses.append("COALESCE(collection_title,'')='' AND source_kind='local'")
    elif group_key == "unassigned":
        clauses.append(
            "COALESCE(collection_title,'')='' AND COALESCE(source_kind,'')<>'local'"
            " AND COALESCE(bvid,'')<>''"
        )
    elif group_key == "other":
        clauses.append(
            "COALESCE(collection_title,'')='' AND COALESCE(source_kind,'')<>'local'"
            " AND COALESCE(bvid,'')=''"
        )
    if not include_archived:
        # 归档是软删除：NULL 与空串都算"未归档"（老库 ALTER 出来的列默认 NULL，
        # 但手工 SQL 写过空串的库也存在）。
        clauses.append("COALESCE(archived_at,'')=''")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


class RepoClosed(RuntimeError):
    """连接已经关掉之后还来写库。

    只在一个地方真的会出现：退出竞态——`JobManager.shutdown()` 的 join 有超时，
    超时后仍活着的任务线程走到 `_finish` 时，`WorkbenchService.close()` 已经把连接关了。
    这时抛 sqlite3.ProgrammingError: Cannot operate on a closed database 会把整个线程
    带着 traceback 打死，而真正的损失是**失败原因没写进库**。所以这里给一个自己的异常，
    让调用方按「退出中」这一支处理。
    """


class Repo:
    """薄封装。每个方法一个 SQL，不做 ORM。

    用 ORM 的话，跨进程共享库（bili-scribe 也在写）出现 schema 漂移时
    报错会发生在很隐蔽的地方；这里每条 SQL 都是明写的，出错点写在脸上。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._closed = False

    # ------------------------------------------------------------- 通用

    def close(self) -> None:
        """关连接。**与写入同一把锁**：close 撞上正在执行的 SQL 会让游标半途失效。"""
        with DB_LOCK:
            if self._closed:
                return
            self._closed = True
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001 - 已经关过/底层已释放，退出路径上不值得抛
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    def _guard(self) -> None:
        """所有 SQL 的共同前置检查（必须在 DB_LOCK 里调）。"""
        if self._closed:
            raise RepoClosed("数据库连接已关闭（程序正在退出）")

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        # 全部 SQL 从这一个口子出去，因此锁加在这里就够——
        # 不用在每个方法里各写一遍 with（漏一处就是偶发的 locked）。
        with DB_LOCK:
            self._guard()
            return self.conn.execute(sql, tuple(params))

    def _one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        # 读也要把"执行 + 取回"整段握在锁里：只锁 execute 的话，游标一离开锁就可能
        # 被别的线程在同一条连接上顶掉——表现为刚插入的行查不到（偶发 NOT_FOUND），
        # 极端情况下直接抛 sqlite3.InterfaceError: bad parameter or other API misuse。
        with DB_LOCK:
            self._guard()
            row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with DB_LOCK:
            self._guard()
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- 任务

    def job_create(
        self,
        job_id: str,
        job_type: str,
        project_id: str | None = None,
        payload: Any = None,
    ) -> dict[str, Any]:
        stamp = now_iso()
        self._exec(
            "INSERT INTO wb_job (id, project_id, type, status, stage, progress,"
            " payload_json, attempts, cancel_requested, created_at)"
            " VALUES (?,?,?,'queued',NULL,0,?,0,0,?)",
            (job_id, project_id, job_type, _dumps(payload), stamp),
        )
        return self.job_get(job_id) or {}

    def job_get(self, job_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wb_job WHERE id=?", (job_id,))
        if row:
            row["payload"] = _loads(row.pop("payload_json", None))
            row["result"] = _loads(row.pop("result_json", None))
            row["cancel_requested"] = bool(row.get("cancel_requested"))
        return row

    def job_update(self, job_id: str, **fields: Any) -> None:
        """只更新传入的列。显式白名单，避免前端塞一个 ``id`` 进来把主键改掉。"""
        allowed = {
            "status",
            "stage",
            "progress",
            "result",
            "error_code",
            "error_message",
            "attempts",
            "cancel_requested",
            "started_at",
            "finished_at",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            column = f"{key}_json" if key == "result" else key
            if key == "result":
                value = _dumps(value)
            elif key == "cancel_requested":
                value = 1 if value else 0
            sets.append(f"{column}=?")
            params.append(value)
        if not sets:
            return
        params.append(job_id)
        self._exec(f"UPDATE wb_job SET {', '.join(sets)} WHERE id=?", params)

    def merge_mark_interrupted(self) -> int:
        """把上次退出时还在跑的合并记录标成失败。

        不标的话那行永远停在 ``running``：合并页会显示"合并中"，用户既等不到结果，
        也没有重试入口——比直接报失败更让人无措。
        """
        cur = self._exec(
            "UPDATE wb_merge SET status='failed', error=? WHERE status='running'",
            ("上次退出时这次合并还没跑完（可以重新开始）",),
        )
        return int(cur.rowcount or 0)

    def job_list(
        self,
        statuses: Iterable[str] | None = None,
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wb_job"
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            marks = ",".join("?" for _ in statuses)
            where.append(f"status IN ({marks})")
            params.extend(statuses)
        if project_id:
            where.append("project_id=?")
            params.append(project_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = self._all(sql, params)
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json", None))
            row["result"] = _loads(row.pop("result_json", None))
            row["cancel_requested"] = bool(row.get("cancel_requested"))
        return rows

    def job_mark_interrupted(self) -> int:
        """把上次进程退出时还在 running 的任务标为失败。

        不标的话它们会永远停在"处理中"，而用户看到的表现是"进度条卡住"，
        没有任何地方能点重试。
        """
        stamp = now_iso()
        cur = self._exec(
            "UPDATE wb_job SET status='failed', error_code='INTERRUPTED',"
            " error_message='上次退出时这个任务还没跑完（可以重试）', finished_at=?"
            " WHERE status IN ('running','queued')",
            (stamp,),
        )
        return cur.rowcount or 0

    # ------------------------------------------------------------- 项目

    def project_create(self, **fields: Any) -> dict[str, Any]:
        stamp = now_iso()
        data = {
            "id": fields.get("id") or new_id("pj"),
            "title": fields["title"],
            "source_kind": fields.get("source_kind", "bilibili"),
            "bvid": fields.get("bvid"),
            "aid": fields.get("aid"),
            "cid": fields.get("cid"),
            "page_no": fields.get("page_no"),
            "up_mid": fields.get("up_mid"),
            "up_name": fields.get("up_name"),
            "collection_id": fields.get("collection_id"),
            "collection_title": fields.get("collection_title"),
            # 合集内序号：0 起。下载 worker 从解析结果带过来；单稿/多P 为 None。
            "collection_index": fields.get("collection_index"),
            "root_dir": fields["root_dir"],
            "stage": fields.get("stage", "new"),
            "status": fields.get("status", "idle"),
            "error": fields.get("error"),
            "created_at": stamp,
            "updated_at": stamp,
        }
        columns = ",".join(data)
        marks = ",".join("?" for _ in data)
        self._exec(f"INSERT INTO wb_project ({columns}) VALUES ({marks})", list(data.values()))
        return self.project_get(data["id"]) or {}

    def project_get(self, project_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM wb_project WHERE id=?", (project_id,))

    def project_find_by_bvid(self, bvid: str, cid: int | None = None) -> dict[str, Any] | None:
        """按 bvid(+cid) 找已有项目——「重复添加同一集」是最高频的误操作。"""
        if cid is None:
            return self._one(
                "SELECT * FROM wb_project WHERE bvid=? ORDER BY created_at LIMIT 1", (bvid,)
            )
        return self._one("SELECT * FROM wb_project WHERE bvid=? AND cid=?", (bvid, cid))

    def project_list(
        self,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        stage: str = "",
        include_archived: bool = False,
        sort: str = "created_desc",
        collection: str = "",
        group: str = "",
    ) -> list[dict[str, Any]]:
        """项目列表。前三个位置参数保持老签名（query, limit, offset）——调用方遍地都是。"""
        order = SORTS.get(str(sort or "created_desc"))
        if order is None:
            raise WBError(
                "VALIDATION",
                f"不支持的排序：{sort}",
                "可选：" + " / ".join(SORTS),
            )
        where, params = _project_where(
            query,
            stage=stage,
            include_archived=include_archived,
            collection=collection,
            group=group,
        )
        sql = f"SELECT * FROM wb_project{where} ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        return self._all(sql, params)

    def project_count(
        self,
        query: str = "",
        stage: str = "",
        include_archived: bool = False,
        collection: str = "",
        group: str = "",
    ) -> int:
        where, params = _project_where(
            query,
            stage=stage,
            include_archived=include_archived,
            collection=collection,
            group=group,
        )
        row = self._one(f"SELECT COUNT(*) AS n FROM wb_project{where}", params)
        return int(row["n"]) if row else 0

    def project_title_rows(
        self,
        query: str = "",
        stage: str = "",
        include_archived: bool = False,
        collection: str = "",
        group: str = "",
    ) -> list[dict[str, Any]]:
        """title 排序用的轻量投影。

        只取排需要的四列：1000 条项目如果每条都把 `SELECT *`（含 error/root_dir 这些
        长文本）拉出来，光解码就比排序本身贵；这里给 service 算自然序用。
        """
        where, params = _project_where(
            query,
            stage=stage,
            include_archived=include_archived,
            collection=collection,
            group=group,
        )
        return self._all(
            "SELECT id, title, created_at, collection_index FROM wb_project"
            f"{where} ORDER BY created_at DESC, id DESC",
            params,
        )

    def project_stage_counts(
        self,
        query: str = "",
        include_archived: bool = False,
        collection: str = "",
        group: str = "",
    ) -> list[dict[str, Any]]:
        """一次 GROUP BY 拿到每个阶段的条数（facets）。

        **不带 stage 过滤**：facets 回答的是「当前搜索下每个阶段各有多少」，
        如果先按 stage 过滤，结果只会剩一个桶，点击其他阶段就没数了。
        """
        where, params = _project_where(
            query,
            include_archived=include_archived,
            collection=collection,
            group=group,
        )
        rows = self._all(
            f"SELECT stage, COUNT(*) AS n FROM wb_project{where} GROUP BY stage", params
        )
        return [{"stage": str(row["stage"] or ""), "count": int(row["n"])} for row in rows]

    def project_archive(self, ids: Sequence[str], archived: bool = True) -> dict[str, Any]:
        """批量归档/取消归档。逐 id 校验，缺的进 skipped，不整体失败。

        已处于目标状态的行不重复写时间戳也不计数——重复点「归档」是幂等的，
        否则每次都会产生一个新的 archived_at 和一次 library.changed 事件。
        """
        wanted = list(dict.fromkeys(str(i) for i in ids))
        if not wanted:
            return {"changed": 0, "skipped": []}
        marks = ",".join("?" for _ in wanted)
        rows = self._all(
            f"SELECT id, archived_at FROM wb_project WHERE id IN ({marks})", wanted
        )
        state = {str(row["id"]): row["archived_at"] for row in rows}
        skipped = [{"id": pid, "reason": "项目不存在"} for pid in wanted if pid not in state]
        stamp = now_iso()
        archived_at = stamp if archived else None
        changed = 0
        with DB_LOCK:
            self._guard()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for pid in wanted:
                    if pid not in state:
                        continue
                    if bool(state[pid]) is bool(archived):
                        continue
                    self.conn.execute(
                        "UPDATE wb_project SET archived_at=?, updated_at=? WHERE id=?",
                        (archived_at, stamp, pid),
                    )
                    changed += 1
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {"changed": changed, "skipped": skipped}

    def project_get_many(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        """按 id 批量取整行（title 排序先算 id、再一次性取回，避免 N+1）。"""
        wanted = list(dict.fromkeys(str(i) for i in ids))
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self._all(f"SELECT * FROM wb_project WHERE id IN ({marks})", wanted)
        by_id = {str(row["id"]): row for row in rows}
        return [by_id[pid] for pid in wanted if pid in by_id]

    def project_related_counts(self, ids: Sequence[str]) -> dict[str, int]:
        """删除预览要的三个数字（只 COUNT，不拉内容）。"""
        wanted = list(dict.fromkeys(str(i) for i in ids))
        if not wanted:
            return {"with_draft": 0, "with_clip": 0, "with_candidate": 0}
        marks = ",".join("?" for _ in wanted)
        out: dict[str, int] = {}
        for key, sql in (
            (
                "with_draft",
                "SELECT COUNT(DISTINCT project_id) AS n FROM wb_publish_note"
                f" WHERE status='draft' AND project_id IN ({marks})",
            ),
            (
                "with_clip",
                f"SELECT COUNT(DISTINCT project_id) AS n FROM wb_clip WHERE project_id IN ({marks})",
            ),
            (
                "with_candidate",
                "SELECT COUNT(DISTINCT project_id) AS n FROM wb_candidate"
                f" WHERE project_id IN ({marks})",
            ),
        ):
            row = self._one(sql, wanted)
            out[key] = int(row["n"]) if row else 0
        return out

    def project_related_rows(self, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """删除前要把三张关联表的内容留证：删完再查就只剩空表了。

        为什么连 `wb_candidate` 也留：切片候选里有用户手调过的 start/end 与备注，
        重建一次分析要花模型钱，删错了没有副本就只能重跑。
        """
        wanted = list(dict.fromkeys(str(i) for i in ids))
        if not wanted:
            return {"publish_note": [], "clip": [], "candidate": []}
        marks = ",".join("?" for _ in wanted)
        return {
            "publish_note": self._all(
                f"SELECT * FROM wb_publish_note WHERE project_id IN ({marks})"
                " ORDER BY created_at, id",
                wanted,
            ),
            "clip": self._all(
                f"SELECT * FROM wb_clip WHERE project_id IN ({marks}) ORDER BY created_at, id",
                wanted,
            ),
            "candidate": self._all(
                f"SELECT * FROM wb_candidate WHERE project_id IN ({marks})"
                " ORDER BY created_at, id",
                wanted,
            ),
        }

    def project_delete_many(self, ids: Sequence[str]) -> int:
        """批量删除。**单事务**：中途失败整批回滚，不留下"删了一半"的库。"""
        wanted = list(dict.fromkeys(str(i) for i in ids))
        if not wanted:
            return 0
        marks = ",".join("?" for _ in wanted)
        with DB_LOCK:
            self._guard()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(f"DELETE FROM wb_project WHERE id IN ({marks})", wanted)
                removed = cur.rowcount or 0
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return removed

    def project_update(self, project_id: str, **fields: Any) -> None:
        # aid 在里面是因为下载 worker 需要"重新下载一集就把 av 号补回来"：
        # 早期版本组装项目字段时漏了它，老项目的这一列全是 NULL，
        # 而发布笔记必须拿 aid 当 oid。bvid/cid 仍**不在**白名单里——
        # 那两列决定"这一行代表哪一集"，改它们等于把项目指向另一个视频。
        allowed = {
            "title",
            "aid",
            "stage",
            "status",
            "error",
            "root_dir",
            "collection_id",
            "collection_title",
            # 合集内序号允许更新：老项目是在 v5 之前导入的，这一列全是 NULL，
            # 回填脚本要靠 project_update 把它补上。
            "collection_index",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        params.extend([now_iso(), project_id])
        self._exec(f"UPDATE wb_project SET {', '.join(sets)} WHERE id=?", params)

    def project_set_publish_video(
        self, project_id: str, *, bvid: str | None, aid: int | None
    ) -> None:
        """只写“发布视频号”绑定，不碰项目身份列。

        ``bvid``/``aid`` 是下载去重身份（``project_find_by_bvid`` 靠它们认“同一集”），
        本地项目永远不该写进去；发布卡填的号只属于发布链路，落在 ``publish_*``。
        """
        self._exec(
            "UPDATE wb_project SET publish_bvid=?, publish_aid=?, updated_at=? WHERE id=?",
            [bvid, aid, now_iso(), project_id],
        )

    def project_delete(self, project_id: str) -> None:
        """删项目。资产/字幕/切片靠外键 ON DELETE CASCADE 一起走，不在这里手写。"""
        self._exec("DELETE FROM wb_project WHERE id=?", [project_id])

    # ------------------------------------------------------------- 资产

    ASSET_FIELDS = (
        "bytes",
        "duration_ms",
        "width",
        "height",
        "fps",
        "vcodec",
        "acodec",
        "language",
        "status",
    )

    def asset_upsert(self, project_id: str, kind: str, path: str, **fields: Any) -> dict[str, Any]:
        """一个项目里同类资产按路径唯一。**只用于多值 kind**（``subtitle`` / ``export``）。

        写 ``source`` 这类"一个项目只该有一份"的资产请改用
        :meth:`asset_upsert_singleton`——本方法的去重键含 path，而源视频文件名跟着视频
        标题走，改名会插出第二行并被 :meth:`asset_list` 的升序读取遮蔽成"权威行"。
        """
        row = self._one(
            "SELECT * FROM wb_asset WHERE project_id=? AND kind=? AND path=?",
            (project_id, kind, path),
        )
        values = {k: v for k, v in fields.items() if k in self.ASSET_FIELDS}
        if row:
            if values:
                sets = ", ".join(f"{k}=?" for k in values)
                self._exec(
                    f"UPDATE wb_asset SET {sets} WHERE id=?",
                    [*values.values(), row["id"]],
                )
                row = self._one("SELECT * FROM wb_asset WHERE id=?", (row["id"],)) or row
            return row

        asset_id = new_id("as")
        data = {
            "id": asset_id,
            "project_id": project_id,
            "kind": kind,
            "path": path,
            "status": values.pop("status", "ready"),
            "created_at": now_iso(),
            **values,
        }
        columns = ",".join(data)
        marks = ",".join("?" for _ in data)
        self._exec(f"INSERT INTO wb_asset ({columns}) VALUES ({marks})", list(data.values()))
        return self._one("SELECT * FROM wb_asset WHERE id=?", (asset_id,)) or {}

    def asset_upsert_singleton(
        self, project_id: str, kind: str, path: str, **fields: Any
    ) -> dict[str, Any]:
        """**单例 kind** 专用（``source`` 这类"一个项目只该有一份"的资产）。

        身份键是 ``(project_id, kind)``，**不含 path**。为什么不能沿用 :meth:`asset_upsert`：
        它的去重条件带 path，而媒体文件名现在跟着视频标题走——UP 改标题、换下载后端、
        换扩展名都会插出第二行 ``kind=source``。而 :meth:`asset_list` 按 ``created_at``
        **升序**返回，读取侧（``service._decorate`` 的 ``setdefault``、各处 ``next(...)``）
        取的是**最早**那行 → 指向死文件的旧行会遮蔽真实文件：素材库标红 missing，
        转录/剪辑/导出拿到不存在的路径，且不报任何错。

        这里把同 kind 的所有行统一改写到新路径（不删行：``wb_transcript.asset_id``
        外键指向资产行，删行会把转录记录的来源抹成 ``NULL``）。多值 kind
        （``subtitle`` / ``export``）不该用这个方法——它们的身份确实含 path。
        """
        values = {k: v for k, v in fields.items() if k in self.ASSET_FIELDS}
        rows = self._all(
            "SELECT * FROM wb_asset WHERE project_id=? AND kind=? ORDER BY created_at DESC, id DESC",
            (project_id, kind),
        )
        if rows:
            sets = ", ".join(["path=?", *(f"{k}=?" for k in values)])
            self._exec(
                f"UPDATE wb_asset SET {sets} WHERE project_id=? AND kind=?",
                [path, *values.values(), project_id, kind],
            )
            return self._one("SELECT * FROM wb_asset WHERE id=?", (rows[0]["id"],)) or rows[0]

        asset_id = new_id("as")
        data = {
            "id": asset_id,
            "project_id": project_id,
            "kind": kind,
            "path": path,
            "status": values.pop("status", "ready"),
            "created_at": now_iso(),
            **values,
        }
        columns = ",".join(data)
        marks = ",".join("?" for _ in data)
        self._exec(f"INSERT INTO wb_asset ({columns}) VALUES ({marks})", list(data.values()))
        return self._one("SELECT * FROM wb_asset WHERE id=?", (asset_id,)) or {}

    def asset_list(self, project_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM wb_asset WHERE project_id=? ORDER BY created_at",
            (project_id,),
        )

    def asset_set_status(self, asset_id: str, status: str) -> None:
        self._exec("UPDATE wb_asset SET status=? WHERE id=?", (status, asset_id))

    # ------------------------------------------------------------- 转录

    def transcript_upsert(
        self,
        project_id: str,
        *,
        segments: list[dict[str, Any]],
        language: str = "",
        model: str = "",
        srt_path: str = "",
        asset_id: str | None = None,
        status: str = "ready",
    ) -> dict[str, Any]:
        """一个项目只保留一条"当前转录"。

        不按 (project, model, language) 存多份的理由很简单：转录结果一旦有了一份，
        它就是这份视频的正文。再转一次是"替换"，不是"多一个版本"——多版本会让
        P2-03 的编辑器不知道该编辑哪一份。历史留在磁盘上的 transcript.json，
        数据库只认最新的。
        """
        self._exec("DELETE FROM wb_transcript WHERE project_id=?", (project_id,))
        row = {
            "id": new_id("tr"),
            "project_id": project_id,
            "asset_id": asset_id,
            "language": language,
            "model": model,
            "segments_json": _dumps(segments),
            "srt_path": srt_path,
            "status": status,
            "created_at": now_iso(),
        }
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        self._exec(f"INSERT INTO wb_transcript ({columns}) VALUES ({marks})", list(row.values()))
        return self.transcript_get(project_id) or {}

    def transcript_get(self, project_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM wb_transcript WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        if not row:
            return None
        row["segments"] = _loads(row.pop("segments_json", None)) or []
        return row

    def transcript_clear(self, project_id: str) -> int:
        cur = self._exec("DELETE FROM wb_transcript WHERE project_id=?", (project_id,))
        return cur.rowcount if cur else 0

    # ------------------------------------------------------------- 字幕派生表
    #
    # ``wb_subtitle`` 存的是**派生字幕**（某种语言的译文、双语合并结果、导出产物），
    # 不是原始字幕——原始字幕永远在 ``wb_transcript``。这个区分很重要：
    # 派生物可以被覆盖、可以只有一半（翻译失败几段），原始正文不行。
    # 唯一键取 (project_id, kind, language)：重译同一种语言是替换，不是加版本。

    def subtitle_upsert(
        self,
        project_id: str,
        *,
        kind: str,
        language: str = "",
        format: str = "json",
        segments: list[dict[str, Any]] | None = None,
        path: str = "",
        style_id: str = "",
        status: str = "ready",
    ) -> dict[str, Any]:
        row = {
            "id": new_id("sub"),
            "project_id": project_id,
            "kind": kind,
            "language": language,
            "format": format,
            "path": path,
            "segments_json": _dumps(segments or []),
            "style_id": style_id,
            "status": status,
            "updated_at": now_iso(),
        }
        self._exec(
            "DELETE FROM wb_subtitle WHERE project_id=? AND kind=? AND language=?",
            (project_id, kind, language),
        )
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        self._exec(f"INSERT INTO wb_subtitle ({columns}) VALUES ({marks})", list(row.values()))
        return self.subtitle_get(project_id, kind=kind, language=language) or {}

    def subtitle_get(
        self, project_id: str, *, kind: str, language: str = ""
    ) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM wb_subtitle WHERE project_id=? AND kind=? AND language=?"
            " ORDER BY updated_at DESC LIMIT 1",
            (project_id, kind, language),
        )
        if not row:
            return None
        row["segments"] = _loads(row.pop("segments_json", None)) or []
        return row

    def subtitle_list(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM wb_subtitle WHERE project_id=? ORDER BY kind, language", (project_id,)
        )
        for row in rows:
            row["segments"] = _loads(row.pop("segments_json", None)) or []
        return rows

    def subtitle_delete(self, project_id: str, *, kind: str, language: str = "") -> int:
        cur = self._exec(
            "DELETE FROM wb_subtitle WHERE project_id=? AND kind=? AND language=?",
            (project_id, kind, language),
        )
        return cur.rowcount if cur else 0

    # ------------------------------------------------------------- 总结版本
    #
    # ``wb_summary`` 与 ``wb_transcript`` 的存法**刻意不同**：转录是"只有一份正文，
    # 重转即替换"；总结是"每次生成/打磨都加一版"。因为总结是给人看的草稿，
    # 用户会想回去看"上一版是怎么写的"——删掉历史就等于让他没法反悔。
    #
    # 版本号按 (project_id, scope) 自增，删版本不重排（``version`` 只是显示用的序号，
    # 不是主键）。恢复旧版也是**新增一版**，见 service.summary_restore。

    def summary_next_version(self, project_id: str, scope: str = "video") -> int:
        row = self._one(
            "SELECT MAX(version) AS v FROM wb_summary WHERE project_id=? AND scope=?",
            (project_id, scope),
        )
        return int((row or {}).get("v") or 0) + 1

    def summary_insert(
        self,
        project_id: str,
        *,
        content: dict[str, Any],
        content_md: str,
        role: str = "",
        model: str = "",
        usage: Any = None,
        status: str = "draft",
        scope: str = "video",
        collection_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        number = version or self.summary_next_version(project_id, scope)
        row = {
            "id": new_id("sum"),
            "project_id": project_id,
            "collection_id": collection_id,
            "scope": scope,
            "role": role,
            "version": number,
            "content_md": content_md,
            "content_json": _dumps(content),
            "model": model,
            "usage_json": _dumps(usage) if usage is not None else "",
            "status": status,
            "created_at": now_iso(),
        }
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        self._exec(f"INSERT INTO wb_summary ({columns}) VALUES ({marks})", list(row.values()))
        return self.summary_get(str(row["id"])) or {}

    def summary_get(self, summary_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wb_summary WHERE id=?", (summary_id,))
        return self._summary_row(row)

    def summary_latest(self, project_id: str, scope: str = "video") -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM wb_summary WHERE project_id=? AND scope=? ORDER BY version DESC LIMIT 1",
            (project_id, scope),
        )
        return self._summary_row(row)

    def summary_list(
        self, project_id: str, scope: str = "video", limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM wb_summary WHERE project_id=? AND scope=? ORDER BY version DESC LIMIT ?",
            (project_id, scope, max(1, int(limit))),
        )
        return [item for item in (self._summary_row(row) for row in rows) if item]

    def summary_delete(self, summary_id: str) -> int:
        cur = self._exec("DELETE FROM wb_summary WHERE id=?", (summary_id,))
        return cur.rowcount if cur else 0

    def _summary_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        out = dict(row)
        out["content"] = _loads(out.pop("content_json", None)) or {}
        out["usage"] = _loads(out.pop("usage_json", None)) or {}
        out["version"] = int(out.get("version") or 1)
        text = str(out.get("content_md") or "")
        # 字数在这里算，不在前端算：中文没有空格分词，前端的 length 会把
        # 换行、Markdown 标记一起数进去，版本列表里两个数字对不上就成了 bug。
        out["chars"] = len("".join(text.split()))
        return out

    # ------------------------------------------------------------- 合集
    #
    # ``wb_collection`` 在 P1 一直是空表：那时只用 ``wb_project.collection_title``
    # 记住"这集属于哪个合集"，没有任何跨集的东西需要落库。P2-08 的总览需要一个
    # **稳定的锚**（每集都变的时候不能拿"第一集的 id"当锚），所以现在按标题生成
    # 一条合集记录，总览挂在它下面。
    #
    # id 由标题派生（``col_`` + 标题哈希前 12 位）而不是随机：同一个合集反复出总览
    # 必须落在同一行，否则版本号会从 1 重新数，用户看到"第 1 版"却已经出过三版。

    def collection_id_for(self, title: str) -> str:
        import hashlib

        digest = hashlib.sha1(str(title or "").strip().encode("utf-8")).hexdigest()
        return f"col_{digest[:12]}"

    def collection_upsert(
        self,
        *,
        title: str,
        kind: str = "collection",
        source_url: str = "",
        up_mid: int | None = None,
        up_name: str = "",
        root_dir: str = "",
    ) -> dict[str, Any]:
        key = self.collection_id_for(title)
        row = {
            "id": key,
            "title": str(title or "").strip(),
            "kind": kind or "collection",
            "source_url": source_url or "",
            "up_mid": up_mid,
            "up_name": up_name or "",
            "root_dir": root_dir or "",
        }
        existing = self.collection_get(key)
        if existing:
            # 只补空字段：合集名可能被用户改过（``collection_title`` 来自下载时的
            # 输入），已落库的 root_dir 是第一次生成总览时算出来的，不该被后来的
            # 单集覆盖成子目录。
            merged = dict(existing)
            for field in ("source_url", "up_name", "root_dir"):
                if not merged.get(field) and row.get(field):
                    merged[field] = row[field]
            if up_mid and not merged.get("up_mid"):
                merged["up_mid"] = up_mid
            self._exec(
                "UPDATE wb_collection SET source_url=?, up_mid=?, up_name=?, root_dir=? WHERE id=?",
                (
                    merged.get("source_url") or "",
                    merged.get("up_mid"),
                    merged.get("up_name") or "",
                    merged.get("root_dir") or "",
                    key,
                ),
            )
            return self.collection_get(key) or {}
        columns = ",".join(
            ["id", "title", "kind", "source_url", "up_mid", "up_name", "root_dir", "created_at"]
        )
        marks = ",".join("?" for _ in range(8))
        self._exec(
            f"INSERT INTO wb_collection ({columns}) VALUES ({marks})",
            [
                row["id"],
                row["title"],
                row["kind"],
                row["source_url"],
                row["up_mid"],
                row["up_name"],
                row["root_dir"],
                now_iso(),
            ],
        )
        return self.collection_get(key) or {}

    def collection_get(self, collection_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM wb_collection WHERE id=?", (collection_id,))

    def collection_list(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM wb_collection ORDER BY created_at DESC")

    def project_list_by_collection(self, title: str) -> list[dict[str, Any]]:
        """按合集名取子项，按下载顺序（``created_at`` 升序）——"第几集"靠它。"""
        return self._all(
            "SELECT * FROM wb_project WHERE collection_title=? ORDER BY created_at ASC, id ASC",
            (str(title or ""),),
        )

    def collection_titles(self) -> list[dict[str, Any]]:
        """库里出现过的合集名与集数。空名不算合集（单稿）。"""
        return self._all(
            "SELECT collection_title AS title, COUNT(*) AS items,"
            " SUM(CASE WHEN stage IN ('summarized','clipped','merged','published')"
            " THEN 1 ELSE 0 END) AS summarized"
            " FROM wb_project WHERE collection_title IS NOT NULL AND collection_title<>''"
            " GROUP BY collection_title ORDER BY MAX(created_at) DESC"
        )

    def collection_summary_next_version(self, collection_id: str) -> int:
        row = self._one(
            "SELECT MAX(version) AS v FROM wb_summary WHERE collection_id=? AND scope='collection'",
            (collection_id,),
        )
        return int((row or {}).get("v") or 0) + 1

    def collection_summary_insert(
        self,
        collection_id: str,
        *,
        content: dict[str, Any],
        content_md: str,
        role: str = "",
        model: str = "",
        usage: Any = None,
        status: str = "ready",
        version: int | None = None,
    ) -> dict[str, Any]:
        number = version or self.collection_summary_next_version(collection_id)
        row = {
            "id": new_id("sum"),
            "project_id": None,
            "collection_id": collection_id,
            "scope": "collection",
            "role": role,
            "version": number,
            "content_md": content_md,
            "content_json": _dumps(content),
            "model": model,
            "usage_json": _dumps(usage) if usage is not None else "",
            "status": status,
            "created_at": now_iso(),
        }
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        self._exec(f"INSERT INTO wb_summary ({columns}) VALUES ({marks})", list(row.values()))
        return self.summary_get(str(row["id"])) or {}

    def collection_summary_latest(self, collection_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM wb_summary WHERE collection_id=? AND scope='collection'"
            " ORDER BY version DESC LIMIT 1",
            (collection_id,),
        )
        return self._summary_row(row)

    def collection_summary_list(self, collection_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM wb_summary WHERE collection_id=? AND scope='collection'"
            " ORDER BY version DESC LIMIT ?",
            (collection_id, max(1, int(limit))),
        )
        return [item for item in (self._summary_row(row) for row in rows) if item]

    # ------------------------------------------------------------- 发布草稿
    #
    # ``wb_publish_note`` 的行有两种命运：一直停在 draft（用户只存不发的常态），
    # 或者在 Phase 5 变成 published。这里只负责草稿的增删改查——
    # **不写 published_at / withdrawn_at**，那两个时间戳只有在真的调过平台接口
    # 之后才有意义，提前填等于伪造记录。
    #
    # 一个项目只留一份「当前草稿」：重新生成 = 覆盖这一行的标题/摘要/正文。
    # 保留多份草稿会让「我到底要发哪一版」变成一道选择题，而发布这个动作
    # 本身就是一次性的——真需要留底，总结的版本列表已经在库里了。

    def publish_note_upsert(
        self,
        project_id: str,
        *,
        bvid: str,
        title: str,
        summary: str = "",
        content: str = "",
        aid: int | None = None,
        note_id: str | None = None,
        note_pk: str = "",
    ) -> dict[str, Any]:
        """存/更新一份草稿。

        ``note_pk`` 给的是**行 id**（``note_xxx``）；``note_id`` 是 B站 侧的笔记 id，
        只有发布成功才会有。两者刻意分开：把平台 id 当本地主键，撤回时就找不到行了。
        """
        existing = self.publish_get(note_pk) if note_pk else self.publish_for_project(project_id)
        # 兜底：note_pk 指向别的项目的行时不当成「要改的那份」，免得 UPDATE 把
        # 另一个项目的草稿覆盖掉（服务层已有同类校验，这里防直接调 repo 的路径）。
        if existing and str(existing.get("project_id") or "") != str(project_id):
            existing = None
        # 只改"还没发出去的"那一份。项目里最新一条已经是 published / withdrawn 时，
        # 再走 update 会把**发出去的那份记录**改掉——那既是改历史，又让后面的
        # "同视频不能重复发"判断失效（它按 status=published 找，找到的是被改脏的同一行）。
        if existing and str(existing.get("status", "")) != "draft" and not note_pk:
            existing = None
        if existing:
            self._exec(
                "UPDATE wb_publish_note SET bvid=?, aid=?, title=?, summary=?, content=?,"
                " updated_at=? WHERE id=?",
                (bvid, aid, title, summary, content, now_iso(), existing["id"]),
            )
            return self.publish_get(str(existing["id"])) or {}
        row = {
            "id": new_id("draft"),
            "project_id": project_id,
            "bvid": bvid,
            "aid": aid,
            "note_id": note_id,
            "title": title,
            "summary": summary,
            "content": content,
            "status": "draft",
            "error": "",
            "published_at": None,
            "updated_at": now_iso(),
            "withdrawn_at": None,
            "created_at": now_iso(),
        }
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        self._exec(f"INSERT INTO wb_publish_note ({columns}) VALUES ({marks})", list(row.values()))
        return self.publish_get(str(row["id"])) or {}

    def publish_get(self, note_pk: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM wb_publish_note WHERE id=?", (note_pk,))

    def publish_for_project(self, project_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM wb_publish_note WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )

    def publish_list(self, project_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """草稿列表，带项目标题——列表页要显示"这是哪个视频的笔记"。"""
        sql = (
            "SELECT n.*, p.title AS project_title, p.up_name AS up_name"
            " FROM wb_publish_note n LEFT JOIN wb_project p ON p.id = n.project_id"
        )
        args: list[Any] = []
        if project_id:
            sql += " WHERE n.project_id=?"
            args.append(project_id)
        sql += " ORDER BY n.created_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return self._all(sql, tuple(args))

    def publish_published(self, bvid: str = "") -> list[dict[str, Any]]:
        """已发布且未撤回的笔记（门禁第三关要用）。

        单独一个查询而不是拿 publish_list 过滤：列表是按创建时间取前 N 条的，
        一个发了 200 份草稿的项目里，真正发过的那条可能根本不在列表里。
        """
        sql = "SELECT * FROM wb_publish_note WHERE status='published' AND withdrawn_at IS NULL"
        args: list[Any] = []
        if bvid:
            sql += " AND bvid=?"
            args.append(bvid)
        sql += " ORDER BY published_at DESC, created_at DESC"
        return self._all(sql, tuple(args))

    def publish_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """发布记录：发过、撤过、发失败的都算，草稿不算（草稿在草稿列表里）。

        排序用「最近一次动作」的时间：撤回过的按 withdrawn_at 往后排，
        否则一条上周发的、今天撤回的笔记会沉到列表中间，用户以为没撤成功。
        """
        sql = (
            "SELECT n.*, p.title AS project_title, p.up_name AS up_name"
            " FROM wb_publish_note n LEFT JOIN wb_project p ON p.id = n.project_id"
            " WHERE n.status IN ('published','withdrawn','failed')"
            " ORDER BY COALESCE(NULLIF(n.withdrawn_at,''), NULLIF(n.published_at,''), n.updated_at, n.created_at) DESC"
            " LIMIT ?"
        )
        return self._all(sql, (max(1, int(limit)),))

    def publish_delete(self, note_pk: str) -> int:
        cur = self._exec("DELETE FROM wb_publish_note WHERE id=?", (note_pk,))
        return cur.rowcount if cur else 0

    def publish_set_status(
        self, note_pk: str, status: str, *, note_id: str | None = None, error: str = ""
    ) -> None:
        """Phase 5 发布成功后改状态用。现在只有测试会碰它。"""
        now = now_iso()
        if status == "published":
            self._exec(
                "UPDATE wb_publish_note SET status=?, note_id=?, error=?, published_at=? WHERE id=?",
                (status, note_id, error, now, note_pk),
            )
        elif status == "withdrawn":
            self._exec(
                "UPDATE wb_publish_note SET status=?, withdrawn_at=? WHERE id=?",
                (status, now, note_pk),
            )
        else:
            self._exec(
                "UPDATE wb_publish_note SET status=?, error=? WHERE id=?", (status, error, note_pk)
            )

    # ------------------------------------------------------------- 切片候选

    def candidate_insert(
        self,
        project_id: str,
        *,
        kind: str,
        start_ms: int,
        end_ms: int,
        score: int = 0,
        title: str = "",
        reason: str = "",
        quote: str = "",
        tags: Sequence[str] | None = None,
        source: str = "ai",
        status: str = "draft",
        notes: Any = None,
        copy: Any = None,
    ) -> dict[str, Any]:
        candidate_id = new_id("cand")
        self._exec(
            "INSERT INTO wb_candidate (id, project_id, kind, start_ms, end_ms, score, title,"
            " reason, quote, tags_json, source, status, notes_json, copy_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                candidate_id,
                project_id,
                kind,
                int(start_ms),
                int(end_ms),
                int(score),
                title,
                reason,
                quote,
                _dumps(list(tags or [])) or "[]",
                source,
                status,
                _dumps(notes),
                _dumps(copy) if copy else None,
                now_iso(),
            ),
        )
        return self.candidate_get(candidate_id) or {}

    def candidate_get(self, candidate_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wb_candidate WHERE id=?", (candidate_id,))
        return _candidate_row(row) if row else None

    def candidate_list(
        self,
        project_id: str,
        *,
        kind: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wb_candidate WHERE project_id=?"
        params: list[Any] = [project_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY score DESC, start_ms ASC, id ASC"
        return [_candidate_row(row) for row in self._all(sql, params)]

    def candidate_update(self, candidate_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "kind",
            "start_ms",
            "end_ms",
            "score",
            "title",
            "reason",
            "quote",
            "status",
            "notes",
            "tags",
            "copy",
        }
        target = {key: value for key, value in fields.items() if key in allowed}
        if not target:
            return self.candidate_get(candidate_id) or {}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in target.items():
            if key == "notes":
                sets.append("notes_json=?")
                params.append(_dumps(value))
            elif key == "tags":
                sets.append("tags_json=?")
                params.append(_dumps(list(value or [])) or "[]")
            elif key == "copy":
                # 列名是 copy_json；直接拼 key 会变成 `copy=?`，SQL 报 no such column
                sets.append("copy_json=?")
                params.append(_dumps(dict(value or {})) or "{}")
            else:
                sets.append(f"{key}=?")
                params.append(value)
        params.append(candidate_id)
        self._exec(f"UPDATE wb_candidate SET {', '.join(sets)} WHERE id=?", params)
        return self.candidate_get(candidate_id) or {}

    def candidate_delete(self, candidate_id: str) -> bool:
        cursor = self._exec("DELETE FROM wb_candidate WHERE id=?", (candidate_id,))
        return cursor.rowcount > 0

    def candidate_clear(self, project_id: str, *, keep_locked: bool = True) -> int:
        """重新分析前清掉旧的 AI 候选。

        `status='locked'` 与 `source='manual'` 的保留——用户花时间改过的东西
        不能因为"再分析一次"就消失（P3-04 的验收就是这么要求的）。
        """
        sql = "DELETE FROM wb_candidate WHERE project_id=? AND source='ai'"
        if keep_locked:
            sql += " AND status!='locked'"
        cursor = self._exec(sql, (project_id,))
        return cursor.rowcount

    # ------------------------------------------------------------- 合并

    def merge_insert(
        self,
        *,
        items: list[Any],
        mode: str,
        preflight: Any = None,
        project_id: str | None = None,
        status: str = "running",
        output_path: str = "",
    ) -> dict[str, Any]:
        """记一条合并。`fallback_used` 留给 P4-03：快速失败切精确时置 1。"""
        merge_id = new_id("mg")
        self._exec(
            "INSERT INTO wb_merge"
            " (id, project_id, items_json, mode, preflight_json, fallback_used,"
            "  output_path, status, error, created_at)"
            " VALUES (?,?,?,?,?,0,?,?,?,?)",
            (
                merge_id,
                project_id,
                _dumps(items) or "[]",
                mode,
                _dumps(preflight),
                output_path,
                status,
                "",
                now_iso(),
            ),
        )
        return self.merge_get(merge_id) or {}

    def merge_update(self, merge_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "mode",
            "status",
            "error",
            "output_path",
            "fallback_used",
            "preflight_json",
            "items_json",
            "project_id",
        }
        pairs = {k: v for k, v in fields.items() if k in allowed}
        if pairs:
            cols = ", ".join(f"{k}=?" for k in pairs)
            self._exec(
                f"UPDATE wb_merge SET {cols} WHERE id=?",
                tuple(pairs.values()) + (merge_id,),
            )
        row = self.merge_get(merge_id)
        if not row:
            raise WBError("NOT_FOUND", "合并记录不存在", merge_id)
        return row

    def merge_get(self, merge_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wb_merge WHERE id=?", (merge_id,))
        return _merge_row(row) if row else None

    def merge_list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM wb_merge ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        )
        return [_merge_row(r) for r in rows]

    # ------------------------------------------------------------- 样式

    def style_list(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM wb_style ORDER BY builtin DESC, name")
        for row in rows:
            row["config"] = _loads(row.pop("config_json", None)) or {}
            row["builtin"] = bool(row.get("builtin"))
        return rows

    def style_get(self, key: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM wb_style WHERE id=? OR name=?", (key, key))
        if row:
            row["config"] = _loads(row.pop("config_json", None)) or {}
            row["builtin"] = bool(row.get("builtin"))
        return row

    def style_upsert(self, style_id: str, name: str, mode: str, config: Any) -> dict[str, Any]:
        builtin_style = self.style_get(style_id)
        if builtin_style and builtin_style.get("builtin"):
            raise WBError("CONFLICT", "内置样式不可修改")
        self._exec(
            "INSERT INTO wb_style (id, name, mode, config_json, builtin, created_at)"
            " VALUES (?,?,?,?,0,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, mode=excluded.mode,"
            " config_json=excluded.config_json",
            (style_id, name, mode, _dumps(config) or "{}", now_iso()),
        )
        return self.style_get(style_id) or {}

    def style_delete(self, key: str) -> dict[str, Any]:
        """删自建样式。内置只读——被删掉后所有引用它的项目会掉回兜底值。"""
        row = self.style_get(key)
        if not row:
            raise WBError("NOT_FOUND", "样式不存在")
        if row.get("builtin"):
            raise WBError("CONFLICT", "内置样式不可删除")
        self._exec("DELETE FROM wb_style WHERE id=?", (str(row.get("id")),))
        return row
