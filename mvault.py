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
    sub = p.add_subparsers(dest="command", metavar="{init,sync,migrate,digest}")
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
        sys.stdout.flush()


def sync_metadata(name):
    counts = {"added": 0, "removed": 0, "updated": 0}
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
            else:
                changed = False
                for f in TRACKED:
                    if set_hist(entry, f, se[f], ts):
                        changed = True
                if set_hist(entry, "removed", False, ts):
                    changed = True
                if changed:
                    counts["updated"] += 1
        for e in cat[c]:
            if e["id"] not in src_ids:
                if set_hist(e, "removed", True, ts):
                    counts["removed"] += 1

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
        groups = [("Entries", cat["entries"])]
    else:
        if (not isinstance(cat.get("source"), str)
                or not all(isinstance(cat.get(c), list) for c in CATEGORIES)):
            raise MVaultError(f"invalid vault '{name}': malformed version {ver} catalog.json")
        source = cat["source"]
        groups = [(c.capitalize(), cat[c]) for c in CATEGORIES]

    order = [("removals", "Removals"), ("additions", "Additions"),
             ("updates", "Field updates")]
    if ver != NATIVE_VERSION:
        order = order[1:]
    lines = []
    total = 0
    for label, entries in groups:
        buckets = {k: [] for k, _ in order}
        for e in entries:
            r = classify_entry(e, ver)
            if r is None or r[0] not in buckets:
                continue
            text = _entry_title(e, ver)
            if r[1]:
                text += " (" + ", ".join(r[1]) + ")"
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


def main(argv=None):
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser.print_usage(sys.stderr)
        print("mvault: error: a subcommand is required (init, sync, migrate, digest)", file=sys.stderr)
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
    except MVaultError as e:
        print(f"mvault: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
