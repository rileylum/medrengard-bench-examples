#!/usr/bin/env python3
"""dynamic_buffer.py - infer a row-wise transformation from a sample
input/output pair and generate a streaming ``DynamicPreprocessor`` module
(Python package or Node.js module) that applies it.

Usage:
    python dynamic_buffer.py <module_name> --output <dir> --sample <dir> \
        (--python | --javascript)
"""

import argparse
import csv
import json
import math
import os
import sys

SUPPORTED_EXTS = ("csv", "tsv", "jsonl", "json")

# ---------------------------------------------------------------------------
# Runtime (Python).  This code is embedded verbatim in the generated package
# and is also exec'd by the generator so inference uses identical semantics.
# ---------------------------------------------------------------------------

RT_PY_CORE = r'''
import csv as _csv
import hashlib as _hashlib
import json as _json
import os as _os
import re as _re

_INT_RE = _re.compile(r'\s*[+-]?[0-9]+\s*\Z')
_FLT_RE = _re.compile(r'\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*\Z')


def _num(v):
    """Interpret a value as a number (int/float) or return None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        if _INT_RE.match(v):
            return int(v.strip())
        if _FLT_RE.match(v):
            return float(v.strip())
    return None


def _str(v):
    """Interpret a value as a string or return None."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _eq(a, b):
    """Type-aware equality (bool != number, None only equals None)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None:
        return a is None and b is None
    an = isinstance(a, (int, float))
    bn = isinstance(b, (int, float))
    if an or bn:
        return an and bn and a == b
    return a == b


def _sop(s, op):
    if op == 'strip':
        return s.strip()
    if op == 'lower':
        return s.lower()
    if op == 'upper':
        return s.upper()
    return s


def _sops(v, ops):
    s = _str(v)
    if s is None:
        return None
    for op in ops:
        s = _sop(s, op)
    return s


def _round(y, d):
    if d is None:
        return y
    return round(y, d)


def _fmt(y, fmt):
    k = fmt[0]
    if k == 'raw':
        return y
    if k == 'int':
        return int(round(y))
    if k == 'float':
        return float(y)
    if k == 'intify':
        y = float(y)
        if y.is_integer() and abs(y) < 1e16:
            return int(y)
        return y
    if k == 's_g':
        if isinstance(y, float) and y.is_integer() and abs(y) < 1e16:
            return str(int(y))
        return repr(y) if isinstance(y, float) else str(y)
    if k == 's_int':
        return str(int(round(y)))
    if k == 's_fixed':
        return format(y, '.%df' % fmt[1])
    if k == 's_repr':
        return repr(float(y))
    return y


def _apply(t, row):
    k = t['type']
    if k == 'copy':
        v = row.get(t['src'])
        if not t['ops']:
            return v
        return _sops(v, t['ops'])
    if k == 'const':
        return t['value']
    if k == 'linear':
        v = row.get(t['src'])
        x = _num(v)
        if x is None:
            return v
        try:
            y = t['a'] * x + t['b']
            y = _round(y, t['round'])
            return _fmt(y, t['fmt'])
        except (OverflowError, ValueError):
            return v
    if k == 'affix':
        s = _sops(row.get(t['src']), t['ops'])
        if s is None:
            return None
        return t['prefix'] + s + t['suffix']
    if k == 'split':
        s = _sops(row.get(t['src']), t['ops'])
        if s is None:
            return None
        sep = t['sep']
        mode = t['mode']
        if mode == 'idx':
            parts = s.split() if sep is None else s.split(sep)
            i = t['index']
            if -len(parts) <= i < len(parts):
                p = parts[i]
            else:
                p = t['default']
        elif mode == 'rest':
            pos = s.find(sep)
            p = s[pos + len(sep):] if pos >= 0 else t['default']
        else:  # 'init'
            pos = s.rfind(sep)
            p = s[:pos] if pos >= 0 else s
        if p is not None and t['post_strip']:
            p = p.strip()
        return p
    if k == 'concat':
        a = _sops(row.get(t['a']), t['ops'])
        b = _sops(row.get(t['b']), t['ops'])
        return t['prefix'] + ('' if a is None else a) + t['sep'] + ('' if b is None else b) + t['suffix']
    if k == 'arith':
        x = _num(row.get(t['a']))
        y = _num(row.get(t['b']))
        if x is None or y is None:
            return t['default']
        op = t['op']
        try:
            if op == '+':
                r = x + y
            elif op == '-':
                r = x - y
            elif op == '*':
                r = x * y
            else:
                r = x / y
            r = _round(r, t['round'])
            return _fmt(r, t['fmt'])
        except (ZeroDivisionError, OverflowError, ValueError):
            return t['default']
    if k == 'missing':
        return t['value']
    return None


def _filt(f, row):
    if f is None:
        return True
    k = f['k']
    if k == 'and':
        for a in f['args']:
            if not _filt(a, row):
                return False
        return True
    if k == 'or':
        for a in f['args']:
            if _filt(a, row):
                return True
        return False
    if k == 'const':
        return f['v']
    v = row.get(f['col'])
    op = f['op']
    if k == 'eq':
        r = _eq(v, f['v'])
        return r if op == '==' else not r
    if k == 'numeq':
        x = _num(v)
        r = x is not None and x == f['v']
        return r if op == '==' else not r
    if k == 'num':
        x = _num(v)
        if x is None:
            return False
        c = f['v']
    elif k == 'str':
        if not isinstance(v, str):
            return False
        x = v
        c = f['v']
    else:
        return False
    if op == '<':
        return x < c
    if op == '<=':
        return x <= c
    if op == '>':
        return x > c
    if op == '>=':
        return x >= c
    if op == '==':
        return x == c
    if op == '!=':
        return x != c
    return False


def _transform_row(spec, raw):
    out = {}
    for name, t in spec['columns']:
        out[name] = _apply(t, raw)
    return out
'''

