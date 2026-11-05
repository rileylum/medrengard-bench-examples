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


class CTEError(WindowError):
    """Errors related to common table expressions (exit code 4)."""
    group = 'CTE_ERROR'
    code = 4


class SubqueryError(WindowError):
    """Errors related to subqueries (exit code 5)."""
    group = 'SUBQUERY_ERROR'
    code = 5


INVALID_CTE = 'INVALID_CTE_SYNTAX'
DEPTH_EXCEEDED = 'RECURSIVE_DEPTH_EXCEEDED'
CTE_NOT_FOUND = 'CTE_NOT_FOUND'
CYCLIC = 'CYCLIC_REFERENCE'
SCALAR_ROWS = 'SCALAR_SUBQUERY_MULTIPLE_ROWS'
INVALID_REF = 'INVALID_SUBQUERY_REFERENCE'
CORRELATED_ERR = 'CORRELATED_SUBQUERY_ERROR'
MAX_RECURSION = 1000
MAX_RECURSIVE_ROWS = 1000000


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
        if two in ('!=', '<>', '<=', '>=', '==', '||'):
            toks.append(Tok('OP', {'<>': '!=', '==': '='}.get(two, two), i, i + 2))
            i += 2
            continue
        if c in '=<>(),.*;+-/%':
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
    'CROSS', 'IN', 'UNION', 'ILIKE', 'WITH', 'EXISTS', 'CASE', 'WHEN', 'THEN',
    'ELSE', 'END', 'BETWEEN', 'INTERSECT', 'EXCEPT',
}
SCALAR_FUNCS = {
    'COALESCE': (1, None), 'IFNULL': (2, 2), 'NULLIF': (2, 2), 'ABS': (1, 1),
    'ROUND': (1, 2), 'FLOOR': (1, 1), 'CEIL': (1, 1), 'CEILING': (1, 1),
    'UPPER': (1, 1), 'LOWER': (1, 1), 'LENGTH': (1, 1), 'TRIM': (1, 1),
    'LTRIM': (1, 1), 'RTRIM': (1, 1), 'SUBSTR': (2, 3), 'SUBSTRING': (2, 3),
    'CONCAT': (1, None), 'REPLACE': (3, 3), 'GREATEST': (1, None),
    'LEAST': (1, None), 'MOD': (2, 2),
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
        q = self.parse_query()
        while self.accept_op(';'):
            pass
        if self.peek().kind != 'EOF':
            raise QueryError('Unexpected input near %s' % self.where())
        return q

    def starts_query(self, k=0):
        return self.is_kw('SELECT', k) or self.is_kw('WITH', k)

    def parse_query(self):
        if self.is_kw('WITH'):
            w = self.parse_with()
            if not (self.starts_query() or self.is_op('(')):
                raise CTEError(INVALID_CTE, 'Expected SELECT after WITH clause near %s'
                               % self.where())
            body = self.parse_compound()
            return {'kind': 'with', 'with': w, 'body': body}
        return self.parse_compound()

    def parse_with(self):
        self.expect_kw('WITH')
        recursive = bool(self.accept_kw('RECURSIVE'))
        ctes = []
        while True:
            try:
                name = self.ident()
            except QueryError:
                raise CTEError(INVALID_CTE, 'Expected CTE name near %s' % self.where())
            cols = None
            if self.accept_op('('):
                cols = []
                while True:
                    try:
                        cols.append(self.ident())
                    except QueryError:
                        raise CTEError(INVALID_CTE, 'Invalid column list for CTE %s' % name)
                    if not self.accept_op(','):
                        break
                if not self.accept_op(')'):
                    raise CTEError(INVALID_CTE, "Expected ')' after column list of CTE %s"
                                   % name)
                if len(set(c.lower() for c in cols)) != len(cols):
                    raise CTEError(INVALID_CTE, 'Duplicate column name in column list of '
                                   'CTE %s' % name)
            if not self.accept_kw('AS'):
                raise CTEError(INVALID_CTE, 'Expected AS after CTE name %s near %s'
                               % (name, self.where()))
            if self.is_kw('NOT') and self.is_kw('MATERIALIZED', 1):
                self.pos += 2
            else:
                self.accept_kw('MATERIALIZED')
            if not self.accept_op('('):
                raise CTEError(INVALID_CTE, "Expected '(' after AS for CTE %s" % name)
            if not (self.starts_query() or self.is_op('(')):
                raise CTEError(INVALID_CTE, 'CTE %s must contain a SELECT statement' % name)
            try:
                q = self.parse_query()
            except WindowError:
                raise
            except QueryError as e:
                raise CTEError(INVALID_CTE, 'Invalid definition of CTE %s: %s' % (name, e))
            if not self.accept_op(')'):
                raise CTEError(INVALID_CTE, "Expected ')' to close CTE %s near %s"
                               % (name, self.where()))
            ctes.append({'name': name, 'cols': cols, 'query': q})
            if not self.accept_op(','):
                break
        return {'recursive': recursive, 'ctes': ctes}

    def parse_compound(self):
        left = self.parse_set_term()
        while True:
            op = self.accept_kw('UNION', 'INTERSECT', 'EXCEPT')
            if not op:
                break
            all_ = bool(self.accept_kw('ALL'))
            if not all_:
                self.accept_kw('DISTINCT')
            right = self.parse_set_term()
            left = {'kind': 'setop', 'op': op, 'all': all_, 'left': left, 'right': right,
                    'order_by': [], 'limit': None, 'offset': None}
        if left['kind'] == 'setop':
            r = left['right']
            if r['kind'] == 'select' and not r.get('paren'):
                left['order_by'], r['order_by'] = r['order_by'], []
                left['limit'], r['limit'] = r['limit'], None
                left['offset'], r['offset'] = r['offset'], None
            self.parse_order_limit(left)
        return left

    def parse_set_term(self):
        if self.is_op('(') and (self.starts_query(1) or self.is_op('(', 1)):
            self.next()
            q = self.parse_query()
            self.expect_op(')')
            q = dict(q)
            q['paren'] = True
            return q
        return self.parse_select()

    def parse_select(self):
        self.expect_kw('SELECT')
        q = {'kind': 'select', 'distinct': False, 'items': [], 'sources': [],
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
        if self.accept_kw('FROM'):
            q['sources'].append((None, self.parse_table_ref(), None))
            while True:
                jt = None
                if self.accept_op(','):
                    q['sources'].append(('CROSS', self.parse_table_ref(), None))
                    continue
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
                q['sources'].append((jt, tref, on))
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
        self.parse_order_limit(q)
        return q

    def parse_order_limit(self, q):
        if self.accept_kw('ORDER'):
            self.expect_kw('BY')
            q['order_by'] = []
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

    def parse_alias(self):
        alias = None
        cols = None
        if self.accept_kw('AS'):
            alias = self.ident()
        elif self.peek().kind == 'QIDENT' or (
                self.peek().kind == 'IDENT' and self.peek().value.upper() not in RESERVED):
            alias = self.ident()
        if alias is not None and self.is_op('(') and \
                self.peek(1).kind in ('IDENT', 'QIDENT'):
            self.next()
            cols = [self.ident()]
            while self.accept_op(','):
                cols.append(self.ident())
            self.expect_op(')')
        return alias, cols

    def parse_table_ref(self):
        if self.is_op('('):
            if self.starts_query(1) or self.is_op('(', 1):
                self.next()
                q = self.parse_query()
                self.expect_op(')')
                alias, cols = self.parse_alias()
                if alias is None:
                    raise SubqueryError(INVALID_REF, 'A subquery in FROM (derived table) '
                                        'must have an alias')
                return {'subquery': q, 'alias': alias, 'cols': cols}
            raise QueryError('Unexpected token near %s' % self.where())
        parts = [self.ident()]
        while self.accept_op('.'):
            parts.append(self.ident())
        alias, cols = self.parse_alias()
        return {'name': '.'.join(parts), 'alias': alias, 'cols': cols}

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

    def parse_subquery_parens(self):
        self.expect_op('(')
        q = self.parse_query()
        self.expect_op(')')
        return q

    def parse_cmp(self):
        left = self.parse_additive()
        t = self.peek()
        if t.kind == 'OP' and t.value in ('=', '!=', '<', '<=', '>', '>='):
            self.next()
            if (self.is_kw('ANY') or self.is_kw('ALL') or self.is_kw('SOME')) and \
                    self.is_op('(', 1) and self.starts_query(2):
                kind = 'ALL' if self.next().value.upper() == 'ALL' else 'ANY'
                return ('quant', t.value, left, self.parse_subquery_parens(), kind)
            return ('cmp', t.value, left, self.parse_additive())
        neg = False
        if self.is_kw('NOT') and (self.is_kw('LIKE', 1) or self.is_kw('ILIKE', 1)
                                  or self.is_kw('IN', 1) or self.is_kw('BETWEEN', 1)):
            self.next()
            neg = True
        kw = self.accept_kw('LIKE', 'ILIKE')
        if kw:
            return ('like', left, self.parse_additive(), neg, kw == 'ILIKE')
        if self.accept_kw('IN'):
            if self.is_op('(') and self.starts_query(1):
                return ('in_sub', left, self.parse_subquery_parens(), neg)
            self.expect_op('(')
            items = [self.parse_expr()]
            while self.accept_op(','):
                items.append(self.parse_expr())
            self.expect_op(')')
            return ('in', left, items, neg)
        if self.accept_kw('BETWEEN'):
            lo = self.parse_additive()
            self.expect_kw('AND')
            hi = self.parse_additive()
            node = ('op', 'BETWEEN', (left, lo, hi))
            return ('not', node) if neg else node
        if self.accept_kw('IS'):
            n = bool(self.accept_kw('NOT'))
            if self.accept_kw('DISTINCT'):
                self.expect_kw('FROM')
                right = self.parse_additive()
                return ('op', 'NOTDISTINCT' if n else 'DISTINCTFROM', (left, right))
            w = self.accept_kw('TRUE', 'FALSE')
            if w:
                return ('op', 'IS' + w + ('_NOT' if n else ''), (left,))
            self.expect_kw('NULL')
            return ('isnull', left, n)
        return left

    def parse_additive(self):
        left = self.parse_mul()
        while True:
            t = self.peek()
            if t.kind == 'OP' and t.value in ('+', '-', '||'):
                self.next()
                left = ('op', t.value, (left, self.parse_mul()))
            else:
                return left

    def parse_mul(self):
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t.kind == 'OP' and t.value in ('*', '/', '%'):
                self.next()
                left = ('op', t.value, (left, self.parse_unary()))
            else:
                return left

    def parse_unary(self):
        t = self.peek()
        if t.kind == 'OP' and t.value in ('-', '+'):
            self.next()
            n = self.peek()
            if n.kind == 'NUMBER':
                self.next()
                return ('lit', -n.value if t.value == '-' else n.value)
            e = self.parse_unary()
            return ('op', 'neg', (e,)) if t.value == '-' else e
        return self.parse_primary()

    def parse_case(self):
        operand = None
        if not self.is_kw('WHEN'):
            operand = self.parse_expr()
        args = [operand]
        if not self.is_kw('WHEN'):
            raise QueryError('Expected WHEN in CASE expression near %s' % self.where())
        while self.accept_kw('WHEN'):
            args.append(self.parse_expr())
            self.expect_kw('THEN')
            args.append(self.parse_expr())
        els = None
        if self.accept_kw('ELSE'):
            els = self.parse_expr()
        args.append(els)
        self.expect_kw('END')
        return ('op', 'CASE', tuple(args))

    def parse_type_name(self):
        t = self.next()
        if t.kind not in ('IDENT', 'QIDENT'):
            raise QueryError('Expected type name in CAST')
        name = t.value.upper()
        if name == 'DOUBLE':
            self.accept_kw('PRECISION')
        if name in ('CHARACTER', 'CHAR') and self.accept_kw('VARYING'):
            name = 'VARCHAR'
        if self.accept_op('('):
            while not self.accept_op(')'):
                if self.peek().kind == 'EOF':
                    raise QueryError('Unterminated type in CAST')
                self.next()
        return name

    def parse_primary(self):
        t = self.peek()
        if t.kind == 'OP' and t.value == '(':
            if self.starts_query(1):
                return ('subquery', self.parse_subquery_parens())
            self.next()
            e = self.parse_expr()
            self.expect_op(')')
            return e
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
        if t.kind == 'IDENT' and t.value.upper() == 'CASE':
            self.next()
            return self.parse_case()
        if t.kind == 'IDENT' and t.value.upper() == 'EXISTS' and self.is_op('(', 1):
            self.next()
            if not self.starts_query(1):
                raise SubqueryError(INVALID_REF, 'EXISTS requires a subquery')
            return ('exists', self.parse_subquery_parens())
        if t.kind == 'IDENT' and t.value.upper() == 'CAST' and self.is_op('(', 1):
            self.next()
            self.next()
            e = self.parse_expr()
            self.expect_kw('AS')
            ty = self.parse_type_name()
            self.expect_op(')')
            return ('op', 'CAST:' + ty, (e,))
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
                return ('win', name, None, False) + spec + (None,)
            if name not in AGGREGATES:
                if self.followed_by_over(self.pos + 1):
                    raise WindowError(INVALID_SPEC,
                                      'Unsupported window function: %s' % t.value)
                if name in SCALAR_FUNCS:
                    self.next()
                    self.next()
                    args = []
                    if not self.accept_op(')'):
                        args.append(self.parse_expr())
                        while self.accept_op(','):
                            args.append(self.parse_expr())
                        self.expect_op(')')
                    lo, hi = SCALAR_FUNCS[name]
                    if len(args) < lo or (hi is not None and len(args) > hi):
                        raise QueryError('Wrong number of arguments to %s' % name)
                    return ('op', 'F:' + name, tuple(args))
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
            filt = None
            if self.is_kw('FILTER') and self.is_op('(', 1):
                self.next()
                self.next()
                self.expect_kw('WHERE')
                filt = self.parse_expr()
                self.expect_op(')')
            if self.accept_kw('OVER'):
                spec = self.parse_window_spec()
                return ('win', name, arg, distinct) + spec + (filt,)
            return ('agg', name, arg, distinct, filt)
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


NULL_TOKENS = ('NULL', 'null')


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
        raw.append([None if c == '' or c in NULL_TOKENS else c for c in row])
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

    def has(self, name):
        try:
            self.resolve_name(name)
            return True
        except QueryError:
            return False

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
    def __init__(self, parent=None, kind='main', ctx=None):
        self.tables = []   # dicts: name, alias, columns, offset, derived
        self.width = 0
        self.parent = parent
        self.kind = kind
        self.ctx = ctx
        self.outer_refs = set()   # (depth, index) references to enclosing scopes
        self.ast = None

    def add(self, name, alias, columns, derived=False):
        for t in self.tables:
            if (alias or name).lower() == (t['alias'] or t['name']).lower():
                raise QueryError('Duplicate table name/alias: %s' % (alias or name))
        self.tables.append({'name': name, 'alias': alias, 'columns': columns,
                            'offset': self.width, 'derived': derived})
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

    def resolve(self, parts, strict=True):
        """Return (index, column_name) or None.

        Unknown qualifiers return None (strict) - callers decide the error."""
        col = parts[-1]
        if len(parts) > 1:
            qual = '.'.join(parts[:-1])
            ts = self.match_table(qual)
            if not ts:
                return None
            hits = []
            for t in ts:
                i = self.find_col(t, col)
                if i is not None:
                    hits.append((t['offset'] + i, t['columns'][i]))
            if not hits:
                if any(t['derived'] for t in ts):
                    raise SubqueryError(INVALID_REF, 'Column %s is not provided by '
                                        'subquery %s' % (col, qual))
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

    def in_subquery(self):
        return self.parent is not None or self.kind == 'sub'


def nested_aliases(q):
    """Lower-cased table names/aliases used inside nested subqueries of q."""
    acc = set()

    def walk(o, top):
        if isinstance(o, dict):
            if o.get('kind') == 'select':
                for _, tref, on in o['sources']:
                    if not top:
                        acc.add((tref.get('alias') or tref.get('name') or '').lower())
                        if tref.get('name'):
                            acc.add(tref['name'].lower())
                    if 'subquery' in tref:
                        walk(tref['subquery'], False)
                    if on is not None:
                        walk(on, False)
                for k, v in o.items():
                    if k != 'sources':
                        walk(v, False)
                return
            for v in o.values():
                walk(v, False)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v, top)
    walk(q, True)
    return acc


def bind_column(parts, scope, aliases, use_alias):
    r = scope.resolve(parts)
    if r is not None:
        return ('idx', r[0])
    if use_alias and aliases and len(parts) == 1:
        a = lookup_alias(aliases, parts[0])
        if a is not None:
            return a
    s = scope.parent
    d = 1
    while s is not None:
        try:
            r = s.resolve(parts)
        except QueryError as e:
            raise SubqueryError(CORRELATED_ERR, 'Invalid outer reference %s in correlated '
                                'subquery: %s' % ('.'.join(parts), getattr(e, 'msg', e)))
        if r is not None:
            scope.outer_refs.add((d, r[0]))
            return ('outer', d, r[0])
        s = s.parent
        d += 1
    name = '.'.join(parts)
    if scope.in_subquery():
        if len(parts) > 1:
            raise SubqueryError(CORRELATED_ERR, 'Unknown table or alias %s referenced in '
                                'subquery (column %s)' % ('.'.join(parts[:-1]), name))
        raise SubqueryError(CORRELATED_ERR, 'Unknown column %s referenced in subquery'
                            % name)
    if len(parts) > 1:
        qual = '.'.join(parts[:-1]).lower()
        if scope.kind == 'derived':
            raise SubqueryError(INVALID_REF, 'Derived table cannot reference %s' % name)
        if scope.ast is not None and qual in nested_aliases(scope.ast):
            raise SubqueryError(INVALID_REF, 'Table %s is only visible inside its '
                                'subquery (column %s)' % ('.'.join(parts[:-1]), name))
        raise QueryError('Unknown table or alias: %s' % '.'.join(parts[:-1]))
    if any(t['derived'] for t in scope.tables):
        raise SubqueryError(INVALID_REF, 'Unknown column %s (not provided by derived '
                            'table)' % name)
    raise QueryError('Unknown column: %s' % name)


def bind_subquery(q, scope, single_column):
    plan = make_plan(q, scope.ctx, scope, 'sub')
    if single_column and len(plan.names) != 1:
        raise SubqueryError(INVALID_REF, 'Subquery must return exactly one column, got %d'
                            % len(plan.names))
    for d, i in plan.refs:
        if d >= 2:
            scope.outer_refs.add((d - 1, i))
    return plan, tuple(sorted(plan.refs))


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
        return bind_column(parts, scope, aliases, not in_agg and not in_win)
    if k == 'agg':
        if not allow_agg:
            raise QueryError('Aggregate functions are not allowed here')
        if in_agg:
            raise QueryError('Nested aggregate functions are not allowed')
        arg = expr[2]
        if arg != '*':
            arg = bind(arg, scope, None, False, allow_agg, True, allow_win, in_win)
        filt = expr[4]
        if filt is not None:
            filt = bind(filt, scope, None, False, allow_agg, True, allow_win, in_win)
        return ('agg', expr[1], arg, expr[3], filt)
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
        _, name, arg, distinct, part, order, frame, filt = expr

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
        if filt is not None:
            filt = bind(filt, scope, None, False, False, True, True, True)
        bpart = tuple(bw(e, True) for e in part)
        border = tuple((bw(e, True), d, nf) for e, d, nf in order)
        if frame is not None and frame[0] == 'RANGE' and len(border) != 1 and \
                (frame[1][0] in ('P', 'F') or frame[2][0] in ('P', 'F')):
            raise WindowError(FRAME_ERROR, 'RANGE with offset requires exactly one '
                              'ORDER BY expression')
        return ('win', name, arg, distinct, bpart, border, frame, filt)
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
    if k == 'op':
        return ('op', expr[1], tuple(None if a is None else b(a) for a in expr[2]))
    if k == 'subquery':
        plan, refs = bind_subquery(expr[1], scope, True)
        return ('subq', plan, refs, {})
    if k == 'exists':
        plan, refs = bind_subquery(expr[1], scope, False)
        return ('exists_b', plan, refs, {})
    if k == 'in_sub':
        left = b(expr[1])
        plan, refs = bind_subquery(expr[2], scope, True)
        return ('insub', left, plan, refs, {}, expr[3])
    if k == 'quant':
        left = b(expr[2])
        plan, refs = bind_subquery(expr[3], scope, True)
        return ('quant_b', expr[1], left, plan, refs, {}, expr[4])
    if k in ('idx', 'outer', 'subq', 'exists_b', 'insub', 'quant_b'):
        return expr
    raise QueryError('Unsupported expression')


def win_children(expr):
    """Sub-expressions of a bound window node (argument, partition, order)."""
    out = []
    if expr[2] is not None and expr[2] != '*':
        out.append(expr[2])
    out.extend(expr[4])
    out.extend(e for e, _, _ in expr[5])
    if expr[7] is not None:
        out.append(expr[7])
    return out


def expr_children(expr):
    """Direct sub-expressions evaluated in the current row context."""
    k = expr[0]
    if k == 'cmp':
        return [expr[2], expr[3]]
    if k == 'like':
        return [expr[1], expr[2]]
    if k == 'in':
        return [expr[1]] + list(expr[2])
    if k in ('isnull', 'not'):
        return [expr[1]]
    if k in ('and', 'or'):
        return [expr[1], expr[2]]
    if k == 'op':
        return [a for a in expr[2] if a is not None]
    if k == 'insub':
        return [expr[1]]
    if k == 'quant_b':
        return [expr[2]]
    return []


def find_windows(expr, acc):
    k = expr[0]
    if k == 'win':
        if all(x is not expr for x in acc):
            acc.append(expr)
        return acc
    for c in expr_children(expr):
        find_windows(c, acc)
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
    if k == 'win':
        return any(has_agg(x) for x in win_children(expr))
    return any(has_agg(c) for c in expr_children(expr))


def check_grouped(expr, group_idx, group_exprs, what):
    """Ensure every column outside aggregates is part of GROUP BY."""
    if expr in group_exprs:
        return
    k = expr[0]
    if k in ('lit', 'agg', 'outer'):
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
    for c in expr_children(expr):
        check_grouped(c, group_idx, group_exprs, what)


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
    if k == 'outer':
        r = OUTER_ROWS[-expr[1]] if len(OUTER_ROWS) >= expr[1] else None
        return None if r is None else r[expr[2]]
    if k == 'op':
        return eval_op(expr, row, group, wv)
    if k == 'subq':
        plan = expr[1]
        rows = subquery_rows(plan, expr[2], expr[3], row)
        if len(rows) > 1:
            raise SubqueryError(SCALAR_ROWS, 'Scalar subquery returned %d rows (expected '
                                'at most one)' % len(rows))
        v = rows[0][0] if rows else None
        if expr[2] and isinstance(v, int) and not isinstance(v, bool) and \
                getattr(plan, 'single_avg', False):
            v = Decimal(v).quantize(Decimal('0.1'))
        return v
    if k == 'exists_b':
        return len(subquery_rows(expr[1], expr[2], expr[3], row)) > 0
    if k == 'insub':
        v = evaluate(expr[1], row, group, wv)
        rows = subquery_rows(expr[2], expr[3], expr[4], row)
        neg = expr[5]
        if not rows:
            return neg
        if v is None:
            return None
        saw_null = False
        for r in rows:
            xv = r[0]
            if xv is None:
                saw_null = True
            elif cmp_values(v, xv) == 0:
                return not neg
        if saw_null:
            return None
        return neg
    if k == 'quant_b':
        _, op, left, plan, refs, memo, kind = expr
        v = evaluate(left, row, group, wv)
        rows = subquery_rows(plan, refs, memo, row)
        if not rows:
            return kind == 'ALL'
        saw_null = False
        for r in rows:
            c = sql_compare(op, v, r[0])
            if c is None:
                saw_null = True
            elif kind == 'ANY' and c:
                return True
            elif kind == 'ALL' and not c:
                return False
        if saw_null:
            return None
        return kind == 'ALL'
    raise QueryError('Cannot evaluate expression')


OUTER_ROWS = []


def subquery_rows(plan, refs, memo, row):
    key = []
    for d, i in refs:
        if d == 1:
            r = row
        else:
            r = OUTER_ROWS[-(d - 1)] if len(OUTER_ROWS) >= d - 1 else None
        v = None if r is None else r[i]
        key.append(hash_key(v) if not isinstance(v, bool) else ('b', v))
    key = tuple(key)
    if key in memo:
        return memo[key]
    OUTER_ROWS.append(row)
    try:
        rows = plan.run()
    finally:
        OUTER_ROWS.pop()
    memo[key] = rows
    return rows


def num_arg(v, what):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    n = v if is_num(v) else to_number(v)
    if n is None:
        raise QueryError('%s requires numeric values, got %r' % (what, v))
    return n


def normalize_num(d):
    if isinstance(d, Decimal):
        return round_sig(d, 28) if d.adjusted() > 28 else d
    return d


def arith(op, a, b):
    if a is None or b is None:
        return None
    x = num_arg(a, "operator '%s'" % op)
    y = num_arg(b, "operator '%s'" % op)
    if op == '+':
        return x + y
    if op == '-':
        return x - y
    if op == '*':
        return x * y
    if y == 0:
        raise QueryError('Division by zero')
    if op == '/':
        if isinstance(x, int) and isinstance(y, int) and x % y == 0:
            return x // y
        with localcontext() as ctx:
            ctx.prec = 40
            q = Decimal(x) / Decimal(y)
        return round_sig(q, 15)
    # modulo, sign follows the dividend
    if isinstance(x, int) and isinstance(y, int):
        r = abs(x) % abs(y)
        return -r if x < 0 else r
    return Decimal(x) % Decimal(y)


def to_text(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return fmt_value(v)


def do_cast(ty, v):
    if v is None:
        return None
    if ty in ('INT', 'INTEGER', 'BIGINT', 'SMALLINT', 'TINYINT'):
        if isinstance(v, bool):
            return int(v)
        n = num_arg(v, 'CAST')
        return int(n)
    if ty in ('DECIMAL', 'NUMERIC', 'FLOAT', 'REAL', 'DOUBLE', 'NUMBER'):
        n = num_arg(v, 'CAST')
        if ty in ('FLOAT', 'REAL', 'DOUBLE') and isinstance(n, int):
            return Decimal(n).quantize(Decimal('0.1'))
        return n
    if ty in ('TEXT', 'VARCHAR', 'CHAR', 'STRING', 'CHARACTER'):
        return to_text(v)
    if ty in ('BOOLEAN', 'BOOL'):
        if isinstance(v, bool):
            return v
        if is_num(v):
            return v != 0
        s = str(v).strip().lower()
        if s in ('true', 't', '1', 'yes'):
            return True
        if s in ('false', 'f', '0', 'no'):
            return False
        raise QueryError('Cannot cast %r to BOOLEAN' % v)
    raise QueryError('Unsupported CAST type: %s' % ty)


def eval_op(expr, row, group, wv):
    _, name, args = expr

    def ev(e):
        return None if e is None else evaluate(e, row, group, wv)
    if name in ('+', '-', '*', '/', '%'):
        return arith(name, ev(args[0]), ev(args[1]))
    if name == 'neg':
        v = ev(args[0])
        if v is None:
            return None
        return -num_arg(v, 'unary minus')
    if name == '||':
        a, b = ev(args[0]), ev(args[1])
        if a is None or b is None:
            return None
        return to_text(a) + to_text(b)
    if name == 'CASE':
        operand = args[0]
        ov = ev(operand) if operand is not None else None
        i = 1
        while i + 1 < len(args):
            if operand is not None:
                hit = sql_compare('=', ov, ev(args[i])) is True
            else:
                c = ev(args[i])
                hit = c is not None and truthy(c)
            if hit:
                return ev(args[i + 1])
            i += 2
        return ev(args[-1])
    if name == 'BETWEEN':
        v, lo, hi = ev(args[0]), ev(args[1]), ev(args[2])
        a = sql_compare('>=', v, lo)
        b = sql_compare('<=', v, hi)
        if a is False or b is False:
            return False
        if a is None or b is None:
            return None
        return True
    if name in ('DISTINCTFROM', 'NOTDISTINCT'):
        a, b = ev(args[0]), ev(args[1])
        if a is None or b is None:
            same = a is None and b is None
        else:
            same = cmp_values(a, b) == 0
        return same if name == 'NOTDISTINCT' else not same
    if name.startswith('IS') and name[2:6] in ('TRUE', 'FALS'):
        v = ev(args[0])
        want = name.startswith('ISTRUE')
        res = v is not None and truthy(v) == want
        return (not res) if name.endswith('_NOT') else res
    if name.startswith('CAST:'):
        return do_cast(name[5:], ev(args[0]))
    fn = name[2:]
    vals = [ev(a) for a in args]
    if fn in ('COALESCE', 'IFNULL'):
        for v in vals:
            if v is not None:
                return v
        return None
    if fn == 'NULLIF':
        if vals[0] is not None and vals[1] is not None and cmp_values(vals[0], vals[1]) == 0:
            return None
        return vals[0]
    if fn in ('GREATEST', 'LEAST'):
        best = None
        for v in vals:
            if v is None:
                continue
            if best is None or (cmp_values(v, best) > 0) == (fn == 'GREATEST') and \
                    cmp_values(v, best) != 0:
                best = v
        return best
    if fn == 'CONCAT':
        return ''.join(to_text(v) for v in vals if v is not None)
    if any(v is None for v in vals):
        return None
    if fn == 'ABS':
        return abs(num_arg(vals[0], fn))
    if fn == 'MOD':
        return arith('%', vals[0], vals[1])
    if fn == 'ROUND':
        n = num_arg(vals[0], fn)
        places = int(num_arg(vals[1], fn)) if len(vals) > 1 else 0
        if isinstance(n, int) and places >= 0:
            return n
        q = Decimal(n).quantize(Decimal(1).scaleb(-places), rounding='ROUND_HALF_UP')
        if places <= 0:
            return int(q)
        return q
    if fn in ('FLOOR', 'CEIL', 'CEILING'):
        n = num_arg(vals[0], fn)
        if isinstance(n, int):
            return n
        return int(n.to_integral_value(rounding='ROUND_FLOOR' if fn == 'FLOOR'
                                       else 'ROUND_CEILING'))
    if fn == 'UPPER':
        return to_text(vals[0]).upper()
    if fn == 'LOWER':
        return to_text(vals[0]).lower()
    if fn == 'LENGTH':
        return len(to_text(vals[0]))
    if fn == 'TRIM':
        return to_text(vals[0]).strip()
    if fn == 'LTRIM':
        return to_text(vals[0]).lstrip()
    if fn == 'RTRIM':
        return to_text(vals[0]).rstrip()
    if fn in ('SUBSTR', 'SUBSTRING'):
        s = to_text(vals[0])
        start = int(num_arg(vals[1], fn))
        if len(vals) > 2:
            ln = int(num_arg(vals[2], fn))
            end = start + ln
        else:
            end = None
        i0 = max(start - 1, 0)
        if end is None:
            return s[i0:]
        return s[i0:max(end - 1, 0)]
    if fn == 'REPLACE':
        return to_text(vals[0]).replace(to_text(vals[1]), to_text(vals[2]))
    raise QueryError('Unsupported function: %s' % fn)


def truthy(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if is_num(v):
        return v != 0
    return bool(v)


def eval_agg(expr, group):
    _, name, arg, distinct, filt = expr
    if filt is not None:
        group = [r for r in group if evaluate(filt, r, None) is True]
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
        _, name, arg, distinct, part, ordby, frame, filt = node
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
        if filt is not None:
            mask = [evaluate(filt, r, g) is True for r, g in contexts]
            if argvals is None:
                argvals = [1 if m_ else None for m_ in mask]
            else:
                argvals = [v if m_ else None for v, m_ in zip(argvals, mask)]
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


def row_key(r):
    return tuple(('b', v) if isinstance(v, bool) else hash_key(v) for v in r)


def dedupe_rows(rows):
    seen = set()
    out = []
    for r in rows:
        k = row_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def table_refs(q):
    """Lower-cased table names referenced in FROM clauses anywhere inside q
    (names defined by nested WITH clauses are excluded)."""
    acc = set()

    def walk(o):
        if isinstance(o, dict):
            kind = o.get('kind')
            if kind == 'with':
                names = {c['name'].lower() for c in o['with']['ctes']}
                sub = set()
                for c in o['with']['ctes']:
                    sub |= table_refs(c['query'])
                sub |= table_refs(o['body'])
                acc.update(sub - names)
                return
            if kind == 'select':
                for _, tref, _ in o['sources']:
                    if 'name' in tref:
                        acc.add(tref['name'].lower())
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    walk(q)
    return acc


class Ctx:
    """Name resolution context for tables: CTE definitions + catalog."""

    def __init__(self, catalog, parent=None, with_active=False):
        self.catalog = catalog
        self.parent = parent
        self.ctes = {}
        self.with_active = with_active or (parent.with_active if parent else False)

    def find_cte(self, name):
        c = self
        low = name.lower()
        while c is not None:
            d = c.ctes.get(low)
            if d is not None:
                return d
            c = c.parent
        return None

    def lookup(self, name):
        d = self.find_cte(name)
        if d is not None:
            header, rows = d.materialize()
            return d.name, header, rows
        if not self.catalog.has(name):
            if self.with_active:
                raise CTEError(CTE_NOT_FOUND, 'CTE or table not found: %s' % name)
            raise QueryError('Table not found: %s' % name)
        n, (h, r) = self.catalog.load(name)
        return n, h, r


class FixedTable:
    def __init__(self, name, names, rows):
        self.name, self.names, self.rows = name, names, rows

    def materialize(self):
        return self.names, self.rows


class CTEDef:
    def __init__(self, name, cols, query, ctx, recursive):
        self.name = name
        self.cols = cols
        self.query = query
        self.ctx = ctx
        self.recursive = recursive
        self.result = None
        self.busy = False

    def apply_cols(self, names):
        if self.cols is None:
            return list(names)
        if len(self.cols) != len(names):
            raise CTEError(INVALID_CTE, 'CTE %s has %d columns but its column list '
                           'specifies %d' % (self.name, len(names), len(self.cols)))
        return list(self.cols)

    def materialize(self):
        if self.result is not None:
            return self.result
        if self.busy:
            raise CTEError(CYCLIC, 'CTE %s references itself' % self.name)
        self.busy = True
        try:
            if self.recursive:
                names, rows = self.run_recursive()
            else:
                plan = make_plan(self.query, self.ctx, None, 'cte')
                rows = plan.run()
                names = self.apply_cols(plan.names)
        finally:
            self.busy = False
        self.result = (names, rows)
        return self.result

    def run_recursive(self):
        low = self.name.lower()

        def flat(x):
            if x['kind'] == 'setop' and x['op'] == 'UNION' and not x.get('paren'):
                if x['order_by'] or x['limit'] is not None or x['offset'] is not None:
                    raise CTEError(INVALID_CTE, 'ORDER BY / LIMIT are not allowed in '
                                   'recursive CTE %s' % self.name)
                return flat(x['left']) + [(x['right'], x['all'])]
            return [(x, True)]
        parts = flat(self.query)
        is_rec = [low in table_refs(b) for b, _ in parts]
        if len(parts) < 2:
            raise CTEError(INVALID_CTE, 'Recursive CTE %s must have the form '
                           '<anchor> UNION [ALL] <recursive member>' % self.name)
        if is_rec[0]:
            raise CTEError(INVALID_CTE, 'The anchor member of recursive CTE %s must not '
                           'reference itself' % self.name)
        first_rec = is_rec.index(True)
        if not all(is_rec[first_rec:]):
            raise CTEError(INVALID_CTE, 'Non-recursive members of CTE %s must precede the '
                           'recursive members' % self.name)
        distinct = any(not a for _, a in parts[1:])
        anchors = [b for b, _ in parts[:first_rec]]
        recs = [b for b, _ in parts[first_rec:]]
        names = None
        result = []
        for a in anchors:
            p = make_plan(a, self.ctx, None, 'cte')
            if names is None:
                names = self.apply_cols(p.names)
            elif len(p.names) != len(names):
                raise CTEError(INVALID_CTE, 'All members of recursive CTE %s must return '
                               'the same number of columns' % self.name)
            result.extend(p.run())
        seen = set()
        if distinct:
            result = dedupe_rows(result)
            seen = {row_key(r) for r in result}
        work = list(result)
        depth = 0
        while work:
            wctx = Ctx(self.ctx.catalog, self.ctx, True)
            wctx.ctes[low] = FixedTable(self.name, names, work)
            new = []
            for b in recs:
                p = make_plan(b, wctx, None, 'cte')
                if len(p.names) != len(names):
                    raise CTEError(INVALID_CTE, 'All members of recursive CTE %s must '
                                   'return the same number of columns' % self.name)
                new.extend(p.run())
            if distinct:
                fresh = []
                for r in new:
                    k = row_key(r)
                    if k not in seen:
                        seen.add(k)
                        fresh.append(r)
                new = fresh
            if not new:
                break
            depth += 1
            if depth > MAX_RECURSION:
                raise CTEError(DEPTH_EXCEEDED, 'Recursive CTE %s exceeded the maximum '
                               'recursion depth of %d' % (self.name, MAX_RECURSION))
            result.extend(new)
            if len(result) > MAX_RECURSIVE_ROWS:
                raise CTEError(DEPTH_EXCEEDED, 'Recursive CTE %s produced more than %d rows'
                               % (self.name, MAX_RECURSIVE_ROWS))
            work = new
        return names, result


def find_cycle(edges, allow_self):
    """Return a list of node indices forming a cycle, or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    stack = []

    def dfs(n):
        color[n] = GREY
        stack.append(n)
        for m in sorted(edges[n]):
            if m == n and allow_self:
                continue
            if color[m] == GREY:
                return stack[stack.index(m):] + [m]
            if color[m] == WHITE:
                r = dfs(m)
                if r:
                    return r
        stack.pop()
        color[n] = BLACK
        return None
    for n in sorted(edges):
        if color[n] == WHITE:
            r = dfs(n)
            if r:
                return r
    return None


def make_with_plan(q, ctx, parent, kind):
    w = q['with']
    ctes = w['ctes']
    rec = w['recursive']
    names = [c['name'].lower() for c in ctes]
    seen = set()
    for c in ctes:
        if c['name'].lower() in seen:
            raise CTEError(INVALID_CTE, 'CTE name %s specified more than once' % c['name'])
        seen.add(c['name'].lower())
    pos = {n: i for i, n in enumerate(names)}
    refs = [table_refs(c['query']) & seen for c in ctes]
    edges = {i: set() for i in range(len(ctes))}
    for i, r in enumerate(refs):
        for n in r:
            j = pos[n]
            if not rec and j >= i and (ctx.find_cte(n) is not None or ctx.catalog.has(n)):
                continue   # refers to an existing table / outer CTE of the same name
            edges[i].add(j)
    cyc = find_cycle(edges, rec)
    if cyc:
        raise CTEError(CYCLIC, 'Cyclic reference between CTEs: %s'
                       % ' -> '.join(ctes[i]['name'] for i in cyc))
    if not rec:
        for i in edges:
            for j in edges[i]:
                if j > i:
                    raise CTEError(CTE_NOT_FOUND, 'CTE %s is referenced by %s before it is '
                                   'defined' % (ctes[j]['name'], ctes[i]['name']))
    if rec:
        shared = Ctx(ctx.catalog, ctx, True)
        for i, c in enumerate(ctes):
            shared.ctes[names[i]] = CTEDef(c['name'], c['cols'], c['query'], shared,
                                           i in edges[i])
        body_ctx = shared
    else:
        cur = Ctx(ctx.catalog, ctx, True)
        for i, c in enumerate(ctes):
            d = CTEDef(c['name'], c['cols'], c['query'], cur, False)
            cur = Ctx(ctx.catalog, cur, True)
            cur.ctes[names[i]] = d
        body_ctx = cur
    return make_plan(q['body'], body_ctx, parent, kind)


def make_plan(q, ctx, parent, kind):
    k = q['kind']
    if k == 'with':
        return make_with_plan(q, ctx, parent, kind)
    if k == 'setop':
        return SetopPlan(q, ctx, parent, kind)
    return SelectPlan(q, ctx, parent, kind)


def finish_rows(results, order_specs, offset, limit):
    if order_specs:
        c = order_cmp(order_specs)
        results.sort(key=functools.cmp_to_key(lambda a, b: c(a[1], b[1])))
    out_rows = [v for v, _ in results]
    if offset:
        out_rows = out_rows[offset:]
    if limit is not None:
        out_rows = out_rows[:limit]
    return out_rows


class SetopPlan:
    def __init__(self, q, ctx, parent, kind):
        self.q = q
        self.left = make_plan(q['left'], ctx, parent, kind)
        self.right = make_plan(q['right'], ctx, parent, kind)
        if len(self.left.names) != len(self.right.names):
            raise QueryError('Each %s query must have the same number of columns'
                             % q['op'])
        self.names = list(self.left.names)
        self.refs = self.left.refs | self.right.refs
        scope = Scope(None, 'main', ctx)
        scope.add('', None, self.names)
        self.order = []
        for e, desc, nf in q['order_by']:
            if e[0] == 'lit' and isinstance(e[1], int) and not isinstance(e[1], bool):
                if not 1 <= e[1] <= len(self.names):
                    raise QueryError('ORDER BY position %d out of range' % e[1])
                be = ('idx', e[1] - 1)
            else:
                be = bind(e, scope, allow_agg=False)
            self.order.append((be, desc, nf))

    def run(self):
        a = self.left.run()
        b = self.right.run()
        op, all_ = self.q['op'], self.q['all']
        if op == 'UNION':
            rows = a + b if all_ else dedupe_rows(a + b)
        else:
            counts = {}
            for r in b:
                k = row_key(r)
                counts[k] = counts.get(k, 0) + 1
            rows = []
            seen = set()
            for r in a:
                k = row_key(r)
                inb = counts.get(k, 0) > 0
                if all_:
                    if op == 'INTERSECT' and inb:
                        counts[k] -= 1
                        rows.append(r)
                    elif op == 'EXCEPT':
                        if inb:
                            counts[k] -= 1
                        else:
                            rows.append(r)
                else:
                    if k in seen:
                        continue
                    if (op == 'INTERSECT') == inb:
                        seen.add(k)
                        rows.append(r)
        results = [(r, tuple(evaluate(e, r, None) for e, _, _ in self.order)) for r in rows]
        return finish_rows(results, [(d, nf) for _, d, nf in self.order],
                           self.q['offset'], self.q['limit'])


class SelectPlan:
    def __init__(self, q, ctx, parent, kind):
        self.q = q
        scope = Scope(parent, kind, ctx)
        scope.ast = q
        self.scope = scope
        self.sources = []
        self.from_static = True
        self.cached = None
        for jtype, tref, on in q['sources']:
            left_width = scope.width
            if 'subquery' in tref:
                sub = make_plan(tref['subquery'], ctx, parent, 'derived')
                cols = list(sub.names)
                if tref['cols']:
                    if len(tref['cols']) != len(cols):
                        raise SubqueryError(INVALID_REF, 'Derived table %s has %d columns '
                                            'but %d column aliases were given'
                                            % (tref['alias'], len(cols), len(tref['cols'])))
                    cols = list(tref['cols'])
                scope.add(tref['alias'], tref['alias'], cols, derived=True)
                if sub.refs:
                    scope.outer_refs |= sub.refs
                    self.from_static = False
                    src = ('plan', sub)
                else:
                    src = ('rows', sub.run())
            else:
                name, cols, rows = ctx.lookup(tref['name'])
                if tref.get('cols'):
                    if len(tref['cols']) != len(cols):
                        raise QueryError('Table %s has %d columns but %d column aliases '
                                         'were given' % (name, len(cols), len(tref['cols'])))
                    cols = list(tref['cols'])
                scope.add(name, tref['alias'], cols)
                src = ('rows', rows)
            bon = None
            if on is not None:
                before = set(scope.outer_refs)
                bon = bind(on, scope, allow_agg=False)
                if scope.outer_refs != before or contains_subquery(bon):
                    self.from_static = False
            self.sources.append((jtype, src, len(cols), bon, left_width))

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
                r = scope.resolve(item['expr'][1]) if e[0] == 'idx' else None
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
        if grouped:
            group_idx = {e[1] for e in group_exprs if e[0] == 'idx'}
            for e in out_exprs:
                check_grouped(e, group_idx, group_exprs, 'SELECT')
            if having is not None:
                check_grouped(having, group_idx, group_exprs, 'HAVING')
            for e, _, _ in order_exprs:
                check_grouped(e, group_idx, group_exprs, 'ORDER BY')
        win_nodes = []
        for e in out_exprs:
            find_windows(e, win_nodes)
        for e, _, _ in order_exprs:
            find_windows(e, win_nodes)

        self.names = out_names
        self.out_exprs = out_exprs
        self.where = where
        self.group_exprs = group_exprs
        self.having = having
        self.order_exprs = order_exprs
        self.grouped = grouped
        self.win_nodes = win_nodes
        self.refs = scope.outer_refs
        self.single_avg = len(out_exprs) == 1 and out_exprs[0][0] == 'agg' and \
            out_exprs[0][1] == 'AVG'
        # correlated equality predicates (inner_col = outer_ref) -> hash lookup
        self.index_keys = []
        self.index = None
        if where is not None and self.from_static:
            for c in conjuncts(where):
                if c[0] == 'cmp' and c[1] == '=':
                    a, b = c[2], c[3]
                    if a[0] == 'outer' and b[0] == 'idx':
                        a, b = b, a
                    if a[0] == 'idx' and b[0] == 'outer':
                        self.index_keys.append((a[1], b))

    def base_rows(self):
        if self.cached is not None:
            return self.cached
        if not self.sources:
            rows = [()]
        else:
            rows = None
            for jtype, src, width, bon, left_width in self.sources:
                srows = src[1] if src[0] == 'rows' else src[1].run()
                if rows is None:
                    rows = list(srows)
                else:
                    rows = do_join(rows, left_width, srows, width, jtype, bon)
        if self.from_static:
            self.cached = rows
        return rows

    def run(self):
        rows = self.base_rows()
        if self.index_keys:
            if self.index is None:
                self.index = {}
                for r in rows:
                    k = tuple(r[i] for i, _ in self.index_keys)
                    if any(v is None for v in k):
                        continue
                    self.index.setdefault(tuple(hash_key(v) for v in k), []).append(r)
            k = tuple(evaluate(o, None, None) for _, o in self.index_keys)
            if any(v is None for v in k):
                rows = []
            else:
                rows = self.index.get(tuple(hash_key(v) for v in k), [])
        where = self.where
        group_exprs = self.group_exprs
        having = self.having
        out_exprs = self.out_exprs
        order_exprs = self.order_exprs
        if where is not None:
            rows = [r for r in rows if evaluate(where, r, None) is True]

        contexts = []
        if self.grouped:
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

        if self.win_nodes:
            corder, wvals = compute_windows(self.win_nodes, contexts)
            seq = [(contexts[i], wvals[i]) for i in corder]
        else:
            seq = [(c, None) for c in contexts]

        results = []
        for (row, grp), wv in seq:
            vals = tuple(evaluate(e, row, grp, wv) for e in out_exprs)
            okeys = tuple(evaluate(e, row, grp, wv) for e, _, _ in order_exprs)
            results.append((vals, okeys))

        if self.q['distinct']:
            seen = set()
            uniq = []
            for vals, okeys in results:
                hk = row_key(vals)
                if hk not in seen:
                    seen.add(hk)
                    uniq.append((vals, okeys))
            results = uniq
        return finish_rows(results, [(d, nf) for _, d, nf in order_exprs],
                           self.q['offset'], self.q['limit'])


def conjuncts(expr):
    if expr[0] == 'and':
        return conjuncts(expr[1]) + conjuncts(expr[2])
    return [expr]


def contains_subquery(expr):
    if expr[0] in ('subq', 'exists_b', 'insub', 'quant_b'):
        return True
    if expr[0] == 'agg':
        return any(contains_subquery(x) for x in (expr[2], expr[4])
                   if x is not None and x != '*')
    if expr[0] == 'win':
        return any(contains_subquery(x) for x in win_children(expr))
    return any(contains_subquery(c) for c in expr_children(expr))


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
    ctx = Ctx(Catalog(data, sharded))
    plan = make_plan(q, ctx, None, 'main')
    return plan.names, plan.run()


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
        return e.code
    except RecursionError:
        print('Error: CTE_ERROR: RECURSIVE_DEPTH_EXCEEDED: query nesting too deep',
              file=sys.stderr)
        return CTEError.code
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
