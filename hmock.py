#!/usr/bin/env python3
"""hmock - YAML-driven HTTP mock server."""
import base64
import decimal
import os
import re
import unicodedata
import urllib.parse
import uuid


class TemplateError(Exception):
    pass


class _Missing:
    def __repr__(self):
        return "<missing>"


MISSING = _Missing()

WS = " \t\r\n"


# ---------------------------------------------------------------------------
# Go-like value formatting
# ---------------------------------------------------------------------------

def _float_digits(f):
    """Return (sign, digits_str, decimal_point) for the shortest repr of f."""
    d = decimal.Decimal(repr(abs(f)))
    _, digits, exp = d.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exp += 1
    s = "".join(str(x) for x in digits)
    return s, len(s) + exp


def fmt_float_g(f, prec=-1, upper=False):
    if f != f:
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "+Inf" if f > 0 else "-Inf"
    neg = f < 0 or (f == 0 and str(f).startswith("-"))
    if f == 0:
        return "-0" if neg else "0"
    if prec < 0:
        digits, dp = _float_digits(f)
        eprec = 6
    else:
        if prec == 0:
            prec = 1
        e = "%.*e" % (prec - 1, abs(f))
        mant, ex = e.split("e")
        digits = mant.replace(".", "").rstrip("0") or "0"
        dp = int(ex) + 1
        eprec = prec
        if eprec > len(digits) and len(digits) >= dp:
            eprec = len(digits)
    x = dp - 1
    sign = "-" if neg else ""
    if x < -4 or x >= eprec:
        mant = digits[0]
        if len(digits) > 1:
            mant += "." + digits[1:]
        es = "-" if x < 0 else "+"
        ax = abs(x)
        return "%s%s%s%s%02d" % (sign, mant, "E" if upper else "e", es, ax)
    if dp <= 0:
        return sign + "0." + "0" * (-dp) + digits
    if dp >= len(digits):
        return sign + digits + "0" * (dp - len(digits))
    return sign + digits[:dp] + "." + digits[dp:]


def gotype(v):
    if v is None:
        return "<nil>"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float64"
    if isinstance(v, str):
        return "string"
    if isinstance(v, GoHeader):
        return "http.Header"
    if isinstance(v, dict):
        return "map[string]interface {}"
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(x, str) for x in v):
            return "[]string"
        return "[]interface {}"
    if callable(v):
        return "func"
    return type(v).__name__


def _sort_key(k):
    if isinstance(k, (int, float)) and not isinstance(k, bool):
        return (0, k, "")
    return (1, 0, str(k))


def gofmt(v):
    """fmt %v formatting."""
    if v is None:
        return "<nil>"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return fmt_float_g(v)
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return "[" + " ".join(str(b) for b in v) + "]"
    if isinstance(v, dict):
        items = sorted(v.items(), key=lambda kv: _sort_key(kv[0]))
        return "map[" + " ".join("%s:%s" % (gofmt(k), gofmt(x)) for k, x in items) + "]"
    if isinstance(v, (list, tuple)):
        return "[" + " ".join(gofmt(x) for x in v) + "]"
    return str(v)


def go_sprint(*args):
    out = []
    prev_string = False
    for i, a in enumerate(args):
        is_string = isinstance(a, str)
        if i > 0 and not is_string and not prev_string:
            out.append(" ")
        out.append(gofmt(a))
        prev_string = is_string
    return "".join(out)


def go_sprintln(*args):
    return " ".join(gofmt(a) for a in args) + "\n"


