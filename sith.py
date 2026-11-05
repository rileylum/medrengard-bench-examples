#!/usr/bin/env python3
"""sith - a small static Python code-intelligence / completion tool.

Usage:
    python sith.py complete <file> <line> <col> [--fuzzy] [--project DIR]
    python sith.py infer <file> <line> <col> [--project DIR]
    python sith.py goto <file> <line> <col> [--follow-imports] [--project DIR]
    python sith.py signatures <file> <line> <col> [--project DIR]
    python sith.py references <file> <line> <col> [--scope file|project] [--project DIR]
    python sith.py search <query> [--project DIR]
    python sith.py names <file> [--all-scopes] [--project DIR]
    python sith.py rename <file> <line> <col> --new-name NAME [--diff] [--project DIR]
    python sith.py inline <file> <line> <col> [--diff] [--project DIR]
    python sith.py extract-variable <file> <line> <col> --until L:C --name NAME [--diff] [--project DIR]
    python sith.py extract-function <file> <line> <col> --until L:C --name NAME [--diff] [--project DIR]
    python sith.py errors <file>
"""
import ast
import builtins
import contextlib
import copy
import importlib
import inspect
import io
import json
import keyword
import os
import sys
import time
import tokenize
import types

PLACEHOLDER = '__sith_placeholder__'
INF_POS = (10 ** 9, 0)
ZERO_POS = (0, -1)

# Avoid importing modules from the directory of this script by accident.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path
               if p not in ('', '.') and os.path.abspath(p) != _SCRIPT_DIR]


class SithError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def is_ident_char(c):
    return c == '_' or c.isalnum() or ('a' + c).isidentifier()


def leading_ws(s):
    return len(s) - len(s.lstrip(' \t\f'))


@contextlib.contextmanager
def quiet():
    """Silence stdout/stderr (at fd level) while importing foreign modules."""
    saved = []
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        for fd in (1, 2):
            saved.append((fd, os.dup(fd)))
            os.dup2(devnull, fd)
        os.close(devnull)
    except Exception:
        pass
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        yield
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        for fd, dup in saved:
            try:
                os.dup2(dup, fd)
                os.close(dup)
            except Exception:
                pass


_import_cache = {}


def real_import(name):
    if name in _import_cache:
        return _import_cache[name]
    mod = None
    try:
        with quiet():
            mod = importlib.import_module(name)
    except BaseException:
        mod = None
    _import_cache[name] = mod
    return mod


def safe_getattr(obj, name):
    try:
        return True, getattr(obj, name)
    except BaseException:
        return False, None


def safe_dir(obj):
    try:
        return [n for n in dir(obj) if isinstance(n, str)]
    except BaseException:
        return []


# ---------------------------------------------------------------------------
# Tolerant parsing
# ---------------------------------------------------------------------------

def try_parse(text):
    try:
        return ast.parse(text), None
    except SyntaxError as e:
        return None, e
    except (ValueError, RecursionError, MemoryError) as e:
        return None, e


def fix_and_parse(lines, max_iter=None):
    """Parse lines, neutralising offending lines until parse succeeds.

    Returns (tree, number_of_modified_lines, final_lines)."""
    lines = list(lines)
    state = [0] * len(lines)
    if max_iter is None:
        max_iter = 3 * len(lines) + 5
    for _ in range(max_iter):
        tree, e = try_parse('\n'.join(lines))
        if tree is not None:
            return tree, sum(1 for s in state if s), lines
        if not lines:
            break
        lineno = getattr(e, 'lineno', None) or len(lines)
        idx = min(max(lineno, 1), len(lines)) - 1
        while idx >= 0 and (state[idx] >= 2 or not lines[idx].strip()):
            idx -= 1
        if idx < 0:
            cand = [k for k in range(len(lines)) if state[k] < 2 and lines[k].strip()]
            if not cand:
                break
            idx = cand[-1]
        if state[idx] == 0 and lines[idx].strip():
            orig = lines[idx]
            lines[idx] = orig[:leading_ws(orig)] + 'pass'
            state[idx] = 1
        else:
            lines[idx] = ''
            state[idx] = 2
    return ast.parse(''), len(lines) + 1, lines


# ---------------------------------------------------------------------------
# Scopes and definitions
# ---------------------------------------------------------------------------

class Def(object):
    __slots__ = ('name', 'kind', 'node', 'scope', 'pos', 'lineno', 'extra', 'tainted')

    def __init__(self, name, kind, node, scope, pos, extra=None):
        self.name = name
        self.kind = kind
        self.node = node
        self.scope = scope
        self.pos = pos
        self.lineno = pos[0]
        self.extra = extra
        self.tainted = False


class Star(object):
    def __init__(self, module, level, pos, scope):
        self.module = module
        self.level = level
        self.pos = pos
        self.lineno = pos[0]
        self.scope = scope
        self._val = False

    def module_value(self):
        if self._val is False:
            self._val = self.scope.module.resolve_import(self.module, self.level)
        return self._val


class StarHit(object):
    def __init__(self, star, name):
        self.star = star
        self.name = name
        self.pos = star.pos
        self.lineno = star.lineno
        self.tainted = False


class Scope(object):
    def __init__(self, kind, node, parent, module):
        self.kind = kind
        self.node = node
        self.parent = parent
        self.module = module
        self.defs = {}
        self.stars = []

    def add(self, d):
        if d.name == PLACEHOLDER:
            return
        self.defs.setdefault(d.name, []).append(d)

    def best(self, name, pos=None, line=None, allow_tainted=True):
        """Latest definition of name before pos (strict) / on or before line."""
        best = None
        for d in self.defs.get(name, ()):
            if not allow_tainted and d.tainted:
                continue
            if pos is not None and not d.pos < pos:
                continue
            if line is not None and d.lineno > line:
                continue
            if best is None or d.pos >= best.pos:
                best = d
        for st in self.stars:
            if pos is not None and not st.pos < pos:
                continue
            if line is not None and st.lineno > line:
                continue
            if best is not None and best.pos > st.pos:
                continue
            mv = st.module_value()
            if mv is not None and name in star_names(mv):
                best = StarHit(st, name)
        return best

    def visible(self, line):
        out = {}
        for name in self.defs:
            d = self.best(name, line=line)
            if d is not None:
                out[name] = d
        for st in self.stars:
            if line is not None and st.lineno > line:
                continue
            mv = st.module_value()
            if mv is None:
                continue
            for name in star_names(mv):
                cur = out.get(name)
                if cur is None or cur.pos <= st.pos:
                    out[name] = StarHit(st, name)
        out.pop(PLACEHOLDER, None)
        return out


COMP_TYPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _contains_placeholder(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == PLACEHOLDER:
            return True
        if isinstance(n, ast.Attribute) and n.attr == PLACEHOLDER:
            return True
        if isinstance(n, ast.alias) and PLACEHOLDER in n.name:
            return True
    return False


class ScopeBuilder(object):
    def __init__(self, module):
        self.module = module
        self.node_scope = {}

    def build_module(self, tree):
        s = Scope('module', tree, None, self.module)
        self.node_scope[tree] = s
        for st in tree.body:
            self.visit(st, s)
        return s

    def _pos(self, node):
        return (getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))

    def _add(self, scope, d, stmt=None):
        if isinstance(stmt, ast.AST) and _contains_placeholder(stmt):
            d.tainted = True
        scope.add(d)

    def add_params(self, node, s, pos):
        a = node.args
        params = list(getattr(a, 'posonlyargs', [])) + list(a.args)
        for i, p in enumerate(params):
            s.add(Def(p.arg, 'param', p, s, pos, extra=(node, i, 'pos')))
        if a.vararg:
            s.add(Def(a.vararg.arg, 'param', a.vararg, s, pos, extra=(node, -1, 'vararg')))
        for p in a.kwonlyargs:
            s.add(Def(p.arg, 'param', p, s, pos, extra=(node, -1, 'kwonly')))
        if a.kwarg:
            s.add(Def(a.kwarg.arg, 'param', a.kwarg, s, pos, extra=(node, -1, 'kwarg')))

    def add_target(self, t, s, kind, value, stmt, path=()):
        if isinstance(t, ast.Name):
            self._add(s, Def(t.id, kind, t, s, self._pos(stmt), extra=(value, path, stmt)),
                      value if isinstance(value, ast.AST) else stmt)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for i, e in enumerate(t.elts):
                if isinstance(e, ast.Starred):
                    self.add_target(e.value, s, kind, value, stmt, path + (None,))
                else:
                    self.add_target(e, s, kind, value, stmt, path + (i,))
        elif isinstance(t, ast.Starred):
            self.add_target(t.value, s, kind, value, stmt, path + (None,))
        else:
            self.visit(t, s)

    def visit_list(self, nodes, s):
        for n in nodes:
            if n is not None:
                self.visit(n, s)

    def func_scope(self, s):
        while s.kind == 'comp':
            s = s.parent
        return s

    def visit(self, n, s):
        if isinstance(n, FUNC_TYPES):
            self._add(s, Def(n.name, 'def', n, s, self._pos(n)))
            self.visit_list(n.decorator_list, s)
            self.visit_list(n.args.defaults, s)
            self.visit_list(n.args.kw_defaults, s)
            child = Scope('function', n, s, self.module)
            self.node_scope[n] = child
            self.add_params(n, child, self._pos(n))
            for st in n.body:
                self.visit(st, child)
            return
        if isinstance(n, ast.ClassDef):
            self._add(s, Def(n.name, 'class', n, s, self._pos(n)))
            self.visit_list(n.decorator_list, s)
            self.visit_list(n.bases, s)
            for k in n.keywords:
                self.visit(k.value, s)
            child = Scope('class', n, s, self.module)
            self.node_scope[n] = child
            for st in n.body:
                self.visit(st, child)
            return
        if isinstance(n, ast.Lambda):
            self.visit_list(n.args.defaults, s)
            self.visit_list(n.args.kw_defaults, s)
            child = Scope('lambda', n, s, self.module)
            self.node_scope[n] = child
            self.add_params(n, child, ZERO_POS)
            self.visit(n.body, child)
            return
        if isinstance(n, COMP_TYPES):
            if n.generators:
                self.visit(n.generators[0].iter, s)
            child = Scope('comp', n, s, self.module)
            self.node_scope[n] = child
            for i, g in enumerate(n.generators):
                if i > 0:
                    self.visit(g.iter, child)
                self._comp_target(g.target, child, g.iter, ())
                self.visit_list(g.ifs, child)
            if isinstance(n, ast.DictComp):
                self.visit(n.key, child)
                self.visit(n.value, child)
            else:
                self.visit(n.elt, child)
            return
        if isinstance(n, ast.Assign):
            self.visit(n.value, s)
            for t in n.targets:
                self.add_target(t, s, 'assign', n.value, n)
            return
        if isinstance(n, ast.AnnAssign):
            if n.value is not None:
                self.visit(n.value, s)
            if isinstance(n.target, ast.Name):
                self._add(s, Def(n.target.id, 'annassign', n.target, s, self._pos(n),
                                 extra=(n.value, n.annotation, n)), n.value)
            else:
                self.visit(n.target, s)
            return
        if isinstance(n, ast.AugAssign):
            self.visit(n.value, s)
            if isinstance(n.target, ast.Name):
                self._add(s, Def(n.target.id, 'augassign', n.target, s, self._pos(n), extra=n), n)
            else:
                self.visit(n.target, s)
            return
        if isinstance(n, (ast.For, ast.AsyncFor)):
            self.visit(n.iter, s)
            self.add_target(n.target, s, 'for', n.iter, n)
            self.visit_list(n.body, s)
            self.visit_list(n.orelse, s)
            return
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                self.visit(item.context_expr, s)
                if item.optional_vars is not None:
                    self.add_target(item.optional_vars, s, 'with', item.context_expr, n)
            self.visit_list(n.body, s)
            return
        if isinstance(n, ast.ExceptHandler):
            if n.type is not None:
                self.visit(n.type, s)
            if n.name:
                self._add(s, Def(n.name, 'except', n, s, self._pos(n), extra=n.type))
            self.visit_list(n.body, s)
            return
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    bound = a.asname
                else:
                    bound = a.name.split('.')[0]
                self._add(s, Def(bound, 'import', n, s, self._pos(n), extra=(a.name, a.asname)), n)
            return
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == '*':
                    s.stars.append(Star(n.module or '', n.level or 0, self._pos(n), s))
                else:
                    self._add(s, Def(a.asname or a.name, 'importfrom', n, s, self._pos(n),
                                     extra=(n.module or '', a.name, n.level or 0)), n)
            return
        if isinstance(n, ast.NamedExpr):
            self.visit(n.value, s)
            if isinstance(n.target, ast.Name):
                fs = self.func_scope(s)
                self._add(fs, Def(n.target.id, 'walrus', n.target, fs, self._pos(n), extra=n.value), n)
            return
        if hasattr(ast, 'MatchAs') and isinstance(n, (ast.MatchAs, ast.MatchStar)):
            if n.name:
                s.add(Def(n.name, 'match', n, s, self._pos(n)))
            if isinstance(n, ast.MatchAs) and n.pattern is not None:
                self.visit(n.pattern, s)
            return
        if hasattr(ast, 'MatchMapping') and isinstance(n, ast.MatchMapping):
            if n.rest:
                s.add(Def(n.rest, 'match', n, s, self._pos(n)))
            for c in ast.iter_child_nodes(n):
                self.visit(c, s)
            return
        for c in ast.iter_child_nodes(n):
            self.visit(c, s)

    def _comp_target(self, t, s, iter_node, path):
        if isinstance(t, ast.Name):
            s.add(Def(t.id, 'comp', t, s, ZERO_POS, extra=(iter_node, path)))
        elif isinstance(t, (ast.Tuple, ast.List)):
            for i, e in enumerate(t.elts):
                self._comp_target(e, s, iter_node, path + (i,))
        elif isinstance(t, ast.Starred):
            self._comp_target(t.value, s, iter_node, path + (None,))


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

class ModVal(object):
    def __init__(self, name, real=None, local=None):
        self.name = name
        self.real = real
        self.local = local


class ClassVal(object):
    def __init__(self, node, scope):
        self.node = node
        self.scope = scope  # defining scope


class InstVal(object):
    def __init__(self, cls, call=None):
        self.cls = cls
        self.call = call  # (call_node, scope, pos, env snapshot) of the constructor call


class FuncVal(object):
    def __init__(self, node, scope, bound=None):
        self.node = node
        self.scope = scope  # defining scope
        self.bound = bound  # InstVal / ClassVal the function is bound to


class RealVal(object):
    def __init__(self, obj):
        self.obj = obj


class RealInst(object):
    def __init__(self, typ, elems=None, items=None, values=None):
        self.typ = typ
        self.elems = elems    # thunk -> list of element values (iteration)
        self.items = items    # list of thunks, per index (tuples)
        self.values = values  # thunk -> list of mapping values (dicts)


def wrap_real(obj, name=None):
    if inspect.ismodule(obj):
        return ModVal(name or getattr(obj, '__name__', ''), real=obj)
    return RealVal(obj)


SAMPLES = {
    int: 1, float: 1.0, complex: 1j, str: 'a', bytes: b'a', bytearray: bytearray(b'a'),
    list: [1], dict: {1: 1}, set: {1}, frozenset: frozenset([1]), tuple: (1,), bool: True,
    type(None): None, range: range(1), type(Ellipsis): Ellipsis,
    types.FunctionType: (lambda: 0),
}


def sample_of(typ):
    if typ in SAMPLES:
        smp = SAMPLES[typ]
        if isinstance(smp, (list, dict, set, bytearray)):
            smp = copy.copy(smp)
        return True, smp
    return False, None


def val_key(v):
    if v is None:
        return None
    if isinstance(v, InstVal):
        return ('inst', id(v.cls.node))
    if isinstance(v, RealInst):
        return ('real', v.typ)
    if isinstance(v, ClassVal):
        return ('cls', id(v.node))
    if isinstance(v, FuncVal):
        return ('func', id(v.node))
    if isinstance(v, ModVal):
        if v.local is not None:
            return ('mod', v.local.path)
        if v.real is not None:
            return ('mod', id(v.real))
        return ('mod', v.name)
    if isinstance(v, RealVal):
        return ('obj', id(v.obj))
    return ('?', id(v))


def uniq(vals):
    out = []
    seen = set()
    for v in vals:
        k = val_key(v)
        if k is None or k in seen:
            continue
        seen.add(k)
        out.append(v)
    return out


def same_val(vals):
    vals = uniq([v for v in vals if v is not None])
    if len(vals) == 1:
        return vals[0]
    return None


def _union(a, b):
    if a is None:
        return None if b is None else list(b)
    if b is None:
        return list(a)
    out = list(a)
    for x in b:
        if all(x is not y for y in out):
            out.append(x)
    return out


def memo(fn):
    state = {}

    def th():
        if 'v' in state:
            return state['v']
        if state.get('busy'):
            _cuts[0] += 1
            return []
        state['busy'] = True
        try:
            v = fn()
        finally:
            state['busy'] = False
        state['v'] = v
        return v
    return th


def const_thunk(vals):
    return lambda: list(vals)


def none_inst():
    return RealInst(type(None))


def describe(val, name):
    if val is None:
        return 'statement', 'statement'
    if isinstance(val, ModVal):
        return 'module', 'module %s' % name
    if isinstance(val, ClassVal):
        return 'class', 'class %s' % name
    if isinstance(val, FuncVal):
        return 'function', 'def %s(...)' % name
    if isinstance(val, InstVal):
        return 'instance', 'instance of %s' % val.cls.node.name
    if isinstance(val, RealInst):
        return 'instance', 'instance of %s' % val.typ.__name__
    if isinstance(val, RealVal):
        obj = val.obj
        if inspect.isclass(obj):
            return 'class', 'class %s' % name
        if inspect.ismodule(obj):
            return 'module', 'module %s' % name
        if is_routine(obj):
            return 'function', 'def %s(...)' % name
        return 'instance', 'instance of %s' % type(obj).__name__
    return 'statement', 'statement'


def is_routine(obj):
    return inspect.isroutine(obj) or isinstance(obj, (types.MethodType, types.BuiltinMethodType)) \
        or type(obj).__name__ in ('method-wrapper', 'wrapper_descriptor',
                                  'method_descriptor', 'classmethod_descriptor')


def star_names(mv):
    """Names imported by `from mv import *` (public names)."""
    cached = getattr(mv, '_star_names', None)
    if cached is not None:
        return cached
    res = _star_names(mv)
    try:
        mv._star_names = set(res)
        return mv._star_names
    except Exception:
        return res


def _star_names(mv):
    if isinstance(mv, ModVal):
        if mv.local is not None:
            return mv.local.public_names()
        if mv.real is not None:
            al = getattr(mv.real, '__all__', None)
            if isinstance(al, (list, tuple)):
                return [n for n in al if isinstance(n, str)]
            return [n for n in safe_dir(mv.real) if not n.startswith('_')]
    return []


BUILTIN_RETURNS = {
    'len': int, 'repr': str, 'input': str, 'chr': str, 'ord': int, 'hash': int, 'id': int,
    'format': str, 'hex': str, 'oct': str, 'bin': str, 'ascii': str, 'sorted': list,
    'isinstance': bool, 'issubclass': bool, 'callable': bool, 'hasattr': bool,
    'open': io.TextIOWrapper, 'round': int, 'dir': list, 'vars': dict, 'globals': dict,
    'locals': dict, 'any': bool, 'all': bool, 'print': type(None), 'abs': int,
    'divmod': tuple,
}


# ---------------------------------------------------------------------------
# Module analysis
# ---------------------------------------------------------------------------

_local_modules = {}


def _read_text(path):
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8', errors='replace')
    except Exception:
        return None
    if text.startswith('﻿'):
        text = text[1:]
    return text.replace('\x00', '')


def load_local_module(path, name):
    path = os.path.abspath(path)
    if path in _local_modules:
        return _local_modules[path]
    text = _read_text(path)
    if text is None:
        _local_modules[path] = None
        return None
    mi = ModuleInfo(path, name)
    _local_modules[path] = mi
    lines = [l.rstrip('\r') for l in text.split('\n')]
    tree, _, lines = fix_and_parse(lines)
    mi.lines = lines
    mi.set_tree(tree)
    return mi


def load_namespace(dirpath, name):
    dirpath = os.path.abspath(dirpath)
    key = dirpath + os.sep
    if key in _local_modules:
        return _local_modules[key]
    mi = ModuleInfo(dirpath, name, namespace=True)
    _local_modules[key] = mi
    mi.lines = []
    mi.set_tree(ast.parse(''))
    return mi


def _is_ident(s):
    return s.isidentifier() and not keyword.iskeyword(s)


def find_module_in(base, part):
    """Locate module `part` in directory `base`: (kind, path) or None."""
    if not part or not os.path.isdir(base):
        return None
    d = os.path.join(base, part)
    init = os.path.join(d, '__init__.py')
    if os.path.isdir(d) and os.path.isfile(init):
        return 'package', init
    f = os.path.join(base, part + '.py')
    if os.path.isfile(f):
        return 'module', f
    if os.path.isdir(d):
        return 'namespace', d
    return None


def list_modules_in(base):
    """Names of modules / packages importable from directory `base`."""
    out = set()
    try:
        entries = os.listdir(base)
    except OSError:
        return out
    for e in entries:
        p = os.path.join(base, e)
        if e.endswith('.py') and os.path.isfile(p):
            n = e[:-3]
            if _is_ident(n) and n != '__init__':
                out.add(n)
        elif os.path.isdir(p) and _is_ident(e) and e != '__pycache__':
            out.add(e)
    return out


def _load_found(found, name):
    kind, p = found
    if kind == 'namespace':
        return load_namespace(p, name)
    return load_local_module(p, name)


_STDLIB = None


def stdlib_names():
    global _STDLIB
    if _STDLIB is None:
        names = set(getattr(sys, 'stdlib_module_names', ()) or ())
        names.update(sys.builtin_module_names)
        if not getattr(sys, 'stdlib_module_names', None):
            import pkgutil
            import sysconfig
            try:
                std = os.path.normcase(os.path.abspath(sysconfig.get_paths()['stdlib']))
                for m in pkgutil.iter_modules([std]):
                    names.add(m.name)
            except Exception:
                pass
        _STDLIB = names
    return _STDLIB


def is_stdlib(dotted):
    return dotted.split('.')[0] in stdlib_names()


def _within_root(path):
    root = os.path.normcase(_root())
    p = os.path.normcase(os.path.abspath(path))
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


class ModuleInfo(object):
    def __init__(self, path, name, namespace=False):
        self.path = path
        self.name = name
        self.namespace = namespace
        if namespace:
            self.dir = os.path.abspath(path)
            self.is_package = True
        else:
            self.dir = os.path.dirname(os.path.abspath(path))
            self.is_package = os.path.basename(path) == '__init__.py'
        self.tree = None
        self.scope = None
        self.node_scope = {}
        self.lines = []
        self._by_node = None
        self._all = False
        self._busy = False
        self.is_stub = path.endswith('.pyi')
        self.source = None      # for stubs: the runtime module (if any)
        self.qual_name = None   # explicit dotted module name override
        self._stub = False
        self._parents = None

    def parents(self):
        if self._parents is None:
            par = {}
            if self.tree is not None:
                for node in ast.walk(self.tree):
                    for c in ast.iter_child_nodes(node):
                        par[c] = node
            self._parents = par
        return self._parents

    def stub(self):
        """The stub module providing type information for this module."""
        if self._stub is not False:
            return self._stub
        self._stub = None
        if self.is_stub or self.namespace:
            return None
        sp = find_stub_path(self.path, module_name_of(self))
        if sp is not None:
            st = load_local_module(sp, self.name)
            if st is not None and st is not self:
                st.source = self
                st.qual_name = module_name_of(self)
                self._stub = st
        return self._stub

    def set_tree(self, tree):
        self.tree = tree
        b = ScopeBuilder(self)
        self.scope = b.build_module(tree)
        self.node_scope = b.node_scope
        self._by_node = None

    def all_scopes(self):
        out = [self.scope]
        for n, s in self.node_scope.items():
            if s is not self.scope:
                out.append(s)
        return out

    def defs_by_node(self, node):
        if self._by_node is None:
            idx = {}
            for s in self.all_scopes():
                for ds in s.defs.values():
                    for d in ds:
                        idx.setdefault(id(d.node), []).append(d)
            self._by_node = idx
        return self._by_node.get(id(node), [])

    def top_names(self):
        if self.scope is None or self._busy:
            return []
        self._busy = True
        try:
            names = list(self.scope.visible(None).keys())
        finally:
            self._busy = False
        st = self.stub()
        if st is not None:
            for n in st.top_names():
                if n not in names and not _stub_private_import(st, n):
                    names.append(n)
        return names

    def dunder_all(self):
        """Names listed in __all__ (or None when not defined)."""
        if self._all is not False:
            return self._all
        names = None
        if self.tree is not None:
            for st in self.tree.body:
                tgt = val = None
                op = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1:
                    tgt, val, op = st.targets[0], st.value, '='
                elif isinstance(st, ast.AnnAssign) and st.value is not None:
                    tgt, val, op = st.target, st.value, '='
                elif isinstance(st, ast.AugAssign) and isinstance(st.op, ast.Add):
                    tgt, val, op = st.target, st.value, '+='
                elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                    f = st.value.func
                    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                            and f.value.id == '__all__' and f.attr in ('extend', 'append') \
                            and st.value.args and names is not None:
                        a = st.value.args[0]
                        if f.attr == 'append':
                            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                                names.append(a.value)
                        else:
                            names.extend(_str_elts(a) or [])
                    continue
                if not (isinstance(tgt, ast.Name) and tgt.id == '__all__'):
                    continue
                elts = _str_elts(val)
                if elts is None:
                    continue
                if op == '=':
                    names = list(elts)
                elif names is not None:
                    names.extend(elts)
        self._all = names
        return names

    def public_names(self):
        al = self.dunder_all()
        if al is not None:
            out = []
            for n in al:
                if n not in out:
                    out.append(n)
            return out
        return [n for n in self.top_names() if not n.startswith('_')]

    def submodule_names(self):
        if not self.is_package:
            return []
        return sorted(list_modules_in(self.dir))

    def attr_defs(self, name):
        if self.scope is None:
            return []
        return reaching_defs(self.scope, name, INF_POS, allow_tainted=True)

    def attr(self, name):
        if self._busy:
            return False, []
        st = self.stub()
        if st is not None and not _stub_private_import(st, name):
            ok, vals = st.attr(name)
            if ok and vals:
                return ok, vals
        self._busy = True
        try:
            ds = self.attr_defs(name)
        finally:
            self._busy = False
        if ds:
            out = []
            for d in ds:
                out.extend(infer_def_vals(d))
            return True, uniq(out)
        # submodule of a local package
        if self.is_package:
            mv = self.submodule(name)
            if mv is not None:
                return True, [mv]
        return False, []

    def submodule(self, name):
        found = find_module_in(self.dir, name)
        if found is None:
            return None
        base = module_name_of(self)
        mi = _load_found(found, (base + '.' + name) if base else name)
        return ModVal(name, local=mi) if mi is not None else None

    def _local_module(self, base, dotted, display):
        parts = dotted.split('.') if dotted else []
        if not parts:
            init = os.path.join(base, '__init__.py')
            if os.path.isfile(init):
                mi = load_local_module(init, os.path.basename(base))
            elif os.path.isdir(base):
                mi = load_namespace(base, os.path.basename(base))
            else:
                mi = None
            return ModVal(display, local=mi) if mi else None
        p = base
        for i, part in enumerate(parts):
            found = find_module_in(p, part)
            if found is None:
                return None
            last = i == len(parts) - 1
            if last:
                mi = _load_found(found, '.'.join(parts))
                return ModVal(display, local=mi) if mi else None
            if found[0] == 'module':
                return None
            p = os.path.join(p, part)
        return None

    def resolve_import(self, dotted, level, display=None):
        display = display or (dotted.split('.')[-1] if dotted else dotted)
        if level:
            base = self.dir
            for _ in range(level - 1):
                base = os.path.dirname(base)
            if not _within_root(base):
                return None
            mv = self._local_module(base, dotted, display)
            return mv
        if not dotted:
            return None
        mv = self._local_module(_root(), dotted, display)
        if mv is not None:
            return mv
        sp = find_stub_only(dotted)
        if sp is not None:
            mi = load_local_module(sp, dotted)
            if mi is not None:
                mi.qual_name = dotted
                return ModVal(display, local=mi)
        if is_stdlib(dotted):
            mod = real_import(dotted)
            if mod is not None:
                return ModVal(display, real=mod)
        return None


