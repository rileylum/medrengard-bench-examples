#!/usr/bin/env python3
"""Database migration tool: applies JSON schema migrations to SQLite databases."""

import json
import os
import re
import sqlite3
import sys

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VALID_TYPES = ("INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP")
OPERATION_TYPES = (
    "create_table", "add_column", "drop_column",
    "transform_data", "migrate_column_data", "backfill_data",
    "add_foreign_key", "drop_foreign_key", "create_index", "drop_index",
    "add_check_constraint", "drop_check_constraint",
)
FK_ACTIONS = ("CASCADE", "RESTRICT", "SET NULL", "NO ACTION", "SET DEFAULT")
# Operations that do not target a single table via the 'table' field.
TABLELESS_OPERATIONS = ("drop_index",)
VALUE_KEYWORDS = (
    "NULL", "TRUE", "FALSE", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME",
)
COLUMN_BOOL_FIELDS = ("primary_key", "auto_increment", "not_null", "unique")
TABLE_CONSTRAINT_KEYWORDS = ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")


class MigrationError(Exception):
    """Error with a full message (without the 'Error: ' prefix)."""


def schema_error(msg):
    return MigrationError(f"invalid migration schema: {msg}")


def op_error(msg):
    return MigrationError(f"invalid operation: {msg}")


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_identifier(value, what, ctx):
    if not isinstance(value, str):
        raise schema_error(f"{ctx}: {what} must be a string")
    if not IDENT_RE.match(value):
        raise schema_error(f"{ctx}: invalid {what} '{value}'")


def validate_column(col, ctx):
    if not isinstance(col, dict):
        raise schema_error(f"{ctx}: column definition must be an object")
    for field in ("name", "type"):
        if field not in col:
            raise schema_error(f"{ctx}: missing required field '{field}' in column")
    validate_identifier(col["name"], "column name", ctx)
    cctx = f"{ctx}, column '{col['name']}'"
    ctype = col["type"]
    if not isinstance(ctype, str) or ctype.upper() not in VALID_TYPES:
        raise schema_error(
            f"{cctx}: invalid column type {json.dumps(ctype)} "
            f"(expected one of {', '.join(VALID_TYPES)})"
        )
    for field in COLUMN_BOOL_FIELDS:
        if field in col and not isinstance(col[field], bool):
            raise schema_error(f"{cctx}: field '{field}' must be a boolean")
    if "default" in col:
        d = col["default"]
        if d is not None and not isinstance(d, str) and not (
            isinstance(d, (int, float)) and not isinstance(d, bool)
        ):
            raise schema_error(f"{cctx}: field 'default' must be a string or null")
    if col.get("auto_increment"):
        if ctype.upper() != "INTEGER" or not col.get("primary_key"):
            raise schema_error(
                f"{cctx}: auto_increment is only allowed on INTEGER PRIMARY KEY columns"
            )


def validate_operation(op, index, label="operation"):
    ctx = f"{label} {index}"
    if not isinstance(op, dict):
        raise schema_error(f"{ctx}: operation must be an object")
    if "type" not in op:
        raise schema_error(f"{ctx}: missing required field 'type'")
    otype = op["type"]
    if not isinstance(otype, str) or otype not in OPERATION_TYPES:
        raise schema_error(f"{ctx}: unknown operation type {json.dumps(otype)}")
    ctx = f"{label} {index} ({otype})"
    if otype not in TABLELESS_OPERATIONS:
        if "table" not in op:
            raise schema_error(f"{ctx}: missing required field 'table'")
        validate_identifier(op["table"], "table name", ctx)

    if otype == "create_table":
        if "columns" not in op:
            raise schema_error(f"{ctx}: missing required field 'columns'")
        cols = op["columns"]
        if not isinstance(cols, list):
            raise schema_error(f"{ctx}: field 'columns' must be an array")
        if not cols:
            raise schema_error(f"{ctx}: field 'columns' must not be empty")
        seen = set()
        pk_count = 0
        for col in cols:
            validate_column(col, ctx)
            key = col["name"].lower()
            if key in seen:
                raise schema_error(f"{ctx}: duplicate column name '{col['name']}'")
            seen.add(key)
            if col.get("primary_key"):
                pk_count += 1
        if pk_count > 1:
            raise schema_error(f"{ctx}: at most one column can be a primary key")
    elif otype == "add_column":
        if "column" not in op:
            raise schema_error(f"{ctx}: missing required field 'column'")
        validate_column(op["column"], ctx)
    elif otype == "drop_column":
        if "column" not in op:
            raise schema_error(f"{ctx}: missing required field 'column'")
        validate_identifier(op["column"], "column name", ctx)
    elif otype == "transform_data":
        if "transformations" not in op:
            raise schema_error(f"{ctx}: missing required field 'transformations'")
        trs = op["transformations"]
        if not isinstance(trs, list):
            raise schema_error(f"{ctx}: field 'transformations' must be an array")
        if not trs:
            raise schema_error(f"{ctx}: field 'transformations' must not be empty")
        for j, tr in enumerate(trs):
            tctx = f"{ctx}, transformation {j}"
            if not isinstance(tr, dict):
                raise schema_error(f"{tctx}: transformation must be an object")
            for field in ("column", "expression"):
                if field not in tr:
                    raise schema_error(f"{tctx}: missing required field '{field}'")
            validate_identifier(tr["column"], "column name", tctx)
            validate_expression(tr["expression"], "expression", tctx)
            if "type" in tr:
                t = tr["type"]
                if not isinstance(t, str) or t.upper() not in VALID_TYPES:
                    raise schema_error(
                        f"{tctx}: invalid column type {json.dumps(t)} "
                        f"(expected one of {', '.join(VALID_TYPES)})"
                    )
    elif otype == "migrate_column_data":
        for field in ("from_column", "to_column"):
            if field not in op:
                raise schema_error(f"{ctx}: missing required field '{field}'")
            validate_identifier(op[field], "column name", ctx)
        if "default_value" in op:
            validate_value(op["default_value"], "default_value", ctx)
    elif otype == "backfill_data":
        for field in ("column", "value"):
            if field not in op:
                raise schema_error(f"{ctx}: missing required field '{field}'")
        validate_identifier(op["column"], "column name", ctx)
        validate_value(op["value"], "value", ctx)
        if op.get("where") is not None:
            validate_expression(op["where"], "where", ctx)
    elif otype == "add_foreign_key":
        _require_fields(op, ("name", "columns", "references"), ctx)
        validate_identifier(op["name"], "constraint name", ctx)
        validate_column_list(op["columns"], "columns", ctx)
        ref = op["references"]
        if not isinstance(ref, dict):
            raise schema_error(f"{ctx}: field 'references' must be an object")
        for field in ("table", "columns"):
            if field not in ref:
                raise schema_error(
                    f"{ctx}: missing required field '{field}' in 'references'"
                )
        validate_identifier(ref["table"], "referenced table name", ctx)
        validate_column_list(ref["columns"], "references.columns", ctx)
        if len(ref["columns"]) != len(op["columns"]):
            raise schema_error(
                f"{ctx}: number of columns ({len(op['columns'])}) does not match "
                f"number of referenced columns ({len(ref['columns'])})"
            )
        for field in ("on_delete", "on_update"):
            if op.get(field) is not None:
                v = op[field]
                if not isinstance(v, str) or normalize_action(v) not in FK_ACTIONS:
                    raise schema_error(
                        f"{ctx}: invalid {field} action {json.dumps(v)} "
                        f"(expected one of {', '.join(FK_ACTIONS)})"
                    )
    elif otype in ("drop_foreign_key", "drop_check_constraint"):
        _require_fields(op, ("name",), ctx)
        validate_identifier(op["name"], "constraint name", ctx)
    elif otype == "create_index":
        _require_fields(op, ("name", "columns"), ctx)
        validate_identifier(op["name"], "index name", ctx)
        validate_column_list(op["columns"], "columns", ctx)
        if "unique" in op and op["unique"] is not None and not isinstance(op["unique"], bool):
            raise schema_error(f"{ctx}: field 'unique' must be a boolean")
    elif otype == "drop_index":
        _require_fields(op, ("name",), ctx)
        validate_identifier(op["name"], "index name", ctx)
    elif otype == "add_check_constraint":
        _require_fields(op, ("name", "expression"), ctx)
        validate_identifier(op["name"], "constraint name", ctx)
        validate_expression(op["expression"], "expression", ctx)


