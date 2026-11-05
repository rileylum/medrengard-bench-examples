#!/usr/bin/env python3
"""EVE Online SDE industry recipe tool."""
import argparse
import bz2
import csv
import io
import math
import os
import re
import sys
from fractions import Fraction
from decimal import Decimal, InvalidOperation

csv.field_size_limit(sys.maxsize)

MANUFACTURING = 1
REACTIONS = 11
ACTIVITY_NAMES = {MANUFACTURING: "Manufacturing", REACTIONS: "Reactions"}


def read_csv(sde, name):
    path = os.path.join(sde, name)
    if not os.path.exists(path):
        alt = path[:-4] if path.endswith(".bz2") else path
        if os.path.exists(alt):
            with open(alt, encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f))
        return []
    with bz2.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def to_int(v):
    if v is None:
        return None
    v = v.strip()
    if v in ("", "None", "NULL", "null"):
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def to_dec(v):
    if v is None:
        return None
    v = v.strip()
    if v in ("", "None", "NULL", "null"):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def load_ship_volumes(sde):
    path = os.path.join(sde, "ship_volumes.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = None
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
        for line in text.splitlines():
            line = line.split("#", 1)[0].rstrip()
            if not line.strip() or ":" not in line:
                continue
            k, _, v = line.rpartition(":")
            data[k.strip().strip("'\"")] = v.strip().strip("'\"")
    out = {}
    for k, v in data.items():
        d = to_dec(str(v))
        if d is not None:
            out[str(k).strip()] = d
    return out


def fmt_plain(d):
    """Format a decimal without scientific notation, trailing zeros stripped."""
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if "." not in s:
        s += ".0"
    return s


DECRYPTORS = [
    ("None", 0, 0, 0, 0),
    ("Accelerant Decryptor", 20, 1, 2, 10),
    ("Attainment Decryptor", 80, 4, -1, 4),
    ("Augmentation Decryptor", -40, 9, -2, 2),
    ("Optimized Attainment Decryptor", 90, 2, 1, -2),
    ("Optimized Augmentation Decryptor", -10, 7, 2, 0),
    ("Parity Decryptor", 50, 3, 1, -2),
    ("Process Decryptor", 10, 0, 3, 6),
    ("Symmetry Decryptor", 0, 2, 1, 8),
]
RELICS = [("Intact", 0.26, 20), ("Malfunctioning", 0.21, 10), ("Wrecked", 0.14, 3)]
INV_ACTS = (8, 7)


def relic_info(name):
    for i, (prefix, chance, runs) in enumerate(RELICS):
        if name.startswith(prefix):
            return i, chance, runs
    return None


def load_invention_data(sde):
    # invention data: target blueprint -> list of (act, source, runs)
    inv_out = {}   # target blueprint -> list of (act, source, runs)
    for row in read_csv(sde, "industryActivityProducts.csv.bz2"):
        act = to_int(row.get("activityID"))
        if act not in INV_ACTS:
            continue
        src = to_int(row.get("typeID"))
        tgt = to_int(row.get("productTypeID"))
        inv_out.setdefault(tgt, []).append(
            (act, src, to_int(row.get("quantity")) or 0))
    probs = {}
    for row in read_csv(sde, "industryActivityProbabilities.csv.bz2"):
        act = to_int(row.get("activityID"))
        if act not in INV_ACTS:
            continue
        try:
            pv = float(row.get("probability"))
        except (TypeError, ValueError):
            continue
        probs[(to_int(row.get("typeID")), act, to_int(row.get("productTypeID")))] = pv
    imats = {}
    for row in read_csv(sde, "industryActivityMaterials.csv.bz2"):
        act = to_int(row.get("activityID"))
        if act not in INV_ACTS:
            continue
        key = (to_int(row.get("typeID")), act)
        imats.setdefault(key, []).append(
            (to_int(row.get("materialTypeID")), to_int(row.get("quantity")) or 0))
    iskills = {}
    for row in read_csv(sde, "industryActivitySkills.csv.bz2"):
        act = to_int(row.get("activityID"))
        if act not in INV_ACTS:
            continue
        key = (to_int(row.get("typeID")), act)
        sid = to_int(row.get("skillID"))
        lst = iskills.setdefault(key, [])
        if sid not in lst:
            lst.append(sid)

    return inv_out, probs, imats, iskills


def invention(args, name, sde, types, by_name, product_bps, bp_products,
              times, header, is_pub):
    skill = args.skill
    inv_out, probs, imats, iskills = load_invention_data(sde)
    ids = by_name.get(name, [])
    if not ids:
        print(f"Error: no published item named '{name}'", file=sys.stderr)
        return 1

    # Resolve to (product, product blueprint)
    target = None
    for tid in sorted(ids):
        # product interpretation
        bps = sorted({bp for act, bp, _ in product_bps.get(tid, [])
                      if act == MANUFACTURING},
                     key=lambda b: (0 if is_pub(b) else 1, b))
        for bp in bps:
            if bp in inv_out:
                target = (tid, bp)
                break
        if target:
            break
        # blueprint interpretation
        if tid in inv_out:
            prods = sorted([x for x in bp_products.get(tid, [])
                            if x[0] == MANUFACTURING],
                           key=lambda x: (0 if is_pub(x[1]) else 1, x[1]))
            if prods:
                target = (prods[0][1], tid)
                break
    if target is None:
        print(f"Error: no invention recipe for '{name}'", file=sys.stderr)
        return 1
    prod, tbp = target
    item, cname, gname, market, tech = header(prod)

    sources = inv_out[tbp]
    relic_sources = [s for s in sources
                     if s[1] in types and relic_info(types[s[1]]["name"])]
    if tech not in ("Tech II", "Tech III"):
        tech = "Tech III" if relic_sources and len(relic_sources) == len(sources) \
            else "Tech II"
    t3 = tech == "Tech III"

    def tname(t):
        return types[t]["name"] if t in types else str(t)

    if t3:
        srcs = relic_sources or sources
        srcs = sorted(srcs, key=lambda s: (
            (relic_info(tname(s[1])) or (9,))[0], tname(s[1]).lower(), s[1]))
    else:
        srcs = sorted(sources, key=lambda s: (
            0 if is_pub(s[1]) else 1, 0 if s[0] == 8 else 1, s[1]))
    act0, src0, runs0 = srcs[0]

    mats = imats.get((src0, act0), [])
    datacores = sorted({tname(m) for m, _ in mats}, key=lambda n: (n.lower(), n))
    qtys = [q for _, q in mats]
    per_run = qtys[0] if qtys else 0
    skill_ids = iskills.get((src0, act0), [])
    skill_names = sorted({tname(s) for s in skill_ids}, key=lambda n: (n.lower(), n))
    n_enc = sum(1 for s in skill_ids if "Encryption Methods" in tname(s))
    n_sci = len(skill_ids) - n_enc
    secs = times.get((src0, act0), 0)
    minutes = math.ceil(secs / 60) if secs else 0
    skill_factor = 1.0
    for _ in range(n_sci):
        skill_factor += skill / 30
    for _ in range(n_enc):
        skill_factor += skill / 40

    decs = sorted(DECRYPTORS, key=lambda d: (d[0] != "None", d[0].lower(), d[0]))

    def prob(base, mult):
        p = base * skill_factor * (1 + mult / 100)
        p = max(0.0, min(1.0, p))
        return f"{p * 100:.2f}"

    out = [
        f"ITEM: {item['name']} ({item['id']})",
        f"Group: {cname} > {gname}",
        f"Market Group: {market}",
        f"Tech Level: {tech}",
        "",
        "Invention:",
    ]
    if not t3:
        out.append(f"Base: {tname(src0)} ({src0})")
    out += [
        f"Datacores: {', '.join(datacores)}",
        f"Required Per Run: {per_run}",
        f"Skills: {', '.join(skill_names)}",
        f"Time: {minutes}",
        "Probabilities:",
    ]
    if t3:
        out.append("| Invented From                | Decryptor                       "
                   "| Probability | Runs | ME | TE |")
        out.append("|------------------------------|---------------------------------"
                   "|-------------|------|----|----|")
        for act, src, sruns in srcs:
            rn = tname(src)
            ri = relic_info(rn)
            if ri:
                base, bruns = ri[1], ri[2]
            else:
                base, bruns = probs.get((src, act, tbp), 0.0), sruns
            for dn, dm, dr, dme, dte in decs:
                out.append(f"| {rn:<28} | {dn:<32}| {prob(base, dm):<11} "
                           f"| {bruns + dr:<4} | {2 + dme:<2} | {4 + dte:<2} |")
    else:
        out.append("| Decryptor                       | Probability | Runs | ME | TE |")
        out.append("|---------------------------------|-------------|------|----|----|")
        base = probs.get((src0, act0, tbp), 0.0)
        for dn, dm, dr, dme, dte in decs:
            out.append(f"| {dn:<32}| {prob(base, dm):<11} "
                       f"| {runs0 + dr:<4} | {2 + dme:<2} | {4 + dte:<2} |")
    print("\n".join(out))
    return 0


def parse_build_string(s, by_name):
    """Return dict describing the build, or None."""
    s = " ".join(s.split())
    jobs = 1
    m = re.match(r"^(.*\S)\s+[xX](\d+)$", s)
    if m:
        s, jobs = m.group(1), int(m.group(2))
    dec_alt = "|".join(re.escape(d[0]) for d in DECRYPTORS)
    relic_alt = "|".join(re.escape(r[0]) for r in RELICS)

    def dec_canon(t):
        for d in DECRYPTORS:
            if d[0].lower() == t.lower():
                return d
        return None

    cands = []
    m = re.match(rf"^(.+?) (\d+) ({dec_alt}) ((?:{relic_alt})(?: .+)?)$", s, re.I)
    if m:
        cands.append(dict(kind="re", name=m.group(1), runs=int(m.group(2)),
                          decryptor=dec_canon(m.group(3)), relic=m.group(4)))
    m = re.match(rf"^(.+) (\d+) ({dec_alt})$", s, re.I)
    if m:
        cands.append(dict(kind="invention", name=m.group(1), runs=int(m.group(2)),
                          decryptor=dec_canon(m.group(3))))
    m = re.match(r"^(.+) (\d+) (\d+) (\d+)$", s)
    if m:
        cands.append(dict(kind="normal", name=m.group(1), runs=int(m.group(2)),
                          me=int(m.group(3)), te=int(m.group(4))))
    m = re.match(r"^(.+) (\d+)$", s)
    if m:
        cands.append(dict(kind="normal", name=m.group(1), runs=int(m.group(2)),
                          me=0, te=0))
    cands.append(dict(kind="normal", name=s, runs=1, me=0, te=0))
    for c in cands:
        c["jobs"] = jobs
    for c in cands:
        if c["name"] in by_name:
            return c
    lower = {k.lower(): k for k in by_name}
    for c in cands:
        if c["name"].lower() in lower:
            c["name"] = lower[c["name"].lower()]
            return c
    return cands[0]


def load_config(path):
    cfg = {}
    if path:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            import yaml
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                cfg = data
        except Exception:
            for line in text.splitlines():
                line = line.split("#", 1)[0]
                if ":" in line:
                    k, _, v = line.partition(":")
                    cfg[k.strip()] = v.strip()
    te_bonus = int(cfg.get("te_bonus", 0) or 0)
    skill = int(cfg.get("invention_skill", 5) or 5)
    return te_bonus, skill


def load_full_config(path):
    cfg = {}
    if path:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            import yaml
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                cfg = data
        except Exception:
            cfg = {}
    te_bonus, skill = load_config(path)
    split = cfg.get("split_days", 3)
    try:
        split = Fraction(Decimal(str(split if split is not None else 3)))
    except (InvalidOperation, ValueError):
        split = Fraction(3)
    split = min(Fraction(30), split)
    comp = cfg.get("component_bpo_defaults") or {}
    if not isinstance(comp, dict):
        comp = {}
    cme = comp.get("me", 10)
    cte = comp.get("te", 20)
    cme = int(cme) if cme is not None else 10
    cte = int(cte) if cte is not None else 20
    return dict(te_bonus=te_bonus, skill=skill, split_days=split,
                comp_me=cme, comp_te=cte)


def split_runs(n, dpr, split_days):
    """Split n runs into list of (runs, count)."""
    if n <= 1 or dpr <= 0 or n * dpr < split_days:
        return [(n, 1)]
    max_runs = math.ceil(split_days / dpr) - 1
    if max_runs < 1:
        max_runs = 1
    t = math.ceil(Fraction(n, max_runs))
    b = n // t
    x = n % t
    if b + 1 == max_runs and x > Fraction(t, 2) and n // (t + 1) + 1 < max_runs:
        t += 1
        b = n // t
        x = n % t
    out = []
    if x:
        out.append((b + 1, x))
    if t - x and b > 0:
        out.append((b, t - x))
    return out


def frac_of(x):
    return Fraction(Decimal(str(x)))


def plan(args, build, sde, types, by_name, product_bps, bp_products, times,
         mats, is_pub, groups, cats):
    te_bonus, skill = load_config(args.config)
    spec = parse_build_string(build, by_name)
    name = spec["name"]
    ids = by_name.get(name, [])
    if not ids:
        print(f"Error: no published item named '{name}'", file=sys.stderr)
        return 1
    runs, njobs = spec["runs"], spec["jobs"]

    def tname(t):
        return types[t]["name"] if t in types else str(t)

    buy = {}

    def add(tid, qty):
        if qty:
            buy[tid] = buy.get(tid, 0) + qty

    def pick_bp(tid, need_inv=None):
        cands = [c for c in product_bps.get(tid, [])
                 if need_inv is None or c[1] in need_inv]
        cands.sort(key=lambda c: (0 if is_pub(c[1]) else 1,
                                  0 if (c[1], c[0]) in mats else 1,
                                  0 if c[0] == MANUFACTURING else 1, c[1]))
        return cands[0] if cands else None

    if spec["kind"] == "normal":
        choice = None
        for tid in sorted(ids):
            choice = pick_bp(tid)
            if choice:
                break
        if choice is None:
            print(f"Error: no recipe for '{name}'", file=sys.stderr)
            return 1
        act, bp, _ = choice
        me, te = spec["me"], spec["te"]
    else:
        inv_out, probs, imats, iskills = load_invention_data(sde)
        choice = None
        for tid in sorted(ids):
            choice = pick_bp(tid, inv_out)
            if choice:
                break
        if choice is None:
            print(f"Error: no invention recipe for '{name}'", file=sys.stderr)
            return 1
        act, bp, _ = choice
        dname, dmult, druns, dme, dte = spec["decryptor"]
        sources = inv_out[bp]
        if spec["kind"] == "re":
            rel = spec["relic"].lower()
            src = None
            for s in sources:
                if tname(s[1]).lower() == rel:
                    src = s
                    break
            if src is None:
                matches = sorted(
                    [s for s in sources if tname(s[1]).lower().startswith(rel)],
                    key=lambda s: (tname(s[1]), s[1]))
                if matches:
                    src = matches[0]
            if src is None:
                print(f"Error: no relic '{spec['relic']}' for '{name}'",
                      file=sys.stderr)
                return 1
            ri = relic_info(tname(src[1]))
            base = frac_of(ri[1]) if ri else frac_of(
                probs.get((src[1], src[0], bp), 0))
            bp_runs = (ri[2] if ri else src[2]) + druns
        else:
            src = sorted(sources, key=lambda s: (
                0 if is_pub(s[1]) else 1, 0 if s[0] == 8 else 1, s[1]))[0]
            base = frac_of(probs.get((src[1], src[0], bp), 0))
            bp_runs = src[2] + druns
        sact, ssrc, _ = src
        skill_ids = iskills.get((ssrc, sact), [])
        factor = Fraction(1)
        for sid in skill_ids:
            if "Encryption Methods" in tname(sid):
                factor += Fraction(skill, 40)
            else:
                factor += Fraction(skill, 30)
        p = base * factor * (1 + Fraction(dmult, 100))
        p = max(Fraction(0), min(Fraction(1), p))
        bp_runs = max(1, bp_runs)
        n_bps = math.ceil(Fraction(runs * njobs, bp_runs))
        if p <= 0:
            print(f"Error: invention impossible for '{name}'", file=sys.stderr)
            return 1
        attempts = math.ceil(n_bps / p)
        for mid, q in imats.get((ssrc, sact), []):
            add(mid, q * attempts)
        if spec["kind"] == "re":
            add(ssrc, attempts)
        if dname != "None":
            dec_ids = by_name.get(dname, [])
            if dec_ids:
                add(sorted(dec_ids)[0], attempts)
        me, te = 2 + dme, 4 + dte

    if act != MANUFACTURING:
        me = 0
    stocks = load_item_list(getattr(args, "stocks", None), by_name)
    targets = load_item_list(getattr(args, "targets", None), by_name)
    if getattr(args, "build_all", False) or stocks or targets \
            or getattr(args, "stocks", None) or getattr(args, "targets", None):
        return plan_build_all(args, types, product_bps, bp_products, times,
                              mats, is_pub, groups, cats, by_name, buy, act,
                              bp, me, te, runs, njobs, tname,
                              stocks=stocks, targets=targets,
                              build_all=getattr(args, "build_all", False))
    for mid, q in mats.get((bp, act), []):
        per_job = max(runs, math.ceil(Fraction(q) * runs * (1 - Fraction(me, 100))))
        add(mid, per_job * njobs)

    secs = Fraction(times.get((bp, act), 0)) * runs * (1 - Fraction(te, 100)) \
        * (1 - Fraction(te_bonus, 100))
    days = round(float(secs / 86400), 2)

    ship_vols = load_ship_volumes(sde)

    def unit_volume(tid):
        t = types.get(tid)
        if not t:
            return Decimal(0)
        gname, cid = groups.get(t["group"], ("Unknown", None))
        if cats.get(cid) == "Ship" and gname in ship_vols:
            return ship_vols[gname]
        return t["volume"] if t["volume"] is not None else Decimal(0)

    os.makedirs(args.output, exist_ok=True)
    rows = []
    for tid, qty in buy.items():
        rows.append((tname(tid), qty, fmt_plain(unit_volume(tid) * qty)))
    rows.sort(key=lambda r: r[0])
    with open(os.path.join(args.output, "materials.csv"), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "to_buy", "buy_volume", "start_amount", "end_amount"])
        for n, q, v in rows:
            w.writerow([n, q, v, 0, 0])
    jobs = [(tname(bp), runs, days, njobs)]
    jobs.sort(key=lambda j: (j[0], j[1], j[3], j[2]))
    with open(os.path.join(args.output, "jobs.csv"), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "runs", "days", "count"])
        for n, r, d, c in jobs:
            w.writerow([n, r, d, c])
    return 0


