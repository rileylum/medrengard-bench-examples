#!/usr/bin/env python3
"""sheeteval: grade a submission workbook against an answer-key workbook."""
import argparse
import datetime
import os
import re
import sys
import traceback

MANIFEST_HEADERS = ("name", "sheet", "tab")
CHECKLIST_HEADERS = ("number", "cell-id", "id")
COORD_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]+)$")


class FatalError(Exception):
    pass


class Reporter:
    def __init__(self, quiet, log):
        self.quiet = quiet
        self.log_enabled = log

    def warn(self, msg):
        if not self.quiet:
            sys.stderr.write("Warning: %s\n" % msg)

    def log(self, msg):
        if self.log_enabled:
            sys.stderr.write("LOG %s\n" % msg)


def fmt_num(value):
    v = round(float(value), 2)
    if v == int(v):
        return str(int(v))
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def find_header_col(ws, candidates):
    headers = {}
    for cell in next(ws.iter_rows(min_row=1, max_row=1), ()):
        val = cell.value
        if isinstance(val, str):
            headers.setdefault(cell.column, val.strip())
    for col in sorted(headers):
        if headers[col] in candidates:
            return col
    return None


def find_exact_col(ws, name):
    for cell in next(ws.iter_rows(min_row=1, max_row=1), ()):
        if isinstance(cell.value, str) and cell.value.strip().lower() == name:
            return cell.column
    return None


def parse_coord(raw):
    if not isinstance(raw, str):
        raise ValueError("invalid cell coordinate: %r" % (raw,))
    m = COORD_RE.match(raw.strip())
    if not m:
        raise ValueError("invalid cell coordinate: %r" % (raw,))
    from openpyxl.utils import column_index_from_string
    col_letters, row = m.group(1).upper(), int(m.group(2))
    col = column_index_from_string(col_letters)
    if row < 1 or row > 1048576 or col > 16384:
        raise ValueError("invalid cell coordinate: %r" % (raw,))
    return raw.strip(), row, col


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def values_equal(a, b):
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, datetime.datetime) != isinstance(b, datetime.datetime):
        return False
    if type(a) is not type(b):
        return False
    return a == b


def parse_annotation(cell):
    """Return (kind, points, rule, data) or None if annotation missing/invalid."""
    import yaml
    comment = getattr(cell, "comment", None) if cell is not None else None
    if comment is None:
        return None
    text = comment.text
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rule = data.get("rule")
    if not isinstance(rule, dict):
        return None
    if "kind" not in rule or "points" not in rule:
        return None
    kind, points = rule["kind"], rule["points"]
    if kind is None or not is_number(points):
        return None
    if isinstance(points, float) and points != points:
        return None
    return kind, points, rule, data


def get_tolerance(rule, where, rep):
    if "tolerance" not in rule or rule["tolerance"] is None:
        return None
    tol = rule["tolerance"]
    if not is_number(tol) or tol != tol:
        rep.warn("%s: invalid rule.tolerance %r; tolerance ignored" % (where, tol))
        return None
    if tol < 0:
        rep.warn("%s: negative rule.tolerance %s; tolerance ignored" % (where, tol))
        return None
    return tol


def get_mode(rule, where, rep):
    if "mode" not in rule or rule["mode"] is None:
        return "award"
    mode = rule["mode"]
    if isinstance(mode, str) and mode.strip().lower() in ("award", "penalty"):
        return mode.strip().lower()
    rep.warn("%s: unrecognized rule.mode %r; defaulting to 'award'" % (where, mode))
    return "award"


def compare(expected, actual, tol):
    if tol is not None and is_number(expected) and is_number(actual):
        try:
            return abs(float(expected) - float(actual)) <= tol
        except (OverflowError, ValueError):
            return False
    return values_equal(expected, actual)


# ---------------------------------------------------------------------------
# Formula engine
# ---------------------------------------------------------------------------

class FormulaError(Exception):
    """Base class for formula-evaluation failures that score the rule 0."""


class MalformedFormula(FormulaError):
    pass


class UnsupportedFunction(FormulaError):
    def __init__(self, name):
        FormulaError.__init__(self, name)
        self.name = name


class CircularReference(FormulaError):
    def __init__(self, cell):
        FormulaError.__init__(self, cell)
        self.cell = cell


class XLError(object):
    """A spreadsheet error value (e.g. #DIV/0!) that propagates through formulas."""

    def __init__(self, code):
        self.code = code

    def __repr__(self):
        return self.code

    def __eq__(self, other):
        return isinstance(other, XLError) and other.code == self.code

    def __hash__(self):
        return hash(self.code)


DIV0 = XLError("#DIV/0!")
VALUE_ERR = XLError("#VALUE!")
NUM_ERR = XLError("#NUM!")
REF_ERR = XLError("#REF!")


class XLRaise(Exception):
    def __init__(self, err):
        Exception.__init__(self, err.code)
        self.err = err


SUPPORTED_FUNCTIONS = ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTIF", "ABS", "ROUND",
                       "INT", "MOD", "IF")

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<qsheet>'(?:[^']|'')+'!)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<ref>\$?[A-Za-z]{1,3}\$?\d+(?![A-Za-z0-9_(.]))
  | (?P<ident>[A-Za-z_\\][A-Za-z0-9_.]*)
  | (?P<op><>|<=|>=|[-+*/^&=<>%:(),!])
