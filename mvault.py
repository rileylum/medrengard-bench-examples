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
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8840


def viewer_link(name, category, entry_id):
    from urllib.parse import quote
    host = os.environ.get("MVAULT_VIEWER_HOST") or DEFAULT_HOST
    port = os.environ.get("MVAULT_VIEWER_PORT") or str(DEFAULT_PORT)
    return (f"http://{host}:{port}/catalog/{quote(str(name), safe='')}/"
            f"{quote(str(category), safe='')}/{quote(str(entry_id), safe='')}")


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


def nonneg_int(text):
    s = text.strip() if isinstance(text, str) else ""
    if not re.fullmatch(r"\+?\d+", s):
        raise argparse.ArgumentTypeError(f"invalid non-negative integer: {text!r}")
    return int(s)


def format_str(text):
    s = text.strip().lstrip(".") if isinstance(text, str) else ""
    if not s or "/" in s or "\\" in s:
        raise argparse.ArgumentTypeError(f"invalid format: {text!r}")
    return s


def build_parser():
    p = argparse.ArgumentParser(
        prog="mvault", allow_abbrev=False,
        description="Create local vaults for media-platform metadata and record "
                    "tracked-field history by sync timestamp.")
    sub = p.add_subparsers(dest="command", metavar="{init,sync,migrate,digest,serve}")
    pi = sub.add_parser("init", help="create a new vault", allow_abbrev=False)
    pi.add_argument("name")
    pi.add_argument("url")
    ps = sub.add_parser("sync", help="sync a vault with its source", allow_abbrev=False)
    ps.add_argument("name")
    for c in CATEGORIES:
        ps.add_argument(f"--{c}", type=nonneg_int, default=None, metavar="N",
                        help=f"maximum number of {c[:-1]} media downloads")
    ps.add_argument("--skip-metadata", action="store_true",
                    help="skip source fetch and metadata update")
    ps.add_argument("--skip-download", action="store_true",
                    help="skip media and preview downloads")
    ps.add_argument("--format", type=format_str, default=None,
                    help="override media download format and extension")
    pm = sub.add_parser("migrate", help="convert a legacy vault catalog to version 3",
                        allow_abbrev=False)
    pm.add_argument("name")
    pd = sub.add_parser("digest", help="summarize notable changes in a vault",
                        allow_abbrev=False)
    pd.add_argument("name")
    pv = sub.add_parser("serve", help="start the local browser viewer", allow_abbrev=False)
    pv.add_argument("name", nargs="?", default=None)
    pv.add_argument("--host", default=DEFAULT_HOST)
    pv.add_argument("--port", type=port_int, default=DEFAULT_PORT)
    return p


def port_int(text):
    s = text.strip() if isinstance(text, str) else ""
    if not re.fullmatch(r"\d+", s) or int(s) > 65535:
        raise argparse.ArgumentTypeError(f"invalid port: {text!r}")
    return int(s)


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
    """Record value; return True if the history changed."""
    hist = entry.get(field)
    if not isinstance(hist, dict):
        hist = {}
        entry[field] = hist
    has, cur = current(hist)
    if has and same(cur, value):
        return False
    hist[ts] = value
    return True


def cmd_sync(name, opts=None):
    opts = opts or {}
    if opts.get("skip_metadata"):
        cat = load_vault(name)
    else:
        cat, counts = sync_metadata(name)
    if not opts.get("skip_download"):
        download_phase(name, cat, opts)
    if not opts.get("skip_metadata"):
        print(f"sync summary: {counts['added']} added, {counts['removed']} removed, "
              f"{counts['updated']} updated")
        for kind, c, eid, fields in counts["changes"]:
            entry = next((e for e in cat[c] if e["id"] == eid), None)
            title = _entry_title(entry, NATIVE_VERSION) if entry else eid
            extra = f" ({', '.join(fields)})" if fields else ""
            print(f"  {kind}: {title}{extra} {viewer_link(name, c, eid)}")
        sys.stdout.flush()


def sync_metadata(name):
    counts = {"added": 0, "removed": 0, "updated": 0, "changes": []}
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
                counts["added"] += 1
                counts["changes"].append(("added", c, sid, []))
            else:
                changed = []
                if set_hist(entry, "removed", False, ts):
                    changed.append("reappeared")
                for f in TRACKED:
                    if set_hist(entry, f, se[f], ts):
                        changed.append(f)
                if changed:
                    counts["updated"] += 1
                    counts["changes"].append(("updated", c, sid, changed))
        for e in cat[c]:
            if e["id"] not in src_ids:
                if set_hist(e, "removed", True, ts):
                    counts["removed"] += 1
                    counts["changes"].append(("removed", c, e["id"], []))

        def pub_key(e):
            try:
                return fmt_dt(parse_dt(e.get("published")))
            except (ValueError, TypeError):
                return ""
        cat[c].sort(key=lambda e: e["id"])
        cat[c].sort(key=pub_key, reverse=True)

    write_catalog(name, cat)
    return cat, counts


# ---------------------------------------------------------------- downloads

