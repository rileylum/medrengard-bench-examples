#!/usr/bin/env python3
"""Single-run backup scheduler simulator.

Parses a YAML schedule, determines which jobs are due in the window
[--now, --now + --duration], simulates running them (file selection with
exclusion globs) and writes a JSON Lines event history to stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MOUNT_PREFIX = "mount://"
BACKUP_PREFIX = "backup://"
STRATEGY_KINDS = ("full", "pack", "verify", "dry-run")
DEFAULT_MAX_PACK_BYTES = 1048576


class ScheduleError(Exception):
    pass


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Glob matching
# --------------------------------------------------------------------------

_glob_cache: dict[str, re.Pattern] = {}


def glob_to_regex(pattern: str) -> re.Pattern:
    cached = _glob_cache.get(pattern)
    if cached is not None:
        return cached
    pat = pattern
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    out = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                # collapse runs of '*'
                j = i
                while j < n and pat[j] == "*":
                    j += 1
                at_seg_start = i == 0 or pat[i - 1] == "/"
                if at_seg_start and j < n and pat[j] == "/":
                    # '**/' -> zero or more directories
                    out.append("(?:.*/)?")
                    i = j + 1
                elif at_seg_start and j == n and i > 0:
                    # trailing '/**' -> everything below
                    out.append(".*")
                    i = j
                else:
                    out.append(".*")
                    i = j
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
                continue
            body = pat[i + 1:j]
            negate = False
            if body and body[0] in "!^":
                negate = True
                body = body[1:]
            body = body.replace("\\", "\\\\")
            # escape regex-special chars inside class except '-'
            body = body.replace("[", "\\[").replace("]", "\\]") if body else body
            if negate:
                out.append("[^/" + body + "]")
            else:
                out.append("(?:(?!/)[" + body + "])")
            i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    try:
        rx = re.compile("^" + "".join(out) + "$", re.DOTALL)
    except re.error:
        rx = re.compile("^" + re.escape(pat) + "$")
    _glob_cache[pattern] = rx
    return rx


def first_match(path: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if glob_to_regex(p).match(path):
            return p
    return None


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def parse_now(value: str) -> datetime:
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ScheduleError(f"invalid --now value: {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_local(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    base = dt.replace(tzinfo=None).isoformat(timespec="seconds")
    if off == timedelta(0):
        return base + "Z"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh, rem = divmod(total, 3600)
    mm = rem // 60
    return f"{base}{sign}{hh:02d}:{mm:02d}"


def parse_hhmm(value, job_id: str) -> time:
    if isinstance(value, bool):
        raise ScheduleError(f"job {job_id}: invalid 'at' value")
    if isinstance(value, int):
        # YAML 1.1 sexagesimal (e.g. unquoted 12:30 -> 750)
        h, m = divmod(value, 60)
        if 0 <= h <= 23:
            return time(h, m)
        raise ScheduleError(f"job {job_id}: invalid 'at' value {value!r}")
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0, tzinfo=None)
    if not isinstance(value, str):
        raise ScheduleError(f"job {job_id}: invalid 'at' value {value!r}")
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", value)
    if not m:
        raise ScheduleError(f"job {job_id}: invalid 'at' time {value!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ScheduleError(f"job {job_id}: invalid 'at' time {value!r}")
    return time(h, mi)


def parse_once(value, job_id: str, tz) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time(0, 0))
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise ScheduleError(f"job {job_id}: invalid once 'at' {value!r}") from e
    else:
        raise ScheduleError(f"job {job_id}: invalid once 'at' {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return floor_minute(dt)


def local_to_aware(d: date, t: time, tz) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=tz)


# --------------------------------------------------------------------------
# Schedule parsing
# --------------------------------------------------------------------------

def parse_strategy(raw, job_id: str):
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"kind": raw}
    if not isinstance(raw, dict):
        raise ScheduleError(f"job {job_id}: strategy must be a mapping")
    kind = raw.get("kind")
    if isinstance(kind, str):
        kind = kind.strip().lower().replace("_", "-")
        if kind == "dryrun":
            kind = "dry-run"
    if kind not in STRATEGY_KINDS:
        raise ScheduleError(f"job {job_id}: invalid strategy kind {kind!r}")
    options = raw.get("options") or {}
    if not isinstance(options, dict):
        raise ScheduleError(f"job {job_id}: strategy options must be a mapping")
    strategy = {"kind": kind, "options": options}
    if kind == "pack":
        mpb = options.get("max_pack_bytes", DEFAULT_MAX_PACK_BYTES)
        if mpb is None:
            mpb = DEFAULT_MAX_PACK_BYTES
        if isinstance(mpb, bool):
            raise ScheduleError(f"job {job_id}: invalid max_pack_bytes")
        if isinstance(mpb, float) and mpb.is_integer():
            mpb = int(mpb)
        if isinstance(mpb, str) and mpb.strip().isdigit():
            mpb = int(mpb.strip())
        if not isinstance(mpb, int) or mpb <= 0:
            raise ScheduleError(f"job {job_id}: invalid max_pack_bytes {mpb!r}")
        strategy["max_pack_bytes"] = mpb
    return strategy


def load_schedule(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise ScheduleError(f"cannot read schedule: {e}") from e
    except yaml.YAMLError as e:
        raise ScheduleError(f"invalid YAML: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ScheduleError("schedule must be a mapping")
    tzname = data.get("timezone")
    if tzname is None:
        tzname = "UTC"
    if not isinstance(tzname, str):
        raise ScheduleError("timezone must be a string")
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(f"unknown timezone: {tzname!r}") from e
    jobs = data.get("jobs")
    if jobs is None:
        jobs = []
    if not isinstance(jobs, list):
        raise ScheduleError("jobs must be a list")
    parsed = []
    for idx, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ScheduleError(f"job #{idx} must be a mapping")
        job_id = job.get("id")
        if job_id is None:
            raise ScheduleError(f"job #{idx} missing id")
        job_id = str(job_id)
        enabled = job.get("enabled", True)
        if enabled is None:
            enabled = True
        when = job.get("when") or {}
        if not isinstance(when, dict):
            raise ScheduleError(f"job {job_id}: 'when' must be a mapping")
        kind = when.get("kind")
        if kind not in ("daily", "weekly", "once"):
            raise ScheduleError(f"job {job_id}: invalid kind {kind!r}")
        exclude = job.get("exclude") or []
        if isinstance(exclude, str):
            exclude = [exclude]
        if not isinstance(exclude, list):
            raise ScheduleError(f"job {job_id}: exclude must be a list")
        exclude = [str(p) for p in exclude]
        source = job.get("source", MOUNT_PREFIX)
        if source is None:
            source = MOUNT_PREFIX
        strategy = parse_strategy(job.get("strategy"), job_id)
        destination = job.get("destination")
        if destination is not None and not isinstance(destination, str):
            raise ScheduleError(f"job {job_id}: destination must be a string")
        entry = {
            "strategy": strategy,
            "id": job_id,
            "enabled": bool(enabled),
            "kind": kind,
            "exclude": exclude,
            "source": str(source),
            "destination": destination,
        }
        if entry["enabled"]:
            if kind in ("daily", "weekly"):
                entry["at"] = parse_hhmm(when.get("at"), job_id)
                if kind == "weekly":
                    days = when.get("days") or []
                    if isinstance(days, str):
                        days = [days]
                    wd = set()
                    for d in days:
                        key = str(d).strip().lower()[:3]
                        if key not in DAY_NAMES:
                            raise ScheduleError(f"job {job_id}: invalid day {d!r}")
                        wd.add(DAY_NAMES[key])
                    entry["days"] = wd
            else:
                entry["at"] = parse_once(when.get("at"), job_id, tz)
        parsed.append(entry)
    return {"timezone": tzname, "tz": tz, "jobs": parsed}


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------

def triggers_in_window(job: dict, start: datetime, end: datetime, tz) -> list[datetime]:
    """Return aware local datetimes of triggers within [start, end]."""
    if not job["enabled"]:
        return []
    results = []
    if job["kind"] == "once":
        t = job["at"]
        if start <= t <= end:
            results.append(t)
        return results
    d = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    seen = set()
    while d <= last:
        if job["kind"] == "daily" or d.weekday() in job["days"]:
            local = local_to_aware(d, job["at"], tz)
            # normalise through UTC so nonexistent local times resolve
            utc = local.astimezone(timezone.utc)
            if start <= utc <= end and utc not in seen:
                seen.add(utc)
                results.append(utc.astimezone(tz))
        d += timedelta(days=1)
    return results


# --------------------------------------------------------------------------
# File walking
# --------------------------------------------------------------------------

def resolve_source(source: str, mount: str) -> str:
    if source.startswith(MOUNT_PREFIX):
        rel = source[len(MOUNT_PREFIX):]
    else:
        rel = source
    rel = rel.strip("/")
    return os.path.join(mount, rel) if rel else mount


def resolve_destination(destination, backup):
    """Return the base destination directory, or None if no backups exist."""
    if destination is None or backup is None:
        return None
    if destination.startswith(BACKUP_PREFIX):
        rel = destination[len(BACKUP_PREFIX):]
    else:
        rel = destination
    rel = rel.strip("/")
    return os.path.join(backup, rel) if rel else backup


def file_sha256(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return "sha256:" + h.hexdigest()


def load_dest_state(dest_root: str | None, job_id: str) -> dict[str, str]:
    """Map relative path -> checksum for existing (non-.tar) backups of a job."""
    state: dict[str, str] = {}
    if dest_root is None:
        return state
    job_dir = os.path.join(dest_root, job_id)
    for rel in list_files(job_dir):
        if rel.lower().endswith(".tar"):
            continue
        digest = file_sha256(os.path.join(job_dir, rel))
        if digest is not None:
            state[rel] = digest
    return state


def list_files(root: str) -> list[str]:
    files = []
    if not os.path.isdir(root):
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files.append(rel)
    files.sort()
    return files


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

def read_file(root: str, rel: str) -> bytes:
    try:
        with open(os.path.join(root, rel), "rb") as f:
            return f.read()
    except OSError:
        return b""


def sha256_tag(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_tar(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Packer:
    def __init__(self, job_id: str, limit: int, timestamp: str):
        self.job_id = job_id
        self.limit = limit
        self.timestamp = timestamp
        self.index = 0
        self.entries: list[tuple[str, bytes]] = []
        self.size = 0
        self.packs = 0

    def add(self, path: str, data: bytes) -> None:
        if self.entries and self.size + len(data) > self.limit:
            self.finalize()
        if not self.entries:
            self.index += 1
        self.entries.append((path, data))
        self.size += len(data)
        emit({"event": "FILE_PACKED", "job_id": self.job_id, "pack_id": self.index,
              "path": path, "size": len(data)})

    def finalize(self) -> None:
        if not self.entries:
            return
        tar_bytes = build_tar(self.entries)
        emit({"event": "PACK_CREATED", "job_id": self.job_id,
              "name": f"pack-{self.index}.tar", "size": self.size,
              "timestamp": self.timestamp, "checksum": sha256_tag(tar_bytes),
              "tar_size": len(tar_bytes)})
        self.packs += 1
        self.entries = []
        self.size = 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(args) -> int:
    sched = load_schedule(args.schedule)
    tz = sched["tz"]
    start = floor_minute(parse_now(args.now))
    try:
        hours = float(args.duration)
    except ValueError as e:
        raise ScheduleError(f"invalid --duration: {args.duration!r}") from e
    if hours < 0:
        raise ScheduleError("--duration must be non-negative")
    end = floor_minute(start + timedelta(hours=hours))

    emit({"event": "SCHEDULE_PARSED", "timezone": sched["timezone"],
          "jobs_total": len(sched["jobs"])})

    runs = []
    for job in sched["jobs"]:
        for t in triggers_in_window(job, start, end, tz):
            runs.append((t.astimezone(timezone.utc), job["id"], t, job))
    runs.sort(key=lambda r: (r[0], r[1]))

    file_cache: dict[str, list[str]] = {}
    for _, job_id, local_t, job in runs:
        emit({"event": "JOB_ELIGIBLE", "job_id": job_id, "kind": job["kind"],
              "now_local": format_local(local_t)})
        emit({"event": "JOB_STARTED", "job_id": job_id,
              "exclude_count": len(job["exclude"])})
        strategy = job.get("strategy")
        skind = strategy["kind"] if strategy else None
        dest_state = load_dest_state(
            resolve_destination(job.get("destination"), args.backup), job_id)
        if dest_state:
            emit({"event": "DEST_STATE_LOADED", "job_id": job_id,
                  "files_total": len(dest_state)})
        incremental = skind in ("full", "verify")
        skipped_unchanged = 0
        if strategy:
            emit({"event": "STRATEGY_SELECTED", "job_id": job_id, "kind": skind})
        packer = None
        if skind == "pack":
            packer = Packer(job_id, strategy["max_pack_bytes"], format_local(local_t))
        total_size = 0
        root = resolve_source(job["source"], args.mount)
        if root not in file_cache:
            file_cache[root] = list_files(root)
        selected = excluded = 0
        for path in file_cache[root]:
            pat = first_match(path, job["exclude"])
            if pat is None:
                selected += 1
                emit({"event": "FILE_SELECTED", "job_id": job_id, "path": path})
                if skind:
                    data = read_file(root, path)
                    if incremental and path in dest_state:
                        digest = sha256_tag(data)
                        if dest_state[path] == digest:
                            skipped_unchanged += 1
                            emit({"event": "FILE_SKIPPED_UNCHANGED", "job_id": job_id,
                                  "path": path, "hash": digest})
                            continue
                    total_size += len(data)
                    if skind == "pack":
                        packer.add(path, data)
                    elif skind in ("full", "verify"):
                        emit({"event": "FILE_BACKED_UP" if skind == "full"
                              else "FILE_VERIFIED", "job_id": job_id, "path": path,
                              "size": len(data), "checksum": sha256_tag(data)})
            else:
                excluded += 1
                emit({"event": "FILE_EXCLUDED", "job_id": job_id, "path": path,
                      "pattern": pat})
        done = {"event": "JOB_COMPLETED", "job_id": job_id, "selected": selected,
                "excluded": excluded}
        if packer is not None:
            packer.finalize()
            done["packs"] = packer.packs
        if skind:
            done["total_size"] = total_size
            done["files_skipped_unchanged"] = skipped_unchanged
            done["dest_state_files"] = len(dest_state)
        emit(done)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Simulated backup scheduler")
    parser.add_argument("--schedule", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--duration", default="24")
    parser.add_argument("--mount", default=".")
    parser.add_argument("--backup", default=None)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ScheduleError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
