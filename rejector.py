#!/usr/bin/env python3
"""rejector: run prompt tasks against an OpenAI-compatible chat completions API."""

import argparse
import json
import math
import re
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

SCHEMES = ("greedy", "sample", "rejection")
EVAL_TYPES = ("exact_match", "contains", "regex")
EXTRACTS = ("last_number", "last_line", "full")
MAX_HTTP_TRIES = 3  # a request is abandoned after its third 5xx failure
REQUEST_TIMEOUT = 600
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
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def load_config(path, args):
    if yaml is None:
        raise ConfigError("PyYAML is not installed (pip install -r requirements.txt)")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in config file {path}: {e}")

    if not isinstance(raw, dict) or not isinstance(raw.get("task"), dict):
        raise ConfigError("config must contain a top-level 'task' mapping")
    task = raw["task"]

    cfg = {}
    cfg["name"] = task.get("name")

    api_url = args.api_url if args.api_url is not None else task.get("api_url")
    if not isinstance(api_url, str) or not api_url.strip():
        raise ConfigError("task.api_url is required and must be a non-empty string")
    cfg["api_url"] = api_url.strip().rstrip("/")

    model = args.model if args.model is not None else task.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("task.model is required and must be a non-empty string")
    cfg["model"] = model

    rpm = args.rpm if args.rpm is not None else task.get("rpm", 60)
    if not _is_int(rpm) or rpm <= 0:
        raise ConfigError(f"task.rpm must be a positive integer, got {rpm!r}")
    cfg["rpm"] = rpm

    prompt = task.get("prompt")
    if not isinstance(prompt, dict):
        raise ConfigError("task.prompt is required and must be a mapping with 'user' (and optional 'system')")
    system = prompt.get("system")
    user = prompt.get("user")
    if system is not None and not isinstance(system, str):
        raise ConfigError("task.prompt.system must be a string")
    if not isinstance(user, str):
        raise ConfigError("task.prompt.user is required and must be a string")
    cfg["system"] = system
    cfg["user"] = user

    gen = task.get("generation", {})
    if gen is None:
        gen = {}
    if not isinstance(gen, dict):
        raise ConfigError("task.generation must be a mapping")

    scheme = args.scheme if args.scheme is not None else gen.get("scheme", "greedy")
    if scheme not in SCHEMES:
        raise ConfigError(f"task.generation.scheme must be one of {', '.join(SCHEMES)}, got {scheme!r}")
    cfg["scheme"] = scheme

    max_tokens = args.max_tokens if args.max_tokens is not None else gen.get("max_tokens", 512)
    if not _is_int(max_tokens) or max_tokens <= 0:
        raise ConfigError(f"task.generation.max_tokens must be a positive integer, got {max_tokens!r}")
    cfg["max_tokens"] = max_tokens

    temperature = args.temperature if args.temperature is not None else gen.get("temperature")
    if temperature is not None and (not _is_num(temperature) or math.isnan(temperature)
                                    or math.isinf(temperature)):
        raise ConfigError(f"task.generation.temperature must be a number, got {temperature!r}")
    if scheme == "greedy":
        temperature = 0.0
    else:
        if temperature is None:
            temperature = 1.0
        if temperature <= 0:
            raise ConfigError(f"temperature must be > 0 for scheme '{scheme}', got {temperature!r}")
    cfg["temperature"] = float(temperature)

    n = args.n if args.n is not None else gen.get("n", 1)
    if scheme == "rejection":
        if not _is_int(n) or n < 1:
            raise ConfigError(f"task.generation.n must be a positive integer for rejection, got {n!r}")
    else:
        n = 1
    cfg["n"] = n

    ev = task.get("evaluation")
    if ev is None:
        if scheme == "rejection":
            raise ConfigError("task.evaluation is required for scheme 'rejection'")
        cfg["evaluation"] = None
    else:
        if not isinstance(ev, dict):
            raise ConfigError("task.evaluation must be a mapping")
        etype = ev.get("type")
        if etype not in EVAL_TYPES:
            raise ConfigError(f"task.evaluation.type must be one of {', '.join(EVAL_TYPES)}, got {etype!r}")
        extract = ev.get("extract", "full")
        if extract not in EXTRACTS:
            raise ConfigError(f"task.evaluation.extract must be one of {', '.join(EXTRACTS)}, got {extract!r}")
        e = {"type": etype, "extract": extract, "answer_field": None, "pattern": None}
        if etype in ("exact_match", "contains"):
            af = ev.get("answer_field")
            if not isinstance(af, str) or not af:
                raise ConfigError(f"task.evaluation.answer_field is required for evaluation type '{etype}'")
            e["answer_field"] = af
        else:
            pat = ev.get("pattern")
            if not isinstance(pat, str) or not pat:
                raise ConfigError("task.evaluation.pattern is required for evaluation type 'regex'")
            try:
                e["regex"] = re.compile(pat)
            except re.error as err:
                raise ConfigError(f"task.evaluation.pattern is not a valid regex: {err}")
            e["pattern"] = pat
        cfg["evaluation"] = e

    of = task.get("output_field", "output")
    if not isinstance(of, str) or not of:
        raise ConfigError("task.output_field must be a non-empty string")
    cfg["output_field"] = of
    return cfg


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
                    raise ConfigError(f"input line {lineno} (row {len(rows)}) is not valid JSON: {e}")
                if not isinstance(obj, dict):
                    raise ConfigError(f"input line {lineno} (row {len(rows)}) must be a JSON object")
                rows.append(obj)
    except OSError as e:
        raise ConfigError(f"cannot read input file {path}: {e}")
    return rows


