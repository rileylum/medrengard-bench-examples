#!/usr/bin/env python3
"""meshctl - manage mesh resources from YAML specs, printing JSON to stdout."""
import copy
import json
import os
import re
import sys

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*|\d+)\.(\d+)\.(\d+)$")
MEM_RE = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti)?$")
CPU_RE = re.compile(r"^(\d+)(m)?$")
MEM_MULT = {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}

_MISSING = object()


def state_path():
    env = os.environ.get("MESHCTL_STATE")
    if env:
        return env
    return os.path.join(os.getcwd(), ".meshctl_state.json")


def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(state):
    path = state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def out(obj, code=0):
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def err(field, message, etype):
    return {"field": field, "message": message, "type": etype}


def fail(errors):
    out({"errors": errors}, 1)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_memory(v):
    """Return bytes or None if invalid."""
    if is_int(v):
        return v if v >= 0 else None
    if isinstance(v, str):
        m = MEM_RE.match(v)
        if m:
            return int(m.group(1)) * MEM_MULT[m.group(2)]
    return None


def parse_cpu(v):
    """Return millicores or None if invalid."""
    if is_int(v):
        return v * 1000 if v >= 0 else None
    if isinstance(v, str):
        m = CPU_RE.match(v)
        if m:
            n = int(m.group(1))
            return n if m.group(2) else n * 1000
    return None


def is_empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def find_forbidden(node, path, errors):
    if isinstance(node, dict):
        for k, v in node.items():
            p = "%s.%s" % (path, k)
            if k == "autoScaling":
                errors.append(err(p, "field autoScaling is forbidden", "forbidden"))
            find_forbidden(v, p, errors)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            find_forbidden(v, "%s[%d]" % (path, i), errors)


def validate_resource(spec_res, kind, parser, default, errors):
    """Validate spec.resources.<kind>; return normalized object or _MISSING."""
    base = "spec.resources.%s" % kind
    val = spec_res.get(kind, _MISSING) if isinstance(spec_res, dict) else _MISSING
    if val is _MISSING or val is None:
        return copy.deepcopy(default) if default is not None else _MISSING
    if not isinstance(val, dict):
        errors.append(err(base, "%s must be an object with limit and request" % kind, "invalid"))
        return _MISSING
    result = dict(val)
    limit = val.get("limit")
    request = val.get("request")
    limit_q = None
    if is_empty(limit):
        errors.append(err(base + ".limit", "%s limit is required" % kind, "required"))
    else:
        limit_q = parser(limit)
        if limit_q is None:
            errors.append(err(base + ".limit", "invalid %s quantity: %r" % (kind, limit), "invalid"))
    if "request" not in val or request is None:
        if not is_empty(limit):
            result["request"] = limit
    else:
        req_q = parser(request)
        if req_q is None:
            errors.append(err(base + ".request", "invalid %s quantity: %r" % (kind, request), "invalid"))
        elif limit_q is not None and req_q > limit_q:
            errors.append(err(base + ".request", "%s request must not exceed limit" % kind, "invalid"))
    return result


