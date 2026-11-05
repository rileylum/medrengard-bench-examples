#!/usr/bin/env python3
"""circopt - circuit tool (Part 1: CLI basics and `check`; Part 2: `eval`;
Part 3: vectors, richer expressions and `--radix`)."""

import json
import os
import re
import sys

VERSION = "0.1.0"

HELP_TEXT = """\
usage: circopt.py [--help] [--version] [--json] <command> [args]

Commands:
  check <file.circ>    Parse and validate a .circ circuit file and report
                       its interface.
  eval <file.circ> [--set name=value ...] [--default 0|1] [--allow-extra]
       [--radix bin|hex|dec] [--mode 2val|3val]
                       Evaluate the circuit (2-valued, or 3-valued with X)
                       and print outputs.

Global flags:
  --help               Show this help and exit.
  --version            Show version and exit.
  --json               Emit exactly one JSON object on stdout.

Exit codes:
  0 success, 1 CLI usage error, 2 parse error, 3 validation error,
  4 internal error, 10 non-equivalence.
"""

KEYWORDS = {"input", "output", "wire"}
OPERATORS = {
    "NOT": (1, 1),
    "BUF": (1, 1),
    "AND": (2, None),
    "OR": (2, None),
    "XOR": (2, None),
    "NAND": (2, None),
    "NOR": (2, None),
    "XNOR": (2, None),
    "MUX": (3, 3),
    "ITE": (3, 3),
    "REDUCE_AND": (1, 1),
    "REDUCE_OR": (1, 1),
    "REDUCE_XOR": (1, 1),
    "EQ": (2, 2),
}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CircoptError(Exception):
    type_name = "InternalError"
    exit_code = 4

    def __init__(self, message, file=None, line=None, col=None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col


def _mk(name, code):
    cls = type(name, (CircoptError,), {"type_name": name, "exit_code": code})
    return cls


CliUsageError = _mk("CliUsageError", 1)
FileNotFoundErr = _mk("FileNotFoundError", 1)
CircParseError = _mk("CircParseError", 2)
DeclarationAfterAssignmentError = _mk("DeclarationAfterAssignmentError", 3)
DuplicateNameError = _mk("DuplicateNameError", 3)
UndefinedNameError = _mk("UndefinedNameError", 3)
UnassignedSignalError = _mk("UnassignedSignalError", 3)
InputAssignmentError = _mk("InputAssignmentError", 3)
MultipleAssignmentError = _mk("MultipleAssignmentError", 3)
ArityError = _mk("ArityError", 3)
CycleError = _mk("CycleError", 3)
MissingInputError = _mk("MissingInputError", 1)
UnknownInputError = _mk("UnknownInputError", 1)
InputValueParseError = _mk("InputValueParseError", 2)
WidthMismatchError = _mk("WidthMismatchError", 3)
IndexOutOfBoundsError = _mk("IndexOutOfBoundsError", 3)
InputWidthMismatchError = _mk("InputWidthMismatchError", 3)
RadixNotAllowedIn3ValError = _mk("RadixNotAllowedIn3ValError", 1)


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

_BIN_DIGITS = set("01")
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_DEC_DIGITS = set("0123456789")


def parse_literal(text):
    """Parse a literal; return (value, width). Raise ValueError on bad syntax."""
    t = text
    if t in ("0", "1"):
        return int(t), 1
    if "'" in t:
        size_s, _, rest = t.partition("'")
        size_s = size_s.replace("_", "")
        if not size_s or not set(size_s) <= _DEC_DIGITS:
            raise ValueError("invalid literal size in %r" % text)
        width = int(size_s)
        if width < 1:
            raise ValueError("literal width must be at least 1 in %r" % text)
        if not rest:
            raise ValueError("missing base in literal %r" % text)
        base = rest[0].lower()
        digits = rest[1:].replace("_", "")
        if not digits:
            raise ValueError("missing digits in literal %r" % text)
        if base == "b":
            ok, radix = _BIN_DIGITS, 2
        elif base == "h":
            ok, radix = _HEX_DIGITS, 16
        elif base == "d":
            ok, radix = _DEC_DIGITS, 10
        else:
            raise ValueError("invalid base %r in literal %r" % (rest[0], text))
        if not set(digits) <= ok:
            raise ValueError("invalid digits in literal %r" % text)
        value = int(digits, radix)
        if value >= (1 << width):
            raise ValueError("literal %r value %d does not fit in %d bit(s)"
                             % (text, value, width))
        return value, width
    if len(t) >= 2 and t[0] == "0" and t[1] in "bBxX":
        digits = t[2:].replace("_", "")
        if not digits:
            raise ValueError("missing digits in literal %r" % text)
        if t[1] in "bB":
            if not set(digits) <= _BIN_DIGITS:
                raise ValueError("invalid binary digits in literal %r" % text)
            return int(digits, 2), len(digits)
        if not set(digits) <= _HEX_DIGITS:
            raise ValueError("invalid hex digits in literal %r" % text)
        return int(digits, 16), 4 * len(digits)
    raise ValueError("invalid literal %r" % text)


def mask(w):
    return (1 << w) - 1


def format_value(value, width, radix):
    if width == 1:
        return str(value)
    if radix == "hex":
        return "0x" + format(value, "0%dx" % ((width + 3) // 4))
    if radix == "dec":
        return str(value)
    return "0b" + format(value, "0%db" % width)


def parse_literal3(text):
    """Parse a 3-valued runtime literal; return ((value, xmask), width).

    Binary forms may contain X/x digits; hex and decimal forms may not.
    Raise ValueError on bad syntax."""
    t = text
    if t in ("X", "x"):
        return (0, 1), 1
    digits = None
    width = None
    if "'" in t:
        size_s, _, rest = t.partition("'")
        if rest[:1] in ("b", "B"):
            size_s2 = size_s.replace("_", "")
            if not size_s2 or not set(size_s2) <= _DEC_DIGITS:
                raise ValueError("invalid literal size in %r" % text)
            width = int(size_s2)
            if width < 1:
                raise ValueError("literal width must be at least 1 in %r" % text)
            digits = rest[1:].replace("_", "")
            if not digits:
                raise ValueError("missing digits in literal %r" % text)
    elif len(t) >= 2 and t[0] == "0" and t[1] in "bB":
        digits = t[2:].replace("_", "")
        if not digits:
            raise ValueError("missing digits in literal %r" % text)
    if digits is None:
        v, w = parse_literal(t)
        return (v, 0), w
    if not set(digits) <= set("01xX"):
        raise ValueError("invalid binary digits in literal %r" % text)
    if width is None:
        width = len(digits)
    elif len(digits) > width:
        extra = digits[:len(digits) - width]
        if set(extra) != {"0"}:
            raise ValueError("literal %r does not fit in %d bit(s)" % (text, width))
        digits = digits[len(digits) - width:]
    v = 0
    x = 0
    for ch in digits:
        v <<= 1
        x <<= 1
        if ch == "1":
            v |= 1
        elif ch in "xX":
            x |= 1
    return (v, x), width


def format_value3(val, width):
    v, x = val
    out = []
    for i in range(width - 1, -1, -1):
        if (x >> i) & 1:
            out.append("X")
        else:
            out.append(str((v >> i) & 1))
    if width == 1:
        return out[0]
    return "0b" + "".join(out)


# ---------------------------------------------------------------------------
# Lexer / parser
# ---------------------------------------------------------------------------

class Tok:
    __slots__ = ("kind", "value", "col")

    def __init__(self, kind, value, col):
        self.kind = kind  # 'ident', 'num', 'punct', 'eol'
        self.value = value
        self.col = col  # 1-based

    def __repr__(self):
        return "Tok(%s,%r,%d)" % (self.kind, self.value, self.col)


def tokenize(text, file, lineno):
    toks = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\f\v":
            i += 1
            continue
        if c == "#":
            break
        if c.isalpha() or c == "_":
            m = IDENT_RE.match(text, i)
            toks.append(Tok("ident", m.group(0), i + 1))
            i = m.end()
            continue
        if c.isdigit():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            if j < n and text[j] == "'":
                j += 1
                while j < n and (text[j].isalnum() or text[j] == "_"):
                    j += 1
            toks.append(Tok("num", text[i:j], i + 1))
            i = j
            continue
        if c in "(),=[]:{}":
            toks.append(Tok("punct", c, i + 1))
            i += 1
            continue
        raise CircParseError("unexpected character %r" % c, file, lineno, i + 1)
    # End-of-line token: placed at the last non-blank character position.
    stripped = text.split("#", 1)[0].rstrip()
    eol_col = max(1, len(stripped))
    toks.append(Tok("eol", None, eol_col))
    return toks


class Expr:
    pass


class Ident(Expr):
    def __init__(self, name, line, col):
        self.name, self.line, self.col = name, line, col


class Const(Expr):
    def __init__(self, value, width, line, col):
        self.value, self.width, self.line, self.col = value, width, line, col


class Index(Expr):
    def __init__(self, name, idx, line, col):
        self.name, self.idx, self.line, self.col = name, idx, line, col


class Slice(Expr):
    def __init__(self, name, hi, lo, line, col):
        self.name, self.hi, self.lo, self.line, self.col = name, hi, lo, line, col


class Concat(Expr):
    def __init__(self, parts, line, col):
        self.parts, self.line, self.col = parts, line, col


class Call(Expr):
    def __init__(self, op, args, line, col):
        self.op, self.args, self.line, self.col = op, args, line, col


class LineParser:
    def __init__(self, toks, file, lineno):
        self.toks = toks
        self.pos = 0
        self.file = file
        self.lineno = lineno

    def peek(self, k=0):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.pos]
        if t.kind != "eol":
            self.pos += 1
        return t

    def err(self, msg, tok):
        return CircParseError(msg, self.file, self.lineno, tok.col)

    def expect_punct(self, p):
        t = self.peek()
        if t.kind == "punct" and t.value == p:
            return self.next()
        what = "end of line" if t.kind == "eol" else repr(t.value)
        raise self.err("expected %r, found %s" % (p, what), t)

    def is_punct(self, p, k=0):
        t = self.peek(k)
        return t.kind == "punct" and t.value == p

    def expect_int(self):
        t = self.peek()
        if t.kind == "num" and t.value.isdigit():
            self.next()
            return int(t.value)
        what = "end of line" if t.kind == "eol" else repr(t.value)
        raise self.err("expected integer, found %s" % what, t)

    def parse_range(self):
        """Parse '[' int (':' int)? ']' ; returns (a, b or None)."""
        self.expect_punct("[")
        a = self.expect_int()
        b = None
        if self.is_punct(":"):
            self.next()
            b = self.expect_int()
        self.expect_punct("]")
        return a, b

    def parse_expr(self):
        t = self.peek()
        if t.kind == "eol":
            raise self.err("unexpected end of line, expected expression", t)
        if t.kind == "num":
            self.next()
            try:
                value, width = parse_literal(t.value)
            except ValueError as ex:
                raise self.err(str(ex), t)
            return Const(value, width, self.lineno, t.col)
        if t.kind == "punct" and t.value == "{":
            self.next()
            parts = []
            while True:
                parts.append(self.parse_expr())
                st = self.peek()
                if st.kind == "punct" and st.value == ",":
                    self.next()
                    continue
                if st.kind == "punct" and st.value == "}":
                    self.next()
                    break
                what = "end of line" if st.kind == "eol" else repr(st.value)
                raise self.err("expected ',' or '}', found %s" % what, st)
            return Concat(parts, self.lineno, t.col)
        if t.kind == "ident":
            self.next()
            nt = self.peek()
            if nt.kind == "punct" and nt.value == "(":
                op = t.value.upper()
                if op not in OPERATORS:
                    raise self.err("unknown operator %r" % t.value, t)
                self.next()
                args = []
                if self.peek().kind == "punct" and self.peek().value == ")":
                    self.next()
                    return Call(op, args, self.lineno, t.col)
                while True:
                    args.append(self.parse_expr())
                    st = self.peek()
                    if st.kind == "punct" and st.value == ",":
                        self.next()
                        continue
                    if st.kind == "punct" and st.value == ")":
                        self.next()
                        break
                    what = "end of line" if st.kind == "eol" else repr(st.value)
                    raise self.err("expected ',' or ')', found %s" % what, st)
                return Call(op, args, self.lineno, t.col)
            if t.value == "X":
                raise self.err("literal X values are not allowed in .circ files", t)
            if t.value.lower() in KEYWORDS:
                raise self.err("keyword %r cannot be used as an expression" % t.value, t)
            if self.is_punct("["):
                a, b = self.parse_range()
                if b is None:
                    return Index(t.value, a, self.lineno, t.col)
                return Slice(t.value, a, b, self.lineno, t.col)
            return Ident(t.value, self.lineno, t.col)
        raise self.err("unexpected %r, expected expression" % t.value, t)


def parse_circ(text, file):
    """Return a list of parsed line items (syntax only)."""
    items = []
    lines = text.split("\n")
    for idx, raw in enumerate(lines):
        lineno = idx + 1
        line = raw.rstrip("\r")
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        toks = tokenize(line, file, lineno)
        if toks[0].kind == "eol":
            continue
        first = toks[0]
        p = LineParser(toks, file, lineno)
        if (first.kind == "ident" and first.value.lower() in KEYWORDS
                and not (toks[1].kind == "punct" and toks[1].value == "=")):
            kind = first.value.lower()
            p.next()
            names = []
            while True:
                t = p.peek()
                if t.kind == "eol":
                    break
                if t.kind != "ident":
                    raise p.err("expected identifier in %s declaration, found %r"
                                % (kind, t.value), t)
                if t.value.lower() in KEYWORDS:
                    raise p.err("keyword %r cannot be used as a name" % t.value, t)
                if t.value == "X":
                    raise p.err("'X' is reserved for the unknown literal", t)
                p.next()
                msb, lsb = 0, 0
                if p.is_punct("["):
                    rt = p.peek()
                    msb, lsb = p.parse_range()
                    if lsb is None:
                        raise p.err("vector declaration requires [msb:lsb]", rt)
                    if msb < lsb:
                        raise p.err("invalid range [%d:%d]: msb must be >= lsb"
                                    % (msb, lsb), rt)
                names.append((t.value, t.col, msb, lsb))
                nt = p.peek()
                if nt.kind == "punct" and nt.value == ",":
                    p.next()
            if not names:
                raise p.err("%s declaration requires at least one name" % kind,
                            p.peek())
            items.append(("decl", kind, names, lineno, first.col))
        else:
            if first.kind != "ident":
                raise p.err("expected declaration or assignment", first)
            if first.value.lower() in KEYWORDS:
                raise p.err("keyword %r cannot be assigned" % first.value, first)
            if first.value == "X":
                raise p.err("literal X values are not allowed in .circ files", first)
            p.next()
            p.expect_punct("=")
            expr = p.parse_expr()
            t = p.peek()
            if t.kind != "eol":
                raise p.err("unexpected %r after expression" % t.value, t)
            items.append(("assign", first.value, expr, lineno, first.col))
    return items


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class Circuit:
    def __init__(self):
        self.inputs = []
        self.outputs = []
        self.wires = []
        self.kinds = {}
        self.ranges = {}  # name -> (msb, lsb)
        self.assigns = {}  # name -> expr
        self.order = []


def expr_refs(e, out):
    if isinstance(e, (Ident, Index, Slice)):
        out.append(e)
    elif isinstance(e, Call):
        for a in e.args:
            expr_refs(a, out)
    elif isinstance(e, Concat):
        for a in e.parts:
            expr_refs(a, out)


def check_arity(e, file):
    if isinstance(e, Call):
        lo, hi = OPERATORS[e.op]
        n = len(e.args)
        if n < lo or (hi is not None and n > hi):
            if hi == lo:
                want = "exactly %d" % lo
            else:
                want = "at least %d" % lo
            raise ArityError("%s expects %s argument(s), got %d" % (e.op, want, n),
                             file, e.line, e.col)
        for a in e.args:
            check_arity(a, file)
    elif isinstance(e, Concat):
        for a in e.parts:
            check_arity(a, file)


BITWISE_NARY = {"AND", "OR", "XOR", "NAND", "NOR", "XNOR"}


def check_width(e, c, file):
    """Compute and annotate expression widths (e.w)."""
    if isinstance(e, Const):
        e.w = e.width
    elif isinstance(e, Ident):
        msb, lsb = c.ranges[e.name]
        e.w = msb - lsb + 1
    elif isinstance(e, Index):
        msb, lsb = c.ranges[e.name]
        if not (lsb <= e.idx <= msb):
            raise IndexOutOfBoundsError(
                "index %d out of bounds for %r [%d:%d] (width %d)"
                % (e.idx, e.name, msb, lsb, msb - lsb + 1), file, e.line, e.col)
        e.w = 1
    elif isinstance(e, Slice):
        msb, lsb = c.ranges[e.name]
        if e.hi < e.lo or not (lsb <= e.lo and e.hi <= msb):
            raise IndexOutOfBoundsError(
                "slice [%d:%d] out of bounds for %r [%d:%d] (width %d)"
                % (e.hi, e.lo, e.name, msb, lsb, msb - lsb + 1),
                file, e.line, e.col)
        e.w = e.hi - e.lo + 1
    elif isinstance(e, Concat):
        e.w = sum(check_width(p, c, file) for p in e.parts)
    else:
        ws = [check_width(a, c, file) for a in e.args]
        op = e.op
        if op in ("NOT", "BUF"):
            e.w = ws[0]
        elif op in BITWISE_NARY:
            for w in ws[1:]:
                if w != ws[0]:
                    raise WidthMismatchError(
                        "%s operand widths differ: %d vs %d" % (op, ws[0], w),
                        file, e.line, e.col)
            e.w = ws[0]
        elif op in ("MUX", "ITE"):
            if ws[0] != 1:
                raise WidthMismatchError(
                    "%s selector must have width 1, got width %d" % (op, ws[0]),
                    file, e.line, e.col)
            if ws[1] != ws[2]:
                raise WidthMismatchError(
                    "%s data operand widths differ: %d vs %d" % (op, ws[1], ws[2]),
                    file, e.line, e.col)
            e.w = ws[1]
        elif op.startswith("REDUCE_"):
            e.w = 1
        elif op == "EQ":
            if ws[0] != ws[1]:
                raise WidthMismatchError(
                    "EQ operand widths differ: %d vs %d" % (ws[0], ws[1]),
                    file, e.line, e.col)
            e.w = 1
        else:
            raise CircoptError("unknown operator %s" % op)
    return e.w


def build_circuit(items, file):
    c = Circuit()
    seen_assign = False
    # Pass 1: declarations
    for it in items:
        if it[0] == "decl":
            _, kind, names, lineno, col = it
            if seen_assign:
                raise DeclarationAfterAssignmentError(
                    "%s declaration after assignment" % kind, file, lineno, col)
            for name, ncol, msb, lsb in names:
                if name in c.kinds:
                    raise DuplicateNameError(
                        "name %r already declared as %s" % (name, c.kinds[name]),
                        file, lineno, ncol)
                c.kinds[name] = kind
                c.ranges[name] = (msb, lsb)
                {"input": c.inputs, "output": c.outputs, "wire": c.wires}[kind].append(name)
        else:
            seen_assign = True
    # Pass 2: assignments
    for it in items:
        if it[0] != "assign":
            continue
        _, lhs, expr, lineno, col = it
        k = c.kinds.get(lhs)
        if k is None:
            raise UndefinedNameError("assignment to undeclared name %r" % lhs,
                                     file, lineno, col)
        if k == "input":
            raise InputAssignmentError("cannot assign to input %r" % lhs,
                                       file, lineno, col)
        if lhs in c.assigns:
            raise MultipleAssignmentError("signal %r assigned more than once" % lhs,
                                          file, lineno, col)
        refs = []
        expr_refs(expr, refs)
        for r in refs:
            if r.name not in c.kinds:
                raise UndefinedNameError("undefined name %r" % r.name,
                                         file, r.line, r.col)
        check_arity(expr, file)
        ew = check_width(expr, c, file)
        msb, lsb = c.ranges[lhs]
        lw = msb - lsb + 1
        if ew != lw:
            raise WidthMismatchError(
                "assignment to %r: target width %d, expression width %d"
                % (lhs, lw, ew), file, lineno, col)
        c.assigns[lhs] = expr
        c.order.append(lhs)
    # Unassigned
    for name in c.outputs + c.wires:
        if name not in c.assigns:
            raise UnassignedSignalError(
                "%s %r is never assigned" % (c.kinds[name], name), file)
    # Cycles
    deps = {}
    for name, e in c.assigns.items():
        refs = []
        expr_refs(e, refs)
        ds = []
        for r in refs:
            if r.name in c.assigns and r.name not in ds:
                ds.append(r.name)
        deps[name] = ds
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in deps}
    for start in sorted(deps):
        if color[start] != WHITE:
            continue
        stack = [(start, iter(deps[start]))]
        path = [start]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if color[nxt] == GRAY:
                i = path.index(nxt)
                cyc = path[i:] + [nxt]
                raise CycleError("combinational cycle detected: " + " -> ".join(cyc),
                                 file)
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, iter(deps[nxt])))
    return c


