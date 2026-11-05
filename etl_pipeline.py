#!/usr/bin/env python3
"""ETL pipeline specification parser / validator / normalizer."""
import json
import sys

SUPPORTED_OPS = ("select", "filter", "map", "rename", "limit")


class ETLError(Exception):
    def __init__(self, code, message, path):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def schema_err(msg, path):
    return ETLError("SCHEMA_VALIDATION_FAILED", msg, path)


# ---------------------------------------------------------------- expressions

KEYWORDS = {"and", "or", "not"}
LITERAL_WORDS = {"true", "false", "null", "none"}
TWO_CHAR_OPS = (">=", "<=", "==", "!=")
ONE_CHAR_OPS = ("+", "-", "*", "/", ">", "<", "(", ")")


def tokenize(src):
    tokens = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < n and (src[j].isdigit() or (src[j] == "." and not seen_dot)):
                if src[j] == ".":
                    seen_dot = True
                j += 1
            if j < n and src[j] in "eE":
                k = j + 1
                if k < n and src[k] in "+-":
                    k += 1
                if k < n and src[k].isdigit():
                    while k < n and src[k].isdigit():
                        k += 1
                    j = k
                else:
                    raise ValueError("malformed number at position %d" % i)
            if j < n and (src[j].isalpha() or src[j] == "_" or src[j] == "."):
                raise ValueError("malformed number at position %d" % i)
            tokens.append(("num", src[i:j], i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            low = word.lower()
            if low in KEYWORDS:
                tokens.append((low, word, i))
            elif low in LITERAL_WORDS:
                tokens.append(("lit", word, i))
            else:
                tokens.append(("ident", word, i))
            i = j
            continue
        if c in "'\"":
            j = i + 1
            buf = []
            closed = False
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j + 1])
                    j += 2
                    continue
                if src[j] == c:
                    closed = True
                    break
                buf.append(src[j])
                j += 1
            if not closed:
                raise ValueError("unterminated string literal at position %d" % i)
            tokens.append(("str", "".join(buf), i))
            i = j + 1
            continue
        two = src[i:i + 2]
        if two in TWO_CHAR_OPS:
            tokens.append(("op", two, i))
            i += 2
            continue
        if c in ONE_CHAR_OPS:
            tokens.append(("op", c, i))
            i += 1
            continue
        raise ValueError("unexpected character %r at position %d" % (c, i))
    return tokens


class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.pos = 0
        self.idents = []

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def at_op(self, *ops):
        t = self.peek()
        return t is not None and t[0] == "op" and t[1] in ops

    def at_kw(self, kw):
        t = self.peek()
        return t is not None and t[0] == kw

    def advance(self):
        t = self.peek()
        self.pos += 1
        return t

    def fail(self, what):
        t = self.peek()
        if t is None:
            raise ValueError("unexpected end of expression; expected %s" % what)
        raise ValueError("unexpected token %r at position %d; expected %s"
                         % (t[1], t[2], what))

    def parse(self):
        if not self.toks:
            raise ValueError("empty expression")
        self.parse_or()
        if self.peek() is not None:
            t = self.peek()
            raise ValueError("unexpected token %r at position %d" % (t[1], t[2]))

    def parse_or(self):
        self.parse_and()
        while self.at_kw("or"):
            self.advance()
            self.parse_and()

    def parse_and(self):
        self.parse_not()
        while self.at_kw("and"):
            self.advance()
            self.parse_not()

    def parse_not(self):
        if self.at_kw("not"):
            self.advance()
            self.parse_not()
        else:
            self.parse_cmp()

    def parse_cmp(self):
        self.parse_add()
        while self.at_op(">", ">=", "<", "<=", "==", "!="):
            self.advance()
            self.parse_add()

    def parse_add(self):
        self.parse_mul()
        while self.at_op("+", "-"):
            self.advance()
            self.parse_mul()

    def parse_mul(self):
        self.parse_unary()
        while self.at_op("*", "/"):
            self.advance()
            self.parse_unary()

    def parse_unary(self):
        if self.at_op("-", "+"):
            self.advance()
            # disallow stacked sign operators like "--x" / "+-x"
            if self.at_op("-", "+"):
                self.fail("operand")
            self.parse_primary()
        else:
            self.parse_primary()

    def parse_primary(self):
        t = self.peek()
        if t is None:
            self.fail("operand")
        kind = t[0]
        if kind in ("num", "str", "lit"):
            self.advance()
            return
        if kind == "ident":
            self.advance()
            self.idents.append(t[1])
            return
        if kind == "op" and t[1] == "(":
            self.advance()
            self.parse_or()
            if not self.at_op(")"):
                self.fail("')'")
            self.advance()
            return
        self.fail("operand")