CAPITAL_GROUPS = {"Carrier", "Dreadnought", "Force Auxiliary", "Freighter",
                  "Supercarrier", "Titan", "Capital Industrial Ship",
                  "Lancer Dreadnought", "Jump Freighter"}


def parse_qty(tok):
    t = tok.replace(",", "").replace("_", "")
    if re.fullmatch(r"[+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[+]?\d+\.\d*|[+]?\d*\.\d+", t):
        return int(Decimal(t))
    return None


def load_item_list(path, by_name):
    """Parse a '{Item Name} {Quantity} [anything]' file into {typeID: qty}."""
    out = {}
    if not path:
        return out
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    lower = {}
    for k in by_name:
        lower.setdefault(k.lower(), k)
    for line in lines:
        toks = line.split()
        if not toks or toks[0].startswith("#"):
            continue
        best = None
        fallback = None
        for i in range(1, len(toks)):
            q = parse_qty(toks[i])
            if q is None:
                continue
            nm = " ".join(toks[:i])
            if fallback is None:
                fallback = (nm, q)
            key = nm if nm in by_name else lower.get(nm.lower())
            if key is not None:
                best = (key, q)  # keep longest matching name
        if best is None:
            if fallback is not None:
                print(f"Warning: unknown item '{fallback[0]}' in {path}",
                      file=sys.stderr)
            continue
        key, q = best
        tid = sorted(by_name[key])[0]
        out[tid] = out.get(tid, 0) + q
    return out


