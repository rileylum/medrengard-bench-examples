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

def state_path(fname="meshes.json"):
    base = os.environ.get("MESHCTL_STATE_DIR") or os.path.join(os.getcwd(), ".meshctl")
    return os.path.join(base, fname)


def load_store(fname="meshes.json"):
    p = state_path(fname)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_store(store, fname="meshes.json"):
    p = state_path(fname)
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


def default_replication(instances):
    return max(1, min(2, instances))


def as_qstr(v):
    return str(v) if is_int(v) else v


def validate(doc, update=False):
    """Validate a document. With update=True the spec is an already-merged spec
    (stored values + provided fields) and instances may be 0."""
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
    min_inst = 0 if update else 1
    inst_ok = False
    if inst is None:
        spec["instances"] = 1
        inst_ok = True
    elif not is_int(inst) or inst < min_inst:
        if update:
            errors.append(err("spec.instances", "instances must be a non-negative integer", "invalid"))
        else:
            errors.append(err("spec.instances", "instances must be a positive integer", "invalid"))
    else:
        spec["instances"] = inst
        inst_ok = True

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

    # network
    net_in = spec_in.get("network")
    if net_in is None:
        net_in = {}
    if not isinstance(net_in, dict):
        errors.append(err("spec.network", "network must be a mapping", "invalid"))
        net_in = {}
    network = {}
    sto_in = net_in.get("storage")
    if sto_in is None:
        sto_in = {}
    if not isinstance(sto_in, dict):
        errors.append(err("spec.network.storage", "storage must be a mapping", "invalid"))
        sto_in = {}
    storage = {}
    size = sto_in.get("size")
    if size is None:
        storage["size"] = "1Gi"
    elif parse_quantity(size, MEM_RE, MEM_MULT) is None:
        errors.append(err("spec.network.storage.size",
                          "invalid storage size quantity: %r" % (size,), "invalid"))
    else:
        storage["size"] = as_qstr(size)
    eph = sto_in.get("ephemeral")
    if eph is None:
        storage["ephemeral"] = False
    elif not isinstance(eph, bool):
        errors.append(err("spec.network.storage.ephemeral", "ephemeral must be a boolean", "invalid"))
    else:
        storage["ephemeral"] = eph
    cn = sto_in.get("className")
    if cn is not None:
        if not isinstance(cn, str) or cn == "":
            errors.append(err("spec.network.storage.className",
                              "className must be a non-empty string", "invalid"))
        else:
            storage["className"] = cn
    network["storage"] = storage

    rf = net_in.get("replicationFactor")
    if rf is None:
        if inst_ok:
            network["replicationFactor"] = default_replication(spec["instances"])
    elif not is_int(rf) or rf < 1:
        errors.append(err("spec.network.replicationFactor",
                          "replicationFactor must be a positive integer, got %r (minimum 1)" % (rf,),
                          "invalid"))
    else:
        network["replicationFactor"] = rf
        if inst_ok and spec["instances"] > 0 and rf > spec["instances"]:
            errors.append(err("spec.network.replicationFactor",
                              "replicationFactor %d exceeds spec.instances %d"
                              % (rf, spec["instances"]), "invalid"))
    spec["network"] = network

    result["spec"] = spec
    return result, name, errors


# ---------------------------------------------------------------- status

def set_condition(status, ctype, cstatus="True", message=""):
    conds = [c for c in status.get("conditions", []) if c.get("type") != ctype]
    conds.append({"type": ctype, "status": cstatus, "message": message})
    status["conditions"] = sorted(conds, key=lambda c: c["type"])


def clear_condition(status, ctype):
    status["conditions"] = sorted(
        [c for c in status.get("conditions", []) if c.get("type") != ctype],
        key=lambda c: c["type"])


def has_condition(status, ctype):
    return any(c.get("type") == ctype for c in status.get("conditions", []))