def go_quote(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\a":
            out.append("\\a")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\v":
            out.append("\\v")
        elif o < 0x20 or o == 0x7F:
            out.append("\\x%02x" % o)
        elif 0xDC80 <= o <= 0xDCFF:  # surrogate-escaped invalid byte
            out.append("\\x%02x" % (o - 0xDC00))
        elif o >= 0x80 and not ch.isprintable():
            if o > 0xFFFF:
                out.append("\\U%08x" % o)
            else:
                out.append("\\u%04x" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


_FMT_RE = re.compile(r"%([-+# 0]*)(\*|\d+)?(?:\.(\*|\d*))?(.)", re.S)


def go_sprintf(fmt, *args):
    if not isinstance(fmt, str):
        raise TemplateError("wrong type for value; expected string; got %s" % gotype(fmt))
    out = []
    pos = 0
    argi = 0
    n = len(fmt)
    while pos < n:
        i = fmt.find("%", pos)
        if i < 0:
            out.append(fmt[pos:])
            break
        out.append(fmt[pos:i])
        m = _FMT_RE.match(fmt, i)
        if not m:
            out.append("%!(NOVERB)")
            break
        pos = m.end()
        flags, width, prec, verb = m.group(1), m.group(2), m.group(3), m.group(4)
        if verb == "%":
            out.append("%")
            continue
        if width == "*":
            if argi < len(args) and isinstance(args[argi], int):
                width = args[argi]
                argi += 1
            else:
                width = None
        elif width is not None:
            width = int(width)
        if prec == "*":
            if argi < len(args) and isinstance(args[argi], int):
                prec = args[argi]
                argi += 1
            else:
                prec = None
        elif prec is not None:
            prec = int(prec) if prec != "" else 0
        if argi >= len(args):
            out.append("%!" + verb + "(MISSING)")
            continue
        arg = args[argi]
        argi += 1
        out.append(_fmt_one(flags, width, prec, verb, arg))
    if argi < len(args):
        extras = ", ".join("%s=%s" % (gotype(a), gofmt(a)) for a in args[argi:])
        out.append("%!(EXTRA " + extras + ")")
    return "".join(out)


def _pad(s, flags, width, numeric):
    if width is None or len(s) >= width:
        return s
    if "-" in flags:
        return s + " " * (width - len(s))
    if "0" in flags and numeric:
        sign = ""
        if s and s[0] in "+-":
            sign, s = s[0], s[1:]
        return sign + "0" * (width - len(sign) - len(s)) + s
    return " " * (width - len(s)) + s


def _bad_verb(verb, arg):
    if arg is None:
        return "%!" + verb + "(<nil>)"
    return "%%!%s(%s=%s)" % (verb, gotype(arg), gofmt(arg))


def _fmt_one(flags, width, prec, verb, arg):
    is_int = isinstance(arg, int) and not isinstance(arg, bool)
    is_float = isinstance(arg, float)
    if verb == "v":
        if "#" in flags and isinstance(arg, str):
            s = go_quote(arg)
        elif is_float:
            s = fmt_float_g(arg)
            if "+" in flags and arg >= 0:
                s = "+" + s
        elif is_int and "+" in flags and arg >= 0:
            s = "+" + str(arg)
        else:
            s = gofmt(arg)
        return _pad(s, flags, width, is_int or is_float)
    if verb == "T":
        return _pad(gotype(arg), flags, width, False)
    if verb == "s":
        if isinstance(arg, (str, bytes)) or not (is_int or is_float or isinstance(arg, bool)):
            s = gofmt(arg) if not isinstance(arg, bytes) else arg.decode("utf-8", "replace")
            if isinstance(arg, (list, tuple)):
                s = "[" + " ".join(_fmt_one("", None, None, "s", x) for x in arg) + "]"
            if prec is not None:
                s = s[:prec]
            return _pad(s, flags, width, False)
        return _bad_verb(verb, arg)
    if verb == "q":
        if isinstance(arg, str):
            return _pad(go_quote(arg), flags, width, False)
        if is_int:
            return _pad("'" + go_quote(chr(arg))[1:-1].replace("'", "\\'").replace('\\"', '"') + "'", flags, width, False)
        return _bad_verb(verb, arg)
    if verb == "t":
        if isinstance(arg, bool):
            return _pad("true" if arg else "false", flags, width, False)
        return _bad_verb(verb, arg)
    if verb in "dboxXcUO":
        if is_int:
            v = arg
            if verb == "c":
                return _pad(chr(v), flags, width, False)
            if verb == "U":
                return _pad("U+%04X" % v, flags, width, False)
            neg = v < 0
            av = abs(v)
            if verb == "d":
                s = str(av)
            elif verb == "b":
                s = format(av, "b")
            elif verb == "o":
                s = format(av, "o")
                if "#" in flags:
                    s = "0" + s
            elif verb == "O":
                s = "0o" + format(av, "o")
            elif verb == "x":
                s = format(av, "x")
                if "#" in flags:
                    s = "0x" + s
            else:
                s = format(av, "X")
                if "#" in flags:
                    s = "0X" + s
            if prec is not None and len(s) < prec:
                s = "0" * (prec - len(s)) + s
            if neg:
                s = "-" + s
            elif "+" in flags:
                s = "+" + s
            elif " " in flags:
                s = " " + s
            return _pad(s, flags, width, True)
        if verb in "xX" and isinstance(arg, str):
            s = arg.encode("utf-8", "surrogateescape").hex()
            if verb == "X":
                s = s.upper()
            return _pad(s, flags, width, False)
        if verb in "xX" and is_float:
            s = float.hex(arg)
            return _pad(s.upper() if verb == "X" else s, flags, width, True)
        if isinstance(arg, (list, tuple)) and verb == "d":
            return "[" + " ".join(_fmt_one(flags, width, prec, verb, x) for x in arg) + "]"
        return _bad_verb(verb, arg)
    if verb in "eEfFgG":
        if is_float:
            v = arg
            if v != v or v in (float("inf"), float("-inf")):
                s = fmt_float_g(v)
            elif verb in "gG":
                s = fmt_float_g(v, -1 if prec is None else prec, upper=(verb == "G"))
            elif verb in "eE":
                p = 6 if prec is None else prec
                s = "%.*e" % (p, v)
                if verb == "E":
                    s = s.upper()
            else:
                p = 6 if prec is None else prec
                s = "%.*f" % (p, v)
            if "+" in flags and not s.startswith("-"):
                s = "+" + s
            elif " " in flags and not s.startswith("-"):
                s = " " + s
            return _pad(s, flags, width, True)
        return _bad_verb(verb, arg)
    return _bad_verb(verb, arg)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def truth(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, (str, bytes, list, tuple, dict)):
        return len(v) > 0
    return True


def canonical_header_key(k):
    # net/textproto.CanonicalMIMEHeaderKey
    for ch in k:
        if not (ch.isalnum() or ch in "-!#$%&'*+.^_`|~") or ord(ch) > 127:
            return k
    parts = k.split("-")
    return "-".join(p[:1].upper() + p[1:].lower() for p in parts)


class GoHeader(dict):
    """Mimics Go's http.Header (map[string][]string with methods)."""

    def add(self, key, value):
        self.setdefault(canonical_header_key(key), []).append(value)

    def _get(self, key):
        if not isinstance(key, str):
            raise TemplateError("wrong type for value; expected string; got %s" % gotype(key))
        vals = dict.get(self, canonical_header_key(key))
        if not vals:
            return ""
        return vals[0]

    def _values(self, key):
        if not isinstance(key, str):
            raise TemplateError("wrong type for value; expected string; got %s" % gotype(key))
        return list(dict.get(self, canonical_header_key(key)) or [])

    def go_method(self, name):
        if name == "Get":
            return self._get
        if name == "Values":
            return self._values
        return None


def _go_method(receiver, name):
    gm = getattr(receiver, "go_method", None)
    if gm is None:
        return None
    return gm(name)


# ---------------------------------------------------------------------------
# Builtin functions
# ---------------------------------------------------------------------------

def _need_str(v, fname=None):
    if not isinstance(v, str):
        raise TemplateError("wrong type for value; expected string; got %s" % gotype(v))
    return v


def _need_int(v):
    if not isinstance(v, int) or isinstance(v, bool):
        raise TemplateError("wrong type for value; expected int; got %s" % gotype(v))
    return v


def _need_bool(v):
    if not isinstance(v, bool):
        raise TemplateError("wrong type for value; expected bool; got %s" % gotype(v))
    return v


def _basic_kind(v):
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (list, tuple, dict)):
        return "noncomparable"
    return "other"


def f_eq(arg1, *args):
    if not args:
        raise TemplateError("missing argument for comparison")
    k1 = _basic_kind(arg1)
    if k1 == "noncomparable":
        raise TemplateError("non-comparable type %s: %s" % (gofmt(arg1), gotype(arg1)))
    for a in args:
        k2 = _basic_kind(a)
        if k2 == "noncomparable":
            raise TemplateError("non-comparable type %s: %s" % (gofmt(a), gotype(a)))
        if k1 != k2:
            if k1 != "nil" and k2 != "nil":
                raise TemplateError("incompatible types for comparison")
            truthv = False
        elif k1 == "nil":
            truthv = True
        elif k1 == "other":
            truthv = arg1 is a or arg1 == a
        else:
            truthv = arg1 == a
        if truthv:
            return True
    return False


def f_ne(a, b):
    return not f_eq(a, b)


def _ordered(a, b):
    k1, k2 = _basic_kind(a), _basic_kind(b)
    if k1 not in ("int", "float", "string"):
        raise TemplateError("invalid type for comparison")
    if k2 not in ("int", "float", "string"):
        raise TemplateError("invalid type for comparison")
    if k1 != k2:
        raise TemplateError("incompatible types for comparison")


def f_lt(a, b):
    _ordered(a, b)
    return a < b


def f_le(a, b):
    _ordered(a, b)
    return a <= b


def f_gt(a, b):
    _ordered(a, b)
    return a > b


def f_ge(a, b):
    _ordered(a, b)
    return a >= b


def f_not(a):
    return not truth(a)


def _eval_args_str(args):
    if len(args) == 1 and isinstance(args[0], str):
        return args[0]
    return go_sprint(*args)


def f_html(*args):
    s = _eval_args_str(args)
    out = []
    for ch in s:
        if ch == '"':
            out.append("&#34;")
        elif ch == "'":
            out.append("&#39;")
        elif ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == "\0":
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)


def f_js(*args):
    s = _eval_args_str(args)
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ch == '"':
            out.append('\\"')
        elif ch == "<":
            out.append("\\u003C")
        elif ch == ">":
            out.append("\\u003E")
        elif ch == "&":
            out.append("\\u0026")
        elif ch == "=":
            out.append("\\u003D")
        elif o < 0x20:
            out.append("\\u%04X" % o)
        elif o >= 0x80 and not ch.isprintable():
            out.append("\\u%04X" % o)
        else:
            out.append(ch)
    return "".join(out)


def f_urlquery(*args):
    s = _eval_args_str(args)
    return urllib.parse.quote_plus(s.encode("utf-8", "surrogateescape"), safe="")


def f_len(v):
    if v is None:
        raise TemplateError("len of nil pointer")
    if isinstance(v, str):
        return len(v.encode("utf-8", "surrogateescape"))
    if isinstance(v, (list, tuple, dict, bytes)):
        return len(v)
    raise TemplateError("len of type %s" % gotype(v))