def _stub_private_import(st, name):
    """Stub-only imports (e.g. `from typing import overload`) are not re-exported."""
    ds = st.scope.defs.get(name, ()) if st.scope is not None else ()
    if not ds:
        return False
    for d in ds:
        if d.kind not in ('import', 'importfrom'):
            return False
        if d.kind == 'import' and d.extra[1] and d.extra[1] == d.extra[0]:
            return False
        if d.kind == 'importfrom' and d.extra[1] == name and \
                any(a.asname == a.name for a in d.node.names if a.name == name):
            return False
    return True


def _stubs_dir():
    return os.path.join(_root(), 'stubs')


def find_stub_path(py_path, dotted):
    """Locate the stub for runtime module at py_path (dotted name `dotted`)."""
    base, ext = os.path.splitext(os.path.abspath(py_path))
    if ext == '.py':
        cand = base + '.pyi'
        if os.path.isfile(cand):
            return cand
    if dotted and dotted != '__main__':
        return find_stub_only(dotted)
    return None


def find_stub_only(dotted):
    if not dotted:
        return None
    parts = dotted.split('.')
    if not all(_is_ident(p) for p in parts):
        return None
    sd = _stubs_dir()
    if not os.path.isdir(sd):
        return None
    p1 = os.path.join(sd, *parts) + '.pyi'
    if os.path.isfile(p1):
        return p1
    p2 = os.path.join(sd, *(parts + ['__init__.pyi']))
    if os.path.isfile(p2):
        return p2
    return None


def _str_elts(node):
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                out.append(e.value)
        return out
    return None


_PROJECT_ROOT = None


# ---------------------------------------------------------------------------
# Flow analysis (reaching definitions)
# ---------------------------------------------------------------------------

def _start(n):
    return (getattr(n, 'lineno', 0), getattr(n, 'col_offset', 0))


def _end(n):
    return (getattr(n, 'end_lineno', None) or getattr(n, 'lineno', 0),
            getattr(n, 'end_col_offset', None) or 0)


class _Reached(Exception):
    def __init__(self, state):
        Exception.__init__(self)
        self.state = state


class Flow(object):
    """Computes which definitions (objects with a .pos) reach a position."""

    def __init__(self, defs, pos, star=None):
        self.defs = sorted(defs, key=lambda d: d.pos)
        self.pos = pos
        self.loops = []
        self.star = star

    def run(self, body, init):
        try:
            return self.block(body, init)
        except _Reached as r:
            return r.state

    def gen(self, a, b, inclusive):
        last = None
        for d in self.defs:
            if a <= d.pos and (d.pos <= b if inclusive else d.pos < b):
                last = d
        return last

    def bind(self, st, a, b, inclusive=False):
        d = self.gen(a, b, inclusive)
        if d is None or st is None:
            return st
        return [d]

    def inside(self, n):
        return self.pos is not None and _start(n) <= self.pos <= _end(n)

    def block(self, stmts, st):
        for n in stmts:
            if self.pos is not None and _start(n) > self.pos:
                raise _Reached(st)
            st = self.stmt(n, st)
        return st

    def sub(self, stmts, st):
        out = self.block(stmts, st)
        if stmts and self.pos is not None and _start(stmts[0]) <= self.pos <= _end(stmts[-1]):
            raise _Reached(out)
        return out

    def _hdr(self, n, body):
        return _start(body[0]) if body else _end(n)

    def loop(self, n, st, is_for):
        hdr = self._hdr(n, n.body)
        if self.pos is not None and _start(n) <= self.pos < hdr:
            raise _Reached(st)
        # dry pass to find definitions flowing around the loop
        saved = self.pos
        self.pos = None
        self.loops.append([])
        try:
            first = self.block(n.body, self.bind(st, _start(n), hdr))
        finally:
            conts = self.loops.pop()
            self.pos = saved
        head = _union(st, first)
        for c in conts:
            head = _union(head, c)
        body_in = self.bind(head, _start(n), hdr)
        self.loops.append([])
        try:
            out = self.sub(n.body, body_in)
        finally:
            brk = self.loops.pop()
        exit_st = _union(head, out)
        exit_st = self.sub(n.orelse, exit_st) if n.orelse else exit_st
        for b in brk:
            exit_st = _union(exit_st, b)
        return exit_st

    def stmt(self, n, st):
        if isinstance(n, ast.If):
            hdr = self._hdr(n, n.body)
            if self.pos is not None and _start(n) <= self.pos < hdr:
                raise _Reached(st)
            st = self.bind(st, _start(n), hdr)
            b = self.sub(n.body, st)
            o = self.sub(n.orelse, st) if n.orelse else st
            return _union(b, o)
        if isinstance(n, (ast.For, ast.AsyncFor)):
            return self.loop(n, st, True)
        if isinstance(n, ast.While):
            return self.loop(n, st, False)
        if isinstance(n, (ast.With, ast.AsyncWith)):
            hdr = self._hdr(n, n.body)
            if self.pos is not None and _start(n) <= self.pos < hdr:
                raise _Reached(st)
            st = self.bind(st, _start(n), hdr)
            return self.sub(n.body, st)
        if isinstance(n, ast.Try) or type(n).__name__ == 'TryStar':
            body_out = self.sub(n.body, st)
            h_in = _union(st, body_out)
            outs = []
            for h in n.handlers:
                hdr = self._hdr(h, h.body)
                if self.pos is not None and _start(h) <= self.pos < hdr:
                    raise _Reached(h_in)
                hs = self.bind(h_in, _start(h), hdr)
                outs.append(self.sub(h.body, hs))
            res = self.sub(n.orelse, body_out) if n.orelse else body_out
            for o in outs:
                res = _union(res, o)
            if n.finalbody:
                if self.pos is not None and _start(n.finalbody[0]) <= self.pos:
                    self.sub(n.finalbody, _union(res, h_in) if res is not None else h_in)
                if res is None:
                    return None
                res = self.sub(n.finalbody, res)
            return res
        if hasattr(ast, 'Match') and isinstance(n, ast.Match):
            if self.pos is not None and n.cases and _start(n) <= self.pos < _start(n.cases[0].pattern):
                raise _Reached(st)
            outs = []
            irrefutable = False
            for c in n.cases:
                pstart = _start(c.pattern)
                if self.pos is not None and self.pos < pstart:
                    raise _Reached(st)
                b0 = _start(c.body[0]) if c.body else _end(c.pattern)
                if self.pos is not None and self.pos < b0:
                    raise _Reached(st)
                cs = self.bind(st, pstart, b0)
                outs.append(self.sub(c.body, cs))
                if isinstance(c.pattern, ast.MatchAs) and c.pattern.pattern is None and c.guard is None:
                    irrefutable = True
            res = None if irrefutable else st
            for o in outs:
                res = _union(res, o)
            return res
        if self.inside(n):
            raise _Reached(st)
        if isinstance(n, (ast.Return, ast.Raise)):
            return None
        if isinstance(n, (ast.Break, ast.Continue)):
            if self.loops:
                self.loops[-1].append(st)
            return None
        if isinstance(n, ast.ImportFrom) and self.star is not None \
                and any(a.name == '*' for a in n.names):
            e = self.star(n)
            if e is not None and st is not None:
                return [e]
        return self.bind(st, _start(n), _end(n), True)


def scope_body(s):
    node = s.node
    if isinstance(node, (ast.Module, ast.ClassDef) + FUNC_TYPES):
        return node.body
    return None


def reaching_defs(s, name, pos, allow_tainted=False):
    if name == PLACEHOLDER:
        return []
    defs = [d for d in s.defs.get(name, ()) if allow_tainted or not d.tainted]
    stars = s.stars
    if not defs and not stars:
        return []
    body = scope_body(s)
    if body is None:
        d = s.best(name, pos=None if pos == INF_POS else pos, allow_tainted=allow_tainted)
        if d is None:
            d = s.best(name, allow_tainted=allow_tainted)
        return [d] if d is not None else []
    params = [d for d in defs if d.kind == 'param']
    others = [d for d in defs if d.kind != 'param']

    def star_fn(stmt):
        for stobj in stars:
            if stobj.pos == _start(stmt):
                mv = stobj.module_value()
                if mv is not None and name in star_names(mv):
                    return StarHit(stobj, name)
        return None

    if not others and not params:
        # only possible through a star import
        if not any(True for _ in stars):
            return []
    fl = Flow(others, pos, star_fn if stars else None)
    st = fl.run(body, [params[-1]] if params else [])
    if not st:
        d = s.best(name, pos=None if pos == INF_POS else pos, allow_tainted=allow_tainted)
        return [d] if d is not None else []
    return [d for d in st if d is not None]


def lookup_defs(name, scope, pos):
    if name == PLACEHOLDER:
        return []
    s = scope
    first = True
    p = pos
    while s is not None:
        if first or s.kind != 'class':
            ds = reaching_defs(s, name, p)
            if ds:
                return ds
        if s.kind in ('function', 'lambda'):
            p = INF_POS
        first = False
        s = s.parent
    return []


def lookup(name, scope, pos, line=None):
    ds = lookup_defs(name, scope, pos)
    return ds[-1] if ds else None


# ---------------------------------------------------------------------------
# Call environments (argument binding)
# ---------------------------------------------------------------------------

_envs = []


def env_for(fnode):
    for fid, mp in reversed(_envs):
        if fid == id(fnode):
            return mp
    return None


@contextlib.contextmanager
def pushed_env(fnode, mapping):
    if mapping is None:
        yield
        return
    _envs.append((id(fnode), mapping))
    try:
        yield
    finally:
        _envs.pop()


def _arg_thunk(node, s, pos, snapshot):
    def th():
        saved = _envs[:]
        _envs[:] = snapshot
        try:
            return infer_vals(node, s, pos)
        finally:
            _envs[:] = saved
    return memo(th)


def func_params(fnode):
    a = fnode.args
    return list(getattr(a, 'posonlyargs', [])) + list(a.args)


def bind_args(fnode, call, s, pos, bound, snapshot=None):
    params = func_params(fnode)
    mapping = {}
    idx = 0
    if bound is not None and params:
        mapping[params[0].arg] = const_thunk([bound])
        idx = 1
    if call is None:
        return mapping
    if snapshot is None:
        snapshot = list(_envs)
    names = set(p.arg for p in params) | set(p.arg for p in fnode.args.kwonlyargs)
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            break
        if idx < len(params):
            mapping[params[idx].arg] = _arg_thunk(arg, s, pos, snapshot)
            idx += 1
    for kw in call.keywords:
        if kw.arg and kw.arg in names:
            mapping[kw.arg] = _arg_thunk(kw.value, s, pos, snapshot)
    return mapping


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

_infer_stack = set()
_depth = [0]
_cuts = [0]          # number of times a recursion guard truncated a result
_cache = {}
_deadline = [None]   # wall-clock budget for one request


def _out_of_time():
    d = _deadline[0]
    return d is not None and time.time() > d


def _cached(key, compute):
    if key in _cache:
        return _cache[key]
    before = _cuts[0]
    v = compute()
    if _cuts[0] == before:
        _cache[key] = v
    return v


def _env_key():
    return tuple(id(m) for _, m in _envs)


def infer_def_vals(d):
    if d is None:
        return []
    key = (id(d), _env_key())
    if key in _cache:
        return _cache[key]
    if key in _infer_stack or len(_infer_stack) > 80 or _out_of_time():
        _cuts[0] += 1
        return []
    _infer_stack.add(key)
    try:
        return _cached(key, lambda: uniq(_infer_def(d) or []))
    except RecursionError:
        _cuts[0] += 1
        return []
    finally:
        _infer_stack.discard(key)


def infer_def(d, depth=0):
    return same_val(infer_def_vals(d))


def builtin_value(name):
    ok, obj = safe_getattr(builtins, name)
    if ok:
        return wrap_real(obj, name)
    return None


def deco_names(fnode):
    out = set()
    for dec in getattr(fnode, 'decorator_list', []):
        if isinstance(dec, ast.Call):
            dec = dec.func
        if isinstance(dec, ast.Name):
            out.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            out.add(dec.attr)
    return out


def param_default(fnode, par):
    a = fnode.args
    params = func_params(fnode)
    if par in params:
        i = params.index(par)
        off = len(params) - len(a.defaults)
        if i >= off:
            return a.defaults[i - off]
        return None
    if par in a.kwonlyargs:
        i = a.kwonlyargs.index(par)
        return a.kw_defaults[i]
    return None


def _infer_def(d):
    if isinstance(d, StarHit):
        mv = d.star.module_value()
        if mv is None:
            return []
        return attr_vals(mv, d.name)[1]
    if isinstance(d, AttrDef):
        return attrdef_vals(d)
    s = d.scope
    k = d.kind
    if k == 'def':
        return [FuncVal(d.node, s)]
    if k == 'class':
        return [ClassVal(d.node, s)]
    if k == 'param':
        fnode, idx, pkind = d.extra
        par = d.node
        env = env_for(fnode)
        if pkind == 'vararg':
            return [RealInst(tuple)]
        if pkind == 'kwarg':
            return [RealInst(dict)]
        is_self = pkind == 'pos' and idx == 0 and isinstance(fnode, FUNC_TYPES) \
            and s.parent is not None and s.parent.kind == 'class'
        if env is not None and d.name in env and is_self:
            v = env[d.name]()
            if v:
                return v
        if isinstance(fnode, FUNC_TYPES) and s.parent is not None and not is_self:
            grp, gsc = stub_group(FuncVal(fnode, s.parent))
            for gn in grp:
                for gp in func_params(gn) + list(gn.args.kwonlyargs):
                    if gp.arg == d.name and gp.annotation is not None:
                        v = annotation_vals(gp.annotation, gsc, (gn.lineno, gn.col_offset))
                        if v:
                            return v
        if par.annotation is not None:
            v = annotation_vals(par.annotation, s.parent, (fnode.lineno, fnode.col_offset))
            if v:
                return v
        if env is not None and d.name in env:
            v = env[d.name]()
            if v:
                return v
        if env is None and not is_self and isinstance(fnode, FUNC_TYPES) and not s.module.is_stub:
            v = callsite_param_vals(d)
            if v:
                return v
        if is_self:
            decos = deco_names(fnode)
            cls = ClassVal(s.parent.node, s.parent.parent)
            if 'staticmethod' in decos:
                pass
            elif 'classmethod' in decos or fnode.name in ('__new__', '__init_subclass__',
                                                          '__class_getitem__'):
                return [cls]
            else:
                return [InstVal(cls)]
        dflt = param_default(fnode, par)
        if dflt is not None and s.parent is not None:
            v = infer_vals(dflt, s.parent, (fnode.lineno, fnode.col_offset))
            if v:
                return v
        return []
    if k == 'import':
        name, asname = d.extra
        mi = s.module
        if asname:
            mv = mi.resolve_import(name, 0, display=asname)
            if mv is None:
                return [ModVal(asname)]
            return [mv]
        top = name.split('.')[0]
        if name != top:
            mi.resolve_import(name, 0)  # make sure submodules are loaded
        mv = mi.resolve_import(top, 0)
        if mv is None:
            return [ModVal(top)]
        return [mv]
    if k == 'importfrom':
        modname, name, level = d.extra
        mi = s.module
        mv = mi.resolve_import(modname, level)
        if mv is not None:
            ok, v = attr_vals(mv, name)
            if ok:
                return v
        sub = (modname + '.' + name) if modname else name
        mv2 = mi.resolve_import(sub, level)
        if mv2 is not None:
            return [mv2]
        return []
    ctx_pos = d.pos
    if k in ('assign', 'with', 'for'):
        value, path, stmt = d.extra
        if k == 'assign':
            return infer_path_vals(value, path, s, ctx_pos)
        if k == 'with':
            if path:
                return []
            out = []
            for v in infer_vals(value, s, ctx_pos):
                out.extend(enter_vals(v))
            return out
        if k == 'for':
            el = element_vals(value, s, ctx_pos)
            return path_of_vals(el, path)
    if k == 'annassign':
        value, ann, stmt = d.extra
        if s.module.is_stub:
            v = annotation_vals(ann, s, ctx_pos)
            if v:
                return v
        if value is not None:
            v = infer_vals(value, s, ctx_pos)
            if v:
                return v
        return annotation_vals(ann, s, ctx_pos)
    if k == 'augassign':
        n = d.extra
        return infer_vals(ast.BinOp(left=ast.Name(id=n.target.id, ctx=ast.Load()), op=n.op,
                                    right=n.value), s, ctx_pos)
    if k == 'walrus':
        return infer_vals(d.extra, s, ctx_pos)
    if k == 'except':
        t = d.extra
        if t is None:
            return []
        nodes = t.elts if isinstance(t, ast.Tuple) else [t]
        out = []
        for e in nodes:
            for v in infer_vals(e, s, ctx_pos):
                iv = instantiate(v)
                if iv is not None:
                    out.append(iv)
        return out
    if k == 'comp':
        iter_node, path = d.extra
        el = element_vals(iter_node, s.parent if s.parent else s, INF_POS)
        return path_of_vals(el, path)
    return []


def _call_targets(fnode, s):
    """Callees whose calls bind fnode's parameters: (name, kind)."""
    fs = s.parent
    if fs is not None and fs.kind == 'class' and fnode.name == '__init__':
        return fs.node.name, 'init'
    return fnode.name, 'func'


def callsite_param_vals(d):
    """Dynamic parameter inference: union of argument types at call sites in the same file."""
    fnode, idx, pkind = d.extra
    s = d.scope
    mi = s.module
    if mi.tree is None or pkind in ('vararg', 'kwarg'):
        return []
    cname, ckind = _call_targets(fnode, s)
    parents = mi.parents()
    params = func_params(fnode)
    out = []
    found = False
    for call in ast.walk(mi.tree):
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        nm = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
        if nm != cname:
            continue
        cs = scope_of_node(mi, call, parents)
        off = None
        for v in infer_vals(fn, cs, _start(call)):
            if ckind == 'func' and isinstance(v, FuncVal) and v.node is fnode:
                off = 1 if v.bound is not None else 0
                break
            if ckind == 'init' and isinstance(v, ClassVal):
                for c in class_mro(v):
                    hit = [st for st in c.node.body if isinstance(st, FUNC_TYPES) and st.name == '__init__']
                    if hit:
                        if hit[-1] is fnode:
                            off = 1
                        break
                if off is not None:
                    break
        if off is None:
            continue
        found = True
        arg = None
        if pkind == 'pos':
            k = idx - off
            if k >= 0:
                stars = False
                for i, a in enumerate(call.args):
                    if isinstance(a, ast.Starred):
                        stars = True
                        break
                    if i == k:
                        arg = a
                        break
                if stars:
                    continue
        if arg is None:
            for kw in call.keywords:
                if kw.arg == d.name:
                    arg = kw.value
                    break
        if arg is None:
            dflt = param_default(fnode, d.node)
            if dflt is not None and s.parent is not None:
                out.extend(infer_vals(dflt, s.parent, (fnode.lineno, fnode.col_offset)))
            continue
        out.extend(infer_vals(arg, cs, _start(call)))
    if not found:
        return []
    return uniq(out)


def path_of_vals(vals, path):
    for p in path:
        nxt = []
        for v in vals:
            if p is None:
                nxt.append(RealInst(list))
                continue
            if isinstance(v, RealInst):
                if v.items is not None:
                    if p < len(v.items):
                        nxt.extend(v.items[p]())
                    continue
                if v.typ in (tuple, list) and v.elems is not None:
                    nxt.extend(v.elems())
                    continue
                if v.typ is str:
                    nxt.append(v)
        vals = uniq(nxt)
    return vals


def infer_path_vals(value, path, s, pos):
    if not path:
        return infer_vals(value, s, pos)
    node = value
    for i, p in enumerate(path):
        if p is None:
            return [RealInst(list)]
        if isinstance(node, (ast.Tuple, ast.List)) and not any(isinstance(e, ast.Starred) for e in node.elts):
            if p < len(node.elts):
                node = node.elts[p]
                continue
            return []
        return path_of_vals(infer_vals(node, s, pos), path[i:])
    return infer_vals(node, s, pos)


def enter_vals(v):
    if isinstance(v, InstVal):
        r = call_method_vals(v, '__enter__', [])
        if r is not None:
            return r
        return [v]
    if isinstance(v, RealInst):
        return [v]
    return []


def instantiate(v):
    if isinstance(v, ClassVal):
        return InstVal(v)
    if isinstance(v, RealVal) and inspect.isclass(v.obj):
        return RealInst(v.obj)
    return None


_TYPING_CONTAINERS = {
    'List': list, 'Dict': dict, 'Set': set, 'FrozenSet': frozenset, 'Tuple': tuple,
    'DefaultDict': dict, 'OrderedDict': dict, 'Deque': list,
    'list': list, 'dict': dict, 'set': set, 'frozenset': frozenset, 'tuple': tuple,
}
_TYPING_ITERABLES = ('Iterable', 'Iterator', 'Sequence', 'MutableSequence', 'Collection',
                     'Generator', 'AbstractSet', 'MutableSet')


def _ann_name(n):
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return n.attr
    return None


def annotation_vals(ann, s, pos, depth=0):
    if ann is None or s is None or depth > 10:
        return []
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value.strip(), mode='eval').body
        except SyntaxError:
            return []
    if isinstance(ann, ast.Constant) and ann.value is None:
        return [none_inst()]
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return uniq(annotation_vals(ann.left, s, pos, depth + 1) +
                    annotation_vals(ann.right, s, pos, depth + 1))
    if isinstance(ann, ast.Subscript):
        base = _ann_name(ann.value)
        sl = ann.slice
        args = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        if base == 'Optional':
            return uniq(annotation_vals(args[0], s, pos, depth + 1) + [none_inst()])
        if base == 'Union':
            out = []
            for a in args:
                out.extend(annotation_vals(a, s, pos, depth + 1))
            return uniq(out)
        if base in ('Annotated', 'ClassVar', 'Final', 'Required', 'NotRequired', 'ReadOnly'):
            return annotation_vals(args[0], s, pos, depth + 1)
        if base == 'Type' or base == 'type':
            out = []
            for v in infer_vals(args[0], s, INF_POS):
                if isinstance(v, (ClassVal, RealVal)):
                    out.append(v)
            return out
        typ = _TYPING_CONTAINERS.get(base)
        if typ is None and base in _TYPING_ITERABLES:
            typ = list
        if typ is not None:
            if typ is dict:
                kt = memo(lambda: annotation_vals(args[0], s, pos, depth + 1))
                vt = memo(lambda: annotation_vals(args[1], s, pos, depth + 1) if len(args) > 1 else [])
                return [RealInst(dict, elems=kt, values=vt)]
            if typ is tuple:
                if len(args) == 2 and isinstance(args[1], ast.Constant) and args[1].value is Ellipsis:
                    et = memo(lambda: annotation_vals(args[0], s, pos, depth + 1))
                    return [RealInst(tuple, elems=et)]
                items = [memo((lambda a: lambda: annotation_vals(a, s, pos, depth + 1))(a)) for a in args]

                def allel(items=items):
                    out = []
                    for it in items:
                        out.extend(it())
                    return uniq(out)
                return [RealInst(tuple, elems=memo(allel), items=items)]
            et = memo(lambda: annotation_vals(args[0], s, pos, depth + 1))
            return [RealInst(typ, elems=et)]
        ann = ann.value
    out = []
    for v in infer_vals(ann, s, INF_POS):
        iv = instantiate(v)
        if iv is not None:
            out.append(iv)
    return out


def _elts_thunk(elts, s, pos):
    def th():
        out = []
        for e in elts:
            if isinstance(e, ast.Starred):
                out.extend(element_vals(e.value, s, pos))
            else:
                out.extend(infer_vals(e, s, pos))
        return uniq(out)
    return memo(th)


def element_vals(iter_node, s, pos):
    if isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name) \
            and iter_node.func.id in ('range', 'enumerate', 'zip', 'reversed', 'sorted') \
            and not lookup_defs(iter_node.func.id, s, pos):
        fid = iter_node.func.id
        if fid == 'range':
            return [RealInst(int)]
        if fid in ('reversed', 'sorted') and iter_node.args:
            return element_vals(iter_node.args[0], s, pos)
        if fid == 'enumerate' and iter_node.args:
            arg = iter_node.args[0]
            return [RealInst(tuple, items=[const_thunk([RealInst(int)]),
                                           memo(lambda: element_vals(arg, s, pos))])]
        if fid == 'zip':
            items = [memo((lambda a: lambda: element_vals(a, s, pos))(a)) for a in iter_node.args]
            return [RealInst(tuple, items=items)]
    out = []
    for v in infer_vals(iter_node, s, pos):
        out.extend(iter_of(v))
    return uniq(out)


def iter_of(v):
    if isinstance(v, RealInst):
        if v.elems is not None:
            return v.elems()
        if v.typ is str:
            return [RealInst(str)]
        if v.typ in (bytes, bytearray, range):
            return [RealInst(int)]
        if v.typ is io.TextIOWrapper:
            return [RealInst(str)]
        return []
    if isinstance(v, RealVal) and isinstance(v.obj, str):
        return [RealInst(str)]
    if isinstance(v, InstVal):
        it = call_method_vals(v, '__iter__', [])
        if it:
            out = []
            for x in it:
                if isinstance(x, InstVal):
                    nx = call_method_vals(x, '__next__', [])
                    if nx:
                        out.extend(nx)
                        continue
                if isinstance(x, RealInst) and x.elems is not None:
                    out.extend(x.elems())
            return out
    return []


