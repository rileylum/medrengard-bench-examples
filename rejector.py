#!/usr/bin/env python3
"""rejector: run prompt tasks against an OpenAI-compatible chat/completions API."""

import argparse
import copy
import json
import math
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SCHEMES = ("greedy", "sample", "rejection", "agentic")
API_TYPES = ("chat", "completions")
CHAT_TEMPLATES = ("chatml", "llama3", "mistral", "zephyr")
HANDLER_TYPES = ("echo", "static_map", "script")
EVAL_TYPES = ("exact_match", "contains", "regex", "script", "llm_judge")
EXTRACTS = ("last_number", "last_line", "full", "letter", "first_number")
ICL_STRATEGIES = ("fixed", "random", "round_robin")
MAX_HTTP_TRIES = 3  # a request is abandoned after its third 5xx failure
REQUEST_TIMEOUT = 600
SCRIPT_TIMEOUT = 10
RESPONSE_KEY = "__response__"
PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
NUMBER_RE = re.compile(r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)")


class ConfigError(Exception):
    pass


class BudgetExceeded(BaseException):
    """Raised when a request may not be sent because the cost budget is exhausted.

    Derives from BaseException so generic `except Exception` handlers do not swallow it.
    """


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- config


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and not math.isnan(v) and not math.isinf(v))


def deep_merge(base, override):
    """Merge mapping `override` on top of `base` (recursively for nested mappings)."""
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_config(path):
    if yaml is None:
        raise ConfigError("PyYAML is not installed (pip install -r requirements.txt)")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in config file {path}: {e}")
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping with a top-level 'task' or 'tasks' key")
    return raw


def _top_defaults(raw):
    """Defaults applied to every task: top-level rate_limits/cost (lowest priority), then `defaults`."""
    base = {}
    for key in ("rate_limits", "cost"):
        if isinstance(raw.get(key), dict):
            base[key] = copy.deepcopy(raw[key])
    defaults = raw.get("defaults")
    if isinstance(defaults, dict):
        base = deep_merge(base, defaults)
    elif defaults is not None:
        raise ConfigError("config 'defaults' must be a mapping")
    return base


def parse_config(raw):
    """Return (multi, [(name, task_mapping, prefix), ...]) without validating task contents."""
    if "task" in raw:
        task = raw["task"]
        if not isinstance(task, dict):
            raise ConfigError("config 'task' must be a mapping")
        defaults = _top_defaults(raw)
        if defaults:
            task = deep_merge(defaults, task)
        name = task.get("name")
        if name is not None and not isinstance(name, str):
            name = str(name)
        return False, [(name, task, "task")]
    if "tasks" not in raw:
        raise ConfigError("config must contain a top-level 'task' or 'tasks' mapping")
    tasks = raw["tasks"]
    if not isinstance(tasks, dict) or not tasks:
        raise ConfigError("config 'tasks' must be a non-empty mapping of task name to task settings")
    defaults = raw.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ConfigError("config 'defaults' must be a mapping")
    defaults = _top_defaults(raw)
    out = []
    for name, t in tasks.items():
        name = str(name)
        if not name or "/" in name or name in (".", ".."):
            raise ConfigError(f"invalid task name {name!r}")
        if t is None:
            t = {}
        if not isinstance(t, dict):
            raise ConfigError(f"tasks.{name} must be a mapping")
        out.append((name, deep_merge(defaults, t), f"tasks.{name}"))
    return True, out


def _template(value, where, required):
    if value is None:
        if required:
            raise ConfigError(f"{where} is required and must be a string")
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string")
    return value


def build_task(name, task, P, args, config_dir="."):
    cfg = {"name": name}

    api_url = args.api_url if args.api_url is not None else task.get("api_url")
    if not isinstance(api_url, str) or not api_url.strip():
        raise ConfigError(f"{P}.api_url is required and must be a non-empty string")
    cfg["api_url"] = api_url.strip().rstrip("/")

    model = args.model if args.model is not None else task.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{P}.model is required and must be a non-empty string")
    cfg["model"] = model

    rl = task.get("rate_limits")
    if rl is None:
        rl = {}
    if not isinstance(rl, dict):
        raise ConfigError(f"{P}.rate_limits must be a mapping")
    if args.rpm is not None:
        rpm = args.rpm
    elif "rpm" in rl:
        rpm = rl.get("rpm")
    else:
        rpm = task.get("rpm")
    if rpm is not None and (not _is_int(rpm) or rpm <= 0):
        raise ConfigError(f"{P}.rate_limits.rpm must be a positive integer, got {rpm!r}")
    cfg["rpm"] = rpm
    tpm = args.tpm if getattr(args, "tpm", None) is not None else rl.get("tpm")
    if tpm is not None and (not _is_int(tpm) or tpm <= 0):
        raise ConfigError(f"{P}.rate_limits.tpm must be a positive integer, got {tpm!r}")
    cfg["tpm"] = tpm
    mc = (args.max_concurrent if getattr(args, "max_concurrent", None) is not None
          else rl.get("max_concurrent"))
    if mc is not None and (not _is_int(mc) or mc <= 0):
        raise ConfigError(f"{P}.rate_limits.max_concurrent must be a positive integer, got {mc!r}")
    cfg["max_concurrent"] = mc

    cfg["cost"] = build_cost(task.get("cost"), P, args)
    cfg["output_schema"] = build_schema(task.get("output_schema"), P)

    prompt = task.get("prompt")
    if not isinstance(prompt, dict):
        raise ConfigError(f"{P}.prompt is required and must be a mapping with 'user' (and optional 'system')")
    cfg["system"] = _template(prompt.get("system"), f"{P}.prompt.system", False)
    cfg["user"] = _template(prompt.get("user"), f"{P}.prompt.user", True)

    gen = task.get("generation", {})
    if gen is None:
        gen = {}
    if not isinstance(gen, dict):
        raise ConfigError(f"{P}.generation must be a mapping")

    scheme = args.scheme if args.scheme is not None else gen.get("scheme", "greedy")
    if scheme not in SCHEMES:
        raise ConfigError(f"{P}.generation.scheme must be one of {', '.join(SCHEMES)}, got {scheme!r}")
    cfg["scheme"] = scheme

    max_tokens = args.max_tokens if args.max_tokens is not None else gen.get("max_tokens", 512)
    if not _is_int(max_tokens) or max_tokens <= 0:
        raise ConfigError(f"{P}.generation.max_tokens must be a positive integer, got {max_tokens!r}")
    cfg["max_tokens"] = max_tokens

    temperature = args.temperature if args.temperature is not None else gen.get("temperature")
    if temperature is not None and not _is_num(temperature):
        raise ConfigError(f"{P}.generation.temperature must be a number, got {temperature!r}")
    if scheme == "greedy":
        temperature = 0.0
    elif scheme == "agentic":
        if temperature is None:
            temperature = 0.0
        if temperature < 0:
            raise ConfigError(f"temperature must be >= 0 for scheme 'agentic' ({P}), got {temperature!r}")
    else:
        if temperature is None:
            temperature = 1.0
        if temperature <= 0:
            raise ConfigError(f"temperature must be > 0 for scheme '{scheme}' ({P}), got {temperature!r}")
    cfg["temperature"] = float(temperature)

    n = args.n if args.n is not None else gen.get("n", 1)
    if scheme == "rejection":
        if not _is_int(n) or n < 1:
            raise ConfigError(f"{P}.generation.n must be a positive integer for rejection, got {n!r}")
    else:
        n = 1
    cfg["n"] = n

    cfg["evaluation"] = build_evaluation(task.get("evaluation"), P, scheme, model, args,
                                         has_schema=cfg["output_schema"] is not None)

    of = task.get("output_field", "output")
    if not isinstance(of, str) or not of:
        raise ConfigError(f"{P}.output_field must be a non-empty string")
    cfg["output_field"] = of

    ns = args.num_solutions if getattr(args, "num_solutions", None) is not None else task.get("num_solutions")
    if ns is None:
        ns = gen.get("num_solutions", 1)
    if not _is_int(ns) or ns < 1:
        raise ConfigError(f"{P}.num_solutions must be a positive integer, got {ns!r}")
    cfg["num_solutions"] = ns

    cfg["icl"] = build_icl(task.get("icl"), P, cfg, args, config_dir)
    cfg["list_mode"] = cfg["icl"] is not None or ns > 1

    ma = args.max_attempts if getattr(args, "max_attempts", None) is not None else gen.get("max_attempts")
    if ma is not None and (not _is_int(ma) or ma < 1):
        raise ConfigError(f"{P}.generation.max_attempts must be a positive integer, got {ma!r}")
    if ma is None:
        ma = 3 * ns
    cfg["max_attempts"] = ma

    api_type = args.api_type if getattr(args, "api_type", None) is not None else task.get("api_type", "chat")
    if api_type is None:
        api_type = "chat"
    if api_type not in API_TYPES:
        raise ConfigError(f"{P}.api_type must be one of {', '.join(API_TYPES)}, got {api_type!r}")
    cfg["api_type"] = api_type
    ct = args.chat_template if getattr(args, "chat_template", None) is not None else task.get("chat_template")
    if ct is None:
        ct = "chatml"
    if ct not in CHAT_TEMPLATES:
        raise ConfigError(f"{P}.chat_template must be one of {', '.join(CHAT_TEMPLATES)}, got {ct!r}")
    cfg["chat_template"] = ct

    mi = gen.get("max_iterations", 10)
    if mi is None:
        mi = 10
    if not _is_int(mi) or mi < 1:
        raise ConfigError(f"{P}.generation.max_iterations must be a positive integer, got {mi!r}")
    cfg["max_iterations"] = mi
    cfg["tools"] = build_tools(task.get("tools"), P)
    return cfg


