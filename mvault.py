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
    """Apply source metadata to catalog; return dict of added/removed/updated counts."""
    counts = {"added": 0, "removed": 0, "updated": 0, "changes": []}
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
                counts["added"] += 1
                counts["changes"].append(("added", cat, entry, []))
                continue
            changed = []
            for field in SOURCE_TRACKED:
                if append_history(entry, field, item[field], ts):
                    changed.append(field)
            removed, has = current_value(entry.get("removed"))
            if not has or removed is not False:
                append_history(entry, "removed", False, ts)
                if has and removed is True:
                    changed.append("reappeared")
            if changed:
                counts["updated"] += 1
                counts["changes"].append(("updated", cat, entry, changed))
        for e in entries:
            if e["id"] in src:
                continue
            removed, has = current_value(e.get("removed"))
            if not has or removed is not True:
                append_history(e, "removed", True, ts)
                counts["removed"] += 1
                counts["changes"].append(("removed", cat, e, []))
        sort_entries(entries)
    return counts


# ---------------------------------------------------------------- downloads

DOWNLOAD_RETRIES = 3
RETRY_DELAYS = (0.2, 0.5)
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".download", ".crdownload")
TRANSIENT_STATUSES = (408, 425, 429, 500, 502, 503, 504)

CONTENT_TYPE_EXT = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "video/matroska": "mkv",
    "video/quicktime": "mov",
    "video/x-msvideo": "avi",
    "video/x-flv": "flv",
    "video/mpeg": "mpeg",
    "video/ogg": "ogv",
    "video/mp2t": "ts",
    "video/3gpp": "3gp",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/webm": "weba",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "application/octet-stream": "bin",
    "application/json": "json",
    "text/plain": "txt",
    "text/html": "html",
}


class TransientDownloadError(Exception):
    pass


class PermanentDownloadError(Exception):
    pass


def ext_from_content_type(ctype):
    import mimetypes
    import re
    if not ctype:
        return "bin"
    mime = ctype.split(";", 1)[0].strip().lower()
    if not mime:
        return "bin"
    if mime in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[mime]
    guess = mimetypes.guess_extension(mime, strict=False)
    if guess:
        return guess.lstrip(".")
    sub = mime.split("/", 1)[-1]
    if "+" in sub:
        sub = sub.rsplit("+", 1)[-1]
    if sub.startswith("x-"):
        sub = sub[2:]
    sub = re.sub(r"[^a-z0-9]+", "", sub)
    return sub or "bin"


def safe_filename_id(entry_id):
    import urllib.parse
    if entry_id in ("", ".", "..") or "/" in entry_id or "\\" in entry_id or "\0" in entry_id \
            or entry_id.startswith("."):
        q = urllib.parse.quote(entry_id, safe="")
        if q.startswith("."):
            q = "%2E" + q[1:]
        return q
    return entry_id


def url_join(source, kind, tail):
    import urllib.parse
    return "%s/%s/%s" % (source.rstrip("/"), kind, urllib.parse.quote(tail, safe=".-_~"))


def is_partial_name(fname):
    return fname.startswith(".") or fname.lower().endswith(PARTIAL_SUFFIXES)


def present_ids(directory):
    """Return set of filename stems (and full names) of completed files in directory."""
    found = set()
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for fname in names:
        if is_partial_name(fname):
            continue
        if not os.path.isfile(os.path.join(directory, fname)):
            continue
        found.add(fname)
        if "." in fname:
            found.add(fname.rsplit(".", 1)[0])
    return found


def http_fetch_to(url, directory, fname_id, ext_override):
    """Download url into directory/<fname_id>.<ext>. Raises Transient/Permanent errors."""
    import socket
    import urllib.error
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        if exc.code in TRANSIENT_STATUSES or exc.code >= 500:
            raise TransientDownloadError("HTTP %s" % exc.code)
        raise PermanentDownloadError("HTTP %s" % exc.code)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransientDownloadError(str(reason))
    except ValueError as exc:
        raise PermanentDownloadError(str(exc))
    tmp = None
    try:
        with resp:
            status = getattr(resp, "status", 200) or 200
            if not (200 <= status < 300):
                if status in TRANSIENT_STATUSES or status >= 500:
                    raise TransientDownloadError("HTTP %s" % status)
                raise PermanentDownloadError("HTTP %s" % status)
            ext = ext_override if ext_override is not None else \
                ext_from_content_type(resp.headers.get("Content-Type"))
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="." + fname_id[:40] + ".", suffix=".part",
                                       dir=directory)
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            final = os.path.join(directory, "%s.%s" % (fname_id, ext) if ext else fname_id)
            os.replace(tmp, final)
            tmp = None
            return final
    except (TransientDownloadError, PermanentDownloadError):
        raise
    except (socket.timeout, TimeoutError, ConnectionError, urllib.error.URLError) as exc:
        raise TransientDownloadError(str(getattr(exc, "reason", exc)))
    except OSError as exc:
        if exc.errno is None:
            raise TransientDownloadError(str(exc))
        raise PermanentDownloadError(str(exc))
    except Exception as exc:  # e.g. http.client.IncompleteRead
        raise TransientDownloadError(str(exc) or exc.__class__.__name__)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def download_with_retry(url, directory, fname_id, ext_override):
    import time
    last = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            return http_fetch_to(url, directory, fname_id, ext_override), None
        except PermanentDownloadError as exc:
            return None, "permanently unavailable (%s)" % exc
        except TransientDownloadError as exc:
            last = exc
            if attempt + 1 < DOWNLOAD_RETRIES:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    return None, "failed after %d attempts (%s)" % (DOWNLOAD_RETRIES, last)


