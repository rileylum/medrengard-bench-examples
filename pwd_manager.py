#!/usr/bin/env python3
"""Interactive command-line password manager with an encrypted vault.

Uses only the Python standard library:
  * key derivation: PBKDF2-HMAC-SHA256 with a per-vault random salt
  * encryption: HMAC-SHA256 in counter mode (keystream) with a random nonce
  * authentication: HMAC-SHA256 over the ciphertext (encrypt-then-MAC)
The whole vault (all secrets) is encrypted as one payload.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time

VAULT_DIR = os.path.join(os.path.expanduser("~"), ".vault")
CONFIG_PATH = os.path.join(VAULT_DIR, "config.json")
VAULT_PATH = os.path.join(VAULT_DIR, "vault.dat")

CONFIG_VERSION = 1
KDF_ITERATIONS = 200_000
MIN_KEY_LEN = 8
MAX_NOTE_LINES = 15
MAX_ATTEMPTS = 3
PAYLOAD_MAGIC = b"PWDV1"
DEFAULT_CLIPBOARD_TTL = 15
DEFAULT_HIDE_DELAY = 5
DEFAULT_AUTO_LOCK = 900
SUGGESTION_LENGTH = 20

BANNER = r"""
==============================================
         Vault Password Manager
==============================================
"""
COMPACT_BANNER = "=== Vault Password Manager ==="


class QuitProgram(Exception):
    pass


class DecryptionError(Exception):
    pass


class AutoLocked(Exception):
    """Raised when the vault was idle for longer than the auto-lock timeout."""


class ManualLock(Exception):
    """Raised when the user explicitly locks the vault from the main menu."""


# ---------------------------------------------------------------- crypto ---

def derive_keys(master_key, salt, iterations=KDF_ITERATIONS):
    material = hashlib.pbkdf2_hmac("sha256", master_key.encode("utf-8"), salt,
                                   iterations, dklen=64)
    return material[:32], material[32:]


def _keystream_xor(key, nonce, data):
    out = bytearray(len(data))
    block = 32
    for counter in range((len(data) + block - 1) // block):
        ks = hmac.new(key, nonce + counter.to_bytes(8, "big"),
                      hashlib.sha256).digest()
        start = counter * block
        chunk = data[start:start + block]
        for i, b in enumerate(chunk):
            out[start + i] = b ^ ks[i]
    return bytes(out)


def encrypt(keys, plaintext):
    enc_key, mac_key = keys
    nonce = secrets.token_bytes(16)
    ct = _keystream_xor(enc_key, nonce, plaintext)
    body = PAYLOAD_MAGIC + nonce + ct
    tag = hmac.new(mac_key, body, hashlib.sha256).digest()
    return body + tag


def decrypt(keys, blob):
    enc_key, mac_key = keys
    if len(blob) < len(PAYLOAD_MAGIC) + 16 + 32 or not blob.startswith(PAYLOAD_MAGIC):
        raise DecryptionError("corrupt vault payload")
    body, tag = blob[:-32], blob[-32:]
    expected = hmac.new(mac_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise DecryptionError("wrong master key or corrupt vault")
    nonce = body[len(PAYLOAD_MAGIC):len(PAYLOAD_MAGIC) + 16]
    ct = body[len(PAYLOAD_MAGIC) + 16:]
    return _keystream_xor(enc_key, nonce, ct)


# --------------------------------------------------------------- storage ---

def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    salt = base64.b64decode(cfg["salt"])
    iterations = int(cfg.get("iterations", KDF_ITERATIONS))
    return cfg, salt, iterations


def write_config(salt):
    cfg = {
        "version": CONFIG_VERSION,
        "format": "pwd_manager-vault",
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
    }
    if SETTINGS:
        cfg["settings"] = dict(SETTINGS)
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2).encode("utf-8"))
    return cfg


class Vault:
    def __init__(self, keys, data):
        self.keys = keys
        self.data = data

    @property
    def items(self):
        return self.data["items"]

    def save(self):
        raw = json.dumps(self.data, sort_keys=True).encode("utf-8")
        _atomic_write(VAULT_PATH, encrypt(self.keys, raw))

    @property
    def categories(self):
        return self.data.setdefault("categories", [])

    def add(self, name, url, login, password, notes, category_id=None):
        item_id = self.data["next_id"]
        self.data["next_id"] = item_id + 1
        self.items.append({
            "id": item_id, "name": name, "url": url, "login": login,
            "password": password, "notes": notes, "category": category_id,
        })
        self.save()
        return item_id

    def delete(self, item_id):
        self.data["items"] = [it for it in self.items if it["id"] != item_id]
        self.save()

    # categories
    def sorted_categories(self):
        return sorted(self.categories, key=lambda c: c["id"])

    def get_category(self, cat_id):
        for c in self.categories:
            if c["id"] == cat_id:
                return c
        return None

    def category_name(self, cat_id):
        if cat_id is None:
            return ""
        c = self.get_category(cat_id)
        return c["name"] if c else ""

    def add_category(self, name):
        cat_id = self.data.setdefault("next_category_id", 1)
        self.data["next_category_id"] = cat_id + 1
        self.categories.append({"id": cat_id, "name": name})
        self.save()
        return cat_id

    def rename_category(self, cat_id, name):
        self.get_category(cat_id)["name"] = name
        self.save()

    def category_in_use(self, cat_id):
        return any(it.get("category") == cat_id for it in self.items)

    def delete_category(self, cat_id):
        self.data["categories"] = [c for c in self.categories
                                   if c["id"] != cat_id]
        self.save()

    def sorted_items(self):
        return sorted(self.items, key=lambda it: it["id"])

    def get(self, item_id):
        for it in self.items:
            if it["id"] == item_id:
                return it
        return None

    def keyword_search(self, query):
        return [it for it in self.sorted_items()
                if query in it.get("name", "") or query in it.get("url", "")
                or query in it.get("login", "")]


def empty_vault_data():
    return {"version": CONFIG_VERSION, "next_id": 1, "items": [],
            "next_category_id": 1, "categories": []}


# -------------------------------------------------------------------- io ---

_stdin_buf = b""
_lock_armed = False


def _remaining_lock_time(started):
    timeout = auto_lock_seconds()
    if not _lock_armed or timeout <= 0:
        return None
    return timeout - (time.monotonic() - started)


def _check_idle(started):
    """Raise AutoLocked if input arrived after the auto-lock timeout."""
    remaining = _remaining_lock_time(started)
    if remaining is not None and remaining < 0:
        raise AutoLocked()


def _raw_readline(started):
    """Read one line from fd 0 (own buffer so select() stays accurate).

    Returns None on EOF with no pending data.
    """
    global _stdin_buf
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        fd = None
    if fd is None:
        line = sys.stdin.readline()
        return line if line != "" else None
    import select
    while b"\n" not in _stdin_buf:
        remaining = _remaining_lock_time(started)
        if remaining is not None and os.name != "nt":
            if remaining <= 0:
                raise AutoLocked()
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise AutoLocked()
        chunk = os.read(fd, 4096)
        if not chunk:
            if _stdin_buf:
                data, _stdin_buf = _stdin_buf, b""
                return data.decode("utf-8", "replace")
            return None
        _stdin_buf += chunk
    idx = _stdin_buf.index(b"\n") + 1
    data, _stdin_buf = _stdin_buf[:idx], _stdin_buf[idx:]
    return data.decode("utf-8", "replace")


def read_line(prompt=""):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    started = time.monotonic()
    line = _raw_readline(started)
    if line is None:
        sys.stdout.write("\n")
        raise QuitProgram()
    _check_idle(started)
    return line.rstrip("\r\n")


def read_secret(prompt):
    if sys.stdin.isatty():
        import getpass
        started = time.monotonic()
        try:
            value = getpass.getpass(prompt)
        except EOFError:
            raise QuitProgram()
        _check_idle(started)
        return value
    return read_line(prompt)


# -------------------------------------------------------------- settings ---

def _env_seconds(names, default):
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value >= 0:
            return value
    return default


# Persisted runtime settings (stored in the metadata/config file).
SETTINGS = {}


def _setting(key, env_names, default):
    if key in SETTINGS:
        return SETTINGS[key]
    return _env_seconds(env_names, default)


def clipboard_ttl():
    return _setting("clipboard_ttl", ("PWD_MANAGER_CLIPBOARD_TTL",
                                      "VAULT_CLIPBOARD_TTL", "CLIPBOARD_TTL"),
                    DEFAULT_CLIPBOARD_TTL)


def hide_delay():
    return _setting("hide_delay", ("PWD_MANAGER_HIDE_DELAY", "VAULT_HIDE_DELAY",
                                   "HIDE_DELAY"), DEFAULT_HIDE_DELAY)


def auto_lock_seconds():
    return _setting("auto_lock", ("PWD_MANAGER_AUTO_LOCK", "VAULT_AUTO_LOCK",
                                  "AUTO_LOCK"), DEFAULT_AUTO_LOCK)


def load_settings():
    """Load persisted settings from the active config file, if any."""
    SETTINGS.clear()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return
    stored = cfg.get("settings") if isinstance(cfg, dict) else None
    if not isinstance(stored, dict):
        return
    for key in ("clipboard_ttl", "hide_delay", "auto_lock"):
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            SETTINGS[key] = value


def save_settings():
    """Persist settings into the active config file (if it exists yet).

    When no vault exists yet the settings are written by ``write_config``
    during first-run setup.
    """
    if not os.path.isfile(CONFIG_PATH):
        return True
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            raise ValueError("config is not a JSON object")
        cfg["settings"] = dict(SETTINGS)
        _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2).encode("utf-8"))
        return True
    except (OSError, ValueError) as exc:
        print("Warning: could not save settings to %s (%s)."
              % (CONFIG_PATH, exc))
        return False


def _fmt_seconds(value):
    if float(value).is_integer():
        return str(int(value))
    return ("%.2f" % value).rstrip("0").rstrip(".")


# ------------------------------------------------------------- clipboard ---

class ClipboardUnavailable(Exception):
    pass


def _clipboard_backends():
    """Return a list of (copy_cmd, paste_cmd, clear_cmd) tuples, best first."""
    candidates = []
    wayland = [("wl-copy",), ("wl-paste", "--no-newline"), ("wl-copy", "--clear")]
    xclip = [("xclip", "-selection", "clipboard", "-in"),
             ("xclip", "-selection", "clipboard", "-out"), None]
    xsel = [("xsel", "--clipboard", "--input"),
            ("xsel", "--clipboard", "--output"),
            ("xsel", "--clipboard", "--clear")]
    mac = [("pbcopy",), ("pbpaste",), None]
    win = [("clip.exe",),
           ("powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"),
           None]
    termux = [("termux-clipboard-set",), ("termux-clipboard-get",), None]
    if sys.platform == "darwin":
        candidates.append(mac)
    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append(wayland)
    if os.environ.get("DISPLAY"):
        candidates.extend([xclip, xsel])
    for c in (mac, wayland, xclip, xsel, win, termux):
        if c not in candidates:
            candidates.append(c)
    return [c for c in candidates if shutil.which(c[0][0])]


class Clipboard:
    def __init__(self):
        self.backends = _clipboard_backends()
        self.active = None

    def available(self):
        return bool(self.backends)

    def copy(self, text):
        errors = []
        for backend in self.backends:
            try:
                subprocess.run(list(backend[0]), input=text.encode("utf-8"),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True,
                               timeout=5)
                self.active = backend
                return
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
        raise ClipboardUnavailable(
            "; ".join(errors) if errors else "no clipboard tool found "
            "(install wl-clipboard, xclip or xsel)")

    def paste(self):
        backend = self.active
        if backend is None or backend[1] is None:
            return None
        try:
            res = subprocess.run(list(backend[1]), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            # e.g. wl-paste exits non-zero when the clipboard is empty
            return ""
        out = res.stdout.decode("utf-8", "replace")
        if backend[0][0] == "powershell.exe":
            out = out.rstrip("\r\n")
        return out

    def clear(self):
        backend = self.active
        if backend is None:
            return False
        try:
            if backend[2] is not None:
                subprocess.run(list(backend[2]), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True,
                               timeout=5)
            else:
                subprocess.run(list(backend[0]), input=b"",
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True,
                               timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def clear_if_unchanged(self, text):
        """Clear the clipboard only if it still holds ``text``.

        Returns "cleared", "changed" or "failed".
        """
        current = self.paste()
        if current is not None and current != text:
            return "changed"
        return "cleared" if self.clear() else "failed"


def _report_clear(result, label):
    if result == "cleared":
        print("Clipboard cleared.")
    elif result == "changed":
        print("Clipboard was changed by another application; left untouched.")
    else:
        print("Error: could not clear the clipboard.")


def clipboard_countdown(clip, text, label, ttl=None):
    ttl = clipboard_ttl() if ttl is None else ttl
    tty = sys.stdout.isatty()
    try:
        remaining = ttl
        while remaining > 0:
            msg = ("Clearing clipboard in %s second(s)... "
                   "(Ctrl-C to clear now)" % _fmt_seconds(remaining))
            if tty:
                sys.stdout.write("\r\033[K" + msg)
            else:
                sys.stdout.write(msg + "\n")
            sys.stdout.flush()
            step = min(1, remaining)
            time.sleep(step)
            remaining -= step
        if tty:
            sys.stdout.write("\r\033[K")
        print("Clipboard time-to-live expired.")
    except KeyboardInterrupt:
        if tty:
            sys.stdout.write("\r\033[K")
        print("Interrupted; clearing clipboard now.")
    _report_clear(clip.clear_if_unchanged(text), label)


COPY_FIELDS = {"l": ("login", "Login"), "p": ("password", "Password"),
               "u": ("url", "URL")}


def copy_field(item, key):
    field, label = COPY_FIELDS[key]
    value = item.get(field) or ""
    if value == "":
        print("Nothing to copy: this secret has no %s." % (label if label == "URL" else label.lower()))
        return
    clip = Clipboard()
    if not clip.available():
        print("Error: no clipboard mechanism is available; "
              "%s was not copied." % label.lower())
        return
    try:
        clip.copy(value)
    except ClipboardUnavailable as exc:
        print("Error: no working clipboard mechanism is available; "
              "%s was not copied (%s)." % (label.lower() if label != "URL"
                                         else label, exc))
        return
    print("%s copied to clipboard." % label)
    clipboard_countdown(clip, value, label)


# ------------------------------------------------------- password reveal ---

def mask_for(password):
    """Asterisk mask whose length is unrelated to the password's length."""
    length = 8 + secrets.randbelow(13)
    if length == len(password):
        length += 1 + secrets.randbelow(4)
    return "*" * length


