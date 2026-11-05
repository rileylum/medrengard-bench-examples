#!/usr/bin/env python3
"""Apply transformation rules to a directory tree of config files."""

import io
import json
import os
import re
import sys

EXTENSIONS = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
}

_MISSING = object()


class ParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def is_mapping(obj):
    if obj is None or isinstance(obj, (str, bytes, list, tuple)):
        return False
    return hasattr(obj, "keys") and hasattr(obj, "__getitem__") and hasattr(obj, "__setitem__")


def to_plain(obj):
    """Convert library-specific containers/scalars to plain python values."""
    if is_mapping(obj):
        return {str(k): to_plain(obj[k]) for k in obj.keys()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    if hasattr(obj, "unwrap"):
        try:
            return to_plain(obj.unwrap())
        except Exception:
            pass
    return obj


def strict_equal(a, b):
    a = to_plain(a)
    b = to_plain(b)
    return _strict_eq(a, b)


def _strict_eq(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a.keys()) != list(b.keys()):
            return False
        return all(_strict_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_strict_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return type(a) is type(b) and a == b
    if type(a) is not type(b):
        return False
    return a == b


def split_path(path):
    if isinstance(path, list):
        return [str(p) for p in path]
    return str(path).split(".")


def glob_to_regex(pattern):
    i = 0
    n = len(pattern)
    out = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # '**'
                j = i + 2
                while j < n and pattern[j] == "*":
                    j += 1
                at_start = i == 0 or pattern[i - 1] == "/"
                if at_start and j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                if at_start and j == n:
                    out.append(".*")
                    i = j
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
                i += 1
            else:
                stuff = pattern[i + 1:j]
                neg = False
                if stuff and stuff[0] in "!^":
                    neg = True
                    stuff = stuff[1:]
                stuff = stuff.replace("\\", "\\\\")
                if neg:
                    out.append("[^/" + stuff + "]")
                else:
                    out.append("[" + stuff + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("(?s:" + "".join(out) + r")\Z")


def glob_match(pattern, relpath):
    pattern = str(pattern)
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return glob_to_regex(pattern).match(relpath) is not None


# ---------------------------------------------------------------------------
# INI handling (line-based, format preserving)
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]*)\]\s*(?:[;#].*)?$")
_KV_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[^=:\s][^=:]*?)(?P<sep>\s*[=:]\s*)(?P<value>.*?)(?P<trail>\s*)$")
_KEY_ONLY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[^=:\s\[;#][^=:]*?)\s*$")


class IniDoc:
    """Format-preserving INI document.

    Top-level keys (before any section) are stored in the global section
    (name None). Sections are exposed as nested dicts.
    """

    def __init__(self, text):
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        # Each section: {"name": str|None, "header": str|None,
        #                "entries": [[kind, data]]}
        self.sections = []
        current = {"name": None, "header": None, "lines": []}
        self.sections.append(current)
        last_entry = None
        for line in lines:
            stripped = line.strip()
            if stripped == "" or stripped[0] in ";#":
                current["lines"].append({"kind": "raw", "text": line})
                last_entry = None
                continue
            m = _SECTION_RE.match(line)
            if m and line.lstrip().startswith("["):
                name = m.group("name").strip()
                current = {"name": name, "header": line, "lines": []}
                self.sections.append(current)
                last_entry = None
                continue
            if line[:1].isspace() and last_entry is not None:
                # continuation line
                last_entry["cont"].append(line)
                continue
            m = _KV_RE.match(line)
            if m:
                entry = {
                    "kind": "kv",
                    "indent": m.group("indent"),
                    "key": m.group("key").strip(),
                    "sep": m.group("sep"),
                    "value": m.group("value"),
                    "trail": m.group("trail"),
                    "cont": [],
                }
                current["lines"].append(entry)
                last_entry = entry
                continue
            m = _KEY_ONLY_RE.match(line)
            if m:
                entry = {
                    "kind": "kv",
                    "indent": m.group("indent"),
                    "key": m.group("key").strip(),
                    "sep": None,
                    "value": None,
                    "trail": "",
                    "cont": [],
                }
                current["lines"].append(entry)
                last_entry = entry
                continue
            raise ParseError("invalid ini line: %r" % line)
        self.default_sep = " = "
        for sec in self.sections:
            for e in sec["lines"]:
                if e["kind"] == "kv" and e["sep"]:
                    self.default_sep = e["sep"]
                    break
            else:
                continue
            break

    # -- conversion to/from nested dicts --------------------------------
    @staticmethod
    def _entry_value(e):
        if e["value"] is None:
            return None
        if e["cont"]:
            return "\n".join([e["value"]] + [c.strip() for c in e["cont"]])
        return e["value"]

    def to_dict(self):
        result = {}
        for sec in self.sections:
            if sec["name"] is None:
                target = result
            else:
                target = result.get(sec["name"])
                if not isinstance(target, dict):
                    target = {}
                    result[sec["name"]] = target
            for e in sec["lines"]:
                if e["kind"] == "kv":
                    target[e["key"]] = self._entry_value(e)
        return result

    @staticmethod
    def _format_value(value):
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, str)):
            return str(value)
        return json.dumps(value, ensure_ascii=False)

    def _render_kv(self, key, value, template=None):
        if template is not None:
            sep = template["sep"] or self.default_sep
            indent = template["indent"]
        else:
            sep = self.default_sep
            indent = ""
        sval = self._format_value(value)
        parts = sval.split("\n")
        entry = {
            "kind": "kv",
            "indent": indent,
            "key": key,
            "sep": sep,
            "value": parts[0],
            "trail": "",
            "cont": ["    " + p for p in parts[1:]],
        }
        return entry

    def from_dict(self, data):
        """Rewrite the document so it reflects `data`, preserving layout."""
        globals_ = {k: v for k, v in data.items() if not isinstance(v, dict)}
        section_data = {k: v for k, v in data.items() if isinstance(v, dict)}

        old_names = []
        for sec in self.sections:
            if sec["name"] is not None and sec["name"] not in old_names:
                old_names.append(sec["name"])
        removed = [n for n in old_names if n not in section_data]
        added = [n for n in section_data if n not in old_names]
        renames = dict(zip(removed, added))
        for sec in self.sections:
            if sec["name"] in renames:
                new_name = renames[sec["name"]]
                hdr = sec["header"]
                m = re.match(r"^(\s*\[)[^\]]*(\].*)$", hdr or "")
                sec["header"] = (m.group(1) + new_name + m.group(2)) if m else "[%s]" % new_name
                sec["name"] = new_name

        new_sections = []
        seen_sections = set()
        for sec in self.sections:
            if sec["name"] is None:
                desired = globals_
            else:
                if sec["name"] not in section_data:
                    continue  # section removed / renamed
                desired = section_data[sec["name"]]
                first = sec["name"] not in seen_sections
                seen_sections.add(sec["name"])
                if not first:
                    # duplicated section in file - keep entries only if they
                    # still exist; values were merged into the first one.
                    pass
            new_sections.append(self._rebuild_section(sec, desired))

        # newly added sections
        for name, values in section_data.items():
            if name in seen_sections:
                continue
            sec = {"name": name, "header": "[%s]" % name, "lines": []}
            sec = self._rebuild_section(sec, values)
            if new_sections:
                prev = new_sections[-1]
                if prev["lines"] or prev["header"] is not None:
                    has_blank = prev["lines"] and prev["lines"][-1]["kind"] == "raw" and prev["lines"][-1]["text"].strip() == ""
                    if not has_blank and (prev["lines"] or prev["header"]):
                        prev["lines"].append({"kind": "raw", "text": ""})
            new_sections.append(sec)
            seen_sections.add(name)
        self.sections = new_sections

    def _rebuild_section(self, sec, desired):
        desired = dict(desired)
        existing_keys = [e["key"] for e in sec["lines"] if e["kind"] == "kv"]
        lines = []
        placed = set()
        template = None
        for e in sec["lines"]:
            if e["kind"] != "kv":
                lines.append(e)
                continue
            template = e
            key = e["key"]
            if key in placed:
                continue
            if key not in desired:
                # Removed (possibly renamed). If renamed, the new key keeps
                # the position: find a key in desired not existing originally
                # that is at this ordinal position.
                continue
            placed.add(key)
            val = desired[key]
            if self._entry_value(e) == val or (
                    e["value"] is None and val is None):
                lines.append(e)
            else:
                new = self._render_kv(key, val, e)
                new["trail"] = e["trail"]
                lines.append(new)

        # Handle renames / new keys: insert keys at their position in the
        # desired ordering.
        desired_keys = list(desired.keys())
        for idx, key in enumerate(desired_keys):
            if key in placed:
                continue
            new = self._render_kv(key, desired[key], template)
            # find position: after the previously placed desired key
            pos = None
            for prev_key in reversed(desired_keys[:idx]):
                if prev_key in placed:
                    for li in range(len(lines) - 1, -1, -1):
                        if lines[li]["kind"] == "kv" and lines[li]["key"] == prev_key:
                            pos = li + 1
                            break
                    if pos is not None:
                        break
            if pos is None:
                # place before the first kv line, or after leading comments
                pos = None
                for li, ln in enumerate(lines):
                    if ln["kind"] == "kv":
                        pos = li
                        break
                if pos is None:
                    # append before trailing blank lines
                    pos = len(lines)
                    while pos > 0 and lines[pos - 1]["kind"] == "raw" and lines[pos - 1]["text"].strip() == "":
                        pos -= 1
            lines.insert(pos, new)
            placed.add(key)
        _ = existing_keys
        return {"name": sec["name"], "header": sec["header"], "lines": lines}

    def dump(self):
        out = []
        for sec in self.sections:
            if sec["header"] is not None:
                out.append(sec["header"])
            for e in sec["lines"]:
                if e["kind"] == "raw":
                    out.append(e["text"])
                else:
                    if e["sep"] is None:
                        out.append(e["indent"] + e["key"])
                    else:
                        out.append(e["indent"] + e["key"] + e["sep"] + e["value"] + e["trail"])
                    out.extend(e["cont"])
        text = self.newline.join(out)
        if out and (self.trailing_newline or True):
            text += self.newline
        if not out:
            text = ""
        return text


# ---------------------------------------------------------------------------
# Format handlers
# ---------------------------------------------------------------------------

class ConfigFile:
    def __init__(self, path, rel, fmt, label):
        self.path = path
        self.rel = rel
        self.fmt = fmt
        self.label = label
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            self.text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ParseError(label)
        if self.text.startswith("﻿"):
            self.bom = True
            self.text = self.text[1:]
        else:
            self.bom = False
        try:
            getattr(self, "_load_" + fmt)()
        except ParseError:
            raise
        except Exception:
            raise ParseError(label)

    # JSON ---------------------------------------------------------------
    def _load_json(self):
        self.data = json.loads(self.text)
        text = self.text
        self.json_indent = None
        m = re.search(r"\n([ \t]+)\S", text)
        if m:
            ws = m.group(1)
            if "\t" in ws:
                self.json_indent = "\t"
            else:
                # Use smallest indentation found among lines
                indents = [len(x) for x in re.findall(r"\n( +)\S", text)]
                self.json_indent = min(indents) if indents else len(ws)
        elif "\n" in text.strip():
            self.json_indent = 0
        self.json_compact = False
        if self.json_indent is None:
            stripped = text.strip()
            # Detect compact separators
            if re.search(r'"\s*:\s', stripped) is None and re.search(r'[,:]\s', stripped) is None and len(stripped) > 2:
                self.json_compact = True
        self.trailing_newline = text.endswith("\n")
        self.newline = "\r\n" if "\r\n" in text else "\n"

    def _dump_json(self):
        if self.json_indent is not None:
            out = json.dumps(self.data, indent=self.json_indent, ensure_ascii=False)
        elif self.json_compact:
            out = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        else:
            out = json.dumps(self.data, ensure_ascii=False)
        if self.newline != "\n":
            out = out.replace("\n", self.newline)
        if self.trailing_newline:
            out += self.newline
        return out

    # YAML ---------------------------------------------------------------
    def _make_yaml(self):
        from ruamel.yaml import YAML
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.width = 4096
        return yaml

    def _load_yaml(self):
        yaml = self._make_yaml()
        self.yaml = yaml
        self.data = yaml.load(self.text)
        mapping, offset = guess_yaml_indent(self.text)
        yaml.indent(mapping=mapping, sequence=offset + 2, offset=offset)
        self.yaml_explicit_start = self.text.lstrip().startswith("---")
        if self.yaml_explicit_start:
            yaml.explicit_start = True

    def _dump_yaml(self):
        if self.data is None:
            return ""
        buf = io.StringIO()
        self.yaml.dump(self.data, buf)
        return buf.getvalue()

    # TOML ---------------------------------------------------------------
    def _load_toml(self):
        import tomlkit
        self.data = tomlkit.parse(self.text)

    def _dump_toml(self):
        import tomlkit
        text = tomlkit.dumps(self.data)
        if getattr(self, "toml_fix_spacing", False):
            text = _fix_toml_spacing(text)
        return text

    # INI ----------------------------------------------------------------
    def _load_ini(self):
        self.ini = IniDoc(self.text)
        self.data = self.ini.to_dict()

    def _dump_ini(self):
        self.ini.from_dict(self.data)
        return self.ini.dump()

    # --------------------------------------------------------------------
    def dump(self):
        text = getattr(self, "_dump_" + self.fmt)()
        if self.bom:
            text = "﻿" + text
        return text

    def save(self):
        text = self.dump()
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    # -- value conversion -------------------------------------------------
    def convert(self, value):
        """Prepare a python value for insertion into this document."""
        if self.fmt == "toml":
            return value  # tomlkit converts on assignment
        if self.fmt == "yaml":
            return _to_yaml(value)
        import copy
        return copy.deepcopy(value)

    def value_supported(self, value):
        if self.fmt == "toml":
            return _toml_supported(value)
        return True


def guess_yaml_indent(text):
    """Return (mapping_indent, sequence_dash_offset) guessed from text."""
    mapping = None
    offset = None
    prev_key_indent = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.lstrip(" ")
        if prev_key_indent is not None and indent >= prev_key_indent:
            diff = indent - prev_key_indent
            if content.startswith("- ") or content == "-":
                if offset is None:
                    offset = diff
            elif diff > 0 and mapping is None:
                mapping = diff
        prev_key_indent = None
        # line of the form "key:" (opening a nested block)
        body = content[2:].lstrip(" ") if content.startswith("- ") else content
        if re.match(r"^[^#]*:\s*(#.*)?$", body) and not body.startswith(("'", '"')) or \
                re.match(r"^(['\"]).*\1:\s*(#.*)?$", body):
            prev_key_indent = len(line) - len(body)
        if mapping is not None and offset is not None:
            break
    if mapping is None or mapping < 1:
        mapping = 2
    if offset is None:
        offset = 0
    return mapping, offset


def _toml_supported(value):
    if value is None:
        return False
    if isinstance(value, dict):
        return all(_toml_supported(v) for v in value.values())
    if isinstance(value, list):
        return all(_toml_supported(v) for v in value)
    return True


def _to_yaml(value):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[k] = _to_yaml(v)
        return m
    if isinstance(value, list):
        s = CommentedSeq()
        for v in value:
            s.append(_to_yaml(v))
        return s
    return value


# ---------------------------------------------------------------------------
# Rule operations
# ---------------------------------------------------------------------------

def get_parent(data, parts, create=False, cfg=None):
    node = data
    for p in parts:
        if not is_mapping(node):
            return None
        if p not in node:
            if not create:
                return None
            node[p] = cfg.convert({}) if cfg is not None else {}
            node = node[p]
            continue
        node = node[p]
    if not is_mapping(node):
        return None
    return node


def apply_replace_value(cfg, rule):
    key = rule.get("key")
    if key is None or "value" not in rule:
        return False
    if has_selector(parse_path(key)):
        value = rule["value"]
        if not cfg.value_supported(value):
            return False
        changed = False
        for parent, k in find_targets(cfg.data, key):
            if not _values_differ(cfg, parent[k], value):
                continue
            _set_child(cfg, parent, k, value)
            changed = True
        return changed
    parts = split_path(key)
    if not parts:
        return False
    parent = get_parent(cfg.data, parts[:-1])
    if parent is None or parts[-1] not in parent:
        return False
    value = rule["value"]
    if strict_equal(parent[parts[-1]], value):
        return False
    if not cfg.value_supported(value):
        return False
    if cfg.fmt == "ini":
        # INI values are strings; compare by rendered representation.
        if not is_mapping(value) and not is_mapping(parent[parts[-1]]):
            if IniDoc._format_value(value) == parent[parts[-1]]:
                return False
    parent[parts[-1]] = cfg.convert(value)
    return True


def _rename_in_place(cfg, parent, old, new):
    """Rename key `old` to `new` in mapping `parent`, keeping position."""
    value = parent[old]
    if new in parent:
        del parent[new]
    # tomlkit containers
    try:
        import tomlkit.container as tkc
        import tomlkit.items as tki
        container = None
        if isinstance(parent, tkc.Container):
            container = parent
        elif isinstance(parent, (tki.Table, tki.InlineTable)):
            container = parent.value
        if container is not None:
            item = container.item(old)
            _clear_display_names(item)
            container._replace(old, new, item)
            return
    except ImportError:
        pass
    # ruamel CommentedMap
    if hasattr(parent, "insert") and hasattr(parent, "ca"):
        keys = list(parent.keys())
        pos = keys.index(old)
        # preserve comment attached to old key
        comment = None
        try:
            comment = parent.ca.items.pop(old, None)
        except Exception:
            comment = None
        del parent[old]
        parent.insert(pos, new, value)
        if comment is not None:
            parent.ca.items[new] = comment
        return
    # plain dict
    items = list(parent.items())
    parent.clear()
    for k, v in items:
        if k == old:
            parent[new] = v
        else:
            parent[k] = v


def _clear_display_names(item):
    try:
        import tomlkit.items as tki
    except ImportError:
        return
    if isinstance(item, tki.Table):
        item.display_name = None
        for _, v in item.value.body:
            _clear_display_names(v)
    elif isinstance(item, tki.AoT):
        for t in item.body:
            _clear_display_names(t)


def _rename_with_selectors(cfg, old_comps, new_comps):
    if len(old_comps) != len(new_comps) or old_comps[:-1] != new_comps[:-1]:
        return False
    old_last, new_last = old_comps[-1], new_comps[-1]
    if isinstance(old_last, tuple) or isinstance(new_last, tuple):
        return False
    if old_last == new_last:
        return False
    if len(old_comps) == 1:
        parents = [cfg.data]
    else:
        parents = [p[k] for p, k, _ in match_pattern(cfg.data, old_comps[:-1], literal=True)]
    changed = False
    for parent in parents:
        if not is_mapping(parent) or old_last not in parent:
            continue
        _rename_in_place(cfg, parent, old_last, new_last)
        changed = True
    return changed


def apply_rename_key(cfg, rule):
    old_key = rule.get("old_key")
    new_key = rule.get("new_key")
    if old_key is None or new_key is None:
        return False
    old_comps = parse_path(old_key)
    new_comps = parse_path(new_key)
    if has_selector(old_comps) or has_selector(new_comps):
        return _rename_with_selectors(cfg, old_comps, new_comps)
    old_parts = split_path(old_key)
    new_parts = split_path(new_key)
    if not old_parts or not new_parts:
        return False
    if old_parts == new_parts:
        return False
    parent = get_parent(cfg.data, old_parts[:-1])
    if parent is None or old_parts[-1] not in parent:
        return False
    # A key cannot be moved inside itself.
    if new_parts[:len(old_parts)] == old_parts:
        return False
    if old_parts[:-1] == new_parts[:-1]:
        _rename_in_place(cfg, parent, old_parts[-1], new_parts[-1])
        return True
    # Move to a different parent.
    # Validate destination path first (without creating).
    node = cfg.data
    for p in new_parts[:-1]:
        if not is_mapping(node):
            return False
        if p not in node:
            break
        node = node[p]
    else:
        if not is_mapping(node):
            return False
    value = parent[old_parts[-1]]
    plain = to_plain(value)
    if cfg.fmt == "ini" and len(new_parts) > 2:
        return False
    del parent[old_parts[-1]]
    dest = get_parent(cfg.data, new_parts[:-1], create=True, cfg=cfg)
    if dest is None:
        return False
    if cfg.fmt == "toml":
        dest[new_parts[-1]] = plain
    elif cfg.fmt == "yaml":
        dest[new_parts[-1]] = value
    else:
        dest[new_parts[-1]] = value
    return True


def deep_merge(cfg, target, data):
    changed = False
    for k, v in data.items():
        if isinstance(v, dict) and k in target and is_mapping(target[k]):
            if deep_merge(cfg, target[k], v):
                changed = True
            continue
        if k in target and strict_equal(target[k], v):
            continue
        if not cfg.value_supported(v):
            if isinstance(v, dict) and k not in target:
                # create table with supported subset
                sub = _strip_none(v)
                if sub == {} and v != {}:
                    continue
                target[k] = cfg.convert(sub)
                changed = True
            continue
        if cfg.fmt == "ini" and k in target and not isinstance(v, dict) \
                and not is_mapping(target[k]) \
                and IniDoc._format_value(v) == target[k]:
            continue
        target[k] = cfg.convert(v)
        changed = True
    return changed


def _strip_none(value):
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value if v is not None]
    return value


