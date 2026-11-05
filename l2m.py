#!/usr/bin/env python3
"""Basic LaTeX-to-Markdown converter (KaTeX-compatible)."""
import argparse
import os
import re
import sys


def _escaped(s, i):
    """True if the character at s[i] is preceded by an odd number of backslashes."""
    n = 0
    j = i - 1
    while j >= 0 and s[j] == "\\":
        n += 1
        j -= 1
    return n % 2 == 1


def remove_comments(text):
    out = []
    for line in text.split("\n"):
        for i, ch in enumerate(line):
            if ch == "%" and not _escaped(line, i):
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def select_body(text):
    begin = "\\begin{document}"
    end = "\\end{document}"
    b = text.find(begin)
    if b == -1:
        return text
    rest = text[b + len(begin):]
    e = rest.find(end)
    if e != -1:
        rest = rest[:e]
    return rest


def strip_lines(text):
    return "\n".join(line.strip(" \t") for line in text.split("\n"))


def _find_unescaped(s, token, start):
    while True:
        i = s.find(token, start)
        if i == -1 or not _escaped(s, i):
            return i
        start = i + 1


def convert_display_math(text, store):
    """Replace \\[...\\] with $$ blocks; math content is stashed in `store`."""
    out = []
    pos = 0
    while True:
        o = _find_unescaped(text, "\\[", pos)
        if o == -1:
            break
        c = _find_unescaped(text, "\\]", o + 2)
        if c == -1:
            break
        before = text[pos:o]
        content = text[o + 2:c].strip(" \t\n")
        out.append(before.rstrip(" \t"))
        joined = "".join(out)
        if joined and not joined.endswith("\n"):
            out.append("\n")
        key = "\x00M%d\x00" % len(store)
        store.append(content)
        out.append("$$\n" + key + "\n$$")
        pos = c + 2
        # trim spaces after the block; ensure a line break before following text
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos < len(text) and text[pos] != "\n":
            out.append("\n")
    out.append(text[pos:])
    return "".join(out)


def _brace_arg(s, i):
    """s[i] must be '{'. Return (content, index after closing brace) or None."""
    if i >= len(s) or s[i] != "{":
        return None
    depth = 0
    j = i
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None


_CMD_RE = re.compile(r"\\(section|subsection|subsubsection|emph|textbf|vspace)(\*?)\s*(?=\{)")

_WRAP = {
    "section": ("## ", ""),
    "subsection": ("### ", ""),
    "subsubsection": ("#### ", ""),
    "emph": ("_", "_"),
    "textbf": ("**", "**"),
}


def convert_commands(s):
    out = []
    pos = 0
    while True:
        m = _CMD_RE.search(s, pos)
        if not m:
            break
        if _escaped(s, m.start()):
            out.append(s[pos:m.start() + 1])
            pos = m.start() + 1
            continue
        arg = _brace_arg(s, m.end())
        if arg is None:
            out.append(s[pos:m.end()])
            pos = m.end()
            continue
        content, after = arg
        out.append(s[pos:m.start()])
        name = m.group(1)
        if name != "vspace":
            pre, post = _WRAP[name]
            out.append(pre + convert_commands(content) + post)
        pos = after
    out.append(s[pos:])
    return "".join(out)


_SKIP_RE = re.compile(r"(?<!\\)((?:\\\\)*)\\(?:medskip|smallskip|bigskip)(?![A-Za-z])")


def delete_skips(s):
    return _SKIP_RE.sub(lambda m: m.group(1), s)


_LIST_RE = re.compile(
    r"\\begin\{(enumerate|itemize)\}|\\end\{(enumerate|itemize)\}|\\item(?![A-Za-z])"
)
_START_RE = re.compile(r"\[start=([0-9]+)\]")


class ListError(Exception):
    pass


def _clean(text):
    lines = [l.strip(" \t") for l in text.split("\n")]
    return "\n".join(l for l in lines if l)