def f_index(item, *indices):
    if item is None:
        raise TemplateError("index of untyped nil")
    for idx in indices:
        if item is None:
            raise TemplateError("index of untyped nil")
        if isinstance(item, (list, tuple, str)):
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise TemplateError("cannot index slice/array with type %s" % gotype(idx))
            seq = item.encode("utf-8", "surrogateescape") if isinstance(item, str) else item
            if idx < 0 or idx >= len(seq):
                raise TemplateError("error calling index: index out of range: %d" % idx)
            item = seq[idx]
        elif isinstance(item, dict):
            if isinstance(item, GoHeader) or all(isinstance(k, str) for k in item):
                if not isinstance(idx, str) and idx is not None:
                    raise TemplateError("value has type %s; should be string" % gotype(idx))
            item = item.get(idx) if idx in item else None
        else:
            raise TemplateError("can't index item of type %s" % gotype(item))
    return item


def f_call(fn, *args):
    if fn is None:
        raise TemplateError("call of nil")
    if not callable(fn):
        raise TemplateError("non-function of type %s" % gotype(fn))
    return fn(*args)


# --- sprig ---------------------------------------------------------------

def strval(v):
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return v.decode("utf-8", "surrogateescape")
    return gofmt(v)


def strslice(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [strval(x) for x in v if x is not None]
    return [strval(v)]


def _is_go_space(ch):
    return ch in "\t\n\v\f\r \x85\xa0" or unicodedata.category(ch) == "Zs"


def _go_trimspace(s):
    i, j = 0, len(s)
    while i < j and _is_go_space(s[i]):
        i += 1
    while j > i and _is_go_space(s[j - 1]):
        j -= 1
    return s[i:j]


def _is_separator(ch):
    o = ord(ch)
    if o <= 0x7F:
        if ch.isalnum() or ch == "_":
            return False
        return True
    if ch.isalpha() or ch.isdigit():
        return False
    return _is_go_space(ch)


def go_title(s):
    prev = " "
    out = []
    for ch in s:
        if _is_separator(prev):
            out.append(ch.title() if len(ch.title()) == 1 else ch.upper())
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


def _go_split(s, sep):
    if sep == "":
        return list(s)
    return s.split(sep)


def s_contains(substr, s):
    return _need_str(substr) in _need_str(s)


def s_has_prefix(prefix, s):
    return _need_str(s).startswith(_need_str(prefix))


def s_has_suffix(suffix, s):
    return _need_str(s).endswith(_need_str(suffix))


def s_replace(old, new, src):
    _need_str(old), _need_str(new), _need_str(src)
    if old == "":
        # Go: inserts new at the start and after each rune
        return new + new.join(src) + new if src else new
    return src.replace(old, new)


def s_trim(s):
    return _go_trimspace(_need_str(s))


def s_upper(s):
    return _need_str(s).upper()


def s_lower(s):
    return _need_str(s).lower()


def s_title(s):
    return go_title(_need_str(s))


def s_split(sep, orig):
    parts = _go_split(_need_str(orig), _need_str(sep))
    return {"_%d" % i: p for i, p in enumerate(parts)}


def s_split_list(sep, orig):
    return _go_split(_need_str(orig), _need_str(sep))


def s_join(sep, v):
    return _need_str(sep).join(strslice(v))


def s_repeat(count, s):
    _need_int(count)
    _need_str(s)
    if count < 0:
        raise TemplateError("strings: negative Repeat count")
    return s * count


def s_nospace(s):
    return "".join(ch for ch in _need_str(s) if not _is_go_space(ch))


def s_to_string(v):
    return strval(v)


def empty(v):
    if v is None:
        return True
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, (str, bytes, list, tuple, dict)):
        return len(v) == 0
    return False


def s_default(d, *given):
    if not given or empty(given[0]):
        return d
    return given[0]


def s_empty(v):
    return empty(v)


def s_coalesce(*vals):
    for v in vals:
        if not empty(v):
            return v
    return None


def s_ternary(vt, vf, v):
    return vt if _need_bool(v) else vf


def s_b64enc(s):
    return base64.b64encode(_need_str(s).encode("utf-8", "surrogateescape")).decode("ascii")


def s_b64dec(s):
    _need_str(s)
    try:
        data = base64.b64decode(s.encode("utf-8", "surrogateescape"), validate=True)
    except Exception:
        # find first illegal byte similar to Go's error message
        bad = 0
        for i, ch in enumerate(s):
            if not (ch.isalnum() and ord(ch) < 128) and ch not in "+/=":
                bad = i
                break
        else:
            bad = len(s) - (len(s) % 4) if len(s) % 4 else max(len(s) - 1, 0)
        return "illegal base64 data at input byte %d" % bad
    return data.decode("utf-8", "surrogateescape")


def s_env(name):
    return os.environ.get(_need_str(name), "")


_INT_RE = re.compile(r"^[+-]?(0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO]?[0-7_]+|[0-9][0-9_]*)$")


def to_int64(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return 0
        return int(v)
    if isinstance(v, str):
        s = v
        # cast.trimZeroDecimal
        m = re.match(r"^(.*?)\.0*$", s)
        if m and m.group(1) and "." not in m.group(1):
            s = m.group(1)
        if not _INT_RE.match(s):
            return 0
        t = s
        sign = 1
        if t[0] in "+-":
            if t[0] == "-":
                sign = -1
            t = t[1:]
        try:
            if len(t) > 1 and t[0] == "0" and t[1] in "01234567_":
                return sign * int(t[1:].replace("_", "") or "0", 8)
            return sign * int(t, 0)
        except ValueError:
            return 0
    return 0


def _wrap64(v):
    v &= (1 << 64) - 1
    if v >= (1 << 63):
        v -= 1 << 64
    return v


def m_add(*args):
    return _wrap64(sum(to_int64(a) for a in args))


def m_add1(a):
    return _wrap64(to_int64(a) + 1)


def m_sub(a, b):
    return _wrap64(to_int64(a) - to_int64(b))


def m_mul(a, *vals):
    r = to_int64(a)
    for v in vals:
        r = _wrap64(r * to_int64(v))
    return r


def _trunc_div(a, b):
    if b == 0:
        raise TemplateError("runtime error: integer divide by zero")
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def m_div(a, b):
    return _wrap64(_trunc_div(to_int64(a), to_int64(b)))


def m_mod(a, b):
    a, b = to_int64(a), to_int64(b)
    q = _trunc_div(a, b)
    return a - b * q


def m_max(a, *vals):
    r = to_int64(a)
    for v in vals:
        r = max(r, to_int64(v))
    return r


def m_min(a, *vals):
    r = to_int64(a)
    for v in vals:
        r = min(r, to_int64(v))
    return r


def s_uuidv4():
    return str(uuid.uuid4())


def s_trim_prefix(prefix, s):
    _need_str(prefix), _need_str(s)
    return s[len(prefix):] if prefix and s.startswith(prefix) else s


def s_trim_suffix(suffix, s):
    _need_str(suffix), _need_str(s)
    return s[: -len(suffix)] if suffix and s.endswith(suffix) else s


def s_trim_all(cutset, s):
    return _need_str(s).strip(_need_str(cutset))


def s_quote(*args):
    return " ".join(go_quote(strval(a)) for a in args if a is not None)


def s_squote(*args):
    return " ".join("'" + strval(a) + "'" for a in args if a is not None)


def s_cat(*args):
    return " ".join(strval(a) for a in args if a is not None)


def s_list(*args):
    return list(args)


def s_dict(*args):
    d = {}
    for i in range(0, len(args), 2):
        k = strval(args[i])
        d[k] = args[i + 1] if i + 1 < len(args) else ""
    return d


FUNCS = {
    # Go builtins
    "and": None,
    "or": None,
    "not": f_not,
    "eq": f_eq,
    "ne": f_ne,
    "lt": f_lt,
    "le": f_le,
    "gt": f_gt,
    "ge": f_ge,
    "print": go_sprint,
    "printf": go_sprintf,
    "println": go_sprintln,
    "html": f_html,
    "js": f_js,
    "urlquery": f_urlquery,
    "len": f_len,
    "index": f_index,
    "call": f_call,
    # sprig
    "contains": s_contains,
    "hasPrefix": s_has_prefix,
    "hasSuffix": s_has_suffix,
    "replace": s_replace,
    "trim": s_trim,
    "trimPrefix": s_trim_prefix,
    "trimSuffix": s_trim_suffix,
    "trimAll": s_trim_all,
    "upper": s_upper,
    "lower": s_lower,
    "title": s_title,
    "split": s_split,
    "splitList": s_split_list,
    "join": s_join,
    "repeat": s_repeat,
    "nospace": s_nospace,
    "toString": s_to_string,
    "quote": s_quote,
    "squote": s_squote,
    "cat": s_cat,
    "list": s_list,
    "dict": s_dict,
    "default": s_default,
    "empty": s_empty,
    "coalesce": s_coalesce,
    "ternary": s_ternary,
    "b64enc": s_b64enc,
    "b64dec": s_b64dec,
    "env": s_env,
    "add": m_add,
    "add1": m_add1,
    "sub": m_sub,
    "mul": m_mul,
    "div": m_div,
    "mod": m_mod,
    "max": m_max,
    "min": m_min,
    "uuidv4": s_uuidv4,
}


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

class Tok:
    __slots__ = ("type", "value", "space")

    def __init__(self, type_, value, space):
        self.type = type_
        self.value = value
        self.space = space

    def __repr__(self):
        return "Tok(%s,%r)" % (self.type, self.value)


KEYWORDS = {"if", "else", "end", "range", "with", "define", "template", "block",
            "break", "continue"}

_NUM_RE = re.compile(
    r"[+-]?(?:0[xX][0-9a-fA-F_]+(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[0-9_]+)?"
    r"|0[bB][01_]+|0[oO][0-7_]+"
    r"|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9_]+)?)"
)

_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
            "v": "\v", "\\": "\\", "'": "'", '"': '"'}


