#!/usr/bin/env python3
"""meshctl: manage mesh resources from YAML specs, printing JSON to stdout."""
import json
import os
import re
import sys

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
RUNTIME_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
MEM_RE = re.compile(r"^([0-9]+)(Ki|Mi|Gi|Ti)?$")
CPU_RE = re.compile(r"^([0-9]+)(m)?$")
MEM_MULT = {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}
CPU_MULT = {None: 1000, "m": 1}


def out(obj):
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()


def err(field, message, etype):
    return {"field": field, "message": message, "type": etype}


def fail(errors):
    out({"errors": errors})
    return 1


# ---------------------------------------------------------------- storage

def state_path():
    base = os.environ.get("MESHCTL_STATE_DIR") or os.path.join(os.getcwd(), ".meshctl")
    return os.path.join(base, "meshes.json")


def load_store():
    p = state_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_store(store):
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


# ---------------------------------------------------------------- validation

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_quantity(value, regex, mult):
    """Return the canonical numeric value, or None if invalid."""
    if is_int(value):
        if value < 0:
            return None
        s = str(value)
    elif isinstance(value, str):
        s = value
    else:
        return None
    m = regex.match(s)
    if not m:
        return None
    return int(m.group(1)) * mult[m.group(2)]


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


def validate_resource(kind, value, regex, mult, errors, default=None):
    base = "spec.resources.%s" % kind
    if value is None:
        return default
    if not isinstance(value, dict):
        errors.append(err(base, "%s must be an object with limit and request" % kind, "invalid"))
        return None
    limit = value.get("limit")
    request = value.get("request")
    has_request = request is not None
    lim_n = req_n = None
    if limit is None or (isinstance(limit, str) and limit == ""):
        errors.append(err(base + ".limit", "%s limit is required" % kind, "required"))
    else:
        lim_n = parse_quantity(limit, regex, mult)
        if lim_n is None:
            errors.append(err(base + ".limit", "invalid %s quantity: %r" % (kind, limit), "invalid"))
    if has_request:
        req_n = parse_quantity(request, regex, mult)
        if req_n is None:
            errors.append(err(base + ".request", "invalid %s quantity: %r" % (kind, request), "invalid"))
    else:
        request = limit
        req_n = lim_n
    if has_request and lim_n is not None and req_n is not None and req_n > lim_n:
        errors.append(err(base + ".request", "%s request must not exceed limit" % kind, "invalid"))
    def as_str(v):
        return str(v) if is_int(v) else v
    return {"limit": as_str(limit), "request": as_str(request)}


