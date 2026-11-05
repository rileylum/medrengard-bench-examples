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


# ---------------------------------------------------------------------------
# Rich helpers: XPath over JSON/XML, gjson-style paths, misc
# ---------------------------------------------------------------------------

import hashlib
import hmac as _hmac
import json as _json
import math
import xml.parsers.expat


class XNode:
    __slots__ = ("type", "name", "prefix", "local", "data", "children", "attrs",
                 "parent", "order")

    def __init__(self, type_, name="", data="", parent=None):
        self.type = type_          # "doc", "elem", "text", "attr", "comment", "pi"
        self.name = name
        if ":" in name:
            self.prefix, self.local = name.split(":", 1)
        else:
            self.prefix, self.local = "", name
        self.data = data
        self.children = []
        self.attrs = []
        self.parent = parent
        self.order = 0

    def inner_text(self):
        if self.type in ("text", "attr", "comment", "pi"):
            return self.data
        out = []
        stack = [self]
        while stack:
            n = stack.pop()
            if n.type == "text":
                out.append(n.data)
            elif n.type in ("doc", "elem"):
                stack.extend(reversed(n.children))
        return "".join(out)


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


def _go_format_float(f):
    """strconv.FormatFloat(f, 'f', -1, 64)."""
    if f != f:
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "+Inf" if f > 0 else "-Inf"
    if f == int(f) and abs(f) < 1e21:
        s = "%d" % int(f)
        if f == 0 and math.copysign(1.0, f) < 0:
            s = "-0"
        return s
    s = format(decimal.Decimal(repr(f)), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _json_to_xtree(text):
    try:
        val = _json.loads(text, parse_int=float, parse_constant=_bad_const)
    except ValueError as e:
        raise TemplateError("invalid json: %s" % e)
    doc = XNode("doc")

    def add(v, parent):
        if isinstance(v, list):
            for vv in v:
                n = XNode("elem", "", parent=parent)
                parent.children.append(n)
                add(vv, n)
        elif isinstance(v, dict):
            for k in sorted(v.keys()):
                n = XNode("elem", k, parent=parent)
                parent.children.append(n)
                add(v[k], n)
        elif isinstance(v, str):
            parent.children.append(XNode("text", data=v, parent=parent))
        elif isinstance(v, bool):
            parent.children.append(XNode("text", data="true" if v else "false", parent=parent))
        elif isinstance(v, float):
            parent.children.append(XNode("text", data=_go_format_float(v), parent=parent))

    add(val, doc)
    _number_order(doc)
    return doc


def _bad_const(c):
    raise ValueError("invalid character in JSON: %s" % c)


def _xml_to_xtree(text):
    doc = XNode("doc")
    stack = [doc]
    p = xml.parsers.expat.ParserCreate()
    p.buffer_text = True
    p.ordered_attributes = True

    def start(name, attrs):
        parent = stack[-1]
        n = XNode("elem", name, parent=parent)
        for i in range(0, len(attrs), 2):
            n.attrs.append(XNode("attr", attrs[i], attrs[i + 1], parent=n))
        parent.children.append(n)
        stack.append(n)

    def end(name):
        stack.pop()

    def chars(data):
        parent = stack[-1]
        if parent.children and parent.children[-1].type == "text":
            parent.children[-1].data += data
        else:
            parent.children.append(XNode("text", data=data, parent=parent))

    def comment(data):
        stack[-1].children.append(XNode("comment", data=data, parent=stack[-1]))

    def pi(target, data):
        stack[-1].children.append(XNode("pi", target, data, parent=stack[-1]))

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    p.CommentHandler = comment
    p.ProcessingInstructionHandler = pi
    try:
        p.Parse(text.encode("utf-8", "surrogateescape"), True)
    except xml.parsers.expat.ExpatError as e:
        raise TemplateError("invalid xml: %s" % e)
    _number_order(doc)
    return doc


# --- XPath 1.0 (subset) ------------------------------------------------------

_XP_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d*)?|\.\d+)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<op>//|::|\.\.|!=|<=|>=|[/()\[\]@,|.=<>+\-*$])
  | (?P<name>(?:[A-Za-z_À-￿][\w.\-·À-￿]*)
              (?::(?:[A-Za-z_À-￿][\w.\-·À-￿]*|\*))?)
