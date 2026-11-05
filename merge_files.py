#!/usr/bin/env python3
"""Multi-format merger and sorter.

Ingests CSV, TSV, JSON Lines and Parquet files (optionally gzip-compressed),
reconciles their schemas (explicit or inferred), casts values, and writes a
single CSV globally sorted by a composite key.  Sorting is done with a
bounded-memory external merge sort; all inputs are streamed.

Exit codes:
  0  success
  1  unexpected / I/O failure
  2  invalid usage (bad arguments, missing input, undetectable format)
  3  key column not present in resolved schema
  4  invalid schema file, or cast failure with --on-type-error=fail
  5  input parse error (malformed CSV/TSV/JSONL/Parquet, bad UTF-8,
     compression mismatch)
  6  unsupported nested structure in JSONL/Parquet input
"""

import argparse
import csv
import datetime as _dt
import decimal
import gzip
import heapq
import io
import json
import os
import pickle
import re
import shutil
import signal
import struct
import sys
import tempfile
import zlib

VALID_TYPES = ("string", "int", "float", "bool", "date", "timestamp")
# Inference priority, highest first.
TYPE_PRIORITY = ("timestamp", "date", "bool", "int", "float", "string")

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_KEY = 3
EXIT_SCHEMA = 4
EXIT_PARSE = 5
EXIT_NESTED = 6


class MergeError(Exception):
    """Fatal user-facing error."""

    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


class CastError(Exception):
    pass


# ---------------------------------------------------------------------------
# Parsing / casting
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ]"
    r"(\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$"
)
_BOOL_TRUE = {"true", "t", "yes", "y", "1"}
_BOOL_FALSE = {"false", "f", "no", "n", "0"}
_UTC = _dt.timezone.utc


def parse_int(s):
    t = s.strip()
    if not _INT_RE.match(t):
        raise CastError
    return int(t)


def parse_float(s):
    t = s.strip()
    if not _FLOAT_RE.match(t):
        raise CastError
    try:
        v = float(t)
    except (ValueError, OverflowError):
        raise CastError
    if v != v or v in (float("inf"), float("-inf")):
        raise CastError
    return v


def parse_bool(s):
    t = s.strip().lower()
    if t in _BOOL_TRUE:
        return True
    if t in _BOOL_FALSE:
        return False
    raise CastError


def parse_date(s):
    m = _DATE_RE.match(s.strip())
    if not m:
        raise CastError
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise CastError


def parse_timestamp(s):
    m = _TS_RE.match(s.strip())
    if not m:
        raise CastError
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    try:
        tzinfo = _UTC
        if tz and tz not in ("Z", "z"):
            sign = 1 if tz[0] == "+" else -1
            body = tz[1:].replace(":", "")
            oh = int(body[:2])
            om = int(body[2:4]) if len(body) > 2 else 0
            if oh > 23 or om > 59:
                raise CastError
            tzinfo = _dt.timezone(sign * _dt.timedelta(hours=oh, minutes=om))
        value = _dt.datetime(
            int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), micro,
            tzinfo=tzinfo,
        )
        return value.astimezone(_UTC)
    except (ValueError, OverflowError):
        raise CastError


def parse_string(s):
    return s


PARSERS = {
    "string": parse_string,
    "int": parse_int,
    "float": parse_float,
    "bool": parse_bool,
    "date": parse_date,
    "timestamp": parse_timestamp,
}


def format_value(typ, v):
    if typ == "string":
        return v
    if typ == "int":
        return str(v)
    if typ == "float":
        return repr(v)
    if typ == "bool":
        return "true" if v else "false"
    if typ == "date":
        return v.isoformat()
    if typ == "timestamp":
        if v.microsecond:
            return v.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        return v.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    raise AssertionError(typ)


def key_component(typ, v):
    """Sortable representation of a typed value."""
    if typ == "bool":
        return int(v)
    if typ == "date":
        return v.toordinal()
    if typ == "timestamp":
        # Exact integer microseconds since epoch (all values are UTC).
        delta = v - _dt.datetime(1970, 1, 1, tzinfo=_UTC)
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    return v


