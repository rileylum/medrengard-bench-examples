#!/usr/bin/env python3
"""synth_configs: merge layered YAML/JSON configs into a canonical experiment spec."""

import copy
import csv
import io
import json
import math
import os
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class SynthError(Exception):
    def __init__(self, code, message, path=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def fail(code, message):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": message}}) + "\n")
    sys.exit(1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

KNOWN_FLAGS = ("base", "overrides", "fragments", "flags", "env", "out", "manifest")
OPTIONAL_FLAGS = ("manifest",)


def parse_args(argv):
    values = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--") or arg == "--":
            raise SynthError("INVALID_INPUT", "unexpected argument or subcommand: %r" % arg)
        name = arg[2:]
        value = None
        if "=" in name:
            name, value = name.split("=", 1)
        if name not in KNOWN_FLAGS:
            raise SynthError("INVALID_INPUT", "unknown flag: --%s" % name)
        if value is None:
            if i + 1 >= len(argv):
                raise SynthError("INVALID_INPUT", "flag --%s requires a value" % name)
            value = argv[i + 1]
            i += 2
        else:
            i += 1
        if name == "out" and "out" in values:
            raise SynthError("INVALID_INPUT", "duplicate --out flag")
        if value == "":
            raise SynthError("INVALID_INPUT", "flag --%s requires a non-empty value" % name)
        values[name] = value  # last occurrence wins
    for name in KNOWN_FLAGS:
        if name not in values and name not in OPTIONAL_FLAGS:
            raise SynthError("INVALID_INPUT", "missing required flag --%s" % name)
    return values


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _build_loader():
    class StrictLoader(yaml.SafeLoader):
        pass

    # Restrict implicit resolvers to JSON-compatible scalars only.
    resolvers = {}
    for ch, lst in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [
            (tag, rx) for tag, rx in lst
            if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
        ]
        if kept:
            resolvers[ch] = kept
    StrictLoader.yaml_implicit_resolvers = resolvers
    StrictLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )

    def construct_bool(loader, node):
        return loader.construct_scalar(node).lower() == "true"

    StrictLoader.add_constructor("tag:yaml.org,2002:bool", construct_bool)

    def construct_timestamp_as_str(loader, node):
        return loader.construct_scalar(node)

    StrictLoader.add_constructor("tag:yaml.org,2002:timestamp", construct_timestamp_as_str)

    def construct_binary_as_str(loader, node):
        return loader.construct_scalar(node)

    StrictLoader.add_constructor("tag:yaml.org,2002:binary", construct_binary_as_str)

    def construct_mapping(loader, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, "expected a mapping node", node.start_mark)
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=True)
            try:
                hashable = key if not isinstance(key, (list, dict)) else repr(key)
                hash(hashable)
            except TypeError:
                hashable = repr(key)
            if hashable in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found duplicate key %r" % (key,), key_node.start_mark)
            seen.add(hashable)
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if isinstance(key, (list, dict)):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "unsupported non-scalar key", key_node.start_mark)
            if isinstance(key, bool):
                key = "true" if key else "false"
            elif key is None:
                key = "null"
            elif not isinstance(key, str):
                key = str(key)
            value = loader.construct_object(value_node, deep=deep)
            result[key] = value
        return result

    def construct_yaml_map(loader, node):
        data = {}
        yield data
        data.update(construct_mapping(loader, node, deep=True))

    StrictLoader.add_constructor("tag:yaml.org,2002:map", construct_yaml_map)
    StrictLoader.construct_mapping = construct_mapping
    return StrictLoader


_LOADER = None


def _check_json_compatible(value, label):
    if isinstance(value, dict):
        for v in value.values():
            _check_json_compatible(v, label)
    elif isinstance(value, list):
        for v in value:
            _check_json_compatible(v, label)
    elif value is None or isinstance(value, (bool, int, str)):
        pass
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise SynthError("PARSE_ERROR", "%s: non-finite number is not valid JSON" % label, path=label)
    else:
        raise SynthError("PARSE_ERROR", "%s: unsupported value type %s" % (label, type(value).__name__), path=label)


