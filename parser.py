import json
import sqlite3
from collections.abc import Callable
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

# Minimal set of object types worth indexing for configurations that are only
# used as a data model reference, not as a code-search target (see
# object_types config key below).
BASE_OBJECT_TYPES = {"Catalogs", "Documents", "InformationRegisters", "Enums"}

REGISTER_TYPES = {
    "InformationRegisters", "AccumulationRegisters",
    "AccountingRegisters", "CalculationRegisters",
}

# Per "Индексы таблиц базы данных" (its.1c.ru/db/metod8dev#content:1590): Код/
# Наименование are indexed automatically only while CodeLength/DescriptionLength
# != 0 - these two types allow either to be 0 (no code/description at all).
CONDITIONAL_CODE_NAME_TYPES = {"Catalogs", "ChartOfCalculationTypes"}

# These types never allow CodeLength/DescriptionLength == 0 (platform enforces
# it), so Код+Наименование are unconditionally auto-indexed.
ALWAYS_CODE_NAME_TYPES = {"ChartOfCharacteristicTypes", "ChartOfAccounts", "ExchangePlans"}

# Дата is always auto-indexed; Номер only while NumberLength != 0. Tasks also
# get Наименование unconditionally (unlike Documents/BusinessProcesses).
DOCUMENT_LIKE_TYPES = {"Documents", "BusinessProcesses", "Tasks"}

# <Indexing> values that mean the field participates in an index (as opposed
# to "DontIndex").
_INDEXED_VALUES = {"Index", "IndexWithAdditionalOrder"}

# BSL module filenames relative to object's Ext/ directory
SIMPLE_MODULES = [
    "Module.bsl",
    "ObjectModule.bsl",
    "ManagerModule.bsl",
    "RecordSetModule.bsl",
    "CommandModule.bsl",
]


def _resolve_object_types(spec) -> set[str]:
    """config["object_types"]: "all" (default) | "base" | explicit list of type names."""
    if spec is None or spec == "all":
        return OBJECT_TYPES
    if spec == "base":
        return BASE_OBJECT_TYPES
    if isinstance(spec, str):
        raise ValueError(
            f"object_types: неверный формат {spec!r} — ожидается 'all', 'base' "
            f"или список типов, например [\"Catalogs\", \"CommonModules\"]"
        )
    unknown = set(spec) - OBJECT_TYPES
    if unknown:
        raise ValueError(f"object_types: неизвестные типы {sorted(unknown)}")
    return set(spec)


def _direct_child(el, name: str):
    for child in el:
        if etree.QName(child.tag).localname == name:
            return child
    return None


def _own_name(el) -> str | None:
    """<X><Properties><Name>...</Name>...` - the element's own declared name,
    as opposed to names of its descendants picked up by a flat root.iter() walk."""
    props = _direct_child(el, "Properties")
    if props is None:
        return None
    name_el = _direct_child(props, "Name")
    if name_el is not None and name_el.text:
        return name_el.text.strip()
    return None


# v8:Type / v8:TypeSet value -> readable 1C type name. Anything not covered
# here falls back to the raw value as-is rather than guessing.
_SIMPLE_TYPE_NAMES = {
    "xs:string": "Строка",
    "xs:boolean": "Булево",
    "xs:decimal": "Число",
    "xs:dateTime": "Дата",
}

_REF_KIND_NAMES = {
    "CatalogRef": "СправочникСсылка",
    "DocumentRef": "ДокументСсылка",
    "EnumRef": "ПеречислениеСсылка",
    "ChartOfCharacteristicTypesRef": "ПланВидовХарактеристикСсылка",
    "ChartOfAccountsRef": "ПланСчетовСсылка",
    "ChartOfCalculationTypesRef": "ПланВидовРасчетаСсылка",
    "BusinessProcessRef": "БизнесПроцессСсылка",
    "TaskRef": "ЗадачаСсылка",
    "ExchangePlanRef": "ПланОбменаСсылка",
    "DefinedType": "ОпределяемыйТип",
}


def _readable_type_name(raw: str, type_el) -> str:
    if raw in _SIMPLE_TYPE_NAMES:
        base = _SIMPLE_TYPE_NAMES[raw]
        if raw == "xs:decimal":
            qual = _direct_child(type_el, "NumberQualifiers")
            if qual is not None:
                digits = _direct_child(qual, "Digits")
                frac = _direct_child(qual, "FractionDigits")
                if digits is not None and digits.text:
                    d = digits.text.strip()
                    f = frac.text.strip() if frac is not None and frac.text else "0"
                    return f"{base}({d},{f})"
        return base
    # e.g. "cfg:CatalogRef.Организации" -> kind="CatalogRef", name="Организации"
    _, _, rest = raw.partition(":")
    kind, sep, name = rest.partition(".")
    if sep and kind in _REF_KIND_NAMES:
        return f"{_REF_KIND_NAMES[kind]}.{name}"
    return raw


