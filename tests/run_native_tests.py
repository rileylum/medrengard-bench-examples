#!/usr/bin/env python3
"""End-to-end tests for the C++ (--cpp) and Rust (--rust) targets.

For every scenario of run_tests.py (per-row transforms) and
run_stateful_tests.py (stateful / neighbor transforms), in all four formats,
generate the Python module (reference) and the C++ / Rust modules, compile
small harnesses against them and check that

  * full runs with several buffer sizes reproduce the Python module's rows
    exactly (same values *and* types: int vs float vs string ...);
  * many stop / resume cycles in separate processes sharing one cache_dir
    concatenate to the full output;
  * generation is deterministic.

Usage: python tests/run_native_tests.py [cpp|rust] [scenario names...]
Set KEEP=1 to keep the temporary directory.  Rust crates are compiled with
rustc directly (one crate is additionally built with `cargo build`).
"""

import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_tests as rt  # noqa: E402
import run_stateful_tests as rst  # noqa: E402

GEN = rt.GEN

HARNESS_CPP = r'''
#include "dynamic_preprocessor.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
using namespace dyna;

static void str_out(const std::string& s) {
    std::string o = "\"";
    for (unsigned char c : s) {
        if (c == '"') o += "\\\"";
        else if (c == '\\') o += "\\\\";
        else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    o += "\"";
    fputs(o.c_str(), stdout);
}

static void val_out(const Value& v) {
    switch (v.type) {
        case ValueType::Null: fputs("[\"n\"]", stdout); break;
        case ValueType::Bool: printf("[\"b\",%s]", v.bool_value ? "true" : "false"); break;
        case ValueType::Int: printf("[\"i\",%lld]", v.int_value); break;
        case ValueType::Double: printf("[\"f\",\"%.17g\"]", v.double_value); break;
        case ValueType::String: fputs("[\"s\",", stdout); str_out(v.string_value); fputs("]", stdout); break;
    }
}

int main(int argc, char** argv) {
    std::string data = argv[1];
    std::size_t buf = std::strtoul(argv[2], nullptr, 10);
    std::string mode = argv[3];
    long k = -1;
    DynamicPreprocessor* pre;
    if (mode == "full") pre = new DynamicPreprocessor(buf);
    else { pre = new DynamicPreprocessor(buf, argv[4]); k = std::strtol(argv[5], nullptr, 10); }
    pre->open(data);
    Row row;
    long n = 0;
    fputs("[", stdout);
    while ((k < 0 || n < k) && pre->next(row)) {
        if (n) fputs(",", stdout);
        fputs("{", stdout);
        bool first = true;
        for (const auto& p : row) {
            if (!first) fputs(",", stdout);
            first = false;
            str_out(p.first);
            fputs(":", stdout);
            val_out(p.second);
        }
        fputs("}", stdout);
        n++;
    }
    fputs("]\n", stdout);
    delete pre;
    return 0;
}
'''

HARNESS_RS = r'''
use dyna::api::{DynamicPreprocessor, Row, Value};
use std::fmt::Write;

fn s_out(s: &str, o: &mut String) {
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => { write!(o, "\\u{:04x}", c as u32).unwrap(); }
            c => o.push(c),
        }
    }
    o.push('"');
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let buf: usize = a[2].parse().unwrap();
    let rows: Vec<Row> = if a[3] == "full" {
        let mut p = DynamicPreprocessor::new(buf, None);
        p.open(&a[1]).unwrap();
        p.collect()
    } else {
        let mut p = DynamicPreprocessor::new(buf, Some(a[4].as_str()));
        p.open(&a[1]).unwrap();
        let k: i64 = a[5].parse().unwrap();
        if k < 0 { p.collect() } else { p.take(k as usize).collect() }
    };
    let mut o = String::from("[");
    for (i, r) in rows.iter().enumerate() {
        if i > 0 { o.push(','); }
        o.push('{');
        for (j, (key, v)) in r.iter().enumerate() {
            if j > 0 { o.push(','); }
            s_out(key, &mut o);
            o.push(':');
            match v {
                Value::Null => o.push_str("[\"n\"]"),
                Value::Bool(b) => { write!(o, "[\"b\",{}]", b).unwrap(); }
                Value::Int(n) => { write!(o, "[\"i\",{}]", n).unwrap(); }
                Value::Float(f) => { write!(o, "[\"f\",\"{:?}\"]", f).unwrap(); }
                Value::Str(s) => { o.push_str("[\"s\","); s_out(s, &mut o); o.push(']'); }
            }
        }
        o.push('}');
    }
    o.push_str("]\n");
    print!("{}", o);
}
'''