def _is_ident_start(ch):
    return ch == "_" or ch.isalpha()


def _is_ident_char(ch):
    return ch == "_" or ch.isalnum()


def _unquote(body, quote):
    out = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            raise TemplateError("bad escape in string")
        c = body[i]
        if c in _ESCAPES:
            if (c == "'" and quote == '"') or (c == '"' and quote == "'"):
                raise TemplateError("invalid syntax in quoted string")
            out.append(_ESCAPES[c])
            i += 1
        elif c == "x":
            h = body[i + 1:i + 3]
            if len(h) != 2 or not all(x in "0123456789abcdefABCDEF" for x in h):
                raise TemplateError("invalid syntax in quoted string")
            b = int(h, 16)
            out.append(chr(b) if b < 0x80 else chr(0xDC00 + b))
            i += 3
        elif c in "uU":
            ln = 4 if c == "u" else 8
            h = body[i + 1:i + 1 + ln]
            if len(h) != ln or not all(x in "0123456789abcdefABCDEF" for x in h):
                raise TemplateError("invalid syntax in quoted string")
            out.append(chr(int(h, 16)))
            i += 1 + ln
        elif c in "01234567":
            o = body[i:i + 3]
            if len(o) != 3 or not all(x in "01234567" for x in o):
                raise TemplateError("invalid syntax in quoted string")
            b = int(o, 8)
            if b > 255:
                raise TemplateError("invalid syntax in quoted string")
            out.append(chr(b) if b < 0x80 else chr(0xDC00 + b))
            i += 3
        else:
            raise TemplateError("invalid syntax in quoted string")
    s = "".join(out)
    # combine surrogate-escaped bytes into proper UTF-8 when possible
    if any(0xDC80 <= ord(c) <= 0xDCFF for c in s):
        s = s.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
    return s


def _lex_action(src, p):
    """Lex tokens of an action starting at p (just after '{{' / '{{- ').
    Returns (tokens, new_pos, trim_right)."""
    toks = []
    n = len(src)
    space = True
    paren = 0
    while True:
        if p >= n:
            raise TemplateError("unclosed action")
        ch = src[p]
        if src.startswith("}}", p):
            if paren:
                raise TemplateError("unclosed left paren")
            return toks, p + 2, False
        if ch in WS:
            if src.startswith("-}}", p + 1):
                if paren:
                    raise TemplateError("unclosed left paren")
                return toks, p + 4, True
            p += 1
            space = True
            continue
        if ch == '"':
            j = p + 1
            while True:
                if j >= n or src[j] == "\n":
                    raise TemplateError("unterminated quoted string")
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            toks.append(Tok("CONST", _unquote(src[p + 1:j], '"'), space))
            p = j + 1
        elif ch == "`":
            j = src.find("`", p + 1)
            if j < 0:
                raise TemplateError("unterminated raw quoted string")
            toks.append(Tok("CONST", src[p + 1:j].replace("\r", ""), space))
            p = j + 1
        elif ch == "'":
            j = p + 1
            while True:
                if j >= n or src[j] == "\n":
                    raise TemplateError("unterminated character constant")
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "'":
                    break
                j += 1
            s = _unquote(src[p + 1:j], "'")
            if len(s) != 1:
                raise TemplateError("malformed character constant")
            toks.append(Tok("CONST", ord(s), space))
            p = j + 1
        elif ch == "(":
            paren += 1
            toks.append(Tok("(", "(", space))
            p += 1
        elif ch == ")":
            paren -= 1
            if paren < 0:
                raise TemplateError("unexpected right paren")
            toks.append(Tok(")", ")", space))
            p += 1
        elif ch == "|":
            toks.append(Tok("|", "|", space))
            p += 1
        elif ch == ",":
            toks.append(Tok(",", ",", space))
            p += 1
        elif src.startswith(":=", p):
            toks.append(Tok(":=", ":=", space))
            p += 2
        elif ch == "=":
            toks.append(Tok("=", "=", space))
            p += 1
        elif ch == "$":
            j = p + 1
            while j < n and _is_ident_char(src[j]):
                j += 1
            toks.append(Tok("VAR", src[p:j], space))
            p = j
        elif ch == "." and not (p + 1 < n and src[p + 1].isdigit()):
            if p + 1 < n and _is_ident_start(src[p + 1]):
                names = []
                j = p
                while j < n and src[j] == "." and j + 1 < n and _is_ident_start(src[j + 1]):
                    k = j + 1
                    while k < n and _is_ident_char(src[k]):
                        k += 1
                    names.append(src[j + 1:k])
                    j = k
                toks.append(Tok("FIELD", names, space))
                p = j
            else:
                toks.append(Tok("DOT", ".", space))
                p += 1
        elif ch.isdigit() or ch == "." or (ch in "+-" and p + 1 < n and (src[p + 1].isdigit() or src[p + 1] == ".")):
            m = _NUM_RE.match(src, p)
            if not m or m.end() == p:
                raise TemplateError("bad number syntax")
            text = m.group(0)
            j = m.end()
            if j < n and (_is_ident_char(src[j]) or src[j] == "."):
                raise TemplateError("bad number syntax: %r" % src[p:j + 1])
            toks.append(Tok("CONST", _parse_number(text), space))
            p = j
        elif _is_ident_start(ch):
            j = p
            while j < n and _is_ident_char(src[j]):
                j += 1
            word = src[p:j]
            if word in KEYWORDS:
                toks.append(Tok("KEYWORD", word, space))
            elif word == "true":
                toks.append(Tok("CONST", True, space))
            elif word == "false":
                toks.append(Tok("CONST", False, space))
            elif word == "nil":
                toks.append(Tok("NIL", None, space))
            else:
                toks.append(Tok("IDENT", word, space))
            p = j
        else:
            raise TemplateError("unrecognized character in action: %r" % ch)
        space = False