def infer_name_vals(name, scope, pos):
    ds = lookup_defs(name, scope, pos)
    if ds:
        out = []
        for d in ds:
            out.extend(infer_def_vals(d))
        return uniq(out)
    if name == PLACEHOLDER:
        return []
    v = builtin_value(name)
    return [v] if v is not None else []


def infer_name(name, scope, pos):
    return same_val(infer_name_vals(name, scope, pos))


_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b, ast.BitAnd: lambda a, b: a & b, ast.BitOr: lambda a, b: a | b,
    ast.BitXor: lambda a, b: a ^ b, ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
}
_OP_DUNDER = {
    ast.Add: '__add__', ast.Sub: '__sub__', ast.Mult: '__mul__', ast.Div: '__truediv__',
    ast.FloorDiv: '__floordiv__', ast.Mod: '__mod__', ast.Pow: '__pow__', ast.BitAnd: '__and__',
    ast.BitOr: '__or__', ast.BitXor: '__xor__', ast.LShift: '__lshift__', ast.RShift: '__rshift__',
    ast.MatMult: '__matmul__',
}


def call_method_vals(inst, mname, arg_thunks):
    """Call method `mname` of an instance; None if no such method."""
    ok, ms = attr_vals(inst, mname)
    if not ok:
        return None
    out = []
    for m in ms:
        if isinstance(m, FuncVal) and isinstance(m.node, FUNC_TYPES):
            params = func_params(m.node)
            mapping = {}
            idx = 0
            if m.bound is not None and params:
                mapping[params[0].arg] = const_thunk([m.bound])
                idx = 1
            for th in arg_thunks:
                if idx < len(params):
                    mapping[params[idx].arg] = th
                    idx += 1
            out.extend(return_vals(m, mapping))
    return uniq(out)


_SAFE_CALL_TYPES = (str, bytes, int, float, complex, list, dict, set, frozenset, tuple, bool)


def call_vals(f, node=None, s=None, pos=None):
    if f is None:
        return []
    if isinstance(f, ClassVal):
        return [InstVal(f, call=(node, s, pos, list(_envs)) if node is not None else None)]
    if isinstance(f, RealVal):
        obj = f.obj
        if inspect.isclass(obj):
            if obj is type or obj is super:
                return []
            if obj in (list, set, frozenset, tuple) and node is not None and node.args and s is not None:
                arg = node.args[0]
                return [RealInst(obj, elems=memo(lambda: element_vals(arg, s, pos)))]
            return [RealInst(obj)]
        for bname, rtype in BUILTIN_RETURNS.items():
            ok, bobj = safe_getattr(builtins, bname)
            if ok and bobj is obj:
                if bname == 'sorted' and node is not None and node.args and s is not None:
                    arg = node.args[0]
                    return [RealInst(list, elems=memo(lambda: element_vals(arg, s, pos)))]
                return [RealInst(rtype)]
        if obj is getattr(builtins, 'max', None) or obj is getattr(builtins, 'min', None):
            if node is not None and s is not None and node.args:
                if len(node.args) == 1:
                    return element_vals(node.args[0], s, pos)
                out = []
                for a in node.args:
                    out.extend(infer_vals(a, s, pos))
                return uniq(out)
            return []
        # bound methods of builtin sample objects: evaluate on samples
        self_obj = getattr(obj, '__self__', None)
        if (inspect.isbuiltin(obj) or type(obj).__name__ == 'method-wrapper') \
                and type(self_obj) in _SAFE_CALL_TYPES and node is not None:
            args = []
            for a in node.args:
                if isinstance(a, ast.Starred) or s is None:
                    return []
                av = same_val(infer_vals(a, s, pos))
                if isinstance(av, RealInst):
                    ok, smp = sample_of(av.typ)
                    if not ok:
                        return []
                    args.append(smp)
                else:
                    return []
            if node.keywords:
                return []
            try:
                tgt = getattr(copy.copy(self_obj), obj.__name__)
                with quiet():
                    res = tgt(*args)
                return [RealInst(type(res))]
            except BaseException:
                return []
        return []
    if isinstance(f, FuncVal):
        if isinstance(f.node, FUNC_TYPES):
            mapping = bind_args(f.node, node, s, pos, f.bound)
        else:
            mapping = bind_args(f.node, node, s, pos, None)
        return return_vals(f, mapping, call=(node, s, pos) if node is not None else None)
    if isinstance(f, InstVal):
        thunks = []
        if node is not None and s is not None:
            snap = list(_envs)
            thunks = [_arg_thunk(a, s, pos, snap) for a in node.args if not isinstance(a, ast.Starred)]
        r = call_method_vals(f, '__call__', thunks)
        return r or []
    return []


def call_value(f, node=None, s=None, pos=None):
    return same_val(call_vals(f, node, s, pos))


_ret_active = {}


def return_vals(f, mapping=None, call=None):
    node = f.node
    cnt = _ret_active.get(id(node), 0)
    if cnt >= 2 or sum(_ret_active.values()) > 30:
        _cuts[0] += 1
        return []
    _ret_active[id(node)] = cnt + 1
    try:
        with pushed_env(node, mapping):
            return uniq(_return_vals(f, mapping, call))
    except RecursionError:
        return []
    finally:
        _ret_active[id(node)] -= 1
        if not _ret_active[id(node)]:
            del _ret_active[id(node)]


def _return_vals(f, mapping, call=None):
    node = f.node
    if isinstance(node, ast.Lambda):
        fs = f.scope.module.node_scope.get(node) if f.scope else None
        if fs is None:
            return []
        return infer_vals(node.body, fs, INF_POS)
    if f.scope is None:
        return []
    grp, gsc = stub_group(f)
    if grp:
        chosen = choose_overloads(grp, gsc, f, call)
        v = []
        for n in chosen:
            if n.returns is not None:
                v.extend(annotation_vals(n.returns, gsc, INF_POS))
        if v:
            return uniq(v)
    if f.scope.module.is_stub:
        src = source_func(f)
        if src is not None:
            return return_vals(src, mapping, call)
        if node.returns is None:
            return []
    if node.returns is not None:
        v = annotation_vals(node.returns, f.scope, INF_POS)
        if v:
            return v
    fs = f.scope.module.node_scope.get(node)
    if fs is None:
        return []
    rets = []
    yields = []

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, FUNC_TYPES + (ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(c, (ast.Yield, ast.YieldFrom)):
                yields.append(c)
            if isinstance(c, ast.Return):
                rets.append(c)
            walk(c)
    for st in node.body:
        if isinstance(st, ast.Return):
            rets.append(st)
        walk(st)
    if isinstance(node, ast.AsyncFunctionDef):
        return []
    if yields:
        snapshot = list(_envs)

        def ys():
            saved = _envs[:]
            _envs[:] = snapshot
            try:
                out = []
                for y in yields:
                    if y.value is None:
                        out.append(none_inst())
                    elif isinstance(y, ast.YieldFrom):
                        out.extend(element_vals(y.value, fs, (y.lineno, y.col_offset)))
                    else:
                        out.extend(infer_vals(y.value, fs, (y.lineno, y.col_offset)))
                return uniq(out)
            finally:
                _envs[:] = saved
        return [RealInst(types.GeneratorType, elems=memo(ys))]
    if not rets:
        return [none_inst()]
    vals = []
    for r in rets:
        if r.value is None:
            vals.append(none_inst())
        else:
            vals.extend(infer_vals(r.value, fs, (r.lineno, r.col_offset)))
    return vals


# --- stubs: counterparts -------------------------------------------------------

def find_qual(mi, chain):
    """Nodes named chain[-1] inside the class/def path chain[:-1] of module mi."""
    if mi is None or mi.tree is None or not chain:
        return [], None
    owner = mi.tree
    for nm in chain[:-1]:
        cand = [n for n in owner.body if isinstance(n, FUNC_TYPES + (ast.ClassDef,)) and n.name == nm]
        if not cand:
            return [], None
        owner = cand[-1]
    nodes = [n for n in owner.body if isinstance(n, FUNC_TYPES + (ast.ClassDef,)) and n.name == chain[-1]]
    return nodes, mi.node_scope.get(owner)


def overload_filter(funcs):
    ov = [n for n in funcs if 'overload' in deco_names(n)]
    return ov or funcs[-1:]


def stub_group(f):
    """(stub function nodes, their container scope) providing types for FuncVal f."""
    node = f.node
    if not isinstance(node, FUNC_TYPES) or f.scope is None:
        return [], None
    mi = f.scope.module
    chain = scope_chain(f.scope) + [node.name]
    if mi.is_stub:
        nodes, sc = find_qual(mi, chain)
    else:
        st = mi.stub()
        if st is None:
            return [], None
        nodes, sc = find_qual(st, chain)
    funcs = [n for n in nodes if isinstance(n, FUNC_TYPES)]
    if not funcs or sc is None:
        return [], None
    return overload_filter(funcs), sc


def source_func(f):
    """Runtime FuncVal corresponding to stub FuncVal f (or None)."""
    mi = f.scope.module
    if not mi.is_stub or mi.source is None:
        return None
    nodes, sc = find_qual(mi.source, scope_chain(f.scope) + [f.node.name])
    funcs = [n for n in nodes if isinstance(n, FUNC_TYPES)]
    if not funcs or sc is None:
        return None
    return FuncVal(funcs[-1], sc, bound=f.bound)


def source_class_node(mi, scope, node):
    """For a stub class/function, the runtime (node, scope) when it exists."""
    if not mi.is_stub or mi.source is None:
        return None
    nodes, sc = find_qual(mi.source, scope_chain(scope) + [node.name])
    want = ast.ClassDef if isinstance(node, ast.ClassDef) else FUNC_TYPES
    nodes = [n for n in nodes if isinstance(n, want)]
    if not nodes or sc is None:
        return None
    return nodes[-1], sc


def _vals_compatible(args, anns):
    if not args or not anns:
        return True
    for a in args:
        for t in anns:
            if isinstance(a, RealInst) and isinstance(t, RealInst):
                try:
                    if a.typ is t.typ or issubclass(a.typ, t.typ):
                        return True
                except Exception:
                    return True
            elif isinstance(a, InstVal) and isinstance(t, InstVal):
                if is_subclass_val(a.cls, t.cls):
                    return True
            elif isinstance(t, RealInst) and t.typ is object:
                return True
            elif type(a) is not type(t) and not (isinstance(a, (RealInst, InstVal)) and
                                                 isinstance(t, (RealInst, InstVal))):
                return True
    return False


def choose_overloads(grp, sc, f, call):
    if len(grp) <= 1 or call is None or call[0] is None:
        return grp
    cnode, cs, cpos = call
    if any(isinstance(a, ast.Starred) for a in cnode.args) or any(k.arg is None for k in cnode.keywords):
        return grp
    for ov in grp:
        params = func_params(ov)
        off = 1 if f.bound is not None and params else 0
        a = ov.args
        pos_params = params[off:]
        if len(cnode.args) > len(pos_params) and a.vararg is None:
            continue
        names = set(p.arg for p in params[off:]) | set(p.arg for p in a.kwonlyargs)
        posonly = set(p.arg for p in getattr(a, 'posonlyargs', []))
        if any(k.arg not in names or k.arg in posonly for k in cnode.keywords) and a.kwarg is None:
            continue
        supplied = set(p.arg for p in pos_params[:len(cnode.args)]) | set(k.arg for k in cnode.keywords)
        ndef = len(a.defaults)
        required = [p.arg for i, p in enumerate(params) if off <= i < len(params) - ndef]
        required += [p.arg for p, dv in zip(a.kwonlyargs, a.kw_defaults) if dv is None]
        if any(r not in supplied for r in required):
            continue
        ok = True
        bound_args = list(zip(pos_params, cnode.args)) + \
            [(p, k.value) for k in cnode.keywords for p in params[off:] + a.kwonlyargs if p.arg == k.arg]
        for p, argnode in bound_args:
            if p.annotation is None:
                continue
            av = infer_vals(argnode, cs, cpos)
            tv = annotation_vals(p.annotation, sc, INF_POS)
            if not _vals_compatible(av, tv):
                ok = False
                break
        if ok:
            return [ov]
    return grp


def infer_return(f):
    return same_val(return_vals(f))


def infer(node, s, pos):
    return same_val(infer_vals(node, s, pos))


def infer_vals(node, s, pos):
    if node is None or s is None:
        return []
    if _depth[0] > 120 or _out_of_time():
        _cuts[0] += 1
        return []
    _depth[0] += 1
    try:
        vals = uniq(_infer_vals(node, s, pos) or [])
        if isinstance(node, (ast.Name, ast.Attribute)) and pos != INF_POS:
            vals = apply_narrowing(node, vals, s, pos)
        return vals
    except RecursionError:
        return []
    finally:
        _depth[0] -= 1


def _infer_vals(node, s, pos):
    if isinstance(node, ast.Constant):
        return [RealInst(type(node.value))]
    if isinstance(node, ast.JoinedStr):
        return [RealInst(str)]
    if isinstance(node, ast.List):
        return [RealInst(list, elems=_elts_thunk(node.elts, s, pos))]
    if isinstance(node, ast.Tuple):
        items = None
        if not any(isinstance(e, ast.Starred) for e in node.elts):
            items = [memo((lambda e: lambda: infer_vals(e, s, pos))(e)) for e in node.elts]
        return [RealInst(tuple, elems=_elts_thunk(node.elts, s, pos), items=items)]
    if isinstance(node, ast.Set):
        return [RealInst(set, elems=_elts_thunk(node.elts, s, pos))]
    if isinstance(node, ast.Dict):
        keys = [k for k in node.keys if k is not None]
        return [RealInst(dict, elems=_elts_thunk(keys, s, pos),
                         values=_elts_thunk(node.values, s, pos))]
    if isinstance(node, COMP_TYPES):
        cs = s.module.node_scope.get(node) if s.module else None
        typ = {ast.ListComp: list, ast.SetComp: set, ast.DictComp: dict,
               ast.GeneratorExp: types.GeneratorType}[type(node)]
        if cs is None:
            return [RealInst(typ)]
        if isinstance(node, ast.DictComp):
            return [RealInst(dict, elems=memo(lambda: infer_vals(node.key, cs, INF_POS)),
                             values=memo(lambda: infer_vals(node.value, cs, INF_POS)))]
        return [RealInst(typ, elems=memo(lambda: infer_vals(node.elt, cs, INF_POS)))]
    if isinstance(node, ast.Lambda):
        return [FuncVal(node, s)]
    if isinstance(node, ast.Name):
        return infer_name_vals(node.id, s, pos)
    if isinstance(node, ast.NamedExpr):
        return infer_vals(node.value, s, pos)
    if isinstance(node, ast.Attribute):
        if node.attr == PLACEHOLDER:
            return []
        out = []
        for v in infer_vals(node.value, s, pos):
            out.extend(attr_vals(v, node.attr)[1])
        return out
    if isinstance(node, ast.Call):
        out = []
        for f in infer_vals(node.func, s, pos):
            out.extend(call_vals(f, node, s, pos))
        return out
    if isinstance(node, ast.Compare):
        return [RealInst(bool)]
    if isinstance(node, ast.BoolOp):
        out = []
        for v in node.values:
            out.extend(infer_vals(v, s, pos))
        return out
    if isinstance(node, ast.IfExp):
        return infer_vals(node.body, s, pos) + infer_vals(node.orelse, s, pos)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return [RealInst(bool)]
        out = []
        for v in infer_vals(node.operand, s, pos):
            if isinstance(v, RealInst) and v.typ in (int, float, complex, bool):
                out.append(RealInst(int) if v.typ is bool else v)
        return out
    if isinstance(node, ast.BinOp):
        lvs = infer_vals(node.left, s, pos)
        rvs = None
        out = []
        for lv in lvs:
            if isinstance(lv, InstVal):
                mname = _OP_DUNDER.get(type(node.op))
                if mname:
                    snap = list(_envs)
                    r = call_method_vals(lv, mname, [_arg_thunk(node.right, s, pos, snap)])
                    if r:
                        out.extend(r)
                continue
            if not isinstance(lv, RealInst):
                continue
            if lv.typ in (str, bytes) and isinstance(node.op, ast.Mod):
                out.append(RealInst(lv.typ))
                continue
            if rvs is None:
                rvs = infer_vals(node.right, s, pos)
            for rv in rvs:
                if not isinstance(rv, RealInst):
                    continue
                if lv.typ is rv.typ and lv.typ in (list, tuple) and isinstance(node.op, ast.Add):
                    a, b = lv, rv

                    def both(a=a, b=b):
                        return uniq((a.elems() if a.elems else []) + (b.elems() if b.elems else []))
                    out.append(RealInst(lv.typ, elems=memo(both)))
                    continue
                if lv.typ in (list, tuple, str) and rv.typ is int and isinstance(node.op, ast.Mult):
                    out.append(RealInst(lv.typ, elems=lv.elems))
                    continue
                ok1, a = sample_of(lv.typ)
                ok2, b = sample_of(rv.typ)
                fn = _OPS.get(type(node.op))
                if ok1 and ok2 and fn is not None:
                    try:
                        out.append(RealInst(type(fn(a, b))))
                    except Exception:
                        pass
        return out
    if isinstance(node, ast.Subscript):
        out = []
        for v in infer_vals(node.value, s, pos):
            out.extend(subscript_vals(v, node, s, pos))
        return out
    if isinstance(node, ast.Await):
        return []
    return []


def subscript_vals(v, node, s, pos):
    if isinstance(v, RealVal) and not inspect.isclass(v.obj) and type(v.obj) in SAMPLES:
        v = RealInst(type(v.obj))
    sl = node.slice
    if isinstance(v, RealInst):
        if isinstance(sl, ast.Slice):
            if v.typ in (str, bytes, bytearray, list, tuple):
                return [RealInst(v.typ, elems=v.elems)]
            return []
        if v.typ is str:
            return [v]
        if v.typ in (bytes, bytearray):
            return [RealInst(int)]
        if v.typ is tuple and v.items is not None:
            idx = None
            if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                idx = sl.value
            elif isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub) \
                    and isinstance(sl.operand, ast.Constant) and isinstance(sl.operand.value, int):
                idx = -sl.operand.value
            if idx is not None:
                try:
                    return v.items[idx]()
                except IndexError:
                    return []
        if v.typ in (list, tuple) and v.elems is not None:
            return v.elems()
        if v.typ is dict and v.values is not None:
            return v.values()
        return []
    if isinstance(v, InstVal):
        snap = list(_envs)
        r = call_method_vals(v, '__getitem__', [_arg_thunk(sl, s, pos, snap)])
        return r or []
    return []


# ---------------------------------------------------------------------------
# Type narrowing (isinstance / is None)
# ---------------------------------------------------------------------------

def expr_key(e):
    if isinstance(e, ast.Name):
        return ('n', e.id)
    if isinstance(e, ast.Attribute):
        k = expr_key(e.value)
        return None if k is None else ('a', k, e.attr)
    return None


def _is_none(e):
    return isinstance(e, ast.Constant) and e.value is None


def narrow_conds(test, positive):
    out = []
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return narrow_conds(test.operand, not positive)
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And) and positive:
            for v in test.values:
                out.extend(narrow_conds(v, True))
        elif isinstance(test.op, ast.Or) and not positive:
            for v in test.values:
                out.extend(narrow_conds(v, False))
        return out
    if isinstance(test, ast.NamedExpr):
        return narrow_conds(test.target, positive)
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id == 'isinstance' \
            and len(test.args) == 2:
        k = expr_key(test.args[0])
        if k is not None:
            out.append((k, 'isinstance', test.args[1], positive, test))
        return out
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        left, right = test.left, test.comparators[0]
        if isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
            pos_is = isinstance(op, (ast.Is, ast.Eq))
            target = None
            if _is_none(right):
                target = left
            elif _is_none(left):
                target = right
            k = expr_key(target) if target is not None else None
            if k is not None:
                out.append((k, 'none', None, positive == pos_is, test))
        return out
    k = expr_key(test)
    if k is not None and positive:
        out.append((k, 'truthy', None, True, test))
    return out


def _terminates(stmts):
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break))


def collect_narrowings(stmts, pos, out):
    for n in stmts:
        if _start(n) > pos:
            return
        if _start(n) <= pos <= _end(n):
            if isinstance(n, (ast.If, ast.While)):
                if n.body and _start(n.body[0]) <= pos <= _end(n.body[-1]):
                    out.append((n.test, True))
                    collect_narrowings(n.body, pos, out)
                elif n.orelse and _start(n.orelse[0]) <= pos:
                    out.append((n.test, False))
                    collect_narrowings(n.orelse, pos, out)
                return
            if isinstance(n, FUNC_TYPES + (ast.ClassDef,)):
                return
            for field in ('body', 'orelse', 'finalbody'):
                sub = getattr(n, field, None)
                if sub and isinstance(sub, list) and _start(sub[0]) <= pos <= _end(sub[-1]):
                    collect_narrowings(sub, pos, out)
                    return
            for h in getattr(n, 'handlers', None) or []:
                if h.body and _start(h) <= pos <= _end(h):
                    collect_narrowings(h.body, pos, out)
                    return
            for c in getattr(n, 'cases', None) or []:
                if c.body and _start(c.body[0]) <= pos <= _end(c.body[-1]):
                    collect_narrowings(c.body, pos, out)
                    return
            return
        if isinstance(n, ast.If):
            bt = _terminates(n.body)
            et = _terminates(n.orelse) if n.orelse else False
            if bt and not et:
                out.append((n.test, False))
            elif et and not bt:
                out.append((n.test, True))


def class_of_val(v):
    if isinstance(v, InstVal):
        return v.cls
    if isinstance(v, RealInst):
        return RealVal(v.typ)
    return None


def is_subclass_val(c, base):
    """c, base: ClassVal or RealVal(class)."""
    if isinstance(c, ClassVal) and isinstance(base, ClassVal):
        return any(m.node is base.node for m in class_mro(c))
    if isinstance(c, RealVal) and isinstance(base, RealVal):
        try:
            return issubclass(c.obj, base.obj)
        except Exception:
            return False
    if isinstance(c, ClassVal) and isinstance(base, RealVal) and base.obj is object:
        return True
    return False


def _rebound_between(key, s, a, b):
    if key[0] != 'n':
        return False
    for d in s.defs.get(key[1], ()):
        if a < d.pos < b:
            return True
    return False


def apply_narrowing(node, vals, s, pos):
    key = expr_key(node)
    if key is None:
        return vals
    fs = s
    while fs is not None and fs.kind == 'comp':
        fs = fs.parent
    if fs is None:
        return vals
    body = scope_body(fs)
    if not body:
        return vals
    tests = []
    collect_narrowings(body, pos, tests)
    for test, positive in tests:
        for k, kind, payload, pos_flag, tnode in narrow_conds(test, positive):
            if k != key:
                continue
            if _rebound_between(key, fs, _end(tnode), pos):
                continue
            vals = _narrow(vals, kind, payload, pos_flag, fs, _start(tnode))
    return vals


def _narrow(vals, kind, payload, positive, s, tpos):
    if kind == 'none':
        if positive:
            return [none_inst()]
        return [v for v in vals if not (isinstance(v, RealInst) and v.typ is type(None))]
    if kind == 'truthy':
        return [v for v in vals if not (isinstance(v, RealInst) and v.typ is type(None))]
    if kind == 'isinstance':
        cnodes = payload.elts if isinstance(payload, ast.Tuple) else [payload]
        classes = []
        for cn in cnodes:
            for c in infer_vals(cn, s, tpos):
                if isinstance(c, ClassVal) or (isinstance(c, RealVal) and inspect.isclass(c.obj)):
                    classes.append(c)
        if not classes:
            return vals
        if positive:
            res = []
            for v in vals:
                vc = class_of_val(v)
                if vc is None:
                    continue
                for c in classes:
                    if is_subclass_val(vc, c):
                        res.append(v)
                        break
                    if is_subclass_val(c, vc):
                        res.append(instantiate(c))
            if not res:
                res = [instantiate(c) for c in classes]
            return uniq(res)
        res = []
        for v in vals:
            vc = class_of_val(v)
            if vc is not None and any(is_subclass_val(vc, c) for c in classes):
                continue
            res.append(v)
        return res
    return vals


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------

def class_mro(cls, seen=None):
    if seen is None:
        seen = set()
    if id(cls.node) in seen:
        return []
    seen.add(id(cls.node))
    out = [cls]
    for b in cls.node.bases:
        bvs = infer_vals(b, cls.scope, (cls.node.lineno, cls.node.col_offset)) if cls.scope else []
        for bv in bvs:
            if isinstance(bv, ClassVal):
                for c in class_mro(bv, seen):
                    if all(c.node is not o.node for o in out):
                        out.append(c)
    return out


def class_scope_of(cls):
    mod = cls.scope.module if cls.scope else None
    if mod is None:
        return None
    return mod.node_scope.get(cls.node)


def is_dataclass(cls):
    for dec in cls.node.decorator_list:
        d = dec.func if isinstance(dec, ast.Call) else dec
        nm = _ann_name(d)
        if nm == 'dataclass':
            return True
        try:
            import dataclasses
            for v in infer_vals(d, cls.scope, (cls.node.lineno, cls.node.col_offset)):
                if isinstance(v, RealVal) and v.obj is dataclasses.dataclass:
                    return True
        except Exception:
            pass
    return False


class AttrDef(object):
    """An assignment `self.<name> = ...` inside a method."""

    def __init__(self, name, node, kind, value, path, stmt, fs, method, cls):
        self.name = name
        self.node = node
        self.kind = kind
        self.value = value
        self.path = path
        self.stmt = stmt
        self.fs = fs
        self.method = method
        self.cls = cls
        self.pos = (stmt.lineno, stmt.col_offset)
        self.lineno = stmt.lineno
        self.tainted = False


def collect_attr_assigns(body, selfname, cb):
    def handle_target(t, value, path, stmt, kind):
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == selfname:
            if t.attr != PLACEHOLDER:
                cb(t.attr, t, kind, value, path, stmt)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for i, e in enumerate(t.elts):
                if isinstance(e, ast.Starred):
                    handle_target(e.value, value, path + (None,), stmt, kind)
                else:
                    handle_target(e, value, path + (i,), stmt, kind)

    def walk(nodes):
        for n in nodes:
            if isinstance(n, FUNC_TYPES + (ast.ClassDef,)):
                continue
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    handle_target(t, n.value, (), n, 'assign')
            elif isinstance(n, ast.AnnAssign):
                handle_target(n.target, n.value, (), n, 'ann')
            elif isinstance(n, ast.AugAssign):
                handle_target(n.target, n.value, (), n, 'aug')
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                handle_target(n.target, n.iter, (), n, 'for')
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        handle_target(item.optional_vars, item.context_expr, (), n, 'with')
            for field in ('body', 'orelse', 'finalbody'):
                sub = getattr(n, field, None)
                if sub and isinstance(sub, list):
                    walk(sub)
            for h in getattr(n, 'handlers', None) or []:
                walk(h.body)
            for c in getattr(n, 'cases', None) or []:
                walk(c.body)
    walk(body)