def _stringify(v):
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def render(template, row, idx):
    def sub(m):
        key = m.group(1)
        if key not in row:
            raise ConfigError(f"row {idx}: missing field '{key}' referenced by prompt template")
        return _stringify(row[key])
    return PLACEHOLDER_RE.sub(sub, template)


def prepare(cfg, rows):
    prepared = []
    ev = cfg["evaluation"]
    for idx, row in enumerate(rows):
        system = render(cfg["system"], row, idx) if cfg["system"] is not None else None
        user = render(cfg["user"], row, idx)
        if ev is not None and ev["answer_field"] is not None and ev["answer_field"] not in row:
            raise ConfigError(f"row {idx}: missing field '{ev['answer_field']}' (evaluation answer_field)")
        msgs = []
        if system is not None:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        prepared.append(msgs)
    return prepared


# ---------------------------------------------------------------- evaluation


def extract(text, method):
    if method == "full":
        return text
    if method == "last_line":
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1].strip() if lines else None
    nums = NUMBER_RE.findall(text)
    if not nums:
        return None
    return nums[-1].replace(",", "").lstrip("+")


def _num(s):
    try:
        return float(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def evaluate(ev, text, row):
    """Return (passed, extracted_answer)."""
    if ev is None:
        return None, None
    if text is None:
        text = ""
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
    na, nb = _num(a), _num(b)
    if ev["extract"] == "last_number" and na is not None and nb is not None:
        return (na == nb), extracted
    return False, extracted


# ---------------------------------------------------------------- client


class Client:
    def __init__(self, cfg, max_inflight):
        self.cfg = cfg
        self.url = cfg["api_url"] + "/v1/chat/completions"
        self.sem = threading.BoundedSemaphore(max_inflight)
        self.lock = threading.Lock()
        self.calls = 0
        self.first_start = None
        self.last_end = None

    def _post(self, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.sem:
            t0 = time.monotonic()
            with self.lock:
                self.calls += 1
                if self.first_start is None or t0 < self.first_start:
                    self.first_start = t0
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
            with self.lock:
                if self.last_end is None or t1 > self.last_end:
                    self.last_end = t1
        return status, payload, err, int(round((t1 - t0) * 1000))

    def complete(self, messages):
        """One logical attempt with retries. Returns (ok, text, meta)."""
        body = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": self.cfg["temperature"],
            "max_tokens": self.cfg["max_tokens"],
        }
        meta = None
        for _ in range(MAX_HTTP_TRIES):
            status, payload, err, latency = self._post(body)
            meta = {"model": self.cfg["model"], "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "latency_ms": latency, "finish_reason": None}
            if status is not None and 200 <= status < 300:
                try:
                    obj = json.loads(payload.decode("utf-8"))
                    choice = obj["choices"][0]
                    text = choice.get("message", {}).get("content")
                    if text is None:
                        text = ""
                    usage = obj.get("usage") or {}
                    meta["model"] = self.cfg["model"]
                    meta["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
                    meta["completion_tokens"] = int(usage.get("completion_tokens") or 0)
                    meta["total_tokens"] = int(usage.get("total_tokens")
                                               or meta["prompt_tokens"] + meta["completion_tokens"])
                    meta["finish_reason"] = choice.get("finish_reason")
                    return True, text, meta
                except Exception:
                    return False, None, meta
            if status is not None and status < 500:
                return False, None, meta  # 4xx: not retryable
            # 5xx or connection-level failure: retry
        return False, None, meta


# ---------------------------------------------------------------- processing


def process_row(client, cfg, row, messages):
    ev = cfg["evaluation"]
    scheme = cfg["scheme"]
    attempts = cfg["n"] if scheme == "rejection" else 1
    metas = []
    for i in range(attempts):
        ok, text, meta = client.complete(messages)
        metas.append(meta)
        if not ok:
            # API failure after retries: row failed
            return {
                "output": None,
                "result": {"passed": False if ev is not None else None,
                           "extracted_answer": None, "attempts": i + 1},
                "metas": metas,
                "api_ok": False,
            }
        passed, extracted = evaluate(ev, text, row)
        if scheme != "rejection" or passed:
            return {
                "output": {cfg["output_field"]: text},
                "result": {"passed": passed, "extracted_answer": extracted, "attempts": i + 1},
                "metas": metas,
                "api_ok": True,
            }
    return {
        "output": None,
        "result": {"passed": False, "extracted_answer": None, "attempts": attempts},
        "metas": metas,
        "api_ok": True,
    }


def run(args):
    try:
        cfg = load_config(args.config, args)
        rows = load_input(args.input)
        prepared = prepare(cfg, rows)
    except ConfigError as e:
        fail(str(e))

    try:
        out_f = open(args.output, "w", encoding="utf-8")
    except OSError as e:
        fail(f"cannot open output file {args.output}: {e}")

    # The server queues requests, so keep plenty in flight: enough to cover
    # the configured per-minute capacity even with multi-second latencies.
    max_inflight = max(8, min(cfg["rpm"], 256))
    workers = max(1, min(len(rows), max_inflight))
    client = Client(cfg, max_inflight)

    results = [None] * len(rows)
    if rows:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(process_row, client, cfg, rows[i], prepared[i]) for i in range(len(rows))]
            for i, fut in enumerate(futs):
                results[i] = fut.result()

    passed = failed = 0
    tp = tc = 0
    with out_f:
        for row, res in zip(rows, results):
            metas = res["metas"]
            for m in metas:
                tp += m["prompt_tokens"]
                tc += m["completion_tokens"]
            rp = res["result"]["passed"]
            if res["output"] is None or rp is False:
                failed += 1
            elif rp is True or (rp is None and res["api_ok"]):
                passed += 1
            meta_out = metas[0] if len(metas) == 1 else metas
            line = {"input": row, "output": res["output"], "result": res["result"], "meta": meta_out}
            out_f.write(json.dumps(line, ensure_ascii=False) + "\n")

    if client.first_start is not None and client.last_end is not None:
        elapsed = max(0.0, client.last_end - client.first_start)
    else:
        elapsed = 0.0
    throughput = (client.calls / elapsed * 60) if elapsed > 0 else 0.0
    summary = {
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "total_prompt_tokens": tp,
        "total_completion_tokens": tc,
        "total_api_calls": client.calls,
        "elapsed_seconds": round(elapsed, 1),
        "throughput_rpm": round(throughput, 1),
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
    r = sub.add_parser("run", help="run a task")
    r.error = p.error
    r.add_argument("--config", required=True)
    r.add_argument("--input", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--api-url", dest="api_url")
    r.add_argument("--model")
    r.add_argument("--rpm", type=int)
    r.add_argument("--max-tokens", dest="max_tokens", type=int)
    r.add_argument("--scheme", choices=SCHEMES)
    r.add_argument("--temperature", type=float)
    r.add_argument("--n", type=int)
    args = p.parse_args(argv)
    if args.command != "run":
        p.error("a command is required: run")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