def run_downloads(name, catalog, limits, fmt):
    source = catalog["source"]
    media_dir = os.path.join(name, "media")
    preview_dir = os.path.join(name, "previews")
    have_media = present_ids(media_dir)
    have_preview = present_ids(preview_dir)
    stats = {"media": 0, "previews": 0, "failed": 0}
    for cat in CATEGORIES:
        limit = limits.get(cat)
        candidates = []
        seen = set()
        for e in catalog[cat]:
            eid = e.get("id")
            if not is_str(eid) or eid in seen:
                continue
            seen.add(eid)
            fid = safe_filename_id(eid)
            if fid in have_media:
                continue
            candidates.append((eid, fid))
        if limit is not None:
            candidates = candidates[:limit]
        for eid, fid in candidates:
            if fmt is not None:
                url = url_join(source, "media", "%s.%s" % (eid, fmt))
            else:
                url = url_join(source, "media", eid)
            path, err = download_with_retry(url, media_dir, fid, fmt)
            if err:
                stats["failed"] += 1
                sys.stdout.flush()
                eprint("warning: media download for %s entry '%s' %s" % (cat[:-1], eid, err))
            else:
                stats["media"] += 1
                have_media.add(fid)
                print("Downloaded media for '%s' -> %s" % (eid, os.path.basename(path)))
            if fid in have_preview:
                continue
            ppath, perr = download_with_retry(url_join(source, "preview", eid),
                                              preview_dir, fid, None)
            if perr:
                stats["failed"] += 1
                sys.stdout.flush()
                eprint("warning: preview download for %s entry '%s' %s" % (cat[:-1], eid, perr))
            else:
                stats["previews"] += 1
                have_preview.add(fid)
    return stats


# ---------------------------------------------------------------- viewer links

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8840


def _q(text):
    import urllib.parse
    return urllib.parse.quote(str(text), safe="")


def catalog_route(name, category=None, entry_id=None):
    parts = ["", "catalog", _q(name)]
    if category is not None:
        parts.append(_q(category))
        if entry_id is not None:
            parts.append(_q(entry_id))
    return "/".join(parts)


def viewer_link(name, category, entry_id, host=DEFAULT_HOST, port=DEFAULT_PORT):
    return "http://%s:%d%s" % (host, port, catalog_route(name, category, entry_id))


# ---------------------------------------------------------------- digest

def _v1_key(k):
    try:
        return (0, float(k), k)
    except (ValueError, TypeError):
        return (1, 0.0, k)


def _sorted_history(h, version):
    if not isinstance(h, dict):
        return []
    if version == 1:
        keys = sorted(h.keys(), key=_v1_key)
    else:
        keys = sorted(h.keys())
    return [h[k] for k in keys]


def _same(a, b):
    return a == b and type(a) is type(b)


def classify_entry(e, version):
    """Return (group, changed_fields) where group in removed/added/updated/None."""
    fields = TRACKED if version == 3 else SOURCE_TRACKED
    hist = {f: _sorted_history(e.get(f), version) for f in fields}
    if version == 3:
        r = hist.get("removed", [])
        if r and r[-1] is True and (len(r) < 2 or r[-2] is False):
            return "removed", []
    if all(len(v) < 2 for v in hist.values()):
        return "added", []
    changed = []
    for f in fields:
        v = hist[f]
        if len(v) >= 2 and not _same(v[-1], v[-2]):
            if f == "removed":
                if v[-1] is False:
                    changed.append("reappeared")
                else:
                    changed.append("removed")
            else:
                changed.append(f)
    if changed:
        return "updated", changed
    return None, []


def entry_title(e, version):
    vals = _sorted_history(e.get("title"), version)
    if vals and vals[-1] is not None:
        return str(vals[-1])
    return str(e.get("id"))


GROUP_LABELS = (("removed", "Removed"), ("added", "Added"), ("updated", "Updated"))


def build_digest(name, catalog, version, now=None):
    if version == 1:
        groups = [("Entries", catalog["entries"])]
        source = v1_source_url(catalog["source_id"])
    else:
        groups = [(c.capitalize(), catalog[c]) for c in CATEGORIES]
        source = catalog["source"]
    lines = []
    total = 0
    for label, entries in groups:
        link_cat = label.lower()
        buckets = {"removed": [], "added": [], "updated": []}
        for e in entries:
            group, changed = classify_entry(e, version)
            if group is None:
                continue
            text = entry_title(e, version)
            if group == "updated":
                text += " (%s)" % ", ".join(changed)
            text += " " + viewer_link(name, link_cat, e.get("id"))
            buckets[group].append(text)
        if not any(buckets.values()):
            continue
        if lines:
            lines.append("")
        lines.append("%s:" % label)
        for key, glabel in GROUP_LABELS:
            items = buckets[key]
            if not items:
                continue
            lines.append("  %s (%d):" % (glabel, len(items)))
            for t in items:
                lines.append("    - %s" % t)
                total += 1
    if not lines:
        lines.append("No notable changes found.")
    if now is None:
        now = fmt_dt(datetime.now().replace(microsecond=0))
    lines.append("")
    lines.append("Digest of vault '%s' (catalog version %d, %d change%s) generated at %s "
                 "from %s" % (name, version, total, "" if total == 1 else "s", now, source))
    return "\n".join(lines)


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
    catalog, version = load_vault(name)
    counts = None
    if not args.skip_metadata:
        source = fetch_source(catalog["source"])
        ts = compute_sync_timestamp(catalog)
        counts = sync_catalog(catalog, source, ts)
        write_catalog(name, catalog, backup=True)
        print("Synced vault '%s' at %s" % (name, ts))
    if not args.skip_download:
        sys.stdout.flush()
        limits = {"episodes": args.episodes, "streams": args.streams, "clips": args.clips}
        run_downloads(name, catalog, limits, args.format)
    if counts is not None:
        print("Summary: %d added, %d removed, %d updated"
              % (counts["added"], counts["removed"], counts["updated"]))
        order = {"removed": 0, "added": 1, "updated": 2}
        changes = sorted(counts["changes"],
                         key=lambda c: (CATEGORIES.index(c[1]), order[c[0]]))
        for group, cat, entry, fields in changes:
            text = entry_title(entry, CURRENT_VERSION)
            if fields:
                text += " (%s)" % ", ".join(fields)
            print("  %s %s: %s %s" % (group.capitalize(), cat[:-1], text,
                                       viewer_link(name, cat, entry.get("id"))))
    return 0


