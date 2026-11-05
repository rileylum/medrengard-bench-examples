#!/usr/bin/env python3
"""LogQL Parts 1-4: query NDJSON logs with a tiny SQL-like language
(GROUP BY, aggregates, CONFLATE joins, GLOSS canonical labels)."""
import json
import math
import re
import sys

KEYWORDS = {"SELECT", "FROM", "WHERE", "AND", "OR", "TRUE", "FALSE", "NULL"}
RESERVED_IDENTS = {"SELECT", "FROM", "WHERE", "AND", "OR"}
AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "UNIQUE"}


class QueryError(Exception):
    def __init__(self, code, message, position=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.position = position


def fail(code, message, position=None):
    raise QueryError(code, message, position)


# ---------------------------------------------------------------- tokenizer

IDENT_START = re.compile(r"[A-Za-z_]")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")


class Tok:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind, value, pos):
        self.kind = kind  # IDENT, KW, STRING, NUMBER, OP, COMMA, DOT, LPAREN, RPAREN, STAR, EOF
        self.value = value
        self.pos = pos

    def __repr__(self):
        return "Tok(%s,%r,%d)" % (self.kind, self.value, self.pos)


def describe(tok):
    if tok.kind == "EOF":
        return "end of query"
    if tok.kind == "STRING":
        return "string literal"
    return repr(tok.value if tok.kind != "KW" else tok.value)


def tokenize(src):
    toks = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        start = i
        if IDENT_START.match(c):
            m = IDENT_RE.match(src, i)
            word = m.group(0)
            i = m.end()
            if word.upper() in KEYWORDS:
                toks.append(Tok("KW", word.upper(), start))
            else:
                toks.append(Tok("IDENT", word, start))
            continue
        if c == '"':
            i += 1
            buf = []
            while True:
                if i >= n:
                    fail("E_PARSE", "unterminated string literal", start)
                ch = src[i]
                if ch == '"':
                    i += 1
                    break
                if ch == "\\":
                    if i + 1 >= n:
                        fail("E_PARSE", "unterminated string literal", start)
                    nxt = src[i + 1]
                    if nxt in ('"', "\\"):
                        buf.append(nxt)
                        i += 2
                        continue
                    fail("E_PARSE", "invalid escape sequence '\\%s' in string literal" % nxt, i)
                buf.append(ch)
                i += 1
            toks.append(Tok("STRING", "".join(buf), start))
            continue
        if c == "-" or c.isdigit():
            m = NUMBER_RE.match(src, i)
            if not m:
                fail("E_PARSE", "invalid number literal", start)
            end = m.end()
            # reject things like 01, 1., 1e5, 1abc, 1.2.3
            if end < n and (src[end].isalnum() or src[end] in "._"):
                fail("E_PARSE", "invalid number literal", start)
            text = m.group(0)
            if m.group(2):
                val = float(text)
            else:
                val = int(text)
            toks.append(Tok("NUMBER", val, start))
            i = end
            continue
        if c in "<>!=":
            two = src[i:i + 2]
            if two in ("<=", ">=", "!="):
                toks.append(Tok("OP", two, start))
                i += 2
                continue
            if c == "!":
                fail("E_PARSE", "unexpected character '!'", start)
            toks.append(Tok("OP", c, start))
            i += 1
            continue
        if c == ":":
            if src[i:i + 2] == ":=":
                toks.append(Tok("ASSIGN", ":=", start))
                i += 2
                continue
            fail("E_PARSE", "unexpected character ':'", start)
        single = {",": "COMMA", ".": "DOT", "(": "LPAREN", ")": "RPAREN", "*": "STAR",
                  "|": "PIPE", "{": "LBRACE", "}": "RBRACE"}
        if c in single:
            toks.append(Tok(single[c], c, start))
            i += 1
            continue
        fail("E_PARSE", "unexpected character %r" % c, start)
    toks.append(Tok("EOF", None, n))
    return toks


# ---------------------------------------------------------------- parser

