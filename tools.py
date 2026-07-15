import re
import sqlite3

_FTS5_SYNTAX_RE = re.compile(r'"|(?:^|\s)(?:AND|OR|NOT)(?:\s|$)|\*(?:\s|$)')


def _fts5_escape(text: str) -> str:
    # Callers that deliberately use FTS5 syntax (quoted phrases, AND/OR/NOT,
    # prefix "*") are passed through as-is and trusted to be well-formed.
    # Everything else (typically a raw BSL snippet/identifier, e.g.
    # "Объект.Метод(Параметр)") is wrapped as a single phrase so FTS5's query
    # grammar doesn't choke on ".", "(", ",", etc.
    if _FTS5_SYNTAX_RE.search(text):
        return text
    return '"' + text.replace('"', '""') + '"'


def _fts5_force_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _fts5_query_with_fallback(
    conn: sqlite3.Connection, sql: str, raw_text: str, other_params: list, query_pos: int = 0
) -> list:
    # _fts5_escape's heuristic can misjudge a raw snippet as deliberate FTS5
    # syntax (e.g. an unbalanced quote, or a quote mixed with unescaped
    # "."/"("/")") and produce a query FTS5 rejects. Retry once with the
    # whole raw_text forced into a single escaped phrase.
    params = list(other_params)
    params.insert(query_pos, _fts5_escape(raw_text))
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        params[query_pos] = _fts5_force_phrase(raw_text)
        return conn.execute(sql, params).fetchall()


def search_code(conn: sqlite3.Connection, args: dict) -> list[dict]:
    query = args.get("query", "")
    config_name = args.get("config_name")
    obj_type = args.get("obj_type")
    limit = int(args.get("limit", 20))

    other_params: list = []
    post_filters = []

    if config_name is not None:
        post_filters.append("o.config_name = ?")
        other_params.append(config_name)
    if obj_type is not None:
        post_filters.append("o.obj_type = ?")
        other_params.append(obj_type)

    other_params.append(limit)
    extra = ("AND " + " AND ".join(post_filters)) if post_filters else ""

    sql = f"""SELECT
                o.config_name, o.obj_type, o.obj_name,
                m.module_type, m.form_name, m.file_path,
                snippet(fts_modules, 3, '>>>', '<<<', '...', 32) AS snippet
            FROM fts_modules
            JOIN modules m ON fts_modules.rowid = m.id
            JOIN objects o ON m.object_id = o.id
            WHERE fts_modules MATCH ?
            {extra}
            ORDER BY rank
            LIMIT ?"""

    rows = _fts5_query_with_fallback(conn, sql, query, other_params)

    return [dict(r) for r in rows]


def find_object(conn: sqlite3.Connection, args: dict) -> list[dict]:
    name = args.get("name", "")
    obj_type = args.get("obj_type")
    config_name = args.get("config_name")

    extra_filters = []
    extra_params: list = []
    if obj_type:
        extra_filters.append("o.obj_type = ?")
        extra_params.append(obj_type)
    if config_name:
        extra_filters.append("o.config_name = ?")
        extra_params.append(config_name)

    where_extra = ("AND " + " AND ".join(extra_filters)) if extra_filters else ""

    sql = f"""SELECT o.id, o.config_name, o.obj_type, o.obj_name, o.xml_summary
            FROM fts_objects
            JOIN objects o ON fts_objects.rowid = o.id
            WHERE fts_objects MATCH ?
            {where_extra}
            ORDER BY rank LIMIT 20"""

    rows = _fts5_query_with_fallback(conn, sql, name, extra_params)

    return [dict(r) for r in rows]


