#!/usr/bin/env python3
"""babel_code_goat: translate a restricted Python ``tests.py`` into a tester
for python / javascript / typescript, and run that tester against a solution.

Commands:
    generate <tests_dir> --entrypoint <name> --lang <python|javascript|typescript>
    test <solution_path> <tests_dir> --lang <python|javascript|typescript> [--timeout SECONDS] [--tol FLOAT]
"""
from __future__ import annotations

import argparse
import ast
import collections
import decimal
import io
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tokenize

LANG_EXT = {"python": "py", "javascript": "js", "typescript": "ts"}
MARKER = "@@BABEL_CODE_GOAT@@ "
META_PREFIX = "BABEL_CODE_GOAT_META: "
DEFAULT_TIMEOUT = 10.0
DEFAULT_TOL = 1e-9


class DiscoveryError(Exception):
    pass


# --------------------------------------------------------------------------
# tests.py parsing / discovery
# --------------------------------------------------------------------------

# Canonical names for bare identifiers when they were not explicitly imported.
_BARE_CANON = {
    "Counter": "collections.Counter",
    "deque": "collections.deque",
    "defaultdict": "collections.defaultdict",
    "OrderedDict": "collections.OrderedDict",
    "Decimal": "decimal.Decimal",
    "isclose": "math.isclose",
    "inf": "math.inf",
    "nan": "math.nan",
}

# Helper callables that are never the entrypoint (used when the entrypoint
# name is unknown during discovery).
_HELPER_CALLS = {
    "set", "frozenset", "tuple", "list", "dict", "float", "int", "str", "abs",
    "collections.Counter", "collections.deque", "collections.defaultdict",
    "collections.OrderedDict", "decimal.Decimal", "math.isclose",
    "re.search", "re.match", "re.fullmatch", "re.compile", "isinstance", "len",
    "sorted", "round",
}

_DD_FACTORIES = {
    "int": int, "float": float, "str": str, "list": list, "set": set,
    "dict": dict, "tuple": tuple, "frozenset": frozenset, "bool": bool,
    "collections.Counter": collections.Counter, "collections.deque": collections.deque,
    "collections.OrderedDict": collections.OrderedDict,
    "collections.defaultdict": collections.defaultdict,
    "decimal.Decimal": decimal.Decimal,
}
_DD_FACTORY_NAMES = {v: k for k, v in _DD_FACTORIES.items()}

_RE_FLAG_VALUES = {
    "I": re.I, "IGNORECASE": re.I, "M": re.M, "MULTILINE": re.M,
    "S": re.S, "DOTALL": re.S, "X": re.X, "VERBOSE": re.X,
    "A": re.A, "ASCII": re.A, "U": re.U, "UNICODE": re.U,
}


class _Ctx:
    def __init__(self, ep_name, aliases):
        self.ep = ep_name
        self.aliases = aliases

    def qual(self, node):
        """Canonical dotted name of a Name/Attribute chain, or None."""
        if isinstance(node, ast.Name):
            if node.id in self.aliases:
                return self.aliases[node.id]
            return _BARE_CANON.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.qual(node.value)
            if base is None:
                return None
            return base + "." + node.attr
        return None


def _collect_aliases(tree):
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                if a.name == "*":
                    continue
                aliases[a.asname or a.name] = node.module + "." + a.name
    return aliases


def _err(node, what="unsupported value"):
    return DiscoveryError(f"line {getattr(node, 'lineno', '?')}: {what}")


def _hashable(v, node):
    try:
        hash(v)
    except TypeError:
        raise _err(node, "unhashable key/element")
    return v


def _iter_value(v, node):
    if isinstance(v, (list, tuple, set, frozenset, str, collections.deque, dict)):
        return list(v)
    raise _err(node, "unsupported iterable")


def _value(node, ctx):
    """Evaluate a restricted literal expression into a Python value."""
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        raise _err(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _value(node.operand, ctx)
        if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
            raise _err(node)
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.List):
        return [_value(e, ctx) for e in _plain_elts(node)]
    if isinstance(node, ast.Tuple):
        return tuple(_value(e, ctx) for e in _plain_elts(node))
    if isinstance(node, ast.Set):
        return {_hashable(_value(e, ctx), node) for e in _plain_elts(node)}
    if isinstance(node, ast.Dict):
        d = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                raise _err(node)
            kv = _hashable(_value(k, ctx), node)
            d[kv] = _value(v, ctx)
        return d
    if isinstance(node, ast.Attribute):
        q = ctx.qual(node)
        if q == "math.inf":
            return math.inf
        if q == "math.nan":
            return math.nan
        if q == "math.pi":
            return math.pi
        if q == "math.e":
            return math.e
        if q == "math.tau":
            return math.tau
        raise _err(node)
    if isinstance(node, ast.Name):
        q = ctx.qual(node)
        if q == "math.inf":
            return math.inf
        if q == "math.nan":
            return math.nan
        raise _err(node)
    if isinstance(node, ast.Call):
        return _value_call(node, ctx)
    raise _err(node)


def _plain_elts(node):
    for e in node.elts:
        if isinstance(e, ast.Starred):
            raise _err(node)
    return node.elts


def _value_call(node, ctx):
    q = ctx.qual(node.func)
    for a in node.args:
        if isinstance(a, ast.Starred):
            raise _err(node)
    for kw in node.keywords:
        if kw.arg is None:
            raise _err(node)
    args = node.args
    kws = node.keywords
    if q in ("set", "frozenset", "tuple", "list", "collections.deque"):
        maxlen = None
        if q == "collections.deque":
            for kw in kws:
                if kw.arg != "maxlen":
                    raise _err(node)
                maxlen = _value(kw.value, ctx)
            if len(args) == 2 and maxlen is None:
                maxlen = _value(args[1], ctx)
                args = args[:1]
            if maxlen is not None and (isinstance(maxlen, bool) or not isinstance(maxlen, int)):
                raise _err(node)
        elif kws:
            raise _err(node)
        if len(args) > 1:
            raise _err(node)
        items = _iter_value(_value(args[0], ctx), node) if args else []
        if q in ("set", "frozenset"):
            s = {_hashable(x, node) for x in items}
            return s if q == "set" else frozenset(s)
        if q == "tuple":
            return tuple(items)
        if q == "list":
            return list(items)
        return collections.deque(items, maxlen)
    if q in ("dict", "collections.OrderedDict", "collections.Counter", "collections.defaultdict"):
        factory = None
        if q == "collections.defaultdict":
            if args:
                f = args[0]
                if isinstance(f, ast.Constant) and f.value is None:
                    factory = None
                else:
                    fq = ctx.qual(f)
                    if fq not in _DD_FACTORIES:
                        raise _err(node, "unsupported defaultdict factory")
                    factory = _DD_FACTORIES[fq]
                args = args[1:]
        if len(args) > 1:
            raise _err(node)
        if q == "collections.Counter":
            out = collections.Counter()
            if args:
                src = _value(args[0], ctx)
                if isinstance(src, dict):
                    out.update(dict(src))
                else:
                    out.update(_hashable(x, node) for x in _iter_value(src, node))
            for kw in kws:
                out[kw.arg] = _value(kw.value, ctx)
            return out
        base = {}
        if args:
            src = _value(args[0], ctx)
            if isinstance(src, dict):
                base.update(src)
            else:
                for pair in _iter_value(src, node):
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        raise _err(node)
                    base[_hashable(pair[0], node)] = pair[1]
        for kw in kws:
            base[kw.arg] = _value(kw.value, ctx)
        if q == "dict":
            return base
        if q == "collections.OrderedDict":
            return collections.OrderedDict(base)
        return collections.defaultdict(factory, base)
    if q == "decimal.Decimal":
        if kws or len(args) > 1:
            raise _err(node)
        if not args:
            return decimal.Decimal()
        v = _value(args[0], ctx)
        if isinstance(v, bool) or not isinstance(v, (int, float, str, tuple)):
            raise _err(node)
        try:
            return decimal.Decimal(v)
        except Exception:
            raise _err(node, "invalid Decimal")
    if q == "float":
        if kws or len(args) != 1:
            raise _err(node)
        v = _value(args[0], ctx)
        if isinstance(v, bool) or not isinstance(v, (int, float, str, decimal.Decimal)):
            raise _err(node)
        try:
            return float(v)
        except ValueError:
            raise _err(node, "invalid float")
    raise _err(node)


