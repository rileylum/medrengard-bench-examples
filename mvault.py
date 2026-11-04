#!/usr/bin/env python3
"""mvault - local vaults for media-platform metadata with tracked-field history."""
import argparse
import json
import os
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
    sub = p.add_subparsers(dest="command", metavar="{init,sync}")
    pi = sub.add_parser("init", help="create a new vault")
    pi.add_argument("name")
    pi.add_argument("url")
    ps = sub.add_parser("sync", help="sync a vault with its source")
    ps.add_argument("name")
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


def load_vault(name):
    path = os.path.join(name, "catalog.json")
    if not os.path.isdir(name):
        raise MVaultError(f"vault '{name}' does not exist")
    if not os.path.isfile(path):
        raise MVaultError(f"invalid vault '{name}': missing catalog.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cat = json.load(f)
    except (OSError, ValueError) as e:
        raise MVaultError(f"invalid vault '{name}': {e}")
    if (not isinstance(cat, dict) or not is_int(cat.get("version")) or cat.get("version") != 3
            or not isinstance(cat.get("source"), str)
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
    return cat


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
    cat = load_vault(name)
    data = fetch_source(cat["source"])

    now = datetime.now().replace(microsecond=0)
    mx = max_timestamp(cat)
    if mx is not None and now <= mx:
        now = mx + timedelta(seconds=1)
    ts = fmt_dt(now)

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
        shutil.copyfile(path, bak)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser.print_usage(sys.stderr)
        print("mvault: error: a subcommand is required (init, sync)", file=sys.stderr)
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
    except MVaultError as e:
        print(f"mvault: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