def validate(doc):
    errors = []
    result = {}

    metadata = doc.get("metadata")
    name = None
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(err("metadata", "metadata must be a mapping", "invalid"))
        metadata = {}
    metadata = metadata or {}
    raw_name = metadata.get("name")
    if raw_name is None or raw_name == "":
        errors.append(err("metadata.name", "name is required", "required"))
    elif not isinstance(raw_name, str) or not NAME_RE.match(raw_name):
        errors.append(err("metadata.name",
                          "name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
    else:
        name = raw_name
    result["metadata"] = {"name": raw_name}

    spec_in = doc.get("spec")
    if spec_in is None:
        spec_in = {}
    if not isinstance(spec_in, dict):
        errors.append(err("spec", "spec must be a mapping", "invalid"))
        spec_in = {}

    find_forbidden(spec_in, "spec", errors)

    spec = {}
    # instances
    inst = spec_in.get("instances")
    if inst is None:
        spec["instances"] = 1
    elif not is_int(inst) or inst < 1:
        errors.append(err("spec.instances", "instances must be a positive integer", "invalid"))
    else:
        spec["instances"] = inst

    # runtime
    if "runtime" in spec_in and spec_in["runtime"] is not None:
        rt = spec_in["runtime"]
        if not isinstance(rt, str) or not RUNTIME_RE.match(rt):
            errors.append(err("spec.runtime", "runtime must be a semantic version major.minor.patch", "invalid"))
        else:
            spec["runtime"] = rt

    # resources
    res_in = spec_in.get("resources")
    if res_in is None:
        res_in = {}
    if not isinstance(res_in, dict):
        errors.append(err("spec.resources", "resources must be a mapping", "invalid"))
        res_in = {}
    resources = {}
    mem = validate_resource("memory", res_in.get("memory"), MEM_RE, MEM_MULT, errors,
                            default={"limit": "1Gi", "request": "1Gi"})
    if mem is not None:
        resources["memory"] = mem
    cpu = validate_resource("cpu", res_in.get("cpu"), CPU_RE, CPU_MULT, errors)
    if cpu is not None:
        resources["cpu"] = cpu
    spec["resources"] = resources

    # access
    acc_in = spec_in.get("access")
    if acc_in is None:
        acc_in = {}
    auth_enabled = True
    if not isinstance(acc_in, dict):
        errors.append(err("spec.access", "access must be a mapping", "invalid"))
    else:
        auth_in = acc_in.get("authentication")
        if auth_in is None:
            auth_in = {}
        if not isinstance(auth_in, dict):
            errors.append(err("spec.access.authentication", "authentication must be a mapping", "invalid"))
        else:
            en = auth_in.get("enabled")
            if en is None:
                pass
            elif not isinstance(en, bool):
                errors.append(err("spec.access.authentication.enabled", "enabled must be a boolean", "invalid"))
            else:
                auth_enabled = en
    spec["access"] = {"authentication": {"enabled": auth_enabled}}

    # migration
    mig_in = spec_in.get("migration")
    if mig_in is None:
        mig_in = {}
    strategy = "FullStop"
    if not isinstance(mig_in, dict):
        errors.append(err("spec.migration", "migration must be a mapping", "invalid"))
    else:
        st = mig_in.get("strategy")
        if st is not None:
            if st != "FullStop":
                errors.append(err("spec.migration.strategy", 'strategy must be "FullStop"', "invalid"))
    spec["migration"] = {"strategy": strategy}

    result["spec"] = spec
    result["status"] = {"state": "Running"}
    return result, name, errors


# ---------------------------------------------------------------- commands

def cmd_create(args):
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--filename", "--file"):
            if i + 1 >= len(args):
                return fail([err("", "flag -f requires a path", "parse")])
            path = args[i + 1]
            i += 2
        elif a.startswith("--filename=") or a.startswith("--file="):
            path = a.split("=", 1)[1]
            i += 1
        elif a.startswith("-f") and len(a) > 2:
            path = a[2:]
            i += 1
        else:
            i += 1
    if path is None:
        return fail([err("", "missing -f <path>", "parse")])
    try:
        import yaml
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        doc = yaml.safe_load(text)
    except Exception as e:
        return fail([err("", "cannot read or parse YAML: %s" % str(e).replace("\n", " "), "parse")])
    if not isinstance(doc, dict):
        return fail([err("", "YAML document must be a mapping", "parse")])

    resource, name, errors = validate(doc)
    store = load_store()
    if name is not None and name in store:
        errors.append(err("metadata.name", "mesh %r already exists" % name, "duplicate"))
    if errors:
        return fail(errors)
    store[name] = resource
    save_store(store)
    out(resource)
    return 0


def get_name(args):
    names = [a for a in args if not a.startswith("-")]
    return names[0] if names else None


def cmd_list(args):
    store = load_store()
    items = []
    for name in sorted(store.keys()):
        r = store[name]
        items.append({"name": name, "status": r.get("status", {"state": "Running"})})
    out(items)
    return 0


def cmd_describe(args):
    name = get_name(args)
    if not name:
        return fail([err("metadata.name", "name is required", "required")])
    store = load_store()
    if name not in store:
        return fail([err("metadata.name", "mesh %r not found" % name, "not_found")])
    out(store[name])
    return 0


def cmd_delete(args):
    name = get_name(args)
    if not name:
        return fail([err("metadata.name", "name is required", "required")])
    store = load_store()
    if name not in store:
        return fail([err("metadata.name", "mesh %r not found" % name, "not_found")])
    del store[name]
    save_store(store)
    out({"message": "mesh %s deleted" % name, "metadata": {"name": name}})
    return 0


COMMANDS = {
    "create": cmd_create,
    "list": cmd_list,
    "describe": cmd_describe,
    "get": cmd_describe,
    "delete": cmd_delete,
}


def main(argv):
    if len(argv) < 2 or argv[0] != "mesh" or argv[1] not in COMMANDS:
        return fail([err("", "usage: meshctl.py mesh <create|list|describe|delete> [arguments]",
                         "invalid")])
    return COMMANDS[argv[1]](argv[2:])


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as e:  # never write to stderr
        out({"errors": [err("", "internal error: %s" % e, "invalid")]})
        code = 1
    sys.exit(code)