def plan_build_all(args, types, product_bps, bp_products, times, mats, is_pub,
                   groups, cats, by_name, buy, act, bp, me, te, runs, njobs,
                   tname, stocks=None, targets=None, build_all=True):
    cfg = load_full_config(args.config)
    te_bonus = cfg["te_bonus"]
    split_days = cfg["split_days"]
    skill = cfg["skill"]
    sde = args.sde
    stocks = dict(stocks or {})
    targets = dict(targets or {})

    def recipe_for(tid):
        cands = [c for c in product_bps.get(tid, [])
                 if is_pub(c[1]) and c[0] in (MANUFACTURING, REACTIONS)]
        cands.sort(key=lambda c: (0 if (c[1], c[0]) in mats else 1,
                                  0 if c[0] == MANUFACTURING else 1, c[1]))
        return cands[0] if cands else None

    def is_t1_capital(tid):
        t = types.get(tid)
        if not t:
            return False
        gname, cid = groups.get(t["group"], ("Unknown", None))
        if cats.get(cid) != "Ship":
            return False
        if gname not in CAPITAL_GROUPS:
            return False
        # T1 if this ship is not itself invented (i.e. no T2 hull)
        return gname != "Jump Freighter"

    def day_per_run(b, a, te_):
        return Fraction(times.get((b, a), 0)) * (1 - Fraction(te_, 100)) \
            * (1 - Fraction(te_bonus, 100)) / 86400

    demand = {}
    produced = {}
    job_rows = {}

    # materials already required by the top-level job (invention inputs)
    for tid, q in buy.items():
        demand[tid] = demand.get(tid, 0) + q
    buy = {}

    def consume(b, a, r, count, me_):
        for mid, q in mats.get((b, a), []):
            per_job = max(r, math.ceil(Fraction(q) * r
                                       * (1 - Fraction(me_, 100))))
            demand[mid] = demand.get(mid, 0) + per_job * count

    def run_jobs(b, a, n, me_, te_, count=1):
        dpr = day_per_run(b, a, te_)
        for r, c in split_runs(n, dpr, split_days):
            key = (tname(b), r, round(float(dpr * r), 2))
            job_rows[key] = job_rows.get(key, 0) + c * count
            consume(b, a, r, c * count, me_)

    if build_all:
        run_jobs(bp, act, runs, me, te, njobs)
    else:
        secs = Fraction(times.get((bp, act), 0)) * runs \
            * (1 - Fraction(te, 100)) * (1 - Fraction(te_bonus, 100))
        key = (tname(bp), runs, round(float(secs / 86400), 2))
        job_rows[key] = job_rows.get(key, 0) + njobs
        consume(bp, act, runs, njobs, me)
    for a_, p_, q_ in bp_products.get(bp, []):
        if a_ == act and (p_ in stocks or p_ in targets):
            produced[p_] = produced.get(p_, 0) + q_ * runs * njobs

    # invention data (only needed for inventable stock targets)
    inv_cache = []

    def inv_data():
        if not inv_cache:
            inv_cache.append(load_invention_data(sde))
        return inv_cache[0]

    def invention_for(tid, rc):
        """Return (src_act, src_id, base_prob, bp_runs, is_relic) or None."""
        a, b, _ = rc
        if a != MANUFACTURING:
            return None
        inv_out, probs, imats, iskills = inv_data()
        sources = inv_out.get(b)
        if not sources:
            return None
        relic_srcs = [s for s in sources if relic_info(tname(s[1]))]
        if relic_srcs and len(relic_srcs) == len(sources):
            s = sorted(relic_srcs, key=lambda s: (
                relic_info(tname(s[1]))[0], tname(s[1]).lower(), s[1]))[0]
            ri = relic_info(tname(s[1]))
            return (s[0], s[1], frac_of(ri[1]), ri[2], True)
        s = sorted(sources, key=lambda s: (
            0 if is_pub(s[1]) else 1, 0 if s[0] == 8 else 1, s[1]))[0]
        return (s[0], s[1], frac_of(probs.get((s[1], s[0], b), 0)), s[2],
                False)

    def buildable_recipe(tid):
        if build_all or tid in targets:
            return recipe_for(tid)
        return None

    # discover buildable graph
    recipes = {}
    stack = list(demand) + list(targets)
    while stack:
        tid = stack.pop()
        if tid in recipes:
            continue
        rc = buildable_recipe(tid)
        recipes[tid] = rc
        if rc and build_all:
            stack.extend(m for m, _ in mats.get((rc[1], rc[0]), []))
    inv_info = {}
    for t in targets:
        rc = recipes.get(t)
        if rc:
            info = invention_for(t, rc)
            if info:
                inv_info[t] = info
    nodes = [t for t, rc in recipes.items() if rc]
    indeg = {t: 0 for t in nodes}
    for t in nodes:
        rc = recipes[t]
        for m in {m for m, _ in mats.get((rc[1], rc[0]), [])}:
            if m in indeg:
                indeg[m] += 1
    order = []
    ready = sorted([t for t in nodes if indeg[t] == 0])
    done = set()
    while len(order) < len(nodes):
        if not ready:
            rest = sorted(t for t in nodes if t not in done)
            ready = [rest[0]]
            indeg[rest[0]] = 0
        t = ready.pop(0)
        if t in done:
            continue
        done.add(t)
        order.append(t)
        rc = recipes[t]
        for m in sorted({m for m, _ in mats.get((rc[1], rc[0]), [])}):
            if m in indeg and m not in done:
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)

    for t in order:
        need = demand.get(t, 0) + targets.get(t, 0) - stocks.get(t, 0) \
            - produced.get(t, 0)
        if need <= 0:
            continue
        a, b, qty = recipes[t]
        qty = max(1, qty)
        n = math.ceil(Fraction(need, qty))
        if t in inv_info:
            sact, ssrc, base, bp_runs, is_relic = inv_info[t]
            _, _, imats, iskills = inv_data()
            factor = Fraction(1)
            for sid in iskills.get((ssrc, sact), []):
                if "Encryption Methods" in tname(sid):
                    factor += Fraction(skill, 40)
                else:
                    factor += Fraction(skill, 30)
            p = max(Fraction(0), min(Fraction(1), base * factor))
            bp_runs = max(1, bp_runs)
            if p > 0:
                n_bps = math.ceil(Fraction(n, bp_runs))
                attempts = math.ceil(n_bps / p)
                for mid, q in imats.get((ssrc, sact), []):
                    demand[mid] = demand.get(mid, 0) + q * attempts
                if is_relic:
                    demand[ssrc] = demand.get(ssrc, 0) + attempts
                produced[t] = produced.get(t, 0) + n * qty
                full, rem = divmod(n, bp_runs)
                if full:
                    run_jobs(b, a, bp_runs, 2, 4, full)
                if rem:
                    run_jobs(b, a, rem, 2, 4)
                continue
        if a != MANUFACTURING:
            cme, cte = 0, 0
        elif t in targets:
            cme, cte = cfg["comp_me"], cfg["comp_te"]
        elif is_t1_capital(t):
            cme, cte = 0, 0
        else:
            cme, cte = cfg["comp_me"], cfg["comp_te"]
        produced[t] = produced.get(t, 0) + n * qty
        run_jobs(b, a, n, cme, cte)

    for tid in set(demand) | set(targets):
        if recipes.get(tid) and tid in produced:
            continue
        q = demand.get(tid, 0) + targets.get(tid, 0) - stocks.get(tid, 0) \
            - produced.get(tid, 0)
        if q > 0:
            buy[tid] = q

    ship_vols = load_ship_volumes(sde)

    def unit_volume(tid):
        t = types.get(tid)
        if not t:
            return Decimal(0)
        gname, cid = groups.get(t["group"], ("Unknown", None))
        if cats.get(cid) == "Ship" and gname in ship_vols:
            return ship_vols[gname]
        return t["volume"] if t["volume"] is not None else Decimal(0)

    rows = []
    for tid in set(buy) | set(produced) | set(demand):
        q = buy.get(tid, 0)
        start = stocks.get(tid, 0)
        end = start + produced.get(tid, 0) + q - demand.get(tid, 0)
        if q == 0 and start == 0 and end == 0:
            continue
        if q == 0 and not produced.get(tid) and not demand.get(tid):
            continue
        rows.append((tname(tid), q, fmt_plain(unit_volume(tid) * q), start,
                     end))
    rows.sort(key=lambda r: r[0])
    os.makedirs(args.output, exist_ok=True)
    with open(os.path.join(args.output, "materials.csv"), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "to_buy", "buy_volume", "start_amount", "end_amount"])
        for r in rows:
            w.writerow(list(r))
    jobs = sorted(((n, r, d, c) for (n, r, d), c in job_rows.items()),
                  key=lambda j: (j[0], j[1], j[2], j[3]))
    with open(os.path.join(args.output, "jobs.csv"), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "runs", "days", "count"])
        for n, r, d, c in jobs:
            w.writerow([n, r, d, c])
    return 0


