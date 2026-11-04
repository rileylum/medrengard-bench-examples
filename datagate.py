#!/usr/bin/env python3
"""datagate: fetch remote CSV files and serve them as JSON datasets."""
import argparse
import codecs
import csv
import hashlib
import io
import json
import os
import sys
import re
import threading
import datetime
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except Exception:  # pragma: no cover
    _cn_from_bytes = None

try:
    import openpyxl as _openpyxl
except Exception:  # pragma: no cover
    _openpyxl = None

try:
    import xlrd as _xlrd
except Exception:  # pragma: no cover
    _xlrd = None

ROW_LIMIT = 100
FETCH_TIMEOUT = 15
MAX_BYTES = 200 * 1024 * 1024

DATASETS = {}
LOCK = threading.Lock()

CACHE_ENABLED = True
CACHE = {}  # source URL -> (dataset id, effective encoding)
_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")


def parse_bool_env(value):
    v = value.lower()
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    raise ValueError("invalid boolean value: %r" % value)

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
    raw = []
    for r in data_rows:
        if len(r) < n:
            r = r + [""] * (n - len(r))
        elif len(r) > n:
            r = r[:n]
        raw.append(list(r))
        out.append([convert_value(c) for c in r])
    return header, out, raw


OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _cell_to_value(v):
    """Normalize a spreadsheet cell value -> (json value, raw string)."""
    if v is None:
        return "", ""
    if isinstance(v, bool):
        return v, "true" if v else "false"
    if isinstance(v, int):
        return v, str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return str(v), str(v)
        if v.is_integer() and abs(v) < 2 ** 53:
            return int(v), str(int(v))
        return v, repr(v)
    if isinstance(v, datetime.datetime):
        if v.time() == datetime.time(0, 0) and v.tzinfo is None:
            s = v.date().isoformat()
        else:
            s = v.isoformat()
        return s, s
    if isinstance(v, (datetime.date, datetime.time)):
        s = v.isoformat()
        return s, s
    if isinstance(v, datetime.timedelta):
        s = str(v)
        return s, s
    s = str(v)
    return s, s


def _table_from_cells(cell_rows):
    rows = []
    for r in cell_rows:
        r = list(r)
        if any(not (c is None or (isinstance(c, str) and not c.strip())) for c in r):
            rows.append(r)
    if len(rows) < 2:
        raise ApiError(400, "Content is not tabular: first sheet needs a header row and at least one data row")
    header_cells = rows[0]
    width = 0
    for i, c in enumerate(header_cells):
        if not (c is None or (isinstance(c, str) and not c.strip())):
            width = i + 1
    for r in rows[1:]:
        for i in range(len(r) - 1, -1, -1):
            c = r[i]
            if not (c is None or (isinstance(c, str) and not c.strip())):
                width = max(width, i + 1)
                break
    if width == 0:
        raise ApiError(400, "Content is not tabular: empty header")
    header = []
    for i in range(width):
        c = header_cells[i] if i < len(header_cells) else None
        header.append(_cell_to_value(c)[1].strip())
    out, raw = [], []
    for r in rows[1:]:
        vals, raws = [], []
        for i in range(width):
            v, s = _cell_to_value(r[i] if i < len(r) else None)
            vals.append(v)
            raws.append(s)
        out.append(vals)
        raw.append(raws)
    return header, out, raw


def parse_xlsx(data):
    if _openpyxl is None:
        raise ApiError(400, "Unsupported format: xlsx support unavailable")
    try:
        wb = _openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ApiError(400, "Unsupported or unrecognized file format")
    try:
        if not wb.worksheets:
            raise ApiError(400, "Content is not tabular: workbook has no worksheets")
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
            raise ApiError(400, "Content is not tabular: unreadable worksheet")
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return _table_from_cells(cells)


def parse_xls(data):
    if _xlrd is None:
        raise ApiError(400, "Unsupported format: xls support unavailable")
    try:
        book = _xlrd.open_workbook(file_contents=data, on_demand=True)
    except Exception:
        raise ApiError(400, "Unsupported or unrecognized file format")
    try:
        if book.nsheets < 1:
            raise ApiError(400, "Content is not tabular: workbook has no worksheets")
        try:
            sh = book.sheet_by_index(0)
        except Exception:
            raise ApiError(400, "Content is not tabular: unreadable worksheet")
        cells = []
        for ri in range(sh.nrows):
            row = []
            for ci in range(sh.ncols):
                c = sh.cell(ri, ci)
                t = c.ctype
                if t in (_xlrd.XL_CELL_EMPTY, _xlrd.XL_CELL_BLANK):
                    row.append(None)
                elif t == _xlrd.XL_CELL_BOOLEAN:
                    row.append(bool(c.value))
                elif t == _xlrd.XL_CELL_ERROR:
                    row.append(_xlrd.error_text_from_code.get(c.value, "#ERR"))
                elif t == _xlrd.XL_CELL_DATE:
                    try:
                        row.append(_xlrd.xldate_as_datetime(c.value, book.datemode))
                    except Exception:
                        row.append(c.value)
                else:
                    row.append(c.value)
            cells.append(row)
    finally:
        try:
            book.release_resources()
        except Exception:
            pass
    return _table_from_cells(cells)


