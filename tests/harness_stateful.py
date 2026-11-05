"""End-to-end checks for stateful / neighbour-aware transforms (part 2).

Each scenario is a reference implementation over a full row list.  A sample is
built from it, a module is generated in both languages, and the module is run
on fresh data: single pass with several buffer sizes, plus multi-run resumes
through a cache directory."""
import json, os, random, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEN = os.path.join(ROOT, "dynamic_buffer.py")
WORK = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dbstate"
ONLY = sys.argv[2].split(",") if len(sys.argv) > 2 else None
sys.path.insert(0, HERE)
from harness import write  # noqa: E402

STATUS = ["ok", "ok", "ok", "duplicate", "start", "stop"]


def gen_rows(n, seed):
    r = random.Random(seed)
    rows = []
    t = 0
    grp = "A"
    lvl_hi = False
    for i in range(n):
        t += r.choice([1, 2, 3, 5, 40]) if r.random() < 0.9 else 90
        if r.random() < 0.25:
            grp = r.choice([g for g in "ABC" if g != grp])
        if r.random() < 0.25:
            lvl_hi = not lvl_hi
        a = r.randint(1, 4)
        rows.append({
            "id": i + 1,
            "t": t,
            "value": r.randint(1, 50),
            "score": round(r.uniform(0, 100), 2),
            "level": round(r.uniform(1.1, 3.0) if lvl_hi else r.uniform(0.0, 0.9), 2),
            "status": r.choice(STATUS),
            "grp": grp,
            "a": a,
            "b": a if r.random() < 0.3 else r.randint(1, 4),
        })
    return rows


def project(rows, cols):
    return [{c: r[c] for c in cols} for r in rows]


def cumsum(rows):
    s = 0
    out = []
    for r in rows:
        s += r["value"]
        out.append(dict(r, cum=s))
    return out


def count_ok(rows):
    c = 0
    out = []
    for r in rows:
        c += r["status"] == "ok"
        out.append({"id": r["id"], "status": r["status"], "n_ok": c})
    return out


def prefix_mean(rows):
    s = 0
    out = []
    for i, r in enumerate(rows):
        s += r["value"]
        out.append({"id": r["id"], "value": r["value"], "avg": round(s / (i + 1), 2)})
    return out


def win(rows, col, w, fn):
    out = []
    for i in range(len(rows)):
        out.append(fn([x[col] for x in rows[max(0, i - w + 1):i + 1]]))
    return out


def win_sum5(rows):
    v = win(rows, "value", 5, sum)
    return [{"id": r["id"], "value": r["value"], "sum5": x} for r, x in zip(rows, v)]


def win_mean3(rows):
    v = win(rows, "score", 3, lambda xs: round(sum(xs) / len(xs), 2))
    return [{"id": r["id"], "score": r["score"], "avg3": x} for r, x in zip(rows, v)]


def win_mean20(rows):
    v = win(rows, "value", 20, lambda xs: sum(xs) / len(xs))
    return [{"id": r["id"], "m20": round(x, 3)} for r, x in zip(rows, v)]


def win_dupcount(rows):
    v = win(rows, "status", 4, lambda xs: sum(1 for x in xs if x == "duplicate"))
    return [{"id": r["id"], "status": r["status"], "dups4": x} for r, x in zip(rows, v)]


def seg_level(rows):
    seg, prev, out = 1, None, []
    for r in rows:
        hi = r["level"] >= 1.0
        if prev is not None and hi != prev:
            seg += 1
        prev = hi
        out.append({"id": r["id"], "level": r["level"], "segment_id": seg})
    return out


def session(rows):
    s, pt, out = 1, None, []
    for r in rows:
        if pt is not None and r["t"] - pt > 30:
            s += 1
        pt = r["t"]
        out.append({"t": r["t"], "session_id": "s" + str(s)})
    return out


def grp_change(rows):
    s, pg, out = 0, None, []
    for r in rows:
        if r["grp"] != pg:
            s += 1
        pg = r["grp"]
        out.append({"id": r["id"], "grp": r["grp"], "run": s})
    return out


def phase(rows):
    st, out = "idle", []
    for r in rows:
        if r["status"] == "start":
            st = "running"
        elif r["status"] == "stop":
            st = "idle"
        out.append({"id": r["id"], "status": r["status"], "phase": st})
    return out


def drop_before_dup(rows):
    out = []
    for i, r in enumerate(rows):
        if r["status"] == "duplicate":
            continue
        if i + 1 < len(rows) and rows[i + 1]["status"] == "duplicate":
            continue
        out.append({"id": r["id"], "status": r["status"]})
    return out


