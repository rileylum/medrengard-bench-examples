#!/usr/bin/env python3
"""circopt - circuit tool (Part 1: CLI basics and `check`; Part 2: `eval`)."""

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
                       Evaluate the circuit (2-valued) and print outputs.

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
            toks.append(Tok("num", text[i:j], i + 1))
            i = j
            continue
        if c in "(),=":
            toks.append(Tok("punct", c, i + 1))
            i += 1
            continue
        if c in "[]":
            raise CircParseError(
                "brackets are not supported in scalar .circ files",
                file, lineno, i + 1)
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
    def __init__(self, value, line, col):
        self.value, self.line, self.col = value, line, col


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

    def parse_expr(self):
        t = self.peek()
        if t.kind == "eol":
            raise self.err("unexpected end of line, expected expression", t)
        if t.kind == "num":
            self.next()
            if t.value in ("0", "1"):
                return Const(int(t.value), self.lineno, t.col)
            raise self.err("invalid literal %r (only 0 and 1 allowed)" % t.value, t)
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
                names.append((t.value, t.col))
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
        self.assigns = {}  # name -> expr
        self.order = []


def expr_refs(e, out):
    if isinstance(e, Ident):
        out.append(e)
    elif isinstance(e, Call):
        for a in e.args:
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
            for name, ncol in names:
                if name in c.kinds:
                    raise DuplicateNameError(
                        "name %r already declared as %s" % (name, c.kinds[name]),
                        file, lineno, ncol)
                c.kinds[name] = kind
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

def sig_list(names):
    return [{"name": n, "msb": 0, "lsb": 0} for n in sorted(names)]


def cmd_check(args):
    if len(args) != 1:
        raise CliUsageError("check requires exactly one file argument")
    c = load_circ(args[0])
    result = {
        "format": "circ",
        "inputs": sig_list(c.inputs),
        "outputs": sig_list(c.outputs),
    }
    text = "OK %s\ninputs: %s\noutputs: %s" % (
        args[0],
        " ".join(sorted(c.inputs)) or "(none)",
        " ".join(sorted(c.outputs)) or "(none)",
    )
    return result, text


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def parse_bit(text, what):
    v = text.strip()
    if v in ("0", "1"):
        return int(v)
    raise InputValueParseError("invalid value %r for %s (expected 0 or 1)" % (text, what))


def eval_expr(e, env):
    if isinstance(e, Const):
        return e.value
    if isinstance(e, Ident):
        return env[e.name]
    vals = [eval_expr(a, env) for a in e.args]
    op = e.op
    if op == "BUF":
        return vals[0]
    if op == "NOT":
        return 1 - vals[0]
    if op in ("AND", "NAND"):
        r = 1 if all(vals) else 0
        return r if op == "AND" else 1 - r
    if op in ("OR", "NOR"):
        r = 1 if any(vals) else 0
        return r if op == "OR" else 1 - r
    if op in ("XOR", "XNOR"):
        r = 0
        for v in vals:
            r ^= v
        return r if op == "XOR" else 1 - r
    raise CircoptError("unknown operator %s" % op)


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
        env[name] = eval_expr(c.assigns[name], env)
    return env


def cmd_eval(args, opts):
    if len(args) != 1:
        raise CliUsageError("eval requires exactly one file argument")
    sets = []
    for s in opts.get("--set", []):
        if "=" not in s:
            raise InputValueParseError("invalid --set %r (expected name=value)" % s)
        name, val = s.split("=", 1)
        name = name.strip()
        if not name:
            raise InputValueParseError("invalid --set %r (empty name)" % s)
        sets.append((name, parse_bit(val, name)))
    default = None
    if opts.get("--default"):
        default = parse_bit(opts["--default"][-1], "--default")
    allow_extra = bool(opts.get("--allow-extra"))

    c = load_circ(args[0])
    inputs = {}
    input_set = set(c.inputs)
    for name, v in sets:
        if name not in input_set:
            if allow_extra:
                continue
            raise UnknownInputError("unknown input %r" % name)
        inputs[name] = v
    missing = [n for n in sorted(c.inputs) if n not in inputs]
    if missing:
        if default is None:
            raise MissingInputError("missing value for input(s): %s"
                                    % ", ".join(missing))
        for n in missing:
            inputs[n] = default
    env = evaluate(c, inputs)
    outs = sorted(c.outputs)
    result = {
        "mode": "2val",
        "radix": "bin",
        "outputs": [{"name": n, "msb": 0, "lsb": 0, "value": str(env[n])}
                    for n in outs],
    }
    text = "\n".join("%s=%d" % (n, env[n]) for n in outs)
    return result, text


COMMANDS = {"check": cmd_check, "eval": cmd_eval}
# command -> {flag: takes_value}
COMMAND_FLAGS = {
    "check": {},
    "eval": {"--set": True, "--default": True, "--allow-extra": False},
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