def _parse_number(text):
    t = text.replace("_", "")
    sign = 1
    body = t
    if body[:1] in "+-":
        if body[0] == "-":
            sign = -1
        body = body[1:]
    try:
        if re.match(r"^0[0-7]+$", body):
            return sign * int(body, 8)
        return sign * int(body, 0)
    except ValueError:
        pass
    try:
        if body.lower().startswith("0x"):
            return sign * float.fromhex(body)
        return sign * float(body)
    except ValueError:
        raise TemplateError("illegal number syntax: %r" % text)


def lex(src):
    items = []
    pos = 0
    n = len(src)
    trim_next = False
    while True:
        i = src.find("{{", pos)
        text = src[pos:] if i < 0 else src[pos:i]
        if trim_next:
            text = text.lstrip(WS)
            trim_next = False
        if i < 0:
            if text:
                items.append(("text", text))
            break
        j = i + 2
        if j + 1 < n and src[j] == "-" and src[j + 1] in WS:
            text = text.rstrip(WS)
            j += 2
        if text:
            items.append(("text", text))
        if src.startswith("/*", j):
            end = src.find("*/", j + 2)
            if end < 0:
                raise TemplateError("unclosed comment")
            k = end + 2
            if k < n and src[k] in WS and src.startswith("-}}", k + 1):
                trim_next = True
                pos = k + 4
            elif src.startswith("}}", k):
                pos = k + 2
            else:
                raise TemplateError("comment ends before closing delimiter")
            continue
        toks, pos, trim_next = _lex_action(src, j)
        items.append(("action", toks))
    return items


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class Pipe:
    __slots__ = ("decl", "is_assign", "cmds")

    def __init__(self, decl, is_assign, cmds):
        self.decl = decl
        self.is_assign = is_assign
        self.cmds = cmds