def get_module(conn: sqlite3.Connection, args: dict) -> dict | list[dict]:
    obj_name = args.get("obj_name", "")
    config_name = args.get("config_name")
    module_type = args.get("module_type")
    form_name = args.get("form_name")

    filters = ["o.obj_name = ?"]
    params: list = [obj_name]
    if config_name:
        filters.append("o.config_name = ?")
        params.append(config_name)
    if module_type:
        filters.append("m.module_type = ?")
        params.append(module_type)
    if form_name:
        filters.append("m.form_name = ?")
        params.append(form_name)

    where = " AND ".join(filters)
    rows = conn.execute(
        f"""SELECT m.content, m.file_path, m.module_type, m.form_name, m.line_count, m.xml_summary,
                   o.config_name, o.obj_type, o.obj_name
            FROM modules m
            JOIN objects o ON m.object_id = o.id
            WHERE {where}
            LIMIT 5""",
        params,
    ).fetchall()

    if not rows:
        return {"error": f"Module not found: {obj_name}"}

    results = []
    for row in rows:
        r = dict(row)
        content = r["content"]
        if len(content) > 200_000:
            r["content"] = content[:200_000]
            r["truncated"] = True
            r["note"] = f"Content truncated at 200000 chars (original {len(content)} chars / {r['line_count']} lines)"
        results.append(r)

    return results[0] if len(results) == 1 else results


def list_objects(conn: sqlite3.Connection, args: dict) -> list[dict]:
    obj_type = args.get("obj_type")
    config_name = args.get("config_name")

    filters = []
    params: list = []
    if obj_type:
        filters.append("obj_type = ?")
        params.append(obj_type)
    if config_name:
        filters.append("config_name = ?")
        params.append(config_name)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = conn.execute(
        f"""SELECT o.config_name, o.obj_type, o.obj_name,
                   COUNT(m.id) AS module_count
            FROM objects o
            LEFT JOIN modules m ON m.object_id = o.id
            {where}
            GROUP BY o.id
            ORDER BY o.config_name, o.obj_type, o.obj_name""",
        params,
    ).fetchall()

    return [dict(r) for r in rows]


def find_procedure(conn: sqlite3.Connection, args: dict) -> list[dict]:
    proc_name = args.get("proc_name", "")
    config_name = args.get("config_name")

    # Query fts_modules directly for every module containing the exact token,
    # unranked and unlimited (search_code's BM25-ranked top-N would bury the
    # definition inside a large common module below smaller modules that
    # merely call it - see e.g. ОбщегоНазначения in a big BSP config).
    extra_params: list = []
    extra = ""
    if config_name:
        extra = "AND o.config_name = ?"
        extra_params.append(config_name)

    sql = f"""SELECT o.config_name, o.obj_type, o.obj_name,
                   m.module_type, m.form_name, m.file_path, m.content
            FROM fts_modules
            JOIN modules m ON fts_modules.rowid = m.id
            JOIN objects o ON m.object_id = o.id
            WHERE fts_modules MATCH ?
            {extra}"""

    rows = _fts5_query_with_fallback(conn, sql, proc_name, extra_params)

    pattern = re.compile(
        r"(Процедура|Функция|Procedure|Function)\s+" + re.escape(proc_name) + r"\s*\(",
        re.IGNORECASE,
    )

    results = []
    seen = set()
    for r in rows:
        file_path = r["file_path"]
        if file_path in seen:
            continue

        content = r["content"]
        for line_no, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                results.append({
                    "config_name": r["config_name"],
                    "obj_name": r["obj_name"],
                    "obj_type": r["obj_type"],
                    "module_type": r["module_type"],
                    "form_name": r["form_name"],
                    "file_path": file_path,
                    "definition_line": line_no,
                    "context": line.strip(),
                })
                seen.add(file_path)
                break

    return results


def _definition_pattern(proc_name: str) -> re.Pattern:
    return re.compile(
        r"(Процедура|Функция|Procedure|Function)\s+" + re.escape(proc_name) + r"\s*\(",
        re.IGNORECASE,
    )


_END_DEF_RE = re.compile(
    r"^\s*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE
)


