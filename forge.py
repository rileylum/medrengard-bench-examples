#!/usr/bin/env python3
"""Forge: manage hardware blueprints with JSON output."""
import json
import os
import sys
import tempfile
import uuid as uuidlib
from datetime import datetime, timezone

STORE_FILE = "blueprints.json"
RS_KEYS = ("resource_type", "resource_count", "capabilities")


class ForgeError(Exception):
    def __init__(self, error, detail):
        super().__init__(detail)
        self.error = error
        self.detail = detail


def emit(obj, code=0):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def load(data_dir):
    path = os.path.join(data_dir, STORE_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return []
    data = json.loads(content)
    return data if isinstance(data, list) else []


def save(data_dir, blueprints):
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".bp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(blueprints, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(data_dir, STORE_FILE))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sort_key(bp):
    return (bp.get("created_at", ""), bp.get("name", ""), bp.get("uuid", ""))


def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def validate(doc):
    errors = []
    if not isinstance(doc, dict):
        return ["input must be a JSON object, got %s" % type_name(doc)]

    if "name" not in doc:
        errors.append("name: is required")
    elif not isinstance(doc["name"], str):
        errors.append("name: must be a string, got %s" % type_name(doc["name"]))
    elif doc["name"] == "":
        errors.append("name: must be a non-empty string")

    if "requirement_sets" not in doc:
        errors.append("requirement_sets: is required")
    else:
        rss = doc["requirement_sets"]
        if not isinstance(rss, list):
            errors.append("requirement_sets: must be an array, got %s" % type_name(rss))
        elif len(rss) == 0:
            errors.append("requirement_sets: must be a non-empty array")
        else:
            for i, rs in enumerate(rss):
                p = "requirement_sets[%d]" % i
                if not isinstance(rs, dict):
                    errors.append("%s: must be an object, got %s" % (p, type_name(rs)))
                    continue
                for k in rs:
                    if k not in RS_KEYS:
                        errors.append("%s.%s: unsupported key" % (p, k))
                if "resource_type" not in rs:
                    errors.append("%s.resource_type: is required" % p)
                elif not isinstance(rs["resource_type"], str):
                    errors.append("%s.resource_type: must be a string, got %s"
                                  % (p, type_name(rs["resource_type"])))
                if "resource_count" not in rs:
                    errors.append("%s.resource_count: is required" % p)
                else:
                    c = rs["resource_count"]
                    if isinstance(c, bool) or not isinstance(c, int):
                        errors.append("%s.resource_count: must be a positive integer, got %s"
                                      % (p, type_name(c)))
                    elif c < 1:
                        errors.append("%s.resource_count: must be a positive integer (>= 1), got %d"
                                      % (p, c))
                if "capabilities" in rs:
                    caps = rs["capabilities"]
                    if not isinstance(caps, list):
                        errors.append("%s.capabilities: must be an array of strings, got %s"
                                      % (p, type_name(caps)))
                    else:
                        for j, cap in enumerate(caps):
                            if not isinstance(cap, str):
                                errors.append("%s.capabilities[%d]: must be a string, got %s"
                                              % (p, j, type_name(cap)))
    return errors


def _reject_constant(c):
    raise ValueError("invalid JSON constant: %s" % c)


def cmd_create(data_dir, args):
    if args:
        raise ForgeError("validation_error", "create takes no arguments")
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise ForgeError("validation_error", "invalid JSON input: %s" % e)
    errors = validate(doc)
    if errors:
        raise ForgeError("validation_error", "; ".join(errors))

    blueprints = load(data_dir)
    name = doc["name"]
    if any(bp.get("name") == name for bp in blueprints):
        raise ForgeError("conflict", "blueprint with name '%s' already exists" % name)

    rsets = []
    for rs in doc["requirement_sets"]:
        out = {"resource_type": rs["resource_type"], "resource_count": rs["resource_count"]}
        if "capabilities" in rs:
            out["capabilities"] = list(rs["capabilities"])
        rsets.append(out)

    bp = {
        "uuid": str(uuidlib.uuid4()),
        "name": name,
        "requirement_sets": rsets,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    blueprints.append(bp)
    save(data_dir, blueprints)
    return bp


def cmd_list(data_dir, args):
    if args:
        raise ForgeError("validation_error", "list takes no arguments")
    return sorted(load(data_dir), key=sort_key)


def cmd_get(data_dir, args):
    if len(args) != 1:
        raise ForgeError("validation_error", "get requires exactly one argument: <uuid>")
    ident = args[0]
    for bp in load(data_dir):
        if bp.get("uuid") == ident:
            return bp
    raise ForgeError("not_found", "blueprint '%s' not found" % ident)


def cmd_delete(data_dir, args):
    names = None
    rest = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--names":
            if i + 1 >= len(args):
                raise ForgeError("validation_error", "--names requires a value")
            if names is not None:
                raise ForgeError("validation_error", "--names given more than once")
            names = args[i + 1]
            i += 2
        elif a.startswith("--names="):
            if names is not None:
                raise ForgeError("validation_error", "--names given more than once")
            names = a[len("--names="):]
            i += 1
        else:
            rest.append(a)
            i += 1

    blueprints = load(data_dir)

    if names is not None:
        if rest:
            raise ForgeError("validation_error",
                             "delete accepts either <uuid> or --names, not both")
        requested = names.split(",")
        existing = {bp.get("name") for bp in blueprints}
        unresolved = []
        for n in requested:
            if n not in existing and n not in unresolved:
                unresolved.append(n)
        if unresolved:
            raise ForgeError("not_found", "blueprints not found for names: %s"
                             % ", ".join(json.dumps(n) for n in unresolved))
        target = set(requested)
        remaining = [bp for bp in blueprints if bp.get("name") not in target]
        count = len(blueprints) - len(remaining)
        save(data_dir, remaining)
        return {"deleted": count}

    if len(rest) != 1:
        raise ForgeError("validation_error",
                         "delete requires exactly one <uuid> or --names <csv>")
    ident = rest[0]
    remaining = [bp for bp in blueprints if bp.get("uuid") != ident]
    if len(remaining) == len(blueprints):
        raise ForgeError("not_found", "blueprint '%s' not found" % ident)
    save(data_dir, remaining)
    return {"deleted": len(blueprints) - len(remaining)}


COMMANDS = {"create": cmd_create, "list": cmd_list, "get": cmd_get, "delete": cmd_delete}


def parse_argv(argv):
    data_dir = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if data_dir is None and a == "--data-dir":
            if i + 1 >= len(argv):
                raise ForgeError("validation_error", "--data-dir requires a value")
            data_dir = argv[i + 1]
            i += 2
            continue
        if data_dir is None and a.startswith("--data-dir="):
            data_dir = a[len("--data-dir="):]
            i += 1
            continue
        rest.append(a)
        i += 1
    if not data_dir:
        raise ForgeError("validation_error", "--data-dir is required")
    if not rest or rest[0] != "blueprint":
        raise ForgeError("validation_error", "usage: forge.py --data-dir <path> blueprint <command> [args]")
    if len(rest) < 2:
        raise ForgeError("validation_error", "missing blueprint command")
    cmd = rest[1]
    if cmd not in COMMANDS:
        raise ForgeError("validation_error", "unknown command '%s'" % cmd)
    return data_dir, cmd, rest[2:]


def main(argv):
    try:
        data_dir, cmd, args = parse_argv(argv)
        result = COMMANDS[cmd](data_dir, args)
    except ForgeError as e:
        emit({"error": e.error, "detail": e.detail}, 1)
    emit(result, 0)


if __name__ == "__main__":
    main(sys.argv[1:])
