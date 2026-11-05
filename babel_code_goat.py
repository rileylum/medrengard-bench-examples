#!/usr/bin/env python3
"""babel_code_goat: translate Python test files (any ``.py`` under <tests_dir>) into a tester for
python / javascript / typescript / cpp / rust and run it against a solution.

Commands:
    generate <tests_dir> --entrypoint <name> --lang <python|javascript|typescript|cpp|rust>
    test <solution_path> <tests_dir> --lang <python|javascript|typescript|cpp|rust> [--timeout SECONDS] [--tol FLOAT]
         [--list-tests] [--run <test_id>]... [--timeout-ms <int>] [--total-timeout-ms <int>]

    profile <tests_dir> <solution_path> --lang <...> [-n <trials>] [--warmup <k>] [--memory]
         [--list-tests] [--run <test_id>]... [--timeout-ms <int>] [--total-timeout-ms <int>] [--tol FLOAT]

``profile`` requires the generated tester, runs the selected tests ``n`` times (the first ``k``
are warmup runs and excluded from statistics) and prints status/passed/failed plus
``runtime_ns`` {mean, std} per trial (population std) and, with ``--memory``, ``memory_kb``
{mean, std} (peak RSS of the tester process). Timed-out trials are included in the statistics.

``test`` selection / timeouts: ``--list-tests`` only discovers (status "pass", passed = all ids);
``--run <id>`` (repeatable) restricts the run to the given ids; ``--timeout-ms`` bounds each test;
``--total-timeout-ms`` bounds the whole execution. Tests that time out or are never executed
are reported in ``failed``.

Async: tests may use ``async def`` / ``await`` / ``asyncio.run(...)`` (pytest asyncio markers are
accepted). Every target drives the entrypoint's result to completion: Python awaitables run
on a fresh event loop, JS/TS promises are awaited, C++ std::future / shared_future / coroutine
task types (``promise_type`` + ``get()``) are waited on, and Rust futures (``async fn``,
``impl Future``, ``Pin<Box<dyn Future>>``) are polled by a built-in block_on executor.

C++ / Rust: ``generate`` writes tester.cpp / tester.rs; ``test`` compiles it together with the
solution (a single .cpp / .rs file; g++/clang++ with C++17 or later, rustc 2021 edition) and
requires the tester to exist. Arguments are converted into the entrypoint's parameter types
(std::optional / Option for None, vectors, maps, sets, tuples, long double / f64 for Decimal,
...); results, post-call arguments, exceptions / panics and output are checked with the same
Python semantics as the interpreted targets.

Each test (assertion / raises block) must be traceable to exactly one entrypoint call.
The call may be wrapped in primitive operations (arithmetic, comparisons, indexing,
slicing, len/sorted/sum/min/max/..., str/list/dict/set methods, comprehensions) and
tolerance helpers (abs(...) < tol, math.isclose, pytest.approx). Tests that call the
entrypoint more than once, or depend on any other function call, are rejected.

Parameterization loops (for / while, nested, enumerate, range, break/continue) are
unrolled at discovery time. The loop statement is itself a test (``tests.py:<line>``)
that passes iff it iterates at least once (on every execution); assertions in the body
get ids ``tests.py:<line>:<k>`` where k counts executions of that assertion.
Test ids are ``<relative/path.py>:<line>`` (``#k`` suffix for several tests on one line).

Mutation-style tests: an entrypoint call used as a statement (or assignment) that is
immediately followed by asserts referencing the variables passed to it; those asserts
check the arguments' state after the call.
"""

from __future__ import annotations

import ast
import collections
import decimal
import io
import json
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize

LANGS = {"python": "tester.py", "javascript": "tester.js", "typescript": "tester.ts",
         "cpp": "tester.cpp", "rust": "tester.rs"}
META_MARKER = "BABEL_CODE_GOAT_META:"
DEFAULT_TIMEOUT = 10.0
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPECT_RE = re.compile(r"^#\s*expect_(stdout|stderr)\s*:\s*(.*?)\s*$")


class DiscoveryError(Exception):
    pass


class GenerateError(Exception):
    pass


PY_EVAL_SRC = r'''
# ---- primitive-expression evaluator (shared by the generator and the Python tester)
import collections as _ev_collections, math as _ev_math, operator as _ev_op

_EV_BIN = {
    "+": _ev_op.add, "-": _ev_op.sub, "*": _ev_op.mul, "/": _ev_op.truediv,
    "//": _ev_op.floordiv, "%": _ev_op.mod, "**": _ev_op.pow, "&": _ev_op.and_,
    "|": _ev_op.or_, "^": _ev_op.xor, "<<": _ev_op.lshift, ">>": _ev_op.rshift,
}
_EV_CMP = {
    "eq": _ev_op.eq, "ne": _ev_op.ne, "lt": _ev_op.lt, "le": _ev_op.le, "gt": _ev_op.gt,
    "ge": _ev_op.ge, "in": lambda a, b: a in b, "notin": lambda a, b: a not in b,
    "is": lambda a, b: a is b, "isnot": lambda a, b: a is not b,
}
_EV_TYPES = {
    "int": int, "float": float, "str": str, "bool": bool, "list": list, "tuple": tuple,
    "dict": dict, "set": set, "frozenset": frozenset, "bytes": bytes, "NoneType": type(None),
    "complex": complex,
}


def _ev_sorted(it, key=None, reverse=False):
    return sorted(it, key=key, reverse=reverse)


_EV_FUNCS = {
    "len": len, "sorted": _ev_sorted, "reversed": lambda x: list(reversed(x)), "list": list,
    "tuple": tuple, "set": set, "frozenset": frozenset, "dict": dict, "str": str, "int": int,
    "float": float, "bool": bool, "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "any": any, "all": all, "enumerate": lambda *a, **k: list(enumerate(*a, **k)),
    "zip": lambda *a: list(zip(*a)), "range": range, "map": lambda f, *its: list(map(f, *its)),
    "filter": lambda f, it: list(filter(f, it)), "repr": repr, "chr": chr, "ord": ord,
    "divmod": divmod, "pow": pow, "Counter": _ev_collections.Counter,
    "isclose": _ev_math.isclose, "math.isclose": _ev_math.isclose,
    "math.sqrt": _ev_math.sqrt, "math.floor": _ev_math.floor, "math.ceil": _ev_math.ceil,
    "math.fabs": _ev_math.fabs, "math.log": _ev_math.log, "math.log2": _ev_math.log2,
    "math.log10": _ev_math.log10, "math.exp": _ev_math.exp, "math.isnan": _ev_math.isnan,
    "math.isinf": _ev_math.isinf, "math.isfinite": _ev_math.isfinite, "math.trunc": _ev_math.trunc,
    "math.gcd": _ev_math.gcd, "math.fsum": _ev_math.fsum, "math.prod": _ev_math.prod,
    "math.isqrt": _ev_math.isqrt, "math.hypot": _ev_math.hypot,
}

_EV_METHODS = frozenset("""
lower upper strip lstrip rstrip split rsplit splitlines join startswith endswith replace count
find rfind index rindex isdigit isalpha isalnum isspace isupper islower isdecimal isnumeric
title capitalize casefold swapcase zfill center ljust rjust partition rpartition
keys values items get union intersection difference symmetric_difference issubset issuperset
isdisjoint most_common total is_integer bit_length
""".split())


def _ev_bind(target, value, env):
    if target[0] == "name":
        env[target[1]] = value
        return
    vals = list(value)
    if len(vals) != len(target[1]):
        raise ValueError("cannot unpack %d values into %d targets" % (len(vals), len(target[1])))
    for t, v in zip(target[1], vals):
        _ev_bind(t, v, env)


def _ev_comp(gens, i, env, out):
    if i == len(gens):
        out(env)
        return
    target, it, ifs = gens[i]
    for v in _ev(it, env["\0r"], env):
        e2 = dict(env)
        _ev_bind(target, v, e2)
        if all(_ev(c, e2["\0r"], e2) for c in ifs):
            _ev_comp(gens, i + 1, e2, out)


def _ev(n, r, env=None, args=None, kwargs=None):
    if env is None:
        env = {"\0r": r, "\0args": args, "\0kwargs": kwargs}
    t = n[0]
    if t == "r":
        return r
    if t == "arg":
        return env["\0args"][n[1]]
    if t == "kwarg":
        return env["\0kwargs"][n[1]]
    if t == "lit":
        return n[1]
    if t == "var":
        return env[n[1]]
    if t == "bin":
        return _EV_BIN[n[1]](_ev(n[2], r, env), _ev(n[3], r, env))
    if t == "un":
        v = _ev(n[2], r, env)
        return {"-": _ev_op.neg, "+": _ev_op.pos, "not": _ev_op.not_, "~": _ev_op.invert}[n[1]](v)
    if t == "cmp":
        left = _ev(n[2][0], r, env)
        for op, nxt in zip(n[1], n[2][1:]):
            right = _ev(nxt, r, env)
            if not _EV_CMP[op](left, right):
                return False
            left = right
        return True
    if t == "and":
        v = True
        for c in n[1]:
            v = _ev(c, r, env)
            if not v:
                return v
        return v
    if t == "or":
        v = False
        for c in n[1]:
            v = _ev(c, r, env)
            if v:
                return v
        return v
    if t == "ifexp":
        return _ev(n[2], r, env) if _ev(n[1], r, env) else _ev(n[3], r, env)
    if t == "sub":
        return _ev(n[1], r, env)[_ev(n[2], r, env)]
    if t == "slice":
        parts = [None if x is None else _ev(x, r, env) for x in n[2:5]]
        return _ev(n[1], r, env)[slice(*parts)]
    if t == "attr":
        return getattr(_ev(n[1], r, env), n[2])
    if t in ("list", "tuple", "set"):
        vals = [_ev(c, r, env) for c in n[1]]
        return {"list": list, "tuple": tuple, "set": set}[t](vals)
    if t == "dict":
        return {_ev(k, r, env): _ev(v, r, env) for k, v in n[1]}
    if t == "call":
        fn = _EV_FUNCS[n[1]]
        args = [_ev(c, r, env) for c in n[2]]
        kwargs = {k: _ev(v, r, env) for _, k, v in n[3]}
        return fn(*args, **kwargs)
    if t == "fn":
        return _EV_FUNCS[n[1]]
    if t == "meth":
        if n[1] not in _EV_METHODS:
            raise AttributeError("unsupported method %s" % n[1])
        obj = _ev(n[2], r, env)
        args = [_ev(c, r, env) for c in n[3]]
        kwargs = {k: _ev(v, r, env) for _, k, v in n[4]}
        return getattr(obj, n[1])(*args, **kwargs)
    if t == "isinst":
        return isinstance(_ev(n[1], r, env), tuple(_EV_TYPES[x] for x in n[2]))
    if t == "lambda":
        params, body = n[1], n[2]

        def _lam(*vals):
            if len(vals) != len(params):
                raise TypeError("lambda takes %d arguments" % len(params))
            e2 = dict(env)
            e2.update(zip(params, vals))
            return _ev(body, r, e2)
        return _lam
    if t == "comp":
        kind, elt, gens = n[1], n[2], n[3]
        env = dict(env)
        env["\0r"] = r
        out = []
        if kind == "dict":
            _ev_comp(gens, 0, env, lambda e: out.append((_ev(elt[0], r, e), _ev(elt[1], r, e))))
            return dict(out)
        _ev_comp(gens, 0, env, lambda e: out.append(_ev(elt, r, e)))
        if kind == "set":
            return set(out)
        return out
    raise ValueError("bad expression node %r" % (t,))
# ---- end evaluator
'''

_EVNS = {}
exec(compile(PY_EVAL_SRC, "<babel_code_goat-eval>", "exec"), _EVNS)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_MISSING = object()


class ConstFactory:
    """A ``defaultdict`` default factory of the form ``lambda: <literal>``."""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        import copy
        return copy.deepcopy(self.value)


FACTORY_NAMES = {
    "int": int, "float": float, "str": str, "list": list, "dict": dict, "set": set,
    "tuple": tuple, "frozenset": frozenset, "bool": bool, "bytes": bytes,
    "Counter": collections.Counter, "deque": collections.deque,
    "OrderedDict": collections.OrderedDict, "defaultdict": collections.defaultdict,
    "Decimal": decimal.Decimal,
}

# canonical names of constructor-like calls that may appear inside values
CONSTRUCTORS = {
    "set", "frozenset", "list", "tuple", "dict", "float", "int", "str", "bool",
    "Counter", "deque", "defaultdict", "OrderedDict", "Decimal",
}
# other helper names that are never the entrypoint
HELPERS = CONSTRUCTORS | {"abs", "isclose", "round", "len", "sorted", "repr", "search",
                          "match", "fullmatch", "raises", "isinstance", "type", "print",
                          "min", "max", "sum", "any", "all", "reversed", "enumerate", "zip",
                          "range", "map", "filter", "chr", "ord", "divmod", "pow", "approx"}

MATH_CONSTS = {"inf": math.inf, "nan": math.nan, "pi": math.pi, "e": math.e, "tau": math.tau}

MAX_LITERAL_SIZE = 10_000_000


def _check_size(v):
    if isinstance(v, (str, bytes, list, tuple)) and len(v) > MAX_LITERAL_SIZE:
        raise DiscoveryError("literal too large")
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > 100_000:
        raise DiscoveryError("literal too large")
    return v


