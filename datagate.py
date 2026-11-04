#!/usr/bin/env python3
"""datagate: fetch remote CSV files and serve them as JSON datasets."""
import argparse
import codecs
import csv
import datetime
import email.errors
import email.parser
import email.policy
import email.utils
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LIMIT = 100
FETCH_TIMEOUT = 15
MAX_BYTES = 200 * 1024 * 1024
CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DATASETS = {}
LOCK = threading.Lock()

# Cache of /convert results: cache key (source, charset) -> dataset id.
CACHE_ENABLED = True
CACHE = {}
INGEST_LOCKS = {}

TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")


def parse_bool_env(name, raw, default):
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in TRUE_VALUES:
        return True
    if v in FALSE_VALUES:
        return False
    raise ValueError("Invalid value for %s: %r (expected one of %s)"
                     % (name, raw, ", ".join(TRUE_VALUES + FALSE_VALUES)))


# ---------------------------------------------------------------- config

MAX_SOURCE_SIZE = None     # int bytes, None = unlimited
ORIGIN_ALLOWLIST = None    # list of lower-case domain suffixes, None = all allowed
REQUIRE_TLS = False
STORAGE_DIR = None
DEFAULT_STORAGE_DIR = os.path.join(tempfile.gettempdir(), "datagate-storage")
CONFIG_KEYS = ("MAX_SOURCE_SIZE", "ORIGIN_ALLOWLIST", "REQUIRE_TLS", "STORAGE_DIR", "CACHE_ENABLED")


class ConfigError(Exception):
    pass