""", re.X)

_AXES = {
    "ancestor", "ancestor-or-self", "attribute", "child", "descendant",
    "descendant-or-self", "following", "following-sibling", "namespace",
    "parent", "preceding", "preceding-sibling", "self",
}
_NODE_TYPES = {"node", "text", "comment", "processing-instruction"}


_XP_NON_OPERAND = {"@", "::", "(", "[", ",", "/", "//", "|", "=", "!=", "<", "<=",
                   ">", ">=", "+", "-", "$"}


def _xp_lex(expr):
    toks = []
    pos = 0
    n = len(expr)
    while pos < n:
        m = _XP_TOKEN.match(expr, pos)
        if not m:
            raise TemplateError("invalid xpath expression: %s" % expr)
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        val = m.group(kind)
        toks.append((kind, val))
    # disambiguate operator names / multiply per XPath 1.0 rules
    out = []
    for i, (k, v) in enumerate(toks):
        prev = out[-1] if out else None
        prev_is_operand = prev is not None and not (
            prev[0] in ("opname", "mul")
            or (prev[0] == "op" and prev[1] in _XP_NON_OPERAND))
        if k == "op" and v == "*" and prev_is_operand:
            out.append(("mul", "*"))
        elif k == "name" and v in ("and", "or", "div", "mod") and prev_is_operand:
            out.append(("opname", v))
        else:
            out.append((k, v))
    return out


class _XPParser:
    def __init__(self, expr):
        self.expr = expr
        self.toks = _xp_lex(expr)
        self.i = 0

    def peek(self, off=0):
        j = self.i + off
        return self.toks[j] if j < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, v):
        t = self.take()
        if t[1] != v:
            raise TemplateError("invalid xpath expression: %s" % self.expr)
        return t

    def parse(self):
        if not self.toks:
            raise TemplateError("invalid xpath expression: empty")
        e = self.or_expr()
        if self.i != len(self.toks):
            raise TemplateError("invalid xpath expression: %s" % self.expr)
        return e

    def _binary(self, sub, ops, kinds=("op", "opname", "mul")):
        left = sub()
        while True:
            k, v = self.peek()
            if k in kinds and v in ops:
                self.take()
                left = ("bin", v, left, sub())
            else:
                return left

    def or_expr(self):
        return self._binary(self.and_expr, ("or",))

    def and_expr(self):
        return self._binary(self.eq_expr, ("and",))

    def eq_expr(self):
        return self._binary(self.rel_expr, ("=", "!="))

    def rel_expr(self):
        return self._binary(self.add_expr, ("<", "<=", ">", ">="))

    def add_expr(self):
        return self._binary(self.mul_expr, ("+", "-"))

    def mul_expr(self):
        return self._binary(self.unary_expr, ("*", "div", "mod"), kinds=("mul", "opname"))

    def unary_expr(self):
        if self.peek() == ("op", "-"):
            self.take()
            return ("neg", self.unary_expr())
        return self.union_expr()

    def union_expr(self):
        left = self.path_expr()
        while self.peek() == ("op", "|"):
            self.take()
            left = ("union", left, self.path_expr())
        return left

    def path_expr(self):
        k, v = self.peek()
        is_filter = False
        if k in ("num", "str") or (k == "op" and v in ("(", "$")):
            is_filter = True
        elif k == "name" and self.peek(1) == ("op", "(") and v not in _NODE_TYPES:
            is_filter = True
        if not is_filter:
            return self.location_path()
        prim = self.primary()
        preds = []
        while self.peek() == ("op", "["):
            preds.append(self.predicate())
        node = ("filter", prim, preds) if preds else prim
        k, v = self.peek()
        if k == "op" and v in ("/", "//"):
            self.take()
            steps = []
            if v == "//":
                steps.append(("descendant-or-self", ("type", "node"), []))
            steps.extend(self.relative_steps())
            node = ("pathfrom", node, steps)
        return node

    def primary(self):
        k, v = self.take()
        if k == "num":
            return ("const", float(v))
        if k == "str":
            return ("const", v[1:-1])
        if v == "(":
            e = self.or_expr()
            self.expect(")")
            return e
        if v == "$":
            name = self.take()
            raise TemplateError("xpath variables not supported: $%s" % name[1])
        if k == "name":
            self.expect("(")
            args = []
            if self.peek() != ("op", ")"):
                args.append(self.or_expr())
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.or_expr())
            self.expect(")")
            return ("call", v, args)
        raise TemplateError("invalid xpath expression: %s" % self.expr)

    def predicate(self):
        self.expect("[")
        e = self.or_expr()
        self.expect("]")
        return e

    def location_path(self):
        k, v = self.peek()
        if k == "op" and v == "/":
            self.take()
            nk, nv = self.peek()
            if nk == "name" or (nk == "op" and nv in ("*", "@", ".", "..")):
                return ("path", True, self.relative_steps())
            return ("path", True, [])
        if k == "op" and v == "//":
            self.take()
            steps = [("descendant-or-self", ("type", "node"), [])]
            steps.extend(self.relative_steps())
            return ("path", True, steps)
        return ("path", False, self.relative_steps())

    def relative_steps(self):
        steps = [self.step()]
        while True:
            k, v = self.peek()
            if k == "op" and v == "/":
                self.take()
                steps.append(self.step())
            elif k == "op" and v == "//":
                self.take()
                steps.append(("descendant-or-self", ("type", "node"), []))
                steps.append(self.step())
            else:
                return steps

    def step(self):
        k, v = self.peek()
        if k == "op" and v == ".":
            self.take()
            return ("self", ("type", "node"), [])
        if k == "op" and v == "..":
            self.take()
            return ("parent", ("type", "node"), [])
        axis = "child"
        if k == "op" and v == "@":
            self.take()
            axis = "attribute"
        elif k == "name" and self.peek(1) == ("op", "::"):
            if v not in _AXES:
                raise TemplateError("invalid xpath axis: %s" % v)
            axis = v
            self.take()
            self.take()
        k, v = self.take()
        if k == "op" and v == "*":
            test = ("name", "*", "*")
        elif k == "name":
            if v in _NODE_TYPES and self.peek() == ("op", "("):
                self.take()
                if self.peek()[0] == "str":
                    self.take()
                self.expect(")")
                test = ("type", v)
            else:
                if ":" in v:
                    pfx, loc = v.split(":", 1)
                else:
                    pfx, loc = "", v
                test = ("name", pfx, loc)
        else:
            raise TemplateError("invalid xpath expression: %s" % self.expr)
        preds = []
        while self.peek() == ("op", "["):
            preds.append(self.predicate())
        return (axis, test, preds)


def _axis_nodes(node, axis):
    if axis == "child":
        return list(node.children)
    if axis == "attribute":
        return list(node.attrs)
    if axis == "self":
        return [node]
    if axis == "parent":
        return [node.parent] if node.parent is not None else []
    if axis in ("descendant", "descendant-or-self"):
        out = [node] if axis == "descendant-or-self" else []
        stack = list(reversed(node.children))
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(reversed(n.children))
        return out
    if axis in ("ancestor", "ancestor-or-self"):
        out = [node] if axis == "ancestor-or-self" else []
        p = node.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out
    if axis in ("following-sibling", "preceding-sibling"):
        if node.parent is None or node.type == "attr":
            return []
        sibs = node.parent.children
        idx = next(i for i, s in enumerate(sibs) if s is node)
        if axis == "following-sibling":
            return sibs[idx + 1:]
        return list(reversed(sibs[:idx]))
    if axis in ("following", "preceding"):
        root = node
        while root.parent is not None:
            root = root.parent
        alln = _axis_nodes(root, "descendant-or-self")
        if axis == "following":
            last = node
            while last.children:
                last = last.children[-1]
            return [n for n in alln if n.order > last.order]
        anc = set(id(a) for a in _axis_nodes(node, "ancestor"))
        return list(reversed([n for n in alln if n.order < node.order and id(n) not in anc]))
    return []


_REVERSE_AXES = {"ancestor", "ancestor-or-self", "preceding", "preceding-sibling"}


def _node_test(n, test, axis):
    if test[0] == "type":
        t = test[1]
        if t == "node":
            return True
        if t == "text":
            return n.type == "text"
        if t == "comment":
            return n.type == "comment"
        return n.type == "pi"
    principal = "attr" if axis == "attribute" else "elem"
    if n.type != principal:
        return False
    _, pfx, loc = test
    if pfx == "*" and loc == "*":
        return True
    if loc == "*":
        return n.prefix == pfx
    return n.local == loc and n.prefix == pfx


def _xp_string(v):
    if isinstance(v, list):
        return v[0].inner_text() if v else ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v in (float("inf"), float("-inf")):
            return "Infinity" if v > 0 else "-Infinity"
        return _go_format_float(v)
    return v


def _xp_number(v):
    if isinstance(v, list):
        v = _xp_string(v)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, float):
        return v
    try:
        s = v.strip()
        if not re.match(r"^-?(\d+(\.\d*)?|\.\d+)$", s):
            return float("nan")
        return float(s)
    except ValueError:
        return float("nan")


def _xp_bool(v):
    if isinstance(v, list):
        return len(v) > 0
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return v != 0 and v == v
    return len(v) > 0


def _xp_compare(op, a, b):
    def cmp(x, y):
        if op in ("=", "!="):
            if isinstance(x, bool) or isinstance(y, bool):
                r = _xp_bool(x) == _xp_bool(y)
            elif isinstance(x, float) or isinstance(y, float):
                r = _xp_number(x) == _xp_number(y)
            else:
                r = _xp_string(x) == _xp_string(y)
            return r if op == "=" else not r
        x, y = _xp_number(x), _xp_number(y)
        return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]

    al = a if isinstance(a, list) else None
    bl = b if isinstance(b, list) else None
    if al is not None and bl is not None:
        return any(cmp(x.inner_text(), y.inner_text()) for x in al for y in bl)
    if al is not None:
        if isinstance(b, bool):
            return cmp(_xp_bool(a), b)
        return any(cmp(x.inner_text(), b) for x in al)
    if bl is not None:
        if isinstance(a, bool):
            return cmp(a, _xp_bool(b))
        return any(cmp(a, y.inner_text()) for y in bl)
    return cmp(a, b)


def _xp_round(x):
    if x != x or x in (float("inf"), float("-inf")):
        return x
    return float(math.floor(x + 0.5))


class _XPEval:
    def __init__(self, root):
        self.root = root

    def ev(self, e, node, pos, size):
        k = e[0]
        if k == "const":
            return e[1]
        if k == "path":
            start = [self.root] if e[1] else [node]
            return self.steps(start, e[2])
        if k == "pathfrom":
            base = self.ev(e[1], node, pos, size)
            if not isinstance(base, list):
                raise TemplateError("xpath: expression must evaluate to a node-set")
            return self.steps(base, e[2])
        if k == "filter":
            base = self.ev(e[1], node, pos, size)
            if not isinstance(base, list):
                raise TemplateError("xpath: expression must evaluate to a node-set")
            for p in e[2]:
                base = self.apply_pred(base, p)
            return base
        if k == "union":
            a = self.ev(e[1], node, pos, size)
            b = self.ev(e[2], node, pos, size)
            if not isinstance(a, list) or not isinstance(b, list):
                raise TemplateError("xpath: union operands must be node-sets")
            seen = {}
            for n in a + b:
                seen[id(n)] = n
            return sorted(seen.values(), key=lambda n: n.order)
        if k == "neg":
            return -_xp_number(self.ev(e[1], node, pos, size))
        if k == "bin":
            op = e[1]
            if op == "or":
                return _xp_bool(self.ev(e[2], node, pos, size)) or _xp_bool(self.ev(e[3], node, pos, size))
            if op == "and":
                return _xp_bool(self.ev(e[2], node, pos, size)) and _xp_bool(self.ev(e[3], node, pos, size))
            a = self.ev(e[2], node, pos, size)
            b = self.ev(e[3], node, pos, size)
            if op in ("=", "!=", "<", "<=", ">", ">="):
                return _xp_compare(op, a, b)
            x, y = _xp_number(a), _xp_number(b)
            if op == "+":
                return x + y
            if op == "-":
                return x - y
            if op == "*":
                return x * y
            if op == "div":
                if y == 0:
                    if x == 0 or x != x:
                        return float("nan")
                    return math.copysign(float("inf"), x) * math.copysign(1.0, y)
                return x / y
            if op == "mod":
                if y == 0:
                    return float("nan")
                return math.fmod(x, y)
        if k == "call":
            return self.call(e[1], e[2], node, pos, size)
        raise TemplateError("xpath: unsupported expression")

    def steps(self, nodes, steps):
        for axis, test, preds in steps:
            seen = {}
            for n in nodes:
                cand = [c for c in _axis_nodes(n, axis) if _node_test(c, test, axis)]
                for p in preds:
                    cand = self.apply_pred(cand, p)
                for c in cand:
                    seen[id(c)] = c
            nodes = sorted(seen.values(), key=lambda n: n.order)
        return nodes

    def apply_pred(self, nodes, pred):
        out = []
        size = len(nodes)
        for i, n in enumerate(nodes):
            v = self.ev(pred, n, i + 1, size)
            if isinstance(v, float) and not isinstance(v, bool):
                if v == i + 1:
                    out.append(n)
            elif _xp_bool(v):
                out.append(n)
        return out

    def call(self, name, args, node, pos, size):
        def a(i):
            return self.ev(args[i], node, pos, size)

        def s(i):
            return _xp_string(a(i)) if i < len(args) else node.inner_text()

        n = len(args)
        if name == "last":
            return float(size)
        if name == "position":
            return float(pos)
        if name == "count":
            v = a(0)
            if not isinstance(v, list):
                raise TemplateError("xpath: count() requires a node-set")
            return float(len(v))
        if name in ("name", "local-name", "namespace-uri"):
            if n:
                v = a(0)
                if not isinstance(v, list):
                    raise TemplateError("xpath: %s() requires a node-set" % name)
                tgt = v[0] if v else None
            else:
                tgt = node
            if tgt is None or tgt.type not in ("elem", "attr", "pi"):
                return ""
            if name == "name":
                return tgt.name
            if name == "local-name":
                return tgt.local
            return ""
        if name == "string":
            return s(0)
        if name == "concat":
            return "".join(_xp_string(a(i)) for i in range(n))
        if name == "contains":
            return s(1) in s(0)
        if name == "starts-with":
            return s(0).startswith(s(1))
        if name == "ends-with":
            return s(0).endswith(s(1))
        if name == "substring-before":
            x, y = s(0), s(1)
            i = x.find(y)
            return x[:i] if i >= 0 else ""
        if name == "substring-after":
            x, y = s(0), s(1)
            i = x.find(y)
            return x[i + len(y):] if i >= 0 else ""
        if name == "substring":
            x = s(0)
            st = _xp_round(_xp_number(a(1)))
            if n > 2:
                ln = _xp_round(_xp_number(a(2)))
                end = st + ln
            else:
                end = float("inf")
            out = []
            for i, ch in enumerate(x):
                p = i + 1
                if p >= st and p < end:
                    out.append(ch)
            return "".join(out)
        if name == "string-length":
            return float(len(s(0)))
        if name == "normalize-space":
            return " ".join(s(0).split())
        if name == "translate":
            x, f, t = s(0), s(1), s(2)
            m = {}
            for i, ch in enumerate(f):
                if ch not in m:
                    m[ch] = t[i] if i < len(t) else None
            return "".join(m.get(ch, ch) or "" for ch in x if m.get(ch, ch) is not None)
        if name in ("lower-case", "upper-case"):
            return s(0).lower() if name == "lower-case" else s(0).upper()
        if name == "not":
            return not _xp_bool(a(0))
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "boolean":
            return _xp_bool(a(0))
        if name == "number":
            return _xp_number(a(0)) if n else _xp_number(node.inner_text())
        if name == "sum":
            v = a(0)
            if not isinstance(v, list):
                raise TemplateError("xpath: sum() requires a node-set")
            return float(sum(_xp_number(x.inner_text()) for x in v))
        if name == "floor":
            x = _xp_number(a(0))
            return float(math.floor(x)) if math.isfinite(x) else x
        if name == "ceiling":
            x = _xp_number(a(0))
            return float(math.ceil(x)) if math.isfinite(x) else x
        if name == "round":
            return _xp_round(_xp_number(a(0)))
        raise TemplateError("xpath: unsupported function %s()" % name)


_XP_CACHE = {}


def xpath_find_one(root, expr):
    ast = _XP_CACHE.get(expr)
    if ast is None:
        ast = _XPParser(expr).parse()
        if len(_XP_CACHE) > 1024:
            _XP_CACHE.clear()
        _XP_CACHE[expr] = ast
    res = _XPEval(root).ev(ast, root, 1, 1)
    if isinstance(res, list):
        return res[0] if res else None
    return None


def h_json_path(expr, data):
    _need_str(expr), _need_str(data)
    if data == "":
        return ""
    root = _json_to_xtree(data)
    n = xpath_find_one(root, expr)
    return n.inner_text() if n is not None else ""


def h_xml_path(expr, data):
    _need_str(expr), _need_str(data)
    if data == "":
        return ""
    root = _xml_to_xtree(data)
    n = xpath_find_one(root, expr)
    return n.inner_text() if n is not None else ""


# --- gjson-style dot paths ---------------------------------------------------

class GJ:
    """A parsed JSON value retaining its raw source text."""
    __slots__ = ("kind", "value", "raw")

    def __init__(self, kind, value, raw):
        self.kind = kind     # "obj", "arr", "str", "num", "true", "false", "null"
        self.value = value   # obj: list of (key, GJ); arr: list of GJ; str: str; num: float
        self.raw = raw


_GJ_WS = " \t\r\n"
_GJ_NUM = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_GJ_STR = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')


def gj_parse(text):
    n = len(text)

    def ws(i):
        while i < n and text[i] in _GJ_WS:
            i += 1
        return i

    def val(i, depth):
        if depth > 1000:
            raise ValueError("too deep")
        i = ws(i)
        if i >= n:
            raise ValueError("unexpected end")
        ch = text[i]
        if ch == "{":
            start = i
            items = []
            i = ws(i + 1)
            if i < n and text[i] == "}":
                return GJ("obj", items, text[start:i + 1]), i + 1
            while True:
                i = ws(i)
                m = _GJ_STR.match(text, i)
                if not m:
                    raise ValueError("bad key")
                key = _json.loads(m.group(0))
                i = ws(m.end())
                if i >= n or text[i] != ":":
                    raise ValueError("expected colon")
                v, i = val(i + 1, depth + 1)
                items.append((key, v))
                i = ws(i)
                if i < n and text[i] == ",":
                    i += 1
                    continue
                if i < n and text[i] == "}":
                    return GJ("obj", items, text[start:i + 1]), i + 1
                raise ValueError("bad object")
        if ch == "[":
            start = i
            items = []
            i = ws(i + 1)
            if i < n and text[i] == "]":
                return GJ("arr", items, text[start:i + 1]), i + 1
            while True:
                v, i = val(i, depth + 1)
                items.append(v)
                i = ws(i)
                if i < n and text[i] == ",":
                    i += 1
                    continue
                if i < n and text[i] == "]":
                    return GJ("arr", items, text[start:i + 1]), i + 1
                raise ValueError("bad array")
        if ch == '"':
            m = _GJ_STR.match(text, i)
            if not m:
                raise ValueError("bad string")
            return GJ("str", _json.loads(m.group(0)), m.group(0)), m.end()
        for lit in ("true", "false", "null"):
            if text.startswith(lit, i):
                return GJ(lit, None, lit), i + len(lit)
        m = _GJ_NUM.match(text, i)
        if m and m.end() > i:
            return GJ("num", float(m.group(0)), m.group(0)), m.end()
        raise ValueError("invalid character")

    v, i = val(0, 0)
    if ws(i) != n:
        raise ValueError("trailing data")
    return v


def _gj_split(path):
    """Split a gjson path into components, honouring escapes and #(...) queries."""
    parts = []
    cur = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == "\\" and i + 1 < n:
            cur.append(("esc", path[i + 1]))
            i += 2
            continue
        if ch == "#" and not cur and i + 1 < n and path[i + 1] == "(":
            depth = 0
            j = i + 1
            quote = False
            while j < n:
                c = path[j]
                if quote:
                    if c == "\\":
                        j += 1
                    elif c == '"':
                        quote = False
                elif c == '"':
                    quote = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            q = path[i + 2:j]
            j += 1
            multi = False
            if j < n and path[j] == "#":
                multi = True
                j += 1
            parts.append(("query", q, multi))
            if j < n and path[j] in ".|":
                j += 1
            i = j
            continue
        if ch in ".|":
            parts.append(("key", cur))
            cur = []
            i += 1
            continue
        cur.append(("ch", ch))
        i += 1
    if cur or (path and path[-1] in ".|"):
        parts.append(("key", cur))
    out = []
    for p in parts:
        if p[0] == "key":
            raw = "".join(c for _, c in p[1])
            wild = any(t == "ch" and c in "*?" for t, c in p[1])
            out.append(("key", raw, wild, p[1]))
        else:
            out.append(p)
    return out


