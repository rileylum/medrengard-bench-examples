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
STATION_POS = {}


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
            try:
                STATION_POS[stid] = (float(r["x"]), float(r["y"]), float(r["z"]))
            except (KeyError, TypeError, ValueError):
                pass
            sys_stations.setdefault(sid, []).append((stid, name))
    for v in sys_stations.values():
        v.sort()
    return systems, stations, sys_stations


def load_gates(sde):
    gates = {}
    try:
        f = open_csv(sde, "mapSolarSystemJumps.csv")
    except FileNotFoundError:
        return gates
    with f:
        for r in csv.DictReader(f):
            a, b = int(r["fromSolarSystemID"]), int(r["toSolarSystemID"])
            gates.setdefault(a, set()).add(b)
            gates.setdefault(b, set()).add(a)
    return gates


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


def is_kspace(sysinfo, sid):
    return (sid != ZARZAKH_SYSTEM and sysinfo["region"] != POCHVEN_REGION
            and sysinfo["region"] < 11000000)


def is_highsec(sysinfo, sid):
    return is_kspace(sysinfo, sid) and round_sec(sysinfo["sec"]) >= 0.5


def load_objects(sde, sys_ids):
    """Stargates and stations in given systems from mapDenormalize.
    Returns {system: {"gates": {dest name: pos}, "stations": {name: pos}}}."""
    out = {sid: {"gates": {}, "stations": {}} for sid in sys_ids}
    want = {str(sid) for sid in sys_ids}
    with open_csv(sde, "mapDenormalize.csv") as f:
        rd = csv.reader(f)
        hdr = next(rd)
        ig, isys, ix, iy, iz, iname = (hdr.index(c) for c in
                                         ("groupID", "solarSystemID", "x", "y", "z", "itemName"))
        for r in rd:
            g = r[ig]
            if g not in ("10", "15") or r[isys] not in want:
                continue
            p = (float(r[ix]), float(r[iy]), float(r[iz]))
            d = out[int(r[isys])]
            name = r[iname]
            if g == "10":
                if name.startswith("Stargate (") and name.endswith(")"):
                    d["gates"][name[len("Stargate ("):-1]] = p
            else:
                d["stations"][name] = p
    return out


def hs_entries(sde, systems, sys_stations, all_gates, e_sys, max_extra):
    """Candidate (warp_m, hs_gates, entry_system, station_name, hs_path) sorted by preference.
    hs_path is the list of systems gated through after leaving the entry system (ends at e_sys)."""
    # BFS over high-sec systems from the destination
    hsd = {e_sys: 0}
    nxt = {e_sys: None}
    frontier = [e_sys]
    while frontier:
        new = []
        for u in frontier:
            for v in all_gates.get(u, ()):
                if v in hsd or v not in systems or not is_highsec(systems[v], v):
                    continue
                hsd[v] = hsd[u] + 1
                new.append(v)
        frontier = new

    def hs_path(y):
        path = [y]
        while path[-1] != e_sys:
            u = path[-1]
            opts = [v for v in all_gates.get(u, ()) if hsd.get(v) == hsd[u] - 1]
            path.append(min(opts, key=lambda v: (systems[v]["name"], v)))
        return path

    pairs = []
    for y, dy in hsd.items():
        for x in all_gates.get(y, ()):
            if x in systems and x in sys_stations and jumpable_into(systems[x], x):
                pairs.append((dy + 1, x, y))
    if not pairs:
        return []
    mg = min(p[0] for p in pairs)
    pairs = [p for p in pairs if p[0] <= mg + max_extra]
    objs = load_objects(sde, {p[1] for p in pairs})
    out = []
    for ng, x, y in pairs:
        gpos = objs[x]["gates"].get(systems[y]["name"])
        best = None
        for stid, stname in sys_stations[x]:
            spos = objs[x]["stations"].get(stname)
            if spos is None:
                spos = STATION_POS.get(stid)
            if spos is None:
                continue
            w = dist(spos, gpos) if gpos is not None else float("inf")
            if best is None or (w, stid) < best[:2]:
                best = (w, stid, stname)
        if best is None:
            continue
        out.append((best[0], ng, x, best[2], hs_path(y)))
    out.sort(key=lambda c: (c[0], c[1], tuple(systems[v]["name"] for v in c[4]), systems[c[2]]["name"]))
    return out


