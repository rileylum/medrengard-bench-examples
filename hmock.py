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
# Rich template helpers: XPath (JSON / XML), gjson paths, regex, crypto
# ---------------------------------------------------------------------------

import hashlib
import hmac as _hmac
import json as _json
import re as _re


class XNode(object):
    """Generic tree node used for XPath evaluation over JSON and XML."""
    __slots__ = ('type', 'name', 'prefix', 'value', 'parent', 'children', 'attrs', 'order')

    def __init__(self, type_, name='', value='', parent=None):
        self.type = type_          # 'root', 'element', 'text', 'attr', 'comment'
        self.name = name
        self.prefix = ''
        self.value = value
        self.parent = parent
        self.children = []
        self.attrs = []
        self.order = 0

    def add(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def inner_text(self):
        if self.type in ('text', 'attr', 'comment'):
            return self.value
        out = []
        stack = [self]
        while stack:
            n = stack.pop()
            if n.type == 'text':
                out.append(n.value)
            elif n.type in ('root', 'element'):
                stack.extend(reversed(n.children))
        return ''.join(out)


def _number_order(root):
    i = 0
    stack = [root]
    while stack:
        n = stack.pop()
        n.order = i
        i += 1
        for a in n.attrs:
            a.order = i
            i += 1
        stack.extend(reversed(n.children))
    return root


# -- JSON document (jsonquery-like) -------------------------------------------

def _json_text(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if v is None:
        return ''
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _format_float_f(v)
    return v


def _format_float_f(f):
    """strconv.FormatFloat(f, 'f', -1, 64)."""
    if math.isnan(f):
        return 'NaN'
    if math.isinf(f):
        return '+Inf' if f > 0 else '-Inf'
    if f == int(f) and abs(f) < 1e21:
        return str(int(f)) if f != 0 or math.copysign(1, f) > 0 else '-0'
    d = Decimal(repr(f))
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s


def _json_strict_loads(s):
    def bad_const(c):
        raise ValueError('invalid JSON constant %s' % c)
    return _json.loads(s, parse_constant=bad_const)


def _json_build(v, parent):
    if isinstance(v, dict):
        for k, vv in v.items():
            n = parent.add(XNode('element', k))
            _json_build(vv, n)
    elif isinstance(v, list):
        for vv in v:
            n = parent.add(XNode('element', ''))
            _json_build(vv, n)
    else:
        parent.add(XNode('text', value=_json_text(v)))


def parse_json_doc(s):
    try:
        v = _json_strict_loads(s)
    except ValueError as e:
        raise TemplateError('invalid json: %s' % e)
    root = XNode('root')
    _json_build(v, root)
    return _number_order(root)


# -- XML document (xmlquery-like) ---------------------------------------------

def _split_qname(q):
    if ':' in q:
        p, _, l = q.partition(':')
        return p, l
    return '', q


def parse_xml_doc(s):
    import xml.parsers.expat as expat
    root = XNode('root')
    cur = [root]
    p = expat.ParserCreate()
    p.buffer_text = True
    try:
        p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except Exception:
        pass

    def start(name, attrs):
        pre, local = _split_qname(name)
        n = XNode('element', local)
        n.prefix = pre
        if isinstance(attrs, dict):
            items = attrs.items()
        else:
            items = zip(attrs[0::2], attrs[1::2])
        for k, v in items:
            ap, al = _split_qname(k)
            a = XNode('attr', al, v, n)
            a.prefix = ap
            n.attrs.append(a)
        cur[-1].add(n)
        cur.append(n)

    def end(name):
        cur.pop()

    def chars(data):
        c = cur[-1].children
        if c and c[-1].type == 'text':
            c[-1].value += data
        else:
            cur[-1].add(XNode('text', value=data))

    def comment(data):
        cur[-1].add(XNode('comment', value=data))

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    p.CommentHandler = comment
    try:
        p.Parse(s.encode('utf-8'), True)
    except expat.ExpatError as e:
        raise TemplateError('invalid xml: %s' % e)
    return _number_order(root)


# -- XPath ----------------------------------------------------------------------

_XP_TOKEN = _re.compile(r'''
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d*)?|\.\d+)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<op>//|::|\.\.|!=|<=|>=|[/()\[\]@,|=<>.*+\-$])
  | (?P<name>[A-Za-z_À-￿][\w.\-·-￿]*(?::(?:[A-Za-z_À-￿][\w.\-·-￿]*|\*))?)
''', _re.X)

_AXES = {'child', 'descendant', 'descendant-or-self', 'parent', 'ancestor',
         'ancestor-or-self', 'following-sibling', 'preceding-sibling',
         'following', 'preceding', 'self', 'attribute', 'namespace'}


class XPathError(TemplateError):
    pass


def _xp_tokenize(expr):
    toks = []
    pos = 0
    while pos < len(expr):
        m = _XP_TOKEN.match(expr, pos)
        if not m:
            raise XPathError('%s has an invalid token' % expr)
        pos = m.end()
        kind = m.lastgroup
        if kind == 'ws':
            continue
        val = m.group(kind)
        if kind == 'name' and val.endswith(':*') is False and ':' in val:
            pass
        toks.append((kind, val))
    # disambiguate '*' (multiply vs name test) and operator names
    out = []
    for kind, val in toks:
        prev = out[-1] if out else None
        prev_is_operand = prev is not None and not (
            prev[0] == 'op' and prev[1] in ('@', '::', '(', '[', ',', '/', '//', '|', '+', '-',
                                            '=', '!=', '<', '<=', '>', '>=')
        ) and not (prev[0] == 'opname')
        if kind == 'op' and val == '*' and prev_is_operand:
            out.append(('op', 'mul'))
        elif kind == 'name' and val in ('and', 'or', 'div', 'mod') and prev_is_operand:
            out.append(('opname', val))
        else:
            out.append((kind, val))
    return out


class _XP(object):
    """Recursive-descent XPath 1.0 parser producing a small AST of tuples."""

    def __init__(self, expr):
        self.expr = expr
        self.toks = _xp_tokenize(expr)
        self.i = 0

    def peek(self, off=0):
        j = self.i + off
        return self.toks[j] if j < len(self.toks) else (None, None)

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def accept(self, val, kind=None):
        k, v = self.peek()
        if v == val and (kind is None or k == kind):
            self.i += 1
            return True
        return False

    def expect(self, val):
        if not self.accept(val):
            raise XPathError('%s has an invalid token' % self.expr)

    def parse(self):
        if not self.toks:
            raise XPathError('expression is empty')
        e = self.or_expr()
        if self.i != len(self.toks):
            raise XPathError('%s has an invalid token' % self.expr)
        return e

    def _binary(self, sub, ops, kinds=('op', 'opname')):
        left = sub()
        while True:
            k, v = self.peek()
            if k in kinds and v in ops:
                self.i += 1
                left = ('bin', v, left, sub())
            else:
                return left

    def or_expr(self):
        return self._binary(self.and_expr, ('or',))

    def and_expr(self):
        return self._binary(self.eq_expr, ('and',))

    def eq_expr(self):
        return self._binary(self.rel_expr, ('=', '!='))

    def rel_expr(self):
        return self._binary(self.add_expr, ('<', '<=', '>', '>='))

    def add_expr(self):
        return self._binary(self.mul_expr, ('+', '-'))

    def mul_expr(self):
        return self._binary(self.unary_expr, ('mul', 'div', 'mod'))

    def unary_expr(self):
        if self.accept('-', 'op'):
            return ('neg', self.unary_expr())
        return self.union_expr()

    def union_expr(self):
        left = self.path_expr()
        while self.accept('|', 'op'):
            left = ('union', left, self.path_expr())
        return left

    def path_expr(self):
        k, v = self.peek()
        if k == 'op' and v in ('/', '//'):
            return self.location_path()
        # filter expression?
        is_primary = (k in ('num', 'str') or (k == 'op' and v in ('(', '$')) or
                      (k == 'name' and self.peek(1) == ('op', '(') and
                       v not in ('node', 'text', 'comment', 'processing-instruction')))
        if is_primary:
            e = self.primary()
            while self.peek() == ('op', '['):
                e = ('filter', e, self.predicate())
            k2, v2 = self.peek()
            if k2 == 'op' and v2 in ('/', '//'):
                steps = self.relative_steps()
                return ('path', e, steps)
            return e
        return self.location_path()

    def primary(self):
        k, v = self.next()
        if k == 'num':
            return ('num', float(v))
        if k == 'str':
            return ('str', v[1:-1])
        if v == '(':
            e = self.or_expr()
            self.expect(')')
            return e
        if v == '$':
            k2, v2 = self.next()
            return ('var', v2)
        if k == 'name':
            self.expect('(')
            args = []
            if not self.accept(')'):
                args.append(self.or_expr())
                while self.accept(','):
                    args.append(self.or_expr())
                self.expect(')')
            return ('call', v, args)
        raise XPathError('%s has an invalid token' % self.expr)

    def location_path(self):
        k, v = self.peek()
        if v == '/':
            self.i += 1
            k2, v2 = self.peek()
            if k2 is None or (k2 == 'op' and v2 in (')', ']', '|', ',')) or k2 == 'opname' or \
                    (k2 == 'op' and v2 in ('=', '!=', '<', '>', '<=', '>=')):
                return ('path', ('rootnode',), [])
            return ('path', ('rootnode',), self.steps())
        if v == '//':
            self.i += 1
            steps = [('descendant-or-self', ('node',), [])] + self.steps()
            return ('path', ('rootnode',), steps)
        return ('path', None, self.steps())

    def relative_steps(self):
        steps = []
        while True:
            k, v = self.peek()
            if v == '/':
                self.i += 1
            elif v == '//':
                self.i += 1
                steps.append(('descendant-or-self', ('node',), []))
            else:
                return steps
            steps.append(self.step())

    def steps(self):
        steps = [self.step()]
        return steps + self.relative_steps()

    def step(self):
        if self.accept('.', 'op'):
            return ('self', ('node',), [])
        if self.accept('..', 'op'):
            return ('parent', ('node',), [])
        axis = 'child'
        if self.accept('@', 'op'):
            axis = 'attribute'
        else:
            k, v = self.peek()
            if k == 'name' and v in _AXES and self.peek(1) == ('op', '::'):
                self.i += 2
                axis = v
        test = self.node_test()
        preds = []
        while self.peek() == ('op', '['):
            preds.append(self.predicate())
        return (axis, test, preds)

    def node_test(self):
        k, v = self.next()
        if k == 'op' and v == '*':
            return ('name', '', '*')
        if k in ('name', 'opname'):
            if v in ('node', 'text', 'comment', 'processing-instruction') and self.peek() == ('op', '('):
                self.i += 1
                if v == 'processing-instruction' and self.peek()[0] == 'str':
                    self.i += 1
                self.expect(')')
                return (v,)
            pre, local = _split_qname(v)
            return ('name', pre, local)
        raise XPathError('%s has an invalid token' % self.expr)

    def predicate(self):
        self.expect('[')
        e = self.or_expr()
        self.expect(']')
        return e


def _xp_string(v):
    if isinstance(v, list):
        return v[0].inner_text() if v else ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, float):
        if math.isnan(v):
            return 'NaN'
        if v == int(v):
            return str(int(v))
        return repr(v)
    return v


def _xp_number(v):
    if isinstance(v, float):
        return v
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    s = _xp_string(v).strip()
    try:
        return float(s)
    except ValueError:
        return float('nan')


def _xp_bool(v):
    if isinstance(v, list):
        return bool(v)
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return v != 0 and not math.isnan(v)
    return v != ''


def _xp_compare(op, a, b):
    def cmp1(x, y):
        if op in ('=', '!='):
            if isinstance(x, bool) or isinstance(y, bool):
                r = _xp_bool(x) == _xp_bool(y)
            elif isinstance(x, float) or isinstance(y, float):
                r = _xp_number(x) == _xp_number(y)
            else:
                r = _xp_string(x) == _xp_string(y)
            return r if op == '=' else not r
        nx, ny = _xp_number(x), _xp_number(y)
        return {'<': nx < ny, '<=': nx <= ny, '>': nx > ny, '>=': nx >= ny}[op]

    if isinstance(a, list) or isinstance(b, list):
        if isinstance(a, list) and isinstance(b, list):
            return any(cmp1(x.inner_text(), y.inner_text()) for x in a for y in b)
        if isinstance(a, list):
            if isinstance(b, bool):
                return cmp1(_xp_bool(a), b)
            if isinstance(b, float):
                return any(cmp1(_xp_number(x.inner_text()), b) for x in a)
            return any(cmp1(x.inner_text(), b) for x in a)
        if isinstance(a, bool):
            return cmp1(a, _xp_bool(b))
        if isinstance(a, float):
            return any(cmp1(a, _xp_number(y.inner_text())) for y in b)
        return any(cmp1(a, y.inner_text()) for y in b)
    return cmp1(a, b)


def _xp_axis(n, axis):
    if axis == 'child':
        return list(n.children)
    if axis == 'attribute':
        return list(n.attrs)
    if axis == 'self':
        return [n]
    if axis == 'parent':
        return [n.parent] if n.parent is not None else []
    if axis in ('ancestor', 'ancestor-or-self'):
        out = [n] if axis == 'ancestor-or-self' else []
        p = n.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out
    if axis in ('descendant', 'descendant-or-self'):
        out = [n] if axis == 'descendant-or-self' else []
        stack = list(reversed(n.children))
        while stack:
            c = stack.pop()
            out.append(c)
            stack.extend(reversed(c.children))
        return out
    if axis in ('following-sibling', 'preceding-sibling'):
        if n.parent is None or n.type == 'attr':
            return []
        sibs = n.parent.children
        idx = next(i for i, s in enumerate(sibs) if s is n)
        if axis == 'following-sibling':
            return sibs[idx + 1:]
        return list(reversed(sibs[:idx]))
    if axis in ('following', 'preceding'):
        root = n
        while root.parent is not None:
            root = root.parent
        allnodes = _xp_axis(root, 'descendant')
        anc = set(id(a) for a in _xp_axis(n, 'ancestor'))
        if axis == 'following':
            last = n
            while last.children:
                last = last.children[-1]
            return [x for x in allnodes if x.order > last.order]
        return list(reversed([x for x in allnodes if x.order < n.order and id(x) not in anc]))
    return []


def _xp_test(n, test, axis):
    t = test[0]
    if t == 'node':
        return True
    if t == 'text':
        return n.type == 'text'
    if t == 'comment':
        return n.type == 'comment'
    if t == 'processing-instruction':
        return False
    _, pre, local = test
    want = 'attr' if axis == 'attribute' else 'element'
    if n.type != want:
        return False
    if local != '*' and n.name != local:
        return False
    if pre and n.prefix != pre:
        return False
    return True


_REVERSE_AXES = ('ancestor', 'ancestor-or-self', 'preceding', 'preceding-sibling', 'parent')


class _XPCtx(object):
    __slots__ = ('node', 'pos', 'size')

    def __init__(self, node, pos=1, size=1):
        self.node = node
        self.pos = pos
        self.size = size


def _xp_doc_order(nodes):
    seen = {}
    for n in nodes:
        seen[id(n)] = n
    return sorted(seen.values(), key=lambda x: x.order)


def _xp_apply_preds(nodes, preds):
    for p in preds:
        size = len(nodes)
        kept = []
        for i, n in enumerate(nodes):
            r = _xp_eval(p, _XPCtx(n, i + 1, size))
            if isinstance(r, float):
                ok = r == i + 1
            else:
                ok = _xp_bool(r)
            if ok:
                kept.append(n)
        nodes = kept
    return nodes


def _xp_steps(nodes, steps):
    for axis, test, preds in steps:
        out = []
        for n in nodes:
            cand = [c for c in _xp_axis(n, axis) if _xp_test(c, test, axis)]
            out.extend(_xp_apply_preds(cand, preds))
        nodes = _xp_doc_order(out)
    return nodes


def _xp_eval(e, ctx):
    t = e[0]
    if t == 'num':
        return e[1]
    if t == 'str':
        return e[1]
    if t == 'rootnode':
        n = ctx.node
        while n.parent is not None:
            n = n.parent
        return [n]
    if t == 'path':
        if e[1] is None:
            base = [ctx.node]
        else:
            base = _xp_eval(e[1], ctx)
            if not isinstance(base, list):
                raise XPathError('expression must evaluate to a node-set')
        return _xp_steps(base, e[2])
    if t == 'filter':
        base = _xp_eval(e[1], ctx)
        if not isinstance(base, list):
            raise XPathError('expression must evaluate to a node-set')
        return _xp_apply_preds(base, [e[2]])
    if t == 'union':
        a, b = _xp_eval(e[1], ctx), _xp_eval(e[2], ctx)
        if not isinstance(a, list) or not isinstance(b, list):
            raise XPathError('union operands must be node-sets')
        return _xp_doc_order(a + b)
    if t == 'neg':
        return -_xp_number(_xp_eval(e[1], ctx))
    if t == 'var':
        raise XPathError('variables are not supported')
    if t == 'bin':
        op = e[1]
        if op == 'or':
            return _xp_bool(_xp_eval(e[2], ctx)) or _xp_bool(_xp_eval(e[3], ctx))
        if op == 'and':
            return _xp_bool(_xp_eval(e[2], ctx)) and _xp_bool(_xp_eval(e[3], ctx))
        a, b = _xp_eval(e[2], ctx), _xp_eval(e[3], ctx)
        if op in ('=', '!=', '<', '<=', '>', '>='):
            return _xp_compare(op, a, b)
        x, y = _xp_number(a), _xp_number(b)
        if op == '+':
            return x + y
        if op == '-':
            return x - y
        if op == 'mul':
            return x * y
        if op == 'div':
            if y == 0:
                if x == 0 or math.isnan(x):
                    return float('nan')
                return math.copysign(float('inf'), x) * math.copysign(1, y)
            return x / y
        if op == 'mod':
            if y == 0:
                return float('nan')
            return math.fmod(x, y)
    if t == 'call':
        return _xp_call(e[1], e[2], ctx)
    raise XPathError('unsupported expression')


def _xp_call(name, args, ctx):
    def arg(i):
        return _xp_eval(args[i], ctx)

    def sarg(i):
        return _xp_string(arg(i)) if len(args) > i else ctx.node.inner_text()

    if name == 'last':
        return float(ctx.size)
    if name == 'position':
        return float(ctx.pos)
    if name == 'count':
        v = arg(0)
        if not isinstance(v, list):
            raise XPathError('count() requires a node-set')
        return float(len(v))
    if name in ('name', 'local-name'):
        if args:
            v = arg(0)
            n = v[0] if isinstance(v, list) and v else None
        else:
            n = ctx.node
        if n is None or n.type not in ('element', 'attr'):
            return ''
        if name == 'name' and n.prefix:
            return n.prefix + ':' + n.name
        return n.name
    if name == 'namespace-uri':
        return ''
    if name == 'string':
        return sarg(0)
    if name == 'concat':
        return ''.join(_xp_string(arg(i)) for i in range(len(args)))
    if name == 'contains':
        return _xp_string(arg(1)) in _xp_string(arg(0))
    if name == 'starts-with':
        return _xp_string(arg(0)).startswith(_xp_string(arg(1)))
    if name == 'ends-with':
        return _xp_string(arg(0)).endswith(_xp_string(arg(1)))
    if name == 'substring-before':
        a, b = _xp_string(arg(0)), _xp_string(arg(1))
        i = a.find(b)
        return a[:i] if i >= 0 else ''
    if name == 'substring-after':
        a, b = _xp_string(arg(0)), _xp_string(arg(1))
        i = a.find(b)
        return a[i + len(b):] if i >= 0 else ''
    if name == 'substring':
        s = _xp_string(arg(0))
        start = _xp_number(arg(1))
        if math.isnan(start):
            return ''
        start = int(round(start))
        if len(args) > 2:
            ln = _xp_number(arg(2))
            if math.isnan(ln):
                return ''
            end = start + int(round(ln))
        else:
            end = len(s) + 1
        start = max(start, 1)
        return s[start - 1:max(end - 1, start - 1)]
    if name == 'string-length':
        return float(len(sarg(0)))
    if name == 'normalize-space':
        return ' '.join(sarg(0).split())
    if name == 'translate':
        s, a, b = _xp_string(arg(0)), _xp_string(arg(1)), _xp_string(arg(2))
        out = []
        for c in s:
            i = a.find(c)
            if i < 0:
                out.append(c)
            elif i < len(b):
                out.append(b[i])
        return ''.join(out)
    if name in ('upper-case', 'lower-case'):
        s = _xp_string(arg(0))
        return s.upper() if name == 'upper-case' else s.lower()
    if name == 'not':
        return not _xp_bool(arg(0))
    if name == 'true':
        return True
    if name == 'false':
        return False
    if name == 'boolean':
        return _xp_bool(arg(0))
    if name == 'number':
        return _xp_number(arg(0)) if args else _xp_number(ctx.node.inner_text())
    if name == 'sum':
        v = arg(0)
        if not isinstance(v, list):
            raise XPathError('sum() requires a node-set')
        return float(sum(_xp_number(x.inner_text()) for x in v))
    if name == 'floor':
        return float(math.floor(_xp_number(arg(0))))
    if name == 'ceiling':
        return float(math.ceil(_xp_number(arg(0))))
    if name == 'round':
        x = _xp_number(arg(0))
        if math.isnan(x) or math.isinf(x):
            return x
        return float(math.floor(x + 0.5))
    raise XPathError('%s is not a supported function' % name)


_xp_cache = {}


def xpath_find_one(doc, expr):
    ast = _xp_cache.get(expr)
    if ast is None:
        ast = _XP(expr).parse()
        if len(_xp_cache) > 1024:
            _xp_cache.clear()
        _xp_cache[expr] = ast
    r = _xp_eval(ast, _XPCtx(doc))
    if isinstance(r, list):
        return r[0] if r else None
    return None


def tf_json_path(expr, data):
    expr, data = _str(expr), _str(data)
    if data == '':
        return ''
    doc = parse_json_doc(data)
    n = xpath_find_one(doc, expr)
    return n.inner_text() if n is not None else ''


def tf_xml_path(expr, data):
    expr, data = _str(expr), _str(data)
    if data == '':
        return ''
    doc = parse_xml_doc(data)
    n = xpath_find_one(doc, expr)
    return n.inner_text() if n is not None else ''


# -- gjson ----------------------------------------------------------------------

class GVal(object):
    """A JSON value that remembers its raw source text."""
    __slots__ = ('kind', 'value', 'raw')

    def __init__(self, kind, value, raw):
        self.kind = kind    # 'obj' (list of (k, GVal)), 'arr' (list), 'str', 'num', 'true', 'false', 'null'
        self.value = value
        self.raw = raw


_G_WS = ' \t\r\n'
_G_NUM = _re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?')


class _GParser(object):
    def __init__(self, s):
        self.s = s
        self.i = 0

    def ws(self):
        s, i = self.s, self.i
        while i < len(s) and s[i] in _G_WS:
            i += 1
        self.i = i

    def value(self):
        self.ws()
        s, i = self.s, self.i
        if i >= len(s):
            raise ValueError('unexpected end')
        c = s[i]
        if c == '{':
            self.i += 1
            items = []
            self.ws()
            if self.s[self.i:self.i + 1] == '}':
                self.i += 1
            else:
                while True:
                    self.ws()
                    if self.s[self.i:self.i + 1] != '"':
                        raise ValueError('expected key')
                    k = self.string().value
                    self.ws()
                    if self.s[self.i:self.i + 1] != ':':
                        raise ValueError('expected colon')
                    self.i += 1
                    v = self.value()
                    items.append((k, v))
                    self.ws()
                    c2 = self.s[self.i:self.i + 1]
                    self.i += 1
                    if c2 == '}':
                        break
                    if c2 != ',':
                        raise ValueError('expected comma')
            return GVal('obj', items, s[i:self.i])
        if c == '[':
            self.i += 1
            items = []
            self.ws()
            if self.s[self.i:self.i + 1] == ']':
                self.i += 1
            else:
                while True:
                    items.append(self.value())
                    self.ws()
                    c2 = self.s[self.i:self.i + 1]
                    self.i += 1
                    if c2 == ']':
                        break
                    if c2 != ',':
                        raise ValueError('expected comma')
            return GVal('arr', items, s[i:self.i])
        if c == '"':
            return self.string()
        for lit in ('true', 'false', 'null'):
            if s.startswith(lit, i):
                self.i = i + len(lit)
                return GVal(lit, None, lit)
        m = _G_NUM.match(s, i)
        if m:
            self.i = m.end()
            return GVal('num', float(m.group(0)), m.group(0))
        raise ValueError('invalid character')

    def string(self):
        s, i = self.s, self.i
        j = i + 1
        while True:
            if j >= len(s):
                raise ValueError('unterminated string')
            c = s[j]
            if c == '"':
                break
            if c == '\\':
                j += 2
                continue
            if ord(c) < 0x20:
                raise ValueError('control character in string')
            j += 1
        raw = s[i:j + 1]
        self.i = j + 1
        return GVal('str', _json.loads(raw), raw)


def gjson_parse(s):
    p = _GParser(s)
    v = p.value()
    p.ws()
    if p.i != len(s):
        raise ValueError('trailing data')
    return v


def _g_split_path(path):
    """Split a gjson path into components, honoring escapes and #(...) queries."""
    comps = []
    cur = []
    i = 0
    depth = 0
    while i < len(path):
        c = path[i]
        if c == '\\' and i + 1 < len(path):
            cur.append('\\' + path[i + 1])
            i += 2
            continue
        if depth == 0 and c == '.':
            comps.append(''.join(cur))
            cur = []
            i += 1
            continue
        if c == '(':
            depth += 1
        elif c == ')' and depth > 0:
            depth -= 1
        elif c == '"' and depth > 0:
            j = i + 1
            while j < len(path) and path[j] != '"':
                if path[j] == '\\':
                    j += 1
                j += 1
            cur.append(path[i:j + 1])
            i = j + 1
            continue
        cur.append(c)
        i += 1
    comps.append(''.join(cur))
    return comps


def _g_unescape(comp):
    out = []
    i = 0
    while i < len(comp):
        if comp[i] == '\\' and i + 1 < len(comp):
            out.append(comp[i + 1])
            i += 2
        else:
            out.append(comp[i])
            i += 1
    return ''.join(out)


def _g_wild_match(pattern, s):
    """Match gjson wildcard (* and ?) pattern with escapes."""
    rx = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '\\' and i + 1 < len(pattern):
            rx.append(_re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == '*':
            rx.append('.*')
        elif c == '?':
            rx.append('.')
        else:
            rx.append(_re.escape(c))
        i += 1
    return _re.fullmatch(''.join(rx), s, _re.S) is not None


def _g_has_wild(comp):
    i = 0
    while i < len(comp):
        if comp[i] == '\\':
            i += 2
            continue
        if comp[i] in '*?':
            return True
        i += 1
    return False


def _g_str(v):
    if v.kind == 'str':
        return v.value
    if v.kind == 'num':
        r = v.raw
        body = r[1:] if r.startswith('-') else r
        if body.isdigit():
            return r
        return _format_float_f(v.value)
    if v.kind == 'true':
        return 'true'
    if v.kind == 'false':
        return 'false'
    if v.kind == 'null':
        return ''
    return v.raw


_G_QUERY = _re.compile(r'^\s*(.*?)\s*(==|!=|<=|>=|%|!%|<|>|=)\s*(.*?)\s*$', _re.S)


def _g_query_match(elem, q):
    m = None
    # find an operator outside of quotes
    m = _G_QUERY.match(q)
    if not m:
        r = _g_get(elem, _g_split_path(q.strip())) if q.strip() else elem
        return r is not None
    lhs, op, rhs = m.group(1), m.group(2), m.group(3)
    if op == '=':
        op = '=='
    left = _g_get(elem, _g_split_path(lhs)) if lhs else elem
    if left is None:
        return False
    try:
        rv = gjson_parse(rhs) if rhs else None
    except ValueError:
        rv = GVal('str', rhs, rhs)
    if rv is None:
        return False
    if op in ('%', '!%'):
        if left.kind != 'str' or rv.kind != 'str':
            return False
        r = _g_wild_match(rv.value, left.value)
        return r if op == '%' else not r
    if left.kind == 'str' and rv.kind == 'str':
        a, b = left.value, rv.value
    elif left.kind == 'num' and rv.kind == 'num':
        a, b = left.value, rv.value
    elif left.kind in ('true', 'false') and rv.kind in ('true', 'false'):
        a, b = left.kind == 'true', rv.kind == 'true'
    else:
        if op == '!=':
            return True
        return False
    return {'==': a == b, '!=': a != b, '<': a < b, '<=': a <= b,
            '>': a > b, '>=': a >= b}[op]


def _g_make_array(vals):
    raw = '[' + ','.join(v.raw for v in vals) + ']'
    return GVal('arr', vals, raw)


def _g_get(v, comps):
    if not comps:
        return v
    comp, rest = comps[0], comps[1:]
    if comp == '@this':
        return _g_get(v, rest)
    if comp.startswith('#(') and (comp.endswith(')') or comp.endswith(')#')):
        if v.kind != 'arr':
            return None
        allm = comp.endswith(')#')
        q = comp[2:-2] if allm else comp[2:-1]
        matches = [e for e in v.value if _g_query_match(e, q)]
        if allm:
            res = []
            for e in matches:
                r = _g_get(e, rest)
                if r is not None:
                    res.append(r)
            return _g_make_array(res)
        if not matches:
            return None
        return _g_get(matches[0], rest)
    if comp == '#':
        if v.kind != 'arr':
            return None
        if not rest:
            n = len(v.value)
            return GVal('num', float(n), str(n))
        res = []
        for e in v.value:
            r = _g_get(e, rest)
            if r is not None:
                res.append(r)
        return _g_make_array(res)
    if v.kind == 'obj':
        if _g_has_wild(comp):
            for k, cv in v.value:
                if _g_wild_match(comp, k):
                    return _g_get(cv, rest)
            return None
        key = _g_unescape(comp)
        for k, cv in v.value:
            if k == key:
                return _g_get(cv, rest)
        return None
    if v.kind == 'arr':
        if comp.isdigit():
            idx = int(comp)
            if idx < len(v.value):
                return _g_get(v.value[idx], rest)
        return None
    return None


def gjson_get(data, path):
    root = gjson_parse(data)
    if path == '':
        return None
    # pipe operator: a|b applies b to the result of a
    parts = []
    depth = 0
    cur = []
    i = 0
    while i < len(path):
        c = path[i]
        if c == '\\' and i + 1 < len(path):
            cur.append(path[i:i + 2])
            i += 2
            continue
        if c == '(':
            depth += 1
        elif c == ')' and depth:
            depth -= 1
        if c == '|' and depth == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append(''.join(cur))
    v = root
    for p in parts:
        if v is None:
            return None
        v = _g_get(v, _g_split_path(p))
    return v


def tf_gjson_path(expr, data):
    expr, data = _str(expr), _str(data)
    if data == '':
        return ''
    try:
        gjson_parse(data)
    except (ValueError, IndexError):
        raise TemplateError('invalid json %s' % data)
    r = gjson_get(data, expr)
    return _g_str(r) if r is not None else ''


# -- misc helpers -----------------------------------------------------------------

def tf_uuidv5(data):
    return str(uuid.uuid5(uuid.NAMESPACE_OID, strval(data)))


_re_cache = {}


def _go_regex(pattern):
    r = _re_cache.get(pattern)
    if r is None:
        p = pattern
        # Go/RE2 named groups (?<name>...) and (?P<name>...)
        p = _re.sub(r'\(\?<(?=[A-Za-z_])', '(?P<', p)
        p = p.replace('\\z', '\\Z')
        try:
            r = _re.compile(p)
        except _re.error as e:
            raise TemplateError('regexp: Compile(%s): %s' % (go_quote(pattern), e))
        if len(_re_cache) > 1024:
            _re_cache.clear()
        _re_cache[pattern] = r
    return r


def tf_regex_find_all_submatch(pattern, s):
    r = _go_regex(_str(pattern))
    m = r.search(_str(s))
    if m is None:
        return []
    return [m.group(0)] + [g if g is not None else '' for g in m.groups()]


def tf_regex_find_first_submatch(pattern, s):
    m = tf_regex_find_all_submatch(pattern, s)
    if len(m) > 1:
        return m[1]
    return ''


def tf_hmac_sha256(secret, data):
    k = strval(secret).encode('utf-8')
    d = strval(data).encode('utf-8')
    return _hmac.new(k, d, hashlib.sha256).hexdigest()


def tf_is_last_index(index, array):
    if not _is_int(index):
        raise TemplateError('wrong type for value; expected int; got %s' % go_type(index))
    return index == go_len(array) - 1


def tf_html_escape_string(s):
    s = _str(s)
    return (s.replace('&', '&amp;').replace("'", '&#39;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&#34;'))


FUNCS.update({
    'jsonPath': tf_json_path,
    'gJsonPath': tf_gjson_path,
    'xmlPath': tf_xml_path,
    'uuidv5': tf_uuidv5,
    'regexFindAllSubmatch': tf_regex_find_all_submatch,
    'regexFindFirstSubmatch': tf_regex_find_first_submatch,
    'hmacSHA256': tf_hmac_sha256,
    'isLastIndex': tf_is_last_index,
    'htmlEscapeString': tf_html_escape_string,
})

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
# Redis backend (embedded in-memory store or external RESP server)
# ---------------------------------------------------------------------------

import fnmatch
import socket


class RedisError(Exception):
    pass


_WRONGTYPE = 'WRONGTYPE Operation against a key holding the wrong kind of value'


def _redis_glob(pattern, s):
    """Redis-style glob matching (*, ?, [...], backslash escapes)."""
    i = 0
    out = []
    while i < len(pattern):
        c = pattern[i]
        if c == '\\' and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == '*':
            out.append('.*')
        elif c == '?':
            out.append('.')
        elif c == '[':
            j = i + 1
            neg = False
            if j < len(pattern) and pattern[j] == '^':
                neg = True
                j += 1
            buf = []
            while j < len(pattern) and pattern[j] != ']':
                if pattern[j] == '\\' and j + 1 < len(pattern):
                    buf.append(re.escape(pattern[j + 1]))
                    j += 2
                    continue
                if pattern[j] == '-' and buf and j + 1 < len(pattern) and pattern[j + 1] != ']':
                    buf.append('-')
                else:
                    buf.append(re.escape(pattern[j]))
                j += 1
            if j >= len(pattern):
                out.append(re.escape('['))
            else:
                out.append('[' + ('^' if neg else '') + ''.join(buf) + ']')
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.fullmatch(''.join(out), s, re.S) is not None


def _rint(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise RedisError('ERR value is not an integer or out of range')


class MemoryRedis(object):
    """A small embedded Redis-compatible store."""

    def __init__(self):
        self.data = {}      # key -> (type, value)
        self.expires = {}   # key -> absolute time
        self.lock = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _alive(self, key):
        exp = self.expires.get(key)
        if exp is not None and time.time() >= exp:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def _get(self, key, typ):
        if not self._alive(key):
            return None
        t, v = self.data[key]
        if t != typ:
            raise RedisError(_WRONGTYPE)
        return v

    def _del(self, key):
        self.expires.pop(key, None)
        return self.data.pop(key, None) is not None

    def _cleanup(self, key):
        if key in self.data:
            t, v = self.data[key]
            if t in ('list', 'hash') and not v:
                self._del(key)

    def do(self, cmd, args):
        name = cmd.upper()
        fn = getattr(self, 'c_' + name.lower(), None)
        if fn is None or not name.isalpha():
            raise RedisError("ERR unknown command '%s'" % cmd)
        with self.lock:
            return fn(args)

    @staticmethod
    def _arity(args, n, name, exact=False):
        if (exact and len(args) != n) or len(args) < n:
            raise RedisError("ERR wrong number of arguments for '%s' command" % name)

    # -- strings ---------------------------------------------------------
    def c_set(self, a):
        self._arity(a, 2, 'set')
        key, val = a[0], a[1]
        nx = xx = keepttl = get = False
        exp = None
        i = 2
        while i < len(a):
            o = a[i].upper()
            if o == 'NX':
                nx = True
            elif o == 'XX':
                xx = True
            elif o == 'KEEPTTL':
                keepttl = True
            elif o == 'GET':
                get = True
            elif o in ('EX', 'PX', 'EXAT', 'PXAT') and i + 1 < len(a):
                n = _rint(a[i + 1])
                if n <= 0:
                    raise RedisError("ERR invalid expire time in 'set' command")
                now = time.time()
                exp = {'EX': now + n, 'PX': now + n / 1000.0,
                       'EXAT': float(n), 'PXAT': n / 1000.0}[o]
                i += 1
            else:
                raise RedisError('ERR syntax error')
            i += 1
        if nx and xx:
            raise RedisError('ERR syntax error')
        old = None
        exists = self._alive(key)
        if get and exists:
            old = self._get(key, 'string')
        if (nx and exists) or (xx and not exists):
            return old if get else None
        prev_exp = self.expires.get(key)
        self._del(key)
        self.data[key] = ('string', val)
        if exp is not None:
            self.expires[key] = exp
        elif keepttl and prev_exp is not None:
            self.expires[key] = prev_exp
        return old if get else 'OK'

    def c_setnx(self, a):
        self._arity(a, 2, 'setnx', True)
        if self._alive(a[0]):
            return 0
        self.data[a[0]] = ('string', a[1])
        return 1

    def c_get(self, a):
        self._arity(a, 1, 'get', True)
        return self._get(a[0], 'string')

    def c_mget(self, a):
        self._arity(a, 1, 'mget')
        out = []
        for k in a:
            if self._alive(k) and self.data[k][0] == 'string':
                out.append(self.data[k][1])
            else:
                out.append(None)
        return out

    def c_append(self, a):
        self._arity(a, 2, 'append', True)
        v = (self._get(a[0], 'string') or '') + a[1]
        if not self._alive(a[0]):
            self.data[a[0]] = ('string', v)
        else:
            self.data[a[0]] = ('string', v)
        return len(v.encode('utf-8'))

    def _incr(self, key, by):
        v = self._get(key, 'string')
        try:
            n = int(v) if v is not None else 0
            if v is not None and str(n) != v:
                raise ValueError
        except ValueError:
            raise RedisError('ERR value is not an integer or out of range')
        n += by
        exp = self.expires.get(key)
        self.data[key] = ('string', str(n))
        if exp is not None:
            self.expires[key] = exp
        return n

    def c_incr(self, a):
        self._arity(a, 1, 'incr', True)
        return self._incr(a[0], 1)

    def c_decr(self, a):
        self._arity(a, 1, 'decr', True)
        return self._incr(a[0], -1)

    def c_incrby(self, a):
        self._arity(a, 2, 'incrby', True)
        return self._incr(a[0], _rint(a[1]))

    def c_decrby(self, a):
        self._arity(a, 2, 'decrby', True)
        return self._incr(a[0], -_rint(a[1]))

    def c_strlen(self, a):
        self._arity(a, 1, 'strlen', True)
        return len((self._get(a[0], 'string') or '').encode('utf-8'))

    # -- lists -----------------------------------------------------------
    def _push(self, a, left, name, need_exist=False):
        self._arity(a, 2, name)
        lst = self._get(a[0], 'list')
        if lst is None:
            if need_exist:
                return 0
            lst = []
            self.data[a[0]] = ('list', lst)
        for v in a[1:]:
            if left:
                lst.insert(0, v)
            else:
                lst.append(v)
        return len(lst)

    def c_rpush(self, a):
        return self._push(a, False, 'rpush')

    def c_lpush(self, a):
        return self._push(a, True, 'lpush')

    def c_rpushx(self, a):
        return self._push(a, False, 'rpushx', True)

    def c_lpushx(self, a):
        return self._push(a, True, 'lpushx', True)

    def c_lrange(self, a):
        self._arity(a, 3, 'lrange', True)
        lst = self._get(a[0], 'list') or []
        start, stop = _rint(a[1]), _rint(a[2])
        n = len(lst)
        if start < 0:
            start = max(n + start, 0)
        if stop < 0:
            stop = n + stop
        if stop >= n:
            stop = n - 1
        if start > stop or start >= n:
            return []
        return list(lst[start:stop + 1])

    def _pop(self, a, left, name):
        self._arity(a, 1, name)
        if len(a) > 2:
            raise RedisError('ERR syntax error')
        lst = self._get(a[0], 'list')
        if len(a) == 2:
            cnt = _rint(a[1])
            if cnt < 0:
                raise RedisError('ERR value is out of range, must be positive')
            if lst is None:
                return None
            out = []
            for _ in range(min(cnt, len(lst))):
                out.append(lst.pop(0) if left else lst.pop())
            self._cleanup(a[0])
            return out
        if not lst:
            return None
        v = lst.pop(0) if left else lst.pop()
        self._cleanup(a[0])
        return v

    def c_lpop(self, a):
        return self._pop(a, True, 'lpop')

    def c_rpop(self, a):
        return self._pop(a, False, 'rpop')

    def c_llen(self, a):
        self._arity(a, 1, 'llen', True)
        return len(self._get(a[0], 'list') or [])

    def c_lindex(self, a):
        self._arity(a, 2, 'lindex', True)
        lst = self._get(a[0], 'list') or []
        i = _rint(a[1])
        if i < 0:
            i += len(lst)
        if 0 <= i < len(lst):
            return lst[i]
        return None

    # -- hashes ----------------------------------------------------------
    def c_hset(self, a):
        if len(a) < 3 or len(a) % 2 == 0:
            raise RedisError("ERR wrong number of arguments for 'hset' command")
        h = self._get(a[0], 'hash')
        if h is None:
            h = {}
            self.data[a[0]] = ('hash', h)
        added = 0
        for i in range(1, len(a), 2):
            if a[i] not in h:
                added += 1
            h[a[i]] = a[i + 1]
        return added

    def c_hmset(self, a):
        self.c_hset(a)
        return 'OK'

    def c_hsetnx(self, a):
        self._arity(a, 3, 'hsetnx', True)
        h = self._get(a[0], 'hash')
        if h is None:
            h = {}
            self.data[a[0]] = ('hash', h)
        if a[1] in h:
            return 0
        h[a[1]] = a[2]
        return 1

    def c_hget(self, a):
        self._arity(a, 2, 'hget', True)
        h = self._get(a[0], 'hash') or {}
        return h.get(a[1])

    def c_hmget(self, a):
        self._arity(a, 2, 'hmget')
        h = self._get(a[0], 'hash') or {}
        return [h.get(f) for f in a[1:]]

    def c_hgetall(self, a):
        self._arity(a, 1, 'hgetall', True)
        h = self._get(a[0], 'hash') or {}
        out = []
        for k in sorted(h):
            out.append(k)
            out.append(h[k])
        return out

    def c_hkeys(self, a):
        self._arity(a, 1, 'hkeys', True)
        return sorted(self._get(a[0], 'hash') or {})

    def c_hvals(self, a):
        self._arity(a, 1, 'hvals', True)
        h = self._get(a[0], 'hash') or {}
        return [h[k] for k in sorted(h)]

    def c_hlen(self, a):
        self._arity(a, 1, 'hlen', True)
        return len(self._get(a[0], 'hash') or {})

    def c_hexists(self, a):
        self._arity(a, 2, 'hexists', True)
        return 1 if a[1] in (self._get(a[0], 'hash') or {}) else 0

    def c_hdel(self, a):
        self._arity(a, 2, 'hdel')
        h = self._get(a[0], 'hash')
        if h is None:
            return 0
        n = 0
        for f in a[1:]:
            if f in h:
                del h[f]
                n += 1
        self._cleanup(a[0])
        return n

    def c_hincrby(self, a):
        self._arity(a, 3, 'hincrby', True)
        h = self._get(a[0], 'hash')
        if h is None:
            h = {}
            self.data[a[0]] = ('hash', h)
        try:
            n = int(h.get(a[1], '0'))
        except ValueError:
            raise RedisError('ERR hash value is not an integer')
        n += _rint(a[2])
        h[a[1]] = str(n)
        return n

    # -- keys ------------------------------------------------------------
    def c_del(self, a):
        self._arity(a, 1, 'del')
        n = 0
        for k in a:
            if self._alive(k) and self._del(k):
                n += 1
        return n

    c_unlink = c_del

    def c_exists(self, a):
        self._arity(a, 1, 'exists')
        return sum(1 for k in a if self._alive(k))

    def c_keys(self, a):
        self._arity(a, 1, 'keys', True)
        return sorted(k for k in list(self.data) if self._alive(k) and _redis_glob(a[0], k))

    def c_type(self, a):
        self._arity(a, 1, 'type', True)
        if not self._alive(a[0]):
            return 'none'
        return self.data[a[0]][0]

    def c_expire(self, a):
        self._arity(a, 2, 'expire', True)
        if not self._alive(a[0]):
            return 0
        n = _rint(a[1])
        if n <= 0:
            self._del(a[0])
        else:
            self.expires[a[0]] = time.time() + n
        return 1

    def c_persist(self, a):
        self._arity(a, 1, 'persist', True)
        if self._alive(a[0]) and a[0] in self.expires:
            del self.expires[a[0]]
            return 1
        return 0

    def c_ttl(self, a):
        self._arity(a, 1, 'ttl', True)
        if not self._alive(a[0]):
            return -2
        exp = self.expires.get(a[0])
        if exp is None:
            return -1
        return int(math.ceil(exp - time.time()))

    def c_flushall(self, a):
        self.data.clear()
        self.expires.clear()
        return 'OK'

    c_flushdb = c_flushall

    def c_dbsize(self, a):
        return sum(1 for k in list(self.data) if self._alive(k))

    def c_ping(self, a):
        return a[0] if a else 'PONG'

    def c_echo(self, a):
        self._arity(a, 1, 'echo', True)
        return a[0]


class RESPRedis(object):
    """Minimal RESP client for an external Redis server."""

    def __init__(self, url):
        self.url = url
        u = urllib.parse.urlsplit(url)
        self.scheme = (u.scheme or 'redis').lower()
        self.host = u.hostname or 'localhost'
        self.port = u.port or 6379
        self.username = urllib.parse.unquote(u.username) if u.username else None
        self.password = urllib.parse.unquote(u.password) if u.password else None
        q = urllib.parse.parse_qs(u.query)
        if self.password is None and q.get('password'):
            self.password = q['password'][0]
        db = (u.path or '').strip('/')
        if not db and q.get('db'):
            db = q['db'][0]
        self.db = int(db) if db.isdigit() else 0
        self.sock = None
        self.rfile = None
        self.lock = threading.Lock()

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=5)
        if self.scheme == 'rediss':
            import ssl
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)
        s.settimeout(10)
        self.sock = s
        self.rfile = s.makefile('rb')
        if self.password is not None:
            if self.username:
                self._roundtrip(['AUTH', self.username, self.password])
            else:
                self._roundtrip(['AUTH', self.password])
        if self.db:
            self._roundtrip(['SELECT', str(self.db)])

    def _close(self):
        try:
            if self.rfile:
                self.rfile.close()
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = self.rfile = None

    @staticmethod
    def _encode(parts):
        out = [b'*%d\r\n' % len(parts)]
        for p in parts:
            b = p.encode('utf-8') if isinstance(p, str) else p
            out.append(b'$%d\r\n' % len(b))
            out.append(b)
            out.append(b'\r\n')
        return b''.join(out)

    def _read(self):
        line = self.rfile.readline()
        if not line:
            raise ConnectionError('connection closed by redis server')
        t, rest = line[:1], line[1:].rstrip(b'\r\n')
        if t == b'+':
            return rest.decode('utf-8', 'replace')
        if t == b'-':
            return RedisError(rest.decode('utf-8', 'replace'))
        if t == b':':
            return int(rest)
        if t == b'$':
            n = int(rest)
            if n < 0:
                return None
            data = self.rfile.read(n + 2)
            return data[:n].decode('utf-8', 'replace')
        if t == b'*':
            n = int(rest)
            if n < 0:
                return None
            return [self._read() for _ in range(n)]
        raise ConnectionError('invalid redis reply')

    def _roundtrip(self, parts):
        self.sock.sendall(self._encode(parts))
        r = self._read()
        if isinstance(r, RedisError):
            raise r
        return r

    def do(self, cmd, args):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()
                    return self._roundtrip([cmd] + list(args))
                except RedisError:
                    raise
                except (OSError, ConnectionError, ValueError) as e:
                    self._close()
                    if attempt:
                        raise RedisError('redis connection error: %s' % e)


_redis = None
_redis_lock = threading.Lock()


def get_redis():
    global _redis
    with _redis_lock:
        if _redis is None:
            _redis = new_redis()
        return _redis


def new_redis():
    typ = (os.environ.get('HM_REDIS_TYPE') or 'memory').strip().lower()
    if typ == 'redis':
        url = os.environ.get('HM_REDIS_URL') or 'redis://redis:6379'
        log('info', 'using external redis', url=url)
        return RESPRedis(url)
    if typ != 'memory':
        log('warn', 'unknown HM_REDIS_TYPE, falling back to memory', type=typ)
    return MemoryRedis()


def _redis_arg(v):
    if v is None or v is MISSING:
        return ''
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode('utf-8', 'replace')
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return go_float(v)
    return fmtv(v)


def _redis_result(v):
    if v is None:
        return ''
    if isinstance(v, list):
        return ';;'.join(_redis_result(x) for x in v)
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, RedisError):
        return ''
    return str(v)


