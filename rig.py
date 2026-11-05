#!/usr/bin/env python3
"""rig.py - dry-run system provisioning planner."""

import argparse
import json
import os
import sys

import yaml

CONFIG_EXTS = (".yaml", ".yml", ".json")
OS_NAMES = ("macos", "linux")
PACKAGE_MANAGERS = ("brew", "apt", "yum")
ACTION_TYPES = ("link", "copy", "run")
SCHEMAS = ("module", "preferences", "dock", "environments", "manifest")
VALUE_TYPES = ("bool", "int", "string", "float")
OS_VERSIONS = ("yosemite", "el_capitan", "sierra", "high_sierra", "mojave",
               "catalina", "big_sur", "monterey", "ventura", "sonoma")
_MISSING = object()


# ---------------------------------------------------------------- discovery

def discover(root):
    """Return list of (relpath, data, parse_error) for rig config candidates.

    Only files that fail to parse or have a top-level `version` are returned.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fname in sorted(filenames):
            if not fname.lower().endswith(CONFIG_EXTS):
                continue
            full = os.path.join(dirpath, fname)
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    text = fh.read()
                if fname.lower().endswith(".json"):
                    data = json.loads(text)
                else:
                    data = yaml.safe_load(text)
            except Exception as exc:  # parse/decoding errors
                msg = str(exc).strip().replace("\n", " ")
                found.append((rel, None, "parse error: %s" % msg))
                continue
            if isinstance(data, dict) and "version" in data:
                found.append((rel, data, None))
    found.sort(key=lambda t: t[0])
    return found


def module_name_of(rel):
    d = os.path.dirname(rel)
    return d.replace(os.sep, "/")


# --------------------------------------------------------------- validation

def _is_str(v):
    return isinstance(v, str)


def _fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    return str(v)


def validate_config(data):
    errors = []

    # 2. version
    version = data.get("version")
    if not _is_str(version):
        errors.append("version: must be a string")
    elif version != "1":
        errors.append("unsupported version '%s'" % version)

    # 3. schema
    schema = data.get("schema", _MISSING)
    if schema is _MISSING:
        errors.append("schema: required")
    elif not _is_str(schema):
        errors.append("schema: must be a string")
    elif schema not in SCHEMAS:
        errors.append("schema: unrecognized schema '%s'" % schema)

    errors.extend(validate_depends_on(data))

    if schema == "preferences":
        errors.extend(validate_preferences(data))
        return errors
    if schema == "dock":
        errors.extend(validate_dock(data))
        return errors
    if schema == "environments":
        errors.extend(validate_environments(data))
        return errors
    if schema == "manifest":
        errors.extend(validate_manifest(data))
        return errors

    # 4-8. os
    osv = data.get("os", _MISSING)
    if osv is not _MISSING:
        if not isinstance(osv, dict):
            errors.append("os: must be an object")
        else:
            name = osv.get("name", _MISSING)
            if name is _MISSING:
                errors.append("os.name: required")
            elif not _is_str(name):
                errors.append("os.name: must be a string")
            elif name not in OS_NAMES:
                errors.append("os.name: unsupported value '%s'" % name)

            pm = osv.get("package_manager", _MISSING)
            needs_pm = "packages" in osv or "applications" in osv
            if pm is _MISSING:
                if needs_pm:
                    errors.append(
                        "os.package_manager: required when packages or "
                        "applications are present")
            elif not _is_str(pm):
                errors.append("os.package_manager: must be a string")
            elif pm not in PACKAGE_MANAGERS:
                errors.append(
                    "os.package_manager: unsupported value '%s'" % pm)

            for key in ("packages", "applications"):
                lst = osv.get(key, _MISSING)
                if lst is _MISSING:
                    continue
                if not isinstance(lst, list):
                    errors.append("os.%s: must be a list" % key)
                    continue
                for i, item in enumerate(lst):
                    if not _is_str(item):
                        errors.append("os.%s[%d]: must be a string" % (key, i))
                    elif item == "":
                        errors.append("os.%s[%d]: must not be empty" % (key, i))

    # 9-13. actions
    actions = data.get("actions", _MISSING)
    if actions is not _MISSING:
        if not isinstance(actions, list):
            errors.append("actions: must be a list")
        else:
            item_errs = []
            objs = []
            for i, act in enumerate(actions):
                if not isinstance(act, dict):
                    item_errs.append("actions[%d]: must be an object" % i)
                else:
                    objs.append((i, act))
            errors.extend(item_errs)
            # type
            for i, act in objs:
                t = act.get("type", _MISSING)
                if t is _MISSING:
                    errors.append("actions[%d].type: required" % i)
                elif not _is_str(t):
                    errors.append("actions[%d].type: must be a string" % i)
                elif t not in ACTION_TYPES:
                    errors.append(
                        "actions[%d].type: unknown action type '%s'" % (i, t))
            # source
            for i, act in objs:
                s = act.get("source", _MISSING)
                if s is _MISSING:
                    errors.append("actions[%d].source: required" % i)
                elif not _is_str(s):
                    errors.append("actions[%d].source: must be a string" % i)
                elif s == "":
                    errors.append("actions[%d].source: must not be empty" % i)
                elif s != "*" and s.startswith("/"):
                    errors.append(
                        "actions[%d].source: must be relative to the module "
                        "directory, got '%s'" % (i, s))
            # destination
            for i, act in objs:
                t = act.get("type")
                d = act.get("destination", _MISSING)
                if d is _MISSING:
                    if t in ("link", "copy"):
                        errors.append("actions[%d].destination: required" % i)
                    continue
                if not _is_str(d):
                    errors.append(
                        "actions[%d].destination: must be a string" % i)
                    continue
                if not (d.startswith("/") or d.startswith("~")):
                    errors.append(
                        "actions[%d].destination: must be absolute or start "
                        "with '~', got '%s'" % (i, d))
                    continue
                if act.get("source") == "*" and not d.endswith("/"):
                    errors.append(
                        "actions[%d].destination: must end with '/' for "
                        "wildcard source" % i)
            # hidden / elevated
            for i, act in objs:
                for key in ("hidden", "elevated"):
                    v = act.get(key, _MISSING)
                    if v is not _MISSING and not isinstance(v, bool):
                        errors.append(
                            "actions[%d].%s: must be a boolean" % (i, key))
    return errors


def validate_depends_on(data):
    errors = []
    deps = data.get("depends_on", _MISSING)
    if deps is _MISSING:
        return errors
    if not isinstance(deps, list):
        return ["depends_on: must be a list"]
    for i, d in enumerate(deps):
        if not _is_str(d):
            errors.append("depends_on[%d]: must be a string" % i)
        elif d == "":
            errors.append("depends_on[%d]: must not be empty" % i)
    return errors


def _check_str(obj, key, pre, errors):
    v = obj.get(key, _MISSING)
    if v is _MISSING:
        errors.append("%s.%s: required" % (pre, key))
    elif not _is_str(v):
        errors.append("%s.%s: must be a string" % (pre, key))
    elif v == "":
        errors.append("%s.%s: must not be empty" % (pre, key))
    else:
        return True
    return False


def validate_environments(data):
    errors = []
    envs = data.get("environments", _MISSING)
    if envs is _MISSING:
        return ["environments: required"]
    if not isinstance(envs, list) or not envs:
        return ["environments: must be a non-empty list"]
    for n, env in enumerate(envs):
        pre = "environments[%d]" % n
        if not isinstance(env, dict):
            errors.append("%s: must be an object" % pre)
            continue
        _check_str(env, "language", pre, errors)
        versions = env.get("versions", _MISSING)
        valid_versions = None
        if versions is _MISSING:
            errors.append("%s.versions: required" % pre)
        elif not isinstance(versions, list) or not versions:
            errors.append("%s.versions: must be a non-empty list" % pre)
        else:
            for i, v in enumerate(versions):
                if not _is_str(v):
                    errors.append("%s.versions[%d]: must be a string"
                                  % (pre, i))
                elif v == "":
                    errors.append("%s.versions[%d]: must not be empty"
                                  % (pre, i))
            valid_versions = versions
        mgr = env.get("manager", _MISSING)
        if mgr is _MISSING:
            errors.append("%s.manager: required" % pre)
            continue
        if not isinstance(mgr, dict):
            errors.append("%s.manager: must be an object" % pre)
            continue
        mpre = pre + ".manager"
        _check_str(mgr, "name", mpre, errors)
        plugins = mgr.get("plugins", _MISSING)
        if plugins is _MISSING or plugins is None:
            continue
        if not isinstance(plugins, list):
            errors.append("%s.plugins: must be a list" % mpre)
            continue
        for m, plug in enumerate(plugins):
            ppre = "%s.plugins[%d]" % (mpre, m)
            if not isinstance(plug, dict):
                errors.append("%s: must be an object" % ppre)
                continue
            _check_str(plug, "name", ppre, errors)
            venvs = plug.get("virtual_environments", _MISSING)
            if venvs is _MISSING or venvs is None:
                continue
            if not isinstance(venvs, list):
                errors.append("%s.virtual_environments: must be a list"
                              % ppre)
                continue
            for k, ve in enumerate(venvs):
                vpre = "%s.virtual_environments[%d]" % (ppre, k)
                if not isinstance(ve, dict):
                    errors.append("%s: must be an object" % vpre)
                    continue
                if _check_str(ve, "version", vpre, errors):
                    if (valid_versions is not None
                            and ve["version"] not in valid_versions):
                        errors.append(
                            "%s.version: '%s' is not in the environment's "
                            "versions list" % (vpre, ve["version"]))
                _check_str(ve, "name", vpre, errors)
    return errors


def validate_manifest(data):
    errors = []
    profiles = data.get("profiles", _MISSING)
    if profiles is _MISSING:
        return ["profiles: required"]
    if not isinstance(profiles, dict) or not profiles:
        return ["profiles: must be a non-empty object"]
    for pname in profiles:
        prof = profiles[pname]
        pre = "profiles.%s" % pname
        if not isinstance(prof, dict):
            errors.append("%s: must be an object" % pre)
            continue
        mods = prof.get("modules", _MISSING)
        if mods is _MISSING:
            errors.append("%s.modules: required" % pre)
        elif not isinstance(mods, list) or not mods:
            errors.append("%s.modules: must be a non-empty list" % pre)
        else:
            for i, m in enumerate(mods):
                if not _is_str(m):
                    errors.append("%s.modules[%d]: must be a string"
                                  % (pre, i))
                elif m == "":
                    errors.append("%s.modules[%d]: must not be empty"
                                  % (pre, i))
        ext = prof.get("extends", _MISSING)
        if ext is _MISSING:
            continue
        if not _is_str(ext):
            errors.append("%s.extends: must be a string" % pre)
        elif ext not in profiles:
            errors.append("%s.extends: unknown profile '%s'" % (pre, ext))
    return errors


def validate_preferences(data):
    errors = []
    prefs = data.get("preferences", _MISSING)
    if prefs is _MISSING:
        return ["preferences: required"]
    if not isinstance(prefs, list):
        return ["preferences: must be a list"]
    for i, p in enumerate(prefs):
        pre = "preferences[%d]" % i
        if not isinstance(p, dict):
            errors.append("%s: must be an object" % pre)
            continue
        for key in ("name", "domain", "key"):
            v = p.get(key, _MISSING)
            if v is _MISSING:
                errors.append("%s.%s: required" % (pre, key))
            elif not _is_str(v):
                errors.append("%s.%s: must be a string" % (pre, key))
            elif v == "":
                errors.append("%s.%s: must not be empty" % (pre, key))
        if "value" not in p:
            errors.append("%s.value: required" % pre)
        vt = p.get("value_type", _MISSING)
        if vt is _MISSING:
            errors.append("%s.value_type: required" % pre)
        elif not _is_str(vt) or vt not in VALUE_TYPES:
            errors.append(
                "%s.value_type: must be 'bool', 'int', 'string', or "
                "'float', got '%s'" % (pre, _fmt(vt)))
        v = p.get("apply_command", _MISSING)
        if v is _MISSING:
            errors.append("%s.apply_command: required" % pre)
        elif not _is_str(v):
            errors.append("%s.apply_command: must be a string" % pre)
        elif v == "":
            errors.append("%s.apply_command: must not be empty" % pre)
        for key in ("check_command", "expected_state"):
            v = p.get(key, _MISSING)
            if v is not _MISSING and not _is_str(v):
                errors.append("%s.%s: must be a string" % (pre, key))
        ok = True
        for key in ("min_version", "max_version"):
            v = p.get(key, _MISSING)
            if v is _MISSING:
                continue
            if not _is_str(v) or v not in OS_VERSIONS:
                ok = False
                errors.append("%s.%s: unknown version '%s'"
                              % (pre, key, _fmt(v)))
        if ok and "min_version" in p and "max_version" in p:
            lo, hi = p["min_version"], p["max_version"]
            if OS_VERSIONS.index(lo) > OS_VERSIONS.index(hi):
                errors.append(
                    "%s: min_version '%s' is later than max_version '%s'"
                    % (pre, lo, hi))
        v = p.get("enabled", _MISSING)
        if v is not _MISSING and not isinstance(v, bool):
            errors.append("%s.enabled: must be a boolean" % pre)
    return errors


def validate_dock(data):
    errors = []
    if data.get("os") != "macos":
        errors.append("dock: 'os' is required and must be 'macos'")
    items = data.get("items", _MISSING)
    if items is _MISSING:
        errors.append("dock.items: required")
    elif not isinstance(items, list) or not items:
        errors.append("dock.items: must be a non-empty list of strings")
    else:
        for i, it in enumerate(items):
            if not _is_str(it):
                errors.append("dock.items[%d]: must be a string" % i)
    return errors


def run_validation(root):
    discovered = discover(root)
    entries = []
    for rel, data, perr in discovered:
        errs = [perr] if perr else validate_config(data)
        entries.append({"rel": rel, "data": data, "errors": errs})

    # 14. duplicate module schema check
    by_dir = {}
    for e in entries:
        if e["data"] is not None and e["data"].get("schema") == "module":
            by_dir.setdefault(module_name_of(e["rel"]), []).append(e)
    for mod, es in by_dir.items():
        if len(es) > 1:
            label = mod if mod else "."
            for e in es:
                e["errors"].append(
                    "duplicate 'module' schema in '%s'" % label)

    docks = [e for e in entries
             if isinstance(e["data"], dict) and e["data"].get("schema") == "dock"]
    if len(docks) > 1:
        for e in docks:
            e["errors"].append(
                "multiple 'dock' schemas found \u2014 only one is allowed")

    manifests = [e for e in entries
                 if isinstance(e["data"], dict)
                 and e["data"].get("schema") == "manifest"]
    root_manifests = []
    for e in manifests:
        mod = module_name_of(e["rel"])
        if mod:
            e["errors"].append(
                "manifest schema must be in the root directory, found in "
                "'%s'" % mod)
        else:
            root_manifests.append(e)
    if len(root_manifests) > 1:
        for e in root_manifests:
            e["errors"].append(
                "multiple 'manifest' schemas found \u2014 only one is allowed")

    files = []
    all_valid = True
    for e in entries:
        f = {"path": e["rel"], "valid": not e["errors"]}
        if e["errors"]:
            f["errors"] = e["errors"]
            all_valid = False
        files.append(f)
    payload = {"valid": all_valid, "files": files}
    return payload, entries, by_dir


# ------------------------------------------------------------------ planning

class PlanError(Exception):
    def __init__(self, kind, details):
        super().__init__(kind)
        self.kind = kind
        self.details = details


def expand_home(path):
    if path.startswith("~"):
        home = os.environ.get("HOME") or os.path.expanduser("~")
        rest = path[1:]
        if rest and not rest.startswith("/"):
            # "~user" style is not supported; treat literally under HOME
            rest = "/" + rest
        path = home.rstrip("/") + rest if rest else home
    return path


def apply_hidden(dest, hidden):
    if not hidden:
        return dest
    head, tail = os.path.split(dest)
    if tail and not tail.startswith("."):
        tail = "." + tail
    return os.path.join(head, tail) if head else tail


def _norm_dest(path):
    trailing = path.endswith("/")
    p = os.path.normpath(path)
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    return p + "/" if trailing and p != "/" else p


def plan_module(name, mod_dir, data):
    out = []
    osv = data.get("os")
    if isinstance(osv, dict):
        pm = osv.get("package_manager")
        for pkg in sorted(osv.get("packages") or []):
            out.append({"type": "install_package", "manager": pm,
                        "package": pkg})
        for app in sorted(osv.get("applications") or []):
            out.append({"type": "install_application", "manager": pm,
                        "application": app})

    for act in data.get("actions") or []:
        t = act["type"]
        src = act["source"]
        hidden = act.get("hidden", False)
        elevated = act.get("elevated", False)
        dest = act.get("destination")

        if src == "*":
            matches = []
            if os.path.isdir(mod_dir):
                for fn in os.listdir(mod_dir):
                    full = os.path.join(mod_dir, fn)
                    if os.path.isdir(full):
                        continue
                    if fn.startswith("."):
                        continue
                    if fn.lower().endswith(CONFIG_EXTS):
                        continue
                    matches.append(fn)
            pairs = []
            for fn in sorted(matches):
                d = None
                if t != "run" and dest is not None:
                    d = apply_hidden(
                        _norm_dest(expand_home(dest)) + fn, hidden)
                pairs.append((os.path.join(mod_dir, fn), d))
        else:
            full = os.path.normpath(os.path.join(mod_dir, src))
            if not os.path.exists(full):
                raise PlanError(
                    "file_not_found",
                    "module '%s': missing source file '%s'" % (name, src))
            d = None
            if t != "run":
                d = _norm_dest(expand_home(dest))
                if d.endswith("/"):
                    d = d + os.path.basename(full)
                d = apply_hidden(d, hidden)
            pairs = [(full, d)]

        for s, d in pairs:
            entry = {"type": t, "source": s}
            if t != "run":
                entry["destination"] = d
            entry["elevated"] = elevated
            out.append(entry)
    return out


def plan_preferences(pref_entries, os_version):
    out = []
    for e in pref_entries:
        for p in e["data"].get("preferences") or []:
            if p.get("enabled", True) is False:
                continue
            if os_version is not None:
                idx = OS_VERSIONS.index(os_version)
                lo = OS_VERSIONS.index(p.get("min_version", OS_VERSIONS[0]))
                hi = OS_VERSIONS.index(p.get("max_version", OS_VERSIONS[-1]))
                if not lo <= idx <= hi:
                    continue
            act = {"type": "set_preference", "name": p["name"],
                   "domain": p["domain"], "key": p["key"],
                   "value": p["value"], "value_type": p["value_type"],
                   "apply_command": p["apply_command"]}
            if "check_command" in p:
                act["check_command"] = p["check_command"]
                if "expected_state" in p:
                    act["expected_state"] = p["expected_state"]
            out.append(act)
    return out


def plan_environments(env_entries):
    out = []
    for e in env_entries:
        for env in e["data"].get("environments") or []:
            lang = env["language"]
            mgr = env["manager"]
            mname = mgr["name"]
            plugins = mgr.get("plugins") or []
            for v in env["versions"]:
                out.append({"type": "install_runtime", "language": lang,
                            "version": v, "manager": mname})
            for p in plugins:
                out.append({"type": "install_plugin", "manager": mname,
                            "plugin": p["name"]})
            for p in plugins:
                for ve in p.get("virtual_environments") or []:
                    out.append({"type": "create_virtual_env",
                                "language": lang, "manager": mname,
                                "plugin": p["name"],
                                "version": ve["version"],
                                "name": ve["name"]})
    return out


class DependencyError(PlanError):
    def __init__(self, payload):
        super().__init__(payload["error"], payload)
        self.payload = payload


def order_modules(modules, deps, priority=None):
    """Topologically order modules (dicts with 'name').

    Ties are broken by `priority` (name -> rank) when given, else
    alphabetically.
    """
    import heapq

    def key(n):
        if priority is not None:
            return (priority.get(n, len(priority)), n)
        return (0, n)
    names = {m["name"] for m in modules}
    for name in sorted(names):
        for d in sorted(deps.get(name, ())):
            if d not in names:
                raise DependencyError({"error": "missing_dependency",
                                       "module": name, "missing": d})
    remaining = {n: set(deps.get(n, ())) for n in names}
    dependents = {n: set() for n in names}
    for n, ds in remaining.items():
        for d in ds:
            dependents[d].add(n)
    heap = [(key(n), n) for n, ds in remaining.items() if not ds]
    heapq.heapify(heap)
    order = []
    done = set()
    while heap:
        _, n = heapq.heappop(heap)
        order.append(n)
        done.add(n)
        for x in dependents[n]:
            remaining[x].discard(n)
            if not remaining[x]:
                heapq.heappush(heap, (key(x), x))
    if len(order) != len(names):
        left = sorted(n for n in names if n not in done)
        path = []
        seen = {}
        cur = left[0]
        while cur not in seen:
            seen[cur] = len(path)
            path.append(cur)
            cur = min(remaining[cur])
        cycle = path[seen[cur]:] + [cur]
        raise DependencyError({"error": "circular_dependency",
                               "cycle": cycle})
    by_name = {m["name"]: m for m in modules}
    return [by_name[n] for n in order]


def find_conflicts(modules):
    by_dest = {}
    for m in modules:
        for act in m["actions"]:
            if act["type"] in ("link", "copy"):
                by_dest.setdefault(act["destination"], []).append(
                    {"module": m["name"], "type": act["type"],
                     "source": act["source"]})
    conflicts = []
    for dest in sorted(by_dest):
        srcs = by_dest[dest]
        if len(srcs) > 1:
            srcs.sort(key=lambda x: (x["module"], x["source"], x["type"]))
            conflicts.append({"destination": dest, "sources": srcs})
    return conflicts


class ProfileError(PlanError):
    def __init__(self, payload):
        super().__init__(payload["error"], payload)
        self.payload = payload


def find_manifest(entries):
    for e in entries:
        if (isinstance(e["data"], dict)
                and e["data"].get("schema") == "manifest"
                and not module_name_of(e["rel"])):
            return e["data"]
    return None


def discovered_modules(entries):
    names = set()
    for e in entries:
        name = module_name_of(e["rel"])
        if (name and isinstance(e["data"], dict)
                and e["data"].get("schema") in
                ("module", "preferences", "dock", "environments")):
            names.add(name)
    return names


def _find_cycle(profiles, start):
    path = []
    seen = {}
    cur = start
    while cur is not None and cur in profiles:
        if cur in seen:
            cyc = path[seen[cur]:]
            i = cyc.index(min(cyc))
            cyc = cyc[i:] + cyc[:i]
            return cyc + [cyc[0]]
        seen[cur] = len(path)
        path.append(cur)
        cur = profiles[cur].get("extends")
    return None


def resolve_profiles(manifest, known_modules, check=None):
    """Return {name: [resolved modules in order]} or raise ProfileError.

    `check` optionally restricts which profiles are validated for missing
    modules (default: all profiles).
    """
    profiles = manifest.get("profiles") or {}
    for name in sorted(profiles):
        cyc = _find_cycle(profiles, name)
        if cyc:
            raise ProfileError({"error": "circular_inheritance",
                                "cycle": cyc})
    resolved = {}

    def res(name):
        if name in resolved:
            return resolved[name]
        prof = profiles[name]
        out = []
        parent = prof.get("extends")
        if parent is not None:
            out.extend(res(parent))
        for m in prof.get("modules") or []:
            if m not in out:
                out.append(m)
        resolved[name] = out
        return out

    for name in sorted(profiles):
        res(name)
    for name in sorted(profiles if check is None else check):
        missing = sorted(set(m for m in resolved[name]
                             if m not in known_modules))
        if missing:
            raise ProfileError({"error": "invalid_profile",
                                "profile": name,
                                "missing_modules": missing})
    return resolved


def list_profiles(root):
    payload, entries, _ = run_validation(root)
    if not payload["valid"]:
        raise PlanError("validation_failed", payload)
    manifest = find_manifest(entries)
    if manifest is None:
        return {"profiles": []}
    resolved = resolve_profiles(manifest, discovered_modules(entries))
    return {"profiles": [{"name": n, "modules": sorted(resolved[n])}
                         for n in sorted(resolved)]}


def build_plan(root, os_filter, module_filter, os_version=None,
               profile=None, check_selected_only=False):
    if os_version is not None and os_version not in OS_VERSIONS:
        raise PlanError("unknown_os_version",
                        "unknown version '%s'" % os_version)
    payload, entries, by_dir = run_validation(root)
    if not payload["valid"]:
        raise PlanError("validation_failed", payload)
    if not entries:
        raise PlanError("no_configs", "no rig config files found in '%s'" % root)

    prefs_by_dir = {}
    dock_by_dir = {}
    env_by_dir = {}
    deps = {}
    for e in entries:
        schema = e["data"].get("schema")
        name = module_name_of(e["rel"])
        if schema == "preferences":
            prefs_by_dir.setdefault(name, []).append(e)
        elif schema == "dock":
            dock_by_dir.setdefault(name, []).append(e)
        elif schema == "environments":
            env_by_dir.setdefault(name, []).append(e)
        for d in e["data"].get("depends_on") or []:
            deps.setdefault(name, set()).add(d)

    profile_order = None
    if profile is not None:
        manifest = find_manifest(entries)
        if manifest is None:
            raise PlanError("no_manifest",
                            "no manifest found but --profile was specified")
        if profile not in (manifest.get("profiles") or {}):
            raise PlanError("unknown_profile",
                            "unknown profile '%s'" % profile)
        resolved = resolve_profiles(
            manifest, discovered_modules(entries),
            [profile] if check_selected_only else None)
        selected = list(resolved[profile])
        if module_filter is not None:
            selected = [m for m in selected if m in module_filter]
        i = 0
        while i < len(selected):
            for d in sorted(deps.get(selected[i], ())):
                if d not in selected:
                    selected.append(d)
            i += 1
        profile_order = {m: n for n, m in enumerate(selected)}
        module_filter = set(selected)

    root_abs = os.path.abspath(root)
    modules = []
    names = (set(by_dir) | set(prefs_by_dir) | set(dock_by_dir)
             | set(env_by_dir))
    for name in sorted(names):
        if not name:
            continue  # root-level configs are not modules
        if module_filter is not None and name not in module_filter:
            continue
        data = by_dir[name][0]["data"] if name in by_dir else None
        osv = data.get("os") if data is not None else None
        if os_filter is not None and isinstance(osv, dict):
            if osv.get("name") != os_filter:
                continue
        dock_ok = name in dock_by_dir and (os_filter is None
                                           or os_filter == "macos")
        if (data is None and name not in prefs_by_dir
                and name not in env_by_dir and not dock_ok):
            continue  # dock-only module excluded by --os
        mod_dir = os.path.join(root_abs, *name.split("/"))
        actions = []
        if data is not None:
            actions.extend(plan_module(name, mod_dir, data))
        actions.extend(plan_preferences(prefs_by_dir.get(name, []),
                                        os_version))
        if dock_ok:
            actions.append({"type": "configure_dock",
                            "items": list(dock_by_dir[name][0]["data"]
                                          ["items"])})
        actions.extend(plan_environments(env_by_dir.get(name, [])))
        modules.append({"name": name, "actions": actions})

    modules = order_modules(modules, deps, profile_order)

    conflicts = find_conflicts(modules)
    if conflicts:
        raise PlanError("conflict", conflicts)
    return {"modules": modules}


# -------------------------------------------------------------------- build

INSTALLER_NAME = "install.sh"

INSTALLER_HEADER = r'''#!/usr/bin/env bash
# Standalone installer generated by rig.py build.
# Usage: ./install.sh [--dry-run]
set -u

RIG_DRY_RUN=0
for rig_arg in "$@"; do
  case "$rig_arg" in
    --dry-run|-n) RIG_DRY_RUN=1 ;;
    -h|--help)
      echo "usage: $0 [--dry-run]"
      exit 0 ;;
    *)
      echo "unknown argument: $rig_arg" >&2
      exit 2 ;;
  esac
done

# Resolve the directory containing this script, regardless of the caller's
# current working directory or how the script was invoked.
rig_src="${BASH_SOURCE[0]}"
while [ -L "$rig_src" ]; do
  rig_dir="$(cd -P "$(dirname "$rig_src")" >/dev/null 2>&1 && pwd)"
  rig_src="$(readlink "$rig_src")"
  case "$rig_src" in /*) ;; *) rig_src="$rig_dir/$rig_src" ;; esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$rig_src")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR" || exit 1

RIG_TARGET_OS=__RIG_OS__

rig_log() { printf '[rig] %s\n' "$*" >&2; }
rig_die() { printf '[rig] error: %s\n' "$*" >&2; exit 1; }

rig_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    rig_die "elevation required but sudo is not available: $*"
  fi
}

rig_maybe_root() {
  local elevated="$1"; shift
  if [ "$elevated" = "1" ]; then rig_root "$@"; else "$@"; fi
}

rig_src_path() {
  printf '%s/%s' "$SCRIPT_DIR" "$1"
}

rig_step() {
  local msg="$1"; shift
  if [ "$RIG_DRY_RUN" = "1" ]; then
    printf '[rig] %s\n' "$msg"
    return 0
  fi
  rig_log "$msg"
  "$@" || rig_die "action failed: $msg"
}

# ---------------------------------------------------------------- packages

rig_pkg_install() {
  local manager="$1" kind="$2" name="$3"
  case "$manager" in
    brew)
      if [ "$kind" = "application" ]; then
        brew install --cask "$name"
      else
        brew install "$name"
      fi ;;
    apt)
      if [ -z "${RIG_APT_UPDATED:-}" ]; then
        rig_root env DEBIAN_FRONTEND=noninteractive apt-get update || true
        RIG_APT_UPDATED=1
      fi
      rig_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$name" ;;
    yum)
      rig_root yum install -y "$name" ;;
    *)
      rig_die "unsupported package manager: $manager" ;;
  esac
}

do_install_package() { rig_pkg_install "$1" package "$2"; }
do_install_application() { rig_pkg_install "$1" application "$2"; }

# ------------------------------------------------------------------ files

do_link() {
  local src dest="$2" elevated="$3"
  src="$(rig_src_path "$1")"
  [ -e "$src" ] || rig_die "missing packaged source: $1"
  rig_maybe_root "$elevated" mkdir -p "$(dirname "$dest")" || return 1
  if [ -d "$dest" ] && [ ! -L "$dest" ]; then
    rig_maybe_root "$elevated" rm -rf "$dest" || return 1
  fi
  rig_maybe_root "$elevated" ln -sfn "$src" "$dest"
}

do_copy() {
  local src dest="$2" elevated="$3"
  src="$(rig_src_path "$1")"
  [ -e "$src" ] || rig_die "missing packaged source: $1"
  rig_maybe_root "$elevated" mkdir -p "$(dirname "$dest")" || return 1
  if [ -L "$dest" ] || { [ -d "$src" ] && [ -e "$dest" ]; }; then
    rig_maybe_root "$elevated" rm -rf "$dest" || return 1
  fi
  if [ -d "$src" ]; then
    rig_maybe_root "$elevated" cp -R "$src" "$dest"
  else
    rig_maybe_root "$elevated" cp -f "$src" "$dest"
  fi
}

do_run() {
  local src elevated="$2" line interp
  src="$(rig_src_path "$1")"
  [ -f "$src" ] || rig_die "missing packaged source: $1"
  if [ -x "$src" ]; then
    rig_maybe_root "$elevated" "$src"
    return $?
  fi
  line="$(head -n 1 "$src" 2>/dev/null || true)"
  case "$line" in
    '#!'*)
      interp="${line#\#!}"
      # shellcheck disable=SC2086
      rig_maybe_root "$elevated" $interp "$src" ;;
    *)
      rig_maybe_root "$elevated" bash "$src" ;;
  esac
}

# ------------------------------------------------------------ preferences

rig_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

do_set_preference() {
  local apply="$1" has_check="$2" check="$3" expected="$4" current
  if [ "$has_check" = "1" ]; then
    current="$(bash -c "$check" 2>/dev/null)"
    if [ "$(rig_trim "$current")" = "$(rig_trim "$expected")" ]; then
      rig_log "preference already set, skipping"
      return 0
    fi
  fi
  bash -c "$apply"
}

# ------------------------------------------------------------------- dock

rig_dock_path() {
  case "$1" in
    /*|~*) printf '%s' "$1" ;;
    *.app) printf '/Applications/%s' "$1" ;;
    *) printf '/Applications/%s.app' "$1" ;;
  esac
}

do_configure_dock() {
  local item path
  if command -v dockutil >/dev/null 2>&1; then
    dockutil --remove all --no-restart >/dev/null 2>&1 || true
    for item in "$@"; do
      path="$(rig_dock_path "$item")"
      dockutil --add "$path" --no-restart || return 1
    done
  else
    defaults write com.apple.dock persistent-apps -array || return 1
    for item in "$@"; do
      path="$(rig_dock_path "$item")"
      defaults write com.apple.dock persistent-apps -array-add \
        "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>${path}</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>" \
        || return 1
    done
  fi
  killall Dock >/dev/null 2>&1 || true
}

# ----------------------------------------------------------- environments

rig_nvm_load() {
  if ! command -v nvm >/dev/null 2>&1; then
    local d="${NVM_DIR:-$HOME/.nvm}"
    # shellcheck disable=SC1091
    [ -s "$d/nvm.sh" ] && . "$d/nvm.sh"
  fi
}

rig_manager_root() {
  local manager="$1" root=""
  case "$manager" in
    pyenv) root="${PYENV_ROOT:-}" ;;
    rbenv) root="${RBENV_ROOT:-}" ;;
    nodenv) root="${NODENV_ROOT:-}" ;;
    goenv) root="${GOENV_ROOT:-}" ;;
  esac
  if [ -z "$root" ] && command -v "$manager" >/dev/null 2>&1; then
    root="$("$manager" root 2>/dev/null || true)"
  fi
  [ -n "$root" ] || root="$HOME/.$manager"
  printf '%s' "$root"
}

rig_plugin_url() {
  local manager="$1" plugin="$2"
  case "$plugin" in
    *://*|git@*) printf '%s' "$plugin" ;;
    */*) printf 'https://github.com/%s.git' "$plugin" ;;
    *)
      case "$manager" in
        pyenv) printf 'https://github.com/pyenv/%s.git' "$plugin" ;;
        rbenv) printf 'https://github.com/rbenv/%s.git' "$plugin" ;;
        nodenv) printf 'https://github.com/nodenv/%s.git' "$plugin" ;;
        *) printf 'https://github.com/%s/%s.git' "$manager" "$plugin" ;;
      esac ;;
  esac
}

do_install_runtime() {
  local manager="$1" language="$2" version="$3"
  case "$manager" in
    pyenv|rbenv|nodenv|goenv|jenv)
      "$manager" install -s "$version" ;;
    asdf)
      asdf plugin add "$language" >/dev/null 2>&1 || true
      asdf install "$language" "$version" ;;
    mise|rtx)
      "$manager" install "$language@$version" ;;
    nvm)
      rig_nvm_load
      nvm install "$version" ;;
    rustup)
      rustup toolchain install "$version" ;;
    sdk|sdkman)
      sdk install "$language" "$version" ;;
    *)
      "$manager" install "$version" ;;
  esac
}

do_install_plugin() {
  local manager="$1" plugin="$2" root dir name
  case "$manager" in
    pyenv|rbenv|nodenv|goenv)
      root="$(rig_manager_root "$manager")"
      name="${plugin##*/}"
      name="${name%.git}"
      dir="$root/plugins/$name"
      if [ -d "$dir" ]; then
        rig_log "plugin $name already installed"
        return 0
      fi
      mkdir -p "$root/plugins" || return 1
      git clone "$(rig_plugin_url "$manager" "$plugin")" "$dir" ;;
    asdf)
      if asdf plugin list 2>/dev/null | grep -qx "$plugin"; then
        return 0
      fi
      asdf plugin add "$plugin" ;;
    mise|rtx)
      "$manager" plugins install "$plugin" ;;
    *)
      "$manager" plugin add "$plugin" ;;
  esac
}

do_create_virtual_env() {
  local manager="$1" plugin="$2" name="$3" language="$4" version="$5"
  case "$manager" in
    pyenv)
      if pyenv versions --bare 2>/dev/null | grep -qx "$name"; then
        rig_log "virtual environment $name already exists"
        return 0
      fi
      pyenv virtualenv "$version" "$name" ;;
    asdf)
      case "$plugin" in
        asdf-venv|*venv*)
          if asdf venv --help >/dev/null 2>&1; then
            asdf venv create "$name" "$language" "$version"
          else
            local py
            py="$(asdf where "$language" "$version")/bin/python"
            mkdir -p "$HOME/.virtualenvs" || return 1
            "$py" -m venv "$HOME/.virtualenvs/$name"
          fi ;;
        *)
          asdf "${plugin#asdf-}" create "$name" "$language" "$version" ;;
      esac ;;
    rbenv)
      rbenv gemset create "$version" "$name" ;;
    *)
      "$manager" virtualenv "$version" "$name" ;;
  esac
}

# ---------------------------------------------------------------- actions
'''


def _q(v):
    import shlex
    return shlex.quote("" if v is None else str(v))


def _pkg_rel(root_abs, src):
    rel = os.path.relpath(src, root_abs)
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        rel = os.path.join("_external", os.path.basename(src))
    return rel.replace(os.sep, "/")


def _installer_line(act, rel_of):
    t = act["type"]
    if t in ("install_package", "install_application"):
        key = "package" if t == "install_package" else "application"
        msg = "%s %s %s" % (t, act["manager"], act[key])
        args = [act["manager"], act[key]]
    elif t in ("link", "copy"):
        rel = rel_of[act["source"]]
        msg = "%s %s %s" % (t, rel, act["destination"])
        args = [rel, act["destination"], "1" if act.get("elevated") else "0"]
    elif t == "run":
        rel = rel_of[act["source"]]
        msg = "run %s" % rel
        args = [rel, "1" if act.get("elevated") else "0"]
    elif t == "set_preference":
        msg = "set_preference %s" % act["name"]
        has = "check_command" in act and "expected_state" in act
        args = [act["apply_command"], "1" if has else "0",
                act.get("check_command", ""), act.get("expected_state", "")]
    elif t == "configure_dock":
        msg = "configure_dock %d items" % len(act["items"])
        args = list(act["items"])
    elif t == "install_runtime":
        msg = "install_runtime %s %s %s" % (act["manager"], act["language"],
                                            act["version"])
        args = [act["manager"], act["language"], act["version"]]
    elif t == "install_plugin":
        msg = "install_plugin %s %s" % (act["manager"], act["plugin"])
        args = [act["manager"], act["plugin"]]
    elif t == "create_virtual_env":
        msg = "create_virtual_env %s %s %s" % (act["manager"], act["plugin"],
                                               act["name"])
        args = [act["manager"], act["plugin"], act["name"],
                act["language"], act["version"]]
    else:  # pragma: no cover - plan only emits the types above
        raise ValueError(t)
    return "rig_step %s do_%s %s" % (_q(msg), t,
                                     " ".join(_q(a) for a in args))


def render_installer(os_name, profile, modules, rel_of):
    lines = [INSTALLER_HEADER.replace("__RIG_OS__", _q(os_name))]
    lines.append("# profile: %s" % profile.replace("\n", " "))
    for m in modules:
        lines.append("")
        lines.append("# module: %s" % m["name"].replace("\n", " "))
        for act in m["actions"]:
            lines.append(_installer_line(act, rel_of))
    lines.append("")
    lines.append("exit 0")
    lines.append("")
    return "\n".join(lines)


def _safe_name(s):
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)
    return out.strip(".") or "_"


def _remove_path(path):
    import shutil
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def write_package(root_abs, pkg_dir, os_name, profile, modules):
    import shutil
    rel_of = {}
    for m in modules:
        for act in m["actions"]:
            if act["type"] in ("link", "copy", "run"):
                rel_of.setdefault(act["source"],
                                  _pkg_rel(root_abs, act["source"]))
    _remove_path(pkg_dir)
    os.makedirs(pkg_dir)
    for src, rel in sorted(rel_of.items(), key=lambda kv: kv[1]):
        dst = os.path.join(pkg_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, symlinks=False)
        else:
            shutil.copy2(src, dst)
    inst = os.path.join(pkg_dir, INSTALLER_NAME)
    with open(inst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_installer(os_name, profile, modules, rel_of))
    os.chmod(inst, 0o755)


def write_archive(pkg_dir, archive):
    import tarfile
    _remove_path(archive)
    with tarfile.open(archive, "w:gz") as tf:
        for name in sorted(os.listdir(pkg_dir)):
            tf.add(os.path.join(pkg_dir, name), arcname=name)


_PLAN_ERROR_RANK = {"file_not_found": 0, "missing_dependency": 1,
                    "circular_dependency": 1, "conflict": 2}


def run_build(root, os_name, os_version, profile, output, release):
    if not os_name:
        raise PlanError("missing_flag", "--os is required for build")
    if os_version is not None and os_version not in OS_VERSIONS:
        raise PlanError("unknown_os_version",
                        "unknown version '%s'" % os_version)
    payload, entries, _ = run_validation(root)
    if not payload["valid"]:
        raise PlanError("validation_failed", payload)
    if not entries:
        raise PlanError("no_configs",
                        "no rig config files found in '%s'" % root)

    manifest = find_manifest(entries)
    if profile is not None:
        targets = [profile]
    elif manifest is None:
        targets = [None]
    else:
        profiles = manifest.get("profiles") or {}
        extended = {p.get("extends") for p in profiles.values()
                    if isinstance(p, dict)}
        leaves = sorted(n for n in profiles if n not in extended)
        # Cycle detection and leaf validation (alphabetical order).
        resolve_profiles(manifest, discovered_modules(entries), leaves)
        targets = leaves

    # Plan every package before touching the filesystem.
    plans = []
    failures = []
    for t in targets:
        try:
            plan = build_plan(root, os_name, None, os_version, t,
                              check_selected_only=True)
        except PlanError as exc:
            if exc.kind not in _PLAN_ERROR_RANK:
                raise
            failures.append((_PLAN_ERROR_RANK[exc.kind], t or "", exc))
            continue
        plans.append((t if t is not None else "default", plan))
    if failures:
        failures.sort(key=lambda f: (f[0], f[1]))
        raise failures[0][2]

    if output is None:
        out_dir = os.path.abspath("build")
    else:
        out_dir = os.path.normpath(output)
    os.makedirs(out_dir, exist_ok=True)
    root_abs = os.path.abspath(root)

    packages = []
    for pname, plan in sorted(plans, key=lambda p: p[0]):
        base = "%s-%s" % (_safe_name(pname), _safe_name(os_name))
        pkg_dir = os.path.join(out_dir, base)
        write_package(root_abs, pkg_dir, os_name, pname, plan["modules"])
        entry = {"profile": pname, "os": os_name, "directory": pkg_dir,
                 "modules": [m["name"] for m in plan["modules"]]}
        if release:
            archive = os.path.join(out_dir, base + ".tar.gz")
            write_archive(pkg_dir, archive)
            entry["archive"] = archive
        packages.append(entry)
    return {"packages": packages}


# ----------------------------------------------------------------------- cli

def main(argv=None):
    parser = argparse.ArgumentParser(prog="rig.py")
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    pv = sub.add_parser("validate")
    pv.add_argument("dir")
    pp = sub.add_parser("plan")
    pp.add_argument("dir")
    pp.add_argument("--os", dest="os_name", default=None)
    pp.add_argument("--os-version", dest="os_version", default=None)
    pp.add_argument("--module", dest="modules", nargs="+", action="extend",
                    default=None)
    pp.add_argument("--profile", dest="profile", default=None)
    pl = sub.add_parser("list-profiles")
    pl.add_argument("dir")
    pb = sub.add_parser("build")
    pb.add_argument("dir")
    pb.add_argument("--os", dest="os_name", default=None)
    pb.add_argument("--os-version", dest="os_version", default=None)
    pb.add_argument("--profile", dest="profile", default=None)
    pb.add_argument("--output", dest="output", default=None)
    pb.add_argument("--release", action="store_true", default=False)
    args = parser.parse_args(argv)

    if args.command == "validate":
        payload, _, _ = run_validation(args.dir)
        print(json.dumps(payload, indent=2))
        return 0 if payload["valid"] else 1

    try:
        if args.command == "list-profiles":
            plan = list_profiles(args.dir)
        elif args.command == "build":
            plan = run_build(args.dir, args.os_name, args.os_version,
                             args.profile, args.output, args.release)
        else:
            plan = build_plan(args.dir, args.os_name,
                              set(args.modules) if args.modules is not None
                              else None, args.os_version, args.profile)
    except PlanError as exc:
        if isinstance(exc, (DependencyError, ProfileError)):
            err = exc.payload
        elif exc.kind == "conflict":
            err = {"error": "conflict", "conflicts": exc.details}
        else:
            err = {"error": exc.kind, "details": exc.details}
        sys.stderr.write(json.dumps(err, indent=2) + "\n")
        return 1
    except OSError as exc:
        err = {"error": "io_error", "details": str(exc)}
        sys.stderr.write(json.dumps(err, indent=2) + "\n")
        return 1
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