def _is_value(node, ctx) -> bool:
    try:
        _value(node, ctx)
        return True
    except DiscoveryError:
        return False


def _call_matches(func: ast.AST, ctx):
    """Return True if ``func`` refers to the entrypoint."""
    ep_name = ctx.ep
    if ep_name is None:
        q = ctx.qual(func)
        if q in _HELPER_CALLS or (q and q.split(".")[0] in ("math", "re", "collections", "decimal")):
            return False
    if isinstance(func, ast.Name):
        return ep_name is None or func.id == ep_name
    if isinstance(func, ast.Attribute):
        if ep_name is not None and func.attr != ep_name:
            return False
        base = func.value
        if isinstance(base, ast.Name):
            return True
        if (isinstance(base, ast.Call) and isinstance(base.func, ast.Name)
                and not base.args and not base.keywords):
            return True
    return False


def _parse_call(node: ast.AST, ctx):
    if not isinstance(node, ast.Call) or not _call_matches(node.func, ctx):
        return None
    args = []
    for a in node.args:
        if isinstance(a, ast.Starred):
            raise DiscoveryError(f"line {node.lineno}: unsupported argument")
        args.append(_value(a, ctx))
    kwargs = []
    for kw in node.keywords:
        if kw.arg is None:
            raise DiscoveryError(f"line {node.lineno}: unsupported keyword argument")
        kwargs.append([kw.arg, _value(kw.value, ctx)])
    return {"args": args, "kwargs": kwargs}


def _msg_ok(msg) -> bool:
    return msg is None or (isinstance(msg, ast.Constant) and isinstance(msg.value, str))


def _num_const(node, ctx, what="tolerance"):
    v = _value(node, ctx)
    if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
        raise _err(node, f"{what} must be a number")
    v = float(v)
    if math.isnan(v) or v < 0:
        raise _err(node, f"invalid {what}")
    return v


def _call_vs_value(a, b, ctx, lineno):
    """One of a/b is the entrypoint call, the other a value."""
    call = _parse_call(a, ctx)
    other = b
    if call is None:
        call = _parse_call(b, ctx)
        other = a
    if call is None:
        return None
    try:
        expected = _value(other, ctx)
    except DiscoveryError:
        raise DiscoveryError(f"line {lineno}: unsupported expected value")
    return call, expected


def _parse_check(t, ctx, lineno):
    """Parse an assertion expression into a test dict (without line)."""
    call = _parse_call(t, ctx)
    if call is not None:
        return dict(kind="truthy", **call)
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
        inner = _parse_check(t.operand, ctx, lineno)
        if inner is not None:
            flip = {"truthy": "falsy", "falsy": "truthy", "eq": "ne", "ne": "eq"}
            inner["kind"] = flip[inner["kind"]]
            return inner
        return None
    if isinstance(t, ast.Compare) and len(t.ops) == 1:
        op = t.ops[0]
        left, right = t.left, t.comparators[0]
        if isinstance(op, (ast.Eq, ast.NotEq)):
            r = _call_vs_value(left, right, ctx, lineno)
            if r is not None:
                call, expected = r
                kind = "eq" if isinstance(op, ast.Eq) else "ne"
                return dict(kind=kind, expected=expected, **call)
        if isinstance(op, (ast.Is, ast.IsNot)):
            for a, b in ((left, right), (right, left)):
                if isinstance(b, ast.Constant) and b.value is None:
                    call = _parse_call(a, ctx)
                    if call is not None:
                        kind = "eq" if isinstance(op, ast.Is) else "ne"
                        return dict(kind=kind, expected=None, **call)
        # abs(call - value) < tol  /  tol > abs(call - value)
        absnode = tolnode = None
        strict = None
        if isinstance(op, (ast.Lt, ast.LtE)):
            absnode, tolnode, strict = left, right, isinstance(op, ast.Lt)
        elif isinstance(op, (ast.Gt, ast.GtE)):
            absnode, tolnode, strict = right, left, isinstance(op, ast.Gt)
        if absnode is not None and _is_abs_diff(absnode, ctx):
            d = absnode.args[0]
            r = _call_vs_value(d.left, d.right, ctx, lineno)
            if r is not None:
                call, expected = r
                tol = _num_const(tolnode, ctx)
                cmp = {"mode": "abs_lt" if strict else "abs_le", "tol": tol}
                return dict(kind="eq", expected=expected, cmp=cmp, **call)
    if isinstance(t, ast.Call) and ctx.qual(t.func) == "math.isclose":
        if len(t.args) != 2 or any(isinstance(a, ast.Starred) for a in t.args):
            raise DiscoveryError(f"line {lineno}: unsupported isclose call")
        rel, abs_ = 1e-9, 0.0
        for kw in t.keywords:
            if kw.arg == "rel_tol":
                rel = _num_const(kw.value, ctx)
            elif kw.arg == "abs_tol":
                abs_ = _num_const(kw.value, ctx)
            else:
                raise DiscoveryError(f"line {lineno}: unsupported isclose argument")
        r = _call_vs_value(t.args[0], t.args[1], ctx, lineno)
        if r is not None:
            call, expected = r
            cmp = {"mode": "isclose", "rel": rel, "abs": abs_}
            return dict(kind="eq", expected=expected, cmp=cmp, **call)
    return None


def _is_abs_diff(node, ctx):
    return (isinstance(node, ast.Call) and ctx.qual(node.func) == "abs"
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.BinOp) and isinstance(node.args[0].op, ast.Sub))


def _parse_assert(stmt: ast.Assert, ctx):
    if not _msg_ok(stmt.msg):
        raise DiscoveryError(f"line {stmt.lineno}: unsupported assert message")
    t = _parse_check(stmt.test, ctx, stmt.lineno)
    if t is None:
        raise DiscoveryError(f"line {stmt.lineno}: unsupported assertion")
    return t


# ---- typed raises -------------------------------------------------------

def _exc_names(node, ctx):
    if node is None:
        return None
    nodes = node.elts if isinstance(node, ast.Tuple) else [node]
    names = []
    for n in nodes:
        if isinstance(n, ast.Name):
            names.append(n.id)
        elif isinstance(n, ast.Attribute):
            names.append(n.attr)
        else:
            return False
    if any(n in ("Exception", "BaseException") for n in names):
        return None
    return names


def _is_str_of(node, var):
    return (var is not None and isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "str" and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Name) and node.args[0].id == var)


def _str_const(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _re_flags(node, ctx):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _re_flags(node.left, ctx) | _re_flags(node.right, ctx)
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    q = ctx.qual(node)
    if q and q.startswith("re."):
        name = q[3:]
        if name.startswith("RegexFlag."):
            name = name[len("RegexFlag."):]
        if name in _RE_FLAG_VALUES:
            return int(_RE_FLAG_VALUES[name])
    raise _err(node, "unsupported regex flags")


def _py_re_to_js(pattern, flags):
    """Convert a Python regex to a JS (source, flags) pair."""
    jsflags = ""
    m = re.match(r"^\(\?([aiLmsux]+)\)", pattern)
    if m:
        for ch in m.group(1):
            flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A,
                      "u": re.U, "L": 0}[ch]
        pattern = pattern[m.end():]
    if flags & re.X:
        # strip verbose-mode whitespace and comments (outside classes)
        out, i, in_cls = [], 0, False
        while i < len(pattern):
            c = pattern[i]
            if c == "\\" and i + 1 < len(pattern):
                out.append(pattern[i:i + 2])
                i += 2
                continue
            if in_cls:
                if c == "]":
                    in_cls = False
                out.append(c)
            elif c == "[":
                in_cls = True
                out.append(c)
            elif c in " \t\n\r\f\v":
                pass
            elif c == "#":
                while i < len(pattern) and pattern[i] != "\n":
                    i += 1
            else:
                out.append(c)
            i += 1
        pattern = "".join(out)
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            n = pattern[i + 1]
            if n == "A":
                out.append("(?<![\\s\\S])")
            elif n == "Z":
                out.append("(?![\\s\\S])")
            else:
                out.append(pattern[i:i + 2])
            i += 2
            continue
        if pattern.startswith("(?P<", i):
            out.append("(?<")
            i += 4
            continue
        if pattern.startswith("(?P=", i):
            j = pattern.find(")", i)
            if j > 0:
                out.append("\\k<" + pattern[i + 4:j] + ">")
                i = j + 1
                continue
        out.append(c)
        i += 1
    if flags & re.I:
        jsflags += "i"
    if flags & re.M:
        jsflags += "m"
    if flags & re.S:
        jsflags += "s"
    return "".join(out), jsflags


