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


def loc_sys(u, loc):
    if loc[0] == "G":
        return u.gates[loc[1]][0]
    if loc[0] == "S":
        return u.stations[loc[1]][1]
    return loc[1]


def loc_pos(u, loc):
    if loc[0] == "G":
        return u.gates[loc[1]][2]
    if loc[0] == "S":
        return u.stations[loc[1]][2]
    return None


def initial_loc(u, place):
    """Location state for a resolved place (as a starting point)."""
    if place[0] == "S":
        return ("S", place[1])
    s = place[1]
    gl = u.sys_gates.get(s, [])
    if gl:
        gid = min(gl, key=lambda g: (u.sys_name[u.gates[g][1]], g))
        return ("G", gid)
    return ("P", s)


def plan_leg(u, start_loc, start_lock, t0, target, ship, allowed=None):
    """Fastest path from a location to a target place.

    Returns (end_time, systems, end_state, warps, moved) or None.
    """
    align = ship["align"]
    top_speed = ship["top_speed"]
    warp_speed = ship["warp_speed"]
    dock = ship["dock"]
    gate = ship["gate"]
    zarzakh = u.name_to_sys.get(ZARZAKH)
    start_sys = loc_sys(u, start_loc)
    start_state = (start_loc, start_lock)

    if target[0] == "Y" and start_sys == target[1]:
        return t0, [start_sys], start_state, 0, False
    if target[0] == "S" and start_loc == ("S", target[1]):
        return t0, [start_sys], start_state, 0, False

    def dist_to(pos, p2):
        if pos is None:
            return 1.0
        return distance(pos, p2)

    best = {start_state: t0}
    parent = {start_state: None}
    warps = {start_state: 0}
    heap = [(t0, 0, start_state)]
    counter = 1
    goal = None
    while heap:
        t, _, state = heapq.heappop(heap)
        if best.get(state, math.inf) < t:
            continue
        loc, lock = state
        s = loc_sys(u, loc)
        if target[0] == "S":
            if loc == ("S", target[1]):
                goal = state
                break
        elif s == target[1] and loc[0] == "G":
            goal = state
            break
        pos = loc_pos(u, loc)
        pre = dock if loc[0] == "S" else 0.0

        def push(nstate, nt, w):
            nonlocal counter
            if nt < best.get(nstate, math.inf):
                best[nstate] = nt
                parent[nstate] = state
                warps[nstate] = warps[state] + w
                heapq.heappush(heap, (nt, counter, nstate))
                counter += 1

        for g in u.sys_gates.get(s, []):
            dest = u.gates[g][1]
            if allowed is not None and not allowed(dest):
                continue
            if loc == ("G", g):
                travel, w = 0.0, 0
            else:
                travel = pre + align + warp_time(dist_to(pos, u.gates[g][2]),
                                                 warp_speed, top_speed)
                w = 1
            jump_start = t + travel
            if s == zarzakh and lock is not None and dest != lock[0]:
                jump_start = max(jump_start, lock[1] + ZARZAKH_LOCK)
            nt = jump_start + gate
            arr_gate = u.gate_to.get((dest, s))
            if arr_gate is None:
                continue
            nlock = (s, nt) if dest == zarzakh else None
            push((("G", arr_gate), nlock), nt, w)
        for st in u.sys_stations.get(s, []):
            if loc == ("S", st):
                continue
            travel = pre + align + warp_time(dist_to(pos, u.stations[st][2]),
                                             warp_speed, top_speed) + dock
            push((("S", st), lock), t + travel, 1)

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
        s = loc_sys(u, st[0])
        if s != systems[-1]:
            systems.append(s)
    return best[goal], systems, goal, warps[goal], True


def sec_class(sec):
    r = round(sec + 1e-9, 1)
    if r >= 0.5:
        return "high"
    if r > 0.0:
        return "low"
    return "null"


