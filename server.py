import asyncio
import json
import logging
import socket
import threading
from datetime import datetime
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import db
import parser as p
import tools as t

CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_PATH = Path(__file__).parent / "server.log"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

app = Server("onec-config-mcp")

_config: dict = {}
_db_path: str = ""

log = logging.getLogger("onec-config-mcp")

# Guards against duplicate watchdog/reindex when both Claude Code and Claude
# Desktop launch their own server.py process against the same config folder.
_SINGLETON_PORT = 47601
_singleton_socket: socket.socket | None = None


def _acquire_singleton_lock() -> bool:
    global _singleton_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
    except OSError:
        s.close()
        return False
    _singleton_socket = s  # kept open for process lifetime to hold the lock
    return True


def _conn():
    return db.get_connection(_db_path)


# --- Watchdog ---

DEBOUNCE_SECONDS = 10.0


class _ConfigHandler(FileSystemEventHandler):
    def __init__(self, config: dict):
        self._config = config
        self._timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        self._reindex_lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return
        path = event.src_path
        if not (path.endswith(".bsl") or path.endswith(".xml")):
            return
        with self._debounce_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._reindex)
            self._timer.daemon = True
            self._timer.start()

    def _reindex(self):
        name = self._config["name"]
        if not self._reindex_lock.acquire(blocking=False):
            log.info(f"[watcher] {name}: reindex already in progress, skipping")
            return
        try:
            log.info(f"[watcher] Re-indexing {name}...")
            conn = db.get_connection(_db_path, timeout=30)
            p.index_config(conn, self._config)
            conn.close()
            log.info(f"[watcher] {name} re-indexed OK")
        except Exception as e:
            log.error(f"[watcher] Re-index failed for {name}: {e}", exc_info=True)
        finally:
            self._reindex_lock.release()


def _startup_reindex(configs: list[dict]) -> None:
    watched = [c for c in configs if c.get("watch") and Path(c["path"]).exists()]
    if not watched:
        return
    conn = db.get_connection(_db_path)
    for cfg in watched:
        name = cfg["name"]
        row = conn.execute(
            "SELECT indexed_at FROM index_runs WHERE config_name = ?", (name,)
        ).fetchone()
        if row is None:
            log.info(f"[startup] {name}: no index found, indexing...")
        else:
            indexed_at = datetime.fromisoformat(row["indexed_at"])
            config_path = Path(cfg["path"])
            max_mtime = max(
                (f.stat().st_mtime for f in config_path.rglob("*")
                 if f.suffix in (".bsl", ".xml")),
                default=0,
            )
            if max_mtime <= indexed_at.timestamp():
                log.info(f"[startup] {name}: up to date, skipping")
                continue
            log.info(f"[startup] {name}: files changed, re-indexing...")
        try:
            p.index_config(conn, cfg)
            log.info(f"[startup] {name}: done")
        except Exception as e:
            log.error(f"[startup] {name}: failed: {e}", exc_info=True)
    conn.close()


def _start_watchers(configs: list[dict]) -> Observer | None:
    watched = [c for c in configs if c.get("watch") and Path(c["path"]).exists()]
    if not watched:
        return None

    observer = Observer()
    for cfg in watched:
        observer.schedule(_ConfigHandler(cfg), cfg["path"], recursive=True)
        log.warning(f"[watcher] Watching {cfg['name']} at {cfg['path']}")

    observer.daemon = True
    observer.start()
    return observer


# --- MCP Tools ---

TOOL_DEFS = [
    types.Tool(
        name="search_code",
        description="Полнотекстовый поиск по BSL-коду 1С конфигураций. Поддерживает FTS5 синтаксис (AND, OR, NOT, \"фраза\"). Возвращает сниппеты.",
        inputSchema={
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Текст для поиска"},
                "config_name": {"type": "string", "description": "Фильтр по конфигурации (Доки, БП, БСП)"},
                "obj_type":    {"type": "string", "description": "Фильтр по типу объекта (CommonModules, Catalogs, ...)"},
                "is_bsl":      {"type": "boolean", "description": "true = только BSL, false = только конфигурационный код"},
                "limit":       {"type": "integer", "description": "Макс. результатов (по умолчанию 20)", "default": 20},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="find_object",
        description="Найти объект метаданных 1С по имени. Возвращает синоним, флаги и xml_summary.",
        inputSchema={
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Имя объекта (полное или частичное)"},
                "obj_type":    {"type": "string", "description": "Фильтр по типу (CommonModules, Catalogs, ...)"},
                "config_name": {"type": "string", "description": "Фильтр по конфигурации"},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="get_module",
        description="Получить полный BSL-код модуля объекта. При >200 КБ — обрезается с предупреждением.",
        inputSchema={
            "type": "object",
            "properties": {
                "obj_name":    {"type": "string", "description": "Имя объекта, например Доки_Авторизация"},
                "config_name": {"type": "string", "description": "Конфигурация. Опционально, если имя уникально."},
                "module_type": {"type": "string", "description": "Module / ObjectModule / ManagerModule / FormModule"},
                "form_name":   {"type": "string", "description": "Имя формы при module_type=FormModule"},
            },
            "required": ["obj_name"],
        },
    ),
    types.Tool(
        name="list_objects",
        description="Список объектов метаданных по типу и/или конфигурации.",
        inputSchema={
            "type": "object",
            "properties": {
                "obj_type":    {"type": "string", "description": "Тип объекта (CommonModules, Catalogs, ...)"},
                "config_name": {"type": "string", "description": "Конфигурация"},
                "is_bsl":      {"type": "boolean", "description": "Фильтр по BSL"},
            },
        },
    ),
    types.Tool(
        name="find_procedure",
        description="Найти определение процедуры или функции по имени (Процедура/Функция ИмяМетода(). Возвращает номер строки.",
        inputSchema={
            "type": "object",
            "properties": {
                "proc_name":   {"type": "string", "description": "Имя процедуры/функции"},
                "config_name": {"type": "string", "description": "Конфигурация. Опционально."},
            },
            "required": ["proc_name"],
        },
    ),
    types.Tool(
        name="list_configs",
        description="Показать что проиндексировано: конфигурации, дата индексирования, кол-во объектов.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_object_metadata",
        description="Метаданные объекта: xml_summary, список модулей с кол-вом строк.",
        inputSchema={
            "type": "object",
            "properties": {
                "obj_name":    {"type": "string", "description": "Имя объекта"},
                "config_name": {"type": "string", "description": "Конфигурация. Опционально."},
            },
            "required": ["obj_name"],
        },
    ),
]

TOOL_HANDLERS = {
    "search_code":         t.search_code,
    "find_object":         t.find_object,
    "get_module":          t.get_module,
    "list_objects":        t.list_objects,
    "find_procedure":      t.find_procedure,
    "list_configs":        t.list_configs,
    "get_object_metadata": t.get_object_metadata,
}


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOL_DEFS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    conn = _conn()
    try:
        result = handler(conn, arguments)
    finally:
        conn.close()
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def main() -> None:
    global _config, _db_path
    _config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    _db_path = _config["db_path"]
    log.info(f"Server starting, db_path={_db_path}")
    db.ensure_schema(_db_path)

    if _acquire_singleton_lock():
        _startup_reindex(_config["configs"])
        _start_watchers(_config["configs"])
        log.info("Server ready (primary instance, watching for changes)")
    else:
        log.info("Server ready (secondary instance, another process already watches for changes)")

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
