#!/usr/bin/env python3
"""EVE Online industry planner: recipe reports from the SDE."""
import argparse
import bz2
import csv
import math
import os
import sys
from decimal import Decimal, InvalidOperation

MANUFACTURING = 1
REACTIONS = 11
ACTIVITY_NAMES = {MANUFACTURING: "Manufacturing", REACTIONS: "Reactions"}
MAX_ME, MAX_TE = 10, 20
TECH_BY_META = {1: "Tech I", 2: "Tech II", 14: "Tech III",
                54: "Tech I", 53: "Tech II"}


def read_csv(sde, name):
    path = os.path.join(sde, name)
    if os.path.exists(path):
        fh = bz2.open(path, "rt", encoding="utf-8", newline="")
    else:
        plain = path[:-4] if path.endswith(".bz2") else path
        fh = open(plain, "r", encoding="utf-8", newline="")
    with fh:
        yield from csv.DictReader(fh)


def to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def load_ship_volumes(sde):
    path = os.path.join(sde, "ship_volumes.yaml")
    vols = {}
    if not os.path.exists(path):
        return vols
    try:
        import yaml  # optional
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return {str(k): v for k, v in data.items()}
    except ImportError:
        pass
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].rstrip()
            if not line.strip() or ":" not in line:
                continue
            key, _, val = line.rpartition(":")
            key = key.strip().strip("'\"")
            val = val.strip().strip("'\"")
            if val:
                vols[key] = val
    return vols


def fmt_volume(value):
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    s = format(d, "f")
    if "." not in s:
        s += ".00"
    whole, frac = s.split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{whole}.{frac}"


class SDE:
    def __init__(self, path):
        self.path = path
        self.types = {}
        self.name_to_id = {}
        for r in read_csv(path, "invTypes.csv.bz2"):
            tid = to_int(r["typeID"])
            if tid is None:
                continue
            self.types[tid] = r
            if r.get("published") == "1":
                self.name_to_id.setdefault(r["typeName"], tid)
        self.groups = {to_int(r["groupID"]): r
                       for r in read_csv(path, "invGroups.csv.bz2")}
        self.categories = {to_int(r["categoryID"]): r
                           for r in read_csv(path, "invCategories.csv.bz2")}
        self.market_groups = {to_int(r["marketGroupID"]): r
                              for r in read_csv(path, "invMarketGroups.csv.bz2")}
        self.meta = {to_int(r["typeID"]): to_int(r["metaGroupID"])
                     for r in read_csv(path, "invMetaTypes.csv.bz2")}
        self.meta_names = {to_int(r["metaGroupID"]): r["metaGroupName"]
                           for r in read_csv(path, "invMetaGroups.csv.bz2")}
        self.times = {}
        for r in read_csv(path, "industryActivity.csv.bz2"):
            self.times[(to_int(r["typeID"]), to_int(r["activityID"]))] = to_int(r["time"], 0)
        # blueprint -> (activity, product, qty); product -> [blueprints]
        self.bp_products = {}
        self.product_bps = {}
        for r in read_csv(path, "industryActivityProducts.csv.bz2"):
            act = to_int(r["activityID"])
            if act not in ACTIVITY_NAMES:
                continue
            bp, prod, qty = to_int(r["typeID"]), to_int(r["productTypeID"]), to_int(r["quantity"], 1)
            self.bp_products.setdefault(bp, []).append((act, prod, qty))
            self.product_bps.setdefault(prod, []).append((bp, act, qty))
        self.materials = {}
        for r in read_csv(path, "industryActivityMaterials.csv.bz2"):
            key = (to_int(r["typeID"]), to_int(r["activityID"]))
            self.materials.setdefault(key, []).append(
                (to_int(r["materialTypeID"]), to_int(r["quantity"], 0)))
        self.ship_volumes = load_ship_volumes(path)

    def published(self, tid):
        t = self.types.get(tid)
        return t is not None and t.get("published") == "1"

    def _bp_rank(self, bp, act):
        return (0 if self.published(bp) else 1,
                0 if act == MANUFACTURING else 1, bp)

    def recipe_for_product(self, prod):
        cands = self.product_bps.get(prod, [])
        if not cands:
            return None
        bp, act, qty = min(cands, key=lambda c: self._bp_rank(c[0], c[1]))
        return bp, act, prod, qty

    def recipe_for_blueprint(self, bp):
        outs = self.bp_products.get(bp, [])
        if not outs:
            return None
        act, prod, qty = min(outs, key=lambda o: (0 if o[0] == MANUFACTURING else 1, o[1]))
        return bp, act, prod, qty

    def is_buildable(self, tid):
        return any(self.published(bp) for bp, _a, _q in self.product_bps.get(tid, []))

    def group_line(self, tid):
        t = self.types[tid]
        g = self.groups.get(to_int(t["groupID"]), {})
        c = self.categories.get(to_int(g.get("categoryID")), {})
        return f"{c.get('categoryName', 'Unknown')} > {g.get('groupName', 'Unknown')}"

    def market_path(self, tid):
        mg = to_int(self.types[tid].get("marketGroupID"))
        parts, seen = [], set()
        while mg is not None and mg in self.market_groups and mg not in seen:
            seen.add(mg)
            row = self.market_groups[mg]
            parts.append(row["marketGroupName"])
            mg = to_int(row.get("parentGroupID"))
        return " > ".join(reversed(parts)) if parts else "None"

    def tech_level(self, tid):
        mgid = self.meta.get(tid)
        if mgid in TECH_BY_META:
            return TECH_BY_META[mgid]
        name = self.meta_names.get(mgid)
        if name in ("Tech I", "Tech II", "Tech III"):
            return name
        return "Tech I"

    def volume(self, tid):
        t = self.types[tid]
        g = self.groups.get(to_int(t["groupID"]), {})
        c = self.categories.get(to_int(g.get("categoryID")), {})
        if c.get("categoryName") == "Ship":
            gname = g.get("groupName")
            if gname in self.ship_volumes:
                return fmt_volume(self.ship_volumes[gname])
        return fmt_volume(t.get("volume") or "0")