class Parser:
    def __init__(self, items, funcs):
        self.items = items
        self.i = 0
        self.vars = ["$"]
        self.funcs = funcs
        self.range_depth = 0

    def parse(self):
        nodes, term, _ = self.parse_list()
        if term is not None:
            raise TemplateError("unexpected {{%s}}" % term)
        return nodes

    def parse_list(self):
        nodes = []
        while self.i < len(self.items):
            it = self.items[self.i]
            self.i += 1
            if it[0] == "text":
                nodes.append(("text", it[1]))
                continue
            toks = it[1]
            if not toks:
                raise TemplateError("missing value for command")
            t0 = toks[0]
            if t0.type == "KEYWORD":
                kw = t0.value
                if kw in ("end", "else"):
                    if kw == "end" and len(toks) > 1:
                        raise TemplateError("unexpected %r in end" % toks[1].value)
                    return nodes, kw, toks
                if kw in ("if", "range", "with"):
                    nodes.append(self.parse_control(kw, toks[1:]))
                    continue
                if kw in ("break", "continue"):
                    if self.range_depth == 0:
                        raise TemplateError("{{%s}} outside {{range}}" % kw)
                    if len(toks) > 1:
                        raise TemplateError("unexpected %r in %s" % (toks[1].value, kw))
                    nodes.append((kw,))
                    continue
                raise TemplateError("unsupported action %r" % kw)
            pipe = self.parse_pipe(toks, 0, None, "command")[0]
            nodes.append(("action", pipe))
        return nodes, None, None

    def parse_control(self, kw, toks):
        mark = len(self.vars)
        if not toks:
            raise TemplateError("missing value for %s" % kw)
        pipe = self.parse_pipe(toks, 0, None, kw)[0]
        if kw == "range":
            self.range_depth += 1
        body, term, ttoks = self.parse_list()
        if kw == "range":
            self.range_depth -= 1
        if term is None:
            raise TemplateError("unexpected EOF")
        else_list = None
        if term == "else":
            rest = ttoks[1:]
            if rest and rest[0].type == "KEYWORD" and rest[0].value in ("if", "with") and kw != "range":
                else_list = [self.parse_control(rest[0].value, rest[1:])]
            elif rest:
                raise TemplateError("unexpected %r in else" % rest[0].value)
            else:
                else_list, term2, _ = self.parse_list()
                if term2 != "end":
                    raise TemplateError("expected end; found %s" % (term2 or "EOF"))
        del self.vars[mark:]
        return (kw, pipe, body, else_list)

    def parse_pipe(self, toks, p, end, ctx):
        decl = []
        is_assign = False
        n = len(toks)
        if p < n and toks[p].type == "VAR":
            if p + 1 < n and toks[p + 1].type in (":=", "="):
                decl = [toks[p].value]
                is_assign = toks[p + 1].type == "="
                p += 2
            elif (p + 3 < n and toks[p + 1].type == "," and toks[p + 2].type == "VAR"
                  and toks[p + 3].type in (":=", "=")):
                if ctx != "range":
                    raise TemplateError("too many declarations in %s" % ctx)
                decl = [toks[p].value, toks[p + 2].value]
                is_assign = toks[p + 3].type == "="
                p += 4
        for v in decl:
            if is_assign:
                if v not in self.vars:
                    raise TemplateError("undefined variable %r" % v)
            else:
                self.vars.append(v)
        cmds = []
        while True:
            cmd, p = self.parse_command(toks, p)
            cmds.append(cmd)
            if p >= n:
                if end is not None:
                    raise TemplateError("unclosed left paren")
                break
            t = toks[p]
            if t.type == "|":
                p += 1
                continue
            if t.type == end:
                break
            raise TemplateError("unexpected %r in operand" % (t.value,))
        return Pipe(decl, is_assign, cmds), p

    def parse_command(self, toks, p):
        args = []
        n = len(toks)
        while p < n and toks[p].type not in ("|", ")"):
            node, p = self.parse_operand(toks, p)
            args.append(node)
        if not args:
            raise TemplateError("missing value for command")
        return args, p

    def parse_operand(self, toks, p):
        t = toks[p]
        tt = t.type
        if tt == "FIELD":
            node = ("field", list(t.value))
            p += 1
        elif tt == "DOT":
            node = ("dot",)
            p += 1
        elif tt == "VAR":
            if t.value not in self.vars:
                raise TemplateError("undefined variable %r" % t.value)
            node = ("var", t.value, [])
            p += 1
        elif tt == "IDENT":
            if t.value not in self.funcs:
                raise TemplateError('function "%s" not defined' % t.value)
            node = ("ident", t.value)
            p += 1
        elif tt == "CONST":
            node = ("const", t.value)
            p += 1
        elif tt == "NIL":
            node = ("nil",)
            p += 1
        elif tt == "(":
            pipe, p = self.parse_pipe(toks, p + 1, ")", "parenthesized pipeline")
            p += 1  # skip ')'
            node = ("pipe", pipe)
        else:
            raise TemplateError("unexpected %r in operand" % (t.value,))
        fields = []
        while p < len(toks) and toks[p].type == "FIELD" and not toks[p].space:
            fields.extend(toks[p].value)
            p += 1
        if fields:
            k = node[0]
            if k == "var":
                node = ("var", node[1], fields)
            elif k == "field":
                node = ("field", node[1] + fields)
            elif k in ("pipe", "ident", "dot"):
                if k == "dot":
                    node = ("field", fields)
                else:
                    node = ("chain", node, fields)
            else:
                raise TemplateError("unexpected . after term")
        return node, p


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class Executor:
    def __init__(self, root, funcs):
        self.vars = [["$", root]]
        self.funcs = funcs

    # variables
    def mark(self):
        return len(self.vars)

    def pop(self, mark):
        del self.vars[mark:]

    def push(self, name, value):
        self.vars.append([name, value])

    def set_var(self, name, value):
        for i in range(len(self.vars) - 1, -1, -1):
            if self.vars[i][0] == name:
                self.vars[i][1] = value
                return
        raise TemplateError("undefined variable: %s" % name)

    def set_top_var(self, n, value):
        self.vars[len(self.vars) - n][1] = value

    def get_var(self, name):
        for i in range(len(self.vars) - 1, -1, -1):
            if self.vars[i][0] == name:
                return self.vars[i][1]
        raise TemplateError("undefined variable: %s" % name)

    # walking
    def walk(self, dot, nodes, out):
        for node in nodes:
            k = node[0]
            if k == "text":
                out.append(node[1])
            elif k == "action":
                val = self.eval_pipeline(dot, node[1])
                if not node[1].decl:
                    out.append(print_value(val))
            elif k == "if" or k == "with":
                mark = self.mark()
                val = self.eval_pipeline(dot, node[1])
                if truth(val):
                    self.walk(val if k == "with" else dot, node[2], out)
                elif node[3] is not None:
                    self.walk(dot, node[3], out)
                self.pop(mark)
            elif k == "range":
                self.walk_range(dot, node, out)
            elif k == "break":
                raise _Break()
            elif k == "continue":
                raise _Continue()
            else:
                raise TemplateError("unknown node")

    def walk_range(self, dot, node, out):
        _, pipe, body, else_list = node
        mark = self.mark()
        val = self.eval_pipeline(dot, pipe)
        ndecl = len(pipe.decl)

        def one(index, elem):
            if ndecl > 0:
                self.set_top_var(1, elem)
            if ndecl > 1:
                self.set_top_var(2, index)
            m2 = self.mark()
            try:
                self.walk(elem, body, out)
            finally:
                self.pop(m2)

        if val is None:
            items = []
        elif isinstance(val, (list, tuple)):
            items = list(enumerate(val))
        elif isinstance(val, dict):
            items = [(k, val[k]) for k in sorted(val.keys(), key=_sort_key)]
        elif isinstance(val, int) and not isinstance(val, bool):
            if ndecl > 1:
                raise TemplateError("can't use %d to iterate over more than one variable" % val)
            items = [(i, i) for i in range(val)]
        else:
            raise TemplateError("range can't iterate over %s" % gofmt(val))
        ran = False
        for index, elem in items:
            ran = True
            try:
                one(index, elem)
            except _Break:
                break
            except _Continue:
                continue
        if not ran and else_list is not None:
            self.walk(dot, else_list, out)
        self.pop(mark)

    def eval_pipeline(self, dot, pipe):
        val = MISSING
        for cmd in pipe.cmds:
            val = self.eval_command(dot, cmd, val)
        if val is MISSING:
            val = None
        for name in pipe.decl:
            if pipe.is_assign:
                self.set_var(name, val)
            else:
                self.push(name, val)
        return val

    @staticmethod
    def not_a_function(args, final):
        if len(args) > 1 or final is not MISSING:
            raise TemplateError("can't give argument to non-function %s" % _node_str(args[0]))

    def eval_command(self, dot, cmd, final):
        first = cmd[0]
        k = first[0]
        if k == "field":
            return self.eval_field_chain(dot, dot, first[1], cmd, final)
        if k == "chain":
            if first[1][0] == "nil":
                raise TemplateError("indirection through explicit nil")
            recv = self.eval_arg(dot, first[1])
            return self.eval_field_chain(dot, recv, first[2], cmd, final)
        if k == "ident":
            return self.eval_function(dot, first[1], cmd, final)
        if k == "pipe":
            self.not_a_function(cmd, final)
            return self.eval_pipeline(dot, first[1])
        if k == "var":
            val = self.get_var(first[1])
            if not first[2]:
                self.not_a_function(cmd, final)
                return val
            return self.eval_field_chain(dot, val, first[2], cmd, final)
        self.not_a_function(cmd, final)
        if k == "dot":
            return dot
        if k == "const":
            return first[1]
        if k == "nil":
            raise TemplateError("nil is not a command")
        raise TemplateError("can't evaluate command %r" % (first,))

    def eval_field_chain(self, dot, receiver, idents, args, final):
        for name in idents[:-1]:
            receiver = self.eval_field(dot, name, receiver, [None], MISSING)
        return self.eval_field(dot, idents[-1], receiver, args, final)

    def eval_field(self, dot, name, receiver, args, final):
        if receiver is None:
            raise TemplateError("nil pointer evaluating interface {}.%s" % name)
        method = _go_method(receiver, name)
        if method is not None:
            argv = [self.eval_arg(dot, a) for a in args[1:]]
            if final is not MISSING:
                argv.append(final)
            return _call(name, method, argv)
        has_args = len(args) > 1 or final is not MISSING
        if isinstance(receiver, dict):
            if has_args:
                raise TemplateError("%s is not a method but has arguments" % name)
            if name in receiver:
                return receiver[name]
            raise TemplateError('map has no entry for key "%s"' % name)
        raise TemplateError("can't evaluate field %s in type %s" % (name, gotype(receiver)))

    def eval_arg(self, dot, node):
        k = node[0]
        if k == "dot":
            return dot
        if k == "nil":
            return None
        if k == "const":
            return node[1]
        if k == "field":
            return self.eval_field_chain(dot, dot, node[1], [node], MISSING)
        if k == "var":
            val = self.get_var(node[1])
            if node[2]:
                return self.eval_field_chain(dot, val, node[2], [node], MISSING)
            return val
        if k == "chain":
            recv = self.eval_arg(dot, node[1])
            return self.eval_field_chain(dot, recv, node[2], [node], MISSING)
        if k == "pipe":
            return self.eval_pipeline(dot, node[1])
        if k == "ident":
            return self.eval_function(dot, node[1], [node], MISSING)
        raise TemplateError("can't handle %r" % (node,))

    def eval_function(self, dot, name, args, final):
        if name in ("and", "or"):
            nodes = args[1:]
            if not nodes and final is MISSING:
                raise TemplateError("wrong number of args for %s: want at least 1 got 0" % name)
            if not nodes:
                return final
            last = None
            for node in nodes:
                last = self.eval_arg(dot, node)
                if (name == "and") != truth(last):
                    return last
            if final is not MISSING:
                return final
            return last
        fn = self.funcs.get(name)
        if fn is None:
            raise TemplateError('"%s" is not a defined function' % name)
        argv = [self.eval_arg(dot, a) for a in args[1:]]
        if final is not MISSING:
            argv.append(final)
        return _call(name, fn, argv)


def _node_str(node):
    k = node[0]
    if k == "const":
        return repr(node[1])
    if k == "field":
        return "." + ".".join(node[1])
    if k == "var":
        return node[1] + "".join("." + f for f in node[2])
    return k


