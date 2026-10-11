"""本地存档：登录凭据、任务记录、发布记录与去重表。

去重表是这个应用的安全核心之一。B站 评论接口对相同内容重复发送会返回
12051（"重复评论，请勿刷屏"），触发它意味着已经踩到风控线。与其等平台拒绝，
不如在发请求之前就用本地记录拦下来。

凭据用 Fernet 对称加密后落盘，密钥单独存放在另一个文件里。这挡不住能读你
整个用户目录的攻击者，但能保证数据库文件被单独拷走时凭据不可读。没装
cryptography 时会退回明文并打警告——功能优先，但风险必须说出来。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credential (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    BLOB    NOT NULL,
    encrypted  INTEGER NOT NULL DEFAULT 1,
    mid        TEXT,
    uname      TEXT,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id           TEXT PRIMARY KEY,
    raw_input    TEXT,
    bvid         TEXT,
    aid          INTEGER,
    cid          INTEGER,
    page         INTEGER,
    title        TEXT,
    up_name      TEXT,
    up_mid       INTEGER,
    status       TEXT NOT NULL,
    progress     TEXT,
    source_kind  TEXT,
    source_label TEXT,
    source_trust TEXT,
    warnings     TEXT,
    candidates   TEXT,
    summary_json TEXT,
    comment_text TEXT,
    preview_md   TEXT,
    platform_summary TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT,
    bvid       TEXT NOT NULL,
    aid        INTEGER,
    rpid       TEXT,
    content    TEXT,
    status     TEXT NOT NULL,
    message    TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

-- 总结的版本史。**这个表必须落库，不能像素材笔记那样只放内存。**
-- 区别在于「能不能重建」：素材笔记丢了重新预读就能算回来（只是贵一点），
-- 而一段已经生成、已经被人逐条改过的文字，重跑模型不会给出同一个结果——
-- 那是丢劳动，不是丢缓存。程序重启一次就把人改的五版全抹掉，是不可接受的。
CREATE TABLE IF NOT EXISTS summary_version (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT    NOT NULL,
    version_no   INTEGER NOT NULL,
    source       TEXT    NOT NULL,   -- initial 初版 / refine 按要求改的 / restore 以旧版为底稿
    requirement  TEXT,               -- 这一版是被什么要求改出来的（初版为空）
    role         TEXT,
    summary_json TEXT,
    preview_md   TEXT,
    comment_text TEXT,
    -- 这一版的消耗：{usage: {...}, chunks, frames, model, elapsed}。JSON 而不是拆成列，
    -- 因为这些字段会随着流水线变化增删，而它们只是给人看的账，不参与查询。
    metrics      TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publish_bvid ON publish(bvid, status);
CREATE INDEX IF NOT EXISTS idx_task_created ON task(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_version_task ON summary_version(task_id, version_no);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str, key_path: str, logger=None):
        self.db_path = db_path
        self.key_path = key_path
        self.log = logger or (lambda msg: None)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._cipher = self._load_cipher()
        self._init_schema()

    # ------------------------------------------------------------------ 连接

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ 加密

    def _load_cipher(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            self.log(
                "未安装 cryptography，登录凭据将以明文存放于数据库。"
                "建议执行 pip install cryptography 后重新登录。"
            )
            return None
        key_file = Path(self.key_path)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
        return Fernet(key_file.read_bytes().strip())

    def _encrypt(self, data: dict[str, Any]) -> tuple[bytes, int]:
        blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if self._cipher is None:
            return blob, 0
        return self._cipher.encrypt(blob), 1

    def _decrypt(self, payload: bytes, encrypted: int) -> dict[str, Any]:
        if not encrypted or self._cipher is None:
            return json.loads(payload.decode("utf-8"))
        return json.loads(self._cipher.decrypt(payload).decode("utf-8"))

    # ------------------------------------------------------------- 凭据读写

    def save_credential(self, cookies: dict[str, str], mid: str = "", uname: str = "") -> None:
        payload, encrypted = self._encrypt(cookies)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO credential (id, payload, encrypted, mid, uname, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    encrypted = excluded.encrypted,
                    mid = excluded.mid,
                    uname = excluded.uname,
                    updated_at = excluded.updated_at
                """,
                (payload, encrypted, mid, uname, _now()),
            )

    def get_credential(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM credential WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            cookies = self._decrypt(row["payload"], row["encrypted"])
        except Exception as exc:  # 密钥换了或数据损坏
            self.log(f"凭据解密失败，已忽略：{exc}")
            return None
        return {
            "cookies": cookies,
            "mid": row["mid"],
            "uname": row["uname"],
            "updated_at": row["updated_at"],
        }

    def clear_credential(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM credential WHERE id = 1")

    # ---------------------------------------------------------------- KV

    def kv_set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def kv_get(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # --------------------------------------------------------------- 任务

    @staticmethod
    def new_task_id() -> str:
        return uuid.uuid4().hex[:12]

    def create_task(self, raw_input: str) -> str:
        task_id = self.new_task_id()
        stamp = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task (id, raw_input, status, progress, created_at, updated_at)
                VALUES (?, ?, 'queued', '已接受请求，排队中', ?, ?)
                """,
                (task_id, raw_input, stamp, stamp),
            )
        return task_id

    _JSON_FIELDS = {"warnings", "candidates", "summary_json"}

    def _task_columns(self, fields: dict[str, Any]) -> tuple[list[str], list[Any]]:
        """把「字段＝值」摊成 SQL 赋值片段。update_task 与 record_version 共用。

        合成一份而不是各写一遍：版本落库要在**同一条语句/同一个事务**里既写版本
        又更新任务当前状态，两处若各拼一套，迟早会有一处漏掉 JSON 序列化。
        """
        assignments: list[str] = []
        values: list[Any] = []
        for name, value in fields.items():
            if name in self._JSON_FIELDS and not isinstance(value, (str, type(None))):
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{name} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_now())
        return assignments, values

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments, values = self._task_columns(fields)
        values.append(task_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE task SET {', '.join(assignments)} WHERE id = ?", values
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        return self._decode_task(row)

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [t for t in (self._decode_task(r) for r in rows) if t]

    def _decode_task(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for name in self._JSON_FIELDS:
            raw = data.get(name)
            if not raw:
                data[name] = [] if name != "summary_json" else None
                continue
            try:
                data[name] = json.loads(raw)
            except (TypeError, ValueError):
                data[name] = None
        return data

    # ------------------------------------------------------------- 版本史

    # 版本从哪儿来。restore 和另外两种不是一回事：它**不调用模型**，
    # 只是把某一版全文重新设为当前版（所以它没有消耗，也不该被算成"未记录"）。
    VERSION_SOURCES = ("initial", "refine", "restore")

    _VERSION_JSON_FIELDS = {"summary_json", "metrics"}

    def record_version(
        self,
        task_id: str,
        *,
        source: str,
        summary_json: Any,
        preview_md: str = "",
        comment_text: str = "",
        requirement: str = "",
        role: str = "",
        metrics: dict[str, Any] | None = None,
        **task_fields: Any,
    ) -> dict[str, Any]:
        """追加一个版本，并把它设为该任务的「当前版」。整件事在一个事务里完成。

        **为什么要合成一个事务**：版本表和 task 表都要写，而它们必须一致。
        分开写的话，进程在两次写之间被杀，就会留下「任务显示新内容、版本史里却没有
        它」这种自相矛盾的状态——而版本史存在的全部意义就是可信。

        **旧记录的自愈**：升级前生成的任务只有 task.summary_json，没有任何版本行。
        这里在确实一条版本都没有、而任务已有总结时，把它补成第 1 版再追加新版本。
        这样老任务第一次迭代后就能看到完整的两版，不必专门写迁移脚本。

        版本号用 `MAX(version_no) + 1` 在**同一条 SQL 里**算，不先读再写——
        先读再写会让两个并发的迭代抢到同一个号，而唯一索引会把它变成一次
        莫名其妙的失败。
        """
        if source not in self.VERSION_SOURCES:
            raise ValueError(f"未知的版本来源：{source}")
        stamp = _now()
        payload = (
            summary_json
            if isinstance(summary_json, str)
            else json.dumps(summary_json, ensure_ascii=False)
        )
        blob = json.dumps(metrics, ensure_ascii=False) if metrics else None

        with self._connect() as conn:
            # BEGIN IMMEDIATE：下面要先读后写，普通事务会先取共享锁、写时再升级，
            # 两个并发迭代就可能双双升级失败（database is locked）。直接拿写锁。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT summary_json, preview_md, comment_text, created_at "
                "FROM task WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)

            backfilled = self._backfill_initial(conn, task_id, row)

            cur = conn.execute(
                """
                INSERT INTO summary_version
                    (task_id, version_no, source, requirement, role,
                     summary_json, preview_md, comment_text, metrics, created_at)
                VALUES (?, (SELECT COALESCE(MAX(version_no), 0) + 1
                            FROM summary_version WHERE task_id = ?),
                        ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task_id,
                    source,
                    requirement,
                    role,
                    payload,
                    preview_md,
                    comment_text,
                    blob,
                    stamp,
                ),
            )
            version_no = conn.execute(
                "SELECT version_no FROM summary_version WHERE id = ?",
                (int(cur.lastrowid),),
            ).fetchone()["version_no"]

            # 新版本一旦写入，它就是「当前版」——所以任务表里那三份当前内容
            # 必须跟着换。这不是顺手同步：少更新一处，界面就会出现"版本史里是
            # 新版、发布框里是旧版"这种自相矛盾，而它看起来像模型没改。
            fields: dict[str, Any] = {
                "summary_json": payload,  # 已是字符串，_task_columns 不会再序列化一遍
                "preview_md": preview_md,
                "comment_text": comment_text,
            }
            fields.update(task_fields)
            assignments, values = self._task_columns(fields)
            values.append(task_id)
            conn.execute(
                f"UPDATE task SET {', '.join(assignments)} WHERE id = ?", values
            )

        return {
            "task_id": task_id,
            "version_no": version_no,
            "source": source,
            "backfilled_previous": backfilled,
            "created_at": stamp,
        }

    @staticmethod
    def _backfill_initial(conn: sqlite3.Connection, task_id: str, row: sqlite3.Row) -> bool:
        """把「升级前生成、还没有版本行」的旧总结补成第 1 版。

        时间用任务的创建时间：真正的生成时刻当时没有记录，而 created_at 至少是
        「不晚于它」的一个稳定下界，比此刻的时间诚实。metrics 留空，界面上会显示
        「未记录消耗」——那时确实没有记账这回事，用 0 冒名顶替才是说谎。

        **来源要按内容自己说的话来定**：升级前已经改过版的任务，它的 summary_json
        里带着 `revise_note`（"我这次改了什么"）。把这种记录标成「初次生成」，
        标签就和内容自己说的话对不上——这种小谎最难被发现，因为它看起来很正常。
        （这一条是活体验收时从真实库里看出来的：那条老任务的第 1 版一边写着"初次生成"，
        一边带着一句"根据修改要求，检查发现……"。）

        调用方负责事务与锁（record_version 在自己的事务里调它）。
        """
        if not row["summary_json"]:
            return False
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM summary_version WHERE task_id = ?", (task_id,)
        ).fetchone()["n"]
        if existing:
            return False
        summary = Store._decode_field(row["summary_json"], {}) or {}
        revised = bool(summary.get("revise_note"))
        conn.execute(
            """
            INSERT INTO summary_version
                (task_id, version_no, source, requirement, role,
                 summary_json, preview_md, comment_text, metrics, created_at)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                task_id,
                "refine" if revised else "initial",
                # 当时的要求没有被记下来（那时还没有"要求"这个概念），如实说而不是留白
                "（旧记录：当时的要求没有留存）" if revised else "",
                # 角色从旧的 summary_json 里取：task 表没有这一列
                summary.get("role", ""),
                row["summary_json"],
                row["preview_md"] or "",
                row["comment_text"] or "",
                row["created_at"],
            ),
        )
        return True

    def ensure_initial_version(self, task_id: str) -> bool:
        """读的时候顺手把旧记录补成第 1 版。

        读接口写库通常是要避免的，这里是个有意的例外：没有迁移框架、而"升级前的
        任务打开后版本史是空的"是一种让人困惑的坏状态（明明有总结，却显示没有版本）。
        它幂等、只在「有总结且零版本」时触发一次，触发后永远不再发生。
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT summary_json, preview_md, comment_text, created_at "
                "FROM task WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            return self._backfill_initial(conn, task_id, row)

    def latest_version_no(self, task_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version_no) AS n FROM summary_version WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def list_versions(self, task_id: str) -> list[dict[str, Any]]:
        """按时间顺序返回全部版本。**含全文**——调用方要按体积自己裁剪。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM summary_version WHERE task_id = ? ORDER BY version_no ASC",
                (task_id,),
            ).fetchall()
        return [self._decode_version(row) for row in rows]

    def get_version(self, task_id: str, version_no: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM summary_version WHERE task_id = ? AND version_no = ?",
                (task_id, version_no),
            ).fetchone()
        return self._decode_version(row) if row else None

    @classmethod
    def _decode_version(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for name in cls._VERSION_JSON_FIELDS:
            data[name] = cls._decode_field(data.get(name), None)
        return data

    @staticmethod
    def _decode_field(raw: Any, empty: Any) -> Any:
        if not raw:
            return empty
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return empty

    # --------------------------------------------------------------- 发布

    def add_publish(
        self,
        task_id: str,
        bvid: str,
        aid: int | None,
        rpid: str | None,
        content: str,
        status: str,
        message: str = "",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO publish (task_id, bvid, aid, rpid, content, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, bvid, aid, rpid, content, status, message, _now()),
            )
            return int(cur.lastrowid)

    def find_live_publish(self, bvid: str) -> dict[str, Any] | None:
        """返回该视频尚未撤回的发布记录，用于去重。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM publish WHERE bvid = ? AND status = 'published' "
                "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (bvid,),
            ).fetchone()
        return dict(row) if row else None

    def last_publish_at(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM publish WHERE status = 'published' "
                "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["created_at"] if row else None

    def mark_publish_deleted(self, publish_id: int, message: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE publish SET deleted_at = ?, message = ? WHERE id = ?",
                (_now(), message, publish_id),
            )

    def list_publishes(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM publish ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_publish(self, publish_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM publish WHERE id = ?", (publish_id,)
            ).fetchone()
        return dict(row) if row else None