def reveal_password(password, delay=None):
    delay = hide_delay() if delay is None else delay
    label = "Password: "
    sys.stdout.write(label + password)
    sys.stdout.flush()
    try:
        time.sleep(max(0, delay))
    except KeyboardInterrupt:
        pass
    # Overwrite the cleartext in place (clear the whole line, then mask).
    sys.stdout.write("\r\033[K" + label + mask_for(password) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------- password suggestion ---

SUGGESTION_ALPHABET = string.ascii_letters + string.digits + "!#$%&*+-=?@^_"
_last_suggestion = None


def generate_password(length=SUGGESTION_LENGTH):
    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits,
              "!#$%&*+-=?@^_")
    while True:
        chars = [secrets.choice(g) for g in groups]
        chars += [secrets.choice(SUGGESTION_ALPHABET)
                  for _ in range(length - len(chars))]
        secrets.SystemRandom().shuffle(chars)
        return "".join(chars)


def suggest_password():
    global _last_suggestion
    while True:
        pw = generate_password()
        if pw != _last_suggestion:
            _last_suggestion = pw
            return pw


def read_new_password(prompt):
    print("Suggested password (generated password): %s" % suggest_password())
    return read_secret(prompt)


# ------------------------------------------------------------ completion ---

_readline = None
_readline_checked = False


