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


def build(doc, state, mode="create", stored=None):
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
    elif mode == "update":
        if not is_int(inst) or inst < 0:
            errors.append(err("spec.instances", "spec.instances must be a non-negative integer", "invalid"))
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

    validate_network(spec_in, spec, errors, stored)

    if mode == "create" and name_ok and name in state:
        errors.append(err("metadata.name", "mesh %r already exists" % name, "duplicate"))

    if errors:
        fail(errors)

    md = dict(metadata_in)
    md["name"] = name
    if mode == "update":
        return {"metadata": md, "spec": spec}
    n = spec["instances"]
    status = {
        "state": "Running" if n > 0 else "Stopped",
        "stable": True,
        "instances": {"ready": n, "starting": 0, "stopped": 0},
        "conditions": [cond("Healthy"), cond("PrechecksPassed")],
    }
    return {"metadata": md, "spec": spec, "status": status}


def cond(ctype, status="True", message=""):
    return {"type": ctype, "status": status, "message": message}


def set_cond(status, ctype, st="True", message=""):
    conds = [c for c in status.get("conditions", []) if c.get("type") != ctype]
    conds.append(cond(ctype, st, message))
    conds.sort(key=lambda c: c["type"])
    status["conditions"] = conds


def clear_cond(status, ctype):
    status["conditions"] = sorted([c for c in status.get("conditions", []) if c.get("type") != ctype],
                                  key=lambda c: c["type"])


def has_cond(status, ctype):
    return any(c.get("type") == ctype for c in status.get("conditions", []))


def default_replication(instances):
    if not is_int(instances) or instances < 1:
        return 1
    return min(instances, 2)


def validate_network(spec_in, spec, errors, stored):
    net_in = spec_in.get("network")
    if net_in is not None and not isinstance(net_in, dict):
        errors.append(err("spec.network", "spec.network must be an object", "invalid"))
        return
    net = dict(net_in or {})
    st_in = net.get("storage")
    if st_in is not None and not isinstance(st_in, dict):
        errors.append(err("spec.network.storage", "spec.network.storage must be an object", "invalid"))
        st = None
    else:
        st = dict(st_in or {})
        size = st.get("size")
        if size is None:
            st["size"] = "1Gi"
        elif parse_memory(size) is None:
            errors.append(err("spec.network.storage.size",
                              "invalid storage size quantity: %r" % (size,), "invalid"))
        else:
            old = None
            if stored is not None:
                old = (((stored.get("spec") or {}).get("network") or {}).get("storage") or {}).get("size")
            if old is not None and parse_memory(old) != parse_memory(size):
                errors.append(err("spec.network.storage.size",
                                  "field 'spec.network.storage.size' is immutable after creation",
                                  "immutable"))
            elif old is not None:
                st["size"] = old
        eph = st.get("ephemeral")
        if eph is None:
            st["ephemeral"] = False
        elif not isinstance(eph, bool):
            errors.append(err("spec.network.storage.ephemeral",
                              "spec.network.storage.ephemeral must be a boolean", "invalid"))
        if "className" in st:
            cn = st["className"]
            if cn is None:
                del st["className"]
            elif not isinstance(cn, str) or cn.strip() == "":
                errors.append(err("spec.network.storage.className",
                                  "spec.network.storage.className must be a non-empty string", "invalid"))
        net["storage"] = st
    inst = spec.get("instances")
    rf = net.get("replicationFactor")
    if rf is None:
        net["replicationFactor"] = default_replication(inst)
    elif not is_int(rf):
        errors.append(err("spec.network.replicationFactor",
                          "spec.network.replicationFactor must be a positive integer, got %r" % (rf,),
                          "invalid"))
    elif rf < 1:
        errors.append(err("spec.network.replicationFactor",
                          "spec.network.replicationFactor is %d but must be at least 1" % rf, "invalid"))
    elif is_int(inst) and inst > 0 and rf > inst:
        errors.append(err("spec.network.replicationFactor",
                          "spec.network.replicationFactor is %d but must not exceed spec.instances (%d)"
                          % (rf, inst), "invalid"))
    spec["network"] = net


def deep_merge(base, upd):
    if not isinstance(base, dict) or not isinstance(upd, dict):
        return copy.deepcopy(upd)
    res = copy.deepcopy(base)
    for k, v in upd.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            res[k] = deep_merge(res[k], v)
        else:
            res[k] = copy.deepcopy(v)
    return res