def first_of_run(rows):
    return [{"id": r["id"], "grp": r["grp"]} for i, r in enumerate(rows)
            if i == 0 or rows[i - 1]["grp"] != r["grp"]]


def last_of_run(rows):
    return [{"id": r["id"], "grp": r["grp"], "value": r["value"]} for i, r in enumerate(rows)
            if i + 1 == len(rows) or rows[i + 1]["grp"] != r["grp"]]


def drop_first_cum(rows):
    out, s = [], 0
    for i, r in enumerate(rows):
        s += r["value"]
        if i == 0 or rows[i - 1]["grp"] != r["grp"]:
            continue
        out.append({"id": r["id"], "grp": r["grp"].lower(), "cum": s})
    return out


def kept_cum(rows):
    out, s = [], 0
    for r in rows:
        if r["value"] > 20:
            s += r["value"]
            out.append({"key": "R" + str(r["id"]), "v2": r["value"] * 2, "cum_kept": s})
    return out


def neq_ab(rows):
    return [{"id": r["id"], "a": r["a"], "b": r["b"]} for r in rows if r["a"] != r["b"]]


def toggle(rows):
    st, prev, out = "off", None, []
    for r in rows:
        if prev == "ok" and r["status"] == "duplicate":
            st = "on" if st == "off" else "off"
        prev = r["status"]
        out.append({"id": r["id"], "status": r["status"], "mode": st})
    return out


def run_max(rows):
    m, out = None, []
    for r in rows:
        m = r["score"] if m is None else max(m, r["score"])
        out.append({"id": r["id"], "best": m})
    return out


def dedupe_value_lag(rows):
    # drop a row whose status equals the previous row's status
    return [{"id": r["id"], "status": r["status"]} for i, r in enumerate(rows)
            if i == 0 or rows[i - 1]["status"] != r["status"]]


SCEN = {
    "cumsum": lambda rs: project(cumsum(rs), ["t", "value", "cum"]),
    "count_ok": count_ok,
    "prefix_mean": prefix_mean,
    "win_sum5": win_sum5,
    "win_mean3": win_mean3,
    "win_mean20": win_mean20,
    "win_dupcount": win_dupcount,
    "seg_level": seg_level,
    "session": session,
    "grp_change": grp_change,
    "phase": phase,
    "drop_before_dup": drop_before_dup,
    "first_of_run": first_of_run,
    "last_of_run": last_of_run,
    "drop_first_cum": drop_first_cum,
    "kept_cum": kept_cum,
    "neq_ab": neq_ab,
    "toggle": toggle,
    "run_max": run_max,
    "dedupe_status": dedupe_value_lag,
}


def cum_score(rows):
    s, out = 0, []
    for r in rows:
        s += r["score"]
        out.append({"id": r["id"], "cs": s})
    return out


def cum_score_r2(rows):
    s, out = 0, []
    for r in rows:
        s += r["score"]
        out.append({"id": r["id"], "cs": round(s, 2)})
    return out


def strict_mean4(rows):
    out = []
    for i, r in enumerate(rows):
        xs = [x["value"] for x in rows[max(0, i - 3):i + 1]]
        out.append({"id": r["id"], "m4": round(sum(xs) / 4, 2) if len(xs) == 4 else None})
    return out


def kept_window(rows):
    kept = [r for r in rows if r["status"] != "duplicate"]
    v = win(kept, "value", 6, sum)
    return [{"id": r["id"], "s6": x} for r, x in zip(kept, v)]


def mode_thresh(rows):
    st, out = "mid", []
    for r in rows:
        if r["level"] >= 2.0:
            st = "high"
        elif r["level"] < 0.5:
            st = "low"
        out.append({"id": r["id"], "level": r["level"], "band": st})
    return out


def flag_started(rows):
    on, out = False, []
    for r in rows:
        if r["status"] == "start":
            on = True
        elif r["status"] == "stop":
            on = False
        out.append({"id": r["id"], "active": on})
    return out


def next_dup_only(rows):
    return [{"id": r["id"], "status": r["status"], "value": r["value"] + 1} for i, r in enumerate(rows)
            if not (i + 1 < len(rows) and rows[i + 1]["status"] == "duplicate")]


def neighbor_cum(rows):
    out, s = [], 0
    for i, r in enumerate(rows):
        s += r["value"]
        if i + 1 < len(rows) and rows[i + 1]["grp"] == r["grp"]:
            continue
        out.append({"id": r["id"], "grp": r["grp"], "total": s})
    return out


