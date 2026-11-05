#!/usr/bin/env python3
"""EVE market tooling API.

Launch: python market_tools.py --sde ./sde [--port 8000] [--address 0.0.0.0]
Uses only the Python standard library.
"""
from __future__ import annotations

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
from urllib.parse import parse_qs, urlparse

csv.field_size_limit(sys.maxsize)

HUBS = [
    ("jita", "Jita IV - Moon 4 - Caldari Navy Assembly Plant"),
    ("amarr", "Amarr VIII (Oris) - Emperor Family Academy"),
    ("dodixie", "Dodixie IX - Moon 20 - Federation Navy Assembly Plant"),
    ("rens", "Rens VI - Moon 8 - Brutor Tribe Treasury"),
    ("hek", "Hek VIII - Moon 12 - Boundless Creation Factory"),
]
HUB_FALLBACK_IDS = {
    "jita": 60003760,
    "amarr": 60008494,
    "dodixie": 60011866,
    "rens": 60004588,
    "hek": 60005686,
}

PRICE_COLUMNS = [
    "order_id", "duration", "is_buy_order", "issued", "location_id",
    "min_volume", "price", "range", "system_id", "type_id",
    "volume_remain", "volume_total",
]
STATION_COLUMNS = ["location_id", "type", "name"]


class ApiError(Exception):
    def __init__(self, status: int, error: str, details: str):
        super().__init__(details)
        self.status = status
        self.error = error
        self.details = details


# --------------------------------------------------------------------------- SDE


def _open_sde(sde_dir: str, base: str):
    for suffix, opener in ((".csv.bz2", bz2.open), (".csv", open), (".csv.gz", gzip.open)):
        path = os.path.join(sde_dir, base + suffix)
        if os.path.exists(path):
            return opener(path, "rt", encoding="utf-8", newline="")
    return None


class SDE:
    def __init__(self, sde_dir: str):
        self.type_names: dict[int, str] = {}
        self.region_names: dict[int, str] = {}
        self.system_region: dict[int, int] = {}
        self.system_names: dict[int, str] = {}
        self.system_by_name: dict[str, int] = {}
        self.station_names: dict[int, str] = {}
        self.station_system: dict[int, int] = {}
        self.station_by_name: dict[str, int] = {}

        f = _open_sde(sde_dir, "invTypes")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        self.type_names[int(row["typeID"])] = row["typeName"]
                    except (ValueError, KeyError, TypeError):
                        continue
        f = _open_sde(sde_dir, "mapRegions")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        self.region_names[int(row["regionID"])] = row["regionName"]
                    except (ValueError, KeyError, TypeError):
                        continue
        f = _open_sde(sde_dir, "mapSolarSystems")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        sid = int(row["solarSystemID"])
                        self.system_region[sid] = int(row["regionID"])
                        self.system_names[sid] = row["solarSystemName"]
                        self.system_by_name[row["solarSystemName"].lower()] = sid
                    except (ValueError, KeyError, TypeError):
                        continue
        f = _open_sde(sde_dir, "staStations")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        st = int(row["stationID"])
                        self.station_names[st] = row["stationName"]
                        self.station_system[st] = int(row["solarSystemID"])
                        self.station_by_name[row["stationName"]] = st
                    except (ValueError, KeyError, TypeError):
                        continue


# --------------------------------------------------------------------------- parsing


def _decompress(body: bytes) -> str:
    if not body:
        raise ApiError(400, "INVALID_FORMAT", "Request body is empty; expected gzipped CSV bytes")
    try:
        raw = gzip.decompress(body)
    except (OSError, EOFError, zlib.error) as exc:
        raise ApiError(400, "INVALID_FORMAT", f"Failed to decompress gzip payload: {exc}")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError(400, "INVALID_FORMAT", f"Payload is not valid UTF-8 text: {exc}")


