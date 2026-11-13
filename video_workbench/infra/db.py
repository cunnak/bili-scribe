"""SQLite 连接、建表、迁移（方案书 6.7）。

**这是与 bili-scribe 共用的库**，边界必须写死：

- 只读它的 ``task`` / ``publish`` / ``summary_version``；
- 只写自己的 ``wb_*`` 表；
- **不替它跑迁移**、不改它的表结构。

迁移是幂等的（全部 ``IF NOT EXISTS``），因此每次启动都能安全执行。
首次迁移前备份，因为"共享库"意味着出错的不只是我们自己。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from video_workbench.app.errors import WBError

SCHEMA_VERSION = 7

# 一个连接要在三类线程间共享：WebView 的 RPC 回调线程、后台任务线程、
# 事件推送线程。sqlite3 的连接对象本身不是线程安全的，所以**所有 SQL 都过这把锁**。
# 用 RLock 而不是 Lock：仓储方法之间会互相调用（style_upsert → style_get）。
DB_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS wb_collection (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  source_url TEXT,
  up_mid INTEGER,
  up_name TEXT,
  root_dir TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_project (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  bvid TEXT, aid INTEGER, cid INTEGER, page_no INTEGER,
  publish_bvid TEXT, publish_aid INTEGER,
  up_mid INTEGER, up_name TEXT,
  collection_id TEXT REFERENCES wb_collection(id) ON DELETE SET NULL,
  collection_title TEXT,
  root_dir TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'new',
  status TEXT NOT NULL DEFAULT 'idle',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_asset (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES wb_project(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  bytes INTEGER,
  duration_ms INTEGER,
  width INTEGER, height INTEGER,
  fps REAL,
  vcodec TEXT, acodec TEXT,
  language TEXT,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_transcript (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES wb_project(id) ON DELETE CASCADE,
  asset_id TEXT REFERENCES wb_asset(id) ON DELETE SET NULL,
  language TEXT,
  model TEXT,
  segments_json TEXT NOT NULL,
  srt_path TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_subtitle (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES wb_project(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  language TEXT,
  format TEXT NOT NULL,
  path TEXT,
  segments_json TEXT,
  style_id TEXT,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_summary (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES wb_project(id) ON DELETE CASCADE,
  collection_id TEXT REFERENCES wb_collection(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  role TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  content_md TEXT,
  content_json TEXT,
  model TEXT,
  usage_json TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_candidate (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES wb_project(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  score INTEGER DEFAULT 0,
  title TEXT,
  reason TEXT,
  quote TEXT,
  tags_json TEXT,
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  notes_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_clip (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES wb_project(id) ON DELETE CASCADE,
  candidate_id TEXT REFERENCES wb_candidate(id) ON DELETE SET NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  aspect TEXT NOT NULL,
  width INTEGER, height INTEGER,
  vertical_mode TEXT,
  crop_x REAL DEFAULT 0.5,
  focus_json TEXT,
  subtitle_mode TEXT NOT NULL,
  style_id TEXT,
  output_path TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_merge (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES wb_project(id) ON DELETE SET NULL,
  items_json TEXT NOT NULL,
  mode TEXT NOT NULL,
  preflight_json TEXT,
  fallback_used INTEGER DEFAULT 0,
  output_path TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_publish_note (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES wb_project(id) ON DELETE CASCADE,
  bvid TEXT NOT NULL,
  aid INTEGER,
  note_id TEXT,
  title TEXT NOT NULL,
  summary TEXT,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  published_at TEXT,
  withdrawn_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_style (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL,
  config_json TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_job (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES wb_project(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT,
  progress REAL DEFAULT 0,
  payload_json TEXT,
  result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  attempts INTEGER DEFAULT 0,
  cancel_requested INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_wb_project_bvid ON wb_project(bvid, cid);
CREATE INDEX IF NOT EXISTS idx_wb_project_collection ON wb_project(collection_id);
-- 素材库列表要按项目查资产（_decorate 每条项目一次），没有这条索引就是全表扫。
CREATE INDEX IF NOT EXISTS idx_wb_asset_project ON wb_asset(project_id);
CREATE INDEX IF NOT EXISTS idx_wb_job_status ON wb_job(status, created_at);
CREATE INDEX IF NOT EXISTS idx_wb_candidate_project ON wb_candidate(project_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_wb_clip_project ON wb_clip(project_id, created_at);
"""