def load_circ(path):
    if not os.path.isfile(path):
        raise FileNotFoundErr("file not found: %s" % path, path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise FileNotFoundErr("cannot read file %s: %s" % (path, e), path)
    except UnicodeDecodeError as e:
        raise CircParseError("file is not valid UTF-8: %s" % e, path, 1, 1)
    if text.startswith("﻿"):
        text = text[1:]
    items = parse_circ(text, path)
    return build_circuit(items, path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def sig_list(c, names):
    return [{"name": n, "msb": c.ranges[n][0], "lsb": c.ranges[n][1]}
            for n in sorted(names)]


def sig_text(c, names):
    parts = []
    for n in sorted(names):
        msb, lsb = c.ranges[n]
        if (msb, lsb) == (0, 0):
            parts.append(n)
        else:
            parts.append("%s[%d:%d]" % (n, msb, lsb))
    return " ".join(parts) or "(none)"


def cmd_check(args):
    if len(args) != 1:
        raise CliUsageError("check requires exactly one file argument")
    c = load_circ(args[0])
    result = {
        "format": "circ",
        "inputs": sig_list(c, c.inputs),
        "outputs": sig_list(c, c.outputs),
    }
    text = "OK %s\ninputs: %s\noutputs: %s" % (
        args[0], sig_text(c, c.inputs), sig_text(c, c.outputs))
    return result, text


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def parse_bit(text, what):
    v = text.strip()
    if v in ("0", "1"):
        return int(v)
    raise InputValueParseError("invalid value %r for %s (expected 0 or 1)" % (text, what))


def parse_runtime_literal(text, what):
    v = text.strip()
    try:
        return parse_literal(v)
    except ValueError as ex:
        raise InputValueParseError("invalid value %r for %s: %s" % (text, what, ex))


def parse_runtime_literal3(text, what):
    v = text.strip()
    try:
        return parse_literal3(v)
    except ValueError as ex:
        raise InputValueParseError("invalid value %r for %s: %s" % (text, what, ex))


def eval_expr(e, env, c):
    if isinstance(e, Const):
        return e.value
    if isinstance(e, Ident):
        return env[e.name]
    if isinstance(e, Index):
        lsb = c.ranges[e.name][1]
        return (env[e.name] >> (e.idx - lsb)) & 1
    if isinstance(e, Slice):
        lsb = c.ranges[e.name][1]
        return (env[e.name] >> (e.lo - lsb)) & mask(e.w)
    if isinstance(e, Concat):
        r = 0
        for p in e.parts:
            r = (r << p.w) | eval_expr(p, env, c)
        return r
    vals = [eval_expr(a, env, c) for a in e.args]
    op = e.op
    m = mask(e.w)
    if op == "BUF":
        return vals[0]
    if op == "NOT":
        return ~vals[0] & m
    if op in ("AND", "NAND"):
        r = vals[0]
        for v in vals[1:]:
            r &= v
        return r if op == "AND" else ~r & m
    if op in ("OR", "NOR"):
        r = vals[0]
        for v in vals[1:]:
            r |= v
        return r if op == "OR" else ~r & m
    if op in ("XOR", "XNOR"):
        r = 0
        for v in vals:
            r ^= v
        return r if op == "XOR" else ~r & m
    if op in ("MUX", "ITE"):
        return vals[1] if vals[0] else vals[2]
    if op == "REDUCE_AND":
        return 1 if vals[0] == mask(e.args[0].w) else 0
    if op == "REDUCE_OR":
        return 1 if vals[0] else 0
    if op == "REDUCE_XOR":
        return bin(vals[0]).count("1") & 1
    if op == "EQ":
        return 1 if vals[0] == vals[1] else 0
    raise CircoptError("unknown operator %s" % op)


def _not3(a, m):
    v, x = a
    return (~v & ~x & m, x)


def _and3(a, b, m):
    k0 = (~a[0] & ~a[1] & m) | (~b[0] & ~b[1] & m)
    one = a[0] & b[0]
    return (one, m & ~k0 & ~one)


def _or3(a, b, m):
    one = (a[0] | b[0]) & m
    k0 = (~a[0] & ~a[1]) & (~b[0] & ~b[1]) & m
    return (one, m & ~k0 & ~one)


def _xor3(a, b, m):
    x = (a[1] | b[1]) & m
    return ((a[0] ^ b[0]) & ~x & m, x)


def eval_expr3(e, env, c):
    """3-valued evaluation; values are (value_bits, x_bits) pairs."""
    if isinstance(e, Const):
        return (e.value, 0)
    if isinstance(e, Ident):
        return env[e.name]
    if isinstance(e, Index):
        lsb = c.ranges[e.name][1]
        v, x = env[e.name]
        sh = e.idx - lsb
        return ((v >> sh) & 1, (x >> sh) & 1)
    if isinstance(e, Slice):
        lsb = c.ranges[e.name][1]
        v, x = env[e.name]
        sh = e.lo - lsb
        m = mask(e.w)
        return ((v >> sh) & m, (x >> sh) & m)
    if isinstance(e, Concat):
        rv = 0
        rx = 0
        for p in e.parts:
            pv, px = eval_expr3(p, env, c)
            rv = (rv << p.w) | pv
            rx = (rx << p.w) | px
        return (rv, rx)
    vals = [eval_expr3(a, env, c) for a in e.args]
    op = e.op
    m = mask(e.w)
    if op == "BUF":
        return vals[0]
    if op == "NOT":
        return _not3(vals[0], m)
    if op in ("AND", "NAND", "OR", "NOR", "XOR", "XNOR"):
        base = {"NAND": "AND", "NOR": "OR", "XNOR": "XOR"}.get(op, op)
        fn = {"AND": _and3, "OR": _or3, "XOR": _xor3}[base]
        r = vals[0]
        for v in vals[1:]:
            r = fn(r, v, m)
        return r if base == op else _not3(r, m)
    if op in ("MUX", "ITE"):
        sv, sx = vals[0]
        a, b = vals[1], vals[2]
        if not sx:
            return a if sv else b
        x = (a[1] | b[1] | (a[0] ^ b[0])) & m
        return (a[0] & ~x & m, x)
    if op in ("REDUCE_AND", "REDUCE_OR", "REDUCE_XOR"):
        w = e.args[0].w
        v, x = vals[0]
        am = mask(w)
        if op == "REDUCE_AND":
            if (~v & ~x & am):
                return (0, 0)
            return (0, 1) if x else (1, 0)
        if op == "REDUCE_OR":
            if v & am:
                return (1, 0)
            return (0, 1) if x else (0, 0)
        if x:
            return (0, 1)
        return (bin(v).count("1") & 1, 0)
    if op == "EQ":
        (av, ax), (bv, bx) = vals
        known = ~(ax | bx) & mask(e.args[0].w)
        if (av ^ bv) & known:
            return (0, 0)
        if ax | bx:
            return (0, 1)
        return (1, 0)
    raise CircoptError("unknown operator %s" % op)


def evaluate3(c, inputs):
    env = dict(inputs)
    for name in topo_order(c):
        env[name] = eval_expr3(c.assigns[name], env, c)
    return env


def topo_order(c):
    order = []
    state = {}
    for start in c.order:
        if start in state:
            continue
        stack = [(start, None)]
        while stack:
            node, it = stack.pop()
            if it is None:
                if node in state:
                    continue
                state[node] = 1
                refs = []
                expr_refs(c.assigns[node], refs)
                it = iter([r.name for r in refs if r.name in c.assigns])
            nxt = None
            for d in it:
                if d not in state:
                    nxt = d
                    break
            if nxt is not None:
                stack.append((node, it))
                stack.append((nxt, None))
            else:
                state[node] = 2
                order.append(node)
    return order


def evaluate(c, inputs):
    env = dict(inputs)
    for name in topo_order(c):
        env[name] = eval_expr(c.assigns[name], env, c)
    return env


def cmd_eval(args, opts):
    if len(args) != 1:
        raise CliUsageError("eval requires exactly one file argument")
    mode = "2val"
    if opts.get("--mode"):
        mode = opts["--mode"][-1].strip().lower()
        if mode not in ("2val", "3val"):
            raise CliUsageError("invalid --mode %r (expected 2val or 3val)"
                                % opts["--mode"][-1])
    tri = mode == "3val"
    radix = "bin"
    if opts.get("--radix"):
        radix = opts["--radix"][-1].strip().lower()
        if radix not in ("bin", "hex", "dec"):
            raise CliUsageError("invalid --radix %r (expected bin, hex or dec)"
                                % opts["--radix"][-1])
    if tri and radix != "bin":
        raise RadixNotAllowedIn3ValError(
            "--radix %s is not allowed with --mode 3val (only bin)" % radix)
    sets = []
    for s in opts.get("--set", []):
        if "=" not in s:
            raise InputValueParseError("invalid --set %r (expected name=value)" % s)
        name, val = s.split("=", 1)
        name = name.strip()
        if not name:
            raise InputValueParseError("invalid --set %r (empty name)" % s)
        if tri:
            sets.append((name, val, parse_runtime_literal3(val, name)))
        else:
            sets.append((name, val, parse_runtime_literal(val, name)))
    default = None
    if opts.get("--default"):
        d = opts["--default"][-1].strip()
        if tri and d in ("X", "x"):
            default = "X"
        else:
            default = parse_bit(opts["--default"][-1], "--default")
    allow_extra = bool(opts.get("--allow-extra"))

    c = load_circ(args[0])
    inputs = {}
    input_set = set(c.inputs)
    for name, raw, (v, w) in sets:
        if name not in input_set:
            if allow_extra:
                continue
            raise UnknownInputError("unknown input %r" % name)
        msb, lsb = c.ranges[name]
        dw = msb - lsb + 1
        if w != dw:
            raise InputWidthMismatchError(
                "value %r for input %r has width %d, but declared width is %d"
                % (raw.strip(), name, w, dw))
        inputs[name] = v
    missing = [n for n in sorted(c.inputs) if n not in inputs]
    if missing:
        if default is None:
            raise MissingInputError("missing value for input(s): %s"
                                    % ", ".join(missing))
        for n in missing:
            msb, lsb = c.ranges[n]
            m = mask(msb - lsb + 1)
            if tri:
                inputs[n] = (0, m) if default == "X" else ((m if default else 0), 0)
            else:
                inputs[n] = m if default else 0
    env = evaluate3(c, inputs) if tri else evaluate(c, inputs)
    outs = sorted(c.outputs)
    result = {
        "mode": mode,
        "radix": radix,
        "outputs": [],
    }
    lines = []
    for n in outs:
        msb, lsb = c.ranges[n]
        if tri:
            val = format_value3(env[n], msb - lsb + 1)
        else:
            val = format_value(env[n], msb - lsb + 1, radix)
        result["outputs"].append({"name": n, "msb": msb, "lsb": lsb, "value": val})
        lines.append("%s=%s" % (n, val))
    text = "\n".join(lines)
    return result, text


COMMANDS = {"check": cmd_check, "eval": cmd_eval}
# command -> {flag: takes_value}
COMMAND_FLAGS = {
    "check": {},
    "eval": {"--set": True, "--default": True, "--allow-extra": False,
             "--radix": True, "--mode": True},
}
ALL_VALUE_FLAGS = {f for d in COMMAND_FLAGS.values() for f, v in d.items() if v}
ALL_CMD_FLAGS = {f for d in COMMAND_FLAGS.values() for f in d}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def emit_json(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")


def main(argv):
    json_mode = "--json" in argv
    want_help = False
    want_version = False
    positional = []
    bad_flag = None
    missing_value = None
    after_dd = False
    opts = {}  # flag -> list of values (or True markers)
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if after_dd:
            positional.append(a)
        elif a == "--":
            after_dd = True
        elif a == "--json":
            pass
        elif a in ("--help", "-h"):
            want_help = True
        elif a == "--version":
            want_version = True
        elif a.startswith("-") and a != "-":
            flag, eq, val = a.partition("=")
            if flag in ALL_CMD_FLAGS:
                if flag in ALL_VALUE_FLAGS:
                    if not eq:
                        if i < len(argv):
                            val = argv[i]
                            i += 1
                        else:
                            if missing_value is None:
                                missing_value = flag
                            continue
                    opts.setdefault(flag, []).append(val)
                elif eq:
                    if bad_flag is None:
                        bad_flag = a
                else:
                    opts.setdefault(flag, []).append(True)
            elif bad_flag is None:
                bad_flag = a
        else:
            positional.append(a)

    command = "__cli__"
    if positional and positional[0] in COMMANDS:
        command = positional[0]

    if want_help:
        sys.stdout.write(HELP_TEXT)
        return 0
    if want_version and not positional:
        if json_mode:
            emit_json({"ok": True, "command": "__version__", "version": VERSION})
        else:
            sys.stdout.write(VERSION + "\n")
        return 0

    try:
        try:
            if bad_flag is not None:
                raise CliUsageError("unknown flag: %s" % bad_flag)
            if want_version:
                raise CliUsageError("--version cannot be combined with a command")
            if not positional:
                raise CliUsageError("no command given (try --help)")
            if positional[0] not in COMMANDS:
                raise CliUsageError("unknown command: %s" % positional[0])
            allowed = COMMAND_FLAGS[command]
            for f in opts:
                if f not in allowed:
                    raise CliUsageError("flag %s is not valid for %s" % (f, command))
            if missing_value is not None:
                raise CliUsageError("flag %s requires a value" % missing_value)
            if command == "check":
                result, text = cmd_check(positional[1:])
            else:
                result, text = COMMANDS[command](positional[1:], opts)
        except CircoptError:
            raise
        except RecursionError:
            raise CircoptError("expression nesting too deep")
        except Exception as e:  # pragma: no cover
            raise CircoptError("internal error: %s: %s" % (type(e).__name__, e))
    except CircoptError as e:
        if json_mode:
            emit_json({
                "ok": False,
                "command": command,
                "exit_code": e.exit_code,
                "error": {
                    "type": e.type_name,
                    "message": e.message,
                    "file": e.file,
                    "line": e.line,
                    "col": e.col,
                },
            })
        else:
            loc = ""
            if e.file is not None:
                loc = e.file
                if e.line is not None:
                    loc += ":%d" % e.line
                    if e.col is not None:
                        loc += ":%d" % e.col
                loc += ": "
            sys.stderr.write("%serror: %s: %s\n" % (loc, e.type_name, e.message))
        return e.exit_code

    if json_mode:
        out = {"ok": True, "command": command}
        out.update(result)
        emit_json(out)
    else:
        if text:
            sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    sys.exit(main(sys.argv[1:]))
