#!/usr/bin/env python3
"""CSV merger and sorter.

Ingests multiple CSV files, aligns their schemas (explicit or inferred),
casts values, and writes a single CSV globally sorted by a composite key.
Sorting is done with a bounded-memory external merge sort.
"""

import argparse
import csv
import datetime as _dt
import heapq
import json
import os
import pickle
import re
import shutil
import signal
import sys
import tempfile

VALID_TYPES = ("string", "int", "float", "bool", "date", "timestamp")
# Inference priority, highest first.
TYPE_PRIORITY = ("timestamp", "date", "bool", "int", "float", "string")

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


class MergeError(Exception):
    """Fatal user-facing error."""


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
# CSV helpers
# ---------------------------------------------------------------------------

def make_reader_kwargs(args):
    kw = {"delimiter": ",", "quotechar": args.csv_quotechar, "doublequote": True,
          "strict": False}
    if args.csv_escapechar is not None:
        kw["escapechar"] = args.csv_escapechar
    return kw


def open_csv(path, reader_kwargs):
    f = open(path, "r", encoding="utf-8-sig", newline="")
    return f, csv.reader(f, **reader_kwargs)


def read_header(reader):
    try:
        header = next(reader)
    except StopIteration:
        return None
    return header


def header_index(header):
    """Map column name -> first index."""
    idx = {}
    for i, name in enumerate(header):
        if name not in idx:
            idx[name] = i
    return idx


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise MergeError("cannot read schema file %r: %s" % (path, e))
    except ValueError as e:
        raise MergeError("invalid schema JSON in %r: %s" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list):
        raise MergeError("schema must be an object with a 'columns' list")
    cols = []
    seen = set()
    for c in data["columns"]:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise MergeError("each schema column must have a string 'name'")
        typ = c.get("type", "string")
        if not isinstance(typ, str) or typ.lower() not in VALID_TYPES:
            raise MergeError("invalid type %r for column %r" % (typ, c["name"]))
        if c["name"] in seen:
            raise MergeError("duplicate column %r in schema" % c["name"])
        seen.add(c["name"])
        cols.append((c["name"], typ.lower()))
    if not cols:
        raise MergeError("schema has no columns")
    return cols


def is_null(s, null_literal):
    return s == "" or s == null_literal


def infer_schema(inputs, args, reader_kwargs):
    null_literal = args.csv_null_literal
    all_cols = set()
    # strict: per-file candidate sets; loose: global candidate sets
    global_cands = {}
    per_file_types = {}  # col -> set of resolved types from each file

    for path in inputs:
        f, reader = open_csv(path, reader_kwargs)
        with f:
            header = read_header(reader)
            if header is None:
                continue
            idx = header_index(header)
            all_cols.update(idx)
            cands = {}
            items = list(idx.items())
            for row in reader:
                n = len(row)
                if n == 0:
                    continue
                for name, i in items:
                    if i >= n:
                        continue
                    v = row[i]
                    if is_null(v, null_literal):
                        continue
                    c = cands.get(name)
                    if c is None:
                        cands[name] = detect_types(v)
                    elif c:
                        c &= detect_types(v)
            for name, c in cands.items():
                if args.infer == "loose":
                    g = global_cands.get(name)
                    if g is None:
                        global_cands[name] = set(c)
                    else:
                        g &= c
                else:
                    per_file_types.setdefault(name, set()).add(best_type(c))

    schema = []
    for name in sorted(all_cols):
        if args.infer == "loose":
            c = global_cands.get(name)
            typ = best_type(c) if c is not None else "string"
        else:
            ts = per_file_types.get(name)
            if not ts:
                typ = "string"
            elif len(ts) == 1:
                typ = next(iter(ts))
            else:
                typ = "string"
        schema.append((name, typ))
    return schema


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


def write_run(records, tmpdir, counter):
    path = os.path.join(tmpdir, "run_%06d.bin" % counter)
    with open(path, "wb") as f:
        p = pickle.Pickler(f, protocol=pickle.HIGHEST_PROTOCOL)
        for r in records:
            p.dump((r.key, r.seq, r.row))
            p.clear_memo()
    return path