_self_attr_cache = {}


def self_attrs(cls):
    """{name: [AttrDef, ...]} reaching the end of each method, union over methods."""
    key = id(cls.node)
    if key in _self_attr_cache:
        return _self_attr_cache[key]
    out = {}
    _self_attr_cache[key] = out
    mod = cls.scope.module if cls.scope else None
    if mod is None:
        return out
    methods = [st for st in cls.node.body if isinstance(st, FUNC_TYPES)]
    methods.sort(key=lambda m: (m.name != '__init__',))
    for m in methods:
        decos = deco_names(m)
        if 'staticmethod' in decos or 'classmethod' in decos:
            continue
        params = func_params(m)
        if not params:
            continue
        fs = mod.node_scope.get(m)
        if fs is None:
            continue
        per = {}

        def cb(name, t, kind, value, path, stmt, per=per, fs=fs, m=m):
            per.setdefault(name, []).append(AttrDef(name, t, kind, value, path, stmt, fs, m, cls))
        collect_attr_assigns(m.body, params[0].arg, cb)
        for name, ads in per.items():
            st = Flow(ads, INF_POS).run(m.body, [])
            if not st:
                st = ads
            lst = out.setdefault(name, [])
            for a in st:
                if a not in lst:
                    lst.append(a)
    return out


def attrdef_vals(a, inst=None):
    ikey = None
    if inst is not None:
        ikey = (id(inst.cls.node), id(inst.call[0]) if inst.call and inst.call[0] is not None else None)
    key = ('attr', id(a), ikey)
    if key in _cache:
        return _cache[key]
    if key in _infer_stack or _out_of_time():
        _cuts[0] += 1
        return []
    _infer_stack.add(key)
    try:
        return _cached(key, lambda: uniq(_attrdef_vals(a, inst)))
    finally:
        _infer_stack.discard(key)


def _attrdef_vals(a, inst=None):
    pos = a.pos
    fs = a.fs
    mapping = None
    if inst is not None:
        if a.method.name == '__init__' and inst.call is not None and inst.call[0] is not None:
            call, cs, cpos, snap = inst.call
            mapping = bind_args(a.method, call, cs, cpos, inst, snapshot=snap)
        else:
            params = func_params(a.method)
            mapping = {params[0].arg: const_thunk([inst])} if params else None
    with pushed_env(a.method, mapping):
        if a.kind == 'ann':
            v = infer_path_vals(a.value, a.path, fs, pos) if a.value is not None else []
            if v:
                return v
            return annotation_vals(a.stmt.annotation, fs, pos)
        if a.kind == 'assign':
            return infer_path_vals(a.value, a.path, fs, pos)
        if a.kind == 'aug':
            return infer_vals(ast.BinOp(left=a.node, op=a.stmt.op, right=a.value), fs, pos)
        if a.kind == 'for':
            return path_of_vals(element_vals(a.value, fs, pos), a.path)
        if a.kind == 'with':
            out = []
            for v in infer_vals(a.value, fs, pos):
                out.extend(enter_vals(v))
            return path_of_vals(out, a.path) if a.path else out
    return []


def _class_body_reaching(cs, name):
    ds = reaching_defs(cs, name, INF_POS, allow_tainted=True)
    out = []
    for d in ds:
        if not isinstance(d, StarHit) and d.kind == 'def' and \
                ({'setter', 'deleter'} & deco_names(d.node)):
            prev = [x for x in cs.defs.get(name, ()) if x.pos < d.pos and x.kind == 'def'
                    and not ({'setter', 'deleter'} & deco_names(x.node))]
            if prev:
                d = prev[-1]
        if d not in out:
            out.append(d)
    return out


_attr_map_cache = {}


def class_attr_map(cls, instance):
    key = (id(cls.node), bool(instance))
    if key in _attr_map_cache:
        return _attr_map_cache[key]
    before = _cuts[0]
    res = _class_attr_map(cls, instance)
    if _cuts[0] == before:
        _attr_map_cache[key] = res
    return res


def _class_attr_map(cls, instance):
    """{name: [(kind, obj, ClassVal)]} with kind in 'def', 'field', 'self'."""
    out = {}
    for c0 in reversed(class_mro(cls)):
        for c, is_stub in class_pairs(c0):
            cs = class_scope_of(c)
            if is_stub:
                if cs is not None:
                    for name in cs.visible(None):
                        ds = _class_body_reaching(cs, name)
                        if not ds:
                            continue
                        ents = [('stub', d, c) for d in ds]
                        out[name] = ents + [e for e in out.get(name, []) if e[0] != 'stub']
                continue
            dc = instance and is_dataclass(c)
            body = {}
            if cs is not None:
                for name in cs.visible(None):
                    ds = _class_body_reaching(cs, name)
                    if not ds:
                        continue
                    ents = []
                    for d in ds:
                        kind = 'def'
                        if dc and not isinstance(d, StarHit) and d.kind == 'annassign':
                            kind = 'field'
                        ents.append((kind, d, c))
                    body[name] = ents
                    out[name] = ents
            if instance:
                for name, ads in self_attrs(c).items():
                    ents = [('self', a, c) for a in ads]
                    if name in body and all(not isinstance(d, StarHit) and d.kind not in ('def', 'class')
                                            for _, d, _ in body[name]):
                        ents = ents + body[name]
                    out[name] = ents
    out.pop(PLACEHOLDER, None)
    return out


def class_pairs(c):
    """[(ClassVal, is_stub)] for a class: runtime version first, then its stub."""
    if c.scope is None:
        return [(c, False)]
    mi = c.scope.module
    chain = scope_chain(c.scope) + [c.node.name]
    if mi.is_stub:
        out = []
        if mi.source is not None:
            nodes, sc = find_qual(mi.source, chain)
            nodes = [n for n in nodes if isinstance(n, ast.ClassDef)]
            if nodes and sc is not None:
                out.append((ClassVal(nodes[-1], sc), False))
        out.append((c, True))
        return out
    st = mi.stub()
    out = [(c, False)]
    if st is not None:
        nodes, sc = find_qual(st, chain)
        nodes = [n for n in nodes if isinstance(n, ast.ClassDef)]
        if nodes and sc is not None:
            out.append((ClassVal(nodes[-1], sc), True))
    return out


def type_ents(ents):
    st = [e for e in ents if e[0] == 'stub']
    return st or ents


def nav_ents(ents):
    src = [e for e in ents if e[0] != 'stub']
    return src or ents


def _source_vals(ent, v, eval_property=True):
    kind, x, c = ent
    inst = v if isinstance(v, InstVal) else None
    if kind == 'self':
        return attrdef_vals(x, inst)
    if kind == 'field':
        value, ann, stmt = x.extra
        r = annotation_vals(ann, x.scope, x.pos)
        if r:
            return r
        return infer_def_vals(x)
    vals = infer_def_vals(x)
    out = []
    for fv in vals:
        if isinstance(fv, FuncVal) and isinstance(fv.node, FUNC_TYPES) and fv.bound is None \
                and not isinstance(x, StarHit) and x.kind == 'def':
            decos = deco_names(fv.node)
            if 'staticmethod' in decos:
                out.append(fv)
            elif 'classmethod' in decos:
                out.append(FuncVal(fv.node, fv.scope, bound=v.cls if inst else v))
            elif inst is not None and eval_property and ({'property', 'cached_property'} & decos):
                out.extend(return_vals(FuncVal(fv.node, fv.scope, bound=inst),
                                       bind_args(fv.node, None, None, None, inst)))
            elif inst is not None:
                out.append(FuncVal(fv.node, fv.scope, bound=inst))
            else:
                out.append(fv)
        else:
            out.append(fv)
    return out


def real_attr_listing(obj, public_only):
    names = safe_dir(obj)
    out = []
    for n in names:
        if public_only and n.startswith('_'):
            continue
        out.append(n)
    return out


def attributes(v):
    """Return list of (name, value) for completion after `v.`."""
    if v is None:
        return []
    if isinstance(v, ModVal):
        if v.local is not None:
            names = [n for n in v.local.top_names() if not n.startswith('_')]
            for n in v.local.submodule_names():
                if n not in names and not n.startswith('_'):
                    names.append(n)
            res = []
            for n in names:
                ok, vals = v.local.attr(n)
                res.append((n, same_val(vals)))
            return res
        if v.real is not None:
            res = []
            for n in real_attr_listing(v.real, True):
                ok, o = safe_getattr(v.real, n)
                res.append((n, wrap_real(o, n) if ok else None))
            return res
        return []
    if isinstance(v, (ClassVal, InstVal)):
        cls = v if isinstance(v, ClassVal) else v.cls
        m = class_attr_map(cls, isinstance(v, InstVal))
        res = []
        for n, ents in m.items():
            if n == PLACEHOLDER:
                continue
            vals = []
            for e in type_ents(ents):
                vals.extend(_source_vals(e, v, eval_property=False))
            res.append((n, same_val(vals)))
        return res
    if isinstance(v, RealInst):
        ok, smp = sample_of(v.typ)
        target = smp if ok else v.typ
        res = []
        for n in safe_dir(target):
            ok2, o = safe_getattr(target, n)
            res.append((n, wrap_real(o, n) if ok2 else None))
        return res
    if isinstance(v, RealVal):
        res = []
        for n in safe_dir(v.obj):
            ok2, o = safe_getattr(v.obj, n)
            res.append((n, wrap_real(o, n) if ok2 else None))
        return res
    if isinstance(v, FuncVal):
        return attributes(RealInst(types.FunctionType))
    return []


def attr_vals(v, name):
    """Return (found, [values]) for attribute `name` of value v."""
    if v is None:
        return False, []
    if isinstance(v, ModVal):
        if v.local is not None:
            return v.local.attr(name)
        if v.real is not None:
            ok, o = safe_getattr(v.real, name)
            if ok:
                return True, [wrap_real(o, name)]
            mod = real_import(v.real.__name__ + '.' + name)
            if mod is not None:
                return True, [ModVal(name, real=mod)]
        return False, []
    if isinstance(v, (ClassVal, InstVal)):
        cls = v if isinstance(v, ClassVal) else v.cls
        m = class_attr_map(cls, isinstance(v, InstVal))
        if name in m:
            out = []
            for e in type_ents(m[name]):
                out.extend(_source_vals(e, v))
            return True, uniq(out)
        if isinstance(v, ClassVal) and name in ('__name__', '__qualname__', '__module__', '__doc__'):
            return True, [RealInst(str)]
        return False, []
    if isinstance(v, RealInst):
        ok, smp = sample_of(v.typ)
        target = smp if ok else v.typ
        ok2, o = safe_getattr(target, name)
        if not ok2:
            return False, []
        if v.typ is dict and v.values is not None and name in ('values', 'get', 'pop', 'setdefault'):
            pass
        return True, [wrap_real(o, name)]
    if isinstance(v, RealVal):
        ok2, o = safe_getattr(v.obj, name)
        return (True, [wrap_real(o, name)]) if ok2 else (False, [])
    if isinstance(v, FuncVal):
        return attr_vals(RealInst(types.FunctionType), name)
    return False, []


def attr_value(v, name):
    ok, vals = attr_vals(v, name)
    return ok, same_val(vals)


# ---------------------------------------------------------------------------
# Cursor context
# ---------------------------------------------------------------------------

def in_string_or_comment(lines, line_no, col, source):
    # Multi-line strings via tokenize.
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in toks:
            if tok.start > (line_no, col):
                break
            if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                if tok.start < (line_no, col) < tok.end:
                    return True
    except Exception:
        pass
    # Single-line scan of the current line.
    text = lines[line_no - 1][:col]
    i = 0
    quote = None
    while i < len(text):
        c = text[i]
        if quote is None:
            if c == '#':
                return True
            if c in '"\'':
                if text[i:i + 3] == c * 3:
                    quote = c * 3
                    i += 3
                    continue
                quote = c
        else:
            if c == '\\':
                i += 2
                continue
            if text[i:i + len(quote)] == quote:
                i += len(quote)
                quote = None
                continue
        i += 1
    return quote is not None


ACCEPT_EXPR = (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Constant, ast.JoinedStr,
               ast.List, ast.Dict, ast.Set, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def find_receiver(text):
    text = text.rstrip()
    if not text:
        return None
    for k in range(len(text)):
        if k > 0 and is_ident_char(text[k - 1]) and is_ident_char(text[k]):
            continue
        sub = text[k:].strip()
        if not sub:
            continue
        try:
            e = ast.parse(sub, mode='eval').body
        except Exception:
            continue
        if isinstance(e, ACCEPT_EXPR) or (isinstance(e, ast.Tuple) and sub.startswith('(')):
            if isinstance(e, ast.Constant) and isinstance(e.value, (int, float, complex)) \
                    and not isinstance(e.value, bool) and sub[-1:].isdigit() and is_ident_char(sub[0]):
                # `1.` is a float literal, not an attribute access.
                return None
            return e
    return None


def bracket_closers(text):
    stack = []
    quote = None
    i = 0
    pairs = {'(': ')', '[': ']', '{': '}'}
    while i < len(text):
        c = text[i]
        if quote is None:
            if c == '#':
                break
            if c in '"\'':
                if text[i:i + 3] == c * 3:
                    quote = c * 3
                    i += 3
                    continue
                quote = c
            elif c in pairs:
                stack.append(pairs[c])
            elif c in ')]}':
                if stack and stack[-1] == c:
                    stack.pop()
        else:
            if c == '\\':
                i += 2
                continue
            if text[i:i + len(quote)] == quote:
                i += len(quote)
                quote = None
                continue
        i += 1
    return ''.join(reversed(stack))


def find_placeholder(tree, line_no):
    parents = {}
    target = None
    for node in ast.walk(tree):
        for c in ast.iter_child_nodes(node):
            parents[c] = node
        if target is None and getattr(node, 'lineno', None) == line_no:
            if (isinstance(node, ast.Name) and node.id == PLACEHOLDER) or \
                    (isinstance(node, ast.Attribute) and node.attr == PLACEHOLDER):
                target = node
    if target is None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == PLACEHOLDER or \
                    isinstance(node, ast.Attribute) and node.attr == PLACEHOLDER:
                target = node
                break
    if target is None:
        return None
    path = [target]
    n = target
    while n in parents:
        n = parents[n]
        path.append(n)
    return list(reversed(path))  # root ... target


def scope_from_path(path, node_scope, module_scope):
    scope = module_scope
    for i, n in enumerate(path[:-1]):
        child = path[i + 1]
        inside = False
        if isinstance(n, FUNC_TYPES + (ast.ClassDef,)):
            inside = child in n.body
        elif isinstance(n, ast.Lambda):
            inside = child is n.body
        elif isinstance(n, COMP_TYPES):
            inside = True
            if n.generators and child is n.generators[0] and i + 2 < len(path) \
                    and path[i + 2] is n.generators[0].iter:
                inside = False
        if inside and n in node_scope:
            scope = node_scope[n]
    return scope


def _pos_in(node, line_no, col, strict_start=True, end_inclusive=True):
    start = (node.lineno, node.col_offset)
    end = (getattr(node, 'end_lineno', None) or node.lineno, getattr(node, 'end_col_offset', None) or 0)
    p = (line_no, col)
    ok_start = start < p if strict_start else start <= p
    ok_end = p <= end if end_inclusive else p < end
    return ok_start and ok_end


def scope_by_indent(tree, node_scope, module_scope, lines, line_no, col):
    cur = lines[line_no - 1]
    indent = min(leading_ws(cur), col) if cur.strip() else col
    best = None
    for n in ast.walk(tree):
        inside = False
        if isinstance(n, ast.Lambda):
            inside = _pos_in(n.body, line_no, col, strict_start=False)
        elif isinstance(n, COMP_TYPES):
            inside = _pos_in(n, line_no, col, end_inclusive=False)
            if inside and n.generators and _pos_in(n.generators[0].iter, line_no, col, strict_start=False):
                inside = False
        elif isinstance(n, FUNC_TYPES + (ast.ClassDef,)) and n.body and n.lineno <= line_no:
            end = getattr(n, 'end_lineno', n.lineno) or n.lineno
            b0 = n.body[0]
            if n.lineno == line_no:
                if b0.lineno == line_no and col >= b0.col_offset:
                    inside = True
            elif line_no <= end:
                inside = True
            elif b0.lineno != n.lineno and indent > n.col_offset:
                inside = True
                for k in range(end + 1, line_no):
                    l = lines[k - 1]
                    st = l.strip()
                    if not st or st.startswith('#'):
                        continue
                    if leading_ws(l) <= n.col_offset:
                        inside = False
                        break
        if inside and n in node_scope:
            if best is None or (n.lineno, n.col_offset) > (best.lineno, best.col_offset):
                best = n
    if best is not None:
        return node_scope[best]
    return module_scope


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

def matches(name, prefix, fuzzy):
    if not prefix:
        return True
    n = name.lower()
    p = prefix.lower()
    if not fuzzy:
        return n.startswith(p)
    i = 0
    for ch in n:
        if i < len(p) and ch == p[i]:
            i += 1
    return i == len(p)


def group_of(name, is_kw):
    if is_kw:
        return 3
    if name.startswith('__') and name.endswith('__') and len(name) > 4:
        return 2
    if name.startswith('_'):
        return 1
    return 0


def describe_def(d, name):
    if isinstance(d, StarHit):
        return describe(infer_def(d), name)
    if d.kind == 'param':
        return 'param', 'param %s' % name
    if d.kind == 'def':
        return 'function', 'def %s(...)' % name
    if d.kind == 'class':
        return 'class', 'class %s' % name
    v = infer_def(d)
    if d.kind == 'import' and v is None:
        return 'module', 'module %s' % name
    return describe(v, name)


_RX_IMPORT = None


def _import_rx():
    global _RX_IMPORT
    if _RX_IMPORT is None:
        import re
        _RX_IMPORT = (
            re.compile(r'^\s*import\s+(.*)$', re.S),
            re.compile(r'^\s*from\s*((?:\.\s*)*)([\w.]*)$', re.S),
            re.compile(r'^\s*from\s*((?:\.\s*)*)([\w.]*)\s+(\w*)$', re.S),
            re.compile(r'^\s*from\s*((?:\.\s*)*)([\w.]*)\s+import\b\s*\(?(.*)$', re.S),
        )
    return _RX_IMPORT


def _stmt_text(lines, line_no, before):
    """Text of the (simple) statement containing the cursor, up to the cursor."""
    text = before
    k = line_no - 1
    # Continuation lines: backslashes or an open parenthesis of a from-import.
    while k > 0 and line_no - k < 30:
        prev = lines[k - 1]
        if prev.rstrip().endswith('\\'):
            text = prev.rstrip()[:-1] + ' ' + text
            k -= 1
            continue
        joined = prev + '\n' + text
        if '(' in joined and joined.count('(') > joined.count(')') and \
                not text.lstrip().startswith(('from ', 'import ')):
            text = joined
            k -= 1
            continue
        break
    text = text.split(';')[-1]
    return text.replace('\\\n', ' ')


def import_context(lines, line_no, before):
    text = _stmt_text(lines, line_no, before)
    stripped = text.lstrip()
    if not (stripped.startswith('import') or stripped.startswith('from')):
        return None
    rx_imp, rx_from_mod, rx_from_kw, rx_from_names = _import_rx()
    m = rx_from_names.match(text)
    if m:
        dots = m.group(1).replace(' ', '').replace('\t', '').replace('\n', '')
        rest = m.group(3)
        if ')' in rest:
            return ('none',)
        seg = rest.split(',')[-1].strip()
        if not all(is_ident_char(c) for c in seg) or ' ' in seg:
            return ('none',)
        if not m.group(2) and not dots:
            return ('none',)
        return ('names', len(dots), m.group(2))
    m = rx_from_mod.match(text)
    if m:
        dots = m.group(1).replace(' ', '').replace('\t', '')
        return ('modules', len(dots), m.group(2))
    m = rx_from_kw.match(text)
    if m:
        return ('keyword_import',)
    m = rx_imp.match(text)
    if m:
        seg = m.group(1).split(',')[-1].strip()
        if not seg or all(is_ident_char(c) or c == '.' for c in seg.replace(' ', '')):
            if ' ' in seg.strip() and not seg.replace(' ', '').count('.'):
                return ('none',)
            return ('modules', 0, seg.replace(' ', ''))
        return ('none',)
    if stripped in ('import', 'from'):
        return None
    return None


def _module_entry_list(mi, level, dotted):
    """List of (name, kind) of modules visible for `[.*]dotted.<prefix>`."""
    parts = dotted.split('.')
    base_parts = parts[:-1]
    names = set()
    if level:
        base = mi.dir
        for _ in range(level - 1):
            base = os.path.dirname(base)
        if not _within_root(base):
            return []
        if base_parts:
            mv = mi._local_module(base, '.'.join(base_parts), base_parts[-1])
            if mv is None or mv.local is None or not mv.local.is_package:
                return []
            names |= list_modules_in(mv.local.dir)
        else:
            names |= list_modules_in(base)
        return sorted(names)
    if not base_parts:
        names |= list_modules_in(_root())
        names |= set(n for n in stdlib_names() if _is_ident(n))
        return sorted(names)
    mv = mi.resolve_import('.'.join(base_parts), 0)
    if mv is None:
        return []
    if mv.local is not None:
        if mv.local.is_package:
            names |= list_modules_in(mv.local.dir)
    elif mv.real is not None:
        for n in safe_dir(mv.real):
            ok, o = safe_getattr(mv.real, n)
            if ok and inspect.ismodule(o) and (getattr(o, '__name__', '').startswith(mv.real.__name__ + '.')
                                               or sys.modules.get(mv.real.__name__ + '.' + n) is o):
                names.add(n)
        mpath = getattr(mv.real, '__path__', None)
        if mpath:
            import pkgutil
            try:
                for m in pkgutil.iter_modules(list(mpath)):
                    if _is_ident(m.name):
                        names.add(m.name)
            except Exception:
                pass
    return sorted(names)


def import_completions(path, imp, prefix, fuzzy):
    kind = imp[0]
    results = []
    if kind == 'none':
        return []
    if kind == 'keyword_import':
        if matches('import', prefix, fuzzy):
            results.append(('import', 'keyword', 'import', True))
    else:
        mi = ModuleInfo(os.path.abspath(path), '__main__')
        if kind == 'modules':
            for n in _module_entry_list(mi, imp[1], imp[2]):
                if matches(n, prefix, fuzzy):
                    results.append((n, 'module', 'module %s' % n, False))
            if not imp[2] and imp[1] and matches('import', prefix, fuzzy):
                results.append(('import', 'keyword', 'import', True))
        else:
            level, dotted = imp[1], imp[2]
            mv = mi.resolve_import(dotted, level)
            seen = set()
            if mv is not None:
                if mv.local is not None:
                    loc = mv.local
                    al = loc.dunder_all()
                    names = list(loc.public_names())
                    if al is None:
                        names += [n for n in loc.submodule_names() if not n.startswith('_')]
                    for n in names:
                        if n in seen or not matches(n, prefix, fuzzy):
                            continue
                        seen.add(n)
                        ok, vals = loc.attr(n)
                        val = same_val(vals) if ok else None
                        if val is None and ok and vals:
                            val = vals[0]
                        typ, desc = describe(val, n)
                        results.append((n, typ, desc, False))
                elif mv.real is not None:
                    names = list(star_names(mv))
                    al = getattr(mv.real, '__all__', None)
                    if not isinstance(al, (list, tuple)):
                        mpath = getattr(mv.real, '__path__', None)
                        if mpath:
                            import pkgutil
                            try:
                                names += [m.name for m in pkgutil.iter_modules(list(mpath))
                                          if not m.name.startswith('_')]
                            except Exception:
                                pass
                    for n in names:
                        if n in seen or not matches(n, prefix, fuzzy):
                            continue
                        seen.add(n)
                        ok, o = safe_getattr(mv.real, n)
                        if ok:
                            typ, desc = describe(wrap_real(o, n), n)
                        else:
                            typ, desc = 'module', 'module %s' % n
                        results.append((n, typ, desc, False))
    results.sort(key=lambda r: (group_of(r[0], r[3]), r[0].lower(), r[0]))
    return [{'name': n, 'complete': n[len(prefix):], 'type': t, 'description': d}
            for n, t, d, _ in results]


def complete(path, line_no, col, fuzzy):
    reset_state()
    if not os.path.exists(path):
        raise SithError('file does not exist: %s' % path)
    if not os.path.isfile(path):
        raise SithError('not a regular file: %s' % path)
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        raise SithError('cannot read file: %s' % e)
    try:
        source = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise SithError('file is not valid UTF-8')
    if source.startswith('﻿'):
        source = source[1:]
    source = source.replace('\x00', '')
    lines = [l[:-1] if l.endswith('\r') else l for l in source.split('\n')]
    if line_no < 1 or line_no > len(lines):
        raise SithError('line out of range')
    cur = lines[line_no - 1]
    if col < 0 or col > len(cur):
        raise SithError('column out of range')

    before = cur[:col]
    i = col
    while i > 0 and is_ident_char(before[i - 1]):
        i -= 1
    prefix = before[i:col]
    if prefix and prefix[0].isdigit():
        return []
    if in_string_or_comment(lines, line_no, col, '\n'.join(lines)):
        return []
    imp = import_context(lines, line_no, before)
    if imp is not None:
        return import_completions(path, imp, prefix, fuzzy)
    is_attr = i > 0 and before[i - 1] == '.'
    receiver = None
    if is_attr:
        if i > 1 and before[i - 2] == '.':
            return []
        receiver = find_receiver(before[:i - 1])
        if receiver is None:
            return []

    # Build a parseable version of the file with a placeholder at the cursor.
    j = col
    while j < len(cur) and is_ident_char(cur[j]):
        j += 1
    rest = cur[j:]
    base = before[:i] + PLACEHOLDER
    closers = bracket_closers(base)
    ind = cur[:leading_ws(cur)]
    candidates = [cur, base + rest, base + closers, base + closers + ':', base + closers + ': pass',
                  ind + 'pass', '']
    best = None
    for cand in candidates:
        trial = list(lines)
        trial[line_no - 1] = cand
        tree, mods, _ = fix_and_parse(trial)
        if best is None or mods < best[1]:
            best = (tree, mods, cand)
        if mods == 0:
            break
    tree = best[0]

    module = ModuleInfo(os.path.abspath(path), '__main__')
    module.set_tree(tree)
    _local_modules[module.path] = module
    node_scope = module.node_scope
    ppath = find_placeholder(tree, line_no)
    if ppath is not None:
        scope = scope_from_path(ppath, node_scope, module.scope)
    else:
        scope = scope_by_indent(tree, node_scope, module.scope, lines, line_no, col)

    results = []
    if is_attr:
        vals = infer_vals(receiver, scope, (line_no, col))
        pairs = []
        for v in vals:
            pairs.extend(attributes(v))
        seen = set()
        for name, val in pairs:
            if name in seen or name == PLACEHOLDER:
                continue
            seen.add(name)
            if not matches(name, prefix, fuzzy):
                continue
            typ, desc = describe(val, name)
            results.append((name, typ, desc, False))
    else:
        seen = set()
        s = scope
        first = True
        while s is not None:
            if first or s.kind != 'class':
                for name, d in s.visible(line_no).items():
                    if name in seen:
                        continue
                    seen.add(name)
                    if not matches(name, prefix, fuzzy):
                        continue
                    typ, desc = describe_def(d, name)
                    results.append((name, typ, desc, False))
            first = False
            s = s.parent
        for name in dir(builtins):
            if name in seen:
                continue
            seen.add(name)
            if not matches(name, prefix, fuzzy):
                continue
            ok, obj = safe_getattr(builtins, name)
            typ, desc = describe(wrap_real(obj, name) if ok else None, name)
            results.append((name, typ, desc, False))
        for kw in keyword.kwlist:
            if matches(kw, prefix, fuzzy):
                results.append((kw, 'keyword', kw, True))

    results.sort(key=lambda r: (group_of(r[0], r[3]), r[0].lower(), r[0]))
    out = []
    for name, typ, desc, _ in results:
        out.append({'name': name, 'complete': name[len(prefix):], 'type': typ,
                    'description': desc})
    return out


# ---------------------------------------------------------------------------
# Navigation: infer / goto
# ---------------------------------------------------------------------------

class NotAName(Exception):
    pass


def _line_bytes(mi, lineno):
    if 1 <= lineno <= len(mi.lines):
        return mi.lines[lineno - 1].encode('utf-8')
    return b''


def _char_col(mi, lineno, bcol):
    b = _line_bytes(mi, lineno)
    return len(b[:bcol].decode('utf-8', errors='ignore'))


def _src_between(mi, a, b):
    """Source text between byte positions a and b (line, col)."""
    (l1, c1), (l2, c2) = a, b
    if l1 < 1:
        return ''
    if l1 == l2:
        return _line_bytes(mi, l1)[c1:c2].decode('utf-8', errors='ignore')
    parts = [_line_bytes(mi, l1)[c1:].decode('utf-8', errors='ignore')]
    for ln in range(l1 + 1, l2):
        parts.append(_line_bytes(mi, ln).decode('utf-8', errors='ignore'))
    parts.append(_line_bytes(mi, l2)[:c2].decode('utf-8', errors='ignore'))
    return '\n'.join(parts)


def _norm(text):
    out = []
    for ln in text.split('\n'):
        st = ln.strip()
        if st.startswith('#'):
            continue
        out.append(ln)
    return ' '.join(' '.join(out).split())


def node_text(mi, node):
    if node is None or not hasattr(node, 'lineno'):
        return ''
    t = _src_between(mi, _start(node), _end(node))
    if not t.strip():
        try:
            return ast.unparse(node)
        except Exception:
            return ''
    return _norm(t)


def header_text(mi, n, body):
    if body:
        t = _src_between(mi, _start(n), _start(body[0]))
    else:
        t = _src_between(mi, _start(n), _end(n))
    t = _norm(t)
    if t.endswith(':'):
        t = t[:-1].rstrip()
    return t


def _search(mi, start_line, start_bcol, pattern, group=1, max_lines=50):
    import re
    rx = re.compile(pattern)
    for ln in range(start_line, min(len(mi.lines), start_line + max_lines) + 1):
        b = _line_bytes(mi, ln)
        off = start_bcol if ln == start_line else 0
        m = rx.search(b, off)
        if m:
            return (ln, m.start(group))
    return None


def _ident(name):
    import re
    return re.escape(name.encode('utf-8'))


def defnode_name_pos(mi, n):
    """Byte position of the name in a def/class statement."""
    kw = rb'class' if isinstance(n, ast.ClassDef) else rb'def'
    p = _search(mi, n.lineno, n.col_offset, kw + rb'\s+(' + _ident(n.name) + rb')\b', 1, 5)
    return p or (n.lineno, n.col_offset)


def module_name_of(mi):
    if getattr(mi, 'qual_name', None):
        return mi.qual_name
    path = os.path.abspath(mi.path)
    root = _root()
    rel = os.path.relpath(path, root)
    if rel.startswith('..'):
        if mi.name and mi.name != '__main__':
            return mi.name
        rel = os.path.basename(path)
    if rel.endswith('.py'):
        rel = rel[:-3]
    elif rel.endswith('.pyi'):
        rel = rel[:-4]
    parts = [p for p in rel.replace(os.sep, '/').split('/') if p]
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts) or os.path.basename(os.path.dirname(path))


