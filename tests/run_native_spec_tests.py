#!/usr/bin/env python3
"""Differential tests of the C++ / Rust runtimes against the Python runtime on
hand-written specs that exercise every transform / filter / stateful kind,
with tricky data: Unicode case mapping and whitespace, numeric strings,
rounding ties, NaN / infinities, CSV quoting (embedded delimiters, quotes and
newlines), CRLF line endings and a UTF-8 BOM.

Usage: python tests/run_native_spec_tests.py [cpp|rust]
"""

import csv
import importlib.util
import io
import json
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import dynamic_buffer as db  # noqa: E402
import run_native_tests as nt  # noqa: E402

NUMS = [0, 1, -3, 7, 12, 2.5, 3.5, -2.5, 0.125, 0.375, 1.005, 2.675, 1e16, 123456.789, -0.0, 1e-7, 99.99]
NUM_STRS = ["5", " 5 ", "+3", "-0", "1.", ".5", "1e3", "2.5", "abc", "", "1_000", "0x10", " 12 ",
            "007", "-4.25", "3.0", "1e400", "-1e-400", "123456789012"]
STRS = ["Hello World", "  padded  ", "Straße", "ΟΔΟΣ ΣΑΣ", "ünïcödé", "a,b;c|d", "x - y - z", " nb ",
        "UPPER lower", "tab\tsep", "", "one", "q\"uote", "multi\nline", "İstanbul", "a@b.com", "trailing,"]


def make_rows(rng, n, text):
    rows = []
    for i in range(n):
        if text:
            x = rng.choice(NUM_STRS + [str(v) for v in NUMS])
            y = rng.choice(NUM_STRS + [str(v) for v in NUMS])
        else:
            x = rng.choice(NUMS + NUM_STRS + [None, True])
            y = rng.choice(NUMS + [None])
        rows.append({"i": i if not text else str(i), "x": x, "y": y, "s": rng.choice(STRS),
                     "g": rng.choice(["a", "b", "c"]), "k": rng.choice(["a", "b", "c"])})
    return rows


def num_f(col, op, v, rel=0):
    d = {"k": "num", "col": col, "op": op, "v": v}
    if rel:
        d["rel"] = rel
    return d


def lin(src, a, b, rnd, fmt):
    return {"type": "linear", "src": src, "a": a, "b": b, "round": rnd, "fmt": fmt}


def post(a=1, b=0, rnd=None, fmt=("raw",)):
    return lin("_s", a, b, rnd, list(fmt))


def st(kind, scope="all", none=None, **kw):
    d = {"type": "st", "kind": kind, "scope": scope, "none": none}
    d.update(kw)
    return d


def columns_basic(text):
    cols = [
        ["i", {"type": "copy", "src": "i", "ops": []}],
        ["s_up", {"type": "copy", "src": "s", "ops": ["strip", "upper"]}],
        ["s_lo", {"type": "copy", "src": "s", "ops": ["lower"]}],
        ["x_str", {"type": "copy", "src": "x", "ops": ["strip"]}],
        ["c", {"type": "const", "value": "web"}],
        ["c2", {"type": "const", "value": 2.5}],
        ["m", {"type": "missing", "value": None}],
        ["af", {"type": "affix", "src": "s", "ops": ["strip"], "prefix": "<", "suffix": ">"}],
        ["sp0", {"type": "split", "src": "s", "ops": [], "sep": None, "mode": "idx", "index": 0,
                 "default": "", "post_strip": False}],
        ["sp_1", {"type": "split", "src": "s", "ops": ["strip"], "sep": " - ", "mode": "idx", "index": -1,
                  "default": None, "post_strip": True}],
        ["sp2", {"type": "split", "src": "s", "ops": [], "sep": ",", "mode": "idx", "index": 1,
                 "default": "?", "post_strip": False}],
        ["rest", {"type": "split", "src": "s", "ops": [], "sep": "@", "mode": "rest", "default": None,
                  "post_strip": False}],
        ["init", {"type": "split", "src": "s", "ops": [], "sep": " ", "mode": "init", "default": None,
                  "post_strip": True}],
        ["cat", {"type": "concat", "a": "s", "b": "x", "ops": ["strip"], "prefix": "[", "sep": "|",
                 "suffix": "]"}],
    ]
    fmts = [["raw"], ["int"], ["float"], ["intify"], ["s_g"], ["s_int"], ["s_fixed", 2], ["s_fixed", 0],
            ["s_repr"]]
    k = 0
    for a, b in [(1, 0), (1.5, 2), (0.1, 0), (3, -1), (2.5, 0.25), (1e308, 0)]:
        for rnd in (None, 0, 1, 2):
            for fmt in fmts:
                k += 1
                if (k * 7) % 3 == 0:
                    continue
                cols.append(["l%d" % k, lin("x", a, b, rnd, fmt)])
    for op in ("+", "-", "*", "/"):
        for rnd, fmt in ((None, ["raw"]), (2, ["s_fixed", 2]), (0, ["int"])):
            cols.append(["ar%s%s" % ({"+": "p", "-": "m", "*": "t", "/": "d"}[op], len(cols)),
                         {"type": "arith", "a": "x", "b": "y", "op": op, "round": rnd, "fmt": fmt,
                          "default": "" if text else None}])
    return cols