PARTIAL_SUFFIXES = (".part", ".tmp", ".partial", ".download", ".crdownload")
MIME_EXT = {
    "video/mp4": "mp4", "video/webm": "webm", "video/x-matroska": "mkv",
    "video/quicktime": "mov", "video/x-msvideo": "avi", "video/mpeg": "mpg",
    "video/ogg": "ogv", "video/x-flv": "flv", "video/3gpp": "3gp",
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/ogg": "ogg", "audio/webm": "weba",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac", "audio/aac": "aac",
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/svg+xml": "svg", "image/bmp": "bmp", "image/avif": "avif",
    "application/octet-stream": "bin", "text/plain": "txt", "application/json": "json",
    "text/html": "html", "application/x-mpegurl": "m3u8",
    "application/vnd.apple.mpegurl": "m3u8",
}
RETRIES = 3
RETRY_DELAYS = (0.5, 1.0, 2.0)


class PermanentDownloadError(Exception):
    pass


class TransientDownloadError(Exception):
    pass


def safe_name(entry_id):
    s = entry_id.replace("/", "_").replace("\\", "_").replace("\0", "_")
    if s in ("", ".", ".."):
        s = "_" + s
    return s


def ext_from_content_type(ct):
    import mimetypes
    mt = (ct or "").split(";", 1)[0].strip().lower()
    if not mt:
        return "bin"
    if mt in MIME_EXT:
        return MIME_EXT[mt]
    guess = mimetypes.guess_extension(mt)
    if guess:
        return guess.lstrip(".")
    sub = mt.split("/", 1)[-1]
    sub = re.sub(r"[^a-z0-9]+", "", sub.split("+")[0].replace("x-", "", 1))
    return sub or "bin"


def find_existing(directory, entry_id, fmt=None):
    base = safe_name(entry_id)
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for n in sorted(names):
        if n.lower().endswith(PARTIAL_SUFFIXES) or n.startswith("."):
            continue
        p = os.path.join(directory, n)
        if not os.path.isfile(p):
            continue
        if n == base:
            return p
        if n.startswith(base + "."):
            rest = n[len(base) + 1:]
            if rest and ("." not in rest or (fmt is not None and rest == fmt)):
                return p
    return None


def http_fetch_to(url, directory, entry_id, fmt=None):
    """Download url into directory; return final path. Raises download errors."""
    import http.client
    import socket
    import urllib.error
    base = safe_name(entry_id)
    tmp = os.path.join(directory, f"{base}.{os.getpid()}.part")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            ct = r.headers.get("Content-Type", "")
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        ext = fmt if fmt is not None else ext_from_content_type(ct)
        final = os.path.join(directory, f"{base}.{ext}")
        os.replace(tmp, final)
        return final
    except urllib.error.HTTPError as e:
        code = e.code
        if code in (408, 425, 429) or code >= 500:
            raise TransientDownloadError(f"HTTP {code}")
        raise PermanentDownloadError(f"HTTP {code}")
    except (urllib.error.URLError, http.client.HTTPException, socket.timeout,
            ConnectionError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise TransientDownloadError(str(reason))
    except ValueError as e:
        raise PermanentDownloadError(str(e))
    except OSError as e:
        raise TransientDownloadError(str(e))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def download_with_retry(url, directory, entry_id, fmt=None):
    import time
    last = None
    for attempt in range(RETRIES):
        try:
            return http_fetch_to(url, directory, entry_id, fmt)
        except PermanentDownloadError:
            raise
        except TransientDownloadError as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    raise TransientDownloadError(f"{last} (gave up after {RETRIES} attempts)")


def download_phase(name, cat, opts):
    from urllib.parse import quote
    src = cat["source"].rstrip("/")
    fmt = opts.get("format")
    limits = opts.get("limits") or {}
    media_dir = os.path.join(name, "media")
    prev_dir = os.path.join(name, "previews")
    for d in (media_dir, prev_dir):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            raise MVaultError(f"cannot create directory '{d}': {e}")
    for c in CATEGORIES:
        candidates = [e for e in cat[c] if find_existing(media_dir, e["id"], fmt) is None]
        lim = limits.get(c)
        if lim is not None:
            candidates = candidates[:lim]
        for e in candidates:
            eid = e["id"]
            qid = quote(eid, safe="")
            murl = f"{src}/media/{qid}" + (f".{fmt}" if fmt is not None else "")
            try:
                download_with_retry(murl, media_dir, eid, fmt)
            except PermanentDownloadError as ex:
                print(f"mvault: warning: media for {c[:-1]} '{eid}' is unavailable "
                      f"({ex}); skipped", file=sys.stderr)
            except TransientDownloadError as ex:
                print(f"mvault: warning: failed to download media for {c[:-1]} '{eid}' "
                      f"({ex}); skipped", file=sys.stderr)
            if find_existing(prev_dir, eid) is None:
                purl = f"{src}/preview/{qid}"
                try:
                    download_with_retry(purl, prev_dir, eid)
                except PermanentDownloadError as ex:
                    print(f"mvault: warning: preview for {c[:-1]} '{eid}' is unavailable "
                          f"({ex}); skipped", file=sys.stderr)
                except TransientDownloadError as ex:
                    print(f"mvault: warning: failed to download preview for {c[:-1]} "
                          f"'{eid}' ({ex}); skipped", file=sys.stderr)


# ---------------------------------------------------------------- digest

def _epoch_sort_key(k):
    try:
        return (1, int(str(k).strip()), str(k))
    except ValueError:
        try:
            return (1, float(str(k).strip()), str(k))
        except ValueError:
            return (0, 0, str(k))


def _sorted_values(hist, ver):
    if not isinstance(hist, dict) or not hist:
        return []
    if ver == 1:
        keys = sorted(hist, key=_epoch_sort_key)
    else:
        keys = sorted(hist, key=str)
    return [hist[k] for k in keys]


def classify_entry(e, ver):
    """Return (group, changed_fields) or None."""
    if not isinstance(e, dict):
        return None
    if ver == NATIVE_VERSION:
        rv = _sorted_values(e.get("removed"), ver)
        if rv and rv[-1] is True and (len(rv) < 2 or rv[-2] is False):
            return ("removals", [])
    changed = []
    multi = False
    for f in TRACKED:
        vals = _sorted_values(e.get(f), ver)
        if len(vals) >= 2:
            multi = True
            if not same(vals[-1], vals[-2]):
                changed.append(f)
    reappeared = False
    if ver == NATIVE_VERSION:
        rv = _sorted_values(e.get("removed"), ver)
        if len(rv) >= 2:
            multi = True
            if rv[-1] is False and rv[-2] is True:
                reappeared = True
    if not multi:
        return ("additions", [])
    if reappeared:
        changed.insert(0, "reappeared")
    if changed:
        return ("updates", changed)
    return None


def _entry_title(e, ver):
    vals = _sorted_values(e.get("title"), ver)
    if vals and isinstance(vals[-1], str):
        return vals[-1]
    return str(e.get("id", "?"))


def cmd_digest(name):
    cat = read_raw_catalog(name)
    ver = cat["version"]
    if ver == 1:
        if not isinstance(cat.get("entries"), list):
            raise MVaultError(f"invalid vault '{name}': malformed version 1 catalog.json")
        sid = cat.get("source_id")
        source = SOURCE_PREFIX + (sid if isinstance(sid, str) else str(sid))
        groups = [("Entries", cat["entries"], "entries")]
    else:
        if (not isinstance(cat.get("source"), str)
                or not all(isinstance(cat.get(c), list) for c in CATEGORIES)):
            raise MVaultError(f"invalid vault '{name}': malformed version {ver} catalog.json")
        source = cat["source"]
        groups = [(c.capitalize(), cat[c], c) for c in CATEGORIES]

    order = [("removals", "Removals"), ("additions", "Additions"),
             ("updates", "Field updates")]
    if ver != NATIVE_VERSION:
        order = order[1:]
    lines = []
    total = 0
    for label, entries, catname in groups:
        buckets = {k: [] for k, _ in order}
        for e in entries:
            r = classify_entry(e, ver)
            if r is None or r[0] not in buckets:
                continue
            text = _entry_title(e, ver)
            if r[1]:
                text += " (" + ", ".join(r[1]) + ")"
            text += " " + viewer_link(name, catname, e.get("id", ""))
            buckets[r[0]].append(text)
        if not any(buckets.values()):
            continue
        lines.append(f"{label}:")
        for key, glabel in order:
            if not buckets[key]:
                continue
            lines.append(f"  {glabel} ({len(buckets[key])}):")
            for t in buckets[key]:
                lines.append(f"    - {t}")
                total += 1
    if not lines:
        lines.append("No notable changes found.")
    gen = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"-- digest of vault '{name}' (catalog version {ver}, {total} changes) "
                 f"from {source} generated {gen}")
    sys.stdout.write("\n".join(lines) + "\n")


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


