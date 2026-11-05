#!/usr/bin/env python3
"""Pipeline executor CLI.

Usage:
    python launch.py <pipeline> --workspace <dir> --output <dir> [--config <toml>]
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class PipeSyntaxError(Exception):
    pass


class InvalidPipe(Exception):
    pass


class BreakEx(Exception):
    pass


class ContinueEx(Exception):
    pass


class ReturnEx(Exception):
    def __init__(self, value):
        super().__init__()
        self.value = value


class EnvStr(str):
    """A string originating from an environment variable (dynamically cast)."""


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

class Tok:
    __slots__ = ("kind", "value", "start", "end", "line", "raw")

    def __init__(self, kind, value, start, end, line, raw=None):
        self.kind = kind
        self.value = value
        self.start = start
        self.end = end
        self.line = line
        self.raw = raw

    def __repr__(self):
        return "Tok(%s,%r)" % (self.kind, self.value)


OPS = [
    "&&", "||", "==", "!=", "<=", ">=", "++", "--", "+=", "-=", "*=", "/=",
    "%=", "**",
    "{", "}", "(", ")", "[", "]", ";", ":", ",", "=", "<", ">", "+", "-",
    "*", "/", "%", "!", ".",
]

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUM_RE = re.compile(r"(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Lexer:
    def __init__(self, text):
        self.t = text
        self.i = 0
        self.buf = []
        self.last_end = 0

    # -- helpers
    def line_of(self, pos):
        return self.t.count("\n", 0, pos) + 1

    def err(self, msg, pos=None):
        if pos is None:
            pos = self.i
        raise PipeSyntaxError("line %d: %s" % (self.line_of(pos), msg))

    def _skip(self):
        t = self.t
        n = len(t)
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif t.startswith("//", self.i) or c == "#":
                j = t.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif t.startswith("/*", self.i):
                j = t.find("*/", self.i + 2)
                if j < 0:
                    self.err("unterminated comment")
                self.i = j + 2
            else:
                break

    def _lex_one(self):
        self._skip()
        t = self.t
        start = self.i
        if self.i >= len(t):
            return Tok("eof", None, start, start, self.line_of(start))
        c = t[self.i]
        line = self.line_of(start)
        if c in "\"'":
            parts, raw = self._lex_string(c)
            return Tok("str", parts, start, self.i, line, raw)
        if c == "$":
            if t.startswith("${", self.i):
                j = t.find("}", self.i + 2)
                if j < 0:
                    self.err("unterminated ${")
                name = t[self.i + 2:j].strip()
                if not name:
                    self.err("empty ${}")
                self.i = j + 1
                return Tok("ref", name, start, self.i, line)
            m = ENV_NAME_RE.match(t, self.i + 1)
            if not m:
                self.err("unexpected '$'")
            self.i = m.end()
            return Tok("env", m.group(0), start, self.i, line)
        if c.isdigit() or (c == "." and self.i + 1 < len(t) and t[self.i + 1].isdigit()):
            m = NUM_RE.match(t, self.i)
            s = m.group(0)
            self.i = m.end()
            if IDENT_RE.match(t, self.i) and not t[self.i].isdigit():
                self.err("invalid number %r" % (s + t[self.i]))
            if "." in s or "e" in s or "E" in s:
                return Tok("num", float(s), start, self.i, line)
            return Tok("num", int(s), start, self.i, line)
        m = IDENT_RE.match(t, self.i)
        if m:
            self.i = m.end()
            return Tok("id", m.group(0), start, self.i, line)
        for op in OPS:
            if t.startswith(op, self.i):
                self.i += len(op)
                return Tok("op", op, start, self.i, line)
        self.err("unexpected character %r" % c)

    def _lex_string(self, q):
        t = self.t
        self.i += 1
        raw_start = self.i
        parts = []
        cur = []
        n = len(t)
        while True:
            if self.i >= n:
                self.err("unterminated string")
            c = t[self.i]
            if c == q:
                raw = t[raw_start:self.i]
                self.i += 1
                break
            if c == "\\":
                if self.i + 1 >= n:
                    self.err("unterminated string")
                d = t[self.i + 1]
                mp = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\",
                      '"': '"', "'": "'", "$": "$", "\n": ""}
                if d in mp:
                    cur.append(mp[d])
                else:
                    cur.append("\\" + d)
                self.i += 2
                continue
            if c == "$" and t.startswith("${", self.i):
                j = t.find("}", self.i + 2)
                if j >= 0:
                    if cur:
                        parts.append("".join(cur))
                        cur = []
                    parts.append(("ref", t[self.i + 2:j].strip()))
                    self.i = j + 1
                    continue
            if c == "$" and q == '"':
                m = ENV_NAME_RE.match(t, self.i + 1)
                if m:
                    if cur:
                        parts.append("".join(cur))
                        cur = []
                    parts.append(("env", m.group(0)))
                    self.i = m.end()
                    continue
            cur.append(c)
            self.i += 1
        if cur or not parts:
            parts.append("".join(cur))
        return parts, raw

    # -- public API
    def peek(self, k=0):
        while len(self.buf) <= k:
            self.buf.append(self._lex_one())
        return self.buf[k]

    def next(self):
        tok = self.peek()
        self.buf.pop(0)
        self.last_end = tok.end
        return tok

    def at(self, kind, value=None, k=0):
        tok = self.peek(k)
        return tok.kind == kind and (value is None or tok.value == value)

    def at_op(self, value, k=0):
        return self.at("op", value, k)

    def accept_op(self, value):
        if self.at_op(value):
            return self.next()
        return None

    def expect_op(self, value):
        tok = self.peek()
        if not (tok.kind == "op" and tok.value == value):
            self.err("expected '%s' but found %s" % (value, self._desc(tok)), tok.start)
        return self.next()

    def expect_id(self, what="identifier"):
        tok = self.peek()
        if tok.kind != "id":
            self.err("expected %s but found %s" % (what, self._desc(tok)), tok.start)
        return self.next()

    @staticmethod
    def _desc(tok):
        if tok.kind == "eof":
            return "end of file"
        return "%r" % (tok.value if tok.kind != "str" else tok.raw)

    def _reset_to_last(self):
        self.buf = []
        self.i = self.last_end

    def raw_block(self):
        """Read raw text up to the brace matching an already consumed '{'."""
        self._reset_to_last()
        t = self.t
        start = self.i
        res = self._match_brace(start, quote_aware=True)
        if res is None:
            res = self._match_brace(start, quote_aware=False)
        if res is None:
            self.err("unterminated block", start)
        self.i = res + 1
        self.last_end = self.i
        return t[start:res]

    def _match_brace(self, start, quote_aware):
        t = self.t
        n = len(t)
        depth = 1
        i = start
        state = None
        while i < n:
            c = t[i]
            if quote_aware:
                if state == "'":
                    if c == "'":
                        state = None
                    i += 1
                    continue
                if state == '"':
                    if c == "\\":
                        i += 2
                        continue
                    if c == '"':
                        state = None
                    i += 1
                    continue
                if c == "\\":
                    i += 2
                    continue
                if c in "'\"":
                    state = c
                    i += 1
                    continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    def raw_line(self):
        self._reset_to_last()
        t = self.t
        j = t.find("\n", self.i)
        if j < 0:
            j = len(t)
        s = t[self.i:j]
        # cut trailing comments
        k = s.find("//")
        if k >= 0:
            s = s[:k]
        # stop at closing brace of task if on same line
        k = -1
        depth = 0
        for idx, ch in enumerate(s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    k = idx
                    break
                depth -= 1
        if k >= 0:
            j = self.i + k
            s = s[:k]
        self.i = j
        self.last_end = j
        return s.strip().rstrip(";").strip()


# --------------------------------------------------------------------------
# Pipeline model
# --------------------------------------------------------------------------

BASE_TYPES = ("string", "int", "float", "bool")


class Param:
    def __init__(self, name, ptype, default, line):
        self.name = name
        self.type = ptype
        self.default = default  # expression node or None
        self.line = line


class Task:
    def __init__(self, name, line):
        self.name = name
        self.line = line
        self.params = []
        self.run = None
        self.success = None  # list of (name, kind, node)
        self.requires = None  # list of statements
        self.output = None
        self.timeout = None
        self.fields = set()


def type_str(t):
    if isinstance(t, tuple):
        return "list[%s]" % type_str(t[1])
    return t


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

FIELD_NAMES = ("params", "run", "success", "requires", "output", "timeout")


class Parser:
    def __init__(self, text):
        self.lx = Lexer(text)

    def err(self, msg, tok=None):
        self.lx.err(msg, tok.start if tok is not None else None)

    # -- top level
    def parse_file(self):
        tasks = {}
        order = []
        lx = self.lx
        while not lx.at("eof"):
            tok = lx.peek()
            if tok.kind == "op" and tok.value == ";":
                lx.next()
                continue
            if not (tok.kind == "id" and tok.value == "task"):
                self.err("expected 'task' but found %s" % Lexer._desc(tok), tok)
            task = self.parse_task()
            if task.name in tasks:
                raise InvalidPipe("duplicate task '%s'" % task.name)
            tasks[task.name] = task
            order.append(task)
        return tasks

    def parse_task(self):
        lx = self.lx
        lx.next()  # task
        name_tok = lx.expect_id("task name")
        task = Task(name_tok.value, name_tok.line)
        lx.expect_op("{")
        while True:
            tok = lx.peek()
            if tok.kind == "op" and tok.value == "}":
                lx.next()
                break
            if tok.kind == "op" and tok.value in (";", ","):
                lx.next()
                continue
            if tok.kind == "eof":
                self.err("unterminated task '%s'" % task.name, tok)
            if tok.kind != "id" or tok.value not in FIELD_NAMES:
                self.err("unknown field %s in task '%s'" % (Lexer._desc(tok), task.name), tok)
            field = tok.value
            if field in task.fields:
                self.err("duplicate field '%s' in task '%s'" % (field, task.name), tok)
            task.fields.add(field)
            lx.next()
            lx.accept_op(":")
            getattr(self, "parse_" + field)(task)
        if task.run is None and task.requires is None:
            raise PipeSyntaxError("task '%s' must have a 'run' or 'requires' field" % task.name)
        return task

    def parse_params(self, task):
        lx = self.lx
        lx.expect_op("{")
        seen_default = False
        names = set()
        while True:
            tok = lx.peek()
            if tok.kind == "op" and tok.value == "}":
                lx.next()
                break
            if tok.kind == "op" and tok.value in (";", ","):
                lx.next()
                continue
            name = lx.expect_id("parameter name")
            if name.value in names:
                self.err("duplicate parameter '%s'" % name.value, name)
            names.add(name.value)
            lx.expect_op(":")
            ptype = self.parse_type()
            default = None
            if lx.accept_op("="):
                default = self.parse_expr()
                seen_default = True
            elif seen_default:
                self.err("parameter '%s' without default follows a parameter with a default"
                         % name.value, name)
            task.params.append(Param(name.value, ptype, default, name.line))
            if not (lx.at_op(";") or lx.at_op(",") or lx.at_op("}")):
                # newline-separated is fine as long as next token is a name
                if not lx.at("id"):
                    self.err("unexpected %s in params" % Lexer._desc(lx.peek()), lx.peek())

    def parse_type(self):
        lx = self.lx
        tok = lx.expect_id("type")
        if tok.value == "list":
            lx.expect_op("[")
            inner = self.parse_type()
            lx.expect_op("]")
            return ("list", inner)
        if tok.value not in BASE_TYPES:
            self.err("unknown type '%s'" % tok.value, tok)
        return tok.value

    def parse_run(self, task):
        self.lx.expect_op("{")
        raw = self.lx.raw_block()
        text = textwrap.dedent(raw.strip("\n"))
        text = text.strip("\n")
        task.run = text

    def parse_output(self, task):
        lx = self.lx
        tok = lx.peek()
        if tok.kind == "str":
            lx.next()
            task.output = tok.raw
        else:
            s = lx.raw_line()
            if not s:
                self.err("empty output")
            task.output = s

    def parse_timeout(self, task):
        lx = self.lx
        neg = lx.accept_op("-")
        tok = lx.peek()
        if tok.kind != "num":
            self.err("timeout must be a number", tok)
        lx.next()
        v = float(tok.value)
        if neg:
            v = -v
        if v <= 0:
            self.err("timeout must be positive", tok)
        task.timeout = v

    def parse_success(self, task):
        lx = self.lx
        lx.expect_op("{")
        entries = []
        names = set()
        while True:
            tok = lx.peek()
            if tok.kind == "op" and tok.value == "}":
                lx.next()
                break
            if tok.kind == "op" and tok.value in (";", ","):
                lx.next()
                continue
            name = lx.peek()
            if name.kind == "str":
                lx.next()
                cname = name.raw
            else:
                cname = lx.expect_id("success criteria name").value
            if cname in names:
                self.err("duplicate success criteria '%s'" % cname, name)
            names.add(cname)
            lx.expect_op(":")
            if lx.at_op("{"):
                ob = lx.next()
                stmts = self.parse_stmts_until_close()
                if len(stmts) == 1 and stmts[0][0] == "expr":
                    entries.append((cname, "expr", stmts[0][1]))
                elif len(stmts) == 1 and stmts[0][0] == "return":
                    entries.append((cname, "block", stmts))
                else:
                    if not contains_return(stmts):
                        self.err("multiline success criteria '%s' must have a return" % cname, ob)
                    entries.append((cname, "block", stmts))
            else:
                e = self.parse_expr()
                entries.append((cname, "expr", e))
        task.success = entries

    def parse_requires(self, task):
        self.lx.expect_op("{")
        task.requires = self.parse_stmts_until_close()

    # -- statements
    def parse_stmts_until_close(self):
        lx = self.lx
        stmts = []
        while True:
            tok = lx.peek()
            if tok.kind == "op" and tok.value == "}":
                lx.next()
                return stmts
            if tok.kind == "eof":
                self.err("unterminated block", tok)
            if tok.kind == "op" and tok.value == ";":
                lx.next()
                continue
            stmts.append(self.parse_stmt())

    def parse_block(self):
        lx = self.lx
        if lx.at_op("{"):
            lx.next()
            return self.parse_stmts_until_close()
        return [self.parse_stmt()]

    def end_stmt(self):
        lx = self.lx
        if lx.accept_op(";"):
            return
        # semicolons are optional before a closing brace / newline-separated statements
        return

    def parse_stmt(self):
        lx = self.lx
        tok = lx.peek()
        if tok.kind == "id":
            kw = tok.value
            if kw == "if":
                return self.parse_if()
            if kw == "while":
                lx.next()
                lx.expect_op("(")
                cond = self.parse_expr()
                lx.expect_op(")")
                lx.accept_op(";")
                body = self.parse_block()
                return ("while", cond, body)
            if kw == "for":
                return self.parse_for()
            if kw == "break":
                lx.next()
                self.end_stmt()
                return ("break",)
            if kw == "continue":
                lx.next()
                self.end_stmt()
                return ("continue",)
            if kw == "return":
                lx.next()
                if lx.at_op(";") or lx.at_op("}"):
                    self.end_stmt()
                    return ("return", None)
                e = self.parse_expr()
                self.end_stmt()
                return ("return", e)
        s = self.parse_simple()
        self.end_stmt()
        return s

    def is_decl_start(self):
        lx = self.lx
        tok = lx.peek()
        if tok.kind != "id":
            return False
        if tok.value in BASE_TYPES:
            return lx.at("id", k=1)
        if tok.value == "list" and lx.at_op("[", 1):
            return True
        return False

    def parse_simple(self):
        """Declaration, assignment, increment or expression statement (no ';')."""
        lx = self.lx
        if self.is_decl_start():
            t = self.parse_type()
            name = lx.expect_id("variable name").value
            e = None
            if lx.accept_op("="):
                e = self.parse_expr()
            return ("decl", t, name, e)
        tok = lx.peek()
        if tok.kind == "op" and tok.value in ("++", "--"):
            lx.next()
            target = self.parse_postfix()
            self.check_target(target, tok)
            return ("incdec", target, tok.value)
        e = self.parse_expr()
        tok = lx.peek()
        if tok.kind == "op" and tok.value in ("=", "+=", "-=", "*=", "/=", "%="):
            self.check_target(e, tok)
            lx.next()
            v = self.parse_expr()
            return ("assign", e, tok.value, v)
        if tok.kind == "op" and tok.value in ("++", "--"):
            self.check_target(e, tok)
            lx.next()
            return ("incdec", e, tok.value)
        return ("expr", e)

    def check_target(self, e, tok):
        if e[0] not in ("var", "index"):
            self.err("invalid assignment target", tok)

    def parse_if(self):
        lx = self.lx
        lx.next()  # if
        branches = []
        lx.expect_op("(")
        cond = self.parse_expr()
        lx.expect_op(")")
        body = self.parse_block()
        branches.append((cond, body))
        else_body = None
        while True:
            if lx.at("id", "elif"):
                lx.next()
                lx.expect_op("(")
                c = self.parse_expr()
                lx.expect_op(")")
                branches.append((c, self.parse_block()))
            elif lx.at("id", "else") and lx.at("id", "if", 1):
                lx.next()
                lx.next()
                lx.expect_op("(")
                c = self.parse_expr()
                lx.expect_op(")")
                branches.append((c, self.parse_block()))
            elif lx.at("id", "else"):
                lx.next()
                else_body = self.parse_block()
                break
            else:
                break
        return ("if", branches, else_body)

    def parse_for(self):
        lx = self.lx
        lx.next()  # for
        tok = lx.peek()
        if tok.kind == "op" and tok.value == "(":
            close = ")"
        elif tok.kind == "op" and tok.value == "{":
            close = "}"
        else:
            self.err("expected '(' after for", tok)
        lx.next()
        init = None
        if not lx.at_op(";"):
            init = self.parse_simple()
        lx.expect_op(";")
        cond = None
        if not lx.at_op(";"):
            cond = self.parse_expr()
        lx.expect_op(";")
        step = None
        if not lx.at_op(close):
            step = self.parse_simple()
        lx.expect_op(close)
        lx.accept_op(";")
        body = self.parse_block()
        return ("for", init, cond, step, body)

    # -- expressions
    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        lx = self.lx
        a = self.parse_and()
        while lx.at_op("||") or lx.at("id", "or"):
            lx.next()
            b = self.parse_and()
            a = ("or", a, b)
        return a

    def parse_and(self):
        lx = self.lx
        a = self.parse_eq()
        while lx.at_op("&&") or lx.at("id", "and"):
            lx.next()
            b = self.parse_eq()
            a = ("and", a, b)
        return a

    def parse_eq(self):
        lx = self.lx
        a = self.parse_cmp()
        while lx.at_op("==") or lx.at_op("!="):
            op = lx.next().value
            a = ("bin", op, a, self.parse_cmp())
        return a

    def parse_cmp(self):
        lx = self.lx
        a = self.parse_add()
        while lx.at_op("<") or lx.at_op(">") or lx.at_op("<=") or lx.at_op(">="):
            op = lx.next().value
            a = ("bin", op, a, self.parse_add())
        return a

    def parse_add(self):
        lx = self.lx
        a = self.parse_mul()
        while lx.at_op("+") or lx.at_op("-") or lx.at_op("%"):
            op = lx.next().value
            a = ("bin", op, a, self.parse_mul())
        return a

    def parse_mul(self):
        lx = self.lx
        a = self.parse_unary()
        while lx.at_op("*") or lx.at_op("/"):
            op = lx.next().value
            a = ("bin", op, a, self.parse_unary())
        return a

    def parse_unary(self):
        lx = self.lx
        if lx.at_op("!") or lx.at("id", "not"):
            lx.next()
            return ("un", "!", self.parse_unary())
        if lx.at_op("-"):
            lx.next()
            return ("un", "-", self.parse_unary())
        if lx.at_op("+"):
            lx.next()
            return ("un", "+", self.parse_unary())
        return self.parse_pow()

    def parse_pow(self):
        lx = self.lx
        a = self.parse_postfix()
        if lx.at_op("**"):
            lx.next()
            b = self.parse_unary()
            return ("bin", "**", a, b)
        return a

    def parse_postfix(self):
        lx = self.lx
        e = self.parse_primary()
        while True:
            if lx.at_op("["):
                lx.next()
                idx = self.parse_expr()
                lx.expect_op("]")
                e = ("index", e, idx)
            else:
                return e

    def parse_primary(self):
        lx = self.lx
        tok = lx.peek()
        if tok.kind == "num":
            lx.next()
            return ("lit", tok.value)
        if tok.kind == "str":
            lx.next()
            if len(tok.value) == 1 and isinstance(tok.value[0], str):
                return ("lit", tok.value[0])
            return ("str", tok.value)
        if tok.kind == "ref":
            lx.next()
            return ("ref", tok.value)
        if tok.kind == "env":
            lx.next()
            return ("env", tok.value)
        if tok.kind == "id":
            lx.next()
            if tok.value in ("TRUE", "true", "True"):
                return ("lit", True)
            if tok.value in ("FALSE", "false", "False"):
                return ("lit", False)
            if lx.at_op("("):
                lx.next()
                args = []
                kwargs = []
                while not lx.at_op(")"):
                    if lx.at("id") and lx.at_op("=", 1):
                        k = lx.next().value
                        lx.next()
                        kwargs.append((k, self.parse_expr()))
                    else:
                        if kwargs:
                            self.err("positional argument follows keyword argument", lx.peek())
                        args.append(self.parse_expr())
                    if not lx.accept_op(","):
                        break
                lx.expect_op(")")
                return ("call", tok.value, args, kwargs, tok.line)
            return ("var", tok.value)
        if tok.kind == "op" and tok.value == "(":
            lx.next()
            e = self.parse_expr()
            lx.expect_op(")")
            return e
        if tok.kind == "op" and tok.value == "[":
            lx.next()
            items = []
            while not lx.at_op("]"):
                items.append(self.parse_expr())
                if not lx.accept_op(","):
                    break
            lx.expect_op("]")
            return ("list", items)
        self.err("unexpected %s in expression" % Lexer._desc(tok), tok)


def contains_return(stmts):
    for s in stmts:
        k = s[0]
        if k == "return":
            return True
        if k == "if":
            for _, body in s[1]:
                if contains_return(body):
                    return True
            if s[2] and contains_return(s[2]):
                return True
        if k == "while" and contains_return(s[2]):
            return True
        if k == "for" and contains_return(s[4]):
            return True
    return False


# --------------------------------------------------------------------------
# Values & types
# --------------------------------------------------------------------------

def to_str(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, list):
        raise InvalidPipe("lists cannot be converted to a string")
    return str(v)


def to_text(v):
    """Lenient conversion for textual interpolation into commands."""
    if isinstance(v, list):
        return " ".join(to_text(x) for x in v)
    return to_str(v)


def dyn_cast(s, t):
    s = str(s)
    if isinstance(t, tuple):
        raise InvalidPipe("cannot cast environment value %r to %s" % (s, type_str(t)))
    st = s.strip()
    if t == "string":
        return s
    if t == "int":
        try:
            return int(st)
        except ValueError:
            try:
                f = float(st)
                if f.is_integer():
                    return int(f)
            except ValueError:
                pass
        raise InvalidPipe("cannot cast %r to int" % s)
    if t == "float":
        try:
            return float(st)
        except ValueError:
            raise InvalidPipe("cannot cast %r to float" % s)
    if t == "bool":
        u = st.upper()
        if u in ("TRUE", "1", "YES", "Y", "ON", "T"):
            return True
        if u in ("FALSE", "0", "NO", "N", "OFF", "F", ""):
            return False
        try:
            return float(st) != 0
        except ValueError:
            raise InvalidPipe("cannot cast %r to bool" % s)
    raise InvalidPipe("unknown type %s" % t)


def check_type(v, t, what):
    if isinstance(v, EnvStr) and t != "string":
        return dyn_cast(v, t)
    if isinstance(t, tuple):
        if not isinstance(v, list):
            raise InvalidPipe("type mismatch for %s: expected %s, got %s"
                              % (what, type_str(t), type_name(v)))
        return [check_type(x, t[1], what) for x in v]
    if t == "int":
        if type(v) is int:
            return v
    elif t == "float":
        if type(v) in (int, float):
            return float(v)
    elif t == "bool":
        if type(v) is bool:
            return v
    elif t == "string":
        if isinstance(v, str):
            return str(v)
    raise InvalidPipe("type mismatch for %s: expected %s, got %s"
                      % (what, type_str(t), type_name(v)))


def type_name(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    return type(v).__name__


def auto_cast_env(s):
    return EnvStr(s)


def num(v):
    if isinstance(v, EnvStr):
        st = v.strip()
        try:
            return int(st)
        except ValueError:
            try:
                return float(st)
            except ValueError:
                return v
    return v


def is_num(v):
    return type(v) in (int, float)


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------

BUILTINS = ("len", "contains", "equals", "exists", "fail", "fails")


class Ctx:
    def __init__(self, runner, mode, params, cwd, output, job=None, job_id=None):
        self.runner = runner
        self.mode = mode  # 'requires' | 'success' | 'const'
        self.params = params
        self.cwd = cwd
        self.output = output
        self.job = job
        self.job_id = job_id
        self.vars = {}


def resolve_ref(name, ctx):
    if name.startswith("params."):
        p = name[len("params."):]
        if ctx.params is None or p not in ctx.params:
            raise InvalidPipe("undefined parameter '%s'" % p)
        return ctx.params[p]
    if name == "workspace":
        if ctx.runner is None:
            raise InvalidPipe("${workspace} not available here")
        return ctx.runner.workspace
    if name == "output":
        if ctx.output is not None:
            return ctx.output
        if ctx.cwd is not None:
            return ctx.cwd
        raise InvalidPipe("${output} not available here")
    return resolve_env(name, ctx)


def get_env(ctx_or_runner):
    r = ctx_or_runner.runner if isinstance(ctx_or_runner, Ctx) else ctx_or_runner
    if r is None:
        return os.environ
    return r.env


def resolve_env(name, ctx):
    env = get_env(ctx)
    if name not in env:
        raise InvalidPipe("undefined environment variable '%s'" % name)
    return auto_cast_env(env[name])


def eval_str_parts(parts, ctx):
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif p[0] == "ref":
            out.append(to_str(resolve_ref(p[1], ctx)))
        else:
            env = get_env(ctx)
            if p[1] in env:
                out.append(env[p[1]])
            else:
                out.append("$" + p[1])
    return "".join(out)


def truthy(v):
    if isinstance(v, EnvStr):
        try:
            return dyn_cast(v, "bool")
        except InvalidPipe:
            return bool(v)
    return bool(v)


def binop(op, a, b):
    if op == "%":
        if isinstance(a, str) and not isinstance(b, str) and not isinstance(b, list):
            return to_str(a) + to_str(b)
        if isinstance(b, str) and not isinstance(a, str) and not isinstance(a, list):
            return to_str(a) + to_str(b)
        if isinstance(a, str) and isinstance(b, str):
            return str(a) + str(b)
        if isinstance(a, list) or isinstance(b, list):
            raise InvalidPipe("lists cannot be converted to a string")
        if is_num(a) and is_num(b):
            if b == 0:
                raise InvalidPipe("modulo by zero")
            return a % b
        return to_str(a) + to_str(b)
    if op in ("==", "!="):
        if isinstance(a, EnvStr) or isinstance(b, EnvStr):
            if is_num(a) or is_num(b):
                a, b = num(a), num(b)
            elif isinstance(a, bool) or isinstance(b, bool):
                a = dyn_cast(a, "bool") if isinstance(a, EnvStr) else a
                b = dyn_cast(b, "bool") if isinstance(b, EnvStr) else b
        if isinstance(a, str) and isinstance(b, str):
            a, b = str(a), str(b)
        r = (a == b) and (isinstance(a, bool) == isinstance(b, bool)
                          or not (isinstance(a, bool) or isinstance(b, bool)))
        return r if op == "==" else not r
    if isinstance(a, EnvStr) and (is_num(b) or isinstance(b, EnvStr)):
        a = num(a)
    if isinstance(b, EnvStr) and is_num(a):
        b = num(b)
    if isinstance(a, EnvStr) and isinstance(b, EnvStr) and op not in ("+",):
        a, b = num(a), num(b)
    try:
        if op in ("<", ">", "<=", ">="):
            if isinstance(a, str) != isinstance(b, str) or isinstance(a, list) or isinstance(b, list):
                raise TypeError
            if isinstance(a, str):
                a, b = str(a), str(b)
            if op == "<":
                return a < b
            if op == ">":
                return a > b
            if op == "<=":
                return a <= b
            return a >= b
        if op == "+":
            if isinstance(a, list) and isinstance(b, list):
                return a + b
            if isinstance(a, str) and isinstance(b, str):
                return str(a) + str(b)
            if is_num(a) and is_num(b):
                return a + b
            raise TypeError
        if not (is_num(a) and is_num(b)):
            raise TypeError
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise InvalidPipe("division by zero")
            if type(a) is int and type(b) is int and a % b == 0:
                return a // b
            return a / b
        if op == "**":
            return a ** b
    except TypeError:
        raise InvalidPipe("type mismatch: cannot apply '%s' to %s and %s"
                          % (op, type_name(a), type_name(b)))
    raise InvalidPipe("unknown operator %s" % op)


def eval_expr(e, ctx):
    k = e[0]
    if k == "lit":
        return e[1]
    if k == "str":
        return eval_str_parts(e[1], ctx)
    if k == "ref":
        return resolve_ref(e[1], ctx)
    if k == "env":
        return resolve_env(e[1], ctx)
    if k == "var":
        return lookup_var(e[1], ctx)
    if k == "list":
        return [eval_expr(x, ctx) for x in e[1]]
    if k == "index":
        a = eval_expr(e[1], ctx)
        i = num(eval_expr(e[2], ctx))
        if not isinstance(a, (list, str)):
            raise InvalidPipe("cannot index a %s" % type_name(a))
        if type(i) is not int:
            raise InvalidPipe("index must be an int")
        try:
            return a[i]
        except IndexError:
            raise InvalidPipe("index %d out of range" % i)
    if k == "un":
        v = eval_expr(e[2], ctx)
        if e[1] == "!":
            return not truthy(v)
        v = num(v)
        if not is_num(v):
            raise InvalidPipe("cannot negate a %s" % type_name(v))
        return -v if e[1] == "-" else v
    if k == "and":
        a = eval_expr(e[1], ctx)
        if not truthy(a):
            return False
        return truthy(eval_expr(e[2], ctx))
    if k == "or":
        a = eval_expr(e[1], ctx)
        if truthy(a):
            return True
        return truthy(eval_expr(e[2], ctx))
    if k == "bin":
        return binop(e[1], eval_expr(e[2], ctx), eval_expr(e[3], ctx))
    if k == "call":
        return eval_call(e, ctx, blocking=False)
    raise InvalidPipe("bad expression")


def lookup_var(name, ctx):
    if name in ctx.vars:
        return ctx.vars[name]
    if ctx.mode == "success" and ctx.job is not None:
        if name == "stdout":
            return ctx.job["stdout"]
        if name == "stderr":
            return ctx.job["stderr"]
        if name in ("exit_code", "exitcode", "code"):
            return ctx.job["exit_code"]
    if ctx.params is not None and name in ctx.params:
        return ctx.params[name]
    raise InvalidPipe("undefined variable '%s'" % name)


def resolve_path(p, ctx):
    p = to_str(p)
    if os.path.isabs(p):
        return p
    base = ctx.cwd or (ctx.runner.workspace if ctx.runner else os.getcwd())
    return os.path.join(base, p)


def read_target(node, ctx):
    """Returns (kind, value). kind in stdout/stderr/exit_code/file."""
    if node[0] == "var" and node[1] not in ctx.vars:
        n = node[1]
        if n in ("stdout", "stderr"):
            if ctx.job is None:
                raise InvalidPipe("'%s' is only available in success" % n)
            return n, ctx.job[n]
        if n in ("exit_code", "exitcode", "code"):
            if ctx.job is None:
                raise InvalidPipe("'%s' is only available in success" % n)
            return "exit_code", ctx.job["exit_code"]
    v = eval_expr(node, ctx)
    if isinstance(v, list):
        raise InvalidPipe("expected a file path, got a list")
    path = resolve_path(v, ctx)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "file", f.read()
    except (OSError, IsADirectoryError):
        return "file", None


def eval_call(e, ctx, blocking):
    _, name, args, kwargs, line = e
    if name == "len":
        if len(args) != 1 or kwargs:
            raise InvalidPipe("len() takes exactly one argument")
        v = eval_expr(args[0], ctx)
        if not isinstance(v, (list, str)):
            raise InvalidPipe("len() requires a list or string")
        return len(v)
    if name == "exists":
        if len(args) != 1 or kwargs:
            raise InvalidPipe("exists() takes exactly one argument")
        return os.path.exists(resolve_path(eval_expr(args[0], ctx), ctx))
    if name == "contains":
        if len(args) != 2 or kwargs:
            raise InvalidPipe("contains() takes exactly two arguments")
        kind, text = read_target(args[0], ctx)
        pat = eval_expr(args[1], ctx)
        if text is None:
            return False
        if kind == "exit_code":
            text = str(text)
        pat = to_str(pat)
        if pat in text:
            return True
        try:
            return re.search(pat, text, re.M) is not None
        except re.error:
            return False
    if name == "equals":
        if len(args) != 2 or kwargs:
            raise InvalidPipe("equals() takes exactly two arguments")
        kind, val = read_target(args[0], ctx)
        other = eval_expr(args[1], ctx)
        if kind == "exit_code":
            if val is None:
                return False
            o = num(other) if isinstance(other, str) else other
            if isinstance(o, str):
                try:
                    o = int(o.strip())
                except ValueError:
                    return False
            if isinstance(o, bool):
                return False
            return val == o
        if val is None:
            return False
        if isinstance(other, list):
            raise InvalidPipe("lists cannot be converted to a string")
        return val == to_str(other)
    if name in ("fail", "fails"):
        if ctx.mode != "requires":
            raise InvalidPipe("%s() is only allowed in requires" % name)
        if len(args) != 1 or kwargs or args[0][0] != "call" or args[0][1] in BUILTINS:
            raise InvalidPipe("%s() takes a single task invocation" % name)
        ok = run_task_call(args[0], ctx)
        return not ok
    # task call
    if ctx.mode != "requires":
        raise InvalidPipe("task '%s' can only be invoked in requires" % name)
    return run_task_call(e, ctx)


def run_task_call(e, ctx):
    _, name, args, kwargs, line = e
    runner = ctx.runner
    if name not in runner.tasks:
        raise InvalidPipe("undefined task '%s'" % name)
    pos = [eval_expr(a, ctx) for a in args]
    kw = [(k, eval_expr(v, ctx)) for k, v in kwargs]
    task = runner.tasks[name]
    bound = bind_args(task, pos, kw, runner)
    return runner.run_task(task, bound, parent_id=ctx.job_id, parent_cwd=ctx.cwd)


def assign_target(target, value, ctx, decl_check=True):
    if target[0] == "var":
        name = target[1]
        if name in ctx.vars:
            t = ctx.vars_types.get(name) if hasattr(ctx, "vars_types") else None
            if t is not None:
                value = check_type(value, t, "variable '%s'" % name)
        ctx.vars[name] = value
        return
    if target[0] == "index":
        arr = eval_expr(target[1], ctx)
        i = num(eval_expr(target[2], ctx))
        if not isinstance(arr, list):
            raise InvalidPipe("cannot assign into a %s" % type_name(arr))
        if type(i) is not int:
            raise InvalidPipe("index must be an int")
        try:
            arr[i] = value
        except IndexError:
            raise InvalidPipe("index %d out of range" % i)
        return
    raise InvalidPipe("invalid assignment target")


def exec_stmts(stmts, ctx):
    for s in stmts:
        exec_stmt(s, ctx)


def exec_stmt(s, ctx):
    k = s[0]
    if k == "expr":
        e = s[1]
        if ctx.mode == "requires" and e[0] == "call" and e[1] not in ("len", "contains", "equals", "exists"):
            ok = eval_call(e, ctx, blocking=True)
            if not ok:
                raise RequiresFailed()
            return
        eval_expr(e, ctx)
        return
    if k == "decl":
        _, t, name, e = s
        if e is None:
            v = default_for(t)
        else:
            v = check_type(eval_expr(e, ctx), t, "variable '%s'" % name)
        if not hasattr(ctx, "vars_types"):
            ctx.vars_types = {}
        ctx.vars_types[name] = t
        ctx.vars[name] = v
        return
    if k == "assign":
        _, target, op, e = s
        v = eval_expr(e, ctx)
        if op != "=":
            cur = eval_expr(target, ctx)
            v = binop(op[0], cur, v)
        assign_target(target, v, ctx)
        return
    if k == "incdec":
        _, target, op = s
        cur = num(eval_expr(target, ctx))
        if not is_num(cur):
            raise InvalidPipe("cannot increment a %s" % type_name(cur))
        assign_target(target, cur + 1 if op == "++" else cur - 1, ctx)
        return
    if k == "if":
        _, branches, else_body = s
        for cond, body in branches:
            if truthy(eval_expr(cond, ctx)):
                exec_stmts(body, ctx)
                return
        if else_body is not None:
            exec_stmts(else_body, ctx)
        return
    if k == "while":
        _, cond, body = s
        while truthy(eval_expr(cond, ctx)):
            try:
                exec_stmts(body, ctx)
            except BreakEx:
                break
            except ContinueEx:
                continue
        return
    if k == "for":
        _, init, cond, step, body = s
        if init is not None:
            exec_stmt(init, ctx)
        while cond is None or truthy(eval_expr(cond, ctx)):
            try:
                exec_stmts(body, ctx)
            except BreakEx:
                break
            except ContinueEx:
                pass
            if step is not None:
                exec_stmt(step, ctx)
        return
    if k == "break":
        raise BreakEx()
    if k == "continue":
        raise ContinueEx()
    if k == "return":
        raise ReturnEx(None if s[1] is None else eval_expr(s[1], ctx))
    raise InvalidPipe("unknown statement")


def default_for(t):
    if isinstance(t, tuple):
        return []
    return {"int": 0, "float": 0.0, "bool": False, "string": ""}[t]


class RequiresFailed(Exception):
    pass


# --------------------------------------------------------------------------
# Argument binding
# --------------------------------------------------------------------------

def eval_default(p, runner):
    ctx = Ctx(runner, "const", {}, None, None)
    return eval_expr(p.default, ctx)


def bind_args(task, pos, kw, runner, static=False):
    ps = task.params
    names = [p.name for p in ps]
    if len(pos) > len(ps):
        raise InvalidPipe("task '%s' takes %d argument(s) but %d were given"
                          % (task.name, len(ps), len(pos)))
    vals = {}
    for p, v in zip(ps, pos):
        vals[p.name] = v
    for k, v in kw:
        if k not in names:
            raise InvalidPipe("task '%s' has no parameter '%s'" % (task.name, k))
        if k in vals:
            raise InvalidPipe("multiple values for parameter '%s' of task '%s'" % (k, task.name))
        vals[k] = v
    out = {}
    for p in ps:
        what = "parameter '%s' of task '%s'" % (p.name, task.name)
        if p.name in vals:
            out[p.name] = check_type(vals[p.name], p.type, what)
        elif p.default is not None:
            if static:
                continue
            v = eval_default(p, runner)
            if isinstance(v, EnvStr) and p.type == "string":
                out[p.name] = str(v)
            elif isinstance(v, EnvStr) and isinstance(p.type, tuple):
                raise InvalidPipe("environment variables cannot be used as list defaults (%s)" % what)
            else:
                out[p.name] = check_type(v, p.type, what)
        else:
            raise InvalidPipe("missing argument '%s' for task '%s'" % (p.name, task.name))
    return out


# --------------------------------------------------------------------------
# Static validation
# --------------------------------------------------------------------------

def is_literal(e):
    k = e[0]
    if k == "lit":
        return True
    if k == "list":
        return all(is_literal(x) for x in e[1])
    if k == "un" and e[1] == "-" and e[2][0] == "lit" and is_num(e[2][1]):
        return True
    return False


def literal_value(e):
    if e[0] == "lit":
        return e[1]
    if e[0] == "list":
        return [literal_value(x) for x in e[1]]
    return -e[2][1]


def iter_exprs_in_stmts(stmts):
    for s in stmts:
        k = s[0]
        if k == "expr":
            yield s[1]
        elif k == "decl":
            if s[3] is not None:
                yield s[3]
        elif k == "assign":
            yield s[1]
            yield s[3]
        elif k == "incdec":
            yield s[1]
        elif k == "if":
            for c, b in s[1]:
                yield c
                yield from iter_exprs_in_stmts(b)
            if s[2]:
                yield from iter_exprs_in_stmts(s[2])
        elif k == "while":
            yield s[1]
            yield from iter_exprs_in_stmts(s[2])
        elif k == "for":
            if s[1] is not None:
                yield from iter_exprs_in_stmts([s[1]])
            if s[2] is not None:
                yield s[2]
            if s[3] is not None:
                yield from iter_exprs_in_stmts([s[3]])
            yield from iter_exprs_in_stmts(s[4])
        elif k == "return":
            if s[1] is not None:
                yield s[1]


def iter_subexprs(e):
    yield e
    k = e[0]
    if k == "list":
        for x in e[1]:
            yield from iter_subexprs(x)
    elif k == "index":
        yield from iter_subexprs(e[1])
        yield from iter_subexprs(e[2])
    elif k == "un":
        yield from iter_subexprs(e[2])
    elif k in ("and", "or"):
        yield from iter_subexprs(e[1])
        yield from iter_subexprs(e[2])
    elif k == "bin":
        yield from iter_subexprs(e[2])
        yield from iter_subexprs(e[3])
    elif k == "call":
        for a in e[2]:
            yield from iter_subexprs(a)
        for _, a in e[3]:
            yield from iter_subexprs(a)


def all_subexprs(stmts):
    for e in iter_exprs_in_stmts(stmts):
        yield from iter_subexprs(e)


REF_RE = re.compile(r"\$\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}")


def check_param_refs_text(text, task):
    names = {p.name for p in task.params}
    for m in REF_RE.finditer(text or ""):
        if m.group(1) not in names:
            raise InvalidPipe("task '%s' references undefined parameter '%s'"
                              % (task.name, m.group(1)))


def check_param_refs_expr(e, task):
    names = {p.name for p in task.params}
    refs = []
    if e[0] == "ref":
        refs.append(e[1])
    elif e[0] == "str":
        refs.extend(p[1] for p in e[1] if not isinstance(p, str) and p[0] == "ref")
    for r in refs:
        if r.startswith("params.") and r[len("params."):] not in names:
            raise InvalidPipe("task '%s' references undefined parameter '%s'"
                              % (task.name, r[len("params."):]))


def validate(tasks, runner):
    graph = {}
    for task in tasks.values():
        # parameter defaults
        for p in task.params:
            if p.default is not None:
                d = p.default
                if is_literal(d):
                    check_type(literal_value(d), p.type,
                               "default of parameter '%s' of task '%s'" % (p.name, task.name))
                elif d[0] in ("env", "ref"):
                    if d[0] == "ref" and (d[1].startswith("params.") or d[1] in ("workspace", "output")):
                        raise InvalidPipe("invalid default for parameter '%s' of task '%s'"
                                          % (p.name, task.name))
                    if isinstance(p.type, tuple):
                        raise InvalidPipe("environment variables cannot be list defaults "
                                          "(parameter '%s' of task '%s')" % (p.name, task.name))
                elif d[0] == "str":
                    check_type("", p.type,
                               "default of parameter '%s' of task '%s'" % (p.name, task.name))
                elif d[0] == "var":
                    # bare word -> treat as string literal
                    if p.type != "string":
                        raise InvalidPipe("type mismatch for default of parameter '%s' of task '%s'"
                                          % (p.name, task.name))
                    p.default = ("lit", d[1])
                else:
                    raise InvalidPipe("default of parameter '%s' of task '%s' must be a literal"
                                      % (p.name, task.name))
        check_param_refs_text(task.run, task)
        check_param_refs_text(task.output, task)
        calls = []
        if task.requires:
            for e in all_subexprs(task.requires):
                check_param_refs_expr(e, task)
                if e[0] == "call":
                    if e[1] in ("fail", "fails"):
                        if len(e[2]) != 1 or e[3] or e[2][0][0] != "call" or e[2][0][1] in BUILTINS:
                            raise InvalidPipe("%s() in task '%s' takes a single task invocation"
                                              % (e[1], task.name))
                    elif e[1] in BUILTINS:
                        pass
                    else:
                        if e[1] not in tasks:
                            raise InvalidPipe("task '%s' requires undefined task '%s'"
                                              % (task.name, e[1]))
                        calls.append(e[1])
                        if all(is_literal(a) for a in e[2]) and all(is_literal(a) for _, a in e[3]):
                            bind_args(tasks[e[1]], [literal_value(a) for a in e[2]],
                                      [(k, literal_value(a)) for k, a in e[3]], runner, static=True)
        if task.success:
            for cname, kind, node in task.success:
                stmts = [("expr", node)] if kind == "expr" else node
                for e in all_subexprs(stmts):
                    check_param_refs_expr(e, task)
                    if e[0] == "call":
                        if e[1] in ("fail", "fails"):
                            raise InvalidPipe("%s() is only allowed in requires (task '%s')"
                                              % (e[1], task.name))
                        if e[1] not in BUILTINS:
                            if e[1] in tasks:
                                raise InvalidPipe("task invocation '%s' not allowed in success of '%s'"
                                                  % (e[1], task.name))
                            raise InvalidPipe("undefined function '%s' in task '%s'"
                                              % (e[1], task.name))
        graph[task.name] = calls
    # cycle detection
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def dfs(n, stack):
        color[n] = GREY
        stack.append(n)
        for m in graph[n]:
            if color[m] == GREY:
                cyc = stack[stack.index(m):] + [m]
                raise InvalidPipe("circular dependency: %s" % " -> ".join(cyc))
            if color[m] == WHITE:
                dfs(m, stack)
        stack.pop()
        color[n] = BLACK

    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
    for n in graph:
        if color[n] == WHITE:
            dfs(n, [])


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

TEMPLATE_RE = re.compile(r"\$\{([^}]*)\}")


def clean_text(s):
    return (s.replace("‘", "'").replace("’", "'")
            .replace("“", '"').replace("”", '"'))


def within(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Runner:
    def __init__(self, tasks, workspace, outdir, env):
        self.tasks = tasks
        self.workspace = workspace
        self.outdir = outdir
        self.env = env
        self.job_counter = 0
        self.stack = []
        self.jobs_path = os.path.join(outdir, "jobs.jsonl")
        self.jobs_file = None

    def emit(self, event, job_id, **extra):
        rec = {"event": event, "job_id": job_id}
        rec.update(extra)
        rec["timestamp"] = time.time()
        sys.stdout.write(json.dumps(rec) + "\n")
        sys.stdout.flush()

    def write_job(self, rec):
        if self.jobs_file is None:
            self.jobs_file = open(self.jobs_path, "a", encoding="utf-8")
        self.jobs_file.write(json.dumps(rec) + "\n")
        self.jobs_file.flush()

    def template(self, text, params, cwd, output, for_shell):
        def rep(m):
            name = m.group(1).strip()
            if name.startswith("params."):
                p = name[len("params."):]
                if p not in params:
                    raise InvalidPipe("undefined parameter '%s'" % p)
                return to_text(params[p])
            if name == "workspace":
                return self.workspace
            if name == "output":
                return output if output is not None else cwd
            if for_shell:
                return m.group(0)
            if name in self.env:
                return self.env[name]
            raise InvalidPipe("undefined variable '${%s}'" % name)
        s = TEMPLATE_RE.sub(rep, text)
        if not for_shell:
            def rep_env(m):
                if m.group(1) in self.env:
                    return self.env[m.group(1)]
                return m.group(0)
            s = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", rep_env, s)
        return s

    def run_task(self, task, params, parent_id=None, parent_cwd=None):
        if task.name in self.stack:
            raise InvalidPipe("recursive invocation of task '%s'" % task.name)
        base = parent_cwd if parent_cwd is not None else self.workspace
        output_rel = None
        output_abs = None
        if task.output is not None:
            raw_out = task.output
            out = self.template(raw_out, params, base, None, for_shell=False)
            if not out.strip():
                raise InvalidPipe("task '%s' has an empty output path" % task.name)
            if os.path.isabs(out):
                output_abs = os.path.normpath(out)
            else:
                output_abs = os.path.normpath(os.path.join(base, out))
            if not within(output_abs, self.workspace):
                raise InvalidPipe("output path '%s' of task '%s' escapes the workspace"
                                  % (out, task.name))
            output_rel = os.path.relpath(output_abs, self.workspace)
            cwd = output_abs
        else:
            cwd = base

        self.job_counter += 1
        job_id = self.job_counter
        start = time.time()
        self.emit("TASK_STARTED", job_id, task=task.name, params=params, parent=parent_id)
        self.stack.append(task.name)
        try:
            if output_abs is not None:
                os.makedirs(output_abs, exist_ok=True)
            job = {"stdout": "", "stderr": "", "exit_code": None, "timed_out": False}
            ok = True
            reason = None
            if task.requires is not None:
                ctx = Ctx(self, "requires", params, cwd, output_abs, job=None, job_id=job_id)
                try:
                    exec_stmts(task.requires, ctx)
                except RequiresFailed:
                    ok = False
                    reason = "requires failed"
                except ReturnEx as r:
                    if r.value is not None and not truthy(r.value):
                        ok = False
                        reason = "requires returned FALSE"
                except (BreakEx, ContinueEx):
                    raise InvalidPipe("break/continue outside of loop in task '%s'" % task.name)
            success = {}
            if ok:
                self.emit("TASK_RUNNING", job_id, task=task.name, params=params, parent=parent_id)
                if task.run is not None:
                    script = self.template(task.run, params, cwd, output_abs, for_shell=True)
                    self.execute(script, cwd, task.timeout, job)
                else:
                    job["exit_code"] = 0
                if task.success:
                    sctx = Ctx(self, "success", params, cwd, output_abs, job=job, job_id=job_id)
                    for cname, kind, node in task.success:
                        sctx.vars = {}
                        sctx.vars_types = {}
                        if kind == "expr":
                            v = eval_expr(node, sctx)
                        else:
                            try:
                                exec_stmts(node, sctx)
                                v = False
                            except ReturnEx as r:
                                v = r.value
                            except (BreakEx, ContinueEx):
                                raise InvalidPipe("break/continue outside of loop")
                        success[cname] = bool(truthy(v))
                ok = (job["exit_code"] == 0 and not job["timed_out"]
                      and all(success.values()))
            duration = round(time.time() - start, 4)
            rec = {
                "job_id": job_id,
                "task": task.name,
                "params": params,
                "stdout": job["stdout"],
                "stderr": job["stderr"],
                "timed_out": job["timed_out"],
                "success": success,
                "output": output_rel,
                "exit_code": job["exit_code"],
                "parent": parent_id,
                "duration": duration,
            }
            self.write_job(rec)
            extra = dict(task=task.name, params=params, parent=parent_id,
                         exit_code=job["exit_code"], duration=duration)
            if reason:
                extra["reason"] = reason
            self.emit("TASK_COMPLETED" if ok else "TASK_FAILED", job_id, **extra)
            return ok
        finally:
            self.stack.pop()

    def execute(self, script, cwd, timeout, job):
        os.makedirs(cwd, exist_ok=True)
        full = "shopt -s xpg_echo 2>/dev/null\n" + script + "\n"
        bash = shutil.which("bash") or "/bin/bash"
        try:
            proc = subprocess.Popen([bash, "-c", full], cwd=cwd, env=self.env,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as ex:
            job["stdout"] = ""
            job["stderr"] = str(ex)
            job["exit_code"] = 127
            return
        try:
            out, err = proc.communicate(timeout=timeout)
            code = proc.returncode
            if code < 0:
                code = 128 - code
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            code = 124
            timed_out = True
        job["stdout"] = clean_text(out.decode("utf-8", errors="replace"))
        job["stderr"] = clean_text(err.decode("utf-8", errors="replace"))
        job["exit_code"] = code
        job["timed_out"] = timed_out


# --------------------------------------------------------------------------
# Config & entry
# --------------------------------------------------------------------------

def env_value_str(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return str(v)


def load_config(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as ex:
        raise PipeSyntaxError("invalid TOML config: %s" % ex)
    except OSError as ex:
        raise PipeSyntaxError("cannot read config: %s" % ex)
    entry = data.get("entry", "main")
    if not isinstance(entry, str):
        raise PipeSyntaxError("'entry' must be a string")
    clean = data.get("clean_cwd", False)
    if isinstance(clean, str):
        u = clean.strip().upper()
        if u in ("TRUE", "1", "YES"):
            clean = True
        elif u in ("FALSE", "0", "NO", ""):
            clean = False
        else:
            raise PipeSyntaxError("'clean_cwd' must be a bool")
    if not isinstance(clean, bool):
        raise PipeSyntaxError("'clean_cwd' must be a bool")
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise PipeSyntaxError("[env] must be a table")
    env = {str(k): env_value_str(v) for k, v in env.items()}
    return entry, clean, env


def parse_entry(text):
    try:
        p = Parser(text)
        if not p.lx.at("id"):
            raise PipeSyntaxError("invalid entry '%s'" % text)
        node = p.parse_primary()
        if not p.lx.at("eof"):
            raise PipeSyntaxError("invalid entry '%s'" % text)
    except PipeSyntaxError:
        raise PipeSyntaxError("invalid entry '%s'" % text)
    if node[0] == "call":
        return node[1], node[2], node[3]
    if node[0] == "var":
        return node[1], [], []
    raise PipeSyntaxError("invalid entry '%s'" % text)


def clean_dir(path, keep):
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if any(within(k, full) for k in keep):
            # keep directories that contain the output directory
            if os.path.realpath(full) in [os.path.realpath(k) for k in keep]:
                continue
            if os.path.isdir(full) and not os.path.islink(full):
                clean_dir(full, keep)
            continue
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except OSError:
            pass


class ArgParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("SYNTAX_ERROR:%s\n" % message)
        sys.exit(2)


def main(argv=None):
    ap = ArgParser(description="Execute a pipeline")
    ap.add_argument("pipeline")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)

    try:
        entry_text, clean, cfg_env = "main", False, {}
        if a.config:
            entry_text, clean, cfg_env = load_config(a.config)
        try:
            with open(a.pipeline, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as ex:
            raise PipeSyntaxError("cannot read pipeline: %s" % ex)
        tasks = Parser(text).parse_file()
        entry_name, entry_args, entry_kwargs = parse_entry(entry_text.strip())

        workspace = os.path.realpath(os.path.abspath(a.workspace))
        outdir = os.path.abspath(a.output)
        env = dict(os.environ)
        env.update(cfg_env)
        env["WORKSPACE"] = env.get("WORKSPACE", workspace)

        runner = Runner(tasks, workspace, outdir, env)
        validate(tasks, runner)
        if entry_name not in tasks:
            raise InvalidPipe("entry task '%s' is not defined" % entry_name)

        ectx = Ctx(runner, "const", {}, workspace, None)
        pos = [eval_expr(x, ectx) for x in entry_args]
        kw = [(k, eval_expr(v, ectx)) for k, v in entry_kwargs]
        params = bind_args(tasks[entry_name], pos, kw, runner)

        if clean and os.path.isdir(workspace):
            clean_dir(workspace, [outdir])
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(outdir, exist_ok=True)
        # fresh jobs file
        with open(runner.jobs_path, "w", encoding="utf-8"):
            pass
        ok = runner.run_task(tasks[entry_name], params, parent_id=None, parent_cwd=None)
        if runner.jobs_file:
            runner.jobs_file.close()
        return 0 if ok else 1
    except PipeSyntaxError as ex:
        sys.stdout.flush()
        sys.stderr.write("SYNTAX_ERROR:%s\n" % ex)
        return 2
    except InvalidPipe as ex:
        sys.stdout.flush()
        sys.stderr.write("INVALID_PIPE:%s\n" % ex)
        return 3
    except RecursionError:
        sys.stderr.write("INVALID_PIPE:recursion too deep\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