class Context:
    """Name bindings (imports, constants, call results) seen while discovering."""

    def __init__(self):
        self.aliases = {}   # local name -> canonical dotted name
        self.consts = {}    # local name -> literal value
        self.calls = {}     # local name -> ast.Call bound by ``x = f(...)``

    def canonical(self, func):
        """Canonical short name of a call target, e.g. ``collections.Counter`` -> ``Counter``."""
        if isinstance(func, ast.Name):
            full = self.aliases.get(func.id, func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = self.aliases.get(func.value.id, func.value.id)
            full = f"{base}.{func.attr}"
        else:
            return None
        return full

    def short(self, func):
        full = self.canonical(func)
        if full is None:
            return None
        return full.split(".")[-1]


def _module_of(ctx, func):
    full = ctx.canonical(func) if ctx else None
    if full and "." in full:
        return full.rsplit(".", 1)[0]
    return None


KNOWN_MODULES = {"collections", "decimal", "math", "re", "builtins", "pytest", "typing", "asyncio"}


def is_value_call(node, ctx, entrypoint=None):
    """True if ``node`` is a call of a known constructor (not the entrypoint)."""
    if not isinstance(node, ast.Call):
        return False
    name = callee_name(node.func)
    if entrypoint is not None and name == entrypoint:
        return False
    short = ctx.short(node.func) if ctx else (name.split(".")[-1] if name else None)
    if short not in CONSTRUCTORS:
        return False
    mod = _module_of(ctx, node.func)
    return mod is None or mod in KNOWN_MODULES


def _to_decimal(v, lineno):
    if isinstance(v, bool):
        raise DiscoveryError(f"line {lineno}: bad Decimal argument")
    try:
        return decimal.Decimal(v)
    except Exception:
        raise DiscoveryError(f"line {lineno}: bad Decimal argument {v!r}")


def _isnumber(v):
    return isinstance(v, (int, float, decimal.Decimal)) and not isinstance(v, bool)


def _hashable(v, lineno):
    try:
        hash(v)
    except TypeError:
        raise DiscoveryError(f"line {lineno}: unhashable value {v!r}")
    return v


def literal_value(node, ctx=None):
    """Return the Python value of an allowed literal node or raise DiscoveryError."""
    lineno = getattr(node, "lineno", "?")
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None or isinstance(v, (bool, int, float, str, bytes)):
            return v
        raise DiscoveryError(f"line {lineno}: unsupported constant {v!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = literal_value(node.operand, ctx)
        if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
            raise DiscoveryError(f"line {lineno}: unary operator on non-number")
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp):
        a = literal_value(node.left, ctx)
        b = literal_value(node.right, ctx)
        ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
               ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
               ast.Mod: operator.mod, ast.Pow: operator.pow, ast.BitOr: operator.or_,
               ast.BitAnd: operator.and_, ast.BitXor: operator.xor,
               ast.LShift: operator.lshift, ast.RShift: operator.rshift}
        fn = ops.get(type(node.op))
        if fn is None:
            raise DiscoveryError(f"line {lineno}: unsupported operator")
        if isinstance(node.op, ast.Pow) and isinstance(b, int) and abs(b) > 10000:
            raise DiscoveryError(f"line {lineno}: exponent too large")
        if isinstance(node.op, ast.LShift) and isinstance(b, int) and b > 100000:
            raise DiscoveryError(f"line {lineno}: shift too large")
        if isinstance(node.op, ast.Mult):
            for x, y in ((a, b), (b, a)):
                if isinstance(x, (str, bytes, list, tuple)) and isinstance(y, int) and len(x) * y > MAX_LITERAL_SIZE:
                    raise DiscoveryError(f"line {lineno}: literal too large")
        try:
            return _check_size(fn(a, b))
        except DiscoveryError:
            raise
        except Exception as e:
            raise DiscoveryError(f"line {lineno}: cannot evaluate expression: {e}")
    if isinstance(node, ast.List):
        return [literal_value(e, ctx) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(literal_value(e, ctx) for e in node.elts)
    if isinstance(node, ast.Set):
        return {_hashable(literal_value(e, ctx), lineno) for e in node.elts}
    if isinstance(node, ast.Dict):
        out = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                inner = literal_value(v, ctx)
                if not isinstance(inner, dict):
                    raise DiscoveryError(f"line {lineno}: bad dict unpacking")
                out.update(inner)
                continue
            kv = _hashable(literal_value(k, ctx), lineno)
            out[kv] = literal_value(v, ctx)
        return out
    if isinstance(node, ast.Name):
        if ctx is not None and node.id in ctx.consts:
            return ctx.consts[node.id]
        full = ctx.aliases.get(node.id) if ctx else None
        if full and full.startswith("math.") and full[5:] in MATH_CONSTS:
            return MATH_CONSTS[full[5:]]
        raise DiscoveryError(f"line {lineno}: unknown name {node.id!r}")
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        base = ctx.aliases.get(node.value.id, node.value.id) if ctx else node.value.id
        if base == "math" and node.attr in MATH_CONSTS:
            return MATH_CONSTS[node.attr]
        raise DiscoveryError(f"line {lineno}: unsupported value {base}.{node.attr}")
    if isinstance(node, ast.Call) and is_value_call(node, ctx):
        return construct_value(node, ctx)
    raise DiscoveryError(
        f"line {lineno}: unsupported value {ast.dump(node)[:80]}"
    )


def _factory_value(node, ctx):
    lineno = getattr(node, "lineno", "?")
    if isinstance(node, ast.Constant) and node.value is None:
        return None
    if isinstance(node, ast.Lambda):
        a = node.args
        if a.args or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg:
            raise DiscoveryError(f"line {lineno}: defaultdict factory lambda must take no arguments")
        return ConstFactory(literal_value(node.body, ctx))
    if isinstance(node, (ast.Name, ast.Attribute)):
        short = ctx.short(node) if ctx else getattr(node, "id", None)
        if short in FACTORY_NAMES:
            return FACTORY_NAMES[short]
    raise DiscoveryError(f"line {lineno}: unsupported defaultdict factory")


def construct_value(node, ctx):
    lineno = node.lineno
    short = ctx.short(node.func) if ctx else callee_name(node.func).split(".")[-1]
    for a in node.args:
        if isinstance(a, ast.Starred):
            raise DiscoveryError(f"line {lineno}: starred arguments not allowed")
    if short == "defaultdict":
        if not node.args:
            factory, rest = None, []
        else:
            factory, rest = _factory_value(node.args[0], ctx), node.args[1:]
        args = [literal_value(a, ctx) for a in rest]
    else:
        args = [literal_value(a, ctx) for a in node.args]
    kwargs = {}
    for kw in node.keywords:
        if kw.arg is None:
            raise DiscoveryError(f"line {lineno}: ** arguments not allowed")
        kwargs[kw.arg] = literal_value(kw.value, ctx)
    try:
        if short == "Decimal":
            if kwargs or len(args) > 1:
                raise DiscoveryError(f"line {lineno}: bad Decimal call")
            return _to_decimal(args[0] if args else "0", lineno)
        if short == "float":
            if kwargs or len(args) > 1 or (args and isinstance(args[0], (list, tuple, dict, set))):
                raise DiscoveryError(f"line {lineno}: bad float call")
            return float(*args)
        if short == "int":
            if kwargs or len(args) > 2:
                raise DiscoveryError(f"line {lineno}: bad int call")
            return _check_size(int(*args))
        if short == "str":
            if kwargs or len(args) > 1:
                raise DiscoveryError(f"line {lineno}: bad str call")
            return str(*args)
        if short == "bool":
            return bool(*args)
        if short in ("list", "tuple", "set", "frozenset"):
            if kwargs or len(args) > 1:
                raise DiscoveryError(f"line {lineno}: bad {short} call")
            ctor = {"list": list, "tuple": tuple, "set": set, "frozenset": frozenset}[short]
            return ctor(*args)
        if short == "dict":
            return dict(*args, **kwargs)
        if short == "OrderedDict":
            return collections.OrderedDict(*args, **kwargs)
        if short == "Counter":
            return collections.Counter(*args, **kwargs)
        if short == "deque":
            return collections.deque(*args, **kwargs)
        if short == "defaultdict":
            return collections.defaultdict(factory, *args, **kwargs)
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError(f"line {lineno}: cannot evaluate {short}(...): {e}")
    raise DiscoveryError(f"line {lineno}: unsupported call {short}")


def callee_name(func):
    """Return the entrypoint spelling of a call target, or None."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return f"{base.id}.{func.attr}"
        if (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Name)
            and not base.args
            and not base.keywords
        ):
            return f"{base.func.id}.{func.attr}"
    return None


def callee_matches(name, entrypoint):
    if entrypoint is None:
        return True
    if name == entrypoint:
        return True
    if "." in entrypoint:
        return name == entrypoint.split(".")[-1]
    if "." in name:
        return name.split(".")[-1] == entrypoint
    return False


RE_FLAG_NAMES = {
    "I": re.I, "IGNORECASE": re.I, "M": re.M, "MULTILINE": re.M, "S": re.S, "DOTALL": re.S,
    "X": re.X, "VERBOSE": re.X, "A": re.A, "ASCII": re.A, "U": re.U, "UNICODE": re.U,
}
CMP_OPS = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}
FLIP = {"lt": "gt", "le": "ge", "gt": "lt", "ge": "le", "eq": "eq", "ne": "ne"}


BIN_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//",
           ast.Mod: "%", ast.Pow: "**", ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^",
           ast.LShift: "<<", ast.RShift: ">>"}
ALL_CMP_OPS = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt",
               ast.GtE: "ge", ast.In: "in", ast.NotIn: "notin", ast.Is: "is", ast.IsNot: "isnot"}


def _ir_closed(ir, bound=frozenset()):
    """True if IR contains no reference to the entrypoint result and no free variables."""
    if not isinstance(ir, list) or not ir:
        return True
    tag = ir[0]
    if tag == "lit":
        return True
    if tag in ("r", "arg", "kwarg"):
        return False
    if tag == "var":
        return ir[1] in bound
    if tag == "lambda":
        return _ir_closed(ir[2], bound | set(ir[1]))
    if tag == "comp":
        inner = set(bound)
        for tgt, it, ifs in ir[3]:
            if not _ir_closed(it, frozenset(inner)):
                return False
            inner |= _target_names(tgt)
            if not all(_ir_closed(c, frozenset(inner)) for c in ifs):
                return False
        return _ir_closed(ir[2], frozenset(inner))
    return all(_ir_closed(c, bound) for c in ir if isinstance(c, list))


def _target_names(tgt):
    if tgt[0] == "name":
        return {tgt[1]}
    out = set()
    for t in tgt[1]:
        out |= _target_names(t)
    return out


def _target_names_ast(t):
    if isinstance(t, ast.Name):
        return {t.id}
    out = set()
    if isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            out |= _target_names_ast(e)
    return out


def _lit_of(ir):
    return ir[1] if isinstance(ir, list) and ir and ir[0] == "lit" else _MISSING


def _fold_safe(ir):
    """Reject discovery-time evaluation of obviously huge computations."""
    if not isinstance(ir, list) or not ir or ir[0] == "lit":
        return True
    tag = ir[0]
    if tag == "bin":
        a, b = _lit_of(ir[2]), _lit_of(ir[3])
        if ir[1] == "**" and isinstance(b, int) and abs(b) > 10000:
            return False
        if ir[1] == "<<" and isinstance(b, int) and b > 100000:
            return False
        if ir[1] == "*":
            for x, y in ((a, b), (b, a)):
                if isinstance(x, (str, bytes, list, tuple)) and isinstance(y, int) \
                        and len(x) * y > MAX_LITERAL_SIZE:
                    return False
    if tag == "call" and ir[1] == "range":
        vals = [_lit_of(c) for c in ir[2]]
        if all(isinstance(v, int) for v in vals):
            try:
                if len(range(*vals)) > MAX_LITERAL_SIZE:
                    return False
            except Exception:
                pass
    if tag == "meth" and ir[1] in ("zfill", "center", "ljust", "rjust"):
        for c in ir[3]:
            v = _lit_of(c)
            if isinstance(v, int) and v > MAX_LITERAL_SIZE:
                return False
    return all(_fold_safe(c) for c in ir if isinstance(c, list))


def _lit_ok(v, depth=0):
    """True if ``v`` can be written as a literal in every target language."""
    if depth > 200:
        return False
    if v is None or isinstance(v, (bool, int, float, str, bytes, decimal.Decimal)):
        return True
    if isinstance(v, collections.defaultdict):
        return False
    if isinstance(v, (list, tuple, collections.deque, set, frozenset)):
        return all(_lit_ok(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return all(_lit_ok(k, depth + 1) and _lit_ok(x, depth + 1) for k, x in v.items())
    return False


def py_regex_to_js(pattern, flags):
    """Translate a Python regex + flags into (js_source, js_flags)."""
    jsflags = ""
    m = re.match(r"^\(\?([aiLmsux]+)\)", pattern)
    if m:
        for ch in m.group(1):
            flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}.get(ch, 0)
        pattern = pattern[m.end():]
    if flags & re.X:
        # strip unescaped whitespace and comments outside classes
        out, i, in_cls = [], 0, False
        while i < len(pattern):
            c = pattern[i]
            if c == "\\" and i + 1 < len(pattern):
                out.append(pattern[i:i + 2]); i += 2; continue
            if in_cls:
                if c == "]":
                    in_cls = False
                out.append(c)
            elif c == "[":
                in_cls = True; out.append(c)
            elif c.isspace():
                pass
            elif c == "#":
                while i < len(pattern) and pattern[i] != "\n":
                    i += 1
                continue
            else:
                out.append(c)
            i += 1
        pattern = "".join(out)
    pattern = re.sub(r"\(\?P<([A-Za-z_][A-Za-z0-9_]*)>", r"(?<\1>", pattern)
    pattern = re.sub(r"\(\?P=([A-Za-z_][A-Za-z0-9_]*)\)", r"\\k<\1>", pattern)
    pattern = pattern.replace("\\A", "^").replace("\\Z", "$(?![\\s\\S])")
    if flags & re.I:
        jsflags += "i"
    if flags & re.M:
        jsflags += "m"
    if flags & re.S:
        jsflags += "s"
    return pattern, jsflags


MAX_LOOP_ITERATIONS = 100_000


class _LoopBreak(Exception):
    pass


class _LoopContinue(Exception):
    pass


class Discoverer:
    def __init__(self, source, entrypoint):
        self.source = source
        self.entrypoint = entrypoint
        self.tests = []  # dicts
        self.callees = []
        self.local_funcs = set()
        self.ctx = Context()
        self._calls = []
        self._loop_depth = 0
        self._exec_counts = {}   # (line, col) -> executions of a test statement inside loops
        self._loops = {}         # (line, col) -> loop-as-test record
        self.tainted = {}        # name -> line of the mutation call that consumed it

    # -- helpers
    def lit(self, node):
        """Value of a node that must be a literal (primitive operations are folded)."""
        self._check_tainted(node)
        try:
            return literal_value(node, self.ctx)
        except DiscoveryError as err:
            first = err
        if self._mentions(node, frozenset()):
            raise DiscoveryError(
                f"line {getattr(node, 'lineno', '?')}: the entrypoint result cannot be used here "
                "(entrypoint arguments must not depend on another entrypoint call; each test must "
                "be traceable to exactly one entrypoint call)")
        saved, self._calls = self._calls, []
        try:
            ir = self.cx(node, frozenset())
        except DiscoveryError:
            raise first
        finally:
            self._calls = saved
        if ir[0] == "lit":
            return ir[1]
        raise first

    def value(self, node):
        """Discovery-time value of an expression that must not involve the entrypoint.
        Unlike ``lit`` the result may be any Python value (e.g. a ``range``)."""
        self._check_tainted(node)
        try:
            return literal_value(node, self.ctx)
        except DiscoveryError as err:
            first = err
        lineno = getattr(node, "lineno", "?")
        if self._mentions(node, frozenset()):
            raise DiscoveryError(f"line {lineno}: expression depends on the entrypoint")
        saved, self._calls = self._calls, []
        try:
            ir = self.cx(node, frozenset())
            calls = self._calls
        except DiscoveryError:
            raise first
        finally:
            self._calls = saved
        if calls:
            raise DiscoveryError(f"line {lineno}: expression depends on the entrypoint")
        if ir[0] == "lit":
            return ir[1]
        if not _ir_closed(ir) or not _fold_safe(ir):
            raise first
        try:
            return _EVNS["_ev"](ir, None)
        except Exception as e:
            raise DiscoveryError(f"line {lineno}: cannot evaluate expression: {e}")

    def is_entry_call(self, node):
        if not isinstance(node, ast.Call):
            return False
        name = callee_name(node.func)
        if name is None:
            return False
        if self.entrypoint is not None and name == self.entrypoint:
            return True
        if is_value_call(node, self.ctx, self.entrypoint):
            return False
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and (f.value.id in self.ctx.calls or f.value.id in self.ctx.consts):
            return False  # a method call on a bound value, not the entrypoint
        mod = _module_of(self.ctx, node.func)
        if mod in KNOWN_MODULES:
            return False
        short = self.ctx.short(node.func)
        if short in HELPERS and short != (self.entrypoint or "").split(".")[-1]:
            return False
        if name in self.local_funcs and name != self.entrypoint:
            return False
        return callee_matches(name, self.entrypoint)

    def parse_call(self, node):
        if not isinstance(node, ast.Call):
            raise DiscoveryError(f"line {getattr(node, 'lineno', '?')}: expected entrypoint call")
        name = callee_name(node.func)
        if name is None:
            raise DiscoveryError(f"line {node.lineno}: unsupported call target")
        if not callee_matches(name, self.entrypoint):
            raise DiscoveryError(
                f"line {node.lineno}: call to {name!r} does not match entrypoint {self.entrypoint!r}"
            )
        args = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                v = self.value(a.value)
                try:
                    vals = list(v)
                except TypeError:
                    raise DiscoveryError(f"line {node.lineno}: starred argument is not iterable")
                for x in vals:
                    if not _lit_ok(x):
                        raise DiscoveryError(f"line {node.lineno}: unsupported starred argument")
                args.extend(vals)
                continue
            args.append(self.lit(a))
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:
                v = self.value(kw.value)
                if not isinstance(v, dict) or not all(isinstance(k, str) for k in v):
                    raise DiscoveryError(f"line {node.lineno}: ** argument must be a dict with str keys")
                for k, x in v.items():
                    if not _lit_ok(x):
                        raise DiscoveryError(f"line {node.lineno}: unsupported ** argument")
                    if k in kwargs:
                        raise DiscoveryError(f"line {node.lineno}: duplicate keyword argument {k!r}")
                    kwargs[k] = x
                continue
            if kw.arg in kwargs:
                raise DiscoveryError(f"line {node.lineno}: duplicate keyword argument {kw.arg!r}")
            kwargs[kw.arg] = self.lit(kw.value)
        self.callees.append(name)
        return {"name": name, "args": args, "kwargs": kwargs, "line": node.lineno}

    # -- primitive-expression compiler ------------------------------------
    def _reg(self, info):
        if not any(info is c for c in self._calls):
            self._calls.append(info)

    def compile_test(self, node):
        """Compile ``node`` into expression IR; return (ir, distinct entrypoint calls)."""
        saved, self._calls = self._calls, []
        try:
            ir = self.cx(node, frozenset())
            return ir, self._calls
        finally:
            self._calls = saved

    def single_call(self, node, lineno, what="assertion"):
        ir, calls = self.compile_test(node)
        if not calls:
            raise DiscoveryError(f"line {lineno}: {what} must call the entrypoint")
        if len(calls) > 1:
            raise DiscoveryError(
                f"line {lineno}: {what} calls the entrypoint {len(calls)} times; "
                "each test must be traceable to exactly one entrypoint call")
        return ir, calls[0]

    def _mentions(self, node, scope):
        """True if ``node`` refers to the entrypoint result or to comprehension variables."""
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and (n.id in scope or n.id in self.ctx.calls):
                return True
            if isinstance(n, ast.Call) and self.is_entry_call(n):
                return True
        return False

    def _fold(self, ir, lineno):
        """Evaluate a closed IR node at discovery time when it yields a literal."""
        if not _ir_closed(ir) or not _fold_safe(ir):
            return ir
        try:
            v = _EVNS["_ev"](ir, None)
        except Exception:
            return ir
        if not _lit_ok(v):
            return ir
        return ["lit", _check_size(v)]

    def _func_key(self, func, lineno):
        """Primitive function name for a call target, or None."""
        full = self.ctx.canonical(func)
        if full is None:
            return None
        funcs = _EVNS["_EV_FUNCS"]
        if "." in full:
            mod, short = full.rsplit(".", 1)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id not in self.ctx.aliases and mod not in KNOWN_MODULES:
                return None  # a method call on a value
            if mod == "builtins" and short in funcs and "." not in short:
                return short
            if mod == "math" and f"math.{short}" in funcs:
                return f"math.{short}"
            if mod == "collections" and short == "Counter":
                return "Counter"
            raise DiscoveryError(f"line {lineno}: unsupported function {full}()")
        if full in self.ctx.consts or full in self.ctx.calls:
            raise DiscoveryError(f"line {lineno}: {full!r} is not callable")
        if full in self.local_funcs:
            raise DiscoveryError(
                f"line {lineno}: tests must not depend on calls to other functions ({full}())")
        if full in funcs and "." not in full:
            return full
        raise DiscoveryError(
            f"line {lineno}: tests must not depend on calls to other functions ({full}())")

    def _type_names(self, node, lineno):
        if isinstance(node, ast.Tuple):
            out = []
            for e in node.elts:
                out.extend(self._type_names(e, lineno))
            return out
        if isinstance(node, ast.Name) and node.id in _EVNS["_EV_TYPES"] \
                and node.id not in self.ctx.consts and node.id not in self.ctx.calls:
            return [node.id]
        raise DiscoveryError(f"line {lineno}: unsupported isinstance type")

    def _target(self, node, lineno):
        if isinstance(node, ast.Name):
            return ["name", node.id], {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            parts, names = [], set()
            for e in node.elts:
                p, ns = self._target(e, lineno)
                parts.append(p)
                names |= ns
            return ["tuple", parts], names
        raise DiscoveryError(f"line {lineno}: unsupported comprehension target")

    def _args(self, node, scope):
        lineno = node.lineno
        args = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                raise DiscoveryError(f"line {lineno}: starred arguments not allowed")
            args.append(self.cx(a, scope))
        kws = []
        for kw in node.keywords:
            if kw.arg is None:
                raise DiscoveryError(f"line {lineno}: ** arguments not allowed")
            kws.append(["kw", kw.arg, self.cx(kw.value, scope)])
        return args, kws

    def cx(self, node, scope):
        lineno = getattr(node, "lineno", "?")
        if isinstance(node, ast.Name):
            if node.id in scope:
                return ["var", node.id]
            if node.id in self.tainted and node.id not in self.ctx.calls:
                raise self._tainted_error(node.id, lineno)
            if node.id in self.ctx.calls:
                ir, info = self.ctx.calls[node.id]
                self._reg(info)
                return ir
            try:
                return ["lit", literal_value(node, self.ctx)]
            except DiscoveryError:
                if node.id in _EVNS["_EV_FUNCS"] and "." not in node.id \
                        and node.id not in self.local_funcs:
                    return ["fn", node.id]
                full = self.ctx.aliases.get(node.id, "")
                if full.startswith("math.") and full in _EVNS["_EV_FUNCS"]:
                    return ["fn", full]
                raise
        if isinstance(node, ast.Call) and self.is_entry_call(node):
            self._reg(self.parse_call(node))
            return ["r"]
        if not self._mentions(node, scope):
            try:
                return ["lit", literal_value(node, self.ctx)]
            except DiscoveryError:
                pass
            return self._fold(self._cx(node, scope, lineno), lineno)
        return self._cx(node, scope, lineno)

    def _cx(self, node, scope, lineno):
        bad = lambda what: DiscoveryError(f"line {lineno}: unsupported {what}")  # noqa: E731
        if isinstance(node, ast.Constant):
            return ["lit", literal_value(node, self.ctx)]
        if isinstance(node, ast.BinOp):
            op = BIN_OPS.get(type(node.op))
            if op is None:
                raise bad("operator")
            return ["bin", op, self.cx(node.left, scope), self.cx(node.right, scope)]
        if isinstance(node, ast.UnaryOp):
            op = {ast.USub: "-", ast.UAdd: "+", ast.Not: "not", ast.Invert: "~"}[type(node.op)]
            return ["un", op, self.cx(node.operand, scope)]
        if isinstance(node, ast.BoolOp):
            return ["and" if isinstance(node.op, ast.And) else "or",
                    [self.cx(v, scope) for v in node.values]]
        if isinstance(node, ast.Compare):
            ops = []
            for op in node.ops:
                name = ALL_CMP_OPS.get(type(op))
                if name is None:
                    raise bad("comparison")
                ops.append(name)
            return ["cmp", ops, [self.cx(x, scope) for x in [node.left] + node.comparators]]
        if isinstance(node, ast.IfExp):
            return ["ifexp", self.cx(node.test, scope), self.cx(node.body, scope),
                    self.cx(node.orelse, scope)]
        if isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Slice):
                return ["slice", self.cx(node.value, scope)] + [
                    None if x is None else self.cx(x, scope) for x in (sl.lower, sl.upper, sl.step)]
            return ["sub", self.cx(node.value, scope), self.cx(sl, scope)]
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in self.ctx.aliases:
                full = self.ctx.canonical(node)
                if full in _EVNS["_EV_FUNCS"]:
                    return ["fn", full]
                return ["lit", literal_value(node, self.ctx)]
            if node.attr.startswith("_"):
                raise bad(f"attribute {node.attr}")
            return ["attr", self.cx(node.value, scope), node.attr]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for e in node.elts:
                if isinstance(e, ast.Starred):
                    raise bad("starred element")
            kind = {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}[type(node)]
            return [kind, [self.cx(e, scope) for e in node.elts]]
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                raise bad("dict unpacking")
            return ["dict", [[self.cx(k, scope), self.cx(v, scope)]
                             for k, v in zip(node.keys, node.values)]]
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            gens = []
            inner = set(scope)
            for g in node.generators:
                if g.is_async:
                    raise bad("async comprehension")
                it = self.cx(g.iter, frozenset(inner))
                tgt, names = self._target(g.target, lineno)
                inner |= names
                ifs = [self.cx(c, frozenset(inner)) for c in g.ifs]
                gens.append([tgt, it, ifs])
            inner = frozenset(inner)
            if isinstance(node, ast.DictComp):
                return ["comp", "dict", [self.cx(node.key, inner), self.cx(node.value, inner)], gens]
            kind = {ast.ListComp: "list", ast.SetComp: "set", ast.GeneratorExp: "list"}[type(node)]
            return ["comp", kind, self.cx(node.elt, inner), gens]
        if isinstance(node, ast.Lambda):
            a = node.args
            if a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg or a.defaults:
                raise bad("lambda signature")
            params = [x.arg for x in a.args]
            return ["lambda", params, self.cx(node.body, frozenset(scope) | set(params))]
        if isinstance(node, ast.Call):
            func = node.func
            short = self.ctx.short(func)
            if short == "isinstance" and isinstance(func, ast.Name):
                if len(node.args) != 2 or node.keywords:
                    raise bad("isinstance call")
                return ["isinst", self.cx(node.args[0], scope), self._type_names(node.args[1], lineno)]
            if short == "approx":
                raise DiscoveryError(f"line {lineno}: approx() is only supported as one side of == / !=")
            key = self._func_key(func, lineno)
            if key is None and isinstance(func, ast.Attribute):
                if func.attr not in _EVNS["_EV_METHODS"]:
                    raise bad(f"method .{func.attr}()")
                args, kws = self._args(node, scope)
                return ["meth", func.attr, self.cx(func.value, scope), args, kws]
            if key is None:
                raise bad("call")
            args, kws = self._args(node, scope)
            return ["call", key, args, kws]
        raise bad(f"expression {type(node).__name__}")

    # -- statements
    def add_test(self, node, kind, info, expected=_MISSING, extra=None, xf=None):
        it = None
        if self._loop_depth:
            key = (node.lineno, node.col_offset)
            it = self._exec_counts.get(key, 0)
            self._exec_counts[key] = it + 1
        t = {"line": node.lineno, "col": node.col_offset, "kind": kind, "args": info["args"],
             "kwargs": info["kwargs"], "extra": extra or {}, "xf": None if xf == ["r"] else xf,
             "iter": it}
        if expected is not _MISSING:
            t["expected"] = expected
        self.tests.append(t)

    def _num(self, v, lineno):
        if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
            raise DiscoveryError(f"line {lineno}: tolerance must be a number")
        return float(v)

    def sides(self, a, b, lineno):
        """Compile two operands. If exactly one depends on the (single) entrypoint call
        and the other is a literal, return (xf, info, literal, call_is_left); else None."""
        air, acalls = self.compile_test(a)
        bir, bcalls = self.compile_test(b)
        calls = list(acalls)
        for c in bcalls:
            if not any(c is x for x in calls):
                calls.append(c)
        if len(calls) > 1:
            raise DiscoveryError(
                f"line {lineno}: assertion calls the entrypoint {len(calls)} times; "
                "each test must be traceable to exactly one entrypoint call")
        if acalls and not bcalls and bir[0] == "lit":
            return air, acalls[0], bir[1], True
        if bcalls and not acalls and air[0] == "lit":
            return bir, bcalls[0], air[1], False
        return None

    def try_abs_close(self, node, test):
        """``abs(f(x) - y) < tol`` (and variants). Returns True if handled."""
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
            return False
        op = CMP_OPS.get(type(test.ops[0]))
        left, right = test.left, test.comparators[0]

        def is_abs(n):
            return (isinstance(n, ast.Call) and self.ctx.short(n.func) == "abs"
                    and not self.is_entry_call(n)
                    and len(n.args) == 1 and not n.keywords
                    and isinstance(n.args[0], ast.BinOp) and isinstance(n.args[0].op, ast.Sub))
        if is_abs(left) and op in ("lt", "le"):
            absn, toln, strict = left, right, op == "lt"
        elif is_abs(right) and op in ("gt", "ge"):
            absn, toln, strict = right, left, op == "gt"
        else:
            return False
        sub = absn.args[0]
        split = self.sides(sub.left, sub.right, node.lineno)
        if split is None:
            return False
        try:
            tol = self._num(self.lit(toln), node.lineno)
        except DiscoveryError:
            return False
        xf, info, expected, _ = split
        if not _isnumber(expected):
            return False
        self.add_test(node, "close", info, expected,
                      {"abs_tol": tol, "rel_tol": 0.0, "strict": strict}, xf)
        return True

    def try_isclose(self, node, test, negate=False):
        if not (isinstance(test, ast.Call) and self.ctx.short(test.func) == "isclose"
                and not self.is_entry_call(test)):
            return False
        if len(test.args) != 2:
            raise DiscoveryError(f"line {node.lineno}: isclose needs two arguments")
        split = self.sides(test.args[0], test.args[1], node.lineno)
        if split is None:
            return False
        xf, info, expected, _ = split
        if not _isnumber(expected):
            return False
        extra = {"abs_tol": 0.0, "rel_tol": 1e-09, "strict": False}
        for kw in test.keywords:
            if kw.arg not in ("abs_tol", "rel_tol"):
                raise DiscoveryError(f"line {node.lineno}: unsupported isclose argument {kw.arg}")
            extra[kw.arg] = self._num(self.lit(kw.value), node.lineno)
        extra["isclose"] = True
        self.add_test(node, "notclose" if negate else "close", info, expected, extra, xf)
        return True

    def is_approx(self, n):
        return (isinstance(n, ast.Call) and self.ctx.short(n.func) == "approx"
                and not self.is_entry_call(n))

    def try_approx(self, node, test):
        """``f(x) == pytest.approx(y, rel=..., abs=...)`` (either side)."""
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], (ast.Eq, ast.NotEq))):
            return False
        left, right = test.left, test.comparators[0]
        if self.is_approx(right):
            apx, other = right, left
        elif self.is_approx(left):
            apx, other = left, right
        else:
            return False
        if len(apx.args) != 1:
            raise DiscoveryError(f"line {node.lineno}: approx needs exactly one positional argument")
        expected = self.lit(apx.args[0])
        rel = abs_ = None
        for kw in apx.keywords:
            if kw.arg == "rel":
                rel = self._num(self.lit(kw.value), node.lineno)
            elif kw.arg == "abs":
                abs_ = self._num(self.lit(kw.value), node.lineno)
            elif kw.arg == "nan_ok":
                continue
            else:
                raise DiscoveryError(f"line {node.lineno}: unsupported approx argument {kw.arg}")
        if rel is None:
            rel = 0.0 if abs_ is not None else 1e-06
        if abs_ is None:
            abs_ = 1e-12
        xf, info = self.single_call(other, node.lineno)
        eq = isinstance(test.ops[0], ast.Eq)
        if _isnumber(expected):
            self.add_test(node, "close" if eq else "notclose", info, expected,
                          {"abs_tol": abs_, "rel_tol": rel, "strict": False}, xf)
        else:
            self.add_test(node, "eq" if eq else "ne", info, expected,
                          {"approx": [abs_, rel]}, xf)
        return True

    def handle_assert(self, node):
        if node.msg is not None:
            try:
                self.lit(node.msg)
            except DiscoveryError:
                pass  # messages are never evaluated by the harness
        test = node.test
        if self.try_approx(node, test):
            return
        if self.try_abs_close(node, test) or self.try_isclose(node, test):
            return
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
                and self.try_isclose(node, test.operand, negate=True):
            return
        # every other assertion: exactly one entrypoint call, wrapped in primitive operations
        ir, info = self.single_call(test, node.lineno)
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            op = test.ops[0]
            split = self.sides(test.left, test.comparators[0], node.lineno)
            if split is not None:
                xf, _, expected, call_left = split
                kind = None
                if isinstance(op, (ast.In, ast.NotIn)):
                    if call_left and isinstance(expected, (list, tuple, set, frozenset, dict, str,
                                                           collections.deque)):
                        kind = "in" if isinstance(op, ast.In) else "notin"
                elif isinstance(op, (ast.Is, ast.IsNot)):
                    if expected is None or isinstance(expected, bool):
                        kind = "is" if isinstance(op, ast.Is) else "isnot"
                else:
                    kind = CMP_OPS.get(type(op))
                    if kind is not None and not call_left:
                        kind = FLIP[kind]
                if kind is not None:
                    self.add_test(node, kind, info, expected, xf=xf)
                    return
        if ir[0] == "un" and ir[1] == "not":
            self.add_test(node, "falsy", info, xf=ir[2])
            return
        self.add_test(node, "truthy", info, xf=ir)

    # -- exceptions
    def exc_names(self, typ, lineno):
        if typ is None:
            return None
        if isinstance(typ, ast.Tuple):
            names = []
            for e in typ.elts:
                sub = self.exc_names(e, lineno)
                if sub is None:
                    return None
                names.extend(sub)
            return names
        if isinstance(typ, ast.Name):
            name = self.ctx.aliases.get(typ.id, typ.id).split(".")[-1]
        elif isinstance(typ, ast.Attribute):
            name = typ.attr
        else:
            raise DiscoveryError(f"line {lineno}: unsupported exception type")
        if name in ("Exception", "BaseException"):
            return None
        return [name]

    def is_str_of(self, node, var):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "str" and len(node.args) == 1 and not node.keywords
                and isinstance(node.args[0], ast.Name) and node.args[0].id == var)

    def is_msg_of(self, node, var):
        """``str(e)`` / ``str(exc.value)`` / ``e.args[0]``."""
        if var is None:
            return False
        if self.is_str_of(node, var):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str"
                and len(node.args) == 1 and isinstance(node.args[0], ast.Attribute)
                and node.args[0].attr == "value" and isinstance(node.args[0].value, ast.Name)
                and node.args[0].value.id == var):
            return True
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "args" and isinstance(node.value.value, ast.Name)
                and node.value.value.id == var and isinstance(node.slice, ast.Constant)
                and node.slice.value == 0):
            return True
        return False

    def regex_check(self, kind, pattern, flags, lineno):
        if not isinstance(pattern, str):
            raise DiscoveryError(f"line {lineno}: regex pattern must be a string")
        try:
            re.compile(pattern, flags)
        except re.error as e:
            raise DiscoveryError(f"line {lineno}: bad regex: {e}")
        jsp, jsf = py_regex_to_js(pattern, flags)
        return [kind, pattern, flags, jsp, jsf]

    def re_flags(self, node, lineno):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.re_flags(node.left, lineno) | self.re_flags(node.right, lineno)
        if isinstance(node, (ast.Attribute, ast.Name)):
            full = self.ctx.canonical(node) or ""
            short = full.split(".")[-1]
            if short in RE_FLAG_NAMES:
                return int(RE_FLAG_NAMES[short])
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        raise DiscoveryError(f"line {lineno}: unsupported regex flags")

    def message_check(self, stmt, var):
        """Translate one assertion about the exception message."""
        lineno = stmt.lineno
        bad = DiscoveryError(f"line {lineno}: unsupported exception message assertion")
        if not isinstance(stmt, ast.Assert):
            raise bad
        test = stmt.test
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            op, left, right = test.ops[0], test.left, test.comparators[0]
            if isinstance(op, (ast.In, ast.NotIn)) and self.is_msg_of(right, var):
                s = self.lit(left)
                if not isinstance(s, str):
                    raise bad
                return ["in" if isinstance(op, ast.In) else "notin", s]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                if self.is_msg_of(left, var):
                    s = self.lit(right)
                elif self.is_msg_of(right, var):
                    s = self.lit(left)
                else:
                    raise bad
                if not isinstance(s, str):
                    raise bad
                return ["eq" if isinstance(op, ast.Eq) else "ne", s]
            raise bad
        negate = False
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            negate, test = True, test.operand
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.IsNot, ast.Is)) \
                and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None:
            if isinstance(test.ops[0], ast.Is):
                negate = not negate
            test = test.left
        if isinstance(test, ast.Call):
            short = self.ctx.short(test.func)
            full = self.ctx.canonical(test.func) or ""
            if short in ("search", "match", "fullmatch") and (full.startswith("re.") or full == short):
                if len(test.args) < 2 or not self.is_msg_of(test.args[1], var):
                    raise bad
                pattern = self.lit(test.args[0])
                flags = 0
                if len(test.args) > 2:
                    flags = self.re_flags(test.args[2], lineno)
                for kw in test.keywords:
                    if kw.arg == "flags":
                        flags = self.re_flags(kw.value, lineno)
                    else:
                        raise bad
                chk = self.regex_check(short, pattern, flags, lineno)
                if negate:
                    chk[0] = "not" + chk[0]
                return chk
            if (isinstance(test.func, ast.Attribute) and test.func.attr in ("startswith", "endswith")
                    and self.is_msg_of(test.func.value, var) and len(test.args) == 1 and not negate):
                s = self.lit(test.args[0])
                if not isinstance(s, str):
                    raise bad
                return [test.func.attr, s]
        raise bad

    @staticmethod
    def is_fail_stmt(stmt):
        if isinstance(stmt, ast.Assert) and isinstance(stmt.test, ast.Constant) and not stmt.test.value:
            return True
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
            if isinstance(exc, ast.Name) and exc.id == "AssertionError":
                return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            name = callee_name(stmt.value.func) or ""
            if name in ("pytest.fail", "fail", "self.fail"):
                return True
        return False

    def call_of_stmt(self, stmt):
        if isinstance(stmt, ast.Expr):
            return stmt.value
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            return stmt.value
        return None

    def handle_try(self, node):
        bad = DiscoveryError(f"line {node.lineno}: unsupported try block")
        if node.finalbody or len(node.handlers) != 1 or not 1 <= len(node.body) <= 2:
            raise bad
        call = self.call_of_stmt(node.body[0])
        if call is None:
            raise bad
        xf, info = self.single_call(call, node.lineno, "try block")
        if len(node.body) == 2 and not self.is_fail_stmt(node.body[1]):
            raise bad
        if node.orelse and not (len(node.orelse) == 1 and self.is_fail_stmt(node.orelse[0])):
            raise bad
        h = node.handlers[0]
        names = self.exc_names(h.type, node.lineno)
        if names is not None and "AssertionError" in names:
            raise bad
        checks = []
        for stmt in h.body:
            if isinstance(stmt, ast.Pass):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            checks.append(self.message_check(stmt, h.name))
        self.add_test(node, "raises", info, extra={"exc": names, "checks": checks}, xf=xf)

    def handle_with(self, node):
        bad = DiscoveryError(f"line {node.lineno}: unsupported with block")
        if len(node.items) != 1:
            raise bad
        item = node.items[0].context_expr
        if not (isinstance(item, ast.Call) and self.ctx.short(item.func) == "raises"):
            raise bad
        if not item.args:
            raise bad
        names = self.exc_names(item.args[0], node.lineno)
        checks = []
        for kw in item.keywords:
            if kw.arg == "match":
                checks.append(self.regex_check("search", self.lit(kw.value), 0, node.lineno))
            else:
                raise bad
        var = None
        if node.items[0].optional_vars is not None:
            if not isinstance(node.items[0].optional_vars, ast.Name):
                raise bad
            var = node.items[0].optional_vars.id
        body = [s for s in node.body if not isinstance(s, ast.Pass)]
        if not body:
            raise bad
        call = self.call_of_stmt(body[0])
        if call is None:
            raise bad
        xf, info = self.single_call(call, node.lineno, "with block")
        for s in body[1:]:
            if not self.is_fail_stmt(s):
                raise bad
        self.add_test(node, "raises", info, extra={"exc": names, "checks": checks}, xf=xf)
        self._pending_raises = (var, self.tests[-1])

    def handle_import(self, stmt):
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                if a.asname:
                    self.ctx.aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    self.ctx.aliases[top] = top
        else:
            mod = stmt.module or ""
            for a in stmt.names:
                if a.name == "*":
                    continue
                self.ctx.aliases[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name

    def handle_assign(self, stmt):
        if isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
            if value is None:
                return
        else:
            targets = stmt.targets
            value = stmt.value
        for t in targets:
            self._check_target(t, stmt.lineno)
        ir, calls = self.compile_test(value)
        if len(calls) > 1:
            raise DiscoveryError(
                f"line {stmt.lineno}: assignment calls the entrypoint {len(calls)} times; "
                "each test must be traceable to exactly one entrypoint call")
        if calls:
            for t in targets:
                self._bind_call(t, ir, calls[0])
            return
        v = self.lit(value)
        for t in targets:
            self._bind_const(t, v, stmt.lineno)

    def _check_target(self, t, lineno):
        if isinstance(t, ast.Name):
            return
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._check_target(e, lineno)
            return
        raise DiscoveryError(f"line {lineno}: unsupported assignment")

    def _bind_call(self, t, ir, info):
        if isinstance(t, ast.Name):
            self.tainted.pop(t.id, None)
            self.ctx.calls[t.id] = (ir, info)
            self.ctx.consts.pop(t.id, None)
            return
        for i, e in enumerate(t.elts):
            self._bind_call(e, ["sub", ir, ["lit", i]], info)

    def _bind_const(self, t, v, lineno):
        if isinstance(t, ast.Name):
            self.tainted.pop(t.id, None)
            self.ctx.consts[t.id] = v
            self.ctx.calls.pop(t.id, None)
            return
        try:
            vals = list(v)
        except TypeError:
            raise DiscoveryError(f"line {lineno}: cannot unpack non-iterable value")
        if len(vals) != len(t.elts):
            raise DiscoveryError(f"line {lineno}: wrong number of values to unpack")
        for e, x in zip(t.elts, vals):
            self._bind_const(e, x, lineno)

    # -- mutation-style tests -----------------------------------------------
    def _check_tainted(self, node):
        if not self.tainted:
            return
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id in self.tainted and n.id not in self.ctx.calls \
                    and n.id not in self.ctx.consts:
                raise self._tainted_error(n.id, getattr(n, "lineno", "?"))

    def _tainted_error(self, name, lineno):
        return DiscoveryError(
            f"line {lineno}: {name!r} was passed to the entrypoint call at line "
            f"{self.tainted[name]} and may have been mutated; mutation-style assertions must "
            "immediately follow the entrypoint call")

    def _has_entry_call(self, node):
        return any(isinstance(n, ast.Call) and self.is_entry_call(n) for n in ast.walk(node))

    def is_mutation_candidate(self, stmt):
        """An entrypoint call used as a statement, or assigned to names."""
        if isinstance(stmt, ast.Expr):
            return isinstance(stmt.value, ast.Call) and self.is_entry_call(stmt.value)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            return self._has_entry_call(stmt.value)
        return False

    def _passed_vars(self, call, exclude):
        """Map variable name -> IR of that argument after the call."""
        out = {}
        pos = 0
        for a in call.args:
            if isinstance(a, ast.Starred):
                try:
                    pos += len(list(self.value(a.value)))
                except (DiscoveryError, TypeError):
                    raise DiscoveryError(f"line {call.lineno}: unsupported starred argument")
                continue
            if isinstance(a, ast.Name) and a.id not in exclude and a.id not in out:
                out[a.id] = ["arg", pos]
            pos += 1
        for kw in call.keywords:
            if kw.arg is not None and isinstance(kw.value, ast.Name) \
                    and kw.value.id not in exclude and kw.value.id not in out:
                out[kw.value.id] = ["kwarg", kw.arg]
        return out

    def handle_mutation(self, body, i):
        """Handle an entrypoint call statement/assignment at ``body[i]`` together with the
        asserts immediately following it. Returns the index of the next statement."""
        stmt = body[i]
        lineno = stmt.lineno
        bare = isinstance(stmt, ast.Expr)
        if bare:
            call = stmt.value
            for n in ast.walk(call):
                if n is not call and isinstance(n, ast.Call) and self.is_entry_call(n):
                    raise DiscoveryError(
                        f"line {lineno}: entrypoint arguments must not depend on another entrypoint call")
            info = self.parse_call(call)
            assigned = set()
        else:
            calls = [n for n in ast.walk(stmt.value) if isinstance(n, ast.Call) and self.is_entry_call(n)]
            targets = [stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets
            assigned = set()
            for t in targets:
                self._check_target(t, lineno)
                assigned |= _target_names_ast(t)
            self.handle_assign(stmt)
            if len(calls) != 1:
                return i + 1
            call = calls[0]
            info = None
            for name in assigned:
                if name in self.ctx.calls:
                    info = self.ctx.calls[name][1]
                    break
            if info is None:
                return i + 1
        passed = self._passed_vars(call, assigned)
        passed = {k: v for k, v in passed.items() if k in self.ctx.consts}
        j = i + 1
        while j < len(body) and isinstance(body[j], ast.Assert) \
                and not self._has_entry_call(body[j].test):
            j += 1
        run = body[i + 1:j]

        def names_of(a):
            return {n.id for n in ast.walk(a.test) if isinstance(n, ast.Name)}
        refs = [names_of(a) & set(passed) for a in run]
        if not bare and not any(refs):
            return i + 1  # an ordinary ``result = f(...)`` assignment
        if not run:
            raise DiscoveryError(
                f"line {lineno}: an entrypoint call statement must be immediately followed by "
                "one or more assertions about the variables passed to it")
        for a, r in zip(run, refs):
            if not r and not (names_of(a) & assigned):
                raise DiscoveryError(
                    f"line {a.lineno}: assertion after the mutation call at line {lineno} must "
                    "reference a variable passed to the call (or assigned from it)")
        saved = {}
        for name, ir in passed.items():
            saved[name] = self.ctx.consts.pop(name)
            self.ctx.calls[name] = (ir, info)
        try:
            for a in run:
                self.handle_assert(a)
        finally:
            for name in passed:
                self.ctx.calls.pop(name, None)
                self.tainted[name] = lineno
        return j

    # -- loops (parameterization) -----------------------------------------
    def _loop_record(self, node):
        key = (node.lineno, node.col_offset)
        rec = self._loops.get(key)
        if rec is None:
            rec = {"line": node.lineno, "col": node.col_offset, "kind": "loop", "ok": True,
                   "iter": None, "args": [], "kwargs": {}, "extra": {}, "xf": None}
            self._loops[key] = rec
            self.tests.append(rec)
        return rec

    def _run_iteration(self, body):
        """Process one iteration of a loop body; return False if the loop was broken."""
        self._loop_depth += 1
        try:
            self.handle_body(body, True)
        except _LoopContinue:
            pass
        except _LoopBreak:
            return False
        finally:
            self._loop_depth -= 1
            self._pending_raises = None
        return True

    def handle_for(self, node):
        rec = self._loop_record(node)
        if getattr(node, "type_comment", None) is not None:
            pass
        self._check_target(node.target, node.lineno)
        try:
            items = list(self.value(node.iter))
        except (DiscoveryError, TypeError):
            rec["ok"] = False  # cannot be evaluated
            return
        if len(items) > MAX_LOOP_ITERATIONS:
            raise DiscoveryError(f"line {node.lineno}: too many loop iterations")
        if not items:
            rec["ok"] = False
        broke = False
        for v in items:
            try:
                self._bind_const(node.target, v, node.lineno)
            except DiscoveryError:
                rec["ok"] = False
                return
            if not self._run_iteration(node.body):
                broke = True
                break
        if not broke and node.orelse:
            self.handle_body(node.orelse, True)

    def _truth(self, node):
        v = self.value(node)
        try:
            return bool(v)
        except Exception as e:
            raise DiscoveryError(f"line {getattr(node, 'lineno', '?')}: bad condition: {e}")

    def handle_while(self, node):
        rec = self._loop_record(node)
        n = 0
        broke = False
        while True:
            try:
                cond = self._truth(node.test)
            except DiscoveryError:
                if n == 0:
                    rec["ok"] = False
                    return
                raise
            if not cond:
                break
            n += 1
            if n > MAX_LOOP_ITERATIONS:
                raise DiscoveryError(f"line {node.lineno}: while loop does not terminate")
            if not self._run_iteration(node.body):
                broke = True
                break
        if n == 0:
            rec["ok"] = False
        if not broke and node.orelse:
            self.handle_body(node.orelse, True)

    def handle_if(self, node):
        branch = node.body if self._truth(node.test) else node.orelse
        if branch:
            self.handle_body(branch, True)

    def handle_augassign(self, stmt):
        t = stmt.target
        if not isinstance(t, ast.Name) or t.id not in self.ctx.consts:
            raise DiscoveryError(f"line {stmt.lineno}: unsupported augmented assignment")
        expr = ast.BinOp(left=ast.Name(id=t.id, ctx=ast.Load()), op=stmt.op, right=stmt.value)
        ast.copy_location(expr, stmt)
        ast.fix_missing_locations(expr)
        v = self.value(expr)
        if not _lit_ok(v):
            raise DiscoveryError(f"line {stmt.lineno}: unsupported augmented assignment")
        self.ctx.consts[t.id] = _check_size(v)

    def handle_body(self, body, in_func):
        self._pending_raises = None
        i = 0
        while i < len(body):
            stmt = body[i]
            i += 1
            pending, self._pending_raises = self._pending_raises, None
            if self.is_mutation_candidate(stmt):
                i = self.handle_mutation(body, i - 1)
                self._pending_raises = None
                continue
            if isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    raise DiscoveryError(f"line {stmt.lineno}: decorators not allowed")
                self.local_funcs.add(stmt.name)
                saved = (dict(self.ctx.consts), dict(self.ctx.calls), dict(self.ctx.aliases),
                         dict(self.tainted))
                self.handle_body(stmt.body, True)
                self.ctx.consts, self.ctx.calls, self.ctx.aliases, self.tainted = saved
                self._pending_raises = None
            elif isinstance(stmt, ast.Assert):
                if pending is not None and pending[0] is not None:
                    try:
                        pending[1]["extra"]["checks"].append(self.message_check(stmt, pending[0]))
                        self._pending_raises = pending
                        continue
                    except DiscoveryError:
                        pass
                self.handle_assert(stmt)
            elif isinstance(stmt, ast.Try):
                self.handle_try(stmt)
            elif isinstance(stmt, ast.With):
                self.handle_with(stmt)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.handle_import(stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                self.handle_assign(stmt)
            elif isinstance(stmt, ast.AugAssign):
                self.handle_augassign(stmt)
            elif isinstance(stmt, ast.For):
                self.handle_for(stmt)
                self._pending_raises = None
            elif isinstance(stmt, ast.While):
                self.handle_while(stmt)
                self._pending_raises = None
            elif isinstance(stmt, (ast.Break, ast.Continue)):
                if not self._loop_depth:
                    raise DiscoveryError(f"line {stmt.lineno}: {type(stmt).__name__.lower()} outside loop")
                raise _LoopBreak() if isinstance(stmt, ast.Break) else _LoopContinue()
            elif isinstance(stmt, ast.If) and not (
                isinstance(stmt.test, ast.Compare)
                and isinstance(stmt.test.left, ast.Name)
                and stmt.test.left.id == "__name__"
            ):
                self.handle_if(stmt)
            elif isinstance(stmt, ast.Pass):
                pass
            elif (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                pass  # docstring
            elif (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id in self.local_funcs
                and not stmt.value.args
                and not stmt.value.keywords
                and stmt.value.func.id != self.entrypoint
            ):
                pass  # calling a local test function: tests are already discovered
            elif (
                isinstance(stmt, ast.If)
                and isinstance(stmt.test, ast.Compare)
                and isinstance(stmt.test.left, ast.Name)
                and stmt.test.left.id == "__name__"
            ):
                pass  # ``if __name__ == "__main__":`` runner block
            else:
                raise DiscoveryError(
                    f"line {stmt.lineno}: unsupported statement {type(stmt).__name__}"
                )


def read_expect_comments(source):
    """Map line -> (stream, value) for full-line expect comments."""
    out = {}
    comment_lines = set()
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in toks:
            if tok.type == tokenize.COMMENT and tok.line.strip().startswith("#"):
                line = tok.start[0]
                comment_lines.add(line)
                m = EXPECT_RE.match(tok.string.strip())
                if m:
                    try:
                        val = ast.literal_eval(m.group(2))
                    except Exception as e:
                        raise DiscoveryError(f"line {line}: bad expect literal: {e}")
                    if not isinstance(val, str):
                        raise DiscoveryError(f"line {line}: expect value must be a string")
                    out[line] = (m.group(1), val)
    except (tokenize.TokenError, IndentationError, SyntaxError) as e:
        raise DiscoveryError(f"tokenize failed: {e}")
    return out, comment_lines


SKIP_DIRS = {"__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
             ".mypy_cache", ".pytest_cache"}


def _is_test_like(name):
    """``test*.{ext}``, ``*_test.{ext}``, ``tests.{ext}``, ``*_tests.{ext}``."""
    if "." not in name or name.startswith("."):
        return False
    stem, ext = name.split(".", 1)
    if not ext:
        return False
    if name.startswith("test"):
        return True
    base = name.rsplit(".", 1)[0]
    for s_ in {stem, base}:
        if s_ == "tests" or s_.endswith("_test") or s_.endswith("_tests"):
            return True
    return False


def find_test_files(tests_dir, exclude=()):
    """Return sorted relative (posix) paths of ``.py`` files under ``tests_dir``.
    Raises DiscoveryError for non-Python files with test-like names."""
    root = os.path.abspath(tests_dir)
    excl = {os.path.normcase(os.path.abspath(x)) for x in exclude if x}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn).replace(os.sep, "/")
            if os.path.normcase(os.path.abspath(full)) in excl:
                continue
            if rel_dir == "." and fn in LANGS.values():
                continue  # generated tester
            if fn.startswith(".bcg-"):
                continue  # in-progress generated tester
            if fn.endswith(".py"):
                if os.path.isfile(full):
                    out.append(rel)
                continue
            if fn.endswith((".pyc", ".pyo")):
                continue
            if _is_test_like(fn):
                raise DiscoveryError(f"{rel}: test files must be Python (.py) files")
    out.sort()
    return out


def _defines_entrypoint(tree, entrypoint):
    """True if the module defines the entrypoint itself (i.e. it is a solution, not tests)."""
    if not entrypoint:
        return False
    head = entrypoint.split(".")[0]
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == head:
            return True
    return False


ASYNC_RUNNERS = {"run", "wait_for", "shield", "ensure_future", "create_task"}
ASYNC_MARKS = {"asyncio", "anyio", "trio"}


class _AsyncNormalizer(ast.NodeTransformer):
    """Rewrite async test code into its synchronous equivalent: ``await X`` -> ``X``,
    ``asyncio.run(X)`` / ``asyncio.wait_for(X, t)`` / ``loop.run_until_complete(X)`` -> ``X``,
    ``async def`` -> ``def`` (dropping ``@pytest.mark.asyncio``-style markers). The harness
    drives the entrypoint's awaitable to completion in every target language."""

    def __init__(self, tree):
        self.asyncio_names = {"asyncio"}
        self.runner_names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "asyncio":
                        self.asyncio_names.add(a.asname or a.name)
            elif isinstance(n, ast.ImportFrom) and n.module == "asyncio":
                for a in n.names:
                    if a.name in ASYNC_RUNNERS:
                        self.runner_names.add(a.asname or a.name)

    def _is_runner(self, func):
        if isinstance(func, ast.Name):
            return func.id in self.runner_names
        if isinstance(func, ast.Attribute):
            if func.attr == "run_until_complete":
                return True
            if func.attr in ASYNC_RUNNERS and isinstance(func.value, ast.Name) \
                    and func.value.id in self.asyncio_names:
                return True
        return False

    @staticmethod
    def _is_mark(dec):
        d = dec.func if isinstance(dec, ast.Call) else dec
        return (isinstance(d, ast.Attribute) and d.attr in ASYNC_MARKS
                and isinstance(d.value, ast.Attribute) and d.value.attr == "mark")

    def visit_Await(self, node):
        return ast.copy_location(self.visit(node.value), node)

    def visit_Call(self, node):
        self.generic_visit(node)
        if self._is_runner(node.func) and len(node.args) >= 1 \
                and not isinstance(node.args[0], ast.Starred):
            return node.args[0]
        return node

    def _func(self, node):
        node.decorator_list = [d for d in node.decorator_list if not self._is_mark(d)]
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node):
        return self._func(node)

    def visit_AsyncFunctionDef(self, node):
        node = self._func(node)
        new = ast.FunctionDef(name=node.name, args=node.args, body=node.body,
                              decorator_list=node.decorator_list, returns=node.returns,
                              type_comment=getattr(node, "type_comment", None))
        if hasattr(node, "type_params"):
            new.type_params = node.type_params
        return ast.copy_location(new, node)

    def visit_AsyncWith(self, node):
        self.generic_visit(node)
        return ast.copy_location(ast.With(items=node.items, body=node.body,
                                          type_comment=getattr(node, "type_comment", None)), node)

    def visit_ClassDef(self, node):
        node.decorator_list = [d for d in node.decorator_list if not self._is_mark(d)]
        self.generic_visit(node)
        return node

    def visit_Module(self, node):
        self.generic_visit(node)
        # module-level ``pytestmark = pytest.mark.asyncio`` markers are no-ops
        body = []
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "pytestmark":
                v = stmt.value
                vals = v.elts if isinstance(v, (ast.List, ast.Tuple)) else [v]
                if all(self._is_mark(x) for x in vals):
                    continue
            body.append(stmt)
        node.body = body
        return node


