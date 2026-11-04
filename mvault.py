#!/usr/bin/env python3
"""mvault - local vaults for media-platform metadata with tracked-field history."""
import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CATEGORIES = ("episodes", "streams", "clips")
TRACKED = ("title", "description", "views", "likes", "preview")
FMT = "%Y-%m-%dT%H:%M:%S"


class MVaultError(Exception):
    pass


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_dt(text):
    if not isinstance(text, str):
        raise ValueError("not a string")
    s = text.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d")
    else:
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def fmt_dt(dt):
    return dt.strftime(FMT)


def build_parser():
    p = argparse.ArgumentParser(
        prog="mvault",
        description="Create local vaults for media-platform metadata and record "
                    "tracked-field history by sync timestamp.")
    sub = p.add_subparsers(dest="command", metavar="{init,sync,migrate}")
    pi = sub.add_parser("init", help="create a new vault")
    pi.add_argument("name")
    pi.add_argument("url")
    ps = sub.add_parser("sync", help="sync a vault with its source")
    ps.add_argument("name")
    pm = sub.add_parser("migrate", help="convert a legacy vault catalog to version 3")
    pm.add_argument("name")
    return p


def cmd_init(name, url):
    if os.path.lexists(name):
        raise MVaultError(f"vault '{name}' already exists")
    try:
        os.makedirs(name)
    except OSError as e:
        raise MVaultError(f"cannot create vault '{name}': {e}")
    catalog = {"version": 3, "source": url, "episodes": [], "streams": [], "clips": []}
    with open(os.path.join(name, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")


SOURCE_PREFIX = "https://media.example.com/channel/"
NATIVE_VERSION = 3
_EPOCH_RE = re.compile(r"^\s*-?\d+\s*$")


def read_raw_catalog(name):
    """Read and JSON-decode catalog.json, checking the version field."""
    path = os.path.join(name, "catalog.json")
    if not os.path.isdir(name):
        raise MVaultError(f"vault '{name}' does not exist")
    if not os.path.isfile(path):
        raise MVaultError(f"invalid vault '{name}': missing catalog.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cat = json.load(f)
    except (OSError, ValueError) as e:
        raise MVaultError(f"invalid vault '{name}': cannot parse catalog.json: {e}")
    if not isinstance(cat, dict):
        raise MVaultError(f"invalid vault '{name}': malformed catalog.json")
    if "version" not in cat:
        raise MVaultError(f"invalid vault '{name}': catalog.json has no version field")
    ver = cat["version"]
    if not is_int(ver):
        raise MVaultError(
            f"invalid vault '{name}': unsupported catalog version {ver!r} (version must be an integer)")
    if ver > NATIVE_VERSION:
        raise MVaultError(
            f"invalid vault '{name}': unsupported catalog version {ver} "
            f"(newest supported version is {NATIVE_VERSION})")
    if ver < 1:
        raise MVaultError(f"invalid vault '{name}': unsupported catalog version {ver}")
    return cat


def validate_v3(name, cat):
    if (not isinstance(cat.get("source"), str)
            or not all(isinstance(cat.get(c), list) for c in CATEGORIES)):
        raise MVaultError(f"invalid vault '{name}': malformed catalog.json")
    for c in CATEGORIES:
        for e in cat[c]:
            if not isinstance(e, dict) or not isinstance(e.get("id"), str):
                raise MVaultError(f"invalid vault '{name}': malformed entry in {c}")
            for fld in TRACKED + ("removed",):
                if fld in e and not isinstance(e[fld], dict):
                    raise MVaultError(f"invalid vault '{name}': malformed history in {c}")
                for k in e.get(fld, {}):
                    try:
                        parse_dt(k)
                    except ValueError:
                        raise MVaultError(f"invalid vault '{name}': bad timestamp {k!r}")


def epoch_to_iso(key):
    if not isinstance(key, str) or not _EPOCH_RE.match(key):
        raise ValueError(f"bad UNIX-epoch key {key!r}")
    try:
        dt = datetime.fromtimestamp(int(key.strip()), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"bad UNIX-epoch key {key!r}")
    return fmt_dt(dt.replace(tzinfo=None))


def _value_ok(fld, v):
    if fld in ("title", "description", "preview"):
        return isinstance(v, str)
    if fld == "views":
        return is_int(v)
    if fld == "likes":
        return v is None or is_int(v)
    return True


def _convert_legacy_entry(name, where, e, epoch_keys):
    """Validate a v1/v2 entry and return a converted (v3-key) deep copy."""
    bad = f"invalid vault '{name}': malformed entry in {where}"
    if not isinstance(e, dict) or not isinstance(e.get("id"), str):
        raise MVaultError(bad)
    if "published" in e:
        try:
            parse_dt(e["published"])
        except (ValueError, TypeError):
            raise MVaultError(f"{bad}: bad published value for {e['id']!r}")
    for f in ("width", "height"):
        if f in e and not is_int(e[f]):
            raise MVaultError(f"{bad}: bad {f} for {e['id']!r}")
    out = {}
    for k, v in e.items():
        if k in TRACKED:
            if not isinstance(v, dict):
                raise MVaultError(f"{bad}: {k} of {e['id']!r} is not a history object")
            hist = {}
            # chronological order of keys
            items = []
            for hk, hv in v.items():
                if not _value_ok(k, hv):
                    raise MVaultError(f"{bad}: bad {k} value for {e['id']!r}")
                try:
                    if epoch_keys:
                        if not _EPOCH_RE.match(hk):
                            raise ValueError(hk)
                        sort_key = int(hk.strip())
                        nk = epoch_to_iso(hk)
                    else:
                        sort_key = parse_dt(hk)
                        nk = hk
                except (ValueError, TypeError):
                    raise MVaultError(f"{bad}: bad timestamp {hk!r} in {k} of {e['id']!r}")
                items.append((sort_key, nk, hv))
            items.sort(key=lambda t: t[0])
            for _, nk, hv in items:
                hist[nk] = hv
            out[k] = hist
        else:
            out[k] = json.loads(json.dumps(v))
    return out


def migrate_catalog(name, cat, ts):
    """Return a v3 catalog converted from a v1/v2 catalog. `cat` is not modified."""
    ver = cat["version"]
    if ver == NATIVE_VERSION:
        return cat
    new = {"version": NATIVE_VERSION}
    if ver == 1:
        sid = cat.get("source_id")
        if not isinstance(sid, str) or not isinstance(cat.get("entries"), list):
            raise MVaultError(f"invalid vault '{name}': malformed version 1 catalog.json")
        new["source"] = SOURCE_PREFIX + sid
        new["episodes"] = [_convert_legacy_entry(name, "entries", e, True) for e in cat["entries"]]
        new["streams"] = []
        new["clips"] = []
        skip = {"version", "source_id", "entries", "source"} | set(CATEGORIES)
    elif ver == 2:
        if (not isinstance(cat.get("source"), str)
                or not all(isinstance(cat.get(c), list) for c in CATEGORIES)):
            raise MVaultError(f"invalid vault '{name}': malformed version 2 catalog.json")
        new["source"] = cat["source"]
        for c in CATEGORIES:
            new[c] = [_convert_legacy_entry(name, c, e, False) for e in cat[c]]
        skip = {"version", "source"} | set(CATEGORIES)
    else:
        raise MVaultError(f"invalid vault '{name}': unsupported catalog version {ver}")
    for k, v in cat.items():
        if k not in skip:
            new[k] = v
    for c in CATEGORIES:
        for e in new[c]:
            e["removed"] = {ts: False}
            e["annotations"] = []
    return new


def legacy_max_timestamp(cat):
    """Latest history timestamp in a raw (possibly legacy) catalog, or None."""
    m = None
    ver = cat.get("version")
    groups = ["entries"] if ver == 1 else list(CATEGORIES)
    for g in groups:
        for e in cat.get(g, []) or []:
            if not isinstance(e, dict):
                continue
            for fld in TRACKED + ("removed",):
                h = e.get(fld)
                if not isinstance(h, dict):
                    continue
                for k in h:
                    try:
                        d = parse_dt(epoch_to_iso(k)) if ver == 1 else parse_dt(k)
                    except (ValueError, TypeError):
                        continue
                    if m is None or d > m:
                        m = d
    return m


def now_ts(cat):
    now = datetime.now().replace(microsecond=0)
    mx = legacy_max_timestamp(cat)
    if mx is not None and now <= mx:
        now = mx + timedelta(seconds=1)
    return fmt_dt(now)


def load_vault_ex(name, ts=None):
    """Load any supported vault version into a v3 in-memory catalog.

    Returns (catalog, original_version). Nothing is written to disk."""
    raw = read_raw_catalog(name)
    ver = raw["version"]
    if ver == NATIVE_VERSION:
        validate_v3(name, raw)
        return raw, ver
    if ts is None:
        ts = now_ts(raw)
    cat = migrate_catalog(name, raw, ts)
    validate_v3(name, cat)
    return cat, ver


def load_vault(name):
    return load_vault_ex(name)[0]


def cmd_migrate(name):
    cat, ver = load_vault_ex(name)
    if ver == NATIVE_VERSION:
        return
    write_catalog(name, cat)


def check_source_entry(e):
    if not isinstance(e, dict):
        return False
    for f in ("id", "published", "title", "description", "preview"):
        if not isinstance(e.get(f), str):
            return False
    for f in ("width", "height", "views"):
        if not is_int(e.get(f)):
            return False
    if "likes" not in e or not (e["likes"] is None or is_int(e["likes"])):
        return False
    try:
        parse_dt(e["published"])
    except (ValueError, TypeError):
        return False
    return True


def fetch_source(url):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise MVaultError(f"failed to fetch source metadata from {url}: {e}")
    if not isinstance(data, dict) or not all(isinstance(data.get(c), list) for c in CATEGORIES):
        raise MVaultError(f"failed to fetch source metadata from {url}: malformed response")
    for c in CATEGORIES:
        seen = set()
        for e in data[c]:
            if not check_source_entry(e):
                raise MVaultError(
                    f"failed to fetch source metadata from {url}: malformed entry in {c}")
            if e["id"] in seen:
                raise MVaultError(
                    f"failed to fetch source metadata from {url}: duplicate id {e['id']!r} in {c}")
            seen.add(e["id"])
    return data


def current(hist):
    if not hist:
        return (False, None)
    key = max(hist, key=parse_dt)
    return (True, hist[key])


def same(a, b):
    if type(a) is not type(b):
        return False
    return a == b


def max_timestamp(cat):
    m = None
    for c in CATEGORIES:
        for e in cat[c]:
            for fld in TRACKED + ("removed",):
                for k in e.get(fld, {}) or {}:
                    d = parse_dt(k)
                    if m is None or d > m:
                        m = d
    return m


def set_hist(entry, field, value, ts):
    hist = entry.get(field)
    if not isinstance(hist, dict):
        hist = {}
        entry[field] = hist
    has, cur = current(hist)
    if has and same(cur, value):
        return
    hist[ts] = value


def cmd_sync(name):
    raw = read_raw_catalog(name)
    ts = now_ts(raw)
    cat, _ver = load_vault_ex(name, ts)
    data = fetch_source(cat["source"])

    mx = max_timestamp(cat)
    if mx is not None and parse_dt(ts) < mx:
        ts = fmt_dt(mx + timedelta(seconds=1))

    for c in CATEGORIES:
        existing = {e["id"]: e for e in cat[c]}
        src_ids = set()
        for se in data[c]:
            sid = se["id"]
            src_ids.add(sid)
            entry = existing.get(sid)
            if entry is None:
                entry = {
                    "id": sid,
                    "published": fmt_dt(parse_dt(se["published"])),
                    "width": se["width"],
                    "height": se["height"],
                }
                for f in TRACKED:
                    entry[f] = {ts: se[f]}
                entry["removed"] = {ts: False}
                cat[c].append(entry)
                existing[sid] = entry
            else:
                for f in TRACKED:
                    set_hist(entry, f, se[f], ts)
                set_hist(entry, "removed", False, ts)
        for e in cat[c]:
            if e["id"] not in src_ids:
                set_hist(e, "removed", True, ts)

        def pub_key(e):
            try:
                return fmt_dt(parse_dt(e.get("published")))
            except (ValueError, TypeError):
                return ""
        cat[c].sort(key=lambda e: e["id"])
        cat[c].sort(key=pub_key, reverse=True)

    write_catalog(name, cat)


def write_catalog(name, cat):
    path = os.path.join(name, "catalog.json")
    bak = os.path.join(name, "catalog.bak")
    tmp = os.path.join(name, "catalog.json.tmp")
    if os.path.exists(path):
        try:
            shutil.copyfile(path, bak)
        except OSError as e:
            raise MVaultError(f"cannot write backup for vault '{name}': {e}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cat, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise MVaultError(f"cannot write catalog for vault '{name}': {e}")


def main(argv=None):
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser.print_usage(sys.stderr)
        print("mvault: error: a subcommand is required (init, sync, migrate)", file=sys.stderr)
        return 2
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        if args.command == "init":
            cmd_init(args.name, args.url)
        elif args.command == "sync":
            cmd_sync(args.name)
        elif args.command == "migrate":
            cmd_migrate(args.name)
    except MVaultError as e:
        print(f"mvault: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