def _require_fields(op, fields, ctx):
    for field in fields:
        if field not in op:
            raise schema_error(f"{ctx}: missing required field '{field}'")


def validate_column_list(cols, field, ctx):
    if not isinstance(cols, list):
        raise schema_error(f"{ctx}: field '{field}' must be an array")
    if not cols:
        raise schema_error(f"{ctx}: field '{field}' must not be empty")
    seen = set()
    for c in cols:
        validate_identifier(c, "column name", ctx)
        if c.lower() in seen:
            raise schema_error(f"{ctx}: duplicate column '{c}' in field '{field}'")
        seen.add(c.lower())


def normalize_action(action):
    return " ".join(action.upper().split())


def validate_expression(value, field, ctx):
    if not isinstance(value, str):
        raise schema_error(f"{ctx}: field '{field}' must be a string")
    if not value.strip():
        raise schema_error(f"{ctx}: field '{field}' must not be empty")
    if ";" in _strip_literals(value):
        raise schema_error(f"{ctx}: field '{field}' must be a single SQL expression")


def validate_value(value, field, ctx):
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if ";" in _strip_literals(value):
            raise schema_error(
                f"{ctx}: field '{field}' must be a single SQL expression"
            )
        return
    raise schema_error(f"{ctx}: field '{field}' must be a string, number, boolean or null")


def _strip_literals(sql):
    """Remove quoted strings/identifiers from an SQL fragment."""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] in "'\"`[":
            i = _skip_quoted(sql, i)
            out.append(" ")
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def validate_migration(mig):
    if not isinstance(mig, dict):
        raise schema_error("migration must be a JSON object")
    for field in ("version", "description", "operations"):
        if field not in mig:
            raise schema_error(f"missing required field '{field}'")
    if not is_int(mig["version"]) or mig["version"] <= 0:
        raise schema_error("field 'version' must be a positive integer")
    if not isinstance(mig["description"], str):
        raise schema_error("field 'description' must be a string")
    if not isinstance(mig["operations"], list):
        raise schema_error("field 'operations' must be an array")
    for i, op in enumerate(mig["operations"]):
        validate_operation(op, i)
    if mig.get("rollback_operations") is not None:
        rops = mig["rollback_operations"]
        if not isinstance(rops, list):
            raise schema_error("field 'rollback_operations' must be an array")
        for i, op in enumerate(rops):
            validate_operation(op, i, "rollback operation")


# --------------------------------------------------------------------------
# SQL generation
# --------------------------------------------------------------------------

SIMPLE_DEFAULT_RE = re.compile(
    r"""^(
        [+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)? |   # numeric literal
        0[xX][0-9A-Fa-f]+ |                       # hex literal
        '([^']|'')*' |                            # string literal
        [xX]'[0-9A-Fa-f]*' |                      # blob literal
        [A-Za-z_][A-Za-z0-9_]* |                  # keyword (NULL, CURRENT_TIMESTAMP...)
        \(.*\)                                    # already parenthesized
    )$""",
    re.VERBOSE | re.DOTALL,
)


def default_sql(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    expr = value.strip()
    if SIMPLE_DEFAULT_RE.match(expr):
        return expr
    return f"({expr})"


def column_sql(col):
    parts = [quote_ident(col["name"]), col["type"].upper()]
    if col.get("primary_key"):
        parts.append("PRIMARY KEY")
        if col.get("auto_increment"):
            parts.append("AUTOINCREMENT")
    if col.get("not_null"):
        parts.append("NOT NULL")
    if col.get("unique"):
        parts.append("UNIQUE")
    if col.get("default") is not None:
        parts.append("DEFAULT " + default_sql(col["default"]))
    return " ".join(parts)


# --------------------------------------------------------------------------
# Parsing CREATE TABLE statements (used by drop_column)
# --------------------------------------------------------------------------

def _skip_quoted(sql, i):
    """sql[i] is an opening quote char; return index just past the closing quote."""
    open_ch = sql[i]
    close_ch = "]" if open_ch == "[" else open_ch
    i += 1
    n = len(sql)
    while i < n:
        if sql[i] == close_ch:
            if close_ch != "]" and i + 1 < n and sql[i + 1] == close_ch:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def split_create_table(sql):
    """Return (prefix, [definitions], suffix) for a CREATE TABLE statement."""
    n = len(sql)
    i = 0
    start = None
    depth = 0
    defs = []
    cur_start = None
    while i < n:
        ch = sql[i]
        if ch in "'\"`[":
            i = _skip_quoted(sql, i)
            continue
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "(":
            depth += 1
            if depth == 1 and start is None:
                start = i
                cur_start = i + 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                defs.append(sql[cur_start:i].strip())
                return sql[:start], defs, sql[i + 1:]
        elif ch == "," and depth == 1:
            defs.append(sql[cur_start:i].strip())
            cur_start = i + 1
        i += 1
    raise MigrationError("SQL error: unable to parse table definition")


def leading_name(definition):
    """Return (name, was_quoted) of the first token of a definition."""
    d = definition.lstrip()
    if not d:
        return "", False
    if d[0] in "'\"`[":
        end = _skip_quoted(d, 0)
        raw = d[1:end - 1]
        q = d[0]
        if q != "[":
            raw = raw.replace(q + q, q)
        return raw, True
    m = re.match(r"[^\s(,]+", d)
    return (m.group(0) if m else ""), False


def mentions_identifier(sql, name):
    """Rough check whether an SQL fragment references identifier `name`."""
    pattern = (
        r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])'
    )
    return re.search(pattern, sql, re.IGNORECASE) is not None


# --------------------------------------------------------------------------
# Database operations
# --------------------------------------------------------------------------

def table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def table_info(conn, table):
    return conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()


def apply_create_table(conn, op):
    table = op["table"]
    if table_exists(conn, table):
        raise op_error(f"table '{table}' already exists")
    cols = ", ".join(column_sql(c) for c in op["columns"])
    conn.execute(f"CREATE TABLE {quote_ident(table)} ({cols})")