def build_cost(cost, P, args):
    C = f"{P}.cost"
    budget_cli = getattr(args, "budget", None)
    if cost is None:
        if budget_cli is None:
            return None
        cost = {}
    if not isinstance(cost, dict):
        raise ConfigError(f"{C} must be a mapping")
    out = {}
    for key in ("prompt_cost_per_1k", "completion_cost_per_1k"):
        v = cost.get(key, 0.0)
        if v is None:
            v = 0.0
        if not _is_num(v) or v < 0:
            raise ConfigError(f"{C}.{key} must be a non-negative number, got {v!r}")
        out[key] = float(v)
    budget = budget_cli if budget_cli is not None else cost.get("budget")
    if budget is not None and (not _is_num(budget) or budget < 0):
        raise ConfigError(f"{C}.budget must be a non-negative number, got {budget!r}")
    out["budget"] = float(budget) if budget is not None else None
    return out


SCHEMA_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")


def _check_schema(schema, where):
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ConfigError(f"{where} must be a mapping")
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        for x in types:
            if x not in SCHEMA_TYPES:
                raise ConfigError(f"{where}.type must be one of {', '.join(SCHEMA_TYPES)}, got {x!r}")
    req = schema.get("required")
    if req is not None and (not isinstance(req, list) or not all(isinstance(r, str) for r in req)):
        raise ConfigError(f"{where}.required must be a list of strings")
    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            raise ConfigError(f"{where}.properties must be a mapping")
        for k, sub in props.items():
            _check_schema(sub, f"{where}.properties.{k}")
    items = schema.get("items")
    if items is not None:
        if isinstance(items, list):
            for i, sub in enumerate(items):
                _check_schema(sub, f"{where}.items[{i}]")
        else:
            _check_schema(items, f"{where}.items")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ConfigError(f"{where}.enum must be a list")
    for key in ("minimum", "maximum"):
        if key in schema and not _is_num(schema[key]):
            raise ConfigError(f"{where}.{key} must be a number")
    for key in ("minLength", "maxLength"):
        if key in schema and (not _is_int(schema[key]) or schema[key] < 0):
            raise ConfigError(f"{where}.{key} must be a non-negative integer")
    if "pattern" in schema:
        pat = schema["pattern"]
        if not isinstance(pat, str):
            raise ConfigError(f"{where}.pattern must be a string")
        try:
            re.compile(pat)
        except re.error as e:
            raise ConfigError(f"{where}.pattern is not a valid regex: {e}")


def build_schema(schema, P):
    if schema is None:
        return None
    if not isinstance(schema, dict):
        raise ConfigError(f"{P}.output_schema must be a mapping")
    _check_schema(schema, f"{P}.output_schema")
    return schema


def build_tools(tools, P):
    T = f"{P}.tools"
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ConfigError(f"{T} must be a list")
    out = []
    seen = set()
    for i, t in enumerate(tools):
        W = f"{T}[{i}]"
        if not isinstance(t, dict):
            raise ConfigError(f"{W} must be a mapping")
        name = t.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{W}.name is required and must be a non-empty string")
        if name in seen:
            raise ConfigError(f"{W}: duplicate tool name {name!r}")
        seen.add(name)
        desc = t.get("description", "")
        if desc is None:
            desc = ""
        if not isinstance(desc, str):
            raise ConfigError(f"{W}.description must be a string")
        params = t.get("parameters")
        if params is None:
            params = {"type": "object", "properties": {}}
        if not isinstance(params, dict):
            raise ConfigError(f"{W}.parameters must be a mapping")
        req = params.get("required") or []
        if not isinstance(req, list):
            raise ConfigError(f"{W}.parameters.required must be a list")
        props = params.get("properties") or {}
        if not isinstance(props, dict):
            raise ConfigError(f"{W}.parameters.properties must be a mapping")
        h = t.get("handler")
        if not isinstance(h, dict):
            raise ConfigError(f"{W}.handler is required and must be a mapping")
        ht = h.get("type")
        if ht not in HANDLER_TYPES:
            raise ConfigError(f"{W}.handler.type must be one of {', '.join(HANDLER_TYPES)}, got {ht!r}")
        handler = {"type": ht}
        key_field = None
        if req:
            key_field = str(req[0])
        elif props:
            key_field = str(next(iter(props)))
        if ht == "static_map":
            mapping = h.get("mapping")
            if mapping is None:
                mapping = {}
            if not isinstance(mapping, dict):
                raise ConfigError(f"{W}.handler.mapping must be a mapping")
            default = h.get("default", "NOT_FOUND")
            if default is None:
                default = "NOT_FOUND"
            handler["mapping"] = {_stringify(k): _stringify(v) for k, v in mapping.items()}
            handler["default"] = _stringify(default)
            handler["key_field"] = key_field
        elif ht == "script":
            cmd = h.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                raise ConfigError(f"{W}.handler.command is required for handler type 'script'")
            try:
                argv = shlex.split(cmd)
            except ValueError as e:
                raise ConfigError(f"{W}.handler.command cannot be parsed: {e}")
            af = h.get("arg_field")
            if af is None:
                af = key_field
            if not isinstance(af, str) or not af:
                raise ConfigError(f"{W}.handler.arg_field is required for handler type 'script'")
            handler["argv"] = argv
            handler["arg_field"] = af
            to = h.get("timeout", SCRIPT_TIMEOUT)
            if not _is_num(to) or to <= 0:
                raise ConfigError(f"{W}.handler.timeout must be a positive number")
            handler["timeout"] = to
        out.append({"name": name, "description": desc, "parameters": params, "handler": handler})
    return out


def _check_example(ex, where):
    if not isinstance(ex, dict):
        raise ConfigError(f"{where} must be an object with 'input' and 'output'")
    if "input" not in ex or "output" not in ex:
        raise ConfigError(f"{where} must have both 'input' and 'output' keys")
    if not isinstance(ex["input"], dict):
        raise ConfigError(f"{where}.input must be an object")
    if not isinstance(ex["output"], str):
        raise ConfigError(f"{where}.output must be a string")
    return {"input": ex["input"], "output": ex["output"]}


def _load_icl_file(path, where):
    examples = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ConfigError(f"{where}: {path} line {lineno} is not valid JSON: {e}")
                examples.append(_check_example(obj, f"{where}: {path} line {lineno}"))
    except OSError as e:
        raise ConfigError(f"{where}: cannot read ICL file {path}: {e}")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{where}: ICL file {path} is not valid UTF-8: {e}")
    return examples


def build_icl(icl, P, cfg, args, config_dir):
    I = f"{P}.icl"
    if icl is None:
        return None
    if not isinstance(icl, dict):
        raise ConfigError(f"{I} must be a mapping")
    setups_raw = icl.get("setups")
    if not isinstance(setups_raw, list) or not setups_raw:
        raise ConfigError(f"{I}.setups must be a non-empty list")
    k = args.icl_k if getattr(args, "icl_k", None) is not None else icl.get("k")
    if k is not None and (not _is_int(k) or k < 1):
        raise ConfigError(f"{I}.k must be a positive integer, got {k!r}")
    strategy = args.icl_strategy if getattr(args, "icl_strategy", None) is not None else icl.get("strategy")
    if strategy is None:
        strategy = "fixed"
    if strategy not in ICL_STRATEGIES:
        raise ConfigError(f"{I}.strategy must be one of {', '.join(ICL_STRATEGIES)}, got {strategy!r}")
    setups = []
    seen = set()
    for i, st in enumerate(setups_raw):
        W = f"{I}.setups[{i}]"
        if not isinstance(st, dict):
            raise ConfigError(f"{W} must be a mapping")
        name = st.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{W}.name is required and must be a non-empty string")
        if name in seen:
            raise ConfigError(f"{W}: duplicate setup name {name!r}")
        seen.add(name)
        has_ex = st.get("examples") is not None
        has_file = st.get("file") is not None
        if has_ex == has_file:
            raise ConfigError(f"{W} ({name}) must have exactly one of 'examples' or 'file'")
        if has_ex:
            exs = st["examples"]
            if not isinstance(exs, list):
                raise ConfigError(f"{W}.examples must be a list")
            examples = [_check_example(ex, f"{W}.examples[{j}]") for j, ex in enumerate(exs)]
        else:
            fp = st["file"]
            if not isinstance(fp, str) or not fp:
                raise ConfigError(f"{W}.file must be a non-empty string")
            path = fp if os.path.isabs(fp) else os.path.join(config_dir, fp)
            examples = _load_icl_file(path, f"{W} ({name})")
        if not examples:
            raise ConfigError(f"{W} ({name}) has no examples")
        if k is not None:
            examples = examples[:k]
        turns = []
        for j, ex in enumerate(examples):
            try:
                u = render(cfg["user"], ex["input"], j, f"prompt.user template (ICL setup {name!r} example)")
            except ConfigError as e:
                raise ConfigError(f"{W} ({name}): example {e}")
            turns.append({"role": "user", "content": u})
            turns.append({"role": "assistant", "content": ex["output"]})
        setups.append({"name": name, "turns": turns})
    return {"setups": setups, "k": k, "strategy": strategy}


