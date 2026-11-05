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


def render_table(rows):
    headers = ("Item", "Quantity", "Buildable")
    widths = [max([len(headers[i])] + [len(r[i]) for r in rows]) for i in range(3)]
    def line(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    sep = ("|:" + "-" * widths[0] + ":|:" + "-" * widths[1] + ":|"
           + "-" * (widths[2] + 1) + ":|")
    return [line(headers), sep] + [line(r) for r in rows]


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
    mats = []
    for mid, mq in sde.materials.get((bp, act), []):
        mname = sde.types[mid]["typeName"] if mid in sde.types else str(mid)
        mats.append((mname, str(mq), "Yes" if sde.is_buildable(mid) else "No"))
    mats.sort(key=lambda r: (r[0].lower(), r[0]))
    seconds = sde.times.get((bp, act), 0)
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
    ] + render_table(mats)
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


def skill_level(v):
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("skill must be an integer 1-5")
    if not 1 <= n <= 5:
        raise argparse.ArgumentTypeError("skill must be an integer 1-5")
    return n


def main(argv=None):
    p = argparse.ArgumentParser(description="EVE Online industry planner")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("recipe", help="Show recipe for a product or blueprint")
    r.add_argument("name", nargs="+")
    r.add_argument("--sde", required=True)
    i = sub.add_parser("invention", help="Plan invention / reverse engineering")
    i.add_argument("name", nargs="+")
    i.add_argument("--sde", required=True)
    i.add_argument("--skill", type=skill_level, default=5)
    args = p.parse_args(argv)
    if args.command == "invention":
        args.name = " ".join(args.name)
        return cmd_invention(args)
    if args.command == "recipe":
        args.name = " ".join(args.name)
        return cmd_recipe(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