def _get_readline():
    global _readline, _readline_checked
    if not _readline_checked:
        _readline_checked = True
        try:
            import readline as rl
        except ImportError:
            rl = None
        if rl is not None:
            try:
                rl.set_completer_delims("")
                doc = rl.__doc__ or ""
                if "libedit" in doc:
                    rl.parse_and_bind("bind ^I rl_complete")
                else:
                    rl.parse_and_bind("tab: complete")
            except Exception:
                rl = None
        _readline = rl
    return _readline


def completion_matches(text, candidates):
    seen = set()
    uniq = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    exact = [c for c in uniq if c.startswith(text)]
    if exact:
        return sorted(exact)
    low = text.lower()
    ci = [c for c in uniq if c.lower().startswith(low)]
    if ci:
        return sorted(ci)
    return sorted(c for c in uniq if low and low in c.lower())


def make_completer(candidates_fn):
    cache = {}

    def complete(text, state):
        rl = _readline
        line = text
        if rl is not None:
            try:
                line = rl.get_line_buffer()[:rl.get_endidx()]
            except Exception:
                line = text
        if state == 0 or cache.get("text") != line:
            cache["text"] = line
            cache["matches"] = completion_matches(line, candidates_fn())
        matches = cache["matches"]
        if state >= len(matches):
            return None
        match = matches[state]
        # Return the completion relative to the word readline gave us.
        prefix_len = len(line) - len(text)
        if prefix_len > 0:
            match = match[prefix_len:] if match.startswith(line[:prefix_len]) \
                else match
        return match

    return complete


