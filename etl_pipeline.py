#!/usr/bin/env python3
"""ETL pipeline specification parser / validator / normalizer."""
import json
import re
import sys

SUPPORTED_OPS = ("select", "filter", "map", "rename", "limit", "branch", "call")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PARAMS_WORD = "params"


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
VALID_OPS = {"*", "/", "<", "<=", ">", ">=", "==", "!=", "&&", "||"}
# characters that may combine into (possibly unsupported) operator runs
OP_RUN_CHARS = set("*/%^&|=<>~")
SIMPLE_OPS = ("+", "-", "(", ")")


class UnsupportedOperator(ValueError):
    def __init__(self, op):
        super().__init__("unsupported operator '%s'" % op)
        self.op = op


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
            if word == PARAMS_WORD:
                k = j
                while k < n and src[k].isspace():
                    k += 1
                if k < n and src[k] == ".":
                    k += 1
                    while k < n and src[k].isspace():
                        k += 1
                    m = k
                    while m < n and (src[m].isalnum() or src[m] == "_"):
                        m += 1
                    if m == k or src[k].isdigit():
                        raise ValueError("expected parameter name after 'params.' "
                                         "at position %d" % i)
                    tokens.append(("param", src[k:m], i))
                    i = m
                    continue
                raise ValueError("'params' is reserved; use params.<name> "
                                 "at position %d" % i)
            if low in KEYWORDS:
                tokens.append((low, word, i))
            elif low in LITERAL_WORDS:
                tokens.append(("lit", low, i))
            else:
                tokens.append(("ident", word, i))
            i = j
            continue
        if c in "'\"":
            j = i + 1
            buf = []
            closed = False
            escapes = {"n": "\n", "t": "\t", "r": "\r"}
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(escapes.get(src[j + 1], src[j + 1]))
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
        if c == "!":
            if src[i:i + 2] == "!=":
                if i + 2 < n and src[i + 2] in OP_RUN_CHARS:
                    j = i + 2
                    while j < n and src[j] in OP_RUN_CHARS:
                        j += 1
                    raise UnsupportedOperator(src[i:j])
                tokens.append(("op", "!=", i))
                i += 2
            else:
                tokens.append(("op", "!", i))
                i += 1
            continue
        if c in OP_RUN_CHARS:
            j = i
            while j < n and src[j] in OP_RUN_CHARS:
                j += 1
            run = src[i:j]
            if run not in VALID_OPS:
                raise UnsupportedOperator(run)
            tokens.append(("op", run, i))
            i = j
            continue
        if c in SIMPLE_OPS:
            tokens.append(("op", c, i))
            i += 1
            continue
        raise UnsupportedOperator(c) if not c.isalnum() else \
            ValueError("unexpected character %r at position %d" % (c, i))
    return tokens