def detect_format(data, filename=None, ctype=None):
    if data.startswith(OLE2_MAGIC):
        return "xls"
    if data.startswith(ZIP_MAGICS):
        return "xlsx"
    return "csv"


def parse_source(data, enc=None, charset=None, filename=None, ctype=None):
    fmt = detect_format(data, filename, ctype)
    if fmt == "xls":
        return parse_xls(data)
    if fmt == "xlsx":
        return parse_xlsx(data)
    if enc is not None:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            raise ApiError(400, "Content cannot be decoded with charset %s" % charset)
    else:
        text = detect_and_decode(data)
    return parse_csv(text)


def store_dataset(did, columns, rows, raw, source):
    with LOCK:
        DATASETS[did] = {"columns": columns, "rows": rows, "raw": raw, "source": source}
    return 200, {"ok": True, "endpoint": "/datasets/%s" % did}


def handle_convert(params, multi=None):
    multi = multi or {}
    force_vals = multi.get("force", [])
    if len(force_vals) > 1:
        raise ApiError(400, "Parameter force may only be specified once")
    force = len(force_vals) == 1
    source = params.get("source")
    if source is None or source == "":
        raise ApiError(400, "Missing required parameter: source")
    validate_url(source)
    charset = params.get("charset")
    enc = None
    if charset is not None:
        enc = validate_charset(charset.strip())
    did = dataset_id(source)
    if CACHE_ENABLED and not force:
        with LOCK:
            entry = CACHE.get(source)
            hit = (entry is not None and entry == (did, enc)
                   and did in DATASETS and DATASETS[did].get("source") == source)
        if hit:
            return 200, {"ok": True, "endpoint": "/datasets/%s" % did}
    data, ctype = fetch(source)
    if len(data) > MAX_BYTES:
        raise ApiError(400, "Source too large")
    columns, rows, raw = parse_source(data, enc, charset, ctype=ctype)
    res = store_dataset(did, columns, rows, raw, source)
    with LOCK:
        CACHE[source] = (did, enc)
    return res


def _parse_header_params(value):
    """Parse 'type/sub; a=b; c="d"' -> (type lowercased, {param: value})."""
    parts = []
    cur, q, esc = [], False, False
    for ch in value:
        if esc:
            cur.append(ch)
            esc = False
        elif q and ch == "\\":
            esc = True
        elif ch == '"':
            q = not q
            cur.append(ch)
        elif ch == ";" and not q:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    main = parts[0].strip().lower()
    params = {}
    for p in parts[1:]:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        params[k] = v
    return main, params


def parse_multipart(body, boundary):
    """Return list of (name, filename, headers, data). Raise ApiError(400) on malformed input."""
    if not boundary or len(boundary) > 200:
        raise ApiError(400, "Malformed multipart body: missing boundary")
    b = boundary.encode("latin-1", "replace")
    delim = b"--" + b
    # locate first delimiter (must be at start or after CRLF/LF)
    if body.startswith(delim):
        pos = 0
    else:
        idx = body.find(b"\r\n" + delim)
        if idx < 0:
            idx = body.find(b"\n" + delim)
            if idx < 0:
                raise ApiError(400, "Malformed multipart body")
            pos = idx + 1
        else:
            pos = idx + 2
    parts = []
    closed = False
    while True:
        pos += len(delim)
        if body[pos:pos + 2] == b"--":
            closed = True
            break
        # skip transport padding and line break
        eol = body.find(b"\n", pos)
        if eol < 0:
            raise ApiError(400, "Malformed multipart body")
        if body[pos:eol].strip(b" \t\r"):
            raise ApiError(400, "Malformed multipart body")
        start = eol + 1
        # find next delimiter (preceded by LF, optionally CRLF)
        nxt = body.find(b"\n" + delim, start - 1 if start > 0 else 0)
        while nxt >= 0 and nxt + 1 < start:
            nxt = body.find(b"\n" + delim, nxt + 1)
        if nxt < 0:
            raise ApiError(400, "Malformed multipart body: missing closing boundary")
        end = nxt - 1 if nxt > start and body[nxt - 1:nxt] == b"\r" else nxt
        part = body[start:max(start, end)]
        sep_len = 1
        # split headers/body at first blank line
        m = re.search(rb"(?:^|\n)\r?\n", part)
        if m is None:
            raise ApiError(400, "Malformed multipart body: bad part headers")
        hend, hlen = m.start(), m.end() - m.start()
        raw_headers = part[:hend].decode("utf-8", "replace")
        data = part[hend + hlen:]
        headers = {}
        for line in re.split(r"\r?\n", raw_headers):
            if not line.strip():
                continue
            if ":" not in line:
                raise ApiError(400, "Malformed multipart body: bad part header")
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        cd = headers.get("content-disposition")
        if cd is None:
            raise ApiError(400, "Malformed multipart body: missing Content-Disposition")
        kind, cdp = _parse_header_params(cd)
        if kind != "form-data":
            raise ApiError(400, "Malformed multipart body: bad Content-Disposition")
        name = cdp.get("name")
        filename = cdp.get("filename")
        if "filename*" in cdp and filename is None:
            fv = cdp["filename*"]
            if "''" in fv:
                fv = urllib.parse.unquote(fv.split("''", 1)[1])
            filename = fv
        parts.append((name, filename, headers, data))
        pos = nxt + sep_len
    if not closed:
        raise ApiError(400, "Malformed multipart body")
    return parts


