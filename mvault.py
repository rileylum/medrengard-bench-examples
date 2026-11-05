#!/usr/bin/env python3
"""mvault - local vaults for media-platform metadata with tracked-field history."""
import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone

CATEGORIES = ("episodes", "streams", "clips")
STATIC_FIELDS = ("id", "published", "width", "height")
SOURCE_TRACKED = ("title", "description", "views", "likes", "preview")
TRACKED = SOURCE_TRACKED + ("removed",)
TS_FMT = "%Y-%m-%dT%H:%M:%S"


class MVaultError(Exception):
    pass


def eprint(msg):
    print(msg, file=sys.stderr)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_str(v):
    return isinstance(v, str)


def is_int_or_null(v):
    return v is None or is_int(v)


SOURCE_SCHEMA = {
    "id": is_str,
    "published": is_str,
    "width": is_int,
    "height": is_int,
    "title": is_str,
    "description": is_str,
    "views": is_int,
    "likes": is_int_or_null,
    "preview": is_str,
}


def parse_dt(text):
    """Parse ISO 8601 datetime or date-only text; return naive datetime (seconds)."""
    if not isinstance(text, str):
        raise ValueError("not a string")
    s = text.strip()
    if not s:
        raise ValueError("empty")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d")
    else:
        if len(s) > 10 and s[10] in (" ", "t"):
            s = s[:10] + "T" + s[11:]
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def fmt_dt(dt):
    return dt.strftime(TS_FMT)


def normalize_published(text):
    return fmt_dt(parse_dt(text))


def history_sort_key(k):
    try:
        return (0, parse_dt(k), k)
    except (ValueError, TypeError):
        return (1, datetime.min, k)


def current_value(history):
    if not isinstance(history, dict) or not history:
        return None, False
    key = max(history.keys(), key=history_sort_key)
    return history[key], True


# ---------------------------------------------------------------- vault io

def catalog_path(name):
    return os.path.join(name, "catalog.json")


def backup_path(name):
    return os.path.join(name, "catalog.bak")


def write_catalog(name, catalog, backup=True):
    path = catalog_path(name)
    if backup and os.path.exists(path):
        try:
            shutil.copyfile(path, backup_path(name))
        except OSError as exc:
            raise MVaultError("cannot write backup '%s': %s" % (backup_path(name), exc))
    data = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    try:
        fd, tmp = tempfile.mkstemp(prefix=".catalog.", suffix=".tmp", dir=name)
    except OSError as exc:
        raise MVaultError("cannot write catalog for vault '%s': %s" % (name, exc))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise MVaultError("cannot write catalog for vault '%s': %s" % (name, exc))
        raise


SUPPORTED_VERSIONS = (1, 2, 3)
CURRENT_VERSION = 3
V1_SOURCE_PREFIX = "https://media.example.com/channel/"

HISTORY_VALUE_CHECKS = {
    "title": is_str,
    "description": is_str,
    "views": is_int,
    "likes": is_int_or_null,
    "preview": is_str,
    "removed": lambda v: isinstance(v, bool),
}


def v1_source_url(source_id):
    return V1_SOURCE_PREFIX + source_id


def epoch_key_to_iso(key):
    """Convert a UNIX-epoch-seconds string key to YYYY-MM-DDTHH:MM:SS (UTC)."""
    if not isinstance(key, str):
        raise ValueError("epoch key is not a string")
    s = key.strip()
    if not s:
        raise ValueError("empty epoch key")
    try:
        secs = int(s)
    except ValueError:
        f = float(s)  # may raise ValueError
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("invalid epoch key")
        secs = int(f)
    dt = datetime(1970, 1, 1) + timedelta(seconds=secs)
    return fmt_dt(dt)


