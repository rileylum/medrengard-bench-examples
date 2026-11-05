#!/usr/bin/env python3
"""CSV Merger and Sorter.

Ingests multiple CSV files, aligns their schemas (explicit or inferred),
casts values to typed columns and writes a single CSV sorted globally by a
composite key, using an external merge sort bounded by --memory-limit-mb.
"""

import argparse
import csv
import datetime as _dt
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

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

VALID_TYPES = ("string", "int", "float", "bool", "date", "timestamp")
# Priority used when inferring: most specific first.
TYPE_PRIORITY = ("timestamp", "date", "bool", "int", "float", "string")

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


class CastError(Exception):
    pass


class UserError(Exception):
    pass


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


def format_timestamp(v):
    out = v.strftime("%Y-%m-%dT%H:%M:%S")
    if v.year < 1000:
        out = "%04d" % v.year + out[out.index("-"):]
    if v.microsecond:
        out += "." + ("%06d" % v.microsecond).rstrip("0")
    return out + "Z"


def ts_sort_value(v):
    delta = v - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def cast_value(text, typ):
    """Return (output_text, sort_key_component) or raise CastError."""
    if typ == "string":
        return text, (1, text)
    if typ == "int":
        v = parse_int(text)
        return str(v), (1, v)
    if typ == "float":
        v = parse_float(text)
        if math.isnan(v):
            return format_float(v), (1, 1)
        return format_float(v), (1, 0, v)
    if typ == "bool":
        v = parse_bool(text)
        return ("true" if v else "false"), (1, v)
    if typ == "date":
        v = parse_date(text)
        return v.isoformat() if v.year >= 1000 else "%04d-%02d-%02d" % (
            v.year, v.month, v.day), (1, v.toordinal())
    if typ == "timestamp":
        v = parse_timestamp(text)
        return format_timestamp(v), (1, ts_sort_value(v))
    raise CastError


NULL_KEY = (0,)


def candidate_types(text):
    """Set of types (except string) a non-null value can be inferred as."""
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


ALL_INFER = frozenset(("timestamp", "date", "bool", "int", "float"))


def pick_type(cands):
    if cands is None:
        return None
    for t in TYPE_PRIORITY:
        if t == "string" or t in cands:
            return t
    return "string"


# --------------------------------------------------------------------------
# CSV IO
# --------------------------------------------------------------------------

def make_reader_kwargs(args):
    kw = {"delimiter": ",", "quotechar": args.csv_quotechar,
          "doublequote": True, "strict": False}
    if args.csv_escapechar is not None:
        kw["escapechar"] = args.csv_escapechar
    return kw