# ---------------------------------------------------------------- viewer

import html as _html
from urllib.parse import quote as _quote, unquote as _unquote, parse_qs as _parse_qs

RECENT_COOKIE = "mvault_recent"
RECENT_MAX = 20
_server_recent = []


def _esc(v):
    return _html.escape(str(v), quote=True)


class ViewerVault:
    """Read-only view of a vault of any supported version (no migration)."""

    def __init__(self, name):
        if (not name or name in (".", "..") or "/" in name or "\\" in name
                or "\0" in name):
            raise MVaultError(f"vault '{name}' does not exist")
        self.name = name
        cat = read_raw_catalog(name)
        self.version = cat["version"]
        self.cat = cat
        if self.version == 1:
            self.categories = ("entries",)
        else:
            self.categories = CATEGORIES
        for c in self.categories:
            if not isinstance(cat.get(c), list):
                raise MVaultError(f"invalid vault '{name}': malformed catalog.json")

    @property
    def default_category(self):
        return self.categories[0]

    def entries(self, category):
        return [e for e in self.cat.get(category, [])
                if isinstance(e, dict) and isinstance(e.get("id"), (str, int))]

    def title(self, e):
        return _entry_title(e, self.version)

    def removed(self, e):
        if self.version != NATIVE_VERSION:
            return False
        vals = _sorted_values(e.get("removed"), self.version)
        return bool(vals) and vals[-1] is True

    def media_names(self):
        try:
            return [n for n in os.listdir(os.path.join(self.name, "media"))]
        except OSError:
            return []


def resolve_default(name):
    """Return the default category route for a vault, or None."""
    try:
        v = ViewerVault(name)
    except Exception:
        return None
    return f"/catalog/{_quote(name, safe='')}/{v.default_category}"


def _page(title, body):
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{_esc(title)}</title><style>"
            "body{font-family:sans-serif;margin:2em;}"
            "table{border-collapse:collapse;}td,th{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left;}"
            ".downloaded .state{color:#070;font-weight:bold;}"
            ".not-downloaded .state{color:#999;}"
            ".removed{text-decoration:line-through;opacity:0.6;background:#fee;}"
            ".highlight{background:#ffd;outline:2px solid #e90;}"
            ".error{color:#b00;font-weight:bold;}"
            "nav a{margin-right:1em;}nav a.current{font-weight:bold;}"
            "</style></head><body>\n" + body + "\n</body></html>\n")