def cmd_digest(args):
    name = args.name
    raw, version = read_raw_catalog(name)
    validate_catalog(name, raw, version)
    print(build_digest(name, raw, version))
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


def non_negative_int(text):
    t = text.strip()
    if not t or not t.isdigit() or not t.isascii():
        raise argparse.ArgumentTypeError("invalid non-negative integer: %r" % text)
    return int(t)


def format_str(text):
    t = text.strip()
    if not t or "/" in t or "\\" in t or t.startswith(".") or "\0" in t:
        raise argparse.ArgumentTypeError("invalid format: %r" % text)
    return t


# ---------------------------------------------------------------- annotations

import re as _re
import threading as _threading
import uuid as _uuid

_TIMECODE_RE = _re.compile(r"[0-9]+(?::[0-9]+){0,2}")
ANNOTATION_LOCK = _threading.Lock()


class AnnotationError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def parse_timecode(text):
    """Parse SS, MM:SS or HH:MM:SS (non-negative integer components) to seconds."""
    if isinstance(text, bool):
        raise ValueError("timecode must be a string")
    if isinstance(text, int):
        if text < 0:
            raise ValueError("timecode must be non-negative")
        return text
    if not isinstance(text, str):
        raise ValueError("timecode must be a string")
    t = text.strip()
    if not _TIMECODE_RE.fullmatch(t):
        raise ValueError("invalid timecode format %s (expected SS, MM:SS or HH:MM:SS)"
                         % json.dumps(text))
    secs = 0
    for part in t.split(":"):
        secs = secs * 60 + int(part)
    return secs


def format_timecode(secs):
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return str(secs)
    if secs < 0:
        return str(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def _new_annotation_id(existing):
    used = {a.get("id") for a in existing if isinstance(a, dict)}
    while True:
        aid = _uuid.uuid4().hex[:12]
        if aid not in used:
            return aid


def _parse_json_body(data):
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AnnotationError(400, "malformed JSON request body")
    if not isinstance(obj, dict):
        raise AnnotationError(400, "malformed JSON request body (expected an object)")
    return obj


def _require_str(obj, field):
    if field not in obj or obj[field] is None:
        raise AnnotationError(400, "missing required field '%s'" % field)
    if not isinstance(obj[field], str):
        raise AnnotationError(400, "field '%s' must be a string" % field)
    return obj[field]


def _optional_body(obj):
    v = obj.get("body")
    if v is not None and not isinstance(v, str):
        raise AnnotationError(400, "field 'body' must be a string or null")
    return v


def mutate_annotation(name, category, entry_id, method, data):
    """Apply an annotation mutation; return (redirect_category, timecode_or_None).

    Raises AnnotationError on failure. The on-disk catalog is only replaced on success,
    and catalog.bak always holds the catalog as it was before this request.
    """
    payload = _parse_json_body(data)
    if method == "POST":
        title = _require_str(payload, "title")
        if "timecode" not in payload or payload["timecode"] is None:
            raise AnnotationError(400, "missing required field 'timecode'")
        try:
            timecode = parse_timecode(payload["timecode"])
        except ValueError as exc:
            raise AnnotationError(400, str(exc))
        body = _optional_body(payload)
    else:
        aid = _require_str(payload, "id")
        if method == "PATCH":
            if "title" not in payload and "body" not in payload:
                raise AnnotationError(
                    400, "missing field: at least one of 'title' or 'body' is required")
            if "title" in payload:
                _require_str(payload, "title")
            if "body" in payload:
                _optional_body(payload)
    if not name or "\0" in name or not os.path.isdir(name):
        raise AnnotationError(404, "vault '%s' does not exist" % name)
    with ANNOTATION_LOCK:
        try:
            raw, version = read_raw_catalog(name)
            validate_catalog(name, raw, version)
        except MVaultError as exc:
            raise AnnotationError(500, str(exc))
        if category not in viewer_valid_categories(version):
            raise AnnotationError(404, "category '%s' not found in vault '%s'"
                                  % (category, name))
        idx = None
        for i, e in enumerate(raw.get(category) or []):
            if isinstance(e, dict) and e.get("id") == entry_id:
                idx = i
                break
        if idx is None:
            raise AnnotationError(404, "entry '%s' not found in %s" % (entry_id, category))
        target_cat = "episodes" if version == 1 else category
        try:
            catalog = migrate_to_v3(copy.deepcopy(raw), version)
        except Exception as exc:
            raise AnnotationError(500, "automatic migration to version %d failed: %s"
                                  % (CURRENT_VERSION, exc))
        entry = catalog[target_cat][idx]
        anns = entry.get("annotations")
        if not isinstance(anns, list):
            anns = []
            entry["annotations"] = anns
        if method == "POST":
            anns.append({"id": _new_annotation_id(anns), "timecode": timecode,
                         "title": title, "body": body})
        else:
            pos = None
            for i, a in enumerate(anns):
                if isinstance(a, dict) and a.get("id") == aid:
                    pos = i
                    break
            if pos is None:
                raise AnnotationError(404, "annotation '%s' not found" % aid)
            if method == "PATCH":
                if "title" in payload:
                    anns[pos]["title"] = payload["title"]
                if "body" in payload:
                    anns[pos]["body"] = payload["body"]
            else:
                del anns[pos]
        try:
            write_catalog(name, catalog, backup=True)
        except MVaultError as exc:
            raise AnnotationError(500, str(exc))
    return target_cat, (timecode if method == "POST" else None)


# ---------------------------------------------------------------- viewer server

RECENT_COOKIE = "mvault_recent"
RECENT_MAX = 20
RECENT_MAX_AGE = 10 * 365 * 24 * 3600

VIEWER_CSS = """
body { font-family: sans-serif; margin: 2em; color: #222; }
a { color: #0645ad; }
table { border-collapse: collapse; margin-top: 1em; }
th, td { border: 1px solid #ccc; padding: 0.3em 0.6em; text-align: left; }
tr.downloaded td.state { background: #d4f7d4; color: #145214; font-weight: bold; }
tr.not-downloaded td.state { background: #f7e1d4; color: #7a3410; }
tr.removed td { text-decoration: line-through; color: #888; }
tr.removed td.flag { text-decoration: none; color: #b00; font-weight: bold; }
tr.highlight td { outline: 2px solid #e0a000; background-color: #fff6d0; }
nav a { margin-right: 1em; }
nav a.current { font-weight: bold; text-decoration: none; color: #222; }
.error { color: #b00; font-weight: bold; }
"""


def html_escape(text):
    # Attributes are always double-quoted, so apostrophes may stay literal.
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def viewer_valid_categories(version):
    return ("entries",) if version == 1 else CATEGORIES


def viewer_default_category(version):
    return "entries" if version == 1 else "episodes"


def viewer_load(name):
    """Return (raw_catalog, version) or raise MVaultError. Never modifies disk."""
    if not name or "\0" in name:
        raise MVaultError("vault '%s' does not exist" % name)
    raw, version = read_raw_catalog(name)
    validate_catalog(name, raw, version)
    return raw, version


def viewer_entry_title(e, version):
    vals = _sorted_history(e.get("title"), version)
    if vals and vals[-1] is not None:
        return str(vals[-1])
    return str(e.get("id"))


def viewer_is_removed(e, version):
    if version != 3:
        return False
    vals = _sorted_history(e.get("removed"), version)
    return bool(vals) and vals[-1] is True


def media_names(name):
    try:
        names = os.listdir(os.path.join(name, "media"))
    except OSError:
        return []
    out = []
    for fname in names:
        if fname.startswith("."):
            continue  # in-progress temporary download files
        if os.path.isfile(os.path.join(name, "media", fname)):
            out.append(fname)
    return out


def html_page(title, body):
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n"
            % (html_escape(title), VIEWER_CSS, body))


