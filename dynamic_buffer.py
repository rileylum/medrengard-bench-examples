#!/usr/bin/env python3
"""dynamic_buffer.py -- infer a row transformation from a sample and generate
a streaming ``DynamicPreprocessor`` module (Python, JavaScript, C++ or Rust).

The C++ and Rust targets embed the same JSON spec as the Python/JS runtimes
and interpret it with a native port of the Python reference runtime, so all
four targets share one set of semantics (per-row transforms, stateful
features, buffering and cache_dir resume)."""
import argparse
import itertools
import json
import math
import os
import sys

# The runtime templates are defined at the bottom of this file.

FORMATS = ("csv", "tsv", "jsonl", "json")


def load_runtime():
    ns = {"__name__": "_dynamic_buffer_runtime"}
    code = RUNTIME_PY.replace("__SPEC_JSON__", repr(json.dumps({"format": "csv", "filter": None, "columns": []})))
    exec(compile(code, "<runtime>", "exec"), ns)
    return ns


RT = None


def rt():
    global RT
    if RT is None:
        RT = load_runtime()
    return RT


# ---------------------------------------------------------------- sample loading
def find_sample(sample_dir):
    for ext in FORMATS:
        ip = os.path.join(sample_dir, "input." + ext)
        op = os.path.join(sample_dir, "output." + ext)
        if os.path.isfile(ip) and os.path.isfile(op):
            return ext, ip, op
    names = sorted(os.listdir(sample_dir))
    ins = [n for n in names if n.startswith("input.")]
    outs = [n for n in names if n.startswith("output.")]
    if ins and outs:
        ext = ins[0].split(".", 1)[1].lower()
        return ext, os.path.join(sample_dir, ins[0]), os.path.join(sample_dir, outs[0])
    raise SystemExit("sample directory must contain input.{ext} and output.{ext}")


def read_header(path, delim):
    import csv
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line == "":
                continue
            return next(csv.reader([line], delimiter=delim))
    return []


def load_file(path, fmt):
    rows = [r for r, _ in rt()["read_rows"](path, fmt, None)]
    if fmt in ("csv", "tsv"):
        cols = read_header(path, "," if fmt == "csv" else "\t")
    else:
        cols = []
        seen = {}
        for r in rows:
            for k in r:
                if k not in seen:
                    seen[k] = True
                    cols.append(k)
    return cols, rows


# ---------------------------------------------------------------- helpers
def same(a, b):
    return rt()["same"](a, b)


def to_str(v):
    return rt()["to_str"](v)


def to_num(v):
    return rt()["to_num"](v)


def norm(v):
    s = to_str(v)
    if s is None:
        return None
    return s.strip().lower()


def vkey(v):
    """Hashable, type-aware key for a primitive value."""
    c = rt()["_cat"](v)
    if c == 2:
        return (2, float(v))
    return (c, v)


def is_numeric_value(v, fmt):
    if fmt in ("csv", "tsv"):
        return isinstance(v, str) and to_num(v) is not None
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def is_null(v, fmt):
    if v is None:
        return True
    if fmt in ("csv", "tsv") and isinstance(v, str) and v.strip() == "":
        return True
    return False


def decimals_of(v):
    """Number of decimals in the textual representation of a numeric value."""
    if isinstance(v, str):
        s = v.strip()
    elif isinstance(v, float):
        s = repr(v)
    else:
        return 0
    s = s.lower()
    if "e" in s:
        mant, exp = s.split("e", 1)
        try:
            e = int(exp)
        except ValueError:
            e = 0
        d = len(mant.split(".", 1)[1]) if "." in mant else 0
        return max(0, d - e)
    if "." in s:
        return len(s.split(".", 1)[1])
    return 0


# ---------------------------------------------------------------- alignment
_TOKEN_RE = None


def tokens(v):
    global _TOKEN_RE
    import re
    if _TOKEN_RE is None:
        _TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
    s = norm(v)
    if not s:
        return []
    return _TOKEN_RE.findall(s)


def in_profile(irow):
    vals = set()
    toks = set()
    for v in irow.values():
        n = norm(v)
        if n is not None and n != "":
            vals.add(n)
            toks.update(tokens(v))
    return vals, toks


def out_profile(orow, out_cols):
    prof = []
    for c in out_cols:
        ov = orow.get(c)
        n = norm(ov)
        if n is None or n == "":
            continue
        prof.append((n, tokens(ov)))
    return prof


def similarity(ip, op):
    vals, toks = ip
    score = 0.0
    for n, ot in op:
        if n in vals:
            score += 1.0
            continue
        if ot:
            score += 0.9 * sum(1 for t in ot if t in toks) / len(ot)
    return score