RT_PY_IO = r'''

def _read_delimited(path, delim):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = _csv.reader(f, delimiter=delim)
        header = None
        for rec in reader:
            if header is None:
                header = rec
                continue
            if not rec:
                continue
            row = {}
            n = len(rec)
            for i, h in enumerate(header):
                row[h] = rec[i] if i < n else None
            yield row


def _read_jsonl(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            obj = _json.loads(s)
            if isinstance(obj, dict):
                yield obj


_WS = ' \t\r\n,'


def _read_json_array(path, chunk_size=1 << 16):
    dec = _json.JSONDecoder()
    with open(path, 'r', encoding='utf-8-sig') as f:
        buf = ''
        pos = 0
        eof = False
        started = False
        while True:
            n = len(buf)
            while pos < n and buf[pos] in _WS:
                pos += 1
            if pos < n and not started:
                if buf[pos] == '[':
                    started = True
                    pos += 1
                    continue
                started = True
            if pos < n and buf[pos] == ']':
                return
            if pos >= n:
                if eof:
                    return
                buf = f.read(chunk_size)
                pos = 0
                if not buf:
                    return
                continue
            try:
                obj, end = dec.raw_decode(buf, pos)
            except ValueError:
                if eof:
                    raise
                more = f.read(chunk_size)
                if not more:
                    eof = True
                buf = buf[pos:] + more
                pos = 0
                continue
            pos = end
            if isinstance(obj, dict):
                yield obj
            if pos > chunk_size:
                buf = buf[pos:]
                pos = 0


def _read_raw(path):
    fmt = SPEC['format']
    if fmt == 'csv':
        return _read_delimited(path, ',')
    if fmt == 'tsv':
        return _read_delimited(path, '\t')
    if fmt == 'jsonl':
        return _read_jsonl(path)
    return _read_json_array(path)


_SPEC_ID = _hashlib.sha256(_json.dumps(SPEC, sort_keys=True).encode('utf-8')).hexdigest()[:16]


def _write_int(fh, value):
    fh.seek(0)
    fh.write(b'%020d\n' % value)
    fh.flush()


class DynamicPreprocessor(object):
    """Streams a data file, applying the inferred filter/column transforms.

    buffer    -- maximum number of rows held in memory at once.
    cache_dir -- optional directory for caching / resuming.
    """

    def __init__(self, buffer=1024, cache_dir=None):
        try:
            buffer = int(buffer)
        except (TypeError, ValueError):
            buffer = 1024
        if buffer < 1:
            buffer = 1
        self.buffer = buffer
        self.cache_dir = None if cache_dir is None else _os.fspath(cache_dir)

    def __call__(self, path):
        return self._run(_os.fspath(path))

    def process(self, path):
        return self._run(_os.fspath(path))

    def transform(self, row):
        return _transform_row(SPEC, row)

    def _compute(self, path):
        filt = SPEC['filter']
        size = self.buffer
        batch = []
        for raw in _read_raw(path):
            batch.append(raw)
            if len(batch) >= size:
                out = [_transform_row(SPEC, r) for r in batch if _filt(filt, r)]
                batch = []
                for row in out:
                    yield row
        if batch:
            out = [_transform_row(SPEC, r) for r in batch if _filt(filt, r)]
            batch = []
            for row in out:
                yield row

    def _read_cached(self, rows_path):
        size = self.buffer
        with open(rows_path, 'r', encoding='utf-8') as f:
            batch = []
            for line in f:
                if line.strip():
                    batch.append(line)
                if len(batch) >= size:
                    out = [_json.loads(x) for x in batch]
                    batch = []
                    for row in out:
                        yield row
            if batch:
                out = [_json.loads(x) for x in batch]
                for row in out:
                    yield row

    def _run(self, path):
        if self.cache_dir is None:
            for row in self._compute(path):
                yield row
            return
        apath = _os.path.abspath(path)
        key = _hashlib.sha256((_SPEC_ID + '\0' + apath).encode('utf-8')).hexdigest()[:40]
        d = _os.path.join(self.cache_dir, key)
        _os.makedirs(d, exist_ok=True)
        meta_p = _os.path.join(d, 'meta.json')
        prog_p = _os.path.join(d, 'progress')
        rows_p = _os.path.join(d, 'rows.jsonl')
        st = _os.stat(path)
        meta = {'path': apath, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'spec': _SPEC_ID}
        old = None
        try:
            with open(meta_p, 'r', encoding='utf-8') as f:
                old = _json.load(f)
        except (OSError, ValueError):
            old = None
        if old != meta:
            for p in (prog_p, rows_p):
                try:
                    _os.remove(p)
                except OSError:
                    pass
            tmp = meta_p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump(meta, f)
            _os.replace(tmp, meta_p)
        start = 0
        try:
            with open(prog_p, 'rb') as f:
                start = int(f.read().strip() or b'0')
        except (OSError, ValueError):
            start = 0
        if start < 0:
            start = 0
        complete = _os.path.exists(rows_p)
        build = (not complete) and start == 0
        source = self._read_cached(rows_p) if complete else self._compute(path)
        tmp_rows = rows_p + '.%d.tmp' % _os.getpid()
        tf = None
        pf = open(prog_p, 'r+b' if _os.path.exists(prog_p) else 'w+b')
        try:
            if build:
                tf = open(tmp_rows, 'w', encoding='utf-8')
            wbuf = []
            idx = 0
            for row in source:
                if tf is not None:
                    wbuf.append(_json.dumps(row))
                    if len(wbuf) >= self.buffer:
                        tf.write('\n'.join(wbuf) + '\n')
                        wbuf = []
                if idx < start:
                    idx += 1
                    continue
                idx += 1
                _write_int(pf, idx)
                yield row
            if tf is not None:
                if wbuf:
                    tf.write('\n'.join(wbuf) + '\n')
                tf.close()
                _os.replace(tmp_rows, rows_p)
                tf = None
            _write_int(pf, 0)
        finally:
            pf.close()
            if tf is not None:
                try:
                    tf.close()
                except OSError:
                    pass
                try:
                    _os.remove(tmp_rows)
                except OSError:
                    pass


__all__ = ['DynamicPreprocessor']
'''

# ---------------------------------------------------------------------------
# Runtime (JavaScript)
# ---------------------------------------------------------------------------

