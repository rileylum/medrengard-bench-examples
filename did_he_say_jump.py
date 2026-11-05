#!/usr/bin/env python3
"""Jump Freighter logistics planner."""
import argparse
import bz2
import csv
import io
import math
import os
import sys
from collections import deque

LY_M = 9460730472580800.0
POCHVEN_REGION = 10000070
ZARZAKH_SYSTEM = 30100000
FATIGUE_CAP = 300.0
COOLDOWN_CAP = 30.0
EPS = 1e-9


def open_csv(sde, base):
    for name in (base + ".csv.bz2", base + ".csv"):
        p = os.path.join(sde, name)
        if os.path.exists(p):
            if name.endswith(".bz2"):
                return io.TextIOWrapper(bz2.open(p, "rb"), encoding="utf-8", newline="")
            return open(p, encoding="utf-8", newline="")
    raise FileNotFoundError(os.path.join(sde, base + ".csv.bz2"))


def fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load(sde):
    systems = {}
    with open_csv(sde, "mapSolarSystems") as f:
        for r in csv.DictReader(f):
            sid = int(r["solarSystemID"])
            systems[sid] = {
                "name": r["solarSystemName"],
                "region": int(fnum(r["regionID"])),
                "pos": (fnum(r["x"]) / LY_M, fnum(r["y"]) / LY_M, fnum(r["z"]) / LY_M),
                "sec": fnum(r["security"]),
            }
    stations = {}
    by_system = {}
    with open_csv(sde, "staStations") as f:
        for r in csv.DictReader(f):
            name = r["stationName"]
            sid = int(r["solarSystemID"])
            stations[name] = sid
            by_system.setdefault(sid, []).append(name)
    for v in by_system.values():
        v.sort()
    return systems, stations, by_system


def is_highsec(sec):
    return math.floor(sec * 10 + 0.5) / 10 >= 0.5


def jumpable_dest(sid, sysinfo):
    if sid == ZARZAKH_SYSTEM or sysinfo["region"] == POCHVEN_REGION:
        return False
    if sysinfo["region"] >= 11000000:
        return False
    return not is_highsec(sysinfo["sec"])


def dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def build_graph(nodes, systems, rng):
    """Adjacency (within range) among node ids using a spatial grid."""
    cell = float(rng)
    grid = {}
    for n in nodes:
        p = systems[n]["pos"]
        key = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
        grid.setdefault(key, []).append(n)
    adj = {n: [] for n in nodes}
    for n in nodes:
        p = systems[n]["pos"]
        kx, ky, kz = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for m in grid.get((kx + dx, ky + dy, kz + dz), ()):
                        if m == n:
                            continue
                        d = dist(p, systems[m]["pos"])
                        if d <= rng:
                            adj[n].append((m, d))
    return adj


def bfs(adj, src, allowed_next):
    depth = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for v, _ in adj[u]:
            if v not in depth and allowed_next(v):
                depth[v] = depth[u] + 1
                q.append(v)
    return depth


def apply_jump(tf, tc, eff):
    """tf/tc are pre-jump (already decayed) values. Returns new (tf, tc)."""
    if tf <= 0 and tc <= 0:
        ntf = 10.0 * (1.0 + eff)
        ntc = 1.0 + eff
    else:
        ntf = max(tf, 10.0) * (1.0 + eff)
        ntc = tf / 10.0
    ntf = max(0.0, min(ntf, FATIGUE_CAP))
    ntc = max(0.0, min(ntc, COOLDOWN_CAP))
    return ntf, ntc


def dominates(a, b):
    # label: (wait, tf, tc, ly, names, path)
    return (a[0] <= b[0] + EPS and a[1] <= b[1] + EPS and a[2] <= b[2] + EPS
            and a[3] <= b[3] + EPS and a[4] <= b[4])


