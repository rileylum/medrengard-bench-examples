#!/usr/bin/env python3
"""Run read-only SQL queries over a directory of data files.

Supported formats: CSV, TSV, JSON, JSONL and Parquet, each optionally
compressed with gzip (.gz) or bzip2 (.bz2). Files sharing a table name
(e.g. users.csv and users.parquet) are combined and must have identical columns.

Usage:
    python query_files.py --data <dir> --sql <query or path to .sql file> [--out <file.csv>]
"""
import argparse
import csv
import os
import re
import sys
from decimal import Decimal, InvalidOperation


class QueryError(Exception):
    pass


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)\s*$")


def to_num(v):
    """Return a Decimal/int for numeric values, else None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, Decimal)):
        return v
    if isinstance(v, str) and _NUM_RE.match(v):
        try:
            return Decimal(v.strip())
        except InvalidOperation:
            return None
    return None


def sort_key(v):
    """Total ordering key: NULL < numbers < strings."""
    if v is None:
        return (0, 0, "")
    n = to_num(v)
    if n is not None:
        return (1, Decimal(n), "")
    return (2, 0, str(v))


def eq_key(v):
    if v is None:
        return None
    n = to_num(v)
    if n is not None:
        return ("n", Decimal(n).normalize() if Decimal(n) != 0 else Decimal(0))
    return ("s", str(v))


def group_key(v):
    if v is None:
        return ("null",)
    return eq_key(v)


def compare(a, b):
    """Return -1/0/1 or None if either is NULL."""
    if a is None or b is None:
        return None
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None:
        na, nb = Decimal(na), Decimal(nb)
    else:
        na, nb = to_str(a), to_str(b)
    return (na > nb) - (na < nb)


def to_str(v):
    if v is None:
        return "NULL"
    if isinstance(v, Decimal):
        s = format(v, "f")
        return s
    return str(v)


def like_to_regex(pattern):
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.DOTALL)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

KEYWORDS = {
    "SELECT", "DISTINCT", "ALL", "FROM", "AS", "INNER", "LEFT", "RIGHT", "FULL",
    "OUTER", "JOIN", "ON", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "ASC",
    "DESC", "LIMIT", "OFFSET", "AND", "OR", "NOT", "LIKE", "IS", "NULL", "IN",
    "CROSS", "NULLS", "FIRST", "LAST", "TRUE", "FALSE",
}


class Tok:
    __slots__ = ("kind", "value", "start", "end")

    def __init__(self, kind, value, start, end):
        self.kind = kind  # KW, IDENT, QIDENT, NUM, STR, OP, EOF
        self.value = value
        self.start = start
        self.end = end

    def __repr__(self):
        return "Tok(%s,%r)" % (self.kind, self.value)


def tokenize(sql):
    toks = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise QueryError("Unterminated comment")
            i = j + 2
            continue
        start = i
        if c == "'":
            buf = []
            i += 1
            while True:
                if i >= n:
                    raise QueryError("Unterminated string literal")
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            toks.append(Tok("STR", "".join(buf), start, i))
            continue
        if c in ('"', "`", "["):
            close = {'"': '"', "`": "`", "[": "]"}[c]
            buf = []
            i += 1
            while True:
                if i >= n:
                    raise QueryError("Unterminated quoted identifier")
                if sql[i] == close:
                    if close != "]" and i + 1 < n and sql[i + 1] == close:
                        buf.append(close)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            toks.append(Tok("QIDENT", "".join(buf), start, i))
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and sql[j].isdigit():
                j += 1
            if j < n and sql[j] == "." and j + 1 < n and sql[j + 1].isdigit():
                j += 1
                while j < n and sql[j].isdigit():
                    j += 1
            elif c == ".":
                j += 1
                while j < n and sql[j].isdigit():
                    j += 1
            if j < n and (sql[j].isalpha() or sql[j] == "_"):
                # identifier that starts with digits (e.g. 2020_sales)
                while j < n and (sql[j].isalnum() or sql[j] == "_"):
                    j += 1
                toks.append(Tok("IDENT", sql[i:j], start, j))
            else:
                toks.append(Tok("NUM", sql[i:j], start, j))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            word = sql[i:j]
            if word.upper() in KEYWORDS:
                toks.append(Tok("KW", word.upper(), start, j))
            else:
                toks.append(Tok("IDENT", word, start, j))
            i = j
            continue
        two = sql[i:i + 2]
        if two in ("!=", "<>", "<=", ">=", "=="):
            toks.append(Tok("OP", "!=" if two == "<>" else ("=" if two == "==" else two), start, i + 2))
            i += 2
            continue
        if c in "=<>(),.*;-+":
            toks.append(Tok("OP", c, start, i + 1))
            i += 1
            continue
        raise QueryError("Unexpected character %r at position %d" % (c, i))
    toks.append(Tok("EOF", None, n, n))
    return toks


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

class Node:
    text = None


class Col(Node):
    def __init__(self, parts):
        self.parts = parts
        self.idx = None

    @property
    def name(self):
        return self.parts[-1]


class Lit(Node):
    def __init__(self, value):
        self.value = value


class Agg(Node):
    def __init__(self, func, arg, distinct=False):
        self.func = func  # COUNT SUM MIN MAX
        self.arg = arg    # None means '*'
        self.distinct = distinct


class Cmp(Node):
    def __init__(self, op, left, right):
        self.op, self.left, self.right = op, left, right


class BoolOp(Node):
    def __init__(self, op, items):
        self.op, self.items = op, items


class Not(Node):
    def __init__(self, item):
        self.item = item


class Like(Node):
    def __init__(self, expr, pattern, negate):
        self.expr, self.pattern, self.negate = expr, pattern, negate
        self._cache = {}


class IsNull(Node):
    def __init__(self, expr, negate):
        self.expr, self.negate = expr, negate


class InList(Node):
    def __init__(self, expr, items, negate):
        self.expr, self.items, self.negate = expr, items, negate


class Star(Node):
    def __init__(self, qualifier=None):
        self.qualifier = qualifier


class SelectItem:
    def __init__(self, expr, alias, text):
        self.expr, self.alias, self.text = expr, alias, text


class TableRef:
    def __init__(self, name, alias):
        self.name, self.alias = name, alias


class Join:
    def __init__(self, kind, table, on):
        self.kind, self.table, self.on = kind, table, on


class OrderItem:
    def __init__(self, expr, desc, nulls):
        self.expr, self.desc, self.nulls = expr, desc, nulls


class Query:
    pass


def children(node):
    if isinstance(node, Cmp):
        return [node.left, node.right]
    if isinstance(node, BoolOp):
        return list(node.items)
    if isinstance(node, Not):
        return [node.item]
    if isinstance(node, Like):
        return [node.expr, node.pattern]
    if isinstance(node, IsNull):
        return [node.expr]
    if isinstance(node, InList):
        return [node.expr] + list(node.items)
    if isinstance(node, Agg):
        return [node.arg] if node.arg is not None else []
    return []


def walk(node):
    yield node
    for c in children(node):
        yield from walk(c)


def contains_agg(node):
    return any(isinstance(n, Agg) for n in walk(node))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

AGG_FUNCS = {"COUNT", "SUM", "MIN", "MAX"}
CLAUSE_END = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
              "INNER", "LEFT", "RIGHT", "FULL", "JOIN", "ON", "CROSS"}


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.toks = tokenize(sql)
        self.pos = 0

    def peek(self, k=0):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.pos]
        if t.kind != "EOF":
            self.pos += 1
        return t

    def is_kw(self, *kws, k=0):
        t = self.peek(k)
        return t.kind == "KW" and t.value in kws

    def is_op(self, *ops, k=0):
        t = self.peek(k)
        return t.kind == "OP" and t.value in ops

    def accept_kw(self, *kws):
        if self.is_kw(*kws):
            return self.next()
        return None

    def accept_op(self, *ops):
        if self.is_op(*ops):
            return self.next()
        return None

    def expect_kw(self, kw):
        t = self.next()
        if t.kind != "KW" or t.value != kw:
            raise QueryError("Expected %s but found %s" % (kw, self.describe(t)))
        return t

    def expect_op(self, op):
        t = self.next()
        if t.kind != "OP" or t.value != op:
            raise QueryError("Expected '%s' but found %s" % (op, self.describe(t)))
        return t

    @staticmethod
    def describe(t):
        if t.kind == "EOF":
            return "end of query"
        return repr(t.value)

    def ident(self):
        t = self.next()
        if t.kind in ("IDENT", "QIDENT"):
            return t.value
        raise QueryError("Expected identifier but found %s" % self.describe(t))

    def is_ident(self, k=0):
        return self.peek(k).kind in ("IDENT", "QIDENT")

    # -- query ---------------------------------------------------------------
    def parse(self):
        q = self.parse_select()
        while self.accept_op(";"):
            pass
        if self.peek().kind != "EOF":
            raise QueryError("Unexpected %s after end of query" % self.describe(self.peek()))
        return q

    def parse_select(self):
        q = Query()
        if self.peek().kind == "EOF":
            raise QueryError("Empty query")
        self.expect_kw("SELECT")
        q.distinct = False
        if self.accept_kw("DISTINCT"):
            q.distinct = True
        else:
            self.accept_kw("ALL")
        q.select = [self.parse_select_item()]
        while self.accept_op(","):
            q.select.append(self.parse_select_item())
        self.expect_kw("FROM")
        q.table = self.parse_table_ref()
        q.joins = []
        while True:
            kind = None
            if self.is_kw("JOIN"):
                kind = "INNER"
            elif self.is_kw("INNER") and self.is_kw("JOIN", k=1):
                kind = "INNER"
                self.next()
            elif self.is_kw("LEFT", "RIGHT", "FULL"):
                kind = self.next().value
                self.accept_kw("OUTER")
                if not self.is_kw("JOIN"):
                    raise QueryError("Expected JOIN after %s" % kind)
            elif self.is_kw("CROSS"):
                self.next()
                kind = "CROSS"
                if not self.is_kw("JOIN"):
                    raise QueryError("Expected JOIN after CROSS")
            else:
                break
            self.expect_kw("JOIN")
            tref = self.parse_table_ref()
            on = None
            if kind != "CROSS":
                self.expect_kw("ON")
                on = self.parse_expr()
                if contains_agg(on):
                    raise QueryError("Aggregate functions are not allowed in JOIN conditions")
            q.joins.append(Join(kind, tref, on))
        q.where = None
        if self.accept_kw("WHERE"):
            q.where = self.parse_expr()
            if contains_agg(q.where):
                raise QueryError("Aggregate functions are not allowed in WHERE")
        q.group_by = []
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            q.group_by.append(self.parse_expr())
            while self.accept_op(","):
                q.group_by.append(self.parse_expr())
            for g in q.group_by:
                if contains_agg(g):
                    raise QueryError("Aggregate functions are not allowed in GROUP BY")
        q.having = None
        if self.accept_kw("HAVING"):
            q.having = self.parse_expr()
        q.order_by = []
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            q.order_by.append(self.parse_order_item())
            while self.accept_op(","):
                q.order_by.append(self.parse_order_item())
        q.limit = None
        q.offset = None
        for _ in range(2):
            if q.limit is None and self.accept_kw("LIMIT"):
                q.limit = self.parse_nonneg_int("LIMIT")
                if self.accept_op(","):
                    # MySQL style LIMIT offset, count
                    q.offset, q.limit = q.limit, self.parse_nonneg_int("LIMIT")
            elif q.offset is None and self.accept_kw("OFFSET"):
                q.offset = self.parse_nonneg_int("OFFSET")
        return q

    def parse_nonneg_int(self, what):
        t = self.next()
        if t.kind != "NUM" or not t.value.isdigit():
            raise QueryError("%s requires a non-negative integer, found %s" % (what, self.describe(t)))
        return int(t.value)

    def parse_select_item(self):
        start_tok = self.peek()
        if self.is_op("*"):
            self.next()
            return SelectItem(Star(), None, "*")
        if self.is_ident() and self.is_op(".", k=1):
            # qualifier.* ?
            k = 0
            parts = []
            while self.is_ident(k) and self.is_op(".", k=k + 1):
                parts.append(self.peek(k).value)
                k += 2
            if self.is_op("*", k=k):
                for _ in range(k + 1):
                    self.next()
                return SelectItem(Star(".".join(parts)), None, ".".join(parts) + ".*")
        expr = self.parse_expr()
        end = self.toks[self.pos - 1].end
        text = self.sql[start_tok.start:end].strip()
        alias = None
        if self.accept_kw("AS"):
            t = self.next()
            if t.kind in ("IDENT", "QIDENT", "STR"):
                alias = t.value
            else:
                raise QueryError("Expected alias after AS but found %s" % self.describe(t))
        elif self.peek().kind in ("IDENT", "QIDENT"):
            alias = self.next().value
        return SelectItem(expr, alias, text)

    def parse_table_ref(self):
        parts = [self.ident()]
        while self.is_op(".") and self.is_ident(1):
            self.next()
            parts.append(self.ident())
        alias = None
        if self.accept_kw("AS"):
            alias = self.ident()
        elif self.is_ident():
            alias = self.ident()
        return TableRef(parts, alias)

    def parse_order_item(self):
        expr = self.parse_expr()
        desc = False
        if self.accept_kw("DESC"):
            desc = True
        else:
            self.accept_kw("ASC")
        nulls = None
        if self.accept_kw("NULLS"):
            t = self.next()
            if t.kind == "KW" and t.value in ("FIRST", "LAST"):
                nulls = t.value
            else:
                raise QueryError("Expected FIRST or LAST after NULLS")
        return OrderItem(expr, desc, nulls)

    # -- expressions -----------------------------------------------------------
    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        items = [self.parse_and()]
        while self.accept_kw("OR"):
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else BoolOp("OR", items)

    def parse_and(self):
        items = [self.parse_not()]
        while self.accept_kw("AND"):
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else BoolOp("AND", items)

    def parse_not(self):
        if self.accept_kw("NOT"):
            return Not(self.parse_not())
        return self.parse_predicate()

    def parse_predicate(self):
        left = self.parse_primary()
        while True:
            if self.is_op("=", "!=", "<", "<=", ">", ">="):
                op = self.next().value
                right = self.parse_primary()
                left = Cmp(op, left, right)
                continue
            negate = False
            if self.is_kw("NOT") and self.is_kw("LIKE", "IN", k=1):
                self.next()
                negate = True
            if self.accept_kw("LIKE"):
                left = Like(left, self.parse_primary(), negate)
                continue
            if self.accept_kw("IN"):
                self.expect_op("(")
                items = [self.parse_primary()]
                while self.accept_op(","):
                    items.append(self.parse_primary())
                self.expect_op(")")
                left = InList(left, items, negate)
                continue
            if self.accept_kw("IS"):
                neg = bool(self.accept_kw("NOT"))
                self.expect_kw("NULL")
                left = IsNull(left, neg)
                continue
            return left

    def parse_primary(self):
        t = self.peek()
        if t.kind == "OP" and t.value == "(":
            self.next()
            e = self.parse_expr()
            self.expect_op(")")
            return e
        if t.kind == "STR":
            self.next()
            return Lit(t.value)
        if t.kind == "NUM":
            self.next()
            return Lit(self.num_lit(t.value))
        if t.kind == "OP" and t.value in ("-", "+") and self.peek(1).kind == "NUM":
            self.next()
            v = self.num_lit(self.next().value)
            return Lit(-v if t.value == "-" else v)
        if t.kind == "KW" and t.value == "NULL":
            self.next()
            return Lit(None)
        if t.kind == "KW" and t.value in ("TRUE", "FALSE"):
            self.next()
            return Lit(1 if t.value == "TRUE" else 0)
        if t.kind in ("IDENT", "QIDENT"):
            if t.kind == "IDENT" and self.is_op("(", k=1):
                return self.parse_func()
            parts = [self.next().value]
            while self.is_op(".") and self.is_ident(1):
                self.next()
                parts.append(self.next().value)
            return Col(parts)
        raise QueryError("Unexpected %s in expression" % self.describe(t))

    @staticmethod
    def num_lit(s):
        if "." in s:
            return Decimal(s)
        return int(s)

    def parse_func(self):
        name_tok = self.next()
        fname = name_tok.value.upper()
        if fname not in AGG_FUNCS:
            raise QueryError("Unsupported function: %s" % name_tok.value)
        self.expect_op("(")
        distinct = bool(self.accept_kw("DISTINCT"))
        if self.is_op("*"):
            if fname != "COUNT" or distinct:
                raise QueryError("%s(*) is not supported" % fname)
            self.next()
            arg = None
        else:
            arg = self.parse_expr()
            if contains_agg(arg):
                raise QueryError("Nested aggregate functions are not allowed")
            if not isinstance(arg, Col):
                raise QueryError("%s() only accepts a column reference" % fname)
        self.expect_op(")")
        return Agg(fname, arg, distinct)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

FORMATS = {".csv": "csv", ".tsv": "tsv", ".json": "json", ".jsonl": "jsonl",
           ".ndjson": "jsonl", ".parquet": "parquet", ".pq": "parquet"}
COMPRESSIONS = {".gz": "gz", ".gzip": "gz", ".bz2": "bz2"}


def split_data_file(fn):
    """Return (base, fmt, compression) or None if not a supported data file."""
    low = fn.lower()
    comp = None
    for ext, c in COMPRESSIONS.items():
        if low.endswith(ext):
            comp = c
            fn, low = fn[:-len(ext)], low[:-len(ext)]
            break
    for ext, f in FORMATS.items():
        if low.endswith(ext) and len(fn) > len(ext):
            return fn[:-len(ext)], f, comp
    return None


def discover_tables(data_dir):
    """Map table name -> list of (path, fmt, compression), in a stable order."""
    tables = {}
    for root, dirs, files in os.walk(data_dir):
        dirs.sort()
        for fn in sorted(files):
            info = split_data_file(fn)
            if info is None:
                continue
            base, fmt, comp = info
            path = os.path.join(root, fn)
            reldir = os.path.relpath(root, data_dir)
            comps = [] if reldir in (".", "") else reldir.split(os.sep)
            comps.append(base)
            name = ".".join(c.replace(".", "_") for c in comps)
            tables.setdefault(name, []).append((path, fmt, comp))
    return tables


def open_raw(path, comp):
    if comp == "gz":
        import gzip
        return gzip.open(path, "rb")
    if comp == "bz2":
        import bz2
        return bz2.open(path, "rb")
    return open(path, "rb")


def read_bytes(path, comp, name):
    try:
        with open_raw(path, comp) as f:
            return f.read()
    except (OSError, EOFError, ValueError) as e:
        raise QueryError("Cannot read file '%s' for table '%s': %s"
                         % (os.path.basename(path), name, e))


def decode_text(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def load_delimited(text, name, delim, kind):
    import io
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    header = None
    for row in reader:
        if row and any(c.strip() for c in row):
            header = row
            break
    if header is None:
        # empty file: no schema, no rows
        return None, []
    header = [h.strip() for h in header]
    if any(h == "" for h in header):
        raise QueryError("Invalid %s file for table '%s': empty column name in header" % (kind, name))
    ncol = len(header)
    rows = []
    for row in reader:
        if not row:
            continue
        row = (row + [""] * ncol)[:ncol]
        rows.append([None if v == "" else v for v in row])
    return header, rows


def norm_value(v):
    """Convert a value from a typed source (JSON/Parquet) into engine values."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v:
            return None
        if v in (float("inf"), float("-inf")):
            return str(v)
        return Decimal(repr(v))
    if isinstance(v, Decimal):
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.hex()
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if _is_datetime(v) else v.isoformat()
    if isinstance(v, (list, tuple, dict)):
        import json
        return json.dumps(v, default=str, ensure_ascii=False)
    return str(v)


