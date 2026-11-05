#!/usr/bin/env python3
"""End-to-end tests: build samples from known ground-truth transforms, generate
modules, and check that they reproduce the ground truth on fresh data
(Python + JavaScript, all formats), plus caching/resume semantics."""

import csv
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "dynamic_buffer.py")

FIRST = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi", "Ivan", "Judy", "Mallory", "Oscar"]
LAST = ["Smith", "Jones", "Brown", "Lee", "Garcia", "Miller", "Davis", "Wilson"]
CITIES = ["Paris", "London", "Berlin", "Tokyo", "Lima", "Oslo"]
STATUS = ["active", "inactive", "pending", "banned"]


def make_rows(rng, n):
    rows = []
    for i in range(n):
        rows.append({
            "id": i + 1,
            "name": "%s %s" % (rng.choice(FIRST), rng.choice(LAST)),
            "age": rng.randint(15, 70),
            "active": rng.random() < 0.7,
            "city": ("  " if rng.random() < 0.3 else "") + rng.choice(CITIES) + (" " if rng.random() < 0.3 else ""),
            "score": round(rng.uniform(0, 100), 1),
            "status": rng.choice(STATUS),
            "email": "%s%d@Example.COM" % (rng.choice(FIRST), rng.randint(1, 99)),
        })
    return rows


def to_text_rows(rows):
    out = []
    for r in rows:
        o = {}
        for k, v in r.items():
            if isinstance(v, bool):
                o[k] = "true" if v else "false"
            elif v is None:
                o[k] = ""
            else:
                o[k] = str(v)
        out.append(o)
    return out


def write_data(path, fmt, rows, cols):
    if fmt in ("csv", "tsv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="," if fmt == "csv" else "\t", lineterminator="\n")
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])
    elif fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)


def num(v):
    return float(v) if isinstance(v, str) else v


def fmt_num(v, text, nd=None):
    if text:
        if nd is not None:
            return "%.*f" % (nd, v)
        return str(v)
    return v


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


@scenario
def s_example(r, text):
    keep = (r["active"] in (True, "true")) and num(r["age"]) >= 21
    if not keep:
        return None
    return {"id": r["id"], "name": r["name"]}


@scenario
def s_transforms(r, text):
    if num(r["score"]) < 40:
        return None
    age = int(num(r["age"]))
    return {
        "user_id": r["id"],
        "full_name": r["name"].upper(),
        "first": r["name"].split(" ")[0],
        "last": r["name"].split(" ")[-1],
        "city": r["city"].strip().lower(),
        "age_months": fmt_num(age * 12 + 6, text),
        "source": "web",
        "email": r["email"].lower(),
        "tag": "user-" + str(r["id"]),
    }


@scenario
def s_or_filter(r, text):
    st = r["status"]
    if not (st == "active" or num(r["age"]) > 60):
        return None
    return {"id": r["id"], "status": st, "label": r["name"] + " (" + r["city"].strip() + ")",
            "score2": fmt_num(round(num(r["score"]) * 1.5 + 2, 2), text, 2 if text else None)}


@scenario
def s_neq(r, text):
    if r["status"] == "banned" or num(r["score"]) < 20:
        return None
    return {"identifier": r["id"], "who": r["name"], "status": r["status"]}


@scenario
def s_nofilter(r, text):
    return {"id": r["id"], "name_and_city": r["name"] + " " + r["city"].strip(), "adult": r["active"]}


@scenario
def s_range(r, text):
    a = num(r["age"])
    if not (30 <= a < 50):
        return None
    return {"id": r["id"], "age": r["age"], "decade": fmt_num(a * 0.1, text) if not text else None} if False else \
        {"id": r["id"], "age": r["age"], "city": r["city"]}


@scenario
def s_multi(r, text):
    if r["status"] not in ("active", "pending"):
        return None
    return {"name": r["name"], "display_name": r["name"], "state": r["status"].upper(),
            "pts": str(r["score"]) + " pts", "f": fmt_num(round(num(r["score"]) * 1.8 + 32, 1), text, 1 if text else None)}


@scenario
def s_dnf(r, text):
    a = num(r["age"])
    ok = (r["active"] in (True, "true") and a < 30) or r["city"] == "Tokyo"
    if not ok:
        return None
    return {"id": r["id"], "domain": r["email"].split("@")[1].lower(), "user": r["email"].split("@")[0]}


def apply_truth(fn, rows, text):
    out = []
    for r in rows:
        o = fn(r, text)
        if o is not None:
            out.append(o)
    return out


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError("command failed: %s\n%s\n%s" % (cmd, p.stdout, p.stderr))
    return p


