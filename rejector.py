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
from concurrent.futures import ThreadPoolExecutor

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


def parse_config(raw):
    """Return (multi, [(name, task_mapping, prefix), ...]) without validating task contents."""
    if "task" in raw:
        task = raw["task"]
        if not isinstance(task, dict):
            raise ConfigError("config 'task' must be a mapping")
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

    rpm = args.rpm if args.rpm is not None else task.get("rpm", 60)
    if not _is_int(rpm) or rpm <= 0:
        raise ConfigError(f"{P}.rpm must be a positive integer, got {rpm!r}")
    cfg["rpm"] = rpm

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

    cfg["evaluation"] = build_evaluation(task.get("evaluation"), P, scheme, model, args)

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


def build_evaluation(ev, P, scheme, model, args):
    E = f"{P}.evaluation"
    if ev is None:
        if scheme == "rejection":
            raise ConfigError(f"{E} is required for scheme 'rejection'")
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
    """Shared per-server connection limiter."""

    def __init__(self, api_url, max_inflight, stats):
        self.base = api_url
        self.sem = threading.BoundedSemaphore(max_inflight)
        self.stats = stats

    def post(self, body, counter, path="/v1/chat/completions"):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.sem:
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


class Client:
    """Per-task client: knows the task's generation settings, counts its calls."""

    def __init__(self, cfg, transport):
        self.cfg = cfg
        self.transport = transport
        self.counter = Counter()

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
        for _ in range(MAX_HTTP_TRIES):
            status, payload, err, latency = self.transport.post(body, self.counter, path)
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
                    return True, text, calls, meta
                except Exception:
                    return False, None, [], meta
            if status is not None and status < 500:
                return False, None, [], meta  # 4xx: not retryable
            # 5xx or connection-level failure: retry
        return False, None, [], meta


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


# ---------------------------------------------------------------- processing


def process_row_agentic(client, cfg, row, messages, idx):
    ev = cfg["evaluation"]
    is_judge = ev is not None and ev["type"] == "llm_judge"
    loop = run_agentic(client, cfg, messages)
    meta = loop["meta"]
    text = loop["text"]
    judge_score = None
    if text is None:
        passed, extracted = (False if ev is not None else None), None
    elif ev is None:
        passed, extracted = None, None
    elif ev["type"] == "script":
        passed, extracted = evaluate_script(ev, text, row, idx)
    elif is_judge:
        passed, extracted, judge_score, jmeta = evaluate_judge(client, cfg, ev, text, row, idx)
        meta["judge_meta"] = jmeta
    else:
        passed, extracted = evaluate_local(ev, text, row)
    r = {"passed": passed, "extracted_answer": extracted, "attempts": 1}
    if is_judge:
        r["judge_score"] = judge_score
    r["iterations"] = loop["iterations"]
    r["tool_calls"] = loop["tool_calls"]
    output = {cfg["output_field"]: text} if text is not None else None
    return {"output": output, "result": r, "metas": [meta], "api_ok": loop["api_ok"]}


