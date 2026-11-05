#!/usr/bin/env python3
"""meshctl: manage mesh resources from YAML specs, printing JSON to stdout."""
import json
import os
import re
import sys

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
MEM_RE = re.compile(r"^([0-9]+)(Ki|Mi|Gi|Ti)?$")
CPU_RE = re.compile(r"^([0-9]+)(m)?$")
MEM_MULT = {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}
CPU_MULT = {None: 1000, "m": 1}

# runtime catalog: version -> status (supported | deprecated | skipped)
RUNTIME_CATALOG = {
    "3.0.0": "deprecated",
    "3.1.0": "skipped",
    "3.1.1": "supported",
    "4.0.0": "supported",
    "4.0.1": "supported",
    "4.0.2": "supported",
    "4.1.0": "supported",
    "4.1.1": "supported",
    "4.2.0": "supported",
    "5.0.0": "supported",
    "5.0.1": "supported",
    "5.1.0": "supported",
}
STRATEGIES = ("FullStop", "LiveMigration", "RollingPatch")
STAGES = {
    "FullStop": ["Migrate"],
    "RollingPatch": ["Migrate"],
    "LiveMigration": ["Prepare", "Migrate", "Cleanup"],
}


def version_tuple(v):
    return tuple(int(x) for x in v.split("."))


def runtime_warnings(spec):
    rt = spec.get("runtime")
    if isinstance(rt, str) and RUNTIME_CATALOG.get(rt) == "deprecated":
        return [{"field": "spec.runtime",
                 "message": "runtime version '%s' is deprecated" % rt}]
    return []


def with_warnings(rendered, warnings):
    if warnings:
        rendered["warnings"] = sorted(warnings, key=lambda w: (w["field"], w["message"]))
    return rendered


def out(obj):
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()


def err(field, message, etype):
    return {"field": field, "message": message, "type": etype}


def fail(errors):
    errors = sorted(errors, key=lambda e: (e.get("field", ""), e.get("type", "")))
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


DIGEST_ALGORITHMS = ("SHA-256", "SHA-384", "SHA-512")
ENC_SOURCES = ("None", "Secret", "Service")
CLIENT_MODES = ("None", "Authenticate", "Validate")


def sub_mapping(parent, key, path, errors):
    v = parent.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        errors.append(err(path, "%s must be a mapping" % key, "invalid"))
        return {}
    return v


def is_set(v):
    return v is not None and v != ""


