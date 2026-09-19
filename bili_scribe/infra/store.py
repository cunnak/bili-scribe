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

CREATE INDEX IF NOT EXISTS idx_publish_bvid ON publish(bvid, status);
CREATE INDEX IF NOT EXISTS idx_task_created ON task(created_at DESC);
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

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = []
        values: list[Any] = []
        for name, value in fields.items():
            if name in self._JSON_FIELDS and not isinstance(value, (str, type(None))):
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{name} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_now())
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
