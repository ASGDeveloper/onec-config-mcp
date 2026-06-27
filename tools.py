import re
import sqlite3


def search_code(conn: sqlite3.Connection, args: dict) -> list[dict]:
    query = args.get("query", "")
    config_name = args.get("config_name")
    obj_type = args.get("obj_type")
    is_bsl = args.get("is_bsl")
    limit = int(args.get("limit", 20))

    # FTS5 filter expressions (column:value syntax for UNINDEXED columns not searchable,
    # so we filter with WHERE on the stored columns after FTS match)
    params: list = [query]
    post_filters = []

    if config_name is not None:
        post_filters.append("config_name = ?")
        params.append(config_name)
    if obj_type is not None:
        post_filters.append("obj_type = ?")
        params.append(obj_type)
    if is_bsl is not None:
        post_filters.append("is_bsl = ?")
        params.append(str(int(is_bsl)))

    params.append(limit)
    extra = ("AND " + " AND ".join(post_filters)) if post_filters else ""

    rows = conn.execute(
        f"""SELECT
                config_name, obj_type, obj_name, is_bsl,
                module_type, form_name, file_path,
                snippet(fts_modules, 7, '>>>', '<<<', '...', 32) AS snippet
            FROM fts_modules
            WHERE fts_modules MATCH ?
            {extra}
            ORDER BY rank
            LIMIT ?""",
        params,
    ).fetchall()

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

    rows = conn.execute(
        f"""SELECT o.id, o.config_name, o.obj_type, o.obj_name, o.is_bsl, o.xml_summary
            FROM fts_objects
            JOIN objects o ON fts_objects.rowid = o.id
            WHERE fts_objects MATCH ?
            {where_extra}
            ORDER BY rank LIMIT 20""",
        [name] + extra_params,
    ).fetchall()

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
        f"""SELECT m.content, m.file_path, m.module_type, m.form_name, m.line_count,
                   o.config_name, o.obj_type, o.obj_name, o.is_bsl
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
    is_bsl = args.get("is_bsl")

    filters = []
    params: list = []
    if obj_type:
        filters.append("obj_type = ?")
        params.append(obj_type)
    if config_name:
        filters.append("config_name = ?")
        params.append(config_name)
    if is_bsl is not None:
        filters.append("is_bsl = ?")
        params.append(int(is_bsl))

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = conn.execute(
        f"""SELECT o.config_name, o.obj_type, o.obj_name, o.is_bsl,
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

    search_args: dict = {"query": proc_name, "limit": 50}
    if config_name:
        search_args["config_name"] = config_name

    candidates = search_code(conn, search_args)
    pattern = re.compile(
        r"(Процедура|Функция|Procedure|Function)\s+" + re.escape(proc_name) + r"\s*\(",
        re.IGNORECASE,
    )

    results = []
    seen = set()
    for c in candidates:
        file_path = c["file_path"]
        if file_path in seen:
            continue

        row = conn.execute(
            "SELECT content FROM modules WHERE file_path = ?", (file_path,)
        ).fetchone()
        if not row:
            continue

        content = row["content"]
        for line_no, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                results.append({
                    "config_name": c["config_name"],
                    "obj_name": c["obj_name"],
                    "obj_type": c["obj_type"],
                    "module_type": c["module_type"],
                    "form_name": c.get("form_name"),
                    "file_path": file_path,
                    "definition_line": line_no,
                    "context": line.strip(),
                })
                seen.add(file_path)
                break

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
            "SELECT module_type, form_name, line_count FROM modules WHERE object_id = ? ORDER BY module_type",
            (obj["id"],),
        ).fetchall()
        results.append({
            **dict(obj),
            "modules": [dict(m) for m in modules],
        })

    return results[0] if len(results) == 1 else results
