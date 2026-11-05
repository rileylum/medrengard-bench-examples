#!/usr/bin/env python3
"""Jump Freighter logistics planner."""
import argparse
import bz2
import csv
import heapq
import io
import math
import os
import sys

LY_M = 9_460_730_472_580_800.0
POCHVEN_REGION = 10000070
ZARZAKH_SYSTEM = 30100000
EPS = 1e-9


def open_csv(sde, name):
    for cand in (name + ".bz2", name):
        p = os.path.join(sde, cand)
        if os.path.exists(p):
            if cand.endswith(".bz2"):
                return io.TextIOWrapper(bz2.open(p, "rb"), encoding="utf-8", newline="")
            return open(p, encoding="utf-8", newline="")
    raise FileNotFoundError(os.path.join(sde, name + ".bz2"))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def round_sec(sec):
    return math.floor(sec * 10 + 0.5) / 10


def load(sde):
    systems = {}
    with open_csv(sde, "mapSolarSystems.csv") as f:
        for r in csv.DictReader(f):
            sid = int(r["solarSystemID"])
            systems[sid] = {
                "name": r["solarSystemName"],
                "region": int(r["regionID"]),
                "pos": (float(r["x"]) / LY_M, float(r["y"]) / LY_M, float(r["z"]) / LY_M),
                "sec": fnum(r["security"]) or 0.0,
            }
    stations = {}  # name -> system id
    sys_stations = {}  # system id -> list of (stationID, name)
    with open_csv(sde, "staStations.csv") as f:
        for r in csv.DictReader(f):
            name = r["stationName"]
            sid = int(r["solarSystemID"])
            stid = int(r["stationID"])
            stations[name] = sid
            sys_stations.setdefault(sid, []).append((stid, name))
    for v in sys_stations.values():
        v.sort()
    return systems, stations, sys_stations


def jumpable_into(sysinfo, sid):
    if sid == ZARZAKH_SYSTEM or sysinfo["region"] == POCHVEN_REGION:
        return False
    if sysinfo["region"] >= 11000000:  # wormhole / abyssal / special space
        return False
    if round_sec(sysinfo["sec"]) >= 0.5:
        return False
    return True


def dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def apply_jump(tf, eff):
    """tf is the pre-jump fatigue (cooldown already elapsed). Returns (tf, tc)."""
    k = 1.0 + eff
    if tf <= 0:
        ntf = 10.0 * k
        ntc = k
    else:
        ntf = max(tf, 10.0) * k
        ntc = tf / 10.0
    return min(ntf, 300.0), max(0.0, min(ntc, 30.0))


def ceil_eps(x):
    return int(math.ceil(x - EPS)) if x > EPS else 0


