#!/usr/bin/env python3
"""datagate: fetch remote CSV files and serve them as JSON datasets."""
import argparse
import codecs
import csv
import hashlib
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LIMIT = 100
FETCH_TIMEOUT = 15
MAX_BYTES = 200 * 1024 * 1024
CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DATASETS = {}
LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- helpers

def dataset_id(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def validate_url(source):
    if not source or source != source.strip() or any(c.isspace() for c in source):
        raise ApiError(400, "Invalid URL")
    try:
        parsed = urllib.parse.urlsplit(source)
        _ = parsed.port  # raises on malformed port
    except ValueError:
        raise ApiError(400, "Invalid URL")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ApiError(400, "Invalid URL")


def validate_charset(charset):
    if charset is None:
        return None
    name = charset.strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9._:\-]+", name):
        raise ApiError(400, "Unsupported or malformed charset")
    try:
        info = codecs.lookup(name)
    except LookupError:
        raise ApiError(400, "Unsupported or malformed charset")
    if not getattr(info, "_is_text_encoding", True):
        raise ApiError(400, "Unsupported or malformed charset")
    return info.name


def fetch(source):
    req = urllib.request.Request(source, headers={"User-Agent": "datagate/1.0", "Accept": "text/csv, text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ApiError(404, "Remote source returned HTTP %d" % e.code)
    except Exception as e:  # URLError, timeouts, connection errors, ...
        raise ApiError(404, "Source unreachable: %s" % (getattr(e, "reason", None) or e))
    if len(data) > MAX_BYTES:
        raise ApiError(400, "Source too large")
    return data


BOMS = [
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
]


def detect_and_decode(data):
    for bom, enc in BOMS:
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                break
    # UTF-16 without BOM: many NUL bytes in alternating positions
    if len(data) >= 4 and data.count(b"\x00") * 3 >= len(data):
        even = data[0::2].count(b"\x00")
        odd = data[1::2].count(b"\x00")
        enc = "utf-16-le" if odd > even else "utf-16-be"
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes  # optional
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def decode(data, charset):
    if charset:
        try:
            text = data.decode(charset)
        except (UnicodeDecodeError, LookupError, ValueError):
            raise ApiError(400, "Content could not be decoded with charset %r" % charset)
        if text.startswith("﻿"):
            text = text[1:]
        return text
    text = detect_and_decode(data)
    if text.startswith("﻿"):
        text = text[1:]
    return text


def parse_rows(text, delim):
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim, quotechar='"', doublequote=True)
    rows = []
    for row in reader:
        if not row or all(c.strip() == "" for c in row):
            continue
        rows.append(row)
    return rows


def looks_non_tabular(text):
    s = text.strip()
    if not s:
        return True
    if "\x00" in s:
        return True
    low = s[:512].lower()
    if low.startswith("<!doctype") or low.startswith("<html") or low.startswith("<?xml") or re.match(r"<[a-z!?]", low):
        return True
    if s[0] in "{[":
        try:
            json.loads(s)
            return True
        except ValueError:
            pass
    # Excessive control characters => binary
    ctrl = sum(1 for ch in s[:4096] if ord(ch) < 32 and ch not in "\r\n\t")
    if ctrl > 0:
        return True
    return False


def parse_csv(text):
    if looks_non_tabular(text):
        raise ApiError(400, "Content is not tabular CSV data")
    best = None
    try:
        for delim in CANDIDATE_DELIMITERS:
            rows = parse_rows(text, delim)
            if len(rows) < 2:
                continue
            widths = {len(r) for r in rows}
            if len(widths) == 1:
                w = widths.pop()
                if w > 1 and (best is None or w > best[0]):
                    best = (w, delim, rows)
    except csv.Error:
        raise ApiError(400, "Content is not tabular CSV data")
    if best is None:
        # Single-column file? Accept only if no candidate delimiter is present at all.
        rows = parse_rows(text, ",")
        if len(rows) < 2:
            raise ApiError(400, "CSV must contain a header row and at least one data row")
        if all(len(r) == 1 for r in rows) and not any(d in text for d in CANDIDATE_DELIMITERS):
            best = (1, ",", rows)
        else:
            raise ApiError(400, "Content is not tabular CSV data")
    _, _, rows = best
    header = [h.strip() for h in rows[0]]
    raw = [list(r) for r in rows[1:]]
    data = [[convert_value(v) for v in r] for r in raw]
    if not data:
        raise ApiError(400, "CSV must contain a header row and at least one data row")
    return header, data, raw


INT_RE = re.compile(r"[+-]?(0|[1-9][0-9]*)")
DEC_RE = re.compile(r"[+-]?((0|[1-9][0-9]*)\.[0-9]+|\.[0-9]+|(0|[1-9][0-9]*)(\.[0-9]+)?[eE][+-]?[0-9]+)")


def convert_value(v):
    s = v.strip()
    if INT_RE.fullmatch(s):
        return int(s)
    if DEC_RE.fullmatch(s):
        f = float(s)
        if f == f and f not in (float("inf"), float("-inf")):
            return f
    return v


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "datagate/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def error(self, status, message):
        self.send_json(status, {"ok": False, "error": message})

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def route(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if len(path) > 1:
            path = path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path == "/convert":
            return "convert", None, params
        m = re.fullmatch(r"/datasets/([^/]+)", path)
        if m:
            return "dataset", urllib.parse.unquote(m.group(1)), params
        return None, None, params

    def do_GET(self):
        try:
            kind, arg, params = self.route()
            if kind == "convert":
                self.handle_convert(params)
            elif kind == "dataset":
                self.handle_dataset(arg, params)
            else:
                self.error(404, "Not found")
        except ApiError as e:
            self.error(e.status, e.message)
        except Exception as e:  # pragma: no cover
            self.error(500, "Internal error: %s" % e)

    do_HEAD = do_GET

    def _other(self):
        kind, _, _ = self.route()
        if kind is None:
            self.error(404, "Not found")
        else:
            self.error(405, "Method not allowed")

    do_POST = do_PUT = do_DELETE = do_PATCH = _other

    def handle_convert(self, params):
        sources = params.get("source")
        if not sources or sources[0] == "":
            raise ApiError(400, "Missing required parameter: source")
        source = sources[0]
        validate_url(source)
        charset = params.get("charset", [None])[0]
        charset = validate_charset(charset) if charset is not None else None
        data = fetch(source)
        text = decode(data, charset)
        columns, rows, raw = parse_csv(text)
        did = dataset_id(source)
        with LOCK:
            DATASETS[did] = {"columns": columns, "rows": rows, "raw": raw}
        self.send_json(200, {"ok": True, "endpoint": "/datasets/%s" % did})

    def handle_dataset(self, did, params):
        start = time.perf_counter()
        with LOCK:
            ds = DATASETS.get(did)
        if ds is None:
            raise ApiError(404, "Dataset not found")
        columns = list(ds["columns"])
        opts = parse_controls(params, columns)
        filters = parse_filters(params, columns)
        deadline = start + QUERY_TIMEOUT
        indexed = apply_filters(ds, filters, deadline)
        if opts["sort_col"] is not None:
            ci = columns.index(opts["sort_col"])
            def key(item):
                return sort_key(item[1][ci] if ci < len(item[1]) else None)
            filled = [it for it in indexed if key(it)[0] != 2]
            blank = [it for it in indexed if key(it)[0] == 2]
            check_deadline(deadline)
            filled.sort(key=key, reverse=opts["sort_desc"])  # stable either way
            indexed = filled + blank  # blanks always last, in source order
            check_deadline(deadline)
        total = len(indexed)
        off, size = opts["offset"], opts["size"]
        page = indexed[off:off + size]
        if opts["shape"] == "objects":
            out_rows = []
            for rowid, row in page:
                obj = {}
                if not opts["hide_rowid"]:
                    obj["rowid"] = rowid
                for i, c in enumerate(columns):
                    if c in obj and c != "rowid":
                        continue  # duplicate header: keep first
                    obj[c] = row[i] if i < len(row) else None
                out_rows.append(obj)
        else:
            out_rows = [list(row) for _, row in page]
        elapsed = max(0.0, (time.perf_counter() - start) * 1000.0)
        payload = {"ok": True, "columns": columns, "rows": out_rows}
        if not opts["hide_total"]:
            payload["total"] = total
        payload["query_ms"] = round(elapsed, 3)
        self.send_json(200, payload)


CONTROL_PARAMS = ("_size", "_offset", "_shape", "_sort", "_sort_desc", "_rowid", "_total")
UINT_RE = re.compile(r"[0-9]+")


def parse_controls(params, columns):
    for name in CONTROL_PARAMS:
        if len(params.get(name, [])) > 1:
            raise ApiError(400, "Parameter %s may only be given once" % name)

    def one(name):
        vals = params.get(name)
        return vals[0] if vals else None

    opts = {}
    v = one("_size")
    if v is None:
        opts["size"] = DEFAULT_LIMIT
    else:
        if not UINT_RE.fullmatch(v) or int(v) <= 0:
            raise ApiError(400, "_size must be a positive integer")
        opts["size"] = int(v)
    v = one("_offset")
    if v is None:
        opts["offset"] = 0
    else:
        if not UINT_RE.fullmatch(v):
            raise ApiError(400, "_offset must be a non-negative integer")
        opts["offset"] = int(v)
    v = one("_shape")
    if v is None:
        v = "lists"
    if v not in ("lists", "objects"):
        raise ApiError(400, "_shape must be 'lists' or 'objects'")
    opts["shape"] = v
    for name, key in (("_rowid", "hide_rowid"), ("_total", "hide_total")):
        v = one(name)
        if v is not None and v != "hide":
            raise ApiError(400, "%s only accepts the value 'hide'" % name)
        opts[key] = v == "hide"
    asc, desc = one("_sort"), one("_sort_desc")
    for name, v in (("_sort", asc), ("_sort_desc", desc)):
        if v is not None and (v == "" or v not in columns):
            raise ApiError(400, "%s: unknown column %r" % (name, v))
    if desc is not None:
        opts["sort_col"], opts["sort_desc"] = desc, True
    elif asc is not None:
        opts["sort_col"], opts["sort_desc"] = asc, False
    else:
        opts["sort_col"], opts["sort_desc"] = None, False
    return opts


QUERY_TIMEOUT = 5.0  # seconds
COMPARATORS = ("exact", "contains", "less", "greater")


def parse_filters(params, columns):
    """Collect `<column>__<comparator>=<value>` filters (non-control params only)."""
    filters = []
    for key, vals in params.items():
        if key.startswith("_") or "__" not in key:
            continue
        column, comparator = key.rsplit("__", 1)
        if comparator not in COMPARATORS:
            raise ApiError(400, "Invalid comparator %r in filter %r (expected one of: %s)"
                           % (comparator, key, ", ".join(COMPARATORS)))
        if len(vals) > 1:
            raise ApiError(400, "Duplicate filter key %r" % key)
        if column not in columns:
            raise ApiError(400, "Unknown filter column %r" % column)
        value = vals[0]
        target = value
        if comparator in ("less", "greater"):
            target = to_number(value)
            if target is None:
                raise ApiError(400, "Filter %r requires a numeric value, got %r" % (key, value))
        filters.append((columns.index(column), comparator, value, target))
    return filters


def to_number(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    try:
        f = float(v.strip())
    except ValueError:
        return None
    if f != f:  # NaN never compares meaningfully
        return None
    return f


def display(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v)


def check_deadline(deadline):
    if time.perf_counter() > deadline:
        raise ApiError(400, "Query timed out")


def match(stored, raw, comparator, value, target):
    if comparator == "exact":
        return raw == value or display(stored) == value
    if comparator == "contains":
        d = display(stored)
        return (raw is not None and value in raw) or (d is not None and value in d)
    n = to_number(stored)
    if n is None and raw is not None:
        n = to_number(raw)
    if n is None:
        return False
    return n < target if comparator == "less" else n > target


def apply_filters(ds, filters, deadline):
    rows, raws = ds["rows"], ds.get("raw") or ds["rows"]
    out = []
    for i, row in enumerate(rows):
        if filters and i % 1024 == 0:
            check_deadline(deadline)
        ok = True
        raw = raws[i]
        for ci, comparator, value, target in filters:
            stored = row[ci] if ci < len(row) else None
            r = raw[ci] if ci < len(raw) else None
            if not isinstance(r, str):
                r = display(r)
            if not match(stored, r, comparator, value, target):
                ok = False
                break
        if ok:
            out.append((i + 1, row))
    check_deadline(deadline)
    return out


def sort_key(v):
    # numbers < non-empty strings < empty/missing values
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, (int, float)):
        return (0, v, "")
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return (2, 0, "")
    return (1, 0, v)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="datagate")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start the HTTP server")
    start.add_argument("--port", type=int, default=8001)
    start.add_argument("--address", default="127.0.0.1")
    args = parser.parse_args(argv)
    if args.command == "start":
        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True
        server = ThreadingHTTPServer((args.address, args.port), Handler)
        print("datagate listening on http://%s:%d" % (args.address, args.port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