def build_evaluation(ev, P, scheme, model, args, has_schema=False):
    E = f"{P}.evaluation"
    if ev is None:
        if scheme == "rejection" and not has_schema:
            raise ConfigError(f"{E} (or {P}.output_schema) is required for scheme 'rejection'")
        return None
    if not isinstance(ev, dict):
        raise ConfigError(f"{E} must be a mapping")
    etype = ev.get("type")
    if etype not in EVAL_TYPES:
        raise ConfigError(f"{E}.type must be one of {', '.join(EVAL_TYPES)}, got {etype!r}")
    default_extract = "first_number" if etype == "llm_judge" else "full"
    extract = ev.get("extract", default_extract)
    if extract is None:
        extract = default_extract
    if extract not in EXTRACTS:
        raise ConfigError(f"{E}.extract must be one of {', '.join(EXTRACTS)}, got {extract!r}")
    e = {"type": etype, "extract": extract, "answer_field": None, "pattern": None}
    if etype in ("exact_match", "contains"):
        af = ev.get("answer_field")
        if not isinstance(af, str) or not af:
            raise ConfigError(f"{E}.answer_field is required for evaluation type '{etype}'")
        e["answer_field"] = af
    elif etype == "regex":
        pat = ev.get("pattern")
        if not isinstance(pat, str) or not pat:
            raise ConfigError(f"{E}.pattern is required for evaluation type 'regex'")
        try:
            e["regex"] = re.compile(pat)
        except re.error as err:
            raise ConfigError(f"{E}.pattern is not a valid regex: {err}")
        e["pattern"] = pat
    elif etype == "script":
        tmpl = ev.get("command_template")
        if not isinstance(tmpl, str) or not tmpl.strip():
            raise ConfigError(f"{E}.command_template is required for evaluation type 'script'")
        code = ev.get("success_exit_code", 0)
        if code is None:
            code = 0
        if not _is_int(code):
            raise ConfigError(f"{E}.success_exit_code must be an integer, got {code!r}")
        e["command_template"] = tmpl
        e["success_exit_code"] = code
    else:  # llm_judge
        jp = ev.get("judge_prompt")
        if not isinstance(jp, dict):
            raise ConfigError(f"{E}.judge_prompt is required for evaluation type 'llm_judge' "
                              "and must be a mapping with 'user' (and optional 'system')")
        e["judge_system"] = _template(jp.get("system"), f"{E}.judge_prompt.system", False)
        e["judge_user"] = _template(jp.get("user"), f"{E}.judge_prompt.user", True)
        thr = ev.get("threshold")
        if not _is_num(thr):
            raise ConfigError(f"{E}.threshold is required for evaluation type 'llm_judge' "
                              f"and must be a number, got {thr!r}")
        e["threshold"] = thr
        jm = args.eval_model if args.eval_model is not None else ev.get("model")
        if jm is None:
            jm = model
        if not isinstance(jm, str) or not jm.strip():
            raise ConfigError(f"{E}.model must be a non-empty string")
        e["model"] = jm
    return e


# ---------------------------------------------------------------- input


def load_input(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ConfigError(f"{path}: input line {lineno} (row {len(rows)}) is not valid JSON: {e}")
                if not isinstance(obj, dict):
                    raise ConfigError(f"{path}: input line {lineno} (row {len(rows)}) must be a JSON object")
                rows.append(obj)
    except OSError as e:
        raise ConfigError(f"cannot read input file {path}: {e}")
    return rows


def _stringify(v):
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def render(template, row, idx, what="prompt template", response=None, nested_response=False):
    """Substitute {field} placeholders.

    `response` (if not None) is used for {__response__}. With `nested_response`,
    occurrences of {__response__} inside substituted row values are replaced too.
    """
    def sub(m):
        key = m.group(1)
        if key == RESPONSE_KEY and response is not None:
            return response
        if key not in row:
            raise ConfigError(f"row {idx}: missing field '{key}' referenced by {what}")
        val = _stringify(row[key])
        if nested_response and response is not None:
            val = val.replace("{" + RESPONSE_KEY + "}", response)
        return val
    return PLACEHOLDER_RE.sub(sub, template)


def check_fields(template, row, idx, what):
    for key in PLACEHOLDER_RE.findall(template):
        if key != RESPONSE_KEY and key not in row:
            raise ConfigError(f"row {idx}: missing field '{key}' referenced by {what}")


def _messages(system, user):
    msgs = []
    if system is not None:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return msgs


def prepare(cfg, rows):
    prepared = []
    ev = cfg["evaluation"]
    label = f"task '{cfg['name']}' " if cfg.get("name") else ""
    for idx, row in enumerate(rows):
        try:
            system = render(cfg["system"], row, idx) if cfg["system"] is not None else None
            user = render(cfg["user"], row, idx)
            if ev is not None:
                if ev["answer_field"] is not None and ev["answer_field"] not in row:
                    raise ConfigError(f"row {idx}: missing field '{ev['answer_field']}' (evaluation answer_field)")
                if ev["type"] == "script":
                    check_fields(ev["command_template"], row, idx, "evaluation command_template")
                elif ev["type"] == "llm_judge":
                    if ev["judge_system"] is not None:
                        check_fields(ev["judge_system"], row, idx, "judge_prompt.system")
                    check_fields(ev["judge_user"], row, idx, "judge_prompt.user")
        except ConfigError as e:
            raise ConfigError(f"{label}{e}")
        if cfg.get("icl") is None:
            prepared.append(_messages(system, user))
        else:
            per_setup = []
            for st in cfg["icl"]["setups"]:
                msgs = [] if system is None else [{"role": "system", "content": system}]
                msgs.extend(copy.deepcopy(st["turns"]))
                msgs.append({"role": "user", "content": user})
                per_setup.append(msgs)
            prepared.append(per_setup)
    return prepared


# ---------------------------------------------------------------- evaluation


ANSWER_LETTER_RE = re.compile(r"\banswer\b\s*(?:is\s*)?[:\-]?\s*[\(\[]?\s*([A-D])\b(?![A-Za-z'])",
                              re.IGNORECASE)
LETTER_RE = re.compile(r"(?<![A-Za-z0-9_'])([A-D])(?![A-Za-z0-9_'])")


def extract_letter(text):
    m = ANSWER_LETTER_RE.search(text)
    if m and m.group(1).isupper():
        return m.group(1)
    for m in LETTER_RE.finditer(text):
        ch = m.group(1)
        after = text[m.end():]
        # skip the English article "A" ("A good answer ..."), but not "A)" / "A." / "A"
        if ch == "A" and re.match(r"\s+[a-z]", after) and not re.match(r"[\)\]\.:]", after):
            continue
        return ch
    return None


def extract(text, method):
    if method == "full":
        return text
    if method == "last_line":
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1].strip() if lines else None
    if method == "letter":
        return extract_letter(text)
    nums = NUMBER_RE.findall(text)
    if not nums:
        return None
    num = nums[0] if method == "first_number" else nums[-1]
    return num.replace(",", "").lstrip("+")