def iter_csv(path, reader_kwargs):
    """Yield the header (list) first, then each data row."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, **reader_kwargs)
        header = None
        for row in reader:
            if header is None:
                header = row
                yield header
                continue
            if not row:
                continue  # blank line
            yield row


def read_header(path, reader_kwargs):
    for row in iter_csv(path, reader_kwargs):
        return row
    return None


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise UserError("cannot read schema %s: %s" % (path, e))
    cols = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(cols, list) or not cols:
        raise UserError("schema must contain a non-empty 'columns' list")
    out = []
    seen = set()
    for c in cols:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise UserError("each schema column needs a string 'name'")
        name = c["name"]
        typ = c.get("type", "string")
        if not isinstance(typ, str) or typ.lower() not in VALID_TYPES:
            raise UserError("invalid type %r for column %r" % (typ, name))
        if name in seen:
            raise UserError("duplicate column %r in schema" % name)
        seen.add(name)
        out.append((name, typ.lower()))
    return out


def is_null(cell, null_literal):
    return cell == "" or cell == null_literal


def infer_schema(inputs, reader_kwargs, mode, null_literal):
    all_cols = set()
    per_file = []  # list of dict col -> candidate set (None = no values)
    for path in inputs:
        it = iter_csv(path, reader_kwargs)
        header = next(it, None)
        if header is None:
            continue
        all_cols.update(header)
        cands = {}
        idx = []
        seen = set()
        for i, name in enumerate(header):
            if name in seen:
                continue
            seen.add(name)
            cands[name] = None
            idx.append((i, name))
        for row in it:
            n = len(row)
            for i, name in idx:
                if i >= n:
                    continue
                cell = row[i]
                if is_null(cell, null_literal):
                    continue
                cur = cands[name]
                if cur is not None and not cur:
                    continue  # already string
                c = candidate_types(cell)
                cands[name] = c if cur is None else (cur & c)
        per_file.append(cands)

    schema = []
    for name in sorted(all_cols):
        if mode == "loose":
            combined = None
            for cands in per_file:
                if name not in cands or cands[name] is None:
                    continue
                combined = cands[name] if combined is None else (
                    combined & cands[name])
            typ = pick_type(combined) or "string"
        else:
            types = set()
            for cands in per_file:
                if name not in cands or cands[name] is None:
                    continue
                types.add(pick_type(cands[name]))
            typ = types.pop() if len(types) == 1 else "string"
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


def estimate_size(row, key):
    s = 120 + 64 * len(row) + 48 * len(key)
    for c in row:
        s += len(c)
    return s


def main(argv=None):
    p = argparse.ArgumentParser(description="Merge and sort CSV files.")
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--desc", action="store_true")
    p.add_argument("--schema")
    p.add_argument("--infer", choices=("strict", "loose"), default="strict")
    p.add_argument("--on-type-error",
                   choices=("coerce-null", "fail", "keep-string"),
                   default="coerce-null")
    p.add_argument("--memory-limit-mb", type=int, default=256)
    p.add_argument("--temp-dir")
    p.add_argument("--csv-quotechar", default='"')
    p.add_argument("--csv-escapechar", default=None)
    p.add_argument("--csv-null-literal", default="")
    p.add_argument("inputs", nargs="+")
    args = p.parse_args(argv)

    if len(args.csv_quotechar) != 1:
        p.error("--csv-quotechar must be a single character")
    if args.csv_escapechar is not None and len(args.csv_escapechar) != 1:
        p.error("--csv-escapechar must be a single character")
    if args.memory_limit_mb <= 0:
        p.error("--memory-limit-mb must be positive")

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
            raise UserError("invalid --key specification")
        for path in args.inputs:
            if not os.path.isfile(path):
                raise UserError("input file not found: %s" % path)
        if args.temp_dir is not None and not os.path.isdir(args.temp_dir):
            raise UserError("temp dir does not exist: %s" % args.temp_dir)

        reader_kwargs = make_reader_kwargs(args)
        null_lit = args.csv_null_literal

        if args.schema:
            schema = load_schema(args.schema)
        else:
            schema = infer_schema(args.inputs, reader_kwargs, args.infer,
                                  null_lit)

        names = [n for n, _ in schema]
        types = [t for _, t in schema]
        pos = {n: i for i, n in enumerate(names)}
        for k in keys:
            if k not in pos:
                raise UserError("key column %r not present in schema" % k)
        key_idx = [pos[k] for k in keys]

        tmpdir = tempfile.mkdtemp(prefix="merge_files_", dir=args.temp_dir)
        budget = max(1 << 20, args.memory_limit_mb * (1 << 20) // 4)
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

        for path in args.inputs:
            it = iter_csv(path, reader_kwargs)
            header = next(it, None)
            if header is None:
                continue
            mapping = []  # (out_idx, in_idx)
            seen = set()
            for i, n in enumerate(header):
                if n in pos and n not in seen:
                    seen.add(n)
                    mapping.append((pos[n], i))
            lineno = 1
            for row in it:
                lineno += 1
                out = [None] * ncols
                sk = [None] * ncols
                rl = len(row)
                for oi, ii in mapping:
                    if ii >= rl:
                        continue
                    cell = row[ii]
                    if is_null(cell, null_lit):
                        continue
                    try:
                        out[oi], sk[oi] = cast_value(cell, types[oi])
                    except CastError:
                        if on_err == "fail":
                            raise UserError(
                                "%s: row %d: cannot cast %r in column %r "
                                "to %s" % (path, lineno, cell, names[oi],
                                           types[oi]))
                        if on_err == "keep-string":
                            out[oi] = cell
                            sk[oi] = (2, cell)
                key = tuple(sk[i] if sk[i] is not None else NULL_KEY
                            for i in key_idx)
                buf.append((key, seq, out))
                seq += 1
                used += estimate_size(row, key)
                if used >= budget:
                    flush()
                    buf = []
                    used = 0

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
        sources = [read_run(r) for r in runs]
        if buf:
            sources.append(iter(buf))
        if len(sources) == 1:
            stream = sources[0]
        else:
            stream = heapq.merge(*sources, key=keyfn)

        if args.output == "-":
            fout = open(sys.stdout.fileno(), "w", encoding="utf-8",
                        newline="", closefd=False)
        else:
            out_dir = os.path.dirname(os.path.abspath(args.output))
            fd, out_tmp = tempfile.mkstemp(prefix=".merge_out_", dir=out_dir)
            fout = os.fdopen(fd, "w", encoding="utf-8", newline="")
        with fout:
            writer = csv.writer(fout, delimiter=",", quotechar='"',
                                doublequote=True, lineterminator="\n",
                                quoting=csv.QUOTE_MINIMAL)
            writer.writerow(names)
            for _, _, out in stream:
                writer.writerow([null_lit if v is None else v for v in out])
        if out_tmp is not None:
            os.replace(out_tmp, args.output)
            out_tmp = None
        return 0
    except UserError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
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
