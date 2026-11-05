#!/usr/bin/env python3
"""synth_configs: merge layered YAML/JSON configs into a canonical experiment spec."""

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
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code, message):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": message}}) + "\n")
    sys.exit(1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

KNOWN_FLAGS = ("base", "overrides", "flags", "env", "out")


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
        if name not in values:
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
            raise SynthError("PARSE_ERROR", "%s: non-finite number is not valid JSON" % label)
    else:
        raise SynthError("PARSE_ERROR", "%s: unsupported value type %s" % (label, type(value).__name__))


def read_text(path, label):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SynthError("MISSING_FILE", "%s: cannot read %s (%s)" % (label, path, exc.strerror))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SynthError("PARSE_ERROR", "%s: file is not valid UTF-8" % label)
    if text.startswith("﻿"):
        text = text[1:]
    return text


def parse_yaml(text, label):
    global _LOADER
    if yaml is None:
        raise SynthError("PARSE_ERROR", "%s: PyYAML is not installed" % label)
    if _LOADER is None:
        _LOADER = _build_loader()
    try:
        docs = list(yaml.load_all(text, Loader=_LOADER))
    except yaml.YAMLError as exc:
        msg = str(exc).replace("\n", " ")
        raise SynthError("PARSE_ERROR", "%s: invalid YAML: %s" % (label, msg))
    except RecursionError:
        raise SynthError("PARSE_ERROR", "%s: YAML nesting too deep or recursive alias" % label)
    if len(docs) > 1:
        raise SynthError("PARSE_ERROR", "%s: multiple YAML documents are not supported" % label)
    data = docs[0] if docs else None
    _check_json_compatible(data, label)
    return data


def parse_json(text, label):
    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise SynthError("PARSE_ERROR", "%s: duplicate key %r" % (label, k))
            obj[k] = v
        return obj

    def bad_constant(name):
        raise SynthError("PARSE_ERROR", "%s: invalid JSON constant %s" % (label, name))

    try:
        return json.loads(text, object_pairs_hook=hook, parse_constant=bad_constant)
    except json.JSONDecodeError as exc:
        raise SynthError("PARSE_ERROR", "%s: invalid JSON: %s" % (label, exc))
    except RecursionError:
        raise SynthError("PARSE_ERROR", "%s: JSON nesting too deep" % label)


def parse_env(path):
    """Parse KEY=VALUE lines; values are unused in Part 1."""
    result = {}
    if not os.path.isfile(path):
        return result
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value
    return result


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
    return token.replace("~", "~0").replace("/", "~1")


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
        warnings.append("CONFLICT %s %s -> %s" % (path, lower.source, source))
    return wrap(higher_value, source)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(doc):
    if not isinstance(doc, dict):
        raise SynthError("INVALID_STRUCTURE",
                         "merged document must be an object, got %s" % json_type(doc))
    for key in ("experiment_name", "training", "resources"):
        if key not in doc:
            raise SynthError("MISSING_KEY", "%s is required" % key)

    name = doc["experiment_name"]
    if not isinstance(name, str):
        raise SynthError("INVALID_VALUE", "experiment_name must be a string")
    if not name.strip() or not any(not c.isspace() and c.isprintable() for c in name):
        raise SynthError("INVALID_VALUE", "experiment_name must contain non-whitespace characters")

    if not isinstance(doc["training"], dict):
        raise SynthError("INVALID_VALUE", "training must be an object")

    resources = doc["resources"]
    if not isinstance(resources, dict):
        raise SynthError("INVALID_VALUE", "resources must be an object")
    if "accelerators" not in resources and "profile" not in resources:
        raise SynthError("MISSING_KEY", "resources.accelerators or resources.profile is required")

    if "accelerators" in resources:
        accs = resources["accelerators"]
        if not isinstance(accs, list):
            raise SynthError("INVALID_VALUE", "resources.accelerators must be an array")
        for idx, acc in enumerate(accs):
            where = "resources.accelerators[%d]" % idx
            if not isinstance(acc, dict):
                raise SynthError("INVALID_VALUE", "%s must be an object" % where)
            if "type" not in acc or not isinstance(acc["type"], str):
                raise SynthError("INVALID_VALUE", "%s.type must be a string" % where)
            if "count" not in acc or not is_positive_int(acc["count"]):
                raise SynthError("INVALID_VALUE", "%s.count must be a positive integer" % where)

    if "profile" in resources and not isinstance(resources["profile"], str):
        raise SynthError("INVALID_VALUE", "resources.profile must be a string")

    if "world_size" in resources and not is_positive_int(resources["world_size"]):
        raise SynthError("INVALID_VALUE", "resources.world_size must be a positive integer")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(argv):
    args = parse_args(argv)

    base_path = args["base"]
    if not os.path.isfile(base_path):
        raise SynthError("MISSING_FILE", "base.yaml: file not found: %s" % base_path)
    defaults_path = os.path.join(args["overrides"], "defaults.yaml")
    if not os.path.isfile(defaults_path):
        raise SynthError("MISSING_FILE", "defaults.yaml: file not found: %s" % defaults_path)
    run_path = os.path.join(args["overrides"], "run.yaml")
    flags_path = args["flags"]

    layers = []
    layers.append(("base.yaml", parse_yaml(read_text(base_path, "base.yaml"), "base.yaml")))
    defaults = parse_yaml(read_text(defaults_path, "defaults.yaml"), "defaults.yaml")
    layers.append(("defaults.yaml", defaults))
    if os.path.isfile(run_path):
        layers.append(("run.yaml", parse_yaml(read_text(run_path, "run.yaml"), "run.yaml")))
    if os.path.isfile(flags_path):
        text = read_text(flags_path, "flags.json")
        if text.strip():
            layers.append(("flags.json", parse_json(text, "flags.json")))

    parse_env(args["env"])

    warnings = []
    merged = None
    for source, data in layers:
        if data is None:
            continue  # empty document: absent layer
        if merged is None:
            merged = wrap(data, source)
        else:
            merged = merge_into(merged, data, source, "", warnings)

    doc = unwrap(merged) if merged is not None else None

    # Defaults injection: fill missing top-level sections only.
    if isinstance(doc, dict) and isinstance(defaults, dict):
        for key, value in defaults.items():
            if key not in doc:
                doc[key] = value

    validate(doc)

    out_dir = args["out"]
    try:
        os.makedirs(out_dir, exist_ok=True)
        spec = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(out_dir, "final_spec.json"), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(spec + "\n")
        with open(os.path.join(out_dir, "warnings.txt"), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write("".join(w + "\n" for w in warnings))
    except OSError as exc:
        raise SynthError("INVALID_INPUT", "cannot write to output directory %s: %s"
                         % (out_dir, exc.strerror))


def main():
    try:
        run(sys.argv[1:])
    except SynthError as exc:
        fail(exc.code, exc.message)
    sys.exit(0)


if __name__ == "__main__":
    main()