RT_JS = r'''
const fs = require("fs");
const nodePath = require("path");
const crypto = require("crypto");
const { StringDecoder } = require("string_decoder");

const INT_RE = /^\s*[+-]?[0-9]+\s*$/;
const FLT_RE = /^\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*$/;

function _num(v) {
  if (v === null || v === undefined || typeof v === "boolean") return null;
  if (typeof v === "number") return v;
  if (typeof v === "string") {
    if (INT_RE.test(v) || FLT_RE.test(v)) return Number(v.trim());
  }
  return null;
}

// Python-compatible repr() of a float.
function _pyFloatRepr(v) {
  if (Number.isNaN(v)) return "nan";
  if (v === Infinity) return "inf";
  if (v === -Infinity) return "-inf";
  if (v === 0) return Object.is(v, -0) ? "-0.0" : "0.0";
  const parts = v.toExponential().split("e");
  let mant = parts[0];
  const exp = parseInt(parts[1], 10);
  let sign = "";
  if (mant[0] === "-") {
    sign = "-";
    mant = mant.slice(1);
  }
  const digits = mant.replace(".", "");
  const L = digits.length;
  if (exp >= -4 && exp < 16) {
    if (exp >= 0) {
      if (L <= exp + 1) return sign + digits + "0".repeat(exp + 1 - L) + ".0";
      return sign + digits.slice(0, exp + 1) + "." + digits.slice(exp + 1);
    }
    return sign + "0." + "0".repeat(-exp - 1) + digits;
  }
  const ae = Math.abs(exp);
  return sign + digits[0] + (L > 1 ? "." + digits.slice(1) : "") + "e" + (exp < 0 ? "-" : "+") + (ae < 10 ? "0" : "") + ae;
}

function _numStr(v, isFloat) {
  if (!isFloat && Number.isInteger(v) && Math.abs(v) < 1e21) return String(v);
  return _pyFloatRepr(v);
}

// Rows parsed from JSON remember which keys held float literals (e.g. 37.0),
// so string conversion matches Python's str(float).
const FLOAT_KEYS = new WeakMap();

function _str(v, isFloat) {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return _numStr(v, isFloat);
  return String(v);
}

function _isFloatKey(row, c) {
  const s = FLOAT_KEYS.get(row);
  return s !== undefined && s.has(c);
}

let _pendingFloat = [];
let _reviverOk = null;
function _reviver(key, value, context) {
  if (typeof value === "number") {
    if (context && typeof context.source === "string" && Number.isInteger(value) && /[.eE]/.test(context.source))
      _pendingFloat.push(key);
  } else if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    if (_pendingFloat.length) FLOAT_KEYS.set(value, new Set(_pendingFloat));
    _pendingFloat = [];
  }
  return value;
}

function _parseRow(text) {
  if (_reviverOk === null) {
    let ok = false;
    try {
      JSON.parse("[1.0]", function (k, v, c) {
        if (c && c.source === "1.0") ok = true;
        return v;
      });
    } catch (e) {}
    _reviverOk = ok;
  }
  if (!_reviverOk || !/[.eE]/.test(text)) return JSON.parse(text);
  _pendingFloat = [];
  const obj = JSON.parse(text, _reviver);
  _pendingFloat = [];
  return obj;
}

function _eq(a, b) {
  if (a === undefined) a = null;
  if (b === undefined) b = null;
  if (typeof a === "boolean" || typeof b === "boolean") return typeof a === typeof b && a === b;
  if (a === null || b === null) return a === null && b === null;
  if (typeof a === "number" || typeof b === "number") return typeof a === typeof b && a === b;
  return a === b;
}

function _sop(s, op) {
  if (op === "strip") return s.trim();
  if (op === "lower") return s.toLowerCase();
  if (op === "upper") return s.toUpperCase();
  return s;
}

function _sops(v, ops, isFloat) {
  let s = _str(v, isFloat);
  if (s === null) return null;
  for (const op of ops) s = _sop(s, op);
  return s;
}

function _roundHalfEven0(y) {
  const f = Math.floor(y);
  const diff = y - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

function _round(y, d) {
  if (d === null || d === undefined) return y;
  if (!isFinite(y)) return y;
  if (d === 0) return _roundHalfEven0(y);
  if (d > 0) return Number(y.toFixed(Math.min(d, 100)));
  const m = Math.pow(10, -d);
  return _roundHalfEven0(y / m) * m;
}

function _isInt(y) {
  return Number.isInteger(y) && Math.abs(y) < 1e16;
}

function _fmt(y, fmt) {
  const k = fmt[0];
  if (k === "raw" || k === "float" || k === "intify") return y;
  if (k === "int") return _roundHalfEven0(y);
  if (k === "s_g") return _isInt(y) ? String(y) : _pyFloatRepr(y);
  if (k === "s_int") return String(_roundHalfEven0(y));
  if (k === "s_fixed") return y.toFixed(fmt[1]);
  if (k === "s_repr") return _pyFloatRepr(y);
  return y;
}

function _get(row, c) {
  const v = row[c];
  return v === undefined ? null : v;
}

function _apply(t, row) {
  const k = t.type;
  if (k === "copy") {
    const v = _get(row, t.src);
    if (!t.ops.length) return v;
    return _sops(v, t.ops, _isFloatKey(row, t.src));
  }
  if (k === "const") return t.value;
  if (k === "linear") {
    const v = _get(row, t.src);
    const x = _num(v);
    if (x === null) return v;
    let y = t.a * x + t.b;
    y = _round(y, t.round);
    return _fmt(y, t.fmt);
  }
  if (k === "affix") {
    const s = _sops(_get(row, t.src), t.ops, _isFloatKey(row, t.src));
    if (s === null) return null;
    return t.prefix + s + t.suffix;
  }
  if (k === "split") {
    const s = _sops(_get(row, t.src), t.ops, _isFloatKey(row, t.src));
    if (s === null) return null;
    const sep = t.sep;
    let p;
    if (t.mode === "idx") {
      const parts = sep === null ? s.split(/\s+/).filter((x) => x !== "") : s.split(sep);
      let i = t.index;
      if (i >= -parts.length && i < parts.length) {
        if (i < 0) i += parts.length;
        p = parts[i];
      } else p = t.default;
    } else if (t.mode === "rest") {
      const pos = s.indexOf(sep);
      p = pos >= 0 ? s.slice(pos + sep.length) : t.default;
    } else {
      const pos = s.lastIndexOf(sep);
      p = pos >= 0 ? s.slice(0, pos) : s;
    }
    if (p !== null && p !== undefined && t.post_strip) p = p.trim();
    return p === undefined ? null : p;
  }
  if (k === "concat") {
    const a = _sops(_get(row, t.a), t.ops, _isFloatKey(row, t.a));
    const b = _sops(_get(row, t.b), t.ops, _isFloatKey(row, t.b));
    return t.prefix + (a === null ? "" : a) + t.sep + (b === null ? "" : b) + t.suffix;
  }
  if (k === "arith") {
    const x = _num(_get(row, t.a));
    const y = _num(_get(row, t.b));
    if (x === null || y === null) return t.default;
    let r;
    if (t.op === "+") r = x + y;
    else if (t.op === "-") r = x - y;
    else if (t.op === "*") r = x * y;
    else {
      if (y === 0) return t.default;
      r = x / y;
    }
    r = _round(r, t.round);
    return _fmt(r, t.fmt);
  }
  if (k === "missing") return t.value;
  return null;
}

function _filt(f, row) {
  if (f === null || f === undefined) return true;
  const k = f.k;
  if (k === "and") {
    for (const a of f.args) if (!_filt(a, row)) return false;
    return true;
  }
  if (k === "or") {
    for (const a of f.args) if (_filt(a, row)) return true;
    return false;
  }
  if (k === "const") return f.v;
  const v = _get(row, f.col);
  const op = f.op;
  if (k === "eq") {
    const r = _eq(v, f.v);
    return op === "==" ? r : !r;
  }
  if (k === "numeq") {
    const x = _num(v);
    const r = x !== null && x === f.v;
    return op === "==" ? r : !r;
  }
  let x, c;
  if (k === "num") {
    x = _num(v);
    if (x === null) return false;
    c = f.v;
  } else if (k === "str") {
    if (typeof v !== "string") return false;
    x = v;
    c = f.v;
  } else return false;
  if (op === "<") return x < c;
  if (op === "<=") return x <= c;
  if (op === ">") return x > c;
  if (op === ">=") return x >= c;
  if (op === "==") return x === c;
  if (op === "!=") return x !== c;
  return false;
}

function _transformRow(raw) {
  const out = {};
  for (const [name, t] of SPEC.columns) out[name] = _apply(t, raw);
  return out;
}

const CHUNK = 1 << 16;

function* _readChunks(path) {
  const fd = fs.openSync(path, "r");
  try {
    const buf = Buffer.alloc(CHUNK);
    const dec = new StringDecoder("utf8");
    let first = true;
    for (;;) {
      const n = fs.readSync(fd, buf, 0, CHUNK, null);
      if (n <= 0) break;
      let s = dec.write(buf.subarray(0, n));
      if (first && s.length) {
        if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
        first = false;
      }
      if (s.length) yield s;
    }
    let s = dec.end();
    if (first && s.length && s.charCodeAt(0) === 0xfeff) s = s.slice(1);
    if (s.length) yield s;
  } finally {
    fs.closeSync(fd);
  }
}

function* _readLines(path) {
  let rest = "";
  for (const chunk of _readChunks(path)) {
    const data = rest + chunk;
    let start = 0;
    for (;;) {
      const nl = data.indexOf("\n", start);
      if (nl < 0) break;
      let line = data.slice(start, nl);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      yield line;
      start = nl + 1;
    }
    rest = data.slice(start);
  }
  if (rest.length) {
    if (rest.endsWith("\r")) rest = rest.slice(0, -1);
    yield rest;
  }
}

function _parseDelimited(line, delim) {
  // Mirrors Python's csv.reader default dialect (minus embedded newlines).
  const out = [];
  let field = "";
  let i = 0;
  const n = line.length;
  let state = 0; // 0 = start of field, 1 = unquoted, 2 = quoted, 3 = after quote in quoted
  while (i < n) {
    const ch = line[i];
    if (state === 0) {
      if (ch === '"') state = 2;
      else if (ch === delim) out.push(field), (field = "");
      else (field += ch), (state = 1);
    } else if (state === 1) {
      if (ch === delim) out.push(field), (field = ""), (state = 0);
      else field += ch;
    } else if (state === 2) {
      if (ch === '"') state = 3;
      else field += ch;
    } else {
      if (ch === '"') (field += '"'), (state = 2);
      else if (ch === delim) out.push(field), (field = ""), (state = 0);
      else (field += ch), (state = 1);
    }
    i++;
  }
  out.push(field);
  return out;
}

function* _readDelimited(path, delim) {
  let header = null;
  for (const line of _readLines(path)) {
    if (header === null) {
      header = _parseDelimited(line, delim);
      continue;
    }
    if (line === "") continue;
    const rec = _parseDelimited(line, delim);
    const row = {};
    for (let i = 0; i < header.length; i++) row[header[i]] = i < rec.length ? rec[i] : null;
    yield row;
  }
}

function* _readJsonl(path) {
  for (const line of _readLines(path)) {
    const s = line.trim();
    if (!s) continue;
    const obj = _parseRow(s);
    if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) yield obj;
  }
}

function* _readJsonArray(path) {
  let depth = 0;
  let inStr = false;
  let esc = false;
  let pending = "";
  for (const chunk of _readChunks(path)) {
    let start = 0;
    const n = chunk.length;
    for (let i = 0; i < n; i++) {
      const ch = chunk.charCodeAt(i);
      if (depth === 0) {
        if (ch === 123) {
          depth = 1;
          start = i;
          inStr = false;
          esc = false;
        }
        continue;
      }
      if (inStr) {
        if (esc) esc = false;
        else if (ch === 92) esc = true;
        else if (ch === 34) inStr = false;
        continue;
      }
      if (ch === 34) inStr = true;
      else if (ch === 123 || ch === 91) depth++;
      else if (ch === 125 || ch === 93) {
        depth--;
        if (depth === 0) {
          const text = pending + chunk.slice(start, i + 1);
          pending = "";
          const obj = _parseRow(text);
          if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) yield obj;
        }
      }
    }
    if (depth > 0) {
      pending += chunk.slice(start);
    }
  }
}

function _readRaw(path) {
  const fmt = SPEC.format;
  if (fmt === "csv") return _readDelimited(path, ",");
  if (fmt === "tsv") return _readDelimited(path, "\t");
  if (fmt === "jsonl") return _readJsonl(path);
  return _readJsonArray(path);
}

const SPEC_ID = crypto.createHash("sha256").update(JSON.stringify(SPEC)).digest("hex").slice(0, 16);

function* _compute(path, size) {
  const filt = SPEC.filter;
  let batch = [];
  for (const raw of _readRaw(path)) {
    batch.push(raw);
    if (batch.length >= size) {
      const out = [];
      for (const r of batch) if (_filt(filt, r)) out.push(_transformRow(r));
      batch = [];
      yield* out;
    }
  }
  if (batch.length) {
    const out = [];
    for (const r of batch) if (_filt(filt, r)) out.push(_transformRow(r));
    batch = [];
    yield* out;
  }
}

function* _readCached(rowsPath, size) {
  let batch = [];
  for (const line of _readLines(rowsPath)) {
    if (line.trim()) batch.push(line);
    if (batch.length >= size) {
      const out = batch.map((x) => JSON.parse(x));
      batch = [];
      yield* out;
    }
  }
  if (batch.length) {
    const out = batch.map((x) => JSON.parse(x));
    batch = [];
    yield* out;
  }
}

function _pad(n) {
  let s = String(n);
  while (s.length < 20) s = "0" + s;
  return s + "\n";
}

function _rm(p) {
  try {
    fs.unlinkSync(p);
  } catch (e) {}
}

function* _run(path, size, cacheDir) {
  path = String(path);
  if (cacheDir === null || cacheDir === undefined) {
    yield* _compute(path, size);
    return;
  }
  const apath = nodePath.resolve(path);
  const key = crypto.createHash("sha256").update(SPEC_ID + "\0" + apath).digest("hex").slice(0, 40);
  const d = nodePath.join(String(cacheDir), key);
  fs.mkdirSync(d, { recursive: true });
  const metaP = nodePath.join(d, "meta.json");
  const progP = nodePath.join(d, "progress");
  const rowsP = nodePath.join(d, "rows.jsonl");
  const st = fs.statSync(path, { bigint: true });
  const meta = JSON.stringify({ path: apath, size: String(st.size), mtime_ns: String(st.mtimeNs), spec: SPEC_ID });
  let old = null;
  try {
    old = fs.readFileSync(metaP, "utf8");
  } catch (e) {
    old = null;
  }
  if (old !== meta) {
    _rm(progP);
    _rm(rowsP);
    fs.writeFileSync(metaP + ".tmp", meta);
    fs.renameSync(metaP + ".tmp", metaP);
  }
  let start = 0;
  try {
    start = parseInt(fs.readFileSync(progP, "utf8").trim() || "0", 10);
    if (!(start >= 0)) start = 0;
  } catch (e) {
    start = 0;
  }
  const complete = fs.existsSync(rowsP);
  const build = !complete && start === 0;
  const source = complete ? _readCached(rowsP, size) : _compute(path, size);
  const tmpRows = rowsP + "." + process.pid + ".tmp";
  let tf = null;
  const pf = fs.openSync(progP, fs.existsSync(progP) ? "r+" : "w+");
  try {
    if (build) tf = fs.openSync(tmpRows, "w");
    let wbuf = [];
    let idx = 0;
    for (const row of source) {
      if (tf !== null) {
        wbuf.push(JSON.stringify(row));
        if (wbuf.length >= size) {
          fs.writeSync(tf, wbuf.join("\n") + "\n");
          wbuf = [];
        }
      }
      if (idx < start) {
        idx++;
        continue;
      }
      idx++;
      fs.writeSync(pf, _pad(idx), 0);
      yield row;
    }
    if (tf !== null) {
      if (wbuf.length) fs.writeSync(tf, wbuf.join("\n") + "\n");
      fs.closeSync(tf);
      tf = null;
      fs.renameSync(tmpRows, rowsP);
    }
    fs.writeSync(pf, _pad(0), 0);
  } finally {
    fs.closeSync(pf);
    if (tf !== null) {
      try {
        fs.closeSync(tf);
      } catch (e) {}
      _rm(tmpRows);
    }
  }
}

function DynamicPreprocessor(opts, cacheDirArg) {
  let buffer = 1024;
  let cacheDir = null;
  if (opts !== null && typeof opts === "object") {
    if (opts.buffer !== undefined) buffer = opts.buffer;
    if (opts.cache_dir !== undefined) cacheDir = opts.cache_dir;
    else if (opts.cacheDir !== undefined) cacheDir = opts.cacheDir;
  } else if (opts !== undefined && opts !== null) {
    buffer = opts;
  }
  if (cacheDirArg !== undefined && cacheDirArg !== null) cacheDir = cacheDirArg;
  buffer = parseInt(buffer, 10);
  if (!(buffer >= 1)) buffer = 1;
  if (cacheDir === undefined) cacheDir = null;
  const fn = function (path) {
    return _run(path, buffer, cacheDir);
  };
  fn.buffer = buffer;
  fn.cache_dir = cacheDir;
  fn.process = fn;
  fn.iterate = fn;
  fn.transform = (row) => _transformRow(row);
  return fn;
}

module.exports = DynamicPreprocessor;
module.exports.DynamicPreprocessor = DynamicPreprocessor;
module.exports.default = DynamicPreprocessor;
module.exports.SPEC = SPEC;
'''

