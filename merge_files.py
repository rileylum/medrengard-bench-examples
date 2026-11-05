#!/usr/bin/env python3
"""Multi-format merger and sorter.

Ingests CSV, TSV, JSON Lines and Parquet files (optionally gzip-compressed),
reconciles their schemas (explicit or inferred), casts every value to the
resolved column type and writes a single CSV sorted globally by a composite
key, using an external merge sort bounded by --memory-limit-mb.

With --partition-by and/or --max-rows-per-file / --max-bytes-per-file the
output is a directory of part-NNNNN.csv files (Hive-style col=value
subdirectories for field partitions), written atomically via a sibling
temporary directory.

With --schema, columns may use nested types (struct, array<T>,
map<string,T>, json); they are cast recursively and emitted as canonical
minified JSON.  --key/--partition-by accept field paths such as user.id,
items.0.qty or attrs["country"] that must resolve to primitives.  Type names
are case-insensitive and may use built-in aliases or --type-alias-file.
Errors are reported on stderr as "ERR <code> <message>".

Exit codes:
  0  success
  1  generic / I/O error (e.g. missing input file)
  2  invalid usage (bad arguments, undetectable input format, bad alias
     file or alias cycle)
  3  schema error (invalid schema file, key column not in schema)
  4  cast failure with --on-type-error=fail
  5  malformed input (CSV/TSV/JSONL parse error, compression mismatch)
  6  nested structure in JSONL/Parquet input without --schema (or
     incompatible with it)
"""

import argparse
import csv
import datetime as _dt
import decimal
import gzip
import heapq
import json
import math
import os
import pickle
import re
import shutil
import signal
import sys
import tempfile
import urllib.parse
import zlib

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

VALID_TYPES = ("string", "int", "float", "bool", "date", "timestamp")
# Priority used when inferring: most specific first.
TYPE_PRIORITY = ("date", "timestamp", "bool", "int", "float", "string")

TRUE_VALUES = {"true", "t", "yes", "y", "1", "on"}
FALSE_VALUES = {"false", "f", "no", "n", "0", "off"}

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(
    r"^[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)$")
FLOAT_SPECIAL_RE = re.compile(r"^[+-]?(?:inf|infinity|nan)$", re.IGNORECASE)
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"(Z|z|[+-]\d{2}(?::?\d{2})?)?)?$")

EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_SCHEMA = 3
EXIT_CAST = 4
EXIT_PARSE = 5
EXIT_NESTED = 6


class CastError(Exception):
    pass


class UserError(Exception):
    def __init__(self, msg, code=EXIT_GENERIC):
        Exception.__init__(self, msg)
        self.code = code


# --------------------------------------------------------------------------
# Parsing / casting
# --------------------------------------------------------------------------

def parse_int(s):
    t = s.strip()
    if not INT_RE.match(t):
        raise CastError
    return int(t)


def parse_float(s):
    t = s.strip()
    if FLOAT_RE.match(t) or FLOAT_SPECIAL_RE.match(t):
        return float(t)
    raise CastError


def parse_bool(s):
    t = s.strip().lower()
    if t in TRUE_VALUES:
        return True
    if t in FALSE_VALUES:
        return False
    raise CastError


def parse_date(s):
    m = DATE_RE.match(s.strip())
    if not m:
        raise CastError
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise CastError


def parse_timestamp(s, require_time=False):
    m = TS_RE.match(s.strip())
    if not m:
        raise CastError
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    if hh is None:
        if require_time:
            raise CastError
        hh = mi = "0"
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    try:
        tzinfo = _dt.timezone.utc
        if tz and tz not in ("Z", "z"):
            sign = -1 if tz[0] == "-" else 1
            rest = tz[1:].replace(":", "")
            oh = int(rest[:2])
            om = int(rest[2:4]) if len(rest) > 2 else 0
            if oh > 23 or om > 59:
                raise CastError
            tzinfo = _dt.timezone(sign * _dt.timedelta(hours=oh, minutes=om))
        value = _dt.datetime(int(y), int(mo), int(d), int(hh), int(mi),
                             int(ss or 0), micro, tzinfo=tzinfo)
        return value.astimezone(_dt.timezone.utc)
    except (ValueError, OverflowError):
        raise CastError


def format_float(v):
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return repr(v)


def format_date(v):
    return "%04d-%02d-%02d" % (v.year, v.month, v.day)


def format_timestamp(v):
    out = "%04d-%02d-%02dT%02d:%02d:%02d" % (
        v.year, v.month, v.day, v.hour, v.minute, v.second)
    if v.microsecond:
        out += "." + ("%06d" % v.microsecond).rstrip("0")
    return out + "Z"


def ts_sort_value(v):
    delta = v - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def to_utc(v):
    if v.tzinfo is None:
        return v.replace(tzinfo=_dt.timezone.utc)
    return v.astimezone(_dt.timezone.utc)


def cast_text(text, typ):
    """Cast a raw string. Return (output_text, sort_key) or raise CastError."""
    if typ == "string":
        return text, (1, text)
    if typ == "int":
        v = parse_int(text)
        return str(v), (1, v)
    if typ == "float":
        return float_result(parse_float(text))
    if typ == "bool":
        v = parse_bool(text)
        return ("true" if v else "false"), (1, v)
    if typ == "date":
        v = parse_date(text)
        return format_date(v), (1, v.toordinal())
    if typ == "timestamp":
        v = parse_timestamp(text)
        return format_timestamp(v), (1, ts_sort_value(v))
    raise CastError


def float_result(v):
    if math.isnan(v):
        return format_float(v), (1, 1)
    return format_float(v), (1, 0, v)


