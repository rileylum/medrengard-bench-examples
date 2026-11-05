#!/usr/bin/env python3
"""Run read-only SQL queries over a directory of data files
(CSV, TSV, Parquet, JSON, JSONL; optionally .gz/.bz2 compressed).

Usage:
    python query_files.py --data <dir> --sql <query or path> [--out <file.csv>]
"""
import argparse
import bz2
import csv
import gzip
import io
import json
import functools
import os
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext


class QueryError(Exception):
    pass


class WindowError(QueryError):
    """Errors related to window functions (exit code 3)."""
    group = 'WINDOW_ERROR'
    code = 3

    def __init__(self, etype, msg):
        self.etype = etype
        self.msg = msg
        super().__init__('%s: %s: %s' % (self.group, etype, msg))


# --------------------------------------------------------------------------
# Values / typing helpers
# --------------------------------------------------------------------------

INT_RE = re.compile(r'^[+-]?(0|[1-9]\d*)$')
DEC_RE = re.compile(r'^[+-]?((0|[1-9]\d*)(\.\d+)?|\.\d+)$')


def is_num(v):
    return isinstance(v, (int, Decimal)) and not isinstance(v, bool)


def to_number(s):
    """Parse a string to int/Decimal, or return None if not numeric."""
    if is_num(s):
        return s
    if not isinstance(s, str):
        return None
    t = s.strip()
    if INT_RE.match(t):
        return int(t)
    if DEC_RE.match(t):
        try:
            return Decimal(t)
        except InvalidOperation:
            return None
    return None


def cmp_values(a, b):
    """Compare two non-null values; returns -1, 0, 1."""
    if is_num(a) and is_num(b):
        return (a > b) - (a < b)
    if is_num(a) and isinstance(b, str):
        nb = to_number(b)
        if nb is not None:
            return (a > nb) - (a < nb)
        return -1  # numbers sort before strings
    if isinstance(a, str) and is_num(b):
        na = to_number(a)
        if na is not None:
            return (na > b) - (na < b)
        return 1
    sa, sb = str(a), str(b)
    return (sa > sb) - (sa < sb)


def hash_key(v):
    """Normalise a value for hash-based equality (joins, grouping)."""
    if isinstance(v, str):
        n = to_number(v)
        if n is not None:
            return n
    return v


def fmt_value(v, null='NULL'):
    if v is None:
        return null
    if isinstance(v, Decimal):
        return format(v, 'f')
    return str(v)


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

class Tok:
    __slots__ = ('kind', 'value', 'start', 'end')

    def __init__(self, kind, value, start, end):
        self.kind, self.value, self.start, self.end = kind, value, start, end

    def __repr__(self):
        return 'Tok(%s,%r)' % (self.kind, self.value)


def tokenize(sql):
    toks = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith('--', i):
            j = sql.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith('/*', i):
            j = sql.find('*/', i + 2)
            if j < 0:
                raise QueryError('Unterminated comment')
            i = j + 2
            continue
        if c == "'":
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise QueryError('Unterminated string literal')
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            toks.append(Tok('STRING', ''.join(buf), i, j + 1))
            i = j + 1
            continue
        if c in '"`[':
            close = {'"': '"', '`': '`', '[': ']'}[c]
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise QueryError('Unterminated quoted identifier')
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close and close != ']':
                        buf.append(close)
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            toks.append(Tok('QIDENT', ''.join(buf), i, j + 1))
            i = j + 1
            continue
        if c.isdigit() or (c == '.' and i + 1 < n and sql[i + 1].isdigit()
                           and (not toks or toks[-1].kind not in ('IDENT', 'QIDENT'))):
            m = re.compile(r'(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?').match(sql, i)
            text = m.group(0)
            j = m.end()
            if j < n and (sql[j].isalpha() or sql[j] == '_'):
                # identifier starting with digits (e.g. 2020_sales)
                m2 = re.compile(r'[A-Za-z0-9_$]+').match(sql, i)
                toks.append(Tok('IDENT', m2.group(0), i, m2.end()))
                i = m2.end()
                continue
            if INT_RE.match(text) or re.match(r'^\d+$', text):
                val = int(text)
            else:
                val = Decimal(text)
            toks.append(Tok('NUMBER', val, i, j))
            i = j
            continue
        if c.isalpha() or c == '_':
            m = re.compile(r'[A-Za-z0-9_$]+', re.UNICODE).match(sql, i)
            j = m.end()
            while j < n and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            toks.append(Tok('IDENT', sql[i:j], i, j))
            i = j
            continue
        two = sql[i:i + 2]
        if two in ('!=', '<>', '<=', '>=', '=='):
            toks.append(Tok('OP', {'<>': '!=', '==': '='}.get(two, two), i, i + 2))
            i += 2
            continue
        if c in '=<>(),.*;+-':
            toks.append(Tok('OP', c, i, i + 1))
            i += 1
            continue
        raise QueryError('Unexpected character %r at position %d' % (c, i))
    toks.append(Tok('EOF', None, n, n))
    return toks


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