def parse_recent_cookie(header):
    import http.cookies
    import urllib.parse
    if not header:
        return []
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(header)
    except http.cookies.CookieError:
        return []
    morsel = jar.get(RECENT_COOKIE)
    if morsel is None:
        return []
    try:
        data = json.loads(urllib.parse.unquote(morsel.value))
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for n in data:
        if isinstance(n, str) and n and n not in out:
            out.append(n)
    return out[:RECENT_MAX]


def make_recent_cookie(names):
    value = _q(json.dumps(names[:RECENT_MAX], ensure_ascii=False, separators=(",", ":")))
    return "%s=%s; Path=/; Max-Age=%d; SameSite=Lax" % (RECENT_COOKIE, value, RECENT_MAX_AGE)


MEDIA_MIME_FALLBACK = {
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska",
    "mov": "video/quicktime", "avi": "video/x-msvideo", "flv": "video/x-flv",
    "mpeg": "video/mpeg", "mpg": "video/mpeg", "ogv": "video/ogg", "ts": "video/mp2t",
    "3gp": "video/3gpp", "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "ogg": "audio/ogg", "opus": "audio/opus", "weba": "audio/webm", "wav": "audio/wav",
    "flac": "audio/flac", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp",
    "svg": "image/svg+xml",
}


def sniff_mime(path):
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in head else "video/x-matroska"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"FLV"):
        return "video/x-flv"
    return None


def file_mime(path, kind):
    """Return a MIME type for a served vault file; kind is 'media' or 'preview'."""
    import mimetypes
    fname = os.path.basename(path)
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    good_prefixes = ("video/", "audio/", "image/") if kind == "media" else ("image/",)
    mime = MEDIA_MIME_FALLBACK.get(ext)
    if mime is None and ext:
        guess = mimetypes.guess_type(fname, strict=False)[0]
        if guess and guess.startswith(good_prefixes):
            mime = guess
    if mime is None or not mime.startswith(good_prefixes):
        sniffed = sniff_mime(path)
        if sniffed and sniffed.startswith(good_prefixes):
            mime = sniffed
    if mime is None or not mime.startswith(good_prefixes):
        mime = "video/mp4" if kind == "media" else "image/jpeg"
    return mime


def vault_files(name, sub):
    """Completed (non-partial) regular files in <vault>/<sub>/, sorted."""
    d = os.path.join(name, sub)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for fname in names:
        if is_partial_name(fname):
            continue
        if os.path.isfile(os.path.join(d, fname)):
            out.append(fname)
    return sorted(out)


def match_file_for_id(files, entry_id):
    """Pick the saved filename containing entry_id (prefer exact stem matches)."""
    if not entry_id:
        return None
    candidates = [f for f in files if entry_id in f]
    if not candidates:
        fid = safe_filename_id(entry_id)
        candidates = [f for f in files if fid and fid in f]
    if not candidates:
        return None
    fid = safe_filename_id(entry_id)

    def rank(f):
        stem = f.rsplit(".", 1)[0] if "." in f else f
        if stem in (entry_id, fid) or f in (entry_id, fid):
            return (0, len(f), f)
        if f.startswith((entry_id, fid)):
            return (1, len(f), f)
        return (2, len(f), f)
    return min(candidates, key=rank)


def entry_source_link(raw, version, entry_id):
    import urllib.parse
    q = urllib.parse.quote(str(entry_id), safe="-_.~!$&'()*+,;=:@")
    if version == 1:
        base = v1_source_url(raw.get("source_id", ""))
    else:
        base = str(raw.get("source", ""))
    return "%s/entry/%s" % (base.rstrip("/"), q)


def chart_points(history, version):
    """Return [{"timestamp": iso, "value": v}, ...] oldest first."""
    if not isinstance(history, dict):
        return []
    if version == 1:
        keys = sorted(history.keys(), key=_v1_key)
        out = []
        for k in keys:
            try:
                ts = epoch_key_to_iso(k)
            except (ValueError, TypeError, OverflowError):
                ts = str(k)
            out.append({"timestamp": ts, "value": history[k]})
        return out
    return [{"timestamp": k, "value": history[k]} for k in sorted(history.keys())]