def _extract_procedure_bodies(
    conn: sqlite3.Connection, proc_name: str, config_name: str | None
) -> list[dict]:
    """Find every module containing a `Процедура/Функция proc_name(` definition
    and cut out its body (up to the matching КонецПроцедуры/КонецФункции).
    BSL procedures/functions don't nest, so the first end-marker after the
    definition line is always the right boundary."""
    extra_params: list = []
    extra = ""
    if config_name:
        extra = "AND o.config_name = ?"
        extra_params.append(config_name)

    sql = f"""SELECT o.config_name, o.obj_type, o.obj_name,
                   m.module_type, m.form_name, m.file_path, m.content
            FROM fts_modules
            JOIN modules m ON fts_modules.rowid = m.id
            JOIN objects o ON m.object_id = o.id
            WHERE fts_modules MATCH ?
            {extra}"""

    rows = _fts5_query_with_fallback(conn, sql, proc_name, extra_params)

    pattern = _definition_pattern(proc_name)

    results = []
    seen = set()
    for r in rows:
        file_path = r["file_path"]
        if file_path in seen:
            continue

        lines = r["content"].splitlines()
        start_idx = next((i for i, line in enumerate(lines) if pattern.search(line)), None)
        if start_idx is None:
            continue

        end_idx = len(lines) - 1
        for i in range(start_idx, len(lines)):
            if _END_DEF_RE.match(lines[i]):
                end_idx = i
                break

        results.append({
            "config_name": r["config_name"],
            "obj_type": r["obj_type"],
            "obj_name": r["obj_name"],
            "module_type": r["module_type"],
            "form_name": r["form_name"],
            "file_path": file_path,
            "start_line": start_idx + 1,
            "end_line": end_idx + 1,
            "body": "\n".join(lines[start_idx:end_idx + 1]),
        })
        seen.add(file_path)

    return results


def get_procedure_body(conn: sqlite3.Connection, args: dict) -> list[dict]:
    proc_name = args.get("proc_name", "")
    config_name = args.get("config_name")
    return _extract_procedure_bodies(conn, proc_name, config_name)


_PROC_START_RE = re.compile(
    r"^\s*(Процедура|Функция|Procedure|Function)\s+(\w+)\s*\(", re.IGNORECASE
)
_EXPORT_RE = re.compile(r"\bЭкспорт\b|\bExport\b", re.IGNORECASE)


def _outline_from_content(content: str) -> list[dict]:
    lines = content.splitlines()
    procedures = []
    for i, line in enumerate(lines):
        m = _PROC_START_RE.match(line)
        if not m:
            continue
        kind_raw, name = m.groups()
        kind = "Функция" if kind_raw.lower() in ("функция", "function") else "Процедура"

        # Экспорт sits after the closing ")" of the signature, which may be on
        # a later line for multi-line parameter lists - join lines up to the
        # first one containing ")" (capped so a stray unbalanced paren can't
        # scan the rest of the module).
        header_lines = [line]
        j = i
        while ")" not in header_lines[-1] and j + 1 < len(lines) and len(header_lines) < 20:
            j += 1
            header_lines.append(lines[j])

        procedures.append({
            "name": name,
            "kind": kind,
            "is_export": bool(_EXPORT_RE.search(" ".join(header_lines))),
            "line": i + 1,
        })
    return procedures


def get_module_outline(conn: sqlite3.Connection, args: dict) -> dict | list[dict]:
    obj_name = args.get("obj_name", "")
    config_name = args.get("config_name")
    module_type = args.get("module_type")
    form_name = args.get("form_name")

    filters = ["o.obj_name = ?"]
    params: list = [obj_name]
    if config_name:
        filters.append("o.config_name = ?")
        params.append(config_name)
    if module_type:
        filters.append("m.module_type = ?")
        params.append(module_type)
    if form_name:
        filters.append("m.form_name = ?")
        params.append(form_name)

    where = " AND ".join(filters)
    rows = conn.execute(
        f"""SELECT m.content, m.module_type, m.form_name,
                   o.config_name, o.obj_type, o.obj_name
            FROM modules m
            JOIN objects o ON m.object_id = o.id
            WHERE {where}
            LIMIT 5""",
        params,
    ).fetchall()

    if not rows:
        return {"error": f"Module not found: {obj_name}"}

    results = []
    for row in rows:
        r = dict(row)
        r["procedures"] = _outline_from_content(r.pop("content"))
        results.append(r)

    return results[0] if len(results) == 1 else results