def convert_lists(text):
    out = []
    pos = 0
    stack = []  # frames: dict(kind, num, depth, parent)
    entries = []  # [is_enum, text, open, has_content]
    seg_start = 0  # start of text segment inside list
    block_start = 0

    def add_text(seg):
        seg = _clean(seg)
        if not seg:
            return
        if entries and entries[-1][2]:
            e = entries[-1]
            e[1] = (e[1] + "\n" + seg) if e[3] else e[1] + seg
            e[3] = True
        else:
            entries.append([False, seg, False, True])

    def close_current():
        if entries:
            entries[-1][2] = False

    def render():
        parts = []
        prev = None
        for is_enum, t, _, _ in entries:
            if prev is not None:
                parts.append("\n\n" if (prev and is_enum) else "\n")
            parts.append(t.rstrip(" "))
            prev = is_enum
        return "".join(parts)

    for m in _LIST_RE.finditer(text):
        if m.start() < pos:
            continue
        if _escaped(text, m.start()):
            continue
        tok = m.group(0)
        if not stack:
            if m.group(1) is None:
                continue  # stray \item or \end outside lists
            out.append(text[pos:m.start()])
            block_start = m.start()
        else:
            add_text(text[seg_start:m.start()])
        end = m.end()
        if m.group(1):
            kind = m.group(1)
            frame = {"kind": kind, "num": 0, "depth": 0, "parent": None, "start": 1}
            if kind == "enumerate":
                if end < len(text) and text[end] == "[":
                    om = _START_RE.match(text, end)
                    if not om:
                        raise ListError("Error: unsupported enumerate options")
                    frame["start"] = int(om.group(1))
                    end = om.end()
                depth = 1
                if stack and stack[-1]["kind"] == "enumerate":
                    depth = stack[-1]["depth"] + 1
                if depth > 2:
                    raise ListError("Error: enumerate nesting deeper than one level")
                frame["depth"] = depth
                if depth == 2:
                    parent = stack[-1]
                    frame["parent"] = parent.get("last", parent["start"])
            close_current()
            stack.append(frame)
        elif m.group(2):
            kind = m.group(2)
            if stack[-1]["kind"] != kind:
                # mismatched end: treat as literal text
                add_text(tok)
                seg_start = end
                pos = end
                continue
            close_current()
            stack.pop()
            if not stack:
                out.append("\n" + render() + "\n")
                entries = []
        else:  # \item
            close_current()
            f = stack[-1]
            if f["kind"] == "itemize":
                entries.append([False, "-   ", True, False])
            else:
                cur = f["start"] + f["num"]
                f["num"] += 1
                if f["depth"] == 2:
                    letter = chr(ord("a") + (cur - 1) % 26)
                    label = "**%s.%s)** " % (f["parent"], letter)
                else:
                    f["last"] = cur
                    label = "**%d.** " % cur
                entries.append([True, label, True, False])
        pos = end
        seg_start = end
    if stack:
        add_text(text[seg_start:])
        out.append("\n" + render() + "\n")
    else:
        out.append(text[pos:])
    return "".join(out)


def convert(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = remove_comments(text)
    text = select_body(text)
    text = strip_lines(text)
    store = []
    text = convert_display_math(text, store)
    text = convert_lists(text)
    text = convert_commands(text)
    text = delete_skips(text)
    text = re.sub(r"\x00M(\d+)\x00", lambda m: store[int(m.group(1))], text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip("\n")
    return text + "\n" if text else ""


def main(argv=None):
    p = argparse.ArgumentParser(prog="l2m.py")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    args = p.parse_args(argv)
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            src = f.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("Error: cannot read '%s'\n" % args.input)
        return 1
    if args.output:
        out_path = args.output
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    else:
        out_path = os.path.splitext(os.path.basename(args.input))[0] + ".md"
    try:
        result = convert(src)
    except ListError as e:
        sys.stderr.write(str(e) + "\n")
        return 1
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
