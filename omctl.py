#!/usr/bin/env python3
"""omctl - remote admin CLI for hmock.

  omctl push   [-d DIR] [-u URL] [-k SET_KEY]
  omctl delete [-u URL] -k SET_KEY
"""
import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - fall back to a local virtualenv
    _here = os.path.dirname(os.path.abspath(__file__))
    for _cand in (os.path.join(_here, ".venv", "bin", "python"),
                  os.path.join(_here, "venv", "bin", "python")):
        if os.path.exists(_cand) and os.path.realpath(_cand) != os.path.realpath(sys.executable) \
                and not os.environ.get("OMCTL_REEXEC"):
            os.environ["OMCTL_REEXEC"] = "1"
            os.execv(_cand, [_cand, os.path.abspath(__file__)] + sys.argv[1:])
    yaml = None

DEFAULT_URL = "http://localhost:9998"
TIMEOUT = 30


def walk_yaml(root):
    """Recursive, lexically ordered list of YAML files under root."""
    out = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if os.path.isdir(full):
            out.extend(walk_yaml(full))
        elif name.lower().endswith((".yaml", ".yml")) and os.path.isfile(full):
            out.append(full)
    return out


def load_payload(directory):
    if not os.path.isdir(directory):
        raise SystemExit("omctl: directory not found: %s" % directory)
    files = walk_yaml(directory)
    if yaml is not None:
        items = []
        for path in files:
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    docs = list(yaml.safe_load_all(fh))
                except yaml.YAMLError as e:
                    raise SystemExit("omctl: failed to parse %s: %s" % (path, e))
            for doc in docs:
                if doc is None:
                    continue
                items.extend(doc if isinstance(doc, list) else [doc])
        return yaml.safe_dump(items, sort_keys=False, allow_unicode=True).encode("utf-8")
    # no YAML library: concatenate the files as a multi-document stream
    parts = []
    for path in files:
        with open(path, "rb") as fh:
            parts.append(fh.read().strip(b"\n"))
    return b"\n---\n".join(p for p in parts if p.strip()) + b"\n"


def request(method, url, body=None, content_type=None):
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit("omctl: request to %s failed: %s" % (url, getattr(e, "reason", e)))


def _base(url):
    return url.rstrip("/")


def cmd_push(args):
    payload = load_payload(args.directory)
    if args.set_key:
        url = "%s/api/v1/template_sets/%s" % (_base(args.url),
                                             urllib.parse.quote(args.set_key, safe=""))
    else:
        url = "%s/api/v1/templates" % _base(args.url)
    status, body = request("POST", url, payload, "application/yaml")
    text = body.decode("utf-8", "replace")
    if 200 <= status < 300:
        print("pushed %s to %s (status %d)" % (args.directory, url, status))
        return 0
    sys.stderr.write("omctl: push failed: status %d: %s\n" % (status, text.strip()))
    return 1


def cmd_delete(args):
    url = "%s/api/v1/template_sets/%s" % (_base(args.url),
                                         urllib.parse.quote(args.set_key, safe=""))
    status, body = request("DELETE", url)
    if status == 204:
        print("deleted template set %s" % args.set_key)
        return 0
    sys.stderr.write("omctl: delete failed: expected status 204, got %d: %s\n"
                     % (status, body.decode("utf-8", "replace").strip()))
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="omctl", description="hmock remote admin CLI")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    push = sub.add_parser("push", help="push local YAML templates to the admin API")
    push.add_argument("-d", "--directory", default="./demo_templates",
                      help="local directory of YAML templates (default ./demo_templates)")
    push.add_argument("-u", "--url", default=DEFAULT_URL,
                      help="admin API base URL (default %s)" % DEFAULT_URL)
    push.add_argument("-k", "--set-key", dest="set_key", default=None,
                      help="upload as a named template set")
    push.set_defaults(func=cmd_push)

    delete = sub.add_parser("delete", help="delete a template set")
    delete.add_argument("-u", "--url", default=DEFAULT_URL,
                        help="admin API base URL (default %s)" % DEFAULT_URL)
    delete.add_argument("-k", "--set-key", dest="set_key", required=True,
                        help="template set to delete")
    delete.set_defaults(func=cmd_delete)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
