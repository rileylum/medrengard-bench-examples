"""Cross-language checks for the C++ and Rust targets (part 3).

For every scenario of tests/harness.py and tests/harness_stateful.py a sample is
built, a Python module (the reference) plus C++ and Rust modules are generated,
and the native modules are driven by small harness programs:

* single-pass runs without cache for several buffer sizes,
* a multi-process resume plan through a cache directory (each partial run is a
  separate process), followed by a replay from the completed cache.

Rows are compared type-strictly against the Python module's output.

usage: python tests/harness_native.py [workdir] [scenario,...] [cpp,rust]
"""
import json, math, os, shutil, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEN = os.path.join(ROOT, "dynamic_buffer.py")
WORK = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dbnative"
ONLY = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else None
LANGS = sys.argv[3].split(",") if len(sys.argv) > 3 else ["cpp", "rust"]
sys.path.insert(0, HERE)
import harness  # noqa: E402
import harness_stateful  # noqa: E402

CPP_DRIVER = r'''
#include "dynamic_preprocessor.h"
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <string>
using namespace dyna;

static void put_str(const std::string& s) {
    std::putchar('"');
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { std::putchar('\\'); std::putchar(c); }
        else if (c < 0x20) std::printf("\\u%04x", c);
        else std::putchar(c);
    }
    std::putchar('"');
}

int main(int argc, char** argv) {
    if (argc < 5) return 2;
    std::string path = argv[1], cache = argv[2];
    std::size_t buf = (std::size_t)std::atol(argv[3]);
    long take = std::atol(argv[4]);
    std::unique_ptr<DynamicPreprocessor> p;
    if (cache == "-") p.reset(new DynamicPreprocessor(buf));
    else p.reset(new DynamicPreprocessor(buf, cache));
    p->open(path);
    Row row;
    long n = 0;
    std::printf("[");
    while ((take <= 0 || n < take) && p->next(row)) {
        if (n) std::printf(",");
        std::printf("{");
        bool first = true;
        for (auto& kv : row) {
            if (!first) std::printf(",");
            first = false;
            put_str(kv.first);
            std::printf(":");
            const Value& v = kv.second;
            switch (v.type) {
                case ValueType::Null: std::printf("[\"n\"]"); break;
                case ValueType::Bool: std::printf("[\"b\",%s]", v.bool_value ? "true" : "false"); break;
                case ValueType::Int: std::printf("[\"i\",%lld]", v.int_value); break;
                case ValueType::Double: std::printf("[\"f\",\"%.17g\"]", v.double_value); break;
                case ValueType::String: std::printf("[\"s\","); put_str(v.string_value); std::printf("]"); break;
            }
        }
        std::printf("}");
        ++n;
    }
    std::printf("]\n");
    return 0;
}
'''

RUST_DRIVER = r'''
use dyna::api::{DynamicPreprocessor, Row, Value};

fn js(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn enc(r: &Row) -> String {
    let parts: Vec<String> = r
        .iter()
        .map(|(k, v)| {
            let x = match v {
                Value::Null => "[\"n\"]".to_string(),
                Value::Bool(b) => format!("[\"b\",{}]", b),
                Value::Int(i) => format!("[\"i\",{}]", i),
                Value::Float(f) => format!("[\"f\",\"{:e}\"]", f),
                Value::Str(s) => format!("[\"s\",{}]", js(s)),
            };
            format!("{}:{}", js(k), x)
        })
        .collect();
    format!("{{{}}}", parts.join(","))
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let cache = if a[2] == "-" { None } else { Some(a[2].as_str()) };
    let buf: usize = a[3].parse().unwrap();
    let take: usize = a[4].parse().unwrap();
    let mut pre = DynamicPreprocessor::new(buf, cache);
    pre.open(&a[1]).unwrap();
    let rows: Vec<Row> = if take > 0 { pre.take(take).collect() } else { pre.collect() };
    let enc: Vec<String> = rows.iter().map(enc).collect();
    println!("[{}]", enc.join(","));
}
'''

PY_REF = r'''
import json, sys
from dyna import DynamicPreprocessor as D
print(json.dumps(list(D(buffer=4)(sys.argv[1]))))
'''

PLAN = [(5, 7), (2, 1), (8, 0), (3, 13), (1, 2), (4, 30)]


def typed(v):
    if v is None:
        return ("n",)
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, int):
        return ("i", v)
    if isinstance(v, float):
        return ("f", v)
    return ("s", v)


def decode_native(rows):
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if v[0] == "f":
                d[k] = ("f", float(v[1]))
            elif v[0] == "n":
                d[k] = ("n",)
            else:
                d[k] = (v[0], v[1])
        out.append(d)
    return out