def validate_access(acc_in, errors):
    access = {}
    base = "spec.access"

    # authentication
    auth_in = sub_mapping(acc_in, "authentication", base + ".authentication", errors)
    auth_enabled = True
    en = auth_in.get("enabled")
    if en is not None:
        if not isinstance(en, bool):
            errors.append(err(base + ".authentication.enabled", "enabled must be a boolean", "invalid"))
        else:
            auth_enabled = en
    auth = {"enabled": auth_enabled}
    dig = auth_in.get("digestAlgorithm")
    dpath = base + ".authentication.digestAlgorithm"
    cref = acc_in.get("credentialRef")
    cpath = base + ".credentialRef"
    if not auth_enabled:
        if dig is not None:
            errors.append(err(dpath, "digestAlgorithm is forbidden when authentication is disabled",
                              "forbidden"))
        if cref is not None:
            errors.append(err(cpath, "credentialRef is forbidden when authentication is disabled",
                              "forbidden"))
    else:
        if dig is None:
            auth["digestAlgorithm"] = "SHA-256"
        elif dig not in DIGEST_ALGORITHMS:
            errors.append(err(dpath, "digestAlgorithm must be one of %s, got %r"
                              % (", ".join(DIGEST_ALGORITHMS), dig), "invalid"))
        else:
            auth["digestAlgorithm"] = dig
        if cref is not None:
            if not isinstance(cref, str):
                errors.append(err(cpath, "credentialRef must be a string", "invalid"))
            else:
                access["credentialRef"] = cref
    access["authentication"] = auth

    # encryption
    epath = base + ".encryption"
    enc_in = sub_mapping(acc_in, "encryption", epath, errors)
    enc = {}
    source = enc_in.get("source")
    source_ok = True
    if source is None:
        source = "None"
    elif source not in ENC_SOURCES:
        errors.append(err(epath + ".source", "source must be one of %s, got %r"
                          % (", ".join(ENC_SOURCES), source), "invalid"))
        source_ok = False
    enc["source"] = source
    cert = enc_in.get("certRef")
    svc = enc_in.get("certServiceRef")
    for fname, val in (("certRef", cert), ("certServiceRef", svc)):
        if val is not None and not isinstance(val, str):
            errors.append(err(epath + "." + fname, "%s must be a string" % fname, "invalid"))
    if source_ok:
        need = {"None": None, "Secret": "certRef", "Service": "certServiceRef"}[source]
        for fname, val in (("certRef", cert), ("certServiceRef", svc)):
            if fname == need:
                if not is_set(val):
                    errors.append(err(epath + "." + fname,
                                      "%s is required when source is %r" % (fname, source),
                                      "required"))
                elif isinstance(val, str):
                    enc[fname] = val
            elif val is not None:
                errors.append(err(epath + "." + fname,
                                  "%s is forbidden when source is %r" % (fname, source),
                                  "forbidden"))
    cm = enc_in.get("clientMode")
    if cm is None:
        cm = "None"
        enc["clientMode"] = cm
    elif cm not in CLIENT_MODES:
        errors.append(err(epath + ".clientMode", "clientMode must be one of %s, got %r"
                          % (", ".join(CLIENT_MODES), cm), "invalid"))
    elif source_ok and source == "None" and cm != "None":
        errors.append(err(epath + ".clientMode",
                          "clientMode %r requires an encryption source other than \"None\"" % cm,
                          "invalid"))
    else:
        enc["clientMode"] = cm
    access["encryption"] = enc

    # permissions
    ppath = base + ".permissions"
    perm_in = sub_mapping(acc_in, "permissions", ppath, errors)
    p_enabled = False
    pen = perm_in.get("enabled")
    if pen is not None:
        if not isinstance(pen, bool):
            errors.append(err(ppath + ".enabled", "enabled must be a boolean", "invalid"))
        else:
            p_enabled = pen
    perms = {"enabled": p_enabled}
    if p_enabled:
        rpath = ppath + ".roles"
        roles_in = perm_in.get("roles")
        if roles_in is None or (isinstance(roles_in, list) and len(roles_in) == 0):
            errors.append(err(rpath, "roles is required when permissions are enabled", "required"))
        elif not isinstance(roles_in, list):
            errors.append(err(rpath, "roles must be an array", "invalid"))
        else:
            roles = []
            seen = set()
            dup = []
            for i, role in enumerate(roles_in):
                ip = "%s[%d]" % (rpath, i)
                if not isinstance(role, dict):
                    errors.append(err(ip, "role must be an object with name and permissions", "invalid"))
                    continue
                rname = role.get("name")
                rperms = role.get("permissions")
                ok = True
                if rname is None or rname == "":
                    errors.append(err(ip + ".name", "role name is required", "required"))
                    ok = False
                elif not isinstance(rname, str):
                    errors.append(err(ip + ".name", "role name must be a string", "invalid"))
                    ok = False
                if rperms is None or (isinstance(rperms, list) and len(rperms) == 0):
                    errors.append(err(ip + ".permissions", "role permissions are required", "required"))
                    ok = False
                elif not isinstance(rperms, list) or not all(
                        isinstance(p, str) and p != "" for p in rperms):
                    errors.append(err(ip + ".permissions",
                                      "role permissions must be an array of non-empty strings",
                                      "invalid"))
                    ok = False
                if isinstance(rname, str) and rname != "":
                    if rname in seen and rname not in dup:
                        dup.append(rname)
                    seen.add(rname)
                if ok:
                    roles.append({"name": rname, "permissions": list(rperms)})
            if dup:
                errors.append(err(rpath, "duplicate role name(s): %s" % ", ".join(dup), "duplicate"))
            perms["roles"] = roles
    access["permissions"] = perms
    return access


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
        if not isinstance(rt, str) or rt not in RUNTIME_CATALOG:
            errors.append(err("spec.runtime",
                              "runtime version %s is not supported; must be one of %s"
                              % ("'%s'" % rt if isinstance(rt, str) else repr(rt),
                                 ", ".join(sorted(RUNTIME_CATALOG, key=version_tuple))),
                              "invalid"))
        elif RUNTIME_CATALOG[rt] == "skipped":
            errors.append(err("spec.runtime",
                              "runtime version '%s' is skipped and cannot be targeted" % rt,
                              "invalid"))
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
    if not isinstance(acc_in, dict):
        errors.append(err("spec.access", "access must be a mapping", "invalid"))
        acc_in = {}
    spec["access"] = validate_access(acc_in, errors)

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
            if st not in STRATEGIES:
                errors.append(err("spec.migration.strategy",
                                  "strategy must be one of %s, got %r"
                                  % (", ".join(STRATEGIES), st), "invalid"))
            else:
                strategy = st
    spec["migration"] = {"strategy": strategy}

    # regions (multi-region topology)
    regions = spec_in.get("regions")
    if regions is not None:
        spec["regions"] = regions
        if strategy == "LiveMigration" and regions not in ([], {}, ""):
            errors.append(err("spec.migration.strategy",
                              "LiveMigration strategy is not supported with multi-region topology",
                              "invalid"))

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