def _format_type(type_el) -> str | None:
    """<Type> element containing one or more <v8:Type>/<v8:TypeSet> children
    (composite types list several) -> readable, comma-joined type name(s)."""
    parts = []
    for child in type_el:
        local = etree.QName(child.tag).localname
        if local in ("Type", "TypeSet") and child.text:
            parts.append(_readable_type_name(child.text.strip(), type_el))
    if not parts:
        return None
    return ", ".join(dict.fromkeys(parts))


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

        # Enrich with typed attribute entries (name + type, and tabular
        # section membership if any) on top of the flat name/comment walk
        # above, e.g. "ТЧ Запасы.Цена (Число(15,2))". Isolated in its own
        # try/except so a malformed <Type> on one attribute can't blank out
        # the already-collected flat names/comments for the whole object.
        try:
            for attr_el in root.iter():
                if etree.QName(attr_el.tag).localname != "Attribute":
                    continue
                attr_name = _own_name(attr_el)
                if not attr_name:
                    continue
                props = _direct_child(attr_el, "Properties")
                type_el = _direct_child(props, "Type") if props is not None else None
                type_name = _format_type(type_el) if type_el is not None else None
                if not type_name:
                    continue
                section_name = None
                for ancestor in attr_el.iterancestors():
                    if etree.QName(ancestor.tag).localname == "TabularSection":
                        section_name = _own_name(ancestor)
                        break
                if section_name:
                    texts.append(f"ТЧ {section_name}.{attr_name} ({type_name})")
                else:
                    texts.append(f"{attr_name} ({type_name})")
        except Exception:
            pass

        return " | ".join(dict.fromkeys(texts))  # deduplicate preserving order
    except Exception:
        return ""


def _int_prop(props, tag_name: str) -> int:
    child = _direct_child(props, tag_name)
    if child is None or not child.text:
        return 0
    try:
        return int(child.text.strip())
    except ValueError:
        return 0


def extract_index_info(xml_path: Path, obj_type: str) -> dict:
    """Field-level indexing info for get_object_metadata.

    Registers: the register's dimensions always form a base composite index in
    declared order (so the *first* dimension is always efficiently searchable
    alone), and any dimension/resource/attribute with <Indexing> = Index or
    IndexWithAdditionalOrder gets its own additional index leading with that
    field. A non-first dimension left at DontIndex has no way to be searched
    on its own - only as part of the composite key, in declaration order.

    Reference/document-like types: which standard attributes the platform
    indexes automatically, and under what condition (Код/Наименование/Номер
    only exist as indexed fields once CodeLength/DescriptionLength/NumberLength
    != 0 for the object). Source: "Индексы таблиц базы данных",
    its.1c.ru/db/metod8dev#content:1590.
    """
    result: dict = {}

    if obj_type in REGISTER_TYPES:
        indexed_fields = []
        try:
            tree = etree.parse(str(xml_path))
            root = tree.getroot()
            dimension_seen = 0
            for el in root.iter():
                tag = etree.QName(el.tag).localname
                if tag not in ("Dimension", "Resource", "Attribute"):
                    continue
                name = _own_name(el)
                if not name:
                    continue
                props = _direct_child(el, "Properties")
                indexing_el = _direct_child(props, "Indexing") if props is not None else None
                indexing_value = indexing_el.text if indexing_el is not None else None

                if tag == "Dimension":
                    is_leading = dimension_seen == 0
                    dimension_seen += 1
                    if is_leading:
                        indexed_fields.append({"field": name, "kind": "dimension", "reason": "leading_dimension"})
                    elif indexing_value in _INDEXED_VALUES:
                        indexed_fields.append({"field": name, "kind": "dimension", "reason": indexing_value})
                elif indexing_value in _INDEXED_VALUES:
                    indexed_fields.append({
                        "field": name,
                        "kind": "resource" if tag == "Resource" else "attribute",
                        "reason": indexing_value,
                    })
        except Exception:
            pass
        if indexed_fields:
            result["indexed_fields"] = indexed_fields
        return result

    try:
        tree = etree.parse(str(xml_path))
        root = tree.getroot()
        object_el = next(iter(root), None)
        props = _direct_child(object_el, "Properties") if object_el is not None else None
    except Exception:
        props = None

    auto_fields: list[str] = []
    if obj_type in CONDITIONAL_CODE_NAME_TYPES:
        auto_fields.append("Ссылка")
        if props is not None:
            if _int_prop(props, "CodeLength") != 0:
                auto_fields.append("Код")
            if _int_prop(props, "DescriptionLength") != 0:
                auto_fields.append("Наименование")
    elif obj_type in ALWAYS_CODE_NAME_TYPES:
        auto_fields = ["Ссылка", "Код", "Наименование"]
    elif obj_type in DOCUMENT_LIKE_TYPES:
        auto_fields = ["Ссылка", "Дата"]
        if obj_type == "Tasks":
            auto_fields.append("Наименование")
        if props is not None and _int_prop(props, "NumberLength") != 0:
            auto_fields.append("Номер")

    if auto_fields:
        result["auto_indexed_fields"] = auto_fields

    return result


