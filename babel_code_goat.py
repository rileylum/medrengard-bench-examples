#!/usr/bin/env python3
"""babel_code_goat: translate a Python ``tests.py`` harness into a tester for
python / javascript / typescript and run it against a solution.

Commands:
    generate <tests_dir> --entrypoint <name> --lang <python|javascript|typescript>
    test <solution_path> <tests_dir> --lang <python|javascript|typescript> [--timeout SECONDS]
"""

from __future__ import annotations

import ast
import io
import json
import math
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


def literal_value(node):
    """Return the Python value of an allowed literal node or raise DiscoveryError."""
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        raise DiscoveryError(f"line {node.lineno}: unsupported constant {v!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = literal_value(node.operand)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise DiscoveryError(f"line {node.lineno}: unary operator on non-number")
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.List):
        return [literal_value(e) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(literal_value(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        out = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                raise DiscoveryError(f"line {node.lineno}: dict unpacking not allowed")
            kv = literal_value(k)
            if not isinstance(kv, str):
                raise DiscoveryError(f"line {node.lineno}: dict keys must be strings")
            out[kv] = literal_value(v)
        return out
    raise DiscoveryError(
        f"line {getattr(node, 'lineno', '?')}: unsupported value {ast.dump(node)[:80]}"
    )


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


class Discoverer:
    def __init__(self, source, entrypoint):
        self.source = source
        self.entrypoint = entrypoint
        self.tests = []  # dicts
        self.callees = []
        self.local_funcs = set()

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
                raise DiscoveryError(f"line {node.lineno}: starred arguments not allowed")
            args.append(literal_value(a))
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise DiscoveryError(f"line {node.lineno}: ** arguments not allowed")
            kwargs[kw.arg] = literal_value(kw.value)
        self.callees.append(name)
        return args, kwargs

    # -- statements
    def add_test(self, node, kind, args, kwargs, expected=_MISSING):
        t = {"line": node.lineno, "col": node.col_offset, "kind": kind, "args": args, "kwargs": kwargs}
        if expected is not _MISSING:
            t["expected"] = expected
        self.tests.append(t)

    def handle_assert(self, node):
        if node.msg is not None:
            literal_value(node.msg)
        test = node.test
        if isinstance(test, ast.Compare):
            if len(test.ops) != 1 or len(test.comparators) != 1:
                raise DiscoveryError(f"line {node.lineno}: chained comparisons not allowed")
            op = test.ops[0]
            left, right = test.left, test.comparators[0]
            if isinstance(left, ast.Call):
                call, other = left, right
            elif isinstance(right, ast.Call):
                call, other = right, left
            else:
                raise DiscoveryError(f"line {node.lineno}: assertion must call the entrypoint")
            if isinstance(op, ast.Eq):
                kind = "eq"
            elif isinstance(op, ast.NotEq):
                kind = "ne"
            elif isinstance(op, (ast.Is, ast.IsNot)):
                kind = "is" if isinstance(op, ast.Is) else "isnot"
                if not (isinstance(other, ast.Constant)
                        and (other.value is None or isinstance(other.value, bool))):
                    raise DiscoveryError(f"line {node.lineno}: 'is' only allowed with None/True/False")
            else:
                raise DiscoveryError(f"line {node.lineno}: unsupported comparison")
            args, kwargs = self.parse_call(call)
            expected = literal_value(other)
            self.add_test(node, kind, args, kwargs, expected)
            return
        if isinstance(test, ast.Call):
            args, kwargs = self.parse_call(test)
            self.add_test(node, "truthy", args, kwargs)
            return
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Call):
            args, kwargs = self.parse_call(test.operand)
            self.add_test(node, "falsy", args, kwargs)
            return
        raise DiscoveryError(f"line {node.lineno}: unsupported assertion")

    def handle_try(self, node):
        bad = DiscoveryError(f"line {node.lineno}: unsupported try block")
        if node.orelse or node.finalbody or len(node.handlers) != 1 or len(node.body) != 2:
            raise bad
        first, second = node.body
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)):
            raise bad
        if not (
            isinstance(second, ast.Assert)
            and isinstance(second.test, ast.Constant)
            and second.test.value is False
        ):
            raise bad
        h = node.handlers[0]
        if not (isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException")):
            raise bad
        if len(h.body) != 1 or not isinstance(h.body[0], ast.Pass):
            raise bad
        args, kwargs = self.parse_call(first.value)
        self.add_test(node, "raises", args, kwargs)

    def handle_body(self, body, in_func):
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    raise DiscoveryError(f"line {stmt.lineno}: decorators not allowed")
                self.local_funcs.add(stmt.name)
                self.handle_body(stmt.body, True)
            elif isinstance(stmt, ast.Assert):
                self.handle_assert(stmt)
            elif isinstance(stmt, ast.Try):
                self.handle_try(stmt)
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
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(py_lit(x) for x in v) + "]"
    if isinstance(v, tuple):
        if len(v) == 1:
            return "(" + py_lit(v[0]) + ",)"
        return "(" + ", ".join(py_lit(x) for x in v) + ")"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{py_lit(k)}: {py_lit(x)}" for k, x in v.items()) + "}"
    raise GenerateError(f"cannot translate {v!r}")