def _call(name, fn, argv):
    try:
        return fn(*argv)
    except TemplateError as e:
        raise TemplateError("error calling %s: %s" % (name, e))
    except TypeError as e:
        raise TemplateError("wrong number of args for %s: %s" % (name, e))
    except RecursionError:
        raise
    except Exception as e:  # noqa: BLE001 - mimic Go's recovered panics
        raise TemplateError("error calling %s: %s" % (name, e))


def print_value(v):
    if v is None or v is MISSING:
        return "<no value>"
    return gofmt(v)


class Template:
    def __init__(self, src, funcs=None):
        self.funcs = funcs if funcs is not None else FUNCS
        self.nodes = Parser(lex(src), self.funcs).parse()

    def execute(self, data):
        out = []
        ex = Executor(data, self.funcs)
        try:
            ex.walk(data, self.nodes, out)
        except RecursionError:
            raise TemplateError("template too deeply nested")
        return "".join(out)


_CACHE = {}
_CACHE_MAX = 2048


def preprocess(src):
    return src.replace("\r\n", " ").replace("\n", " ").replace("\t", " ")


def render(src, data):
    """Render a template string (after newline/tab normalisation)."""
    src = preprocess(src)
    tmpl = _CACHE.get(src)
    if tmpl is None:
        try:
            tmpl = Template(src)
        except RecursionError:
            raise TemplateError("template too deeply nested")
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[src] = tmpl
    return tmpl.execute(data)


# ===========================================================================
# hmock server
# ===========================================================================

import datetime
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yaml
except ImportError:  # pragma: no cover - fall back to a local virtualenv
    _here = os.path.dirname(os.path.abspath(__file__))
    for _cand in (os.path.join(_here, ".venv", "bin", "python"),
                  os.path.join(_here, "venv", "bin", "python")):
        if os.path.exists(_cand) and os.path.realpath(_cand) != os.path.realpath(sys.executable) \
                and not os.environ.get("HM_REEXEC"):
            os.environ["HM_REEXEC"] = "1"
            os.execv(_cand, [_cand, os.path.abspath(__file__)] + sys.argv[1:])
    raise


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}
_LEVEL_NAMES = {10: "debug", 20: "info", 30: "warning", 40: "error"}
_log_lock = threading.Lock()


class Logger:
    def __init__(self, level="info", stream=None):
        self.level = _LEVELS.get(str(level).strip().lower(), 20)
        self.stream = stream or sys.stdout

    def log(self, level, msg, **fields):
        lv = _LEVELS[level]
        if lv < self.level:
            return
        rec = {
            "level": _LEVEL_NAMES[lv],
            "msg": msg,
            "time": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        for k, v in fields.items():
            rec[k] = v
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _log_lock:
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except Exception:
                pass

    def debug(self, msg, **f):
        self.log("debug", msg, **f)

    def info(self, msg, **f):
        self.log("info", msg, **f)

    def warn(self, msg, **f):
        self.log("warn", msg, **f)

    def error(self, msg, **f):
        self.log("error", msg, **f)


LOG = Logger()


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------

_DUR_UNITS = {
    "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3,
    "s": 1.0, "m": 60.0, "h": 3600.0,
}
_DUR_PART = re.compile(r"([0-9]*(?:\.[0-9]*)?)(ns|us|µs|μs|ms|s|m|h)")


def parse_duration(value):
    """Parse a Go-style duration. Returns seconds (float)."""
    if isinstance(value, bool):
        raise ValueError("invalid duration %r" % value)
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("invalid duration %r" % value)
        return float(value)
    if not isinstance(value, str):
        raise ValueError("invalid duration %r" % (value,))
    s = value.strip()
    orig = s
    if not s:
        raise ValueError("invalid duration %r" % orig)
    neg = False
    if s[0] in "+-":
        neg = s[0] == "-"
        s = s[1:]
    if s == "0":
        return 0.0
    if not s:
        raise ValueError("invalid duration %r" % orig)
    total = 0.0
    pos = 0
    while pos < len(s):
        m = _DUR_PART.match(s, pos)
        if not m or m.group(1) in ("", "."):
            raise ValueError("invalid duration %r" % orig)
        total += float(m.group(1)) * _DUR_UNITS[m.group(2)]
        pos = m.end()
    return -total if neg else total


# ---------------------------------------------------------------------------
# Mock definitions
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _to_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return gofmt(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


class ReplyHTTP:
    def __init__(self, spec):
        if not isinstance(spec, dict):
            raise ValidationError("reply_http must be a mapping")
        if "status_code" not in spec or spec["status_code"] is None:
            raise ValidationError("reply_http.status_code is required")
        sc = spec["status_code"]
        if isinstance(sc, bool):
            raise ValidationError("reply_http.status_code must be an integer")
        if isinstance(sc, str) and sc.strip().isdigit():
            sc = int(sc.strip())
        if isinstance(sc, float) and sc.is_integer():
            sc = int(sc)
        if not isinstance(sc, int) or sc < 100 or sc > 999:
            raise ValidationError("reply_http.status_code must be a valid HTTP status")
        self.status_code = sc
        headers = spec.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValidationError("reply_http.headers must be a mapping")
        self.headers = [(str(k), _to_text(v)) for k, v in headers.items()]
        self.body = _to_text(spec.get("body"))


class Sleep:
    def __init__(self, spec):
        if isinstance(spec, dict):
            if "duration" not in spec:
                raise ValidationError("sleep.duration is required")
            d = spec["duration"]
        else:
            d = spec
        try:
            self.duration = parse_duration(d)
        except ValueError as e:
            raise ValidationError(str(e))
        if self.duration < 0:
            self.duration = 0.0


def _compile_path(path):
    if not isinstance(path, str):
        raise ValidationError("expect.http.path must be a string")
    if not path.startswith("/"):
        path = "/" + path
    names = []
    out = ["^"]
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == ":" and (i == 0 or path[i - 1] == "/"):
            j = i + 1
            while j < n and path[j] != "/":
                j += 1
            name = path[i + 1:j]
            names.append(name)
            out.append("(?P<p%d>[^/]+)" % (len(names) - 1))
            i = j
        elif ch == "*":
            names.append("*")
            out.append("(?P<p%d>.*)" % (len(names) - 1))
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out), re.S), names


class Behavior:
    def __init__(self, raw, source):
        if not isinstance(raw, dict):
            raise ValidationError("mock must be a mapping")
        self.source = source
        if "key" not in raw:
            raise ValidationError("key is required")
        key = raw["key"]
        if not isinstance(key, str) or key == "":
            raise ValidationError("key must be a non-empty string")
        self.key = key
        kind = raw.get("kind")
        if kind is None or kind == "":
            kind = "Behavior"
        self.kind = str(kind)
        expect = raw.get("expect") or {}
        if not isinstance(expect, dict):
            raise ValidationError("expect must be a mapping")
        cond = expect.get("condition")
        self.condition = "" if cond is None else _to_text(cond)
        http = expect.get("http")
        self.method = None
        self.path_re = None
        self.path_names = []
        if http is not None:
            if not isinstance(http, dict):
                raise ValidationError("expect.http must be a mapping")
            method = http.get("method")
            path = http.get("path")
            self.method = str(method).upper() if method else None
            if path is not None and path != "":
                self.path_re, self.path_names = _compile_path(path)
            self.path = path
        actions = raw.get("actions") or []
        if not isinstance(actions, list):
            raise ValidationError("actions must be a list")
        self.actions = []
        replies = 0
        for a in actions:
            if not isinstance(a, dict):
                raise ValidationError("each action must be a mapping")
            for name, spec in a.items():
                if name == "reply_http":
                    replies += 1
                    if replies > 1:
                        raise ValidationError("more than one reply_http action")
                    self.actions.append(("reply_http", ReplyHTTP(spec)))
                elif name == "sleep":
                    self.actions.append(("sleep", Sleep(spec)))
                elif name == "order":
                    continue
                else:
                    LOG.warn("unsupported action ignored", key=key, action=str(name))

    @property
    def is_http(self):
        return self.kind == "Behavior" and self.path_re is not None and self.method

    def match_route(self, method, path):
        if self.method != method:
            return None
        m = self.path_re.match(path)
        if not m:
            return None
        params = {}
        for i, name in enumerate(self.path_names):
            params[name] = urllib.parse.unquote(m.group("p%d" % i))
        return params