def apply_add_column(conn, op):
    table = op["table"]
    col = op["column"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    existing = [r[1] for r in table_info(conn, table)]
    if col["name"] in existing or col["name"].lower() in (e.lower() for e in existing):
        raise op_error(f"column '{col['name']}' already exists in table '{table}'")
    conn.execute(f"ALTER TABLE {quote_ident(table)} ADD COLUMN {column_sql(col)}")


def table_sql(conn, table):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row[0] if row else None


def table_defs(conn, table):
    """Return (definitions, suffix) of the table's CREATE TABLE statement."""
    _prefix, defs, suffix = split_create_table(table_sql(conn, table))
    return defs, suffix


def _next_token(s):
    """Return (token, was_quoted, rest) for the first token of s."""
    s = s.lstrip()
    if not s:
        return "", False, ""
    if s[0] in "'\"`[":
        end = _skip_quoted(s, 0)
        raw = s[1:end - 1]
        if s[0] != "[":
            raw = raw.replace(s[0] * 2, s[0])
        return raw, True, s[end:]
    m = re.match(r"[^\s(,]+", s)
    if not m:
        return s[0], False, s[1:]
    return m.group(0), False, s[m.end():]


def is_table_constraint(definition):
    name, quoted = leading_name(definition)
    return not quoted and name.upper() in TABLE_CONSTRAINT_KEYWORDS


def named_constraint(definition):
    """Return (name, kind) for 'CONSTRAINT name KIND ...' definitions, else None."""
    tok, quoted, rest = _next_token(definition)
    if quoted or tok.upper() != "CONSTRAINT":
        return None
    name, _q, rest = _next_token(rest)
    kind, _q, _rest = _next_token(rest)
    return name, kind.upper()


def find_named_constraint(defs, name):
    """Return (index, kind) of a named table constraint (case-insensitive)."""
    for i, d in enumerate(defs):
        nc = named_constraint(d)
        if nc and nc[0].lower() == name.lower():
            return i, nc[1]
    return None, None


def _unused_name(conn, base):
    name = base
    k = 0
    while conn.execute(
        "SELECT 1 FROM sqlite_master WHERE lower(name) = lower(?)", (name,)
    ).fetchone():
        k += 1
        name = f"{base}{k}"
    return name


def has_rowid_alias(conn, table):
    """True if the table has an INTEGER PRIMARY KEY column (alias of rowid)."""
    pk = [r for r in table_info(conn, table) if r[5]]
    return len(pk) == 1 and (pk[0][2] or "").upper() == "INTEGER"


def rebuild_table(conn, table, new_defs, suffix, copy_cols, keep_extra=None,
                  extra_cols=None, from_sql=None):
    """Recreate `table` with new column/constraint definitions, preserving data,
    indexes and triggers (those for which keep_extra(sql) is true).

    The source table is aliased as "_src" in the copy query. `extra_cols` is a
    list of (column, select expression) pairs for additional target columns;
    `from_sql` optionally replaces the FROM clause of the copy query."""
    extra_cols = extra_cols or []
    preserve_rowid = (
        not has_rowid_alias(conn, table)
        and "WITHOUT ROWID" not in " ".join(suffix.upper().split())
    )
    extras = conn.execute(
        "SELECT type, sql FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, rowid",
        (table,),
    ).fetchall()
    seq = None
    if table_exists(conn, "sqlite_sequence"):
        r = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
        ).fetchone()
        seq = r[0] if r else None

    tmp = _unused_name(conn, f"_{table}_new")
    target = [quote_ident(c) for c in copy_cols]
    source = [f"_src.{quote_ident(c)}" for c in copy_cols]
    for c, expr in extra_cols:
        target.append(quote_ident(c))
        source.append(expr)
    conn.execute(f"CREATE TABLE {quote_ident(tmp)} ({', '.join(new_defs)}){suffix}")
    if preserve_rowid and has_rowid_alias(conn, tmp):
        preserve_rowid = False
    if preserve_rowid:
        target.insert(0, "rowid")
        source.insert(0, "_src.rowid")
    if from_sql is None:
        from_sql = f"{quote_ident(table)} AS _src"
    try:
        conn.execute(
            f"INSERT INTO {quote_ident(tmp)} ({', '.join(target)}) "
            f"SELECT {', '.join(source)} FROM {from_sql}"
        )
    except sqlite3.IntegrityError as e:
        raise integrity_error(e, table)
    conn.execute(f"DROP TABLE {quote_ident(table)}")
    # Legacy mode: do not rewrite references to the table in other schema objects
    # (child tables' REFERENCES clauses must keep pointing at the original name).
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute(f"ALTER TABLE {quote_ident(tmp)} RENAME TO {quote_ident(table)}")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")

    for _type, sql in extras:
        if keep_extra is not None and not keep_extra(sql):
            continue
        conn.execute(sql)

    if seq is not None:
        cur = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (seq, table)
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq)
            )


def integrity_error(e, table):
    msg = str(e)
    if "CHECK constraint failed" in msg:
        return MigrationError(f"check constraint violation: {msg} (table '{table}')")
    if "FOREIGN KEY constraint failed" in msg:
        return MigrationError(f"foreign key violation in table '{table}': {msg}")
    if "UNIQUE constraint failed" in msg:
        return MigrationError(f"unique constraint violation: {msg}")
    if "NOT NULL constraint failed" in msg:
        return MigrationError(f"not null constraint violation: {msg}")
    return MigrationError(f"constraint violation in table '{table}': {msg}")


def all_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]


def foreign_key_list(conn, table):
    """List of (id, parent_table, [from cols], [to cols]) for a table."""
    rows = conn.execute(f"PRAGMA foreign_key_list({quote_ident(table)})").fetchall()
    fks = {}
    for r in rows:
        fid, _seq, parent, frm, to = r[0], r[1], r[2], r[3], r[4]
        entry = fks.setdefault(fid, [fid, parent, [], []])
        entry[2].append(frm)
        entry[3].append(to)
    return [tuple(v) for v in fks.values()]


def child_tables(conn, table):
    """Tables (other than `table`) having a foreign key referencing `table`."""
    out = []
    for t in all_tables(conn):
        if t == table:
            continue
        if any(fk[1].lower() == table.lower() for fk in foreign_key_list(conn, t)):
            out.append(t)
    return out


def check_foreign_keys(conn, tables):
    for t in tables:
        try:
            row = conn.execute(
                f"PRAGMA foreign_key_check({quote_ident(t)})"
            ).fetchone()
        except sqlite3.Error as e:
            raise MigrationError(f"foreign key error on table '{t}': {e}")
        if row is not None:
            raise MigrationError(
                f"foreign key violation: referenced row in '{row[2]}' table does not exist"
            )