def _parse_recent(cookie_header):
    if not cookie_header:
        return None
    from http.cookies import SimpleCookie
    try:
        c = SimpleCookie()
        c.load(cookie_header)
    except Exception:
        return None
    if RECENT_COOKIE not in c:
        return None
    try:
        data = json.loads(_unquote(c[RECENT_COOKIE].value))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, str)][:RECENT_MAX]


def landing_html(recent, notfound=None, error=None):
    parts = ["<h1>mvault viewer</h1>"]
    if notfound is not None:
        parts.append(f"<p class=\"error not-found\" id=\"not-found\">Vault "
                     f"&#39;{_esc(notfound)}&#39; not found.</p>")
    if error:
        parts.append(f"<p class=\"error\">{_esc(error)}</p>")
    parts.append("<form method=\"post\" action=\"/\">"
                 "<label for=\"catalog\">Vault name: </label>"
                 "<input type=\"text\" id=\"catalog\" name=\"catalog\" required>"
                 "<button type=\"submit\">Open</button></form>")
    items = []
    for n in recent:
        route = resolve_default(n)
        if route is None:
            continue
        items.append(f"<li><a class=\"recent-vault\" href=\"{_esc(route)}\">{_esc(n)}</a></li>")
    parts.append("<h2>Recent vaults</h2>")
    if items:
        parts.append("<ul id=\"recent\" class=\"recent-vaults\">" + "".join(items) + "</ul>")
    else:
        parts.append("<p>No recently visited vaults.</p>")
    return _page("mvault viewer", "\n".join(parts))


def listing_html(v, category, highlight=None):
    qn = _quote(v.name, safe="")
    media = v.media_names()
    nav = " ".join(
        f"<a href=\"/catalog/{_esc(qn)}/{c}\"" + (" class=\"current\"" if c == category else "")
        + f">{c}</a>" for c in v.categories)
    rows = []
    for e in v.entries(category):
        eid = str(e["id"])
        downloaded = any(eid in n for n in media)
        removed = v.removed(e)
        classes = ["entry", "downloaded" if downloaded else "not-downloaded"]
        if removed:
            classes.append("removed")
        else:
            classes.append("active")
        if highlight is not None and eid == highlight:
            classes.append("highlight")
        link = f"/catalog/{qn}/{_quote(category, safe='')}/{_quote(eid, safe='')}"
        status = "Downloaded" if downloaded else "Not downloaded"
        rcol = ""
        if v.version == NATIVE_VERSION:
            rcol = f"<td class=\"removed-state\">{'Removed' if removed else 'Active'}</td>"
        rows.append(
            f"<tr class=\"{' '.join(classes)}\" id=\"entry-{_esc(eid)}\" data-id=\"{_esc(eid)}\">"
            f"<td><a href=\"{_esc(link)}\">{_esc(v.title(e))}</a></td>"
            f"<td><code>{_esc(eid)}</code></td>"
            f"<td class=\"state\">{'&#10004; ' if downloaded else '&#10008; '}{status}</td>"
            f"{rcol}</tr>")
    head = "<tr><th>Title</th><th>ID</th><th>Media</th>" + (
        "<th>Status</th>" if v.version == NATIVE_VERSION else "") + "</tr>"
    body = [f"<p><a href=\"/\">&larr; Home</a></p>",
            f"<h1>{_esc(v.name)} &mdash; {_esc(category)}</h1>",
            f"<p>Catalog version {v.version}</p>",
            f"<nav>{nav}</nav>"]
    if highlight is not None:
        match = [e for e in v.entries(category) if str(e["id"]) == highlight]
        if match:
            e = match[0]
            body.append(f"<div class=\"selected-entry highlight\"><h2>{_esc(v.title(e))}</h2>"
                        f"<p>ID: <code>{_esc(highlight)}</code></p></div>")
        else:
            body.append(f"<p class=\"error\">Entry &#39;{_esc(highlight)}&#39; not found "
                        f"in {_esc(category)}.</p>")
    if rows:
        body.append(f"<table class=\"entries\">{head}{''.join(rows)}</table>")
    else:
        body.append("<p>No entries.</p>")
    return _page(f"{v.name} - {category}", "\n".join(body))



def _hist_points(hist, ver):
    """Chronological [(iso_timestamp, value)] pairs for a history object."""
    if not isinstance(hist, dict) or not hist:
        return []
    if ver == 1:
        keys = sorted(hist, key=_epoch_sort_key)
    else:
        keys = sorted(hist, key=str)
    out = []
    for k in keys:
        ts = str(k)
        if ver == 1:
            try:
                ts = epoch_to_iso(ts)
            except ValueError:
                pass
        out.append((ts, hist[k]))
    return out


def _latest(hist, ver):
    vals = _sorted_values(hist, ver)
    return vals[-1] if vals else None


def _usable_name(n):
    return not n.startswith(".") and not n.lower().endswith(PARTIAL_SUFFIXES)