def _ranged_int(lo, hi, even=False):
    def conv(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"invalid integer: {v!r}")
        if n < lo or n > hi:
            raise argparse.ArgumentTypeError(
                f"must be in range [{lo},{hi}], got {n}")
        if even and n % 2:
            raise argparse.ArgumentTypeError(f"must be an even integer, got {n}")
        return n
    return conv


def mat_amount(q, me, runs=1):
    return math.ceil(max(1, Fraction(q) * (1 - Fraction(me, 100)) * runs))


def main(argv=None):
    p = argparse.ArgumentParser(description="EVE industry recipe planner")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recipe")
    r.add_argument("name", nargs="+")
    r.add_argument("--sde", required=True)
    r.add_argument("--material-efficiency", "-me", dest="me",
                   type=_ranged_int(0, 10), default=0)
    r.add_argument("--time-efficiency", "-te", dest="te",
                   type=_ranged_int(0, 20, even=True), default=0)
    r.add_argument("--display-waste", "-waste", dest="waste",
                   action="store_true")
    inv = sub.add_parser("invention")
    inv.add_argument("name", nargs="+")
    inv.add_argument("--sde", required=True)
    inv.add_argument("--skill", type=int, default=5, choices=[1, 2, 3, 4, 5])
    pl = sub.add_parser("plan")
    pl.add_argument("name", nargs="+")
    pl.add_argument("--config", default=None)
    pl.add_argument("--sde", required=True)
    pl.add_argument("--output", required=True)
    pl.add_argument("--build-all", dest="build_all", action="store_true")
    pl.add_argument("--stocks", default=None)
    pl.add_argument("--targets", default=None)
    args = p.parse_args(argv)
    name = " ".join(args.name)
    sde = args.sde

    types = {}
    by_name = {}
    for row in read_csv(sde, "invTypes.csv.bz2"):
        tid = to_int(row.get("typeID"))
        if tid is None:
            continue
        t = {
            "id": tid,
            "name": row.get("typeName", ""),
            "group": to_int(row.get("groupID")),
            "volume": to_dec(row.get("volume")),
            "published": to_int(row.get("published")) == 1,
            "market": to_int(row.get("marketGroupID")),
        }
        types[tid] = t
        if t["published"]:
            by_name.setdefault(t["name"], []).append(tid)

    groups = {}
    for row in read_csv(sde, "invGroups.csv.bz2"):
        gid = to_int(row.get("groupID"))
        groups[gid] = (row.get("groupName", ""), to_int(row.get("categoryID")))
    cats = {}
    for row in read_csv(sde, "invCategories.csv.bz2"):
        cats[to_int(row.get("categoryID"))] = row.get("categoryName", "")
    mgroups = {}
    for row in read_csv(sde, "invMarketGroups.csv.bz2"):
        mgroups[to_int(row.get("marketGroupID"))] = (
            row.get("marketGroupName", ""), to_int(row.get("parentGroupID")))
    metagroups = {}
    for row in read_csv(sde, "invMetaGroups.csv.bz2"):
        metagroups[to_int(row.get("metaGroupID"))] = row.get("metaGroupName", "")
    metatypes = {}
    for row in read_csv(sde, "invMetaTypes.csv.bz2"):
        metatypes[to_int(row.get("typeID"))] = to_int(row.get("metaGroupID"))

    # products: blueprint -> activity -> list of (product, qty)
    bp_products = {}
    product_bps = {}
    for row in read_csv(sde, "industryActivityProducts.csv.bz2"):
        act = to_int(row.get("activityID"))
        if act not in ACTIVITY_NAMES:
            continue
        bp = to_int(row.get("typeID"))
        prod = to_int(row.get("productTypeID"))
        qty = to_int(row.get("quantity")) or 0
        bp_products.setdefault(bp, []).append((act, prod, qty))
        product_bps.setdefault(prod, []).append((act, bp, qty))
    times = {}
    for row in read_csv(sde, "industryActivity.csv.bz2"):
        times[(to_int(row.get("typeID")), to_int(row.get("activityID")))] = \
            to_int(row.get("time")) or 0
    mats = {}
    for row in read_csv(sde, "industryActivityMaterials.csv.bz2"):
        key = (to_int(row.get("typeID")), to_int(row.get("activityID")))
        mats.setdefault(key, []).append(
            (to_int(row.get("materialTypeID")), to_int(row.get("quantity")) or 0))

    def is_pub(tid):
        return tid in types and types[tid]["published"]

    def header(prod):
        item = types.get(prod, {"id": prod, "name": str(prod), "group": None,
                                "volume": None, "market": None})
        gname, cid = groups.get(item["group"], ("Unknown", None))
        cname = cats.get(cid, "Unknown")
        path = []
        seen = set()
        mg = item["market"]
        while mg is not None and mg in mgroups and mg not in seen:
            seen.add(mg)
            path.append(mgroups[mg][0])
            mg = mgroups[mg][1]
        market = " > ".join(reversed(path)) if path else "None"
        mgname = metagroups.get(metatypes.get(prod), "")
        tech = {"Tech I": "Tech I", "Tech II": "Tech II", "Tech III": "Tech III",
                "Structure Tech I": "Tech I", "Structure Tech II": "Tech II"}.get(
            mgname, "Tech I")
        return item, cname, gname, market, tech

    if args.cmd == "plan":
        return plan(args, name, sde, types, by_name, product_bps, bp_products,
                    times, mats, is_pub, groups, cats)

    if args.cmd == "invention":
        return invention(args, name, sde, types, by_name, product_bps,
                         bp_products, times, header, is_pub)

    def score(bp, act):
        # prefer published blueprints, with materials, manufacturing first, lowest id
        return (0 if is_pub(bp) else 1, 0 if (bp, act) in mats else 1,
                0 if act == MANUFACTURING else 1, bp)

    def best_bp_for(prod):
        cands = product_bps.get(prod, [])
        if not cands:
            return None
        cands = sorted(cands, key=lambda c: score(c[1], c[0]))
        return cands[0]

    def buildable(tid):
        return any(is_pub(bp) for _, bp, _ in product_bps.get(tid, []))

    ids = by_name.get(name, [])
    if not ids:
        print(f"Error: no published item named '{name}'", file=sys.stderr)
        return 1

    choice = None  # (activity, blueprint, product, qty)
    # Try product interpretation first, then blueprint interpretation.
    for tid in sorted(ids):
        b = best_bp_for(tid)
        if b:
            choice = (b[0], b[1], tid, b[2])
            break
    if choice is None:
        for tid in sorted(ids):
            prods = bp_products.get(tid)
            if prods:
                prods = sorted(prods, key=lambda x: (
                    0 if is_pub(x[1]) else 1, 0 if x[0] == MANUFACTURING else 1, x[1]))
                act, prod, qty = prods[0]
                choice = (act, tid, prod, qty)
                break
    if choice is None:
        print(f"Error: no manufacturing or reaction recipe for '{name}'",
              file=sys.stderr)
        return 1

    act, bp, prod, qty = choice
    item = types.get(prod, {"id": prod, "name": str(prod), "group": None,
                            "volume": None, "market": None})
    gname, cid = groups.get(item["group"], ("Unknown", None))
    cname = cats.get(cid, "Unknown")

    # market path
    path = []
    seen = set()
    mg = item["market"]
    while mg is not None and mg in mgroups and mg not in seen:
        seen.add(mg)
        path.append(mgroups[mg][0])
        mg = mgroups[mg][1]
    market = " > ".join(reversed(path)) if path else "None"

    mgname = metagroups.get(metatypes.get(prod), "")
    tech = {"Tech I": "Tech I", "Tech II": "Tech II", "Tech III": "Tech III",
            "Structure Tech I": "Tech I", "Structure Tech II": "Tech II"}.get(
        mgname, "Tech I")

    ship_vols = load_ship_volumes(sde)
    if cname == "Ship" and gname in ship_vols:
        volume = format(ship_vols[gname].quantize(Decimal("0.01")), "f")
    else:
        v = item["volume"] if item["volume"] is not None else Decimal(0)
        volume = fmt_plain(v)

    secs = times.get((bp, act), 0)
    secs = Fraction(secs) * (1 - Fraction(args.te, 100))
    minutes = math.ceil(secs / 60) if secs else 0

    me = args.me if act == MANUFACTURING else 0
    rows = []
    for mid, mq in mats.get((bp, act), []):
        mname = types[mid]["name"] if mid in types else str(mid)
        need = mat_amount(mq, me)
        best = mat_amount(mq, 10) if act == MANUFACTURING else need
        rows.append((mname, need, need - best,
                     "Yes" if buildable(mid) else "No"))
    rows.sort(key=lambda x: (x[0].lower(), x[0]))

    out = [
        f"ITEM: {item['name']} ({item['id']})",
        f"Group: {cname} > {gname}",
        f"Market Group: {market}",
        f"Tech Level: {tech}",
        f"Volume: {volume}",
        "",
        "Recipe:",
        f"Activity: {ACTIVITY_NAMES[act]}",
        f"Output Quantity: {qty}",
        f"Run Time: {minutes}",
    ]
    if args.waste:
        out += ["| Item | Quantity | Waste | Buildable |",
                "|:-:|:---:|:---:|---:|"]
    else:
        out += ["| Item | Quantity | Buildable |",
                "|:-:|:---:|---:|"]
    for mname, mq, w, b in rows:
        if args.waste:
            out.append(f"| {mname} | {mq} | {w} | {b} |")
        else:
            out.append(f"| {mname} | {mq} | {b} |")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