def to_text(v):
    """Canonical textual representation of a typed value."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format_float(v)
    if isinstance(v, _dt.datetime):
        return format_timestamp(to_utc(v))
    if isinstance(v, _dt.date):
        return format_date(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (dict, list, tuple)):
        return canonical_json(normalize_json(v))
    return str(v)


def cast_value(v, typ):
    """Cast a raw string or typed value to typ.

    Returns (output_text, sort_key) or raises CastError.
    """
    if isinstance(v, str):
        return cast_text(v, typ)
    if isinstance(v, (dict, list, tuple)):
        raise CastError
    if typ == "string":
        t = to_text(v)
        return t, (1, t)
    if isinstance(v, (bytes, bytearray)):
        return cast_text(bytes(v).decode("utf-8", "replace"), typ)
    if typ == "int":
        if isinstance(v, bool):
            raise CastError
        if isinstance(v, int):
            return str(v), (1, v)
        if isinstance(v, (float, decimal.Decimal)):
            try:
                if v == v and not math.isinf(v) and v == int(v):
                    iv = int(v)
                    return str(iv), (1, iv)
            except (ValueError, OverflowError, ArithmeticError):
                pass
        raise CastError
    if typ == "float":
        if isinstance(v, bool):
            raise CastError
        if isinstance(v, (int, float, decimal.Decimal)):
            try:
                return float_result(float(v))
            except (OverflowError, ValueError):
                raise CastError
        raise CastError
    if typ == "bool":
        if isinstance(v, bool):
            return ("true" if v else "false"), (1, v)
        if isinstance(v, int) and v in (0, 1):
            return ("true" if v else "false"), (1, bool(v))
        raise CastError
    if typ == "date":
        if isinstance(v, _dt.datetime):
            u = to_utc(v)
            if (u.hour, u.minute, u.second, u.microsecond) != (0, 0, 0, 0):
                raise CastError
            d = u.date()
            return format_date(d), (1, d.toordinal())
        if isinstance(v, _dt.date):
            return format_date(v), (1, v.toordinal())
        raise CastError
    if typ == "timestamp":
        if isinstance(v, _dt.datetime):
            u = to_utc(v)
            return format_timestamp(u), (1, ts_sort_value(u))
        if isinstance(v, _dt.date):
            u = _dt.datetime(v.year, v.month, v.day, tzinfo=_dt.timezone.utc)
            return format_timestamp(u), (1, ts_sort_value(u))
        raise CastError
    raise CastError


NULL_KEY = (0,)


def candidate_types(text):
    """Set of types (except string) a non-null text value can be read as."""
    out = set()
    t = text.strip()
    try:
        parse_timestamp(t, require_time=True)
        out.add("timestamp")
    except CastError:
        pass
    try:
        parse_date(t)
        out.add("date")
        out.add("timestamp")  # a date can always be held by a timestamp
    except CastError:
        pass
    low = t.lower()
    if low in TRUE_VALUES or low in FALSE_VALUES:
        out.add("bool")
    if INT_RE.match(t):
        out.add("int")
        out.add("float")
    elif FLOAT_RE.match(t) or FLOAT_SPECIAL_RE.match(t):
        out.add("float")
    return out


_C_BOOL = frozenset(("bool",))
_C_INT = frozenset(("int", "float"))
_C_FLOAT = frozenset(("float",))
_C_DATE = frozenset(("date", "timestamp"))
_C_TS = frozenset(("timestamp",))
_C_STR = frozenset()


def value_candidates(v):
    if isinstance(v, str):
        return candidate_types(v)
    if isinstance(v, bool):
        return _C_BOOL
    if isinstance(v, int):
        return _C_INT
    if isinstance(v, (float, decimal.Decimal)):
        return _C_FLOAT
    if isinstance(v, _dt.datetime):
        return _C_TS
    if isinstance(v, _dt.date):
        return _C_DATE
    return _C_STR


def pick_type(cands):
    if cands is None:
        return None
    for t in TYPE_PRIORITY:
        if t == "string" or t in cands:
            return t
    return "string"


# --------------------------------------------------------------------------
# Nested types, aliases and field paths
# --------------------------------------------------------------------------
#
# A resolved type is either a primitive name (one of VALID_TYPES) or a tuple:
#   ("struct", ((name, type), ...)), ("array", type), ("map", type),
#   ("json",)  -- accept any JSON value.

JSON_T = ("json",)

BUILTIN_ALIASES = {
    "integer": "int", "long": "int", "double": "float", "number": "float",
    "boolean": "bool", "datetime": "timestamp", "timestamptz": "timestamp",
    "text": "string", "varchar": "string",
}
GENERIC_ALIASES = {"list": "array", "array": "array", "map": "map",
                   "struct": "struct"}


def is_primitive(t):
    return isinstance(t, str)


def type_name(t):
    if isinstance(t, str):
        return t
    k = t[0]
    if k == "json":
        return "json"
    if k == "array":
        return "array<%s>" % type_name(t[1])
    if k == "map":
        return "map<string,%s>" % type_name(t[1])
    return "struct<%s>" % ",".join("%s:%s" % (n, type_name(ft))
                                   for n, ft in t[1])


class _TypeParser(object):
    """Recursive-descent parser for textual types like array<map<string,int>>."""

    def __init__(self, text, resolver, stack):
        self.s = text
        self.i = 0
        self.r = resolver
        self.stack = stack

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def ident(self):
        self.ws()
        j = self.i
        while j < len(self.s) and (self.s[j].isalnum() or self.s[j] in "_-$"):
            j += 1
        if j == self.i:
            raise UserError("invalid type %s" % json.dumps(self.s),
                            EXIT_SCHEMA)
        name = self.s[self.i:j]
        self.i = j
        return name

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch):
        if self.peek() != ch:
            raise UserError("invalid type %s" % json.dumps(self.s),
                            EXIT_SCHEMA)
        self.i += 1

    def parse(self):
        name = self.ident()
        if self.peek() != "<":
            return self.r.resolve_name(name, self.stack)
        self.i += 1
        base = self.r.generic_base(name, self.stack)
        if base == "array":
            el = self.parse()
            self.expect(">")
            return ("array", el)
        if base == "map":
            k = self.parse()
            self.expect(",")
            v = self.parse()
            self.expect(">")
            if k != "string":
                raise UserError("map keys must be string, got %s"
                                % type_name(k), EXIT_SCHEMA)
            return ("map", v)
        # struct<name:type,...>
        fields = []
        if self.peek() == ">":
            self.i += 1
            return self.r.make_struct(fields)
        while True:
            fname = self.ident()
            self.expect(":")
            fields.append((fname, self.parse()))
            c = self.peek()
            self.i += 1
            if c == ">":
                break
            if c != ",":
                raise UserError("invalid type %s" % json.dumps(self.s),
                                EXIT_SCHEMA)
        return self.r.make_struct(fields)

    def parse_all(self):
        t = self.parse()
        self.ws()
        if self.i != len(self.s):
            raise UserError("invalid type %s" % json.dumps(self.s),
                            EXIT_SCHEMA)
        return t


class TypeResolver(object):
    def __init__(self, aliases=None):
        self.aliases = aliases or {}

    def _cycle(self, name, stack):
        chain = list(stack[stack.index(name):]) + [name]
        raise UserError("type alias cycle: %s" % " -> ".join(chain),
                        EXIT_USAGE)

    def resolve_name(self, name, stack=()):
        n = name.strip().lower()
        if n in VALID_TYPES:
            return n
        if n in self.aliases:
            if n in stack:
                self._cycle(n, stack)
            return self.resolve(self.aliases[n], stack + (n,))
        if n in BUILTIN_ALIASES:
            return BUILTIN_ALIASES[n]
        if n in ("json", "struct", "object", "any"):
            return JSON_T
        if n in ("array", "list"):
            return ("array", JSON_T)
        if n == "map":
            return ("map", JSON_T)
        raise UserError("unknown type %s" % json.dumps(name), EXIT_SCHEMA)

    def generic_base(self, name, stack=()):
        n = name.strip().lower()
        seen = stack
        while n in self.aliases and n not in VALID_TYPES:
            if n in seen:
                self._cycle(n, seen)
            seen = seen + (n,)
            tgt = self.aliases[n]
            if not isinstance(tgt, str):
                break
            n = tgt.strip().lower()
        if n in GENERIC_ALIASES:
            return GENERIC_ALIASES[n]
        raise UserError("unknown generic type %s" % json.dumps(name),
                        EXIT_SCHEMA)

    def make_struct(self, fields):
        seen = set()
        for n, _ in fields:
            if n in seen:
                raise UserError("duplicate field %s in struct"
                                % json.dumps(n), EXIT_SCHEMA)
            seen.add(n)
        return ("struct", tuple(fields))

    def resolve(self, spec, stack=()):
        if isinstance(spec, str):
            return _TypeParser(spec, self, stack).parse_all()
        if isinstance(spec, dict):
            if len(spec) != 1:
                if "type" in spec:
                    return self.resolve(spec["type"], stack)
                raise UserError("invalid type %s" % json.dumps(spec),
                                EXIT_SCHEMA)
            (kind, body), = spec.items()
            if not isinstance(kind, str):
                raise UserError("invalid type", EXIT_SCHEMA)
            k = kind.strip().lower()
            if k == "type":
                return self.resolve(body, stack)
            if k in ("json", "any", "object"):
                return JSON_T
            base = self.generic_base(k, stack)
            if base == "struct":
                fields = body.get("fields") if isinstance(body, dict) else body
                if not isinstance(fields, list):
                    raise UserError("struct needs a 'fields' list",
                                    EXIT_SCHEMA)
                out = []
                for f in fields:
                    if (not isinstance(f, dict)
                            or not isinstance(f.get("name"), str)):
                        raise UserError("each struct field needs a string "
                                        "'name'", EXIT_SCHEMA)
                    out.append((f["name"],
                                self.resolve(f.get("type", "string"), stack)))
                return self.make_struct(out)
            if base == "array":
                el = body
                if isinstance(body, dict) and len(body) == 1 and (
                        "element" in body or "items" in body
                        or "type" in body):
                    el = list(body.values())[0]
                return ("array", self.resolve(el, stack))
            # map
            if isinstance(body, dict) and ("value" in body or "key" in body):
                kt = self.resolve(body.get("key", "string"), stack)
                vt = body.get("value", "json")
            else:
                kt = "string"
                vt = body
            if kt != "string":
                raise UserError("map keys must be string, got %s"
                                % type_name(kt), EXIT_SCHEMA)
            return ("map", self.resolve(vt, stack))
        raise UserError("invalid type %s" % json.dumps(spec, default=str),
                        EXIT_SCHEMA)


def load_aliases(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise UserError("cannot read type alias file %s: %s" % (path, e),
                        EXIT_USAGE)
    if isinstance(data, dict) and "aliases" in data:
        data = data["aliases"]
    if not isinstance(data, dict):
        raise UserError("type alias file must contain an 'aliases' object",
                        EXIT_USAGE)
    aliases = {}
    for k, v in data.items():
        if not isinstance(v, (str, dict)) or not k.strip():
            raise UserError("invalid alias %s" % json.dumps(k), EXIT_USAGE)
        key = k.strip().lower()
        if key in VALID_TYPES:
            continue  # primitive names cannot be redefined
        aliases[key] = v
    resolver = TypeResolver(aliases)
    # Detect cycles eagerly (deterministic), unknown targets only when used.
    for k in sorted(aliases):
        try:
            resolver.resolve_name(k)
        except UserError as e:
            if e.code == EXIT_USAGE:
                raise
    return resolver


# -- canonical JSON ---------------------------------------------------------

def canonical_json(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def _is_pairs(v):
    return (isinstance(v, list) and all(
        isinstance(e, tuple) and len(e) == 2 and isinstance(e[0], str)
        for e in v) and len(v) > 0)


def normalize_json(v):
    """Normalise an arbitrary (JSON or Arrow-derived) value to JSON data."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, dict):
        return {k: normalize_json(v[k]) for k in sorted(v, key=str)}
    if _is_pairs(v):
        return {k: normalize_json(x) for k, x in sorted(v)}
    if isinstance(v, (list, tuple)):
        return [normalize_json(x) for x in v]
    if hasattr(v, "to_pydatetime"):
        v = v.to_pydatetime()
    if isinstance(v, _dt.datetime):
        return format_timestamp(to_utc(v))
    if isinstance(v, _dt.date):
        return format_date(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def stringify(v):
    """Original/stringified form of a value, for keep-string."""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list, tuple)):
        return canonical_json(normalize_json(v))
    return to_text(v)