class Parser:
    def __init__(self, src):
        self.toks = tokenize(src)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def advance(self):
        t = self.toks[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t

    def is_kw(self, word):
        t = self.peek()
        return t.kind == "KW" and t.value == word

    def expect_kw(self, word):
        t = self.peek()
        if not (t.kind == "KW" and t.value == word):
            fail("E_PARSE", "expected %s but found %s" % (word, describe(t)), t.pos)
        return self.advance()

    def parse_query(self):
        self.expect_kw("SELECT")
        select = self.parse_select_list()
        self.expect_kw("FROM")
        t = self.peek()
        if t.kind == "IDENT":
            self.advance()
            source = (t.value, t.pos)
        elif t.kind == "KW":
            fail("E_UNKNOWN_TABLE", "unknown table %r" % t.value, t.pos)
        else:
            fail("E_PARSE", "expected table name but found %s" % describe(t), t.pos)
        where = None
        gloss = None
        conflates = []
        # Clause order per the grammar is WHERE, GLOSS, CONFLATE*; WHERE and GLOSS are
        # also accepted (once each) after CONFLATE clauses.
        while True:
            t = self.peek()
            if self.is_kw("WHERE"):
                if where is not None:
                    fail("E_PARSE", "duplicate WHERE clause", t.pos)
                self.advance()
                where = self.parse_or()
            elif self.is_word("GLOSS"):
                if gloss is not None:
                    fail("E_PARSE", "duplicate GLOSS clause", t.pos)
                gloss = self.parse_gloss()
            elif self.is_word("CONFLATE"):
                conflates.append(self.parse_conflate())
            else:
                break
        group = None
        if self.is_word("GROUP"):
            self.advance()
            t = self.peek()
            if not self.is_word("BY"):
                fail("E_PARSE", "expected BY but found %s" % describe(t), t.pos)
            self.advance()
            group = []
            while True:
                pos = self.peek().pos
                group.append((self.parse_field_ref(), pos))
                if self.peek().kind == "COMMA":
                    self.advance()
                    continue
                break
        t = self.peek()
        if t.kind != "EOF":
            fail("E_PARSE", "unexpected %s" % describe(t), t.pos)
        return {"select": select, "source": source, "where": where, "gloss": gloss,
                "conflates": conflates, "group": group}

    def parse_gloss(self):
        start = self.advance().pos  # GLOSS
        strict = False
        if self.is_word("STRICT"):
            self.advance()
            strict = True
        t = self.peek()
        if t.kind != "LBRACE":
            fail("E_PARSE", "expected '{' after GLOSS but found %s" % describe(t), t.pos)
        self.advance()
        decls = []
        while True:
            t = self.peek()
            if t.kind != "IDENT":
                fail("E_PARSE", "expected canonical label name but found %s" % describe(t), t.pos)
            self.advance()
            a = self.peek()
            if a.kind != "ASSIGN":
                fail("E_PARSE", "expected ':=' but found %s" % describe(a), a.pos)
            self.advance()
            cands = []
            while True:
                cpos = self.peek().pos
                cands.append((self.parse_field_ref(), cpos))
                if self.peek().kind == "PIPE":
                    self.advance()
                    continue
                break
            default = ("null", None)
            if self.is_word("DEFAULT"):
                self.advance()
                default = self.parse_literal()
            decls.append({"name": t.value, "pos": t.pos, "cands": cands, "default": default[1]})
            if self.peek().kind == "COMMA":
                self.advance()
                continue
            break
        t = self.peek()
        if t.kind != "RBRACE":
            fail("E_PARSE", "expected ',' or '}' in GLOSS but found %s" % describe(t), t.pos)
        self.advance()
        return {"strict": strict, "decls": decls, "pos": start}

    def parse_conflate(self):
        start = self.advance().pos  # CONFLATE
        flavor = "INNER"
        if self.is_word("INTERSECTING"):
            self.advance()
        elif self.is_word("PRESERVING"):
            self.advance()
            t = self.peek()
            if self.is_word("LEFT"):
                flavor = "LEFT"
            elif self.is_word("RIGHT"):
                flavor = "RIGHT"
            elif self.is_word("BOTH"):
                flavor = "BOTH"
            else:
                fail("E_PARSE", "expected LEFT, RIGHT or BOTH but found %s" % describe(t), t.pos)
            self.advance()
        t = self.peek()
        if t.kind != "IDENT":
            fail("E_PARSE", "expected source alias but found %s" % describe(t), t.pos)
        self.advance()
        alias = (t.value, t.pos)
        t = self.peek()
        if not self.is_word("UPON"):
            fail("E_PARSE", "expected UPON but found %s" % describe(t), t.pos)
        self.advance()
        conj = []
        while True:
            lpos = self.peek().pos
            left = self.parse_field_ref()
            op = self.peek()
            if not (op.kind == "OP" and op.value == "="):
                fail("E_PARSE", "expected '=' in UPON predicate but found %s" % describe(op), op.pos)
            self.advance()
            rpos = self.peek().pos
            right = self.parse_field_ref()
            conj.append(((left, lpos), (right, rpos)))
            if self.is_kw("AND"):
                self.advance()
                continue
            break
        return {"flavor": flavor, "alias": alias, "on": conj, "pos": start}

    def is_word(self, word):
        t = self.peek()
        return t.kind == "IDENT" and t.value.upper() == word

    def parse_select_list(self):
        items = []
        while True:
            items.append(self.parse_select_item())
            if self.peek().kind == "COMMA":
                self.advance()
                continue
            break
        return items

    def parse_select_item(self):
        t = self.peek()
        if t.kind == "STAR":
            self.advance()
            return {"kind": "star", "pos": t.pos}
        nxt = self.toks[self.i + 1] if t.kind == "IDENT" else None
        if (nxt is not None and nxt.kind == "DOT"
                and self.toks[self.i + 2].kind == "STAR"):
            if t.value.upper() == "CANON":
                fail("E_PARSE", "wildcard is not allowed on CANON", self.toks[self.i + 2].pos)
            self.i += 3
            return {"kind": "astar", "source": t.value, "pos": t.pos,
                    "key": t.value + ".*", "alias": None}
        if nxt is not None and nxt.kind == "LPAREN":
            item = self.parse_agg_call()
        else:
            path = self.parse_field_ref()
            item = {"kind": "field", "path": path, "key": ".".join(path), "pos": t.pos}
        item["alias"] = None
        if self.is_word("AS"):
            self.advance()
            at = self.peek()
            if at.kind != "IDENT":
                fail("E_PARSE", "expected alias identifier but found %s" % describe(at), at.pos)
            self.advance()
            item["alias"] = at.value
            item["key"] = at.value
        return item

    def parse_agg_call(self):
        t = self.advance()
        name = t.value.upper()
        if name == "AVERAGE":
            name = "AVG"
        if name not in AGG_FUNCS:
            fail("E_PARSE", "unknown function %r" % t.value, t.pos)
        self.advance()  # LPAREN
        a = self.peek()
        if a.kind == "STAR":
            if name != "COUNT":
                fail("E_PARSE", "'*' is only allowed in COUNT(*)", a.pos)
            self.advance()
            path = None
            arg = "*"
            argpos = a.pos
        else:
            path = self.parse_field_ref()
            arg = ".".join(path)
        argpos = a.pos
        r = self.peek()
        if r.kind != "RPAREN":
            fail("E_PARSE", "expected ')' but found %s" % describe(r), r.pos)
        self.advance()
        return {"kind": "agg", "func": name, "path": path,
                "key": "%s(%s)" % (name, arg), "pos": t.pos, "argpos": argpos}

    def parse_ident(self, first):
        t = self.peek()
        if t.kind == "IDENT":
            self.advance()
            return t.value
        fail("E_PARSE", "expected identifier but found %s" % describe(t), t.pos)

    def parse_field_ref(self):
        path = [self.parse_ident(True)]
        while self.peek().kind == "DOT":
            self.advance()
            path.append(self.parse_ident(False))
        return path

    def parse_or(self):
        left = self.parse_and()
        while self.is_kw("OR"):
            self.advance()
            right = self.parse_and()
            left = ("OR", left, right)
        return left

    def parse_and(self):
        left = self.parse_primary()
        while self.is_kw("AND"):
            self.advance()
            right = self.parse_primary()
            left = ("AND", left, right)
        return left

    def parse_primary(self):
        t = self.peek()
        if t.kind == "LPAREN":
            self.advance()
            e = self.parse_or()
            t2 = self.peek()
            if t2.kind != "RPAREN":
                fail("E_PARSE", "expected ')' but found %s" % describe(t2), t2.pos)
            self.advance()
            return e
        return self.parse_predicate()

    def parse_predicate(self):
        ppos = self.peek().pos
        path = self.parse_field_ref()
        t = self.peek()
        if t.kind != "OP":
            fail("E_PARSE", "expected comparison operator but found %s" % describe(t), t.pos)
        self.advance()
        op = t.value
        lit = self.parse_literal()
        return ("CMP", path, op, lit, ppos)

    def parse_literal(self):
        lt = self.peek()
        if lt.kind == "NUMBER":
            lit = ("number", lt.value)
        elif lt.kind == "STRING":
            lit = ("string", lt.value)
        elif lt.kind == "KW" and lt.value == "TRUE":
            lit = ("bool", True)
        elif lt.kind == "KW" and lt.value == "FALSE":
            lit = ("bool", False)
        elif lt.kind == "KW" and lt.value == "NULL":
            lit = ("null", None)
        else:
            fail("E_PARSE", "expected literal but found %s" % describe(lt), lt.pos)
        self.advance()
        return lit


# ---------------------------------------------------------------- evaluation

def resolve(record, path):
    cur = record
    for part in path:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def type_of(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    return "complex"


def compare(value, op, lit):
    ltype, lval = lit
    vtype = type_of(value)
    if ltype == "null":
        if op == "=":
            return vtype == "null"
        if op == "!=":
            return vtype in ("bool", "number", "string")
        return False
    if vtype != ltype:
        return False
    if vtype == "bool":
        if op == "=":
            return value == lval
        if op == "!=":
            return value != lval
        return False
    if op == "=":
        return value == lval
    if op == "!=":
        return value != lval
    if op == "<":
        return value < lval
    if op == "<=":
        return value <= lval
    if op == ">":
        return value > lval
    if op == ">=":
        return value >= lval
    return False


def evaluate(expr, record):
    kind = expr[0]
    if kind == "AND":
        return evaluate(expr[1], record) and evaluate(expr[2], record)
    if kind == "OR":
        return evaluate(expr[1], record) or evaluate(expr[2], record)
    _, ref, op, lit = expr
    return compare(resolve_ref(record, ref), op, lit)


CANON_TAG = object()


class Canon:
    """A GLOSS declaration: CANON.<name> := cand1 | cand2 ... [DEFAULT lit].

    Hashed by identity so (CANON_TAG, canon) refs can be used like ordinary field refs."""

    def __init__(self, name, cands, default, strict):
        self.name = name
        self.cands = cands  # list of bound (alias, path) refs
        self.default = default
        self.strict = strict
        self.aliases = {c[0] for c in cands}

    def value(self, row):
        """First non-null candidate (left to right); candidates whose alias is absent
        from `row` (not joined / not visible) count as null."""
        found = None
        found_key = None
        for ref in self.cands:
            v = resolve_ref(row, ref)
            if v is None:
                continue
            if not self.strict:
                return v
            if found_key is None:
                found, found_key = v, deep_key(v)
            elif deep_key(v) != found_key:
                fail("E_RUNTIME", "LOGQL_ERROR: conflict in CANON.%s" % self.name)
        if found_key is not None:
            return found
        return self.default


def is_canon(ref):
    return ref is not None and ref[0] is CANON_TAG


def resolve_ref(row, ref):
    """row maps alias -> record (or None when null-padded by an outer conflation)."""
    if ref[0] is CANON_TAG:
        return ref[1].value(row)
    rec = row.get(ref[0])
    if rec is None:
        return None
    return resolve(rec, ref[1])


def resolve_ref_present(row, ref):
    if ref[0] is CANON_TAG:
        return ref[1].value(row)
    rec = row.get(ref[0])
    if rec is None:
        return MISSING
    return resolve_present(rec, ref[1])


# ---------------------------------------------------------------- I/O

def _reject_constant(name):
    raise ValueError("invalid JSON constant %s" % name)


def load_records(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        fail("E_IO", "log file not found: %s" % path)
    except IsADirectoryError:
        fail("E_IO", "log file is a directory: %s" % path)
    except OSError as e:
        fail("E_IO", "cannot read log file %s: %s" % (path, e.strerror or e))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fail("E_INPUT", "log file is not valid UTF-8")
    if text.startswith("﻿"):
        text = text[1:]
    records = []
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if line.strip() == "":
            continue
        try:
            obj = json.loads(line, parse_constant=_reject_constant)
        except ValueError as e:
            fail("E_INPUT", "invalid JSON on line %d: %s" % (lineno, e))
        if not isinstance(obj, dict):
            fail("E_INPUT", "line %d is not a JSON object" % lineno)
        records.append(obj)
    return records


# ---------------------------------------------------------------- semantics

def bind_query(q):
    """Resolve every field reference to (alias, subpath). Returns analysis info."""
    from_alias, from_pos = q["source"]
    conflates = q["conflates"]
    multi = bool(conflates)
    aliases = [from_alias]
    for c in conflates:
        name, pos = c["alias"]
        if name in aliases:
            fail("E_SEMANTIC", "source alias %r used more than once" % name, pos)
        aliases.append(name)

    canons = {}

    def bind(path, pos, scope=None, canon_ok=True):
        if len(path) >= 2 and path[0].upper() == "CANON" and path[0] not in aliases:
            if len(path) != 2:
                fail("E_PARSE", "CANON reference must be CANON.<name>", pos)
            if not canon_ok:
                fail("E_SEMANTIC", "CANON.%s cannot be used here" % path[1], pos)
            c = canons.get(path[1])
            if c is None:
                fail("E_SEMANTIC", "undeclared canonical label CANON.%s" % path[1], pos)
            return (CANON_TAG, c)
        scope = aliases if scope is None else scope
        if not multi:
            if len(path) >= 2 and path[0] == from_alias:
                return (from_alias, tuple(path[1:]))
            return (from_alias, tuple(path))
        if len(path) < 2:
            fail("E_SEMANTIC", "field reference %r must be qualified with a source alias"
                 % ".".join(path), pos)
        if path[0] not in aliases:
            fail("E_SEMANTIC", "unknown source alias %r in field reference %r"
                 % (path[0], ".".join(path)), pos)
        if path[0] not in scope:
            fail("E_SEMANTIC", "source alias %r is not in scope here" % path[0], pos)
        return (path[0], tuple(path[1:]))

    gloss = q.get("gloss")
    if gloss is not None:
        for d in gloss["decls"]:
            if d["name"] in canons:
                fail("E_SEMANTIC", "canonical label CANON.%s declared more than once"
                     % d["name"], d["pos"])
            cands = []
            for path, pos in d["cands"]:
                if len(path) < 2:
                    fail("E_SEMANTIC", "GLOSS candidate %r must be qualified with a source alias"
                         % ".".join(path), pos)
                if path[0].upper() == "CANON" and path[0] not in aliases:
                    fail("E_SEMANTIC", "GLOSS candidate cannot reference CANON", pos)
                if path[0] not in aliases:
                    fail("E_SEMANTIC", "unknown source alias %r in GLOSS candidate %r"
                         % (path[0], ".".join(path)), pos)
                cands.append((path[0], tuple(path[1:])))
            canons[d["name"]] = Canon(d["name"], cands, d["default"], gloss["strict"])

    def bind_expr(e):
        if e[0] in ("AND", "OR"):
            return (e[0], bind_expr(e[1]), bind_expr(e[2]))
        _, path, op, lit, pos = e
        ref = bind(path, pos)
        if is_canon(ref) and any(a != from_alias for a in ref[1].aliases):
            fail("E_SEMANTIC", "CANON.%s cannot be used in WHERE: not all candidates reference %r"
                 % (ref[1].name, from_alias), pos)
        return ("CMP", ref, op, lit)

    select = q["select"]
    for it in select:
        if it["kind"] == "field":
            it["ref"] = bind(it["path"], it["pos"])
        elif it["kind"] == "agg":
            it["ref"] = bind(it["path"], it["argpos"]) if it["path"] is not None else None
        elif it["kind"] == "astar":
            if it["source"] not in aliases:
                fail("E_SEMANTIC", "unknown source alias %r in %s.*"
                     % (it["source"], it["source"]), it["pos"])
        elif it["kind"] == "star" and multi:
            fail("E_SEMANTIC", "SELECT * is not allowed with CONFLATE; use alias.*", it["pos"])
    q["where_b"] = bind_expr(q["where"]) if q["where"] is not None else None
    if q["group"] is not None:
        q["group_b"] = [(bind(p, pos), p, pos) for p, pos in q["group"]]
    else:
        q["group_b"] = None
    scope = [from_alias]
    for c in conflates:
        name = c["alias"][0]
        scope = scope + [name]
        on = []
        for (lp, lpos), (rp, rpos) in c["on"]:
            l = bind(lp, lpos, scope)
            r = bind(rp, rpos, scope)
            if not is_canon(l) and not is_canon(r) and l[0] == r[0]:
                fail("E_SEMANTIC", "UPON predicate must compare two different aliases", lpos)
            on.append((l, r))
        c["on_b"] = on
    q["aliases"] = aliases
    q["multi"] = multi


def validate(q):
    """Check selection rules; returns True when the query is an aggregation."""
    select = q["select"]
    group = q["group_b"]
    multi = q["multi"]
    stars = [it for it in select if it["kind"] in ("star", "astar")]
    aggs = [it for it in select if it["kind"] == "agg"]
    if stars:
        if aggs:
            fail("E_SEMANTIC", "* cannot be combined with aggregate functions", stars[0]["pos"])
        if group is not None:
            fail("E_SEMANTIC", "* cannot be used with GROUP BY", stars[0]["pos"])
        bare = [it for it in select if it["kind"] == "star"]
        if bare and len(select) > 1:
            other = [it for it in select if it is not bare[0]]
            fail("E_PARSE", "'*' cannot be combined with other select items", other[0]["pos"])
    # output key uniqueness (static part)
    seen = {}
    for it in select:
        if it["kind"] == "star":
            continue
        k = it["key"]
        if k in seen:
            prev = seen[k]
            if it["kind"] == "astar":
                fail("E_SEMANTIC", "duplicate expansion %r in select list" % k, it["pos"])
            if (not multi and not aggs and group is None and it["alias"] is None
                    and prev["alias"] is None and prev["kind"] == "field"):
                fail("E_DUPLICATE_FIELD", "duplicate field %r in select list" % k, it["pos"])
            fail("E_SEMANTIC", "duplicate output key %r" % k, it["pos"])
        seen[k] = it
    if group is not None:
        gseen = set()
        for ref, path, pos in group:
            if ref in gseen:
                fail("E_SEMANTIC", "duplicate field %r in GROUP BY" % ".".join(path), pos)
            gseen.add(ref)
        for it in select:
            if it["kind"] == "field" and it["ref"] not in gseen:
                fail("E_SEMANTIC", "field %r must appear in GROUP BY or be used in an aggregate"
                     % ".".join(it["path"]), it["pos"])
        return True
    if aggs:
        for it in select:
            if it["kind"] == "field":
                fail("E_SEMANTIC", "field %r must appear in GROUP BY or be used in an aggregate"
                     % ".".join(it["path"]), it["pos"])
        return True
    return False


# ---------------------------------------------------------------- aggregation

MISSING = object()


def resolve_present(record, path):
    cur = record
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def group_key(v):
    if v is None:
        return ("null", None)
    if isinstance(v, bool):
        return ("bool", v)
    if is_number(v):
        return ("number", v)
    if isinstance(v, str):
        return ("string", v)
    return ("null", None)


def deep_key(v):
    if v is None:
        return ("null",)
    if isinstance(v, bool):
        return ("bool", v)
    if is_number(v):
        return ("number", v)
    if isinstance(v, str):
        return ("string", v)
    if isinstance(v, list):
        return ("array", tuple(deep_key(x) for x in v))
    if isinstance(v, dict):
        return ("object", frozenset((k, deep_key(x)) for k, x in v.items()))
    return ("other", repr(v))


def tidy_number(x):
    if isinstance(x, float) and math.isfinite(x) and x.is_integer() and abs(x) < 2 ** 53:
        return int(x)
    return x


class Agg:
    __slots__ = ("func", "path", "count", "ints", "floats", "nums", "best_num",
                 "best_str", "seen", "uniq")

    def __init__(self, func, ref):
        self.func = func
        self.path = ref
        self.count = 0
        self.ints = 0
        self.floats = []
        self.nums = 0
        self.best_num = MISSING
        self.best_str = MISSING
        self.seen = set()
        self.uniq = []

    def add(self, rec):
        f = self.func
        if f == "COUNT":
            if self.path is None:
                self.count += 1
            else:
                v = resolve_ref_present(rec, self.path)
                if v is not MISSING and v is not None:
                    self.count += 1
            return
        v = resolve_ref_present(rec, self.path)
        if f == "SUM" or f == "AVG":
            if is_number(v):
                self.nums += 1
                if isinstance(v, int):
                    self.ints += v
                else:
                    self.floats.append(v)
        elif f == "MIN" or f == "MAX":
            if is_number(v):
                b = self.best_num
                if b is MISSING or (v < b if f == "MIN" else v > b):
                    self.best_num = v
            elif isinstance(v, str) and self.best_num is MISSING:
                b = self.best_str
                if b is MISSING or (v < b if f == "MIN" else v > b):
                    self.best_str = v
        else:  # UNIQUE
            if v is MISSING:
                v = None
            k = deep_key(v)
            if k not in self.seen:
                self.seen.add(k)
                self.uniq.append(v)

    def result(self):
        f = self.func
        if f == "COUNT":
            return self.count
        if f == "SUM" or f == "AVG":
            if self.nums == 0:
                return None
            if self.floats:
                total = math.fsum(self.floats + [float(self.ints)]) if abs(self.ints) < 2 ** 53 \
                    else math.fsum(self.floats) + self.ints
                if f == "AVG":
                    return tidy_number(total / self.nums)
                return tidy_number(total)
            if f == "AVG":
                return tidy_number(self.ints / self.nums)
            return self.ints
        if f == "MIN" or f == "MAX":
            if self.best_num is not MISSING:
                return self.best_num
            if self.best_str is not MISSING:
                return self.best_str
            return None
        return self.uniq


def aggregate(rows, select, group):
    group_refs = [ref for ref, _, _ in group] if group is not None else []
    agg_items = [it for it in select if it["kind"] == "agg"]
    groups = {}
    order = []
    for row in rows:
        gk_vals = []
        gk = []
        for ref in group_refs:
            v = resolve_ref(row, ref)
            k = group_key(v)
            gk.append(k)
            gk_vals.append(v if k[0] != "null" else None)
        gk = tuple(gk)
        state = groups.get(gk)
        if state is None:
            state = (gk_vals, [Agg(it["func"], it["ref"]) for it in agg_items])
            groups[gk] = state
            order.append(gk)
        for a in state[1]:
            a.add(row)
    if group is None and not order:
        order.append(())
        groups[()] = ([], [Agg(it["func"], it["ref"]) for it in agg_items])
    index = {ref: i for i, ref in enumerate(group_refs)}
    out = []
    for gk in order:
        vals, aggs = groups[gk]
        row = {}
        ai = 0
        for it in select:
            if it["kind"] == "agg":
                row[it["key"]] = aggs[ai].result()
                ai += 1
            else:
                row[it["key"]] = vals[index[it["ref"]]]
        out.append(row)
    return out


# ---------------------------------------------------------------- conflation

def conflate(left_rows, left_aliases, right_alias, right_records, on, flavor):
    # Each operand is assigned a side: L (already-bound aliases) or R (the new alias).
    # Conjuncts with one operand on each side are hash-joinable, each operand being
    # evaluated against its own side only; anything else is checked per candidate pair.
    # A CANON operand whose candidates all live on one side takes that side; a mixed
    # one takes the side opposite its partner (positional when both are mixed), and is
    # then resolved using only the candidates visible on that side.
    def side_of(ref):
        if is_canon(ref):
            al = ref[1].aliases
            if al and al <= set(left_aliases):
                return "L"
            if al == {right_alias}:
                return "R"
            return None
        return "R" if ref[0] == right_alias else "L"

    hash_l, hash_r, extra = [], [], []
    for l, r in on:
        sl, sr = side_of(l), side_of(r)
        if sl is None and sr is None:
            sl, sr = "L", "R"
        elif sl is None:
            sl = "L" if sr == "R" else "R"
        elif sr is None:
            sr = "L" if sl == "R" else "R"
        if sl == "L" and sr == "R":
            hash_l.append(l)
            hash_r.append(r)
        elif sl == "R" and sr == "L":
            hash_l.append(r)
            hash_r.append(l)
        else:
            extra.append((l, r))

    index = {}
    for j, rec in enumerate(right_records):
        view = {right_alias: rec}
        k = tuple(deep_key(resolve_ref(view, ref)) for ref in hash_r)
        index.setdefault(k, []).append(j)

    all_right = list(range(len(right_records)))
    matches = []
    for row in left_rows:
        if hash_l:
            k = tuple(deep_key(resolve_ref(row, ref)) for ref in hash_l)
            cand = index.get(k, [])
        else:
            cand = all_right
        if extra:
            ok = []
            for j in cand:
                combined = dict(row)
                combined[right_alias] = right_records[j]
                if all(deep_key(resolve_ref(combined, a)) == deep_key(resolve_ref(combined, b))
                       for a, b in extra):
                    ok.append(j)
            cand = ok
        matches.append(cand)

    def pair(i, j):
        row = dict(left_rows[i])
        row[right_alias] = right_records[j]
        return row

    def left_only(i):
        row = dict(left_rows[i])
        row[right_alias] = None
        return row

    def right_only(j):
        row = {a: None for a in left_aliases}
        row[right_alias] = right_records[j]
        return row

    out = []
    if flavor == "INNER":
        for i, ms in enumerate(matches):
            out.extend(pair(i, j) for j in ms)
    elif flavor == "LEFT":
        for i, ms in enumerate(matches):
            if ms:
                out.extend(pair(i, j) for j in ms)
            else:
                out.append(left_only(i))
    elif flavor == "RIGHT":
        matched_r = set()
        for i, ms in enumerate(matches):
            for j in ms:
                out.append(pair(i, j))
                matched_r.add(j)
        out.extend(right_only(j) for j in all_right if j not in matched_r)
    else:  # BOTH
        rev = [[] for _ in right_records]
        for i, ms in enumerate(matches):
            for j in ms:
                rev[j].append(i)
        for j, lefts in enumerate(rev):
            out.extend(pair(i, j) for i in lefts)
        out.extend(left_only(i) for i, ms in enumerate(matches) if not ms)
        out.extend(right_only(j) for j in all_right if not rev[j])
    return out


def top_level_keys(records):
    keys = {}
    for rec in records:
        for k in rec:
            if k not in keys:
                keys[k] = True
    return list(keys)


def run(sources, query):
    q = Parser(query).parse_query()
    from_alias, from_pos = q["source"]
    if from_alias not in sources:
        fail("E_UNKNOWN_TABLE", "unknown table %r; no source bound to that alias" % from_alias,
             from_pos)
    for c in q["conflates"]:
        name, pos = c["alias"]
        if name not in sources:
            fail("E_UNKNOWN_TABLE", "unknown table %r; no source bound to that alias" % name, pos)
    bind_query(q)
    is_agg = validate(q)
    select = q["select"]

    data = {}
    for a in q["aliases"]:
        data[a] = load_records(sources[a])

    rows = [{from_alias: rec} for rec in data[from_alias]]
    bound = [from_alias]
    for c in q["conflates"]:
        name = c["alias"][0]
        rows = conflate(rows, bound, name, data[name], c["on_b"], c["flavor"])
        bound.append(name)

    where = q["where_b"]
    if where is not None:
        rows = [r for r in rows if evaluate(where, r)]

    if is_agg:
        return aggregate(rows, select, q["group_b"])

    if select[0]["kind"] == "star":
        return [r[from_alias] for r in rows]

    # Build output column plan (alias.* expanded to the source's top-level keys).
    plan = []
    seen = set()
    for it in select:
        if it["kind"] == "astar":
            for k in top_level_keys(data[it["source"]]):
                key = "%s.%s" % (it["source"], k)
                if key in seen:
                    fail("E_SEMANTIC", "duplicate output key %r" % key, it["pos"])
                seen.add(key)
                plan.append((key, (it["source"], (k,))))
        else:
            if it["key"] in seen:
                fail("E_SEMANTIC", "duplicate output key %r" % it["key"], it["pos"])
            seen.add(it["key"])
            plan.append((it["key"], it["ref"]))
    return [{key: resolve_ref(r, ref) for key, ref in plan} for r in rows]


def emit_error(code, message, position=None):
    if code == "E_RUNTIME":
        # runtime errors use the flat shape shown in the Part 4 spec
        sys.stderr.write(json.dumps({"error": message, "code": code}, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return
    err = {"code": code, "message": message}
    if position is not None:
        err["position"] = position
    sys.stderr.write(json.dumps({"error": err}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def parse_args(argv):
    opts = {}
    sources = {}
    allowed = {"--log-file": "log_file", "--query": "query", "--output": "output",
               "--source": "source"}

    def bind_source(alias, path, flag):
        if alias in sources and sources[alias] != path:
            fail("E_SEMANTIC", "alias %r bound to conflicting paths %r and %r"
                 % (alias, sources[alias], path))
        sources[alias] = path

    i = 0
    while i < len(argv):
        a = argv[i]
        name, val = a, None
        if a.startswith("--") and "=" in a:
            name, val = a.split("=", 1)
        if name not in allowed:
            fail("E_ARGS", "unknown argument: %s" % a)
        if val is None:
            if i + 1 >= len(argv):
                fail("E_ARGS", "missing value for %s" % name)
            val = argv[i + 1]
            i += 2
        else:
            i += 1
        key = allowed[name]
        if key == "source":
            if "=" not in val:
                fail("E_ARGS", "--source expects <alias>=<path>, got %r" % val)
            alias, path = val.split("=", 1)
            if not IDENT_RE.fullmatch(alias):
                fail("E_ARGS", "invalid source alias %r" % alias)
            if path == "":
                fail("E_ARGS", "missing path for source alias %r" % alias)
            bind_source(alias, path, name)
            continue
        if key in opts:
            fail("E_ARGS", "argument %s given more than once" % name)
        if key == "log_file":
            if val == "":
                fail("E_ARGS", "missing path for --log-file")
            bind_source("logs", val, name)
        opts[key] = val
    if "query" not in opts:
        fail("E_ARGS", "missing required argument --query")
    if not sources:
        fail("E_ARGS", "missing required argument --log-file or --source")
    opts["sources"] = sources
    return opts


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        opts = parse_args(argv)
        result = run(opts["sources"], opts["query"])
        payload = json.dumps(result, ensure_ascii=False)
        if opts.get("output") is not None:
            try:
                with open(opts["output"], "w", encoding="utf-8") as f:
                    f.write(payload + "\n")
            except OSError as e:
                fail("E_IO", "cannot write output file %s: %s" % (opts["output"], e.strerror or e))
        else:
            sys.stdout.buffer.write((payload + "\n").encode("utf-8"))
            sys.stdout.flush()
        return 0
    except QueryError as e:
        emit_error(e.code, e.message, e.position)
        return 1
    except RecursionError:
        emit_error("E_PARSE", "query or input nested too deeply")
        return 1
    except Exception as e:  # pragma: no cover
        emit_error("E_INTERNAL", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