def normalize_async(tree):
    tree = _AsyncNormalizer(tree).visit(tree)
    ast.fix_missing_locations(tree)
    return tree


def _discover_file(path, rel, entrypoint):
    try:
        with open(path, "rb") as f:
            raw = f.read()
        source = raw.decode("utf-8-sig")
    except Exception as e:
        raise DiscoveryError(f"cannot read {rel}: {e}")
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as e:
        raise DiscoveryError(f"syntax error in {rel}: {e}")
    if _defines_entrypoint(tree, entrypoint):
        return None
    tree = normalize_async(tree)
    d = Discoverer(source, entrypoint)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            d.local_funcs.add(n.name)
    try:
        d.handle_body(tree.body, False)
    except DiscoveryError as e:
        raise DiscoveryError(f"{rel}: {e}")
    return d, source


def _assign_ids(rel, raw_tests, source):
    try:
        expects, comment_lines = read_expect_comments(source)
    except DiscoveryError as e:
        raise DiscoveryError(f"{rel}: {e}")
    lines = source.splitlines()

    def _ik(t):
        return -1 if t.get("iter") is None else t["iter"]

    tests = sorted(raw_tests, key=lambda t: (t["line"], _ik(t), t["col"]))
    counts = {}
    for t in tests:
        k = (t["line"], t.get("iter"))
        counts[k] = counts.get(k, 0) + 1
    seen = {}
    for t in tests:
        line = t["line"]
        k = (line, t.get("iter"))
        idx = seen.get(k, 0)
        seen[k] = idx + 1
        base = f"{rel}:{line}" if t.get("iter") is None else f"{rel}:{line}:{t['iter']}"
        t["id"] = base if counts[k] == 1 else f"{base}#{idx}"
        t["file"] = rel
        t["stdout"] = None
        t["stderr"] = None
        if idx == 0:
            k = line - 1
            while k >= 1:
                text = lines[k - 1].strip() if k - 1 < len(lines) else ""
                if k in comment_lines:
                    if k in expects:
                        stream, val = expects[k]
                        if t[stream] is None:
                            t[stream] = val
                    k -= 1
                    continue
                if text == "":
                    k -= 1
                    continue
                break
    return tests


def discover(tests_dir, entrypoint=None, exclude=()):
    """Return (tests, entrypoint). Raises DiscoveryError."""
    if not os.path.isdir(tests_dir):
        raise DiscoveryError(f"{tests_dir} is not a directory")
    files = find_test_files(tests_dir, exclude)
    found = []
    callees = []
    for rel in files:
        r = _discover_file(os.path.join(tests_dir, *rel.split("/")), rel, entrypoint)
        if r is None:
            continue
        d, source = r
        callees.extend(d.callees)
        if d.tests:
            found.append((rel, d.tests, source))
    if not found:
        raise DiscoveryError(f"no tests discovered in {tests_dir}")

    if entrypoint is None:
        callees = set(callees)
        if not callees:
            entrypoint = None
        else:
            bases = {c.split(".")[-1] for c in callees}
            if len(bases) != 1:
                raise DiscoveryError(f"cannot infer a single entrypoint from {sorted(callees)}")
            dotted = sorted(c for c in callees if "." in c)
            classes = {c.split(".")[0] for c in dotted}
            if len(classes) > 1:
                raise DiscoveryError(f"inconsistent entrypoint classes {sorted(classes)}")
            entrypoint = dotted[0] if dotted else next(iter(bases))

    tests = []
    for rel, raw_tests, source in found:
        tests.extend(_assign_ids(rel, raw_tests, source))
    return tests, entrypoint


# ---------------------------------------------------------------------------
# Literal translation
# ---------------------------------------------------------------------------

def _py_factory(f):
    if f is None:
        return "None"
    if isinstance(f, ConstFactory):
        return "lambda: " + py_lit(f.value)
    for name, obj in FACTORY_NAMES.items():
        if f is obj:
            if name in ("Counter", "deque", "OrderedDict", "defaultdict"):
                return "collections." + name
            if name == "Decimal":
                return "decimal.Decimal"
            return name
    raise GenerateError(f"cannot translate defaultdict factory {f!r}")