def read_text(path, label):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SynthError("MISSING_FILE", "%s: cannot read %s (%s)" % (label, path, exc.strerror), path=label)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SynthError("PARSE_ERROR", "%s: file is not valid UTF-8" % label, path=label)
    if text.startswith("﻿"):
        text = text[1:]
    return text


def parse_yaml(text, label):
    global _LOADER
    if yaml is None:
        raise SynthError("PARSE_ERROR", "%s: PyYAML is not installed" % label, path=label)
    if _LOADER is None:
        _LOADER = _build_loader()
    try:
        docs = list(yaml.load_all(text, Loader=_LOADER))
    except yaml.YAMLError as exc:
        msg = str(exc).replace("\n", " ")
        raise SynthError("PARSE_ERROR", "%s: invalid YAML: %s" % (label, msg), path=label)
    except RecursionError:
        raise SynthError("PARSE_ERROR", "%s: YAML nesting too deep or recursive alias" % label, path=label)
    if len(docs) > 1:
        raise SynthError("PARSE_ERROR", "%s: multiple YAML documents are not supported" % label, path=label)
    data = docs[0] if docs else None
    _check_json_compatible(data, label)
    return data


def parse_json(text, label):
    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise SynthError("PARSE_ERROR", "%s: duplicate key %r" % (label, k), path=label)
            obj[k] = v
        return obj

    def bad_constant(name):
        raise SynthError("PARSE_ERROR", "%s: invalid JSON constant %s" % (label, name), path=label)

    try:
        return json.loads(text, object_pairs_hook=hook, parse_constant=bad_constant)
    except json.JSONDecodeError as exc:
        raise SynthError("PARSE_ERROR", "%s: invalid JSON: %s" % (label, exc), path=label)
    except RecursionError:
        raise SynthError("PARSE_ERROR", "%s: JSON nesting too deep" % label, path=label)


def parse_env(path):
    """Parse KEY=VALUE lines from env.list (required in Part 2)."""
    if not os.path.isfile(path):
        raise SynthError("MISSING_FILE", "env.list: file not found: %s" % path,
                         path="env.list")
    text = read_text(path, "env.list")
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            result[key] = value.strip()
    return result


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------

