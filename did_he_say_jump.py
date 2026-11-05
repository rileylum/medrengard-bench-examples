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


def round_sec(sec):
    return math.floor(sec * 10 + 0.5) / 10


def is_highsec(sec):
    return round_sec(sec) >= 0.5


def fmt_sec(sec):
    r = round_sec(sec)
    if r == 0:
        r = 0.0
    return "%.1f" % r


def jumpable_dest(sid, sysinfo):
    if sid == ZARZAKH_SYSTEM or sysinfo["region"] == POCHVEN_REGION:
        return False
    if sysinfo["region"] >= 11000000:
        return False
    return not is_highsec(sysinfo["sec"])


def dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


class Grid:
    """Spatial grid over node ids for range queries."""

    def __init__(self, nodes, systems, rng):
        self.cell = float(rng)
        self.rng = rng
        self.systems = systems
        self.grid = {}
        for n in nodes:
            self.grid.setdefault(self.key(systems[n]["pos"]), []).append(n)

    def key(self, p):
        c = self.cell
        return (math.floor(p[0] / c), math.floor(p[1] / c), math.floor(p[2] / c))

    def near(self, p):
        kx, ky, kz = self.key(p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for m in self.grid.get((kx + dx, ky + dy, kz + dz), ()):
                        d = dist(p, self.systems[m]["pos"])
                        if d <= self.rng:
                            yield m, d


def build_graph(nodes, systems, rng, gates=None):
    """Adjacency among node ids: adj[n] = [(m, d, via)].

    `via` is None for a direct jump from n, or the gate-neighbour system id
    the jump is made from when the cloak trick is used (gates given).
    For each (n, m) only the shortest option is kept (direct preferred on ties).
    """
    grid = Grid(nodes, systems, rng)
    adj = {}
    for n in nodes:
        best = {}
        for m, d in grid.near(systems[n]["pos"]):
            if m != n:
                best[m] = (d, 0, "", None)
        if gates is not None:
            for g in gates.get(n, ()):
                if g not in systems:
                    continue
                gname = systems[g]["name"]
                for m, d in grid.near(systems[g]["pos"]):
                    if m == n or m == g:
                        continue
                    cand = (d, 1, gname, g)
                    cur = best.get(m)
                    if cur is None or cand[:3] < cur[:3]:
                        best[m] = cand
        adj[n] = sorted(((m, v[0], v[3]) for m, v in best.items()), key=lambda t: t[0])
    return adj


def load_gates(sde):
    gates = {}
    with open_csv(sde, "mapSolarSystemJumps") as f:
        for r in csv.DictReader(f):
            a = int(r["fromSolarSystemID"])
            b = int(r["toSolarSystemID"])
            gates.setdefault(a, set()).add(b)
            gates.setdefault(b, set()).add(a)
    return gates


def bfs(adj, src, allowed_next):
    depth = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for v, _, _ in adj[u]:
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
    # label: (wait, tf, tc, ly, names, path, dists, vias, gates_used)
    return (a[0] <= b[0] + EPS and a[1] <= b[1] + EPS and a[2] <= b[2] + EPS
            and a[3] <= b[3] + EPS and a[8] <= b[8] and a[4] <= b[4])


def plan(adj, systems, start, end, factor):
    eligible = lambda v: v in adj
    ds = bfs(adj, start, eligible)
    if end not in ds:
        return None
    # edges may be asymmetric (cloak gate trick), so search backwards on
    # the reversed graph
    radj = {n: [] for n in adj}
    for u, lst in adj.items():
        for v, d, via in lst:
            radj[v].append((u, d, via))
    de = bfs(radj, end, eligible)
    total = ds[end]
    if total == 0:
        return []
    layers = [[] for _ in range(total + 1)]
    for n, d in ds.items():
        if n in de and d + de[n] == total:
            layers[d].append(n)
    # labels per node
    labels = {start: [(0.0, 0.0, 0.0, 0.0, (systems[start]["name"],), (start,), (), (), 0)]}
    for k in range(total):
        nxt = {}
        for u in layers[k]:
            for lab in labels.get(u, ()):
                wait, tf, tc, ly, names, path, dists, vias, ng = lab
                # wait out cooldown before jumping
                w = wait + tc
                ptf = max(0.0, tf - tc)
                for v, d, via in adj[u]:
                    if ds.get(v) != k + 1 or de.get(v) != total - k - 1:
                        continue
                    ntf, ntc = apply_jump(ptf, 0.0, d * factor)
                    new = (w, ntf, ntc, ly + d, names + (systems[v]["name"],), path + (v,), dists + (d,),
                           vias + (via,), ng + (via is not None))
                    lst = nxt.setdefault(v, [])
                    if any(dominates(o, new) for o in lst):
                        continue
                    lst[:] = [o for o in lst if not dominates(new, o)]
                    lst.append(new)
        labels = nxt
    finals = labels.get(end, [])
    best = min(finals, key=lambda l: (ceil_min(l[0]), round(l[3], 9), l[8], l[4]))
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
    ap.add_argument("--cloak", action="store_true",
                    help="allow taking one gate after each undock before jumping")
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
    gates = load_gates(a.sde) if a.cloak else None
    adj = build_graph(nodes, systems, a.range, gates)
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
        wait, tf, tc, ly, names, path, dists, vias, _ = res
        total_k = 0
        for i, (sid, d) in enumerate(zip(path[1:], dists)):
            k = ceil_k(d * a.fuel)
            total_k += k
            out.append("UNDOCK")
            via = vias[i]
            if via is not None:
                frm = path[i]
                out.append("GO: %s (%s) -> %s (%s)" % (
                    systems[frm]["name"], fmt_sec(systems[frm]["sec"]),
                    systems[via]["name"], fmt_sec(systems[via]["sec"])))
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