def render_table(rows, headers=("Item", "Quantity", "Buildable")):
    n = len(headers)
    widths = [max([len(headers[i])] + [len(r[i]) for r in rows]) for i in range(n)]
    def line(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    sep = "|" + "|".join(":" + "-" * w + ":" for w in widths[:-1]) + "|"
    sep += "-" * (widths[-1] + 1) + ":|"
    return [line(headers), sep] + [line(r) for r in rows]


def me_amount(quantity, me, runs=1):
    return math.ceil(max(1, quantity * (1 - me / 100) * runs))


def te_seconds(seconds, te):
    return math.ceil(seconds * (1 - te / 100))


def cmd_recipe(args):
    sde = SDE(args.sde)
    name = args.name
    tid = sde.name_to_id.get(name)
    if tid is None:
        print(f"Error: no published item named '{name}'", file=sys.stderr)
        return 1
    recipe = sde.recipe_for_blueprint(tid) if tid in sde.bp_products else None
    if recipe is None:
        recipe = sde.recipe_for_product(tid)
    if recipe is None:
        print(f"Error: '{name}' has no manufacturing or reaction recipe", file=sys.stderr)
        return 1
    bp, act, prod, qty = recipe
    if prod not in sde.types:
        print(f"Error: product {prod} not found", file=sys.stderr)
        return 1
    me = args.material_efficiency if act == MANUFACTURING else 0
    mats = []
    for mid, mq in sde.materials.get((bp, act), []):
        mname = sde.types[mid]["typeName"] if mid in sde.types else str(mid)
        if act == MANUFACTURING:
            need = me_amount(mq, me)
            waste = need - me_amount(mq, MAX_ME)
        else:
            need, waste = mq, 0
        row = [mname, str(need)]
        if args.display_waste:
            row.append(str(waste))
        row.append("Yes" if sde.is_buildable(mid) else "No")
        mats.append(tuple(row))
    mats.sort(key=lambda r: (r[0].lower(), r[0]))
    seconds = te_seconds(sde.times.get((bp, act), 0), args.time_efficiency)
    headers = (("Item", "Quantity", "Waste", "Buildable") if args.display_waste
               else ("Item", "Quantity", "Buildable"))
    out = [
        f"ITEM: {sde.types[prod]['typeName']} ({prod})",
        f"Group: {sde.group_line(prod)}",
        f"Market Group: {sde.market_path(prod)}",
        f"Tech Level: {sde.tech_level(prod)}",
        f"Volume: {sde.volume(prod)}",
        "",
        "Recipe:",
        f"Activity: {ACTIVITY_NAMES[act]}",
        f"Output Quantity: {qty}",
        f"Run Time: {math.ceil(seconds / 60)}",
    ] + render_table(mats, headers)
    print("\n".join(out))
    return 0


INVENTION = 8
REVERSE_ENGINEERING = 7
INVENTION_ACTIVITIES = (INVENTION, REVERSE_ENGINEERING)

# name: (probability multiplier %, runs, ME, TE)
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
RELIC_QUALITIES = [("Intact", 0.26, 20), ("Malfunctioning", 0.21, 10),
                   ("Wrecked", 0.14, 3)]
BASE_ME, BASE_TE = 2, 4


def relic_quality(name):
    for idx, (q, _c, _r) in enumerate(RELIC_QUALITIES):
        if name.startswith(q) or (" " + q + " ") in (" " + name + " "):
            return idx
    return None


def name_key(s):
    return (s.lower(), s)


class InventionData:
    def __init__(self, sde):
        self.sources = {}   # target blueprint -> [(source, activity, runs)]
        for r in read_csv(sde.path, "industryActivityProducts.csv.bz2"):
            act = to_int(r["activityID"])
            if act not in INVENTION_ACTIVITIES:
                continue
            src, prod = to_int(r["typeID"]), to_int(r["productTypeID"])
            self.sources.setdefault(prod, []).append((src, act, to_int(r["quantity"], 1)))
        self.probs = {}
        for r in read_csv(sde.path, "industryActivityProbabilities.csv.bz2"):
            act = to_int(r["activityID"])
            if act not in INVENTION_ACTIVITIES:
                continue
            self.probs[(to_int(r["typeID"]), act, to_int(r["productTypeID"]))] = r["probability"]
        self.skills = {}
        for r in read_csv(sde.path, "industryActivitySkills.csv.bz2"):
            act = to_int(r["activityID"])
            if act not in INVENTION_ACTIVITIES:
                continue
            key = (to_int(r["typeID"]), act)
            sid = to_int(r["skillID"])
            lst = self.skills.setdefault(key, [])
            if sid not in lst:
                lst.append(sid)


def chance(base, n_science, skill, n_encryption, mult):
    factor = 1
    for _ in range(n_science):
        factor += skill / 30
    for _ in range(n_encryption):
        factor += skill / 40
    p = base * factor * (1 + mult / 100)
    return min(1.0, max(0.0, p))


def fmt_row(cells, widths):
    return "|" + "|".join((" " + c).ljust(w) for c, w in zip(cells, widths)) + "|"


def cmd_invention(args):
    sde = SDE(args.sde)
    inv = InventionData(sde)
    name = args.name
    tid = sde.name_to_id.get(name)
    if tid is None:
        print(f"Error: no published item named '{name}'", file=sys.stderr)
        return 1
    # Resolve to product and its blueprint.
    product = tid
    if tid in sde.bp_products:
        rec = sde.recipe_for_blueprint(tid)
        if rec is not None:
            product = rec[2]
    target_bps = []
    if tid != product:
        target_bps.append(tid)
    for bp, act, _q in sorted(sde.product_bps.get(product, []),
                              key=lambda c: sde._bp_rank(c[0], c[1])):
        if bp not in target_bps:
            target_bps.append(bp)
    target_bp, sources = None, []
    for bp in target_bps:
        if inv.sources.get(bp):
            target_bp, sources = bp, inv.sources[bp]
            break
    if not sources:
        print(f"Error: '{name}' cannot be invented or reverse engineered", file=sys.stderr)
        return 1

    def src_rank(s):
        return (0 if sde.published(s[0]) else 1,
                0 if s[1] == INVENTION else 1, s[0])

    published_sources = [s for s in sources if sde.published(s[0])] or sources
    relic_sources = [s for s in published_sources
                     if relic_quality(sde.types.get(s[0], {}).get("typeName", "")) is not None
                     and s[0] not in sde.bp_products]
    tech = sde.tech_level(product)
    is_t3 = tech == "Tech III" or (relic_sources and len(relic_sources) == len(published_sources))
    if is_t3 and relic_sources:
        used = sorted(relic_sources, key=lambda s: (
            relic_quality(sde.types[s[0]]["typeName"]),
            name_key(sde.types[s[0]]["typeName"]), s[0]))
    else:
        used = [min(published_sources, key=src_rank)]
    tech_label = "Tech III" if is_t3 else "Tech II"

    first_src, first_act, _ = used[0]
    tname = lambda i: sde.types[i]["typeName"] if i in sde.types else str(i)
    mats = sde.materials.get((first_src, first_act), [])
    datacores = [(m, q) for m, q in mats if tname(m).startswith("Datacore")] or mats
    dc_names = sorted({tname(m) for m, _q in datacores}, key=name_key)
    per_run = datacores[0][1] if datacores else 0
    skill_ids = inv.skills.get((first_src, first_act), [])
    skill_names = sorted({tname(s) for s in skill_ids}, key=name_key)
    n_enc = sum(1 for s in skill_ids if "Encryption" in tname(s))
    n_sci = len(skill_ids) - n_enc
    seconds = sde.times.get((first_src, first_act), 0)
    level = args.skill

    out = [
        f"ITEM: {tname(product)} ({product})",
        f"Group: {sde.group_line(product)}",
        f"Market Group: {sde.market_path(product)}",
        f"Tech Level: {tech_label}",
        "",
        "Invention:",
    ]
    if not is_t3:
        out.append(f"Base: {tname(first_src)} ({first_src})")
    out += [
        f"Datacores: {', '.join(dc_names)}",
        f"Required Per Run: {per_run}",
        f"Skills: {', '.join(skill_names)}",
        f"Time: {math.ceil(seconds / 60)}",
        "Probabilities:",
    ]
    if is_t3:
        widths = [30, 33, 13, 6, 4, 4]
        headers = ["Invented From", "Decryptor", "Probability", "Runs", "ME", "TE"]
    else:
        widths = [33, 13, 6, 4, 4]
        headers = ["Decryptor", "Probability", "Runs", "ME", "TE"]
    out.append(fmt_row(headers, widths))
    out.append("|" + "|".join("-" * w for w in widths) + "|")
    for src, act, runs in used:
        if is_t3:
            q = relic_quality(tname(src))
            _qn, base, base_runs = RELIC_QUALITIES[q]
        else:
            raw = inv.probs.get((src, act, target_bp))
            base = float(raw) if raw not in (None, "", "None") else 0.0
            base_runs = runs
        for dname, mult, druns, dme, dte in sorted(
                DECRYPTORS, key=lambda d: (d[0] != "None", name_key(d[0]))):
            p = chance(base, n_sci, level, n_enc, mult)
            cells = [f"{p * 100:.2f}", str(base_runs + druns),
                     str(BASE_ME + dme), str(BASE_TE + dte)]
            row = [dname] + cells
            if is_t3:
                row = [tname(src)] + row
            out.append(fmt_row(row, widths))
    print("\n".join(out))
    return 0


# ---------------------------------------------------------------- plan
from fractions import Fraction


def parse_simple_yaml(text):
    """Minimal YAML subset: nested mappings of scalars via indentation."""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        k, _, v = line.strip().partition(":")
        k = k.strip().strip("'\"")
        v = v.strip().strip("'\"")
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if v == "":
            child = {}
            parent[k] = child
            stack.append((indent, child))
        else:
            parent[k] = v
    return root


def load_config(path):
    data = None
    try:
        import yaml  # optional
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except ImportError:
        with open(path, encoding="utf-8") as fh:
            data = parse_simple_yaml(fh.read())
    if not isinstance(data, dict):
        raise ValueError("config must be a mapping")
    te_bonus = to_int(data.get("te_bonus", 0), None)
    skill = to_int(data.get("invention_skill", 5), None)
    if te_bonus is None or not 0 <= te_bonus <= 30:
        raise ValueError("te_bonus must be an integer in [0, 30]")
    if skill is None or not 1 <= skill <= 5:
        raise ValueError("invention_skill must be an integer in [1, 5]")
    try:
        split = Fraction(str(data.get("split_days", 3)))
    except (ValueError, ZeroDivisionError):
        raise ValueError("split_days must be a number <= 30")
    if not 0 < split <= 30:
        raise ValueError("split_days must be a number in (0, 30]")
    comp = data.get("component_bpo_defaults") or {}
    if not isinstance(comp, dict):
        raise ValueError("component_bpo_defaults must be a mapping")
    cme = to_int(comp.get("me", 10), None)
    cte = to_int(comp.get("te", 20), None)
    if cme is None or not 0 <= cme <= MAX_ME:
        raise ValueError("component_bpo_defaults.me must be an integer in [0, 10]")
    if cte is None or not 0 <= cte <= MAX_TE:
        raise ValueError("component_bpo_defaults.te must be an integer in [0, 20]")
    return {"te_bonus": te_bonus, "invention_skill": skill, "split_days": split,
            "component_me": cme, "component_te": cte}


def is_int_token(t):
    return t.isdigit()


def ceil_div(a, b):
    return -(-a // b)


class Planner:
    def __init__(self, sde):
        self.sde = sde
        self._inv = None
        self.decryptors = {d[0]: d for d in DECRYPTORS}

    @property
    def inv(self):
        if self._inv is None:
            self._inv = InventionData(self.sde)
        return self._inv

    def product_recipe(self, name):
        tid = self.sde.name_to_id.get(name)
        if tid is None:
            return None
        if tid in self.sde.bp_products:
            rec = self.sde.recipe_for_blueprint(tid)
            if rec is not None:
                return rec
        return self.sde.recipe_for_product(tid)

    def relic_names(self):
        names = {}
        for bp, srcs in self.inv.sources.items():
            for s, _a, _r in srcs:
                n = self.sde.types.get(s, {}).get("typeName")
                if n and relic_quality(n) is not None and s not in self.sde.bp_products:
                    names[n] = s
        return names

    def parse(self, build):
        tokens = build.split()
        jobs = 1
        if tokens and len(tokens[-1]) > 1 and tokens[-1][0] in "xX" \
                and tokens[-1][1:].isdigit():
            jobs = int(tokens[-1][1:])
            tokens = tokens[:-1]
        if not tokens:
            raise ValueError("empty build string")
        text = " ".join(tokens)
        cands = []
        dec_names = sorted(self.decryptors, key=len, reverse=True)

        def dec_split(s):
            for d in dec_names:
                if s == d or s.endswith(" " + d):
                    rest = s[:len(s) - len(d)].rstrip()
                    yield d, rest
        # Reverse engineering: name runs decryptor relic
        if len(tokens) >= 4:
            relics = self.relic_names()
            for rname in sorted(relics, key=len, reverse=True):
                if text.endswith(" " + rname):
                    before = text[:len(text) - len(rname)].rstrip()
                    for d, rest in dec_split(before):
                        rt = rest.split()
                        if len(rt) >= 2 and is_int_token(rt[-1]):
                            cands.append(dict(kind="re", name=" ".join(rt[:-1]),
                                              runs=int(rt[-1]), decryptor=d,
                                              relic=relics[rname]))
        # Invention: name runs decryptor
        for d, rest in dec_split(text):
            rt = rest.split()
            if len(rt) >= 2 and is_int_token(rt[-1]):
                cands.append(dict(kind="invent", name=" ".join(rt[:-1]),
                                  runs=int(rt[-1]), decryptor=d))
        # Normal: name runs me te
        if len(tokens) >= 4 and all(is_int_token(t) for t in tokens[-3:]):
            cands.append(dict(kind="normal", name=" ".join(tokens[:-3]),
                              runs=int(tokens[-3]), me=int(tokens[-2]),
                              te=int(tokens[-1])))
        # Item: name runs
        if len(tokens) >= 2 and is_int_token(tokens[-1]):
            cands.append(dict(kind="item", name=" ".join(tokens[:-1]),
                              runs=int(tokens[-1]), me=0, te=0))
        cands.append(dict(kind="item", name=text, runs=1, me=0, te=0))
        for c in cands:
            rec = self.product_recipe(c["name"])
            if rec is not None:
                c["recipe"] = rec
                c["jobs"] = jobs
                return c
        raise ValueError(f"could not resolve build string '{build}'")

    def invention_source(self, bp, relic=None):
        srcs = self.inv.sources.get(bp, [])
        if relic is not None:
            for s in srcs:
                if s[0] == relic:
                    return s
            return None
        pub = [s for s in srcs if self.sde.published(s[0])] or srcs
        norm = [s for s in pub if s[0] in self.sde.bp_products] or pub
        if not norm:
            return None
        return min(norm, key=lambda s: (0 if s[1] == INVENTION else 1, s[0]))

    def plan(self, spec, config):
        sde = self.sde
        bp, act, prod, qty = spec["recipe"]
        runs, jobs = spec["runs"], spec["jobs"]
        buy = {}

        def add(tid, n):
            if n:
                buy[tid] = buy.get(tid, 0) + n

        if spec["kind"] in ("invent", "re"):
            relic = spec.get("relic")
            src = self.invention_source(bp, relic)
            if src is None:
                raise ValueError(f"'{spec['name']}' cannot be invented"
                                 + (" from that relic" if relic else ""))
            s_id, s_act, s_runs = src
            _dn, mult, druns, dme, dte = self.decryptors[spec["decryptor"]]
            q = relic_quality(sde.types.get(s_id, {}).get("typeName", "")) \
                if s_id not in sde.bp_products else None
            if q is not None:
                _qn, base_f, base_runs = RELIC_QUALITIES[q]
                base = Fraction(str(base_f))
            else:
                raw = self.inv.probs.get((s_id, s_act, bp))
                base = Fraction(str(raw)) if raw not in (None, "", "None") else Fraction(0)
                base_runs = s_runs
            skill_ids = self.inv.skills.get((s_id, s_act), [])
            tn = lambda i: sde.types[i]["typeName"] if i in sde.types else ""
            n_enc = sum(1 for s in skill_ids if "Encryption" in tn(s))
            n_sci = len(skill_ids) - n_enc
            lvl = config["invention_skill"]
            p = base * (1 + n_sci * Fraction(lvl, 30) + n_enc * Fraction(lvl, 40)) \
                * (1 + Fraction(mult, 100))
            p = min(Fraction(1), max(Fraction(0), p))
            if p <= 0:
                raise ValueError("invention probability is zero")
            bp_runs = max(1, base_runs + druns)
            me, te = BASE_ME + dme, BASE_TE + dte
            if runs <= bp_runs:
                n_bps = ceil_div(jobs, bp_runs // runs)
            else:
                n_bps = jobs * ceil_div(runs, bp_runs)
            attempts = math.ceil(Fraction(n_bps) / p)
            for mid, mq in sde.materials.get((s_id, s_act), []):
                add(mid, mq * attempts)
            if q is not None:
                add(s_id, attempts)
            if spec["decryptor"] != "None":
                did = sde.name_to_id.get(spec["decryptor"])
                if did is not None:
                    add(did, attempts)
        else:
            me, te = spec["me"], spec["te"]
        if act != MANUFACTURING:
            me, te = 0, 0
        te_bonus = config["te_bonus"]
        split_days = config["split_days"]
        build_all = config.get("build_all", False)
        need = {}
        produced = {}
        job_rows = {}

        def bp_name(b):
            return sde.types[b]["typeName"] if b in sde.types else str(b)

        def run_jobs(b, a, job_runs, j_me, j_te):
            secs = Fraction(sde.times.get((b, a), 0)) * Fraction(100 - j_te, 100) \
                * Fraction(100 - te_bonus, 100)
            for r in job_runs:
                days = round(float(secs * r / 86400), 2)
                key = (bp_name(b), r, days)
                job_rows[key] = job_rows.get(key, 0) + 1
                for mid, mq in sde.materials.get((b, a), []):
                    amt = max(r, ceil_div(mq * r * (100 - j_me), 100))
                    if build_all and self.component_recipe(mid) is not None:
                        need[mid] = need.get(mid, 0) + amt
                    else:
                        add(mid, amt)
            return secs

        secs_run = Fraction(sde.times.get((bp, act), 0)) * Fraction(100 - te, 100) \
            * Fraction(100 - te_bonus, 100)
        top_runs = []
        for _ in range(jobs):
            top_runs.extend(split_runs(runs, secs_run, split_days))
        run_jobs(bp, act, top_runs, me, te)
        produced[prod] = produced.get(prod, 0)

        if build_all:
            cme, cte = config["component_me"], config["component_te"]
            for tid in self.topo_order(bp, act):
                total = need.get(tid, 0)
                if total <= 0:
                    continue
                cb, ca, _cp, cq = self.component_recipe(tid)
                n = ceil_div(total, max(1, cq))
                produced[tid] = n * cq - total
                if ca != MANUFACTURING:
                    j_me, j_te = 0, 0
                elif self.is_t1_capital(tid):
                    j_me, j_te = 0, 0
                else:
                    j_me, j_te = cme, cte
                s = Fraction(sde.times.get((cb, ca), 0)) * Fraction(100 - j_te, 100) \
                    * Fraction(100 - te_bonus, 100)
                run_jobs(cb, ca, split_runs(n, s, split_days), j_me, j_te)

        mat_rows = []
        ids = set(buy) | {t for t, e in produced.items() if e > 0 and t != prod}
        for tid in ids:
            n = buy.get(tid, 0)
            end = produced.get(tid, 0) if tid != prod else 0
            if n <= 0 and end <= 0:
                continue
            name = sde.types[tid]["typeName"] if tid in sde.types else str(tid)
            vol = Decimal(sde.volume(tid)) if tid in sde.types else Decimal(0)
            bv = float(round(vol * n, 2))
            mat_rows.append((name, n, bv, 0, end))
        mat_rows.sort(key=lambda r: r[0])
        rows = [(k[0], k[1], k[2], c) for k, c in job_rows.items()]
        rows.sort(key=lambda r: (r[0], r[1], r[3], r[2]))
        return mat_rows, rows

    def component_recipe(self, tid):
        if not hasattr(self, "_comp_cache"):
            self._comp_cache = {}
        if tid not in self._comp_cache:
            rec = self.sde.recipe_for_product(tid)
            if rec is not None and not self.sde.published(rec[0]):
                rec = None
            self._comp_cache[tid] = rec
        return self._comp_cache[tid]

    def is_t1_capital(self, tid):
        t = self.sde.types.get(tid)
        if t is None:
            return False
        g = self.sde.groups.get(to_int(t["groupID"]), {})
        if g.get("categoryID") != "6" or g.get("groupName") not in CAPITAL_GROUPS:
            return False
        return self.sde.tech_level(tid) == "Tech I"

    def topo_order(self, bp, act):
        """Buildable components reachable from (bp, act), consumers first."""
        order, state = [], {}

        def visit(tid):
            if state.get(tid):
                return
            state[tid] = 1
            rec = self.component_recipe(tid)
            for mid, _q in self.sde.materials.get((rec[0], rec[1]), []):
                if self.component_recipe(mid) is not None:
                    visit(mid)
            order.append(tid)

        for mid, _q in self.sde.materials.get((bp, act), []):
            if self.component_recipe(mid) is not None:
                visit(mid)
        order.reverse()
        return order


CAPITAL_GROUPS = {"Titan", "Dreadnought", "Lancer Dreadnought", "Freighter",
                  "Carrier", "Supercarrier", "Capital Industrial Ship",
                  "Jump Freighter", "Force Auxiliary"}


def split_runs(n, secs_per_run, split_days):
    """Split a job of n runs so none exceeds split_days. Returns list of runs."""
    limit = min(Fraction(30), split_days)
    # A job reaching the threshold breaches it; split jobs must stay strictly
    # below it (matches the reference examples, e.g. 30 x 0.1 day -> 2 x 15).
    if n <= 1 or secs_per_run <= 0 or secs_per_run * n < limit * 86400:
        return [n]
    days_per_run = secs_per_run / 86400
    max_runs = math.ceil(limit / days_per_run) - 1
    if max_runs < 1:
        max_runs = 1
    t = ceil_div(n, max_runs)
    b, x = n // t, n % t
    if b + 1 == max_runs and x > Fraction(t, 2) and n // (t + 1) + 1 < max_runs:
        t += 1
        b, x = n // t, n % t
    return [b + 1] * x + [b] * (t - x)


def cmd_plan(args):
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    config["build_all"] = getattr(args, "build_all", False)
    sde = SDE(args.sde)
    planner = Planner(sde)
    try:
        spec = planner.parse(args.build)
        mats, jobs = planner.plan(spec, config)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    os.makedirs(args.output, exist_ok=True)
    with open(os.path.join(args.output, "materials.csv"), "w",
              newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["name", "to_buy", "buy_volume", "start_amount", "end_amount"])
        w.writerows(mats)
    with open(os.path.join(args.output, "jobs.csv"), "w",
              newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["name", "runs", "days", "count"])
        w.writerows(jobs)
    return 0


def skill_level(v):
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("skill must be an integer 1-5")
    if not 1 <= n <= 5:
        raise argparse.ArgumentTypeError("skill must be an integer 1-5")
    return n


def material_efficiency(v):
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("material efficiency must be an integer 0-10")
    if not 0 <= n <= MAX_ME:
        raise argparse.ArgumentTypeError("material efficiency must be an integer 0-10")
    return n


def time_efficiency(v):
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("time efficiency must be an even integer 0-20")
    if not 0 <= n <= MAX_TE or n % 2:
        raise argparse.ArgumentTypeError("time efficiency must be an even integer 0-20")
    return n


def main(argv=None):
    p = argparse.ArgumentParser(description="EVE Online industry planner")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("recipe", help="Show recipe for a product or blueprint")
    r.add_argument("name", nargs="+")
    r.add_argument("--sde", required=True)
    r.add_argument("--material-efficiency", "-me", dest="material_efficiency",
                   type=material_efficiency, default=0)
    r.add_argument("--time-efficiency", "-te", dest="time_efficiency",
                   type=time_efficiency, default=0)
    r.add_argument("--display-waste", "-waste", dest="display_waste",
                   action="store_true")
    i = sub.add_parser("invention", help="Plan invention / reverse engineering")
    i.add_argument("name", nargs="+")
    i.add_argument("--sde", required=True)
    i.add_argument("--skill", type=skill_level, default=5)
    pl = sub.add_parser("plan", help="Create a build plan")
    pl.add_argument("build", nargs="+")
    pl.add_argument("--config", required=True)
    pl.add_argument("--sde", required=True)
    pl.add_argument("--output", required=True)
    pl.add_argument("--build-all", dest="build_all", action="store_true")
    args = p.parse_args(argv)
    if args.command == "plan":
        args.build = " ".join(args.build)
        return cmd_plan(args)
    if args.command == "invention":
        args.name = " ".join(args.name)
        return cmd_invention(args)
    if args.command == "recipe":
        args.name = " ".join(args.name)
        return cmd_recipe(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