FRAGMENT_EXACT_RE = re.compile(r"^\s*\{\{\s*fragment\s*:(.*?)\}\}\s*$", re.S)
FRAGMENT_ANY_RE = re.compile(r"\{\{\s*fragment\s*:")
FRAGMENT_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class FragmentResolver:
    def __init__(self, fragments_dir, warnings):
        self.dir = fragments_dir
        self.warnings = warnings
        self.cache = {}
        self.warned = set()
        self.origin = ()
        self.meta = self._load_meta()

    def _load_meta(self):
        path = os.path.join(self.dir, "meta.yaml")
        if not os.path.isfile(path):
            return {}
        data = parse_yaml(read_text(path, "fragments/meta.yaml"), "fragments/meta.yaml")
        if not isinstance(data, dict):
            return {}
        return data

    def _deprecation_reason(self, name, own_meta):
        entry = self.meta.get(name)
        if isinstance(entry, dict) and "deprecated_reason" in entry \
                and entry["deprecated_reason"] is not None:
            return entry["deprecated_reason"]
        if isinstance(own_meta, dict) and own_meta.get("deprecated_reason") is not None:
            return own_meta["deprecated_reason"]
        return None

    def _load(self, name, stack):
        if name in stack:
            cycle = stack[stack.index(name):] + [name]
            raise SynthError("FRAGMENT_CYCLE",
                             "fragment cycle detected: %s" % " -> ".join(cycle),
                             path="fragments/%s.yaml" % name)
        if name in self.cache:
            return self.cache[name]
        path = os.path.join(self.dir, name + ".yaml")
        if not os.path.isfile(path):
            raise SynthError("UNKNOWN_FRAGMENT",
                             "fragment %r not found (expected %s)" % (name, path),
                             path="fragments/%s.yaml" % name)
        label = "fragments/%s.yaml" % name
        data = parse_yaml(read_text(path, label), label)
        own_meta = None
        if isinstance(data, dict) and "_meta" in data:
            own_meta = data.pop("_meta")
        reason = self._deprecation_reason(name, own_meta)
        if reason is not None and name not in self.warned:
            self.warned.add(name)
            if not isinstance(reason, str):
                reason = json.dumps(reason, sort_keys=True, ensure_ascii=False)
            self.warnings.deprecated(name, reason, self.origin)
        if isinstance(data, list):
            raise SynthError("INVALID_TEMPLATE",
                             "fragment %r evaluates to an array; only objects or scalars "
                             "may be inlined" % name, path=label)
        resolved = self._expand(data, stack + [name], None, ())
        self.cache[name] = resolved
        return resolved

    def _expand(self, value, stack, record, path):
        if isinstance(value, dict):
            return {k: self._expand(v, stack, record, path + (k,)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand(v, stack, record, path + (i,)) for i, v in enumerate(value)]
        if isinstance(value, str):
            m = FRAGMENT_EXACT_RE.match(value)
            if m:
                name = m.group(1).strip()
                if not FRAGMENT_NAME_RE.match(name):
                    raise SynthError("INVALID_TEMPLATE",
                                     "invalid fragment name %r in placeholder %r"
                                     % (name, value), path=pointer(path))
                if record is not None:
                    record.add(path)
                    self.origin = path
                return copy.deepcopy(self._load(name, stack))
            if FRAGMENT_ANY_RE.search(value):
                raise SynthError("INVALID_TEMPLATE",
                                 "malformed fragment placeholder in %r" % value,
                                 path=pointer(path))
        return value

    def expand_layer(self, data):
        """Expand placeholders in a layer; returns (data, set of inlined paths)."""
        record = set()
        result = self._expand(data, [], record, ())
        return result, record


# --------------------------------------------------------------------------
# Environment interpolation
# --------------------------------------------------------------------------

ENV_TOKEN_RE = re.compile(r"\$\{([^}]*)\}")


def interpolate(value, env, path, touched):
    if isinstance(value, dict):
        return {k: interpolate(v, env, path + (k,), touched) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env, path + (i,), touched) for i, v in enumerate(value)]
    if isinstance(value, str) and "${" in value:
        def repl(m):
            name = m.group(1)
            if name not in env:
                raise SynthError("UNKNOWN_ENV_VAR", "%s is not defined" % name,
                                 path=pointer(path))
            return env[name]
        new = ENV_TOKEN_RE.sub(repl, value)
        if new != value or ENV_TOKEN_RE.search(value):
            touched.add(path)
        return new
    return value


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def pointer_escape(token):
    return str(token).replace("~", "~0").replace("/", "~1")


def pointer(path):
    return "".join("/" + pointer_escape(t) for t in path)


class WarningSink:
    """Collects warnings; with a run id, uses Part 3 formats and pointer ordering."""

    def __init__(self, run_id=None):
        self.run_id = run_id
        self.items = []  # (pointer, seq, kind, text)

    def _add(self, ptr, kind, text):
        self.items.append((ptr, len(self.items), kind, text))

    def conflict(self, ptr, loser, winner):
        if self.run_id is None:
            text = "CONFLICT %s %s -> %s" % (ptr, loser, winner)
        else:
            text = "CONFLICT %s %s %s -> %s" % (self.run_id, ptr, loser, winner)
        self._add(ptr, "conflict", text)

    def deprecated(self, name, reason, origin):
        if self.run_id is None:
            text = "FRAGMENT_DEPRECATED %s %s" % (name, reason)
        else:
            text = "FRAGMENT_DEPRECATED %s %s %s" % (self.run_id, name, reason)
        self._add(pointer(origin), "deprecated", text)

    def lines(self):
        if self.run_id is None:
            return [it[3] for it in self.items]
        return [it[3] for it in sorted(self.items, key=lambda it: (it[0], it[1]))]

    def count(self):
        return len(self.items)

    def conflicts(self):
        return sum(1 for it in self.items if it[2] == "conflict")


class Node:
    """Value plus provenance (which source last wrote it)."""

    __slots__ = ("value", "source")

    def __init__(self, value, source):
        self.value = value  # for objects: dict of key -> Node
        self.source = source


