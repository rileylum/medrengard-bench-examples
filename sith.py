#!/usr/bin/env python3
"""sith - a small static Python code-intelligence / completion tool.

Usage:
    python sith.py complete <file> <line> <col> [--fuzzy]
"""
import ast
import builtins
import contextlib
import importlib
import inspect
import io
import json
import keyword
import os
import sys
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
    def __init__(self, cls):
        self.cls = cls


class FuncVal(object):
    def __init__(self, node, scope):
        self.node = node
        self.scope = scope  # defining scope


class RealVal(object):
    def __init__(self, obj):
        self.obj = obj


class RealInst(object):
    def __init__(self, typ):
        self.typ = typ


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
        return True, SAMPLES[typ]
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
        return ('mod', v.name)
    if isinstance(v, RealVal):
        return ('obj', id(v.obj))
    return ('?', id(v))


def same_val(vals):
    if not vals:
        return None
    k = val_key(vals[0])
    if k is None:
        return None
    for v in vals[1:]:
        if val_key(v) != k:
            return None
    return vals[0]


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
        if inspect.isroutine(obj) or isinstance(obj, (types.MethodType, types.BuiltinMethodType)) \
                or type(obj).__name__ in ('method-wrapper', 'wrapper_descriptor',
                                          'method_descriptor', 'classmethod_descriptor'):
            return 'function', 'def %s(...)' % name
        return 'instance', 'instance of %s' % type(obj).__name__
    return 'statement', 'statement'


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
    'locals': dict, 'any': bool, 'all': bool,
}


# ---------------------------------------------------------------------------
# Module analysis & inference
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
    tree, _, _ = fix_and_parse(lines)
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

    def set_tree(self, tree):
        self.tree = tree
        b = ScopeBuilder(self)
        self.scope = b.build_module(tree)
        self.node_scope = b.node_scope

    def top_names(self):
        return list(self.scope.visible(None).keys())

    def attr(self, name):
        d = self.scope.best(name)
        if d is not None:
            return True, infer_def(d)
        # submodule of a local package
        if self.is_package:
            mv = self._local_module(self.dir, name, name)
            if mv is not None:
                return True, mv
        return False, None

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
        mod = real_import(dotted)
        if mod is not None:
            return ModVal(display, real=mod)
        return None


_infer_stack = set()


def infer_def(d, depth=0):
    if d is None:
        return None
    key = id(d)
    if key in _infer_stack or len(_infer_stack) > 60:
        return None
    _infer_stack.add(key)
    try:
        return _infer_def(d)
    except RecursionError:
        return None
    finally:
        _infer_stack.discard(key)


def builtin_value(name):
    ok, obj = safe_getattr(builtins, name)
    if ok:
        return wrap_real(obj, name)
    return None


