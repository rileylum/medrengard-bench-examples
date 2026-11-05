#!/usr/bin/env python3
"""sith - a small static Python code-intelligence / completion tool.

Usage:
    python sith.py complete <file> <line> <col> [--fuzzy]
    python sith.py infer <file> <line> <col>
    python sith.py goto <file> <line> <col>
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
            return [n for n in mv.local.top_names() if not n.startswith('_')]
        if mv.real is not None:
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


def load_local_module(path, name):
    path = os.path.abspath(path)
    if path in _local_modules:
        return _local_modules[path]
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8')
    except Exception:
        _local_modules[path] = None
        return None
    if text.startswith('﻿'):
        text = text[1:]
    mi = ModuleInfo(path, name)
    _local_modules[path] = mi
    lines = [l.rstrip('\r') for l in text.split('\n')]
    tree, _, lines = fix_and_parse(lines)
    mi.lines = lines
    mi.set_tree(tree)
    return mi


class ModuleInfo(object):
    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.dir = os.path.dirname(os.path.abspath(path))
        self.is_package = os.path.basename(path) == '__init__.py'
        self.tree = None
        self.scope = None
        self.node_scope = {}
        self.lines = []
        self._by_node = None

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
        return list(self.scope.visible(None).keys())

    def attr_defs(self, name):
        return reaching_defs(self.scope, name, INF_POS, allow_tainted=True)

    def attr(self, name):
        ds = self.attr_defs(name)
        if ds:
            out = []
            for d in ds:
                out.extend(infer_def_vals(d))
            return True, uniq(out)
        # submodule of a local package
        if self.is_package:
            mv = self._local_module(self.dir, name, name)
            if mv is not None:
                return True, [mv]
        return False, []

    def _local_module(self, base, dotted, display):
        parts = dotted.split('.') if dotted else []
        p = base
        for i, part in enumerate(parts):
            cand_dir = os.path.join(p, part)
            cand_file = os.path.join(p, part + '.py')
            last = i == len(parts) - 1
            if last:
                if os.path.isfile(cand_file):
                    mi = load_local_module(cand_file, dotted)
                    return ModVal(display, local=mi) if mi else None
                init = os.path.join(cand_dir, '__init__.py')
                if os.path.isfile(init):
                    mi = load_local_module(init, dotted)
                    return ModVal(display, local=mi) if mi else None
                return None
            if not os.path.isdir(cand_dir):
                return None
            p = cand_dir
        if not parts:
            init = os.path.join(base, '__init__.py')
            if os.path.isfile(init):
                mi = load_local_module(init, os.path.basename(base))
                return ModVal(display, local=mi) if mi else None
        return None

    def resolve_import(self, dotted, level, display=None):
        display = display or dotted
        if level:
            base = self.dir
            for _ in range(level - 1):
                base = os.path.dirname(base)
            return self._local_module(base, dotted, display)
        mv = self._local_module(self.dir, dotted, display)
        if mv is not None:
            return mv
        if _PROJECT_ROOT and os.path.abspath(_PROJECT_ROOT) != self.dir:
            mv = self._local_module(os.path.abspath(_PROJECT_ROOT), dotted, display)
            if mv is not None:
                return mv
        mod = real_import(dotted)
        if mod is not None:
            return ModVal(display, real=mod)
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
        if par.annotation is not None:
            v = annotation_vals(par.annotation, s.parent, (fnode.lineno, fnode.col_offset))
            if v:
                return v
        if env is not None and d.name in env:
            v = env[d.name]()
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
        return return_vals(f, mapping)
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


def return_vals(f, mapping=None):
    node = f.node
    cnt = _ret_active.get(id(node), 0)
    if cnt >= 2 or sum(_ret_active.values()) > 30:
        _cuts[0] += 1
        return []
    _ret_active[id(node)] = cnt + 1
    try:
        with pushed_env(node, mapping):
            return uniq(_return_vals(f, mapping))
    except RecursionError:
        return []
    finally:
        _ret_active[id(node)] -= 1
        if not _ret_active[id(node)]:
            del _ret_active[id(node)]


def _return_vals(f, mapping):
    node = f.node
    if isinstance(node, ast.Lambda):
        fs = f.scope.module.node_scope.get(node) if f.scope else None
        if fs is None:
            return []
        return infer_vals(node.body, fs, INF_POS)
    if f.scope is None:
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
    for c in reversed(class_mro(cls)):
        cs = class_scope_of(c)
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
            for e in ents:
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
            for e in m[name]:
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
    path = os.path.abspath(mi.path)
    root = _root()
    rel = os.path.relpath(path, root)
    if rel.startswith('..'):
        if mi.name and mi.name != '__main__':
            return mi.name
        rel = os.path.basename(path)
    if rel.endswith('.py'):
        rel = rel[:-3]
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
    rel = os.path.relpath(path, _root())
    if rel.startswith('..'):
        return path
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
    line, bcol = defnode_name_pos(mi, n)
    desc = 'class %s' % n.name if typ == 'class' else 'instance of %s' % n.name
    return mk(n.name, typ, full_name_for(s, n.name), rel_module_path(mi.path), line,
              _char_col(mi, line, bcol), desc, _docstring(n))


def func_out(fv):
    n = fv.node
    s = fv.scope
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


def def_out(d):
    """Output dict(s) for a definition site (goto)."""
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
            ds = v.local.attr_defs(name)
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
        for kind, x, c in m.get(name, ()):
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
        if ln is None or not (ln <= line_no <= eln):
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
        if mode == 'goto':
            if mv is not None:
                outs = attr_def_outs(mv, a.name)
                if outs:
                    return outs
            sub = ((stmt.module + '.') if stmt.module else '') + a.name
            mv2 = mi.resolve_import(sub, level)
            return [module_out(mv2)] if mv2 is not None else []
        ds = [d for d in mi.defs_by_node(stmt) if d.name == (a.asname or a.name)]
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


USAGE = ('usage: sith.py complete <file> <line> <col> [--fuzzy] [--project DIR]\n'
         '       sith.py infer <file> <line> <col> [--project DIR]\n'
         '       sith.py goto <file> <line> <col> [--project DIR]\n')


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
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--fuzzy':
            fuzzy = True
        elif a == '--project':
            if i + 1 >= len(args):
                sys.stderr.write(USAGE)
                return 1
            project = args[i + 1]
            i += 1
        elif a.startswith('--project='):
            project = a.split('=', 1)[1]
        else:
            rest.append(a)
        i += 1
    args = rest
    if len(args) != 4 or args[0] not in ('complete', 'infer', 'goto'):
        sys.stderr.write(USAGE)
        return 1
    cmd, path, line_s, col_s = args
    try:
        line_no = int(line_s)
        col = int(col_s)
    except ValueError:
        sys.stderr.write('error: line and column must be integers\n')
        return 1
    if project:
        if not os.path.isdir(project):
            sys.stderr.write('error: project directory does not exist: %s\n' % project)
            return 1
        _PROJECT_ROOT = os.path.abspath(project)
    else:
        _PROJECT_ROOT = os.path.dirname(os.path.abspath(path))
    try:
        if cmd == 'complete':
            comps = complete(path, line_no, col, fuzzy)
            _emit({'completions': comps})
            return 0
        defs = navigate_file(path, line_no, col, cmd)
    except NotAName:
        sys.stderr.write('error: no name at cursor position\n')
        return 1
    except SithError as e:
        sys.stderr.write('error: %s\n' % e)
        return 1
    _emit({'definitions': defs})
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