def wrap(value, source):
    if isinstance(value, dict):
        return Node({k: wrap(v, source) for k, v in value.items()}, source)
    return Node(value, source)


def unwrap(node):
    if isinstance(node.value, dict):
        return {k: unwrap(v) for k, v in node.value.items()}
    return node.value


def merge_into(lower, higher_value, source, path, warnings):
    """Merge a raw value from `source` over Node `lower`; returns resulting Node."""
    if isinstance(lower.value, dict) and isinstance(higher_value, dict):
        lower.source = source
        for key in sorted(higher_value):
            child_path = path + "/" + pointer_escape(key)
            if key in lower.value:
                lower.value[key] = merge_into(lower.value[key], higher_value[key], source,
                                              child_path, warnings)
            else:
                lower.value[key] = wrap(higher_value[key], source)
        return lower
    if json_type(lower.value) != json_type(higher_value):
        warnings.conflict(path, lower.source, source)
    return wrap(higher_value, source)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ValidationContext:
    def __init__(self, merged=None, fragment_paths=None, interpolated=None):
        self.merged = merged
        self.fragment_paths = fragment_paths or {}
        self.interpolated = interpolated or set()

    def _source_at(self, path):
        node = self.merged
        if node is None:
            return None
        for key in path:
            if isinstance(node.value, dict) and key in node.value:
                node = node.value[key]
            else:
                return None
        return node.source

    def from_fragment(self, path):
        source = self._source_at(path)
        if source is None:
            return False
        record = self.fragment_paths.get(source, ())
        return any(tuple(path[:i]) in record for i in range(len(path) + 1))

    def require_object(self, value, path, label):
        if isinstance(value, dict):
            return
        if self.from_fragment(path):
            raise SynthError("INVALID_TEMPLATE",
                             "fragment inlined at %s evaluates to %s, but an object is required"
                             % (label, json_type(value)), path=pointer(path))
        raise SynthError("INVALID_VALUE", "%s must be an object" % label, path=pointer(path))

    def empty_interpolated(self, value, path):
        return (path in self.interpolated and isinstance(value, str)
                and not value.strip())


def validate(doc, ctx=None):
    if ctx is None:
        ctx = ValidationContext()
    if not isinstance(doc, dict):
        if ctx.from_fragment(()):
            raise SynthError("INVALID_TEMPLATE",
                             "fragment at document root evaluates to %s, but an object is "
                             "required" % json_type(doc), path="")
        raise SynthError("INVALID_STRUCTURE",
                         "merged document must be an object, got %s" % json_type(doc), path="")
    for key in ("experiment_name", "training", "resources"):
        if key not in doc:
            raise SynthError("MISSING_KEY", "%s is required" % key, path="/" + key)

    name = doc["experiment_name"]
    if ctx.empty_interpolated(name, ("experiment_name",)):
        raise SynthError("MISSING_KEY", "experiment_name is empty after interpolation",
                         path="/experiment_name")
    if not isinstance(name, str):
        raise SynthError("INVALID_VALUE", "experiment_name must be a string",
                         path="/experiment_name")
    if not name.strip() or not any(not c.isspace() and c.isprintable() for c in name):
        raise SynthError("INVALID_VALUE", "experiment_name must contain non-whitespace characters",
                         path="/experiment_name")

    ctx.require_object(doc["training"], ("training",), "training")

    resources = doc["resources"]
    ctx.require_object(resources, ("resources",), "resources")
    if "accelerators" not in resources and "profile" not in resources:
        raise SynthError("MISSING_KEY", "resources.accelerators or resources.profile is required",
                         path="/resources/accelerators")

    if "accelerators" in resources:
        accs = resources["accelerators"]
        if not isinstance(accs, list):
            raise SynthError("INVALID_VALUE", "resources.accelerators must be an array",
                             path="/resources/accelerators")
        for idx, acc in enumerate(accs):
            where = "resources.accelerators[%d]" % idx
            ptr = "/resources/accelerators/%d" % idx
            if not isinstance(acc, dict):
                raise SynthError("INVALID_VALUE", "%s must be an object" % where,
                                 path=ptr)
            if ctx.empty_interpolated(acc.get("type"), ("resources", "accelerators", idx, "type")):
                raise SynthError("MISSING_KEY", "%s.type is empty after interpolation" % where,
                                 path=ptr + "/type")
            if "type" not in acc or not isinstance(acc["type"], str):
                raise SynthError("INVALID_VALUE", "%s.type must be a string" % where,
                                 path=ptr + "/type")
            if "count" not in acc or not is_positive_int(acc["count"]):
                raise SynthError("INVALID_VALUE", "%s.count must be a positive integer" % where,
                                 path=ptr + "/count")

    if "profile" in resources:
        if ctx.empty_interpolated(resources["profile"], ("resources", "profile")) \
                and "accelerators" not in resources:
            raise SynthError("MISSING_KEY", "resources.profile is empty after interpolation",
                             path="/resources/profile")
        if not isinstance(resources["profile"], str):
            raise SynthError("INVALID_VALUE", "resources.profile must be a string",
                             path="/resources/profile")

    if "world_size" in resources and not is_positive_int(resources["world_size"]):
        raise SynthError("INVALID_VALUE", "resources.world_size must be a positive integer",
                         path="/resources/world_size")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_flags_layer(path, label):
    text = read_text(path, label)
    if not text.strip():
        return None
    return parse_json(text, label)