def get_callers(conn: sqlite3.Connection, args: dict) -> list[dict]:
    proc_name = args.get("proc_name", "")
    config_name = args.get("config_name")

    extra_params: list = []
    extra = ""
    if config_name:
        extra = "AND o.config_name = ?"
        extra_params.append(config_name)

    sql = f"""SELECT o.config_name, o.obj_type, o.obj_name,
                   m.module_type, m.form_name, m.file_path, m.content
            FROM fts_modules
            JOIN modules m ON fts_modules.rowid = m.id
            JOIN objects o ON m.object_id = o.id
            WHERE fts_modules MATCH ?
            {extra}"""

    rows = _fts5_query_with_fallback(conn, sql, proc_name, extra_params)

    def_pattern = _definition_pattern(proc_name)
    call_pattern = re.compile(r"\b" + re.escape(proc_name) + r"\s*\(", re.IGNORECASE)

    results = []
    for r in rows:
        for line_no, line in enumerate(r["content"].splitlines(), start=1):
            if def_pattern.search(line):
                continue
            if call_pattern.search(line):
                results.append({
                    "config_name": r["config_name"],
                    "obj_type": r["obj_type"],
                    "obj_name": r["obj_name"],
                    "module_type": r["module_type"],
                    "form_name": r["form_name"],
                    "file_path": r["file_path"],
                    "line": line_no,
                    "context": line.strip(),
                })

    return results


# Words that follow the "Identifier(" shape of a call but aren't one -
# BSL control-flow/declaration keywords that can precede a parenthesized
# expression (e.g. "Если(", "Пока(", "Новый(").
_BSL_KEYWORDS = {
    "Если", "Пока", "Для", "Тогда", "Иначе", "ИначеЕсли", "КонецЕсли",
    "КонецЦикла", "Процедура", "Функция", "КонецПроцедуры", "КонецФункции",
    "Возврат", "Новый", "Прервать", "Продолжить", "Попытка", "Исключение",
    "КонецПопытки", "Экспорт", "Перем", "Каждого", "Из", "По", "Цикл",
    "If", "While", "For", "Then", "Else", "ElsIf", "EndIf", "EndDo",
    "Procedure", "Function", "EndProcedure", "EndFunction", "Return",
    "New", "Break", "Continue", "Try", "Except", "EndTry", "Export",
    "Var", "Each", "In", "To", "Do",
}

_CALL_RE = re.compile(r"\b([A-ZА-Я][\wА-Яа-я]*)\s*\(")


def get_callees(conn: sqlite3.Connection, args: dict) -> list[dict]:
    proc_name = args.get("proc_name", "")
    config_name = args.get("config_name")

    bodies = _extract_procedure_bodies(conn, proc_name, config_name)

    results = []
    for b in bodies:
        counts: dict[str, int] = {}
        for m in _CALL_RE.finditer(b["body"]):
            name = m.group(1)
            if name in _BSL_KEYWORDS or name.lower() == proc_name.lower():
                continue
            counts[name] = counts.get(name, 0) + 1

        callees = [
            {"name": n, "count": c}
            for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        results.append({
            "config_name": b["config_name"],
            "obj_type": b["obj_type"],
            "obj_name": b["obj_name"],
            "module_type": b["module_type"],
            "form_name": b["form_name"],
            "file_path": b["file_path"],
            "callees": callees,
        })

    return results


def list_configs(conn: sqlite3.Connection, _args: dict) -> list[dict]:
    rows = conn.execute("SELECT * FROM index_runs ORDER BY config_name").fetchall()
    return [dict(r) for r in rows]


def get_object_metadata(conn: sqlite3.Connection, args: dict) -> dict | list[dict]:
    obj_name = args.get("obj_name", "")
    config_name = args.get("config_name")

    filters = ["o.obj_name = ?"]
    params: list = [obj_name]
    if config_name:
        filters.append("o.config_name = ?")
        params.append(config_name)

    where = " AND ".join(filters)
    obj_rows = conn.execute(
        f"SELECT * FROM objects o WHERE {where} LIMIT 5", params
    ).fetchall()

    if not obj_rows:
        return {"error": f"Object not found: {obj_name}"}

    results = []
    for obj in obj_rows:
        modules = conn.execute(
            "SELECT module_type, form_name, line_count, xml_summary FROM modules WHERE object_id = ? ORDER BY module_type",
            (obj["id"],),
        ).fetchall()
        results.append({
            **dict(obj),
            "modules": [dict(m) for m in modules],
        })

    return results[0] if len(results) == 1 else results