MAX_FORM_SUMMARY_LEN = 4000
MAX_FORM_ELEMENTS = 60


def _extract_logform_summary(logform_xml_path: Path) -> str:
    """Parse a form's Ext/Form.xml (namespace xcf/logform): attributes, commands,
    child elements (with DataPath bindings) and top-level event handlers."""
    try:
        tree = etree.parse(str(logform_xml_path))
        root = tree.getroot()

        attrs = []
        for attr_el in root.iter():
            if etree.QName(attr_el.tag).localname != "Attribute":
                continue
            name = attr_el.get("name")
            if name:
                attrs.append(name)

        commands = []
        for cmd_el in root.iter():
            if etree.QName(cmd_el.tag).localname != "Command":
                continue
            name = cmd_el.get("name")
            if name:
                commands.append(name)

        events = []
        for events_el in root.iter():
            if etree.QName(events_el.tag).localname != "Events":
                continue
            for event_el in events_el:
                if etree.QName(event_el.tag).localname != "Event":
                    continue
                event_name = event_el.get("name")
                handler = (event_el.text or "").strip()
                if event_name and handler:
                    events.append(f"{event_name}->{handler}")

        elements = []
        for el in root.iter():
            tag = etree.QName(el.tag).localname
            if tag in ("Attribute", "Command", "Event", "Form"):
                continue
            name = el.get("name")
            if not name:
                continue
            data_path = None
            for child in el:
                if etree.QName(child.tag).localname == "DataPath" and child.text:
                    data_path = child.text.strip()
                    break
            elements.append(f"{name}({tag}->{data_path})" if data_path else f"{name}({tag})")

        parts = []
        if attrs:
            parts.append("Attrs: " + ", ".join(dict.fromkeys(attrs)))
        if commands:
            parts.append("Commands: " + ", ".join(dict.fromkeys(commands)))
        if elements:
            deduped = list(dict.fromkeys(elements))
            truncated_note = ""
            if len(deduped) > MAX_FORM_ELEMENTS:
                truncated_note = f" (+{len(deduped) - MAX_FORM_ELEMENTS} more)"
                deduped = deduped[:MAX_FORM_ELEMENTS]
            parts.append("Elements: " + ", ".join(deduped) + truncated_note)
        if events:
            parts.append("Events: " + ", ".join(dict.fromkeys(events)))

        return " | ".join(parts)
    except Exception:
        return ""


def extract_form_summary(form_xml_path: Path | None, logform_xml_path: Path | None) -> str:
    """Build a searchable text summary for a form from its sibling descriptor
    (Forms/<Name>.xml) and its logform layout (<form_dir>/Ext/Form.xml)."""
    parts = []
    if form_xml_path and form_xml_path.exists():
        summary = extract_xml_summary(form_xml_path)
        if summary:
            parts.append(summary)
    if logform_xml_path and logform_xml_path.exists():
        summary = _extract_logform_summary(logform_xml_path)
        if summary:
            parts.append(summary)

    result = " | ".join(parts)
    if len(result) > MAX_FORM_SUMMARY_LEN:
        result = result[:MAX_FORM_SUMMARY_LEN] + "..."
    return result


