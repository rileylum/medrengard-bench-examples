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
            vaults = data.pop(VAULT_KEY, None)
            VAULTS.clear()
            if isinstance(vaults, dict):
                VAULTS.update(vaults)
            return data
    except Exception:
        pass
    return {}


VAULT_KEY = "__vaults__"
VAULTS = {}


def save_state(state):
    state = dict(state)
    state.pop(VAULT_KEY, None)
    if VAULTS:
        state[VAULT_KEY] = VAULTS
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
    errors = sorted(errors, key=lambda e: (str(e.get("field", "")), str(e.get("type", ""))))
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

    validate_access(spec_in, spec, errors)

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


DIGESTS = ("SHA-256", "SHA-384", "SHA-512")
ENC_SOURCES = ("None", "Secret", "Service")
CLIENT_MODES = ("None", "Authenticate", "Validate")


def _sub_obj(parent, key, path, errors):
    v = parent.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        errors.append(err(path, "%s must be an object" % path, "invalid"))
        return None
    return dict(v)


def validate_access(spec_in, spec, errors):
    access_in = spec_in.get("access")
    if access_in is not None and not isinstance(access_in, dict):
        errors.append(err("spec.access", "spec.access must be an object", "invalid"))
        return
    access = dict(access_in or {})
    base = "spec.access"

    # authentication
    auth = _sub_obj(access, "authentication", base + ".authentication", errors)
    auth_enabled = True
    if auth is not None:
        enabled = auth.get("enabled")
        if enabled is None:
            auth["enabled"] = True
        elif not isinstance(enabled, bool):
            errors.append(err(base + ".authentication.enabled",
                              "spec.access.authentication.enabled must be a boolean", "invalid"))
        else:
            auth_enabled = enabled
        dg = auth.get("digestAlgorithm")
        if auth_enabled:
            if dg is None:
                auth["digestAlgorithm"] = "SHA-256"
            elif dg not in DIGESTS:
                errors.append(err(base + ".authentication.digestAlgorithm",
                                  "spec.access.authentication.digestAlgorithm must be one of "
                                  "\"SHA-256\", \"SHA-384\", \"SHA-512\", got %r" % (dg,), "invalid"))
        else:
            if dg is not None:
                errors.append(err(base + ".authentication.digestAlgorithm",
                                  "spec.access.authentication.digestAlgorithm is forbidden when "
                                  "authentication is disabled", "forbidden"))
            auth = {"enabled": False}
        access["authentication"] = auth
    if "credentialRef" in access:
        if access["credentialRef"] is None:
            del access["credentialRef"]
        elif not auth_enabled:
            errors.append(err(base + ".credentialRef",
                              "spec.access.credentialRef is forbidden when authentication is disabled",
                              "forbidden"))

    # permissions
    perm = _sub_obj(access, "permissions", base + ".permissions", errors)
    if perm is not None:
        pb = base + ".permissions"
        enabled = perm.get("enabled")
        p_enabled = False
        if enabled is None:
            perm["enabled"] = False
        elif not isinstance(enabled, bool):
            errors.append(err(pb + ".enabled", "spec.access.permissions.enabled must be a boolean",
                              "invalid"))
        else:
            p_enabled = enabled
        if p_enabled:
            roles = perm.get("roles")
            if roles is None or (isinstance(roles, list) and len(roles) == 0):
                errors.append(err(pb + ".roles",
                                  "spec.access.permissions.roles is required when permissions are enabled",
                                  "required"))
            elif not isinstance(roles, list):
                errors.append(err(pb + ".roles", "spec.access.permissions.roles must be an array",
                                  "invalid"))
            else:
                seen = {}
                dups = []
                for i, role in enumerate(roles):
                    rp = "%s.roles[%d]" % (pb, i)
                    if not isinstance(role, dict):
                        errors.append(err(rp, "role must be an object", "invalid"))
                        continue
                    nm = role.get("name")
                    if is_empty(nm):
                        errors.append(err(rp + ".name", "role name is required", "required"))
                    elif not isinstance(nm, str):
                        errors.append(err(rp + ".name", "role name must be a string", "invalid"))
                    else:
                        if nm in seen:
                            if nm not in dups:
                                dups.append(nm)
                        else:
                            seen[nm] = i
                    ps = role.get("permissions")
                    if ps is None or (isinstance(ps, list) and len(ps) == 0):
                        errors.append(err(rp + ".permissions", "role permissions are required",
                                          "required"))
                    elif not isinstance(ps, list) or not all(isinstance(p, str) for p in ps):
                        errors.append(err(rp + ".permissions",
                                          "role permissions must be an array of strings", "invalid"))
                for nm in dups:
                    errors.append(err(pb + ".roles", "duplicate role name %r" % nm, "duplicate"))
        else:
            perm.pop("roles", None)
        access["permissions"] = perm

    # encryption
    enc = _sub_obj(access, "encryption", base + ".encryption", errors)
    if enc is not None:
        eb = base + ".encryption"
        for k in ("certRef", "certServiceRef"):
            if k in enc and enc[k] is None:
                del enc[k]
        src = enc.get("source")
        if src is None:
            src = enc["source"] = "None"
        cm = enc.get("clientMode")
        if cm is None:
            cm = enc["clientMode"] = "None"
        src_ok = src in ENC_SOURCES
        if not src_ok:
            errors.append(err(eb + ".source", "spec.access.encryption.source must be one of "
                              "\"None\", \"Secret\", \"Service\", got %r" % (src,), "invalid"))
        if cm not in CLIENT_MODES:
            errors.append(err(eb + ".clientMode", "spec.access.encryption.clientMode must be one of "
                              "\"None\", \"Authenticate\", \"Validate\", got %r" % (cm,), "invalid"))
        elif src == "None" and cm != "None":
            errors.append(err(eb + ".clientMode",
                              "spec.access.encryption.clientMode must be \"None\" when source is \"None\"",
                              "invalid"))
        if src_ok:
            rules = {"None": (), "Secret": ("certRef",), "Service": ("certServiceRef",)}[src]
            for k in ("certRef", "certServiceRef"):
                present = k in enc
                if k in rules:
                    if not present or is_empty(enc[k]):
                        errors.append(err("%s.%s" % (eb, k), "spec.access.encryption.%s is required "
                                          "when source is %r" % (k, src), "required"))
                elif present:
                    errors.append(err("%s.%s" % (eb, k), "spec.access.encryption.%s is forbidden "
                                      "when source is %r" % (k, src), "forbidden"))
        access["encryption"] = enc
    spec["access"] = access


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