# Namespace with the runtime helpers, used by the generator for inference.
RT = {}
exec(compile(RT_PY_CORE, "<dynamic_buffer_runtime>", "exec"), RT)
_num = RT["_num"]
_str = RT["_str"]
_eq = RT["_eq"]
_apply = RT["_apply"]
_filt = RT["_filt"]
_sops = RT["_sops"]


# ---------------------------------------------------------------------------
# Sample loading
# ---------------------------------------------------------------------------

def load_file(path, fmt):
    if fmt in ("csv", "tsv"):
        delim = "," if fmt == "csv" else "\t"
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=delim)
            header = None
            rows = []
            for rec in reader:
                if header is None:
                    header = rec
                    continue
                if not rec:
                    continue
                rows.append({h: (rec[i] if i < len(rec) else None) for i, h in enumerate(header)})
        return (header or []), rows
    with open(path, "r", encoding="utf-8-sig") as f:
        if fmt == "jsonl":
            rows = []
            for line in f:
                s = line.strip()
                if s:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        rows.append(obj)
        else:
            text = f.read().strip()
            data = json.loads(text) if text else []
            if isinstance(data, dict):
                data = [data]
            rows = [r for r in data if isinstance(r, dict)]
    cols = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols, rows


# ---------------------------------------------------------------------------
# Value matching helpers
# ---------------------------------------------------------------------------

def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def approx_eq(p, y):
    """Equality allowing tiny float error between numbers (or numeric strings)."""
    if _eq(p, y):
        return True
    a = p if is_number(p) else (_num(p) if isinstance(p, str) else None)
    b = y if is_number(y) else (_num(y) if isinstance(y, str) else None)
    if a is None or b is None:
        return False
    if isinstance(p, str) != isinstance(y, str):
        return False
    try:
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    except OverflowError:
        return False


def fits(t, pairs, approx=False):
    cmp = approx_eq if approx else _eq
    for row, y in pairs:
        try:
            p = _apply(t, row)
        except Exception:
            return False
        if not cmp(p, y):
            return False
    return True


def count_fit(t, pairs):
    c = 0
    for row, y in pairs:
        try:
            if _eq(_apply(t, row), y):
                c += 1
        except Exception:
            pass
    return c


def decimals_of(s):
    s = s.strip()
    if "e" in s or "E" in s:
        return None
    if "." in s:
        return len(s) - s.index(".") - 1
    return 0


def tidy_number(v):
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return int(v)
    return v


def snap_candidates(v):
    out = []
    for d in range(0, 13):
        try:
            r = round(v, d)
        except (OverflowError, ValueError):
            continue
        out.append(tidy_number(r))
    out.append(tidy_number(v))
    return out


# ---------------------------------------------------------------------------
# Column transformation inference
# ---------------------------------------------------------------------------

STR_OPS = [["strip"], ["lower"], ["upper"], ["strip", "lower"], ["strip", "upper"]]
AFFIX_OPS = [[], ["strip"], ["lower"], ["upper"], ["strip", "lower"], ["strip", "upper"]]
SPLIT_SEPS = [" ", ",", ";", "|", "-", "_", "/", ":", ".", "@", "\t", "#", "=", "+", "&", "~", ", ", " - ", "; ", " | ", None]


def numeric_fmt_candidates(ys, is_text):
    if is_text:
        decs = []
        for y in ys:
            if isinstance(y, str):
                d = decimals_of(y)
                if d is not None and d > 0 and d not in decs:
                    decs.append(d)
        decs.sort()
        return [["s_g"], ["s_int"]] + [["s_fixed", d] for d in decs] + [["s_repr"]]
    nums = [y for y in ys if is_number(y)]
    if nums and all(isinstance(y, int) for y in nums):
        return [["int"], ["raw"]]
    if nums and all(isinstance(y, float) for y in nums):
        return [["float"], ["raw"]]
    return [["intify"], ["raw"]]


