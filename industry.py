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