def read_config_file(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError("Cannot read config file %r: %s" % (path, e))
    values = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            raise ConfigError("Config file %r line %d: expected KEY=VALUE" % (path, lineno))
        key, value = s.split("=", 1)
        key = key.strip().upper()
        if not key or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ConfigError("Config file %r line %d: invalid key %r" % (path, lineno, key))
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def parse_size(raw):
    s = raw.strip()
    if s == "":
        return None
    if not re.fullmatch(r"\+?[0-9]+", s):
        raise ConfigError("Invalid value for MAX_SOURCE_SIZE: %r (expected a non-negative integer)" % raw)
    return int(s)


def normalise_suffix(entry):
    e = entry.strip().lower()
    if "://" in e:
        try:
            e = urllib.parse.urlsplit(e).hostname or ""
        except ValueError:
            e = ""
    if e.startswith("*"):
        e = e[1:]
    e = e.strip(".")
    if not e or any(c.isspace() or c in "/?#@" for c in e):
        raise ConfigError("Invalid ORIGIN_ALLOWLIST entry: %r" % entry)
    return e


def parse_allowlist(raw):
    entries = [p for p in raw.split(",") if p.strip() != ""]
    if not entries:
        return None
    return [normalise_suffix(p) for p in entries]


def load_config(environ=None):
    environ = os.environ if environ is None else environ
    values = {}
    path = environ.get("DATAGATE_CONFIG")
    if path is not None and path.strip() != "":
        for k, v in read_config_file(path).items():
            values[k] = v
    for k in CONFIG_KEYS:
        if k in environ:
            values[k] = environ[k]
    cfg = {"MAX_SOURCE_SIZE": None, "ORIGIN_ALLOWLIST": None, "REQUIRE_TLS": False,
           "STORAGE_DIR": DEFAULT_STORAGE_DIR, "CACHE_ENABLED": True}
    try:
        if "MAX_SOURCE_SIZE" in values:
            cfg["MAX_SOURCE_SIZE"] = parse_size(values["MAX_SOURCE_SIZE"])
        if "ORIGIN_ALLOWLIST" in values:
            cfg["ORIGIN_ALLOWLIST"] = parse_allowlist(values["ORIGIN_ALLOWLIST"])
        for k in ("REQUIRE_TLS", "CACHE_ENABLED"):
            if k in values:
                cfg[k] = parse_bool_env(k, values[k], cfg[k])
    except ValueError as e:
        raise ConfigError(str(e))
    if "STORAGE_DIR" in values and values["STORAGE_DIR"].strip() != "":
        cfg["STORAGE_DIR"] = os.path.expanduser(values["STORAGE_DIR"].strip())
    return cfg


def apply_config(cfg):
    global MAX_SOURCE_SIZE, ORIGIN_ALLOWLIST, REQUIRE_TLS, STORAGE_DIR, CACHE_ENABLED
    MAX_SOURCE_SIZE = cfg["MAX_SOURCE_SIZE"]
    ORIGIN_ALLOWLIST = cfg["ORIGIN_ALLOWLIST"]
    REQUIRE_TLS = cfg["REQUIRE_TLS"]
    CACHE_ENABLED = cfg["CACHE_ENABLED"]
    path = os.path.abspath(cfg["STORAGE_DIR"])
    try:
        os.makedirs(path, exist_ok=True)
        if not os.path.isdir(path) or not os.access(path, os.W_OK | os.X_OK):
            raise OSError("not a writable directory")
    except OSError as e:
        raise ConfigError("Cannot use STORAGE_DIR %r: %s" % (path, e))
    STORAGE_DIR = path


def origin_allowed(referer):
    if not referer or not referer.strip():
        return False
    try:
        host = urllib.parse.urlsplit(referer.strip()).hostname
    except ValueError:
        host = None
    if not host:
        return False
    host = host.lower().rstrip(".")
    for suffix in ORIGIN_ALLOWLIST:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def check_size(n):
    if MAX_SOURCE_SIZE is not None and n > MAX_SOURCE_SIZE:
        raise ApiError(400, "Source size %d bytes exceeds maximum of %d bytes" % (n, MAX_SOURCE_SIZE))


# ---------------------------------------------------------------- storage

ID_RE = re.compile(r"[0-9a-f]{16}")


def dataset_path(did):
    if STORAGE_DIR is None or not ID_RE.fullmatch(did):
        return None
    return os.path.join(STORAGE_DIR, did + ".json")


def store_dataset(did, ds):
    with LOCK:
        DATASETS[did] = ds
    path = dataset_path(did)
    if path is None:
        return
    body = json.dumps(ds, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=STORAGE_DIR, prefix="." + did, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_dataset(did):
    with LOCK:
        ds = DATASETS.get(did)
    if ds is not None:
        return ds
    path = dataset_path(did)
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            ds = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(ds, dict) or "columns" not in ds or "rows" not in ds:
        return None
    with LOCK:
        DATASETS.setdefault(did, ds)
        return DATASETS[did]


def has_dataset(did):
    return get_dataset(did) is not None


def ingest_lock(key):
    with LOCK:
        lk = INGEST_LOCKS.get(key)
        if lk is None:
            lk = INGEST_LOCKS[key] = threading.Lock()
        return lk


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
            cap = MAX_BYTES if MAX_SOURCE_SIZE is None else min(MAX_BYTES, MAX_SOURCE_SIZE)
            data = resp.read(cap + 1)
    except urllib.error.HTTPError as e:
        raise ApiError(404, "Remote source returned HTTP %d" % e.code)
    except Exception as e:  # URLError, timeouts, connection errors, ...
        raise ApiError(404, "Source unreachable: %s" % (getattr(e, "reason", None) or e))
    check_size(len(data))
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


XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"


def detect_format(data):
    if data.startswith(XLS_MAGIC):
        return "xls"
    if data.startswith(ZIP_MAGIC) or data.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except Exception:
            raise ApiError(400, "Unrecognized or corrupt file format")
        if any(n.startswith("xl/") for n in names) and "[Content_Types].xml" in names:
            return "xlsx"
        raise ApiError(400, "Unrecognized file format")
    return "csv"


def cell_value(v):
    """Normalise a spreadsheet cell to (typed value, raw text)."""
    if v is None:
        return "", ""
    if isinstance(v, bool):
        return v, "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return v, str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return "", ""
        if v.is_integer() and abs(v) < 2**53:
            return int(v), str(int(v))
        return v, repr(v)
    if isinstance(v, datetime.datetime):
        if v.time() == datetime.time(0):
            t = v.date().isoformat()
        else:
            t = v.isoformat()
        return t, t
    if isinstance(v, (datetime.date, datetime.time)):
        t = v.isoformat()
        return t, t
    if isinstance(v, datetime.timedelta):
        t = str(v)
        return t, t
    t = str(v)
    return convert_value(t), t


def table_from_cells(cells):
    """cells: iterable of row sequences of python values (first sheet)."""
    rows = []
    for r in cells:
        conv = [cell_value(v) for v in r]
        if not conv or all(raw.strip() == "" for _, raw in conv):
            continue
        rows.append(conv)
    if len(rows) < 2:
        raise ApiError(400, "First worksheet must contain a header row and at least one data row")
    width = 0
    for r in rows:
        for i in range(len(r) - 1, -1, -1):
            if r[i][1].strip() != "":
                width = max(width, i + 1)
                break
    if width == 0:
        raise ApiError(400, "First worksheet is not tabular")
    rows = [(r + [("", "")] * width)[:width] for r in rows]
    header = [raw.strip() for _, raw in rows[0]]
    if all(h == "" for h in header):
        raise ApiError(400, "First worksheet has no header row")
    data = [[v for v, _ in r] for r in rows[1:]]
    raw = [[t for _, t in r] for r in rows[1:]]
    return header, data, raw


def parse_xlsx(data):
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        raise ApiError(400, "XLSX support unavailable")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ApiError(400, "Could not read XLSX workbook")
    try:
        if not wb.worksheets:
            raise ApiError(400, "Workbook has no worksheets")
        ws = wb.worksheets[0]
        try:
            ws.reset_dimensions()
        except Exception:
            pass
        try:
            cells = [list(r) for r in ws.iter_rows(values_only=True)]
        except ApiError:
            raise
        except Exception:
            raise ApiError(400, "Could not read XLSX worksheet")
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return table_from_cells(cells)


def parse_xls(data):
    try:
        import xlrd
    except ImportError:  # pragma: no cover
        raise ApiError(400, "XLS support unavailable")
    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
        if book.nsheets < 1:
            raise ApiError(400, "Workbook has no worksheets")
        sheet = book.sheet_by_index(0)
        cells = []
        for ri in range(sheet.nrows):
            row = []
            for c in sheet.row(ri):
                t = c.ctype
                if t == xlrd.XL_CELL_NUMBER:
                    row.append(float(c.value))
                elif t == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate.xldate_as_datetime(c.value, book.datemode))
                    except Exception:
                        row.append(float(c.value))
                elif t == xlrd.XL_CELL_BOOLEAN:
                    row.append(bool(c.value))
                elif t == xlrd.XL_CELL_TEXT:
                    row.append(c.value)
                else:  # empty, blank, error
                    row.append(None)
            cells.append(row)
    except ApiError:
        raise
    except Exception:
        raise ApiError(400, "Could not read XLS workbook")
    return table_from_cells(cells)


def ingest(data, charset, with_format=False):
    fmt = detect_format(data)
    if fmt == "xlsx":
        result = parse_xlsx(data)
    elif fmt == "xls":
        result = parse_xls(data)
    else:
        result = parse_csv(decode(data, charset))
    if with_format:
        return fmt, result
    return result


def is_missing(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def column_type(values):
    """Classify a column from its (typed) non-missing values."""
    has_int = has_float = False
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "text"
        if isinstance(v, int):
            has_int = True
        else:
            has_float = True
    if has_int and has_float:
        return "number"
    if has_float:
        return "float"
    if has_int:
        return "integer"
    return "text"


def compute_enrichment(fmt, columns, rows, raw):
    """Return the enrichment metadata dict for a freshly ingested dataset."""
    try:
        summary = {
            "filetype": "csv" if fmt == "csv" else "excel",
            "row_count": len(rows),
            "column_count": len(columns),
        }
        meta = {"dataset_summary": summary}
        if fmt == "csv":
            details = {}
            for ci, name in enumerate(columns):
                if name in details:
                    continue  # duplicate header: keep first
                typed, distinct, missing = [], set(), 0
                for ri, row in enumerate(rows):
                    v = row[ci] if ci < len(row) else None
                    rv = raw[ri][ci] if ci < len(raw[ri]) else None
                    if is_missing(v) or is_missing(rv):
                        missing += 1
                        continue
                    typed.append(v)
                    distinct.add(rv)
                details[name] = {
                    "type": column_type(typed),
                    "distinct_count": len(distinct),
                    "missing_count": missing,
                }
            meta["column_details"] = details
        json.dumps(meta)  # must be serialisable for storage
        return meta
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(500, "Enrichment failed: %s" % e)


def parse_multipart(content_type, body):
    """Return dict name -> list of bytes values for a multipart body."""
    msg_bytes = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    try:
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(msg_bytes)
    except Exception:
        raise ApiError(400, "Malformed multipart body")
    if not msg.is_multipart() or msg.defects:
        raise ApiError(400, "Malformed multipart body")
    fields = {}
    parts = msg.get_payload()
    if not isinstance(parts, list) or not parts:
        raise ApiError(400, "Malformed multipart body")
    for part in parts:
        if part.defects and any(not isinstance(d, email.errors.InvalidHeaderDefect) for d in part.defects):
            raise ApiError(400, "Malformed multipart body")
        disp = part.get("Content-Disposition")
        if disp is None:
            continue
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        if isinstance(name, tuple):
            name = email.utils.collapse_rfc2231_value(name)
        payload = part.get_payload(decode=True)
        if payload is None:
            p = part.get_payload()
            payload = p.encode("utf-8") if isinstance(p, str) else b""
        fields.setdefault(name, []).append(payload)
    return fields


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
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def error(self, status, message):
        self.send_json(status, {"ok": False, "error": message})

    def gate(self):
        """Origin allowlist check; returns True if the request may proceed."""
        if ORIGIN_ALLOWLIST is None:
            return True
        referer = self.headers.get("Referer")
        if referer is None or not referer.strip():
            msg = "Missing Referer header"
        elif not origin_allowed(referer):
            msg = "Referer origin not allowed"
        else:
            return True
        self.discard_body()
        self.error(403, msg)
        return False

    def endpoint(self, did):
        rel = "/datasets/%s" % did
        if not REQUIRE_TLS:
            return rel
        host = (self.headers.get("Host") or "").strip()
        if not host:
            addr = self.server.server_address
            host = "%s:%d" % (addr[0], addr[1])
        return "https://%s%s" % (host, rel)

    def do_OPTIONS(self):
        if not self.gate():
            return
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
        if path == "/upload":
            return "upload", None, params
        m = re.fullmatch(r"/datasets/([^/]+)/export", path)
        if m:
            return "export", urllib.parse.unquote(m.group(1)), params
        m = re.fullmatch(r"/datasets/([^/]+)", path)
        if m:
            return "dataset", urllib.parse.unquote(m.group(1)), params
        return None, None, params

    def do_GET(self):
        if not self.gate():
            return
        try:
            kind, arg, params = self.route()
            if kind == "convert":
                self.handle_convert(params)
            elif kind == "dataset":
                self.handle_dataset(arg, params)
            elif kind == "export":
                self.handle_export(arg, params)
            elif kind == "upload":
                self.error(405, "Method not allowed")
            else:
                self.error(404, "Not found")
        except ApiError as e:
            self.error(e.status, e.message)
        except Exception as e:  # pragma: no cover
            self.error(500, "Internal error: %s" % e)

    do_HEAD = do_GET

    def _other(self):
        if not self.gate():
            return
        self.discard_body()
        kind, _, _ = self.route()
        if kind is None:
            self.error(404, "Not found")
        else:
            self.error(405, "Method not allowed")

    do_PUT = do_DELETE = do_PATCH = _other

    def do_POST(self):
        if not self.gate():
            return
        try:
            kind, _, params = self.route()
            if kind == "upload":
                self.handle_upload(params)
            else:
                self.discard_body()
                self.error(404 if kind is None else 405,
                           "Not found" if kind is None else "Method not allowed")
        except ApiError as e:
            self.error(e.status, e.message)
        except Exception as e:  # pragma: no cover
            self.error(500, "Internal error: %s" % e)

    def read_body(self):
        if getattr(self, "_body_read", False):
            return b""
        self._body_read = True
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    raise ApiError(400, "Malformed chunked body")
                try:
                    n = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    self.close_connection = True
                    raise ApiError(400, "Malformed chunked body")
                if n == 0:
                    while True:
                        t = self.rfile.readline(65537)
                        if not t or t in (b"\r\n", b"\n"):
                            break
                    break
                total += n
                if total > MAX_BYTES:
                    self.close_connection = True
                    raise ApiError(400, "Upload too large")
                chunks.append(self.rfile.read(n))
                self.rfile.readline(3)
            return b"".join(chunks)
        cl = self.headers.get("Content-Length")
        if not cl:
            return b""
        try:
            n = int(cl)
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "Invalid Content-Length")
        if n < 0:
            self.close_connection = True
            raise ApiError(400, "Invalid Content-Length")
        if n > MAX_BYTES:
            self.close_connection = True
            raise ApiError(400, "Upload too large")
        return self.rfile.read(n)

    def discard_body(self):
        try:
            self.read_body()
        except ApiError:
            self.close_connection = True

    def handle_upload(self, params):
        ctype = self.headers.get("Content-Type") or ""
        main = ctype.split(";", 1)[0].strip().lower()
        if not main.startswith("multipart/"):
            self.discard_body()
            raise ApiError(415, "Upload must be multipart/form-data")
        body = self.read_body()
        if not re.search(r"(?i);\s*boundary\s*=", ctype):
            raise ApiError(400, "Malformed multipart body: missing boundary")
        fields = parse_multipart(ctype, body)
        files = fields.get("file") or fields.get("attachment")
        if not files:
            raise ApiError(400, "Missing file field ('file' or 'attachment')")
        data = files[0]
        check_size(len(data))
        charset = params.get("charset", [None])[0]
        if charset is None and fields.get("charset"):
            charset = fields["charset"][0].decode("utf-8", "replace")
        charset = validate_charset(charset) if charset is not None else None
        columns, rows, raw = ingest(data, charset)
        did = hashlib.sha256(data).hexdigest()[:16]
        store_dataset(did, {"columns": columns, "rows": rows, "raw": raw})
        self.send_json(200, {"ok": True, "endpoint": self.endpoint(did)})

    def handle_convert(self, params):
        sources = params.get("source")
        if not sources or sources[0] == "":
            raise ApiError(400, "Missing required parameter: source")
        source = sources[0]
        validate_url(source)
        forces = params.get("force")
        if forces is not None and len(forces) > 1:
            raise ApiError(400, "Parameter force may only be given once")
        force = forces is not None
        charset = params.get("charset", [None])[0]
        charset = validate_charset(charset) if charset is not None else None
        # only a single, exact `enrich=yes` turns enrichment on
        enrich = params.get("enrich") == ["yes"]
        did = dataset_id(source)
        key = (source, charset)
        response = {"ok": True, "endpoint": self.endpoint(did)}
        use_cache = CACHE_ENABLED and not force

        def cache_hit():
            with LOCK:
                if CACHE.get(key) != did:
                    return False
                ds = DATASETS.get(did)
                if ds is None:
                    return False
                # a non-enriched cached dataset cannot satisfy an enrich request
                return not enrich or "dataset_summary" in ds

        if use_cache and cache_hit():
            return self.send_json(200, response)
        with ingest_lock(key):
            if use_cache and cache_hit():
                # another request may have populated the cache meanwhile
                return self.send_json(200, response)
            data = fetch(source)
            fmt, (columns, rows, raw) = ingest(data, charset, with_format=True)
            ds = {"columns": columns, "rows": rows, "raw": raw}
            if enrich:
                ds.update(compute_enrichment(fmt, columns, rows, raw))
            store_dataset(did, ds)
            with LOCK:
                # the dataset id is per-source; drop entries for other charsets
                for k in [k for k, v in CACHE.items() if v == did]:
                    del CACHE[k]
                if CACHE_ENABLED:
                    CACHE[key] = did
        self.send_json(200, response)

    def query(self, did, params, start):
        ds = get_dataset(did)
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
        return ds, columns, opts, total, page

    def handle_export(self, did, params):
        start = time.perf_counter()
        # presentation-only controls do not affect CSV output
        params = {k: v for k, v in params.items() if k not in ("_shape", "_rowid", "_total")}
        ds, columns, opts, total, page = self.query(did, params, start)
        raws = ds.get("raw") or ds["rows"]
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(columns)
        n = len(columns)
        for rowid, _ in page:
            r = raws[rowid - 1]
            out = [(c if isinstance(c, str) else ("" if c is None else display(c))) for c in r[:n]]
            out += [""] * (n - len(out))
            w.writerow(out)
        body = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", 'attachment; filename="%s.csv"' % did)
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_dataset(self, did, params):
        start = time.perf_counter()
        ds, columns, opts, total, page = self.query(did, params, start)
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
        if isinstance(ds.get("dataset_summary"), dict):
            payload["dataset_summary"] = ds["dataset_summary"]
            if isinstance(ds.get("column_details"), dict):
                payload["column_details"] = ds["column_details"]
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
        try:
            apply_config(load_config())
        except ConfigError as e:
            print("datagate: %s" % e, file=sys.stderr, flush=True)
            sys.exit(2)
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
