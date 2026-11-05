#!/usr/bin/env python3
"""synth_configs: merge layered YAML/JSON configs into a canonical experiment spec.

Layering (lowest -> highest precedence):
    base.yaml -> overrides/run.yaml (optional) -> flags.json (optional)
then any key still missing (at any depth) is filled in from
overrides/defaults.yaml (defaults never override user-provided keys).

Part 2: every layer has its `{{fragment:<name>}}` placeholders expanded
(recursively, from --fragments) before merging, and `${VAR}` tokens in string
values are interpolated from env.list after merging.
"""
import json
import math
import os
import re
import sys

import yaml


class SynthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


REQUIRED_SECTIONS = ("experiment_name", "training", "resources")
FLAG_NAMES = ("base", "overrides", "fragments", "flags", "env", "out")
OPTIONAL_FLAGS = ("fragments",)


# --------------------------------------------------------------------------
# CLI parsing
# --------------------------------------------------------------------------
def parse_args(argv):
    values = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--") or arg == "--":
            raise SynthError("INVALID_INPUT", "unexpected argument or subcommand: %s" % arg)
        name = arg[2:]
        value = None
        if "=" in name:
            name, value = name.split("=", 1)
        if name not in FLAG_NAMES:
            raise SynthError("INVALID_INPUT", "unknown flag: --%s" % name)
        if value is None:
            if i + 1 >= len(argv):
                raise SynthError("INVALID_INPUT", "flag --%s requires a value" % name)
            value = argv[i + 1]
            i += 1
        if name == "out" and "out" in values:
            raise SynthError("INVALID_INPUT", "duplicate --out flag")
        values[name] = value  # last occurrence wins
        i += 1
    for name in FLAG_NAMES:
        if name not in values:
            if name in OPTIONAL_FLAGS:
                continue
            raise SynthError("INVALID_INPUT", "missing required flag --%s" % name)
        if values[name] == "":
            raise SynthError("INVALID_INPUT", "flag --%s requires a non-empty value" % name)
    return values


# --------------------------------------------------------------------------
# YAML loading (JSON types only, duplicate keys rejected)
# --------------------------------------------------------------------------
class StrictLoader(yaml.SafeLoader):
    pass


# Restrict implicit resolvers: only true/false/null style booleans (no yes/no/on/off)
StrictLoader.yaml_implicit_resolvers = {}
for _ch, _resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items():
    kept = [
        (tag, rx)
        for tag, rx in _resolvers
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
    ]
    if kept:
        StrictLoader.yaml_implicit_resolvers[_ch] = kept
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _key_to_str(key):
    if isinstance(key, str):
        return key
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, (int, float)):
        return json.dumps(key)
    raise yaml.constructor.ConstructorError(
        None, None, "unsupported mapping key type: %r" % type(key).__name__
    )


def _construct_mapping(loader, node, deep=False):
    if not isinstance(node, yaml.MappingNode):
        raise yaml.constructor.ConstructorError(
            None, None, "expected a mapping node", node.start_mark
        )
    # Detect duplicates among explicit keys (before merge-key flattening).
    seen = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = _key_to_str(loader.construct_object(key_node, deep=True))
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found duplicate key %r" % key, key_node.start_mark,
            )
        seen.add(key)
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = _key_to_str(loader.construct_object(key_node, deep=True))
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


def _construct_yaml_map(loader, node):
    return _construct_mapping(loader, node)


def _reject(loader, node):
    raise yaml.constructor.ConstructorError(
        None, None, "unsupported YAML type %s" % node.tag, node.start_mark
    )


StrictLoader.add_constructor("tag:yaml.org,2002:map", _construct_yaml_map)
for _tag in ("binary", "set", "omap", "pairs", "timestamp"):
    StrictLoader.add_constructor("tag:yaml.org,2002:" + _tag, _reject)


def _check_json_types(value, where):
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise SynthError("PARSE_ERROR", "%s: non-finite numbers are not supported" % where)
        return
    if isinstance(value, list):
        for v in value:
            _check_json_types(v, where)
        return
    if isinstance(value, dict):
        for v in value.values():
            _check_json_types(v, where)
        return
    raise SynthError("PARSE_ERROR", "%s: unsupported value type %s" % (where, type(value).__name__))


def read_text(path, label):
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SynthError("PARSE_ERROR", "%s is not valid UTF-8" % label)


def load_yaml(path, label):
    text = read_text(path, label)
    try:
        docs = list(yaml.load_all(text, Loader=StrictLoader))
    except yaml.YAMLError as exc:
        msg = str(exc).replace("\n", " ")
        raise SynthError("PARSE_ERROR", "failed to parse %s: %s" % (label, msg))
    except RecursionError:
        raise SynthError("PARSE_ERROR", "failed to parse %s: recursive structure" % label)
    if len(docs) > 1:
        raise SynthError("PARSE_ERROR", "failed to parse %s: multiple YAML documents" % label)
    data = docs[0] if docs else None
    _check_json_types(data, label)
    return data