def _match_file(directory, entry_id):
    """Saved filename in directory containing entry_id (exact base preferred)."""
    eid = str(entry_id)
    if not eid:
        return None
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    cands = [n for n in names if eid in n and _usable_name(n)
             and os.path.isfile(os.path.join(directory, n))]
    if not cands:
        return None
    for pred in (lambda n: n == eid,
                 lambda n: n.startswith(eid + ".") and "." not in n[len(eid) + 1:],
                 lambda n: n.startswith(eid + "."),
                 lambda n: n.startswith(eid)):
        for n in cands:
            if pred(n):
                return n
    return cands[0]


def _source_link(v, eid):
    if v.version == 1:
        sid = v.cat.get("source_id")
        return f"{SOURCE_PREFIX}{sid if isinstance(sid, str) else str(sid)}/entry/{eid}"
    return f"{v.cat.get('source')}/entry/{eid}"


def _json_script(obj, elem_id):
    data = json.dumps(obj, ensure_ascii=False)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"<script type=\"application/json\" id=\"{elem_id}\">{data}</script>"


def detail_html(v, category, e, seek=None):
    qn = _quote(v.name, safe="")
    eid = str(e["id"])
    title = _latest(e.get("title"), v.version)
    desc = _latest(e.get("description"), v.version)
    title_s = title if isinstance(title, str) else eid
    desc_s = "" if desc is None else str(desc)
    published = e.get("published", "")
    width = e.get("width", "")
    height = e.get("height", "")
    src_link = _source_link(v, eid)
    back = f"/catalog/{qn}/{_quote(category, safe='')}"
    removed = v.removed(e)
    body = [f"<p><a class=\"back\" id=\"back\" href=\"{_esc(back)}\">&larr; Back to "
            f"{_esc(v.name)} / {_esc(category)}</a></p>",
            f"<h1 class=\"entry-title\" id=\"title\">{_esc(title_s)}</h1>"]
    if removed:
        body.append("<p class=\"removed-state removed\">Removed from source</p>")
    body.append(
        "<dl class=\"entry-meta\">"
        f"<dt>ID</dt><dd class=\"entry-id\"><code>{_esc(eid)}</code></dd>"
        f"<dt>Description</dt><dd class=\"description\" id=\"description\">{_esc(desc_s)}</dd>"
        f"<dt>Published</dt><dd class=\"published\" id=\"published\">{_esc(published)}</dd>"
        f"<dt>Dimensions</dt><dd class=\"dimensions\" id=\"dimensions\">"
        f"<span class=\"width\">{_esc(width)}</span> &times; "
        f"<span class=\"height\">{_esc(height)}</span> ({_esc(width)}x{_esc(height)})</dd>"
        f"<dt>Source</dt><dd><a class=\"source-link\" id=\"source-link\" "
        f"href=\"{_esc(src_link)}\">{_esc(src_link)}</a></dd>"
        "</dl>")
    media = _match_file(os.path.join(v.name, "media"), eid)
    preview = _match_file(os.path.join(v.name, "previews"), eid)
    prev_url = f"/vault/{qn}/preview/{_quote(eid, safe='')}" if preview else None
    if media:
        murl = f"/vault/{qn}/media/{_quote(media, safe='')}"
        poster = f" poster=\"{_esc(prev_url)}\"" if prev_url else ""
        frag = f"#t={seek}" if seek is not None else ""
        sattr = f" data-timecode=\"{seek}\"" if seek is not None else ""
        body.append(f"<div class=\"media\"><video id=\"player\" controls preload=\"metadata\" "
                    f"src=\"{_esc(murl + frag)}\"{poster}{sattr}></video>"
                    f"<p><a class=\"media-link\" href=\"{_esc(murl)}\">{_esc(media)}</a></p></div>")
    else:
        body.append("<p class=\"no-media\">Media not downloaded.</p>")
        if prev_url:
            body.append(f"<p><img class=\"preview\" alt=\"preview\" src=\"{_esc(prev_url)}\" "
                        f"style=\"max-width:480px\"></p>")
    if seek is not None:
        body.append(f"<p class=\"seek\" id=\"seek\" data-timecode=\"{seek}\">"
                    f"Playback position: {_esc(format_timecode(seek))} ({seek} s)</p>")
        body.append("<script>(function(){var v=document.getElementById('player');"
                    f"if(!v)return;var t={seek};"
                    "function s(){try{v.currentTime=t;}catch(e){}}"
                    "if(v.readyState>=1)s();else v.addEventListener('loadedmetadata',s);"
                    "})();</script>")
    body.append(annotations_html(v, category, e))
    charts = {}
    for f in ("views", "likes"):
        pts = _hist_points(e.get(f), v.version)
        charts[f] = [{"timestamp": t, "value": val} for t, val in pts]
    body.append(_json_script(charts, "chart-data"))
    for f in ("views", "likes"):
        pts = charts[f]
        if not pts:
            continue
        body.append(f"<h2>{f.capitalize()}</h2>")
        body.append(_svg_chart(pts))
        rows = "".join(f"<tr><td>{_esc(p['timestamp'])}</td>"
                       f"<td>{'&mdash;' if p['value'] is None else _esc(p['value'])}</td></tr>"
                       for p in pts)
        body.append(f"<table class=\"history {f}\"><tr><th>Timestamp</th><th>{f}</th></tr>"
                    f"{rows}</table>")
    return _page(f"{title_s} - {v.name}", "\n".join(body))


# ---------------------------------------------------------------- annotations

_TIMECODE_RE = re.compile(r"^\d+(?::\d+){0,2}$")