def read_raw_catalog(name):
    """Read catalog.json; return (catalog_dict, version). No modification."""
    if not os.path.isdir(name):
        raise MVaultError("vault '%s' does not exist" % name)
    path = catalog_path(name)
    if not os.path.isfile(path):
        raise MVaultError("invalid vault '%s': missing catalog.json" % name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    except (OSError, ValueError) as exc:
        raise MVaultError("invalid vault '%s': cannot read catalog.json (%s)" % (name, exc))
    if not isinstance(catalog, dict):
        raise MVaultError("invalid vault '%s': catalog is not an object" % name)
    if "version" not in catalog:
        raise MVaultError("invalid vault '%s': catalog version is missing" % name)
    version = catalog["version"]
    if not is_int(version):
        raise MVaultError(
            "invalid vault '%s': catalog version must be an integer (got %s)"
            % (name, json.dumps(version)))
    if version > CURRENT_VERSION:
        raise MVaultError(
            "invalid vault '%s': unsupported catalog version %d (newer than supported "
            "version %d)" % (name, version, CURRENT_VERSION))
    if version not in SUPPORTED_VERSIONS:
        raise MVaultError(
            "invalid vault '%s': unsupported catalog version %d" % (name, version))
    return catalog, version


def _check_entry(name, where, e, version, idx):
    label = "%s entry #%d" % (where, idx)
    if not isinstance(e, dict):
        raise MVaultError("invalid vault '%s': malformed %s (not an object)" % (name, label))
    if not is_str(e.get("id")):
        raise MVaultError("invalid vault '%s': malformed %s (invalid 'id')" % (name, label))
    if version in (1, 2):
        if "published" in e and not is_str(e["published"]):
            raise MVaultError(
                "invalid vault '%s': malformed %s (invalid 'published')" % (name, label))
        for f in ("width", "height"):
            if f in e and not is_int(e[f]):
                raise MVaultError(
                    "invalid vault '%s': malformed %s (invalid '%s')" % (name, label, f))
    fields = SOURCE_TRACKED if version in (1, 2) else TRACKED
    for field in fields:
        if field not in e:
            continue
        h = e[field]
        if not isinstance(h, dict):
            raise MVaultError(
                "invalid vault '%s': malformed %s (history '%s' is not an object)"
                % (name, label, field))
        if version == 3:
            continue
        check = HISTORY_VALUE_CHECKS[field]
        for k, v in h.items():
            try:
                if version == 1:
                    epoch_key_to_iso(k)
                else:
                    parse_dt(k)
            except (ValueError, TypeError, OverflowError):
                raise MVaultError(
                    "invalid vault '%s': malformed %s (invalid history key %s in '%s')"
                    % (name, label, json.dumps(k), field))
            if not check(v):
                raise MVaultError(
                    "invalid vault '%s': malformed %s (invalid value in history '%s')"
                    % (name, label, field))
    if version == 3 and "annotations" in e and not isinstance(e["annotations"], list):
        raise MVaultError(
            "invalid vault '%s': malformed %s ('annotations' is not an array)" % (name, label))


def validate_catalog(name, catalog, version):
    if version == 1:
        if not is_str(catalog.get("source_id")):
            raise MVaultError("invalid vault '%s': missing or invalid 'source_id'" % name)
        entries = catalog.get("entries")
        if not isinstance(entries, list):
            raise MVaultError("invalid vault '%s': 'entries' is not an array" % name)
        for i, e in enumerate(entries):
            _check_entry(name, "entries", e, 1, i)
        return
    if not is_str(catalog.get("source")):
        raise MVaultError("invalid vault '%s': missing source URL" % name)
    for cat in CATEGORIES:
        entries = catalog.get(cat)
        if not isinstance(entries, list):
            raise MVaultError("invalid vault '%s': '%s' is not an array" % (name, cat))
        for i, e in enumerate(entries):
            _check_entry(name, cat, e, version, i)


def migration_timestamp():
    return fmt_dt(datetime.now().replace(microsecond=0))


def _migrate_entry(e, version, ts):
    out = {}
    for k, v in e.items():
        if version == 1 and k in SOURCE_TRACKED and isinstance(v, dict):
            out[k] = {epoch_key_to_iso(hk): hv
                      for hk, hv in sorted(v.items(), key=lambda kv: int(float(kv[0])))}
        else:
            out[k] = copy.deepcopy(v)
    out["removed"] = {ts: False}
    out["annotations"] = []
    return out


def migrate_to_v3(catalog, version, ts=None):
    """Return a new v3 catalog built from a validated v1/v2/v3 catalog."""
    if version == CURRENT_VERSION:
        return catalog
    if ts is None:
        ts = migration_timestamp()
    new = {"version": CURRENT_VERSION}
    if version == 1:
        new["source"] = v1_source_url(catalog["source_id"])
        new["episodes"] = [_migrate_entry(e, 1, ts) for e in catalog["entries"]]
        new["streams"] = []
        new["clips"] = []
        skip = ("version", "source_id", "entries")
    else:
        new["source"] = catalog["source"]
        for cat in CATEGORIES:
            new[cat] = [_migrate_entry(e, 2, ts) for e in catalog[cat]]
        skip = ("version", "source") + CATEGORIES
    for k, v in catalog.items():
        if k not in skip and k not in new:
            new[k] = copy.deepcopy(v)
    return new


def load_vault(name):
    """Load any supported catalog version; return (v3_catalog, original_version).

    Never modifies anything on disk.
    """
    raw, version = read_raw_catalog(name)
    validate_catalog(name, raw, version)
    try:
        catalog = migrate_to_v3(raw, version)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MVaultError("invalid vault '%s': malformed entry data (%s)" % (name, exc))
    return catalog, version


def load_catalog(name):
    return load_vault(name)[0]


# ---------------------------------------------------------------- source

def fetch_source(url):
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", None)
            if status is not None and not (200 <= status < 300):
                raise MVaultError("source metadata fetch failed: HTTP %s" % status)
            raw = resp.read()
    except MVaultError:
        raise
    except Exception as exc:
        raise MVaultError("source metadata fetch failed: %s" % exc)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise MVaultError("source metadata fetch failed: invalid JSON (%s)" % exc)
    if not isinstance(data, dict):
        raise MVaultError("source metadata fetch failed: response is not an object")
    result = {}
    for cat in CATEGORIES:
        entries = data.get(cat)
        if not isinstance(entries, list):
            raise MVaultError("source metadata fetch failed: '%s' is not an array" % cat)
        seen = {}
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                raise MVaultError(
                    "source metadata fetch failed: malformed %s entry #%d" % (cat, i))
            for field, check in SOURCE_SCHEMA.items():
                if field not in e:
                    raise MVaultError(
                        "source metadata fetch failed: %s entry #%d missing '%s'" % (cat, i, field))
                if not check(e[field]):
                    raise MVaultError(
                        "source metadata fetch failed: %s entry #%d has invalid '%s'"
                        % (cat, i, field))
            try:
                published = normalize_published(e["published"])
            except (ValueError, TypeError):
                raise MVaultError(
                    "source metadata fetch failed: %s entry #%d has invalid 'published'"
                    % (cat, i))
            item = dict(e)
            item["published"] = published
            seen[e["id"]] = item
        result[cat] = seen
    return result


# ---------------------------------------------------------------- sync

def all_history_keys(catalog):
    for cat in CATEGORIES:
        for e in catalog[cat]:
            for field in TRACKED:
                h = e.get(field)
                if isinstance(h, dict):
                    for k in h:
                        yield k


def compute_sync_timestamp(catalog):
    now = datetime.now().replace(microsecond=0)
    latest = None
    for k in all_history_keys(catalog):
        try:
            dt = parse_dt(k)
        except (ValueError, TypeError):
            continue
        if latest is None or dt > latest:
            latest = dt
    if latest is not None and now <= latest:
        now = latest + timedelta(seconds=1)
    return fmt_dt(now)


def append_history(entry, field, value, ts):
    h = entry.get(field)
    if not isinstance(h, dict):
        h = {}
        entry[field] = h
    cur, has = current_value(h)
    if has and cur == value and type(cur) is type(value):
        return False
    key = ts
    # Guarantee the new key is strictly newer than any existing key.
    existing = [history_sort_key(k)[1] for k in h if history_sort_key(k)[0] == 0]
    if existing:
        newest = max(existing)
        tdt = parse_dt(key)
        if tdt <= newest:
            key = fmt_dt(newest + timedelta(seconds=1))
    while key in h:
        key = fmt_dt(parse_dt(key) + timedelta(seconds=1))
    h[key] = value
    return True


def sort_entries(entries):
    def pub_key(e):
        p = e.get("published")
        try:
            return parse_dt(p)
        except (ValueError, TypeError):
            return datetime.min
    entries.sort(key=lambda e: str(e.get("id")))
    entries.sort(key=pub_key, reverse=True)


def sync_catalog(catalog, source, ts):
    for cat in CATEGORIES:
        entries = catalog[cat]
        by_id = {}
        for e in entries:
            by_id.setdefault(e["id"], e)
        src = source[cat]
        for sid, item in src.items():
            entry = by_id.get(sid)
            if entry is None:
                entry = {
                    "id": item["id"],
                    "published": item["published"],
                    "width": item["width"],
                    "height": item["height"],
                }
                for field in SOURCE_TRACKED:
                    entry[field] = {ts: item[field]}
                entry["removed"] = {ts: False}
                entries.append(entry)
                by_id[sid] = entry
                continue
            for field in SOURCE_TRACKED:
                append_history(entry, field, item[field], ts)
            removed, has = current_value(entry.get("removed"))
            if not has or removed is not False:
                append_history(entry, "removed", False, ts)
        for e in entries:
            if e["id"] in src:
                continue
            removed, has = current_value(e.get("removed"))
            if not has or removed is not True:
                append_history(e, "removed", True, ts)
        sort_entries(entries)
    return catalog


# ---------------------------------------------------------------- commands

def cmd_init(args):
    name, url = args.name, args.url
    if os.path.lexists(name):
        raise MVaultError("vault '%s' already exists" % name)
    try:
        os.makedirs(name)
    except OSError as exc:
        raise MVaultError("cannot create vault '%s': %s" % (name, exc))
    catalog = {
        "version": 3,
        "source": url,
        "episodes": [],
        "streams": [],
        "clips": [],
    }
    write_catalog(name, catalog, backup=False)
    print("Initialized vault '%s'" % name)
    return 0


def cmd_sync(args):
    name = args.name
    catalog, _version = load_vault(name)
    source = fetch_source(catalog["source"])
    ts = compute_sync_timestamp(catalog)
    sync_catalog(catalog, source, ts)
    write_catalog(name, catalog, backup=True)
    print("Synced vault '%s' at %s" % (name, ts))
    return 0


def cmd_migrate(args):
    name = args.name
    catalog, version = load_vault(name)
    if version == CURRENT_VERSION:
        print("Vault '%s' is already at version %d; nothing to migrate"
              % (name, CURRENT_VERSION))
        return 0
    write_catalog(name, catalog, backup=True)
    print("Migrated vault '%s' from version %d to version %d"
          % (name, version, CURRENT_VERSION))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mvault.py",
        description="Create local vaults for media-platform metadata and record "
                    "tracked-field history by sync timestamp.")
    sub = parser.add_subparsers(dest="command", metavar="{init,sync,migrate}",
                                title="subcommands")
    p_init = sub.add_parser("init", help="create a new vault with an empty catalog")
    p_init.add_argument("name", help="vault directory name")
    p_init.add_argument("url", help="source metadata URL")
    p_init.set_defaults(func=cmd_init)
    p_sync = sub.add_parser("sync", help="sync a vault with its source URL")
    p_sync.add_argument("name", help="vault directory name")
    p_sync.set_defaults(func=cmd_sync)
    p_mig = sub.add_parser("migrate", help="convert a legacy (v1/v2) vault catalog to v3")
    p_mig.add_argument("name", help="vault directory name")
    p_mig.set_defaults(func=cmd_migrate)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_usage(sys.stderr)
        eprint("mvault.py: error: a subcommand is required")
        return 2
    try:
        return args.func(args)
    except MVaultError as exc:
        eprint("error: %s" % exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
