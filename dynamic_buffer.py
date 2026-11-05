#!/usr/bin/env python3
"""dynamic_buffer.py -- infer a row transformation from a sample and generate
a streaming ``DynamicPreprocessor`` module (Python or JavaScript)."""
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


def infer_linear(xs, ys, fmt, out_is_str):
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
    if a == 1.0 and b == 0.0 and all(same(x, y) for x, y in zip(xs, ys)):
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


def infer_filter(in_rows, kept_idx, in_cols, fmt):
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
            tcost = 3 if id_like else 1
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
        ecost = 3 if high_card else (2 if numeric else 1)

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


# ---------------------------------------------------------------- driver
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
        return {"format": fmt, "input_columns": in_cols, "filter": None, "columns": cols}

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
    flt = infer_filter(in_rows, align, in_cols, fmt)
    return {"format": fmt, "input_columns": in_cols, "filter": flt, "columns": cols}


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("module_name")
    ap.add_argument("--output", required=True)
    ap.add_argument("--sample", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--python", action="store_true")
    g.add_argument("--javascript", action="store_true")
    args = ap.parse_args(argv)
    spec = infer(args.sample)
    os.makedirs(args.output, exist_ok=True)
    if args.python:
        generate_python(spec, args.output, args.module_name)
    else:
        generate_js(spec, args.output, args.module_name)
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

    # rows as (out_row, input_end_offset), in batches of at most `buffer`
    def _stream_with_offsets(self, path, start):
        it = read_rows(path, SPEC["format"], start)
        batch = []
        for row, off in it:
            batch.append((row, off))
            if len(batch) >= self.buffer:
                for r, o in batch:
                    t = transform(r)
                    if t is not None:
                        yield t, o
                batch = []
        for r, o in batch:
            t = transform(r)
            if t is not None:
                yield t, o

    def _stream(self, path, start):
        for t, _ in self._stream_with_offsets(path, start):
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
        if m > n:
            m, offset = 0, None
        counter = m
        out = open(rows_path, "ab")
        try:
            since = 0
            for t, off in self._stream_with_offsets(path, offset):
                counter += 1
                if counter <= n:
                    continue
                out.write((_dump(t) + "\n").encode("utf-8"))
                out.flush()
                since += 1
                if since >= self.buffer:
                    since = 0
                    state = dict(state, out=counter, offset=off)
                    _write_json_atomic(state_path, state)
                yield t
            state = dict(state, out=counter, offset=None, complete=True)
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

function* streamWithOffsets(path, start, bufferSize) {
  let batch = [];
  for (const item of readRows(path, SPEC.format, start)) {
    batch.push(item);
    if (batch.length >= bufferSize) {
      const b = batch;
      batch = [];
      for (const [r, o] of b) {
        const t = transform(r);
        if (t !== null) yield [t, o];
      }
    }
  }
  for (const [r, o] of batch) {
    const t = transform(r);
    if (t !== null) yield [t, o];
  }
}

function* plainStream(path, bufferSize) {
  for (const [t] of streamWithOffsets(path, null, bufferSize)) yield t;
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
  if (m > n) { m = 0; offset = null; }
  let counter = m;
  const fd = fs.openSync(rowsPath, "a");
  try {
    let since = 0;
    for (const [t, off] of streamWithOffsets(path, offset, bufferSize)) {
      counter++;
      if (counter <= n) continue;
      fs.writeSync(fd, JSON.stringify(t) + "\n");
      since++;
      if (since >= bufferSize) {
        since = 0;
        state = Object.assign({}, state, { out: counter, offset: off });
        writeJsonAtomic(statePath, state);
      }
      yield t;
    }
    state = Object.assign({}, state, { out: counter, offset: null, complete: true });
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


if __name__ == "__main__":
    sys.exit(main())