def _gj_wild_match(chars, s):
    pat = []
    for t, c in chars:
        if t == "ch" and c == "*":
            pat.append(".*")
        elif t == "ch" and c == "?":
            pat.append(".")
        else:
            pat.append(re.escape(c))
    return re.fullmatch("".join(pat), s, re.S) is not None


def _gj_query_match(elem, q):
    m = re.match(r"^\s*(.*?)\s*(==|!=|<=|>=|<|>|%|!%|=)\s*(.*?)\s*$", q, re.S)
    if m:
        lhs_path, op, rhs = m.group(1), m.group(2), m.group(3)
    else:
        lhs_path, op, rhs = q.strip(), None, None
    if lhs_path in ("", "@this"):
        lhs = elem
    else:
        lhs = _gj_get(elem, _gj_split(lhs_path))
    if lhs is None:
        return False
    if op is None:
        return True
    if op == "=":
        op = "=="
    if rhs.startswith('"') and rhs.endswith('"') and len(rhs) >= 2:
        try:
            rv = _json.loads(rhs)
        except ValueError:
            rv = rhs[1:-1]
        rk = "str"
    elif rhs in ("true", "false"):
        rk, rv = "bool", rhs == "true"
    elif rhs == "null":
        rk, rv = "null", None
    else:
        try:
            rv = float(rhs)
            rk = "num"
        except ValueError:
            rk, rv = "str", rhs
    if rk == "str":
        if lhs.kind != "str":
            return False
        lv = lhs.value
        if op in ("%", "!%"):
            r = _gj_wild_match([("ch", c) for c in rv], lv)
            return r if op == "%" else not r
    elif rk == "num":
        if lhs.kind != "num":
            return False
        lv = lhs.value
    elif rk == "bool":
        if lhs.kind not in ("true", "false"):
            return False
        lv = lhs.kind == "true"
        if op not in ("==", "!="):
            return False
    else:
        r = lhs.kind == "null"
        return r if op == "==" else (not r if op == "!=" else False)
    return {"==": lv == rv, "!=": lv != rv, "<": lv < rv, "<=": lv <= rv,
            ">": lv > rv, ">=": lv >= rv}.get(op, False)