def cond_status(status, ctype):
    for c in status.get("conditions", []):
        if c.get("type") == ctype:
            return c.get("status")
    return None


def compute_stable(status):
    return (cond_status(status, "Healthy") == "True"
            and cond_status(status, "PrechecksPassed") == "True"
            and cond_status(status, "GracefulShutdown") in (None, "False")
            and cond_status(status, "Scaling") in (None, "False")
            and cond_status(status, "Migration") in (None, "False"))


def migration_active(status):
    return cond_status(status, "Migration") == "True" and isinstance(status.get("migration"), dict)


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
    stable = compute_stable(st)
    if st.get("stable") != stable or "stable" not in st:
        st["stable"] = stable
        changed = True
    resource["status"] = st
    return changed


def render(resource):
    r = json.loads(json.dumps(resource))
    sto = r.get("spec", {}).get("network", {}).get("storage")
    if isinstance(sto, dict) and sto.get("ephemeral"):
        r["spec"]["network"]["storage"] = {"ephemeral": True}
    st = r.get("status", {})
    order = ["state", "stable", "instances", "conditions", "migration", "desiredInstancesOnResume"]
    if "conditions" in st:
        st["conditions"] = [{"type": c.get("type"), "status": c.get("status"),
                             "message": c.get("message", "")}
                            for c in sorted(st["conditions"], key=lambda c: c.get("type", ""))]
    if isinstance(st.get("migration"), dict):
        m = st["migration"]
        st["migration"] = {k: m[k] for k in ("sourceRuntime", "stage", "targetRuntime") if k in m}
    r["status"] = {k: st[k] for k in order if k in st}
    return r