def apply_drop_column(conn, op):
    table = op["table"]
    column = op["column"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    info = table_info(conn, table)
    names = [r[1] for r in info]
    if column not in names:
        raise op_error(f"column '{column}' does not exist in table '{table}'")
    if len(names) == 1:
        raise op_error(
            f"cannot drop column '{column}': it is the only column in table '{table}'"
        )
    if any(r[1] == column and r[5] for r in info):
        raise op_error(
            f"cannot drop column '{column}': it is a PRIMARY KEY column of table '{table}'"
        )
    for child in child_tables(conn, table):
        for _fid, _parent, _frm, to in foreign_key_list(conn, child):
            if _parent.lower() == table.lower() and any(
                (c or "").lower() == column.lower() for c in to
            ):
                raise op_error(
                    f"cannot drop column '{column}': it is referenced by a foreign key "
                    f"in table '{child}'"
                )

    defs, suffix = table_defs(conn, table)
    kept_defs = []
    for d in defs:
        if is_table_constraint(d):
            # Table-level constraint: keep only if it doesn't involve the column.
            # For foreign keys only the local column list matters (the
            # REFERENCES part names columns of another table).
            m = re.search(r"\bREFERENCES\b", _strip_literals(d), re.IGNORECASE)
            local_part = d[:m.start()] if m else d
            if not mentions_identifier(local_part, column):
                kept_defs.append(d)
            continue
        if leading_name(d)[0].lower() == column.lower():
            continue
        kept_defs.append(d)

    keep_cols = [n for n in names if n != column]
    rebuild_table(conn, table, kept_defs, suffix, keep_cols,
                  keep_extra=lambda sql: not mentions_identifier(sql, column))


# --------------------------------------------------------------------------
# Foreign keys, indexes and check constraints
# --------------------------------------------------------------------------

def _resolve_columns(conn, table, columns):
    """Map requested column names to actual (name, declared type); None if missing."""
    info = {r[1].lower(): (r[1], (r[2] or "").upper()) for r in table_info(conn, table)}
    return [info.get(c.lower()) for c in columns]


def _type_class(t):
    t = (t or "").upper()
    if not t:
        return "ANY"
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t:
        return "BLOB"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if any(k in t for k in ("TIMESTAMP", "DATE", "TIME")):
        return "TIMESTAMP"
    return "NUMERIC"


_COMPATIBLE = {
    frozenset(("INTEGER", "REAL")), frozenset(("INTEGER", "NUMERIC")),
    frozenset(("REAL", "NUMERIC")), frozenset(("TIMESTAMP", "TEXT")),
    frozenset(("TIMESTAMP", "INTEGER")), frozenset(("TIMESTAMP", "REAL")),
    frozenset(("TIMESTAMP", "NUMERIC")),
}


def types_compatible(a, b):
    ca, cb = _type_class(a), _type_class(b)
    return ca == cb or "ANY" in (ca, cb) or frozenset((ca, cb)) in _COMPATIBLE


def _is_parent_key(conn, table, columns):
    """True if columns form the PRIMARY KEY or a UNIQUE index of table."""
    want = sorted(c.lower() for c in columns)
    info = table_info(conn, table)
    pk = sorted(r[1].lower() for r in info if r[5])
    if pk and pk == want:
        return True
    for idx in conn.execute(f"PRAGMA index_list({quote_ident(table)})").fetchall():
        name, unique, partial = idx[1], idx[2], idx[4] if len(idx) > 4 else 0
        if not unique or partial:
            continue
        cols = [r[2] for r in conn.execute(
            f"PRAGMA index_info({quote_ident(name)})").fetchall()]
        if None not in cols and sorted(c.lower() for c in cols) == want:
            return True
    return False


def _find_cycle(conn, start, target):
    """Return path from start to target following FK edges, or None."""
    graph = {}
    for t in all_tables(conn):
        graph[t.lower()] = (t, [fk[1] for fk in foreign_key_list(conn, t)])
    stack = [(start, [start])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node.lower() == target.lower():
            return path
        if node.lower() in seen:
            continue
        seen.add(node.lower())
        for nxt in graph.get(node.lower(), (node, []))[1]:
            if nxt.lower() == node.lower():
                continue
            real = graph.get(nxt.lower(), (nxt, []))[0]
            stack.append((real, path + [real]))
    return None


def apply_add_foreign_key(conn, op):
    table = op["table"]
    name = op["name"]
    ref = op["references"]
    ref_table = ref["table"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    local = _resolve_columns(conn, table, op["columns"])
    for c, r in zip(op["columns"], local):
        if r is None:
            raise op_error(f"column '{c}' does not exist in table '{table}'")
    real_ref = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND lower(name) = lower(?)",
        (ref_table,),
    ).fetchone()
    if real_ref is None:
        raise MigrationError(
            f"cannot add foreign key: referenced table '{ref_table}' does not exist"
        )
    ref_table = real_ref[0]
    remote = _resolve_columns(conn, ref_table, ref["columns"])
    for c, r in zip(ref["columns"], remote):
        if r is None:
            raise MigrationError(
                f"cannot add foreign key: referenced column '{ref_table}.{c}' does not exist"
            )
    for (lc, lt), (rc, rt) in zip(local, remote):
        if not types_compatible(lt, rt):
            raise MigrationError(
                f"cannot add foreign key: column '{table}.{lc}' ({lt or 'untyped'}) is "
                f"not compatible with referenced column '{ref_table}.{rc}' "
                f"({rt or 'untyped'})"
            )
    if not _is_parent_key(conn, ref_table, [r[0] for r in remote]):
        raise MigrationError(
            f"cannot add foreign key: referenced column(s) "
            f"{', '.join(repr(ref_table + '.' + r[0]) for r in remote)} "
            f"must be a PRIMARY KEY or have a UNIQUE constraint"
        )

    defs, suffix = table_defs(conn, table)
    idx, _kind = find_named_constraint(defs, name)
    if idx is not None:
        raise op_error(f"constraint '{name}' already exists on table '{table}'")

    if ref_table.lower() != table.lower():
        path = _find_cycle(conn, ref_table, table)
        if path is not None:
            cycle = " -> ".join([table] + path)
            raise MigrationError(f"circular foreign key dependency detected: {cycle}")

    clause = (
        f"CONSTRAINT {quote_ident(name)} FOREIGN KEY "
        f"({', '.join(quote_ident(r[0]) for r in local)}) "
        f"REFERENCES {quote_ident(ref_table)} "
        f"({', '.join(quote_ident(r[0]) for r in remote)})"
    )
    if op.get("on_delete") is not None:
        clause += " ON DELETE " + normalize_action(op["on_delete"])
    if op.get("on_update") is not None:
        clause += " ON UPDATE " + normalize_action(op["on_update"])

    cols = [r[1] for r in table_info(conn, table)]
    rebuild_table(conn, table, defs + [clause], suffix, cols)
    check_foreign_keys(conn, [table])


def apply_drop_foreign_key(conn, op):
    table = op["table"]
    name = op["name"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    defs, suffix = table_defs(conn, table)
    idx, kind = find_named_constraint(defs, name)
    if idx is None or kind != "FOREIGN":
        raise op_error(f"foreign key '{name}' does not exist on table '{table}'")
    new_defs = defs[:idx] + defs[idx + 1:]
    cols = [r[1] for r in table_info(conn, table)]
    rebuild_table(conn, table, new_defs, suffix, cols)


def apply_add_check_constraint(conn, op):
    table = op["table"]
    name = op["name"]
    expr = op["expression"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    defs, suffix = table_defs(conn, table)
    idx, _kind = find_named_constraint(defs, name)
    if idx is not None:
        raise op_error(f"constraint '{name}' already exists on table '{table}'")
    _check_expression(conn, table, expr, f"check constraint '{name}'")
    bad = conn.execute(
        f"SELECT COUNT(*) FROM {quote_ident(table)} WHERE NOT ({expr})"
    ).fetchone()[0]
    if bad:
        raise MigrationError(
            f"check constraint violation: {bad} existing row(s) in table '{table}' "
            f"do not satisfy constraint '{name}' ({expr})"
        )
    clause = f"CONSTRAINT {quote_ident(name)} CHECK ({expr})"
    cols = [r[1] for r in table_info(conn, table)]
    rebuild_table(conn, table, defs + [clause], suffix, cols)


def apply_drop_check_constraint(conn, op):
    table = op["table"]
    name = op["name"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    defs, suffix = table_defs(conn, table)
    idx, kind = find_named_constraint(defs, name)
    if idx is None or kind != "CHECK":
        raise op_error(f"check constraint '{name}' does not exist on table '{table}'")
    new_defs = defs[:idx] + defs[idx + 1:]
    cols = [r[1] for r in table_info(conn, table)]
    rebuild_table(conn, table, new_defs, suffix, cols)


def apply_create_index(conn, op):
    table = op["table"]
    name = op["name"]
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")
    resolved = _resolve_columns(conn, table, op["columns"])
    for c, r in zip(op["columns"], resolved):
        if r is None:
            raise op_error(f"column '{c}' does not exist in table '{table}'")
    existing = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if existing is not None:
        if existing[0] == "index":
            raise MigrationError(f"index '{existing[1]}' already exists")
        raise MigrationError(
            f"cannot create index '{name}': name is already used by {existing[0]} "
            f"'{existing[1]}'"
        )
    unique = bool(op.get("unique"))
    col_sql = ", ".join(quote_ident(r[0]) for r in resolved)
    try:
        conn.execute(
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {quote_ident(name)} "
            f"ON {quote_ident(table)} ({col_sql})"
        )
    except sqlite3.IntegrityError as e:
        raise MigrationError(
            f"cannot create unique index '{name}': duplicate values exist in "
            f"column(s) {', '.join(repr(r[0]) for r in resolved)} of table "
            f"'{table}' ({e})"
        )


def apply_drop_index(conn, op):
    name = op["name"]
    row = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND lower(name) = lower(?)",
        (name,),
    ).fetchone()
    if row is None:
        raise op_error(f"index '{name}' does not exist")
    if row[1] is None:
        raise op_error(
            f"cannot drop index '{name}': it is created automatically by a "
            f"PRIMARY KEY or UNIQUE constraint"
        )
    conn.execute(f"DROP INDEX {quote_ident(row[0])}")


# --------------------------------------------------------------------------
# Data migrations
# --------------------------------------------------------------------------

def _require_table(conn, table):
    if not table_exists(conn, table):
        raise op_error(f"table '{table}' does not exist")


def _column_types(conn, table):
    """Map of column name -> declared type (upper case)."""
    return {r[1]: (r[2] or "").upper() for r in table_info(conn, table)}


def _require_column(cols, table, column):
    if column not in cols:
        raise op_error(f"column '{column}' does not exist in table '{table}'")


def _check_expression(conn, table, expr, what):
    """Make sure an SQL expression compiles against the table."""
    try:
        conn.execute(f"SELECT ({expr}) FROM {quote_ident(table)} LIMIT 0").fetchall()
    except sqlite3.Error as e:
        raise op_error(f"invalid SQL expression in {what} '{expr}': {e}")


def _check_where(conn, table, where):
    try:
        conn.execute(
            f"SELECT 1 FROM {quote_ident(table)} WHERE ({where}) LIMIT 0"
        ).fetchall()
    except sqlite3.Error as e:
        raise op_error(f"invalid SQL expression in where clause '{where}': {e}")


def _run_update(conn, sql, params, table, column):
    try:
        cur = conn.execute(sql, params)
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if "datatype mismatch" in msg.lower():
            raise MigrationError(
                f"data type mismatch: cannot write to column '{column}' "
                f"of table '{table}': {e}"
            )
        raise integrity_error(e, table)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")
    check_foreign_keys(conn, [table] + child_tables(conn, table))
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0


def _check_types(conn, table, column, declared):
    """Detect values that could not be coerced to a numeric column's type."""
    if "INT" in declared:
        bad_types = ("text", "blob")
        label = "INTEGER"
    elif any(k in declared for k in ("REAL", "FLOA", "DOUB")):
        bad_types = ("text", "blob")
        label = "REAL"
    else:
        return
    placeholders = ", ".join("?" for _ in bad_types)
    row = conn.execute(
        f"SELECT {quote_ident(column)} FROM {quote_ident(table)} "
        f"WHERE typeof({quote_ident(column)}) IN ({placeholders}) LIMIT 1",
        bad_types,
    ).fetchone()
    if row is not None:
        val = row[0]
        if isinstance(val, bytes):
            val = "<blob>"
        raise MigrationError(
            f"data type mismatch: value {json.dumps(val)} cannot be stored "
            f"in {label} column '{column}' of table '{table}'"
        )


def _value_sql(value, cols):
    """Return (sql_fragment, params) for a backfill value."""
    if value is None:
        return "NULL", ()
    if isinstance(value, bool):
        return "?", (1 if value else 0,)
    if isinstance(value, (int, float)):
        return "?", (value,)
    stripped = value.strip()
    if (
        IDENT_RE.match(stripped)
        and stripped.upper() not in VALUE_KEYWORDS
        and stripped.lower() not in (c.lower() for c in cols)
    ):
        # A bare word that isn't a column or keyword: treat as a string constant.
        return "?", (value,)
    if not stripped:
        return "?", (value,)
    return f"({value})", ()


def apply_transform_data(conn, op):
    table = op["table"]
    _require_table(conn, table)
    total = 0
    for tr in op["transformations"]:
        cols = _column_types(conn, table)
        column = tr["column"]
        match = next((c for c in cols if c.lower() == column.lower()), None)
        if match is None:
            ctype = tr.get("type")
            type_sql = f" {ctype.upper()}" if ctype else ""
            conn.execute(
                f"ALTER TABLE {quote_ident(table)} "
                f"ADD COLUMN {quote_ident(column)}{type_sql}"
            )
            cols = _column_types(conn, table)
            match = column
        expr = tr["expression"]
        _check_expression(conn, table, expr, f"transformation for column '{match}'")
        total = _run_update(
            conn,
            f"UPDATE {quote_ident(table)} SET {quote_ident(match)} = ({expr})",
            (), table, match,
        )
        _check_types(conn, table, match, cols[match])
    return {"rows_affected": total}


def apply_migrate_column_data(conn, op):
    table = op["table"]
    _require_table(conn, table)
    cols = _column_types(conn, table)
    src, dst = op["from_column"], op["to_column"]
    _require_column(cols, table, src)
    _require_column(cols, table, dst)
    default = op.get("default_value")
    if default is None:
        expr, params = quote_ident(src), ()
    else:
        if isinstance(default, bool):
            default = 1 if default else 0
        expr, params = f"COALESCE({quote_ident(src)}, ?)", (default,)
    rows = _run_update(
        conn,
        f"UPDATE {quote_ident(table)} SET {quote_ident(dst)} = {expr}",
        params, table, dst,
    )
    _check_types(conn, table, dst, cols[dst])
    return {"from_column": src, "to_column": dst, "rows_affected": rows}


def apply_backfill_data(conn, op):
    table = op["table"]
    _require_table(conn, table)
    cols = _column_types(conn, table)
    column = op["column"]
    _require_column(cols, table, column)
    value_sql, params = _value_sql(op["value"], cols)
    if not params and value_sql != "NULL":
        _check_expression(conn, table, value_sql, f"value for column '{column}'")
    sql = f"UPDATE {quote_ident(table)} SET {quote_ident(column)} = {value_sql}"
    where = op.get("where")
    if where is not None:
        _check_where(conn, table, where)
        sql += f" WHERE ({where})"
    rows = _run_update(conn, sql, params, table, column)
    _check_types(conn, table, column, cols[column])
    return {"column": column, "rows_affected": rows}


OPERATIONS = {
    "create_table": apply_create_table,
    "add_column": apply_add_column,
    "drop_column": apply_drop_column,
    "transform_data": apply_transform_data,
    "migrate_column_data": apply_migrate_column_data,
    "backfill_data": apply_backfill_data,
    "add_foreign_key": apply_add_foreign_key,
    "drop_foreign_key": apply_drop_foreign_key,
    "create_index": apply_create_index,
    "drop_index": apply_drop_index,
    "add_check_constraint": apply_add_check_constraint,
    "drop_check_constraint": apply_drop_check_constraint,
}


# --------------------------------------------------------------------------
# Rollback support: capturing metadata and reversing operations
# --------------------------------------------------------------------------

IRREVERSIBLE_OPERATIONS = ("transform_data", "backfill_data")
FK_VIOLATION_MSG = "cannot rollback: foreign key constraint violation"


class RollbackError(MigrationError):
    """Rollback error carrying its final message."""


def rollback_failed(msg):
    return RollbackError(f"rollback failed: {msg}")


def _guard(fn):
    """Run a rollback step, normalizing errors to rollback error messages."""
    try:
        return fn()
    except RollbackError:
        raise
    except MigrationError as e:
        msg = str(e)
        if msg.startswith("invalid operation: "):
            msg = msg[len("invalid operation: "):]
        low = msg.lower()
        if ("foreign key violation" in low or "foreign key constraint" in low
                or "referenced by a foreign key" in low):
            raise RollbackError(FK_VIOLATION_MSG)
        raise rollback_failed(msg)
    except sqlite3.IntegrityError as e:
        if "FOREIGN KEY" in str(e).upper():
            raise RollbackError(FK_VIOLATION_MSG)
        raise rollback_failed(str(e))
    except sqlite3.Error as e:
        raise rollback_failed(f"SQL error: {e}")


def rollback_event(op, version, explicit=False):
    """operation_rolled_back event for a stored (or explicit rollback) operation."""
    event = operation_event("operation_rolled_back", op, version)
    if op["type"] == "migrate_column_data":
        if explicit:
            event["from_column"] = op["from_column"]
            event["to_column"] = op["to_column"]
        else:
            # Automatic rollback copies the data back the other way.
            event["from_column"] = op["to_column"]
            event["to_column"] = op["from_column"]
    elif op["type"] == "backfill_data":
        event["column"] = op["column"]
    event["version"] = event.pop("version")
    return event


def _encode_value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$blob": bytes(v).hex()}
    return v


def _decode_value(v):
    if isinstance(v, dict) and "$blob" in v:
        return bytes.fromhex(v["$blob"])
    return v


def _real_table(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND lower(name) = lower(?)",
        (table,),
    ).fetchone()
    return row[0] if row else None


def _table_suffix_without_rowid(suffix):
    return "WITHOUT ROWID" in " ".join((suffix or "").upper().split())


def _local_part(definition):
    m = re.search(r"\bREFERENCES\b", _strip_literals(definition), re.IGNORECASE)
    return definition[:m.start()] if m else definition


def _parse_fk_clause(sql):
    """Best-effort parse of 'CONSTRAINT n FOREIGN KEY (...) REFERENCES t (...) ...'."""
    out = {}
    stripped = _strip_literals(sql)
    m = re.search(r"\bFOREIGN\s+KEY\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if m:
        out["columns"] = [_unquote(c) for c in m.group(1).split(",")]
    m = re.search(r"\bREFERENCES\s+(\"(?:[^\"]|\"\")*\"|[^\s(]+)\s*(?:\(([^)]*)\))?",
                  sql, re.IGNORECASE)
    if m:
        ref = {"table": _unquote(m.group(1))}
        if m.group(2):
            ref["columns"] = [_unquote(c) for c in m.group(2).split(",")]
        out["references"] = ref
    for field, kw in (("on_delete", "DELETE"), ("on_update", "UPDATE")):
        m = re.search(
            r"\bON\s+" + kw + r"\s+(SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION|CASCADE|RESTRICT)",
            stripped, re.IGNORECASE,
        )
        if m:
            out[field] = normalize_action(m.group(1))
    return out


def _unquote(token):
    t = token.strip()
    if len(t) >= 2 and t[0] in "\"`[" :
        close = "]" if t[0] == "[" else t[0]
        if t[-1] == close:
            inner = t[1:-1]
            return inner if t[0] == "[" else inner.replace(close * 2, close)
    return t


def _check_expression_of(definition):
    """Return the expression inside 'CONSTRAINT n CHECK (expr)'."""
    _tok, _q, rest = _next_token(definition)   # CONSTRAINT
    _name, _q, rest = _next_token(rest)         # name
    rest = rest.lstrip()
    if rest[:5].upper() == "CHECK":
        rest = rest[5:].strip()
    if rest.startswith("("):
        _p, defs, _s = split_create_table(rest)
        return ", ".join(defs)
    return rest


def capture_rollback_info(conn, op):
    """Return a copy of op enriched with what is needed to reverse it later.
    Must be called before the operation is applied."""
    info = dict(op)
    try:
        otype = op["type"]
        if otype == "drop_column":
            _capture_drop_column(conn, op, info)
        elif otype in ("drop_foreign_key", "drop_check_constraint"):
            table = _real_table(conn, op["table"])
            if table:
                defs, _suffix = table_defs(conn, table)
                idx, kind = find_named_constraint(defs, op["name"])
                if idx is not None:
                    d = {"name": op["name"]}
                    if otype == "drop_foreign_key":
                        d.update(_parse_fk_clause(defs[idx]))
                    else:
                        d["expression"] = _check_expression_of(defs[idx])
                    d["sql"] = defs[idx]
                    d["position"] = idx
                    info["original_definition"] = d
        elif otype == "drop_index":
            row = conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'index' AND lower(name) = lower(?)",
                (op["name"],),
            ).fetchone()
            if row is not None and row[2] is not None:
                cols = [r[2] for r in conn.execute(
                    f"PRAGMA index_info({quote_ident(row[0])})").fetchall()]
                unique = bool(re.match(r"\s*CREATE\s+UNIQUE\b", row[2], re.IGNORECASE))
                info["original_definition"] = {
                    "name": row[0], "table": row[1], "columns": cols,
                    "unique": unique, "sql": row[2],
                }
    except (sqlite3.Error, MigrationError):
        # The operation itself will report the problem when applied.
        pass
    return info


def _capture_drop_column(conn, op, info):
    table = _real_table(conn, op["table"])
    if not table:
        return
    column = op["column"]
    rows = table_info(conn, table)
    col = next((r for r in rows if r[1] == column), None)
    if col is None:
        return
    defs, suffix = table_defs(conn, table)
    position = None
    dropped_constraints = []
    for i, d in enumerate(defs):
        if is_table_constraint(d):
            if mentions_identifier(_local_part(d), column):
                dropped_constraints.append([i, d])
        elif position is None and leading_name(d)[0].lower() == column.lower():
            position = i
    col_sql = defs[position] if position is not None else None
    info["original_definition"] = {
        "name": col[1],
        "type": col[2] or "",
        "not_null": bool(col[3]),
        "default": col[4],
        "primary_key": bool(col[5]),
        "unique": bool(col_sql and re.search(
            r"\bUNIQUE\b", _strip_literals(col_sql), re.IGNORECASE)),
    }
    if col_sql is not None:
        info["original_sql"] = col_sql
        info["position"] = position
    if dropped_constraints:
        info["dropped_constraints"] = dropped_constraints
    objs = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, rowid",
        (table,),
    ).fetchall()
    dropped_objects = [r[0] for r in objs if mentions_identifier(r[0], column)]
    if dropped_objects:
        info["dropped_objects"] = dropped_objects
    # Preserve the column's data, keyed by primary key (or rowid).
    pk = sorted((r for r in rows if r[5]), key=lambda r: r[5])
    if pk:
        keys = [r[1] for r in pk]
        key_sql = [quote_ident(k) for k in keys]
    elif not _table_suffix_without_rowid(suffix):
        keys = ["rowid"]
        key_sql = ["rowid"]
    else:
        return
    data = conn.execute(
        f"SELECT {', '.join(key_sql)}, {quote_ident(column)} FROM {quote_ident(table)}"
    ).fetchall()
    info["original_data"] = {
        "key_columns": keys,
        "rows": [[_encode_value(v) for v in r] for r in data],
    }


def _placeholder_sql(declared):
    cls = _type_class(declared)
    if cls in ("INTEGER", "REAL", "NUMERIC"):
        return "0"
    if cls == "BLOB":
        return "X''"
    return "''"


def _column_def_sql(d):
    parts = [quote_ident(d["name"])]
    if d.get("type"):
        parts.append(str(d["type"]))
    if d.get("primary_key"):
        parts.append("PRIMARY KEY")
    if d.get("not_null"):
        parts.append("NOT NULL")
    if d.get("unique"):
        parts.append("UNIQUE")
    if d.get("default") is not None:
        parts.append("DEFAULT " + default_sql(d["default"]))
    return " ".join(parts)


def _require_real_table(conn, table):
    real = _real_table(conn, table)
    if real is None:
        raise rollback_failed(f"table '{table}' does not exist")
    return real


def _insert_definitions(defs, items):
    """Insert (position, sql, is_column) items into defs at their positions."""
    defs = list(defs)
    for pos, sql, is_column in sorted(items, key=lambda x: x[0]):
        limit = len(defs)
        if is_column:
            limit = next(
                (i for i, d in enumerate(defs) if is_table_constraint(d)), len(defs)
            )
        pos = limit if pos is None else max(0, min(pos, limit))
        defs.insert(pos, sql)
    return defs


def rb_create_table(conn, op):
    table = _require_real_table(conn, op["table"])
    if child_tables(conn, table):
        raise RollbackError(FK_VIOLATION_MSG)
    conn.execute(f"DROP TABLE {quote_ident(table)}")


def rb_add_column(conn, op):
    table = _require_real_table(conn, op["table"])
    column = op["column"]["name"]
    match = next(
        (r[1] for r in table_info(conn, table) if r[1].lower() == column.lower()), None
    )
    if match is None:
        raise rollback_failed(
            f"column '{column}' does not exist in table '{table}'"
        )
    for child in child_tables(conn, table):
        for _fid, parent, _frm, to in foreign_key_list(conn, child):
            if parent.lower() == table.lower() and any(
                (c or "").lower() == match.lower() for c in to
            ):
                raise RollbackError(FK_VIOLATION_MSG)
    for idx in conn.execute(f"PRAGMA index_list({quote_ident(table)})").fetchall():
        cols = [r[2] for r in conn.execute(
            f"PRAGMA index_info({quote_ident(idx[1])})").fetchall()]
        if any((c or "").lower() == match.lower() for c in cols):
            raise rollback_failed(
                f"cannot drop column '{match}': index '{idx[1]}' depends on it"
            )
    apply_drop_column(conn, {"type": "drop_column", "table": table, "column": match})


def rb_drop_column(conn, op):
    table = _require_real_table(conn, op["table"])
    column = op["column"]
    rows = table_info(conn, table)
    if any(r[1].lower() == column.lower() for r in rows):
        raise rollback_failed(f"column '{column}' already exists in table '{table}'")
    orig = op.get("original_definition") or {"name": column, "type": "TEXT"}
    col_sql = op.get("original_sql") or _column_def_sql(orig)

    defs, suffix = table_defs(conn, table)
    items = [(op.get("position"), col_sql, True)]
    for pos, sql in op.get("dropped_constraints") or []:
        items.append((pos, sql, False))
    new_defs = _insert_definitions(defs, items)

    # Value for the restored column: preserved data, else default/placeholder.
    if orig.get("default") is not None:
        fallback = f"({orig['default']})"
    elif orig.get("not_null"):
        fallback = _placeholder_sql(orig.get("type"))
    else:
        fallback = "NULL"
    data = op.get("original_data")
    existing = [r[1] for r in rows]
    from_sql = None
    expr = fallback
    conn.execute("DROP TABLE IF EXISTS temp._rollback_data")
    if data and data.get("rows") and data.get("key_columns"):
        keys = data["key_columns"]
        lower_existing = {c.lower(): c for c in existing}
        key_refs = []
        for k in keys:
            if k == "rowid":
                key_refs.append("_src.rowid")
            elif k.lower() in lower_existing:
                key_refs.append(f"_src.{quote_ident(lower_existing[k.lower()])}")
            else:
                key_refs = None
                break
        if key_refs:
            kcols = [f"k{i}" for i in range(len(keys))]
            conn.execute(
                f"CREATE TEMP TABLE _rollback_data ({', '.join(kcols)}, v)"
            )
            conn.executemany(
                f"INSERT INTO temp._rollback_data VALUES "
                f"({', '.join('?' for _ in range(len(keys) + 1))})",
                [[_decode_value(v) for v in r] for r in data["rows"]
                 if isinstance(r, list) and len(r) == len(keys) + 1],
            )
            conn.execute(
                f"CREATE INDEX temp._rollback_data_idx ON _rollback_data "
                f"({', '.join(kcols)})"
            )
            cond = " AND ".join(f"{ref} IS _r.{k}" for ref, k in zip(key_refs, kcols))
            from_sql = (
                f"{quote_ident(table)} AS _src LEFT JOIN temp._rollback_data AS _r "
                f"ON {cond}"
            )
            expr = f"CASE WHEN _r.rowid IS NOT NULL THEN _r.v ELSE {fallback} END"
    try:
        rebuild_table(conn, table, new_defs, suffix, existing,
                      extra_cols=[(orig.get("name") or column, expr)],
                      from_sql=from_sql)
    finally:
        conn.execute("DROP TABLE IF EXISTS temp._rollback_data")
    for sql in op.get("dropped_objects") or []:
        conn.execute(sql)
    check_foreign_keys(conn, [table])


def rb_migrate_column_data(conn, op):
    _require_real_table(conn, op["table"])
    apply_migrate_column_data(conn, {
        "type": "migrate_column_data", "table": op["table"],
        "from_column": op["to_column"], "to_column": op["from_column"],
    })


def rb_add_foreign_key(conn, op):
    _require_real_table(conn, op["table"])
    apply_drop_foreign_key(conn, op)


def rb_drop_foreign_key(conn, op):
    table = _require_real_table(conn, op["table"])
    orig = op.get("original_definition")
    if not orig or not (orig.get("sql") or (orig.get("columns") and orig.get("references"))):
        raise rollback_failed(
            f"original definition of foreign key '{op['name']}' is not available"
        )
    defs, suffix = table_defs(conn, table)
    idx, _kind = find_named_constraint(defs, op["name"])
    if idx is not None:
        raise rollback_failed(f"constraint '{op['name']}' already exists on table '{table}'")
    if not orig.get("sql"):
        new_op = dict(orig, type="add_foreign_key", table=table, name=op["name"])
        apply_add_foreign_key(conn, new_op)
        return
    ref = (orig.get("references") or {}).get("table")
    if ref and ref.lower() != table.lower() and _real_table(conn, ref) is None:
        raise rollback_failed(
            f"cannot restore foreign key '{op['name']}': referenced table '{ref}' "
            f"does not exist"
        )
    new_defs = _insert_definitions(defs, [(orig.get("position"), orig["sql"], False)])
    cols = [r[1] for r in table_info(conn, table)]
    rebuild_table(conn, table, new_defs, suffix, cols)
    check_foreign_keys(conn, [table])


def rb_create_index(conn, op):
    apply_drop_index(conn, op)


def rb_drop_index(conn, op):
    orig = op.get("original_definition")
    if not orig or not (orig.get("sql") or (orig.get("table") and orig.get("columns"))):
        raise rollback_failed(
            f"original definition of index '{op['name']}' is not available"
        )
    _require_real_table(conn, orig["table"])
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE lower(name) = lower(?)", (op["name"],)
    ).fetchone()
    if existing:
        raise rollback_failed(f"index '{op['name']}' already exists")
    if orig.get("sql"):
        try:
            conn.execute(orig["sql"])
        except sqlite3.IntegrityError as e:
            raise rollback_failed(
                f"cannot re-create unique index '{op['name']}': duplicate values ({e})"
            )
    else:
        apply_create_index(conn, {
            "type": "create_index", "name": op["name"], "table": orig["table"],
            "columns": orig["columns"], "unique": bool(orig.get("unique")),
        })


def rb_add_check_constraint(conn, op):
    _require_real_table(conn, op["table"])
    apply_drop_check_constraint(conn, op)


def rb_drop_check_constraint(conn, op):
    table = _require_real_table(conn, op["table"])
    orig = op.get("original_definition")
    if not orig or not (orig.get("sql") or orig.get("expression")):
        raise rollback_failed(
            f"original definition of check constraint '{op['name']}' is not available"
        )
    expr = orig.get("expression")
    if not orig.get("sql"):
        apply_add_check_constraint(conn, {
            "type": "add_check_constraint", "table": table,
            "name": op["name"], "expression": expr,
        })
        return
    defs, suffix = table_defs(conn, table)
    idx, _kind = find_named_constraint(defs, op["name"])
    if idx is not None:
        raise rollback_failed(f"constraint '{op['name']}' already exists on table '{table}'")
    if expr:
        bad = conn.execute(
            f"SELECT COUNT(*) FROM {quote_ident(table)} WHERE NOT ({expr})"
        ).fetchone()[0]
        if bad:
            raise rollback_failed(
                f"check constraint violation: {bad} existing row(s) in table "
                f"'{table}' do not satisfy constraint '{op['name']}' ({expr})"
            )
    new_defs = _insert_definitions(defs, [(orig.get("position"), orig["sql"], False)])
    cols = [r[1] for r in table_info(conn, table)]
    rebuild_table(conn, table, new_defs, suffix, cols)


def rb_irreversible(conn, op):
    raise RollbackError(
        f"cannot rollback: missing rollback_operations for {op['type']}"
    )


ROLLBACKS = {
    "create_table": rb_create_table,
    "add_column": rb_add_column,
    "drop_column": rb_drop_column,
    "transform_data": rb_irreversible,
    "migrate_column_data": rb_migrate_column_data,
    "backfill_data": rb_irreversible,
    "add_foreign_key": rb_add_foreign_key,
    "drop_foreign_key": rb_drop_foreign_key,
    "create_index": rb_create_index,
    "drop_index": rb_drop_index,
    "add_check_constraint": rb_add_check_constraint,
    "drop_check_constraint": rb_drop_check_constraint,
}


# --------------------------------------------------------------------------
# Migration driver
# --------------------------------------------------------------------------

def load_migration(path):
    if not os.path.isfile(path):
        raise MigrationError(f"migration file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise MigrationError(f"cannot read migration file: {path}: {e}")
    except UnicodeDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {path}: {e}")
    try:
        mig = json.loads(text)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {path}: {e}")
    validate_migration(mig)
    return mig


MIGRATIONS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS _migrations ("
    "version INTEGER PRIMARY KEY, "
    "description TEXT NOT NULL, "
    "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
    "operations TEXT NOT NULL, "
    "rollback_operations TEXT)"
)


def _migrations_columns(conn):
    return [r[1] for r in table_info(conn, "_migrations")]


def ensure_migrations_table(conn):
    """Create the _migrations table, upgrading tables from older versions."""
    conn.execute(MIGRATIONS_TABLE_SQL)
    cols = _migrations_columns(conn)
    if "operations" not in cols:
        # Migrations recorded before rollback support have no stored operations;
        # the empty string marks them as not automatically reversible.
        conn.execute(
            "ALTER TABLE _migrations ADD COLUMN operations TEXT NOT NULL DEFAULT ''"
        )
    if "rollback_operations" not in cols:
        conn.execute("ALTER TABLE _migrations ADD COLUMN rollback_operations TEXT")


def operation_event(event_name, op, version, extra=None):
    otype = op["type"]
    event = {"event": event_name, "type": otype}
    if otype in ("create_index", "drop_index"):
        event["name"] = op["name"]
        if "table" in op:
            event["table"] = op["table"]
    else:
        event["table"] = op["table"]
    if otype in ("add_foreign_key", "drop_foreign_key",
                 "add_check_constraint", "drop_check_constraint"):
        event["name"] = op["name"]
    if otype == "add_column":
        event["column"] = op["column"]["name"]
    elif otype == "drop_column":
        event["column"] = op["column"]
    if extra:
        event.update(extra)
    event["version"] = version
    return event


def migrate(migration_path, db_path):
    mig = load_migration(migration_path)
    version = mig["version"]

    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    try:
        try:
            # Foreign keys are enforced by the tool itself during a migration:
            # SQLite cannot toggle the foreign_keys pragma inside a transaction,
            # and table recreation (drop_column, drop_foreign_key, check
            # constraints...) must not trigger ON DELETE actions on child tables
            # when the old table is dropped. After every operation that can
            # affect referential integrity, PRAGMA foreign_key_check is run and
            # violations abort (and roll back) the migration.
            conn.execute("PRAGMA foreign_keys = OFF")
            ensure_migrations_table(conn)
            row = conn.execute(
                "SELECT 1 FROM _migrations WHERE version = ?", (version,)
            ).fetchone()
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error: {e}")

        if row is not None:
            sys.stderr.write(
                f"Warning: Migration version {version} already applied, skipping\n"
            )
            emit({"event": "migration_skipped", "version": version,
                  "reason": "already_applied"})
            return

        try:
            conn.execute("BEGIN")
            stored = []
            for op in mig["operations"]:
                stored.append(capture_rollback_info(conn, op))
                extra = OPERATIONS[op["type"]](conn, op)
                emit(operation_event("operation_applied", op, version, extra))
            rops = mig.get("rollback_operations")
            conn.execute(
                "INSERT INTO _migrations "
                "(version, description, operations, rollback_operations) "
                "VALUES (?, ?, ?, ?)",
                (version, mig["description"], json.dumps(stored),
                 None if rops is None else json.dumps(rops)),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            _rollback(conn)
            raise MigrationError(f"SQL error: {e}")
        except BaseException:
            _rollback(conn)
            raise

        emit({"event": "migration_complete", "version": version,
              "operations_count": len(mig["operations"])})
    finally:
        conn.close()


def _rollback(conn):
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# Rollback driver
# --------------------------------------------------------------------------

def _load_json_field(text, version, what):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, list) or not all(
        isinstance(op, dict) and op.get("type") in OPERATION_TYPES for op in value
    ):
        raise MigrationError(
            f"cannot rollback version {version}: no valid {what} stored"
        )
    return value


def rollback(db_path, to_version=None, count=None):
    if not os.path.isfile(db_path):
        raise MigrationError("no migrations to rollback")
    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    try:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            if not table_exists(conn, "_migrations"):
                raise MigrationError("no migrations to rollback")
            cols = _migrations_columns(conn)
            ops_sql = "operations" if "operations" in cols else "NULL"
            rops_sql = (
                "rollback_operations" if "rollback_operations" in cols else "NULL"
            )
            rows = conn.execute(
                f"SELECT version, description, {ops_sql}, {rops_sql} "
                "FROM _migrations ORDER BY version DESC"
            ).fetchall()
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error: {e}")

        if not rows:
            raise MigrationError("no migrations to rollback")

        if to_version is not None:
            if to_version != 0 and not any(r[0] == to_version for r in rows):
                raise MigrationError(f"version {to_version} not found")
            targets = [r for r in rows if r[0] > to_version]
        else:
            targets = rows[:count if count is not None else 1]

        # Validate everything before touching the database.
        plan = []
        for version, description, ops_text, rops_text in targets:
            explicit = None
            if rops_text is not None:
                explicit = _load_json_field(rops_text, version, "rollback_operations")
            if explicit is None:
                ops = _load_json_field(ops_text, version, "operation metadata")
                for op in reversed(ops):
                    if op["type"] in IRREVERSIBLE_OPERATIONS:
                        raise MigrationError(
                            f"cannot rollback version {version}: missing "
                            f"rollback_operations for {op['type']}"
                        )
            else:
                ops = None
            plan.append((version, description, ops, explicit))

        rolled_back = []
        try:
            conn.execute("BEGIN")
            for version, description, ops, explicit in plan:
                emit({"event": "rollback_started", "version": version,
                      "description": description})
                if explicit is not None:
                    for op in explicit:
                        _guard(lambda op=op: OPERATIONS[op["type"]](conn, op))
                        emit(rollback_event(op, version, explicit=True))
                else:
                    for op in reversed(ops):
                        _guard(lambda op=op: ROLLBACKS[op["type"]](conn, op))
                        emit(rollback_event(op, version))
                conn.execute("DELETE FROM _migrations WHERE version = ?", (version,))
                emit({"event": "rollback_complete", "version": version})
                rolled_back.append(version)
            _guard(lambda: check_foreign_keys(conn, all_tables(conn)))
            final = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM _migrations"
            ).fetchone()[0]
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

        emit({"event": "rollback_finished", "versions_rolled_back": rolled_back,
              "final_version": final})
    finally:
        conn.close()


USAGE = (
    "usage: migration_tool.py migrate <migration.json> <database.db>\n"
    "       migration_tool.py rollback <database.db> "
    "[--to-version <version>] [--count <n>]"
)


def _parse_int(value, option):
    try:
        if not re.match(r"^\s*[+-]?\d+\s*$", value):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        raise ArgumentError(f"invalid value for {option}: {value!r}")


class ArgumentError(Exception):
    pass


def parse_rollback_args(args):
    db_path = None
    to_version = None
    count = None
    i = 0
    while i < len(args):
        a = args[i]
        opt, eq, val = a.partition("=")
        if opt in ("--to-version", "--count"):
            if not eq:
                if i + 1 >= len(args):
                    raise ArgumentError(f"missing value for {opt}")
                i += 1
                val = args[i]
            n = _parse_int(val, opt)
            if opt == "--to-version":
                if n < 0:
                    raise ArgumentError(f"invalid value for {opt}: {val!r}")
                to_version = n
            else:
                if n < 1:
                    raise ArgumentError(f"invalid value for {opt}: {val!r}")
                count = n
        elif a.startswith("--"):
            raise ArgumentError(f"unknown option {a}")
        elif db_path is None:
            db_path = a
        else:
            raise ArgumentError("too many arguments")
        i += 1
    if db_path is None:
        raise ArgumentError("missing database path")
    if to_version is not None and count is not None:
        raise ArgumentError("--to-version and --count cannot be used together")
    return db_path, to_version, count


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 3 and args[0] == "migrate":
            migrate(args[1], args[2])
        elif args and args[0] == "rollback":
            try:
                db_path, to_version, count = parse_rollback_args(args[1:])
            except ArgumentError as e:
                sys.stderr.write(f"Error: invalid arguments: {e}\n{USAGE}\n")
                return 1
            rollback(db_path, to_version, count)
        else:
            sys.stderr.write(f"Error: invalid arguments\n{USAGE}\n")
            return 1
    except MigrationError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
