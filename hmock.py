"""hmock: a YAML-driven HTTP mock server.

The first part of this file is a Python implementation of Go's text/template
language (plus a sprig subset); the second part is the server.

Only the pieces needed by hmock are implemented, but the semantics follow Go
as closely as practical: pipelines, variables and scoping, if/else/with/range,
define/template/block, break/continue, trim markers, comments, the builtin
function set and a subset of sprig functions.
"""

import base64
import math
import os
import threading
import unicodedata
import urllib.parse
import uuid
from decimal import Decimal


class TemplateError(Exception):
    pass


class _Missing(object):
    def __repr__(self):
        return '<missing>'


MISSING = _Missing()


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


# ---------------------------------------------------------------------------
# Value helpers (Go fmt emulation)
# ---------------------------------------------------------------------------

def go_float(f, verb='g', prec=-1):
    if math.isnan(f):
        return 'NaN'
    if math.isinf(f):
        return '+Inf' if f > 0 else '-Inf'
    if f == 0:
        return '-0' if math.copysign(1.0, f) < 0 else '0'
    d = Decimal(repr(f))
    sign, digits, exp = d.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exp += 1
    nd = len(digits)
    dp = nd + exp
    x = dp - 1
    s = '-' if sign else ''
    ds = ''.join(str(c) for c in digits)
    if x < -4 or x >= 6:
        mant = ds[0] + ('.' + ds[1:] if nd > 1 else '')
        es = '-' if x < 0 else '+'
        ex = abs(x)
        return '%s%se%s%02d' % (s, mant, es, ex)
    if dp <= 0:
        return s + '0.' + '0' * (-dp) + ds
    if dp >= nd:
        return s + ds + '0' * (dp - nd)
    return s + ds[:dp] + '.' + ds[dp:]


def go_type(v):
    if v is None:
        return '<nil>'
    if isinstance(v, bool):
        return 'bool'
    if isinstance(v, int):
        return 'int'
    if isinstance(v, float):
        return 'float64'
    if isinstance(v, str):
        return 'string'
    if isinstance(v, (bytes, bytearray)):
        return '[]uint8'
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(x, str) for x in v):
            return '[]string'
        return '[]interface {}'
    if isinstance(v, dict):
        if v and all(isinstance(x, str) for x in v.values()):
            return 'map[string]string'
        return 'map[string]interface {}'
    tn = getattr(v, '_go_type', None)
    if tn:
        return tn
    if callable(v):
        return 'func'
    return type(v).__name__


def _sort_keys(keys):
    try:
        return sorted(keys)
    except TypeError:
        return list(keys)


def as_map(v):
    """Return a dict if v behaves like a Go map, else None."""
    if isinstance(v, dict):
        return v
    m = getattr(v, '_tmpl_map', None)
    if m is not None:
        return m()
    return None


