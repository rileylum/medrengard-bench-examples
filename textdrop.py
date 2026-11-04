#!/usr/bin/env python3
"""TextDrop - self-hosted text sharing service."""
import argparse
import base64
import hashlib
import hmac
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
from urllib.parse import parse_qs, unquote, urlsplit

ALPHABET = string.ascii_letters + string.digits
ID_LEN = 8
NOTE_RE = re.compile(r"^/n/([A-Za-z0-9]{4,12})$")
NOTE_TEXT_RE = re.compile(r"^/n/([A-Za-z0-9]{4,12})/text$")
MAX_BODY = 10 * 1024 * 1024
START_TIME = time.time()
OPS_SECRET = ""
ID_SEED = ""
DRAINED = False
USER_COOKIE = "user_id"


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _user_sig(seed, uid):
    return _b64u(hmac.new(seed.encode("utf-8"), b"textdrop-user:" + uid.encode("ascii"),
                          hashlib.sha256).digest())


def make_user_token(seed):
    uid = _b64u(secrets.token_bytes(16))
    return f"{uid}.{_user_sig(seed, uid)}"


def valid_user_token(seed, token):
    if not seed or not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    uid, sig = parts
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
        return False
    return hmac.compare_digest(sig.encode("ascii", "replace"), _user_sig(seed, uid).encode("ascii"))


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

    def create(self, content, mode="", user=""):
        with self.lock:
            while True:
                digest = hashlib.sha256(
                    secrets.token_bytes(16) + b"\0" + user.encode("utf-8") + b"\0"
                    + content.encode("utf-8")).digest()
                nid = "".join(ALPHABET[b % len(ALPHABET)] for b in digest[:ID_LEN])
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

    def get_note(self, nid):
        with self.lock:
            row = self.conn.execute("SELECT content, mode FROM notes WHERE id = ?", (nid,)).fetchone()
        return (row[0], row[1] or "") if row else None

    def count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]


STORE = None


# ---------------------------------------------------------------------------
# Resources (bundled or, in debug mode, read from the working directory)
# ---------------------------------------------------------------------------

DEBUG = False
APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TEMPLATE = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>{{ title }}</title>\n{{ head }}"
    "<link rel=\"stylesheet\" href=\"/static/style.css\">\n"
    "</head>\n<body>\n{{ body }}\n</body>\n</html>\n"
)


def _load_tree(sub):
    base = os.path.join(APP_DIR, sub)
    out = {}
    for dirpath, _, files in os.walk(base):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            try:
                with open(full, "rb") as fh:
                    out[rel] = fh.read()
            except OSError:
                pass
    return out


try:
    import textdrop_bundle as _bundle
    BUNDLED_STATIC = dict(_bundle.STATIC)
    BUNDLED_DOCS = dict(_bundle.DOCS)
    BUNDLED_TEMPLATES = dict(_bundle.TEMPLATES)
except ImportError:  # pragma: no cover - fall back to files shipped beside the script
    BUNDLED_STATIC = _load_tree("static")
    BUNDLED_DOCS = _load_tree("docs")
    BUNDLED_TEMPLATES = _load_tree("templates")