def py_lit(v):
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
    if isinstance(v, decimal.Decimal):
        return f"decimal.Decimal({str(v)!r})"
    if isinstance(v, (str, bytes)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(py_lit(x) for x in v) + "]"
    if isinstance(v, tuple):
        if len(v) == 1:
            return "(" + py_lit(v[0]) + ",)"
        return "(" + ", ".join(py_lit(x) for x in v) + ")"
    if isinstance(v, collections.deque):
        inner = "[" + ", ".join(py_lit(x) for x in v) + "]"
        if v.maxlen is not None:
            return f"collections.deque({inner}, maxlen={v.maxlen})"
        return f"collections.deque({inner})"
    if isinstance(v, frozenset):
        if not v:
            return "frozenset()"
        return "frozenset({" + ", ".join(py_lit(x) for x in v) + "})"
    if isinstance(v, set):
        if not v:
            return "set()"
        return "{" + ", ".join(py_lit(x) for x in v) + "}"
    if isinstance(v, dict):
        body = "{" + ", ".join(f"{py_lit(k)}: {py_lit(x)}" for k, x in v.items()) + "}"
        if isinstance(v, collections.Counter):
            return f"collections.Counter({body})"
        if isinstance(v, collections.defaultdict):
            return f"collections.defaultdict({_py_factory(v.default_factory)}, {body})"
        if isinstance(v, collections.OrderedDict):
            return f"collections.OrderedDict({body})"
        return body
    raise GenerateError(f"cannot translate {v!r}")


def js_str(s):
    # json.dumps escapes everything needed; also escape U+2028/2029 for older engines.
    return json.dumps(s).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _js_float(v):
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    if v == 0 and math.copysign(1.0, v) < 0:
        return "-0"
    return repr(v)


def _js_key_string(k):
    """String form a JS object would use for the Python key ``k``, or None."""
    if isinstance(k, str):
        return k
    if isinstance(k, bool) or k is None:
        return None
    if isinstance(k, int):
        return str(k)
    if isinstance(k, float):
        if math.isnan(k) or math.isinf(k):
            return None
        if k == int(k) and abs(k) < 1e21:
            return str(int(k))
        return None
    return None


def js_lit(v, mode="arg"):
    """Translate a Python value into a JS expression.

    mode "arg": a plain JS value passed to the solution.
    mode "exp": an expected value, with Python type information preserved
    through the runtime helpers __F / __D / __S / __M.
    """
    exp = mode == "exp"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        if abs(v) > 2**53 - 1:
            return f"{v}n"
        return str(v)
    if isinstance(v, float):
        return f"__F({_js_float(v)})" if exp else _js_float(v)
    if isinstance(v, decimal.Decimal):
        f = float(v)
        if exp:
            return f"__D({_js_float(f)}, {js_str(str(v))})"
        if v == v.to_integral_value() and v.is_finite() and abs(v) <= 2**53 - 1:
            return str(int(v))
        return _js_float(f)
    if isinstance(v, str):
        return js_str(v)
    if isinstance(v, bytes):
        return "[" + ", ".join(str(b) for b in v) + "]"
    if isinstance(v, (list, tuple, collections.deque)):
        return "[" + ", ".join(js_lit(x, mode) for x in v) + "]"
    if isinstance(v, (set, frozenset)):
        items = "[" + ", ".join(js_lit(x, mode) for x in v) + "]"
        return f"__S({items})" if exp else f"new Set({items})"
    if isinstance(v, dict):
        if exp:
            kind = "counter" if isinstance(v, collections.Counter) else "dict"
            entries = ", ".join(f"[{js_lit(k, mode)}, {js_lit(x, mode)}]" for k, x in v.items())
            return f"__M([{entries}], {js_str(kind)})"
        keys = [_js_key_string(k) for k in v]
        if all(k is not None for k in keys) and len(set(keys)) == len(keys):
            return "{" + ", ".join(f"[{js_str(ks)}]: {js_lit(x, mode)}" for ks, x in zip(keys, v.values())) + "}"
        entries = ", ".join(f"[{js_lit(k, mode)}, {js_lit(x, mode)}]" for k, x in v.items())
        return f"new Map([{entries}])"
    raise GenerateError(f"cannot translate {v!r}")


# ---------------------------------------------------------------------------
# Tester generation
# ---------------------------------------------------------------------------

PY_RUNTIME_HEAD = r'''
import sys, os, io, json, inspect, importlib.util, importlib.machinery
import collections, collections.abc, decimal, fractions, math, operator, re


def _emit(fh, obj):
    if fh is None:
        return
    fh.write(json.dumps(obj) + "\n")
    fh.flush()


class _Capture:
    def __init__(self):
        self._bufs = {}
        self._saved = None

    def start(self):
        self._saved = (sys.stdout, sys.stderr)
        self._bufs = {}
        streams = []
        for name in ("stdout", "stderr"):
            b = io.BytesIO()
            w = io.TextIOWrapper(b, encoding="utf-8", errors="surrogateescape", newline="\n", write_through=True)
            self._bufs[name] = (b, w)
            streams.append(w)
        sys.stdout, sys.stderr = streams

    def stop(self):
        out = {}
        for name, (b, w) in self._bufs.items():
            try:
                w.flush()
            except Exception:
                pass
            out[name] = b.getvalue().decode("utf-8", errors="surrogateescape")
        if self._saved is not None:
            sys.stdout, sys.stderr = self._saved
            self._saved = None
        return out


def _load_solution(path):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("solution not found: " + path)
    d = os.path.dirname(path)
    if d not in sys.path:
        sys.path.insert(0, d)
    name = "solution"
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader, origin=path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


def _class_factory(cls, meth):
    try:
        raw = inspect.getattr_static(cls, meth)
    except AttributeError:
        return None
    if isinstance(raw, (staticmethod, classmethod)):
        return lambda: getattr(cls, meth)
    if not callable(getattr(cls, meth, None)):
        return None

    def make():
        inst = cls()
        return getattr(inst, meth)
    return make


def _resolve(mod, entry):
    parts = entry.split(".")
    meth = parts[-1]
    if len(parts) > 1:
        cls = getattr(mod, parts[0], None)
        if inspect.isclass(cls):
            f = _class_factory(cls, meth)
            if f is not None:
                return f
    fn = getattr(mod, meth, None)
    if fn is not None and callable(fn) and not inspect.isclass(fn) or (inspect.isclass(fn) and len(parts) == 1):
        return lambda: fn
    classes = [v for v in vars(mod).values() if inspect.isclass(v)]
    local = [c for c in classes if getattr(c, "__module__", None) == mod.__name__]
    for cls in local + [c for c in classes if c not in local]:
        f = _class_factory(cls, meth)
        if f is not None:
            return f
    raise LookupError("entrypoint %r not found in solution" % entry)


_TOL = {"abs": 1e-09, "rel": 1e-09}


def _isnum(x):
    return isinstance(x, (int, float, decimal.Decimal, fractions.Fraction)) and not isinstance(x, bool)


def _tofloat(x):
    try:
        return float(x)
    except OverflowError:
        return math.inf if x > 0 else -math.inf


def _close(a, e, abs_tol, rel_tol, strict=False):
    fa, fe = _tofloat(a), _tofloat(e)
    if math.isnan(fa) or math.isnan(fe):
        return math.isnan(fa) and math.isnan(fe)
    if math.isinf(fa) or math.isinf(fe):
        return fa == fe
    try:
        if not strict and a == e:
            return True
        diff = abs(a - e)
        if isinstance(diff, (decimal.Decimal, fractions.Fraction)):
            diff = _tofloat(diff)
    except Exception:
        diff = abs(fa - fe)
    bound = max(abs_tol, rel_tol * max(abs(fa), abs(fe)))
    return diff < bound if strict else diff <= bound


def _numeq(a, e):
    if isinstance(a, int) and isinstance(e, int):
        return a == e
    return _close(a, e, _TOL["abs"], _TOL["rel"])


def _is_mapping(x):
    return isinstance(x, collections.abc.Mapping)


def _match_unordered(actual, expected):
    rest = list(actual)
    for x in expected:
        for i, y in enumerate(rest):
            if _deq(y, x):
                del rest[i]
                break
        else:
            return False
    return not rest


_NATIVE = [False]
_SEQS = (list, tuple, collections.deque)


def _deq(a, e):
    """Compare an actual value with an expected value (container semantics, float tolerance)."""
    if e is None or a is None:
        return a is e
    if _NATIVE[0]:
        # values produced by statically typed targets: sequence kinds are interchangeable,
        # bytes may come back as a list of ints or a string, sets may come back as sequences
        if isinstance(e, (bytes, bytearray)) and not isinstance(a, (bytes, bytearray)):
            if isinstance(a, str):
                return a.encode("utf-8", "surrogateescape") == bytes(e)
            if isinstance(a, _SEQS) and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 256 for x in a):
                return bytes(a) == bytes(e)
            return False
        if isinstance(e, _SEQS) and isinstance(a, _SEQS):
            return len(a) == len(e) and all(_deq(x, y) for x, y in zip(a, e))
        if isinstance(e, (set, frozenset)) and isinstance(a, _SEQS):
            return len(a) == len(e) and _match_unordered(list(a), list(e))
        if isinstance(e, _SEQS) and isinstance(a, (set, frozenset)):
            return len(a) == len(e) and _match_unordered(list(a), list(e))
    try:
        if type(a) is type(e) and not isinstance(e, (float, decimal.Decimal)) and a == e:
            return True
    except Exception:
        pass
    if isinstance(e, bool) or isinstance(a, bool):
        return isinstance(a, (bool, int)) and isinstance(e, (bool, int)) and a == e
    if _isnum(e) or _isnum(a):
        return _isnum(a) and _isnum(e) and _numeq(a, e)
    if isinstance(e, (str, bytes)) or isinstance(a, (str, bytes)):
        try:
            return type(a) is type(e) and a == e
        except Exception:
            return False
    if isinstance(e, (set, frozenset)):
        if not isinstance(a, (set, frozenset)) or len(a) != len(e):
            return False
        try:
            if a == e:
                return True
            ra = set(a)
            re_ = [x for x in e if not (x in ra and (ra.discard(x) or True))]
        except Exception:
            ra, re_ = list(a), list(e)
        return _match_unordered(ra, re_)
    if _is_mapping(e):
        if not _is_mapping(a):
            return False
        ea, aa = list(e.items()), list(a.items())
        if isinstance(e, collections.Counter) or isinstance(a, collections.Counter):
            ea = [(k, v) for k, v in ea if v != 0]
            aa = [(k, v) for k, v in aa if v != 0]
        if len(ea) != len(aa):
            return False
        adict = {}
        unhashable = []
        for k, v in aa:
            try:
                adict[k] = v
            except TypeError:
                unhashable.append((k, v))
        leftover_e = []
        for k, v in ea:
            try:
                if k in adict:
                    if not _deq(adict.pop(k), v):
                        return False
                    continue
            except TypeError:
                pass
            leftover_e.append((k, v))
        rest = list(adict.items()) + unhashable
        for k, v in leftover_e:
            for i, (ak, av) in enumerate(rest):
                if _deq(ak, k):
                    if not _deq(av, v):
                        return False
                    del rest[i]
                    break
            else:
                return False
        return not rest
    if isinstance(e, (list, collections.deque)):
        if not isinstance(a, (list, collections.deque)) or len(a) != len(e):
            return False
        return all(_deq(x, y) for x, y in zip(a, e))
    if isinstance(e, tuple):
        if not isinstance(a, tuple) or len(a) != len(e):
            return False
        return all(_deq(x, y) for x, y in zip(a, e))
    try:
        return bool(a == e)
    except Exception:
        return False


def _exc_match(exc, names):
    if names is None or _NATIVE[0]:
        return isinstance(exc, Exception)
    mro = [c.__name__ for c in type(exc).__mro__]
    return any(n in mro for n in names)


def _check_msg(msg, chk):
    kind = chk[0]
    if kind == "in":
        return chk[1] in msg
    if kind == "notin":
        return chk[1] not in msg
    if kind == "eq":
        return msg == chk[1]
    if kind == "ne":
        return msg != chk[1]
    if kind == "startswith":
        return msg.startswith(chk[1])
    if kind == "endswith":
        return msg.endswith(chk[1])
    neg = kind.startswith("not")
    fn = getattr(re, kind[3:] if neg else kind)
    found = fn(chk[1], msg, chk[2]) is not None
    return found != neg


def _complete(value):
    """Run an awaitable (coroutine, future, task, async generator result) to completion."""
    if not inspect.isawaitable(value):
        return value
    import asyncio

    async def _drive():
        return await value
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_drive())
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except BaseException:
                pass
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _run_one(t, factory, cap):
    kind = t["kind"]
    extra = t.get("extra") or {}
    args = t["args"]()
    kwargs = t["kwargs"]()
    raised = None
    result = raw = None
    cap.start()
    try:
        try:
            fn = factory()
            result = raw = _complete(fn(*args, **kwargs))
            xf = t["xf"]()
            if xf is not None:
                result = _ev(xf, raw, None, args, kwargs)
        except Exception as e:
            raised = e
        except BaseException as e:  # SystemExit, KeyboardInterrupt, ...
            raised = e
    finally:
        out = cap.stop()
    return _judge(t, result, raw, raised, out)


def _judge(t, result, raw, raised, out):
    kind = t["kind"]
    extra = t.get("extra") or {}
    if kind == "raises":
        if raised is None:
            return False, "expected an exception, none raised"
        if not isinstance(raised, Exception):
            return False, "raised non-Exception %r" % (raised,)
        if not _exc_match(raised, extra.get("exc")):
            return False, "raised wrong exception type %s" % type(raised).__name__
        try:
            msg = str(raised)
        except Exception:
            msg = ""
        for chk in extra.get("checks") or []:
            if not _check_msg(msg, chk):
                return False, "exception message %r does not satisfy %r" % (msg, chk[:2])
    else:
        if raised is not None:
            return False, "raised %s: %s" % (type(raised).__name__, raised)
        saved_tol = dict(_TOL)
        try:
            if extra.get("approx"):
                _TOL["abs"], _TOL["rel"] = extra["approx"][0], extra["approx"][1]
            if kind == "eq":
                ok = _deq(result, t["expected"]())
            elif kind == "ne":
                ok = not _deq(result, t["expected"]())
            elif kind == "truthy":
                ok = bool(result)
            elif kind == "falsy":
                ok = not bool(result)
            elif kind == "is":
                ok = result is t["expected"]()
            elif kind == "isnot":
                ok = result is not t["expected"]()
            elif kind in ("close", "notclose"):
                e = t["expected"]()
                if not _isnum(result):
                    ok = False
                else:
                    ok = _close(result, e, extra.get("abs_tol", 0.0), extra.get("rel_tol", 0.0),
                                bool(extra.get("strict")))
                if kind == "notclose":
                    ok = _isnum(result) and not ok
            elif kind in ("lt", "le", "gt", "ge"):
                e = t["expected"]()
                if _isnum(result) and _isnum(e) and isinstance(result, decimal.Decimal) != isinstance(e, decimal.Decimal) \
                        and (isinstance(result, float) or isinstance(e, float)):
                    result_c, e_c = float(result), float(e)
                else:
                    result_c, e_c = result, e
                ok = bool({"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}[kind](result_c, e_c))
            elif kind in ("in", "notin"):
                e = t["expected"]()
                if isinstance(e, str):
                    found = isinstance(result, str) and result in e
                else:
                    found = any(_deq(result, x) for x in e)
                ok = found if kind == "in" else not found
            else:
                return False, "unknown test kind"
        except Exception as e:
            return False, "comparison failed: %r" % (e,)
        finally:
            _TOL.update(saved_tol)
        if not ok:
            try:
                if t["xf"]() is not None:
                    return False, "got %r (entrypoint returned %r)" % (result, raw)
                return False, "got %r" % (result,)
            except Exception:
                return False, "assertion failed"
    if t["stdout"] is not None and out["stdout"] != t["stdout"]:
        return False, "stdout mismatch: %r" % (out["stdout"],)
    if t["stderr"] is not None and out["stderr"] != t["stderr"]:
        return False, "stderr mismatch: %r" % (out["stderr"],)
    return True, ""


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: tester.py <solution_path> [--results FILE] [--ids FILE] [--tol FLOAT]\n")
        return 2
    solution = argv[1]
    results_path = None
    ids_path = None
    i = 2
    while i < len(argv):
        if argv[i] == "--results" and i + 1 < len(argv):
            results_path = argv[i + 1]; i += 2
        elif argv[i] == "--ids" and i + 1 < len(argv):
            ids_path = argv[i + 1]; i += 2
        elif argv[i] == "--tol" and i + 1 < len(argv):
            _TOL["abs"] = float(argv[i + 1]); _TOL["rel"] = 0.0; i += 2
        else:
            i += 1
    selected = TESTS
    if ids_path:
        with open(ids_path, encoding="utf-8") as f:
            wanted = json.load(f)
        by_id = {t["id"]: t for t in TESTS}
        selected = [by_id[x] for x in wanted if x in by_id]
    fh = open(results_path, "a", encoding="utf-8") if results_path else None
    real_stdout = sys.stdout
    cap = _Capture()
    cap.start()
    try:
        try:
            mod = _load_solution(solution)
            factory = _resolve(mod, ENTRYPOINT)
        finally:
            cap.stop()
    except BaseException as e:
        _emit(fh, {"event": "load_error", "msg": "%s: %s" % (type(e).__name__, e)})
        if fh is None:
            real_stdout.write(json.dumps({"status": "error", "passed": [], "failed": [t["id"] for t in TESTS]}) + "\n")
        return 2
    passed, failed = [], []
    for t in selected:
        _emit(fh, {"event": "start", "id": t["id"]})
        try:
            ok, msg = _run_one(t, factory, cap)
        except BaseException as e:
            ok, msg = False, "harness error: %r" % (e,)
        (passed if ok else failed).append(t["id"])
        _emit(fh, {"event": "result", "id": t["id"], "ok": ok, "msg": msg})
    _emit(fh, {"event": "done"})
    if fh is None:
        status = "pass" if not failed else "fail"
        real_stdout.write(json.dumps({"status": status, "passed": passed, "failed": failed}) + "\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    _code = main(sys.argv)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(_code)
'''


PY_RUNTIME = PY_EVAL_SRC + PY_RUNTIME_HEAD

JS_RUNTIME = r'''
function __bcgMod(name) {
  if (typeof process !== "undefined" && typeof process.getBuiltinModule === "function") {
    return process.getBuiltinModule(name);
  }
  // eslint-disable-next-line no-undef
  return require(name);
}
const fs = __bcgMod("fs");
const path = __bcgMod("path");
const vm = __bcgMod("vm");
const nodeModule = __bcgMod("module");

function emit(resultsPath, obj) {
  if (!resultsPath) return;
  fs.appendFileSync(resultsPath, JSON.stringify(obj) + "\n");
}

function findTypeScript(fromFile) {
  const tries = [];
  try { tries.push(nodeModule.createRequire(fromFile)); } catch (e) {}
  try { tries.push(nodeModule.createRequire(path.resolve(process.argv[1] || "."))); } catch (e) {}
  for (const req of tries) {
    try { return req("typescript"); } catch (e) {}
  }
  const dirs = [
    path.join(path.dirname(process.execPath), "..", "lib", "node_modules", "typescript"),
    "/usr/lib/node_modules/typescript",
    "/usr/local/lib/node_modules/typescript",
  ];
  if (process.env.NODE_PATH) {
    for (const p of process.env.NODE_PATH.split(path.delimiter)) if (p) dirs.push(path.join(p, "typescript"));
  }
  for (const d of dirs) {
    try {
      if (fs.existsSync(d)) return nodeModule.createRequire(path.join(d, "package.json"))(d);
    } catch (e) {}
  }
  return null;
}

function stripEsm(src) {
  return src
    .replace(/^\s*export\s+default\s+(?=(async\s+)?(function|class)\b)/gm, "")
    .replace(/^\s*export\s+default\s+/gm, "module.exports.default = ")
    .replace(/^\s*export\s+(?=(declare\s+|abstract\s+|async\s+)*(function|class|const|let|var|enum|interface|type|namespace)\b)/gm, "")
    .replace(/^\s*export\s*\{[^}]*\}\s*(from\s*['"][^'"]*['"])?\s*;?/gm, "")
    .replace(/^\s*export\s*\*\s*from\s*['"][^'"]*['"]\s*;?/gm, "");
}

function hasEsm(src) {
  return /^\s*(import|export)\b(?!\s*\()/m.test(src);
}

function transpile(src, file, isTs) {
  if (!isTs && !hasEsm(src)) return src;
  const ts = findTypeScript(file);
  if (ts && typeof ts.transpileModule === "function") {
    try {
      const out = ts.transpileModule(src, {
        fileName: isTs ? "solution.ts" : "solution.js",
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          target: ts.ScriptTarget.ES2020,
          esModuleInterop: true,
          allowJs: true,
          experimentalDecorators: true,
        },
        reportDiagnostics: false,
      });
      return out.outputText;
    } catch (e) {}
  }
  if (isTs && typeof nodeModule.stripTypeScriptTypes === "function") {
    const origEmit = process.emitWarning;
    process.emitWarning = function () {};
    try {
      let out;
      try {
        out = nodeModule.stripTypeScriptTypes(src, { mode: "transform" });
      } catch (e) {
        out = nodeModule.stripTypeScriptTypes(src);
      }
      return stripEsm(out);
    } finally {
      process.emitWarning = origEmit;
    }
  }
  return stripEsm(src);
}

function loadSolution(file, isTs) {
  const abs = path.resolve(file);
  if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) throw new Error("solution not found: " + abs);
  let src = fs.readFileSync(abs, "utf8");
  if (src.charCodeAt(0) === 0xfeff) src = src.slice(1);
  if (src.startsWith("#!")) src = "//" + src.slice(2);
  src = transpile(src, abs, isTs);
  const classNames = [];
  const re = /\bclass\s+([A-Za-z_$][\w$]*)/g;
  let m;
  while ((m = re.exec(src)) !== null) if (!classNames.includes(m[1])) classNames.push(m[1]);
  const varRe = /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\{|class\b)/g;
  while ((m = varRe.exec(src)) !== null) if (!classNames.includes(m[1])) classNames.push(m[1]);
  const wrapped =
    "(function (exports, require, module, __filename, __dirname) {" + src +
    "\n;return function (__bcg_name) { try { return eval(__bcg_name); } catch (__bcg_e) { return undefined; } };\n})";
  const fn = vm.runInThisContext(wrapped, { filename: abs });
  const mod = { exports: {} };
  const req = nodeModule.createRequire(abs);
  const lookup = fn.call(mod.exports, mod.exports, req, mod, abs, path.dirname(abs));
  return { lookup, mod, classNames };
}

function isClassFn(f) {
  try { return /^class\b/.test(Function.prototype.toString.call(f)); } catch (e) { return false; }
}

function classFactory(holder, meth) {
  if (holder === null || holder === undefined) return null;
  const t = typeof holder;
  if (t === "function") {
    if (typeof holder[meth] === "function" && !(meth in Function.prototype && holder[meth] === Function.prototype[meth])) {
      return () => holder[meth].bind(holder);
    }
    if (holder.prototype && typeof holder.prototype[meth] === "function") {
      return () => { const inst = new holder(); return inst[meth].bind(inst); };
    }
    return null;
  }
  if (t === "object" && typeof holder[meth] === "function") {
    return () => holder[meth].bind(holder);
  }
  return null;
}

function resolve(loaded, entry) {
  const parts = entry.split(".");
  const meth = parts[parts.length - 1];
  const { lookup, mod, classNames } = loaded;
  const exp = mod.exports;
  const getExport = (n) => {
    try {
      if (exp && (typeof exp === "object" || typeof exp === "function") && n in exp) return exp[n];
    } catch (e) {}
    return undefined;
  };
  if (parts.length > 1) {
    const holder = lookup(parts[0]) ?? getExport(parts[0]);
    const f = classFactory(holder, meth);
    if (f) return f;
  }
  let direct = lookup(meth);
  if (typeof direct !== "function") direct = getExport(meth);
  if (typeof direct !== "function" && typeof exp === "function" && exp.name === meth) direct = exp;
  if (typeof direct !== "function" && exp && typeof exp.default === "function" && exp.default.name === meth) direct = exp.default;
  if (typeof direct === "function") {
    if (isClassFn(direct)) return () => (...a) => new direct(...a);
    return () => direct;
  }
  const holders = [];
  for (const n of classNames) {
    const v = lookup(n);
    if (v !== undefined) holders.push(v);
  }
  if (exp && (typeof exp === "object" || typeof exp === "function")) {
    if (typeof exp === "function") holders.push(exp);
    for (const k of Object.keys(exp)) holders.push(exp[k]);
  }
  for (const h of holders) {
    const f = classFactory(h, meth);
    if (f) return f;
  }
  throw new Error("entrypoint " + JSON.stringify(entry) + " not found in solution");
}

function isPlainObject(v) {
  if (v === null || typeof v !== "object") return false;
  const p = Object.getPrototypeOf(v);
  return p === null || p === Object.prototype || !Array.isArray(v);
}

const TOL = { abs: 1e-9, rel: 1e-9 };

// Tagged expected values (Python type information).
class PyFloat { constructor(v) { this.v = v; } }
class PyDec { constructor(v, s) { this.v = v; this.s = s; } }
class PySet { constructor(items) { this.items = items; } }
class PyDict { constructor(entries, kind) { this.entries = entries; this.kind = kind; } }
function __F(v) { return new PyFloat(v); }
function __D(v, s) { return new PyDec(v, s); }
function __S(items) { return new PySet(items); }
function __M(entries, kind) { return new PyDict(entries, kind); }

function isNumLike(a) {
  if (typeof a === "number" || typeof a === "bigint") return true;
  if (a instanceof PyFloat || a instanceof PyDec || a instanceof Number) return true;
  if (a !== null && typeof a === "object" && !Array.isArray(a) && !(a instanceof Map) && !(a instanceof Set)) {
    // decimal.js / big.js style objects
    const ctor = a.constructor && a.constructor.name;
    if (typeof a.toNumber === "function" || (ctor && /^(Decimal|Big|BigNumber|Fraction)$/.test(ctor))) {
      return Number.isFinite(Number(String(a))) || /^-?(Infinity|NaN)$/.test(String(a));
    }
  }
  return false;
}

function toNum(a) {
  if (typeof a === "number") return a;
  if (typeof a === "bigint") return Number(a);
  if (a instanceof PyFloat || a instanceof PyDec) return a.v;
  if (a instanceof Number) return a.valueOf();
  if (typeof a === "string") return Number(a);
  if (a !== null && typeof a === "object") {
    if (typeof a.toNumber === "function") { try { return Number(a.toNumber()); } catch (e) {} }
    return Number(String(a));
  }
  return NaN;
}

function closeNum(a, e, absTol, relTol, strict) {
  if (Number.isNaN(a) || Number.isNaN(e)) return Number.isNaN(a) && Number.isNaN(e);
  if (!Number.isFinite(a) || !Number.isFinite(e)) return a === e;
  if (!strict && a === e) return true;
  const diff = Math.abs(a - e);
  const bound = Math.max(absTol, relTol * Math.max(Math.abs(a), Math.abs(e)));
  return strict ? diff < bound : diff <= bound;
}

function numEq(a, e) {
  const floaty = (x) => x instanceof PyFloat || x instanceof PyDec ||
    (typeof x === "number" && !Number.isInteger(x)) ||
    (x !== null && typeof x === "object" && !(x instanceof PyFloat) && !(typeof x === "bigint"));
  if (!floaty(a) && !floaty(e)) {
    const ia = a instanceof Number ? a.valueOf() : a;
    if (typeof ia === "bigint" || typeof e === "bigint") {
      try {
        if (typeof ia === "number" && !Number.isInteger(ia)) return false;
        if (typeof e === "number" && !Number.isInteger(e)) return false;
        return BigInt(ia) === BigInt(e);
      } catch (x) { return false; }
    }
    return ia === e;
  }
  if (e instanceof PyDec && a !== null && typeof a === "object" && !(a instanceof Number)) {
    try { if (String(a) === e.s) return true; } catch (x) {}
  }
  return closeNum(toNum(a), toNum(e), TOL.abs, TOL.rel, false);
}

function toArrayLike(a) {
  if (Array.isArray(a)) return a;
  if (a !== null && typeof a === "object" && ArrayBuffer.isView(a) && !(a instanceof DataView)) return Array.from(a);
  return null;
}

function primKey(x) {
  if (typeof x === "string") return "s:" + x;
  if (typeof x === "number" && Number.isInteger(x)) return "n:" + x;
  if (typeof x === "bigint") return "n:" + x.toString();
  if (typeof x === "boolean") return "b:" + x;
  return null;
}

function matchUnordered(actual, expected) {
  if (actual.length !== expected.length) return false;
  const rest = actual.slice();
  // fast path for primitive members
  const counts = new Map();
  let allPrim = true;
  for (const x of rest) {
    const k = primKey(x);
    if (k === null) { allPrim = false; break; }
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  if (allPrim) {
    let ok = true;
    for (const x of expected) {
      const k = primKey(x);
      if (k === null) { ok = false; break; }
      const c = counts.get(k) || 0;
      if (!c) return false;
      counts.set(k, c - 1);
    }
    if (ok) return true;
  }
  for (const x of expected) {
    let found = -1;
    for (let i = 0; i < rest.length; i++) {
      if (deepEq(rest[i], x)) { found = i; break; }
    }
    if (found < 0) return false;
    rest.splice(found, 1);
  }
  return rest.length === 0;
}

function plain(v) {
  // expected (tagged) value -> plain JS value
  if (v instanceof PyFloat || v instanceof PyDec) return v.v;
  if (v instanceof PySet) return v.items.map(plain);
  if (v instanceof PyDict) return v.entries.map(([k, x]) => [plain(k), plain(x)]);
  if (Array.isArray(v)) return v.map(plain);
  return v;
}

function keyStrings(k) {
  if (typeof k === "string") return [k];
  if (typeof k === "number") return [String(k)];
  if (typeof k === "bigint") return [k.toString()];
  if (k instanceof PyFloat) return [String(k.v), Number.isInteger(k.v) ? k.v.toFixed(1) : String(k.v)];
  if (k instanceof PyDec) return [k.s, String(k.v)];
  if (typeof k === "boolean") return [String(k), k ? "True" : "False"];
  if (k === null) return ["null", "None"];
  const p = plain(k);
  const out = [];
  try { out.push(String(p)); } catch (e) {}
  try { out.push(JSON.stringify(p)); } catch (e) {}
  if (Array.isArray(p)) {
    try { out.push("(" + p.map((x) => (typeof x === "string" ? "'" + x + "'" : String(x))).join(", ") + (p.length === 1 ? ",)" : ")")); } catch (e) {}
  }
  return out;
}

function dictEq(a, e) {
  const isCounter = e.kind === "counter";
  let eEntries = e.entries;
  const zero = (v) => (typeof v === "number" && v === 0) || (typeof v === "bigint" && v === 0n);
  if (isCounter) eEntries = eEntries.filter(([k, v]) => !zero(v));
  let aEntries;
  let fromObject = false;
  if (a instanceof Map) {
    aEntries = Array.from(a.entries());
  } else if (a !== null && typeof a === "object" && !Array.isArray(a) && !(a instanceof Set) && !isNumLike(a)) {
    aEntries = Object.keys(a).map((k) => [k, a[k]]);
    fromObject = true;
  } else {
    return false;
  }
  if (isCounter) aEntries = aEntries.filter(([k, v]) => !zero(v));
  if (aEntries.length !== eEntries.length) return false;
  const rest = aEntries.slice();
  if (fromObject) {
    const idx = new Map();
    rest.forEach(([k], i) => idx.set(k, i));
    const used = new Set();
    for (const [k, v] of eEntries) {
      let hit = -1;
      for (const ks of keyStrings(k)) {
        if (idx.has(ks) && !used.has(idx.get(ks))) { hit = idx.get(ks); break; }
      }
      if (hit < 0) return false;
      used.add(hit);
      if (!deepEq(rest[hit][1], v)) return false;
    }
    return true;
  }
  for (const [k, v] of eEntries) {
    let found = -1;
    const pk = primKey(k);
    if (pk !== null) {
      for (let i = 0; i < rest.length; i++) if (primKey(rest[i][0]) === pk && typeof rest[i][0] !== "boolean" === (typeof k !== "boolean")) { found = i; break; }
    }
    if (found < 0) {
      for (let i = 0; i < rest.length; i++) if (deepEq(rest[i][0], k)) { found = i; break; }
    }
    if (found < 0) return false;
    if (!deepEq(rest[found][1], v)) return false;
    rest.splice(found, 1);
  }
  return rest.length === 0;
}

// actual (JS value) vs expected (translated Python value)
function deepEq(a, e) {
  if (e === null) return a === null || a === undefined;
  if (a === undefined) a = null;
  if (typeof e === "boolean") return typeof a === "boolean" && a === e;
  if (typeof e === "number" || typeof e === "bigint" || e instanceof PyFloat || e instanceof PyDec) {
    if (!isNumLike(a) && !(e instanceof PyDec && typeof a === "string" && a.trim() !== "" && !Number.isNaN(Number(a)))) return false;
    return numEq(a, e);
  }
  if (typeof e === "string") return typeof a === "string" && a === e;
  if (Array.isArray(e)) {
    const arr = toArrayLike(a);
    if (!arr || arr.length !== e.length) return false;
    for (let i = 0; i < e.length; i++) if (!deepEq(arr[i], e[i])) return false;
    return true;
  }
  if (e instanceof PySet) {
    let items;
    if (a instanceof Set) items = Array.from(a);
    else {
      const arr = toArrayLike(a);
      if (!arr) return false;
      items = arr;
    }
    return matchUnordered(items, e.items);
  }
  if (e instanceof PyDict) return dictEq(a, e);
  if (typeof e === "object") {
    // legacy plain-object expected values
    return dictEq(a, new PyDict(Object.keys(e).map((k) => [k, e[k]]), "dict"));
  }
  return false;
}

// ---- Python-semantics evaluator for primitive operations around the entrypoint result
class PyEvalError extends Error { constructor(name, msg) { super(msg); this.name = name; } }
function pyErr(name, msg) { return new PyEvalError(name, msg); }

function tagged(v) {
  // plain JS value -> expected-style tagged value (for deepEq)
  if (v === undefined) return null;
  if (Array.isArray(v)) return v.map(tagged);
  if (v instanceof Set) return new PySet(Array.from(v).map(tagged));
  if (v instanceof Map) return new PyDict(Array.from(v.entries()).map(([k, x]) => [tagged(k), tagged(x)]), "dict");
  if (typeof v === "number" && !Number.isInteger(v)) return new PyFloat(v);
  if (v !== null && typeof v === "object" && !isNumLike(v) && !ArrayBuffer.isView(v) &&
      Object.getPrototypeOf(v) === Object.prototype) {
    return new PyDict(Object.keys(v).map((k) => [k, tagged(v[k])]), "dict");
  }
  return v;
}
function pyEq(a, b) {
  if (a === b) return true;
  if (typeof a === "boolean" || typeof b === "boolean") {
    if (typeof a === "boolean" && typeof b === "boolean") return a === b;
    const x = typeof a === "boolean" ? Number(a) : a, y = typeof b === "boolean" ? Number(b) : b;
    return isNumLike(x) && isNumLike(y) && toNum(x) === toNum(y);
  }
  if (a === undefined) a = null;
  if (b === undefined) b = null;
  if (a === null || b === null) return a === b;
  if (isNumLike(a) && isNumLike(b)) {
    if (typeof a === "bigint" || typeof b === "bigint") {
      try { return BigInt(a) === BigInt(b); } catch (e) { return toNum(a) === toNum(b); }
    }
    return toNum(a) === toNum(b);
  }
  return deepEq(a, tagged(b));
}
function isPyInt(x) { return typeof x === "bigint" || (typeof x === "number" && Number.isInteger(x)) || typeof x === "boolean"; }
function isStrObj(v) { return typeof v === "string"; }
function isDictLike(v) {
  return v instanceof Map || (v !== null && typeof v === "object" && !Array.isArray(v) && !(v instanceof Set) &&
    !isNumLike(v) && !ArrayBuffer.isView(v) && Object.getPrototypeOf(v) === Object.prototype);
}
function pyIter(x) {
  if (Array.isArray(x)) return x;
  if (typeof x === "string") return Array.from(x);
  if (x instanceof Set) return Array.from(x);
  if (x instanceof Map) return Array.from(x.keys());
  if (x !== null && typeof x === "object" && ArrayBuffer.isView(x)) return Array.from(x);
  if (isDictLike(x)) return Object.keys(x);
  if (x !== null && x !== undefined && typeof x[Symbol.iterator] === "function") return Array.from(x);
  throw pyErr("TypeError", "object is not iterable: " + fmt(x));
}
function pyLen(x) {
  if (typeof x === "string") return Array.from(x).length;
  if (Array.isArray(x) || ArrayBuffer.isView(x)) return x.length;
  if (x instanceof Map || x instanceof Set) return x.size;
  if (isDictLike(x)) return Object.keys(x).length;
  throw pyErr("TypeError", "object has no len(): " + fmt(x));
}
function numArgs(a, b) {
  if (typeof a === "boolean") a = Number(a);
  if (typeof b === "boolean") b = Number(b);
  if (typeof a === "bigint" || typeof b === "bigint") {
    if (isPyInt(a) && isPyInt(b)) return [BigInt(a), BigInt(b), true];
    return [toNum(a), toNum(b), false];
  }
  if (!isNumLike(a) || !isNumLike(b)) return null;
  return [toNum(a), toNum(b), false];
}
function normInt(v) {
  if (typeof v === "bigint" && v <= BigInt(Number.MAX_SAFE_INTEGER) && v >= -BigInt(Number.MAX_SAFE_INTEGER)) return Number(v);
  return v;
}
function bigOrNum(a, b, f) {
  // integer op that must not lose precision
  if (Number.isInteger(a) && Number.isInteger(b)) {
    const r = f(BigInt(a), BigInt(b));
    return normInt(r);
  }
  throw pyErr("TypeError", "unsupported operand types");
}
function pySetOf(items) {
  const out = [];
  for (const x of items) if (!out.some((y) => pyEq(x, y))) out.push(x);
  return new Set(out);
}
function pyRepeat(seq, n) {
  n = toNum(n);
  if (typeof seq === "string") return n > 0 ? seq.repeat(n) : "";
  let out = [];
  for (let i = 0; i < n; i++) out = out.concat(seq);
  return out;
}
function pyFloorDiv(a, b) {
  if (typeof a === "bigint") {
    if (b === 0n) throw pyErr("ZeroDivisionError", "integer division or modulo by zero");
    let q = a / b;
    if ((a % b !== 0n) && ((a < 0n) !== (b < 0n))) q -= 1n;
    return normInt(q);
  }
  if (b === 0) throw pyErr("ZeroDivisionError", "division by zero");
  return Math.floor(a / b);
}
function pyMod(a, b) {
  if (typeof a === "bigint") {
    if (b === 0n) throw pyErr("ZeroDivisionError", "integer division or modulo by zero");
    return normInt(((a % b) + b) % b);
  }
  if (b === 0) throw pyErr("ZeroDivisionError", "modulo by zero");
  const m = a % b;
  return m !== 0 && (m < 0) !== (b < 0) ? m + b : m;
}
function pyBin(op, a, b) {
  const nums = numArgs(a, b);
  switch (op) {
    case "+":
      if (nums) {
        const [x, y, big] = nums;
        if (big) return normInt(x + y);
        const r = x + y;
        if (Number.isInteger(x) && Number.isInteger(y) && !Number.isSafeInteger(r)) return bigOrNum(x, y, (p, q) => p + q);
        return r;
      }
      if (typeof a === "string" && typeof b === "string") return a + b;
      if (Array.isArray(a) && Array.isArray(b)) return a.concat(b);
      break;
    case "-":
      if (nums) {
        const [x, y, big] = nums;
        if (big) return normInt(x - y);
        const r = x - y;
        if (Number.isInteger(x) && Number.isInteger(y) && !Number.isSafeInteger(r)) return bigOrNum(x, y, (p, q) => p - q);
        return r;
      }
      if (a instanceof Set && b instanceof Set) return new Set(Array.from(a).filter((x) => !Array.from(b).some((y) => pyEq(x, y))));
      break;
    case "*":
      if (nums) {
        const [x, y, big] = nums;
        if (big) return normInt(x * y);
        const r = x * y;
        if (Number.isInteger(x) && Number.isInteger(y) && !Number.isSafeInteger(r)) return bigOrNum(x, y, (p, q) => p * q);
        return r;
      }
      if ((typeof a === "string" || Array.isArray(a)) && isPyInt(b)) return pyRepeat(a, b);
      if ((typeof b === "string" || Array.isArray(b)) && isPyInt(a)) return pyRepeat(b, a);
      break;
    case "/":
      if (nums) {
        const x = Number(nums[0]), y = Number(nums[1]);
        if (y === 0) throw pyErr("ZeroDivisionError", "division by zero");
        return x / y;
      }
      break;
    case "//": if (nums) return pyFloorDiv(nums[0], nums[1]); break;
    case "%":
      if (nums) return pyMod(nums[0], nums[1]);
      break;
    case "**":
      if (nums) {
        const [x, y, big] = nums;
        if (big) return y < 0n ? Number(x) ** Number(y) : normInt(x ** y);
        const r = x ** y;
        if (Number.isInteger(x) && Number.isInteger(y) && y >= 0 && !Number.isSafeInteger(r)) return normInt(BigInt(x) ** BigInt(y));
        return r;
      }
      break;
    case "&": case "|": case "^":
      if (a instanceof Set && b instanceof Set) {
        const A = Array.from(a), B = Array.from(b);
        const inB = (x) => B.some((y) => pyEq(x, y)), inA = (x) => A.some((y) => pyEq(x, y));
        if (op === "&") return new Set(A.filter(inB));
        if (op === "|") return pySetOf(A.concat(B));
        return new Set(A.filter((x) => !inB(x)).concat(B.filter((x) => !inA(x))));
      }
      if (isDictLike(a) && isDictLike(b) && op === "|") {
        const m = new Map(a instanceof Map ? a : Object.entries(a));
        for (const [k, v] of (b instanceof Map ? b : Object.entries(b))) m.set(k, v);
        return m;
      }
      if (nums && isPyInt(a) && isPyInt(b)) {
        if (typeof a === "boolean" && typeof b === "boolean") return op === "&" ? a && b : op === "|" ? a || b : a !== b;
        const x = BigInt(nums[0]), y = BigInt(nums[1]);
        return normInt(op === "&" ? x & y : op === "|" ? x | y : x ^ y);
      }
      break;
    case "<<": case ">>":
      if (nums && isPyInt(a) && isPyInt(b)) {
        const x = BigInt(nums[0]), y = BigInt(nums[1]);
        if (y < 0n) throw pyErr("ValueError", "negative shift count");
        return normInt(op === "<<" ? x << y : x >> y);
      }
      break;
  }
  throw pyErr("TypeError", "unsupported operand type(s) for " + op + ": " + fmt(a) + " and " + fmt(b));
}
function pyCmp(a, b) {
  if (typeof a === "boolean") a = Number(a);
  if (typeof b === "boolean") b = Number(b);
  if (isNumLike(a) && isNumLike(b)) {
    if (typeof a === "bigint" || typeof b === "bigint") {
      try { const x = BigInt(a), y = BigInt(b); return x < y ? -1 : x > y ? 1 : 0; } catch (e) {}
    }
    const x = toNum(a), y = toNum(b);
    return x < y ? -1 : x > y ? 1 : 0;
  }
  if (typeof a === "string" && typeof b === "string") {
    const A = Array.from(a), B = Array.from(b);
    for (let i = 0; i < Math.min(A.length, B.length); i++) {
      const x = A[i].codePointAt(0), y = B[i].codePointAt(0);
      if (x !== y) return x < y ? -1 : 1;
    }
    return A.length - B.length < 0 ? -1 : A.length > B.length ? 1 : 0;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      if (!pyEq(a[i], b[i])) return pyCmp(a[i], b[i]);
    }
    return a.length < b.length ? -1 : a.length > b.length ? 1 : 0;
  }
  if (a instanceof Set && b instanceof Set) {
    const A = Array.from(a), B = Array.from(b);
    const sub = A.every((x) => B.some((y) => pyEq(x, y))), sup = B.every((x) => A.some((y) => pyEq(x, y)));
    if (sub && sup) return 0;
    if (sub) return -1;
    if (sup) return 1;
    return NaN;
  }
  throw pyErr("TypeError", "'<' not supported between " + fmt(a) + " and " + fmt(b));
}
function pyContains(c, x) {
  if (typeof c === "string") {
    if (typeof x !== "string") throw pyErr("TypeError", "'in <string>' requires string as left operand");
    return c.includes(x);
  }
  if (c instanceof Map) {
    if (c.has(x)) return true;
    for (const k of c.keys()) if (pyEq(k, x)) return true;
    return false;
  }
  if (isDictLike(c)) {
    if (x === null || typeof x === "object") return false;
    return Object.prototype.hasOwnProperty.call(c, typeof x === "boolean" ? String(x) : String(x));
  }
  return pyIter(c).some((y) => pyEq(y, x));
}
function pyIndex(obj, k) {
  if (typeof obj === "string" || Array.isArray(obj) || ArrayBuffer.isView(obj)) {
    const seq = typeof obj === "string" ? Array.from(obj) : obj;
    if (!isPyInt(k)) throw pyErr("TypeError", "indices must be integers");
    let i = Number(k);
    if (i < 0) i += seq.length;
    if (i < 0 || i >= seq.length) throw pyErr("IndexError", "index out of range");
    return seq[i];
  }
  if (obj instanceof Map) {
    if (obj.has(k)) return obj.get(k);
    for (const [kk, v] of obj) if (pyEq(kk, k)) return v;
    throw pyErr("KeyError", fmt(k));
  }
  if (isDictLike(obj)) {
    for (const ks of keyStrings(k)) if (Object.prototype.hasOwnProperty.call(obj, ks)) return obj[ks];
    throw pyErr("KeyError", fmt(k));
  }
  if (obj !== null && obj !== undefined && (typeof k === "string" || typeof k === "number") && k in Object(obj)) return obj[k];
  throw pyErr("TypeError", "object is not subscriptable: " + fmt(obj));
}
function pySlice(obj, lo, hi, step) {
  const isStr = typeof obj === "string";
  const seq = isStr ? Array.from(obj) : Array.isArray(obj) ? obj : ArrayBuffer.isView(obj) ? Array.from(obj) : null;
  if (seq === null) throw pyErr("TypeError", "object is not sliceable: " + fmt(obj));
  const n = seq.length;
  const st = step === null || step === undefined ? 1 : Number(step);
  if (st === 0) throw pyErr("ValueError", "slice step cannot be zero");
  const norm = (v, dflt, lowB, highB) => {
    if (v === null || v === undefined) return dflt;
    v = Number(v);
    if (v < 0) v += n;
    return Math.min(Math.max(v, lowB), highB);
  };
  const out = [];
  if (st > 0) {
    const s = norm(lo, 0, 0, n), e = norm(hi, n, 0, n);
    for (let i = s; i < e; i += st) out.push(seq[i]);
  } else {
    const s = norm(lo, n - 1, -1, n - 1), e = norm(hi, -1, -1, n - 1);
    for (let i = s; i > e; i += st) out.push(seq[i]);
  }
  return isStr ? out.join("") : out;
}
function pyFloatRepr(x) {
  if (Number.isNaN(x)) return "nan";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  if (Number.isInteger(x) && Math.abs(x) < 1e16) return (Object.is(x, -0) ? "-0" : String(x)) + ".0";
  let s = String(x);
  if (Math.abs(x) >= 1e16 && !s.includes("e")) s = x.toExponential();
  return s.replace(/e([+-])(\d)$/, "e$10$2").replace(/^(-?\d+)e/, "$1.0e").replace(/\.0e/, "e");
}
function pyRepr(v) {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : pyFloatRepr(v);
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "string") {
    const q = v.includes("'") && !v.includes('"') ? '"' : "'";
    let out = q;
    for (const ch of v) {
      if (ch === "\\") out += "\\\\";
      else if (ch === q) out += "\\" + q;
      else if (ch === "\n") out += "\\n";
      else if (ch === "\r") out += "\\r";
      else if (ch === "\t") out += "\\t";
      else if (ch.codePointAt(0) < 32 || ch.codePointAt(0) === 127) out += "\\x" + ch.codePointAt(0).toString(16).padStart(2, "0");
      else out += ch;
    }
    return out + q;
  }
  if (Array.isArray(v)) return "[" + v.map(pyRepr).join(", ") + "]";
  if (v instanceof Set) return v.size ? "{" + Array.from(v).map(pyRepr).join(", ") + "}" : "set()";
  if (v instanceof Map) return "{" + Array.from(v.entries()).map(([k, x]) => pyRepr(k) + ": " + pyRepr(x)).join(", ") + "}";
  if (isDictLike(v)) return "{" + Object.keys(v).map((k) => pyRepr(k) + ": " + pyRepr(v[k])).join(", ") + "}";
  return String(v);
}
function pyStr(v) { return typeof v === "string" ? v : pyRepr(v); }
function pyRound(x, nd) {
  const roundHalfEven = (y) => {
    const f = Math.floor(y), d = y - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  };
  if (typeof x === "bigint") return x;
  x = toNum(x);
  if (nd === undefined || nd === null) {
    if (!Number.isFinite(x)) throw pyErr("OverflowError", "cannot convert float infinity to integer");
    return roundHalfEven(x);
  }
  nd = Number(nd);
  if (!Number.isFinite(x)) return x;
  const p = 10 ** nd;
  const scaled = Number((x * p).toPrecision(15));
  return roundHalfEven(scaled) / p;
}
function pyInt(x, base) {
  if (base !== undefined) {
    const s = String(x).trim().replace(/_/g, "").toLowerCase();
    const b = Number(base);
    let body = s.replace(/^[+-]/, "");
    if ((b === 16 && body.startsWith("0x")) || (b === 8 && body.startsWith("0o")) || (b === 2 && body.startsWith("0b"))) body = body.slice(2);
    const digits = "0123456789abcdefghijklmnopqrstuvwxyz".slice(0, b);
    if (!body || [...body].some((c) => !digits.includes(c))) throw pyErr("ValueError", "invalid literal for int()");
    let r = 0n;
    for (const c of body) r = r * BigInt(b) + BigInt(digits.indexOf(c));
    return normInt(s.startsWith("-") ? -r : r);
  }
  if (typeof x === "boolean") return Number(x);
  if (typeof x === "bigint") return x;
  if (typeof x === "string") {
    const s = x.trim().replace(/_/g, "");
    if (!/^[+-]?\d+$/.test(s)) throw pyErr("ValueError", "invalid literal for int() with base 10: " + pyRepr(x));
    return normInt(BigInt(s));
  }
  if (isNumLike(x)) {
    const n = toNum(x);
    if (!Number.isFinite(n)) throw pyErr(Number.isNaN(n) ? "ValueError" : "OverflowError", "cannot convert float to integer");
    return Math.trunc(n);
  }
  throw pyErr("TypeError", "int() argument must be a string or a number");
}
function pyFloat(x) {
  if (typeof x === "string") {
    const s = x.trim().toLowerCase().replace(/_/g, "");
    if (/^[+-]?(inf|infinity)$/.test(s)) return s.startsWith("-") ? -Infinity : Infinity;
    if (/^[+-]?nan$/.test(s)) return NaN;
    if (s === "" || Number.isNaN(Number(s))) throw pyErr("ValueError", "could not convert string to float: " + pyRepr(x));
    return Number(s);
  }
  if (typeof x === "boolean") return Number(x);
  if (isNumLike(x)) return toNum(x);
  throw pyErr("TypeError", "float() argument must be a string or a number");
}
function pyCall(f, args) {
  if (typeof f !== "function") throw pyErr("TypeError", "object is not callable");
  return f(...args);
}
function pySorted(items, key, reverse) {
  const arr = pyIter(items).slice();
  const keys = arr.map((x) => (key ? pyCall(key, [x]) : x));
  const idx = arr.map((_, i) => i);
  idx.sort((i, j) => {
    const c = pyCmp(keys[i], keys[j]);
    return reverse ? -c : c;
  });
  return idx.map((i) => arr[i]);
}
function pyMinMax(isMax, args, kw) {
  let items = args.length === 1 ? pyIter(args[0]) : args;
  const key = kw.key || null;
  if (!items.length) {
    if ("default" in kw) return kw.default;
    throw pyErr("ValueError", (isMax ? "max" : "min") + "() arg is an empty sequence");
  }
  let best = items[0], bk = key ? pyCall(key, [best]) : best;
  for (const x of items.slice(1)) {
    const k = key ? pyCall(key, [x]) : x;
    const c = pyCmp(k, bk);
    if (isMax ? c > 0 : c < 0) { best = x; bk = k; }
  }
  return best;
}
function pyDict(x) {
  if (x === undefined) return new Map();
  if (x instanceof Map) return new Map(x);
  if (isDictLike(x)) return new Map(Object.entries(x));
  const m = new Map();
  for (const p of pyIter(x)) { const [k, v] = pyIter(p); m.set(k, v); }
  return m;
}
function pyCounter(x) {
  const m = new Map();
  if (x === undefined) return m;
  if (x instanceof Map || isDictLike(x)) return pyDict(x);
  for (const item of pyIter(x)) {
    let found = false;
    for (const k of m.keys()) if (pyEq(k, item)) { m.set(k, m.get(k) + 1); found = true; break; }
    if (!found) m.set(item, 1);
  }
  return m;
}
function pyIsclose(a, b, kw) {
  const rel = kw.rel_tol === undefined ? 1e-9 : toNum(kw.rel_tol);
  const abs = kw.abs_tol === undefined ? 0 : toNum(kw.abs_tol);
  const x = toNum(a), y = toNum(b);
  if (x === y) return true;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  return Math.abs(x - y) <= Math.max(rel * Math.max(Math.abs(x), Math.abs(y)), abs);
}
function pyRange(a, b, c) {
  let start = 0, stop, step = 1;
  if (b === undefined) stop = Number(a); else { start = Number(a); stop = Number(b); if (c !== undefined) step = Number(c); }
  if (step === 0) throw pyErr("ValueError", "range() arg 3 must not be zero");
  const out = [];
  if (step > 0) for (let i = start; i < stop; i += step) out.push(i);
  else for (let i = start; i > stop; i += step) out.push(i);
  return out;
}
function pySum(args, kw) {
  let acc = args.length > 1 ? args[1] : kw.start !== undefined ? kw.start : 0;
  for (const x of pyIter(args[0])) acc = pyBin("+", acc, x);
  return acc;
}
function mathFn(f) { return (x) => { const r = f(toNum(x)); if (Number.isNaN(r) && !Number.isNaN(toNum(x))) throw pyErr("ValueError", "math domain error"); return r; }; }
const PY_FUNCS = {
  len: (a) => pyLen(a[0]),
  sorted: (a, kw) => pySorted(a[0], kw.key || null, pyTruthy(kw.reverse)),
  reversed: (a) => pyIter(a[0]).slice().reverse(),
  list: (a) => (a.length ? pyIter(a[0]).slice() : []),
  tuple: (a) => (a.length ? pyIter(a[0]).slice() : []),
  set: (a) => (a.length ? pySetOf(pyIter(a[0])) : new Set()),
  frozenset: (a) => (a.length ? pySetOf(pyIter(a[0])) : new Set()),
  dict: (a, kw) => { const m = pyDict(a[0]); for (const k of Object.keys(kw)) m.set(k, kw[k]); return m; },
  str: (a) => (a.length ? pyStr(a[0]) : ""),
  repr: (a) => pyRepr(a[0]),
  int: (a) => (a.length ? pyInt(a[0], a[1]) : 0),
  float: (a) => (a.length ? pyFloat(a[0]) : 0),
  bool: (a) => (a.length ? pyTruthy(a[0]) : false),
  abs: (a) => { const x = a[0]; if (typeof x === "bigint") return x < 0n ? -x : x; if (!isNumLike(x) && typeof x !== "boolean") throw pyErr("TypeError", "bad operand type for abs()"); return Math.abs(toNum(x)); },
  round: (a, kw) => pyRound(a[0], a.length > 1 ? a[1] : kw.ndigits),
  min: (a, kw) => pyMinMax(false, a, kw),
  max: (a, kw) => pyMinMax(true, a, kw),
  sum: (a, kw) => pySum(a, kw),
  any: (a) => pyIter(a[0]).some(pyTruthy),
  all: (a) => pyIter(a[0]).every(pyTruthy),
  enumerate: (a, kw) => { const s = Number(a.length > 1 ? a[1] : kw.start || 0); return pyIter(a[0]).map((x, i) => [i + s, x]); },
  zip: (a) => { const its = a.map(pyIter); const n = its.length ? Math.min(...its.map((x) => x.length)) : 0; const out = []; for (let i = 0; i < n; i++) out.push(its.map((x) => x[i])); return out; },
  range: (a) => pyRange(a[0], a[1], a[2]),
  map: (a) => { const its = a.slice(1).map(pyIter); const n = Math.min(...its.map((x) => x.length)); const out = []; for (let i = 0; i < n; i++) out.push(pyCall(a[0], its.map((x) => x[i]))); return out; },
  filter: (a) => pyIter(a[1]).filter((x) => (a[0] === null ? pyTruthy(x) : pyTruthy(pyCall(a[0], [x])))),
  chr: (a) => String.fromCodePoint(Number(a[0])),
  ord: (a) => { const s = Array.from(a[0]); if (s.length !== 1) throw pyErr("TypeError", "ord() expected a character"); return s[0].codePointAt(0); },
  divmod: (a) => [pyBin("//", a[0], a[1]), pyBin("%", a[0], a[1])],
  pow: (a) => (a.length > 2 ? normInt((() => { let b = BigInt(a[0]), e = BigInt(a[1]), m = BigInt(a[2]), r = 1n; b %= m; while (e > 0n) { if (e & 1n) r = (r * b) % m; b = (b * b) % m; e >>= 1n; } return ((r % m) + m) % m; })()) : pyBin("**", a[0], a[1])),
  Counter: (a) => pyCounter(a[0]),
  isclose: (a, kw) => pyIsclose(a[0], a[1], kw),
  "math.isclose": (a, kw) => pyIsclose(a[0], a[1], kw),
  "math.sqrt": (a) => mathFn(Math.sqrt)(a[0]),
  "math.floor": (a) => (typeof a[0] === "bigint" ? a[0] : Math.floor(toNum(a[0]))),
  "math.ceil": (a) => (typeof a[0] === "bigint" ? a[0] : Math.ceil(toNum(a[0]))),
  "math.trunc": (a) => (typeof a[0] === "bigint" ? a[0] : Math.trunc(toNum(a[0]))),
  "math.fabs": (a) => Math.abs(toNum(a[0])),
  "math.log": (a) => { const x = toNum(a[0]); if (x <= 0) throw pyErr("ValueError", "math domain error"); return a.length > 1 ? Math.log(x) / Math.log(toNum(a[1])) : Math.log(x); },
  "math.log2": (a) => { const x = toNum(a[0]); if (x <= 0) throw pyErr("ValueError", "math domain error"); return Math.log2(x); },
  "math.log10": (a) => { const x = toNum(a[0]); if (x <= 0) throw pyErr("ValueError", "math domain error"); return Math.log10(x); },
  "math.exp": (a) => Math.exp(toNum(a[0])),
  "math.isnan": (a) => Number.isNaN(toNum(a[0])),
  "math.isinf": (a) => { const x = toNum(a[0]); return x === Infinity || x === -Infinity; },
  "math.isfinite": (a) => Number.isFinite(toNum(a[0])),
  "math.gcd": (a) => { let r = 0n; for (const x of a) { let p = BigInt(x) < 0n ? -BigInt(x) : BigInt(x); let q = r; while (p) { [q, p] = [p, q % p]; } r = q; } return normInt(r); },
  "math.fsum": (a) => pyIter(a[0]).reduce((s, x) => s + toNum(x), 0),
  "math.prod": (a, kw) => pyIter(a[0]).reduce((s, x) => pyBin("*", s, x), kw.start !== undefined ? kw.start : 1),
  "math.isqrt": (a) => { const n = BigInt(a[0]); if (n < 0n) throw pyErr("ValueError", "isqrt() argument must be nonnegative"); if (n < 2n) return normInt(n); let x = BigInt(Math.floor(Math.sqrt(Number(n)))); while (x * x > n) x -= 1n; while ((x + 1n) * (x + 1n) <= n) x += 1n; return normInt(x); },
  "math.hypot": (a) => Math.hypot(...a.map(toNum)),
};
function asFn(name) { return (...a) => PY_FUNCS[name](a, {}); }
function isUpperStr(s) { return /\p{Lu}|\p{Lt}/u.test(s) && !/\p{Ll}/u.test(s); }
function isLowerStr(s) { return /\p{Ll}/u.test(s) && !/\p{Lu}|\p{Lt}/u.test(s); }
const PY_WS = " \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000";
function pyStrip(s, chars, left, right) {
  const set = chars === undefined || chars === null ? PY_WS : chars;
  const arr = Array.from(s);
  let i = 0, j = arr.length;
  if (left) while (i < j && set.includes(arr[i])) i++;
  if (right) while (j > i && set.includes(arr[j - 1])) j--;
  return arr.slice(i, j).join("");
}
function pySplit(s, sep, maxsplit, fromRight) {
  maxsplit = maxsplit === undefined || maxsplit === null ? -1 : Number(maxsplit);
  if (sep === undefined || sep === null) {
    const parts = [];
    const arr = Array.from(s);
    if (!fromRight) {
      let i = 0;
      while (i < arr.length) {
        while (i < arr.length && PY_WS.includes(arr[i])) i++;
        if (i >= arr.length) break;
        if (maxsplit >= 0 && parts.length === maxsplit) { parts.push(pyStrip(arr.slice(i).join(""), null, false, true)); break; }
        let j = i;
        while (j < arr.length && !PY_WS.includes(arr[j])) j++;
        parts.push(arr.slice(i, j).join(""));
        i = j;
      }
      return parts;
    }
    let j = arr.length;
    while (j > 0) {
      while (j > 0 && PY_WS.includes(arr[j - 1])) j--;
      if (j <= 0) break;
      if (maxsplit >= 0 && parts.length === maxsplit) { parts.unshift(pyStrip(arr.slice(0, j).join(""), null, true, false)); break; }
      let i = j;
      while (i > 0 && !PY_WS.includes(arr[i - 1])) i--;
      parts.unshift(arr.slice(i, j).join(""));
      j = i;
    }
    return parts;
  }
  if (sep === "") throw pyErr("ValueError", "empty separator");
  const all = s.split(sep);
  if (maxsplit < 0 || all.length - 1 <= maxsplit) return all;
  if (!fromRight) return all.slice(0, maxsplit).concat([all.slice(maxsplit).join(sep)]);
  const cut = all.length - maxsplit;
  return [all.slice(0, cut).join(sep)].concat(all.slice(cut));
}
function pyFind(s, sub, start, end, fromRight) {
  const arr = Array.from(s);
  const n = arr.length;
  const norm = (v, d) => { if (v === undefined || v === null) return d; v = Number(v); if (v < 0) v += n; return Math.min(Math.max(v, 0), n); };
  const st = norm(start, 0), en = norm(end, n);
  const hay = arr.slice(st, en).join("");
  const at = fromRight ? hay.lastIndexOf(sub) : hay.indexOf(sub);
  if (at < 0 || en < st) return -1;
  return st + Array.from(hay.slice(0, at)).length;
}
function dictEntries(o) { return o instanceof Map ? Array.from(o.entries()) : Object.keys(o).map((k) => [k, o[k]]); }
function pyMethod(obj, name, a, kw) {
  if (typeof obj === "string") {
    const s = obj;
    switch (name) {
      case "lower": return s.toLowerCase();
      case "upper": return s.toUpperCase();
      case "casefold": return s.toLowerCase();
      case "strip": return pyStrip(s, a[0], true, true);
      case "lstrip": return pyStrip(s, a[0], true, false);
      case "rstrip": return pyStrip(s, a[0], false, true);
      case "split": return pySplit(s, a.length > 0 ? a[0] : kw.sep, a.length > 1 ? a[1] : kw.maxsplit, false);
      case "rsplit": return pySplit(s, a.length > 0 ? a[0] : kw.sep, a.length > 1 ? a[1] : kw.maxsplit, true);
      case "splitlines": { const parts = s.split(/\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]/); if (parts.length && parts[parts.length - 1] === "") parts.pop(); return parts; }
      case "join": { const items = pyIter(a[0]); for (const x of items) if (typeof x !== "string") throw pyErr("TypeError", "sequence item: expected str instance"); return items.join(s); }
      case "startswith": case "endswith": {
        const opts = Array.isArray(a[0]) ? a[0] : [a[0]];
        const sub = pySlice(s, a[1] === undefined ? null : a[1], a[2] === undefined ? null : a[2], null);
        return opts.some((p) => (name === "startswith" ? sub.startsWith(p) : sub.endsWith(p)));
      }
      case "replace": {
        let n = a.length > 2 ? Number(a[2]) : -1;
        if (n < 0) return s.split(a[0]).join(a[1]);
        const parts = s.split(a[0]);
        if (parts.length - 1 <= n) return parts.join(a[1]);
        return parts.slice(0, n + 1).join(a[1]) + a[0] + parts.slice(n + 1).join(a[0]);
      }
      case "count": {
        const sub = pySlice(s, a[1] === undefined ? null : a[1], a[2] === undefined ? null : a[2], null);
        if (a[0] === "") return Array.from(sub).length + 1;
        return sub.split(a[0]).length - 1;
      }
      case "find": return pyFind(s, a[0], a[1], a[2], false);
      case "rfind": return pyFind(s, a[0], a[1], a[2], true);
      case "index": case "rindex": { const r = pyFind(s, a[0], a[1], a[2], name === "rindex"); if (r < 0) throw pyErr("ValueError", "substring not found"); return r; }
      case "isdigit": case "isdecimal": case "isnumeric": return s.length > 0 && /^\p{Nd}+$/u.test(s);
      case "isalpha": return s.length > 0 && /^\p{L}+$/u.test(s);
      case "isalnum": return s.length > 0 && /^[\p{L}\p{Nd}]+$/u.test(s);
      case "isspace": return s.length > 0 && Array.from(s).every((c) => PY_WS.includes(c));
      case "isupper": return isUpperStr(s);
      case "islower": return isLowerStr(s);
      case "title": return s.replace(/\p{L}+/gu, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase());
      case "capitalize": return s.length ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s;
      case "swapcase": return Array.from(s).map((c) => (c === c.toUpperCase() ? c.toLowerCase() : c.toUpperCase())).join("");
      case "zfill": { const w = Number(a[0]); const n = Array.from(s).length; if (n >= w) return s; const sign = s[0] === "+" || s[0] === "-" ? s[0] : ""; return sign + "0".repeat(w - n) + s.slice(sign.length); }
      case "center": case "ljust": case "rjust": {
        const w = Number(a[0]), fill = a.length > 1 ? a[1] : " ";
        const n = Array.from(s).length;
        if (n >= w) return s;
        const pad = w - n;
        if (name === "ljust") return s + fill.repeat(pad);
        if (name === "rjust") return fill.repeat(pad) + s;
        const left = Math.floor(pad / 2) + (pad & w & 1);
        return fill.repeat(left) + s + fill.repeat(pad - left);
      }
      case "partition": case "rpartition": {
        const i = name === "partition" ? s.indexOf(a[0]) : s.lastIndexOf(a[0]);
        if (i < 0) return name === "partition" ? [s, "", ""] : ["", "", s];
        return [s.slice(0, i), a[0], s.slice(i + a[0].length)];
      }
    }
  } else if (Array.isArray(obj)) {
    switch (name) {
      case "count": return obj.filter((x) => pyEq(x, a[0])).length;
      case "index": { const i = obj.findIndex((x) => pyEq(x, a[0])); if (i < 0) throw pyErr("ValueError", "value is not in list"); return i; }
    }
  } else if (obj instanceof Set) {
    const items = Array.from(obj);
    const other = () => a.map((x) => pyIter(x));
    switch (name) {
      case "union": return pySetOf(items.concat(...other()));
      case "intersection": return new Set(items.filter((x) => other().every((o) => o.some((y) => pyEq(x, y)))));
      case "difference": return new Set(items.filter((x) => !other().some((o) => o.some((y) => pyEq(x, y)))));
      case "symmetric_difference": return pyBin("^", obj, pySetOf(pyIter(a[0])));
      case "issubset": { const o = pyIter(a[0]); return items.every((x) => o.some((y) => pyEq(x, y))); }
      case "issuperset": return pyIter(a[0]).every((x) => items.some((y) => pyEq(x, y)));
      case "isdisjoint": { const o = pyIter(a[0]); return !items.some((x) => o.some((y) => pyEq(x, y))); }
    }
  } else if (isDictLike(obj)) {
    switch (name) {
      case "keys": return dictEntries(obj).map((p) => p[0]);
      case "values": return dictEntries(obj).map((p) => p[1]);
      case "items": return dictEntries(obj).map((p) => [p[0], p[1]]);
      case "get": return pyContains(obj, a[0]) ? pyIndex(obj, a[0]) : (a.length > 1 ? a[1] : null);
      case "most_common": {
        const e = dictEntries(obj).map((p, i) => [p, i]);
        e.sort((x, y) => (pyCmp(y[0][1], x[0][1]) || x[1] - y[1]));
        const out = e.map((x) => [x[0][0], x[0][1]]);
        return a.length && a[0] !== null ? out.slice(0, Number(a[0])) : out;
      }
      case "total": return dictEntries(obj).reduce((s, p) => pyBin("+", s, p[1]), 0);
    }
  } else if (isNumLike(obj) || typeof obj === "boolean") {
    switch (name) {
      case "is_integer": return Number.isInteger(toNum(obj));
      case "bit_length": { let x = BigInt(obj); if (x < 0n) x = -x; return x.toString(2).length - (x === 0n ? 1 : 0); }
    }
  }
  if (obj !== null && obj !== undefined && typeof obj[name] === "function") return obj[name](...a);
  throw pyErr("AttributeError", "object has no method " + name);
}
function pyIsinstance(v, names) {
  for (const n of names) {
    switch (n) {
      case "int": if (typeof v === "bigint" || typeof v === "boolean" || (typeof v === "number" && Number.isInteger(v))) return true; break;
      case "float": if (typeof v === "number") return true; break;
      case "bool": if (typeof v === "boolean") return true; break;
      case "str": if (typeof v === "string") return true; break;
      case "list": case "tuple": if (Array.isArray(v)) return true; break;
      case "dict": if (isDictLike(v)) return true; break;
      case "set": case "frozenset": if (v instanceof Set) return true; break;
      case "bytes": if (v instanceof Uint8Array) return true; break;
      case "NoneType": if (v === null || v === undefined) return true; break;
    }
  }
  return false;
}
function pyCmpOp(op, a, b) {
  switch (op) {
    case "eq": return pyEq(a, b);
    case "ne": return !pyEq(a, b);
    case "lt": return pyCmp(a, b) < 0;
    case "le": { if (pyEq(a, b)) return true; return pyCmp(a, b) < 0; }
    case "gt": return pyCmp(a, b) > 0;
    case "ge": { if (pyEq(a, b)) return true; return pyCmp(a, b) > 0; }
    case "in": return pyContains(b, a);
    case "notin": return !pyContains(b, a);
    case "is": return (a === null || a === undefined) && (b === null || b === undefined) ? true : a === b;
    case "isnot": return !((a === null || a === undefined) && (b === null || b === undefined) ? true : a === b);
  }
  throw pyErr("TypeError", "bad comparison");
}
function pyBind(target, value, env) {
  if (target[0] === "name") { env[target[1]] = value; return; }
  const vals = pyIter(value);
  if (vals.length !== target[1].length) throw pyErr("ValueError", "cannot unpack");
  target[1].forEach((t, i) => pyBind(t, vals[i], env));
}
function pyComp(gens, i, env, r, out) {
  if (i === gens.length) { out(env); return; }
  const [target, it, ifs] = gens[i];
  for (const v of pyIter(pyEv(it, r, env))) {
    const e2 = Object.assign(Object.create(null), env);
    pyBind(target, v, e2);
    if (ifs.every((c) => pyTruthy(pyEv(c, r, e2)))) pyComp(gens, i + 1, e2, r, out);
  }
}
function pyKw(list, r, env) {
  const kw = {};
  for (const [, k, v] of list) kw[k] = pyEv(v, r, env);
  return kw;
}
function pyEv(n, r, env) {
  if (env === undefined) env = Object.create(null);
  switch (n[0]) {
    case "r": return r === undefined ? null : r;
    case "arg": return env["\0args"][n[1]];
    case "lit": return n[1];
    case "var": return env[n[1]];
    case "bin": return pyBin(n[1], pyEv(n[2], r, env), pyEv(n[3], r, env));
    case "un": {
      const v = pyEv(n[2], r, env);
      if (n[1] === "not") return !pyTruthy(v);
      if (n[1] === "~") return normInt(~BigInt(v));
      if (typeof v === "boolean") return n[1] === "-" ? -Number(v) : Number(v);
      if (typeof v === "bigint") return n[1] === "-" ? normInt(-v) : v;
      if (!isNumLike(v)) throw pyErr("TypeError", "bad operand type for unary " + n[1]);
      return n[1] === "-" ? -toNum(v) : toNum(v);
    }
    case "cmp": {
      let left = pyEv(n[2][0], r, env);
      for (let i = 0; i < n[1].length; i++) {
        const right = pyEv(n[2][i + 1], r, env);
        if (!pyCmpOp(n[1][i], left, right)) return false;
        left = right;
      }
      return true;
    }
    case "and": { let v = true; for (const c of n[1]) { v = pyEv(c, r, env); if (!pyTruthy(v)) return v; } return v; }
    case "or": { let v = false; for (const c of n[1]) { v = pyEv(c, r, env); if (pyTruthy(v)) return v; } return v; }
    case "ifexp": return pyTruthy(pyEv(n[1], r, env)) ? pyEv(n[2], r, env) : pyEv(n[3], r, env);
    case "sub": return pyIndex(pyEv(n[1], r, env), pyEv(n[2], r, env));
    case "slice": return pySlice(pyEv(n[1], r, env), n[2] === null ? null : pyEv(n[2], r, env), n[3] === null ? null : pyEv(n[3], r, env), n[4] === null ? null : pyEv(n[4], r, env));
    case "attr": {
      const o = pyEv(n[1], r, env);
      if (o === null || o === undefined) throw pyErr("AttributeError", "'NoneType' object has no attribute " + n[2]);
      if (o instanceof Map && o.has(n[2])) return o.get(n[2]);
      const v = o[n[2]];
      if (v === undefined) throw pyErr("AttributeError", "object has no attribute " + n[2]);
      return typeof v === "function" ? v.bind(o) : v;
    }
    case "list": case "tuple": return n[1].map((c) => pyEv(c, r, env));
    case "set": return pySetOf(n[1].map((c) => pyEv(c, r, env)));
    case "dict": { const m = new Map(); for (const [k, v] of n[1]) m.set(pyEv(k, r, env), pyEv(v, r, env)); return m; }
    case "call": {
      const f = PY_FUNCS[n[1]];
      if (!f) throw pyErr("NameError", "unsupported function " + n[1]);
      return f(n[2].map((c) => pyEv(c, r, env)), pyKw(n[3], r, env));
    }
    case "fn": {
      if (!PY_FUNCS[n[1]]) throw pyErr("NameError", "unsupported function " + n[1]);
      return asFn(n[1]);
    }
    case "meth": return pyMethod(pyEv(n[2], r, env), n[1], n[3].map((c) => pyEv(c, r, env)), pyKw(n[4], r, env));
    case "isinst": return pyIsinstance(pyEv(n[1], r, env), n[2]);
    case "lambda": {
      const params = n[1], body = n[2];
      return (...vals) => {
        if (vals.length !== params.length) throw pyErr("TypeError", "lambda takes " + params.length + " arguments");
        const e2 = Object.assign(Object.create(null), env);
        params.forEach((p, i) => { e2[p] = vals[i]; });
        return pyEv(body, r, e2);
      };
    }
    case "comp": {
      const [, kind, elt, gens] = n;
      if (kind === "dict") {
        const m = new Map();
        pyComp(gens, 0, env, r, (e) => {
          const k = pyEv(elt[0], r, e), v = pyEv(elt[1], r, e);
          for (const kk of m.keys()) if (pyEq(kk, k)) { m.set(kk, v); return; }
          m.set(k, v);
        });
        return m;
      }
      const out = [];
      pyComp(gens, 0, env, r, (e) => out.push(pyEv(elt, r, e)));
      return kind === "set" ? pySetOf(out) : out;
    }
  }
  throw pyErr("ValueError", "bad expression node " + n[0]);
}
// ---- end evaluator

const JS_BUILTIN_ERRORS = new Set(["Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "EvalError", "URIError", "AggregateError", "InternalError"]);
const PY_PARENT = {
  ValueError: "Exception", UnicodeError: "ValueError", UnicodeDecodeError: "UnicodeError",
  UnicodeEncodeError: "UnicodeError", JSONDecodeError: "ValueError", KeyError: "LookupError",
  IndexError: "LookupError", LookupError: "Exception", ZeroDivisionError: "ArithmeticError",
  OverflowError: "ArithmeticError", FloatingPointError: "ArithmeticError", ArithmeticError: "Exception",
  TypeError: "Exception", AttributeError: "Exception", NameError: "Exception",
  UnboundLocalError: "NameError", RuntimeError: "Exception", RecursionError: "RuntimeError",
  NotImplementedError: "RuntimeError", AssertionError: "Exception", StopIteration: "Exception",
  OSError: "Exception", IOError: "Exception", FileNotFoundError: "OSError", FileExistsError: "OSError",
  PermissionError: "OSError", IsADirectoryError: "OSError", NotADirectoryError: "OSError",
  TimeoutError: "OSError", ConnectionError: "OSError", EOFError: "Exception", ImportError: "Exception",
  ModuleNotFoundError: "ImportError", MemoryError: "Exception", BufferError: "Exception",
  InvalidOperation: "ArithmeticError", DivisionByZero: "ZeroDivisionError",
};
const JS_TO_PY = {
  TypeError: ["TypeError", "AttributeError", "NotImplementedError"],
  RangeError: ["ValueError", "IndexError", "KeyError", "LookupError", "ArithmeticError", "ZeroDivisionError",
    "OverflowError", "RecursionError", "RuntimeError", "UnicodeError", "InvalidOperation"],
  ReferenceError: ["NameError", "UnboundLocalError"],
  SyntaxError: ["SyntaxError", "ValueError", "JSONDecodeError"],
  URIError: ["ValueError", "UnicodeError"],
  EvalError: ["RuntimeError"],
};

function pyAncestors(name) {
  const out = [];
  let n = name;
  let guard = 0;
  while (n && guard++ < 20) { out.push(n); n = PY_PARENT[n]; }
  return out;
}

function errorChain(err) {
  const chain = [];
  if (err === null || (typeof err !== "object" && typeof err !== "function")) return chain;
  try {
    if (typeof err.name === "string" && err.name) chain.push(err.name);
    let p = Object.getPrototypeOf(err);
    let guard = 0;
    while (p && p !== Object.prototype && guard++ < 50) {
      const c = p.constructor;
      if (c && typeof c.name === "string" && c.name && !chain.includes(c.name)) chain.push(c.name);
      p = Object.getPrototypeOf(p);
    }
  } catch (e) {}
  return chain;
}

function excMatches(err, names) {
  if (!names || names.length === 0) return true;
  const chain = errorChain(err);
  if (chain.length === 0) return true; // thrown primitive: no type information
  for (const c of chain) {
    const anc = pyAncestors(c);
    for (const n of names) if (anc.includes(n)) return true;
  }
  // custom classes named after Python exceptions are authoritative
  for (const c of chain) {
    if (!JS_BUILTIN_ERRORS.has(c) && PY_PARENT[c] !== undefined) return false;
  }
  const base = chain.find((c) => JS_BUILTIN_ERRORS.has(c));
  if (base === undefined || base === "Error" || base === "AggregateError" || base === "InternalError") return true;
  const allowed = JS_TO_PY[base] || [];
  return names.some((n) => allowed.includes(n));
}

function errMessage(err) {
  try {
    if (err !== null && typeof err === "object" && typeof err.message === "string") return err.message;
    return String(err);
  } catch (e) { return ""; }
}

function makeRegex(src, flags, mode) {
  let s = src;
  let f = flags;
  if (mode === "match") f += "y";
  else if (mode === "fullmatch") { s = "(?:" + src + ")$"; f += "y"; }
  try { return new RegExp(s, f); } catch (e) {}
  try { return new RegExp(s, f + "u"); } catch (e) {}
  return null;
}

function checkMsg(msg, chk) {
  const kind = chk[0];
  switch (kind) {
    case "in": return msg.includes(chk[1]);
    case "notin": return !msg.includes(chk[1]);
    case "eq": return msg === chk[1] || "'" + msg + "'" === chk[1];
    case "ne": return !(msg === chk[1] || "'" + msg + "'" === chk[1]);
    case "startswith": return msg.startsWith(chk[1]);
    case "endswith": return msg.endsWith(chk[1]);
  }
  const neg = kind.startsWith("not");
  const mode = neg ? kind.slice(3) : kind;
  const rx = makeRegex(chk[3], chk[4], mode);
  let found;
  if (rx === null) found = msg.includes(chk[1]);
  else { rx.lastIndex = 0; found = rx.test(msg); }
  return found !== neg;
}

function pyTruthy(v) {
  if (v === null || v === undefined || v === false) return false;
  if (typeof v === "number") return v !== 0 && !Number.isNaN(v);
  if (typeof v === "bigint") return v !== 0n;
  if (typeof v === "string") return v.length > 0;
  if (Array.isArray(v)) return v.length > 0;
  if (v instanceof Map || v instanceof Set) return v.size > 0;
  if (typeof v === "object" && isPlainObject(v) && Object.getPrototypeOf(v) === Object.prototype) {
    return Object.keys(v).length > 0;
  }
  return true;
}

function fmt(v) {
  try {
    return JSON.stringify(v, (k, x) => (typeof x === "bigint" ? x.toString() : x === undefined ? null : x));
  } catch (e) {
    return String(v);
  }
}

const origOut = process.stdout.write;
const origErr = process.stderr.write;
function capStart() {
  const buf = { out: [], err: [] };
  const mk = (arr) => function (chunk, enc, cb) {
    if (typeof chunk === "string") arr.push(chunk);
    else if (chunk !== null && chunk !== undefined) {
      try { arr.push(Buffer.from(chunk).toString("utf8")); } catch (e) { arr.push(String(chunk)); }
    }
    const done = typeof enc === "function" ? enc : cb;
    if (typeof done === "function") { try { done(); } catch (e) {} }
    return true;
  };
  process.stdout.write = mk(buf.out);
  process.stderr.write = mk(buf.err);
  return buf;
}
function capStop(buf) {
  process.stdout.write = origOut;
  process.stderr.write = origErr;
  return { stdout: buf.out.join(""), stderr: buf.err.join("") };
}

function isThenable(v) {
  return v !== null && (typeof v === "object" || typeof v === "function") && typeof v.then === "function";
}

async function runOne(t, factory) {
  const args = t.args();
  const xf = t.xf ? t.xf() : null;
  let raised = false, err = null, result, raw;
  const buf = capStart();
  let out;
  try {
    try {
      const fn = factory();
      result = fn(...args);
      if (isThenable(result)) result = await result;
      raw = result;
      if (xf !== null) {
        const env = Object.create(null);
        env["\0args"] = args;
        result = pyEv(xf, raw, env);
      }
    } catch (e) {
      raised = true; err = e;
    }
  } finally {
    out = capStop(buf);
  }
  const extra = t.extra || {};
  if (t.kind === "raises") {
    if (!raised) return [false, "expected an exception, none raised"];
    if (!excMatches(err, extra.exc)) return [false, "raised wrong exception type " + (errorChain(err)[0] || typeof err)];
    const msg = errMessage(err);
    for (const chk of extra.checks || []) {
      if (!checkMsg(msg, chk)) return [false, "exception message " + JSON.stringify(msg) + " does not satisfy " + JSON.stringify(chk.slice(0, 2))];
    }
  } else {
    if (raised) return [false, "raised " + (err && err.stack ? String(err.stack).split("\n")[0] : String(err))];
    let ok;
    const savedTol = { abs: TOL.abs, rel: TOL.rel };
    if (extra.approx) { TOL.abs = extra.approx[0]; TOL.rel = extra.approx[1]; }
    try {
    switch (t.kind) {
      case "eq": ok = deepEq(result, t.expected()); break;
      case "ne": ok = !deepEq(result, t.expected()); break;
      case "truthy": ok = pyTruthy(result); break;
      case "falsy": ok = !pyTruthy(result); break;
      case "is": {
        const e = t.expected();
        ok = e === null ? (result === null || result === undefined) : result === e;
        break;
      }
      case "isnot": {
        const e = t.expected();
        ok = !(e === null ? (result === null || result === undefined) : result === e);
        break;
      }
      case "close":
      case "notclose": {
        const e = t.expected();
        if (!isNumLike(result)) { ok = false; break; }
        ok = closeNum(toNum(result), toNum(e), extra.abs_tol || 0, extra.rel_tol || 0, !!extra.strict);
        if (t.kind === "notclose") ok = !ok;
        break;
      }
      case "lt": case "le": case "gt": case "ge": {
        const e = t.expected();
        let x, y;
        if (isNumLike(result) && isNumLike(e)) {
          if (typeof result === "bigint" && typeof e === "bigint") { x = result; y = e; }
          else { x = toNum(result); y = toNum(e); }
        } else if (typeof result === "string" && typeof e === "string") { x = result; y = e; }
        else {
          try {
            const c = pyCmp(result, plain(e));
            ok = t.kind === "lt" ? c < 0 : t.kind === "le" ? c <= 0 : t.kind === "gt" ? c > 0 : c >= 0;
          } catch (x2) { ok = false; }
          break;
        }
        ok = t.kind === "lt" ? x < y : t.kind === "le" ? x <= y : t.kind === "gt" ? x > y : x >= y;
        break;
      }
      case "in": case "notin": {
        const e = t.expected();
        let found;
        if (typeof e === "string") found = typeof result === "string" && e.includes(result);
        else {
          const items = Array.isArray(e) ? e : e instanceof PySet ? e.items : e instanceof PyDict ? e.entries.map((p) => p[0]) : [];
          found = items.some((x) => deepEq(result, x));
        }
        ok = t.kind === "in" ? found : !found;
        break;
      }
      default: return [false, "unknown test kind"];
    }
    } catch (e) {
      return [false, "comparison failed: " + String(e)];
    } finally {
      TOL.abs = savedTol.abs; TOL.rel = savedTol.rel;
    }
    if (!ok) return [false, "got " + fmt(result) + (xf !== null ? " (entrypoint returned " + fmt(raw) + ")" : "")];
  }
  if (t.stdout !== null && out.stdout !== t.stdout) return [false, "stdout mismatch: " + JSON.stringify(out.stdout)];
  if (t.stderr !== null && out.stderr !== t.stderr) return [false, "stderr mismatch: " + JSON.stringify(out.stderr)];
  return [true, ""];
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.length < 1) {
    process.stderr.write("usage: tester <solution_path> [--results FILE] [--ids FILE] [--tol FLOAT]\n");
    return 2;
  }
  const solution = argv[0];
  let resultsPath = null, idsPath = null;
  for (let i = 1; i < argv.length; i++) {
    if (argv[i] === "--results" && i + 1 < argv.length) resultsPath = argv[++i];
    else if (argv[i] === "--ids" && i + 1 < argv.length) idsPath = argv[++i];
    else if (argv[i] === "--tol" && i + 1 < argv.length) { TOL.abs = Number(argv[++i]); TOL.rel = 0; }
  }
  let selected = TESTS;
  if (idsPath) {
    const wanted = JSON.parse(fs.readFileSync(idsPath, "utf8"));
    const byId = new Map(TESTS.map((t) => [t.id, t]));
    selected = wanted.filter((x) => byId.has(x)).map((x) => byId.get(x));
  }
  let factory;
  const buf = capStart();
  try {
    const loaded = loadSolution(solution, LANG === "typescript");
    factory = resolve(loaded, ENTRYPOINT);
  } catch (e) {
    capStop(buf);
    emit(resultsPath, { event: "load_error", msg: String(e && e.stack ? e.stack : e) });
    if (!resultsPath) origOut.call(process.stdout, JSON.stringify({ status: "error", passed: [], failed: TESTS.map((t) => t.id) }) + "\n");
    return 2;
  }
  capStop(buf);
  const passed = [], failed = [];
  for (const t of selected) {
    emit(resultsPath, { event: "start", id: t.id });
    let ok, msg;
    try {
      [ok, msg] = await runOne(t, factory);
    } catch (e) {
      ok = false; msg = "harness error: " + String(e);
    }
    (ok ? passed : failed).push(t.id);
    emit(resultsPath, { event: "result", id: t.id, ok, msg });
  }
  emit(resultsPath, { event: "done" });
  if (!resultsPath) {
    origOut.call(process.stdout, JSON.stringify({ status: failed.length ? "fail" : "pass", passed, failed }) + "\n");
  }
  return failed.length ? 1 : 0;
}

main().then(
  (code) => { process.exit(code); },
  (e) => { try { origErr.call(process.stderr, String(e) + "\n"); } catch (x) {} process.exit(2); }
);
'''


def ir_src(ir, litfn, lang):
    """Source text of an expression-IR tree (literals rendered with ``litfn``)."""
    if ir is None:
        return "None" if lang == "python" else "null"
    if isinstance(ir, str):
        return repr(ir) if lang == "python" else js_str(ir)
    if isinstance(ir, bool) or isinstance(ir, int):
        return litfn(ir)
    if isinstance(ir, list):
        if len(ir) == 2 and ir[0] == "lit":
            return "[" + ir_src("lit", litfn, lang) + ", " + litfn(ir[1]) + "]"
        return "[" + ", ".join(ir_src(x, litfn, lang) for x in ir) + "]"
    raise GenerateError(f"cannot translate expression node {ir!r}")


def render_python(tests, entrypoint):
    lines = [
        "# Auto-generated by babel_code_goat.py -- do not edit.",
        "# " + META_MARKER + " " + json.dumps({"lang": "python", "entrypoint": entrypoint}),
        "ENTRYPOINT = " + repr(entrypoint),
        "TESTS = [",
    ]
    for t in tests:
        if t["kind"] == "loop":
            continue
        kw = "{" + ", ".join(f"{k!r}: {py_lit(v)}" for k, v in t["kwargs"].items()) + "}"
        exp = py_lit(t["expected"]) if "expected" in t else "None"
        lines.append(
            "    {"
            f"'id': {t['id']!r}, 'kind': {t['kind']!r}, "
            f"'args': lambda: [{', '.join(py_lit(a) for a in t['args'])}], "
            f"'kwargs': lambda: {kw}, "
            f"'expected': lambda: {exp}, 'extra': {py_lit(t.get('extra') or {})}, "
            f"'xf': lambda: {ir_src(t.get('xf'), py_lit, 'python')}, "
            f"'stdout': {t['stdout']!r}, 'stderr': {t['stderr']!r}"
            "},"
        )
    lines.append("]")
    return "\n".join(lines) + "\n" + PY_RUNTIME


def render_js(tests, entrypoint, lang):
    header = ["// Auto-generated by babel_code_goat.py -- do not edit."]
    if lang == "typescript":
        header.append("// @ts-nocheck")
    header.append("// " + META_MARKER + " " + json.dumps({"lang": lang, "entrypoint": entrypoint}))
    if lang == "typescript":
        header.append("type TestCase = { id: string; kind: string; args: () => any[]; expected: () => any; extra: any; xf: () => any; stdout: string | null; stderr: string | null };")
        header.append(f"const LANG: string = {js_str(lang)};")
        header.append(f"const ENTRYPOINT: string = {js_str(entrypoint)};")
        header.append("const TESTS: TestCase[] = [")
    else:
        header.append(f"const LANG = {js_str(lang)};")
        header.append(f"const ENTRYPOINT = {js_str(entrypoint)};")
        header.append("const TESTS = [")
    for t in tests:
        if t["kind"] == "loop":
            continue
        if t["kwargs"]:
            raise GenerateError(f"{t['id']}: keyword arguments cannot be translated to {lang}")
        exp = js_lit(t["expected"], "exp") if "expected" in t else "null"
        so = "null" if t["stdout"] is None else js_str(t["stdout"])
        se = "null" if t["stderr"] is None else js_str(t["stderr"])
        header.append(
            "  { "
            f"id: {js_str(t['id'])}, kind: {js_str(t['kind'])}, "
            f"args: () => [{', '.join(js_lit(a) for a in t['args'])}], "
            f"expected: () => ({exp}), extra: {js_lit(t.get('extra') or {})}, "
            f"xf: () => ({ir_src(t.get('xf'), js_lit, lang)}), stdout: {so}, stderr: {se}"
            " },"
        )
    header.append("];")
    return "\n".join(header) + "\n" + JS_RUNTIME


CPP_RUNTIME = r'''
#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <exception>
#include <fstream>
#include <functional>
#include <future>
#include <iostream>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <optional>
#include <queue>
#include <set>
#include <sstream>
#include <stack>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <typeinfo>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <variant>
#include <vector>
#include <cxxabi.h>
#include <fcntl.h>
#include <unistd.h>

namespace bcg {

// ---- dynamic value (Python value model) --------------------------------------------
// tags: N none, T/F bool, O opaque, i int, f float, d decimal, s str, b bytes,
//       l list, q deque, t tuple, S set, z frozenset, m dict, c Counter, o OrderedDict, D defaultdict
struct Value {
  char tag = 'N';
  std::string s;
  std::vector<Value> items;
  bool is_seq() const { return tag == 'l' || tag == 'q' || tag == 't'; }
  bool is_set() const { return tag == 'S' || tag == 'z'; }
  bool is_dict() const { return tag == 'm' || tag == 'c' || tag == 'o' || tag == 'D'; }
};

inline Value mk(char tag, std::string s = std::string()) {
  Value v;
  v.tag = tag;
  v.s = std::move(s);
  return v;
}

struct ConvError : std::exception {
  std::string m;
  explicit ConvError(std::string msg) : m(std::move(msg)) {}
  const char* what() const noexcept override { return m.c_str(); }
};

inline std::string desc(const Value& v) {
  switch (v.tag) {
    case 'N': return "None";
    case 'T': case 'F': return "bool";
    case 'O': return "object";
    case 'i': return "int " + v.s;
    case 'f': return "float " + v.s;
    case 'd': return "Decimal " + v.s;
    case 's': return "str";
    case 'b': return "bytes";
    case 'l': case 'q': return "list";
    case 't': return "tuple";
    case 'S': case 'z': return "set";
    default: return "dict";
  }
}

inline int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return 0;
}

inline std::string hex(const std::string& s) {
  static const char* d = "0123456789abcdef";
  std::string o;
  o.reserve(s.size() * 2);
  for (unsigned char c : s) { o.push_back(d[c >> 4]); o.push_back(d[c & 15]); }
  return o;
}

struct Parser {
  const char* p;
  Value parse() {
    Value v;
    v.tag = *p++;
    switch (v.tag) {
      case 'N': case 'T': case 'F': case 'O': return v;
      case 'i': case 'f': case 'd': {
        const char* e = std::strchr(p, ';');
        v.s.assign(p, e);
        p = e + 1;
        return v;
      }
      case 's': case 'b': {
        while (*p != ';') { v.s.push_back((char)(hexval(p[0]) * 16 + hexval(p[1]))); p += 2; }
        ++p;
        return v;
      }
      default: {
        ++p;  // '['
        while (*p != ']') v.items.push_back(parse());
        ++p;
        return v;
      }
    }
  }
};

inline Value parse_value(const char* src) { Parser ps{src}; return ps.parse(); }

inline void encode(const Value& v, std::string& o) {
  o.push_back(v.tag);
  switch (v.tag) {
    case 'N': case 'T': case 'F': case 'O': return;
    case 'i': case 'f': case 'd': o += v.s; o.push_back(';'); return;
    case 's': case 'b': o += hex(v.s); o.push_back(';'); return;
    default:
      o.push_back('[');
      for (const auto& x : v.items) encode(x, o);
      o.push_back(']');
  }
}

inline std::string encode(const Value& v) { std::string o; encode(v, o); return o; }

// strict mode: used while choosing a std::variant alternative (exact Python kinds only)
inline bool& strict() { static bool s = false; return s; }
struct StrictGuard {
  bool old;
  explicit StrictGuard(bool v) : old(strict()) { strict() = v; }
  ~StrictGuard() { strict() = old; }
};

inline bool parse_i128(const std::string& s, __int128& out) {
  size_t i = 0;
  bool neg = false;
  if (i < s.size() && (s[i] == '-' || s[i] == '+')) { neg = s[i] == '-'; ++i; }
  if (i >= s.size()) return false;
  unsigned __int128 x = 0;
  const unsigned __int128 lim = ((unsigned __int128)1 << 127);
  for (; i < s.size(); ++i) {
    if (s[i] < '0' || s[i] > '9') return false;
    x = x * 10 + (unsigned)(s[i] - '0');
    if (x > lim) return false;
  }
  if (!neg && x == lim) return false;
  out = neg ? (__int128)(0 - x) : (__int128)x;
  return true;
}

inline std::string i128_str(__int128 x) {
  if (x == 0) return "0";
  bool neg = x < 0;
  unsigned __int128 u = neg ? (unsigned __int128)0 - (unsigned __int128)x : (unsigned __int128)x;
  std::string s;
  while (u) { s.push_back((char)('0' + (int)(u % 10))); u /= 10; }
  if (neg) s.push_back('-');
  std::reverse(s.begin(), s.end());
  return s;
}

inline std::string u128_str(unsigned __int128 u) {
  if (u == 0) return "0";
  std::string s;
  while (u) { s.push_back((char)('0' + (int)(u % 10))); u /= 10; }
  std::reverse(s.begin(), s.end());
  return s;
}

inline long double to_ld(const Value& v) {
  if (v.tag == 'T') return 1;
  if (v.tag == 'F') return 0;
  const std::string& s = v.s;
  std::string t;
  for (char c : s) t.push_back((char)std::tolower((unsigned char)c));
  if (t == "nan" || t == "-nan" || t == "+nan" || t == "snan" || t == "-snan") return std::numeric_limits<long double>::quiet_NaN();
  if (t == "inf" || t == "+inf" || t == "infinity" || t == "+infinity") return std::numeric_limits<long double>::infinity();
  if (t == "-inf" || t == "-infinity") return -std::numeric_limits<long double>::infinity();
  return std::strtold(s.c_str(), nullptr);
}

inline std::string fmt_double(double d) {
  if (std::isnan(d)) return "nan";
  if (std::isinf(d)) return d > 0 ? "inf" : "-inf";
  char buf[64];
  std::snprintf(buf, sizeof buf, "%.17g", d);
  return buf;
}

inline std::string fmt_float(float d) {
  if (std::isnan(d)) return "nan";
  if (std::isinf(d)) return d > 0 ? "inf" : "-inf";
  char buf[64];
  for (int prec = 1; prec <= 9; ++prec) {
    std::snprintf(buf, sizeof buf, "%.*g", prec, (double)d);
    if (std::strtof(buf, nullptr) == d) break;
  }
  return buf;
}

inline std::string fmt_ld(long double d) {
  if (std::isnan(d)) return "nan";
  if (std::isinf(d)) return d > 0 ? "inf" : "-inf";
  char buf[96];
  std::snprintf(buf, sizeof buf, "%.21Lg", d);
  return buf;
}

template <class T>
std::string type_name() {
  int st = 0;
  char* n = abi::__cxa_demangle(typeid(T).name(), nullptr, nullptr, &st);
  std::string r = (st == 0 && n) ? n : typeid(T).name();
  std::free(n);
  return r;
}

// ---- conversions between Value and C++ types ---------------------------------------
template <class T, class = void>
struct Conv {
  static constexpr bool supported = false;
  static T from(const Value&) { throw ConvError("unsupported parameter type " + type_name<T>()); }
  static Value to(const T&) { return mk('O'); }
};

template <class T> T from_value(const Value& v) { return Conv<T>::from(v); }
template <class T> Value to_value(const T& x) { return Conv<T>::to(x); }

template <class T>
struct Conv<T, std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool> && !std::is_same_v<T, char>>> {
  static constexpr bool supported = true;
  static T from(const Value& v) {
    __int128 x = 0;
    if (v.tag == 'i') {
      if (!parse_i128(v.s, x)) throw ConvError("integer " + v.s + " out of range for " + type_name<T>());
    } else if (!strict() && (v.tag == 'T' || v.tag == 'F')) {
      x = v.tag == 'T';
    } else if (!strict() && (v.tag == 'f' || v.tag == 'd')) {
      long double d = to_ld(v);
      if (!std::isfinite(d) || d != std::floor(d) || std::fabs(d) > 1.7e38L) throw ConvError("expected int, got " + desc(v));
      x = (__int128)d;
    } else {
      throw ConvError("expected int, got " + desc(v));
    }
    if constexpr (sizeof(T) < 16) {
      if (x < (__int128)std::numeric_limits<T>::min() || x > (__int128)std::numeric_limits<T>::max())
        throw ConvError("integer " + i128_str(x) + " out of range for " + type_name<T>());
    } else if constexpr (std::is_unsigned_v<T>) {
      if (x < 0) throw ConvError("negative integer for unsigned type");
    }
    return (T)x;
  }
  static Value to(const T& x) {
    if constexpr (std::is_unsigned_v<T>) return mk('i', u128_str((unsigned __int128)x));
    else return mk('i', i128_str((__int128)x));
  }
};

template <>
struct Conv<bool> {
  static constexpr bool supported = true;
  static bool from(const Value& v) {
    if (v.tag == 'T') return true;
    if (v.tag == 'F') return false;
    if (!strict() && v.tag == 'i') return v.s != "0";
    throw ConvError("expected bool, got " + desc(v));
  }
  static Value to(const bool& b) { return mk(b ? 'T' : 'F'); }
};

template <>
struct Conv<char> {
  static constexpr bool supported = true;
  static char from(const Value& v) {
    if ((v.tag == 's' || v.tag == 'b') && v.s.size() == 1) return v.s[0];
    if (!strict() && v.tag == 'i') return (char)Conv<int>::from(v);
    throw ConvError("expected single character, got " + desc(v));
  }
  static Value to(const char& c) { return mk('s', std::string(1, c)); }
};

template <class T>
struct Conv<T, std::enable_if_t<std::is_floating_point_v<T>>> {
  static constexpr bool supported = true;
  static T from(const Value& v) {
    if (v.tag == 'f' || v.tag == 'd') return (T)to_ld(v);
    if (v.tag == 'i') {
      if (strict()) throw ConvError("expected float, got int");
      return (T)std::strtold(v.s.c_str(), nullptr);
    }
    if (!strict() && (v.tag == 'T' || v.tag == 'F')) return v.tag == 'T' ? 1 : 0;
    throw ConvError("expected float, got " + desc(v));
  }
  static Value to(const T& x) {
    if constexpr (std::is_same_v<T, long double>) return mk('d', fmt_ld(x));
    else if constexpr (std::is_same_v<T, float>) return mk('f', fmt_float(x));
    else return mk('f', fmt_double((double)x));
  }
};

template <>
struct Conv<std::string> {
  static constexpr bool supported = true;
  static std::string from(const Value& v) {
    if (v.tag == 's' || (!strict() && v.tag == 'b')) return v.s;
    throw ConvError("expected str, got " + desc(v));
  }
  static Value to(const std::string& s) { return mk('s', s); }
};

template <>
struct Conv<std::string_view> {
  static constexpr bool supported = true;
  static std::string_view from(const Value& v) { return *new std::string(Conv<std::string>::from(v)); }
  static Value to(const std::string_view& s) { return mk('s', std::string(s)); }
};

template <>
struct Conv<std::nullptr_t> {
  static constexpr bool supported = true;
  static std::nullptr_t from(const Value& v) {
    if (v.tag == 'N') return nullptr;
    throw ConvError("expected None, got " + desc(v));
  }
  static Value to(const std::nullptr_t&) { return mk('N'); }
};

template <>
struct Conv<std::monostate> {
  static constexpr bool supported = true;
  static std::monostate from(const Value& v) {
    if (v.tag == 'N') return {};
    throw ConvError("expected None, got " + desc(v));
  }
  static Value to(const std::monostate&) { return mk('N'); }
};

template <class T>
struct Conv<T*> {
  static constexpr bool supported = true;
  using E = std::remove_cv_t<T>;
  static T* from(const Value& v) {
    if (v.tag == 'N') return nullptr;
    if constexpr (std::is_same_v<E, char>) {
      std::string s = Conv<std::string>::from(v);
      char* p = (char*)std::malloc(s.size() + 1);
      std::memcpy(p, s.c_str(), s.size() + 1);
      return p;
    } else if constexpr (Conv<E>::supported && std::is_copy_constructible_v<E>) {
      return new E(Conv<E>::from(v));
    } else {
      throw ConvError("unsupported parameter type " + type_name<T*>());
    }
  }
  static Value to(T* const& p) {
    if (!p) return mk('N');
    if constexpr (std::is_same_v<E, char>) return mk('s', std::string(p));
    else if constexpr (Conv<E>::supported) return Conv<E>::to(*p);
    else return mk('O');
  }
};

template <class T>
struct Conv<std::optional<T>> {
  static constexpr bool supported = true;
  static std::optional<T> from(const Value& v) {
    if (v.tag == 'N') return std::nullopt;
    return std::optional<T>(Conv<T>::from(v));
  }
  static Value to(const std::optional<T>& o) { return o ? Conv<T>::to(*o) : mk('N'); }
};

template <class T>
struct Conv<std::shared_ptr<T>> {
  static constexpr bool supported = true;
  static std::shared_ptr<T> from(const Value& v) {
    if (v.tag == 'N') return nullptr;
    if constexpr (Conv<T>::supported) return std::make_shared<T>(Conv<T>::from(v));
    else throw ConvError("unsupported parameter type " + type_name<T>());
  }
  static Value to(const std::shared_ptr<T>& p) {
    if (!p) return mk('N');
    if constexpr (Conv<T>::supported) return Conv<T>::to(*p);
    else return mk('O');
  }
};

template <class T, class D>
struct Conv<std::unique_ptr<T, D>> {
  static constexpr bool supported = true;
  static std::unique_ptr<T, D> from(const Value& v) {
    if (v.tag == 'N') return nullptr;
    if constexpr (Conv<T>::supported) return std::unique_ptr<T, D>(new T(Conv<T>::from(v)));
    else throw ConvError("unsupported parameter type " + type_name<T>());
  }
  static Value to(const std::unique_ptr<T, D>& p) {
    if (!p) return mk('N');
    if constexpr (Conv<T>::supported) return Conv<T>::to(*p);
    else return mk('O');
  }
};

// sequence elements of a Value (lenient: sets, bytes, and strings as sequences)
template <class E>
std::vector<const Value*> seq_items(const Value& v, std::vector<Value>& tmp) {
  std::vector<const Value*> out;
  if (v.is_seq() || (!strict() && v.is_set())) {
    for (const auto& x : v.items) out.push_back(&x);
    return out;
  }
  if (!strict() && v.tag == 'b') {
    tmp.clear();
    for (unsigned char c : v.s) tmp.push_back(mk('i', std::to_string((int)c)));
    for (const auto& x : tmp) out.push_back(&x);
    return out;
  }
  if (!strict() && v.tag == 's' && (std::is_same_v<E, char> || std::is_same_v<E, std::string>)) {
    tmp.clear();
    for (char c : v.s) tmp.push_back(mk('s', std::string(1, c)));
    for (const auto& x : tmp) out.push_back(&x);
    return out;
  }
  throw ConvError("expected list, got " + desc(v));
}

template <class C, class E>
struct SeqConv {
  static constexpr bool supported = true;
  static C from(const Value& v) {
    std::vector<Value> tmp;
    C c;
    for (const Value* x : seq_items<E>(v, tmp)) c.push_back(Conv<E>::from(*x));
    return c;
  }
  static Value to(const C& c) {
    Value r = mk('l');
    for (const auto& x : c) r.items.push_back(Conv<E>::to(x));
    return r;
  }
};

template <class T, class A> struct Conv<std::vector<T, A>> : SeqConv<std::vector<T, A>, T> {};
template <class T, class A> struct Conv<std::deque<T, A>> : SeqConv<std::deque<T, A>, T> {};
template <class T, class A> struct Conv<std::list<T, A>> : SeqConv<std::list<T, A>, T> {};

template <class A>
struct Conv<std::vector<bool, A>> {
  static constexpr bool supported = true;
  static std::vector<bool, A> from(const Value& v) {
    std::vector<Value> tmp;
    std::vector<bool, A> c;
    for (const Value* x : seq_items<bool>(v, tmp)) c.push_back(Conv<bool>::from(*x));
    return c;
  }
  static Value to(const std::vector<bool, A>& c) {
    Value r = mk('l');
    for (bool x : c) r.items.push_back(Conv<bool>::to(x));
    return r;
  }
};

template <class T, size_t N>
struct Conv<std::array<T, N>> {
  static constexpr bool supported = true;
  static std::array<T, N> from(const Value& v) {
    std::vector<Value> tmp;
    auto items = seq_items<T>(v, tmp);
    if (items.size() != N) throw ConvError("expected " + std::to_string(N) + " items");
    std::array<T, N> a{};
    for (size_t i = 0; i < N; ++i) a[i] = Conv<T>::from(*items[i]);
    return a;
  }
  static Value to(const std::array<T, N>& c) {
    Value r = mk('l');
    for (const auto& x : c) r.items.push_back(Conv<T>::to(x));
    return r;
  }
};

template <class C, class E>
struct SetConv {
  static constexpr bool supported = true;
  static C from(const Value& v) {
    C c;
    if (v.is_set() || (!strict() && v.is_seq())) {
      for (const auto& x : v.items) c.insert(Conv<E>::from(x));
      return c;
    }
    throw ConvError("expected set, got " + desc(v));
  }
  static Value to(const C& c) {
    Value r = mk('S');
    for (const auto& x : c) r.items.push_back(Conv<E>::to(x));
    return r;
  }
};

template <class T, class Cm, class A> struct Conv<std::set<T, Cm, A>> : SetConv<std::set<T, Cm, A>, T> {};
template <class T, class Cm, class A> struct Conv<std::multiset<T, Cm, A>> : SetConv<std::multiset<T, Cm, A>, T> {
  static Value to(const std::multiset<T, Cm, A>& c) {
    Value r = mk('l');
    for (const auto& x : c) r.items.push_back(Conv<T>::to(x));
    return r;
  }
};
template <class T, class H, class Eq, class A> struct Conv<std::unordered_set<T, H, Eq, A>> : SetConv<std::unordered_set<T, H, Eq, A>, T> {};
template <class T, class H, class Eq, class A> struct Conv<std::unordered_multiset<T, H, Eq, A>> : SetConv<std::unordered_multiset<T, H, Eq, A>, T> {
  static Value to(const std::unordered_multiset<T, H, Eq, A>& c) {
    Value r = mk('l');
    for (const auto& x : c) r.items.push_back(Conv<T>::to(x));
    return r;
  }
};

template <class C, class K, class V>
struct MapConv {
  static constexpr bool supported = true;
  static C from(const Value& v) {
    if (!v.is_dict()) throw ConvError("expected dict, got " + desc(v));
    C c;
    for (size_t i = 0; i + 1 < v.items.size(); i += 2)
      c.emplace(Conv<K>::from(v.items[i]), Conv<V>::from(v.items[i + 1]));
    return c;
  }
  static Value to(const C& c) {
    Value r = mk('m');
    for (const auto& kv : c) {
      r.items.push_back(Conv<K>::to(kv.first));
      r.items.push_back(Conv<V>::to(kv.second));
    }
    return r;
  }
};

template <class K, class V, class Cm, class A> struct Conv<std::map<K, V, Cm, A>> : MapConv<std::map<K, V, Cm, A>, K, V> {};
template <class K, class V, class Cm, class A> struct Conv<std::multimap<K, V, Cm, A>> : MapConv<std::multimap<K, V, Cm, A>, K, V> {};
template <class K, class V, class H, class Eq, class A> struct Conv<std::unordered_map<K, V, H, Eq, A>> : MapConv<std::unordered_map<K, V, H, Eq, A>, K, V> {};
template <class K, class V, class H, class Eq, class A> struct Conv<std::unordered_multimap<K, V, H, Eq, A>> : MapConv<std::unordered_multimap<K, V, H, Eq, A>, K, V> {};

template <class A, class B>
struct Conv<std::pair<A, B>> {
  static constexpr bool supported = true;
  static std::pair<A, B> from(const Value& v) {
    if (!(v.is_seq() || (!strict() && v.is_set())) || v.items.size() != 2) throw ConvError("expected 2-tuple, got " + desc(v));
    return std::pair<A, B>(Conv<A>::from(v.items[0]), Conv<B>::from(v.items[1]));
  }
  static Value to(const std::pair<A, B>& p) {
    Value r = mk('t');
    r.items.push_back(Conv<A>::to(p.first));
    r.items.push_back(Conv<B>::to(p.second));
    return r;
  }
};

template <class... Ts>
struct Conv<std::tuple<Ts...>> {
  static constexpr bool supported = true;
  template <size_t... I>
  static std::tuple<Ts...> build(const Value& v, std::index_sequence<I...>) {
    return std::tuple<Ts...>{Conv<Ts>::from(v.items[I])...};
  }
  static std::tuple<Ts...> from(const Value& v) {
    if (!v.is_seq() || v.items.size() != sizeof...(Ts))
      throw ConvError("expected " + std::to_string(sizeof...(Ts)) + "-tuple, got " + desc(v));
    return build(v, std::index_sequence_for<Ts...>{});
  }
  static Value to(const std::tuple<Ts...>& t) {
    Value r = mk('t');
    std::apply([&](const auto&... x) { (r.items.push_back(Conv<std::decay_t<decltype(x)>>::to(x)), ...); }, t);
    return r;
  }
};

template <class... Ts>
struct Conv<std::variant<Ts...>> {
  static constexpr bool supported = true;
  using Var = std::variant<Ts...>;
  template <size_t I>
  static bool attempt(const Value& v, std::optional<Var>& out) {
    if constexpr (I == sizeof...(Ts)) {
      return false;
    } else {
      try {
        out.emplace(std::in_place_index<I>, Conv<std::variant_alternative_t<I, Var>>::from(v));
        return true;
      } catch (const ConvError&) {
        return attempt<I + 1>(v, out);
      }
    }
  }
  static Var from(const Value& v) {
    std::optional<Var> out;
    {
      StrictGuard g(true);
      if (attempt<0>(v, out)) return std::move(*out);
    }
    {
      StrictGuard g(false);
      if (attempt<0>(v, out)) return std::move(*out);
    }
    throw ConvError("no variant alternative accepts " + desc(v));
  }
  static Value to(const Var& x) {
    if (x.valueless_by_exception()) return mk('N');
    return std::visit([](const auto& y) { return Conv<std::decay_t<decltype(y)>>::to(y); }, x);
  }
};

template <class T, class C>
struct Conv<std::queue<T, C>> {
  static constexpr bool supported = true;
  static std::queue<T, C> from(const Value& v) {
    std::queue<T, C> q;
    for (auto& x : Conv<std::vector<T>>::from(v)) q.push(std::move(x));
    return q;
  }
  static Value to(const std::queue<T, C>& q0) {
    auto q = q0;
    Value r = mk('l');
    while (!q.empty()) { r.items.push_back(Conv<T>::to(q.front())); q.pop(); }
    return r;
  }
};

template <class T, class C>
struct Conv<std::stack<T, C>> {
  static constexpr bool supported = true;
  static std::stack<T, C> from(const Value& v) {
    std::stack<T, C> q;
    for (auto& x : Conv<std::vector<T>>::from(v)) q.push(std::move(x));
    return q;
  }
  static Value to(const std::stack<T, C>& q0) {
    auto q = q0;
    Value r = mk('l');
    while (!q.empty()) { r.items.push_back(Conv<T>::to(q.top())); q.pop(); }
    std::reverse(r.items.begin(), r.items.end());
    return r;
  }
};

template <class T, class C, class Cm>
struct Conv<std::priority_queue<T, C, Cm>> {
  static constexpr bool supported = true;
  static std::priority_queue<T, C, Cm> from(const Value& v) {
    std::priority_queue<T, C, Cm> q;
    for (auto& x : Conv<std::vector<T>>::from(v)) q.push(std::move(x));
    return q;
  }
  static Value to(const std::priority_queue<T, C, Cm>& q0) {
    auto q = q0;
    Value r = mk('l');
    while (!q.empty()) { r.items.push_back(Conv<T>::to(q.top())); q.pop(); }
    return r;
  }
};

template <class T>
struct Conv<std::reference_wrapper<T>> {
  static constexpr bool supported = Conv<std::remove_cv_t<T>>::supported;
  static std::reference_wrapper<T> from(const Value& v) { return std::ref(*Conv<std::remove_cv_t<T>*>::from(v)); }
  static Value to(const std::reference_wrapper<T>& r) { return Conv<std::remove_cv_t<T>>::to(r.get()); }
};

// ---- function traits and invocation -------------------------------------------------
template <class F> struct FnTraits;
template <class R, class... A> struct FnTraits<R (*)(A...)> {
  using Ret = R; using Cls = void; using Args = std::tuple<A...>;
  static constexpr bool member = false;
};
template <class R, class... A> struct FnTraits<R (*)(A...) noexcept> : FnTraits<R (*)(A...)> {};
#define BCG_MEMBER_TRAITS(QUAL)                                                    \
  template <class R, class C, class... A> struct FnTraits<R (C::*)(A...) QUAL> {   \
    using Ret = R; using Cls = C; using Args = std::tuple<A...>;                   \
    static constexpr bool member = true;                                           \
  };
BCG_MEMBER_TRAITS()
BCG_MEMBER_TRAITS(const)
BCG_MEMBER_TRAITS(&)
BCG_MEMBER_TRAITS(const&)
BCG_MEMBER_TRAITS(noexcept)
BCG_MEMBER_TRAITS(const noexcept)
BCG_MEMBER_TRAITS(& noexcept)
BCG_MEMBER_TRAITS(const& noexcept)
#undef BCG_MEMBER_TRAITS

template <class A> using storage_t = std::remove_cv_t<std::remove_reference_t<A>>;

template <class A, class S>
decltype(auto) pass(S& s) {
  if constexpr (std::is_rvalue_reference_v<A>) return std::move(s);
  else if constexpr (!std::is_reference_v<A> && !std::is_copy_constructible_v<S>) return std::move(s);
  else return (s);
}

struct Outcome {
  bool conv_error = false;
  std::string conv_msg;
  bool raised = false;
  std::string exc_type, exc_msg;
  Value ret;
  Value post = mk('l');
  std::string out, err;
};

struct Capture {
  std::string base;
  int saved[2] = {-1, -1};
  void start() {
    std::cout.flush(); std::cerr.flush(); std::clog.flush();
    std::fflush(stdout); std::fflush(stderr);
    for (int i = 0; i < 2; ++i) {
      std::string path = base + (i ? ".err" : ".out");
      saved[i] = ::dup(i + 1);
      int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
      if (fd >= 0) { ::dup2(fd, i + 1); ::close(fd); }
    }
  }
  std::string slurp(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    std::stringstream ss;
    ss << f.rdbuf();
    return ss.str();
  }
  void stop(Outcome& o) {
    std::cout.flush(); std::cerr.flush(); std::clog.flush();
    std::fflush(stdout); std::fflush(stderr);
    for (int i = 0; i < 2; ++i) {
      if (saved[i] >= 0) { ::dup2(saved[i], i + 1); ::close(saved[i]); saved[i] = -1; }
    }
    o.out = slurp(base + ".out");
    o.err = slurp(base + ".err");
  }
};

// ---- async results: futures / coroutine tasks are driven to completion -----------------
template <class T> struct IsStdFuture : std::false_type {};
template <class T> struct IsStdFuture<std::future<T>> : std::true_type {};
template <class T> struct IsStdFuture<std::shared_future<T>> : std::true_type {};
template <class T, class = void> struct IsTask : std::false_type {};
template <class T>
struct IsTask<T, std::void_t<typename T::promise_type, decltype(std::declval<T&>().get())>> : std::true_type {};
template <class T, class = void> struct IsSyncWaitable : std::false_type {};
template <class T>
struct IsSyncWaitable<T, std::void_t<typename T::promise_type, decltype(std::declval<T&>().sync_wait())>> : std::true_type {};

template <class R>
Value complete_value(R&& r) {
  using S = storage_t<R>;
  if constexpr (IsStdFuture<S>::value || IsTask<S>::value) {
    if constexpr (std::is_void_v<decltype(r.get())>) { r.get(); return mk('N'); }
    else { decltype(auto) v = r.get(); return complete_value(v); }
  } else if constexpr (IsSyncWaitable<S>::value) {
    if constexpr (std::is_void_v<decltype(r.sync_wait())>) { r.sync_wait(); return mk('N'); }
    else { decltype(auto) v = r.sync_wait(); return complete_value(v); }
  } else {
    return Conv<S>::to(r);
  }
}

template <class F, class Tr, class... A, size_t... I>
void invoke_impl(F fn, std::vector<Value>& args, Outcome& o, Capture& cap, std::tuple<A...>*, std::index_sequence<I...>) {
  using Tup = std::tuple<storage_t<A>...>;
  if (args.size() != sizeof...(A)) {
    o.conv_error = true;
    o.conv_msg = "entrypoint takes " + std::to_string(sizeof...(A)) + " arguments, got " + std::to_string(args.size());
    return;
  }
  std::unique_ptr<Tup> st;
  try {
    st.reset(new Tup{Conv<storage_t<A>>::from(args[I])...});
  } catch (const std::exception& e) {
    o.conv_error = true;
    o.conv_msg = std::string("argument conversion failed: ") + e.what();
    return;
  }
  using R = typename Tr::Ret;
  using Cls = typename Tr::Cls;
  cap.start();
  try {
    if constexpr (Tr::member) {
      if constexpr (std::is_default_constructible_v<Cls>) {
        Cls obj{};
        if constexpr (std::is_void_v<R>) {
          (obj.*fn)(pass<A>(std::get<I>(*st))...);
          o.ret = mk('N');
        } else {
          decltype(auto) r = (obj.*fn)(pass<A>(std::get<I>(*st))...);
          o.ret = complete_value(r);
        }
      } else {
        throw ConvError("entrypoint class is not default-constructible");
      }
    } else {
      if constexpr (std::is_void_v<R>) {
        fn(pass<A>(std::get<I>(*st))...);
        o.ret = mk('N');
      } else {
        decltype(auto) r = fn(pass<A>(std::get<I>(*st))...);
        o.ret = complete_value(r);
      }
    }
  } catch (const std::exception& e) {
    o.raised = true;
    int s = 0;
    char* n = abi::__cxa_demangle(typeid(e).name(), nullptr, nullptr, &s);
    o.exc_type = (s == 0 && n) ? n : typeid(e).name();
    std::free(n);
    o.exc_msg = e.what();
  } catch (const std::string& e) {
    o.raised = true; o.exc_type = "std::string"; o.exc_msg = e;
  } catch (const char* e) {
    o.raised = true; o.exc_type = "const char*"; o.exc_msg = e ? e : "";
  } catch (...) {
    o.raised = true; o.exc_type = "unknown"; o.exc_msg = "";
  }
  cap.stop(o);
  try {
    o.post.items = {Conv<storage_t<A>>::to(std::get<I>(*st))...};
  } catch (...) {
    o.post.items.clear();
  }
}

template <class F>
Outcome invoke(F fn, std::vector<Value>& args, Capture& cap) {
  using Tr = FnTraits<F>;
  using Args = typename Tr::Args;
  Outcome o;
  invoke_impl<F, Tr>(fn, args, o, cap, (Args*)nullptr, std::make_index_sequence<std::tuple_size_v<Args>>{});
  return o;
}

inline void emit(std::FILE* fh, const std::string& line) {
  if (!fh) return;
  std::fputs(line.c_str(), fh);
  std::fputc('\n', fh);
  std::fflush(fh);
}

// bind keyword arguments to the remaining parameters by name
inline std::string merge_kwargs(std::vector<Value>& args, const Value& kwargs) {
  if (kwargs.items.empty()) return "";
  static const char* const names[] = BCG_PARAM_NAMES;
  size_t n = sizeof(names) / sizeof(names[0]) - 1;
  std::vector<bool> used(kwargs.items.size() / 2, false);
  for (size_t i = args.size(); i < n; ++i) {
    bool found = false;
    for (size_t j = 0; j + 1 < kwargs.items.size(); j += 2) {
      if (kwargs.items[j].s == names[i]) { args.push_back(kwargs.items[j + 1]); used[j / 2] = true; found = true; break; }
    }
    if (!found) return std::string("missing argument ") + names[i];
  }
  for (size_t j = 0; j < used.size(); ++j)
    if (!used[j]) return "unexpected keyword argument " + kwargs.items[2 * j].s;
  return "";
}

template <class F>
int run_main(int argc, char** argv, F entry) {
  std::string results, ids;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--results" && i + 1 < argc) results = argv[++i];
    else if (a == "--ids" && i + 1 < argc) ids = argv[++i];
    else if (a == "--tol" && i + 1 < argc) ++i;
  }
  std::vector<size_t> order;
  if (!ids.empty()) {
    std::ifstream f(ids);
    std::string line;
    while (std::getline(f, line)) {
      for (size_t k = 0; k < bcg_data::N_TESTS; ++k)
        if (line == bcg_data::TEST_IDS[k]) { order.push_back(k); break; }
    }
  } else {
    for (size_t k = 0; k < bcg_data::N_TESTS; ++k) order.push_back(k);
  }
  std::FILE* fh = results.empty() ? nullptr : std::fopen(results.c_str(), "a");
  Capture cap;
  cap.base = (results.empty() ? std::string("bcg-capture") : results) + ".cap";
  int failed = 0;
  for (size_t k : order) {
    std::string id = bcg_data::TEST_IDS[k];
    emit(fh, "{\"event\":\"start\",\"id\":" + id + "}");
    Value args = parse_value(bcg_data::TEST_ARGS[k]);
    Value kwargs = parse_value(bcg_data::TEST_KWARGS[k]);
    std::string merr = merge_kwargs(args.items, kwargs);
    Outcome o;
    if (!merr.empty()) { o.conv_error = true; o.conv_msg = merr; }
    else o = invoke(entry, args.items, cap);
    std::string ev = "{\"event\":\"value\",\"id\":" + id;
    if (o.conv_error) {
      ev += ",\"conv\":\"" + hex(o.conv_msg) + "\"";
      ++failed;
    } else {
      if (o.raised) ev += ",\"exc\":\"" + hex(o.exc_msg) + "\",\"etype\":\"" + hex(o.exc_type) + "\"";
      else ev += ",\"ret\":\"" + encode(o.ret) + "\"";
      ev += ",\"post\":\"" + encode(o.post) + "\"";
      ev += ",\"out\":\"" + hex(o.out) + "\",\"err\":\"" + hex(o.err) + "\"";
    }
    ev += "}";
    emit(fh, ev);
  }
  emit(fh, "{\"event\":\"done\"}");
  std::remove((cap.base + ".out").c_str());
  std::remove((cap.base + ".err").c_str());
  if (fh) std::fclose(fh);
  return failed ? 1 : 0;
}

}  // namespace bcg

int main(int argc, char** argv) {
  return bcg::run_main(argc, argv, BCG_ENTRY_PTR);
}
'''

RUST_RUNTIME = r'''
#[allow(dead_code, unused_imports, unused_variables, unused_mut, non_snake_case, unused_parens, non_camel_case_types)]
pub mod bcg {
    use std::cell::{Cell, RefCell};
    use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap, HashSet, LinkedList, VecDeque};
    use std::convert::TryFrom;
    use std::hash::{BuildHasher, Hash};
    use std::io::{Read, Write};
    use std::rc::Rc;
    use std::sync::Arc;

    // tags: N none, T/F bool, O opaque, i int, f float, d decimal, s str, b bytes,
    //       l list, q deque, t tuple, S set, z frozenset, m dict, c Counter, o OrderedDict, D defaultdict
    #[derive(Clone, Debug)]
    pub struct Val {
        pub tag: u8,
        pub s: Vec<u8>,
        pub items: Vec<Val>,
    }

    impl Val {
        pub fn mk(tag: u8) -> Val { Val { tag, s: Vec::new(), items: Vec::new() } }
        pub fn scalar(tag: u8, s: &str) -> Val { Val { tag, s: s.as_bytes().to_vec(), items: Vec::new() } }
        pub fn bytes(tag: u8, s: &[u8]) -> Val { Val { tag, s: s.to_vec(), items: Vec::new() } }
        pub fn text(&self) -> String { String::from_utf8_lossy(&self.s).into_owned() }
        pub fn is_seq(&self) -> bool { matches!(self.tag, b'l' | b'q' | b't') }
        pub fn is_set(&self) -> bool { matches!(self.tag, b'S' | b'z') }
        pub fn is_dict(&self) -> bool { matches!(self.tag, b'm' | b'c' | b'o' | b'D') }
        pub fn desc(&self) -> String {
            match self.tag {
                b'N' => "None".into(),
                b'T' | b'F' => "bool".into(),
                b'O' => "object".into(),
                b'i' => format!("int {}", self.text()),
                b'f' => format!("float {}", self.text()),
                b'd' => format!("Decimal {}", self.text()),
                b's' => "str".into(),
                b'b' => "bytes".into(),
                b'l' | b'q' => "list".into(),
                b't' => "tuple".into(),
                b'S' | b'z' => "set".into(),
                _ => "dict".into(),
            }
        }
    }

    fn hexval(c: u8) -> u8 {
        match c {
            b'0'..=b'9' => c - b'0',
            b'a'..=b'f' => c - b'a' + 10,
            b'A'..=b'F' => c - b'A' + 10,
            _ => 0,
        }
    }

    pub fn hex(s: &[u8]) -> String {
        const D: &[u8; 16] = b"0123456789abcdef";
        let mut o = String::with_capacity(s.len() * 2);
        for &c in s {
            o.push(D[(c >> 4) as usize] as char);
            o.push(D[(c & 15) as usize] as char);
        }
        o
    }

    pub fn parse(src: &[u8], p: &mut usize) -> Val {
        let tag = src[*p];
        *p += 1;
        let mut v = Val::mk(tag);
        match tag {
            b'N' | b'T' | b'F' | b'O' => {}
            b'i' | b'f' | b'd' => {
                while src[*p] != b';' { v.s.push(src[*p]); *p += 1; }
                *p += 1;
            }
            b's' | b'b' => {
                while src[*p] != b';' { v.s.push(hexval(src[*p]) * 16 + hexval(src[*p + 1])); *p += 2; }
                *p += 1;
            }
            _ => {
                *p += 1;
                while src[*p] != b']' { v.items.push(parse(src, p)); }
                *p += 1;
            }
        }
        v
    }

    pub fn encode(v: &Val, o: &mut String) {
        o.push(v.tag as char);
        match v.tag {
            b'N' | b'T' | b'F' | b'O' => {}
            b'i' | b'f' | b'd' => { o.push_str(&v.text()); o.push(';'); }
            b's' | b'b' => { o.push_str(&hex(&v.s)); o.push(';'); }
            _ => {
                o.push('[');
                for x in &v.items { encode(x, o); }
                o.push(']');
            }
        }
    }

    pub trait FromVal: Sized {
        fn from_val(v: &Val) -> Result<Self, String>;
    }

    pub trait ToVal {
        fn to_val(&self) -> Val;
        fn to_ret(&self) -> Result<Val, String> { Ok(self.to_val()) }
    }

    pub fn parse_f(v: &Val) -> Result<f64, String> {
        match v.tag {
            b'T' => Ok(1.0),
            b'F' => Ok(0.0),
            b'i' | b'f' | b'd' => {
                let t = v.text().to_lowercase();
                let t = t.trim_start_matches('+');
                match t {
                    "nan" | "-nan" | "snan" | "-snan" => Ok(f64::NAN),
                    "inf" | "infinity" => Ok(f64::INFINITY),
                    "-inf" | "-infinity" => Ok(f64::NEG_INFINITY),
                    _ => t.parse::<f64>().map_err(|_| format!("bad number {}", t)),
                }
            }
            _ => Err(format!("expected number, got {}", v.desc())),
        }
    }

    macro_rules! bcg_int {
        ($($t:ty),*) => { $(
            impl FromVal for $t {
                fn from_val(v: &Val) -> Result<Self, String> {
                    match v.tag {
                        b'i' => v.text().parse::<$t>().map_err(|_| format!("integer {} out of range for {}", v.text(), stringify!($t))),
                        b'T' => Ok(1 as $t),
                        b'F' => Ok(0 as $t),
                        b'f' | b'd' => {
                            let x = parse_f(v)?;
                            if x.is_finite() && x.fract() == 0.0 && x.abs() < 1.7e38 {
                                <$t>::try_from(x as i128).map_err(|_| format!("integer out of range for {}", stringify!($t)))
                            } else {
                                Err(format!("expected int, got {}", v.desc()))
                            }
                        }
                        _ => Err(format!("expected int, got {}", v.desc())),
                    }
                }
            }
            impl ToVal for $t {
                fn to_val(&self) -> Val { Val::scalar(b'i', &self.to_string()) }
            }
        )* };
    }
    bcg_int!(i8, i16, i32, i64, i128, isize, u8, u16, u32, u64, u128, usize);

    impl FromVal for f64 {
        fn from_val(v: &Val) -> Result<Self, String> { parse_f(v) }
    }
    impl ToVal for f64 {
        fn to_val(&self) -> Val {
            if self.is_nan() { Val::scalar(b'f', "nan") }
            else if self.is_infinite() { Val::scalar(b'f', if *self > 0.0 { "inf" } else { "-inf" }) }
            else { Val::scalar(b'f', &format!("{:?}", self)) }
        }
    }
    impl FromVal for f32 {
        fn from_val(v: &Val) -> Result<Self, String> { parse_f(v).map(|x| x as f32) }
    }
    impl ToVal for f32 {
        fn to_val(&self) -> Val {
            if self.is_nan() { Val::scalar(b'f', "nan") }
            else if self.is_infinite() { Val::scalar(b'f', if *self > 0.0 { "inf" } else { "-inf" }) }
            else { Val::scalar(b'f', &format!("{:?}", self)) }
        }
    }

    impl FromVal for bool {
        fn from_val(v: &Val) -> Result<Self, String> {
            match v.tag {
                b'T' => Ok(true),
                b'F' => Ok(false),
                b'i' => Ok(v.text() != "0"),
                _ => Err(format!("expected bool, got {}", v.desc())),
            }
        }
    }
    impl ToVal for bool {
        fn to_val(&self) -> Val { Val::mk(if *self { b'T' } else { b'F' }) }
    }

    impl FromVal for char {
        fn from_val(v: &Val) -> Result<Self, String> {
            if v.tag == b's' || v.tag == b'b' {
                let s = v.text();
                let mut it = s.chars();
                if let (Some(c), None) = (it.next(), it.next()) { return Ok(c); }
            }
            if v.tag == b'i' {
                if let Ok(n) = v.text().parse::<u32>() {
                    if let Some(c) = char::from_u32(n) { return Ok(c); }
                }
            }
            Err(format!("expected single character, got {}", v.desc()))
        }
    }
    impl ToVal for char {
        fn to_val(&self) -> Val { Val::scalar(b's', &self.to_string()) }
    }

    impl FromVal for String {
        fn from_val(v: &Val) -> Result<Self, String> {
            match v.tag {
                b's' | b'b' => Ok(v.text()),
                _ => Err(format!("expected str, got {}", v.desc())),
            }
        }
    }
    impl ToVal for String {
        fn to_val(&self) -> Val { Val::bytes(b's', self.as_bytes()) }
    }
    impl ToVal for str {
        fn to_val(&self) -> Val { Val::bytes(b's', self.as_bytes()) }
    }
    impl FromVal for &'static str {
        fn from_val(v: &Val) -> Result<Self, String> {
            let s: String = FromVal::from_val(v)?;
            Ok(Box::leak(s.into_boxed_str()))
        }
    }
    impl<T: FromVal + 'static> FromVal for &'static [T] {
        fn from_val(v: &Val) -> Result<Self, String> {
            let s: Vec<T> = FromVal::from_val(v)?;
            Ok(Box::leak(s.into_boxed_slice()))
        }
    }
    impl<T: FromVal + 'static> FromVal for &'static mut [T] {
        fn from_val(v: &Val) -> Result<Self, String> {
            let s: Vec<T> = FromVal::from_val(v)?;
            Ok(Box::leak(s.into_boxed_slice()))
        }
    }
    impl FromVal for std::borrow::Cow<'static, str> {
        fn from_val(v: &Val) -> Result<Self, String> {
            let s: String = FromVal::from_val(v)?;
            Ok(std::borrow::Cow::Owned(s))
        }
    }
    impl<'a> ToVal for std::borrow::Cow<'a, str> {
        fn to_val(&self) -> Val { Val::bytes(b's', self.as_bytes()) }
    }

    impl FromVal for () {
        fn from_val(v: &Val) -> Result<Self, String> {
            if v.tag == b'N' { Ok(()) } else { Err(format!("expected None, got {}", v.desc())) }
        }
    }
    impl ToVal for () {
        fn to_val(&self) -> Val { Val::mk(b'N') }
    }

    impl<T: FromVal> FromVal for Option<T> {
        fn from_val(v: &Val) -> Result<Self, String> {
            if v.tag == b'N' { Ok(None) } else { T::from_val(v).map(Some) }
        }
    }
    impl<T: ToVal> ToVal for Option<T> {
        fn to_val(&self) -> Val {
            match self { Some(x) => x.to_val(), None => Val::mk(b'N') }
        }
    }

    impl<T: ToVal, E: std::fmt::Debug> ToVal for Result<T, E> {
        fn to_val(&self) -> Val {
            match self { Ok(x) => x.to_val(), Err(_) => Val::mk(b'O') }
        }
        fn to_ret(&self) -> Result<Val, String> {
            match self {
                Ok(x) => x.to_ret(),
                Err(e) => {
                    let mut m = format!("{:?}", e);
                    if m.len() >= 2 && m.starts_with('"') && m.ends_with('"') {
                        m = m[1..m.len() - 1].replace("\\\"", "\"").replace("\\n", "\n").replace("\\\\", "\\");
                    }
                    Err(m)
                }
            }
        }
    }

    impl<'a, T: ToVal + ?Sized> ToVal for &'a T {
        fn to_val(&self) -> Val { (**self).to_val() }
        fn to_ret(&self) -> Result<Val, String> { (**self).to_ret() }
    }
    impl<'a, T: ToVal + ?Sized> ToVal for &'a mut T {
        fn to_val(&self) -> Val { (**self).to_val() }
        fn to_ret(&self) -> Result<Val, String> { (**self).to_ret() }
    }
    impl<T: FromVal> FromVal for Box<T> {
        fn from_val(v: &Val) -> Result<Self, String> { T::from_val(v).map(Box::new) }
    }
    impl<T: ToVal + ?Sized> ToVal for Box<T> {
        fn to_val(&self) -> Val { (**self).to_val() }
    }
    impl<T: FromVal> FromVal for Rc<T> {
        fn from_val(v: &Val) -> Result<Self, String> { T::from_val(v).map(Rc::new) }
    }
    impl<T: ToVal + ?Sized> ToVal for Rc<T> {
        fn to_val(&self) -> Val { (**self).to_val() }
    }
    impl<T: FromVal> FromVal for Arc<T> {
        fn from_val(v: &Val) -> Result<Self, String> { T::from_val(v).map(Arc::new) }
    }
    impl<T: ToVal + ?Sized> ToVal for Arc<T> {
        fn to_val(&self) -> Val { (**self).to_val() }
    }
    impl<T: FromVal> FromVal for RefCell<T> {
        fn from_val(v: &Val) -> Result<Self, String> { T::from_val(v).map(RefCell::new) }
    }
    impl<T: ToVal> ToVal for RefCell<T> {
        fn to_val(&self) -> Val {
            match self.try_borrow() { Ok(b) => b.to_val(), Err(_) => Val::mk(b'O') }
        }
    }
    impl<T: FromVal> FromVal for Cell<T> {
        fn from_val(v: &Val) -> Result<Self, String> { T::from_val(v).map(Cell::new) }
    }
    impl<T: ToVal + Copy> ToVal for Cell<T> {
        fn to_val(&self) -> Val { self.get().to_val() }
    }

    fn seq_items(v: &Val) -> Result<Vec<Val>, String> {
        if v.is_seq() || v.is_set() { return Ok(v.items.clone()); }
        if v.tag == b'b' { return Ok(v.s.iter().map(|c| Val::scalar(b'i', &c.to_string())).collect()); }
        if v.tag == b's' { return Ok(v.text().chars().map(|c| Val::scalar(b's', &c.to_string())).collect()); }
        Err(format!("expected list, got {}", v.desc()))
    }

    fn list_of<'a, T: ToVal + 'a, I: Iterator<Item = &'a T>>(tag: u8, it: I) -> Val {
        let mut r = Val::mk(tag);
        for x in it { r.items.push(x.to_val()); }
        r
    }

    impl<T: FromVal> FromVal for Vec<T> {
        fn from_val(v: &Val) -> Result<Self, String> {
            seq_items(v)?.iter().map(T::from_val).collect()
        }
    }
    impl<T: ToVal> ToVal for Vec<T> {
        fn to_val(&self) -> Val { list_of(b'l', self.iter()) }
    }
    impl<T: ToVal> ToVal for [T] {
        fn to_val(&self) -> Val { list_of(b'l', self.iter()) }
    }
    impl<T: FromVal, const N: usize> FromVal for [T; N] {
        fn from_val(v: &Val) -> Result<Self, String> {
            let items: Vec<T> = FromVal::from_val(v)?;
            let n = items.len();
            <[T; N]>::try_from(items).map_err(|_| format!("expected {} items, got {}", N, n))
        }
    }
    impl<T: ToVal, const N: usize> ToVal for [T; N] {
        fn to_val(&self) -> Val { list_of(b'l', self.iter()) }
    }
    impl<T: FromVal> FromVal for VecDeque<T> {
        fn from_val(v: &Val) -> Result<Self, String> {
            seq_items(v)?.iter().map(T::from_val).collect()
        }
    }
    impl<T: ToVal> ToVal for VecDeque<T> {
        fn to_val(&self) -> Val { list_of(b'l', self.iter()) }
    }
    impl<T: FromVal> FromVal for LinkedList<T> {
        fn from_val(v: &Val) -> Result<Self, String> {
            seq_items(v)?.iter().map(T::from_val).collect()
        }
    }
    impl<T: ToVal> ToVal for LinkedList<T> {
        fn to_val(&self) -> Val { list_of(b'l', self.iter()) }
    }
    impl<T: FromVal + Ord> FromVal for BinaryHeap<T> {
        fn from_val(v: &Val) -> Result<Self, String> {
            seq_items(v)?.iter().map(T::from_val).collect()
        }
    }
    impl<T: ToVal + Ord> ToVal for BinaryHeap<T> {
        fn to_val(&self) -> Val {
            let mut items: Vec<&T> = self.iter().collect();
            items.sort_by(|a, b| b.cmp(a));
            let mut r = Val::mk(b'l');
            for x in items { r.items.push(x.to_val()); }
            r
        }
    }

    fn set_items(v: &Val) -> Result<&Vec<Val>, String> {
        if v.is_set() || v.is_seq() { Ok(&v.items) } else { Err(format!("expected set, got {}", v.desc())) }
    }
    impl<T: FromVal + Eq + Hash, S: BuildHasher + Default> FromVal for HashSet<T, S> {
        fn from_val(v: &Val) -> Result<Self, String> {
            set_items(v)?.iter().map(T::from_val).collect()
        }
    }
    impl<T: ToVal, S> ToVal for HashSet<T, S> {
        fn to_val(&self) -> Val { list_of(b'S', self.iter()) }
    }
    impl<T: FromVal + Ord> FromVal for BTreeSet<T> {
        fn from_val(v: &Val) -> Result<Self, String> {
            set_items(v)?.iter().map(T::from_val).collect()
        }
    }
    impl<T: ToVal> ToVal for BTreeSet<T> {
        fn to_val(&self) -> Val { list_of(b'S', self.iter()) }
    }

    fn dict_pairs<K: FromVal, V: FromVal>(v: &Val) -> Result<Vec<(K, V)>, String> {
        if !v.is_dict() { return Err(format!("expected dict, got {}", v.desc())); }
        let mut out = Vec::new();
        let mut i = 0;
        while i + 1 < v.items.len() {
            out.push((K::from_val(&v.items[i])?, V::from_val(&v.items[i + 1])?));
            i += 2;
        }
        Ok(out)
    }
    impl<K: FromVal + Eq + Hash, V: FromVal, S: BuildHasher + Default> FromVal for HashMap<K, V, S> {
        fn from_val(v: &Val) -> Result<Self, String> { Ok(dict_pairs(v)?.into_iter().collect()) }
    }
    impl<K: ToVal, V: ToVal, S> ToVal for HashMap<K, V, S> {
        fn to_val(&self) -> Val {
            let mut r = Val::mk(b'm');
            for (k, x) in self.iter() { r.items.push(k.to_val()); r.items.push(x.to_val()); }
            r
        }
    }
    impl<K: FromVal + Ord, V: FromVal> FromVal for BTreeMap<K, V> {
        fn from_val(v: &Val) -> Result<Self, String> { Ok(dict_pairs(v)?.into_iter().collect()) }
    }
    impl<K: ToVal, V: ToVal> ToVal for BTreeMap<K, V> {
        fn to_val(&self) -> Val {
            let mut r = Val::mk(b'm');
            for (k, x) in self.iter() { r.items.push(k.to_val()); r.items.push(x.to_val()); }
            r
        }
    }

    macro_rules! bcg_tuple {
        ($n:expr; $($T:ident $i:tt),+) => {
            impl<$($T: FromVal),+> FromVal for ($($T,)+) {
                fn from_val(v: &Val) -> Result<Self, String> {
                    if !(v.is_seq()) || v.items.len() != $n {
                        return Err(format!("expected {}-tuple, got {}", $n, v.desc()));
                    }
                    Ok(($($T::from_val(&v.items[$i])?,)+))
                }
            }
            impl<$($T: ToVal),+> ToVal for ($($T,)+) {
                fn to_val(&self) -> Val {
                    let mut r = Val::mk(b't');
                    $( r.items.push(self.$i.to_val()); )+
                    r
                }
            }
        };
    }
    bcg_tuple!(1; A 0);
    bcg_tuple!(2; A 0, B 1);
    bcg_tuple!(3; A 0, B 1, C 2);
    bcg_tuple!(4; A 0, B 1, C 2, D 3);
    bcg_tuple!(5; A 0, B 1, C 2, D 3, E 4);
    bcg_tuple!(6; A 0, B 1, C 2, D 3, E 4, F 5);
    bcg_tuple!(7; A 0, B 1, C 2, D 3, E 4, F 5, G 6);
    bcg_tuple!(8; A 0, B 1, C 2, D 3, E 4, F 5, G 6, H 7);
    bcg_tuple!(9; A 0, B 1, C 2, D 3, E 4, F 5, G 6, H 7, I 8);
    bcg_tuple!(10; A 0, B 1, C 2, D 3, E 4, F 5, G 6, H 7, I 8, J 9);
    bcg_tuple!(11; A 0, B 1, C 2, D 3, E 4, F 5, G 6, H 7, I 8, J 9, K 10);
    bcg_tuple!(12; A 0, B 1, C 2, D 3, E 4, F 5, G 6, H 7, I 8, J 9, K 10, L 11);

    pub fn from_arg<T: FromVal>(v: &Val) -> Result<T, String> {
        T::from_val(v).map_err(|e| format!("argument conversion failed: {}", e))
    }

    // ---- stdout / stderr capture --------------------------------------------------------
    extern "C" {
        fn dup(fd: i32) -> i32;
        fn dup2(src: i32, dst: i32) -> i32;
        fn close(fd: i32) -> i32;
    }

    pub struct Capture {
        pub base: String,
        saved: [i32; 2],
    }

    impl Capture {
        pub fn new(base: String) -> Capture { Capture { base, saved: [-1, -1] } }
        pub fn start(&mut self) {
            let _ = std::io::stdout().flush();
            let _ = std::io::stderr().flush();
            for i in 0..2 {
                use std::os::unix::io::AsRawFd;
                let path = format!("{}{}", self.base, if i == 0 { ".out" } else { ".err" });
                unsafe { self.saved[i] = dup(i as i32 + 1); }
                if let Ok(f) = std::fs::File::create(&path) {
                    unsafe { dup2(f.as_raw_fd(), i as i32 + 1); }
                }
            }
        }
        pub fn stop(&mut self) -> (Vec<u8>, Vec<u8>) {
            let _ = std::io::stdout().flush();
            let _ = std::io::stderr().flush();
            for i in 0..2 {
                if self.saved[i] >= 0 {
                    unsafe { dup2(self.saved[i], i as i32 + 1); close(self.saved[i]); }
                    self.saved[i] = -1;
                }
            }
            let rd = |suffix: &str| std::fs::read(format!("{}{}", self.base, suffix)).unwrap_or_default();
            (rd(".out"), rd(".err"))
        }
    }

    // ---- async: futures returned by the entrypoint are driven to completion ----
    pub fn block_on<F: std::future::Future>(f: F) -> F::Output {
        use std::task::{Context, Poll, Wake, Waker};
        struct ThreadWaker(std::thread::Thread);
        impl Wake for ThreadWaker {
            fn wake(self: std::sync::Arc<Self>) { self.0.unpark(); }
            fn wake_by_ref(self: &std::sync::Arc<Self>) { self.0.unpark(); }
        }
        let waker = Waker::from(std::sync::Arc::new(ThreadWaker(std::thread::current())));
        let mut cx = Context::from_waker(&waker);
        let mut f = Box::pin(f);
        loop {
            match f.as_mut().poll(&mut cx) {
                Poll::Ready(v) => return v,
                Poll::Pending => std::thread::park(),
            }
        }
    }
    pub struct Done<T>(pub std::cell::Cell<Option<T>>);
    pub trait DoneFut { type Out; fn bcg_done(self) -> Self::Out; }
    impl<F: std::future::Future> DoneFut for Done<F> {
        type Out = F::Output;
        fn bcg_done(self) -> F::Output { block_on(self.0.into_inner().unwrap()) }
    }
    pub trait DoneVal { type Out; fn bcg_done(self) -> Self::Out; }
    impl<'a, T> DoneVal for &'a Done<T> {
        type Out = T;
        fn bcg_done(self) -> T { self.0.take().unwrap() }
    }

    pub struct Outcome {
        pub conv: Option<String>,
        pub exc: Option<(String, String)>,
        pub ret: Val,
        pub post: Vec<Val>,
        pub out: Vec<u8>,
        pub err: Vec<u8>,
    }

    impl Outcome {
        pub fn conv(msg: String) -> Outcome {
            Outcome { conv: Some(msg), exc: None, ret: Val::mk(b'N'), post: Vec::new(), out: Vec::new(), err: Vec::new() }
        }
        pub fn finish(r: std::thread::Result<Result<Val, String>>, post: Vec<Val>, cap: (Vec<u8>, Vec<u8>)) -> Outcome {
            let mut o = Outcome { conv: None, exc: None, ret: Val::mk(b'N'), post, out: cap.0, err: cap.1 };
            match r {
                Ok(Ok(v)) => o.ret = v,
                Ok(Err(m)) => o.exc = Some(("Err".to_string(), m)),
                Err(p) => {
                    let m = if let Some(s) = p.downcast_ref::<&str>() { s.to_string() }
                        else if let Some(s) = p.downcast_ref::<String>() { s.clone() }
                        else { String::new() };
                    o.exc = Some(("panic".to_string(), m));
                }
            }
            o
        }
    }

    fn emit(fh: &mut Option<std::fs::File>, line: &str) {
        if let Some(f) = fh.as_mut() {
            let _ = f.write_all(line.as_bytes());
            let _ = f.write_all(b"\n");
            let _ = f.flush();
        }
    }

    fn merge_kwargs(args: &mut Vec<Val>, kwargs: &Val, names: &[&str]) -> Result<(), String> {
        if kwargs.items.is_empty() { return Ok(()); }
        let mut used = vec![false; kwargs.items.len() / 2];
        for i in args.len()..names.len() {
            let mut found = false;
            let mut j = 0;
            while j + 1 < kwargs.items.len() {
                if kwargs.items[j].s == names[i].as_bytes() {
                    args.push(kwargs.items[j + 1].clone());
                    used[j / 2] = true;
                    found = true;
                    break;
                }
                j += 2;
            }
            if !found { return Err(format!("missing argument {}", names[i])); }
        }
        for (j, u) in used.iter().enumerate() {
            if !u { return Err(format!("unexpected keyword argument {}", kwargs.items[2 * j].text())); }
        }
        Ok(())
    }

    pub fn run_main(ids: &[&str], args: &[&str], kwargs: &[&str], names: &[&str],
                    entry: fn(&[Val], &mut Capture) -> Outcome) -> i32 {
        std::panic::set_hook(Box::new(|_| {}));
        let argv: Vec<String> = std::env::args().collect();
        let mut results = String::new();
        let mut ids_path = String::new();
        let mut i = 1;
        while i < argv.len() {
            if argv[i] == "--results" && i + 1 < argv.len() { results = argv[i + 1].clone(); i += 2; }
            else if argv[i] == "--ids" && i + 1 < argv.len() { ids_path = argv[i + 1].clone(); i += 2; }
            else if argv[i] == "--tol" && i + 1 < argv.len() { i += 2; }
            else { i += 1; }
        }
        let mut order: Vec<usize> = Vec::new();
        if !ids_path.is_empty() {
            let text = std::fs::read_to_string(&ids_path).unwrap_or_default();
            for line in text.lines() {
                if let Some(k) = ids.iter().position(|x| *x == line) { order.push(k); }
            }
        } else {
            order = (0..ids.len()).collect();
        }
        let mut fh = if results.is_empty() { None } else {
            std::fs::OpenOptions::new().append(true).create(true).open(&results).ok()
        };
        let base = format!("{}.cap", if results.is_empty() { "bcg-capture".to_string() } else { results.clone() });
        let mut cap = Capture::new(base.clone());
        let mut failed = 0;
        for k in order {
            let id = ids[k];
            emit(&mut fh, &format!("{{\"event\":\"start\",\"id\":{}}}", id));
            let mut p = 0usize;
            let mut av = parse(args[k].as_bytes(), &mut p);
            let mut p2 = 0usize;
            let kv = parse(kwargs[k].as_bytes(), &mut p2);
            let o = match merge_kwargs(&mut av.items, &kv, names) {
                Ok(()) => entry(&av.items, &mut cap),
                Err(e) => Outcome::conv(e),
            };
            let mut ev = format!("{{\"event\":\"value\",\"id\":{}", id);
            if let Some(m) = &o.conv {
                ev.push_str(&format!(",\"conv\":\"{}\"", hex(m.as_bytes())));
                failed += 1;
            } else {
                if let Some((t, m)) = &o.exc {
                    ev.push_str(&format!(",\"exc\":\"{}\",\"etype\":\"{}\"", hex(m.as_bytes()), hex(t.as_bytes())));
                } else {
                    let mut s = String::new();
                    encode(&o.ret, &mut s);
                    ev.push_str(&format!(",\"ret\":\"{}\"", s));
                }
                let mut s = String::from("l[");
                for x in &o.post { encode(x, &mut s); }
                s.push(']');
                ev.push_str(&format!(",\"post\":\"{}\",\"out\":\"{}\",\"err\":\"{}\"", s, hex(&o.out), hex(&o.err)));
            }
            ev.push('}');
            emit(&mut fh, &ev);
        }
        emit(&mut fh, "{\"event\":\"done\"}");
        let _ = std::fs::remove_file(format!("{}.out", base));
        let _ = std::fs::remove_file(format!("{}.err", base));
        if failed > 0 { 1 } else { 0 }
    }
}
'''


# ---------------------------------------------------------------------------
# Native (C++ / Rust) targets
# ---------------------------------------------------------------------------
#
# The native tester converts each test's arguments (encoded with ``native_enc``) into the
# parameter types of the solution's entrypoint, calls it, and reports the return value, the
# post-call state of the arguments, any exception / panic and the captured stdout / stderr.
# The driver then evaluates the assertion with the same Python-semantics evaluator that the
# Python tester uses (``_judge``), so every value type and operation supported for Python
# tests is supported for C++ / Rust too.

NATIVE_LANGS = ("cpp", "rust")
NATIVE_PARAMS = []  # parameter names of the entrypoint found by the last native build


def cpp_param_names(src, entry):
    """Parameter names of the C++ entrypoint (empty strings for unnamed ones)."""
    meth = entry.lstrip("&").split("::")[-1]
    code = _blank_code(src, "cpp")
    blocks = _blocks(code)
    for m in re.finditer(r"(?<![\w.>:~])" + re.escape(meth) + r"\s*\(", code):
        if any("(" in h and not re.search(r"\b(class|struct|namespace)\b", h) for _, _, h in _enclosing(blocks, m.start())):
            continue
        k = m.end() - 1
        e = _match_close(code, k, "(", ")")
        if e < 0:
            continue
        params = [p.strip() for p in _split_top(code[k + 1:e])]
        if params == ["void"]:
            return []
        names = []
        for p in params:
            p = _split_top(p, "=")[0].strip()
            p = re.sub(r"\[[^\]]*\]\s*$", "", p).strip()
            nm = re.search(r"([A-Za-z_]\w*)\s*$", p)
            kw = {"int", "long", "short", "char", "double", "float", "bool", "unsigned", "signed", "auto", "const"}
            ok = nm and len(re.findall(r"[A-Za-z_]\w*", p)) > 1 and nm.group(1) not in kw
            names.append(nm.group(1) if ok else "")
        return names
    return []


def native_enc(v):
    """Encode a Python value in the compact tagged format understood by the native runtimes."""
    out = []

    def enc(x):
        if x is None:
            out.append("N")
        elif isinstance(x, bool):
            out.append("T" if x else "F")
        elif isinstance(x, int):
            out.append("i%d;" % x)
        elif isinstance(x, float):
            out.append("f%s;" % repr(x))
        elif isinstance(x, decimal.Decimal):
            if x.is_nan():
                t = "nan"
            elif x.is_infinite():
                t = "inf" if x > 0 else "-inf"
            else:
                t = str(x)
            out.append("d%s;" % t)
        elif isinstance(x, str):
            out.append("s" + x.encode("utf-8", "surrogatepass").hex() + ";")
        elif isinstance(x, (bytes, bytearray)):
            out.append("b" + bytes(x).hex() + ";")
        elif isinstance(x, dict):
            if isinstance(x, collections.Counter):
                tag = "c"
            elif isinstance(x, collections.defaultdict):
                tag = "D"
            elif isinstance(x, collections.OrderedDict):
                tag = "o"
            else:
                tag = "m"
            out.append(tag + "[")
            for k, val in x.items():
                enc(k)
                enc(val)
            out.append("]")
        elif isinstance(x, (list, tuple, collections.deque, set, frozenset)):
            if isinstance(x, list):
                tag = "l"
            elif isinstance(x, tuple):
                tag = "t"
            elif isinstance(x, collections.deque):
                tag = "q"
            elif isinstance(x, frozenset):
                tag = "z"
            else:
                tag = "S"
            out.append(tag + "[")
            for y in x:
                enc(y)
            out.append("]")
        else:
            raise GenerateError(f"cannot translate {x!r}")

    enc(v)
    return "".join(out)


class _Opaque:
    """A native value with no Python counterpart (e.g. a user-defined struct)."""

    def __repr__(self):
        return "<native object>"


def _hashable_key(v):
    if isinstance(v, list):
        return tuple(_hashable_key(x) for x in v)
    if isinstance(v, set):
        return frozenset(v)
    if isinstance(v, dict):
        return tuple((k, _hashable_key(x)) for k, x in v.items())
    return v


def native_dec(src, decimals=False):
    """Decode a value reported by a native tester into Python values."""
    pos = 0

    def num(text):
        if decimals:
            try:
                return decimal.Decimal(text)
            except Exception:
                pass
        return float(text)

    def dec():
        nonlocal pos
        tag = src[pos]
        pos += 1
        if tag == "N":
            return None
        if tag == "T":
            return True
        if tag == "F":
            return False
        if tag == "O":
            return _Opaque()
        if tag in "ifdsb":
            end = src.index(";", pos)
            text = src[pos:end]
            pos = end + 1
            if tag == "i":
                return int(text)
            if tag in "fd":
                return num(text)
            raw = bytes.fromhex(text)
            if tag == "b":
                return raw
            return raw.decode("utf-8", "surrogateescape")
        pos += 1  # '['
        items = []
        while src[pos] != "]":
            items.append(dec())
        pos += 1
        if tag in "mcoD":
            pairs = [(_hashable_key(items[i]), items[i + 1]) for i in range(0, len(items) - 1, 2)]
            if tag == "c":
                return collections.Counter(dict(pairs))
            if tag == "o":
                return collections.OrderedDict(pairs)
            return dict(pairs)
        if tag in "Sz":
            vals = {_hashable_key(x) for x in items}
            return frozenset(vals) if tag == "z" else vals
        if tag == "t":
            return tuple(items)
        if tag == "q":
            return collections.deque(items)
        return items

    return dec()


def _involves_decimal(t):
    """True if a test uses Decimal values (then native floats are compared as Decimals)."""
    def walk(v, depth=0):
        if depth > 50:
            return False
        if isinstance(v, decimal.Decimal):
            return True
        if isinstance(v, dict):
            return any(walk(k, depth + 1) or walk(x, depth + 1) for k, x in v.items())
        if isinstance(v, (list, tuple, set, frozenset, collections.deque)):
            return any(walk(x, depth + 1) for x in v)
        return False
    return any(walk(t.get(k)) for k in ("expected", "args", "kwargs", "xf"))


def _c_str(s):
    """A C / C++ / Rust string literal for an ASCII string."""
    out = ['"']
    for ch in s:
        if ch in '\\"':
            out.append("\\" + ch)
        elif ch == "?":
            out.append("\\?")
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch))
    out.append('"')
    return "".join(out)