def _json_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key %r" % k)
        out[k] = v
    return out


def _bad_constant(name):
    raise ValueError("invalid constant %s" % name)


def load_json(path, label):
    text = read_text(path, label)
    if text.strip() == "":
        return None
    try:
        return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_bad_constant)
    except (ValueError, RecursionError) as exc:
        raise SynthError("PARSE_ERROR", "failed to parse %s: %s" % (label, exc))


def parse_env(path):
    """Parse env.list: KEY=VALUE per line; blank lines and # comments ignored."""
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        raise SynthError("MISSING_FILE", "env.list not found or unreadable: %s" % path)
    try:
        text = read_text(path, "env.list")
    except OSError as exc:
        raise SynthError("MISSING_FILE", "failed to read env.list: %s" % exc)
    env = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export ") and "=" in s:
            s = s[len("export "):].lstrip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if not k:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k] = v
    return env


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------
FRAGMENT_FULL_RE = re.compile(r"^\s*\{\{\s*fragment\s*:\s*(.*?)\s*\}\}\s*$", re.S)
FRAGMENT_ANY_RE = re.compile(r"\{\{\s*fragment\s*:")
FRAGMENT_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Positions (JSON pointers in the layer document) with a known required type.
OBJECT_POSITIONS = ("", "/training", "/resources")
ARRAY_POSITIONS = ("/resources/accelerators",)
STRING_POSITIONS = ("/experiment_name", "/resources/profile")
ACCEL_ITEM_RE = re.compile(r"^/resources/accelerators/\d+$")


class FragmentResolver:
    def __init__(self, fragments_dir):
        self.dir = fragments_dir
        self.cache = {}
        self.warned = set()
        self.warnings = []
        self.meta = self._load_meta()

    def _load_meta(self):
        if not self.dir:
            return {}
        path = os.path.join(self.dir, "meta.yaml")
        if not os.path.isfile(path):
            return {}
        try:
            meta = load_yaml(path, "fragments/meta.yaml")
        except OSError as exc:
            raise SynthError("MISSING_FILE", "failed to read fragments/meta.yaml: %s" % exc)
        return meta if isinstance(meta, dict) else {}

    def _load(self, name):
        if name in self.cache:
            return self.cache[name]
        path = os.path.join(self.dir, name + ".yaml") if self.dir else None
        if path is None or not os.path.isfile(path):
            raise SynthError("UNKNOWN_FRAGMENT", "fragment %r not found in fragments directory" % name)
        try:
            data = load_yaml(path, "fragments/%s.yaml" % name)
        except OSError as exc:
            raise SynthError("MISSING_FILE", "failed to read fragment %s: %s" % (name, exc))
        inline_reason = None
        if isinstance(data, dict) and "_meta" in data:
            m = data["_meta"]
            if isinstance(m, dict) and m.get("deprecated_reason") is not None:
                inline_reason = m["deprecated_reason"]
            data = {k: v for k, v in data.items() if k != "_meta"}
        self.cache[name] = (data, inline_reason)
        return self.cache[name]

    def _deprecation(self, name, inline_reason):
        if name in self.warned:
            return
        reason = None
        entry = self.meta.get(name)
        if isinstance(entry, dict) and entry.get("deprecated_reason") is not None:
            reason = entry["deprecated_reason"]
        elif inline_reason is not None:
            reason = inline_reason
        if reason is None:
            return
        if not isinstance(reason, str):
            reason = json.dumps(reason, ensure_ascii=False)
        self.warned.add(name)
        self.warnings.append(("FRAGMENT_DEPRECATED %s %s" % (name, reason)).rstrip())

    def expand(self, value, pointer="", stack=()):
        if isinstance(value, dict):
            return {
                k: self.expand(v, pointer + "/" + escape_pointer(k), stack)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.expand(v, "%s/%d" % (pointer, i), stack) for i, v in enumerate(value)]
        if not isinstance(value, str):
            return value
        m = FRAGMENT_FULL_RE.match(value)
        if m is None:
            if FRAGMENT_ANY_RE.search(value):
                raise SynthError("INVALID_TEMPLATE",
                                 "malformed fragment placeholder at %s: %r" % (pointer or "/", value))
            return value
        name = m.group(1)
        if not FRAGMENT_NAME_RE.match(name):
            raise SynthError("INVALID_TEMPLATE",
                             "invalid fragment name %r at %s" % (name, pointer or "/"))
        if name in stack:
            cycle = list(stack[stack.index(name):]) + [name]
            raise SynthError("FRAGMENT_CYCLE", "fragment cycle detected: %s" % " -> ".join(cycle))
        data, inline_reason = self._load(name)
        self._deprecation(name, inline_reason)
        result = self.expand(data, pointer, stack + (name,))
        self._check_position(name, result, pointer)
        return result

    @staticmethod
    def _check_position(name, result, pointer):
        rtype = json_type(result)
        where = pointer or "/"
        if pointer in OBJECT_POSITIONS or ACCEL_ITEM_RE.match(pointer):
            if rtype != "object":
                raise SynthError("INVALID_TEMPLATE",
                                 "fragment %r evaluates to %s but %s requires an object" % (name, rtype, where))
            return
        if pointer in ARRAY_POSITIONS:
            if rtype != "array":
                raise SynthError("INVALID_TEMPLATE",
                                 "fragment %r evaluates to %s but %s requires an array" % (name, rtype, where))
            return
        if pointer in STRING_POSITIONS and rtype in ("object", "array"):
            raise SynthError("INVALID_TEMPLATE",
                             "fragment %r evaluates to %s but %s requires a string" % (name, rtype, where))
        if rtype == "array":
            raise SynthError("INVALID_TEMPLATE",
                             "fragment %r evaluates to an array; fragments must be objects or scalars" % name)