def _root():
    return os.path.abspath(_PROJECT_ROOT) if _PROJECT_ROOT else os.getcwd()


def rel_module_path(path):
    if not path:
        return ''
    path = os.path.abspath(path)
    try:
        rel = os.path.relpath(path, _root())
    except ValueError:
        rel = '..'
    if rel == '..' or rel.startswith('..' + os.sep):
        return path.replace(os.sep, '/')
    return rel.replace(os.sep, '/')


def scope_chain(s):
    names = []
    while s is not None and s.kind != 'module':
        if s.kind in ('function', 'class'):
            names.append(s.node.name)
        s = s.parent
    return list(reversed(names))


def full_name_for(s, name):
    mi = s.module
    parts = [module_name_of(mi)] + scope_chain(s) + [name]
    return '.'.join(p for p in parts if p)


def _docstring(node):
    try:
        return ast.get_docstring(node, clean=True) or ''
    except Exception:
        return ''


def func_description(mi, node):
    if isinstance(node, ast.Lambda):
        return 'lambda'
    try:
        params = ast.unparse(node.args)
    except Exception:
        params = '...'
    return 'def %s(%s)' % (node.name, params)


def mk(name, typ, full_name, module_path, line, column, description, docstring):
    return {'name': name, 'type': typ, 'full_name': full_name, 'module_path': module_path,
            'line': line, 'column': column, 'description': description,
            'docstring': docstring or ''}


def _is_builtin_obj(obj):
    try:
        mod = getattr(obj, '__module__', None)
        if inspect.isclass(obj):
            return mod == 'builtins'
        if is_routine(obj):
            oc = getattr(obj, '__objclass__', None)
            if oc is not None:
                return getattr(oc, '__module__', None) == 'builtins'
            sf = getattr(obj, '__self__', None)
            if sf is not None and not inspect.ismodule(sf):
                return getattr(type(sf), '__module__', None) == 'builtins' or \
                    (inspect.isclass(sf) and sf.__module__ == 'builtins')
            return mod == 'builtins' or sf is builtins
    except Exception:
        pass
    return False


def _real_full_name(obj):
    if inspect.ismodule(obj):
        return obj.__name__
    qual = getattr(obj, '__qualname__', None) or getattr(obj, '__name__', None) or type(obj).__name__
    mod = getattr(obj, '__module__', None)
    if mod is None:
        oc = getattr(obj, '__objclass__', None)
        if oc is not None:
            mod = getattr(oc, '__module__', None)
        else:
            sf = getattr(obj, '__self__', None)
            if sf is not None:
                mod = getattr(type(sf), '__module__', None)
    if not isinstance(mod, str):
        mod = 'builtins'
    return '%s.%s' % (mod, qual)


def _real_location(obj):
    try:
        f = inspect.getsourcefile(obj) or inspect.getfile(obj)
    except Exception:
        f = None
    if not f or f.startswith('<'):
        modname = obj.__name__ if inspect.ismodule(obj) else getattr(obj, '__module__', None)
        mod = sys.modules.get(modname) if isinstance(modname, str) else None
        f = getattr(mod, '__file__', None) if mod is not None else None
        if not f or f.startswith('<'):
            return '', 0, 0
        if not inspect.ismodule(obj):
            return rel_module_path(f), 0, 0
    if inspect.ismodule(obj):
        return rel_module_path(f), 1, 0
    try:
        lines, ln = inspect.getsourcelines(obj)
        col = 0
        if lines:
            first = lines[0]
            name = getattr(obj, '__name__', '')
            import re
            m = re.search(r'(?:def|class)\s+(%s)\b' % re.escape(name), first)
            col = m.start(1) if m else leading_ws(first)
        return rel_module_path(f), max(ln, 1), col
    except Exception:
        return rel_module_path(f), 0, 0


def _real_signature(obj, name):
    try:
        sig = str(inspect.signature(obj))
        return 'def %s%s' % (name, sig.split(' ->')[0])
    except Exception:
        return 'def %s(...)' % name


def real_obj_out(obj, name=None):
    name = name or getattr(obj, '__name__', None) or type(obj).__name__
    if inspect.ismodule(obj):
        path, line, col = _real_location(obj)
        if not path:
            line = col = 0
        return mk(obj.__name__.split('.')[-1], 'module', obj.__name__, path, line, col,
                  'module %s' % obj.__name__.split('.')[-1], '')
    if inspect.isclass(obj):
        nm = obj.__name__
        if _is_builtin_obj(obj):
            return mk(nm, 'class', 'builtins.%s' % nm, '', 0, 0, 'class %s' % nm, '')
        path, line, col = _real_location(obj)
        return mk(nm, 'class', _real_full_name(obj), path, line, col, 'class %s' % nm,
                  inspect.getdoc(obj) or '')
    if is_routine(obj):
        nm = getattr(obj, '__name__', name)
        if _is_builtin_obj(obj):
            return mk(nm, 'function', 'builtins.' + (getattr(obj, '__qualname__', None) or nm), '', 0, 0,
                      _real_signature(obj, nm), '')
        path, line, col = _real_location(obj)
        return mk(nm, 'function', _real_full_name(obj), path, line, col, _real_signature(obj, nm),
                  inspect.getdoc(obj) or '')
    return real_type_out(type(obj))


def real_type_out(typ):
    nm = typ.__name__
    if typ.__module__ == 'builtins' or typ is type(None):
        return mk(nm, 'instance', 'builtins.%s' % nm, '', 0, 0, 'instance of %s' % nm, '')
    path, line, col = _real_location(typ)
    return mk(nm, 'instance', _real_full_name(typ), path, line, col, 'instance of %s' % nm, '')


def class_out(cv, typ):
    n = cv.node
    s = cv.scope
    mi = s.module
    m = source_class_node(mi, s, n)
    if m is not None:
        n, s = m
        mi = s.module
    line, bcol = defnode_name_pos(mi, n)
    desc = 'class %s' % n.name if typ == 'class' else 'instance of %s' % n.name
    return mk(n.name, typ, full_name_for(s, n.name), rel_module_path(mi.path), line,
              _char_col(mi, line, bcol), desc, _docstring(n))


def func_out(fv):
    n = fv.node
    s = fv.scope
    mi = s.module
    if isinstance(n, FUNC_TYPES):
        m = source_class_node(mi, s, n)
        if m is not None:
            n, s = m
            mi = s.module
    if isinstance(n, ast.Lambda):
        return mk('<lambda>', 'function', full_name_for(s, '<lambda>'), rel_module_path(mi.path),
                  n.lineno, _char_col(mi, n.lineno, n.col_offset), 'lambda', '')
    line, bcol = defnode_name_pos(mi, n)
    return mk(n.name, 'function', full_name_for(s, n.name), rel_module_path(mi.path), line,
              _char_col(mi, line, bcol), func_description(mi, n), _docstring(n))


def module_out(mv):
    if mv.local is not None:
        mi = mv.local
        full = module_name_of(mi)
        nm = full.split('.')[-1]
        doc = _docstring(mi.tree) if mi.tree is not None else ''
        return mk(nm, 'module', full, rel_module_path(mi.path), 1, 0, 'module %s' % nm, doc)
    if mv.real is not None:
        return real_obj_out(mv.real)
    return None


def val_out(v):
    if v is None:
        return None
    if isinstance(v, ModVal):
        return module_out(v)
    if isinstance(v, ClassVal):
        return class_out(v, 'class')
    if isinstance(v, InstVal):
        return class_out(v.cls, 'instance')
    if isinstance(v, FuncVal):
        return func_out(v)
    if isinstance(v, RealInst):
        return real_type_out(v.typ)
    if isinstance(v, RealVal):
        return real_obj_out(v.obj)
    return None


def _value_type(vals, default):
    for v in vals:
        if isinstance(v, ModVal):
            return 'module'
        if isinstance(v, ClassVal):
            return 'class'
        if isinstance(v, FuncVal):
            return 'function'
        if isinstance(v, (InstVal, RealInst)):
            return 'instance'
        if isinstance(v, RealVal):
            return describe(v, '')[0]
    return default


def alias_name_pos(mi, stmt, alias, which):
    """Byte position of the bound name of an import alias."""
    start = (getattr(alias, 'lineno', None) or stmt.lineno, getattr(alias, 'col_offset', None))
    if start[1] is None:
        p = _search(mi, stmt.lineno, stmt.col_offset,
                    rb'(?<![\w.])(' + _ident(alias.name.split('.')[0]) + rb')\b', 1)
        start = p or (stmt.lineno, stmt.col_offset)
    if which == 'as' and alias.asname:
        p = _search(mi, start[0], start[1], rb'\bas\s+(' + _ident(alias.asname) + rb')\b', 1, 5)
        if p:
            return p
    return start


_FOLLOW = [False]


def stub_attr_defs(local, name):
    """Definitions that only exist in the stub of module `local`."""
    st = local.stub() if not local.is_stub else None
    if st is None or _stub_private_import(st, name):
        return []
    return st.attr_defs(name)


def _alias_target(d):
    """For an import definition: (dotted module, level, imported name or None)."""
    if d.kind == 'import':
        name, asname = d.extra
        return (name if asname else name.split('.')[0]), 0, None
    modname, name, level = d.extra
    return modname or '', level or 0, name


def follow_def(d, seen=None):
    """Follow an import chain to the final definitions; None if unresolvable."""
    if seen is None:
        seen = set()
    if isinstance(d, StarHit):
        key = ('star', id(d.star), d.name)
        if key in seen:
            return None
        seen.add(key)
        mv = d.star.module_value()
        if mv is None or mv.local is None:
            return None
        return follow_module_attr(mv.local, d.name, seen)
    if isinstance(d, AttrDef) or d.kind not in ('import', 'importfrom'):
        return def_out(d, follow=False)
    key = (id(d.node), d.name)
    if key in seen:
        return None
    seen.add(key)
    mi = d.scope.module
    dotted, level, name = _alias_target(d)
    if name is None:
        mv = mi.resolve_import(dotted, level)
        if mv is None or mv.local is None:
            return None
        o = module_out(mv)
        return [o] if o else None
    mv = mi.resolve_import(dotted, level)
    if mv is not None and mv.local is not None:
        res = follow_module_attr(mv.local, name, seen)
        if res:
            return res
    sub = (dotted + '.' + name) if dotted else name
    mv2 = mi.resolve_import(sub, level)
    if mv2 is not None and mv2.local is not None:
        o = module_out(mv2)
        return [o] if o else None
    return None


def follow_module_attr(local, name, seen):
    ds = local.attr_defs(name) or stub_attr_defs(local, name)
    if ds:
        out = []
        for x in ds:
            r = follow_def(x, seen)
            if r:
                out.extend(r)
        return out or None
    if local.is_package:
        mv = local.submodule(name)
        if mv is not None and mv.local is not None:
            o = module_out(mv)
            return [o] if o else None
    return None


def def_out(d, follow=None):
    """Output dict(s) for a definition site (goto)."""
    if follow is None:
        follow = _FOLLOW[0]
    if follow and (isinstance(d, StarHit) or (isinstance(d, Def) and d.kind in ('import', 'importfrom'))):
        r = follow_def(d)
        if r:
            return r
    if isinstance(d, StarHit):
        mv = d.star.module_value()
        if mv is None:
            return []
        return attr_def_outs(mv, d.name)
    if isinstance(d, AttrDef):
        return [attrdef_out(d)]
    s = d.scope
    mi = s.module
    k = d.kind
    name = d.name
    full = full_name_for(s, name)
    mpath = rel_module_path(mi.path)
    if k == 'def':
        return [func_out(FuncVal(d.node, s))]
    if k == 'class':
        return [class_out(ClassVal(d.node, s), 'class')]
    if k == 'param':
        p = d.node
        return [mk(name, 'param', full, mpath, p.lineno, _char_col(mi, p.lineno, p.col_offset),
                   'param %s' % name, '')]
    if k in ('import', 'importfrom'):
        stmt = d.node
        alias = None
        for a in stmt.names:
            bound = a.asname or (a.name.split('.')[0] if k == 'import' else a.name)
            if bound == name:
                alias = a
                break
        if alias is not None:
            line, bcol = alias_name_pos(mi, stmt, alias, 'as')
        else:
            line, bcol = stmt.lineno, stmt.col_offset
        vals = infer_def_vals(d)
        return [mk(name, _value_type(vals, 'module'), full, mpath, line, _char_col(mi, line, bcol),
                   node_text(mi, stmt), '')]
    # name-target based definitions
    node = d.node
    if isinstance(node, ast.ExceptHandler):
        p = _search(mi, node.lineno, node.col_offset, rb'\bas\s+(' + _ident(name) + rb')\b', 1, 10)
        line, bcol = p or (node.lineno, node.col_offset)
        desc = header_text(mi, node, node.body)
    elif hasattr(ast, 'MatchAs') and isinstance(node, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
        p = _search(mi, node.lineno, node.col_offset, rb'(?<![\w.])(' + _ident(name) + rb')\b', 1, 10)
        line, bcol = p or (node.lineno, node.col_offset)
        desc = node_text(mi, node)
    else:
        line, bcol = getattr(node, 'lineno', d.pos[0]), getattr(node, 'col_offset', d.pos[1])
        desc = ''
        if k == 'assign':
            value, path, stmt = d.extra
            desc = node_text(mi, value)
        elif k == 'annassign':
            value, ann, stmt = d.extra
            desc = node_text(mi, value) if value is not None else node_text(mi, stmt)
        elif k == 'walrus':
            desc = node_text(mi, d.extra)
        elif k == 'augassign':
            desc = node_text(mi, d.extra)
        elif k in ('for', 'with'):
            value, path, stmt = d.extra
            desc = header_text(mi, stmt, stmt.body)
        elif k == 'comp':
            iter_node, path = d.extra
            desc = 'for %s in %s' % (name, node_text(mi, iter_node))
    return [mk(name, 'statement', full, mpath, line, _char_col(mi, line, bcol), desc, '')]


def attrdef_out(a):
    mi = a.fs.module
    t = a.node
    line = getattr(t, 'end_lineno', None) or t.lineno
    bcol = (getattr(t, 'end_col_offset', None) or 0) - len(a.name.encode('utf-8'))
    if a.kind in ('assign', 'ann') and a.value is not None and not isinstance(a.value, tuple):
        desc = node_text(mi, a.value)
    elif a.kind in ('for', 'with'):
        desc = header_text(mi, a.stmt, a.stmt.body)
    else:
        desc = node_text(mi, a.stmt)
    cls_scope = class_scope_of(a.cls)
    full = full_name_for(cls_scope, a.name) if cls_scope is not None else a.name
    return mk(a.name, 'statement', full, rel_module_path(mi.path), line,
              _char_col(mi, line, bcol), desc, '')


def attr_def_outs(v, name):
    """Definition sites of attribute `name` on value v."""
    if isinstance(v, ModVal):
        if v.local is not None:
            ds = v.local.attr_defs(name) or stub_attr_defs(v.local, name)
            if ds:
                out = []
                for d in ds:
                    out.extend(def_out(d))
                return out
            if v.local.is_package:
                mv = v.local._local_module(v.local.dir, name, name)
                if mv is not None:
                    o = module_out(mv)
                    return [o] if o else []
            return []
        ok, vals = attr_vals(v, name)
        return [o for o in (val_out(x) for x in vals) if o]
    if isinstance(v, (ClassVal, InstVal)):
        cls = v if isinstance(v, ClassVal) else v.cls
        m = class_attr_map(cls, isinstance(v, InstVal))
        out = []
        for kind, x, c in nav_ents(m.get(name, ())):
            out.extend(def_out(x))
        return out
    ok, vals = attr_vals(v, name)
    return [o for o in (val_out(x) for x in vals) if o]


def finalize(outs):
    seen = set()
    res = []
    for o in outs:
        if not o:
            continue
        k = (o['module_path'], o['line'], o['column'], o['name'], o['type'], o['full_name'])
        if k in seen:
            continue
        seen.add(k)
        res.append(o)
    res.sort(key=lambda o: (o['module_path'], o['line'], o['column']))
    return res


# --- occurrences --------------------------------------------------------------

class Occ(object):
    def __init__(self, line, bcol, name, kind, node, extra=None):
        self.line = line
        self.bcol = bcol
        self.name = name
        self.kind = kind
        self.node = node
        self.extra = extra


def collect_occurrences(mi, line_no):
    """Occurrences of identifiers on line `line_no`, plus a parent map."""
    tree = mi.tree
    parents = {}
    occs = []
    for node in ast.walk(tree):
        for c in ast.iter_child_nodes(node):
            parents[c] = node
    for node in ast.walk(tree):
        ln = getattr(node, 'lineno', None)
        eln = getattr(node, 'end_lineno', None) or ln
        if line_no is not None and (ln is None or not (ln <= line_no <= eln)):
            if not isinstance(node, (ast.Module,)):
                continue
        if isinstance(node, ast.Name):
            occs.append(Occ(node.lineno, node.col_offset, node.id, 'name', node))
        elif isinstance(node, ast.Attribute):
            el = getattr(node, 'end_lineno', None)
            ec = getattr(node, 'end_col_offset', None)
            if el is not None and ec is not None:
                bc = ec - len(node.attr.encode('utf-8'))
                if _line_bytes(mi, el)[bc:ec] == node.attr.encode('utf-8'):
                    occs.append(Occ(el, bc, node.attr, 'attr', node))
        elif isinstance(node, FUNC_TYPES + (ast.ClassDef,)):
            l, c = defnode_name_pos(mi, node)
            occs.append(Occ(l, c, node.name, 'defname', node))
        elif isinstance(node, ast.arg):
            occs.append(Occ(node.lineno, node.col_offset, node.arg, 'arg', node))
        elif isinstance(node, ast.keyword):
            if node.arg is not None and hasattr(node, 'lineno'):
                occs.append(Occ(node.lineno, node.col_offset, node.arg, 'keyword', node))
        elif isinstance(node, ast.Import):
            for a in node.names:
                start = alias_name_pos(mi, node, a, 'name')
                off = start[1]
                parts = a.name.split('.')
                for i, part in enumerate(parts):
                    occs.append(Occ(start[0], off, part, 'importmod', node, ('.'.join(parts[:i + 1]), 0, a)))
                    off += len(part.encode('utf-8')) + 1
                if a.asname:
                    l, c = alias_name_pos(mi, node, a, 'as')
                    occs.append(Occ(l, c, a.asname, 'importas', node, (a.name, 0, a)))
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if node.module:
                p = _search(mi, node.lineno, node.col_offset,
                            rb'from\s*\.*\s*(' + _ident(node.module.split('.')[0]) + rb')', 1, 5)
                if p:
                    off = p[1]
                    parts = node.module.split('.')
                    for i, part in enumerate(parts):
                        occs.append(Occ(p[0], off, part, 'importmod', node,
                                        ('.'.join(parts[:i + 1]), level, None)))
                        off += len(part.encode('utf-8')) + 1
            for a in node.names:
                if a.name == '*':
                    continue
                start = alias_name_pos(mi, node, a, 'name')
                occs.append(Occ(start[0], start[1], a.name, 'importname', node, a))
                if a.asname:
                    l, c = alias_name_pos(mi, node, a, 'as')
                    occs.append(Occ(l, c, a.asname, 'importname', node, a))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for nm in node.names:
                p = _search(mi, node.lineno, node.col_offset + 6, rb'\b(' + _ident(nm) + rb')\b', 1, 5)
                if p:
                    occs.append(Occ(p[0], p[1], nm, 'global' if isinstance(node, ast.Global) else 'nonlocal',
                                    node))
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                p = _search(mi, node.lineno, node.col_offset, rb'\bas\s+(' + _ident(node.name) + rb')\b', 1, 10)
                if p:
                    occs.append(Occ(p[0], p[1], node.name, 'binder', node))
        elif hasattr(ast, 'MatchAs') and isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name:
                if isinstance(node, ast.MatchAs) and node.pattern is None:
                    occs.append(Occ(node.lineno, node.col_offset, node.name, 'binder', node))
                else:
                    rx = (rb'\bas\s+(' if isinstance(node, ast.MatchAs) else rb'\*\s*(') + _ident(node.name) + rb')\b'
                    p = _search(mi, node.lineno, node.col_offset, rx, 1, 10)
                    if p:
                        occs.append(Occ(p[0], p[1], node.name, 'binder', node))
        elif isinstance(node, ast.Constant):
            if node.value is None or isinstance(node.value, bool):
                txt = repr(node.value)
                if _line_bytes(mi, node.lineno)[node.col_offset:node.col_offset + len(txt)] == txt.encode():
                    occs.append(Occ(node.lineno, node.col_offset, txt, 'const', node))
    return occs, parents


def find_occurrence(mi, line_no, bcol):
    occs, parents = collect_occurrences(mi, line_no)
    strict = []
    edge = []
    for o in occs:
        if o.line != line_no:
            continue
        ln = len(o.name.encode('utf-8'))
        if o.bcol <= bcol < o.bcol + ln:
            strict.append(o)
        elif bcol == o.bcol + ln:
            edge.append(o)
    pick = strict or edge
    if not pick:
        return None, parents
    order = {'importas': 0, 'importname': 0, 'importmod': 1, 'attr': 1, 'defname': 1, 'arg': 1,
             'keyword': 1, 'binder': 1, 'global': 1, 'nonlocal': 1, 'name': 2, 'const': 3}
    pick.sort(key=lambda o: order.get(o.kind, 5))
    return pick[0], parents


def path_to(node, parents):
    path = [node]
    while path[-1] in parents:
        path.append(parents[path[-1]])
    return list(reversed(path))


def scope_of_node(mi, node, parents):
    return scope_from_path(path_to(node, parents), mi.node_scope, mi.scope)


def builtin_out(name):
    ok, obj = safe_getattr(builtins, name)
    if not ok:
        return None
    if inspect.isclass(obj) or is_routine(obj):
        return real_obj_out(obj, name)
    return mk(name, 'instance', 'builtins.%s' % name, '', 0, 0,
              'instance of %s' % type(obj).__name__, '')


def _defs_outs(ds):
    out = []
    for d in ds:
        out.extend(def_out(d))
    return out


def _vals_outs(vals):
    return [val_out(v) for v in vals]


def _enclosing_assign_vals(node, parents, s):
    """Values assigned to the store-context expression `node`."""
    path = []
    n = node
    while n in parents:
        p = parents[n]
        if isinstance(p, ast.Assign) and n in p.targets:
            return infer_path_vals(p.value, tuple(reversed(path)), s, _start(p))
        if isinstance(p, ast.AnnAssign) and n is p.target:
            v = infer_vals(p.value, s, _start(p)) if p.value is not None else []
            return v or annotation_vals(p.annotation, s, _start(p))
        if isinstance(p, ast.AugAssign) and n is p.target:
            return infer_vals(ast.BinOp(left=node, op=p.op, right=p.value), s, _start(p))
        if isinstance(p, (ast.For, ast.AsyncFor)) and n is p.target:
            return path_of_vals(element_vals(p.iter, s, _start(p)), tuple(reversed(path)))
        if isinstance(p, ast.withitem) and n is p.optional_vars:
            out = []
            for v in infer_vals(p.context_expr, s, _start(node)):
                out.extend(enter_vals(v))
            return path_of_vals(out, tuple(reversed(path)))
        if isinstance(p, (ast.Tuple, ast.List)):
            idx = p.elts.index(n)
            path.append(None if isinstance(n, ast.Starred) else idx)
        elif not isinstance(p, ast.Starred):
            return []
        n = p
    return []


def _attr_store_out(mi, node, parents, s):
    stmt = node
    while stmt in parents and not isinstance(stmt, ast.stmt):
        stmt = parents[stmt]
    line = node.end_lineno
    bcol = node.end_col_offset - len(node.attr.encode('utf-8'))
    desc = ''
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
        desc = node_text(mi, stmt.value)
    elif isinstance(stmt, ast.stmt):
        desc = header_text(mi, stmt, getattr(stmt, 'body', None)) if hasattr(stmt, 'body') \
            else node_text(mi, stmt)
    owner = s
    if isinstance(node.value, ast.Name) and s.kind == 'function' and s.parent is not None \
            and s.parent.kind == 'class':
        params = func_params(s.node)
        if params and params[0].arg == node.value.id:
            owner = mi.node_scope.get(s.parent.node, s)
            return mk(node.attr, 'statement', full_name_for(owner, node.attr), rel_module_path(mi.path),
                      line, _char_col(mi, line, bcol), desc, '')
    return mk(node.attr, 'statement', full_name_for(owner, node.attr), rel_module_path(mi.path),
              line, _char_col(mi, line, bcol), desc, '')


def _callable_params(fv_list, kwname):
    out = []
    for f in fv_list:
        fnode = None
        if isinstance(f, FuncVal) and isinstance(f.node, FUNC_TYPES + (ast.Lambda,)):
            fnode = f.node
            mod = f.scope.module if f.scope else None
        elif isinstance(f, ClassVal):
            for c in class_mro(f):
                for st in c.node.body:
                    if isinstance(st, FUNC_TYPES) and st.name == '__init__':
                        fnode = st
                        mod = c.scope.module if c.scope else None
                        break
                if fnode is not None:
                    break
            if fnode is None and is_dataclass(f):
                cs = class_scope_of(f)
                if cs is not None:
                    out.extend(d for d in cs.defs.get(kwname, ()) if d.kind == 'annassign')
                continue
        if fnode is None or mod is None:
            continue
        fs = mod.node_scope.get(fnode)
        if fs is None:
            continue
        out.extend(d for d in fs.defs.get(kwname, ()) if d.kind == 'param')
    return out


def navigate(mi, occ, parents, mode):
    """Return list of output dicts for occurrence `occ`; mode is 'infer' or 'goto'."""
    k = occ.kind
    node = occ.node
    pos = (occ.line, occ.bcol)
    if k == 'const':
        if mode == 'goto':
            return []
        return [real_type_out(type(node.value))]
    if k == 'name':
        s = scope_of_node(mi, node, parents)
        if isinstance(node.ctx, ast.Store):
            ds = [d for d in mi.defs_by_node(node) if d.name == node.id]
            if ds:
                if mode == 'goto':
                    return _defs_outs(ds[:1])
                out = []
                for d in ds[:1]:
                    out.extend(infer_def_vals(d))
                return _vals_outs(uniq(out))
            if mode == 'goto':
                return _defs_outs(lookup_defs(node.id, s, INF_POS))
            return _vals_outs(_enclosing_assign_vals(node, parents, s))
        if mode == 'goto':
            ds = lookup_defs(node.id, s, _start(node))
            if not ds:
                b = builtin_out(node.id)
                return [b] if b else []
            return _defs_outs(ds)
        return _vals_outs(infer_vals(node, s, _start(node)))
    if k == 'attr':
        s = scope_of_node(mi, node, parents)
        if isinstance(node.ctx, ast.Store):
            if mode == 'goto':
                return [_attr_store_out(mi, node, parents, s)]
            return _vals_outs(_enclosing_assign_vals(node, parents, s))
        if mode == 'goto':
            out = []
            for v in infer_vals(node.value, s, _start(node)):
                out.extend(attr_def_outs(v, node.attr))
            return out
        return _vals_outs(infer_vals(node, s, _start(node)))
    if k == 'defname':
        s = scope_of_node(mi, node, parents)
        if isinstance(node, ast.ClassDef):
            return [class_out(ClassVal(node, s), 'class')]
        return [func_out(FuncVal(node, s))]
    if k == 'arg':
        ds = [d for d in mi.defs_by_node(node) if d.kind == 'param']
        if not ds:
            return []
        if mode == 'goto':
            return _defs_outs(ds[:1])
        return _vals_outs(infer_def_vals(ds[0]))
    if k == 'binder':
        ds = [d for d in mi.defs_by_node(node) if d.name == occ.name]
        if not ds:
            return []
        if mode == 'goto':
            return _defs_outs(ds[:1])
        return _vals_outs(infer_def_vals(ds[0]))
    if k in ('importmod', 'importas'):
        dotted, level, alias = occ.extra
        mv = mi.resolve_import(dotted, level)
        if mv is None and alias is None and level == 0:
            return []
        return [module_out(mv)] if mv is not None else []
    if k == 'importname':
        stmt = node
        a = occ.extra
        level = stmt.level or 0
        mv = mi.resolve_import(stmt.module or '', level)
        ds = [d for d in mi.defs_by_node(stmt) if d.name == (a.asname or a.name)]
        if mode == 'goto':
            return _defs_outs(ds[:1])
        out = []
        for d in ds[:1]:
            out.extend(infer_def_vals(d))
        return _vals_outs(out)
    if k in ('global', 'nonlocal'):
        s = scope_of_node(mi, node, parents)
        target = mi.scope if k == 'global' else (s.parent if s is not None else None)
        ds = []
        while target is not None:
            if target.kind != 'class':
                ds = reaching_defs(target, occ.name, INF_POS)
                if ds or k == 'global':
                    break
            target = target.parent
        if mode == 'goto':
            return _defs_outs(ds)
        out = []
        for d in ds:
            out.extend(infer_def_vals(d))
        return _vals_outs(uniq(out))
    if k == 'keyword':
        call = parents.get(node)
        if not isinstance(call, ast.Call):
            return []
        s = scope_of_node(mi, call, parents)
        if mode == 'goto':
            fvals = infer_vals(call.func, s, _start(call))
            return _defs_outs(_callable_params(fvals, node.arg))
        return _vals_outs(infer_vals(node.value, s, _start(call)))
    return []


def read_source(path):
    if not os.path.exists(path):
        raise SithError('file does not exist: %s' % path)
    if not os.path.isfile(path):
        raise SithError('not a regular file: %s' % path)
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        raise SithError('cannot read file: %s' % e)
    try:
        source = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise SithError('file is not valid UTF-8')
    if source.startswith('﻿'):
        source = source[1:]
    source = source.replace('\x00', '')
    return source


def reset_state():
    _cache.clear()
    _attr_map_cache.clear()
    _self_attr_cache.clear()
    _infer_stack.clear()
    _ret_active.clear()
    _cuts[0] = 0
    _deadline[0] = time.time() + 8


def navigate_file(path, line_no, col, mode):
    reset_state()
    source = read_source(path)
    lines = [l[:-1] if l.endswith('\r') else l for l in source.split('\n')]
    if line_no < 1 or line_no > len(lines):
        raise SithError('line out of range')
    cur = lines[line_no - 1]
    if col < 0 or col > len(cur):
        raise SithError('column out of range')
    tree, mods, fixed = fix_and_parse(lines)
    if mods and fixed[line_no - 1] != cur:
        # The cursor line itself is broken: keep the text up to the end of the
        # identifier under the cursor and close any open brackets.
        j = col
        while j < len(cur) and is_ident_char(cur[j]):
            j += 1
        head = cur[:j]
        for cand in (head + bracket_closers(head), head + bracket_closers(head) + ': pass'):
            trial = list(lines)
            trial[line_no - 1] = cand
            t2, m2, f2 = fix_and_parse(trial)
            if f2[line_no - 1] == cand and m2 <= mods:
                tree = t2
                break
    apath = os.path.abspath(path)
    mi = ModuleInfo(apath, os.path.splitext(os.path.basename(apath))[0])
    mi.lines = lines
    mi.set_tree(tree)
    _local_modules[apath] = mi
    bcol = len(cur[:col].encode('utf-8'))
    occ, parents = find_occurrence(mi, line_no, bcol)
    if occ is None:
        raise NotAName()
    outs = navigate(mi, occ, parents, mode)
    return finalize(outs)


# ---------------------------------------------------------------------------
# Shared target loading
# ---------------------------------------------------------------------------

def load_target(path, line_no=None, col=None):
    """Parse the target file and register it as a local module.

    Returns (ModuleInfo, lines)."""
    source = read_source(path)
    lines = [l[:-1] if l.endswith('\r') else l for l in source.split('\n')]
    tree, mods, fixed = fix_and_parse(lines)
    if line_no is not None:
        if line_no < 1 or line_no > len(lines):
            raise SithError('line out of range')
        cur = lines[line_no - 1]
        if col < 0 or col > len(cur):
            raise SithError('column out of range')
        if mods and fixed[line_no - 1] != cur:
            j = col
            while j < len(cur) and is_ident_char(cur[j]):
                j += 1
            head = cur[:j]
            for cand in (head + bracket_closers(head), head + bracket_closers(head) + ': pass'):
                trial = list(lines)
                trial[line_no - 1] = cand
                t2, m2, f2 = fix_and_parse(trial)
                if f2[line_no - 1] == cand and m2 <= mods:
                    tree = t2
                    break
    apath = os.path.abspath(path)
    mi = ModuleInfo(apath, os.path.splitext(os.path.basename(apath))[0])
    mi.lines = lines
    mi.set_tree(tree)
    _local_modules[apath] = mi
    return mi, lines


_SKIP_DIRS = {'__pycache__', 'node_modules', 'site-packages'}


def project_files():
    root = _root()
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if not d.startswith('.') and d not in _SKIP_DIRS
                        and not os.path.isfile(os.path.join(dp, d, 'pyvenv.cfg')))
        for fn in sorted(fns):
            if fn.endswith('.py'):
                out.append(os.path.abspath(os.path.join(dp, fn)))
    return out