def redis_do(command, *args):
    """Template function: {{ redisDo "SET" "k" "v" }}."""
    cmd = _redis_arg(command)
    argv = [_redis_arg(a) for a in args]
    try:
        r = get_redis().do(cmd, argv)
    except RedisError as e:
        log('error', 'redisDo failed', command=cmd, args=argv, err=str(e))
        return ''
    except Exception as e:  # pragma: no cover - defensive
        log('error', 'redisDo failed', command=cmd, args=argv, err=repr(e))
        return ''
    return _redis_result(r)


FUNCS['redisDo'] = redis_do


# ---------------------------------------------------------------------------
# Outbound HTTP (send_http)
# ---------------------------------------------------------------------------

import http.client
import ssl as _ssl


def send_http_request(method, url, headers, body, timeout=10.0):
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or 'http').lower()
    if scheme not in ('http', 'https') or not u.hostname:
        raise ValueError('invalid url %r' % url)
    host = u.hostname
    port = u.port
    if scheme == 'https':
        conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=_ssl._create_unverified_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    target = u.path or '/'
    if u.query:
        target += '?' + u.query
    data = body.encode('utf-8') if body else b''
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        has_host = False
        for k, v in headers:
            if k.lower() == 'content-length':
                continue
            if k.lower() == 'host':
                has_host = True
            conn.putheader(k, v.replace('\r', ' ').replace('\n', ' '))
        if not has_host:
            conn.putheader('Host', u.netloc.rsplit('@', 1)[-1])
        if data or method not in ('GET', 'HEAD', 'DELETE', 'OPTIONS', 'TRACE'):
            conn.putheader('Content-Length', str(len(data)))
        conn.endheaders(data if data else None)
        resp = conn.getresponse()
        rbody = resp.read()
        return resp.status, rbody
    finally:
        conn.close()


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


