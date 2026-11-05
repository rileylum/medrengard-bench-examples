#!/usr/bin/env python3
"""EVE market tools API server (stdlib only)."""
import argparse
import bz2
import csv
import gzip
import io
import json
import os
import math
import re
import secrets
import sys
import threading
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

csv.field_size_limit(sys.maxsize)

PRICE_SCHEMA = {
    "order_id": "int",
    "duration": "int",
    "is_buy_order": "bool",
    "issued": "datetime",
    "location_id": "int",
    "min_volume": "int",
    "price": "float",
    "range": "str",
    "system_id": "int",
    "type_id": "int",
    "volume_remain": "int",
    "volume_total": "int",
}
STATION_SCHEMA = {"location_id": "int", "type": "str", "name": "str"}

HUBS = [
    ("jita", "Jita IV - Moon 4 - Caldari Navy Assembly Plant", 60003760),
    ("amarr", "Amarr VIII (Oris) - Emperor Family Academy", 60008494),
    ("dodixie", "Dodixie IX - Moon 20 - Federation Navy Assembly Plant", 60011866),
    ("rens", "Rens VI - Moon 8 - Brutor Tribe Treasury", 60004588),
    ("hek", "Hek VIII - Moon 12 - Boundless Creation Factory", 60005686),
]


class ApiError(Exception):
    def __init__(self, status, error, details):
        super().__init__(details)
        self.status = status
        self.error = error
        self.details = details


# --------------------------------------------------------------------------
# SDE
# --------------------------------------------------------------------------

def _open_sde(sde_dir, base):
    for ext, opener in ((".csv.bz2", bz2.open), (".csv.gz", gzip.open), (".csv", open)):
        p = os.path.join(sde_dir, base + ext)
        if os.path.exists(p):
            return opener(p, "rt", encoding="utf-8", newline="")
    return None