def load_project_module(path):
    path = os.path.abspath(path)
    if path in _local_modules and _local_modules[path] is not None:
        return _local_modules[path]
    rel = os.path.relpath(path, _root())
    name = rel[:-3].replace(os.sep, '.') if rel.endswith('.py') else os.path.basename(path)
    if name.endswith('.__init__'):
        name = name[:-9]
    return load_local_module(path, name)


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def blank_code(text):
    """Copy of text with string contents replaced by 'x' and comments by spaces."""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '#':
            while i < n and text[i] != '\n':
                out[i] = ' '
                i += 1
            continue
        if c in '"\'':
            q = c * 3 if text[i:i + 3] == c * 3 else c
            j = i + len(q)
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j:j + len(q)] == q:
                    break
                if len(q) == 1 and text[j] == '\n':
                    break
                j += 1
            end = min(j + len(q), n) if j < n and text[j:j + len(q)] == q else min(j, n)
            for k in range(i, end):
                if out[k] != '\n':
                    out[k] = 'x'
            i = end
            continue
        i += 1
    return ''.join(out)


def _is_call_paren(code, i):
    j = i - 1
    while j >= 0 and code[j] in ' \t\r\n\\':
        j -= 1
    if j < 0:
        return False
    c = code[j]
    if c in ')]':
        return True
    if not is_ident_char(c):
        return False
    k = j
    while k >= 0 and is_ident_char(code[k]):
        k -= 1
    word = code[k + 1:j + 1]
    if not word or word[0].isdigit():
        return False
    if keyword.iskeyword(word):
        return False
    m = k
    while m >= 0 and code[m] in ' \t':
        m -= 1
    e = m
    while m >= 0 and is_ident_char(code[m]):
        m -= 1
    prev = code[m + 1:e + 1]
    if prev in ('def', 'class'):
        return False
    return True


def _split_args(code):
    segs = []
    depth = 0
    start = 0
    for i, c in enumerate(code):
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            segs.append(code[start:i])
            start = i + 1
    segs.append(code[start:])
    return segs


def _classify_seg(seg):
    import re
    st = seg.strip()
    if st.startswith('**'):
        return ('dstar', None)
    if st.startswith('*'):
        return ('star', None)
    m = re.match(r'^([^\W\d]\w*)\s*=(?!=)', st)
    if m:
        return ('kw', m.group(1))
    return ('pos', None)


def _offset_to_pos(text, off):
    line = text.count('\n', 0, off) + 1
    ls = text.rfind('\n', 0, off) + 1
    return line, off - ls


