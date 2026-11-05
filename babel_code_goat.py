#!/usr/bin/env python3
"""babel_code_goat: translate a Python ``tests.py`` harness into a tester for
python / javascript / typescript and run it against a solution.

Commands:
    generate <tests_dir> --entrypoint <name> --lang <python|javascript|typescript>
    test <solution_path> <tests_dir> --lang <python|javascript|typescript> [--timeout SECONDS] [--tol FLOAT]
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

LANGS = {"python": "tester.py", "javascript": "tester.js", "typescript": "tester.ts"}
META_MARKER = "BABEL_CODE_GOAT_META:"
DEFAULT_TIMEOUT = 10.0
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EXPECT_RE = re.compile(r"^#\s*expect_(stdout|stderr)\s*:\s*(.*?)\s*$")


class DiscoveryError(Exception):
    pass


class GenerateError(Exception):
    pass


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
                          "match", "fullmatch", "raises", "isinstance", "type", "print"}

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


KNOWN_MODULES = {"collections", "decimal", "math", "re", "builtins", "pytest", "typing"}


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


class Discoverer:
    def __init__(self, source, entrypoint):
        self.source = source
        self.entrypoint = entrypoint
        self.tests = []  # dicts
        self.callees = []
        self.local_funcs = set()
        self.ctx = Context()

    # -- helpers
    def lit(self, node):
        return literal_value(node, self.ctx)

    def resolve(self, node):
        """Resolve a name bound to an entrypoint call back to the call node."""
        if isinstance(node, ast.Name) and node.id in self.ctx.calls:
            return self.ctx.calls[node.id]
        return node

    def is_entry_call(self, node):
        node = self.resolve(node)
        if not isinstance(node, ast.Call):
            return False
        name = callee_name(node.func)
        if name is None:
            return False
        if self.entrypoint is not None and name == self.entrypoint:
            return True
        if is_value_call(node, self.ctx, self.entrypoint):
            return False
        short = self.ctx.short(node.func)
        if short in HELPERS and short != (self.entrypoint or "").split(".")[-1]:
            return False
        if name in self.local_funcs and name != self.entrypoint:
            return False
        return callee_matches(name, self.entrypoint)

    def parse_call(self, node):
        node = self.resolve(node)
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
                raise DiscoveryError(f"line {node.lineno}: starred arguments not allowed")
            args.append(self.lit(a))
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise DiscoveryError(f"line {node.lineno}: ** arguments not allowed")
            kwargs[kw.arg] = self.lit(kw.value)
        self.callees.append(name)
        return args, kwargs

    def split_call(self, a, b, lineno):
        """Given two operands, return (call_node, other_node, call_is_left)."""
        if self.is_entry_call(a):
            return self.resolve(a), b, True
        if self.is_entry_call(b):
            return self.resolve(b), a, False
        raise DiscoveryError(f"line {lineno}: assertion must call the entrypoint")

    # -- statements
    def add_test(self, node, kind, args, kwargs, expected=_MISSING, extra=None):
        t = {"line": node.lineno, "col": node.col_offset, "kind": kind, "args": args,
             "kwargs": kwargs, "extra": extra or {}}
        if expected is not _MISSING:
            t["expected"] = expected
        self.tests.append(t)

    def _num(self, v, lineno):
        if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
            raise DiscoveryError(f"line {lineno}: tolerance must be a number")
        return float(v)

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
        call, other, _ = self.split_call(sub.left, sub.right, node.lineno)
        args, kwargs = self.parse_call(call)
        expected = self.lit(other)
        tol = self._num(self.lit(toln), node.lineno)
        self.add_test(node, "close", args, kwargs, expected,
                      {"abs_tol": tol, "rel_tol": 0.0, "strict": strict})
        return True

    def try_isclose(self, node, test, negate=False):
        if not (isinstance(test, ast.Call) and self.ctx.short(test.func) == "isclose"
                and not self.is_entry_call(test)):
            return False
        if len(test.args) != 2:
            raise DiscoveryError(f"line {node.lineno}: isclose needs two arguments")
        call, other, _ = self.split_call(test.args[0], test.args[1], node.lineno)
        args, kwargs = self.parse_call(call)
        expected = self.lit(other)
        extra = {"abs_tol": 0.0, "rel_tol": 1e-09, "strict": False}
        for kw in test.keywords:
            if kw.arg not in ("abs_tol", "rel_tol"):
                raise DiscoveryError(f"line {node.lineno}: unsupported isclose argument {kw.arg}")
            extra[kw.arg] = self._num(self.lit(kw.value), node.lineno)
        extra["isclose"] = True
        self.add_test(node, "notclose" if negate else "close", args, kwargs, expected, extra)
        return True

    def handle_assert(self, node):
        if node.msg is not None:
            try:
                self.lit(node.msg)
            except DiscoveryError:
                pass  # messages are never evaluated by the harness
        test = node.test
        if self.try_abs_close(node, test) or self.try_isclose(node, test):
            return
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
                and self.try_isclose(node, test.operand, negate=True):
            return
        if isinstance(test, ast.Compare):
            if len(test.ops) != 1 or len(test.comparators) != 1:
                raise DiscoveryError(f"line {node.lineno}: chained comparisons not allowed")
            op = test.ops[0]
            left, right = test.left, test.comparators[0]
            if isinstance(op, (ast.In, ast.NotIn)):
                if not self.is_entry_call(left):
                    raise DiscoveryError(f"line {node.lineno}: 'in' assertion must call the entrypoint on the left")
                args, kwargs = self.parse_call(left)
                expected = self.lit(right)
                if not isinstance(expected, (list, tuple, set, frozenset, dict, str, collections.deque)):
                    raise DiscoveryError(f"line {node.lineno}: 'in' needs a container literal")
                self.add_test(node, "in" if isinstance(op, ast.In) else "notin", args, kwargs, expected)
                return
            call, other, call_left = self.split_call(left, right, node.lineno)
            if isinstance(op, (ast.Is, ast.IsNot)):
                kind = "is" if isinstance(op, ast.Is) else "isnot"
                if not (isinstance(other, ast.Constant)
                        and (other.value is None or isinstance(other.value, bool))):
                    raise DiscoveryError(f"line {node.lineno}: 'is' only allowed with None/True/False")
            else:
                kind = CMP_OPS.get(type(op))
                if kind is None:
                    raise DiscoveryError(f"line {node.lineno}: unsupported comparison")
                if not call_left:
                    kind = FLIP[kind]
            args, kwargs = self.parse_call(call)
            expected = self.lit(other)
            self.add_test(node, kind, args, kwargs, expected)
            return
        if self.is_entry_call(test):
            args, kwargs = self.parse_call(test)
            self.add_test(node, "truthy", args, kwargs)
            return
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and self.is_entry_call(test.operand):
            args, kwargs = self.parse_call(test.operand)
            self.add_test(node, "falsy", args, kwargs)
            return
        raise DiscoveryError(f"line {node.lineno}: unsupported assertion")

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
        if call is None or not self.is_entry_call(call):
            raise bad
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
        args, kwargs = self.parse_call(call)
        self.add_test(node, "raises", args, kwargs, extra={"exc": names, "checks": checks})

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
        if call is None or not self.is_entry_call(call):
            raise bad
        for s in body[1:]:
            if not self.is_fail_stmt(s):
                raise bad
        args, kwargs = self.parse_call(call)
        self.add_test(node, "raises", args, kwargs, extra={"exc": names, "checks": checks})
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
        if not all(isinstance(t, ast.Name) for t in targets):
            raise DiscoveryError(f"line {stmt.lineno}: unsupported assignment")
        if self.is_entry_call(value):
            for t in targets:
                self.ctx.calls[t.id] = value
                self.ctx.consts.pop(t.id, None)
            return
        v = self.lit(value)
        for t in targets:
            self.ctx.consts[t.id] = v
            self.ctx.calls.pop(t.id, None)

    def handle_body(self, body, in_func):
        self._pending_raises = None
        for stmt in body:
            pending, self._pending_raises = self._pending_raises, None
            if isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    raise DiscoveryError(f"line {stmt.lineno}: decorators not allowed")
                self.local_funcs.add(stmt.name)
                saved = (dict(self.ctx.consts), dict(self.ctx.calls), dict(self.ctx.aliases))
                self.handle_body(stmt.body, True)
                self.ctx.consts, self.ctx.calls, self.ctx.aliases = saved
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


def discover(tests_dir, entrypoint=None):
    """Return (tests, entrypoint). Raises DiscoveryError."""
    path = os.path.join(tests_dir, "tests.py")
    if not os.path.isfile(path):
        raise DiscoveryError(f"{path} not found")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        source = raw.decode("utf-8-sig")
    except Exception as e:
        raise DiscoveryError(f"cannot read tests.py: {e}")
    try:
        tree = ast.parse(source, filename="tests.py")
    except SyntaxError as e:
        raise DiscoveryError(f"syntax error in tests.py: {e}")

    # Pre-collect locally defined function names (so calls to them are allowed).
    d = Discoverer(source, entrypoint)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            d.local_funcs.add(n.name)
    d.handle_body(tree.body, False)

    callees = set(d.callees)
    if entrypoint is None:
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

    expects, comment_lines = read_expect_comments(source)
    lines = source.splitlines()

    tests = sorted(d.tests, key=lambda t: (t["line"], t["col"]))
    counts = {}
    for t in tests:
        counts[t["line"]] = counts.get(t["line"], 0) + 1
    seen = {}
    for t in tests:
        line = t["line"]
        idx = seen.get(line, 0)
        seen[line] = idx + 1
        t["id"] = f"tests.py:{line}" if counts[line] == 1 else f"tests.py:{line}#{idx}"
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

PY_RUNTIME = r'''
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


def _deq(a, e):
    """Compare an actual value with an expected value (container semantics, float tolerance)."""
    if e is None or a is None:
        return a is e
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
    if names is None:
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


def _run_one(t, factory, cap):
    kind = t["kind"]
    extra = t.get("extra") or {}
    args = t["args"]()
    kwargs = t["kwargs"]()
    raised = None
    result = None
    cap.start()
    try:
        try:
            fn = factory()
            result = fn(*args, **kwargs)
        except Exception as e:
            raised = e
        except BaseException as e:  # SystemExit, KeyboardInterrupt, ...
            raised = e
    finally:
        out = cap.stop()
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
        try:
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
        if not ok:
            try:
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
  let raised = false, err = null, result;
  const buf = capStart();
  let out;
  try {
    try {
      const fn = factory();
      result = fn(...args);
      if (isThenable(result)) result = await result;
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
        else { ok = false; break; }
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
    if (!ok) return [false, "got " + fmt(result)];
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


def render_python(tests, entrypoint):
    lines = [
        "# Auto-generated by babel_code_goat.py -- do not edit.",
        "# " + META_MARKER + " " + json.dumps({"lang": "python", "entrypoint": entrypoint}),
        "ENTRYPOINT = " + repr(entrypoint),
        "TESTS = [",
    ]
    for t in tests:
        kw = "{" + ", ".join(f"{k!r}: {py_lit(v)}" for k, v in t["kwargs"].items()) + "}"
        exp = py_lit(t["expected"]) if "expected" in t else "None"
        lines.append(
            "    {"
            f"'id': {t['id']!r}, 'kind': {t['kind']!r}, "
            f"'args': lambda: [{', '.join(py_lit(a) for a in t['args'])}], "
            f"'kwargs': lambda: {kw}, "
            f"'expected': lambda: {exp}, 'extra': {py_lit(t.get('extra') or {})}, "
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
        header.append("type TestCase = { id: string; kind: string; args: () => any[]; expected: () => any; extra: any; stdout: string | null; stderr: string | null };")
        header.append(f"const LANG: string = {js_str(lang)};")
        header.append(f"const ENTRYPOINT: string = {js_str(entrypoint)};")
        header.append("const TESTS: TestCase[] = [")
    else:
        header.append(f"const LANG = {js_str(lang)};")
        header.append(f"const ENTRYPOINT = {js_str(entrypoint)};")
        header.append("const TESTS = [")
    for t in tests:
        if t["kwargs"]:
            raise GenerateError(f"{t['id']}: keyword arguments cannot be translated to {lang}")
        exp = js_lit(t["expected"], "exp") if "expected" in t else "null"
        so = "null" if t["stdout"] is None else js_str(t["stdout"])
        se = "null" if t["stderr"] is None else js_str(t["stderr"])
        header.append(
            "  { "
            f"id: {js_str(t['id'])}, kind: {js_str(t['kind'])}, "
            f"args: () => [{', '.join(js_lit(a) for a in t['args'])}], "
            f"expected: () => ({exp}), extra: {js_lit(t.get('extra') or {})}, stdout: {so}, stderr: {se}"
            " },"
        )
    header.append("];")
    return "\n".join(header) + "\n" + JS_RUNTIME


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


def build_command(lang, tester_path, workdir):
    """Return argv prefix for running the tester or None if impossible."""
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


def run_tester(cmd_prefix, solution, ids, timeout, workdir, lang, tol=None):
    """Run tests; return (results: id->bool, error: bool)."""
    results = {}
    remaining = list(ids)
    error = False
    attempt = 0
    while remaining:
        attempt += 1
        res_path = os.path.join(workdir, f"results{attempt}.jsonl")
        ids_path = os.path.join(workdir, f"ids{attempt}.json")
        with open(ids_path, "w", encoding="utf-8") as f:
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
        last_size = -1
        last_progress = time.monotonic()
        killed = False
        while True:
            rc = proc.poll()
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
            if now - last_progress > timeout:
                killed = True
                try:
                    os.killpg(proc.pid, 9)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                proc.wait()
                break
            time.sleep(0.01)
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
            elif kind == "done":
                done = True
        if load_error:
            error = True
            break
        if started is not None and started not in results:
            results[started] = False  # crashed or timed out mid-test
        remaining = [i for i in remaining if i not in results]
        if done:
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


def cmd_test(opts):
    pos = opts["pos"]
    if len(pos) < 2:
        return emit_result("error", [], [])
    solution, tests_dir = pos[0], pos[1]
    lang = opts.get("lang")
    tester_path = os.path.join(tests_dir, LANGS[lang]) if lang in LANGS else None
    meta = read_meta(tester_path) if tester_path and os.path.isfile(tester_path) else None
    ep = opts.get("entrypoint")
    if not ep and meta and isinstance(meta.get("entrypoint"), str):
        ep = meta["entrypoint"]
    try:
        tests, ep = discover(tests_dir, ep)
    except DiscoveryError:
        if ep is not None:
            try:
                tests, ep = discover(tests_dir, None)
            except DiscoveryError:
                return emit_result("error", [], [])
        else:
            return emit_result("error", [], [])
    ids = []
    for t in tests:
        if t["id"] not in ids:
            ids.append(t["id"])
    if lang not in LANGS or not os.path.isfile(tester_path) or not os.path.isfile(solution):
        return emit_result("error", [], ids)
    timeout = opts.get("timeout", DEFAULT_TIMEOUT)
    workdir = tempfile.mkdtemp(prefix="bcg-run-")
    try:
        prefix = build_command(lang, os.path.abspath(tester_path), workdir)
        if prefix is None:
            return emit_result("error", [], ids)
        results, error = run_tester(prefix, os.path.abspath(solution), ids, timeout, workdir, lang,
                                    opts.get("tol"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    passed = [i for i in ids if results.get(i) is True]
    failed = [i for i in ids if results.get(i) is not True]
    if error:
        return emit_result("error", passed, failed)
    return emit_result("pass" if not failed else "fail", passed, failed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    opts = {"pos": []}
    valued = {"--entrypoint": "entrypoint", "--lang": "lang", "--timeout": "timeout", "--tol": "tol"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                if k in valued:
                    opts[valued[k]] = v
            elif a in valued:
                if i + 1 >= len(argv):
                    raise ValueError(f"{a} requires a value")
                opts[valued[a]] = argv[i + 1]
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
    if "tol" in opts:
        try:
            opts["tol"] = float(opts["tol"])
            if not (opts["tol"] >= 0 and math.isfinite(opts["tol"])):
                raise ValueError
        except ValueError:
            raise ValueError("--tol must be a non-negative number")
    return opts


def main(argv):
    if not argv or argv[0] not in ("generate", "test"):
        sys.stderr.write(__doc__)
        return 2
    cmd = argv[0]
    try:
        opts = parse_args(argv[1:])
    except ValueError as e:
        if cmd == "test":
            return emit_result("error", [], [])
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
    try:
        return cmd_test(opts)
    except Exception:
        return emit_result("error", [], [])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
