#!/usr/bin/env python3
"""EVE Online SDE industry recipe tool."""
import argparse
import bz2
import csv
import io
import math
import os
import sys
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


def main(argv=None):
    p = argparse.ArgumentParser(description="EVE industry recipe planner")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recipe")
    r.add_argument("name", nargs="+")
    r.add_argument("--sde", required=True)
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
    minutes = math.ceil(secs / 60) if secs else 0

    rows = []
    for mid, mq in mats.get((bp, act), []):
        mname = types[mid]["name"] if mid in types else str(mid)
        rows.append((mname, mq, "Yes" if buildable(mid) else "No"))
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
        "| Item | Quantity | Buildable |",
        "|:-:|:---:|---:|",
    ]
    for mname, mq, b in rows:
        out.append(f"| {mname} | {mq} | {b} |")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
