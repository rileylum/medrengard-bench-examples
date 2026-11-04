#!/usr/bin/env python3
"""TextDrop: a small self-hosted text sharing service (stdlib only)."""
import argparse
import html
import json
import os
import re
import secrets
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from textdrop_markdown import render_document

ALPHABET = string.ascii_letters + string.digits
ID_LENGTH = 8
NOTE_RE = re.compile(r"^/n/([A-Za-z0-9]{4,12})$")
NOTE_TEXT_RE = re.compile(r"^/n/([A-Za-z0-9]{4,12})/text$")
MAX_BODY = 10 * 1024 * 1024


class NoteStore:
    def __init__(self):
        self._notes = {}
        self._lock = threading.Lock()

    def add(self, content, mode=""):
        with self._lock:
            while True:
                nid = "".join(secrets.choice(ALPHABET) for _ in range(ID_LENGTH))
                if nid not in self._notes:
                    self._notes[nid] = (content, mode)
                    return nid

    def get(self, nid):
        with self._lock:
            entry = self._notes.get(nid)
            return entry[0] if entry else None

    def get_entry(self, nid):
        with self._lock:
            return self._notes.get(nid)

    def count(self):
        with self._lock:
            return len(self._notes)


STORE = NoteStore()
START_TIME = time.time()


