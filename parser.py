import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

OBJECT_TYPES = {
    "CommonModules", "Catalogs", "Documents", "DataProcessors", "Reports",
    "InformationRegisters", "AccumulationRegisters", "AccountingRegisters",
    "CalculationRegisters", "BusinessProcesses", "Tasks", "ExchangePlans",
    "CommonForms", "Constants", "Enums", "ChartOfCharacteristicTypes",
    "ChartOfAccounts", "ChartOfCalculationTypes", "DocumentJournals",
    "ScheduledJobs", "Sequences",
}

# BSL module filenames relative to object's Ext/ directory
SIMPLE_MODULES = [
    "Module.bsl",
    "ObjectModule.bsl",
    "ManagerModule.bsl",
    "RecordSetModule.bsl",
    "CommandModule.bsl",
]


def extract_xml_summary(xml_path: Path) -> str:
    try:
        tree = etree.parse(str(xml_path))
        root = tree.getroot()
        texts = []
        for el in root.iter():
            tag = etree.QName(el.tag).localname
            if tag in ("Name", "content", "Comment") and el.text and el.text.strip():
                texts.append(el.text.strip())
            elif tag in ("Server", "Global", "Privileged", "ClientManagedApplication") and el.text == "true":
                texts.append(f"{tag}=true")
        return " | ".join(dict.fromkeys(texts))  # deduplicate preserving order
    except Exception:
        return ""


def index_config(conn: sqlite3.Connection, config: dict) -> tuple[int, int]:
    config_name = config["name"]
    config_path = Path(config["path"])
    is_bsl = int(config.get("is_bsl", False))

    # Delete existing data for this config (CASCADE removes modules; FTS triggers fire)
    conn.execute("DELETE FROM objects WHERE config_name = ?", (config_name,))

    obj_count = 0
    file_count = 0

    for type_dir in config_path.iterdir():
        if not type_dir.is_dir() or type_dir.name not in OBJECT_TYPES:
            continue
        obj_type = type_dir.name

        for item in type_dir.iterdir():
            if not item.is_dir():
                continue
            obj_name = item.name
            xml_path = type_dir / f"{obj_name}.xml"
            xml_summary = extract_xml_summary(xml_path) if xml_path.exists() else ""

            conn.execute(
                """INSERT OR REPLACE INTO objects
                   (config_name, obj_type, obj_name, is_bsl, xml_path, xml_summary)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (config_name, obj_type, obj_name, is_bsl,
                 str(xml_path) if xml_path.exists() else None,
                 xml_summary),
            )
            obj_id = conn.execute(
                "SELECT id FROM objects WHERE config_name=? AND obj_type=? AND obj_name=?",
                (config_name, obj_type, obj_name),
            ).fetchone()["id"]
            obj_count += 1

            # Simple modules in Ext/
            ext_dir = item / "Ext"
            if ext_dir.exists():
                for module_name in SIMPLE_MODULES:
                    bsl_path = ext_dir / module_name
                    if bsl_path.exists():
                        _insert_module(conn, obj_id, module_name.replace(".bsl", ""), None, bsl_path)
                        file_count += 1

            # Session module at root Ext/SessionModule.bsl (for ExchangePlans etc.)
            session_bsl = ext_dir / "SessionModule.bsl" if ext_dir.exists() else None
            if session_bsl and session_bsl.exists():
                _insert_module(conn, obj_id, "SessionModule", None, session_bsl)
                file_count += 1

            # Form modules: Forms/<FormName>/Ext/Form/Module.bsl
            forms_dir = item / "Forms"
            if forms_dir.exists():
                for form_dir in forms_dir.iterdir():
                    if not form_dir.is_dir():
                        continue
                    form_name = form_dir.name
                    form_module = form_dir / "Ext" / "Form" / "Module.bsl"
                    if form_module.exists():
                        _insert_module(conn, obj_id, "FormModule", form_name, form_module)
                        file_count += 1

    # Root-level modules: Ext/SessionModule.bsl, Ext/ManagedApplicationModule.bsl, etc.
    root_ext = config_path / "Ext"
    if root_ext.exists():
        for bsl_path in root_ext.glob("*.bsl"):
            obj_name = "_Configuration"
            obj_type = "Configuration"
            conn.execute(
                """INSERT OR IGNORE INTO objects
                   (config_name, obj_type, obj_name, is_bsl, xml_path, xml_summary)
                   VALUES (?, ?, ?, ?, NULL, ?)""",
                (config_name, obj_type, obj_name, is_bsl, "Root configuration modules"),
            )
            obj_id_row = conn.execute(
                "SELECT id FROM objects WHERE config_name=? AND obj_type=? AND obj_name=?",
                (config_name, obj_type, obj_name),
            ).fetchone()
            if obj_id_row:
                module_type = bsl_path.stem  # "SessionModule", "ManagedApplicationModule"
                _insert_module(conn, obj_id_row["id"], module_type, None, bsl_path)
                file_count += 1
        obj_count += 1  # count _Configuration as 1 pseudo-object

    conn.execute(
        """INSERT OR REPLACE INTO index_runs (config_name, indexed_at, file_count, obj_count)
           VALUES (?, ?, ?, ?)""",
        (config_name, datetime.now(timezone.utc).isoformat(), file_count, obj_count),
    )
    conn.commit()
    return obj_count, file_count


def _insert_module(
    conn: sqlite3.Connection,
    obj_id: int,
    module_type: str,
    form_name: str | None,
    bsl_path: Path,
) -> None:
    content = bsl_path.read_text(encoding="utf-8-sig")
    line_count = content.count("\n") + 1
    conn.execute(
        """INSERT OR REPLACE INTO modules
           (object_id, module_type, form_name, file_path, content, line_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (obj_id, module_type, form_name, str(bsl_path), content, line_count),
    )
