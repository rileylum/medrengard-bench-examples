#!/usr/bin/env python3
"""Tiny local runner HTTP API: POST /v1/execute, GET /v1/stats/execution."""
import argparse
import bz2
import csv
import glob
import gzip
import io
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_TIMEOUT = 10

try:  # optional; JSON is valid YAML so we can fall back to it
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

COMPRESSION_EXTS = {".gz": "gzip", ".bz2": "bz2"}
STRUCTURED_EXTS = {".json", ".yaml", ".yml", ".jsonl", ".ndjson", ".csv", ".tsv"}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _summary(d):
    if not d:
        return {"average": None, "median": None, "max": None, "min": None, "stddev": None}
    return {
        "average": round(sum(d) / len(d), 3),
        "median": round(statistics.median(d), 3),
        "max": round(max(d), 3),
        "min": round(min(d), 3),
        "stddev": round(statistics.pstdev(d), 3),
    }


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._durations = []
        self._cmd_durations = []
        self._cmd_total = 0

    def record(self, duration, cmd_durations=None, cmd_total=None):
        with self._lock:
            self._durations.append(duration)
            if cmd_durations is None:
                cmd_durations = [duration]
            if cmd_total is None:
                cmd_total = len(cmd_durations)
            self._cmd_durations.extend(cmd_durations)
            self._cmd_total += cmd_total

    def snapshot(self):
        with self._lock:
            d = list(self._durations)
            cd = list(self._cmd_durations)
            total = self._cmd_total
        n = len(d)
        return {
            "ran": n,
            "commands": {
                "total": total,
                "ran": len(cd),
                "average": round(total / n, 3) if n else None,
                "average_ran": round(len(cd) / n, 3) if n else None,
                "duration": _summary(cd),
            },
            "duration": _summary(d),
        }


STATS = Stats()