def _safe_rel(rel):
    """Validate a relative resource path; return normalized form or None."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return "/".join(parts)


def read_debug_file(sub, rel):
    """Read ./<sub>/<rel> from the working directory; None if missing or not a regular file."""
    rel = _safe_rel(rel)
    if rel is None:
        return None
    base = os.path.realpath(os.path.join(os.getcwd(), sub))
    full = os.path.realpath(os.path.join(base, *rel.split("/")))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return None
    try:
        with open(full, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def get_template(name="page.html"):
    data = None
    if DEBUG:
        data = read_debug_file("templates", name)
    if data is None:
        data = BUNDLED_TEMPLATES.get(name)
    if data is None:
        return DEFAULT_TEMPLATE
    return data.decode("utf-8", errors="replace")


_TPL_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(tpl, values):
    return _TPL_VAR_RE.sub(lambda m: values.get(m.group(1), ""), tpl)


def page(title, body, raw_title=False, head=""):
    full_title = title if raw_title else f"{title} - TextDrop"
    return render_template(get_template(), {
        "title": html.escape(full_title),
        "head": head,
        "body": body,
    })


def editor_page():
    return page(
        "New note",
        "<h1>TextDrop</h1>\n"
        "<form method=\"post\" action=\"/submit\" enctype=\"application/x-www-form-urlencoded\">\n"
        "<p><textarea name=\"content\" placeholder=\"Paste or type your text here\" required></textarea></p>\n"
        "<p><label>Format: <select name=\"mode\"><option value=\"\">Plain text</option>"
        "<option value=\"markdown\">Markdown</option></select></label></p>\n"
        "<p><button type=\"submit\">Save</button></p>\n"
        "</form>\n"
        "<p><a href=\"/about\">About</a> | <a href=\"/help/formatting\">Formatting help</a></p>",
    )


def status_page(code, title, message):
    return page(
        f"{code} {title}",
        f"<h1>{code} {html.escape(title)}</h1>\n<p>{html.escape(message)}</p>\n"
        "<p><a href=\"/\">Back to editor</a></p>",
    )


MIME_OVERRIDES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".json": "application/json",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".map": "application/json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".wasm": "application/wasm",
}


def mime_for(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    import mimetypes
    guess, _ = mimetypes.guess_type("x" + ext)
    return guess or "application/octet-stream"


def get_static(rel):
    rel = _safe_rel(rel)
    if rel is None:
        return None
    if DEBUG:
        return read_debug_file("static", rel)
    return BUNDLED_STATIC.get(rel)


ROBOTS_TXT = (
    "User-agent: *\n"
    "Disallow: /n/\n"
    "Disallow: /submit\n"
    "Disallow: /render\n"
    "Disallow: /_health\n"
    "Disallow: /_ops/\n"
    "Allow: /\n"
)

DOC_ROUTES = {}      # route -> relative path under docs root
BUNDLED_DOC_PAGES = {}  # route -> (body html, title, preview)


def _doc_route(rel):
    return "/" + rel[:-3]


def render_doc(text):
    rendered, title, preview = render_content(text, "markdown")
    body = "<p><a href=\"/\">New note</a></p>\n" + rendered
    return body, title, preview


def init_docs():
    DOC_ROUTES.clear()
    BUNDLED_DOC_PAGES.clear()
    for rel, data in BUNDLED_DOCS.items():
        if rel.endswith(".md") and _safe_rel(rel):
            route = _doc_route(rel)
            DOC_ROUTES[route] = rel
            if not DEBUG:
                BUNDLED_DOC_PAGES[route] = render_doc(data.decode("utf-8", errors="replace"))
    if DEBUG:
        base = os.path.join(os.getcwd(), "docs")
        for dirpath, _, files in os.walk(base):
            for f in files:
                if not f.endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, f), base).replace(os.sep, "/")
                if _safe_rel(rel):
                    DOC_ROUTES[_doc_route(rel)] = rel


def get_doc_page(route):
    rel = DOC_ROUTES.get(route)
    if rel is None:
        return None
    if not DEBUG:
        return BUNDLED_DOC_PAGES.get(route)
    data = read_debug_file("docs", rel)
    if data is None:
        return None
    return render_doc(data.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

try:
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter as _PygHtmlFormatter
    from pygments.lexers import get_lexer_by_name as _pyg_get_lexer
    from pygments.util import ClassNotFound as _PygClassNotFound
except ImportError:  # pragma: no cover - fallback tokenizer is used instead
    _pyg_highlight = None

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
HR_RE = re.compile(r"^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
BQ_RE = re.compile(r"^ {0,3}> ?(.*)$")
UL_RE = re.compile(r"^( {0,3})([-*+])(?:[ \t]+(.*)|[ \t]*)$")
OL_RE = re.compile(r"^( {0,3})(\d{1,9})([.)])(?:[ \t]+(.*)|[ \t]*)$")
TOC_RE = re.compile(r"^ {0,3}\{\{[ \t]*toc(?:[ \t]+([1-6])[ \t]+([1-6]))?[ \t]*\}\}[ \t]*$")
TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def _blank(line):
    return not line.strip()


def slugify(text):
    s = text.lower()
    s = re.sub(r"\s", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _fallback_tokens(code):
    out = []
    for tok in re.findall(r"\s+|\w+|[^\w\s]", code):
        if tok.isspace():
            out.append(html.escape(tok, quote=False))
        else:
            out.append(f"<span class=\"tok\">{html.escape(tok, quote=True)}</span>")
    return "".join(out)


def highlight_code(code, lang):
    result = None
    if _pyg_highlight is not None:
        try:
            lexer = _pyg_get_lexer(lang, stripnl=False, ensurenl=False)
        except (_PygClassNotFound, Exception):
            lexer = None
        if lexer is not None:
            try:
                result = _pyg_highlight(code, lexer, _PygHtmlFormatter(nowrap=True))
            except Exception:
                result = None
    if result is None or ("<span" not in result and code.strip()):
        result = _fallback_tokens(code)
    return result


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, i = [], [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur).strip())
    return cells


class MarkdownRenderer:
    ESCAPABLE = "\\`*_{}[]()#+-.!|>~<"

    def __init__(self):
        self._ph = []
        self.title = None
        self.preview = None
        self.headings = []  # (level, id, plain text)
        self.tocs = []

    # -- inline -----------------------------------------------------------
    def _hold(self, s):
        self._ph.append(s)
        return f"\x00{len(self._ph) - 1}\x00"

    def _restore(self, s):
        prev = None
        while prev != s:
            prev = s
            s = re.sub(r"\x00(\d+)\x00", lambda m: self._ph[int(m.group(1))], s)
        return s

    @staticmethod
    def _safe_url(url):
        u = html.unescape(url).strip()
        scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", re.sub(r"[\x00-\x20]", "", u))
        if scheme and scheme.group(1).lower() in ("javascript", "vbscript", "data"):
            return "#"
        return u

    def inline(self, text):
        text = text.replace("\x00", "�")
        # code spans
        def code_span(m):
            body = m.group(2)
            if len(body) >= 2 and body[0] == " " and body[-1] == " " and body.strip():
                body = body[1:-1]
            return self._hold(f"<code>{html.escape(body, quote=False)}</code>")
        text = re.sub(r"(`+)(.+?)(?<!`)\1(?!`)", code_span, text, flags=re.S)
        # backslash escapes
        text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~<])",
                      lambda m: self._hold(html.escape(m.group(1), quote=False)), text)
        # autolinks <http://...>
        def autolink(m):
            url = m.group(1)
            return self._hold(f"<a href=\"{html.escape(self._safe_url(url), quote=True)}\">"
                              f"{html.escape(url, quote=False)}</a>")
        text = re.sub(r"<((?:https?|ftp|mailto):[^\s<>]+)>", autolink, text)
        text = html.escape(text, quote=False)
        # images and links
        link_re = re.compile(r"(!?)\[([^\[\]]*)\]\(\s*(&lt;[^>]*?&gt;|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
                             r"(?:\s+(?:\"([^\"]*)\"|'([^']*)'))?\s*\)")

        def link(m):
            url = m.group(3)
            if url.startswith("&lt;") and url.endswith("&gt;"):
                url = url[4:-4]
            url = self._safe_url(url)
            title = m.group(4) if m.group(4) is not None else m.group(5)
            title_attr = ""
            if title is not None:
                title_attr = f" title=\"{html.escape(html.unescape(title), quote=True)}\""
            href = html.escape(url, quote=True)
            if m.group(1):
                alt = html.escape(html.unescape(m.group(2)), quote=True)
                return self._hold(f"<img src=\"{href}\" alt=\"{alt}\"{title_attr}>")
            return (self._hold(f"<a href=\"{href}\"{title_attr}>") + m.group(2) + self._hold("</a>"))
        text = link_re.sub(link, text)
        # emphasis
        for _ in range(3):
            new = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<em><strong>\1</strong></em>", text, flags=re.S)
            new = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", new, flags=re.S)
            new = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", new, flags=re.S)
            new = re.sub(r"\*(?=[^\s*])(.+?)(?<=[^\s*])\*", r"<em>\1</em>", new, flags=re.S)
            new = re.sub(r"\*(?=[^\s*])([^*])\*", r"<em>\1</em>", new)
            new = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", new, flags=re.S)
            new = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", new, flags=re.S)
            if new == text:
                break
            text = new
        # hard line breaks
        text = re.sub(r"(?: {2,}|\\)\n", "<br>\n", text)
        return self._restore(text)

    # -- blocks -----------------------------------------------------------
    def _starts_block(self, lines, i):
        line = lines[i]
        if FENCE_RE.match(line) or HEADING_RE.match(line) or HR_RE.match(line) or BQ_RE.match(line):
            return True
        m = UL_RE.match(line)
        if m and m.group(3):
            return True
        m = OL_RE.match(line)
        if m and m.group(4):
            return True
        if "|" in line and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]) and "|" in lines[i + 1] + line:
            return True
        return False

    def blocks(self, lines, tight=False):
        out = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if _blank(line):
                i += 1
                continue
            m = TOC_RE.match(line)
            if m and (i == 0 or _blank(lines[i - 1])) and (i + 1 >= n or _blank(lines[i + 1])):
                lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (1, 6)
                self.tocs.append((lo, hi))
                out.append(f"\x00TOC{len(self.tocs) - 1}\x00")
                i += 1
                continue
            m = FENCE_RE.match(line)
            if m:
                fence = m.group(1)
                lang = m.group(2)
                indent = len(line) - len(line.lstrip(" "))
                i += 1
                code = []
                while i < n:
                    if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", lines[i]):
                        i += 1
                        break
                    cl = lines[i]
                    k = 0
                    while k < indent and k < len(cl) and cl[k] == " ":
                        k += 1
                    code.append(cl[k:])
                    i += 1
                src = "\n".join(code) + ("\n" if code else "")
                if lang:
                    lang_attr = html.escape(lang, quote=True)
                    out.append(f"<pre><code class=\"language-{lang_attr}\">"
                               f"{highlight_code(src, lang)}</code></pre>")
                else:
                    out.append(f"<pre><code>{html.escape(src, quote=False)}</code></pre>")
                continue
            m = HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                content = (m.group(2) or "").strip()
                rendered = self.inline(content)
                plain = html.unescape(re.sub(r"<[^>]*>", "", rendered))
                hid = slugify(plain)
                self.headings.append((level, hid, plain_text(rendered)))
                if self.title is None:
                    self.title = plain_text(rendered)
                out.append(f"<h{level} id=\"{hid}\">{rendered}</h{level}>")
                i += 1
                continue
            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue
            if BQ_RE.match(line):
                inner = []
                while i < n:
                    bm = BQ_RE.match(lines[i])
                    if bm:
                        inner.append(bm.group(1))
                    elif not _blank(lines[i]) and inner and not _blank(inner[-1]) \
                            and not self._starts_block(lines, i):
                        inner.append(lines[i])  # lazy continuation
                    else:
                        break
                    i += 1
                out.append("<blockquote>\n" + self.blocks(inner) + "\n</blockquote>")
                continue
            if UL_RE.match(line) or OL_RE.match(line):
                html_list, i = self._list(lines, i)
                out.append(html_list)
                continue
            if "|" in line and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]) and \
                    ("|" in lines[i + 1] or line.strip().startswith("|")):
                header = _split_row(line)
                seps = _split_row(lines[i + 1])
                if len(header) == len(seps):
                    aligns = []
                    for s in seps:
                        s = s.strip()
                        if s.startswith(":") and s.endswith(":"):
                            aligns.append("center")
                        elif s.endswith(":"):
                            aligns.append("right")
                        elif s.startswith(":"):
                            aligns.append("left")
                        else:
                            aligns.append(None)
                    i += 2
                    rows = []
                    while i < n and not _blank(lines[i]) and "|" in lines[i]:
                        rows.append(_split_row(lines[i]))
                        i += 1
                    out.append(self._table(header, aligns, rows))
                    continue
            # paragraph
            para = [line.strip()]
            i += 1
            while i < n and not _blank(lines[i]) and not self._starts_block(lines, i):
                para.append(lines[i].strip() if not lines[i].endswith("  ") else lines[i].lstrip())
                i += 1
            para[0] = line.lstrip()
            content = self.inline("\n".join(para).rstrip())
            if not tight and self.preview is None:
                self.preview = plain_text(content)
            out.append(content if tight else f"<p>{content}</p>")
        return "\n".join(out)

    def _table(self, header, aligns, rows):
        def cell(tag, text, idx):
            a = aligns[idx] if idx < len(aligns) else None
            style = f" style=\"text-align: {a}\"" if a else ""
            return f"<{tag}{style}>{self.inline(text)}</{tag}>"
        parts = ["<table>", "<thead>", "<tr>"]
        parts += [cell("th", h, k) for k, h in enumerate(header)]
        parts += ["</tr>", "</thead>", "<tbody>"]
        for r in rows:
            r = (r + [""] * len(header))[:len(header)]
            parts.append("<tr>")
            parts += [cell("td", c, k) for k, c in enumerate(r)]
            parts.append("</tr>")
        parts += ["</tbody>", "</table>"]
        return "\n".join(parts)

    def _list_marker(self, line):
        m = UL_RE.match(line)
        if m:
            content = m.group(3) or ""
            width = len(line) - len(content) if content else len(m.group(1)) + 2
            return "ul", m.group(2), None, content, width
        m = OL_RE.match(line)
        if m:
            content = m.group(4) or ""
            width = len(line) - len(content) if content else len(m.group(1)) + len(m.group(2)) + 2
            return "ol", m.group(3), int(m.group(2)), content, width
        return None

    def _list(self, lines, i):
        n = len(lines)
        kind, delim, start, _, _ = self._list_marker(lines[i])
        items = []
        loose = False
        while i < n:
            mk = self._list_marker(lines[i])
            if not mk or mk[0] != kind or mk[1] != delim or HR_RE.match(lines[i]):
                break
            _, _, _, content, width = mk
            item = [content]
            i += 1
            while i < n:
                l = lines[i]
                if _blank(l):
                    # look ahead: continue item if next non-blank is indented
                    j = i
                    while j < n and _blank(lines[j]):
                        j += 1
                    if j < n and len(lines[j]) - len(lines[j].lstrip()) >= min(width, 4) \
                            and len(lines[j]) - len(lines[j].lstrip()) >= 2:
                        item.extend([""] * (j - i))
                        i = j
                        continue
                    break
                ind = len(l) - len(l.lstrip())
                if ind >= min(width, 4) and ind >= 2:
                    item.append(l[min(ind, width):])
                    i += 1
                    continue
                if self._list_marker(l) or self._starts_block(lines, i):
                    break
                item.append(l)  # lazy continuation
                i += 1
            items.append(item)
            # blank line between items => loose
            if i < n and _blank(lines[i]):
                j = i
                while j < n and _blank(lines[j]):
                    j += 1
                nm = self._list_marker(lines[j]) if j < n else None
                if nm and nm[0] == kind and nm[1] == delim and not HR_RE.match(lines[j]):
                    loose = True
                    i = j
                    continue
                break
        for item in items:
            stripped = list(item)
            while stripped and _blank(stripped[-1]):
                stripped.pop()
            if any(_blank(x) for x in stripped):
                # blank inside an item between paragraphs makes it loose
                k = [idx for idx, x in enumerate(stripped) if _blank(x)]
                for idx in k:
                    if idx + 1 < len(stripped) and not (len(stripped[idx + 1]) - len(stripped[idx + 1].lstrip()) > 0):
                        loose = True
        tag = kind
        attr = f" start=\"{start}\"" if kind == "ol" and start != 1 else ""
        parts = [f"<{tag}{attr}>"]
        for item in items:
            inner = self.blocks(item, tight=not loose)
            if "\n" in inner or inner.startswith("<"):
                parts.append(f"<li>{inner}</li>")
            else:
                parts.append(f"<li>{inner}</li>")
        parts.append(f"</{tag}>")
        return "\n".join(parts), i

    def render(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).replace("\x00", "\ufffd")
        out = self.blocks(text.split("\n"))
        out = re.sub(r"\x00TOC(\d+)\x00", lambda m: self._toc(*self.tocs[int(m.group(1))]), out)
        return out

    def _toc(self, lo, hi):
        items = [h for h in self.headings if lo <= h[0] <= hi]
        if not items:
            return ""

        def link(h):
            return f"<li><a href=\"#{html.escape(h[1], quote=True)}\">{html.escape(h[2], quote=False)}</a>"
        out = ["<nav class=\"table-of-contents\">", "<ul>", link(items[0])]
        stack = [items[0][0]]
        for h in items[1:]:
            lvl = h[0]
            if lvl > stack[-1]:
                out.append("<ul>")
                stack.append(lvl)
            else:
                out.append("</li>")
                while len(stack) > 1 and lvl <= stack[-2]:
                    stack.pop()
                    out.append("</ul>")
                    out.append("</li>")
                stack[-1] = lvl
            out.append(link(h))
        out.append("</li>")
        for _ in range(len(stack) - 1):
            out.append("</ul>")
            out.append("</li>")
        out.append("</ul>")
        out.append("</nav>")
        return "\n".join(out)


def plain_text(fragment):
    """Strip inline HTML formatting, returning plain text content."""
    def img(m):
        a = re.search(r'\balt="([^"]*)"', m.group(0))
        return a.group(1) if a else ""
    s = re.sub(r"<img\b[^>]*>", img, fragment)
    s = re.sub(r"<br\s*/?>", "", s)
    s = re.sub(r"<[^>]*>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]*\n[ \t]*", " ", s)
    return s.strip()


def render_markdown(text):
    return MarkdownRenderer().render(text)


def render_content(content, mode):
    """Return (html, title or None, preview string)."""
    if mode == "markdown":
        r = MarkdownRenderer()
        try:
            rendered = r.render(content)
            title, preview = r.title, r.preview or ""
        except Exception:
            rendered = f"<pre><code>{html.escape(content, quote=True)}</code></pre>"
            title, preview = None, ""
        return f"<div class=\"markdown\">\n{rendered}\n</div>", title, preview
    return f"<pre><code>{html.escape(content, quote=True)}</code></pre>", None, ""


def og_head(title, preview):
    return (f"<meta property=\"og:title\" content=\"{html.escape(title or 'TextDrop', quote=True)}\">\n"
            f"<meta property=\"og:description\" content=\"{html.escape(preview or '', quote=True)}\">\n")


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
            return self.send_html(200, editor_page())
        if path in ("/health", "/_health"):
            if DRAINED:
                return self.send(404, '{"status":"drained"}', "application/json")
            body = json.dumps({"status": "active", "notes": STORE.count(),
                               "uptime": round(time.time() - START_TIME, 3)})
            return self.send(200, body, "application/json")
        if path == "/_ops/drain":
            return self.drain()
        m = NOTE_TEXT_RE.match(path)
        if m:
            content = STORE.get(m.group(1))
            if content is None:
                return self.error_page(404, "Not Found", "That note does not exist.")
            return self.send(200, content, "text/plain; charset=utf-8")
        m = NOTE_RE.match(path)
        if m:
            nid = m.group(1)
            note = STORE.get_note(nid)
            if note is None:
                return self.error_page(404, "Not Found", "That note does not exist.")
            content, mode = note
            rendered, title, preview = render_content(content, mode)
            if mode == "markdown":
                header = f"<p><strong>Note {html.escape(nid)}</strong> | "
            else:
                header = f"<h1>Note {html.escape(nid)}</h1>\n<p>"
            body = (
                header +
                f"<a href=\"/n/{nid}/text\">Raw text</a> | <a href=\"/\">New note</a></p>\n"
                f"{rendered}"
            )
            return self.send_html(200, page(title or "TextDrop", body, raw_title=True,
                                            head=og_head(title, preview)))
        if path == "/robots.txt":
            return self.send(200, ROBOTS_TXT, "text/plain")
        if path == "/favicon.ico":
            data = get_static("favicon.ico")
            if data is None:
                return self.not_found()
            return self.send(200, data, "image/x-icon")
        if path == "/static" or path.startswith("/static/"):
            rel = unquote(path[len("/static/"):]) if path.startswith("/static/") else ""
            data = get_static(rel)
            if data is None:
                return self.not_found()
            return self.send(200, data, mime_for(rel))
        if path in DOC_ROUTES:
            doc = get_doc_page(path)
            if doc is None:
                return self.not_found()
            body, title, preview = doc
            return self.send_html(200, page(title or "TextDrop", body, raw_title=True,
                                            head=og_head(title, preview)))
        return self.not_found()

    do_HEAD = do_GET

    def drain(self):
        global DRAINED
        auth = self.headers.get_all("Authorization") or []
        ok = (bool(OPS_SECRET) and len(auth) == 1
              and hmac.compare_digest(auth[0].encode("utf-8", "surrogateescape"),
                                      ("Bearer " + OPS_SECRET).encode("utf-8", "surrogateescape")))
        if not ok:
            return self.send(401, '{"error":"unauthorized"}', "application/json")
        DRAINED = True
        return self.send(200, '{"status":"ok"}', "application/json")

    def user_cookie(self):
        for raw in self.headers.get_all("Cookie") or []:
            for part in raw.split(";"):
                name, sep, val = part.strip().partition("=")
                if sep and name.strip() == USER_COOKIE:
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] == '"':
                        val = val[1:-1]
                    if valid_user_token(ID_SEED, val):
                        return val
        return None

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
        if path not in ("/submit", "/render"):
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
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype not in ("application/x-www-form-urlencoded", "application/json"):
            return self.error_page(400, "Bad Request",
                                   "Unsupported Content-Type; use form data or JSON.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self.error_page(400, "Bad Request", "Content must be valid UTF-8.")
        if ctype == "application/json":
            try:
                data = json.loads(text) if text.strip() else None
            except (ValueError, RecursionError):
                return self.error_page(400, "Bad Request", "Malformed JSON body.")
            if not isinstance(data, dict):
                return self.error_page(400, "Bad Request", "JSON body must be an object.")
            content = data.get("body", data.get("content", ""))
            mode = data.get("mode", "")
            if content is None:
                content = ""
            if mode is None:
                mode = ""
            if not isinstance(content, str) or not isinstance(mode, str):
                return self.error_page(400, "Bad Request", "Fields 'body' and 'mode' must be strings.")
        else:
            try:
                fields = parse_qs(text, keep_blank_values=True, encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                return self.error_page(400, "Bad Request", "Content must be valid UTF-8.")
            except ValueError:
                return self.error_page(400, "Bad Request", "Malformed form data.")
            if "content" in fields:
                content = fields["content"][0]
            else:
                content = fields.get("body", [""])[0]
            mode = fields.get("mode", [""])[0]
        try:
            content.encode("utf-8")
            mode.encode("utf-8")
        except UnicodeEncodeError:
            return self.error_page(400, "Bad Request", "Content must be valid UTF-8.")
        if mode not in ("", "markdown"):
            return self.error_page(400, "Bad Request", "Unsupported rendering mode.")
        if not content.strip():
            return self.error_page(400, "Bad Request", "Content must not be empty.")
        if path == "/render":
            rendered, title, preview = render_content(content, mode)
            body = ("<p><strong>Preview</strong> | <a href=\"/\">New note</a></p>\n"
                    f"{rendered}")
            return self.send_html(200, page(f"{title or 'TextDrop'} - Preview", body, raw_title=True,
                                            head=og_head(title, preview)))
        headers = {}
        user = ""
        if ID_SEED:
            user = self.user_cookie()
            if user is None:
                user = make_user_token(ID_SEED)
                headers["Set-Cookie"] = (f"{USER_COOKIE}={user}; Path=/; Max-Age=31536000; "
                                         "HttpOnly; SameSite=Lax")
        nid = STORE.create(content, mode, user.split(".", 1)[0])
        loc = f"/n/{nid}"
        headers["Location"] = loc
        self.send_html(302, page("Redirect", f"<p>Redirecting to <a href=\"{loc}\">{loc}</a></p>"),
                       headers)

    def _known(self, path):
        if path in ("/", "/submit", "/health", "/_health", "/_ops/drain", "/robots.txt") or path in DOC_ROUTES:
            return True
        if path == "/favicon.ico":
            return get_static("favicon.ico") is not None
        if path.startswith("/static/"):
            return get_static(unquote(path[len("/static/"):])) is not None
        return bool(NOTE_RE.match(path) or NOTE_TEXT_RE.match(path))

    def _other(self):
        path = self.path_only()
        self.read_body()
        if self._known(path):
            allow = "GET, HEAD, POST" if path == "/submit" else "GET, HEAD"
            return self.error_page(405, "Method Not Allowed", "This method is not allowed here.",
                                   {"Allow": allow})
        return self.not_found()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other


init_docs()


def main(argv=None):
    p = argparse.ArgumentParser(description="TextDrop text sharing service")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--db", default=None, help="SQLite database path")
    p.add_argument("--debug", action="store_true",
                   help="load templates, static assets and docs from ./templates, ./static and ./docs")
    p.add_argument("--ops_secret", "--ops-secret", dest="ops_secret", default=None,
                   help="bearer token for /_ops/drain (env: TEXTDROP_OPS_SECRET)")
    p.add_argument("--id_seed", "--id-seed", dest="id_seed", default=None,
                   help="seed for signing user_id cookies (env: TEXTDROP_ID_SEED)")
    a = p.parse_args(argv)
    global OPS_SECRET, ID_SEED
    OPS_SECRET = a.ops_secret if a.ops_secret is not None else os.environ.get("TEXTDROP_OPS_SECRET", "")
    ID_SEED = a.id_seed if a.id_seed is not None else os.environ.get("TEXTDROP_ID_SEED", "")
    global DEBUG
    DEBUG = bool(a.debug)
    init_docs()
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