def read_line_completing(prompt, candidates_fn):
    """Read a line, offering tab-completion from ``candidates_fn()``."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return read_line(prompt)
    rl = _get_readline()
    if rl is None:
        return read_line(prompt)
    old = rl.get_completer()
    rl.set_completer(make_completer(candidates_fn))
    started = time.monotonic()
    try:
        try:
            line = input(prompt)
        except EOFError:
            sys.stdout.write("\n")
            raise QuitProgram()
    finally:
        rl.set_completer(old)
        try:
            n = rl.get_current_history_length()
            if n:
                rl.remove_history_item(n - 1)
        except Exception:
            pass
    _check_idle(started)
    return line.rstrip("\r\n")


def clear_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
    else:
        sys.stdout.write("\n")
    sys.stdout.flush()


def _grid(headers, rows):
    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(headers)]

    def fmt(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [sep, fmt(headers), sep]
    lines.extend(fmt(r) for r in rows)
    lines.append(sep)
    return "\n".join(lines)


def render_table(items, vault=None):
    if not items:
        return "Empty!"
    headers = ["Item", "Category", "Name", "URL", "Login"]
    rows = [[str(it["id"]),
             vault.category_name(it.get("category")) if vault else "",
             it.get("name", ""), it.get("url", ""), it.get("login", "")]
            for it in items]
    return _grid(headers, rows)


def render_categories(vault):
    cats = vault.sorted_categories()
    if not cats:
        return "Empty!"
    return _grid(["ID", "Category"], [[str(c["id"]), c["name"]] for c in cats])


def ask_yes_no(prompt):
    """Ask a yes/no question; default is No. Re-prompts on invalid input."""
    while True:
        ans = read_line("%s [y / n]: " % prompt).strip().lower()
        if ans == "y":
            return True
        if ans in ("n", ""):
            return False
        print("Please answer 'y' or 'n'.")


def read_notes():
    print("Notes (up to %d lines, submit a blank line to finish):"
          % MAX_NOTE_LINES)
    lines = []
    while True:
        line = read_line("")
        if line.strip() == "":
            break
        if len(lines) < MAX_NOTE_LINES:
            lines.append(line)
    return "\n".join(lines)


def choose_category_id(vault, prompt):
    """Show the category table then ask for an ID. Returns ID or None."""
    print(render_categories(vault))
    raw = read_line(prompt).strip()
    if raw.isdigit() and vault.get_category(int(raw)) is not None:
        return int(raw)
    return None


# ------------------------------------------------------------------ flows ---

def run_setup():
    while True:
        print(BANNER)
        print("Hello and welcome! No vault was found, so let's create one.")
        print("Choose a master key of at least %d characters." % MIN_KEY_LEN)
        key = read_secret("Choose a master key: ")
        confirm = read_secret("Confirm master key: ")
        if len(key) < MIN_KEY_LEN:
            print("Error: master key must be at least %d characters long. "
                  "Please start again." % MIN_KEY_LEN)
            continue
        if key != confirm:
            print("Error: master keys do not match. Please start again.")
            continue
        break
    cfg = None
    iterations = KDF_ITERATIONS
    if os.path.isfile(CONFIG_PATH):
        # The metadata file may be shared with another vault payload (e.g.
        # only -v was given); reuse it rather than overwriting its salt.
        try:
            cfg, salt, iterations = load_config()
            if "settings" not in cfg and SETTINGS:
                save_settings()
        except (OSError, ValueError, KeyError, TypeError):
            cfg = None
    if cfg is None:
        salt = secrets.token_bytes(16)
        cfg = write_config(salt)
        iterations = KDF_ITERATIONS
    vault = Vault(derive_keys(key, salt, iterations), empty_vault_data())
    vault.save()
    print("Vault created successfully.")
    print("Your config salt is: %s" % cfg["salt"])
    print("Please write down this salt and keep it somewhere safe.")
    print()


def unlock():
    print(BANNER)
    try:
        _cfg, salt, iterations = load_config()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if not os.path.isfile(CONFIG_PATH):
            config_missing_error()
        print("Error: the config file %s is unreadable (%s). The config file "
              "is required to unlock the vault. Please restore it from a "
              "backup and try again." % (CONFIG_PATH, exc))
        sys.exit(2)
    with open(VAULT_PATH, "rb") as fh:
        blob = fh.read()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        key = read_secret("Enter master key: ")
        try:
            keys = derive_keys(key, salt, iterations)
            data = json.loads(decrypt(keys, blob).decode("utf-8"))
            return Vault(keys, data)
        except (DecryptionError, ValueError, UnicodeDecodeError):
            remaining = MAX_ATTEMPTS - attempt
            print("Error: incorrect master key. %d attempt(s) remaining."
                  % remaining)
    print("Too many failed attempts. The vault is locked; "
          "restart and enter the correct master key.")
    sys.exit(1)


def add_secret(vault):
    print("Add a new secret")
    category_id = None
    if vault.categories:
        category_id = choose_category_id(
            vault, "Category ID (leave empty for no category): ")
    name = read_line("Name: ")
    url = read_line("URL: ")
    login = read_line_completing(
        "Login: ", lambda: sorted({it.get("login", "") for it in vault.items
                                   if it.get("login")}))
    password = read_new_password("Password: ")
    notes = read_notes()
    item_id = vault.add(name, url, login, password, notes, category_id)
    print("Secret saved with ID %d." % item_id)
    return "menu"


def show_item(vault, item):
    print(render_table([item], vault))
    if item.get("notes"):
        print("Notes:")
        print(item["notes"])


def edit_category(vault, item):
    current = vault.category_name(item.get("category"))
    print("Current category: %s" % (current if current else "Empty!"))
    if not vault.categories:
        print("Empty!")
        return False
    new_id = choose_category_id(vault, "New category ID: ")
    if new_id is None:
        print("Unknown category ID; the secret was not changed.")
        return False
    item["category"] = new_id
    vault.save()
    print("Category updated.")
    return True


def edit_field(vault, item, field, label):
    print("Current %s: %s" % (label, item.get(field, "")))
    if field == "password":
        value = read_new_password("New password: ")
    elif field == "notes":
        value = read_notes()
    else:
        value = read_line("New %s: " % label)
    item[field] = value
    vault.save()
    print("%s updated." % label.capitalize())
    return True


EDIT_FIELDS = {
    "n": ("name", "name"),
    "u": ("url", "URL"),
    "l": ("login", "login"),
    "p": ("password", "password"),
    "o": ("notes", "notes"),
}


def edit_menu(vault, item):
    while True:
        choice = read_line("Edit: [c] category  [n] name  [u] URL  [l] login  "
                           "[p] password  [o] notes  [b] back: ").strip().lower()
        if choice == "b":
            return
        if choice == "c":
            if edit_category(vault, item):
                return
            continue
        if choice in EDIT_FIELDS:
            field, label = EDIT_FIELDS[choice]
            edit_field(vault, item, field, label)
            return
        print("Please choose one of c, n, u, l, p, o or b.")


def item_actions(item):
    copy = [label for key, label in (("login", "(l)ogin"),
                                     ("password", "(p)assword"),
                                     ("url", "(u)rl"))
            if item.get(key)]
    lines = []
    if copy:
        lines.append("Copy: " + "  ".join(copy))
    if item.get("password"):
        lines.append("(o) show password")
    return lines


def item_view(vault, item):
    clear_screen()
    show_item(vault, item)
    while True:
        for line in item_actions(item):
            print(line)
        choice = read_line("[s] new search  [b] back to menu  [e] edit  "
                           "[d] delete: ").strip().lower()
        if choice in COPY_FIELDS:
            copy_field(item, choice)
            continue
        if choice == "o":
            if item.get("password"):
                reveal_password(item["password"])
            else:
                print("Nothing to show: this secret has no password.")
            continue
        if choice == "s":
            return "search"
        if choice == "b":
            return "menu"
        if choice == "q":
            raise QuitProgram()
        if choice == "e":
            edit_menu(vault, item)
            show_item(vault, item)
            continue
        if choice == "d":
            if ask_yes_no("Delete secret '%s'?" % item.get("name", "")):
                vault.delete(item["id"])
                print("Secret deleted.")
                return "menu"
            print("Secret kept.")
            continue
        print("Please enter 's', 'b', 'e', 'd', 'l', 'p', 'u' or 'o'.")


def category_menu(vault):
    clear_screen()
    while True:
        print(render_categories(vault))
        choice = read_line("[a] add category  [r] rename category  "
                           "[d] delete category  [b] back: ").strip().lower()
        if choice == "b":
            return
        if choice == "q":
            raise QuitProgram()
        if choice == "a":
            name = read_line("Category name: ").strip()
            if not name:
                print("Category name cannot be empty.")
                continue
            cid = vault.add_category(name)
            print("Category '%s' added with ID %d." % (name, cid))
        elif choice == "r":
            cid = choose_category_id(vault, "Category ID to rename: ")
            if cid is None:
                print("Unknown category ID.")
                continue
            name = read_line("New category name: ").strip()
            if not name:
                print("Category name cannot be empty.")
                continue
            old = vault.get_category(cid)["name"]
            vault.rename_category(cid, name)
            print("Category '%s' renamed to '%s'." % (old, name))
        elif choice == "d":
            cid = choose_category_id(vault, "Category ID to delete: ")
            if cid is None:
                print("Unknown category ID.")
                continue
            cname = vault.get_category(cid)["name"]
            if vault.category_in_use(cid):
                print("Cannot delete category '%s': it is still assigned to "
                      "at least one secret." % cname)
                continue
            if ask_yes_no("Delete category '%s'?" % cname):
                vault.delete_category(cid)
                print("Category '%s' deleted." % cname)
            else:
                print("Category kept.")
        else:
            print("Please enter 'a', 'r', 'd' or 'b'.")


def search(vault):
    """Run the search prompt. Returns the next action."""
    while True:
        query = read_line_completing(
            "Enter search query ([s]earch, [a]dd, [b]ack, [q]uit): ",
            lambda: [it.get("name", "") for it in vault.sorted_items()])
        lowered = query.lower()
        if len(query) == 1 and lowered in ("s", "a", "q", "b"):
            if lowered == "s":
                continue
            if lowered == "a":
                return "add"
            if lowered == "q":
                raise QuitProgram()
            return "menu"
        if query == "":
            print("The search query cannot be empty.")
            return "menu"

        results = None
        stripped = query.strip()
        if stripped.isdigit():
            item = vault.get(int(stripped))
            if item is not None:
                results = [item]
        if results is None:
            results = vault.keyword_search(query)

        if not results:
            print("No results found for '%s'." % query)
            return "menu"
        if len(results) == 1:
            nxt = item_view(vault, results[0])
            if nxt == "search":
                continue
            return nxt
        print(render_table(results, vault))
        choice = read_line("Select an item by ID: ").strip()
        chosen = None
        if choice.isdigit():
            cid = int(choice)
            for it in results:
                if it["id"] == cid:
                    chosen = it
                    break
        if chosen is None:
            return "menu"
        nxt = item_view(vault, chosen)
        if nxt == "search":
            continue
        return nxt


def main_menu(vault):
    while True:
        clear_screen()
        print(COMPACT_BANNER)
        print("%d saved in the vault" % len(vault.items))
        print("[s] search   [all] show all   [a] add   [cat] categories   "
              "[l] lock   [q] quit")
        choice = read_line("> ").strip().lower()
        action = None
        if choice == "q":
            raise QuitProgram()
        elif choice == "l":
            raise ManualLock()
        elif choice == "s":
            action = "search"
        elif choice == "all":
            print(render_table(vault.sorted_items(), vault))
            action = "search"
        elif choice == "a":
            action = "add"
        elif choice == "cat":
            category_menu(vault)
            continue
        else:
            continue
        while action in ("search", "add"):
            if action == "search":
                action = search(vault)
            else:
                action = add_secret(vault)


# --------------------------------------------------------- import/export ---

SUPPORTED_FORMATS = ("json",)
EXPORT_FIELDS = ("name", "url", "login", "password", "notes", "category")


def export_records(vault):
    records = []
    for it in vault.sorted_items():
        records.append({
            "name": it.get("name", "") or "",
            "url": it.get("url", "") or "",
            "login": it.get("login", "") or "",
            "password": it.get("password", "") or "",
            "notes": it.get("notes", "") or "",
            "category": vault.category_name(it.get("category")),
        })
    return records


def run_export(vault, path):
    data = json.dumps(export_records(vault), indent=2, ensure_ascii=False)
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data + "\n")
    except OSError as exc:
        print("Error: could not write export file %s: %s" % (path, exc))
        return 1
    print("Exported %d item(s) to %s" % (len(vault.items), path))
    return 0


def _field_str(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False)


def load_import_file(path):
    """Return (records, error_message)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return None, "Error: could not open import file %s: %s" % (path, exc)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, ("Error: import file %s is not valid JSON: %s"
                      % (path, exc))
    if not isinstance(data, list):
        return None, ("Error: import file %s must contain a JSON array of "
                      "objects." % path)
    records = []
    for idx, element in enumerate(data, 1):
        if not isinstance(element, dict):
            return None, ("Error: element %d in import file %s is not a JSON "
                          "object; nothing was imported." % (idx, path))
        records.append({f: _field_str(element.get(f)) for f in EXPORT_FIELDS})
    return records, None