def render(resource):
    r = copy.deepcopy(resource)
    r.pop("_transient", None)
    st = ((r.get("spec") or {}).get("network") or {}).get("storage")
    if isinstance(st, dict) and st.get("ephemeral") is True:
        for k in list(st.keys()):
            if k != "ephemeral":
                del st[k]
    return r


def settle(resource):
    """Complete transient transitions. Returns True if anything changed."""
    if not resource.get("_transient"):
        return False
    status = resource.setdefault("status", {})
    n = resource["spec"].get("instances", 0)
    status["instances"] = {"ready": n, "starting": 0, "stopped": 0}
    clear_cond(status, "Scaling")
    status["stable"] = True
    status["state"] = "Running" if n > 0 else "Stopped"
    del resource["_transient"]
    return True


def read_doc(args):
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
    return doc


def cmd_create(args):
    doc = read_doc(args)
    state = load_state()
    resource = build(doc, state)
    state[resource["metadata"]["name"]] = resource
    save_state(state)
    out(render(resource))


def cmd_update(args):
    doc = read_doc(args)
    if not isinstance(doc, dict):
        fail([err("", "document must be a YAML mapping", "parse")])
    md = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    name = md.get("name")
    state = load_state()
    if not isinstance(name, str) or name not in state:
        if name is None or name == "":
            fail([err("metadata.name", "metadata.name is required", "required")])
        fail([err("metadata.name", "mesh %r not found" % (name,), "not_found")])
    stored = copy.deepcopy(state[name])
    settle(stored)
    status = copy.deepcopy(stored.get("status") or {})

    spec_upd = doc.get("spec")
    if spec_upd is None:
        spec_upd = {}
    if not isinstance(spec_upd, dict):
        fail([err("spec", "spec must be an object", "invalid")])
    stopped = has_cond(status, "GracefulShutdown")
    merged_spec = deep_merge(stored.get("spec") or {}, spec_upd)
    if spec_upd.get("instances") is None:
        if stopped and is_int(status.get("desiredInstancesOnResume")):
            merged_spec["instances"] = status["desiredInstancesOnResume"]
        else:
            merged_spec["instances"] = (stored.get("spec") or {}).get("instances", 1)
    merged_md = deep_merge(stored.get("metadata") or {}, md)
    merged_md["name"] = name
    # build() validates everything and exits with all errors on failure
    result = build({"metadata": merged_md, "spec": merged_spec}, state, "update", stored)
    spec = result["spec"]

    old = (stored.get("spec") or {}).get("instances", 0)
    new = spec["instances"]
    transient = False
    if old > 0 and new == 0:
        set_cond(status, "GracefulShutdown", "True", "")
        clear_cond(status, "Scaling")
        status["desiredInstancesOnResume"] = old
        status["instances"] = {"ready": 0, "starting": 0, "stopped": old}
        status["state"] = "Stopped"
        status["stable"] = True
    elif old == 0 and new > 0:
        clear_cond(status, "GracefulShutdown")
        status.pop("desiredInstancesOnResume", None)
        status["instances"] = {"ready": 0, "starting": new, "stopped": 0}
        status["state"] = "Running"
        status["stable"] = False
        if not stopped:
            set_cond(status, "Scaling", "True", "Scaling up from %d to %d instances" % (old, new))
        transient = True
    elif new > old:
        set_cond(status, "Scaling", "True", "Scaling up from %d to %d instances" % (old, new))
        status["instances"] = {"ready": old, "starting": new - old, "stopped": 0}
        status["state"] = "Running"
        status["stable"] = False
        transient = True
    elif new < old:
        set_cond(status, "Scaling", "True", "Scaling down from %d to %d instances" % (old, new))
        status["instances"] = {"ready": new, "starting": 0, "stopped": 0}
        status["state"] = "Running"
        status["stable"] = False
        transient = True
    status.setdefault("conditions", [])
    status["conditions"].sort(key=lambda c: c["type"])

    resource = {"metadata": result["metadata"], "spec": spec, "status": status}
    if transient:
        resource["_transient"] = True
    state[name] = resource
    save_state(state)
    out(render(resource))


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
    resource = state[name]
    if settle(resource):
        save_state(state)
    out(render(resource))


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
        fail([err("", "usage: meshctl.py mesh <create|update|list|describe|delete> [arguments]", "invalid")])
    op, rest = argv[1], argv[2:]
    handlers = {"create": cmd_create, "update": cmd_update, "list": cmd_list, "describe": cmd_describe, "delete": cmd_delete}
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