DRIVER_PY = r'''
import sys, json, importlib, math
sys.path.insert(0, sys.argv[1])
mod = importlib.import_module("dyna")
def tag(v):
    if v is None: return ["n"]
    if isinstance(v, bool): return ["b", v]
    if isinstance(v, int): return ["i", v]
    if isinstance(v, float): return ["f", repr(v)]
    if isinstance(v, str): return ["s", v]
    return ["s", json.dumps(v)]
rows = list(mod.DynamicPreprocessor(int(sys.argv[3]))(sys.argv[2]))
print(json.dumps([{k: tag(v) for k, v in r.items()} for r in rows]))
'''


def canon(rows):
    """Typed, order-independent canonical form (floats compared bitwise)."""
    out = []
    for r in rows:
        o = {}
        for k, v in r.items():
            if v[0] == "f":
                x = float(v[1])
                v = ["f", "nan" if math.isnan(x) else repr(x)]
            o[k] = tuple(v)
        out.append(tuple(sorted(o.items())))
    return out


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError("command failed: %s\n%s\n%s" % (" ".join(cmd), p.stdout[-2000:], p.stderr[-4000:]))
    return p


def build_cpp(od, tmp):
    mod = os.path.join(od, "dyna")
    exe = os.path.join(od, "h_cpp")
    run(["g++", "-std=c++17", "-O1", "-I", mod, os.path.join(tmp, "h.cpp"),
         os.path.join(mod, "dynamic_preprocessor.cpp"), "-o", exe])
    return [exe]


def build_rust(od, tmp, use_cargo=False):
    mod = os.path.join(od, "dyna")
    exe = os.path.join(od, "h_rs")
    if use_cargo:
        env = dict(os.environ, CARGO_TARGET_DIR=os.path.join(od, "target"))
        run(["cargo", "build", "--quiet", "--offline"], cwd=mod, env=env)
        libdir = os.path.join(od, "target", "debug")
        run(["rustc", "--edition", "2021", os.path.join(tmp, "h.rs"), "--extern",
             "dyna=" + os.path.join(libdir, "libdyna.rlib"), "-L", libdir, "-o", exe])
    else:
        out = os.path.join(od, "rlib")
        os.makedirs(out, exist_ok=True)
        run(["rustc", "--edition", "2021", "--crate-type", "rlib", "--crate-name", "dyna", "-O",
             os.path.join(mod, "src", "lib.rs"), "--out-dir", out])
        run(["rustc", "--edition", "2021", "-O", os.path.join(tmp, "h.rs"), "--extern",
             "dyna=" + os.path.join(out, "libdyna.rlib"), "-L", out, "-o", exe])
    return [exe]


def cases():
    # per-row scenarios (run_tests.py)
    for si, fn in enumerate(rt.SCENARIOS):
        def mk(fn=fn, si=si):
            sample = rt.make_rows(random.Random(10 + si), 120)
            test = rt.make_rows(random.Random(500 + si), 250)
            return sample, test, (lambda rows, text: rt.apply_truth(fn, rows, text))
        yield fn.__name__, mk
    # stateful scenarios (run_stateful_tests.py)
    for si, fn in enumerate(rst.SCENARIOS):
        def mk(fn=fn, si=si):
            sample = rst.make_rows(random.Random(100 + si), 150)
            test = rst.make_rows(random.Random(900 + si), 300)
            return sample, test, fn
        yield fn.__name__, mk