def _num(s):
    try:
        return float(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def evaluate_local(ev, text, row):
    """Evaluate exact_match/contains/regex. Return (passed, extracted_answer)."""
    if ev["type"] == "regex":
        m = ev["regex"].search(text)
        if not m:
            return False, None
        val = m.group(1) if m.re.groups >= 1 and m.group(1) is not None else m.group(0)
        return True, val
    expected = _stringify(row.get(ev["answer_field"]))
    extracted = extract(text, ev["extract"])
    if ev["type"] == "contains":
        return (expected in text), extracted
    # exact_match
    if extracted is None:
        return False, None
    a, b = extracted.strip(), expected.strip()
    if a == b:
        return True, extracted
    if ev["extract"] == "letter" and a.upper() == b.upper():
        return True, extracted
    na, nb = _num(a), _num(b)
    if ev["extract"] in ("last_number", "first_number") and na is not None and nb is not None:
        return (na == nb), extracted
    return False, extracted


def run_script(command):
    """Run `command` in a shell. Return exit code, or None on timeout/launch failure."""
    try:
        proc = subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError:
        return None
    try:
        proc.communicate(timeout=SCRIPT_TIMEOUT)  # output captured, then discarded
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return None
    return proc.returncode


def evaluate_script(ev, text, row, idx):
    cmd = render(ev["command_template"], row, idx, "evaluation command_template",
                 response=text, nested_response=True)
    code = run_script(cmd)
    return (code is not None and code == ev["success_exit_code"]), None


def _score_value(s):
    v = _num(s) if s is not None else None
    if v is None or math.isnan(v) or math.isinf(v):
        return None
    if v == int(v):
        return int(v)
    return v


def evaluate_judge(client, cfg, ev, text, row, idx):
    """Return (passed, extracted, judge_score, judge_meta)."""
    system = (render(ev["judge_system"], row, idx, "judge_prompt.system", response=text)
              if ev["judge_system"] is not None else None)
    user = render(ev["judge_user"], row, idx, "judge_prompt.user", response=text)
    ok, jtext, jmeta = client.complete(_messages(system, user), ev["model"], 0.0, cfg["max_tokens"])
    if not ok:
        return False, None, None, jmeta
    extracted = extract(jtext, ev["extract"])
    score = _score_value(extracted)
    if score is None:
        return False, extracted, None, jmeta
    return score >= ev["threshold"], extracted, score, jmeta


# ---------------------------------------------------------------- structured output


FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\n?(.*?)```", re.DOTALL)


def _json_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _type_ok(v, t):
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer())
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return True


def _json_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _join_path(path, key):
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def schema_errors(schema, v, path=""):
    """Return a human-readable message for the first violation, or None if `v` is valid."""
    if schema is False:
        return f"{'Field ' + repr(path) + ': ' if path else ''}no value is allowed"
    if not isinstance(schema, dict):
        return None
    at = f"Field '{path}': " if path else ""
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in types):
            return f"{at}expected type {' or '.join(types)}, got {_json_type(v)}"
    if "enum" in schema and not any(_json_equal(v, e) for e in schema["enum"]):
        return f"{at}value {json.dumps(v, ensure_ascii=False)} is not one of " \
               f"{json.dumps(schema['enum'], ensure_ascii=False)}"
    if isinstance(v, str):
        if "minLength" in schema and len(v) < schema["minLength"]:
            return f"{at}string length {len(v)} is shorter than minLength {schema['minLength']}"
        if "maxLength" in schema and len(v) > schema["maxLength"]:
            return f"{at}string length {len(v)} is longer than maxLength {schema['maxLength']}"
        if "pattern" in schema and not re.search(schema["pattern"], v):
            return f"{at}string {json.dumps(v, ensure_ascii=False)} does not match pattern " \
                   f"{json.dumps(schema['pattern'])}"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            return f"{at}value {v} is less than minimum {schema['minimum']}"
        if "maximum" in schema and v > schema["maximum"]:
            return f"{at}value {v} is greater than maximum {schema['maximum']}"
    if isinstance(v, dict):
        for r in schema.get("required") or []:
            if r not in v:
                return f"Missing required field: {_join_path(path, r)}"
        for k, sub in (schema.get("properties") or {}).items():
            if k in v:
                err = schema_errors(sub, v[k], _join_path(path, k))
                if err:
                    return err
    if isinstance(v, list) and "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            for i, (x, sub) in enumerate(zip(v, items)):
                err = schema_errors(sub, x, _join_path(path, i))
                if err:
                    return err
        else:
            for i, x in enumerate(v):
                err = schema_errors(items, x, _join_path(path, i))
                if err:
                    return err
    return None


def parse_json_response(text):
    """Return (ok, value, error)."""
    if text is None:
        return False, None, "Invalid JSON: empty response"
    s = text.strip()
    try:
        return True, json.loads(s), None
    except (json.JSONDecodeError, ValueError) as e:
        first = e
    m = FENCE_RE.search(s)
    if m:
        try:
            return True, json.loads(m.group(1).strip()), None
        except (json.JSONDecodeError, ValueError):
            pass
    if not s:
        return False, None, "Invalid JSON: empty response"
    return False, None, f"Invalid JSON: {first}"


def validate_output(schema, text):
    """Return (valid, parsed_value, error_message)."""
    ok, value, err = parse_json_response(text)
    if not ok:
        return False, None, err
    err = schema_errors(schema, value)
    if err:
        return False, None, err
    return True, value, None


# ---------------------------------------------------------------- client


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.first_start = None
        self.last_end = None

    def start(self, t0):
        with self.lock:
            self.calls += 1
            if self.first_start is None or t0 < self.first_start:
                self.first_start = t0

    def end(self, t1):
        with self.lock:
            if self.last_end is None or t1 > self.last_end:
                self.last_end = t1


class Transport:
    """Posts JSON bodies to one server."""

    def __init__(self, api_url, stats):
        self.base = api_url
        self.stats = stats

    def post(self, body, counter, path="/v1/chat/completions"):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        self.stats.start(t0)
        counter.incr()
        status, payload, err = None, None, None
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                status = resp.status
                payload = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                payload = e.read()
            except Exception:
                payload = b""
            err = f"HTTP {e.code}"
        except Exception as e:  # connection errors, timeouts
            err = f"{type(e).__name__}: {e}"
        t1 = time.monotonic()
        self.stats.end(t1)
        return status, payload, err, int(round((t1 - t0) * 1000))


class Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = 0

    def incr(self):
        with self.lock:
            self.value += 1


class CostTracker:
    """Global running cost across all tasks, with an optional budget."""

    def __init__(self, budget):
        self.lock = threading.Lock()
        self.budget = budget
        self.prompt = 0.0
        self.completion = 0.0
        self.exceeded = budget is not None and budget <= 0
        self.limiters = []

    @property
    def total(self):
        return self.prompt + self.completion

    def add(self, prompt_cost, completion_cost):
        wake = False
        with self.lock:
            self.prompt += prompt_cost
            self.completion += completion_cost
            if (self.budget is not None and not self.exceeded
                    and self.prompt + self.completion >= self.budget):
                self.exceeded = True
                wake = True
        if wake:
            for lim in self.limiters:
                lim.wake()


WINDOW = 60.0


class Limiter:
    """Per-task scheduler: sliding-window RPM and TPM budgets plus a concurrency cap.

    Rate capacity is granted in FIFO order. Concurrency slots are separate so a
    thread that already holds a slot (an agentic loop) never waits behind one
    that needs a slot.
    """

    def __init__(self, rpm, tpm, max_concurrent, cost):
        self.rpm = rpm
        self.tpm = tpm
        self.max_concurrent = max_concurrent
        self.cost = cost
        self.cond = threading.Condition()
        self.inflight = 0
        self.req_times = deque()
        self.tok = deque()  # entries [timestamp, tokens, in_flight]
        self.queue = deque()
        self.slot_queue = deque()
        self.local = threading.local()
        cost.limiters.append(self)

    def wake(self):
        with self.cond:
            self.cond.notify_all()

    # -- concurrency slots
    def _take_slot(self):
        with self.cond:
            me = object()
            self.slot_queue.append(me)
            try:
                while True:
                    if self.cost.exceeded:
                        raise BudgetExceeded()
                    if self.slot_queue[0] is me and self.inflight < self.max_concurrent:
                        self.inflight += 1
                        return
                    self.cond.wait(1.0)
            finally:
                self.slot_queue.remove(me)
                self.cond.notify_all()

    def _give_slot(self):
        with self.cond:
            self.inflight -= 1
            self.cond.notify_all()

    def holding(self):
        return getattr(self.local, "held", False)

    class _Hold:
        def __init__(self, lim):
            self.lim = lim
            self.owner = False

        def __enter__(self):
            if not self.lim.holding():
                self.lim._take_slot()
                self.lim.local.held = True
                self.owner = True
            return self

        def __exit__(self, *exc):
            if self.owner:
                self.lim.local.held = False
                self.lim._give_slot()
            return False

    def hold(self):
        """Context manager holding one concurrency slot for its whole lifetime."""
        return Limiter._Hold(self)

    # -- rate budgets
    def _prune(self, now):
        cutoff = now - WINDOW
        while self.req_times and self.req_times[0] <= cutoff:
            self.req_times.popleft()
        if self.tok:
            self.tok = deque(e for e in self.tok if e[2] or e[0] > cutoff)

    def _wait_time(self, now, reserve):
        waits = []
        if self.rpm is not None and len(self.req_times) >= self.rpm:
            waits.append(self.req_times[0] + WINDOW - now)
        if self.tpm is not None:
            done = sorted(e[0] for e in self.tok if not e[2])
            if done:
                waits.append(done[0] + WINDOW - now)
        if not waits:
            return 1.0
        return min(1.0, max(0.005, min(waits)))

    def _rate_ok(self, now, reserve):
        if self.rpm is not None and len(self.req_times) >= self.rpm:
            return False
        if self.tpm is not None and self.tok:
            used = sum(e[1] for e in self.tok)
            if used + reserve > self.tpm:
                return False
        return True

    def acquire(self, reserve):
        """Block until a request may be sent. Returns a ticket for release()."""
        need_slot = not self.holding()
        if need_slot:
            self._take_slot()
        try:
            with self.cond:
                me = object()
                self.queue.append(me)
                try:
                    while True:
                        if self.cost.exceeded:
                            raise BudgetExceeded()
                        now = time.monotonic()
                        self._prune(now)
                        if self.queue[0] is me and self._rate_ok(now, reserve):
                            break
                        self.cond.wait(self._wait_time(now, reserve) if self.queue[0] is me else 1.0)
                finally:
                    self.queue.remove(me)
                    self.cond.notify_all()
                self.req_times.append(now)
                entry = [now, reserve, True]
                if self.tpm is not None:
                    self.tok.append(entry)
        except BaseException:
            if need_slot:
                self._give_slot()
            raise
        return (entry, need_slot)

    def release(self, ticket, actual_tokens):
        entry, had_slot = ticket
        with self.cond:
            entry[0] = time.monotonic()
            entry[1] = max(0, int(actual_tokens))
            entry[2] = False
            if had_slot:
                self.inflight -= 1
            self.cond.notify_all()


def count_words(messages):
    n = 0
    for m in messages:
        c = m.get("content")
        if c is None:
            c = ""
        if not isinstance(c, str):
            c = _stringify(c)
        n += len(c.split())
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            n += len(_stringify(fn.get("name") or "").split())
            n += len(_stringify(fn.get("arguments") or "").split())
    return n


def estimate_prompt_tokens(messages):
    return int(math.ceil(count_words(messages) / 0.75))


class Client:
    """Per-task client: knows the task's generation settings, counts its calls."""

    def __init__(self, cfg, transport, limiter, cost):
        self.cfg = cfg
        self.transport = transport
        self.limiter = limiter
        self.cost = cost
        self.counter = Counter()
        self.lock = threading.Lock()
        self.prompt_cost = 0.0
        self.completion_cost = 0.0

    def _charge(self, prompt_tokens, completion_tokens):
        c = self.cfg.get("cost") or {}
        pc = prompt_tokens / 1000.0 * c.get("prompt_cost_per_1k", 0.0)
        cc = completion_tokens / 1000.0 * c.get("completion_cost_per_1k", 0.0)
        with self.lock:
            self.prompt_cost += pc
            self.completion_cost += cc
        self.cost.add(pc, cc)

    def complete(self, messages, model=None, temperature=None, max_tokens=None):
        """One logical attempt with retries. Returns (ok, text, meta)."""
        ok, text, _calls, meta = self.request(messages, model, temperature, max_tokens)
        if ok and text is None:
            text = ""
        return ok, text, meta

    def request(self, messages, model=None, temperature=None, max_tokens=None, tools=None):
        """One API request (with 5xx retries).

        Returns (ok, text, tool_calls, meta). tool_calls is a list of
        {"id", "name", "arguments"(raw), "raw"} dicts, or [] when none.
        """
        model = self.cfg["model"] if model is None else model
        temperature = self.cfg["temperature"] if temperature is None else temperature
        max_tokens = self.cfg["max_tokens"] if max_tokens is None else max_tokens
        completions = self.cfg.get("api_type") == "completions"
        if completions:
            body = {
                "model": model,
                "prompt": render_chat_template(self.cfg.get("chat_template") or "chatml", messages, tools),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            path = "/v1/completions"
        else:
            body = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                body["tools"] = [{"type": "function",
                                  "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["parameters"]}} for t in tools]
            path = "/v1/chat/completions"
        meta = None
        if completions:
            est = int(math.ceil(len(body["prompt"].split()) / 0.75))
        else:
            est = estimate_prompt_tokens(messages)
        reserve = est + max_tokens
        for _ in range(MAX_HTTP_TRIES):
            ticket = self.limiter.acquire(reserve)
            actual = 0
            try:
                status, payload, err, latency = self.transport.post(body, self.counter, path)
                if status is not None and 200 <= status < 300:
                    actual = _usage_tokens(payload)
            finally:
                self.limiter.release(ticket, actual)
            meta = {"model": model, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "latency_ms": latency, "finish_reason": None}
            if status is not None and 200 <= status < 300:
                try:
                    obj = json.loads(payload.decode("utf-8"))
                    choice = obj["choices"][0]
                    calls = []
                    if completions:
                        text = choice.get("text")
                        if text is None:
                            text = ""
                        if tools is not None:
                            calls = parse_text_tool_calls(text)
                    else:
                        msg = choice.get("message") or {}
                        text = msg.get("content")
                        for k, tc in enumerate(msg.get("tool_calls") or []):
                            fn = (tc or {}).get("function") or {}
                            calls.append({"id": tc.get("id") or f"call_{k}",
                                          "name": fn.get("name"),
                                          "arguments": fn.get("arguments"),
                                          "raw": tc})
                    usage = obj.get("usage") or {}
                    meta["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
                    meta["completion_tokens"] = int(usage.get("completion_tokens") or 0)
                    meta["total_tokens"] = int(usage.get("total_tokens")
                                               or meta["prompt_tokens"] + meta["completion_tokens"])
                    meta["finish_reason"] = choice.get("finish_reason")
                except Exception:
                    self._charge(*_usage_pair(payload))
                    return False, None, [], meta
                self._charge(meta["prompt_tokens"], meta["completion_tokens"])
                return True, text, calls, meta
            if status is not None and status < 500:
                return False, None, [], meta  # 4xx: not retryable
            # 5xx or connection-level failure: retry
        return False, None, [], meta


def _usage_pair(payload):
    try:
        usage = json.loads(payload.decode("utf-8")).get("usage") or {}
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    except Exception:
        return 0, 0


def _usage_tokens(payload):
    p, c = _usage_pair(payload)
    return p + c


# ---------------------------------------------------------------- templates


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def parse_text_tool_calls(text):
    calls = []
    for k, m in enumerate(TOOL_CALL_RE.finditer(text or "")):
        body = m.group(1)
        name, arguments = None, None
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                name = obj.get("name")
                arguments = obj.get("arguments", obj.get("parameters", {}))
            else:
                arguments = body
        except (json.JSONDecodeError, ValueError):
            arguments = body
        calls.append({"id": f"call_{k}", "name": name, "arguments": arguments, "raw": None})
    return calls


def tools_prompt(tools):
    lines = ["# Tools", "",
             "You may call one or more functions to assist with the user query.",
             "You are provided with function signatures within <tools></tools> XML tags:",
             "<tools>"]
    for t in tools:
        lines.append(json.dumps({"type": "function",
                                 "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["parameters"]}}, ensure_ascii=False))
    lines += ["</tools>", "",
              "For each function call, return a json object with function name and arguments "
              "within <tool_call></tool_call> XML tags:",
              "<tool_call>",
              '{"name": <function-name>, "arguments": <args-json-object>}',
              "</tool_call>",
              "When you have the final answer, respond with plain text and no <tool_call> block."]
    return "\n".join(lines)


def _msg_text(m):
    c = m.get("content")
    if c is None:
        c = ""
    if not isinstance(c, str):
        c = _stringify(c)
    if m.get("role") == "assistant" and m.get("tool_calls") and "<tool_call>" not in c:
        blocks = []
        for tc in m["tool_calls"]:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    pass
            blocks.append("<tool_call>\n" + json.dumps({"name": fn.get("name"), "arguments": args},
                                                        ensure_ascii=False) + "\n</tool_call>")
        c = (c + "\n" if c else "") + "\n".join(blocks)
    return c


def render_chat_template(name, messages, tools=None):
    """Render a message list into a prompt string using a built-in template."""
    msgs = [dict(m) for m in messages]
    if tools:
        tp = tools_prompt(tools)
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = (_msg_text(msgs[0]) + "\n\n" + tp) if _msg_text(msgs[0]) else tp
        else:
            msgs.insert(0, {"role": "system", "content": tp})
    if name == "chatml":
        out = "".join(f"<|im_start|>{m['role']}\n{_msg_text(m)}<|im_end|>\n" for m in msgs)
        return out + "<|im_start|>assistant\n"
    if name == "llama3":
        out = "<|begin_of_text|>"
        for m in msgs:
            out += f"<|start_header_id|>{m['role']}<|end_header_id|>\n\n{_msg_text(m)}<|eot_id|>"
        return out + "<|start_header_id|>assistant<|end_header_id|>\n\n"
    if name == "zephyr":
        out = "".join(f"<|{m['role']}|>\n{_msg_text(m)}</s>\n" for m in msgs)
        return out + "<|assistant|>\n"
    if name == "mistral":
        system = None
        if msgs and msgs[0].get("role") == "system":
            system = _msg_text(msgs[0])
            msgs = msgs[1:]
        out = ""
        first_user = True
        for m in msgs:
            role, text = m.get("role"), _msg_text(m)
            if role == "user" or role == "system":
                if first_user and system is not None:
                    text = f"{system}\n\n{text}"
                first_user = False
                out += f"[INST] {text} [/INST]"
            elif role == "assistant":
                out += f" {text}</s>"
            elif role == "tool":
                out += f"[TOOL_RESULTS] {text} [/TOOL_RESULTS]"
            else:
                out += f"[INST] {text} [/INST]"
        if first_user and system is not None:
            out = f"[INST] {system} [/INST]" + out
        return out
    raise ValueError(f"unknown chat template {name!r}")


# ---------------------------------------------------------------- tools


def run_tool_script(handler, value):
    argv = list(handler["argv"]) + [value]
    timeout = handler.get("timeout", SCRIPT_TIMEOUT)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        return f"ERROR: {e}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return f"ERROR: timeout after {timeout} seconds"
    out = out.decode("utf-8", errors="replace")
    err = err.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        msg = err.strip() or f"command exited with status {proc.returncode}"
        return f"ERROR: {msg}"
    return out.strip()


def execute_tool(tools_by_name, name, args):
    tool = tools_by_name.get(name)
    if tool is None:
        return f"ERROR: unknown tool {name!r}"
    h = tool["handler"]
    try:
        if h["type"] == "echo":
            return json.dumps(args, ensure_ascii=False)
        if h["type"] == "static_map":
            kf = h["key_field"]
            if isinstance(args, dict):
                if kf is not None and kf in args:
                    key = args[kf]
                elif kf is None and args:
                    key = next(iter(args.values()))
                else:
                    return h["default"]
            else:
                key = args
            return h["mapping"].get(_stringify(key), h["default"])
        # script
        if isinstance(args, dict):
            if h["arg_field"] not in args:
                return f"ERROR: missing argument {h['arg_field']!r}"
            value = args[h["arg_field"]]
        else:
            value = args
        return run_tool_script(h, _stringify(value) if value is not None else "")
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def run_agentic(client, cfg, messages):
    """Run one agentic loop. Returns a dict describing it."""
    tools = cfg.get("tools") or []
    tools_by_name = {t["name"]: t for t in tools}
    completions = cfg.get("api_type") == "completions"
    convo = copy.deepcopy(messages)
    t0 = time.monotonic()
    details, tool_calls = [], []
    text, finish, api_ok = None, "max_iterations", True
    iterations = 0
    with client.limiter.hold():
        loop_out = _agentic_iterations(client, cfg, convo, tools, tools_by_name, completions,
                                       details, tool_calls)
    text, finish, api_ok, iterations = loop_out
    latency = int(round((time.monotonic() - t0) * 1000))
    agg = {
        "model": cfg["model"],
        "total_prompt_tokens": sum(d["prompt_tokens"] for d in details),
        "total_completion_tokens": sum(d["completion_tokens"] for d in details),
        "total_tokens": sum(d["total_tokens"] for d in details),
        "latency_ms": latency,
        "finish_reason": finish,
        "iterations_detail": details,
    }
    return {"text": text, "iterations": iterations, "tool_calls": tool_calls,
            "meta": agg, "api_ok": api_ok}


def _agentic_iterations(client, cfg, convo, tools, tools_by_name, completions, details, tool_calls):
    text, finish, api_ok = None, "max_iterations", True
    iterations = 0
    for it in range(1, cfg["max_iterations"] + 1):
        ok, rtext, calls, meta = client.request(convo, tools=tools)
        iterations = it
        if meta is None:
            meta = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                    "latency_ms": 0, "finish_reason": None}
        details.append({"prompt_tokens": meta["prompt_tokens"],
                        "completion_tokens": meta["completion_tokens"],
                        "total_tokens": meta["total_tokens"],
                        "latency_ms": meta["latency_ms"],
                        "finish_reason": meta["finish_reason"]})
        if not ok:
            api_ok, finish = False, "error"
            break
        if not calls:
            text = rtext if rtext is not None else ""
            finish = "stop"
            break
        # execute every call, then extend the conversation
        results = []
        for c in calls:
            raw = c["arguments"]
            if isinstance(raw, str):
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except (json.JSONDecodeError, ValueError):
                    args = raw
            elif raw is None:
                args = {}
            else:
                args = raw
            if isinstance(raw, str) and not isinstance(args, (dict, list)) and args is raw:
                res = "ERROR: invalid JSON arguments"
            else:
                res = execute_tool(tools_by_name, c["name"], args)
            tool_calls.append({"iteration": it, "tool": c["name"], "args": args, "result": res})
            results.append((c, res))
        if completions:
            convo.append({"role": "assistant", "content": rtext or ""})
            for c, res in results:
                convo.append({"role": "tool", "content": res})
        else:
            raw_calls = []
            for c, _ in results:
                rc = copy.deepcopy(c["raw"]) if isinstance(c["raw"], dict) else {}
                rc.setdefault("id", c["id"])
                rc.setdefault("type", "function")
                fn = rc.setdefault("function", {})
                fn.setdefault("name", c["name"])
                if not isinstance(fn.get("arguments"), str):
                    fn["arguments"] = json.dumps(fn.get("arguments") if fn.get("arguments") is not None else {})
                raw_calls.append(rc)
            convo.append({"role": "assistant", "content": rtext, "tool_calls": raw_calls})
            for c, res in results:
                convo.append({"role": "tool", "tool_call_id": c["id"], "content": res})
    return text, finish, api_ok, iterations


# ---------------------------------------------------------------- processing


def process_row_agentic(client, cfg, row, messages, idx):
    ev = cfg["evaluation"]
    is_judge = ev is not None and ev["type"] == "llm_judge"
    schema = cfg.get("output_schema")
    loop = run_agentic(client, cfg, messages)
    meta = loop["meta"]
    text = loop["text"]
    judge_score = None
    sv = None
    if text is not None and schema is not None:
        sv = validate_output(schema, text)
    if text is None:
        passed, extracted = (False if (ev is not None or schema is not None) else None), None
    elif sv is not None and not sv[0]:
        passed, extracted = False, None
    elif ev is None:
        passed, extracted = None, None
    elif ev["type"] == "script":
        passed, extracted = evaluate_script(ev, text, row, idx)
    elif is_judge:
        passed, extracted, judge_score, jmeta = evaluate_judge(client, cfg, ev, text, row, idx)
        meta["judge_meta"] = jmeta
    else:
        passed, extracted = evaluate_local(ev, text, row)
    if sv is not None and sv[0] and ev is None:
        passed = True
    r = {"passed": passed, "extracted_answer": extracted, "attempts": 1}
    if is_judge:
        r["judge_score"] = judge_score
    r["iterations"] = loop["iterations"]
    r["tool_calls"] = loop["tool_calls"]
    if sv is not None:
        r["schema_valid"] = sv[0]
        if not sv[0]:
            r["schema_error"] = sv[2]
    if text is None or (sv is not None and not sv[0]):
        output = None
    else:
        output = {cfg["output_field"]: sv[1] if sv is not None else text}
    return {"output": output, "result": r, "metas": [meta], "api_ok": loop["api_ok"]}


def process_row(client, cfg, row, messages, idx):
    ev = cfg["evaluation"]
    if cfg["scheme"] == "agentic":
        return process_row_agentic(client, cfg, row, messages, idx)
    scheme = cfg["scheme"]
    schema = cfg.get("output_schema")
    is_judge = ev is not None and ev["type"] == "llm_judge"
    attempts = cfg["n"] if scheme == "rejection" else 1
    metas = []
    judge_score = None
    last_sv = None

    def result(passed, extracted, n_attempts, sv=None):
        r = {"passed": passed, "extracted_answer": extracted, "attempts": n_attempts}
        if is_judge:
            r["judge_score"] = judge_score
        if schema is not None and sv is not None:
            r["schema_valid"] = sv[0]
            if not sv[0]:
                r["schema_error"] = sv[2]
        return r

    for i in range(attempts):
        ok, text, meta = client.complete(messages)
        metas.append(meta)
        if not ok:
            # API failure after retries: row failed
            return {"output": None,
                    "result": result(False if (ev is not None or schema is not None) else None,
                                     None, i + 1, last_sv),
                    "metas": metas, "api_ok": False}
        sv = None
        if schema is not None:
            sv = validate_output(schema, text)
            last_sv = sv
            meta["schema_valid"] = sv[0]
            if not sv[0]:
                if scheme == "rejection":
                    continue
                return {"output": None, "result": result(False, None, i + 1, sv),
                        "metas": metas, "api_ok": True}
        if ev is None:
            passed, extracted = (True if schema is not None else None), None
        elif ev["type"] == "script":
            passed, extracted = evaluate_script(ev, text, row, idx)
        elif is_judge:
            passed, extracted, judge_score, jmeta = evaluate_judge(client, cfg, ev, text, row, idx)
            meta["judge_meta"] = jmeta
        else:
            passed, extracted = evaluate_local(ev, text, row)
        if scheme != "rejection" or passed:
            value = sv[1] if sv is not None else text
            return {"output": {cfg["output_field"]: value},
                    "result": result(passed, extracted, i + 1, sv),
                    "metas": metas, "api_ok": True}
    return {"output": None, "result": result(False, None, attempts, last_sv),
            "metas": metas, "api_ok": True}


def _evaluate(client, cfg, text, row, idx, meta):
    ev = cfg["evaluation"]
    if ev is None:
        return None
    if ev["type"] == "script":
        passed, _ = evaluate_script(ev, text, row, idx)
    elif ev["type"] == "llm_judge":
        passed, _, score, jmeta = evaluate_judge(client, cfg, ev, text, row, idx)
        meta["judge_meta"] = jmeta
        meta["judge_score"] = score
    else:
        passed, _ = evaluate_local(ev, text, row)
    return bool(passed)


def _one_attempt(client, cfg, row, idx, messages, setup_name):
    """Return (has_output, value, passed, meta)."""
    ev = cfg["evaluation"]
    if cfg["scheme"] == "agentic":
        try:
            loop = run_agentic(client, cfg, messages)
            ok, text, meta = loop["text"] is not None, loop["text"], loop["meta"]
            meta["iterations"] = loop["iterations"]
            meta["tool_calls"] = loop["tool_calls"]
        except Exception as e:
            print(f"warning: task {cfg.get('name')!r} row {idx}: {type(e).__name__}: {e}", file=sys.stderr)
            ok, text = False, None
            meta = {"model": cfg["model"], "total_prompt_tokens": 0, "total_completion_tokens": 0,
                    "total_tokens": 0, "latency_ms": 0, "finish_reason": "error",
                    "iterations_detail": [], "iterations": 0, "tool_calls": []}
    else:
        try:
            ok, text, meta = client.complete(messages)
        except Exception as e:
            print(f"warning: task {cfg.get('name')!r} row {idx}: {type(e).__name__}: {e}", file=sys.stderr)
            ok, text, meta = False, None, None
    if meta is None:
        meta = {"model": cfg["model"], "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "latency_ms": 0, "finish_reason": None}
    meta["icl_setup"] = setup_name
    schema = cfg.get("output_schema")
    if not ok:
        meta["evaluation_passed"] = False if (ev is not None or schema is not None) else None
        return False, None, meta["evaluation_passed"], meta
    value = text
    if schema is not None:
        valid, value, err = validate_output(schema, text)
        meta["schema_valid"] = valid
        if not valid:
            meta["schema_error"] = err
            meta["evaluation_passed"] = False
            return False, None, False, meta
    try:
        passed = _evaluate(client, cfg, text, row, idx, meta)
    except Exception as e:
        print(f"warning: task {cfg.get('name')!r} row {idx}: evaluation error: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        passed = False
    if ev is None and schema is not None:
        passed = True
    meta["evaluation_passed"] = passed
    return True, value, passed, meta


def process_row_multi(client, cfg, row, prepared, idx):
    """List-format processing (ICL configured and/or num_solutions > 1)."""
    ev = cfg["evaluation"]
    scheme = cfg["scheme"]
    icl = cfg["icl"]
    N = cfg["num_solutions"]
    setups = icl["setups"] if icl is not None else None
    strategy = icl["strategy"] if icl is not None else None
    rng = random.Random()

    if scheme == "greedy":
        if icl is not None and N > 1:
            strategy = "walk"
            target, max_att = N, len(setups)
        else:
            target, max_att = 1, 1
    elif scheme in ("sample", "agentic"):
        target, max_att = N, N
    else:  # rejection
        target, max_att = N, cfg["max_attempts"]

    def keep(has, passed):
        if not has:
            return False
        if scheme == "rejection":
            return passed is True
        return True

    def choose(i):
        if setups is None:
            return None
        if strategy == "walk":
            return i
        if strategy == "round_robin":
            return i % len(setups)
        if strategy == "random":
            return rng.randrange(len(setups))
        return 0

    attempts = []  # (text, passed, meta, setup_name) in attempt order
    collected = 0
    while collected < target and len(attempts) < max_att:
        batch = min(target - collected, max_att - len(attempts))
        plan = []
        for b in range(batch):
            si = choose(len(attempts) + b)
            if si is None:
                plan.append((prepared, None))
            else:
                plan.append((prepared[si], setups[si]["name"]))
        if batch == 1:
            outs = [_one_attempt(client, cfg, row, idx, plan[0][0], plan[0][1])]
        else:
            with ThreadPoolExecutor(max_workers=batch) as ex:
                futs = [ex.submit(_one_attempt, client, cfg, row, idx, m, nm) for m, nm in plan]
                outs = [f.result() for f in futs]
        for (has, value, passed, meta), (_, nm) in zip(outs, plan):
            attempts.append((has, value, passed, meta, nm))
            if keep(has, passed):
                collected += 1

    of = cfg["output_field"]
    output = [{of: value, "icl_setup": nm} for has, value, passed, _, nm in attempts if keep(has, passed)]
    n_att = len(attempts)
    if ev is not None or cfg.get("output_schema") is not None:
        n_passed = sum(1 for a in attempts if a[2] is True)
    else:
        n_passed = len(output)
    result = {"passed": n_passed, "failed": n_att - n_passed, "attempts": n_att}
    return {"output": output, "result": result, "metas": [a[3] for a in attempts], "api_ok": True}


ABORTED = None  # result marker for rows cut short by the cost budget


def _safe_process(client, cfg, row, messages, idx):
    if client.cost.exceeded:
        return ABORTED
    try:
        if cfg.get("list_mode"):
            return process_row_multi(client, cfg, row, messages, idx)
        return process_row(client, cfg, row, messages, idx)
    except BudgetExceeded:
        return ABORTED
    except Exception as e:  # never let a single row kill the run
        print(f"warning: task {cfg.get('name')!r} row {idx}: {type(e).__name__}: {e}", file=sys.stderr)
        if cfg.get("list_mode"):
            return {"output": [], "result": {"passed": 0, "failed": 0, "attempts": 0},
                    "metas": [], "api_ok": False}
        r = {"passed": False if cfg["evaluation"] is not None else None,
             "extracted_answer": None, "attempts": 1}
        if cfg["evaluation"] is not None and cfg["evaluation"]["type"] == "llm_judge":
            r["judge_score"] = None
        if cfg["scheme"] == "agentic":
            r["iterations"] = 0
            r["tool_calls"] = []
        return {"output": None, "result": r, "metas": [], "api_ok": False}


# ---------------------------------------------------------------- run


def resolve_jobs(args):
    """Validate everything up front. Return (multi, [job, ...]).

    job = {"cfg", "rows", "prepared", "output_path"}
    """
    raw = read_config(args.config)
    multi, tasks = parse_config(raw)
    config_dir = os.path.dirname(os.path.abspath(args.config))
    inputs = args.input or []
    selected = args.task or []

    if not multi:
        name, task, prefix = tasks[0]
        if selected:
            bad = [s for s in selected if s != name]
            if bad:
                raise ConfigError(f"unknown task {bad[0]!r} (config defines task {name!r})"
                                  if name else f"unknown task {bad[0]!r} (config task has no name)")
        if args.input_dir is not None and inputs:
            raise ConfigError("--input and --input-dir cannot be combined")
        if args.input_dir is not None:
            if not name:
                raise ConfigError("--input-dir requires task.name in single-task configs")
            path = os.path.join(args.input_dir, f"{name}.jsonl")
            if not os.path.isfile(path):
                raise ConfigError(f"input file for task {name!r} not found: {path}")
        else:
            if not inputs:
                raise ConfigError("--input is required")
            if len(inputs) > 1:
                raise ConfigError("single-task configs take exactly one --input")
            path = inputs[0]
            if name and path.startswith(name + "=") and not os.path.exists(path):
                path = path[len(name) + 1:]
        if args.output is None and not args.dry_run:
            raise ConfigError("--output is required")
        if args.output is not None and os.path.isdir(args.output):
            raise ConfigError(f"--output {args.output} is a directory; single-task configs write a file")
        cfg = build_task(name, task, prefix, args, config_dir)
        rows = load_input(path)
        prepared = prepare(cfg, rows)
        return False, [{"cfg": cfg, "rows": rows, "prepared": prepared, "output_path": args.output}]

    names = [t[0] for t in tasks]
    for s in selected:
        if s not in names:
            raise ConfigError(f"unknown task {s!r}; config defines: {', '.join(names)}")
    run_names = [n for n in names if n in selected] if selected else names

    if args.input_dir is not None and inputs:
        raise ConfigError("--input <task=path> and --input-dir are alternative modes; do not combine them")
    paths = {}
    if args.input_dir is not None:
        if not os.path.isdir(args.input_dir):
            raise ConfigError(f"--input-dir {args.input_dir} is not a directory")
        for n in run_names:
            p = os.path.join(args.input_dir, f"{n}.jsonl")
            if not os.path.isfile(p):
                raise ConfigError(f"input file for task {n!r} not found: {p}")
            paths[n] = p
    else:
        if not inputs:
            raise ConfigError("multi-task configs require --input <task=path> (repeatable) or --input-dir")
        mapping = {}
        for item in inputs:
            if "=" not in item:
                raise ConfigError(f"--input {item!r}: multi-task configs require the form <task>=<path>")
            n, p = item.split("=", 1)
            if n not in names:
                raise ConfigError(f"--input {item!r}: unknown task {n!r}; config defines: {', '.join(names)}")
            if not p:
                raise ConfigError(f"--input {item!r}: empty path")
            if n in mapping:
                raise ConfigError(f"--input given more than once for task {n!r}")
            mapping[n] = p
        for n in run_names:
            if n not in mapping:
                raise ConfigError(f"no --input given for task {n!r}")
            paths[n] = mapping[n]

    if args.output is None and not args.dry_run:
        raise ConfigError("--output is required")
    if args.output is not None and os.path.exists(args.output) and not os.path.isdir(args.output):
        raise ConfigError(f"--output {args.output} must be a directory for multi-task configs")

    by_name = {t[0]: t for t in tasks}
    jobs = []
    for n in run_names:
        _, task, prefix = by_name[n]
        cfg = build_task(n, task, prefix, args, config_dir)
        rows = load_input(paths[n])
        prepared = prepare(cfg, rows)
        jobs.append({"cfg": cfg, "rows": rows, "prepared": prepared,
                     "output_path": os.path.join(args.output, f"{n}.jsonl") if args.output else None})
    return True, jobs


def _first_messages(prepared_row):
    """The message list for a prepared row (first ICL setup when ICL is configured)."""
    if prepared_row and isinstance(prepared_row[0], list):
        return prepared_row[0]
    return prepared_row


def dry_run_estimate(jobs):
    tasks = {}
    total_inputs = total_tokens = 0
    total_cost = 0.0
    minutes = 0.0
    for job in jobs:
        cfg = job["cfg"]
        n = len(job["rows"])
        words = sum(count_words(_first_messages(p)) for p in job["prepared"])
        avg_words = words / n if n else 0.0
        est_prompt = int(round(avg_words * 1.33 * n))
        est_completion = n * cfg["max_tokens"]
        est_total = est_prompt + est_completion
        c = cfg.get("cost") or {}
        est_cost = (est_prompt / 1000.0 * c.get("prompt_cost_per_1k", 0.0)
                    + est_completion / 1000.0 * c.get("completion_cost_per_1k", 0.0))
        avg_attempts = cfg["n"] if cfg["scheme"] == "rejection" else 1
        rpm = cfg["rpm"] if cfg["rpm"] is not None else 60
        minutes += n * avg_attempts / rpm
        name = cfg["name"] or "task"
        tasks[name] = {
            "inputs": n,
            "est_prompt_tokens": est_prompt,
            "est_completion_tokens": est_completion,
            "est_total_tokens": est_total,
            "est_cost": round(est_cost, 4),
        }
        total_inputs += n
        total_tokens += est_total
        total_cost += est_cost
    return {
        "tasks": tasks,
        "total_inputs": total_inputs,
        "est_total_tokens": total_tokens,
        "est_total_cost": round(total_cost, 4),
        "est_time_minutes": round(minutes, 4),
    }


def line_complete(cfg, obj):
    """Whether a previously written output line is a finished result for its row."""
    if not isinstance(obj, dict) or "output" not in obj or "result" not in obj:
        return False
    out, res = obj["output"], obj["result"]
    if not isinstance(res, dict):
        return False
    scheme = cfg["scheme"]
    if cfg.get("list_mode"):
        if not isinstance(out, list):
            return False
        if scheme in ("rejection", "sample", "agentic"):
            return len(out) >= cfg["num_solutions"]
        return True
    if scheme == "rejection":
        return res.get("passed") is True
    return True


def read_existing(path, nrows):
    """Return (lines, complete_flags_placeholder, byte_ends) for non-blank lines of an output file."""
    with open(path, "rb") as f:
        data = f.read()
    lines, ends = [], []
    pos = 0
    for raw in data.splitlines(keepends=True):
        pos += len(raw)
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not text.strip():
            continue
        if len(lines) >= nrows:
            break
        lines.append(text)
        ends.append(pos)
    return lines, ends


def classify(cfg, res):
    """Return 'passed', 'failed' or None (neither) for a processed row."""
    if cfg.get("list_mode"):
        return "passed" if res["result"]["passed"] > 0 else "failed"
    rp = res["result"]["passed"]
    if res["output"] is None or rp is False:
        return "failed"
    if rp is True or (rp is None and res["api_ok"]):
        return "passed"
    return None


class TaskWriter:
    """Writes one task's output lines strictly in input order as results arrive."""

    def __init__(self, job, f, start, old_lines, old_complete):
        self.job = job
        self.cfg = job["cfg"]
        self.f = f
        self.n = len(job["rows"])
        self.next = start
        self.old = old_lines
        self.old_complete = old_complete
        self.slots = {}
        self.stopped = False
        self.written = self.passed = self.failed = 0
        self.tp = self.tc = self.sols = 0

    def put(self, i, res):
        self.slots[i] = res
        self.drain()

    def drain(self):
        wrote = False
        while not self.stopped and self.next < self.n:
            i = self.next
            if i < len(self.old) and self.old_complete[i]:
                self.f.write(self.old[i] + "\n")
            elif i not in self.slots:
                break
            else:
                res = self.slots.pop(i)
                if res is ABORTED:
                    if i < len(self.old):
                        self.f.write(self.old[i] + "\n")
                    else:
                        self.stopped = True
                        break
                else:
                    self.f.write(self._line(self.job["rows"][i], res) + "\n")
            self.next += 1
            wrote = True
        if wrote:
            self.f.flush()

    def _line(self, row, res):
        metas = res["metas"]
        for m in metas:
            self.tp += m.get("total_prompt_tokens", m.get("prompt_tokens", 0))
            self.tc += m.get("total_completion_tokens", m.get("completion_tokens", 0))
            jm = m.get("judge_meta")
            if jm:
                self.tp += jm["prompt_tokens"]
                self.tc += jm["completion_tokens"]
        if self.cfg.get("list_mode"):
            self.sols += len(res["output"])
            meta_out = metas
        else:
            if res["output"] is not None:
                self.sols += 1
            meta_out = metas[0] if len(metas) == 1 else metas
        c = classify(self.cfg, res)
        if c == "passed":
            self.passed += 1
        elif c == "failed":
            self.failed += 1
        self.written += 1
        return json.dumps({"input": row, "output": res["output"], "result": res["result"],
                           "meta": meta_out}, ensure_ascii=False)


class Progress:
    def __init__(self, total, show_eval, show_cost, cost, stats, t_start):
        self.total = total
        self.show_eval = show_eval
        self.show_cost = show_cost
        self.cost = cost
        self.stats = stats
        self.t_start = t_start
        self.done = self.passed = self.failed = 0
        self.lock = threading.Lock()
        self.last_print = t_start
        self.last_bucket = 0
        self.stop_ev = threading.Event()
        self.thread = threading.Thread(target=self._tick, daemon=True)

    def start(self):
        self.thread.start()

    def _tick(self):
        while not self.stop_ev.wait(0.2):
            with self.lock:
                if time.monotonic() - self.last_print >= 5.0:
                    self._print()

    def update(self, outcome):
        with self.lock:
            self.done += 1
            if outcome == "passed":
                self.passed += 1
            elif outcome == "failed":
                self.failed += 1
            bucket = (self.done * 10 // self.total) if self.total else 10
            if bucket > self.last_bucket:
                self.last_bucket = bucket
                self._print()

    def finish(self):
        self.stop_ev.set()
        self.thread.join(timeout=1.0)

    def _print(self):
        now = time.monotonic()
        self.last_print = now
        elapsed = now - self.t_start
        pct = int(self.done * 100 / self.total) if self.total else 100
        parts = [f"[{self.done}/{self.total}] {pct}% complete"]
        if self.show_eval:
            parts.append(f"{self.passed} passed, {self.failed} failed")
        rpm = self.stats.calls / elapsed * 60 if elapsed > 0 else 0.0
        parts.append(f"{rpm:.1f} rpm")
        if self.show_cost:
            parts.append(f"${self.cost.total:.2f} spent")
        if self.done > 0:
            eta = int(round(elapsed / self.done * (self.total - self.done)))
            parts.append(f"ETA: {eta}s")
        else:
            parts.append("ETA: ?")
        print(" | ".join(parts), file=sys.stderr, flush=True)


def _open_output(job, resume):
    """Open a task's output file. Returns (file, start_index, old_lines, old_complete, resumed_from)."""
    path = job["output_path"]
    cfg = job["cfg"]
    n = len(job["rows"])
    if resume and os.path.isfile(path):
        lines, ends = read_existing(path, n)
        complete = []
        for text in lines:
            try:
                obj = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                obj = None
            complete.append(line_complete(cfg, obj))
        k = 0
        while k < len(lines) and complete[k]:
            k += 1
        f = open(path, "r+b")
        f.truncate(ends[k - 1] if k > 0 else 0)
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.seek(0, os.SEEK_END)
                f.write(b"\n")
        f.close()
        f = open(path, "a", encoding="utf-8")
        return f, k, lines, complete, sum(1 for c in complete if c)
    return open(path, "w", encoding="utf-8"), 0, [], [], 0


def _r(x):
    return round(x, 6)


def run(args):
    try:
        multi, jobs = resolve_jobs(args)
    except ConfigError as e:
        fail(str(e))

    if args.dry_run:
        print(json.dumps(dry_run_estimate(jobs)))
        sys.stdout.flush()
        return 0

    if multi:
        try:
            os.makedirs(args.output, exist_ok=True)
        except OSError as e:
            fail(f"cannot create output directory {args.output}: {e}")
    writers = []
    resumed = []
    for job in jobs:
        try:
            f, start, old, complete, rf = _open_output(job, args.resume)
        except OSError as e:
            for w in writers:
                w.f.close()
            fail(f"cannot open output file {job['output_path']}: {e}")
        writers.append(TaskWriter(job, f, start, old, complete))
        resumed.append(rf)

    cost_cfgs = [job["cfg"]["cost"] for job in jobs if job["cfg"].get("cost") is not None]
    cost_enabled = bool(cost_cfgs)
    budgets = [c["budget"] for c in cost_cfgs if c["budget"] is not None]
    budget = args.budget if args.budget is not None else (min(budgets) if budgets else None)
    cost = CostTracker(budget)

    stats = Stats()
    transports = {}
    clients = []
    for job in jobs:
        cfg = job["cfg"]
        url = cfg["api_url"]
        if url not in transports:
            transports[url] = Transport(url, stats)
        mc = cfg["max_concurrent"]
        if mc is None:
            mc = max(8, min(cfg["rpm"], 256)) if cfg["rpm"] is not None else 64
        limiter = Limiter(cfg["rpm"], cfg["tpm"], mc, cost)
        clients.append(Client(cfg, transports[url], limiter, cost))

    todo = []
    for j, (job, w) in enumerate(zip(jobs, writers)):
        idxs = [i for i in range(len(job["rows"]))
                if i >= len(w.old) or not w.old_complete[i]]
        todo.append(idxs)
        w.drain()
    total_todo = sum(len(t) for t in todo)

    progress = None
    t_start = time.monotonic()
    if args.progress:
        show_eval = any(job["cfg"]["evaluation"] is not None or job["cfg"].get("output_schema") is not None
                        for job in jobs)
        progress = Progress(total_todo, show_eval, cost_enabled, cost, stats, t_start)
        progress.start()

    if total_todo:
        cap = sum(c.limiter.max_concurrent for c in clients)
        workers = max(1, min(total_todo, 2 * cap + 8, 512))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {}
            longest = max(len(t) for t in todo)
            # interleave submission across tasks so all tasks make progress together
            for k in range(longest):
                for j, job in enumerate(jobs):
                    if k < len(todo[j]):
                        i = todo[j][k]
                        fut = pool.submit(_safe_process, clients[j], job["cfg"],
                                          job["rows"][i], job["prepared"][i], i)
                        futs[fut] = (j, i)
            for fut in as_completed(futs):
                j, i = futs[fut]
                res = fut.result()
                if progress is not None and res is not ABORTED:
                    progress.update(classify(jobs[j]["cfg"], res))
                writers[j].put(i, res)
    if progress is not None:
        progress.finish()

    for w in writers:
        w.drain()
        w.f.close()

    g_passed = g_failed = g_tp = g_tc = g_total = 0
    task_summaries = {}
    for j, (job, w) in enumerate(zip(jobs, writers)):
        name = job["cfg"]["name"] or "task"
        ts = {
            "total": w.written,
            "passed": w.passed,
            "failed": w.failed,
            "total_solutions": w.sols,
            "avg_solutions_per_input": (w.sols / w.written) if w.written else 0.0,
            "total_api_calls": clients[j].counter.value,
            "total_prompt_tokens": w.tp,
            "total_completion_tokens": w.tc,
        }
        if args.resume:
            ts["resumed_from"] = resumed[j]
        if job["cfg"].get("cost") is not None:
            c = clients[j]
            ts["cost"] = {"total": _r(c.prompt_cost + c.completion_cost),
                          "prompt": _r(c.prompt_cost), "completion": _r(c.completion_cost)}
        task_summaries[name] = ts
        g_total += w.written
        g_passed += w.passed
        g_failed += w.failed
        g_tp += w.tp
        g_tc += w.tc

    if stats.first_start is not None and stats.last_end is not None:
        elapsed = max(0.0, stats.last_end - stats.first_start)
    else:
        elapsed = 0.0
    throughput = (stats.calls / elapsed * 60) if elapsed > 0 else 0.0
    summary = {"total": g_total}
    if args.resume:
        summary["resumed_from"] = sum(resumed)
    summary.update({
        "passed": g_passed,
        "failed": g_failed,
        "total_prompt_tokens": g_tp,
        "total_completion_tokens": g_tc,
        "total_api_calls": stats.calls,
        "elapsed_seconds": round(elapsed, 1),
        "throughput_rpm": round(throughput, 1),
    })
    if cost_enabled:
        total = cost.total
        summary["cost"] = {
            "total": _r(total),
            "prompt": _r(cost.prompt),
            "completion": _r(cost.completion),
            "budget": budget,
            "budget_remaining": _r(max(0.0, budget - total)) if budget is not None else None,
            "budget_exceeded": bool(cost.exceeded),
        }
    summary["tasks"] = task_summaries
    print(json.dumps(summary))
    sys.stdout.flush()
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    p = Parser(prog="rejector.py")
    sub = p.add_subparsers(dest="command")
    r = sub.add_parser("run", help="run one or more tasks")
    r.error = p.error
    r.add_argument("--config", required=True)
    r.add_argument("--input", action="append",
                   help="input JSONL path (single-task) or <task>=<path> (multi-task, repeatable)")
    r.add_argument("--input-dir", dest="input_dir", help="directory containing <task_name>.jsonl files")
    r.add_argument("--output",
                   help="output JSONL file (single-task) or directory (multi-task)")
    r.add_argument("--task", action="append", help="run only this task (repeatable)")
    r.add_argument("--api-url", dest="api_url")
    r.add_argument("--model")
    r.add_argument("--eval-model", dest="eval_model", help="override the llm_judge model")
    r.add_argument("--rpm", type=int)
    r.add_argument("--max-tokens", dest="max_tokens", type=int)
    r.add_argument("--scheme", choices=SCHEMES)
    r.add_argument("--temperature", type=float)
    r.add_argument("--n", type=int)
    r.add_argument("--num-solutions", dest="num_solutions", type=int)
    r.add_argument("--max-attempts", dest="max_attempts", type=int)
    r.add_argument("--icl-strategy", dest="icl_strategy", choices=ICL_STRATEGIES)
    r.add_argument("--icl-k", dest="icl_k", type=int)
    r.add_argument("--api-type", dest="api_type", choices=API_TYPES)
    r.add_argument("--chat-template", dest="chat_template", choices=CHAT_TEMPLATES)
    r.add_argument("--tpm", type=int)
    r.add_argument("--max-concurrent", dest="max_concurrent", type=int)
    r.add_argument("--budget", type=float)
    r.add_argument("--resume", action="store_true")
    r.add_argument("--dry-run", dest="dry_run", action="store_true")
    r.add_argument("--progress", action="store_true")
    args = p.parse_args(argv)
    if args.command != "run":
        p.error("a command is required: run")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