def fmtv(v):
    """fmt.Sprint style %v formatting."""
    if v is None:
        return '<nil>'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return go_float(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return '[' + ' '.join(str(b) for b in v) + ']'
    if isinstance(v, (list, tuple)):
        return '[' + ' '.join(fmtv(x) for x in v) + ']'
    m = as_map(v)
    if m is not None:
        return 'map[' + ' '.join('%s:%s' % (fmtv(k), fmtv(m[k])) for k in _sort_keys(m.keys())) + ']'
    fields = getattr(v, '_tmpl_fields', None)
    if fields is not None:
        return '{' + ' '.join(fmtv(getattr(v, f)) for f in fields) + '}'
    return str(v)


def print_value(v):
    if v is None:
        return '<no value>'
    return fmtv(v)


def sprint(*args):
    out = []
    prev_string = False
    for i, a in enumerate(args):
        is_string = isinstance(a, str)
        if i > 0 and not is_string and not prev_string:
            out.append(' ')
        out.append(fmtv(a))
        prev_string = is_string
    return ''.join(out)


def sprintln(*args):
    return ' '.join(fmtv(a) for a in args) + '\n'


def go_quote(s, ascii_only=False):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\a':
            out.append('\\a')
        elif ch == '\b':
            out.append('\\b')
        elif ch == '\f':
            out.append('\\f')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif ch == '\v':
            out.append('\\v')
        elif o < 0x20 or o == 0x7f:
            out.append('\\x%02x' % o)
        elif o < 0x80:
            out.append(ch)
        elif ch.isprintable() and not ascii_only:
            out.append(ch)
        elif o < 0x10000:
            out.append('\\u%04x' % o)
        else:
            out.append('\\U%08x' % o)
    out.append('"')
    return ''.join(out)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _fmt_one(verb, flags, width, prec, a):
    def pad(s, numeric=False):
        if width is None or len(s) >= width:
            return s
        if '-' in flags:
            return s + ' ' * (width - len(s))
        if '0' in flags and numeric:
            if s and s[0] in '+- ':
                return s[0] + '0' * (width - len(s)) + s[1:]
            return '0' * (width - len(s)) + s
        return ' ' * (width - len(s)) + s

    def bad():
        if a is None:
            return '%!' + verb + '(<nil>)'
        return '%%!%s(%s=%s)' % (verb, go_type(a), fmtv(a))

    def sign(neg):
        if neg:
            return '-'
        if '+' in flags:
            return '+'
        if ' ' in flags:
            return ' '
        return ''

    if verb == 'v':
        if '#' in flags and isinstance(a, str):
            return pad(go_quote(a))
        if isinstance(a, float) and prec is not None:
            return pad(_fmt_one('g', flags, None, prec, a), True)
        if '+' in flags and _is_int(a):
            return pad(sign(a < 0) + str(abs(a)), True)
        return pad(fmtv(a), _is_int(a) or isinstance(a, float))
    if verb == 'T':
        return pad(go_type(a))
    if isinstance(a, (list, tuple)) and verb not in 'T':
        if verb in 'sxXq' and isinstance(a, (bytes, bytearray)):
            pass
        else:
            return '[' + ' '.join(_fmt_one(verb, flags, width, prec, x) for x in a) + ']'
    if verb == 't':
        if isinstance(a, bool):
            return pad('true' if a else 'false')
        return bad()
    if verb in 'dboxXcqU' and _is_int(a):
        n = a
        if verb == 'd':
            s = str(abs(n))
        elif verb == 'b':
            s = format(abs(n), 'b')
        elif verb == 'o':
            s = format(abs(n), 'o')
            if '#' in flags:
                s = '0' + s
        elif verb == 'x':
            s = format(abs(n), 'x')
            if '#' in flags:
                s = '0x' + s
        elif verb == 'X':
            s = format(abs(n), 'X')
            if '#' in flags:
                s = '0X' + s
        elif verb == 'c':
            try:
                return pad(chr(n))
            except (ValueError, OverflowError):
                return pad('�')
        elif verb == 'q':
            try:
                c = chr(n)
            except (ValueError, OverflowError):
                c = '�'
            q = go_quote(c)
            return pad("'" + q[1:-1].replace('\\"', '"').replace("'", "\\'") + "'")
        elif verb == 'U':
            s = 'U+%04X' % n
            return pad(s)
        if prec is not None and verb in 'dboxX':
            s = s.rjust(prec, '0')
        return pad(sign(n < 0) + s, prec is None)
    if verb in 'eEfFgG' and isinstance(a, (float,)) :
        f = a
        if math.isnan(f) or math.isinf(f):
            s = 'NaN' if math.isnan(f) else ('+Inf' if f > 0 else '-Inf')
            if s == '+Inf' and '+' not in flags:
                s = 'Inf' if False else '+Inf'
            return pad(s)
        neg = f < 0 or (f == 0 and math.copysign(1, f) < 0)
        af = abs(f)
        if verb in 'fF':
            s = format(af, '.%df' % (6 if prec is None else prec))
        elif verb in 'eE':
            s = format(af, '.%de' % (6 if prec is None else prec))
            if verb == 'E':
                s = s.upper()
        else:
            if prec is None:
                s = go_float(af)
            else:
                s = format(af, '.%dg' % (prec if prec > 0 else 1))
            if verb == 'G':
                s = s.upper()
        return pad(sign(neg) + s, True)
    if verb in 'sqxX' and isinstance(a, (str, bytes, bytearray)):
        s = a if isinstance(a, str) else bytes(a).decode('utf-8', 'replace')
        if verb == 's':
            if prec is not None:
                s = s[:prec]
            return pad(s)
        if verb == 'q':
            if prec is not None:
                s = s[:prec]
            if '#' in flags and '`' not in s and all(c.isprintable() for c in s):
                return pad('`' + s + '`')
            return pad(go_quote(s, '+' in flags))
        b = s.encode('utf-8')
        if prec is not None:
            b = b[:prec]
        h = b.hex()
        if verb == 'X':
            h = h.upper()
        if ' ' in flags:
            pre = ('0x' if verb == 'x' else '0X') if '#' in flags else ''
            h = ' '.join(pre + h[i:i + 2] for i in range(0, len(h), 2))
        elif '#' in flags and h:
            h = ('0x' if verb == 'x' else '0X') + h
        return pad(h)
    if verb == 's' and a is not None and not isinstance(a, (bool, int, float)):
        return pad(fmtv(a))
    if verb == 'q' and isinstance(a, (bool, int, float)) is False and a is not None:
        return pad(go_quote(fmtv(a)))
    return bad()


def go_sprintf(f, *args):
    if not isinstance(f, str):
        raise TemplateError('wrong type for value; expected string; got %s' % go_type(f))
    args = list(args)
    out = []
    i = 0
    n = len(f)
    argi = 0
    while i < n:
        c = f[i]
        if c != '%':
            j = f.find('%', i)
            if j < 0:
                j = n
            out.append(f[i:j])
            i = j
            continue
        i += 1
        flags = ''
        while i < n and f[i] in '+-# 0':
            flags += f[i]
            i += 1
        width = None
        if i < n and f[i] == '*':
            i += 1
            if argi < len(args) and _is_int(args[argi]):
                width = args[argi]
                if width < 0:
                    flags += '-'
                    width = -width
            else:
                out.append('%!(BADWIDTH)')
            argi += 1
        else:
            st = i
            while i < n and f[i].isdigit():
                i += 1
            if i > st:
                width = int(f[st:i])
        prec = None
        if i < n and f[i] == '.':
            i += 1
            if i < n and f[i] == '*':
                i += 1
                if argi < len(args) and _is_int(args[argi]):
                    prec = args[argi]
                    if prec < 0:
                        prec = None
                else:
                    out.append('%!(BADPREC)')
                argi += 1
            else:
                st = i
                while i < n and f[i].isdigit():
                    i += 1
                prec = int(f[st:i]) if i > st else 0
        if i >= n:
            out.append('%!(NOVERB)')
            break
        verb = f[i]
        i += 1
        if verb == '%':
            out.append('%')
            continue
        if argi >= len(args):
            out.append('%!' + verb + '(MISSING)')
            continue
        a = args[argi]
        argi += 1
        out.append(_fmt_one(verb, flags, width, prec, a))
    if argi < len(args):
        out.append('%!(EXTRA ' + ', '.join(
            '%s=%s' % (go_type(a), fmtv(a)) if a is not None else '<nil>' for a in args[argi:]) + ')')
    return ''.join(out)


# ---------------------------------------------------------------------------
# Truth / comparison
# ---------------------------------------------------------------------------

def is_true(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, (str, bytes, bytearray, list, tuple, dict)):
        return len(v) > 0
    m = as_map(v)
    if m is not None:
        return len(m) > 0
    return True


def _kind(v):
    if isinstance(v, bool):
        return 'bool'
    if isinstance(v, int):
        return 'int'
    if isinstance(v, float):
        return 'float'
    if isinstance(v, str):
        return 'string'
    return None


def go_eq(arg1=MISSING, *args):
    if arg1 is MISSING or not args:
        raise TemplateError('missing argument for comparison')
    k1 = _kind(arg1)
    for a in args:
        k2 = _kind(a)
        truth = False
        if k1 != k2:
            if arg1 is not None and a is not None:
                raise TemplateError('incompatible types for comparison')
            truth = arg1 is None and a is None
        elif k1 is None:
            if arg1 is None or a is None:
                truth = (arg1 is None) == (a is None)
            elif isinstance(arg1, (list, dict)) or isinstance(a, (list, dict)) or as_map(arg1) is not None:
                raise TemplateError('non-comparable type %s: %s' % (fmtv(arg1), go_type(arg1)))
            else:
                truth = arg1 is a or arg1 == a
        else:
            truth = arg1 == a
        if truth:
            return True
    return False


def go_ne(a, b):
    return not go_eq(a, b)


def go_lt(a, b):
    ka = _kind(a)
    kb = _kind(b)
    if ka is None or ka == 'bool':
        raise TemplateError('invalid type for comparison')
    if kb is None or kb == 'bool':
        raise TemplateError('invalid type for comparison')
    if ka != kb:
        raise TemplateError('incompatible types for comparison')
    if ka == 'string':
        return a.encode('utf-8') < b.encode('utf-8')
    return a < b


def go_le(a, b):
    return go_lt(a, b) or go_eq(a, b)


def go_gt(a, b):
    return not go_le(a, b)


def go_ge(a, b):
    return not go_lt(a, b)


def go_not(a):
    return not is_true(a)


# ---------------------------------------------------------------------------
# Builtin functions
# ---------------------------------------------------------------------------

def _eval_args_str(args):
    if len(args) == 1 and isinstance(args[0], str):
        return args[0]
    return sprint(*args)


def html_escape(*args):
    s = _eval_args_str(args)
    rep = {'\0': '�', '"': '&#34;', "'": '&#39;', '&': '&amp;', '<': '&lt;', '>': '&gt;'}
    return ''.join(rep.get(c, c) for c in s)


def js_escape(*args):
    s = _eval_args_str(args)
    out = []
    for c in s:
        o = ord(c)
        if c == '\\':
            out.append('\\\\')
        elif c == "'":
            out.append("\\'")
        elif c == '"':
            out.append('\\"')
        elif c == '<':
            out.append('\\u003C')
        elif c == '>':
            out.append('\\u003E')
        elif c == '&':
            out.append('\\u0026')
        elif c == '=':
            out.append('\\u003D')
        elif o < 0x20:
            out.append('\\u%04X' % o)
        elif o < 0x80 or c.isprintable():
            out.append(c)
        else:
            out.append('\\u%04X' % o)
    return ''.join(out)


def urlquery(*args):
    s = _eval_args_str(args)
    return urllib.parse.quote_plus(s, safe='')


def go_len(v=MISSING):
    if v is MISSING:
        raise TemplateError('wrong number of args for len: want 1 got 0')
    if v is None:
        raise TemplateError('len of nil pointer')
    if isinstance(v, str):
        return len(v.encode('utf-8'))
    if isinstance(v, (list, tuple, dict, bytes, bytearray)):
        return len(v)
    m = as_map(v)
    if m is not None:
        return len(m)
    raise TemplateError('len of type %s' % go_type(v))


def _index_one(item, idx):
    if isinstance(item, str):
        item = item.encode('utf-8')
    if isinstance(item, (list, tuple, bytes, bytearray)):
        if not _is_int(idx):
            raise TemplateError('cannot index slice/array with type %s' % go_type(idx))
        if idx < 0 or idx >= len(item):
            raise TemplateError('index out of range: %d' % idx)
        return item[idx]
    m = as_map(item)
    if m is not None:
        if idx in m:
            return m[idx]
        zero = getattr(item, '_tmpl_zero', None)
        return zero() if zero else None
    raise TemplateError("can't index item of type %s" % go_type(item))


def go_index(item=MISSING, *indexes):
    if item is MISSING:
        raise TemplateError('wrong number of args for index: want at least 1 got 0')
    if item is None:
        raise TemplateError('index of untyped nil')
    for i in indexes:
        item = _index_one(item, i)
        if item is None and i is not indexes[-1]:
            raise TemplateError('index of nil pointer')
    return item


def go_slice(item, *indexes):
    if item is None:
        raise TemplateError('slice of untyped nil')
    if len(indexes) > 3:
        raise TemplateError('too many slice indexes: %d' % len(indexes))
    if not isinstance(item, (str, list, tuple)):
        raise TemplateError("can't slice item of type %s" % go_type(item))
    seq = item.encode('utf-8') if isinstance(item, str) else item
    if isinstance(item, str) and len(indexes) == 3:
        raise TemplateError('cannot 3-index slice a string')
    idx = []
    for i in indexes:
        if not _is_int(i):
            raise TemplateError('cannot index slice/array with type %s' % go_type(i))
        if i < 0 or i > len(seq):
            raise TemplateError('index out of range: %d' % i)
        idx.append(i)
    start = idx[0] if len(idx) > 0 else 0
    end = idx[1] if len(idx) > 1 else len(seq)
    if end < start:
        raise TemplateError('invalid slice index: %d > %d' % (start, end))
    res = seq[start:end]
    if isinstance(item, str):
        return res.decode('utf-8', 'replace')
    return list(res)


def go_call(fn=MISSING, *args):
    if fn is MISSING:
        raise TemplateError('wrong number of args for call: want at least 1 got 0')
    if fn is None:
        raise TemplateError('call of nil')
    if not callable(fn):
        raise TemplateError('non-function of type %s' % go_type(fn))
    return fn(*args)


# ---------------------------------------------------------------------------
# Sprig subset
# ---------------------------------------------------------------------------

def _str(v, name='value'):
    if not isinstance(v, str):
        raise TemplateError('wrong type for value; expected string; got %s' % go_type(v))
    return v


def _int(v):
    if not _is_int(v):
        raise TemplateError('wrong type for value; expected int; got %s' % go_type(v))
    return v


def strval(v):
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode('utf-8', 'replace')
    return fmtv(v)


def strslice(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [strval(x) for x in v if x is not None]
    return [strval(v)]


def go_parse_int(s):
    s0 = s
    neg = False
    if s[:1] in '+-':
        neg = s[0] == '-'
        s = s[1:]
    if not s:
        raise ValueError(s0)
    base = 10
    allow_us = False
    low = s.lower()
    if low.startswith('0x'):
        base, s, allow_us = 16, s[2:], True
    elif low.startswith('0b'):
        base, s, allow_us = 2, s[2:], True
    elif low.startswith('0o'):
        base, s, allow_us = 8, s[2:], True
    elif len(s) > 1 and s[0] == '0':
        base, s, allow_us = 8, s[1:], True
    if '_' in s and not allow_us:
        raise ValueError(s0)
    if not s or s.startswith('_') or s.endswith('_') or '__' in s:
        if not (base == 8 and s == ''):
            raise ValueError(s0)
    for ch in s.replace('_', ''):
        if ch in '+-':
            raise ValueError(s0)
    v = int(s.replace('_', ''), base) if s else 0
    v = -v if neg else v
    if v >= 2 ** 63 or v < -2 ** 63:
        raise ValueError(s0)
    return v


def _trim_zero_decimal(s):
    found_zero = False
    for i in range(len(s), 0, -1):
        c = s[i - 1]
        if c == '.':
            if found_zero:
                return s[:i - 1]
        elif c == '0':
            found_zero = True
        else:
            return s
    return s


def _wrap64(v):
    v &= (1 << 64) - 1
    if v >= 1 << 63:
        v -= 1 << 64
    return v


def to_int64(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return _wrap64(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return -2 ** 63
        return _wrap64(int(v))
    if isinstance(v, str):
        try:
            return go_parse_int(_trim_zero_decimal(v))
        except ValueError:
            return 0
    return 0


def _go_div(a, b):
    if b == 0:
        raise TemplateError('runtime error: integer divide by zero')
    q = abs(a) // abs(b)
    return _wrap64(q if (a >= 0) == (b >= 0) else -q)


def _go_mod(a, b):
    if b == 0:
        raise TemplateError('runtime error: integer divide by zero')
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def sp_add(*args):
    return _wrap64(sum(to_int64(a) for a in args))


def sp_add1(a):
    return _wrap64(to_int64(a) + 1)


def sp_sub(a, b):
    return _wrap64(to_int64(a) - to_int64(b))


def sp_mul(a, *v):
    r = to_int64(a)
    for x in v:
        r = _wrap64(r * to_int64(x))
    return r


def sp_div(a, b):
    return _go_div(to_int64(a), to_int64(b))


def sp_mod(a, b):
    return _go_mod(to_int64(a), to_int64(b))


def sp_max(a, *i):
    r = to_int64(a)
    for x in i:
        r = max(r, to_int64(x))
    return r


def sp_min(a, *i):
    r = to_int64(a)
    for x in i:
        r = min(r, to_int64(x))
    return r


def sp_empty(v=None):
    if v is None:
        return True
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, (str, list, tuple, dict, bytes, bytearray)):
        return len(v) == 0
    m = as_map(v)
    if m is not None:
        return len(m) == 0
    return False


def sp_default(d, *given):
    if not given or sp_empty(given[0]):
        return d
    return given[0]


def sp_coalesce(*v):
    for x in v:
        if not sp_empty(x):
            return x
    return None


def sp_ternary(vt, vf, v):
    if not isinstance(v, bool):
        raise TemplateError('wrong type for value; expected bool; got %s' % go_type(v))
    return vt if v else vf


def _is_separator(c):
    o = ord(c)
    if o <= 0x7F:
        if c.isalnum() or c == '_':
            return False
        return True
    cat = unicodedata.category(c)
    if cat.startswith('L') or cat == 'Nd':
        return False
    return c.isspace()


def sp_title(s):
    s = _str(s)
    out = []
    prev = ' '
    for c in s:
        if _is_separator(prev):
            out.append(c.title() if len(c.title()) == 1 else c.upper())
        else:
            out.append(c)
        prev = c
    return ''.join(out)


def _go_split(s, sep):
    if sep == '':
        return list(s)
    return s.split(sep)


def sp_split(sep, orig):
    parts = _go_split(_str(orig), _str(sep))
    return {'_%d' % i: p for i, p in enumerate(parts)}


def sp_split_list(sep, orig):
    return _go_split(_str(orig), _str(sep))


def sp_join(sep, v):
    return _str(sep).join(strslice(v))


def sp_repeat(count, s):
    count = _int(count)
    if count < 0:
        raise TemplateError('strings: negative Repeat count')
    return _str(s) * count


GO_SPACE = ' \t\n\v\f\r\x85\xa0'


def _go_isspace(c):
    return c in GO_SPACE or (ord(c) > 0xff and c.isspace())


def sp_trim(s):
    s = _str(s)
    i, j = 0, len(s)
    while i < j and _go_isspace(s[i]):
        i += 1
    while j > i and _go_isspace(s[j - 1]):
        j -= 1
    return s[i:j]


def sp_nospace(s):
    return ''.join(c for c in _str(s) if not _go_isspace(c))


def sp_b64enc(s):
    return base64.b64encode(_str(s).encode('utf-8')).decode('ascii')


def sp_b64dec(s):
    s = _str(s).replace('\r', '').replace('\n', '')
    try:
        return base64.b64decode(s.encode('ascii'), validate=True).decode('utf-8', 'replace')
    except Exception:
        pos = 0
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        for pos, c in enumerate(s):
            if c not in alphabet:
                break
        else:
            pos = len(s) - (len(s) % 4) if len(s) % 4 else len(s)
        return 'illegal base64 data at input byte %d' % pos


def sp_env(s):
    return os.environ.get(_str(s), '')


def sp_uuidv4():
    return str(uuid.uuid4())


def sp_to_string(v):
    return strval(v)


def sp_quote(*s):
    return ' '.join(go_quote(strval(x)) for x in s if x is not None)


def sp_squote(*s):
    return ' '.join("'" + strval(x) + "'" for x in s if x is not None)


def sp_atoi(s):
    try:
        return int(_str(s).strip()) if _str(s).strip().lstrip('+-').isdigit() else 0
    except ValueError:
        return 0


def sp_trim_all(cut, s):
    return _str(s).strip(_str(cut))


def sp_trim_prefix(p, s):
    s, p = _str(s), _str(p)
    return s[len(p):] if p and s.startswith(p) else s


def sp_trim_suffix(p, s):
    s, p = _str(s), _str(p)
    return s[:-len(p)] if p and s.endswith(p) else s


def sp_list(*v):
    return list(v)


def sp_dict(*v):
    d = {}
    for i in range(0, len(v), 2):
        k = strval(v[i])
        d[k] = v[i + 1] if i + 1 < len(v) else ''
    return d


def sp_has_key(d, k):
    m = as_map(d)
    return m is not None and k in m


def sp_has(needle, haystack):
    if haystack is None:
        return False
    if isinstance(haystack, (list, tuple)):
        return any(needle == x and type(needle) is type(x) for x in haystack)
    raise TemplateError('Cannot find has on type %s' % go_type(haystack))


def sp_int(v):
    return to_int64(v)


def sp_float64(v):
    if v is None:
        return 0.0
    if isinstance(v, (bool, int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    return 0.0


def sp_abbrev(width, s):
    s = _str(s)
    if width < 4 or len(s) <= width:
        return s
    return s[:width - 3] + '...'


def _bool_str_fn(fn):
    def f(a, b):
        return fn(_str(b), _str(a))
    return f


FUNCS = {
    # builtins (and/or are handled specially by the executor)
    'and': None,
    'or': None,
    'not': go_not,
    'eq': go_eq,
    'ne': go_ne,
    'lt': go_lt,
    'le': go_le,
    'gt': go_gt,
    'ge': go_ge,
    'print': sprint,
    'printf': go_sprintf,
    'println': sprintln,
    'html': html_escape,
    'js': js_escape,
    'urlquery': urlquery,
    'len': go_len,
    'index': go_index,
    'slice': go_slice,
    'call': go_call,
    # sprig subset
    'contains': lambda substr, s: _str(substr) in _str(s),
    'hasPrefix': lambda p, s: _str(s).startswith(_str(p)),
    'hasSuffix': lambda p, s: _str(s).endswith(_str(p)),
    'replace': lambda old, new, src: _str(src).replace(_str(old), _str(new)),
    'trim': sp_trim,
    'trimAll': sp_trim_all,
    'trimPrefix': sp_trim_prefix,
    'trimSuffix': sp_trim_suffix,
    'upper': lambda s: _str(s).upper(),
    'lower': lambda s: _str(s).lower(),
    'title': sp_title,
    'split': sp_split,
    'splitList': sp_split_list,
    'join': sp_join,
    'repeat': sp_repeat,
    'nospace': sp_nospace,
    'toString': sp_to_string,
    'quote': sp_quote,
    'squote': sp_squote,
    'abbrev': sp_abbrev,
    'default': sp_default,
    'empty': sp_empty,
    'coalesce': sp_coalesce,
    'ternary': sp_ternary,
    'b64enc': sp_b64enc,
    'b64dec': sp_b64dec,
    'env': sp_env,
    'add': sp_add,
    'add1': sp_add1,
    'sub': sp_sub,
    'mul': sp_mul,
    'div': sp_div,
    'mod': sp_mod,
    'max': sp_max,
    'min': sp_min,
    'atoi': sp_atoi,
    'int': sp_int,
    'int64': sp_int,
    'float64': sp_float64,
    'list': sp_list,
    'dict': sp_dict,
    'hasKey': sp_has_key,
    'has': sp_has,
    'uuidv4': sp_uuidv4,
}


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

SPACE_CHARS = ' \t\r\n'
KEYWORDS = {'block', 'break', 'continue', 'define', 'else', 'end', 'if',
            'range', 'nil', 'template', 'with'}


def _is_alnum(c):
    return c == '_' or c.isalnum()


def _unquote_char_escape(s, i, quote):
    """Parse an escape sequence starting at s[i] == '\\'. Returns (str, newi)."""
    if i + 1 >= len(s):
        raise TemplateError('unterminated quoted string')
    c = s[i + 1]
    simple = {'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r',
              't': '\t', 'v': '\v', '\\': '\\'}
    if c in simple:
        return simple[c], i + 2
    if c == quote:
        return c, i + 2
    if c == 'x':
        h = s[i + 2:i + 4]
        if len(h) != 2 or any(ch not in '0123456789abcdefABCDEF' for ch in h):
            raise TemplateError('invalid syntax')
        return chr(int(h, 16)), i + 4
    if c in 'uU':
        n = 4 if c == 'u' else 8
        h = s[i + 2:i + 2 + n]
        if len(h) != n or any(ch not in '0123456789abcdefABCDEF' for ch in h):
            raise TemplateError('invalid syntax')
        v = int(h, 16)
        if v > 0x10FFFF:
            raise TemplateError('invalid syntax')
        return chr(v), i + 2 + n
    if c in '01234567':
        h = s[i + 1:i + 4]
        if len(h) != 3 or any(ch not in '01234567' for ch in h):
            raise TemplateError('invalid syntax')
        v = int(h, 8)
        if v > 255:
            raise TemplateError('invalid syntax')
        return chr(v), i + 4
    raise TemplateError('invalid syntax')


def _decode_octets(s):
    """Strings with \\x escapes may build byte sequences; reassemble them as UTF-8."""
    return s


def unquote_string(raw):
    q = raw[0]
    body = raw[1:-1]
    if q == '`':
        return body.replace('\r', '')
    out = []
    pending = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == '\\':
            esc = body[i + 1] if i + 1 < len(body) else ''
            ch, i = _unquote_char_escape(body, i, q)
            if esc == 'x' or esc in '01234567':
                pending.append(ord(ch))
                continue
            if pending:
                out.append(pending.decode('utf-8', 'replace'))
                pending = bytearray()
            out.append(ch)
        else:
            if pending:
                out.append(pending.decode('utf-8', 'replace'))
                pending = bytearray()
            out.append(c)
            i += 1
    if pending:
        out.append(pending.decode('utf-8', 'replace'))
    return ''.join(out)


def parse_number(text):
    t = text
    if t.endswith('i'):
        raise TemplateError('complex numbers are not supported: %s' % text)
    low = t.lower().lstrip('+-')
    is_hex = low.startswith('0x')
    if not is_hex and any(ch in t for ch in '.eE'):
        if '_' in t:
            t = t.replace('_', '')
        try:
            return float(t)
        except ValueError:
            raise TemplateError('illegal number syntax: %q' % text)
    if is_hex and any(ch in low for ch in '.p'):
        try:
            sign = -1 if t.startswith('-') else 1
            return sign * float.fromhex(low.replace('_', ''))
        except ValueError:
            raise TemplateError('illegal number syntax: %s' % text)
    try:
        return go_parse_int(t)
    except ValueError:
        raise TemplateError('illegal number syntax: %s' % text)


class Lexer(object):
    def __init__(self, src):
        self.src = src
        self.toks = []

    def error(self, msg):
        raise TemplateError('template: :%s' % msg)

    def lex(self):
        src = self.src
        n = len(src)
        i = 0
        trim_next = False
        toks = self.toks
        while True:
            j = src.find('{{', i)
            if j < 0:
                text = src[i:]
                if trim_next:
                    text = text.lstrip(SPACE_CHARS)
                if text:
                    toks.append(('TEXT', text))
                break
            text = src[i:j]
            if trim_next:
                text = text.lstrip(SPACE_CHARS)
                trim_next = False
            k = j + 2
            if k + 1 < n and src[k] == '-' and src[k + 1] in SPACE_CHARS:
                text = text.rstrip(SPACE_CHARS)
                k += 2
            if text:
                toks.append(('TEXT', text))
            # comment?
            kk = k
            if src.startswith('/*', kk):
                end = src.find('*/', kk + 2)
                if end < 0:
                    self.error('unclosed comment')
                m = end + 2
                if m + 1 < n and src[m] in SPACE_CHARS and src.startswith('-}}', m + 1):
                    trim_next = True
                    i = m + 4
                elif src.startswith('}}', m):
                    i = m + 2
                else:
                    self.error('comment ends before closing delimiter')
                continue
            toks.append(('LDELIM', None))
            k, trim_next = self.lex_action(k)
            i = k
        toks.append(('EOF', None))
        return toks

    def lex_action(self, k):
        src = self.src
        n = len(src)
        toks = self.toks
        depth = 0
        while True:
            if k >= n:
                self.error('unclosed action')
            c = src[k]
            if c in SPACE_CHARS:
                m = k
                while m < n and src[m] in SPACE_CHARS:
                    m += 1
                if src.startswith('-}}', m):
                    if depth:
                        self.error('unclosed left paren')
                    if m - k > 1:
                        toks.append(('SPACE', None))
                    toks.append(('RDELIM', None))
                    return m + 3, True
                toks.append(('SPACE', None))
                k = m
                continue
            if src.startswith('}}', k):
                if depth:
                    self.error('unclosed left paren')
                toks.append(('RDELIM', None))
                return k + 2, False
            if c == '=':
                toks.append(('ASSIGN', None))
                k += 1
            elif c == ':':
                if k + 1 < n and src[k + 1] == '=':
                    toks.append(('DECLARE', None))
                    k += 2
                else:
                    self.error('expected :=')
            elif c == '|':
                toks.append(('PIPE', None))
                k += 1
            elif c == '(':
                depth += 1
                toks.append(('LPAREN', None))
                k += 1
            elif c == ')':
                depth -= 1
                if depth < 0:
                    self.error('unexpected right paren')
                toks.append(('RPAREN', None))
                k += 1
            elif c == ',':
                toks.append(('COMMA', None))
                k += 1
            elif c == '"':
                m = k + 1
                while True:
                    if m >= n:
                        self.error('unterminated quoted string')
                    ch = src[m]
                    if ch == '\\':
                        m += 2
                        continue
                    if ch == '\n':
                        self.error('unterminated quoted string')
                    if ch == '"':
                        break
                    m += 1
                try:
                    val = unquote_string(src[k:m + 1])
                except TemplateError as e:
                    self.error(str(e))
                toks.append(('STRING', val))
                k = m + 1
            elif c == '`':
                m = src.find('`', k + 1)
                if m < 0:
                    self.error('unterminated raw quoted string')
                toks.append(('STRING', src[k + 1:m]))
                k = m + 1
            elif c == "'":
                m = k + 1
                while True:
                    if m >= n:
                        self.error('unterminated character constant')
                    ch = src[m]
                    if ch == '\\':
                        m += 2
                        continue
                    if ch == '\n':
                        self.error('unterminated character constant')
                    if ch == "'":
                        break
                    m += 1
                body = src[k + 1:m]
                try:
                    if body.startswith('\\'):
                        ch, e = _unquote_char_escape(body, 0, "'")
                        if e != len(body):
                            raise TemplateError('invalid syntax')
                    elif len(body) == 1:
                        ch = body
                    else:
                        raise TemplateError('invalid syntax')
                except TemplateError:
                    self.error('malformed character constant: %s' % src[k:m + 1])
                toks.append(('NUMBER', ord(ch)))
                k = m + 1
            elif c == '$':
                m = k + 1
                while m < n and _is_alnum(src[m]):
                    m += 1
                toks.append(('VARIABLE', src[k:m]))
                k = m
                self._check_term(k)
            elif c == '.':
                if k + 1 < n and src[k + 1].isdigit():
                    k = self.lex_number(k)
                elif k + 1 < n and _is_alnum(src[k + 1]):
                    m = k + 1
                    while m < n and _is_alnum(src[m]):
                        m += 1
                    toks.append(('FIELD', src[k + 1:m]))
                    k = m
                    self._check_term(k)
                else:
                    toks.append(('DOT', None))
                    k += 1
            elif c in '+-' or c.isdigit():
                k = self.lex_number(k)
            elif _is_alnum(c):
                m = k
                while m < n and _is_alnum(src[m]):
                    m += 1
                word = src[k:m]
                self._check_term(m)
                if word in ('true', 'false'):
                    toks.append(('BOOL', word == 'true'))
                elif word == 'nil':
                    toks.append(('NIL', None))
                elif word in KEYWORDS:
                    toks.append(('KEYWORD', word))
                else:
                    toks.append(('IDENT', word))
                k = m
            else:
                self.error('unrecognized character in action: %r' % c)

    def _check_term(self, k):
        src = self.src
        if k >= len(src):
            return
        c = src[k]
        if c in SPACE_CHARS or c in '.,|:)(=' or src.startswith('}}', k):
            return
        self.error('bad character %r' % c)

    def lex_number(self, k):
        import re
        m = re.compile(
            r'[+-]?(?:0[xX][0-9a-fA-F_]*(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[0-9_]+)?'
            r'|0[bB][01_]+|0[oO][0-7_]+'
            r'|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9_]+)?)i?'
        ).match(self.src, k)
        if not m or m.end() == k:
            self.error('bad number syntax: %r' % self.src[k:k + 10])
        end = m.end()
        if end < len(self.src) and _is_alnum(self.src[end]):
            self.error('bad number syntax: %r' % self.src[k:end + 1])
        text = m.group(0)
        if text in ('+', '-'):
            self.error('bad number syntax: %r' % text)
        try:
            val = parse_number(text)
        except TemplateError as e:
            self.error(str(e))
        self.toks.append(('NUMBER', val))
        return end


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

class Node(object):
    __slots__ = ()


class Text(Node):
    __slots__ = ('text',)

    def __init__(self, text):
        self.text = text


class Pipe(Node):
    __slots__ = ('decls', 'is_assign', 'cmds')

    def __init__(self, decls, is_assign, cmds):
        self.decls = decls
        self.is_assign = is_assign
        self.cmds = cmds


class Action(Node):
    __slots__ = ('pipe',)

    def __init__(self, pipe):
        self.pipe = pipe


class Command(Node):
    __slots__ = ('args',)

    def __init__(self, args):
        self.args = args


class Field(Node):
    __slots__ = ('idents',)

    def __init__(self, idents):
        self.idents = idents


class Variable(Node):
    __slots__ = ('idents',)

    def __init__(self, idents):
        self.idents = idents


class Chain(Node):
    __slots__ = ('node', 'fields')

    def __init__(self, node, fields):
        self.node = node
        self.fields = fields


class Ident(Node):
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name


class Dot(Node):
    __slots__ = ()


class Nil(Node):
    __slots__ = ()


class Const(Node):
    __slots__ = ('value',)

    def __init__(self, value):
        self.value = value


class If(Node):
    __slots__ = ('pipe', 'list', 'else_list')

    def __init__(self, pipe, lst, else_list):
        self.pipe = pipe
        self.list = lst
        self.else_list = else_list


class With(If):
    __slots__ = ()


class Range(If):
    __slots__ = ()


class TemplateCall(Node):
    __slots__ = ('name', 'pipe')

    def __init__(self, name, pipe):
        self.name = name
        self.pipe = pipe


class Break(Node):
    __slots__ = ()


class Continue(Node):
    __slots__ = ()


class _End(Node):
    __slots__ = ()


class _Else(Node):
    __slots__ = ()


END = _End()
ELSE = _Else()

OPERAND_START = ('BOOL', 'DOT', 'FIELD', 'IDENT', 'NUMBER', 'NIL', 'STRING',
                 'VARIABLE', 'LPAREN')


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class Parser(object):
    def __init__(self, toks, funcs):
        self.toks = toks
        self.pos = 0
        self.vars = ['$']
        self.funcs = funcs
        self.range_depth = 0
        self.defs = {}

    def error(self, msg):
        raise TemplateError('template: :%s' % msg)

    def next(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def peek(self):
        return self.toks[self.pos]

    def backup(self):
        self.pos -= 1

    def next_non_space(self):
        t = self.next()
        while t[0] == 'SPACE':
            t = self.next()
        return t

    def peek_non_space(self):
        t = self.next_non_space()
        self.backup()
        return t

    def expect(self, typ, context):
        t = self.next_non_space()
        if t[0] != typ:
            self.unexpected(t, context)
        return t

    def unexpected(self, tok, context):
        if tok[0] == 'EOF':
            self.error('unexpected EOF in %s' % context)
        self.error('unexpected %s in %s' % (tok[1] if tok[1] is not None else tok[0], context))

    def parse(self):
        lst = []
        while self.peek()[0] != 'EOF':
            if self.peek()[0] == 'LDELIM':
                p = self.pos
                self.next()
                t = self.next_non_space()
                if t == ('KEYWORD', 'define'):
                    self.parse_define()
                    continue
                self.pos = p
            n = self.text_or_action()
            if n is END or n is ELSE:
                self.error('unexpected {{%s}}' % ('end' if n is END else 'else'))
            lst.append(n)
        return lst

    def parse_define(self):
        t = self.next_non_space()
        if t[0] != 'STRING':
            self.unexpected(t, 'define clause')
        name = t[1]
        self.expect('RDELIM', 'define clause')
        saved = self.vars
        self.vars = ['$']
        lst, nxt = self.item_list()
        if nxt is not END:
            self.error('unexpected {{else}} in define')
        self.vars = saved
        self.defs[name] = lst

    def text_or_action(self):
        t = self.next_non_space()
        if t[0] == 'TEXT':
            return Text(t[1])
        if t[0] == 'LDELIM':
            return self.action()
        self.unexpected(t, 'input')

    def item_list(self):
        lst = []
        while self.peek_non_space()[0] != 'EOF':
            n = self.text_or_action()
            if n is END or n is ELSE:
                return lst, n
            lst.append(n)
        self.error('unexpected EOF')

    def action(self):
        t = self.next_non_space()
        if t[0] == 'KEYWORD':
            kw = t[1]
            if kw == 'block':
                return self.block_control()
            if kw == 'break':
                return self.break_control(Break, 'break')
            if kw == 'continue':
                return self.break_control(Continue, 'continue')
            if kw == 'else':
                return self.else_control()
            if kw == 'end':
                self.expect('RDELIM', 'end')
                return END
            if kw == 'if':
                return self.if_control()
            if kw == 'range':
                return self.range_control()
            if kw == 'template':
                return self.template_control()
            if kw == 'with':
                return self.with_control()
            if kw == 'define':
                self.error('unexpected define in action')
            self.backup()
        else:
            self.backup()
        return Action(self.pipeline('command', 'RDELIM'))

    def break_control(self, cls, name):
        t = self.next_non_space()
        if t[0] != 'RDELIM':
            self.unexpected(t, '{{%s}}' % name)
        if self.range_depth == 0:
            self.error('{{%s}} outside {{range}}' % name)
        return cls()

    def else_control(self):
        p = self.peek_non_space()
        if p[0] == 'KEYWORD' and p[1] in ('if', 'with'):
            return ELSE
        self.expect('RDELIM', 'else')
        return ELSE

    def parse_control(self, context):
        nvars = len(self.vars)
        pipe = self.pipeline(context, 'RDELIM')
        if context == 'range':
            self.range_depth += 1
        lst, nxt = self.item_list()
        if context == 'range':
            self.range_depth -= 1
        else_list = None
        if nxt is ELSE:
            p = self.peek()
            if context == 'if' and p == ('KEYWORD', 'if'):
                self.next()
                else_list = [self.if_control()]
            elif context == 'with' and p == ('KEYWORD', 'with'):
                self.next()
                else_list = [self.with_control()]
            else:
                else_list, nxt = self.item_list()
                if nxt is not END:
                    self.error('expected end; found {{else}}')
        del self.vars[nvars:]
        return pipe, lst, else_list

    def if_control(self):
        return If(*self.parse_control('if'))

    def with_control(self):
        return With(*self.parse_control('with'))

    def range_control(self):
        return Range(*self.parse_control('range'))

    def template_control(self):
        t = self.next_non_space()
        if t[0] != 'STRING':
            self.unexpected(t, 'template clause')
        name = t[1]
        pipe = None
        if self.next_non_space()[0] != 'RDELIM':
            self.backup()
            pipe = self.pipeline('template clause', 'RDELIM')
        return TemplateCall(name, pipe)

    def block_control(self):
        t = self.next_non_space()
        if t[0] != 'STRING':
            self.unexpected(t, 'block clause')
        name = t[1]
        pipe = self.pipeline('block clause', 'RDELIM')
        saved = self.vars
        self.vars = ['$']
        lst, nxt = self.item_list()
        if nxt is not END:
            self.error('unexpected {{else}} in block clause')
        self.vars = saved
        self.defs[name] = lst
        return TemplateCall(name, pipe)

    def pipeline(self, context, end):
        decls = []
        is_assign = False
        p0 = self.pos
        if self.peek_non_space()[0] == 'VARIABLE':
            while True:
                v = self.next_non_space()
                nxt = self.peek_non_space()
                if nxt[0] in ('ASSIGN', 'DECLARE'):
                    is_assign = nxt[0] == 'ASSIGN'
                    self.next_non_space()
                    decls.append(v[1])
                    break
                elif nxt[0] == 'COMMA':
                    self.next_non_space()
                    decls.append(v[1])
                    if context == 'range' and len(decls) < 2 and self.peek_non_space()[0] == 'VARIABLE':
                        continue
                    self.error('too many declarations in %s' % context)
                else:
                    self.pos = p0
                    decls = []
                    break
        if decls:
            if is_assign:
                for d in decls:
                    if d not in self.vars:
                        self.error('undefined variable "%s"' % d)
            else:
                self.vars.extend(decls)
        cmds = []
        while True:
            t = self.next_non_space()
            if t[0] == end:
                break
            if t[0] in OPERAND_START:
                self.backup()
                cmds.append(self.command())
            else:
                self.unexpected(t, context)
        if not cmds:
            self.error('missing value for %s' % context)
        for i, c in enumerate(cmds[1:]):
            if isinstance(c.args[0], (Const, Dot, Nil)):
                self.error('non executable command in pipeline stage %d' % (i + 2))
        return Pipe(decls, is_assign, cmds)

    def command(self):
        args = []
        while True:
            self.peek_non_space()
            op = self.operand()
            if op is not None:
                args.append(op)
            t = self.next()
            if t[0] == 'SPACE':
                continue
            if t[0] in ('RDELIM', 'RPAREN'):
                self.backup()
            elif t[0] == 'PIPE':
                pass
            else:
                self.unexpected(t, 'operand')
            break
        if not args:
            self.error('empty command')
        return Command(args)

    def operand(self):
        node = self.term()
        if node is None:
            return None
        if self.peek()[0] == 'FIELD':
            fields = []
            while self.peek()[0] == 'FIELD':
                fields.append(self.next()[1])
            if isinstance(node, Field):
                node = Field(node.idents + fields)
            elif isinstance(node, Variable):
                node = Variable(node.idents + fields)
            elif isinstance(node, (Const, Nil, Dot)):
                self.error('unexpected . after term')
            else:
                node = Chain(node, fields)
        return node

    def term(self):
        t = self.next_non_space()
        typ = t[0]
        if typ == 'IDENT':
            if t[1] not in self.funcs:
                self.error('function "%s" not defined' % t[1])
            return Ident(t[1])
        if typ == 'DOT':
            return Dot()
        if typ == 'NIL':
            return Nil()
        if typ == 'VARIABLE':
            if t[1] not in self.vars:
                self.error('undefined variable "%s"' % t[1])
            return Variable([t[1]])
        if typ == 'FIELD':
            return Field([t[1]])
        if typ in ('BOOL', 'NUMBER', 'STRING'):
            return Const(t[1])
        if typ == 'LPAREN':
            return self.pipeline('parenthesized pipeline', 'RPAREN')
        self.backup()
        return None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

MAX_EXEC_DEPTH = 200


class State(object):
    def __init__(self, tmpl, root, depth=0):
        self.tmpl = tmpl
        self.vars = [['$', root]]
        self.depth = depth

    def error(self, msg):
        raise TemplateError('template: :%s' % msg)

    def var_value(self, name):
        for i in range(len(self.vars) - 1, -1, -1):
            if self.vars[i][0] == name:
                return self.vars[i][1]
        self.error('undefined variable: %s' % name)

    def set_var(self, name, value):
        for i in range(len(self.vars) - 1, -1, -1):
            if self.vars[i][0] == name:
                self.vars[i][1] = value
                return
        self.error('undefined variable: %s' % name)

    def walk_list(self, dot, lst, out):
        for n in lst:
            self.walk(dot, n, out)

    def walk(self, dot, node, out):
        if isinstance(node, Text):
            out.append(node.text)
        elif isinstance(node, Action):
            v = self.eval_pipeline(dot, node.pipe)
            if not node.pipe.decls:
                out.append(print_value(v))
        elif isinstance(node, Range):
            self.walk_range(dot, node, out)
        elif isinstance(node, If):
            mark = len(self.vars)
            try:
                v = self.eval_pipeline(dot, node.pipe)
                if is_true(v):
                    self.walk_list(v if isinstance(node, With) else dot, node.list, out)
                elif node.else_list is not None:
                    self.walk_list(dot, node.else_list, out)
            finally:
                del self.vars[mark:]
        elif isinstance(node, TemplateCall):
            lst = self.tmpl.defs.get(node.name)
            if lst is None:
                self.error('no such template "%s"' % node.name)
            if self.depth >= MAX_EXEC_DEPTH:
                self.error('exceeded maximum template depth (%d)' % MAX_EXEC_DEPTH)
            newdot = self.eval_pipeline(dot, node.pipe) if node.pipe is not None else None
            st = State(self.tmpl, newdot, self.depth + 1)
            st.walk_list(newdot, lst, out)
        elif isinstance(node, Break):
            raise _Break()
        elif isinstance(node, Continue):
            raise _Continue()
        else:
            self.error('unknown node')

    def walk_range(self, dot, node, out):
        outer = len(self.vars)
        try:
            v = self.eval_pipeline(dot, node.pipe)
            mark = len(self.vars)
            items = None
            if v is None:
                items = []
            elif isinstance(v, bool):
                self.error("range can't iterate over %s" % fmtv(v))
            elif isinstance(v, int):
                if v < 0:
                    items = []
                else:
                    items = ((i, i) for i in range(v))
                    if v == 0:
                        items = []
            elif isinstance(v, (list, tuple)):
                items = list(enumerate(v))
            elif isinstance(v, (bytes, bytearray)):
                items = list(enumerate(v))
            else:
                m = as_map(v)
                if m is not None:
                    items = [(k, m[k]) for k in _sort_keys(m.keys())]
                else:
                    self.error("range can't iterate over %s" % fmtv(v))
            any_item = False
            decls = node.pipe.decls
            for k, e in items:
                any_item = True
                if decls:
                    if node.pipe.is_assign:
                        if len(decls) > 1:
                            self.set_var(decls[0], k)
                        self.set_var(decls[-1], e)
                    else:
                        self.vars[-1][1] = e
                        if len(decls) > 1:
                            self.vars[-2][1] = k
                try:
                    self.walk_list(e, node.list, out)
                except _Break:
                    break
                except _Continue:
                    pass
                finally:
                    del self.vars[mark:]
            if not any_item and node.else_list is not None:
                self.walk_list(dot, node.else_list, out)
        finally:
            del self.vars[outer:]

    def eval_pipeline(self, dot, pipe):
        value = MISSING
        for cmd in pipe.cmds:
            value = self.eval_command(dot, cmd, value)
        if value is MISSING:
            value = None
        for d in pipe.decls:
            if pipe.is_assign:
                self.set_var(d, value)
            else:
                self.vars.append([d, value])
        return value

    def not_a_function(self, args, final):
        if (args is not None and len(args) > 1) or final is not MISSING:
            self.error("can't give argument to non-function %s" % '?')

    def eval_command(self, dot, cmd, final):
        first = cmd.args[0]
        if isinstance(first, Field):
            return self.eval_field_chain(dot, dot, first.idents, cmd.args, final)
        if isinstance(first, Chain):
            return self.eval_chain(dot, first, cmd.args, final)
        if isinstance(first, Ident):
            return self.eval_function(dot, first.name, cmd.args, final)
        if isinstance(first, Pipe):
            self.not_a_function(cmd.args, final)
            return self.eval_pipeline(dot, first)
        if isinstance(first, Variable):
            return self.eval_variable(dot, first, cmd.args, final)
        self.not_a_function(cmd.args, final)
        if isinstance(first, Dot):
            return dot
        if isinstance(first, Nil):
            self.error('nil is not a command')
        if isinstance(first, Const):
            return first.value
        self.error("can't evaluate command")

    def eval_chain(self, dot, chain, args, final):
        if isinstance(chain.node, Nil):
            self.error('indirection through explicit nil')
        recv = self.eval_arg(dot, chain.node)
        return self.eval_field_chain(dot, recv, chain.fields, args, final)

    def eval_variable(self, dot, var, args, final):
        value = self.var_value(var.idents[0])
        if len(var.idents) == 1:
            self.not_a_function(args, final)
            return value
        return self.eval_field_chain(dot, value, var.idents[1:], args, final)

    def eval_field_chain(self, dot, recv, idents, args, final):
        for ident in idents[:-1]:
            recv = self.eval_field(dot, ident, None, MISSING, recv)
        return self.eval_field(dot, idents[-1], args, final, recv)

    def eval_field(self, dot, name, args, final, recv):
        if recv is None:
            self.error('nil pointer evaluating interface {}.%s' % name)
        has_args = (args is not None and len(args) > 1) or final is not MISSING
        methods = getattr(recv, '_tmpl_methods', ())
        if name in methods:
            fn = getattr(recv, name)
            return self.eval_call(dot, fn, name, args, final)
        m = as_map(recv)
        if m is not None:
            if name in m:
                if has_args:
                    self.error('%s is not a method but has arguments' % name)
                return m[name]
            self.error('map has no entry for key "%s"' % name)
        fields = getattr(recv, '_tmpl_fields', None)
        if fields is not None and name in fields:
            if has_args:
                self.error('%s is not a method but has arguments' % name)
            return getattr(recv, name)
        self.error("can't evaluate field %s in type %s" % (name, go_type(recv)))

    def eval_function(self, dot, name, args, final):
        if name in ('and', 'or'):
            nodes = list(args[1:]) if args else []
            vals = [('n', n) for n in nodes]
            if final is not MISSING:
                vals.append(('v', final))
            if not vals:
                self.error('wrong number of args for %s: want at least 1 got 0' % name)
            v = None
            for kind, x in vals:
                v = self.eval_arg(dot, x) if kind == 'n' else x
                t = is_true(v)
                if name == 'and' and not t:
                    return v
                if name == 'or' and t:
                    return v
            return v
        fn = self.tmpl.funcs.get(name)
        if fn is None:
            self.error('"%s" is not a defined function' % name)
        return self.eval_call(dot, fn, name, args, final)

    def eval_call(self, dot, fn, name, args, final):
        argv = []
        if args:
            for a in args[1:]:
                argv.append(self.eval_arg(dot, a))
        if final is not MISSING:
            argv.append(final)
        try:
            return fn(*argv)
        except (TemplateError, _Break, _Continue):
            raise
        except TypeError as e:
            self.error('error calling %s: wrong number of args or bad argument types (%s)' % (name, e))
        except RecursionError:
            raise
        except Exception as e:
            self.error('error calling %s: %s' % (name, e))

    def eval_arg(self, dot, n):
        if isinstance(n, Dot):
            return dot
        if isinstance(n, Nil):
            return None
        if isinstance(n, Field):
            return self.eval_field_chain(dot, dot, n.idents, [n], MISSING)
        if isinstance(n, Variable):
            return self.eval_variable(dot, n, None, MISSING)
        if isinstance(n, Pipe):
            return self.eval_pipeline(dot, n)
        if isinstance(n, Ident):
            return self.eval_function(dot, n.name, [n], MISSING)
        if isinstance(n, Chain):
            return self.eval_chain(dot, n, None, MISSING)
        if isinstance(n, Const):
            return n.value
        self.error("can't handle argument")


class Template(object):
    def __init__(self, src, funcs=None):
        self.funcs = funcs if funcs is not None else FUNCS
        toks = Lexer(src).lex()
        p = Parser(toks, self.funcs)
        self.tree = p.parse()
        self.defs = p.defs

    def execute(self, data):
        out = []
        st = State(self, data)
        try:
            st.walk_list(data, self.tree, out)
        except (_Break, _Continue):
            raise TemplateError('break/continue outside range')
        except RecursionError:
            raise TemplateError('template too deeply nested')
        return ''.join(out)


_cache = {}
_cache_lock = threading.Lock()


def clean(src):
    return src.replace('\r\n', ' ').replace('\n', ' ').replace('\t', ' ')


def render(src, data):
    """Render a template string. Raises TemplateError on parse/exec errors."""
    src = clean(src)
    with _cache_lock:
        t = _cache.get(src)
    if t is None:
        try:
            t = Template(src)
        except RecursionError:
            raise TemplateError('template too deeply nested')
        with _cache_lock:
            if len(_cache) > 4096:
                _cache.clear()
            _cache[src] = t
    return t.execute(data)


# ===========================================================================
# hmock: YAML-driven HTTP mock server
# ===========================================================================

import datetime
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write('PyYAML is required: pip install -r requirements.txt\n')
    raise

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'warning': 30, 'error': 40}
LEVEL_NAMES = {10: 'debug', 20: 'info', 30: 'warning', 40: 'error'}
_log_level = 20
_log_lock = threading.Lock()


def set_log_level(name):
    global _log_level
    _log_level = LEVELS.get((name or 'info').strip().lower(), 20)


def log(level, msg, **fields):
    lv = LEVELS[level]
    if lv < _log_level:
        return
    rec = {'level': LEVEL_NAMES[lv], 'msg': msg,
           'time': datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()}
    for k, v in fields.items():
        rec[k] = v
    line = json.dumps(rec, default=str)
    with _log_lock:
        try:
            sys.stderr.write(line + '\n')
            sys.stderr.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------

_TOKEN_CHARS = set("!#$%&'*+-.^_`|~0123456789"
                   "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def canonical_header_key(key):
    if not key or any(c not in _TOKEN_CHARS for c in key):
        return key
    out = []
    upper = True
    for c in key:
        if upper and 'a' <= c <= 'z':
            c = c.upper()
        elif not upper and 'A' <= c <= 'Z':
            c = c.lower()
        out.append(c)
        upper = c == '-'
    return ''.join(out)


class Header(object):
    """Emulates Go's http.Header (map[string][]string with Get/Values)."""
    _go_type = 'http.Header'
    _tmpl_methods = ('Get', 'Values')

    def __init__(self, pairs=()):
        self._d = {}
        for k, v in pairs:
            self._d.setdefault(canonical_header_key(k), []).append(v)

    def _tmpl_map(self):
        return self._d

    def _tmpl_zero(self):
        return []

    def Get(self, key):
        if not isinstance(key, str):
            raise TemplateError('wrong type for value; expected string; got %s' % go_type(key))
        v = self._d.get(canonical_header_key(key))
        return v[0] if v else ''

    def Values(self, key):
        if not isinstance(key, str):
            raise TemplateError('wrong type for value; expected string; got %s' % go_type(key))
        return list(self._d.get(canonical_header_key(key), []))


class HTTPContext(object):
    """Request URL context: exposes path parameters and query values."""
    _go_type = 'echo.Context'
    _tmpl_methods = ('Param', 'ParamNames', 'ParamValues', 'QueryParam',
                     'QueryString', 'Path')

    def __init__(self, route, params, query):
        self._route = route
        self._params = params
        self._query = query

    def Param(self, name):
        return self._params.get(_str(name), '')

    def ParamNames(self):
        return list(self._params.keys())

    def ParamValues(self):
        return list(self._params.values())

    def QueryParam(self, name):
        v = urllib.parse.parse_qs(self._query, keep_blank_values=True).get(_str(name))
        return v[0] if v else ''

    def QueryString(self):
        return self._query

    def Path(self):
        return self._route


class Context(object):
    _go_type = 'openmock.Context'
    _tmpl_fields = ('HTTPHeader', 'HTTPBody', 'HTTPPath', 'HTTPQueryString',
                    'HTTPContext', 'HTTPParams')

    def __init__(self, header, body, path, query):
        self.HTTPHeader = header
        self.HTTPBody = body
        self.HTTPPath = path
        self.HTTPQueryString = query
        self.HTTPParams = {}
        self.HTTPContext = HTTPContext('', {}, query)

    def with_params(self, route, params):
        self.HTTPParams = params
        self.HTTPContext = HTTPContext(route, params, self.HTTPQueryString)
        return self


# ---------------------------------------------------------------------------
# Mock loading
# ---------------------------------------------------------------------------

class LoadError(Exception):
    pass


_DUR_UNITS = {'ns': 1e-9, 'us': 1e-6, 'µs': 1e-6, 'μs': 1e-6,
              'ms': 1e-3, 's': 1.0, 'm': 60.0, 'h': 3600.0}
_DUR_PART = re.compile(r'(\d+\.?\d*|\.\d+)(ns|us|µs|μs|ms|s|m|h)')


def parse_duration(v):
    """Parse a Go duration; returns seconds."""
    if isinstance(v, bool) or v is None:
        raise LoadError('invalid duration %r' % (v,))
    if isinstance(v, int):
        return v / 1e9
    if isinstance(v, float):
        raise LoadError('invalid duration %r' % (v,))
    s = str(v).strip()
    orig = s
    sign = 1
    if s[:1] in '+-':
        sign = -1 if s[0] == '-' else 1
        s = s[1:]
    if s == '0':
        return 0.0
    if not s:
        raise LoadError('invalid duration "%s"' % orig)
    total = 0.0
    pos = 0
    while pos < len(s):
        m = _DUR_PART.match(s, pos)
        if not m:
            raise LoadError('invalid duration "%s"' % orig)
        total += float(m.group(1)) * _DUR_UNITS[m.group(2)]
        pos = m.end()
    return sign * total


def _scalar_str(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float):
        return go_float(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


class Behavior(object):
    def __init__(self, key, kind, method, path, condition, actions, raw):
        self.key = key
        self.kind = kind
        self.method = method
        self.path = path
        self.segments = path.split('/') if path is not None else None
        self.condition = condition
        self.actions = actions
        self.raw = raw


def parse_mock(m, source):
    if not isinstance(m, dict):
        raise LoadError('%s: mock must be a mapping, got %s' % (source, type(m).__name__))
    if 'key' not in m or m.get('key') is None:
        raise LoadError('%s: key is required' % source)
    key = m['key']
    if not isinstance(key, str) or key == '':
        raise LoadError('%s: key must be a non-empty string' % source)
    kind = m.get('kind')
    if kind is None or kind == '':
        kind = 'Behavior'
    kind = str(kind)
    expect = m.get('expect') or {}
    if not isinstance(expect, dict):
        raise LoadError('%s: mock %s: expect must be a mapping' % (source, key))
    http = expect.get('http')
    method = path = None
    if http is not None:
        if not isinstance(http, dict):
            raise LoadError('%s: mock %s: expect.http must be a mapping' % (source, key))
        method = http.get('method')
        path = http.get('path')
        method = _scalar_str(method).upper() if method is not None else None
        if path is not None:
            path = _scalar_str(path)
            if not path.startswith('/'):
                path = '/' + path
    condition = expect.get('condition')
    condition = _scalar_str(condition) if condition is not None else ''

    raw_actions = m.get('actions') or []
    if not isinstance(raw_actions, list):
        raise LoadError('%s: mock %s: actions must be a list' % (source, key))
    actions = []
    replies = 0
    for a in raw_actions:
        if not isinstance(a, dict):
            raise LoadError('%s: mock %s: each action must be a mapping' % (source, key))
        for name, spec in a.items():
            if name == 'reply_http':
                replies += 1
                if replies > 1:
                    raise LoadError('%s: mock %s: only one reply_http action is allowed'
                                    % (source, key))
                if not isinstance(spec, dict):
                    raise LoadError('%s: mock %s: reply_http must be a mapping' % (source, key))
                sc = spec.get('status_code')
                if sc is None or isinstance(sc, bool):
                    raise LoadError('%s: mock %s: reply_http.status_code is required'
                                    % (source, key))
                try:
                    sc = int(sc)
                except (TypeError, ValueError):
                    raise LoadError('%s: mock %s: invalid status_code %r' % (source, key, sc))
                if sc < 100 or sc > 999:
                    raise LoadError('%s: mock %s: invalid status_code %d' % (source, key, sc))
                headers = spec.get('headers') or {}
                if not isinstance(headers, dict):
                    raise LoadError('%s: mock %s: reply_http.headers must be a mapping'
                                    % (source, key))
                headers = [(str(k), _scalar_str(v)) for k, v in headers.items()]
                body = _scalar_str(spec.get('body'))
                actions.append(('reply_http', {'status_code': sc, 'headers': headers,
                                               'body': body}))
            elif name == 'sleep':
                if not isinstance(spec, dict) or spec.get('duration') is None:
                    raise LoadError('%s: mock %s: sleep.duration is required' % (source, key))
                try:
                    d = parse_duration(spec.get('duration'))
                except LoadError as e:
                    raise LoadError('%s: mock %s: %s' % (source, key, e))
                actions.append(('sleep', {'duration': max(d, 0.0)}))
            else:
                log('warn', 'unsupported action ignored', key=key, action=str(name))
    return Behavior(key, kind, method, path, condition, actions, m)


def _walk_files(root):
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        log('warn', 'cannot read templates directory', dir=root, err=str(e))
        return
    for name in entries:
        p = os.path.join(root, name)
        if os.path.isdir(p):
            if os.path.islink(p):
                continue
            for f in _walk_files(p):
                yield f
        elif name.lower().endswith(('.yaml', '.yml')) and os.path.isfile(p):
            yield p


def load_mocks(root):
    if not os.path.isdir(root):
        log('warn', 'templates directory does not exist', dir=root)
        return []
    raw = []
    for path in _walk_files(root):
        try:
            with open(path, 'rb') as fh:
                docs = list(yaml.safe_load_all(fh.read().decode('utf-8-sig')))
        except Exception as e:
            raise LoadError('failed to parse %s: %s' % (path, e))
        for doc in docs:
            if doc is None:
                continue
            if isinstance(doc, list):
                for m in doc:
                    raw.append((m, path))
            else:
                raw.append((doc, path))
    ordered = {}
    for m, path in raw:
        b = parse_mock(m, path)
        if b.key in ordered:
            log('warn', 'duplicate template key, the latter one will override the former one',
                key=b.key, file=path)
        ordered[b.key] = b
        log('debug', 'loaded mock', key=b.key, file=path)
    return list(ordered.values())


# ---------------------------------------------------------------------------
# Matching / execution
# ---------------------------------------------------------------------------

def match_path(segments, path):
    psegs = path.split('/')
    params = {}
    for i, s in enumerate(segments):
        if s.startswith('*'):
            params[s[1:] or '*'] = '/'.join(psegs[i:])
            return params
        if i >= len(psegs):
            return None
        if s.startswith(':') and len(s) > 1:
            if psegs[i] == '':
                return None
            params[s[1:]] = psegs[i]
        elif s != psegs[i]:
            return None
    if len(psegs) != len(segments):
        return None
    return params


def condition_passes(b, ctx):
    if not b.condition:
        return True
    try:
        return render(b.condition, ctx) == 'true'
    except TemplateError as e:
        log('debug', 'condition render failed', key=b.key, err=str(e))
        return False
    except Exception as e:
        log('debug', 'condition render failed', key=b.key, err=repr(e))
        return False


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, addr, handler, behaviors):
        self.behaviors = behaviors
        ThreadingHTTPServer.__init__(self, addr, handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'hmock'
    sys_version = ''

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        log('debug', fmt % args)

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self._handle
        raise AttributeError(name)

    def _read_body(self):
        te = (self.headers.get('Transfer-Encoding') or '').lower()
        if 'chunked' in te:
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    break
                size = int(line.split(b';', 1)[0].strip() or b'0', 16)
                if size == 0:
                    while True:
                        t = self.rfile.readline(65537)
                        if not t or t in (b'\r\n', b'\n'):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)
            return b''.join(chunks)
        cl = self.headers.get('Content-Length')
        if cl:
            try:
                n = int(cl)
            except ValueError:
                n = 0
            if n > 0:
                return self.rfile.read(n)
        return b''

    def _send(self, status, headers, body):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v.replace('\r', ' ').replace('\n', ' '))
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)
        self.wfile.flush()

    def _handle(self):
        start = time.time()
        body_bytes = self._read_body()
        body = body_bytes.decode('utf-8', 'replace')
        raw_path = self.path
        if raw_path.startswith(('http://', 'https://')):
            u = urllib.parse.urlsplit(raw_path)
            raw_path = u.path + ('?' + u.query if u.query else '')
        path_part, _, query = raw_path.partition('?')
        path_part = path_part.split('#', 1)[0]
        decoded = urllib.parse.unquote(path_part) or '/'
        header = Header(self.headers.items())
        ctx = Context(header, body, raw_path, query)
        method = self.command.upper()

        status = 404
        res_body = b'not found'
        sent = False
        matched_key = None
        try:
            for b in self.server.behaviors:
                if b.kind != 'Behavior' or b.method is None or b.segments is None:
                    continue
                if b.method != method:
                    continue
                params = match_path(b.segments, decoded)
                if params is None:
                    continue
                ctx.with_params(b.path, params)
                if not condition_passes(b, ctx):
                    continue
                matched_key = b.key
                status, res_body, sent = self._perform(b, ctx)
                break
            if matched_key is None:
                self._send(404, [('Content-Type', 'text/plain; charset=UTF-8'),
                                 ('Content-Length', '9')], b'not found')
                sent = True
            elif not sent:
                status, res_body = 200, b''
                self._send(200, [('Content-Length', '0')], b'')
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        log('info', 'http request', http_path=decoded, http_method=method,
            http_host=self.headers.get('Host', ''), http_req=body,
            http_res=res_body.decode('utf-8', 'replace'), http_status=status,
            key=matched_key, latency_ms=round((time.time() - start) * 1000, 3))

    def _perform(self, b, ctx):
        status, res_body, sent = 200, b'', False
        for typ, spec in b.actions:
            if typ == 'reply_http':
                try:
                    out_headers = []
                    for k, v in spec['headers']:
                        out_headers.append((k, render(v, ctx)))
                    rbody = render(spec['body'], ctx).encode('utf-8')
                except Exception as e:
                    log('error', 'failed to render template for http', key=b.key, err=str(e))
                    eb = b'{"message":"Internal Server Error"}\n'
                    self._send(500, [('Content-Type', 'application/json; charset=UTF-8'),
                                     ('Content-Length', str(len(eb)))], eb)
                    return 500, eb, True
                headers = [(k, v) for k, v in out_headers if k.lower() != 'content-length']
                if not any(k.lower() == 'content-type' for k, _ in headers):
                    headers.insert(0, ('Content-Type', 'application/json'))
                headers.append(('Content-Length', str(len(rbody))))
                self._send(spec['status_code'], headers, rbody)
                status, res_body, sent = spec['status_code'], rbody, True
            elif typ == 'sleep':
                time.sleep(spec['duration'])
        return status, res_body, sent


def main():
    set_log_level(os.environ.get('HM_LOG_LEVEL', 'info'))
    tdir = os.environ.get('HM_TEMPLATES_DIR', './templates')
    host = os.environ.get('HM_HTTP_HOST', '0.0.0.0')
    try:
        port = int(os.environ.get('HM_HTTP_PORT', '9999'))
    except ValueError:
        log('error', 'invalid HM_HTTP_PORT', value=os.environ.get('HM_HTTP_PORT'))
        sys.exit(1)
    try:
        behaviors = load_mocks(tdir)
    except LoadError as e:
        log('error', 'failed to load mocks', err=str(e))
        sys.exit(1)
    log('info', 'mocks loaded', count=len(behaviors), dir=tdir)
    try:
        server = MockServer((host, port), Handler, behaviors)
    except OSError as e:
        log('error', 'failed to start http server', err=str(e))
        sys.exit(1)
    log('info', 'starting http server', host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