def js_str(s):
    # json.dumps escapes everything needed; also escape U+2028/2029 for older engines.
    return json.dumps(s).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def js_lit(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        if abs(v) > 2**53 - 1:
            return f"{v}n"
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        if v == 0 and math.copysign(1.0, v) < 0:
            return "-0"
        r = repr(v)
        return r
    if isinstance(v, str):
        return js_str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(js_lit(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"[{js_str(k)}]: {js_lit(x)}" for k, x in v.items()) + "}"
    raise GenerateError(f"cannot translate {v!r}")


# ---------------------------------------------------------------------------
# Tester generation
# ---------------------------------------------------------------------------

PY_RUNTIME = r'''
import sys, os, io, json, inspect, importlib.util, importlib.machinery


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


def _eq(a, b):
    try:
        return bool(a == b)
    except Exception:
        return False


def _run_one(t, factory, cap):
    kind = t["kind"]
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
    else:
        if raised is not None:
            return False, "raised %s: %s" % (type(raised).__name__, raised)
        try:
            if kind == "eq":
                ok = _eq(result, t["expected"]())
            elif kind == "ne":
                ok = not _eq(result, t["expected"]())
            elif kind == "truthy":
                ok = bool(result)
            elif kind == "falsy":
                ok = not bool(result)
            elif kind == "is":
                ok = result is t["expected"]()
            elif kind == "isnot":
                ok = result is not t["expected"]()
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
        sys.stderr.write("usage: tester.py <solution_path> [--results FILE] [--ids FILE]\n")
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

function numEq(a, b) {
  if (typeof a === "bigint" || typeof b === "bigint") {
    try {
      if (typeof a === "number" && !Number.isInteger(a)) return false;
      if (typeof b === "number" && !Number.isInteger(b)) return false;
      return BigInt(a) === BigInt(b);
    } catch (e) { return false; }
  }
  return a === b;
}

// actual (JS value) vs expected (translated Python value)
function deepEq(a, e) {
  if (e === null) return a === null || a === undefined;
  if (a === undefined) a = null;
  if (typeof e === "boolean") return typeof a === "boolean" && a === e;
  if (typeof e === "number" || typeof e === "bigint") {
    return (typeof a === "number" || typeof a === "bigint") && numEq(a, e);
  }
  if (typeof e === "string") return typeof a === "string" && a === e;
  if (Array.isArray(e)) {
    let arr = a;
    if (!Array.isArray(arr)) {
      if (arr !== null && typeof arr === "object" && ArrayBuffer.isView(arr) && !(arr instanceof DataView)) arr = Array.from(arr);
      else return false;
    }
    if (arr.length !== e.length) return false;
    for (let i = 0; i < e.length; i++) if (!deepEq(arr[i], e[i])) return false;
    return true;
  }
  if (typeof e === "object") {
    if (a === null || typeof a !== "object" || Array.isArray(a)) return false;
    const ek = Object.keys(e);
    if (a instanceof Map) {
      if (a.size !== ek.length) return false;
      for (const k of ek) {
        if (!a.has(k) || !deepEq(a.get(k), e[k])) return false;
      }
      return true;
    }
    const ak = Object.keys(a);
    if (ak.length !== ek.length) return false;
    for (const k of ek) {
      if (!Object.prototype.hasOwnProperty.call(a, k)) return false;
      if (!deepEq(a[k], e[k])) return false;
    }
    return true;
  }
  return false;
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
  if (t.kind === "raises") {
    if (!raised) return [false, "expected an exception, none raised"];
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
    process.stderr.write("usage: tester <solution_path> [--results FILE] [--ids FILE]\n");
    return 2;
  }
  const solution = argv[0];
  let resultsPath = null, idsPath = null;
  for (let i = 1; i < argv.length; i++) {
    if (argv[i] === "--results" && i + 1 < argv.length) resultsPath = argv[++i];
    else if (argv[i] === "--ids" && i + 1 < argv.length) idsPath = argv[++i];
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
            f"'expected': lambda: {exp}, "
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
        header.append("type TestCase = { id: string; kind: string; args: () => any[]; expected: () => any; stdout: string | null; stderr: string | null };")
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
        exp = js_lit(t["expected"]) if "expected" in t else "null"
        so = "null" if t["stdout"] is None else js_str(t["stdout"])
        se = "null" if t["stderr"] is None else js_str(t["stderr"])
        header.append(
            "  { "
            f"id: {js_str(t['id'])}, kind: {js_str(t['kind'])}, "
            f"args: () => [{', '.join(js_lit(a) for a in t['args'])}], "
            f"expected: () => ({exp}), stdout: {so}, stderr: {se}"
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


def run_tester(cmd_prefix, solution, ids, timeout, workdir, lang):
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
        results, error = run_tester(prefix, os.path.abspath(solution), ids, timeout, workdir, lang)
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
    valued = {"--entrypoint": "entrypoint", "--lang": "lang", "--timeout": "timeout"}
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