def render_import_preview(records):
    if not records:
        return "Empty!"
    rows = [[r["name"], r["url"], r["login"], r["category"]] for r in records]
    return _grid(["Name", "URL", "Login", "Category"], rows)


def import_records(vault, records):
    by_name = {}
    for c in vault.sorted_categories():
        by_name.setdefault(c["name"], c["id"])
    for r in records:
        cat_name = r["category"]
        cat_id = None
        if cat_name.strip() != "":
            if cat_name not in by_name:
                cid = vault.data.setdefault("next_category_id", 1)
                vault.data["next_category_id"] = cid + 1
                vault.categories.append({"id": cid, "name": cat_name})
                by_name[cat_name] = cid
            cat_id = by_name[cat_name]
        item_id = vault.data["next_id"]
        vault.data["next_id"] = item_id + 1
        vault.items.append({
            "id": item_id, "name": r["name"], "url": r["url"],
            "login": r["login"], "password": r["password"],
            "notes": r["notes"], "category": cat_id,
        })
    vault.save()
    return len(records)


def run_import(path):
    print("Warning: importing will add secrets to your vault. "
          "Please back up your vault (%s and %s) before continuing."
          % (VAULT_PATH, CONFIG_PATH))
    vault = unlock()
    records, error = load_import_file(path)
    if error:
        print(error)
        print("The vault was not modified.")
        return 1
    print(render_import_preview(records))
    if not ask_yes_no("Import these %d item(s)?" % len(records)):
        print("Import cancelled; the vault was not modified.")
        return 0
    original = json.loads(json.dumps(vault.data))
    try:
        count = import_records(vault, records)
    except OSError as exc:
        vault.data = original
        print("Error: could not save the vault: %s. Nothing was imported."
              % exc)
        return 1
    print("%d items have been imported." % count)
    return 0