def _find_call(mi, pline, pcol_char):
    """ast.Call whose argument list opens at (pline, pcol_char)."""
    if not (1 <= pline <= len(mi.lines)):
        return None
    pb = len(mi.lines[pline - 1][:pcol_char].encode('utf-8'))
    best = None
    for node in ast.walk(mi.tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        el, ec = getattr(f, 'end_lineno', None), getattr(f, 'end_col_offset', None)
        if el is None or (el, ec) > (pline, pb):
            continue
        between = _src_between(mi, (el, ec), (pline, pb))
        if between.replace('\\', '').strip() == '' and _line_bytes(mi, pline)[pb:pb + 1] == b'(':
            if best is None or _start(node) > _start(best):
                best = node
    return best


class SigParam(object):
    def __init__(self, text, kind, name):
        self.text = text
        self.kind = kind      # posonly, pos, var, kwonly, kw
        self.name = name


def _fmt_param(name, ann, dflt):
    t = name
    if ann:
        t += ': ' + ann
    if dflt is not None:
        t += '=' + dflt
    return t


def ast_params(mi, node, skip_first):
    a = node.args
    posonly = list(getattr(a, 'posonlyargs', []))
    args = list(a.args)
    allpos = posonly + args
    ndef = len(a.defaults)
    dmap = {}
    for i, p in enumerate(allpos):
        if i >= len(allpos) - ndef:
            dmap[id(p)] = a.defaults[i - (len(allpos) - ndef)]
    if skip_first and allpos:
        first = allpos[0]
        if posonly:
            posonly = posonly[1:]
        else:
            args = args[1:]
        allpos = allpos[1:]
        dmap.pop(id(first), None)

    def ann_of(p):
        return node_text(mi, p.annotation) if getattr(p, 'annotation', None) is not None else ''

    out = []
    for p in posonly:
        d = dmap.get(id(p))
        out.append(SigParam(_fmt_param(p.arg, ann_of(p), node_text(mi, d) if d is not None else None),
                            'posonly', p.arg))
    for p in args:
        d = dmap.get(id(p))
        out.append(SigParam(_fmt_param(p.arg, ann_of(p), node_text(mi, d) if d is not None else None),
                            'pos', p.arg))
    if a.vararg:
        out.append(SigParam(_fmt_param('*' + a.vararg.arg, ann_of(a.vararg), None), 'var', a.vararg.arg))
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append(SigParam(_fmt_param(p.arg, ann_of(p), node_text(mi, d) if d is not None else None),
                            'kwonly', p.arg))
    if a.kwarg:
        out.append(SigParam(_fmt_param('**' + a.kwarg.arg, ann_of(a.kwarg), None), 'kw', a.kwarg.arg))
    return out


def _sig_description(name, params, ret):
    parts = []
    has_var = any(p.kind == 'var' for p in params)
    for i, p in enumerate(params):
        if p.kind == 'kwonly' and not has_var and (i == 0 or params[i - 1].kind != 'kwonly'):
            parts.append('*')
        parts.append(p.text)
        if p.kind == 'posonly' and (i + 1 == len(params) or params[i + 1].kind != 'posonly'):
            parts.append('/')
    d = 'def %s(%s)' % (name, ', '.join(parts))
    if ret:
        d += ' -> ' + ret
    return d


def _sig(name, params, ret, doc, path, line):
    return {'name': name, 'params': params, 'ret': ret, 'docstring': doc or '',
            'key': (path, line)}


def _overload_siblings(mi, scope, node):
    nodes, sc = find_qual(mi, scope_chain(scope) + [node.name])
    funcs = [n for n in nodes if isinstance(n, FUNC_TYPES)]
    ov = [n for n in funcs if 'overload' in deco_names(n)]
    return ov or [node]


def func_sigs(f, name=None, skip_first=None, is_class=False, doc=None):
    node = f.node
    mi = f.scope.module if f.scope else None
    if mi is None:
        return []
    if isinstance(node, ast.Lambda):
        ps = ast_params(mi, node, f.bound is not None)
        return [_sig(name or '<lambda>', ps, None, doc or '', rel_module_path(mi.path), node.lineno)]
    if skip_first is None:
        decos = deco_names(node)
        skip_first = 'staticmethod' not in decos and (
            f.bound is not None or (f.scope.kind == 'class'))
    grp, gsc = stub_group(f)
    if grp and gsc is not None:
        tmi = gsc.module
        nodes = grp
    else:
        tmi = mi
        nodes = _overload_siblings(mi, f.scope, node)
    if doc is None:
        doc = ''
        srcf = source_func(f) if mi.is_stub else f
        if srcf is not None:
            doc = _docstring(srcf.node)
        if not doc:
            doc = _docstring(node)
    out = []
    for n in nodes:
        ps = ast_params(tmi, n, skip_first)
        ret = None if is_class or n.returns is None else node_text(tmi, n.returns)
        d = doc
        if not is_class:
            d = _docstring(n) or doc
        out.append(_sig(name or n.name, ps, ret, d, rel_module_path(tmi.path), n.lineno))
    return out


def class_sigs(cv):
    n = cv.node
    sm = source_class_node(cv.scope.module, cv.scope, n)
    doc = _docstring(sm[0]) if sm else ''
    doc = doc or _docstring(n)
    for c0 in class_mro(cv):
        pairs = class_pairs(c0)
        for c, is_stub in reversed(pairs):
            inits = [st for st in c.node.body if isinstance(st, FUNC_TYPES) and st.name == '__init__']
            if inits:
                cs = class_scope_of(c)
                if cs is None:
                    continue
                return func_sigs(FuncVal(inits[-1], cs), name=n.name, skip_first=True, is_class=True,
                                 doc=doc or _docstring(inits[-1]))
    if any(is_dataclass(c) for c in class_mro(cv)):
        fields = {}
        order = []
        for c in reversed(class_mro(cv)):
            mi = c.scope.module
            for st in c.node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    if _ann_name(st.annotation.value if isinstance(st.annotation, ast.Subscript)
                                 else st.annotation) == 'ClassVar':
                        continue
                    nm = st.target.id
                    if nm not in fields:
                        order.append(nm)
                    dflt = None
                    if st.value is not None:
                        v = st.value
                        if isinstance(v, ast.Call) and _ann_name(v.func) == 'field':
                            dflt = None
                            for kw in v.keywords:
                                if kw.arg == 'default':
                                    dflt = node_text(mi, kw.value)
                                elif kw.arg == 'default_factory':
                                    dflt = '<factory>'
                        else:
                            dflt = node_text(mi, v)
                    fields[nm] = SigParam(_fmt_param(nm, node_text(mi, st.annotation), dflt), 'pos', nm)
        ps = [fields[k] for k in order]
        mi = cv.scope.module
        return [_sig(n.name, ps, None, doc, rel_module_path(mi.path), n.lineno)]
    mi = cv.scope.module
    return [_sig(n.name, [], None, doc, rel_module_path(mi.path), n.lineno)]


def real_sigs(obj):
    try:
        sig = inspect.signature(obj)
    except Exception:
        return []
    name = getattr(obj, '__name__', None) or type(obj).__name__
    ps = []
    for p in sig.parameters.values():
        ann = '' if p.annotation is inspect.Parameter.empty else inspect.formatannotation(p.annotation)
        dflt = None if p.default is inspect.Parameter.empty else repr(p.default)
        k = p.kind
        if k == p.VAR_POSITIONAL:
            ps.append(SigParam(_fmt_param('*' + p.name, ann, None), 'var', p.name))
        elif k == p.VAR_KEYWORD:
            ps.append(SigParam(_fmt_param('**' + p.name, ann, None), 'kw', p.name))
        else:
            kind = {p.POSITIONAL_ONLY: 'posonly', p.KEYWORD_ONLY: 'kwonly'}.get(k, 'pos')
            ps.append(SigParam(_fmt_param(p.name, ann, dflt), kind, p.name))
    ret = None
    if not inspect.isclass(obj) and sig.return_annotation is not inspect.Signature.empty:
        ret = inspect.formatannotation(sig.return_annotation)
    path, line, _ = _real_location(obj) if not _is_builtin_obj(obj) else ('', 0, 0)
    return [_sig(name, ps, ret, inspect.getdoc(obj) or '', path, line)]


def value_sigs(v):
    if isinstance(v, FuncVal):
        return func_sigs(v)
    if isinstance(v, ClassVal):
        return class_sigs(v)
    if isinstance(v, InstVal):
        ok, ms = attr_vals(v, '__call__')
        out = []
        for m in ms if ok else []:
            if isinstance(m, FuncVal):
                out.extend(func_sigs(m))
        return out
    if isinstance(v, RealVal):
        return real_sigs(v.obj)
    return []


def param_index(params, segs):
    """Python-style binding of the argument under the cursor (last segment)."""
    prev = segs[:-1]
    cur_kind, cur_name = _classify_seg(segs[-1])
    npos = 0
    star_seen = False
    for sg in prev:
        k, nm = _classify_seg(sg)
        if k == 'pos':
            npos += 1
        elif k == 'star':
            star_seen = True
    positional = [i for i, p in enumerate(params) if p.kind in ('posonly', 'pos')]
    var = [i for i, p in enumerate(params) if p.kind == 'var']
    kw = [i for i, p in enumerate(params) if p.kind == 'kw']
    if cur_kind == 'kw':
        for i, p in enumerate(params):
            if p.name == cur_name and p.kind in ('pos', 'kwonly'):
                return i
        return kw[0] if kw else None
    if cur_kind == 'dstar':
        return kw[0] if kw else None
    if cur_kind == 'star':
        return var[0] if var else None
    if star_seen:
        return var[0] if var else None
    if npos < len(positional):
        return positional[npos]
    return var[0] if var else None


def signatures(path, line_no, col):
    reset_state()
    source = read_source(path)
    lines = [l[:-1] if l.endswith('\r') else l for l in source.split('\n')]
    if line_no < 1 or line_no > len(lines):
        raise SithError('line out of range')
    cur = lines[line_no - 1]
    if col < 0 or col > len(cur):
        raise SithError('column out of range')
    prefix = '\n'.join(lines[:line_no - 1] + [cur[:col]])
    code = blank_code(prefix)
    stack = []
    for i, c in enumerate(code):
        if c in '([{':
            stack.append(i)
        elif c in ')]}':
            if stack:
                stack.pop()
    popen = None
    for i in reversed(stack):
        if code[i] == '(' and _is_call_paren(code, i):
            popen = i
            break
    if popen is None:
        return []
    pline, pcol = _offset_to_pos(prefix, popen)
    segs = _split_args(code[popen + 1:])

    apath = os.path.abspath(path)
    closers = ''.join({'(': ')', '[': ']', '{': '}'}[code[i]] for i in reversed(stack))
    head = cur[:col]
    cands = [None, head + closers, head + closers + ':', head + closers + ': pass']
    mi = call = None
    for cand in cands:
        trial = list(lines)
        if cand is not None:
            trial[line_no - 1] = cand
        tree, mods, fixed = fix_and_parse(trial)
        m = ModuleInfo(apath, os.path.splitext(os.path.basename(apath))[0])
        m.lines = fixed if cand is None else trial
        m.set_tree(tree)
        c = _find_call(m, pline, pcol)
        if c is not None:
            mi, call = m, c
            break
    if call is None:
        return []
    _local_modules[apath] = mi
    parents = mi.parents()
    s = scope_of_node(mi, call, parents)
    vals = infer_vals(call.func, s, _start(call))
    sigs = []
    seen = set()
    for v in vals:
        for sg in value_sigs(v):
            k = (sg['key'], sg['name'], tuple(p.text for p in sg['params']))
            if k in seen:
                continue
            seen.add(k)
            sigs.append(sg)
    sigs.sort(key=lambda sg: sg['key'])
    out = []
    for sg in sigs:
        ps = sg['params']
        out.append({'name': sg['name'], 'params': [p.text for p in ps],
                    'index': param_index(ps, segs),
                    'description': _sig_description(sg['name'], ps, sg['ret']),
                    'docstring': sg['docstring']})
    return out


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _mod_key(mv, dotted):
    if mv is not None and mv.local is not None:
        return ('mod', os.path.abspath(mv.local.path))
    if mv is not None and mv.real is not None:
        return ('ext', mv.real.__name__)
    return ('ext', dotted)


def def_keys(d, seen=None):
    if seen is None:
        seen = set()
    if d is None:
        return set()
    if id(d) in seen:
        return set()
    seen.add(id(d))
    if isinstance(d, StarHit):
        mv = d.star.module_value()
        if mv is not None and mv.local is not None:
            out = set()
            for x in mv.local.attr_defs(d.name):
                out |= def_keys(x, seen)
            return out or {('mattr', os.path.abspath(mv.local.path), d.name)}
        return {('ext', d.star.module, d.name)}
    if isinstance(d, AttrDef):
        return {('cls', id(d.cls.node), d.name)}
    k = d.kind
    mi = d.scope.module
    if k == 'import':
        name, asname = d.extra
        dotted = name if asname else name.split('.')[0]
        return {_mod_key(mi.resolve_import(dotted, 0), dotted)}
    if k == 'importfrom':
        modname, name, level = d.extra
        mv = mi.resolve_import(modname, level)
        if mv is not None and mv.local is not None:
            ds = mv.local.attr_defs(name)
            out = set()
            for x in ds:
                out |= def_keys(x, seen)
            if out:
                return out
            if mv.local.is_package:
                sub = mv.local.submodule(name)
                if sub is not None:
                    return {_mod_key(sub, name)}
            return {('mattr', os.path.abspath(mv.local.path), name)}
        sub = (modname + '.' + name) if modname else name
        mv2 = mi.resolve_import(sub, level)
        if mv2 is not None and mv2.local is not None:
            return {_mod_key(mv2, sub)}
        return {('ext', ('.' * level) + modname, name)}
    if d.scope.kind == 'class':
        return {('cls', id(d.scope.node), d.name)}
    if d.scope.kind == 'module':
        return {('mattr', os.path.abspath(mi.path), d.name)} | {('scope', id(d.scope.node), d.name)}
    return {('scope', id(d.scope.node), d.name)}


def _norm_keys(keys):
    return set(keys)


def _value_attr_keys(v, name):
    if isinstance(v, ModVal):
        if v.local is not None:
            ds = v.local.attr_defs(name)
            if ds:
                out = set()
                for d in ds:
                    out |= def_keys(d)
                return out
            if v.local.is_package:
                sub = v.local.submodule(name)
                if sub is not None:
                    return {_mod_key(sub, name)}
            return {('mattr', os.path.abspath(v.local.path), name)}
        if v.real is not None:
            return {('ext', v.real.__name__, name)}
        return set()
    if isinstance(v, (ClassVal, InstVal)):
        cls = v if isinstance(v, ClassVal) else v.cls
        m = class_attr_map(cls, isinstance(v, InstVal))
        ents = m.get(name)
        if not ents and isinstance(v, ClassVal):
            ents = class_attr_map(cls, True).get(name)
        if ents:
            out = set()
            for kind, x, c in nav_ents(ents):
                if kind == 'stub':
                    continue
                out |= def_keys(x)
            if out:
                return out
        return {('cls', id(cls.node), name)}
    if isinstance(v, RealInst):
        return {('ext', v.typ.__module__ + '.' + v.typ.__name__, name)}
    if isinstance(v, RealVal):
        return {('ext', _real_full_name(v.obj), name)}
    return set()


def occ_keys(mi, occ, parents):
    k = occ.kind
    node = occ.node
    try:
        if k == 'name':
            s = scope_of_node(mi, node, parents)
            if isinstance(node.ctx, ast.Store):
                ds = [d for d in mi.defs_by_node(node) if d.name == node.id]
                if not ds:
                    ds = lookup_defs(node.id, s, INF_POS)
            else:
                ds = lookup_defs(node.id, s, _start(node))
                if not ds:
                    ds = lookup_defs(node.id, s, INF_POS)
            out = set()
            for d in ds:
                out |= def_keys(d)
            if not out:
                if hasattr(builtins, node.id):
                    return {('builtin', node.id)}
                return {('free', node.id)}
            return out
        if k == 'attr':
            s = scope_of_node(mi, node, parents)
            out = set()
            for v in infer_vals(node.value, s, _start(node)):
                out |= _value_attr_keys(v, node.attr)
            return out
        if k in ('defname', 'arg', 'binder'):
            ds = [d for d in mi.defs_by_node(node) if d.name == occ.name]
            out = set()
            for d in ds[:1]:
                out |= def_keys(d)
            return out
        if k == 'keyword':
            call = parents.get(node)
            if not isinstance(call, ast.Call):
                return set()
            s = scope_of_node(mi, call, parents)
            fvals = infer_vals(call.func, s, _start(call))
            out = set()
            for d in _callable_params(fvals, node.arg):
                out |= def_keys(d)
            return out
        if k == 'importname':
            a = occ.extra
            ds = [d for d in mi.defs_by_node(node) if d.name == (a.asname or a.name)]
            out = set()
            for d in ds[:1]:
                out |= def_keys(d)
            return out
        if k in ('importmod', 'importas'):
            dotted, level, alias = occ.extra
            return {_mod_key(mi.resolve_import(dotted, level), ('.' * level) + dotted)}
        if k in ('global', 'nonlocal'):
            s = scope_of_node(mi, node, parents)
            target = mi.scope if k == 'global' else (s.parent if s is not None else None)
            while target is not None:
                if target.kind != 'class':
                    ds = reaching_defs(target, occ.name, INF_POS)
                    if ds or k == 'global':
                        out = set()
                        for d in ds:
                            out |= def_keys(d)
                        if not out:
                            out = def_keys(Def(occ.name, 'assign', node, target, (0, 0)))
                        return out
                target = target.parent
            return set()
    except RecursionError:
        return set()
    return set()


def occ_is_definition(mi, occ):
    k = occ.kind
    node = occ.node
    if k == 'name':
        return isinstance(node.ctx, ast.Store)
    if k == 'attr':
        return isinstance(node.ctx, ast.Store)
    if k in ('defname', 'arg', 'binder', 'importas'):
        return True
    if k == 'importname':
        a = occ.extra
        if not a.asname:
            return True
        return alias_name_pos(mi, node, a, 'as') == (occ.line, occ.bcol)
    if k == 'importmod':
        dotted, level, alias = occ.extra
        return isinstance(node, ast.Import) and alias is not None and not alias.asname \
            and '.' not in dotted
    return False


def _all_occurrences(mi):
    occs, parents = collect_occurrences(mi, None)
    return occs, parents


def _occ_out(mi, occ):
    return {'module_path': rel_module_path(mi.path), 'line': occ.line,
            'column': _char_col(mi, occ.line, occ.bcol), 'is_definition': occ_is_definition(mi, occ)}


def references(path, line_no, col, scope):
    mi, occ, hits = find_reference_occs(path, line_no, col, scope)
    results = {}
    for m, o in hits:
        r = _occ_out(m, o)
        key = (r['module_path'], r['line'], r['column'])
        if key in results:
            results[key]['is_definition'] = results[key]['is_definition'] or r['is_definition']
        else:
            results[key] = r
    out = list(results.values())
    out.sort(key=lambda r: (r['module_path'], r['line'], r['column']))
    return out


def find_reference_occs(path, line_no, col, scope):
    """Returns (target module, cursor occurrence, [(module, occurrence), ...])."""
    reset_state()
    _deadline[0] = time.time() + 30
    mi, lines = load_target(path, line_no, col)
    cur = lines[line_no - 1]
    bcol = len(cur[:col].encode('utf-8'))
    occ, parents = find_occurrence(mi, line_no, bcol)
    if occ is None or occ.kind == 'const':
        raise NotAName()
    target = _norm_keys(occ_keys(mi, occ, parents))
    name = occ.name
    modules = [mi]
    if scope == 'project':
        for p in project_files():
            if p == mi.path:
                continue
            m = load_project_module(p)
            if m is not None and m.tree is not None:
                modules.append(m)
    hits = []
    for m in modules:
        occs, pars = _all_occurrences(m)
        m._ref_parents = pars
        for o in occs:
            if o.name != name or o.kind == 'const':
                continue
            if m is mi and o.line == occ.line and o.bcol == occ.bcol:
                hit = True
            elif target:
                hit = bool(_norm_keys(occ_keys(m, o, pars)) & target)
            else:
                hit = False
            if hit:
                hits.append((m, o))
    mi._ref_target = target
    mi._ref_parents = parents
    return mi, occ, hits


# ---------------------------------------------------------------------------
# Search / names
# ---------------------------------------------------------------------------

_SEARCH_KINDS = ('def', 'class', 'assign', 'annassign')


def _searchable_defs(mi):
    out = []

    def visit(s, top):
        for name, ds in s.defs.items():
            for d in ds:
                if d.kind in ('def', 'class') or (top and d.kind in ('assign', 'annassign')):
                    out.append(d)
                if d.kind == 'class':
                    cs = mi.node_scope.get(d.node)
                    if cs is not None:
                        visit(cs, False)
    visit(mi.scope, True)
    return out


def search(query):
    reset_state()
    _deadline[0] = time.time() + 30
    q = query.lower()
    hits = []
    for p in project_files():
        mi = load_project_module(p)
        if mi is None or mi.scope is None:
            continue
        for d in _searchable_defs(mi):
            nl = d.name.lower()
            if q not in nl:
                continue
            rank = 0 if nl == q else (1 if nl.startswith(q) else 2)
            for o in def_out(d, follow=False):
                if o is None:
                    continue
                o = dict(o)
                o.pop('docstring', None)
                hits.append((rank, o))
    seen = set()
    res = []
    hits.sort(key=lambda h: (h[0], h[1]['module_path'], h[1]['line'], h[1]['column']))
    for rank, o in hits:
        k = (o['module_path'], o['line'], o['column'], o['name'])
        if k in seen:
            continue
        seen.add(k)
        res.append(o)
    return res


def names(path, all_scopes):
    reset_state()
    mi, lines = load_target(path)
    items = []
    scopes = mi.all_scopes() if all_scopes else [mi.scope]
    for s in scopes:
        for nm, ds in s.defs.items():
            for d in ds:
                if nm == PLACEHOLDER:
                    continue
                items.extend(def_out(d, follow=False))
    if all_scopes:
        for node in ast.walk(mi.tree):
            if not isinstance(node, ast.ClassDef):
                continue
            cs = mi.node_scope.get(node)
            if cs is None:
                continue
            cv = ClassVal(node, cs.parent)
            for m in node.body:
                if not isinstance(m, FUNC_TYPES):
                    continue
                decos = deco_names(m)
                params = func_params(m)
                if 'staticmethod' in decos or 'classmethod' in decos or not params:
                    continue
                fs = mi.node_scope.get(m)
                if fs is None:
                    continue

                def cb(name, t, kind, value, path_, stmt, fs=fs, m=m):
                    items.append(attrdef_out(AttrDef(name, t, kind, value, path_, stmt, fs, m, cv)))
                collect_attr_assigns(m.body, params[0].arg, cb)
    seen = set()
    out = []
    for o in items:
        if not o:
            continue
        k = (o['line'], o['column'], o['name'])
        if k in seen:
            continue
        seen.add(k)
        o = dict(o)
        o['is_definition'] = True
        out.append(o)
    out.sort(key=lambda o: (o['line'], o['column']))
    return out


# ---------------------------------------------------------------------------
# Refactoring
# ---------------------------------------------------------------------------

class RefactorError(SithError):
    pass


class _Doc(object):
    """Original text of a file plus a list of pending edits."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.text = read_source(self.path)
        self.lines = self.text.split('\n')
        self.starts = []
        o = 0
        for l in self.lines:
            self.starts.append(o)
            o += len(l) + 1
        self.edits = []

    def line(self, ln):
        if 1 <= ln <= len(self.lines):
            return self.lines[ln - 1].rstrip('\r')
        return ''

    def off(self, ln, ccol):
        """Absolute offset of a (1-based line, character column) position."""
        if ln < 1:
            return 0
        if ln > len(self.lines):
            return len(self.text)
        return self.starts[ln - 1] + max(0, min(ccol, len(self.line(ln))))

    def boff(self, ln, bcol):
        if ln < 1 or ln > len(self.lines):
            return self.off(ln, 0)
        b = self.line(ln).encode('utf-8')
        return self.starts[ln - 1] + len(b[:bcol].decode('utf-8', errors='ignore'))

    def node_start(self, node):
        return self.boff(node.lineno, node.col_offset)

    def node_end(self, node):
        return self.boff(node.end_lineno, node.end_col_offset)

    def pos_of(self, off):
        import bisect
        i = bisect.bisect_right(self.starts, off) - 1
        return i + 1, off - self.starts[i]

    def line_start(self, ln):
        return self.off(ln, 0)

    def line_end(self, ln):
        """Offset just after the newline ending line ln (or end of text)."""
        if ln >= len(self.lines):
            return len(self.text)
        return self.starts[ln]

    def replace(self, a, b, text):
        self.edits.append((a, b, text))

    def new_text(self):
        out = self.text
        eds = sorted(set(self.edits), key=lambda e: (e[0], e[1]))
        for i in range(1, len(eds)):
            if eds[i][0] < eds[i - 1][1]:
                raise RefactorError('overlapping edits')
        for a, b, t in reversed(eds):
            out = out[:a] + t + out[b:]
        return out


def _check_ident(name):
    if not name or not _is_ident(name):
        raise RefactorError('invalid identifier: %r' % (name,))


def _rel(path):
    return rel_module_path(path)


def _unified_diff(old, new, from_p, to_p):
    import difflib
    old_lines = old.splitlines(True) or ['']
    new_lines = new.splitlines(True) or ['']
    if old_lines[-1] and not old_lines[-1].endswith('\n'):
        old_lines[-1] += '\n'
    if new_lines[-1] and not new_lines[-1].endswith('\n'):
        new_lines[-1] += '\n'
    old_lines = [l for l in old_lines if l]
    new_lines = [l for l in new_lines if l]
    return ''.join(difflib.unified_diff(old_lines, new_lines, fromfile=from_p, tofile=to_p))


def _map_renamed(path, renames):
    for old, new in renames:
        if path == old:
            return new
        if path.startswith(old + os.sep):
            return new + path[len(old):]
    return path


def refactor_result(docs, renames=()):
    """Build {'changed_files':..., 'renames':...} plus a diff text."""
    changed = {}
    diff_parts = []
    for old, new in renames:
        diff_parts.append('rename from %s\nrename to %s\n' % (_rel(old), _rel(new)))
    for path in sorted(docs):
        d = docs[path]
        nt = d.new_text()
        if nt == d.text:
            continue
        changed[_rel(path)] = nt
        diff_parts.append(_unified_diff(d.text, nt, _rel(path), _rel(_map_renamed(path, renames))))
    ren = dict((_rel(o), _rel(n)) for o, n in renames)
    return {'changed_files': changed, 'renames': ren}, ''.join(diff_parts)


def _get_doc(docs, path):
    path = os.path.abspath(path)
    if path not in docs:
        docs[path] = _Doc(path)
    return docs[path]


def _parents_of(tree):
    par = {}
    for node in ast.walk(tree):
        for c in ast.iter_child_nodes(node):
            par[c] = node
    return par


def _strict_parse(doc):
    try:
        return ast.parse(doc.text)
    except (SyntaxError, ValueError) as e:
        raise RefactorError('cannot refactor a file with syntax errors: %s' % getattr(e, 'msg', e))


# --- rename -------------------------------------------------------------------

def _module_target(mi, occ, keys):
    """Path of the local module/package the cursor names (or None)."""
    for k in keys:
        if k[0] != 'mod':
            continue
        p = k[1]
        if os.path.isdir(p):
            base = os.path.basename(p.rstrip(os.sep))
        elif os.path.basename(p) in ('__init__.py', '__init__.pyi'):
            base = os.path.basename(os.path.dirname(p))
        else:
            base = os.path.splitext(os.path.basename(p))[0]
        if base == occ.name and occ.kind != 'importas':
            return p
    return None


def _name_collides(m, o, pars, new_name):
    try:
        node = o.node
        if o.kind == 'attr':
            s = scope_of_node(m, node, pars)
            for v in infer_vals(node.value, s, _start(node)):
                if isinstance(v, (ClassVal, InstVal)):
                    cls = v if isinstance(v, ClassVal) else v.cls
                    if class_attr_map(cls, True).get(new_name) or class_attr_map(cls, False).get(new_name):
                        return True
                elif isinstance(v, ModVal) and v.local is not None:
                    if v.local.attr_defs(new_name):
                        return True
            return False
        if o.kind == 'keyword':
            return False
        if o.kind == 'defname' and node in pars:
            s = scope_of_node(m, pars[node], pars)
        else:
            s = scope_of_node(m, node, pars)
        if s is None:
            return False
        if o.kind == 'arg':
            fs = m.node_scope.get(pars.get(pars.get(node)))
            if fs is not None and new_name in fs.defs:
                return True
        if new_name in s.defs:
            return True
        return bool(lookup_defs(new_name, s, INF_POS))
    except RecursionError:
        return False


def rename(path, line_no, col, new_name):
    _check_ident(new_name)
    mi, occ, hits = find_reference_occs(path, line_no, col, 'project')
    keys = mi._ref_target
    if keys and all(k[0] in ('builtin', 'ext') for k in keys):
        raise RefactorError('cannot rename a builtin or external name: %s' % occ.name)
    old = occ.name
    docs = {}
    renames = []
    modpath = _module_target(mi, occ, keys)
    if modpath is not None:
        if os.path.isdir(modpath) or os.path.basename(modpath) in ('__init__.py', '__init__.pyi'):
            src = modpath.rstrip(os.sep) if os.path.isdir(modpath) else os.path.dirname(modpath)
            dst = os.path.join(os.path.dirname(src), new_name)
        else:
            ext = os.path.splitext(modpath)[1]
            src = modpath
            dst = os.path.join(os.path.dirname(src), new_name + ext)
        if src != dst:
            par = os.path.dirname(src)
            if os.path.exists(dst) or os.path.exists(os.path.join(par, new_name + '.py')) \
                    or os.path.isdir(os.path.join(par, new_name)):
                raise RefactorError('cannot rename module %r: %r already exists' % (old, _rel(dst)))
            renames.append((src, dst))
            # Keep a stub next to a renamed module in sync.
            if not os.path.isdir(src) and src.endswith('.py') and os.path.isfile(src + 'i'):
                renames.append((src + 'i', dst + 'i'))
    else:
        only_mod = keys and all(k[0] == 'mod' for k in keys)
        if only_mod:
            hits = [(m, o) for m, o in hits if m is mi]
        if new_name != old:
            for m, o in hits:
                if _name_collides(m, o, getattr(m, '_ref_parents', {}), new_name):
                    raise RefactorError('new name %r collides with an existing name in scope (%s:%d)'
                                        % (new_name, _rel(m.path), o.line))
    if new_name != old:
        seen = set()
        for m, o in hits:
            if m.namespace:
                continue
            k = (m.path, o.line, o.bcol)
            if k in seen:
                continue
            seen.add(k)
            d = _get_doc(docs, m.path)
            a = d.boff(o.line, o.bcol)
            b = d.boff(o.line, o.bcol + len(old.encode('utf-8')))
            if d.text[a:b] != old:
                continue
            d.replace(a, b, new_name)
    return refactor_result(docs, renames)


# --- precedence ---------------------------------------------------------------

_ATOM = 16

_BINOP_PREC = {ast.BitOr: 7, ast.BitXor: 8, ast.BitAnd: 9, ast.LShift: 10, ast.RShift: 10,
               ast.Add: 11, ast.Sub: 11, ast.Mult: 12, ast.MatMult: 12, ast.Div: 12,
               ast.FloorDiv: 12, ast.Mod: 12, ast.Pow: 14}


def _expr_prec(node):
    if isinstance(node, (ast.NamedExpr, ast.Tuple, ast.Yield, ast.YieldFrom, ast.Starred)):
        return 0
    if isinstance(node, ast.Lambda):
        return 1
    if isinstance(node, ast.IfExp):
        return 2
    if isinstance(node, ast.BoolOp):
        return 3 if isinstance(node.op, ast.Or) else 4
    if isinstance(node, ast.UnaryOp):
        return 5 if isinstance(node.op, ast.Not) else 13
    if isinstance(node, ast.Compare):
        return 6
    if isinstance(node, ast.BinOp):
        return _BINOP_PREC.get(type(node.op), 12)
    if isinstance(node, ast.Await):
        return 15
    return _ATOM


def _required_prec(node, parent):
    """Minimum precedence an expression replacing `node` needs to keep its meaning."""
    if parent is None:
        return 1
    if isinstance(parent, ast.BinOp):
        p = _BINOP_PREC.get(type(parent.op), 12)
        if isinstance(parent.op, ast.Pow):
            return 15 if node is parent.left else 13
        return p if node is parent.left else p + 1
    if isinstance(parent, ast.UnaryOp):
        return 5 if isinstance(parent.op, ast.Not) else 13
    if isinstance(parent, ast.BoolOp):
        return (3 if isinstance(parent.op, ast.Or) else 4) + 1
    if isinstance(parent, ast.Compare):
        return 7
    if isinstance(parent, ast.IfExp):
        return 2 if node is parent.orelse else 3
    if isinstance(parent, ast.Await):
        return _ATOM
    if isinstance(parent, (ast.Attribute, ast.Subscript)) and node is parent.value:
        return _ATOM
    if isinstance(parent, ast.Call) and node is parent.func:
        return _ATOM
    if isinstance(parent, ast.Starred):
        return 7
    if isinstance(parent, ast.comprehension) and node is not parent.target:
        return 3
    if isinstance(parent, ast.FormattedValue):
        return 3
    if isinstance(parent, (ast.Dict,)) or isinstance(parent, ast.keyword) and parent.arg is None:
        return 7 if isinstance(parent, ast.keyword) else 1
    return 1


def _wrapped_in_parens(code):
    """True if code (already blanked) is enclosed in one pair of matching brackets."""
    code = code.strip()
    if len(code) < 2 or code[0] != '(' or code[-1] != ')':
        return False
    depth = 0
    for i, c in enumerate(code):
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0 and i != len(code) - 1:
                return False
    return True


def _is_bracketed(code):
    code = code.strip()
    if len(code) < 2 or code[0] not in '([{':
        return False
    close = {'(': ')', '[': ']', '{': '}'}[code[0]]
    if code[-1] != close:
        return False
    depth = 0
    for i, c in enumerate(code):
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0 and i != len(code) - 1:
                return False
    return True


# --- inline -------------------------------------------------------------------

def _value_text(doc, stmt, value):
    """Source of an assignment's value, including any parentheses around it."""
    a = doc.node_start(value)
    b = doc.node_end(value)
    code = blank_code(doc.text)
    lo = doc.node_start(stmt)
    hi = doc.node_end(stmt)
    while True:
        i = a - 1
        while i > lo and code[i] in ' \t\\\r\n':
            i -= 1
        j = b
        while j < hi and code[j] in ' \t\\\r\n':
            j += 1
        if i >= lo and j < hi and code[i] == '(' and code[j] == ')':
            a, b = i, j + 1
        else:
            break
    return doc.text[a:b], code[a:b]


def _block_of(stmt, parents):
    par = parents.get(stmt)
    if par is None:
        return None
    for field in ('body', 'orelse', 'finalbody', 'handlers'):
        lst = getattr(par, field, None)
        if isinstance(lst, list) and stmt in lst:
            return lst
    return None


def _remove_stmt(doc, stmt, parents):
    """Delete a simple statement from the document."""
    a = doc.node_start(stmt)
    b = doc.node_end(stmt)
    code = blank_code(doc.text)
    ls = doc.line_start(stmt.lineno)
    before = code[ls:a]
    j = b
    while j < len(code) and code[j] in ' \t':
        j += 1
    has_semi_after = j < len(code) and code[j] == ';'
    block = _block_of(stmt, parents)
    if block is not None and len(block) == 1:
        doc.replace(a, b, 'pass')
        return
    if before.strip() == '':
        if has_semi_after:
            k = j + 1
            while k < len(code) and code[k] in ' \t':
                k += 1
            doc.replace(a, k, '')
            return
        # whole line(s)
        le = doc.line_end(stmt.end_lineno)
        rest = code[b:le].strip()
        if rest == '' or rest.startswith('#') or set(rest) <= set(' '):
            doc.replace(ls, le, '')
        else:
            doc.replace(a, b, '')
        return
    # something precedes on the same line, i.e. "...; x = 1"
    k = a - 1
    while k > ls and code[k] in ' \t':
        k -= 1
    if code[k] == ';':
        doc.replace(k, b, '')
    else:
        doc.replace(a, b, '')


def inline(path, line_no, col):
    mi, occ, hits = find_reference_occs(path, line_no, col, 'project')
    defs = []
    refs = []
    for m, o in hits:
        if o.kind == 'defname':
            raise RefactorError('cannot inline a function/class definition')
        if occ_is_definition(m, o):
            defs.append((m, o))
        elif o.kind in ('name', 'attr'):
            refs.append((m, o))
    if not defs:
        raise RefactorError('no definition found to inline')
    for m, o in defs:
        if o.kind == 'arg':
            raise RefactorError('cannot inline a parameter')
        if o.kind in ('importname', 'importas', 'importmod'):
            raise RefactorError('cannot inline an import')
        if o.kind != 'name' and o.kind != 'attr':
            raise RefactorError('cannot inline this name')
    if len(defs) > 1:
        raise RefactorError('cannot inline a name with multiple definitions')
    dm, do = defs[0]
    pars = getattr(dm, '_ref_parents', {})
    stmt = pars.get(do.node)
    if isinstance(stmt, ast.Assign):
        if len(stmt.targets) != 1 or stmt.targets[0] is not do.node:
            raise RefactorError('cannot inline a statement with multiple definitions')
        value = stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.target is do.node:
        if stmt.value is None:
            raise RefactorError('cannot inline a statement that is defined by an annotation')
        value = stmt.value
    else:
        raise RefactorError('can only inline a simple assignment (x = <expr>)')
    if not refs:
        raise RefactorError('name has no references to inline')
    docs = {}
    ddoc = _get_doc(docs, dm.path)
    vtext, vcode = _value_text(ddoc, stmt, value)
    if _wrapped_in_parens(vcode) or _is_bracketed(vcode):
        prec = _ATOM
    else:
        prec = _expr_prec(value)
    multiline = '\n' in vtext
    if multiline and prec != _ATOM:
        vtext = '(' + vtext + ')'
        prec = _ATOM
    is_number = isinstance(value, ast.Constant) and isinstance(value.value, (int, float, complex)) \
        and not isinstance(value.value, bool) and prec == _ATOM and not vcode.strip().startswith('(')
    for m, o in refs:
        d = _get_doc(docs, m.path)
        mp = getattr(m, '_ref_parents', {})
        node = o.node
        parent = mp.get(node)
        need = _required_prec(node, parent)
        s = vtext
        if prec < need or (is_number and isinstance(parent, ast.Attribute) and node is parent.value):
            s = '(' + vtext + ')'
        d.replace(d.node_start(node), d.node_end(node), s)
    _remove_stmt(ddoc, stmt, pars)
    return refactor_result(docs)


# --- extract variable ---------------------------------------------------------

def _parse_until(text):
    try:
        a, b = text.split(':', 1)
        return int(a), int(b)
    except (ValueError, AttributeError):
        raise RefactorError('--until must be <line>:<col>')


def _check_pos(doc, ln, col, what):
    if ln < 1 or ln > len(doc.lines):
        raise RefactorError('%s line out of range' % what)
    if col < 0 or col > len(doc.line(ln)):
        raise RefactorError('%s column out of range' % what)


def _selection(doc, line_no, col, until):
    ul, uc = until
    _check_pos(doc, line_no, col, 'start')
    _check_pos(doc, ul, uc, 'end')
    a = doc.off(line_no, col)
    b = doc.off(ul, uc)
    if b < a:
        a, b = b, a
    return a, b


def _bound_names(node):
    """Names bound inside a lambda/comprehension node (its own scope)."""
    out = set()
    if isinstance(node, ast.Lambda):
        a = node.args
        for p in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            if p is not None:
                out.add(p.arg)
    elif isinstance(node, COMP_TYPES):
        for g in node.generators:
            for n in ast.walk(g.target):
                if isinstance(n, ast.Name):
                    out.add(n.id)
    return out


def _enclosing_stmt(node, parents):
    n = node
    while n is not None and not isinstance(n, ast.stmt):
        n = parents.get(n)
    return n


def extract_variable(path, line_no, col, until, name):
    _check_ident(name)
    doc = _Doc(path)
    tree = _strict_parse(doc)
    a, b = _selection(doc, line_no, col, until)
    code = blank_code(doc.text)
    while a < b and code[a] in ' \t\r\n\\':
        a += 1
    while b > a and code[b - 1] in ' \t\r\n\\':
        b -= 1
    if a >= b:
        raise RefactorError('selection is not a complete expression')
    parents = _parents_of(tree)
    spans = [(a, b)]
    # Selection with surrounding parentheses: also try the inner expression.
    aa, bb = a, b
    while _wrapped_in_parens(code[aa:bb]):
        aa += 1
        bb -= 1
        while aa < bb and code[aa] in ' \t\r\n\\':
            aa += 1
        while bb > aa and code[bb - 1] in ' \t\r\n\\':
            bb -= 1
        spans.append((aa, bb))
    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr) or not hasattr(node, 'end_lineno'):
            continue
        sp = (doc.node_start(node), doc.node_end(node))
        if sp in spans:
            found = node
            break
    if found is None:
        raise RefactorError('selection is not a complete expression')
    node = found
    if isinstance(getattr(node, 'ctx', None), (ast.Store, ast.Del)):
        raise RefactorError('cannot extract an assignment target')
    if isinstance(node, (ast.Starred, ast.Slice)):
        raise RefactorError('selection is not a complete expression')
    par = parents.get(node)
    if isinstance(par, (ast.JoinedStr,)):
        raise RefactorError('selection is not a complete expression')
    # Names bound by enclosing lambdas / comprehensions cannot be extracted.
    used = set(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
    p = par
    while p is not None and not isinstance(p, ast.stmt):
        if isinstance(p, (ast.Lambda,) + COMP_TYPES) and used & _bound_names(p):
            raise RefactorError('cannot extract an expression using names local to a lambda/comprehension')
        p = parents.get(p)
    stmt = _enclosing_stmt(node, parents)
    if stmt is None:
        raise RefactorError('selection is not inside a statement')
    # elif headers: insert before the whole if/elif chain
    while isinstance(stmt, ast.If):
        up = parents.get(stmt)
        if isinstance(up, ast.If) and up.orelse == [stmt] and doc.line(stmt.lineno).lstrip().startswith('elif'):
            stmt = up
        else:
            break
    expr_text = doc.text[a:b]
    expr_code = code[a:b]
    if (node is found and (a, b) == spans[0]) and not (_is_bracketed(expr_code)):
        if '\n' in expr_text or isinstance(node, (ast.NamedExpr,)):
            expr_text = '(' + expr_text + ')'
    start_line = stmt.lineno
    start_off = doc.node_start(stmt)
    decos = getattr(stmt, 'decorator_list', None)
    if decos:
        dl = min(dd.lineno for dd in decos)
        start_line = dl
        # the '@' precedes the decorator expression
        ls = doc.line_start(dl)
        start_off = ls + leading_ws(doc.line(dl))
    ls = doc.line_start(start_line)
    docs = {doc.path: doc}
    if code[ls:start_off].strip() == '':
        indent = doc.line(start_line)[:leading_ws(doc.line(start_line))]
        doc.replace(ls, ls, '%s%s = %s\n' % (indent, name, expr_text))
    else:
        doc.replace(start_off, start_off, '%s = %s; ' % (name, expr_text))
    doc.replace(a, b, name)
    return refactor_result(docs)


# --- extract function ---------------------------------------------------------

_BODY_FIELDS = ('body', 'orelse', 'finalbody')


def _sub_bodies(st):
    out = []
    for f in _BODY_FIELDS:
        lst = getattr(st, f, None)
        if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
            out.append(lst)
    for h in getattr(st, 'handlers', None) or []:
        out.append(h.body)
    for c in getattr(st, 'cases', None) or []:
        out.append(c.body)
    return out


def _stmt_span(doc, st):
    a = doc.node_start(st)
    decos = getattr(st, 'decorator_list', None)
    if decos:
        dl = min(d.lineno for d in decos)
        a = min(a, doc.line_start(dl) + leading_ws(doc.line(dl)))
    return a, doc.node_end(st)


def _find_selected_stmts(doc, tree, a, b):
    code = blank_code(doc.text)
    body = tree.body
    while True:
        over = []
        for st in body:
            sa, sb = _stmt_span(doc, st)
            if sa < b and sb > a:
                over.append((st, sa, sb))
        if not over:
            raise RefactorError('selection does not contain any statements')
        full = [sa >= a and sb <= b + (1 if sb - 1 >= a else 0) for st, sa, sb in over]
        if all(full):
            break
        if len(over) == 1:
            st = over[0][0]
            sub = None
            for lst in _sub_bodies(st):
                lo = _stmt_span(doc, lst[0])[0]
                hi = _stmt_span(doc, lst[-1])[1]
                if lo < b and hi > a:
                    if sub is not None:
                        raise RefactorError('selection is not a complete set of statements')
                    sub = lst
            if sub is None:
                raise RefactorError('selection is not a complete set of statements')
            body = sub
            continue
        raise RefactorError('selection is not a complete set of statements')
    stmts = [o[0] for o in over]
    first_a = over[0][1]
    last_b = max(over[-1][2], over[-1][1])
    if code[min(a, first_a):first_a].strip() not in ('',) or code[last_b:max(b, last_b)].strip(' \t\r\n;') != '':
        raise RefactorError('selection is not a complete set of statements')
    # Whole lines only: nothing else may share the first/last line.
    ls = doc.line_start(stmts[0].lineno if not getattr(stmts[0], 'decorator_list', None)
                        else doc.pos_of(first_a)[0])
    if code[ls:first_a].strip() != '':
        raise RefactorError('selection must span complete lines')
    le = doc.line_end(stmts[-1].end_lineno)
    if code[last_b:le].strip() != '':
        raise RefactorError('selection must span complete lines')
    return stmts, body, first_a, last_b


class _NameFlow(object):
    """Walks statements in evaluation order collecting reads/writes."""

    def __init__(self):
        self.events = []   # (kind, name, node) with kind in 'load'/'store'

    def load(self, name, node):
        self.events.append(('load', name, node))

    def store(self, name, node):
        self.events.append(('store', name, node))

    def target(self, t):
        if isinstance(t, ast.Name):
            if isinstance(t.ctx, ast.Del):
                return
            self.store(t.id, t)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self.target(e)
        elif isinstance(t, ast.Starred):
            self.target(t.value)
        else:
            self.expr(t)

    def nested_free(self, node, bound):
        stores = set(bound)
        loads = []
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Load):
                    loads.append(n)
                else:
                    stores.add(n.id)
            elif isinstance(n, ast.arg):
                stores.add(n.arg)
            elif isinstance(n, FUNC_TYPES + (ast.ClassDef,)) and n is not node:
                stores.add(n.name)
        for n in sorted(loads, key=lambda x: (x.lineno, x.col_offset)):
            if n.id not in stores:
                self.load(n.id, n)

    def expr(self, n):
        if n is None:
            return
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load):
                self.load(n.id, n)
            else:
                self.store(n.id, n)
            return
        if isinstance(n, ast.NamedExpr):
            self.expr(n.value)
            self.target(n.target)
            return
        if isinstance(n, ast.Lambda):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                self.expr(d)
            self.nested_free(n.body, _bound_names(n))
            return
        if isinstance(n, COMP_TYPES):
            self.expr(n.generators[0].iter)
            parts = [g for g in n.generators]
            bound = _bound_names(n)
            for i, g in enumerate(parts):
                if i:
                    self.nested_free(g.iter, bound)
                for c in g.ifs:
                    self.nested_free(c, bound)
            if isinstance(n, ast.DictComp):
                self.nested_free(n.key, bound)
                self.nested_free(n.value, bound)
            else:
                self.nested_free(n.elt, bound)
            return
        for c in ast.iter_child_nodes(n):
            if isinstance(c, ast.expr):
                self.expr(c)
            elif isinstance(c, ast.keyword):
                self.expr(c.value)
            elif isinstance(c, ast.comprehension):
                pass
            else:
                for cc in ast.walk(c):
                    if isinstance(cc, ast.expr):
                        self.expr(cc)
                        break

    def stmts(self, lst):
        for s in lst:
            self.stmt(s)

    def stmt(self, s):
        if isinstance(s, ast.Assign):
            self.expr(s.value)
            for t in s.targets:
                self.target(t)
        elif isinstance(s, ast.AugAssign):
            if isinstance(s.target, ast.Name):
                self.load(s.target.id, s.target)
            else:
                self.expr(s.target)
            self.expr(s.value)
            if isinstance(s.target, ast.Name):
                self.store(s.target.id, s.target)
        elif isinstance(s, ast.AnnAssign):
            self.expr(s.value)
            if s.value is not None or not isinstance(s.target, ast.Name):
                self.target(s.target)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            self.expr(s.iter)
            self.target(s.target)
            self.stmts(s.body)
            self.stmts(s.orelse)
        elif isinstance(s, (ast.While, ast.If)):
            self.expr(s.test)
            self.stmts(s.body)
            self.stmts(s.orelse)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for it in s.items:
                self.expr(it.context_expr)
                if it.optional_vars is not None:
                    self.target(it.optional_vars)
            self.stmts(s.body)
        elif isinstance(s, ast.Try) or (hasattr(ast, 'TryStar') and isinstance(s, ast.TryStar)):
            self.stmts(s.body)
            for h in s.handlers:
                self.expr(h.type)
                if h.name:
                    self.store(h.name, h)
                self.stmts(h.body)
            self.stmts(s.orelse)
            self.stmts(s.finalbody)
        elif isinstance(s, FUNC_TYPES):
            for d in s.decorator_list:
                self.expr(d)
            a = s.args
            for d in a.defaults + [x for x in a.kw_defaults if x is not None]:
                self.expr(d)
            bound = set(p.arg for p in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
                        if p is not None)
            for st in s.body:
                self.nested_free(st, bound)
            self.store(s.name, s)
        elif isinstance(s, ast.ClassDef):
            for d in s.decorator_list:
                self.expr(d)
            for e in s.bases:
                self.expr(e)
            for k in s.keywords:
                self.expr(k.value)
            for st in s.body:
                self.nested_free(st, set())
            self.store(s.name, s)
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            for al in s.names:
                if al.name == '*':
                    continue
                nm = al.asname or al.name.split('.')[0]
                self.store(nm, s)
        elif isinstance(s, (ast.Global, ast.Nonlocal)):
            pass
        elif isinstance(s, ast.Delete):
            for t in s.targets:
                if not isinstance(t, ast.Name):
                    self.expr(t)
        elif hasattr(ast, 'Match') and isinstance(s, ast.Match):
            self.expr(s.subject)
            for c in s.cases:
                for n in ast.walk(c.pattern):
                    if isinstance(n, ast.expr):
                        self.expr(n)
                    nm = getattr(n, 'name', None) if not isinstance(n, ast.expr) else None
                    if isinstance(nm, str):
                        self.store(nm, n)
                    if isinstance(n, ast.MatchMapping) and n.rest:
                        self.store(n.rest, n)
                self.expr(c.guard)
                self.stmts(c.body)
        else:
            for c in ast.iter_child_nodes(s):
                if isinstance(c, ast.expr):
                    self.expr(c)