class NestedCastError(Exception):
    def __init__(self, value, typ, path):
        Exception.__init__(self)
        self.value = value
        self.typ = typ
        self.path = path


def _leaf_json(text, typ):
    if typ == "int":
        return int(text)
    if typ == "float":
        f = float(text)
        return f if math.isfinite(f) else None
    if typ == "bool":
        return text == "true"
    return text


def cast_nested(v, t, path, policy):
    """Recursively cast v to type t, returning canonical JSON data."""
    if v is None:
        return None
    if t == JSON_T:
        return normalize_json(v)
    if hasattr(v, "to_pydatetime"):
        v = v.to_pydatetime()
    if is_primitive(t):
        if not isinstance(v, (dict, list, tuple)):
            try:
                text, _ = cast_value(v, t)
                return _leaf_json(text, t)
            except (CastError, ValueError, OverflowError):
                pass
        return _nested_fail(v, t, path, policy)
    kind = t[0]
    if kind == "struct":
        if not isinstance(v, dict):
            return _nested_fail(v, t, path, policy)
        out = {}
        for name, ft in t[1]:
            out[name] = cast_nested(v.get(name), ft, path + "." + name,
                                    policy)
        return out
    if kind == "array":
        if not isinstance(v, (list, tuple)):
            return _nested_fail(v, t, path, policy)
        return [cast_nested(x, t[1], "%s.%d" % (path, i), policy)
                for i, x in enumerate(v)]
    # map
    if isinstance(v, list) and all(isinstance(e, tuple) and len(e) == 2
                                   for e in v):
        v = dict((str(k), x) for k, x in v)
    if not isinstance(v, dict):
        return _nested_fail(v, t, path, policy)
    out = {}
    for k in sorted(v, key=str):
        out[str(k)] = cast_nested(v[k], t[1],
                                  "%s[%s]" % (path, canonical_json(str(k))),
                                  policy)
    return out


def _nested_fail(v, t, path, policy):
    if policy == "fail":
        raise NestedCastError(v, t, path)
    if policy == "keep-string":
        return stringify(v)
    return None


# -- field paths ------------------------------------------------------------

