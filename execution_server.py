#!/usr/bin/env python3
"""Tiny local runner HTTP API (stdlib only)."""
import argparse
import glob
import json
import math
import os
import shutil
import signal
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


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._durations = []

    def add(self, d):
        with self._lock:
            self._durations.append(d)

    def snapshot(self):
        with self._lock:
            ds = list(self._durations)
        if not ds:
            return {"ran": 0, "duration": {"average": None, "median": None,
                                           "max": None, "min": None, "stddev": None}}
        return {
            "ran": len(ds),
            "duration": {
                "average": round(statistics.fmean(ds), 3),
                "median": round(statistics.median(ds), 3),
                "max": round(max(ds), 3),
                "min": round(min(ds), 3),
                "stddev": round(statistics.pstdev(ds), 3),
            },
        }


STATS = Stats()


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(body):
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object", "INVALID_REQUEST")

    if "command" not in body or body["command"] is None:
        raise ClientError("'command' is required", "MISSING_FIELD")
    command = body["command"]
    if not isinstance(command, str):
        raise ClientError("'command' must be a string", "INVALID_FIELD")
    if not command.strip():
        raise ClientError("'command' must be a non-empty string", "INVALID_FIELD")
    if "\x00" in command:
        raise ClientError("'command' must not contain NUL bytes", "INVALID_FIELD")

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

    files = body.get("files")
    if files is None:
        files = {}
    if not isinstance(files, dict):
        raise ClientError("'files' must be an object of path to string", "INVALID_FIELD")
    clean_files = {}
    for path, content in files.items():
        if not isinstance(content, str):
            raise ClientError(f"content of file {path!r} must be a string", "INVALID_FIELD")
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
        clean_files[norm] = content

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

    return command, env, clean_files, stdin, float(timeout), track


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


def execute(command, env, files, stdin, timeout, track=None):
    workdir = tempfile.mkdtemp(prefix="run-")
    try:
        for rel, content in files.items():
            full = os.path.join(workdir, rel)
            d = os.path.dirname(full)
            try:
                os.makedirs(d, exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content)
            except (IsADirectoryError, NotADirectoryError, FileExistsError) as e:
                raise ClientError(f"cannot create file {rel!r}: {e.strerror}", "INVALID_FIELD")

        run_env = dict(os.environ)
        run_env.update(env)

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

        result = {
            "id": str(uuid.uuid4()),
            "stdout": (out or b"").decode("utf-8", errors="replace"),
            "stderr": (err or b"").decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "duration": round(duration, 3),
            "timed_out": timed_out,
        }
        if track:
            result["files"] = collect_tracked(workdir, track)
        return result, duration
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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

    def do_POST(self):
        path = self._path()
        if path != "/v1/execute":
            if path == "/v1/stats/execution":
                return self._error(405, "METHOD_NOT_ALLOWED", "method not allowed")
            return self._error(404, "NOT_FOUND", "not found")
        try:
            raw = self._read_body()
            if not raw.strip():
                raise ClientError("request body is required", "INVALID_JSON")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ClientError("malformed JSON body", "INVALID_JSON")
            args = validate(body)
            result, duration = execute(*args)
        except ClientError as e:
            self.close_connection = True if e.status == 413 else self.close_connection
            return self._error(e.status, e.code, e.message)
        except Exception as e:  # unexpected
            return self._error(500, "INTERNAL_ERROR", f"internal error: {e}")
        STATS.add(duration)
        self._send(201, result)

    def do_GET(self):
        path = self._path()
        if path == "/v1/stats/execution":
            return self._send(200, STATS.snapshot())
        if path == "/v1/execute":
            return self._error(405, "METHOD_NOT_ALLOWED", "method not allowed")
        return self._error(404, "NOT_FOUND", "not found")

    def _other(self):
        path = self._path()
        if path in ("/v1/execute", "/v1/stats/execution"):
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