def check_expr(src):
    """Validate expression; return list of referenced identifiers."""
    p = Parser(tokenize(src))
    p.parse()
    return p.idents


# ---------------------------------------------------------------- validation

def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def req_trimmed_string(step, field, path):
    fpath = "%s.%s" % (path, field)
    if field not in step:
        raise schema_err("missing required field '%s'" % field, fpath)
    v = step[field]
    if not isinstance(v, str):
        raise schema_err("field '%s' must be a string, got %s" % (field, type_name(v)), fpath)
    v = v.strip()
    if not v:
        raise schema_err("field '%s' must not be empty" % field, fpath)
    return v


def check_idents(idents, known, fpath):
    if known is None:
        return
    for name in idents:
        if name not in known:
            raise ETLError("MISSING_COLUMN", "unknown column '%s'" % name, fpath)


def normalize_step(step, i, known):
    path = "pipeline.steps[%d]" % i
    if not isinstance(step, dict):
        raise schema_err("step must be an object, got %s" % type_name(step), path)
    if "op" not in step:
        raise schema_err("missing required field 'op'", path + ".op")
    op = step["op"]
    if not isinstance(op, str):
        raise schema_err("field 'op' must be a string, got %s" % type_name(op), path + ".op")
    op = op.strip().lower()
    if not op:
        raise schema_err("field 'op' must not be empty", path + ".op")
    if op not in SUPPORTED_OPS:
        raise ETLError("UNKNOWN_OP", "unsupported op '%s'" % op, path + ".op")

    out = {"op": op}

    if op == "select":
        fpath = path + ".columns"
        if "columns" not in step:
            raise schema_err("missing required field 'columns'", fpath)
        cols = step["columns"]
        if not isinstance(cols, list):
            raise schema_err("field 'columns' must be an array, got %s" % type_name(cols), fpath)
        if not cols:
            raise schema_err("field 'columns' must not be empty", fpath)
        for j, c in enumerate(cols):
            cpath = "%s[%d]" % (fpath, j)
            if not isinstance(c, str):
                raise schema_err("column must be a string, got %s" % type_name(c), cpath)
            if not c.strip():
                raise schema_err("column name must not be empty", cpath)
        for j, c in enumerate(cols):
            if known is not None and c not in known:
                raise ETLError("MISSING_COLUMN", "unknown column '%s'" % c,
                               "%s[%d]" % (fpath, j))
        out["columns"] = list(cols)
        known = set(cols)

    elif op == "filter":
        where = req_trimmed_string(step, "where", path)
        fpath = path + ".where"
        try:
            idents = check_expr(where)
        except ValueError as e:
            raise ETLError("BAD_EXPR", "invalid expression: %s" % e, fpath)
        check_idents(idents, known, fpath)
        out["where"] = where

    elif op == "map":
        as_ = req_trimmed_string(step, "as", path)
        expr = req_trimmed_string(step, "expr", path)
        fpath = path + ".expr"
        try:
            idents = check_expr(expr)
        except ValueError as e:
            raise ETLError("BAD_EXPR", "invalid expression: %s" % e, fpath)
        check_idents(idents, known, fpath)
        out["as"] = as_
        out["expr"] = expr
        if known is not None:
            known = set(known)
            known.add(as_)

    elif op == "rename":
        has_mapping = "mapping" in step
        has_from = "from" in step
        has_to = "to" in step
        if has_mapping and (has_from or has_to):
            raise schema_err("rename must use either 'mapping' or 'from'/'to', not both",
                             path + ".mapping")
        if has_mapping:
            fpath = path + ".mapping"
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise schema_err("field 'mapping' must be an object, got %s"
                                 % type_name(mapping), fpath)
            if not mapping:
                raise schema_err("field 'mapping' must not be empty", fpath)
            for k, v in mapping.items():
                kpath = "%s.%s" % (fpath, k)
                if not k.strip():
                    raise schema_err("mapping key must not be empty", kpath)
                if not isinstance(v, str):
                    raise schema_err("mapping value must be a string, got %s"
                                     % type_name(v), kpath)
                if not v.strip():
                    raise schema_err("mapping value must not be empty", kpath)
            src_path = fpath
        elif has_from or has_to:
            frm = req_trimmed_string(step, "from", path)
            to = req_trimmed_string(step, "to", path)
            mapping = {frm: to}
            src_path = path + ".from"
        else:
            raise schema_err("rename requires 'mapping' or 'from' and 'to'", path + ".mapping")
        if known is not None:
            for k in mapping:
                if k not in known:
                    raise ETLError("MISSING_COLUMN", "unknown column '%s'" % k, src_path)
            new_known = set(c for c in known if c not in mapping)
            new_known.update(mapping.values())
            known = new_known
        out["mapping"] = dict(mapping)

    elif op == "limit":
        fpath = path + ".n"
        if "n" not in step:
            raise schema_err("missing required field 'n'", fpath)
        n = step["n"]
        if isinstance(n, bool) or not isinstance(n, int):
            raise schema_err("field 'n' must be an integer, got %s" % type_name(n), fpath)
        if n < 0:
            raise schema_err("field 'n' must be >= 0", fpath)
        out["n"] = n

    ordered = {"op": out.pop("op")}
    for k in sorted(out):
        ordered[k] = out[k]
    return ordered, known