def adjust_access_merge(merged_spec, spec_upd):
    upd = spec_upd.get("access")
    acc = merged_spec.get("access")
    if not isinstance(upd, dict) or not isinstance(acc, dict):
        return
    ua = upd.get("authentication")
    ma = acc.get("authentication")
    if isinstance(ua, dict) and isinstance(ma, dict) and ua.get("enabled") is False:
        if ua.get("digestAlgorithm") is None:
            ma.pop("digestAlgorithm", None)
        if upd.get("credentialRef") is None:
            acc.pop("credentialRef", None)
    ue = upd.get("encryption")
    me = acc.get("encryption")
    if isinstance(ue, dict) and isinstance(me, dict) and ue.get("source") is not None:
        for k in ("certRef", "certServiceRef"):
            if ue.get(k) is None:
                me.pop(k, None)
        if ue.get("clientMode") is None and ue.get("source") == "None":
            me.pop("clientMode", None)


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
    adjust_access_merge(merged_spec, spec_upd)
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
    deps = sorted(vn for vn, v in VAULTS.items() if (v.get("spec") or {}).get("meshRef") == name)
    if deps:
        fail([err("metadata.name", "mesh %r cannot be deleted: referenced by vaults: %s"
                  % (name, ", ".join(deps)), "conflict")])
    del state[name]
    save_state(state)
    out({"message": "mesh %s deleted" % name, "metadata": {"name": name}})


VAULT_POLICIES = ("retain", "recreate")
VAULT_STR_FIELDS = ("meshRef", "vaultName", "updatePolicy", "template", "templateRef")


def vault_status(vault, meshes):
    parent = meshes.get((vault.get("spec") or {}).get("meshRef"))
    stable = isinstance(parent, dict) and (parent.get("status") or {}).get("stable") is True
    ready = "True" if stable else "False"
    return {"conditions": [cond("Ready", ready, "")], "state": "Ready" if stable else "Pending"}


def vault_render(vault, meshes):
    r = {"metadata": copy.deepcopy(vault.get("metadata") or {}),
         "spec": copy.deepcopy(vault.get("spec") or {})}
    r["status"] = vault_status(vault, meshes)
    return r