def detect_types(s):
    """Return set of types (excluding string) the value parses as."""
    out = set()
    for t in ("timestamp", "date", "bool", "int", "float"):
        try:
            PARSERS[t](s)
        except CastError:
            continue
        out.add(t)
    return out


# ---------------------------------------------------------------------------


_TEMPORAL_ONLY = ("timestamp", "date")


def detect_temporal(s):
    """Types a *typed* string value (JSONL/Parquet) may be promoted to."""
    out = set()
    for t in _TEMPORAL_ONLY:
        try:
            PARSERS[t](s)
        except CastError:
            continue
        out.add(t)
    return out


def to_utc(v):
    if v.tzinfo is None:
        return v.replace(tzinfo=_UTC)
    return v.astimezone(_UTC)


def to_text(v):
    """Canonical text rendering of a typed (non-null) value."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, _dt.datetime):
        return format_value("timestamp", to_utc(v))
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def value_support(v, is_text):
    """Set of non-string types a non-null value can be represented as."""
    if isinstance(v, str):
        return detect_types(v) if is_text else detect_temporal(v)
    if isinstance(v, bool):
        return {"bool"}
    if isinstance(v, int):
        return {"int", "float"}
    if isinstance(v, float):
        return {"float"}
    if isinstance(v, _dt.datetime):
        return {"timestamp"}
    if isinstance(v, _dt.date):
        return {"date"}
    if isinstance(v, decimal.Decimal):
        return {"float"}
    return set()


def cast_value(v, typ):
    """Cast a non-null value to the target type; raises CastError."""
    if isinstance(v, str):
        return PARSERS[typ](v)
    if typ == "string":
        return to_text(v)
    if isinstance(v, decimal.Decimal):
        if not v.is_finite():
            raise CastError
        if typ == "int":
            if v != v.to_integral_value():
                raise CastError
            return int(v)
        if typ == "float":
            return parse_float(str(float(v)))
        raise CastError
    if typ == "int":
        if isinstance(v, bool):
            raise CastError
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")) or not v.is_integer():
                raise CastError
            return int(v)
        raise CastError
    if typ == "float":
        if isinstance(v, bool):
            raise CastError
        if isinstance(v, (int, float)):
            try:
                f = float(v)
            except OverflowError:
                raise CastError
            if f != f or f in (float("inf"), float("-inf")):
                raise CastError
            return f
        raise CastError
    if typ == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, int) and v in (0, 1):
            return bool(v)
        raise CastError
    if typ == "date":
        if isinstance(v, _dt.datetime):
            raise CastError
        if isinstance(v, _dt.date):
            return v
        raise CastError
    if typ == "timestamp":
        if isinstance(v, _dt.datetime):
            return to_utc(v)
        raise CastError
    raise CastError


# ---------------------------------------------------------------------------
# Input sources
# ---------------------------------------------------------------------------

EXT_FORMATS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".parquet": "parquet",
}
# Precedence tiers for --schema-strategy=authoritative (lower wins).
FORMAT_TIER = {"parquet": 0, "jsonl": 1, "csv": 2, "tsv": 2}
GZIP_MAGIC = b"\x1f\x8b"
PARQUET_MAGIC = b"PAR1"


def resolve_compression(path, mode):
    try:
        with open(path, "rb") as f:
            magic = f.read(2)
    except OSError as e:
        raise MergeError(EXIT_GENERIC, "cannot read %s: %s" % (path, e))
    is_gz = magic == GZIP_MAGIC
    if mode == "auto":
        want = path.lower().endswith(".gz")
    else:
        want = mode == "gzip"
    if want and not is_gz:
        raise MergeError(EXIT_PARSE,
                         "compression mismatch: %s is not gzip-compressed" % path)
    if not want and is_gz:
        raise MergeError(EXIT_PARSE,
                         "compression mismatch: %s is gzip-compressed" % path)
    return want


def open_binary(path, gz):
    if gz:
        return gzip.open(path, "rb")
    return open(path, "rb")


def peek_magic(path, gz, n=4):
    try:
        with open_binary(path, gz) as f:
            return f.read(n)
    except (OSError, EOFError, zlib.error) as e:
        raise MergeError(EXIT_PARSE, "cannot read %s: %s" % (path, e))


def resolve_format(path, mode, gz):
    if mode != "auto":
        fmt = mode
    else:
        name = os.path.basename(path).lower()
        if name.endswith(".gz"):
            name = name[:-3]
        ext = os.path.splitext(name)[1]
        fmt = EXT_FORMATS.get(ext)
        if fmt is None:
            if peek_magic(path, gz) == PARQUET_MAGIC:
                fmt = "parquet"
            else:
                raise MergeError(EXIT_USAGE,
                                 "cannot determine input format of %s" % path)
    if fmt == "parquet" and peek_magic(path, gz) != PARQUET_MAGIC:
        raise MergeError(EXIT_PARSE, "%s is not a valid Parquet file" % path)
    return fmt


def wrap_stream_errors(path, gen):
    """Translate decompression / decoding failures into parse errors."""
    try:
        for item in gen:
            yield item
    except (EOFError, zlib.error, gzip.BadGzipFile) as e:
        raise MergeError(EXIT_PARSE, "corrupt gzip stream in %s: %s" % (path, e))
    except UnicodeDecodeError as e:
        raise MergeError(EXIT_PARSE, "%s is not valid UTF-8: %s" % (path, e))
    except csv.Error as e:
        raise MergeError(EXIT_PARSE, "CSV parse error in %s: %s" % (path, e))


def first_index(header):
    idx = {}
    for i, name in enumerate(header):
        if name not in idx:
            idx[name] = i
    return idx


class Source(object):
    """One input file.  Values yielded are None (null), str or typed."""

    def __init__(self, path, fmt, gz, args, tmpdir):
        self.path = path
        self.fmt = fmt
        self.gz = gz
        self.args = args
        self.tmpdir = tmpdir
        self.is_text = fmt in ("csv", "tsv")
        self.tier = FORMAT_TIER[fmt]
        self._pq_path = None

    # -- text formats -----------------------------------------------------

    def _text_rows(self):
        """Yield (header, None) first, then raw row lists (text formats)."""
        if self.fmt == "csv":
            return self._csv_rows()
        return self._tsv_rows()

    def _csv_rows(self):
        a = self.args
        kw = {"delimiter": ",", "quotechar": a.csv_quotechar,
              "doublequote": True, "strict": False}
        if a.csv_escapechar is not None:
            kw["escapechar"] = a.csv_escapechar
        with open_binary(self.path, self.gz) as b:
            f = io.TextIOWrapper(b, encoding="utf-8-sig", newline="")
            reader = csv.reader(f, **kw)
            for row in reader:
                yield row

    def _tsv_rows(self):
        with open_binary(self.path, self.gz) as b:
            first = True
            ncols = None
            lineno = 0
            for raw in b:
                lineno += 1
                line = raw.decode("utf-8")
                if first and line.startswith("﻿"):
                    line = line[1:]
                if line.endswith("\n"):
                    line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
                if first:
                    first = False
                    fields = line.split("\t")
                    ncols = len(fields)
                    yield fields
                    continue
                if line == "":
                    continue
                fields = line.split("\t")
                if len(fields) > ncols:
                    raise MergeError(
                        EXIT_PARSE,
                        "TSV parse error in %s line %d: %d fields, header has %d "
                        "(literal tab inside field?)"
                        % (self.path, lineno, len(fields), ncols))
                yield fields

    def _text_iter(self, names):
        null_literal = self.args.csv_null_literal
        it = self._text_rows()
        header = None
        for header in it:
            break
        if header is None:
            return
        idx = first_index(header)
        src = [idx.get(n) for n in names]
        for row in it:
            n = len(row)
            if n == 0:
                continue
            out = []
            for i in src:
                if i is None or i >= n:
                    out.append(None)
                else:
                    v = row[i]
                    out.append(None if (v == "" or v == null_literal) else v)
            yield out

    def _text_columns(self):
        for header in self._text_rows():
            return list(first_index(header))
        return []

    # -- JSON Lines ---------------------------------------------------------

    def _json_objects(self):
        with open_binary(self.path, self.gz) as b:
            lineno = 0
            for raw in b:
                lineno += 1
                line = raw.decode("utf-8")
                if lineno == 1 and line.startswith("﻿"):
                    line = line[1:]
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError as e:
                    raise MergeError(EXIT_PARSE, "invalid JSON in %s line %d: %s"
                                     % (self.path, lineno, e))
                if not isinstance(obj, dict):
                    raise MergeError(EXIT_PARSE,
                                     "%s line %d: expected a JSON object"
                                     % (self.path, lineno))
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        raise MergeError(
                            EXIT_NESTED,
                            "nested value for field %r in %s line %d"
                            % (k, self.path, lineno))
                    if isinstance(v, int) and not isinstance(v, bool):
                        if not (-(1 << 63) <= v < (1 << 63)):
                            try:
                                obj[k] = float(v)
                            except OverflowError:
                                obj[k] = str(v)
                yield obj

    def _json_iter(self, names):
        for obj in self._json_objects():
            yield [obj.get(n) for n in names]

    # -- Parquet ---------------------------------------------------------------

    def _parquet_file(self):
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise MergeError(EXIT_GENERIC,
                             "pyarrow is required to read Parquet input %s" % self.path)
        path = self.path
        if self.gz:
            if self._pq_path is None:
                fd, tmp = tempfile.mkstemp(prefix="pq_", suffix=".parquet",
                                           dir=self.tmpdir)
                try:
                    with os.fdopen(fd, "wb") as out, gzip.open(self.path, "rb") as src:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                except (OSError, EOFError, zlib.error) as e:
                    raise MergeError(EXIT_PARSE, "corrupt gzip stream in %s: %s"
                                     % (self.path, e))
                self._pq_path = tmp
            path = self._pq_path
        try:
            pf = pq.ParquetFile(path)
        except Exception as e:
            raise MergeError(EXIT_PARSE, "cannot read Parquet file %s: %s"
                             % (self.path, e))
        import pyarrow as pa
        for field in pf.schema_arrow:
            t = field.type
            if pa.types.is_dictionary(t):
                t = t.value_type
            if pa.types.is_nested(t):
                raise MergeError(EXIT_NESTED,
                                 "nested Parquet column %r (%s) in %s"
                                 % (field.name, field.type, self.path))
        return pf

    def _batch_rows(self, pf):
        meta = pf.metadata
        rows = max(1, meta.num_rows)
        total = 0
        for i in range(meta.num_row_groups):
            total += meta.row_group(i).total_byte_size
        per_row = max(1, total // rows)
        budget = max(1, int(self.args.parquet_row_group_bytes))
        mem_cap = max(64 * 1024, self.args.memory_limit_mb * 1024 * 1024 // 16)
        n = min(budget // per_row, mem_cap // (per_row * 4 + 64))
        return int(max(1, min(n, 65536)))

    def _parquet_batches(self, pf, cols):
        import pyarrow as pa
        import pyarrow.compute as pc
        try:
            for batch in pf.iter_batches(batch_size=self._batch_rows(pf),
                                         columns=cols, use_threads=False):
                arrays = []
                for arr in batch.columns:
                    t = arr.type
                    if pa.types.is_dictionary(t):
                        arr = arr.dictionary_decode()
                        t = arr.type
                    if pa.types.is_timestamp(t) and t.unit == "ns":
                        arr = pc.cast(arr, pa.timestamp("us", t.tz), safe=False)
                    elif pa.types.is_duration(t) and t.unit == "ns":
                        arr = pc.cast(arr, pa.duration("us"), safe=False)
                    elif pa.types.is_time64(t) and t.unit == "ns":
                        arr = pc.cast(arr, pa.time64("us"), safe=False)
                    arrays.append(arr.to_pylist())
                yield arrays
        except MergeError:
            raise
        except Exception as e:
            raise MergeError(EXIT_PARSE, "error reading Parquet file %s: %s"
                             % (self.path, e))

    def _parquet_iter(self, names):
        pf = self._parquet_file()
        present = set(pf.schema_arrow.names)
        cols = []
        for n in names:
            if n in present and n not in cols:
                cols.append(n)
        pos = {n: i for i, n in enumerate(cols)}
        src = [pos.get(n) for n in names]
        if not cols:
            # Still must emit one row per record.
            for _ in range(pf.metadata.num_rows):
                yield [None] * len(names)
            return
        for arrays in self._parquet_batches(pf, cols):
            for vals in zip(*arrays):
                yield [None if i is None else _pq_norm(vals[i]) for i in src]

    # -- public API -----------------------------------------------------------

    def iter_rows(self, names):
        if self.is_text:
            gen = self._text_iter(names)
        elif self.fmt == "jsonl":
            gen = self._json_iter(names)
        else:
            gen = self._parquet_iter(names)
        return wrap_stream_errors(self.path, gen)

    def observe(self):
        """Return (column names, {col: support set}) for schema inference.

        A column appears in the support mapping only if this file provides
        type evidence for it (non-null values, or a typed Parquet column).
        """
        if self.is_text or self.fmt == "jsonl":
            return self._observe_rows()
        return self._observe_parquet()

    def _observe_rows(self):
        cands = {}
        if self.is_text:
            cols = self._text_columns_safe()
            rows = self.iter_rows(cols)
            for row in rows:
                for name, v in zip(cols, row):
                    if v is None:
                        continue
                    c = cands.get(name)
                    if c is None:
                        cands[name] = detect_types(v)
                    elif c:
                        c &= detect_types(v)
            return cols, cands
        seen = {}
        for obj in wrap_stream_errors(self.path, self._json_objects()):
            for name, v in obj.items():
                seen[name] = True
                if v is None:
                    continue
                c = cands.get(name)
                if c is None:
                    cands[name] = value_support(v, False)
                elif c:
                    c &= value_support(v, False)
        return list(seen), cands

    def _text_columns_safe(self):
        def gen():
            for c in self._text_columns():
                yield c
        return list(wrap_stream_errors(self.path, gen()))

    def _observe_parquet(self):
        import pyarrow as pa
        pf = self._parquet_file()
        cols = list(pf.schema_arrow.names)
        cands = {}
        string_cols = []
        for field in pf.schema_arrow:
            t = field.type
            if pa.types.is_dictionary(t):
                t = t.value_type
            if pa.types.is_boolean(t):
                cands[field.name] = {"bool"}
            elif pa.types.is_integer(t):
                cands[field.name] = {"int", "float"}
            elif pa.types.is_floating(t) or pa.types.is_decimal(t):
                cands[field.name] = {"float"}
            elif pa.types.is_date(t):
                cands[field.name] = {"date"}
            elif pa.types.is_timestamp(t):
                cands[field.name] = {"timestamp"}
            elif pa.types.is_null(t):
                pass
            elif (pa.types.is_string(t) or pa.types.is_large_string(t)
                  or getattr(pa.types, "is_string_view", lambda _t: False)(t)):
                string_cols.append(field.name)
            else:
                cands[field.name] = set()
        if string_cols:
            uniq = []
            for n in string_cols:
                if n not in uniq:
                    uniq.append(n)
            for arrays in self._parquet_batches(pf, uniq):
                for name, values in zip(uniq, arrays):
                    c = cands.get(name)
                    for v in values:
                        if v is None:
                            continue
                        if c is None:
                            c = cands[name] = detect_temporal(v)
                        elif c:
                            c &= detect_temporal(v)
                        else:
                            break
        return cols, cands


def _pq_norm(v):
    if v is None or isinstance(v, (str, bool, int, float, _dt.date,
                                   decimal.Decimal)):
        return v
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(v).hex()
    # time-of-day, durations, etc. are carried as plain strings
    return str(v)


def open_sources(args, tmpdir):
    sources = []
    for p in args.inputs:
        if not os.path.isfile(p):
            raise MergeError(EXIT_USAGE, "input file not found: %s" % p)
    for p in args.inputs:
        gz = resolve_compression(p, args.compression)
        fmt = resolve_format(p, args.input_format, gz)
        sources.append(Source(p, fmt, gz, args, tmpdir))
    return sources


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise MergeError(EXIT_SCHEMA, "cannot read schema file %r: %s" % (path, e))
    except ValueError as e:
        raise MergeError(EXIT_SCHEMA, "invalid schema JSON in %r: %s" % (path, e))
    cols_in = None
    if isinstance(data, dict) and isinstance(data.get("columns"), list):
        cols_in = data["columns"]
    elif isinstance(data, list):
        cols_in = data
    if cols_in is None:
        raise MergeError(EXIT_SCHEMA, "schema must be an object with a 'columns' list")
    cols = []
    seen = set()
    for c in cols_in:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise MergeError(EXIT_SCHEMA, "each schema column must have a string 'name'")
        typ = c.get("type", "string")
        if not isinstance(typ, str) or typ.lower() not in VALID_TYPES:
            raise MergeError(EXIT_SCHEMA,
                             "invalid type %r for column %r" % (typ, c["name"]))
        if c["name"] in seen:
            raise MergeError(EXIT_SCHEMA, "duplicate column %r in schema" % c["name"])
        seen.add(c["name"])
        cols.append((c["name"], typ.lower()))
    if not cols:
        raise MergeError(EXIT_SCHEMA, "schema has no columns")
    return cols


def combine_by_infer(supports, infer):
    """Combine per-file support sets according to --infer."""
    if infer == "loose":
        acc = None
        for s in supports:
            acc = set(s) if acc is None else (acc & s)
        return best_type(acc or ())
    types = set(best_type(s) for s in supports)
    if len(types) == 1:
        return types.pop()
    return "string"


def resolve_column_type(obs, strategy, infer):
    """obs: list of (tier, support set), one per file with evidence."""
    if not obs:
        return "string"
    if strategy == "authoritative":
        top = min(t for t, _ in obs)
        return combine_by_infer([s for t, s in obs if t == top], infer)
    if strategy == "consensus":
        n = len(obs)
        for t in TYPE_PRIORITY:
            if t == "string":
                break
            count = sum(1 for _, s in obs if t in s)
            if count * 2 > n:
                return t
        return "string"
    # union: the most specific type every observed value fits.
    acc = None
    for _, s in obs:
        acc = set(s) if acc is None else (acc & s)
    return best_type(acc or ())


def infer_schema(sources, args):
    all_cols = set()
    obs = {}
    for src in sources:
        cols, cands = src.observe()
        all_cols.update(cols)
        for name, c in cands.items():
            obs.setdefault(name, []).append((src.tier, c))
    return [(name, resolve_column_type(obs.get(name, []), args.schema_strategy,
                                       args.infer))
            for name in sorted(all_cols)]

def best_type(cands):
    for t in TYPE_PRIORITY:
        if t == "string" or t in cands:
            return t
    return "string"


# ---------------------------------------------------------------------------
# External sort
# ---------------------------------------------------------------------------

class Record(object):
    __slots__ = ("key", "seq", "row")

    def __init__(self, key, seq, row):
        self.key = key
        self.seq = seq
        self.row = row


def make_lt(desc):
    if desc:
        def lt(a, b):
            if a.key != b.key:
                return a.key > b.key
            return a.seq < b.seq
    else:
        def lt(a, b):
            if a.key != b.key:
                return a.key < b.key
            return a.seq < b.seq
    return lt


_LEN = struct.Struct("<I")


def write_run(records, tmpdir, counter):
    # Each record is an independent, length-prefixed pickle so that reading
    # never accumulates an unpickler memo (which would hold the whole run).
    path = os.path.join(tmpdir, "run_%06d.bin" % counter)
    dumps = pickle.dumps
    proto = pickle.HIGHEST_PROTOCOL
    pack = _LEN.pack
    with open(path, "wb", buffering=1024 * 1024) as f:
        w = f.write
        for r in records:
            b = dumps((r.key, r.seq, r.row), proto)
            w(pack(len(b)))
            w(b)
    return path


def iter_run(path):
    loads = pickle.loads
    unpack = _LEN.unpack
    size = _LEN.size
    with open(path, "rb", buffering=256 * 1024) as f:
        read = f.read
        while True:
            h = read(size)
            if len(h) < size:
                return
            k, s, row = loads(read(unpack(h)[0]))
            yield Record(k, s, row)


def merge_iters(iters, desc):
    """K-way stable merge of sorted Record iterators."""
    lt = make_lt(desc)

    class Entry(object):
        __slots__ = ("rec", "it")

        def __init__(self, rec, it):
            self.rec = rec
            self.it = it

        def __lt__(self, other):
            return lt(self.rec, other.rec)

    heap = []
    for it in iters:
        for rec in it:
            heap.append(Entry(rec, it))
            break
    heapq.heapify(heap)
    while heap:
        e = heap[0]
        yield e.rec
        nxt = next(e.it, None)
        if nxt is None:
            heapq.heappop(heap)
        else:
            e.rec = nxt
            heapq.heapreplace(heap, e)


def sort_records(records, desc):
    lt = make_lt(desc)

    class K(object):
        __slots__ = ("r",)

        def __init__(self, r):
            self.r = r

        def __lt__(self, other):
            return lt(self.r, other.r)

    # Records are produced in seq order; Python's sort is stable, and the
    # comparator falls back to seq for equal keys anyway.
    records.sort(key=K)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def row_size_estimate(row, key):
    size = 120 + 8 * len(row)
    for v in row:
        size += 50 + len(v)
    size += 64 * (len(key) + 1)
    return size


def process(args):
    if args.temp_dir:
        try:
            os.makedirs(args.temp_dir, exist_ok=True)
        except OSError as e:
            raise MergeError(EXIT_GENERIC, "cannot create temp dir %s: %s"
                             % (args.temp_dir, e))
    tmpdir = tempfile.mkdtemp(prefix="merge_files_", dir=args.temp_dir)
    out_tmp = None
    try:
        sources = open_sources(args, tmpdir)
        null_literal = args.csv_null_literal

        if args.schema:
            schema = load_schema(args.schema)
        else:
            schema = None

        key_cols = [k.strip() for k in args.key.split(",")]
        if not key_cols or any(k == "" for k in key_cols):
            raise MergeError(EXIT_USAGE, "invalid --key specification: %r" % args.key)

        if schema is None:
            schema = infer_schema(sources, args)

        names = [n for n, _ in schema]
        types = [t for _, t in schema]
        name_pos = {n: i for i, n in enumerate(names)}
        for k in key_cols:
            if k not in name_pos:
                raise MergeError(EXIT_KEY,
                                 "key column %r not present in resolved schema" % k)
        key_pos = [name_pos[k] for k in key_cols]

        mem_bytes = max(1, args.memory_limit_mb) * 1024 * 1024
        chunk_budget = max(256 * 1024, int(mem_bytes * 0.25))
        fan_in = 64

        runs = []
        buf = []
        buf_size = 0
        seq = 0
        on_err = args.on_type_error
        ncols = len(names)
        null_key = (0,)

        for src in sources:
            rowno = 0
            for row in src.iter_rows(names):
                rowno += 1
                out_row = [null_literal] * ncols
                key_vals = [null_key] * ncols
                for j in range(ncols):
                    v = row[j]
                    if v is None:
                        continue
                    typ = types[j]
                    try:
                        cv = cast_value(v, typ)
                    except CastError:
                        if on_err == "fail":
                            raise MergeError(
                                EXIT_SCHEMA,
                                "cannot cast value %r in column %r to %s "
                                "(file %s, record %d)"
                                % (to_text(v), names[j], typ, src.path, rowno))
                        if on_err == "keep-string":
                            raw = to_text(v)
                            out_row[j] = raw
                            key_vals[j] = (2, raw)
                        continue
                    out_row[j] = format_value(typ, cv)
                    key_vals[j] = (1, key_component(typ, cv))
                key = tuple([key_vals[p] for p in key_pos])
                buf.append(Record(key, seq, out_row))
                seq += 1
                buf_size += row_size_estimate(out_row, key)
                if buf_size >= chunk_budget:
                    sort_records(buf, args.desc)
                    runs.append(write_run(buf, tmpdir, len(runs)))
                    buf = []
                    buf_size = 0

        sort_records(buf, args.desc)

        # Reduce number of runs to at most fan_in via multi-pass merging.
        counter = len(runs)
        while len(runs) + (1 if buf else 0) > fan_in:
            group, runs = runs[:fan_in], runs[fan_in:]
            merged = merge_iters([iter_run(p) for p in group], args.desc)
            runs.append(write_run(merged, tmpdir, counter))
            counter += 1
            for p in group:
                os.remove(p)

        streams = [iter_run(p) for p in runs]
        if buf:
            streams.append(iter(buf))
        stream = merge_iters(streams, args.desc)

        if args.output == "-":
            out = sys.stdout
            try:
                out.reconfigure(newline="")
            except Exception:
                pass
            write_output(out, names, stream, args)
            out.flush()
        else:
            out_dir = os.path.dirname(os.path.abspath(args.output))
            fd, out_tmp = tempfile.mkstemp(prefix=".merge_out_", dir=out_dir)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
                write_output(out, names, stream, args)
            os.replace(out_tmp, args.output)
            out_tmp = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if out_tmp is not None:
            try:
                os.remove(out_tmp)
            except OSError:
                pass


def write_output(out, names, stream, args):
    kw = {"delimiter": ",", "quotechar": args.csv_quotechar,
          "lineterminator": "\n", "quoting": csv.QUOTE_MINIMAL}
    if args.csv_escapechar is not None:
        kw["escapechar"] = args.csv_escapechar
        kw["doublequote"] = False
    else:
        kw["doublequote"] = True
    w = csv.writer(out, **kw)
    w.writerow(names)
    for rec in stream:
        w.writerow(rec.row)


def single_char(name):
    def conv(s):
        if len(s) != 1:
            raise argparse.ArgumentTypeError("%s must be a single character" % name)
        return s
    return conv


def build_parser():
    p = argparse.ArgumentParser(
        prog="merge_files.py",
        description="Merge CSV/TSV/JSONL/Parquet files into one sorted CSV.")
    p.add_argument("--output", required=True, help="output path or - for stdout")
    p.add_argument("--key", required=True, help="comma-separated key columns")
    p.add_argument("--desc", action="store_true", help="sort descending")
    p.add_argument("--schema", default=None, help="schema JSON file")
    p.add_argument("--infer", choices=["strict", "loose"], default="strict")
    p.add_argument("--schema-strategy",
                   choices=["authoritative", "consensus", "union"],
                   default="authoritative")
    p.add_argument("--on-type-error", choices=["coerce-null", "fail", "keep-string"],
                   default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=256)
    p.add_argument("--temp-dir", default=None)
    p.add_argument("--csv-quotechar", type=single_char("--csv-quotechar"), default='"')
    p.add_argument("--csv-escapechar", type=single_char("--csv-escapechar"), default=None)
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("--input-format",
                   choices=["auto", "csv", "tsv", "jsonl", "parquet"], default="auto")
    p.add_argument("--compression", choices=["auto", "none", "gzip"], default="auto")
    p.add_argument("--parquet-row-group-bytes", type=int, default=16 * 1024 * 1024)
    p.add_argument("inputs", nargs="+", help="input files")
    return p


def _on_signal(signum, frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.memory_limit_mb <= 0:
        sys.stderr.write("error: --memory-limit-mb must be positive\n")
        return EXIT_USAGE
    if args.parquet_row_group_bytes <= 0:
        sys.stderr.write("error: --parquet-row-group-bytes must be positive\n")
        return EXIT_USAGE
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass
    try:
        process(args)
    except MergeError as e:
        sys.stderr.write("error: %s\n" % e)
        return e.code
    except csv.Error as e:
        sys.stderr.write("error: CSV parse error: %s\n" % e)
        return EXIT_PARSE
    except UnicodeDecodeError as e:
        sys.stderr.write("error: input is not valid UTF-8: %s\n" % e)
        return EXIT_PARSE
    except BrokenPipeError:
        try:
            sys.stdout = None
        except Exception:
            pass
        return EXIT_GENERIC
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        sys.stderr.write("error: %s\n" % e)
        return EXIT_GENERIC
    return 0


if __name__ == "__main__":
    sys.exit(main())