def main():
    args = sys.argv[1:]
    langs = [a for a in args if a in ("cpp", "rust")] or ["cpp", "rust"]
    only = [a for a in args if a not in ("cpp", "rust")]
    tmp = tempfile.mkdtemp(prefix="dbnt_")
    open(os.path.join(tmp, "h.cpp"), "w").write(HARNESS_CPP)
    open(os.path.join(tmp, "h.rs"), "w").write(HARNESS_RS)
    pyd = os.path.join(tmp, "d.py")
    open(pyd, "w").write(DRIVER_PY)
    total = failures = 0
    cargo_done = False
    for name, mk in cases():
        if only and name not in only:
            continue
        for fmt in ("csv", "tsv", "jsonl", "json"):
            text = fmt in ("csv", "tsv")
            sample, test, truth = mk()
            if text:
                sample, test = rt.to_text_rows(sample), rt.to_text_rows(test)
            in_cols = list(sample[0].keys())
            outs = truth(sample, text)
            d = os.path.join(tmp, "%s_%s" % (name, fmt))
            sd = os.path.join(d, "sample")
            os.makedirs(sd)
            rt.write_data(os.path.join(sd, "input." + fmt), fmt, sample, in_cols)
            rt.write_data(os.path.join(sd, "output." + fmt), fmt, outs, list(outs[0].keys()))
            data = os.path.join(d, "data." + fmt)
            rt.write_data(data, fmt, test, in_cols)
            pyo = os.path.join(d, "o_py")
            run([sys.executable, GEN, "dyna", "--output", pyo, "--sample", sd, "--python"])
            expected = canon(json.loads(run([sys.executable, pyd, pyo, data, "7"]).stdout))
            for lang in langs:
                total += 1
                problems = []
                od = os.path.join(d, "o_" + lang)
                od2 = od + "_2"
                for o in (od, od2):
                    p = run([sys.executable, GEN, "dyna", "--output", o, "--sample", sd, "--" + lang])
                files = (["dynamic_preprocessor.h", "dynamic_preprocessor.cpp"] if lang == "cpp"
                         else ["Cargo.toml", os.path.join("src", "lib.rs")])
                for f in files:
                    if open(os.path.join(od, "dyna", f), "rb").read() != open(os.path.join(od2, "dyna", f), "rb").read():
                        problems.append("non-deterministic " + f)
                try:
                    if lang == "cpp":
                        base = build_cpp(od, tmp)
                    else:
                        base = build_rust(od, tmp, use_cargo=not cargo_done)
                        cargo_done = True
                except RuntimeError as e:
                    problems.append("build: " + str(e)[:3000])
                    base = None
                if base:
                    for buf in (1, 3, 1000):
                        got = canon(json.loads(run(base + [data, str(buf), "full"]).stdout))
                        if got != expected:
                            msg = "buf=%d len got %d exp %d" % (buf, len(got), len(expected))
                            for i, (a, b) in enumerate(zip(got, expected)):
                                if a != b:
                                    msg += "; row %d got=%s exp=%s" % (i, a, b)
                                    break
                            problems.append("mismatch: " + msg)
                            break
                    rng = random.Random(len(name) * 7 + len(fmt))
                    for buf in (1, 2, 5):
                        cache = os.path.join(d, "cache_%s_%d" % (lang, buf))
                        acc = []
                        ended = False
                        for _ in range(30):
                            k = rng.choice([1, 2, 3, 7, 11])
                            part = canon(json.loads(run(base + [data, str(buf), "resume", cache, str(k)]).stdout))
                            acc += part
                            if len(part) < k:
                                ended = True
                                break
                        if not ended:
                            # drain (possibly zero remaining rows) so the run reaches end of stream
                            acc += canon(json.loads(run(base + [data, str(buf), "resume", cache, "-1"]).stdout))
                        if acc != expected:
                            problems.append("resume mismatch buf=%d (%d vs %d)" % (buf, len(acc), len(expected)))
                            break
                    # a completed run followed by a fresh run yields everything again
                    again = canon(json.loads(run(base + [data, "4", "resume", cache, "-1"]).stdout))
                    if again != expected:
                        problems.append("rerun after completion mismatch (%d vs %d)" % (len(again), len(expected)))
                status = "OK " if not problems else "FAIL"
                failures += bool(problems)
                print("%s %-22s %-5s %-5s %s" % (status, name, fmt, lang, "; ".join(problems)), flush=True)
    print("%d/%d passed" % (total - failures, total))
    if not os.environ.get("KEEP"):
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(tmp)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