# ------------------------------------------------------------------- cli ---

def _parse_seconds(flag, raw):
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or value != value or value in (float("inf"),
                                                    float("-inf")):
        print("Error: %s expects a number of seconds, got '%s'." % (flag, raw))
        sys.exit(2)
    return value


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="pwd_manager.py", allow_abbrev=False,
        description="Interactive password manager with an encrypted vault.")
    parser.add_argument("-t", metavar="N", dest="clipboard_ttl",
                        help="clipboard auto-clear seconds (default %d)"
                        % DEFAULT_CLIPBOARD_TTL)
    parser.add_argument("-p", metavar="N", dest="hide_delay",
                        help="password hide delay seconds (default %d)"
                        % DEFAULT_HIDE_DELAY)
    parser.add_argument("-a", metavar="N", dest="auto_lock",
                        help="auto-lock seconds (default %d)"
                        % DEFAULT_AUTO_LOCK)
    parser.add_argument("-v", metavar="PATH", dest="vault_path",
                        help="custom vault payload path")
    parser.add_argument("-c", metavar="PATH", dest="config_path",
                        help="custom metadata (config) path")
    parser.add_argument("-i", metavar="FILE", dest="import_file",
                        help="import secrets from FILE and exit")
    parser.add_argument("-x", metavar="FILE", dest="export_file",
                        help="export secrets to FILE and exit")
    parser.add_argument("-f", metavar="FORMAT", dest="format", default="json",
                        help="import/export file format (only 'json')")
    parser.add_argument("-e", action="store_true", dest="erase",
                        help="erase the vault and config files, then exit")
    parser.add_argument("-k", action="store_true", dest="change_key",
                        help="show how to change the master key, then exit")
    return parser