def _gj_arr(results):
    raw = "[" + ",".join(r.raw for r in results) + "]"
    return GJ("arr", results, raw)


def _gj_get(v, parts):
    if v is None:
        return None
    if not parts:
        return v
    p = parts[0]
    rest = parts[1:]
    if p[0] == "query":
        if v.kind != "arr":
            return None
        _, q, multi = p
        if multi:
            res = []
            for e in v.value:
                if _gj_query_match(e, q):
                    r = _gj_get(e, rest)
                    if r is not None:
                        res.append(r)
            return _gj_arr(res)
        for e in v.value:
            if _gj_query_match(e, q):
                return _gj_get(e, rest)
        return None
    _, key, wild, chars = p
    if v.kind == "arr":
        if key == "#" and not wild and not any(t == "esc" for t, _ in chars):
            if not rest:
                return GJ("num", float(len(v.value)), str(len(v.value)))
            res = []
            for e in v.value:
                r = _gj_get(e, rest)
                if r is not None:
                    res.append(r)
            return _gj_arr(res)
        if re.fullmatch(r"\d+", key):
            idx = int(key)
            if idx < len(v.value):
                return _gj_get(v.value[idx], rest)
        return None
    if v.kind == "obj":
        for k, child in v.value:
            if (wild and _gj_wild_match(chars, k)) or (not wild and k == key):
                return _gj_get(child, rest)
        return None
    return None


def _gj_string(r):
    if r is None or r.kind == "null":
        return ""
    if r.kind == "str":
        return r.value
    if r.kind in ("true", "false"):
        return r.kind
    if r.kind == "num":
        raw = r.raw
        if re.fullmatch(r"-?\d+", raw):
            return raw
        return _go_format_float(r.value)
    return r.raw


def h_gjson_path(expr, data):
    _need_str(expr), _need_str(data)
    if data == "":
        return ""
    try:
        doc = gj_parse(data)
    except (ValueError, RecursionError):
        raise TemplateError("invalid json")
    if expr == "":
        return ""
    return _gj_string(_gj_get(doc, _gj_split(expr)))


# --- misc --------------------------------------------------------------------

def h_uuidv5(data):
    return str(uuid.uuid5(uuid.NAMESPACE_OID, _need_str(data)))


_RE_CACHE = {}


def _go_regex(pattern):
    r = _RE_CACHE.get(pattern)
    if r is not None:
        return r
    p = pattern.replace("(?<", "(?P<").replace("(?P<=", "(?<=").replace("(?P<!", "(?<!")
    p = re.sub(r"(?<!\\)((?:\\\\)*)\\z", r"\1\\Z", p)
    try:
        r = re.compile(p)
    except re.error as e:
        raise TemplateError("regexp: Compile(%s): %s" % (go_quote(pattern), e))
    if len(_RE_CACHE) > 512:
        _RE_CACHE.clear()
    _RE_CACHE[pattern] = r
    return r