def plan_route(systems, sys_stations, gates, s_sys, t_sys, rng, factor):
    """Returns (hops, sort_key) for best route s_sys -> t_sys, or None."""
    if s_sys == t_sys:
        return [], (0, 0.0, 0, ())
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

    def in_range(p):
        cx, cy, cz = (math.floor(p[0] / cell), math.floor(p[1] / cell), math.floor(p[2] / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for o in grid.get((cx + dx, cy + dy, cz + dz), ()):
                        d = dist(p, pos[o])
                        if d <= rng:
                            yield o, d

    def neighbours(sid):
        """List of (target, jump distance, gate system or None)."""
        if sid in nbr_cache:
            return nbr_cache[sid]
        best = {}
        for o, d in in_range(pos[sid]):
            if o != sid:
                best[o] = (d, 0, "", None)
        for g in sorted(gates.get(sid, ())):
            if g not in systems:
                continue
            gname = systems[g]["name"]
            for o, d in in_range(systems[g]["pos"]):
                if o == sid or o == g:
                    continue
                key = (d, 1, gname, g)
                if o not in best or key[:3] < best[o][:3]:
                    best[o] = key
        out = [(o, k[0], k[3]) for o, k in best.items()]
        nbr_cache[sid] = out
        return out

    def bfs(src, adj):
        depth = {src: 0}
        frontier = [src]
        while frontier:
            nxt = []
            for u in frontier:
                for v in adj(u):
                    if v not in depth:
                        depth[v] = depth[u] + 1
                        nxt.append(v)
            frontier = nxt
        return depth

    ds = bfs(s_sys, lambda u: [e[0] for e in neighbours(u)])
    if t_sys not in ds:
        return None
    n = ds[t_sys]
    # reverse graph over the nodes reachable within n jumps
    radj = {}
    for u, du in ds.items():
        if du >= n:
            continue
        for v, _, _ in neighbours(u):
            radj.setdefault(v, []).append(u)
    de = bfs(t_sys, lambda u: radj.get(u, ()))

    # Layered label search over shortest-path DAG.
    # Label: (wait_committed, fatigue_at_next_jump, total_ly, names_tuple, path, tf, tc, wait)
    def dominated(lab, others):
        for o in others:
            if (o[0] <= lab[0] + EPS and o[1] <= lab[1] + EPS and o[2] <= lab[2] + EPS
                    and (o[8], o[3]) <= (lab[8], lab[3])):
                return True
        return False

    layer = {s_sys: [(0.0, 0.0, 0.0, (), (s_sys,), 0.0, 0.0, (), 0)]}
    final = []
    for step in range(1, n + 1):
        new = {}
        for u, labs in layer.items():
            for v, d, via in neighbours(u):
                if ds.get(v) != step or de.get(v, 10 ** 9) != n - step:
                    continue
                for lab in labs:
                    wc, fpre, ly, names, path, _, _, vias, ng = lab
                    tf, tc = apply_jump(fpre, d * factor)
                    nl = ly + d
                    nnames = names + (systems[v]["name"],)
                    npath = path + (v,)
                    nvias = vias + (via,)
                    nng = ng + (via is not None)
                    if v == t_sys:
                        final.append((wc, nl, nnames, npath, tf, tc, nvias, nng))
                    else:
                        new.setdefault(v, []).append(
                            (wc + tc, max(tf - tc, 0.0), nl, nnames, npath, tf, tc, nvias, nng))
        layer = {}
        for v, labs in new.items():
            labs.sort(key=lambda l: (l[0], l[1], l[2], l[8], l[3]))
            kept = []
            for lab in labs:
                if not dominated(lab, kept):
                    kept.append(lab)
            layer[v] = kept
    final.sort(key=lambda f: (ceil_eps(f[0]), f[1], f[7], f[2]))
    best = final[0]
    path = best[3]
    vias = best[6]
    hops = []
    for i in range(1, len(path)):
        g = vias[i - 1]
        origin = systems[g]["pos"] if g is not None else pos[path[i - 1]]
        hops.append((path[i], dist(origin, pos[path[i]]), path[i - 1], g))

    return hops, (ceil_eps(best[0]), best[1], best[7], best[2])


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] == "plan":
        argv = argv[1:]
    ap = argparse.ArgumentParser(description="Plan Jump Freighter routes.")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sde", required=True)
    ap.add_argument("--range", type=int_in(5, 10), default=10)
    ap.add_argument("--fuel", type=int_in(1, 10000), default=10000)
    ap.add_argument("--reduction", type=int_in(0, 100), default=90)
    ap.add_argument("--cloak", action="store_true",
                    help="allow taking one stargate after each undock before jumping")
    ap.add_argument("--max-extra-gates", "-gates", dest="max_extra_gates", type=int_in(0, 10000),
                    default=0,
                    help="max extra high-sec gates to accept for a station closer to the entry gate")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.sde):
        die(f"SDE directory not found: {a.sde}")
    systems, stations, sys_stations = load(a.sde)
    all_gates = load_gates(a.sde)
    gates = all_gates if a.cloak else {}

    if a.start not in stations:
        die(f"start station not found: {a.start}")
    if a.end not in stations:
        die(f"end station not found: {a.end}")
    s_sys = stations[a.start]
    e_sys = stations[a.end]
    hs_dest = e_sys in systems and is_highsec(systems[e_sys], e_sys)
    if e_sys not in systems or not (hs_dest or jumpable_into(systems[e_sys], e_sys)):
        die(f"end station is not in a jumpable system: {a.end}")
    if s_sys not in systems:
        die(f"unknown start system for: {a.start}")

    rng = float(a.range)
    factor = 1.0 - a.reduction / 100.0

    lines = [f"START: {a.start}"]
    final_dock = a.end
    hs_go = None
    if s_sys == e_sys:
        hops = []
    elif not hs_dest:
        r = plan_route(systems, sys_stations, gates, s_sys, e_sys, rng, factor)
        if r is None:
            die("no route found")
        hops = r[0]
    else:
        entries = hs_entries(a.sde, systems, sys_stations, all_gates, e_sys, a.max_extra_gates)
        best = None
        gi = 0
        while gi < len(entries) and best is None:
            lead = entries[gi]
            gj = gi
            while gj < len(entries) and entries[gj][:2] == lead[:2]:
                gj += 1
            for warp, ng, x_sys, st_name, hs_path in entries[gi:gj]:
                r = plan_route(systems, sys_stations, gates, s_sys, x_sys, rng, factor)
                if r is None:
                    continue
                hops_c, key = r
                names = key[3] + tuple(systems[v]["name"] for v in hs_path)
                k = (len(hops_c), key[0], key[1], key[2], names, warp)
                if best is None or k < best[0]:
                    best = (k, hops_c, x_sys, st_name, hs_path)
            gi = gj
        if best is None:
            die("no route found")
        _, hops, x_sys, st_name, hs_path = best
        final_dock = st_name
        hs_go = [x_sys] + hs_path

    # Simulate and print
    tf = tc = waited = total_ly = 0.0
    iso_total = 0
    for i, (v, d, u, g) in enumerate(hops):
        if tc > 0:
            waited += tc
            tf = max(tf - tc, 0.0)
            tc = 0.0
        tf, tc = apply_jump(tf, d * factor)
        iso = a.fuel * d
        iso_int = ceil_eps(iso)
        iso_total += iso_int
        total_ly += d
        if i == len(hops) - 1:
            dock = final_dock
        else:
            dock = sys_stations[v][0][1]
        lines.append("UNDOCK")
        if g is not None:
            lines.append(f"GO: {systems[u]['name']} ({round_sec(systems[u]['sec']):.1f}) -> "
                         f"{systems[g]['name']} ({round_sec(systems[g]['sec']):.1f})")
        lines.append(f"JUMP {d:.2f} LY: {systems[v]['name']} ({ceil_eps(iso / 1000.0):,}K isotopes)")
        lines.append(f"DOCK: {dock}")
    if hs_go is not None:
        lines.append("UNDOCK")
        lines.append("GO: " + " -> ".join(
            f"{systems[v]['name']} ({round_sec(systems[v]['sec']):.1f})" for v in hs_go))
        lines.append(f"DOCK: {a.end}")
    elif not hops and a.start != a.end:
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