def iter_run(path):
    with open(path, "rb") as f:
        u = pickle.Unpickler(f)
        while True:
            try:
                k, s, row = u.load()
            except EOFError:
                return
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
    inputs = args.inputs
    for p in inputs:
        if not os.path.isfile(p):
            raise MergeError("input file not found: %s" % p)

    reader_kwargs = make_reader_kwargs(args)
    null_literal = args.csv_null_literal

    if args.schema:
        schema = load_schema(args.schema)
    else:
        schema = infer_schema(inputs, args, reader_kwargs)

    names = [n for n, _ in schema]
    types = [t for _, t in schema]
    name_pos = {n: i for i, n in enumerate(names)}

    key_cols = [k.strip() for k in args.key.split(",")]
    if not key_cols or any(k == "" for k in key_cols):
        raise MergeError("invalid --key specification: %r" % args.key)
    for k in key_cols:
        if k not in name_pos:
            raise MergeError("key column %r not present in resolved schema" % k)
    key_pos = [name_pos[k] for k in key_cols]

    mem_bytes = max(1, args.memory_limit_mb) * 1024 * 1024
    chunk_budget = max(256 * 1024, int(mem_bytes * 0.25))
    fan_in = 64

    tmp_root = args.temp_dir
    if tmp_root:
        os.makedirs(tmp_root, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="merge_files_", dir=tmp_root)
    out_tmp = None
    try:
        runs = []
        buf = []
        buf_size = 0
        seq = 0
        on_err = args.on_type_error
        parsers = [PARSERS[t] for t in types]
        ncols = len(names)

        for path in inputs:
            f, reader = open_csv(path, reader_kwargs)
            with f:
                header = read_header(reader)
                if header is None:
                    continue
                idx = header_index(header)
                src = [idx.get(n) for n in names]
                for row in reader:
                    if not row:
                        continue
                    n = len(row)
                    out_row = [None] * ncols
                    key_vals = {}
                    for j in range(ncols):
                        i = src[j]
                        if i is None or i >= n:
                            out_row[j] = null_literal
                            key_vals[j] = (0,)
                            continue
                        raw = row[i]
                        if is_null(raw, null_literal):
                            out_row[j] = null_literal
                            key_vals[j] = (0,)
                            continue
                        typ = types[j]
                        try:
                            v = parsers[j](raw)
                        except CastError:
                            if on_err == "fail":
                                raise MergeError(
                                    "cannot cast value %r in column %r to %s "
                                    "(file %s, line %d)"
                                    % (raw, names[j], typ, path, reader.line_num))
                            if on_err == "keep-string":
                                out_row[j] = raw
                                key_vals[j] = (2, raw)
                            else:
                                out_row[j] = null_literal
                                key_vals[j] = (0,)
                            continue
                        out_row[j] = format_value(typ, v)
                        key_vals[j] = (1, key_component(typ, v))
                    key = tuple(key_vals[p] for p in key_pos)
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

        sources = [iter_run(p) for p in runs]
        if buf:
            sources.append(iter(buf))
        stream = merge_iters(sources, args.desc)

        if args.output == "-":
            out = sys.stdout
            out_tmp = None
            try:
                out.reconfigure(newline="")
            except Exception:
                pass
            write_output(out, names, stream)
            out.flush()
        else:
            out_dir = os.path.dirname(os.path.abspath(args.output))
            fd, out_tmp = tempfile.mkstemp(prefix=".merge_out_", dir=out_dir)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
                write_output(out, names, stream)
            os.replace(out_tmp, args.output)
            out_tmp = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if out_tmp is not None:
            try:
                os.remove(out_tmp)
            except OSError:
                pass


def write_output(out, names, stream):
    w = csv.writer(out, delimiter=",", quotechar='"', doublequote=True,
                   lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
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
        description="Merge multiple CSV files and sort them by key.")
    p.add_argument("--output", required=True, help="output path or - for stdout")
    p.add_argument("--key", required=True, help="comma-separated key columns")
    p.add_argument("--desc", action="store_true", help="sort descending")
    p.add_argument("--schema", default=None, help="schema JSON file")
    p.add_argument("--infer", choices=["strict", "loose"], default="strict")
    p.add_argument("--on-type-error", choices=["coerce-null", "fail", "keep-string"],
                   default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=256)
    p.add_argument("--temp-dir", default=None)
    p.add_argument("--csv-quotechar", type=single_char("--csv-quotechar"), default='"')
    p.add_argument("--csv-escapechar", type=single_char("--csv-escapechar"), default=None)
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("inputs", nargs="+", help="input CSV files")
    return p


def _on_signal(signum, frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.memory_limit_mb <= 0:
        sys.stderr.write("error: --memory-limit-mb must be positive\n")
        return 2
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
        return 1
    except csv.Error as e:
        sys.stderr.write("error: CSV parse error: %s\n" % e)
        return 1
    except UnicodeDecodeError as e:
        sys.stderr.write("error: input is not valid UTF-8: %s\n" % e)
        return 1
    except BrokenPipeError:
        try:
            sys.stdout = None
        except Exception:
            pass
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