def _read_csv(text: str, expected: list[str]):
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
    except csv.Error as exc:
        raise ApiError(400, "INVALID_FORMAT", f"CSV parse error: {exc}")
    if header is None:
        raise ApiError(400, "INVALID_FORMAT", "CSV is empty; missing header row")
    header = [h.strip() for h in header]
    if len(header) != len(set(header)) or set(header) != set(expected):
        missing = [c for c in expected if c not in header]
        extra = [c for c in header if c not in expected]
        parts = []
        if missing:
            parts.append("missing columns: " + ", ".join(missing))
        if extra:
            parts.append("unexpected columns: " + ", ".join(extra))
        if len(header) != len(set(header)):
            parts.append("duplicate columns present")
        raise ApiError(
            400, "INVALID_FORMAT",
            "CSV columns do not match the expected schema (" + "; ".join(parts) +
            "). Expected: " + ", ".join(expected),
        )
    idx = {name: i for i, name in enumerate(header)}
    rows = []
    try:
        for lineno, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) != len(header):
                raise ApiError(
                    400, "INVALID_FORMAT",
                    f"CSV row {lineno} has {len(row)} fields, expected {len(header)}",
                )
            rows.append((lineno, {name: row[i] for name, i in idx.items()}))
    except csv.Error as exc:
        raise ApiError(400, "INVALID_FORMAT", f"CSV parse error: {exc}")
    return rows


def _parse_int(value: str, col: str, lineno: int) -> int:
    v = value.strip()
    try:
        return int(v)
    except ValueError:
        try:
            f = float(v)
            if f.is_integer():
                return int(f)
        except ValueError:
            pass
    raise ApiError(400, "INVALID_FORMAT", f"Row {lineno}: column '{col}' must be an integer, got {value!r}")


def _parse_float(value: str, col: str, lineno: int) -> float:
    try:
        f = float(value.strip())
    except ValueError:
        raise ApiError(400, "INVALID_FORMAT", f"Row {lineno}: column '{col}' must be a number, got {value!r}")
    if f != f or f in (float("inf"), float("-inf")):
        raise ApiError(400, "INVALID_FORMAT", f"Row {lineno}: column '{col}' must be finite, got {value!r}")
    return f


def _parse_bool(value: str, col: str, lineno: int) -> bool:
    v = value.strip().lower()
    if v in ("true", "1", "t", "yes", "y"):
        return True
    if v in ("false", "0", "f", "no", "n"):
        return False
    raise ApiError(400, "INVALID_FORMAT", f"Row {lineno}: column '{col}' must be a boolean, got {value!r}")