def _is_datetime(v):
    import datetime
    return isinstance(v, datetime.datetime)


def records_to_table(records, name, kind):
    header, seen = [], set()
    for r in records:
        if not isinstance(r, dict):
            raise QueryError("Invalid %s file for table '%s': expected objects" % (kind, name))
        for k in r:
            if k not in seen:
                seen.add(k)
                header.append(k)
    rows = [[norm_value(r.get(c)) for c in header] for r in records]
    return header, rows


def load_json(text, name):
    import json
    if not text.strip():
        return None, []
    try:
        obj = json.loads(text, parse_float=Decimal)
    except ValueError as e:
        # maybe it is actually JSON lines
        try:
            return load_jsonl(text, name)
        except QueryError:
            raise QueryError("Invalid JSON file for table '%s': %s" % (name, e))
    kind = "JSON"
    if isinstance(obj, list):
        if all(isinstance(r, list) for r in obj) and obj:
            # array of arrays: first row is header
            header = [str(h) for h in obj[0]]
            n = len(header)
            rows = [[norm_value(v) for v in (r + [None] * n)[:n]] for r in obj[1:]]
            return header, rows
        return records_to_table(obj, name, kind)
    if isinstance(obj, dict):
        # split orientation: {"columns": [...], "data": [[...], ...]}
        if isinstance(obj.get("columns"), list) and isinstance(obj.get("data"), list) \
                and all(isinstance(r, list) for r in obj["data"]):
            header = [str(h) for h in obj["columns"]]
            n = len(header)
            rows = [[norm_value(v) for v in (r + [None] * n)[:n]] for r in obj["data"]]
            return header, rows
        # wrapper: {"data": [ {...}, ... ]} / {"records": [...]} with a single key
        if len(obj) == 1:
            (v,) = obj.values()
            if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
                return records_to_table(v, name, kind)
        vals = list(obj.values())
        if vals and all(isinstance(v, list) for v in vals):
            # column orientation: {"col": [v1, v2, ...]}
            header = list(obj.keys())
            n = max(len(v) for v in vals)
            cols = [list(v) + [None] * (n - len(v)) for v in vals]
            rows = [[norm_value(c[i]) for c in cols] for i in range(n)]
            return header, rows
        if vals and all(isinstance(v, dict) for v in vals):
            # pandas "columns" orientation: {"col": {"0": v, "1": v}}
            header = list(obj.keys())
            idx, seen = [], set()
            for v in vals:
                for k in v:
                    if k not in seen:
                        seen.add(k)
                        idx.append(k)
            rows = [[norm_value(obj[c].get(i)) for c in header] for i in idx]
            return header, rows
        # single record
        return records_to_table([obj], name, kind)
    raise QueryError("Invalid JSON file for table '%s': unsupported structure" % name)


