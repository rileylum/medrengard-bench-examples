"""Small, safe markdown-to-HTML renderer used by TextDrop.

Raw HTML in the source is always escaped. Supported constructs: paragraphs,
ATX headings (with slug ids), emphasis, links, images, unordered/ordered
lists (with nesting), blockquotes, inline code, fenced code blocks (with
syntax highlighting when a language is given), horizontal rules and
pipe tables.
"""
import html
import re
import secrets

try:  # optional dependency; a built-in fallback highlighter is used otherwise
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter as _PygFormatter
    from pygments.lexers import get_lexer_by_name as _pyg_lexer
    from pygments.util import ClassNotFound as _PygNotFound
except Exception:  # pragma: no cover
    _pyg_highlight = None

__all__ = ["render_markdown", "render_document", "slugify"]

# ---------------------------------------------------------------------------
# helpers

def _esc(s):
    return html.escape(s, quote=True)


def slugify(text):
    s = text.lower()
    s = re.sub(r"\s", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


_TAG_RE = re.compile(r"<[^>]*>")


def _strip_tags(rendered):
    return html.unescape(_TAG_RE.sub("", rendered))


# ---------------------------------------------------------------------------
# syntax highlighting

_GENERIC_TOKEN_RE = re.compile(
    r"(?P<comment>//[^\n]*|#[^\n]*|/\*.*?\*/|--[^\n]*)"
    r"|(?P<string>\"(?:\\.|[^\"\\\n])*\"?|'(?:\\.|[^'\\\n])*'?|`[^`]*`?)"
    r"|(?P<number>\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\b0x[0-9a-fA-F]+\b)"
    r"|(?P<name>[A-Za-z_$][\w$]*)"
    r"|(?P<punct>[^\w\s])",
    re.S,
)

_GENERIC_CLASSES = {"comment": "c", "string": "s", "number": "m",
                    "name": "n", "punct": "p"}


def _generic_highlight(code):
    out = []
    pos = 0
    for m in _GENERIC_TOKEN_RE.finditer(code):
        if m.start() > pos:
            out.append(_esc(code[pos:m.start()]))
        out.append('<span class="%s">%s</span>'
                   % (_GENERIC_CLASSES[m.lastgroup], _esc(m.group(0))))
        pos = m.end()
    out.append(_esc(code[pos:]))
    return "".join(out)


def _highlight(code, lang):
    result = None
    if _pyg_highlight is not None:
        try:
            lexer = _pyg_lexer(lang, stripnl=False, ensurenl=False)
        except _PygNotFound:
            lexer = None
        except Exception:
            lexer = None
        if lexer is not None:
            try:
                result = _pyg_highlight(code, lexer, _PygFormatter(nowrap=True))
            except Exception:
                result = None
    if result is None or "<span" not in result:
        result = _generic_highlight(code)
    return result


def _code_block(code, lang):
    if lang:
        cls = re.sub(r"[^A-Za-z0-9_+#.-]", "", lang)
        return ('<pre class="highlight"><code class="language-%s">%s</code></pre>'
                % (_esc(cls), _highlight(code, lang)))
    return "<pre><code>%s</code></pre>" % _esc(code)


# ---------------------------------------------------------------------------
# inline rendering

_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_SAFE_URL_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|[^:]*$)")
_BAD_SCHEMES = ("javascript:", "vbscript:", "data:")


def _safe_url(url):
    probe = re.sub(r"[\x00-\x20]", "", html.unescape(url)).lower()
    if probe.startswith(_BAD_SCHEMES):
        return "#"
    return url


_LINK_TAIL_RE = re.compile(
    r"\(\s*(?:<([^<>\n]*)>|((?:[^\s()\\]|\\.|\((?:[^\s()\\]|\\.)*\))*))"
    r"(?:\s+(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'))?\s*\)"
)
_AUTOLINK_RE = re.compile(r"<((?:https?|ftp|mailto):[^\s<>]*)>", re.I)


def _unescape_md(s):
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", s)