def _parse_time(value: str, col: str, lineno: int) -> float:
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.fromisoformat(v.replace(" ", "T"))
        except ValueError:
            raise ApiError(400, "INVALID_FORMAT", f"Row {lineno}: column '{col}' must be ISO-8601, got {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class Order:
    __slots__ = ("order_id", "is_buy", "issued", "location_id", "price",
                 "system_id", "type_id", "volume_remain")

    def __init__(self, order_id, is_buy, issued, location_id, price, system_id, type_id, volume_remain):
        self.order_id = order_id
        self.is_buy = is_buy
        self.issued = issued
        self.location_id = location_id
        self.price = price
        self.system_id = system_id
        self.type_id = type_id
        self.volume_remain = volume_remain


def parse_orders(body: bytes) -> list[Order]:
    rows = _read_csv(_decompress(body), PRICE_COLUMNS)
    out = []
    for lineno, r in rows:
        _parse_int(r["duration"], "duration", lineno)
        _parse_int(r["min_volume"], "min_volume", lineno)
        _parse_int(r["volume_total"], "volume_total", lineno)
        out.append(Order(
            _parse_int(r["order_id"], "order_id", lineno),
            _parse_bool(r["is_buy_order"], "is_buy_order", lineno),
            _parse_time(r["issued"], "issued", lineno),
            _parse_int(r["location_id"], "location_id", lineno),
            _parse_float(r["price"], "price", lineno),
            _parse_int(r["system_id"], "system_id", lineno),
            _parse_int(r["type_id"], "type_id", lineno),
            _parse_int(r["volume_remain"], "volume_remain", lineno),
        ))
    return out


def parse_stations(body: bytes) -> list[tuple[int, str, str]]:
    rows = _read_csv(_decompress(body), STATION_COLUMNS)
    out = []
    for lineno, r in rows:
        loc = _parse_int(r["location_id"], "location_id", lineno)
        typ = r["type"].strip()
        if typ.lower() not in ("structure", "station"):
            raise ApiError(400, "INVALID_FORMAT",
                           f"Row {lineno}: column 'type' must be 'Structure' or 'Station', got {r['type']!r}")
        out.append((loc, typ.capitalize(), r["name"]))
    return out


# --------------------------------------------------------------------------- stats


def r2(x):
    return None if x is None else round(x, 2)


def five_pct(orders: list[Order], is_buy: bool):
    """Volume weighted average of the best 5% of volume."""
    if not orders:
        return None
    srt = sorted(orders, key=lambda o: -o.price if is_buy else o.price)
    total = sum(o.volume_remain for o in srt)
    if total <= 0:
        return srt[0].price
    target = total * 0.05
    acc = 0.0
    value = 0.0
    for o in srt:
        take = min(o.volume_remain, target - acc)
        if take <= 0:
            continue
        acc += take
        value += take * o.price
        if acc >= target:
            break
    return value / acc if acc > 0 else srt[0].price


def thresholds(buys: list[Order], sells: list[Order]):
    bt = max(o.price for o in buys) * 0.1 if buys else None
    st = min(o.price for o in sells) * 10.0 if sells else None
    return bt, st


def apply_filter(buys, sells, bt, st):
    fb = [o for o in buys if bt is None or o.price >= bt]
    fs = [o for o in sells if st is None or o.price <= st]
    return fb, fs


# --------------------------------------------------------------------------- state


class State:
    def __init__(self, sde: SDE):
        self.sde = sde
        self.lock = threading.RLock()
        self.books: dict[str, dict[int, Order]] = {}
        self.stations: dict[int, tuple[str, str]] = {}  # location_id -> (type, name)
        self.version = 0
        self._cache_version = -1
        self._merged: dict[int, Order] = {}
        self._by_region: dict[int, list[Order]] = {}

        self.hub_ids: dict[str, int] = {}
        for key, name in HUBS:
            self.hub_ids[key] = sde.station_by_name.get(name, HUB_FALLBACK_IDS[key])

    # ---- ingestion
    def ingest_prices(self, orders: list[Order], market: str, mode: str, location_id):
        if location_id is not None:
            orders = [o for o in orders if o.location_id == location_id]
        with self.lock:
            if mode == "replace":
                book: dict[int, Order] = {}
            else:
                book = self.books.get(market, {})
            for o in orders:
                cur = book.get(o.order_id)
                if cur is None or o.issued >= cur.issued:
                    book[o.order_id] = o
            self.books[market] = book
            self.version += 1
        return len(orders)

    def ingest_stations(self, rows):
        with self.lock:
            for loc, typ, name in rows:
                self.stations[loc] = (typ, name)
            self.version += 1
        return len(rows)

    # ---- lookup
    def station_name(self, loc: int) -> str:
        if loc in self.stations:
            return self.stations[loc][1]
        if loc in self.sde.station_names:
            return self.sde.station_names[loc]
        return str(loc)

    def system_of_location(self, loc: int):
        if loc in self.sde.station_system:
            return self.sde.station_system[loc]
        if loc in self.stations:
            name = self.stations[loc][1]
            sysname = name.split(" - ")[0].strip().lower()
            if sysname in self.sde.system_by_name:
                return self.sde.system_by_name[sysname]
            m = re.match(r"^\s*(\S+)", name)
            if m and m.group(1).lower() in self.sde.system_by_name:
                return self.sde.system_by_name[m.group(1).lower()]
        return None

    def region_of(self, o: Order):
        reg = self.sde.system_region.get(o.system_id)
        if reg is None:
            sysid = self.system_of_location(o.location_id)
            if sysid is not None:
                reg = self.sde.system_region.get(sysid)
        return reg

    def _rebuild(self):
        if self._cache_version == self.version:
            return
        merged: dict[int, Order] = {}
        for book in self.books.values():
            for oid, o in book.items():
                cur = merged.get(oid)
                if cur is None or o.issued >= cur.issued:
                    merged[oid] = o
        by_region: dict[int, list[Order]] = {}
        for o in merged.values():
            reg = self.region_of(o)
            if reg is not None:
                by_region.setdefault(reg, []).append(o)
        self._merged = merged
        self._by_region = by_region
        self._cache_version = self.version

    def region_orders(self, region_id: int) -> list[Order]:
        self._rebuild()
        return self._by_region.get(region_id, [])

    # ---- endpoints
    def _check_region(self, region_id: int):
        if region_id not in self.sde.region_names:
            raise ApiError(404, "UNKNOWN_ITEMS", f"Unknown region id: {region_id}")

    def _check_type(self, type_id: int):
        if type_id not in self.sde.type_names:
            raise ApiError(404, "UNKNOWN_ITEMS", f"Unknown type id: {type_id}")

    @staticmethod
    def _regional_hub(orders: list[Order]):
        counts: dict[int, int] = {}
        for o in orders:
            if not o.is_buy:
                counts[o.location_id] = counts.get(o.location_id, 0) + 1
        if not counts:
            for o in orders:
                counts[o.location_id] = counts.get(o.location_id, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]

    def _region_type_data(self, orders: list[Order], hub):
        """Group region orders by type, apply hub-based outlier filtering."""
        by_type: dict[int, tuple[list, list]] = {}
        for o in orders:
            b, s = by_type.setdefault(o.type_id, ([], []))
            (b if o.is_buy else s).append(o)
        result = {}
        for tid, (buys, sells) in by_type.items():
            hb = [o for o in buys if o.location_id == hub]
            hs = [o for o in sells if o.location_id == hub]
            bt = max(o.price for o in hb) * 0.1 if hb else (max(o.price for o in buys) * 0.1 if buys else None)
            st = min(o.price for o in hs) * 10.0 if hs else (min(o.price for o in sells) * 10.0 if sells else None)
            fb, fs = apply_filter(buys, sells, bt, st)
            fhb = [o for o in fb if o.location_id == hub]
            fhs = [o for o in fs if o.location_id == hub]
            result[tid] = {
                "buys": fb, "sells": fs, "hub_buys": fhb, "hub_sells": fhs,
                "bt": bt, "st": st,
            }
        return result

    def market_overview(self, region_id: int, params: dict):
        self._check_region(region_id)
        type_ids = None
        if "type_ids" in params:
            raw = ",".join(params["type_ids"])
            type_ids = []
            bad = []
            for tok in raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    type_ids.append(int(tok))
                except ValueError:
                    bad.append(tok)
            unknown = bad + [str(t) for t in type_ids if t not in self.sde.type_names]
            if unknown:
                raise ApiError(404, "UNKNOWN_ITEMS", "Unknown type ids: " + ", ".join(unknown))
        hubs_n = None
        if "hubs" in params:
            try:
                hubs_n = int(params["hubs"][-1])
            except ValueError:
                raise ApiError(400, "INVALID_FORMAT", "hubs must be an integer")
            if hubs_n < 0:
                raise ApiError(400, "INVALID_FORMAT", "hubs must be non-negative")

        with self.lock:
            orders = self.region_orders(region_id)
            hub = self._regional_hub(orders)
            data = self._region_type_data(orders, hub)

        sell_orders = buy_orders = 0
        sell_value = buy_value = 0.0
        unique = 0
        station_counts: dict[int, list] = {}
        for tid, d in data.items():
            if d["buys"] or d["sells"]:
                unique += 1
            buy_orders += len(d["buys"])
            sell_orders += len(d["sells"])
            buy_value += sum(o.price * o.volume_remain for o in d["buys"])
            for o in d["sells"]:
                v = o.price * o.volume_remain
                sell_value += v
                sc = station_counts.setdefault(o.location_id, [0, 0.0])
                sc[0] += 1
                sc[1] += v

        out = {
            "name": self.sde.region_names[region_id],
            "sell_orders": sell_orders,
            "buy_orders": buy_orders,
            "sell_value": round(sell_value / 1e9, 2),
            "buy_value": round(buy_value / 1e9, 2),
            "unique_items": unique,
        }
        if type_ids is not None:
            types = {}
            for tid in type_ids:
                d = data.get(tid)
                name = self.sde.type_names[tid]
                if d is None:
                    types[name] = {"buy": None, "sell": None, "split": None,
                                   "buy_orders": 0, "sell_orders": 0}
                    continue
                buy = max((o.price for o in d["hub_buys"]), default=None)
                sell = min((o.price for o in d["hub_sells"]), default=None)
                split = (buy + sell) / 2 if buy is not None and sell is not None else None
                types[name] = {
                    "buy": r2(buy), "sell": r2(sell), "split": r2(split),
                    "buy_orders": len(d["buys"]), "sell_orders": len(d["sells"]),
                }
            out["types"] = types
        if hubs_n is not None:
            ranked = sorted(station_counts.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
            out["hubs"] = [
                {"station": self.station_name(loc), "orders": c, "sell_value": round(v / 1e9, 2)}
                for loc, (c, v) in ranked[:hubs_n]
            ]
        return out

    def market_type(self, region_id: int, type_id: int):
        self._check_region(region_id)
        self._check_type(type_id)
        with self.lock:
            orders = [o for o in self.region_orders(region_id) if o.type_id == type_id]
            all_orders = self.region_orders(region_id)
            hub = self._regional_hub(all_orders)
            data = self._region_type_data(orders, hub).get(type_id)
        name = self.sde.type_names[type_id]
        if data is None:
            return {
                "name": name, "buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None,
                "buy_orders": 0, "sell_orders": 0, "buy_threshold": None, "sell_threshold": None,
                "buy_volume": 0, "sell_volume": 0, "total_value": 0.0,
            }
        fb, fs = data["buys"], data["sells"]
        buy = max((o.price for o in data["hub_buys"]), default=None)
        sell = min((o.price for o in data["hub_sells"]), default=None)
        if buy is None and fb:
            buy = max(o.price for o in fb)
        if sell is None and fs:
            sell = min(o.price for o in fs)
        b5 = five_pct(fb, True)
        s5 = five_pct(fs, False)
        bvol = sum(o.volume_remain for o in fb)
        svol = sum(o.volume_remain for o in fs)
        total_value = (s5 * svol) if s5 is not None else 0.0
        return {
            "name": name,
            "buy": r2(buy),
            "sell": r2(sell),
            "buy_5pct": r2(b5),
            "sell_5pct": r2(s5),
            "buy_orders": len(fb),
            "sell_orders": len(fs),
            "buy_threshold": r2(data["bt"]),
            "sell_threshold": r2(data["st"]),
            "buy_volume": bvol,
            "sell_volume": svol,
            "total_value": r2(total_value),
        }

    def hub_compare(self, type_id: int):
        self._check_type(type_id)
        out = {}
        any_hub = False
        with self.lock:
            self._rebuild()
            merged = list(self._merged.values())
            for key, _name in HUBS:
                hid = self.hub_ids[key]
                at_hub = [o for o in merged if o.location_id == hid]
                if not at_hub and key in self.books:
                    at_hub = list(self.books[key].values())
                    has = True
                else:
                    has = bool(at_hub) or key in self.books
                if not has:
                    continue
                any_hub = True
                buys = [o for o in at_hub if o.type_id == type_id and o.is_buy]
                sells = [o for o in at_hub if o.type_id == type_id and not o.is_buy]
                bt, st = thresholds(buys, sells)
                fb, fs = apply_filter(buys, sells, bt, st)
                buy = max((o.price for o in fb), default=None)
                sell = min((o.price for o in fs), default=None)
                b5 = five_pct(fb, True)
                s5 = five_pct(fs, False)
                svol = sum(o.volume_remain for o in fs) if fs else None
                bvol = sum(o.volume_remain for o in fb) if fb else None
                value = (s5 * svol / 1e9) if (s5 is not None and svol is not None) else None
                out[f"{key}_sell"] = r2(sell)
                out[f"{key}_buy"] = r2(buy)
                out[f"{key}_sell_volume"] = svol
                out[f"{key}_buy_volume"] = bvol
                out[f"{key}_value"] = r2(value)
                out[f"{key}_sell_5pct"] = r2(s5)
                out[f"{key}_buy_5pct"] = r2(b5)
        if not any_hub:
            raise ApiError(404, "NO_PRICE_DATA", "No price data is available for any of the main market hubs")
        return out


# --------------------------------------------------------------------------- HTTP


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MarketTools/1.0"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            length = self.headers.get("Content-Length")
            if length:
                try:
                    return self.rfile.read(int(length))
                except ValueError:
                    return b""
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

        def _opt(self, params, name, default=None):
            if name in params and params[name]:
                return params[name][-1]
            for h in (name, "X-" + name, "X-" + name.replace("_", "-"), name.replace("_", "-")):
                v = self.headers.get(h)
                if v is not None:
                    return v
            return default

        def _handle(self, method: str):
            url = urlparse(self.path)
            params = parse_qs(url.query, keep_blank_values=False)
            parts = [p for p in url.path.split("/") if p]
            try:
                if method == "POST" and parts == ["v1", "prices"]:
                    body = self._body()
                    market = (self._opt(params, "market", "jita") or "jita").strip().lower()
                    mode = (self._opt(params, "mode", "replace") or "replace").strip().lower()
                    if mode not in ("replace", "append"):
                        raise ApiError(400, "INVALID_FORMAT", f"mode must be 'replace' or 'append', got {mode!r}")
                    loc = self._opt(params, "location_id")
                    loc_id = None
                    if loc is not None and str(loc).strip() != "":
                        try:
                            loc_id = int(str(loc).strip())
                        except ValueError:
                            raise ApiError(400, "INVALID_FORMAT", f"location_id must be an integer, got {loc!r}")
                    orders = parse_orders(body)
                    count = state.ingest_prices(orders, market, mode, loc_id)
                    return self._send(200, {"status": "PRICES_UPDATED", "count": count})
                if method == "POST" and parts == ["v1", "stations"]:
                    rows = parse_stations(self._body())
                    count = state.ingest_stations(rows)
                    return self._send(200, {"status": "STATIONS_UPDATED", "count": count})
                if method == "GET" and len(parts) in (3, 4) and parts[:2] == ["v1", "market"]:
                    try:
                        region_id = int(parts[2])
                    except ValueError:
                        raise ApiError(404, "UNKNOWN_ITEMS", f"Unknown region: {parts[2]}")
                    if len(parts) == 3:
                        return self._send(200, state.market_overview(region_id, params))
                    try:
                        type_id = int(parts[3])
                    except ValueError:
                        raise ApiError(404, "UNKNOWN_ITEMS", f"Unknown type: {parts[3]}")
                    return self._send(200, state.market_type(region_id, type_id))
                if method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "hub-compare"]:
                    try:
                        type_id = int(parts[2])
                    except ValueError:
                        raise ApiError(404, "UNKNOWN_ITEMS", f"Unknown type: {parts[2]}")
                    return self._send(200, state.hub_compare(type_id))
                if method == "POST":
                    self._body()
                return self._send(404, {"error": "NOT_FOUND", "details": f"No route for {method} {url.path}"})
            except ApiError as exc:
                return self._send(exc.status, {"error": exc.error, "details": exc.details})
            except Exception as exc:  # pragma: no cover
                return self._send(500, {"error": "INTERNAL_ERROR", "details": str(exc)})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="EVE market tools API")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--address", default="0.0.0.0")
    ap.add_argument("--sde", required=True, help="Path to SDE directory")
    args = ap.parse_args(argv)
    sde = SDE(args.sde)
    state = State(sde)
    server = ThreadingHTTPServer((args.address, args.port), make_handler(state))
    server.daemon_threads = True
    print(f"Serving on {args.address}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