def _rs_str(s):
    out = ['"']
    for ch in s:
        if ch in '\\"':
            out.append("\\" + ch)
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            out.append("\\u{%x}" % ord(ch))
    out.append('"')
    return "".join(out)


def _native_id(tid):
    return json.dumps(tid, ensure_ascii=True)


def render_native(tests, entrypoint, lang):
    meta = "// " + META_MARKER + " " + json.dumps({"lang": lang, "entrypoint": entrypoint})
    ids, args, kwargs = [], [], []
    for t in tests:
        if t["kind"] == "loop":
            continue
        ids.append(_native_id(t["id"]))
        args.append(native_enc(list(t["args"])))
        kwargs.append(native_enc(dict(t["kwargs"])))
    lines = ["// Auto-generated by babel_code_goat.py -- do not edit.", meta]
    if lang == "cpp":
        lines.append("// Compiled by `babel_code_goat.py test` together with the solution (C++17 or later).")
        lines.append("#include <cstddef>")
        lines.append("namespace bcg_data {")
        lines.append(f"static const std::size_t N_TESTS = {len(ids)};")
        lines.append("static const char* const TEST_IDS[] = {")
        lines.extend("    " + _c_str(x) + "," for x in ids)
        lines.append("    nullptr};")
        lines.append("static const char* const TEST_ARGS[] = {")
        lines.extend("    " + _c_str(x) + "," for x in args)
        lines.append("    nullptr};")
        lines.append("static const char* const TEST_KWARGS[] = {")
        lines.extend("    " + _c_str(x) + "," for x in kwargs)
        lines.append("    nullptr};")
        lines.append("}  // namespace bcg_data")
        return "\n".join(lines) + "\n" + CPP_RUNTIME
    lines.append("// Compiled by `babel_code_goat.py test` together with the solution (Rust 2021).")
    lines.append("static BCG_IDS: &[&str] = &[")
    lines.extend("    " + _rs_str(x) + "," for x in ids)
    lines.append("];")
    lines.append("static BCG_ARGS: &[&str] = &[")
    lines.extend("    " + _rs_str(x) + "," for x in args)
    lines.append("];")
    lines.append("static BCG_KWARGS: &[&str] = &[")
    lines.extend("    " + _rs_str(x) + "," for x in kwargs)
    lines.append("];")
    lines.append("fn main() {")
    lines.append("    std::process::exit(bcg::run_main(BCG_IDS, BCG_ARGS, BCG_KWARGS, BCG_PARAM_NAMES, __bcg_entry));")
    lines.append("}")
    return "\n".join(lines) + "\n" + RUST_RUNTIME