def parse_field_path(spec):
    """Split 'a.b.0["k"]' into [('name','a'),('name','b'),('name','0'),
    ('key','k')]."""
    segs = []
    i = 0
    n = len(spec)
    cur = []
    expect_name = True

    def bad():
        raise UserError("invalid field path %s" % json.dumps(spec),
                        EXIT_USAGE)

    while i < n:
        c = spec[i]
        if c == ".":
            if cur:
                segs.append(("name", "".join(cur)))
                cur = []
            elif expect_name:
                bad()
            expect_name = True
            i += 1
        elif c == "[":
            if cur:
                segs.append(("name", "".join(cur)))
                cur = []
            elif expect_name and segs:
                bad()
            elif not segs:
                bad()
            j = i + 1
            if j < n and spec[j] in "\"'":
                q = spec[j]
                k = j + 1
                buf = []
                while k < n and spec[k] != q:
                    if spec[k] == "\\" and k + 1 < n:
                        buf.append(spec[k:k + 2])
                        k += 2
                        continue
                    buf.append(spec[k])
                    k += 1
                if k >= n or k + 1 >= n or spec[k + 1] != "]":
                    bad()
                raw = "".join(buf)
                if q == '"':
                    try:
                        key = json.loads('"' + raw + '"')
                    except ValueError:
                        bad()
                else:
                    key = raw.replace("\\'", "'").replace("\\\\", "\\")
                segs.append(("key", key))
                i = k + 2
            else:
                k = spec.find("]", j)
                if k < 0:
                    bad()
                segs.append(("index", spec[j:k].strip()))
                i = k + 1
            expect_name = False
        else:
            if not expect_name and not cur:
                bad()
            cur.append(c)
            i += 1
    if cur:
        segs.append(("name", "".join(cur)))
    elif expect_name:
        bad()
    if not segs or segs[0][0] != "name":
        bad()
    return segs


class FieldRef(object):
    """A resolved key/partition reference into the (possibly nested) row."""

    def __init__(self, spec, col, steps, leaf):
        self.spec = spec
        self.col = col          # column index
        self.steps = steps      # list of ('field'|'index'|'key'|'any', v)
        self.leaf = leaf        # primitive type or JSON_T (dynamic)


def resolve_field_ref(spec, pos, types, what):
    if spec in pos:
        segs = [("name", spec)]
    else:
        segs = parse_field_path(spec)
        if segs[0][1] not in pos:
            raise UserError("%s column %s not present in schema"
                            % (what, json.dumps(spec)), EXIT_SCHEMA)
    col = pos[segs[0][1]]
    t = types[col]
    steps = []
    for kind, val in segs[1:]:
        if t == JSON_T:
            if kind == "index":
                steps.append(("any", val))
            else:
                steps.append(("any", val) if kind == "name"
                             else ("key", val))
            continue
        if is_primitive(t):
            raise UserError("%s column %s does not resolve to a primitive"
                            % (what, json.dumps(spec)), EXIT_SCHEMA)
        k = t[0]
        if k == "struct":
            fields = dict(t[1])
            if kind == "index" or val not in fields:
                raise UserError("%s column %s not present in schema"
                                % (what, json.dumps(spec)), EXIT_SCHEMA)
            steps.append(("field", val))
            t = fields[val]
        elif k == "array":
            if kind == "key" or not re.match(r"^\d+$", val):
                raise UserError("%s column %s does not resolve to a "
                                "primitive (array index must be a "
                                "non-negative integer)"
                                % (what, json.dumps(spec)), EXIT_SCHEMA)
            steps.append(("index", int(val)))
            t = t[1]
        else:  # map
            steps.append(("key", val))
            t = t[1]
    if t == JSON_T and not steps:
        raise UserError("%s column %s does not resolve to a primitive"
                        % (what, json.dumps(spec)), EXIT_SCHEMA)
    if not is_primitive(t) and t != JSON_T:
        raise UserError("%s column %s does not resolve to a primitive"
                        % (what, json.dumps(spec)), EXIT_SCHEMA)
    return FieldRef(spec, col, steps, t)


def walk_steps(v, steps):
    for kind, s in steps:
        if v is None:
            return None
        if kind == "field" or kind == "key":
            v = v.get(s) if isinstance(v, dict) else None
        elif kind == "index":
            v = v[s] if isinstance(v, list) and s < len(v) else None
        else:  # dynamic (json)
            if isinstance(v, dict):
                v = v.get(s)
            elif isinstance(v, list) and re.match(r"^\d+$", s):
                i = int(s)
                v = v[i] if i < len(v) else None
            else:
                return None
    return v


def json_leaf_key(v, spec):
    if isinstance(v, (dict, list)):
        raise UserError("key column %s does not resolve to a primitive"
                        % json.dumps(spec), EXIT_SCHEMA)
    if isinstance(v, bool):
        return ("true" if v else "false"), (1, 0, v)
    if isinstance(v, (int, float)):
        return to_text(v), (1, 1, v)
    return v, (1, 2, v)


# --------------------------------------------------------------------------
# Input detection
# --------------------------------------------------------------------------

EXT_FORMATS = {".csv": "csv", ".tsv": "tsv", ".jsonl": "jsonl",
               ".ndjson": "jsonl", ".parquet": "parquet"}
GZIP_MAGIC = b"\x1f\x8b"
PARQUET_MAGIC = b"PAR1"


def read_magic(path, n=4):
    with open(path, "rb") as f:
        return f.read(n)


def detect_input(path, fmt_arg, comp_arg):
    """Return (format, gzipped) for path."""
    lower = path.lower()
    has_gz_ext = lower.endswith(".gz")
    base = lower[:-3] if has_gz_ext else lower
    magic = read_magic(path)
    is_gz_data = magic[:2] == GZIP_MAGIC

    if comp_arg == "auto":
        gz = has_gz_ext
    else:
        gz = comp_arg == "gzip"
    if gz and not is_gz_data:
        if magic == b"":
            gz = False  # empty file: nothing to decompress
        else:
            raise UserError("%s: expected gzip-compressed data" % path,
                            EXIT_PARSE)
    if not gz and is_gz_data:
        raise UserError("%s: data is gzip-compressed but compression is "
                        "'%s'" % (path, comp_arg), EXIT_PARSE)

    if fmt_arg != "auto":
        return fmt_arg, gz

    ext = os.path.splitext(base)[1]
    fmt = EXT_FORMATS.get(ext)
    if fmt is not None:
        return fmt, gz
    # Ambiguous extension: sniff Parquet magic bytes.
    if gz:
        try:
            with gzip.open(path, "rb") as f:
                inner = f.read(4)
        except (OSError, EOFError):
            inner = b""
    else:
        inner = magic
    if inner == PARQUET_MAGIC:
        return "parquet", gz
    raise UserError("%s: cannot detect input format" % path, EXIT_USAGE)


# --------------------------------------------------------------------------
# Readers. Each yields dicts mapping column name -> value; null/missing
# values are omitted. CSV/TSV values are raw strings, JSONL/Parquet typed.
# --------------------------------------------------------------------------

def open_text(src):
    if src.gz:
        return gzip.open(src.path, "rt", encoding="utf-8-sig", newline="")
    return open(src.path, "r", encoding="utf-8-sig", newline="")