def h_regex_find_all_submatch(pattern, s):
    _need_str(pattern), _need_str(s)
    m = _go_regex(pattern).search(s)
    if not m:
        return []
    return [m.group(0)] + [g if g is not None else "" for g in m.groups()]


def h_regex_find_first_submatch(pattern, s):
    _need_str(pattern), _need_str(s)
    r = _go_regex(pattern)
    m = r.search(s)
    if not m or r.groups < 1:
        return ""
    return m.group(1) or ""


def h_hmac_sha256(secret, data):
    _need_str(secret), _need_str(data)
    return _hmac.new(secret.encode("utf-8", "surrogateescape"),
                     data.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()


def h_is_last_index(index, array):
    _need_int(index)
    if array is None:
        raise TemplateError("reflect: call of reflect.Value.Len on zero Value")
    if isinstance(array, str):
        n = len(array.encode("utf-8", "surrogateescape"))
    elif isinstance(array, (list, tuple, dict, bytes)):
        n = len(array)
    else:
        raise TemplateError("reflect: call of reflect.Value.Len on %s Value" % gotype(array))
    return index == n - 1


def h_html_escape_string(s):
    return f_html(_need_str(s))


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
    # hmock helpers
    "jsonPath": h_json_path,
    "gJsonPath": h_gjson_path,
    "xmlPath": h_xml_path,
    "uuidv5": h_uuidv5,
    "regexFindAllSubmatch": h_regex_find_all_submatch,
    "regexFindFirstSubmatch": h_regex_find_first_submatch,
    "hmacSHA256": h_hmac_sha256,
    "isLastIndex": h_is_last_index,
    "htmlEscapeString": h_html_escape_string,
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
                if kw == "template":
                    if len(toks) < 2 or toks[1].type != "CONST" or not isinstance(toks[1].value, str):
                        raise TemplateError("template name must be a string constant")
                    pipe = None
                    if len(toks) > 2:
                        pipe = self.parse_pipe(toks, 2, None, "template")[0]
                    nodes.append(("template", toks[1].value, pipe))
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
        self.depth = 0

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
            elif k == "template":
                val = self.eval_pipeline(dot, node[2]) if node[2] is not None else None
                sub = lookup_named_template(node[1])
                if self.depth >= 1000:
                    raise TemplateError("exceeded maximum template depth (1000)")
                ex = Executor(val, self.funcs)
                ex.depth = self.depth + 1
                ex.walk(val, sub, out)
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

# named templates registered by `kind: Template` mocks
NAMED_TEMPLATES = {}
_NAMED_PARSED = {}


def set_named_templates(mapping):
    global NAMED_TEMPLATES, _NAMED_PARSED
    NAMED_TEMPLATES = dict(mapping)
    _NAMED_PARSED = {}


def lookup_named_template(name):
    nodes = _NAMED_PARSED.get(name)
    if nodes is not None:
        return nodes
    src = NAMED_TEMPLATES.get(name)
    if src is None:
        raise TemplateError('no such template "%s"' % name)
    try:
        nodes = Parser(lex(preprocess(src)), FUNCS).parse()
    except RecursionError:
        raise TemplateError("template too deeply nested")
    _NAMED_PARSED[name] = nodes
    return nodes


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


def _resolve_template_file(templates_dir, rel):
    # like Go's filepath.Join: the file is always resolved inside templates_dir
    rel = rel.replace("\\", "/").lstrip("/")
    return os.path.normpath(os.path.join(templates_dir or ".", rel))


class ReplyHTTP:
    def __init__(self, spec, templates_dir=None):
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
        bff = spec.get("body_from_file")
        self.body_from_file = "" if bff is None else _to_text(bff)
        if self.body == "" and self.body_from_file != "":
            path = _resolve_template_file(templates_dir, self.body_from_file)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                raise ValidationError("reply_http.body_from_file: cannot read %s: %s"
                                      % (self.body_from_file, e.strerror or e))
            # snapshot of the file contents for this loaded configuration
            self.body = _dec(data)


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


class RedisAction:
    def __init__(self, spec):
        if spec is None:
            spec = []
        if isinstance(spec, (str, int, float, bool)):
            spec = [spec]
        if not isinstance(spec, list):
            raise ValidationError("redis must be a list of template strings")
        self.items = [_to_text(x) for x in spec]


class SendHTTP:
    def __init__(self, spec, templates_dir=None):
        if not isinstance(spec, dict):
            raise ValidationError("send_http must be a mapping")
        url = spec.get("url")
        if url is None or _to_text(url) == "":
            raise ValidationError("send_http.url is required")
        self.url = _to_text(url)
        method = spec.get("method")
        if method is None or _to_text(method).strip() == "":
            raise ValidationError("send_http.method is required")
        self.method = _to_text(method).strip().upper()
        headers = spec.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValidationError("send_http.headers must be a mapping")
        self.headers = [(str(k), _to_text(v)) for k, v in headers.items()]
        self.body = _to_text(spec.get("body"))
        bff = spec.get("body_from_file")
        self.body_from_file = "" if bff is None else _to_text(bff)
        if self.body == "" and self.body_from_file != "":
            path = _resolve_template_file(templates_dir, self.body_from_file)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                raise ValidationError("send_http.body_from_file: cannot read %s: %s"
                                      % (self.body_from_file, e.strerror or e))
            self.body = _dec(data)


SEND_HTTP_TIMEOUT = 5.0


def do_send_http(method, url, headers, body):
    """Perform an outbound request; returns (status, body) or raises."""
    import http.client
    sp = urllib.parse.urlsplit(url.strip())
    scheme = (sp.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise ValueError("unsupported protocol scheme %r" % sp.scheme)
    if not sp.hostname:
        raise ValueError("no host in request URL")
    if scheme == "https":
        conn = http.client.HTTPSConnection(sp.hostname, sp.port, timeout=SEND_HTTP_TIMEOUT)
    else:
        conn = http.client.HTTPConnection(sp.hostname, sp.port, timeout=SEND_HTTP_TIMEOUT)
    target = sp.path or "/"
    if sp.query:
        target += "?" + sp.query
    target = target.replace(" ", "%20")
    data = _enc(body)
    try:
        conn.putrequest(method, target, skip_accept_encoding=True)
        names = {k.lower() for k, _ in headers}
        if "user-agent" not in names:
            conn.putheader("User-Agent", "hmock")
        for k, v in headers:
            if k.lower() == "content-length":
                continue
            conn.putheader(k, v)
        if data or method not in ("GET", "HEAD", "DELETE", "OPTIONS", "TRACE"):
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data if data else None)
        resp = conn.getresponse()
        rb = resp.read()
        return resp.status, _dec(rb)
    finally:
        conn.close()


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


_KIND_FIELDS = {
    "Behavior": {"key", "kind", "extend", "expect", "actions", "values"},
    "AbstractBehavior": {"key", "kind", "expect", "actions", "values"},
    "Template": {"key", "kind", "template"},
}
_ALL_FIELDS = {"key", "kind", "extend", "expect", "actions", "values", "template"}


def _is_zero(v):
    return v is None or v == "" or (isinstance(v, (dict, list)) and len(v) == 0)


def _check_fields(raw, kind):
    allowed = _KIND_FIELDS[kind]
    for f in _ALL_FIELDS:
        if f not in allowed and f in raw and not _is_zero(raw[f]):
            raise ValidationError("field %r is not allowed for kind %s" % (f, kind))


def _action_order(a):
    o = a.get("order", 0)
    if o is None:
        return 0
    if isinstance(o, bool):
        raise ValidationError("action order must be an integer")
    if isinstance(o, float) and o.is_integer():
        o = int(o)
    if isinstance(o, str):
        try:
            o = int(o.strip())
        except ValueError:
            raise ValidationError("action order must be an integer")
    if not isinstance(o, int):
        raise ValidationError("action order must be an integer")
    return o


def _deep_merge(parent, child):
    if isinstance(parent, dict) and isinstance(child, dict):
        out = dict(parent)
        for k, v in child.items():
            if k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    if _is_zero(child):
        return parent
    return child


def _merge_raw(parent, child):
    out = {}
    for k in set(parent) | set(child):
        if k == "extend":
            continue
        pv, cv = parent.get(k), child.get(k)
        if k == "values":
            m = dict(pv) if isinstance(pv, dict) else {}
            if isinstance(cv, dict):
                m.update(cv)
            elif cv is not None:
                m = cv  # invalid; let validation reject it
            out[k] = m
        elif k == "actions":
            pa = pv if isinstance(pv, list) else ([] if pv is None else pv)
            ca = cv if isinstance(cv, list) else ([] if cv is None else cv)
            if isinstance(pa, list) and isinstance(ca, list):
                out[k] = list(pa) + list(ca)
            else:
                out[k] = cv if not isinstance(ca, list) else pv
        elif k == "expect":
            if isinstance(pv, dict) and isinstance(cv, dict):
                out[k] = _deep_merge(pv, cv)
            elif _is_zero(cv):
                out[k] = pv
            else:
                out[k] = cv
        else:
            out[k] = pv if _is_zero(cv) else cv
    return out


class Behavior:
    def __init__(self, raw, source, templates_dir=None):
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
        if self.kind not in ("Behavior", "AbstractBehavior"):
            raise ValidationError("invalid kind %r" % self.kind)
        _check_fields(raw, self.kind)
        values = raw.get("values")
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValidationError("values must be a mapping")
        self.values = values
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
        ordered = []
        for a in actions:
            if not isinstance(a, dict):
                raise ValidationError("each action must be a mapping")
            ordered.append((_action_order(a), a))
        ordered.sort(key=lambda t: t[0])  # stable
        for _, a in ordered:
            for name, spec in a.items():
                if name == "reply_http":
                    replies += 1
                    if replies > 1:
                        raise ValidationError("more than one reply_http action")
                    self.actions.append(("reply_http", ReplyHTTP(spec, templates_dir)))
                elif name == "sleep":
                    self.actions.append(("sleep", Sleep(spec)))
                elif name == "redis":
                    self.actions.append(("redis", RedisAction(spec)))
                elif name == "send_http":
                    self.actions.append(("send_http", SendHTTP(spec, templates_dir)))
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

    # first pass: per-item structural validation, split templates/behaviors
    templates = {}
    entries = []  # (item, path) of Behavior / AbstractBehavior
    for item, path in raw:
        try:
            if not isinstance(item, dict):
                raise ValidationError("mock must be a mapping")
            key = item.get("key")
            if not isinstance(key, str) or key == "":
                raise ValidationError("key is required and must be a non-empty string")
            kind = item.get("kind")
            if kind is None or kind == "":
                kind = "Behavior"
            if not isinstance(kind, str) or kind not in _KIND_FIELDS:
                raise ValidationError("invalid kind %r" % (kind,))
            _check_fields(item, kind)
            if kind == "Template":
                src = item.get("template")
                if src is None:
                    raise ValidationError("template is required")
                src = _to_text(src)
                try:
                    Parser(lex(preprocess(src)), FUNCS).parse()
                except RecursionError:
                    raise ValidationError("template too deeply nested")
                except TemplateError as e:
                    raise ValidationError("invalid template: %s" % e)
                if key in templates:
                    LOG.warn("duplicate template key; overriding previous definition", key=key)
                templates[key] = src
                continue
            ext = item.get("extend")
            if ext is not None and not isinstance(ext, str):
                raise ValidationError("extend must be a string")
            if item.get("values") is not None and not isinstance(item.get("values"), dict):
                raise ValidationError("values must be a mapping")
            if item.get("actions") is not None and not isinstance(item.get("actions"), list):
                raise ValidationError("actions must be a list")
            if item.get("expect") is not None and not isinstance(item.get("expect"), dict):
                raise ValidationError("expect must be a mapping")
        except ValidationError as e:
            key = item.get("key") if isinstance(item, dict) else None
            LOG.error("invalid mock rejected", file=path, key=_to_text(key), err=str(e))
            continue
        entries.append((item, path))
    set_named_templates(templates)

    # duplicate keys: the last loaded mock wins
    last_index = {}
    for i, (item, path) in enumerate(entries):
        k = item["key"]
        if k in last_index:
            prev = entries[last_index[k]]
            LOG.warn("duplicate mock key; overriding previous definition",
                     key=k, previous_file=prev[1], file=path)
        last_index[k] = i
    entries = [e for i, e in enumerate(entries) if last_index[e[0]["key"]] == i]
    defs = {item["key"]: item for item, _ in entries}

    resolved = {}

    def resolve(key, stack):
        if key in resolved:
            return resolved[key]
        item = defs[key]
        ext = item.get("extend")
        if ext:
            if ext in defs and ext not in stack:
                parent = resolve(ext, stack | {key})
                merged = _merge_raw(parent, item)
            else:
                LOG.warn("extend target not found; extension skipped", key=key, extend=ext)
                merged = {k: v for k, v in item.items() if k != "extend"}
        else:
            merged = {k: v for k, v in item.items() if k != "extend"}
        if key not in stack:
            resolved[key] = merged
        return merged

    result = []
    for item, path in entries:
        merged = resolve(item["key"], frozenset())
        try:
            b = Behavior(merged, path, templates_dir)
        except ValidationError as e:
            LOG.error("invalid mock rejected", file=path, key=_to_text(item.get("key")), err=str(e))
            continue
        result.append(b)
    LOG.info("mocks loaded", count=len(result), dir=templates_dir)
    return result


# ---------------------------------------------------------------------------
# Redis backend (embedded in-memory store or external Redis over RESP)
# ---------------------------------------------------------------------------

import socket


class RedisError(Exception):
    pass


_WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"


def _redis_glob_to_re(pat):
    out = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        elif c == "[":
            j = i + 1
            neg = False
            if j < n and pat[j] == "^":
                neg = True
                j += 1
            parts = []
            while j < n and pat[j] != "]":
                if pat[j] == "\\" and j + 1 < n:
                    j += 1
                    parts.append(re.escape(pat[j]))
                elif j + 2 < n and pat[j + 1] == "-" and pat[j + 2] != "]":
                    a, b = pat[j], pat[j + 2]
                    if a > b:
                        a, b = b, a
                    parts.append(re.escape(a) + "-" + re.escape(b))
                    j += 2
                else:
                    parts.append(re.escape(pat[j]))
                j += 1
            if j >= n:  # unterminated: treat literally
                out.append(re.escape("["))
            else:
                body = "".join(parts)
                if not body:
                    out.append("(?!)" if not neg else ".")
                else:
                    out.append("[" + ("^" if neg else "") + body + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.S)


def _rint(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        raise RedisError("ERR value is not an integer or out of range")


class MemoryRedis:
    """A small embedded Redis-compatible store."""

    def __init__(self):
        self.data = {}
        self.expires = {}
        self.lock = threading.RLock()

    # -- helpers ----------------------------------------------------------
    def _alive(self, key):
        exp = self.expires.get(key)
        if exp is not None and time.time() >= exp:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def _get(self, key, typ):
        if not self._alive(key):
            return None
        v = self.data[key]
        if not isinstance(v, typ):
            raise RedisError(_WRONGTYPE)
        return v

    def _set(self, key, v):
        self.data[key] = v
        self.expires.pop(key, None)

    def _cleanup(self, key):
        v = self.data.get(key)
        if isinstance(v, (list, dict)) and not v:
            self.data.pop(key, None)
            self.expires.pop(key, None)

    @staticmethod
    def _arity(args, n, name, exact=False):
        if (exact and len(args) != n) or len(args) < n:
            raise RedisError("ERR wrong number of arguments for '%s' command" % name.lower())

    def do(self, cmd, *args):
        name = cmd.upper()
        fn = getattr(self, "c_" + name, None)
        if fn is None:
            raise RedisError("ERR unknown command '%s'" % cmd)
        with self.lock:
            return fn(name, list(args))

    # -- strings ----------------------------------------------------------
    def c_SET(self, n, a):
        self._arity(a, 2, n)
        key, val = a[0], a[1]
        nx = xx = keepttl = get = False
        exp = None
        i = 2
        while i < len(a):
            o = a[i].upper()
            if o in ("EX", "PX", "EXAT", "PXAT") and i + 1 < len(a):
                t = _rint(a[i + 1])
                if t <= 0 and o in ("EX", "PX"):
                    raise RedisError("ERR invalid expire time in 'set' command")
                exp = {"EX": time.time() + t, "PX": time.time() + t / 1000.0,
                       "EXAT": float(t), "PXAT": t / 1000.0}[o]
                i += 2
                continue
            if o == "NX":
                nx = True
            elif o == "XX":
                xx = True
            elif o == "KEEPTTL":
                keepttl = True
            elif o == "GET":
                get = True
            else:
                raise RedisError("ERR syntax error")
            i += 1
        exists = self._alive(key)
        old = None
        if get:
            old = self._get(key, str)
        if (nx and exists) or (xx and not exists):
            return old if get else None
        ttl = self.expires.get(key) if keepttl else None
        self._set(key, val)
        if exp is not None:
            self.expires[key] = exp
        elif ttl is not None:
            self.expires[key] = ttl
        return old if get else "OK"

    def c_GET(self, n, a):
        self._arity(a, 1, n, True)
        return self._get(a[0], str)

    def c_SETNX(self, n, a):
        self._arity(a, 2, n, True)
        if self._alive(a[0]):
            return 0
        self._set(a[0], a[1])
        return 1

    def c_APPEND(self, n, a):
        self._arity(a, 2, n, True)
        cur = self._get(a[0], str) or ""
        ttl = self.expires.get(a[0])
        self._set(a[0], cur + a[1])
        if ttl is not None:
            self.expires[a[0]] = ttl
        return len(_enc(cur + a[1]))

    def _incr(self, key, by):
        cur = self._get(key, str)
        v = _rint(cur if cur is not None else "0") + by
        ttl = self.expires.get(key)
        self._set(key, str(v))
        if ttl is not None:
            self.expires[key] = ttl
        return v

    def c_INCR(self, n, a):
        self._arity(a, 1, n, True)
        return self._incr(a[0], 1)

    def c_DECR(self, n, a):
        self._arity(a, 1, n, True)
        return self._incr(a[0], -1)

    def c_INCRBY(self, n, a):
        self._arity(a, 2, n, True)
        return self._incr(a[0], _rint(a[1]))

    def c_DECRBY(self, n, a):
        self._arity(a, 2, n, True)
        return self._incr(a[0], -_rint(a[1]))

    # -- lists ------------------------------------------------------------
    def _push(self, n, a, left):
        self._arity(a, 2, n)
        lst = self._get(a[0], list)
        if lst is None:
            lst = []
            self.data[a[0]] = lst
        for v in a[1:]:
            if left:
                lst.insert(0, v)
            else:
                lst.append(v)
        return len(lst)

    def c_RPUSH(self, n, a):
        return self._push(n, a, False)

    def c_LPUSH(self, n, a):
        return self._push(n, a, True)

    def c_LRANGE(self, n, a):
        self._arity(a, 3, n, True)
        lst = self._get(a[0], list) or []
        start, stop = _rint(a[1]), _rint(a[2])
        ln = len(lst)
        if start < 0:
            start = max(ln + start, 0)
        if stop < 0:
            stop = ln + stop
        if stop >= ln:
            stop = ln - 1
        if start > stop or start >= ln:
            return []
        return lst[start:stop + 1]

    def _pop(self, n, a, left):
        if len(a) not in (1, 2):
            self._arity(a, 99, n, True)
        lst = self._get(a[0], list)
        if len(a) == 2:
            cnt = _rint(a[1])
            if cnt < 0:
                raise RedisError("ERR value is out of range, must be positive")
            if lst is None:
                return None
            out = []
            while lst and len(out) < cnt:
                out.append(lst.pop(0) if left else lst.pop())
            self._cleanup(a[0])
            return out
        if not lst:
            return None
        v = lst.pop(0) if left else lst.pop()
        self._cleanup(a[0])
        return v

    def c_LPOP(self, n, a):
        return self._pop(n, a, True)

    def c_RPOP(self, n, a):
        return self._pop(n, a, False)

    def c_LLEN(self, n, a):
        self._arity(a, 1, n, True)
        return len(self._get(a[0], list) or [])

    def c_LINDEX(self, n, a):
        self._arity(a, 2, n, True)
        lst = self._get(a[0], list) or []
        i = _rint(a[1])
        if i < 0:
            i += len(lst)
        return lst[i] if 0 <= i < len(lst) else None

    # -- hashes -----------------------------------------------------------
    def c_HSET(self, n, a):
        if len(a) < 3 or len(a) % 2 != 1:
            raise RedisError("ERR wrong number of arguments for '%s' command" % n.lower())
        h = self._get(a[0], dict)
        if h is None:
            h = {}
            self.data[a[0]] = h
        added = 0
        for i in range(1, len(a), 2):
            if a[i] not in h:
                added += 1
            h[a[i]] = a[i + 1]
        return added

    def c_HMSET(self, n, a):
        self.c_HSET(n, a)
        return "OK"

    def c_HSETNX(self, n, a):
        self._arity(a, 3, n, True)
        h = self._get(a[0], dict)
        if h is not None and a[1] in h:
            return 0
        return self.c_HSET(n, a)

    def c_HGET(self, n, a):
        self._arity(a, 2, n, True)
        h = self._get(a[0], dict) or {}
        return h.get(a[1])

    def c_HMGET(self, n, a):
        self._arity(a, 2, n)
        h = self._get(a[0], dict) or {}
        return [h.get(f) for f in a[1:]]

    def c_HGETALL(self, n, a):
        self._arity(a, 1, n, True)
        h = self._get(a[0], dict) or {}
        out = []
        for k, v in h.items():
            out.append(k)
            out.append(v)
        return out

    def c_HKEYS(self, n, a):
        self._arity(a, 1, n, True)
        return list((self._get(a[0], dict) or {}).keys())

    def c_HVALS(self, n, a):
        self._arity(a, 1, n, True)
        return list((self._get(a[0], dict) or {}).values())

    def c_HLEN(self, n, a):
        self._arity(a, 1, n, True)
        return len(self._get(a[0], dict) or {})

    def c_HEXISTS(self, n, a):
        self._arity(a, 2, n, True)
        return 1 if a[1] in (self._get(a[0], dict) or {}) else 0

    def c_HDEL(self, n, a):
        self._arity(a, 2, n)
        h = self._get(a[0], dict)
        if h is None:
            return 0
        cnt = 0
        for f in a[1:]:
            if f in h:
                del h[f]
                cnt += 1
        self._cleanup(a[0])
        return cnt

    # -- keys -------------------------------------------------------------
    def c_DEL(self, n, a):
        self._arity(a, 1, n)
        cnt = 0
        for k in a:
            if self._alive(k):
                del self.data[k]
                self.expires.pop(k, None)
                cnt += 1
        return cnt

    c_UNLINK = c_DEL

    def c_EXISTS(self, n, a):
        self._arity(a, 1, n)
        return sum(1 for k in a if self._alive(k))

    def c_KEYS(self, n, a):
        self._arity(a, 1, n, True)
        rx = _redis_glob_to_re(a[0])
        return sorted(k for k in list(self.data) if self._alive(k) and rx.match(k))

    def c_TYPE(self, n, a):
        self._arity(a, 1, n, True)
        if not self._alive(a[0]):
            return "none"
        v = self.data[a[0]]
        return "string" if isinstance(v, str) else "list" if isinstance(v, list) else "hash"

    def c_EXPIRE(self, n, a):
        self._arity(a, 2, n)
        if not self._alive(a[0]):
            return 0
        self.expires[a[0]] = time.time() + _rint(a[1])
        self._alive(a[0])
        return 1

    def c_TTL(self, n, a):
        self._arity(a, 1, n, True)
        if not self._alive(a[0]):
            return -2
        exp = self.expires.get(a[0])
        if exp is None:
            return -1
        return max(0, int(round(exp - time.time())))

    def c_PERSIST(self, n, a):
        self._arity(a, 1, n, True)
        if self._alive(a[0]) and a[0] in self.expires:
            del self.expires[a[0]]
            return 1
        return 0

    def c_FLUSHALL(self, n, a):
        self.data.clear()
        self.expires.clear()
        return "OK"

    c_FLUSHDB = c_FLUSHALL

    def c_DBSIZE(self, n, a):
        return sum(1 for k in list(self.data) if self._alive(k))

    def c_PING(self, n, a):
        return a[0] if a else "PONG"

    def c_ECHO(self, n, a):
        self._arity(a, 1, n, True)
        return a[0]


class RespRedis:
    """Minimal RESP client for an external Redis server."""

    def __init__(self, url, timeout=5.0):
        sp = urllib.parse.urlsplit(url if "://" in url else "redis://" + url)
        self.tls = sp.scheme == "rediss"
        self.host = sp.hostname or "localhost"
        self.port = sp.port or 6379
        self.username = urllib.parse.unquote(sp.username) if sp.username else None
        self.password = urllib.parse.unquote(sp.password) if sp.password else None
        db = (sp.path or "").strip("/")
        q = urllib.parse.parse_qs(sp.query)
        if not db and q.get("db"):
            db = q["db"][0]
        self.db = int(db) if db.isdigit() else 0
        self.timeout = timeout
        self.sock = None
        self.rf = None
        self.lock = threading.Lock()

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.tls:
            import ssl
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        self.sock = s
        self.rf = s.makefile("rb")
        if self.password is not None:
            if self.username:
                self._roundtrip(["AUTH", self.username, self.password])
            else:
                self._roundtrip(["AUTH", self.password])
        if self.db:
            self._roundtrip(["SELECT", str(self.db)])

    def _close(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self.rf = None

    def _roundtrip(self, parts):
        buf = [b"*%d\r\n" % len(parts)]
        for p in parts:
            b = _enc(p)
            buf.append(b"$%d\r\n%s\r\n" % (len(b), b))
        self.sock.sendall(b"".join(buf))
        return self._read()

    def _read(self):
        line = self.rf.readline()
        if not line:
            raise ConnectionError("connection closed by redis")
        t, rest = line[:1], line[1:].rstrip(b"\r\n")
        if t == b"+":
            return _dec(rest)
        if t == b"-":
            raise RedisError(_dec(rest))
        if t == b":":
            return int(rest)
        if t == b"$":
            ln = int(rest)
            if ln < 0:
                return None
            data = self.rf.read(ln + 2)
            return _dec(data[:ln])
        if t == b"*":
            ln = int(rest)
            if ln < 0:
                return None
            return [self._read() for _ in range(ln)]
        raise ConnectionError("bad redis protocol reply")

    def do(self, cmd, *args):
        parts = [cmd] + list(args)
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()
                    return self._roundtrip(parts)
                except RedisError:
                    raise
                except (OSError, ConnectionError, ValueError) as e:
                    self._close()
                    if attempt:
                        raise RedisError("redis connection error: %s" % e)


REDIS = None
_redis_init_lock = threading.Lock()


def make_redis():
    typ = (os.environ.get("HM_REDIS_TYPE") or "memory").strip().lower()
    if typ == "redis":
        url = os.environ.get("HM_REDIS_URL") or "redis://redis:6379"
        return RespRedis(url)
    return MemoryRedis()


def get_redis():
    global REDIS
    if REDIS is None:
        with _redis_init_lock:
            if REDIS is None:
                REDIS = make_redis()
    return REDIS


def _redis_arg(v):
    if v is None or v is MISSING:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple, dict, bytes)):
        return gofmt(v)
    return gofmt(v)


def _redis_result(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return ";;".join(_redis_result(x) for x in v)
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return str(v)


def h_redis_do(command, *args):
    cmd = _redis_arg(command)
    argv = [_redis_arg(a) for a in args]
    try:
        res = get_redis().do(cmd, *argv)
    except Exception as e:  # noqa: BLE001
        try:
            LOG.error("redisDo failed", command=cmd, args=" ".join(argv), err=str(e))
        except NameError:
            pass
        return ""
    return _redis_result(res)


FUNCS["redisDo"] = h_redis_do


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
            "Values": {},
        }

        status, resp_headers, resp_body = 404, [("Content-Type", "text/plain; charset=utf-8")], "not found"
        matched_key = None
        for b in self.behaviors:
            params = b.match_route(method, decoded_path)
            if params is None:
                continue
            ctx["HTTPPathParams"] = params
            ctx["Values"] = b.values
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
            elif name == "redis":
                for item in act.items:
                    try:
                        render(item, ctx)
                    except Exception as e:  # noqa: BLE001
                        LOG.error("failed to render redis action", key=b.key, err=str(e))
            elif name == "send_http":
                self._send_http(b, act, ctx)
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


    def _send_http(self, b, act, ctx):
        try:
            url = render(act.url, ctx)
            hs = []
            for k, v in act.headers:
                v = render(v, ctx)
                if "\r" in k or "\n" in k:
                    continue
                hs.append((k, v.replace("\r", " ").replace("\n", " ")))
            body = render(act.body, ctx)
        except Exception as e:  # noqa: BLE001
            LOG.error("failed to render send_http", key=b.key, err=str(e))
            return
        try:
            status, rbody = do_send_http(act.method, url, hs, body)
        except Exception as e:  # noqa: BLE001
            LOG.error("send_http request failed", key=b.key, method=act.method,
                      url=_log_str(url), err=str(e))
            return
        LOG.info("send_http", key=b.key, method=act.method, url=_log_str(url),
                 req=_log_str(body), status=status, res=_log_str(rbody))


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

    global REDIS
    try:
        REDIS = make_redis()
    except Exception as e:  # noqa: BLE001
        LOG.error("failed to configure redis", err=str(e))
        REDIS = MemoryRedis()
    LOG.info("redis backend", type=(os.environ.get("HM_REDIS_TYPE") or "memory"))

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