def _find_bracket_end(text, start):
    """text[start] == '['; return index of the matching ']' or -1."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            run = re.match(r"`+", text[i:]).group(0)
            close = text.find(run, i + len(run))
            if close != -1:
                i = close + len(run)
                continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


class _Inline:
    def __init__(self):
        self.slots = []

    def _slot(self, value):
        self.slots.append(value)
        return "\x00%d\x00" % (len(self.slots) - 1)

    def render(self, text, allow_links=True):
        out = []
        buf = []
        i = 0
        n = len(text)

        def flush():
            if buf:
                out.append(self._emphasis(_esc("".join(buf))))
                buf.clear()

        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n and text[i + 1] in _PUNCT:
                flush()
                out.append(self._slot(_esc(text[i + 1])))
                i += 2
                continue
            if c == "\\" and i + 1 < n and text[i + 1] == "\n":
                flush()
                out.append(self._slot("<br>\n"))
                i += 2
                continue
            if c == "`":
                run = re.match(r"`+", text[i:]).group(0)
                j = i + len(run)
                close = -1
                while True:
                    k = text.find(run, j)
                    if k == -1:
                        break
                    if (k + len(run) < n and text[k + len(run)] == "`"):
                        j = k + len(run)
                        while j < n and text[j] == "`":
                            j += 1
                        continue
                    close = k
                    break
                if close != -1:
                    code = text[i + len(run):close].replace("\n", " ")
                    if (len(code) >= 2 and code[0] == " " and code[-1] == " "
                            and code.strip(" ")):
                        code = code[1:-1]
                    flush()
                    out.append(self._slot("<code>%s</code>" % _esc(code)))
                    i = close + len(run)
                    continue
                buf.append(run)
                i += len(run)
                continue
            if c == "<":
                m = _AUTOLINK_RE.match(text, i)
                if m and allow_links:
                    url = m.group(1)
                    flush()
                    out.append(self._slot('<a href="%s">%s</a>'
                                          % (_esc(_safe_url(url)), _esc(url))))
                    i = m.end()
                    continue
            is_img = c == "!" and i + 1 < n and text[i + 1] == "["
            if (c == "[" or is_img) and allow_links:
                b = i + 1 if is_img else i
                end = _find_bracket_end(text, b)
                if end != -1:
                    m = _LINK_TAIL_RE.match(text, end + 1)
                    if m:
                        label = text[b + 1:end]
                        url = m.group(1) if m.group(1) is not None else (m.group(2) or "")
                        url = _unescape_md(url)
                        title = m.group(3) if m.group(3) is not None else m.group(4)
                        title_attr = (' title="%s"' % _esc(_unescape_md(title))
                                      if title is not None else "")
                        flush()
                        if is_img:
                            alt = _strip_tags(_Inline().render(label, allow_links=False))
                            out.append(self._slot('<img src="%s" alt="%s"%s>'
                                                  % (_esc(_safe_url(url)), _esc(alt), title_attr)))
                        else:
                            sub = _Inline()
                            inner = sub.finish(sub.render(label, allow_links=False))
                            out.append(self._slot('<a href="%s"%s>%s</a>'
                                                  % (_esc(_safe_url(url)), title_attr, inner)))
                        i = m.end()
                        continue
            if c == "\n":
                # hard line break: two or more trailing spaces
                stripped = "".join(buf)
                if stripped.endswith("  "):
                    buf[:] = [stripped.rstrip(" ")]
                    flush()
                    out.append(self._slot("<br>\n"))
                    i += 1
                    continue
                buf[:] = [stripped.rstrip(" ")] if stripped else []
            buf.append(c)
            i += 1
        flush()
        return "".join(out)

    _STRONG_EM = [
        (re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", re.S), r"<strong><em>\1</em></strong>"),
        (re.compile(r"(?<![\w_])___(?=\S)(.+?)(?<=\S)___(?![\w_])", re.S), r"<strong><em>\1</em></strong>"),
        (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S), r"<strong>\1</strong>"),
        (re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])", re.S), r"<strong>\1</strong>"),
        (re.compile(r"\*(?=[^\s*])(.+?)(?<=[^\s*])\*", re.S), r"<em>\1</em>"),
        (re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", re.S), r"<em>\1</em>"),
        (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S), r"<del>\1</del>"),
    ]

    def _emphasis(self, s):
        # Strong/emphasis can span slot placeholders, so this is applied to
        # the whole escaped string afterwards (see finish()).
        return s

    def finish(self, s):
        prev = None
        while prev != s:
            prev = s
            for rx, rep in self._STRONG_EM:
                s = rx.sub(rep, s)
        # restore slots (they may nest)
        slot_re = re.compile(r"\x00(\d+)\x00")
        for _ in range(100):
            new = slot_re.sub(lambda m: self.slots[int(m.group(1))], s)
            if new == s:
                break
            s = new
        return s


def render_inline(text):
    r = _Inline()
    return r.finish(r.render(text.strip()))


# ---------------------------------------------------------------------------
# block rendering

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_UL_RE = re.compile(r"^( {0,3})([-*+])(?:[ \t]+(.*))?$")
_OL_RE = re.compile(r"^( {0,3})(\d{1,9})([.)])(?:[ \t]+(.*))?$")
_BQ_RE = re.compile(r"^ {0,3}> ?(.*)$")
_TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def _is_blank(line):
    return not line.strip()


def _list_match(line):
    m = _UL_RE.match(line)
    if m and not _HR_RE.match(line):
        return ("ul", m.group(2), len(m.group(1)) + len(m.group(2)) + 1, m.group(3) or "", None)
    m = _OL_RE.match(line)
    if m:
        return ("ol", m.group(3), len(m.group(1)) + len(m.group(2)) + 2, m.group(4) or "",
                int(m.group(2)))
    return None


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = []
    cur = []
    i = 0
    in_code = None
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "`":
            run = re.match(r"`+", s[i:]).group(0)
            if in_code is None:
                in_code = run
            elif in_code == run:
                in_code = None
            cur.append(run)
            i += len(run)
            continue
        if c == "|" and in_code is None:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _is_table_start(lines, i):
    if i + 1 >= len(lines):
        return False
    head, sep = lines[i], lines[i + 1]
    if "|" not in head or "|" not in sep and "|" not in head.strip()[1:]:
        pass
    if "|" not in head:
        return False
    if not _TABLE_SEP_RE.match(sep):
        return False
    if "|" not in sep and len(_split_row(head)) < 2:
        return False
    return len(_split_row(sep)) == len(_split_row(head))


def _starts_block(lines, i):
    line = lines[i]
    return bool(_FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line)
                or _BQ_RE.match(line) or _list_match(line) or _is_table_start(lines, i))


_TOC_RE = re.compile(r"^ {0,3}\{\{[ \t]*toc(?:[ \t]+([1-6])[ \t]+([1-6]))?[ \t]*\}\}[ \t]*$")


class _Renderer:
    def __init__(self):
        self.headings = []      # (level, slug, plain text)
        self.preview = None     # plain text of first paragraph
        self.tocs = []          # (placeholder, from, to)
        self._token = secrets.token_hex(8)

    def _toc_directive(self, lines, i):
        m = _TOC_RE.match(lines[i])
        if not m:
            return None
        if i > 0 and not _is_blank(lines[i - 1]):
            return None
        if i + 1 < len(lines) and not _is_blank(lines[i + 1]):
            return None
        lo = int(m.group(1)) if m.group(1) else 1
        hi = int(m.group(2)) if m.group(2) else 6
        return lo, hi

    def build_toc(self, lo, hi):
        hs = [h for h in self.headings if lo <= h[0] <= hi]
        if not hs:
            return ""
        parts = ['<nav class="table-of-contents">\n']
        stack = []
        for level, slug, text in hs:
            if not stack:
                parts.append("<ul>\n<li>")
                stack.append(level)
            elif level > stack[-1]:
                parts.append("\n<ul>\n<li>")
                stack.append(level)
            else:
                parts.append("</li>\n")
                while len(stack) > 1 and level <= stack[-2]:
                    stack.pop()
                    parts.append("</ul>\n</li>\n")
                stack[-1] = level
                parts.append("<li>")
            parts.append('<a href="#%s">%s</a>' % (_esc(slug), _esc(text)))
        parts.append("</li>\n")
        for _ in range(len(stack) - 1):
            parts.append("</ul>\n</li>\n")
        parts.append("</ul>\n</nav>")
        return "".join(parts)

    def finalize(self, rendered):
        for placeholder, lo, hi in self.tocs:
            rendered = rendered.replace(placeholder, self.build_toc(lo, hi))
        return rendered

    def blocks(self, lines, tight=False):
        out = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if _is_blank(line):
                i += 1
                continue

            m = _FENCE_RE.match(line)
            if m:
                indent, fence, lang = len(m.group(1)), m.group(2), m.group(3)
                if fence[0] == "`" or True:
                    body = []
                    i += 1
                    close_re = re.compile(r"^ {0,3}%s%s*[ \t]*$"
                                          % (re.escape(fence), re.escape(fence[0])))
                    while i < n and not close_re.match(lines[i]):
                        l = lines[i]
                        k = 0
                        while k < indent and k < len(l) and l[k] == " ":
                            k += 1
                        body.append(l[k:])
                        i += 1
                    i += 1  # skip closing fence (or run off the end)
                    code = "\n".join(body)
                    if body:
                        code += "\n"
                    out.append(_code_block(code, _unescape_md(lang)))
                    continue

            m = _HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                inner = render_inline(m.group(2) or "")
                plain = _strip_tags(inner)
                slug = slugify(plain)
                self.headings.append((level, slug, plain.strip()))
                out.append('<h%d id="%s">%s</h%d>' % (level, _esc(slug), inner, level))
                i += 1
                continue

            if _HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue

            if _is_table_start(lines, i):
                i = self._table(lines, i, out)
                continue

            if _BQ_RE.match(line):
                inner = []
                while i < n:
                    bm = _BQ_RE.match(lines[i])
                    if bm:
                        inner.append(bm.group(1))
                    elif (not _is_blank(lines[i]) and inner and not _is_blank(inner[-1])
                          and not _starts_block(lines, i)):
                        inner.append(lines[i])  # lazy continuation
                    else:
                        break
                    i += 1
                out.append("<blockquote>\n%s\n</blockquote>" % self.blocks(inner))
                continue

            lm = _list_match(line)
            if lm:
                i = self._list(lines, i, lm, out)
                continue

            toc = self._toc_directive(lines, i)
            if toc:
                placeholder = "\x02TOC%s-%d\x02" % (self._token, len(self.tocs))
                self.tocs.append((placeholder, toc[0], toc[1]))
                out.append(placeholder)
                i += 1
                continue

            # paragraph
            para = [line]
            i += 1
            while i < n and not _is_blank(lines[i]) and not _starts_block(lines, i):
                para.append(lines[i])
                i += 1
            text = "\n".join(l.lstrip() for l in para)
            inner = render_inline(text)
            if not tight and self.preview is None:
                self.preview = _strip_tags(inner).strip()
            out.append(inner if tight else "<p>%s</p>" % inner)
        return "\n".join(out)

    def _table(self, lines, i, out):
        header = _split_row(lines[i])
        aligns = []
        for cell in _split_row(lines[i + 1]):
            c = cell.strip()
            if c.startswith(":") and c.endswith(":"):
                aligns.append("center")
            elif c.endswith(":"):
                aligns.append("right")
            elif c.startswith(":"):
                aligns.append("left")
            else:
                aligns.append(None)
        ncols = len(header)
        i += 2
        rows = []
        while i < len(lines) and not _is_blank(lines[i]) and "|" in lines[i]:
            if (_FENCE_RE.match(lines[i]) or _HEADING_RE.match(lines[i])
                    or _BQ_RE.match(lines[i])):
                break
            cells = _split_row(lines[i])
            cells = (cells + [""] * ncols)[:ncols]
            rows.append(cells)
            i += 1

        def cell(tag, text, idx):
            a = aligns[idx] if idx < len(aligns) else None
            attr = ' style="text-align: %s"' % a if a else ""
            return "<%s%s>%s</%s>" % (tag, attr, render_inline(text), tag)

        parts = ["<table>", "<thead>", "<tr>"]
        parts += [cell("th", h, k) for k, h in enumerate(header)]
        parts += ["</tr>", "</thead>", "<tbody>"]
        for r in rows:
            parts.append("<tr>")
            parts += [cell("td", c, k) for k, c in enumerate(r)]
            parts.append("</tr>")
        parts += ["</tbody>", "</table>"]
        out.append("\n".join(parts))
        return i

    def _list(self, lines, i, first, out):
        kind, marker, _, _, start = first
        n = len(lines)
        items = []
        loose = False
        while i < n:
            lm = _list_match(lines[i])
            if not lm or lm[0] != kind or lm[1] != marker:
                break
            _, _, width, rest, _ = lm
            item = [rest]
            i += 1
            saw_blank = False
            while i < n:
                l = lines[i]
                if _is_blank(l):
                    saw_blank = True
                    item.append("")
                    i += 1
                    continue
                indent = len(l) - len(l.lstrip(" "))
                if indent >= width or (indent >= 2 and not saw_blank):
                    item.append(l[min(indent, width):] if indent >= width else l.lstrip(" "))
                    saw_blank = False
                    i += 1
                    continue
                if saw_blank:
                    break
                # lazy paragraph continuation
                if not _starts_block(lines, i) and item and not _is_blank(item[-1]):
                    item.append(l)
                    i += 1
                    continue
                break
            # trailing blank lines belong between items, not to the item
            trailing = 0
            while item and _is_blank(item[-1]):
                item.pop()
                trailing += 1
            if trailing and i < n:
                nxt = _list_match(lines[i])
                if nxt and nxt[0] == kind and nxt[1] == marker:
                    loose = True
            if any(_is_blank(x) for x in item):
                # blank line between two paragraphs inside the item
                inner_blocks = [x for x in item]
                joined = "\n".join(inner_blocks)
                if re.search(r"\S\n\s*\n\s*\S", joined) and not _FENCE_RE.match(item[0]):
                    loose = True
            items.append(item)
        tag = kind
        attr = ' start="%d"' % start if kind == "ol" and start not in (None, 1) else ""
        parts = ["<%s%s>" % (tag, attr)]
        for item in items:
            body = self.blocks(item, tight=not loose)
            if "\n" in body or body.startswith("<"):
                parts.append("<li>%s</li>" % body)
            else:
                parts.append("<li>%s</li>" % body)
        parts.append("</%s>" % tag)
        out.append("\n".join(parts))
        return i


def render_document(text):
    """Render markdown; return (html, title or None, preview string)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
    text = text.expandtabs(4)
    lines = text.split("\n")
    r = _Renderer()
    rendered = r.finalize(r.blocks(lines))
    title = (r.headings[0][2] or None) if r.headings else None
    return rendered, title, r.preview or ""


def render_markdown(text):
    return render_document(text)[0]
