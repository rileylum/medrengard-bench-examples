#!/usr/bin/env python3
"""Forge: manage hardware blueprints, allocations, units, modules and tags with JSON output."""
import json
import os
import re
import sys
import tempfile
import uuid as uuidlib
from datetime import datetime, timezone

STORE_FILE = "blueprints.json"
RS_KEYS = ("resource_type", "resource_count", "capabilities")


class ForgeError(Exception):
    def __init__(self, error, detail, extra=None):
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.extra = extra or {}


MIN_REVISION = (1, 0)
MAX_REVISION = (1, 2)
REVISION = MIN_REVISION
REVISION_RE = re.compile(r"^[0-9]+\.[0-9]+$")


def fmt_rev(r):
    return "%d.%d" % r


def parse_revision(value):
    if value == "latest":
        return MAX_REVISION
    if not isinstance(value, str) or not REVISION_RE.match(value):
        raise ForgeError("parse_error", "invalid revision '%s': expected <major>.<minor>" % value)
    major, minor = value.split(".")
    rev = (int(major), int(minor))
    if not (MIN_REVISION <= rev <= MAX_REVISION) or rev[0] != 1:
        raise ForgeError("unsupported_revision",
                         "revision %s is not supported (supported: %s through %s)"
                         % (fmt_rev(rev), fmt_rev(MIN_REVISION), fmt_rev(MAX_REVISION)))
    return rev


def require_revision(minimum, feature):
    if REVISION < minimum:
        raise ForgeError("revision_too_low",
                         "%s requires revision %s or higher (negotiated %s)"
                         % (feature, fmt_rev(minimum), fmt_rev(REVISION)),
                         {"feature": feature, "min_revision": fmt_rev(minimum)})


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
        raise ForgeError("validation_error", "get requires exactly one argument: <identifier>")
    ident = args[0]
    if UUID_RE.match(ident):
        for bp in load(data_dir):
            if bp.get("uuid") == ident:
                return bp
        raise ForgeError("not_found", "blueprint '%s' not found" % ident)
    require_revision((1, 2), "blueprint get-by-name")
    for bp in load(data_dir):
        if bp.get("name") == ident:
            return bp
    raise ForgeError("not_found", "blueprint with name '%s' not found" % ident)


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

ALLOC_FILE = "allocations.json"
CAT_TYPE_A = "category=TYPE_A"