def eq_rows(a, b):
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x.keys() != y.keys():
            return False
        for k in x:
            p, q = x[k], y[k]
            if p[0] != q[0]:
                return False
            if p[0] == "f" and math.isnan(p[1]) and math.isnan(q[1]):
                continue
            if p != q:
                return False
    return True


def build(lang, od, base):
    if lang == "cpp":
        exe = os.path.join(base, "drv_cpp")
        src = os.path.join(base, "drv.cpp")
        with open(src, "w") as fh:
            fh.write(CPP_DRIVER)
        subprocess.run(["g++", "-std=c++17", "-O1", "-Wall", "-I", os.path.join(od, "dyna"), src,
                        os.path.join(od, "dyna", "dynamic_preprocessor.cpp"), "-o", exe],
                       check=True, capture_output=True, text=True)
        return [exe]
    drv = os.path.join(base, "drv_rs")
    os.makedirs(os.path.join(drv, "src"), exist_ok=True)
    with open(os.path.join(drv, "Cargo.toml"), "w") as fh:
        fh.write('[package]\nname = "drv"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'
                 'dyna = { path = "%s" }\n' % os.path.join(od, "dyna"))
    with open(os.path.join(drv, "src", "main.rs"), "w") as fh:
        fh.write(RUST_DRIVER)
    # the crate itself must build standalone, as the autograder does
    subprocess.run(["cargo", "build", "-q"], cwd=os.path.join(od, "dyna"), check=True, capture_output=True,
                   text=True)
    subprocess.run(["cargo", "build", "-q"], cwd=drv, check=True, capture_output=True, text=True)
    return [os.path.join(drv, "target", "debug", "drv")]


def run(cmd, *args):
    res = subprocess.run(cmd + [str(a) for a in args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-2000:])
    return decode_native(json.loads(res.stdout))


def check(task):
    fmt, name, base, sd, tp, lang = task
    od = os.path.join(base, "gen_" + lang)
    try:
        subprocess.run([sys.executable, GEN, "dyna", "--output", od, "--sample", sd, "--" + lang], check=True,
                       capture_output=True)
        cmd = build(lang, od, base)
        ref = [{k: typed(v) for k, v in r.items()} for r in json.loads(
            subprocess.run([sys.executable, "-c", PY_REF, tp], env=dict(os.environ, PYTHONPATH=os.path.join(base, "gen_python")),
                           capture_output=True, text=True, check=True).stdout)]
        bad = []
        for b in (1, 3, 8, 1000):
            if not eq_rows(run(cmd, tp, "-", b, 0), ref):
                bad.append("full%d" % b)
        cd = os.path.join(base, "cache_" + lang)
        got = []
        k = 0
        while True:
            b, take = PLAN[k % len(PLAN)]
            k += 1
            part = run(cmd, tp, cd, b, take)
            got += part
            if take == 0 or len(part) < take or k > 500:
                break
        if not eq_rows(got, ref):
            bad.append("resume")
        if not eq_rows(run(cmd, tp, cd, 2, 0), ref):
            bad.append("again")
        if bad:
            return "FAIL %s %s %s %s (ref %d rows)" % (fmt, name, lang, bad, len(ref))
        return "ok   %s %s %s" % (fmt, name, lang)
    except Exception as e:  # noqa: BLE001
        return "FAIL %s %s %s %s" % (fmt, name, lang, str(e)[-1500:])


def scenarios():
    for name, (f, t) in harness.SCENARIOS.items():
        yield ("row_" + name, lambda rows, f=f, t=t: harness.expected(None, rows, f, t), harness.gen_rows,
               len(name) * 7 + 1)
    for name, fn in harness_stateful.SCEN.items():
        yield ("st_" + name, fn, harness_stateful.gen_rows, len(name) * 13 + 5)


def main():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    tasks = []
    for fmt in ("csv", "tsv", "jsonl", "json"):
        for name, fn, gen, seed in scenarios():
            if ONLY and name not in ONLY:
                continue
            base = os.path.join(WORK, fmt, name)
            sd = os.path.join(base, "sample")
            os.makedirs(sd)
            sin = gen(40, seed)
            cols = list(sin[0].keys())
            sout = fn(sin)
            harness.write(os.path.join(sd, "input." + fmt), fmt, sin, cols)
            harness.write(os.path.join(sd, "output." + fmt), fmt, sout, list(sout[0].keys()))
            tp = os.path.join(base, "data." + fmt)
            harness.write(tp, fmt, gen(300, 99), cols)
            subprocess.run([sys.executable, GEN, "dyna", "--output", os.path.join(base, "gen_python"), "--sample",
                            sd, "--python"], check=True)
            for lang in LANGS:
                tasks.append((fmt, name, base, sd, tp, lang))
    fails = 0
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for line in ex.map(check, tasks):
            print(line, flush=True)
            if line.startswith("FAIL"):
                fails += 1
    print("FAILS", fails)


if __name__ == "__main__":
    main()
