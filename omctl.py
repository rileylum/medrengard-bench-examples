#!/usr/bin/env python3
"""omctl: remote admin CLI for hmock.

    omctl push   [-d DIR] [-u URL] [-k SET_KEY]
    omctl delete [-u URL] -k SET_KEY
"""

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = 'http://localhost:9998'
DEFAULT_DIR = './demo_templates'


def _yaml_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(('.yaml', '.yml')):
                out.append(os.path.join(dirpath, name))
    return out


def build_payload(directory):
    if not os.path.isdir(directory):
        raise SystemExit('omctl: directory %s does not exist' % directory)
    files = _yaml_files(directory)
    texts = []
    for p in files:
        with open(p, 'rb') as fh:
            texts.append(fh.read().decode('utf-8-sig'))
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is None:
        parts = [t for t in texts if t.strip()]
        return ('\n---\n'.join(parts) if parts else '[]\n').encode('utf-8')
    mocks = []
    for p, t in zip(files, texts):
        try:
            docs = list(yaml.safe_load_all(t))
        except Exception as e:
            raise SystemExit('omctl: failed to parse %s: %s' % (p, e))
        for d in docs:
            if d is None:
                continue
            if isinstance(d, list):
                mocks.extend(d)
            else:
                mocks.append(d)
    return yaml.safe_dump(mocks, allow_unicode=True, sort_keys=False,
                          default_flow_style=False).encode('utf-8')


def _request(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit('omctl: request to %s failed: %s' % (url, getattr(e, 'reason', e)))


def _base(url):
    return url.rstrip('/')


def cmd_push(args):
    payload = build_payload(args.directory)
    if args.set_key:
        url = '%s/api/v1/template_sets/%s' % (_base(args.url),
                                              urllib.parse.quote(args.set_key, safe=''))
    else:
        url = '%s/api/v1/templates' % _base(args.url)
    status, body = _request('POST', url, payload, {'Content-Type': 'application/yaml'})
    if not 200 <= status < 300:
        sys.stderr.write('omctl: push failed: %d %s\n'
                         % (status, body.decode('utf-8', 'replace').strip()))
        return 1
    print('pushed templates from %s to %s (%d)' % (args.directory, url, status))
    return 0


def cmd_delete(args):
    url = '%s/api/v1/template_sets/%s' % (_base(args.url),
                                          urllib.parse.quote(args.set_key, safe=''))
    status, body = _request('DELETE', url)
    if status != 204:
        sys.stderr.write('omctl: delete failed: expected 204, got %d %s\n'
                         % (status, body.decode('utf-8', 'replace').strip()))
        return 1
    print('deleted template set %s' % args.set_key)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='omctl', description='hmock admin CLI')
    sub = ap.add_subparsers(dest='command')
    sub.required = True
    p = sub.add_parser('push', help='push local templates to a remote hmock')
    p.add_argument('-d', '--directory', default=DEFAULT_DIR)
    p.add_argument('-u', '--url', default=DEFAULT_URL)
    p.add_argument('-k', '--set-key', dest='set_key', default=None)
    p.set_defaults(func=cmd_push)
    d = sub.add_parser('delete', help='delete a template set on a remote hmock')
    d.add_argument('-u', '--url', default=DEFAULT_URL)
    d.add_argument('-k', '--set-key', dest='set_key', required=True)
    d.set_defaults(func=cmd_delete)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
