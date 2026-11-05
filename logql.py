#!/usr/bin/env python3
"""LogQL Parts 1-2: query NDJSON logs with a tiny SQL-like language (with GROUP BY / aggregates)."""
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
        single = {",": "COMMA", ".": "DOT", "(": "LPAREN", ")": "RPAREN", "*": "STAR"}
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
        if t.kind == "IDENT" and t.value == "logs":
            self.advance()
        elif t.kind == "IDENT" or t.kind == "KW":
            fail("E_UNKNOWN_TABLE", "unknown table %r; only 'logs' is supported"
                 % (t.value if t.kind == "IDENT" else t.value), t.pos)
        else:
            fail("E_PARSE", "expected table name 'logs' but found %s" % describe(t), t.pos)
        where = None
        if self.is_kw("WHERE"):
            self.advance()
            where = self.parse_or()
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
        return select, where, group

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
        else:
            path = self.parse_field_ref()
            arg = ".".join(path)
        r = self.peek()
        if r.kind != "RPAREN":
            fail("E_PARSE", "expected ')' but found %s" % describe(r), r.pos)
        self.advance()
        return {"kind": "agg", "func": name, "path": path,
                "key": "%s(%s)" % (name, arg), "pos": t.pos}

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
        path = self.parse_field_ref()
        t = self.peek()
        if t.kind != "OP":
            fail("E_PARSE", "expected comparison operator but found %s" % describe(t), t.pos)
        self.advance()
        op = t.value
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
        return ("CMP", path, op, lit)


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
    _, path, op, lit = expr
    return compare(resolve(record, path), op, lit)


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

def validate(select, group):
    """Check selection rules; returns True when the query is an aggregation."""
    stars = [it for it in select if it["kind"] == "star"]
    aggs = [it for it in select if it["kind"] == "agg"]
    if stars:
        if aggs:
            fail("E_SEMANTIC", "SELECT * cannot be combined with aggregate functions", stars[0]["pos"])
        if group is not None:
            fail("E_SEMANTIC", "SELECT * cannot be used with GROUP BY", stars[0]["pos"])
        if len(select) > 1:
            other = [it for it in select if it["kind"] != "star"]
            pos = (other or stars[1:])[0]["pos"]
            fail("E_PARSE", "'*' cannot be combined with other select items", pos)
        return False
    # output key uniqueness
    seen = {}
    for it in select:
        k = it["key"]
        if k in seen:
            prev = seen[k]
            if (not aggs and group is None and it["alias"] is None
                    and prev["alias"] is None):
                fail("E_DUPLICATE_FIELD", "duplicate field %r in select list" % k, it["pos"])
            fail("E_SEMANTIC", "duplicate output key %r in select list" % k, it["pos"])
        seen[k] = it
    if group is not None:
        gseen = set()
        for path, pos in group:
            key = tuple(path)
            if key in gseen:
                fail("E_SEMANTIC", "duplicate field %r in GROUP BY" % ".".join(path), pos)
            gseen.add(key)
        for it in select:
            if it["kind"] == "field" and tuple(it["path"]) not in gseen:
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

    def __init__(self, func, path):
        self.func = func
        self.path = path
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
                v = resolve_present(rec, self.path)
                if v is not MISSING and v is not None:
                    self.count += 1
            return
        v = resolve_present(rec, self.path)
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


def aggregate(records, where, select, group):
    group_paths = [p for p, _ in group] if group is not None else []
    agg_items = [it for it in select if it["kind"] == "agg"]
    groups = {}
    order = []
    for rec in records:
        if where is not None and not evaluate(where, rec):
            continue
        gk_vals = []
        gk = []
        for p in group_paths:
            v = resolve(rec, p)
            k = group_key(v)
            gk.append(k)
            gk_vals.append(v if k[0] != "null" else None)
        gk = tuple(gk)
        state = groups.get(gk)
        if state is None:
            state = (gk_vals, [Agg(it["func"], it["path"]) for it in agg_items])
            groups[gk] = state
            order.append(gk)
        for a in state[1]:
            a.add(rec)
    if group is None and not order:
        order.append(())
        groups[()] = ([], [Agg(it["func"], it["path"]) for it in agg_items])
    index = {tuple(p): i for i, p in enumerate(group_paths)}
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
                row[it["key"]] = vals[index[tuple(it["path"])]]
        out.append(row)
    return out


def run(log_file, query):
    select, where, group = Parser(query).parse_query()
    is_agg = validate(select, group)
    records = load_records(log_file)
    if is_agg:
        return aggregate(records, where, select, group)
    out = []
    star = select[0]["kind"] == "star"
    for rec in records:
        if where is not None and not evaluate(where, rec):
            continue
        if star:
            out.append(rec)
        else:
            out.append({it["key"]: resolve(rec, it["path"]) for it in select})
    return out


def emit_error(code, message, position=None):
    err = {"code": code, "message": message}
    if position is not None:
        err["position"] = position
    sys.stderr.write(json.dumps({"error": err}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def parse_args(argv):
    opts = {}
    allowed = {"--log-file": "log_file", "--query": "query", "--output": "output"}
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
        if key in opts:
            fail("E_ARGS", "argument %s given more than once" % name)
        opts[key] = val
    for req in ("--log-file", "--query"):
        if allowed[req] not in opts:
            fail("E_ARGS", "missing required argument %s" % req)
    return opts


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        opts = parse_args(argv)
        result = run(opts["log_file"], opts["query"])
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
