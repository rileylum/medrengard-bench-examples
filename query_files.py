#!/usr/bin/env python3
"""Run read-only SQL queries over a directory of data files.

Supported formats: CSV, TSV, JSON, JSONL and Parquet, each optionally
compressed with gzip (.gz) or bzip2 (.bz2). Files sharing a table name
(e.g. users.csv and users.parquet) are combined and must have identical columns.

Window functions (COUNT/SUM/AVG/MIN/MAX/ROW_NUMBER/RANK/DENSE_RANK ... OVER (...))
are supported; window errors exit with code 3.

Common table expressions (WITH [RECURSIVE] name [(cols)] AS (...)), set operations
(UNION [ALL] / INTERSECT / EXCEPT), scalar, derived-table, correlated, IN, EXISTS and
ANY/ALL subqueries, arithmetic, CASE, common scalar functions and aggregate
FILTER (WHERE ...) clauses are supported. CTE errors exit with code 4, subquery
errors with code 5.

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
    exit_code = 1


class WindowError(QueryError):
    """Errors in window function usage (exit code 3, group WINDOW_ERROR)."""
    exit_code = 3

    def __init__(self, kind, msg):
        self.kind = kind  # INVALID_WINDOW_SPEC, FRAME_ERROR, NESTED_WINDOW_ERROR
        super().__init__("WINDOW_ERROR (%s): %s" % (kind, msg))


class CTEError(QueryError):
    """Errors in common table expressions (exit code 4, group CTE_ERROR)."""
    exit_code = 4

    def __init__(self, kind, msg):
        # INVALID_CTE_SYNTAX, RECURSIVE_DEPTH_EXCEEDED, CTE_NOT_FOUND, CYCLIC_REFERENCE
        self.kind = kind
        super().__init__("CTE_ERROR (%s): %s" % (kind, msg))


class SubqueryError(QueryError):
    """Errors in subqueries (exit code 5, group SUBQUERY_ERROR)."""
    exit_code = 5

    def __init__(self, kind, msg):
        # SCALAR_SUBQUERY_MULTIPLE_ROWS, INVALID_SUBQUERY_REFERENCE, CORRELATED_SUBQUERY_ERROR
        self.kind = kind
        super().__init__("SUBQUERY_ERROR (%s): %s" % (kind, msg))


class ColumnMissing(QueryError):
    """A qualifier matched a table but the column does not exist in it."""


MAX_RECURSION = 1000  # maximum number of recursive iterations of a recursive CTE


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
    if isinstance(v, str):
        try:
            return _NUM_CACHE[v]
        except KeyError:
            pass
        n = None
        if _NUM_RE.match(v):
            try:
                n = Decimal(v.strip())
            except InvalidOperation:
                n = None
        if len(_NUM_CACHE) > 500000:
            _NUM_CACHE.clear()
        _NUM_CACHE[v] = n
        return n
    return None


_NUM_CACHE = {}


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
        pass
    else:
        na, nb = to_str(a), to_str(b)
    return (na > nb) - (na < nb)


class FloatDec(Decimal):
    """A Decimal rendered like a floating point number (e.g. 94000.0)."""


def to_str(v):
    if v is None:
        return "NULL"
    if isinstance(v, FloatDec):
        return repr(float(v))
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
    "WITH", "UNION", "INTERSECT", "EXCEPT", "EXISTS",
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
        if two == "||":
            toks.append(Tok("OP", "||", start, i + 2))
            i += 2
            continue
        if two in ("!=", "<>", "<=", ">=", "=="):
            toks.append(Tok("OP", "!=" if two == "<>" else ("=" if two == "==" else two), start, i + 2))
            i += 2
            continue
        if c in "=<>(),.*;-+/%":
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
    FIELDS = ()


class Col(Node):
    def __init__(self, parts):
        self.parts = parts
        self.idx = None
        self.level = 0
        self.outer = False

    @property
    def name(self):
        return self.parts[-1]


class Lit(Node):
    def __init__(self, value):
        self.value = value


class Agg(Node):
    def __init__(self, func, arg, distinct=False, filter=None):
        self.func = func  # COUNT SUM AVG MIN MAX
        self.arg = arg    # None means '*'
        self.distinct = distinct
        self.filter = filter  # FILTER (WHERE ...) condition or None


class Window(Node):
    """func(arg) OVER (PARTITION BY ... ORDER BY ... frame)."""

    def __init__(self, func, arg, partition, order, frame, filter=None):
        self.func = func          # COUNT SUM AVG MIN MAX ROW_NUMBER RANK DENSE_RANK
        self.arg = arg            # None for COUNT(*) and ranking functions
        self.partition = partition  # list of exprs
        self.order = order        # list of OrderItem
        self.frame = frame        # None or (mode, start, end); bound = (kind, n)
        self.filter = filter
        self.values = None


class Cmp(Node):
    FIELDS = ("left", "right")

    def __init__(self, op, left, right):
        self.op, self.left, self.right = op, left, right


class BoolOp(Node):
    FIELDS = ("items",)

    def __init__(self, op, items):
        self.op, self.items = op, items


class Not(Node):
    FIELDS = ("item",)

    def __init__(self, item):
        self.item = item


class Like(Node):
    FIELDS = ("expr", "pattern")

    def __init__(self, expr, pattern, negate):
        self.expr, self.pattern, self.negate = expr, pattern, negate
        self._cache = {}


class IsNull(Node):
    FIELDS = ("expr",)

    def __init__(self, expr, negate):
        self.expr, self.negate = expr, negate


class InList(Node):
    FIELDS = ("expr", "items")

    def __init__(self, expr, items, negate):
        self.expr, self.items, self.negate = expr, items, negate


class Between(Node):
    FIELDS = ("expr", "lo", "hi")

    def __init__(self, expr, lo, hi, negate):
        self.expr, self.lo, self.hi, self.negate = expr, lo, hi, negate


class Arith(Node):
    FIELDS = ("left", "right")

    def __init__(self, op, left, right):
        self.op, self.left, self.right = op, left, right


class Neg(Node):
    FIELDS = ("item",)

    def __init__(self, item):
        self.item = item


class Case(Node):
    FIELDS = ("operand", "conds", "results", "default")

    def __init__(self, operand, conds, results, default):
        self.operand, self.conds, self.results, self.default = operand, conds, results, default


class Func(Node):
    FIELDS = ("args",)

    def __init__(self, name, args):
        self.name, self.args = name, args


class Cast(Node):
    FIELDS = ("expr",)

    def __init__(self, expr, typ):
        self.expr, self.typ = expr, typ


class SubqueryExpr(Node):
    """A nested SELECT used inside an expression.

    kind: SCALAR, EXISTS, IN (expr [NOT] IN (subquery)), ANY / ALL (expr op ANY|ALL (subquery)).
    """
    FIELDS = ("expr",)

    def __init__(self, kind, query, expr=None, op=None, negate=False):
        self.kind, self.query, self.expr, self.op, self.negate = kind, query, expr, op, negate
        self.plan = None
        self.level = 0
        self.refs = set()   # outer (level, idx) references -> correlation
        self.cache = {}
        self.epoch = None
        self.float_avg = False


class Star(Node):
    def __init__(self, qualifier=None):
        self.qualifier = qualifier


class SelectItem:
    def __init__(self, expr, alias, text):
        self.expr, self.alias, self.text = expr, alias, text


class TableRef:
    def __init__(self, name, alias):
        self.name, self.alias = name, alias


class DerivedRef:
    def __init__(self, query, alias, columns):
        self.query, self.alias, self.columns = query, alias, columns


class Join:
    def __init__(self, kind, table, on):
        self.kind, self.table, self.on = kind, table, on


class OrderItem:
    def __init__(self, expr, desc, nulls):
        self.expr, self.desc, self.nulls = expr, desc, nulls


class Query:
    """A single SELECT ... FROM ... block."""


class SetQuery:
    """left UNION/INTERSECT/EXCEPT [ALL] right (op None: parenthesised pass-through)."""

    def __init__(self, op, all_, left, right):
        self.op, self.all, self.left, self.right = op, all_, left, right
        self.order_by, self.limit, self.offset = [], None, None


class CTEDef:
    def __init__(self, name, columns, query):
        self.name, self.columns, self.query = name, columns, query


class With:
    def __init__(self, ctes, recursive, body):
        self.ctes, self.recursive, self.body = ctes, recursive, body


def children(node):
    if isinstance(node, Agg):
        return [x for x in (node.arg, node.filter) if x is not None]
    if isinstance(node, Window):
        out = [node.arg] if node.arg is not None else []
        out += list(node.partition) + [o.expr for o in node.order]
        if node.filter is not None:
            out.append(node.filter)
        return out
    out = []
    for f in node.FIELDS:
        v = getattr(node, f)
        if isinstance(v, list):
            out.extend(x for x in v if x is not None)
        elif v is not None:
            out.append(v)
    return out


def walk(node):
    yield node
    for c in children(node):
        yield from walk(c)


def contains_agg(node):
    return any(isinstance(n, Agg) for n in walk(node))


def contains_window(node):
    return any(isinstance(n, Window) for n in walk(node))


def reject_window(node, where):
    if node is not None and contains_window(node):
        raise WindowError("INVALID_WINDOW_SPEC", "Window functions are not allowed in %s" % where)


def query_exprs(q):
    """All expressions directly contained in a SELECT block."""
    out = [it.expr for it in q.select]
    for j in q.joins:
        if j.on is not None:
            out.append(j.on)
    for e in (q.where, q.having):
        if e is not None:
            out.append(e)
    out += list(q.group_by) + [o.expr for o in q.order_by]
    return out


def query_refs(q, out=None):
    """Lower-cased single-part table names referenced anywhere in a query."""
    if out is None:
        out = set()
    if isinstance(q, With):
        for d in q.ctes:
            query_refs(d.query, out)
        query_refs(q.body, out)
    elif isinstance(q, SetQuery):
        query_refs(q.left, out)
        if q.right is not None:
            query_refs(q.right, out)
    elif isinstance(q, Query):
        trefs = ([q.table] if q.table is not None else []) + [j.table for j in q.joins]
        for t in trefs:
            if isinstance(t, DerivedRef):
                query_refs(t.query, out)
            elif len(t.name) == 1:
                out.add(t.name[0].lower())
        for e in query_exprs(q):
            for n in walk(e):
                if isinstance(n, SubqueryExpr):
                    query_refs(n.query, out)
    return out


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
RANK_FUNCS = {"ROW_NUMBER", "RANK", "DENSE_RANK"}
# scalar function name -> (min args, max args)
SCALAR_FUNCS = {
    "COALESCE": (1, 99), "IFNULL": (2, 2), "NULLIF": (2, 2), "ABS": (1, 1), "ROUND": (1, 2),
    "UPPER": (1, 1), "LOWER": (1, 1), "LENGTH": (1, 1), "LEN": (1, 1), "TRIM": (1, 1),
    "LTRIM": (1, 1), "RTRIM": (1, 1), "SUBSTR": (2, 3), "SUBSTRING": (2, 3), "CONCAT": (1, 99),
    "FLOOR": (1, 1), "CEIL": (1, 1), "CEILING": (1, 1), "REPLACE": (3, 3),
}
CLAUSE_END = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET",
              "INNER", "LEFT", "RIGHT", "FULL", "JOIN", "ON", "CROSS"}


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.toks = tokenize(sql)
        self.pos = 0
        self.saw_with = False

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
        if self.peek().kind == "EOF":
            raise QueryError("Empty query")
        q = self.parse_query()
        while self.accept_op(";"):
            pass
        if self.peek().kind != "EOF":
            raise QueryError("Unexpected %s after end of query" % self.describe(self.peek()))
        return q

    def starts_query(self, k=0):
        """True if a (possibly parenthesised) query starts at offset k."""
        while self.is_op("(", k=k):
            k += 1
        return self.is_kw("SELECT", "WITH", k=k)

    def parse_query(self):
        if self.is_kw("WITH"):
            self.saw_with = True
            self.next()
            recursive = bool(self.accept_word("RECURSIVE"))
            ctes = self.parse_cte_list()
            if not self.starts_query():
                raise CTEError("INVALID_CTE_SYNTAX", "Expected SELECT after WITH clause but found %s"
                               % self.describe(self.peek()))
            body = self.parse_set_expr()
            return With(ctes, recursive, body)
        return self.parse_set_expr()

    def parse_cte_list(self):
        ctes = []
        seen = set()
        while True:
            try:
                d = self.parse_cte()
            except (CTEError, WindowError, SubqueryError):
                raise
            except QueryError as e:
                raise CTEError("INVALID_CTE_SYNTAX", str(e))
            if d.name.lower() in seen:
                raise CTEError("INVALID_CTE_SYNTAX", "CTE name '%s' specified more than once" % d.name)
            seen.add(d.name.lower())
            ctes.append(d)
            if not self.accept_op(","):
                break
        return ctes

    def parse_cte(self):
        t = self.peek()
        if t.kind not in ("IDENT", "QIDENT"):
            raise CTEError("INVALID_CTE_SYNTAX", "Expected CTE name but found %s" % self.describe(t))
        name = self.next().value
        cols = None
        if self.accept_op("("):
            cols = [self.ident()]
            while self.accept_op(","):
                cols.append(self.ident())
            self.expect_op(")")
            if len(set(c.lower() for c in cols)) != len(cols):
                raise CTEError("INVALID_CTE_SYNTAX", "Duplicate column name in column list of CTE '%s'" % name)
        if not self.accept_kw("AS"):
            raise CTEError("INVALID_CTE_SYNTAX", "Expected AS after CTE name '%s' but found %s"
                           % (name, self.describe(self.peek())))
        if not self.is_op("("):
            raise CTEError("INVALID_CTE_SYNTAX", "Expected '(' after AS in CTE '%s' but found %s"
                           % (name, self.describe(self.peek())))
        self.next()
        if not self.starts_query():
            raise CTEError("INVALID_CTE_SYNTAX", "Expected SELECT in CTE '%s' but found %s"
                           % (name, self.describe(self.peek())))
        q = self.parse_query()
        if not self.is_op(")"):
            raise CTEError("INVALID_CTE_SYNTAX", "Expected ')' to close CTE '%s' but found %s"
                           % (name, self.describe(self.peek())))
        self.next()
        return CTEDef(name, cols, q)

    def parse_set_expr(self):
        node = self.parse_set_term()
        while self.is_kw("UNION", "INTERSECT", "EXCEPT"):
            op = self.next().value
            all_ = bool(self.accept_kw("ALL"))
            if not all_:
                self.accept_kw("DISTINCT")
            right = self.parse_set_term()
            node = SetQuery(op, all_, node, right)
        order, limit, offset = self.parse_order_limit()
        if order or limit is not None or offset is not None:
            if isinstance(node, Query) and not node.order_by and node.limit is None and node.offset is None:
                target = node
            elif isinstance(node, SetQuery) and not node.order_by and node.limit is None \
                    and node.offset is None:
                target = node
            else:
                target = node = SetQuery(None, False, node, None)
            target.order_by, target.limit, target.offset = order, limit, offset
        return node

    def parse_set_term(self):
        if self.is_op("(") and self.starts_query(1):
            self.next()
            q = self.parse_query()
            self.expect_op(")")
            return q
        return self.parse_select()

    def parse_order_limit(self):
        order = []
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            order.append(self.parse_order_item())
            while self.accept_op(","):
                order.append(self.parse_order_item())
        limit = offset = None
        for _ in range(2):
            if limit is None and self.accept_kw("LIMIT"):
                limit = self.parse_nonneg_int("LIMIT")
                if self.accept_op(","):
                    # MySQL style LIMIT offset, count
                    offset, limit = limit, self.parse_nonneg_int("LIMIT")
            elif offset is None and self.accept_kw("OFFSET"):
                offset = self.parse_nonneg_int("OFFSET")
        return order, limit, offset

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
        q.table = None
        q.joins = []
        if self.accept_kw("FROM"):
            q.table = self.parse_table_ref()
            while True:
                kind = None
                if self.is_op(","):
                    self.next()
                    q.joins.append(Join("CROSS", self.parse_table_ref(), None))
                    continue
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
                    reject_window(on, "JOIN conditions")
                    if contains_agg(on):
                        raise QueryError("Aggregate functions are not allowed in JOIN conditions")
                q.joins.append(Join(kind, tref, on))
        q.where = None
        if self.accept_kw("WHERE"):
            q.where = self.parse_expr()
            reject_window(q.where, "WHERE")
            if contains_agg(q.where):
                raise QueryError("Aggregate functions are not allowed in WHERE")
        q.group_by = []
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            q.group_by.append(self.parse_expr())
            while self.accept_op(","):
                q.group_by.append(self.parse_expr())
            for g in q.group_by:
                reject_window(g, "GROUP BY")
                if contains_agg(g):
                    raise QueryError("Aggregate functions are not allowed in GROUP BY")
        q.having = None
        if self.accept_kw("HAVING"):
            q.having = self.parse_expr()
            reject_window(q.having, "HAVING")
        q.order_by = []
        q.limit = None
        q.offset = None
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
        if self.is_op("("):
            if not self.starts_query(1):
                raise QueryError("Expected subquery after '(' in FROM clause but found %s"
                                 % self.describe(self.peek(1)))
            self.next()
            sub = self.parse_query()
            self.expect_op(")")
            alias = None
            if self.accept_kw("AS"):
                alias = self.ident()
            elif self.is_ident():
                alias = self.ident()
            if alias is None:
                raise SubqueryError("INVALID_SUBQUERY_REFERENCE", "Subquery in FROM must have an alias")
            cols = None
            if self.is_op("(") and self.is_ident(1):
                self.next()
                cols = [self.ident()]
                while self.accept_op(","):
                    cols.append(self.ident())
                self.expect_op(")")
            return DerivedRef(sub, alias, cols)
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

    def parse_subquery_parens(self):
        self.expect_op("(")
        q = self.parse_query()
        self.expect_op(")")
        return q

    def parse_predicate(self):
        left = self.parse_additive()
        while True:
            if self.is_op("=", "!=", "<", "<=", ">", ">="):
                op = self.next().value
                if (self.is_word("ANY") or self.is_word("SOME") or self.is_kw("ALL")) \
                        and self.is_op("(", k=1) and self.starts_query(2):
                    quant = "ALL" if self.next().value.upper() == "ALL" else "ANY"
                    left = SubqueryExpr(quant, self.parse_subquery_parens(), left, op)
                    continue
                right = self.parse_additive()
                left = Cmp(op, left, right)
                continue
            negate = False
            if self.is_kw("NOT") and (self.is_kw("LIKE", "IN", k=1) or self.is_word("BETWEEN", k=1)):
                self.next()
                negate = True
            if self.accept_kw("LIKE"):
                left = Like(left, self.parse_additive(), negate)
                continue
            if self.accept_word("BETWEEN"):
                lo = self.parse_additive()
                self.expect_kw("AND")
                hi = self.parse_additive()
                left = Between(left, lo, hi, negate)
                continue
            if self.accept_kw("IN"):
                if self.is_op("(") and self.starts_query(1):
                    left = SubqueryExpr("IN", self.parse_subquery_parens(), left, negate=negate)
                    continue
                self.expect_op("(")
                items = [self.parse_additive()]
                while self.accept_op(","):
                    items.append(self.parse_additive())
                self.expect_op(")")
                left = InList(left, items, negate)
                continue
            if self.accept_kw("IS"):
                neg = bool(self.accept_kw("NOT"))
                self.expect_kw("NULL")
                left = IsNull(left, neg)
                continue
            return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.is_op("+", "-", "||"):
            op = self.next().value
            left = Arith(op, left, self.parse_multiplicative())
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.is_op("*", "/", "%"):
            op = self.next().value
            left = Arith(op, left, self.parse_unary())
        return left

    def parse_unary(self):
        if self.is_op("-", "+") and self.peek(1).kind != "NUM":
            op = self.next().value
            item = self.parse_unary()
            return Neg(item) if op == "-" else item
        return self.parse_primary()

    def parse_primary(self):
        t = self.peek()
        if t.kind == "OP" and t.value == "(":
            if self.starts_query(1):
                return SubqueryExpr("SCALAR", self.parse_subquery_parens())
            self.next()
            e = self.parse_expr()
            self.expect_op(")")
            return e
        if t.kind == "KW" and t.value == "EXISTS":
            self.next()
            if not (self.is_op("(") and self.starts_query(1)):
                raise QueryError("Expected subquery after EXISTS")
            return SubqueryExpr("EXISTS", self.parse_subquery_parens())
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
        if t.kind == "IDENT" and t.value.upper() == "CASE":
            return self.parse_case()
        if t.kind in ("IDENT", "QIDENT"):
            if t.kind == "IDENT" and self.is_op("(", k=1):
                return self.parse_func()
            parts = [self.next().value]
            while self.is_op(".") and self.is_ident(1):
                self.next()
                parts.append(self.next().value)
            return Col(parts)
        raise QueryError("Unexpected %s in expression" % self.describe(t))

    def parse_case(self):
        self.next()  # CASE
        operand = None
        if not self.is_word("WHEN"):
            operand = self.parse_expr()
        conds, results = [], []
        while self.accept_word("WHEN"):
            conds.append(self.parse_expr())
            if not self.accept_word("THEN"):
                raise QueryError("Expected THEN in CASE expression but found %s" % self.describe(self.peek()))
            results.append(self.parse_expr())
        if not conds:
            raise QueryError("CASE expression requires at least one WHEN clause")
        default = None
        if self.accept_word("ELSE"):
            default = self.parse_expr()
        if not self.accept_word("END"):
            raise QueryError("Expected END to close CASE expression but found %s" % self.describe(self.peek()))
        return Case(operand, conds, results, default)

    @staticmethod
    def num_lit(s):
        if "." in s:
            return Decimal(s)
        return int(s)

    def is_word(self, word, k=0):
        t = self.peek(k)
        return t.kind == "IDENT" and t.value.upper() == word

    def accept_word(self, word):
        if self.is_word(word):
            return self.next()
        return None

    def parse_filter(self):
        """Optional FILTER (WHERE cond) after an aggregate call."""
        if not (self.is_word("FILTER") and self.is_op("(", k=1)):
            return None
        self.next()
        self.next()
        self.expect_kw("WHERE")
        cond = self.parse_expr()
        self.expect_op(")")
        reject_window(cond, "FILTER clauses")
        if contains_agg(cond):
            raise QueryError("Aggregate functions are not allowed in FILTER clauses")
        return cond

    def parse_func(self):
        name_tok = self.next()
        fname = name_tok.value.upper()
        if fname in RANK_FUNCS:
            self.expect_op("(")
            if not self.is_op(")"):
                raise WindowError("INVALID_WINDOW_SPEC", "%s() takes no arguments" % fname)
            self.next()
            if not self.is_word("OVER"):
                raise WindowError("INVALID_WINDOW_SPEC", "%s() requires an OVER clause" % fname)
            self.next()
            w = self.parse_window(fname, None)
            if self.is_word("FILTER"):
                raise WindowError("INVALID_WINDOW_SPEC", "FILTER is not allowed with %s()" % fname)
            return w
        if fname == "CAST":
            self.expect_op("(")
            e = self.parse_expr()
            self.expect_kw("AS")
            typ = self.ident().upper()
            while self.is_ident():
                typ += " " + self.ident().upper()
            if self.accept_op("("):
                while not self.is_op(")"):
                    if self.peek().kind == "EOF":
                        raise QueryError("Unterminated type in CAST")
                    self.next()
                self.next()
            self.expect_op(")")
            return Cast(e, typ)
        if fname in SCALAR_FUNCS:
            self.expect_op("(")
            args = []
            if not self.is_op(")"):
                args.append(self.parse_expr())
                while self.accept_op(","):
                    args.append(self.parse_expr())
            self.expect_op(")")
            lo, hi = SCALAR_FUNCS[fname]
            if not lo <= len(args) <= hi:
                raise QueryError("Wrong number of arguments to %s()" % name_tok.value)
            return Func(fname, args)
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
        self.expect_op(")")
        filt = self.parse_filter()
        if self.is_word("OVER"):
            self.next()
            if distinct:
                raise WindowError("INVALID_WINDOW_SPEC", "DISTINCT is not supported in window functions")
            if arg is not None and contains_window(arg):
                raise WindowError("NESTED_WINDOW_ERROR", "Window functions cannot be nested")
            w = self.parse_window(fname, arg)
            w.filter = filt
            return w
        if arg is not None:
            if contains_window(arg):
                raise WindowError("NESTED_WINDOW_ERROR",
                                  "Window functions are not allowed inside aggregate functions")
            if contains_agg(arg):
                raise QueryError("Nested aggregate functions are not allowed")
        return Agg(fname, arg, distinct, filt)

    def parse_window(self, fname, arg):
        if not self.is_op("("):
            raise WindowError("INVALID_WINDOW_SPEC",
                              "Expected '(' after OVER but found %s" % self.describe(self.peek()))
        self.next()
        try:
            partition = []
            if self.accept_word("PARTITION"):
                self.expect_kw("BY")
                partition.append(self.parse_expr())
                while self.accept_op(","):
                    partition.append(self.parse_expr())
            order = []
            if self.accept_kw("ORDER"):
                self.expect_kw("BY")
                order.append(self.parse_order_item())
                while self.accept_op(","):
                    order.append(self.parse_order_item())
            frame = None
            if self.is_word("ROWS") or self.is_word("RANGE") or self.is_word("GROUPS"):
                frame = self.parse_frame(order)
            if not self.is_op(")"):
                raise QueryError("Expected ')' to close window specification but found %s"
                                 % self.describe(self.peek()))
            self.next()
        except WindowError:
            raise
        except QueryError as e:
            raise WindowError("INVALID_WINDOW_SPEC", str(e))
        for e in partition + [o.expr for o in order]:
            if contains_window(e):
                raise WindowError("NESTED_WINDOW_ERROR", "Window functions cannot be nested")
        return Window(fname, arg, partition, order, frame)

    def parse_frame(self, order):
        mode = self.next().value.upper()
        if mode == "GROUPS":
            raise WindowError("FRAME_ERROR", "GROUPS frames are not supported")
        if self.is_word("BETWEEN"):
            self.next()
            start = self.parse_bound(mode)
            if not self.accept_kw("AND"):
                raise WindowError("FRAME_ERROR", "Expected AND in frame clause but found %s"
                                  % self.describe(self.peek()))
            end = self.parse_bound(mode)
        else:
            start = self.parse_bound(mode)
            end = ("C", None)
        rank = {"UP": 0, "P": 1, "C": 2, "F": 3, "UF": 4}
        if start[0] == "UF":
            raise WindowError("FRAME_ERROR", "Frame start cannot be UNBOUNDED FOLLOWING")
        if end[0] == "UP":
            raise WindowError("FRAME_ERROR", "Frame end cannot be UNBOUNDED PRECEDING")
        if rank[start[0]] > rank[end[0]]:
            raise WindowError("FRAME_ERROR", "Frame starting from %s cannot end with %s"
                              % (self.bound_text(start), self.bound_text(end)))
        if mode == "RANGE" and (start[0] in ("P", "F") or end[0] in ("P", "F")):
            if len(order) != 1:
                raise WindowError("FRAME_ERROR", "RANGE with offset PRECEDING/FOLLOWING requires "
                                  "exactly one ORDER BY column")
        if self.is_word("EXCLUDE"):
            raise WindowError("FRAME_ERROR", "EXCLUDE is not supported in frame clauses")
        return (mode, start, end)

    @staticmethod
    def bound_text(b):
        return {"UP": "UNBOUNDED PRECEDING", "UF": "UNBOUNDED FOLLOWING", "C": "CURRENT ROW",
                "P": "%s PRECEDING" % b[1], "F": "%s FOLLOWING" % b[1]}[b[0]]

    def parse_bound(self, mode):
        t = self.peek()
        if self.accept_word("UNBOUNDED"):
            if self.accept_word("PRECEDING"):
                return ("UP", None)
            if self.accept_word("FOLLOWING"):
                return ("UF", None)
            raise WindowError("FRAME_ERROR", "Expected PRECEDING or FOLLOWING after UNBOUNDED")
        if self.accept_word("CURRENT"):
            if not self.accept_word("ROW"):
                raise WindowError("FRAME_ERROR", "Expected ROW after CURRENT")
            return ("C", None)
        if t.kind == "NUM":
            self.next()
            if mode == "ROWS":
                if not t.value.isdigit():
                    raise WindowError("FRAME_ERROR", "ROWS frame offset must be a non-negative integer")
                n = int(t.value)
            else:
                n = Decimal(t.value)
            if self.accept_word("PRECEDING"):
                return ("P", n)
            if self.accept_word("FOLLOWING"):
                return ("F", n)
            raise WindowError("FRAME_ERROR", "Expected PRECEDING or FOLLOWING after frame offset")
        if t.kind == "OP" and t.value == "-":
            raise WindowError("FRAME_ERROR", "Frame offset must not be negative")
        raise WindowError("FRAME_ERROR", "Invalid frame bound: %s" % self.describe(t))


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
        rows.append([None if v == "" or v == "NULL" else v for v in row])
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

    def add(self, name, alias, columns, derived=False):
        quals = []
        if alias is not None:
            quals.append(alias)
        quals.append(name)
        self.tables.append({"name": name, "alias": alias, "columns": columns,
                            "offset": self.width, "quals": quals, "derived": derived})
        self.width += len(columns)

    def copy(self):
        s = Scope()
        s.tables = list(self.tables)
        s.width = self.width
        return s

    @property
    def has_derived(self):
        return any(t["derived"] for t in self.tables)

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

    def lookup(self, parts):
        """Return the row index of a column, or None if it is not visible in this scope.
        Raises on ambiguity or on a missing column of a matched table."""
        name = parts[-1]
        if len(parts) > 1:
            qual = ".".join(parts[:-1])
            t = self.find_table(qual)
            if t is None:
                return None
            i = self.find_col(t, name)
            if i is None:
                raise ColumnMissing("Unknown column '%s' in '%s'" % (name, qual))
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
            return None
        if len(hits) > 1:
            raise QueryError("Ambiguous column reference '%s'" % name)
        return hits[0]


class Ctx:
    """Planning context: visible CTEs, enclosing scopes (one per outer level) and
    the chain of enclosing expression subqueries (for correlation tracking)."""

    def __init__(self, env, ctes=None, outer=None, sub_chain=None, in_sub=False):
        self.env = env
        self.ctes = ctes or {}
        self.outer = outer or []
        self.sub_chain = sub_chain or []
        self.in_sub = in_sub

    @property
    def level(self):
        return len(self.outer)

    def derive(self, **kw):
        c = Ctx(self.env, self.ctes, self.outer, self.sub_chain, self.in_sub)
        for k, v in kw.items():
            setattr(c, k, v)
        return c


class Resolver:
    def __init__(self, ctx, scope):
        self.ctx = ctx
        self.scope = scope

    def resolve_col(self, node):
        ctx = self.ctx
        scopes = ctx.outer + [self.scope]
        cur = len(scopes) - 1
        for lv in range(cur, -1, -1):
            try:
                idx = scopes[lv].lookup(node.parts)
            except ColumnMissing as e:
                if lv < cur:
                    raise SubqueryError("CORRELATED_SUBQUERY_ERROR", "%s (outer query reference)" % e)
                if ctx.in_sub:
                    raise SubqueryError("INVALID_SUBQUERY_REFERENCE", "%s in subquery" % e)
                t = self.scope.find_table(".".join(node.parts[:-1]))
                if t is not None and t["derived"]:
                    raise SubqueryError("INVALID_SUBQUERY_REFERENCE",
                                        "%s: derived table does not expose this column" % e)
                raise QueryError(str(e))
            if idx is None:
                continue
            node.idx, node.level, node.outer = idx, lv, lv < cur
            if lv < cur:
                for sq in ctx.sub_chain:
                    if sq.level > lv:
                        sq.refs.add((lv, idx))
            return node
        full = ".".join(node.parts)
        if len(node.parts) > 1:
            qual = ".".join(node.parts[:-1])
            if ctx.in_sub:
                raise SubqueryError("CORRELATED_SUBQUERY_ERROR",
                                    "Unknown table or alias '%s' referenced in subquery (column '%s')"
                                    % (qual, full))
            if self.scope.has_derived:
                raise SubqueryError("INVALID_SUBQUERY_REFERENCE",
                                    "Unknown table or alias '%s'; tables inside a derived table are not "
                                    "visible outside it" % qual)
            raise QueryError("Unknown table or alias '%s'" % qual)
        if ctx.in_sub:
            raise SubqueryError("INVALID_SUBQUERY_REFERENCE", "Unknown column '%s' in subquery" % full)
        if self.scope.has_derived:
            raise SubqueryError("INVALID_SUBQUERY_REFERENCE",
                                "Unknown column '%s' (not exposed by derived table)" % full)
        raise QueryError("Unknown column '%s'" % full)

    def try_resolve_local(self, parts):
        try:
            return self.scope.lookup(parts)
        except QueryError:
            return None

    def resolve(self, node, aliases=None):
        """Resolve column references and plan subqueries; returns (possibly replaced) node."""
        if node is None:
            return None
        if isinstance(node, Col):
            if aliases is not None and len(node.parts) == 1:
                if self.try_resolve_local(node.parts) is None:
                    target = lookup_alias(aliases, node.parts[0])
                    if target is not None:
                        return target
            return self.resolve_col(node)
        if isinstance(node, Agg):
            node.arg = self.resolve(node.arg)
            node.filter = self.resolve(node.filter)
            return node
        if isinstance(node, Window):
            node.arg = self.resolve(node.arg)
            node.filter = self.resolve(node.filter)
            node.partition = [self.resolve(p) for p in node.partition]
            for o in node.order:
                o.expr = self.resolve(o.expr)
            return node
        if isinstance(node, SubqueryExpr):
            node.expr = self.resolve(node.expr, aliases)
            self.plan_subquery(node)
            return node
        for f in node.FIELDS:
            v = getattr(node, f)
            if isinstance(v, list):
                setattr(node, f, [self.resolve(x, aliases) for x in v])
            elif v is not None:
                setattr(node, f, self.resolve(v, aliases))
        return node

    def plan_subquery(self, sq):
        if sq.plan is not None:
            return
        ctx = self.ctx
        sq.level = ctx.level + 1
        sub_ctx = ctx.derive(outer=ctx.outer + [self.scope], sub_chain=ctx.sub_chain + [sq], in_sub=True)
        sq.plan = plan_query(sq.query, sub_ctx)
        if sq.kind != "EXISTS" and len(sq.plan.names) != 1:
            raise SubqueryError("INVALID_SUBQUERY_REFERENCE",
                                "Subquery used as %s must return exactly one column, got %d"
                                % ("a scalar value" if sq.kind == "SCALAR" else "an operand of %s" % sq.kind,
                                   len(sq.plan.names)))
        p = sq.plan
        if sq.kind == "SCALAR" and isinstance(p, SelectPlan) and isinstance(p.items[0].expr, Agg) \
                and p.items[0].expr.func == "AVG":
            sq.float_avg = True


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
        if node.outer:
            orow = OUTER[node.level] if node.level < len(OUTER) else None
            return None if orow is None else orow[node.idx]
        if row is None:
            return None
        return row[node.idx]
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, Agg):
        if group is None:
            raise QueryError("Aggregate functions are not allowed here")
        return eval_agg(node, group)
    if isinstance(node, Window):
        if node.values is None or _CUR_CTX[0] is None:
            raise WindowError("INVALID_WINDOW_SPEC", "Window functions are not allowed here")
        return node.values[_CUR_CTX[0]]
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
    if isinstance(node, Arith):
        return arith(node.op, evaluate(node.left, row, group), evaluate(node.right, row, group))
    if isinstance(node, Neg):
        v = evaluate(node.item, row, group)
        if v is None:
            return None
        n = to_num(v)
        if n is None:
            raise QueryError("Cannot negate non-numeric value %r" % (v,))
        return -n
    if isinstance(node, Between):
        v = evaluate(node.expr, row, group)
        lo = compare(v, evaluate(node.lo, row, group))
        hi = compare(v, evaluate(node.hi, row, group))
        a = None if lo is None else lo >= 0
        b = None if hi is None else hi <= 0
        if a is False or b is False:
            res = False
        elif a is None or b is None:
            return None
        else:
            res = True
        return (not res) if node.negate else res
    if isinstance(node, Case):
        if node.operand is not None:
            base = evaluate(node.operand, row, group)
            for c, r in zip(node.conds, node.results):
                if compare(base, evaluate(c, row, group)) == 0:
                    return evaluate(r, row, group)
        else:
            for c, r in zip(node.conds, node.results):
                if truth(evaluate(c, row, group)) is True:
                    return evaluate(r, row, group)
        return evaluate(node.default, row, group) if node.default is not None else None
    if isinstance(node, Func):
        return eval_func(node, row, group)
    if isinstance(node, Cast):
        return eval_cast(evaluate(node.expr, row, group), node.typ)
    if isinstance(node, SubqueryExpr):
        return eval_subquery(node, row, group)
    raise QueryError("Cannot evaluate expression")


def arith(op, a, b):
    if a is None or b is None:
        return None
    if op == "||":
        return to_str(a) + to_str(b)
    na, nb = to_num(a), to_num(b)
    if na is None or nb is None:
        bad = a if na is None else b
        raise QueryError("Arithmetic operator '%s' requires numeric values, found %r" % (op, bad))
    if isinstance(na, int) and isinstance(nb, int) and op in ("+", "-", "*"):
        return na + nb if op == "+" else (na - nb if op == "-" else na * nb)
    da, db = Decimal(na), Decimal(nb)
    if op == "+":
        return da + db
    if op == "-":
        return da - db
    if op == "*":
        return da * db
    if db == 0:
        return None
    if op == "%":
        r = da % db
        return int(r) if isinstance(na, int) and isinstance(nb, int) else r
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 15
        q = da / db
    q = q.normalize()
    return Decimal(0) if q == 0 else q


def eval_func(node, row, group):
    name = node.name
    args = [evaluate(a, row, group) for a in node.args]
    if name in ("COALESCE", "IFNULL"):
        for v in args:
            if v is not None:
                return v
        return None
    if name == "NULLIF":
        return None if compare(args[0], args[1]) == 0 else args[0]
    if name == "CONCAT":
        return "".join(to_str(v) for v in args if v is not None)
    if args[0] is None:
        return None
    v = args[0]
    if name in ("UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM", "LENGTH", "LEN"):
        s = to_str(v)
        return {"UPPER": s.upper, "LOWER": s.lower, "TRIM": s.strip, "LTRIM": s.lstrip,
                "RTRIM": s.rstrip, "LENGTH": lambda: len(s), "LEN": lambda: len(s)}[name]()
    if name == "REPLACE":
        if args[1] is None or args[2] is None:
            return None
        return to_str(v).replace(to_str(args[1]), to_str(args[2]))
    if name in ("SUBSTR", "SUBSTRING"):
        s = to_str(v)
        if any(a is None for a in args[1:]):
            return None
        start = int(to_num(args[1]) or 0)
        begin = max(start - 1, 0)
        if len(args) > 2:
            ln = int(to_num(args[2]) or 0)
            end = start - 1 + ln
            return s[begin:max(end, begin)]
        return s[begin:]
    n = to_num(v)
    if n is None:
        raise QueryError("%s() requires a numeric value, found %r" % (name, v))
    if name == "ABS":
        return abs(n)
    if name == "FLOOR":
        import math
        return int(math.floor(Decimal(n)))
    if name in ("CEIL", "CEILING"):
        import math
        return int(math.ceil(Decimal(n)))
    if name == "ROUND":
        from decimal import ROUND_HALF_UP
        digits = 0
        if len(args) > 1:
            if args[1] is None:
                return None
            digits = int(to_num(args[1]) or 0)
        if isinstance(n, int) and digits >= 0:
            return n
        q = Decimal(n).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        return q
    raise QueryError("Unsupported function: %s" % name)


def eval_cast(v, typ):
    if v is None:
        return None
    base = typ.split()[0]
    if base in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"):
        n = to_num(v)
        if n is None:
            raise QueryError("Cannot cast %r to %s" % (v, typ))
        return int(n)
    if base in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
        n = to_num(v)
        if n is None:
            raise QueryError("Cannot cast %r to %s" % (v, typ))
        return Decimal(n)
    if base in ("TEXT", "VARCHAR", "CHAR", "STRING"):
        return to_str(v)
    raise QueryError("Unsupported CAST type: %s" % typ)


def eval_agg(node, group):
    if node.filter is not None:
        group = [r for r in group if truth(evaluate(node.filter, r)) is True]
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
    if node.func == "AVG":
        return avg_of(vals)
    if node.func == "MIN":
        return min(vals, key=sort_key)
    if node.func == "MAX":
        return max(vals, key=sort_key)
    raise QueryError("Unsupported aggregate %s" % node.func)


# ---------------------------------------------------------------------------
# Window functions
# ---------------------------------------------------------------------------

_CUR_CTX = [None]  # index of the context (row / group) currently being evaluated


def num_or_error(v, func):
    n = to_num(v)
    if n is None:
        raise QueryError("%s() requires numeric values, found %r" % (func, v))
    return Decimal(n)


def avg_value(total, count):
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 15
        q = Decimal(total) / Decimal(count)
    q = q.normalize()
    if q == 0:
        return Decimal(0)
    return q


def avg_of(vals):
    total = Decimal(0)
    for v in vals:
        total += num_or_error(v, "AVG")
    return avg_value(total, len(vals))


def window_sort(seq, keys, specs):
    """Stable sort of seq (context indices) by keys[i][j] using specs[j]=(desc, nulls_first)."""
    for j in range(len(specs) - 1, -1, -1):
        desc, nulls_first = specs[j]
        nonnull = [i for i in seq if keys[i][j] is not None]
        nulls = [i for i in seq if keys[i][j] is None]
        nonnull.sort(key=lambda i: sort_key(keys[i][j]), reverse=desc)
        seq = nulls + nonnull if nulls_first else nonnull + nulls
    return seq


def compute_window(w, ctxs, seq):
    """Compute w.values for all contexts; returns the new processing order."""
    n = len(ctxs)
    pvals = [[evaluate(p, r, g) for p in w.partition] for r, g in ctxs]
    ovals = [[evaluate(o.expr, r, g) for o in w.order] for r, g in ctxs]
    specs = [(False, True)] * len(w.partition)
    ospecs = [(o.desc, o.nulls == "FIRST") for o in w.order]
    keys = [pvals[i] + ovals[i] for i in range(n)]
    if specs or ospecs:
        seq = window_sort(list(seq), keys, specs + ospecs)
    # split into partitions
    parts = []
    prev = object()
    for i in seq:
        k = tuple(group_key(v) for v in pvals[i])
        if k != prev:
            parts.append([])
            prev = k
        parts[-1].append(i)
    values = [None] * n
    for part in parts:
        compute_partition(w, ctxs, part, ovals, values)
    w.values = values
    return seq


def peer_groups(part, ovals):
    """Return list peer_start, peer_end (inclusive) indexes within part for each position."""
    m = len(part)
    starts = [0] * m
    ends = [0] * m
    j = 0
    while j < m:
        k = tuple(group_key(v) for v in ovals[part[j]])
        e = j
        while e + 1 < m and tuple(group_key(v) for v in ovals[part[e + 1]]) == k:
            e += 1
        for x in range(j, e + 1):
            starts[x] = j
            ends[x] = e
        j = e + 1
    return starts, ends


def frame_bounds(w, part, ovals, starts, ends):
    """Return lists lo, hi (inclusive positions; hi < lo means empty frame)."""
    m = len(part)
    if w.frame is None:
        if w.order:
            return [0] * m, list(ends)
        return [0] * m, [m - 1] * m
    mode, start, end = w.frame
    lo = [0] * m
    hi = [0] * m
    if mode == "ROWS":
        for x in range(m):
            lo[x] = bound_rows(start, x, m)
            hi[x] = bound_rows(end, x, m)
        return lo, hi
    # RANGE
    offset = start[0] in ("P", "F") or end[0] in ("P", "F")
    if not offset:
        for x in range(m):
            lo[x] = 0 if start[0] == "UP" else starts[x]
            hi[x] = m - 1 if end[0] == "UF" else ends[x]
        return lo, hi
    o = w.order[0]
    vals = [ovals[i][0] for i in part]
    nums = []
    for v in vals:
        if v is None:
            nums.append(None)
        else:
            n = to_num(v)
            if n is None:
                raise WindowError("FRAME_ERROR", "RANGE offset frames require a numeric ORDER BY "
                                  "column, found %r" % (v,))
            nums.append(Decimal(n))
    sign = -1 if o.desc else 1
    for x in range(m):
        if nums[x] is None:
            lo[x] = 0 if start[0] == "UP" else starts[x]
            hi[x] = m - 1 if end[0] == "UF" else ends[x]
            continue
        cur = nums[x] * sign

        def pos(b, is_start):
            kind, off = b
            if kind == "UP":
                return 0
            if kind == "UF":
                return m - 1
            if kind == "C":
                return starts[x] if is_start else ends[x]
            target = cur - off if kind == "P" else cur + off
            if is_start:
                for y in range(m):
                    if nums[y] is not None and nums[y] * sign >= target:
                        return y
                return m
            r = -1
            for y in range(m):
                if nums[y] is not None and nums[y] * sign <= target:
                    r = y
            return r
        lo[x] = pos(start, True)
        hi[x] = pos(end, False)
    return lo, hi


def bound_rows(b, x, m):
    kind, off = b
    if kind == "UP":
        return 0
    if kind == "UF":
        return m - 1
    if kind == "C":
        return x
    if kind == "P":
        return x - off
    return x + off


def compute_partition(w, ctxs, part, ovals, values):
    m = len(part)
    func = w.func
    if func in RANK_FUNCS:
        starts, ends = peer_groups(part, ovals)
        dense = 0
        for x in range(m):
            if func == "ROW_NUMBER":
                values[part[x]] = x + 1
            elif func == "RANK":
                values[part[x]] = starts[x] + 1
            else:
                if starts[x] == x:
                    dense += 1
                values[part[x]] = dense
        return
    starts, ends = peer_groups(part, ovals)
    lo, hi = frame_bounds(w, part, ovals, starts, ends)
    lo = [max(0, v) for v in lo]
    hi = [min(m - 1, v) for v in hi]
    if func == "COUNT" and w.arg is None and w.filter is None:
        for x in range(m):
            values[part[x]] = max(0, hi[x] - lo[x] + 1)
        return
    if w.arg is None:
        args = [1] * m
    else:
        args = [evaluate(w.arg, ctxs[i][0], ctxs[i][1]) for i in part]
    if w.filter is not None:
        keep = [truth(evaluate(w.filter, ctxs[i][0], ctxs[i][1])) is True for i in part]
        args = [a if k else None for a, k in zip(args, keep)]
    if func in ("COUNT", "SUM", "AVG"):
        cnt = [0] * (m + 1)
        tot = [Decimal(0)] * (m + 1)
        for x in range(m):
            v = args[x]
            cnt[x + 1] = cnt[x] + (v is not None)
            if func != "COUNT" and v is not None:
                tot[x + 1] = tot[x] + num_or_error(v, func)
            else:
                tot[x + 1] = tot[x]
        for x in range(m):
            if hi[x] < lo[x]:
                c, t = 0, Decimal(0)
            else:
                c = cnt[hi[x] + 1] - cnt[lo[x]]
                t = tot[hi[x] + 1] - tot[lo[x]]
            if func == "COUNT":
                values[part[x]] = c
            elif c == 0:
                values[part[x]] = None
            elif func == "SUM":
                values[part[x]] = t
            else:
                values[part[x]] = avg_value(t, c)
        return
    # MIN / MAX
    better = (lambda a, b: sort_key(a) < sort_key(b)) if func == "MIN" else \
        (lambda a, b: sort_key(a) > sort_key(b))

    def combine(a, b):
        if a is None:
            return b
        if b is None:
            return a
        return b if better(b, a) else a
    if all(v == 0 for v in lo):
        pre = [None] * m
        acc = None
        for x in range(m):
            acc = combine(acc, args[x])
            pre[x] = acc
        for x in range(m):
            values[part[x]] = pre[hi[x]] if hi[x] >= 0 else None
        return
    if all(v == m - 1 for v in hi):
        suf = [None] * m
        acc = None
        for x in range(m - 1, -1, -1):
            acc = combine(acc, args[x])
            suf[x] = acc
        for x in range(m):
            values[part[x]] = suf[lo[x]] if lo[x] <= m - 1 else None
        return
    for x in range(m):
        acc = None
        for y in range(lo[x], hi[x] + 1):
            acc = combine(acc, args[y])
        values[part[x]] = acc


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

OUTER = []      # OUTER[level] = current row of the enclosing query at that level
_EPOCH = [0]    # bumped whenever CTE contents change; invalidates subquery caches


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


class Env:
    """Per-execution state: discovered tables and a cache of loaded data."""

    def __init__(self, tables, has_with=False):
        self.tables = tables
        self.has_with = has_with
        self.cache = {}

    def has_table(self, lname):
        try:
            resolve_table_name(self.tables, [lname])
            return True
        except QueryError:
            return False

    def load(self, name):
        if name not in self.cache:
            self.cache[name] = load_table(self.tables[name], name)
        return self.cache[name]


# -- subquery evaluation --------------------------------------------------------

def eval_subquery(sq, row, group):
    left = None
    if sq.kind in ("IN", "ANY", "ALL"):
        left = evaluate(sq.expr, row, group)
    lv = sq.level - 1
    saved = OUTER[lv:]
    del OUTER[lv:]
    while len(OUTER) < lv:
        OUTER.append(None)
    OUTER.append(row)
    try:
        if sq.epoch != _EPOCH[0]:
            sq.cache = {}
            sq.epoch = _EPOCH[0]
        key = []
        for l, i in sorted(sq.refs):
            orow = OUTER[l] if l < len(OUTER) else None
            key.append(group_key(None if orow is None else orow[i]))
        key = tuple(key)
        if key in sq.cache:
            res = sq.cache[key]
        else:
            rows = sq.plan.run()
            if sq.kind == "SCALAR":
                if len(rows) > 1:
                    raise SubqueryError("SCALAR_SUBQUERY_MULTIPLE_ROWS",
                                        "Scalar subquery returned %d rows (expected at most one)" % len(rows))
                res = rows[0][0] if rows else None
                if sq.float_avg and sq.refs and isinstance(res, Decimal):
                    res = FloatDec(res)
            elif sq.kind == "EXISTS":
                res = bool(rows)
            else:
                vals = [r[0] for r in rows]
                keys = set(eq_key(v) for v in vals if v is not None)
                res = (vals, keys, any(v is None for v in vals))
            sq.cache[key] = res
    finally:
        del OUTER[lv:]
        OUTER.extend(saved)
    if sq.kind == "SCALAR":
        return res
    if sq.kind == "EXISTS":
        return (not res) if sq.negate else res
    vals, keys, has_null = res
    if sq.kind == "IN":
        if not vals:
            out = False
        elif left is None:
            out = None
        elif eq_key(left) in keys:
            out = True
        else:
            out = True if any(compare(left, v) == 0 for v in vals if v is not None) else \
                (None if has_null else False)
        if sq.negate:
            return None if out is None else (not out)
        return out
    # ANY / ALL
    tmp = Cmp(sq.op, Lit(left), Lit(None))
    results = []
    for v in vals:
        tmp.right = Lit(v)
        results.append(evaluate(tmp, None))
    if sq.kind == "ANY":
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if any(r is False for r in results):
        return False
    return None if any(r is None for r in results) else True


# -- CTEs -------------------------------------------------------------------------

class CTE:
    def __init__(self, name):
        self.name = name
        self.names = None
        self.plan = None       # non-recursive body
        self.anchors = []      # recursive: anchor member plans
        self.recs = []         # recursive: recursive member plans
        self.union_all = True
        self.rows = None
        self.working = None
        self.computing = False

    def get_rows(self):
        if self.working is not None:
            return self.working
        if self.rows is None:
            if self.computing:
                raise CTEError("CYCLIC_REFERENCE", "CTE '%s' depends on itself" % self.name)
            self.computing = True
            try:
                self.rows = self.compute()
            finally:
                self.computing = False
        return self.rows

    def compute(self):
        if not self.recs:
            return self.plan.run()
        seen = set()

        def add(rows):
            if self.union_all:
                return list(rows)
            out = []
            for r in rows:
                k = tuple(group_key(v) for v in r)
                if k not in seen:
                    seen.add(k)
                    out.append(r)
            return out
        working = []
        for p in self.anchors:
            working += add(p.run())
        result = list(working)
        iterations = 0
        while working:
            if iterations >= MAX_RECURSION:
                raise CTEError("RECURSIVE_DEPTH_EXCEEDED",
                               "Recursive CTE '%s' exceeded the maximum recursion depth of %d"
                               % (self.name, MAX_RECURSION))
            iterations += 1
            self.working = working
            _EPOCH[0] += 1
            try:
                new = []
                for p in self.recs:
                    new += p.run()
            finally:
                self.working = None
                _EPOCH[0] += 1
            working = add(new)
            result += working
        return result


def union_terms(q):
    """Flatten a left-deep chain of UNION [ALL] into (terms, all_flags)."""
    if isinstance(q, SetQuery) and q.op == "UNION" and not q.order_by and q.limit is None \
            and q.offset is None:
        terms, flags = union_terms(q.left)
        return terms + [q.right], flags + [q.all]
    return [q], []


def plan_cte(d, ctx, recursive):
    lname = d.name.lower()
    obj = CTE(d.name)
    if recursive and lname in query_refs(d.query):
        terms, flags = union_terms(d.query)
        if not flags:
            raise CTEError("INVALID_CTE_SYNTAX",
                           "Recursive CTE '%s' must have the form <anchor> UNION [ALL] <recursive member>"
                           % d.name)
        anchors = [t for t in terms if lname not in query_refs(t)]
        recs = [t for t in terms if lname in query_refs(t)]
        if not anchors:
            raise CTEError("INVALID_CTE_SYNTAX", "Recursive CTE '%s' has no non-recursive anchor member"
                           % d.name)
        aplans = [plan_query(t, ctx) for t in anchors]
        ncols = len(aplans[0].names)
        if any(len(p.names) != ncols for p in aplans):
            raise CTEError("INVALID_CTE_SYNTAX", "Members of recursive CTE '%s' have different numbers "
                           "of columns" % d.name)
        names = d.columns or aplans[0].names
        if len(names) != ncols:
            raise CTEError("INVALID_CTE_SYNTAX", "CTE '%s' has %d columns but %d column names were "
                           "specified" % (d.name, ncols, len(names)))
        obj.names = list(names)
        ctes = dict(ctx.ctes)
        ctes[lname] = obj
        rctx = ctx.derive(ctes=ctes)
        rplans = [plan_query(t, rctx) for t in recs]
        if any(len(p.names) != ncols for p in rplans):
            raise CTEError("INVALID_CTE_SYNTAX", "Recursive member of CTE '%s' returns a different number "
                           "of columns than the anchor member" % d.name)
        obj.anchors, obj.recs = aplans, rplans
        obj.union_all = all(flags)
        return obj
    p = plan_query(d.query, ctx)
    names = d.columns or p.names
    if len(names) != len(p.names):
        raise CTEError("INVALID_CTE_SYNTAX", "CTE '%s' has %d columns but %d column names were specified"
                       % (d.name, len(p.names), len(names)))
    obj.plan = p
    obj.names = list(names)
    return obj


class WithPlan:
    def __init__(self, w, ctx):
        env = ctx.env
        defs = w.ctes
        lnames = [d.name.lower() for d in defs]
        index = {n: i for i, n in enumerate(lnames)}
        refs = [query_refs(d.query) & set(lnames) for d in defs]
        edges = [[index[r] for r in refs[i] if index[r] != i] for i in range(len(defs))]

        def reaches(src, target):
            stack, seen = [src], set()
            while stack:
                x = stack.pop()
                if x == target:
                    return True
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(edges[x])
            return False

        ctes = dict(ctx.ctes)
        self.ctes = []
        if not w.recursive:
            for i, d in enumerate(defs):
                if lnames[i] in refs[i] and not env.has_table(lnames[i]):
                    raise CTEError("CYCLIC_REFERENCE", "CTE '%s' references itself (use WITH RECURSIVE)"
                                   % d.name)
                for j in range(i + 1, len(defs)):
                    if lnames[j] in refs[i] and not env.has_table(lnames[j]):
                        if reaches(j, i):
                            raise CTEError("CYCLIC_REFERENCE", "CTEs '%s' and '%s' reference each other"
                                           % (d.name, defs[j].name))
                        raise CTEError("CTE_NOT_FOUND", "CTE '%s' is referenced by '%s' before it is defined"
                                       % (defs[j].name, d.name))
                obj = plan_cte(d, ctx.derive(ctes=ctes), False)
                ctes = dict(ctes)
                ctes[lnames[i]] = obj
                self.ctes.append(obj)
        else:
            # reject cycles between distinct CTEs, then plan in dependency order
            state = [0] * len(defs)
            order = []

            def visit(i, path):
                if state[i] == 2:
                    return
                if state[i] == 1:
                    cyc = path[path.index(i):] + [i]
                    raise CTEError("CYCLIC_REFERENCE", "Cyclic reference between CTEs: %s"
                                   % " -> ".join(defs[k].name for k in cyc))
                state[i] = 1
                for j in sorted(edges[i]):
                    visit(j, path + [i])
                state[i] = 2
                order.append(i)
            for i in range(len(defs)):
                visit(i, [])
            for i in order:
                obj = plan_cte(defs[i], ctx.derive(ctes=ctes), True)
                ctes = dict(ctes)
                ctes[lnames[i]] = obj
                self.ctes.append(obj)
        self.body = plan_query(w.body, ctx.derive(ctes=ctes))
        self.names = self.body.names

    def run(self):
        for c in self.ctes:
            c.rows = None
        _EPOCH[0] += 1
        return self.body.run()


# -- planning -----------------------------------------------------------------------

def plan_query(q, ctx):
    if isinstance(q, With):
        return WithPlan(q, ctx)
    if isinstance(q, SetQuery):
        return SetPlan(q, ctx)
    return SelectPlan(q, ctx)


def sort_results(results, specs):
    """Stable multi-key sort of (output_values, order_values) pairs.
    specs[i] = (desc, nulls) for order value i."""
    for i in range(len(specs) - 1, -1, -1):
        desc, nulls = specs[i]
        nulls_first = (not desc) if nulls is None else nulls == "FIRST"
        nonnull = [x for x in results if x[1][i] is not None]
        nulls_part = [x for x in results if x[1][i] is None]
        nonnull.sort(key=lambda x: sort_key(x[1][i]), reverse=desc)
        results = nulls_part + nonnull if nulls_first else nonnull + nulls_part
    return results


def dedupe(rows):
    seen = set()
    out = []
    for r in rows:
        k = tuple(group_key(v) for v in r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


class SetPlan:
    def __init__(self, q, ctx):
        self.q = q
        self.left = plan_query(q.left, ctx)
        self.right = plan_query(q.right, ctx) if q.right is not None else None
        self.names = list(self.left.names)
        if self.right is not None and len(self.right.names) != len(self.names):
            raise QueryError("Each %s query must have the same number of columns (%d vs %d)"
                             % (q.op, len(self.names), len(self.right.names)))
        self.order = []
        for oi in q.order_by:
            e = oi.expr
            idx = None
            if isinstance(e, Lit) and isinstance(e.value, int):
                if not 1 <= e.value <= len(self.names):
                    raise QueryError("ORDER BY position %d is out of range" % e.value)
                idx = e.value - 1
            elif isinstance(e, Col):
                name = e.parts[-1]
                hits = [i for i, n in enumerate(self.names) if n == name]
                if not hits:
                    hits = [i for i, n in enumerate(self.names) if n.lower() == name.lower()]
                if hits:
                    idx = hits[0]
            if idx is None:
                raise QueryError("ORDER BY term of a set operation must be an output column name "
                                 "or position")
            self.order.append((idx, oi.desc, oi.nulls))

    def run(self):
        q = self.q
        left = self.left.run()
        if self.right is None:
            rows = list(left)
        else:
            right = self.right.run()
            if q.op == "UNION":
                rows = left + right if q.all else dedupe(left + right)
            else:
                from collections import Counter
                rkeys = Counter(tuple(group_key(v) for v in r) for r in right)
                rows = []
                for r in left:
                    k = tuple(group_key(v) for v in r)
                    if q.op == "INTERSECT":
                        if rkeys[k] > 0:
                            rows.append(r)
                            if q.all:
                                rkeys[k] -= 1
                    else:
                        if rkeys[k] > 0:
                            if q.all:
                                rkeys[k] -= 1
                        else:
                            rows.append(r)
                if not q.all:
                    rows = dedupe(rows)
        if self.order:
            res = sort_results([(r, [r[i] for i, _, _ in self.order]) for r in rows],
                               [(d, n) for _, d, n in self.order])
            rows = [r[0] for r in res]
        if q.offset:
            rows = rows[q.offset:]
        if q.limit is not None:
            rows = rows[:q.limit]
        return rows


class SelectPlan:
    def __init__(self, q, ctx):
        self.q = q
        self.ctx = ctx
        scope = Scope()
        self.sources = []   # (fetch, width, join kind, on)
        if q.table is None:
            self.sources.append((lambda: [[]], 0, None, None))
        else:
            fetch, name, alias, cols, derived = self.plan_from(q.table)
            scope.add(name, alias, cols, derived)
            self.sources.append((fetch, len(cols), None, None))
            for j in q.joins:
                fetch, name, alias, cols, derived = self.plan_from(j.table)
                scope.add(name, alias, cols, derived)
                on = Resolver(ctx, scope.copy()).resolve(j.on) if j.on is not None else None
                self.sources.append((fetch, len(cols), j.kind, on))
        rs = Resolver(ctx, scope)
        self.scope = scope

        # Expand select list
        items = []
        for it in q.select:
            if isinstance(it.expr, Star):
                if it.expr.qualifier is None:
                    tlist = scope.tables
                    if q.table is None:
                        raise QueryError("SELECT * requires a FROM clause")
                else:
                    t = scope.find_table(it.expr.qualifier)
                    if t is None:
                        if ctx.in_sub or scope.has_derived:
                            raise SubqueryError("INVALID_SUBQUERY_REFERENCE",
                                                "Unknown table or alias '%s'" % it.expr.qualifier)
                        raise QueryError("Unknown table or alias '%s'" % it.expr.qualifier)
                    tlist = [t]
                for t in tlist:
                    for ci, cname in enumerate(t["columns"]):
                        c = Col([cname])
                        c.idx = t["offset"] + ci
                        items.append(SelectItem(c, None, cname))
            else:
                it.expr = rs.resolve(it.expr)
                items.append(it)
        self.items = items
        self.names = [output_name(it) for it in items]
        aliases = {}
        for it in items:
            if it.alias is not None:
                aliases.setdefault(it.alias, it.expr)

        self.where = rs.resolve(q.where) if q.where is not None else None
        group_by = [rs.resolve(g, aliases) for g in q.group_by]
        for g in group_by:
            if contains_agg(g):
                raise QueryError("Aggregate functions are not allowed in GROUP BY")
        self.group_by = group_by
        self.having = rs.resolve(q.having, aliases) if q.having is not None else None

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
                target = ("expr", rs.resolve(e, aliases))
            order.append((target, oi.desc, oi.nulls))
        self.order = order

        self.is_agg = bool(group_by) or self.having is not None or \
            any(contains_agg(it.expr) for it in items) or \
            any(t[0] == "expr" and contains_agg(t[1]) for t, _, _ in order)
        if self.is_agg:
            grouped_idx = set()
            for g in group_by:
                if isinstance(g, Col) and not g.outer:
                    grouped_idx.add(g.idx)

            def validate(node, where_txt):
                if isinstance(node, Agg):
                    return
                for gexpr in group_by:
                    if same_expr(node, gexpr):
                        return
                if isinstance(node, Col):
                    if not node.outer and node.idx not in grouped_idx:
                        raise QueryError("Column '%s' must appear in the GROUP BY clause or be used in an "
                                         "aggregate function (%s)" % (".".join(node.parts), where_txt))
                    return
                for c in children(node):
                    validate(c, where_txt)

            for it in items:
                validate(it.expr, "SELECT")
            if self.having is not None:
                validate(self.having, "HAVING")
            for t, _, _ in order:
                if t[0] == "expr":
                    validate(t[1], "ORDER BY")

        windows = []
        seen_w = set()
        exprs = [it.expr for it in items] + [t[1] for t, _, _ in order if t[0] == "expr"]
        for e in exprs:
            for nd in walk(e):
                if isinstance(nd, Window) and id(nd) not in seen_w:
                    seen_w.add(id(nd))
                    windows.append(nd)
        self.windows = windows

    def plan_from(self, tref):
        ctx = self.ctx
        if isinstance(tref, DerivedRef):
            p = plan_query(tref.query, ctx.derive(in_sub=True))
            cols = tref.columns or p.names
            if len(cols) != len(p.names):
                raise SubqueryError("INVALID_SUBQUERY_REFERENCE",
                                    "Derived table '%s' has %d columns but %d column names were specified"
                                    % (tref.alias, len(p.names), len(cols)))
            return p.run, tref.alias, tref.alias, list(cols), True
        if len(tref.name) == 1 and tref.name[0].lower() in ctx.ctes:
            c = ctx.ctes[tref.name[0].lower()]
            return c.get_rows, c.name, tref.alias, c.names, False
        env = ctx.env
        try:
            name = resolve_table_name(env.tables, tref.name)
        except QueryError:
            if env.has_with:
                raise CTEError("CTE_NOT_FOUND", "Table or CTE '%s' not found" % ".".join(tref.name))
            raise
        header, _ = env.load(name)
        return (lambda: env.load(name)[1]), name, tref.alias, header, False

    def run(self):
        saved = _CUR_CTX[0]
        try:
            return self._run()
        finally:
            _CUR_CTX[0] = saved

    def _run(self):
        q = self.q
        fetch, width, _, _ = self.sources[0]
        rows = fetch()
        for fetch, w, kind, on in self.sources[1:]:
            rows = do_join(rows, width, fetch(), w, kind, on)
            width += w
        if self.where is not None:
            where = self.where
            rows = [r for r in rows if truth(evaluate(where, r)) is True]
        items, order, group_by, having = self.items, self.order, self.group_by, self.having

        if self.is_agg:
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
            ctxs = []
            for grp in glist:
                rep = grp[0] if grp else None
                if having is not None and truth(evaluate(having, rep, grp)) is not True:
                    continue
                ctxs.append((rep, grp))
        else:
            ctxs = [(r, None) for r in rows]

        seq = list(range(len(ctxs)))
        for w in self.windows:
            seq = compute_window(w, ctxs, seq)
        results = []
        for ci in seq:
            rep, grp = ctxs[ci]
            _CUR_CTX[0] = ci
            outv = [evaluate(it.expr, rep, grp) for it in items]
            ordv = [outv[t[1]] if t[0] == "out" else evaluate(t[1], rep, grp) for t, _, _ in order]
            results.append((outv, ordv))
        _CUR_CTX[0] = None

        results = sort_results(results, [(d, n) for _, d, n in order])
        out_rows = [r[0] for r in results]
        if q.distinct:
            out_rows = dedupe(out_rows)
        if q.offset:
            out_rows = out_rows[q.offset:]
        if q.limit is not None:
            out_rows = out_rows[:q.limit]
        return out_rows


def simple_equi(on, left_scope_width):
    """If ON is col = col across both sides, return (left_idx, right_idx)."""
    if isinstance(on, Cmp) and on.op == "=" and isinstance(on.left, Col) and isinstance(on.right, Col) \
            and not on.left.outer and not on.right.outer:
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


def execute(q, data_dir, sharded=None, has_with=False):
    tables = discover_tables(data_dir) if data_dir else {}
    for sname, pattern in (sharded or {}).items():
        # sharded tables take precedence over discovered tables of the same name
        for t in [t for t in tables if t.lower() == sname.lower()]:
            del tables[t]
        tables[sname] = expand_shards(sname, pattern, data_dir)
    del OUTER[:]
    env = Env(tables, has_with)
    plan = plan_query(q, Ctx(env))
    rows = plan.run()
    return plan.names, rows


def same_expr(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, Col):
        return a.idx == b.idx and a.level == b.level
    if isinstance(a, Lit):
        return a.value == b.value and type(a.value) is type(b.value)
    if isinstance(a, SubqueryExpr):
        return a is b
    if isinstance(a, Agg):
        if a.func != b.func or a.distinct != b.distinct:
            return False
    if isinstance(a, Window):
        return a is b
    if isinstance(a, (Cmp, BoolOp, Arith)) and a.op != b.op:
        return False
    if isinstance(a, Func) and a.name != b.name:
        return False
    if isinstance(a, Cast) and a.typ != b.typ:
        return False
    if isinstance(a, (Like, IsNull, InList, Between)) and a.negate != b.negate:
        return False
    if isinstance(a, Case) and ((a.operand is None) != (b.operand is None)
                                or (a.default is None) != (b.default is None)):
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
        parser = Parser(sql)
        q = parser.parse()
        names, rows = execute(q, args.data, sharded, parser.saw_with)
        print(to_markdown(names, rows))
        if args.out:
            write_csv(args.out, names, rows)
    except QueryError as e:
        print("Error: %s" % e, file=sys.stderr)
        return e.exit_code
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