class Parser:
    """Recursive-descent parser producing a small tuple-based AST.

    Precedence (low -> high): ||/or, &&/and, not, ==/!=, </<=/>/>=,
    +/-, * and /, unary (! - +), primary.
    """

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
        node = self.parse_or()
        if self.peek() is not None:
            t = self.peek()
            raise ValueError("unexpected token %r at position %d" % (t[1], t[2]))
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.at_kw("or") or self.at_op("||"):
            self.advance()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_not()
        while self.at_kw("and") or self.at_op("&&"):
            self.advance()
            node = ("and", node, self.parse_not())
        return node

    def parse_not(self):
        if self.at_kw("not"):
            self.advance()
            return ("not", self.parse_not())
        return self.parse_eq()

    def parse_eq(self):
        node = self.parse_rel()
        while self.at_op("==", "!="):
            op = self.advance()[1]
            node = ("cmp", op, node, self.parse_rel())
        return node

    def parse_rel(self):
        node = self.parse_add()
        while self.at_op(">", ">=", "<", "<="):
            op = self.advance()[1]
            node = ("cmp", op, node, self.parse_add())
        return node

    def parse_add(self):
        node = self.parse_mul()
        while self.at_op("+", "-"):
            op = self.advance()[1]
            node = ("arith", op, node, self.parse_mul())
        return node

    def parse_mul(self):
        node = self.parse_unary()
        while self.at_op("*", "/"):
            op = self.advance()[1]
            node = ("arith", op, node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.at_op("!"):
            self.advance()
            return ("not", self.parse_unary())
        if self.at_op("-", "+"):
            op = self.advance()[1]
            # disallow stacked sign operators like "--x" / "+-x"
            if self.at_op("-", "+"):
                self.fail("operand")
            return ("neg" if op == "-" else "pos", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        t = self.peek()
        if t is None:
            self.fail("operand")
        kind = t[0]
        if kind == "num":
            self.advance()
            txt = t[1]
            if any(ch in txt for ch in ".eE"):
                return ("lit", float(txt))
            return ("lit", int(txt))
        if kind == "str":
            self.advance()
            return ("lit", t[1])
        if kind == "lit":
            self.advance()
            return ("lit", {"true": True, "false": False}.get(t[1]))
        if kind == "ident":
            self.advance()
            self.idents.append(t[1])
            return ("ident", t[1])
        if kind == "param":
            self.advance()
            return ("param", t[1])
        if kind == "op" and t[1] == "(":
            self.advance()
            node = self.parse_or()
            if not self.at_op(")"):
                self.fail("')'")
            self.advance()
            return node
        self.fail("operand")


def compile_expr(src):
    """Parse expression; return (ast, referenced identifiers)."""
    try:
        p = Parser(tokenize(src))
        return p.parse(), p.idents
    except RecursionError:
        raise ValueError("expression too deeply nested")


def check_expr(src):
    """Validate expression; return list of referenced identifiers."""
    return compile_expr(src)[1]


def expr_error(e, fpath):
    if isinstance(e, UnsupportedOperator):
        return ETLError("BAD_EXPR", str(e), fpath)
    return ETLError("BAD_EXPR", "invalid expression: %s" % e, fpath)


# ---------------------------------------------------------------- evaluation

class EvalError(Exception):
    pass


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def truthy(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if is_number(v):
        return v != 0
    if isinstance(v, (str, list, dict)):
        return len(v) > 0
    return bool(v)


def tidy_number(v):
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        if v.is_integer() and abs(v) < 2 ** 53:
            return int(v)
    return v


def same_kind(a, b):
    if is_number(a) and is_number(b):
        return True
    return type(a) is type(b)


def evaluate(node, row, params=None):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "ident":
        return row.get(node[1])
    if kind == "param":
        return (params or {}).get(node[1])
    if kind == "and":
        return truthy(evaluate(node[1], row, params)) and truthy(evaluate(node[2], row, params))
    if kind == "or":
        return truthy(evaluate(node[1], row, params)) or truthy(evaluate(node[2], row, params))
    if kind == "not":
        return not truthy(evaluate(node[1], row, params))
    if kind in ("neg", "pos"):
        v = evaluate(node[1], row, params)
        if v is None:
            return None
        if not is_number(v):
            raise EvalError("unary '%s' requires a number, got %s"
                            % ("-" if kind == "neg" else "+", type_name(v)))
        return tidy_number(-v if kind == "neg" else v)
    if kind == "arith":
        op = node[1]
        a = evaluate(node[2], row, params)
        b = evaluate(node[3], row, params)
        if a is None or b is None:
            return None
        if op == "+" and isinstance(a, str) and isinstance(b, str):
            return a + b
        if not (is_number(a) and is_number(b)):
            raise EvalError("operator '%s' not supported between %s and %s"
                            % (op, type_name(a), type_name(b)))
        try:
            if op == "+":
                r = a + b
            elif op == "-":
                r = a - b
            elif op == "*":
                r = a * b
            else:
                if b == 0:
                    return None
                r = a / b
        except OverflowError:
            return None
        return tidy_number(r)
    if kind == "cmp":
        op = node[1]
        a = evaluate(node[2], row, params)
        b = evaluate(node[3], row, params)
        if a is None or b is None:
            return False
        if not same_kind(a, b):
            return False
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if not (is_number(a) or isinstance(a, str)):
            return False
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        return a >= b
    raise EvalError("unknown expression node %r" % (kind,))


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


class DefsContext:
    """Holds top-level definitions while validating / expanding calls."""

    def __init__(self, defs):
        self.defs = defs          # name -> raw def object (already shape-checked)
        self.stack = []           # names of defs currently being expanded
        self.normalized = {}      # name -> normalized steps (validated, acyclic)
        self.normalized_known = {}  # name -> known columns after def (from None)

    def def_path(self, name):
        return "defs[%s].steps" % name


def is_param_value(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, list):
        return all(is_param_value(x) for x in v)
    return False


def normalize_call(step, path, known, ctx):
    npath = path + ".name"
    if "name" not in step:
        raise schema_err("missing required field 'name'", npath)
    name = step["name"]
    if not isinstance(name, str):
        raise schema_err("field 'name' must be a string, got %s" % type_name(name), npath)
    name = name.strip()
    if not NAME_RE.match(name):
        raise schema_err("field 'name' must match ^[A-Za-z_][A-Za-z0-9_]*$", npath)
    params = step.get("params")
    ppath = path + ".params"
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise schema_err("field 'params' must be an object, got %s" % type_name(params), ppath)
    for k, v in params.items():
        if not is_param_value(v):
            raise schema_err("param '%s' must be a JSON scalar or array (no nested objects)"
                             % k,
                             "%s.%s" % (ppath, k))
    if ctx is None or name not in ctx.defs:
        raise ETLError("UNKNOWN_DEF", "unknown definition '%s'" % name, npath)
    if name in ctx.stack:
        raise ETLError("RECURSION_FORBIDDEN",
                       "recursive call to '%s' (%s)" % (name, " -> ".join(ctx.stack + [name])),
                       npath)
    if known is not None or name not in ctx.normalized:
        known_in = known
        ctx.stack.append(name)
        try:
            sub, known = normalize_steps(ctx.defs[name]["steps"], ctx.def_path(name),
                                         known, ctx)
        finally:
            ctx.stack.pop()
        if known_in is None and name not in ctx.normalized:
            ctx.normalized[name] = sub
            ctx.normalized_known[name] = known
    else:
        known = ctx.normalized_known.get(name)
    return {"name": name, "params": dict(params)}, known


def normalize_steps(steps, base, known, ctx=None):
    """Normalize a list of steps located at path `base` (e.g. 'pipeline.steps')."""
    out = []
    for i, step in enumerate(steps):
        ns, known = normalize_step(step, "%s[%d]" % (base, i), known, ctx)
        out.append(ns)
    return out, known


def normalize_branch(step, path, known, ctx=None):
    bpath = path + ".branches"
    if "branches" not in step:
        raise schema_err("missing required field 'branches'", bpath)
    branches = step["branches"]
    if not isinstance(branches, list):
        raise schema_err("field 'branches' must be an array, got %s" % type_name(branches), bpath)
    if not branches:
        raise schema_err("field 'branches' must not be empty", bpath)

    # Pass 1: shape of each branch + 'otherwise' placement rules.
    shaped = []
    otherwise_idx = None
    for j, br in enumerate(branches):
        jpath = "%s[%d]" % (bpath, j)
        if not isinstance(br, dict):
            raise schema_err("branch must be an object, got %s" % type_name(br), jpath)
        nb = {}
        if "id" in br and br["id"] is not None:
            if not isinstance(br["id"], str):
                raise schema_err("field 'id' must be a string, got %s" % type_name(br["id"]),
                                 jpath + ".id")
            nb["id"] = br["id"]
        when = req_trimmed_string(br, "when", jpath)
        is_otherwise = when.lower() == "otherwise"
        if is_otherwise:
            if otherwise_idx is not None:
                raise ETLError("MALFORMED_STEP", "at most one 'otherwise' branch is allowed",
                               bpath)
            otherwise_idx = j
            when = "otherwise"
        nb["when"] = when
        spath = jpath + ".steps"
        if "steps" not in br:
            raise schema_err("missing required field 'steps'", spath)
        if not isinstance(br["steps"], list):
            raise schema_err("field 'steps' must be an array, got %s" % type_name(br["steps"]),
                             spath)
        shaped.append((nb, is_otherwise, jpath))
    if otherwise_idx is not None and otherwise_idx != len(branches) - 1:
        raise ETLError("MALFORMED_STEP", "'otherwise' branch must be last", bpath)

    # merge
    strategy = "concat"
    if "merge" in step and step["merge"] is not None:
        merge = step["merge"]
        mpath = path + ".merge"
        if not isinstance(merge, dict):
            raise schema_err("field 'merge' must be an object, got %s" % type_name(merge), mpath)
        if "strategy" in merge and merge["strategy"] is not None:
            st = merge["strategy"]
            if not isinstance(st, str):
                raise schema_err("field 'strategy' must be a string, got %s" % type_name(st),
                                 mpath + ".strategy")
            if st.strip().lower() != "concat":
                raise ETLError("MALFORMED_STEP", "unsupported merge strategy '%s'" % st,
                               mpath + ".strategy")

    # Pass 2: expressions and nested steps.
    out_branches = []
    result_known = None if known is None else set()
    for (nb, is_otherwise, jpath), br in zip(shaped, branches):
        if not is_otherwise:
            fpath = jpath + ".when"
            try:
                idents = check_expr(nb["when"])
            except ValueError as e:
                raise expr_error(e, fpath)
            check_idents(idents, known, fpath)
        sub, sub_known = normalize_steps(br["steps"], jpath + ".steps",
                                         None if known is None else set(known), ctx)
        nb["steps"] = sub
        if result_known is not None:
            result_known.update(sub_known)
        out_branches.append(nb)
    return {"branches": out_branches, "merge": {"strategy": strategy}}, result_known


def normalize_step(step, path, known, ctx=None):
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

    if op == "branch":
        fields, known = normalize_branch(step, path, known, ctx)
        out.update(fields)

    elif op == "call":
        fields, known = normalize_call(step, path, known, ctx)
        out.update(fields)

    elif op == "select":
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
            raise expr_error(e, fpath)
        check_idents(idents, known, fpath)
        out["where"] = where

    elif op == "map":
        as_ = req_trimmed_string(step, "as", path)
        expr = req_trimmed_string(step, "expr", path)
        fpath = path + ".expr"
        try:
            idents = check_expr(expr)
        except ValueError as e:
            raise expr_error(e, fpath)
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


def validate_request(req):
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
    defs = req.get("defs")
    if defs is None:
        defs = {}
    if not isinstance(defs, dict):
        raise schema_err("'defs' must be an object, got %s" % type_name(defs), "defs")
    for name, d in defs.items():
        dpath = "defs[%s]" % name
        if not isinstance(d, dict):
            raise schema_err("definition must be an object, got %s" % type_name(d), dpath)
        if "steps" not in d:
            raise schema_err("missing required field 'steps'", dpath + ".steps")
        if not isinstance(d["steps"], list):
            raise schema_err("'steps' must be an array, got %s" % type_name(d["steps"]),
                             dpath + ".steps")
    return steps, dataset, defs


def normalize_all(steps, defs, known):
    """Normalize the main pipeline (expanding calls), then every definition."""
    ctx = DefsContext(defs)
    normalized, _ = normalize_steps(steps, "pipeline.steps", known, ctx)
    for name in defs:
        if name not in ctx.normalized:
            ctx.stack.append(name)
            try:
                sub, _ = normalize_steps(defs[name]["steps"], ctx.def_path(name), None, ctx)
            finally:
                ctx.stack.pop()
            ctx.normalized[name] = sub
    return normalized, ctx


def process(req):
    steps, dataset, defs = validate_request(req)
    known = None
    if dataset:
        known = set()
        for row in dataset:
            known.update(row.keys())

    normalized, ctx = normalize_all(steps, defs, known)
    result = {"steps": normalized}
    if "defs" in req and isinstance(req["defs"], dict):
        result["defs"] = {name: {"steps": ctx.normalized[name]} for name in defs}
    return {"status": "ok", "normalized": result}


def missing_col(name, path):
    return ETLError("MISSING_COLUMN", "column '%s' not found in row" % name, path)


def rename_key(row, src, dst):
    value = row[src]
    out = {}
    for k, v in row.items():
        if k == src:
            out[dst] = value
        elif k == dst:
            continue
        else:
            out[k] = v
    return out


def run_expr(ast, row, fpath, params=None):
    try:
        return evaluate(ast, row, params)
    except EvalError as e:
        raise ETLError("EXECUTION_FAILED", str(e), fpath)
    except RecursionError:
        raise ETLError("EXECUTION_FAILED", "expression too deeply nested", fpath)


def build_plan(norm_steps, raw_steps, base, ctx=None, cache=None):
    """Attach compiled expressions and paths to normalized steps (recursively)."""
    plan = []
    for i, (ns, raw) in enumerate(zip(norm_steps, raw_steps)):
        path = "%s[%d]" % (base, i)
        ns = dict(ns)
        if ns["op"] == "filter":
            ns["_ast"] = compile_expr(ns["where"])[0]
            ns["_path"] = path + ".where"
        elif ns["op"] == "map":
            ns["_ast"] = compile_expr(ns["expr"])[0]
            ns["_path"] = path + ".expr"
        elif ns["op"] == "rename":
            ns["_from_to"] = "mapping" not in raw
        elif ns["op"] == "branch":
            brs = []
            for j, (nb, rb) in enumerate(zip(ns["branches"], raw["branches"])):
                jpath = "%s.branches[%d]" % (path, j)
                cb = {"otherwise": nb["when"] == "otherwise"}
                if not cb["otherwise"]:
                    cb["ast"] = compile_expr(nb["when"])[0]
                    cb["path"] = jpath + ".when"
                cb["plan"] = build_plan(nb["steps"], rb["steps"], jpath + ".steps",
                                        ctx, cache)
                brs.append(cb)
            ns["_branches"] = brs
        elif ns["op"] == "call":
            name = ns["name"]
            if cache is None:
                cache = {}
            if name not in cache:
                cache[name] = None  # placeholder (acyclic, so never read early)
                cache[name] = build_plan(ctx.normalized[name], ctx.defs[name]["steps"],
                                         ctx.def_path(name), ctx, cache)
            ns["_plan"] = cache[name]
        ns["_base"] = path
        plan.append(ns)
    return plan


def run_steps(plan, rows, params=None):
    for st in plan:
        op = st["op"]
        base = st["_base"]
        if op == "select":
            cols = st["columns"]
            new_rows = []
            for row in rows:
                out = {}
                for j, c in enumerate(cols):
                    if c not in row:
                        raise missing_col(c, "%s.columns[%d]" % (base, j))
                    out[c] = row[c]
                new_rows.append(out)
            rows = new_rows
        elif op == "filter":
            ast, fpath = st["_ast"], st["_path"]
            rows = [row for row in rows if truthy(run_expr(ast, row, fpath, params))]
        elif op == "map":
            ast, fpath, target = st["_ast"], st["_path"], st["as"]
            for row in rows:
                row[target] = run_expr(ast, row, fpath, params)
        elif op == "rename":
            mapping = st["mapping"]
            new_rows = []
            for row in rows:
                for src_col, dst_col in mapping.items():
                    if src_col not in row:
                        if st["_from_to"]:
                            p = base + ".from"
                        else:
                            p = "%s.mapping.%s" % (base, src_col)
                        raise missing_col(src_col, p)
                    if src_col != dst_col:
                        row = rename_key(row, src_col, dst_col)
                new_rows.append(row)
            rows = new_rows
        elif op == "limit":
            rows = rows[:st["n"]]
        elif op == "branch":
            brs = st["_branches"]
            buckets = [[] for _ in brs]
            for row in rows:
                for k, br in enumerate(brs):
                    if br["otherwise"] or truthy(run_expr(br["ast"], row, br["path"], params)):
                        buckets[k].append(row)
                        break
            merged = []
            for br, bucket in zip(brs, buckets):
                merged.extend(run_steps(br["plan"], bucket, params))
            rows = merged
        elif op == "call":
            rows = run_steps(st["_plan"], rows, st["params"])
    return rows


def execute(req):
    steps, dataset, defs = validate_request(req)

    # Validate / normalize every step up front (schema + expression syntax).
    # Column existence is checked at run time against the actual rows.
    normalized, ctx = normalize_all(steps, defs, None)
    plan = build_plan(normalized, steps, "pipeline.steps", ctx, {})

    rows = run_steps(plan, [dict(r) for r in dataset])
    return {
        "status": "ok",
        "data": rows,
        "metrics": {"rows_in": len(dataset), "rows_out": len(rows)},
    }


TRUE_WORDS = {"1", "true", "yes", "on", "y", "t"}
FALSE_WORDS = {"0", "false", "no", "off", "n", "f"}


def parse_args(argv):
    execute_flag = False
    files = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--execute":
            if i + 1 < len(argv) and argv[i + 1].lower() in TRUE_WORDS | FALSE_WORDS:
                execute_flag = argv[i + 1].lower() in TRUE_WORDS
                i += 1
            else:
                execute_flag = True
        elif a.startswith("--execute="):
            val = a.split("=", 1)[1].lower()
            if val in TRUE_WORDS:
                execute_flag = True
            elif val in FALSE_WORDS:
                execute_flag = False
            else:
                raise SystemExit("invalid value for --execute: %r" % val)
        elif a in ("--no-execute",):
            execute_flag = False
        elif a == "-":
            files.append(None)
        elif a.startswith("-"):
            raise SystemExit("unknown argument: %s" % a)
        else:
            files.append(a)
        i += 1
    return execute_flag, files


def main(argv=None):
    execute_flag, files = parse_args(sys.argv[1:] if argv is None else argv)
    if files and files[0] is not None:
        with open(files[0], encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()
    try:
        try:
            req = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise schema_err("invalid JSON input: %s" % e, "")
        result = execute(req) if execute_flag else process(req)
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