def _parse_msg_check(test, var, ctx, lineno):
    err = DiscoveryError(f"line {lineno}: unsupported exception assertion")
    negate = False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        negate = True
        test = test.operand
    chk = None
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, l, r = test.ops[0], test.left, test.comparators[0]
        if isinstance(op, (ast.In, ast.NotIn)) and _str_const(l) is not None and _is_str_of(r, var):
            chk = {"op": "in", "v": _str_const(l)}
            if isinstance(op, ast.NotIn):
                negate = not negate
        elif isinstance(op, (ast.Eq, ast.NotEq)):
            s = None
            if _is_str_of(l, var):
                s = _str_const(r)
            elif _is_str_of(r, var):
                s = _str_const(l)
            if s is not None:
                chk = {"op": "eq", "v": s}
                if isinstance(op, ast.NotEq):
                    negate = not negate
    elif isinstance(test, ast.Call):
        q = ctx.qual(test.func)
        if q in ("re.search", "re.match", "re.fullmatch"):
            args = list(test.args)
            flags_node = None
            for kw in test.keywords:
                if kw.arg == "flags":
                    flags_node = kw.value
                elif kw.arg == "pattern" and not args:
                    args.insert(0, kw.value)
                elif kw.arg == "string" and len(args) == 1:
                    args.append(kw.value)
                else:
                    raise err
            if len(args) == 3 and flags_node is None:
                flags_node = args.pop()
            if len(args) != 2:
                raise err
            pat = _str_const(args[0])
            if pat is None or not _is_str_of(args[1], var):
                raise err
            flags = _re_flags(flags_node, ctx) if flags_node is not None else 0
            try:
                re.compile(pat, flags)
            except re.error:
                raise DiscoveryError(f"line {lineno}: invalid regex")
            jspat, jsflags = _py_re_to_js(pat, flags)
            chk = {"op": q[3:], "v": pat, "flags": flags, "js": jspat, "jsflags": jsflags}
        elif (isinstance(test.func, ast.Attribute) and test.func.attr in ("startswith", "endswith")
              and _is_str_of(test.func.value, var) and len(test.args) == 1
              and not test.keywords and _str_const(test.args[0]) is not None):
            chk = {"op": test.func.attr, "v": _str_const(test.args[0])}
    if chk is None:
        raise err
    chk["neg"] = negate
    return chk


def _is_assert_false(stmt):
    if isinstance(stmt, ast.Assert):
        return (isinstance(stmt.test, ast.Constant) and stmt.test.value in (False, 0, None)
                and not isinstance(stmt.test.value, str) and _msg_ok(stmt.msg))
    if isinstance(stmt, ast.Raise) and stmt.exc is not None and stmt.cause is None:
        e = stmt.exc
        if isinstance(e, ast.Name) and e.id == "AssertionError":
            return True
        if (isinstance(e, ast.Call) and isinstance(e.func, ast.Name)
                and e.func.id == "AssertionError" and all(_str_const(a) is not None for a in e.args)):
            return True
    return False


def _parse_try(stmt: ast.Try, ctx):
    err = DiscoveryError(f"line {stmt.lineno}: unsupported try block")
    if stmt.orelse or stmt.finalbody or len(stmt.handlers) != 1 or len(stmt.body) != 2:
        raise err
    first, second = stmt.body
    if isinstance(first, ast.Expr):
        callnode = first.value
    elif isinstance(first, ast.Assign) and len(first.targets) == 1 and isinstance(first.targets[0], ast.Name):
        callnode = first.value
    else:
        raise err
    call = _parse_call(callnode, ctx)
    if call is None:
        raise err
    if not _is_assert_false(second):
        raise err
    h = stmt.handlers[0]
    exc = _exc_names(h.type, ctx)
    if exc is False:
        raise err
    checks = []
    for s in h.body:
        if isinstance(s, ast.Pass):
            continue
        if isinstance(s, ast.Expr) and _str_const(s.value) is not None:
            continue
        if isinstance(s, ast.Assert) and _msg_ok(s.msg):
            checks.append(_parse_msg_check(s.test, h.name, ctx, s.lineno))
            continue
        raise err
    return dict(kind="raises", exc=exc, checks=checks, **call)


EXPECT_RE = re.compile(r"^#\s*expect_(stdout|stderr)\s*:\s*(.*?)\s*$")


def _comment_lines(source: str):
    """Map line number -> comment text for lines that contain only a comment."""
    result = {}
    lines = source.splitlines()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as e:
        raise DiscoveryError(f"tokenize error: {e}")
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            ln = tok.start[0]
            if lines[ln - 1][: tok.start[1]].strip() == "":
                result[ln] = tok.string
    return result, lines


def _expectations(line: int, comments, lines):
    exp = {}
    ln = line - 1
    while ln >= 1 and ln in comments:
        m = EXPECT_RE.match(comments[ln].strip())
        if m:
            kind = m.group(1)
            try:
                val = ast.literal_eval(m.group(2))
            except Exception:
                raise DiscoveryError(f"line {ln}: invalid expect_{kind} literal")
            if not isinstance(val, str):
                raise DiscoveryError(f"line {ln}: expect_{kind} must be a string literal")
            if kind not in exp:  # nearest comment wins
                exp[kind] = val
        ln -= 1
    return exp


def discover(tests_dir: str, ep_name=None):
    """Parse tests.py. Returns list of test dicts (with ``id``)."""
    path = os.path.join(tests_dir, "tests.py")
    if not os.path.isfile(path):
        raise DiscoveryError("tests.py not found")
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise DiscoveryError(str(e))
    try:
        tree = ast.parse(source, filename="tests.py")
    except (SyntaxError, ValueError) as e:
        raise DiscoveryError(f"syntax error: {e}")
    comments, lines = _comment_lines(source)
    ctx = _Ctx(ep_name, _collect_aliases(tree))

    tests = []

    def walk(body):
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    raise DiscoveryError(f"line {stmt.lineno}: decorators not allowed")
                walk(stmt.body)
            elif isinstance(stmt, ast.Assert):
                t = _parse_assert(stmt, ctx)
                t["line"] = stmt.lineno
                tests.append(t)
            elif isinstance(stmt, ast.Try):
                t = _parse_try(stmt, ctx)
                t["line"] = stmt.lineno
                tests.append(t)
            elif isinstance(stmt, (ast.Pass, ast.Import, ast.ImportFrom)):
                pass
            elif (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                  and isinstance(stmt.value.value, str)):
                pass  # docstring / bare string
            else:
                raise DiscoveryError(
                    f"line {stmt.lineno}: unsupported statement {type(stmt).__name__}")

    walk(tree.body)

    counts = {}
    for t in tests:
        counts[t["line"]] = counts.get(t["line"], 0) + 1
    seen = {}
    for t in tests:
        ln = t["line"]
        if counts[ln] > 1:
            idx = seen.get(ln, 0)
            seen[ln] = idx + 1
            t["id"] = f"tests.py:{ln}#{idx}"
        else:
            t["id"] = f"tests.py:{ln}"
        exp = _expectations(ln, comments, lines)
        t["stdout"] = exp.get("stdout")
        t["stderr"] = exp.get("stderr")
    return tests


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------

def _sorted_items(items):
    items = list(items)
    try:
        return sorted(items, key=lambda x: (type(x).__name__, repr(x)))
    except Exception:
        return items