def process_row(client, cfg, row, messages, idx):
    ev = cfg["evaluation"]
    if cfg["scheme"] == "agentic":
        return process_row_agentic(client, cfg, row, messages, idx)
    scheme = cfg["scheme"]
    is_judge = ev is not None and ev["type"] == "llm_judge"
    attempts = cfg["n"] if scheme == "rejection" else 1
    metas = []
    judge_score = None

    def result(passed, extracted, n_attempts):
        r = {"passed": passed, "extracted_answer": extracted, "attempts": n_attempts}
        if is_judge:
            r["judge_score"] = judge_score
        return r

    for i in range(attempts):
        ok, text, meta = client.complete(messages)
        metas.append(meta)
        if not ok:
            # API failure after retries: row failed
            return {"output": None,
                    "result": result(False if ev is not None else None, None, i + 1),
                    "metas": metas, "api_ok": False}
        if ev is None:
            passed, extracted = None, None
        elif ev["type"] == "script":
            passed, extracted = evaluate_script(ev, text, row, idx)
        elif is_judge:
            passed, extracted, judge_score, jmeta = evaluate_judge(client, cfg, ev, text, row, idx)
            meta["judge_meta"] = jmeta
        else:
            passed, extracted = evaluate_local(ev, text, row)
        if scheme != "rejection" or passed:
            return {"output": {cfg["output_field"]: text},
                    "result": result(passed, extracted, i + 1),
                    "metas": metas, "api_ok": True}
    return {"output": None, "result": result(False, None, attempts),
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
    """Return (text_or_None, passed, meta)."""
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
    if not ok:
        meta["evaluation_passed"] = False if ev is not None else None
        return None, meta["evaluation_passed"], meta
    try:
        passed = _evaluate(client, cfg, text, row, idx, meta)
    except Exception as e:
        print(f"warning: task {cfg.get('name')!r} row {idx}: evaluation error: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        passed = False
    meta["evaluation_passed"] = passed
    return text, passed, meta


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

    def keep(text, passed):
        if text is None:
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
        for (text, passed, meta), (_, nm) in zip(outs, plan):
            attempts.append((text, passed, meta, nm))
            if keep(text, passed):
                collected += 1

    of = cfg["output_field"]
    output = [{of: text, "icl_setup": nm} for text, passed, _, nm in attempts if keep(text, passed)]
    n_att = len(attempts)
    if ev is not None:
        n_passed = sum(1 for _, passed, _, _ in attempts if passed is True)
    else:
        n_passed = len(output)
    result = {"passed": n_passed, "failed": n_att - n_passed, "attempts": n_att}
    return {"output": output, "result": result, "metas": [a[2] for a in attempts], "api_ok": True}


def _safe_process(client, cfg, row, messages, idx):
    try:
        if cfg.get("list_mode"):
            return process_row_multi(client, cfg, row, messages, idx)
        return process_row(client, cfg, row, messages, idx)
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
        if args.output is None:
            raise ConfigError("--output is required")
        if os.path.isdir(args.output):
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

    if args.output is None:
        raise ConfigError("--output is required")
    if os.path.exists(args.output) and not os.path.isdir(args.output):
        raise ConfigError(f"--output {args.output} must be a directory for multi-task configs")

    by_name = {t[0]: t for t in tasks}
    jobs = []
    for n in run_names:
        _, task, prefix = by_name[n]
        cfg = build_task(n, task, prefix, args, config_dir)
        rows = load_input(paths[n])
        prepared = prepare(cfg, rows)
        jobs.append({"cfg": cfg, "rows": rows, "prepared": prepared,
                     "output_path": os.path.join(args.output, f"{n}.jsonl")})
    return True, jobs


def run(args):
    try:
        multi, jobs = resolve_jobs(args)
    except ConfigError as e:
        fail(str(e))

    if multi:
        try:
            os.makedirs(args.output, exist_ok=True)
        except OSError as e:
            fail(f"cannot create output directory {args.output}: {e}")
    files = []
    for job in jobs:
        try:
            files.append(open(job["output_path"], "w", encoding="utf-8"))
        except OSError as e:
            for f in files:
                f.close()
            fail(f"cannot open output file {job['output_path']}: {e}")

    # The server queues requests, so keep plenty in flight: enough to cover
    # the configured per-minute capacity even with multi-second latencies.
    stats = Stats()
    transports = {}
    url_inflight = {}
    for job in jobs:
        cfg = job["cfg"]
        mi = max(8, min(cfg["rpm"], 256))
        url_inflight[cfg["api_url"]] = max(url_inflight.get(cfg["api_url"], 0), mi)
    for url, mi in url_inflight.items():
        transports[url] = Transport(url, mi, stats)
    clients = [Client(job["cfg"], transports[job["cfg"]["api_url"]]) for job in jobs]

    total_rows = sum(len(job["rows"]) for job in jobs)
    # Extra workers beyond the in-flight limit let script evaluations overlap with requests.
    workers = max(1, min(total_rows, 2 * sum(url_inflight.values()) + 8, 512))
    results = [[None] * len(job["rows"]) for job in jobs]
    if total_rows:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = []
            # interleave submission across tasks so all tasks make progress together
            longest = max(len(job["rows"]) for job in jobs)
            for i in range(longest):
                for j, job in enumerate(jobs):
                    if i < len(job["rows"]):
                        futs.append((j, i, pool.submit(_safe_process, clients[j], job["cfg"],
                                                       job["rows"][i], job["prepared"][i], i)))
            for j, i, fut in futs:
                results[j][i] = fut.result()

    g_passed = g_failed = g_tp = g_tc = g_total = 0
    task_summaries = {}
    for j, job in enumerate(jobs):
        passed = failed = tp = tc = sols = 0
        list_mode = job["cfg"].get("list_mode")
        with files[j] as out_f:
            for row, res in zip(job["rows"], results[j]):
                metas = res["metas"]
                for m in metas:
                    tp += m.get("total_prompt_tokens", m.get("prompt_tokens", 0))
                    tc += m.get("total_completion_tokens", m.get("completion_tokens", 0))
                    jm = m.get("judge_meta")
                    if jm:
                        tp += jm["prompt_tokens"]
                        tc += jm["completion_tokens"]
                if list_mode:
                    sols += len(res["output"])
                    if res["result"]["passed"] > 0:
                        passed += 1
                    else:
                        failed += 1
                    meta_out = metas
                else:
                    rp = res["result"]["passed"]
                    if res["output"] is not None:
                        sols += 1
                    if res["output"] is None or rp is False:
                        failed += 1
                    elif rp is True or (rp is None and res["api_ok"]):
                        passed += 1
                    meta_out = metas[0] if len(metas) == 1 else metas
                line = {"input": row, "output": res["output"], "result": res["result"], "meta": meta_out}
                out_f.write(json.dumps(line, ensure_ascii=False) + "\n")
        name = job["cfg"]["name"] or "task"
        task_summaries[name] = {
            "total": len(job["rows"]),
            "passed": passed,
            "failed": failed,
            "total_solutions": sols,
            "avg_solutions_per_input": (sols / len(job["rows"])) if job["rows"] else 0.0,
            "total_api_calls": clients[j].counter.value,
            "total_prompt_tokens": tp,
            "total_completion_tokens": tc,
        }
        g_total += len(job["rows"])
        g_passed += passed
        g_failed += failed
        g_tp += tp
        g_tc += tc

    if stats.first_start is not None and stats.last_end is not None:
        elapsed = max(0.0, stats.last_end - stats.first_start)
    else:
        elapsed = 0.0
    throughput = (stats.calls / elapsed * 60) if elapsed > 0 else 0.0
    summary = {
        "total": g_total,
        "passed": g_passed,
        "failed": g_failed,
        "total_prompt_tokens": g_tp,
        "total_completion_tokens": g_tc,
        "total_api_calls": stats.calls,
        "elapsed_seconds": round(elapsed, 1),
        "throughput_rpm": round(throughput, 1),
        "tasks": task_summaries,
    }
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
    r.add_argument("--output", required=True,
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
    args = p.parse_args(argv)
    if args.command != "run":
        p.error("a command is required: run")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