def synthesize(base_path, overrides_dir, extra_layers, fragments_dir, env_path, sink):
    """Build the merged, interpolated, validated document.

    extra_layers: list of (label, loader) callables evaluated in order, each
    returning parsed data or None (absent layer).
    """
    if not os.path.isfile(base_path):
        raise SynthError("MISSING_FILE", "base.yaml: file not found: %s" % base_path,
                         path="base.yaml")
    defaults_path = os.path.join(overrides_dir, "defaults.yaml")
    if not os.path.isfile(defaults_path):
        raise SynthError("MISSING_FILE", "defaults.yaml: file not found: %s" % defaults_path,
                         path="defaults.yaml")

    layers = []
    layers.append(("base.yaml", parse_yaml(read_text(base_path, "base.yaml"), "base.yaml")))
    defaults = parse_yaml(read_text(defaults_path, "defaults.yaml"), "defaults.yaml")
    layers.append(("defaults.yaml", defaults))
    for label, loader in extra_layers:
        data = loader()
        if data is not None:
            layers.append((label, data))

    env = parse_env(env_path)

    resolver = FragmentResolver(fragments_dir, sink)
    fragment_paths = {}
    expanded_layers = []
    for source, data in layers:
        if data is None:
            expanded_layers.append((source, data))
            continue
        data, record = resolver.expand_layer(data)
        fragment_paths[source] = record
        expanded_layers.append((source, data))
        if source == "defaults.yaml":
            defaults = data

    merged = None
    for source, data in expanded_layers:
        if data is None:
            continue  # empty document: absent layer
        if merged is None:
            merged = wrap(data, source)
        else:
            merged = merge_into(merged, data, source, "", sink)

    doc = unwrap(merged) if merged is not None else None

    # Defaults injection: fill missing top-level sections only.
    if isinstance(doc, dict) and isinstance(defaults, dict):
        for key, value in defaults.items():
            if key not in doc:
                doc[key] = copy.deepcopy(value)

    # Environment interpolation (post-merge; never produces warnings).
    interpolated = set()
    doc = interpolate(doc, env, (), interpolated)

    validate(doc, ValidationContext(merged, fragment_paths, interpolated))
    return doc