def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def apply_paths(args):
    global VAULT_PATH, CONFIG_PATH
    if args.vault_path is not None:
        VAULT_PATH = os.path.abspath(os.path.expanduser(args.vault_path))
    if args.config_path is not None:
        CONFIG_PATH = os.path.abspath(os.path.expanduser(args.config_path))
    if VAULT_PATH == CONFIG_PATH:
        print("Error: the vault path and the config path must differ.")
        sys.exit(2)
    for path in (VAULT_PATH, CONFIG_PATH):
        try:
            _ensure_parent(path)
        except OSError as exc:
            print("Error: could not create directory for %s: %s" % (path, exc))
            sys.exit(2)
    try:
        if os.path.isdir(VAULT_DIR) and VAULT_PATH.startswith(VAULT_DIR):
            os.chmod(VAULT_DIR, 0o700)
    except OSError:
        pass


TTL_FLAGS = (
    ("clipboard_ttl", "-t", "Clipboard auto-clear timeout"),
    ("hide_delay", "-p", "Password hide delay"),
    ("auto_lock", "-a", "Auto-lock timeout"),
)


def apply_ttl_flags(args):
    changed = False
    for key, flag, label in TTL_FLAGS:
        raw = getattr(args, key)
        if raw is None:
            continue
        value = _parse_seconds(flag, raw)
        SETTINGS[key] = value
        changed = True
        print("%s set to %s seconds." % (label, _fmt_seconds(value)))
    if changed:
        save_settings()