def load_jsonl(text, name):
    import json
    records = []
    for ln, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        try:
            records.append(json.loads(s, parse_float=Decimal))
        except ValueError as e:
            raise QueryError("Invalid JSONL file for table '%s' (line %d): %s" % (name, ln, e))
    if not records:
        return None, []
    return records_to_table(records, name, "JSONL")


def load_parquet(data, name):
    import io
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise QueryError("Reading Parquet files requires the 'pyarrow' package")
    try:
        table = pq.read_table(io.BytesIO(data), use_threads=False)
    except Exception as e:  # pyarrow raises various exception types
        raise QueryError("Invalid Parquet file for table '%s': %s" % (name, e))
    header = [str(c) for c in table.column_names]
    cols = [table.column(i).to_pylist() for i in range(table.num_columns)]
    rows = [[norm_value(c[i]) for c in cols] for i in range(table.num_rows)]
    return header, rows


def load_file(path, fmt, comp, name):
    data = read_bytes(path, comp, name)
    if fmt == "parquet":
        return load_parquet(data, name)
    text = decode_text(data)
    if fmt == "csv":
        return load_delimited(text, name, ",", "CSV")
    if fmt == "tsv":
        return load_delimited(text, name, "\t", "TSV")
    if fmt == "json":
        return load_json(text, name)
    if fmt == "jsonl":
        return load_jsonl(text, name)
    raise QueryError("Unsupported file format for table '%s'" % name)


