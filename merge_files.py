#!/usr/bin/env python3
"""Multi-format merger and sorter.

Ingests CSV, TSV, JSON Lines and Parquet files (optionally gzip-compressed),
reconciles their schemas (explicit or inferred), casts every value to the
resolved column type and writes a single CSV sorted globally by a composite
key, using an external merge sort bounded by --memory-limit-mb.

Exit codes:
  0  success
  1  generic / I/O error (e.g. missing input file)
  2  invalid usage (bad arguments, undetectable input format)
  3  schema error (invalid schema file, key column not in schema)
  4  cast failure with --on-type-error=fail
  5  malformed input (CSV/TSV/JSONL parse error, compression mismatch)
  6  unsupported nested structure in JSONL/Parquet input
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
    return str(v)


def cast_value(v, typ):
    """Cast a raw string or typed value to typ.

    Returns (output_text, sort_key) or raises CastError.
    """
    if isinstance(v, str):
        return cast_text(v, typ)
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
                    yield header
                    continue
                if not row:
                    continue
                yield row

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
                    yield header
                    continue
                if line == "":
                    continue
                row = line.split("\t")
                if len(row) > len(header):
                    raise UserError(
                        "%s: line %d: literal tab inside field (expected %d "
                        "fields, got %d)" % (self.path, lineno, len(header),
                                             len(row)), EXIT_PARSE)
                yield row

    def _delimited_records(self):
        it = self._csv_rows() if self.fmt == "csv" else self._tsv_rows()
        header = next(it, None)
        if header is None:
            return
        idx = []
        seen = set()
        for i, name in enumerate(header):
            if name not in seen:
                seen.add(name)
                idx.append((i, name))
        null_lit = self.ctx.null_literal
        for row in it:
            n = len(row)
            rec = {}
            for i, name in idx:
                if i < n:
                    cell = row[i]
                    if cell != "" and cell != null_lit:
                        rec[name] = cell
            yield rec

    def header(self):
        """Column names known up-front (CSV/TSV/Parquet), else None."""
        if self.fmt in ("csv", "tsv"):
            it = self._csv_rows() if self.fmt == "csv" else self._tsv_rows()
            try:
                return next(it, None) or []
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
                    obj = json.loads(line, parse_float=_parse_json_float,
                                     parse_int=_parse_json_int)
                except ValueError as e:
                    raise UserError("%s: line %d: invalid JSON: %s"
                                    % (self.path, lineno, e), EXIT_PARSE)
                if not isinstance(obj, dict):
                    raise UserError("%s: line %d: expected a JSON object"
                                    % (self.path, lineno), EXIT_PARSE)
                rec = {}
                for k, v in obj.items():
                    if v is None:
                        continue
                    if isinstance(v, (dict, list)):
                        raise UserError(
                            "%s: line %d: nested value in field %r is not "
                            "supported" % (self.path, lineno, k), EXIT_NESTED)
                    rec[k] = v
                yield rec

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

    def _parquet_records(self):
        pa = import_pyarrow()
        pf = self.parquet_file()
        schema = pf.schema_arrow
        for field in schema:
            arrow_candidates(pa, field.type, self.path, field.name)
        meta = pf.metadata
        row_bytes = 64
        if meta is not None and meta.num_rows > 0:
            total = 0
            for i in range(meta.num_row_groups):
                total += meta.row_group(i).total_byte_size
            row_bytes = max(16, total // max(1, meta.num_rows))
        target = self.ctx.parquet_batch_bytes
        batch_rows = int(max(1, min(65536, target // row_bytes)))
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
                        yield rec
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


def arrow_candidates(pa, t, path, name):
    types = pa.types
    if types.is_dictionary(t):
        t = t.value_type
    if (types.is_struct(t) or types.is_list(t) or types.is_large_list(t)
            or types.is_fixed_size_list(t) or types.is_map(t)
            or types.is_union(t)
            or (hasattr(types, "is_list_view") and types.is_list_view(t))
            or (hasattr(types, "is_large_list_view")
                and types.is_large_list_view(t))):
        raise UserError("%s: nested field %r is not supported" % (path, name),
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

def load_schema(path):
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
        if not isinstance(typ, str) or typ.lower() not in VALID_TYPES:
            raise UserError("invalid type %r for column %r" % (typ, name),
                            EXIT_SCHEMA)
        if name in seen:
            raise UserError("duplicate column %r in schema" % name,
                            EXIT_SCHEMA)
        seen.add(name)
        out.append((name, typ.lower()))
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
    for rec in src.records():
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


def single_char(p, name, value):
    if value is not None and len(value) != 1:
        p.error("%s must be a single character" % name)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Merge and sort CSV/TSV/JSONL/Parquet files into CSV.")
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true")
    p.add_argument("--schema")
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

    def _term(signum, frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _term)
        except (ValueError, OSError, AttributeError):
            pass

    tmpdir = None
    out_tmp = None
    try:
        keys = [k.strip() for k in args.key.split(",")]
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

        if args.schema:
            schema = load_schema(args.schema)
            # Still enforce flatness of parquet inputs up-front.
            for s in sources:
                if s.fmt == "parquet":
                    s.parquet_schema()
        else:
            schema = infer_schema(sources, args.infer, args.schema_strategy)

        names = [n for n, _ in schema]
        types = [t for _, t in schema]
        pos = {n: i for i, n in enumerate(names)}
        for k in keys:
            if k not in pos:
                raise UserError("key column %r not present in schema" % k,
                                EXIT_SCHEMA)
        key_idx = [pos[k] for k in keys]

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

        for src in sources:
            rowno = 0
            for rec in src.records():
                rowno += 1
                out = [None] * ncols
                sk = [None] * ncols
                for name, v in rec.items():
                    oi = pos.get(name)
                    if oi is None:
                        continue
                    try:
                        out[oi], sk[oi] = cast_value(v, types[oi])
                    except CastError:
                        if on_err == "fail":
                            raise UserError(
                                "%s: row %d: cannot cast %r in column %r "
                                "to %s" % (src.path, rowno, v, names[oi],
                                           types[oi]), EXIT_CAST)
                        if on_err == "keep-string":
                            t = to_text(v)
                            out[oi] = t
                            sk[oi] = (2, t)
                key = tuple(sk[i] if sk[i] is not None else NULL_KEY
                            for i in key_idx)
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

        if args.output == "-":
            fout = open(sys.stdout.fileno(), "w", encoding="utf-8",
                        newline="", closefd=False)
        else:
            out_dir = os.path.dirname(os.path.abspath(args.output))
            fd, out_tmp = tempfile.mkstemp(prefix=".merge_out_", dir=out_dir)
            fout = os.fdopen(fd, "w", encoding="utf-8", newline="")
        with fout:
            wkw = {"delimiter": ",", "quotechar": args.csv_quotechar,
                   "doublequote": True, "lineterminator": "\n",
                   "quoting": csv.QUOTE_MINIMAL}
            if args.csv_escapechar is not None:
                wkw["escapechar"] = args.csv_escapechar
            writer = csv.writer(fout, **wkw)
            writer.writerow(names)
            null_lit = args.csv_null_literal
            for _, _, out in stream:
                writer.writerow([null_lit if v is None else v for v in out])
        if out_tmp is not None:
            os.replace(out_tmp, args.output)
            out_tmp = None
        return 0
    except UserError as e:
        sys.stderr.write("error: %s\n" % e)
        return e.code
    except (UnicodeDecodeError, csv.Error, EOFError, gzip.BadGzipFile,
            zlib.error) as e:
        sys.stderr.write("error: malformed input: %s\n" % e)
        return EXIT_PARSE
    except OSError as e:
        sys.stderr.write("error: %s\n" % e)
        return EXIT_GENERIC
    except KeyboardInterrupt:
        return 130
    finally:
        if out_tmp is not None:
            try:
                os.remove(out_tmp)
            except OSError:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