""", re.VERBOSE)


def tokenize(text, strict=True):
    """Return a list of (type, text) tokens. In non-strict mode never fails."""
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            if strict:
                if text[pos] == '"':
                    raise MalformedFormula("unterminated string literal")
                raise MalformedFormula("unexpected character %r" % text[pos])
            if text[pos] == '"':
                tokens.append(("str", text[pos:]))
                break
            tokens.append(("other", text[pos]))
            pos += 1
            continue
        kind = m.lastgroup
        if kind != "ws":
            tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


def normalize_formula(formula):
    text = formula.strip()
    if text.startswith("="):
        text = text[1:]
    toks = tokenize(text, strict=False)
    return " ".join(t.lower() for _, t in toks)


def formula_text(value):
    """Return formula text (with leading '=') if value is a formula, else None."""
    if isinstance(value, str):
        if value.startswith("=") and len(value) > 1:
            return value
        return None
    text = getattr(value, "text", None)  # openpyxl ArrayFormula
    if isinstance(text, str) and text.strip():
        t = text.strip()
        return t if t.startswith("=") else "=" + t
    return None


def _col_index(letters):
    from openpyxl.utils import column_index_from_string
    return column_index_from_string(letters.upper())


def _parse_ref_text(text):
    m = COORD_RE.match(text)
    if not m:
        raise MalformedFormula("invalid reference %r" % text)
    row = int(m.group(2))
    col = _col_index(m.group(1))
    if row < 1 or row > 1048576 or col > 16384:
        raise MalformedFormula("invalid reference %r" % text)
    return row, col


class Parser(object):
    """Recursive-descent parser producing a tuple-based AST.

    Precedence (lowest to highest): comparison, &, + -, * /, ^, %, unary -, range/primary.
    """

    def __init__(self, text):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self, offset=0):
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else (None, None)

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def accept_op(self, *ops):
        t = self.peek()
        if t[0] == "op" and t[1] in ops:
            self.i += 1
            return t[1]
        return None

    def expect_op(self, op):
        if not self.accept_op(op):
            raise MalformedFormula("expected %r" % op)

    def parse(self):
        if not self.toks:
            raise MalformedFormula("empty formula")
        node = self.comparison()
        if self.i != len(self.toks):
            raise MalformedFormula("unexpected token %r" % (self.peek()[1],))
        return node

    def comparison(self):
        node = self.concat()
        while True:
            op = self.accept_op("=", "<>", "<", ">", "<=", ">=")
            if not op:
                return node
            node = ("cmp", op, node, self.concat())

    def concat(self):
        node = self.additive()
        while self.accept_op("&"):
            node = ("concat", node, self.additive())
        return node

    def additive(self):
        node = self.term()
        while True:
            op = self.accept_op("+", "-")
            if not op:
                return node
            node = ("bin", op, node, self.term())

    def term(self):
        node = self.power()
        while True:
            op = self.accept_op("*", "/")
            if not op:
                return node
            node = ("bin", op, node, self.power())

    def power(self):
        node = self.percent()
        while self.accept_op("^"):
            node = ("bin", "^", node, self.percent())
        return node

    def percent(self):
        node = self.unary()
        while self.accept_op("%"):
            node = ("pct", node)
        return node

    def unary(self):
        op = self.accept_op("-", "+")
        if op:
            operand = self.unary()
            return ("neg", operand) if op == "-" else ("pos", operand)
        return self.primary()

    def reference(self, sheet):
        kind, text = self.next()
        if kind != "ref":
            raise MalformedFormula("expected cell reference")
        r1, c1 = _parse_ref_text(text)
        if self.accept_op(":"):
            # optional repeated sheet prefix on second corner is not supported
            kind2, text2 = self.next()
            if kind2 != "ref":
                raise MalformedFormula("expected cell reference after ':'")
            r2, c2 = _parse_ref_text(text2)
            return ("range", sheet, min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))
        return ("ref", sheet, r1, c1)

    def primary(self):
        kind, text = self.peek()
        if kind is None:
            raise MalformedFormula("unexpected end of formula")
        if kind == "num":
            self.i += 1
            try:
                return ("num", float(text))
            except ValueError:
                raise MalformedFormula("bad number %r" % text)
        if kind == "str":
            self.i += 1
            return ("str", text[1:-1].replace('""', '"'))
        if kind == "op" and text == "(":
            self.i += 1
            node = self.comparison()
            self.expect_op(")")
            return ("paren", node)
        if kind == "qsheet":
            self.i += 1
            return self.reference(text[1:-2].replace("''", "'"))
        if kind == "ref":
            # Sheet names that look like references (e.g. Q1!A1)
            if self.peek(1) == ("op", "!"):
                self.i += 2
                return self.reference(text)
            return self.reference(None)
        if kind == "ident":
            nxt = self.peek(1)
            if nxt == ("op", "("):
                self.i += 2
                return self.function(text)
            if nxt == ("op", "!"):
                self.i += 2
                return self.reference(text)
            self.i += 1
            if text.upper() == "TRUE":
                return ("bool", True)
            if text.upper() == "FALSE":
                return ("bool", False)
            raise MalformedFormula("unknown name %r" % text)
        raise MalformedFormula("unexpected token %r" % text)

    def function(self, name):
        upper = name.upper()
        if upper not in SUPPORTED_FUNCTIONS:
            raise UnsupportedFunction(name)
        args = []
        if self.accept_op(")"):
            return ("func", upper, name, args)
        while True:
            t = self.peek()
            if t[0] == "op" and t[1] in (",", ")"):
                args.append(("empty",))
            else:
                args.append(self.comparison())
            if self.accept_op(")"):
                return ("func", upper, name, args)
            self.expect_op(",")


def parse_formula(formula):
    text = formula.strip()
    if text.startswith("="):
        text = text[1:]
    return Parser(text).parse()


def _scan_unsupported(text):
    """Return the first unsupported function name (original case) in a formula, if any."""
    try:
        toks = tokenize(text, strict=False)
    except Exception:
        return None
    for i, (kind, val) in enumerate(toks):
        if kind == "ident" and i + 1 < len(toks) and toks[i + 1] == ("op", "("):
            if val.upper() not in SUPPORTED_FUNCTIONS:
                return val
    return None


def _num_text(v):
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    s = repr(float(v))
    return s


def _round_half_away(x, places):
    import decimal
    try:
        d = decimal.Decimal(repr(float(x)))
        q = decimal.Decimal(1).scaleb(-int(places))
        return float(d.quantize(q, rounding=decimal.ROUND_HALF_UP))
    except (decimal.InvalidOperation, OverflowError, ValueError):
        return float(x)


class Evaluator(object):
    """Evaluates formulas against a workbook (formula view + optional cached-value view)."""

    def __init__(self, wb_formulas, wb_values=None):
        self.wbf = wb_formulas
        self.wbv = wb_values
        self.cache = {}
        self.stack = []
        self.parsed = {}

    # -- cell access -------------------------------------------------------
    def _sheet(self, name):
        if self.wbf is None or name not in self.wbf.sheetnames:
            return None
        return self.wbf[name]

    def raw_cell(self, sheet, row, col):
        ws = self._sheet(sheet)
        if ws is None:
            return None
        return ws.cell(row=row, column=col).value

    def cell_value(self, sheet, row, col):
        key = (sheet, row, col)
        if key in self.cache:
            return self.cache[key]
        ws = self._sheet(sheet)
        if ws is None:
            if self.wbf is not None and sheet not in self.wbf.sheetnames:
                return REF_ERR
            return None
        raw = ws.cell(row=row, column=col).value
        f = formula_text(raw)
        if f is None:
            val = raw
            if isinstance(val, bool):
                pass
            elif is_number(val):
                val = float(val)
            self.cache[key] = val
            return val
        if key in self.stack:
            raise CircularReference(self._loc(key))
        self.stack.append(key)
        try:
            val = self.eval_formula(f, sheet)
        finally:
            self.stack.pop()
        self.cache[key] = val
        return val

    @staticmethod
    def _loc(key):
        from openpyxl.utils import get_column_letter
        return "%s!%s%d" % (key[0], get_column_letter(key[2]), key[1])

    def eval_formula(self, formula, sheet):
        ast = self.parsed.get(formula)
        if ast is None:
            ast = parse_formula(formula)
            self.parsed[formula] = ast
        return self.scalar(self.ev(ast, sheet))

    def evaluate_with_overrides(self, sheet, row, col, overrides):
        """Evaluate a cell with temporary cell-value overrides; nothing carries over."""
        saved_cache, saved_stack = self.cache, self.stack
        self.cache = dict(overrides)
        self.cache.pop((sheet, row, col), None)
        self.stack = []
        try:
            return self.cell_value(sheet, row, col)
        finally:
            self.cache, self.stack = saved_cache, saved_stack

    def evaluate_cell(self, sheet, row, col):
        """Evaluate the formula at the given cell (top-level); returns its value."""
        key = (sheet, row, col)
        self.cache.pop(key, None)
        return self.cell_value(sheet, row, col)

    # -- evaluation --------------------------------------------------------
    def scalar(self, v):
        if isinstance(v, RangeVal):
            if v.size() == 1:
                return v.values()[0]
            return VALUE_ERR
        return v

    def ev(self, node, sheet):
        t = node[0]
        if t == "num":
            return node[1]
        if t == "str":
            return node[1]
        if t == "bool":
            return node[1]
        if t == "empty":
            return None
        if t == "paren":
            return self.ev(node[1], sheet)
        if t == "ref":
            return self.cell_value(node[1] or sheet, node[2], node[3])
        if t == "range":
            return RangeVal(self, node[1] or sheet, node[2], node[3], node[4], node[5])
        try:
            if t == "neg":
                return -self.to_num(self.ev(node[1], sheet))
            if t == "pos":
                return self.scalar_or_raise(self.ev(node[1], sheet))
            if t == "pct":
                return self.to_num(self.ev(node[1], sheet)) / 100.0
            if t == "bin":
                a = self.to_num(self.ev(node[2], sheet))
                b = self.to_num(self.ev(node[3], sheet))
                return self.arith(node[1], a, b)
            if t == "concat":
                a = self.to_text(self.ev(node[1], sheet))
                b = self.to_text(self.ev(node[2], sheet))
                return a + b
            if t == "cmp":
                a = self.scalar_or_raise(self.ev(node[2], sheet))
                b = self.scalar_or_raise(self.ev(node[3], sheet))
                return 1.0 if self.compare_values(node[1], a, b) else 0.0
            if t == "func":
                return self.call(node[1], node[3], sheet)
        except XLRaise as e:
            return e.err
        raise MalformedFormula("bad node")

    def scalar_or_raise(self, v):
        v = self.scalar(v)
        if isinstance(v, XLError):
            raise XLRaise(v)
        return v

    def arith(self, op, a, b):
        try:
            if op == "+":
                r = a + b
            elif op == "-":
                r = a - b
            elif op == "*":
                r = a * b
            elif op == "/":
                if b == 0:
                    raise XLRaise(DIV0)
                r = a / b
            else:
                if a == 0 and b < 0:
                    raise XLRaise(DIV0)
                if a == 0 and b == 0:
                    raise XLRaise(NUM_ERR)
                r = a ** b
                if isinstance(r, complex):
                    raise XLRaise(NUM_ERR)
        except (OverflowError, ZeroDivisionError, ValueError):
            raise XLRaise(NUM_ERR)
        if r != r or r in (float("inf"), float("-inf")):
            raise XLRaise(NUM_ERR)
        return float(r)

    def to_num(self, v):
        v = self.scalar_or_raise(v)
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if is_number(v):
            return float(v)
        if isinstance(v, str):
            s = v.strip()
            if s == "":
                raise XLRaise(VALUE_ERR)
            try:
                return float(s)
            except ValueError:
                raise XLRaise(VALUE_ERR)
        if isinstance(v, (datetime.datetime, datetime.date)):
            from openpyxl.utils.datetime import to_excel
            return float(to_excel(v))
        raise XLRaise(VALUE_ERR)

    def to_text(self, v):
        v = self.scalar_or_raise(v)
        if v is None:
            return ""
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if is_number(v):
            return _num_text(v)
        return str(v)

    @staticmethod
    def _rank(v):
        if isinstance(v, bool):
            return 2
        if isinstance(v, str):
            return 1
        return 0

    def compare_values(self, op, a, b):
        # Blank cells adopt the type of the other operand.
        if a is None:
            a = "" if isinstance(b, str) else (False if isinstance(b, bool) else 0.0)
        if b is None:
            b = "" if isinstance(a, str) else (False if isinstance(a, bool) else 0.0)
        if isinstance(a, (datetime.datetime, datetime.date)):
            a = self.to_num(a)
        if isinstance(b, (datetime.datetime, datetime.date)):
            b = self.to_num(b)
        ra, rb = self._rank(a), self._rank(b)
        if ra != rb:
            ka, kb = ra, rb
        elif ra == 1:
            ka, kb = a.lower(), b.lower()
        else:
            ka, kb = float(a), float(b)
        if op == "=":
            return ka == kb
        if op == "<>":
            return ka != kb
        if op == "<":
            return ka < kb
        if op == ">":
            return ka > kb
        if op == "<=":
            return ka <= kb
        return ka >= kb

    # -- functions ---------------------------------------------------------
    def _numbers(self, args, sheet):
        """Collect numeric values for SUM-like functions."""
        nums = []
        for a in args:
            if a[0] == "empty":
                continue
            v = self.ev(a, sheet)
            if isinstance(v, RangeVal) or a[0] in ("ref", "range"):
                vals = v.values() if isinstance(v, RangeVal) else [v]
                for x in vals:
                    if isinstance(x, XLError):
                        raise XLRaise(x)
                    if is_number(x):
                        nums.append(float(x))
                    elif isinstance(x, (datetime.datetime, datetime.date)):
                        nums.append(self.to_num(x))
            else:
                nums.append(self.to_num(v))
        return nums

    def _arg(self, args, i, sheet):
        if i >= len(args) or args[i][0] == "empty":
            return None
        return self.ev(args[i], sheet)

    def _nargs(self, args, lo, hi):
        if len(args) < lo or (hi is not None and len(args) > hi):
            raise MalformedFormula("wrong number of arguments")

    def call(self, name, args, sheet):
        if name == "SUM":
            self._nargs(args, 1, None)
            return float(sum(self._numbers(args, sheet)))
        if name == "AVERAGE":
            self._nargs(args, 1, None)
            nums = self._numbers(args, sheet)
            if not nums:
                raise XLRaise(DIV0)
            return sum(nums) / len(nums)
        if name == "MIN":
            self._nargs(args, 1, None)
            nums = self._numbers(args, sheet)
            return min(nums) if nums else 0.0
        if name == "MAX":
            self._nargs(args, 1, None)
            nums = self._numbers(args, sheet)
            return max(nums) if nums else 0.0
        if name == "COUNT":
            self._nargs(args, 1, None)
            count = 0
            for a in args:
                if a[0] == "empty":
                    continue
                v = self.ev(a, sheet)
                vals = v.values() if isinstance(v, RangeVal) else [v]
                direct = not isinstance(v, RangeVal) and a[0] != "ref"
                for x in vals:
                    if is_number(x) or isinstance(x, (datetime.datetime, datetime.date)):
                        count += 1
                    elif direct and isinstance(x, bool):
                        count += 1
                    elif direct and isinstance(x, str):
                        try:
                            float(x.strip())
                            count += 1
                        except ValueError:
                            pass
            return float(count)
        if name == "COUNTIF":
            self._nargs(args, 2, 2)
            if args[0][0] not in ("ref", "range"):
                raise MalformedFormula("COUNTIF requires a range")
            rng = self.ev(args[0], sheet)
            vals = rng.values() if isinstance(rng, RangeVal) else [rng]
            crit = self.scalar_or_raise(self._arg(args, 1, sheet))
            match = self._criteria(crit)
            return float(sum(1 for x in vals if match(x)))
        if name == "ABS":
            self._nargs(args, 1, 1)
            return abs(self.to_num(self._arg(args, 0, sheet)))
        if name == "ROUND":
            self._nargs(args, 2, 2)
            x = self.to_num(self._arg(args, 0, sheet))
            p = self.to_num(self._arg(args, 1, sheet))
            p = int(p) if p >= 0 else -int(-p)
            return _round_half_away(x, p)
        if name == "INT":
            self._nargs(args, 1, 1)
            import math
            return float(math.floor(self.to_num(self._arg(args, 0, sheet))))
        if name == "MOD":
            self._nargs(args, 2, 2)
            import math
            n = self.to_num(self._arg(args, 0, sheet))
            d = self.to_num(self._arg(args, 1, sheet))
            if d == 0:
                raise XLRaise(DIV0)
            return n - d * math.floor(n / d)
        if name == "IF":
            self._nargs(args, 2, 3)
            cond = self.scalar_or_raise(self._arg(args, 0, sheet))
            if isinstance(cond, str):
                if cond.upper() == "TRUE":
                    truth = True
                elif cond.upper() == "FALSE":
                    truth = False
                else:
                    raise XLRaise(VALUE_ERR)
            else:
                truth = self.to_num(cond) != 0
            if truth:
                v = self._arg(args, 1, sheet)
                return 0.0 if v is None else self.scalar(v)
            if len(args) < 3:
                return False
            v = self._arg(args, 2, sheet)
            return 0.0 if v is None else self.scalar(v)
        raise UnsupportedFunction(name)

    def _criteria(self, crit):
        op = "="
        val = crit
        if isinstance(crit, str):
            m = re.match(r"^(<>|<=|>=|=|<|>)(.*)$", crit, re.S)
            if m:
                op, text = m.group(1), m.group(2)
            else:
                text = crit
            val = text
            try:
                val = float(text.strip()) if text.strip() != "" else text
            except ValueError:
                u = text.upper()
                val = True if u == "TRUE" else (False if u == "FALSE" else text)
        elif crit is None:
            val = 0.0
        elif is_number(crit):
            val = float(crit)

        def to_cmp(x):
            if isinstance(x, (datetime.datetime, datetime.date)):
                return self.to_num(x)
            if is_number(x):
                return float(x)
            return x

        def pattern(s):
            out = []
            i = 0
            while i < len(s):
                ch = s[i]
                if ch == "~" and i + 1 < len(s) and s[i + 1] in "*?~":
                    out.append(re.escape(s[i + 1]))
                    i += 2
                    continue
                if ch == "*":
                    out.append(".*")
                elif ch == "?":
                    out.append(".")
                else:
                    out.append(re.escape(ch))
                i += 1
            return re.compile("^" + "".join(out) + "$", re.I | re.S)

        if isinstance(val, str):
            if val == "":
                if op == "=":
                    return lambda x: x is None or x == ""
                if op == "<>":
                    return lambda x: not (x is None or x == "")
            rx = pattern(val)
            if op == "=":
                return lambda x: isinstance(x, str) and rx.match(x) is not None
            if op == "<>":
                return lambda x: not (isinstance(x, str) and rx.match(x) is not None)
            low = val.lower()

            def str_cmp(x):
                if not isinstance(x, str):
                    return False
                a = x.lower()
                return {"<": a < low, ">": a > low, "<=": a <= low, ">=": a >= low}[op]
            return str_cmp
        if isinstance(val, bool):
            if op == "=":
                return lambda x: isinstance(x, bool) and x == val
            if op == "<>":
                return lambda x: not (isinstance(x, bool) and x == val)
            return lambda x: False
        target = float(val)

        def num_cmp(x):
            x = to_cmp(x)
            if isinstance(x, bool) or not isinstance(x, float):
                if op == "<>":
                    return True
                if op == "=" and isinstance(x, str):
                    try:
                        return float(x.strip()) == target
                    except ValueError:
                        return False
                return False
            return {"=": x == target, "<>": x != target, "<": x < target, ">": x > target,
                    "<=": x <= target, ">=": x >= target}[op]
        return num_cmp


class RangeVal(object):
    def __init__(self, ev, sheet, r1, c1, r2, c2):
        self.ev = ev
        self.sheet = sheet
        self.r1, self.c1, self.r2, self.c2 = r1, c1, r2, c2
        self._vals = None

    def size(self):
        return (self.r2 - self.r1 + 1) * (self.c2 - self.c1 + 1)

    def values(self):
        if self._vals is None:
            vals = []
            for r in range(self.r1, self.r2 + 1):
                for c in range(self.c1, self.c2 + 1):
                    vals.append(self.ev.cell_value(self.sheet, r, c))
            self._vals = vals
        return self._vals


class Output:
    def __init__(self, detailed):
        self.detailed = detailed
        self.lines = []

    def line(self, text):
        self.lines.append(text)
        if self.detailed:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


def get_cell(ws, row, col):
    if ws is None:
        return None
    return ws.cell(row=row, column=col)


RULE_KINDS = ("literal", "expression", "computed", "reference", "reference_expr",
              "result", "boolean", "scenario")


class RuleContext(object):
    def __init__(self, sheet, coord, row, col, where, tol, alts, rep,
                 key_ws, sub_ws, key_wbf, sub_wbf, key_eval, sub_eval):
        self.sheet, self.coord, self.row, self.col = sheet, coord, row, col
        self.where, self.tol, self.alts, self.rep = where, tol, alts, rep
        self.key_ws, self.sub_ws = key_ws, sub_ws
        self.key_wbf, self.sub_wbf = key_wbf, sub_wbf
        self.key_eval, self.sub_eval = key_eval, sub_eval
        self.expected = None
        self.actual = None

    def alt_cells(self):
        for alt in self.alts:
            try:
                _, ar, ac = parse_coord(alt)
            except ValueError:
                self.rep.warn("%s: alternate cell reference %r could not be found or "
                              "resolved; alternate skipped" % (self.where, alt))
                continue
            yield alt, ar, ac

    def key_formula(self, row, col):
        if self.key_ws is None or self.sheet not in self.key_wbf.sheetnames:
            return None
        return formula_text(self.key_wbf[self.sheet].cell(row=row, column=col).value)

    def sub_formula(self):
        if self.sub_ws is None or self.sheet not in self.sub_wbf.sheetnames:
            return None
        return formula_text(self.sub_wbf[self.sheet].cell(row=self.row, column=self.col).value)


def grade_literal(ctx):
    key_val = ctx.key_ws.cell(row=ctx.row, column=ctx.col).value
    sub_val = (ctx.sub_ws.cell(row=ctx.row, column=ctx.col).value
               if ctx.sub_ws is not None else None)
    ctx.expected, ctx.actual = key_val, sub_val
    if compare(key_val, sub_val, ctx.tol):
        return True
    for _, ar, ac in ctx.alt_cells():
        if compare(ctx.key_ws.cell(row=ar, column=ac).value, sub_val, ctx.tol):
            return True
    return False


def grade_expression(ctx):
    expected = ctx.key_formula(ctx.row, ctx.col)
    if expected is None:
        ctx.rep.warn("%s: answer key cell has no formula for 'expression' rule; rule scored 0"
                     % ctx.where)
        return None
    actual = ctx.sub_formula()
    ctx.expected, ctx.actual = expected, actual
    if actual is None:
        return False
    norm = normalize_formula(actual)
    if normalize_formula(expected) == norm:
        return True
    for alt, ar, ac in ctx.alt_cells():
        f = ctx.key_formula(ar, ac)
        if f is None:
            ctx.rep.warn("%s: alternate cell %s has no formula; alternate skipped"
                         % (ctx.where, alt))
            continue
        if normalize_formula(f) == norm:
            return True
    return False


def _formula_failure(ctx, exc, which):
    if isinstance(exc, UnsupportedFunction):
        ctx.rep.warn("%s: unsupported function '%s' in %s formula at cell %s; rule scored 0"
                     % (ctx.where, exc.name, which, ctx.coord))
    elif isinstance(exc, CircularReference):
        ctx.rep.warn("%s: circular reference detected in %s formula (via %s); rule scored 0"
                     % (ctx.where, which, exc.cell))
    else:
        ctx.rep.warn("%s: malformed %s formula (%s); rule scored 0" % (ctx.where, which, exc))


def _expected_value(ctx, row, col):
    """Value of an answer-key cell: cached value, or evaluated formula when no cache exists."""
    cached = ctx.key_ws.cell(row=row, column=col).value
    if cached is not None:
        return cached
    if ctx.key_formula(row, col) is None:
        return cached
    return ctx.key_eval.evaluate_cell(ctx.sheet, row, col)


def _compare_computed(expected, actual, tol):
    if isinstance(expected, XLError) or isinstance(actual, XLError):
        return False
    if isinstance(expected, bool) and is_number(actual) and not isinstance(actual, bool):
        expected = 1.0 if expected else 0.0
    if is_number(expected) and isinstance(actual, bool):
        actual = 1.0 if actual else 0.0
    if is_number(expected) and is_number(actual):
        try:
            if tol is not None:
                return abs(float(expected) - float(actual)) <= tol
            return float(expected) == float(actual)
        except (OverflowError, ValueError):
            return False
    if actual is None and isinstance(expected, str):
        actual = ""
    if isinstance(expected, (datetime.datetime, datetime.date)) and is_number(actual):
        from openpyxl.utils.datetime import to_excel
        try:
            return compare(float(to_excel(expected)), actual, tol)
        except Exception:
            return False
    return values_equal(expected, actual)


def grade_computed(ctx):
    formula = ctx.sub_formula()
    if formula is None:
        return False
    name = _scan_unsupported(formula[1:])
    if name is not None:
        _formula_failure(ctx, UnsupportedFunction(name), "submission")
        return None
    try:
        actual = ctx.sub_eval.evaluate_cell(ctx.sheet, ctx.row, ctx.col)
    except FormulaError as e:
        _formula_failure(ctx, e, "submission")
        return None
    except RecursionError:
        _formula_failure(ctx, MalformedFormula("formula nesting too deep"), "submission")
        return None
    try:
        expected = _expected_value(ctx, ctx.row, ctx.col)
    except (FormulaError, RecursionError) as e:
        _formula_failure(ctx, e, "answer key")
        return None
    ctx.expected, ctx.actual = expected, actual
    if _compare_computed(expected, actual, ctx.tol):
        return True
    for alt, ar, ac in ctx.alt_cells():
        try:
            alt_val = _expected_value(ctx, ar, ac)
        except (FormulaError, RecursionError) as e:
            ctx.rep.warn("%s: alternate cell %s could not be evaluated (%s); alternate skipped"
                         % (ctx.where, alt, e))
            continue
        if _compare_computed(alt_val, actual, ctx.tol):
            return True
    return False


def _sub_cell_value(ctx):
    """Submission graded-cell value: cached display value, or evaluated formula if none."""
    cached = (ctx.sub_ws.cell(row=ctx.row, column=ctx.col).value
              if ctx.sub_ws is not None else None)
    if cached is not None or ctx.sub_formula() is None:
        return cached
    return _eval_sub_cell(ctx)


def _eval_sub_cell(ctx):
    formula = ctx.sub_formula()
    name = _scan_unsupported(formula[1:])
    if name is not None:
        raise UnsupportedFunction(name)
    try:
        return ctx.sub_eval.evaluate_cell(ctx.sheet, ctx.row, ctx.col)
    except RecursionError:
        raise MalformedFormula("formula nesting too deep")


def _eval_key_formula_on_sub(ctx, formula):
    """Evaluate an answer-key formula with references resolved against the submission."""
    name = _scan_unsupported(formula[1:])
    if name is not None:
        raise UnsupportedFunction(name)
    try:
        return ctx.sub_eval.eval_formula(formula, ctx.sheet)
    except RecursionError:
        raise MalformedFormula("formula nesting too deep")


def _grade_reference(ctx, kind, require_sub_formula):
    expected_f = ctx.key_formula(ctx.row, ctx.col)
    if expected_f is None:
        ctx.rep.warn("%s: answer key cell has no formula for '%s' rule; rule scored 0"
                     % (ctx.where, kind))
        return None
    has_sub_formula = ctx.sub_formula() is not None
    if require_sub_formula and not has_sub_formula:
        return False
    try:
        if has_sub_formula:
            actual = _eval_sub_cell(ctx)
        else:
            actual = (ctx.sub_ws.cell(row=ctx.row, column=ctx.col).value
                      if ctx.sub_ws is not None else None)
    except FormulaError as e:
        _formula_failure(ctx, e, "submission")
        return None
    try:
        expected = _eval_key_formula_on_sub(ctx, expected_f)
    except FormulaError as e:
        _formula_failure(ctx, e, "answer key")
        return None
    ctx.expected, ctx.actual = expected, actual
    if _compare_computed(expected, actual, ctx.tol):
        return True
    for alt, ar, ac in ctx.alt_cells():
        f = ctx.key_formula(ar, ac)
        if f is None:
            ctx.rep.warn("%s: alternate cell %s has no formula; alternate skipped"
                         % (ctx.where, alt))
            continue
        try:
            alt_val = _eval_key_formula_on_sub(ctx, f)
        except FormulaError as e:
            ctx.rep.warn("%s: alternate cell %s formula could not be evaluated (%s); "
                         "alternate skipped" % (ctx.where, alt, e))
            continue
        if _compare_computed(alt_val, actual, ctx.tol):
            return True
    return False


def grade_reference(ctx):
    return _grade_reference(ctx, "reference", False)


def grade_reference_expr(ctx):
    return _grade_reference(ctx, "reference_expr", True)


def is_truthy(v):
    if isinstance(v, bool):
        return v
    if is_number(v):
        return v != 0
    if isinstance(v, str):
        return v.lower() in ("true", "yes")
    return False


def grade_boolean(ctx):
    try:
        v = _sub_cell_value(ctx)
    except FormulaError as e:
        _formula_failure(ctx, e, "submission")
        return None
    ctx.expected, ctx.actual = True, v
    return is_truthy(v)


def _key_cell_exists(ctx, row, col):
    if ctx.key_ws is None:
        return False
    return (row, col) in getattr(ctx.key_ws, "_cells", {})


def _result_compare(expected, actual, tol):
    if isinstance(expected, XLError) or isinstance(actual, XLError):
        return False
    return compare(expected, actual, tol)


def grade_result(ctx, target):
    try:
        tcoord, tr, tc = parse_coord(target)
    except ValueError:
        ctx.rep.warn("%s: rule.target %r is not a valid cell in the answer key; rule scored 0"
                     % (ctx.where, target))
        return None
    if not _key_cell_exists(ctx, tr, tc):
        ctx.rep.warn("%s: rule.target %s does not exist in the answer key; rule scored 0"
                     % (ctx.where, tcoord))
        return None
    try:
        actual = _sub_cell_value(ctx)
    except FormulaError as e:
        _formula_failure(ctx, e, "submission")
        return None
    try:
        expected = _expected_value(ctx, tr, tc)
    except (FormulaError, RecursionError) as e:
        _formula_failure(ctx, e, "answer key")
        return None
    ctx.expected, ctx.actual = expected, actual
    if _result_compare(expected, actual, ctx.tol):
        return True
    for alt, ar, ac in ctx.alt_cells():
        if not _key_cell_exists(ctx, ar, ac):
            ctx.rep.warn("%s: alternate cell %s does not exist in the answer key; "
                         "alternate skipped" % (ctx.where, alt))
            continue
        try:
            alt_val = _expected_value(ctx, ar, ac)
        except (FormulaError, RecursionError) as e:
            ctx.rep.warn("%s: alternate cell %s could not be evaluated (%s); alternate skipped"
                         % (ctx.where, alt, e))
            continue
        if _result_compare(alt_val, actual, ctx.tol):
            return True
    return False


def _parse_sheet_ref(text, default_sheet):
    """Parse 'A1', 'Sheet!A1' or "'My Sheet'!A1" into (sheet, coord, row, col)."""
    text = str(text).strip()
    sheet = default_sheet
    ref = text
    if "!" in text:
        sh, _, ref = text.rpartition("!")
        sh = sh.strip()
        if len(sh) >= 2 and sh[0] == "'" and sh[-1] == "'":
            sh = sh[1:-1].replace("''", "'")
        sheet = sh
    coord, row, col = parse_coord(ref)
    return sheet, coord, row, col


def parse_scenarios(data, rule, where, rep):
    """Return the list of valid scenario cases: (name, input, output, tol, fail_template)."""
    raw = data.get("scenarios") if isinstance(data, dict) else None
    if raw is None and isinstance(rule, dict):
        raw = rule.get("scenarios")
    if raw is None:
        rep.warn("%s: 'scenario' rule has no scenarios; rule scored 0" % where)
        return []
    if isinstance(raw, (list, tuple)):
        items = []
        for i, case in enumerate(raw):
            if isinstance(case, dict) and "name" in case:
                items.append((str(case["name"]), case))
            else:
                items.append(("case%d" % (i + 1), case))
    elif isinstance(raw, dict):
        items = [(str(k), v) for k, v in raw.items()]
    else:
        rep.warn("%s: 'scenarios' is not a mapping of cases; rule scored 0" % where)
        return []
    cases = []
    for name, case in items:
        if not isinstance(case, dict):
            rep.warn("%s: scenario case '%s' is not a mapping; case skipped" % (where, name))
            continue
        missing = [k for k in ("input", "output") if k not in case]
        if missing:
            rep.warn("%s: scenario case '%s' is missing %s; case skipped"
                     % (where, name, " and ".join("'%s'" % m for m in missing)))
            continue
        inp = case["input"]
        if inp is None:
            inp = {}
        if not isinstance(inp, dict):
            rep.warn("%s: scenario case '%s' input is not a mapping; case skipped"
                     % (where, name))
            continue
        tol = None
        if case.get("tolerance") is not None:
            t = case["tolerance"]
            if not is_number(t) or t != t or t < 0:
                rep.warn("%s: scenario case '%s' has invalid tolerance %r; using rule tolerance"
                         % (where, name, t))
                tol = "rule"
            else:
                tol = t
        else:
            tol = "rule"
        ft = case.get("fail-template")
        if ft is not None and not isinstance(ft, str):
            ft = str(ft)
        cases.append((name, inp, case["output"], tol, ft))
    return cases


def _override_value(v):
    if isinstance(v, bool):
        return v
    if is_number(v):
        return float(v)
    return v


def grade_scenario(ctx, cases):
    """Return (rule_ok, case_results) where case_results is a list of
    (name, ok, expected, actual, fail_template). rule_ok is None on scoring failure."""
    results = []
    if ctx.key_formula(ctx.row, ctx.col) is None:
        ctx.rep.warn("%s: answer key cell has no formula for 'scenario' rule; rule scored 0"
                     % ctx.where)
        for name, _, output, _, ft in cases:
            results.append((name, False, output, None, ft))
        return None, results
    formula = ctx.sub_formula()
    unsupported = _scan_unsupported(formula[1:]) if formula is not None else None
    if unsupported is not None:
        _formula_failure(ctx, UnsupportedFunction(unsupported), "submission")
    for name, inp, output, tol, ft in cases:
        if tol == "rule":
            tol = ctx.tol
        if formula is None:
            actual = (ctx.sub_ws.cell(row=ctx.row, column=ctx.col).value
                      if ctx.sub_ws is not None else None)
            results.append((name, False, output, actual, ft))
            continue
        if unsupported is not None:
            results.append((name, False, output, None, ft))
            continue
        overrides = {}
        for ref, val in inp.items():
            try:
                sh, _, r, c = _parse_sheet_ref(ref, ctx.sheet)
            except ValueError:
                ctx.rep.warn("%s: scenario case '%s' input %r is not a valid cell reference; "
                             "input ignored" % (ctx.where, name, ref))
                continue
            overrides[(sh, r, c)] = _override_value(val)
        try:
            actual = ctx.sub_eval.evaluate_with_overrides(ctx.sheet, ctx.row, ctx.col,
                                                          overrides)
        except RecursionError:
            ctx.rep.warn("%s: scenario case '%s': formula nesting too deep; case failed"
                         % (ctx.where, name))
            results.append((name, False, output, None, ft))
            continue
        except FormulaError as e:
            _formula_failure(ctx, e, "submission")
            results.append((name, False, output, None, ft))
            continue
        ok = _compare_computed(_override_value(output), actual, tol)
        results.append((name, ok, output, actual, ft))
    if not results:
        return None, results
    return all(r[1] for r in results), results


def render_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if is_number(v):
        try:
            return fmt_num(v)
        except (OverflowError, ValueError):
            return str(v)
    if isinstance(v, XLError):
        return v.code
    return str(v)


_PLACEHOLDER_RE = re.compile(
    r"\$(?:(?P<kw>expected|actual)(?![A-Za-z0-9_])"
    r"|(?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z0-9_.]+)!)?\$?[A-Za-z]{1,3}\$?[0-9]+)"
    r"(?![A-Za-z0-9_]))")


def render_template(template, expected, actual, ctx, rep):
    """Single-pass placeholder substitution for fail templates."""
    def sub(m):
        kw = m.group("kw")
        if kw == "expected":
            return render_value(expected)
        if kw == "actual":
            return render_value(actual)
        ref = m.group("ref")
        try:
            sh, coord, r, c = _parse_sheet_ref(ref, ctx.sheet)
        except ValueError:
            rep.warn("%s: fail-template placeholder $%s does not reference a cell in the "
                     "submission; substituted with empty string" % (ctx.where, ref))
            return ""
        sub_wb = ctx.sub_wbf
        if sub_wb is None or sh not in sub_wb.sheetnames:
            rep.warn("%s: fail-template placeholder $%s references missing submission sheet "
                     "'%s'; substituted with empty string" % (ctx.where, ref, sh))
            return ""
        ws = sub_wb[sh]
        if (r, c) not in getattr(ws, "_cells", {}):
            rep.warn("%s: fail-template placeholder $%s references a cell that does not exist "
                     "in the submission; substituted with empty string" % (ctx.where, ref))
            return ""
        val = None
        vwb = ctx.sub_eval.wbv
        if vwb is not None and sh in vwb.sheetnames:
            val = vwb[sh].cell(row=r, column=c).value
        if val is None and formula_text(ws.cell(row=r, column=c).value) is not None:
            try:
                val = ctx.sub_eval.cell_value(sh, r, c)
            except (FormulaError, RecursionError):
                val = None
        elif val is None:
            val = ws.cell(row=r, column=c).value
        return render_value(val)
    return _PLACEHOLDER_RE.sub(sub, template)


def parse_expected_outcome(raw, where, rep):
    if raw is None:
        return "pass"
    text = str(raw).strip().lower()
    if not text:
        return "pass"
    if text in ("pass", "fail"):
        return text
    rep.warn("%s: unrecognized expected-outcome %r; defaulting to 'pass'" % (where, raw))
    return "pass"


def parse_expected_points(raw, where, rep):
    if raw is None:
        return None
    if isinstance(raw, bool):
        rep.warn("%s: non-numeric expected-points %r; ignored" % (where, raw))
        return None
    if is_number(raw):
        if raw != raw:
            rep.warn("%s: non-numeric expected-points %r; ignored" % (where, raw))
            return None
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    if NUMERIC_TEXT_RE.match(text):
        return float(text)
    rep.warn("%s: non-numeric expected-points %r; ignored" % (where, text))
    return None


def cell_text(raw):
    if raw is None:
        return None
    if isinstance(raw, float) and raw == int(raw):
        raw = int(raw)
    text = str(raw).strip()
    return text or None


DEFAULT_THRESHOLD_MESSAGE = "Minimum score not met."
NUMERIC_TEXT_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def coord_key(coord):
    return coord.replace("$", "").strip().upper()


def parse_flags(raw, where, rep):
    """Return (fatal, concealed, manual) for a checklist flags value."""
    if raw is None:
        return False, False, False
    if isinstance(raw, float) and raw == int(raw):
        raw = int(raw)
    text = str(raw).strip()
    if not text:
        return False, False, False
    if text.lower() == "manual":
        return False, False, True
    fatal = concealed = manual = False
    bad = []
    for ch in text:
        u = ch.upper()
        if u == "F":
            fatal = True
        elif u == "C":
            concealed = True
        elif u == "M":
            manual = True
        elif ch.isspace():
            continue
        else:
            bad.append(ch)
    if bad:
        rep.warn("%s: unrecognized flag(s) %r in flags value %r; ignored"
                 % (where, "".join(bad), text))
    return fatal, concealed, manual


def parse_min_score(raw, sheet, rep):
    if raw is None:
        return None
    if is_number(raw):
        if raw != raw:
            rep.warn("sheet '%s': non-numeric min-score %r; threshold ignored" % (sheet, raw))
            return None
        return raw
    text = str(raw).strip()
    if not text:
        return None
    if NUMERIC_TEXT_RE.match(text):
        return float(text)
    rep.warn("sheet '%s': non-numeric min-score %r; threshold ignored" % (sheet, text))
    return None


def check_depends(rule, sheet, where, outcomes, checklist_cells, rep):
    """Return the first unmet dependency cell (as written), or None if all are met."""
    if "depends" not in rule or rule["depends"] is None:
        return None
    deps = rule["depends"]
    if not isinstance(deps, (list, tuple)):
        deps = [deps]
    for dep in deps:
        text = str(dep).strip() if dep is not None else ""
        ref = text
        if "!" in text:
            sh, _, ref = text.rpartition("!")
            sh = sh.strip()
            if len(sh) >= 2 and sh[0] == "'" and sh[-1] == "'":
                sh = sh[1:-1].replace("''", "'")
            if sh != sheet:
                rep.warn("%s: rule.depends references %r, which is not on sheet '%s'; "
                         "dependency treated as unmet" % (where, text, sheet))
                return text
        try:
            coord, _, _ = parse_coord(ref)
        except ValueError:
            rep.warn("%s: rule.depends references %r, which is not a cell on sheet '%s'; "
                     "dependency treated as unmet" % (where, text, sheet))
            return text
        key = coord_key(coord)
        if key not in checklist_cells:
            rep.warn("%s: rule.depends references %s, which has no rule on sheet '%s'; "
                     "dependency treated as unmet" % (where, coord, sheet))
            return key
        if outcomes.get(key) != "CORRECT":
            return key
    return None


def _esc(text):
    import html as _html
    return _html.escape("" if text is None else str(text), quote=True)


class HtmlReport(object):
    """Collects structured grading results and renders a standalone HTML5 report."""

    def __init__(self, check_mode=False):
        self.check_mode = check_mode
        self.sheets = []
        self.total = None

    def start_sheet(self, name):
        sec = {"name": name, "rows": [], "threshold": None, "subtotal": ""}
        self.sheets.append(sec)
        return sec

    @staticmethod
    def _cell_html(coord, concealed):
        if concealed:
            return ('[concealed]<span class="hint" style="display:none">%s</span>'
                    % _esc(coord))
        return _esc(coord)

    def render(self):
        if self.check_mode:
            headers = ("Rule ID", "Cell", "Check Name", "Result", "Fail Message")
        else:
            headers = ("Rule ID", "Cell", "Outcome", "Earned", "Possible")
        out = []
        out.append("<!DOCTYPE html>")
        out.append('<html lang="en">')
        out.append("<head>")
        out.append('<meta charset="utf-8">')
        out.append("<title>sheeteval report</title>")
        out.append("</head>")
        out.append("<body>")
        out.append("<h1>sheeteval report</h1>")
        for sec in self.sheets:
            out.append('<section class="sheet">')
            out.append("<h2>%s</h2>" % _esc(sec["name"]))
            if sec["threshold"] is not None:
                out.append('<p class="threshold">%s</p>' % _esc(sec["threshold"]))
            else:
                out.append("<table>")
                out.append("<tr>%s</tr>" % "".join("<th>%s</th>" % h for h in headers))
                for row in sec["rows"]:
                    if row[0] == "feedback":
                        out.append('<tr class="feedback"><td colspan="5">%s</td></tr>'
                                   % _esc(row[1]))
                    elif row[0] == "rule":
                        _, rid, coord, concealed, outcome, earned, possible = row
                        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                                   "<td>%s</td></tr>"
                                   % (_esc(rid), self._cell_html(coord, concealed),
                                      _esc(outcome), _esc(fmt_num(earned)),
                                      _esc(fmt_num(possible))))
                    else:
                        _, rid, coord, concealed, name, passed, msg = row
                        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                                   "<td>%s</td></tr>"
                                   % (_esc(rid), self._cell_html(coord, concealed),
                                      _esc(name), "PASS" if passed else "FAIL",
                                      _esc(msg) if (not passed and msg is not None) else ""))
                out.append("</table>")
            out.append('<p class="subtotal">%s</p>' % _esc(sec["subtotal"]))
            out.append("</section>")
        out.append('<p class="total">%s</p>' % _esc(self.total or ""))
        out.append("</body>")
        out.append("</html>")
        return "\n".join(out) + "\n"


class SheetBuffer(object):
    """Collects a sheet's per-rule report lines, optionally deferring them."""

    def __init__(self, out, defer):
        self.out, self.defer = out, defer
        self.pending = []

    def header(self, text):
        if self.defer:
            self.pending.append(text)
        else:
            self.out.line(text)

    def line(self, text):
        self.header(text)

    def replace_rules(self, text):
        # Keep the header (first pending line), drop rule lines.
        self.pending = self.pending[:1] + [text]

    def flush(self):
        for ln in self.pending:
            self.out.line(ln)
        self.pending = []