# ---- solution source analysis ----------------------------------------------------------

def _blank_code(src, lang):
    """Replace comments and string / char literals by spaces (keeping offsets and newlines)."""
    out = list(src)
    n = len(src)
    i = 0

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif lang == "rust" and c == "r" and re.match(r'r#*"', src[i:i + 300]) and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            m = re.match(r'r(#*)"', src[i:])
            close = '"' + m.group(1)
            j = src.find(close, i + len(m.group(0)))
            j = n if j < 0 else j + len(close)
            blank(i, j)
            i = j
        elif lang == "cpp" and c == "R" and src.startswith('R"', i) and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            m = re.match(r'R"([^(\s]*)\(', src[i:])
            if m:
                close = ")" + m.group(1) + '"'
                j = src.find(close, i + len(m.group(0)))
                j = n if j < 0 else j + len(close)
                blank(i, j)
                i = j
            else:
                i += 1
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        elif c == "'":
            if lang == "rust":
                m = re.match(r"'(\\(x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|.)|[^\\'])'", src[i:i + 16])
                if m:
                    blank(i, i + len(m.group(0)))
                    i += len(m.group(0))
                    continue
                i += 1  # a lifetime
            else:
                if i > 0 and (src[i - 1].isalnum()) and i + 1 < n and src[i + 1].isalnum() and src[i - 1] not in "uUL8":
                    i += 1  # digit separator 1'000
                    continue
                j = i + 1
                while j < n and src[j] != "'" and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                blank(i, j + 1)
                i = j + 1
        else:
            i += 1
    return "".join(out)


