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
)
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


def validate_operation(op, index):
    ctx = f"operation {index}"
    if not isinstance(op, dict):
        raise schema_error(f"{ctx}: operation must be an object")
    if "type" not in op:
        raise schema_error(f"{ctx}: missing required field 'type'")
    otype = op["type"]
    if not isinstance(otype, str) or otype not in OPERATION_TYPES:
        raise schema_error(f"{ctx}: unknown operation type {json.dumps(otype)}")
    ctx = f"operation {index} ({otype})"
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

    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()[0]
    _prefix, defs, suffix = split_create_table(create_sql)

    kept_defs = []
    for d in defs:
        name, quoted = leading_name(d)
        if not quoted and name.upper() in TABLE_CONSTRAINT_KEYWORDS:
            # Table-level constraint: keep only if it doesn't involve the column.
            if not mentions_identifier(d, column):
                kept_defs.append(d)
            continue
        if name.lower() == column.lower():
            continue
        kept_defs.append(d)

    keep_cols = [n for n in names if n != column]

    # Collect indexes and triggers to recreate (dropped along with the table).
    extras = conn.execute(
        "SELECT type, sql FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()

    tmp = f"_{table}_new"
    k = 0
    while conn.execute(
        "SELECT 1 FROM sqlite_master WHERE lower(name) = lower(?)", (tmp,)
    ).fetchone():
        k += 1
        tmp = f"_{table}_new{k}"

    col_list = ", ".join(quote_ident(c) for c in keep_cols)
    conn.execute(f"CREATE TABLE {quote_ident(tmp)} ({', '.join(kept_defs)}){suffix}")
    conn.execute(
        f"INSERT INTO {quote_ident(tmp)} ({col_list}) "
        f"SELECT {col_list} FROM {quote_ident(table)}"
    )
    conn.execute(f"DROP TABLE {quote_ident(table)}")
    conn.execute(f"ALTER TABLE {quote_ident(tmp)} RENAME TO {quote_ident(table)}")

    for _type, sql in extras:
        if mentions_identifier(sql, column):
            continue
        conn.execute(sql)


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
        raise MigrationError(
            f"data type mismatch: cannot write to column '{column}' "
            f"of table '{table}': {e}"
        )
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")
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


def migrate(migration_path, db_path):
    mig = load_migration(migration_path)
    version = mig["version"]

    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    try:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _migrations ("
                "version INTEGER PRIMARY KEY, "
                "description TEXT NOT NULL, "
                "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
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
            for op in mig["operations"]:
                extra = OPERATIONS[op["type"]](conn, op)
                event = {"event": "operation_applied", "type": op["type"],
                         "table": op["table"]}
                if op["type"] == "add_column":
                    event["column"] = op["column"]["name"]
                elif op["type"] == "drop_column":
                    event["column"] = op["column"]
                if extra:
                    event.update(extra)
                event["version"] = version
                emit(event)
            conn.execute(
                "INSERT INTO _migrations (version, description) VALUES (?, ?)",
                (version, mig["description"]),
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


USAGE = "usage: migration_tool.py migrate <migration.json> <database.db>"


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "migrate":
        sys.stderr.write(f"Error: invalid arguments\n{USAGE}\n")
        return 1
    try:
        migrate(args[1], args[2])
    except MigrationError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