def py_literal(v) -> str:
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "float('nan')"
        if math.isinf(v):
            return "float('inf')" if v > 0 else "float('-inf')"
        return repr(v)
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return "_decimal.Decimal(" + repr(str(v)) + ")"
    if isinstance(v, collections.deque):
        inner = "[" + ", ".join(py_literal(x) for x in v) + "]"
        if v.maxlen is not None:
            return "_collections.deque(" + inner + ", " + repr(v.maxlen) + ")"
        return "_collections.deque(" + inner + ")"
    if isinstance(v, list):
        return "[" + ", ".join(py_literal(x) for x in v) + "]"
    if isinstance(v, tuple):
        if len(v) == 1:
            return "(" + py_literal(v[0]) + ",)"
        return "(" + ", ".join(py_literal(x) for x in v) + ")"
    if isinstance(v, (set, frozenset)):
        inner = "{" + ", ".join(py_literal(x) for x in _sorted_items(v)) + "}"
        if isinstance(v, frozenset):
            return "frozenset(" + (inner if v else "") + ")"
        return inner if v else "set()"
    if isinstance(v, dict):
        inner = "{" + ", ".join(py_literal(k) + ": " + py_literal(x) for k, x in v.items()) + "}"
        if isinstance(v, collections.Counter):
            return "_collections.Counter(" + inner + ")"
        if isinstance(v, collections.defaultdict):
            f = v.default_factory
            fname = "None" if f is None else _DD_FACTORY_NAMES.get(f, "None")
            if fname.startswith("collections."):
                fname = "_" + fname
            elif fname.startswith("decimal."):
                fname = "_" + fname
            return "_collections.defaultdict(" + fname + ", " + inner + ")"
        if isinstance(v, collections.OrderedDict):
            return "_collections.OrderedDict(" + inner + ")"
        return inner
    raise TypeError(type(v))


def _js_float(v):
    if math.isnan(v):
        return {"$bcg": "float", "v": "nan"}
    if math.isinf(v):
        return {"$bcg": "float", "v": "inf" if v > 0 else "-inf"}
    return v