def _match_close(code, i, open_ch, close_ch):
    """Index of the bracket closing the one at ``i``."""
    depth = 0
    for k in range(i, len(code)):
        ch = code[k]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _blocks(code):
    """Yield (start, end, header) for every ``{ ... }`` block; header is the text before it."""
    stack = []
    last_stop = 0
    res = []
    for k, ch in enumerate(code):
        if ch == "{":
            stack.append((k, code[last_stop:k]))
            last_stop = k + 1
        elif ch == "}":
            if stack:
                start, header = stack.pop()
                res.append((start, k, header))
            last_stop = k + 1
        elif ch == ";":
            last_stop = k + 1
    return res


def _enclosing(blocks, pos):
    """Innermost-first list of headers of blocks enclosing ``pos``."""
    encl = [(s, e, h) for s, e, h in blocks if s < pos < e]
    encl.sort(key=lambda b: b[0], reverse=True)
    return encl


def _split_top(text, sep=","):
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


_CPP_CLASS_RE = re.compile(r"\b(?:class|struct)\s+(?:alignas\s*\([^)]*\)\s*)?([A-Za-z_]\w*)\s*(?:final\b)?[^;{()]*$")


def _name_variants(name):
    """The name itself, then its snake_case and camelCase spellings."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    snake = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", snake).lower()
    head, *rest = name.split("_") if "_" in name.strip("_") else (name, )
    camel = head + "".join(w[:1].upper() + w[1:] for w in rest)
    out = []
    for v in (name, snake, camel):
        if v and v not in out:
            out.append(v)
    return out


def _pick_name(code, name, pattern):
    """First spelling of ``name`` that occurs in ``code`` (``pattern`` formats the regex)."""
    for v in _name_variants(name):
        if re.search(pattern % re.escape(v), code):
            return v
    return name


def cpp_entry_expr(src, entrypoint):
    """C++ expression naming the entrypoint (``&f`` or ``&Class::method``)."""
    parts = entrypoint.split(".")
    code = _blank_code(src, "cpp")
    meth = _pick_name(code, parts[-1], r"(?<![\w.>:~])%s\s*\(")
    if len(parts) == 2:
        return f"&{parts[0]}::{meth}"
    blocks = _blocks(code)
    frees, owners = [], []
    for m in re.finditer(r"(?<![\w.>:~])" + re.escape(meth) + r"\s*\(", code):
        scopes = []  # innermost first: ("ns" | "cls", name)
        in_function = False
        for s_, e_, h in _enclosing(blocks, m.start()):
            hs = h.strip()
            hm = _CPP_CLASS_RE.search(hs)
            nm = re.search(r"\bnamespace\s*([A-Za-z_][\w:]*)?\s*$", hs)
            if hm and "(" not in h:
                scopes.append(("cls", hm.group(1)))
            elif nm:
                if nm.group(1):
                    scopes.append(("ns", nm.group(1)))
            elif re.search(r'\bextern\s*$', hs):
                continue
            else:
                in_function = True
                break
        if in_function:
            continue
        qual = "::".join(n for _, n in reversed(scopes))
        target = owners if any(k == "cls" for k, _ in scopes) else frees
        target.append(qual)
    if frees:
        q = frees[0]
        return f"&{q}::{meth}" if q else f"&{meth}"
    if owners:
        return f"&{owners[0]}::{meth}"
    return f"&{meth}"


def _strip_lifetimes(t):
    t = re.sub(r"&\s*'\w+\s*", "&", t)
    t = re.sub(r"'\w+\s*,\s*", "", t)
    t = re.sub(r"<\s*'\w+\s*>", "", t)
    t = re.sub(r",\s*'\w+\s*", "", t)
    t = re.sub(r"\bdyn\s+", "dyn ", t)
    return t.strip()


def _rust_storage(ty):
    """(storage type or None, pass prefix) for a Rust parameter type."""
    t = _strip_lifetimes(ty)
    prefix = ""
    m = re.match(r"^&\s*(mut\b)?\s*(.*)$", t, re.S)
    if m:
        prefix = "&mut " if m.group(1) else "&"
        t = m.group(2).strip()
    if t == "str":
        t = "String"
    elif t.startswith("[") and t.endswith("]") and len(_split_top(t[1:-1], ";")) == 1:
        t = "Vec<" + t[1:-1].strip() + ">"
    elif re.match(r"^impl\s+(AsRef\s*<\s*str\s*>|Into\s*<\s*String\s*>|ToString|Display|std::fmt::Display)", t):
        t = "String"
    elif re.match(r"^impl\s+(?:std::iter::)?IntoIterator\s*<\s*Item\s*=", t):
        t = "Vec<" + re.match(r"^impl\s+(?:std::iter::)?IntoIterator\s*<\s*Item\s*=\s*(.*)>$", t, re.S).group(1).strip() + ">"
    elif re.match(r"^impl\s+(?:std::iter::)?Iterator\s*<\s*Item\s*=", t):
        inner = re.match(r"^impl\s+(?:std::iter::)?Iterator\s*<\s*Item\s*=\s*(.*)>$", t, re.S).group(1).strip()
        return "Vec<" + inner + ">", "iter:"
    elif re.match(r"^impl\b", t) or re.match(r"^dyn\b", t):
        return None, prefix
    return t, prefix


def _rust_generic_subst(generics):
    """Concrete stand-ins for a function's generic type parameters."""
    out = {}
    for g in _split_top(generics):
        g = g.strip()
        if not g or g.startswith("'") or g.startswith("const "):
            continue
        m = re.match(r"^([A-Za-z_]\w*)\s*(?::\s*(.*))?$", g, re.S)
        if not m:
            continue
        bounds = m.group(2) or ""
        if re.search(r"\b(AsRef\s*<\s*str|Into\s*<\s*String|Display|ToString)\b", bounds):
            out[m.group(1)] = "String"
        elif re.search(r"\b(Float)\b", bounds):
            out[m.group(1)] = "f64"
        else:
            out[m.group(1)] = "i64"
    return out


