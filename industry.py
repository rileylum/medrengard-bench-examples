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


def main(argv=None):
    p = argparse.ArgumentParser(description="EVE Online industry planner")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("recipe", help="Show recipe for a product or blueprint")
    r.add_argument("name", nargs="+")
    r.add_argument("--sde", required=True)
    args = p.parse_args(argv)
    if args.command == "recipe":
        args.name = " ".join(args.name)
        return cmd_recipe(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