def handle_upload(content_type, body, query):
    ctype, cparams = _parse_header_params(content_type or "")
    if ctype != "multipart/form-data":
        raise ApiError(415, "Unsupported media type: expected multipart/form-data")
    boundary = cparams.get("boundary")
    if not boundary:
        raise ApiError(400, "Malformed multipart body: missing boundary")
    parts = parse_multipart(body, boundary)
    fields = {}
    for name, filename, headers, data in parts:
        if name is None:
            continue
        fields.setdefault(name, (filename, headers, data))
    chosen = fields.get("file") or fields.get("attachment")
    if chosen is None:
        raise ApiError(400, "Missing file field: expected 'file' or 'attachment'")
    filename, headers, data = chosen
    charset = query.get("charset")
    if charset is None and "charset" in fields and fields["charset"][0] is None:
        charset = fields["charset"][2].decode("utf-8", "replace")
    enc = None
    if charset is not None:
        enc = validate_charset(charset.strip())
    if len(data) > MAX_BYTES:
        raise ApiError(400, "Source too large")
    columns, rows, raw = parse_source(data, enc, charset, filename=filename,
                                      ctype=headers.get("content-type"))
    did = hashlib.sha256(b"upload:" + data).hexdigest()[:16]
    return store_dataset(did, columns, rows, raw, "upload:%s" % (filename or ""))


COMPARATORS = ("exact", "contains", "less", "greater")
QUERY_TIMEOUT = 5.0


def _parse_number(s):
    if not isinstance(s, str):
        s = str(s)
    try:
        f = float(s)
    except (ValueError, OverflowError):
        return None
    if f != f:
        return None
    return f


def parse_filters(multi, columns):
    """Return list of (column_index, comparator, value) from query params."""
    filters = []
    for key, values in multi.items():
        if key.startswith("_") or "__" not in key:
            continue
        col, comp = key.rsplit("__", 1)
        if comp not in COMPARATORS:
            raise ApiError(400, "Invalid comparator '%s' for parameter %s; expected one of: %s"
                           % (comp, key, ", ".join(COMPARATORS)))
        if col not in columns:
            raise ApiError(400, "Unknown filter column: %s" % col)
        if len(values) > 1:
            raise ApiError(400, "Duplicate filter parameter: %s" % key)
        value = values[0]
        if comp in ("less", "greater"):
            num = _parse_number(value)
            if num is None:
                raise ApiError(400, "Filter %s requires a numeric value, got '%s'" % (key, value))
            value = num
        filters.append((columns.index(col), comp, value))
    return filters


def _match(cell, comp, value):
    if cell is None:
        return False
    if comp == "exact":
        return cell == value
    if comp == "contains":
        return value in cell
    num = _parse_number(cell)
    if num is None:
        return False
    if comp == "less":
        return num < value
    return num > value


def apply_filters(indexed, raw_rows, filters, deadline):
    if not filters:
        return indexed
    out = []
    for n, (rowid, row) in enumerate(indexed):
        if n % 1024 == 0 and time.perf_counter() > deadline:
            raise ApiError(400, "Query timeout")
        raw = raw_rows[rowid - 1]
        ok = True
        for ci, comp, value in filters:
            cell = raw[ci] if ci < len(raw) else None
            if not _match(cell, comp, value):
                ok = False
                break
        if ok:
            out.append((rowid, row))
    if time.perf_counter() > deadline:
        raise ApiError(400, "Query timeout")
    return out


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


