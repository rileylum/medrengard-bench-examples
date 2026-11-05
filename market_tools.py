#!/usr/bin/env python3
"""EVE market tooling API.

Launch: python market_tools.py --sde ./sde [--port 8000] [--address 0.0.0.0]
Requires numpy + scipy (HiGHS MILP) for POST /v1/compress.
"""
from __future__ import annotations

import argparse
import bz2
import csv
import gzip
import io
import json
import math
import os
import re
import secrets
import sys
import threading
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:  # optional at import time; required for /v1/compress
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix
except ImportError:  # pragma: no cover
    np = None

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

SHIP_CATEGORY = 6
ASTEROID_CATEGORY = 25  # ore, ice, moon ore (compressed variants included)
GAS_GROUPS = {711, 4168}  # Harvestable Cloud, Compressed Gas


class ApiError(Exception):
    def __init__(self, status: int, error: str, details: str):
        super().__init__(details)
        self.status = status
        self.error = error
        self.details = details


class JsonError(Exception):
    """Error with a fully custom JSON payload."""

    def __init__(self, status: int, payload: dict):
        super().__init__(str(payload))
        self.status = status
        self.payload = payload


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
        # reprocessing data
        self.type_group: dict[int, int] = {}
        self.type_volume: dict[int, float] = {}
        self.type_portion: dict[int, int] = {}
        self.type_by_name: dict[str, int] = {}
        self.type_by_lname: dict[str, int] = {}
        self.group_category: dict[int, int] = {}
        self.group_names: dict[int, str] = {}
        self.materials: dict[int, list[tuple[int, int]]] = {}
        self.reprocess_skill: dict[int, int] = {}
        self.ship_volumes: dict[str, float] = {}

        names_rank: dict[str, tuple] = {}
        lnames_rank: dict[str, tuple] = {}
        f = _open_sde(sde_dir, "invTypes")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        tid = int(row["typeID"])
                        self.type_names[tid] = row["typeName"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    try:
                        self.type_group[tid] = int(row["groupID"])
                    except (ValueError, KeyError, TypeError):
                        pass
                    try:
                        self.type_volume[tid] = float(row["volume"])
                    except (ValueError, KeyError, TypeError):
                        self.type_volume[tid] = 0.0
                    try:
                        self.type_portion[tid] = max(1, int(float(row["portionSize"])))
                    except (ValueError, KeyError, TypeError):
                        self.type_portion[tid] = 1
                    published = row.get("published") == "1"
                    name = row["typeName"]
                    rank = (0 if published else 1, tid)
                    for d, k in ((names_rank, name), (lnames_rank, name.lower())):
                        cur = d.get(k)
                        if cur is None or rank < cur:
                            d[k] = rank
        self.type_by_name = {k: v[1] for k, v in names_rank.items()}
        self.type_by_lname = {k: v[1] for k, v in lnames_rank.items()}
        self._load_reprocessing(sde_dir)
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

    def _load_reprocessing(self, sde_dir: str):
        f = _open_sde(sde_dir, "invGroups")
        if f:
            with f:
                for row in csv.DictReader(f):
                    try:
                        gid = int(row["groupID"])
                        self.group_category[gid] = int(row["categoryID"])
                        self.group_names[gid] = row["groupName"]
                    except (ValueError, KeyError, TypeError):
                        continue
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
                    if q > 0:
                        self.materials.setdefault(tid, []).append((mid, q))
        f = _open_sde(sde_dir, "dgmTypeAttributes")
        if f:
            with f:
                header = f.readline().strip().split(",")
                try:
                    ti, ai = header.index("typeID"), header.index("attributeID")
                    vi, vf = header.index("valueInt"), header.index("valueFloat")
                except ValueError:
                    ti = None
                if ti is not None:
                    for line in f:
                        if ",790," not in line:
                            continue
                        parts = line.strip().split(",")
                        if len(parts) <= max(ti, ai, vi, vf) or parts[ai] != "790":
                            continue
                        val = None
                        for raw in (parts[vi], parts[vf]):
                            try:
                                val = int(float(raw))
                                break
                            except ValueError:
                                continue
                        if val:
                            try:
                                self.reprocess_skill[int(parts[ti])] = val
                            except ValueError:
                                pass
        path = os.path.join(sde_dir, "ship_volumes.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].rstrip()
                    if ":" not in line:
                        continue
                    k, v = line.rsplit(":", 1)
                    k = k.strip().strip("'\"")
                    try:
                        self.ship_volumes[k] = float(v.strip().strip("'\""))
                    except ValueError:
                        continue

    def ore_variants(self) -> dict[int, str]:
        """Classify ore types into variant tiers (base/plus5/plus10/plus15/plus100)."""
        cached = getattr(self, "_variants", None)
        if cached is not None:
            return cached
        families: dict[tuple, list[int]] = {}
        for tid, mats in self.materials.items():
            gid = self.type_group.get(tid)
            if self.group_category.get(gid) != ASTEROID_CATEGORY:
                continue
            key = (gid, frozenset(m for m, _ in mats))
            families.setdefault(key, []).append(tid)
        tiers = [(1.0, "base"), (1.05, "plus5"), (1.10, "plus10"), (1.15, "plus15"), (2.0, "plus100")]
        out: dict[int, str] = {}

        def stripped(tid):
            n = self.type_names.get(tid, "")
            n = re.sub(r"^(Batch\s+)?Compressed\s+", "", n)
            return n

        for tids in families.values():
            totals = {t: sum(q for _, q in self.materials[t]) for t in tids}
            base = min(tids, key=lambda t: (len(stripped(t)), totals[t], t))
            bsum = totals[base]
            for t in tids:
                ratio = totals[t] / bsum if bsum else 1.0
                best = min(tiers, key=lambda tr: abs(tr[0] - ratio))
                out[t] = best[1] if abs(best[0] - ratio) <= 0.02 else "base"
        self._variants = out
        return out

    def resolve(self, name: str):
        tid = self.type_by_name.get(name)
        if tid is None:
            tid = self.type_by_lname.get(name.strip().lower())
        return tid

    def volume(self, tid: int) -> float:
        gid = self.type_group.get(tid)
        if gid is not None and self.group_category.get(gid) == SHIP_CATEGORY:
            gname = self.group_names.get(gid)
            if gname in self.ship_volumes:
                return self.ship_volumes[gname]
        return self.type_volume.get(tid, 0.0)

    def reprocess_class(self, tid: int) -> str:
        gid = self.type_group.get(tid)
        if gid in GAS_GROUPS:
            return "gas"
        if self.group_category.get(gid) == ASTEROID_CATEGORY or tid in self.reprocess_skill:
            return "ore"
        return "scrap"


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


# --------------------------------------------------------------------------- reprocessing

STRUCTURE_SM = {"npc_station": 0.0, "citadel": 0.0, "athanor": 0.02, "tatara": 0.055}
RIG_RM = {None: 0.0, "t1": 1.0, "t2": 3.0}
SECURITY_SEC = {"highsec": 0.0, "lowsec": 0.06, "nullsec": 0.12}
IMPLANT_IM = {None: 0.0, "RX-801": 0.01, "RX-802": 0.02, "RX-804": 0.04}
MARKETS = {"jita": "jita", "amarr": "amarr", "dodixie": "dodixie", "rens": "rens", "hek": "hek"}
MIP_GAP = 1e-7
MIP_TIME_LIMIT = 10.0
DEFAULT_EFFICIENCY = {"ore": 0.9063, "gas": 0.95, "scrap": 0.55}


def _invalid(path: str, message: str):
    return JsonError(404, {"code": "INVALID_VALUE", "path": path, "message": message})


def _bad_request(path: str, message: str):
    return JsonError(400, {"error": "INVALID_FORMAT", "details": message,
                           "code": "INVALID_VALUE", "path": path, "message": message})


def _enum(value, allowed, path, default):
    if value is None:
        return default
    if isinstance(value, str):
        if value in allowed:
            return value
        low = {a.lower(): a for a in allowed if isinstance(a, str)}
        if value.strip().lower() in low:
            return low[value.strip().lower()]
    opts = ", ".join("null" if a is None else repr(a) for a in allowed)
    raise _invalid(path, f"Invalid value {value!r} for {path}; expected one of: {opts}")


def parse_config(body) -> dict:
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _invalid("", "Config body must be a JSON object")
    structure = body.get("structure")
    if structure is None:
        structure = {}
    if not isinstance(structure, dict):
        raise _invalid("structure", "structure must be an object or null")
    stype = _enum(structure.get("type"), list(STRUCTURE_SM), "structure.type", "npc_station")
    rig_raw = structure.get("rig")
    rig = _enum(rig_raw, [r for r in RIG_RM if r is not None], "structure.rig", None) if rig_raw is not None else None
    if stype == "npc_station" and rig is not None:
        raise _invalid("structure.rig", "npc_station cannot specify a rig; rig must be null")
    sec = _enum(structure.get("security"), list(SECURITY_SEC), "structure.security", "highsec")
    skills_raw = body.get("skills")
    if skills_raw is None:
        skills_raw = {}
    if not isinstance(skills_raw, dict):
        raise _invalid("skills", "skills must be an object mapping skill names to levels 0-5")
    skills = {}
    for name, lvl in skills_raw.items():
        path = f"skills.{name}"
        if lvl is None:
            lvl = 0
        if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) or (isinstance(lvl, float) and not lvl.is_integer()):
            raise _invalid(path, f"Skill level for {name!r} must be an integer 0-5, got {lvl!r}")
        lvl = int(lvl)
        if lvl < 0 or lvl > 5:
            raise _invalid(path, f"Skill level for {name!r} must be between 0 and 5, got {lvl}")
        skills[name] = lvl
    imp_raw = body.get("implant")
    implant = _enum(imp_raw, [i for i in IMPLANT_IM if i is not None], "implant", None) if imp_raw is not None else None
    return {"type": stype, "rig": rig, "security": sec, "skills": skills, "implant": implant}


def _skill(skills: dict, name: str) -> int:
    if name in skills:
        return skills[name]
    low = name.lower()
    for k, v in skills.items():
        if k.lower() == low:
            return v
    return 0


def config_yield(cfg: dict, op_level: int) -> float:
    rm = RIG_RM[cfg["rig"]]
    sec = SECURITY_SEC[cfg["security"]]
    sm = STRUCTURE_SM[cfg["type"]]
    r = _skill(cfg["skills"], "Reprocessing")
    re_ = _skill(cfg["skills"], "Reprocessing Efficiency")
    im = IMPLANT_IM[cfg["implant"]]
    y = ((50 + rm) * (1 + sec) * (1 + sm) * (1 + r * 0.03) * (1 + re_ * 0.02)
         * (1 + op_level * 0.02) * (1 + im))
    return round(round(y, 2) / 100.0, 4)


def config_scrap_yield(cfg: dict) -> float:
    return round((50 + _skill(cfg["skills"], "Scrapmetal Processing")) / 100.0, 4)


class YieldModel:
    def __init__(self, sde: SDE, efficiency: dict | None = None, cfg: dict | None = None):
        self.sde = sde
        self.eff = efficiency
        self.cfg = cfg
        self.used: dict[str, list[float]] = {"ore": [], "gas": [], "scrap": []}

    def for_type(self, tid: int) -> float:
        cls = self.sde.reprocess_class(tid)
        if self.cfg is None:
            y = self.eff[cls]
        elif cls == "scrap":
            y = config_scrap_yield(self.cfg)
        else:
            op = 0
            sk = self.sde.reprocess_skill.get(tid)
            if sk is not None and cls == "ore":
                sname = self.sde.type_names.get(sk)
                if sname:
                    op = _skill(self.cfg["skills"], sname)
            y = config_yield(self.cfg, op)
        self.used[cls].append(y)
        return y

    def report(self) -> dict:
        out = {}
        for cls in ("ore", "gas", "scrap"):
            if self.used[cls]:
                out[cls] = min(self.used[cls])
            elif self.cfg is None:
                out[cls] = self.eff[cls]
            elif cls == "scrap":
                out[cls] = config_scrap_yield(self.cfg)
            else:
                out[cls] = config_yield(self.cfg, 0)
        return out


def reprocess(sde: SDE, items: dict[int, int], model: YieldModel, drill_down: bool) -> dict[int, int]:
    def can(tid, q):
        return tid in sde.materials and q >= sde.type_portion.get(tid, 1)

    products: dict[int, int] = {}
    pending = dict(items)
    while True:
        work = {t: q for t, q in pending.items() if can(t, q)}
        if not work:
            for t, q in pending.items():
                if q > 0:
                    products[t] = products.get(t, 0) + q
            break
        new: dict[int, int] = {}
        for t, q in pending.items():
            if t not in work and q > 0:
                new[t] = new.get(t, 0) + q
        for t, q in work.items():
            portion = sde.type_portion.get(t, 1)
            n = q // portion
            left = q - n * portion
            if left:
                new[t] = new.get(t, 0) + left
            y = model.for_type(t)
            for mid, mq in sde.materials[t]:
                out = int(math.floor(mq * n * y + 1e-9))
                if out > 0:
                    new[mid] = new.get(mid, 0) + out
        if not drill_down:
            for t, q in new.items():
                if q > 0:
                    products[t] = products.get(t, 0) + q
            break
        pending = new
    return products


def sde_mq(sde, tid: int, mid: int) -> int:
    return sum(q for m, q in sde.materials.get(tid, []) if m == mid)


# --------------------------------------------------------------------------- state


class _PortionEvaluator:
    """Exact (floor-rounded) yield/cost evaluation and local repair of a portion plan."""

    def __init__(self, sde, cands, targets, mats, cpm, tax):
        self.sde = sde
        self.info = {c[0]: c for c in cands}
        self.targets = targets
        self.mats = mats
        self.cpm = cpm
        self.tax = tax
        self.tmats = {tid: [(m, q) for m, q in sde.materials[tid] if m in targets and m in set(mats)]
                      for tid in self.info}

    def yield_of(self, tid, m_q, n):
        return int(math.floor(m_q * n * self.info[tid][4] + 1e-9)) if n > 0 else 0

    def produced(self, portions):
        out = {m: 0 for m in self.mats}
        for tid, n in portions.items():
            for m, q in self.tmats[tid]:
                out[m] += self.yield_of(tid, q, n)
        return out

    def units_cost(self, tid, start_units, units):
        """Cost of buying `units` more after `start_units` already bought; None if not enough volume."""
        _t, _cls, portion, vol, _y, _ymap, sells = self.info[tid]
        have = start_units
        need = units
        cost = 0.0
        for o in sells:
            if need <= 0:
                break
            if have >= o.volume_remain:
                have -= o.volume_remain
                continue
            take = min(o.volume_remain - have, need)
            have = 0
            cost += take * o.price
            need -= take
        if need > 0:
            return None
        return cost * (1 + self.tax) + units * vol * self.cpm

    def cost(self, portions):
        tot = 0.0
        for tid, n in portions.items():
            if n > 0:
                tot += self.units_cost(tid, 0, n * self.info[tid][2])
        return tot

    def finalize(self, portions):
        portions = dict(portions)
        got = self.produced(portions)
        # repair shortfalls greedily (cheapest cost per unit of shortfall covered)
        for _ in range(1000000):
            short = {m: self.targets[m] - got[m] for m in self.mats if got[m] < self.targets[m]}
            if not short:
                break
            best = None
            for tid, (_t, _c, portion, _v, y, ymap, _s) in self.info.items():
                n = portions.get(tid, 0)
                gain = 0
                for m, q in self.tmats[tid]:
                    if m in short:
                        d = self.yield_of(tid, q, n + 1) - self.yield_of(tid, q, n)
                        gain += min(d, short[m]) / max(self.targets[m], 1)
                if gain <= 0:
                    continue
                nc = self.units_cost(tid, n * portion, portion)
                if nc is None:
                    continue
                score = nc / gain
                if best is None or score < best[0]:
                    best = (score, tid)
            if best is None:
                return None
            tid = best[1]
            n = portions.get(tid, 0)
            for m, q in self.tmats[tid]:
                got[m] += self.yield_of(tid, q, n + 1) - self.yield_of(tid, q, n)
            portions[tid] = n + 1
        # prune: drop portions (most expensive marginal first) while targets still hold
        changed = True
        while changed:
            changed = False
            order = []
            for tid, n in portions.items():
                if n > 0:
                    portion = self.info[tid][2]
                    order.append((-(self.units_cost(tid, (n - 1) * portion, portion) or 0.0), tid))
            order.sort()
            for _neg, tid in order:
                while portions[tid] > 0:
                    n = portions[tid]
                    ok = True
                    for m, q in self.tmats[tid]:
                        d = self.yield_of(tid, q, n) - self.yield_of(tid, q, n - 1)
                        if got[m] - d < self.targets[m]:
                            ok = False
                            break
                    if not ok:
                        break
                    for m, q in self.tmats[tid]:
                        got[m] -= self.yield_of(tid, q, n) - self.yield_of(tid, q, n - 1)
                    portions[tid] = n - 1
                    changed = True
        return {t: n for t, n in portions.items() if n > 0}


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
        self.api_keys: dict[str, dict] = {}

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

    # ---- reprocessing
    def create_config(self, body) -> str:
        cfg = parse_config(body)
        key = secrets.token_hex(16)
        with self.lock:
            self.api_keys[key] = cfg
        return key

    def _hub_prices(self, market_key: str):
        """Return a pricing function for the hub, or None if no data loaded."""
        with self.lock:
            self._rebuild()
            hid = self.hub_ids[market_key]
            at_hub = [o for o in self._merged.values() if o.location_id == hid]
            if not at_hub and market_key in self.books:
                at_hub = list(self.books[market_key].values())
            if not at_hub:
                return None
        by_type: dict[int, tuple[list, list]] = {}
        for o in at_hub:
            b, s_ = by_type.setdefault(o.type_id, ([], []))
            (b if o.is_buy else s_).append(o)
        cache: dict[int, tuple] = {}

        def price(tid: int):
            if tid in cache:
                return cache[tid]
            buys, sells = by_type.get(tid, ([], []))
            bt, st = thresholds(buys, sells)
            fb, fs = apply_filter(buys, sells, bt, st)
            res = (
                max((o.price for o in fb), default=None),
                min((o.price for o in fs), default=None),
                five_pct(fb, True),
                five_pct(fs, False),
            )
            cache[tid] = res
            return res

        return price

    def reprocess(self, body, api_key):
        cfg = None
        if api_key is not None and api_key.strip() != "":
            with self.lock:
                cfg = self.api_keys.get(api_key.strip())
            if cfg is None:
                raise JsonError(401, {"code": "BAD_API_KEY"})
        if not isinstance(body, dict):
            raise _bad_request("", "Request body must be a JSON object")
        items = body.get("items")
        if not isinstance(items, dict) or not items:
            raise _bad_request("items", "items must be a non-empty object mapping item names to quantities")
        for name, q in items.items():
            if isinstance(q, bool) or not isinstance(q, (int, float)) or (isinstance(q, float) and not q.is_integer()) or q <= 0:
                raise _bad_request(f"items.{name}", f"Quantity for {name!r} must be a positive integer, got {q!r}")
        eff_raw = body.get("efficiency")
        if eff_raw is None:
            eff_raw = {}
        if not isinstance(eff_raw, dict):
            raise _bad_request("efficiency", "efficiency must be an object")
        eff = dict(DEFAULT_EFFICIENCY)
        for k in ("ore", "gas", "scrap"):
            v = eff_raw.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 1):
                raise _bad_request(f"efficiency.{k}", f"efficiency.{k} must be a number between 0 and 1, got {v!r}")
            eff[k] = float(v)
        drill = body.get("drill_down", False)
        if drill is None:
            drill = False
        if not isinstance(drill, bool):
            raise _bad_request("drill_down", "drill_down must be a boolean")
        if "market" in body:
            mraw = body["market"]
            if mraw is None:
                market = None
            elif isinstance(mraw, str) and mraw.strip().lower() in MARKETS:
                market = MARKETS[mraw.strip().lower()]
            else:
                raise _bad_request("market", f"market must be one of Jita, Amarr, Dodixie, Rens, Hek or null, got {mraw!r}")
        else:
            market = "jita"

        resolved: dict[int, int] = {}
        unknown = []
        for name, q in items.items():
            tid = self.sde.resolve(name) if isinstance(name, str) else None
            if tid is None:
                unknown.append(name)
                continue
            resolved[tid] = resolved.get(tid, 0) + int(q)
        if unknown:
            raise JsonError(404, {"error": "UNKNOWN_ITEMS", "details": sorted(unknown)})

        model = YieldModel(self.sde, efficiency=eff, cfg=cfg)
        products = reprocess(self.sde, resolved, model, drill)
        price = self._hub_prices(market) if market is not None else None

        def summary(qtys: dict[int, int]):
            vol = sum(q * self.sde.volume(t) for t, q in qtys.items())
            out = {"volume": round(vol, 2), "buy": None, "sell": None, "buy_5pct": None, "sell_5pct": None}
            if price is not None:
                tot = [0.0, 0.0, 0.0, 0.0]
                for t, q in qtys.items():
                    for i, p in enumerate(price(t)):
                        if p is not None:
                            tot[i] += q * p
                out["buy"], out["sell"], out["buy_5pct"], out["sell_5pct"] = (round(x, 2) for x in tot)
            return out

        prod_out = {}
        for t, q in sorted(products.items(), key=lambda kv: self.sde.type_names.get(kv[0], "")):
            entry = {"quantity": q, "volume": round(q * self.sde.volume(t), 2), "buy": None, "sell": None}
            if price is not None:
                b, s_, _b5, _s5 = price(t)
                entry["buy"] = round(q * b, 2) if b is not None else None
                entry["sell"] = round(q * s_, 2) if s_ is not None else None
            prod_out[self.sde.type_names.get(t, str(t))] = entry

        return {
            "inputs": summary(resolved),
            "outputs": summary(products),
            "yields": model.report(),
            "products": prod_out,
        }

    # ---- compression
    def _hub_order_list(self, keys: list[str]) -> list[Order]:
        with self.lock:
            self._rebuild()
            out: list[Order] = []
            merged = list(self._merged.values())
            for key in keys:
                hid = self.hub_ids[key]
                at_hub = [o for o in merged if o.location_id == hid]
                if not at_hub and key in self.books:
                    at_hub = list(self.books[key].values())
                out.extend(at_hub)
        seen = set()
        uniq = []
        for o in out:
            if o.order_id not in seen:
                seen.add(o.order_id)
                uniq.append(o)
        return uniq

    def compress(self, body):
        if np is None:
            raise ApiError(500, "INTERNAL_ERROR", "numpy/scipy are required for /v1/compress")
        if not isinstance(body, dict):
            raise _bad_request("", "Request body must be a JSON object")
        mats = body.get("materials")
        if not isinstance(mats, dict) or not mats:
            raise _bad_request("materials", "materials must be a non-empty object mapping material names to quantities")
        for name, q in mats.items():
            if isinstance(q, bool) or not isinstance(q, (int, float)) or (isinstance(q, float) and not q.is_integer()) or q < 0:
                raise _bad_request(f"materials.{name}", f"Quantity for {name!r} must be a non-negative integer, got {q!r}")
        hubs_raw = body.get("hubs")
        if hubs_raw is None:
            hubs_raw = ["jita"]
        if isinstance(hubs_raw, str):
            hubs_raw = [hubs_raw]
        if not isinstance(hubs_raw, list) or not hubs_raw:
            raise _bad_request("hubs", "hubs must be a non-empty list of hub names")
        hubs: list[str] = []
        for h in hubs_raw:
            if not isinstance(h, str) or h.strip().lower() not in MARKETS:
                raise _bad_request("hubs", f"Unknown hub {h!r}; expected one of Jita, Amarr, Dodixie, Rens, Hek")
            k = MARKETS[h.strip().lower()]
            if k not in hubs:
                hubs.append(k)

        def num(key, default, lo=0.0, hi=None):
            v = body.get(key)
            if v is None:
                return default
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi) or v != v:
                raise _bad_request(key, f"{key} must be a non-negative number, got {v!r}")
            return float(v)

        cpm = num("cost_per_m3", 0.0)
        tax = num("collateral_tax", 0.0)
        eff_raw = body.get("efficiencies")
        if eff_raw is None:
            eff_raw = {}
        if not isinstance(eff_raw, dict):
            raise _bad_request("efficiencies", "efficiencies must be an object")
        eff = dict(DEFAULT_EFFICIENCY)
        for k in ("ore", "gas", "scrap"):
            v = eff_raw.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 1):
                raise _bad_request(f"efficiencies.{k}", f"efficiencies.{k} must be a number between 0 and 1, got {v!r}")
            eff[k] = float(v)
        var_raw = body.get("ore_variants")
        if var_raw is None:
            var_raw = {}
        if not isinstance(var_raw, dict):
            raise _bad_request("ore_variants", "ore_variants must be an object")
        variants = {}
        for k in ("base", "plus5", "plus10", "plus15"):
            v = var_raw.get(k, True)
            if v is None:
                v = True
            if not isinstance(v, bool):
                raise _bad_request(f"ore_variants.{k}", f"ore_variants.{k} must be a boolean")
            variants[k] = v
        v100 = var_raw.get("plus100")
        variants["plus100"] = variants["plus15"] if not isinstance(v100, bool) else v100

        sde = self.sde
        targets: dict[int, int] = {}
        unknown = []
        for name, q in mats.items():
            tid = sde.resolve(name) if isinstance(name, str) else None
            if tid is None:
                unknown.append(name)
                continue
            targets[tid] = targets.get(tid, 0) + int(q)
        if unknown:
            raise JsonError(404, {"error": "UNKNOWN_ITEMS", "details": sorted(unknown)})

        orders = self._hub_order_list(hubs)
        by_type: dict[int, tuple[list, list]] = {}
        for o in orders:
            b, s_ = by_type.setdefault(o.type_id, ([], []))
            (b if o.is_buy else s_).append(o)
        price_cache: dict[int, tuple] = {}

        def p5(tid):
            if tid not in price_cache:
                buys, sells = by_type.get(tid, ([], []))
                bt, st = thresholds(buys, sells)
                fb, fs = apply_filter(buys, sells, bt, st)
                price_cache[tid] = (five_pct(fb, True), five_pct(fs, False))
            return price_cache[tid]

        # ---- candidates
        vmap = sde.ore_variants()
        active = {t for t, q in targets.items() if q > 0}
        cands = []  # (tid, cls, portion, volume, yields{mid: per-portion float}, sorted sell orders)
        for tid, (_b, sells) in by_type.items():
            sells = [o for o in sells if o.volume_remain > 0]
            if not sells or tid not in sde.materials:
                continue
            gid = sde.type_group.get(tid)
            if gid in GAS_GROUPS:
                cls = "gas"
            elif sde.group_category.get(gid) == ASTEROID_CATEGORY:
                cls = "ore"
                if not variants.get(vmap.get(tid, "base"), True):
                    continue
            else:
                continue
            y = eff[cls]
            ymap = {m: q * y for m, q in sde.materials[tid] if m in active}
            if not ymap:
                continue
            sells.sort(key=lambda o: (o.price, o.order_id))
            cands.append((tid, cls, sde.type_portion.get(tid, 1), sde.volume(tid), y, ymap, sells))
        if active:
            if not cands:
                raise ApiError(404, "NO_CANDIDATES",
                               "No ore or gas with sell orders in the selected hubs yields the requested materials")
            covered = set()
            for c in cands:
                covered.update(c[5])
            missing = sorted(sde.type_names.get(m, str(m)) for m in active - covered)
            if missing:
                raise ApiError(404, "NO_CANDIDATES",
                               "No ore or gas with sell orders in the selected hubs yields: " + ", ".join(missing))
        cands.sort(key=lambda c: c[0])

        # ---- feasibility pre-check (upper bound on yields)
        for m in active:
            cap = 0
            for tid, cls, portion, vol, y, ymap, sells in cands:
                if m in ymap:
                    n = sum(o.volume_remain for o in sells) // portion
                    cap += int(math.floor(sde_mq(sde, tid, m) * n * y + 1e-9))
            if cap < targets[m]:
                raise ApiError(400, "INFEASIBLE",
                               f"Cannot meet target for {sde.type_names.get(m, m)}: requested {targets[m]}, "
                               f"at most {cap} obtainable from available sell volume")

        portions: dict[int, int] = {}
        if active:
            portions = self._solve_compress(cands, targets, active, cpm, tax)

        # ---- build purchases
        buy_list = {}
        items_total = 0.0
        total_volume = 0.0
        produced: dict[int, int] = {}
        for tid, cls, portion, vol, y, ymap, sells in cands:
            n = portions.get(tid, 0)
            if n <= 0:
                continue
            need = n * portion
            purchases: dict[str, list] = {}
            cost = 0.0
            for o in sells:
                if need <= 0:
                    break
                take = min(need, o.volume_remain)
                need -= take
                cost += take * o.price
                st = self.station_name(o.location_id)
                pr = purchases.setdefault(st, [0, 0.0])
                pr[0] += take
                pr[1] += take * o.price
            qty = n * portion
            items_total += cost
            total_volume += qty * vol
            for mid, mq in sde.materials[tid]:
                out = int(math.floor(mq * n * y + 1e-9))
                if out > 0:
                    produced[mid] = produced.get(mid, 0) + out
            buy_list[sde.type_names.get(tid, str(tid))] = {
                "quantity": qty,
                "cost": round(cost, 2),
                "volume": round(qty * vol, 2),
                "category": cls,
                "purchases": {
                    st: {"quantity": q, "total": round(t, 2), "avg_price": round(t / q, 2) if q else 0.0}
                    for st, (q, t) in purchases.items()
                },
            }
        buy_list = dict(sorted(buy_list.items()))

        shipping = total_volume * cpm
        collateral = items_total * tax
        raw_ship = 0.0
        material_value = 0.0
        excess_value = 0.0
        yields = {}
        for m in sorted(set(produced) | set(targets), key=lambda t: sde.type_names.get(t, str(t))):
            actual = produced.get(m, 0)
            b5, s5 = p5(m)
            tgt = targets.get(m)
            if tgt is not None:
                raw_ship += tgt * sde.volume(m) * cpm
                if s5 is not None:
                    material_value += tgt * s5
            excess = actual - (tgt or 0)
            if excess > 0 and s5 is not None:
                excess_value += excess * s5
            yields[sde.type_names.get(m, str(m))] = {
                "target": tgt,
                "actual": actual,
                "buy": round(actual * b5, 2) if b5 is not None else None,
                "sell": round(actual * s5, 2) if s5 is not None else None,
            }
        return {
            "buy_list": buy_list,
            "total": round(items_total + shipping + collateral, 2),
            "shipping": round(shipping, 2),
            "collateral_tax": round(collateral, 2),
            "items": round(items_total, 2),
            "raw_ship": round(raw_ship, 2),
            "material_value": round(material_value, 2),
            "excess_value": round(excess_value, 2),
            "yields": yields,
        }

    def _solve_compress(self, cands, targets, active, cpm, tax) -> dict[int, int]:
        sde = self.sde
        mats = sorted(active)
        midx = {m: i for i, m in enumerate(mats)}
        cost = []
        lb = []
        ub = []
        integ = []
        rows, cols, vals = [], [], []
        clb, cub = [], []
        nvar = 0
        nrow = 0

        def var(c, lo, hi, is_int):
            nonlocal nvar
            cost.append(c)
            lb.append(lo)
            ub.append(hi)
            integ.append(1 if is_int else 0)
            nvar += 1
            return nvar - 1

        target_rows: dict[int, list[int]] = {m: [] for m in mats}
        n_vars = {}
        for tid, cls, portion, vol, y, ymap, sells in cands:
            avail = sum(o.volume_remain for o in sells)
            nmax = avail // portion
            # more portions than needed to alone cover every target it yields cannot help
            useful = max(math.ceil(targets[m] / per) + 1 for m, per in ymap.items() if per > 0) if any(
                per > 0 for per in ymap.values()) else 0
            nmax = min(nmax, useful)
            if nmax <= 0:
                continue
            nv = var(0.0, 0, nmax, True)
            n_vars[tid] = nv
            # sum_o x_o - portion*n == 0
            r = nrow
            nrow += 1
            cum = 0
            for o in sells:
                if cum >= nmax * portion:
                    break
                cum += o.volume_remain
                xv = var(o.price * (1 + tax) + vol * cpm, 0, o.volume_remain, False)
                rows.append(r); cols.append(xv); vals.append(1.0)
            rows.append(r); cols.append(nv); vals.append(-float(portion))
            clb.append(0.0); cub.append(0.0)
            for m, per in ymap.items():
                yv = var(0.0, 0, math.floor(per * nmax + 1e-9), True)
                r = nrow
                nrow += 1
                rows.append(r); cols.append(yv); vals.append(1.0)
                rows.append(r); cols.append(nv); vals.append(-per)
                clb.append(-np.inf); cub.append(1e-9)
                target_rows[m].append(yv)
        for m in mats:
            r = nrow
            nrow += 1
            for yv in target_rows[m]:
                rows.append(r); cols.append(yv); vals.append(1.0)
            clb.append(float(targets[m])); cub.append(np.inf)
        A = coo_matrix((vals, (rows, cols)), shape=(nrow, nvar)).tocsr()
        c_arr = np.array(cost)
        cons = LinearConstraint(A, np.array(clb), np.array(cub))
        bnds = Bounds(np.array(lb, dtype=float), np.array(ub, dtype=float))
        ev = _PortionEvaluator(sde, cands, targets, mats, cpm, tax)

        # 1) LP relaxation -> rounded, repaired, pruned heuristic solution
        lp = milp(c=c_arr, constraints=cons, bounds=bnds, options={"time_limit": 30.0, "disp": False})
        if lp.status == 2:
            raise ApiError(400, "INFEASIBLE", "Cannot meet material targets with the available market volume")
        best = None
        if lp.x is not None:
            start = {tid: max(0, int(math.floor(lp.x[nv] + 1e-6))) for tid, nv in n_vars.items()}
            sol = ev.finalize(start)
            if sol is not None:
                best = (ev.cost(sol), sol)
        # 2) exact MILP (time limited), keep the better of the two
        res = milp(c=c_arr, constraints=cons, integrality=np.array(integ), bounds=bnds,
                   options={"time_limit": MIP_TIME_LIMIT, "mip_rel_gap": MIP_GAP, "disp": False})
        if res.x is not None:
            start = {tid: max(0, int(round(res.x[nv]))) for tid, nv in n_vars.items()}
            sol = ev.finalize(start)
            if sol is not None:
                c_ = ev.cost(sol)
                if best is None or c_ < best[0] - 1e-6:
                    best = (c_, sol)
        if best is None:
            raise ApiError(400, "INFEASIBLE", "Cannot meet material targets with the available market volume")
        return best[1]



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

        def _json_body(self, err):
            raw = self._body()
            if not raw.strip():
                return None
            try:
                return json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise err(f"Request body is not valid JSON: {exc}")

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
                if method == "POST" and parts == ["v1", "config"]:
                    body = self._json_body(lambda m: _invalid("", m))
                    return self._send(201, {"key": state.create_config(body)})
                if method == "POST" and parts == ["v1", "reprocess"]:
                    api_key = self.headers.get("X-API-Key")
                    body = self._json_body(lambda m: _bad_request("", m))
                    return self._send(201, state.reprocess(body, api_key))
                if method == "POST" and parts == ["v1", "compress"]:
                    body = self._json_body(lambda m: _bad_request("", m))
                    return self._send(200, state.compress(body))
                if method == "POST":
                    self._body()
                return self._send(404, {"error": "NOT_FOUND", "details": f"No route for {method} {url.path}"})
            except JsonError as exc:
                return self._send(exc.status, exc.payload)
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