def canonical_json(doc):
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def run_single(args):
    """Part 1/2 mode: no manifest."""
    run_path = os.path.join(args["overrides"], "run.yaml")
    flags_path = args["flags"]

    def load_run():
        if os.path.isfile(run_path):
            return parse_yaml(read_text(run_path, "run.yaml"), "run.yaml")
        return None

    def load_flags():
        if os.path.isfile(flags_path):
            return load_flags_layer(flags_path, "flags.json")
        return None

    sink = WarningSink()
    doc = synthesize(args["base"], args["overrides"],
                     [("run.yaml", load_run), ("flags.json", load_flags)],
                     args["fragments"], args["env"], sink)

    out_dir = args["out"]
    try:
        os.makedirs(out_dir, exist_ok=True)
        write_text(os.path.join(out_dir, "final_spec.json"), canonical_json(doc))
        write_text(os.path.join(out_dir, "warnings.txt"),
                   "".join(w + "\n" for w in sink.lines()))
    except OSError as exc:
        raise SynthError("INVALID_INPUT", "cannot write to output directory %s: %s"
                         % (out_dir, exc.strerror))


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
REQUIRED_COLUMNS = ("run_id", "override_path")


def _filter_manifest_lines(text):
    """Drop blank and '#' comment lines that are not inside a quoted field."""
    kept = []
    in_quotes = False
    for line in text.splitlines(True):
        if not in_quotes:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
        kept.append(line)
        if line.count('"') % 2 == 1:
            in_quotes = not in_quotes
    return "".join(kept)


def parse_manifest(path):
    if not os.path.isfile(path):
        raise SynthError("INVALID_MANIFEST", "manifest not found: %s" % path, path=path)
    try:
        text = read_text(path, "runs_manifest.csv")
    except SynthError as exc:
        raise SynthError("INVALID_MANIFEST", exc.message, path=path)
    body = _filter_manifest_lines(text)
    try:
        rows = list(csv.reader(io.StringIO(body, newline=""), strict=True))
    except csv.Error as exc:
        raise SynthError("INVALID_MANIFEST", "malformed CSV: %s" % exc, path=path)
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise SynthError("INVALID_MANIFEST", "manifest is missing a header row", path=path)
    header = [h.strip() for h in rows[0]]
    for col in REQUIRED_COLUMNS:
        if col not in header:
            raise SynthError("INVALID_MANIFEST",
                             "manifest header is missing required column %r" % col, path=path)
    if len(set(header)) != len(header):
        raise SynthError("INVALID_MANIFEST", "manifest header has duplicate columns", path=path)

    entries = []
    seen = set()
    for lineno, row in enumerate(rows[1:], start=2):
        rec = {}
        for idx, name in enumerate(header):
            rec[name] = row[idx].strip() if idx < len(row) else ""
        run_id = rec["run_id"]
        if not run_id:
            raise SynthError("INVALID_MANIFEST", "row %d: run_id is empty" % lineno, path=path)
        if not RUN_ID_RE.match(run_id):
            raise SynthError("INVALID_MANIFEST",
                             "row %d: run_id %r contains invalid characters" % (lineno, run_id),
                             path=path)
        if run_id in seen:
            raise SynthError("INVALID_MANIFEST", "duplicate run_id %r" % run_id, path=path)
        seen.add(run_id)
        if not rec["override_path"]:
            raise SynthError("INVALID_MANIFEST",
                             "row %d: override_path is required" % lineno, path=path)
        notes = rec.get("notes", "")
        entries.append({
            "run_id": run_id,
            "override_path": rec["override_path"],
            "flags_path": rec.get("flags_path", "") or None,
            "notes": notes if notes != "" else None,
        })
    if not entries:
        raise SynthError("INVALID_MANIFEST", "manifest contains no runs", path=path)
    return entries


def _resolve(manifest_dir, p):
    return p if os.path.isabs(p) else os.path.join(manifest_dir, p)


def process_run(args, entry, manifest_dir, sink):
    override_label = entry["override_path"]
    override_path = _resolve(manifest_dir, override_label)
    if not os.path.isfile(override_path):
        raise SynthError("MISSING_FILE", "%s not found" % override_label, path=override_label)
    flags_label = entry["flags_path"]
    flags_path = None
    if flags_label is not None:
        flags_path = _resolve(manifest_dir, flags_label)
        if not os.path.isfile(flags_path):
            raise SynthError("MISSING_FILE", "%s not found" % flags_label, path=flags_label)

    global_path = args["flags"]
    global_label = os.path.basename(global_path) or global_path

    def load_override():
        return parse_yaml(read_text(override_path, override_label), override_label)

    def load_run_flags():
        if flags_path is None:
            return None
        return load_flags_layer(flags_path, flags_label)

    def load_global_flags():
        if not os.path.isfile(global_path):
            return None
        try:
            text = read_text(global_path, global_label)
        except SynthError as exc:
            if exc.code == "MISSING_FILE":
                return None  # unreadable: skipped
            raise
        if not text.strip():
            return None
        return parse_json(text, global_label)

    layers = [(override_label, load_override)]
    if flags_label is not None:
        layers.append((flags_label, load_run_flags))
    layers.append((global_label, load_global_flags))
    return synthesize(args["base"], args["overrides"], layers, args["fragments"],
                      args["env"], sink)


