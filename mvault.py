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
    counts = {"added": 0, "removed": 0, "updated": 0}
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
                continue
            changed = False
            for field in SOURCE_TRACKED:
                if append_history(entry, field, item[field], ts):
                    changed = True
            removed, has = current_value(entry.get("removed"))
            if not has or removed is not False:
                append_history(entry, "removed", False, ts)
                if has and removed is True:
                    changed = True
            if changed:
                counts["updated"] += 1
        for e in entries:
            if e["id"] in src:
                continue
            removed, has = current_value(e.get("removed"))
            if not has or removed is not True:
                append_history(e, "removed", True, ts)
                counts["removed"] += 1
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
        buckets = {"removed": [], "added": [], "updated": []}
        for e in entries:
            group, changed = classify_entry(e, version)
            if group is None:
                continue
            text = entry_title(e, version)
            if group == "updated":
                text += " (%s)" % ", ".join(changed)
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mvault.py", allow_abbrev=False,
        description="Create local vaults for media-platform metadata and record "
                    "tracked-field history by sync timestamp.")
    sub = parser.add_subparsers(dest="command", metavar="{init,sync,migrate,digest}",
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