def win_count_hi(rows):
    v = win(rows, "level", 8, lambda xs: sum(1 for x in xs if x >= 1.0))
    return [{"id": r["id"], "hi8": x} for r, x in zip(rows, v)]


SCEN.update({
    "cum_score": cum_score,
    "cum_score_r2": cum_score_r2,
    "strict_mean4": strict_mean4,
    "kept_window": kept_window,
    "mode_thresh": mode_thresh,
    "flag_started": flag_started,
    "next_dup_only": next_dup_only,
    "neighbor_cum": neighbor_cum,
    "win_count_hi": win_count_hi,
})

PY = r'''
import json, sys
from dyna import DynamicPreprocessor as D
p, c = sys.argv[1], sys.argv[2]
res = {}
for b in (1, 3, 8, 1000):
    res["full%d" % b] = list(D(buffer=b)(p))
got = []
plan = [(5, 7), (2, 1), (8, 0), (3, 13), (1, 2), (4, 30)]
k = 0
while True:
    b, take = plan[k % len(plan)]
    k += 1
    it = D(buffer=b, cache_dir=c)(p)
    n = 0
    stopped = False
    for r in it:
        got.append(r)
        n += 1
        if n >= take and take > 0:
            stopped = True
            break
    if take == 0 and n == 0:
        pass
    if not stopped:
        break
    if k > 500:
        break
res["resume"] = got
res["again"] = list(D(buffer=2, cache_dir=c)(p))
print(json.dumps(res))
'''

JS = r'''
const {DynamicPreprocessor: D} = require(process.argv[3]);
const p = process.argv[1], c = process.argv[2];
const res = {};
for (const b of [1, 3, 8, 1000]) res["full" + b] = [...D({buffer: b})(p)];
const got = [];
const plan = [[5, 7], [2, 1], [8, 0], [3, 13], [1, 2], [4, 30]];
let k = 0;
while (true) {
  const [b, take] = plan[k % plan.length];
  k++;
  let n = 0, stopped = false;
  for (const r of D({buffer: b, cache_dir: c})(p)) {
    got.push(r); n++;
    if (take > 0 && n >= take) { stopped = true; break; }
  }
  if (!stopped || k > 500) break;
}
res.resume = got;
res.again = [...D({buffer: 2, cache_dir: c})(p)];
console.log(JSON.stringify(res));
'''


def main():
    sys.path.insert(0, ROOT)
    import dynamic_buffer as db
    rt = db.rt()
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    fails = 0
    for fmt in ("csv", "tsv", "jsonl", "json"):
        for name, fn in SCEN.items():
            if ONLY and name not in ONLY:
                continue
            base = os.path.join(WORK, fmt, name)
            sd = os.path.join(base, "sample")
            os.makedirs(sd)
            sin = gen_rows(40, len(name) * 13 + 5)
            cols = list(sin[0].keys())
            sout = fn(sin)
            ocols = list(sout[0].keys())
            write(os.path.join(sd, "input." + fmt), fmt, sin, cols)
            write(os.path.join(sd, "output." + fmt), fmt, sout, ocols)
            tin = gen_rows(300, 99)
            tout = fn(tin)
            tp = os.path.join(base, "data." + fmt)
            ep = os.path.join(base, "expected." + fmt)
            write(tp, fmt, tin, cols)
            write(ep, fmt, tout, ocols)
            exp = [r for r, _ in rt["read_rows"](ep, fmt, None)]
            for lang in ("python", "javascript"):
                od = os.path.join(base, "gen_" + lang)
                subprocess.run([sys.executable, GEN, "dyna", "--output", od, "--sample", sd, "--" + lang], check=True)
                cd = os.path.join(base, "cache_" + lang)
                if lang == "python":
                    res = subprocess.run([sys.executable, "-c", PY, tp, cd], env=dict(os.environ, PYTHONPATH=od),
                                         capture_output=True, text=True)
                else:
                    res = subprocess.run(["node", "-e", JS, tp, cd, os.path.join(od, "dyna")],
                                         capture_output=True, text=True)
                if res.returncode != 0:
                    print("FAIL", fmt, name, lang, res.stderr[-2000:])
                    fails += 1
                    continue
                got = json.loads(res.stdout)
                bad = [k for k, v in got.items() if v != exp]
                if bad:
                    fails += 1
                    print("FAIL", fmt, name, lang, bad)
                    g = got[bad[0]]
                    for x, y in zip(g, exp):
                        if x != y:
                            print("   got", x, "\n   exp", y)
                            break
                    print("   lens", len(g), len(exp))
                else:
                    print("ok  ", fmt, name, lang)
    print("FAILS", fails)


if __name__ == "__main__":
    main()
