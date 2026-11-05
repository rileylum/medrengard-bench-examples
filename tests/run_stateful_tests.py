#!/usr/bin/env python3
"""End-to-end tests for stateful / neighbor-dependent transforms: build samples
from known sequence-level ground truths, generate modules, and check them on
fresh data (Python + JavaScript, all formats) with small buffers and many
stop/resume cycles sharing one cache_dir."""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_tests import GEN, to_text_rows, write_data, run  # noqa: E402

USERS = ["ann", "bob", "cyd", "dan", "eve"]


def make_rows(rng, n):
    rows = []
    user = rng.choice(USERS)
    level = 0.5
    for i in range(n):
        if rng.random() < 0.25:
            user = rng.choice(USERS)
        if rng.random() < 0.15:
            level = rng.choice([0.2, 0.5, 1.4, 2.2])
        st = rng.random()
        status = ("duplicate" if st < 0.12 else "start" if st < 0.2 else "stop" if st < 0.28
                  else "toggle" if st < 0.34 else "ok")
        rows.append({
            "t": i + 1,
            "user": user,
            "value": rng.randint(0, 50),
            "score": round(rng.uniform(0, 100), 1),
            "level": round(level + rng.uniform(-0.15, 0.15), 2),
            "status": status,
        })
    return rows


def num(v):
    return float(v) if isinstance(v, str) else v


def ival(v):
    return int(num(v))


def out_num(v, text):
    if text:
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return v


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


@scenario
def s_cumsum(rows, text):
    out, s = [], 0
    for r in rows:
        s += ival(r["value"])
        out.append({"t": r["t"], "value": r["value"], "cumulative": out_num(s, text)})
    return out


@scenario
def s_window(rows, text):
    out, w3, w5 = [], [], []
    for r in rows:
        w3 = (w3 + [num(r["score"])])[-3:]
        w5 = (w5 + [ival(r["value"])])[-5:]
        out.append({"t": r["t"], "avg3": out_num(round(sum(w3) / len(w3), 2), text),
                    "sum5": out_num(sum(w5), text)})
    return out


@scenario
def s_segment(rows, text):
    out, seg, prev = [], 1, None
    for r in rows:
        hi = num(r["level"]) >= 1.0
        if prev is not None and hi != prev:
            seg += 1
        prev = hi
        out.append({"t": r["t"], "level": r["level"], "segment_id": out_num(seg, text)})
    return out


@scenario
def s_session(rows, text):
    out, sid, prev = [], 0, None
    for r in rows:
        if r["user"] != prev:
            sid += 1
        prev = r["user"]
        out.append({"t": r["t"], "user": r["user"], "session_id": out_num(sid, text)})
    return out


@scenario
def s_fsm(rows, text):
    out, mode = [], "idle"
    for r in rows:
        if r["status"] == "start":
            mode = "running"
        elif r["status"] == "stop":
            mode = "idle"
        out.append({"t": r["t"], "status": r["status"], "mode": mode})
    return out


@scenario
def s_next_dup(rows, text):
    out = []
    for i, r in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if r["status"] == "duplicate" or (nxt is not None and nxt["status"] == "duplicate"):
            continue
        out.append({"t": r["t"], "status": r["status"]})
    return out


@scenario
def s_first_of_run(rows, text):
    out, n = [], 0
    for i, r in enumerate(rows):
        if i > 0 and rows[i - 1]["user"] == r["user"]:
            continue
        n += 1
        out.append({"t": r["t"], "user": r["user"], "n": out_num(n, text)})
    return out


@scenario
def s_drop_first_kept_cum(rows, text):
    out, s = [], 0
    for i, r in enumerate(rows):
        if i == 0 or rows[i - 1]["user"] != r["user"]:
            continue
        if ival(r["value"]) <= 10:
            continue
        s += ival(r["value"])
        out.append({"t": r["t"], "user": r["user"], "value": r["value"], "running": out_num(s, text)})
    return out


@scenario
def s_window_count_lag(rows, text):
    out, win, prev = [], [], None
    for r in rows:
        win = (win + [r["status"] == "ok"])[-4:]
        out.append({"t": r["t"], "ok4": out_num(sum(win), text),
                    "prev_value": prev if prev is not None else ("" if text else None)})
        prev = r["value"]
    return out


@scenario
def s_toggle(rows, text):
    out, phase = [], "A"
    for r in rows:
        if r["status"] == "toggle":
            phase = "B" if phase == "A" else "A"
        out.append({"t": r["t"], "phase": phase})
    return out


@scenario
def s_wide_window(rows, text):
    out, w = [], []
    for r in rows:
        w = (w + [ival(r["value"])])[-20:]
        out.append({"t": r["t"], "sum20": out_num(sum(w), text), "mx20": out_num(max(w), text)})
    return out


@scenario
def s_prefix_mean_affine(rows, text):
    out, s = [], 0
    for i, r in enumerate(rows):
        if r["status"] == "duplicate":
            continue
        s += ival(r["value"])
        out.append({"t": r["t"], "mean": out_num(round(s / (len(out) + 1), 3), text),
                    "rank": out_num(2 * (len(out) + 1) + 100, text)})
    return out


@scenario
def s_fsm_markers_dropped(rows, text):
    out, mode = [], "idle"
    for r in rows:
        if r["status"] == "start":
            mode = "running"
        elif r["status"] == "stop":
            mode = "idle"
        if r["status"] in ("start", "stop"):
            continue
        out.append({"t": r["t"], "mode": mode})
    return out


