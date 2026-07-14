import sqlite3
from pathlib import Path

_DDL = [
    """CREATE TABLE IF NOT EXISTS objects (
        id          INTEGER PRIMARY KEY,
        config_name TEXT NOT NULL,
        obj_type    TEXT NOT NULL,
        obj_name    TEXT NOT NULL,
        is_ssl      INTEGER NOT NULL DEFAULT 0,
        xml_path    TEXT,
        xml_summary TEXT,
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
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_modules USING fts5(
        config_name UNINDEXED,
        obj_type    UNINDEXED,
        obj_name    UNINDEXED,
        is_ssl      UNINDEXED,
        module_type UNINDEXED,
        form_name   UNINDEXED,
        file_path   UNINDEXED,
        content,
        xml_summary,
        tokenize="unicode61 remove_diacritics 1"
    )""",
    # Insert fts_modules with rowid = modules.id so deletion is simply rowid = OLD.id
    """CREATE TRIGGER IF NOT EXISTS modules_ai AFTER INSERT ON modules BEGIN
        INSERT INTO fts_modules(rowid, config_name, obj_type, obj_name, is_ssl, module_type, form_name, file_path, content, xml_summary)
        SELECT NEW.id, o.config_name, o.obj_type, o.obj_name, o.is_ssl,
               NEW.module_type, COALESCE(NEW.form_name, ''), NEW.file_path, NEW.content, COALESCE(NEW.xml_summary, '')
        FROM objects o WHERE o.id = NEW.object_id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS modules_ad AFTER DELETE ON modules BEGIN
        DELETE FROM fts_modules WHERE rowid = OLD.id;
    END""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_objects USING fts5(
        obj_name,
        xml_summary,
        tokenize="unicode61 remove_diacritics 1"
    )""",
    """CREATE TRIGGER IF NOT EXISTS objects_ai AFTER INSERT ON objects BEGIN
        INSERT INTO fts_objects(rowid, obj_name, xml_summary)
        VALUES(NEW.id, NEW.obj_name, COALESCE(NEW.xml_summary, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS objects_ad AFTER DELETE ON objects BEGIN
        DELETE FROM fts_objects WHERE rowid = OLD.id;
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


def _migrate_xml_summary(conn: sqlite3.Connection) -> None:
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='modules'"
    )}
    if not tables:
        return  # fresh DB, _DDL will create the up-to-date schema

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(modules)")}
    if "xml_summary" in columns:
        return  # already migrated

    conn.execute("ALTER TABLE modules ADD COLUMN xml_summary TEXT")
    conn.execute("DROP TABLE IF EXISTS fts_modules")
    # Existing fts_modules content is now stale relative to modules/objects; force a
    # full reindex of every config so fts_modules and the new xml_summary get populated.
    conn.execute("DELETE FROM index_runs")


def ensure_schema(db_path: str) -> None:
    conn = get_connection(db_path)
    for stmt in _DROP_TRIGGERS:
        conn.execute(stmt)
    _migrate_xml_summary(conn)
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