def json_for_html(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def svg_chart(field, points):
    points = [(p["timestamp"], p["value"]) for p in points]
    vals = [(i, v) for i, (_, v) in enumerate(points) if is_int(v) or isinstance(v, float)]
    w, h, pad = 600, 200, 30
    if not vals:
        return "<p class=\"no-chart\">No numeric %s data.</p>" % html_escape(field)
    lo = min(v for _, v in vals)
    hi = max(v for _, v in vals)
    span = (hi - lo) or 1
    n = max(len(points) - 1, 1)

    def xy(i, v):
        x = pad + (w - 2 * pad) * (i / n if len(points) > 1 else 0.5)
        y = h - pad - (h - 2 * pad) * ((v - lo) / span if hi != lo else 0.5)
        return "%.1f,%.1f" % (x, y)
    # break the line at null values
    segments, cur = [], []
    for i, (_, v) in enumerate(points):
        if is_int(v) or isinstance(v, float):
            cur.append(xy(i, v))
        elif cur:
            segments.append(cur)
            cur = []
    if cur:
        segments.append(cur)
    lines = "".join("<polyline fill=\"none\" stroke=\"#0645ad\" stroke-width=\"2\" "
                    "points=\"%s\"/>" % " ".join(seg) for seg in segments)
    dots = "".join("<circle r=\"3\" fill=\"#0645ad\" cx=\"%s\" cy=\"%s\"/>"
                   % tuple(xy(i, v).split(",")) for i, v in vals)
    labels = ("<text x=\"2\" y=\"%d\" font-size=\"10\">%s</text>"
              "<text x=\"2\" y=\"%d\" font-size=\"10\">%s</text>"
              % (pad - 5, html_escape(hi), h - pad + 15, html_escape(lo)))
    return ("<svg class=\"chart\" role=\"img\" aria-label=\"%s chart\" width=\"%d\" "
            "height=\"%d\" viewBox=\"0 0 %d %d\">%s%s%s</svg>"
            % (html_escape(field), w, h, w, h, lines, dots, labels))


def make_handler():
    import http.server
    import urllib.parse

    class ViewerHandler(http.server.BaseHTTPRequestHandler):
        server_version = "mvault"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # ---- helpers
        def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None,
                  head=False):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or []):
                self.send_header(k, v)
            self.end_headers()
            if not head and body:
                self.wfile.write(body)

        def _redirect(self, location, headers=None, head=False):
            body = html_page("Redirect", "<p>Redirecting to <a href=\"%s\">%s</a></p>"
                             % (html_escape(location), html_escape(location)))
            hdrs = [("Location", location)] + list(headers or [])
            status = 303 if self.command in ("POST", "PATCH", "DELETE") else 302
            self._send(status, body, headers=hdrs, head=head)

        def _error(self, status, message, head=False):
            body = html_page("Error %d" % status, "<h1>Error %d</h1><p>%s</p>"
                             "<p><a href=\"/\">Back to start</a></p>"
                             % (status, html_escape(message)))
            self._send(status, body, head=head)

        def _recent(self):
            return parse_recent_cookie(self.headers.get("Cookie"))

        def _visit_cookie(self, name):
            names = [name] + [n for n in self._recent() if n != name]
            return ("Set-Cookie", make_recent_cookie(names))

        def _safe(self, fn, *args):
            try:
                fn(*args)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # never expose tracebacks
                eprint("viewer error: %s: %s" % (exc.__class__.__name__, exc))
                try:
                    self._error(500, "Internal server error.")
                except Exception:
                    pass

        # ---- verbs
        def do_GET(self):
            self._safe(self._get, False)

        def do_HEAD(self):
            self._safe(self._get, True)

        def do_POST(self):
            self._safe(self._post)

        def do_PATCH(self):
            self._safe(self._mutate)

        def do_DELETE(self):
            self._safe(self._mutate)

        def _method_not_allowed(self):
            body = html_page("Error 405", "<h1>Error 405</h1><p>Method not allowed.</p>")
            self._send(405, body, headers=[("Allow", "GET, HEAD, POST, PATCH, DELETE")])

        do_PUT = _method_not_allowed

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            return self.rfile.read(length) if length > 0 else b""

        def _json_error(self, status, message):
            body = json.dumps({"error": message, "status": status}) + "\n"
            self._send(status, body, ctype="application/json; charset=utf-8")

        def _mutate(self, data=None):
            segs, _ = self._split()
            if data is None:
                data = self._read_body()
            if len(segs) != 4 or segs[0] != "catalog":
                if self.command == "POST":
                    self._error(404, "Not found.")
                else:
                    self._method_not_allowed()
                return
            name, category, entry_id = segs[1], segs[2], segs[3]
            try:
                cat, timecode = mutate_annotation(name, category, entry_id,
                                                  self.command, data)
            except AnnotationError as exc:
                self._json_error(exc.status, exc.message)
                return
            loc = catalog_route(name, cat, entry_id)
            if timecode is not None:
                loc += "?timecode=%d" % timecode
            self._redirect(loc)

        # ---- routing
        def _split(self):
            parsed = urllib.parse.urlsplit(self.path)
            raw = [p for p in parsed.path.split("/") if p != ""]
            segs = [urllib.parse.unquote(p) for p in raw]
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            return segs, query

        def _post(self):
            segs, _ = self._split()
            data = self._read_body()
            if segs:
                self._mutate(data)
                return
            form = urllib.parse.parse_qs(data.decode("utf-8", "replace"),
                                         keep_blank_values=True)
            value = (form.get("catalog") or [""])[0].strip()
            if not value:
                self._redirect("/")
                return
            self._redirect(catalog_route(value))

        def _get(self, head):
            segs, query = self._split()
            if not segs:
                self._landing(query, head)
                return
            if segs[0] == "vault":
                self._static(segs, head)
                return
            if segs[0] != "catalog":
                self._error(404, "Not found.", head)
                return
            if len(segs) == 1:
                self._redirect("/", head=head)
                return
            name = segs[1]
            try:
                raw, version = viewer_load(name)
            except MVaultError as exc:
                msg = str(exc)
                if "does not exist" in msg:
                    loc = "/?notfound=" + _q(name)
                else:
                    loc = "/?invalid=" + _q(name)
                self._redirect(loc, head=head)
                return
            cookie = self._visit_cookie(name)
            default = viewer_default_category(version)
            if len(segs) == 2:
                self._redirect(catalog_route(name, default), [cookie], head)
                return
            category = segs[2]
            if category not in viewer_valid_categories(version) or len(segs) > 4:
                self._redirect(catalog_route(name, default), [cookie], head)
                return
            if len(segs) == 4:
                status, body = self._detail(name, raw, version, category, segs[3], query)
            else:
                status, body = self._listing(name, raw, version, category, None)
            self._send(status, body, headers=[cookie], head=head)

        # ---- static vault files
        def _static(self, segs, head):
            # Reject traversal attempts before touching the filesystem.
            raw_path = urllib.parse.urlsplit(self.path).path
            lowered = raw_path.lower()
            if len(segs) >= 3 and (
                    ".." in segs[3:] or "%2e%2e" in lowered or "/../" in raw_path + "/"
                    or any(("/" in p or "\\" in p or "\0" in p or p in (".", ".."))
                           for p in segs[3:])):
                self._error(403, "Forbidden.", head)
                return
            if len(segs) != 4 or segs[2] not in ("media", "preview"):
                self._error(404, "Not found.", head)
                return
            name, kind, item = segs[1], segs[2], segs[3]
            if not name or "\0" in name:
                self._redirect("/?notfound=" + _q(name), head=head)
                return
            try:
                viewer_load(name)
            except MVaultError as exc:
                if "does not exist" in str(exc):
                    self._redirect("/?notfound=" + _q(name), head=head)
                else:
                    self._redirect("/?invalid=" + _q(name), head=head)
                return
            if kind == "media":
                sub = "media"
                files = vault_files(name, sub)
                fname = item if item in files else None
            else:
                sub = "previews"
                fname = match_file_for_id(vault_files(name, sub), item)
            if fname is None:
                self._error(404, "File not found.", head)
                return
            base = os.path.realpath(os.path.join(name, sub))
            path = os.path.realpath(os.path.join(base, fname))
            if os.path.dirname(path) != base:
                self._error(403, "Forbidden.", head)
                return
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self._error(404, "File not found.", head)
                return
            ctype = file_mime(path, kind)
            self._send(200, data, ctype=ctype, head=head)

        # ---- entry detail
        def _detail(self, name, raw, version, category, entry_id, query=None):
            entries = raw.get(category) or []
            entry = None
            for e in entries:
                if isinstance(e, dict) and e.get("id") == entry_id:
                    entry = e
                    break
            back = catalog_route(name, category)
            if entry is None:
                body = ("<p><a class=\"back\" href=\"%s\">&larr; Back to %s / %s</a></p>"
                        "<h1>Error 404</h1><p class=\"error entry-not-found\">"
                        "Entry '%s' not found in %s.</p>"
                        % (html_escape(back), html_escape(name), html_escape(category),
                           html_escape(entry_id), html_escape(category)))
                return 404, html_page("Entry not found", body)
            title_vals = _sorted_history(entry.get("title"), version)
            title = title_vals[-1] if title_vals else None
            desc_vals = _sorted_history(entry.get("description"), version)
            desc = desc_vals[-1] if desc_vals else None
            published = entry.get("published")
            width, height = entry.get("width"), entry.get("height")
            link = entry_source_link(raw, version, entry_id)
            removed = viewer_is_removed(entry, version)
            media = match_file_for_id(vault_files(name, "media"), entry_id)
            preview = match_file_for_id(vault_files(name, "previews"), entry_id)
            charts = {f: chart_points(entry.get(f), version) for f in ("views", "likes")}

            def show(v):
                return "&mdash;" if v is None else html_escape(v)
            parts = [
                "<p><a class=\"back\" href=\"%s\">&larr; Back to %s / %s</a></p>"
                % (html_escape(back), html_escape(name), html_escape(category)),
                "<h1 class=\"entry-title\">%s</h1>"
                % (html_escape(title) if title is not None else html_escape(entry_id)),
            ]
            if removed:
                parts.append("<p class=\"removed-flag error\">REMOVED from source</p>")
            parts.append(
                "<dl class=\"metadata\">\n"
                "<dt>ID</dt><dd class=\"id\">%s</dd>\n"
                "<dt>Title</dt><dd class=\"title\">%s</dd>\n"
                "<dt>Description</dt><dd class=\"description\">%s</dd>\n"
                "<dt>Published</dt><dd class=\"published\">%s</dd>\n"
                "<dt>Dimensions</dt><dd class=\"dimensions\">"
                "<span class=\"width\">%s</span> &times; <span class=\"height\">%s</span>"
                " (width %s, height %s)</dd>\n"
                "<dt>Source</dt><dd class=\"source\"><a class=\"source-link\" href=\"%s\">"
                "%s</a></dd>\n</dl>"
                % (html_escape(entry_id), show(title), show(desc), show(published),
                   show(width), show(height), show(width), show(height),
                   html_escape(link), html_escape(link)))
            seek = None
            for tv in (query or {}).get("timecode", []):
                try:
                    seek = parse_timecode(tv)
                except ValueError:
                    seek = None
            if seek is not None:
                parts.append("<p class=\"seek\" data-timecode=\"%d\">Playback position: "
                             "<span class=\"timecode\">%s</span> (%d s)</p>"
                             % (seek, format_timecode(seek), seek))
            if media is not None:
                murl = "/vault/%s/media/%s" % (_q(name), _q(media))
                if seek is not None:
                    murl += "#t=%d" % seek
                mtype = file_mime(os.path.join(name, "media", media), "media")
                tag = "audio" if mtype.startswith("audio/") else "video"
                if mtype.startswith("image/"):
                    player = "<img src=\"%s\" alt=\"media\">" % html_escape(murl)
                else:
                    player = ("<%s controls preload=\"metadata\" src=\"%s\">"
                              "<source src=\"%s\" type=\"%s\"></%s>"
                              % (tag, html_escape(murl), html_escape(murl),
                                 html_escape(mtype), tag))
                parts.append("<section class=\"media downloaded\"><h2>Media</h2>%s"
                             "<p><a class=\"media-link\" href=\"%s\">%s</a></p></section>"
                             % (player, html_escape(murl), html_escape(media)))
                if seek is not None and not mtype.startswith("image/"):
                    parts.append(
                        "<script>(function(){var m=document.querySelector("
                        "'section.media %s');if(!m)return;var t=%d;function go(){"
                        "try{m.currentTime=t;}catch(e){}}if(m.readyState>=1)go();"
                        "else m.addEventListener('loadedmetadata',go);})();</script>"
                        % (tag, seek))
            else:
                parts.append("<section class=\"media not-downloaded\"><h2>Media</h2>"
                             "<p class=\"no-media\">Media not downloaded.</p></section>")
            if preview is not None:
                purl = "/vault/%s/preview/%s" % (_q(name), _q(entry_id))
                parts.append("<section class=\"preview\"><h2>Preview</h2>"
                             "<img src=\"%s\" alt=\"preview\" style=\"max-width:480px\">"
                             "</section>" % html_escape(purl))
            anns = [a for a in (entry.get("annotations") or []) if isinstance(a, dict)] \
                if version == 3 and isinstance(entry.get("annotations"), list) else []
            ann_items = []
            for a in anns:
                aid = str(a.get("id"))
                tc = a.get("timecode")
                tc_href = catalog_route(name, category, entry_id)
                if is_int(tc):
                    tc_href += "?timecode=%d" % tc
                abody = a.get("body")
                ann_items.append(
                    "<li class=\"annotation\" id=\"annotation-%s\" data-id=\"%s\" "
                    "data-timecode=\"%s\"><a class=\"timecode\" href=\"%s\">%s</a> "
                    "<span class=\"annotation-title\">%s</span> "
                    "<span class=\"annotation-id\">[%s]</span>%s</li>"
                    % (html_escape(aid), html_escape(aid), html_escape(tc),
                       html_escape(tc_href), html_escape(format_timecode(tc)),
                       html_escape(a.get("title")), html_escape(aid),
                       "" if abody is None else
                       "<div class=\"annotation-body\">%s</div>" % html_escape(abody)))
            if ann_items:
                parts.append("<section class=\"annotations\" id=\"annotations\">"
                             "<h2>Annotations</h2><ol>\n%s\n</ol></section>"
                             % "\n".join(ann_items))
            else:
                parts.append("<section class=\"annotations\" id=\"annotations\">"
                             "<h2>Annotations</h2><p class=\"no-annotations\">"
                             "No annotations.</p></section>")
            parts.append("<script type=\"application/json\" id=\"annotation-data\">%s"
                         "</script>" % json_for_html(anns))
            parts.append("<script type=\"application/json\" id=\"chart-data\">%s</script>"
                         % json_for_html(charts))
            chart_html = []
            for f in ("views", "likes"):
                pts = charts[f]
                if len(pts) < 2:
                    continue
                rows = "".join("<tr><td>%s</td><td>%s</td></tr>"
                               % (html_escape(t), "null" if v is None else html_escape(v))
                               for t, v in ((p["timestamp"], p["value"]) for p in pts))
                chart_html.append(
                    "<section class=\"chart %s\" id=\"chart-%s\"><h3>%s</h3>%s"
                    "<table><thead><tr><th>Timestamp</th><th>%s</th></tr></thead>"
                    "<tbody>%s</tbody></table></section>"
                    % (f, f, f, svg_chart(f, pts), f, rows))
            if chart_html:
                parts.append("<section class=\"charts\"><h2>History</h2>%s</section>"
                             % "".join(chart_html))
            page_title = "%s / %s / %s" % (name, category, entry_id)
            return 200, html_page(page_title, "\n".join(parts))

        # ---- pages
        def _landing(self, query, head):
            parts = ["<h1>mvault viewer</h1>"]
            for key, fmt in (("notfound", "Vault '%s' not found."),
                             ("invalid", "Vault '%s' could not be opened (invalid catalog).")):
                for n in query.get(key, []):
                    parts.append("<p class=\"error vault-not-found\">%s</p>"
                                 % html_escape(fmt % n))
            parts.append(
                "<form method=\"post\" action=\"/\">\n"
                "<label for=\"catalog\">Vault name:</label>\n"
                "<input type=\"text\" id=\"catalog\" name=\"catalog\" required autofocus>\n"
                "<button type=\"submit\">Open</button>\n</form>")
            items = []
            for n in self._recent():
                try:
                    _, version = viewer_load(n)
                except MVaultError:
                    continue
                href = catalog_route(n, viewer_default_category(version))
                items.append("<li><a href=\"%s\">%s</a></li>"
                             % (html_escape(href), html_escape(n)))
            if items:
                parts.append("<section id=\"recent\"><h2>Recent vaults</h2>\n<ol>\n%s\n</ol>"
                             "</section>" % "\n".join(items))
            self._send(200, html_page("mvault viewer", "\n".join(parts)), head=head)

        def _listing(self, name, raw, version, category, entry_id):
            entries = raw.get(category) or []
            files = media_names(name)
            nav = []
            for c in viewer_valid_categories(version):
                cls = " class=\"current\"" if c == category else ""
                nav.append("<a%s href=\"%s\">%s</a>"
                           % (cls, html_escape(catalog_route(name, c)), html_escape(c)))
            rows = []
            found = entry_id is None
            for e in entries:
                if not isinstance(e, dict):
                    continue
                eid = str(e.get("id"))
                downloaded = any(eid in f for f in files) if eid else False
                removed = viewer_is_removed(e, version)
                classes = ["entry", "downloaded" if downloaded else "not-downloaded"]
                if version == 3:
                    classes.append("removed" if removed else "active")
                if entry_id is not None and eid == entry_id:
                    classes.append("highlight")
                    found = True
                href = catalog_route(name, category, eid)
                cells = [
                    "<td class=\"title\"><a href=\"%s\">%s</a></td>"
                    % (html_escape(href), html_escape(viewer_entry_title(e, version))),
                    "<td class=\"id\">%s</td>" % html_escape(eid),
                    "<td class=\"state\">%s</td>"
                    % ("&#10003; downloaded" if downloaded else "&#10007; not downloaded"),
                ]
                if version == 3:
                    cells.append("<td class=\"flag\">%s</td>"
                                 % ("REMOVED" if removed else "active"))
                rows.append("<tr id=\"entry-%s\" class=\"%s\" data-id=\"%s\" "
                            "data-downloaded=\"%s\"%s>%s</tr>"
                            % (html_escape(eid), " ".join(classes), html_escape(eid),
                               "true" if downloaded else "false",
                               (" data-removed=\"%s\"" % ("true" if removed else "false"))
                               if version == 3 else "",
                               "".join(cells)))
            head_cells = "<th>Title</th><th>ID</th><th>Downloaded</th>"
            if version == 3:
                head_cells += "<th>Status</th>"
            parts = [
                "<p><a href=\"/\">&larr; All vaults</a></p>",
                "<h1>%s &mdash; %s</h1>" % (html_escape(name), html_escape(category)),
                "<p>Catalog version %d</p>" % version,
                "<nav>%s</nav>" % "".join(nav),
            ]
            if entry_id is not None:
                if found:
                    parts.append("<p class=\"selected\">Selected entry: <a href=\"#entry-%s\">"
                                 "%s</a></p>" % (html_escape(entry_id), html_escape(entry_id)))
                else:
                    parts.append("<p class=\"error\">Entry '%s' not found in %s.</p>"
                                 % (html_escape(entry_id), html_escape(category)))
            if rows:
                parts.append("<table>\n<thead><tr>%s</tr></thead>\n<tbody>\n%s\n</tbody>\n"
                             "</table>" % (head_cells, "\n".join(rows)))
            else:
                parts.append("<p class=\"empty\">No entries.</p>")
            title = "%s / %s" % (name, category)
            return (200 if found else 404), html_page(title, "\n".join(parts))

    return ViewerHandler


