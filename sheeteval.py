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


def grade(key_path, sub_path, rep, out):
    import openpyxl

    for p in (key_path, sub_path):
        if not os.path.isfile(p):
            raise FatalError("file not found: %s" % p)
    key_wb = openpyxl.load_workbook(key_path, data_only=True)
    sub_wb = openpyxl.load_workbook(sub_path, data_only=True)

    if "EvalManifest" not in key_wb.sheetnames:
        raise FatalError("answer key has no 'EvalManifest' worksheet")
    manifest = key_wb["EvalManifest"]
    name_col = find_header_col(manifest, MANIFEST_HEADERS)
    if name_col is None:
        raise FatalError("EvalManifest worksheet has no name/sheet/tab column")

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
        sheets.append(s)

    total_earned = 0
    total_max = 0
    for sheet in sheets:
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
        key_ws = key_wb[sheet] if sheet in key_wb.sheetnames else None
        sub_ws = sub_wb[sheet] if sub_wb is not None and sheet in sub_wb.sheetnames else None
        out.line("Sheet '%s':" % sheet)
        sheet_earned = 0
        sheet_max = 0

        for row in range(2, cl.max_row + 1):
            raw = cl.cell(row=row, column=id_col).value
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            coord, r, c = parse_coord(raw)
            flags = cl.cell(row=row, column=flags_col).value if flags_col else None
            if isinstance(flags, str) and flags.strip().lower() == "manual":
                continue
            parsed = parse_annotation(get_cell(key_ws, r, c))
            if parsed is None:
                rep.warn("%s.%s: missing or unparseable rule annotation; rule skipped"
                         % (sheet, coord))
                continue
            kind, points, rule, data = parsed
            if kind != "literal":
                rep.warn("%s.%s: unknown rule kind '%s'; rule skipped" % (sheet, coord, kind))
                continue
            where = "%s.%s" % (sheet, coord)
            tol = get_tolerance(rule, where, rep)
            mode = get_mode(rule, where, rep)
            key_val = key_ws.cell(row=r, column=c).value
            sub_val = sub_ws.cell(row=r, column=c).value if sub_ws is not None else None
            ok = compare(key_val, sub_val, tol)
            if not ok and "alternates" in data and data["alternates"] is not None:
                alts = data["alternates"]
                if not isinstance(alts, (list, tuple)):
                    alts = [alts]
                for alt in alts:
                    try:
                        acoord, ar, ac = parse_coord(alt)
                    except ValueError:
                        rep.warn("%s: alternate cell reference %r could not be found or "
                                 "resolved; alternate skipped" % (where, alt))
                        continue
                    if compare(key_ws.cell(row=ar, column=ac).value, sub_val, tol):
                        ok = True
                        break
            if mode == "penalty":
                possible = abs(points)
                earned = 0 if ok else -abs(points)
            else:
                possible = points
                earned = points if ok else 0
                sheet_max += points
            sheet_earned += earned
            out.line("  [%s] %s %s: %s / %s" % ("CORRECT" if ok else "INCORRECT", coord, coord,
                                               fmt_num(earned), fmt_num(possible)))
            rep.log("%s.%s: %s %s" % (sheet, coord, kind, "CORRECT" if ok else "INCORRECT"))
        out.line("Sheet '%s': %s / %s" % (sheet, fmt_num(sheet_earned), fmt_num(sheet_max)))
        out.line("")
        total_earned += sheet_earned
        total_max += sheet_max
    out.line("Total: %s / %s" % (fmt_num(total_earned), fmt_num(total_max)))
    return total_earned, total_max


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
    args = parser.parse_args(argv)
    rep = Reporter(args.quiet_warnings, args.log)
    try:
        out = Output(args.detailed)
        earned, maximum = grade(args.answer_key, args.submission, rep, out)
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