def load_allocs(data_dir):
    path = os.path.join(data_dir, ALLOC_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return []
    data = json.loads(content)
    return data if isinstance(data, list) else []


def save_allocs(data_dir, allocs):
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".al-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(allocs, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(data_dir, ALLOC_FILE))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def alloc_view(a):
    out = {k: v for k, v in a.items() if k != "tenant_id"}
    if REVISION >= (1, 1):
        out["tenant_id"] = a.get("tenant_id")
    return out


def alloc_sort_key(a):
    return (a.get("created_at", ""), a.get("uuid", ""))


def cmd_alloc_create(data_dir, args):
    if args:
        raise ForgeError("validation_error", "allocation create takes no arguments")
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise ForgeError("validation_error", "invalid JSON input: %s" % e)
    if not isinstance(doc, dict):
        raise ForgeError("validation_error",
                         "input must be a JSON object, got %s" % type_name(doc))
    if "blueprint_name" not in doc:
        raise ForgeError("validation_error", "blueprint_name: is required")
    name = doc["blueprint_name"]
    if not isinstance(name, str):
        raise ForgeError("validation_error",
                         "blueprint_name: must be a string, got %s" % type_name(name))
    if name == "":
        raise ForgeError("validation_error", "blueprint_name: must be a non-empty string")

    bp = None
    for b in load(data_dir):
        if b.get("name") == name:
            bp = b
            break
    if bp is None:
        raise ForgeError("not_found", "blueprint with name '%s' not found" % name)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    created = []
    for idx, rs in enumerate(bp.get("requirement_sets") or []):
        caps = rs.get("capabilities") if isinstance(rs, dict) else None
        n = 2 if isinstance(caps, list) and CAT_TYPE_A in caps else 1
        for _ in range(n):
            created.append({
                "uuid": str(uuidlib.uuid4()),
                "blueprint_name": name,
                "requirement_set_index": idx,
                "binding_status": "unbound",
                "assignment_id": None,
                "created_at": now,
            })
    allocs = load_allocs(data_dir)
    allocs.extend(created)
    save_allocs(data_dir, allocs)
    return [alloc_view(a) for a in created]


def _take_opt(args, i, flag):
    a = args[i]
    if a == flag:
        if i + 1 >= len(args):
            raise ForgeError("validation_error", "%s requires a value" % flag)
        return args[i + 1], i + 2
    return a[len(flag) + 1:], i + 1


def cmd_alloc_list(data_dir, args):
    assignment = None
    status = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--assignment" or a.startswith("--assignment="):
            assignment, i = _take_opt(args, i, "--assignment")
        elif a == "--status" or a.startswith("--status="):
            status, i = _take_opt(args, i, "--status")
        else:
            raise ForgeError("validation_error", "unexpected argument '%s'" % a)
    out = []
    for al in load_allocs(data_dir):
        if assignment is not None and al.get("assignment_id") != assignment:
            continue
        if status is not None and al.get("binding_status") != status:
            continue
        out.append(al)
    return [alloc_view(a) for a in sorted(out, key=alloc_sort_key)]


def cmd_alloc_get(data_dir, args):
    if len(args) != 1:
        raise ForgeError("validation_error", "get requires exactly one argument: <uuid>")
    ident = args[0]
    for al in load_allocs(data_dir):
        if al.get("uuid") == ident:
            return alloc_view(al)
    raise ForgeError("not_found", "allocation '%s' not found" % ident)


def _parse_flags(args, flags):
    vals = {}
    i = 0
    while i < len(args):
        a = args[i]
        for f in flags:
            if a == f or a.startswith(f + "="):
                if f in vals:
                    raise ForgeError("validation_error", "%s given more than once" % f)
                vals[f], i = _take_opt(args, i, f)
                break
        else:
            raise ForgeError("validation_error", "unexpected argument '%s'" % a)
    return vals


def cmd_alloc_bind(data_dir, args):
    if args:
        raise ForgeError("validation_error", "allocation bind takes no arguments")
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise ForgeError("validation_error", "invalid JSON input: %s" % e)
    if not isinstance(doc, dict):
        raise ForgeError("validation_error",
                         "input must be a JSON object, got %s" % type_name(doc))
    if "tenant_id" in doc:
        require_revision((1, 1), "tenant_id in allocation bind")
    errors = []
    if "assignment_id" not in doc or doc["assignment_id"] is None:
        errors.append("assignment_id: is required")
    elif not isinstance(doc["assignment_id"], str):
        errors.append("assignment_id: must be a string, got %s"
                      % type_name(doc["assignment_id"]))
    elif doc["assignment_id"] == "":
        errors.append("assignment_id: must be a non-empty string")
    if "allocations" not in doc or doc["allocations"] is None:
        errors.append("allocations: is required")
    elif not isinstance(doc["allocations"], dict):
        errors.append("allocations: must be an object, got %s"
                      % type_name(doc["allocations"]))
    elif len(doc["allocations"]) == 0:
        errors.append("allocations: must contain at least one entry")
    if "tenant_id" in doc and doc["tenant_id"] is not None \
            and not isinstance(doc["tenant_id"], str):
        errors.append("tenant_id: must be a string, got %s" % type_name(doc["tenant_id"]))
    if errors:
        raise ForgeError("validation_error", "; ".join(errors))

    assignment = doc["assignment_id"]
    ops = doc["allocations"]
    bad = [k for k, v in ops.items() if v not in ("add", "remove") or not isinstance(v, str)]
    if bad:
        raise ForgeError("invalid_operation",
                         "invalid operation for allocations: %s (must be \"add\" or \"remove\")"
                         % ", ".join(json.dumps(k) for k in bad))

    allocs = load_allocs(data_dir)
    by_id = {a.get("uuid"): a for a in allocs}
    missing = [k for k in ops if k not in by_id]
    if missing:
        raise ForgeError("not_found", "allocations not found: %s"
                         % ", ".join(json.dumps(k) for k in missing))

    has_add = any(v == "add" for v in ops.values())
    if has_add and any(a.get("assignment_id") == assignment
                       and a.get("binding_status") == "bound" for a in allocs):
        raise ForgeError("duplicate_assignment",
                         "assignment '%s' already has bound allocations" % assignment)

    changed = False
    for k, v in ops.items():
        al = by_id[k]
        if "tenant_id" in doc and al.get("tenant_id") != doc["tenant_id"]:
            al["tenant_id"] = doc["tenant_id"]
            changed = True
        if v == "add":
            al["binding_status"] = "bound"
            al["assignment_id"] = assignment
            changed = True
        elif al.get("binding_status") != "unbound" or al.get("assignment_id") is not None:
            al["binding_status"] = "unbound"
            al["assignment_id"] = None
            changed = True
    if changed:
        save_allocs(data_dir, allocs)
    return {"status": "accepted"}


def cmd_alloc_delete(data_dir, args):
    vals = _parse_flags(args, ("--assignment", "--ids"))
    if len(vals) != 1:
        raise ForgeError("validation_error",
                         "exactly one of --assignment or --ids must be provided")
    allocs = load_allocs(data_dir)
    if "--assignment" in vals:
        target = vals["--assignment"]
        remaining = [a for a in allocs if a.get("assignment_id") != target]
    else:
        requested = vals["--ids"].split(",")
        existing = {a.get("uuid") for a in allocs}
        unresolved = []
        for r in requested:
            if r not in existing and r not in unresolved:
                unresolved.append(r)
        if unresolved:
            raise ForgeError("not_found", "allocations not found: %s"
                             % ", ".join(json.dumps(r) for r in unresolved))
        target = set(requested)
        remaining = [a for a in allocs if a.get("uuid") not in target]
    count = len(allocs) - len(remaining)
    if count:
        save_allocs(data_dir, remaining)
    return {"deleted": count}


UNIT_FILE = "units.json"
UNIT_SYSTEM_FIELDS = ("uuid", "status", "module_uuids", "created_at", "reserved_capacity")


def load_units(data_dir):
    path = os.path.join(data_dir, UNIT_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return []
    data = json.loads(content)
    return data if isinstance(data, list) else []


def save_units(data_dir, units):
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".un-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(units, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(data_dir, UNIT_FILE))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def unit_view(u):
    out = dict(u)
    mods = out.get("module_uuids")
    out["module_uuids"] = sorted(mods) if isinstance(mods, list) else []
    caps = out.get("capabilities")
    out["capabilities"] = sorted(set(caps)) if isinstance(caps, list) else []
    cap = out.get("capacity")
    if out.get("status") == "inactive":
        out["reserved_capacity"] = cap if isinstance(cap, int) and not isinstance(cap, bool) else 0
    else:
        out["reserved_capacity"] = 0
    return out


def unit_sort_key(u):
    return (u.get("host", ""), u.get("category", ""), u.get("created_at", ""),
            u.get("uuid", ""))


def cmd_unit_create(data_dir, args):
    if args:
        raise ForgeError("validation_error", "unit create takes no arguments")
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise ForgeError("validation_error", "invalid JSON input: %s" % e)
    if not isinstance(doc, dict):
        raise ForgeError("validation_error",
                         "input must be a JSON object, got %s" % type_name(doc))
    errors = []
    for k in ("category", "manufacturer", "host"):
        if k not in doc:
            errors.append("%s: is required" % k)
        elif not isinstance(doc[k], str):
            errors.append("%s: must be a string, got %s" % (k, type_name(doc[k])))
        elif doc[k] == "":
            errors.append("%s: must be a non-empty string" % k)
    if "capacity" not in doc:
        errors.append("capacity: is required")
    else:
        c = doc["capacity"]
        if isinstance(c, bool) or not isinstance(c, int):
            errors.append("capacity: must be a non-negative integer, got %s" % type_name(c))
        elif c < 0:
            errors.append("capacity: must be a non-negative integer, got %d" % c)
    if "parent_uuid" in doc:
        p = doc["parent_uuid"]
        if not isinstance(p, str):
            errors.append("parent_uuid: must be a string, got %s" % type_name(p))
        elif p == "":
            errors.append("parent_uuid: must be a non-empty string")
    if "capabilities" in doc:
        caps = doc["capabilities"]
        if not isinstance(caps, list):
            errors.append("capabilities: must be an array of strings, got %s" % type_name(caps))
        else:
            for j, cap in enumerate(caps):
                if not isinstance(cap, str):
                    errors.append("capabilities[%d]: must be a string, got %s"
                                  % (j, type_name(cap)))
    for k in UNIT_SYSTEM_FIELDS:
        if k in doc:
            errors.append("%s: is system-assigned and must not be provided" % k)
    if errors:
        raise ForgeError("validation_error", "; ".join(errors))

    units = load_units(data_dir)
    parent = doc.get("parent_uuid")
    if parent is not None and not any(u.get("uuid") == parent for u in units):
        raise ForgeError("not_found", "parent unit '%s' not found" % parent)

    unit = {
        "uuid": str(uuidlib.uuid4()),
        "category": doc["category"],
        "manufacturer": doc["manufacturer"],
        "host": doc["host"],
        "status": "active",
        "parent_uuid": parent,
        "module_uuids": [],
        "capacity": doc["capacity"],
        "capabilities": sorted(set(doc.get("capabilities") or [])),
        "reserved_capacity": 0,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    units.append(unit)
    save_units(data_dir, units)
    return unit_view(unit)


def _unit_field_matches(u, field, value):
    if field not in u:
        return False
    v = u[field]
    if v is None:
        return value == "null"
    if isinstance(v, bool):
        return value == ("true" if v else "false")
    if isinstance(v, int):
        return str(v) == value
    if isinstance(v, str):
        return v == value
    return False


def cmd_unit_list(data_dir, args):
    named = {"--category": "category", "--manufacturer": "manufacturer", "--host": "host"}
    filters = []
    i = 0
    while i < len(args):
        a = args[i]
        for f in list(named) + ["--filter"]:
            if a == f or a.startswith(f + "="):
                val, i = _take_opt(args, i, f)
                if f == "--filter":
                    if "=" not in val:
                        raise ForgeError("validation_error",
                                         "invalid filter '%s': expected key=value" % val)
                    k, v = val.split("=", 1)
                    filters.append((k, v))
                else:
                    filters.append((named[f], val))
                break
        else:
            raise ForgeError("validation_error", "unexpected argument '%s'" % a)
    out = []
    for u in load_units(data_dir):
        view = unit_view(u)
        if all(_unit_field_matches(view, k, v) for k, v in filters):
            out.append(view)
    return sorted(out, key=unit_sort_key)


def cmd_unit_get(data_dir, args):
    if len(args) != 1:
        raise ForgeError("validation_error", "get requires exactly one argument: <uuid>")
    ident = args[0]
    for u in load_units(data_dir):
        if u.get("uuid") == ident:
            return unit_view(u)
    raise ForgeError("not_found", "unit '%s' not found" % ident)


def _unit_transition(data_dir, args, name, frm, to):
    if len(args) != 1:
        raise ForgeError("validation_error", "%s requires exactly one argument: <uuid>" % name)
    ident = args[0]
    units = load_units(data_dir)
    for u in units:
        if u.get("uuid") == ident:
            if u.get("status") != frm:
                raise ForgeError("invalid_transition",
                                 "cannot %s unit '%s': status is '%s', expected '%s'"
                                 % (name, ident, u.get("status"), frm))
            u["status"] = to
            cap = u.get("capacity")
            u["reserved_capacity"] = (cap if to == "inactive" and isinstance(cap, int)
                                      and not isinstance(cap, bool) else 0)
            if not isinstance(u.get("capabilities"), list):
                u["capabilities"] = []
            save_units(data_dir, units)
            return unit_view(u)
    raise ForgeError("not_found", "unit '%s' not found" % ident)


def cmd_unit_activate(data_dir, args):
    return _unit_transition(data_dir, args, "activate", "inactive", "active")


def cmd_unit_deactivate(data_dir, args):
    return _unit_transition(data_dir, args, "deactivate", "active", "inactive")


def cmd_ledger_capabilities(data_dir, args):
    if args:
        raise ForgeError("validation_error", "ledger capabilities takes no arguments")
    caps = set()
    for u in load_units(data_dir):
        if u.get("status") == "active" and isinstance(u.get("capabilities"), list):
            caps.update(c for c in u["capabilities"] if isinstance(c, str))
    return sorted(caps)


MODULE_FILE = "modules.json"
TAG_FILE = "tags.json"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _load_list(data_dir, fname):
    path = os.path.join(data_dir, fname)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return []
    data = json.loads(content)
    return data if isinstance(data, list) else []


def _save_list(data_dir, fname, items):
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".x-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(data_dir, fname))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json_object():
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as e:
        raise ForgeError("validation_error", "invalid JSON input: %s" % e)
    if not isinstance(doc, dict):
        raise ForgeError("validation_error",
                         "input must be a JSON object, got %s" % type_name(doc))
    return doc


def _require_str(doc, k, errors):
    if k not in doc or doc[k] is None:
        errors.append("%s: is required" % k)
    elif not isinstance(doc[k], str):
        errors.append("%s: must be a string, got %s" % (k, type_name(doc[k])))
    elif doc[k] == "":
        errors.append("%s: must be a non-empty string" % k)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def module_view(m):
    return {
        "uuid": m.get("uuid"),
        "unit_uuid": m.get("unit_uuid"),
        "component_type": m.get("component_type"),
        "firmware_image": m.get("firmware_image"),
        "created_at": m.get("created_at"),
    }


def cmd_module_create(data_dir, args):
    if args:
        raise ForgeError("validation_error", "module create takes no arguments")
    doc = _read_json_object()
    errors = []
    _require_str(doc, "unit_uuid", errors)
    _require_str(doc, "component_type", errors)
    if errors:
        raise ForgeError("validation_error", "; ".join(errors))
    img = doc.get("firmware_image")
    if img is not None and (not isinstance(img, str) or not UUID_RE.match(img)):
        raise ForgeError("invalid_image",
                         "firmware_image: %s is not a valid lowercase UUID" % json.dumps(img))
    units = load_units(data_dir)
    unit = None
    for u in units:
        if u.get("uuid") == doc["unit_uuid"]:
            unit = u
            break
    if unit is None:
        raise ForgeError("not_found", "unit '%s' not found" % doc["unit_uuid"])
    mod = {
        "uuid": str(uuidlib.uuid4()),
        "unit_uuid": doc["unit_uuid"],
        "component_type": doc["component_type"],
        "firmware_image": img,
        "created_at": _now(),
    }
    mods = _load_list(data_dir, MODULE_FILE)
    mods.append(mod)
    _save_list(data_dir, MODULE_FILE, mods)
    mu = unit.get("module_uuids")
    mu = list(mu) if isinstance(mu, list) else []
    mu.append(mod["uuid"])
    unit["module_uuids"] = sorted(mu)
    save_units(data_dir, units)
    return module_view(mod)


def cmd_module_list(data_dir, args):
    filters = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--unit" or a.startswith("--unit="):
            val, i = _take_opt(args, i, "--unit")
            filters.append(("unit_uuid", val))
        elif a == "--filter" or a.startswith("--filter="):
            val, i = _take_opt(args, i, "--filter")
            if "=" not in val:
                raise ForgeError("validation_error",
                                 "invalid filter '%s': expected key=value" % val)
            k, v = val.split("=", 1)
            filters.append((k, v))
        else:
            raise ForgeError("validation_error", "unexpected argument '%s'" % a)
    out = []
    for m in _load_list(data_dir, MODULE_FILE):
        view = module_view(m)
        if all(_unit_field_matches(view, k, v) for k, v in filters):
            out.append(view)
    return sorted(out, key=lambda m: (m["unit_uuid"] or "", m["created_at"] or "",
                                      m["uuid"] or ""))


def cmd_module_get(data_dir, args):
    if len(args) != 1:
        raise ForgeError("validation_error", "get requires exactly one argument: <uuid>")
    for m in _load_list(data_dir, MODULE_FILE):
        if m.get("uuid") == args[0]:
            return module_view(m)
    raise ForgeError("not_found", "module '%s' not found" % args[0])


def cmd_module_flash(data_dir, args):
    image = None
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--image" or a.startswith("--image="):
            if image is not None:
                raise ForgeError("validation_error", "--image given more than once")
            image, i = _take_opt(args, i, "--image")
        else:
            pos.append(a)
            i += 1
    if image is None:
        raise ForgeError("validation_error", "--image is required")
    if len(pos) != 1:
        raise ForgeError("validation_error", "flash requires exactly one argument: <uuid>")
    if not UUID_RE.match(image):
        raise ForgeError("invalid_image", "image '%s' is not a valid lowercase UUID" % image)
    mods = _load_list(data_dir, MODULE_FILE)
    target = None
    for m in mods:
        if m.get("uuid") == pos[0]:
            target = m
            break
    if target is None:
        raise ForgeError("not_found", "module '%s' not found" % pos[0])
    known = {m.get("firmware_image") for m in mods if m.get("firmware_image") is not None}
    if image not in known:
        raise ForgeError("flash_failed", "firmware image '%s' is not a known image" % image)
    if target.get("firmware_image") != image:
        target["firmware_image"] = image
        _save_list(data_dir, MODULE_FILE, mods)
    return module_view(target)


def tag_view(t):
    return {k: t.get(k) for k in ("uuid", "module_uuid", "key", "value", "created_at")}


def cmd_tag_create(data_dir, args):
    if args:
        raise ForgeError("validation_error", "tag create takes no arguments")
    doc = _read_json_object()
    errors = []
    for k in ("module_uuid", "key", "value"):
        _require_str(doc, k, errors)
    if errors:
        raise ForgeError("validation_error", "; ".join(errors))
    if not any(m.get("uuid") == doc["module_uuid"]
               for m in _load_list(data_dir, MODULE_FILE)):
        raise ForgeError("not_found", "module '%s' not found" % doc["module_uuid"])
    tags = _load_list(data_dir, TAG_FILE)
    if any(t.get("module_uuid") == doc["module_uuid"] and t.get("key") == doc["key"]
           for t in tags):
        raise ForgeError("conflict", "tag with key '%s' already exists on module '%s'"
                         % (doc["key"], doc["module_uuid"]))
    tag = {
        "uuid": str(uuidlib.uuid4()),
        "module_uuid": doc["module_uuid"],
        "key": doc["key"],
        "value": doc["value"],
        "created_at": _now(),
    }
    tags.append(tag)
    _save_list(data_dir, TAG_FILE, tags)
    return tag_view(tag)


def cmd_tag_list(data_dir, args):
    vals = _parse_flags(args, ("--module", "--key"))
    out = []
    for t in _load_list(data_dir, TAG_FILE):
        if "--module" in vals and t.get("module_uuid") != vals["--module"]:
            continue
        if "--key" in vals and t.get("key") != vals["--key"]:
            continue
        out.append(tag_view(t))
    return sorted(out, key=lambda t: (t["module_uuid"] or "", t["key"] or "",
                                      t["uuid"] or ""))


def cmd_tag_get(data_dir, args):
    if len(args) != 1:
        raise ForgeError("validation_error", "get requires exactly one argument: <uuid>")
    for t in _load_list(data_dir, TAG_FILE):
        if t.get("uuid") == args[0]:
            return tag_view(t)
    raise ForgeError("not_found", "tag '%s' not found" % args[0])


def cmd_tag_delete(data_dir, args):
    if len(args) != 1:
        raise ForgeError("validation_error", "delete requires exactly one argument: <uuid>")
    tags = _load_list(data_dir, TAG_FILE)
    remaining = [t for t in tags if t.get("uuid") != args[0]]
    if len(remaining) == len(tags):
        raise ForgeError("not_found", "tag '%s' not found" % args[0])
    _save_list(data_dir, TAG_FILE, remaining)
    return {"deleted": len(tags) - len(remaining)}


GROUPS = {
    "blueprint": COMMANDS,
    "allocation": {"create": cmd_alloc_create, "list": cmd_alloc_list, "get": cmd_alloc_get,
                   "bind": cmd_alloc_bind, "delete": cmd_alloc_delete},
    "unit": {"create": cmd_unit_create, "list": cmd_unit_list, "get": cmd_unit_get,
             "activate": cmd_unit_activate, "deactivate": cmd_unit_deactivate},
    "ledger": {"capabilities": cmd_ledger_capabilities},
    "module": {"create": cmd_module_create, "list": cmd_module_list, "get": cmd_module_get,
               "flash": cmd_module_flash},
    "tag": {"create": cmd_tag_create, "list": cmd_tag_list, "get": cmd_tag_get,
            "delete": cmd_tag_delete},
}
UNKNOWN_COMMAND_GROUPS = ("module", "tag")


COLLECTION_KEYS = {
    cmd_list: "blueprints",
    cmd_alloc_create: "allocations",
    cmd_alloc_list: "allocations",
    cmd_unit_list: "units",
    cmd_ledger_capabilities: "capabilities",
    cmd_module_list: "modules",
    cmd_tag_list: "tags",
}


def extract_revision(argv):
    """Remove --revision options from argv; return (raw_value_or_None, remaining)."""
    raw = None
    seen = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--revision":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise ForgeError("parse_error", "--revision requires a value")
            val = argv[i + 1]
            i += 2
        elif a.startswith("--revision="):
            val = a[len("--revision="):]
            i += 1
        else:
            rest.append(a)
            i += 1
            continue
        if not seen:
            raw = val
            seen = True
    return raw, rest


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
    if rest and rest[0] == "version":
        if len(rest) > 1:
            raise ForgeError("validation_error", "version takes no arguments")
        return data_dir, None, []
    if not data_dir:
        raise ForgeError("validation_error", "--data-dir is required")
    if not rest or rest[0] not in GROUPS:
        raise ForgeError("validation_error",
                         "usage: forge.py --data-dir <path> {version|blueprint|allocation|unit|ledger|module|tag} <command> [args]")
    group = rest[0]
    if len(rest) < 2:
        raise ForgeError("unknown_command" if group in UNKNOWN_COMMAND_GROUPS
                         else "validation_error", "missing %s command" % group)
    cmd = rest[1]
    if cmd not in GROUPS[group]:
        raise ForgeError("unknown_command" if group in UNKNOWN_COMMAND_GROUPS
                         else "validation_error", "unknown command '%s'" % cmd)
    return data_dir, GROUPS[group][cmd], rest[2:]


def main(argv):
    global REVISION
    REVISION = MIN_REVISION
    try:
        raw, argv = extract_revision(argv)
        if raw is not None:
            REVISION = parse_revision(raw)
    except ForgeError as e:
        REVISION = MIN_REVISION
        out = {"error": e.error, "detail": e.detail}
        out.update(e.extra)
        out["revision"] = fmt_rev(MIN_REVISION)
        emit(out, 1)
    try:
        data_dir, func, args = parse_argv(argv)
        if func is None:
            emit({"max_revision": fmt_rev(MAX_REVISION),
                  "min_revision": fmt_rev(MIN_REVISION)}, 0)
        result = func(data_dir, args)
    except ForgeError as e:
        out = {"error": e.error, "detail": e.detail}
        out.update(e.extra)
        out["revision"] = fmt_rev(REVISION)
        emit(out, 1)
    rev = fmt_rev(REVISION)
    if func in COLLECTION_KEYS:
        result = {"revision": rev, COLLECTION_KEYS[func]: result}
    elif isinstance(result, dict):
        result = dict(result)
        result["revision"] = rev
    emit(result, 0)


if __name__ == "__main__":
    main(sys.argv[1:])