def columns_stateful(text):
    nv = "" if text else None
    sfmt = ["s_g"] if text else ["intify"]
    cond_big = num_f("x", ">", 4)
    return [
        ["i", {"type": "copy", "src": "i", "ops": []}],
        ["cum", st("agg", fn="sum", src="x", w=0, acc="plain", post=post(fmt=sfmt), none=nv)],
        ["cumneu", st("agg", fn="sum", src="x", w=0, acc="neu", post=post(), none=nv)],
        ["cummean", st("agg", fn="mean", src="x", w=0, post=post(1, 0, 3, ["raw"]), none=nv)],
        ["cmin", st("agg", fn="min", src="x", w=0, post=post(), none=nv)],
        ["cmax", st("agg", fn="max", src="y", w=0, post=post(), none=nv, scope="kept")],
        ["w3", st("agg", fn="sum", src="x", w=3, acc="neu", post=post(), none=nv)],
        ["w5mean", st("agg", fn="mean", src="x", w=5, full=True, post=post(2, 1, 2, ["s_fixed", 2]), none="-")],
        ["w4min", st("agg", fn="min", src="y", w=4, post=post(), none=nv)],
        ["w64max", st("agg", fn="max", src="x", w=64, post=post(), none=nv)],
        ["wcnt", st("agg", fn="count", cond=cond_big, w=7, post=post(), none=nv)],
        ["pcnt", st("agg", fn="count", cond={"k": "eq", "col": "g", "op": "==", "v": "a"}, w=0, post=None,
                    none=nv, scope="kept")],
        ["seg", st("counter", cond={"k": "same", "col": "g", "rel": -1, "op": "!="}, post=None)],
        ["thr", st("counter", cond={"k": "edge", "mode": "rise", "arg": cond_big}, post=post(10, 0))],
        ["cyc", st("counter", cond={"k": "const", "v": True}, labels=["r", "g", "b"])],
        ["lag1", st("lag", src="s", k=1, none="NA")],
        ["lag3", st("lag", src="x", k=3, none=nv, scope="kept")],
        ["dif", st("diff", src="x", post=post(fmt=sfmt), none=nv)],
        ["fsm", st("fsm", init="idle", rules=[
            [{"k": "eq", "col": "g", "op": "==", "v": "a"}, "A"],
            [{"k": "and", "args": [{"k": "eq", "col": "k", "op": "==", "v": "b"}, num_f("x", "<=", 3)]}, "B"],
            [{"k": "or", "args": [{"k": "str", "col": "s", "op": ">", "v": "t"},
                                  {"k": "colcmp", "a": "g", "b": "k", "op": "=="}]}, "C"],
        ])],
        ["fsmk", st("fsm", init=0, scope="kept", rules=[
            [{"k": "numeq", "col": "x", "op": "==", "v": 7}, 1],
            [{"k": "edge", "mode": "fall", "arg": num_f("y", ">=", 2)}, 2],
            [{"k": "edge", "mode": "change", "arg": {"k": "eq", "col": "g", "op": "==", "v": "c"}}, 3],
        ])],
    ]


FILTERS = [
    None,
    {"k": "or", "args": [num_f("x", ">=", 2), {"k": "str", "col": "s", "op": "<", "v": "M"}]},
    {"k": "and", "args": [{"k": "same", "col": "g", "rel": 1, "op": "!="},
                          {"k": "eq", "col": "s", "op": "!=", "v": "one"}]},
    {"k": "or", "args": [{"k": "same", "col": "g", "rel": -1, "op": "!="}, num_f("x", "<", 1, rel=1),
                         {"k": "numeq", "col": "y", "op": "!=", "v": 2.5}]},
]


