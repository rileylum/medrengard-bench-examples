"""End-to-end checks: build samples from known transformations, generate modules
in both languages, and compare them on fresh data (plus cache/resume checks)."""
import json, os, random, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEN = os.path.join(ROOT, "dynamic_buffer.py")
WORK = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dbtest"

CITIES = ["Paris", "Rome", "Berlin", "Madrid", "Oslo"]
FIRST = ["alice", "Bob", "CAROL", "dave", "Eve", "frank", "Grace", "heidi", "Ivan", "judy"]
LAST = ["Smith", "jones", "Brown", "LEE", "Khan"]
DOMS = ["example.com", "mail.org", "corp.net"]


def gen_rows(n, seed):
    r = random.Random(seed)
    rows = []
    for i in range(n):
        f, l = r.choice(FIRST), r.choice(LAST)
        rows.append({
            "id": i + 1,
            "name": (" " if r.random() < 0.3 else "") + f + " " + l + (" " if r.random() < 0.3 else ""),
            "age": r.randint(10, 80),
            "score": round(r.uniform(0, 100), 2),
            "active": r.random() < 0.6,
            "city": r.choice(CITIES),
            "email": f.lower() + "." + l.lower() + "@" + r.choice(DOMS),
        })
    return rows


def cell(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def write(path, fmt, rows, cols):
    with open(path, "w", encoding="utf-8") as fh:
        if fmt in ("csv", "tsv"):
            d = "," if fmt == "csv" else "\t"
            fh.write(d.join(cols) + "\n")
            for r in rows:
                fh.write(d.join(cell(r[c]) for c in cols) + "\n")
        elif fmt == "jsonl":
            for r in rows:
                fh.write(json.dumps({c: r[c] for c in cols}) + "\n")
        else:
            json.dump([{c: r[c] for c in cols} for r in rows], fh, indent=2)


def b(r):  # typed bool regardless of fmt
    return r["active"]


SCENARIOS = {
    "example": (lambda r: r["active"] and r["age"] >= 21,
                lambda r: {"id": r["id"], "name": r["name"]}),
    "rename_const": (lambda r: r["score"] > 50,
                     lambda r: {"id": r["id"], "full_name": r["name"], "source": "web", "score": r["score"]}),
    "case_linear": (lambda r: r["city"] in ("Paris", "Rome"),
                    lambda r: {"city": r["city"].upper(), "name": r["name"].strip().lower(),
                               "age": r["age"] * 2 + 1}),
    "split_prefix_concat": (lambda r: r["age"] < 40 and r["active"],
                            lambda r: {"domain": r["email"].split("@")[-1], "key": "ID-" + str(r["id"]),
                                       "label": r["name"].strip() + " - " + r["city"]}),
    "float_linear": (lambda r: True,
                     lambda r: {"id": r["id"], "price": round(r["score"] * 1.1, 2)}),
    "copy_or": (lambda r: r["city"] == "Oslo" or r["score"] >= 90,
                lambda r: {"id": r["id"], "id2": r["id"], "city": r["city"], "first": r["name"].split()[0].title()}),
    "const_num_tsfx": (lambda r: r["age"] < 50 or r["city"] == "Rome",
                       lambda r: {"version": 2, "user": r["email"].split("@")[0] + "_x", "name": r["name"].strip().title(),
                                  "active": r["active"]}),
    "three_or": (lambda r: r["city"] == "Rome" or r["city"] == "Oslo" or r["age"] >= 70,
                 lambda r: {"city_l": r["city"].lower(), "s": r["score"]}),
    "neg_linear": (lambda r: not r["active"],
                   lambda r: {"id": r["id"], "neg": 100 - r["age"], "half": r["age"] / 2}),
    "neq_filter": (lambda r: r["city"] != "Berlin",
                   lambda r: {"email": r["email"].upper(), "age_months": r["age"] * 12}),
}


def expected(fmt, rows, f, t):
    out = []
    for r in rows:
        if f(r):
            out.append(t(r))
    return out


def norm_rows(path, fmt, rt):
    return [r for r, _ in rt["read_rows"](path, fmt, None)]


def main():
    sys.path.insert(0, ROOT)
    import dynamic_buffer as db
    rt = db.rt()
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    fails = 0
    for fmt in ("csv", "tsv", "jsonl", "json"):
        for name, (f, t) in SCENARIOS.items():
            base = os.path.join(WORK, fmt, name)
            sd = os.path.join(base, "sample")
            os.makedirs(sd)
            sin = gen_rows(40, hash(name) % 1000 if False else len(name) * 7 + 1)
            cols = list(sin[0].keys())
            sout = expected(fmt, sin, f, t)
            ocols = list(t(sin[0]).keys())
            write(os.path.join(sd, "input." + fmt), fmt, sin, cols)
            write(os.path.join(sd, "output." + fmt), fmt, sout, ocols)
            tin = gen_rows(400, 99)
            tout = expected(fmt, tin, f, t)
            tp = os.path.join(base, "data." + fmt)
            ep = os.path.join(base, "expected." + fmt)
            write(tp, fmt, tin, cols)
            write(ep, fmt, tout, ocols)
            exp = norm_rows(ep, fmt, rt)
            for lang in ("python", "javascript"):
                od = os.path.join(base, "gen_" + lang)
                subprocess.run([sys.executable, GEN, "dyna", "--output", od, "--sample", sd, "--" + lang], check=True)
                cd = os.path.join(base, "cache_" + lang)
                if lang == "python":
                    script = ("import json,sys\nfrom dyna import DynamicPreprocessor as D\n"
                              "p=sys.argv[1];c=sys.argv[2]\n"
                              "full=list(D(buffer=7)(p))\n"
                              "a=[]\n"
                              "for i,r in enumerate(D(5,cache_dir=c)(p)):\n a.append(r)\n if i==12: break\n"
                              "b=list(D(buffer=3,cache_dir=c)(p))\n"
                              "again=list(D(buffer=3,cache_dir=c)(p))\n"
                              "print(json.dumps({'full':full,'resume':a+b,'again':again}))\n")
                    res = subprocess.run([sys.executable, "-c", script, tp, cd], env=dict(os.environ, PYTHONPATH=od),
                                         capture_output=True, text=True)
                else:
                    script = ("const {DynamicPreprocessor:D}=require(process.argv[3]);\n"
                              "const p=process.argv[1],c=process.argv[2];\n"
                              "const full=[...D({buffer:7})(p)];\n"
                              "const a=[];let i=0;for(const r of new D({buffer:5,cache_dir:c})(p)){a.push(r);if(i++==12)break;}\n"
                              "const b=[...D({buffer:3,cache_dir:c})(p)];\n"
                              "const again=[...D({buffer:3,cache_dir:c})(p)];\n"
                              "console.log(JSON.stringify({full,resume:a.concat(b),again}));\n")
                    res = subprocess.run(["node", "-e", script, tp, cd, os.path.join(od, "dyna")],
                                         capture_output=True, text=True)
                if res.returncode != 0:
                    print("FAIL", fmt, name, lang, res.stderr[-2000:])
                    fails += 1
                    continue
                got = json.loads(res.stdout)
                ok = got["full"] == exp and got["resume"] == exp and got["again"] == exp
                if not ok:
                    fails += 1
                    print("FAIL", fmt, name, lang, "full", got["full"] == exp, "resume", got["resume"] == exp,
                          "again", got["again"] == exp)
                    for x, y in zip(got["full"], exp):
                        if x != y:
                            print("   got", x, "\n   exp", y)
                            break
                    print("   lens", len(got["full"]), len(exp))
                    spec = open(os.path.join(od, "dyna", "__init__.py" if lang == "python" else "index.js")).read()
                else:
                    print("ok  ", fmt, name, lang)
    print("FAILS", fails)


if __name__ == "__main__":
    main()