def _walk_no_scopes(nodes):
    """Walk nodes without descending into nested functions/classes/lambdas."""
    stack = list(nodes)
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if isinstance(c, FUNC_TYPES + (ast.ClassDef, ast.Lambda)):
                continue
            stack.append(c)


def _func_locals(fn):
    """Names local to function fn (parameters and assigned names)."""
    a = fn.args
    out = set(p.arg for p in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if p is not None)
    declared = set()
    for n in _walk_no_scopes(fn.body):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            declared.update(n.names)
    flow = _NameFlow()
    flow.stmts(fn.body)
    for kind, nm, node in flow.events:
        if kind == 'store':
            out.add(nm)
    return out - declared


def _module_names(tree):
    flow = _NameFlow()
    flow.stmts(tree.body)
    return set(nm for kind, nm, node in flow.events if kind == 'store')


def _deco_name(d):
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Attribute):
        return d.attr
    return None


def extract_function(path, line_no, col, until, name):
    _check_ident(name)
    doc = _Doc(path)
    tree = _strict_parse(doc)
    a, b = _selection(doc, line_no, col, until)
    parents = _parents_of(tree)
    stmts, body, first_a, last_b = _find_selected_stmts(doc, tree, a, b)
    # Enclosing function / class.
    container = None
    p = parents.get(stmts[0])
    while p is not None:
        if isinstance(p, FUNC_TYPES + (ast.ClassDef,)):
            container = p
            break
        p = parents.get(p)
    sel_nodes = set()
    for st in stmts:
        for n in ast.walk(st):
            sel_nodes.add(n)
    # Control-flow checks.
    last = stmts[-1]
    ends_with_return = isinstance(last, ast.Return)
    loops_inside = set()
    for n in _walk_no_scopes(stmts):
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            for c in _walk_no_scopes(n.body + n.orelse):
                loops_inside.add(c)
    for n in _walk_no_scopes(stmts):
        if isinstance(n, ast.Return) and not (ends_with_return and n is last):
            raise RefactorError('cannot extract a return statement that is not the last selected statement')
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            raise RefactorError('cannot extract code containing yield')
        if isinstance(n, (ast.Break, ast.Continue)) and n not in loops_inside:
            raise RefactorError('cannot extract break/continue outside of a selected loop')
    is_async = any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in _walk_no_scopes(stmts))
    # Which names are "outside" variables?
    fn = container if isinstance(container, FUNC_TYPES) else None
    if fn is not None:
        outer = _func_locals(fn)
    elif container is None:
        outer = _module_names(tree)
    else:
        outer = set()
        f2 = _NameFlow()
        f2.stmts(container.body)
        outer = set(nm for k, nm, nd in f2.events if k == 'store')
    flow = _NameFlow()
    flow.stmts(stmts)
    assigned = set()
    param_first = {}
    stored_order = []
    for kind, nm, node in flow.events:
        if kind == 'load':
            if nm in assigned or nm in param_first:
                continue
            if nm in outer or (container is None and not hasattr(builtins, nm)):
                pos = (getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))
                param_first[nm] = pos
        else:
            assigned.add(nm)
            if nm not in stored_order:
                stored_order.append(nm)
    # first appearance in source order
    first_seen = {}
    for n in ast.walk(ast.Module(body=list(stmts), type_ignores=[])):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in param_first:
            pos = (n.lineno, n.col_offset)
            if n.id not in first_seen or pos < first_seen[n.id]:
                first_seen[n.id] = pos
    params = sorted(param_first, key=lambda k: first_seen.get(k, param_first[k]))
    # Return values: assigned here and read after the selection.
    scope_root = fn if fn is not None else (container if container is not None else tree)
    after = set()
    sel_end = (stmts[-1].end_lineno, stmts[-1].end_col_offset)
    for n in ast.walk(scope_root):
        if n in sel_nodes:
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and (n.lineno, n.col_offset) > sel_end:
            after.add(n.id)
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and \
                (n.lineno, n.col_offset) > sel_end:
            after.add(n.target.id)
    # Loops: names read earlier in an enclosing loop body are also live.
    p = parents.get(stmts[0])
    while p is not None and p is not scope_root:
        if isinstance(p, (ast.For, ast.AsyncFor, ast.While)):
            for n in ast.walk(p):
                if n not in sel_nodes and isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    after.add(n.id)
        p = parents.get(p)
    returns = [] if ends_with_return else [nm for nm in stored_order if nm in after]
    # Method handling.
    self_name = None
    decorator = ''
    call_prefix = ''
    if fn is not None and isinstance(parents.get(fn), ast.ClassDef):
        cls = parents.get(fn)
        decos = [_deco_name(d) for d in fn.decorator_list]
        allp = fn.args.posonlyargs + fn.args.args
        if 'staticmethod' in decos:
            decorator = '@staticmethod'
            call_prefix = cls.name + '.'
        elif allp:
            self_name = allp[0].arg
            if 'classmethod' in decos:
                decorator = '@classmethod'
            call_prefix = self_name + '.'
            params = [x for x in params if x != self_name]
        else:
            decorator = '@staticmethod'
            call_prefix = cls.name + '.'
    # Source of the selection.
    code = blank_code(doc.text)
    first_line = doc.pos_of(first_a)[0]
    last_line = stmts[-1].end_lineno
    orig_lines = [doc.line(ln) for ln in range(first_line, last_line + 1)]
    orig_indent = doc.line(first_line)[:leading_ws(doc.line(first_line))]
    # Lines that lie inside multi-line strings must not be re-indented.
    in_string = set()
    for n in ast.walk(ast.Module(body=list(stmts), type_ignores=[])):
        if isinstance(n, (ast.Constant, ast.JoinedStr)) and isinstance(getattr(n, 'value', None), (str, bytes, list)) \
                and n.end_lineno > n.lineno:
            for ln in range(n.lineno + 1, n.end_lineno + 1):
                in_string.add(ln)
    if fn is not None or container is not None:
        ins_node = container
        ins_line = ins_node.lineno
        if getattr(ins_node, 'decorator_list', None):
            ins_line = min([ins_line] + [d.lineno for d in ins_node.decorator_list])
        ins_indent = doc.line(ins_line)[:leading_ws(doc.line(ins_line))]
    else:
        ins_line = first_line
        ins_indent = orig_indent
    unit = '\t' if orig_indent.startswith('\t') or ins_indent.startswith('\t') else '    '
    body_indent = ins_indent + unit
    new_body = []
    for i, l in enumerate(orig_lines):
        ln = first_line + i
        if ln in in_string:
            new_body.append(l)
        elif not l.strip():
            new_body.append('')
        elif l.startswith(orig_indent):
            new_body.append(body_indent + l[len(orig_indent):])
        else:
            new_body.append(body_indent + l.lstrip())
    if returns:
        new_body.append(body_indent + 'return ' + ', '.join(returns))
    sig = ([self_name] if self_name else []) + params
    head = []
    if decorator:
        head.append(ins_indent + decorator)
    head.append('%s%sdef %s(%s):' % (ins_indent, 'async ' if is_async else '', name, ', '.join(sig)))
    blank = '\n\n' if ins_indent == '' else '\n'
    func_text = '\n'.join(head + new_body) + '\n' + blank
    call = '%s%s(%s)' % (call_prefix, name, ', '.join(params))
    if is_async:
        call = 'await ' + call
    if ends_with_return:
        call_line = orig_indent + 'return ' + call
    elif returns:
        call_line = orig_indent + ', '.join(returns) + ' = ' + call
    else:
        call_line = orig_indent + call
    rs = doc.line_start(first_line)
    re_ = doc.off(last_line, len(doc.line(last_line)))
    docs = {doc.path: doc}
    if container is None:
        doc.replace(rs, re_, func_text + call_line)
    else:
        il = doc.line_start(ins_line)
        doc.replace(il, il, func_text)
        doc.replace(rs, re_, call_line)
    return refactor_result(docs)


# --- syntax errors ------------------------------------------------------------

def _syntax_error_entry(e, lines):
    ln = getattr(e, 'lineno', None) or 1
    off = getattr(e, 'offset', None)
    col = max(0, (off or 1) - 1)
    eln = getattr(e, 'end_lineno', None)
    eoff = getattr(e, 'end_offset', None)
    if not eln or eln < ln:
        eln = ln
    ecol = (eoff - 1) if eoff and eoff > 0 else None
    if ecol is None or (eln == ln and ecol <= col):
        eln = ln
        ecol = col + 1
    msg = getattr(e, 'msg', None) or str(e)
    return {'line': ln, 'column': col, 'until_line': eln, 'until_column': ecol, 'message': msg}


def _first_syntax_error(text, path):
    with warnings_off():
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as e:
            return e
        except (ValueError, MemoryError, RecursionError) as e:
            err = SyntaxError(str(e))
            err.lineno, err.offset = 1, 1
            return err
        try:
            compile(tree, path, 'exec', dont_inherit=True)
        except SyntaxError as e:
            return e
        except Exception:
            pass
    return None


@contextlib.contextmanager
def warnings_off():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        yield


def syntax_errors(path):
    text = read_source(path)
    lines = [l.rstrip('\r') for l in text.split('\n')]
    errors = []
    work = list(lines)
    last_line = 0
    for _ in range(20):
        e = _first_syntax_error('\n'.join(work), path)
        if e is None:
            break
        ent = _syntax_error_entry(e, lines)
        if errors and ent['line'] <= last_line:
            break
        errors.append(ent)
        last_line = ent['until_line']
        # Neutralise the offending lines and look for further errors.
        lo, hi = ent['line'], ent['until_line']
        if isinstance(e, IndentationError) or lo < 1 or lo > len(work):
            break
        ind = leading_ws(work[lo - 1])
        nxt = hi
        while nxt < len(work) and not work[nxt].strip():
            nxt += 1
        opens_block = nxt < len(work) and leading_ws(work[nxt]) > ind
        work[lo - 1] = work[lo - 1][:ind] + ('if 1:' if opens_block else 'pass')
        for k in range(lo, min(hi, len(work))):
            work[k] = ''
    if len(errors) > 1:
        # Only keep follow-up errors that are not artefacts of an unclosed bracket etc.
        errors = errors[:1] + [x for x in errors[1:] if x['line'] > errors[0]['until_line']]
    return errors


USAGE = ('usage: sith.py complete <file> <line> <col> [--fuzzy] [--project DIR]\n'
         '       sith.py infer <file> <line> <col> [--project DIR]\n'
         '       sith.py goto <file> <line> <col> [--follow-imports] [--project DIR]\n'
         '       sith.py signatures <file> <line> <col> [--project DIR]\n'
         '       sith.py references <file> <line> <col> [--scope file|project] [--project DIR]\n'
         '       sith.py search <query> [--project DIR]\n'
         '       sith.py names <file> [--all-scopes] [--project DIR]\n'
         '       sith.py rename <file> <line> <col> --new-name NAME [--diff] [--project DIR]\n'
         '       sith.py inline <file> <line> <col> [--diff] [--project DIR]\n'
         '       sith.py extract-variable <file> <line> <col> --until L:C --name NAME [--diff] [--project DIR]\n'
         '       sith.py extract-function <file> <line> <col> --until L:C --name NAME [--diff] [--project DIR]\n'
         '       sith.py errors <file>\n')

_ARITY = {'complete': 4, 'infer': 4, 'goto': 4, 'signatures': 4, 'references': 4,
          'search': 2, 'names': 2, 'rename': 4, 'inline': 4, 'extract-variable': 4,
          'extract-function': 4, 'errors': 2}

_VALUE_OPTS = ('--project', '--scope', '--new-name', '--name', '--until')


def _emit(obj):
    data = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
    out = sys.stdout
    try:
        out.buffer.write((data + '\n').encode('utf-8'))
        out.flush()
    except AttributeError:
        out.write(data + '\n')


def main(argv):
    global _PROJECT_ROOT
    args = list(argv)
    fuzzy = False
    project = None
    scope = 'file'
    all_scopes = False
    diff = False
    opts = {}
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--fuzzy':
            fuzzy = True
        elif a == '--follow-imports':
            _FOLLOW[0] = True
        elif a == '--all-scopes':
            all_scopes = True
        elif a == '--diff':
            diff = True
        elif a in _VALUE_OPTS:
            if i + 1 >= len(args):
                sys.stderr.write(USAGE)
                return 1
            opts[a] = args[i + 1]
            i += 1
        elif '=' in a and a.split('=', 1)[0] in _VALUE_OPTS:
            k, v = a.split('=', 1)
            opts[k] = v
        else:
            rest.append(a)
        i += 1
    args = rest
    project = opts.get('--project', project)
    scope = opts.get('--scope', scope)
    if not args or args[0] not in _ARITY or len(args) != _ARITY[args[0]]:
        sys.stderr.write(USAGE)
        return 1
    if scope not in ('file', 'project'):
        sys.stderr.write('error: --scope must be "file" or "project"\n')
        return 1
    cmd = args[0]
    line_no = col = None
    if _ARITY[cmd] == 4:
        try:
            line_no = int(args[2])
            col = int(args[3])
        except ValueError:
            sys.stderr.write('error: line and column must be integers\n')
            return 1
    if project:
        if not os.path.isdir(project):
            sys.stderr.write('error: project directory does not exist: %s\n' % project)
            return 1
        _PROJECT_ROOT = os.path.abspath(project)
    elif cmd == 'search':
        _PROJECT_ROOT = os.getcwd()
    else:
        _PROJECT_ROOT = os.path.dirname(os.path.abspath(args[1]))
    try:
        if cmd == 'complete':
            _emit({'completions': complete(args[1], line_no, col, fuzzy)})
        elif cmd in ('infer', 'goto'):
            _emit({'definitions': navigate_file(args[1], line_no, col, cmd)})
        elif cmd == 'signatures':
            _emit({'signatures': signatures(args[1], line_no, col)})
        elif cmd == 'references':
            _emit({'references': references(args[1], line_no, col, scope)})
        elif cmd == 'search':
            _emit({'definitions': search(args[1])})
        elif cmd == 'names':
            _emit({'definitions': names(args[1], all_scopes)})
        elif cmd == 'errors':
            _emit({'errors': syntax_errors(args[1])})
        else:
            if cmd == 'rename':
                if '--new-name' not in opts:
                    raise RefactorError('--new-name is required')
                res = rename(args[1], line_no, col, opts['--new-name'])
            elif cmd == 'inline':
                res = inline(args[1], line_no, col)
            else:
                if '--name' not in opts:
                    raise RefactorError('--name is required')
                if '--until' not in opts:
                    raise RefactorError('--until is required')
                _check_ident(opts['--name'])
                until = _parse_until(opts['--until'])
                fn = extract_variable if cmd == 'extract-variable' else extract_function
                res = fn(args[1], line_no, col, until, opts['--name'])
            obj, text = res
            if diff:
                out = sys.stdout
                try:
                    out.buffer.write(text.encode('utf-8'))
                    out.flush()
                except AttributeError:
                    out.write(text)
            else:
                _emit(obj)
    except NotAName:
        sys.stderr.write('error: no name at cursor position\n')
        return 1
    except SithError as e:
        sys.stderr.write('error: %s\n' % e)
        return 1
    return 0


if __name__ == '__main__':
    sys.setrecursionlimit(10000)
    try:
        code = main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover - last-resort safety net
        sys.stderr.write('error: %s\n' % e)
        code = 1
    sys.exit(code)