class SDE:
    def __init__(self, sde_dir):
        self.types = {}  # type_id -> name
        self.type_by_name = {}
        self.regions = {}  # region_id -> name
        self.region_by_name = {}
        self.systems = {}  # system_id -> (name, region_id)
        self.system_by_name = {}
        self.stations = {}  # station_id -> (name, system_id, region_id)
        self.station_by_name = {}
        self.type_exact = {}
        self.type_group = {}
        self.type_volume = {}
        self.type_portion = {}
        self.group_category = {}
        self.group_name = {}
        self.materials = {}  # type_id -> [(material_id, qty)]
        self.reprocess_skill = {}  # type_id -> skill type_id
        self.ship_volumes = {}  # group name -> packaged volume

        f = _open_sde(sde_dir, "invTypes")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        tid = int(row["typeID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    name = row.get("typeName") or ""
                    self.types[tid] = name
                    self.type_by_name.setdefault(name.lower(), tid)
                    self.type_exact.setdefault(name, tid)
                    try:
                        self.type_group[tid] = int(row.get("groupID") or 0)
                    except ValueError:
                        self.type_group[tid] = 0
                    try:
                        self.type_volume[tid] = float(row.get("volume") or 0)
                    except ValueError:
                        self.type_volume[tid] = 0.0
                    try:
                        self.type_portion[tid] = max(1, int(float(row.get("portionSize") or 1)))
                    except ValueError:
                        self.type_portion[tid] = 1
        f = _open_sde(sde_dir, "mapRegions")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        rid = int(row["regionID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.regions[rid] = row["regionName"]
                    self.region_by_name[row["regionName"].lower()] = rid
        f = _open_sde(sde_dir, "mapSolarSystems")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        sid = int(row["solarSystemID"])
                        rid = int(row["regionID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.systems[sid] = (row["solarSystemName"], rid)
                    self.system_by_name[row["solarSystemName"].lower()] = sid
        f = _open_sde(sde_dir, "staStations")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        stid = int(row["stationID"])
                        sysid = int(row["solarSystemID"])
                        rid = int(row["regionID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.stations[stid] = (row["stationName"], sysid, rid)
                    self.station_by_name[row["stationName"]] = stid
        f = _open_sde(sde_dir, "invGroups")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        gid = int(row["groupID"])
                        cid = int(row["categoryID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.group_category[gid] = cid
                    self.group_name[gid] = row.get("groupName") or ""
        f = _open_sde(sde_dir, "invTypeMaterials")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        tid = int(row["typeID"])
                        mid = int(row["materialTypeID"])
                        q = int(float(row["quantity"]))
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.materials.setdefault(tid, []).append((mid, q))
        f = _open_sde(sde_dir, "dgmTypeAttributes")
        if f:
            with f:
                for line in f:
                    # fast path: only attribute 790 (reprocessingSkillType)
                    if ",790," not in line:
                        continue
                    parts = line.strip().split(",")
                    if len(parts) < 4 or parts[1] != "790":
                        continue
                    val = parts[2] if parts[2] not in ("", "None") else parts[3]
                    try:
                        self.reprocess_skill[int(parts[0])] = int(float(val))
                    except ValueError:
                        continue
        self.jumps = {}  # system_id -> set(system_id)
        f = _open_sde(sde_dir, "mapSolarSystemJumps")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        a = int(row["fromSolarSystemID"])
                        b = int(row["toSolarSystemID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.jumps.setdefault(a, set()).add(b)
                    self.jumps.setdefault(b, set()).add(a)
        yp = os.path.join(sde_dir, "ship_volumes.yaml")
        if os.path.exists(yp):
            with open(yp, encoding="utf-8") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if not line or ":" not in line:
                        continue
                    k, v = line.rsplit(":", 1)
                    k = k.strip().strip("'\"")
                    try:
                        self.ship_volumes[k] = float(v.strip().strip("'\""))
                    except ValueError:
                        continue
        # sorted system names (longest first) for structure prefix matching
        self._sys_names_sorted = sorted(self.system_by_name.keys(), key=len, reverse=True)

    def system_from_structure_name(self, name):
        low = name.lower().strip()
        # Typical format: "<System> - <Structure name>"
        if " - " in low:
            cand = low.split(" - ", 1)[0].strip()
            if cand in self.system_by_name:
                return self.system_by_name[cand]
        for sn in self._sys_names_sorted:
            if low.startswith(sn):
                rest = low[len(sn):]
                if not rest or not (rest[0].isalnum()):
                    return self.system_by_name[sn]
        return None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_bool(v):
    s = v.strip().lower()
    if s in ("true", "1", "t", "yes", "y"):
        return True
    if s in ("false", "0", "f", "no", "n"):
        return False
    raise ValueError("invalid boolean %r" % v)


def parse_int(v):
    s = v.strip()
    try:
        return int(s)
    except ValueError:
        f = float(s)
        if f != int(f):
            raise ValueError("invalid integer %r" % v)
        return int(f)


def parse_datetime(v):
    s = v.strip()
    if not s:
        raise ValueError("empty datetime")
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1) if "T" not in s and " " in s else s
    # Python <3.11 compatibility for fractional seconds of odd lengths
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", s)
    if m:
        frac = (m.group(2) + "000000")[:6]
        s = m.group(1) + "." + frac + m.group(3)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


CONVERTERS = {
    "int": parse_int,
    "float": lambda v: float(v.strip()),
    "bool": parse_bool,
    "str": lambda v: v,
    "datetime": parse_datetime,
}


def read_gzip_csv(body, schema):
    if not body:
        raise ApiError(400, "INVALID_FORMAT", "Request body is empty")
    try:
        raw = gzip.decompress(body)
    except (OSError, EOFError, zlib.error) as e:
        raise ApiError(400, "INVALID_FORMAT", "Failed to decompress gzip data: %s" % e)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ApiError(400, "INVALID_FORMAT", "CSV is not valid UTF-8: %s" % e)
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, None)
        if header is None:
            raise ApiError(400, "INVALID_FORMAT", "CSV is empty; missing header row")
        header = [h.strip() for h in header]
        expected = list(schema.keys())
        if sorted(header) != sorted(expected) or len(set(header)) != len(header):
            missing = [c for c in expected if c not in header]
            extra = [c for c in header if c not in schema]
            parts = []
            if missing:
                parts.append("missing columns: %s" % ", ".join(missing))
            if extra:
                parts.append("unexpected columns: %s" % ", ".join(extra))
            if len(set(header)) != len(header):
                parts.append("duplicate columns")
            raise ApiError(400, "INVALID_FORMAT",
                           "CSV columns do not match the required schema (%s); expected: %s"
                           % ("; ".join(parts), ", ".join(expected)))
        rows = []
        for lineno, rec in enumerate(reader, start=2):
            if not rec or (len(rec) == 1 and rec[0].strip() == ""):
                continue
            if len(rec) != len(header):
                raise ApiError(400, "INVALID_FORMAT",
                               "Row %d has %d fields, expected %d" % (lineno, len(rec), len(header)))
            out = {}
            for col, val in zip(header, rec):
                kind = schema[col]
                try:
                    out[col] = CONVERTERS[kind](val)
                except (ValueError, TypeError) as e:
                    raise ApiError(400, "INVALID_FORMAT",
                                   "Row %d: invalid value %r for column '%s' (expected %s): %s"
                                   % (lineno, val, col, kind, e))
            rows.append(out)
        return rows
    except csv.Error as e:
        raise ApiError(400, "INVALID_FORMAT", "CSV parsing error: %s" % e)


# --------------------------------------------------------------------------
# Market computations
# --------------------------------------------------------------------------

def billions(v):
    return round(v / 1e9, 2)


def filter_orders(orders):
    """Split orders into filtered buy/sell lists, returning thresholds.

    Buy orders priced below 10% of the highest buy are outliers; sell orders
    priced above 10x the lowest sell are outliers.
    """
    buys = [o for o in orders if o["is_buy_order"]]
    sells = [o for o in orders if not o["is_buy_order"]]
    buy_thr = sell_thr = None
    if buys:
        buy_thr = max(o["price"] for o in buys) * 0.1
        buys = [o for o in buys if o["price"] >= buy_thr]
    if sells:
        sell_thr = min(o["price"] for o in sells) * 10.0
        sells = [o for o in sells if o["price"] <= sell_thr]
    return buys, sells, buy_thr, sell_thr


def filter_by_type(orders):
    """Apply outlier filtering per type. Returns (buys, sells) lists."""
    by_type = {}
    for o in orders:
        by_type.setdefault(o["type_id"], []).append(o)
    buys, sells = [], []
    for tid, lst in by_type.items():
        b, s, _, _ = filter_orders(lst)
        buys.extend(b)
        sells.extend(s)
    return buys, sells


def pct5(orders, buy):
    if not orders:
        return None
    srt = sorted(orders, key=lambda o: o["price"], reverse=buy)
    total = sum(o["volume_remain"] for o in srt)
    if total <= 0:
        return None
    target = total * 0.05
    acc = 0.0
    value = 0.0
    for o in srt:
        take = min(o["volume_remain"], target - acc)
        if take <= 0:
            break
        acc += take
        value += take * o["price"]
        if acc >= target:
            break
    if acc <= 0:
        return None
    return value / acc


def type_stats(orders):
    buys, sells, buy_thr, sell_thr = filter_orders(orders)
    buy = max((o["price"] for o in buys), default=None)
    sell = min((o["price"] for o in sells), default=None)
    sell_5 = pct5(sells, buy=False)
    buy_5 = pct5(buys, buy=True)
    sell_vol = sum(o["volume_remain"] for o in sells)
    buy_vol = sum(o["volume_remain"] for o in buys)
    total_value = sell_5 * sell_vol if sell_5 is not None else None
    return {
        "buy": buy,
        "sell": sell,
        "buy_5pct": buy_5,
        "sell_5pct": sell_5,
        "buy_orders": len(buys),
        "sell_orders": len(sells),
        "buy_threshold": buy_thr,
        "sell_threshold": sell_thr,
        "buy_volume": buy_vol,
        "sell_volume": sell_vol,
        "total_value": total_value,
    }


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class State:
    def __init__(self, sde):
        self.sde = sde
        self.lock = threading.RLock()
        self.books = {}  # market -> {order_id: order}
        self.stations = {}  # location_id -> {"name", "type", "system_id", "region_id"}
        self.configs = {}  # api key -> config
        self.effective_ts = None  # max issued timestamp of latest price upload
        self.history = {}  # (region_id, type_id) -> {date: (average, volume)}
        self.history_max_date = None

    def all_orders(self):
        merged = {}
        for book in self.books.values():
            for oid, o in book.items():
                cur = merged.get(oid)
                if cur is None or o["issued"] >= cur["issued"]:
                    merged[oid] = o
        return list(merged.values())

    def region_of_order(self, o):
        sysinfo = self.sde.systems.get(o["system_id"])
        if sysinfo:
            return sysinfo[1]
        st = self.sde.stations.get(o["location_id"])
        if st:
            return st[2]
        st = self.stations.get(o["location_id"])
        if st:
            return st.get("region_id")
        return None

    def station_name(self, loc):
        st = self.sde.stations.get(loc)
        if st:
            return st[0]
        st = self.stations.get(loc)
        if st:
            return st["name"]
        return str(loc)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def get_param(query, headers, name):
    if name in query and query[name]:
        return query[name][0]
    for hn in (name, "x-" + name, "x-" + name.replace("_", "-"), name.replace("_", "-")):
        v = headers.get(hn)
        if v is not None:
            return v
    return None


def handle_prices(state, query, headers, body):
    market = get_param(query, headers, "market")
    market = (market or "jita").strip().lower() or "jita"
    mode = (get_param(query, headers, "mode") or "replace").strip().lower()
    if mode not in ("replace", "append"):
        raise ApiError(400, "INVALID_FORMAT", "mode must be 'replace' or 'append', got %r" % mode)
    loc = get_param(query, headers, "location_id")
    loc_id = None
    if loc is not None and str(loc).strip() != "":
        try:
            loc_id = int(str(loc).strip())
        except ValueError:
            raise ApiError(400, "INVALID_FORMAT", "location_id must be an integer, got %r" % loc)
    rows = read_gzip_csv(body, PRICE_SCHEMA)
    eff_ts = max((r["issued"] for r in rows), default=None)
    if eff_ts is not None:
        with state.lock:
            state.effective_ts = eff_ts
    if loc_id is not None:
        rows = [r for r in rows if r["location_id"] == loc_id]
    with state.lock:
        if mode == "replace":
            book = {}
            for r in rows:
                cur = book.get(r["order_id"])
                if cur is None or r["issued"] >= cur["issued"]:
                    book[r["order_id"]] = r
            state.books[market] = book
        else:
            book = state.books.setdefault(market, {})
            for r in rows:
                cur = book.get(r["order_id"])
                if cur is None or r["issued"] >= cur["issued"]:
                    book[r["order_id"]] = r
    return 200, {"status": "PRICES_UPDATED", "count": len(rows)}


def handle_stations(state, query, headers, body):
    rows = read_gzip_csv(body, STATION_SCHEMA)
    for i, r in enumerate(rows, start=2):
        t = r["type"].strip()
        if t.lower() not in ("structure", "station"):
            raise ApiError(400, "INVALID_FORMAT",
                           "Row %d: type must be 'Structure' or 'Station', got %r" % (i, r["type"]))
    with state.lock:
        for r in rows:
            name = r["name"]
            sysid = None
            rid = None
            sde_st = state.sde.stations.get(r["location_id"])
            if sde_st:
                sysid, rid = sde_st[1], sde_st[2]
            else:
                sysid = state.sde.system_from_structure_name(name)
                if sysid is not None:
                    rid = state.sde.systems[sysid][1]
            state.stations[r["location_id"]] = {
                "name": name,
                "type": r["type"].strip(),
                "system_id": sysid,
                "region_id": rid,
            }
    return 200, {"status": "STATIONS_UPDATED", "count": len(rows)}


def resolve_region(state, token):
    t = unquote(token).strip()
    try:
        rid = int(t)
    except ValueError:
        rid = state.sde.region_by_name.get(t.lower())
    if rid is None or rid not in state.sde.regions:
        raise ApiError(404, "UNKNOWN_ITEMS", "Unknown region: %s" % t)
    return rid


def resolve_type(state, token):
    t = unquote(str(token)).strip()
    try:
        tid = int(t)
    except ValueError:
        tid = state.sde.type_by_name.get(t.lower())
    if tid is None or tid not in state.sde.types:
        return None
    return tid


def region_orders(state, rid):
    return [o for o in state.all_orders() if state.region_of_order(o) == rid]


def handle_region(state, rid, query):
    with state.lock:
        orders = region_orders(state, rid)

    # validate params first
    type_ids = None
    if "type_ids" in query:
        raw = ",".join(query["type_ids"])
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        type_ids = []
        unknown = []
        for tok in tokens:
            tid = resolve_type(state, tok)
            if tid is None:
                unknown.append(tok)
            else:
                type_ids.append(tid)
        if unknown:
            raise ApiError(404, "UNKNOWN_ITEMS", "Unknown type ids: %s" % ", ".join(unknown))
    hubs_n = None
    if "hubs" in query:
        try:
            hubs_n = int(query["hubs"][0])
        except ValueError:
            raise ApiError(400, "INVALID_FORMAT", "hubs must be an integer")
        if hubs_n < 0:
            raise ApiError(400, "INVALID_FORMAT", "hubs must be non-negative")

    buys, sells = filter_by_type(orders)
    result = {
        "name": state.sde.regions[rid],
        "sell_orders": len(sells),
        "buy_orders": len(buys),
        "sell_value": billions(sum(o["price"] * o["volume_remain"] for o in sells)),
        "buy_value": billions(sum(o["price"] * o["volume_remain"] for o in buys)),
        "unique_items": len({o["type_id"] for o in buys} | {o["type_id"] for o in sells}),
    }

    # Station sell-order counts (post filtering) to determine the regional hub
    station_counts = {}
    station_values = {}
    for o in sells:
        loc = o["location_id"]
        station_counts[loc] = station_counts.get(loc, 0) + 1
        station_values[loc] = station_values.get(loc, 0.0) + o["price"] * o["volume_remain"]
    ranked = sorted(station_counts.keys(), key=lambda l: (-station_counts[l], l))
    hub_loc = ranked[0] if ranked else None
    if hub_loc is None and orders:
        # fall back to location with most orders of any kind
        cnt = {}
        for o in orders:
            cnt[o["location_id"]] = cnt.get(o["location_id"], 0) + 1
        hub_loc = sorted(cnt.keys(), key=lambda l: (-cnt[l], l))[0]

    if type_ids is not None:
        hub_orders_by_type = {}
        for o in orders:
            if o["location_id"] == hub_loc:
                hub_orders_by_type.setdefault(o["type_id"], []).append(o)
        types = {}
        for tid in type_ids:
            lst = hub_orders_by_type.get(tid, [])
            b, s, _, _ = filter_orders(lst)
            buy = max((o["price"] for o in b), default=None)
            sell = min((o["price"] for o in s), default=None)
            split = (buy + sell) / 2 if buy is not None and sell is not None else None
            types[state.sde.types[tid]] = {
                "buy": buy,
                "sell": sell,
                "split": split,
                "buy_orders": len(b),
                "sell_orders": len(s),
            }
        result["types"] = types

    if hubs_n is not None:
        result["hubs"] = [
            {
                "station": state.station_name(loc),
                "orders": station_counts[loc],
                "sell_value": billions(station_values[loc]),
            }
            for loc in ranked[:hubs_n]
        ]
    return 200, result


def handle_region_type(state, rid, ttoken):
    tid = resolve_type(state, ttoken)
    if tid is None:
        raise ApiError(404, "UNKNOWN_ITEMS", "Unknown type id: %s" % unquote(ttoken))
    with state.lock:
        orders = [o for o in region_orders(state, rid) if o["type_id"] == tid]
    stats = type_stats(orders)
    out = {"name": state.sde.types[tid]}
    out.update(stats)
    return 200, out


def handle_hub_compare(state, ttoken):
    tid = resolve_type(state, ttoken)
    if tid is None:
        raise ApiError(404, "UNKNOWN_ITEMS", "Unknown type id: %s" % unquote(ttoken))
    with state.lock:
        all_orders = state.all_orders()
        books = {k: list(v.values()) for k, v in state.books.items()}
    by_loc = {}
    for o in all_orders:
        by_loc.setdefault(o["location_id"], []).append(o)
    out = {}
    have_any = False
    for key, _name, loc in HUBS:
        hub_orders = by_loc.get(loc)
        if not hub_orders:
            book = books.get(key)
            if book:
                hub_orders = book
        if not hub_orders:
            continue
        have_any = True
        typed = [o for o in hub_orders if o["type_id"] == tid]
        if not typed:
            for suffix in ("sell", "buy", "sell_volume", "buy_volume", "value",
                           "sell_5pct", "buy_5pct"):
                out["%s_%s" % (key, suffix)] = None
            continue
        st = type_stats(typed)
        has_sell = st["sell_orders"] > 0
        has_buy = st["buy_orders"] > 0
        out[key + "_sell"] = st["sell"]
        out[key + "_buy"] = st["buy"]
        out[key + "_sell_volume"] = st["sell_volume"] if has_sell else None
        out[key + "_buy_volume"] = st["buy_volume"] if has_buy else None
        out[key + "_value"] = (billions(st["sell_5pct"] * st["sell_volume"])
                               if st["sell_5pct"] is not None else None)
        out[key + "_sell_5pct"] = st["sell_5pct"]
        out[key + "_buy_5pct"] = st["buy_5pct"]
    if not have_any:
        raise ApiError(404, "NO_PRICE_DATA", "No price data available for any of the main trade hubs")
    return 200, out


# --------------------------------------------------------------------------
# Reprocessing
# --------------------------------------------------------------------------

class ValueApiError(Exception):
    """Error rendered as {"code", "path", "message"}."""

    def __init__(self, status, code, path=None, message=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.path = path
        self.message = message

    def payload(self):
        out = {"code": self.code}
        if self.path is not None:
            out["path"] = self.path
        if self.message is not None:
            out["message"] = self.message
        return out


STRUCTURE_SM = {"npc_station": 0.0, "citadel": 0.0, "athanor": 0.02, "tatara": 0.055}
RIG_RM = {None: 0.0, "t1": 1.0, "t2": 3.0}
SECURITY_SEC = {"highsec": 0.0, "lowsec": 0.06, "nullsec": 0.12}
IMPLANT_IM = {None: 0.0, "RX-801": 0.01, "RX-802": 0.02, "RX-804": 0.04}
GAS_GROUPS = {711, 4168}
ORE_CATEGORY = 25
SHIP_CATEGORY = 6
DEFAULT_EFF = {"ore": 0.9063, "gas": 0.95, "scrap": 0.55}
MARKET_KEYS = {k: (k, loc) for k, _n, loc in HUBS}
SCRAP_SKILL = "Scrapmetal Processing"
EPS = 1e-9


def _enum_error(path, value, allowed):
    shown = ", ".join("null" if a is None else '"%s"' % a for a in allowed)
    return ValueApiError(404, "INVALID_VALUE", path,
                         "Invalid value %s for '%s'; expected one of: %s"
                         % (json.dumps(value), path, shown))


def parse_config(body):
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueApiError(404, "INVALID_VALUE", "", "Request body must be a JSON object")
    structure = body.get("structure")
    if structure is None:
        structure = {}
    if not isinstance(structure, dict):
        raise ValueApiError(404, "INVALID_VALUE", "structure", "structure must be an object or null")
    stype = structure.get("type")
    if stype is None:
        stype = "npc_station"
    if not isinstance(stype, str) or stype not in STRUCTURE_SM:
        raise _enum_error("structure.type", stype, list(STRUCTURE_SM))
    rig = structure.get("rig")
    if rig is not None and (not isinstance(rig, str) or rig not in RIG_RM):
        raise _enum_error("structure.rig", rig, list(RIG_RM))
    if stype == "npc_station" and rig is not None:
        raise ValueApiError(404, "INVALID_VALUE", "structure.rig",
                            "npc_station cannot specify a rig; rig must be null")
    sec = structure.get("security")
    if sec is None:
        sec = "highsec"
    if not isinstance(sec, str) or sec not in SECURITY_SEC:
        raise _enum_error("structure.security", sec, list(SECURITY_SEC))
    skills = body.get("skills")
    if skills is None:
        skills = {}
    if not isinstance(skills, dict):
        raise ValueApiError(404, "INVALID_VALUE", "skills", "skills must be an object mapping names to levels")
    clean_skills = {}
    for name, lvl in skills.items():
        if lvl is None:
            lvl = 0
        if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) or \
                (isinstance(lvl, float) and lvl != int(lvl)) or not 0 <= lvl <= 5:
            raise ValueApiError(404, "INVALID_VALUE", "skills.%s" % name,
                                "Skill level for '%s' must be an integer between 0 and 5, got %s"
                                % (name, json.dumps(lvl)))
        clean_skills[name] = int(lvl)
    implant = body.get("implant")
    if implant is not None and (not isinstance(implant, str) or implant not in IMPLANT_IM):
        raise _enum_error("implant", implant, list(IMPLANT_IM))
    tax = body.get("sales_tax")
    if tax is not None:
        if isinstance(tax, bool) or not isinstance(tax, (int, float)) or tax < 0 or tax > 100:
            raise ValueApiError(404, "INVALID_VALUE", "sales_tax",
                                "sales_tax must be a number between 0 and 1, got %s" % json.dumps(tax))
        tax = float(tax) / 100.0 if tax > 1 else float(tax)
    return {
        "structure": {"type": stype, "rig": rig, "security": sec},
        "skills": clean_skills,
        "implant": implant,
        "sales_tax": tax,
    }


def _skill_level(skills, name):
    if name is None:
        return 0
    if name in skills:
        return skills[name]
    low = name.lower()
    for k, v in skills.items():
        if k.lower() == low:
            return v
    return 0


class YieldModel:
    """Computes the efficiency multiplier for each type."""

    def __init__(self, sde, efficiency=None, config=None):
        self.sde = sde
        self.eff = efficiency or dict(DEFAULT_EFF)
        self.config = config
        self.used = {"ore": [], "gas": [], "scrap": []}
        if config is not None:
            st = config["structure"]
            sk = config["skills"]
            self._base = ((50.0 + RIG_RM[st["rig"]])
                          * (1.0 + SECURITY_SEC[st["security"]])
                          * (1.0 + STRUCTURE_SM[st["type"]])
                          * (1.0 + _skill_level(sk, "Reprocessing") * 0.03)
                          * (1.0 + _skill_level(sk, "Reprocessing Efficiency") * 0.02)
                          * (1.0 + IMPLANT_IM[config["implant"]]))
            self._scrap = round(50.0 + min(5, _skill_level(sk, SCRAP_SKILL)), 2) / 100.0

    def kind(self, tid):
        gid = self.sde.type_group.get(tid, 0)
        if gid in GAS_GROUPS:
            return "gas"
        if self.sde.group_category.get(gid) == ORE_CATEGORY:
            return "ore"
        return "scrap"

    def _config_yield(self, kind, tid):
        if kind == "scrap":
            return self._scrap
        op = 0
        if tid is not None:
            skill_tid = self.sde.reprocess_skill.get(tid)
            if skill_tid is not None:
                op = _skill_level(self.config["skills"], self.sde.types.get(skill_tid))
        return round(self._base * (1.0 + op * 0.02), 2) / 100.0

    def multiplier(self, tid):
        kind = self.kind(tid)
        if self.config is None:
            m = self.eff[kind]
        else:
            m = self._config_yield(kind, tid)
        self.used[kind].append(m)
        return m

    def reported(self):
        out = {}
        for kind in ("ore", "gas", "scrap"):
            if self.config is None:
                out[kind] = self.eff[kind]
            elif self.used[kind]:
                out[kind] = round(min(self.used[kind]), 4)
            else:
                out[kind] = round(self._config_yield(kind, None), 4)
        return out


def item_volume(sde, tid):
    gid = sde.type_group.get(tid, 0)
    if sde.group_category.get(gid) == SHIP_CATEGORY:
        gname = sde.group_name.get(gid)
        if gname in sde.ship_volumes:
            return sde.ship_volumes[gname]
    return sde.type_volume.get(tid, 0.0)


def reprocess_one(sde, model, tid, qty):
    """Returns (leftover_qty, {material_id: qty})."""
    mats = sde.materials.get(tid)
    portion = sde.type_portion.get(tid, 1)
    if not mats or qty < portion:
        return qty, {}
    n = qty // portion
    left = qty - n * portion
    base = {}
    for mid, q in mats:
        base[mid] = base.get(mid, 0) + q * n
    mult = model.multiplier(tid)
    out = {}
    for mid, total in base.items():
        v = int(math.floor(total * mult + EPS))
        if v > 0:
            out[mid] = v
    return left, out


def run_reprocess(sde, model, items, drill_down):
    pool = {}
    for tid, q in items.items():
        pool[tid] = pool.get(tid, 0) + q
    if not drill_down:
        products = {}
        for tid, q in pool.items():
            left, out = reprocess_one(sde, model, tid, q)
            if left:
                products[tid] = products.get(tid, 0) + left
            for mid, v in out.items():
                products[mid] = products.get(mid, 0) + v
        return products

    reach_memo = {}

    def reach(t, stack=()):
        if t in reach_memo:
            return reach_memo[t]
        res = set()
        for mid, _q in sde.materials.get(t, ()):
            if mid in stack:
                continue
            res.add(mid)
            res |= reach(mid, stack + (t,))
        reach_memo[t] = res
        return res

    stuck = set()
    for _ in range(10000):
        cand = [t for t, q in pool.items()
                if q > 0 and t not in stuck and sde.materials.get(t)
                and q >= sde.type_portion.get(t, 1)]
        if not cand:
            break
        ready = [t for t in cand if not any(t in reach(c) for c in cand if c != t)]
        if not ready:
            ready = cand
        for t in ready:
            q = pool.pop(t)
            left, out = reprocess_one(sde, model, t, q)
            if left:
                pool[t] = pool.get(t, 0) + left
            if not out:
                stuck.add(t)
            for mid, v in out.items():
                pool[mid] = pool.get(mid, 0) + v
    return {t: q for t, q in pool.items() if q > 0}


def hub_orders(state, market_key):
    """Orders at the hub station for the given market (or None)."""
    _k, loc = MARKET_KEYS[market_key]
    all_orders = state.all_orders()
    at = [o for o in all_orders if o["location_id"] == loc]
    if not at:
        book = state.books.get(market_key)
        if book:
            at = list(book.values())
    return at or None


def handle_config(state, body):
    data = _json_body(body, allow_empty=True)
    cfg = parse_config(data)
    key = secrets.token_hex(16)
    with state.lock:
        state.configs[key] = cfg
    return 201, {"key": key}


def _json_body(body, allow_empty=False):
    if not body or not body.strip():
        if allow_empty:
            return None
        raise ValueApiError(400, "INVALID_VALUE", "", "Request body is empty")
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueApiError(400, "INVALID_VALUE", "", "Request body is not valid JSON: %s" % e)


def _r2(v):
    return None if v is None else round(v, 2)


def handle_reprocess(state, headers, body):
    api_key = headers.get("x-api-key")
    config = None
    if api_key is not None and api_key.strip() != "":
        with state.lock:
            config = state.configs.get(api_key.strip())
        if config is None:
            return 401, {"code": "BAD_API_KEY"}

    data = _json_body(body)
    if not isinstance(data, dict):
        raise ValueApiError(400, "INVALID_VALUE", "", "Request body must be a JSON object")
    items = data.get("items")
    if not isinstance(items, dict) or not items:
        raise ValueApiError(400, "INVALID_VALUE", "items",
                            "items must be a non-empty object mapping item names to quantities")
    for name, q in items.items():
        if isinstance(q, bool) or not isinstance(q, (int, float)) or \
                (isinstance(q, float) and q != int(q)) or q <= 0:
            raise ValueApiError(400, "INVALID_VALUE", "items.%s" % name,
                                "Quantity for '%s' must be a positive integer, got %s"
                                % (name, json.dumps(q)))
    eff_in = data.get("efficiency")
    if eff_in is None:
        eff_in = {}
    if not isinstance(eff_in, dict):
        raise ValueApiError(400, "INVALID_VALUE", "efficiency", "efficiency must be an object")
    eff = dict(DEFAULT_EFF)
    for k in ("ore", "gas", "scrap"):
        v = eff_in.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ValueApiError(400, "INVALID_VALUE", "efficiency.%s" % k,
                                "efficiency.%s must be a number between 0 and 1, got %s"
                                % (k, json.dumps(v)))
        eff[k] = float(v)
    drill = data.get("drill_down", False)
    if drill is None:
        drill = False
    if not isinstance(drill, bool):
        raise ValueApiError(400, "INVALID_VALUE", "drill_down", "drill_down must be a boolean")
    if "market" in data:
        market = data.get("market")
    else:
        market = "jita"
    if market is not None:
        if not isinstance(market, str) or market.strip().lower() not in MARKET_KEYS:
            raise _enum_error("market", market, ["Jita", "Amarr", "Dodixie", "Rens", "Hek", None])
        market = market.strip().lower()

    sde = state.sde
    resolved = {}
    unknown = []
    for name, q in items.items():
        tid = sde.type_exact.get(name)
        if tid is None:
            tid = sde.type_by_name.get(name.strip().lower())
        if tid is None:
            unknown.append(name)
            continue
        resolved[tid] = resolved.get(tid, 0) + int(q)
    if unknown:
        raise ApiError(404, "UNKNOWN_ITEMS", sorted(unknown))

    model = YieldModel(sde, efficiency=eff, config=config)
    products = run_reprocess(sde, model, resolved, drill)

    prices = None
    if market is not None:
        with state.lock:
            orders = hub_orders(state, market)
        if orders:
            by_type = {}
            for o in orders:
                by_type.setdefault(o["type_id"], []).append(o)
            prices = {}

            def unit(tid):
                if tid not in prices:
                    lst = by_type.get(tid)
                    prices[tid] = type_stats(lst) if lst else None
                return prices[tid]

    def summary(qtys):
        vol = sum(q * item_volume(sde, t) for t, q in qtys.items())
        out = {"volume": round(vol, 2)}
        for f in ("buy", "sell", "buy_5pct", "sell_5pct"):
            if prices is None:
                out[f] = None
                continue
            total = 0.0
            for t, q in qtys.items():
                st = unit(t)
                if st and st[f] is not None:
                    total += q * st[f]
            out[f] = round(total, 2)
        return out

    prod_out = {}
    for t in sorted(products, key=lambda t: sde.types.get(t, "")):
        q = products[t]
        entry = {"quantity": q, "volume": round(q * item_volume(sde, t), 2)}
        for f in ("buy", "sell"):
            if prices is None:
                entry[f] = None
            else:
                st = unit(t)
                entry[f] = _r2(q * st[f]) if st and st[f] is not None else None
        prod_out[sde.types.get(t, str(t))] = entry

    return 201, {
        "inputs": summary(resolved),
        "outputs": summary(products),
        "yields": model.reported(),
        "products": prod_out,
    }


# --------------------------------------------------------------------------
# Compression optimizer
# --------------------------------------------------------------------------

VARIANT_KEYS = ("base", "plus5", "plus10", "plus15")
_COMPRESS_PREFIXES = ("Batch Compressed ", "Compressed ")


def _strip_compressed(name):
    for p in _COMPRESS_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name


def ore_variant_map(sde):
    """Map ore type_id -> variant key, derived from material ratios per family."""
    cached = getattr(sde, "_variant_map", None)
    if cached is not None:
        return cached
    fams = {}
    for tid, gid in sde.type_group.items():
        if gid in GAS_GROUPS or sde.group_category.get(gid) != ORE_CATEGORY:
            continue
        mats = sde.materials.get(tid)
        if not mats:
            continue
        key = (gid, frozenset(m for m, _ in mats))
        fams.setdefault(key, []).append(tid)
    names = set(sde.type_exact)
    out = {}
    for tids in fams.values():
        totals = {t: sum(q for _, q in sde.materials[t]) for t in tids}
        # Reference members: compressed ores or ores with compressed counterparts.
        ref = [t for t in tids
               if sde.types[t] != _strip_compressed(sde.types[t])
               or any(p + sde.types[t] in names for p in _COMPRESS_PREFIXES)]
        base_total = min(totals[t] for t in (ref or tids))
        for t in tids:
            r = totals[t] / base_total if base_total > 0 else 1.0
            if r < 1.025:
                out[t] = "base"
            elif r < 1.075:
                out[t] = "plus5"
            elif r < 1.125:
                out[t] = "plus10"
            else:
                out[t] = "plus15"
    sde._variant_map = out
    return out


def _err400(details, error="INVALID_VALUE"):
    return ApiError(400, error, details)


def _num(v, path, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise _err400("%s must be a number, got %s" % (path, json.dumps(v)))
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise _err400("%s is out of range: %s" % (path, json.dumps(v)))
    return float(v)


def _parse_compress(data):
    if not isinstance(data, dict):
        raise _err400("Request body must be a JSON object")
    mats = data.get("materials")
    if not isinstance(mats, dict) or not mats:
        raise _err400("materials must be a non-empty object mapping names to quantities")
    for name, q in mats.items():
        if isinstance(q, bool) or not isinstance(q, (int, float)) or \
                (isinstance(q, float) and q != int(q)) or q < 0:
            raise _err400("Quantity for '%s' must be a non-negative integer, got %s"
                          % (name, json.dumps(q)))
    hubs = data.get("hubs")
    if hubs is None:
        hubs = ["jita"]
    if isinstance(hubs, str):
        hubs = [hubs]
    if not isinstance(hubs, list) or not hubs:
        raise _err400("hubs must be a non-empty list of hub names")
    hub_keys = []
    for h in hubs:
        if not isinstance(h, str) or h.strip().lower() not in MARKET_KEYS:
            raise _err400("Unknown hub %s; expected one of: %s"
                          % (json.dumps(h), ", ".join(MARKET_KEYS)))
        k = h.strip().lower()
        if k not in hub_keys:
            hub_keys.append(k)
    cost_m3 = data.get("cost_per_m3")
    cost_m3 = 0.0 if cost_m3 is None else _num(cost_m3, "cost_per_m3", lo=0)
    tax = data.get("collateral_tax")
    tax = 0.0 if tax is None else _num(tax, "collateral_tax", lo=0)
    eff_in = data.get("efficiencies")
    if eff_in is None:
        eff_in = data.get("efficiency") or {}
    if not isinstance(eff_in, dict):
        raise _err400("efficiencies must be an object")
    eff = dict(DEFAULT_EFF)
    for k in ("ore", "gas", "scrap"):
        if eff_in.get(k) is not None:
            eff[k] = _num(eff_in[k], "efficiencies.%s" % k, lo=0, hi=1)
    var_in = data.get("ore_variants")
    if var_in is None:
        var_in = {}
    if not isinstance(var_in, dict):
        raise _err400("ore_variants must be an object")
    variants = {}
    for k in VARIANT_KEYS:
        v = var_in.get(k, True)
        if v is None:
            v = True
        if not isinstance(v, bool):
            raise _err400("ore_variants.%s must be a boolean" % k)
        variants[k] = v
    return mats, hub_keys, cost_m3, tax, eff, variants


def _solve_milp(cands, orders_by_type, targets, cost_m3, tax, rhs_extra):
    import numpy as np
    from scipy.optimize import milp, LinearConstraint, Bounds
    from scipy.sparse import lil_matrix

    nt = len(cands)
    order_list = []
    for i, c in enumerate(cands):
        for o in orders_by_type[c["tid"]]:
            order_list.append((i, o))
    no = len(order_list)
    nv = nt + no
    cost = np.zeros(nv)
    lb = np.zeros(nv)
    ub = np.full(nv, np.inf)
    integrality = np.zeros(nv)
    for i, c in enumerate(cands):
        cost[i] = c["portion"] * c["volume"] * cost_m3
        integrality[i] = 1
        ub[i] = c["max_portions"]
    for j, (i, o) in enumerate(order_list):
        cost[nt + j] = o["price"] * (1.0 + tax)
        ub[nt + j] = o["volume_remain"]
    mids = list(targets)
    A = lil_matrix((nt + len(mids), nv))
    lo = np.zeros(nt + len(mids))
    hi = np.zeros(nt + len(mids))
    for i, c in enumerate(cands):
        A[i, i] = -float(c["portion"])
    for j, (i, _o) in enumerate(order_list):
        A[i, nt + j] = 1.0
    # sum(x) - portion*n in [0, portion-1] keeps purchases tied to whole portions.
    for i, c in enumerate(cands):
        lo[i] = 0.0
        hi[i] = 0.0
    for r, m in enumerate(mids):
        for i, c in enumerate(cands):
            a = c["yield"].get(m)
            if a:
                A[nt + r, i] = a
        lo[nt + r] = targets[m] + rhs_extra.get(m, 0)
        hi[nt + r] = np.inf
    res = milp(cost, constraints=LinearConstraint(A.tocsr(), lo, hi),
               integrality=integrality, bounds=Bounds(lb, ub),
               options={"time_limit": 60, "mip_rel_gap": 1e-9, "presolve": True})
    if res.x is None:
        return None
    return [int(round(res.x[i])) for i in range(nt)]


def handle_compress(state, body):
    data = _json_body(body)
    try:
        mats, hub_keys, cost_m3, tax, eff, variants = _parse_compress(data)
    except ValueApiError as e:
        raise ApiError(400, "INVALID_VALUE", e.message)
    sde = state.sde

    targets = {}
    unknown = []
    for name, q in mats.items():
        tid = sde.type_exact.get(name)
        if tid is None:
            tid = sde.type_by_name.get(name.strip().lower())
        if tid is None:
            unknown.append(name)
            continue
        targets[tid] = targets.get(tid, 0) + int(q)
    if unknown:
        raise ApiError(404, "UNKNOWN_ITEMS", sorted(unknown))

    # Gather hub orders.
    with state.lock:
        hub_order_lists = []
        for hk in hub_keys:
            lst = hub_orders(state, hk)
            if lst:
                hub_order_lists.append(lst)
    all_hub = {}
    for lst in hub_order_lists:
        for o in lst:
            all_hub[o["order_id"]] = o
    hub_list = list(all_hub.values())
    by_type = {}
    for o in hub_list:
        by_type.setdefault(o["type_id"], []).append(o)

    model = YieldModel(sde, efficiency=eff)
    vmap = ore_variant_map(sde)
    need = {m for m, q in targets.items() if q > 0}

    cands = []
    orders_by_type = {}
    for tid, lst in by_type.items():
        sells = [o for o in lst if not o["is_buy_order"] and o["volume_remain"] > 0
                 and o["price"] >= 0]
        if not sells:
            continue
        mats_t = sde.materials.get(tid)
        if not mats_t:
            continue
        kind = model.kind(tid)
        if kind == "scrap":
            continue
        if kind == "ore" and not variants.get(vmap.get(tid, "base"), True):
            continue
        base = {}
        for mid, q in mats_t:
            base[mid] = base.get(mid, 0) + q
        if not any(m in need for m in base):
            continue
        mult = eff[kind]
        portion = sde.type_portion.get(tid, 1)
        avail = sum(o["volume_remain"] for o in sells)
        if avail < portion:
            continue
        cands.append({
            "tid": tid, "kind": kind, "portion": portion, "mult": mult,
            "base": base, "volume": item_volume(sde, tid),
            "yield": {m: q * mult for m, q in base.items()},
            "max_portions": avail // portion,
        })
        orders_by_type[tid] = sorted(sells, key=lambda o: (o["price"], o["order_id"]))
    if need and not cands:
        raise ApiError(404, "NO_CANDIDATES",
                       "No ore or gas with sell orders in the selected hubs yields the requested materials")
    cands.sort(key=lambda c: c["tid"])

    def actual_yields(ns):
        out = {}
        for c, n in zip(cands, ns):
            if n <= 0:
                continue
            for mid, q in c["base"].items():
                v = int(math.floor(n * q * c["mult"] + EPS))
                if v > 0:
                    out[mid] = out.get(mid, 0) + v
        return out

    # Quick feasibility check on volume.
    for m in need:
        cap = sum(c["yield"].get(m, 0) * c["max_portions"] for c in cands)
        if cap + EPS < targets[m]:
            raise ApiError(400, "INFEASIBLE",
                           "Cannot meet target for %s with available market volume "
                           "(max %d of %d)" % (sde.types[m], int(cap), targets[m]))

    ns = [0] * len(cands)
    if need:
        extra = {}
        ns = None
        for _ in range(50):
            sol = _solve_milp(cands, orders_by_type, {m: targets[m] for m in need},
                              cost_m3, tax, extra)
            if sol is None:
                raise ApiError(400, "INFEASIBLE",
                               "Cannot meet material targets with available market volume")
            got = actual_yields(sol)
            short = {m: targets[m] - got.get(m, 0) for m in need if got.get(m, 0) < targets[m]}
            if not short:
                ns = sol
                break
            for m, d in short.items():
                extra[m] = extra.get(m, 0) + d
        if ns is None:
            raise ApiError(400, "INFEASIBLE", "Could not find a solution meeting all targets")

    # Build purchases (cheapest orders first for each chosen type).
    buy_list = {}
    items_total = 0.0
    volume_total = 0.0
    for c, n in zip(cands, ns):
        if n <= 0:
            continue
        qty = n * c["portion"]
        remaining = qty
        per_station = {}
        cost = 0.0
        for o in orders_by_type[c["tid"]]:
            if remaining <= 0:
                break
            take = min(remaining, o["volume_remain"])
            remaining -= take
            st = state.station_name(o["location_id"])
            ps = per_station.setdefault(st, [0, 0.0])
            ps[0] += take
            ps[1] += take * o["price"]
            cost += take * o["price"]
        vol = qty * c["volume"]
        items_total += cost
        volume_total += vol
        buy_list[sde.types[c["tid"]]] = {
            "quantity": qty,
            "cost": round(cost, 2),
            "volume": round(vol, 2),
            "category": c["kind"],
            "purchases": {
                st: {"quantity": q, "total": round(t, 2),
                     "avg_price": round(t / q, 2) if q else 0.0}
                for st, (q, t) in sorted(per_station.items())
            },
        }
    buy_list = dict(sorted(buy_list.items()))

    shipping = volume_total * cost_m3
    collateral = items_total * tax
    total = items_total + shipping + collateral

    got = actual_yields(ns)
    stats_cache = {}

    def stats(tid):
        if tid not in stats_cache:
            lst = by_type.get(tid)
            stats_cache[tid] = type_stats(lst) if lst else None
        return stats_cache[tid]

    def price(tid, field):
        st = stats(tid)
        return st[field] if st and st[field] is not None else None

    raw_ship = sum(q * item_volume(sde, m) for m, q in targets.items()) * cost_m3
    material_value = 0.0
    for m, q in targets.items():
        p = price(m, "sell_5pct")
        if p is not None:
            material_value += q * p
    excess_value = 0.0
    yields = {}
    for m in sorted(set(got) | set(targets), key=lambda t: sde.types.get(t, "")):
        actual = got.get(m, 0)
        tgt = targets.get(m)
        s5 = price(m, "sell_5pct")
        b5 = price(m, "buy_5pct")
        excess = actual - (tgt or 0)
        if s5 is not None and excess > 0:
            excess_value += excess * s5
        yields[sde.types.get(m, str(m))] = {
            "target": tgt,
            "actual": actual,
            "buy": round(actual * b5, 2) if b5 is not None else None,
            "sell": round(actual * s5, 2) if s5 is not None else None,
        }

    return 200, {
        "buy_list": buy_list,
        "total": round(total, 2),
        "shipping": round(shipping, 2),
        "collateral_tax": round(collateral, 2),
        "items": round(items_total, 2),
        "raw_ship": round(raw_ship, 2),
        "material_value": round(material_value, 2),
        "excess_value": round(excess_value, 2),
        "yields": yields,
    }


# --------------------------------------------------------------------------
# Profit finding
# --------------------------------------------------------------------------

from bisect import bisect_left, bisect_right
from datetime import date as _date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def parse_date(v):
    s = v.strip()
    if not s:
        raise ValueError("empty date")
    try:
        return _date.fromisoformat(s[:10])
    except ValueError:
        return parse_datetime(s).date()


CONVERTERS["date"] = parse_date

VOLUME_SCHEMA = {
    "date": "date",
    "region_id": "int",
    "type_id": "int",
    "average": "float",
    "highest": "float",
    "lowest": "float",
    "count": "int",
    "volume": "int",
}
HISTORY_DAYS = 30
DEFAULT_TAX = 0.04
_CENT = Decimal("0.01")


def _price_step(d):
    m = d.adjusted()
    return max(_CENT, Decimal(10) ** (m - 3)), m


def undercut_price(p):
    """Largest valid price (4 significant digits, 0.01 resolution) below p."""
    d = Decimal(repr(float(p)))
    if d <= _CENT:
        return float(d)
    step, m = _price_step(d)
    k = (d / step).to_integral_value(rounding=ROUND_CEILING) - 1
    x = k * step
    if x < Decimal(10) ** m:
        step2 = max(_CENT, Decimal(10) ** (m - 4))
        x = Decimal(10) ** m - step2
    return float(x)


def overbid_price(p):
    """Smallest valid price (4 significant digits, 0.01 resolution) above p."""
    d = Decimal(repr(float(p)))
    if d <= 0:
        return 0.01
    step, _m = _price_step(d)
    k = (d / step).to_integral_value(rounding=ROUND_FLOOR) + 1
    return float(k * step)


def handle_volumes(state, query, headers, body):
    rows = read_gzip_csv(body, VOLUME_SCHEMA)
    with state.lock:
        for r in rows:
            key = (r["region_id"], r["type_id"])
            state.history.setdefault(key, {})[r["date"]] = (r["average"], r["volume"])
            if state.history_max_date is None or r["date"] > state.history_max_date:
                state.history_max_date = r["date"]
    return 200, {"status": "VOLUME_UPDATED", "count": len(rows)}


def daily_stats(state, region_id, tid):
    """(avg daily volume, avg daily value) over the 30 day window, 0-filled."""
    hist = state.history.get((region_id, tid))
    if not hist or state.history_max_date is None:
        return 0.0, 0.0
    start = state.history_max_date - timedelta(days=HISTORY_DAYS - 1)
    vol = 0.0
    val = 0.0
    for d, (avg, v) in hist.items():
        if start <= d <= state.history_max_date:
            vol += v
            val += avg * v
    return vol / HISTORY_DAYS, val / HISTORY_DAYS


def _bad(msg):
    return ApiError(400, "INVALID_VALUE", msg)


def _parse_hub(v):
    if v is None:
        return "jita"
    if not isinstance(v, str):
        raise _bad("hub must be one of: %s" % ", ".join(MARKET_KEYS))
    t = v.strip().lower()
    if t in MARKET_KEYS:
        return t
    for k, name, loc in HUBS:
        if t == name.lower() or t == str(loc):
            return k
    raise _bad("Unknown hub %s; expected one of: %s" % (json.dumps(v), ", ".join(MARKET_KEYS)))


def _parse_tax(v, path="sales_tax"):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip().rstrip("%"))
        except ValueError:
            raise _bad("%s must be a number" % path)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0 or v > 100:
        raise _bad("%s must be a number between 0 and 1" % path)
    v = float(v)
    return v / 100.0 if v > 1 else v


def _get_num(v, path, default, lo=None, integer=False):
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return default
        try:
            v = float(s)
        except ValueError:
            raise _bad("%s must be a number, got %s" % (path, json.dumps(v)))
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise _bad("%s must be a number, got %s" % (path, json.dumps(v)))
    if integer:
        if v != int(v):
            raise _bad("%s must be an integer, got %s" % (path, json.dumps(v)))
        v = int(v)
    if lo is not None and v < lo:
        raise _bad("%s must be >= %s, got %s" % (path, lo, json.dumps(v)))
    return v


def _api_config(state, headers):
    """Returns (config or None, error response or None)."""
    api_key = headers.get("x-api-key")
    if api_key is None or api_key.strip() == "":
        return None, None
    with state.lock:
        cfg = state.configs.get(api_key.strip())
    if cfg is None:
        return None, (401, {"code": "BAD_API_KEY"})
    return cfg, None


def _config_tax(cfg):
    if cfg is None:
        return None
    if cfg.get("sales_tax") is not None:
        return cfg["sales_tax"]
    skills = cfg.get("skills") or {}
    if any(k.lower() == "accounting" for k in skills):
        lvl = _skill_level(skills, "Accounting")
        return 0.075 * (1.0 - 0.11 * lvl)
    return None


def _station_orders(state, market_key):
    with state.lock:
        lst = hub_orders(state, market_key) or []
    return lst


def _competition(orders, eff_ts, minutes):
    if eff_ts is None:
        return len(orders)
    cnt = 0
    limit = minutes * 60.0
    for o in orders:
        age = (eff_ts - o["issued"]).total_seconds()
        if age <= limit:
            cnt += 1
    return cnt


class _Book:
    """Cumulative view of an order list sorted by fill priority."""

    def __init__(self, orders):
        self.orders = orders
        self.cumvol = []
        self.cumval = []
        v = 0
        t = 0.0
        for o in orders:
            v += o["volume_remain"]
            t += o["volume_remain"] * o["price"]
            self.cumvol.append(v)
            self.cumval.append(t)
        self.total = v

    def value(self, qty):
        """Value of filling qty units (capped at total)."""
        if qty <= 0 or not self.orders:
            return 0.0
        if qty >= self.total:
            return self.cumval[-1]
        i = bisect_left(self.cumvol, qty)
        prev_v = self.cumvol[i - 1] if i else 0
        prev_t = self.cumval[i - 1] if i else 0.0
        return prev_t + (qty - prev_v) * self.orders[i]["price"]

    def marginal_price(self, qty):
        """Price of the qty-th unit."""
        i = bisect_left(self.cumvol, qty)
        return self.orders[min(i, len(self.orders) - 1)]["price"]


# ---------------------------- reprocess profit ----------------------------

def handle_profit_reprocess(state, headers, body):
    cfg, err = _api_config(state, headers)
    if err:
        return err
    data = _json_body(body)
    if not isinstance(data, dict):
        raise _bad("Request body must be a JSON object")
    k = _get_num(data.get("k"), "k", 5, lo=0, integer=True)
    hub = _parse_hub(data.get("hub"))
    sort_by = data.get("sort_by")
    if sort_by is None:
        sort_by = "profit"
    if not isinstance(sort_by, str) or sort_by.strip().lower() not in ("roi", "profit"):
        raise _bad("sort_by must be 'roi' or 'profit'")
    sort_by = sort_by.strip().lower()
    tax = _parse_tax(data.get("sales_tax"))
    eff = data.get("scrap_efficiency")
    if eff is not None:
        eff = _get_num(eff, "scrap_efficiency", None, lo=0)
        if eff > 1:
            eff = eff / 100.0
    if cfg is not None:
        model = YieldModel(state.sde, config=cfg)
        eff = model._scrap
        ctax = _config_tax(cfg)
        if ctax is not None:
            tax = ctax
    if eff is None:
        eff = DEFAULT_EFF["scrap"]
    if tax is None:
        tax = DEFAULT_TAX

    sde = state.sde
    orders = _station_orders(state, hub)
    sells_by, buys_by = {}, {}
    for o in orders:
        if o["volume_remain"] <= 0:
            continue
        (buys_by if o["is_buy_order"] else sells_by).setdefault(o["type_id"], []).append(o)
    buy_books = {}

    def buy_book(mid):
        if mid not in buy_books:
            lst = sorted(buys_by.get(mid, []), key=lambda o: (-o["price"], o["order_id"]))
            buy_books[mid] = _Book(lst)
        return buy_books[mid]

    kind_model = YieldModel(sde)
    results = []
    for tid, sells in sells_by.items():
        name = sde.types.get(tid)
        if not name:
            continue
        mats = sde.materials.get(tid)
        if not mats or kind_model.kind(tid) != "scrap":
            continue
        portion = sde.type_portion.get(tid, 1)
        sbook = _Book(sorted(sells, key=lambda o: (o["price"], o["order_id"])))
        nmax = sbook.total // portion
        if nmax <= 0:
            continue
        base = {}
        for mid, q in mats:
            base[mid] = base.get(mid, 0) + q
        mat_books = {mid: buy_book(mid) for mid in base}
        if not any(b.total > 0 for b in mat_books.values()):
            continue

        def revenue(n):
            tot = 0.0
            for mid, q in base.items():
                b = mat_books[mid]
                if b.total <= 0:
                    continue
                qty = int(math.floor(n * q * eff + EPS))
                tot += b.value(qty)
            return tot

        def profit(n):
            return revenue(n) * (1.0 - tax) - sbook.value(n * portion) * (1.0 + tax)

        cands = {1, nmax}
        for cv in sbook.cumvol:
            c = cv // portion
            for x in (c, c + 1):
                if 1 <= x <= nmax:
                    cands.add(x)
        for mid, q in base.items():
            y = q * eff
            if y <= 0:
                continue
            for cv in mat_books[mid].cumvol:
                c = int(cv / y)
                for x in (c - 1, c, c + 1, c + 2):
                    if 1 <= x <= nmax:
                        cands.add(x)
        best_n, best_p = 0, 0.0
        for n in sorted(cands):
            p = profit(n)
            if p > best_p + 1e-9:
                best_n, best_p = n, p
        if best_n <= 0:
            continue
        qty = best_n * portion
        cost = sbook.value(qty)
        value = revenue(best_n)
        results.append({
            "item": name,
            "quantity": qty,
            "cost": cost,
            "value": value,
            "profit": best_p,
            "roi": best_p / cost if cost > 0 else 0.0,
            "max_price": sbook.marginal_price(qty),
        })
    results.sort(key=lambda r: (-r[sort_by], r["item"]))
    results = results[:k]
    items = [{
        "item": r["item"],
        "quantity": r["quantity"],
        "cost": round(r["cost"], 2),
        "value": round(r["value"], 2),
        "profit": round(r["profit"], 2),
        "roi": round(r["roi"], 4),
        "max_price": r["max_price"],
    } for r in results]
    return 200, {
        "cost": round(sum(r["cost"] for r in results), 2),
        "revenue": round(sum(r["value"] for r in results), 2),
        "profit": round(sum(r["profit"] for r in results), 2),
        "items": items,
    }


# ------------------------------- hauling ---------------------------------

def _systems_within(sde, start, max_jumps):
    seen = {start: 0}
    frontier = [start]
    for d in range(1, max_jumps + 1):
        nxt = []
        for s in frontier:
            for t in sde.jumps.get(s, ()):
                if t not in seen:
                    seen[t] = d
                    nxt.append(t)
        frontier = nxt
        if not frontier:
            break
    return seen


def _order_system(state, o):
    sid = o.get("system_id")
    if sid:
        return sid
    st = state.sde.stations.get(o["location_id"])
    if st:
        return st[1]
    st = state.stations.get(o["location_id"])
    return st.get("system_id") if st else None


def _qbool(v, path, default):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    if isinstance(v, bool):
        return v
    try:
        return parse_bool(str(v))
    except ValueError:
        raise _bad("%s must be true or false" % path)


def handle_profit_haul(state, query, headers):
    cfg, err = _api_config(state, headers)
    if err:
        return err

    def q(name):
        v = query.get(name)
        return v[0] if v else None

    hub = _parse_hub(q("hub"))
    capacity = _get_num(q("capacity"), "capacity", float("inf"), lo=0)
    shipping = _get_num(q("shipping"), "shipping", 350.0, lo=0)
    max_jumps = _get_num(q("max_jumps"), "max_jumps", 5, lo=0, integer=True)
    margin = _get_num(q("margin"), "margin", 20.0)
    include_hubs = _qbool(q("include_hubs"), "include_hubs", False)
    daily_value = _get_num(q("daily_value"), "daily_value", 1e9, lo=0)
    optimize = (q("optimize") or "profit").strip().lower()
    if optimize not in ("m3", "roi", "profit"):
        raise _bad("optimize must be one of: m3, roi, profit")
    tax = _parse_tax(q("sales_tax"))
    ctax = _config_tax(cfg)
    if ctax is not None:
        tax = ctax
    if tax is None:
        tax = DEFAULT_TAX

    sde = state.sde
    _k, hub_loc = MARKET_KEYS[hub]
    hub_st = sde.stations.get(hub_loc)
    hub_sys = hub_st[1] if hub_st else None
    hub_region = hub_st[2] if hub_st else None
    other_hubs = {loc for kk, _n, loc in HUBS if kk != hub}
    with state.lock:
        all_orders = state.all_orders()
        eff_ts = state.effective_ts
    reach = _systems_within(sde, hub_sys, max_jumps) if hub_sys is not None else {}

    dest_sells, dest_buys, src = {}, {}, {}
    for o in all_orders:
        if o["volume_remain"] <= 0:
            continue
        loc = o["location_id"]
        if loc == hub_loc:
            (dest_buys if o["is_buy_order"] else dest_sells).setdefault(o["type_id"], []).append(o)
            continue
        if o["is_buy_order"]:
            continue
        if loc in other_hubs and not include_hubs:
            continue
        if _order_system(state, o) not in reach:
            continue
        src.setdefault(o["type_id"], []).append(o)

    lots = []
    sell_prices = {}
    for tid, lst in src.items():
        name = sde.types.get(tid)
        if not name:
            continue
        ds = dest_sells.get(tid)
        db = dest_buys.get(tid)
        cand = []
        if ds:
            cand.append(undercut_price(min(o["price"] for o in ds)))
        if db:
            cand.append(max(o["price"] for o in db))
        if not cand:
            continue
        sp = max(cand)
        if hub_region is not None and daily_stats(state, hub_region, tid)[1] < daily_value:
            continue
        vol = item_volume(sde, tid)
        ship_unit = vol * shipping
        net_sell = sp * (1.0 - tax)
        for o in lst:
            c = o["price"]
            if c <= 0:
                continue
            pu = net_sell - c * (1.0 + tax) - ship_unit
            if pu <= 0 or pu / c * 100.0 < margin:
                continue
            lots.append({"tid": tid, "name": name, "order": o, "pu": pu, "vol": vol,
                         "roi": pu / c, "density": pu / vol if vol > 0 else float("inf"),
                         "total": pu * o["volume_remain"]})
            sell_prices[tid] = sp

    key = {"m3": "density", "roi": "roi", "profit": "pu"}[optimize]
    lots.sort(key=lambda l: (-l[key], -l["pu"], l["name"], l["order"]["price"], l["order"]["order_id"]))
    remaining = capacity
    bought = {}  # tid -> {loc: [qty, total]}
    for lot in lots:
        o = lot["order"]
        qty = o["volume_remain"]
        if lot["vol"] > 0:
            if remaining != float("inf"):
                qty = min(qty, int(math.floor(remaining / lot["vol"] + EPS)))
        if qty <= 0:
            continue
        if lot["vol"] > 0 and remaining != float("inf"):
            remaining -= qty * lot["vol"]
        ent = bought.setdefault(lot["tid"], {}).setdefault(o["location_id"], [0, 0.0])
        ent[0] += qty
        ent[1] += qty * o["price"]

    purchases = {}
    sales = {}
    cost = revenue = ship_total = 0.0
    for tid in sorted(bought, key=lambda t: sde.types[t]):
        name = sde.types[tid]
        total_qty = 0
        for loc, (qq, tt) in bought[tid].items():
            purchases.setdefault(state.station_name(loc), []).append(
                {"item": name, "quantity": qq, "total": round(tt, 2)})
            total_qty += qq
            cost += tt
        vol = item_volume(sde, tid) * total_qty
        ship_total += vol * shipping
        sp = sell_prices[tid]
        rev = sp * total_qty
        revenue += rev
        comp_orders = dest_sells.get(tid, [])
        sales[name] = {
            "sell_price": sp,
            "total": total_qty,
            "volume": round(vol, 2),
            "revenue": round(rev, 2),
            "competition": {
                "5_minutes": _competition(comp_orders, eff_ts, 5),
                "1_hour": _competition(comp_orders, eff_ts, 60),
                "1_day": _competition(comp_orders, eff_ts, 1440),
            },
        }
    for st in purchases:
        purchases[st].sort(key=lambda e: (e["item"]))
    purchases = dict(sorted(purchases.items()))
    profit = revenue * (1.0 - tax) - cost * (1.0 + tax) - ship_total
    return 200, {
        "cost": round(cost, 2),
        "revenue": round(revenue, 2),
        "profit": round(profit, 2),
        "purchases": purchases,
        "sales": sales,
    }


# ----------------------------- station trading ---------------------------

def handle_profit_trade(state, headers, body):
    cfg, err = _api_config(state, headers)
    if err:
        return err
    data = _json_body(body, allow_empty=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _bad("Request body must be a JSON object")
    hub = _parse_hub(data.get("hub"))
    margin = _get_num(data.get("margin"), "margin", 20.0)
    sort_by = data.get("sorting_by", data.get("sort_by"))
    if sort_by is None:
        sort_by = "ppu"
    if not isinstance(sort_by, str) or sort_by.strip().lower() not in ("ppu", "margin", "daily"):
        raise _bad("sorting_by must be one of: ppu, margin, daily")
    sort_by = sort_by.strip().lower()
    k = _get_num(data.get("k"), "k", 5, lo=0, integer=True)
    daily_traded = _get_num(data.get("daily_traded"), "daily_traded", 1e8, lo=0)
    daily_volume = _get_num(data.get("daily_volume"), "daily_volume", 5.0, lo=0)
    window = _get_num(data.get("competition_window"), "competition_window", 1440, lo=0)
    max_buy = _get_num(data.get("buy_competition"), "buy_competition", 3, lo=0)
    max_sell = _get_num(data.get("sell_competition"), "sell_competition", 3, lo=0)
    tax = _parse_tax(data.get("sales_tax"))
    ctax = _config_tax(cfg)
    if ctax is not None:
        tax = ctax
    if tax is None:
        tax = DEFAULT_TAX

    sde = state.sde
    _k, hub_loc = MARKET_KEYS[hub]
    hub_st = sde.stations.get(hub_loc)
    orders = _station_orders(state, hub)
    with state.lock:
        eff_ts = state.effective_ts
    region = hub_st[2] if hub_st else None
    if region is None and orders:
        region = state.region_of_order(orders[0])
    by_type = {}
    for o in orders:
        if o["volume_remain"] <= 0:
            continue
        ent = by_type.setdefault(o["type_id"], ([], []))
        ent[0 if o["is_buy_order"] else 1].append(o)

    res = []
    for tid, (buys, sells) in by_type.items():
        name = sde.types.get(tid)
        if not name or not buys or not sells:
            continue
        bp = overbid_price(max(o["price"] for o in buys))
        sp = undercut_price(min(o["price"] for o in sells))
        if bp <= 0:
            continue
        ppu = sp - bp
        m = ppu / bp * 100.0
        if m < margin or ppu <= 0:
            continue
        avg_vol, avg_val = daily_stats(state, region, tid)
        if avg_val < daily_traded or avg_vol < daily_volume:
            continue
        bc = _competition(buys, eff_ts, window)
        sc = _competition(sells, eff_ts, window)
        if bc > max_buy or sc > max_sell:
            continue
        dp = avg_vol * 0.10 * ppu * (1.0 - tax)
        res.append({"name": name, "margin": m, "value": avg_val, "volume": avg_vol,
                    "ppu": ppu, "buy_competition": bc, "sell_competition": sc,
                    "buy_price": bp, "sell_price": sp, "daily_profit": dp})
    key = {"ppu": "ppu", "margin": "margin", "daily": "daily_profit"}[sort_by]
    res.sort(key=lambda r: (-r[key], r["name"]))
    out = {}
    for r in res[:k]:
        out[r["name"]] = {
            "margin": round(r["margin"], 2),
            "value": round(r["value"], 2),
            "volume": round(r["volume"], 2),
            "ppu": round(r["ppu"], 2),
            "buy_competition": r["buy_competition"],
            "sell_competition": r["sell_competition"],
            "buy_price": r["buy_price"],
            "sell_price": r["sell_price"],
            "daily_profit": round(r["daily_profit"], 2),
        }
    return 200, out


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MarketTools/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = self.headers.get("Content-Length")
            if length:
                try:
                    n = int(length)
                except ValueError:
                    n = 0
                return self.rfile.read(n) if n > 0 else b""
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while True:
                    line = self.rfile.readline().strip()
                    size = int(line.split(b";")[0], 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
                return b"".join(chunks)
            return b""

        def _dispatch(self, method):
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                parts = [p for p in parsed.path.split("/") if p]
                headers = {k.lower(): v for k, v in self.headers.items()}
                body = self._body() if method == "POST" else b""
                if len(parts) >= 2 and parts[0] == "v1":
                    route = parts[1]
                    if method == "POST" and route == "prices" and len(parts) == 2:
                        return self._send(*handle_prices(state, query, headers, body))
                    if method == "POST" and route == "stations" and len(parts) == 2:
                        return self._send(*handle_stations(state, query, headers, body))
                    if method == "GET" and route == "market" and len(parts) in (3, 4):
                        rid = resolve_region(state, parts[2])
                        if len(parts) == 3:
                            return self._send(*handle_region(state, rid, query))
                        return self._send(*handle_region_type(state, rid, parts[3]))
                    if method == "POST" and route == "config" and len(parts) == 2:
                        return self._send(*handle_config(state, body))
                    if method == "POST" and route == "reprocess" and len(parts) == 2:
                        return self._send(*handle_reprocess(state, headers, body))
                    if method == "POST" and route == "compress" and len(parts) == 2:
                        return self._send(*handle_compress(state, body))
                    if method == "POST" and route == "volumes" and len(parts) == 2:
                        return self._send(*handle_volumes(state, query, headers, body))
                    if route == "profit" and len(parts) == 3:
                        sub = parts[2]
                        if method == "POST" and sub == "reprocess":
                            return self._send(*handle_profit_reprocess(state, headers, body))
                        if method == "GET" and sub == "haul":
                            return self._send(*handle_profit_haul(state, query, headers))
                        if method == "POST" and sub == "trade":
                            return self._send(*handle_profit_trade(state, headers, body))
                    if method == "GET" and route == "hub-compare" and len(parts) == 3:
                        return self._send(*handle_hub_compare(state, parts[2]))
                self._send(404, {"error": "NOT_FOUND", "details": "No route for %s %s" % (method, parsed.path)})
            except ValueApiError as e:
                self._send(e.status, e.payload())
            except ApiError as e:
                self._send(e.status, {"error": e.error, "details": e.details})
            except Exception as e:  # pragma: no cover
                self._send(500, {"error": "INTERNAL_ERROR", "details": str(e)})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="EVE market tools API")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--address", default="0.0.0.0")
    ap.add_argument("--sde", required=True, help="Path to SDE directory")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.sde):
        print("SDE directory not found: %s" % args.sde, file=sys.stderr)
        return 2
    sde = SDE(args.sde)
    state = State(sde)
    server = ThreadingHTTPServer((args.address, args.port), make_handler(state))
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