def _infer_def(d):
    if isinstance(d, StarHit):
        mv = d.star.module_value()
        if mv is None:
            return None
        return attr_value(mv, d.name)[1]
    if d == 'builtin':
        return None
    s = d.scope
    k = d.kind
    if k == 'def':
        return FuncVal(d.node, s)
    if k == 'class':
        return ClassVal(d.node, s)
    if k == 'param':
        fnode, idx, pkind = d.extra
        par = d.node
        if pkind == 'pos' and idx == 0 and isinstance(fnode, FUNC_TYPES) \
                and s.parent is not None and s.parent.kind == 'class':
            decos = [dec.id if isinstance(dec, ast.Name) else getattr(dec, 'attr', None)
                     for dec in fnode.decorator_list]
            cdef = s.parent.parent and s.parent.parent  # scope defining the class
            cls = ClassVal(s.parent.node, s.parent.parent)
            if 'staticmethod' in decos:
                pass
            elif 'classmethod' in decos or fnode.name in ('__new__', '__init_subclass__',
                                                          '__class_getitem__'):
                return cls
            else:
                return InstVal(cls)
        if pkind == 'vararg':
            return RealInst(tuple)
        if pkind == 'kwarg':
            return RealInst(dict)
        if par.annotation is not None:
            v = infer_annotation(par.annotation, s.parent, (fnode.lineno, fnode.col_offset))
            if v is not None:
                return v
        return None
    if k == 'import':
        name, asname = d.extra
        mi = s.module
        if asname:
            mv = mi.resolve_import(name, 0, display=asname)
            if mv is None:
                return ModVal(asname)
            return mv
        top = name.split('.')[0]
        if name != top:
            mi.resolve_import(name, 0)  # make sure submodules are loaded
        mv = mi.resolve_import(top, 0)
        if mv is None:
            return ModVal(top)
        return mv
    if k == 'importfrom':
        modname, name, level = d.extra
        mi = s.module
        mv = mi.resolve_import(modname, level)
        if mv is not None:
            ok, v = attr_value(mv, name)
            if ok:
                return v
        sub = (modname + '.' + name) if modname else name
        mv2 = mi.resolve_import(sub, level)
        if mv2 is not None:
            return mv2
        return None
    ctx_pos = d.pos
    if k in ('assign', 'with', 'for'):
        value, path, stmt = d.extra
        if k == 'assign':
            return infer_path(value, path, s, ctx_pos)
        if k == 'with':
            v = infer_path_ctx(value, path, s, ctx_pos)
            return enter_value(v)
        if k == 'for':
            if path:
                return None
            return element_of(value, s, ctx_pos)
    if k == 'annassign':
        value, ann, stmt = d.extra
        if value is not None:
            v = infer(value, s, ctx_pos)
            if v is not None:
                return v
        return infer_annotation(ann, s, ctx_pos)
    if k == 'augassign':
        n = d.extra
        return infer(ast.BinOp(left=ast.Name(id=n.target.id, ctx=ast.Load()), op=n.op, right=n.value),
                     s, ctx_pos)
    if k == 'walrus':
        return infer(d.extra, s, ctx_pos)
    if k == 'except':
        t = d.extra
        if t is None:
            return None
        v = infer(t, s, ctx_pos)
        return instantiate(v)
    if k == 'comp':
        iter_node, path = d.extra
        if path:
            return None
        return element_of(iter_node, s.parent if s.parent else s, INF_POS)
    return None


def infer_path_ctx(value, path, s, pos):
    if path:
        return None
    return infer(value, s, pos)


def infer_path(value, path, s, pos):
    if not path:
        return infer(value, s, pos)
    node = value
    for p in path:
        if p is None:
            return RealInst(list)
        if isinstance(node, (ast.Tuple, ast.List)) and not any(isinstance(e, ast.Starred) for e in node.elts):
            if p < len(node.elts):
                node = node.elts[p]
                continue
        return None
    return infer(node, s, pos)


def enter_value(v):
    if v is None:
        return None
    if isinstance(v, InstVal):
        ok, m = attr_value(v, '__enter__')
        if ok and isinstance(m, FuncVal):
            r = call_value(m)
            if r is not None:
                return r
        return v
    if isinstance(v, RealInst):
        return v
    return None


def instantiate(v):
    if isinstance(v, ClassVal):
        return InstVal(v)
    if isinstance(v, RealVal) and inspect.isclass(v.obj):
        return RealInst(v.obj)
    return None


def infer_annotation(ann, s, pos):
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode='eval').body
        except SyntaxError:
            return None
    if isinstance(ann, ast.Constant) and ann.value is None:
        return RealInst(type(None))
    if isinstance(ann, ast.Subscript):
        ann = ann.value
    v = infer(ann, s, INF_POS)
    return instantiate(v)


def element_of(iter_node, s, pos):
    if isinstance(iter_node, (ast.List, ast.Tuple, ast.Set)):
        if not iter_node.elts or any(isinstance(e, ast.Starred) for e in iter_node.elts):
            return None
        return same_val([infer(e, s, pos) for e in iter_node.elts])
    if isinstance(iter_node, ast.Dict):
        if not iter_node.keys or any(k is None for k in iter_node.keys):
            return None
        return same_val([infer(e, s, pos) for e in iter_node.keys])
    if isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name) \
            and iter_node.func.id == 'range':
        fv = infer(iter_node.func, s, pos)
        if isinstance(fv, RealVal) and fv.obj is range:
            return RealInst(int)
    v = infer(iter_node, s, pos)
    if isinstance(v, RealInst):
        if v.typ is str:
            return RealInst(str)
        if v.typ in (bytes, bytearray, range):
            return RealInst(int)
    if isinstance(v, RealVal) and isinstance(v.obj, str):
        return RealInst(str)
    return None


