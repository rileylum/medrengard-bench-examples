#!/usr/bin/env python3
"""dynamic_buffer.py - infer a transformation from a sample input/output pair
and generate a streaming ``DynamicPreprocessor`` module that applies it, as a
Python package, a Node.js module, a C++ library module or a Rust crate.

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

Every target embeds the inferred spec (JSON) plus a runtime that interprets
it with identical semantics (Python's int/float typing, float repr, rounding,
csv-module parsing, Unicode strip/split/case mapping):

  --python      <output>/<module>/__init__.py
  --javascript  <output>/<module>.js (+ <output>/<module>/index.js)
  --cpp         <output>/<module>/dynamic_preprocessor.{h,cpp}, namespace
                <module>: DynamicPreprocessor(buffer[, cache_dir]),
                open(path), bool next(Row&)
  --rust        <output>/<module>/{Cargo.toml,src/lib.rs}, crate <module>,
                module ``api``: DynamicPreprocessor::new(buffer, cache_dir),
                open(path), Iterator<Item = Row>

The C++ / Rust runtimes use only the standard library.  Integers beyond the
64-bit range (Python bigints) are represented as doubles there.

Usage:
    python dynamic_buffer.py <module_name> --output <dir> --sample <dir> \
        (--python | --javascript | --cpp | --rust)
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

# ---------------------------------------------------------------------------
# Runtime (C++).  The header exposes the required API; the .cpp file is a
# line-by-line port of the Python runtime interpreting the embedded spec.
# ---------------------------------------------------------------------------

RT_CPP_H = r'''// Generated by dynamic_buffer.py for module @@MODULE@@. Do not edit.
#pragma once

#include <cstddef>
#include <map>
#include <memory>
#include <optional>
#include <string>

namespace @@NS@@ {

enum class ValueType {
    Null,
    Bool,
    Int,
    Double,
    String
};

// Only the *_value field matching `type` is meaningful.
struct Value {
    ValueType type = ValueType::Null;
    bool bool_value = false;
    long long int_value = 0;
    double double_value = 0.0;
    std::string string_value;
};

// Column name -> value (keys sorted lexicographically).
using Row = std::map<std::string, Value>;

// Streams a data file (same format / schema as the sample input), applying
// the inferred filter, per-row and stateful column transforms.
//
// `buffer` is the maximum number of input rows read and processed at once.
// With `cache_dir`, a checkpoint of the streaming state (aggregates, window
// tails, state-machine labels, previous row, the row awaiting lookahead) is
// stored at every buffer boundary, plus a consumed-row counter, so a later
// run (another process) resumes exactly where the previous one stopped.
class DynamicPreprocessor {
public:
    // No caching
    explicit DynamicPreprocessor(std::size_t buffer);

    // With caching
    DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir);

    ~DynamicPreprocessor();
    DynamicPreprocessor(DynamicPreprocessor&&) noexcept;
    DynamicPreprocessor& operator=(DynamicPreprocessor&&) noexcept;
    DynamicPreprocessor(const DynamicPreprocessor&) = delete;
    DynamicPreprocessor& operator=(const DynamicPreprocessor&) = delete;

    // Open a data file; resets the stream / state to the start of the file
    // (or to the resume point recorded in cache_dir).  Throws
    // std::runtime_error if the file cannot be opened.
    void open(const std::string& path);

    // Get next transformed row.  Returns true and writes into `out` if a row
    // is available; returns false when the stream is exhausted.
    bool next(Row& out);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace @@NS@@
'''

RT_CPP = r'''// Generated by dynamic_buffer.py for module @@MODULE@@. Do not edit.
//
// A streaming interpreter for the inferred transformation (embedded below as
// JSON).  Its semantics mirror the generated Python module exactly: Python
// number typing (int vs float), repr() of floats, round-half-even, Unicode
// whitespace for strip/split, csv-module parsing, etc.
#include "dynamic_preprocessor.h"

#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwctype>
#include <deque>
#include <locale.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace @@NS@@ {
namespace {

const char* const SPEC_ID = "@@SPECID@@";
const char* const SPEC_TEXT = R"@@DELIM@@(@@SPEC@@)@@DELIM@@";

// ---------------------------------------------------------------------------
// Dynamic (JSON-like) value with Python's int / float distinction
// ---------------------------------------------------------------------------

struct J {
    enum T { NUL, BOOL, INT, FLT, STR, ARR, OBJ };
    T t = NUL;
    bool b = false;
    long long i = 0;
    double f = 0.0;
    std::string s;
    std::vector<J> a;
    std::vector<std::pair<std::string, J>> o;

    static J boolean(bool v) { J j; j.t = BOOL; j.b = v; return j; }
    static J integer(long long v) { J j; j.t = INT; j.i = v; return j; }
    static J real(double v) { J j; j.t = FLT; j.f = v; return j; }
    static J str(std::string v) { J j; j.t = STR; j.s = std::move(v); return j; }
    static J arr() { J j; j.t = ARR; return j; }
    static J obj() { J j; j.t = OBJ; return j; }

    bool null() const { return t == NUL; }
    bool isnum() const { return t == INT || t == FLT; }
    double num() const { return t == INT ? static_cast<double>(i) : f; }
    const J* get(std::string_view k) const {
        for (const auto& p : o)
            if (p.first == k) return &p.second;
        return nullptr;
    }
    J* getm(std::string_view k) {
        for (auto& p : o)
            if (p.first == k) return &p.second;
        return nullptr;
    }
    void set(std::string_view k, J v) {
        if (J* p = getm(k)) *p = std::move(v);
        else o.emplace_back(std::string(k), std::move(v));
    }
    J& at(std::string_view k) {
        if (J* p = getm(k)) return *p;
        o.emplace_back(std::string(k), J());
        return o.back().second;
    }
};

const J& nullj() {
    static const J n;
    return n;
}

const J& field(const J& obj, std::string_view k) {
    const J* p = obj.get(k);
    return p ? *p : nullj();
}

bool truthy(const J& v) {
    switch (v.t) {
        case J::NUL: return false;
        case J::BOOL: return v.b;
        case J::INT: return v.i != 0;
        case J::FLT: return v.f != 0.0;
        case J::STR: return !v.s.empty();
        case J::ARR: return !v.a.empty();
        case J::OBJ: return !v.o.empty();
    }
    return false;
}

struct PyError {};  // OverflowError / ValueError / ZeroDivisionError

locale_t c_locale() {
    static locale_t loc = newlocale(LC_ALL_MASK, "C", (locale_t)0);
    return loc;
}

locale_t utf8_locale() {
    static locale_t loc = []() {
        locale_t l = newlocale(LC_CTYPE_MASK, "C.UTF-8", (locale_t)0);
        if (!l) l = newlocale(LC_CTYPE_MASK, "en_US.UTF-8", (locale_t)0);
        return l;
    }();
    return loc;
}

double parse_double(const std::string& s) {
    return strtod_l(s.c_str(), nullptr, c_locale());
}

// ---------------------------------------------------------------------------
// UTF-8 / Python string helpers
// ---------------------------------------------------------------------------

uint32_t u8dec(const std::string& s, size_t& i) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    int n = 0;
    uint32_t cp = c;
    if (c >= 0xF0 && c < 0xF8) { n = 3; cp = c & 0x07; }
    else if (c >= 0xE0) { n = (c < 0xF0) ? 2 : 0; cp = c & 0x0F; }
    else if (c >= 0xC0) { n = 1; cp = c & 0x1F; }
    if (n == 0 || i + n >= s.size()) { i += 1; return c; }
    for (int k = 1; k <= n; k++) {
        unsigned char d = static_cast<unsigned char>(s[i + k]);
        if ((d & 0xC0) != 0x80) { i += 1; return c; }
        cp = (cp << 6) | (d & 0x3F);
    }
    i += n + 1;
    return cp;
}

void u8enc(uint32_t cp, std::string& out) {
    if (cp < 0x80) out += static_cast<char>(cp);
    else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

bool py_space(uint32_t c) {
    return (c >= 9 && c <= 13) || (c >= 0x1C && c <= 0x20) || c == 0x85 || c == 0xA0 || c == 0x1680 ||
           (c >= 0x2000 && c <= 0x200A) || c == 0x2028 || c == 0x2029 || c == 0x202F || c == 0x205F ||
           c == 0x3000;
}

std::string py_strip(const std::string& s) {
    size_t i = 0, start = s.size(), end = 0;
    while (i < s.size()) {
        size_t p = i;
        uint32_t cp = u8dec(s, i);
        if (!py_space(cp)) {
            if (start == s.size()) start = p;
            end = i;
        }
    }
    if (start >= end) return std::string();
    return s.substr(start, end - start);
}

std::vector<std::string> py_split_ws(const std::string& s) {
    std::vector<std::string> out;
    size_t i = 0;
    std::string cur;
    bool in = false;
    while (i < s.size()) {
        size_t p = i;
        uint32_t cp = u8dec(s, i);
        if (py_space(cp)) {
            if (in) { out.push_back(cur); cur.clear(); in = false; }
        } else {
            cur.append(s, p, i - p);
            in = true;
        }
    }
    if (in) out.push_back(cur);
    return out;
}

std::vector<std::string> py_split(const std::string& s, const std::string& sep) {
    std::vector<std::string> out;
    if (sep.empty()) throw PyError();
    size_t start = 0;
    for (;;) {
        size_t pos = s.find(sep, start);
        if (pos == std::string::npos) break;
        out.push_back(s.substr(start, pos - start));
        start = pos + sep.size();
    }
    out.push_back(s.substr(start));
    return out;
}

bool is_alpha_cp(uint32_t cp) {
    if (cp < 0x80) return (cp >= 'a' && cp <= 'z') || (cp >= 'A' && cp <= 'Z');
    locale_t l = utf8_locale();
    return l ? iswalpha_l(static_cast<wint_t>(cp), l) != 0 : false;
}

std::string py_lower(const std::string& s) {
    std::vector<uint32_t> cps;
    for (size_t i = 0; i < s.size();) cps.push_back(u8dec(s, i));
    std::string out;
    locale_t l = utf8_locale();
    for (size_t k = 0; k < cps.size(); k++) {
        uint32_t cp = cps[k];
        if (cp < 0x80) {
            if (cp >= 'A' && cp <= 'Z') cp += 32;
        } else if (cp == 0x3A3) {
            bool before = k > 0 && is_alpha_cp(cps[k - 1]);
            bool after = k + 1 < cps.size() && is_alpha_cp(cps[k + 1]);
            cp = (before && !after) ? 0x3C2 : 0x3C3;
        } else if (cp == 0x130) {
            out += "i\xCC\x87";
            continue;
        } else if (l) {
            cp = static_cast<uint32_t>(towlower_l(static_cast<wint_t>(cp), l));
        }
        u8enc(cp, out);
    }
    return out;
}

std::string py_upper(const std::string& s) {
    std::string out;
    locale_t l = utf8_locale();
    for (size_t i = 0; i < s.size();) {
        uint32_t cp = u8dec(s, i);
        if (cp < 0x80) {
            if (cp >= 'a' && cp <= 'z') cp -= 32;
        } else if (cp == 0xDF) {
            out += "SS";
            continue;
        } else if (l) {
            cp = static_cast<uint32_t>(towupper_l(static_cast<wint_t>(cp), l));
        }
        u8enc(cp, out);
    }
    return out;
}

// ---------------------------------------------------------------------------
// Python number formatting
// ---------------------------------------------------------------------------

std::string py_repr(double v) {
    if (std::isnan(v)) return "nan";
    if (std::isinf(v)) return v > 0 ? "inf" : "-inf";
    if (v == 0) return std::signbit(v) ? "-0.0" : "0.0";
    char buf[64];
    auto r = std::to_chars(buf, buf + sizeof(buf), v, std::chars_format::scientific);
    std::string t(buf, r.ptr);
    std::string sign;
    if (!t.empty() && t[0] == '-') { sign = "-"; t = t.substr(1); }
    size_t e = t.find('e');
    std::string mant = t.substr(0, e);
    int exp = std::atoi(t.c_str() + e + 1);
    std::string digits;
    for (char c : mant)
        if (c != '.') digits += c;
    int L = static_cast<int>(digits.size());
    if (exp >= -4 && exp < 16) {
        if (exp >= 0) {
            if (L <= exp + 1) return sign + digits + std::string(exp + 1 - L, '0') + ".0";
            return sign + digits.substr(0, exp + 1) + "." + digits.substr(exp + 1);
        }
        return sign + "0." + std::string(-exp - 1, '0') + digits;
    }
    int ae = exp < 0 ? -exp : exp;
    std::string out = sign + digits.substr(0, 1);
    if (L > 1) out += "." + digits.substr(1);
    out += exp < 0 ? "e-" : "e+";
    if (ae < 10) out += "0";
    out += std::to_string(ae);
    return out;
}

std::string py_fixed(double v, long long d) {
    if (std::isnan(v)) return "nan";
    if (std::isinf(v)) return v > 0 ? "inf" : "-inf";
    if (d < 0) d = 0;
    if (d > 1000) d = 1000;
    std::vector<char> buf(static_cast<size_t>(d) + 400);
    int n = snprintf(buf.data(), buf.size(), "%.*f", static_cast<int>(d), v);
    if (n < 0) return std::string();
    if (static_cast<size_t>(n) >= buf.size()) {
        buf.resize(static_cast<size_t>(n) + 1);
        snprintf(buf.data(), buf.size(), "%.*f", static_cast<int>(d), v);
    }
    return std::string(buf.data(), static_cast<size_t>(n));
}

bool is_integer(double v) { return std::isfinite(v) && v == std::floor(v); }

// int(round(y)) for a float y
J round_to_int(double v) {
    if (!std::isfinite(v)) throw PyError();
    double r = std::nearbyint(v);
    if (std::fabs(r) < 9.2e18) return J::integer(static_cast<long long>(r));
    return J::real(r);
}

std::string int_str(const J& v) {
    if (v.t == J::INT) return std::to_string(v.i);
    return py_fixed(v.f, 0);
}

std::string py_str_repr_quote(const std::string& s) {
    bool sq = s.find('\'') != std::string::npos && s.find('"') == std::string::npos;
    char q = sq ? '"' : '\'';
    std::string out(1, q);
    for (char c : s) {
        if (c == q || c == '\\') { out += '\\'; out += c; }
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else out += c;
    }
    out += q;
    return out;
}

std::string py_repr_value(const J& v);

std::string py_str_value(const J& v) {
    switch (v.t) {
        case J::NUL: return "None";
        case J::BOOL: return v.b ? "True" : "False";
        case J::INT: return std::to_string(v.i);
        case J::FLT: return py_repr(v.f);
        case J::STR: return v.s;
        default: return py_repr_value(v);
    }
}

std::string py_repr_value(const J& v) {
    if (v.t == J::STR) return py_str_repr_quote(v.s);
    if (v.t == J::ARR) {
        std::string out = "[";
        for (size_t k = 0; k < v.a.size(); k++) {
            if (k) out += ", ";
            out += py_repr_value(v.a[k]);
        }
        return out + "]";
    }
    if (v.t == J::OBJ) {
        std::string out = "{";
        for (size_t k = 0; k < v.o.size(); k++) {
            if (k) out += ", ";
            out += py_str_repr_quote(v.o[k].first) + ": " + py_repr_value(v.o[k].second);
        }
        return out + "}";
    }
    return py_str_value(v);
}

// ---------------------------------------------------------------------------
// JSON parsing / serialization
// ---------------------------------------------------------------------------

struct StrSrc {
    const std::string& s;
    size_t pos = 0;
    explicit StrSrc(const std::string& str) : s(str) {}
    int peek() { return pos < s.size() ? static_cast<unsigned char>(s[pos]) : -1; }
    int get() { return pos < s.size() ? static_cast<unsigned char>(s[pos++]) : -1; }
};

struct FileSrc {
    FILE* f = nullptr;
    std::vector<char> buf;
    size_t pos = 0, len = 0;
    explicit FileSrc(FILE* fp) : f(fp), buf(1 << 16) {
        if (peek() == 0xEF) {
            fill_min(3);
            if (len - pos >= 3 && static_cast<unsigned char>(buf[pos + 1]) == 0xBB &&
                static_cast<unsigned char>(buf[pos + 2]) == 0xBF)
                pos += 3;
        }
    }
    ~FileSrc() {
        if (f) fclose(f);
    }
    FileSrc(const FileSrc&) = delete;
    FileSrc& operator=(const FileSrc&) = delete;
    void fill_min(size_t want) {
        if (len - pos >= want) return;
        if (pos > 0) {
            memmove(buf.data(), buf.data() + pos, len - pos);
            len -= pos;
            pos = 0;
        }
        while (len < want && f) {
            size_t n = fread(buf.data() + len, 1, buf.size() - len, f);
            if (n == 0) break;
            len += n;
        }
    }
    int peek() {
        if (pos >= len) {
            pos = len = 0;
            if (f) len = fread(buf.data(), 1, buf.size(), f);
            if (len == 0) return -1;
        }
        return static_cast<unsigned char>(buf[pos]);
    }
    int get() {
        int c = peek();
        if (c >= 0) pos++;
        return c;
    }
};

template <class S>
struct JsonParser {
    S& s;
    explicit JsonParser(S& src) : s(src) {}

    void ws() {
        for (;;) {
            int c = s.peek();
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') s.get();
            else break;
        }
    }

    bool lit(const char* w) {
        for (const char* p = w; *p; ++p)
            if (s.get() != static_cast<unsigned char>(*p)) return false;
        return true;
    }

    bool hex4(uint32_t& out) {
        out = 0;
        for (int k = 0; k < 4; k++) {
            int c = s.get();
            int d;
            if (c >= '0' && c <= '9') d = c - '0';
            else if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
            else return false;
            out = out * 16 + static_cast<uint32_t>(d);
        }
        return true;
    }

    bool string(std::string& out) {
        // opening quote already consumed
        for (;;) {
            int c = s.get();
            if (c < 0) return false;
            if (c == '"') return true;
            if (c != '\\') {
                out += static_cast<char>(c);
                continue;
            }
            int e = s.get();
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    uint32_t cp;
                    if (!hex4(cp)) return false;
                    if (cp >= 0xD800 && cp < 0xDC00 && s.peek() == '\\') {
                        s.get();
                        if (s.peek() == 'u') {
                            s.get();
                            uint32_t lo;
                            if (!hex4(lo)) return false;
                            if (lo >= 0xDC00 && lo < 0xE000) {
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                u8enc(cp, out);
                            } else {
                                u8enc(cp, out);
                                u8enc(lo, out);
                            }
                            break;
                        }
                        u8enc(cp, out);
                        // re-handle the escape that followed
                        int e2 = s.get();
                        switch (e2) {
                            case '"': out += '"'; break;
                            case '\\': out += '\\'; break;
                            case '/': out += '/'; break;
                            case 'b': out += '\b'; break;
                            case 'f': out += '\f'; break;
                            case 'n': out += '\n'; break;
                            case 'r': out += '\r'; break;
                            case 't': out += '\t'; break;
                            default: return false;
                        }
                        break;
                    }
                    u8enc(cp, out);
                    break;
                }
                default: return false;
            }
        }
    }

    bool number(J& out, int first) {
        std::string tok(1, static_cast<char>(first));
        bool flt = false;
        for (;;) {
            int c = s.peek();
            if ((c >= '0' && c <= '9') || c == '+' || c == '-') tok += static_cast<char>(s.get());
            else if (c == '.' || c == 'e' || c == 'E') {
                flt = true;
                tok += static_cast<char>(s.get());
            } else break;
        }
        if (tok == "-" && s.peek() == 'I') {
            if (!lit("Infinity")) return false;
            out = J::real(-INFINITY);
            return true;
        }
        if (!flt) {
            errno = 0;
            char* end = nullptr;
            long long v = strtoll(tok.c_str(), &end, 10);
            if (end && *end == '\0' && errno != ERANGE) {
                out = J::integer(v);
                return true;
            }
            if (end && *end != '\0') return false;
        }
        char* end = nullptr;
        double d = strtod_l(tok.c_str(), &end, c_locale());
        if (!end || *end != '\0') return false;
        out = J::real(d);
        return true;
    }

    bool value(J& out, int depth = 0) {
        if (depth > 10000) return false;
        ws();
        int c = s.get();
        if (c < 0) return false;
        if (c == '{') {
            out = J::obj();
            ws();
            if (s.peek() == '}') { s.get(); return true; }
            for (;;) {
                ws();
                if (s.get() != '"') return false;
                std::string k;
                if (!string(k)) return false;
                ws();
                if (s.get() != ':') return false;
                J v;
                if (!value(v, depth + 1)) return false;
                out.set(k, std::move(v));
                ws();
                int d = s.get();
                if (d == ',') continue;
                if (d == '}') return true;
                return false;
            }
        }
        if (c == '[') {
            out = J::arr();
            ws();
            if (s.peek() == ']') { s.get(); return true; }
            for (;;) {
                J v;
                if (!value(v, depth + 1)) return false;
                out.a.push_back(std::move(v));
                ws();
                int d = s.get();
                if (d == ',') continue;
                if (d == ']') return true;
                return false;
            }
        }
        if (c == '"') {
            out = J::str("");
            return string(out.s);
        }
        if (c == 't') { out = J::boolean(true); return lit("rue"); }
        if (c == 'f') { out = J::boolean(false); return lit("alse"); }
        if (c == 'n') { out = J(); return lit("ull"); }
        if (c == 'N') { out = J::real(NAN); return lit("aN"); }
        if (c == 'I') { out = J::real(INFINITY); return lit("nfinity"); }
        if (c == '-' || (c >= '0' && c <= '9')) return number(out, c);
        return false;
    }
};

bool parse_json_text(const std::string& text, J& out) {
    StrSrc src(text);
    JsonParser<StrSrc> p(src);
    if (!p.value(out)) return false;
    p.ws();
    return src.peek() < 0;
}

void json_escape(const std::string& s, std::string& out) {
    out += '"';
    for (char ch : s) {
        unsigned char c = static_cast<unsigned char>(ch);
        if (c == '"') out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c < 0x20) {
            char b[8];
            snprintf(b, sizeof(b), "\\u%04x", c);
            out += b;
        } else out += ch;
    }
    out += '"';
}

void json_dump(const J& v, std::string& out) {
    switch (v.t) {
        case J::NUL: out += "null"; break;
        case J::BOOL: out += v.b ? "true" : "false"; break;
        case J::INT: out += std::to_string(v.i); break;
        case J::FLT:
            if (std::isnan(v.f)) out += "NaN";
            else if (std::isinf(v.f)) out += v.f > 0 ? "Infinity" : "-Infinity";
            else out += py_repr(v.f);
            break;
        case J::STR: json_escape(v.s, out); break;
        case J::ARR:
            out += '[';
            for (size_t k = 0; k < v.a.size(); k++) {
                if (k) out += ',';
                json_dump(v.a[k], out);
            }
            out += ']';
            break;
        case J::OBJ:
            out += '{';
            for (size_t k = 0; k < v.o.size(); k++) {
                if (k) out += ',';
                json_escape(v.o[k].first, out);
                out += ':';
                json_dump(v.o[k].second, out);
            }
            out += '}';
            break;
    }
}

std::string json_str(const J& v) {
    std::string out;
    json_dump(v, out);
    return out;
}

const J& SPEC() {
    static const J spec = []() {
        J j;
        if (!parse_json_text(SPEC_TEXT, j)) throw std::runtime_error("corrupt embedded spec");
        return j;
    }();
    return spec;
}

// ---------------------------------------------------------------------------
// Python semantics of the runtime helpers
// ---------------------------------------------------------------------------

bool all_digits(const std::string& s, size_t from) {
    if (from >= s.size()) return false;
    for (size_t k = from; k < s.size(); k++)
        if (s[k] < '0' || s[k] > '9') return false;
    return true;
}

bool float_pattern(const std::string& s) {
    // [+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
    size_t k = 0, n = s.size();
    if (k < n && (s[k] == '+' || s[k] == '-')) k++;
    size_t d1 = 0;
    while (k < n && s[k] >= '0' && s[k] <= '9') { k++; d1++; }
    size_t d2 = 0;
    if (k < n && s[k] == '.') {
        k++;
        while (k < n && s[k] >= '0' && s[k] <= '9') { k++; d2++; }
    }
    if (d1 == 0 && d2 == 0) return false;
    if (k < n && (s[k] == 'e' || s[k] == 'E')) {
        k++;
        if (k < n && (s[k] == '+' || s[k] == '-')) k++;
        size_t d3 = 0;
        while (k < n && s[k] >= '0' && s[k] <= '9') { k++; d3++; }
        if (d3 == 0) return false;
    }
    return k == n;
}

J to_num(const J& v) {
    if (v.t == J::INT || v.t == J::FLT) return v;
    if (v.t != J::STR) return J();
    std::string t = py_strip(v.s);
    if (t.empty()) return J();
    size_t from = (t[0] == '+' || t[0] == '-') ? 1 : 0;
    if (all_digits(t, from)) {
        errno = 0;
        long long x = strtoll(t.c_str(), nullptr, 10);
        if (errno != ERANGE) return J::integer(x);
        return J::real(parse_double(t));
    }
    if (float_pattern(t)) return J::real(parse_double(t));
    return J();
}

bool to_str(const J& v, std::string& out) {
    switch (v.t) {
        case J::NUL: return false;
        case J::STR: out = v.s; return true;
        case J::BOOL: out = v.b ? "true" : "false"; return true;
        case J::INT: out = std::to_string(v.i); return true;
        case J::FLT: out = py_repr(v.f); return true;
        default: out = py_str_value(v); return true;
    }
}

bool py_eq(const J& a, const J& b) {
    if (a.t == J::BOOL || b.t == J::BOOL) return a.t == J::BOOL && b.t == J::BOOL && a.b == b.b;
    if (a.t == J::NUL || b.t == J::NUL) return a.t == J::NUL && b.t == J::NUL;
    bool an = a.isnum(), bn = b.isnum();
    if (an || bn) {
        if (!(an && bn)) return false;
        if (a.t == J::INT && b.t == J::INT) return a.i == b.i;
        return a.num() == b.num();
    }
    if (a.t != b.t) return false;
    if (a.t == J::STR) return a.s == b.s;
    if (a.t == J::ARR) {
        if (a.a.size() != b.a.size()) return false;
        for (size_t k = 0; k < a.a.size(); k++)
            if (!py_eq(a.a[k], b.a[k])) return false;
        return true;
    }
    if (a.o.size() != b.o.size()) return false;
    for (const auto& p : a.o) {
        const J* q = b.get(p.first);
        if (!q || !py_eq(p.second, *q)) return false;
    }
    return true;
}

bool n_lt(const J& a, const J& b) {
    if (a.t == J::INT && b.t == J::INT) return a.i < b.i;
    return a.num() < b.num();
}

bool n_eq(const J& a, const J& b) {
    if (a.t == J::INT && b.t == J::INT) return a.i == b.i;
    return a.num() == b.num();
}

J n_add(const J& x, const J& y) {
    if (x.t == J::INT && y.t == J::INT) {
        long long r;
        if (!__builtin_add_overflow(x.i, y.i, &r)) return J::integer(r);
        return J::real(static_cast<double>(x.i) + static_cast<double>(y.i));
    }
    return J::real(x.num() + y.num());
}

J n_sub(const J& x, const J& y) {
    if (x.t == J::INT && y.t == J::INT) {
        long long r;
        if (!__builtin_sub_overflow(x.i, y.i, &r)) return J::integer(r);
        return J::real(static_cast<double>(x.i) - static_cast<double>(y.i));
    }
    return J::real(x.num() - y.num());
}

J n_mul(const J& x, const J& y) {
    if (x.t == J::INT && y.t == J::INT) {
        long long r;
        if (!__builtin_mul_overflow(x.i, y.i, &r)) return J::integer(r);
        return J::real(static_cast<double>(x.i) * static_cast<double>(y.i));
    }
    return J::real(x.num() * y.num());
}

J n_div(const J& x, const J& y) {
    if (y.num() == 0) throw PyError();
    return J::real(x.num() / y.num());
}

std::string sop(const std::string& s, const std::string& op) {
    if (op == "strip") return py_strip(s);
    if (op == "lower") return py_lower(s);
    if (op == "upper") return py_upper(s);
    return s;
}

bool sops(const J& v, const J& ops, std::string& out) {
    if (!to_str(v, out)) return false;
    for (const J& op : ops.a) out = sop(out, op.s);
    return true;
}

J py_round(const J& y, const J& d) {
    if (d.null()) return y;
    long long nd = d.t == J::INT ? d.i : static_cast<long long>(d.num());
    if (y.t == J::INT) {
        if (nd >= 0) return y;
        if (-nd > 18) return J::integer(0);
        long long p = 1;
        for (long long k = 0; k < -nd; k++) p *= 10;
        long long q = y.i / p, r = y.i % p;
        if (r < 0) { r += p; q -= 1; }
        if (2 * r > p || (2 * r == p && (q % 2 != 0))) q += 1;
        long long out;
        if (__builtin_mul_overflow(q, p, &out)) return J::real(static_cast<double>(q) * p);
        return J::integer(out);
    }
    double v = y.num();
    if (!std::isfinite(v)) return J::real(v);
    if (nd >= 0) {
        if (nd > 330) return J::real(v);
        return J::real(parse_double(py_fixed(v, nd)));
    }
    double p = std::pow(10.0, static_cast<double>(-nd));
    if (!std::isfinite(p)) return J::real(0.0 * v);
    return J::real(std::nearbyint(v / p) * p);
}

J py_fmt(const J& y, const J& fmt) {
    const std::string& k = fmt.a.empty() ? std::string() : fmt.a[0].s;
    if (k == "raw") return y;
    if (k == "int") return y.t == J::INT ? y : round_to_int(y.f);
    if (k == "float") return J::real(y.num());
    if (k == "intify") {
        double v = y.num();
        if (is_integer(v) && std::fabs(v) < 1e16) return J::integer(static_cast<long long>(v));
        return J::real(v);
    }
    if (k == "s_g") {
        if (y.t == J::FLT) {
            if (is_integer(y.f) && std::fabs(y.f) < 1e16) return J::str(std::to_string(static_cast<long long>(y.f)));
            return J::str(py_repr(y.f));
        }
        return J::str(std::to_string(y.i));
    }
    if (k == "s_int") return J::str(y.t == J::INT ? std::to_string(y.i) : int_str(round_to_int(y.f)));
    if (k == "s_fixed") return J::str(py_fixed(y.num(), fmt.a.size() > 1 ? fmt.a[1].i : 0));
    if (k == "s_repr") return J::str(py_repr(y.num()));
    return y;
}

J apply_t(const J& t, const J& row) {
    const std::string& k = field(t, "type").s;
    if (k == "copy") {
        const J& v = field(row, field(t, "src").s);
        const J& ops = field(t, "ops");
        if (ops.a.empty()) return v;
        std::string s;
        if (!sops(v, ops, s)) return J();
        return J::str(s);
    }
    if (k == "const") return field(t, "value");
    if (k == "linear") {
        const J& v = field(row, field(t, "src").s);
        J x = to_num(v);
        if (x.null()) return v;
        try {
            J y = n_add(n_mul(field(t, "a"), x), field(t, "b"));
            y = py_round(y, field(t, "round"));
            return py_fmt(y, field(t, "fmt"));
        } catch (const PyError&) {
            return v;
        }
    }
    if (k == "affix") {
        std::string s;
        if (!sops(field(row, field(t, "src").s), field(t, "ops"), s)) return J();
        return J::str(field(t, "prefix").s + s + field(t, "suffix").s);
    }
    if (k == "split") {
        std::string s;
        if (!sops(field(row, field(t, "src").s), field(t, "ops"), s)) return J();
        const J& sep = field(t, "sep");
        const std::string& mode = field(t, "mode").s;
        J p;
        if (mode == "idx") {
            std::vector<std::string> parts = sep.null() ? py_split_ws(s) : py_split(s, sep.s);
            long long i = field(t, "index").i;
            long long n = static_cast<long long>(parts.size());
            if (-n <= i && i < n) p = J::str(parts[static_cast<size_t>(i < 0 ? i + n : i)]);
            else p = field(t, "default");
        } else if (mode == "rest") {
            size_t pos = s.find(sep.s);
            p = pos != std::string::npos ? J::str(s.substr(pos + sep.s.size())) : field(t, "default");
        } else {
            size_t pos = s.rfind(sep.s);
            p = J::str(pos != std::string::npos ? s.substr(0, pos) : s);
        }
        if (!p.null() && truthy(field(t, "post_strip")) && p.t == J::STR) p.s = py_strip(p.s);
        return p;
    }
    if (k == "concat") {
        std::string a, b;
        if (!sops(field(row, field(t, "a").s), field(t, "ops"), a)) a.clear();
        if (!sops(field(row, field(t, "b").s), field(t, "ops"), b)) b.clear();
        return J::str(field(t, "prefix").s + a + field(t, "sep").s + b + field(t, "suffix").s);
    }
    if (k == "arith") {
        J x = to_num(field(row, field(t, "a").s));
        J y = to_num(field(row, field(t, "b").s));
        if (x.null() || y.null()) return field(t, "default");
        const std::string& op = field(t, "op").s;
        try {
            J r;
            if (op == "+") r = n_add(x, y);
            else if (op == "-") r = n_sub(x, y);
            else if (op == "*") r = n_mul(x, y);
            else r = n_div(x, y);
            r = py_round(r, field(t, "round"));
            return py_fmt(r, field(t, "fmt"));
        } catch (const PyError&) {
            return field(t, "default");
        }
    }
    if (k == "missing") return field(t, "value");
    return J();
}

bool filt(const J& f, const J& row, const J* prev, const J* nxt) {
    if (f.null()) return true;
    const std::string& k = field(f, "k").s;
    if (k == "and") {
        for (const J& a : field(f, "args").a)
            if (!filt(a, row, prev, nxt)) return false;
        return true;
    }
    if (k == "or") {
        for (const J& a : field(f, "args").a)
            if (filt(a, row, prev, nxt)) return true;
        return false;
    }
    if (k == "const") return truthy(field(f, "v"));
    if (k == "edge") {
        if (!prev) return false;
        bool c = filt(field(f, "arg"), row, nullptr, nullptr);
        bool p = filt(field(f, "arg"), *prev, nullptr, nullptr);
        const std::string& m = field(f, "mode").s;
        if (m == "rise") return c && !p;
        if (m == "fall") return p && !c;
        return c != p;
    }
    if (k == "same") {
        const J* nb = field(f, "rel").num() < 0 ? prev : nxt;
        const std::string& col = field(f, "col").s;
        bool r = nb && py_eq(field(row, col), field(*nb, col));
        return field(f, "op").s == "==" ? r : !r;
    }
    if (k == "colcmp") {
        bool r = py_eq(field(row, field(f, "a").s), field(row, field(f, "b").s));
        return field(f, "op").s == "==" ? r : !r;
    }
    double rel = field(f, "rel").num();
    const J* vp;
    if (rel != 0) {
        const J* src = rel < 0 ? prev : nxt;
        vp = src ? &field(*src, field(f, "col").s) : &nullj();
    } else {
        vp = &field(row, field(f, "col").s);
    }
    const J& v = *vp;
    const std::string& op = field(f, "op").s;
    const J& c = field(f, "v");
    if (k == "eq") {
        bool r = py_eq(v, c);
        return op == "==" ? r : !r;
    }
    if (k == "numeq") {
        J x = to_num(v);
        bool r = !x.null() && c.isnum() && n_eq(x, c);
        return op == "==" ? r : !r;
    }
    if (k == "num") {
        J x = to_num(v);
        if (x.null() || !c.isnum()) return false;
        if (op == "<") return n_lt(x, c);
        if (op == "<=") return n_lt(x, c) || n_eq(x, c);
        if (op == ">") return n_lt(c, x);
        if (op == ">=") return n_lt(c, x) || n_eq(x, c);
        if (op == "==") return n_eq(x, c);
        if (op == "!=") return !n_eq(x, c);
        return false;
    }
    if (k == "str") {
        if (v.t != J::STR || c.t != J::STR) return false;
        int r = v.s.compare(c.s);
        if (op == "<") return r < 0;
        if (op == "<=") return r <= 0;
        if (op == ">") return r > 0;
        if (op == ">=") return r >= 0;
        if (op == "==") return r == 0;
        if (op == "!=") return r != 0;
        return false;
    }
    return false;
}

// --- accumulators (plain or CPython 3.12 compensated sum) ------------------

J acc_new() {
    J a = J::arr();
    a.a.push_back(J::integer(0));
    a.a.push_back(J::integer(0));
    a.a.push_back(J::real(0.0));
    a.a.push_back(J::real(0.0));
    return a;
}

void acc_add(J& acc, const J& x, bool neu) {
    std::vector<J>& A = acc.a;
    if (!neu) {
        A[1] = n_add(A[1], x);
        return;
    }
    if (A[0].num() == 0) {
        if (x.t == J::INT) {
            A[1] = n_add(A[1], x);
            return;
        }
        A[0] = J::integer(1);
        A[2] = n_add(A[1], x);
        A[3] = J::real(0.0);
        return;
    }
    if (x.t == J::INT) {
        A[2] = J::real(A[2].num() + static_cast<double>(x.i));
        return;
    }
    double xf = x.num();
    double f = A[2].num();
    double s = f + xf;
    double c = A[3].num();
    if (std::fabs(f) >= std::fabs(xf)) c += (f - s) + xf;
    else c += (xf - s) + f;
    A[3] = J::real(c);
    A[2] = J::real(s);
}

J acc_val(const J& acc, bool neu) {
    const std::vector<J>& A = acc.a;
    if (!neu || A[0].num() == 0) return A[1];
    double f = A[2].num();
    double c = A[3].num();
    if (c != 0 && c - c == 0) f = f + c;
    return J::real(f);
}

bool better(const std::string& fn, const J& v, const J& m) {
    return m.null() || (fn == "min" && n_lt(v, m)) || (fn == "max" && n_lt(m, v));
}

J st_init(const J& t) {
    const std::string& k = field(t, "kind").s;
    J s = J::obj();
    if (k == "agg") {
        if (truthy(field(t, "w"))) {
            s.set("win", J::arr());
            s.set("n", J::integer(0));
        } else {
            s.set("a", acc_new());
            s.set("c", J::integer(0));
            s.set("m", J());
            s.set("n", J::integer(0));
        }
    } else if (k == "counter") {
        s.set("c", J::integer(0));
    } else if (k == "lag") {
        s.set("h", J::arr());
    } else if (k == "diff") {
        s.set("p", J());
        s.set("n", J::integer(0));
    } else if (k == "fsm") {
        s.set("v", field(t, "init"));
    }
    return s;
}

J st_step(const J& t, J& st, const J& raw, const J* prev) {
    const std::string& k = field(t, "kind").s;
    if (k == "agg") {
        const std::string& fn = field(t, "fn").s;
        bool neu = field(t, "acc").t == J::STR && field(t, "acc").s == "neu";
        J x;
        if (fn == "count") x = J::integer(filt(field(t, "cond"), raw, prev, nullptr) ? 1 : 0);
        else x = to_num(field(raw, field(t, "src").s));
        J& n = st.at("n");
        n = J::integer(n.i + 1);
        const J& w = field(t, "w");
        J s, c, m;
        if (truthy(w)) {
            J& win = st.at("win");
            win.a.push_back(x);
            if (static_cast<double>(win.a.size()) > w.num()) win.a.erase(win.a.begin());
            J acc = acc_new();
            long long cnt = 0;
            for (const J& v : win.a) {
                if (v.null()) continue;
                acc_add(acc, v, neu);
                cnt++;
                if (better(fn, v, m)) m = v;
            }
            s = acc_val(acc, neu);
            c = J::integer(cnt);
        } else {
            if (!x.null()) {
                acc_add(st.at("a"), x, neu);
                J& cc = st.at("c");
                cc = J::integer(cc.i + 1);
                if (better(fn, x, field(st, "m"))) st.set("m", x);
            }
            s = acc_val(field(st, "a"), neu);
            c = field(st, "c");
            m = field(st, "m");
        }
        if (truthy(field(t, "full")) && n_lt(field(st, "n"), w)) return J();
        if (fn == "sum" || fn == "count") return s;
        if (fn == "mean") return c.i ? n_div(s, c) : J();
        return m;
    }
    if (k == "counter") {
        J& c = st.at("c");
        if (filt(field(t, "cond"), raw, prev, nullptr)) c = J::integer(c.i + 1);
        return c;
    }
    if (k == "lag") {
        J& h = st.at("h");
        long long kk = field(t, "k").i;
        bool has = static_cast<long long>(h.a.size()) >= kk;
        J r = has && !h.a.empty() ? h.a[0] : J();
        h.a.push_back(field(raw, field(t, "src").s));
        if (static_cast<long long>(h.a.size()) > kk) h.a.erase(h.a.begin());
        if (!has) return J();
        J out = J::arr();
        out.a.push_back(r);
        return out;
    }
    if (k == "diff") {
        J x = to_num(field(raw, field(t, "src").s));
        J p = field(st, "p");
        J& n = st.at("n");
        n = J::integer(n.i + 1);
        st.set("p", x);
        if (x.null() || p.null()) return J();
        return n_sub(x, p);
    }
    if (k == "fsm") {
        for (const J& rule : field(t, "rules").a) {
            if (rule.a.size() < 2) continue;
            if (filt(rule.a[0], raw, prev, nullptr)) {
                st.set("v", rule.a[1]);
                break;
            }
        }
        return field(st, "v");
    }
    return J();
}

J st_out(const J& t, const J& s) {
    if (s.null()) return field(t, "none");
    const std::string& k = field(t, "kind").s;
    if (k == "lag") return s.a.empty() ? J() : s.a[0];
    if (k == "fsm") return s;
    const J& labels = field(t, "labels");
    if (!labels.null()) {
        long long n = static_cast<long long>(labels.a.size());
        if (n == 0) return J();
        long long v = s.t == J::INT ? s.i : static_cast<long long>(s.num());
        long long idx = ((v % n) + n) % n;
        return labels.a[static_cast<size_t>(idx)];
    }
    const J& post = field(t, "post");
    if (post.null()) return s;
    J row = J::obj();
    row.set("_s", s);
    return apply_t(post, row);
}

J new_state(const J& spec) {
    J s = J::obj();
    s.set("prev", J());
    s.set("pk", J());
    s.set("pend", J());
    s.set("hp", J::boolean(false));
    J sts = J::arr();
    for (const J& col : field(spec, "columns").a) {
        const J& t = col.a[1];
        sts.a.push_back(field(t, "type").s == "st" ? st_init(t) : J());
    }
    s.set("st", std::move(sts));
    return s;
}

// Row-at-a-time engine: neighbor-aware filtering (one row of lookahead) plus
// stateful columns.  All state lives in `s`, a JSON value (checkpointable).
struct Engine {
    const J& spec;
    const J& cols;
    const J& filter;
    bool look;
    J s;

    explicit Engine(const J& sp)
        : spec(sp), cols(field(sp, "columns")), filter(field(sp, "filter")), look(truthy(field(sp, "lookahead"))),
          s(new_state(sp)) {}

    void push(const J& raw, std::deque<J>& out) {
        if (!look) {
            proc(raw, nullptr, out);
            return;
        }
        if (truthy(field(s, "hp"))) {
            J cur = field(s, "pend");
            proc(cur, &raw, out);
        }
        s.set("pend", raw);
        s.set("hp", J::boolean(true));
    }

    void finish(std::deque<J>& out) {
        if (look && truthy(field(s, "hp"))) {
            J r = field(s, "pend");
            s.set("pend", J());
            s.set("hp", J::boolean(false));
            proc(r, nullptr, out);
        }
    }

    void proc(const J& raw, const J* nxt, std::deque<J>& out) {
        J prev_v = field(s, "prev");
        const J* prev = prev_v.null() ? nullptr : &prev_v;
        bool keep = filt(filter, raw, prev, nxt);
        J& sts = s.at("st");
        size_t nc = cols.a.size();
        std::vector<J> vals(nc);
        for (size_t i = 0; i < nc; i++) {
            const J& t = cols.a[i].a[1];
            if (field(t, "type").s == "st" && field(t, "scope").s == "all")
                vals[i] = st_out(t, st_step(t, sts.a[i], raw, prev));
        }
        s.set("prev", raw);
        if (!keep) return;
        J pk_v = field(s, "pk");
        const J* pk = pk_v.null() ? nullptr : &pk_v;
        J row = J::obj();
        for (size_t i = 0; i < nc; i++) {
            const std::string& name = cols.a[i].a[0].s;
            const J& t = cols.a[i].a[1];
            if (field(t, "type").s == "st") {
                if (field(t, "scope").s != "all") vals[i] = st_out(t, st_step(t, sts.a[i], raw, pk));
                row.set(name, std::move(vals[i]));
            } else {
                row.set(name, apply_t(t, raw));
            }
        }
        s.set("pk", raw);
        out.push_back(std::move(row));
    }
};

// ---------------------------------------------------------------------------
// Readers (csv / tsv / jsonl / json), streaming
// ---------------------------------------------------------------------------

struct RowSource {
    virtual ~RowSource() {}
    virtual bool next(J& row) = 0;
};

// Mirrors Python's csv.reader (default dialect, strict=False) fed by a file
// opened with newline=''.
struct DelimSource : RowSource {
    FileSrc src;
    char delim;
    bool have_header = false;
    std::vector<std::string> header;
    explicit DelimSource(FILE* f, char d) : src(f), delim(d) {}

    enum St { START_RECORD, START_FIELD, IN_FIELD, IN_QUOTED_FIELD, QUOTE_IN_QUOTED_FIELD, EAT_CRNL };

    // Read one record; returns false at end of input.
    bool record(std::vector<std::string>& rec) {
        rec.clear();
        if (src.peek() < 0) return false;
        St state = START_RECORD;
        std::string fld;
        bool fld_started = false;
        auto save = [&]() {
            rec.push_back(fld);
            fld.clear();
            fld_started = false;
        };
        const int EOL = -2;
        for (;;) {
            // read one physical line, feeding chars, then EOL
            int c;
            bool line_had = false;
            for (;;) {
                c = src.get();
                if (c < 0) break;
                line_had = true;
                bool end_line = false;
                if (c == '\r') {
                    if (src.peek() == '\n') {
                        // feed '\r' then '\n'
                    } else {
                        end_line = true;
                    }
                } else if (c == '\n') {
                    end_line = true;
                }
                // process c
                switch (state) {
                    case START_RECORD:
                        if (c == '\n' || c == '\r') { state = EAT_CRNL; break; }
                        state = START_FIELD;
                        // fallthrough
                    case START_FIELD:
                        if (c == '\n' || c == '\r') { save(); state = EAT_CRNL; }
                        else if (c == '"') { state = IN_QUOTED_FIELD; fld_started = true; }
                        else if (c == delim) { save(); }
                        else { fld += static_cast<char>(c); fld_started = true; state = IN_FIELD; }
                        break;
                    case IN_FIELD:
                        if (c == '\n' || c == '\r') { save(); state = EAT_CRNL; }
                        else if (c == delim) { save(); state = START_FIELD; }
                        else fld += static_cast<char>(c);
                        break;
                    case IN_QUOTED_FIELD:
                        if (c == '"') state = QUOTE_IN_QUOTED_FIELD;
                        else fld += static_cast<char>(c);
                        break;
                    case QUOTE_IN_QUOTED_FIELD:
                        if (c == '"') { fld += '"'; state = IN_QUOTED_FIELD; }
                        else if (c == delim) { save(); state = START_FIELD; }
                        else if (c == '\n' || c == '\r') { save(); state = EAT_CRNL; }
                        else { fld += static_cast<char>(c); state = IN_FIELD; }
                        break;
                    case EAT_CRNL:
                        break;
                }
                if (end_line) break;
            }
            if (line_had) {
                // EOL
                c = EOL;
                switch (state) {
                    case START_RECORD: break;
                    case START_FIELD: save(); state = START_RECORD; break;
                    case IN_FIELD: save(); state = START_RECORD; break;
                    case IN_QUOTED_FIELD: break;
                    case QUOTE_IN_QUOTED_FIELD: save(); state = START_RECORD; break;
                    case EAT_CRNL: state = START_RECORD; break;
                }
                if (state == START_RECORD) return true;
                continue;
            }
            // end of input inside a record
            if (!fld.empty() || fld_started || state == IN_QUOTED_FIELD) save();
            return true;
        }
    }

    bool next(J& row) override {
        std::vector<std::string> rec;
        if (!have_header) {
            if (!record(header)) return false;
            have_header = true;
        }
        for (;;) {
            if (!record(rec)) return false;
            if (rec.empty()) continue;
            row = J::obj();
            for (size_t i = 0; i < header.size(); i++)
                row.set(header[i], i < rec.size() ? J::str(rec[i]) : J());
            return true;
        }
    }
};

struct JsonlSource : RowSource {
    FileSrc src;
    explicit JsonlSource(FILE* f) : src(f) {}
    bool line(std::string& out) {
        out.clear();
        int c = src.get();
        if (c < 0) return false;
        for (;;) {
            if (c < 0) break;
            if (c == '\n') break;
            if (c == '\r') {
                if (src.peek() == '\n') src.get();
                break;
            }
            out += static_cast<char>(c);
            c = src.get();
        }
        return true;
    }
    bool next(J& row) override {
        std::string ln;
        while (line(ln)) {
            std::string s = py_strip(ln);
            if (s.empty()) continue;
            J obj;
            if (!parse_json_text(s, obj)) continue;
            if (obj.t == J::OBJ) {
                row = std::move(obj);
                return true;
            }
        }
        return false;
    }
};

struct JsonArraySource : RowSource {
    FileSrc src;
    bool started = false;
    bool done = false;
    explicit JsonArraySource(FILE* f) : src(f) {}
    void skip() {
        for (;;) {
            int c = src.peek();
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == ',') src.get();
            else break;
        }
    }
    bool next(J& row) override {
        while (!done) {
            skip();
            int c = src.peek();
            if (c < 0) { done = true; break; }
            if (!started) {
                started = true;
                if (c == '[') { src.get(); continue; }
            }
            if (c == ']') { done = true; break; }
            J v;
            JsonParser<FileSrc> p(src);
            if (!p.value(v)) { done = true; break; }
            if (v.t == J::OBJ) {
                row = std::move(v);
                return true;
            }
        }
        return false;
    }
};

std::unique_ptr<RowSource> open_source(const std::string& path) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) throw std::runtime_error("cannot open " + path + ": " + std::strerror(errno));
    const std::string& fmt = field(SPEC(), "format").s;
    if (fmt == "csv") return std::unique_ptr<RowSource>(new DelimSource(f, ','));
    if (fmt == "tsv") return std::unique_ptr<RowSource>(new DelimSource(f, '\t'));
    if (fmt == "jsonl") return std::unique_ptr<RowSource>(new JsonlSource(f));
    return std::unique_ptr<RowSource>(new JsonArraySource(f));
}

// ---------------------------------------------------------------------------
// Filesystem helpers for the cache
// ---------------------------------------------------------------------------

std::string hash_hex(const std::string& s) {
    uint64_t h1 = 1469598103934665603ULL, h2 = 0x84222325cbf29ce4ULL ^ 0x9e3779b97f4a7c15ULL;
    for (unsigned char c : s) {
        h1 ^= c;
        h1 *= 1099511628211ULL;
        h2 ^= c;
        h2 *= 0x100000001b3ULL;
        h2 ^= h2 >> 29;
    }
    char buf[40];
    snprintf(buf, sizeof(buf), "%016llx%016llx", static_cast<unsigned long long>(h1),
             static_cast<unsigned long long>(h2));
    return buf;
}

std::string join_path(const std::string& a, const std::string& b) {
    if (a.empty()) return b;
    if (a.back() == '/') return a + b;
    return a + "/" + b;
}

std::string abs_path(const std::string& p) {
    std::string full = p;
    if (p.empty() || p[0] != '/') {
        std::vector<char> buf(4096);
        while (!getcwd(buf.data(), buf.size())) {
            if (errno != ERANGE) break;
            buf.resize(buf.size() * 2);
        }
        full = join_path(std::string(buf.data()), p);
    }
    std::vector<std::string> parts;
    size_t i = 0;
    while (i <= full.size()) {
        size_t j = full.find('/', i);
        if (j == std::string::npos) j = full.size();
        std::string seg = full.substr(i, j - i);
        if (seg.empty() || seg == ".") {
        } else if (seg == "..") {
            if (!parts.empty()) parts.pop_back();
        } else parts.push_back(seg);
        i = j + 1;
    }
    std::string out;
    for (const auto& s : parts) out += "/" + s;
    return out.empty() ? "/" : out;
}

void mkdirs(const std::string& p) {
    if (p.empty()) return;
    std::string cur;
    size_t i = 0;
    if (p[0] == '/') { cur = "/"; i = 1; }
    while (i <= p.size()) {
        size_t j = p.find('/', i);
        if (j == std::string::npos) j = p.size();
        std::string seg = p.substr(i, j - i);
        if (!seg.empty()) {
            cur = cur.empty() || cur.back() == '/' ? cur + seg : cur + "/" + seg;
            mkdir(cur.c_str(), 0777);
        }
        i = j + 1;
    }
}

bool read_file(const std::string& p, std::string& out) {
    FILE* f = fopen(p.c_str(), "rb");
    if (!f) return false;
    out.clear();
    char buf[1 << 14];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    fclose(f);
    return true;
}

bool write_file_atomic(const std::string& p, const std::string& content) {
    std::string tmp = p + "." + std::to_string(static_cast<long long>(getpid())) + ".tmp";
    FILE* f = fopen(tmp.c_str(), "wb");
    if (!f) return false;
    bool ok = fwrite(content.data(), 1, content.size(), f) == content.size();
    ok = (fclose(f) == 0) && ok;
    if (!ok || rename(tmp.c_str(), p.c_str()) != 0) {
        remove(tmp.c_str());
        return false;
    }
    return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

struct DynamicPreprocessor::Impl {
    std::size_t buffer = 1024;
    bool use_cache = false;
    std::string cache_dir;

    bool opened = false;
    std::unique_ptr<RowSource> src;
    std::unique_ptr<Engine> eng;
    std::deque<J> queue;
    bool finished = false;
    bool closed = false;
    long long n_in = 0, emitted = 0, idx = 0, start = 0;

    std::string prog_p, state_p;
    FILE* pf = nullptr;

    ~Impl() { close_files(); }

    void close_files() {
        if (pf) {
            fclose(pf);
            pf = nullptr;
        }
    }

    void write_progress(long long v) {
        if (!pf) return;
        char buf[32];
        int n = snprintf(buf, sizeof(buf), "%020lld\n", v);
        fseek(pf, 0, SEEK_SET);
        fwrite(buf, 1, static_cast<size_t>(n), pf);
        fflush(pf);
    }

    void save_state() {
        J ck = J::obj();
        ck.set("spec", J::str(SPEC_ID));
        ck.set("in", J::integer(n_in));
        ck.set("out", J::integer(emitted));
        ck.set("state", eng->s);
        write_file_atomic(state_p, json_str(ck));
    }

    void open(const std::string& path) {
        close_files();
        src.reset();
        queue.clear();
        finished = false;
        closed = false;
        n_in = emitted = idx = start = 0;
        opened = false;
        eng.reset(new Engine(SPEC()));
        src = open_source(path);
        if (!use_cache) {
            opened = true;
            return;
        }
        std::string apath = abs_path(path);
        std::string d = join_path(cache_dir, std::string("cpp_") + hash_hex(std::string(SPEC_ID) + '\0' + apath));
        mkdirs(d);
        std::string meta_p = join_path(d, "meta.json");
        prog_p = join_path(d, "progress");
        state_p = join_path(d, "state.json");
        struct stat sb;
        long long size = 0, mtime = 0;
        if (stat(path.c_str(), &sb) == 0) {
            size = static_cast<long long>(sb.st_size);
            mtime = static_cast<long long>(sb.st_mtim.tv_sec) * 1000000000LL + static_cast<long long>(sb.st_mtim.tv_nsec);
        }
        J meta = J::obj();
        meta.set("path", J::str(apath));
        meta.set("size", J::integer(size));
        meta.set("mtime_ns", J::integer(mtime));
        meta.set("spec", J::str(SPEC_ID));
        std::string meta_s = json_str(meta);
        std::string old;
        if (!read_file(meta_p, old) || old != meta_s) {
            remove(prog_p.c_str());
            remove(state_p.c_str());
            write_file_atomic(meta_p, meta_s);
        }
        std::string prog;
        if (read_file(prog_p, prog)) {
            std::string t = py_strip(prog);
            if (all_digits(t, 0)) start = std::strtoll(t.c_str(), nullptr, 10);
        }
        if (start < 0) start = 0;
        if (start > 0) {
            std::string text;
            J ck;
            if (read_file(state_p, text) && parse_json_text(text, ck) && ck.t == J::OBJ) {
                const J& spec = field(ck, "spec");
                const J& in = field(ck, "in");
                const J& out = field(ck, "out");
                const J& st = field(ck, "state");
                if (spec.t == J::STR && spec.s == SPEC_ID && in.t == J::INT && out.t == J::INT && in.i >= 0 &&
                    out.i >= 0 && out.i <= start && st.t == J::OBJ) {
                    eng->s = st;
                    n_in = in.i;
                    emitted = out.i;
                    idx = out.i;
                    J tmp;
                    long long skipped = 0;
                    while (skipped < n_in && src->next(tmp)) skipped++;
                }
            }
        }
        pf = fopen(prog_p.c_str(), "r+b");
        if (!pf) pf = fopen(prog_p.c_str(), "w+b");
        opened = true;
    }

    void fill() {
        if (use_cache) save_state();
        std::size_t got = 0;
        J raw;
        while (got < buffer && src->next(raw)) {
            got++;
            std::size_t before = queue.size();
            eng->push(raw, queue);
            emitted += static_cast<long long>(queue.size() - before);
        }
        n_in += static_cast<long long>(got);
        if (got == 0) {
            eng->finish(queue);
            finished = true;
            src.reset();
        }
    }

    bool next(J& out) {
        if (!opened) return false;
        for (;;) {
            if (!queue.empty()) {
                J row = std::move(queue.front());
                queue.pop_front();
                if (idx < start) {
                    idx++;
                    continue;
                }
                idx++;
                write_progress(idx);
                out = std::move(row);
                return true;
            }
            if (finished) {
                if (!closed) {
                    closed = true;
                    if (use_cache) {
                        write_progress(0);
                        remove(state_p.c_str());
                    }
                    close_files();
                }
                return false;
            }
            fill();
        }
    }
};

namespace {
Value to_value(const J& v) {
    Value out;
    out.type = ValueType::Null;
    out.bool_value = false;
    out.int_value = 0;
    out.double_value = 0.0;
    switch (v.t) {
        case J::NUL: break;
        case J::BOOL: out.type = ValueType::Bool; out.bool_value = v.b; break;
        case J::INT: out.type = ValueType::Int; out.int_value = v.i; break;
        case J::FLT: out.type = ValueType::Double; out.double_value = v.f; break;
        case J::STR: out.type = ValueType::String; out.string_value = v.s; break;
        default: out.type = ValueType::String; out.string_value = json_str(v); break;
    }
    return out;
}
}  // namespace

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer) : impl_(new Impl()) {
    impl_->buffer = buffer < 1 ? 1 : buffer;
}

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir) : impl_(new Impl()) {
    impl_->buffer = buffer < 1 ? 1 : buffer;
    impl_->use_cache = true;
    impl_->cache_dir = cache_dir;
}

DynamicPreprocessor::~DynamicPreprocessor() = default;
DynamicPreprocessor::DynamicPreprocessor(DynamicPreprocessor&&) noexcept = default;
DynamicPreprocessor& DynamicPreprocessor::operator=(DynamicPreprocessor&&) noexcept = default;

void DynamicPreprocessor::open(const std::string& path) { impl_->open(path); }

bool DynamicPreprocessor::next(Row& out) {
    J row;
    if (!impl_ || !impl_->next(row)) return false;
    out.clear();
    for (const auto& p : row.o) out[p.first] = to_value(p.second);
    return true;
}

}  // namespace @@NS@@
'''

# ---------------------------------------------------------------------------
# Runtime (Rust).  Same design as the C++ runtime.
# ---------------------------------------------------------------------------

RT_CARGO = '''[package]
name = "@@NAME@@"
version = "1.0.0"
edition = "2021"
publish = false

[lib]
name = "@@NAME@@"
path = "src/lib.rs"

[dependencies]

[workspace]
'''

RT_RUST = r'''//! Generated by dynamic_buffer.py for module @@MODULE@@. Do not edit.
//!
//! A streaming interpreter for the inferred transformation (embedded below as
//! JSON).  Its semantics mirror the generated Python module exactly: Python
//! number typing (int vs float), repr() of floats, round-half-even, Unicode
//! whitespace for strip/split, csv-module parsing, etc.
#![allow(dead_code)]
#![allow(clippy::all)]

pub mod api {
    use std::collections::VecDeque;
    use std::fs::{self, File, OpenOptions};
    use std::io::{Read, Seek, SeekFrom, Write};
    use std::path::{Path, PathBuf};

    const SPEC_ID: &str = "@@SPECID@@";
    const SPEC_TEXT: &str = r@@HASHES@@"@@SPEC@@"@@HASHES@@;

    #[derive(Clone, Debug, PartialEq)]
    pub enum Value {
        Null,
        Bool(bool),
        Int(i64),
        Float(f64),
        Str(String),
    }

    pub type Row = std::collections::BTreeMap<String, Value>;

    // -----------------------------------------------------------------------
    // Dynamic (JSON-like) value with Python's int / float distinction
    // -----------------------------------------------------------------------

    #[derive(Clone, Debug)]
    enum J {
        Null,
        Bool(bool),
        Int(i64),
        Flt(f64),
        Str(String),
        Arr(Vec<J>),
        Obj(Vec<(String, J)>),
    }

    static NULLJ: J = J::Null;

    struct PyError;

    impl J {
        fn is_null(&self) -> bool {
            matches!(self, J::Null)
        }
        fn is_num(&self) -> bool {
            matches!(self, J::Int(_) | J::Flt(_))
        }
        fn num(&self) -> f64 {
            match self {
                J::Int(i) => *i as f64,
                J::Flt(f) => *f,
                _ => 0.0,
            }
        }
        fn int(&self) -> i64 {
            match self {
                J::Int(i) => *i,
                J::Flt(f) => *f as i64,
                _ => 0,
            }
        }
        fn s(&self) -> &str {
            match self {
                J::Str(s) => s.as_str(),
                _ => "",
            }
        }
        fn arr(&self) -> &[J] {
            match self {
                J::Arr(a) => a.as_slice(),
                _ => &[],
            }
        }
        fn arr_mut(&mut self) -> &mut Vec<J> {
            if !matches!(self, J::Arr(_)) {
                *self = J::Arr(Vec::new());
            }
            match self {
                J::Arr(a) => a,
                _ => unreachable!(),
            }
        }
        fn get(&self, k: &str) -> &J {
            if let J::Obj(o) = self {
                for (kk, v) in o.iter() {
                    if kk == k {
                        return v;
                    }
                }
            }
            &NULLJ
        }
        fn at(&mut self, k: &str) -> &mut J {
            if !matches!(self, J::Obj(_)) {
                *self = J::Obj(Vec::new());
            }
            if let J::Obj(o) = self {
                let pos = o.iter().position(|(kk, _)| kk == k);
                let i = match pos {
                    Some(i) => i,
                    None => {
                        o.push((k.to_string(), J::Null));
                        o.len() - 1
                    }
                };
                return &mut o[i].1;
            }
            unreachable!()
        }
        fn set(&mut self, k: &str, v: J) {
            *self.at(k) = v;
        }
    }

    fn truthy(v: &J) -> bool {
        match v {
            J::Null => false,
            J::Bool(b) => *b,
            J::Int(i) => *i != 0,
            J::Flt(f) => *f != 0.0,
            J::Str(s) => !s.is_empty(),
            J::Arr(a) => !a.is_empty(),
            J::Obj(o) => !o.is_empty(),
        }
    }

    // -----------------------------------------------------------------------
    // Python string helpers
    // -----------------------------------------------------------------------

    fn py_space(c: char) -> bool {
        let c = c as u32;
        (9..=13).contains(&c)
            || (0x1C..=0x20).contains(&c)
            || c == 0x85
            || c == 0xA0
            || c == 0x1680
            || (0x2000..=0x200A).contains(&c)
            || c == 0x2028
            || c == 0x2029
            || c == 0x202F
            || c == 0x205F
            || c == 0x3000
    }

    fn py_strip(s: &str) -> &str {
        s.trim_matches(py_space)
    }

    fn py_split_ws(s: &str) -> Vec<String> {
        s.split(py_space).filter(|x| !x.is_empty()).map(|x| x.to_string()).collect()
    }

    fn py_lower(s: &str) -> String {
        s.to_lowercase()
    }

    fn py_upper(s: &str) -> String {
        s.to_uppercase()
    }

    // -----------------------------------------------------------------------
    // Python number formatting
    // -----------------------------------------------------------------------

    fn py_repr(v: f64) -> String {
        if v.is_nan() {
            return "nan".to_string();
        }
        if v.is_infinite() {
            return if v > 0.0 { "inf".to_string() } else { "-inf".to_string() };
        }
        if v == 0.0 {
            return if v.is_sign_negative() { "-0.0".to_string() } else { "0.0".to_string() };
        }
        let t = format!("{:e}", v);
        let (sign, t) = if let Some(r) = t.strip_prefix('-') { ("-", r.to_string()) } else { ("", t) };
        let e = t.find('e').unwrap();
        let mant = &t[..e];
        let exp: i32 = t[e + 1..].parse().unwrap_or(0);
        let digits: String = mant.chars().filter(|c| *c != '.').collect();
        let l = digits.len() as i32;
        if exp >= -4 && exp < 16 {
            if exp >= 0 {
                if l <= exp + 1 {
                    return format!("{}{}{}.0", sign, digits, "0".repeat((exp + 1 - l) as usize));
                }
                let (a, b) = digits.split_at((exp + 1) as usize);
                return format!("{}{}.{}", sign, a, b);
            }
            return format!("{}0.{}{}", sign, "0".repeat((-exp - 1) as usize), digits);
        }
        let ae = exp.abs();
        let mut out = format!("{}{}", sign, &digits[..1]);
        if l > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push_str(if exp < 0 { "e-" } else { "e+" });
        if ae < 10 {
            out.push('0');
        }
        out.push_str(&ae.to_string());
        out
    }

    fn py_fixed(v: f64, d: i64) -> String {
        if v.is_nan() {
            return "nan".to_string();
        }
        if v.is_infinite() {
            return if v > 0.0 { "inf".to_string() } else { "-inf".to_string() };
        }
        let d = d.clamp(0, 1000) as usize;
        format!("{:.*}", d, v)
    }

    fn is_integer(v: f64) -> bool {
        v.is_finite() && v == v.floor()
    }

    fn round_to_int(v: f64) -> Result<J, PyError> {
        if !v.is_finite() {
            return Err(PyError);
        }
        let r = v.round_ties_even();
        if r.abs() < 9.2e18 {
            Ok(J::Int(r as i64))
        } else {
            Ok(J::Flt(r))
        }
    }

    fn int_str(v: &J) -> String {
        match v {
            J::Int(i) => i.to_string(),
            other => py_fixed(other.num(), 0),
        }
    }

    fn py_quote(s: &str) -> String {
        let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
        let mut out = String::new();
        out.push(q);
        for c in s.chars() {
            if c == q || c == '\\' {
                out.push('\\');
                out.push(c);
            } else if c == '\n' {
                out.push_str("\\n");
            } else if c == '\r' {
                out.push_str("\\r");
            } else if c == '\t' {
                out.push_str("\\t");
            } else {
                out.push(c);
            }
        }
        out.push(q);
        out
    }

    fn py_repr_value(v: &J) -> String {
        match v {
            J::Str(s) => py_quote(s),
            J::Arr(a) => format!("[{}]", a.iter().map(py_repr_value).collect::<Vec<_>>().join(", ")),
            J::Obj(o) => format!(
                "{{{}}}",
                o.iter().map(|(k, v)| format!("{}: {}", py_quote(k), py_repr_value(v))).collect::<Vec<_>>().join(", ")
            ),
            other => py_str_value(other),
        }
    }

    fn py_str_value(v: &J) -> String {
        match v {
            J::Null => "None".to_string(),
            J::Bool(b) => if *b { "True".to_string() } else { "False".to_string() },
            J::Int(i) => i.to_string(),
            J::Flt(f) => py_repr(*f),
            J::Str(s) => s.clone(),
            other => py_repr_value(other),
        }
    }

    // -----------------------------------------------------------------------
    // JSON parsing / serialization
    // -----------------------------------------------------------------------

    trait Src {
        fn peek(&mut self) -> Option<u8>;
        fn next_byte(&mut self) -> Option<u8>;
    }

    struct StrSrc<'a> {
        s: &'a [u8],
        pos: usize,
    }

    impl<'a> Src for StrSrc<'a> {
        fn peek(&mut self) -> Option<u8> {
            self.s.get(self.pos).copied()
        }
        fn next_byte(&mut self) -> Option<u8> {
            let c = self.s.get(self.pos).copied();
            if c.is_some() {
                self.pos += 1;
            }
            c
        }
    }

    struct FileSrc {
        f: File,
        buf: Vec<u8>,
        pos: usize,
        len: usize,
    }

    impl FileSrc {
        fn new(f: File) -> FileSrc {
            let mut s = FileSrc { f, buf: vec![0u8; 1 << 16], pos: 0, len: 0 };
            if s.peek() == Some(0xEF) {
                s.fill_min(3);
                if s.len - s.pos >= 3 && s.buf[s.pos + 1] == 0xBB && s.buf[s.pos + 2] == 0xBF {
                    s.pos += 3;
                }
            }
            s
        }
        fn fill_min(&mut self, want: usize) {
            if self.len - self.pos >= want {
                return;
            }
            if self.pos > 0 {
                self.buf.copy_within(self.pos..self.len, 0);
                self.len -= self.pos;
                self.pos = 0;
            }
            while self.len < want {
                match self.f.read(&mut self.buf[self.len..]) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => self.len += n,
                }
            }
        }
    }

    impl Src for FileSrc {
        fn peek(&mut self) -> Option<u8> {
            if self.pos >= self.len {
                self.pos = 0;
                self.len = 0;
                loop {
                    match self.f.read(&mut self.buf[..]) {
                        Ok(n) => {
                            self.len = n;
                            break;
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                        Err(_) => break,
                    }
                }
                if self.len == 0 {
                    return None;
                }
            }
            Some(self.buf[self.pos])
        }
        fn next_byte(&mut self) -> Option<u8> {
            let c = self.peek();
            if c.is_some() {
                self.pos += 1;
            }
            c
        }
    }

    fn push_cp(out: &mut Vec<u8>, cp: u32) {
        // Encodes lone surrogates too (like Python's str would hold them).
        if cp < 0x80 {
            out.push(cp as u8);
        } else if cp < 0x800 {
            out.push((0xC0 | (cp >> 6)) as u8);
            out.push((0x80 | (cp & 0x3F)) as u8);
        } else if cp < 0x10000 {
            out.push((0xE0 | (cp >> 12)) as u8);
            out.push((0x80 | ((cp >> 6) & 0x3F)) as u8);
            out.push((0x80 | (cp & 0x3F)) as u8);
        } else {
            out.push((0xF0 | (cp >> 18)) as u8);
            out.push((0x80 | ((cp >> 12) & 0x3F)) as u8);
            out.push((0x80 | ((cp >> 6) & 0x3F)) as u8);
            out.push((0x80 | (cp & 0x3F)) as u8);
        }
    }

    fn bytes_to_string(b: Vec<u8>) -> String {
        match String::from_utf8(b) {
            Ok(s) => s,
            Err(e) => String::from_utf8_lossy(e.as_bytes()).into_owned(),
        }
    }

    struct JsonParser<'a, S: Src> {
        s: &'a mut S,
    }

    impl<'a, S: Src> JsonParser<'a, S> {
        fn ws(&mut self) {
            while let Some(c) = self.s.peek() {
                if c == b' ' || c == b'\t' || c == b'\n' || c == b'\r' {
                    self.s.next_byte();
                } else {
                    break;
                }
            }
        }
        fn lit(&mut self, w: &[u8]) -> bool {
            for &c in w {
                if self.s.next_byte() != Some(c) {
                    return false;
                }
            }
            true
        }
        fn hex4(&mut self) -> Option<u32> {
            let mut out = 0u32;
            for _ in 0..4 {
                let c = self.s.next_byte()?;
                let d = (c as char).to_digit(16)?;
                out = out * 16 + d;
            }
            Some(out)
        }
        fn simple_escape(e: u8, out: &mut Vec<u8>) -> bool {
            match e {
                b'"' => out.push(b'"'),
                b'\\' => out.push(b'\\'),
                b'/' => out.push(b'/'),
                b'b' => out.push(8),
                b'f' => out.push(12),
                b'n' => out.push(b'\n'),
                b'r' => out.push(b'\r'),
                b't' => out.push(b'\t'),
                _ => return false,
            }
            true
        }
        fn string(&mut self) -> Option<String> {
            let mut out: Vec<u8> = Vec::new();
            loop {
                let c = self.s.next_byte()?;
                if c == b'"' {
                    return Some(bytes_to_string(out));
                }
                if c != b'\\' {
                    out.push(c);
                    continue;
                }
                let e = self.s.next_byte()?;
                if e == b'u' {
                    let cp = self.hex4()?;
                    if (0xD800..0xDC00).contains(&cp) && self.s.peek() == Some(b'\\') {
                        self.s.next_byte();
                        if self.s.peek() == Some(b'u') {
                            self.s.next_byte();
                            let lo = self.hex4()?;
                            if (0xDC00..0xE000).contains(&lo) {
                                push_cp(&mut out, 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00));
                            } else {
                                push_cp(&mut out, cp);
                                push_cp(&mut out, lo);
                            }
                        } else {
                            push_cp(&mut out, cp);
                            let e2 = self.s.next_byte()?;
                            if !Self::simple_escape(e2, &mut out) {
                                return None;
                            }
                        }
                    } else {
                        push_cp(&mut out, cp);
                    }
                } else if !Self::simple_escape(e, &mut out) {
                    return None;
                }
            }
        }
        fn number(&mut self, first: u8) -> Option<J> {
            let mut tok = String::new();
            tok.push(first as char);
            let mut flt = false;
            while let Some(c) = self.s.peek() {
                if c.is_ascii_digit() || c == b'+' || c == b'-' {
                    tok.push(c as char);
                    self.s.next_byte();
                } else if c == b'.' || c == b'e' || c == b'E' {
                    flt = true;
                    tok.push(c as char);
                    self.s.next_byte();
                } else {
                    break;
                }
            }
            if tok == "-" && self.s.peek() == Some(b'I') {
                if !self.lit(b"Infinity") {
                    return None;
                }
                return Some(J::Flt(f64::NEG_INFINITY));
            }
            if !flt {
                if let Ok(v) = tok.parse::<i64>() {
                    return Some(J::Int(v));
                }
            }
            tok.parse::<f64>().ok().map(J::Flt)
        }
        fn value(&mut self, depth: usize) -> Option<J> {
            if depth > 10000 {
                return None;
            }
            self.ws();
            let c = self.s.next_byte()?;
            match c {
                b'{' => {
                    let mut o: Vec<(String, J)> = Vec::new();
                    self.ws();
                    if self.s.peek() == Some(b'}') {
                        self.s.next_byte();
                        return Some(J::Obj(o));
                    }
                    loop {
                        self.ws();
                        if self.s.next_byte()? != b'"' {
                            return None;
                        }
                        let k = self.string()?;
                        self.ws();
                        if self.s.next_byte()? != b':' {
                            return None;
                        }
                        let v = self.value(depth + 1)?;
                        if let Some(p) = o.iter_mut().find(|(kk, _)| *kk == k) {
                            p.1 = v;
                        } else {
                            o.push((k, v));
                        }
                        self.ws();
                        match self.s.next_byte()? {
                            b',' => continue,
                            b'}' => return Some(J::Obj(o)),
                            _ => return None,
                        }
                    }
                }
                b'[' => {
                    let mut a = Vec::new();
                    self.ws();
                    if self.s.peek() == Some(b']') {
                        self.s.next_byte();
                        return Some(J::Arr(a));
                    }
                    loop {
                        a.push(self.value(depth + 1)?);
                        self.ws();
                        match self.s.next_byte()? {
                            b',' => continue,
                            b']' => return Some(J::Arr(a)),
                            _ => return None,
                        }
                    }
                }
                b'"' => self.string().map(J::Str),
                b't' => if self.lit(b"rue") { Some(J::Bool(true)) } else { None },
                b'f' => if self.lit(b"alse") { Some(J::Bool(false)) } else { None },
                b'n' => if self.lit(b"ull") { Some(J::Null) } else { None },
                b'N' => if self.lit(b"aN") { Some(J::Flt(f64::NAN)) } else { None },
                b'I' => if self.lit(b"nfinity") { Some(J::Flt(f64::INFINITY)) } else { None },
                b'-' | b'0'..=b'9' => self.number(c),
                _ => None,
            }
        }
    }

    fn parse_json_text(text: &str) -> Option<J> {
        let mut src = StrSrc { s: text.as_bytes(), pos: 0 };
        let v = {
            let mut p = JsonParser { s: &mut src };
            let v = p.value(0)?;
            p.ws();
            v
        };
        if src.peek().is_some() {
            return None;
        }
        Some(v)
    }

    fn json_escape(s: &str, out: &mut String) {
        out.push('"');
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out.push('"');
    }

    fn json_dump(v: &J, out: &mut String) {
        match v {
            J::Null => out.push_str("null"),
            J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            J::Int(i) => out.push_str(&i.to_string()),
            J::Flt(f) => {
                if f.is_nan() {
                    out.push_str("NaN")
                } else if f.is_infinite() {
                    out.push_str(if *f > 0.0 { "Infinity" } else { "-Infinity" })
                } else {
                    out.push_str(&py_repr(*f))
                }
            }
            J::Str(s) => json_escape(s, out),
            J::Arr(a) => {
                out.push('[');
                for (k, x) in a.iter().enumerate() {
                    if k > 0 {
                        out.push(',');
                    }
                    json_dump(x, out);
                }
                out.push(']');
            }
            J::Obj(o) => {
                out.push('{');
                for (k, (key, x)) in o.iter().enumerate() {
                    if k > 0 {
                        out.push(',');
                    }
                    json_escape(key, out);
                    out.push(':');
                    json_dump(x, out);
                }
                out.push('}');
            }
        }
    }

    fn json_str(v: &J) -> String {
        let mut s = String::new();
        json_dump(v, &mut s);
        s
    }

    fn spec() -> &'static J {
        static SPEC: std::sync::OnceLock<J> = std::sync::OnceLock::new();
        SPEC.get_or_init(|| parse_json_text(SPEC_TEXT).expect("corrupt embedded spec"))
    }

    // -----------------------------------------------------------------------
    // Python semantics of the runtime helpers
    // -----------------------------------------------------------------------

    fn float_pattern(s: &str) -> bool {
        let b = s.as_bytes();
        let n = b.len();
        let mut k = 0;
        if k < n && (b[k] == b'+' || b[k] == b'-') {
            k += 1;
        }
        let mut d1 = 0;
        while k < n && b[k].is_ascii_digit() {
            k += 1;
            d1 += 1;
        }
        let mut d2 = 0;
        if k < n && b[k] == b'.' {
            k += 1;
            while k < n && b[k].is_ascii_digit() {
                k += 1;
                d2 += 1;
            }
        }
        if d1 == 0 && d2 == 0 {
            return false;
        }
        if k < n && (b[k] == b'e' || b[k] == b'E') {
            k += 1;
            if k < n && (b[k] == b'+' || b[k] == b'-') {
                k += 1;
            }
            let mut d3 = 0;
            while k < n && b[k].is_ascii_digit() {
                k += 1;
                d3 += 1;
            }
            if d3 == 0 {
                return false;
            }
        }
        k == n
    }

    fn parse_f64(t: &str) -> f64 {
        t.strip_prefix('+').unwrap_or(t).parse::<f64>().unwrap_or(f64::NAN)
    }

    fn to_num(v: &J) -> J {
        match v {
            J::Int(_) | J::Flt(_) => v.clone(),
            J::Str(s) => {
                let t = py_strip(s);
                if t.is_empty() {
                    return J::Null;
                }
                let digits = t.strip_prefix('+').or_else(|| t.strip_prefix('-')).unwrap_or(t);
                if !digits.is_empty() && digits.bytes().all(|c| c.is_ascii_digit()) {
                    let tt = t.strip_prefix('+').unwrap_or(t);
                    return match tt.parse::<i64>() {
                        Ok(i) => J::Int(i),
                        Err(_) => J::Flt(parse_f64(t)),
                    };
                }
                if float_pattern(t) {
                    return J::Flt(parse_f64(t));
                }
                J::Null
            }
            _ => J::Null,
        }
    }

    fn to_str(v: &J) -> Option<String> {
        match v {
            J::Null => None,
            J::Str(s) => Some(s.clone()),
            J::Bool(b) => Some(if *b { "true".to_string() } else { "false".to_string() }),
            J::Int(i) => Some(i.to_string()),
            J::Flt(f) => Some(py_repr(*f)),
            other => Some(py_str_value(other)),
        }
    }

    fn py_eq(a: &J, b: &J) -> bool {
        match (a, b) {
            (J::Bool(x), J::Bool(y)) => x == y,
            (J::Bool(_), _) | (_, J::Bool(_)) => false,
            (J::Null, J::Null) => true,
            (J::Null, _) | (_, J::Null) => false,
            (J::Int(x), J::Int(y)) => x == y,
            (J::Int(_) | J::Flt(_), J::Int(_) | J::Flt(_)) => a.num() == b.num(),
            (J::Int(_) | J::Flt(_), _) | (_, J::Int(_) | J::Flt(_)) => false,
            (J::Str(x), J::Str(y)) => x == y,
            (J::Arr(x), J::Arr(y)) => x.len() == y.len() && x.iter().zip(y.iter()).all(|(p, q)| py_eq(p, q)),
            (J::Obj(x), J::Obj(y)) => {
                x.len() == y.len() && x.iter().all(|(k, v)| has_key(b, k) && py_eq(v, b.get(k)))
            }
            _ => false,
        }
    }

    fn has_key(o: &J, k: &str) -> bool {
        if let J::Obj(v) = o {
            v.iter().any(|(kk, _)| kk == k)
        } else {
            false
        }
    }

    fn n_lt(a: &J, b: &J) -> bool {
        match (a, b) {
            (J::Int(x), J::Int(y)) => x < y,
            _ => a.num() < b.num(),
        }
    }

    fn n_eq(a: &J, b: &J) -> bool {
        match (a, b) {
            (J::Int(x), J::Int(y)) => x == y,
            _ => a.num() == b.num(),
        }
    }

    fn n_add(x: &J, y: &J) -> J {
        if let (J::Int(a), J::Int(b)) = (x, y) {
            return match a.checked_add(*b) {
                Some(r) => J::Int(r),
                None => J::Flt(*a as f64 + *b as f64),
            };
        }
        J::Flt(x.num() + y.num())
    }

    fn n_sub(x: &J, y: &J) -> J {
        if let (J::Int(a), J::Int(b)) = (x, y) {
            return match a.checked_sub(*b) {
                Some(r) => J::Int(r),
                None => J::Flt(*a as f64 - *b as f64),
            };
        }
        J::Flt(x.num() - y.num())
    }

    fn n_mul(x: &J, y: &J) -> J {
        if let (J::Int(a), J::Int(b)) = (x, y) {
            return match a.checked_mul(*b) {
                Some(r) => J::Int(r),
                None => J::Flt(*a as f64 * *b as f64),
            };
        }
        J::Flt(x.num() * y.num())
    }

    fn n_div(x: &J, y: &J) -> Result<J, PyError> {
        if y.num() == 0.0 {
            return Err(PyError);
        }
        Ok(J::Flt(x.num() / y.num()))
    }

    fn sop(s: String, op: &str) -> String {
        match op {
            "strip" => py_strip(&s).to_string(),
            "lower" => py_lower(&s),
            "upper" => py_upper(&s),
            _ => s,
        }
    }

    fn sops(v: &J, ops: &J) -> Option<String> {
        let mut s = to_str(v)?;
        for op in ops.arr() {
            s = sop(s, op.s());
        }
        Some(s)
    }

    fn py_round(y: J, d: &J) -> J {
        if d.is_null() {
            return y;
        }
        let nd = d.int();
        if let J::Int(i) = y {
            if nd >= 0 {
                return y;
            }
            if -nd > 18 {
                return J::Int(0);
            }
            let p = 10i64.pow((-nd) as u32);
            let mut q = i.div_euclid(p);
            let r = i.rem_euclid(p);
            if 2 * r > p || (2 * r == p && q % 2 != 0) {
                q += 1;
            }
            return match q.checked_mul(p) {
                Some(v) => J::Int(v),
                None => J::Flt(q as f64 * p as f64),
            };
        }
        let v = y.num();
        if !v.is_finite() {
            return J::Flt(v);
        }
        if nd >= 0 {
            if nd > 330 {
                return J::Flt(v);
            }
            return J::Flt(py_fixed(v, nd).parse::<f64>().unwrap_or(v));
        }
        let p = 10f64.powi((-nd) as i32);
        if !p.is_finite() {
            return J::Flt(0.0 * v);
        }
        J::Flt((v / p).round_ties_even() * p)
    }

    fn py_fmt(y: J, fmt: &J) -> Result<J, PyError> {
        let f = fmt.arr();
        let k = f.first().map(|x| x.s()).unwrap_or("");
        Ok(match k {
            "raw" => y,
            "int" => match y {
                J::Int(_) => y,
                _ => round_to_int(y.num())?,
            },
            "float" => J::Flt(y.num()),
            "intify" => {
                let v = y.num();
                if is_integer(v) && v.abs() < 1e16 {
                    J::Int(v as i64)
                } else {
                    J::Flt(v)
                }
            }
            "s_g" => match y {
                J::Flt(v) => {
                    if is_integer(v) && v.abs() < 1e16 {
                        J::Str((v as i64).to_string())
                    } else {
                        J::Str(py_repr(v))
                    }
                }
                J::Int(i) => J::Str(i.to_string()),
                other => other,
            },
            "s_int" => match y {
                J::Int(i) => J::Str(i.to_string()),
                other => J::Str(int_str(&round_to_int(other.num())?)),
            },
            "s_fixed" => J::Str(py_fixed(y.num(), f.get(1).map(|x| x.int()).unwrap_or(0))),
            "s_repr" => J::Str(py_repr(y.num())),
            _ => y,
        })
    }

    fn apply_t(t: &J, row: &J) -> J {
        let k = t.get("type").s();
        match k {
            "copy" => {
                let v = row.get(t.get("src").s());
                let ops = t.get("ops");
                if ops.arr().is_empty() {
                    return v.clone();
                }
                match sops(v, ops) {
                    Some(s) => J::Str(s),
                    None => J::Null,
                }
            }
            "const" => t.get("value").clone(),
            "linear" => {
                let v = row.get(t.get("src").s());
                let x = to_num(v);
                if x.is_null() {
                    return v.clone();
                }
                let y = n_add(&n_mul(t.get("a"), &x), t.get("b"));
                let y = py_round(y, t.get("round"));
                match py_fmt(y, t.get("fmt")) {
                    Ok(r) => r,
                    Err(_) => v.clone(),
                }
            }
            "affix" => match sops(row.get(t.get("src").s()), t.get("ops")) {
                Some(s) => J::Str(format!("{}{}{}", t.get("prefix").s(), s, t.get("suffix").s())),
                None => J::Null,
            },
            "split" => {
                let s = match sops(row.get(t.get("src").s()), t.get("ops")) {
                    Some(s) => s,
                    None => return J::Null,
                };
                let sep = t.get("sep");
                let mode = t.get("mode").s();
                let mut p: J;
                if mode == "idx" {
                    let parts: Vec<String> = if sep.is_null() {
                        py_split_ws(&s)
                    } else if sep.s().is_empty() {
                        return J::Null;
                    } else {
                        s.split(sep.s()).map(|x| x.to_string()).collect()
                    };
                    let i = t.get("index").int();
                    let n = parts.len() as i64;
                    if -n <= i && i < n {
                        p = J::Str(parts[(if i < 0 { i + n } else { i }) as usize].clone());
                    } else {
                        p = t.get("default").clone();
                    }
                } else if mode == "rest" {
                    p = match s.find(sep.s()) {
                        Some(pos) => J::Str(s[pos + sep.s().len()..].to_string()),
                        None => t.get("default").clone(),
                    };
                } else {
                    p = match s.rfind(sep.s()) {
                        Some(pos) => J::Str(s[..pos].to_string()),
                        None => J::Str(s.clone()),
                    };
                }
                if truthy(t.get("post_strip")) {
                    if let J::Str(x) = &p {
                        p = J::Str(py_strip(x).to_string());
                    }
                }
                p
            }
            "concat" => {
                let a = sops(row.get(t.get("a").s()), t.get("ops")).unwrap_or_default();
                let b = sops(row.get(t.get("b").s()), t.get("ops")).unwrap_or_default();
                J::Str(format!("{}{}{}{}{}", t.get("prefix").s(), a, t.get("sep").s(), b, t.get("suffix").s()))
            }
            "arith" => {
                let x = to_num(row.get(t.get("a").s()));
                let y = to_num(row.get(t.get("b").s()));
                if x.is_null() || y.is_null() {
                    return t.get("default").clone();
                }
                let r = match t.get("op").s() {
                    "+" => Ok(n_add(&x, &y)),
                    "-" => Ok(n_sub(&x, &y)),
                    "*" => Ok(n_mul(&x, &y)),
                    _ => n_div(&x, &y),
                };
                let r = r.and_then(|r| py_fmt(py_round(r, t.get("round")), t.get("fmt")));
                match r {
                    Ok(v) => v,
                    Err(_) => t.get("default").clone(),
                }
            }
            "missing" => t.get("value").clone(),
            _ => J::Null,
        }
    }

    fn filt(f: &J, row: &J, prev: Option<&J>, nxt: Option<&J>) -> bool {
        if f.is_null() {
            return true;
        }
        let k = f.get("k").s();
        match k {
            "and" => return f.get("args").arr().iter().all(|a| filt(a, row, prev, nxt)),
            "or" => return f.get("args").arr().iter().any(|a| filt(a, row, prev, nxt)),
            "const" => return truthy(f.get("v")),
            "edge" => {
                let p = match prev {
                    Some(p) => p,
                    None => return false,
                };
                let c = filt(f.get("arg"), row, None, None);
                let pv = filt(f.get("arg"), p, None, None);
                return match f.get("mode").s() {
                    "rise" => c && !pv,
                    "fall" => pv && !c,
                    _ => c != pv,
                };
            }
            "same" => {
                let nb = if f.get("rel").num() < 0.0 { prev } else { nxt };
                let col = f.get("col").s();
                let r = match nb {
                    Some(n) => py_eq(row.get(col), n.get(col)),
                    None => false,
                };
                return if f.get("op").s() == "==" { r } else { !r };
            }
            "colcmp" => {
                let r = py_eq(row.get(f.get("a").s()), row.get(f.get("b").s()));
                return if f.get("op").s() == "==" { r } else { !r };
            }
            _ => {}
        }
        let rel = f.get("rel").num();
        let v: &J = if rel != 0.0 {
            match if rel < 0.0 { prev } else { nxt } {
                Some(src) => src.get(f.get("col").s()),
                None => &NULLJ,
            }
        } else {
            row.get(f.get("col").s())
        };
        let op = f.get("op").s();
        let c = f.get("v");
        match k {
            "eq" => {
                let r = py_eq(v, c);
                if op == "==" { r } else { !r }
            }
            "numeq" => {
                let x = to_num(v);
                let r = !x.is_null() && c.is_num() && n_eq(&x, c);
                if op == "==" { r } else { !r }
            }
            "num" => {
                let x = to_num(v);
                if x.is_null() || !c.is_num() {
                    return false;
                }
                match op {
                    "<" => n_lt(&x, c),
                    "<=" => n_lt(&x, c) || n_eq(&x, c),
                    ">" => n_lt(c, &x),
                    ">=" => n_lt(c, &x) || n_eq(&x, c),
                    "==" => n_eq(&x, c),
                    "!=" => !n_eq(&x, c),
                    _ => false,
                }
            }
            "str" => {
                let (x, y) = match (v, c) {
                    (J::Str(x), J::Str(y)) => (x, y),
                    _ => return false,
                };
                match op {
                    "<" => x < y,
                    "<=" => x <= y,
                    ">" => x > y,
                    ">=" => x >= y,
                    "==" => x == y,
                    "!=" => x != y,
                    _ => false,
                }
            }
            _ => false,
        }
    }

    // --- accumulators (plain or CPython 3.12 compensated sum) --------------

    fn acc_new() -> J {
        J::Arr(vec![J::Int(0), J::Int(0), J::Flt(0.0), J::Flt(0.0)])
    }

    fn acc_add(acc: &mut J, x: &J, neu: bool) {
        let a = acc.arr_mut();
        while a.len() < 4 {
            a.push(J::Int(0));
        }
        if !neu {
            a[1] = n_add(&a[1], x);
            return;
        }
        if a[0].num() == 0.0 {
            if let J::Int(_) = x {
                a[1] = n_add(&a[1], x);
                return;
            }
            a[0] = J::Int(1);
            a[2] = n_add(&a[1], x);
            a[3] = J::Flt(0.0);
            return;
        }
        if let J::Int(i) = x {
            a[2] = J::Flt(a[2].num() + *i as f64);
            return;
        }
        let xf = x.num();
        let f = a[2].num();
        let s = f + xf;
        let mut c = a[3].num();
        if f.abs() >= xf.abs() {
            c += (f - s) + xf;
        } else {
            c += (xf - s) + f;
        }
        a[3] = J::Flt(c);
        a[2] = J::Flt(s);
    }

    fn acc_val(acc: &J, neu: bool) -> J {
        let a = acc.arr();
        if a.len() < 4 {
            return J::Int(0);
        }
        if !neu || a[0].num() == 0.0 {
            return a[1].clone();
        }
        let mut f = a[2].num();
        let c = a[3].num();
        if c != 0.0 && c - c == 0.0 {
            f += c;
        }
        J::Flt(f)
    }

    fn better(fn_: &str, v: &J, m: &J) -> bool {
        m.is_null() || (fn_ == "min" && n_lt(v, m)) || (fn_ == "max" && n_lt(m, v))
    }

    fn st_init(t: &J) -> J {
        let mut s = J::Obj(Vec::new());
        match t.get("kind").s() {
            "agg" => {
                if truthy(t.get("w")) {
                    s.set("win", J::Arr(Vec::new()));
                    s.set("n", J::Int(0));
                } else {
                    s.set("a", acc_new());
                    s.set("c", J::Int(0));
                    s.set("m", J::Null);
                    s.set("n", J::Int(0));
                }
            }
            "counter" => s.set("c", J::Int(0)),
            "lag" => s.set("h", J::Arr(Vec::new())),
            "diff" => {
                s.set("p", J::Null);
                s.set("n", J::Int(0));
            }
            "fsm" => s.set("v", t.get("init").clone()),
            _ => {}
        }
        s
    }

    fn st_step(t: &J, st: &mut J, raw: &J, prev: Option<&J>) -> J {
        match t.get("kind").s() {
            "agg" => {
                let fn_ = t.get("fn").s();
                let neu = t.get("acc").s() == "neu";
                let x = if fn_ == "count" {
                    J::Int(if filt(t.get("cond"), raw, prev, None) { 1 } else { 0 })
                } else {
                    to_num(raw.get(t.get("src").s()))
                };
                let n = st.get("n").int() + 1;
                st.set("n", J::Int(n));
                let w = t.get("w");
                let (s, c, m);
                if truthy(w) {
                    let win = st.at("win").arr_mut();
                    win.push(x);
                    if (win.len() as f64) > w.num() {
                        win.remove(0);
                    }
                    let mut acc = acc_new();
                    let mut cnt = 0i64;
                    let mut mm = J::Null;
                    for v in win.iter() {
                        if v.is_null() {
                            continue;
                        }
                        acc_add(&mut acc, v, neu);
                        cnt += 1;
                        if better(fn_, v, &mm) {
                            mm = v.clone();
                        }
                    }
                    s = acc_val(&acc, neu);
                    c = cnt;
                    m = mm;
                } else {
                    if !x.is_null() {
                        acc_add(st.at("a"), &x, neu);
                        let cc = st.get("c").int() + 1;
                        st.set("c", J::Int(cc));
                        if better(fn_, &x, st.get("m")) {
                            st.set("m", x.clone());
                        }
                    }
                    s = acc_val(st.get("a"), neu);
                    c = st.get("c").int();
                    m = st.get("m").clone();
                }
                if truthy(t.get("full")) && n_lt(&J::Int(n), w) {
                    return J::Null;
                }
                match fn_ {
                    "sum" | "count" => s,
                    "mean" => {
                        if c != 0 {
                            n_div(&s, &J::Int(c)).unwrap_or(J::Null)
                        } else {
                            J::Null
                        }
                    }
                    _ => m,
                }
            }
            "counter" => {
                let mut c = st.get("c").int();
                if filt(t.get("cond"), raw, prev, None) {
                    c += 1;
                }
                st.set("c", J::Int(c));
                J::Int(c)
            }
            "lag" => {
                let kk = t.get("k").int();
                let h = st.at("h").arr_mut();
                let has = (h.len() as i64) >= kk;
                let r = if has && !h.is_empty() { h[0].clone() } else { J::Null };
                h.push(raw.get(t.get("src").s()).clone());
                if (h.len() as i64) > kk {
                    h.remove(0);
                }
                if has {
                    J::Arr(vec![r])
                } else {
                    J::Null
                }
            }
            "diff" => {
                let x = to_num(raw.get(t.get("src").s()));
                let p = st.get("p").clone();
                let n = st.get("n").int() + 1;
                st.set("n", J::Int(n));
                st.set("p", x.clone());
                if x.is_null() || p.is_null() {
                    return J::Null;
                }
                n_sub(&x, &p)
            }
            "fsm" => {
                for rule in t.get("rules").arr() {
                    let r = rule.arr();
                    if r.len() < 2 {
                        continue;
                    }
                    if filt(&r[0], raw, prev, None) {
                        st.set("v", r[1].clone());
                        break;
                    }
                }
                st.get("v").clone()
            }
            _ => J::Null,
        }
    }

    fn st_out(t: &J, s: J) -> J {
        if s.is_null() {
            return t.get("none").clone();
        }
        match t.get("kind").s() {
            "lag" => return s.arr().first().cloned().unwrap_or(J::Null),
            "fsm" => return s,
            _ => {}
        }
        let labels = t.get("labels");
        if !labels.is_null() {
            let l = labels.arr();
            let n = l.len() as i64;
            if n == 0 {
                return J::Null;
            }
            let idx = s.int().rem_euclid(n);
            return l[idx as usize].clone();
        }
        let post = t.get("post");
        if post.is_null() {
            return s;
        }
        let row = J::Obj(vec![("_s".to_string(), s)]);
        apply_t(post, &row)
    }

    fn new_state(spec: &J) -> J {
        let mut s = J::Obj(Vec::new());
        s.set("prev", J::Null);
        s.set("pk", J::Null);
        s.set("pend", J::Null);
        s.set("hp", J::Bool(false));
        let sts: Vec<J> = spec
            .get("columns")
            .arr()
            .iter()
            .map(|col| {
                let t = &col.arr()[1];
                if t.get("type").s() == "st" {
                    st_init(t)
                } else {
                    J::Null
                }
            })
            .collect();
        s.set("st", J::Arr(sts));
        s
    }

    /// Row-at-a-time engine: neighbor-aware filtering (one row of lookahead)
    /// plus stateful columns.  All state lives in `s`, a JSON value.
    struct Engine {
        spec: &'static J,
        look: bool,
        s: J,
    }

    impl Engine {
        fn new(spec: &'static J) -> Engine {
            Engine { spec, look: truthy(spec.get("lookahead")), s: new_state(spec) }
        }

        fn push(&mut self, raw: J, out: &mut VecDeque<J>) {
            if !self.look {
                self.proc(&raw, None, out);
                return;
            }
            if truthy(self.s.get("hp")) {
                let cur = self.s.get("pend").clone();
                self.proc(&cur, Some(&raw), out);
            }
            self.s.set("pend", raw);
            self.s.set("hp", J::Bool(true));
        }

        fn finish(&mut self, out: &mut VecDeque<J>) {
            if self.look && truthy(self.s.get("hp")) {
                let r = self.s.get("pend").clone();
                self.s.set("pend", J::Null);
                self.s.set("hp", J::Bool(false));
                self.proc(&r, None, out);
            }
        }

        fn proc(&mut self, raw: &J, nxt: Option<&J>, out: &mut VecDeque<J>) {
            let spec = self.spec;
            let cols = spec.get("columns").arr();
            let prev_v = self.s.get("prev").clone();
            let prev = if prev_v.is_null() { None } else { Some(&prev_v) };
            let keep = filt(spec.get("filter"), raw, prev, nxt);
            let mut vals: Vec<J> = vec![J::Null; cols.len()];
            {
                let sts = self.s.at("st").arr_mut();
                for (i, col) in cols.iter().enumerate() {
                    let t = &col.arr()[1];
                    if t.get("type").s() == "st" && t.get("scope").s() == "all" {
                        if i < sts.len() {
                            vals[i] = st_out(t, st_step(t, &mut sts[i], raw, prev));
                        }
                    }
                }
            }
            self.s.set("prev", raw.clone());
            if !keep {
                return;
            }
            let pk_v = self.s.get("pk").clone();
            let pk = if pk_v.is_null() { None } else { Some(&pk_v) };
            let mut row: Vec<(String, J)> = Vec::with_capacity(cols.len());
            {
                let sts = self.s.at("st").arr_mut();
                for (i, col) in cols.iter().enumerate() {
                    let c = col.arr();
                    let name = c[0].s().to_string();
                    let t = &c[1];
                    let v = if t.get("type").s() == "st" {
                        if t.get("scope").s() != "all" && i < sts.len() {
                            vals[i] = st_out(t, st_step(t, &mut sts[i], raw, pk));
                        }
                        std::mem::replace(&mut vals[i], J::Null)
                    } else {
                        apply_t(t, raw)
                    };
                    if let Some(p) = row.iter_mut().find(|(k, _)| *k == name) {
                        p.1 = v;
                    } else {
                        row.push((name, v));
                    }
                }
            }
            self.s.set("pk", raw.clone());
            out.push_back(J::Obj(row));
        }
    }

    // -----------------------------------------------------------------------
    // Readers (csv / tsv / jsonl / json), streaming
    // -----------------------------------------------------------------------

    enum Source {
        Delim(DelimSource),
        Jsonl(JsonlSource),
        JsonArr(JsonArraySource),
    }

    impl Source {
        fn next_row(&mut self) -> Option<J> {
            match self {
                Source::Delim(s) => s.next_row(),
                Source::Jsonl(s) => s.next_row(),
                Source::JsonArr(s) => s.next_row(),
            }
        }
    }

    #[derive(Clone, Copy, PartialEq)]
    enum CsvSt {
        StartRecord,
        StartField,
        InField,
        InQuoted,
        QuoteInQuoted,
        EatCrnl,
    }

    /// Mirrors Python's csv.reader (default dialect, strict=False) fed by a
    /// file opened with newline=''.
    struct DelimSource {
        src: FileSrc,
        delim: u8,
        header: Option<Vec<String>>,
    }

    impl DelimSource {
        fn record(&mut self) -> Option<Vec<String>> {
            self.src.peek()?;
            let mut rec: Vec<String> = Vec::new();
            let mut fld: Vec<u8> = Vec::new();
            let mut state = CsvSt::StartRecord;
            let delim = self.delim;
            fn save(rec: &mut Vec<String>, fld: &mut Vec<u8>) {
                rec.push(bytes_to_string(std::mem::take(fld)));
            }
            loop {
                let mut line_had = false;
                loop {
                    let c = match self.src.next_byte() {
                        Some(c) => c,
                        None => break,
                    };
                    line_had = true;
                    let end_line = if c == b'\r' { self.src.peek() != Some(b'\n') } else { c == b'\n' };
                    let nl = c == b'\n' || c == b'\r';
                    if state == CsvSt::StartRecord {
                        if nl {
                            state = CsvSt::EatCrnl;
                        } else {
                            state = CsvSt::StartField;
                        }
                    }
                    if !(nl && state == CsvSt::EatCrnl) {
                        match state {
                            CsvSt::StartField => {
                                if nl {
                                    save(&mut rec, &mut fld);
                                    state = CsvSt::EatCrnl;
                                } else if c == b'"' {
                                    state = CsvSt::InQuoted;
                                } else if c == delim {
                                    save(&mut rec, &mut fld);
                                } else {
                                    fld.push(c);
                                    state = CsvSt::InField;
                                }
                            }
                            CsvSt::InField => {
                                if nl {
                                    save(&mut rec, &mut fld);
                                    state = CsvSt::EatCrnl;
                                } else if c == delim {
                                    save(&mut rec, &mut fld);
                                    state = CsvSt::StartField;
                                } else {
                                    fld.push(c);
                                }
                            }
                            CsvSt::InQuoted => {
                                if c == b'"' {
                                    state = CsvSt::QuoteInQuoted;
                                } else {
                                    fld.push(c);
                                }
                            }
                            CsvSt::QuoteInQuoted => {
                                if c == b'"' {
                                    fld.push(b'"');
                                    state = CsvSt::InQuoted;
                                } else if c == delim {
                                    save(&mut rec, &mut fld);
                                    state = CsvSt::StartField;
                                } else if nl {
                                    save(&mut rec, &mut fld);
                                    state = CsvSt::EatCrnl;
                                } else {
                                    fld.push(c);
                                    state = CsvSt::InField;
                                }
                            }
                            CsvSt::EatCrnl | CsvSt::StartRecord => {}
                        }
                    }
                    if end_line {
                        break;
                    }
                }
                if line_had {
                    match state {
                        CsvSt::StartRecord => {}
                        CsvSt::StartField | CsvSt::InField | CsvSt::QuoteInQuoted => {
                            save(&mut rec, &mut fld);
                            state = CsvSt::StartRecord;
                        }
                        CsvSt::InQuoted => {}
                        CsvSt::EatCrnl => state = CsvSt::StartRecord,
                    }
                    if state == CsvSt::StartRecord {
                        return Some(rec);
                    }
                    continue;
                }
                save(&mut rec, &mut fld);
                return Some(rec);
            }
        }

        fn next_row(&mut self) -> Option<J> {
            if self.header.is_none() {
                let h = self.record()?;
                self.header = Some(h);
            }
            loop {
                let rec = self.record()?;
                if rec.is_empty() {
                    continue;
                }
                let header = self.header.as_ref().unwrap();
                let mut row = J::Obj(Vec::with_capacity(header.len()));
                for (i, h) in header.iter().enumerate() {
                    row.set(h, if i < rec.len() { J::Str(rec[i].clone()) } else { J::Null });
                }
                return Some(row);
            }
        }
    }

    struct JsonlSource {
        src: FileSrc,
    }

    impl JsonlSource {
        fn line(&mut self) -> Option<Vec<u8>> {
            let mut out = Vec::new();
            let mut c = self.src.next_byte()?;
            loop {
                if c == b'\n' {
                    break;
                }
                if c == b'\r' {
                    if self.src.peek() == Some(b'\n') {
                        self.src.next_byte();
                    }
                    break;
                }
                out.push(c);
                c = match self.src.next_byte() {
                    Some(c) => c,
                    None => break,
                };
            }
            Some(out)
        }

        fn next_row(&mut self) -> Option<J> {
            loop {
                let ln = bytes_to_string(self.line()?);
                let s = py_strip(&ln);
                if s.is_empty() {
                    continue;
                }
                if let Some(obj @ J::Obj(_)) = parse_json_text(s) {
                    return Some(obj);
                }
            }
        }
    }

    struct JsonArraySource {
        src: FileSrc,
        started: bool,
        done: bool,
    }

    impl JsonArraySource {
        fn next_row(&mut self) -> Option<J> {
            while !self.done {
                while let Some(c) = self.src.peek() {
                    if c == b' ' || c == b'\t' || c == b'\r' || c == b'\n' || c == b',' {
                        self.src.next_byte();
                    } else {
                        break;
                    }
                }
                let c = match self.src.peek() {
                    Some(c) => c,
                    None => {
                        self.done = true;
                        break;
                    }
                };
                if !self.started {
                    self.started = true;
                    if c == b'[' {
                        self.src.next_byte();
                        continue;
                    }
                }
                if c == b']' {
                    self.done = true;
                    break;
                }
                let v = {
                    let mut p = JsonParser { s: &mut self.src };
                    p.value(0)
                };
                match v {
                    Some(obj @ J::Obj(_)) => return Some(obj),
                    Some(_) => continue,
                    None => {
                        self.done = true;
                        break;
                    }
                }
            }
            None
        }
    }

    fn open_source(path: &Path) -> std::io::Result<Source> {
        let f = File::open(path)?;
        let src = FileSrc::new(f);
        Ok(match spec().get("format").s() {
            "csv" => Source::Delim(DelimSource { src, delim: b',', header: None }),
            "tsv" => Source::Delim(DelimSource { src, delim: b'\t', header: None }),
            "jsonl" => Source::Jsonl(JsonlSource { src }),
            _ => Source::JsonArr(JsonArraySource { src, started: false, done: false }),
        })
    }

    // -----------------------------------------------------------------------
    // Cache helpers
    // -----------------------------------------------------------------------

    fn hash_hex(s: &[u8]) -> String {
        let mut h1: u64 = 1469598103934665603;
        let mut h2: u64 = 0x84222325cbf29ce4u64 ^ 0x9e3779b97f4a7c15u64;
        for &c in s {
            h1 ^= c as u64;
            h1 = h1.wrapping_mul(1099511628211);
            h2 ^= c as u64;
            h2 = h2.wrapping_mul(0x100000001b3);
            h2 ^= h2 >> 29;
        }
        format!("{:016x}{:016x}", h1, h2)
    }

    fn abs_path(p: &Path) -> PathBuf {
        let full = if p.is_absolute() {
            p.to_path_buf()
        } else {
            std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/")).join(p)
        };
        let mut out = PathBuf::new();
        for comp in full.components() {
            match comp {
                std::path::Component::CurDir => {}
                std::path::Component::ParentDir => {
                    out.pop();
                }
                other => out.push(other.as_os_str()),
            }
        }
        out
    }

    fn write_atomic(p: &Path, content: &str) {
        let mut tmp = p.as_os_str().to_owned();
        tmp.push(format!(".{}.tmp", std::process::id()));
        let tmp = PathBuf::from(tmp);
        let ok = fs::write(&tmp, content.as_bytes()).is_ok();
        if !ok || fs::rename(&tmp, p).is_err() {
            let _ = fs::remove_file(&tmp);
        }
    }

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    fn to_value(v: J) -> Value {
        match v {
            J::Null => Value::Null,
            J::Bool(b) => Value::Bool(b),
            J::Int(i) => Value::Int(i),
            J::Flt(f) => Value::Float(f),
            J::Str(s) => Value::Str(s),
            other => Value::Str(json_str(&other)),
        }
    }

    /// Streams a data file, applying the inferred filter / column transforms.
    ///
    /// `buffer` is the maximum number of input rows read and processed at
    /// once; with `cache_dir`, a checkpoint of the streaming state is stored
    /// at every buffer boundary (plus a consumed-row counter) so a later run
    /// resumes exactly where the previous one stopped.
    pub struct DynamicPreprocessor {
        buffer: usize,
        cache_dir: Option<PathBuf>,
        src: Option<Source>,
        eng: Option<Engine>,
        queue: VecDeque<J>,
        finished: bool,
        closed: bool,
        n_in: i64,
        emitted: i64,
        idx: i64,
        start: i64,
        state_p: Option<PathBuf>,
        pf: Option<File>,
    }

    impl DynamicPreprocessor {
        pub fn new(buffer: usize, cache_dir: Option<&str>) -> DynamicPreprocessor {
            DynamicPreprocessor {
                buffer: if buffer < 1 { 1 } else { buffer },
                cache_dir: cache_dir.map(PathBuf::from),
                src: None,
                eng: None,
                queue: VecDeque::new(),
                finished: false,
                closed: false,
                n_in: 0,
                emitted: 0,
                idx: 0,
                start: 0,
                state_p: None,
                pf: None,
            }
        }

        /// Open a data file (same format / schema as the sample input).
        pub fn open<P: AsRef<Path>>(&mut self, path: P) -> std::io::Result<()> {
            let path = path.as_ref();
            self.src = None;
            self.pf = None;
            self.state_p = None;
            self.queue.clear();
            self.finished = false;
            self.closed = false;
            self.n_in = 0;
            self.emitted = 0;
            self.idx = 0;
            self.start = 0;
            let mut eng = Engine::new(spec());
            let mut src = open_source(path)?;
            let cache_dir = match &self.cache_dir {
                None => {
                    self.eng = Some(eng);
                    self.src = Some(src);
                    return Ok(());
                }
                Some(c) => c.clone(),
            };
            let apath = abs_path(path);
            let apath_s = apath.to_string_lossy().into_owned();
            let mut keysrc = SPEC_ID.as_bytes().to_vec();
            keysrc.push(0);
            keysrc.extend_from_slice(apath_s.as_bytes());
            let d = cache_dir.join(format!("rs_{}", hash_hex(&keysrc)));
            fs::create_dir_all(&d)?;
            let meta_p = d.join("meta.json");
            let prog_p = d.join("progress");
            let state_p = d.join("state.json");
            let md = fs::metadata(path)?;
            let mtime: i128 = md
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i128)
                .unwrap_or(0);
            let mut meta = J::Obj(Vec::new());
            meta.set("path", J::Str(apath_s.clone()));
            meta.set("size", J::Int(md.len() as i64));
            meta.set("mtime_ns", J::Str(mtime.to_string()));
            meta.set("spec", J::Str(SPEC_ID.to_string()));
            let meta_s = json_str(&meta);
            let old = fs::read_to_string(&meta_p).ok();
            if old.as_deref() != Some(meta_s.as_str()) {
                let _ = fs::remove_file(&prog_p);
                let _ = fs::remove_file(&state_p);
                write_atomic(&meta_p, &meta_s);
            }
            let mut start: i64 = 0;
            if let Ok(t) = fs::read_to_string(&prog_p) {
                start = py_strip(&t).parse::<i64>().unwrap_or(0);
            }
            if start < 0 {
                start = 0;
            }
            self.start = start;
            if start > 0 {
                let ck = fs::read_to_string(&state_p).ok().and_then(|t| parse_json_text(&t));
                if let Some(ck) = ck {
                    let ok = ck.get("spec").s() == SPEC_ID
                        && matches!(ck.get("in"), J::Int(i) if *i >= 0)
                        && matches!(ck.get("out"), J::Int(o) if *o >= 0 && *o <= start)
                        && matches!(ck.get("state"), J::Obj(_));
                    if ok {
                        self.n_in = ck.get("in").int();
                        self.emitted = ck.get("out").int();
                        self.idx = self.emitted;
                        eng.s = ck.get("state").clone();
                        let mut skipped = 0i64;
                        while skipped < self.n_in {
                            if src.next_row().is_none() {
                                break;
                            }
                            skipped += 1;
                        }
                    }
                }
            }
            let pf = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(&prog_p)?;
            self.pf = Some(pf);
            self.state_p = Some(state_p);
            self.eng = Some(eng);
            self.src = Some(src);
            Ok(())
        }

        fn write_progress(&mut self, v: i64) {
            if let Some(pf) = self.pf.as_mut() {
                let s = format!("{:020}\n", v);
                let _ = pf.seek(SeekFrom::Start(0));
                let _ = pf.write_all(s.as_bytes());
                let _ = pf.flush();
            }
        }

        fn save_state(&self) {
            if let (Some(p), Some(eng)) = (&self.state_p, &self.eng) {
                let mut ck = J::Obj(Vec::new());
                ck.set("spec", J::Str(SPEC_ID.to_string()));
                ck.set("in", J::Int(self.n_in));
                ck.set("out", J::Int(self.emitted));
                ck.set("state", eng.s.clone());
                write_atomic(p, &json_str(&ck));
            }
        }

        fn fill(&mut self) {
            self.save_state();
            let mut got = 0usize;
            let (src, eng) = match (self.src.as_mut(), self.eng.as_mut()) {
                (Some(s), Some(e)) => (s, e),
                _ => {
                    self.finished = true;
                    return;
                }
            };
            while got < self.buffer {
                let raw = match src.next_row() {
                    Some(r) => r,
                    None => break,
                };
                got += 1;
                let before = self.queue.len();
                eng.push(raw, &mut self.queue);
                self.emitted += (self.queue.len() - before) as i64;
            }
            self.n_in += got as i64;
            if got == 0 {
                eng.finish(&mut self.queue);
                self.finished = true;
                self.src = None;
            }
        }

        fn next_j(&mut self) -> Option<J> {
            self.eng.as_ref()?;
            loop {
                if let Some(row) = self.queue.pop_front() {
                    if self.idx < self.start {
                        self.idx += 1;
                        continue;
                    }
                    self.idx += 1;
                    let i = self.idx;
                    self.write_progress(i);
                    return Some(row);
                }
                if self.finished {
                    if !self.closed {
                        self.closed = true;
                        if self.state_p.is_some() {
                            self.write_progress(0);
                            if let Some(p) = &self.state_p {
                                let _ = fs::remove_file(p);
                            }
                        }
                        self.pf = None;
                    }
                    return None;
                }
                self.fill();
            }
        }
    }

    impl Iterator for DynamicPreprocessor {
        type Item = Row;

        fn next(&mut self) -> Option<Row> {
            let j = self.next_j()?;
            let mut row = Row::new();
            if let J::Obj(o) = j {
                for (k, v) in o {
                    row.insert(k, to_value(v));
                }
            }
            Some(row)
        }
    }
}
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


def spec_id(spec):
    import hashlib
    return hashlib.sha256(spec_json(spec).encode("utf-8")).hexdigest()[:16]


def _fill(template, subs):
    # The spec text (which may contain arbitrary sample data) goes in last so
    # it is never scanned for placeholders.
    spec_text = subs.pop("@@SPEC@@", None)
    for k, v in subs.items():
        template = template.replace(k, v)
    if spec_text is not None:
        template = template.replace("@@SPEC@@", spec_text)
    return template


def gen_cpp(spec, module):
    text = spec_json(spec, indent=1)
    delim = "DBSPEC"
    n = 0
    while (")" + delim + '"') in text:
        n += 1
        delim = "DBSPEC%d" % n
    header = _fill(RT_CPP_H, {"@@MODULE@@": module, "@@NS@@": module})
    source = _fill(RT_CPP, {"@@MODULE@@": module, "@@NS@@": module, "@@SPECID@@": spec_id(spec),
                            "@@DELIM@@": delim, "@@SPEC@@": text})
    return header, source


def gen_rust(spec, module):
    text = spec_json(spec, indent=1)
    n = 1
    while ('"' + "#" * n) in text:
        n += 1
    cargo = _fill(RT_CARGO, {"@@NAME@@": module})
    lib = _fill(RT_RUST, {"@@MODULE@@": module, "@@SPECID@@": spec_id(spec), "@@HASHES@@": "#" * n,
                          "@@SPEC@@": text})
    return cargo, lib


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
    g.add_argument("--cpp", action="store_true")
    g.add_argument("--rust", action="store_true")
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
    elif args.cpp:
        header, source = gen_cpp(spec, module)
        pkg = os.path.join(args.output, module)
        write_file(os.path.join(pkg, "dynamic_preprocessor.h"), header)
        write_file(os.path.join(pkg, "dynamic_preprocessor.cpp"), source)
    elif args.rust:
        cargo, lib = gen_rust(spec, module)
        pkg = os.path.join(args.output, module)
        write_file(os.path.join(pkg, "Cargo.toml"), cargo)
        write_file(os.path.join(pkg, "src", "lib.rs"), lib)
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