def settle(resource):
    """Complete transient transitions. Returns True if anything changed."""
    st = resource.get("status", {})
    changed = False
    n = resource["spec"]["instances"]
    if has_condition(st, "Scaling"):
        clear_condition(st, "Scaling")
        changed = True
    inst = st.get("instances", {})
    if n > 0 and (inst.get("starting", 0) != 0 or inst.get("ready") != n):
        st["instances"] = {"ready": n, "starting": 0, "stopped": 0}
        changed = True
    if not st.get("stable", True) or "stable" not in st:
        st["stable"] = True
        changed = True
    resource["status"] = st
    return changed


def render(resource):
    r = json.loads(json.dumps(resource))
    sto = r.get("spec", {}).get("network", {}).get("storage")
    if isinstance(sto, dict) and sto.get("ephemeral"):
        r["spec"]["network"]["storage"] = {"ephemeral": True}
    st = r.get("status", {})
    order = ["state", "stable", "instances", "conditions", "desiredInstancesOnResume"]
    if "conditions" in st:
        st["conditions"] = [{"type": c.get("type"), "status": c.get("status"),
                             "message": c.get("message", "")}
                            for c in sorted(st["conditions"], key=lambda c: c.get("type", ""))]
    r["status"] = {k: st[k] for k in order if k in st}
    return r


def deep_merge(base, patch):
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    res = dict(base)
    for k, v in patch.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            res[k] = deep_merge(res[k], v)
        else:
            res[k] = v
    return res


# ---------------------------------------------------------------- commands

def read_doc(args):
    """Return (doc, error_code_or_None)."""
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--filename", "--file"):
            if i + 1 >= len(args):
                return None, fail([err("", "flag -f requires a path", "parse")])
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
        return None, fail([err("", "missing -f <path>", "parse")])
    try:
        import yaml
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        doc = yaml.safe_load(text)
    except Exception as e:
        return None, fail([err("", "cannot read or parse YAML: %s" % str(e).replace("\n", " "), "parse")])
    if not isinstance(doc, dict):
        return None, fail([err("", "YAML document must be a mapping", "parse")])
    return doc, None


def cmd_create(args):
    doc, code = read_doc(args)
    if code is not None:
        return code
    resource, name, errors = validate(doc)
    store = load_store()
    if name is not None and name in store:
        errors.append(err("metadata.name", "mesh %r already exists" % name, "duplicate"))
    if errors:
        return fail(errors)
    n = resource["spec"]["instances"]
    status = {
        "state": "Running" if n > 0 else "Stopped",
        "stable": True,
        "instances": {"ready": n, "starting": 0, "stopped": 0},
        "conditions": [],
    }
    set_condition(status, "Healthy")
    set_condition(status, "PrechecksPassed")
    resource["status"] = status
    store[name] = resource
    save_store(store)
    out(render(resource))
    return 0