def config_missing_error():
    print("Error: the vault file %s exists but the config file %s is "
          "missing. The config file is required to unlock the vault. "
          "Please restore the config file (for example from a backup) "
          "and try again." % (VAULT_PATH, CONFIG_PATH))
    sys.exit(2)


def ensure_vault():
    """Run first-time setup if needed; exit if the metadata is missing."""
    if not os.path.isfile(VAULT_PATH):
        run_setup()
    elif not os.path.isfile(CONFIG_PATH):
        config_missing_error()


def print_change_key_advice():
    print("Changing the master key is not done in place; the vault is not "
          "modified by this command.")
    print("To change the master key:")
    print("  1. Export the current vault to JSON:")
    print("       python pwd_manager.py -x vault_export.json")
    print("  2. Create a new vault with the desired master key (move the old "
          "vault and config files aside, or use new -v/-c paths), e.g.:")
    print("       python pwd_manager.py -v NEW_VAULT_PATH -c NEW_CONFIG_PATH")
    print("  3. Import that JSON into the new vault:")
    print("       python pwd_manager.py -v NEW_VAULT_PATH -c NEW_CONFIG_PATH "
          "-i vault_export.json")
    print("Then securely delete the exported JSON file, since it contains "
          "your secrets in plain text.")


def run_erase():
    targets = [p for p in (VAULT_PATH, CONFIG_PATH) if os.path.exists(p)]
    print("Vault file:  %s" % VAULT_PATH)
    print("Config file: %s" % CONFIG_PATH)
    if not targets:
        print("Nothing to erase: neither the vault file nor the config file "
              "exists.")
        return 0
    if not ask_yes_no("Erase the vault and config files permanently? "
                      "This cannot be undone."):
        print("Erase cancelled; nothing was deleted.")
        return 0
    failed = False
    for path in targets:
        try:
            os.remove(path)
        except OSError as exc:
            print("Error: could not delete %s: %s" % (path, exc))
            failed = True
    if failed:
        return 1
    print("The vault and config files have been deleted.")
    return 0


def run_interactive():
    global _lock_armed
    vault = unlock()
    while True:
        _lock_armed = True
        try:
            main_menu(vault)
        except AutoLocked:
            _lock_armed = False
            print()
            print("Vault locked after %s second(s) of inactivity."
                  % _fmt_seconds(auto_lock_seconds()))
            vault = unlock()
        except ManualLock:
            _lock_armed = False
            print("Vault locked.")
            vault = unlock()


def main(argv=None):
    args = build_parser().parse_args(argv)
    one_shot = args.import_file is not None or args.export_file is not None
    if one_shot:
        if args.import_file is not None and args.export_file is not None:
            print("Error: -i and -x cannot be used together.")
            return 2
        fmt = (args.format or "").strip().lower()
        if fmt not in SUPPORTED_FORMATS:
            print("Error: unsupported format '%s'. Only 'json' is supported."
                  % args.format)
            return 2
    try:
        apply_paths(args)
        if args.change_key:
            print_change_key_advice()
            return 0
        if args.erase:
            return run_erase()
        load_settings()
        apply_ttl_flags(args)
        ensure_vault()
        if args.export_file is not None:
            vault = unlock()
            return run_export(vault, args.export_file)
        if args.import_file is not None:
            return run_import(args.import_file)
        run_interactive()
    except (QuitProgram, KeyboardInterrupt):
        print("Goodbye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