def _walk_files(root):
    """Lexical-order recursive walk (like Go's filepath.Walk)."""
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        LOG.error("failed to read templates directory", dir=root, err=str(e))
        return
    for name in entries:
        full = os.path.join(root, name)
        if os.path.isdir(full):
            yield from _walk_files(full)
        elif name.lower().endswith((".yaml", ".yml")) and os.path.isfile(full):
            yield full


def load_mocks(templates_dir):
    raw = []
    if not os.path.isdir(templates_dir):
        LOG.warn("templates directory not found", dir=templates_dir)
        return []
    for path in _walk_files(templates_dir):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                docs = list(yaml.safe_load_all(fh))
        except Exception as e:  # noqa: BLE001
            LOG.error("failed to parse mock file", file=path, err=str(e))
            continue
        count = 0
        for doc in docs:
            if doc is None:
                continue
            items = doc if isinstance(doc, list) else [doc]
            for item in items:
                raw.append((item, path))
                count += 1
        LOG.debug("loaded mock file", file=path, count=count)

    behaviors = []
    for item, path in raw:
        try:
            b = Behavior(item, path)
        except ValidationError as e:
            key = item.get("key") if isinstance(item, dict) else None
            LOG.error("invalid mock rejected", file=path, key=_to_text(key), err=str(e))
            continue
        behaviors.append(b)

    # duplicate keys: the last loaded mock wins
    last_index = {}
    for i, b in enumerate(behaviors):
        if b.key in last_index:
            prev = behaviors[last_index[b.key]]
            LOG.warn("duplicate mock key; overriding previous definition",
                     key=b.key, previous_file=prev.source, file=b.source)
        last_index[b.key] = i
    result = [b for i, b in enumerate(behaviors) if last_index[b.key] == i]
    LOG.info("mocks loaded", count=len(result), dir=templates_dir)
    return result


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def _enc(s):
    return s.encode("utf-8", "surrogateescape")


def _dec(b):
    return b.decode("utf-8", "surrogateescape")


def _log_str(s):
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hmock"
    sys_version = ""
    behaviors = []

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):  # silence default logging
        LOG.debug("http server: " + (fmt % args))

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    break
                size_s = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_s, 16)
                except ValueError:
                    break
                if size == 0:
                    # trailers
                    while True:
                        tl = self.rfile.readline(65537)
                        if not tl or tl in (b"\r\n", b"\n"):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)
            return b"".join(chunks)
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                n = int(cl)
            except ValueError:
                n = 0
            if n > 0:
                return self.rfile.read(n)
        return b""

    def _handle(self):
        start = time.time()
        method = self.command.upper()
        raw_uri = self.path
        uri = raw_uri
        if uri.startswith(("http://", "https://")):
            sp = urllib.parse.urlsplit(uri)
            uri = sp.path or "/"
            if sp.query:
                uri += "?" + sp.query
        path_part, _, query = uri.partition("?")
        path_part = path_part.split("#", 1)[0]
        query = query.split("#", 1)[0]
        decoded_path = urllib.parse.unquote(path_part)

        body_bytes = self._read_body()
        body = _dec(body_bytes)

        hdr = GoHeader()
        for k, v in self.headers.items():
            hdr.add(k, v)

        ctx = {
            "HTTPHeader": hdr,
            "HTTPBody": body,
            "HTTPPath": uri,
            "HTTPQueryString": query,
            "HTTPPathParams": {},
        }

        status, resp_headers, resp_body = 404, [("Content-Type", "text/plain; charset=utf-8")], "not found"
        matched_key = None
        for b in self.behaviors:
            params = b.match_route(method, decoded_path)
            if params is None:
                continue
            ctx["HTTPPathParams"] = params
            if b.condition:
                try:
                    out = render(b.condition, ctx)
                except Exception as e:  # noqa: BLE001
                    LOG.debug("condition render failed", key=b.key, err=str(e))
                    continue
                if out != "true":
                    continue
            matched_key = b.key
            status, resp_headers, resp_body = self._run_actions(b, ctx)
            break

        out_body = _enc(resp_body)
        try:
            self.send_response(status)
            has_ct = False
            for k, v in resp_headers:
                lk = k.lower()
                if lk == "content-length":
                    continue
                if lk == "content-type":
                    has_ct = True
                self.send_header(k, v)
            if not has_ct and status not in (204, 304) and matched_key is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(out_body)
            self.wfile.flush()
        finally:
            LOG.info("http request",
                     http_path=_log_str(uri),
                     http_method=method,
                     http_host=self.headers.get("Host", ""),
                     http_req=_log_str(body),
                     http_res=_log_str(resp_body),
                     http_status=status,
                     key=matched_key or "",
                     latency_ms=round((time.time() - start) * 1000, 3))

    def _run_actions(self, b, ctx):
        status, headers, body = 200, [], ""
        replied = False
        for name, act in b.actions:
            if name == "sleep":
                if act.duration > 0:
                    time.sleep(act.duration)
            elif name == "reply_http":
                try:
                    hs = []
                    for k, v in act.headers:
                        hs.append((k, render(v, ctx)))
                    rb = render(act.body, ctx)
                except Exception as e:  # noqa: BLE001
                    LOG.error("failed to render reply_http", key=b.key, err=str(e))
                    status = 500
                    headers = [("Content-Type", "text/plain; charset=utf-8")]
                    body = "internal server error"
                    replied = True
                    continue
                for k, v in hs:
                    if "\r" in v or "\n" in v:
                        v = v.replace("\r", " ").replace("\n", " ")
                    if "\r" in k or "\n" in k:
                        continue
                    headers.append((k, v))
                status, body = act.status_code, rb
                replied = True
        if not replied:
            headers = [("Content-Type", "text/plain; charset=utf-8")]
        return status, headers, body


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def main():
    global LOG
    LOG = Logger(os.environ.get("HM_LOG_LEVEL", "info"))
    templates_dir = os.environ.get("HM_TEMPLATES_DIR") or "./templates"
    host = os.environ.get("HM_HTTP_HOST")
    if host is None or host == "":
        host = "0.0.0.0"
    port_s = os.environ.get("HM_HTTP_PORT") or "9999"
    try:
        port = int(port_s)
    except ValueError:
        LOG.error("invalid HM_HTTP_PORT", value=port_s)
        sys.exit(1)

    behaviors = load_mocks(templates_dir)
    MockHandler.behaviors = [b for b in behaviors if b.is_http]

    try:
        httpd = Server((host, port), MockHandler)
    except OSError as e:
        LOG.error("failed to start http server", err=str(e), host=host, port=port)
        sys.exit(1)
    LOG.info("starting http server", host=host, port=port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