def load_table(files, name):
    """Load and concatenate the given files. Empty files (no header/records)
    contribute nothing; all other files must share the same set of columns.
    Columns are ordered as in the first non-empty file."""
    header, rows = None, []
    first = None
    for path, fmt, comp in files:
        h, r = load_file(path, fmt, comp, name)
        if h is None:
            continue
        if header is None:
            header, first = h, path
            rows.extend(r)
        elif h == header:
            rows.extend(r)
        elif len(h) == len(header) and set(h) == set(header):
            perm = [h.index(c) for c in header]
            rows.extend([row[i] for i in perm] for row in r)
        else:
            raise QueryError(
                "Files for table '%s' have different columns: '%s' has [%s] but '%s' has [%s]"
                % (name, os.path.basename(first), ", ".join(header),
                   os.path.basename(path), ", ".join(h)))
    if header is None:
        header = []
    return header, rows


def parse_sharded(specs):
    """Parse --sharded NAME=GLOB values into an ordered dict name -> glob."""
    out = {}
    for spec in specs or []:
        if "=" not in spec:
            raise QueryError("Invalid --sharded value '%s': expected <table_name>=<glob path>" % spec)
        name, pattern = spec.split("=", 1)
        name, pattern = name.strip(), pattern.strip()
        if not name:
            raise QueryError("Invalid --sharded value '%s': empty table name" % spec)
        if not pattern:
            raise QueryError("Invalid --sharded value '%s': empty glob path" % spec)
        if any(n.lower() == name.lower() for n in out):
            raise QueryError("Table '%s' specified more than once with --sharded" % name)
        out[name] = pattern
    return out


