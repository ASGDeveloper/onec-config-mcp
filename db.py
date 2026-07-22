import sqlite3
from pathlib import Path

_DDL = [
    """CREATE TABLE IF NOT EXISTS objects (
        id          INTEGER PRIMARY KEY,
        config_name TEXT NOT NULL,
        obj_type    TEXT NOT NULL,
        obj_name    TEXT NOT NULL,
        xml_path    TEXT,
        xml_summary TEXT,
        index_info  TEXT,
        UNIQUE(config_name, obj_type, obj_name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_objects_name ON objects(obj_name)",
    "CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(obj_type, config_name)",
    """CREATE TABLE IF NOT EXISTS modules (
        id          INTEGER PRIMARY KEY,
        object_id   INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
        module_type TEXT NOT NULL,
        form_name   TEXT,
        file_path   TEXT NOT NULL UNIQUE,
        content     TEXT NOT NULL,
        line_count  INTEGER NOT NULL DEFAULT 0,
        xml_summary TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_modules_object ON modules(object_id)",
    # External content table: fts_modules stores only the FTS index, not a
    # second copy of `content`/`xml_summary` - values are fetched from
    # `modules` by rowid on demand. Halves storage for the dominant cost
    # (full BSL source text) compared to a standalone FTS5 table.
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_modules USING fts5(
        module_type UNINDEXED,
        form_name   UNINDEXED,
        file_path   UNINDEXED,
        content,
        xml_summary,
        content='modules',
        content_rowid='id',
        tokenize="unicode61 remove_diacritics 1"
    )""",
    """CREATE TRIGGER IF NOT EXISTS modules_ai AFTER INSERT ON modules BEGIN
        INSERT INTO fts_modules(rowid, module_type, form_name, file_path, content, xml_summary)
        VALUES (NEW.id, NEW.module_type, COALESCE(NEW.form_name, ''), NEW.file_path, NEW.content, COALESCE(NEW.xml_summary, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS modules_ad AFTER DELETE ON modules BEGIN
        INSERT INTO fts_modules(fts_modules, rowid, module_type, form_name, file_path, content, xml_summary)
        VALUES ('delete', OLD.id, OLD.module_type, COALESCE(OLD.form_name, ''), OLD.file_path, OLD.content, COALESCE(OLD.xml_summary, ''));
    END""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_objects USING fts5(
        obj_name,
        xml_summary,
        content='objects',
        content_rowid='id',
        tokenize="unicode61 remove_diacritics 1"
    )""",
    """CREATE TRIGGER IF NOT EXISTS objects_ai AFTER INSERT ON objects BEGIN
        INSERT INTO fts_objects(rowid, obj_name, xml_summary)
        VALUES(NEW.id, NEW.obj_name, COALESCE(NEW.xml_summary, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS objects_ad AFTER DELETE ON objects BEGIN
        INSERT INTO fts_objects(fts_objects, rowid, obj_name, xml_summary)
        VALUES ('delete', OLD.id, OLD.obj_name, COALESCE(OLD.xml_summary, ''));
    END""",
    """CREATE TABLE IF NOT EXISTS index_runs (
        config_name TEXT PRIMARY KEY,
        indexed_at  TEXT NOT NULL,
        file_count  INTEGER,
        obj_count   INTEGER
    )""",
]


def get_connection(db_path: str, timeout: float = 5.0) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_DROP_TRIGGERS = [
    "DROP TRIGGER IF EXISTS modules_ai",
    "DROP TRIGGER IF EXISTS modules_ad",
    "DROP TRIGGER IF EXISTS objects_ai",
    "DROP TRIGGER IF EXISTS objects_ad",
]


def _migrate_index_info(conn: sqlite3.Connection) -> None:
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='objects'"
    )}
    if not tables:
        return  # fresh DB, _DDL will create the up-to-date schema

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(objects)")}
    if "index_info" in columns:
        return  # already migrated

    conn.execute("ALTER TABLE objects ADD COLUMN index_info TEXT")
    # Existing rows have no index_info; force a full reindex of every config
    # so it gets populated.
    conn.execute("DELETE FROM index_runs")


def ensure_schema(db_path: str) -> None:
    conn = get_connection(db_path)
    for stmt in _DROP_TRIGGERS:
        conn.execute(stmt)
    _migrate_index_info(conn)
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