def page(title, body, og=None):
    og_tags = ""
    if og is not None:
        og_tags = (
            f'<meta property="og:title" content="{html.escape(og[0], quote=True)}">\n'
            f'<meta property="og:description" content="{html.escape(og[1], quote=True)}">\n'
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"{og_tags}"
        "<style>body{font-family:sans-serif;max-width:60em;margin:2em auto;padding:0 1em}"
        "textarea{width:100%;min-height:20em;font-family:monospace}"
        "pre{background:#f4f4f4;padding:1em;overflow:auto;white-space:pre-wrap}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


EDITOR_PAGE = page(
    "TextDrop",
    "<h1>TextDrop</h1>\n"
    '<form method="post" action="/submit" enctype="application/x-www-form-urlencoded">\n'
    '<p><label for="content">Text</label></p>\n'
    '<textarea id="content" name="content" required></textarea>\n'
    '<p><label for="mode">Format</label> <select id="mode" name="mode">'
    '<option value="">Plain text</option><option value="markdown">Markdown</option>'
    '</select></p>\n'
    '<p><button type="submit">Submit</button></p>\n'
    "</form>",
)


def error_page(code, title, message):
    return page(
        f"{code} {title}",
        f"<h1>{code} {html.escape(title)}</h1>\n<p>{html.escape(message)}</p>\n"
        '<p><a href="/">Back to editor</a></p>',
    )


def _valid_utf8(s):
    """Reject strings containing lone surrogates (e.g. from JSON \\ud800 escapes)."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def render_note_page(content, mode, nid=None):
    """Render a full HTML page for content. nid=None means ephemeral preview."""
    if mode == "markdown":
        rendered, title, preview = render_document(content)
    else:
        rendered, title, preview = None, None, ""
    og_title = title if title else "TextDrop"
    page_title = og_title if nid is not None else f"{og_title} - Preview"
    if nid is not None:
        links = (f'<a href="/n/{nid}/text">Raw text</a> | <a href="/">New note</a>')
        label = f"Note {html.escape(nid)}"
    else:
        links = '<a href="/">New note</a>'
        label = "Preview"
    if rendered is not None:
        body = (
            f"<nav><p><strong>{label}</strong> | {links}</p></nav>\n"
            f'<article class="markdown">\n{rendered}\n</article>'
        )
    else:
        body = (
            f"<h1>{label}</h1>\n"
            f"<p>{links}</p>\n"
            f"<pre><code>{html.escape(content, quote=True)}</code></pre>"
        )
    return page(page_title, body, (og_title, preview))


class Handler(BaseHTTPRequestHandler):
    server_version = "TextDrop/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -------------------------------------------------------
    def _send(self, code, body, ctype, headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _html(self, code, body, headers=None):
        self._send(code, body, "text/html; charset=utf-8", headers)

    def _error(self, code, title, message, headers=None):
        self._html(code, error_page(code, title, message), headers)

    def _not_found(self):
        self._error(404, "Not Found", "The page you requested does not exist.")

    def _path(self):
        return urlsplit(self.path).path

    def _route_kind(self, path):
        if path in ("/", "/submit", "/health"):
            return path
        if NOTE_RE.match(path):
            return "note"
        if NOTE_TEXT_RE.match(path):
            return "text"
        return None

    def _method_not_allowed(self, allow):
        self._error(405, "Method Not Allowed",
                    "This method is not supported for this page.",
                    {"Allow": allow})

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        path = self._path()
        if path in ("/", "/submit"):
            return self._html(200, EDITOR_PAGE)
        if path == "/health":
            body = json.dumps({
                "status": "active",
                "notes": STORE.count(),
                "uptime_seconds": round(time.time() - START_TIME, 3),
            })
            return self._send(200, body, "application/json")
        m = NOTE_RE.match(path)
        if m:
            entry = STORE.get_entry(m.group(1))
            if entry is None:
                return self._error(404, "Not Found", "That note does not exist.")
            content, mode = entry
            return self._html(200, render_note_page(content, mode, m.group(1)))
        m = NOTE_TEXT_RE.match(path)
        if m:
            content = STORE.get(m.group(1))
            if content is None:
                return self._error(404, "Not Found", "That note does not exist.")
            return self._send(200, content, "text/plain; charset=utf-8")
        return self._not_found()

    def do_HEAD(self):
        self.do_GET()

    # -- POST ----------------------------------------------------------
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        path = self._path()
        if path not in ("/submit", "/render"):
            raw = self._read_body()  # drain
            kind = self._route_kind(path)
            if kind is None:
                return self._not_found()
            return self._method_not_allowed("GET, HEAD")
        parsed = self._parse_submission()
        if parsed is None:
            return
        content, mode = parsed
        if path == "/render":
            return self._html(200, render_note_page(content, mode, None))
        nid = STORE.add(content, mode)
        loc = f"/n/{nid}"
        self._html(302, page("Redirecting", f'<p>Redirecting to <a href="{loc}">{loc}</a>.</p>'),
                   {"Location": loc})

    def _parse_submission(self):
        """Return (content, mode), or None after sending an error response."""
        raw = self._read_body()
        if raw is None:
            self.close_connection = True
            return self._error(413, "Payload Too Large", "The submitted text is too large.")
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype not in ("application/x-www-form-urlencoded", "application/json"):
            return self._error(400, "Bad Request",
                               "Unsupported Content-Type. Submit form data or JSON.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(400, "Bad Request", "The submission is not valid UTF-8.")
        if ctype == "application/json":
            try:
                data = json.loads(text) if text.strip() else None
            except ValueError:
                return self._error(400, "Bad Request", "The submission is not valid JSON.")
            if not isinstance(data, dict):
                return self._error(400, "Bad Request",
                                   "The JSON submission must be an object.")
            content = data.get("body", "")
            mode = data.get("mode", "")
            if content is None:
                content = ""
            if mode is None:
                mode = ""
            if not isinstance(content, str):
                return self._error(400, "Bad Request", "The body field must be a string.")
            if not isinstance(mode, str):
                return self._error(400, "Bad Request", "Unsupported rendering mode.")
        else:
            try:
                fields = parse_qs(text, keep_blank_values=True, encoding="utf-8",
                                  errors="strict")
            except UnicodeDecodeError:
                return self._error(400, "Bad Request", "The submission is not valid UTF-8.")
            mode = fields.get("mode", [""])[0]
            if "content" in fields:
                content = fields["content"][0]
            else:
                content = fields.get("body", [""])[0]
        if not _valid_utf8(content) or not _valid_utf8(mode):
            return self._error(400, "Bad Request", "The submission is not valid UTF-8.")
        if mode not in ("", "markdown"):
            return self._error(400, "Bad Request",
                               "Unsupported rendering mode. Use plain text or markdown.")
        if not content.strip():
            return self._error(400, "Bad Request", "Content must not be empty.")
        return content, mode

    def _other(self):
        path = self._path()
        self._read_body()
        kind = self._route_kind(path)
        if kind is None:
            return self._not_found()
        allow = "GET, HEAD, POST" if kind == "/submit" else "GET, HEAD"
        return self._method_not_allowed(allow)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other


def main(argv=None):
    parser = argparse.ArgumentParser(description="TextDrop text sharing service")
    parser.add_argument("--host", default=None, help="address to bind (default: all interfaces)")
    parser.add_argument("--port", type=int, default=None, help="port to listen on (default: 8080)")
    args = parser.parse_args(argv)

    host = args.host if args.host is not None else os.environ.get("TEXTDROP_HOST", "")
    if args.port is not None:
        port = args.port
    else:
        env_port = os.environ.get("TEXTDROP_PORT", "").strip()
        try:
            port = int(env_port) if env_port else 8080
        except ValueError:
            parser.error(f"invalid TEXTDROP_PORT: {env_port!r}")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"TextDrop listening on {host or '0.0.0.0'}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