def _rust_fn_sig(code, pos):
    """Parse ``fn name<...>(params) -> ret`` starting at the ``fn`` keyword."""
    m = re.compile(r"fn\s+\w+\s*").match(code, pos)
    k = m.end()
    generics = ""
    if k < len(code) and code[k] == "<":
        e = _match_close(code, k, "<", ">")
        generics = code[k + 1:e]
        k = e + 1
    while k < len(code) and code[k].isspace():
        k += 1
    if k >= len(code) or code[k] != "(":
        return None
    e = _match_close(code, k, "(", ")")
    params = _split_top(code[k + 1:e])
    return generics, [p.strip() for p in params if p.strip()]


def rust_glue(src, entrypoint):
    """Rust source of ``__bcg_entry`` calling the solution's entrypoint, plus extra items."""
    parts = entrypoint.split(".")
    want_cls = parts[0] if len(parts) == 2 else None
    code = _blank_code(src, "rust")
    meth = _pick_name(code, parts[-1], r"\bfn\s+%s\b")
    blocks = _blocks(code)
    cands = []
    for m in re.finditer(r"\bfn\s+" + re.escape(meth) + r"\b", code):
        encl = _enclosing(blocks, m.start())
        owner = None
        inside_fn = False
        mods = []
        for s, e, h in encl:
            hs = h.strip()
            im = re.search(r"\bimpl\b(?:\s*<[^{]*?>)?\s+(?:[\w:<>, ]+\s+for\s+)?(?:\w+::)*(\w+)\s*(?:<[^{]*>)?\s*(?:where[^{]*)?$", hs)
            if im and owner is None and not mods:
                owner = im.group(1)
            elif re.search(r"\bmod\s+(\w+)\s*$", hs):
                if re.search(r"\bcfg\s*\(\s*test\s*\)", hs):
                    inside_fn = True
                    break
                mods.append(re.search(r"\bmod\s+(\w+)\s*$", hs).group(1))
            elif re.search(r"\btrait\s+\w+", hs):
                inside_fn = True
                break
            else:
                inside_fn = True
                break
        if inside_fn:
            continue
        cands.append((owner, list(reversed(mods)), m.start()))
    chosen = None
    if want_cls:
        chosen = next((c for c in cands if c[0] == want_cls), None)
    if chosen is None:
        chosen = next((c for c in cands if c[0] is None and not c[1]), None)
    if chosen is None:
        chosen = next((c for c in cands if c[0] is None), None)
    if chosen is None and cands:
        chosen = cands[0]
    extra = []
    if chosen is None:
        if want_cls:
            call_base, has_self, params, self_mut = f"{want_cls}::{meth}", False, None, False
        else:
            call_base, has_self, params, self_mut = meth, False, None, False
        owner = want_cls
        modpath = ""
    else:
        owner, mods, pos = chosen
        sig = _rust_fn_sig(code, pos)
        params = sig[1] if sig else None
        modpath = "".join(m + "::" for m in mods)
        has_self = False
        self_mut = False
        if params and re.match(r"^(&\s*('\w+\s*)?(mut\s+)?)?(mut\s+)?self\b", params[0]):
            has_self = True
            self_mut = bool(re.match(r"^&\s*('\w+\s*)?mut\b", params[0])) or params[0].startswith("mut ")
            params = params[1:]
        if owner:
            call_base = f"{modpath}{owner}::{meth}"
        else:
            call_base = f"{modpath}{meth}"
    if owner and not re.search(r"\b(struct|enum|type|union)\s+" + re.escape(owner) + r"\b", code):
        extra.append(f"pub struct {owner};")
    names = []
    for p in params or []:
        pm = re.match(r"^(?:mut\s+)?(?:ref\s+)?([A-Za-z_]\w*)\s*:", p)
        names.append(pm.group(1) if pm else "")
    NATIVE_PARAMS[:] = names
    extra.append("static BCG_PARAM_NAMES: &[&str] = &[" + "".join(_rs_str(x) + ", " for x in names) + "];")
    lines = ["#[allow(unused_mut, unused_variables, non_snake_case, unused_parens, dead_code)]",
             "fn __bcg_entry(__args: &[bcg::Val], __cap: &mut bcg::Capture) -> bcg::Outcome {"]
    call_args = []
    post = []
    if params is None:
        n = None
    else:
        n = len(params)
        lines.append(f"    if __args.len() != {n} {{ return bcg::Outcome::conv(format!(\"entrypoint takes {n} arguments, got {{}}\", __args.len())); }}")
        gsubst = _rust_generic_subst(sig[0] if chosen is not None and sig else "")
        for i, p in enumerate(params):
            pm = re.match(r"^(?:mut\s+)?([^:]+?)\s*:\s*(.*)$", p, re.S)
            ty = pm.group(2) if pm else "_"
            for gname, gty in gsubst.items():
                ty = re.sub(r"\b%s\b" % re.escape(gname), gty, ty)
            storage, prefix = _rust_storage(ty)
            ann = f": {storage}" if storage else ""
            lines.append(f"    let mut __a{i}{ann} = match bcg::from_arg(&__args[{i}]) {{ Ok(v) => v, Err(e) => return bcg::Outcome::conv(e) }};")
            if prefix == "iter:":
                lines.append(f"    let __p{i} = bcg::ToVal::to_val(&__a{i});")
                call_args.append(f"__a{i}.into_iter()")
                post.append(f"__p{i}")
            elif prefix:
                call_args.append(f"{prefix}__a{i}")
                post.append(f"bcg::ToVal::to_val(&__a{i})")
            else:
                lines.append(f"    let __p{i} = bcg::ToVal::to_val(&__a{i});")
                call_args.append(f"__a{i}")
                post.append(f"__p{i}")
    if n is None:
        lines.append("    return bcg::Outcome::conv(String::from(\"entrypoint not found in solution\"));")
        lines.append("}")
        return "\n".join(extra + lines) + "\n"
    if has_self:
        ctor = _rust_ctor(code, owner)
        lines.append(f"    let mut __obj = {modpath}{ctor};")
        call = f"__obj.{meth}({', '.join(call_args)})"
    else:
        call = f"{call_base}({', '.join(call_args)})"
    lines.append("    __cap.start();")
    lines.append(f"    let __r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {{ #[allow(unused_imports)] use bcg::{{DoneFut as _, DoneVal as _}}; let __ret = bcg::Done(std::cell::Cell::new(Some({call}))).bcg_done(); bcg::ToVal::to_ret(&__ret) }}));")
    lines.append("    let __c = __cap.stop();")
    lines.append(f"    let __post = vec![{', '.join(post)}];")
    lines.append("    bcg::Outcome::finish(__r, __post, __c)")
    lines.append("}")
    return "\n".join(extra + lines) + "\n"


def _rust_ctor(code, owner):
    for s, e, h in _blocks(code):
        if re.search(r"\bimpl\b(?:\s*<[^{]*?>)?\s+(?:\w+::)*" + re.escape(owner) + r"\s*(?:<[^{]*>)?\s*$", h.strip()):
            body = code[s:e]
            m = re.search(r"\bfn\s+new\s*\(\s*\)", body)
            if m:
                return f"{owner}::new()"
    if re.search(r"\bstruct\s+" + re.escape(owner) + r"\s*;", code):
        return owner
    if re.search(r"\bstruct\s+" + re.escape(owner) + r"\s*\{\s*\}", code):
        return owner + " {}"
    if not re.search(r"\b(struct|enum|type|union)\s+" + re.escape(owner) + r"\b", code):
        return owner
    return f"<{owner} as ::std::default::Default>::default()"


_RUST_OWNED_KEY_RE = re.compile(
    r"\.(get|get_mut|contains_key|remove|get_key_value)\(\s*(?=(String::from\s*\(|format!\s*\(|\"(?:[^\"\\]|\\.)*\"\s*\.\s*(?:to_string|to_owned|into)\s*\(\s*\)))")


def rust_prepare_solution(src):
    """Make a Rust solution includable next to the tester (``main`` renamed, owned-key lookups)."""
    code = _blank_code(src, "rust")
    out = []
    last = 0
    for m in re.finditer(r"\bfn\s+(main)\s*\(", code):
        out.append(src[last:m.start(1)])
        out.append("__bcg_user_main")
        last = m.end(1)
    out.append(src[last:])
    src = "".join(out)
    code = _blank_code(src, "rust")
    out, last = [], 0
    for m in _RUST_OWNED_KEY_RE.finditer(code):
        k = m.end()
        # skip when the argument is already a reference
        out.append(src[last:k])
        out.append("&")
        last = k
    out.append(src[last:])
    return "".join(out)


# ---- compilation -----------------------------------------------------------------------

def _native_cache_dir():
    d = os.path.join(tempfile.gettempdir(), "bcg-native-cache-%d" % os.getuid() if hasattr(os, "getuid") else "bcg-native-cache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return d


def _compile_cached(key_parts, attempts, workdir):
    """Run compile attempts [(argv, binary_path)] until one produces a binary; cache it."""
    import hashlib
    h = hashlib.sha256()
    for p in key_parts:
        h.update(p.encode("utf-8", "surrogateescape") if isinstance(p, str) else p)
        h.update(b"\0")
    key = h.hexdigest()
    cache = _native_cache_dir()
    final = os.path.join(workdir, "tester-bin")
    if cache:
        cached = os.path.join(cache, key)
        if os.path.isfile(cached) and os.access(cached, os.X_OK):
            try:
                shutil.copyfile(cached, final)
                os.chmod(final, 0o755)
                return final
            except OSError:
                pass
    for argv, out in attempts:
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=600, cwd=workdir)
        except Exception:
            continue
        if proc.returncode == 0 and os.path.isfile(out):
            if out != final:
                shutil.move(out, final)
            if cache:
                try:
                    tmp = os.path.join(cache, ".%s.%d" % (key, os.getpid()))
                    shutil.copyfile(final, tmp)
                    os.chmod(tmp, 0o755)
                    os.replace(tmp, os.path.join(cache, key))
                except OSError:
                    pass
            return final
        if os.environ.get("BCG_DEBUG"):
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    return None


def build_native(lang, tester_path, solution, workdir):
    with open(tester_path, encoding="utf-8") as f:
        tester_src = f.read()
    with open(solution, encoding="utf-8", errors="surrogateescape") as f:
        sol_src = f.read()
    meta = read_meta(tester_path) or {}
    ep = meta.get("entrypoint") or ""
    if lang == "cpp":
        cxx = os.environ.get("CXX") or shutil.which("g++") or shutil.which("clang++") or shutil.which("c++")
        if not cxx:
            return None
        sol_copy = os.path.join(workdir, "solution_src.cpp")
        with open(sol_copy, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(sol_src)
        tester_copy = os.path.join(workdir, "tester_src.cpp")
        with open(tester_copy, "w", encoding="utf-8") as f:
            f.write(tester_src)
        entry = cpp_entry_expr(sol_src, ep)
        names = cpp_param_names(sol_src, entry)
        NATIVE_PARAMS[:] = names
        attempts = []
        variants = []
        for std in ("gnu++20", "gnu++17"):
            for using in (False, True):
                variants.append((std, using))
        for idx, (std, using) in enumerate(variants):
            main_src = os.path.join(workdir, f"main{idx}.cpp")
            with open(main_src, "w", encoding="utf-8") as f:
                f.write("#if __has_include(<bits/stdc++.h>)\n#include <bits/stdc++.h>\n#endif\n")
                if using:
                    f.write("using namespace std;\n")
                f.write("#define main bcg_user_main_\n")
                f.write('#include "solution_src.cpp"\n')
                f.write("#undef main\n")
                f.write(f"#define BCG_ENTRY_PTR ({entry})\n")
                f.write("#define BCG_PARAM_NAMES {" + "".join(_c_str(x or "") + ", " for x in names) + "nullptr}\n")
                f.write('#include "tester_src.cpp"\n')
            out = os.path.join(workdir, f"bin{idx}")
            attempts.append(([cxx, f"-std={std}", "-O2", "-w", "-pthread", "-o", out, main_src], out))
        return _compile_cached(["cpp", cxx, entry, repr(names), sol_src, tester_src], attempts, workdir)
    rustc = os.environ.get("RUSTC") or shutil.which("rustc")
    if not rustc:
        return None
    prepared = rust_prepare_solution(sol_src)
    glue = rust_glue(sol_src, ep)
    main_src = os.path.join(workdir, "main.rs")
    with open(main_src, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(prepared)
        f.write("\n\n// ---- babel_code_goat tester ----\n")
        f.write(tester_src)
        f.write("\n")
        f.write(glue)
    attempts = []
    for edition in ("2021", "2018"):
        out = os.path.join(workdir, f"bin-{edition}")
        attempts.append(([rustc, "--edition", edition, "-O", "-A", "warnings", "--crate-name", "bcg_tester",
                          "-o", out, main_src], out))
    return _compile_cached(["rust", rustc, prepared, tester_src, glue], attempts, workdir)


# ---- judging ---------------------------------------------------------------------------

class NativeJudge:
    """Evaluates native outcomes with the Python tester's semantics."""

    def __init__(self, tests, tol=None):
        ns = {"__name__": "bcg_native_judge"}
        exec(compile(PY_RUNTIME, "<babel_code_goat-runtime>", "exec"), ns)
        ns["_NATIVE"][0] = True
        if tol is not None:
            ns["_TOL"]["abs"] = float(tol)
            ns["_TOL"]["rel"] = 0.0
        self.ns = ns
        self.params = list(NATIVE_PARAMS)
        self.tests = {}
        for t in tests:
            self.tests.setdefault(t["id"], t)

    def judge(self, ev):
        import copy
        t = self.tests.get(ev.get("id"))
        if t is None or "conv" in ev:
            return False
        ns = self.ns
        decimals = _involves_decimal(t)
        out = {
            "stdout": bytes.fromhex(ev.get("out", "")).decode("utf-8", "surrogateescape"),
            "stderr": bytes.fromhex(ev.get("err", "")).decode("utf-8", "surrogateescape"),
        }
        raised = None
        result = raw = None
        try:
            if "exc" in ev:
                msg = bytes.fromhex(ev["exc"]).decode("utf-8", "replace")
                raised = RuntimeError(msg)
            else:
                post = native_dec(ev.get("post", "l[]"), decimals)
                result = raw = native_dec(ev.get("ret", "N"), decimals)
                npos = len(t.get("args") or [])
                kwpost = {}
                for name in (t.get("kwargs") or {}):
                    if name in self.params:
                        idx = self.params.index(name)
                        if idx < len(post):
                            kwpost[name] = post[idx]
                xf = t.get("xf")
                if xf is not None:
                    try:
                        result = ns["_ev"](xf, raw, None, post[:npos], kwpost)
                    except Exception as e:
                        raised = e
        except Exception as e:
            return False
        expected = t.get("expected")
        tt = {
            "id": t["id"], "kind": t["kind"], "extra": t.get("extra") or {},
            "expected": lambda: copy.deepcopy(expected),
            "xf": lambda: t.get("xf"),
            "stdout": t.get("stdout"), "stderr": t.get("stderr"),
        }
        try:
            ok, _msg = ns["_judge"](tt, result, raw, raised, out)
        except Exception:
            return False
        return bool(ok)


def validate_entrypoint(ep):
    if not ep:
        return False
    parts = ep.split(".")
    if len(parts) > 2:
        return False
    return all(IDENT_RE.match(p) for p in parts)


def cmd_generate(opts):
    if len(opts["pos"]) < 1:
        raise GenerateError("missing <tests_dir>")
    tests_dir = opts["pos"][0]
    lang = opts.get("lang")
    if lang not in LANGS:
        raise GenerateError(f"--lang must be one of {', '.join(LANGS)}")
    ep = opts.get("entrypoint")
    if ep is None or not validate_entrypoint(ep):
        raise GenerateError("--entrypoint must be a valid function or Class.method name")
    if not os.path.isdir(tests_dir):
        raise GenerateError(f"{tests_dir} is not a directory")
    try:
        tests, _ = discover(tests_dir, ep)
    except DiscoveryError as e:
        raise GenerateError(str(e))
    if lang == "python":
        content = render_python(tests, ep)
    elif lang in NATIVE_LANGS:
        content = render_native(tests, ep, lang)
    else:
        content = render_js(tests, ep, lang)
    target = os.path.join(tests_dir, LANGS[lang])
    fd, tmp = tempfile.mkstemp(prefix=".bcg-", dir=tests_dir)
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
    return 0


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def read_meta(tester_path):
    try:
        with open(tester_path, encoding="utf-8") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                idx = line.find(META_MARKER)
                if idx >= 0:
                    return json.loads(line[idx + len(META_MARKER):].strip())
    except Exception:
        pass
    return None


def node_supports_strip():
    node = shutil.which("node")
    if not node:
        return None, False
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        m = re.match(r"v(\d+)\.(\d+)", out)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            return node, (major, minor) >= (22, 6)
    except Exception:
        pass
    return node, False


def build_command(lang, tester_path, workdir, solution=None):
    """Return argv prefix for running the tester or None if impossible."""
    if lang in NATIVE_LANGS:
        try:
            binary = build_native(lang, tester_path, solution, workdir)
        except Exception:
            return None
        return [binary] if binary else None
    if lang == "python":
        return [sys.executable, tester_path]
    node = shutil.which("node")
    if not node:
        return None
    if lang == "javascript":
        # Run a copy as CommonJS-agnostic script; the tester only uses builtins.
        return [node, tester_path]
    node, strip = node_supports_strip()
    if strip:
        return [node, "--experimental-strip-types", "--no-warnings", tester_path]
    tsc = shutil.which("tsc")
    if not tsc:
        return None
    out_dir = os.path.join(workdir, "tsout")
    os.makedirs(out_dir, exist_ok=True)
    try:
        subprocess.run(
            [tsc, "--outDir", out_dir, "--target", "es2020", "--module", "commonjs",
             "--skipLibCheck", "--noEmitOnError", "false", tester_path],
            capture_output=True, timeout=300,
        )
    except Exception:
        return None
    js = os.path.join(out_dir, "tester.js")
    if not os.path.isfile(js):
        return None
    cjs = os.path.join(out_dir, "tester.cjs")
    shutil.copyfile(js, cjs)
    return [node, cjs]


def parse_events(path):
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.endswith("\n"):
                    break
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return events


def _kill(proc, usage=None):
    try:
        os.killpg(proc.pid, 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        _reap(proc, True, usage)
    except Exception:
        pass


def _reap(proc, block, usage):
    """wait4()-based poll that records the child's peak RSS (KB) into ``usage``."""
    if proc.returncode is not None:
        return proc.returncode
    try:
        pid, status, ru = os.wait4(proc.pid, 0 if block else os.WNOHANG)
    except ChildProcessError:
        proc.returncode = 0 if proc.returncode is None else proc.returncode
        return proc.returncode
    except Exception:
        return proc.wait() if block else proc.poll()
    if pid == 0:
        return None
    if usage is not None:
        rss = ru.ru_maxrss
        if sys.platform == "darwin":
            rss //= 1024
        usage.append(rss)
    proc.returncode = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
    return proc.returncode


def run_tester(cmd_prefix, solution, ids, timeout, workdir, lang, tol=None, judge=None,
               deadline=None, startup=None, usage=None):
    """Run tests; return (results: id->bool, error: bool).

    ``timeout`` bounds each test (seconds without progress); ``startup`` bounds the time
    until the first test starts; ``deadline`` (time.monotonic()) bounds the whole run --
    tests still running or not yet executed when it passes are left without a result."""
    results = {}
    remaining = list(ids)
    error = False
    attempt = 0
    if startup is None:
        startup = timeout
    while remaining:
        if deadline is not None and time.monotonic() >= deadline:
            break
        attempt += 1
        res_path = os.path.join(workdir, f"results{attempt}.jsonl")
        ids_path = os.path.join(workdir, f"ids{attempt}.json")
        with open(ids_path, "w", encoding="utf-8") as f:
            if lang in NATIVE_LANGS:
                f.write("".join(_native_id(i) + "\n" for i in remaining))
            else:
                json.dump(remaining, f)
        open(res_path, "w").close()
        cmd = cmd_prefix + [solution, "--results", res_path, "--ids", ids_path]
        if tol is not None:
            cmd += ["--tol", repr(float(tol))]
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.pop("NODE_OPTIONS", None)
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env, start_new_session=True,
            )
        except Exception:
            error = True
            break
        last_size = 0
        last_progress = time.monotonic()
        killed = False
        expired = False
        while True:
            rc = _reap(proc, False, usage)
            try:
                size = os.path.getsize(res_path)
            except OSError:
                size = 0
            now = time.monotonic()
            if size != last_size:
                last_size = size
                last_progress = now
            if rc is not None:
                break
            if deadline is not None and now >= deadline:
                killed = expired = True
                _kill(proc, usage)
                break
            if now - last_progress > (timeout if last_size else startup):
                killed = True
                _kill(proc, usage)
                break
            time.sleep(0.005)
        events = parse_events(res_path)
        started = None
        done = False
        load_error = False
        progressed = False
        for ev in events:
            kind = ev.get("event")
            if kind == "load_error":
                load_error = True
            elif kind == "start":
                started = ev.get("id")
                progressed = True
            elif kind == "result":
                rid = ev.get("id")
                if rid not in results:
                    results[rid] = bool(ev.get("ok"))
                started = None
            elif kind == "value" and judge is not None:
                rid = ev.get("id")
                if rid not in results:
                    results[rid] = judge(ev)
                started = None
            elif kind == "done":
                done = True
        if load_error:
            error = True
            break
        if started is not None and started not in results:
            results[started] = False  # crashed or timed out mid-test
        remaining = [i for i in remaining if i not in results]
        if done or expired:
            break
        if not progressed:
            error = True
            break
        _ = killed
    return results, error


def emit_result(status, passed, failed):
    sys.stdout.write(json.dumps({"status": status, "passed": passed, "failed": failed}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return {"pass": 0, "fail": 1}.get(status, 2)


def _prepare(opts, profile=False):
    """Shared front half of ``test`` / ``profile``: resolve paths, discover and select tests.

    Returns ("exit", (status, passed, failed)) or ("run", ctx)."""
    pos = list(opts["pos"])
    if opts.get("list_tests") and len(pos) == 1:
        pos = [pos[0], None] if profile else [None, pos[0]]
    if len(pos) < 2:
        return "exit", ("error", [], [])
    if profile:
        tests_dir, solution = pos[0], pos[1]
        if solution and not os.path.isdir(tests_dir) and os.path.isdir(solution):
            tests_dir, solution = solution, tests_dir
    else:
        solution, tests_dir = pos[0], pos[1]
        if solution and os.path.isdir(solution) and not os.path.isdir(tests_dir):
            solution, tests_dir = tests_dir, solution
    lang = opts.get("lang")
    tester_path = os.path.join(tests_dir, LANGS[lang]) if lang in LANGS else None
    if profile and (tester_path is None or not os.path.isfile(tester_path)):
        return "exit", ("error", [], [])
    meta = read_meta(tester_path) if tester_path and os.path.isfile(tester_path) else None
    ep = opts.get("entrypoint")
    if not ep and meta and isinstance(meta.get("entrypoint"), str):
        ep = meta["entrypoint"]
    excl = [solution] if solution else []
    try:
        tests, ep = discover(tests_dir, ep, excl)
    except DiscoveryError:
        if ep is not None:
            try:
                tests, ep = discover(tests_dir, None, excl)
            except DiscoveryError:
                return "exit", ("error", [], [])
        else:
            return "exit", ("error", [], [])
    ids = []
    for t in tests:
        if t["id"] not in ids:
            ids.append(t["id"])
    selected = opts.get("run")
    if selected is not None:
        known = set(ids)
        if not selected or any(x not in known for x in selected):
            return "exit", ("error", [], [x for x in selected if x in known])
        ids = [i for i in ids if i in set(selected)]
        tests = [t for t in tests if t["id"] in set(selected)]
    if opts.get("list_tests"):
        return "exit", ("pass", ids, [])
    if lang not in LANGS or not os.path.isfile(tester_path) or not solution or not os.path.isfile(solution):
        return "exit", ("error", [], ids)
    timeout = opts.get("timeout", DEFAULT_TIMEOUT)
    startup = timeout
    if "timeout_ms" in opts:
        timeout = opts["timeout_ms"] / 1000.0
        startup = max(timeout, opts.get("timeout", DEFAULT_TIMEOUT))
    return "run", {"solution": os.path.abspath(solution), "tester": os.path.abspath(tester_path),
                   "lang": lang, "tests": tests, "ids": ids, "timeout": timeout, "startup": startup,
                   "total": opts.get("total_timeout_ms"), "tol": opts.get("tol")}


def _run_once(ctx, prefix, workdir, usage=None):
    """Execute the selected tests once; returns (results id->bool, error)."""
    lang, tests, ids = ctx["lang"], ctx["tests"], ctx["ids"]
    static = {t["id"]: bool(t["ok"]) for t in tests if t["kind"] == "loop"}
    run_ids = [i for i in ids if i not in static]
    judge = NativeJudge(tests, ctx["tol"]).judge if lang in NATIVE_LANGS else None
    total = ctx["total"]
    deadline = None if total is None else time.monotonic() + total / 1000.0
    results, error = run_tester(prefix, ctx["solution"], run_ids, ctx["timeout"], workdir,
                                lang, ctx["tol"], judge, deadline, ctx["startup"], usage)
    results.update(static)
    return results, error


def cmd_test(opts):
    action, val = _prepare(opts)
    if action == "exit":
        return emit_result(*val)
    ctx = val
    ids = ctx["ids"]
    workdir = tempfile.mkdtemp(prefix="bcg-run-")
    try:
        prefix = build_command(ctx["lang"], ctx["tester"], workdir, ctx["solution"])
        if prefix is None:
            return emit_result("error", [], ids)
        results, error = _run_once(ctx, prefix, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    passed = [i for i in ids if results.get(i) is True]
    failed = [i for i in ids if results.get(i) is not True]
    if error:
        return emit_result("error", passed, failed)
    return emit_result("pass" if not failed else "fail", passed, failed)


def _stats(values):
    if not values:
        return {"mean": 0.0, "std": 0.0}
    mean = math.fsum(values) / len(values)
    var = math.fsum((v - mean) ** 2 for v in values) / len(values)
    return {"mean": mean, "std": math.sqrt(var)}


def emit_profile(status, passed, failed, opts, runtimes=(), memories=(), trials=0, warmup=0):
    out = {"status": status, "passed": passed, "failed": failed,
           "runtime_ns": _stats(list(runtimes))}
    if opts.get("memory"):
        out["memory_kb"] = _stats(list(memories))
    out["trials"] = trials
    out["warmup"] = warmup
    sys.stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return {"pass": 0, "fail": 1}.get(status, 2)


def cmd_profile(opts):
    n = opts.get("trials", 1)
    k = opts.get("warmup", 0)
    if k >= n:
        return emit_profile("error", [], [], opts)
    action, val = _prepare(opts, profile=True)
    if action == "exit":
        return emit_profile(*val, opts)
    ctx = val
    ids = ctx["ids"]
    runtimes, memories = [], []
    ever_failed = set()
    error = False
    workdir = tempfile.mkdtemp(prefix="bcg-prof-")
    try:
        prefix = build_command(ctx["lang"], ctx["tester"], workdir, ctx["solution"])
        if prefix is None:
            return emit_profile("error", [], ids, opts)
        for trial in range(n):
            tdir = os.path.join(workdir, "trial%d" % trial)
            os.makedirs(tdir, exist_ok=True)
            usage = []
            t0 = time.perf_counter_ns()
            results, err = _run_once(ctx, prefix, tdir, usage)
            elapsed = time.perf_counter_ns() - t0
            if trial < k:
                continue
            runtimes.append(float(elapsed))
            memories.append(float(max(usage)) if usage else 0.0)
            error = error or err
            ever_failed.update(i for i in ids if results.get(i) is not True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    passed = [i for i in ids if i not in ever_failed]
    failed = [i for i in ids if i in ever_failed]
    status = "error" if error else ("pass" if not failed else "fail")
    return emit_profile(status, passed, failed, opts, runtimes, memories, n, k)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    opts = {"pos": []}
    valued = {"--entrypoint": "entrypoint", "--lang": "lang", "--timeout": "timeout", "--tol": "tol",
              "--timeout-ms": "timeout_ms", "--total-timeout-ms": "total_timeout_ms", "--run": "run"}
    flags = {"--list-tests": "list_tests", "--memory": "memory"}
    valued.update({"-n": "trials", "--trials": "trials", "--warmup": "warmup"})

    def put(key, v):
        if key == "run":
            opts.setdefault("run", []).append(v)
        else:
            opts[key] = v
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-n") and a != "-n" and len(a) > 2:
            put("trials", a[3:] if a[2] == "=" else a[2:])
        elif a.startswith("--") or a == "-n":
            if "=" in a:
                k, v = a.split("=", 1)
                if k in valued:
                    put(valued[k], v)
                elif k in flags:
                    raise ValueError(f"{k} does not take a value")
            elif a in flags:
                opts[flags[a]] = True
            elif a in valued:
                if i + 1 >= len(argv):
                    raise ValueError(f"{a} requires a value")
                put(valued[a], argv[i + 1])
                i += 1
            # unknown boolean flags are ignored
        else:
            opts["pos"].append(a)
        i += 1
    if "timeout" in opts:
        try:
            opts["timeout"] = float(opts["timeout"])
            if not opts["timeout"] > 0:
                raise ValueError
        except ValueError:
            raise ValueError("--timeout must be a positive number")
    for key in ("timeout_ms", "total_timeout_ms"):
        if key in opts:
            flag = "--" + key.replace("_", "-")
            try:
                v = int(str(opts[key]).strip())
            except ValueError:
                raise ValueError(f"{flag} must be a positive integer")
            if v <= 0:
                raise ValueError(f"{flag} must be a positive integer")
            opts[key] = v
    for key, flag, lo in (("trials", "-n", 1), ("warmup", "--warmup", 0)):
        if key in opts:
            try:
                v = int(str(opts[key]).strip())
            except ValueError:
                raise ValueError(f"{flag} must be an integer >= {lo}")
            if v < lo:
                raise ValueError(f"{flag} must be an integer >= {lo}")
            opts[key] = v
    if "tol" in opts:
        try:
            opts["tol"] = float(opts["tol"])
            if not (opts["tol"] >= 0 and math.isfinite(opts["tol"])):
                raise ValueError
        except ValueError:
            raise ValueError("--tol must be a non-negative number")
    return opts


def main(argv):
    if not argv or argv[0] not in ("generate", "test", "profile"):
        sys.stderr.write(__doc__)
        return 2
    cmd = argv[0]
    try:
        opts = parse_args(argv[1:])
    except ValueError as e:
        if cmd == "test":
            return emit_result("error", [], [])
        if cmd == "profile":
            return emit_profile("error", [], [], {"memory": "--memory" in argv})
        sys.stderr.write(f"error: {e}\n")
        return 2
    if cmd == "generate":
        try:
            return cmd_generate(opts)
        except GenerateError as e:
            sys.stderr.write(f"error: {e}\n")
            return 1
        except Exception as e:
            sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
            return 1
    if cmd == "profile":
        try:
            return cmd_profile(opts)
        except Exception:
            return emit_profile("error", [], [], opts)
    try:
        return cmd_test(opts)
    except Exception:
        return emit_result("error", [], [])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
