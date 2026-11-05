#!/usr/bin/env python3
"""EVE market tools API server (stdlib only)."""
import argparse
import bz2
import csv
import gzip
import io
import json
import os
import re
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
                    if method == "GET" and route == "hub-compare" and len(parts) == 3:
                        return self._send(*handle_hub_compare(state, parts[2]))
                self._send(404, {"error": "NOT_FOUND", "details": "No route for %s %s" % (method, parsed.path)})
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