def apply_merge_data(cfg, rule):
    pattern = rule.get("glob", "**/*")
    if not glob_match(pattern, cfg.rel):
        return False
    data = rule.get("data")
    if not isinstance(data, dict):
        return False
    if cfg.data is None and cfg.fmt == "yaml":
        cfg.data = cfg.convert({})
        if not data:
            cfg.data = None
            return False
        changed = deep_merge(cfg, cfg.data, data)
        if not changed:
            cfg.data = None
        return changed
    if not is_mapping(cfg.data):
        return False
    return deep_merge(cfg, cfg.data, data)


# ---------------------------------------------------------------------------
# Checkpoint 2 helpers: generic path access (mappings and arrays)
# ---------------------------------------------------------------------------

def is_sequence(obj):
    return isinstance(obj, (list, tuple))


def _child(node, part):
    """Return (found, value) for path component `part` of `node`."""
    if is_mapping(node):
        if part in node:
            return True, node[part]
        return False, None
    if is_sequence(node):
        try:
            idx = int(part)
        except (TypeError, ValueError):
            return False, None
        if str(idx) != str(part).strip() or idx < 0 or idx >= len(node):
            return False, None
        return True, node[idx]
    return False, None


def resolve_path(data, parts):
    """Return (found, parent, key, value) for the given path parts."""
    if not parts:
        return False, None, None, None
    node = data
    for p in parts[:-1]:
        found, node = _child(node, p)
        if not found:
            return False, None, None, None
    last = parts[-1]
    found, value = _child(node, last)
    if not found:
        return False, None, None, None
    key = int(last) if is_sequence(node) else last
    return True, node, key, value