def lookup(name, scope, pos, line=None):
    if name == PLACEHOLDER:
        return None
    s = scope
    first = True
    while s is not None:
        if first or s.kind != 'class':
            d = s.best(name, pos=pos, line=line, allow_tainted=False)
            if d is not None:
                return d
        first = False
        s = s.parent
    return None


def infer_name(name, scope, pos):
    d = lookup(name, scope, pos)
    if d is not None:
        return infer_def(d)
    if name == PLACEHOLDER:
        return None
    return builtin_value(name)


_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b, ast.BitAnd: lambda a, b: a & b, ast.BitOr: lambda a, b: a | b,
    ast.BitXor: lambda a, b: a ^ b, ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
}


def call_value(f, node=None, s=None, pos=None):
    if f is None:
        return None
    if isinstance(f, ClassVal):
        return InstVal(f)
    if isinstance(f, RealVal):
        obj = f.obj
        if inspect.isclass(obj):
            if obj is type or obj is super:
                return None
            return RealInst(obj)
        for bname, rtype in BUILTIN_RETURNS.items():
            ok, bobj = safe_getattr(builtins, bname)
            if ok and bobj is obj:
                return RealInst(rtype)
        return None
    if isinstance(f, FuncVal):
        return infer_return(f)
    return None


_ret_stack = set()