def build(doc, state):
    errors = []
    if not isinstance(doc, dict):
        fail([err("", "document must be a YAML mapping", "parse")])

    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        metadata_in = {}
        if metadata is not None:
            errors.append(err("metadata", "metadata must be an object", "invalid"))
    else:
        metadata_in = metadata

    name = metadata_in.get("name")
    name_ok = False
    if name is None or (isinstance(name, str) and name == ""):
        errors.append(err("metadata.name", "metadata.name is required", "required"))
    elif not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(err("metadata.name",
                          "metadata.name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
    else:
        name_ok = True

    spec_in = doc.get("spec")
    if spec_in is None:
        spec_in = {}
    if not isinstance(spec_in, dict):
        errors.append(err("spec", "spec must be an object", "invalid"))
        spec_in = {}

    find_forbidden(spec_in, "spec", errors)

    spec = copy.deepcopy(spec_in)

    # instances
    inst = spec_in.get("instances", _MISSING)
    if inst is _MISSING or inst is None:
        spec["instances"] = 1
    elif not is_int(inst) or inst < 1:
        errors.append(err("spec.instances", "spec.instances must be a positive integer", "invalid"))

    # runtime
    if "runtime" in spec_in:
        rt = spec_in["runtime"]
        if rt is None:
            del spec["runtime"]
        elif not isinstance(rt, str) or not SEMVER_RE.match(rt):
            errors.append(err("spec.runtime", "spec.runtime must be a semantic version major.minor.patch",
                              "invalid"))

    # resources
    res_in = spec_in.get("resources")
    if res_in is not None and not isinstance(res_in, dict):
        errors.append(err("spec.resources", "spec.resources must be an object", "invalid"))
        res_in = {}
    res_in = res_in or {}
    resources = dict(res_in)
    mem = validate_resource(res_in, "memory", parse_memory, {"limit": "1Gi", "request": "1Gi"}, errors)
    if mem is not _MISSING:
        resources["memory"] = mem
    cpu = validate_resource(res_in, "cpu", parse_cpu, None, errors)
    if cpu is not _MISSING:
        resources["cpu"] = cpu
    else:
        resources.pop("cpu", None)
    spec["resources"] = resources

    # access.authentication.enabled
    access_in = spec_in.get("access")
    access = {}
    if access_in is not None and not isinstance(access_in, dict):
        errors.append(err("spec.access", "spec.access must be an object", "invalid"))
    else:
        access = dict(access_in or {})
        auth_in = access.get("authentication")
        if auth_in is not None and not isinstance(auth_in, dict):
            errors.append(err("spec.access.authentication",
                              "spec.access.authentication must be an object", "invalid"))
        else:
            auth = dict(auth_in or {})
            enabled = auth.get("enabled")
            if enabled is None:
                auth["enabled"] = True
            elif not isinstance(enabled, bool):
                errors.append(err("spec.access.authentication.enabled",
                                  "spec.access.authentication.enabled must be a boolean", "invalid"))
            access["authentication"] = auth
    spec["access"] = access

    # migration.strategy
    mig_in = spec_in.get("migration")
    if mig_in is not None and not isinstance(mig_in, dict):
        errors.append(err("spec.migration", "spec.migration must be an object", "invalid"))
    else:
        mig = dict(mig_in or {})
        strat = mig.get("strategy")
        if strat is None:
            mig["strategy"] = "FullStop"
        elif strat != "FullStop":
            errors.append(err("spec.migration.strategy",
                              "spec.migration.strategy must be \"FullStop\"", "invalid"))
        spec["migration"] = mig

    if name_ok and name in state:
        errors.append(err("metadata.name", "mesh %r already exists" % name, "duplicate"))

    if errors:
        fail(errors)

    md = dict(metadata_in)
    md["name"] = name
    return {"metadata": md, "spec": spec, "status": {"state": "Running"}}


def cmd_create(args):
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--filename", "--file"):
            if i + 1 < len(args):
                path = args[i + 1]
            i += 2
            continue
        if a.startswith("-f=") or a.startswith("--filename="):
            path = a.split("=", 1)[1]
        elif a.startswith("-f") and len(a) > 2:
            path = a[2:]
        i += 1
    if path is None:
        fail([err("", "missing required flag -f <path>", "parse")])
    try:
        import yaml
    except Exception:
        fail([err("", "PyYAML is not installed", "parse")])
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        doc = yaml.safe_load(text)
    except Exception as e:
        fail([err("", "cannot read or parse YAML: %s" % str(e).replace("\n", " "), "parse")])
    state = load_state()
    resource = build(doc, state)
    state[resource["metadata"]["name"]] = resource
    save_state(state)
    out(resource)


def cmd_list(args):
    state = load_state()
    items = [{"name": n, "status": {"state": r.get("status", {}).get("state", "Running")}}
             for n, r in state.items()]
    items.sort(key=lambda x: x["name"])
    out(items)


def need_name(args):
    names = [a for a in args if not a.startswith("-")]
    if not names:
        fail([err("metadata.name", "mesh name is required", "required")])
    return names[0]


def cmd_describe(args):
    name = need_name(args)
    state = load_state()
    if name not in state:
        fail([err("metadata.name", "mesh %r not found" % name, "not_found")])
    out(state[name])


def cmd_delete(args):
    name = need_name(args)
    state = load_state()
    if name not in state:
        fail([err("metadata.name", "mesh %r not found" % name, "not_found")])
    del state[name]
    save_state(state)
    out({"message": "mesh %s deleted" % name, "metadata": {"name": name}})


def main(argv):
    if len(argv) < 2 or argv[0] != "mesh":
        fail([err("", "usage: meshctl.py mesh <create|list|describe|delete> [arguments]", "invalid")])
    op, rest = argv[1], argv[2:]
    handlers = {"create": cmd_create, "list": cmd_list, "describe": cmd_describe, "delete": cmd_delete}
    if op not in handlers:
        fail([err("", "unknown operation %r" % op, "invalid")])
    handlers[op](rest)


if __name__ == "__main__":
    try:
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # never leak tracebacks
        out({"errors": [err("", "internal error: %s" % e, "invalid")]}, 1)