def vault_validate(doc, meshes, mode, stored=None):
    errors = []
    if not isinstance(doc, dict):
        fail([err("", "document must be a YAML mapping", "parse")])
    md_in = doc.get("metadata")
    if md_in is None:
        md_in = {}
    if not isinstance(md_in, dict):
        errors.append(err("metadata", "metadata must be an object", "invalid"))
        md_in = {}
    name = md_in.get("name")
    name_ok = False
    if name is None or name == "":
        errors.append(err("metadata.name", "metadata.name is required", "required"))
    elif not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(err("metadata.name",
                          "metadata.name must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", "invalid"))
    else:
        name_ok = True
    if mode == "update" and name_ok and name not in VAULTS:
        fail([err("metadata.name", "vault %r not found" % name, "not_found")])

    spec_in = doc.get("spec")
    if spec_in is None:
        spec_in = {}
    if not isinstance(spec_in, dict):
        errors.append(err("spec", "spec must be an object", "invalid"))
        spec_in = {}
    if stored is not None:
        old_spec = stored.get("spec") or {}
        for f in ("meshRef", "vaultName"):
            v = spec_in.get(f)
            if v is not None and v != old_spec.get(f):
                errors.append(err("spec.%s" % f, "field 'spec.%s' is immutable after creation" % f,
                                  "immutable"))
        spec = deep_merge(old_spec, spec_in)
        for k, v in spec_in.items():
            if v is None and k not in ("meshRef", "vaultName"):
                spec.pop(k, None)
        for f in ("meshRef", "vaultName"):
            if f in old_spec:
                spec[f] = old_spec[f]
    else:
        spec = {k: copy.deepcopy(v) for k, v in spec_in.items() if v is not None}

    bad = set()
    for f in VAULT_STR_FIELDS:
        v = spec.get(f)
        if v is not None and not isinstance(v, str):
            errors.append(err("spec.%s" % f, "spec.%s must be a string" % f, "invalid"))
            bad.add(f)

    mesh_ref = spec.get("meshRef")
    if mesh_ref is None or mesh_ref == "":
        errors.append(err("spec.meshRef", "spec.meshRef is required", "required"))
        mesh_ref = None
    elif "meshRef" not in bad and mesh_ref not in meshes:
        errors.append(err("spec.meshRef", "referenced mesh %r does not exist" % mesh_ref, "invalid"))

    if spec.get("vaultName") is None or spec.get("vaultName") == "":
        if name_ok:
            spec["vaultName"] = name
        elif "vaultName" in spec:
            del spec["vaultName"]
    if spec.get("updatePolicy") is None:
        spec["updatePolicy"] = "retain"
    elif "updatePolicy" not in bad and spec["updatePolicy"] not in VAULT_POLICIES:
        errors.append(err("spec.updatePolicy",
                          "spec.updatePolicy must be one of \"retain\", \"recreate\", got %r"
                          % (spec["updatePolicy"],), "invalid"))
    if spec.get("template") is not None and spec.get("templateRef") is not None:
        errors.append(err("spec.template",
                          "spec.template and spec.templateRef are mutually exclusive", "invalid"))

    if mode == "create":
        if name_ok and name in VAULTS:
            errors.append(err("metadata.name", "vault %r already exists" % name, "duplicate"))
        vn = spec.get("vaultName")
        if mesh_ref is not None and isinstance(vn, str):
            for other_name, other in VAULTS.items():
                os_ = other.get("spec") or {}
                if os_.get("meshRef") == mesh_ref and os_.get("vaultName") == vn:
                    errors.append(err("spec.vaultName",
                                      "vault %r already exists in mesh %r (vault %s)"
                                      % (vn, mesh_ref, other_name), "duplicate"))
                    break
    if errors:
        fail(errors)
    md = deep_merge((stored or {}).get("metadata") or {}, md_in)
    md["name"] = name
    return {"metadata": md, "spec": spec}


def vault_create(args):
    doc = read_doc(args)
    meshes = load_state()
    vault = vault_validate(doc, meshes, "create")
    VAULTS[vault["metadata"]["name"]] = vault
    save_state(meshes)
    out(vault_render(vault, meshes))


def vault_update(args):
    doc = read_doc(args)
    meshes = load_state()
    md = doc.get("metadata") if isinstance(doc, dict) else None
    name = md.get("name") if isinstance(md, dict) else None
    stored = VAULTS.get(name) if isinstance(name, str) else None
    if stored is None and isinstance(name, str) and name != "":
        fail([err("metadata.name", "vault %r not found" % name, "not_found")])
    vault = vault_validate(doc, meshes, "update", stored)
    VAULTS[name] = vault
    save_state(meshes)
    out(vault_render(vault, meshes))


def vault_list(args):
    meshes = load_state()
    items = [{"name": n, "status": {"state": vault_status(v, meshes)["state"]}}
             for n, v in VAULTS.items()]
    items.sort(key=lambda x: x["name"])
    out(items)


def vault_need(args):
    names = [a for a in args if not a.startswith("-")]
    if not names:
        fail([err("metadata.name", "vault name is required", "required")])
    meshes = load_state()
    if names[0] not in VAULTS:
        fail([err("metadata.name", "vault %r not found" % names[0], "not_found")])
    return names[0], meshes


def vault_describe(args):
    name, meshes = vault_need(args)
    out(vault_render(VAULTS[name], meshes))


def vault_delete(args):
    name, meshes = vault_need(args)
    del VAULTS[name]
    save_state(meshes)
    out({"message": "vault %s deleted" % name, "metadata": {"name": name}})


def main(argv):
    if len(argv) < 2 or argv[0] not in ("mesh", "vault"):
        fail([err("", "usage: meshctl.py <mesh|vault> <create|update|list|describe|delete> [arguments]",
                  "invalid")])
    op, rest = argv[1], argv[2:]
    if argv[0] == "vault":
        handlers = {"create": vault_create, "update": vault_update, "list": vault_list,
                    "describe": vault_describe, "delete": vault_delete}
    else:
        handlers = {"create": cmd_create, "update": cmd_update, "list": cmd_list,
                    "describe": cmd_describe, "delete": cmd_delete}
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