def reconcile_access(merged, patch):
    """Drop stored access fields that the patch made inapplicable and did not set."""
    acc = merged.get("access")
    pacc = patch.get("access")
    if not isinstance(acc, dict) or not isinstance(pacc, dict):
        return
    def sub(d, k):
        v = d.get(k)
        return v if isinstance(v, dict) else {}
    pauth = sub(pacc, "authentication")
    if pauth.get("enabled") is False:
        auth = acc.get("authentication")
        if isinstance(auth, dict) and "digestAlgorithm" not in pauth:
            auth.pop("digestAlgorithm", None)
        if "credentialRef" not in pacc:
            acc.pop("credentialRef", None)
    penc = sub(pacc, "encryption")
    enc = acc.get("encryption")
    if "source" in penc and isinstance(enc, dict):
        src = penc.get("source")
        for fname, keep in (("certRef", "Secret"), ("certServiceRef", "Service")):
            if src != keep and fname not in penc:
                enc.pop(fname, None)
        if src == "None" and "clientMode" not in penc:
            enc.pop("clientMode", None)


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
    status["stable"] = compute_stable(status)
    resource["status"] = status
    store[name] = resource
    save_store(store)
    out(with_warnings(render(resource), runtime_warnings(resource["spec"])))
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
    reconcile_access(merged_spec, spec_patch)
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

    # runtime version changes / migrations
    status0 = stored["status"]
    old_rt = stored["spec"].get("runtime")
    new_rt = resource["spec"].get("runtime")
    old_strat = stored["spec"].get("migration", {}).get("strategy", "FullStop")
    strat_err = any(e["field"] == "spec.migration.strategy" and e["type"] == "invalid"
                    and "must be one of" in e["message"] for e in errors)
    rt_err = any(e["field"] == "spec.runtime" for e in errors)
    new_strat = resource["spec"]["migration"]["strategy"]
    mig_action = None  # None | "start" | "rollback"
    if migration_active(status0):
        mig = status0["migration"]
        if not strat_err and new_strat != old_strat:
            errors.append(err("spec.migration.strategy",
                              "cannot change migration strategy while a migration is in progress",
                              "invalid"))
        if not rt_err and new_rt != old_rt:
            if old_strat == "LiveMigration" and new_rt == mig.get("sourceRuntime"):
                mig_action = "rollback"
            else:
                errors.append(err("spec.runtime",
                                  "cannot change runtime version while a migration is in progress",
                                  "invalid"))
    elif (not rt_err and old_rt in RUNTIME_CATALOG and new_rt is not None
          and new_rt != old_rt):
        ov, nv_ = version_tuple(old_rt), version_tuple(new_rt)
        if nv_ < ov:
            errors.append(err("spec.runtime",
                              "version downgrade from '%s' to '%s' is not allowed" % (old_rt, new_rt),
                              "invalid"))
        if not strat_err and new_strat == "RollingPatch":
            if nv_[:2] != ov[:2]:
                errors.append(err("spec.runtime",
                                  "RollingPatch strategy requires source and target to share the "
                                  "same major and minor version, got '%s' to '%s'" % (old_rt, new_rt),
                                  "invalid"))
            if nv_[0] < 4:
                errors.append(err("spec.runtime",
                                  "RollingPatch strategy requires target major version 4 or "
                                  "later, got '%s'" % new_rt, "invalid"))
        mig_action = "start"
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
    if mig_action == "start":
        set_condition(status, "Migration", "True", "")
        status["migration"] = {"sourceRuntime": old_rt,
                               "stage": STAGES[new_strat][0],
                               "targetRuntime": new_rt}
    elif mig_action == "rollback":
        clear_condition(status, "Migration")
        status.pop("migration", None)
    status["stable"] = bool(status.get("stable", True)) and compute_stable(status)
    resource["status"] = status
    store[raw_name] = resource
    save_store(store)
    out(with_warnings(render(resource), runtime_warnings(resource["spec"])))
    return 0


def cmd_migrate(args):
    name = get_name(args)
    if not name:
        return fail([err("metadata.name", "name is required", "required")])
    store = load_store()
    if name not in store:
        return fail([err("metadata.name", "mesh %r not found" % name, "not_found")])
    r = store[name]
    settle(r)
    st = r["status"]
    if not migration_active(st):
        return fail([err("status.migration", "no active migration for mesh '%s'" % name, "invalid")])
    mig = st["migration"]
    strat = r["spec"].get("migration", {}).get("strategy", "FullStop")
    stages = STAGES.get(strat, ["Migrate"])
    try:
        idx = stages.index(mig.get("stage"))
    except ValueError:
        idx = len(stages) - 1
    if idx + 1 < len(stages):
        mig["stage"] = stages[idx + 1]
    else:
        clear_condition(st, "Migration")
        st.pop("migration", None)
    st["stable"] = compute_stable(st)
    save_store(store)
    out(render(r))
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


# ---------------------------------------------------------------- one-shot ops