def index_config(
    conn: sqlite3.Connection,
    config: dict,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    config_name = config["name"]
    config_path = Path(config["path"])
    object_types = _resolve_object_types(config.get("object_types"))
    index_forms = bool(config.get("index_forms", True))

    def report(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # Delete existing data for this config (CASCADE removes modules; FTS triggers fire)
    conn.execute("DELETE FROM objects WHERE config_name = ?", (config_name,))

    obj_count = 0
    file_count = 0

    type_dirs = sorted(
        d for d in config_path.iterdir() if d.is_dir() and d.name in object_types
    )
    for type_dir_index, type_dir in enumerate(type_dirs, start=1):
        obj_type = type_dir.name
        obj_count_before, file_count_before = obj_count, file_count
        report(f"[{type_dir_index}/{len(type_dirs)}] {obj_type} ...")

        for item in type_dir.iterdir():
            if not item.is_dir():
                continue
            obj_name = item.name
            xml_path = type_dir / f"{obj_name}.xml"
            xml_summary = extract_xml_summary(xml_path) if xml_path.exists() else ""
            index_info = extract_index_info(xml_path, obj_type) if xml_path.exists() else {}

            conn.execute(
                """INSERT OR REPLACE INTO objects
                   (config_name, obj_type, obj_name, xml_path, xml_summary, index_info)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (config_name, obj_type, obj_name,
                 str(xml_path) if xml_path.exists() else None,
                 xml_summary,
                 json.dumps(index_info, ensure_ascii=False) if index_info else None),
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

            if not index_forms:
                continue

            # CommonForms: the object dir *is* the form dir (Ext/Form/Module.bsl,
            # Ext/Form.xml), unlike SIMPLE_MODULES's flat Ext/Module.bsl layout.
            if obj_type == "CommonForms":
                inserted = _index_form(
                    conn, obj_id, form_name=obj_name, form_dir=item,
                    form_meta_xml=xml_path if xml_path.exists() else None,
                )
                if inserted:
                    file_count += 1

            # Owned forms: Forms/<FormName>/Ext/Form/Module.bsl (+ Ext/Form.xml).
            # Every form dir gets indexed, even ones with no code, so their
            # metadata (attributes/commands/elements) is still searchable.
            forms_dir = item / "Forms"
            if forms_dir.exists():
                for form_dir in forms_dir.iterdir():
                    if not form_dir.is_dir():
                        continue
                    form_name = form_dir.name
                    form_meta_xml = forms_dir / f"{form_name}.xml"
                    inserted = _index_form(
                        conn, obj_id, form_name=form_name, form_dir=form_dir,
                        form_meta_xml=form_meta_xml if form_meta_xml.exists() else None,
                    )
                    if inserted:
                        file_count += 1

        report(
            f"[{type_dir_index}/{len(type_dirs)}] {obj_type}: "
            f"{obj_count - obj_count_before} objects, {file_count - file_count_before} modules"
        )

    # Root-level modules: Ext/SessionModule.bsl, Ext/ManagedApplicationModule.bsl, etc.
    root_ext = config_path / "Ext"
    if root_ext.exists():
        for bsl_path in root_ext.glob("*.bsl"):
            obj_name = "_Configuration"
            obj_type = "Configuration"
            conn.execute(
                """INSERT OR IGNORE INTO objects
                   (config_name, obj_type, obj_name, xml_path, xml_summary)
                   VALUES (?, ?, ?, NULL, ?)""",
                (config_name, obj_type, obj_name, "Root configuration modules"),
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


def _index_form(
    conn: sqlite3.Connection,
    obj_id: int,
    form_name: str,
    form_dir: Path,
    form_meta_xml: Path | None,
) -> bool:
    """Insert a modules row for a form, whether or not it has a Module.bsl.
    Returns True if a form module (.bsl) file was found (for file_count)."""
    form_module = form_dir / "Ext" / "Form" / "Module.bsl"
    logform_xml = form_dir / "Ext" / "Form.xml"

    xml_summary = extract_form_summary(
        form_meta_xml, logform_xml if logform_xml.exists() else None
    )

    if form_module.exists():
        content = form_module.read_text(encoding="utf-8-sig")
        file_path = form_module
        has_module = True
    else:
        content = ""
        # No Module.bsl to key off of - fall back to another path unique to
        # this form so modules.file_path (UNIQUE) doesn't collide across forms.
        file_path = logform_xml if logform_xml.exists() else form_dir
        has_module = False

    _insert_module(
        conn, obj_id, "FormModule", form_name, file_path,
        content=content, xml_summary=xml_summary or None,
    )
    return has_module


def _insert_module(
    conn: sqlite3.Connection,
    obj_id: int,
    module_type: str,
    form_name: str | None,
    bsl_path: Path,
    content: str | None = None,
    xml_summary: str | None = None,
) -> None:
    if content is None:
        content = bsl_path.read_text(encoding="utf-8-sig")
    line_count = content.count("\n") + 1 if content else 0
    conn.execute(
        """INSERT OR REPLACE INTO modules
           (object_id, module_type, form_name, file_path, content, line_count, xml_summary)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (obj_id, module_type, form_name, str(bsl_path), content, line_count, xml_summary),
    )
