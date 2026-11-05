#!/usr/bin/env python3
"""dynamic_buffer.py - infer a transformation from a sample input/output pair
and generate a streaming ``DynamicPreprocessor`` module (Python package or
Node.js module) that applies it.

Supported transformations:
  * row filters: CNF/DNF of column-vs-constant comparisons, column equality,
    and neighbor predicates on the previous / next input row (one row of
    lookahead, e.g. "drop a row whose next row is a duplicate", "keep only the
    first / last row of a run");
  * per-row columns: copy/rename, constants, linear, string cleanup, affixes,
    splits, concatenation, two-column arithmetic;
  * stateful columns: prefix (cumulative) and sliding-window (W <= 64)
    sum / mean / min / max / predicate counts with ``a * S + b``, counters and
    segment ids (increment on a predicate, a value change or a threshold
    crossing), cyclic labels, rule-driven state machines (<= 16 labels),
    lag and difference to the previous row.  Each is computed either over all
    input rows or over the rows that survive the filter.

The generated module keeps only the state it needs (aggregates, window tails,
previous row, current label, the row awaiting lookahead) and, with
``cache_dir``, checkpoints it at every buffer boundary so runs can stop and
resume anywhere with output identical to a single pass.

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


def _filt(f, row, prev=None, nxt=None):
    if f is None:
        return True
    k = f['k']
    if k == 'and':
        for a in f['args']:
            if not _filt(a, row, prev, nxt):
                return False
        return True
    if k == 'or':
        for a in f['args']:
            if _filt(a, row, prev, nxt):
                return True
        return False
    if k == 'const':
        return f['v']
    if k == 'edge':
        if prev is None:
            return False
        c = bool(_filt(f['arg'], row))
        p = bool(_filt(f['arg'], prev))
        m = f['mode']
        if m == 'rise':
            return c and not p
        if m == 'fall':
            return p and not c
        return c != p
    if k == 'same':
        nb = prev if f['rel'] < 0 else nxt
        r = nb is not None and _eq(row.get(f['col']), nb.get(f['col']))
        return r if f['op'] == '==' else not r
    if k == 'colcmp':
        r = _eq(row.get(f['a']), row.get(f['b']))
        return r if f['op'] == '==' else not r
    rel = f.get('rel', 0)
    if rel:
        src = prev if rel < 0 else nxt
        v = None if src is None else src.get(f['col'])
    else:
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


def _isint(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _acc_new():
    # [phase(0 = ints only so far, 1 = float), int partial, float sum, compensation]
    return [0, 0, 0.0, 0.0]


def _acc_add(acc, x, neu):
    """Add x to an accumulator.  neu=False: plain left-to-right ``s + x``;
    neu=True: the Neumaier-compensated algorithm of CPython 3.12+ sum()."""
    if not neu:
        acc[1] = acc[1] + x
        return
    if acc[0] == 0:
        if _isint(x):
            acc[1] = acc[1] + x
            return
        acc[0] = 1
        acc[2] = acc[1] + x
        acc[3] = 0.0
        return
    if _isint(x):
        acc[2] = acc[2] + float(x)
        return
    x = float(x)
    f = acc[2]
    s = f + x
    if abs(f) >= abs(x):
        acc[3] += (f - s) + x
    else:
        acc[3] += (x - s) + f
    acc[2] = s


def _acc_val(acc, neu):
    if not neu or acc[0] == 0:
        return acc[1]
    f = acc[2]
    c = acc[3]
    if c and c - c == 0:
        f = f + c
    return f


def _st_init(t):
    k = t['kind']
    if k == 'agg':
        if t['w']:
            return {'win': [], 'n': 0}
        return {'a': _acc_new(), 'c': 0, 'm': None, 'n': 0}
    if k == 'counter':
        return {'c': 0}
    if k == 'lag':
        return {'h': []}
    if k == 'diff':
        return {'p': None, 'n': 0}
    if k == 'fsm':
        return {'v': t['init']}
    return {}


def _st_step(t, st, raw, prev):
    """Advance one stateful column by one row; return the series value."""
    k = t['kind']
    if k == 'agg':
        fn = t['fn']
        neu = t.get('acc') == 'neu'
        if fn == 'count':
            x = 1 if _filt(t['cond'], raw, prev) else 0
        else:
            x = _num(raw.get(t['src']))
        st['n'] += 1
        w = t['w']
        if w:
            win = st['win']
            win.append(x)
            if len(win) > w:
                del win[0]
            acc = _acc_new()
            c = 0
            m = None
            for v in win:
                if v is None:
                    continue
                _acc_add(acc, v, neu)
                c += 1
                if m is None or (fn == 'min' and v < m) or (fn == 'max' and v > m):
                    m = v
            s = _acc_val(acc, neu)
        else:
            if x is not None:
                _acc_add(st['a'], x, neu)
                st['c'] += 1
                m = st['m']
                if m is None or (fn == 'min' and x < m) or (fn == 'max' and x > m):
                    st['m'] = x
            s = _acc_val(st['a'], neu)
            c = st['c']
            m = st['m']
        if t.get('full') and st['n'] < w:
            return None
        if fn == 'sum' or fn == 'count':
            return s
        if fn == 'mean':
            return s / c if c else None
        return m
    if k == 'counter':
        if _filt(t['cond'], raw, prev):
            st['c'] += 1
        return st['c']
    if k == 'lag':
        h = st['h']
        kk = t['k']
        r = h[0] if len(h) >= kk else None
        has = len(h) >= kk
        h.append(raw.get(t['src']))
        if len(h) > kk:
            del h[0]
        return [r] if has else None
    if k == 'diff':
        x = _num(raw.get(t['src']))
        p = st['p']
        st['n'] += 1
        st['p'] = x
        if x is None or p is None:
            return None
        return x - p
    if k == 'fsm':
        for cond, label in t['rules']:
            if _filt(cond, raw, prev):
                st['v'] = label
                break
        return st['v']
    return None


def _st_out(t, s):
    if s is None:
        return t['none']
    k = t['kind']
    if k == 'lag':
        return s[0]
    if k == 'fsm':
        return s
    labels = t.get('labels')
    if labels is not None:
        return labels[s % len(labels)]
    p = t.get('post')
    if p is None:
        return s
    return _apply(p, {'_s': s})


def _new_state(spec):
    return {'prev': None, 'pk': None, 'pend': None, 'hp': False,
            'st': [(_st_init(t) if t['type'] == 'st' else None) for _, t in spec['columns']]}


class _Engine(object):
    """Row-at-a-time state machine: neighbor-aware filtering (one row of
    lookahead) plus stateful columns.  All state lives in ``self.s`` which is
    plain JSON so it can be checkpointed and restored."""

    def __init__(self, spec, state=None):
        self.spec = spec
        self.cols = spec['columns']
        self.filt = spec['filter']
        self.look = bool(spec.get('lookahead'))
        self.s = _new_state(spec) if state is None else state

    def push(self, raw):
        s = self.s
        if not self.look:
            return self._proc(raw, None)
        out = self._proc(s['pend'], raw) if s['hp'] else []
        s['pend'] = raw
        s['hp'] = True
        return out

    def finish(self):
        s = self.s
        if self.look and s['hp']:
            r = s['pend']
            s['pend'] = None
            s['hp'] = False
            return self._proc(r, None)
        return []

    def _proc(self, raw, nxt):
        s = self.s
        prev = s['prev']
        keep = _filt(self.filt, raw, prev, nxt)
        sts = s['st']
        vals = {}
        for i, (name, t) in enumerate(self.cols):
            if t['type'] == 'st' and t['scope'] == 'all':
                vals[i] = _st_out(t, _st_step(t, sts[i], raw, prev))
        s['prev'] = raw
        if not keep:
            return []
        pk = s['pk']
        out = {}
        for i, (name, t) in enumerate(self.cols):
            if t['type'] == 'st':
                if t['scope'] != 'all':
                    vals[i] = _st_out(t, _st_step(t, sts[i], raw, pk))
                out[name] = vals[i]
            else:
                out[name] = _apply(t, raw)
        s['pk'] = raw
        return [out]


def _run_engine(spec, rows):
    eng = _Engine(spec)
    out = []
    for r in rows:
        out.extend(eng.push(r))
    out.extend(eng.finish())
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

    buffer    -- maximum number of input rows read and processed at once.
    cache_dir -- optional directory for caching / resuming.  Besides the
                 consumed-row counter, a checkpoint of the streaming state
                 (running aggregates, window tails, state-machine labels,
                 previous row and the row awaiting its lookahead) is stored
                 at every buffer boundary so a later run resumes exactly
                 where the previous one stopped.
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
        return _Engine(dict(SPEC, filter=None, lookahead=False))._proc(row, None)[0]

    def _compute(self, path, ck=None, save=None):
        size = self.buffer
        eng = _Engine(SPEC, ck['state'] if ck else None)
        n_in = ck['in'] if ck else 0
        emitted = ck['out'] if ck else 0
        it = iter(_read_raw(path))
        skipped = 0
        while skipped < n_in:
            try:
                next(it)
            except StopIteration:
                break
            skipped += 1
        while True:
            if save is not None:
                save(n_in, emitted, eng.s)
            batch = []
            for raw in it:
                batch.append(raw)
                if len(batch) >= size:
                    break
            if not batch:
                break
            out = []
            for raw in batch:
                out.extend(eng.push(raw))
            n_in += len(batch)
            batch = []
            emitted += len(out)
            for row in out:
                yield row
        out = eng.finish()
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
        state_p = _os.path.join(d, 'state.json')
        st = _os.stat(path)
        meta = {'path': apath, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'spec': _SPEC_ID}
        old = None
        try:
            with open(meta_p, 'r', encoding='utf-8') as f:
                old = _json.load(f)
        except (OSError, ValueError):
            old = None
        if old != meta:
            for p in (prog_p, rows_p, state_p):
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
        ck = None
        if not complete and start > 0:
            try:
                with open(state_p, 'r', encoding='utf-8') as f:
                    ck = _json.load(f)
                if not (isinstance(ck, dict) and ck.get('spec') == _SPEC_ID
                        and 0 <= ck['out'] <= start and ck['in'] >= 0):
                    ck = None
            except (OSError, ValueError, KeyError, TypeError):
                ck = None

        def save(n_in, emitted, state):
            tmp = state_p + '.%d.tmp' % _os.getpid()
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump({'spec': _SPEC_ID, 'in': n_in, 'out': emitted, 'state': state}, f)
            _os.replace(tmp, state_p)

        source = self._read_cached(rows_p) if complete else self._compute(path, ck, save)
        tmp_rows = rows_p + '.%d.tmp' % _os.getpid()
        tf = None
        pf = open(prog_p, 'r+b' if _os.path.exists(prog_p) else 'w+b')
        try:
            if build:
                tf = open(tmp_rows, 'w', encoding='utf-8')
            wbuf = []
            idx = ck['out'] if ck else 0
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
            try:
                _os.remove(state_p)
            except OSError:
                pass
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

// Python round(y, d) for d > 0: nearest decimal, exact ties to even.
function _roundPos(y, d) {
  const a = Math.abs(y);
  if (!(a < 1e21)) return y;
  const s = a.toFixed(Math.min(100, d + 40));
  const dot = s.indexOf(".");
  const rest = s.slice(dot + 1 + d);
  if (rest[0] === "5" && /^50*$/.test(rest)) {
    const kept = s.slice(0, dot + 1 + d);
    const last = kept.charCodeAt(kept.length - 1) - 48;
    let r = Number(kept);
    if (last % 2 !== 0) {
      // Build the incremented decimal string exactly.
      const digits = kept.replace(".", "").split("");
      let i = digits.length - 1;
      while (i >= 0) {
        if (digits[i] === "9") {
          digits[i] = "0";
          i--;
        } else {
          digits[i] = String.fromCharCode(digits[i].charCodeAt(0) + 1);
          break;
        }
      }
      let str = digits.join("");
      if (i < 0) str = "1" + str;
      r = Number(str.slice(0, str.length - d) + "." + str.slice(str.length - d));
    }
    return y < 0 ? -r : r;
  }
  const r = Number(a.toFixed(d));
  return y < 0 ? -r : r;
}

function _round(y, d) {
  if (d === null || d === undefined) return y;
  if (!isFinite(y)) return y;
  if (d === 0) return _roundHalfEven0(y);
  if (d > 0) return _roundPos(y, Math.min(d, 60));
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
  if (k === "s_fixed") {
    if (!isFinite(y) || Math.abs(y) >= 1e21) return y.toFixed(fmt[1]);
    let s = _roundPos(y, fmt[1]).toFixed(fmt[1]);
    if ((y < 0 || Object.is(y, -0)) && s[0] !== "-") s = "-" + s;
    return s;
  }
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

function _filt(f, row, prev, nxt) {
  if (f === null || f === undefined) return true;
  if (prev === undefined) prev = null;
  if (nxt === undefined) nxt = null;
  const k = f.k;
  if (k === "and") {
    for (const a of f.args) if (!_filt(a, row, prev, nxt)) return false;
    return true;
  }
  if (k === "or") {
    for (const a of f.args) if (_filt(a, row, prev, nxt)) return true;
    return false;
  }
  if (k === "const") return f.v;
  if (k === "edge") {
    if (prev === null) return false;
    const c = !!_filt(f.arg, row, null, null);
    const p = !!_filt(f.arg, prev, null, null);
    if (f.mode === "rise") return c && !p;
    if (f.mode === "fall") return p && !c;
    return c !== p;
  }
  if (k === "same") {
    const nb = f.rel < 0 ? prev : nxt;
    const r = nb !== null && _eq(_get(row, f.col), _get(nb, f.col));
    return f.op === "==" ? r : !r;
  }
  if (k === "colcmp") {
    const r = _eq(_get(row, f.a), _get(row, f.b));
    return f.op === "==" ? r : !r;
  }
  const rel = f.rel || 0;
  let v;
  if (rel) {
    const src = rel < 0 ? prev : nxt;
    v = src === null ? null : _get(src, f.col);
  } else v = _get(row, f.col);
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

// Whether Python would see this value as an int (vs float) after _num().
function _numIsInt(row, c) {
  const v = _get(row, c);
  if (typeof v === "string") return INT_RE.test(v);
  if (typeof v === "number") return Number.isInteger(v) && !_isFloatKey(row, c);
  return false;
}

function _accNew() {
  return [0, 0, 0.0, 0.0];
}

// neu=false: plain left-to-right s + x; neu=true: CPython 3.12+ sum() (Neumaier).
function _accAdd(acc, x, isInt, neu) {
  if (!neu) {
    acc[1] = acc[1] + x;
    return;
  }
  if (acc[0] === 0) {
    if (isInt) {
      acc[1] = acc[1] + x;
      return;
    }
    acc[0] = 1;
    acc[2] = acc[1] + x;
    acc[3] = 0.0;
    return;
  }
  if (isInt) {
    acc[2] = acc[2] + x;
    return;
  }
  const f = acc[2];
  const s = f + x;
  if (Math.abs(f) >= Math.abs(x)) acc[3] += f - s + x;
  else acc[3] += x - s + f;
  acc[2] = s;
}

function _accVal(acc, neu) {
  if (!neu || acc[0] === 0) return acc[1];
  let f = acc[2];
  const c = acc[3];
  if (c && c - c === 0) f = f + c;
  return f;
}

function _stInit(t) {
  const k = t.kind;
  if (k === "agg") return t.w ? { win: [], n: 0 } : { a: _accNew(), c: 0, m: null, n: 0 };
  if (k === "counter") return { c: 0 };
  if (k === "lag") return { h: [] };
  if (k === "diff") return { p: null, n: 0 };
  if (k === "fsm") return { v: t.init };
  return {};
}

function _stStep(t, st, raw, prev) {
  const k = t.kind;
  if (k === "agg") {
    const fn = t.fn;
    const neu = t.acc === "neu";
    let x, xi;
    if (fn === "count") {
      x = _filt(t.cond, raw, prev, null) ? 1 : 0;
      xi = true;
    } else {
      x = _num(_get(raw, t.src));
      xi = x !== null && _numIsInt(raw, t.src);
    }
    st.n += 1;
    const w = t.w;
    let s, c, m;
    if (w) {
      const win = st.win;
      win.push(x === null ? null : [x, xi]);
      if (win.length > w) win.shift();
      const acc = _accNew();
      c = 0;
      m = null;
      for (const e of win) {
        if (e === null) continue;
        const v = e[0];
        _accAdd(acc, v, e[1], neu);
        c += 1;
        if (m === null || (fn === "min" && v < m) || (fn === "max" && v > m)) m = v;
      }
      s = _accVal(acc, neu);
    } else {
      if (x !== null) {
        _accAdd(st.a, x, xi, neu);
        st.c += 1;
        const mm = st.m;
        if (mm === null || (fn === "min" && x < mm) || (fn === "max" && x > mm)) st.m = x;
      }
      s = _accVal(st.a, neu);
      c = st.c;
      m = st.m;
    }
    if (t.full && st.n < w) return null;
    if (fn === "sum" || fn === "count") return s;
    if (fn === "mean") return c ? s / c : null;
    return m;
  }
  if (k === "counter") {
    if (_filt(t.cond, raw, prev, null)) st.c += 1;
    return st.c;
  }
  if (k === "lag") {
    const h = st.h;
    const kk = t.k;
    const has = h.length >= kk;
    const r = has ? h[0] : null;
    h.push(_get(raw, t.src));
    if (h.length > kk) h.shift();
    return has ? [r] : null;
  }
  if (k === "diff") {
    const x = _num(_get(raw, t.src));
    const p = st.p;
    st.n += 1;
    st.p = x;
    if (x === null || p === null) return null;
    return x - p;
  }
  if (k === "fsm") {
    for (const [cond, label] of t.rules) {
      if (_filt(cond, raw, prev, null)) {
        st.v = label;
        break;
      }
    }
    return st.v;
  }
  return null;
}

function _stOut(t, s) {
  if (s === null || s === undefined) return t.none === undefined ? null : t.none;
  const k = t.kind;
  if (k === "lag") return s[0];
  if (k === "fsm") return s;
  if (t.labels !== undefined && t.labels !== null) return t.labels[s % t.labels.length];
  if (t.post === undefined || t.post === null) return s;
  return _apply(t.post, { _s: s });
}

function _newState() {
  return {
    prev: null,
    pk: null,
    pend: null,
    hp: false,
    st: SPEC.columns.map(([, t]) => (t.type === "st" ? _stInit(t) : null)),
  };
}

class _Engine {
  constructor(spec, state) {
    this.cols = spec.columns;
    this.filt = spec.filter;
    this.look = !!spec.lookahead;
    this.s = state ? state : _newState();
  }
  push(raw) {
    const s = this.s;
    if (!this.look) return this._proc(raw, null);
    const out = s.hp ? this._proc(s.pend, raw) : [];
    s.pend = raw;
    s.hp = true;
    return out;
  }
  finish() {
    const s = this.s;
    if (this.look && s.hp) {
      const r = s.pend;
      s.pend = null;
      s.hp = false;
      return this._proc(r, null);
    }
    return [];
  }
  _proc(raw, nxt) {
    const s = this.s;
    const prev = s.prev;
    const keep = _filt(this.filt, raw, prev, nxt);
    const sts = s.st;
    const vals = new Array(this.cols.length);
    for (let i = 0; i < this.cols.length; i++) {
      const t = this.cols[i][1];
      if (t.type === "st" && t.scope === "all") vals[i] = _stOut(t, _stStep(t, sts[i], raw, prev));
    }
    s.prev = raw;
    if (!keep) return [];
    const pk = s.pk;
    const out = {};
    for (let i = 0; i < this.cols.length; i++) {
      const [name, t] = this.cols[i];
      if (t.type === "st") {
        if (t.scope !== "all") vals[i] = _stOut(t, _stStep(t, sts[i], raw, pk));
        out[name] = vals[i];
      } else out[name] = _apply(t, raw);
    }
    s.pk = raw;
    return [out];
  }
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

function* _compute(path, size, ck, save) {
  const eng = new _Engine(SPEC, ck ? ck.state : null);
  let nIn = ck ? ck.in : 0;
  let emitted = ck ? ck.out : 0;
  const it = _readRaw(path)[Symbol.iterator]();
  let skipped = 0;
  while (skipped < nIn) {
    if (it.next().done) break;
    skipped++;
  }
  try {
    for (;;) {
      if (save) save(nIn, emitted, eng.s);
      const batch = [];
      while (batch.length < size) {
        const r = it.next();
        if (r.done) break;
        batch.push(r.value);
      }
      if (!batch.length) break;
      const out = [];
      for (const raw of batch) for (const o of eng.push(raw)) out.push(o);
      nIn += batch.length;
      emitted += out.length;
      yield* out;
    }
    yield* eng.finish();
  } finally {
    if (typeof it.return === "function") it.return();
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
    yield* _compute(path, size, null, null);
    return;
  }
  const apath = nodePath.resolve(path);
  const key = crypto.createHash("sha256").update(SPEC_ID + "\0" + apath).digest("hex").slice(0, 40);
  const d = nodePath.join(String(cacheDir), key);
  fs.mkdirSync(d, { recursive: true });
  const metaP = nodePath.join(d, "meta.json");
  const progP = nodePath.join(d, "progress");
  const rowsP = nodePath.join(d, "rows.jsonl");
  const stateP = nodePath.join(d, "state.json");
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
    _rm(stateP);
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
  let ck = null;
  if (!complete && start > 0) {
    try {
      ck = _parseRow(fs.readFileSync(stateP, "utf8"));
      if (!(ck && ck.spec === SPEC_ID && ck.out >= 0 && ck.out <= start && ck.in >= 0)) ck = null;
    } catch (e) {
      ck = null;
    }
  }
  const save = (nIn, emitted, state) => {
    const tmp = stateP + "." + process.pid + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify({ spec: SPEC_ID, in: nIn, out: emitted, state: state }));
    fs.renameSync(tmp, stateP);
  };
  const source = complete ? _readCached(rowsP, size) : _compute(path, size, ck, save);
  const tmpRows = rowsP + "." + process.pid + ".tmp";
  let tf = null;
  const pf = fs.openSync(progP, fs.existsSync(progP) ? "r+" : "w+");
  try {
    if (build) tf = fs.openSync(tmpRows, "w");
    let wbuf = [];
    let idx = ck ? ck.out : 0;
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
    _rm(stateP);
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
  fn.transform = (row) => new _Engine(Object.assign({}, SPEC, { filter: null, lookahead: false }), null)._proc(row, null)[0];
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


def lossy_int(t, pairs):
    if t.get("type") not in ("linear", "arith"):
        return False
    raw_t = dict(t, round=None, fmt=["raw"])
    lossy = False
    for row, y in pairs:
        yn = y if is_number(y) else (_num(y) if isinstance(y, str) else None)
        if yn is None:
            continue
        if yn != int(yn):
            return False
        try:
            p = _apply(raw_t, row)
        except Exception:
            return False
        if is_number(p) and abs(p - yn) > 1e-9 * max(1.0, abs(yn)):
            lossy = True
    return lossy


def infer_column(oc, in_cols, pairs, is_text, final=True, stateful=None):
    ys = [y for _, y in pairs]
    if not pairs:
        if oc in in_cols:
            return {"type": "copy", "src": oc, "ops": []}
        return {"type": "missing", "value": "" if is_text else None}

    def first_fit(gen):
        for t in gen:
            if fits(t, pairs):
                return prefer_stateful(t)
        return None

    def prefer_stateful(t):
        # A numeric fit that only works thanks to rounding to integers is a
        # weak explanation (e.g. segment ids); an exact stateful one wins.
        if final and stateful is not None and lossy_int(t, pairs):
            s = stateful()
            if s:
                return s
        return t

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
    if not final:
        return {"type": "unfit"}
    if stateful is not None:
        t = stateful()
        if t:
            return t
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


def infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text, final=True):
    cols = []
    for oc in out_cols:
        pairs = [(in_rows[align[j]], out_rows[j].get(oc)) for j in range(len(out_rows))]
        ys = [y for _, y in pairs]

        def st(oc=oc, ys=ys):
            return infer_stateful(oc, in_cols, in_rows, align, ys, is_text)

        cols.append([oc, infer_column(oc, in_cols, pairs, is_text, final, st if final else None)])
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
                    oc = out_cols[idx]
                    if oc in in_cols and _eq(in_rows[i].get(oc), o.get(oc)):
                        score += 3
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
        if t.get("type") in ("unfit", "st"):
            continue
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
            # Thresholds on a strictly increasing row-id like column rarely
            # generalize; make them slightly more expensive.
            seqx = [p[0] for p in pts]
            gcost = 100
            if n >= 4 and len(seqx) == n and all(seqx[i] < seqx[i + 1] for i in range(n - 1)):
                gcost = 130
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
                    atoms.append((gcost, {"k": "gapnum", "col": c, "side": "ge", "lo": lo, "hi": hi, "g": g, "b": bnd}, mge))
                    atoms.append((gcost, {"k": "gapnum", "col": c, "side": "le", "lo": lo, "hi": hi, "g": g, "b": bnd}, mle))
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


def widen_thresholds(kind, groups, raw_atoms, K, D, ALL, rows, valid=None):
    """For each threshold atom, find the contiguous range of gaps that keep the
    formula consistent and replace it with an atom spanning that whole range."""
    masks = {}
    by_axis = {}
    for cost, node, mask in raw_atoms:
        masks[id(node)] = mask
        if node["k"] in ("gapnum", "gapstr"):
            by_axis.setdefault((node["k"], node["col"], node["side"]), {})[node["g"]] = (node, mask)
    groups = [list(g) for g in groups]
    if valid is None:
        def valid(m):
            return m == K

    def mask_of(a):
        m = masks.get(id(a))
        if m is None:
            m = 0
            for i, r in enumerate(rows):
                if _filt(a, r):
                    m |= (1 << i)
            masks[id(a)] = m
        return m

    base_ok = valid(formula_mask(kind, groups, mask_of, ALL))
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
                    if valid(formula_mask(kind, groups, mask_of, ALL)):
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
                if not valid(formula_mask(kind, groups, mask_of, ALL)):
                    groups[gi][ai] = a
            else:
                if a["side"] == "ge":
                    groups[gi][ai] = {"k": "str", "col": a["col"], "op": ">=", "v": a["hi"]}
                else:
                    groups[gi][ai] = {"k": "str", "col": a["col"], "op": "<=", "v": a["lo"]}
    return groups


def infer_formula(in_cols, rows, T, F, bounds_k=None, penalty=None, want_cost=False):
    """Find a CNF/DNF formula over atoms that is true on rows in mask T and
    false on rows in mask F (other rows are don't-care)."""
    n = len(rows)
    ALL = (1 << n) - 1
    K, D = T, F
    raw_atoms = gen_atoms(in_cols, rows, bounds_k)
    if penalty:
        raw_atoms = [(c + penalty(node), node, m) for (c, node, m) in raw_atoms]
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

    def valid(m):
        return (m & K) == K and (m & D) == 0

    groups = widen_thresholds(best[4], best[5], raw_atoms, K, D, ALL, rows, valid)
    node = materialize(build_node(best[4], groups))
    if want_cost:
        return node, not best[0], best[1]
    return node


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
    return infer_formula(in_cols, rows, K, D, K)


# ---------------------------------------------------------------------------
# Neighbor features (for neighbor-aware filters and state transitions)
# ---------------------------------------------------------------------------

FEAT = "\x01"


def augment(in_cols, rows, use_next=True, use_colcmp=True):
    """Add synthetic columns describing the previous / next row so the plain
    atom-based formula search can express neighbor-dependent predicates."""
    n = len(rows)
    cols = list(in_cols)
    extra = []
    for c in in_cols:
        extra.append(("p", c, FEAT + "p" + FEAT + c))
        if use_next:
            extra.append(("n", c, FEAT + "n" + FEAT + c))
        extra.append(("s-", c, FEAT + "s-" + FEAT + c))
        if use_next:
            extra.append(("s+", c, FEAT + "s+" + FEAT + c))
    pairs = []
    if use_colcmp and len(in_cols) <= 40:
        for i in range(len(in_cols)):
            for j in range(i + 1, len(in_cols)):
                a, b = in_cols[i], in_cols[j]
                if any(_eq(r.get(a), r.get(b)) for r in rows):
                    pairs.append((a, b, FEAT + "c" + FEAT + a + FEAT + b))
    out = []
    for i, r in enumerate(rows):
        d = dict(r)
        pv = rows[i - 1] if i > 0 else None
        nx = rows[i + 1] if i + 1 < n else None
        for tag, c, key in extra:
            if tag == "p":
                d[key] = None if pv is None else pv.get(c)
            elif tag == "n":
                d[key] = None if nx is None else nx.get(c)
            elif tag == "s-":
                d[key] = pv is not None and _eq(r.get(c), pv.get(c))
            else:
                d[key] = nx is not None and _eq(r.get(c), nx.get(c))
        for a, b, key in pairs:
            d[key] = _eq(r.get(a), r.get(b))
        out.append(d)
    cols = cols + [e[2] for e in extra] + [p[2] for p in pairs]
    return cols, out


def feat_penalty(node):
    col = node.get("col")
    if not isinstance(col, str) or not col.startswith(FEAT):
        return 0
    tag = col.split(FEAT)[1]
    if tag in ("p", "n"):
        return 60
    if tag in ("s-", "s+"):
        return 40
    return 50


def delift(node):
    k = node.get("k")
    if k in ("and", "or"):
        return {"k": k, "args": [delift(a) for a in node["args"]]}
    col = node.get("col")
    if not isinstance(col, str) or not col.startswith(FEAT):
        return node
    parts = col.split(FEAT)
    tag = parts[1]
    if tag in ("p", "n"):
        nn = dict(node)
        nn["col"] = FEAT.join(parts[2:])
        nn["rel"] = -1 if tag == "p" else 1
        return nn
    if k == "eq":
        positive = (node["op"] == "==") == (node["v"] is True)
    else:
        positive = True
    op = "==" if positive else "!="
    if tag in ("s-", "s+"):
        return {"k": "same", "col": FEAT.join(parts[2:]), "rel": -1 if tag == "s-" else 1, "op": op}
    return {"k": "colcmp", "a": parts[2], "b": parts[3], "op": op}


def uses_next(node):
    if node is None:
        return False
    k = node.get("k")
    if k in ("and", "or"):
        return any(uses_next(a) for a in node["args"])
    if k == "edge":
        return uses_next(node["arg"])
    return node.get("rel", 0) > 0


def node_mask(node, rows):
    m = 0
    n = len(rows)
    for i, r in enumerate(rows):
        if _filt(node, r, rows[i - 1] if i > 0 else None, rows[i + 1] if i + 1 < n else None):
            m |= (1 << i)
    return m


def find_predicate(in_cols, rows, T, F, with_prev=False, with_next=False):
    """Formula true on T, false on F (rows elsewhere don't care); None if no exact one."""
    n = len(rows)
    if T & F:
        return None
    if F == 0:
        return {"k": "const", "v": True}, 0
    if T == 0:
        return {"k": "const", "v": False}, 0
    bk = T if (T | F) == (1 << n) - 1 else None
    cands = []
    r = infer_formula(in_cols, rows, T, F, bk, want_cost=True)
    if r and r[1]:
        cands.append((r[2], 0, r[0]))
    if (with_prev or with_next) and not cands:
        acols, arows = augment(in_cols, rows, use_next=with_next)
        r = infer_formula(acols, arows, T, F, None, penalty=feat_penalty, want_cost=True)
        if r and r[1]:
            cands.append((r[2], 1, delift(r[0])))
    for cost, _, node in sorted(cands, key=lambda x: (x[0], x[1])):
        m = node_mask(node, rows)
        if (m & T) == T and (m & F) == 0:
            return node, cost
    return None


# ---------------------------------------------------------------------------
# Stateful column inference
# ---------------------------------------------------------------------------

_st_init = RT["_st_init"]
_st_step = RT["_st_step"]
_st_out = RT["_st_out"]


def y_numeric(y, is_text):
    if is_text:
        return _num(y) if isinstance(y, str) else None
    return y if is_number(y) else None


def sim_series(t, rows):
    st = _st_init(t)
    prev = None
    out = []
    for r in rows:
        out.append(_st_step(t, st, r, prev))
        prev = r
    return out


def max_decimals(ys, is_text):
    d = 0
    for y in ys:
        if isinstance(y, str):
            dd = decimals_of(y)
        elif isinstance(y, float):
            s = repr(y)
            dd = None if ("e" in s or "n" in s) else (len(s) - s.index(".") - 1 if "." in s else 0)
        else:
            dd = 0
        if dd is None:
            return None
        d = max(d, dd)
    return d


def precheck(xs, yv, ys, is_text):
    x0, y0 = xs[0], yv[0]
    j = max(range(len(xs)), key=lambda i: (abs(xs[i] - x0), -i))
    if xs[j] == x0:
        return False
    dec = max_decimals(ys, is_text)
    if dec is None:
        return True
    try:
        a = (yv[j] - y0) / (xs[j] - x0)
        b = y0 - a * x0
        base = 0.6 * (10.0 ** -dec)
        for x, y in zip(xs, yv):
            if abs(a * x + b - y) > 3 * base + 1e-6 * max(1.0, abs(y)):
                return False
    except (OverflowError, ZeroDivisionError, ValueError):
        return False
    return True


def fit_post(t, S, ys, is_text, strict=False):
    """Find post-processing (y = a*s + b, rounding, format) for series S."""
    nv = "" if is_text else None
    nones = [y for s, y in zip(S, ys) if s is None]
    if nones:
        nv = nones[0]
        if not all(_eq(v, nv) for v in nones):
            return None
    xs, ysub = [], []
    for s, y in zip(S, ys):
        if s is not None:
            xs.append(s)
            ysub.append(y)
    if len(xs) < 2 or all(x == xs[0] for x in xs):
        return None
    yv = [y_numeric(y, is_text) for y in ysub]
    if any(v is None for v in yv):
        return None
    if not precheck(xs, yv, ysub, is_text):
        return None
    pairs = [({"_s": x}, y) for x, y in zip(xs, ysub)]
    ident = {"type": "linear", "src": "_s", "a": 1, "b": 0, "round": None, "fmt": ["s_g" if is_text else "intify"]}
    if fits(ident, pairs):
        tt = dict(t)
        tt["post"] = ident
        tt["none"] = nv
        return tt
    for post in linear_candidates("_s", pairs, ysub, is_text):
        if strict and not (post["a"] == 1 and post["b"] == 0):
            return None
        tt = dict(t)
        tt["post"] = post
        tt["none"] = nv
        return tt
    return None


def verify_col(t, rows, obs, ys):
    S = sim_series(t, rows)
    for j, a in enumerate(obs):
        if not _eq(_st_out(t, S[a]), ys[j]):
            return False
    return True


def numeric_cols(in_cols, rows):
    out = []
    for c in in_cols:
        vals = [r.get(c) for r in rows]
        nn = [_num(v) for v in vals if v is not None and v != ""]
        if nn and sum(1 for x in nn if x is not None) * 2 >= len(vals) and \
                not all(isinstance(v, bool) for v in vals):
            out.append(c)
    return out


def base_spec(kind, scope, **kw):
    t = {"type": "st", "kind": kind, "scope": scope}
    t.update(kw)
    return t


class Scope(object):
    def __init__(self, name, rows, obs):
        self.name = name
        self.rows = rows
        self.obs = obs
        self.full = len(obs) == len(rows)
        self._conds = None

    def conds(self, in_cols):
        if self._conds is None:
            self._conds = cond_candidates(in_cols, self.rows)
        return self._conds


def edge_mask(m, mode, ALL):
    pm = (m << 1) & ALL
    if mode == "flip":
        return (m ^ pm) & ALL & ~1
    if mode == "rise":
        return m & ~pm & ALL & ~1
    if mode == "fall":
        return pm & ~m & ALL
    return m


def cond_candidates(in_cols, rows):
    n = len(rows)
    ALL = (1 << n) - 1
    raw = gen_atoms(in_cols, rows, None)
    cands = [(0, 0, ("const",), ALL)]
    seq = 1
    for idx, (c, node, m) in enumerate(raw):
        for extra, mode in ((0, None), (30, "flip"), (32, "rise"), (34, "fall")):
            cands.append((c + extra, seq, ("atom", idx, mode), edge_mask(m, mode, ALL)))
            seq += 1
    for c in in_cols:
        same = 0
        for i in range(1, n):
            if _eq(rows[i].get(c), rows[i - 1].get(c)):
                same |= (1 << i)
        cands.append((90, seq, ("same", c, "!="), ALL & ~same))
        cands.append((95, seq + 1, ("same", c, "=="), same))
        seq += 2
    cands.sort(key=lambda x: (x[0], x[1]))
    seen = set()
    out = []
    for c in cands:
        if c[3] in seen:
            continue
        seen.add(c[3])
        out.append(c)
    return raw, out


def build_cond(raw, cand, rows, ok):
    """Turn an enumerated candidate into a runtime condition node; numeric
    thresholds are widened over all equivalent gaps and rounded."""
    n = len(rows)
    ALL = (1 << n) - 1
    kind = cand[0]
    if kind == "const":
        return {"k": "const", "v": True}
    if kind == "same":
        return {"k": "same", "col": cand[1], "rel": -1, "op": cand[2]}
    idx, mode = cand[1], cand[2]
    node = raw[idx][1]
    base = materialize(node)
    if node["k"] == "gapnum":
        axis = {}
        for c, nd, m in raw:
            if nd["k"] == "gapnum" and nd["col"] == node["col"] and nd["side"] == node["side"]:
                axis[nd["g"]] = (nd, m)
        g0 = node["g"]
        lo_g = hi_g = g0
        while lo_g - 1 in axis and ok(edge_mask(axis[lo_g - 1][1], mode, ALL)):
            lo_g -= 1
        while hi_g + 1 in axis and ok(edge_mask(axis[hi_g + 1][1], mode, ALL)):
            hi_g += 1
        lo, hi = axis[lo_g][0]["lo"], axis[hi_g][0]["hi"]
        t = roundest(lo, hi)
        ge = node["side"] == "ge"
        if t == hi:
            op, v = (">=", hi) if ge else ("<", hi)
        elif t == lo:
            op, v = (">", lo) if ge else ("<=", lo)
        else:
            op, v = (">=", t) if ge else ("<", t)
        cand_node = {"k": "num", "col": node["col"], "op": op, "v": v}
        m = 0
        for i, r in enumerate(rows):
            if _filt(cand_node, r):
                m |= (1 << i)
        if ok(edge_mask(m, mode, ALL)):
            base = cand_node
    if mode is None:
        return base
    return {"k": "edge", "mode": mode, "arg": base}


def prefix_counts(m, obs):
    return [(m & ((2 << a) - 1)).bit_count() for a in obs]


def cycle_labels(cs, ys):
    if not cs:
        return None
    span = max(cs) - min(cs) + 1
    for m in range(2, 17):
        if span <= m:
            break
        mp = {}
        ok = True
        for c, y in zip(cs, ys):
            r = c % m
            if r in mp:
                if not _eq(mp[r], y):
                    ok = False
                    break
            else:
                mp[r] = y
        if ok and len(mp) == m:
            return [mp[r] for r in range(m)]
    return None


def infer_counter(in_cols, sc, ys, yn, is_text):
    """segment ids / prefix counts: value = a * (#rows with cond so far) + b."""
    if any(v is None for v in yn):
        return None
    rows, obs = sc.rows, sc.obs
    T = F = None
    if sc.full:
        T = F = 0
        step = None
        for j in range(1, len(yn)):
            d = yn[j] - yn[j - 1]
            if d == 0:
                F |= (1 << j)
            else:
                if step is None:
                    step = d
                elif d != step:
                    return None
                T |= (1 << j)
        if step is None:
            return None
    raw, cands = sc.conds(in_cols)

    def ok_full(m):
        return (m & T) == T and (m & F) == 0

    for cost, _, cand, m in cands:
        if sc.full:
            if not ok_full(m):
                continue
            ok = ok_full
        else:
            cs = prefix_counts(m, obs)
            j1 = None
            for j in range(1, len(cs)):
                if cs[j] != cs[0]:
                    j1 = j
                    break
            if j1 is None:
                continue
            step = (yn[j1] - yn[0]) / (cs[j1] - cs[0])
            good = True
            for j in range(len(cs)):
                if abs(yn[0] + step * (cs[j] - cs[0]) - yn[j]) > 1e-9 * max(1.0, abs(yn[j])):
                    good = False
                    break
            if not good:
                continue

            def ok(mm, _cs=cs):
                return prefix_counts(mm, obs) == _cs
        cond = build_cond(raw, cand, rows, ok)
        t0 = base_spec("counter", sc.name, cond=cond)
        S = sim_series(t0, rows)
        t = fit_post(t0, [S[a] for a in obs], ys, is_text)
        if t and verify_col(t, rows, obs, ys):
            return t
    return None


def infer_counter_formula(in_cols, sc, ys, yn, is_text):
    """Counter whose increment condition needs a compound (prev/current) formula."""
    if not sc.full or any(v is None for v in yn):
        return None
    T = F = 0
    step = None
    for j in range(1, len(yn)):
        d = yn[j] - yn[j - 1]
        if d == 0:
            F |= (1 << j)
        else:
            if step is None:
                step = d
            elif d != step:
                return None
            T |= (1 << j)
    if step is None:
        return None
    r = find_predicate(in_cols, sc.rows, T, F, with_prev=True)
    if not r:
        return None
    t0 = base_spec("counter", sc.name, cond=r[0])
    S = sim_series(t0, sc.rows)
    t = fit_post(t0, [S[a] for a in sc.obs], ys, is_text)
    if t and verify_col(t, sc.rows, sc.obs, ys):
        return t
    return None


def infer_cycle(in_cols, sc, ys):
    """Labels that cycle through a fixed list each time a condition fires."""
    rows, obs = sc.rows, sc.obs
    T = F = None
    if sc.full:
        T = F = 0
        for j in range(1, len(ys)):
            if _eq(ys[j], ys[j - 1]):
                F |= (1 << j)
            else:
                T |= (1 << j)
        if T == 0:
            return None
    raw, cands = sc.conds(in_cols)
    for cost, _, cand, m in cands:
        if sc.full and not ((m & T) == T and (m & F) == 0):
            continue
        cs = prefix_counts(m, obs)
        labels = cycle_labels(cs, ys)
        if labels is None or not plausible_labels(labels, len(ys)):
            continue
        if sc.full:
            def ok(mm):
                return (mm & T) == T and (mm & F) == 0
        else:
            def ok(mm, _cs=cs):
                return cycle_labels(prefix_counts(mm, obs), ys) == labels
        cond = build_cond(raw, cand, rows, ok)
        t = base_spec("counter", sc.name, cond=cond, labels=labels, none=None)
        if verify_col(t, rows, obs, ys):
            return t, cost
    return None


def infer_fsm_partial(in_cols, sc, ys):
    """FSM over all input rows when only the kept rows' labels are observed:
    each label's trigger must fire somewhere since the previous observed row
    and never on an observed row carrying another label."""
    rows, obs = sc.rows, sc.obs
    labels = []
    for y in ys:
        if not any(_eq(l, y) for l in labels):
            labels.append(y)
            if len(labels) > 16:
                return None
    if not plausible_labels(labels, len(ys)):
        return None
    raw, cands = sc.conds(in_cols)
    rules = []
    total = 0
    for L in labels:
        F = 0
        windows = []
        first = None
        for j, y in enumerate(ys):
            if _eq(y, L):
                if j > 0 and not _eq(ys[j - 1], L):
                    lo = obs[j - 1] + 1
                    windows.append(((1 << (obs[j] + 1)) - (1 << lo)))
                    if first is None:
                        first = j
            else:
                F |= (1 << obs[j])
        if not windows:
            continue

        def ok(m, F=F, windows=windows):
            return (m & F) == 0 and all(m & w for w in windows)

        found = None
        for cost, _, cand, m in cands:
            if cand[0] == "const" or not ok(m):
                continue
            found = (build_cond(raw, cand, rows, ok), cost)
            break
        if found is None:
            return None
        rules.append((first, [found[0], L]))
        total += found[1]
    rules.sort(key=lambda x: x[0])
    t = base_spec("fsm", sc.name, init=ys[0], rules=[r for _, r in rules], none=None)
    if verify_col(t, rows, obs, ys):
        return t, total
    return None


def plausible_labels(labels, n):
    """State labels are a small set of strings / small integers / booleans."""
    if len(labels) < 2 or len(labels) > 16 or n < 2 * len(labels):
        return False
    for l in labels:
        if isinstance(l, float) and not l.is_integer():
            return False
        if isinstance(l, str):
            x = _num(l)
            if x is not None and isinstance(x, float) and not x.is_integer():
                return False
    return True


def infer_fsm(in_cols, sc, ys):
    """Labels set by rules: 'if predicate P_k holds, switch to S_k, else keep'."""
    if not ys:
        return None
    if not sc.full:
        return infer_fsm_partial(in_cols, sc, ys)
    rows = sc.rows
    labels = []
    for y in ys:
        if not any(_eq(l, y) for l in labels):
            labels.append(y)
            if len(labels) > 16:
                return None
    if not plausible_labels(labels, len(ys)):
        return None
    rules = []
    total = 0
    for L in labels:
        E = F = 0
        first = None
        for j, y in enumerate(ys):
            if _eq(y, L):
                if j > 0 and not _eq(ys[j - 1], L):
                    E |= (1 << j)
                    if first is None:
                        first = j
            else:
                F |= (1 << j)
        if E == 0:
            continue
        r = find_predicate(in_cols, rows, E, F, with_prev=True)
        if not r:
            return None
        rules.append((first, [r[0], L]))
        total += r[1]
    rules.sort(key=lambda x: x[0])
    t = base_spec("fsm", sc.name, init=ys[0], rules=[r for _, r in rules], none=None)
    if verify_col(t, rows, sc.obs, ys):
        return t, total
    return None


def infer_lag(in_cols, sc, ys, kmax=64):
    rows, obs = sc.rows, sc.obs
    for k in range(1, min(kmax, len(rows) - 1) + 1):
        for c in in_cols:
            nv = None
            have_nv = False
            good = True
            hits = 0
            for j, a in enumerate(obs):
                if a - k < 0:
                    if not have_nv:
                        nv, have_nv = ys[j], True
                    elif not _eq(nv, ys[j]):
                        good = False
                        break
                else:
                    if not _eq(rows[a - k].get(c), ys[j]):
                        good = False
                        break
                    hits += 1
            if good and hits >= 2:
                t = base_spec("lag", sc.name, src=c, k=k, none=nv)
                if verify_col(t, rows, obs, ys):
                    return t
    return None


def approx_window(vals, w, fn, full):
    """Fast (approximate for floats) window series, used only to pre-screen."""
    out = []
    n = len(vals)
    ps = [0.0]
    pc = [0]
    for v in vals:
        ps.append(ps[-1] + (0.0 if v is None else float(v)))
        pc.append(pc[-1] + (0 if v is None else 1))
    for i in range(n):
        lo = 0 if not w else max(0, i + 1 - w)
        if full and w and i + 1 < w:
            out.append(None)
            continue
        c = pc[i + 1] - pc[lo]
        if fn == "sum":
            out.append(ps[i + 1] - ps[lo])
        elif fn == "mean":
            out.append((ps[i + 1] - ps[lo]) / c if c else None)
        else:
            seg = [v for v in vals[lo:i + 1] if v is not None]
            out.append((min(seg) if fn == "min" else max(seg)) if seg else None)
    return out


def try_agg(t, approx, sc, ys, yn, is_text, strict=False):
    S = [approx[a] for a in sc.obs]
    xs, yv, ysub = [], [], []
    for s, v, y in zip(S, yn, ys):
        if (s is None) != (v is None):
            return None
        if s is not None:
            xs.append(s)
            yv.append(v)
            ysub.append(y)
    if len(xs) < 2 or all(abs(x - xs[0]) < 1e-12 for x in xs):
        return None
    if not precheck(xs, yv, ysub, is_text):
        return None
    if t["kind"] != "agg" or t["fn"] in ("min", "max"):
        variants = [t]
    else:
        # Running totals are usually plain "s += x"; window sums usually come
        # from sum(window) (compensated in Python >= 3.12).  Try both.
        order = ("naive", "neu") if not t["w"] else ("neu", "naive")
        variants = [dict(t, acc=a) for a in order]
    for tv in variants:
        ex = sim_series(tv, sc.rows)
        tt = fit_post(tv, [ex[a] for a in sc.obs], ys, is_text, strict)
        if tt and verify_col(tt, sc.rows, sc.obs, ys):
            return tt
    return None


def infer_window_count(in_cols, sc, ys, yn, is_text):
    if not sc.full or any(v is None or v != int(v) for v in yn):
        return None
    y = [int(v) for v in yn]
    n = len(y)
    for w in range(2, min(64, n) + 1):
        P = []
        good = True
        for i in range(n):
            p = y[i] - (y[i - 1] if i > 0 else 0) + (P[i - w] if i - w >= 0 else 0)
            if p not in (0, 1):
                good = False
                break
            P.append(p)
        if not good or all(p == P[0] for p in P):
            continue
        T = 0
        for i, p in enumerate(P):
            if p:
                T |= (1 << i)
        F = ((1 << n) - 1) & ~T
        r = find_predicate(in_cols, sc.rows, T, F)
        if not r:
            continue
        t0 = base_spec("agg", sc.name, fn="count", src=None, cond=r[0], w=w, full=False)
        S = sim_series(t0, sc.rows)
        t = fit_post(t0, [S[a] for a in sc.obs], ys, is_text)
        if t and verify_col(t, sc.rows, sc.obs, ys):
            return t
    return None


def infer_stateful(oc, in_cols, in_rows, align, ys, is_text):
    if not ys or not in_rows:
        return None
    scopes = [Scope("all", in_rows, list(align))]
    if len(align) != len(in_rows):
        scopes.append(Scope("kept", [in_rows[a] for a in align], list(range(len(align)))))
    yn = [y_numeric(y, is_text) for y in ys]
    nn = [y for y, v in zip(ys, yn) if v is None]
    numeric = (sum(1 for v in yn if v is not None) >= 2 and
               all(_eq(y, nn[0]) for y in nn) and len(nn) * 2 <= len(ys) + 2)
    if numeric:
        ncols = {sc.name: numeric_cols(in_cols, sc.rows) for sc in scopes}
        series = {}

        def vals_of(sc, c):
            key = (sc.name, c)
            if key not in series:
                series[key] = [_num(r.get(c)) for r in sc.rows]
            return series[key]

        has_none = any(v is None for v in yn)

        def prefix_aggs(strict):
            for sc in scopes:
                for c in ncols[sc.name]:
                    v = vals_of(sc, c)
                    for fn in ("sum", "mean", "max", "min"):
                        t = base_spec("agg", sc.name, fn=fn, src=c, w=0, full=False)
                        r = try_agg(t, approx_window(v, 0, fn, False), sc, ys, yn, is_text, strict)
                        if r:
                            return r
            return None

        def windows(strict):
            for sc in scopes:
                for w in range(2, min(64, len(sc.rows)) + 1):
                    for c in ncols[sc.name]:
                        v = vals_of(sc, c)
                        for fn in ("sum", "mean", "max", "min"):
                            for full in ((False, True) if has_none else (False,)):
                                t = base_spec("agg", sc.name, fn=fn, src=c, w=w, full=full)
                                r = try_agg(t, approx_window(v, w, fn, full), sc, ys, yn, is_text, strict)
                                if r:
                                    return r
            return None

        def diffs(strict):
            for sc in scopes:
                for c in ncols[sc.name]:
                    v = vals_of(sc, c)
                    approx = [None] + [(v[i] - v[i - 1]) if v[i] is not None and v[i - 1] is not None else None
                                       for i in range(1, len(v))]
                    t = base_spec("diff", sc.name, src=c)
                    r = try_agg(t, approx, sc, ys, yn, is_text, strict)
                    if r:
                        return r
            return None

        steps = [
            lambda: prefix_aggs(True),                       # cumulative sum / mean / max / min
            lambda: next((r for r in (infer_counter(in_cols, sc, ys, yn, is_text) for sc in scopes) if r), None),
            lambda: windows(True),                           # sliding windows
            lambda: next((r for r in (infer_lag(in_cols, sc, ys) for sc in scopes) if r), None),
            lambda: diffs(True),
            lambda: next((r for r in (infer_window_count(in_cols, sc, ys, yn, is_text) for sc in scopes) if r), None),
            lambda: next((r for r in (infer_counter_formula(in_cols, sc, ys, yn, is_text) for sc in scopes) if r),
                         None),
            lambda: prefix_aggs(False),                      # affine variants: a * S + b
            lambda: windows(False),
            lambda: diffs(False),
        ]
        for step in steps:
            r = step()
            if r:
                return r
    else:
        for sc in scopes:
            r = infer_lag(in_cols, sc, ys)
            if r:
                return r
    # 6. label state machines
    for sc in scopes:
        best = None
        for i, f in enumerate((infer_fsm, infer_cycle)):
            r = f(in_cols, sc, ys)
            if r and (best is None or r[1] < best[1]):
                best = r
        if best:
            return best[0]
    return None


# ---------------------------------------------------------------------------
# Top-level inference
# ---------------------------------------------------------------------------

def filter_exact(node, rows, kept):
    n = len(rows)
    for i, r in enumerate(rows):
        k = _filt(node, r, rows[i - 1] if i > 0 else None, rows[i + 1] if i + 1 < n else None)
        if bool(k) != (i in kept):
            return False
    return True


def infer_row_filter(in_cols, in_rows, kept):
    n = len(in_rows)
    K = 0
    for i in kept:
        K |= (1 << i)
    D = ((1 << n) - 1) & ~K
    if D == 0:
        return None
    if K == 0:
        return {"k": "const", "v": False}
    best = None
    r = infer_formula(in_cols, in_rows, K, D, K, want_cost=True)
    if r is not None:
        best = (0 if filter_exact(r[0], in_rows, kept) else 1, r[2], 0, r[0])
    # Neighbor-aware filter: predicates may also look at the previous / next row.
    acols, arows = augment(in_cols, in_rows, use_next=True)
    r = infer_formula(acols, arows, K, D, K, penalty=feat_penalty, want_cost=True)
    if r is not None:
        node = delift(r[0])
        cand = (0 if filter_exact(node, in_rows, kept) else 1, r[2], 1, node)
        if best is None or cand[:3] < best[:3]:
            best = cand
    return None if best is None else best[3]


def infer_spec(fmt, in_cols, in_rows, out_cols, out_rows):
    is_text = fmt in ("csv", "tsv")
    n_in, n_out = len(in_rows), len(out_rows)
    if n_out > n_in:
        out_rows = out_rows[:n_in]
        n_out = n_in
    if n_out == n_in:
        align = list(range(n_in))
    else:
        sim = basic_similarity(in_cols, in_rows, out_cols, out_rows)
        align = align_dp(n_in, n_out, sim)
        columns = infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text, final=False)
        for _ in range(2):
            sim2 = exact_similarity(columns, in_rows, out_rows)
            align2 = align_dp(n_in, n_out, sim2)
            if align2 == align:
                break
            align = align2
            columns = infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text, final=False)
    columns = infer_columns(in_cols, in_rows, out_cols, out_rows, align, is_text, final=True)
    kept = set(align)
    filt = infer_row_filter(in_cols, in_rows, kept)
    return {"format": fmt, "input_columns": list(in_cols), "columns": columns, "filter": filt,
            "lookahead": uses_next(filt)}


def check_spec(spec, in_rows, out_rows):
    got = RT["_run_engine"](spec, in_rows)
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