def parse_timecode(text):
    """Parse SS, MM:SS or HH:MM:SS into whole seconds; ValueError if invalid."""
    if is_int(text) and text >= 0:
        return text
    if not isinstance(text, str):
        raise ValueError("timecode must be a string")
    s = text.strip()
    if not s.isascii() or not _TIMECODE_RE.match(s):
        raise ValueError(f"invalid timecode {text!r}")
    total = 0
    for p in s.split(":"):
        total = total * 60 + int(p)
    return total


def format_timecode(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def annotations_html(v, category, e):
    anns = e.get("annotations")
    if not isinstance(anns, list):
        anns = []
    anns = [a for a in anns if isinstance(a, dict)]
    qn = _quote(v.name, safe="")
    base = f"/catalog/{qn}/{_quote(category, safe='')}/{_quote(str(e['id']), safe='')}"
    parts = ["<h2>Annotations</h2>"]
    items = []
    for a in anns:
        aid = str(a.get("id", ""))
        tc = a.get("timecode")
        tc_i = tc if is_int(tc) else None
        title = a.get("title")
        body = a.get("body")
        tc_txt = format_timecode(tc_i) if tc_i is not None else _esc(tc)
        link = f"{base}?timecode={tc_i}" if tc_i is not None else base
        body_html = ("<p class=\"annotation-body\"></p>" if body is None else
                     f"<p class=\"annotation-body\">{_esc(body)}</p>")
        items.append(
            f"<li class=\"annotation\" id=\"annotation-{_esc(aid)}\" data-id=\"{_esc(aid)}\" "
            f"data-timecode=\"{_esc('' if tc_i is None else tc_i)}\">"
            f"<a class=\"annotation-timecode\" href=\"{_esc(link)}\">{_esc(tc_txt)}</a> "
            f"<span class=\"annotation-title\">{_esc('' if title is None else title)}</span>"
            f"{body_html}</li>")
    if items:
        parts.append("<ol class=\"annotations\" id=\"annotations\">" + "".join(items) + "</ol>")
    else:
        parts.append("<p class=\"no-annotations\">No annotations.</p>")
    parts.append(_json_script(anns, "annotation-data"))
    return "\n".join(parts)


def _new_annotation_id(anns):
    import uuid
    used = {str(a.get("id")) for a in anns if isinstance(a, dict)}
    while True:
        aid = uuid.uuid4().hex[:12]
        if aid not in used:
            return aid


def annotation_request(method, name, category, eid, raw):
    """Handle an annotation mutation. Returns (status, location_or_message)."""
    if (not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name
            or not os.path.isdir(name)):
        return 404, f"vault '{name}' does not exist"
    try:
        cat = read_raw_catalog(name)
    except MVaultError as e:
        return 500, str(e)
    ver = cat["version"]
    cats = ("entries",) if ver == 1 else CATEGORIES
    if category not in cats or not isinstance(cat.get(category), list):
        return 404, f"category '{category}' not found in vault '{name}'"
    idx = None
    for i, e in enumerate(cat[category]):
        if isinstance(e, dict) and str(e.get("id")) == eid:
            idx = i
            break
    if idx is None:
        return 404, f"entry '{eid}' not found in {category}"

    # parse and validate the request body before touching the catalog
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError) as e:
        return 400, f"malformed JSON body: {e}"
    if not isinstance(data, dict):
        return 400, "malformed JSON body: expected a JSON object"
    if method == "POST":
        for f in ("title", "timecode"):
            if f not in data or data[f] is None:
                return 400, f"missing required field: {f}"
        if not isinstance(data["title"], str):
            return 400, "invalid field: title must be a string"
        try:
            seconds = parse_timecode(data["timecode"])
        except ValueError:
            return 400, (f"invalid timecode format: {data['timecode']!r} "
                         "(expected SS, MM:SS or HH:MM:SS)")
        body = data.get("body")
        if body is not None and not isinstance(body, str):
            return 400, "invalid field: body must be a string or null"
    else:
        if "id" not in data or data["id"] is None:
            return 400, "missing required field: id"
        if not isinstance(data["id"], (str, int)) or isinstance(data["id"], bool):
            return 400, "invalid field: id must be a string"
        aid = str(data["id"])
        if method == "PATCH":
            if "title" not in data and "body" not in data:
                return 400, "missing field: at least one of title or body is required"
            if "title" in data and not isinstance(data["title"], str):
                return 400, "invalid field: title must be a string"
            if "body" in data and data["body"] is not None and not isinstance(data["body"], str):
                return 400, "invalid field: body must be a string or null"

    # migrate to v3 if needed (in memory only)
    new_cat = category
    try:
        if ver != NATIVE_VERSION:
            cat = migrate_catalog(name, cat, now_ts(cat))
            if ver == 1:
                new_cat = "episodes"
        validate_v3(name, cat)
    except MVaultError as e:
        return 500, f"migration failed: {e}"
    except Exception as e:
        return 500, f"migration failed: {type(e).__name__}: {e}"
    entry = next((e for e in cat[new_cat] if str(e.get("id")) == eid), None)
    if entry is None:
        return 500, f"entry '{eid}' missing after migration"
    anns = entry.get("annotations")
    if anns is None:
        anns = []
        entry["annotations"] = anns
    if not isinstance(anns, list):
        return 500, f"invalid vault '{name}': malformed annotations for entry '{eid}'"

    qn = _quote(name, safe="")
    location = f"/catalog/{qn}/{_quote(new_cat, safe='')}/{_quote(eid, safe='')}"
    if method == "POST":
        anns.append({"id": _new_annotation_id(anns), "timecode": seconds,
                     "title": data["title"], "body": body})
        location += f"?timecode={seconds}"
    else:
        target = next((a for a in anns if isinstance(a, dict) and str(a.get("id")) == aid), None)
        if target is None:
            return 404, f"annotation '{aid}' not found"
        if method == "PATCH":
            if "title" in data:
                target["title"] = data["title"]
            if "body" in data:
                target["body"] = data["body"]
        else:
            anns.remove(target)
    try:
        write_catalog(name, cat)
    except MVaultError as e:
        return 500, str(e)
    return 303, location