def query_dataset(did, multi=None, for_export=False):
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
    if for_export:
        for k in ("_shape", "_rowid", "_total"):
            ctl.pop(k, None)

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

    filters = parse_filters(multi, columns)
    indexed = list(enumerate(ds["rows"], start=1))
    raw_rows = ds.get("raw")
    if raw_rows is None:
        raw_rows = [["" if v is None else str(v) for v in r] for r in ds["rows"]]
    indexed = apply_filters(indexed, raw_rows, filters, start + QUERY_TIMEOUT)
    if sort_col is not None:
        ci = columns.index(sort_col)
        indexed.sort(key=lambda t: _sort_key(t[1][ci] if ci < len(t[1]) else None),
                     reverse=desc)
    total = len(indexed)
    page = indexed[offset:offset + size]
    return start, ds, columns, ctl, shape, total, page, raw_rows


def handle_export(did, multi=None):
    _start, ds, columns, _ctl, _shape, _total, page, raw_rows = query_dataset(did, multi, True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    n = len(columns)
    for rowid, _r in page:
        raw = list(raw_rows[rowid - 1])
        if len(raw) < n:
            raw += [""] * (n - len(raw))
        w.writerow(raw[:n])
    return buf.getvalue().encode("utf-8")


def handle_dataset(did, multi=None):
    start, ds, columns, ctl, shape, total, page, raw_rows = query_dataset(did, multi)

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
            return handle_convert(params, multi)
        if path == "/upload":
            raise ApiError(405, "Method not allowed")
        m = re.match(r"^/datasets/([^/]+)/export$", path)
        if m:
            did = urllib.parse.unquote(m.group(1))
            return "csv", handle_export(did, multi), did
        m = re.match(r"^/datasets/([^/]+)$", path)
        if m:
            return handle_dataset(urllib.parse.unquote(m.group(1)), multi)
        raise ApiError(404, "Not found")

    def _handle(self, body=True):
        try:
            res = self._route()
            if res and res[0] == "csv":
                _, data, did = res
                safe = re.sub(r'[^A-Za-z0-9._-]', "_", did)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="%s.csv"' % safe)
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                if body:
                    self.wfile.write(data)
                return
            status, obj = res
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

    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    raise ApiError(400, "Malformed chunked body")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise ApiError(400, "Malformed chunked body")
                if size == 0:
                    while True:
                        l = self.rfile.readline(65537)
                        if not l or l in (b"\r\n", b"\n"):
                            break
                    break
                total += size
                if total > MAX_BYTES:
                    self.close_connection = True
                    raise ApiError(413, "Upload too large")
                chunks.append(self.rfile.read(size))
                self.rfile.readline(3)
            return b"".join(chunks)
        cl = self.headers.get("Content-Length")
        if cl is None:
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
            raise ApiError(413, "Upload too large")
        return self.rfile.read(n)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if len(path) > 1:
            path = path.rstrip("/")
        if path != "/upload":
            return self._not_allowed()
        try:
            try:
                body = self._read_body()
            except ApiError:
                raise
            except Exception:
                self.close_connection = True
                raise ApiError(400, "Could not read request body")
            query = {}
            for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                query.setdefault(k, v)
            status, obj = handle_upload(self.headers.get("Content-Type"), body, query)
        except ApiError as e:
            status, obj = e.status, {"ok": False, "error": e.message}
        except Exception as e:  # pragma: no cover
            status, obj = 500, {"ok": False, "error": "Internal error: %s" % e}
        self._send(status, obj)

    def _not_allowed(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path in ("/convert", "/upload") or re.match(r"^/datasets/[^/]+(/export)?$", path):
            self._send(405, {"ok": False, "error": "Method not allowed"})
        else:
            self._send(404, {"ok": False, "error": "Not found"})

    do_PUT = do_DELETE = do_PATCH = _not_allowed


def main():
    ap = argparse.ArgumentParser(prog="datagate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("--port", type=int, default=8001)
    st.add_argument("--address", default="127.0.0.1")
    args = ap.parse_args()
    global CACHE_ENABLED
    raw_cache = os.environ.get("CACHE_ENABLED")
    if raw_cache is not None:
        try:
            CACHE_ENABLED = parse_bool_env(raw_cache)
        except ValueError:
            sys.stderr.write("datagate: invalid CACHE_ENABLED value %r "
                             "(expected 1/true/yes/on or 0/false/no/off)\n" % raw_cache)
            sys.exit(2)
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
