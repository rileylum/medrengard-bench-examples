#!/usr/bin/env python3
"""TextDrop - self-hosted text sharing service."""
import argparse
import html
import json
import os
import re
import secrets
import sqlite3
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ALPHABET = string.ascii_letters + string.digits
ID_LEN = 8
NOTE_RE = re.compile(r"^/n/([A-Za-z0-9]{4,12})$")
NOTE_TEXT_RE = re.compile(r"^/n/([A-Za-z0-9]{4,12})/text$")
MAX_BODY = 10 * 1024 * 1024
START_TIME = time.time()


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, content TEXT NOT NULL, "
                "mode TEXT NOT NULL DEFAULT '', created REAL NOT NULL)"
            )
            self.conn.commit()

    def create(self, content, mode=""):
        with self.lock:
            while True:
                nid = "".join(secrets.choice(ALPHABET) for _ in range(ID_LEN))
                try:
                    self.conn.execute(
                        "INSERT INTO notes (id, content, mode, created) VALUES (?, ?, ?, ?)",
                        (nid, content, mode, time.time()),
                    )
                    self.conn.commit()
                    return nid
                except sqlite3.IntegrityError:
                    continue

    def get(self, nid):
        with self.lock:
            row = self.conn.execute("SELECT content FROM notes WHERE id = ?", (nid,)).fetchone()
        return row[0] if row else None

    def count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]


STORE = None


def page(title, body):
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)} - TextDrop</title>\n"
        "<style>body{font-family:sans-serif;max-width:900px;margin:2em auto;padding:0 1em}"
        "textarea{width:100%;min-height:20em;font-family:monospace}"
        "pre{background:#f4f4f4;padding:1em;overflow:auto;white-space:pre-wrap}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n</body>\n</html>\n"
    )


EDITOR = page(
    "New note",
    "<h1>TextDrop</h1>\n"
    "<form method=\"post\" action=\"/submit\" enctype=\"application/x-www-form-urlencoded\">\n"
    "<p><textarea name=\"content\" placeholder=\"Paste or type your text here\" required></textarea></p>\n"
    "<input type=\"hidden\" name=\"mode\" value=\"\">\n"
    "<p><button type=\"submit\">Save</button></p>\n"
    "</form>",
)


def status_page(code, title, message):
    return page(
        f"{code} {title}",
        f"<h1>{code} {html.escape(title)}</h1>\n<p>{html.escape(message)}</p>\n"
        "<p><a href=\"/\">Back to editor</a></p>",
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "TextDrop/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send(self, code, body, ctype, headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_html(self, code, body, headers=None):
        self.send(code, body, "text/html; charset=utf-8", headers)

    def error_page(self, code, title, message, headers=None):
        self.send_html(code, status_page(code, title, message), headers)

    def not_found(self):
        self.error_page(404, "Not Found", "The page you requested could not be found.")

    def path_only(self):
        return urlsplit(self.path).path

    def do_GET(self):
        path = self.path_only()
        if path in ("/", "/submit"):
            return self.send_html(200, EDITOR)
        if path == "/health":
            body = json.dumps({"status": "active", "notes": STORE.count(),
                               "uptime": round(time.time() - START_TIME, 3)})
            return self.send(200, body, "application/json")
        m = NOTE_TEXT_RE.match(path)
        if m:
            content = STORE.get(m.group(1))
            if content is None:
                return self.error_page(404, "Not Found", "That note does not exist.")
            return self.send(200, content, "text/plain; charset=utf-8")
        m = NOTE_RE.match(path)
        if m:
            nid = m.group(1)
            content = STORE.get(nid)
            if content is None:
                return self.error_page(404, "Not Found", "That note does not exist.")
            body = (
                f"<h1>Note {html.escape(nid)}</h1>\n"
                f"<p><a href=\"/n/{nid}/text\">Raw text</a> | <a href=\"/\">New note</a></p>\n"
                f"<pre><code>{html.escape(content, quote=True)}</code></pre>"
            )
            return self.send_html(200, page(f"Note {nid}", body))
        return self.not_found()

    do_HEAD = do_GET

    def read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            n = int(length)
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n)

    def do_POST(self):
        path = self.path_only()
        if path != "/submit":
            if self._known(path):
                self.read_body()
                return self.error_page(405, "Method Not Allowed", "This method is not allowed here.",
                                       {"Allow": "GET, HEAD"})
            self.read_body()
            return self.not_found()
        raw = self.read_body()
        if raw is None:
            self.close_connection = True
            return self.error_page(400, "Bad Request", "Invalid request body.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self.error_page(400, "Bad Request", "Request body must be UTF-8.")
        try:
            fields = parse_qs(text, keep_blank_values=True, encoding="utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError):
            return self.error_page(400, "Bad Request", "Malformed form data.")
        content = fields.get("content", [""])[0]
        mode = fields.get("mode", [""])[0]
        if mode != "":
            return self.error_page(400, "Bad Request", "Unsupported rendering mode.")
        if not content.strip():
            return self.error_page(400, "Bad Request", "Content must not be empty.")
        nid = STORE.create(content, "")
        loc = f"/n/{nid}"
        self.send_html(302, page("Redirect", f"<p>Redirecting to <a href=\"{loc}\">{loc}</a></p>"),
                       {"Location": loc})

    def _known(self, path):
        return path in ("/", "/submit", "/health") or bool(NOTE_RE.match(path) or NOTE_TEXT_RE.match(path))

    def _other(self):
        path = self.path_only()
        self.read_body()
        if self._known(path):
            allow = "GET, HEAD, POST" if path == "/submit" else "GET, HEAD"
            return self.error_page(405, "Method Not Allowed", "This method is not allowed here.",
                                   {"Allow": allow})
        return self.not_found()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other


def main(argv=None):
    p = argparse.ArgumentParser(description="TextDrop text sharing service")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--db", default=None, help="SQLite database path")
    a = p.parse_args(argv)
    host = a.host if a.host is not None else os.environ.get("TEXTDROP_HOST", "")
    port = a.port
    if port is None:
        env = os.environ.get("TEXTDROP_PORT", "").strip()
        port = int(env) if env else 8080
    db = a.db or os.environ.get("TEXTDROP_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "textdrop.db")
    global STORE
    STORE = Store(db)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"TextDrop listening on {host or '0.0.0.0'}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