def cmd_update(args):
    doc, code = read_doc(args)
    if code is not None:
        return code
    errors = []
    metadata = doc.get("metadata")
    raw_name = metadata.get("name") if isinstance(metadata, dict) else None
    if raw_name is None or raw_name == "":
        return fail([err("metadata.name", "name is required", "required")])
    if not isinstance(raw_name, str) or not NAME_RE.match(raw_name):
        return fail([err("metadata.name", "name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid")])
    store = load_store()
    if raw_name not in store:
        return fail([err("metadata.name", "mesh %r not found" % raw_name, "not_found")])
    stored = json.loads(json.dumps(store[raw_name]))
    settle(stored)
    old_status = stored["status"]
    old_n = stored["spec"]["instances"]
    stopped = has_condition(old_status, "GracefulShutdown")

    spec_patch = doc.get("spec")
    if spec_patch is None:
        spec_patch = {}
    if not isinstance(spec_patch, dict):
        return fail([err("spec", "spec must be a mapping", "invalid")])

    req_inst = spec_patch.get("instances")
    resuming = False
    if stopped and old_n == 0:
        if req_inst is None:
            target = old_status.get("desiredInstancesOnResume") or 1
            spec_patch = dict(spec_patch)
            spec_patch["instances"] = target
            resuming = True
        elif is_int(req_inst) and req_inst > 0:
            resuming = True

    merged_spec = deep_merge(stored["spec"], spec_patch)
    resource, _, errors = validate({"metadata": {"name": raw_name}, "spec": merged_spec}, update=True)

    # immutable storage size
    new_size = None
    sto_patch = spec_patch.get("network", {})
    if isinstance(sto_patch, dict):
        sto_patch = sto_patch.get("storage", {})
        if isinstance(sto_patch, dict):
            new_size = sto_patch.get("size")
    old_size = stored["spec"].get("network", {}).get("storage", {}).get("size")
    if new_size is not None:
        nv = parse_quantity(new_size, MEM_RE, MEM_MULT)
        ov = parse_quantity(old_size, MEM_RE, MEM_MULT) if old_size is not None else None
        if nv is not None and (old_size is None or nv != ov):
            errors = [e for e in errors if e["field"] != "spec.network.storage.size"]
            errors.append(err("spec.network.storage.size",
                              "field 'spec.network.storage.size' is immutable after creation",
                              "immutable"))
        elif nv is not None:
            resource["spec"]["network"]["storage"]["size"] = old_size
    if errors:
        return fail(errors)

    new_n = resource["spec"]["instances"]
    status = old_status
    if resuming:
        clear_condition(status, "GracefulShutdown")
        status.pop("desiredInstancesOnResume", None)
        status["instances"] = {"ready": 0, "starting": new_n, "stopped": 0}
        status["state"] = "Running"
        status["stable"] = False
    elif new_n == 0 and old_n > 0:
        set_condition(status, "GracefulShutdown")
        status["desiredInstancesOnResume"] = old_n
        status["instances"] = {"ready": 0, "starting": 0, "stopped": old_n}
        status["state"] = "Stopped"
        status["stable"] = True
    elif new_n > old_n:
        set_condition(status, "Scaling", "True",
                      "scaling up from %d to %d instances" % (old_n, new_n))
        status["instances"] = {"ready": old_n, "starting": new_n - old_n, "stopped": 0}
        status["state"] = "Running"
        status["stable"] = False
    elif new_n < old_n:
        set_condition(status, "Scaling", "True",
                      "scaling down from %d to %d instances" % (old_n, new_n))
        status["instances"] = {"ready": new_n, "starting": 0, "stopped": 0}
        status["state"] = "Running"
        status["stable"] = False
    else:
        status["state"] = "Running" if new_n > 0 else "Stopped"
    resource["status"] = status
    store[raw_name] = resource
    save_store(store)
    out(render(resource))
    return 0


def get_name(args):
    names = [a for a in args if not a.startswith("-")]
    return names[0] if names else None


def cmd_list(args):
    store = load_store()
    items = []
    for name in sorted(store.keys()):
        r = render(store[name])
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
    r = store[name]
    if settle(r):
        save_store(store)
    out(render(r))
    return 0


def cmd_delete(args):
    name = get_name(args)
    if not name:
        return fail([err("metadata.name", "name is required", "required")])
    store = load_store()
    if name not in store:
        return fail([err("metadata.name", "mesh %r not found" % name, "not_found")])
    vaults = load_store(VAULT_FILE)
    deps = sorted(v for v, r in vaults.items()
                  if r.get("spec", {}).get("meshRef") == name)
    if deps:
        return fail([err("metadata.name",
                         "mesh %r cannot be deleted: referenced by vault(s) %s"
                         % (name, ", ".join(deps)), "conflict")])
    del store[name]
    save_store(store)
    out({"message": "mesh %s deleted" % name, "metadata": {"name": name}})
    return 0


# ---------------------------------------------------------------- vaults

VAULT_FILE = "vaults.json"
UPDATE_POLICIES = ("retain", "recreate")