# 元信息表。用它记 schema_version 而不是单独一张单列表：
# 以后要加「上次清理时间」「上次备份路径」之类的东西不用再建表。
_META_DDL = """
CREATE TABLE IF NOT EXISTS wb_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


# 迁移时一起种入的内置样式。放这里而不是 Phase 3，
# 是因为「样式表非空」是后续所有渲染路径的前提（NULL style_id 会让 FFmpeg 参数为空）。
#
# P3-07 起内置样式是**嵌套结构**（方案书 5.11.2：primary/secondary/rounded/preview）。
# 定义真源在 ``video_workbench.core.styles``——这里延迟导入，避免 infra→core 的
# 模块级循环（core.styles 又要用 app.errors）。**不做 _BUILTIN_STYLES 字面量副本**：
# 两份 JSON 迟早对不上，那正是「样式页 48、导出 54」的成因。
def _builtin_style_rows() -> list[tuple[str, str, str, str, int]]:
    from ..core import styles as style_presets

    rows = []
    for preset in style_presets.builtin_presets():
        rows.append(
            (
                str(preset["id"]),
                str(preset["name"]),
                str(preset["mode"]),
                json.dumps(preset, ensure_ascii=False),
                1,
            )
        )
    return rows


def now_iso() -> str:
    """统一 ISO8601 **UTC**。混用本地时间会让跨时区的排序错乱，
    而"晚上 8 点"和"12:00Z"看起来都像正常值，问题会藏很久。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接。``timeout=3`` 对应方案书的"等 3 秒再试一次"策略。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False + DB_LOCK：跨线程共享同一个连接（见上方说明）。
    conn = sqlite3.connect(str(path), timeout=3.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 桌面单进程场景下 NORMAL 足够：WAL 下只有断电才可能丢最后几条，
    # 而 FULL 会让每条 INSERT 都 fsync，任务进度写入会明显拖慢。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _backup(db_path: Path) -> Path | None:
    """迁移前备份。文件名带时间戳，避免第二次迁移覆盖掉第一次的好副本。

    失败**不吞**：调用方（`migrate`）负责把它变成 WBError 并中止迁移——
    没有一份可回滚的副本就动共享库，是拿 bili-scribe 的数据在赌。
    """
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    target = backup_target(db_path)
    shutil.copy2(db_path, target)
    return target


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM wb_meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        # 还没建 wb_meta，说明是全新库
        return 0
    return int(row["value"]) if row else 0


# v2：`wb_candidate.notes_json` 存吸附过程的原值与原因。放 JSON 一列而不是拆成四个
# 强类型列，因为它们是"这次分析怎么算出来的"过程记录，P3-04 之后可能还会加字段，
# 而 `start_ms/end_ms` 这些是要参与查询的坐标必须独立成列。
_V2_COLUMNS = {
    "wb_candidate": {"notes_json": "TEXT"},
}

# v3：切片文案（标题/简介/标签/封面路径）。整块塞一个 JSON 列而不是拆四列——
# 文案是"一坨要一起读一起写"的东西，拆开只会让 update 白名单越来越长。
_V3_COLUMNS = {
    "wb_candidate": {"copy_json": "TEXT"},
}

# v4：草稿的"更新时间"。列表里要显示它（方案书 §5.9.1），而 created_at 只在
# 第一次生成时写一次——用户改完标题再看列表，看到的还是三天前，等于没更新。
_V4_COLUMNS = {
    "wb_publish_note": {"updated_at": "TEXT"},
}

# v5：素材库信息架构。archived_at 是"软删除"的时间戳（NULL=未归档，归档才有值），
# 用它而不是 status 里加一个值：status 已经承担任务态（idle/queued/running/failed），
# 混进去会让所有按 status 过滤的路径都要多想一步。
# collection_index 是"这条在合集中的第几个"，用来按合集顺序排；平台给的下标是 0 起。
_V5_COLUMNS = {
    "wb_project": {"archived_at": "TEXT", "collection_index": "INTEGER"},
}

# v6：本地导入项目的“发布视频号”。不能写进 bvid/aid——那两列是下载去重身份，
# 下载 worker 用 project_find_by_bvid 找已有项目；本地视频一旦被写上 bvid，
# 后续同 bv 的下载会把这条本地项目当成“同一集”去更新。
# publish_* 只服务发布链路：为空时回落到 bvid/aid，已有下载项目不受影响。
_V6_COLUMNS = {
    "wb_project": {"publish_bvid": "TEXT", "publish_aid": "INTEGER"},
}

# v7：单视频的字幕样式**整份覆盖**。NULL = 继承全局（这是默认，也是"查看不创建覆盖"
# 的落法）；非 NULL = 一份完整 config JSON（含 base_preset_id）。
# 不做逐字段继承：半覆盖会让"我改了字号但颜色怎么没跟过来"变成常态问题。
_V7_COLUMNS = {
    "wb_project": {"subtitle_style_json": "TEXT"},
}

# 所有补列桶，**按版本顺序**排。加版本时必须往这里补一个桶（哪怕空 dict）——
# 下面的检查是"忘了写迁移"的牙：schema_version 涨了但没登记桶，import 直接失败。
_ALL_COLUMN_BUCKETS = (
    _V2_COLUMNS,
    _V3_COLUMNS,
    _V4_COLUMNS,
    _V5_COLUMNS,
    _V6_COLUMNS,
    _V7_COLUMNS,
)


def _assert_column_buckets() -> None:
    """schema 版本与补列桶的绑定检查。

    单独抽成函数是为了能让测试「删掉一个桶再显式调用」——import 期只调用一次，
    import 之后再改模块属性不会重跑，没法证明这条牙还在。
    """
    expected = SCHEMA_VERSION - 1
    actual = len(_ALL_COLUMN_BUCKETS)
    if actual != expected:
        raise RuntimeError(
            f"SCHEMA_VERSION={SCHEMA_VERSION}，但登记了 {actual} 个补列桶；"
            "每把 SCHEMA_VERSION 加一版，必须往 _ALL_COLUMN_BUCKETS 末尾补一个桶"
            "（这一版没有新列也要补空 dict，用来证明迁移被想过了）"
        )


_assert_column_buckets()


def _merged_columns() -> dict[str, dict[str, str]]:
    """所有桶按版本顺序合并成 {表: {列: 类型}}。"""
    merged: dict[str, dict[str, str]] = {}
    for bucket in _ALL_COLUMN_BUCKETS:
        for table, columns in bucket.items():
            merged.setdefault(table, {}).update(columns)
    return merged


def pending_columns(conn: sqlite3.Connection) -> list[str]:
    """只读：现在跑 migrate() 会补哪些列（`表.列` 形式）。

    `--migrate-check` 用它打印"将补的列"，不执行任何 ALTER。表还不存在的
    不算补列——那种情况 DDL 会整张表建出来。
    """
    out: list[str] = []
    for table, columns in _merged_columns().items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:  # pragma: no cover - 表不存在时交给 DDL
            continue
        if not rows:
            continue
        have = {row[1] for row in rows}
        out.extend(f"{table}.{name}" for name in columns if name not in have)
    return out


def backup_target(db_path: str | Path) -> Path:
    """迁移会写到哪个备份文件（只算路径，不复制）。`--migrate-check` 用它报落点。"""
    path = Path(db_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.bak-{stamp}")


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """给老库补新列（SQLite 没有 ADD COLUMN IF NOT EXISTS）。

    只补自己表；不动 bili-scribe 的表。返回补过哪些列，便于日志里说清楚。
    """
    added: list[str] = []
    merged = _merged_columns()
    for table, columns in merged.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:  # pragma: no cover - 表不存在时交给 DDL
            continue
        have = {row[1] for row in rows}
        for name, kind in columns.items():
            if name in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
            added.append(f"{table}.{name}")
    return added


def migrate(conn: sqlite3.Connection, db_path: str | Path | None = None) -> dict:
    """建表 + 记版本。幂等，可反复执行。

    返回 ``{"from": 旧版本, "to": 新版本, "backup": 备份路径或 None}``。
    """
    before = schema_version(conn)
    backup_path = None
    if before < SCHEMA_VERSION and db_path is not None:
        # 只要这次真会改结构就备份（不只是"从无到有"）：v4→v5 会 ALTER 共享库，
        # 出问题时受害者不只是我们自己的表。空的/不存在的库 _backup 返回 None。
        try:
            backup_path = _backup(Path(db_path))
        except OSError as exc:
            raise WBError(
                "IO",
                "迁移前备份失败，已中止（数据库未被改动）",
                f"{type(exc).__name__}: {exc}",
            ) from exc

    conn.execute(_META_DDL)
    # executescript 自带隐式 COMMIT，不能放在事务里，所以这里不用 BEGIN。
    conn.executescript(_DDL)
    added = _add_missing_columns(conn)

    stamp = now_iso()
    for style_id, name, mode, config_json, builtin in _builtin_style_rows():
        # 内置行**覆盖式**写入：P3-07 把 default 从平铺字段换成了嵌套结构，
        # 用 INSERT OR IGNORE 会让老库永远停在旧结构上。用户自建样式（builtin=0）不碰。
        conn.execute(
            "INSERT INTO wb_style (id, name, mode, config_json, builtin, created_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, mode=excluded.mode,"
            " config_json=excluded.config_json"
            " WHERE wb_style.builtin=1",
            (style_id, name, mode, config_json, builtin, stamp),
        )

    conn.execute(
        "INSERT INTO wb_meta (key, value) VALUES ('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return {
        "from": before,
        "to": SCHEMA_VERSION,
        "backup": str(backup_path) if backup_path else None,
        "columns": added,
    }


def open_and_migrate(db_path: str | Path) -> sqlite3.Connection:
    """带"被占用则等 3 秒重试一次"的连接。

    被 bili-scribe 占着时**不强杀进程**：那是别人的进程，杀它等于把我们的问题
    转嫁给正在用的用户。宁可提示一句"请先关闭 bili-scribe"。
    """
    path = Path(db_path)
    last: Exception | None = None
    for attempt in (0, 1):
        try:
            conn = connect(path)
            migrate(conn, path)
            return conn
        except sqlite3.OperationalError as exc:  # pragma: no cover - 依赖外部状态
            last = exc
            if "locked" not in str(exc).lower() or attempt == 1:
                break
            time.sleep(3)
    raise WBError(
        "CONFLICT",
        "数据库被占用，请先关闭 bili-scribe 后重试",
        str(last) if last else "",
    )