def hhmm(minutes):
    m = ceil_eps(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def int_in(lo, hi):
    def f(s):
        try:
            v = int(s.replace(",", ""))
        except ValueError:
            raise argparse.ArgumentTypeError(f"must be an integer in [{lo},{hi}]")
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be an integer in [{lo},{hi}]")
        return v
    return f


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan Jump Freighter routes.")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sde", required=True)
    ap.add_argument("--range", type=int_in(5, 10), default=10)
    ap.add_argument("--fuel", type=int_in(1, 10000), default=10000)
    ap.add_argument("--reduction", type=int_in(0, 100), default=90)
    a = ap.parse_args(argv)

    if not os.path.isdir(a.sde):
        die(f"SDE directory not found: {a.sde}")
    systems, stations, sys_stations = load(a.sde)

    if a.start not in stations:
        die(f"start station not found: {a.start}")
    if a.end not in stations:
        die(f"end station not found: {a.end}")
    s_sys = stations[a.start]
    e_sys = stations[a.end]
    if e_sys not in systems or not jumpable_into(systems[e_sys], e_sys):
        die(f"end station is not in a jumpable system: {a.end}")
    if s_sys not in systems:
        die(f"unknown start system for: {a.start}")

    rng = float(a.range)
    factor = 1.0 - a.reduction / 100.0

    lines = [f"START: {a.start}"]
    if s_sys == e_sys:
        hops = []
    else:
        cands = [sid for sid in sys_stations if sid in systems and jumpable_into(systems[sid], sid)]
        pos = {sid: systems[sid]["pos"] for sid in cands}
        pos[s_sys] = systems[s_sys]["pos"]
        # spatial grid for neighbour search
        cell = rng
        grid = {}
        for sid in cands:
            p = pos[sid]
            key = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
            grid.setdefault(key, []).append(sid)
        nbr_cache = {}

        def neighbours(sid):
            if sid in nbr_cache:
                return nbr_cache[sid]
            p = pos[sid]
            cx, cy, cz = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
            out = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for o in grid.get((cx + dx, cy + dy, cz + dz), ()):
                            if o == sid:
                                continue
                            d = dist(p, pos[o])
                            if d <= rng:
                                out.append((o, d))
            nbr_cache[sid] = out
            return out

        def bfs(src):
            depth = {src: 0}
            frontier = [src]
            while frontier:
                nxt = []
                for u in frontier:
                    for v, _ in neighbours(u):
                        if v not in depth:
                            depth[v] = depth[u] + 1
                            nxt.append(v)
                frontier = nxt
            return depth

        ds = bfs(s_sys)
        if e_sys not in ds:
            die("no route found")
        n = ds[e_sys]
        de = bfs(e_sys)  # symmetric graph (start excluded only as a target)

        # Layered label search over shortest-path DAG.
        # Label: (wait_committed, fatigue_at_next_jump, total_ly, names_tuple, path, tf, tc, wait)
        def dominated(lab, others):
            for o in others:
                if (o[0] <= lab[0] + EPS and o[1] <= lab[1] + EPS and o[2] <= lab[2] + EPS
                        and o[3] <= lab[3]):
                    return True
            return False

        layer = {s_sys: [(0.0, 0.0, 0.0, (), (s_sys,), 0.0, 0.0, 0.0)]}
        final = []
        for step in range(1, n + 1):
            new = {}
            for u, labs in layer.items():
                for v, d in neighbours(u):
                    if ds.get(v) != step or de.get(v, 10 ** 9) != n - step:
                        continue
                    for lab in labs:
                        wc, fpre, ly, names, path, _, _, _ = lab
                        tf, tc = apply_jump(fpre, d * factor)
                        nl = ly + d
                        nnames = names + (systems[v]["name"],)
                        npath = path + (v,)
                        if v == e_sys:
                            final.append((wc, nl, nnames, npath, tf, tc))
                        else:
                            new.setdefault(v, []).append(
                                (wc + tc, max(tf - tc, 0.0), nl, nnames, npath, tf, tc, wc))
            layer = {}
            for v, labs in new.items():
                labs.sort(key=lambda l: (l[0], l[1], l[2], l[3]))
                kept = []
                for lab in labs:
                    if not dominated(lab, kept):
                        kept.append(lab)
                layer[v] = kept
        final.sort(key=lambda f: (ceil_eps(f[0]), f[1], f[2]))
        best = final[0]
        path = best[3]
        hops = []
        for i in range(1, len(path)):
            hops.append((path[i], dist(pos[path[i - 1]], pos[path[i]])))

    # Simulate and print
    tf = tc = waited = total_ly = 0.0
    iso_total = 0
    for i, (v, d) in enumerate(hops):
        if tc > 0:
            waited += tc
            tf = max(tf - tc, 0.0)
            tc = 0.0
        tf, tc = apply_jump(tf, d * factor)
        iso = a.fuel * d
        iso_int = ceil_eps(iso)
        iso_total += iso_int
        total_ly += d
        dock = a.end if v == e_sys else sys_stations[v][0][1]
        lines.append("UNDOCK")
        lines.append(f"JUMP {d:.2f} LY: {systems[v]['name']} ({ceil_eps(iso / 1000.0):,}K isotopes)")
        lines.append(f"DOCK: {dock}")
    if not hops and a.start != a.end:
        lines.append("UNDOCK")
        lines.append(f"DOCK: {a.end}")
    lines.append("SUMMARY:")
    lines.append(f"  End Cooldown: {hhmm(tc)}")
    lines.append(f"  End Fatigue: {hhmm(tf)}")
    lines.append(f"  Isotopes Used: {ceil_eps(iso_total / 1000.0):,}K")
    lines.append(f"  Time Waiting: {hhmm(waited)}")
    lines.append(f"  Total LY: {total_ly:.2f}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