def route_leg(u, start_loc, start_lock, t0, target, ship):
    if ship.get("type") != "Freighter":
        return plan_leg(u, start_loc, start_lock, t0, target, ship)
    res = plan_leg(u, start_loc, start_lock, t0, target, ship,
                   lambda s: sec_class(u.sys_sec.get(s, -1.0)) == "high")
    if res is None:
        res = plan_leg(u, start_loc, start_lock, t0, target, ship,
                       lambda s: sec_class(u.sys_sec.get(s, -1.0)) != "null")
    return res


EPS = 1e-6


class ConfigError(Exception):
    pass


def fmt_m3(v):
    if abs(v) < 0.005:
        v = 0.0
    return f"{v:,.2f} m3"


def _num(d, key, where, cond, desc, required=True):
    if key not in d or d[key] is None:
        if required:
            raise ConfigError(f"missing '{key}' in {where}")
        return None
    v = d[key]
    if isinstance(v, bool):
        raise ConfigError(f"'{key}' in {where} must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"'{key}' in {where} must be a number")
    if not math.isfinite(f) or not cond(f):
        raise ConfigError(f"'{key}' in {where} must be {desc}")
    return f


def load_yaml(path, what):
    try:
        import yaml
    except ImportError:
        raise ConfigError("PyYAML is required (pip install -r requirements.txt)")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError as e:
        raise ConfigError(f"could not read {what} '{path}': {e}")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {what} '{path}': {e}")


SHIP_TYPES = {
    "deep space transport": "Deep Space Transport",
    "blockade runner": "Blockade Runner",
    "freighter": "Freighter",
}


def load_config(path, ship_name):
    data = load_yaml(path, "config")
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    ships = data.get("ships")
    if not isinstance(ships, dict) or not ships:
        raise ConfigError("config has no 'ships'")
    times = data.get("times")
    if not isinstance(times, dict):
        raise ConfigError("config has no 'times'")
    key = None
    if ship_name in ships:
        key = ship_name
    else:
        for k in ships:
            if str(k).lower() == ship_name.lower():
                key = k
                break
    if key is None:
        raise ConfigError(f"unknown ship '{ship_name}'")
    sd = ships[key]
    where = f"ship '{key}'"
    if not isinstance(sd, dict):
        raise ConfigError(f"{where} must be a mapping")
    stype = sd.get("type")
    stype = SHIP_TYPES.get(str(stype).strip().lower()) if stype is not None else None
    if stype is None:
        raise ConfigError(f"{where} has invalid 'type'")
    ship = {
        "name": str(key),
        "type": stype,
        "align": _num(sd, "align", where, lambda f: f > 0, "> 0"),
        "top_speed": _num(sd, "top_speed", where, lambda f: f >= 0, ">= 0"),
        "warp_speed": _num(sd, "warp_speed", where, lambda f: f > 0, "> 0"),
        "cargo_size": _num(sd, "cargo_size", where, lambda f: f >= 0, ">= 0"),
        "dock": _num(times, "dock", "times", lambda f: f > 0, "> 0"),
        "gate": _num(times, "gate", "times", lambda f: f > 0, "> 0"),
        "move_cargo": _num(times, "move_cargo", "times", lambda f: f > 0, "> 0"),
    }
    ship["align"] = float(math.ceil(ship["align"]))
    return ship


def load_manifest(path):
    data = load_yaml(path, "manifest")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("manifest must be a mapping")
    start_cargo = _num(data, "start_cargo", "manifest", lambda f: f >= 0,
                       ">= 0", required=False) or 0.0
    wps = data.get("waypoints") or []
    if not isinstance(wps, list):
        raise ConfigError("manifest 'waypoints' must be a list")
    out = []
    for i, w in enumerate(wps):
        where = f"waypoint {i + 1}"
        if isinstance(w, str):
            out.append((w, None))
            continue
        if not isinstance(w, dict) or w.get("name") is None:
            raise ConfigError(f"{where} must have a 'name'")
        c = _num(w, "cargo", where, lambda f: f >= 0, ">= 0", required=False)
        out.append((str(w["name"]), c))
    return start_cargo, out


class Hauler:
    def __init__(self, u, ship):
        self.u = u
        self.ship = ship
        self.cache = {}

    def leg(self, loc, lock, t0, target):
        if lock is None:
            key = (loc, target)
            if key not in self.cache:
                self.cache[key] = route_leg(self.u, loc, None, 0.0, target, self.ship)
            r = self.cache[key]
            if r is None:
                return None
            t, systems, (eloc, elock), warps, moved = r
            if elock is not None:
                elock = (elock[0], elock[1] + t0)
            return t + t0, systems, (eloc, elock), warps, moved
        return route_leg(self.u, loc, lock, t0, target, self.ship)

    def run_trip(self, state, t, stops, end, end_name, lines):
        """Run a trip: visit stops [(place, name, amount)] then unload at end.

        Returns (state, time, cargo_warps, moved) or raises ConfigError.
        """
        u = self.u
        cargo = 0.0
        cargo_warps = 0
        mc = self.ship["move_cargo"]
        for place, name, amount in list(stops) + [(end, end_name, None)]:
            loc, lock = state
            r = self.leg(loc, lock, t, place)
            if r is None:
                raise ConfigError(f"no route to '{name}'")
            t, systems, state, warps, moved = r
            if moved:
                if cargo > EPS:
                    cargo_warps += warps
                if loc[0] == "S":
                    lines.append("UNDOCK")
                if len(systems) > 1:
                    lines.append("GO: " + " -> ".join(
                        f"{u.sys_name[s]} ({fmt_sec(u.sys_sec[s])})" for s in systems))
                if place[0] == "S":
                    lines.append(f"DOCK: {u.stations[place[1]][0]}")
            if amount is not None and amount > EPS:
                lines.append(f"LOAD: {fmt_m3(amount)}")
                t += mc
                cargo += amount
        delivered = cargo
        if cargo > EPS:
            lines.append(f"UNLOAD: {fmt_m3(cargo)}")
            t += mc
        return state, t, cargo_warps, delivered


def haul(u, start, end, start_name, end_name, ship, manifest):
    start_cargo, waypoints = manifest
    resolved = []
    for name, cargo in waypoints:
        p = resolve(u, name)
        if p is None:
            raise ConfigError(f"unknown waypoint location '{name}'")
        pname = u.stations[p[1]][0] if p[0] == "S" else u.sys_name[p[1]]
        resolved.append((p, pname, cargo))

    # Pickups in manifest order: (order index, place, name, amount)
    pickups = []
    if start_cargo > EPS:
        pickups.append((0, start, start_name, start_cargo))
    visits = []  # cargo-less waypoints (only visited on the first trip)
    for i, (p, pname, cargo) in enumerate(resolved, start=1):
        if cargo is not None and cargo > EPS:
            pickups.append((i, p, pname, cargo))
        else:
            visits.append((i, p, pname))

    cap = ship["cargo_size"]
    total = sum(pk[3] for pk in pickups)
    if total > EPS and cap <= EPS:
        raise ConfigError(f"ship '{ship['name']}' has no cargo capacity")

    # Next-fit packing with splitting: every trip but the last is full.
    trips = []  # each trip: tuple of (pickup idx, amount)
    cur, room = [], cap
    for k, pk in enumerate(pickups):
        left = pk[3]
        while left > EPS:
            if room <= EPS:
                trips.append(tuple(cur))
                cur, room = [], cap
            take = min(left, room)
            if left - take <= EPS:
                take = left
            cur.append((k, take))
            left -= take
            room -= take
    if cur:
        trips.append(tuple(cur))
    if not trips:
        trips = [()]

    hauler = Hauler(u, ship)
    start_state = (initial_loc(u, start), None)

    def stops_for(trip, first):
        items = [(pickups[k][0], pickups[k][1], pickups[k][2], amt) for k, amt in trip]
        if first:
            items += [(i, p, n, None) for i, p, n in visits]
        items.sort(key=lambda x: x[0])
        return [(p, n, a) for _, p, n, a in items]

    def finished(trip, remaining):
        return sum(1 for k, amt in trip if amt >= remaining[k] - EPS)

    def names(trip, first):
        return tuple(n for _, n, _ in stops_for(trip, first))

    def simulate(first_idx):
        remaining = [pk[3] for pk in pickups]
        lines = [f"START: {start_name}"]
        state, t = start_state, 0.0
        order = [first_idx]
        left = [i for i in range(len(trips)) if i != first_idx]
        moved_total = 0.0
        first_key = None
        trip_no = 1
        cur = first_idx
        while True:
            trip = trips[cur]
            fin = finished(trip, remaining)
            state, t, cw, moved = hauler.run_trip(
                state, t, stops_for(trip, trip_no == 1), end, end_name, lines)
            moved_total += moved
            if first_key is None:
                first_key = (-fin, cw, names(trip, True))
            for k, amt in trip:
                remaining[k] -= amt
            if not left:
                break
            # Choose the next trip greedily by the tie-break rules.
            best_i, best_key = None, None
            seen = {}
            for i in left:
                if trips[i] in seen:
                    continue
                seen[trips[i]] = True
                sub = []
                _, _, cw2, _ = hauler.run_trip(state, t, stops_for(trips[i], False),
                                               end, end_name, sub)
                key = (-finished(trips[i], remaining), cw2, names(trips[i], False))
                if best_key is None or key < best_key:
                    best_i, best_key = i, key
            left.remove(best_i)
            cur = best_i
            trip_no += 1
            lines.append(f"[--- TRIP {trip_no} ---]")
        return t, first_key, lines, moved_total

    best = None
    tried = set()
    for i, trip in enumerate(trips):
        if trip in tried:
            continue
        tried.add(trip)
        res = simulate(i)
        if best is None:
            best = res
            continue
        if res[0] < best[0] - EPS or (abs(res[0] - best[0]) <= EPS and res[1] < best[1]):
            best = res
    t, _, lines, moved_total = best
    lines.append(f"DONE: {fmt_time(t)}")
    if total > EPS:
        lines.append(f"MOVED: {fmt_m3(moved_total)}")
    return lines


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
    p.add_argument("--manifest")
    p.add_argument("--config")
    p.add_argument("--ship")
    p.add_argument("--align", type=positive)
    p.add_argument("--top-speed", type=non_negative)
    p.add_argument("--warp-speed", type=positive)
    p.add_argument("--dock-time", type=positive)
    p.add_argument("--gate-time", type=positive)
    p.add_argument("--sde", required=True)
    args = p.parse_args(argv)

    try:
        if args.config is not None:
            if not args.ship:
                p.error("--ship is required with --config")
            ship = load_config(args.config, args.ship)
        else:
            missing = [n for n in ("align", "top_speed", "warp_speed",
                                   "dock_time", "gate_time")
                       if getattr(args, n) is None]
            if missing:
                p.error("--config/--ship (or the legacy ship options) are required")
            if args.manifest is not None:
                p.error("--manifest requires --config and --ship")
            ship = {"name": "", "type": "", "align": float(math.ceil(args.align)),
                    "top_speed": args.top_speed, "warp_speed": args.warp_speed,
                    "cargo_size": 0.0, "dock": args.dock_time,
                    "gate": args.gate_time, "move_cargo": 0.0}
        manifest = (0.0, [])
        if args.manifest is not None:
            manifest = load_manifest(args.manifest)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        u = Universe(args.sde)
    except (FileNotFoundError, OSError, KeyError) as e:
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
    start_name = u.stations[start[1]][0] if start[0] == "S" else u.sys_name[start[1]]
    end_name = u.stations[end[1]][0] if end[0] == "S" else u.sys_name[end[1]]

    try:
        lines = haul(u, start, end, start_name, end_name, ship, manifest)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