def expand_shards(name, pattern, data_dir=None):
    """Return the list of (path, fmt, compression) matched by a shard glob,
    in lexicographic order by path."""
    import glob
    pattern = os.path.expanduser(pattern)
    paths = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
    if not paths and data_dir and not os.path.isabs(pattern):
        # fall back to resolving the pattern relative to the data directory
        paths = [p for p in glob.glob(os.path.join(data_dir, pattern), recursive=True)
                 if os.path.isfile(p)]
    files = []
    for p in sorted(paths):
        info = split_data_file(os.path.basename(p))
        if info is None:
            raise QueryError("Shard file '%s' for table '%s' has an unsupported format" % (p, name))
        files.append((p, info[1], info[2]))
    return files


# ---------------------------------------------------------------------------
# Scope & resolution
# ---------------------------------------------------------------------------

class Scope:
    def __init__(self):
        self.tables = []   # list of dict(name, alias, columns, offset)
        self.width = 0

    def add(self, name, alias, columns):
        quals = []
        if alias is not None:
            quals.append(alias)
        quals.append(name)
        self.tables.append({"name": name, "alias": alias, "columns": columns,
                            "offset": self.width, "quals": quals})
        self.width += len(columns)

    def copy(self):
        s = Scope()
        s.tables = list(self.tables)
        s.width = self.width
        return s

    def find_table(self, qual):
        for matcher in (lambda a, b: a == b, lambda a, b: a.lower() == b.lower()):
            # aliases take priority, then real table names
            hits = [t for t in self.tables if t["alias"] is not None and matcher(t["alias"], qual)]
            if not hits:
                hits = [t for t in self.tables if matcher(t["name"], qual)]
            if not hits:
                hits = [t for t in self.tables
                        if t["alias"] is None and matcher(t["name"].split(".")[-1], qual)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise QueryError("Ambiguous table reference '%s'" % qual)
        return None

    @staticmethod
    def find_col(table, name):
        cols = table["columns"]
        for i, c in enumerate(cols):
            if c == name:
                return i
        lname = name.lower()
        for i, c in enumerate(cols):
            if c.lower() == lname:
                return i
        return None

    def resolve(self, parts):
        name = parts[-1]
        if len(parts) > 1:
            qual = ".".join(parts[:-1])
            t = self.find_table(qual)
            if t is None:
                raise QueryError("Unknown table or alias '%s'" % qual)
            i = self.find_col(t, name)
            if i is None:
                raise QueryError("Unknown column '%s' in '%s'" % (name, qual))
            return t["offset"] + i
        hits = []
        for t in self.tables:
            cols = t["columns"]
            if name in cols:
                hits.append(t["offset"] + cols.index(name))
        if not hits:
            for t in self.tables:
                i = self.find_col(t, name)
                if i is not None:
                    hits.append(t["offset"] + i)
        if not hits:
            raise QueryError("Unknown column '%s'" % name)
        if len(hits) > 1:
            raise QueryError("Ambiguous column reference '%s'" % name)
        return hits[0]

    def try_resolve(self, parts):
        try:
            return self.resolve(parts)
        except QueryError:
            return None


def resolve_expr(node, scope, aliases=None):
    """Resolve column references; returns (possibly replaced) node."""
    if isinstance(node, Col):
        if aliases is not None and len(node.parts) == 1:
            idx = scope.try_resolve(node.parts)
            if idx is None:
                target = lookup_alias(aliases, node.parts[0])
                if target is not None:
                    return target
        node.idx = scope.resolve(node.parts)
        return node
    if isinstance(node, Cmp):
        node.left = resolve_expr(node.left, scope, aliases)
        node.right = resolve_expr(node.right, scope, aliases)
    elif isinstance(node, BoolOp):
        node.items = [resolve_expr(i, scope, aliases) for i in node.items]
    elif isinstance(node, Not):
        node.item = resolve_expr(node.item, scope, aliases)
    elif isinstance(node, Like):
        node.expr = resolve_expr(node.expr, scope, aliases)
        node.pattern = resolve_expr(node.pattern, scope, aliases)
    elif isinstance(node, IsNull):
        node.expr = resolve_expr(node.expr, scope, aliases)
    elif isinstance(node, InList):
        node.expr = resolve_expr(node.expr, scope, aliases)
        node.items = [resolve_expr(i, scope, aliases) for i in node.items]
    elif isinstance(node, Agg):
        if node.arg is not None:
            node.arg = resolve_expr(node.arg, scope, None)
    return node


def lookup_alias(aliases, name):
    if name in aliases:
        return aliases[name]
    lname = name.lower()
    hits = [v for k, v in aliases.items() if k.lower() == lname]
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def truth(v):
    """Convert value to three-valued boolean (True/False/None)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    n = to_num(v)
    if n is not None:
        return n != 0
    return bool(v)


def evaluate(node, row, group=None):
    if isinstance(node, Col):
        if row is None:
            return None
        return row[node.idx]
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, Agg):
        if group is None:
            raise QueryError("Aggregate functions are not allowed here")
        return eval_agg(node, group)
    if isinstance(node, Cmp):
        c = compare(evaluate(node.left, row, group), evaluate(node.right, row, group))
        if c is None:
            return None
        op = node.op
        if op == "=":
            return c == 0
        if op == "!=":
            return c != 0
        if op == "<":
            return c < 0
        if op == "<=":
            return c <= 0
        if op == ">":
            return c > 0
        return c >= 0
    if isinstance(node, BoolOp):
        if node.op == "AND":
            result = True
            for it in node.items:
                v = truth(evaluate(it, row, group))
                if v is False:
                    return False
                if v is None:
                    result = None
            return result
        result = False
        for it in node.items:
            v = truth(evaluate(it, row, group))
            if v is True:
                return True
            if v is None:
                result = None
        return result
    if isinstance(node, Not):
        v = truth(evaluate(node.item, row, group))
        return None if v is None else (not v)
    if isinstance(node, Like):
        v = evaluate(node.expr, row, group)
        p = evaluate(node.pattern, row, group)
        if v is None or p is None:
            return None
        p = to_str(p)
        rx = node._cache.get(p)
        if rx is None:
            rx = node._cache[p] = like_to_regex(p)
        m = rx.match(to_str(v)) is not None
        return (not m) if node.negate else m
    if isinstance(node, IsNull):
        v = evaluate(node.expr, row, group)
        return (v is not None) if node.negate else (v is None)
    if isinstance(node, InList):
        v = evaluate(node.expr, row, group)
        if v is None:
            return None
        found = False
        saw_null = False
        for it in node.items:
            c = compare(v, evaluate(it, row, group))
            if c is None:
                saw_null = True
            elif c == 0:
                found = True
                break
        if found:
            return not node.negate
        if saw_null:
            return None
        return node.negate
    raise QueryError("Cannot evaluate expression")


def eval_agg(node, group):
    if node.func == "COUNT" and node.arg is None:
        return len(group)
    vals = [evaluate(node.arg, r) for r in group]
    vals = [v for v in vals if v is not None]
    if node.distinct:
        seen = set()
        uniq = []
        for v in vals:
            k = eq_key(v)
            if k not in seen:
                seen.add(k)
                uniq.append(v)
        vals = uniq
    if node.func == "COUNT":
        return len(vals)
    if not vals:
        return None
    if node.func == "SUM":
        total = Decimal(0)
        for v in vals:
            n = to_num(v)
            if n is None:
                raise QueryError("SUM() requires numeric values, found %r" % (v,))
            total += Decimal(n)
        return total
    if node.func == "MIN":
        return min(vals, key=sort_key)
    if node.func == "MAX":
        return max(vals, key=sort_key)
    raise QueryError("Unsupported aggregate %s" % node.func)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def resolve_table_name(tables, parts):
    name = ".".join(parts)
    if name in tables:
        return name
    alt = ".".join(p.replace(".", "_") for p in parts)
    if alt in tables:
        return alt
    lname = name.lower()
    hits = [t for t in tables if t.lower() == lname or t.lower() == alt.lower()]
    if len(hits) == 1:
        return hits[0]
    raise QueryError("Unknown table '%s'" % name)


def simple_equi(on, left_scope_width):
    """If ON is col = col across both sides, return (left_idx, right_idx)."""
    if isinstance(on, Cmp) and on.op == "=" and isinstance(on.left, Col) and isinstance(on.right, Col):
        a, b = on.left.idx, on.right.idx
        if a < left_scope_width <= b:
            return a, b
        if b < left_scope_width <= a:
            return b, a
    return None


def do_join(left_rows, left_width, right_rows, right_width, kind, on):
    out = []
    null_left = [None] * left_width
    null_right = [None] * right_width
    right_matched = [False] * len(right_rows)
    eq = simple_equi(on, left_width) if on is not None else None
    index = None
    if eq is not None:
        index = {}
        ri = eq[1] - left_width
        for j, r in enumerate(right_rows):
            k = eq_key(r[ri])
            if k is not None:
                index.setdefault(k, []).append(j)
    for lrow in left_rows:
        matched = False
        if index is not None:
            k = eq_key(lrow[eq[0]])
            cands = index.get(k, ()) if k is not None else ()
            for j in cands:
                out.append(lrow + right_rows[j])
                right_matched[j] = True
                matched = True
        else:
            for j, rrow in enumerate(right_rows):
                combined = lrow + rrow
                if on is None or truth(evaluate(on, combined)) is True:
                    out.append(combined)
                    right_matched[j] = True
                    matched = True
        if not matched and kind in ("LEFT", "FULL"):
            out.append(lrow + null_right)
    if kind in ("RIGHT", "FULL"):
        for j, rrow in enumerate(right_rows):
            if not right_matched[j]:
                out.append(null_left + rrow)
    return out


def output_name(item):
    if item.alias is not None:
        return item.alias
    if isinstance(item.expr, Col):
        return item.expr.name
    return item.text


def execute(q, data_dir, sharded=None):
    tables = discover_tables(data_dir) if data_dir else {}
    for sname, pattern in (sharded or {}).items():
        # sharded tables take precedence over discovered tables of the same name
        for t in [t for t in tables if t.lower() == sname.lower()]:
            del tables[t]
        tables[sname] = expand_shards(sname, pattern, data_dir)
    scope = Scope()

    def load(tref):
        name = resolve_table_name(tables, tref.name)
        header, rows = load_table(tables[name], name)
        return name, header, rows

    name, header, rows = load(q.table)
    scope.add(name, q.table.alias, header)
    for j in q.joins:
        jname, jheader, jrows = load(j.table)
        left_width = scope.width
        scope.add(jname, j.table.alias, jheader)
        if j.on is not None:
            j.on = resolve_expr(j.on, scope.copy())
        rows = do_join(rows, left_width, jrows, len(jheader), j.kind, j.on)

    # Expand select list
    items = []
    for it in q.select:
        if isinstance(it.expr, Star):
            if it.expr.qualifier is None:
                tlist = scope.tables
            else:
                t = scope.find_table(it.expr.qualifier)
                if t is None:
                    raise QueryError("Unknown table or alias '%s'" % it.expr.qualifier)
                tlist = [t]
            for t in tlist:
                for ci, cname in enumerate(t["columns"]):
                    c = Col([cname])
                    c.idx = t["offset"] + ci
                    items.append(SelectItem(c, None, cname))
        else:
            it.expr = resolve_expr(it.expr, scope)
            items.append(it)

    names = [output_name(it) for it in items]
    aliases = {}
    for it in items:
        if it.alias is not None:
            aliases.setdefault(it.alias, it.expr)

    if q.where is not None:
        where = resolve_expr(q.where, scope)
        rows = [r for r in rows if truth(evaluate(where, r)) is True]

    group_by = [resolve_expr(g, scope, aliases) for g in q.group_by]
    for g in group_by:
        if contains_agg(g):
            raise QueryError("Aggregate functions are not allowed in GROUP BY")
    having = resolve_expr(q.having, scope, aliases) if q.having is not None else None

    # ORDER BY: map to output column index or expression
    order = []
    for oi in q.order_by:
        e = oi.expr
        target = None
        if isinstance(e, Lit) and isinstance(e.value, int) and not isinstance(e.value, bool):
            if not 1 <= e.value <= len(items):
                raise QueryError("ORDER BY position %d is out of range" % e.value)
            target = ("out", e.value - 1)
        elif isinstance(e, Col) and len(e.parts) == 1:
            matches = [i for i, it in enumerate(items) if it.alias is not None and it.alias == e.parts[0]]
            if not matches:
                matches = [i for i, it in enumerate(items)
                           if it.alias is not None and it.alias.lower() == e.parts[0].lower()]
            if len(matches) == 1:
                target = ("out", matches[0])
        if target is None:
            target = ("expr", resolve_expr(e, scope, aliases))
        order.append((target, oi.desc, oi.nulls))

    is_agg = bool(group_by) or having is not None or any(contains_agg(it.expr) for it in items) or \
        any(t[0] == "expr" and contains_agg(t[1]) for t, _, _ in order)

    results = []  # list of (output_values, order_values)
    if is_agg:
        grouped_idx = set()
        for g in group_by:
            if isinstance(g, Col):
                grouped_idx.add(g.idx)

        def validate(node, where_txt):
            if isinstance(node, Agg):
                return
            for gexpr in group_by:
                if same_expr(node, gexpr):
                    return
            if isinstance(node, Col):
                if node.idx not in grouped_idx:
                    raise QueryError("Column '%s' must appear in the GROUP BY clause or be used in an "
                                     "aggregate function (%s)" % (".".join(node.parts), where_txt))
                return
            for c in children(node):
                validate(c, where_txt)

        for it in items:
            validate(it.expr, "SELECT")
        if having is not None:
            validate(having, "HAVING")
        for t, _, _ in order:
            if t[0] == "expr":
                validate(t[1], "ORDER BY")

        groups = {}
        gorder = []
        if group_by:
            for r in rows:
                k = tuple(group_key(evaluate(g, r)) for g in group_by)
                if k not in groups:
                    groups[k] = []
                    gorder.append(k)
                groups[k].append(r)
            # deterministic order: sort by the group key values
            gorder.sort(key=lambda k: tuple(sort_key(evaluate(g, groups[k][0])) for g in group_by))
            glist = [groups[k] for k in gorder]
        else:
            glist = [rows]
        for grp in glist:
            rep = grp[0] if grp else None
            if having is not None and truth(evaluate(having, rep, grp)) is not True:
                continue
            outv = [evaluate(it.expr, rep, grp) for it in items]
            ordv = [outv[t[1]] if t[0] == "out" else evaluate(t[1], rep, grp) for t, _, _ in order]
            results.append((outv, ordv))
    else:
        for r in rows:
            outv = [evaluate(it.expr, r) for it in items]
            ordv = [outv[t[1]] if t[0] == "out" else evaluate(t[1], r) for t, _, _ in order]
            results.append((outv, ordv))

    # sort (stable, last key first)
    for i in range(len(order) - 1, -1, -1):
        _, desc, nulls = order[i]
        if nulls is None:
            nulls_first = not desc
        else:
            nulls_first = nulls == "FIRST"
        nonnull = [x for x in results if x[1][i] is not None]
        nulls_part = [x for x in results if x[1][i] is None]
        nonnull.sort(key=lambda x: sort_key(x[1][i]), reverse=desc)
        if desc:
            # keep stability for equal keys under reverse sort
            nonnull = stable_desc(nonnull, i)
        results = nulls_part + nonnull if nulls_first else nonnull + nulls_part

    out_rows = [r[0] for r in results]
    if q.distinct:
        seen = set()
        uniq = []
        for r in out_rows:
            k = tuple(group_key(v) for v in r)
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        out_rows = uniq
    if q.offset:
        out_rows = out_rows[q.offset:]
    if q.limit is not None:
        out_rows = out_rows[:q.limit]
    return names, out_rows


def stable_desc(lst, i):
    # Python's sort with reverse=True is already stable for equal elements,
    # this function exists for clarity and returns the list unchanged.
    return lst


def same_expr(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, Col):
        return a.idx == b.idx
    if isinstance(a, Lit):
        return a.value == b.value and type(a.value) is type(b.value)
    if isinstance(a, Agg):
        return a.func == b.func and a.distinct == b.distinct and \
            ((a.arg is None and b.arg is None) or (a.arg is not None and b.arg is not None and same_expr(a.arg, b.arg)))
    if isinstance(a, (Cmp, BoolOp)) and a.op != b.op:
        return False
    if isinstance(a, (Like, IsNull, InList)) and a.negate != b.negate:
        return False
    ca, cb = children(a), children(b)
    return len(ca) == len(cb) and all(same_expr(x, y) for x, y in zip(ca, cb))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def md_escape(s):
    return s.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def to_markdown(names, rows):
    hdr = [md_escape(str(n)) for n in names]
    body = [[md_escape(to_str(v)) for v in r] for r in rows]
    widths = [max([len(hdr[i])] + [len(r[i]) for r in body] + [1]) for i in range(len(hdr))]
    lines = ["| " + " | ".join(h.ljust(w) for h, w in zip(hdr, widths)) + " |",
             "| " + " | ".join("-" * w for w in widths) + " |"]
    for r in body:
        lines.append("| " + " | ".join(v.ljust(w) for v, w in zip(r, widths)) + " |")
    return "\n".join(lines)


def write_csv(path, names, rows):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(names)
        for r in rows:
            w.writerow([to_str(v) for v in r])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def read_sql(arg):
    if os.path.isfile(arg):
        with open(arg, encoding="utf-8-sig") as f:
            return f.read()
    return arg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run read-only SQL queries over CSV/TSV/JSON/JSONL/Parquet files.")
    ap.add_argument("--data", help="Directory containing data files (may be nested)")
    ap.add_argument("--sql", required=True, help="SQL query string or path to a .sql file")
    ap.add_argument("--out", help="Optional path to write the result as CSV")
    ap.add_argument("--sharded", action="append", default=[], metavar="TABLE=GLOB",
                    help="Combine all files matching GLOB (lexicographic order) into table TABLE; "
                         "may be given multiple times")
    args = ap.parse_args(argv)

    try:
        if args.data is None and not args.sharded:
            raise QueryError("--data is required unless --sharded is given")
        if args.data is not None and not os.path.isdir(args.data):
            raise QueryError("Data directory not found: %s" % args.data)
        sharded = parse_sharded(args.sharded)
        sql = read_sql(args.sql)
        q = Parser(sql).parse()
        names, rows = execute(q, args.data, sharded)
        print(to_markdown(names, rows))
        if args.out:
            write_csv(args.out, names, rows)
    except QueryError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    rc = main()
    if "pyarrow" in sys.modules:
        # pyarrow's native thread pools can abort during interpreter
        # shutdown; flush output and exit directly to keep the exit code.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
    sys.exit(rc)