def numeric_points(pairs, src, is_text):
    pts = []
    for row, y in pairs:
        v = row.get(src)
        x = _num(v)
        if x is None:
            if not _eq(v, y):
                return None
            continue
        if is_text:
            if not isinstance(y, str):
                return None
            yn = _num(y)
        else:
            yn = y if is_number(y) else None
        if yn is None:
            return None
        pts.append((x, yn))
    return pts


def linear_candidates(src, pairs, ys, is_text):
    pts = numeric_points(pairs, src, is_text)
    if not pts:
        return
    fmts = numeric_fmt_candidates(ys, is_text)
    abs_ = [(1, 0)]
    xs = [p[0] for p in pts]
    x0, y0 = pts[0]
    far = max(range(len(pts)), key=lambda i: (abs(pts[i][0] - x0), -i))
    x1, y1 = pts[far]
    raw = []
    if x1 != x0:
        try:
            a = (y1 - y0) / (x1 - x0)
            b = y0 - a * x0
            raw.append((a, b))
        except (OverflowError, ZeroDivisionError):
            pass
    else:
        raw.append((1, y0 - x0))
        if x0 != 0:
            raw.append((y0 / x0, 0))
    for a, b in raw:
        if not (math.isfinite(a) and math.isfinite(b)):
            continue
        sa = snap_candidates(a)
        sb = snap_candidates(b)
        for i in range(len(sa)):
            abs_.append((sa[i], sb[i]))
        # mixed precision combos
        for i in range(len(sa)):
            for j in range(len(sb)):
                abs_.append((sa[i], sb[j]))
    seen = set()
    uniq = []
    for ab in abs_:
        key = (repr(ab[0]), repr(ab[1]))
        if key not in seen:
            seen.add(key)
            uniq.append(ab)
    del xs
    rounds = [None] + list(range(0, 11))
    # exact pass
    for a, b in uniq:
        for rnd in rounds:
            for fmt in fmts:
                t = {"type": "linear", "src": src, "a": a, "b": b, "round": rnd, "fmt": fmt}
                if fits(t, pairs):
                    yield t
                    return
    # approximate pass
    for a, b in uniq:
        for fmt in fmts:
            t = {"type": "linear", "src": src, "a": a, "b": b, "round": None, "fmt": fmt}
            if fits(t, pairs, approx=True):
                yield t
                return


def affix_candidates(src, pairs, ys, is_text):
    if not all(y is None or isinstance(y, str) for y in ys):
        return
    for ops in AFFIX_OPS:
        if not ops and is_text is False:
            pass
        first = None
        for row, y in pairs:
            s = _sops(row.get(src), ops)
            if s is not None and s != "" and isinstance(y, str):
                first = (s, y)
                break
        if first is None:
            continue
        s, y = first
        pos = y.find(s)
        tried = 0
        while pos >= 0 and tried < 8:
            tried += 1
            p = y[:pos]
            q = y[pos + len(s):]
            if p or q:
                yield {"type": "affix", "src": src, "ops": ops, "prefix": p, "suffix": q}
            pos = y.find(s, pos + 1)


def split_candidates(src, pairs, ys, is_text):
    if not all(y is None or isinstance(y, str) for y in ys):
        return
    defaults = ["", None]
    for ops in ([], ["strip"]):
        first = None
        for row, y in pairs:
            s = _sops(row.get(src), ops)
            if s is not None and isinstance(y, str) and s != y:
                first = (s, y)
                break
        if first is None:
            continue
        s, y = first
        for sep in SPLIT_SEPS:
            if sep is not None and sep not in s:
                continue
            parts = s.split() if sep is None else s.split(sep)
            if len(parts) < 2:
                continue
            for post in (False, True):
                for i, p in enumerate(parts):
                    pp = p.strip() if post else p
                    if pp == y:
                        for idx in (i, i - len(parts)):
                            for dflt in defaults:
                                yield {"type": "split", "src": src, "ops": ops, "sep": sep, "mode": "idx",
                                       "index": idx, "default": dflt, "post_strip": post}
                if sep is not None:
                    for mode in ("rest", "init"):
                        for dflt in defaults:
                            yield {"type": "split", "src": src, "ops": ops, "sep": sep, "mode": mode,
                                   "index": 0, "default": dflt, "post_strip": post}


def single_source_candidates(src, pairs, ys, is_text, same_name):
    yield {"type": "copy", "src": src, "ops": []}
    if any(isinstance(y, str) for y in ys):
        for ops in STR_OPS:
            yield {"type": "copy", "src": src, "ops": ops}
        if not is_text:
            yield {"type": "copy", "src": src, "ops": ["str"]}
    if any(is_number(y) or (isinstance(y, str) and _num(y) is not None) for y in ys):
        for t in linear_candidates(src, pairs, ys, is_text):
            yield t
    for t in affix_candidates(src, pairs, ys, is_text):
        yield t
    for t in split_candidates(src, pairs, ys, is_text):
        yield t


def concat_candidates(in_cols, pairs, ys, is_text):
    if not ys or not all(isinstance(y, str) for y in ys):
        return
    row0, y0 = pairs[0]
    for ops in ([], ["strip"], ["lower"], ["upper"]):
        for a in in_cols:
            sa = _sops(row0.get(a), ops)
            sa = "" if sa is None else sa
            if not sa:
                continue
            for b in in_cols:
                if b == a:
                    continue
                sb = _sops(row0.get(b), ops)
                sb = "" if sb is None else sb
                if not sb:
                    continue
                pa = y0.find(sa)
                while pa >= 0:
                    pb = y0.find(sb, pa + len(sa))
                    while pb >= 0:
                        yield {"type": "concat", "a": a, "b": b, "ops": ops, "prefix": y0[:pa],
                               "sep": y0[pa + len(sa):pb], "suffix": y0[pb + len(sb):]}
                        pb = y0.find(sb, pb + 1)
                    pa = y0.find(sa, pa + 1)


def arith_candidates(in_cols, pairs, ys, is_text):
    if not ys:
        return
    if is_text:
        if not all(isinstance(y, str) and _num(y) is not None for y in ys):
            return
    elif not all(is_number(y) for y in ys):
        return
    fmts = numeric_fmt_candidates(ys, is_text)
    numeric_cols = []
    for c in in_cols:
        if all(_num(r.get(c)) is not None for r, _ in pairs):
            numeric_cols.append(c)
    default = "" if is_text else None
    for approx in (False, True):
        for op in ("+", "-", "*", "/"):
            for a in numeric_cols:
                for b in numeric_cols:
                    if a == b:
                        continue
                    for rnd in ([None] + list(range(0, 11)) if not approx else [None]):
                        for fmt in fmts:
                            t = {"type": "arith", "a": a, "b": b, "op": op, "round": rnd, "fmt": fmt,
                                 "default": default}
                            if fits(t, pairs, approx=approx):
                                yield t
                                return


def infer_column(oc, in_cols, pairs, is_text):
    ys = [y for _, y in pairs]
    if not pairs:
        if oc in in_cols:
            return {"type": "copy", "src": oc, "ops": []}
        return {"type": "missing", "value": "" if is_text else None}

    def first_fit(gen):
        for t in gen:
            if fits(t, pairs):
                return t
        return None

    def first_approx(gen):
        for t in gen:
            if fits(t, pairs, approx=True):
                return t
        return None

    all_same = all(_eq(y, ys[0]) for y in ys)
    const_t = {"type": "const", "value": ys[0]}

    if oc in in_cols:
        t = first_fit(single_source_candidates(oc, pairs, ys, is_text, True))
        if t:
            return t
    if all_same and len(ys) >= 2:
        return const_t
    for c in in_cols:
        if c == oc:
            continue
        t = first_fit(single_source_candidates(c, pairs, ys, is_text, False))
        if t:
            return t
    t = first_fit(concat_candidates(in_cols, pairs, ys, is_text))
    if t:
        return t
    t = first_fit(arith_candidates(in_cols, pairs, ys, is_text))
    if t:
        return t
    if all_same:
        return const_t
    # Approximate fallbacks / best partial matches.
    best = None
    best_score = -1
    cands = []
    if oc in in_cols:
        cands.append({"type": "copy", "src": oc, "ops": []})
    for c in in_cols:
        for ops in [[]] + STR_OPS:
            cands.append({"type": "copy", "src": c, "ops": ops})
    for t in cands:
        s = count_fit(t, pairs)
        if s > best_score:
            best, best_score = t, s
    # most common output value as constant
    counts = []
    for y in ys:
        for e in counts:
            if _eq(e[0], y):
                e[1] += 1
                break
        else:
            counts.append([y, 1])
    cv, cn = max(counts, key=lambda e: e[1])
    if cn > best_score:
        return {"type": "const", "value": cv}
    if best is not None and best_score > 0:
        return best
    if oc in in_cols:
        return {"type": "copy", "src": oc, "ops": []}
    return {"type": "const", "value": ys[0]}


def infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text):
    cols = []
    for oc in out_cols:
        pairs = [(in_rows[align[j]], out_rows[j].get(oc)) for j in range(len(out_rows))]
        cols.append([oc, infer_column(oc, in_cols, pairs, is_text)])
    return cols


# ---------------------------------------------------------------------------
# Row alignment (output rows are an order-preserving subsequence of input)
# ---------------------------------------------------------------------------

def value_keys(v):
    if v is None:
        return ["N"]
    ks = []
    s = _str(v)
    ks.append("s:" + s.strip().lower())
    n = _num(v)
    if n is not None:
        try:
            ks.append("n:" + repr(float(n)))
        except OverflowError:
            pass
    return ks


def align_dp(n_in, n_out, sim):
    """Pick strictly increasing input indices for each output row maximizing sum(sim)."""
    if n_out == 0:
        return []
    NEG = float("-inf")
    # dp[j][i]: best score matching first j outputs using first i inputs
    prev = [0] * (n_in + 1)
    choice = []
    for j in range(1, n_out + 1):
        cur = [NEG] * (n_in + 1)
        take = bytearray(n_in + 1)
        srow = sim[j - 1]
        for i in range(j, n_in + 1):
            skip = cur[i - 1]
            tk = prev[i - 1] + srow[i - 1] if prev[i - 1] != NEG else NEG
            if tk > skip:
                cur[i] = tk
                take[i] = 1
            else:
                cur[i] = skip
        choice.append(take)
        prev = cur
    # backtrack
    res = [0] * n_out
    i = n_in
    for j in range(n_out, 0, -1):
        take = choice[j - 1]
        while i > j and not take[i]:
            i -= 1
        res[j - 1] = i - 1
        i -= 1
    return res


def basic_similarity(in_cols, in_rows, out_cols, out_rows):
    in_sets = []
    in_strs = []
    for r in in_rows:
        s = set()
        strs = []
        for c in in_cols:
            v = r.get(c)
            for k in value_keys(v):
                s.add(k)
            sv = _str(v)
            if sv is not None and len(sv.strip()) >= 2:
                strs.append(sv.strip().lower())
        in_sets.append(s)
        in_strs.append(strs)
    small = len(in_rows) * len(out_rows) * max(1, len(in_cols)) * max(1, len(out_cols)) <= 3000000
    sim = []
    for o in out_rows:
        okeys = []
        ostrs = []
        for oc in out_cols:
            v = o.get(oc)
            okeys.append(value_keys(v))
            sv = _str(v)
            ostrs.append(sv.strip().lower() if sv is not None else None)
        row_sim = []
        for i in range(len(in_rows)):
            s = in_sets[i]
            score = 0
            for idx, ks in enumerate(okeys):
                hit = False
                for k in ks:
                    if k in s:
                        hit = True
                        break
                if hit:
                    score += 2
                elif small and ostrs[idx] is not None and len(ostrs[idx]) >= 2:
                    os_ = ostrs[idx]
                    for st in in_strs[i]:
                        if st in os_ or os_ in st:
                            score += 1
                            break
            row_sim.append(score)
        sim.append(row_sim)
    return sim


def canon_key(v):
    if v is None:
        return ("n",)
    if isinstance(v, bool):
        return ("b", v)
    if is_number(v):
        try:
            return ("x", float("%.9g" % v))
        except (OverflowError, ValueError):
            return ("x", repr(v))
    if isinstance(v, str):
        n = _num(v)
        if n is not None:
            try:
                return ("x", float("%.9g" % n))
            except (OverflowError, ValueError):
                pass
        return ("s", v)
    return ("o", repr(v))


def exact_similarity(columns, in_rows, out_rows):
    n_in = len(in_rows)
    indexes = []
    for name, t in columns:
        idx = {}
        for i, r in enumerate(in_rows):
            try:
                k = canon_key(_apply(t, r))
            except Exception:
                continue
            idx.setdefault(k, []).append(i)
        indexes.append((name, idx))
    sim = []
    for o in out_rows:
        counts = [0] * n_in
        for name, idx in indexes:
            for i in idx.get(canon_key(o.get(name)), ()):
                counts[i] += 1
        sim.append(counts)
    return sim


# ---------------------------------------------------------------------------
# Filter inference
# ---------------------------------------------------------------------------

def popcount(x):
    return bin(x).count("1")