ONESHOT_FILES = {"task": "tasks.json", "snapshot": "snapshots.json", "recovery": "recoveries.json"}
SCOPE_KEYS = ("stores", "blueprints", "tallies", "definitions", "procedures")


def oneshot_name(doc, errors):
    metadata = doc.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(err("metadata", "metadata must be a mapping", "invalid"))
        metadata = {}
    metadata = metadata or {}
    raw_name = metadata.get("name")
    if raw_name is None or raw_name == "":
        errors.append(err("metadata.name", "name is required", "required"))
        return raw_name, None
    if not isinstance(raw_name, str) or not NAME_RE.match(raw_name):
        errors.append(err("metadata.name",
                          "name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
        return raw_name, None
    return raw_name, raw_name


def check_mesh_ref(spec_in, spec, meshes, errors):
    mref = spec_in.get("meshRef")
    if mref is None or mref == "":
        errors.append(err("spec.meshRef", "meshRef is required and must reference an existing mesh",
                          "invalid"))
        return None
    if not isinstance(mref, str):
        errors.append(err("spec.meshRef", "meshRef must be a string", "invalid"))
        return None
    spec["meshRef"] = mref
    if mref not in meshes:
        errors.append(err("spec.meshRef", "mesh %r does not exist" % mref, "invalid"))
        return None
    return mref


def validate_scope(spec_in, spec, errors):
    scope = spec_in.get("scope")
    if scope is None:
        return
    if not isinstance(scope, dict):
        errors.append(err("spec.scope", "scope must be a mapping", "invalid"))
        return
    out_scope = {}
    for k, v in scope.items():
        p = "spec.scope.%s" % k
        if k not in SCOPE_KEYS:
            errors.append(err(p, "unknown scope key %r; allowed: %s" % (k, ", ".join(SCOPE_KEYS)),
                              "invalid"))
            continue
        if v is None:
            continue
        if not isinstance(v, list) or not all(isinstance(x, str) and x != "" for x in v):
            errors.append(err(p, "%s must be an array of non-empty strings" % k, "invalid"))
            continue
        out_scope[k] = list(v)
    spec["scope"] = out_scope


def validate_oneshot_resources(spec_in, spec, errors):
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


def validate_task_spec(spec_in, spec, errors):
    present = []
    empty = False
    for f in ("inline", "bundleRef"):
        v = spec_in.get(f)
        if v is None:
            continue
        if not isinstance(v, str):
            errors.append(err("spec." + f, "%s must be a string" % f, "invalid"))
            continue
        if v.strip() == "":
            empty = True
            continue
        present.append(f)
        spec[f] = v
    if len(present) != 1 or empty:
        errors.append(err("spec", "exactly one of 'spec.inline' or 'spec.bundleRef' must be set",
                          "invalid"))


def validate_snapshot_spec(spec_in, spec, errors):
    sto = spec_in.get("storage")
    if sto is not None:
        if not isinstance(sto, dict):
            errors.append(err("spec.storage", "storage must be a mapping", "invalid"))
        else:
            storage = {}
            size = sto.get("size")
            if size is not None:
                if parse_quantity(size, MEM_RE, MEM_MULT) is None:
                    errors.append(err("spec.storage.size",
                                      "invalid storage size quantity: %r" % (size,), "invalid"))
                else:
                    storage["size"] = as_qstr(size)
            cn = sto.get("className")
            if cn is not None:
                if not isinstance(cn, str) or cn == "":
                    errors.append(err("spec.storage.className",
                                      "className must be a non-empty string", "invalid"))
                else:
                    storage["className"] = cn
            spec["storage"] = storage
    validate_scope(spec_in, spec, errors)
    validate_oneshot_resources(spec_in, spec, errors)


def validate_recovery_spec(spec_in, spec, errors, snapshots):
    sref = spec_in.get("snapshotRef")
    if sref is None or sref == "":
        errors.append(err("spec.snapshotRef",
                          "snapshotRef is required and must reference an existing snapshot",
                          "invalid"))
    elif not isinstance(sref, str):
        errors.append(err("spec.snapshotRef", "snapshotRef must be a string", "invalid"))
    else:
        spec["snapshotRef"] = sref
        snap = snapshots.get(sref)
        if snap is None:
            errors.append(err("spec.snapshotRef", "snapshot %r does not exist" % sref, "invalid"))
        else:
            smesh = snap.get("spec", {}).get("meshRef")
            rmesh = spec_in.get("meshRef")
            if isinstance(rmesh, str) and rmesh != "" and smesh != rmesh:
                errors.append(err("spec.snapshotRef",
                                  "snapshot '%s' belongs to mesh '%s', not '%s'"
                                  % (sref, smesh, rmesh), "invalid"))
    validate_scope(spec_in, spec, errors)
    validate_oneshot_resources(spec_in, spec, errors)


def validate_oneshot(kind, doc, meshes, snapshots):
    errors = []
    raw_name, name = oneshot_name(doc, errors)
    spec_in = doc.get("spec")
    if spec_in is None:
        spec_in = {}
    if not isinstance(spec_in, dict):
        errors.append(err("spec", "spec must be a mapping", "invalid"))
        spec_in = {}
    spec = {}
    check_mesh_ref(spec_in, spec, meshes, errors)
    if kind == "task":
        validate_task_spec(spec_in, spec, errors)
    elif kind == "snapshot":
        validate_snapshot_spec(spec_in, spec, errors)
    else:
        validate_recovery_spec(spec_in, spec, errors, snapshots)
    return {"metadata": {"name": raw_name}, "spec": spec}, name, errors


def render_oneshot(resource):
    r = json.loads(json.dumps(resource))
    st = r.get("status", {})
    status = {"state": st.get("state", "Initializing")}
    if status["state"] in ("Failed", "Unknown") and st.get("detail"):
        status["detail"] = st["detail"]
    if status["state"] == "Succeeded" and st.get("storageRef"):
        status["storageRef"] = st["storageRef"]
    return {"metadata": r.get("metadata", {}), "spec": r.get("spec", {}), "status": status}


def make_oneshot_commands(kind):
    fname = ONESHOT_FILES[kind]

    def lookup(args):
        name = get_name(args)
        if not name:
            return None, None, fail([err("metadata.name", "name is required", "required")])
        store = load_store(fname)
        if name not in store:
            return None, None, fail([err("metadata.name", "%s %r not found" % (kind, name),
                                         "not_found")])
        return name, store, None

    def create(args):
        doc, code = read_doc(args)
        if code is not None:
            return code
        meshes = load_store()
        snapshots = load_store(ONESHOT_FILES["snapshot"])
        store = load_store(fname)
        resource, name, errors = validate_oneshot(kind, doc, meshes, snapshots)
        if name is not None and name in store:
            errors.append(err("metadata.name", "%s %r already exists" % (kind, name), "duplicate"))
        if errors:
            return fail(errors)
        resource["status"] = {"state": "Initializing"}
        store[name] = resource
        save_store(store, fname)
        out(render_oneshot(resource))
        return 0

    def update(args):
        doc, code = read_doc(args)
        if code is not None:
            return code
        errors = []
        raw_name, name = oneshot_name(doc, errors)
        if errors:
            return fail(errors)
        store = load_store(fname)
        if name not in store:
            return fail([err("metadata.name", "%s %r not found" % (kind, name), "not_found")])
        stored = store[name]
        patch = doc.get("spec")
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            return fail([err("spec", "spec must be a mapping", "invalid")])
        diffs = []
        spec_diff(stored.get("spec", {}), patch, "spec", diffs)
        if diffs:
            return fail([err(p, "field '%s' is immutable after creation" % p, "immutable")
                         for p in diffs])
        out(render_oneshot(stored))
        return 0

    def list_(args):
        store = load_store(fname)
        items = []
        for n in sorted(store.keys()):
            r = render_oneshot(store[n])
            items.append({"name": n, "status": r["status"]})
        out(items)
        return 0

    def describe(args):
        name, store, code = lookup(args)
        if code is not None:
            return code
        out(render_oneshot(store[name]))
        return 0

    def delete(args):
        name, store, code = lookup(args)
        if code is not None:
            return code
        if kind == "snapshot":
            recs = load_store(ONESHOT_FILES["recovery"])
            deps = sorted(r for r, v in recs.items()
                          if v.get("spec", {}).get("snapshotRef") == name)
            if deps:
                return fail([err("metadata.name",
                                 "snapshot %r cannot be deleted: referenced by recovery(s) %s"
                                 % (name, ", ".join(deps)), "conflict")])
        del store[name]
        save_store(store, fname)
        out({"message": "%s %s deleted" % (kind, name), "metadata": {"name": name}})
        return 0

    def run(args):
        name, store, code = lookup(args)
        if code is not None:
            return code
        r = store[name]
        st = r.get("status", {})
        cur = st.get("state", "Initializing")
        if cur != "Initializing":
            return fail([err("status.state",
                             "resource is in state '%s', expected 'Initializing'" % cur,
                             "invalid")])
        # Running -> terminal
        r["status"] = {"state": "Running"}
        r["status"] = execute_oneshot(kind, name, r)
        store[name] = r
        save_store(store, fname)
        out(render_oneshot(r))
        return 0

    return {"create": create, "list": list_, "describe": describe, "get": describe,
            "delete": delete, "update": update, "run": run}


def _norm(v):
    if is_int(v):
        return str(v)
    return v


def spec_diff(stored, patch, path, diffs):
    for k, v in patch.items():
        p = "%s.%s" % (path, k)
        if v is None:
            continue
        if k not in stored:
            diffs.append(p)
        elif isinstance(v, dict) and isinstance(stored[k], dict):
            spec_diff(stored[k], v, p, diffs)
        elif isinstance(v, list) and isinstance(stored[k], list):
            if [_norm(x) for x in v] != [_norm(x) for x in stored[k]]:
                diffs.append(p)
        elif _norm(v) != _norm(stored[k]):
            diffs.append(p)


def execute_oneshot(kind, name, r):
    spec = r.get("spec", {})
    mref = spec.get("meshRef")
    if kind == "task":
        inline = spec.get("inline")
        if inline is not None:
            for i, line in enumerate(inline.splitlines()):
                cmd = line.strip()
                if cmd.startswith("FAIL:"):
                    reason = cmd[len("FAIL:"):].strip()
                    return {"state": "Failed",
                            "detail": "command %d failed: %s" % (i, reason)}
        return {"state": "Succeeded"}
    meshes = load_store()
    mesh = meshes.get(mref)
    if mesh is None:
        return {"state": "Failed", "detail": "mesh '%s' not found" % mref}
    if mesh.get("status", {}).get("stable", True) is False:
        return {"state": "Unknown",
                "detail": "mesh '%s' is not stable; outcome could not be determined" % mref}
    if kind == "snapshot":
        return {"state": "Succeeded", "storageRef": "snapshots/%s/%s" % (mref, name)}
    return {"state": "Succeeded"}


COMMANDS = {
    "create": cmd_create,
    "list": cmd_list,
    "describe": cmd_describe,
    "get": cmd_describe,
    "delete": cmd_delete,
    "update": cmd_update,
    "migrate": cmd_migrate,
}


def main(argv):
    table = {"mesh": COMMANDS, "vault": VAULT_COMMANDS,
             "task": make_oneshot_commands("task"),
             "snapshot": make_oneshot_commands("snapshot"),
             "recovery": make_oneshot_commands("recovery")}.get(argv[0] if argv else None)
    if table is None or len(argv) < 2 or argv[1] not in table:
        return fail([err("", "usage: meshctl.py <mesh|vault|task|snapshot|recovery> <create|list|describe|delete|update|run> [arguments]",
                         "invalid")])
    return table[argv[1]](argv[2:])


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as e:  # never write to stderr
        out({"errors": [err("", "internal error: %s" % e, "invalid")]})
        code = 1
    sys.exit(code)