def infer_return(f):
    node = f.node
    if id(node) in _ret_stack:
        return None
    _ret_stack.add(id(node))
    try:
        if isinstance(node, ast.Lambda):
            fs = f.scope.module.node_scope.get(node) if f.scope else None
            if fs is None:
                return None
            return infer(node.body, fs, INF_POS)
        if node.returns is not None:
            v = infer_annotation(node.returns, f.scope, INF_POS)
            if v is not None:
                return v
        fs = f.scope.module.node_scope.get(node)
        if fs is None:
            return None
        rets = []
        has_yield = [False]

        def walk(n):
            for c in ast.iter_child_nodes(n):
                if isinstance(c, FUNC_TYPES + (ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(c, (ast.Yield, ast.YieldFrom)):
                    has_yield[0] = True
                if isinstance(c, ast.Return):
                    rets.append(c)
                walk(c)
        for st in node.body:
            if isinstance(st, ast.Return):
                rets.append(st)
            walk(st)
        if has_yield[0]:
            return RealInst(types.GeneratorType)
        if isinstance(node, ast.AsyncFunctionDef):
            return None
        vals = []
        for r in rets:
            if r.value is None:
                vals.append(RealInst(type(None)))
            else:
                vals.append(infer(r.value, fs, (r.lineno, r.col_offset)))
        if not rets:
            return RealInst(type(None))
        return same_val(vals)
    finally:
        _ret_stack.discard(id(node))


def infer(node, s, pos):
    try:
        return _infer(node, s, pos)
    except RecursionError:
        return None


def _infer(node, s, pos):
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return RealInst(type(node.value))
    if isinstance(node, ast.JoinedStr):
        return RealInst(str)
    if isinstance(node, (ast.List, ast.ListComp)):
        return RealInst(list)
    if isinstance(node, ast.Tuple):
        return RealInst(tuple)
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return RealInst(dict)
    if isinstance(node, (ast.Set, ast.SetComp)):
        return RealInst(set)
    if isinstance(node, ast.GeneratorExp):
        return RealInst(types.GeneratorType)
    if isinstance(node, ast.Lambda):
        return FuncVal(node, s)
    if isinstance(node, ast.Name):
        return infer_name(node.id, s, pos)
    if isinstance(node, ast.NamedExpr):
        return infer(node.value, s, pos)
    if isinstance(node, ast.Attribute):
        if node.attr == PLACEHOLDER:
            return None
        v = infer(node.value, s, pos)
        if v is None:
            return None
        return attr_value(v, node.attr)[1]
    if isinstance(node, ast.Call):
        f = infer(node.func, s, pos)
        return call_value(f, node, s, pos)
    if isinstance(node, ast.Compare):
        return RealInst(bool)
    if isinstance(node, ast.BoolOp):
        return same_val([infer(v, s, pos) for v in node.values])
    if isinstance(node, ast.IfExp):
        return same_val([infer(node.body, s, pos), infer(node.orelse, s, pos)])
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return RealInst(bool)
        v = infer(node.operand, s, pos)
        if isinstance(v, RealInst) and v.typ in (int, float, complex, bool):
            if v.typ is bool:
                return RealInst(int)
            return v
        return None
    if isinstance(node, ast.BinOp):
        lv = infer(node.left, s, pos)
        rv = infer(node.right, s, pos)
        if isinstance(lv, RealInst) and lv.typ in (str, bytes) and isinstance(node.op, ast.Mod):
            return lv
        if isinstance(lv, RealInst) and isinstance(rv, RealInst):
            ok1, a = sample_of(lv.typ)
            ok2, b = sample_of(rv.typ)
            fn = _OPS.get(type(node.op))
            if ok1 and ok2 and fn is not None:
                try:
                    return RealInst(type(fn(a, b)))
                except Exception:
                    return None
        return None
    if isinstance(node, ast.Subscript):
        v = infer(node.value, s, pos)
        if isinstance(v, RealVal) and not inspect.isclass(v.obj) and type(v.obj) in SAMPLES:
            v = RealInst(type(v.obj))
        if isinstance(v, RealInst):
            if isinstance(node.slice, ast.Slice):
                if v.typ in (str, bytes, bytearray, list, tuple):
                    return v
                return None
            if v.typ is str:
                return v
            if v.typ in (bytes, bytearray):
                return RealInst(int)
        return None
    if isinstance(node, ast.Starred):
        return None
    return None


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
        bv = infer(b, cls.scope, (cls.node.lineno, cls.node.col_offset)) if cls.scope else None
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


def class_body_defs(cls):
    cs = class_scope_of(cls)
    if cs is None:
        return {}
    return cs.visible(None)


def self_attr_defs(cls):
    """Attributes assigned as self.X in __init__ -> {name: (value_node_or_None, path, scope, pos)}"""
    out = {}
    init = None
    for st in cls.node.body:
        if isinstance(st, FUNC_TYPES) and st.name == '__init__':
            init = st
    if init is None:
        return out
    params = list(getattr(init.args, 'posonlyargs', [])) + list(init.args.args)
    if not params:
        return out
    selfname = params[0].arg
    mod = cls.scope.module if cls.scope else None
    fs = mod.node_scope.get(init) if mod else None
    if fs is None:
        return out

    def handle_target(t, value, path, stmt):
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == selfname:
            if t.attr != PLACEHOLDER:
                out[t.attr] = (value, path, fs, (stmt.lineno, stmt.col_offset), stmt)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for i, e in enumerate(t.elts):
                if isinstance(e, ast.Starred):
                    handle_target(e.value, value, path + (None,), stmt)
                else:
                    handle_target(e, value, path + (i,), stmt)

    def walk(nodes):
        for n in nodes:
            if isinstance(n, FUNC_TYPES + (ast.ClassDef,)):
                continue
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    handle_target(t, n.value, (), n)
            elif isinstance(n, ast.AnnAssign):
                handle_target(n.target, n.value if n.value is not None else ('ann', n.annotation), (), n)
            elif isinstance(n, ast.AugAssign):
                if isinstance(n.target, ast.Attribute):
                    t = n.target
                    if isinstance(t.value, ast.Name) and t.value.id == selfname and t.attr not in out:
                        out[t.attr] = (None, (), fs, (n.lineno, n.col_offset), n)
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                if isinstance(n, (ast.For, ast.AsyncFor)):
                    handle_target(n.target, None, (), n)
                else:
                    for item in n.items:
                        if item.optional_vars is not None:
                            handle_target(item.optional_vars, None, (), n)
            if isinstance(n, ast.stmt):
                for field in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
                    sub = getattr(n, field, None)
                    if sub:
                        walk(sub)
            elif isinstance(n, (ast.ExceptHandler,)) or type(n).__name__ == 'match_case':
                walk(n.body)
    walk(init.body)
    return out


def self_attr_value(entry):
    value, path, fs, pos, stmt = entry
    if value is None:
        return None
    if isinstance(value, tuple) and value and value[0] == 'ann':
        return infer_annotation(value[1], fs, pos)
    v = infer_path(value, path, fs, pos)
    if v is None and isinstance(stmt, ast.AnnAssign):
        v = infer_annotation(stmt.annotation, fs, pos)
    return v


def _class_attr_map(cls, instance):
    """Return {name: thunk} for class (and instance) attributes."""
    out = {}
    for c in reversed(class_mro(cls)):
        for name, d in class_body_defs(c).items():
            out[name] = ('def', d)
        if instance:
            for name, entry in self_attr_defs(c).items():
                if name in out and out[name][0] == 'def' and getattr(out[name][1], 'kind', None) in ('def', 'class'):
                    # methods defined in class take precedence on the same class level
                    pass
                out[name] = ('self', entry)
    return out


def _thunk_value(t):
    kind, x = t
    if kind == 'def':
        return infer_def(x)
    if kind == 'self':
        return self_attr_value(x)
    return None


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
                d = v.local.scope.best(n)
                res.append((n, infer_def(d) if d is not None else None))
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
        m = _class_attr_map(cls, isinstance(v, InstVal))
        return [(n, _thunk_value(t)) for n, t in m.items() if n != PLACEHOLDER]
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


def attr_value(v, name):
    """Return (found, value) for attribute `name` of value v."""
    if v is None:
        return False, None
    if isinstance(v, ModVal):
        if v.local is not None:
            return v.local.attr(name)
        if v.real is not None:
            ok, o = safe_getattr(v.real, name)
            if ok:
                return True, wrap_real(o, name)
            mod = real_import(v.real.__name__ + '.' + name)
            if mod is not None:
                return True, ModVal(name, real=mod)
        return False, None
    if isinstance(v, (ClassVal, InstVal)):
        cls = v if isinstance(v, ClassVal) else v.cls
        m = _class_attr_map(cls, isinstance(v, InstVal))
        if name in m:
            return True, _thunk_value(m[name])
        return False, None
    if isinstance(v, RealInst):
        ok, smp = sample_of(v.typ)
        target = smp if ok else v.typ
        ok2, o = safe_getattr(target, name)
        return (True, wrap_real(o, name)) if ok2 else (False, None)
    if isinstance(v, RealVal):
        ok2, o = safe_getattr(v.obj, name)
        return (True, wrap_real(o, name)) if ok2 else (False, None)
    if isinstance(v, FuncVal):
        return attr_value(RealInst(types.FunctionType), name)
    return False, None


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
        v = infer(receiver, scope, (line_no, col))
        seen = set()
        for name, val in attributes(v):
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


def main(argv):
    args = list(argv)
    fuzzy = False
    if '--fuzzy' in args:
        fuzzy = True
        args = [a for a in args if a != '--fuzzy']
    if len(args) != 4 or args[0] != 'complete':
        sys.stderr.write('usage: sith.py complete <file> <line> <col> [--fuzzy]\n')
        return 1
    _, path, line_s, col_s = args
    try:
        line_no = int(line_s)
        col = int(col_s)
    except ValueError:
        sys.stderr.write('error: line and column must be integers\n')
        return 1
    try:
        comps = complete(path, line_no, col, fuzzy)
    except SithError as e:
        sys.stderr.write('error: %s\n' % e)
        return 1
    data = json.dumps({'completions': comps}, separators=(',', ':'), ensure_ascii=False)
    out = sys.stdout
    try:
        out.buffer.write((data + '\n').encode('utf-8'))
        out.flush()
    except AttributeError:
        out.write(data + '\n')
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