def parse_mock(m, source, root=None):
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
                bff = spec.get('body_from_file')
                if body == '' and bff is not None and _scalar_str(bff) != '':
                    body = _read_body_file(root, _scalar_str(bff), source, key)
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
            elif name == 'redis':
                if spec is None:
                    spec = []
                if not isinstance(spec, list):
                    spec = [spec]
                items = [_scalar_str(x) for x in spec if x is not None]
                actions.append(('redis', {'items': items}))
            elif name == 'send_http':
                if not isinstance(spec, dict):
                    raise LoadError('%s: mock %s: send_http must be a mapping' % (source, key))
                url = spec.get('url')
                if url is None or _scalar_str(url) == '':
                    raise LoadError('%s: mock %s: send_http.url is required' % (source, key))
                smethod = spec.get('method')
                if smethod is None or _scalar_str(smethod).strip() == '':
                    raise LoadError('%s: mock %s: send_http.method is required' % (source, key))
                headers = spec.get('headers') or {}
                if not isinstance(headers, dict):
                    raise LoadError('%s: mock %s: send_http.headers must be a mapping'
                                    % (source, key))
                headers = [(str(k), _scalar_str(v)) for k, v in headers.items()]
                body = _scalar_str(spec.get('body'))
                bff = spec.get('body_from_file')
                if body == '' and bff is not None and _scalar_str(bff) != '':
                    body = _read_body_file(root, _scalar_str(bff), source, key)
                actions.append(('send_http', {'url': _scalar_str(url),
                                              'method': _scalar_str(smethod).strip().upper(),
                                              'headers': headers, 'body': body}))
            else:
                log('warn', 'unsupported action ignored', key=key, action=str(name))
    return Behavior(key, kind, method, path, condition, actions, m)