class Source(object):
    def __init__(self, path, fmt, gz, ctx):
        self.path = path
        self.fmt = fmt
        self.gz = gz
        self.ctx = ctx
        self._local = None  # decompressed parquet copy

    # -- CSV ---------------------------------------------------------------
    def _csv_rows(self):
        with open_text(self) as f:
            reader = csv.reader(f, **self.ctx.reader_kwargs)
            header = None
            for row in reader:
                if header is None:
                    header = row
                    yield reader.line_num, header
                    continue
                if not row:
                    continue
                yield reader.line_num, row

    # -- TSV ---------------------------------------------------------------
    def _tsv_rows(self):
        with open_text(self) as f:
            header = None
            lineno = 0
            while True:
                line = f.readline()
                if not line:
                    break
                lineno += 1
                if line.endswith("\n"):
                    line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
                if header is None:
                    header = line.split("\t")
                    yield lineno, header
                    continue
                if line == "":
                    continue
                row = line.split("\t")
                if len(row) > len(header):
                    raise UserError(
                        "%s: line %d: literal tab inside field (expected %d "
                        "fields, got %d)" % (self.path, lineno, len(header),
                                             len(row)), EXIT_PARSE)
                yield lineno, row

    def _delimited_records(self):
        it = self._csv_rows() if self.fmt == "csv" else self._tsv_rows()
        first = next(it, None)
        if first is None:
            return
        header = first[1]
        idx = []
        seen = set()
        for i, name in enumerate(header):
            if name not in seen:
                seen.add(name)
                idx.append((i, name))
        null_lit = self.ctx.null_literal
        for lineno, row in it:
            n = len(row)
            rec = {}
            for i, name in idx:
                if i < n:
                    cell = row[i]
                    if cell != "" and cell != null_lit:
                        rec[name] = cell
            yield lineno, rec

    def header(self):
        """Column names known up-front (CSV/TSV/Parquet), else None."""
        if self.fmt in ("csv", "tsv"):
            it = self._csv_rows() if self.fmt == "csv" else self._tsv_rows()
            try:
                first = next(it, None)
                return first[1] if first else []
            finally:
                it.close()
        return None

    # -- JSONL -------------------------------------------------------------
    def _jsonl_records(self):
        with open_text(self) as f:
            lineno = 0
            for line in f:
                lineno += 1
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line, parse_float=_parse_json_float)
                except ValueError as e:
                    raise UserError("%s: line %d: invalid JSON: %s"
                                    % (self.path, lineno, e), EXIT_PARSE)
                if not isinstance(obj, dict):
                    raise UserError("%s: line %d: expected a JSON object"
                                    % (self.path, lineno), EXIT_PARSE)
                rec = {}
                allow = self.ctx.allow_nested
                for k, v in obj.items():
                    if v is None:
                        continue
                    if isinstance(v, (dict, list)):
                        if not allow:
                            raise UserError(
                                "nested structure requires provided --schema",
                                EXIT_NESTED)
                    elif type(v) is int and not INT64_MIN <= v <= INT64_MAX:
                        v = float(v)
                    rec[k] = v
                yield lineno, rec

    # -- Parquet -----------------------------------------------------------
    def _parquet_path(self):
        if not self.gz:
            return self.path
        if self._local is None:
            fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=self.ctx.tmpdir)
            with os.fdopen(fd, "wb") as out, gzip.open(self.path, "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
            self._local = tmp
        return self._local

    def parquet_file(self):
        pq = import_parquet()
        try:
            return pq.ParquetFile(self._parquet_path())
        except Exception as e:  # pyarrow raises various error types
            raise UserError("%s: cannot read parquet: %s" % (self.path, e),
                            EXIT_PARSE)

    def parquet_schema(self):
        """Return list of (name, candidate set) and validate flatness."""
        pa = import_pyarrow()
        pf = self.parquet_file()
        schema = pf.schema_arrow
        out = []
        for field in schema:
            out.append((field.name, arrow_candidates(pa, field.type,
                                                     self.path, field.name)))
        return out

    def check_parquet(self):
        """Validate the parquet schema against the provided/inferred one."""
        pa = import_pyarrow()
        pf = self.parquet_file()
        declared = self.ctx.declared
        for field in pf.schema_arrow:
            if declared is None:
                arrow_candidates(pa, field.type, self.path, field.name)
                continue
            if field.name not in declared:
                continue
            check_arrow_compat(pa, field.type, declared[field.name],
                               self.path, field.name)

    def _parquet_records(self):
        pa = import_pyarrow()
        pf = self.parquet_file()
        self.check_parquet()
        meta = pf.metadata
        row_bytes = 64
        if meta is not None and meta.num_rows > 0:
            total = 0
            for i in range(meta.num_row_groups):
                total += meta.row_group(i).total_byte_size
            row_bytes = max(16, total // max(1, meta.num_rows))
        target = self.ctx.parquet_batch_bytes
        batch_rows = int(max(1, min(65536, target // row_bytes)))
        rowno = 0
        try:
            for rg in range(pf.num_row_groups):
                for batch in pf.iter_batches(batch_size=batch_rows,
                                             row_groups=[rg]):
                    cols = []
                    for i, field in enumerate(batch.schema):
                        arr = batch.column(i)
                        arr = normalise_arrow(pa, arr)
                        cols.append((field.name, arr.to_pylist()))
                    n = batch.num_rows
                    for r in range(n):
                        rec = {}
                        for name, vals in cols:
                            v = vals[r]
                            if v is not None:
                                rec[name] = v
                        rowno += 1
                        yield rowno, rec
                    del cols
        except UserError:
            raise
        except (OSError, ValueError) as e:
            raise UserError("%s: cannot read parquet: %s" % (self.path, e),
                            EXIT_PARSE)
        except Exception as e:
            if type(e).__module__.startswith("pyarrow"):
                raise UserError("%s: cannot read parquet: %s" % (self.path, e),
                                EXIT_PARSE)
            raise

    def records(self):
        if self.fmt in ("csv", "tsv"):
            return self._delimited_records()
        if self.fmt == "jsonl":
            return self._jsonl_records()
        return self._parquet_records()


def _parse_json_int(s):
    v = int(s)
    if INT64_MIN <= v <= INT64_MAX:
        return v
    return float(s)


def _parse_json_float(s):
    return float(s)


_pa = None
_pq = None


def import_pyarrow():
    global _pa
    if _pa is None:
        try:
            import pyarrow
        except ImportError:
            raise UserError("pyarrow is required to read parquet files",
                            EXIT_GENERIC)
        _pa = pyarrow
    return _pa


def import_parquet():
    global _pq
    if _pq is None:
        import_pyarrow()
        import pyarrow.parquet
        _pq = pyarrow.parquet
    return _pq


def arrow_is_list(types, t):
    return (types.is_list(t) or types.is_large_list(t)
            or types.is_fixed_size_list(t)
            or (hasattr(types, "is_list_view") and types.is_list_view(t))
            or (hasattr(types, "is_large_list_view")
                and types.is_large_list_view(t)))


def arrow_is_nested(types, t):
    return (types.is_struct(t) or arrow_is_list(types, t) or types.is_map(t)
            or types.is_union(t))


def check_arrow_compat(pa, at, t, path, name):
    """Raise EXIT_NESTED when nested arrow type at mismatches schema type t."""
    types = pa.types
    if types.is_dictionary(at):
        at = at.value_type
    if not arrow_is_nested(types, at) or t == JSON_T:
        return
    ok = False
    if is_primitive(t) or types.is_union(at):
        ok = False
    elif t[0] == "struct" and types.is_struct(at):
        ok = True
        fields = dict(t[1])
        for i in range(at.num_fields):
            f = at.field(i)
            if f.name in fields:
                check_arrow_compat(pa, f.type, fields[f.name], path,
                                   name + "." + f.name)
    elif t[0] == "array" and arrow_is_list(types, at):
        ok = True
        check_arrow_compat(pa, at.value_type, t[1], path, name + ".0")
    elif t[0] == "map" and types.is_map(at):
        ok = True
        check_arrow_compat(pa, at.item_type, t[1], path, name + "[]")
    if not ok:
        raise UserError("nested structure in field %s does not match "
                        "declared type %s (file=%s)"
                        % (json.dumps(name), type_name(t), path), EXIT_NESTED)


def arrow_candidates(pa, t, path, name):
    types = pa.types
    if types.is_dictionary(t):
        t = t.value_type
    if arrow_is_nested(types, t):
        raise UserError("nested structure requires provided --schema",
                        EXIT_NESTED)
    if types.is_null(t):
        return None
    if types.is_boolean(t):
        return _C_BOOL
    if types.is_integer(t):
        return _C_INT
    if types.is_floating(t) or types.is_decimal(t):
        return _C_FLOAT
    if types.is_timestamp(t):
        return _C_TS
    if types.is_date(t):
        return _C_DATE
    return _C_STR


def normalise_arrow(pa, arr):
    t = arr.type
    if pa.types.is_dictionary(t):
        arr = arr.cast(t.value_type)
        t = arr.type
    if pa.types.is_timestamp(t) and t.unit == "ns":
        arr = arr.cast(pa.timestamp("us", tz=t.tz), safe=False)
    elif pa.types.is_date64(t):
        arr = arr.cast(pa.date32())
    elif pa.types.is_float16(t):
        arr = arr.cast(pa.float32())
    return arr


class Context(object):
    pass


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def load_schema(path, resolver):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise UserError("cannot read schema %s: %s" % (path, e), EXIT_SCHEMA)
    cols = data.get("columns") if isinstance(data, dict) else data
    if not isinstance(cols, list) or not cols:
        raise UserError("schema must contain a non-empty 'columns' list",
                        EXIT_SCHEMA)
    out = []
    seen = set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise UserError("each schema column needs a string 'name'",
                            EXIT_SCHEMA)
        name = c["name"]
        typ = c.get("type", "string")
        try:
            rtyp = resolver.resolve(typ)
        except UserError as e:
            if e.code == EXIT_SCHEMA:
                raise UserError("invalid type %s for column %s: %s"
                                % (json.dumps(typ, default=str),
                                   json.dumps(name), e), EXIT_SCHEMA)
            raise
        if name in seen:
            raise UserError("duplicate column %s in schema"
                            % json.dumps(name), EXIT_SCHEMA)
        seen.add(name)
        out.append((name, rtyp))
    return out


TIER = {"parquet": 0, "jsonl": 1, "csv": 2, "tsv": 2}


def scan_source(src):
    """Return dict column -> candidate set (None when no values seen)."""
    if src.fmt == "parquet":
        return dict(src.parquet_schema())
    cands = {}
    if src.fmt in ("csv", "tsv"):
        for name in src.header():
            cands[name] = None
    for _, rec in src.records():
        for name, v in rec.items():
            cur = cands.get(name)
            if cur is not None and not cur:
                continue  # already degraded to string
            c = value_candidates(v)
            cands[name] = c if cur is None else (cur & c)
    return cands


def combine(cand_list, mode):
    if not cand_list:
        return "string"
    if mode == "loose":
        acc = None
        for c in cand_list:
            acc = c if acc is None else (acc & c)
        return pick_type(acc)
    picked = set(pick_type(c) for c in cand_list)
    return picked.pop() if len(picked) == 1 else "string"


def infer_schema(sources, mode, strategy):
    per_file = [(TIER[s.fmt], scan_source(s)) for s in sources]
    all_cols = set()
    for _, cands in per_file:
        all_cols.update(cands)
    schema = []
    for name in sorted(all_cols):
        observed = [(tier, cands[name]) for tier, cands in per_file
                    if cands.get(name) is not None]
        if not observed:
            typ = "string"
        elif strategy == "authoritative":
            best = min(t for t, _ in observed)
            typ = combine([c for t, c in observed if t == best], mode)
        elif strategy == "consensus":
            n = len(observed)
            typ = "string"
            for t in TYPE_PRIORITY:
                if t == "string":
                    break
                support = sum(1 for _, c in observed if t in c)
                if support * 2 > n:
                    typ = t
                    break
        else:  # union
            typ = combine([c for _, c in observed], "loose")
        schema.append((name, typ))
    return schema


# --------------------------------------------------------------------------
# External sort
# --------------------------------------------------------------------------

class DescKey(object):
    __slots__ = ("k", "seq")

    def __init__(self, k, seq):
        self.k = k
        self.seq = seq

    def __lt__(self, other):
        if self.k != other.k:
            return self.k > other.k
        return self.seq < other.seq


def asc_key(rec):
    return (rec[0], rec[1])


def desc_key(rec):
    return DescKey(rec[0], rec[1])


BATCH = 256
MAX_FANIN = 32


def write_run(records, path):
    with open(path, "wb") as f:
        for i in range(0, len(records), BATCH):
            pickle.dump(records[i:i + BATCH], f,
                        protocol=pickle.HIGHEST_PROTOCOL)


def read_run(path):
    with open(path, "rb") as f:
        while True:
            try:
                batch = pickle.load(f)
            except EOFError:
                return
            for rec in batch:
                yield rec


def estimate_size(out, key):
    s = 200 + 80 * len(out) + 120 * len(key)
    for c in out:
        if c is not None:
            s += len(c)
    return s


# --------------------------------------------------------------------------
# Partitioned output
# --------------------------------------------------------------------------

def encode_segment(text):
    """Percent-encode UTF-8 bytes outside [A-Za-z0-9._-]."""
    return urllib.parse.quote(text, safe="").replace("~", "%7E")


def partition_path(specs, values):
    segs = []
    for spec, v in zip(specs, values):
        val = "_null" if v is None else encode_segment(v)
        segs.append(encode_segment(spec) + "=" + val)
    return tuple(segs)


def split_paths(text):
    """Split a comma-separated list of field paths, honouring [..] quotes."""
    out = []
    cur = []
    depth = 0
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif depth and c in "\"'":
            quote = c
            cur.append(c)
        elif c == "[":
            depth += 1
            cur.append(c)
        elif c == "]":
            depth = max(0, depth - 1)
            cur.append(c)
        elif c == "," and not depth:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur).strip())
    return out


class RowEncoder(object):
    def __init__(self, wkw):
        import io
        self.buf = io.StringIO()
        self.writer = csv.writer(self.buf, **wkw)

    def encode(self, row):
        self.buf.seek(0)
        self.buf.truncate()
        self.writer.writerow(row)
        return self.buf.getvalue().encode("utf-8")


class Sharder(object):
    """Writes part-NNNNN.csv files into a directory, honouring limits."""

    def __init__(self, directory, header, max_rows, max_bytes):
        self.dir = directory
        self.header = header
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.index = 0
        self.f = None
        self.rows = 0
        self.size = 0

    def open_new(self):
        if self.f is not None:
            self.f.close()
        path = os.path.join(self.dir, "part-%05d.csv" % self.index)
        self.index += 1
        self.f = open(path, "wb")
        self.f.write(self.header)
        self.rows = 0
        self.size = len(self.header)

    def write(self, data):
        if self.f is None:
            self.open_new()
        elif self.rows > 0 and (
                (self.max_rows is not None and self.rows >= self.max_rows)
                or (self.max_bytes is not None
                    and self.size + len(data) > self.max_bytes)):
            self.open_new()
        self.f.write(data)
        self.rows += 1
        self.size += len(data)

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None


def single_char(p, name, value):
    if value is not None and len(value) != 1:
        p.error("%s must be a single character" % name)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Merge and sort CSV/TSV/JSONL/Parquet files into CSV.")
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--partition-by")
    p.add_argument("--max-rows-per-file", type=int)
    p.add_argument("--max-bytes-per-file", type=int)
    p.add_argument("--desc", action="store_true")
    p.add_argument("--schema")
    p.add_argument("--type-alias-file")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--schema-strategy",
                   choices=("authoritative", "consensus", "union"),
                   default="authoritative")
    p.add_argument("--on-type-error",
                   choices=("coerce-null", "fail", "keep-string"),
                   default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=256)
    p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"')
    p.add_argument("--csv-escapechar", default=None)
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("--input-format",
                   choices=("auto", "csv", "tsv", "jsonl", "parquet"),
                   default="auto")
    p.add_argument("--compression", choices=("auto", "none", "gzip"),
                   default="auto")
    p.add_argument("--parquet-row-group-bytes", type=int,
                   default=64 * 1024 * 1024)
    p.add_argument("inputs", nargs="+")
    args = p.parse_args(argv)

    single_char(p, "--csv-quotechar", args.csv_quotechar)
    single_char(p, "--csv-escapechar", args.csv_escapechar)
    if args.memory_limit_mb <= 0:
        p.error("--memory-limit-mb must be positive")
    if args.parquet_row_group_bytes <= 0:
        p.error("--parquet-row-group-bytes must be positive")
    if args.max_rows_per_file is not None and args.max_rows_per_file <= 0:
        p.error("--max-rows-per-file must be positive")
    if args.max_bytes_per_file is not None and args.max_bytes_per_file <= 0:
        p.error("--max-bytes-per-file must be positive")
    part_cols = []
    if args.partition_by is not None:
        part_cols = split_paths(args.partition_by)
        if any(c == "" for c in part_cols):
            p.error("invalid --partition-by specification")
        if len(set(part_cols)) != len(part_cols):
            p.error("duplicate column in --partition-by")
    partitioned = (args.partition_by is not None
                   or args.max_rows_per_file is not None
                   or args.max_bytes_per_file is not None)
    if partitioned and args.output == "-":
        p.error("--output must be a directory path when partitioning")

    def _term(signum, frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _term)
        except (ValueError, OSError, AttributeError):
            pass

    tmpdir = None
    out_tmp = None
    out_tmp_dir = None
    try:
        if partitioned:
            out_abs = os.path.abspath(args.output)
            if os.path.exists(out_abs) and not os.path.isdir(out_abs):
                raise UserError("output path exists and is not a directory: "
                                "%s" % args.output, EXIT_USAGE)
        keys = split_paths(args.key)
        if not keys or any(k == "" for k in keys):
            raise UserError("invalid --key specification", EXIT_USAGE)
        for path in args.inputs:
            if not os.path.isfile(path):
                raise UserError("input file not found: %s" % path)
        if args.temp_dir is not None and not os.path.isdir(args.temp_dir):
            raise UserError("temp dir does not exist: %s" % args.temp_dir,
                            EXIT_USAGE)

        tmpdir = tempfile.mkdtemp(prefix="merge_files_", dir=args.temp_dir)
        mem_bytes = args.memory_limit_mb * (1 << 20)

        ctx = Context()
        ctx.reader_kwargs = {"delimiter": ",",
                             "quotechar": args.csv_quotechar,
                             "doublequote": True, "strict": False}
        if args.csv_escapechar is not None:
            ctx.reader_kwargs["escapechar"] = args.csv_escapechar
        ctx.null_literal = args.csv_null_literal
        ctx.tmpdir = tmpdir
        ctx.parquet_batch_bytes = max(
            1 << 16, min(args.parquet_row_group_bytes, mem_bytes // 16))

        sources = []
        for path in args.inputs:
            fmt, gz = detect_input(path, args.input_format, args.compression)
            sources.append(Source(path, fmt, gz, ctx))

        if args.type_alias_file:
            resolver = load_aliases(args.type_alias_file)
        else:
            resolver = TypeResolver()
        ctx.allow_nested = bool(args.schema)
        ctx.declared = None
        if args.schema:
            schema = load_schema(args.schema, resolver)
            ctx.declared = dict(schema)
        else:
            schema = infer_schema(sources, args.infer, args.schema_strategy)

        names = [n for n, _ in schema]
        types = [t for _, t in schema]
        pos = {n: i for i, n in enumerate(names)}
        key_refs = [resolve_field_ref(k, pos, types, "key") for k in keys]
        part_refs = [resolve_field_ref(c, pos, types, "partition")
                     for c in part_cols]
        if args.schema:
            # Enforce structural compatibility of parquet inputs up-front.
            for s in sources:
                if s.fmt == "parquet":
                    s.check_parquet()
        nested_cols = [not is_primitive(t) for t in types]

        budget = max(1 << 20, mem_bytes // 4)
        runs = []
        buf = []
        used = 0
        seq = 0
        on_err = args.on_type_error
        keyfn = desc_key if args.desc else asc_key
        ncols = len(names)

        def flush():
            buf.sort(key=keyfn)
            path = os.path.join(tmpdir, "run%06d.bin" % len(runs))
            write_run(buf, path)
            runs.append(path)

        def ref_value(ref, out, sk, data):
            """Return (text, sort_key) for a key/partition reference."""
            if not ref.steps:
                return out[ref.col], sk[ref.col]
            leaf = walk_steps(data.get(ref.col), ref.steps)
            if leaf is None:
                return None, None
            if ref.leaf == JSON_T:
                return json_leaf_key(leaf, ref.spec)
            try:
                return cast_value(leaf, ref.leaf)
            except CastError:
                t = stringify(leaf)
                return t, (2, t)

        delimited = ("csv", "tsv")
        for src in sources:
            is_delim = src.fmt in delimited
            for lineno, rec in src.records():
                out = [None] * ncols
                sk = [None] * ncols
                data = {}
                for name, v in rec.items():
                    oi = pos.get(name)
                    if oi is None:
                        continue
                    typ = types[oi]
                    if nested_cols[oi]:
                        if isinstance(v, str) and (is_delim
                                                   or src.fmt == "parquet"):
                            try:
                                v = json.loads(v, parse_float=_parse_json_float)
                            except ValueError:
                                if on_err == "fail":
                                    raise UserError(
                                        "invalid JSON %s for type %s in field "
                                        "%s (file=%s line=%d)"
                                        % (json.dumps(v, ensure_ascii=False),
                                           type_name(typ), json.dumps(name),
                                           src.path, lineno), EXIT_PARSE)
                                if on_err == "keep-string":
                                    out[oi] = v
                                continue
                        try:
                            val = cast_nested(v, typ, name, on_err)
                        except NestedCastError as e:
                            raise UserError(
                                'cannot cast "%s" to %s in field "%s" '
                                "(file=%s line=%d)"
                                % (stringify(e.value), type_name(e.typ),
                                   e.path, src.path, lineno), EXIT_CAST)
                        if val is None:
                            continue
                        data[oi] = val
                        if isinstance(val, str) and typ != JSON_T:
                            out[oi] = val  # keep-string of whole value
                        else:
                            out[oi] = canonical_json(val)
                        continue
                    try:
                        out[oi], sk[oi] = cast_value(v, typ)
                    except CastError:
                        if on_err == "fail":
                            raise UserError(
                                'cannot cast "%s" to %s in field "%s" '
                                "(file=%s line=%d)"
                                % (stringify(v), typ, name, src.path,
                                   lineno), EXIT_CAST)
                        if on_err == "keep-string":
                            t = stringify(v)
                            out[oi] = t
                            sk[oi] = (2, t)
                key = []
                for ref in key_refs:
                    k = ref_value(ref, out, sk, data)[1]
                    key.append(NULL_KEY if k is None else k)
                key = tuple(key)
                if part_refs:
                    # Group rows by partition directory first, so each
                    # partition is a contiguous, key-sorted run.
                    key = (partition_path(
                        part_cols,
                        [ref_value(r, out, sk, data)[0] for r in part_refs]),
                        key)
                buf.append((key, seq, out))
                seq += 1
                used += estimate_size(out, key)
                if used >= budget:
                    flush()
                    buf = []
                    used = 0
            if src._local is not None:
                try:
                    os.remove(src._local)
                except OSError:
                    pass
                src._local = None

        # Multi-pass merge to bound open files.
        while len(runs) > MAX_FANIN:
            new_runs = []
            for i in range(0, len(runs), MAX_FANIN):
                group = runs[i:i + MAX_FANIN]
                if len(group) == 1:
                    new_runs.append(group[0])
                    continue
                dest = os.path.join(tmpdir, "merge%06d_%06d.bin" % (
                    len(runs), i))
                with open(dest, "wb") as f:
                    batch = []
                    for rec in heapq.merge(*[read_run(g) for g in group],
                                           key=keyfn):
                        batch.append(rec)
                        if len(batch) >= BATCH:
                            pickle.dump(batch, f,
                                        protocol=pickle.HIGHEST_PROTOCOL)
                            batch = []
                    if batch:
                        pickle.dump(batch, f, protocol=pickle.HIGHEST_PROTOCOL)
                for g in group:
                    os.remove(g)
                new_runs.append(dest)
            runs = new_runs

        buf.sort(key=keyfn)
        streams = [read_run(r) for r in runs]
        if buf:
            streams.append(iter(buf))
        if len(streams) == 1:
            stream = streams[0]
        else:
            stream = heapq.merge(*streams, key=keyfn)

        wkw = {"delimiter": ",", "quotechar": args.csv_quotechar,
               "doublequote": True, "lineterminator": "\n",
               "quoting": csv.QUOTE_MINIMAL}
        if args.csv_escapechar is not None:
            wkw["escapechar"] = args.csv_escapechar
        null_lit = args.csv_null_literal

        if not partitioned:
            if args.output == "-":
                fout = open(sys.stdout.fileno(), "w", encoding="utf-8",
                            newline="", closefd=False)
            else:
                out_dir = os.path.dirname(os.path.abspath(args.output))
                fd, out_tmp = tempfile.mkstemp(prefix=".merge_out_",
                                               dir=out_dir)
                fout = os.fdopen(fd, "w", encoding="utf-8", newline="")
            with fout:
                writer = csv.writer(fout, **wkw)
                writer.writerow(names)
                for _, _, out in stream:
                    writer.writerow([null_lit if v is None else v
                                     for v in out])
            if out_tmp is not None:
                os.replace(out_tmp, args.output)
                out_tmp = None
            return 0

        parent = os.path.dirname(out_abs)
        os.makedirs(parent, exist_ok=True)
        out_tmp_dir = tempfile.mkdtemp(
            prefix="." + os.path.basename(out_abs) + ".tmp-", dir=parent)
        enc = RowEncoder(wkw)
        header_bytes = enc.encode(names)
        sharder = None
        cur_part = None
        for key, _, out in stream:
            if part_refs:
                ppath = key[0]
                if sharder is None or ppath != cur_part:
                    if sharder is not None:
                        sharder.close()
                    cur_part = ppath
                    d = os.path.join(out_tmp_dir, *ppath)
                    os.makedirs(d, exist_ok=True)
                    sharder = Sharder(d, header_bytes, args.max_rows_per_file,
                                      args.max_bytes_per_file)
            elif sharder is None:
                sharder = Sharder(out_tmp_dir, header_bytes,
                                  args.max_rows_per_file,
                                  args.max_bytes_per_file)
            sharder.write(enc.encode([null_lit if v is None else v
                                      for v in out]))
        if sharder is None and not part_refs:
            # No rows: still emit a single header-only part file.
            sharder = Sharder(out_tmp_dir, header_bytes, None, None)
            sharder.open_new()
        if sharder is not None:
            sharder.close()
        sharder = None

        # Atomically move the finished tree into place.
        if os.path.isdir(out_abs):
            backup = tempfile.mkdtemp(
                prefix="." + os.path.basename(out_abs) + ".old-", dir=parent)
            os.rmdir(backup)
            os.rename(out_abs, backup)
            try:
                os.rename(out_tmp_dir, out_abs)
            except OSError:
                os.rename(backup, out_abs)
                raise
            out_tmp_dir = None
            shutil.rmtree(backup, ignore_errors=True)
        else:
            os.rename(out_tmp_dir, out_abs)
            out_tmp_dir = None
        return 0
    except UserError as e:
        sys.stderr.write("ERR %d %s\n" % (e.code, e))
        return e.code
    except (UnicodeDecodeError, csv.Error, EOFError, gzip.BadGzipFile,
            zlib.error) as e:
        sys.stderr.write("ERR %d malformed input: %s\n" % (EXIT_PARSE, e))
        return EXIT_PARSE
    except OSError as e:
        sys.stderr.write("ERR %d %s\n" % (EXIT_GENERIC, e))
        return EXIT_GENERIC
    except KeyboardInterrupt:
        return 130
    finally:
        if out_tmp_dir is not None:
            shutil.rmtree(out_tmp_dir, ignore_errors=True)
        if out_tmp is not None:
            try:
                os.remove(out_tmp)
            except OSError:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
