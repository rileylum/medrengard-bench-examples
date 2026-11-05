#!/usr/bin/env python3
"""cfgpipe - configuration resolver."""
import json
import math
import os
import re
import sys

SOURCES = ("default", "env", "file", "arg")
TYPES = ("string", "integer", "float", "boolean")


class CfgError(Exception):
    pass


class ParseFailure(Exception):
    pass


_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_TRUE = {"true", "t", "yes", "y", "on", "1"}
_FALSE = {"false", "f", "no", "n", "off", "0"}


def parse_value(typ, raw):
    if typ == "string":
        return raw
    s = raw.strip()
    if typ == "integer":
        if not _INT_RE.match(s):
            raise ParseFailure("invalid integer %r" % raw)
        return str(int(s))
    if typ == "float":
        if not _FLOAT_RE.match(s):
            raise ParseFailure("invalid float %r" % raw)
        try:
            v = float(s)
        except (ValueError, OverflowError):
            raise ParseFailure("invalid float %r" % raw)
        if math.isinf(v) or math.isnan(v):
            raise ParseFailure("float out of range %r" % raw)
        if v == 0:
            v = 0.0
        return repr(v)
    if typ == "boolean":
        low = s.lower()
        if low in _TRUE:
            return "true"
        if low in _FALSE:
            return "false"
        raise ParseFailure("invalid boolean %r" % raw)
    raise ParseFailure("unknown type %r" % typ)


def _no_dupes(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise CfgError("duplicate key %r in schema" % k)
        obj[k] = v
    return obj


def _bad_constant(name):
    raise CfgError("invalid JSON constant %s in schema" % name)


def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise CfgError("schema file not found: %s" % path)
    except (OSError, UnicodeDecodeError) as exc:
        raise CfgError("cannot read schema file %s: %s" % (path, exc))
    try:
        schema = json.loads(text, object_pairs_hook=_no_dupes,
                            parse_constant=_bad_constant)
    except json.JSONDecodeError as exc:
        raise CfgError("invalid JSON in schema %s: %s" % (path, exc))
    if not isinstance(schema, dict):
        raise CfgError("schema root must be a JSON object")
    for name, decl in schema.items():
        if not isinstance(decl, dict):
            raise CfgError("parameter %r: declaration must be an object" % name)
        if "type" not in decl:
            raise CfgError("parameter %r: missing required field 'type'" % name)
        if not isinstance(decl["type"], str) or decl["type"] not in TYPES:
            raise CfgError("parameter %r: invalid type %r (expected one of %s)"
                           % (name, decl["type"], ", ".join(TYPES)))
        for src in SOURCES:
            if src in decl and not isinstance(decl[src], str):
                raise CfgError("parameter %r: field %r must be a string"
                               % (name, src))
    return schema


def parse_args(candidates):
    """Return dict name -> value; last occurrence wins."""
    result = {}
    i = 0
    n = len(candidates)
    while i < n:
        tok = candidates[i]
        i += 1
        if tok.startswith("--"):
            body = tok[2:]
        elif tok.startswith("-"):
            body = tok[1:]
        else:
            continue
        if not body:
            continue
        if "=" in body:
            name, value = body.split("=", 1)
            if name:
                result[name] = value
        elif i < n and not candidates[i].startswith("-"):
            result[body] = candidates[i]
            i += 1
    return result


def read_file_source(path):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    content = content.strip()
    if content == "":
        return None
    return content


def lookup(src, key, args):
    if src == "default":
        return key
    if src == "env":
        return os.environ.get(key)
    if src == "file":
        return read_file_source(key)
    if src == "arg":
        return args.get(key)
    return None


def resolve(schema, args):
    out = {}
    unresolved = []
    for name, decl in schema.items():
        typ = decl["type"]
        value = None
        for src in SOURCES:
            if src not in decl:
                continue
            raw = lookup(src, decl[src], args)
            if raw is None:
                continue
            try:
                value = parse_value(typ, raw)
            except ParseFailure as exc:
                raise CfgError("parameter %r: failed to parse source %r as %s: %s"
                               % (name, src, typ, exc))
            break
        if value is None:
            unresolved.append(name)
        else:
            out[name] = value
    if unresolved:
        raise CfgError("unresolved parameters: %s" % ", ".join(unresolved))
    return out


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: cfgpipe.py <schema-file> [arg-candidates...]\n")
        return 2
    try:
        schema = load_schema(argv[1])
        args = parse_args(argv[2:])
        out = resolve(schema, args)
    except CfgError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