def load_py(path):
    spec = importlib.util.spec_from_file_location("dyna_ref_%d" % abs(hash(path)), os.path.join(path, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tag(v):
    if v is None:
        return ["n"]
    if isinstance(v, bool):
        return ["b", v]
    if isinstance(v, int):
        # The native Value types hold 64-bit integers; Python ints beyond that
        # range come out as (the nearest) double.
        if -2 ** 63 <= v < 2 ** 63:
            return ["i", v]
        return ["f", repr(float(v))]
    if isinstance(v, float):
        return ["f", repr(v)]
    if isinstance(v, str):
        return ["s", v]
    return ["s", json.dumps(v)]


def write_raw(path, fmt, rows, cols, rng):
    if fmt in ("csv", "tsv"):
        sio = io.StringIO()
        w = csv.writer(sio, delimiter="," if fmt == "csv" else "\t", lineterminator="\r\n")
        w.writerow(cols)
        for i, r in enumerate(rows):
            w.writerow(["" if r[c] is None else r[c] for c in cols])
            if i % 17 == 5:
                sio.write("\r\n")  # blank line (skipped)
        text = "﻿" + sio.getvalue()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    elif fmt == "jsonl":
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("﻿")
            for i, r in enumerate(rows):
                f.write(json.dumps(r, ensure_ascii=bool(i % 2)) + ("\r\n" if i % 3 else "\n"))
                if i % 11 == 3:
                    f.write("   \n")
            f.write(json.dumps({"i": 99999, "x": float("nan"), "y": float("inf"), "s": "nan", "g": "a",
                                "k": "b"}) + "\n")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write("[\n")
            for i, r in enumerate(rows):
                f.write(("  " if i % 2 else "") + json.dumps(r, ensure_ascii=bool(i % 3)) +
                        (",\n" if i + 1 < len(rows) else "\n"))
            f.write("]\n")


def main():
    langs = [a for a in sys.argv[1:] if a in ("cpp", "rust")] or ["cpp", "rust"]
    tmp = tempfile.mkdtemp(prefix="dbns_")
    open(os.path.join(tmp, "h.cpp"), "w").write(nt.HARNESS_CPP)
    open(os.path.join(tmp, "h.rs"), "w").write(nt.HARNESS_RS)
    total = failures = 0
    case = 0
    for fmt in ("csv", "tsv", "jsonl", "json"):
        text = fmt in ("csv", "tsv")
        for kind in ("basic", "stateful"):
            for fi, filt in enumerate(FILTERS):
                case += 1
                rng = random.Random(case)
                rows = make_rows(rng, 220, text)
                cols = columns_basic(text) if kind == "basic" else columns_stateful(text)
                spec = {"format": fmt, "input_columns": ["i", "x", "y", "s", "g", "k"], "columns": cols,
                        "filter": filt, "lookahead": db.uses_next(filt)}
                d = os.path.join(tmp, "%s_%s_%d" % (fmt, kind, fi))
                os.makedirs(d)
                data = os.path.join(d, "data." + fmt)
                write_raw(data, fmt, rows, ["i", "x", "y", "s", "g", "k"], rng)
                db.write_file(os.path.join(d, "py", "dyna", "__init__.py"), db.gen_python(spec, "dyna"))
                mod = load_py(os.path.join(d, "py", "dyna"))
                expected = nt.canon([{k: tag(v) for k, v in r.items()} for r in mod.DynamicPreprocessor(5)(data)])
                for lang in langs:
                    total += 1
                    problems = []
                    od = os.path.join(d, lang)
                    if lang == "cpp":
                        h, c = db.gen_cpp(spec, "dyna")
                        db.write_file(os.path.join(od, "dyna", "dynamic_preprocessor.h"), h)
                        db.write_file(os.path.join(od, "dyna", "dynamic_preprocessor.cpp"), c)
                    else:
                        cargo, lib = db.gen_rust(spec, "dyna")
                        db.write_file(os.path.join(od, "dyna", "Cargo.toml"), cargo)
                        db.write_file(os.path.join(od, "dyna", "src", "lib.rs"), lib)
                    try:
                        base = nt.build_cpp(od, tmp) if lang == "cpp" else nt.build_rust(od, tmp)
                    except RuntimeError as e:
                        problems.append("build: " + str(e)[:3000])
                        base = None
                    if base:
                        for buf in (1, 4, 1000):
                            got = nt.canon(json.loads(nt.run(base + [data, str(buf), "full"]).stdout))
                            if got != expected:
                                msg = "buf=%d len got %d exp %d" % (buf, len(got), len(expected))
                                for i, (a, b) in enumerate(zip(got, expected)):
                                    if a != b:
                                        diffs = [(x, y) for x, y in zip(a, b) if x != y]
                                        msg += "; row %d: %s" % (i, diffs[:4])
                                        break
                                problems.append("mismatch: " + msg)
                                break
                        cache = os.path.join(d, "cache_" + lang)
                        acc = []
                        for _ in range(12):
                            k = rng.choice([1, 5, 13, 29])
                            part = nt.canon(json.loads(nt.run(base + [data, "3", "resume", cache, str(k)]).stdout))
                            acc += part
                            if len(part) < k:
                                break
                        if len(acc) < len(expected):
                            acc += nt.canon(json.loads(nt.run(base + [data, "3", "resume", cache, "-1"]).stdout))
                        if acc != expected:
                            problems.append("resume mismatch (%d vs %d)" % (len(acc), len(expected)))
                    failures += bool(problems)
                    print("%s %-5s %-8s filter%d %-4s %s" % ("OK " if not problems else "FAIL", fmt, kind, fi, lang,
                                                            "; ".join(problems)), flush=True)
    print("%d/%d passed" % (total - failures, total))
    if not os.environ.get("KEEP"):
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(tmp)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