@scenario
def s_count_ok_filtered(rows, text):
    out, n, seg, prev = [], 0, 0, None
    for i, r in enumerate(rows):
        if r["status"] == "ok":
            n += 1
        if prev is None or r["user"] != prev:
            seg += 1
        prev = r["user"]
        if num(r["score"]) < 30:
            continue
        out.append({"t": r["t"], "user": r["user"].upper(), "oks": out_num(n, text),
                    "session": "s" if False else out_num(seg, text)})
    return out


@scenario
def s_keep_last_of_run(rows, text):
    out, w = [], []
    for i, r in enumerate(rows):
        w = (w + [num(r["score"])])[-6:]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt is not None and nxt["user"] == r["user"]:
            continue
        out.append({"t": r["t"], "user": r["user"], "mx": out_num(max(w), text),
                    "avg": out_num(round(sum(w) / len(w), 1), text)})
    return out


DRIVER_PY = r'''
import sys, json, importlib
sys.path.insert(0, sys.argv[1])
mod = importlib.import_module(sys.argv[2])
buf = int(sys.argv[4])
mode = sys.argv[5]
out = []
if mode == "full":
    out = list(mod.DynamicPreprocessor(buf)(sys.argv[3]))
else:
    p = mod.DynamicPreprocessor(buffer=buf, cache_dir=sys.argv[6])
    k = int(sys.argv[7])
    if k != 0:
        for r in p(sys.argv[3]):
            out.append(r)
            if len(out) == k:
                break
print(json.dumps(out))
'''

DRIVER_JS = r'''
const mod = require(process.argv[2]);
const DP = mod.DynamicPreprocessor;
const buf = parseInt(process.argv[4], 10);
const mode = process.argv[5];
const out = [];
if (mode === "full") {
  for (const r of DP({ buffer: buf })(process.argv[3])) out.push(r);
} else {
  const p = DP({ buffer: buf, cache_dir: process.argv[6] });
  const k = parseInt(process.argv[7], 10);
  if (k !== 0) for (const r of p(process.argv[3])) { out.push(r); if (out.length === k) break; }
}
process.stdout.write(JSON.stringify(out));
'''


def main():
    only = sys.argv[1:]
    tmp = tempfile.mkdtemp(prefix="dbst_")
    pyd = os.path.join(tmp, "d.py")
    jsd = os.path.join(tmp, "d.js")
    open(pyd, "w").write(DRIVER_PY)
    open(jsd, "w").write(DRIVER_JS)
    total = failures = 0
    for si, fn in enumerate(SCENARIOS):
        if only and fn.__name__ not in only:
            continue
        for fmt in ("csv", "tsv", "jsonl", "json"):
            text = fmt in ("csv", "tsv")
            sample = make_rows(random.Random(100 + si), 150)
            test = make_rows(random.Random(900 + si), 300)
            if text:
                sample, test = to_text_rows(sample), to_text_rows(test)
            in_cols = list(sample[0].keys())
            outs = fn(sample, text)
            d = os.path.join(tmp, "%s_%s" % (fn.__name__, fmt))
            sd = os.path.join(d, "sample")
            os.makedirs(sd)
            write_data(os.path.join(sd, "input." + fmt), fmt, sample, in_cols)
            write_data(os.path.join(sd, "output." + fmt), fmt, outs, list(outs[0].keys()))
            data = os.path.join(d, "data." + fmt)
            write_data(data, fmt, test, in_cols)
            expected = json.loads(json.dumps(fn(test, text)))
            for lang in ("python", "javascript"):
                total += 1
                od = os.path.join(d, "o_" + lang)
                p = run([sys.executable, GEN, "dyna", "--output", od, "--sample", sd, "--" + lang])
                problems = []
                if p.stderr.strip():
                    problems.append("gen: " + p.stderr.strip()[:200])
                main_file = os.path.join(od, "dyna", "__init__.py" if lang == "python" else "index.js")
                od2 = od + "_2"
                run([sys.executable, GEN, "dyna", "--output", od2, "--sample", sd, "--" + lang])
                if open(main_file).read() != open(main_file.replace(od, od2)).read():
                    problems.append("non-deterministic generation")
                base = ([sys.executable, pyd, od, "dyna", data] if lang == "python"
                        else ["node", jsd, os.path.join(od, "dyna"), data])
                for buf in (1, 3, 1000):
                    got = json.loads(run(base + [str(buf), "full"]).stdout)
                    if got != expected:
                        msg = "buf=%d len got %d exp %d" % (buf, len(got), len(expected))
                        for i, (a, b) in enumerate(zip(got, expected)):
                            if a != b:
                                msg += "; row %d got=%s exp=%s" % (i, a, b)
                                break
                        problems.append("mismatch: " + msg)
                        break
                rng = random.Random(si * 7 + len(fmt))
                for buf in (1, 2, 5):
                    cache = os.path.join(d, "cache_%s_%d" % (lang, buf))
                    acc = []
                    for _ in range(40):
                        k = rng.choice([1, 2, 3, 7, 11])
                        part = json.loads(run(base + [str(buf), "resume", cache, str(k)]).stdout)
                        acc += part
                        if len(part) < k:
                            break
                    rest = json.loads(run(base + [str(buf), "resume", cache, "-1"]).stdout)
                    if len(acc) < len(expected):
                        acc += rest
                    if acc != expected:
                        problems.append("resume mismatch buf=%d (%d vs %d)" % (buf, len(acc), len(expected)))
                        break
                status = "OK " if not problems else "FAIL"
                failures += bool(problems)
                print("%s %-22s %-5s %-10s %s" % (status, fn.__name__, fmt, lang, "; ".join(problems)),
                      flush=True)
    print("%d/%d passed" % (total - failures, total))
    if not os.environ.get("KEEP"):
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(tmp)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