# --------------------------------------------------------------------------
# Environment interpolation
# --------------------------------------------------------------------------
ENV_TOKEN_RE = re.compile(r"\$\{([^}]*)\}")


def interpolate(value, env, pointer, touched):
    if isinstance(value, dict):
        return {k: interpolate(v, env, pointer + "/" + escape_pointer(k), touched)
                for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env, "%s/%d" % (pointer, i), touched) for i, v in enumerate(value)]
    if not isinstance(value, str) or "${" not in value:
        return value

    def repl(m):
        var = m.group(1)
        if var not in env:
            raise SynthError("UNKNOWN_ENV_VAR", "%s is not defined" % var)
        return env[var]

    out = ENV_TOKEN_RE.sub(repl, value)
    if out != value or ENV_TOKEN_RE.search(value):
        touched.add(pointer)
    return out


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------
def json_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def escape_pointer(token):
    return token.replace("~", "~0").replace("/", "~1")


def deep_merge(low, high, pointer, low_src, high_src, warnings, sources):
    """Merge `high` over `low`. `sources` mirrors the structure tracking which
    layer each leaf/value came from (used for warning messages)."""
    if isinstance(low, dict) and isinstance(high, dict):
        result = dict(low)
        src_map = dict(sources) if isinstance(sources, dict) else {}
        for key in sorted(high.keys()):
            child_ptr = pointer + "/" + escape_pointer(key)
            if key in low:
                child_low_src = src_map.get(key, low_src)
                result[key], src_map[key] = deep_merge(
                    low[key], high[key], child_ptr, child_low_src, high_src,
                    warnings, child_low_src,
                )
            else:
                result[key] = high[key]
                src_map[key] = high_src
        return result, src_map
    if json_type(low) != json_type(high):
        loser = _src_name(sources, low_src)
        warnings.append("CONFLICT %s %s -> %s" % (pointer or "/", loser, high_src))
    return high, high_src


def _src_name(sources, default):
    if isinstance(sources, str):
        return sources
    return default


def merge_layers(layers):
    merged = None
    sources = None
    warnings = []
    have = False
    for name, doc in layers:
        if doc is None:
            continue  # empty/absent layer contributes nothing
        if not have:
            merged, sources, have = doc, name, True
            continue
        if isinstance(merged, dict) and isinstance(doc, dict):
            merged, sources = deep_merge(merged, doc, "", name, name, warnings, sources)
        else:
            merged, sources = deep_merge(merged, doc, "", _src_name(sources, name), name,
                                         warnings, sources)
    return (merged if have else {}), warnings