def grade(key_path, sub_path, rep, out, check_mode=False, cout=None, html=None):
    import openpyxl

    for p in (key_path, sub_path):
        if not os.path.isfile(p):
            raise FatalError("file not found: %s" % p)
    key_wb = openpyxl.load_workbook(key_path, data_only=True)
    sub_wb = openpyxl.load_workbook(sub_path, data_only=True)
    key_wbf = openpyxl.load_workbook(key_path, data_only=False)
    sub_wbf = openpyxl.load_workbook(sub_path, data_only=False)
    key_eval = Evaluator(key_wbf, key_wb)
    sub_eval = Evaluator(sub_wbf, sub_wb)

    if "EvalManifest" not in key_wb.sheetnames:
        raise FatalError("answer key has no 'EvalManifest' worksheet")
    manifest = key_wb["EvalManifest"]
    name_col = find_header_col(manifest, MANIFEST_HEADERS)
    if name_col is None:
        raise FatalError("EvalManifest worksheet has no name/sheet/tab column")

    min_col = find_exact_col(manifest, "min-score")
    msg_col = find_exact_col(manifest, "threshold-message")

    sheets = []
    for row in range(2, manifest.max_row + 1):
        v = manifest.cell(row=row, column=name_col).value
        if v is None:
            continue
        if isinstance(v, float) and v == int(v):
            v = int(v)
        s = str(v)
        if not s.strip():
            continue
        min_score = None
        if min_col is not None:
            min_score = parse_min_score(manifest.cell(row=row, column=min_col).value, s, rep)
        message = None
        if msg_col is not None:
            mv = manifest.cell(row=row, column=msg_col).value
            if mv is not None:
                if isinstance(mv, float) and mv == int(mv):
                    mv = int(mv)
                mv = str(mv).strip()
                if mv:
                    message = mv
        if message is None:
            message = DEFAULT_THRESHOLD_MESSAGE
        sheets.append((s, min_score, message))

    total_earned = 0
    total_max = 0
    total_passed = 0
    total_checks = 0
    for sheet, min_score, threshold_message in sheets:
        cl_name = "%s_Checklist" % sheet
        if cl_name not in key_wb.sheetnames:
            rep.warn("missing checklist sheet '%s'; skipping sheet '%s'" % (cl_name, sheet))
            continue
        cl = key_wb[cl_name]
        id_col = find_header_col(cl, CHECKLIST_HEADERS)
        if id_col is None:
            rep.warn("checklist sheet '%s' has no cell reference column; skipping sheet '%s'"
                     % (cl_name, sheet))
            continue
        flags_col = find_exact_col(cl, "flags")
        name_col_cl = find_exact_col(cl, "check-name")
        eo_col = find_exact_col(cl, "expected-outcome")
        fm_col = find_exact_col(cl, "fail-message")
        ep_col = find_exact_col(cl, "expected-points")
        key_ws = key_wb[sheet] if sheet in key_wb.sheetnames else None
        sub_ws = sub_wb[sheet] if sub_wb is not None and sheet in sub_wb.sheetnames else None

        # Per-rule lines are deferred when a threshold is configured.
        sheet_out = SheetBuffer(out, defer=min_score is not None)
        sheet_out.header("Sheet '%s':" % sheet)
        hsec = html.start_sheet(sheet) if html is not None else None
        nsec = hsec if not check_mode else None  # normal-mode HTML rows
        sheet_earned = 0
        sheet_max = 0
        check_lines = []
        sheet_passed = 0
        sheet_checks = 0

        def cval(row, col):
            return cl.cell(row=row, column=col).value if col else None

        # Collect checklist rows first (needed to validate dependencies).
        rows = []
        for row in range(2, cl.max_row + 1):
            raw = cl.cell(row=row, column=id_col).value
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            coord, r, c = parse_coord(raw)
            meta = {
                "flags": cval(row, flags_col),
                "check-name": cell_text(cval(row, name_col_cl)),
                "expected-outcome": cval(row, eo_col),
                "fail-message": cell_text(cval(row, fm_col)),
                "expected-points": cval(row, ep_col),
            }
            rows.append((coord, r, c, meta))
        checklist_cells = set(coord_key(coord) for coord, _, _, _ in rows)

        outcomes = {}  # normalized coord -> outcome of an earlier rule
        halted = False
        for coord, r, c, meta in rows:
            where = "%s.%s" % (sheet, coord)
            fatal, concealed, manual = parse_flags(meta["flags"], where, rep)
            if manual:
                continue
            parsed = parse_annotation(get_cell(key_ws, r, c))
            if parsed is None:
                rep.warn("%s.%s: missing or unparseable rule annotation; rule skipped"
                         % (sheet, coord))
                continue
            kind, points, rule, data = parsed
            if kind not in RULE_KINDS:
                rep.warn("%s.%s: unknown rule kind '%s'; rule skipped" % (sheet, coord, kind))
                continue
            if kind == "result" and rule.get("target") is None:
                rep.warn("%s.%s: 'result' rule has no rule.target; rule skipped" % (sheet, coord))
                continue
            label = "[concealed]" if concealed else coord
            key = coord_key(coord)
            fail_template = data.get("fail-template")
            if fail_template is not None and not isinstance(fail_template, str):
                fail_template = str(fail_template)
            cases = parse_scenarios(data, rule, where, rep) if kind == "scenario" else None

            if check_mode:
                check_name = meta["check-name"] or "%s.%s" % (sheet, coord)
                exp_outcome = parse_expected_outcome(meta["expected-outcome"], where, rep)
                exp_points = parse_expected_points(meta["expected-points"], where, rep)
            tol = None
            mode = "award"
            ok = None
            ctx = None
            case_results = None

            if halted:
                possible = abs(points)
                sheet_max += possible
                outcome = "SKIPPED"
                earned = 0
                outcomes[key] = outcome
                sheet_out.line("  [SKIPPED] %s %s: 0 / %s" % (coord, label, fmt_num(possible)))
                if nsec is not None:
                    nsec["rows"].append(("rule", coord, coord, concealed, outcome, 0, possible))
                rep.log("%s.%s: %s SKIPPED" % (sheet, coord, kind))
            else:
                unmet = check_depends(rule, sheet, where, outcomes, checklist_cells, rep)
                if unmet is not None:
                    possible = abs(points)
                    sheet_max += possible
                    outcome = "PREREQ"
                    earned = 0
                    outcomes[key] = outcome
                    sheet_out.line("  [PREREQ] %s %s: 0 / %s (requires %s)"
                                   % (coord, label, fmt_num(possible), unmet))
                    if nsec is not None:
                        nsec["rows"].append(("rule", coord, coord, concealed, outcome, 0,
                                             possible))
                    rep.log("%s.%s: %s PREREQ (requires %s)" % (sheet, coord, kind, unmet))
                else:
                    tol = get_tolerance(rule, where, rep)
                    mode = get_mode(rule, where, rep)
                    alts = []
                    if "alternates" in data and data["alternates"] is not None:
                        alts = data["alternates"]
                        if not isinstance(alts, (list, tuple)):
                            alts = [alts]
                    ctx = RuleContext(sheet, coord, r, c, where, tol, alts, rep,
                                      key_ws, sub_ws, key_wbf, sub_wbf, key_eval, sub_eval)
                    if kind == "literal":
                        ok = grade_literal(ctx)
                    elif kind == "expression":
                        ok = grade_expression(ctx)
                    elif kind == "computed":
                        ok = grade_computed(ctx)
                    elif kind == "reference":
                        ok = grade_reference(ctx)
                    elif kind == "reference_expr":
                        ok = grade_reference_expr(ctx)
                    elif kind == "result":
                        ok = grade_result(ctx, rule.get("target"))
                    elif kind == "scenario":
                        ok, case_results = grade_scenario(ctx, cases)
                        for cr in case_results:
                            if not cr[1]:
                                ctx.expected, ctx.actual = cr[2], cr[3]
                                break
                    else:
                        ok = grade_boolean(ctx)
                    if ok is None:
                        # Formula-evaluation failure: scores 0 regardless of mode.
                        if mode == "penalty":
                            possible = abs(points)
                        else:
                            possible = points
                            sheet_max += points
                        earned = 0
                        outcome = "INCORRECT"
                        rep.log("%s.%s: %s ERROR" % (sheet, coord, kind))
                    else:
                        if mode == "penalty":
                            possible = abs(points)
                            earned = 0 if ok else -abs(points)
                        else:
                            possible = points
                            earned = points if ok else 0
                            sheet_max += points
                        outcome = "CORRECT" if ok else "INCORRECT"
                        rep.log("%s.%s: %s %s" % (sheet, coord, kind, outcome))
                    sheet_earned += earned
                    outcomes[key] = outcome
                    sheet_out.line("  [%s] %s %s: %s / %s" % (outcome, coord, label,
                                                             fmt_num(earned), fmt_num(possible)))
                    if nsec is not None:
                        nsec["rows"].append(("rule", coord, coord, concealed, outcome, earned,
                                             possible))
                    if outcome == "INCORRECT" and fail_template is not None:
                        fb_text = render_template(
                            fail_template, ctx.expected, ctx.actual, ctx, rep)
                        sheet_out.line("    Feedback: %s" % fb_text)
                        if nsec is not None:
                            nsec["rows"].append(("feedback", fb_text))
                    if fatal and outcome != "CORRECT":
                        halted = True

            if not check_mode:
                continue

            # Build this rule's checks.
            if ctx is None:
                ctx = RuleContext(sheet, coord, r, c, where, tol, [], rep,
                                  key_ws, sub_ws, key_wbf, sub_wbf, key_eval, sub_eval)
            checks = []  # (name, base_pass, earned, expected, actual, case_template)
            if kind == "scenario":
                if case_results is None:
                    case_results = [(n, False, out_v, None, ft)
                                    for n, _, out_v, _, ft in cases]
                for name, cok, exp_v, act_v, ft in case_results:
                    base = outcome != "SKIPPED" and outcome != "PREREQ" and cok
                    if base:
                        cearned = 0 if mode == "penalty" else points
                    elif outcome in ("SKIPPED", "PREREQ") or ok is None:
                        cearned = 0
                    else:
                        cearned = -abs(points) if mode == "penalty" else 0
                    checks.append(("%s.%s" % (check_name, name), base, cearned,
                                   exp_v, act_v, ft))
            else:
                checks.append((check_name, outcome == "CORRECT", earned,
                               ctx.expected, ctx.actual, None))
            for name, base, cearned, exp_v, act_v, ft in checks:
                passed = base if exp_outcome == "pass" else not base
                if exp_points is not None and float(cearned) != exp_points:
                    passed = False
                sheet_checks += 1
                msg = None
                if passed:
                    sheet_passed += 1
                    check_lines.append("PASS: %s" % name)
                else:
                    check_lines.append("FAIL: %s" % name)
                    tmpl = ft or meta["fail-message"] or fail_template
                    if tmpl is not None:
                        msg = render_template(tmpl, exp_v, act_v, ctx, rep)
                        check_lines.append("    Feedback: %s" % msg)
                if hsec is not None:
                    hsec["rows"].append(("check", coord, coord, concealed, name, passed, msg))

        threshold_hit = min_score is not None and sheet_earned < min_score
        if threshold_hit:
            sheet_earned = 0
            sheet_out.replace_rules("  %s" % threshold_message)
            if hsec is not None:
                hsec["threshold"] = threshold_message
        if check_mode:
            for ln in check_lines:
                cout.line(ln)
            if threshold_hit:
                sheet_passed = 0
                cout.line(threshold_message)
            cout.line("%s: %d / %d passed" % (sheet, sheet_passed, sheet_checks))
            if hsec is not None:
                hsec["subtotal"] = "%s: %d / %d passed" % (sheet, sheet_passed, sheet_checks)
            total_passed += sheet_passed
            total_checks += sheet_checks
        elif hsec is not None:
            hsec["subtotal"] = "%s: %s / %s" % (sheet, fmt_num(sheet_earned),
                                                fmt_num(sheet_max))
        sheet_out.flush()
        out.line("Sheet '%s': %s / %s" % (sheet, fmt_num(sheet_earned), fmt_num(sheet_max)))
        out.line("")
        total_earned += sheet_earned
        total_max += sheet_max
    out.line("Total: %s / %s" % (fmt_num(total_earned), fmt_num(total_max)))
    if html is not None:
        if check_mode:
            html.total = "Total: %d / %d passed" % (total_passed, total_checks)
        else:
            html.total = "Total: %s / %s" % (fmt_num(total_earned), fmt_num(total_max))
    if check_mode:
        cout.lines.append("Total: %d / %d passed" % (total_passed, total_checks))
        return total_passed, total_checks
    return total_earned, total_max