def _svg_chart(pts):
    vals = [(i, p["value"]) for i, p in enumerate(pts) if is_int(p["value"])]
    if len(pts) < 2 or not vals:
        return ""
    w, h, pad = 480, 160, 10
    lo = min(x for _, x in vals)
    hi = max(x for _, x in vals)
    span = (hi - lo) or 1
    n = len(pts) - 1
    coords = " ".join(f"{pad + (w - 2 * pad) * i / n:.1f},"
                      f"{h - pad - (h - 2 * pad) * (x - lo) / span:.1f}" for i, x in vals)
    return (f"<svg class=\"chart\" width=\"{w}\" height=\"{h}\" viewBox=\"0 0 {w} {h}\" "
            f"role=\"img\"><polyline fill=\"none\" stroke=\"#36c\" stroke-width=\"2\" "
            f"points=\"{coords}\"/></svg>")


_MIME_BY_EXT = {}
for _mt, _ext in MIME_EXT.items():
    _MIME_BY_EXT.setdefault(_ext, _mt)


def _guess_mime(path, kind):
    import mimetypes
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        head = b""
    if kind == "image":
        if head.startswith(b"\x89PNG"):
            return "image/png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mt = _MIME_BY_EXT.get(ext) or mimetypes.guess_type(path)[0]
    if mt and mt != "application/octet-stream":
        if kind == "image" and not mt.startswith("image/"):
            return "image/jpeg"
        return mt
    if kind == "image":
        return "image/jpeg"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return "application/octet-stream"


