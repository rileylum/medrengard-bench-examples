#!/usr/bin/env python3
"""TextDrop: a small self-hosted text sharing service (stdlib only)."""
import argparse
import html
import json
import mimetypes
import os
import re
import secrets
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_STATIC = os.path.join(BASE_DIR, "static")
BUNDLED_DOCS = os.path.join(BASE_DIR, "docs")
BUNDLED_TEMPLATES = os.path.join(BASE_DIR, "templates")

DEFAULT_TEMPLATE = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>{{title}}</title>\n"
    '{{meta}}<link rel="stylesheet" href="/static/style.css">\n'
    "</head>\n<body>\n{{body}}\n</body>\n</html>\n"
)

ROBOTS_TXT = (
    "User-agent: *\n"
    "Disallow: /n/\n"
    "Disallow: /submit\n"
    "Disallow: /render\n"
    "Disallow: /_health\n"
    "Disallow: /_ops/\n"
    "Allow: /\n"
)

MIME_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".map": "application/json",
    ".pdf": "application/pdf",
}


def mime_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in MIME_TYPES:
        return MIME_TYPES[ext]
    guessed = mimetypes.guess_type(name)[0]
    return guessed or "application/octet-stream"


def _read_file(path, binary=False):
    """Return file contents, or None if it is missing or not a regular file."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if binary:
        return data
    return data.decode("utf-8", errors="replace")


def _scan_files(root, suffix=None):
    """Return relative POSIX paths of regular files under root."""
    found = []
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if suffix and not fn.endswith(suffix):
                continue
            full = os.path.join(dirpath, fn)
            if not os.path.isfile(full):
                continue
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return found


def _safe_rel(name):
    """Validate a relative static path; return it or None."""
    if not name or name.endswith("/") or "\\" in name or "\x00" in name:
        return None
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return None
    return "/".join(parts)


class Resources:
    """Resolves templates, static assets and docs from bundled or debug sources."""

    def __init__(self, debug=False, cwd=None):
        self.debug = debug
        if debug:
            base = os.path.abspath(cwd or os.getcwd())
            self.static_dir = os.path.join(base, "static")
            self.docs_dir = os.path.join(base, "docs")
            self.templates_dir = os.path.join(base, "templates")
        else:
            self.static_dir = BUNDLED_STATIC
            self.docs_dir = BUNDLED_DOCS
            self.templates_dir = BUNDLED_TEMPLATES
        # Documentation routes are fixed for the lifetime of the process.
        self.doc_routes = {}
        for rel in _scan_files(self.docs_dir, ".md"):
            route = "/" + rel[:-3]
            if route == "/" or route in self.doc_routes:
                continue
            self.doc_routes[route] = rel
        self._static_cache = {}
        self._doc_cache = {}
        self._template_cache = None
        if not debug:
            for rel in _scan_files(self.static_dir):
                data = _read_file(os.path.join(self.static_dir, rel), binary=True)
                if data is not None:
                    self._static_cache[rel] = data
            tpl = _read_file(os.path.join(self.templates_dir, "page.html"))
            self._template_cache = tpl if tpl else DEFAULT_TEMPLATE
            for route, rel in self.doc_routes.items():
                text = _read_file(os.path.join(self.docs_dir, rel))
                if text is not None:
                    self._doc_cache[route] = render_document(text)

    def template(self):
        if not self.debug:
            return self._template_cache
        tpl = _read_file(os.path.join(self.templates_dir, "page.html"))
        return tpl if tpl else DEFAULT_TEMPLATE

    def static(self, name):
        rel = _safe_rel(name)
        if rel is None:
            return None
        if not self.debug:
            return self._static_cache.get(rel)
        return _read_file(os.path.join(self.static_dir, *rel.split("/")), binary=True)

    def doc(self, route):
        """Return (html, title, preview) for a doc route, or None."""
        rel = self.doc_routes.get(route)
        if rel is None:
            return None
        if not self.debug:
            return self._doc_cache.get(route)
        text = _read_file(os.path.join(self.docs_dir, *rel.split("/")))
        if text is None:
            return None
        return render_document(text)


RESOURCES = Resources(debug=False)

_TPL_RE = re.compile(r"\{\{(title|meta|body)\}\}")


def page(title, body, og=None):
    og_tags = ""
    if og is not None:
        og_tags = (
            f'<meta property="og:title" content="{html.escape(og[0], quote=True)}">\n'
            f'<meta property="og:description" content="{html.escape(og[1], quote=True)}">\n'
        )
    values = {"title": html.escape(title), "meta": og_tags, "body": body}
    return _TPL_RE.sub(lambda m: values[m.group(1)], RESOURCES.template())


def editor_page():
    return page(
    "TextDrop",
    "<h1>TextDrop</h1>\n"
    '<form method="post" action="/submit" enctype="application/x-www-form-urlencoded">\n'
    '<p><label for="content">Text</label></p>\n'
    '<textarea id="content" name="content" required></textarea>\n'
    '<p><label for="mode">Format</label> <select id="mode" name="mode">'
    '<option value="">Plain text</option><option value="markdown">Markdown</option>'
    '</select></p>\n'
    '<p><button type="submit">Submit</button></p>\n'
    "</form>\n"
    '<p><a href="/about">About</a> | <a href="/help/formatting">Formatting help</a></p>',
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


def render_doc_page(doc):
    rendered, title, preview = doc
    page_title = title if title else "TextDrop"
    body = (
        '<nav><p><a href="/">TextDrop</a> | <a href="/about">About</a> | '
        '<a href="/help/formatting">Formatting help</a></p></nav>\n'
        f'<article class="markdown">\n{rendered}\n</article>'
    )
    return page(page_title, body, (page_title, preview))


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
        return unquote(urlsplit(self.path).path)

    def _route_kind(self, path):
        if path in ("/", "/submit", "/health", "/robots.txt", "/favicon.ico"):
            return path
        if path in RESOURCES.doc_routes:
            return "doc"
        if path.startswith("/static/") and RESOURCES.static(path[len("/static/"):]) is not None:
            return "static"
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
            return self._html(200, editor_page())
        if path == "/health":
            body = json.dumps({
                "status": "active",
                "notes": STORE.count(),
                "uptime_seconds": round(time.time() - START_TIME, 3),
            })
            return self._send(200, body, "application/json")
        if path == "/robots.txt":
            return self._send(200, ROBOTS_TXT, "text/plain; charset=utf-8")
        if path == "/favicon.ico":
            data = RESOURCES.static("favicon.ico")
            if data is None:
                return self._not_found()
            return self._send(200, data, "image/x-icon")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            data = RESOURCES.static(name)
            if data is None:
                return self._not_found()
            return self._send(200, data, mime_type(name))
        if path in RESOURCES.doc_routes:
            doc = RESOURCES.doc(path)
            if doc is None:
                return self._not_found()
            return self._html(200, render_doc_page(doc))
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
    parser.add_argument("--debug", action="store_true",
                        help="load templates, static assets and docs from the working directory")
    args = parser.parse_args(argv)

    global RESOURCES
    RESOURCES = Resources(debug=args.debug)

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