JS_DRIVER = r'''
const mod = require(process.argv[2]);
const DP = mod.DynamicPreprocessor;
const mode = process.argv[4];
const out = [];
if (mode === "full") {
  const p = DP({ buffer: 7 });
  for (const r of p(process.argv[3])) out.push(r);
} else if (mode === "partial") {
  const p = new DP({ buffer: 7, cache_dir: process.argv[5] });
  const k = parseInt(process.argv[6], 10);
  let i = 0;
  if (k > 0) for (const r of p(process.argv[3])) { out.push(r); if (++i >= k) break; }
} else {
  const p = new DP({ buffer: 7, cache_dir: process.argv[5] });
  for (const r of p(process.argv[3])) out.push(r);
}
process.stdout.write(JSON.stringify(out));
'''

PY_DRIVER = r'''
import sys, json, importlib
sys.path.insert(0, sys.argv[1])
mod = importlib.import_module(sys.argv[2])
mode = sys.argv[4]
out = []
if mode == "full":
    out = list(mod.DynamicPreprocessor(7)(sys.argv[3]))
elif mode == "partial":
    p = mod.DynamicPreprocessor(buffer=7, cache_dir=sys.argv[5])
    k = int(sys.argv[6])
    if k > 0:
        for r in p(sys.argv[3]):
            out.append(r)
            if len(out) >= k:
                break
else:
    out = list(mod.DynamicPreprocessor(buffer=7, cache_dir=sys.argv[5])(sys.argv[3]))
print(json.dumps(out))
'''


def normalize(rows, text):
    return json.loads(json.dumps(rows))


def main():
    tmp = tempfile.mkdtemp(prefix="dbtest_")
    failures = 0
    total = 0
    pyd = os.path.join(tmp, "driver.py")
    jsd = os.path.join(tmp, "driver.js")
    with open(pyd, "w") as f:
        f.write(PY_DRIVER)
    with open(jsd, "w") as f:
        f.write(JS_DRIVER)
    for si, fn in enumerate(SCENARIOS):
        for fmt in ("csv", "tsv", "jsonl", "json"):
            text = fmt in ("csv", "tsv")
            rng = random.Random(1000 + si)
            sample_rows = make_rows(rng, 200)
            test_rows = make_rows(random.Random(5000 + si), 400)
            if text:
                sample_rows = to_text_rows(sample_rows)
                test_rows = to_text_rows(test_rows)
            in_cols = list(sample_rows[0].keys())
            out_rows = apply_truth(fn, sample_rows, text)
            out_cols = list(out_rows[0].keys())
            d = os.path.join(tmp, "%s_%s" % (fn.__name__, fmt))
            sd = os.path.join(d, "sample")
            os.makedirs(sd)
            write_data(os.path.join(sd, "input." + fmt), fmt, sample_rows, in_cols)
            write_data(os.path.join(sd, "output." + fmt), fmt, out_rows, out_cols)
            data = os.path.join(d, "data." + fmt)
            write_data(data, fmt, test_rows, in_cols)
            expected = normalize(apply_truth(fn, test_rows, text), text)
            for lang in ("python", "javascript"):
                total += 1
                od = os.path.join(d, "out_" + lang)
                run([sys.executable, GEN, "dyna", "--output", od, "--sample", sd, "--" + lang])
                code1 = open(os.path.join(od, "dyna", "__init__.py" if lang == "python" else "index.js")).read()
                od2 = od + "_again"
                run([sys.executable, GEN, "dyna", "--output", od2, "--sample", sd, "--" + lang])
                code2 = open(os.path.join(od2, "dyna", "__init__.py" if lang == "python" else "index.js")).read()
                if lang == "python":
                    base = [sys.executable, pyd, od, "dyna", data]
                else:
                    base = ["node", jsd, os.path.join(od, "dyna"), data]
                got = json.loads(run(base + ["full"]).stdout)
                problems = []
                if code1 != code2:
                    problems.append("non-deterministic generation")
                if got != expected:
                    # find first diff
                    msg = "len got %d exp %d" % (len(got), len(expected))
                    for a, b in zip(got, expected):
                        if a != b:
                            msg += "; first diff got=%s exp=%s" % (a, b)
                            break
                    problems.append("mismatch: " + msg)
                # caching/resume
                cache = os.path.join(d, "cache_" + lang)
                k = 13
                part = json.loads(run(base + ["partial", cache, str(k)]).stdout)
                rest = json.loads(run(base + ["resume", cache]).stdout)
                if part + rest != got:
                    problems.append("resume mismatch: %d + %d vs %d" % (len(part), len(rest), len(got)))
                again = json.loads(run(base + ["resume", cache]).stdout)
                if again != got:
                    problems.append("complete-cache rerun mismatch (%d vs %d)" % (len(again), len(got)))
                part = json.loads(run(base + ["partial", cache, "5"]).stdout)
                rest = json.loads(run(base + ["resume", cache]).stdout)
                if part + rest != got:
                    problems.append("resume from complete cache mismatch")
                status = "OK " if not problems else "FAIL"
                if problems:
                    failures += 1
                print("%s %-14s %-5s %-10s %s" % (status, fn.__name__, fmt, lang, "; ".join(problems)))
    print("%d/%d passed" % (total - failures, total))
    if not os.environ.get("KEEP"):
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(tmp)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