def get_path_value(data, path):
    found, _, _, value = resolve_path(data, split_path(path))
    return found, value


def _set_child(cfg, parent, key, value):
    parent[key] = cfg.convert(value)


def stringify(value):
    value = to_plain(value)
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_TEMPLATE_RE = re.compile(r"\$\{([^}]*)\}")


def render_template(template, variables):
    def repl(m):
        name = m.group(1)
        if name in variables:
            return stringify(variables[name])
        return m.group(0)
    return _TEMPLATE_RE.sub(repl, template)


def substitute_value(value, variables):
    if isinstance(value, str):
        return render_template(value, variables)
    if isinstance(value, dict):
        return {k: substitute_value(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_value(v, variables) for v in value]
    return value


def _values_differ(cfg, current, value):
    if strict_equal(current, value):
        return False
    if cfg.fmt == "ini" and not is_mapping(value) and not is_mapping(current):
        if IniDoc._format_value(value) == current:
            return False
    return True


# ---------------------------------------------------------------------------
# Checkpoint 3: array selectors in paths
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def _parse_filter_value(raw):
    """Return (value, raw_string_or_None)."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1], None
    low = raw
    if low == "true":
        return True, raw
    if low == "false":
        return False, raw
    if low in ("null", "None", "~"):
        return None, raw
    if _INT_RE.match(raw):
        return int(raw), raw
    if _FLOAT_RE.match(raw) and any(ch.isdigit() for ch in raw):
        try:
            return float(raw), raw
        except ValueError:
            pass
    return raw, None


def _parse_selector(inner):
    s = inner.strip()
    if s == "*":
        return ("all",)
    if _INT_RE.match(s):
        return ("idx", int(s))
    if s.startswith("?"):
        body = s[1:].strip()
        if body.startswith("(") and body.endswith(")"):
            body = body[1:-1]
        conds = []
        for part in body.split("&"):
            part = part.strip()
            if part == "":
                continue
            neg = False
            if "!=" in part and part.index("!=") < part.index("=") + 1:
                k, v = part.split("!=", 1)
                neg = True
            elif "=" in part:
                k, v = part.split("=", 1)
                if v.startswith("="):
                    v = v[1:]
            else:
                return None
            k = k.strip()
            if not k:
                return None
            value, raw = _parse_filter_value(v)
            conds.append((k, value, raw, neg))
        if not conds:
            return None
        return ("filter", tuple(conds))
    return None


def parse_path(path):
    """Split a key path into components.

    String components are mapping keys (or numeric indices / wildcards as in
    checkpoint 2); tuple components are array selectors:
    ("all",), ("idx", n), ("filter", ((key, value, raw, negated), ...)).
    """
    if isinstance(path, list):
        return [str(p) for p in path]
    s = str(path)
    comps = []
    buf = []
    after_sel = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == ".":
            if not after_sel:
                comps.append("".join(buf))
            buf = []
            after_sel = False
            i += 1
            continue
        if c == "[":
            j = s.find("]", i + 1)
            sel = _parse_selector(s[i + 1:j]) if j != -1 else None
            if sel is None:
                buf.append(c)
                after_sel = False
                i += 1
                continue
            if buf:
                comps.append("".join(buf))
                buf = []
            comps.append(sel)
            after_sel = True
            i = j + 1
            continue
        if after_sel:
            after_sel = False
        buf.append(c)
        i += 1
    if buf or not after_sel:
        comps.append("".join(buf))
    return comps


def has_selector(comps):
    return any(isinstance(c, tuple) for c in comps)


def _filter_matches(elem, conds):
    if not is_mapping(elem):
        return False
    for key, value, raw, neg in conds:
        if key in elem:
            cur = elem[key]
            ok = strict_equal(cur, value)
            if not ok and raw is not None:
                plain = to_plain(cur)
                ok = isinstance(plain, str) and plain == raw
        else:
            ok = False
        if neg:
            ok = not ok
        if not ok:
            return False
    return True


def _selector_indices(node, sel):
    if not is_sequence(node):
        return []
    if sel[0] == "all":
        return list(range(len(node)))
    if sel[0] == "idx":
        idx = sel[1]
        if idx < 0:
            idx += len(node)
        if 0 <= idx < len(node):
            return [idx]
        return []
    if sel[0] == "filter":
        return [i for i in range(len(node)) if _filter_matches(node[i], sel[1])]
    return []


def match_pattern(data, pattern_parts, literal=False):
    """Return [(parent, key, captures)] for every path matching the pattern.

    With literal=True, string components are plain keys (no '*' / '$var'
    wildcards); array selectors always apply.
    """
    results = []

    def walk(node, idx, captures):
        if idx == len(pattern_parts):
            return
        comp = pattern_parts[idx]
        last = idx == len(pattern_parts) - 1
        if isinstance(comp, tuple):
            for i in _selector_indices(node, comp):
                if last:
                    results.append((node, i, captures))
                else:
                    walk(node[i], idx + 1, captures)
            return
        if literal:
            found, _ = _child(node, comp)
            if not found:
                return
            key = int(comp) if is_sequence(node) else comp
            if last:
                results.append((node, key, captures))
            else:
                walk(node[key], idx + 1, captures)
            return
        if is_mapping(node):
            children = [(str(k), k) for k in list(node.keys())]
        elif is_sequence(node):
            children = [(str(i), i) for i in range(len(node))]
        else:
            return
        for name, key in children:
            new_caps = captures
            if comp == "*":
                pass
            elif comp.startswith("$") and len(comp) > 1:
                var = comp[1:]
                if var.startswith("{") and var.endswith("}"):
                    var = var[1:-1]
                if var in captures:
                    if captures[var] != name:
                        continue
                else:
                    new_caps = dict(captures)
                    new_caps[var] = name
            elif comp != name:
                continue
            if last:
                results.append((node, key, new_caps))
            else:
                walk(node[key], idx + 1, new_caps)

    walk(data, 0, {})
    return results


def find_targets(data, path):
    """Return [(parent, key)] for a (possibly array-selecting) key path."""
    comps = parse_path(path)
    if not has_selector(comps):
        found, parent, key, _ = resolve_path(data, split_path(path))
        return [(parent, key)] if found else []
    return [(p, k) for p, k, _ in match_pattern(data, comps, literal=True)]


def lookup_value(data, path):
    targets = find_targets(data, path)
    if not targets:
        return False, None
    parent, key = targets[0]
    return True, parent[key]


def apply_pattern_replace(cfg, rule):
    pattern = rule.get("key_pattern")
    if pattern is None or "value" not in rule:
        return False
    parts = parse_path(pattern)
    if not parts:
        return False
    matches = match_pattern(cfg.data, parts)
    changed = False
    for parent, key, caps in matches:
        value = substitute_value(rule["value"], caps)
        if not _values_differ(cfg, parent[key], value):
            continue
        if not cfg.value_supported(value):
            continue
        _set_child(cfg, parent, key, value)
        changed = True
    return changed


def apply_template_string(cfg, rule):
    key = rule.get("key")
    template = rule.get("template")
    variables = rule.get("variables") or {}
    if key is None or not isinstance(template, str) or not isinstance(variables, dict):
        return False
    targets = find_targets(cfg.data, key)
    targets = [(p, k) for p, k in targets if isinstance(to_plain(p[k]), str)]
    if not targets:
        return False
    values = {}
    for name, path in variables.items():
        ok, val = lookup_value(cfg.data, path)
        if not ok:
            return False
        values[name] = val
    result = render_template(template, values)
    changed = False
    for parent, k in targets:
        if to_plain(parent[k]) == result:
            continue
        _set_child(cfg, parent, k, result)
        changed = True
    return changed


def _condition_holds(current, condition):
    if "current_value_equals" in condition:
        if not strict_equal(current, condition["current_value_equals"]):
            return False
    if "current_value_not_equals" in condition:
        if strict_equal(current, condition["current_value_not_equals"]):
            return False
    if "current_value_matches" in condition:
        if re.search(condition["current_value_matches"], stringify(current)) is None:
            return False
    return True


def apply_conditional_replace(cfg, rule):
    key = rule.get("key")
    condition = rule.get("condition")
    if key is None or "value" not in rule or not isinstance(condition, dict):
        return False
    value = rule["value"]
    if not cfg.value_supported(value):
        return False
    changed = False
    for parent, k in find_targets(cfg.data, key):
        current = parent[k]
        if not _condition_holds(current, condition):
            continue
        if not _values_differ(cfg, current, value):
            continue
        _set_child(cfg, parent, k, value)
        changed = True
    return changed


CONDITION_TYPES = ("current_value_equals", "current_value_not_equals",
                   "current_value_matches")


def validate_rule(name, rule):
    """Return an error message if the rule is invalid, else None."""
    if not isinstance(rule, dict):
        return None
    rtype = rule.get("type")
    if rtype == "pattern_replace":
        if "key_pattern" not in rule:
            return "missing required field 'key_pattern'"
        if not isinstance(rule["key_pattern"], (str, list)):
            return "field 'key_pattern' must be a string"
    elif rtype == "template_string":
        if "template" not in rule:
            return "missing required field 'template'"
        if not isinstance(rule["template"], str):
            return "field 'template' must be a string"
        if "variables" not in rule:
            return "missing required field 'variables'"
        variables = rule["variables"]
        if not isinstance(variables, dict):
            return "field 'variables' must be an object mapping names to key paths"
        for vname, path in variables.items():
            if not isinstance(path, str):
                return "variable '%s' must be a string key path" % vname
    elif rtype == "conditional_replace":
        if "condition" not in rule:
            return "missing required field 'condition'"
        condition = rule["condition"]
        if not isinstance(condition, dict):
            return "field 'condition' must be an object"
        if not any(c in condition for c in CONDITION_TYPES):
            return "condition must contain at least one of %s" % ", ".join(CONDITION_TYPES)
        if "current_value_matches" in condition:
            pat = condition["current_value_matches"]
            if not isinstance(pat, str):
                return "condition 'current_value_matches' must be a regex string"
            try:
                re.compile(pat)
            except re.error as exc:
                return "invalid regex in 'current_value_matches': %s" % exc
    return None


RULE_HANDLERS = {
    "replace_value": apply_replace_value,
    "rename_key": apply_rename_key,
    "merge_data": apply_merge_data,
    "pattern_replace": apply_pattern_replace,
    "template_string": apply_template_string,
    "conditional_replace": apply_conditional_replace,
}


# ---------------------------------------------------------------------------
# Checkpoint 3: config inheritance
# ---------------------------------------------------------------------------

INHERIT_FORMATS = {"json": "json", "yaml": "yaml", "yml": "yaml",
                   "toml": "toml", "ini": "ini"}


def load_inheritance_config(path):
    """Return {fmt: [path parts]} or None if invalid."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (ValueError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    result = {}
    for ext, spec in raw.items():
        if not isinstance(spec, dict):
            return None
        pattern = spec.get("pattern")
        if not isinstance(pattern, str) or pattern.strip() == "":
            return None
        parts = pattern.split(".")
        if any(p == "" for p in parts):
            return None
        fmt = INHERIT_FORMATS.get(str(ext).lower().lstrip("."))
        if fmt is None:
            continue
        # explicit "yaml" wins over "yml" alias
        if fmt in result and str(ext).lower().lstrip(".") == "yml":
            continue
        result[fmt] = parts
    return result


class InheritanceError(Exception):
    pass


def _get_directive(data, parts):
    node = data
    for p in parts:
        if not is_mapping(node) or p not in node:
            return _MISSING
        node = node[p]
    return node


def _remove_directive(data, parts):
    """Delete the directive key and any parent mappings left empty."""
    chain = [data]
    node = data
    for p in parts[:-1]:
        node = node[p]
        chain.append(node)
    del chain[-1][parts[-1]]
    for i in range(len(parts) - 2, -1, -1):
        if len(chain[i + 1]) == 0:
            del chain[i][parts[i]]
        else:
            break


def _merge_parent(cfg, target, parent):
    """Deep merge plain `parent` into `target`; target values win."""
    added_top = []
    for k, v in parent.items():
        if k in target:
            cur = target[k]
            if isinstance(v, dict) and is_mapping(cur):
                _merge_parent(cfg, cur, v)
            continue
        if not cfg.value_supported(v):
            if isinstance(v, dict):
                v = _strip_none(v)
            else:
                continue
        if cfg.fmt == "ini" and isinstance(v, dict) and target is not cfg.data:
            continue
        target[k] = cfg.convert(v)
        added_top.append(k)
    return added_top


def _fix_toml_spacing(text):
    """Ensure a blank line separates top-level keys from the first table."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            if i > 0 and lines[i - 1].strip() != "":
                lines.insert(i, "")
            break
    return "\n".join(lines)


class Inheritance:
    def __init__(self, patterns):
        self.patterns = patterns
        self.cache = {}

    def _pattern_for(self, fmt):
        return self.patterns.get(fmt)

    def resolve(self, cfg):
        """Resolve inheritance for target config `cfg` in place.

        Returns True when the config was modified."""
        parts = self._pattern_for(cfg.fmt)
        if parts is None or not is_mapping(cfg.data):
            return False
        start = os.path.realpath(cfg.path)
        return self._resolve_into(cfg, parts, [start], [cfg.rel])

    def _resolve_into(self, cfg, parts, chain, names):
        ref = _get_directive(cfg.data, parts)
        if ref is _MISSING:
            return False
        ref_plain = to_plain(ref)
        if not isinstance(ref_plain, str) or ref_plain == "":
            return False
        base_dir = os.path.dirname(os.path.abspath(cfg.path))
        parent_path = os.path.normpath(os.path.join(base_dir, ref_plain))
        real = os.path.realpath(parent_path)
        if real in chain:
            raise InheritanceError("circular dependency detected: %s"
                                   % " -> ".join(names + [ref_plain]))
        if not os.path.isfile(parent_path):
            return False  # leave directive in place
        ext = os.path.splitext(parent_path)[1].lower()
        pfmt = EXTENSIONS.get(ext, cfg.fmt)
        try:
            pcfg = ConfigFile(parent_path, ref_plain, pfmt, ref_plain)
        except (ParseError, OSError):
            raise InheritanceError("failed to parse parent config: %s" % ref_plain)
        if pcfg.data is None and pfmt == "yaml":
            pcfg.data = {}
        if not is_mapping(pcfg.data):
            raise InheritanceError("failed to parse parent config: %s" % ref_plain)
        pparts = self._pattern_for(pfmt)
        if pparts is not None:
            self._resolve_into(pcfg, pparts, chain + [real], names + [ref_plain])
            # an unresolved directive in the parent is not propagated
            if _get_directive(pcfg.data, pparts) is not _MISSING:
                _remove_directive(pcfg.data, pparts)
        parent_data = to_plain(pcfg.data)
        _remove_directive(cfg.data, parts)
        added = _merge_parent(cfg, cfg.data, parent_data)
        if cfg.fmt == "toml" and any(not isinstance(parent_data[k], (dict, list))
                                     for k in added):
            cfg.toml_fix_spacing = True
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def error(msg):
    sys.stderr.write("Error: " + msg + "\n")
    sys.exit(1)


def find_config_files(target):
    found = []
    for root, dirs, files in os.walk(target):
        dirs.sort()
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in EXTENSIONS:
                continue
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, target).replace(os.sep, "/")
            found.append((rel, full, EXTENSIONS[ext]))
    found.sort(key=lambda t: t[0])
    return found


def parse_args(argv):
    positional = []
    inheritance = None
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--inheritance":
            if i + 1 >= len(args):
                return None
            inheritance = args[i + 1]
            i += 2
            continue
        if a.startswith("--inheritance="):
            inheritance = a[len("--inheritance="):]
            i += 1
            continue
        positional.append(a)
        i += 1
    if len(positional) != 2:
        return None
    return positional[0], positional[1], inheritance


def main(argv):
    parsed = parse_args(argv)
    if parsed is None:
        sys.stderr.write("Usage: python migrate.py rules.json /path/to/configs "
                         "[--inheritance inheritance.json]\n")
        sys.exit(1)
    rules_path, target, inheritance_path = parsed

    if not os.path.isfile(rules_path):
        error("rules file not found: %s" % rules_path)
    try:
        with open(rules_path, "r", encoding="utf-8") as fh:
            rules = json.load(fh)
    except (ValueError, UnicodeDecodeError, OSError):
        error("invalid rules file: %s" % rules_path)
    if not isinstance(rules, dict):
        error("invalid rules file: %s" % rules_path)

    for name in sorted(rules.keys()):
        msg = validate_rule(name, rules[name])
        if msg is not None:
            error("invalid rule '%s': %s" % (name, msg))

    inheritance = None
    if inheritance_path is not None:
        patterns = load_inheritance_config(inheritance_path)
        if patterns is None:
            error("invalid inheritance config: %s" % inheritance_path)
        inheritance = Inheritance(patterns)

    if not os.path.isdir(target):
        error("target directory not found: %s" % target)

    configs = []
    for rel, full, fmt in find_config_files(target):
        label = os.path.join(target, rel)
        try:
            configs.append(ConfigFile(full, rel, fmt, label))
        except (ParseError, OSError):
            error("failed to parse %s" % label)

    inherited = set()
    if inheritance is not None:
        for idx, cfg in enumerate(configs):
            try:
                if inheritance.resolve(cfg):
                    inherited.add(idx)
            except InheritanceError as exc:
                error(str(exc))

    rule_names = sorted(rules.keys())
    for idx, cfg in enumerate(configs):
        applied = []
        for name in rule_names:
            rule = rules[name]
            if not isinstance(rule, dict):
                continue
            handler = RULE_HANDLERS.get(rule.get("type"))
            if handler is None:
                continue
            try:
                ok = handler(cfg, rule)
            except Exception:
                ok = False
            if ok:
                applied.append(name)
        if applied or idx in inherited:
            cfg.save()
            sys.stdout.write(json.dumps({
                "event": "file_updated",
                "file": cfg.rel,
                "rules_applied": applied,
            }) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv)