def validate_vault(doc):
    """Validate a vault document (already merged for update).
    Returns (resource, name, errors)."""
    errors = []
    metadata = doc.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(err("metadata", "metadata must be a mapping", "invalid"))
        metadata = {}
    metadata = metadata or {}
    raw_name = metadata.get("name")
    name = None
    if raw_name is None or raw_name == "":
        errors.append(err("metadata.name", "name is required", "required"))
    elif not isinstance(raw_name, str) or not NAME_RE.match(raw_name):
        errors.append(err("metadata.name",
                          "name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
    else:
        name = raw_name

    spec_in = doc.get("spec")
    if spec_in is None:
        spec_in = {}
    if not isinstance(spec_in, dict):
        errors.append(err("spec", "spec must be a mapping", "invalid"))
        spec_in = {}

    spec = {}
    mref = spec_in.get("meshRef")
    if mref is None or mref == "":
        errors.append(err("spec.meshRef", "meshRef is required", "required"))
    elif not isinstance(mref, str):
        errors.append(err("spec.meshRef", "meshRef must be a string", "invalid"))
    else:
        spec["meshRef"] = mref

    vname = spec_in.get("vaultName")
    if vname is None or vname == "":
        if name is not None:
            spec["vaultName"] = name
    elif not isinstance(vname, str):
        errors.append(err("spec.vaultName", "vaultName must be a string", "invalid"))
    else:
        spec["vaultName"] = vname

    pol = spec_in.get("updatePolicy")
    if pol is None:
        spec["updatePolicy"] = "retain"
    elif pol not in UPDATE_POLICIES:
        errors.append(err("spec.updatePolicy",
                          'updatePolicy must be "retain" or "recreate", got %r' % (pol,),
                          "invalid"))
    else:
        spec["updatePolicy"] = pol

    tpl = spec_in.get("template")
    tref = spec_in.get("templateRef")
    if tpl is not None:
        if not isinstance(tpl, str):
            errors.append(err("spec.template", "template must be a string", "invalid"))
        else:
            spec["template"] = tpl
    if tref is not None:
        if not isinstance(tref, str):
            errors.append(err("spec.templateRef", "templateRef must be a string", "invalid"))
        else:
            spec["templateRef"] = tref
    if tpl is not None and tref is not None:
        errors.append(err("spec.template",
                          "at most one of spec.template and spec.templateRef may be set",
                          "invalid"))
    return {"metadata": {"name": raw_name}, "spec": spec}, name, errors


def vault_status(meshes, mesh_name):
    mesh = meshes.get(mesh_name)
    if mesh is None:
        ready, msg = "False", "parent mesh %r not found" % mesh_name
    elif mesh.get("status", {}).get("stable", True):
        ready, msg = "True", ""
    else:
        ready, msg = "False", "parent mesh %r is not stable" % mesh_name
    return {
        "state": "Ready" if ready == "True" else "Pending",
        "conditions": [{"message": msg, "status": ready, "type": "Ready"}],
    }


def render_vault(resource, meshes):
    r = json.loads(json.dumps(resource))
    spec = r.get("spec", {})
    order = ["meshRef", "vaultName", "updatePolicy", "template", "templateRef"]
    r["spec"] = {k: spec[k] for k in order if k in spec}
    r["status"] = vault_status(meshes, spec.get("meshRef"))
    return {"metadata": r.get("metadata", {}), "spec": r["spec"], "status": r["status"]}


def check_vault_refs(resource, name, meshes, vaults, errors):
    spec = resource["spec"]
    mref = spec.get("meshRef")
    if mref is not None and mref not in meshes:
        errors.append(err("spec.meshRef", "mesh %r does not exist" % mref, "invalid"))
    vname = spec.get("vaultName")
    if mref is not None and vname is not None:
        for other, r in vaults.items():
            if other == name:
                continue
            os_ = r.get("spec", {})
            if os_.get("meshRef") == mref and os_.get("vaultName") == vname:
                errors.append(err("spec.vaultName",
                                  "vault %r in mesh %r already exists (vault %s)"
                                  % (vname, mref, other), "duplicate"))
                break


def cmd_vault_create(args):
    doc, code = read_doc(args)
    if code is not None:
        return code
    resource, name, errors = validate_vault(doc)
    meshes = load_store()
    vaults = load_store(VAULT_FILE)
    if name is not None and name in vaults:
        errors.append(err("metadata.name", "vault %r already exists" % name, "duplicate"))
    check_vault_refs(resource, name, meshes, vaults, errors)
    if errors:
        return fail(errors)
    vaults[name] = resource
    save_store(vaults, VAULT_FILE)
    out(render_vault(resource, meshes))
    return 0


def cmd_vault_update(args):
    doc, code = read_doc(args)
    if code is not None:
        return code
    metadata = doc.get("metadata")
    raw_name = metadata.get("name") if isinstance(metadata, dict) else None
    if raw_name is None or raw_name == "":
        return fail([err("metadata.name", "name is required", "required")])
    if not isinstance(raw_name, str) or not NAME_RE.match(raw_name):
        return fail([err("metadata.name", "name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid")])
    meshes = load_store()
    vaults = load_store(VAULT_FILE)
    if raw_name not in vaults:
        return fail([err("metadata.name", "vault %r not found" % raw_name, "not_found")])
    stored = vaults[raw_name]
    patch = doc.get("spec")
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        return fail([err("spec", "spec must be a mapping", "invalid")])
    errors = []
    old_spec = stored.get("spec", {})
    for f in ("meshRef", "vaultName"):
        nv = patch.get(f)
        if nv is not None and nv != old_spec.get(f):
            errors.append(err("spec." + f,
                              "field 'spec.%s' is immutable after creation" % f, "immutable"))
    merged = deep_merge(old_spec, patch)
    for f in ("meshRef", "vaultName"):
        if f in old_spec:
            merged[f] = old_spec[f]
    resource, name, verrs = validate_vault({"metadata": {"name": raw_name}, "spec": merged})
    errors.extend(verrs)
    check_vault_refs(resource, raw_name, meshes, vaults, errors)
    if errors:
        return fail(errors)
    vaults[raw_name] = resource
    save_store(vaults, VAULT_FILE)
    out(render_vault(resource, meshes))
    return 0


def cmd_vault_list(args):
    meshes = load_store()
    vaults = load_store(VAULT_FILE)
    items = []
    for name in sorted(vaults.keys()):
        r = render_vault(vaults[name], meshes)
        items.append({"name": name, "status": r["status"]})
    out(items)
    return 0


def cmd_vault_describe(args):
    name = get_name(args)
    if not name:
        return fail([err("metadata.name", "name is required", "required")])
    vaults = load_store(VAULT_FILE)
    if name not in vaults:
        return fail([err("metadata.name", "vault %r not found" % name, "not_found")])
    out(render_vault(vaults[name], load_store()))
    return 0


def cmd_vault_delete(args):
    name = get_name(args)
    if not name:
        return fail([err("metadata.name", "name is required", "required")])
    vaults = load_store(VAULT_FILE)
    if name not in vaults:
        return fail([err("metadata.name", "vault %r not found" % name, "not_found")])
    del vaults[name]
    save_store(vaults, VAULT_FILE)
    out({"message": "vault %s deleted" % name, "metadata": {"name": name}})
    return 0


VAULT_COMMANDS = {
    "create": cmd_vault_create,
    "list": cmd_vault_list,
    "describe": cmd_vault_describe,
    "get": cmd_vault_describe,
    "delete": cmd_vault_delete,
    "update": cmd_vault_update,
}


COMMANDS = {
    "create": cmd_create,
    "list": cmd_list,
    "describe": cmd_describe,
    "get": cmd_describe,
    "delete": cmd_delete,
    "update": cmd_update,
}


def main(argv):
    table = {"mesh": COMMANDS, "vault": VAULT_COMMANDS}.get(argv[0] if argv else None)
    if table is None or len(argv) < 2 or argv[1] not in table:
        return fail([err("", "usage: meshctl.py <mesh|vault> <create|list|describe|delete|update> [arguments]",
                         "invalid")])
    return table[argv[1]](argv[2:])


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as e:  # never write to stderr
        out({"errors": [err("", "internal error: %s" % e, "invalid")]})
        code = 1
    sys.exit(code)
