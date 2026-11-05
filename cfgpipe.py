#!/usr/bin/env python3
"""cfgpipe - configuration resolver."""
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

SOURCES = ("default", "env", "file", "primary-store", "secondary-store", "arg")
STORE_TIMEOUT = 10
TYPES = ("string", "integer", "float", "boolean", "duration", "pattern",
         "map", "list", "redacted")
CUSTOM_TYPES = ("port",)
ALL_TYPES = TYPES + CUSTOM_TYPES


class CfgError(Exception):
    pass


class ParseFailure(Exception):
    pass


_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_TRUE = {"true", "t", "yes", "y", "on", "1"}
_FALSE = {"false", "f", "no", "n", "off", "0"}


_DURATION_RE = re.compile(r"^(?:[0-9]+[hms])+$")
_DURATION_PART_RE = re.compile(r"([0-9]+)([hms])")
_DURATION_MULT = {"h": 3600, "m": 60, "s": 1}
REDACTED_MASK = "<masked>"


def parse_duration_value(raw):
    s = raw.strip()
    if not _DURATION_RE.match(s):
        raise ParseFailure("invalid duration %r: expected one or more "
                           "integer-unit pairs with units h, m, s" % raw)
    total = 0
    for num, unit in _DURATION_PART_RE.findall(s):
        total += int(num) * _DURATION_MULT[unit]
    return total


def format_duration(seconds):
    if seconds == 0:
        return "0s"
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    out = ""
    if h:
        out += "%dh" % h
    if m:
        out += "%dm" % m
    if sec:
        out += "%ds" % sec
    return out


def format_float(v):
    text = "%.6f" % v
    if text.startswith("-") and float(text) == 0:
        text = text[1:]
    return text


def parse_value(typ, raw):
    """Parse a raw source string into the type-native value."""
    if typ in ("string", "redacted"):
        return raw
    if typ == "pattern":
        try:
            re.compile(raw)
        except (re.error, RecursionError, OverflowError, ValueError) as exc:
            raise ParseFailure("invalid pattern %r: %s" % (raw, exc))
        return raw
    if typ == "list":
        if raw == "":
            return []
        return raw.split(",")
    if typ == "map":
        result = {}
        if raw == "":
            return result
        for seg in raw.split(","):
            if ":" not in seg:
                raise ParseFailure("invalid map entry %r in %r: missing ':'"
                                   % (seg, raw))
            k, v = seg.split(":", 1)
            result[k] = v
        return result
    if typ == "duration":
        return parse_duration_value(raw)
    s = raw.strip()
    if typ == "integer":
        if not _INT_RE.match(s):
            raise ParseFailure("invalid integer %r" % raw)
        return int(s)
    if typ == "float":
        if not _FLOAT_RE.match(s):
            raise ParseFailure("invalid float %r" % raw)
        try:
            v = float(s)
        except (ValueError, OverflowError):
            raise ParseFailure("invalid float %r" % raw)
        if math.isinf(v) or math.isnan(v):
            raise ParseFailure("float out of range %r" % raw)
        if v == 0:
            v = 0.0
        return v
    if typ == "boolean":
        low = s.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ParseFailure("invalid boolean %r" % raw)
    if typ == "port":
        if not _INT_RE.match(s):
            raise ParseFailure("invalid port %r: not a base-10 integer" % raw)
        v = int(s)
        if v < 0 or v > 65535:
            raise ParseFailure("invalid port %r: out of range 0-65535" % raw)
        return v
    raise ParseFailure("unknown type %r" % typ)


def render(typ, value):
    """String representation of a resolved value (used by change events)."""
    if value is None:
        return ""
    if typ == "redacted":
        return REDACTED_MASK
    if typ == "float":
        return format_float(value)
    if typ == "boolean":
        return "true" if value else "false"
    if typ in ("integer", "port"):
        return str(value)
    if typ == "duration":
        return format_duration(value)
    if typ == "map":
        return ",".join("%s:%s" % (k, value[k]) for k in sorted(value))
    if typ == "list":
        return ",".join(value)
    return value


class RawJSON:
    """Pre-serialized JSON fragment."""

    def __init__(self, text):
        self.text = text