def roundest(lo, hi):
    """Return the 'roundest' number in [lo, hi] (lo <= hi)."""
    if lo == hi:
        return lo
    both_int = isinstance(lo, int) and isinstance(hi, int)
    for e in range(18, -13, -1):
        if e >= 0:
            step = 10 ** e
            k_lo = -((-lo) // step) if isinstance(lo, int) else math.ceil(lo / step)
            k_hi = hi // step if isinstance(hi, int) else math.floor(hi / step)
        else:
            m = 10 ** (-e)
            k_lo = math.ceil(lo * m - 1e-9)
            k_hi = math.floor(hi * m + 1e-9)
        k_lo = int(k_lo)
        k_hi = int(k_hi)
        if k_lo > k_hi:
            continue
        mid = (lo + hi) / 2.0
        ks = range(k_lo, k_hi + 1) if k_hi - k_lo < 50 else [k_lo, k_hi]

        def val(k):
            if e >= 0:
                return k * (10 ** e)
            return k / (10 ** (-e))

        best = None
        bkey = None
        for k in ks:
            v = val(k)
            if v < lo or v > hi:
                continue
            key = (0 if k % 5 == 0 else 1, 0 if k % 2 == 0 else 1, abs(v - mid), v)
            if bkey is None or key < bkey:
                best, bkey = v, key
        if best is None:
            continue
        if isinstance(best, float) and best.is_integer() and (both_int or abs(best) < 1e15):
            if both_int:
                best = int(best)
        if not both_int and isinstance(best, int) and not (isinstance(lo, int) or isinstance(hi, int)):
            pass
        return best
    return lo


def materialize(node):
    """Turn placeholder threshold atoms into concrete comparison nodes."""
    if node.get("k") in ("and", "or"):
        return {"k": node["k"], "args": [materialize(a) for a in node["args"]]}
    if node.get("k") == "gapnum":
        lo, hi = node["lo"], node["hi"]
        t = roundest(lo, hi)
        if node["side"] == "ge":
            if t == hi:
                return {"k": "num", "col": node["col"], "op": ">=", "v": hi}
            if t == lo:
                return {"k": "num", "col": node["col"], "op": ">", "v": lo}
            return {"k": "num", "col": node["col"], "op": ">=", "v": t}
        else:
            if t == lo:
                return {"k": "num", "col": node["col"], "op": "<=", "v": lo}
            if t == hi:
                return {"k": "num", "col": node["col"], "op": "<", "v": hi}
            return {"k": "num", "col": node["col"], "op": "<", "v": t}
    if node.get("k") == "gapstr":
        if node["side"] == "ge":
            return {"k": "str", "col": node["col"], "op": ">=", "v": node["hi"]}
        return {"k": "str", "col": node["col"], "op": "<=", "v": node["lo"]}
    return node


def type_key(v):
    if v is None:
        return ("n", None)
    if isinstance(v, bool):
        return ("b", v)
    if is_number(v):
        return ("x", float(v))
    return ("s", v)


def lab_of(mask, K):
    if K is None:
        return (True, True)
    return (bool(mask & K), bool(mask & ~K))


def boundary(a, b):
    """A threshold between two adjacent values is only useful at a label change."""
    return a != b or a == (True, True)


def gen_atoms(in_cols, rows, K=None):
    n = len(rows)
    atoms = []  # (cost, node, mask)
    for c in in_cols:
        vals = [r.get(c) for r in rows]
        nonmiss = [v for v in vals if v is not None and v != ""]
        nums = [_num(v) for v in nonmiss]
        is_num = bool(nonmiss) and all(x is not None for x in nums)
        distinct = {}
        order = []
        for i, v in enumerate(vals):
            k = type_key(v)
            if k not in distinct:
                distinct[k] = [v, 0, 0]
                order.append(k)
            distinct[k][1] += 1
            distinct[k][2] |= (1 << i)
        card = len(order)
        highcard = n >= 4 and card >= 0.9 * n
        keys = order
        if card > 60:
            keys = sorted(order, key=lambda k: (-distinct[k][1], order.index(k)))[:60]
        if card >= 2:
            for k in keys:
                v, cnt, mask = distinct[k]
                cost = 95 if card <= 2 else 100
                if cnt == 1:
                    cost += 150
                if highcard:
                    cost += 100
                if is_num and v is not None and v != "":
                    nv = _num(v)
                    nm = 0
                    for i, rv in enumerate(vals):
                        x = _num(rv)
                        if x is not None and x == nv:
                            nm |= (1 << i)
                    nv = tidy_number(nv) if isinstance(nv, float) else nv
                    atoms.append((cost + 10, {"k": "numeq", "col": c, "op": "==", "v": nv}, nm))
                    atoms.append((cost + 11, {"k": "numeq", "col": c, "op": "!=", "v": nv}, ((1 << n) - 1) & ~nm))
                else:
                    atoms.append((cost, {"k": "eq", "col": c, "op": "==", "v": v}, mask))
                    atoms.append((cost + 1, {"k": "eq", "col": c, "op": "!=", "v": v}, ((1 << n) - 1) & ~mask))
        if is_num:
            pts = []
            for i, v in enumerate(vals):
                x = _num(v)
                if x is not None:
                    pts.append((x, i))
            uniq = sorted(set(p[0] for p in pts))
            if len(uniq) >= 2:
                # mask of rows with value >= uniq[g+1]
                pts.sort()
                masks_ge = []
                total = 0
                bucket = {}
                for x, i in pts:
                    bucket[x] = bucket.get(x, 0) | (1 << i)
                suffix = 0
                ge = [0] * len(uniq)
                for g in range(len(uniq) - 1, -1, -1):
                    suffix |= bucket[uniq[g]]
                    ge[g] = suffix
                allnum = ge[0]
                del masks_ge, total
                labs = [lab_of(bucket[u], K) for u in uniq]
                for g in range(len(uniq) - 1):
                    bnd = boundary(labs[g], labs[g + 1])
                    lo, hi = uniq[g], uniq[g + 1]
                    lo = tidy_number(lo) if isinstance(lo, float) else lo
                    hi = tidy_number(hi) if isinstance(hi, float) else hi
                    mge = ge[g + 1]
                    mle = allnum & ~mge
                    atoms.append((100, {"k": "gapnum", "col": c, "side": "ge", "lo": lo, "hi": hi, "g": g, "b": bnd}, mge))
                    atoms.append((100, {"k": "gapnum", "col": c, "side": "le", "lo": lo, "hi": hi, "g": g, "b": bnd}, mle))
        elif nonmiss and all(isinstance(v, str) for v in nonmiss) and card >= 3:
            strs = sorted(set(v for v in vals if isinstance(v, str) and v != ""))
            if 2 <= len(strs) <= 5000:
                bucket = {}
                for i, v in enumerate(vals):
                    if isinstance(v, str) and v != "":
                        bucket[v] = bucket.get(v, 0) | (1 << i)
                suffix = 0
                ge = [0] * len(strs)
                for g in range(len(strs) - 1, -1, -1):
                    suffix |= bucket[strs[g]]
                    ge[g] = suffix
                allstr = ge[0]
                labs = [lab_of(bucket[u], K) for u in strs]
                for g in range(len(strs) - 1):
                    bnd = boundary(labs[g], labs[g + 1])
                    lo, hi = strs[g], strs[g + 1]
                    mge = ge[g + 1]
                    atoms.append((200, {"k": "gapstr", "col": c, "side": "ge", "lo": lo, "hi": hi, "g": g, "b": bnd}, mge))
                    atoms.append((200, {"k": "gapstr", "col": c, "side": "le", "lo": lo, "hi": hi, "g": g, "b": bnd}, allstr & ~mge))
    return atoms


def dedupe_atoms(atoms):
    best = {}
    order = []
    for idx, (cost, node, mask) in enumerate(atoms):
        if mask not in best:
            best[mask] = (cost, idx, node)
            order.append(mask)
        elif (cost, idx) < best[mask][:2]:
            best[mask] = (cost, idx, node)
    out = [(best[m][0], best[m][1], best[m][2], m) for m in order]
    out.sort(key=lambda a: (a[0], a[1]))
    return out


def solve_cover(cands, target, triple_limit=120):
    """cands: list of (cost, nodes, cov). Find min-cost subset covering target."""
    best = {}
    for idx, (cost, nodes, cov) in enumerate(cands):
        cov &= target
        if not cov:
            continue
        if cov not in best or (cost, idx) < (best[cov][0], best[cov][1]):
            best[cov] = (cost, idx, nodes)
    items = [(v[0], v[1], v[2], cov) for cov, v in best.items()]
    items.sort(key=lambda a: (a[0], a[1]))
    # remove dominated
    kept = []
    for it in items:
        dom = False
        for k in kept:
            if k[0] <= it[0] and (k[3] | it[3]) == k[3]:
                dom = True
                break
        if not dom:
            kept.append(it)
        if len(kept) > 3000:
            break
    items = kept
    N = len(items)
    sol = None  # (cost, size, idxs)

    def consider(idxs):
        nonlocal sol
        cost = sum(items[i][0] for i in idxs)
        key = (cost, len(idxs), tuple(sorted(items[i][1] for i in idxs)))
        if sol is None or key < sol[0]:
            sol = (key, idxs)

    for i in range(N):
        if items[i][3] == target:
            consider([i])
    for i in range(N):
        ci = items[i][3]
        for j in range(i + 1, N):
            if (ci | items[j][3]) == target:
                consider([i, j])
    if N <= triple_limit:
        for i in range(N):
            ci = items[i][3]
            for j in range(i + 1, N):
                cij = ci | items[j][3]
                if cij == ci or cij == items[j][3]:
                    continue
                for k in range(j + 1, N):
                    if (cij | items[k][3]) == target:
                        consider([i, j, k])
    if sol is not None:
        return sol[0][0], [items[i][2] for i in sol[1]], True
    # greedy weighted set cover
    remaining = target
    chosen = []
    total = 0
    while remaining:
        bi = None
        bkey = None
        for i in range(N):
            g = popcount(items[i][3] & remaining)
            if g == 0:
                continue
            key = (-g / items[i][0], items[i][0], items[i][1])
            if bkey is None or key < bkey:
                bi, bkey = i, key
        if bi is None:
            break
        chosen.append(bi)
        total += items[bi][0]
        remaining &= ~items[bi][3]
    if not chosen:
        return None
    # order chosen by original index for determinism
    chosen.sort(key=lambda i: items[i][1])
    return total, [items[i][2] for i in chosen], remaining == 0


def build_node(kind, groups):
    """kind 'cnf': AND of OR-groups; 'dnf': OR of AND-groups."""
    outer, inner = ("and", "or") if kind == "cnf" else ("or", "and")
    args = []
    for g in groups:
        if len(g) == 1:
            args.append(g[0])
        else:
            args.append({"k": inner, "args": list(g)})
    if len(args) == 1:
        return args[0]
    return {"k": outer, "args": args}


def formula_mask(kind, groups, mask_of, ALL):
    if kind == "cnf":
        m = ALL
        for g in groups:
            o = 0
            for a in g:
                o |= mask_of(a)
            m &= o
        return m
    m = 0
    for g in groups:
        a_ = ALL
        for a in g:
            a_ &= mask_of(a)
        m |= a_
    return m


def widen_thresholds(kind, groups, raw_atoms, K, D, ALL, rows):
    """For each threshold atom, find the contiguous range of gaps that keep the
    formula consistent and replace it with an atom spanning that whole range."""
    masks = {}
    by_axis = {}
    for cost, node, mask in raw_atoms:
        masks[id(node)] = mask
        if node["k"] in ("gapnum", "gapstr"):
            by_axis.setdefault((node["k"], node["col"], node["side"]), {})[node["g"]] = (node, mask)
    groups = [list(g) for g in groups]
    target = K if kind == "cnf" else K  # formula must equal K on sample rows

    def mask_of(a):
        m = masks.get(id(a))
        if m is None:
            m = 0
            for i, r in enumerate(rows):
                if _filt(a, r):
                    m |= (1 << i)
            masks[id(a)] = m
        return m

    base_ok = formula_mask(kind, groups, mask_of, ALL) == target
    if not base_ok:
        return groups
    for gi in range(len(groups)):
        for ai in range(len(groups[gi])):
            a = groups[gi][ai]
            if a.get("k") not in ("gapnum", "gapstr") or "g" not in a:
                continue
            axis = by_axis.get((a["k"], a["col"], a["side"]), {})
            g0 = a["g"]
            ok = [g0]
            for step in (-1, 1):
                g = g0 + step
                while g in axis:
                    groups[gi][ai] = axis[g][0]
                    if formula_mask(kind, groups, mask_of, ALL) == target:
                        ok.append(g)
                        g += step
                    else:
                        break
                groups[gi][ai] = a
            lo = axis[min(ok)][0]["lo"]
            hi = axis[max(ok)][0]["hi"]
            if a["k"] == "gapnum":
                # Interior candidates must lie within the full feasible span.
                t = roundest(lo, hi)
                if t == hi:
                    op, v = (">=", hi) if a["side"] == "ge" else ("<", hi)
                elif t == lo:
                    op, v = (">", lo) if a["side"] == "ge" else ("<=", lo)
                else:
                    op, v = (">=", t) if a["side"] == "ge" else ("<", t)
                new = {"k": "num", "col": a["col"], "op": op, "v": v}
                groups[gi][ai] = new
                if formula_mask(kind, groups, mask_of, ALL) != target:
                    groups[gi][ai] = a
            else:
                if a["side"] == "ge":
                    groups[gi][ai] = {"k": "str", "col": a["col"], "op": ">=", "v": a["hi"]}
                else:
                    groups[gi][ai] = {"k": "str", "col": a["col"], "op": "<=", "v": a["lo"]}
    return groups


def infer_filter(in_cols, rows, kept_idx):
    n = len(rows)
    ALL = (1 << n) - 1
    K = 0
    for i in kept_idx:
        K |= (1 << i)
    D = ALL & ~K
    if D == 0:
        return None
    if K == 0:
        return {"k": "const", "v": False}
    raw_atoms = gen_atoms(in_cols, rows, K)
    atoms = dedupe_atoms([a for a in raw_atoms if a[1].get("b", True)])

    results = []
    # CNF with single-atom clauses: atoms true on all kept rows; cover dropped rows.
    cnf = [(c, [node], D & ~m) for (c, _, node, m) in atoms if (m & K) == K]
    r = solve_cover(cnf, D)
    if r:
        results.append((not r[2], r[0], len(r[1]), 0, "cnf", r[1]))
    dnf = [(c, [node], m & K) for (c, _, node, m) in atoms if (m & D) == 0]
    r = solve_cover(dnf, K)
    if r:
        results.append((not r[2], r[0], len(r[1]), 1, "dnf", r[1]))
    exact = [x for x in results if not x[0]]
    if not exact:
        def dnf_rank(a):
            c, i, _, m = a
            return (popcount(m & D) / (popcount(m & K) + 1.0), c, i)

        def cnf_rank(a):
            c, i, _, m = a
            return (popcount(K & ~m) / (popcount(D & ~m) + 1.0), c, i)

        top_d = sorted([a for a in atoms if a[3] & K], key=dnf_rank)[:300]
        top_c = sorted([a for a in atoms if D & ~a[3]], key=cnf_rank)[:300]
        cnf2 = list(cnf)
        dnf2 = list(dnf)
        for top, want in ((top_c, "c"), (top_d, "d")):
            top = sorted(top, key=lambda a: a[1])
            for a in range(len(top)):
                ca, _, na, ma = top[a]
                for b in range(a + 1, len(top)):
                    cb, _, nb, mb = top[b]
                    if want == "c":
                        mo = ma | mb
                        if (mo & K) == K and (ma & K) != K and (mb & K) != K:
                            cnf2.append((ca + cb + 50, [na, nb], D & ~mo))
                    else:
                        mi = ma & mb
                        if (mi & D) == 0 and (ma & D) != 0 and (mb & D) != 0:
                            dnf2.append((ca + cb + 50, [na, nb], mi & K))

        r = solve_cover(cnf2, D)
        if r:
            results.append((not r[2], r[0], len(r[1]), 0, "cnf", r[1]))
        r = solve_cover(dnf2, K)
        if r:
            results.append((not r[2], r[0], len(r[1]), 1, "dnf", r[1]))
    if not results:
        return None
    results.sort(key=lambda x: x[:4])
    best = results[0]
    groups = widen_thresholds(best[4], best[5], raw_atoms, K, D, ALL, rows)
    node = build_node(best[4], groups)
    return materialize(node)


# ---------------------------------------------------------------------------
# Top-level inference
# ---------------------------------------------------------------------------

def infer_spec(fmt, in_cols, in_rows, out_cols, out_rows):
    is_text = fmt in ("csv", "tsv")
    n_in, n_out = len(in_rows), len(out_rows)
    if n_out > n_in:
        out_rows = out_rows[:n_in]
        n_out = n_in
    if n_out == n_in:
        align = list(range(n_in))
        columns = infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text)
    else:
        sim = basic_similarity(in_cols, in_rows, out_cols, out_rows)
        align = align_dp(n_in, n_out, sim)
        columns = infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text)
        for _ in range(2):
            sim2 = exact_similarity(columns, in_rows, out_rows)
            align2 = align_dp(n_in, n_out, sim2)
            if align2 == align:
                break
            align = align2
            columns = infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text)
    filt = infer_filter(in_cols, in_rows, set(align))
    return {"format": fmt, "input_columns": list(in_cols), "columns": columns, "filter": filt}


