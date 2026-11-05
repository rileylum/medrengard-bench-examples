#!/usr/bin/env python3
"""Route planner for New Eden using the EVE Static Data Export."""
import argparse
import bz2
import csv
import heapq
import io
import math
import os
import sys

AU_IN_M = 149_597_870_700.0
ZARZAKH = "Zarzakh"
ZARZAKH_LOCK = 6 * 3600.0
STARGATE_GROUP = "10"


def open_csv(sde, name):
    path = os.path.join(sde, name)
    if os.path.exists(path):
        fh = bz2.open(path, "rt", encoding="utf-8", newline="")
    elif os.path.exists(path[:-4]):
        fh = open(path[:-4], "r", encoding="utf-8", newline="")
    else:
        raise FileNotFoundError(path)
    return fh


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Universe:
    def __init__(self, sde):
        self.sys_name = {}
        self.sys_sec = {}
        self.name_to_sys = {}
        with open_csv(sde, "mapSolarSystems.csv.bz2") as fh:
            for r in csv.DictReader(fh):
                sid = int(r["solarSystemID"])
                self.sys_name[sid] = r["solarSystemName"]
                self.sys_sec[sid] = fnum(r["security"])
                self.name_to_sys.setdefault(r["solarSystemName"], sid)

        self.neighbors = {}
        with open_csv(sde, "mapSolarSystemJumps.csv.bz2") as fh:
            for r in csv.DictReader(fh):
                a = int(r["fromSolarSystemID"])
                b = int(r["toSolarSystemID"])
                self.neighbors.setdefault(a, set()).add(b)
                self.neighbors.setdefault(b, set()).add(a)

        self.stations = {}  # id -> (name, system, pos)
        self.name_to_station = {}
        self.sys_stations = {}
        with open_csv(sde, "staStations.csv.bz2") as fh:
            for r in csv.DictReader(fh):
                st = int(r["stationID"])
                s = int(r["solarSystemID"])
                pos = (fnum(r["x"]), fnum(r["y"]), fnum(r["z"]))
                self.stations[st] = (r["stationName"], s, pos)
                self.name_to_station.setdefault(r["stationName"], st)
                self.sys_stations.setdefault(s, []).append(st)

        # Stargates: gate id -> (system, destination system, pos)
        self.gates = {}
        self.sys_gates = {}
        self.gate_to = {}  # (from_sys, to_sys) -> gate id in from_sys
        raw = []
        with open_csv(sde, "mapDenormalize.csv.bz2") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            ix = {h: i for i, h in enumerate(header)}
            gi, si = ix["groupID"], ix["solarSystemID"]
            ii, ni = ix["itemID"], ix["itemName"]
            xi, yi, zi = ix["x"], ix["y"], ix["z"]
            for r in reader:
                if r[gi] != STARGATE_GROUP:
                    continue
                raw.append((int(r[ii]), int(r[si]), r[ni],
                            (fnum(r[xi]), fnum(r[yi]), fnum(r[zi]))))
        for gid, s, name, pos in raw:
            dest_name = name
            if name.startswith("Stargate (") and name.endswith(")"):
                dest_name = name[len("Stargate ("):-1]
            dest = None
            for n in self.neighbors.get(s, ()):
                if self.sys_name.get(n) == dest_name:
                    dest = n
                    break
            if dest is None:
                dest = self.name_to_sys.get(dest_name)
            if dest is None:
                continue
            self.gates[gid] = (s, dest, pos)
            self.sys_gates.setdefault(s, []).append(gid)
            self.gate_to[(s, dest)] = gid


def warp_time(dist, warp_speed, top_speed):
    """Warp time in seconds for a distance in meters."""
    if dist <= 0:
        return 0.0
    ka = warp_speed
    kd = min(warp_speed / 3.0, 2.0)
    v_drop = min(top_speed / 2.0, 100.0)
    if v_drop <= 0:
        v_drop = 1e-9
    v_warp = warp_speed * AU_IN_M
    d_a = AU_IN_M
    d_d = v_warp / kd
    d_min = d_a + d_d
    cruise = 0.0
    if dist < d_min:
        v_warp = dist * ka * kd / (ka + kd)
    else:
        cruise = (dist - d_min) / v_warp
    t_accel = math.log(v_warp / ka) / ka
    t_decel = math.log(v_warp / v_drop) / kd
    return t_accel + cruise + t_decel