def plan(adj, systems, start, end, factor):
    eligible = lambda v: v in adj
    ds = bfs(adj, start, eligible)
    if end not in ds:
        return None
    de = bfs(adj, end, eligible)
    total = ds[end]
    if total == 0:
        return []
    layers = [[] for _ in range(total + 1)]
    for n, d in ds.items():
        if n in de and d + de[n] == total:
            layers[d].append(n)
    # labels per node
    labels = {start: [(0.0, 0.0, 0.0, 0.0, (systems[start]["name"],), (start,), ())]}
    for k in range(total):
        nxt = {}
        for u in layers[k]:
            for lab in labels.get(u, ()):
                wait, tf, tc, ly, names, path, dists = lab
                # wait out cooldown before jumping
                w = wait + tc
                ptf = max(0.0, tf - tc)
                for v, d in adj[u]:
                    if ds.get(v) != k + 1 or de.get(v) != total - k - 1:
                        continue
                    ntf, ntc = apply_jump(ptf, 0.0, d * factor)
                    new = (w, ntf, ntc, ly + d, names + (systems[v]["name"],), path + (v,), dists + (d,))
                    lst = nxt.setdefault(v, [])
                    if any(dominates(o, new) for o in lst):
                        continue
                    lst[:] = [o for o in lst if not dominates(new, o)]
                    lst.append(new)
        labels = nxt
    finals = labels.get(end, [])
    best = min(finals, key=lambda l: (ceil_min(l[0]), l[3], l[4]))
    return best


def ceil_min(minutes):
    return max(0, math.ceil(round(minutes, 6)))


def fmt_time(minutes):
    m = ceil_min(minutes)
    return "%02d:%02d" % (m // 60, m % 60)


def ceil_k(x):
    return math.ceil(round(x / 1000.0, 6))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jump Freighter planner")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sde", required=True)
    ap.add_argument("--range", type=int, default=10)
    ap.add_argument("--fuel", type=int, default=10000)
    ap.add_argument("--reduction", type=int, default=90)
    a = ap.parse_args(argv)
    if not 5 <= a.range <= 10:
        ap.error("--range must be an int in [5,10]")
    if not 1 <= a.fuel <= 10000:
        ap.error("--fuel must be in [1,10000]")
    if not 0 <= a.reduction <= 100:
        ap.error("--reduction must be an int in [0,100]")

    systems, stations, by_system = load(a.sde)
    if a.start not in stations:
        print("Error: start station not found: %s" % a.start, file=sys.stderr)
        return 1
    if a.end not in stations:
        print("Error: end station not found: %s" % a.end, file=sys.stderr)
        return 1
    s_sys = stations[a.start]
    e_sys = stations[a.end]
    if s_sys not in systems or e_sys not in systems:
        print("Error: unknown system", file=sys.stderr)
        return 1
    if not jumpable_dest(e_sys, systems[e_sys]):
        print("Error: end station must not be in high sec, pochven or zarzakh", file=sys.stderr)
        return 1

    nodes = set(sid for sid in by_system if sid in systems and jumpable_dest(sid, systems[sid]))
    nodes.add(s_sys)
    nodes.add(e_sys)
    adj = build_graph(nodes, systems, a.range)
    factor = 1.0 - a.reduction / 100.0
    res = plan(adj, systems, s_sys, e_sys, factor)
    if res is None:
        print("Error: no route found", file=sys.stderr)
        return 1

    out = ["START: %s" % a.start]
    if res == []:
        wait = tf = tc = ly = 0.0
        out.append("UNDOCK")
        out.append("DOCK: %s" % a.end)
        total_k = 0
    else:
        wait, tf, tc, ly, names, path, dists = res
        total_k = 0
        for i, (sid, d) in enumerate(zip(path[1:], dists)):
            k = ceil_k(d * a.fuel)
            total_k += k
            out.append("UNDOCK")
            out.append("JUMP %.2f LY: %s (%sK isotopes)" % (d, systems[sid]["name"], format(k, ",")))
            dock = a.end if i == len(dists) - 1 else by_system[sid][0]
            out.append("DOCK: %s" % dock)
    out.append("SUMMARY:")
    out.append("  End Cooldown: %s" % fmt_time(tc))
    out.append("  End Fatigue: %s" % fmt_time(tf))
    out.append("  Isotopes Used: %sK" % format(total_k, ","))
    out.append("  Time Waiting: %s" % fmt_time(wait))
    out.append("  Total LY: %.2f" % ly)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