def to_json_value(typ, value):
    """Type-native JSON value for resolved configuration output."""
    if typ == "redacted":
        return REDACTED_MASK
    if typ == "float":
        return RawJSON(format_float(value))
    if typ == "integer":
        return RawJSON(str(int(value)))
    if typ == "boolean":
        return bool(value)
    if typ == "duration":
        return format_duration(value)
    if typ == "port":
        return str(value)
    if typ == "map":
        return {str(k): str(v) for k, v in value.items()}
    if typ == "list":
        return [str(x) for x in value]
    return value


def changed(typ, before, after):
    if typ == "redacted":
        return before != after
    if (before is None) != (after is None):
        return True
    return render(typ, before) != render(typ, after)


def _no_dupes(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise CfgError("duplicate key %r in schema" % k)
        obj[k] = v
    return obj


def _bad_constant(name):
    raise CfgError("invalid JSON constant %s in schema" % name)


def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise CfgError("schema file not found: %s" % path)
    except (OSError, UnicodeDecodeError) as exc:
        raise CfgError("cannot read schema file %s: %s" % (path, exc))
    try:
        schema = json.loads(text, object_pairs_hook=_no_dupes,
                            parse_constant=_bad_constant)
    except json.JSONDecodeError as exc:
        raise CfgError("invalid JSON in schema %s: %s" % (path, exc))
    if not isinstance(schema, dict):
        raise CfgError("schema root must be a JSON object")
    if not schema:
        raise CfgError("schema root must be a non-empty JSON object")
    params = []
    _walk_group(schema, (), params)
    for store_src in ("primary-store", "secondary-store"):
        seen = {}
        for path, decl in params:
            if store_src in decl:
                key = decl[store_src]
                if key in seen:
                    raise CfgError("parameters %r and %r share %s key %r"
                                   % (dotted(seen[key]), dotted(path),
                                      store_src, key))
                seen[key] = path
    return schema, params


def dotted(path):
    return ".".join(path)


def is_param(obj):
    return isinstance(obj, dict) and isinstance(obj.get("type"), str)


def _walk_group(group, prefix, params):
    gname = dotted(prefix) if prefix else "<root>"
    for key in SOURCES:
        if key in group and not isinstance(group[key], dict):
            raise CfgError("group %r: must not carry source annotation %r"
                           % (gname, key))
    for name, entry in group.items():
        if name == "" or "." in name:
            raise CfgError("group %r: invalid entry name %r" % (gname, name))
        path = prefix + (name,)
        if not isinstance(entry, dict):
            if name == "type":
                raise CfgError("group %r: invalid 'type' field %r"
                               % (gname, entry))
            raise CfgError("%r: entry must be an object (parameter declaration "
                           "or group)" % dotted(path))
        if is_param(entry):
            _check_param(path, entry)
            params.append((path, entry))
        else:
            _walk_group(entry, path, params)


def _check_param(path, decl):
    name = dotted(path)
    if decl["type"] not in ALL_TYPES:
        raise CfgError("parameter %r: unrecognized type %r (expected one of %s)"
                       % (name, decl["type"], ", ".join(ALL_TYPES)))
    for src in SOURCES:
        if src in decl and not isinstance(decl[src], str):
            raise CfgError("parameter %r: field %r must be a string"
                           % (name, src))


def parse_args(candidates):
    """Return dict name -> value; last occurrence wins."""
    result = {}
    i = 0
    n = len(candidates)
    while i < n:
        tok = candidates[i]
        i += 1
        if tok.startswith("--"):
            body = tok[2:]
        elif tok.startswith("-"):
            body = tok[1:]
        else:
            continue
        if not body:
            continue
        if "=" in body:
            name, value = body.split("=", 1)
            if name:
                result[name] = value
        elif i < n and not candidates[i].startswith("-"):
            result[body] = candidates[i]
            i += 1
    return result


def read_file_source(path):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    content = content.strip()
    if content == "":
        return None
    return content


def _http(method, url, body=None, timeout=STORE_TIMEOUT):
    """Perform an HTTP request; return (status, decoded JSON or None).

    Raises OSError-like exceptions on connector failure."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:
            raw = b""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        parsed = None
    return status, parsed


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class PrimaryStore:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def get_versioned(self, key):
        """Return (value, version) or (None, None) if the key is missing."""
        url = "%s/v1/primary/kv?key=%s" % (
            self.base_url, urllib.parse.quote(key, safe=""))
        try:
            status, data = _http("GET", url)
        except Exception as exc:
            raise CfgError("primary-store: request for key %r failed: %s"
                           % (key, exc))
        if status == 200:
            if isinstance(data, dict) and data.get("found") is False:
                return None, None
            if (not isinstance(data, dict) or data.get("found") is not True
                    or not isinstance(data.get("value"), str)
                    or ("version" in data and data["version"] is not None
                        and not _is_int(data["version"]))):
                raise CfgError("primary-store: malformed response for key %r"
                               % key)
            return data["value"], data.get("version")
        if status == 404:
            if isinstance(data, dict) and data.get("found") is False:
                return None, None
            raise CfgError("primary-store: malformed 404 response for key %r"
                           % key)
        raise CfgError("primary-store: unexpected HTTP status %d for key %r"
                       % (status, key))

    def get(self, key):
        return self.get_versioned(key)[0]

    def watch(self, cursor, keys, timeout=None):
        """Return (next_cursor, events)."""
        query = [("cursor", str(cursor))] + [("key", k) for k in keys]
        url = "%s/v1/primary/watch?%s" % (
            self.base_url, urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe=""))
        try:
            status, data = _http("GET", url, timeout=timeout)
        except Exception as exc:
            raise CfgError("primary-store: watch request failed: %s" % exc)
        if status != 200:
            raise CfgError("primary-store: watch returned unexpected HTTP "
                           "status %d" % status)
        if (not isinstance(data, dict) or not _is_int(data.get("cursor"))
                or not isinstance(data.get("events"), list)):
            raise CfgError("primary-store: malformed watch response")
        events = []
        for ev in data["events"]:
            if (not isinstance(ev, dict) or not isinstance(ev.get("key"), str)
                    or not (isinstance(ev.get("value"), str)
                            or ev.get("value") is None)
                    or "value" not in ev
                    or not _is_int(ev.get("version"))):
                raise CfgError("primary-store: malformed watch event %r" % (ev,))
            events.append((ev["key"], ev["value"], ev["version"]))
        return data["cursor"], events


class SecondaryStore:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def get(self, key):
        url = "%s/v1/secondary/kv?key=%s" % (
            self.base_url, urllib.parse.quote(key, safe=""))
        try:
            status, data = _http("GET", url)
        except Exception as exc:
            raise CfgError("secondary-store: request for key %r failed: %s"
                           % (key, exc))
        if status in (200, 404) and isinstance(data, dict):
            if data.get("found") is False:
                return None
            if (status == 200 and data.get("found") is True
                    and isinstance(data.get("value"), str)):
                return data["value"]
            raise CfgError("secondary-store: malformed response for key %r"
                           % key)
        if status in (200, 404):
            raise CfgError("secondary-store: malformed response for key %r"
                           % key)
        raise CfgError("secondary-store: unexpected HTTP status %d for key %r"
                       % (status, key))

    def batch_read(self, keys):
        """Return list of (key, status, value) or raise CfgError."""
        url = "%s/v1/secondary/batch-read" % self.base_url
        try:
            status, data = _http("POST", url, body={"keys": list(keys)})
        except Exception as exc:
            raise CfgError("secondary-store: batch-read failed: %s" % exc)
        if status != 200:
            raise CfgError("secondary-store: batch-read returned HTTP %d"
                           % status)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise CfgError("secondary-store: malformed batch-read response")
        items = data["items"]
        by_key = {}
        for idx, item in enumerate(items):
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                by_key.setdefault(item["key"], item)
            elif idx < len(keys) and isinstance(item, dict):
                by_key.setdefault(keys[idx], item)
        out = []
        for key in keys:
            item = by_key.get(key)
            if item is None:
                # Per-key failure: no usable item for this key.
                out.append((key, "error", None))
                continue
            st = item.get("status")
            if st == "ok" and isinstance(item.get("value"), str):
                out.append((key, "ok", item["value"]))
            elif st == "missing":
                out.append((key, "missing", None))
            else:
                out.append((key, "error", None))
        return out

    def read_each(self, keys):
        """Per-key reads; operational failures are isolated per key."""
        out = []
        for key in keys:
            try:
                value = self.get(key)
            except Exception:
                out.append((key, "error", None))
                continue
            if value is None:
                out.append((key, "missing", None))
            else:
                out.append((key, "ok", value))
        return out


def compose_primary_key(prefix, key):
    if prefix is None:
        return key
    p = prefix.strip("/")
    k = key.lstrip("/")
    if p == "":
        return k
    return p + "/" + k


def compose_secondary_key(prefix, separator, key):
    if not prefix:
        return key
    return prefix + (separator or "") + key


def store_key(stores, src, key):
    if src == "primary-store":
        return compose_primary_key(stores.get("primary-store-prefix"), key)
    if src == "secondary-store":
        return compose_secondary_key(stores.get("secondary-store-key-prefix"),
                                     stores.get("secondary-store-key-separator"),
                                     key)
    return key


class Param:
    """Per-parameter resolution state: parsed value per source layer."""

    def __init__(self, path, decl):
        self.path = path
        self.name = dotted(path)
        self.decl = decl
        self.type = decl["type"]
        self.layers = {}
        self.primary_version = None

    def effective(self):
        value = None
        for src in SOURCES:
            if self.layers.get(src) is not None:
                value = self.layers[src]
        return value


def lookup(src, key, args, store):
    if src == "default":
        return key
    if src == "env":
        return os.environ.get(key)
    if src == "file":
        return read_file_source(key)
    if src in ("primary-store", "secondary-store"):
        return store.get(key)
    if src == "arg":
        return args.get(key)
    return None


def seed(params, args, stores):
    states = []
    unresolved = []
    for path, decl in params:
        st = Param(path, decl)
        for src in SOURCES:
            if src not in decl:
                continue
            key = decl[src]
            version = None
            if src == "primary-store":
                raw, version = stores["primary-store"].get_versioned(
                    store_key(stores, src, key))
            elif src == "secondary-store":
                raw = stores["secondary-store"].get(
                    store_key(stores, src, key))
            else:
                raw = lookup(src, key, args, None)
            if raw is None:
                continue
            try:
                st.layers[src] = parse_value(st.type, raw)
            except ParseFailure as exc:
                raise CfgError("parameter %r: failed to parse source %r as %s: %s"
                               % (st.name, src, st.type, exc))
            if src == "primary-store":
                st.primary_version = version
        if st.effective() is None:
            unresolved.append(st.name)
        states.append(st)
    if unresolved:
        raise CfgError("unresolved parameters: %s" % ", ".join(unresolved))
    return states


def build_output(schema, states):
    values = {st.path: to_json_value(st.type, st.effective()) for st in states}

    def walk(group, prefix):
        out = {}
        for name, entry in group.items():
            path = prefix + (name,)
            if is_param(entry):
                out[name] = values[path]
            else:
                out[name] = walk(entry, path)
        return out

    return walk(schema, ())


def resolve(schema, params, args, store=None, secondary=None):
    stores = {"primary-store": store, "secondary-store": secondary}
    return build_output(schema, seed(params, args, stores))


def _skeleton(group):
    out = {}
    for name, entry in group.items():
        if not is_param(entry):
            out[name] = _skeleton(entry)
    return out


def dumps(obj):
    if isinstance(obj, RawJSON):
        return obj.text
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(str(k)) + ":" + dumps(v)
                              for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in obj) + "]"
    if isinstance(obj, float):
        return format_float(obj)
    if isinstance(obj, int) and not isinstance(obj, bool):
        return str(obj)
    return json.dumps(obj)


_DUR_RE = re.compile(r"([0-9]*\.?[0-9]+)(ns|us|µs|ms|s|m|h)")
_DUR_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0,
              "m": 60.0, "h": 3600.0}


def parse_duration(text):
    """Parse a Go-style duration ("2s", "500ms", "1m30s"); bare numbers are
    seconds. Returns seconds as float."""
    s = text.strip()
    if s == "":
        raise CfgError("invalid duration %r" % text)
    sign = 1.0
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1.0
        s = s[1:]
    if re.fullmatch(r"[0-9]*\.?[0-9]+", s):
        return sign * float(s)
    pos = 0
    total = 0.0
    while pos < len(s):
        m = _DUR_RE.match(s, pos)
        if not m:
            raise CfgError("invalid duration %r" % text)
        total += float(m.group(1)) * _DUR_UNITS[m.group(2)]
        pos = m.end()
    if pos == 0:
        raise CfgError("invalid duration %r" % text)
    return sign * total


class Watcher:
    def __init__(self, states, stores, poll_interval):
        self.states = states
        self.stores = stores
        self.poll_interval = poll_interval
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.fatal = None
        self.by_primary = {}
        self.by_secondary = {}
        for st in states:
            if "primary-store" in st.decl and stores["primary-store"]:
                self.by_primary[store_key(stores, "primary-store",
                                          st.decl["primary-store"])] = st
            if "secondary-store" in st.decl and stores["secondary-store"]:
                self.by_secondary[store_key(stores, "secondary-store",
                                            st.decl["secondary-store"])] = st

    def monitorable(self):
        return bool(self.by_primary) or bool(self.by_secondary)

    def emit(self, st, previous, current):
        line = dumps({"path": st.name, "type": st.type,
                      "previous": previous, "current": current})
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def set_layer(self, st, src, value, force_event=False):
        """Update a layer; emit an event on visible change. Caller holds lock."""
        before = st.effective()
        st.layers[src] = value
        after = st.effective()
        if changed(st.type, before, after):
            self.emit(st, render(st.type, before), render(st.type, after))
        elif force_event:
            self.emit(st, "", render(st.type, after))

    def primary_loop(self):
        store = self.stores["primary-store"]
        keys = list(self.by_primary)
        cursor = 0
        try:
            while not self.done.is_set():
                cursor, events = store.watch(cursor, keys)
                with self.lock:
                    for key, value, version in events:
                        st = self.by_primary.get(key)
                        if st is None:
                            continue
                        if (st.primary_version is not None
                                and version <= st.primary_version):
                            continue
                        if value is None:
                            parsed = None
                        else:
                            try:
                                parsed = parse_value(st.type, value)
                            except ParseFailure:
                                continue
                        st.primary_version = version
                        self.set_layer(st, "primary-store", parsed)
        except CfgError as exc:
            self.fail(exc)
        except Exception as exc:  # unexpected
            self.fail(CfgError("primary-store monitoring failed: %s" % exc))

    def secondary_loop(self):
        store = self.stores["secondary-store"]
        keys = list(self.by_secondary)
        baseline = {}
        while not self.done.is_set():
            try:
                items = store.batch_read(keys)
            except Exception:
                try:
                    items = store.read_each(keys)
                except Exception:
                    items = []
            with self.lock:
                for key, status, value in items:
                    st = self.by_secondary.get(key)
                    if st is None:
                        continue
                    if status == "error":
                        continue
                    if status == "missing":
                        parsed = None
                    else:
                        try:
                            parsed = parse_value(st.type, value)
                        except ParseFailure:
                            continue
                    first = key not in baseline
                    baseline[key] = parsed
                    self.set_layer(st, "secondary-store", parsed,
                                   force_event=first)
            if self.done.wait(self.poll_interval):
                break

    def fail(self, exc):
        with self.lock:
            if self.fatal is None:
                self.fatal = exc
        self.done.set()

    def run(self):
        threads = []
        if self.by_primary:
            threads.append(threading.Thread(target=self.primary_loop,
                                            daemon=True))
        if self.by_secondary:
            threads.append(threading.Thread(target=self.secondary_loop,
                                            daemon=True))
        for t in threads:
            t.start()
        try:
            while not self.done.wait(0.5):
                pass
        except KeyboardInterrupt:
            return 0
        if self.fatal is not None:
            with self.lock:
                sys.stderr.write("error: %s\n" % self.fatal)
                sys.stderr.flush()
            return 1
        return 0


USAGE = ("usage: cfgpipe.py [--primary-store <base-url>] "
         "[--secondary-store <base-url>] "
         "[--secondary-store-poll-interval <duration>] "
         "[--primary-store-prefix <path>] "
         "[--secondary-store-key-prefix <prefix> "
         "--secondary-store-key-separator <separator>] [--watch] "
         "<schema-file> [arg-candidates...]\n")

_VALUE_FLAGS = ("primary-store", "secondary-store",
                "secondary-store-poll-interval", "primary-store-prefix",
                "secondary-store-key-prefix", "secondary-store-key-separator")


def parse_globals(argv):
    """Parse global flags preceding the schema path.

    Returns (options, index_of_schema_path)."""
    opts = {name: None for name in _VALUE_FLAGS}
    opts["watch"] = False
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-") or tok == "-":
            break
        if tok == "--watch":
            opts["watch"] = True
            i += 1
            continue
        matched = False
        for name in _VALUE_FLAGS:
            flag = "--" + name
            if tok == flag:
                if i + 1 >= n:
                    raise CfgError("%s requires a value" % flag)
                opts[name] = argv[i + 1]
                i += 2
                matched = True
                break
            if tok.startswith(flag + "="):
                opts[name] = tok[len(flag) + 1:]
                i += 1
                matched = True
                break
        if not matched:
            raise CfgError("unknown global flag %r" % tok)
    if i >= n:
        raise CfgError("missing schema file")
    return opts, i


DEFAULT_POLL_INTERVAL = 1.0


def main(argv):
    try:
        opts, idx = parse_globals(argv)
    except CfgError as exc:
        sys.stderr.write("error: %s\n" % exc)
        sys.stderr.write(USAGE)
        return 2
    watch = opts["watch"]
    try:
        if (opts["secondary-store-key-prefix"] is not None
                and opts["secondary-store-key-separator"] is None):
            raise CfgError("--secondary-store-key-prefix requires "
                           "--secondary-store-key-separator")
        schema, params = load_schema(argv[idx])
        stores = {"primary-store": None, "secondary-store": None,
                  "primary-store-prefix": opts["primary-store-prefix"],
                  "secondary-store-key-prefix":
                      opts["secondary-store-key-prefix"],
                  "secondary-store-key-separator":
                      opts["secondary-store-key-separator"]}
        classes = {"primary-store": PrimaryStore,
                   "secondary-store": SecondaryStore}
        for src in ("primary-store", "secondary-store"):
            base = opts[src]
            if base is not None:
                if base == "":
                    raise CfgError("--%s base URL must not be empty" % src)
                stores[src] = classes[src](base)
            else:
                needing = [dotted(p) for p, d in params if src in d]
                if needing:
                    raise CfgError("--%s is not configured but required by "
                                   "parameter(s): %s"
                                   % (src, ", ".join(needing)))
        for src in ("primary-store", "secondary-store"):
            seen = {}
            for path, decl in params:
                if src in decl:
                    ck = store_key(stores, src, decl[src])
                    if ck in seen:
                        raise CfgError("parameters %r and %r share composed "
                                       "%s key %r" % (dotted(seen[ck]),
                                                      dotted(path), src, ck))
                    seen[ck] = path
        poll_interval = DEFAULT_POLL_INTERVAL
        if watch and stores["secondary-store"] is not None:
            if opts["secondary-store-poll-interval"] is not None:
                poll_interval = parse_duration(
                    opts["secondary-store-poll-interval"])
            if not poll_interval > 0:
                raise CfgError("--secondary-store-poll-interval must be "
                               "strictly positive")
            if not any("secondary-store" in d for _, d in params):
                raise CfgError("--watch with --secondary-store requires at "
                               "least one parameter declaring "
                               "'secondary-store'")
        args = parse_args(argv[idx + 1:])
        states = seed(params, args, stores)
    except CfgError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    out = build_output(schema, states)
    if not watch:
        sys.stdout.write(dumps(out) + "\n")
        return 0
    for st in states:
        sys.stdout.write(dumps({"path": st.name, "type": st.type,
                                "previous": "",
                                "current": render(st.type, st.effective())})
                         + "\n")
    sys.stdout.write(dumps(out) + "\n")
    sys.stdout.flush()
    watcher = Watcher(states, stores, poll_interval)
    if not watcher.monitorable():
        return 0
    return watcher.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