def dp_align(n, m, score):
    """Monotone injective alignment of m output rows into n input rows maximizing score."""
    NEG = float("-inf")
    f = [[NEG] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        f[i][0] = 0.0
    for i in range(1, n + 1):
        fi, fp = f[i], f[i - 1]
        for j in range(1, min(i, m) + 1):
            a = fp[j]
            b = fp[j - 1] + score(i - 1, j - 1) if fp[j - 1] != NEG else NEG
            fi[j] = a if a >= b else b
    res = [0] * m
    i, j = n, m
    while j > 0:
        if i > j and f[i - 1][j] == f[i][j]:
            i -= 1
            continue
        res[j - 1] = i - 1
        i -= 1
        j -= 1
    return res


# ---------------------------------------------------------------- column inference
CASES = [None, "lower", "upper", "title", "capitalize"]
SEPS = [None, " ", ",", ", ", ";", "|", "-", "_", "/", ".", "@", ":", "\t", "#", "+", "="]
SPLIT_IDX = [0, -1, 1, 2, -2, 3, -3, 4, 5]


def str_cores(xs):
    """Yield step lists in increasing complexity (no split, then split)."""
    base = []
    for strip in (False, True):
        for case in CASES:
            steps = []
            if strip:
                steps.append(["strip"])
            if case:
                steps.append([case])
            base.append(steps)
    base.sort(key=len)
    for s in base:
        yield s
    strs = [x for x in xs if isinstance(x, str)]
    for sep in SEPS:
        if sep is None:
            if not any(len(x.split()) > 1 for x in strs):
                continue
        elif not any(sep in x for x in strs):
            continue
        for k in SPLIT_IDX:
            for pre in (False, True):
                for post in (False, True):
                    for case in CASES:
                        steps = []
                        if pre:
                            steps.append(["strip"])
                        steps.append(["split", sep, k])
                        if post:
                            steps.append(["strip"])
                        if case:
                            steps.append([case])
                        yield steps


def check_str(xs, ys, steps, prefix, suffix):
    ap = rt()["apply_steps"]
    for x, y in zip(xs, ys):
        if x is None:
            if y is not None:
                return False
            continue
        if not isinstance(y, str):
            return False
        if y != prefix + ap(to_str(x), steps) + suffix:
            return False
    return True


def find_affix(xs, ys, steps):
    ap = rt()["apply_steps"]
    first = None
    for x, y in zip(xs, ys):
        if x is not None:
            if not isinstance(y, str):
                return None
            h = ap(to_str(x), steps)
            if h != "":
                first = (h, y)
                break
    if first is None:
        return None
    h, y = first
    pos = y.find(h)
    while pos >= 0:
        p, s = y[:pos], y[pos + len(h):]
        if (p or s) and check_str(xs, ys, steps, p, s):
            return p, s
        pos = y.find(h, pos + 1)
    return None


def nice_coef(v, check):
    for k in range(0, 13):
        c = round(v, k)
        if c == 0:
            c = 0.0
        if check(c):
            return c
    return v


def infer_linear(xs, ys, fmt, out_is_str, allow_identity=False):
    pts = []
    raws = []
    for x, y in zip(xs, ys):
        xn = to_num(x)
        if xn is None:
            if not same(x, y):
                return None
            continue
        if out_is_str:
            if not isinstance(y, str):
                return None
        else:
            if not (isinstance(y, (int, float)) and not isinstance(y, bool)):
                return None
        yn = to_num(y)
        if yn is None:
            return None
        pts.append((xn, yn, y))
        raws.append(x)
    if not pts:
        return None
    distinct = sorted(set(p[0] for p in pts))
    tol = []
    for xn, yn, y in pts:
        d = decimals_of(y)
        tol.append(max(1e-9 * max(1.0, abs(yn)), 10 ** (-d) * 0.75 + 1e-12))
    if len(distinct) == 1:
        x0, y0 = pts[0][0], pts[0][1]
        if x0 == y0:
            a, b = 1.0, 0.0
        else:
            a, b = 1.0, y0 - x0
    else:
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
        a = sxy / sxx
        b = my - a * mx

    def fits(a_, b_):
        return all(abs(a_ * xn + b_ - yn) <= t for (xn, yn, _), t in zip(pts, tol))

    if not fits(a, b):
        return None

    def check_a(ca):
        bb = sum(p[1] - ca * p[0] for p in pts) / len(pts)
        return fits(ca, bb) or any(fits(ca, round(bb, k)) for k in range(0, 13))

    a = nice_coef(a, check_a)
    b0 = sum(p[1] - a * p[0] for p in pts) / len(pts)
    b = nice_coef(b0, lambda cb: fits(a, cb))
    if not fits(a, b):
        return None
    a = float(a)
    b = float(b)
    if not allow_identity and a == 1.0 and b == 0.0 and all(same(x, y) for x, y in zip(xs, ys)):
        return None  # plain copy, handled elsewhere

    fn = rt()["format_num"]
    if out_is_str:
        maxd = max(decimals_of(p[2]) for p in pts)
        all_dot = all(any(ch in p[2] for ch in ".eE") for p in pts)
        order = ["repr", "g"] if all_dot else ["g", "repr"]
        fmts = [(None, order[0], 0), (None, order[1], 0)]
        for d in range(0, 13):
            fmts.append((d, order[0], 0))
        fmts.append((None, "fixed", maxd))
        for d in range(0, 13):
            fmts.append((d, order[1], 0))
    else:
        if all(isinstance(p[2], int) for p in pts):
            fl = ["int"]
        elif all(isinstance(p[2], float) for p in pts):
            fl = ["float"]
        else:
            fl = ["pytype", "int"]
        fmts = []
        for f in fl:
            fmts += [(None, f, 0)] + [(d, f, 0) for d in range(0, 13)]
    best = None
    best_hits = -1
    for r, f, dg in fmts:
        c = {"kind": "linear", "src": "x", "a": a, "b": b, "round": r, "fmt": f, "digits": dg}
        hits = 0
        for (xn, yn, y), xr in zip(pts, raws):
            try:
                v = rt()["apply_col"](c, {"x": xr})
            except (ValueError, OverflowError):
                v = None
            if same(v, y) and type(v) is type(y):
                hits += 1
        if hits > best_hits:
            best, best_hits = c, hits
        if hits == len(pts):
            break
    if best_hits < len(pts):
        # numerically right but formatting is unknown: round to the sample precision
        maxd = max(decimals_of(p[2]) for p in pts)
        if out_is_str:
            best = {"round": maxd, "fmt": "g", "digits": 0}
        else:
            best = {"round": maxd, "fmt": fl[0], "digits": 0}
    col = {"kind": "linear", "a": a, "b": b, "round": best["round"], "fmt": best["fmt"]}
    if best["fmt"] == "fixed":
        col["digits"] = best["digits"]
    return col


def infer_concat(pairs, name, sources):
    ys = [o.get(name) for _, o in pairs]
    if not ys or not all(isinstance(y, str) for y in ys):
        return None
    ap = rt()["apply_steps"]
    part_steps = [[], [["strip"]]]
    posts = [[], [["lower"]], [["upper"]], [["title"]]]
    for a in sources:
        for b in sources:
            if a == b:
                continue
            for ps in part_steps:
                for post in posts:
                    i0, y0 = pairs[0][0], ys[0]
                    sa = ap("" if i0.get(a) is None else to_str(i0.get(a)), ps)
                    sb = ap("" if i0.get(b) is None else to_str(i0.get(b)), ps)
                    ya = ap(sa, post)
                    yb = ap(sb, post)
                    if not (y0.startswith(ya) and y0.endswith(yb) and len(y0) >= len(ya) + len(yb)):
                        continue
                    if not ya or not yb:
                        continue
                    sep = y0[len(ya):len(y0) - len(yb)]
                    spec = {"kind": "concat",
                            "parts": [["col", a, ps], ["lit", sep], ["col", b, ps]],
                            "post": post}
                    ok = True
                    for (ir, _), y in zip(pairs, ys):
                        if rt()["apply_col"](spec, ir) != y:
                            ok = False
                            break
                    if ok:
                        return spec
    return None


def infer_column(name, pairs, in_cols, out_cols, fmt):
    ys = [o.get(name) for _, o in pairs]
    out_set = set(out_cols)
    sources = []
    if name in in_cols:
        sources.append(name)
    sources += [c for c in in_cols if c != name and c not in out_set]
    sources += [c for c in in_cols if c != name and c in out_set]
    xs_of = {c: [i.get(c) for i, _ in pairs] for c in in_cols}

    def all_same(xs):
        return all(same(x, y) for x, y in zip(xs, ys))

    if not pairs:
        if name in in_cols:
            return {"kind": "copy", "src": name}
        return {"kind": "const", "value": None}

    # 1. identical same-named column
    if name in in_cols and all_same(xs_of[name]):
        return {"kind": "copy", "src": name}

    constant = all(same(y, ys[0]) for y in ys)
    if constant and len(pairs) >= 2:
        if name not in in_cols:
            for c in in_cols:
                if c not in out_set and all_same(xs_of[c]):
                    return {"kind": "copy", "src": c}
        return {"kind": "const", "value": ys[0]}

    # 2. rename / copy
    for c in sources:
        if all_same(xs_of[c]):
            return {"kind": "copy", "src": c}

    out_is_str = all(isinstance(y, str) or y is None for y in ys)

    # 3. per-source transforms
    for c in sources:
        xs = xs_of[c]
        if out_is_str:
            cores = list(str_cores(xs))
            for steps in cores:
                if check_str(xs, ys, steps, "", ""):
                    return {"kind": "str", "src": c, "steps": steps, "prefix": "", "suffix": ""}
        lin = infer_linear(xs, ys, fmt, out_is_str)
        if lin is not None:
            lin = dict({"kind": "linear", "src": c}, **{k: v for k, v in lin.items() if k != "kind"})
            return lin
        if out_is_str:
            for steps in cores:
                aff = find_affix(xs, ys, steps)
                if aff is not None:
                    return {"kind": "str", "src": c, "steps": steps, "prefix": aff[0], "suffix": aff[1]}

    # 4. combination of two columns
    cc = infer_concat(pairs, name, sources)
    if cc is not None:
        return cc

    # 5. constant
    if constant:
        return {"kind": "const", "value": ys[0]}

    # 6. best effort: column agreeing with the most rows
    best, best_hits = None, -1
    for c in sources:
        hits = sum(1 for x, y in zip(xs_of[c], ys) if same(x, y))
        if hits > best_hits:
            best, best_hits = c, hits
    if best is not None and best_hits > 0:
        return {"kind": "copy", "src": best}
    if name in in_cols:
        return {"kind": "copy", "src": name}
    return {"kind": "const", "value": ys[0]}


def infer_columns(pairs, in_cols, out_cols, fmt):
    cols = []
    for name in out_cols:
        spec = infer_column(name, pairs, in_cols, out_cols, fmt)
        spec = dict({"name": name}, **spec)
        cols.append(spec)
    return cols


# ---------------------------------------------------------------- filter inference
def sig_digits(c):
    if c == 0:
        return 1
    s = repr(float(c)).lower().lstrip("-")
    if "e" in s:
        s = s.split("e")[0]
    s = s.replace(".", "").lstrip("0").rstrip("0")
    return max(1, len(s))


def clean_num(c):
    c = float(c)
    r = round(c, 12)
    return r if abs(r - c) < 1e-12 * max(1, abs(c)) else c


def nice_between(lo, hi, intonly):
    """Number in the open interval (lo, hi) with the fewest significant digits."""
    if lo is None or hi is None or not hi > lo:
        return None
    kmin = 0 if intonly else -6
    for k in range(15, kmin - 1, -1):
        step = 10.0 ** k
        c = (math.floor(lo / step) + 1) * step
        if k < 0:
            c = round(c, -k)
        if lo < c < hi:
            return clean_num(c)
    return None


def threshold_atom(col, lo, hi, direction, intonly):
    """direction 'ge': keep x >= c for some c in (lo, hi]; 'lt': keep x < c with c in (lo, hi]."""
    cands = []
    if direction == "ge":
        cands.append((hi, ">="))
        if lo is not None:
            cands.append((lo, ">"))
        nb = nice_between(lo, hi, intonly)
        if nb is not None:
            cands.append((nb, ">="))
    else:
        cands.append((lo, "<="))
        if hi is not None:
            cands.append((hi, "<"))
        nb = nice_between(lo, hi, intonly)
        if nb is not None:
            cands.append((nb, "<"))
    best = None
    for idx, (c, op) in enumerate(cands):
        key = (sig_digits(c), idx)
        if best is None or key < best[0]:
            best = (key, c, op)
    return {"op": "cmp", "col": col, "cmp": best[2], "value": clean_num(best[1]), "numeric": True}


class Atom:
    __slots__ = ("expr", "mask", "cost", "col", "order")

    def __init__(self, expr, mask, cost, col, order):
        self.expr, self.mask, self.cost, self.col, self.order = expr, mask, cost, col, order


def refine_thresholds(exprs, kind, in_rows, kept_set, fmt):
    """Re-pick numeric thresholds using only the rows each atom is responsible for."""
    ev = rt()["eval_filter"]
    n = len(in_rows)
    truth = [[ev(e, r) for r in in_rows] for e in exprs]
    out = list(exprs)
    for ai, e in enumerate(exprs):
        if not (e.get("op") == "cmp" and e.get("numeric") and e["cmp"] in ("<", "<=", ">", ">=")):
            continue
        others = [t for bi, t in enumerate(truth) if bi != ai]
        rej, acc = [], []
        for i in range(n):
            x = to_num(in_rows[i].get(e["col"])) if not is_null(in_rows[i].get(e["col"]), fmt) else None
            if x is None:
                continue
            if kind == "and":
                if i in kept_set:
                    acc.append(x)
                elif all(t[i] for t in others):
                    rej.append(x)
            else:
                if i not in kept_set:
                    rej.append(x)
                elif not any(t[i] for t in others):
                    acc.append(x)
        if not acc:
            continue
        vals = [to_num(r.get(e["col"])) for r in in_rows if not is_null(r.get(e["col"]), fmt)]
        intonly = all(v is not None and float(v).is_integer() for v in vals)
        if e["cmp"] in (">", ">="):
            hi = min(acc)
            lo_c = [x for x in rej if x < hi]
            if len(lo_c) != len(rej):
                continue
            lo = max(lo_c) if lo_c else None
            if lo is None:
                continue
            out[ai] = threshold_atom(e["col"], lo, hi, "ge", intonly)
        else:
            lo = max(acc)
            hi_c = [x for x in rej if x > lo]
            if len(hi_c) != len(rej):
                continue
            hi = min(hi_c) if hi_c else None
            if hi is None:
                continue
            out[ai] = threshold_atom(e["col"], lo, hi, "lt", intonly)
    return out


def infer_filter(in_rows, kept_idx, in_cols, fmt, penalty=None):
    penalty = penalty or {}
    n = len(in_rows)
    full = (1 << n) - 1
    K = 0
    for i in kept_idx:
        K |= 1 << i
    D = full & ~K
    if D == 0:
        return None
    if K == 0:
        return {"op": "false"}
    ev = rt()["eval_filter"]

    def mask_of(expr):
        m = 0
        for i, r in enumerate(in_rows):
            if ev(expr, r):
                m |= 1 << i
        return m

    kept = [i for i in range(n) if (K >> i) & 1]
    dropped = [i for i in range(n) if (D >> i) & 1]

    necessary, sufficient, pool = [], [], []
    seen_exprs = set()
    order = [0]

    generic = []

    def add(lst, expr, cost, col):
        m = mask_of(expr)
        key = json.dumps(expr, sort_keys=True)
        a = Atom(expr, m, cost, col, order[0])
        order[0] += 1
        lst.append(a)
        if key not in seen_exprs:
            seen_exprs.add(key)
            pool.append(a)

    for col in in_cols:
        vals = [r.get(col) for r in in_rows]
        nonnull = [v for v in vals if not is_null(v, fmt)]
        numeric = bool(nonnull) and all(is_numeric_value(v, fmt) for v in nonnull)
        distinct_keys = []
        dk_seen = set()
        for v in vals:
            k = vkey(v)
            if k not in dk_seen:
                dk_seen.add(k)
                distinct_keys.append(v)
        high_card = (len(distinct_keys) == n and n > 2) or len(distinct_keys) > max(3, n * 0.5)
        if numeric:
            nums = [to_num(v) if not is_null(v, fmt) else None for v in vals]
            intonly = all(float(x).is_integer() for x in nums if x is not None)
            nn = [x for x in nums if x is not None]
            id_like = len(set(nn)) == len(nn) == n and all(nn[i] < nn[i + 1] for i in range(len(nn) - 1))
            tcost = (3 if id_like else 1) + penalty.get(col, 0)
            uniq = sorted(set(nn))
            kn = [nums[i] for i in kept]
            dn = [nums[i] for i in dropped]
            # necessary bounds (true on every kept row)
            if kn and all(x is not None for x in kn):
                mk, Mk = min(kn), max(kn)
                below = [u for u in uniq if u < mk]
                if below:
                    add(necessary, threshold_atom(col, below[-1], mk, "ge", intonly), tcost, col)
                above = [u for u in uniq if u > Mk]
                if above:
                    add(necessary, threshold_atom(col, Mk, above[0], "lt", intonly), tcost, col)
            # sufficient bounds (false on every dropped row)
            dnn = [x for x in dn if x is not None]
            if dnn:
                md, mind = max(dnn), min(dnn)
                above = [u for u in uniq if u > md]
                if above:
                    add(sufficient, threshold_atom(col, md, above[0], "ge", intonly), tcost, col)
                below = [u for u in uniq if u < mind]
                if below:
                    add(sufficient, threshold_atom(col, below[-1], mind, "lt", intonly), tcost, col)
            # generic thresholds for the DNF fallback
            gaps = list(zip(uniq, uniq[1:]))
            if len(gaps) > 40:
                gaps = [gaps[(k * (len(gaps) - 1)) // 39] for k in range(40)]
            for a_, b_ in gaps:
                generic.append((threshold_atom(col, a_, b_, "ge", intonly), tcost + 1, col))
                generic.append((threshold_atom(col, a_, b_, "lt", intonly), tcost + 1, col))
        ecost = (3 if high_card else (2 if numeric else 1)) + penalty.get(col, 0)

        def eq_expr(v, op):
            if numeric and not is_null(v, fmt):
                return {"op": "cmp", "col": col, "cmp": op, "value": clean_num(to_num(v)), "numeric": True}
            return {"op": "cmp", "col": col, "cmp": op, "value": v, "numeric": False}

        kv, kv_seen = [], set()
        for i in kept:
            v = in_rows[i].get(col)
            k = vkey(v)
            if k not in kv_seen:
                kv_seen.add(k)
                kv.append(v)
        dv, dv_seen = [], set()
        for i in dropped:
            v = in_rows[i].get(col)
            k = vkey(v)
            if k not in dv_seen:
                dv_seen.add(k)
                dv.append(v)
        if numeric:
            # numeric equality must compare numerically; null values compare raw
            def k2(v):
                return ("n", to_num(v)) if not is_null(v, fmt) else ("r", vkey(v))
            kv_seen = set(k2(v) for v in kv)
            dv_seen = set(k2(v) for v in dv)
        else:
            def k2(v):
                return vkey(v)
        if len(kv) == 1 or (numeric and len(kv_seen) == 1):
            add(necessary, eq_expr(kv[0], "=="), ecost, col)
        for w in dv:
            if k2(w) not in kv_seen:
                add(necessary, eq_expr(w, "!="), ecost, col)
        if len(dv) == 1 or (numeric and len(dv_seen) == 1):
            add(sufficient, eq_expr(dv[0], "!="), ecost, col)
        for v in kv:
            if k2(v) not in dv_seen:
                add(sufficient, eq_expr(v, "=="), ecost, col)
        for v in distinct_keys[:40]:
            generic.append((eq_expr(v, "=="), ecost + 1, col))
            generic.append((eq_expr(v, "!="), ecost + 1, col))

    necessary = [a for a in necessary if (a.mask & K) == K and (a.mask & D) != D]
    sufficient = [a for a in sufficient if (a.mask & D) == 0 and (a.mask & K) != 0]

    # single atoms
    perfect = [a for a in necessary if a.mask == K]
    if perfect:
        c0 = min(a.cost for a in perfect)
        chosen, cols_used = [], set()
        for a in perfect:
            if a.cost == c0 and a.col not in cols_used:
                cols_used.add(a.col)
                chosen.append(a)
        exprs = refine_thresholds([a.expr for a in chosen], "and", in_rows, set(kept), fmt)
        if len(exprs) == 1:
            return exprs[0]
        return {"op": "and", "args": exprs}

    # small conjunctions / disjunctions
    best = None
    for size in (2, 3):
        for kind, lst in (("and", necessary), ("or", sufficient)):
            if len(lst) > 120 and size == 3:
                lst = sorted(lst, key=lambda a: (a.cost, a.order))[:120]
            for combo in itertools.combinations(lst, size):
                if kind == "and":
                    m = full
                    for a in combo:
                        m &= a.mask
                else:
                    m = 0
                    for a in combo:
                        m |= a.mask
                if m != K:
                    continue
                if len(set(a.col for a in combo)) < size and kind == "and":
                    pass
                cost = sum(a.cost for a in combo)
                key = (cost, size, 0 if kind == "and" else 1)
                if best is None or key < best[0]:
                    best = (key, kind, combo)
        if best is not None and best[0][0] <= size:
            break
    if best is not None:
        exprs = refine_thresholds([a.expr for a in best[2]], best[1], in_rows, set(kept), fmt)
        return {"op": best[1], "args": exprs}

    # greedy DNF fallback
    terms = []
    for expr, cost, col in generic:
        add(pool, expr, cost, col)
    singles = sorted(pool, key=lambda a: (a.cost, a.order))[:400]
    cands = []
    for a in singles:
        if (a.mask & D) == 0 and (a.mask & K):
            cands.append(((a.cost,), [a], a.mask))
    for a, b in itertools.combinations(singles, 2):
        m = a.mask & b.mask
        if (m & D) == 0 and (m & K):
            cands.append(((a.cost + b.cost,), [a, b], m))
    uncovered = K
    while uncovered and cands:
        bestc = None
        for c in cands:
            gain = bin(c[2] & uncovered).count("1")
            if gain == 0:
                continue
            key = (-gain, c[0])
            if bestc is None or key < bestc[0]:
                bestc = (key, c)
        if bestc is None:
            break
        c = bestc[1]
        uncovered &= ~c[2]
        atoms = c[1]
        terms.append(atoms[0].expr if len(atoms) == 1 else {"op": "and", "args": [x.expr for x in atoms]})
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return {"op": "or", "args": terms}


# ---------------------------------------------------------------- neighbour-aware filters
VPEN = {"prev": 4, "next": 4, "sameprev": 3, "samenext": 3, "eq": 3}


def virtual_defs(in_cols):
    defs = []
    for c in in_cols:
        for k in ("sameprev", "samenext", "prev", "next"):
            defs.append({"kind": k, "col": c, "name": "<%s>%s" % (k, c)})
    if len(in_cols) <= 24:
        for a, b in itertools.combinations(in_cols, 2):
            defs.append({"kind": "eq", "a": a, "b": b, "name": "<eq>%s<&>%s" % (a, b)})
    return defs


def filter_cols(f, acc):
    if not isinstance(f, dict):
        return acc
    if f.get("op") == "cmp":
        acc.add(f["col"])
    for a in f.get("args") or []:
        filter_cols(a, acc)
    return acc


def infer_filter_ext(in_rows, kept_idx, in_cols, fmt):
    """Per-row filter, falling back to neighbour / cross-column predicates."""
    defs = virtual_defs(in_cols)
    names = set(c for c in in_cols)
    defs = [d for d in defs if d["name"] not in names]
    aug_fn = rt()["augment"]
    n = len(in_rows)
    aug = [aug_fn(in_rows[i], in_rows[i - 1] if i > 0 else None, in_rows[i + 1] if i + 1 < n else None, defs)
           for i in range(n)]
    penalty = dict((d["name"], VPEN[d["kind"]]) for d in defs)
    cols = list(in_cols) + [d["name"] for d in defs]
    flt = infer_filter(aug, kept_idx, cols, fmt, penalty)
    used = filter_cols(flt, set())
    vdefs = [d for d in defs if d["name"] in used]
    return flt, vdefs


# ---------------------------------------------------------------- stateful columns
def numeric_col(seq, col, fmt):
    vals = [r.get(col) for r in seq]
    nonnull = [v for v in vals if not is_null(v, fmt)]
    return bool(nonnull) and all(is_numeric_value(v, fmt) for v in nonnull)


def nice_threshold(col, lo, hi, direction, intonly):
    """Threshold for a gap (lo, hi): fewest significant digits, preferring the
    boundary sample value `hi` (x >= hi / x < hi), then a round number inside
    the gap, then the low side (x > lo / x <= lo)."""
    op_hi, op_lo = (">=", ">") if direction == "ge" else ("<", "<=")
    cands = [(hi, op_hi)]
    nb = nice_between(lo, hi, intonly)
    if nb is not None:
        cands.append((nb, op_hi))
    cands.append((lo, op_lo))
    best = None
    for idx, (c, op) in enumerate(cands):
        key = (sig_digits(c), idx)
        if best is None or key < best[0]:
            best = (key, c, op)
    return {"op": "cmp", "col": col, "cmp": best[2], "value": clean_num(best[1]), "numeric": True}


def row_atoms(seq, in_cols, fmt, with_neq=False):
    """Simple single-column predicates over the rows of `seq`."""
    atoms = []
    for col in in_cols:
        vals = [r.get(col) for r in seq]
        numeric = numeric_col(seq, col, fmt)
        distinct, seen = [], set()
        for v in vals:
            if numeric and not is_null(v, fmt):
                k = ("n", to_num(v))
            else:
                k = ("r", vkey(v)) if not isinstance(v, (dict, list)) else ("j", json.dumps(v, sort_keys=True))
            if k not in seen:
                seen.add(k)
                distinct.append(v)
        if numeric:
            nums = sorted(set(to_num(v) for v in vals if not is_null(v, fmt)))
            intonly = all(float(x).is_integer() for x in nums)
            if len(distinct) <= 12:
                for v in distinct:
                    if is_null(v, fmt):
                        continue
                    atoms.append({"op": "cmp", "col": col, "cmp": "==", "value": clean_num(to_num(v)), "numeric": True})
                    if with_neq:
                        atoms.append({"op": "cmp", "col": col, "cmp": "!=", "value": clean_num(to_num(v)),
                                      "numeric": True})
            gaps = list(zip(nums, nums[1:]))
            if len(gaps) > 40:
                gaps = [gaps[(k * (len(gaps) - 1)) // 39] for k in range(40)]
            for a_, b_ in gaps:
                atoms.append(nice_threshold(col, a_, b_, "ge", intonly))
                atoms.append(nice_threshold(col, a_, b_, "lt", intonly))
        else:
            if len(distinct) > 16:
                continue
            for v in distinct:
                if isinstance(v, (dict, list)):
                    continue
                atoms.append({"op": "cmp", "col": col, "cmp": "==", "value": v, "numeric": False})
                if with_neq:
                    atoms.append({"op": "cmp", "col": col, "cmp": "!=", "value": v, "numeric": False})
    return atoms


def trigger_cands(seq, in_cols, fmt, atoms):
    cands = []
    for col in in_cols:
        cands.append({"kind": "change", "col": col})
    for a in atoms:
        for k in ("rise", "flip", "pred", "rise0"):
            cands.append({"kind": k, "pred": a})
    for col in in_cols:
        vals = [r.get(col) for r in seq]
        if any(isinstance(v, (dict, list)) for v in vals):
            continue
        distinct, seen = [], set()
        for v in vals:
            k = vkey(v)
            if k not in seen:
                seen.add(k)
                distinct.append(v)
        if 2 <= len(distinct) <= 8:
            for a in distinct:
                for b in distinct:
                    if vkey(a) != vkey(b):
                        cands.append({"kind": "trans", "col": col, "from": a, "to": b})
        if numeric_col(seq, col, fmt):
            nums = [to_num(v) for v in vals]
            diffs = sorted(set(nums[i] - nums[i - 1] for i in range(1, len(nums))
                               if nums[i] is not None and nums[i - 1] is not None))
            gaps = list(zip(diffs, diffs[1:]))
            if len(gaps) > 30:
                gaps = [gaps[(k * (len(gaps) - 1)) // 29] for k in range(30)]
            intonly = all(float(x).is_integer() for x in diffs)
            for a_, b_ in gaps:
                for d in ("ge", "lt"):
                    t = nice_threshold(col, a_, b_, d, intonly)
                    cands.append({"kind": "gap", "col": col, "cmp": t["cmp"], "value": t["value"]})
    return cands


def y_info(ys):
    out = []
    for y in ys:
        if isinstance(y, bool):
            out.append(None)
            continue
        yn = to_num(y)
        if yn is None:
            out.append(None)
            continue
        out.append((yn, max(1e-9 * max(1.0, abs(yn)), 10 ** (-decimals_of(y)) * 0.5 + 1e-9)))
    return out


class NumCheck:
    """Progressive check that ys ~ a * F + b (loose: a prefilter only)."""

    def __init__(self, strict_null, ys, yi):
        self.strict_null = strict_null
        self.ys = ys
        self.yi = yi
        self.null_set = False
        self.null = None
        self.p1 = None
        self.ab = None

    def add(self, f, j):
        y = self.ys[j]
        if f is None:
            if self.null_set:
                return same(y, self.null)
            if not (y is None or (isinstance(y, str) and y.strip() == "")):
                return False
            self.null_set, self.null = True, y
            return True
        info = self.yi[j]
        if info is None:
            return False
        yn, tol = info
        fn = float(f)
        if self.p1 is None:
            self.p1 = (fn, yn, tol)
            return True
        f1, y1, t1 = self.p1
        if self.ab is None:
            if fn == f1:
                return abs(yn - y1) <= t1 + tol
            a = (yn - y1) / (fn - f1)
            if a == 0:
                return False
            self.ab = (a, y1 - a * f1, fn - f1, t1 + tol)
            return True
        a, b, span, t0 = self.ab
        d = fn - f1
        r = d / span
        if r < 0:
            r = -r
        if r < 1.0:
            r = 1.0
        return abs(a * fn + b - yn) <= tol + t0 * r + 1e-9 * (abs(yn) if abs(yn) > 1 else 1.0)


class LabelCheck:
    def __init__(self, ys):
        self.ys = ys
        self.fy = {}
        self.yf = {}

    def add(self, f, j):
        y = self.ys[j]
        if f is None or isinstance(y, (dict, list)):
            return False
        kf, ky = vkey(f), vkey(y)
        if self.fy.setdefault(kf, ky) != ky:
            return False
        if self.yf.setdefault(ky, kf) != kf:
            return False
        return True


def run_feature(f, seq, pos, ys=None, check=None):
    """Feature values at `pos`; None if `check` rejects them early."""
    R = rt()
    step = R["feat_step"]
    st = R["feat_init"](f)
    out = []
    j = 0
    last = pos[-1]
    for i, row in enumerate(seq):
        if i > last:
            break
        v = step(f, st, row)
        if i == pos[j]:
            if check is not None and not check.add(v, j):
                return None
            out.append(v)
            j += 1
    return out


def quick_line(pairs, name):
    """False when the numeric outputs clearly are not a * feature + b."""
    pts = []
    for i_, o_ in pairs:
        y = o_.get(name)
        if isinstance(y, bool):
            return True
        yn = to_num(y)
        if yn is None:
            return True  # non-numeric output: let the general search decide
        pts.append((float(i_["__f"]), yn, max(1e-9 * max(1.0, abs(yn)), 10 ** (-decimals_of(y)) * 0.75 + 1e-9)))
    if len(pts) < 2:
        return True
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return all(abs(p[1] - pts[0][1]) <= p[2] + pts[0][2] for p in pts)
    a = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    b = my - a * mx
    return all(abs(a * p[0] + b - p[1]) <= 2 * p[2] for p in pts)


def finish_column(name, f, scope, seq, pos, ys, fmt, labels=False):
    """Fit the mapping feature-value -> output and verify it exactly."""
    R = rt()
    fv = run_feature(f, seq, pos)
    if fv is None or len(fv) != len(ys):
        return None
    col = {"name": name, "kind": "stateful", "scope": scope, "feature": f, "inner": None}
    nulls = [y for v, y in zip(fv, ys) if v is None]
    if nulls:
        col["null"] = nulls[0]
    if not labels:
        pairs = [({"__f": v}, {name: y}) for v, y in zip(fv, ys) if v is not None]
        if not pairs:
            return None
        numeric_f = all(isinstance(p[0]["__f"], (int, float)) and not isinstance(p[0]["__f"], bool) for p in pairs)
        if numeric_f and not quick_line(pairs, name):
            return None
        if numeric_f or not all(same(R["map_feat"](col, v), y) for v, y in zip(fv, ys)):
            inner = None
            xs = [p[0]["__f"] for p in pairs]
            if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in xs):
                out_is_str = all(isinstance(p[1][name], str) or p[1][name] is None for p in pairs)
                lin = infer_linear(xs, [p[1][name] for p in pairs], fmt, out_is_str, True)
                if lin is not None:
                    lin = dict({"kind": "linear", "src": "__f"}, **{k: v for k, v in lin.items() if k != "kind"})
                    col["inner"] = lin
                    if all(same(R["map_feat"](col, v), y) for v, y in zip(fv, ys)):
                        inner = lin
            if inner is None:
                col["inner"] = None
                if all(same(R["map_feat"](col, v), y) for v, y in zip(fv, ys)):
                    inner = {"kind": "copy", "src": "__f"}
            if inner is None:
                if numeric_f and all(to_num(p[1][name]) is not None and not isinstance(p[1][name], bool)
                                     for p in pairs):
                    return None
                inner = infer_column(name, pairs, ["__f"], [name], fmt)
            inner.pop("name", None)
            if inner.get("kind") == "const":
                return None
            col["inner"] = inner
    for v, y in zip(fv, ys):
        if not same(R["map_feat"](col, v), y):
            return None
    return col


def infer_mode(name, seq, pos, ys, scope, atoms, fmt, num_cols):
    ev = rt()["eval_filter"]
    keys = [vkey(y) for y in ys]
    labels = []
    for y in ys:
        if vkey(y) not in [vkey(l) for l in labels]:
            labels.append(y)
    if not (2 <= len(labels) <= 16):
        return None
    truth = [[bool(ev(a, r)) for r in seq] for a in atoms]
    rules = []
    for L in labels:
        kl = vkey(L)
        req = [pos[j] for j in range(1, len(ys)) if keys[j] == kl and keys[j - 1] != kl]
        if not req:
            continue
        forb = [pos[j] for j in range(len(ys)) if keys[j] != kl]
        found = None
        for ai, a in enumerate(atoms):
            if a.get("numeric") and a.get("cmp") not in ("==", "!="):
                continue
            t = truth[ai]
            if all(t[p] for p in req) and not any(t[p] for p in forb):
                found = a
                break
        if found is None:
            # widest numeric threshold separating the trigger rows from rows with other labels
            for col in num_cols:
                rv = [to_num(seq[p].get(col)) for p in req]
                fv = [to_num(seq[p].get(col)) for p in forb]
                if any(v is None for v in rv):
                    continue
                fv = [v for v in fv if v is not None]
                vals = sorted(set(v for v in (to_num(r.get(col)) for r in seq) if v is not None))
                intonly = all(float(v).is_integer() for v in vals)
                lo, hi = max(rv), min(rv)
                if all(v > lo for v in fv):
                    up = min(fv) if fv else None
                    if up is None:
                        above = [v for v in vals if v > lo]
                        up = above[0] if above else None
                    if up is not None:
                        found = nice_threshold(col, lo, up, "lt", intonly)
                        break
                if all(v < hi for v in fv):
                    dn = max(fv) if fv else None
                    if dn is None:
                        below = [v for v in vals if v < hi]
                        dn = below[-1] if below else None
                    if dn is not None:
                        found = nice_threshold(col, dn, hi, "ge", intonly)
                        break
        if found is None:
            return None
        rules.append([found, L])
    f = {"type": "mode", "rules": rules, "init": ys[0]}
    return finish_column(name, f, scope, seq, pos, ys, fmt, labels=True)


def counter_values(trig, seq, last, truth_of):
    """Fast trigger-count sequence (verified later with the runtime)."""
    kind = trig["kind"]
    ks = []
    k = 0
    if kind in ("pred", "rise", "rise0", "flip"):
        t = truth_of(trig["pred"])
        for i in range(last + 1):
            b = t[i]
            if kind == "pred":
                fire = b
            elif kind == "rise":
                fire = i > 0 and b and not t[i - 1]
            elif kind == "rise0":
                fire = b and not (i > 0 and t[i - 1])
            else:
                fire = i > 0 and b != t[i - 1]
            if fire:
                k += 1
            ks.append(k)
        return ks
    R = rt()
    st = R["feat_init"]({"type": "counter"})
    fire_fn = R["trig_fire"]
    for i in range(last + 1):
        if fire_fn(trig, st, seq[i]):
            k += 1
        ks.append(k)
    return ks


def infer_stateful(name, in_rows, align, in_cols, fmt, ys, has_filter):
    if not ys or not align:
        return None
    scopes = [("all", in_rows, list(align))]
    if has_filter:
        scopes.append(("kept", [in_rows[i] for i in align], list(range(len(align)))))
    ys_numeric = all((not isinstance(y, bool)) and (to_num(y) is not None or y is None
                                                   or (isinstance(y, str) and y.strip() == "")) for y in ys) \
        and any(to_num(y) is not None and not isinstance(y, bool) for y in ys)
    ys_hashable = all(not isinstance(y, (dict, list)) for y in ys)
    yi = y_info(ys)
    ctx = {}

    def info(scope, seq):
        if scope not in ctx:
            numc = [c for c in in_cols if numeric_col(seq, c, fmt)]
            atoms = row_atoms(seq, in_cols, fmt)
            ev = rt()["eval_filter"]
            tcache = {}

            def truth_of(a):
                k = json.dumps(a, sort_keys=True)
                if k not in tcache:
                    tcache[k] = [bool(ev(a, r)) for r in seq]
                return tcache[k]
            ctx[scope] = {"num": numc, "atoms": atoms, "truth": truth_of, "trig": None, "ks": {}}
        return ctx[scope]

    def try_num(f, scope, seq, pos, strict=False):
        if run_feature(f, seq, pos, ys, NumCheck(strict, ys, yi)) is None:
            return None
        return finish_column(name, f, scope, seq, pos, ys, fmt)

    def best_of(cands, scope, seq, pos, strict=False):
        first = None
        for f in cands:
            r = try_num(f, scope, seq, pos, strict)
            if r:
                if simple_map(r):
                    return r
                if first is None:
                    first = r
        return first

    # 1. prefix aggregates
    if ys_numeric:
        for scope, seq, pos in scopes:
            I = info(scope, seq)
            cands = [{"type": "prefix", "agg": agg, "src": c} for c in I["num"] for agg in ("sum", "mean")]
            cands.append({"type": "prefix", "agg": "count"})
            r = best_of(cands, scope, seq, pos)
            if r:
                return r
            cands = [{"type": "prefix", "agg": agg, "src": c} for c in I["num"] for agg in ("max", "min")]
            r = best_of(cands, scope, seq, pos)
            if r:
                return r

    # 2. counters (segment ids / session ids)
    if ys_hashable:
        for scope, seq, pos in scopes:
            I = info(scope, seq)
            if I["trig"] is None:
                I["trig"] = trigger_cands(seq, in_cols, fmt, I["atoms"])
            for trig in I["trig"]:
                key = json.dumps(trig, sort_keys=True)
                ks = counter_values(trig, seq, pos[-1], I["truth"])
                I["ks"][key] = ks
                chk = LabelCheck(ys)
                if not all(chk.add(ks[p], j) for j, p in enumerate(pos)):
                    continue
                if len(set(ks[p] for p in pos)) < 2:
                    continue
                for base in (0, 1):
                    f = {"type": "counter", "trigger": trig}
                    if base:
                        f["base"] = base
                    r = finish_column(name, f, scope, seq, pos, ys, fmt)
                    if r:
                        return r

    # 3. sliding windows
    if ys_numeric:
        for strict in (False, True):
            for scope, seq, pos in scopes:
                I = info(scope, seq)
                wmax = min(64, pos[-1] + (1 if strict else 0))
                for w in range(2, wmax + 1):
                    cands = []
                    for c in I["num"]:
                        cands += [{"type": "window", "w": w, "agg": "sum", "src": c},
                                  {"type": "window", "w": w, "agg": "mean", "src": c}]
                    cands.append({"type": "window", "w": w, "agg": "count"})
                    for c in I["num"]:
                        cands += [{"type": "window", "w": w, "agg": "max", "src": c},
                                  {"type": "window", "w": w, "agg": "min", "src": c}]
                    for f in cands:
                        if strict:
                            f["strict"] = True
                    r = best_of(cands, scope, seq, pos, strict)
                    if r:
                        return r

    # 4. predicate counts (prefix, then windows)
    if ys_numeric:
        for scope, seq, pos in scopes:
            I = info(scope, seq)
            patoms = row_atoms(seq, in_cols, fmt, with_neq=True)
            for a in patoms:
                r = try_num({"type": "prefix", "agg": "countp", "pred": a}, scope, seq, pos)
                if r:
                    return r
            pref = {}
            for a in patoms:
                t = I["truth"](a)
                acc = [0]
                for b_ in t:
                    acc.append(acc[-1] + (1 if b_ else 0))
                pref[id(a)] = acc
            for w in range(2, min(64, pos[-1]) + 1):
                for a in patoms:
                    acc = pref[id(a)]
                    # quick window-count check before running the runtime
                    chk = NumCheck(False, ys, yi)
                    ok = True
                    for j, p in enumerate(pos):
                        if not chk.add(acc[p + 1] - acc[max(0, p - w + 1)], j):
                            ok = False
                            break
                    if not ok:
                        continue
                    r = finish_column(name, {"type": "window", "w": w, "agg": "countp", "pred": a},
                                      scope, seq, pos, ys, fmt)
                    if r:
                        return r

    # 5. mode state machine, then 6. cyclic labels toggled by a trigger
    if ys_hashable:
        nlab = len(set(vkey(y) for y in ys))
        if 2 <= nlab <= 16:
            for scope, seq, pos in scopes:
                I = info(scope, seq)
                r = infer_mode(name, seq, pos, ys, scope, I["atoms"], fmt, I["num"])
                if r:
                    return r
            for scope, seq, pos in scopes:
                I = info(scope, seq)
                if I["trig"] is None:
                    I["trig"] = trigger_cands(seq, in_cols, fmt, I["atoms"])
                order = ([t for t in I["trig"] if t["kind"] == "trans"] +
                         [t for t in I["trig"] if t["kind"] == "change"] +
                         [t for t in I["trig"] if t["kind"] not in ("trans", "change")])
                for trig in order:
                    key = json.dumps(trig, sort_keys=True)
                    ks = I["ks"].get(key) or counter_values(trig, seq, pos[-1], I["truth"])
                    I["ks"][key] = ks
                    for L in range(2, nlab + 1):
                        lab = {}
                        ok = True
                        for p, y in zip(pos, ys):
                            r_ = ks[p] % L
                            if r_ in lab:
                                if not same(lab[r_], y) or vkey(lab[r_]) != vkey(y):
                                    ok = False
                                    break
                            else:
                                lab[r_] = y
                        if not ok or len(lab) != L or len(set(ks[p] for p in pos)) <= L:
                            continue
                        f = {"type": "cycle", "trigger": trig, "labels": [lab[i] for i in range(L)]}
                        r = finish_column(name, f, scope, seq, pos, ys, fmt, labels=True)
                        if r:
                            return r
    return None


def simple_map(col):
    inner = col.get("inner")
    if inner is None:
        return True
    k = inner.get("kind")
    if k == "linear":
        return inner.get("a") == 1.0 and inner.get("b") == 0.0
    if k == "str":
        return not inner.get("steps") and not inner.get("prefix") and not inner.get("suffix")
    return k == "copy"


def column_exact(col, pairs):
    ac = rt()["apply_col"]
    return all(same(ac(col, ir), orow.get(col["name"])) for ir, orow in pairs)


def add_stateful(cols, in_rows, out_rows, align, in_cols, fmt, has_filter):
    pairs = [(in_rows[i], out_rows[j]) for j, i in enumerate(align)]
    res = []
    for col in cols:
        if pairs and not column_exact(col, pairs):
            ys = [o.get(col["name"]) for _, o in pairs]
            st = infer_stateful(col["name"], in_rows, align, in_cols, fmt, ys, has_filter)
            if st is not None:
                col = st
        res.append(col)
    return res


# ---------------------------------------------------------------- driver
def canonical_align(in_rows, align, in_cols):
    """Within runs of identical input rows the alignment is arbitrary; move each
    aligned row to the earliest identical row of its run."""
    def key(r):
        return json.dumps([r.get(c) for c in in_cols], sort_keys=True)
    res = []
    prev = -1
    for i in align:
        k = key(in_rows[i])
        while i - 1 > prev and key(in_rows[i - 1]) == k:
            i -= 1
        res.append(i)
        prev = i
    return res


def transformed_anchors(in_rows, out_rows, in_cols, out_cols, n, m, limit=6):
    """Monotone alignments where one output column equals a simple transform
    (affix, offset, ratio) of one input column, found by greedy matching."""
    res = []
    if m == 0 or n * len(in_cols) * len(out_cols) * min(n, 20) > 3000000:
        return res
    for oc in out_cols:
        ys = [r.get(oc) for r in out_rows]
        yn = [norm(y) for y in ys]
        ynum = [to_num(y) if not isinstance(y, bool) else None for y in ys]
        if len(set(v for v in yn if v)) < max(2, m // 2):
            continue
        for ic in in_cols:
            xs = [r.get(ic) for r in in_rows]
            xn = [norm(x) for x in xs]
            xnum = [to_num(x) if not isinstance(x, bool) else None for x in xs]
            tried = set()
            for i in range(min(n - m + 1, 20)):
                ts = []
                if xnum[i] is not None and ynum[0] is not None:
                    ts.append(("add", ynum[0] - xnum[i]))
                    if xnum[i] != 0 and ynum[0] != 0:
                        ts.append(("mul", ynum[0] / xnum[i]))
                if xn[i] and yn[0] and xn[i] in yn[0] and xn[i] != yn[0]:
                    k = yn[0].find(xn[i])
                    ts.append(("aff", (yn[0][:k], yn[0][k + len(xn[i]):])))
                for t in ts:
                    if t in tried or t == ("add", 0.0) or t == ("mul", 1.0):
                        continue
                    tried.add(t)

                    def ok(a, j, t=t):
                        if t[0] == "aff":
                            return xn[a] is not None and yn[j] == t[1][0] + xn[a] + t[1][1]
                        if xnum[a] is None or ynum[j] is None:
                            return False
                        v = xnum[a] + t[1] if t[0] == "add" else xnum[a] * t[1]
                        return abs(v - ynum[j]) <= 1e-9 * max(1.0, abs(ynum[j]))
                    al = []
                    j = 0
                    for a in range(n):
                        if j < m and ok(a, j):
                            al.append(a)
                            j += 1
                    if j == m and al not in res:
                        res.append(al)
                        if len(res) >= limit:
                            return res
    return res


def infer(sample_dir):
    fmt, ip, op = find_sample(sample_dir)
    if fmt not in FORMATS:
        raise SystemExit("unsupported sample format: %s" % fmt)
    in_cols, in_rows = load_file(ip, fmt)
    out_cols, out_rows = load_file(op, fmt)
    n, m = len(in_rows), len(out_rows)

    if m >= n:
        align = list(range(min(n, m)))
        out_rows_used = out_rows[:len(align)]
        pairs = [(in_rows[i], out_rows_used[j]) for j, i in enumerate(align)]
        cols = infer_columns(pairs, in_cols, out_cols, fmt)
        cols = add_stateful(cols, in_rows, out_rows_used, align, in_cols, fmt, False)
        return {"format": fmt, "input_columns": in_cols, "filter": None, "columns": cols, "virtual": []}

    sims = {}
    in_profs = [in_profile(r) for r in in_rows]
    out_profs = [out_profile(r, out_cols) for r in out_rows]

    def s1(i, j):
        k = (i, j)
        if k not in sims:
            sims[k] = similarity(in_profs[i], out_profs[j])
        return sims[k]

    ac = rt()["apply_col"]

    def refine(align):
        cols = None
        for _ in range(4):
            pairs = [(in_rows[i], out_rows[j]) for j, i in enumerate(align)]
            cols = infer_columns(pairs, in_cols, out_cols, fmt)
            cache = {}

            def s2(i, j):
                k = (i, j)
                if k not in cache:
                    hits = 0
                    for c in cols:
                        if same(ac(c, in_rows[i]), out_rows[j].get(c["name"])):
                            hits += 1
                    cache[k] = hits + (len(cols) * 2 if hits == len(cols) else 0) + 0.001 * s1(i, j)
                return cache[k]

            new_align = dp_align(n, m, s2)
            if new_align == align:
                break
            align = new_align
        pairs = [(in_rows[i], out_rows[j]) for j, i in enumerate(align)]
        cols = infer_columns(pairs, in_cols, out_cols, fmt)
        quality = 0
        for ir, orow in pairs:
            for c in cols:
                if same(ac(c, ir), orow.get(c["name"])):
                    quality += 1
        return quality, align, cols

    # candidate alignments: exact matches through a single column pair, then
    # whole-row similarity
    candidates = []
    col_pairs = []
    for oc in out_cols:
        if oc in in_cols:
            col_pairs.append((oc, oc))
    for oc in out_cols:
        for ic in in_cols:
            if ic != oc:
                col_pairs.append((oc, ic))
    budget = 4000000
    in_norm = {ic: [norm(r.get(ic)) for r in in_rows] for ic in in_cols}
    for oc, ic in col_pairs:
        if n * m > budget:
            break
        budget -= n * m
        on = [norm(r.get(oc)) for r in out_rows]
        inn = in_norm[ic]
        if len(set(v for v in on if v)) < max(2, m // 2):
            continue  # too few distinct values to anchor on
        sc = lambda i, j, inn=inn, on=on: 1.0 if (on[j] and inn[i] == on[j]) else 0.0
        al = dp_align(n, m, sc)
        if all(inn[i] == on[j] for j, i in enumerate(al)):
            if al not in candidates:
                candidates.append(al)
    for al in transformed_anchors(in_rows, out_rows, in_cols, out_cols, n, m):
        if al not in candidates:
            candidates.append(al)
    a0 = dp_align(n, m, s1)
    if a0 not in candidates:
        candidates.append(a0)
    best = None
    seen = []
    for al in candidates[:12]:
        q, al2, cols = refine(al)
        if al2 in seen:
            continue
        seen.append(al2)
        if best is None or q > best[0]:
            best = (q, al2, cols)
        if q == m * len(out_cols):
            break
    _, align, cols = best
    align = canonical_align(in_rows, align, in_cols)
    flt, vdefs = infer_filter_ext(in_rows, align, in_cols, fmt)
    cols = add_stateful(cols, in_rows, out_rows, align, in_cols, fmt, flt is not None)
    return {"format": fmt, "input_columns": in_cols, "filter": flt, "columns": cols, "virtual": vdefs}


def spec_json(spec):
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=1)


def write_file(path, content):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def generate_python(spec, out_dir, module):
    pkg = os.path.join(out_dir, module)
    os.makedirs(pkg, exist_ok=True)
    sj = spec_json(spec)
    code = RUNTIME_PY.replace("__SPEC_JSON__", json.dumps(sj, ensure_ascii=False))
    write_file(os.path.join(pkg, "__init__.py"), code)


def generate_js(spec, out_dir, module):
    pkg = os.path.join(out_dir, module)
    os.makedirs(pkg, exist_ok=True)
    sj = spec_json(spec)
    lit = json.dumps(sj, ensure_ascii=True)
    code = RUNTIME_JS.replace("__SPEC_JSON__", lit)
    write_file(os.path.join(pkg, "index.js"), code)
    write_file(os.path.join(pkg, "package.json"),
               json.dumps({"name": module, "version": "1.0.0", "main": "index.js"}, indent=2) + "\n")
    write_file(os.path.join(out_dir, module + ".js"),
               '"use strict";\nmodule.exports = require("./' + module + '/index.js");\n')


def spec_hash(sj):
    import hashlib
    return hashlib.sha256(sj.encode("utf-8")).hexdigest()


def cpp_literal(s):
    """C++ raw string literal holding `s` (UTF-8 source)."""
    delim = "DBSPEC"
    k = 0
    while (")" + delim + '"') in s:
        k += 1
        delim = "DBSPEC%d" % k
    return 'R"' + delim + "(" + s + ")" + delim + '"'


def rust_literal(s):
    """Rust raw string literal holding `s`."""
    n = 1
    while ('"' + "#" * n) in s:
        n += 1
    return "r" + "#" * n + '"' + s + '"' + "#" * n


def generate_cpp(spec, out_dir, module):
    pkg = os.path.join(out_dir, module)
    os.makedirs(pkg, exist_ok=True)
    sj = spec_json(spec)
    header = RUNTIME_CPP_H.replace("__MODULE__", module)
    code = (RUNTIME_CPP.replace("__MODULE__", module)
            .replace("__SPEC_HASH__", spec_hash(sj))
            .replace("__SPEC_LITERAL__", cpp_literal(sj)))
    write_file(os.path.join(pkg, "dynamic_preprocessor.h"), header)
    write_file(os.path.join(pkg, "dynamic_preprocessor.cpp"), code)


def generate_rust(spec, out_dir, module):
    pkg = os.path.join(out_dir, module)
    os.makedirs(os.path.join(pkg, "src"), exist_ok=True)
    sj = spec_json(spec)
    code = (RUNTIME_RS.replace("__SPEC_HASH__", spec_hash(sj))
            .replace("__SPEC_LITERAL__", rust_literal(sj)))
    cargo = ("[package]\n"
             'name = "%s"\n'
             'version = "0.1.0"\n'
             'edition = "2021"\n'
             "publish = false\n"
             "\n"
             "[lib]\n"
             'name = "%s"\n'
             'path = "src/lib.rs"\n'
             'crate-type = ["rlib"]\n'
             "\n"
             "[dependencies]\n"
             "\n"
             "[profile.dev]\n"
             "opt-level = 2\n") % (module, module)
    write_file(os.path.join(pkg, "Cargo.toml"), cargo)
    write_file(os.path.join(pkg, "src", "lib.rs"), code)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("module_name")
    ap.add_argument("--output", required=True)
    ap.add_argument("--sample", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--python", action="store_true")
    g.add_argument("--javascript", action="store_true")
    g.add_argument("--cpp", action="store_true")
    g.add_argument("--rust", action="store_true")
    args = ap.parse_args(argv)
    spec = infer(args.sample)
    os.makedirs(args.output, exist_ok=True)
    if args.python:
        generate_python(spec, args.output, args.module_name)
    elif args.javascript:
        generate_js(spec, args.output, args.module_name)
    elif args.cpp:
        generate_cpp(spec, args.output, args.module_name)
    else:
        generate_rust(spec, args.output, args.module_name)
    return 0


RUNTIME_PY = r'''# Generated by dynamic_buffer.py -- do not edit.
import csv
import hashlib
import json
import math
import os
import re

__all__ = ["DynamicPreprocessor"]

_SPEC_JSON = __SPEC_JSON__
SPEC = json.loads(_SPEC_JSON)
SPEC_HASH = hashlib.sha256(_SPEC_JSON.encode("utf-8")).hexdigest()

_NUM_RE = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$")


# ---------------------------------------------------------------- values
def to_num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str) and _NUM_RE.match(v):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def fmt_repr(v):
    return repr(float(v))


def fmt_g(v):
    v = float(v)
    if math.isfinite(v) and v.is_integer() and abs(v) < 1e16:
        return str(int(v))
    return repr(v)


def fmt_fixed(v, d):
    return format(float(v), ".%df" % d)


def to_str(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return fmt_g(v)
    return str(v)


def _cat(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1
    if isinstance(v, (int, float)):
        return 2
    return 3


def same(a, b):
    ca, cb = _cat(a), _cat(b)
    if ca != cb:
        return False
    return a == b


def _isletter(c):
    return c.lower() != c.upper()


def _title(s):
    out = []
    prev = False
    for c in s:
        il = _isletter(c)
        if il:
            out.append(c.lower() if prev else c.upper())
        else:
            out.append(c)
        prev = il
    return "".join(out)


def _capitalize(s):
    if not s:
        return s
    return s[0].upper() + s[1:].lower()


def apply_steps(s, steps):
    for st in steps:
        op = st[0]
        if op == "strip":
            s = s.strip()
        elif op == "lower":
            s = s.lower()
        elif op == "upper":
            s = s.upper()
        elif op == "title":
            s = _title(s)
        elif op == "capitalize":
            s = _capitalize(s)
        elif op == "split":
            sep, k = st[1], st[2]
            parts = s.split() if sep is None else s.split(sep)
            s = parts[k] if -len(parts) <= k < len(parts) else ""
    return s


def format_num(y, c):
    r = c.get("round")
    if r is not None:
        y = round(y, r)
    f = c.get("fmt")
    if f == "int":
        if math.isfinite(y) and float(y).is_integer():
            return int(y)
        return float(y)
    if f == "float":
        return float(y)
    if f == "g":
        return fmt_g(y)
    if f == "repr":
        return fmt_repr(y)
    if f == "fixed":
        return fmt_fixed(y, c.get("digits", 0))
    return y


def apply_col(c, row):
    k = c["kind"]
    if k == "copy":
        return row.get(c["src"])
    if k == "const":
        return c["value"]
    if k == "str":
        v = row.get(c["src"])
        if v is None:
            return None
        return c["prefix"] + apply_steps(to_str(v), c["steps"]) + c["suffix"]
    if k == "concat":
        out = []
        for p in c["parts"]:
            if p[0] == "lit":
                out.append(p[1])
            else:
                v = row.get(p[1])
                s = "" if v is None else to_str(v)
                out.append(apply_steps(s, p[2]))
        return apply_steps("".join(out), c["post"])
    if k == "linear":
        v = row.get(c["src"])
        x = to_num(v)
        if x is None:
            return v
        y = format_num(c["a"] * x + c["b"], c)
        if c.get("fmt") == "pytype":
            if (isinstance(v, int) and not isinstance(v, bool) and float(c["a"]).is_integer()
                    and float(c["b"]).is_integer() and math.isfinite(y) and float(y).is_integer()):
                return int(y)
            return float(y)
        return y
    return None


def eval_filter(f, row):
    if f is None:
        return True
    op = f["op"]
    if op == "and":
        for a in f["args"]:
            if not eval_filter(a, row):
                return False
        return True
    if op == "or":
        for a in f["args"]:
            if eval_filter(a, row):
                return True
        return False
    if op == "true":
        return True
    if op == "false":
        return False
    if op == "cmp":
        v = row.get(f["col"])
        cmp = f["cmp"]
        val = f["value"]
        if f.get("numeric"):
            x = to_num(v)
            if x is None:
                return cmp == "!="
            if cmp == "==":
                return x == val
            if cmp == "!=":
                return x != val
            if cmp == "<":
                return x < val
            if cmp == "<=":
                return x <= val
            if cmp == ">":
                return x > val
            if cmp == ">=":
                return x >= val
            return False
        if cmp == "==":
            return same(v, val)
        if cmp == "!=":
            return not same(v, val)
        return False
    return True


def transform(row):
    """Return the transformed row, or None when the row is filtered out."""
    if not eval_filter(SPEC["filter"], row):
        return None
    out = {}
    for c in SPEC["columns"]:
        out[c["name"]] = apply_col(c, row)
    return out


# ---------------------------------------------------------------- stateful transforms
_INT_RE = re.compile(r"^\s*[-+]?\d+\s*$")
_COLS = SPEC.get("columns") or []
_VIRT = SPEC.get("virtual") or []
_LOOKAHEAD = any(v.get("kind") in ("next", "samenext") for v in _VIRT)
_ALL = [(str(i), c) for i, c in enumerate(_COLS) if c.get("kind") == "stateful" and c.get("scope") != "kept"]
_KEPT = [(str(i), c) for i, c in enumerate(_COLS) if c.get("kind") == "stateful" and c.get("scope") == "kept"]


def num_of(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        if _INT_RE.match(v):
            return int(v)
        return to_num(v)
    return None


def _cmp(x, op, val):
    if op == ">=":
        return x >= val
    if op == ">":
        return x > val
    if op == "<":
        return x < val
    if op == "<=":
        return x <= val
    if op == "==":
        return x == val
    if op == "!=":
        return x != val
    return False


def augment(row, prev, nxt, defs):
    """Row extended with virtual neighbour / cross-column columns."""
    if not defs:
        return row
    r = dict(row)
    for d in defs:
        k = d["kind"]
        if k == "prev":
            r[d["name"]] = None if prev is None else prev.get(d["col"])
        elif k == "next":
            r[d["name"]] = None if nxt is None else nxt.get(d["col"])
        elif k == "sameprev":
            r[d["name"]] = prev is not None and same(row.get(d["col"]), prev.get(d["col"]))
        elif k == "samenext":
            r[d["name"]] = nxt is not None and same(row.get(d["col"]), nxt.get(d["col"]))
        elif k == "eq":
            r[d["name"]] = same(row.get(d["a"]), row.get(d["b"]))
    return r


def trig_fire(tr, st, row):
    kind = tr["kind"]
    has = st["h"]
    pv = st["p"]
    if kind in ("pred", "rise", "rise0", "flip"):
        b = bool(eval_filter(tr["pred"], row))
        if kind == "pred":
            fire = b
        elif kind == "rise":
            fire = has and b and not pv
        elif kind == "rise0":
            fire = b and not (has and pv)
        else:
            fire = has and b != pv
        st["p"] = b
    else:
        v = row.get(tr["col"])
        if kind == "change":
            fire = has and not same(v, pv)
        elif kind == "trans":
            fire = has and same(pv, tr["from"]) and same(v, tr["to"])
        elif kind == "gap":
            x, px = to_num(v), to_num(pv)
            fire = has and x is not None and px is not None and _cmp(x - px, tr["cmp"], tr["value"])
        else:
            fire = False
        st["p"] = v
    st["h"] = True
    return bool(fire)


def feat_init(f):
    t = f["type"]
    if t == "prefix":
        return {"s": 0, "c": 0, "m": None}
    if t == "window":
        return {"b": []}
    if t == "mode":
        return {"l": f.get("init")}
    return {"k": f.get("base", 0), "h": False, "p": None}


def _contrib(f, row):
    agg = f["agg"]
    if agg == "countp":
        return 1 if eval_filter(f["pred"], row) else 0
    if agg == "count":
        return 1
    return num_of(row.get(f["src"]))


def _window_agg(agg, b):
    if agg in ("count", "countp"):
        s = 0
        for x in b:
            s = s + x
        return s
    s = 0
    c = 0
    m = None
    for x in b:
        if x is None:
            continue
        c += 1
        if agg == "min":
            if m is None or x < m:
                m = x
        elif agg == "max":
            if m is None or x > m:
                m = x
        else:
            s = s + x
    if c == 0:
        return None
    if agg == "sum":
        return s
    if agg == "mean":
        return s / c
    return m


def feat_step(f, st, row):
    """Advance the state `st` of feature `f` by one row and return its value."""
    t = f["type"]
    if t == "prefix":
        agg = f["agg"]
        x = _contrib(f, row)
        if agg in ("count", "countp"):
            st["s"] = st["s"] + x
            return st["s"]
        if x is not None:
            st["c"] += 1
            if agg == "min":
                if st["m"] is None or x < st["m"]:
                    st["m"] = x
            elif agg == "max":
                if st["m"] is None or x > st["m"]:
                    st["m"] = x
            else:
                st["s"] = st["s"] + x
        if st["c"] == 0:
            return None
        if agg == "sum":
            return st["s"]
        if agg == "mean":
            return st["s"] / st["c"]
        return st["m"]
    if t == "window":
        b = st["b"]
        b.append(_contrib(f, row))
        while len(b) > f["w"]:
            b.pop(0)
        if f.get("strict") and len(b) < f["w"]:
            return None
        return _window_agg(f["agg"], b)
    if t == "counter":
        if trig_fire(f["trigger"], st, row):
            st["k"] += 1
        return st["k"]
    if t == "cycle":
        if trig_fire(f["trigger"], st, row):
            st["k"] += 1
        labels = f["labels"]
        return labels[st["k"] % len(labels)]
    if t == "mode":
        for atom, label in f["rules"]:
            if eval_filter(atom, row):
                st["l"] = label
                break
        return st["l"]
    return None


def map_feat(c, v):
    if v is None:
        return c.get("null")
    inner = c.get("inner")
    if inner is None:
        return v
    return apply_col(inner, {"__f": v})


class _Machine:
    """Streaming state machine: per-row features, one-row lookahead and
    neighbour-aware filtering.  All state lives in the JSON-able dict `s`."""

    def __init__(self, snap=None):
        if snap is None:
            snap = {"prev": None, "pend": None, "pprev": None, "pvals": None,
                    "fa": dict((k, feat_init(c["feature"])) for k, c in _ALL),
                    "fk": dict((k, feat_init(c["feature"])) for k, c in _KEPT)}
        self.s = snap

    def _decide(self, row, prev, nxt, vals):
        r = augment(row, prev, nxt, _VIRT)
        if not eval_filter(SPEC.get("filter"), r):
            return None
        kv = {}
        for k, c in _KEPT:
            kv[k] = feat_step(c["feature"], self.s["fk"][k], row)
        out = {}
        for i, c in enumerate(_COLS):
            if c.get("kind") == "stateful":
                k = str(i)
                out[c["name"]] = map_feat(c, kv[k] if c.get("scope") == "kept" else vals[k])
            else:
                out[c["name"]] = apply_col(c, row)
        return out

    def push(self, row):
        s = self.s
        vals = {}
        for k, c in _ALL:
            vals[k] = feat_step(c["feature"], s["fa"][k], row)
        out = []
        if _LOOKAHEAD:
            if s["pend"] is not None:
                t = self._decide(s["pend"], s["pprev"], row, s["pvals"])
                if t is not None:
                    out.append(t)
            s["pend"], s["pprev"], s["pvals"] = row, s["prev"], vals
        else:
            t = self._decide(row, s["prev"], None, vals)
            if t is not None:
                out.append(t)
        s["prev"] = row
        return out

    def finish(self):
        s = self.s
        out = []
        if s["pend"] is not None:
            t = self._decide(s["pend"], s["pprev"], None, s["pvals"])
            if t is not None:
                out.append(t)
            s["pend"], s["pprev"], s["pvals"] = None, None, None
        return out


# ---------------------------------------------------------------- readers
def _strip_eol(b):
    if b.endswith(b"\n"):
        b = b[:-1]
    if b.endswith(b"\r"):
        b = b[:-1]
    return b


def _read_delimited(path, start, delim):
    """Yield (row, end_offset) for each data row at or after byte `start`."""
    with open(path, "rb") as fh:
        header = None
        pos = 0
        while True:
            line = fh.readline()
            if not line:
                return
            pos += len(line)
            text = _strip_eol(line).decode("utf-8")
            if text.startswith("﻿"):
                text = text[1:]
            if text == "":
                continue
            header = next(csv.reader([text], delimiter=delim))
            break
        if start is not None and start > pos:
            fh.seek(start)
            pos = start
        while True:
            line = fh.readline()
            if not line:
                return
            pos += len(line)
            text = _strip_eol(line).decode("utf-8")
            if text == "":
                continue
            fields = next(csv.reader([text], delimiter=delim), [])
            row = {}
            for i, h in enumerate(header):
                row[h] = fields[i] if i < len(fields) else None
            yield row, pos


def _read_jsonl(path, start):
    with open(path, "rb") as fh:
        pos = 0
        if start:
            fh.seek(start)
            pos = start
        while True:
            line = fh.readline()
            if not line:
                return
            pos += len(line)
            text = line.decode("utf-8").strip()
            if text.startswith("﻿"):
                text = text[1:].strip()
            if not text:
                continue
            obj = json.loads(text)
            if isinstance(obj, dict):
                yield obj, pos


def _read_json(path, start, chunk=65536):
    with open(path, "rb") as fh:
        pos = 0
        if start:
            fh.seek(start)
            pos = start
        depth = 0
        in_str = False
        esc = False
        cur = None
        while True:
            data = fh.read(chunk)
            if not data:
                return
            seg_start = 0
            for i, b in enumerate(data):
                if in_str:
                    if esc:
                        esc = False
                    elif b == 0x5C:
                        esc = True
                    elif b == 0x22:
                        in_str = False
                    continue
                if b == 0x22:
                    if depth > 0:
                        in_str = True
                    continue
                if b == 0x7B or b == 0x5B:
                    if b == 0x7B and depth == 0:
                        cur = bytearray()
                        seg_start = i
                        depth = 1
                    elif depth > 0:
                        depth += 1
                    continue
                if (b == 0x7D or b == 0x5D) and depth > 0:
                    depth -= 1
                    if depth == 0:
                        cur += data[seg_start:i + 1]
                        obj = json.loads(cur.decode("utf-8"))
                        cur = None
                        if isinstance(obj, dict):
                            yield obj, pos + i + 1
            if cur is not None:
                cur += data[seg_start:]
            pos += len(data)


def read_rows(path, fmt, start=None):
    if fmt == "csv":
        return _read_delimited(path, start, ",")
    if fmt == "tsv":
        return _read_delimited(path, start, "\t")
    if fmt == "jsonl":
        return _read_jsonl(path, start)
    if fmt == "json":
        return _read_json(path, start)
    raise ValueError("unsupported format: %r" % (fmt,))


# ---------------------------------------------------------------- preprocessor
def _dump(row):
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def _write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


class DynamicPreprocessor:
    def __init__(self, buffer=1024, cache_dir=None):
        try:
            buffer = int(buffer)
        except (TypeError, ValueError):
            buffer = 1024
        self.buffer = max(1, buffer)
        self.cache_dir = None if cache_dir is None else os.fspath(cache_dir)

    def __call__(self, path):
        path = os.fspath(path)
        if self.cache_dir is None:
            return self._stream(path, None)
        return self._cached(path)

    process = __call__

    # Events: (out_row, None) for each output row and (None, offset) after each
    # input row has been fully processed.  Input is read in batches of at most
    # `buffer` rows; history lives in the machine state, not in the batch.
    def _events(self, path, start, mach):
        it = read_rows(path, SPEC["format"], start)
        batch = []
        for row, off in it:
            batch.append((row, off))
            if len(batch) >= self.buffer:
                for r, o in batch:
                    for t in mach.push(r):
                        yield t, None
                    yield None, o
                batch = []
        for r, o in batch:
            for t in mach.push(r):
                yield t, None
            yield None, o
        for t in mach.finish():
            yield t, None

    def _stream(self, path, start):
        for t, _ in self._events(path, start, _Machine()):
            if t is not None:
                yield t

    def _cache_paths(self, path):
        ap = os.path.abspath(path)
        key = hashlib.sha256((ap + "\0" + SPEC_HASH).encode("utf-8")).hexdigest()[:40]
        d = os.path.join(self.cache_dir, key)
        return ap, d, os.path.join(d, "rows.jsonl"), os.path.join(d, "state.json")

    def _cached(self, path):
        ap, d, rows_path, state_path = self._cache_paths(path)
        os.makedirs(d, exist_ok=True)
        try:
            st = os.stat(path)
            fp = [st.st_size, st.st_mtime_ns]
        except OSError:
            fp = None
        state = None
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = None
        if not isinstance(state, dict) or state.get("fingerprint") != fp or state.get("path") != ap:
            state = {"path": ap, "fingerprint": fp, "complete": False, "out": 0, "offset": None}
            with open(rows_path, "wb"):
                pass
            _write_json_atomic(state_path, state)
        if state.get("complete"):
            return self._replay(rows_path)
        return self._resume(path, rows_path, state_path, state)

    def _replay(self, rows_path):
        with open(rows_path, "rb") as fh:
            for line in fh:
                if line.endswith(b"\n"):
                    yield json.loads(line.decode("utf-8"))

    def _resume(self, path, rows_path, state_path, state):
        # count fully written cached rows; drop a torn trailing line
        n = 0
        good = 0
        if os.path.exists(rows_path):
            with open(rows_path, "rb") as fh:
                pos = 0
                for line in fh:
                    if not line.endswith(b"\n"):
                        break
                    pos += len(line)
                    n += 1
                good = pos
            with open(rows_path, "r+b") as fh:
                fh.truncate(good)
        m = int(state.get("out") or 0)
        offset = state.get("offset")
        snap = state.get("snap")
        if m > n or (offset is not None and not isinstance(snap, dict)):
            m, offset, snap = 0, None, None
        mach = _Machine(snap if offset is not None else None)
        counter = m
        out = open(rows_path, "ab")
        try:
            steps = 0
            for t, off in self._events(path, offset, mach):
                if t is None:
                    steps += 1
                    if steps >= self.buffer:
                        steps = 0
                        state = dict(state, out=counter, offset=off, snap=mach.s)
                        _write_json_atomic(state_path, state)
                    continue
                counter += 1
                if counter <= n:
                    continue
                out.write((_dump(t) + "\n").encode("utf-8"))
                out.flush()
                yield t
            state = dict(state, out=counter, offset=None, snap=None, complete=True)
            _write_json_atomic(state_path, state)
        finally:
            out.close()
'''

RUNTIME_JS = r'''// Generated by dynamic_buffer.py -- do not edit.
"use strict";
const fs = require("fs");
const nodePath = require("path");
const crypto = require("crypto");
const { StringDecoder } = require("string_decoder");

const SPEC_JSON = __SPEC_JSON__;
const SPEC = JSON.parse(SPEC_JSON);
const SPEC_HASH = crypto.createHash("sha256").update(SPEC_JSON, "utf8").digest("hex");

const NUM_RE = /^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$/;

// ---------------------------------------------------------------- values
function toNum(v) {
  if (v === null || v === undefined || typeof v === "boolean") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && NUM_RE.test(v)) {
    const f = Number(v.trim());
    return Number.isFinite(f) ? f : null;
  }
  return null;
}

function fmtRepr(v) {
  if (!Number.isFinite(v)) return v > 0 ? "inf" : v < 0 ? "-inf" : "nan";
  if (v === 0) return Object.is(v, -0) ? "-0.0" : "0.0";
  const a = Math.abs(v);
  if (a < 1e-4 || a >= 1e16) {
    let s = v.toExponential();
    const m = /^(-?[\d.]+)e([+-])(\d+)$/.exec(s);
    if (m) {
      let exp = m[3];
      if (exp.length < 2) exp = "0" + exp;
      s = m[1] + "e" + m[2] + exp;
    }
    return s;
  }
  if (Number.isInteger(v)) return String(v) + ".0";
  return String(v);
}

function fmtG(v) {
  if (Number.isFinite(v) && Number.isInteger(v) && Math.abs(v) < 1e16) {
    return Object.is(v, -0) ? "0" : String(v);
  }
  return fmtRepr(v);
}

// Decimal string of v with exactly d decimals, rounding half-to-even on the
// exact binary value (matches Python's round() / format(v, ".Nf")).
function pyFixed(v, d) {
  if (!Number.isFinite(v)) return fmtRepr(v);
  if (Math.abs(v) >= 1e21) return v.toFixed(d);
  const neg = v < 0 || Object.is(v, -0);
  const s = Math.abs(v).toFixed(100);
  const dot = s.indexOf(".");
  const ip = s.slice(0, dot), fp = s.slice(dot + 1);
  const keep = fp.slice(0, d), rest = fp.slice(d);
  let digits = BigInt(ip + keep);
  let up = false;
  if (rest.length) {
    const c = rest[0];
    if (c > "5") up = true;
    else if (c === "5") {
      if (/[1-9]/.test(rest.slice(1))) up = true;
      else up = digits % 2n === 1n;
    }
  }
  if (up) digits += 1n;
  let str = digits.toString();
  if (d > 0) {
    while (str.length <= d) str = "0" + str;
    str = str.slice(0, str.length - d) + "." + str.slice(str.length - d);
  }
  return (neg ? "-" : "") + str;
}

function fmtFixed(v, d) {
  return pyFixed(v, d);
}

function toStr(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "string") return v;
  if (typeof v === "number") return fmtG(v);
  return String(v);
}

function cat(v) {
  if (v === null || v === undefined) return 0;
  if (typeof v === "boolean") return 1;
  if (typeof v === "number") return 2;
  return 3;
}

function same(a, b) {
  const ca = cat(a), cb = cat(b);
  if (ca !== cb) return false;
  if (ca === 0) return true;
  return a === b;
}

function isLetter(c) {
  return c.toLowerCase() !== c.toUpperCase();
}

function title(s) {
  let out = "";
  let prev = false;
  for (const c of s) {
    const il = isLetter(c);
    if (il) out += prev ? c.toLowerCase() : c.toUpperCase();
    else out += c;
    prev = il;
  }
  return out;
}

function capitalize(s) {
  if (!s) return s;
  const chars = Array.from(s);
  return chars[0].toUpperCase() + chars.slice(1).join("").toLowerCase();
}

function pyStrip(s) {
  return s.replace(/^[\s\x1c-\x1f\x85]+|[\s\x1c-\x1f\x85]+$/g, "");
}

function applySteps(s, steps) {
  for (const st of steps) {
    const op = st[0];
    if (op === "strip") s = pyStrip(s);
    else if (op === "lower") s = s.toLowerCase();
    else if (op === "upper") s = s.toUpperCase();
    else if (op === "title") s = title(s);
    else if (op === "capitalize") s = capitalize(s);
    else if (op === "split") {
      const sep = st[1], k = st[2];
      let parts;
      if (sep === null) {
        const t = pyStrip(s);
        parts = t === "" ? [] : t.split(/[\s\x1c-\x1f\x85]+/);
      } else {
        parts = s.split(sep);
      }
      const idx = k < 0 ? parts.length + k : k;
      s = idx >= 0 && idx < parts.length ? parts[idx] : "";
    }
  }
  return s;
}

function pyRound(y, r) {
  if (!Number.isFinite(y)) return y;
  if (Math.abs(y) >= 1e21) return y;
  const out = Number(pyFixed(y, r));
  return out === 0 ? (y < 0 || Object.is(y, -0) ? -0 : 0) : out;
}

function formatNum(y, c) {
  const r = c.round;
  if (r !== null && r !== undefined) y = pyRound(y, r);
  const f = c.fmt;
  if (f === "int" || f === "float" || f === "pytype") return y;
  if (f === "g") return fmtG(y);
  if (f === "repr") return fmtRepr(y);
  if (f === "fixed") return fmtFixed(y, c.digits || 0);
  return y;
}

function get(row, k) {
  const v = row[k];
  return v === undefined ? null : v;
}

function applyCol(c, row) {
  const k = c.kind;
  if (k === "copy") return get(row, c.src);
  if (k === "const") return c.value;
  if (k === "str") {
    const v = get(row, c.src);
    if (v === null) return null;
    return c.prefix + applySteps(toStr(v), c.steps) + c.suffix;
  }
  if (k === "concat") {
    let out = "";
    for (const p of c.parts) {
      if (p[0] === "lit") out += p[1];
      else {
        const v = get(row, p[1]);
        out += applySteps(v === null ? "" : toStr(v), p[2]);
      }
    }
    return applySteps(out, c.post);
  }
  if (k === "linear") {
    const v = get(row, c.src);
    const x = toNum(v);
    if (x === null) return v;
    return formatNum(c.a * x + c.b, c);
  }
  return null;
}

function evalFilter(f, row) {
  if (f === null || f === undefined) return true;
  const op = f.op;
  if (op === "and") {
    for (const a of f.args) if (!evalFilter(a, row)) return false;
    return true;
  }
  if (op === "or") {
    for (const a of f.args) if (evalFilter(a, row)) return true;
    return false;
  }
  if (op === "true") return true;
  if (op === "false") return false;
  if (op === "cmp") {
    const v = get(row, f.col);
    const cmp = f.cmp, val = f.value;
    if (f.numeric) {
      const x = toNum(v);
      if (x === null) return cmp === "!=";
      switch (cmp) {
        case "==": return x === val;
        case "!=": return x !== val;
        case "<": return x < val;
        case "<=": return x <= val;
        case ">": return x > val;
        case ">=": return x >= val;
        default: return false;
      }
    }
    if (cmp === "==") return same(v, val);
    if (cmp === "!=") return !same(v, val);
    return false;
  }
  return true;
}

function transform(row) {
  if (!evalFilter(SPEC.filter, row)) return null;
  const out = {};
  for (const c of SPEC.columns) out[c.name] = applyCol(c, row);
  return out;
}

// ---------------------------------------------------------------- stateful transforms
const INT_RE = /^\s*[-+]?\d+\s*$/;
const COLS = SPEC.columns || [];
const VIRT = SPEC.virtual || [];
const LOOKAHEAD = VIRT.some((v) => v.kind === "next" || v.kind === "samenext");
const ALL = [], KEPT = [];
COLS.forEach((c, i) => {
  if (c.kind === "stateful") (c.scope === "kept" ? KEPT : ALL).push([String(i), c]);
});

function numOf(v) {
  if (typeof v === "string" && INT_RE.test(v)) return Number(v.trim());
  return toNum(v);
}

function cmpOp(x, op, val) {
  switch (op) {
    case ">=": return x >= val;
    case ">": return x > val;
    case "<": return x < val;
    case "<=": return x <= val;
    case "==": return x === val;
    case "!=": return x !== val;
    default: return false;
  }
}

function augment(row, prev, nxt, defs) {
  if (!defs || !defs.length) return row;
  const r = Object.assign({}, row);
  for (const d of defs) {
    const k = d.kind;
    if (k === "prev") r[d.name] = prev === null ? null : get(prev, d.col);
    else if (k === "next") r[d.name] = nxt === null ? null : get(nxt, d.col);
    else if (k === "sameprev") r[d.name] = prev !== null && same(get(row, d.col), get(prev, d.col));
    else if (k === "samenext") r[d.name] = nxt !== null && same(get(row, d.col), get(nxt, d.col));
    else if (k === "eq") r[d.name] = same(get(row, d.a), get(row, d.b));
  }
  return r;
}

function trigFire(tr, st, row) {
  const kind = tr.kind;
  const has = st.h, pv = st.p;
  let fire = false;
  if (kind === "pred" || kind === "rise" || kind === "rise0" || kind === "flip") {
    const b = !!evalFilter(tr.pred, row);
    if (kind === "pred") fire = b;
    else if (kind === "rise") fire = has && b && !pv;
    else if (kind === "rise0") fire = b && !(has && pv);
    else fire = has && b !== pv;
    st.p = b;
  } else {
    const v = get(row, tr.col);
    if (kind === "change") fire = has && !same(v, pv);
    else if (kind === "trans") fire = has && same(pv, tr.from) && same(v, tr.to);
    else if (kind === "gap") {
      const x = toNum(v), px = toNum(pv);
      fire = has && x !== null && px !== null && cmpOp(x - px, tr.cmp, tr.value);
    }
    st.p = v === undefined ? null : v;
  }
  st.h = true;
  return !!fire;
}

function featInit(f) {
  const t = f.type;
  if (t === "prefix") return { s: 0, c: 0, m: null };
  if (t === "window") return { b: [] };
  if (t === "mode") return { l: f.init === undefined ? null : f.init };
  return { k: f.base === undefined ? 0 : f.base, h: false, p: null };
}

function contrib(f, row) {
  if (f.agg === "countp") return evalFilter(f.pred, row) ? 1 : 0;
  if (f.agg === "count") return 1;
  return numOf(get(row, f.src));
}

function windowAgg(agg, b) {
  if (agg === "count" || agg === "countp") {
    let s = 0;
    for (const x of b) s = s + x;
    return s;
  }
  let s = 0, c = 0, m = null;
  for (const x of b) {
    if (x === null) continue;
    c++;
    if (agg === "min") { if (m === null || x < m) m = x; }
    else if (agg === "max") { if (m === null || x > m) m = x; }
    else s = s + x;
  }
  if (c === 0) return null;
  if (agg === "sum") return s;
  if (agg === "mean") return s / c;
  return m;
}

function featStep(f, st, row) {
  const t = f.type;
  if (t === "prefix") {
    const agg = f.agg;
    const x = contrib(f, row);
    if (agg === "count" || agg === "countp") { st.s = st.s + x; return st.s; }
    if (x !== null) {
      st.c++;
      if (agg === "min") { if (st.m === null || x < st.m) st.m = x; }
      else if (agg === "max") { if (st.m === null || x > st.m) st.m = x; }
      else st.s = st.s + x;
    }
    if (st.c === 0) return null;
    if (agg === "sum") return st.s;
    if (agg === "mean") return st.s / st.c;
    return st.m;
  }
  if (t === "window") {
    const b = st.b;
    b.push(contrib(f, row));
    while (b.length > f.w) b.shift();
    if (f.strict && b.length < f.w) return null;
    return windowAgg(f.agg, b);
  }
  if (t === "counter") {
    if (trigFire(f.trigger, st, row)) st.k++;
    return st.k;
  }
  if (t === "cycle") {
    if (trigFire(f.trigger, st, row)) st.k++;
    return f.labels[st.k % f.labels.length];
  }
  if (t === "mode") {
    for (const [atom, label] of f.rules) {
      if (evalFilter(atom, row)) { st.l = label; break; }
    }
    return st.l;
  }
  return null;
}

function mapFeat(c, v) {
  if (v === null || v === undefined) return c.null === undefined ? null : c.null;
  if (c.inner === null || c.inner === undefined) return v;
  return applyCol(c.inner, { __f: v });
}

class Machine {
  constructor(snap) {
    if (!snap) {
      const fa = {}, fk = {};
      for (const [k, c] of ALL) fa[k] = featInit(c.feature);
      for (const [k, c] of KEPT) fk[k] = featInit(c.feature);
      snap = { prev: null, pend: null, pprev: null, pvals: null, fa, fk };
    }
    this.s = snap;
  }

  decide(row, prev, nxt, vals) {
    const r = augment(row, prev, nxt, VIRT);
    if (!evalFilter(SPEC.filter, r)) return null;
    const kv = {};
    for (const [k, c] of KEPT) kv[k] = featStep(c.feature, this.s.fk[k], row);
    const out = {};
    COLS.forEach((c, i) => {
      if (c.kind === "stateful") {
        const k = String(i);
        out[c.name] = mapFeat(c, c.scope === "kept" ? kv[k] : vals[k]);
      } else out[c.name] = applyCol(c, row);
    });
    return out;
  }

  push(row) {
    const s = this.s;
    const vals = {};
    for (const [k, c] of ALL) vals[k] = featStep(c.feature, s.fa[k], row);
    const out = [];
    if (LOOKAHEAD) {
      if (s.pend !== null) {
        const t = this.decide(s.pend, s.pprev, row, s.pvals);
        if (t !== null) out.push(t);
      }
      s.pend = row; s.pprev = s.prev; s.pvals = vals;
    } else {
      const t = this.decide(row, s.prev, null, vals);
      if (t !== null) out.push(t);
    }
    s.prev = row;
    return out;
  }

  finish() {
    const s = this.s;
    const out = [];
    if (s.pend !== null) {
      const t = this.decide(s.pend, s.pprev, null, s.pvals);
      if (t !== null) out.push(t);
      s.pend = null; s.pprev = null; s.pvals = null;
    }
    return out;
  }
}

// ---------------------------------------------------------------- readers
const CHUNK = 65536;

// Yields [lineBuffer, endOffset] for each line (without the trailing \n).
function* readLines(path, start) {
  const fd = fs.openSync(path, "r");
  try {
    let pos = start || 0;
    let pending = [];
    let pendingLen = 0;
    const buf = Buffer.allocUnsafe(CHUNK);
    while (true) {
      const n = fs.readSync(fd, buf, 0, CHUNK, pos);
      if (n <= 0) break;
      let s = 0;
      for (let i = 0; i < n; i++) {
        if (buf[i] === 0x0a) {
          const piece = Buffer.from(buf.subarray(s, i));
          let line = pendingLen ? Buffer.concat(pending.concat([piece])) : piece;
          pending = [];
          pendingLen = 0;
          s = i + 1;
          yield [line, pos + i + 1];
        }
      }
      if (s < n) {
        pending.push(Buffer.from(buf.subarray(s, n)));
        pendingLen += n - s;
      }
      pos += n;
    }
    if (pendingLen) yield [Buffer.concat(pending), pos];
  } finally {
    fs.closeSync(fd);
  }
}

function lineText(b) {
  let s = b.toString("utf8");
  if (s.endsWith("\r")) s = s.slice(0, -1);
  return s;
}

// Mirrors Python's csv.reader default dialect for a single line.
function parseDelimited(line, delim) {
  const fields = [];
  if (line.length === 0) return fields;
  let field = "";
  let state = 0; // 0 start, 1 unquoted, 2 quoted, 3 quote-in-quoted
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (state === 0) {
      if (c === '"') state = 2;
      else if (c === delim) { fields.push(""); }
      else { field += c; state = 1; }
    } else if (state === 1) {
      if (c === delim) { fields.push(field); field = ""; state = 0; }
      else field += c;
    } else if (state === 2) {
      if (c === '"') state = 3;
      else field += c;
    } else {
      if (c === '"') { field += '"'; state = 2; }
      else if (c === delim) { fields.push(field); field = ""; state = 0; }
      else { field += c; state = 1; }
    }
  }
  fields.push(field);
  return fields;
}

function* readDelimited(path, start, delim) {
  let header = null;
  let headerEnd = 0;
  const hl = readLines(path, 0);
  try {
    for (const [b, off] of hl) {
      let t = lineText(b);
      if (t.startsWith("﻿")) t = t.slice(1);
      if (t === "") continue;
      header = parseDelimited(t, delim);
      headerEnd = off;
      break;
    }
  } finally {
    hl.return();
  }
  if (header === null) return;
  const from = start !== null && start !== undefined && start > headerEnd ? start : headerEnd;
  for (const [b, off] of readLines(path, from)) {
    const t = lineText(b);
    if (t === "") continue;
    const fields = parseDelimited(t, delim);
    const row = {};
    for (let i = 0; i < header.length; i++) row[header[i]] = i < fields.length ? fields[i] : null;
    yield [row, off];
  }
}

function* readJsonl(path, start) {
  for (const [b, off] of readLines(path, start || 0)) {
    let t = b.toString("utf8");
    if (t.startsWith("﻿")) t = t.slice(1);
    t = t.trim();
    if (!t) continue;
    const obj = JSON.parse(t);
    if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) yield [obj, off];
  }
}

function* readJson(path, start) {
  const fd = fs.openSync(path, "r");
  try {
    let pos = start || 0;
    let depth = 0, inStr = false, esc = false;
    let cur = null;
    const buf = Buffer.allocUnsafe(CHUNK);
    while (true) {
      const n = fs.readSync(fd, buf, 0, CHUNK, pos);
      if (n <= 0) break;
      let segStart = 0;
      const ready = [];
      for (let i = 0; i < n; i++) {
        const b = buf[i];
        if (inStr) {
          if (esc) esc = false;
          else if (b === 0x5c) esc = true;
          else if (b === 0x22) inStr = false;
          continue;
        }
        if (b === 0x22) { if (depth > 0) inStr = true; continue; }
        if (b === 0x7b || b === 0x5b) {
          if (b === 0x7b && depth === 0) { cur = []; segStart = i; depth = 1; }
          else if (depth > 0) depth++;
          continue;
        }
        if ((b === 0x7d || b === 0x5d) && depth > 0) {
          depth--;
          if (depth === 0) {
            cur.push(Buffer.from(buf.subarray(segStart, i + 1)));
            const obj = JSON.parse(Buffer.concat(cur).toString("utf8"));
            cur = null;
            if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) ready.push([obj, pos + i + 1]);
          }
        }
      }
      if (cur !== null) cur.push(Buffer.from(buf.subarray(segStart, n)));
      pos += n;
      for (const r of ready) yield r;
    }
  } finally {
    fs.closeSync(fd);
  }
}

function readRows(path, fmt, start) {
  if (fmt === "csv") return readDelimited(path, start, ",");
  if (fmt === "tsv") return readDelimited(path, start, "\t");
  if (fmt === "jsonl") return readJsonl(path, start);
  if (fmt === "json") return readJson(path, start);
  throw new Error("unsupported format: " + fmt);
}

// ---------------------------------------------------------------- preprocessor
function writeJsonAtomic(p, obj) {
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(obj));
  fs.renameSync(tmp, p);
}

// Events: [row, null] per output row, [null, offset] after each input row.
function* events(path, start, bufferSize, mach) {
  let batch = [];
  for (const item of readRows(path, SPEC.format, start)) {
    batch.push(item);
    if (batch.length >= bufferSize) {
      const bt = batch;
      batch = [];
      for (const [r, o] of bt) {
        for (const t of mach.push(r)) yield [t, null];
        yield [null, o];
      }
    }
  }
  for (const [r, o] of batch) {
    for (const t of mach.push(r)) yield [t, null];
    yield [null, o];
  }
  for (const t of mach.finish()) yield [t, null];
}

function* plainStream(path, bufferSize) {
  for (const [t] of events(path, null, bufferSize, new Machine(null))) if (t !== null) yield t;
}

function* replay(rowsPath) {
  for (const [b] of readLines(rowsPath, 0)) {
    const t = b.toString("utf8");
    if (t) yield JSON.parse(t);
  }
}

function countComplete(rowsPath) {
  let n = 0, good = 0;
  if (!fs.existsSync(rowsPath)) return 0;
  const fd = fs.openSync(rowsPath, "r");
  try {
    const buf = Buffer.allocUnsafe(CHUNK);
    let pos = 0;
    while (true) {
      const r = fs.readSync(fd, buf, 0, CHUNK, pos);
      if (r <= 0) break;
      for (let i = 0; i < r; i++) if (buf[i] === 0x0a) { n++; good = pos + i + 1; }
      pos += r;
    }
  } finally {
    fs.closeSync(fd);
  }
  fs.truncateSync(rowsPath, good);
  return n;
}

function* resume(path, rowsPath, statePath, state, bufferSize) {
  const n = countComplete(rowsPath);
  let m = Number(state.out || 0);
  let offset = state.offset === undefined ? null : state.offset;
  let snap = state.snap && typeof state.snap === "object" ? state.snap : null;
  if (m > n || (offset !== null && snap === null)) { m = 0; offset = null; snap = null; }
  const mach = new Machine(offset !== null ? snap : null);
  let counter = m;
  const fd = fs.openSync(rowsPath, "a");
  try {
    let steps = 0;
    for (const [t, off] of events(path, offset, bufferSize, mach)) {
      if (t === null) {
        steps++;
        if (steps >= bufferSize) {
          steps = 0;
          state = Object.assign({}, state, { out: counter, offset: off, snap: mach.s });
          writeJsonAtomic(statePath, state);
        }
        continue;
      }
      counter++;
      if (counter <= n) continue;
      fs.writeSync(fd, JSON.stringify(t) + "\n");
      yield t;
    }
    state = Object.assign({}, state, { out: counter, offset: null, snap: null, complete: true });
    writeJsonAtomic(statePath, state);
  } finally {
    fs.closeSync(fd);
  }
}

function cachedStream(path, cacheDir, bufferSize) {
  const ap = nodePath.resolve(path);
  const key = crypto.createHash("sha256").update(ap + "\0" + SPEC_HASH, "utf8").digest("hex").slice(0, 40);
  const d = nodePath.join(cacheDir, key);
  const rowsPath = nodePath.join(d, "rows.jsonl");
  const statePath = nodePath.join(d, "state.json");
  fs.mkdirSync(d, { recursive: true });
  let fp = null;
  try {
    const st = fs.statSync(path);
    fp = [st.size, st.mtimeMs];
  } catch (e) {
    fp = null;
  }
  let state = null;
  try {
    state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  } catch (e) {
    state = null;
  }
  if (!state || typeof state !== "object" || JSON.stringify(state.fingerprint) !== JSON.stringify(fp) || state.path !== ap) {
    state = { path: ap, fingerprint: fp, complete: false, out: 0, offset: null };
    fs.writeFileSync(rowsPath, "");
    writeJsonAtomic(statePath, state);
  }
  if (state.complete) return replay(rowsPath);
  return resume(path, rowsPath, statePath, state, bufferSize);
}

function makeIterable(genFactory) {
  // Lazily start work on first iteration, like a Python generator.
  let it = null;
  const start = () => { if (it === null) it = genFactory(); return it; };
  const obj = {
    next(v) { return start().next(v); },
    return(v) { if (it === null) { it = (function* () {})(); } return it.return(v); },
    throw(e) { return start().throw(e); },
    [Symbol.iterator]() { return this; },
    toArray() { return Array.from(this); },
  };
  return obj;
}

function DynamicPreprocessor(opts, maybeCacheDir) {
  let bufferSize = 1024;
  let cacheDir = null;
  if (typeof opts === "number" || typeof opts === "string") {
    bufferSize = Number(opts);
    if (maybeCacheDir !== undefined && maybeCacheDir !== null) cacheDir = String(maybeCacheDir);
  } else if (opts && typeof opts === "object") {
    if (opts.buffer !== undefined && opts.buffer !== null) bufferSize = Number(opts.buffer);
    const cd = opts.cache_dir !== undefined ? opts.cache_dir : opts.cacheDir;
    if (cd !== undefined && cd !== null) cacheDir = String(cd);
  }
  if (!Number.isFinite(bufferSize) || bufferSize < 1) bufferSize = 1024;
  bufferSize = Math.floor(bufferSize);

  const pre = function (path) {
    path = String(path);
    if (cacheDir === null) return makeIterable(() => plainStream(path, bufferSize));
    return makeIterable(() => cachedStream(path, cacheDir, bufferSize));
  };
  pre.buffer = bufferSize;
  pre.cache_dir = cacheDir;
  pre.process = pre;
  pre.iterate = pre;
  return pre;
}

module.exports = { DynamicPreprocessor, transform, SPEC };
module.exports.default = DynamicPreprocessor;
'''


RUNTIME_CPP_H = r'''// Generated by dynamic_buffer.py -- do not edit.
#pragma once
#include <cstddef>
#include <map>
#include <memory>
#include <optional>
#include <string>

namespace __MODULE__ {

enum class ValueType {
    Null,
    Bool,
    Int,
    Double,
    String
};

struct Value {
    ValueType type;
    bool bool_value;
    long long int_value;
    double double_value;
    std::string string_value;
};

using Row = std::map<std::string, Value>;

class DynamicPreprocessor {
public:
    // No caching
    explicit DynamicPreprocessor(std::size_t buffer);

    // With caching
    DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir);

    ~DynamicPreprocessor();
    DynamicPreprocessor(DynamicPreprocessor&& other) noexcept;
    DynamicPreprocessor& operator=(DynamicPreprocessor&& other) noexcept;
    DynamicPreprocessor(const DynamicPreprocessor&) = delete;
    DynamicPreprocessor& operator=(const DynamicPreprocessor&) = delete;

    // Open a data file (same format/schema as the sample input)
    // Must reset internal stream/state to the start of this file.
    void open(const std::string& path);

    // Get next transformed row.
    // Returns true and writes into 'out' if a row is available.
    // Returns false when the stream is exhausted.
    bool next(Row& out);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace __MODULE__
'''

RUNTIME_CPP = r'''// Generated by dynamic_buffer.py -- do not edit.
#include "dynamic_preprocessor.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <utility>
#include <vector>

#include <locale.h>
#include <sys/stat.h>
#include <wctype.h>

namespace __MODULE__ {
namespace {

static const char* const SPEC_JSON = __SPEC_LITERAL__;
static const char* const SPEC_HASH = "__SPEC_HASH__";

// ---------------------------------------------------------------- dynamic values
struct J {
    enum T : unsigned char { NUL, BOOL, INT, DBL, STR, ARR, OBJ };
    T t = NUL;
    bool b = false;
    long long i = 0;
    double d = 0.0;
    std::string s;
    std::vector<J> a;            // array elements, or object values
    std::vector<std::string> k;  // object keys (parallel to a)

    static J boolean(bool v) { J j; j.t = BOOL; j.b = v; return j; }
    static J integer(long long v) { J j; j.t = INT; j.i = v; return j; }
    static J dbl(double v) { J j; j.t = DBL; j.d = v; return j; }
    static J str(std::string v) { J j; j.t = STR; j.s = std::move(v); return j; }
    static J arr() { J j; j.t = ARR; return j; }
    static J obj() { J j; j.t = OBJ; return j; }

    bool is_null() const { return t == NUL; }
    const J& get(const std::string& key) const;
    J* find(const std::string& key) {
        if (t != OBJ) return nullptr;
        for (std::size_t x = 0; x < k.size(); ++x)
            if (k[x] == key) return &a[x];
        return nullptr;
    }
    void set(const std::string& key, J v) {
        J* p = find(key);
        if (p) { *p = std::move(v); return; }
        k.push_back(key);
        a.push_back(std::move(v));
    }
    J& at(const std::string& key) {
        J* p = find(key);
        if (p) return *p;
        k.push_back(key);
        a.emplace_back();
        return a.back();
    }
};

const J& nullj() {
    static const J n;
    return n;
}

const J& J::get(const std::string& key) const {
    if (t != OBJ) return nullj();
    for (std::size_t x = 0; x < k.size(); ++x)
        if (k[x] == key) return a[x];
    return nullj();
}

bool deep_eq(const J& x, const J& y);

// ---------------------------------------------------------------- number formatting
std::string shortest_digits(double v, int& exp10) {
    // returns the shortest round-tripping decimal digits of |v| (v finite, != 0)
    char buf[64];
    for (int p = 1; p <= 17; ++p) {
        std::snprintf(buf, sizeof buf, "%.*e", p - 1, v);
        if (std::strtod(buf, nullptr) == v) break;
    }
    std::string s(buf);
    std::size_t e = s.find('e');
    exp10 = std::atoi(s.c_str() + e + 1);
    std::string digits;
    for (std::size_t x = 0; x < e; ++x)
        if (s[x] >= '0' && s[x] <= '9') digits += s[x];
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    return digits;
}

std::string fmt_repr(double v) {
    if (std::isnan(v)) return "nan";
    if (std::isinf(v)) return v > 0 ? "inf" : "-inf";
    if (v == 0.0) return std::signbit(v) ? "-0.0" : "0.0";
    int e = 0;
    std::string dg = shortest_digits(v, e);
    std::string out = v < 0 ? "-" : "";
    if (e >= -4 && e < 16) {
        if (e >= 0) {
            std::string ip = dg.substr(0, std::min<std::size_t>(dg.size(), (std::size_t)e + 1));
            while ((int)ip.size() < e + 1) ip += '0';
            std::string fp = (int)dg.size() > e + 1 ? dg.substr(e + 1) : "0";
            out += ip + "." + fp;
        } else {
            out += "0." + std::string((std::size_t)(-e - 1), '0') + dg;
        }
    } else {
        out += dg.substr(0, 1);
        if (dg.size() > 1) out += "." + dg.substr(1);
        char eb[16];
        std::snprintf(eb, sizeof eb, "e%c%02d", e < 0 ? '-' : '+', e < 0 ? -e : e);
        out += eb;
    }
    return out;
}

std::string fmt_g(double v) {
    if (std::isfinite(v) && std::floor(v) == v && std::fabs(v) < 1e16) {
        char b[32];
        std::snprintf(b, sizeof b, "%lld", (long long)v);
        return b;
    }
    return fmt_repr(v);
}

std::string fmt_fixed(double v, int d) {
    if (std::isnan(v)) return "nan";
    if (std::isinf(v)) return v > 0 ? "inf" : "-inf";
    if (d < 0) d = 0;
    int n = std::snprintf(nullptr, 0, "%.*f", d, v);
    std::string s((std::size_t)n + 1, '\0');
    std::snprintf(&s[0], s.size(), "%.*f", d, v);
    s.resize((std::size_t)n);
    return s;
}

double py_round(double y, int r) {
    if (!std::isfinite(y)) return y;
    std::string s = fmt_fixed(y, r);
    return std::strtod(s.c_str(), nullptr);
}

bool is_integral(double v) { return std::isfinite(v) && std::floor(v) == v; }

bool fits_ll(double v) { return v >= -9223372036854775808.0 && v < 9223372036854775808.0; }

// ---------------------------------------------------------------- JSON
void dump_str(const std::string& s, std::string& out) {
    out += '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    std::snprintf(b, sizeof b, "\\u%04x", c);
                    out += b;
                } else {
                    out += (char)c;
                }
        }
    }
    out += '"';
}

void dump(const J& j, std::string& out) {
    switch (j.t) {
        case J::NUL: out += "null"; break;
        case J::BOOL: out += j.b ? "true" : "false"; break;
        case J::INT: out += std::to_string(j.i); break;
        case J::DBL:
            if (std::isnan(j.d)) out += "NaN";
            else if (std::isinf(j.d)) out += j.d > 0 ? "Infinity" : "-Infinity";
            else out += fmt_repr(j.d);
            break;
        case J::STR: dump_str(j.s, out); break;
        case J::ARR:
            out += '[';
            for (std::size_t x = 0; x < j.a.size(); ++x) {
                if (x) out += ',';
                dump(j.a[x], out);
            }
            out += ']';
            break;
        case J::OBJ:
            out += '{';
            for (std::size_t x = 0; x < j.a.size(); ++x) {
                if (x) out += ',';
                dump_str(j.k[x], out);
                out += ':';
                dump(j.a[x], out);
            }
            out += '}';
            break;
    }
}

std::string dumps(const J& j) {
    std::string s;
    dump(j, s);
    return s;
}

void put_utf8(std::string& out, uint32_t c) {
    if (c < 0x80) {
        out += (char)c;
    } else if (c < 0x800) {
        out += (char)(0xC0 | (c >> 6));
        out += (char)(0x80 | (c & 0x3F));
    } else if (c < 0x10000) {
        out += (char)(0xE0 | (c >> 12));
        out += (char)(0x80 | ((c >> 6) & 0x3F));
        out += (char)(0x80 | (c & 0x3F));
    } else {
        out += (char)(0xF0 | (c >> 18));
        out += (char)(0x80 | ((c >> 12) & 0x3F));
        out += (char)(0x80 | ((c >> 6) & 0x3F));
        out += (char)(0x80 | (c & 0x3F));
    }
}

struct Parser {
    const std::string& src;
    std::size_t p = 0;
    explicit Parser(const std::string& s) : src(s) {}

    [[noreturn]] void fail() { throw std::runtime_error("invalid JSON"); }
    void ws() {
        while (p < src.size() && (src[p] == ' ' || src[p] == '\t' || src[p] == '\n' || src[p] == '\r')) ++p;
    }
    bool lit(const char* w) {
        std::size_t n = std::strlen(w);
        if (src.compare(p, n, w) == 0) { p += n; return true; }
        return false;
    }
    uint32_t hex4() {
        if (p + 4 > src.size()) fail();
        uint32_t v = 0;
        for (int x = 0; x < 4; ++x) {
            char c = src[p++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= (uint32_t)(c - '0');
            else if (c >= 'a' && c <= 'f') v |= (uint32_t)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= (uint32_t)(c - 'A' + 10);
            else fail();
        }
        return v;
    }
    std::string string() {
        if (src[p] != '"') fail();
        ++p;
        std::string out;
        while (true) {
            if (p >= src.size()) fail();
            char c = src[p++];
            if (c == '"') break;
            if (c != '\\') { out += c; continue; }
            if (p >= src.size()) fail();
            char e = src[p++];
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
                    uint32_t u = hex4();
                    if (u >= 0xD800 && u < 0xDC00 && p + 1 < src.size() && src[p] == '\\' && src[p + 1] == 'u') {
                        std::size_t save = p;
                        p += 2;
                        uint32_t lo = hex4();
                        if (lo >= 0xDC00 && lo < 0xE000) u = 0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00);
                        else p = save;
                    }
                    put_utf8(out, u);
                    break;
                }
                default: fail();
            }
        }
        return out;
    }
    J number() {
        std::size_t st = p;
        bool flt = false;
        if (p < src.size() && src[p] == '-') ++p;
        if (lit("Infinity")) return J::dbl(src[st] == '-' ? -INFINITY : INFINITY);
        while (p < src.size()) {
            char c = src[p];
            if (c >= '0' && c <= '9') { ++p; continue; }
            if (c == '.' || c == 'e' || c == 'E' || c == '+' || (c == '-' && p > st)) { flt = true; ++p; continue; }
            break;
        }
        std::string t = src.substr(st, p - st);
        if (t.empty() || t == "-") fail();
        if (!flt) {
            errno = 0;
            char* end = nullptr;
            long long v = std::strtoll(t.c_str(), &end, 10);
            if (errno == 0 && end && *end == '\0') return J::integer(v);
        }
        char* end = nullptr;
        double d = std::strtod(t.c_str(), &end);
        if (!end || *end != '\0') fail();
        return J::dbl(d);
    }
    J value() {
        ws();
        if (p >= src.size()) fail();
        char c = src[p];
        if (c == '{') {
            ++p;
            J o = J::obj();
            ws();
            if (p < src.size() && src[p] == '}') { ++p; return o; }
            while (true) {
                ws();
                if (p >= src.size() || src[p] != '"') fail();
                std::string key = string();
                ws();
                if (p >= src.size() || src[p] != ':') fail();
                ++p;
                J v = value();
                o.set(key, std::move(v));
                ws();
                if (p < src.size() && src[p] == ',') { ++p; continue; }
                if (p < src.size() && src[p] == '}') { ++p; return o; }
                fail();
            }
        }
        if (c == '[') {
            ++p;
            J arr = J::arr();
            ws();
            if (p < src.size() && src[p] == ']') { ++p; return arr; }
            while (true) {
                arr.a.push_back(value());
                ws();
                if (p < src.size() && src[p] == ',') { ++p; continue; }
                if (p < src.size() && src[p] == ']') { ++p; return arr; }
                fail();
            }
        }
        if (c == '"') return J::str(string());
        if (lit("true")) return J::boolean(true);
        if (lit("false")) return J::boolean(false);
        if (lit("null")) return J();
        if (lit("NaN")) return J::dbl(NAN);
        return number();
    }
};

J parse_json(const std::string& s) {
    Parser ps(s);
    J v = ps.value();
    ps.ws();
    if (ps.p != s.size()) ps.fail();
    return v;
}

// ---------------------------------------------------------------- unicode helpers
std::u32string decode(const std::string& s) {
    std::u32string out;
    out.reserve(s.size());
    std::size_t x = 0, n = s.size();
    while (x < n) {
        unsigned char c = (unsigned char)s[x];
        uint32_t cp;
        int len;
        if (c < 0x80) { cp = c; len = 1; }
        else if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; len = 2; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; len = 3; }
        else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; len = 4; }
        else { out += (char32_t)(0xDC00 + c); ++x; continue; }
        if (x + (std::size_t)len > n) { out += (char32_t)(0xDC00 + c); ++x; continue; }
        bool ok = true;
        for (int y = 1; y < len; ++y) {
            unsigned char cc = (unsigned char)s[x + (std::size_t)y];
            if ((cc & 0xC0) != 0x80) { ok = false; break; }
            cp = (cp << 6) | (cc & 0x3F);
        }
        if (!ok) { out += (char32_t)(0xDC00 + c); ++x; continue; }
        out += (char32_t)cp;
        x += (std::size_t)len;
    }
    return out;
}

std::string encode(const std::u32string& s) {
    std::string out;
    out.reserve(s.size());
    for (char32_t c : s) {
        if (c >= 0xDC80 && c <= 0xDCFF) out += (char)(c - 0xDC00);
        else put_utf8(out, (uint32_t)c);
    }
    return out;
}

bool py_space(char32_t c) {
    return (c >= 9 && c <= 13) || (c >= 0x1C && c <= 0x20) || c == 0x85 || c == 0xA0 || c == 0x1680 ||
           (c >= 0x2000 && c <= 0x200A) || c == 0x2028 || c == 0x2029 || c == 0x202F || c == 0x205F ||
           c == 0x3000;
}

locale_t utf8_locale() {
    static locale_t loc = []() {
        locale_t l = newlocale(LC_CTYPE_MASK, "C.UTF-8", (locale_t)0);
        if (!l) l = newlocale(LC_CTYPE_MASK, "C.utf8", (locale_t)0);
        if (!l) l = newlocale(LC_CTYPE_MASK, "en_US.UTF-8", (locale_t)0);
        return l;
    }();
    return loc;
}

char32_t simple_lower(char32_t c) {
    if (c < 0x80) return (c >= 'A' && c <= 'Z') ? c + 32 : c;
    if (c >= 0xD800 && c < 0xE000) return c;
    locale_t l = utf8_locale();
    if (l) return (char32_t)towlower_l((wint_t)c, l);
    if ((c >= 0xC0 && c <= 0xDE && c != 0xD7)) return c + 32;
    return c;
}

char32_t simple_upper(char32_t c) {
    if (c < 0x80) return (c >= 'a' && c <= 'z') ? c - 32 : c;
    if (c >= 0xD800 && c < 0xE000) return c;
    locale_t l = utf8_locale();
    if (l) return (char32_t)towupper_l((wint_t)c, l);
    if ((c >= 0xE0 && c <= 0xFE && c != 0xF7)) return c - 32;
    if (c == 0xFF) return 0x178;
    return c;
}

void lower_char(char32_t c, std::u32string& out) {
    if (c == 0x130) { out += U'i'; out += (char32_t)0x307; return; }
    out += simple_lower(c);
}

void upper_char(char32_t c, std::u32string& out) {
    switch (c) {
        case 0xDF: out += U"SS"; return;
        case 0xFB00: out += U"FF"; return;
        case 0xFB01: out += U"FI"; return;
        case 0xFB02: out += U"FL"; return;
        case 0xFB03: out += U"FFI"; return;
        case 0xFB04: out += U"FFL"; return;
        case 0x149: out += (char32_t)0x2BC; out += U'N'; return;
        default: out += simple_upper(c);
    }
}

bool is_letter(char32_t c) {
    std::u32string lo, up;
    lower_char(c, lo);
    upper_char(c, up);
    return lo != up;
}

std::u32string lower_u(const std::u32string& s) {
    std::u32string out;
    for (std::size_t x = 0; x < s.size(); ++x) {
        char32_t c = s[x];
        if (c == 0x3A3) {
            bool before = x > 0 && is_letter(s[x - 1]);
            bool after = x + 1 < s.size() && is_letter(s[x + 1]);
            out += (before && !after) ? (char32_t)0x3C2 : (char32_t)0x3C3;
            continue;
        }
        lower_char(c, out);
    }
    return out;
}

std::u32string upper_u(const std::u32string& s) {
    std::u32string out;
    for (char32_t c : s) upper_char(c, out);
    return out;
}

std::string py_lower(const std::string& s) { return encode(lower_u(decode(s))); }
std::string py_upper(const std::string& s) { return encode(upper_u(decode(s))); }

std::string py_strip(const std::string& s) {
    std::u32string u = decode(s);
    std::size_t b = 0, e = u.size();
    while (b < e && py_space(u[b])) ++b;
    while (e > b && py_space(u[e - 1])) --e;
    return encode(u.substr(b, e - b));
}

std::string py_title(const std::string& s) {
    std::u32string u = decode(s), out;
    bool prev = false;
    for (char32_t c : u) {
        bool il = is_letter(c);
        if (il) {
            if (prev) lower_char(c, out);
            else upper_char(c, out);
        } else {
            out += c;
        }
        prev = il;
    }
    return encode(out);
}

std::string py_capitalize(const std::string& s) {
    std::u32string u = decode(s);
    if (u.empty()) return s;
    std::u32string out;
    upper_char(u[0], out);
    out += lower_u(u.substr(1));
    return encode(out);
}

std::vector<std::string> split_ws(const std::string& s) {
    std::u32string u = decode(s);
    std::vector<std::string> parts;
    std::size_t x = 0;
    while (x < u.size()) {
        while (x < u.size() && py_space(u[x])) ++x;
        if (x >= u.size()) break;
        std::size_t st = x;
        while (x < u.size() && !py_space(u[x])) ++x;
        parts.push_back(encode(u.substr(st, x - st)));
    }
    return parts;
}

std::vector<std::string> split_sep(const std::string& s, const std::string& sep) {
    std::vector<std::string> parts;
    if (sep.empty()) { parts.push_back(s); return parts; }
    std::size_t st = 0;
    while (true) {
        std::size_t f = s.find(sep, st);
        if (f == std::string::npos) { parts.push_back(s.substr(st)); break; }
        parts.push_back(s.substr(st, f - st));
        st = f + sep.size();
    }
    return parts;
}

// ---------------------------------------------------------------- values
bool is_num(const J& v) { return v.t == J::INT || v.t == J::DBL; }

double as_d(const J& v) {
    if (v.t == J::INT) return (double)v.i;
    if (v.t == J::DBL) return v.d;
    if (v.t == J::BOOL) return v.b ? 1.0 : 0.0;
    return 0.0;
}

// Python regex ^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$ ; returns trimmed core
bool match_num(const std::string& s, bool int_only, std::string& core) {
    std::u32string u = decode(s);
    std::size_t b = 0, e = u.size();
    while (b < e && py_space(u[b])) ++b;
    while (e > b && py_space(u[e - 1])) --e;
    std::size_t x = b;
    auto dig = [&](std::size_t y) { return y < e && u[y] >= '0' && u[y] <= '9'; };
    if (x < e && (u[x] == '+' || u[x] == '-')) ++x;
    std::size_t ds = x;
    while (dig(x)) ++x;
    bool intd = x > ds;
    if (int_only) {
        if (!intd || x != e) return false;
        core = encode(u.substr(b, e - b));
        return true;
    }
    if (intd) {
        if (x < e && u[x] == '.') { ++x; while (dig(x)) ++x; }
    } else {
        if (!(x < e && u[x] == '.')) return false;
        ++x;
        if (!dig(x)) return false;
        while (dig(x)) ++x;
    }
    if (x < e && (u[x] == 'e' || u[x] == 'E')) {
        std::size_t y = x + 1;
        if (y < e && (u[y] == '+' || u[y] == '-')) ++y;
        if (!dig(y)) return false;
        while (dig(y)) ++y;
        x = y;
    }
    if (x != e) return false;
    core = encode(u.substr(b, e - b));
    return true;
}

std::optional<double> to_num(const J& v) {
    if (v.t == J::INT) return (double)v.i;
    if (v.t == J::DBL) {
        if (std::isfinite(v.d)) return v.d;
        return std::nullopt;
    }
    if (v.t == J::STR) {
        std::string core;
        if (!match_num(v.s, false, core)) return std::nullopt;
        double f = std::strtod(core.c_str(), nullptr);
        if (std::isfinite(f)) return f;
    }
    return std::nullopt;
}

J num_of(const J& v) {
    if (v.t == J::INT) return v;
    if (v.t == J::DBL) return std::isfinite(v.d) ? v : J();
    if (v.t == J::STR) {
        std::string core;
        if (match_num(v.s, true, core)) {
            errno = 0;
            char* end = nullptr;
            long long x = std::strtoll(core.c_str(), &end, 10);
            if (errno == 0) return J::integer(x);
        }
        auto f = to_num(v);
        if (f) return J::dbl(*f);
    }
    return J();
}

std::string to_str_raw(const J& v);

std::string py_repr_value(const J& v) {
    switch (v.t) {
        case J::NUL: return "None";
        case J::BOOL: return v.b ? "True" : "False";
        case J::STR: {
            std::string o = "'";
            for (char c : v.s) {
                if (c == '\'') o += "\\'";
                else if (c == '\\') o += "\\\\";
                else o += c;
            }
            return o + "'";
        }
        case J::ARR: {
            std::string o = "[";
            for (std::size_t x = 0; x < v.a.size(); ++x) {
                if (x) o += ", ";
                o += py_repr_value(v.a[x]);
            }
            return o + "]";
        }
        case J::OBJ: {
            std::string o = "{";
            for (std::size_t x = 0; x < v.a.size(); ++x) {
                if (x) o += ", ";
                o += py_repr_value(J::str(v.k[x])) + ": " + py_repr_value(v.a[x]);
            }
            return o + "}";
        }
        case J::DBL: return fmt_repr(v.d);
        default: return to_str_raw(v);
    }
}

std::string to_str_raw(const J& v) {
    switch (v.t) {
        case J::NUL: return "None";
        case J::BOOL: return v.b ? "true" : "false";
        case J::STR: return v.s;
        case J::INT: return std::to_string(v.i);
        case J::DBL: return fmt_g(v.d);
        default: return py_repr_value(v);
    }
}

int cat(const J& v) {
    if (v.t == J::NUL) return 0;
    if (v.t == J::BOOL) return 1;
    if (is_num(v)) return 2;
    return 3;
}

bool deep_eq(const J& x, const J& y) {
    if (is_num(x) && is_num(y)) {
        if (x.t == J::INT && y.t == J::INT) return x.i == y.i;
        return as_d(x) == as_d(y);
    }
    if (x.t != y.t) {
        // Python: True == 1 inside containers
        if ((x.t == J::BOOL && is_num(y)) || (y.t == J::BOOL && is_num(x))) return as_d(x) == as_d(y);
        return false;
    }
    switch (x.t) {
        case J::NUL: return true;
        case J::BOOL: return x.b == y.b;
        case J::STR: return x.s == y.s;
        case J::ARR:
            if (x.a.size() != y.a.size()) return false;
            for (std::size_t z = 0; z < x.a.size(); ++z)
                if (!deep_eq(x.a[z], y.a[z])) return false;
            return true;
        case J::OBJ:
            if (x.a.size() != y.a.size()) return false;
            for (std::size_t z = 0; z < x.a.size(); ++z) {
                const J* o = const_cast<J&>(y).find(x.k[z]);
                if (!o || !deep_eq(x.a[z], *o)) return false;
            }
            return true;
        default: return false;
    }
}

bool same(const J& a, const J& b) {
    if (cat(a) != cat(b)) return false;
    return deep_eq(a, b);
}

bool truthy(const J& v) {
    switch (v.t) {
        case J::NUL: return false;
        case J::BOOL: return v.b;
        case J::INT: return v.i != 0;
        case J::DBL: return v.d != 0.0;
        case J::STR: return !v.s.empty();
        default: return !v.a.empty();
    }
}

J num_add(const J& a, const J& b) {
    if (a.t == J::INT && b.t == J::INT) {
        long long r;
        if (!__builtin_add_overflow(a.i, b.i, &r)) return J::integer(r);
        return J::dbl((double)a.i + (double)b.i);
    }
    return J::dbl(as_d(a) + as_d(b));
}

bool num_lt(const J& a, const J& b) {
    if (a.t == J::INT && b.t == J::INT) return a.i < b.i;
    return as_d(a) < as_d(b);
}

J num_div(const J& a, long long c) { return J::dbl(as_d(a) / (double)c); }

// ---------------------------------------------------------------- per-row transforms
std::string apply_steps(std::string s, const J& steps) {
    for (const J& st : steps.a) {
        if (st.a.empty()) continue;
        const std::string& op = st.a[0].s;
        if (op == "strip") s = py_strip(s);
        else if (op == "lower") s = py_lower(s);
        else if (op == "upper") s = py_upper(s);
        else if (op == "title") s = py_title(s);
        else if (op == "capitalize") s = py_capitalize(s);
        else if (op == "split") {
            const J& sep = st.a.size() > 1 ? st.a[1] : nullj();
            long long k = st.a.size() > 2 ? st.a[2].i : 0;
            std::vector<std::string> parts = sep.is_null() ? split_ws(s) : split_sep(s, sep.s);
            long long n = (long long)parts.size();
            if (-n <= k && k < n) s = parts[(std::size_t)(k < 0 ? n + k : k)];
            else s = "";
        }
    }
    return s;
}

J format_num(double y, const J& c) {
    const J& r = c.get("round");
    if (!r.is_null()) y = py_round(y, (int)as_d(r));
    const J& f = c.get("fmt");
    if (f.t == J::STR) {
        if (f.s == "int") {
            if (is_integral(y) && fits_ll(y)) return J::integer((long long)y);
            return J::dbl(y);
        }
        if (f.s == "float") return J::dbl(y);
        if (f.s == "g") return J::str(fmt_g(y));
        if (f.s == "repr") return J::str(fmt_repr(y));
        if (f.s == "fixed") {
            const J& dg = c.get("digits");
            return J::str(fmt_fixed(y, dg.is_null() ? 0 : (int)as_d(dg)));
        }
    }
    return J::dbl(y);
}

J apply_col(const J& c, const J& row) {
    const std::string& k = c.get("kind").s;
    if (k == "copy") return row.get(c.get("src").s);
    if (k == "const") return c.get("value");
    if (k == "str") {
        const J& v = row.get(c.get("src").s);
        if (v.is_null()) return J();
        return J::str(c.get("prefix").s + apply_steps(to_str_raw(v), c.get("steps")) + c.get("suffix").s);
    }
    if (k == "concat") {
        std::string out;
        for (const J& p : c.get("parts").a) {
            if (p.a.empty()) continue;
            if (p.a[0].s == "lit") {
                out += p.a.size() > 1 ? p.a[1].s : std::string();
            } else {
                const J& v = row.get(p.a.size() > 1 ? p.a[1].s : std::string());
                std::string s = v.is_null() ? std::string() : to_str_raw(v);
                out += apply_steps(s, p.a.size() > 2 ? p.a[2] : nullj());
            }
        }
        return J::str(apply_steps(out, c.get("post")));
    }
    if (k == "linear") {
        const J& v = row.get(c.get("src").s);
        auto x = to_num(v);
        if (!x) return v;
        double a = as_d(c.get("a")), b = as_d(c.get("b"));
        J y = format_num(a * *x + b, c);
        const J& f = c.get("fmt");
        if (f.t == J::STR && f.s == "pytype") {
            double yd = as_d(y);
            if (v.t == J::INT && is_integral(a) && is_integral(b) && is_integral(yd) && fits_ll(yd))
                return J::integer((long long)yd);
            return J::dbl(yd);
        }
        return y;
    }
    return J();
}

bool cmp_d(double x, const std::string& op, double val) {
    if (op == "==") return x == val;
    if (op == "!=") return x != val;
    if (op == "<") return x < val;
    if (op == "<=") return x <= val;
    if (op == ">") return x > val;
    if (op == ">=") return x >= val;
    return false;
}

bool eval_filter(const J& f, const J& row) {
    if (f.is_null()) return true;
    const std::string& op = f.get("op").s;
    if (op == "and") {
        for (const J& a : f.get("args").a)
            if (!eval_filter(a, row)) return false;
        return true;
    }
    if (op == "or") {
        for (const J& a : f.get("args").a)
            if (eval_filter(a, row)) return true;
        return false;
    }
    if (op == "true") return true;
    if (op == "false") return false;
    if (op == "cmp") {
        const J& v = row.get(f.get("col").s);
        const std::string& cmp = f.get("cmp").s;
        const J& val = f.get("value");
        if (truthy(f.get("numeric"))) {
            auto x = to_num(v);
            if (!x) return cmp == "!=";
            return cmp_d(*x, cmp, as_d(val));
        }
        if (cmp == "==") return same(v, val);
        if (cmp == "!=") return !same(v, val);
        return false;
    }
    return true;
}

// ---------------------------------------------------------------- stateful transforms
struct Spec {
    J spec;
    J filter;
    std::vector<const J*> cols;
    std::vector<const J*> virt;
    bool lookahead = false;
    std::vector<std::pair<std::string, const J*>> all, kept;
    std::string format;

    Spec() {
        spec = parse_json(SPEC_JSON);
        filter = spec.get("filter");
        format = spec.get("format").s;
        const J& cs = spec.get("columns");
        for (std::size_t x = 0; x < cs.a.size(); ++x) {
            const J* c = &cs.a[x];
            cols.push_back(c);
            if (c->get("kind").s == "stateful") {
                if (c->get("scope").t == J::STR && c->get("scope").s == "kept") kept.emplace_back(std::to_string(x), c);
                else all.emplace_back(std::to_string(x), c);
            }
        }
        for (const J& v : spec.get("virtual").a) {
            virt.push_back(&v);
            const std::string& k = v.get("kind").s;
            if (k == "next" || k == "samenext") lookahead = true;
        }
    }
};

const Spec& S() {
    static const Spec sp;
    return sp;
}

J augment(const J& row, const J& prev, const J& nxt) {
    const Spec& sp = S();
    if (sp.virt.empty()) return row;
    J r = row;
    for (const J* dp : sp.virt) {
        const J& d = *dp;
        const std::string& k = d.get("kind").s;
        const std::string& name = d.get("name").s;
        if (k == "prev") r.set(name, prev.is_null() ? J() : prev.get(d.get("col").s));
        else if (k == "next") r.set(name, nxt.is_null() ? J() : nxt.get(d.get("col").s));
        else if (k == "sameprev")
            r.set(name, J::boolean(!prev.is_null() && same(row.get(d.get("col").s), prev.get(d.get("col").s))));
        else if (k == "samenext")
            r.set(name, J::boolean(!nxt.is_null() && same(row.get(d.get("col").s), nxt.get(d.get("col").s))));
        else if (k == "eq") r.set(name, J::boolean(same(row.get(d.get("a").s), row.get(d.get("b").s))));
    }
    return r;
}

bool trig_fire(const J& tr, J& st, const J& row) {
    const std::string& kind = tr.get("kind").s;
    bool has = truthy(st.get("h"));
    J pv = st.get("p");
    bool fire = false;
    if (kind == "pred" || kind == "rise" || kind == "rise0" || kind == "flip") {
        bool b = eval_filter(tr.get("pred"), row);
        if (kind == "pred") fire = b;
        else if (kind == "rise") fire = has && b && !truthy(pv);
        else if (kind == "rise0") fire = b && !(has && truthy(pv));
        else fire = has && !same(J::boolean(b), pv);
        st.set("p", J::boolean(b));
    } else {
        const J& v = row.get(tr.get("col").s);
        if (kind == "change") fire = has && !same(v, pv);
        else if (kind == "trans") fire = has && same(pv, tr.get("from")) && same(v, tr.get("to"));
        else if (kind == "gap") {
            auto x = to_num(v), px = to_num(pv);
            fire = has && x && px && cmp_d(*x - *px, tr.get("cmp").s, as_d(tr.get("value")));
        }
        st.set("p", v);
    }
    st.set("h", J::boolean(true));
    return fire;
}

J feat_init(const J& f) {
    const std::string& t = f.get("type").s;
    J st = J::obj();
    if (t == "prefix") {
        st.set("s", J::integer(0));
        st.set("c", J::integer(0));
        st.set("m", J());
    } else if (t == "window") {
        st.set("b", J::arr());
    } else if (t == "mode") {
        st.set("l", f.get("init"));
    } else {
        const J* base = const_cast<J&>(f).find("base");
        st.set("k", base ? *base : J::integer(0));
        st.set("h", J::boolean(false));
        st.set("p", J());
    }
    return st;
}

J contrib(const J& f, const J& row) {
    const std::string& agg = f.get("agg").s;
    if (agg == "countp") return J::integer(eval_filter(f.get("pred"), row) ? 1 : 0);
    if (agg == "count") return J::integer(1);
    return num_of(row.get(f.get("src").s));
}

J window_agg(const std::string& agg, const J& b) {
    if (agg == "count" || agg == "countp") {
        J s = J::integer(0);
        for (const J& x : b.a) s = num_add(s, x);
        return s;
    }
    J s = J::integer(0), m;
    long long c = 0;
    for (const J& x : b.a) {
        if (x.is_null()) continue;
        ++c;
        if (agg == "min") {
            if (m.is_null() || num_lt(x, m)) m = x;
        } else if (agg == "max") {
            if (m.is_null() || num_lt(m, x)) m = x;
        } else {
            s = num_add(s, x);
        }
    }
    if (c == 0) return J();
    if (agg == "sum") return s;
    if (agg == "mean") return num_div(s, c);
    return m;
}

long long floor_mod(long long a, long long n) {
    long long r = a % n;
    if (r != 0 && ((r < 0) != (n < 0))) r += n;
    return r;
}

J feat_step(const J& f, J& st, const J& row) {
    const std::string& t = f.get("type").s;
    if (t == "prefix") {
        const std::string& agg = f.get("agg").s;
        J x = contrib(f, row);
        if (agg == "count" || agg == "countp") {
            J s = num_add(st.get("s"), x);
            st.set("s", s);
            return s;
        }
        if (!x.is_null()) {
            st.set("c", J::integer(st.get("c").i + 1));
            if (agg == "min") {
                if (st.get("m").is_null() || num_lt(x, st.get("m"))) st.set("m", x);
            } else if (agg == "max") {
                if (st.get("m").is_null() || num_lt(st.get("m"), x)) st.set("m", x);
            } else {
                st.set("s", num_add(st.get("s"), x));
            }
        }
        long long c = st.get("c").i;
        if (c == 0) return J();
        if (agg == "sum") return st.get("s");
        if (agg == "mean") return num_div(st.get("s"), c);
        return st.get("m");
    }
    if (t == "window") {
        J& b = st.at("b");
        if (b.t != J::ARR) b = J::arr();
        b.a.push_back(contrib(f, row));
        std::size_t w = (std::size_t)std::max<long long>(0, (long long)as_d(f.get("w")));
        if (b.a.size() > w) b.a.erase(b.a.begin(), b.a.begin() + (std::ptrdiff_t)(b.a.size() - w));
        if (truthy(f.get("strict")) && b.a.size() < w) return J();
        return window_agg(f.get("agg").s, b);
    }
    if (t == "counter" || t == "cycle") {
        if (trig_fire(f.get("trigger"), st, row)) st.set("k", num_add(st.get("k"), J::integer(1)));
        if (t == "counter") return st.get("k");
        const J& labels = f.get("labels");
        if (labels.a.empty()) return J();
        long long k = (long long)as_d(st.get("k"));
        return labels.a[(std::size_t)floor_mod(k, (long long)labels.a.size())];
    }
    if (t == "mode") {
        for (const J& rule : f.get("rules").a) {
            if (rule.a.size() < 2) continue;
            if (eval_filter(rule.a[0], row)) {
                st.set("l", rule.a[1]);
                break;
            }
        }
        return st.get("l");
    }
    return J();
}

J map_feat(const J& c, const J& v) {
    if (v.is_null()) return c.get("null");
    const J& inner = c.get("inner");
    if (inner.is_null()) return v;
    J r = J::obj();
    r.set("__f", v);
    return apply_col(inner, r);
}

struct Machine {
    J s;

    Machine() { reset(); }

    void reset() {
        const Spec& sp = S();
        s = J::obj();
        s.set("prev", J());
        s.set("pend", J());
        s.set("pprev", J());
        s.set("pvals", J());
        J fa = J::obj(), fk = J::obj();
        for (auto& kc : sp.all) fa.set(kc.first, feat_init(kc.second->get("feature")));
        for (auto& kc : sp.kept) fk.set(kc.first, feat_init(kc.second->get("feature")));
        s.set("fa", fa);
        s.set("fk", fk);
    }

    bool decide(const J& row, const J& prev, const J& nxt, const J& vals, J& out) {
        const Spec& sp = S();
        J r = augment(row, prev, nxt);
        if (!eval_filter(sp.filter, r)) return false;
        J kv = J::obj();
        J& fk = s.at("fk");
        for (auto& kc : sp.kept) kv.set(kc.first, feat_step(kc.second->get("feature"), fk.at(kc.first), row));
        out = J::obj();
        for (std::size_t x = 0; x < sp.cols.size(); ++x) {
            const J& c = *sp.cols[x];
            if (c.get("kind").s == "stateful") {
                std::string k = std::to_string(x);
                bool isk = c.get("scope").t == J::STR && c.get("scope").s == "kept";
                out.set(c.get("name").s, map_feat(c, isk ? kv.get(k) : vals.get(k)));
            } else {
                out.set(c.get("name").s, apply_col(c, row));
            }
        }
        return true;
    }

    void push(const J& row, std::vector<J>& out) {
        const Spec& sp = S();
        J vals = J::obj();
        J& fa = s.at("fa");
        for (auto& kc : sp.all) vals.set(kc.first, feat_step(kc.second->get("feature"), fa.at(kc.first), row));
        J t;
        if (sp.lookahead) {
            if (!s.get("pend").is_null()) {
                J pend = s.get("pend"), pprev = s.get("pprev"), pvals = s.get("pvals");
                if (decide(pend, pprev, row, pvals, t)) out.push_back(std::move(t));
            }
            s.set("pend", row);
            s.set("pprev", s.get("prev"));
            s.set("pvals", vals);
        } else {
            J prev = s.get("prev");
            if (decide(row, prev, J(), vals, t)) out.push_back(std::move(t));
        }
        s.set("prev", row);
    }

    void finish(std::vector<J>& out) {
        if (!s.get("pend").is_null()) {
            J t;
            J pend = s.get("pend"), pprev = s.get("pprev"), pvals = s.get("pvals");
            if (decide(pend, pprev, J(), pvals, t)) out.push_back(std::move(t));
            s.set("pend", J());
            s.set("pprev", J());
            s.set("pvals", J());
        }
    }
};

// ---------------------------------------------------------------- readers
struct LineReader {
    std::ifstream in;
    long long pos = 0;
    bool ok = false;
    LineReader(const std::string& path, long long start) : in(path, std::ios::binary) {
        ok = (bool)in;
        if (ok && start > 0) {
            in.seekg(start);
            pos = start;
        }
    }
    // reads one raw line without the trailing \n; returns false at EOF
    bool line(std::string& out) {
        if (!ok) return false;
        if (!std::getline(in, out)) return false;
        pos += (long long)out.size();
        if (!in.eof()) pos += 1;
        return true;
    }
};

std::string strip_cr(std::string s) {
    if (!s.empty() && s.back() == '\r') s.pop_back();
    return s;
}

bool starts_bom(const std::string& s) { return s.size() >= 3 && s.compare(0, 3, "\xEF\xBB\xBF") == 0; }

std::vector<std::string> parse_delimited(const std::string& line, char delim) {
    std::vector<std::string> fields;
    if (line.empty()) return fields;
    std::string field;
    int state = 0;  // 0 start, 1 unquoted, 2 quoted, 3 quote-in-quoted
    for (char c : line) {
        if (state == 0) {
            if (c == '"') state = 2;
            else if (c == delim) fields.push_back("");
            else { field += c; state = 1; }
        } else if (state == 1) {
            if (c == delim) { fields.push_back(field); field.clear(); state = 0; }
            else field += c;
        } else if (state == 2) {
            if (c == '"') state = 3;
            else field += c;
        } else {
            if (c == '"') { field += '"'; state = 2; }
            else if (c == delim) { fields.push_back(field); field.clear(); state = 0; }
            else { field += c; state = 1; }
        }
    }
    fields.push_back(field);
    return fields;
}

struct Reader {
    virtual ~Reader() {}
    virtual bool next(J& row, long long& off) = 0;
};

struct DelimReader : Reader {
    LineReader lr;
    char delim;
    std::vector<std::string> header;
    bool have_header = false;
    DelimReader(const std::string& path, long long start, char d) : lr(path, 0), delim(d) {
        std::string ln;
        while (lr.line(ln)) {
            std::string t = strip_cr(ln);
            if (starts_bom(t)) t = t.substr(3);
            if (t.empty()) continue;
            header = parse_delimited(t, delim);
            have_header = true;
            break;
        }
        if (have_header && start > lr.pos) {
            lr.in.clear();
            lr.in.seekg(start);
            lr.pos = start;
        }
    }
    bool next(J& row, long long& off) override {
        if (!have_header) return false;
        std::string ln;
        while (lr.line(ln)) {
            std::string t = strip_cr(ln);
            if (t.empty()) continue;
            std::vector<std::string> f = parse_delimited(t, delim);
            row = J::obj();
            for (std::size_t x = 0; x < header.size(); ++x) row.set(header[x], x < f.size() ? J::str(f[x]) : J());
            off = lr.pos;
            return true;
        }
        return false;
    }
};

struct JsonlReader : Reader {
    LineReader lr;
    JsonlReader(const std::string& path, long long start) : lr(path, start) {}
    bool next(J& row, long long& off) override {
        std::string ln;
        while (lr.line(ln)) {
            std::string t = py_strip(ln);
            if (starts_bom(t)) t = py_strip(t.substr(3));
            if (t.empty()) continue;
            J obj;
            try {
                obj = parse_json(t);
            } catch (const std::exception&) {
                continue;
            }
            if (obj.t == J::OBJ) {
                row = std::move(obj);
                off = lr.pos;
                return true;
            }
        }
        return false;
    }
};

struct JsonArrayReader : Reader {
    std::ifstream in;
    long long pos = 0;  // file offset of data[0]
    std::vector<char> data;
    std::size_t n = 0, i = 0, seg = 0;
    int depth = 0;
    bool in_str = false, esc = false, have_cur = false;
    std::string cur;
    JsonArrayReader(const std::string& path, long long start) : in(path, std::ios::binary), data(65536) {
        if (in && start > 0) {
            in.seekg(start);
            pos = start;
        }
    }
    bool fill() {
        if (have_cur) cur.append(data.data() + seg, n - seg);
        pos += (long long)n;
        n = 0;
        i = 0;
        seg = 0;
        if (!in) return false;
        in.read(data.data(), (std::streamsize)data.size());
        n = (std::size_t)in.gcount();
        return n > 0;
    }
    bool next(J& row, long long& off) override {
        while (true) {
            if (i >= n) {
                if (!fill()) return false;
            }
            unsigned char b = (unsigned char)data[i];
            if (in_str) {
                if (esc) esc = false;
                else if (b == 0x5C) esc = true;
                else if (b == 0x22) in_str = false;
                ++i;
                continue;
            }
            if (b == 0x22) {
                if (depth > 0) in_str = true;
                ++i;
                continue;
            }
            if (b == 0x7B || b == 0x5B) {
                if (b == 0x7B && depth == 0) {
                    cur.clear();
                    have_cur = true;
                    seg = i;
                    depth = 1;
                } else if (depth > 0) {
                    ++depth;
                }
                ++i;
                continue;
            }
            if ((b == 0x7D || b == 0x5D) && depth > 0) {
                --depth;
                if (depth == 0) {
                    cur.append(data.data() + seg, i + 1 - seg);
                    have_cur = false;
                    off = pos + (long long)i + 1;
                    ++i;
                    J obj;
                    bool good = true;
                    try {
                        obj = parse_json(cur);
                    } catch (const std::exception&) {
                        good = false;
                    }
                    cur.clear();
                    if (good && obj.t == J::OBJ) {
                        row = std::move(obj);
                        return true;
                    }
                    continue;
                }
            }
            ++i;
        }
    }
};

std::unique_ptr<Reader> make_reader(const std::string& path, long long start) {
    const std::string& fmt = S().format;
    if (fmt == "csv") return std::unique_ptr<Reader>(new DelimReader(path, start, ','));
    if (fmt == "tsv") return std::unique_ptr<Reader>(new DelimReader(path, start, '\t'));
    if (fmt == "jsonl") return std::unique_ptr<Reader>(new JsonlReader(path, start));
    if (fmt == "json") return std::unique_ptr<Reader>(new JsonArrayReader(path, start));
    throw std::runtime_error("unsupported format: " + fmt);
}

// ---------------------------------------------------------------- output conversion
Value to_value(const J& j) {
    Value v{ValueType::Null, false, 0, 0.0, std::string()};
    switch (j.t) {
        case J::NUL: break;
        case J::BOOL: v.type = ValueType::Bool; v.bool_value = j.b; break;
        case J::INT: v.type = ValueType::Int; v.int_value = j.i; break;
        case J::DBL: v.type = ValueType::Double; v.double_value = j.d; break;
        case J::STR: v.type = ValueType::String; v.string_value = j.s; break;
        default: v.type = ValueType::String; v.string_value = dumps(j); break;
    }
    return v;
}

void to_row(const J& j, Row& out) {
    out.clear();
    for (std::size_t x = 0; x < j.a.size(); ++x) out[j.k[x]] = to_value(j.a[x]);
}

// ---------------------------------------------------------------- cache helpers
std::string hex64(uint64_t v) {
    char b[17];
    std::snprintf(b, sizeof b, "%016llx", (unsigned long long)v);
    return b;
}

uint64_t fnv1a(const std::string& s, uint64_t h) {
    for (unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    return h;
}

void write_file_atomic(const std::string& path, const std::string& content) {
    std::string tmp = path + ".tmp";
    {
        std::ofstream o(tmp, std::ios::binary | std::ios::trunc);
        o << content;
    }
    std::error_code ec;
    std::filesystem::rename(tmp, path, ec);
}

bool read_file(const std::string& path, std::string& out) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    out.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    return true;
}

}  // namespace

// ---------------------------------------------------------------- preprocessor
struct DynamicPreprocessor::Impl {
    struct Ev {
        bool is_row;
        J row;
        long long off;
    };
    enum Mode { NONE, PLAIN, REPLAY, RESUME, DONE };

    std::size_t buffer = 1;
    bool cached = false;
    std::string cache_dir;
    Mode mode = NONE;

    std::string path;
    std::unique_ptr<Reader> reader;
    Machine mach;
    std::deque<Ev> q;
    bool finished = false;

    // cache
    std::string rows_path, state_path;
    J state;
    std::ifstream replay;
    std::ofstream out;
    long long n_cached = 0, counter = 0, steps = 0;
    bool resume_started = false;

    void close() {
        if (out.is_open()) out.close();
        if (replay.is_open()) replay.close();
        reader.reset();
        q.clear();
        finished = false;
        resume_started = false;
        mode = NONE;
    }

    void start_stream(long long start, const J* snap) {
        mach.reset();
        if (snap && snap->t == J::OBJ) mach.s = *snap;
        reader = make_reader(path, start);
        q.clear();
        finished = false;
    }

    // Produce the next batch of events; false when the stream has ended.
    bool fill() {
        if (finished) return false;
        J row;
        long long off = 0;
        std::vector<J> outs;
        if (reader && reader->next(row, off)) {
            mach.push(row, outs);
            for (auto& t : outs) q.push_back(Ev{true, std::move(t), 0});
            q.push_back(Ev{false, J(), off});
            return true;
        }
        mach.finish(outs);
        for (auto& t : outs) q.push_back(Ev{true, std::move(t), 0});
        finished = true;
        return !q.empty();
    }

    void open(const std::string& p) {
        close();
        path = p;
        if (!cached) {
            start_stream(0, nullptr);
            mode = PLAIN;
            return;
        }
        namespace fs = std::filesystem;
        std::error_code ec;
        std::string ap = fs::absolute(fs::path(p), ec).lexically_normal().string();
        if (ec) ap = p;
        std::string keysrc = ap + std::string(1, '\0') + SPEC_HASH + std::string(1, '\0') + "cpp";
        std::string key = "cpp-" + hex64(fnv1a(keysrc, 1469598103934665603ULL)) +
                          hex64(fnv1a(keysrc, 0x9E3779B97F4A7C15ULL));
        fs::path d = fs::path(cache_dir) / key;
        fs::create_directories(d, ec);
        rows_path = (d / "rows.jsonl").string();
        state_path = (d / "state.json").string();

        J fp;
        struct stat stt;
        if (::stat(p.c_str(), &stt) == 0) {
            fp = J::arr();
            fp.a.push_back(J::integer((long long)stt.st_size));
            fp.a.push_back(J::integer((long long)stt.st_mtim.tv_sec * 1000000000LL + (long long)stt.st_mtim.tv_nsec));
        }
        J st;
        std::string txt;
        if (read_file(state_path, txt)) {
            try {
                st = parse_json(txt);
            } catch (const std::exception&) {
                st = J();
            }
        }
        if (st.t != J::OBJ || !deep_eq(st.get("fingerprint"), fp) || st.get("path").t != J::STR ||
            st.get("path").s != ap) {
            st = J::obj();
            st.set("path", J::str(ap));
            st.set("fingerprint", fp);
            st.set("complete", J::boolean(false));
            st.set("out", J::integer(0));
            st.set("offset", J());
            { std::ofstream trunc(rows_path, std::ios::binary | std::ios::trunc); }
            write_file_atomic(state_path, dumps(st));
        }
        state = st;
        if (truthy(state.get("complete"))) {
            replay.open(rows_path, std::ios::binary);
            mode = REPLAY;
        } else {
            mode = RESUME;
            resume_started = false;
        }
    }

    void begin_resume() {
        resume_started = true;
        // count fully written cached rows; drop a torn trailing line
        long long n = 0, good = 0;
        {
            std::ifstream in(rows_path, std::ios::binary);
            if (in) {
                std::vector<char> buf(65536);
                long long pos = 0;
                while (in) {
                    in.read(buf.data(), (std::streamsize)buf.size());
                    std::streamsize r = in.gcount();
                    if (r <= 0) break;
                    for (std::streamsize x = 0; x < r; ++x)
                        if (buf[(std::size_t)x] == '\n') { ++n; good = pos + x + 1; }
                    pos += r;
                }
            }
        }
        std::error_code ec;
        if (std::filesystem::exists(rows_path, ec)) std::filesystem::resize_file(rows_path, (std::uintmax_t)good, ec);
        n_cached = n;
        long long m = (long long)as_d(state.get("out"));
        J offset = state.get("offset");
        J snap = state.get("snap");
        if (m > n || (!offset.is_null() && snap.t != J::OBJ)) {
            m = 0;
            offset = J();
            snap = J();
        }
        if (!offset.is_null()) start_stream((long long)as_d(offset), &snap);
        else start_stream(0, nullptr);
        counter = m;
        steps = 0;
        out.open(rows_path, std::ios::binary | std::ios::app);
    }

    bool next_plain(J& t) {
        while (true) {
            while (!q.empty()) {
                Ev e = std::move(q.front());
                q.pop_front();
                if (e.is_row) {
                    t = std::move(e.row);
                    return true;
                }
            }
            if (!fill()) return false;
        }
    }

    bool next_resume(J& t) {
        if (!resume_started) begin_resume();
        while (true) {
            while (!q.empty()) {
                Ev e = std::move(q.front());
                q.pop_front();
                if (!e.is_row) {
                    ++steps;
                    if (steps >= (long long)buffer) {
                        steps = 0;
                        state.set("out", J::integer(counter));
                        state.set("offset", J::integer(e.off));
                        state.set("snap", mach.s);
                        write_file_atomic(state_path, dumps(state));
                    }
                    continue;
                }
                ++counter;
                if (counter <= n_cached) continue;
                std::string line = dumps(e.row);
                line += '\n';
                out.write(line.data(), (std::streamsize)line.size());
                out.flush();
                t = std::move(e.row);
                return true;
            }
            if (!fill()) {
                state.set("out", J::integer(counter));
                state.set("offset", J());
                state.set("snap", J());
                state.set("complete", J::boolean(true));
                write_file_atomic(state_path, dumps(state));
                if (out.is_open()) out.close();
                reader.reset();
                mode = DONE;
                return false;
            }
        }
    }

    bool next_replay(J& t) {
        std::string ln;
        while (std::getline(replay, ln)) {
            if (replay.eof()) break;  // torn trailing line without newline
            try {
                t = parse_json(ln);
                return true;
            } catch (const std::exception&) {
                continue;
            }
        }
        replay.close();
        mode = DONE;
        return false;
    }

    bool next(Row& r) {
        J t;
        bool ok = false;
        switch (mode) {
            case PLAIN: ok = next_plain(t); if (!ok) { reader.reset(); mode = DONE; } break;
            case RESUME: ok = next_resume(t); break;
            case REPLAY: ok = next_replay(t); break;
            default: ok = false;
        }
        if (ok) to_row(t, r);
        return ok;
    }
};

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer) : impl_(new Impl()) {
    impl_->buffer = buffer < 1 ? 1 : buffer;
}

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir) : impl_(new Impl()) {
    impl_->buffer = buffer < 1 ? 1 : buffer;
    impl_->cached = true;
    impl_->cache_dir = cache_dir;
}

DynamicPreprocessor::~DynamicPreprocessor() = default;
DynamicPreprocessor::DynamicPreprocessor(DynamicPreprocessor&& other) noexcept = default;
DynamicPreprocessor& DynamicPreprocessor::operator=(DynamicPreprocessor&& other) noexcept = default;

void DynamicPreprocessor::open(const std::string& path) { impl_->open(path); }

bool DynamicPreprocessor::next(Row& out) {
    if (!impl_) return false;
    return impl_->next(out);
}

}  // namespace __MODULE__
'''

RUNTIME_RS = r'''// Generated by dynamic_buffer.py -- do not edit.
#![allow(dead_code)]
#![allow(clippy::all)]

pub mod api {
    #[derive(Clone, Debug, PartialEq)]
    pub enum Value {
        Null,
        Bool(bool),
        Int(i64),
        Float(f64),
        Str(String),
    }

    pub type Row = std::collections::BTreeMap<String, Value>;

    pub struct DynamicPreprocessor {
        inner: crate::engine::Engine,
    }

    impl DynamicPreprocessor {
        pub fn new(buffer: usize, cache_dir: Option<&str>) -> DynamicPreprocessor {
            DynamicPreprocessor {
                inner: crate::engine::Engine::new(buffer, cache_dir),
            }
        }

        // Open a data file (same format/schema as the sample input)
        // Must reset internal stream/state to the start of this file.
        pub fn open<P: AsRef<std::path::Path>>(&mut self, path: P) -> std::io::Result<()> {
            self.inner.open(path.as_ref())
        }
    }

    impl Iterator for DynamicPreprocessor {
        type Item = Row;

        fn next(&mut self) -> Option<Row> {
            self.inner.next_row().map(|j| crate::engine::to_row(&j))
        }
    }
}

mod engine {
    use super::api::{Row, Value};
    use std::collections::VecDeque;
    use std::fs;
    use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
    use std::path::{Component, Path, PathBuf};
    use std::sync::OnceLock;

    const SPEC_JSON: &str = __SPEC_LITERAL__;
    const SPEC_HASH: &str = "__SPEC_HASH__";

    // ------------------------------------------------------------ dynamic values
    #[derive(Clone, Debug)]
    pub enum J {
        Nul,
        Bool(bool),
        Int(i64),
        Dbl(f64),
        Str(String),
        Arr(Vec<J>),
        Obj(Vec<(String, J)>),
    }

    static NULLJ: J = J::Nul;

    impl J {
        fn obj() -> J {
            J::Obj(Vec::new())
        }
        fn is_null(&self) -> bool {
            matches!(self, J::Nul)
        }
        fn get(&self, key: &str) -> &J {
            if let J::Obj(v) = self {
                for (k, x) in v.iter() {
                    if k == key {
                        return x;
                    }
                }
            }
            &NULLJ
        }
        fn has(&self, key: &str) -> bool {
            if let J::Obj(v) = self {
                return v.iter().any(|(k, _)| k == key);
            }
            false
        }
        fn set(&mut self, key: &str, val: J) {
            if !matches!(self, J::Obj(_)) {
                *self = J::obj();
            }
            if let J::Obj(v) = self {
                for (k, x) in v.iter_mut() {
                    if k == key {
                        *x = val;
                        return;
                    }
                }
                v.push((key.to_string(), val));
            }
        }
        fn at(&mut self, key: &str) -> &mut J {
            if !self.has(key) {
                self.set(key, J::Nul);
            }
            if let J::Obj(v) = self {
                for (k, x) in v.iter_mut() {
                    if k == key {
                        return x;
                    }
                }
            }
            unreachable!()
        }
        fn s(&self) -> &str {
            if let J::Str(s) = self {
                s
            } else {
                ""
            }
        }
        fn arr(&self) -> &[J] {
            if let J::Arr(a) = self {
                a
            } else {
                &[]
            }
        }
    }

    // ------------------------------------------------------------ number formatting
    fn shortest_digits(v: f64) -> (String, i32) {
        let s = format!("{:e}", v.abs());
        let (m, e) = s.split_once('e').unwrap();
        let exp: i32 = e.parse().unwrap();
        let mut digits: String = m.chars().filter(|c| c.is_ascii_digit()).collect();
        while digits.len() > 1 && digits.ends_with('0') {
            digits.pop();
        }
        (digits, exp)
    }

    pub fn fmt_repr(v: f64) -> String {
        if v.is_nan() {
            return "nan".into();
        }
        if v.is_infinite() {
            return if v > 0.0 { "inf".into() } else { "-inf".into() };
        }
        if v == 0.0 {
            return if v.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
        }
        let (dg, e) = shortest_digits(v);
        let mut out = String::new();
        if v < 0.0 {
            out.push('-');
        }
        if (-4..16).contains(&e) {
            if e >= 0 {
                let e = e as usize;
                let mut ip: String = dg.chars().take(e + 1).collect();
                while ip.len() < e + 1 {
                    ip.push('0');
                }
                let fp: String = if dg.len() > e + 1 { dg[e + 1..].to_string() } else { "0".into() };
                out.push_str(&ip);
                out.push('.');
                out.push_str(&fp);
            } else {
                out.push_str("0.");
                for _ in 0..(-e - 1) {
                    out.push('0');
                }
                out.push_str(&dg);
            }
        } else {
            out.push_str(&dg[..1]);
            if dg.len() > 1 {
                out.push('.');
                out.push_str(&dg[1..]);
            }
            out.push_str(&format!("e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs()));
        }
        out
    }

    fn is_integral(v: f64) -> bool {
        v.is_finite() && v.floor() == v
    }

    fn fits_i64(v: f64) -> bool {
        v >= -9223372036854775808.0 && v < 9223372036854775808.0
    }

    pub fn fmt_g(v: f64) -> String {
        if is_integral(v) && v.abs() < 1e16 {
            return format!("{}", v as i64);
        }
        fmt_repr(v)
    }

    fn fmt_fixed(v: f64, d: i64) -> String {
        if v.is_nan() {
            return "nan".into();
        }
        if v.is_infinite() {
            return if v > 0.0 { "inf".into() } else { "-inf".into() };
        }
        let d = if d < 0 { 0 } else { d as usize };
        format!("{:.*}", d, v)
    }

    fn py_round(y: f64, r: i64) -> f64 {
        if !y.is_finite() {
            return y;
        }
        fmt_fixed(y, r).parse::<f64>().unwrap_or(y)
    }

    // ------------------------------------------------------------ JSON
    fn dump_str(s: &str, out: &mut String) {
        out.push('"');
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out.push('"');
    }

    fn dump(j: &J, out: &mut String) {
        match j {
            J::Nul => out.push_str("null"),
            J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            J::Int(i) => out.push_str(&i.to_string()),
            J::Dbl(d) => {
                if d.is_nan() {
                    out.push_str("NaN")
                } else if d.is_infinite() {
                    out.push_str(if *d > 0.0 { "Infinity" } else { "-Infinity" })
                } else {
                    out.push_str(&fmt_repr(*d))
                }
            }
            J::Str(s) => dump_str(s, out),
            J::Arr(a) => {
                out.push('[');
                for (i, x) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    dump(x, out);
                }
                out.push(']');
            }
            J::Obj(o) => {
                out.push('{');
                for (i, (k, x)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    dump_str(k, out);
                    out.push(':');
                    dump(x, out);
                }
                out.push('}');
            }
        }
    }

    pub fn dumps(j: &J) -> String {
        let mut s = String::new();
        dump(j, &mut s);
        s
    }

    struct Parser<'a> {
        src: &'a [u8],
        p: usize,
    }

    impl<'a> Parser<'a> {
        fn ws(&mut self) {
            while self.p < self.src.len() && matches!(self.src[self.p], b' ' | b'\t' | b'\n' | b'\r') {
                self.p += 1;
            }
        }
        fn lit(&mut self, w: &str) -> bool {
            let w = w.as_bytes();
            if self.src.len() >= self.p + w.len() && &self.src[self.p..self.p + w.len()] == w {
                self.p += w.len();
                true
            } else {
                false
            }
        }
        fn hex4(&mut self) -> Option<u32> {
            if self.p + 4 > self.src.len() {
                return None;
            }
            let s = std::str::from_utf8(&self.src[self.p..self.p + 4]).ok()?;
            let v = u32::from_str_radix(s, 16).ok()?;
            self.p += 4;
            Some(v)
        }
        fn string(&mut self) -> Option<String> {
            if self.src.get(self.p) != Some(&b'"') {
                return None;
            }
            self.p += 1;
            let mut out: Vec<u8> = Vec::new();
            loop {
                let c = *self.src.get(self.p)?;
                self.p += 1;
                if c == b'"' {
                    break;
                }
                if c != b'\\' {
                    out.push(c);
                    continue;
                }
                let e = *self.src.get(self.p)?;
                self.p += 1;
                match e {
                    b'"' => out.push(b'"'),
                    b'\\' => out.push(b'\\'),
                    b'/' => out.push(b'/'),
                    b'b' => out.push(8),
                    b'f' => out.push(12),
                    b'n' => out.push(b'\n'),
                    b'r' => out.push(b'\r'),
                    b't' => out.push(b'\t'),
                    b'u' => {
                        let mut u = self.hex4()?;
                        if (0xD800..0xDC00).contains(&u)
                            && self.src.get(self.p) == Some(&b'\\')
                            && self.src.get(self.p + 1) == Some(&b'u')
                        {
                            let save = self.p;
                            self.p += 2;
                            match self.hex4() {
                                Some(lo) if (0xDC00..0xE000).contains(&lo) => {
                                    u = 0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00);
                                }
                                _ => self.p = save,
                            }
                        }
                        let ch = char::from_u32(u).unwrap_or('\u{FFFD}');
                        let mut b = [0u8; 4];
                        out.extend_from_slice(ch.encode_utf8(&mut b).as_bytes());
                    }
                    _ => return None,
                }
            }
            Some(String::from_utf8_lossy(&out).into_owned())
        }
        fn number(&mut self) -> Option<J> {
            let st = self.p;
            let mut flt = false;
            if self.src.get(self.p) == Some(&b'-') {
                self.p += 1;
            }
            if self.lit("Infinity") {
                return Some(J::Dbl(if self.src[st] == b'-' { f64::NEG_INFINITY } else { f64::INFINITY }));
            }
            while self.p < self.src.len() {
                let c = self.src[self.p];
                if c.is_ascii_digit() {
                    self.p += 1;
                    continue;
                }
                if c == b'.' || c == b'e' || c == b'E' || c == b'+' || (c == b'-' && self.p > st) {
                    flt = true;
                    self.p += 1;
                    continue;
                }
                break;
            }
            let t = std::str::from_utf8(&self.src[st..self.p]).ok()?;
            if t.is_empty() || t == "-" {
                return None;
            }
            if !flt {
                if let Ok(v) = t.parse::<i64>() {
                    return Some(J::Int(v));
                }
            }
            t.parse::<f64>().ok().map(J::Dbl)
        }
        fn value(&mut self) -> Option<J> {
            self.ws();
            let c = *self.src.get(self.p)?;
            if c == b'{' {
                self.p += 1;
                let mut o = J::obj();
                self.ws();
                if self.src.get(self.p) == Some(&b'}') {
                    self.p += 1;
                    return Some(o);
                }
                loop {
                    self.ws();
                    let key = self.string()?;
                    self.ws();
                    if self.src.get(self.p) != Some(&b':') {
                        return None;
                    }
                    self.p += 1;
                    let v = self.value()?;
                    o.set(&key, v);
                    self.ws();
                    match self.src.get(self.p) {
                        Some(b',') => self.p += 1,
                        Some(b'}') => {
                            self.p += 1;
                            return Some(o);
                        }
                        _ => return None,
                    }
                }
            }
            if c == b'[' {
                self.p += 1;
                let mut a = Vec::new();
                self.ws();
                if self.src.get(self.p) == Some(&b']') {
                    self.p += 1;
                    return Some(J::Arr(a));
                }
                loop {
                    a.push(self.value()?);
                    self.ws();
                    match self.src.get(self.p) {
                        Some(b',') => self.p += 1,
                        Some(b']') => {
                            self.p += 1;
                            return Some(J::Arr(a));
                        }
                        _ => return None,
                    }
                }
            }
            if c == b'"' {
                return self.string().map(J::Str);
            }
            if self.lit("true") {
                return Some(J::Bool(true));
            }
            if self.lit("false") {
                return Some(J::Bool(false));
            }
            if self.lit("null") {
                return Some(J::Nul);
            }
            if self.lit("NaN") {
                return Some(J::Dbl(f64::NAN));
            }
            self.number()
        }
    }

    pub fn parse_json(s: &[u8]) -> Option<J> {
        let mut p = Parser { src: s, p: 0 };
        let v = p.value()?;
        p.ws();
        if p.p != s.len() {
            return None;
        }
        Some(v)
    }

    // ------------------------------------------------------------ string helpers
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

    fn py_strip(s: &str) -> String {
        s.trim_matches(py_space).to_string()
    }

    fn char_lower(c: char) -> String {
        c.to_lowercase().collect()
    }

    fn char_upper(c: char) -> String {
        c.to_uppercase().collect()
    }

    fn is_letter(c: char) -> bool {
        char_lower(c) != char_upper(c)
    }

    fn py_title(s: &str) -> String {
        let mut out = String::new();
        let mut prev = false;
        for c in s.chars() {
            let il = is_letter(c);
            if il {
                if prev {
                    out.push_str(&char_lower(c));
                } else {
                    out.push_str(&char_upper(c));
                }
            } else {
                out.push(c);
            }
            prev = il;
        }
        out
    }

    fn py_capitalize(s: &str) -> String {
        let mut it = s.chars();
        match it.next() {
            None => String::new(),
            Some(f) => {
                let rest: String = it.collect();
                let mut out = char_upper(f);
                out.push_str(&rest.to_lowercase());
                out
            }
        }
    }

    fn apply_steps(mut s: String, steps: &J) -> String {
        for st in steps.arr() {
            let a = st.arr();
            if a.is_empty() {
                continue;
            }
            match a[0].s() {
                "strip" => s = py_strip(&s),
                "lower" => s = s.to_lowercase(),
                "upper" => s = s.to_uppercase(),
                "title" => s = py_title(&s),
                "capitalize" => s = py_capitalize(&s),
                "split" => {
                    let sep = a.get(1).unwrap_or(&NULLJ);
                    let k = match a.get(2) {
                        Some(J::Int(k)) => *k,
                        Some(J::Dbl(d)) => *d as i64,
                        _ => 0,
                    };
                    let parts: Vec<String> = if sep.is_null() {
                        s.split(py_space).filter(|x| !x.is_empty()).map(|x| x.to_string()).collect()
                    } else if sep.s().is_empty() {
                        vec![s.clone()]
                    } else {
                        s.split(sep.s()).map(|x| x.to_string()).collect()
                    };
                    let n = parts.len() as i64;
                    s = if -n <= k && k < n {
                        parts[(if k < 0 { n + k } else { k }) as usize].clone()
                    } else {
                        String::new()
                    };
                }
                _ => {}
            }
        }
        s
    }

    // ------------------------------------------------------------ values
    fn is_num(v: &J) -> bool {
        matches!(v, J::Int(_) | J::Dbl(_))
    }

    fn as_d(v: &J) -> f64 {
        match v {
            J::Int(i) => *i as f64,
            J::Dbl(d) => *d,
            J::Bool(b) => {
                if *b {
                    1.0
                } else {
                    0.0
                }
            }
            _ => 0.0,
        }
    }

    fn as_i(v: &J) -> i64 {
        match v {
            J::Int(i) => *i,
            J::Dbl(d) => *d as i64,
            J::Bool(b) => *b as i64,
            _ => 0,
        }
    }

    // Python regex ^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$
    fn match_num(s: &str, int_only: bool) -> Option<String> {
        let t = s.trim_matches(py_space);
        let b = t.as_bytes();
        let n = b.len();
        let mut x = 0;
        if x < n && (b[x] == b'+' || b[x] == b'-') {
            x += 1;
        }
        let ds = x;
        while x < n && b[x].is_ascii_digit() {
            x += 1;
        }
        let intd = x > ds;
        if int_only {
            return if intd && x == n { Some(t.to_string()) } else { None };
        }
        if intd {
            if x < n && b[x] == b'.' {
                x += 1;
                while x < n && b[x].is_ascii_digit() {
                    x += 1;
                }
            }
        } else {
            if !(x < n && b[x] == b'.') {
                return None;
            }
            x += 1;
            if !(x < n && b[x].is_ascii_digit()) {
                return None;
            }
            while x < n && b[x].is_ascii_digit() {
                x += 1;
            }
        }
        if x < n && (b[x] == b'e' || b[x] == b'E') {
            let mut y = x + 1;
            if y < n && (b[y] == b'+' || b[y] == b'-') {
                y += 1;
            }
            if !(y < n && b[y].is_ascii_digit()) {
                return None;
            }
            while y < n && b[y].is_ascii_digit() {
                y += 1;
            }
            x = y;
        }
        if x != n {
            return None;
        }
        Some(t.to_string())
    }

    fn parse_float(core: &str) -> Option<f64> {
        let mut c = core.to_string();
        if c.ends_with('.') {
            c.push('0');
        }
        if c.starts_with('.') || c.starts_with("-.") || c.starts_with("+.") {
            c = c.replacen('.', "0.", 1);
        }
        let c = c.replace(".e", ".0e").replace(".E", ".0E");
        c.parse::<f64>().ok()
    }

    fn to_num(v: &J) -> Option<f64> {
        match v {
            J::Int(i) => Some(*i as f64),
            J::Dbl(d) => {
                if d.is_finite() {
                    Some(*d)
                } else {
                    None
                }
            }
            J::Str(s) => {
                let core = match_num(s, false)?;
                let f = parse_float(&core)?;
                if f.is_finite() {
                    Some(f)
                } else {
                    None
                }
            }
            _ => None,
        }
    }

    fn num_of(v: &J) -> J {
        match v {
            J::Int(_) => v.clone(),
            J::Dbl(d) => {
                if d.is_finite() {
                    v.clone()
                } else {
                    J::Nul
                }
            }
            J::Str(s) => {
                if let Some(core) = match_num(s, true) {
                    let c = core.strip_prefix('+').unwrap_or(&core);
                    if let Ok(x) = c.parse::<i64>() {
                        return J::Int(x);
                    }
                }
                match to_num(v) {
                    Some(f) => J::Dbl(f),
                    None => J::Nul,
                }
            }
            _ => J::Nul,
        }
    }

    fn py_repr_value(v: &J) -> String {
        match v {
            J::Nul => "None".into(),
            J::Bool(b) => (if *b { "True" } else { "False" }).into(),
            J::Str(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
            J::Arr(a) => format!("[{}]", a.iter().map(py_repr_value).collect::<Vec<_>>().join(", ")),
            J::Obj(o) => format!(
                "{{{}}}",
                o.iter()
                    .map(|(k, x)| format!("{}: {}", py_repr_value(&J::Str(k.clone())), py_repr_value(x)))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            J::Dbl(d) => fmt_repr(*d),
            J::Int(i) => i.to_string(),
        }
    }

    fn to_str_raw(v: &J) -> String {
        match v {
            J::Nul => "None".into(),
            J::Bool(b) => (if *b { "true" } else { "false" }).into(),
            J::Str(s) => s.clone(),
            J::Int(i) => i.to_string(),
            J::Dbl(d) => fmt_g(*d),
            _ => py_repr_value(v),
        }
    }

    fn cat(v: &J) -> u8 {
        match v {
            J::Nul => 0,
            J::Bool(_) => 1,
            J::Int(_) | J::Dbl(_) => 2,
            _ => 3,
        }
    }

    fn deep_eq(x: &J, y: &J) -> bool {
        match (x, y) {
            (J::Int(a), J::Int(b)) => a == b,
            (a, b) if is_num(a) && is_num(b) => as_d(a) == as_d(b),
            (J::Bool(_), b) if is_num(b) => as_d(x) == as_d(b),
            (a, J::Bool(_)) if is_num(a) => as_d(a) == as_d(y),
            (J::Nul, J::Nul) => true,
            (J::Bool(a), J::Bool(b)) => a == b,
            (J::Str(a), J::Str(b)) => a == b,
            (J::Arr(a), J::Arr(b)) => a.len() == b.len() && a.iter().zip(b.iter()).all(|(p, q)| deep_eq(p, q)),
            (J::Obj(a), J::Obj(_)) => {
                if let J::Obj(b) = y {
                    if a.len() != b.len() {
                        return false;
                    }
                }
                a.iter().all(|(k, v)| y.has(k) && deep_eq(v, y.get(k)))
            }
            _ => false,
        }
    }

    fn same(a: &J, b: &J) -> bool {
        cat(a) == cat(b) && deep_eq(a, b)
    }

    fn truthy(v: &J) -> bool {
        match v {
            J::Nul => false,
            J::Bool(b) => *b,
            J::Int(i) => *i != 0,
            J::Dbl(d) => *d != 0.0,
            J::Str(s) => !s.is_empty(),
            J::Arr(a) => !a.is_empty(),
            J::Obj(o) => !o.is_empty(),
        }
    }

    fn num_add(a: &J, b: &J) -> J {
        if let (J::Int(x), J::Int(y)) = (a, b) {
            if let Some(r) = x.checked_add(*y) {
                return J::Int(r);
            }
            return J::Dbl(*x as f64 + *y as f64);
        }
        J::Dbl(as_d(a) + as_d(b))
    }

    fn num_lt(a: &J, b: &J) -> bool {
        if let (J::Int(x), J::Int(y)) = (a, b) {
            return x < y;
        }
        as_d(a) < as_d(b)
    }

    // ------------------------------------------------------------ per-row transforms
    fn format_num(mut y: f64, c: &J) -> J {
        let r = c.get("round");
        if !r.is_null() {
            y = py_round(y, as_i(r));
        }
        match c.get("fmt").s() {
            "int" => {
                if is_integral(y) && fits_i64(y) {
                    J::Int(y as i64)
                } else {
                    J::Dbl(y)
                }
            }
            "float" => J::Dbl(y),
            "g" => J::Str(fmt_g(y)),
            "repr" => J::Str(fmt_repr(y)),
            "fixed" => J::Str(fmt_fixed(y, as_i(c.get("digits")))),
            _ => J::Dbl(y),
        }
    }

    fn apply_col(c: &J, row: &J) -> J {
        match c.get("kind").s() {
            "copy" => row.get(c.get("src").s()).clone(),
            "const" => c.get("value").clone(),
            "str" => {
                let v = row.get(c.get("src").s());
                if v.is_null() {
                    return J::Nul;
                }
                let mut s = c.get("prefix").s().to_string();
                s.push_str(&apply_steps(to_str_raw(v), c.get("steps")));
                s.push_str(c.get("suffix").s());
                J::Str(s)
            }
            "concat" => {
                let mut out = String::new();
                for p in c.get("parts").arr() {
                    let a = p.arr();
                    if a.is_empty() {
                        continue;
                    }
                    if a[0].s() == "lit" {
                        out.push_str(a.get(1).unwrap_or(&NULLJ).s());
                    } else {
                        let v = row.get(a.get(1).unwrap_or(&NULLJ).s());
                        let s = if v.is_null() { String::new() } else { to_str_raw(v) };
                        out.push_str(&apply_steps(s, a.get(2).unwrap_or(&NULLJ)));
                    }
                }
                J::Str(apply_steps(out, c.get("post")))
            }
            "linear" => {
                let v = row.get(c.get("src").s());
                let x = match to_num(v) {
                    Some(x) => x,
                    None => return v.clone(),
                };
                let a = as_d(c.get("a"));
                let b = as_d(c.get("b"));
                let y = format_num(a * x + b, c);
                if c.get("fmt").s() == "pytype" {
                    let yd = as_d(&y);
                    if matches!(v, J::Int(_)) && is_integral(a) && is_integral(b) && is_integral(yd) && fits_i64(yd) {
                        return J::Int(yd as i64);
                    }
                    return J::Dbl(yd);
                }
                y
            }
            _ => J::Nul,
        }
    }

    fn cmp_d(x: f64, op: &str, val: f64) -> bool {
        match op {
            "==" => x == val,
            "!=" => x != val,
            "<" => x < val,
            "<=" => x <= val,
            ">" => x > val,
            ">=" => x >= val,
            _ => false,
        }
    }

    fn eval_filter(f: &J, row: &J) -> bool {
        if f.is_null() {
            return true;
        }
        match f.get("op").s() {
            "and" => f.get("args").arr().iter().all(|a| eval_filter(a, row)),
            "or" => f.get("args").arr().iter().any(|a| eval_filter(a, row)),
            "true" => true,
            "false" => false,
            "cmp" => {
                let v = row.get(f.get("col").s());
                let cmp = f.get("cmp").s();
                let val = f.get("value");
                if truthy(f.get("numeric")) {
                    return match to_num(v) {
                        None => cmp == "!=",
                        Some(x) => cmp_d(x, cmp, as_d(val)),
                    };
                }
                match cmp {
                    "==" => same(v, val),
                    "!=" => !same(v, val),
                    _ => false,
                }
            }
            _ => true,
        }
    }

    // ------------------------------------------------------------ stateful transforms
    struct Spec {
        spec: J,
        lookahead: bool,
        all: Vec<(String, usize)>,
        kept: Vec<(String, usize)>,
        format: String,
    }

    impl Spec {
        fn cols(&self) -> &[J] {
            self.spec.get("columns").arr()
        }
        fn virt(&self) -> &[J] {
            self.spec.get("virtual").arr()
        }
        fn filter(&self) -> &J {
            self.spec.get("filter")
        }
    }

    fn spec() -> &'static Spec {
        static S: OnceLock<Spec> = OnceLock::new();
        S.get_or_init(|| {
            let spec = parse_json(SPEC_JSON.as_bytes()).expect("embedded spec");
            let mut all = Vec::new();
            let mut kept = Vec::new();
            for (i, c) in spec.get("columns").arr().iter().enumerate() {
                if c.get("kind").s() == "stateful" {
                    if c.get("scope").s() == "kept" {
                        kept.push((i.to_string(), i));
                    } else {
                        all.push((i.to_string(), i));
                    }
                }
            }
            let lookahead = spec
                .get("virtual")
                .arr()
                .iter()
                .any(|v| matches!(v.get("kind").s(), "next" | "samenext"));
            let format = spec.get("format").s().to_string();
            Spec { spec, lookahead, all, kept, format }
        })
    }

    fn augment(row: &J, prev: &J, nxt: &J) -> J {
        let sp = spec();
        let defs = sp.virt();
        if defs.is_empty() {
            return row.clone();
        }
        let mut r = row.clone();
        for d in defs {
            let name = d.get("name").s();
            let col = d.get("col").s();
            match d.get("kind").s() {
                "prev" => r.set(name, if prev.is_null() { J::Nul } else { prev.get(col).clone() }),
                "next" => r.set(name, if nxt.is_null() { J::Nul } else { nxt.get(col).clone() }),
                "sameprev" => r.set(name, J::Bool(!prev.is_null() && same(row.get(col), prev.get(col)))),
                "samenext" => r.set(name, J::Bool(!nxt.is_null() && same(row.get(col), nxt.get(col)))),
                "eq" => r.set(name, J::Bool(same(row.get(d.get("a").s()), row.get(d.get("b").s())))),
                _ => {}
            }
        }
        r
    }

    fn trig_fire(tr: &J, st: &mut J, row: &J) -> bool {
        let kind = tr.get("kind").s();
        let has = truthy(st.get("h"));
        let pv = st.get("p").clone();
        let fire;
        if matches!(kind, "pred" | "rise" | "rise0" | "flip") {
            let b = eval_filter(tr.get("pred"), row);
            fire = match kind {
                "pred" => b,
                "rise" => has && b && !truthy(&pv),
                "rise0" => b && !(has && truthy(&pv)),
                _ => has && !same(&J::Bool(b), &pv),
            };
            st.set("p", J::Bool(b));
        } else {
            let v = row.get(tr.get("col").s()).clone();
            fire = match kind {
                "change" => has && !same(&v, &pv),
                "trans" => has && same(&pv, tr.get("from")) && same(&v, tr.get("to")),
                "gap" => match (to_num(&v), to_num(&pv)) {
                    (Some(x), Some(px)) => has && cmp_d(x - px, tr.get("cmp").s(), as_d(tr.get("value"))),
                    _ => false,
                },
                _ => false,
            };
            st.set("p", v);
        }
        st.set("h", J::Bool(true));
        fire
    }

    fn feat_init(f: &J) -> J {
        let mut st = J::obj();
        match f.get("type").s() {
            "prefix" => {
                st.set("s", J::Int(0));
                st.set("c", J::Int(0));
                st.set("m", J::Nul);
            }
            "window" => st.set("b", J::Arr(Vec::new())),
            "mode" => st.set("l", f.get("init").clone()),
            _ => {
                st.set("k", if f.has("base") { f.get("base").clone() } else { J::Int(0) });
                st.set("h", J::Bool(false));
                st.set("p", J::Nul);
            }
        }
        st
    }

    fn contrib(f: &J, row: &J) -> J {
        match f.get("agg").s() {
            "countp" => J::Int(if eval_filter(f.get("pred"), row) { 1 } else { 0 }),
            "count" => J::Int(1),
            _ => num_of(row.get(f.get("src").s())),
        }
    }

    fn window_agg(agg: &str, b: &[J]) -> J {
        if agg == "count" || agg == "countp" {
            let mut s = J::Int(0);
            for x in b {
                s = num_add(&s, x);
            }
            return s;
        }
        let mut s = J::Int(0);
        let mut m = J::Nul;
        let mut c: i64 = 0;
        for x in b {
            if x.is_null() {
                continue;
            }
            c += 1;
            match agg {
                "min" => {
                    if m.is_null() || num_lt(x, &m) {
                        m = x.clone();
                    }
                }
                "max" => {
                    if m.is_null() || num_lt(&m, x) {
                        m = x.clone();
                    }
                }
                _ => s = num_add(&s, x),
            }
        }
        if c == 0 {
            return J::Nul;
        }
        match agg {
            "sum" => s,
            "mean" => J::Dbl(as_d(&s) / c as f64),
            _ => m,
        }
    }

    fn feat_step(f: &J, st: &mut J, row: &J) -> J {
        match f.get("type").s() {
            "prefix" => {
                let agg = f.get("agg").s();
                let x = contrib(f, row);
                if agg == "count" || agg == "countp" {
                    let s = num_add(st.get("s"), &x);
                    st.set("s", s.clone());
                    return s;
                }
                if !x.is_null() {
                    let c = as_i(st.get("c")) + 1;
                    st.set("c", J::Int(c));
                    match agg {
                        "min" => {
                            if st.get("m").is_null() || num_lt(&x, st.get("m")) {
                                st.set("m", x);
                            }
                        }
                        "max" => {
                            if st.get("m").is_null() || num_lt(st.get("m"), &x) {
                                st.set("m", x);
                            }
                        }
                        _ => {
                            let s = num_add(st.get("s"), &x);
                            st.set("s", s);
                        }
                    }
                }
                let c = as_i(st.get("c"));
                if c == 0 {
                    return J::Nul;
                }
                match agg {
                    "sum" => st.get("s").clone(),
                    "mean" => J::Dbl(as_d(st.get("s")) / c as f64),
                    _ => st.get("m").clone(),
                }
            }
            "window" => {
                let w = as_i(f.get("w")).max(0) as usize;
                let x = contrib(f, row);
                let b = st.at("b");
                if !matches!(b, J::Arr(_)) {
                    *b = J::Arr(Vec::new());
                }
                if let J::Arr(v) = b {
                    v.push(x);
                    if v.len() > w {
                        let extra = v.len() - w;
                        v.drain(0..extra);
                    }
                    if truthy(f.get("strict")) && v.len() < w {
                        return J::Nul;
                    }
                    return window_agg(f.get("agg").s(), v);
                }
                J::Nul
            }
            t @ ("counter" | "cycle") => {
                if trig_fire(f.get("trigger"), st, row) {
                    let k = num_add(st.get("k"), &J::Int(1));
                    st.set("k", k);
                }
                if t == "counter" {
                    return st.get("k").clone();
                }
                let labels = f.get("labels").arr();
                if labels.is_empty() {
                    return J::Nul;
                }
                let k = as_i(st.get("k"));
                labels[k.rem_euclid(labels.len() as i64) as usize].clone()
            }
            "mode" => {
                for rule in f.get("rules").arr() {
                    let r = rule.arr();
                    if r.len() < 2 {
                        continue;
                    }
                    if eval_filter(&r[0], row) {
                        st.set("l", r[1].clone());
                        break;
                    }
                }
                st.get("l").clone()
            }
            _ => J::Nul,
        }
    }

    fn map_feat(c: &J, v: &J) -> J {
        if v.is_null() {
            return c.get("null").clone();
        }
        let inner = c.get("inner");
        if inner.is_null() {
            return v.clone();
        }
        let mut r = J::obj();
        r.set("__f", v.clone());
        apply_col(inner, &r)
    }

    struct Machine {
        s: J,
    }

    impl Machine {
        fn new() -> Machine {
            let sp = spec();
            let cols = sp.cols();
            let mut s = J::obj();
            s.set("prev", J::Nul);
            s.set("pend", J::Nul);
            s.set("pprev", J::Nul);
            s.set("pvals", J::Nul);
            let mut fa = J::obj();
            let mut fk = J::obj();
            for (k, i) in &sp.all {
                fa.set(k, feat_init(cols[*i].get("feature")));
            }
            for (k, i) in &sp.kept {
                fk.set(k, feat_init(cols[*i].get("feature")));
            }
            s.set("fa", fa);
            s.set("fk", fk);
            Machine { s }
        }

        fn decide(&mut self, row: &J, prev: &J, nxt: &J, vals: &J) -> Option<J> {
            let sp = spec();
            let cols = sp.cols();
            let r = augment(row, prev, nxt);
            if !eval_filter(sp.filter(), &r) {
                return None;
            }
            let mut kv = J::obj();
            {
                let fk = self.s.at("fk");
                for (k, i) in &sp.kept {
                    let v = feat_step(cols[*i].get("feature"), fk.at(k), row);
                    kv.set(k, v);
                }
            }
            let mut out = J::obj();
            for (i, c) in cols.iter().enumerate() {
                if c.get("kind").s() == "stateful" {
                    let k = i.to_string();
                    let src = if c.get("scope").s() == "kept" { kv.get(&k) } else { vals.get(&k) };
                    out.set(c.get("name").s(), map_feat(c, src));
                } else {
                    out.set(c.get("name").s(), apply_col(c, row));
                }
            }
            Some(out)
        }

        fn push(&mut self, row: J, out: &mut Vec<J>) {
            let sp = spec();
            let cols = sp.cols();
            let mut vals = J::obj();
            {
                let fa = self.s.at("fa");
                for (k, i) in &sp.all {
                    let v = feat_step(cols[*i].get("feature"), fa.at(k), &row);
                    vals.set(k, v);
                }
            }
            if sp.lookahead {
                let pend = self.s.get("pend").clone();
                if !pend.is_null() {
                    let pprev = self.s.get("pprev").clone();
                    let pvals = self.s.get("pvals").clone();
                    if let Some(t) = self.decide(&pend, &pprev, &row, &pvals) {
                        out.push(t);
                    }
                }
                let prev = self.s.get("prev").clone();
                self.s.set("pend", row.clone());
                self.s.set("pprev", prev);
                self.s.set("pvals", vals);
            } else {
                let prev = self.s.get("prev").clone();
                if let Some(t) = self.decide(&row, &prev, &J::Nul, &vals) {
                    out.push(t);
                }
            }
            self.s.set("prev", row);
        }

        fn finish(&mut self, out: &mut Vec<J>) {
            let pend = self.s.get("pend").clone();
            if !pend.is_null() {
                let pprev = self.s.get("pprev").clone();
                let pvals = self.s.get("pvals").clone();
                if let Some(t) = self.decide(&pend, &pprev, &J::Nul, &pvals) {
                    out.push(t);
                }
                self.s.set("pend", J::Nul);
                self.s.set("pprev", J::Nul);
                self.s.set("pvals", J::Nul);
            }
        }
    }

    // ------------------------------------------------------------ readers
    struct LineReader {
        rd: Option<BufReader<fs::File>>,
        pos: u64,
    }

    impl LineReader {
        fn new(path: &Path, start: u64) -> LineReader {
            let mut rd = fs::File::open(path).ok().map(|f| BufReader::with_capacity(65536, f));
            let mut pos = 0;
            if start > 0 {
                if let Some(r) = rd.as_mut() {
                    if r.seek(SeekFrom::Start(start)).is_ok() {
                        pos = start;
                    }
                }
            }
            LineReader { rd, pos }
        }
        fn seek(&mut self, start: u64) {
            if let Some(r) = self.rd.as_mut() {
                if r.seek(SeekFrom::Start(start)).is_ok() {
                    self.pos = start;
                }
            }
        }
        // one raw line without its trailing \n
        fn line(&mut self) -> Option<Vec<u8>> {
            let r = self.rd.as_mut()?;
            let mut buf = Vec::new();
            match r.read_until(b'\n', &mut buf) {
                Ok(0) | Err(_) => None,
                Ok(n) => {
                    self.pos += n as u64;
                    if buf.last() == Some(&b'\n') {
                        buf.pop();
                    }
                    Some(buf)
                }
            }
        }
    }

    fn line_text(mut b: Vec<u8>) -> String {
        if b.last() == Some(&b'\r') {
            b.pop();
        }
        String::from_utf8_lossy(&b).into_owned()
    }

    fn parse_delimited(line: &str, delim: char) -> Vec<String> {
        let mut fields = Vec::new();
        if line.is_empty() {
            return fields;
        }
        let mut field = String::new();
        let mut state = 0;
        for c in line.chars() {
            match state {
                0 => {
                    if c == '"' {
                        state = 2;
                    } else if c == delim {
                        fields.push(String::new());
                    } else {
                        field.push(c);
                        state = 1;
                    }
                }
                1 => {
                    if c == delim {
                        fields.push(std::mem::take(&mut field));
                        state = 0;
                    } else {
                        field.push(c);
                    }
                }
                2 => {
                    if c == '"' {
                        state = 3;
                    } else {
                        field.push(c);
                    }
                }
                _ => {
                    if c == '"' {
                        field.push('"');
                        state = 2;
                    } else if c == delim {
                        fields.push(std::mem::take(&mut field));
                        state = 0;
                    } else {
                        field.push(c);
                        state = 1;
                    }
                }
            }
        }
        fields.push(field);
        fields
    }

    enum Reader {
        Delim { lr: LineReader, delim: char, header: Option<Vec<String>> },
        Jsonl { lr: LineReader },
        JsonArr(JsonArr),
    }

    struct JsonArr {
        f: Option<fs::File>,
        pos: u64,
        data: Vec<u8>,
        n: usize,
        i: usize,
        seg: usize,
        depth: i64,
        in_str: bool,
        esc: bool,
        cur: Option<Vec<u8>>,
    }

    impl JsonArr {
        fn new(path: &Path, start: u64) -> JsonArr {
            let mut f = fs::File::open(path).ok();
            let mut pos = 0;
            if start > 0 {
                if let Some(fh) = f.as_mut() {
                    if fh.seek(SeekFrom::Start(start)).is_ok() {
                        pos = start;
                    }
                }
            }
            JsonArr { f, pos, data: vec![0u8; 65536], n: 0, i: 0, seg: 0, depth: 0, in_str: false, esc: false, cur: None }
        }
        fn fill(&mut self) -> bool {
            if let Some(c) = self.cur.as_mut() {
                c.extend_from_slice(&self.data[self.seg..self.n]);
            }
            self.pos += self.n as u64;
            self.n = 0;
            self.i = 0;
            self.seg = 0;
            let fh = match self.f.as_mut() {
                Some(f) => f,
                None => return false,
            };
            loop {
                match fh.read(&mut self.data) {
                    Ok(n) => {
                        self.n = n;
                        return n > 0;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => return false,
                }
            }
        }
        fn next(&mut self) -> Option<(J, u64)> {
            loop {
                if self.i >= self.n && !self.fill() {
                    return None;
                }
                let i = self.i;
                let b = self.data[i];
                self.i += 1;
                if self.in_str {
                    if self.esc {
                        self.esc = false;
                    } else if b == 0x5C {
                        self.esc = true;
                    } else if b == 0x22 {
                        self.in_str = false;
                    }
                    continue;
                }
                if b == 0x22 {
                    if self.depth > 0 {
                        self.in_str = true;
                    }
                    continue;
                }
                if b == 0x7B || b == 0x5B {
                    if b == 0x7B && self.depth == 0 {
                        self.cur = Some(Vec::new());
                        self.seg = i;
                        self.depth = 1;
                    } else if self.depth > 0 {
                        self.depth += 1;
                    }
                    continue;
                }
                if (b == 0x7D || b == 0x5D) && self.depth > 0 {
                    self.depth -= 1;
                    if self.depth == 0 {
                        let mut cur = self.cur.take().unwrap_or_default();
                        cur.extend_from_slice(&self.data[self.seg..i + 1]);
                        let off = self.pos + i as u64 + 1;
                        if let Some(obj @ J::Obj(_)) = parse_json(&cur) {
                            return Some((obj, off));
                        }
                    }
                }
            }
        }
    }

    fn strip_bom(s: &str) -> &str {
        s.strip_prefix('\u{FEFF}').unwrap_or(s)
    }

    impl Reader {
        fn new(path: &Path, start: u64) -> Reader {
            let fmt = spec().format.as_str();
            match fmt {
                "csv" | "tsv" => {
                    let delim = if fmt == "csv" { ',' } else { '\t' };
                    let mut lr = LineReader::new(path, 0);
                    let mut header = None;
                    while let Some(b) = lr.line() {
                        let t = line_text(b);
                        let t = strip_bom(&t);
                        if t.is_empty() {
                            continue;
                        }
                        header = Some(parse_delimited(t, delim));
                        break;
                    }
                    if header.is_some() && start > lr.pos {
                        lr.seek(start);
                    }
                    Reader::Delim { lr, delim, header }
                }
                "jsonl" => Reader::Jsonl { lr: LineReader::new(path, start) },
                _ => Reader::JsonArr(JsonArr::new(path, start)),
            }
        }

        fn next(&mut self) -> Option<(J, u64)> {
            match self {
                Reader::Delim { lr, delim, header } => {
                    let header = header.as_ref()?;
                    while let Some(b) = lr.line() {
                        let t = line_text(b);
                        if t.is_empty() {
                            continue;
                        }
                        let f = parse_delimited(&t, *delim);
                        let mut row = J::obj();
                        for (i, h) in header.iter().enumerate() {
                            row.set(h, match f.get(i) {
                                Some(x) => J::Str(x.clone()),
                                None => J::Nul,
                            });
                        }
                        return Some((row, lr.pos));
                    }
                    None
                }
                Reader::Jsonl { lr } => {
                    while let Some(b) = lr.line() {
                        let t = String::from_utf8_lossy(&b).into_owned();
                        let mut t = py_strip(&t);
                        if t.starts_with('\u{FEFF}') {
                            t = py_strip(strip_bom(&t));
                        }
                        if t.is_empty() {
                            continue;
                        }
                        if let Some(obj @ J::Obj(_)) = parse_json(t.as_bytes()) {
                            return Some((obj, lr.pos));
                        }
                    }
                    None
                }
                Reader::JsonArr(r) => r.next(),
            }
        }
    }

    // ------------------------------------------------------------ output conversion
    fn to_value(j: &J) -> Value {
        match j {
            J::Nul => Value::Null,
            J::Bool(b) => Value::Bool(*b),
            J::Int(i) => Value::Int(*i),
            J::Dbl(d) => Value::Float(*d),
            J::Str(s) => Value::Str(s.clone()),
            other => Value::Str(dumps(other)),
        }
    }

    pub fn to_row(j: &J) -> Row {
        let mut r = Row::new();
        if let J::Obj(o) = j {
            for (k, v) in o {
                r.insert(k.clone(), to_value(v));
            }
        }
        r
    }

    // ------------------------------------------------------------ cache helpers
    fn fnv1a(s: &[u8], mut h: u64) -> u64 {
        for &c in s {
            h ^= c as u64;
            h = h.wrapping_mul(1099511628211);
        }
        h
    }

    fn absolute(p: &Path) -> PathBuf {
        let base = if p.is_absolute() {
            PathBuf::new()
        } else {
            std::env::current_dir().unwrap_or_default()
        };
        let joined = base.join(p);
        let mut out = PathBuf::new();
        for c in joined.components() {
            match c {
                Component::ParentDir => {
                    out.pop();
                }
                Component::CurDir => {}
                other => out.push(other.as_os_str()),
            }
        }
        out
    }

    fn write_atomic(path: &Path, content: &str) {
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        if fs::write(&tmp, content.as_bytes()).is_ok() {
            let _ = fs::rename(&tmp, path);
        }
    }

    enum Ev {
        Row(J),
        Off(u64),
    }

    #[derive(PartialEq)]
    enum Mode {
        None,
        Plain,
        Replay,
        Resume,
        Done,
    }

    pub struct Engine {
        buffer: usize,
        cache_dir: Option<String>,
        mode: Mode,
        path: PathBuf,
        reader: Option<Reader>,
        mach: Machine,
        q: VecDeque<Ev>,
        finished: bool,
        rows_path: PathBuf,
        state_path: PathBuf,
        state: J,
        replay: Option<BufReader<fs::File>>,
        out: Option<fs::File>,
        n_cached: i64,
        counter: i64,
        steps: usize,
        resume_started: bool,
    }

    impl Engine {
        pub fn new(buffer: usize, cache_dir: Option<&str>) -> Engine {
            Engine {
                buffer: buffer.max(1),
                cache_dir: cache_dir.map(|s| s.to_string()),
                mode: Mode::None,
                path: PathBuf::new(),
                reader: None,
                mach: Machine::new(),
                q: VecDeque::new(),
                finished: false,
                rows_path: PathBuf::new(),
                state_path: PathBuf::new(),
                state: J::Nul,
                replay: None,
                out: None,
                n_cached: 0,
                counter: 0,
                steps: 0,
                resume_started: false,
            }
        }

        fn close(&mut self) {
            self.out = None;
            self.replay = None;
            self.reader = None;
            self.q.clear();
            self.finished = false;
            self.resume_started = false;
            self.mode = Mode::None;
        }

        fn start_stream(&mut self, start: u64, snap: Option<J>) {
            self.mach = Machine::new();
            if let Some(s @ J::Obj(_)) = snap {
                self.mach.s = s;
            }
            self.reader = Some(Reader::new(&self.path, start));
            self.q.clear();
            self.finished = false;
        }

        fn fill(&mut self) -> bool {
            if self.finished {
                return false;
            }
            let mut outs = Vec::new();
            let item = self.reader.as_mut().and_then(|r| r.next());
            if let Some((row, off)) = item {
                self.mach.push(row, &mut outs);
                for t in outs {
                    self.q.push_back(Ev::Row(t));
                }
                self.q.push_back(Ev::Off(off));
                return true;
            }
            self.mach.finish(&mut outs);
            for t in outs {
                self.q.push_back(Ev::Row(t));
            }
            self.finished = true;
            !self.q.is_empty()
        }

        pub fn open(&mut self, p: &Path) -> std::io::Result<()> {
            self.close();
            self.path = p.to_path_buf();
            let meta = fs::metadata(p)?;
            let cache_dir = match &self.cache_dir {
                None => {
                    self.start_stream(0, None);
                    self.mode = Mode::Plain;
                    return Ok(());
                }
                Some(c) => c.clone(),
            };
            let ap = absolute(p).to_string_lossy().into_owned();
            let mut keysrc = Vec::new();
            keysrc.extend_from_slice(ap.as_bytes());
            keysrc.push(0);
            keysrc.extend_from_slice(SPEC_HASH.as_bytes());
            keysrc.push(0);
            keysrc.extend_from_slice(b"rust");
            let key = format!(
                "rs-{:016x}{:016x}",
                fnv1a(&keysrc, 1469598103934665603),
                fnv1a(&keysrc, 0x9E3779B97F4A7C15)
            );
            let d = Path::new(&cache_dir).join(key);
            fs::create_dir_all(&d)?;
            self.rows_path = d.join("rows.jsonl");
            self.state_path = d.join("state.json");
            let mtime = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|x| x.as_nanos() as i64)
                .unwrap_or(0);
            let fp = J::Arr(vec![J::Int(meta.len() as i64), J::Int(mtime)]);
            let st = fs::read(&self.state_path).ok().and_then(|b| parse_json(&b)).unwrap_or(J::Nul);
            let valid = matches!(st, J::Obj(_))
                && deep_eq(st.get("fingerprint"), &fp)
                && matches!(st.get("path"), J::Str(s) if *s == ap);
            let st = if valid {
                st
            } else {
                let mut s = J::obj();
                s.set("path", J::Str(ap.clone()));
                s.set("fingerprint", fp);
                s.set("complete", J::Bool(false));
                s.set("out", J::Int(0));
                s.set("offset", J::Nul);
                fs::write(&self.rows_path, b"")?;
                write_atomic(&self.state_path, &dumps(&s));
                s
            };
            self.state = st;
            if truthy(self.state.get("complete")) {
                self.replay = Some(BufReader::new(fs::File::open(&self.rows_path)?));
                self.mode = Mode::Replay;
            } else {
                self.mode = Mode::Resume;
                self.resume_started = false;
            }
            Ok(())
        }

        fn begin_resume(&mut self) {
            self.resume_started = true;
            let mut n: i64 = 0;
            let mut good: u64 = 0;
            if let Ok(data) = fs::read(&self.rows_path) {
                for (i, &b) in data.iter().enumerate() {
                    if b == b'\n' {
                        n += 1;
                        good = i as u64 + 1;
                    }
                }
                if let Ok(f) = fs::OpenOptions::new().write(true).open(&self.rows_path) {
                    let _ = f.set_len(good);
                }
            }
            self.n_cached = n;
            let mut m = as_i(self.state.get("out"));
            let mut offset = self.state.get("offset").clone();
            let mut snap = self.state.get("snap").clone();
            if m > n || (!offset.is_null() && !matches!(snap, J::Obj(_))) {
                m = 0;
                offset = J::Nul;
                snap = J::Nul;
            }
            if offset.is_null() {
                self.start_stream(0, None);
            } else {
                self.start_stream(as_i(&offset).max(0) as u64, Some(snap));
            }
            self.counter = m;
            self.steps = 0;
            self.out = fs::OpenOptions::new().create(true).append(true).open(&self.rows_path).ok();
        }

        fn next_plain(&mut self) -> Option<J> {
            loop {
                while let Some(e) = self.q.pop_front() {
                    if let Ev::Row(t) = e {
                        return Some(t);
                    }
                }
                if !self.fill() {
                    return None;
                }
            }
        }

        fn next_resume(&mut self) -> Option<J> {
            if !self.resume_started {
                self.begin_resume();
            }
            loop {
                while let Some(e) = self.q.pop_front() {
                    match e {
                        Ev::Off(off) => {
                            self.steps += 1;
                            if self.steps >= self.buffer {
                                self.steps = 0;
                                self.state.set("out", J::Int(self.counter));
                                self.state.set("offset", J::Int(off as i64));
                                self.state.set("snap", self.mach.s.clone());
                                write_atomic(&self.state_path, &dumps(&self.state));
                            }
                        }
                        Ev::Row(t) => {
                            self.counter += 1;
                            if self.counter <= self.n_cached {
                                continue;
                            }
                            if let Some(f) = self.out.as_mut() {
                                let mut line = dumps(&t);
                                line.push('\n');
                                let _ = f.write_all(line.as_bytes());
                                let _ = f.flush();
                            }
                            return Some(t);
                        }
                    }
                }
                if !self.fill() {
                    self.state.set("out", J::Int(self.counter));
                    self.state.set("offset", J::Nul);
                    self.state.set("snap", J::Nul);
                    self.state.set("complete", J::Bool(true));
                    write_atomic(&self.state_path, &dumps(&self.state));
                    self.out = None;
                    self.reader = None;
                    self.mode = Mode::Done;
                    return None;
                }
            }
        }

        fn next_replay(&mut self) -> Option<J> {
            if let Some(r) = self.replay.as_mut() {
                loop {
                    let mut buf = Vec::new();
                    match r.read_until(b'\n', &mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            if buf.last() != Some(&b'\n') {
                                break;
                            }
                            buf.pop();
                            if let Some(j) = parse_json(&buf) {
                                return Some(j);
                            }
                        }
                    }
                }
            }
            self.replay = None;
            self.mode = Mode::Done;
            None
        }

        pub fn next_row(&mut self) -> Option<J> {
            match self.mode {
                Mode::Plain => {
                    let r = self.next_plain();
                    if r.is_none() {
                        self.reader = None;
                        self.mode = Mode::Done;
                    }
                    r
                }
                Mode::Resume => self.next_resume(),
                Mode::Replay => self.next_replay(),
                _ => None,
            }
        }
    }
}
'''


if __name__ == "__main__":
    sys.exit(main())