def check_spec(spec, in_rows, out_rows):
    got = [RT["_transform_row"](spec, r) for r in in_rows if _filt(spec["filter"], r)]
    if len(got) != len(out_rows):
        return False, "row count %d != %d" % (len(got), len(out_rows))
    for i, (g, o) in enumerate(zip(got, out_rows)):
        for k in o:
            if not _eq(g.get(k), o.get(k)):
                return False, "row %d column %r: %r != %r" % (i, k, g.get(k), o.get(k))
    return True, "ok"


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

def spec_json(spec, indent=None):
    return json.dumps(spec, ensure_ascii=True, indent=indent, separators=(",", ": ") if indent else (",", ":"),
                      allow_nan=False)


def gen_python(spec, module):
    text = spec_json(spec, indent=1)
    header = ('# -*- coding: utf-8 -*-\n'
              '"""Generated by dynamic_buffer.py for module %r. Do not edit."""\n' % module)
    return (header + "import json as _json_spec\n\nSPEC = _json_spec.loads(%r)\n" % text
            + RT_PY_CORE + RT_PY_IO)


def gen_js(spec, module):
    text = spec_json(spec, indent=1)
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    header = '"use strict";\n// Generated by dynamic_buffer.py for module %s. Do not edit.\n' % json.dumps(module)
    return header + "const SPEC = " + text + ";\n" + RT_JS


def find_sample(sample_dir):
    for ext in SUPPORTED_EXTS:
        ip = os.path.join(sample_dir, "input." + ext)
        op = os.path.join(sample_dir, "output." + ext)
        if os.path.isfile(ip) and os.path.isfile(op):
            return ext, ip, op
    raise SystemExit("error: sample directory must contain input.{ext} and output.{ext} "
                     "with ext in %s" % (", ".join(SUPPORTED_EXTS),))


def write_file(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a DynamicPreprocessor module from a sample.")
    ap.add_argument("module_name")
    ap.add_argument("--output", required=True)
    ap.add_argument("--sample", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--python", action="store_true")
    g.add_argument("--javascript", action="store_true")
    args = ap.parse_args(argv)

    fmt, ip, op = find_sample(args.sample)
    in_cols, in_rows = load_file(ip, fmt)
    out_cols, out_rows = load_file(op, fmt)
    spec = infer_spec(fmt, in_cols, in_rows, out_cols, out_rows)
    ok, msg = check_spec(spec, in_rows, out_rows)
    if not ok:
        sys.stderr.write("warning: inferred transformation does not exactly reproduce the sample: %s\n" % msg)

    module = args.module_name
    if args.python:
        pkg = os.path.join(args.output, module)
        write_file(os.path.join(pkg, "__init__.py"), gen_python(spec, module))
    else:
        code = gen_js(spec, module)
        pkg = os.path.join(args.output, module)
        write_file(os.path.join(pkg, "index.js"), code)
        write_file(os.path.join(pkg, "package.json"),
                   json.dumps({"name": module, "version": "1.0.0", "main": "index.js"}, indent=2) + "\n")
        write_file(os.path.join(args.output, module + ".js"),
                   '"use strict";\nmodule.exports = require("./%s/index.js");\n' % module.replace('"', '\\"'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
