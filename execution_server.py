#!/usr/bin/env python3
"""Tiny local runner HTTP API: POST /v1/execute, GET /v1/stats/execution."""
import argparse
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


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._durations = []

    def record(self, duration):
        with self._lock:
            self._durations.append(duration)

    def snapshot(self):
        with self._lock:
            d = list(self._durations)
        if not d:
            return {"ran": 0, "duration": {"average": None, "median": None,
                                           "max": None, "min": None, "stddev": None}}
        return {
            "ran": len(d),
            "duration": {
                "average": round(sum(d) / len(d), 3),
                "median": round(statistics.median(d), 3),
                "max": round(max(d), 3),
                "min": round(min(d), 3),
                "stddev": round(statistics.pstdev(d), 3),
            },
        }


STATS = Stats()


def validate(body):
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")

    if "command" not in body:
        raise ApiError(400, "MISSING_FIELD", "'command' is required")
    command = body["command"]
    if not isinstance(command, str):
        raise ApiError(400, "INVALID_FIELD", "'command' must be a string")
    if not command.strip():
        raise ApiError(400, "INVALID_FIELD", "'command' must be a non-empty string")

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
    for path, content in files.items():
        if not isinstance(content, str):
            raise ApiError(400, "INVALID_FIELD", f"content for file {path!r} must be a string")
        if (not path or "\x00" in path or os.path.isabs(path)
                or any(p == ".." for p in path.replace("\\", "/").split("/"))):
            raise ApiError(400, "INVALID_FIELD", f"invalid file path: {path!r}")

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

    return command, env, files, stdin, float(timeout)


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def execute(command, env, files, stdin, timeout):
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
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(content)
            except (IsADirectoryError, NotADirectoryError, FileExistsError) as e:
                raise ApiError(400, "INVALID_FIELD", f"cannot create file {rel!r}: {e}")

        run_env = dict(os.environ)
        run_env.update(env)

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
        return {
            "id": str(uuid.uuid4()),
            "stdout": (out or b"").decode("utf-8", errors="replace"),
            "stderr": (err or b"").decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "duration": round(duration, 3),
            "timed_out": timed_out,
        }, duration
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
            result, duration = execute(*args)
            STATS.record(round(duration, 3))
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
