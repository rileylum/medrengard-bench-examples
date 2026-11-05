#!/usr/bin/env python3
"""babel_code_goat: translate a restricted Python ``tests.py`` into a tester
for python / javascript / typescript, and run that tester against a solution.

Commands:
    generate <tests_dir> --entrypoint <name> --lang <python|javascript|typescript>
    test <solution_path> <tests_dir> --lang <python|javascript|typescript> [--timeout SECONDS]
"""
from __future__ import annotations

import argparse
import ast
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


class DiscoveryError(Exception):
    pass


# --------------------------------------------------------------------------
# tests.py parsing / discovery
# --------------------------------------------------------------------------

def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        v = node.value
        return v is None or isinstance(v, (bool, int, float, str))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        o = node.operand
        return (isinstance(o, ast.Constant) and isinstance(o.value, (int, float))
                and not isinstance(o.value, bool))
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(not isinstance(e, ast.Starred) and _is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if k is None:
                return False
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return False
            if not _is_literal(v):
                return False
        return True
    return False


def _literal_value(node: ast.AST):
    if not _is_literal(node):
        raise DiscoveryError(f"line {getattr(node, 'lineno', '?')}: unsupported value")
    return ast.literal_eval(node)


def _call_matches(func: ast.AST, ep_name):
    """Return True if ``func`` refers to the entrypoint."""
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


def _parse_call(node: ast.AST, ep_name):
    if not isinstance(node, ast.Call) or not _call_matches(node.func, ep_name):
        return None
    args = []
    for a in node.args:
        if isinstance(a, ast.Starred) or not _is_literal(a):
            raise DiscoveryError(f"line {node.lineno}: unsupported argument")
        args.append(_literal_value(a))
    kwargs = []
    for kw in node.keywords:
        if kw.arg is None or not _is_literal(kw.value):
            raise DiscoveryError(f"line {node.lineno}: unsupported keyword argument")
        kwargs.append([kw.arg, _literal_value(kw.value)])
    return {"args": args, "kwargs": kwargs}


def _msg_ok(msg) -> bool:
    return msg is None or (isinstance(msg, ast.Constant) and isinstance(msg.value, str))


def _parse_assert(stmt: ast.Assert, ep_name):
    if not _msg_ok(stmt.msg):
        raise DiscoveryError(f"line {stmt.lineno}: unsupported assert message")
    t = stmt.test
    call = _parse_call(t, ep_name)
    if call is not None:
        return dict(kind="truthy", **call)
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
        call = _parse_call(t.operand, ep_name)
        if call is not None:
            return dict(kind="falsy", **call)
    if (isinstance(t, ast.Compare) and len(t.ops) == 1
            and isinstance(t.ops[0], (ast.Eq, ast.NotEq))):
        kind = "eq" if isinstance(t.ops[0], ast.Eq) else "ne"
        left, right = t.left, t.comparators[0]
        call = _parse_call(left, ep_name)
        other = right
        if call is None:
            call = _parse_call(right, ep_name)
            other = left
        if call is not None:
            if not _is_literal(other):
                raise DiscoveryError(f"line {stmt.lineno}: unsupported expected value")
            return dict(kind=kind, expected=_literal_value(other), **call)
    raise DiscoveryError(f"line {stmt.lineno}: unsupported assertion")


def _parse_try(stmt: ast.Try, ep_name):
    err = DiscoveryError(f"line {stmt.lineno}: unsupported try block")
    if stmt.orelse or stmt.finalbody or len(stmt.handlers) != 1 or len(stmt.body) != 2:
        raise err
    first, second = stmt.body
    if not isinstance(first, ast.Expr):
        raise err
    call = _parse_call(first.value, ep_name)
    if call is None:
        raise err
    if not (isinstance(second, ast.Assert) and isinstance(second.test, ast.Constant)
            and second.test.value is False and _msg_ok(second.msg)):
        raise err
    h = stmt.handlers[0]
    if h.type is not None and not (isinstance(h.type, ast.Name)
                                   and h.type.id in ("Exception", "BaseException")):
        raise err
    if len(h.body) != 1 or not isinstance(h.body[0], ast.Pass):
        raise err
    return dict(kind="raises", **call)


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

    tests = []

    def walk(body, top=False):
        for i, stmt in enumerate(body):
            if isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    raise DiscoveryError(f"line {stmt.lineno}: decorators not allowed")
                walk(stmt.body)
            elif isinstance(stmt, ast.Assert):
                t = _parse_assert(stmt, ep_name)
                t["line"] = stmt.lineno
                tests.append(t)
            elif isinstance(stmt, ast.Try):
                t = _parse_try(stmt, ep_name)
                t["line"] = stmt.lineno
                tests.append(t)
            elif isinstance(stmt, ast.Pass):
                pass
            elif (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                  and isinstance(stmt.value.value, str)):
                pass  # docstring / bare string
            else:
                raise DiscoveryError(
                    f"line {stmt.lineno}: unsupported statement {type(stmt).__name__}")

    walk(tree.body, top=True)

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
    if isinstance(v, list):
        return "[" + ", ".join(py_literal(x) for x in v) + "]"
    if isinstance(v, tuple):
        if len(v) == 1:
            return "(" + py_literal(v[0]) + ",)"
        return "(" + ", ".join(py_literal(x) for x in v) + ")"
    if isinstance(v, dict):
        return "{" + ", ".join(py_literal(k) + ": " + py_literal(x) for k, x in v.items()) + "}"
    raise TypeError(type(v))


def json_safe(v):
    """Convert to JSON-compatible data; special floats become sentinels."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return {"$bcg_float": "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, dict):
        return {k: json_safe(x) for k, x in v.items()}
    return v


# --------------------------------------------------------------------------
# Tester templates
# --------------------------------------------------------------------------

PY_TEMPLATE = r'''# Auto-generated by babel_code_goat.py -- do not edit.
# __META__
# Usage: python tester.py <solution.py> [--start N]
import copy
import importlib.util
import inspect
import io
import json
import os
import sys

MARKER = __MARKER__
ENTRYPOINT = __ENTRYPOINT__
CLASS_HINT = __CLASS_HINT__
TESTS = __TESTS__

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


def deep_eq(a, b):
    if b is None:
        return a is None
    if isinstance(b, (list, tuple)):
        if not isinstance(a, (list, tuple)) or len(a) != len(b):
            return False
        return all(deep_eq(x, y) for x, y in zip(a, b))
    if isinstance(b, dict):
        if not isinstance(a, dict) or len(a) != len(b):
            return False
        for k, v in b.items():
            if k not in a or not deep_eq(a[k], v):
                return False
        return True
    if isinstance(a, (list, tuple, dict)) or a is None:
        return False
    return bool(a == b)


def run_test(factory, t):
    kind = t["kind"]
    args = copy.deepcopy(t["args"])
    kwargs = {k: copy.deepcopy(v) for k, v in t["kwargs"]}
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
                except Exception:
                    ok = True
                except BaseException:
                    ok = False
            else:
                try:
                    res = fn(*args, **kwargs)
                    if kind == "eq":
                        ok = deep_eq(res, t["expected"])
                    elif kind == "ne":
                        ok = not deep_eq(res, t["expected"])
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
const TESTS = JSON.parse(__TESTS__, function (k, v) {
  if (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 &&
      typeof v["$bcg_float"] === "string") {
    return v["$bcg_float"] === "nan" ? NaN : (v["$bcg_float"] === "inf" ? Infinity : -Infinity);
  }
  return v;
});

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
function pyEqual(a, b) {
  if (b === null || b === undefined) return a === null || a === undefined;
  if (typeof b === "number" || typeof b === "boolean") {
    if (typeof a === "number" || typeof a === "boolean") return Number(a) === Number(b);
    if (typeof a === "bigint") return Number.isInteger(Number(b)) && a === BigInt(Number(b));
    return false;
  }
  if (typeof b === "string") return a === b;
  if (Array.isArray(b)) {
    if (!Array.isArray(a) && !(ArrayBuffer.isView(a) && !(a instanceof DataView))) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < b.length; i++) if (!pyEqual(a[i], b[i])) return false;
    return true;
  }
  if (isPlainObject(b)) {
    if (a instanceof Map) {
      const bk = Object.keys(b);
      if (a.size !== bk.length) return false;
      for (const k of bk) { if (!a.has(k) || !pyEqual(a.get(k), b[k])) return false; }
      return true;
    }
    if (!isPlainObject(a) || a instanceof Set) return false;
    const ak = Object.keys(a), bk = Object.keys(b);
    if (ak.length !== bk.length) return false;
    for (const k of bk) {
      if (!Object.prototype.hasOwnProperty.call(a, k)) return false;
      if (!pyEqual(a[k], b[k])) return false;
    }
    return true;
  }
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
function deepCopy(v) {
  if (Array.isArray(v)) return v.map(deepCopy);
  if (isPlainObject(v)) {
    const o = {};
    for (const k of Object.keys(v)) Object.defineProperty(o, k, { value: deepCopy(v[k]), enumerable: true, writable: true, configurable: true });
    return o;
  }
  return v;
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
  const args = deepCopy(t.args).concat(t.kwargs.map(function (kv) { return deepCopy(kv[1]); }));
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
        } catch (e) { ok = true; }
      } else {
        try {
          let res = fn.apply(null, args);
          if (res && typeof res.then === "function") res = await res;
          if (t.kind === "eq") ok = pyEqual(res, t.expected);
          else if (t.kind === "ne") ok = !pyEqual(res, t.expected);
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
            "stdout": t.get("stdout"), "stderr": t.get("stderr"),
        })
    if lang == "python":
        body = "[\n" + "".join("    " + py_literal(r) + ",\n" for r in records) + "]"
        out = PY_TEMPLATE
        out = out.replace("__META__", meta)
        out = out.replace("__MARKER__", repr(MARKER))
        out = out.replace("__ENTRYPOINT__", repr(name))
        out = out.replace("__CLASS_HINT__", repr(class_hint))
        out = out.replace("__TESTS__", body)
        return out
    data = json.dumps(json_safe(records), allow_nan=False)
    out = JS_TEMPLATE
    out = out.replace("__HEADER__", "// @ts-nocheck" if lang == "typescript" else "")
    out = out.replace("__META__", meta)
    out = out.replace("__FILENAME__", "tester." + LANG_EXT[lang])
    out = out.replace("__SOLEXT__", "." + LANG_EXT[lang])
    out = out.replace("__MARKER__", json.dumps(MARKER))
    out = out.replace("__ENTRYPOINT__", json.dumps(name))
    out = out.replace("__CLASS_HINT__", json.dumps(class_hint))
    out = out.replace("__IS_TS__", "true" if lang == "typescript" else "false")
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


def _run_tester(base_cmd, tester, solution, n_tests, timeout):
    """Run tester, restarting after crashes/timeouts. Returns dict id->bool."""
    results = {}
    start = 0
    while start < n_tests:
        cmd = base_cmd + [tester, solution, "--start", str(start)]
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
    results = _run_tester(base, os.path.abspath(tester), solution, len(ids), timeout)
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