def json_safe(v):
    """Convert a value to tagged JSON-compatible data for the JS testers."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return _js_float(v)
    if isinstance(v, decimal.Decimal):
        return {"$bcg": "dec", "v": str(v)}
    if isinstance(v, (list, tuple, collections.deque)):
        return [json_safe(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return {"$bcg": "set", "v": [json_safe(x) for x in _sorted_items(v)]}
    if isinstance(v, dict):
        return {"$bcg": "dict", "c": isinstance(v, collections.Counter),
                "v": [[json_safe(k), json_safe(x)] for k, x in v.items()]}
    raise TypeError(type(v))


def json_safe_record(r):
    out = dict(r)
    out["args"] = [json_safe(a) for a in r["args"]]
    out["kwargs"] = [[k, json_safe(v)] for k, v in r["kwargs"]]
    out["expected"] = json_safe(r.get("expected"))
    return out


# --------------------------------------------------------------------------
# Tester templates
# --------------------------------------------------------------------------

PY_TEMPLATE = r'''# Auto-generated by babel_code_goat.py -- do not edit.
# __META__
# Usage: python tester.py <solution.py> [--start N]
import collections as _collections
import collections.abc as _abc
import copy
import decimal as _decimal
import importlib.util
import inspect
import io
import json
import math
import os
import re
import sys

MARKER = __MARKER__
ENTRYPOINT = __ENTRYPOINT__
CLASS_HINT = __CLASS_HINT__
TESTS = __TESTS__
TOL = __DEFAULT_TOL__

_REAL_OUT = sys.stdout


def emit(obj):
    _REAL_OUT.write(MARKER + json.dumps(obj) + "\n")
    _REAL_OUT.flush()


class _Capture:
    def __enter__(self):
        self.old = (sys.stdout, sys.stderr)
        self.out = io.StringIO()
        self.err = io.StringIO()
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *exc):
        sys.stdout, sys.stderr = self.old
        return False


def load_solution(path):
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    if d not in sys.path:
        sys.path.insert(0, d)
    name = os.path.splitext(os.path.basename(path))[0] or "solution"
    if not name.isidentifier() or name in sys.modules:
        name = "_bcg_solution"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _class_factory(cls, name):
    try:
        static = inspect.getattr_static(cls, name)
    except AttributeError:
        return None
    if isinstance(static, (staticmethod, classmethod)):
        return lambda: getattr(cls, name)
    return lambda: getattr(cls(), name)


def resolve(mod, name):
    ns = vars(mod)
    if CLASS_HINT:
        c = ns.get(CLASS_HINT)
        if isinstance(c, type):
            f = _class_factory(c, name)
            if f is not None:
                return f
    v = ns.get(name)
    if v is not None and callable(v):
        return lambda: v
    classes = [o for o in list(ns.values()) if isinstance(o, type)]
    local = [c for c in classes if getattr(c, "__module__", None) == mod.__name__]
    for c in local + [c for c in classes if c not in local]:
        if hasattr(c, name):
            f = _class_factory(c, name)
            if f is not None:
                return f
    return None


def _is_num(x):
    return isinstance(x, (int, float, _decimal.Decimal))


def num_eq(a, b, cmp):
    mode = cmp["mode"] if cmp else "default"
    try:
        if a == b:
            return True
    except Exception:
        pass
    if mode == "default" and isinstance(a, int) and isinstance(b, int):
        return False
    try:
        if isinstance(a, _decimal.Decimal) and isinstance(b, _decimal.Decimal):
            if not (a.is_finite() and b.is_finite()):
                return False
            diff = float(abs(a - b))
            fa, fb = float(a), float(b)
        else:
            fa, fb = float(a), float(b)
            if math.isnan(fa) or math.isnan(fb):
                return False
            if math.isinf(fa) or math.isinf(fb):
                return fa == fb
            diff = abs(fa - fb)
    except Exception:
        return False
    if math.isnan(diff):
        return False
    if mode == "default":
        return diff <= TOL
    if mode == "isclose":
        return diff <= max(cmp["rel"] * max(abs(fa), abs(fb)), cmp["abs"])
    if mode == "abs_lt":
        return diff < cmp["tol"]
    if mode == "abs_le":
        return diff <= cmp["tol"]
    return False


_SEQ = (list, tuple, _collections.deque)


def _contains(container, x):
    try:
        return x in container
    except TypeError:
        return False


def _nonzero(m):
    out = {}
    for k, v in m.items():
        try:
            z = (v == 0)
        except Exception:
            z = False
        if not z:
            out[k] = v
    return out


def deep_eq(a, b, cmp=None):
    if b is None:
        return a is None
    if isinstance(b, bool):
        if _is_num(a):
            try:
                return bool(a == b)
            except Exception:
                return False
        return False
    if _is_num(b):
        if _is_num(a):
            return num_eq(a, b, cmp)
        return False
    if isinstance(b, str):
        return isinstance(a, str) and a == b
    if isinstance(b, _SEQ):
        if not isinstance(a, _SEQ) or len(a) != len(b):
            return False
        return all(deep_eq(x, y, cmp) for x, y in zip(a, b))
    if isinstance(b, _collections.Counter):
        if not isinstance(a, _abc.Mapping):
            return False
        bm, am = _nonzero(b), _nonzero(a)
        if len(am) != len(bm):
            return False
        for k, v in bm.items():
            if not _contains(am, k) or not deep_eq(am[k], v, cmp):
                return False
        return True
    if isinstance(b, dict):
        if not isinstance(a, _abc.Mapping) or len(a) != len(b):
            return False
        for k, v in b.items():
            if not _contains(a, k) or not deep_eq(a[k], v, cmp):
                return False
        return True
    if isinstance(b, (set, frozenset)):
        if not isinstance(a, (set, frozenset)) or len(a) != len(b):
            return False
        rest = [x for x in b if not _contains(a, x)]
        if not rest:
            return True
        pool = [y for y in a if not _contains(b, y)]
        for x in rest:
            for j, y in enumerate(pool):
                if deep_eq(y, x, cmp):
                    del pool[j]
                    break
            else:
                return False
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


def _exc_matches(e, names):
    if names is None:
        return isinstance(e, Exception)
    mro_names = {c.__name__ for c in type(e).__mro__}
    return any(n in mro_names for n in names)


def _check_msg(e, chk):
    try:
        s = str(e)
    except Exception:
        return False
    op = chk["op"]
    v = chk["v"]
    try:
        if op == "in":
            r = v in s
        elif op == "eq":
            r = s == v
        elif op == "startswith":
            r = s.startswith(v)
        elif op == "endswith":
            r = s.endswith(v)
        elif op == "search":
            r = re.search(v, s, chk.get("flags", 0)) is not None
        elif op == "match":
            r = re.match(v, s, chk.get("flags", 0)) is not None
        elif op == "fullmatch":
            r = re.fullmatch(v, s, chk.get("flags", 0)) is not None
        else:
            return False
    except Exception:
        return False
    return (not r) if chk.get("neg") else r


def run_test(factory, t):
    kind = t["kind"]
    args = copy.deepcopy(t["args"])
    kwargs = {k: copy.deepcopy(v) for k, v in t["kwargs"]}
    cmp = t.get("cmp")
    ok = False
    with _Capture() as cap:
        try:
            fn = factory()
        except BaseException:
            fn = None
        if fn is not None:
            if kind == "raises":
                try:
                    fn(*args, **kwargs)
                    ok = False
                except BaseException as e:
                    ok = _exc_matches(e, t.get("exc"))
                    if ok:
                        for chk in t.get("checks") or []:
                            if not _check_msg(e, chk):
                                ok = False
                                break
            else:
                try:
                    res = fn(*args, **kwargs)
                    if kind == "eq":
                        ok = deep_eq(res, t["expected"], cmp)
                    elif kind == "ne":
                        ok = not deep_eq(res, t["expected"], cmp)
                    elif kind == "truthy":
                        ok = bool(res)
                    elif kind == "falsy":
                        ok = not bool(res)
                except BaseException:
                    ok = False
    if ok and t.get("stdout") is not None and cap.out.getvalue() != t["stdout"]:
        ok = False
    if ok and t.get("stderr") is not None and cap.err.getvalue() != t["stderr"]:
        ok = False
    return ok


def main(argv):
    if len(argv) < 1:
        sys.stderr.write("usage: tester.py <solution.py> [--start N]\n")
        return 2
    sol = argv[0]
    start = 0
    if "--start" in argv:
        start = int(argv[argv.index("--start") + 1])
    global TOL
    if "--tol" in argv:
        TOL = float(argv[argv.index("--tol") + 1])
    try:
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    except Exception:
        pass
    passed, failed = [], []
    factory = None
    with _Capture():
        try:
            mod = load_solution(sol)
            factory = resolve(mod, ENTRYPOINT)
        except BaseException as e:  # noqa
            factory = None
    if factory is None:
        emit({"event": "load_error"})
        failed = [t["id"] for t in TESTS[start:]]
    else:
        for i in range(start, len(TESTS)):
            t = TESTS[i]
            emit({"event": "start", "index": i, "id": t["id"]})
            ok = run_test(factory, t)
            emit({"event": "result", "index": i, "id": t["id"], "ok": ok})
            (passed if ok else failed).append(t["id"])
    emit({"event": "done"})
    status = "pass" if not failed else "fail"
    _REAL_OUT.write(json.dumps({"status": status, "passed": passed, "failed": failed}, separators=(",", ":")) + "\n")
    _REAL_OUT.flush()
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)
'''


JS_TEMPLATE = r'''__HEADER__
// Auto-generated by babel_code_goat.py -- do not edit.
// __META__
// Usage: node __FILENAME__ <solution__SOLEXT__> [--start N]
(function () {
"use strict";
const fs = process.getBuiltinModule("fs");
const pathMod = process.getBuiltinModule("path");
const vm = process.getBuiltinModule("vm");
const nodeModule = process.getBuiltinModule("module");

const MARKER = __MARKER__;
const ENTRYPOINT = __ENTRYPOINT__;
const CLASS_HINT = __CLASS_HINT__;
const IS_TS = __IS_TS__;
function PySet(items) { this.items = items; }
function PyDec(s) { this.s = s; this.n = Number(s); }
function PyDict(entries, counter) { this.entries = entries; this.counter = !!counter; }
const TESTS = JSON.parse(__TESTS__, function (k, v) {
  if (v && typeof v === "object" && !Array.isArray(v) && typeof v["$bcg"] === "string") {
    const tag = v["$bcg"];
    if (tag === "float") return v.v === "nan" ? NaN : (v.v === "inf" ? Infinity : -Infinity);
    if (tag === "dec") return new PyDec(v.v);
    if (tag === "set") return new PySet(v.v);
    if (tag === "dict") return new PyDict(v.v, v.c);
  }
  return v;
});
let TOL = __DEFAULT_TOL__;

function emit(obj) {
  fs.writeSync(1, MARKER + JSON.stringify(obj) + "\n");
}

// ---------- output capture ----------
const origOut = process.stdout.write;
const origErr = process.stderr.write;
let capOut = null, capErr = null;
function chunkToString(chunk, enc) {
  if (typeof chunk === "string") return chunk;
  try { return Buffer.from(chunk).toString(typeof enc === "string" ? enc : "utf8"); }
  catch (e) { return String(chunk); }
}
function makeWriter(which) {
  return function (chunk, enc, cb) {
    const s = chunkToString(chunk, enc);
    if (which === "out") { if (capOut !== null) capOut += s; }
    else { if (capErr !== null) capErr += s; }
    if (typeof enc === "function") enc();
    else if (typeof cb === "function") cb();
    return true;
  };
}
process.stdout.write = makeWriter("out");
process.stderr.write = makeWriter("err");

// ---------- python-like semantics ----------
function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}
function isRecord(v) {
  return isPlainObject(v) && !(v instanceof Map) && !(v instanceof Set) &&
    !ArrayBuffer.isView(v) && !(v instanceof PySet) && !(v instanceof PyDict) && !(v instanceof PyDec);
}
function isSeq(v) {
  return Array.isArray(v) || (ArrayBuffer.isView(v) && !(v instanceof DataView));
}
function asNumber(a) {
  if (typeof a === "number") return a;
  if (typeof a === "boolean") return a ? 1 : 0;
  if (typeof a === "bigint") return Number(a);
  if (a instanceof PyDec) return a.n;
  if (a instanceof Number) return a.valueOf();
  if (a !== null && typeof a === "object" && typeof a.toNumber === "function") {
    try { const n = a.toNumber(); if (typeof n === "number") return n; } catch (e) {}
  }
  return undefined;
}
function numEq(a, b, cmp) {
  if (typeof a === "bigint" && typeof b === "number" && Number.isSafeInteger(b)) {
    if (a === BigInt(b)) return true;
  }
  const na = asNumber(a), nb = asNumber(b);
  if (na === undefined || nb === undefined) return false;
  if (na === nb) return true;
  if (Number.isNaN(na) || Number.isNaN(nb)) return false;
  if (!Number.isFinite(na) || !Number.isFinite(nb)) return false;
  const mode = cmp ? cmp.mode : "default";
  if (mode === "default" && Number.isInteger(na) && Number.isInteger(nb) &&
      !(Number.isSafeInteger(na) === false || Number.isSafeInteger(nb) === false)) return false;
  const diff = Math.abs(na - nb);
  if (mode === "default") return diff <= TOL;
  if (mode === "isclose") return diff <= Math.max(cmp.rel * Math.max(Math.abs(na), Math.abs(nb)), cmp.abs);
  if (mode === "abs_lt") return diff < cmp.tol;
  if (mode === "abs_le") return diff <= cmp.tol;
  return false;
}
function keyStr(k) {
  if (typeof k === "string") return k;
  if (k === null || k === undefined) return "null";
  if (Array.isArray(k)) return k.map(keyStr).join(",");
  if (k instanceof PyDec) return String(k.n);
  return String(k);
}
function isZero(v) {
  const n = asNumber(v);
  return n === 0;
}
function mapEntries(a) {
  if (a instanceof Map) return Array.from(a.entries());
  if (isRecord(a)) return Object.keys(a).map(function (k) { return [k, a[k]]; });
  return null;
}
function dictEqual(a, b, cmp) {
  let exp = b.entries;
  if (b.counter) exp = exp.filter(function (e) { return !isZero(e[1]); });
  let act = mapEntries(a);
  if (act === null) return false;
  if (b.counter) act = act.filter(function (e) { return !isZero(e[1]); });
  if (act.length !== exp.length) return false;
  const used = new Array(act.length).fill(false);
  const isMap = a instanceof Map;
  for (const [k, v] of exp) {
    let found = -1;
    if (isMap) {
      for (let i = 0; i < act.length; i++) {
        if (!used[i] && pyEqual(act[i][0], k, null)) { found = i; break; }
      }
      if (found < 0) {
        const ks = keyStr(k);
        for (let i = 0; i < act.length; i++) {
          if (!used[i] && typeof act[i][0] === "string" && act[i][0] === ks) { found = i; break; }
        }
      }
    } else {
      const ks = keyStr(k);
      for (let i = 0; i < act.length; i++) {
        if (!used[i] && act[i][0] === ks) { found = i; break; }
      }
    }
    if (found < 0) return false;
    used[found] = true;
    if (!pyEqual(act[found][1], v, cmp)) return false;
  }
  return true;
}
function setEqual(a, b, cmp) {
  let act;
  if (a instanceof Set) act = Array.from(a);
  else if (a instanceof PySet) act = a.items.slice();
  else return false;
  if (act.length !== b.items.length) return false;
  const used = new Array(act.length).fill(false);
  for (const x of b.items) {
    let found = -1;
    for (let i = 0; i < act.length; i++) {
      if (!used[i] && pyEqual(act[i], x, null)) { found = i; break; }
    }
    if (found < 0 && cmp !== undefined) {
      for (let i = 0; i < act.length; i++) {
        if (!used[i] && pyEqual(act[i], x, cmp)) { found = i; break; }
      }
    }
    if (found < 0) return false;
    used[found] = true;
  }
  return true;
}
function pyEqual(a, b, cmp) {
  if (b === null || b === undefined) return a === null || a === undefined;
  if (typeof b === "boolean") {
    const na = asNumber(a);
    if (na === undefined || a instanceof PyDec) return false;
    return na === (b ? 1 : 0);
  }
  if (typeof b === "number" || b instanceof PyDec) {
    if (typeof a === "string" || a === null || a === undefined) return false;
    return numEq(a, b, cmp);
  }
  if (typeof b === "string") return a === b || (a instanceof String && a.valueOf() === b);
  if (Array.isArray(b)) {
    if (!isSeq(a)) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < b.length; i++) if (!pyEqual(a[i], b[i], cmp)) return false;
    return true;
  }
  if (b instanceof PySet) return setEqual(a, b, cmp);
  if (b instanceof PyDict) return dictEqual(a, b, cmp);
  return false;
}
function pyTruthy(v) {
  if (v === null || v === undefined || v === false) return false;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "bigint") return v !== BigInt(0);
  if (typeof v === "string") return v.length > 0;
  if (Array.isArray(v)) return v.length > 0;
  if (v instanceof Map || v instanceof Set) return v.size > 0;
  if (ArrayBuffer.isView(v) && !(v instanceof DataView)) return v.length > 0;
  if (isPlainObject(v) && (Object.getPrototypeOf(v) === Object.prototype || Object.getPrototypeOf(v) === null))
    return Object.keys(v).length > 0;
  return true;
}
// Convert an encoded test value into a fresh native JS value.
function toJS(v) {
  if (Array.isArray(v)) return v.map(toJS);
  if (v instanceof PyDec) return v.n;
  if (v instanceof PySet) return new Set(v.items.map(toJS));
  if (v instanceof PyDict) {
    if (v.entries.every(function (e) { return typeof e[0] === "string"; })) {
      const o = {};
      for (const [k, x] of v.entries) Object.defineProperty(o, k, { value: toJS(x), enumerable: true, writable: true, configurable: true });
      return o;
    }
    return new Map(v.entries.map(function (e) { return [toJS(e[0]), toJS(e[1])]; }));
  }
  return v;
}

// ---------- exceptions ----------
const PY_EXC_PARENT = {
  BaseException: null, Exception: "BaseException", SystemExit: "BaseException",
  KeyboardInterrupt: "BaseException", GeneratorExit: "BaseException",
  ArithmeticError: "Exception", ZeroDivisionError: "ArithmeticError",
  OverflowError: "ArithmeticError", FloatingPointError: "ArithmeticError",
  AssertionError: "Exception", AttributeError: "Exception", BufferError: "Exception",
  EOFError: "Exception", ImportError: "Exception", ModuleNotFoundError: "ImportError",
  LookupError: "Exception", IndexError: "LookupError", KeyError: "LookupError",
  MemoryError: "Exception", NameError: "Exception", UnboundLocalError: "NameError",
  OSError: "Exception", FileNotFoundError: "OSError", FileExistsError: "OSError",
  PermissionError: "OSError", TimeoutError: "OSError", IsADirectoryError: "OSError",
  NotADirectoryError: "OSError", ConnectionError: "OSError",
  RuntimeError: "Exception", NotImplementedError: "RuntimeError", RecursionError: "RuntimeError",
  StopIteration: "Exception", StopAsyncIteration: "Exception",
  SyntaxError: "Exception", SystemError: "Exception", TypeError: "Exception",
  ValueError: "Exception", UnicodeError: "ValueError", UnicodeDecodeError: "UnicodeError",
  UnicodeEncodeError: "UnicodeError", Warning: "Exception"
};
const JS_BUILTIN_ERRORS = new Set(["Error", "RangeError", "TypeError", "SyntaxError",
  "ReferenceError", "EvalError", "URIError", "AggregateError", "Object", ""]);
function errorNames(e) {
  const names = [];
  if (e !== null && (typeof e === "object" || typeof e === "function")) {
    try { if (typeof e.name === "string") names.push(e.name); } catch (x) {}
    let p = null;
    try { p = Object.getPrototypeOf(e); } catch (x) {}
    let guard = 0;
    while (p && guard++ < 50) {
      try {
        if (p.constructor && typeof p.constructor.name === "string") names.push(p.constructor.name);
      } catch (x) {}
      try { p = Object.getPrototypeOf(p); } catch (x) { p = null; }
    }
  }
  return names;
}
function excMatches(e, wanted) {
  if (wanted === null || wanted === undefined) return true;
  const names = errorNames(e);
  for (const n of names) {
    if (wanted.indexOf(n) >= 0) return true;
    let p = Object.prototype.hasOwnProperty.call(PY_EXC_PARENT, n) ? PY_EXC_PARENT[n] : undefined;
    while (p) {
      if (wanted.indexOf(p) >= 0) return true;
      p = PY_EXC_PARENT[p];
    }
  }
  // the error names a (different) Python exception: mismatch
  for (const n of names) {
    if (!JS_BUILTIN_ERRORS.has(n) && Object.prototype.hasOwnProperty.call(PY_EXC_PARENT, n)) return false;
  }
  // generic JS errors / custom classes carry no Python type: accept
  return true;
}
function errorMessage(e) {
  if (e !== null && typeof e === "object" && typeof e.message === "string") return e.message;
  try { return String(e); } catch (x) { return ""; }
}
function checkMsg(e, chk) {
  const s = errorMessage(e);
  let r = false;
  try {
    if (chk.op === "in") r = s.indexOf(chk.v) >= 0;
    else if (chk.op === "eq") r = s === chk.v;
    else if (chk.op === "startswith") r = s.startsWith(chk.v);
    else if (chk.op === "endswith") r = s.endsWith(chk.v);
    else if (chk.op === "search") r = new RegExp(chk.js, chk.jsflags).test(s);
    else if (chk.op === "match") r = new RegExp(chk.js, chk.jsflags + "y").test(s);
    else if (chk.op === "fullmatch") r = new RegExp("(?:" + chk.js + ")(?![\\s\\S])", chk.jsflags + "y").test(s);
    else return false;
  } catch (x) { return false; }
  return chk.neg ? !r : r;
}

// ---------- solution loading ----------
function findTypeScript(fromPath) {
  const candidates = [fromPath, __filename_safe(), pathMod.join(process.cwd(), "x.js")];
  const npmRoots = [];
  try { npmRoots.push(pathMod.join(pathMod.dirname(pathMod.dirname(process.execPath)), "lib", "node_modules", "x.js")); } catch (e) {}
  npmRoots.push("/usr/lib/node_modules/x.js", "/usr/local/lib/node_modules/x.js");
  for (const c of candidates.concat(npmRoots)) {
    if (!c) continue;
    try { return nodeModule.createRequire(c)("typescript"); } catch (e) {}
  }
  return null;
}
function __filename_safe() {
  try { return pathMod.resolve(process.argv[1] || "x.js"); } catch (e) { return null; }
}
function stripTypes(src, file) {
  if (typeof nodeModule.stripTypeScriptTypes === "function") {
    try { return nodeModule.stripTypeScriptTypes(src, { mode: "transform" }); } catch (e) {}
  }
  const ts = findTypeScript(file);
  if (ts) {
    const out = ts.transpileModule(src, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
    return out.outputText;
  }
  return src;
}
function esmToScript(src) {
  src = src.replace(/^([ \t]*)import\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+(['"][^'"]+['"])\s*;?/gm,
    "$1const $2 = require($3);");
  src = src.replace(/^([ \t]*)import\s+([A-Za-z_$][\w$]*)\s*,\s*\{([^}]*)\}\s*from\s+(['"][^'"]+['"])\s*;?/gm,
    function (m, ind, def, names, mod) {
      return ind + "const " + def + " = require(" + mod + "); const {" + names.replace(/\bas\b/g, ":") + "} = require(" + mod + ");";
    });
  src = src.replace(/^([ \t]*)import\s+\{([^}]*)\}\s*from\s+(['"][^'"]+['"])\s*;?/gm,
    function (m, ind, names, mod) { return ind + "const {" + names.replace(/\bas\b/g, ":") + "} = require(" + mod + ");"; });
  src = src.replace(/^([ \t]*)import\s+([A-Za-z_$][\w$]*)\s+from\s+(['"][^'"]+['"])\s*;?/gm,
    "$1const $2 = require($3);");
  src = src.replace(/^([ \t]*)import\s+(['"][^'"]+['"])\s*;?/gm, "$1require($2);");
  src = src.replace(/^([ \t]*)export\s+default\s+(?=(async\s+)?function\b|class\b)/gm, "$1");
  src = src.replace(/^([ \t]*)export\s+(?=(async\s+)?function\b|class\b|const\b|let\b|var\b|abstract\b)/gm, "$1");
  src = src.replace(/^([ \t]*)export\s*\{[^}]*\}(\s*from\s*['"][^'"]+['"])?\s*;?/gm, "$1");
  src = src.replace(/^([ \t]*)export\s+default\s+/gm, "$1module.exports.default = ");
  return src;
}
function isIdent(s) { return /^[A-Za-z_$][\w$]*$/.test(s); }
function lookupGlobal(name) {
  if (!isIdent(name)) return undefined;
  try { return vm.runInThisContext("typeof " + name + " !== 'undefined' ? " + name + " : undefined"); }
  catch (e) { return undefined; }
}
function classFactory(obj, name) {
  if (obj === null || obj === undefined) return null;
  if (typeof obj === "function") {
    if (typeof obj[name] === "function") return function () { return obj[name].bind(obj); };
    if (obj.prototype && typeof obj.prototype[name] === "function") {
      return function () { const inst = new obj(); return inst[name].bind(inst); };
    }
    return null;
  }
  if (typeof obj === "object" && typeof obj[name] === "function") {
    return function () { return obj[name].bind(obj); };
  }
  return null;
}
function loadSolution(solPath) {
  const abs = pathMod.resolve(solPath);
  let src = fs.readFileSync(abs, "utf8");
  if (src.startsWith("#!")) src = "//" + src.slice(2);
  if (IS_TS || /\.(ts|mts|cts|tsx)$/.test(abs)) src = stripTypes(src, abs);
  src = esmToScript(src);
  const mod = { exports: {} };
  globalThis.module = mod;
  globalThis.exports = mod.exports;
  globalThis.require = nodeModule.createRequire(abs);
  globalThis.__filename = abs;
  globalThis.__dirname = pathMod.dirname(abs);
  vm.runInThisContext(src, { filename: abs });
  const names = new Set();
  const re = /\b(?:class|function|const|let|var)\s+([A-Za-z_$][\w$]*)/g;
  let m;
  while ((m = re.exec(src)) !== null) names.add(m[1]);
  return { mod: mod, names: Array.from(names) };
}
function resolve(loaded, name) {
  const exp = loaded.mod.exports;
  const getters = [];
  if (CLASS_HINT) {
    getters.push(function () { return lookupGlobal(CLASS_HINT); });
    getters.push(function () { return exp && exp[CLASS_HINT]; });
  }
  for (const g of getters) {
    const f = classFactory(g(), name);
    if (f) return f;
  }
  const direct = [lookupGlobal(name), exp && exp[name],
    exp && exp.default && exp.default[name],
    (typeof exp === "function" && exp.name === name) ? exp : undefined,
    exp && typeof exp.default === "function" && exp.default.name === name ? exp.default : undefined];
  for (const v of direct) {
    if (typeof v === "function") return function () { return v; };
  }
  const objs = [];
  for (const n of loaded.names) objs.push(lookupGlobal(n));
  if (exp) {
    for (const k of Object.keys(exp)) objs.push(exp[k]);
    objs.push(exp);
    objs.push(exp.default);
  }
  for (const o of objs) {
    let f = null;
    try { f = classFactory(o, name); } catch (e) { f = null; }
    if (f) return f;
  }
  return null;
}

// ---------- running ----------
async function runTest(factory, t) {
  const args = toJS(t.args).concat(t.kwargs.map(function (kv) { return toJS(kv[1]); }));
  const cmp = t.cmp || null;
  let ok = false;
  capOut = ""; capErr = "";
  try {
    let fn = null;
    try { fn = factory(); } catch (e) { fn = null; }
    if (fn !== null) {
      if (t.kind === "raises") {
        try {
          let r = fn.apply(null, args);
          if (r && typeof r.then === "function") r = await r;
          ok = false;
        } catch (e) {
          ok = excMatches(e, t.exc);
          if (ok) for (const chk of (t.checks || [])) { if (!checkMsg(e, chk)) { ok = false; break; } }
        }
      } else {
        try {
          let res = fn.apply(null, args);
          if (res && typeof res.then === "function") res = await res;
          if (t.kind === "eq") ok = pyEqual(res, t.expected, cmp);
          else if (t.kind === "ne") ok = !pyEqual(res, t.expected, cmp);
          else if (t.kind === "truthy") ok = pyTruthy(res);
          else if (t.kind === "falsy") ok = !pyTruthy(res);
        } catch (e) { ok = false; }
      }
    }
    if (ok && t.stdout !== null && t.stdout !== undefined && capOut !== t.stdout) ok = false;
    if (ok && t.stderr !== null && t.stderr !== undefined && capErr !== t.stderr) ok = false;
  } finally {
    capOut = null; capErr = null;
  }
  return ok;
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.length < 1) {
    origErr.call(process.stderr, "usage: tester <solution> [--start N]\n");
    return 2;
  }
  let start = 0;
  const si = argv.indexOf("--start");
  if (si >= 0) start = parseInt(argv[si + 1], 10) || 0;
  const ti = argv.indexOf("--tol");
  if (ti >= 0) { const v = parseFloat(argv[ti + 1]); if (!Number.isNaN(v)) TOL = v; }
  const passed = [], failed = [];
  let factory = null;
  capOut = ""; capErr = "";
  try {
    const loaded = loadSolution(argv[0]);
    factory = resolve(loaded, ENTRYPOINT);
  } catch (e) {
    factory = null;
  }
  capOut = null; capErr = null;
  if (factory === null) {
    emit({ event: "load_error" });
    for (let i = start; i < TESTS.length; i++) failed.push(TESTS[i].id);
  } else {
    for (let i = start; i < TESTS.length; i++) {
      const t = TESTS[i];
      emit({ event: "start", index: i, id: t.id });
      const ok = await runTest(factory, t);
      emit({ event: "result", index: i, id: t.id, ok: ok });
      (ok ? passed : failed).push(t.id);
    }
  }
  emit({ event: "done" });
  const status = failed.length === 0 ? "pass" : "fail";
  fs.writeSync(1, JSON.stringify({ status: status, passed: passed, failed: failed }) + "\n");
  return status === "pass" ? 0 : 1;
}

main().then(function (code) { process.exit(code); },
  function (e) { process.exit(3); });
})();
'''


def render_tester(lang: str, tests, entrypoint: str) -> str:
    parts = entrypoint.split(".")
    name = parts[-1]
    class_hint = parts[-2] if len(parts) > 1 else None
    meta = META_PREFIX + json.dumps({"entrypoint": entrypoint, "lang": lang,
                                     "ids": [t["id"] for t in tests]})
    records = []
    for t in tests:
        records.append({
            "id": t["id"], "kind": t["kind"], "args": t["args"],
            "kwargs": t["kwargs"], "expected": t.get("expected"),
            "cmp": t.get("cmp"), "exc": t.get("exc"), "checks": t.get("checks"),
            "stdout": t.get("stdout"), "stderr": t.get("stderr"),
        })
    if lang == "python":
        body = "[\n" + "".join("    " + py_literal(r) + ",\n" for r in records) + "]"
        out = PY_TEMPLATE
        out = out.replace("__META__", meta)
        out = out.replace("__MARKER__", repr(MARKER))
        out = out.replace("__ENTRYPOINT__", repr(name))
        out = out.replace("__CLASS_HINT__", repr(class_hint))
        out = out.replace("__DEFAULT_TOL__", repr(DEFAULT_TOL))
        out = out.replace("__TESTS__", body)
        return out
    data = json.dumps([json_safe_record(r) for r in records], allow_nan=False)
    out = JS_TEMPLATE
    out = out.replace("__HEADER__", "// @ts-nocheck" if lang == "typescript" else "")
    out = out.replace("__META__", meta)
    out = out.replace("__FILENAME__", "tester." + LANG_EXT[lang])
    out = out.replace("__SOLEXT__", "." + LANG_EXT[lang])
    out = out.replace("__MARKER__", json.dumps(MARKER))
    out = out.replace("__ENTRYPOINT__", json.dumps(name))
    out = out.replace("__CLASS_HINT__", json.dumps(class_hint))
    out = out.replace("__IS_TS__", "true" if lang == "typescript" else "false")
    out = out.replace("__DEFAULT_TOL__", repr(DEFAULT_TOL))
    out = out.replace("__TESTS__", json.dumps(data))
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

EP_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")


def cmd_generate(args) -> int:
    lang = args.lang
    if lang not in LANG_EXT:
        sys.stderr.write(f"error: unsupported --lang {lang!r}\n")
        return 2
    ep = args.entrypoint
    if not ep or not EP_RE.match(ep.strip()):
        sys.stderr.write("error: --entrypoint is required and must be a name\n")
        return 2
    ep = ep.strip()
    tests_dir = args.tests_dir
    if not os.path.isdir(tests_dir):
        sys.stderr.write(f"error: tests dir not found: {tests_dir}\n")
        return 2
    try:
        tests = discover(tests_dir, ep.split(".")[-1])
    except DiscoveryError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    try:
        content = render_tester(lang, tests, ep)
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f"error: {e}\n")
        return 1
    target = os.path.join(tests_dir, "tester." + LANG_EXT[lang])
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tester.", dir=tests_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        sys.stderr.write(f"error: cannot write tester: {e}\n")
        return 1
    return 0


def _read_meta(tester_path):
    try:
        with open(tester_path, "r", encoding="utf-8") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                i = line.find(META_PREFIX)
                if i >= 0:
                    return json.loads(line[i + len(META_PREFIX):])
    except Exception:
        pass
    return None


def _node_cmd(lang):
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        return None
    cmd = [node, "--no-warnings"]
    if lang == "typescript":
        try:
            v = subprocess.run([node, "--version"], capture_output=True, text=True,
                               timeout=10).stdout.strip().lstrip("v")
            major, minor = (int(x) for x in v.split(".")[:2])
            if (major, minor) < (23, 6):
                cmd.append("--experimental-strip-types")
        except Exception:
            pass
    return cmd


def _run_tester(base_cmd, tester, solution, n_tests, timeout, extra=()):
    """Run tester, restarting after crashes/timeouts. Returns dict id->bool."""
    results = {}
    start = 0
    while start < n_tests:
        cmd = base_cmd + [tester, solution, "--start", str(start)] + list(extra)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return results
        q: "queue.Queue" = queue.Queue()

        def reader(stream=proc.stdout):
            try:
                for raw in iter(stream.readline, b""):
                    q.put(raw)
            finally:
                q.put(None)

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        current = None
        progressed = False
        done = False
        load_error = False
        # loading gets a somewhat larger allowance
        limit = timeout + 5.0
        import time
        deadline = time.monotonic() + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = q.get(timeout=remaining)
            except queue.Empty:
                break
            if raw is None:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith(MARKER):
                continue
            try:
                ev = json.loads(line[len(MARKER):])
            except ValueError:
                continue
            e = ev.get("event")
            if e == "start":
                current = ev.get("index")
                progressed = True
                deadline = time.monotonic() + timeout
            elif e == "result":
                results[ev.get("id")] = bool(ev.get("ok"))
                idx = ev.get("index")
                if isinstance(idx, int):
                    start = max(start, idx + 1)
                current = None
                deadline = time.monotonic() + timeout
            elif e == "load_error":
                load_error = True
            elif e == "done":
                done = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if done or load_error or not progressed:
            break
        if current is not None and isinstance(current, int):
            # the in-progress test crashed or timed out: it is failed; move on
            start = max(start, current + 1)
    return results


def emit_result(status, passed, failed) -> int:
    sys.stdout.write(json.dumps({"status": status, "passed": passed, "failed": failed}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return {"pass": 0, "fail": 1}.get(status, 2)


def cmd_test(args) -> int:
    tests_dir = args.tests_dir
    lang = args.lang
    tol = args.tol
    if tol is None or not math.isfinite(tol) or tol < 0:
        return emit_result("error", [], [])
    meta = None
    tester = None
    if lang in LANG_EXT:
        tester = os.path.join(tests_dir, "tester." + LANG_EXT[lang])
        if os.path.isfile(tester):
            meta = _read_meta(tester)
    ep_name = None
    if meta and isinstance(meta.get("entrypoint"), str):
        ep_name = meta["entrypoint"].split(".")[-1]
    try:
        try:
            tests = discover(tests_dir, ep_name)
        except DiscoveryError:
            if ep_name is None:
                raise
            tests = discover(tests_dir, None)
    except DiscoveryError:
        return emit_result("error", [], [])
    ids = [t["id"] for t in tests]

    if lang not in LANG_EXT or tester is None or not os.path.isfile(tester):
        return emit_result("error", [], ids)
    solution = args.solution_path
    if not os.path.isfile(solution):
        return emit_result("error", [], ids)
    solution = os.path.abspath(solution)
    if lang == "python":
        base = [sys.executable]
    else:
        base = _node_cmd(lang)
        if base is None:
            return emit_result("error", [], ids)
    if not ids:
        return emit_result("pass", [], [])
    timeout = args.timeout if args.timeout and args.timeout > 0 else DEFAULT_TIMEOUT
    extra = ["--tol", repr(float(tol))]
    results = _run_tester(base, os.path.abspath(tester), solution, len(ids), timeout, extra)
    passed = [i for i in ids if results.get(i) is True]
    failed = [i for i in ids if results.get(i) is not True]
    return emit_result("pass" if not failed else "fail", passed, failed)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _Parser(prog="babel_code_goat.py")
    sub = parser.add_subparsers(dest="command")
    g = sub.add_parser("generate")
    g.add_argument("tests_dir")
    g.add_argument("--entrypoint")
    g.add_argument("--lang")
    t = sub.add_parser("test")
    t.add_argument("solution_path")
    t.add_argument("tests_dir")
    t.add_argument("--lang")
    t.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    t.add_argument("--tol", type=float, default=DEFAULT_TOL)
    is_test = bool(argv) and argv[0] == "test"
    try:
        args, _unknown = parser.parse_known_args(argv)
    except _UsageError as e:
        if is_test:
            return emit_result("error", [], [])
        sys.stderr.write(f"error: {e}\n")
        return 2
    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "test":
        return cmd_test(args)
    sys.stderr.write("usage: babel_code_goat.py {generate,test} ...\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