def distance(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def fmt_sec(sec):
    r = round(sec + 1e-9, 1)
    if r == 0:
        r = 0.0
    return f"{r:.1f}"


def fmt_time(seconds):
    minutes = int(math.ceil(seconds / 60.0 - 1e-9)) if seconds > 0 else 0
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def resolve(u, name):
    if name in u.name_to_station:
        return ("S", u.name_to_station[name])
    if name in u.name_to_sys:
        return ("Y", u.name_to_sys[name])
    low = name.lower()
    for n, st in u.name_to_station.items():
        if n.lower() == low:
            return ("S", st)
    for n, s in u.name_to_sys.items():
        if n.lower() == low:
            return ("Y", s)
    return None


def plan(u, start, end, align, top_speed, warp_speed, dock, gate):
    zarzakh = u.name_to_sys.get(ZARZAKH)

    # Location: ("G", gate_id) in space at gate, ("S", station_id) docked.
    def loc_sys(loc):
        return u.gates[loc[1]][0] if loc[0] == "G" else u.stations[loc[1]][1]

    def loc_pos(loc):
        return u.gates[loc[1]][2] if loc[0] == "G" else u.stations[loc[1]][2]

    if start[0] == "S":
        start_loc = ("S", start[1])
        start_sys = u.stations[start[1]][1]
    else:
        start_sys = start[1]
        gl = u.sys_gates.get(start_sys, [])
        if gl:
            gid = min(gl, key=lambda g: (u.sys_name[u.gates[g][1]], g))
            start_loc = ("G", gid)
        else:
            start_loc = None

    # Trivial cases
    if end[0] == "Y" and start_sys == end[1]:
        return 0.0, [start_sys], False
    if end[0] == "S" and start[0] == "S" and start[1] == end[1]:
        return 0.0, [start_sys], False
    if start_loc is None:
        # No gates: only possible move is to a station in the same system.
        if end[0] == "S" and u.stations[end[1]][1] == start_sys:
            return align + warp_time(1.0, warp_speed, top_speed) + dock, [start_sys], True
        return None

    # state = (loc, lock) ; lock = (from_sys, t0) only while in Zarzakh
    start_state = (start_loc, None)
    best = {start_state: 0.0}
    parent = {start_state: None}
    heap = [(0.0, 0, start_state)]
    counter = 1
    goal = None
    while heap:
        t, _, state = heapq.heappop(heap)
        if best.get(state, math.inf) < t:
            continue
        loc, lock = state
        s = loc_sys(loc)
        if end[0] == "S":
            if loc == ("S", end[1]):
                goal = state
                break
        elif s == end[1] and loc[0] == "G":
            goal = state
            break
        pos = loc_pos(loc)
        pre = dock if loc[0] == "S" else 0.0

        def push(nstate, nt):
            nonlocal counter
            if nt < best.get(nstate, math.inf):
                best[nstate] = nt
                parent[nstate] = state
                heapq.heappush(heap, (nt, counter, nstate))
                counter += 1

        # Gate jumps
        for g in u.sys_gates.get(s, []):
            dest = u.gates[g][1]
            if loc == ("G", g):
                travel = 0.0
            else:
                travel = pre + align + warp_time(distance(pos, u.gates[g][2]),
                                                 warp_speed, top_speed)
            jump_start = t + travel
            if s == zarzakh and lock is not None and dest != lock[0]:
                jump_start = max(jump_start, lock[1] + ZARZAKH_LOCK)
            nt = jump_start + gate
            arr_gate = u.gate_to.get((dest, s))
            if arr_gate is None:
                continue
            nlock = (s, nt) if dest == zarzakh else None
            push((("G", arr_gate), nlock), nt)
        # Docking at stations in the system
        for st in u.sys_stations.get(s, []):
            if loc == ("S", st):
                continue
            travel = pre + align + warp_time(distance(pos, u.stations[st][2]),
                                             warp_speed, top_speed) + dock
            push((("S", st), lock), t + travel)

    if goal is None:
        return None
    chain = []
    st = goal
    while st is not None:
        chain.append(st)
        st = parent[st]
    chain.reverse()
    systems = [start_sys]
    for st in chain[1:]:
        s = loc_sys(st[0])
        if s != systems[-1]:
            systems.append(s)
    return best[goal], systems, True


def positive(v):
    f = float(v)
    if not math.isfinite(f) or f <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0: {v}")
    return f


def non_negative(v):
    f = float(v)
    if not math.isfinite(f) or f < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0: {v}")
    return f


def main(argv=None):
    p = argparse.ArgumentParser(description="Plan a hauling route in New Eden.")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("--align", type=positive, required=True)
    p.add_argument("--top-speed", type=non_negative, required=True)
    p.add_argument("--warp-speed", type=positive, required=True)
    p.add_argument("--dock-time", type=positive, required=True)
    p.add_argument("--gate-time", type=positive, required=True)
    p.add_argument("--sde", required=True)
    args = p.parse_args(argv)

    try:
        u = Universe(args.sde)
    except (FileNotFoundError, OSError) as e:
        print(f"Error: could not load SDE: {e}", file=sys.stderr)
        return 1

    start = resolve(u, args.start)
    if start is None:
        print(f"Error: unknown start location '{args.start}'", file=sys.stderr)
        return 1
    end = resolve(u, args.end)
    if end is None:
        print(f"Error: unknown end location '{args.end}'", file=sys.stderr)
        return 1

    align = float(math.ceil(args.align))
    res = plan(u, start, end, align, args.top_speed, args.warp_speed,
               args.dock_time, args.gate_time)
    if res is None:
        print(f"Error: no route from '{args.start}' to '{args.end}'", file=sys.stderr)
        return 1
    total, systems, moved = res

    out = []
    if start[0] == "S":
        out.append(f"START: {u.stations[start[1]][0]}")
        if moved:
            out.append("UNDOCK")
    else:
        out.append(f"START: {u.sys_name[start[1]]}")
    if len(systems) > 1:
        out.append("GO: " + " -> ".join(
            f"{u.sys_name[s]} ({fmt_sec(u.sys_sec[s])})" for s in systems))
    if end[0] == "S":
        out.append(f"DOCK: {u.stations[end[1]][0]}")
    out.append(f"DONE: {fmt_time(total)}")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