def fill_defaults(target, defaults):
    result = dict(target)
    for key, dval in defaults.items():
        if key not in result:
            result[key] = dval
        elif isinstance(result[key], dict) and isinstance(dval, dict):
            result[key] = fill_defaults(result[key], dval)
    return result


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def is_pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def validate(doc, interpolated=frozenset()):
    if not isinstance(doc, dict):
        raise SynthError("INVALID_STRUCTURE", "merged configuration must be an object, got %s" % json_type(doc))
    for key in REQUIRED_SECTIONS:
        if key not in doc:
            raise SynthError("MISSING_KEY", "%s is required" % key)

    name = doc["experiment_name"]
    if isinstance(name, str) and "/experiment_name" in interpolated and name.strip() == "":
        raise SynthError("MISSING_KEY", "experiment_name is empty after interpolation")
    if not isinstance(name, str):
        raise SynthError("INVALID_VALUE", "experiment_name must be a string")
    if name.strip() == "" or not any(not c.isspace() and c.isprintable() for c in name):
        raise SynthError("INVALID_VALUE", "experiment_name must contain non-whitespace characters")

    if not isinstance(doc["training"], dict):
        raise SynthError("INVALID_VALUE", "training must be an object")

    res = doc["resources"]
    if not isinstance(res, dict):
        raise SynthError("INVALID_VALUE", "resources must be an object")
    if ("profile" in res and "/resources/profile" in interpolated
            and isinstance(res["profile"], str) and res["profile"].strip() == ""):
        raise SynthError("MISSING_KEY", "resources.profile is empty after interpolation")
    if "accelerators" not in res and "profile" not in res:
        raise SynthError("MISSING_KEY", "resources.accelerators or resources.profile is required")
    if "accelerators" in res:
        accs = res["accelerators"]
        if not isinstance(accs, list):
            raise SynthError("INVALID_VALUE", "resources.accelerators must be an array")
        for i, acc in enumerate(accs):
            if not isinstance(acc, dict):
                raise SynthError("INVALID_VALUE", "resources.accelerators[%d] must be an object" % i)
            if "type" not in acc or not isinstance(acc["type"], str):
                raise SynthError("INVALID_VALUE", "resources.accelerators[%d].type must be a string" % i)
            if "count" not in acc or not is_pos_int(acc["count"]):
                raise SynthError("INVALID_VALUE", "resources.accelerators[%d].count must be a positive integer" % i)
    if "profile" in res and not isinstance(res["profile"], str):
        raise SynthError("INVALID_VALUE", "resources.profile must be a string")
    if "world_size" in res and not is_pos_int(res["world_size"]):
        raise SynthError("INVALID_VALUE", "resources.world_size must be a positive integer")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run(argv):
    args = parse_args(argv)

    base_path = args["base"]
    if not os.path.isfile(base_path) or not os.access(base_path, os.R_OK):
        raise SynthError("MISSING_FILE", "base file not found or unreadable: %s" % base_path)
    defaults_path = os.path.join(args["overrides"], "defaults.yaml")
    if not os.path.isfile(defaults_path) or not os.access(defaults_path, os.R_OK):
        raise SynthError("MISSING_FILE", "overrides/defaults.yaml not found or unreadable: %s" % defaults_path)
    run_path = os.path.join(args["overrides"], "run.yaml")

    try:
        base = load_yaml(base_path, "base.yaml")
        defaults = load_yaml(defaults_path, "defaults.yaml")
        run_doc = load_yaml(run_path, "run.yaml") if os.path.isfile(run_path) else None
        flags = load_json(args["flags"], "flags.json") if os.path.isfile(args["flags"]) else None
    except OSError as exc:
        raise SynthError("MISSING_FILE", "failed to read input: %s" % exc)
    env = parse_env(args["env"])

    # Fragment expansion happens per layer, before any merging.
    resolver = FragmentResolver(args.get("fragments"))
    base = resolver.expand(base)
    defaults = resolver.expand(defaults)
    run_doc = resolver.expand(run_doc)
    flags = resolver.expand(flags)

    merged, conflicts = merge_layers(
        [("base.yaml", base), ("run.yaml", run_doc), ("flags.json", flags)]
    )
    warnings = resolver.warnings + conflicts

    # Defaults injection: fill keys missing at any depth (never override).
    if isinstance(merged, dict) and isinstance(defaults, dict):
        merged = fill_defaults(merged, defaults)

    # Environment interpolation (post-merge; never produces warnings).
    touched = set()
    merged = interpolate(merged, env, "", touched)

    validate(merged, touched)

    out_dir = args["out"]
    os.makedirs(out_dir, exist_ok=True)
    spec_text = json.dumps(merged, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    warn_text = "".join(w + "\n" for w in warnings)
    with open(os.path.join(out_dir, "final_spec.json"), "w", encoding="utf-8") as fh:
        fh.write(spec_text)
    with open(os.path.join(out_dir, "warnings.txt"), "w", encoding="utf-8") as fh:
        fh.write(warn_text)
    return 0


def main():
    try:
        return run(sys.argv[1:])
    except SynthError as exc:
        envelope = {"error": {"code": exc.code, "message": exc.message}}
        sys.stderr.write(json.dumps(envelope) + "\n")
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        envelope = {"error": {"code": "INVALID_INPUT", "message": "unexpected error: %s" % exc}}
        sys.stderr.write(json.dumps(envelope) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