def write_html(path, html):
    if not path or html is None:
        return
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(html.render())
    except OSError as e:
        raise FatalError("cannot write HTML file '%s': %s" % (path, e.strerror or e))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sheeteval.py")
    parser.add_argument("answer_key")
    parser.add_argument("submission")
    parser.add_argument("--score-file")
    parser.add_argument("--report-file")
    parser.add_argument("--detailed", action="store_true")
    parser.add_argument("--debug", "-d", action="store_true")
    parser.add_argument("--log", "-l", action="store_true")
    parser.add_argument("--quiet-warnings", action="store_true")
    parser.add_argument("--check-mode", "-C", action="store_true")
    parser.add_argument("--html-file")
    args = parser.parse_args(argv)
    rep = Reporter(args.quiet_warnings, args.log)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    try:
        if args.check_mode:
            out = Output(False)
            cout = Output(args.detailed)
            html = HtmlReport(check_mode=True) if args.html_file else None
            passed, total = grade(args.answer_key, args.submission, rep, out,
                                  check_mode=True, cout=cout, html=html)
            if args.report_file:
                try:
                    with open(args.report_file, "w", encoding="utf-8", newline="\n") as f:
                        for ln in cout.lines:
                            f.write(ln + "\n")
                except OSError as e:
                    raise FatalError("cannot write report file '%s': %s"
                                     % (args.report_file, e.strerror or e))
            if args.score_file:
                with open(args.score_file, "w", encoding="utf-8") as f:
                    f.write("%d\n" % passed)
            write_html(args.html_file, html)
            sys.stdout.write("Total: %d / %d passed\n" % (passed, total))
            sys.stdout.flush()
            return 0
        out = Output(args.detailed)
        html = HtmlReport() if args.html_file else None
        earned, maximum = grade(args.answer_key, args.submission, rep, out, html=html)
        if args.report_file:
            try:
                with open(args.report_file, "w", encoding="utf-8", newline="\n") as f:
                    for ln in out.lines:
                        f.write(ln + "\n")
            except OSError as e:
                raise FatalError("cannot write report file '%s': %s"
                                 % (args.report_file, e.strerror or e))
        if args.score_file:
            with open(args.score_file, "w", encoding="utf-8") as f:
                f.write(fmt_num(earned) + "\n")
        write_html(args.html_file, html)
        sys.stdout.write("Assignment Score: %s / %s\n" % (fmt_num(earned), fmt_num(maximum)))
        sys.stdout.flush()
        return 0
    except FatalError as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
    except Exception as e:
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        else:
            sys.stderr.write("Error: %s\n" % (e or e.__class__.__name__))
        return 1


if __name__ == "__main__":
    sys.exit(main())
