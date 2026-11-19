import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 窗界默认值沿用原始出厂种子：截止 2026-11-01，早于今天，装起来即处于「窗界已过期」；
# 种子批次业务日（2026-11-13/14）全在窗外，正是本题「窗外批仍能落标」的现场。
# 修复后这些批次落标会被中文短句拦下；要落标须由调度改窗把窗界延到覆盖业务日。
DEFAULT_WINDOW_START = "2026-11-01"
DEFAULT_WINDOW_END = "2026-11-01"
# 旧版把窗界写在 meta.window_end。迁移时原值并入唯一窗界行（不擅自延长），
# 之后走 API 改窗会把 meta 键删掉，只剩 window_state 一行。
LEGACY_META_KEYS = ("window_start", "window_end", "window_revision")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_db(db_path: str | None = None):
    """写事务：BEGIN IMMEDIATE 先拿排他锁，避免双人调度同时改窗界行。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  effective_from TEXT NOT NULL DEFAULT '2026-11-01',
  effective_to TEXT NOT NULL DEFAULT '2026-11-30',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  template_version INTEGER,
  stamped_window_start TEXT,
  stamped_window_end TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  window_snapshot TEXT,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS window_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  actor TEXT,
  origin TEXT NOT NULL DEFAULT 'default',
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS window_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision INTEGER NOT NULL,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  actor TEXT NOT NULL,
  changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行", "2026-11-01", "2026-11-30"),
    # 换版生效窗：v2 从 12-01 起生效，11 月的批次不得被 v2 顶上落标。
    ("dw_orders_d", "wm_batch", 2, "现行", "2026-12-01", "2026-12-31"),
    ("dw_payments_d", "wm_batch", 1, "现行", "2026-11-01", "2026-11-30"),
    ("dm_gmv_d", "wm_trace", 1, "现行", "2026-11-01", "2026-11-30"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None, None, None, None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None, None, None, None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None, None, None, None),
    (
        "dw_orders_d",
        "ord-20261113-01",
        "2026-11-13",
        "已盖印",
        "WM-ORD-131",
        1,
        "2026-11-01",
        "2026-11-30",
    ),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]

_TEMPLATES_V2 = """
CREATE TABLE watermark_templates_v2 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  effective_from TEXT NOT NULL DEFAULT '2026-11-01',
  effective_to TEXT NOT NULL DEFAULT '2026-11-30',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str, cols: set[str]
) -> None:
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        cols.add(column)


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容旧库：补生效窗列、放开单表唯一约束、收敛窗界双源、回填盖印快照。"""
    tcols = _columns(conn, "watermark_templates")
    _add_column(
        conn,
        "watermark_templates",
        "effective_from",
        "TEXT NOT NULL DEFAULT '2026-11-01'",
        tcols,
    )
    _add_column(
        conn,
        "watermark_templates",
        "effective_to",
        "TEXT NOT NULL DEFAULT '2026-11-30'",
        tcols,
    )

    # 旧版表把 table_name 设为 UNIQUE，导致一个表只能有一版；换版生效窗需要多版共存。
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='watermark_templates'"
    ).fetchone()
    if row and "table_name TEXT NOT NULL UNIQUE" in (row["sql"] or ""):
        conn.execute(_TEMPLATES_V2)
        conn.execute(
            "INSERT INTO watermark_templates_v2"
            "(id, table_name, column_name, version_no, status, effective_from, effective_to, created_at) "
            "SELECT id, table_name, column_name, version_no, status, effective_from, effective_to, created_at "
            "FROM watermark_templates"
        )
        conn.execute("DROP TABLE watermark_templates")
        conn.execute("ALTER TABLE watermark_templates_v2 RENAME TO watermark_templates")

    bcols = _columns(conn, "run_batches")
    _add_column(conn, "run_batches", "template_version", "INTEGER", bcols)
    _add_column(conn, "run_batches", "stamped_window_start", "TEXT", bcols)
    _add_column(conn, "run_batches", "stamped_window_end", "TEXT", bcols)

    gcols = _columns(conn, "gap_reports")
    _add_column(conn, "gap_reports", "window_snapshot", "TEXT", gcols)

    # 旧库列上的空值回填到版式 v1 的生效窗，而不是沿用（可能已过期的）当前窗界；
    # 模板生效窗本身就是这一版「能不能用」的第一手依据。
    conn.execute(
        "UPDATE watermark_templates SET effective_from=? "
        "WHERE effective_from IS NULL OR effective_from=''",
        ("2026-11-01",),
    )
    conn.execute(
        "UPDATE watermark_templates SET effective_to=? "
        "WHERE effective_to IS NULL OR effective_to=''",
        ("2026-11-30",),
    )

    if conn.execute("SELECT value FROM meta WHERE key='schema_window_v2'").fetchone():
        return

    # 窗界双源收敛：旧 meta 键（若存在）原值迁入唯一窗界行，不擅自延长。
    meta = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM meta WHERE key IN (?,?,?)", LEGACY_META_KEYS
        ).fetchall()
    }
    start = (meta.get("window_start") or DEFAULT_WINDOW_START).strip()
    end = (meta.get("window_end") or DEFAULT_WINDOW_END).strip()
    try:
        revision = max(0, int(meta.get("window_revision") or 0))
    except ValueError:
        revision = 0
    origin = "meta" if meta else "seed"
    conn.execute(
        "INSERT OR IGNORE INTO window_state"
        "(id, start_date, end_date, revision, actor, origin, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, NULL)",
        (start, end, max(1, revision + 1), "migrate", origin),
    )
    conn.execute(
        "INSERT INTO window_events(revision, window_start, window_end, actor) "
        "SELECT revision, start_date, end_date, ? FROM window_state "
        "WHERE id=1 AND NOT EXISTS (SELECT 1 FROM window_events)",
        (f"窗界初始化（{origin}）",),
    )

    # 旧库的已盖印行没有版本号与窗界快照，按覆盖业务日的版式回填。
    conn.execute(
        "UPDATE run_batches SET template_version=("
        "  SELECT t.version_no FROM watermark_templates t "
        "  WHERE t.table_name = run_batches.table_name "
        "    AND t.effective_from <= run_batches.biz_date "
        "    AND t.effective_to >= run_batches.biz_date "
        "  ORDER BY (t.status='现行') DESC, t.version_no DESC, t.id DESC LIMIT 1"
        ") WHERE status='已盖印' AND template_version IS NULL"
    )
    # 旧库的盖印快照按「该版式当时的生效窗」回填：旧实现不看窗就写印，库里没留窗界，
    # 能追溯到的最接近口径就是盖印所用版式的生效窗；随后复验还会核对盖印时刻未越窗。
    conn.execute(
        "UPDATE run_batches SET "
        "stamped_window_start=("
        "  SELECT t.effective_from FROM watermark_templates t "
        "  WHERE t.table_name = run_batches.table_name "
        "    AND t.version_no = run_batches.template_version LIMIT 1), "
        "stamped_window_end=("
        "  SELECT t.effective_to FROM watermark_templates t "
        "  WHERE t.table_name = run_batches.table_name "
        "    AND t.version_no = run_batches.template_version LIMIT 1) "
        "WHERE status='已盖印' AND stamped_window_start IS NULL "
        "AND template_version IS NOT NULL"
    )
    # 迁移已把 meta 窗界键并入 window_state，旧键就地删除，避免迁移后仍被旧读覆盖
    # （尤其是出厂坏种子 window_end=2026-11-01）。运行期运维直写 meta 仍可作覆写通道。
    conn.execute(
        "DELETE FROM meta WHERE key IN ('window_start','window_end','window_revision')"
    )
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_window_v2','1')")


def _seed(conn: sqlite3.Connection) -> None:
    for table, col, ver, status, start, end in SEED_TEMPLATES:
        conn.execute(
            "INSERT OR IGNORE INTO watermark_templates"
            "(table_name, column_name, version_no, status, effective_from, effective_to) "
            "VALUES (?,?,?,?,?,?)",
            (table, col, ver, status, start, end),
        )
    for (
        table,
        code,
        biz,
        status,
        wm,
        ver,
        ws,
        we,
    ) in SEED_BATCHES:
        conn.execute(
            "INSERT OR IGNORE INTO run_batches"
            "(table_name, run_code, biz_date, status, watermark_value, template_version, "
            "stamped_window_start, stamped_window_end, stamped_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                table,
                code,
                biz,
                status,
                wm,
                ver,
                ws,
                we,
                "2026-11-13 18:00:00" if status == "已盖印" else None,
            ),
        )
    for up, down in SEED_EDGES:
        conn.execute(
            "INSERT OR IGNORE INTO lineage_edges(upstream_run_code, downstream_run_code) "
            "VALUES (?,?)",
            (up, down),
        )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        _seed(conn)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('seeded','1')")