RESERVED = {
    'SELECT', 'FROM', 'WHERE', 'GROUP', 'BY', 'HAVING', 'ORDER', 'LIMIT',
    'OFFSET', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'OUTER', 'ON', 'AS',
    'AND', 'OR', 'NOT', 'LIKE', 'IS', 'NULL', 'ASC', 'DESC', 'DISTINCT',
    'CROSS', 'IN', 'UNION', 'ILIKE',
}
AGGREGATES = {'COUNT', 'SUM', 'AVG', 'MIN', 'MAX'}
RANKING = {'ROW_NUMBER', 'RANK', 'DENSE_RANK'}
INVALID_SPEC = 'INVALID_WINDOW_SPEC'
FRAME_ERROR = 'FRAME_ERROR'
NESTED_ERROR = 'NESTED_WINDOW_ERROR'


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.toks = tokenize(sql)
        self.pos = 0

    # helpers
    def peek(self, k=0):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def is_kw(self, kw, k=0):
        t = self.peek(k)
        return t.kind == 'IDENT' and t.value.upper() == kw

    def accept_kw(self, *kws):
        if self.peek().kind == 'IDENT' and self.peek().value.upper() in kws:
            return self.next().value.upper()
        return None

    def expect_kw(self, kw):
        if not self.accept_kw(kw):
            raise QueryError('Expected %s near %s' % (kw, self.where()))

    def is_op(self, op, k=0):
        t = self.peek(k)
        return t.kind == 'OP' and t.value == op

    def accept_op(self, op):
        if self.is_op(op):
            return self.next()
        return None

    def expect_op(self, op):
        if not self.accept_op(op):
            raise QueryError("Expected '%s' near %s" % (op, self.where()))

    def where(self):
        t = self.peek()
        if t.kind == 'EOF':
            return 'end of query'
        return repr(self.sql[t.start:t.start + 30])

    def ident(self):
        t = self.peek()
        if t.kind == 'QIDENT':
            self.next()
            return t.value
        if t.kind == 'IDENT' and t.value.upper() not in RESERVED:
            self.next()
            return t.value
        raise QueryError('Expected identifier near %s' % self.where())

    # grammar
    def parse(self):
        q = self.parse_select()
        while self.accept_op(';'):
            pass
        if self.peek().kind != 'EOF':
            raise QueryError('Unexpected input near %s' % self.where())
        return q

    def parse_select(self):
        self.expect_kw('SELECT')
        q = {'distinct': False, 'items': [], 'from': None, 'joins': [],
             'where': None, 'group_by': [], 'having': None, 'order_by': [],
             'limit': None, 'offset': None}
        if self.accept_kw('DISTINCT'):
            q['distinct'] = True
        else:
            self.accept_kw('ALL')
        while True:
            q['items'].append(self.parse_select_item())
            if not self.accept_op(','):
                break
        self.expect_kw('FROM')
        q['from'] = self.parse_table_ref()
        while True:
            jt = None
            if self.accept_kw('INNER'):
                jt = 'INNER'
            elif self.is_kw('LEFT') or self.is_kw('RIGHT') or self.is_kw('FULL'):
                jt = self.next().value.upper()
                self.accept_kw('OUTER')
            elif self.is_kw('CROSS'):
                self.next()
                jt = 'CROSS'
            elif self.is_kw('JOIN'):
                jt = 'INNER'
            if jt is None:
                break
            self.expect_kw('JOIN')
            tref = self.parse_table_ref()
            on = None
            if jt != 'CROSS':
                self.expect_kw('ON')
                on = self.parse_expr()
            q['joins'].append((jt, tref, on))
        if self.accept_kw('WHERE'):
            q['where'] = self.parse_expr()
        if self.accept_kw('GROUP'):
            self.expect_kw('BY')
            while True:
                q['group_by'].append(self.parse_expr())
                if not self.accept_op(','):
                    break
        if self.accept_kw('HAVING'):
            q['having'] = self.parse_expr()
        if self.accept_kw('ORDER'):
            self.expect_kw('BY')
            while True:
                e = self.parse_expr()
                desc = False
                d = self.accept_kw('ASC', 'DESC')
                if d == 'DESC':
                    desc = True
                nulls_first = None
                if self.accept_kw('NULLS'):
                    w = self.accept_kw('FIRST', 'LAST')
                    if not w:
                        raise QueryError('Expected FIRST or LAST after NULLS')
                    nulls_first = (w == 'FIRST')
                q['order_by'].append((e, desc, nulls_first))
                if not self.accept_op(','):
                    break
        for _ in range(2):
            if q['limit'] is None and self.accept_kw('LIMIT'):
                q['limit'] = self.parse_nonneg_int('LIMIT')
            elif q['offset'] is None and self.accept_kw('OFFSET'):
                q['offset'] = self.parse_nonneg_int('OFFSET')
                self.accept_kw('ROWS') or self.accept_kw('ROW')
        return q

    def parse_nonneg_int(self, what):
        t = self.next()
        if t.kind != 'NUMBER' or not isinstance(t.value, int) or t.value < 0:
            raise QueryError('%s requires a non-negative integer' % what)
        return t.value

    def parse_select_item(self):
        start = self.peek().start
        if self.is_op('*'):
            self.next()
            return {'star': True, 'qual': None}
        # qualified star: a.b.*
        k = 0
        parts = []
        while self.peek(k).kind in ('IDENT', 'QIDENT') and self.is_op('.', k + 1):
            parts.append(self.peek(k).value)
            k += 2
        if parts and self.is_op('*', k):
            self.pos += k + 1
            return {'star': True, 'qual': '.'.join(parts)}
        e = self.parse_expr()
        end = self.toks[self.pos - 1].end
        alias = None
        if self.accept_kw('AS'):
            t = self.peek()
            if t.kind == 'STRING':
                self.next()
                alias = t.value
            else:
                alias = self.ident()
        elif self.peek().kind == 'QIDENT' or (
                self.peek().kind == 'IDENT' and self.peek().value.upper() not in RESERVED):
            alias = self.ident()
        return {'star': False, 'expr': e, 'alias': alias,
                'text': self.sql[start:end]}

    def parse_table_ref(self):
        parts = [self.ident()]
        while self.accept_op('.'):
            parts.append(self.ident())
        alias = None
        if self.accept_kw('AS'):
            alias = self.ident()
        elif self.peek().kind == 'QIDENT' or (
                self.peek().kind == 'IDENT' and self.peek().value.upper() not in RESERVED):
            alias = self.ident()
        return {'name': '.'.join(parts), 'alias': alias}

    # expressions
    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.accept_kw('OR'):
            left = ('or', left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.accept_kw('AND'):
            left = ('and', left, self.parse_not())
        return left

    def parse_not(self):
        if self.accept_kw('NOT'):
            return ('not', self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_primary()
        t = self.peek()
        if t.kind == 'OP' and t.value in ('=', '!=', '<', '<=', '>', '>='):
            self.next()
            return ('cmp', t.value, left, self.parse_primary())
        neg = False
        if self.is_kw('NOT') and (self.is_kw('LIKE', 1) or self.is_kw('ILIKE', 1)
                                  or self.is_kw('IN', 1)):
            self.next()
            neg = True
        kw = self.accept_kw('LIKE', 'ILIKE')
        if kw:
            return ('like', left, self.parse_primary(), neg, kw == 'ILIKE')
        if self.accept_kw('IN'):
            self.expect_op('(')
            items = [self.parse_primary()]
            while self.accept_op(','):
                items.append(self.parse_primary())
            self.expect_op(')')
            return ('in', left, items, neg)
        if self.accept_kw('IS'):
            n = bool(self.accept_kw('NOT'))
            self.expect_kw('NULL')
            return ('isnull', left, n)
        return left

    def parse_primary(self):
        t = self.peek()
        if t.kind == 'OP' and t.value == '(':
            self.next()
            e = self.parse_expr()
            self.expect_op(')')
            return e
        if t.kind == 'OP' and t.value in ('-', '+'):
            self.next()
            n = self.next()
            if n.kind != 'NUMBER':
                raise QueryError('Expected number after sign')
            return ('lit', -n.value if t.value == '-' else n.value)
        if t.kind == 'NUMBER':
            self.next()
            return ('lit', t.value)
        if t.kind == 'STRING':
            self.next()
            return ('lit', t.value)
        if t.kind == 'IDENT' and t.value.upper() == 'NULL':
            self.next()
            return ('lit', None)
        if t.kind == 'IDENT' and t.value.upper() in ('TRUE', 'FALSE') \
                and not self.is_op('.', 1):
            self.next()
            return ('lit', t.value.upper() == 'TRUE')
        if t.kind == 'IDENT' and self.is_op('(', 1):
            name = t.value.upper()
            if name in RANKING:
                self.next()
                self.next()
                if not self.accept_op(')'):
                    raise WindowError(INVALID_SPEC, '%s() takes no arguments' % name)
                if not self.accept_kw('OVER'):
                    raise WindowError(INVALID_SPEC, '%s() requires an OVER clause' % name)
                spec = self.parse_window_spec()
                return ('win', name, None, False) + spec
            if name not in AGGREGATES:
                if self.followed_by_over(self.pos + 1):
                    raise WindowError(INVALID_SPEC,
                                      'Unsupported window function: %s' % t.value)
                raise QueryError('Unsupported function: %s' % t.value)
            self.next()
            self.next()
            distinct = bool(self.accept_kw('DISTINCT'))
            if self.is_op('*'):
                if name != 'COUNT' or distinct:
                    raise QueryError('%s(*) is not supported' % name)
                self.next()
                arg = '*'
            else:
                arg = self.parse_expr()
            self.expect_op(')')
            if self.accept_kw('OVER'):
                spec = self.parse_window_spec()
                return ('win', name, arg, distinct) + spec
            return ('agg', name, arg, distinct)
        if t.kind in ('IDENT', 'QIDENT'):
            parts = [self.ident()]
            while self.is_op('.') and self.peek(1).kind in ('IDENT', 'QIDENT'):
                self.next()
                parts.append(self.next().value)
            return ('col', parts)
        raise QueryError('Unexpected token near %s' % self.where())

    # window functions
    def followed_by_over(self, open_pos):
        """Is the parenthesised group starting at open_pos followed by OVER?"""
        depth = 0
        i = open_pos
        while i < len(self.toks):
            t = self.toks[i]
            if t.kind == 'EOF':
                return False
            if t.kind == 'OP' and t.value == '(':
                depth += 1
            elif t.kind == 'OP' and t.value == ')':
                depth -= 1
                if depth == 0:
                    n = self.toks[i + 1]
                    return n.kind == 'IDENT' and n.value.upper() == 'OVER'
            i += 1
        return False

    def window_expr(self):
        try:
            return self.parse_expr()
        except WindowError:
            raise
        except QueryError as e:
            raise WindowError(INVALID_SPEC, 'Invalid expression in window '
                              'specification: %s' % e)

    def parse_window_spec(self):
        if not self.accept_op('('):
            if self.peek().kind in ('IDENT', 'QIDENT'):
                raise WindowError(INVALID_SPEC, 'Named windows are not supported')
            raise WindowError(INVALID_SPEC, "Expected '(' after OVER near %s"
                              % self.where())
        partition = []
        order = []
        frame = None
        if self.accept_kw('PARTITION'):
            if not self.accept_kw('BY'):
                raise WindowError(INVALID_SPEC, 'Expected BY after PARTITION')
            while True:
                partition.append(self.window_expr())
                if not self.accept_op(','):
                    break
        if self.accept_kw('ORDER'):
            if not self.accept_kw('BY'):
                raise WindowError(INVALID_SPEC, 'Expected BY after ORDER')
            while True:
                e = self.window_expr()
                desc = self.accept_kw('ASC', 'DESC') == 'DESC'
                nf = None
                if self.accept_kw('NULLS'):
                    w = self.accept_kw('FIRST', 'LAST')
                    if not w:
                        raise WindowError(INVALID_SPEC, 'Expected FIRST or LAST after NULLS')
                    nf = (w == 'FIRST')
                order.append((e, desc, nf))
                if not self.accept_op(','):
                    break
        if self.is_kw('ROWS') or self.is_kw('RANGE') or self.is_kw('GROUPS'):
            frame = self.parse_frame(len(order))
        if not self.accept_op(')'):
            if self.is_kw('PARTITION') or self.is_kw('ORDER'):
                raise WindowError(INVALID_SPEC, 'Misplaced %s in window specification'
                                  % self.peek().value.upper())
            raise WindowError(INVALID_SPEC, 'Unexpected input in window specification '
                              'near %s' % self.where())
        return (tuple(partition), tuple(order), frame)

    BOUND_RANK = {'UP': 0, 'P': 1, 'C': 2, 'F': 3, 'UF': 4}

    def parse_frame(self, n_order):
        unit = self.next().value.upper()
        if self.accept_kw('BETWEEN'):
            start = self.parse_bound(unit)
            if not self.accept_kw('AND'):
                raise WindowError(FRAME_ERROR, 'Expected AND in frame clause near %s'
                                  % self.where())
            end = self.parse_bound(unit)
        else:
            start = self.parse_bound(unit)
            end = ('C', None)
        if self.is_kw('EXCLUDE'):
            raise WindowError(FRAME_ERROR, 'EXCLUDE is not supported in frame clauses')
        if start[0] == 'UF':
            raise WindowError(FRAME_ERROR, 'Frame start cannot be UNBOUNDED FOLLOWING')
        if end[0] == 'UP':
            raise WindowError(FRAME_ERROR, 'Frame end cannot be UNBOUNDED PRECEDING')
        if self.BOUND_RANK[start[0]] > self.BOUND_RANK[end[0]]:
            raise WindowError(FRAME_ERROR, 'Frame start cannot be after frame end')
        if unit == 'RANGE' and (start[0] in ('P', 'F') or end[0] in ('P', 'F')) \
                and n_order != 1:
            raise WindowError(FRAME_ERROR, 'RANGE with offset PRECEDING/FOLLOWING '
                              'requires exactly one ORDER BY expression')
        return (unit, start, end)

    def parse_bound(self, unit):
        if self.accept_kw('UNBOUNDED'):
            w = self.accept_kw('PRECEDING', 'FOLLOWING')
            if not w:
                raise WindowError(FRAME_ERROR, 'Expected PRECEDING or FOLLOWING after '
                                  'UNBOUNDED')
            return ('UP' if w == 'PRECEDING' else 'UF', None)
        if self.accept_kw('CURRENT'):
            if not self.accept_kw('ROW'):
                raise WindowError(FRAME_ERROR, 'Expected ROW after CURRENT')
            return ('C', None)
        t = self.peek()
        neg = False
        if t.kind == 'OP' and t.value in ('-', '+'):
            neg = t.value == '-'
            self.next()
            t = self.peek()
        if t.kind == 'NUMBER':
            self.next()
            n = t.value
            if neg and n != 0:
                raise WindowError(FRAME_ERROR, 'Frame offset must be non-negative')
            if unit in ('ROWS', 'GROUPS') and not isinstance(n, int):
                raise WindowError(FRAME_ERROR, 'Frame offset for %s must be a '
                                  'non-negative integer' % unit)
            w = self.accept_kw('PRECEDING', 'FOLLOWING')
            if not w:
                raise WindowError(FRAME_ERROR, 'Expected PRECEDING or FOLLOWING after '
                                  'frame offset')
            return ('P' if w == 'PRECEDING' else 'F', n)
        if t.kind == 'STRING' or (t.kind == 'IDENT' and t.value.upper() == 'NULL'):
            raise WindowError(FRAME_ERROR, 'Frame offset must be a non-negative number')
        raise WindowError(FRAME_ERROR, 'Invalid frame bound near %s' % self.where())


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def _native(v):
    """Normalise a natively-typed value (from JSON/Parquet) for the engine."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            return None if v != v else str(v)
        return Decimal(repr(v))
    if isinstance(v, Decimal):
        if not v.is_finite():
            return None if v.is_nan() else str(v)
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except UnicodeDecodeError:
            return v.hex()
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, default=str, ensure_ascii=False)
    if hasattr(v, 'isoformat'):
        return v.isoformat(sep=' ') if hasattr(v, 'hour') and hasattr(v, 'day') else v.isoformat()
    return str(v)


def infer_column(values):
    """Convert a list of raw values (None = null) to typed values.

    Strings are parsed as numbers when the whole column is numeric; already
    typed numbers (int/Decimal) participate in the inference."""
    kind = 'int'
    for v in values:
        if v is None:
            continue
        if isinstance(v, int):
            continue
        if isinstance(v, Decimal):
            if kind == 'int':
                kind = 'dec'
            continue
        if kind == 'int' and not INT_RE.match(v):
            kind = 'dec'
        if kind == 'dec' and not DEC_RE.match(v):
            kind = 'str'
            break
    if kind == 'int':
        return [None if v is None else int(v) for v in values]
    if kind == 'dec':
        return [None if v is None else (v if isinstance(v, Decimal) else Decimal(v))
                for v in values]
    return [v if v is None or isinstance(v, str) else fmt_value(v) for v in values]


FORMATS = ('.csv', '.tsv', '.parquet', '.jsonl', '.ndjson', '.json')
COMPRESSIONS = ('.gz', '.bz2')


def split_data_name(fn):
    """Return (stem, format, compression) or None if not a data file."""
    low = fn.lower()
    comp = None
    for c in COMPRESSIONS:
        if low.endswith(c):
            comp = c
            low = low[:-len(c)]
            fn = fn[:-len(c)]
            break
    for f in FORMATS:
        if low.endswith(f) and len(low) > len(f):
            return fn[:-len(f)], f, comp
    return None


def open_binary(path, comp):
    if comp == '.gz':
        return gzip.open(path, 'rb')
    if comp == '.bz2':
        return bz2.open(path, 'rb')
    return open(path, 'rb')


def read_text(path, comp):
    with open_binary(path, comp) as f:
        data = f.read()
    return data.decode('utf-8-sig')


def load_delimited(path, comp, delim):
    text = read_text(path, comp)
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delim)
    header = None
    for row in reader:
        if row and any(c.strip() for c in row):
            header = row
            break
    if header is None:
        raise QueryError('Invalid file (no header): %s' % path)
    header = [h.strip() for h in header]
    if any(h == '' for h in header):
        raise QueryError('Invalid header in %s' % path)
    width = len(header)
    raw = []
    for row in reader:
        if not row:
            continue
        if len(row) < width:
            row = row + [''] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        raw.append([None if c == '' else c for c in row])
    return header, raw


def records_to_table(records, path):
    header = []
    seen = set()
    for r in records:
        if not isinstance(r, dict):
            raise QueryError('Unsupported JSON structure in %s' % path)
        for k in r:
            if k not in seen:
                seen.add(k)
                header.append(k)
    raw = [[_native(r.get(k)) for k in header] for r in records]
    return header, raw


def json_to_table(obj, path):
    if isinstance(obj, list):
        if obj and all(isinstance(r, list) for r in obj):
            # list of rows: first row is the header
            header = [str(h) for h in obj[0]]
            raw = []
            for r in obj[1:]:
                r = list(r[:len(header)]) + [None] * (len(header) - len(r))
                raw.append([_native(v) for v in r])
            return header, raw
        return records_to_table(obj, path)
    if isinstance(obj, dict):
        keys = set(obj)
        if 'columns' in keys and 'data' in keys and isinstance(obj['data'], list) \
                and isinstance(obj['columns'], list):
            header = [str(c) for c in obj['columns']]
            raw = []
            for r in obj['data']:
                if isinstance(r, dict):
                    raw.append([_native(r.get(k)) for k in header])
                else:
                    r = list(r[:len(header)]) + [None] * (len(header) - len(r))
                    raw.append([_native(v) for v in r])
            return header, raw
        if 'schema' in keys and 'data' in keys and isinstance(obj['data'], list):
            header, raw = records_to_table(obj['data'], path)
            fields = obj['schema'].get('fields') if isinstance(obj['schema'], dict) else None
            if fields:
                names = [f.get('name') for f in fields if isinstance(f, dict)]
                idx_names = obj['schema'].get('primaryKey') or []
                order = [n for n in names if n in header and n not in idx_names
                         and n != 'index'] or [n for n in names if n in header]
                pos = [header.index(n) for n in order]
                return order, [[r[i] for i in pos] for r in raw]
            return header, raw
        vals = list(obj.values())
        if vals and all(isinstance(v, list) for v in vals):
            n = max(len(v) for v in vals)
            header = [str(k) for k in obj]
            cols = [list(v) + [None] * (n - len(v)) for v in vals]
            raw = [[_native(c[i]) for c in cols] for i in range(n)]
            return header, raw
        if vals and all(isinstance(v, dict) for v in vals):
            # pandas "columns" orient: {col: {index: value}}
            index = []
            seen = set()
            for v in vals:
                for k in v:
                    if k not in seen:
                        seen.add(k)
                        index.append(k)
            header = [str(k) for k in obj]
            raw = [[_native(v.get(i)) for v in vals] for i in index]
            return header, raw
        if not vals:
            return [], []
        return records_to_table([obj], path)
    raise QueryError('Unsupported JSON structure in %s' % path)


def load_jsonl(path, comp, text=None):
    if text is None:
        text = read_text(path, comp)
    records = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line, parse_float=Decimal)
        except ValueError as e:
            raise QueryError('Invalid JSON in %s (line %d): %s' % (path, i, e))
        if isinstance(obj, list) and all(isinstance(r, dict) for r in obj):
            records.extend(obj)
        else:
            records.append(obj)
    return records_to_table(records, path)


def load_json(path, comp):
    text = read_text(path, comp)
    try:
        obj = json.loads(text, parse_float=Decimal)
    except ValueError:
        return load_jsonl(path, comp, text)
    return json_to_table(obj, path)


def load_parquet(path, comp):
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise QueryError('pyarrow is required to read Parquet files')
    try:
        if comp:
            with open_binary(path, comp) as f:
                buf = io.BytesIO(f.read())
            table = pq.read_table(buf)
        else:
            table = pq.read_table(path)
    except QueryError:
        raise
    except Exception as e:
        raise QueryError('Invalid Parquet file %s: %s' % (path, e))
    header = [str(n) for n in table.column_names]
    # drop pandas index columns that were stored as data
    meta = table.schema.pandas_metadata or {}
    idx = [c for c in meta.get('index_columns', []) if isinstance(c, str)]
    cols = []
    keep = []
    for i, name in enumerate(header):
        if name in idx and name.startswith('__index_level_'):
            continue
        keep.append(name)
        cols.append([_native(v) for v in table.column(i).to_pylist()])
    n = table.num_rows
    raw = [[c[j] for c in cols] for j in range(n)]
    return keep, raw


def load_file(path, fmt, comp):
    if fmt == '.csv':
        return load_delimited(path, comp, ',')
    if fmt == '.tsv':
        return load_delimited(path, comp, '\t')
    if fmt == '.parquet':
        return load_parquet(path, comp)
    if fmt in ('.jsonl', '.ndjson'):
        return load_jsonl(path, comp)
    return load_json(path, comp)


def is_blank_file(path, comp):
    try:
        with open_binary(path, comp) as f:
            data = f.read()
    except (OSError, EOFError):
        return False
    if not data:
        return True
    try:
        return data.decode('utf-8-sig').strip() == ''
    except UnicodeDecodeError:
        return False


def parse_sharded(spec):
    if '=' not in spec:
        raise QueryError('Invalid --sharded value (expected <table>=<glob>): %s' % spec)
    name, pattern = spec.split('=', 1)
    name = name.strip()
    if not name or not pattern:
        raise QueryError('Invalid --sharded value (expected <table>=<glob>): %s' % spec)
    return name, pattern


class Catalog:
    def __init__(self, root, sharded=None):
        self.root = root
        self.files = {}   # table name -> list of (path, fmt, comp)
        self.cache = {}
        self.sharded = set()
        if root is not None:
            if not os.path.isdir(root):
                raise QueryError('Data directory not found: %s' % root)
            self._scan(root)
        for spec in sharded or ():
            self.add_sharded(*parse_sharded(spec))

    def add_sharded(self, name, pattern):
        import glob as _glob
        paths = sorted(p for p in _glob.glob(pattern, recursive=True) if os.path.isfile(p))
        entries = []
        for p in paths:
            info = split_data_name(os.path.basename(p))
            if info is None:
                continue
            entries.append((p, info[1], info[2]))
        # sharded table overrides any same-named (case-insensitive) table
        for k in [k for k in self.files if k.lower() == name.lower()]:
            del self.files[k]
            self.sharded.discard(k)
        self.files[name] = entries
        self.sharded.add(name)

    def _scan(self, root):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                info = split_data_name(fn)
                if info is None:
                    continue
                stem, fmt, comp = info
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(os.path.join(dirpath, stem), root)
                parts = rel.split(os.sep)
                name = '.'.join(p.replace('.', '_') for p in parts)
                self.files.setdefault(name, []).append((full, fmt, comp))

    def resolve_name(self, name):
        if name in self.files:
            return name
        low = {k.lower(): k for k in self.files}
        if name.lower() in low:
            return low[name.lower()]
        # allow dotted file names written literally (my.file -> my_file)
        alt = name.replace('.', '_')
        if alt.lower() in low:
            return low[alt.lower()]
        # unique basename match
        cands = [k for k in self.files if k.split('.')[-1].lower() == name.lower()]
        if len(cands) == 1:
            return cands[0]
        raise QueryError('Table not found: %s' % name)

    def load(self, name):
        name = self.resolve_name(name)
        if name in self.cache:
            return name, self.cache[name]
        header = None
        first_path = None
        raw = []
        is_sharded = name in self.sharded
        for path, fmt, comp in self.files[name]:
            if is_sharded and is_blank_file(path, comp):
                continue
            h, r = load_file(path, fmt, comp)
            if is_sharded and not h and not r:
                continue
            if len(set(h)) != len(h):
                raise QueryError('Duplicate column names in %s' % path)
            if header is None:
                header, first_path = h, path
                raw.extend(r)
                continue
            if h != header:
                if sorted(h) != sorted(header):
                    raise QueryError(
                        'Column mismatch for table %s: %s has columns [%s] but %s has '
                        'columns [%s]' % (name, first_path, ', '.join(header),
                                          path, ', '.join(h)))
                pos = [h.index(c) for c in header]
                r = [[row[i] for i in pos] for row in r]
            raw.extend(r)
        if header is None:
            header = []
        width = len(header)
        if width == 0 and not is_sharded:
            raise QueryError('No columns found for table %s' % name)
        cols = [infer_column([r[i] for r in raw]) for i in range(width)]
        rows = [tuple(cols[i][j] for i in range(width)) for j in range(len(raw))]
        self.cache[name] = (header, rows)
        return name, (header, rows)


# --------------------------------------------------------------------------
# Binding (column resolution) and validation
# --------------------------------------------------------------------------

class Scope:
    def __init__(self):
        self.tables = []   # dicts: name, alias, columns, offset
        self.width = 0

    def add(self, name, alias, columns):
        for t in self.tables:
            if (alias or name).lower() == (t['alias'] or t['name']).lower():
                raise QueryError('Duplicate table name/alias: %s' % (alias or name))
        self.tables.append({'name': name, 'alias': alias, 'columns': columns,
                            'offset': self.width})
        self.width += len(columns)

    def match_table(self, qual):
        ql = qual.lower()
        res = []
        for t in self.tables:
            names = {t['name'].lower(), t['name'].split('.')[-1].lower()}
            if t['alias']:
                names = {t['alias'].lower()}  # alias hides table name
            if ql in names:
                res.append(t)
        if not res:
            # fall back to the real table name even if aliased
            res = [t for t in self.tables if t['name'].lower() == ql]
        return res

    @staticmethod
    def find_col(t, col):
        if col in t['columns']:
            return t['columns'].index(col)
        low = [c.lower() for c in t['columns']]
        if low.count(col.lower()) == 1:
            return low.index(col.lower())
        return None

    def resolve(self, parts):
        """Return (index, column_name) or None."""
        col = parts[-1]
        if len(parts) > 1:
            qual = '.'.join(parts[:-1])
            ts = self.match_table(qual)
            if not ts:
                raise QueryError('Unknown table or alias: %s' % qual)
            hits = []
            for t in ts:
                i = self.find_col(t, col)
                if i is not None:
                    hits.append((t['offset'] + i, t['columns'][i]))
            if not hits:
                raise QueryError('Unknown column: %s' % '.'.join(parts))
            if len(hits) > 1:
                raise QueryError('Ambiguous column: %s' % '.'.join(parts))
            return hits[0]
        hits = []
        for t in self.tables:
            i = self.find_col(t, col)
            if i is not None:
                hits.append((t['offset'] + i, t['columns'][i]))
        if len(hits) > 1:
            raise QueryError('Ambiguous column: %s' % col)
        return hits[0] if hits else None


def bind(expr, scope, aliases=None, alias_first=False, allow_agg=True, in_agg=False,
         allow_win=False, in_win=False):
    """Resolve column references to ('idx', i). aliases: name -> bound expr."""
    def b(e):
        return bind(e, scope, aliases, alias_first, allow_agg, in_agg, allow_win, in_win)
    k = expr[0]
    if k == 'lit':
        return expr
    if k == 'col':
        parts = expr[1]
        if alias_first and aliases and len(parts) == 1 and not in_agg and not in_win:
            a = lookup_alias(aliases, parts[0])
            if a is not None:
                return a
        r = scope.resolve(parts)
        if r is None:
            if aliases and len(parts) == 1 and not in_agg and not in_win:
                a = lookup_alias(aliases, parts[0])
                if a is not None:
                    return a
            raise QueryError('Unknown column: %s' % '.'.join(parts))
        return ('idx', r[0])
    if k == 'agg':
        if not allow_agg:
            raise QueryError('Aggregate functions are not allowed here')
        if in_agg:
            raise QueryError('Nested aggregate functions are not allowed')
        arg = expr[2]
        if arg != '*':
            arg = bind(arg, scope, None, False, allow_agg, True, allow_win, in_win)
        return ('agg', expr[1], arg, expr[3])
    if k == 'win':
        if in_agg:
            raise WindowError(NESTED_ERROR, 'Window functions cannot be nested inside '
                              'aggregate functions')
        if in_win:
            raise WindowError(NESTED_ERROR, 'Window functions cannot be nested inside '
                              'other window functions')
        if not allow_win:
            raise WindowError(INVALID_SPEC, 'Window functions are only allowed in the '
                              'SELECT list and ORDER BY')
        _, name, arg, distinct, part, order, frame = expr

        def bw(e, spec):
            try:
                return bind(e, scope, None, False, allow_agg, False, True, True)
            except WindowError:
                raise
            except QueryError as ex:
                if not spec:
                    raise
                raise WindowError(INVALID_SPEC, 'Invalid window specification: %s' % ex)
        if arg is not None and arg != '*':
            arg = bw(arg, False)
        bpart = tuple(bw(e, True) for e in part)
        border = tuple((bw(e, True), d, nf) for e, d, nf in order)
        if frame is not None and frame[0] == 'RANGE' and len(border) != 1 and \
                (frame[1][0] in ('P', 'F') or frame[2][0] in ('P', 'F')):
            raise WindowError(FRAME_ERROR, 'RANGE with offset requires exactly one '
                              'ORDER BY expression')
        return ('win', name, arg, distinct, bpart, border, frame)
    if k == 'cmp':
        return ('cmp', expr[1], b(expr[2]), b(expr[3]))
    if k == 'like':
        return ('like', b(expr[1]), b(expr[2]), expr[3], expr[4])
    if k == 'in':
        return ('in', b(expr[1]), [b(x) for x in expr[2]], expr[3])
    if k == 'isnull':
        return ('isnull', b(expr[1]), expr[2])
    if k in ('and', 'or'):
        return (k, b(expr[1]), b(expr[2]))
    if k == 'not':
        return ('not', b(expr[1]))
    if k == 'idx':
        return expr
    raise QueryError('Unsupported expression')


def win_children(expr):
    """Sub-expressions of a bound window node (argument, partition, order)."""
    out = []
    if expr[2] is not None and expr[2] != '*':
        out.append(expr[2])
    out.extend(expr[4])
    out.extend(e for e, _, _ in expr[5])
    return out


def find_windows(expr, acc):
    k = expr[0]
    if k == 'win':
        if all(x is not expr for x in acc):
            acc.append(expr)
        return acc
    if k in ('lit', 'idx', 'col', 'agg'):
        return acc
    if k == 'cmp':
        find_windows(expr[2], acc)
        find_windows(expr[3], acc)
    elif k == 'like':
        find_windows(expr[1], acc)
        find_windows(expr[2], acc)
    elif k == 'in':
        find_windows(expr[1], acc)
        for x in expr[2]:
            find_windows(x, acc)
    elif k in ('isnull', 'not'):
        find_windows(expr[1], acc)
    elif k in ('and', 'or'):
        find_windows(expr[1], acc)
        find_windows(expr[2], acc)
    return acc


def lookup_alias(aliases, name):
    if name in aliases:
        return aliases[name]
    low = [(a, e) for a, e in aliases.items() if a.lower() == name.lower()]
    if len(low) == 1:
        return low[0][1]
    return None


def has_agg(expr):
    k = expr[0]
    if k == 'agg':
        return True
    if k in ('lit', 'idx', 'col'):
        return False
    if k == 'win':
        return any(has_agg(x) for x in win_children(expr))
    if k == 'cmp':
        return has_agg(expr[2]) or has_agg(expr[3])
    if k == 'like':
        return has_agg(expr[1]) or has_agg(expr[2])
    if k == 'in':
        return has_agg(expr[1]) or any(has_agg(x) for x in expr[2])
    if k in ('isnull', 'not'):
        return has_agg(expr[1])
    if k in ('and', 'or'):
        return has_agg(expr[1]) or has_agg(expr[2])
    return False


def check_grouped(expr, group_idx, group_exprs, what):
    """Ensure every column outside aggregates is part of GROUP BY."""
    if expr in group_exprs:
        return
    k = expr[0]
    if k in ('lit', 'agg'):
        return
    if k == 'win':
        for x in win_children(expr):
            check_grouped(x, group_idx, group_exprs, what)
        return
    if k == 'idx':
        if expr[1] not in group_idx:
            raise QueryError('Column in %s must appear in GROUP BY or be used in '
                             'an aggregate function' % what)
        return
    if k == 'cmp':
        check_grouped(expr[2], group_idx, group_exprs, what)
        check_grouped(expr[3], group_idx, group_exprs, what)
    elif k == 'like':
        check_grouped(expr[1], group_idx, group_exprs, what)
        check_grouped(expr[2], group_idx, group_exprs, what)
    elif k == 'in':
        check_grouped(expr[1], group_idx, group_exprs, what)
        for x in expr[2]:
            check_grouped(x, group_idx, group_exprs, what)
    elif k in ('isnull', 'not'):
        check_grouped(expr[1], group_idx, group_exprs, what)
    elif k in ('and', 'or'):
        check_grouped(expr[1], group_idx, group_exprs, what)
        check_grouped(expr[2], group_idx, group_exprs, what)


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

_like_cache = {}


def like_regex(pattern, ci):
    key = (pattern, ci)
    r = _like_cache.get(key)
    if r is None:
        out = []
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == '\\' and i + 1 < len(pattern):
                out.append(re.escape(pattern[i + 1]))
                i += 2
                continue
            if c == '%':
                out.append('.*')
            elif c == '_':
                out.append('.')
            else:
                out.append(re.escape(c))
            i += 1
        r = re.compile(''.join(out), re.DOTALL | (re.IGNORECASE if ci else 0))
        _like_cache[key] = r
    return r


def sql_compare(op, a, b):
    if a is None or b is None:
        return None
    c = cmp_values(a, b)
    if op == '=':
        return c == 0
    if op == '!=':
        return c != 0
    if op == '<':
        return c < 0
    if op == '<=':
        return c <= 0
    if op == '>':
        return c > 0
    return c >= 0


def evaluate(expr, row, group, wv=None):
    k = expr[0]
    if k == 'idx':
        if row is None:
            return None
        return row[expr[1]]
    if k == 'lit':
        return expr[1]
    if k == 'agg':
        if group is None:
            raise QueryError('Aggregate used outside of an aggregate context')
        return eval_agg(expr, group)
    if k == 'win':
        if wv is None:
            raise QueryError('Window function used outside of a window context')
        return wv[id(expr)]
    if k == 'cmp':
        return sql_compare(expr[1], evaluate(expr[2], row, group, wv),
                           evaluate(expr[3], row, group, wv))
    if k == 'and':
        a = evaluate(expr[1], row, group, wv)
        if a is False:
            return False
        b = evaluate(expr[2], row, group, wv)
        if b is False:
            return False
        if a is None or b is None:
            return None
        return True
    if k == 'or':
        a = evaluate(expr[1], row, group, wv)
        if a is True:
            return True
        b = evaluate(expr[2], row, group, wv)
        if b is True:
            return True
        if a is None or b is None:
            return None
        return False
    if k == 'not':
        a = evaluate(expr[1], row, group, wv)
        return None if a is None else (not truthy(a))
    if k == 'like':
        v = evaluate(expr[1], row, group, wv)
        p = evaluate(expr[2], row, group, wv)
        if v is None or p is None:
            return None
        m = like_regex(fmt_value(p), expr[4]).fullmatch(fmt_value(v)) is not None
        return (not m) if expr[3] else m
    if k == 'in':
        v = evaluate(expr[1], row, group, wv)
        if v is None:
            return None
        saw_null = False
        for x in expr[2]:
            xv = evaluate(x, row, group, wv)
            if xv is None:
                saw_null = True
            elif cmp_values(v, xv) == 0:
                return not expr[3]
        if saw_null:
            return None
        return expr[3]
    if k == 'isnull':
        v = evaluate(expr[1], row, group, wv)
        return (v is not None) if expr[2] else (v is None)
    raise QueryError('Cannot evaluate expression')


def truthy(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if is_num(v):
        return v != 0
    return bool(v)


def eval_agg(expr, group):
    _, name, arg, distinct = expr
    if arg == '*':
        return len(group)
    vals = [evaluate(arg, r, None) for r in group]
    vals = [v for v in vals if v is not None]
    if distinct:
        seen = set()
        uniq = []
        for v in vals:
            hk = hash_key(v)
            if hk not in seen:
                seen.add(hk)
                uniq.append(v)
        vals = uniq
    if name == 'COUNT':
        return len(vals)
    if not vals:
        return None
    if name in ('SUM', 'AVG'):
        total = 0
        for v in vals:
            n = v if is_num(v) else to_number(v)
            if n is None:
                raise QueryError('%s requires numeric values, got %r' % (name, v))
            total = total + n
        if name == 'AVG':
            return avg_value(total, len(vals))
        return total
    if name in ('MIN', 'MAX'):
        best = vals[0]
        for v in vals[1:]:
            c = cmp_values(v, best)
            if (name == 'MIN' and c < 0) or (name == 'MAX' and c > 0):
                best = v
        return best
    raise QueryError('Unsupported aggregate %s' % name)


def round_sig(d, sig=15):
    """Round a Decimal to `sig` significant digits; integral results -> int."""
    if d == 0:
        return 0
    q = d.quantize(Decimal(1).scaleb(d.adjusted() - sig + 1), rounding=ROUND_HALF_EVEN)
    if q == q.to_integral_value():
        return int(q)
    return q.normalize()


def avg_value(total, cnt):
    if isinstance(total, int) and total % cnt == 0:
        return total // cnt
    q = float(total) / cnt
    if q != q or q in (float('inf'), float('-inf')):
        return round_sig(Decimal(total) / Decimal(cnt))
    d = Decimal('%.15g' % q)
    if d == d.to_integral_value():
        return int(d)
    return d.normalize()


def numeric_or_error(v, name):
    if v is None:
        return None
    n = v if is_num(v) else to_number(v)
    if n is None:
        raise QueryError('%s requires numeric values, got %r' % (name, v))
    return n


# --------------------------------------------------------------------------
# Window functions
# --------------------------------------------------------------------------

def _first_true(lo, hi, pred):
    """Smallest index i in [lo, hi) with pred(i) true (pred monotone), else hi."""
    while lo < hi:
        mid = (lo + hi) // 2
        if pred(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def compute_frames(frame, has_order, m, gid, gstart, gend, okeys, desc):
    """Return list of (start, end) half-open frame bounds for a partition."""
    if frame is None:
        if not has_order:
            return [(0, m)] * m
        frame = ('RANGE', ('UP', None), ('C', None))
    unit, sb, eb = frame
    ngroups = len(gstart)
    res = []
    rkeys = None
    if unit == 'RANGE' and (sb[0] in ('P', 'F') or eb[0] in ('P', 'F')):
        inf = float('inf')
        rkeys = []
        for k in okeys:
            v = k[0]
            if v is None:
                rkeys.append(inf)
                continue
            n = v if is_num(v) else to_number(v)
            if n is None:
                raise WindowError(FRAME_ERROR, 'RANGE with offset requires a numeric '
                                  'ORDER BY expression, got %r' % v)
            rkeys.append(-n if desc else n)
    for j in range(m):
        g = gid[j]
        out = []
        for which, (kind, n) in (('s', sb), ('e', eb)):
            if kind == 'UP':
                out.append(0)
                continue
            if kind == 'UF':
                out.append(m)
                continue
            if unit == 'ROWS':
                off = 0 if kind == 'C' else (-n if kind == 'P' else n)
                pos = j + off + (1 if which == 'e' else 0)
                out.append(min(max(pos, 0), m))
            elif unit == 'GROUPS':
                off = 0 if kind == 'C' else (-n if kind == 'P' else n)
                tg = g + off
                if which == 's':
                    out.append(0 if tg < 0 else (m if tg >= ngroups else gstart[tg]))
                else:
                    out.append(0 if tg < 0 else (m if tg >= ngroups else gend[tg]))
            else:  # RANGE
                if kind == 'C' or rkeys[j] == float('inf'):
                    out.append(gstart[g] if which == 's' else gend[g])
                    continue
                bv = rkeys[j] - n if kind == 'P' else rkeys[j] + n
                if which == 's':
                    out.append(_first_true(0, m, lambda i: rkeys[i] >= bv))
                else:
                    out.append(_first_true(0, m, lambda i: rkeys[i] > bv))
        s, e = out
        if e < s:
            e = s
        res.append((s, e))
    return res


def window_aggregate(name, distinct, vals, frames):
    m = len(vals)
    if distinct:
        res = []
        for s, e in frames:
            seen = set()
            uniq = []
            for v in vals[s:e]:
                if v is None:
                    continue
                hk = hash_key(v)
                if hk not in seen:
                    seen.add(hk)
                    uniq.append(v)
            res.append(_agg_list(name, uniq))
        return res
    if name in ('COUNT', 'SUM', 'AVG'):
        nums = vals
        if name != 'COUNT':
            nums = [numeric_or_error(v, name) for v in vals]
        psum = [0] * (m + 1)
        pcnt = [0] * (m + 1)
        for i, v in enumerate(nums):
            psum[i + 1] = psum[i] + (v if v is not None and name != 'COUNT' else 0)
            pcnt[i + 1] = pcnt[i] + (v is not None)
        res = []
        for s, e in frames:
            c = pcnt[e] - pcnt[s]
            if name == 'COUNT':
                res.append(c)
            elif c == 0:
                res.append(None)
            else:
                if s == 0 or (isinstance(psum[e], int) and isinstance(psum[s], int)):
                    tot = psum[e] - psum[s]
                else:
                    tot = _sum_of(nums, s, e)
                res.append(tot if name == 'SUM' else avg_value(tot, c))
        return res
    # MIN / MAX
    sign = -1 if name == 'MIN' else 1
    if all(s == 0 for s, _ in frames) and all(
            frames[i][1] <= frames[i + 1][1] for i in range(m - 1)):
        res = []
        best = None
        pos = 0
        for s, e in frames:
            while pos < e:
                v = vals[pos]
                if v is not None and (best is None or cmp_values(v, best) * sign > 0):
                    best = v
                pos += 1
            res.append(best)
        return res
    return [_agg_list(name, [v for v in vals[s:e] if v is not None]) for s, e in frames]


def _sum_of(nums, s, e):
    # Decimal sums are recomputed directly to keep exact digits for the frame
    total = 0
    for v in nums[s:e]:
        if v is not None:
            total = total + v
    return total


def _agg_list(name, vals):
    if name == 'COUNT':
        return len(vals)
    if not vals:
        return None
    if name in ('SUM', 'AVG'):
        total = 0
        for v in vals:
            total = total + numeric_or_error(v, name)
        return avg_value(total, len(vals)) if name == 'AVG' else total
    best = vals[0]
    for v in vals[1:]:
        c = cmp_values(v, best)
        if (name == 'MIN' and c < 0) or (name == 'MAX' and c > 0):
            best = v
    return best


def compute_windows(win_nodes, contexts):
    """Compute every window function for each context.

    Returns (order, values) where values[i] maps id(node) -> value for context i
    and order is the row order after the last window has been processed."""
    n = len(contexts)
    values = [dict() for _ in range(n)]
    order = list(range(n))
    for node in win_nodes:
        _, name, arg, distinct, part, ordby, frame = node
        pkeys = [tuple(evaluate(e, r, g) for e in part) for r, g in contexts]
        okeys = [tuple(evaluate(e, r, g) for e, _, _ in ordby) for r, g in contexts]
        pc = order_cmp([(False, None)] * len(part))
        oc = order_cmp([(d, nf) for _, d, nf in ordby])
        if part or ordby:
            order.sort(key=functools.cmp_to_key(
                lambda a, b: pc(pkeys[a], pkeys[b]) or oc(okeys[a], okeys[b])))
        if arg is None or arg == '*':
            argvals = None
        else:
            argvals = [evaluate(arg, r, g) for r, g in contexts]
        i = 0
        while i < n:
            j = i + 1
            while j < n and pc(pkeys[order[i]], pkeys[order[j]]) == 0:
                j += 1
            P = order[i:j]
            m = len(P)
            gid = [0] * m
            gstart = [0]
            gend = []
            for t in range(1, m):
                if ordby and oc(okeys[P[t - 1]], okeys[P[t]]) != 0:
                    gid[t] = gid[t - 1] + 1
                    gend.append(t)
                    gstart.append(t)
                else:
                    gid[t] = gid[t - 1]
            gend.append(m)
            if name == 'ROW_NUMBER':
                res = list(range(1, m + 1))
            elif name == 'RANK':
                res = [gstart[gid[t]] + 1 for t in range(m)]
            elif name == 'DENSE_RANK':
                res = [gid[t] + 1 for t in range(m)]
            else:
                frames = compute_frames(frame, bool(ordby), m, gid, gstart, gend,
                                        [okeys[x] for x in P],
                                        ordby[0][1] if ordby else False)
                if argvals is None:
                    res = [e - s for s, e in frames]
                else:
                    res = window_aggregate(name, distinct, [argvals[x] for x in P], frames)
            for t in range(m):
                values[P[t]][id(node)] = res[t]
            i = j
    return order, values


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def equi_join_keys(on, left_scope_width, right_offset, right_width):
    """If ON is a simple col=col between left and right, return (li, ri)."""
    if on[0] == 'cmp' and on[1] == '=' and on[2][0] == 'idx' and on[3][0] == 'idx':
        a, b = on[2][1], on[3][1]
        in_right = lambda i: right_offset <= i < right_offset + right_width
        if a < left_scope_width and in_right(b):
            return a, b - right_offset
        if b < left_scope_width and in_right(a):
            return b, a - right_offset
    return None


def do_join(left_rows, left_width, right_rows, right_width, jtype, on):
    out = []
    null_left = (None,) * left_width
    null_right = (None,) * right_width
    matched_right = set()
    keys = equi_join_keys(on, left_width, left_width, right_width) if on else None
    index = None
    if keys:
        index = {}
        for j, r in enumerate(right_rows):
            v = r[keys[1]]
            if v is None:
                continue
            index.setdefault(hash_key(v), []).append(j)
    for l in left_rows:
        found = False
        if jtype == 'CROSS':
            cands = range(len(right_rows))
        elif index is not None:
            v = l[keys[0]]
            cands = [] if v is None else index.get(hash_key(v), [])
        else:
            cands = range(len(right_rows))
        for j in cands:
            combined = l + right_rows[j]
            if jtype == 'CROSS' or index is not None or evaluate(on, combined, None) is True:
                out.append(combined)
                found = True
                matched_right.add(j)
        if not found and jtype in ('LEFT', 'FULL'):
            out.append(l + null_right)
    if jtype in ('RIGHT', 'FULL'):
        for j, r in enumerate(right_rows):
            if j not in matched_right:
                out.append(null_left + r)
    return out


def order_cmp(specs):
    def cmp(a, b):
        for (desc, nulls_first), x, y in zip(specs, a, b):
            if x is None and y is None:
                continue
            if x is None or y is None:
                nf = nulls_first if nulls_first is not None else False
                if x is None:
                    return -1 if nf else 1
                return 1 if nf else -1
            c = cmp_values(x, y)
            if c:
                return -c if desc else c
        return 0
    return cmp


def execute(q, catalog):
    scope = Scope()
    name, (header, rows) = catalog.load(q['from']['name'])
    scope.add(name, q['from']['alias'], header)
    for jtype, tref, on in q['joins']:
        jname, (jheader, jrows) = catalog.load(tref['name'])
        left_width = scope.width
        scope.add(jname, tref['alias'], jheader)
        bon = bind(on, scope, allow_agg=False) if on is not None else None
        rows = do_join(rows, left_width, jrows, len(jheader), jtype, bon)

    # SELECT list
    out_names = []
    out_exprs = []
    aliases = {}
    for item in q['items']:
        if item['star']:
            tabs = scope.tables if item['qual'] is None else scope.match_table(item['qual'])
            if not tabs:
                raise QueryError('Unknown table or alias: %s' % item['qual'])
            for t in tabs:
                for i, c in enumerate(t['columns']):
                    out_names.append(c)
                    out_exprs.append(('idx', t['offset'] + i))
            continue
        e = bind(item['expr'], scope, allow_win=True)
        out_exprs.append(e)
        if item['alias'] is not None:
            out_names.append(item['alias'])
            aliases.setdefault(item['alias'], e)
        elif item['expr'][0] == 'col':
            r = scope.resolve(item['expr'][1])
            out_names.append(r[1] if r else item['expr'][1][-1])
        else:
            out_names.append(item['text'])
    where = bind(q['where'], scope, allow_agg=False) if q['where'] is not None else None
    group_exprs = []
    for g in q['group_by']:
        if g[0] == 'lit' and isinstance(g[1], int) and not isinstance(g[1], bool):
            if not 1 <= g[1] <= len(out_exprs):
                raise QueryError('GROUP BY position %d out of range' % g[1])
            ge = out_exprs[g[1] - 1]
        else:
            ge = bind(g, scope, aliases, allow_agg=False)
        if has_agg(ge):
            raise QueryError('Aggregate functions are not allowed in GROUP BY')
        group_exprs.append(ge)
    having = bind(q['having'], scope, aliases) if q['having'] is not None else None

    order_exprs = []
    for e, desc, nf in q['order_by']:
        if e[0] == 'lit' and isinstance(e[1], int) and not isinstance(e[1], bool):
            if not 1 <= e[1] <= len(out_exprs):
                raise QueryError('ORDER BY position %d out of range' % e[1])
            be = out_exprs[e[1] - 1]
        else:
            be = bind(e, scope, aliases, alias_first=True, allow_win=True)
        order_exprs.append((be, desc, nf))

    grouped = bool(group_exprs) or having is not None or \
        any(has_agg(e) for e in out_exprs) or any(has_agg(e) for e, _, _ in order_exprs)

    if where is not None:
        rows = [r for r in rows if evaluate(where, r, None) is True]

    contexts = []
    if grouped:
        group_idx = {e[1] for e in group_exprs if e[0] == 'idx'}
        for e in out_exprs:
            check_grouped(e, group_idx, group_exprs, 'SELECT')
        if having is not None:
            check_grouped(having, group_idx, group_exprs, 'HAVING')
        for e, _, _ in order_exprs:
            check_grouped(e, group_idx, group_exprs, 'ORDER BY')
        if group_exprs:
            groups = {}
            keys_raw = {}
            for r in rows:
                kv = tuple(evaluate(g, r, None) for g in group_exprs)
                hk = tuple(hash_key(v) for v in kv)
                if hk not in groups:
                    groups[hk] = []
                    keys_raw[hk] = kv
                groups[hk].append(r)
            items = list(groups.items())
            if not order_exprs:
                spec = [(False, None)] * len(group_exprs)
                c = order_cmp(spec)
                items.sort(key=functools.cmp_to_key(
                    lambda a, b: c(keys_raw[a[0]], keys_raw[b[0]])))
            for hk, grows in items:
                contexts.append((grows[0], grows))
        else:
            contexts.append((rows[0] if rows else None, rows))
        if having is not None:
            contexts = [c for c in contexts if evaluate(having, c[0], c[1]) is True]
    else:
        contexts = [(r, None) for r in rows]

    win_nodes = []
    for e in out_exprs:
        find_windows(e, win_nodes)
    for e, _, _ in order_exprs:
        find_windows(e, win_nodes)
    if win_nodes:
        corder, wvals = compute_windows(win_nodes, contexts)
        seq = [(contexts[i], wvals[i]) for i in corder]
    else:
        seq = [(c, None) for c in contexts]

    results = []
    for (row, grp), wv in seq:
        vals = tuple(evaluate(e, row, grp, wv) for e in out_exprs)
        okeys = tuple(evaluate(e, row, grp, wv) for e, _, _ in order_exprs)
        results.append((vals, okeys))

    if q['distinct']:
        seen = set()
        uniq = []
        for vals, okeys in results:
            hk = tuple(hash_key(v) for v in vals)
            if hk not in seen:
                seen.add(hk)
                uniq.append((vals, okeys))
        results = uniq

    if order_exprs:
        c = order_cmp([(d, nf) for _, d, nf in order_exprs])
        results.sort(key=functools.cmp_to_key(lambda a, b: c(a[1], b[1])))

    out_rows = [v for v, _ in results]
    if q['offset']:
        out_rows = out_rows[q['offset']:]
    if q['limit'] is not None:
        out_rows = out_rows[:q['limit']]
    return out_names, out_rows


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def md_escape(s):
    return s.replace('|', '\\|').replace('\r\n', ' ').replace('\n', ' ')


def to_markdown(names, rows):
    hdr = [md_escape(n) for n in names]
    body = [[md_escape(fmt_value(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in body] + [3]) for i, h in enumerate(hdr)]
    lines = ['| ' + ' | '.join(h.ljust(w) for h, w in zip(hdr, widths)) + ' |',
             '|' + '|'.join('-' * (w + 2) for w in widths) + '|']
    for r in body:
        lines.append('| ' + ' | '.join(c.ljust(w) for c, w in zip(r, widths)) + ' |')
    return '\n'.join(lines)


def write_csv(path, names, rows):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(names)
        for r in rows:
            w.writerow([fmt_value(v, null='') for v in r])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def read_sql(arg):
    if os.path.isfile(arg):
        with open(arg, encoding='utf-8-sig') as f:
            return f.read()
    return arg


def run(data, sql_arg, out=None, sharded=None):
    sql = read_sql(sql_arg).strip()
    if not sql:
        raise QueryError('Empty SQL query')
    q = Parser(sql).parse()
    return execute(q, Catalog(data, sharded))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Run read-only SQL over data files (CSV, TSV, Parquet, JSON, JSONL).')
    ap.add_argument('--data', help='Directory containing data files')
    ap.add_argument('--sharded', action='append', default=[], metavar='TABLE=GLOB',
                    help='Merge files matching GLOB into table TABLE (repeatable)')
    ap.add_argument('--sql', required=True, help='SQL query string or path to .sql file')
    ap.add_argument('--out', help='Optional output CSV path')
    args = ap.parse_args(argv)
    try:
        if args.data is None and not args.sharded:
            ap.error('one of --data or --sharded is required')
        names, rows = run(args.data, args.sql, sharded=args.sharded)
        if args.out:
            write_csv(args.out, names, rows)
    except WindowError as e:
        print('Error: %s' % e, file=sys.stderr)
        return WindowError.code
    except QueryError as e:
        print('Error: %s' % e, file=sys.stderr)
        return 1
    except (OSError, EOFError, UnicodeDecodeError, csv.Error) as e:
        print('Error: %s' % e, file=sys.stderr)
        return 1
    print(to_markdown(names, rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