def process(req):
    if not isinstance(req, dict):
        raise schema_err("request must be a JSON object", "")
    if "pipeline" not in req:
        raise schema_err("missing required field 'pipeline'", "pipeline")
    pipeline = req["pipeline"]
    if not isinstance(pipeline, dict):
        raise schema_err("'pipeline' must be an object, got %s" % type_name(pipeline), "pipeline")
    if "steps" not in pipeline:
        raise schema_err("missing required field 'steps'", "pipeline.steps")
    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise schema_err("'steps' must be an array, got %s" % type_name(steps), "pipeline.steps")
    if "dataset" not in req:
        raise schema_err("missing required field 'dataset'", "dataset")
    dataset = req["dataset"]
    if not isinstance(dataset, list):
        raise schema_err("'dataset' must be an array, got %s" % type_name(dataset), "dataset")
    for j, row in enumerate(dataset):
        if not isinstance(row, dict):
            raise schema_err("dataset rows must be objects, got %s" % type_name(row),
                             "dataset[%d]" % j)

    known = None
    if dataset:
        known = set()
        for row in dataset:
            known.update(row.keys())

    normalized = []
    for i, step in enumerate(steps):
        ns, known = normalize_step(step, i, known)
        normalized.append(ns)
    return {"status": "ok", "normalized": {"steps": normalized}}


def main():
    raw = sys.stdin.read()
    try:
        try:
            req = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise schema_err("invalid JSON input: %s" % e, "")
        result = process(req)
    except ETLError as e:
        out = {
            "status": "error",
            "error_code": e.code,
            "message": "ETL_ERROR: " + e.message,
            "path": e.path,
        }
        sys.stdout.write(json.dumps(out) + "\n")
        return 1
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