def open_browser(url):
    if os.environ.get("MVAULT_NO_BROWSER"):
        return
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                  or os.environ.get("WAYLAND_DISPLAY")):
        return  # headless: avoid launching terminal browsers that grab the console
    import threading
    import webbrowser

    def run():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def cmd_serve(args):
    import http.server
    host = args.host if args.host is not None else DEFAULT_HOST
    port = args.port if args.port is not None else DEFAULT_PORT
    try:
        httpd = http.server.ThreadingHTTPServer((host, port), make_handler())
    except (OSError, OverflowError) as exc:
        raise MVaultError("cannot start server on %s:%d: %s" % (host, port, exc))
    httpd.daemon_threads = True
    url_host = host if host not in ("", "0.0.0.0", "::") else DEFAULT_HOST
    if ":" in url_host:
        url_host = "[%s]" % url_host
    base = "http://%s:%d" % (url_host, port)
    path = "/"
    if args.name is not None:
        try:
            _, version = viewer_load(args.name)
            path = catalog_route(args.name, viewer_default_category(version))
        except MVaultError:
            path = catalog_route(args.name)
    print("Serving mvault viewer at %s/ (press Ctrl+C to stop)" % base)
    sys.stdout.flush()
    open_browser(base + path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def port_number(text):
    t = text.strip()
    if not t.isdigit() or not t.isascii() or not (0 <= int(t) <= 65535):
        raise argparse.ArgumentTypeError("invalid port: %r" % text)
    return int(t)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mvault.py", allow_abbrev=False,
        description="Create local vaults for media-platform metadata and record "
                    "tracked-field history by sync timestamp.")
    sub = parser.add_subparsers(dest="command", metavar="{init,sync,migrate,digest,serve}",
                                title="subcommands")
    p_init = sub.add_parser("init", allow_abbrev=False, help="create a new vault with an empty catalog")
    p_init.add_argument("name", help="vault directory name")
    p_init.add_argument("url", help="source metadata URL")
    p_init.set_defaults(func=cmd_init)
    p_sync = sub.add_parser("sync", allow_abbrev=False, help="sync a vault with its source URL")
    p_sync.add_argument("name", help="vault directory name")
    for cat in CATEGORIES:
        p_sync.add_argument("--%s" % cat, type=non_negative_int, default=None, metavar="N",
                            help="maximum number of %s media downloads" % cat[:-1])
    p_sync.add_argument("--skip-metadata", action="store_true",
                        help="skip source fetch and metadata update")
    p_sync.add_argument("--skip-download", action="store_true",
                        help="skip media and preview downloads")
    p_sync.add_argument("--format", type=format_str, default=None, metavar="FMT",
                        help="override media download format and extension")
    p_sync.set_defaults(func=cmd_sync)
    p_mig = sub.add_parser("migrate", allow_abbrev=False, help="convert a legacy (v1/v2) vault catalog to v3")
    p_mig.add_argument("name", help="vault directory name")
    p_mig.set_defaults(func=cmd_migrate)
    p_dig = sub.add_parser("digest", allow_abbrev=False, help="summarize notable changes in a vault")
    p_dig.add_argument("name", help="vault directory name")
    p_dig.set_defaults(func=cmd_digest)
    p_srv = sub.add_parser("serve", allow_abbrev=False, help="start the local browser viewer")
    p_srv.add_argument("name", nargs="?", default=None, help="vault to open in the browser")
    p_srv.add_argument("--host", default=None, help="bind host (default %s)" % DEFAULT_HOST)
    p_srv.add_argument("--port", type=port_number, default=None,
                       help="bind port (default %d)" % DEFAULT_PORT)
    p_srv.set_defaults(func=cmd_serve)
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