def _resolve_body_file(root, rel):
    if root is None:
        root = os.environ.get('HM_TEMPLATES_DIR', './templates')
    p = os.path.join(root, rel.lstrip('/\\'))
    if not os.path.isfile(p) and os.path.isabs(rel) and os.path.isfile(rel):
        p = rel
    return p


def _read_body_file(root, rel, source, key):
    p = _resolve_body_file(root, rel)
    try:
        with open(p, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        log('error', 'failed to read body_from_file', key=key, file=p, err=str(e))
        return ''
    if data.startswith(b'\xef\xbb\xbf'):
        data = data[3:]
    return data.decode('utf-8', 'replace')


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
        b = parse_mock(m, path, root)
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
            elif typ == 'redis':
                for item in spec['items']:
                    try:
                        render(item, ctx)
                    except Exception as e:
                        log('error', 'failed to render redis action', key=b.key, err=str(e))
            elif typ == 'send_http':
                do_send_http(b, spec, ctx)
        return status, res_body, sent


def do_send_http(b, spec, ctx):
    try:
        url = render(spec['url'], ctx).strip()
        headers = [(k, render(v, ctx)) for k, v in spec['headers']]
        body = render(spec['body'], ctx)
    except Exception as e:
        log('error', 'failed to render template for send_http', key=b.key, err=str(e))
        return
    try:
        st, rbody = send_http_request(spec['method'], url, headers, body)
        log('info', 'send_http', key=b.key, url=url, method=spec['method'], req=body,
            status=st, res=rbody.decode('utf-8', 'replace'))
    except Exception as e:
        log('error', 'send_http failed', key=b.key, url=url, method=spec['method'], err=str(e))


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
    get_redis()
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
