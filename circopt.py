#!/usr/bin/env python3
"""circopt - circuit tool (Part 1: CLI basics and `check`; Part 2: `eval`;
Part 3: vectors, richer expressions and `--radix`;
Parts 4-6: 3-valued eval, input formats, stats/lint/dot)."""

import json
import os
import re
import sys

VERSION = "0.1.0"

HELP_TEXT = """\
usage: circopt.py [--help] [--version] [--json] <command> [args]

Commands:
  check <file> [--format auto|circ|json|bench]
                       Parse and validate a circuit file (.circ, .json or
                       .bench) and report its interface.
  eval <file> [--format auto|circ|json|bench] [--set name=value ...] [--default 0|1] [--allow-extra]
       [--radix bin|hex|dec] [--mode 2val|3val]
                       Evaluate the circuit (2-valued, or 3-valued with X)
                       and print outputs.
  stats <file> [--format ...]
                       Print circuit metrics (counts, depth, op histogram).
  lint <file> [--format ...] [--max-input-bits N]
                       Report warnings (unused inputs, constant outputs).
  dot <file> -o <dot_file> [--format ...] [--cone out1,out2,...]
                       Export the circuit as a Graphviz DOT graph.
  cone <file> -o <out_file> --outputs y,z [--format ...] [--out-format circ|json]
                       Extract the subcircuit needed for the given outputs.
  truth-table <file> --max-input-bits N [--format ...] [--radix bin|hex|dec]
       [-o <path>] [--out-format csv|json]
                       Print the exhaustive truth table (2val only).
  equiv <a> <b> --max-input-bits N [--format ...] [--trials T] [--outputs y,z]
                       Check functional equivalence (exhaustive or randomized).

Global flags:
  --help               Show this help and exit.
  --version            Show version and exit.
  --json               Emit exactly one JSON object on stdout.
  --seed <n>           Seed for randomized behavior (non-negative integer).

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
UnknownInputFormatError = _mk("UnknownInputFormatError", 1)
JsonParseError = _mk("JsonParseError", 2)
JsonSchemaError = _mk("JsonSchemaError", 3)
BenchParseError = _mk("BenchParseError", 2)
RedefinitionError = _mk("RedefinitionError", 3)


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
# JSON and BENCH formats
# ---------------------------------------------------------------------------

FORMAT_BY_EXT = {".circ": "circ", ".json": "json", ".bench": "bench"}
FORMATS = ("auto", "circ", "json", "bench")


def resolve_format(path, fmt):
    if fmt == "auto":
        ext = os.path.splitext(path)[1].lower()
        if ext not in FORMAT_BY_EXT:
            raise UnknownInputFormatError(
                "cannot determine input format from extension %r of %s "
                "(use --format circ|json|bench)" % (ext, path), path)
        return FORMAT_BY_EXT[ext]
    return fmt


def read_text(path, decode_err_cls):
    if not os.path.isfile(path):
        raise FileNotFoundErr("file not found: %s" % path, path)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise FileNotFoundErr("cannot read file %s: %s" % (path, e), path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise decode_err_cls("file is not valid UTF-8: %s" % e, path, 1, 1)
    if text.startswith("﻿"):
        text = text[1:]
    return text


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_rhs(text, file, where):
    if "\n" in text or "\r" in text:
        raise CircParseError("%s: expression must not contain newlines" % where,
                             file)
    toks = tokenize(text, file, None)
    p = LineParser(toks, file, None)
    expr = p.parse_expr()
    t = p.peek()
    if t.kind != "eol":
        raise p.err("unexpected %r after expression" % t.value, t)
    return expr


def load_json_circuit(path):
    text = read_text(path, JsonParseError)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise JsonParseError("invalid JSON: %s" % e.msg, path, e.lineno, e.colno)
    except ValueError as e:
        raise JsonParseError("invalid JSON: %s" % e, path)

    def bad(msg):
        return JsonSchemaError(msg, path)

    if not isinstance(doc, dict):
        raise bad("top-level JSON value must be an object")
    required = ("format_version", "inputs", "outputs", "wires", "assignments")
    for k in required:
        if k not in doc:
            raise bad("missing required key %r" % k)
    for k in doc:
        if k not in required:
            raise bad("unknown key %r" % k)
    fv = doc["format_version"]
    if not _is_int(fv) or fv != 1:
        raise bad("format_version must be 1, got %s" % json.dumps(fv))

    items = []
    for kind, key in (("input", "inputs"), ("output", "outputs"), ("wire", "wires")):
        ports = doc[key]
        if not isinstance(ports, list):
            raise bad("%r must be an array" % key)
        names = []
        for i, p in enumerate(ports):
            where = "%s[%d]" % (key, i)
            if not isinstance(p, dict):
                raise bad("%s must be an object" % where)
            for k in ("name", "msb", "lsb"):
                if k not in p:
                    raise bad("%s: missing required key %r" % (where, k))
            for k in p:
                if k not in ("name", "msb", "lsb"):
                    raise bad("%s: unknown key %r" % (where, k))
            name, msb, lsb = p["name"], p["msb"], p["lsb"]
            if not isinstance(name, str) or not IDENT_RE.fullmatch(name):
                raise bad("%s: name must be an identifier string, got %s"
                          % (where, json.dumps(name)))
            if name.lower() in KEYWORDS or name == "X":
                raise bad("%s: %r is a reserved word" % (where, name))
            if not _is_int(msb) or not _is_int(lsb):
                raise bad("%s: msb and lsb must be integers" % where)
            if lsb < 0 or msb < lsb:
                raise bad("%s: invalid range [%d:%d] (need msb >= lsb >= 0)"
                          % (where, msb, lsb))
            names.append((name, None, msb, lsb))
        if names:
            items.append(("decl", kind, names, None, None))

    assigns = doc["assignments"]
    if not isinstance(assigns, list):
        raise bad("'assignments' must be an array")
    parsed = []
    for i, a in enumerate(assigns):
        where = "assignments[%d]" % i
        if not isinstance(a, dict):
            raise bad("%s must be an object" % where)
        for k in ("lhs", "rhs"):
            if k not in a:
                raise bad("%s: missing required key %r" % (where, k))
        for k in a:
            if k not in ("lhs", "rhs"):
                raise bad("%s: unknown key %r" % (where, k))
        lhs, rhs = a["lhs"], a["rhs"]
        if not isinstance(lhs, str) or not IDENT_RE.fullmatch(lhs):
            raise bad("%s: lhs must be an identifier string, got %s"
                      % (where, json.dumps(lhs)))
        if not isinstance(rhs, str):
            raise bad("%s: rhs must be a string" % where)
        parsed.append((lhs, rhs, where))
    for lhs, rhs, where in parsed:
        expr = _parse_rhs(rhs, path, where)
        items.append(("assign", lhs, expr, None, None))
    return build_circuit(items, path)


BENCH_OPS = {"NOT", "BUF", "AND", "OR", "XOR", "NAND", "NOR", "XNOR"}


class BenchLineParser(LineParser):
    def err(self, msg, tok):
        return BenchParseError(msg, self.file, self.lineno, tok.col)

    def expect_ident(self, what):
        t = self.peek()
        if t.kind == "ident":
            self.next()
            nt = self.peek()
            if nt.kind == "punct" and nt.value in "[]":
                raise self.err("vector syntax is not allowed in BENCH files "
                               "(signals are scalar)", nt)
            return t
        if t.kind == "num":
            raise self.err("literals are not allowed in BENCH files (%r)"
                           % t.value, t)
        if t.kind == "punct" and t.value in "[]":
            raise self.err("vector syntax is not allowed in BENCH files", t)
        found = "end of line" if t.kind == "eol" else repr(t.value)
        raise self.err("expected %s, found %s" % (what, found), t)

    def parse_bench_expr(self):
        t = self.expect_ident("identifier or operator")
        nt = self.peek()
        if nt.kind == "punct" and nt.value == "(":
            op = t.value.upper()
            if op not in BENCH_OPS:
                raise self.err("unknown or unsupported BENCH operator %r"
                               % t.value, t)
            self.next()
            args = []
            if self.is_punct(")"):
                self.next()
                return Call(op, args, self.lineno, t.col)
            while True:
                args.append(self.parse_bench_expr())
                st = self.peek()
                if st.kind == "punct" and st.value == ",":
                    self.next()
                    continue
                if st.kind == "punct" and st.value == ")":
                    self.next()
                    break
                if st.kind == "num":
                    raise self.err("literals are not allowed in BENCH files", st)
                if st.kind == "punct" and st.value in "[]":
                    raise self.err("vector syntax is not allowed in BENCH files", st)
                found = "end of line" if st.kind == "eol" else repr(st.value)
                raise self.err("expected ',' or ')', found %s" % found, st)
            return Call(op, args, self.lineno, t.col)
        return Ident(t.value, self.lineno, t.col)


def _bench_tokenize(line, file, lineno):
    try:
        return tokenize(line, file, lineno)
    except CircParseError as e:
        raise BenchParseError(e.message, e.file, e.line, e.col)


def load_bench_circuit(path):
    text = read_text(path, BenchParseError)
    inputs = []          # (name, line, col)
    outputs = []         # (name, line, col)
    assigns = []         # (lhs, expr, line, col)
    defined = {}         # name -> description of first definition
    for idx, raw in enumerate(text.split("\n")):
        lineno = idx + 1
        line = raw.rstrip("\r")
        if not line.strip() or line.strip().startswith("#"):
            continue
        if "[" in line.split("#", 1)[0] or "]" in line.split("#", 1)[0]:
            s = line.split("#", 1)[0]
            pos = min(i for i in (s.find("["), s.find("]")) if i >= 0)
            raise BenchParseError("'[' and ']' are not allowed in BENCH files "
                                  "(signals are scalar)", path, lineno, pos + 1)
        toks = _bench_tokenize(line, path, lineno)
        if toks[0].kind == "eol":
            continue
        p = BenchLineParser(toks, path, lineno)
        first = toks[0]
        kw = first.value.upper() if first.kind == "ident" else None
        if kw in ("INPUT", "OUTPUT") and toks[1].kind == "punct" and toks[1].value == "(":
            p.next()
            p.next()
            nt = p.expect_ident("signal name")
            p.expect_punct(")")
            t = p.peek()
            if t.kind != "eol":
                raise p.err("unexpected %r after %s(...)" % (t.value, kw), t)
            if kw == "INPUT":
                if nt.value in defined:
                    raise RedefinitionError(
                        "signal %r already defined (%s)" % (nt.value, defined[nt.value]),
                        path, lineno, nt.col)
                defined[nt.value] = "input at line %d" % lineno
                inputs.append((nt.value, lineno, nt.col))
            else:
                outputs.append((nt.value, lineno, nt.col))
            continue
        if first.kind == "num":
            raise p.err("literals are not allowed in BENCH files (%r)" % first.value,
                        first)
        if first.kind != "ident":
            raise p.err("expected INPUT, OUTPUT or assignment", first)
        lhs = p.expect_ident("signal name")
        p.expect_punct("=")
        ot = p.peek()
        if not (ot.kind == "ident" and p.is_punct("(", 1)):
            if ot.kind == "num":
                raise p.err("literals are not allowed in BENCH files (%r)"
                            % ot.value, ot)
            raise p.err("expected operator call OP(...) on right-hand side", ot)
        expr = p.parse_bench_expr()
        t = p.peek()
        if t.kind != "eol":
            if t.kind == "num":
                raise p.err("literals are not allowed in BENCH files", t)
            raise p.err("unexpected %r after expression" % t.value, t)
        if lhs.value in defined:
            raise RedefinitionError(
                "signal %r already defined (%s)" % (lhs.value, defined[lhs.value]),
                path, lineno, lhs.col)
        defined[lhs.value] = "assigned at line %d" % lineno
        assigns.append((lhs.value, expr, lineno, lhs.col))

    items = []
    if inputs:
        items.append(("decl", "input", [(n, col, 0, 0) for n, _, col in inputs],
                      inputs[0][1], None))
    if assigns:
        items.append(("decl", "wire", [(n, col, 0, 0) for n, _, _, col in assigns],
                      assigns[0][2], None))
    for lhs, expr, lineno, col in assigns:
        items.append(("assign", lhs, expr, lineno, col))
    c = build_circuit(items, path)
    seen = set()
    for name, lineno, col in outputs:
        if name not in c.kinds:
            raise UndefinedNameError(
                "OUTPUT(%s) references undefined signal %r" % (name, name),
                path, lineno, col)
        if name not in seen:
            seen.add(name)
            c.outputs.append(name)
    return c


def load_circuit(path, fmt="auto"):
    """Load a circuit in the given format; return (circuit, format)."""
    fmt = resolve_format(path, fmt)
    if fmt == "circ":
        return load_circ(path), fmt
    if fmt == "json":
        return load_json_circuit(path), fmt
    return load_bench_circuit(path), fmt


def get_format_opt(opts):
    if opts and opts.get("--format"):
        raw = opts["--format"][-1]
        f = raw.strip().lower()
        if f not in FORMATS:
            raise CliUsageError("invalid --format %r (expected auto, circ, json "
                                "or bench)" % raw)
        return f
    return "auto"


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


def cmd_check(args, opts=None):
    if len(args) != 1:
        raise CliUsageError("check requires exactly one file argument")
    c, fmt = load_circuit(args[0], get_format_opt(opts))
    result = {
        "format": fmt,
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
    get_format_opt(opts)
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

    c, _fmt = load_circuit(args[0], get_format_opt(opts))
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


# ---------------------------------------------------------------------------
# Analysis: stats, lint, dot
# ---------------------------------------------------------------------------

HIST_CATEGORIES = ("AND", "BUF", "CONCAT", "EQ", "MUX", "NAND", "NOR", "NOT",
                   "OR", "REDUCE_AND", "REDUCE_OR", "REDUCE_XOR", "XOR", "XNOR")
OP_ALIASES = {"ITE": "MUX"}


def op_category(e):
    if isinstance(e, Concat):
        return "CONCAT"
    if isinstance(e, Call):
        return OP_ALIASES.get(e.op, e.op)
    return "BUF"


def dep_names(e):
    refs = []
    expr_refs(e, refs)
    return [r.name for r in refs]


def is_scalar(c, name):
    return c.ranges[name] == (0, 0)


def sig_label(c, name):
    if is_scalar(c, name):
        return name
    msb, lsb = c.ranges[name]
    return "%s[%d:%d]" % (name, msb, lsb)


def signal_depths(c):
    depth = {n: 0 for n in c.inputs}
    for name in topo_order(c):
        ds = [depth.get(d, 0) for d in dep_names(c.assigns[name])]
        depth[name] = 1 + (max(ds) if ds else 0)
    return depth


def backward_cone(c, roots):
    seen = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if n in c.assigns:
            stack.extend(dep_names(c.assigns[n]))
    return seen


def cmd_stats(args, opts):
    if len(args) != 1:
        raise CliUsageError("stats requires exactly one file argument")
    c, _fmt = load_circuit(args[0], get_format_opt(opts))
    outs = set(c.outputs)
    hist = {k: 0 for k in HIST_CATEGORIES}
    edges = 0
    for name in c.order:
        e = c.assigns[name]
        cat = op_category(e)
        hist[cat] = hist.get(cat, 0) + 1
        edges += len(dep_names(e))
    depth = signal_depths(c)
    result = {
        "inputs_count": len(c.inputs),
        "outputs_count": len(c.outputs),
        "wires_count": len([w for w in c.wires if w not in outs]),
        "assignments_count": len(c.assigns),
        "edges_count": edges,
        "depth": max([depth.get(o, 0) for o in c.outputs] or [0]),
        "op_histogram": hist,
    }
    lines = ["%s: %s" % (k, result[k]) for k in
             ("inputs_count", "outputs_count", "wires_count",
              "assignments_count", "edges_count", "depth")]
    lines.append("op_histogram:")
    for k, v in hist.items():
        lines.append("  %s: %d" % (k, v))
    return result, "\n".join(lines)


def _int_opt(opts, flag, default):
    if not opts.get(flag):
        return default
    raw = opts[flag][-1]
    s = raw.strip()
    if not s.isdigit():
        raise CliUsageError("invalid %s %r (expected a non-negative integer)"
                            % (flag, raw))
    return int(s)


def cmd_lint(args, opts):
    if len(args) != 1:
        raise CliUsageError("lint requires exactly one file argument")
    fmt = get_format_opt(opts)
    limit = _int_opt(opts, "--max-input-bits", 16)
    c, _fmt = load_circuit(args[0], fmt)
    warnings = []
    used = backward_cone(c, c.outputs)
    for n in c.inputs:
        if n not in used:
            warnings.append({"type": "UnusedInputWarning", "name": n,
                             "message": "input '%s' is not used by any output" % n})
    widths = [(n, c.ranges[n][0] - c.ranges[n][1] + 1) for n in c.inputs]
    total = sum(w for _, w in widths)
    if total > limit:
        warnings.append({
            "type": "ConstantOutputCheckSkippedWarning", "name": None,
            "message": "constant output check skipped: %d input bits exceeds "
                       "limit %d" % (total, limit)})
    else:
        order = topo_order(c)
        outs = list(dict.fromkeys(c.outputs))
        first = {}
        candidates = set(outs)
        for combo in range(1 << total):
            env = {}
            shift = 0
            for n, w in widths:
                env[n] = (combo >> shift) & mask(w)
                shift += w
            for name in order:
                env[name] = eval_expr(c.assigns[name], env, c)
            for o in list(candidates):
                if combo == 0:
                    first[o] = env[o]
                elif env[o] != first[o]:
                    candidates.discard(o)
            if not candidates:
                break
        for o in outs:
            if o in candidates:
                msb, lsb = c.ranges[o]
                w = msb - lsb + 1
                if is_scalar(c, o):
                    val = str(first[o])
                else:
                    val = "0b" + format(first[o], "0%db" % w)
                warnings.append({"type": "ConstantOutputWarning", "name": o,
                                 "message": "output '%s' is constant" % o,
                                 "value": val})
    warnings.sort(key=lambda w: (w["type"], w["name"] or ""))
    lines = []
    for w in warnings:
        lines.append("warning: %s: %s" % (w["type"], w["message"]))
    return {"warnings": warnings}, "\n".join(lines) if lines else "no warnings"


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_dot(c, cone=None):
    inputs = set(c.inputs)
    keep = backward_cone(c, cone) if cone is not None else None

    def nid(n):
        return ("in:" if n in inputs else "sig:") + n

    lines = set()
    for n in c.inputs:
        if keep is None or n in keep:
            lines.add("  %s [label=%s];" % (_q(nid(n)), _q(sig_label(c, n))))
    for n in list(c.wires) + list(c.outputs):
        if n in inputs or (keep is not None and n not in keep):
            continue
        lines.add("  %s [label=%s];" % (_q(nid(n)), _q(sig_label(c, n))))
    for lhs, e in c.assigns.items():
        if keep is not None and lhs not in keep:
            continue
        op = "op:" + lhs
        lines.add("  %s [label=%s];" % (_q(op), _q(op_category(e))))
        for d in dep_names(e):
            lines.add("  %s -> %s;" % (_q(nid(d)), _q(op)))
        lines.add("  %s -> %s;" % (_q(op), _q(nid(lhs))))
    return "digraph circopt {\n" + "".join(l + "\n" for l in sorted(lines)) + "}\n"


def cmd_dot(args, opts):
    if len(args) != 1:
        raise CliUsageError("dot requires exactly one file argument")
    fmt = get_format_opt(opts)
    if not opts.get("-o"):
        raise CliUsageError("dot requires -o <dot_file>")
    out_path = opts["-o"][-1]
    if not out_path:
        raise CliUsageError("dot requires a non-empty -o <dot_file>")
    cone = None
    if opts.get("--cone"):
        cone = []
        for raw in opts["--cone"]:
            cone.extend(p.strip() for p in raw.split(",") if p.strip())
        if not cone:
            raise CliUsageError("--cone requires at least one output name")
    c, _fmt = load_circuit(args[0], fmt)
    if cone is not None:
        outs = set(c.outputs)
        for n in cone:
            if n not in outs:
                raise UndefinedNameError("unknown output: %s" % n)
    text = build_dot(c, cone)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise CliUsageError("cannot write %s: %s" % (out_path, e))
    return {"dot_path": out_path}, "wrote %s" % out_path


# ---------------------------------------------------------------------------
# Part 7: cone, truth-table, equiv
# ---------------------------------------------------------------------------

UnknownOutputFormatError = _mk("UnknownOutputFormatError", 1)
UnsupportedFeatureError = _mk("UnsupportedFeatureError", 1)
InputLimitExceeded = _mk("InputLimitExceeded", 1)
PortMismatchError = _mk("PortMismatchError", 3)

ASSOC_OPS = {"AND", "OR", "XOR"}


def width_of(c, name):
    msb, lsb = c.ranges[name]
    return msb - lsb + 1


def format_expr(e):
    """Canonical text of an expression (with associative flattening)."""
    if isinstance(e, Const):
        if e.width == 1:
            return str(e.value)
        return "0b" + format(e.value, "0%db" % e.width)
    if isinstance(e, Ident):
        return e.name
    if isinstance(e, Index):
        return "%s[%d]" % (e.name, e.idx)
    if isinstance(e, Slice):
        return "%s[%d:%d]" % (e.name, e.hi, e.lo)
    if isinstance(e, Concat):
        return "{" + ", ".join(format_expr(p) for p in e.parts) + "}"
    if e.op in ASSOC_OPS:
        flat = []
        stack = list(reversed(e.args))
        while stack:
            a = stack.pop()
            if isinstance(a, Call) and a.op == e.op:
                stack.extend(reversed(a.args))
            else:
                flat.append(format_expr(a))
        flat.sort()
        return "%s(%s)" % (e.op, ", ".join(flat))
    return "%s(%s)" % (e.op, ", ".join(format_expr(a) for a in e.args))


def canonical_assign_order(c, names):
    """Dependency order over `names` (assigned signals); ties by LHS name."""
    import heapq
    names = set(names)
    deps = {}
    users = {n: [] for n in names}
    for n in names:
        ds = {d for d in dep_names(c.assigns[n]) if d in names and d != n}
        deps[n] = len(ds)
        for d in ds:
            users[d].append(n)
    heap = [n for n in names if deps[n] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        n = heapq.heappop(heap)
        order.append(n)
        for u in users[n]:
            deps[u] -= 1
            if deps[u] == 0:
                heapq.heappush(heap, u)
    return order


def decl_text(c, name):
    return sig_label(c, name)


def write_circ_text(c, inputs, outputs, wires, order):
    lines = []
    if inputs:
        lines.append("input " + " ".join(decl_text(c, n) for n in sorted(inputs)))
    lines.append("output " + " ".join(decl_text(c, n) for n in sorted(outputs)))
    if wires:
        lines.append("wire " + " ".join(decl_text(c, n) for n in sorted(wires)))
    lines.append("")
    for n in order:
        lines.append("%s = %s" % (n, format_expr(c.assigns[n])))
    return "\n".join(lines) + "\n"


def write_json_text(c, inputs, outputs, wires, order):
    def ports(ns):
        return [{"name": n, "msb": c.ranges[n][0], "lsb": c.ranges[n][1]}
                for n in sorted(ns)]
    doc = {
        "format_version": 1,
        "inputs": ports(inputs),
        "outputs": ports(outputs),
        "wires": ports(wires),
        "assignments": [{"lhs": n, "rhs": format_expr(c.assigns[n])}
                        for n in order],
    }
    return json.dumps(doc, indent=2) + "\n"


def parse_name_list(opts, flag):
    names = []
    for raw in opts.get(flag, []):
        for p in raw.split(","):
            p = p.strip()
            if p and p not in names:
                names.append(p)
    return names


def write_file(path, text):
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        raise CliUsageError("cannot write %s: %s" % (path, e))


def cmd_cone(args, opts):
    if len(args) != 1:
        raise CliUsageError("cone requires exactly one file argument")
    fmt = get_format_opt(opts)
    if not opts.get("-o") or not opts["-o"][-1]:
        raise CliUsageError("cone requires -o <out_file>")
    out_path = opts["-o"][-1]
    if opts.get("--out-format"):
        raw = opts["--out-format"][-1]
        out_fmt = raw.strip().lower()
        if out_fmt == "bench":
            raise UnsupportedFeatureError(
                "BENCH output is not supported by cone (use circ or json)")
        if out_fmt not in ("circ", "json"):
            raise CliUsageError("invalid --out-format %r (expected circ or json)"
                                % raw)
    else:
        ext = os.path.splitext(out_path)[1].lower()
        if ext == ".circ":
            out_fmt = "circ"
        elif ext == ".json":
            out_fmt = "json"
        else:
            raise UnknownOutputFormatError(
                "cannot infer output format from extension %r of %s "
                "(use --out-format circ|json)" % (ext, out_path))
    if not opts.get("--outputs"):
        raise CliUsageError("cone requires --outputs name1,name2,...")
    wanted = parse_name_list(opts, "--outputs")
    if not wanted:
        raise CliUsageError("--outputs requires at least one output name")
    c, _fmt = load_circuit(args[0], fmt)
    outs = set(c.outputs)
    for n in wanted:
        if n not in outs:
            raise UndefinedNameError("unknown output: %s" % n)
    for n in wanted:
        if n in c.kinds and c.kinds[n] == "input":
            raise UnsupportedFeatureError(
                "output %r is also an input; cannot be represented" % n)
    keep = backward_cone(c, wanted)
    inputs = [n for n in c.inputs if n in keep]
    assigned = [n for n in keep if n in c.assigns]
    wset = set(wanted)
    wires = [n for n in assigned if n not in wset]
    order = canonical_assign_order(c, assigned)
    if out_fmt == "circ":
        text = write_circ_text(c, inputs, wanted, wires, order)
    else:
        text = write_json_text(c, inputs, wanted, wires, order)
    write_file(out_path, text)
    return {"out_path": out_path}, "wrote %s" % out_path


# --- fast evaluation -------------------------------------------------------

def _py_expr(e, c, var):
    if isinstance(e, Const):
        return str(e.value)
    if isinstance(e, Ident):
        return var[e.name]
    if isinstance(e, Index):
        sh = e.idx - c.ranges[e.name][1]
        return "((%s >> %d) & 1)" % (var[e.name], sh)
    if isinstance(e, Slice):
        sh = e.lo - c.ranges[e.name][1]
        return "((%s >> %d) & %d)" % (var[e.name], sh, mask(e.w))
    if isinstance(e, Concat):
        parts = []
        shift = e.w
        for p in e.parts:
            shift -= p.w
            s = _py_expr(p, c, var)
            parts.append("(%s << %d)" % (s, shift) if shift else s)
        return "(" + " | ".join(parts) + ")"
    a = [_py_expr(x, c, var) for x in e.args]
    op = e.op
    m = mask(e.w)
    if op == "BUF":
        return a[0]
    if op == "NOT":
        return "(%s ^ %d)" % (a[0], m)
    if op in ("AND", "NAND", "OR", "NOR", "XOR", "XNOR"):
        sym = {"AND": "&", "NAND": "&", "OR": "|", "NOR": "|",
               "XOR": "^", "XNOR": "^"}[op]
        s = "(" + (" %s " % sym).join(a) + ")"
        if op in ("NAND", "NOR", "XNOR"):
            s = "(%s ^ %d)" % (s, m)
        return s
    if op in ("MUX", "ITE"):
        return "(%s if %s else %s)" % (a[1], a[0], a[2])
    if op == "REDUCE_AND":
        return "(1 if %s == %d else 0)" % (a[0], mask(e.args[0].w))
    if op == "REDUCE_OR":
        return "(1 if %s else 0)" % a[0]
    if op == "REDUCE_XOR":
        return "(bin(%s).count('1') & 1)" % a[0]
    if op == "EQ":
        return "(1 if %s == %s else 0)" % (a[0], a[1])
    raise CircoptError("unknown operator %s" % op)


def make_evaluator(c, in_names, out_names):
    """Return f(values_tuple_for_in_names) -> tuple of output values."""
    keep = backward_cone(c, out_names)
    order = [n for n in topo_order(c) if n in keep]
    try:
        var = {}
        for i, n in enumerate(in_names):
            var[n] = "i%d" % i
        for i, n in enumerate(order):
            var[n] = "v%d" % i
        lines = ["def _f(_vals):"]
        if in_names:
            lines.append("    %s, = _vals" % ", ".join(var[n] for n in in_names))
        for n in order:
            lines.append("    %s = %s" % (var[n], _py_expr(c.assigns[n], c, var)))
        lines.append("    return (%s,)" % ", ".join(var[n] for n in out_names)
                     if out_names else "    return ()")
        ns = {}
        exec(compile("\n".join(lines), "<circuit>", "exec"), ns)
        return ns["_f"]
    except (RecursionError, SyntaxError, MemoryError, ValueError):
        def _slow(vals):
            env = dict(zip(in_names, vals))
            for n in order:
                env[n] = eval_expr(c.assigns[n], env, c)
            return tuple(env[n] for n in out_names)
        return _slow


def input_layout(c):
    """Sorted inputs with widths and their shifts in the enumeration counter."""
    names = sorted(c.inputs)
    widths = [width_of(c, n) for n in names]
    total = sum(widths)
    shifts = []
    s = total
    for w in widths:
        s -= w
        shifts.append(s)
    return names, widths, shifts, total


def split_combo(combo, widths, shifts):
    return tuple((combo >> s) & mask(w) for w, s in zip(widths, shifts))


def get_radix(opts):
    radix = "bin"
    if opts.get("--radix"):
        radix = opts["--radix"][-1].strip().lower()
        if radix not in ("bin", "hex", "dec"):
            raise CliUsageError("invalid --radix %r (expected bin, hex or dec)"
                                % opts["--radix"][-1])
    return radix


def required_int(opts, flag, cmd):
    if not opts.get(flag):
        raise CliUsageError("%s requires %s N" % (cmd, flag))
    return _int_opt(opts, flag, None)


def cmd_truth_table(args, opts):
    if len(args) != 1:
        raise CliUsageError("truth-table requires exactly one file argument")
    fmt = get_format_opt(opts)
    if opts.get("--mode"):
        mode = opts["--mode"][-1].strip().lower()
        if mode == "3val":
            raise UnsupportedFeatureError("truth-table supports only --mode 2val")
        if mode != "2val":
            raise CliUsageError("invalid --mode %r (expected 2val or 3val)"
                                % opts["--mode"][-1])
    radix = get_radix(opts)
    limit = required_int(opts, "--max-input-bits", "truth-table")
    out_fmt = None
    if opts.get("--out-format"):
        raw = opts["--out-format"][-1]
        out_fmt = raw.strip().lower()
        if out_fmt not in ("csv", "json"):
            raise CliUsageError("invalid --out-format %r (expected csv or json)"
                                % raw)
    out_path = None
    if opts.get("-o"):
        out_path = opts["-o"][-1]
        if not out_path:
            raise CliUsageError("-o requires a non-empty path")
    c, _fmt = load_circuit(args[0], fmt)
    names, widths, shifts, total = input_layout(c)
    if total > limit:
        raise InputLimitExceeded(
            "circuit has %d input bits, exceeding --max-input-bits %d"
            % (total, limit))
    outs = sorted(set(c.outputs))
    owidths = [width_of(c, n) for n in outs]
    f = make_evaluator(c, names, outs)
    rows = []
    for combo in range(1 << total):
        ivals = split_combo(combo, widths, shifts)
        ovals = f(ivals)
        rows.append(([format_value(v, w, radix) for v, w in zip(ivals, widths)],
                     [format_value(v, w, radix) for v, w in zip(ovals, owidths)]))

    def as_json():
        return {
            "radix": radix,
            "inputs": sig_list(c, names),
            "outputs": sig_list(c, outs),
            "rows": [{"inputs": dict(zip(names, iv)), "outputs": dict(zip(outs, ov))}
                     for iv, ov in rows],
        }

    def as_csv():
        lines = [",".join(names + outs)]
        for iv, ov in rows:
            lines.append(",".join(iv + ov))
        return "\n".join(lines) + "\n"

    json_mode = bool(opts.get("__json__"))
    result = None
    if out_path is not None:
        if (out_fmt or "csv") == "json":
            doc = {"ok": True, "command": "truth-table"}
            result = as_json()
            doc.update(result)
            write_file(out_path, json.dumps(doc, separators=(",", ":")) + "\n")
        else:
            write_file(out_path, as_csv())
        text = ""
    elif out_fmt == "json" and not json_mode:
        doc = {"ok": True, "command": "truth-table"}
        doc.update(as_json())
        text = json.dumps(doc, separators=(",", ":"))
    else:
        text = "" if json_mode else as_csv()[:-1]
    if json_mode and result is None:
        result = as_json()
    return result or {}, text


def cmd_equiv(args, opts):
    import random
    if len(args) != 2:
        raise CliUsageError("equiv requires exactly two file arguments")
    fmt = get_format_opt(opts)
    limit = required_int(opts, "--max-input-bits", "equiv")
    trials = _int_opt(opts, "--trials", 10000)
    if trials < 1:
        raise CliUsageError("--trials must be a positive integer")
    seed = _int_opt(opts, "--seed", 0)
    restrict = None
    if opts.get("--outputs"):
        restrict = parse_name_list(opts, "--outputs")
        if not restrict:
            raise CliUsageError("--outputs requires at least one output name")
    ca, _ = load_circuit(args[0], fmt)
    cb, _ = load_circuit(args[1], fmt)

    def ports(c, names):
        return {n: c.ranges[n] for n in names}

    pa, pb = ports(ca, ca.inputs), ports(cb, cb.inputs)
    if pa != pb:
        diffs = []
        for n in sorted(set(pa) | set(pb)):
            if n not in pb:
                diffs.append("input %r only in %s" % (n, args[0]))
            elif n not in pa:
                diffs.append("input %r only in %s" % (n, args[1]))
            elif pa[n] != pb[n]:
                diffs.append("input %r range [%d:%d] vs [%d:%d]"
                             % (n, pa[n][0], pa[n][1], pb[n][0], pb[n][1]))
        raise PortMismatchError("input ports differ: " + "; ".join(diffs))
    oa, ob = ports(ca, ca.outputs), ports(cb, cb.outputs)
    if restrict is None:
        if oa != ob:
            diffs = []
            for n in sorted(set(oa) | set(ob)):
                if n not in ob:
                    diffs.append("output %r only in %s" % (n, args[0]))
                elif n not in oa:
                    diffs.append("output %r only in %s" % (n, args[1]))
                elif oa[n] != ob[n]:
                    diffs.append("output %r range [%d:%d] vs [%d:%d]"
                                 % (n, oa[n][0], oa[n][1], ob[n][0], ob[n][1]))
            raise PortMismatchError("output ports differ: " + "; ".join(diffs))
        compared = sorted(oa)
    else:
        for n in restrict:
            if n not in oa and n not in ob:
                raise UndefinedNameError("unknown output: %s" % n)
            if n not in oa:
                raise PortMismatchError("output %r not present in %s" % (n, args[0]))
            if n not in ob:
                raise PortMismatchError("output %r not present in %s" % (n, args[1]))
            if oa[n] != ob[n]:
                raise PortMismatchError(
                    "output %r range [%d:%d] vs [%d:%d]"
                    % (n, oa[n][0], oa[n][1], ob[n][0], ob[n][1]))
        compared = sorted(restrict)

    names, widths, shifts, total = input_layout(ca)
    fa = make_evaluator(ca, names, compared)
    fb = make_evaluator(cb, names, compared)
    owidths = [width_of(ca, n) for n in compared]

    if total <= limit:
        method = "exhaustive"
        gen = (split_combo(k, widths, shifts) for k in range(1 << total))
    else:
        method = "randomized"
        rng = random.Random(seed)

        def _gen():
            for _ in range(trials):
                yield tuple(rng.getrandbits(w) for w in widths)
        gen = _gen()

    count = 0
    cex = None
    for ivals in gen:
        count += 1
        ra = fa(ivals)
        rb = fb(ivals)
        if ra != rb:
            cex = (ivals, ra, rb)
            break
    result = {"equivalent": cex is None, "method": method, "comparisons": count}
    if cex is None:
        text = "equivalent (%s, %d comparisons)" % (method, count)
        return result, text
    ivals, ra, rb = cex

    def vals(c, ns, vs, ws):
        return [{"name": n, "msb": c.ranges[n][0], "lsb": c.ranges[n][1],
                 "value": format_value(v, w, "bin")}
                for n, v, w in zip(ns, vs, ws)]

    result["counterexample"] = {
        "inputs": vals(ca, names, ivals, widths),
        "outputs_compared": compared,
        "a_outputs": vals(ca, compared, ra, owidths),
        "b_outputs": vals(cb, compared, rb, owidths),
    }
    lines = ["not equivalent (%s, %d comparisons)" % (method, count),
             "counterexample:"]
    for d in result["counterexample"]["inputs"]:
        lines.append("  %s=%s" % (d["name"], d["value"]))
    for da, db in zip(result["counterexample"]["a_outputs"],
                      result["counterexample"]["b_outputs"]):
        lines.append("  %s: a=%s b=%s" % (da["name"], da["value"], db["value"]))
    return result, "\n".join(lines), 10


COMMANDS = {"check": cmd_check, "eval": cmd_eval, "stats": cmd_stats,
            "lint": cmd_lint, "dot": cmd_dot, "cone": cmd_cone,
            "truth-table": cmd_truth_table, "equiv": cmd_equiv}
# command -> {flag: takes_value}
COMMAND_FLAGS = {
    "check": {"--format": True},
    "eval": {"--format": True, "--set": True, "--default": True, "--allow-extra": False,
             "--radix": True, "--mode": True},
    "stats": {"--format": True},
    "lint": {"--format": True, "--max-input-bits": True},
    "dot": {"--format": True, "-o": True, "--cone": True},
    "cone": {"--format": True, "-o": True, "--outputs": True, "--out-format": True},
    "truth-table": {"--format": True, "--max-input-bits": True, "--radix": True,
                    "-o": True, "--out-format": True, "--mode": True},
    "equiv": {"--format": True, "--max-input-bits": True, "--trials": True,
              "--outputs": True},
}
for _flags in COMMAND_FLAGS.values():
    _flags["--seed"] = True
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
            if opts.get("--seed"):
                _int_opt(opts, "--seed", 0)
            if json_mode:
                opts["__json__"] = [True]
            ret = COMMANDS[command](positional[1:], opts)
            result, text = ret[0], ret[1]
            exit_code = ret[2] if len(ret) > 2 else 0
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
    return exit_code


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    sys.exit(main(sys.argv[1:]))
