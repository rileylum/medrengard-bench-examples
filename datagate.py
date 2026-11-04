#!/usr/bin/env python3
"""datagate: fetch remote CSV files and serve them as JSON datasets."""
import argparse
import codecs
import csv
import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except Exception:  # pragma: no cover
    _cn_from_bytes = None

ROW_LIMIT = 100
FETCH_TIMEOUT = 15
MAX_BYTES = 200 * 1024 * 1024

DATASETS = {}
LOCK = threading.Lock()

INT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)$")
DEC_RE = re.compile(r"^[+-]?((0|[1-9]\d*)\.\d+|\.\d+|(0|[1-9]\d*)\.)([eE][+-]?\d+)?$|^[+-]?(0|[1-9]\d*)[eE][+-]?\d+$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def dataset_id(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def validate_url(source):
    try:
        p = urllib.parse.urlsplit(source)
    except ValueError:
        raise ApiError(400, "Invalid URL")
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        raise ApiError(400, "Invalid URL")
    try:
        host = p.hostname
        p.port  # may raise ValueError
    except ValueError:
        raise ApiError(400, "Invalid URL")
    if not host or any(c.isspace() for c in source):
        raise ApiError(400, "Invalid URL")


def validate_charset(charset):
    if not charset or not re.match(r"^[A-Za-z0-9._:\-]+$", charset):
        raise ApiError(400, "Unsupported or malformed charset")
    try:
        info = codecs.lookup(charset)
    except (LookupError, ValueError):
        raise ApiError(400, "Unsupported or malformed charset")
    if not getattr(info, "_is_text_encoding", True):
        raise ApiError(400, "Unsupported or malformed charset")
    return info.name


def fetch(source):
    req = urllib.request.Request(source, headers={"User-Agent": "datagate/1.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise ApiError(404, "Remote returned HTTP %d" % status)
            data = resp.read(MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "") or ""
    except ApiError:
        raise
    except urllib.error.HTTPError as e:
        raise ApiError(404, "Remote returned HTTP %d" % e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ApiError(404, "Source unreachable: %s" % (getattr(e, "reason", None) or e))
    except Exception as e:
        raise ApiError(404, "Source unreachable: %s" % e)
    return data, ctype


def detect_and_decode(data):
    boms = [
        (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
    ]
    for bom, enc in boms:
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # BOM-less UTF-16 heuristic
    if len(data) >= 4 and data.count(b"\x00") > len(data) // 4:
        even = data[0::2].count(b"\x00")
        odd = data[1::2].count(b"\x00")
        enc = "utf-16-le" if odd > even else "utf-16-be"
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    if _cn_from_bytes is not None:
        try:
            best = _cn_from_bytes(data).best()
            if best is not None and best.encoding:
                return data.decode(best.encoding)
        except Exception:
            pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def convert_value(v):
    s = v.strip()
    if INT_RE.match(s):
        try:
            return int(s)
        except ValueError:
            return v
    if DEC_RE.match(s):
        try:
            f = float(s)
            if f != f or f in (float("inf"), float("-inf")):
                return v
            return f
        except ValueError:
            return v
    return v


DELIMS = [",", ";", "\t", "|"]


def detect_delimiter(text):
    lines = [l for l in text.splitlines() if l.strip()][:50]
    sample = "\n".join(lines)
    best = None
    best_score = None
    for d in DELIMS:
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=d))
        except csv.Error:
            continue
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            continue
        counts = [len(r) for r in rows]
        hdr = counts[0]
        if hdr < 2:
            continue
        consistent = sum(1 for c in counts if c == hdr) / len(counts)
        score = (consistent, hdr)
        if best_score is None or score > best_score:
            best, best_score = d, score
    return best


def parse_csv(text):
    if text.startswith("﻿"):
        text = text[1:]
    if "\x00" in text:
        raise ApiError(400, "Content is not tabular")
    stripped = text.strip()
    if not stripped:
        raise ApiError(400, "Content is not tabular: empty")
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            raise ApiError(400, "Content is not tabular: JSON")
        except ValueError:
            pass
    low = stripped[:200].lower()
    if low.startswith("<") and ("<html" in low or "<!doctype" in low or "<?xml" in low or re.match(r"^<[a-z!?]", low)):
        raise ApiError(400, "Content is not tabular: markup")
    delim = detect_delimiter(text)
    if delim is None:
        raise ApiError(400, "Content is not tabular: no delimiter detected")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    except csv.Error as e:
        raise ApiError(400, "Content is not tabular: %s" % e)
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ApiError(400, "Content is not tabular: need a header row and at least one data row")
    header = [c.strip() for c in rows[0]]
    n = len(header)
    data_rows = rows[1:]
    matching = sum(1 for r in data_rows if len(r) == n)
    if matching * 2 < len(data_rows):
        raise ApiError(400, "Content is not tabular: inconsistent column counts")
    out = []
    for r in data_rows:
        if len(r) < n:
            r = r + [""] * (n - len(r))
        elif len(r) > n:
            r = r[:n]
        out.append([convert_value(c) for c in r])
    return header, out


def handle_convert(params):
    source = params.get("source")
    if source is None or source == "":
        raise ApiError(400, "Missing required parameter: source")
    validate_url(source)
    charset = params.get("charset")
    enc = None
    if charset is not None:
        enc = validate_charset(charset.strip())
    data, _ctype = fetch(source)
    if len(data) > MAX_BYTES:
        raise ApiError(400, "Source too large")
    if enc is not None:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            raise ApiError(400, "Content cannot be decoded with charset %s" % charset)
    else:
        text = detect_and_decode(data)
    columns, rows = parse_csv(text)
    did = dataset_id(source)
    with LOCK:
        DATASETS[did] = {"columns": columns, "rows": rows, "source": source}
    return 200, {"ok": True, "endpoint": "/datasets/%s" % did}


CONTROL_PARAMS = ("_size", "_offset", "_shape", "_sort", "_sort_desc", "_rowid", "_total")
UINT_RE = re.compile(r"^[0-9]+$")


def _sort_key(v):
    if isinstance(v, bool):
        return (0, int(v), "")
    if isinstance(v, (int, float)):
        return (0, v, "")
    if v is None:
        return (-1, 0, "")
    return (1, 0, str(v))


def handle_dataset(did, multi=None):
    start = time.perf_counter()
    multi = multi or {}
    with LOCK:
        ds = DATASETS.get(did)
    if ds is None:
        raise ApiError(404, "Dataset not found")
    columns = list(ds["columns"])

    for name in CONTROL_PARAMS:
        if len(multi.get(name, [])) > 1:
            raise ApiError(400, "Parameter %s may only be specified once" % name)
    ctl = {k: multi[k][0] for k in CONTROL_PARAMS if k in multi}

    size = ROW_LIMIT
    if "_size" in ctl:
        v = ctl["_size"]
        if not UINT_RE.match(v) or int(v) <= 0:
            raise ApiError(400, "_size must be a positive integer")
        size = int(v)
    offset = 0
    if "_offset" in ctl:
        v = ctl["_offset"]
        if not UINT_RE.match(v):
            raise ApiError(400, "_offset must be a non-negative integer")
        offset = int(v)
    shape = ctl.get("_shape", "lists")
    if shape not in ("lists", "objects"):
        raise ApiError(400, "_shape must be 'lists' or 'objects'")
    for name in ("_rowid", "_total"):
        if name in ctl and ctl[name] != "hide":
            raise ApiError(400, "%s only accepts the value 'hide'" % name)

    sort_col = None
    desc = False
    for name in ("_sort", "_sort_desc"):
        if name in ctl:
            v = ctl[name]
            if v == "":
                raise ApiError(400, "%s requires a column name" % name)
            if v not in columns:
                raise ApiError(400, "Unknown column for %s: %s" % (name, v))
    if "_sort_desc" in ctl:
        sort_col, desc = ctl["_sort_desc"], True
    elif "_sort" in ctl:
        sort_col = ctl["_sort"]

    indexed = list(enumerate(ds["rows"], start=1))
    if sort_col is not None:
        ci = columns.index(sort_col)
        indexed.sort(key=lambda t: _sort_key(t[1][ci] if ci < len(t[1]) else None),
                     reverse=desc)
    total = len(indexed)
    page = indexed[offset:offset + size]

    show_rowid = ctl.get("_rowid") != "hide"
    if shape == "objects":
        rows = []
        for rowid, r in page:
            obj = {}
            if show_rowid:
                obj["rowid"] = rowid
            for i, c in enumerate(columns):
                if show_rowid and c == "rowid":
                    continue
                obj[c] = r[i] if i < len(r) else None
            rows.append(obj)
    else:
        rows = [list(r) for _, r in page]

    elapsed = max(0.0, (time.perf_counter() - start) * 1000.0)
    out = {"ok": True, "columns": columns, "rows": rows}
    if ctl.get("_total") != "hide":
        out["total"] = total
    out["query_ms"] = round(elapsed, 3)
    return 200, out


class Handler(BaseHTTPRequestHandler):
    server_version = "datagate/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send(self, status, obj, body=True):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def _route(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if len(path) > 1:
            path = path.rstrip("/")
        params = {}
        multi = {}
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            params.setdefault(k, v)
            multi.setdefault(k, []).append(v)
        if path == "/convert":
            return handle_convert(params)
        m = re.match(r"^/datasets/([^/]+)$", path)
        if m:
            return handle_dataset(urllib.parse.unquote(m.group(1)), multi)
        raise ApiError(404, "Not found")

    def _handle(self, body=True):
        try:
            status, obj = self._route()
        except ApiError as e:
            status, obj = e.status, {"ok": False, "error": e.message}
        except Exception as e:  # pragma: no cover
            status, obj = 500, {"ok": False, "error": "Internal error: %s" % e}
        self._send(status, obj, body)

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle(body=False)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_allowed(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path == "/convert" or re.match(r"^/datasets/[^/]+$", path):
            self._send(405, {"ok": False, "error": "Method not allowed"})
        else:
            self._send(404, {"ok": False, "error": "Not found"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed


def main():
    ap = argparse.ArgumentParser(prog="datagate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("--port", type=int, default=8001)
    st.add_argument("--address", default="127.0.0.1")
    args = ap.parse_args()
    if args.cmd == "start":
        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True
        srv = ThreadingHTTPServer((args.address, args.port), Handler)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()


if __name__ == "__main__":
    main()
