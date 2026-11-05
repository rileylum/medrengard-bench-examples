#!/usr/bin/env python3
"""Tiny local runner HTTP API (stdlib only)."""
import argparse
import base64
import bz2
import copy
import hashlib
import csv
import glob
import gzip
import io
import json
import math
import os
import re
import shutil
import signal
import stat
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 50 * 1024 * 1024


class ClientError(Exception):
    def __init__(self, message, code="INVALID_REQUEST", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _dist(ds):
    if not ds:
        return {"average": None, "median": None, "max": None, "min": None,
                "stddev": None}
    return {
        "average": round(statistics.fmean(ds), 3),
        "median": round(statistics.median(ds), 3),
        "max": round(max(ds), 3),
        "min": round(min(ds), 3),
        "stddev": round(statistics.pstdev(ds), 3),
    }


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._durations = []
        self._cmd_durations = []
        self._cmd_total = 0
        self._hits = 0
        self._misses = 0

    def hit(self):
        with self._lock:
            self._hits += 1

    def miss(self):
        with self._lock:
            self._misses += 1

    def add(self, duration, cmd_durations, cmd_total):
        with self._lock:
            self._durations.append(duration)
            self._cmd_durations.extend(cmd_durations)
            self._cmd_total += cmd_total

    def snapshot(self):
        with self._lock:
            ds = list(self._durations)
            cds = list(self._cmd_durations)
            total = self._cmd_total
            hits, misses = self._hits, self._misses
        n = len(ds)
        lookups = hits + misses
        if lookups:
            cache = {"hits": hits, "misses": misses,
                     "hit_rate": round(hits / lookups, 3)}
        else:
            cache = {"hits": None, "misses": None, "hit_rate": None}
        return {
            "ran": n,
            "commands": {
                "total": total,
                "ran": len(cds),
                "average": round(total / n, 3) if n else None,
                "average_ran": round(len(cds) / n, 3) if n else None,
                "duration": _dist(cds),
            },
            "duration": _dist(ds),
            "cache": cache,
        }


STATS = Stats()


class ResultCache:
    """In-memory cache of execution results keyed by the normalized request."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    @staticmethod
    def key(command, env, files, stdin, timeout, track, coe, environment=None):
        canon = {
            "environment": environment,
            "command": command,
            "env": sorted(env.items()),
            "files": sorted((k, base64.b64encode(v).decode("ascii"))
                            for k, v in files.items()),
            "stdin": stdin,
            "timeout": timeout,
            "track": track,
            "continue_on_error": coe,
        }
        raw = json.dumps(canon, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key):
        with self._lock:
            v = self._data.get(key)
        return copy.deepcopy(v) if v is not None else None

    def put(self, key, result):
        stored = copy.deepcopy(result)
        stored.pop("id", None)
        stored.pop("cached", None)
        stored.pop("environment", None)
        with self._lock:
            self._data[key] = stored


CACHE = ResultCache()


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_cmd(cmd, where):
    if not isinstance(cmd, str):
        raise ClientError(f"{where} must be a string", "INVALID_FIELD")
    if not cmd.strip():
        raise ClientError(f"{where} must be a non-empty string", "INVALID_FIELD")
    if "\x00" in cmd:
        raise ClientError(f"{where} must not contain NUL bytes", "INVALID_FIELD")


def validate_files(files, originals=None):
    if files is None:
        files = {}
    if not isinstance(files, dict):
        raise ClientError("'files' must be an object of path to string", "INVALID_FIELD")
    clean_files = {}
    for path, content in files.items():
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ClientError(f"invalid file path: {path!r}", "INVALID_FIELD")
        if os.path.isabs(path) or path.startswith("/") or path.startswith("\\"):
            raise ClientError(f"file path must be relative: {path!r}", "INVALID_FIELD")
        norm = os.path.normpath(path)
        parts = norm.replace("\\", "/").split("/")
        if norm in (".", "") or ".." in parts:
            raise ClientError(f"file path escapes working directory: {path!r}", "INVALID_FIELD")
        if path.endswith("/"):
            raise ClientError(f"file path must name a file: {path!r}", "INVALID_FIELD")
        clean_files[norm] = encode_file(path, content)
        if originals is not None:
            originals[path] = norm
    return clean_files


def validate(body):
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object", "INVALID_REQUEST")

    if "command" not in body or body["command"] is None:
        raise ClientError("'command' is required", "MISSING_FIELD")
    command = body["command"]
    if isinstance(command, str):
        _check_cmd(command, "'command'")
    elif isinstance(command, list):
        if not command:
            raise ClientError("'command' array must not be empty", "INVALID_FIELD")
        chain = []
        for i, item in enumerate(command):
            where = f"'command[{i}]'"
            if not isinstance(item, dict):
                raise ClientError(f"{where} must be an object", "INVALID_FIELD")
            if "cmd" not in item or item["cmd"] is None:
                raise ClientError(f"{where}.cmd is required", "MISSING_FIELD")
            _check_cmd(item["cmd"], f"{where}.cmd")
            t = item.get("timeout")
            if t is not None and (not _is_number(t) or not math.isfinite(t) or t <= 0):
                raise ClientError(f"{where}.timeout must be a positive number of seconds",
                                  "INVALID_FIELD")
            req = item.get("required")
            if req is None:
                req = False
            if not isinstance(req, bool):
                raise ClientError(f"{where}.required must be a boolean", "INVALID_FIELD")
            chain.append({"cmd": item["cmd"],
                          "timeout": None if t is None else float(t),
                          "required": req})
        command = chain
    else:
        raise ClientError("'command' must be a string or an array of command objects",
                          "INVALID_FIELD")

    coe = body.get("continue_on_error")
    if coe is None:
        coe = False
    if not isinstance(coe, bool):
        raise ClientError("'continue_on_error' must be a boolean", "INVALID_FIELD")

    env = body.get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise ClientError("'env' must be an object of string to string", "INVALID_FIELD")
    for k, v in env.items():
        if not isinstance(k, str) or not k or "=" in k or "\x00" in k:
            raise ClientError(f"invalid env variable name: {k!r}", "INVALID_FIELD")
        if not isinstance(v, str) or "\x00" in v:
            raise ClientError(f"env value for {k!r} must be a string", "INVALID_FIELD")

    clean_files = validate_files(body.get("files"))

    stdin = body.get("stdin")
    if stdin is None:
        stdin = ""
    if isinstance(stdin, list):
        if not all(isinstance(s, str) for s in stdin):
            raise ClientError("'stdin' array must contain only strings", "INVALID_FIELD")
        stdin = "\n".join(stdin)
        if body["stdin"] and stdin and not stdin.endswith("\n"):
            stdin += "\n"
    elif not isinstance(stdin, str):
        raise ClientError("'stdin' must be a string or array of strings", "INVALID_FIELD")

    timeout = body.get("timeout")
    if timeout is None:
        timeout = 10
    if not _is_number(timeout) or not math.isfinite(timeout) or timeout <= 0:
        raise ClientError("'timeout' must be a positive number of seconds", "INVALID_FIELD")

    track = validate_track(body.get("track"))

    force = body.get("force")
    if force is None:
        force = False
    if not isinstance(force, bool):
        raise ClientError("'force' must be a boolean", "INVALID_FIELD")

    return (command, env, clean_files, stdin, float(timeout), track, coe), force


COMPRESSIONS = {".gz": "gz", ".bz2": "bz2"}
JSON_EXTS = {".json"}
YAML_EXTS = {".yaml", ".yml"}
JSONL_EXTS = {".jsonl", ".ndjson"}
TABLE_EXTS = {".csv": ",", ".tsv": "\t"}


def _suffixes(path):
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    parts = name.split(".")
    if len(parts) <= 1:
        return []
    # A leading dot (hidden file) is not an extension separator.
    if parts[0] == "":
        parts = parts[1:]
        if len(parts) <= 1:
            return []
    return ["." + p.lower() for p in parts[1:]]


def _bad(path, msg):
    return ClientError(f"file {path!r}: {msg}", "INVALID_FIELD")


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _to_rows(path, value):
    """Normalize a 'dict of columns' or 'list of rows' value into a list of rows."""
    if isinstance(value, dict):
        cols = sorted(value.keys())
        for c in cols:
            if not isinstance(value[c], list):
                raise _bad(path, "dict of columns must map column names to arrays")
        n = max((len(value[c]) for c in cols), default=0)
        rows = []
        for i in range(n):
            row = {}
            for c in cols:
                if i < len(value[c]):
                    row[c] = value[c][i]
            rows.append(row)
        return rows
    if isinstance(value, list):
        return value
    raise _bad(path, "expected a dict of columns or a list of rows")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return _json_dumps(v)
    return _json_dumps(v)


def _serialize_table(path, value, delimiter):
    if isinstance(value, str):
        return value if value else "\n"
    rows = _to_rows(path, value)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    if all(isinstance(r, dict) for r in rows):
        header = sorted({k for r in rows for k in r})
        w.writerow(header)
        for r in rows:
            w.writerow([_cell(r.get(k)) for k in header])
    elif all(isinstance(r, list) for r in rows):
        for r in rows:
            w.writerow([_cell(c) for c in r])
    else:
        raise _bad(path, "rows must all be objects or all be arrays")
    out = buf.getvalue()
    return out if out else "\n"


def _serialize_jsonl(path, value):
    if isinstance(value, str):
        return value
    rows = _to_rows(path, value)
    return "".join(_json_dumps(r) + "\n" for r in rows)


def _serialize_yaml(value):
    try:
        import yaml
    except ImportError:  # JSON is valid YAML
        return _json_dumps(value) + "\n"
    return yaml.safe_dump(value, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)


def encode_file(path, content):
    """Turn a request file value into the bytes to write on disk."""
    sfx = _suffixes(path)
    comp = None
    if sfx and sfx[-1] in COMPRESSIONS:
        comp = COMPRESSIONS[sfx[-1]]
        sfx = sfx[:-1]
        if sfx and sfx[-1] in COMPRESSIONS:
            raise _bad(path, "multiple compression extensions are not allowed")
    base = sfx[-1] if sfx else ""

    try:
        if base in JSON_EXTS:
            text = content if isinstance(content, str) else _json_dumps(content) + "\n"
        elif base in YAML_EXTS:
            text = content if isinstance(content, str) else _serialize_yaml(content)
        elif base in JSONL_EXTS:
            text = _serialize_jsonl(path, content)
        elif base in TABLE_EXTS:
            text = _serialize_table(path, content, TABLE_EXTS[base])
        else:
            if isinstance(content, str):
                text = content
            elif content is None:
                raise _bad(path, "content must not be null")
            else:
                text = _json_dumps(content)
    except (ValueError, RecursionError) as e:
        raise _bad(path, f"cannot serialize content: {e}")

    data = text.encode("utf-8")
    if comp == "gz":
        data = gzip.compress(data, mtime=0)
    elif comp == "bz2":
        data = bz2.compress(data)
    return data


def validate_track(track):
    if track is None:
        return []
    if not isinstance(track, list):
        raise ClientError("'track' must be an array of glob strings", "INVALID_FIELD")
    clean = []
    for pat in track:
        if not isinstance(pat, str):
            raise ClientError("'track' entries must be strings", "INVALID_FIELD")
        if not pat.strip() or "\x00" in pat:
            raise ClientError(f"invalid track pattern: {pat!r}", "INVALID_FIELD")
        if pat.startswith("/") or pat.startswith("\\") or os.path.isabs(pat):
            raise ClientError(f"track pattern must be relative: {pat!r}", "INVALID_FIELD")
        parts = pat.replace("\\", "/").split("/")
        if ".." in parts:
            raise ClientError(f"track pattern escapes working directory: {pat!r}",
                              "INVALID_FIELD")
        if pat.count("[") != pat.count("]"):
            raise ClientError(f"invalid track pattern: {pat!r}", "INVALID_FIELD")
        clean.append(pat)
    return clean


def collect_tracked(workdir, patterns):
    root = os.path.realpath(workdir)
    matched = set()
    for pat in patterns:
        for rel in glob.glob(pat, root_dir=root, recursive=True):
            norm = os.path.normpath(rel)
            if norm in (".", "") or norm.startswith(".."):
                continue
            full = os.path.join(root, norm)
            real = os.path.realpath(full)
            if real != root and not real.startswith(root + os.sep):
                continue
            if not os.path.isfile(real):
                continue
            matched.add(norm)
    out = {}
    for rel in sorted(matched):
        try:
            with open(os.path.join(root, rel), "rb") as f:
                data = f.read()
        except OSError:
            continue
        out[rel.replace(os.sep, "/")] = data.decode("utf-8", errors="replace")
    return out


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def run_one(command, workdir, run_env, stdin, timeout):
    timed_out = False
    start = time.monotonic()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=workdir,
        env=run_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=stdin.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    duration = time.monotonic() - start
    # Clean up any leftover background children of the run.
    if not timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass

    exit_code = -1 if timed_out else proc.returncode
    if exit_code is None:
        exit_code = -1
    elif exit_code < 0 and not timed_out:
        exit_code = 128 + (-exit_code)

    return {
        "stdout": (out or b"").decode("utf-8", errors="replace"),
        "stderr": (err or b"").decode("utf-8", errors="replace"),
        "exit_code": exit_code,
        "duration": round(duration, 3),
        "timed_out": timed_out,
    }, duration


def write_files(workdir, files):
    root = os.path.realpath(workdir)
    for rel, content in files.items():
        full = os.path.join(root, rel)
        d = os.path.dirname(full)
        try:
            os.makedirs(d, exist_ok=True)
            real_d = os.path.realpath(d)
            if real_d != root and not real_d.startswith(root + os.sep):
                raise ClientError(f"file path escapes working directory: {rel!r}",
                                  "INVALID_FIELD")
            if os.path.islink(full):
                os.unlink(full)
            with open(full, "wb") as f:
                f.write(content)
        except (IsADirectoryError, NotADirectoryError, FileExistsError) as e:
            raise ClientError(f"cannot create file {rel!r}: {e.strerror}", "INVALID_FIELD")


def execute(command, env, files, stdin, timeout, track=None, continue_on_error=False,
            workdir=None):
    """Returns (response, total_duration, per-command durations, command count).

    If ``workdir`` is given it is used (and left in place) instead of a fresh
    temporary directory."""
    own = workdir is None
    if own:
        workdir = tempfile.mkdtemp(prefix="run-")
    try:
        write_files(workdir, files)

        run_env = dict(os.environ)
        run_env.update(env)

        if isinstance(command, str):
            res, duration = run_one(command, workdir, run_env, stdin, timeout)
            result = {"id": str(uuid.uuid4())}
            result.update(res)
            result["files"] = collect_tracked(workdir, track) if track else {}
            return result, duration, [duration], 1

        results = []
        durations = []
        failed = False
        first_stdin = True
        for item in command:
            if failed and not continue_on_error and not item["required"]:
                continue
            t = item["timeout"] if item["timeout"] is not None else timeout
            res, dur = run_one(item["cmd"], workdir, run_env,
                               stdin if first_stdin else "", t)
            first_stdin = False
            entry = {"cmd": item["cmd"]}
            entry.update(res)
            if item["required"]:
                entry["required"] = True
            results.append(entry)
            durations.append(dur)
            if res["exit_code"] != 0 or res["timed_out"]:
                failed = True

        overall = 0
        for r in results:
            if r["exit_code"] != 0:
                overall = r["exit_code"]
                break
        total = sum(durations)
        result = {
            "id": str(uuid.uuid4()),
            "commands": results,
            "exit_code": overall,
            "duration": round(sum(r["duration"] for r in results), 3),
            "timed_out": any(r["timed_out"] for r in results),
            "files": collect_tracked(workdir, track) if track else {},
        }
        return result, total, durations, len(command)
    finally:
        if own:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Persistent environments
# ---------------------------------------------------------------------------

CONCURRENCY_MODES = ("fork", "base", "never")
ENV_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def tree_hash(root):
    """Content hash of a directory tree (paths, types, modes, contents)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        entries = sorted(dirnames + filenames)
        for name in entries:
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            try:
                st = os.lstat(full)
            except OSError:
                continue
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            if stat.S_ISLNK(st.st_mode):
                h.update(b"L" + os.readlink(full).encode("utf-8", "surrogateescape"))
            elif stat.S_ISDIR(st.st_mode):
                h.update(b"D%o" % stat.S_IMODE(st.st_mode))
            elif stat.S_ISREG(st.st_mode):
                h.update(b"F%o:%d:" % (stat.S_IMODE(st.st_mode), st.st_size))
                try:
                    with open(full, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                except OSError:
                    pass
            else:
                h.update(b"O")
            h.update(b"\0")
    return h.hexdigest()


def copy_tree(src, dst):
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)


class Environment:
    def __init__(self, name, mode, snapshot_dir):
        self.name = name
        self.mode = mode
        self.lock = threading.Lock()      # guards snapshot swaps / copies / busy
        self.snapshot = snapshot_dir      # current state (base state for "base")
        self.hash = tree_hash(snapshot_dir)
        self.busy = False                 # an exclusive execution is running


class EnvironmentStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._envs = {}
        self._root = None

    def root(self):
        with self._lock:
            if self._root is None or not os.path.isdir(self._root):
                self._root = tempfile.mkdtemp(prefix="envs-")
            return self._root

    def new_dir(self, prefix):
        return tempfile.mkdtemp(prefix=prefix, dir=self.root())

    def get(self, name):
        with self._lock:
            return self._envs.get(name)

    def create(self, name, mode, files):
        with self._lock:
            if name in self._envs:
                raise ClientError("Environment already exists", "ENVIRONMENT_EXISTS", 409)
        d = self.new_dir("snap-")
        try:
            write_files(d, files)
            env = Environment(name, mode, d)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        with self._lock:
            if name in self._envs:
                shutil.rmtree(d, ignore_errors=True)
                raise ClientError("Environment already exists", "ENVIRONMENT_EXISTS", 409)
            self._envs[name] = env
        return env


ENVS = EnvironmentStore()


def validate_environment_create(body):
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object", "INVALID_REQUEST")
    name = body.get("name")
    if name is None:
        raise ClientError("Missing required field: name", "INVALID_ENVIRONMENT")
    if not isinstance(name, str) or not ENV_NAME_RE.match(name) or name in (".", ".."):
        raise ClientError("Invalid environment name", "INVALID_ENVIRONMENT")
    mode = body.get("concurrency_mode")
    if mode is None:
        mode = "never"
    if not isinstance(mode, str) or mode not in CONCURRENCY_MODES:
        raise ClientError("Invalid concurrency_mode: must be one of fork, base, never",
                          "INVALID_ENVIRONMENT")
    originals = {}
    files = validate_files(body.get("files"), originals)
    return name, mode, files, originals


def validate_environment_ref(body):
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object", "INVALID_REQUEST")
    name = body.get("environment")
    if name is None or name == "":
        raise ClientError("Missing required field: environment", "MISSING_ENVIRONMENT")
    if not isinstance(name, str):
        raise ClientError("'environment' must be a string", "INVALID_ENVIRONMENT")
    env = ENVS.get(name)
    if env is None:
        raise ClientError("Environment not found", "ENVIRONMENT_NOT_FOUND", 404)
    return env


class EnvSession:
    """Acquires an environment for one execution according to its mode."""

    def __init__(self, env):
        self.env = env
        self.source = None
        self.workdir = None
        self.pre_hash = None
        self.released = False

    def acquire(self):
        env = self.env
        with env.lock:
            if env.mode == "never":
                if env.busy:
                    raise ClientError("Environment is in use", "ENVIRONMENT_LOCKED", 423)
                env.busy = True
                self.source = "exclusive"
            elif env.mode == "fork":
                if env.busy:
                    self.source = "current"
                else:
                    env.busy = True
                    self.source = "exclusive"
            else:
                self.source = "base"
            self.pre_hash = env.hash
            snap = env.snapshot
            # Copy while holding the lock so commits can't swap it mid-copy.
            if self.workdir is None:
                self.workdir = ENVS.new_dir("run-")
                try:
                    copy_tree(snap, self.workdir)
                except BaseException:
                    self._release_locked()
                    raise
        return self

    @property
    def commits(self):
        return self.env.mode in ("never", "fork")

    def meta(self):
        return {"name": self.env.name, "snapshot_source": self.source,
                "committed": self.commits}

    def _release_locked(self):
        if self.released:
            return
        self.released = True
        if self.source == "exclusive":
            self.env.busy = False

    def finish(self, commit):
        """Commit the workdir (if requested) and release. Returns new hash."""
        env = self.env
        new_hash = None
        old = None
        try:
            if commit and self.commits and self.workdir is not None:
                new_hash = tree_hash(self.workdir)
                with env.lock:
                    old = env.snapshot
                    env.snapshot = self.workdir
                    env.hash = new_hash
                    self.workdir = None
        finally:
            with env.lock:
                self._release_locked()
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)
            if self.workdir is not None:
                shutil.rmtree(self.workdir, ignore_errors=True)
                self.workdir = None
        return new_hash


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ExecutionServer/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        self._send(status, {"error": message, "code": code})

    def _path(self):
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
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
        try:
            n = int(length)
        except ValueError:
            raise ClientError("invalid Content-Length", "INVALID_REQUEST")
        if n < 0:
            raise ClientError("invalid Content-Length", "INVALID_REQUEST")
        if n > MAX_BODY:
            raise ClientError("request body too large", "PAYLOAD_TOO_LARGE", 413)
        return self.rfile.read(n)

    def _json_body(self):
        raw = self._read_body()
        if not raw.strip():
            raise ClientError("request body is required", "INVALID_JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ClientError("malformed JSON body", "INVALID_JSON")

    def do_POST(self):
        path = self._path()
        if path == "/v1/environment":
            return self._create_environment()
        if path != "/v1/execute":
            if path == "/v1/stats/execution":
                return self._error(405, "METHOD_NOT_ALLOWED", "method not allowed")
            return self._error(404, "NOT_FOUND", "not found")
        try:
            body = self._json_body()
            args, force = validate(body)
            env = validate_environment_ref(body)
            session = EnvSession(env).acquire()
            hit = False
            try:
                key = ResultCache.key(*args, environment=[env.name, env.mode,
                                                          session.pre_hash])
                cached = None if force else CACHE.get(key)
                if cached is not None:
                    hit = True
                    result = {"id": str(uuid.uuid4())}
                    result.update(cached)
                    result["cached"] = True
                    session.finish(commit=False)
                else:
                    try:
                        result, duration, cmd_durs, cmd_total = execute(
                            *args, workdir=session.workdir)
                    except BaseException:
                        session.finish(commit=False)
                        raise
                    result["cached"] = False
                    post_hash = session.finish(commit=True)
                    if not session.commits or post_hash == session.pre_hash:
                        CACHE.put(key, result)
            finally:
                if session.workdir is not None:
                    session.finish(commit=False)
            result["environment"] = session.meta()
        except ClientError as e:
            self.close_connection = True if e.status == 413 else self.close_connection
            return self._error(e.status, e.code, e.message)
        except Exception as e:  # unexpected
            return self._error(500, "INTERNAL_ERROR", f"internal error: {e}")
        if hit:
            STATS.hit()
        else:
            STATS.miss()
            STATS.add(duration, cmd_durs, cmd_total)
        self._send(201, result)

    def _create_environment(self):
        try:
            body = self._json_body()
            name, mode, files, originals = validate_environment_create(body)
            ENVS.create(name, mode, files)
        except ClientError as e:
            self.close_connection = True if e.status == 413 else self.close_connection
            return self._error(e.status, e.code, e.message)
        except Exception as e:  # unexpected
            return self._error(500, "INTERNAL_ERROR", f"internal error: {e}")
        self._send(201, {
            "name": name,
            "concurrency_mode": mode,
            "files": {orig: {"written_bytes": len(files[norm])}
                      for orig, norm in originals.items()},
        })

    def do_GET(self):
        path = self._path()
        if path == "/v1/stats/execution":
            return self._send(200, STATS.snapshot())
        if path in ("/v1/execute", "/v1/environment"):
            return self._error(405, "METHOD_NOT_ALLOWED", "method not allowed")
        return self._error(404, "NOT_FOUND", "not found")

    def _other(self):
        path = self._path()
        if path in ("/v1/execute", "/v1/stats/execution", "/v1/environment"):
            return self._error(405, "METHOD_NOT_ALLOWED", "method not allowed")
        return self._error(404, "NOT_FOUND", "not found")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _other


def main():
    p = argparse.ArgumentParser(description="Local execution server")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--address", type=str, default="0.0.0.0")
    a = p.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((a.address, a.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