def write_manifest_outputs(out_dir, summary, warning_lines, violations):
    try:
        os.makedirs(out_dir, exist_ok=True)
        write_text(os.path.join(out_dir, "summary.json"),
                   json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        write_text(os.path.join(out_dir, "warnings.txt"),
                   "".join(w + "\n" for w in warning_lines))
        write_text(os.path.join(out_dir, "violations.ndjson"),
                   "".join(json.dumps(v, ensure_ascii=False, separators=(",", ":")) + "\n"
                           for v in violations))
    except OSError as exc:
        raise SynthError("INVALID_INPUT", "cannot write to output directory %s: %s"
                         % (out_dir, exc.strerror))


def run_manifest(args):
    out_dir = args["out"]
    manifest_path = args["manifest"]
    try:
        entries = parse_manifest(manifest_path)
    except SynthError as exc:
        violation = {"run_id": None, "code": exc.code, "message": exc.message,
                     "path": exc.path if exc.path is not None else manifest_path}
        summary = {"total_runs": 0, "succeeded": 0, "failed": 0, "runs": []}
        try:
            write_manifest_outputs(out_dir, summary, [], [violation])
        except SynthError:
            pass
        raise

    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    spec_dir = os.path.join(out_dir, "out")
    try:
        os.makedirs(spec_dir, exist_ok=True)
    except OSError as exc:
        raise SynthError("INVALID_INPUT", "cannot write to output directory %s: %s"
                         % (out_dir, exc.strerror))

    runs = []
    warning_lines = []
    violations = []
    succeeded = 0
    for entry in entries:
        run_id = entry["run_id"]
        sink = WarningSink(run_id)
        status = "ok"
        try:
            doc = process_run(args, entry, manifest_dir, sink)
        except SynthError as exc:
            status = "error"
            violations.append({"run_id": run_id, "code": exc.code, "message": exc.message,
                               "path": exc.path if exc.path is not None else ""})
        except RecursionError:
            status = "error"
            violations.append({"run_id": run_id, "code": "PARSE_ERROR",
                               "message": "document nesting too deep", "path": ""})
        spec_path = os.path.join(spec_dir, run_id + ".json")
        if status == "ok":
            try:
                write_text(spec_path, canonical_json(doc))
                succeeded += 1
            except OSError as exc:
                raise SynthError("INVALID_INPUT", "cannot write %s: %s"
                                 % (spec_path, exc.strerror))
        else:
            if os.path.isfile(spec_path):
                try:
                    os.remove(spec_path)  # never leave a stale spec for a failed run
                except OSError:
                    pass
        warning_lines.extend(sink.lines())
        runs.append({
            "run_id": run_id,
            "status": status,
            "warnings": sink.count(),
            "conflicts": sink.conflicts(),
            "notes": entry["notes"],
        })

    summary = {
        "total_runs": len(entries),
        "succeeded": succeeded,
        "failed": len(entries) - succeeded,
        "runs": runs,
    }
    write_manifest_outputs(out_dir, summary, warning_lines, violations)
    if succeeded == 0:
        raise SynthError("RUN_FAILURE", "all %d run(s) failed" % len(entries))


def run(argv):
    args = parse_args(argv)
    if "manifest" in args:
        run_manifest(args)
    else:
        run_single(args)


def main():
    try:
        run(sys.argv[1:])
    except SynthError as exc:
        fail(exc.code, exc.message)
    sys.exit(0)


if __name__ == "__main__":
    main()