def validate(body):
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")

    if "command" not in body:
        raise ApiError(400, "MISSING_FIELD", "'command' is required")
    command = body["command"]
    if isinstance(command, str):
        if not command.strip():
            raise ApiError(400, "INVALID_FIELD", "'command' must be a non-empty string")
    elif isinstance(command, list):
        if not command:
            raise ApiError(400, "INVALID_FIELD", "'command' array must not be empty")
    else:
        raise ApiError(400, "INVALID_FIELD",
                       "'command' must be a string or an array of command objects")

    env = body.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise ApiError(400, "INVALID_FIELD", "'env' must be an object of strings")
    for k, v in env.items():
        if not isinstance(k, str) or not k or "=" in k or "\x00" in k:
            raise ApiError(400, "INVALID_FIELD", f"invalid env variable name: {k!r}")
        if not isinstance(v, str) or "\x00" in v:
            raise ApiError(400, "INVALID_FIELD", f"env value for {k!r} must be a string")

    files = body.get("files", {})
    if files is None:
        files = {}
    if not isinstance(files, dict):
        raise ApiError(400, "INVALID_FIELD", "'files' must be an object of strings")
    encoded_files = {}
    for path, content in files.items():
        if (not path or "\x00" in path or os.path.isabs(path)
                or any(p == ".." for p in path.replace("\\", "/").split("/"))):
            raise ApiError(400, "INVALID_FIELD", f"invalid file path: {path!r}")
        encoded_files[path] = encode_file(path, content)
    files = encoded_files

    stdin = body.get("stdin", "")
    if stdin is None:
        stdin = ""
    if isinstance(stdin, list):
        if not all(isinstance(s, str) for s in stdin):
            raise ApiError(400, "INVALID_FIELD", "'stdin' array must contain only strings")
        stdin = "\n".join(stdin)
    elif not isinstance(stdin, str):
        raise ApiError(400, "INVALID_FIELD", "'stdin' must be a string or array of strings")

    timeout = body.get("timeout", DEFAULT_TIMEOUT)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ApiError(400, "INVALID_FIELD", "'timeout' must be a number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ApiError(400, "INVALID_FIELD", "'timeout' must be a positive number")

    continue_on_error = body.get("continue_on_error", False)
    if continue_on_error is None:
        continue_on_error = False
    if not isinstance(continue_on_error, bool):
        raise ApiError(400, "INVALID_FIELD", "'continue_on_error' must be a boolean")

    if isinstance(command, list):
        steps = []
        for i, item in enumerate(command):
            if not isinstance(item, dict):
                raise ApiError(400, "INVALID_FIELD", f"command[{i}] must be an object")
            if "cmd" not in item:
                raise ApiError(400, "MISSING_FIELD", f"command[{i}].cmd is required")
            cmd = item["cmd"]
            if not isinstance(cmd, str) or not cmd.strip():
                raise ApiError(400, "INVALID_FIELD",
                               f"command[{i}].cmd must be a non-empty string")
            t = item.get("timeout")
            if t is None:
                t = timeout
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                raise ApiError(400, "INVALID_FIELD", f"command[{i}].timeout must be a number")
            if not math.isfinite(t) or t <= 0:
                raise ApiError(400, "INVALID_FIELD",
                               f"command[{i}].timeout must be a positive number")
            req = item.get("required", False)
            if req is None:
                req = False
            if not isinstance(req, bool):
                raise ApiError(400, "INVALID_FIELD", f"command[{i}].required must be a boolean")
            steps.append({"cmd": cmd, "timeout": float(t), "required": req})
        command = steps

    track = body.get("track")
    if track is not None:
        if not isinstance(track, list):
            raise ApiError(400, "INVALID_FIELD", "'track' must be an array of glob strings")
        for pat in track:
            if not isinstance(pat, str) or not pat.strip() or "\x00" in pat:
                raise ApiError(400, "INVALID_FIELD", f"invalid track pattern: {pat!r}")
            if pat.startswith("/") or any(seg == ".." for seg in pat.split("/")):
                raise ApiError(400, "INVALID_FIELD",
                               f"track pattern must be relative to the working directory: {pat!r}")
        if not track:
            track = None

    return command, env, files, stdin, float(timeout), track, continue_on_error


def _split_exts(path):
    """Return (base_ext, compression) for a file path; 400 on multiple compressions."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    parts = name.split(".")
    suffixes = ["." + p for p in parts[1:]] if len(parts) > 1 else []
    if parts and parts[0] == "" and len(parts) == 2:
        # dotfile like ".gz" or ".json" has no extension
        suffixes = []
    comps = [s for s in suffixes if s in COMPRESSION_EXTS]
    if len(comps) > 1:
        raise ApiError(400, "INVALID_FIELD",
                       f"multiple compression extensions in file path: {path!r}")
    compression = None
    if suffixes and suffixes[-1] in COMPRESSION_EXTS:
        compression = COMPRESSION_EXTS[suffixes[-1]]
        suffixes = suffixes[:-1]
    base = suffixes[-1] if suffixes else ""
    return base, compression


def _json_dump(value):
    return json.dumps(value, ensure_ascii=False)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return repr(value)
    return _json_dump(value)


def _table_rows(path, value):
    """Return (header, rows) for a dict-of-columns or list-of-rows value."""
    if value is None or value == "" or value == [] or value == {}:
        return [], []
    if isinstance(value, dict):
        columns = sorted(value.keys())
        cols = []
        for c in columns:
            v = value[c]
            cols.append(list(v) if isinstance(v, list) else [v])
        n = max((len(c) for c in cols), default=0)
        rows = [[_cell(col[i]) if i < len(col) else "" for col in cols] for i in range(n)]
        return columns, rows
    if isinstance(value, list):
        if all(isinstance(r, dict) for r in value):
            columns = sorted({k for r in value for k in r.keys()})
            rows = [[_cell(r[c]) if c in r else "" for c in columns] for r in value]
            return columns, rows
        if all(isinstance(r, list) for r in value):
            return None, [[_cell(x) for x in r] for r in value]
        raise ApiError(400, "INVALID_FIELD",
                       f"content for file {path!r} must be a list of rows or a dict of columns")
    raise ApiError(400, "INVALID_FIELD",
                   f"content for file {path!r} must be a list of rows or a dict of columns")


def _serialize_table(path, value, delimiter):
    header, rows = _table_rows(path, value)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    if header is not None:
        writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def _serialize(path, base, content):
    if isinstance(content, str):
        if content == "" and base in (".csv", ".tsv"):
            return _serialize_table(path, content, "," if base == ".csv" else "\t")
        return content
    if base == ".json":
        return _json_dump(content) + "\n"
    if base in (".yaml", ".yml"):
        if yaml is not None:
            return yaml.safe_dump(content, sort_keys=False, default_flow_style=False,
                                  allow_unicode=True)
        return _json_dump(content) + "\n"
    if base in (".jsonl", ".ndjson"):
        items = content if isinstance(content, list) else [content]
        return "".join(_json_dump(item) + "\n" for item in items)
    if base == ".csv":
        return _serialize_table(path, content, ",")
    if base == ".tsv":
        return _serialize_table(path, content, "\t")
    # unknown extension with a structured value: write its JSON text
    return _json_dump(content) + "\n"


def encode_file(path, content):
    """Turn a request file value into the bytes written to disk."""
    base, compression = _split_exts(path)
    data = _serialize(path, base, content).encode("utf-8")
    if compression == "gzip":
        data = gzip.compress(data, mtime=0)
    elif compression == "bz2":
        data = bz2.compress(data)
    return data


def collect_tracked(workdir, patterns):
    real_root = os.path.realpath(workdir)
    result = {}
    for pat in patterns:
        try:
            matches = glob.glob(pat, root_dir=workdir, recursive=True)
        except (OSError, ValueError):
            continue
        for rel in matches:
            full = os.path.join(workdir, rel)
            real = os.path.realpath(full)
            if not real.startswith(real_root + os.sep) or not os.path.isfile(real):
                continue
            key = os.path.normpath(rel).replace(os.sep, "/")
            if key in result:
                continue
            try:
                with open(real, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            result[key] = data.decode("utf-8", errors="replace")
    return dict(sorted(result.items()))


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_one(command, workdir, run_env, stdin, timeout):
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
    timed_out = False
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(proc)
        try:
            out, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # grandchildren may hold pipes open; give up on further output
            for f in (proc.stdout, proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass
            proc.wait()
            out, err = b"", b""
    duration = time.monotonic() - start
    # make sure no stragglers remain in the group
    kill_group(proc)

    exit_code = -1 if timed_out else proc.returncode
    result = {
        "stdout": (out or b"").decode("utf-8", errors="replace"),
        "stderr": (err or b"").decode("utf-8", errors="replace"),
        "exit_code": exit_code,
        "duration": round(duration, 3),
        "timed_out": timed_out,
    }
    return result, duration


def execute(command, env, files, stdin, timeout, track=None, continue_on_error=False):
    """Run a command (string) or chain (list of steps).

    Returns (response, total_duration, per_command_durations, total_commands).
    """
    workdir = tempfile.mkdtemp(prefix="exec-")
    try:
        real_root = os.path.realpath(workdir)
        for rel, content in files.items():
            target = os.path.realpath(os.path.join(workdir, rel))
            if not target.startswith(real_root + os.sep):
                raise ApiError(400, "INVALID_FIELD", f"invalid file path: {rel!r}")
            parent = os.path.dirname(target)
            try:
                os.makedirs(parent, exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(content)
            except (IsADirectoryError, NotADirectoryError, FileExistsError) as e:
                raise ApiError(400, "INVALID_FIELD", f"cannot create file {rel!r}: {e}")

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
        for step in command:
            if failed and not continue_on_error and not step["required"]:
                continue
            res, duration = run_one(step["cmd"], workdir, run_env,
                                    stdin if first_stdin else "", step["timeout"])
            first_stdin = False
            entry = {"cmd": step["cmd"]}
            entry.update(res)
            if step["required"]:
                entry["required"] = True
            results.append(entry)
            durations.append(duration)
            if res["exit_code"] != 0 or res["timed_out"]:
                failed = True

        overall_exit = next((r["exit_code"] for r in results if r["exit_code"] != 0), 0)
        total = sum(durations)
        result = {
            "id": str(uuid.uuid4()),
            "commands": results,
            "exit_code": overall_exit,
            "duration": round(total, 3),
            "timed_out": any(r["timed_out"] for r in results),
            "files": collect_tracked(workdir, track) if track else {},
        }
        return result, total, durations, len(command)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "execution_server/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
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
        try:
            n = int(length)
        except ValueError:
            raise ApiError(400, "INVALID_REQUEST", "invalid Content-Length")
        if n < 0:
            raise ApiError(400, "INVALID_REQUEST", "invalid Content-Length")
        return self.rfile.read(n)

    def _dispatch(self, method):
        try:
            path = self._path()
            routes = {"/v1/execute": "POST", "/v1/stats/execution": "GET"}
            if path not in routes:
                # drain body to keep connection sane
                if method in ("POST", "PUT", "PATCH"):
                    self._read_body()
                raise ApiError(404, "NOT_FOUND", f"no route for {path}")
            if routes[path] != method:
                if method in ("POST", "PUT", "PATCH"):
                    self._read_body()
                raise ApiError(405, "METHOD_NOT_ALLOWED",
                               f"{method} not allowed on {path}")
            if path == "/v1/stats/execution":
                self._send(200, STATS.snapshot())
                return
            raw = self._read_body()
            if not raw.strip():
                raise ApiError(400, "INVALID_JSON", "request body is empty")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ApiError(400, "INVALID_JSON", "request body is not valid JSON")
            args = validate(body)
            result, duration, cmd_durations, cmd_total = execute(*args)
            STATS.record(round(duration, 3), [round(x, 3) for x in cmd_durations], cmd_total)
            self._send(201, result)
        except ApiError as e:
            self._error(e.status, e.code, e.message)
        except Exception as e:  # pragma: no cover
            try:
                self._error(500, "INTERNAL_ERROR", f"internal server error: {e}")
            except Exception:
                pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local execution server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--address", type=str, default="0.0.0.0")
    args = parser.parse_args(argv)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.address, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