def make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "mvault"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- helpers
        def _recent(self):
            r = _parse_recent(self.headers.get("Cookie"))
            if r is None:
                return list(_server_recent)
            return r

        def _send(self, status, body, headers=None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, val in (headers or []):
                self.send_header(k, val)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect(self, location, status=302, headers=None):
            body = _page("Redirect", f"<p>Redirecting to <a href=\"{_esc(location)}\">"
                                     f"{_esc(location)}</a></p>")
            self._send(status, body, [("Location", location)] + list(headers or []))

        def _visit(self, name):
            r = [n for n in self._recent() if n != name]
            r.insert(0, name)
            r = r[:RECENT_MAX]
            _server_recent[:] = [name] + [n for n in _server_recent if n != name][:RECENT_MAX - 1]
            val = _quote(json.dumps(r, separators=(",", ":")), safe="")
            return [("Set-Cookie", f"{RECENT_COOKIE}={val}; Path=/; Max-Age=31536000; "
                                   f"Expires=Fri, 31 Dec 9999 23:59:59 GMT; SameSite=Lax")]

        def _safe(self, fn):
            try:
                fn()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                try:
                    self._send(500, _page("Error", "<h1>Internal error</h1>"
                                                   "<p><a href=\"/\">Home</a></p>"))
                except Exception:
                    pass

        # -- methods
        def do_GET(self):
            self._safe(self._get)

        def do_HEAD(self):
            self._safe(self._get)

        def do_POST(self):
            self._safe(self._post)

        def do_PATCH(self):
            self._safe(lambda: self._annotation("PATCH"))

        def do_DELETE(self):
            self._safe(lambda: self._annotation("DELETE"))

        def _send_json(self, status, obj, headers=None):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, val in (headers or []):
                self.send_header(k, val)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            return self.rfile.read(length) if length > 0 else b""

        def _annotation(self, method, raw=None):
            path, parts = self._path_parts()
            if raw is None:
                raw = self._read_body()
            if len(parts) < 4 or parts[0] != "catalog":
                self._send_json(404, {"error": "not found"})
                return
            status, result = annotation_request(
                method, parts[1], parts[2], "/".join(parts[3:]), raw)
            if status in (301, 302, 303, 307, 308):
                self._redirect(result, 303)
            else:
                self._send_json(status, {"error": result})

        def _path_parts(self):
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            return path, [_unquote(p) for p in path.split("/") if p != ""]

        def _query(self):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            return _parse_qs(q, keep_blank_values=True)

        def _get(self):
            path, parts = self._path_parts()
            if not parts:
                q = self._query()
                nf = q.get("notfound", [None])[0]
                err = q.get("error", [None])[0]
                self._send(200, landing_html(self._recent(), nf, err))
                return
            if parts[0] == "vault":
                self._static(parts)
                return
            if parts[0] != "catalog" or len(parts) < 2:
                self._send(404, _page("Not found", "<h1>404 Not Found</h1>"
                                                   "<p><a href=\"/\">Home</a></p>"))
                return
            name = parts[1]
            try:
                v = ViewerVault(name)
            except Exception as e:
                if isinstance(e, MVaultError) and "does not exist" in str(e):
                    self._redirect("/?notfound=" + _quote(name, safe=""))
                else:
                    self._redirect("/?notfound=" + _quote(name, safe="")
                                   + "&error=" + _quote(f"vault '{name}' could not be read", safe=""))
                return
            cookies = self._visit(name)
            qn = _quote(name, safe="")
            default = f"/catalog/{qn}/{v.default_category}"
            if len(parts) == 2:
                self._redirect(default, headers=cookies)
                return
            category = parts[2]
            if category not in v.categories:
                self._redirect(default, headers=cookies)
                return
            if len(parts) == 3:
                self._send(200, listing_html(v, category), cookies)
                return
            eid = "/".join(parts[3:])
            match = [e for e in v.entries(category) if str(e["id"]) == eid]
            if match:
                seek = None
                tc = self._query().get("timecode", [None])[0]
                if tc is not None:
                    try:
                        seek = parse_timecode(tc)
                    except ValueError:
                        seek = None
                self._send(200, detail_html(v, category, match[0], seek), cookies)
            else:
                self._send(404, listing_html(v, category, eid), cookies)

        def _not_found(self, status=404):
            msg = "403 Forbidden" if status == 403 else "404 Not Found"
            self._send(status, _page(msg, f"<h1>{msg}</h1><p><a href=\"/\">Home</a></p>"))

        def _static(self, parts):
            if len(parts) < 2:
                self._not_found()
                return
            name = parts[1]
            try:
                v = ViewerVault(name)
            except Exception:
                self._redirect("/?notfound=" + _quote(name, safe=""))
                return
            if len(parts) < 3 or parts[2] not in ("media", "preview"):
                self._not_found()
                return
            rest = parts[3:]
            if len(rest) != 1:
                self._not_found(403 if any(p in ("..", ".") for p in rest) or len(rest) > 1
                                else 404)
                return
            item = rest[0]
            if (item in (".", "..") or "/" in item or "\\" in item or "\0" in item
                    or ".." in item.replace("\\", "/").split("/")):
                self._not_found(403)
                return
            if parts[2] == "media":
                base = os.path.join(v.name, "media")
                fname = item
                kind = "media"
            else:
                base = os.path.join(v.name, "previews")
                fname = _match_file(base, item)
                kind = "image"
                if fname is None:
                    self._not_found()
                    return
            full = os.path.join(base, fname)
            try:
                real_base = os.path.realpath(base)
                real = os.path.realpath(full)
            except (OSError, ValueError):
                self._not_found()
                return
            if os.path.commonpath([real_base, real]) != real_base or real == real_base:
                self._not_found(403)
                return
            if not os.path.isfile(real):
                self._not_found()
                return
            self._send_file(real, _guess_mime(real, kind))

        def _send_file(self, path, ctype):
            try:
                size = os.path.getsize(path)
                f = open(path, "rb")
            except OSError:
                self._not_found()
                return
            with f:
                start, end, status = 0, size - 1, 200
                rng = self.headers.get("Range")
                m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng or "")
                if m and (m.group(1) or m.group(2)) and size > 0:
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    else:
                        start = max(0, size - int(m.group(2)))
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                length = max(0, end - start + 1)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _post(self):
            path, parts = self._path_parts()
            raw = self._read_body()
            if len(parts) >= 4 and parts[0] == "catalog":
                self._annotation("POST", raw)
                return
            if parts:
                self._send(404, _page("Not found", "<h1>404 Not Found</h1>"))
                return
            try:
                form = _parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            except Exception:
                form = {}
            val = form.get("catalog")
            if val and val[0].strip():
                self._redirect("/catalog/" + _quote(val[0].strip(), safe=""), 303)
            else:
                self._redirect("/", 303)

    return Handler


def _can_open_browser():
    if sys.platform.startswith(("win", "darwin")):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def cmd_serve(name, host, port):
    from http.server import ThreadingHTTPServer
    import threading
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler())
    except (OSError, OverflowError) as e:
        raise MVaultError(f"cannot bind to {host}:{port}: {e}")
    httpd.daemon_threads = True
    disp_host = host if host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in disp_host:
        disp_host = f"[{disp_host}]"
    base = f"http://{disp_host}:{httpd.server_address[1]}"
    path = "/"
    if name is not None:
        path = resolve_default(name) or f"/catalog/{_quote(name, safe='')}"
    url = base + path
    print(f"mvault viewer serving at {base}/ (open {url})")
    sys.stdout.flush()
    if _can_open_browser() and not os.environ.get("MVAULT_NO_BROWSER"):
        def _open():
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser.print_usage(sys.stderr)
        print("mvault: error: a subcommand is required (init, sync, migrate, digest, serve)", file=sys.stderr)
        return 2
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        if args.command == "init":
            cmd_init(args.name, args.url)
        elif args.command == "sync":
            cmd_sync(args.name, {
                "skip_metadata": args.skip_metadata,
                "skip_download": args.skip_download,
                "format": args.format,
                "limits": {c: getattr(args, c) for c in CATEGORIES},
            })
        elif args.command == "migrate":
            cmd_migrate(args.name)
        elif args.command == "digest":
            cmd_digest(args.name)
        elif args.command == "serve":
            return cmd_serve(args.name, args.host, args.port)
    except MVaultError as e:
        print(f"mvault: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
