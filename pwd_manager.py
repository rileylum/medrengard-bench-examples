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
import sys

VAULT_DIR = os.path.join(os.path.expanduser("~"), ".vault")
CONFIG_PATH = os.path.join(VAULT_DIR, "config.json")
VAULT_PATH = os.path.join(VAULT_DIR, "vault.dat")

CONFIG_VERSION = 1
KDF_ITERATIONS = 200_000
MIN_KEY_LEN = 8
MAX_NOTE_LINES = 15
MAX_ATTEMPTS = 3
PAYLOAD_MAGIC = b"PWDV1"

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

def read_line(prompt=""):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if line == "":
        sys.stdout.write("\n")
        raise QuitProgram()
    return line.rstrip("\r\n")


def read_secret(prompt):
    if sys.stdin.isatty():
        import getpass
        try:
            return getpass.getpass(prompt)
        except EOFError:
            raise QuitProgram()
    return read_line(prompt)


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
    salt = secrets.token_bytes(16)
    cfg = write_config(salt)
    vault = Vault(derive_keys(key, salt), empty_vault_data())
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
        print("Error: vault config file is missing or unreadable (%s). "
              "The vault cannot be opened without it." % CONFIG_PATH,
              file=sys.stderr)
        print("Error: config file missing or invalid: %s" % exc)
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
    login = read_line("Login: ")
    password = read_secret("Password: ")
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
        value = read_secret("New password: ")
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


def item_view(vault, item):
    clear_screen()
    show_item(vault, item)
    while True:
        choice = read_line("[s] new search  [b] back to menu  [e] edit  "
                           "[d] delete: ").strip().lower()
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
        print("Please enter 's', 'b', 'e' or 'd'.")


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
        query = read_line("Enter search query ([s]earch, [a]dd, [b]ack, [q]uit): ")
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
        print("[s] search   [all] show all   [a] add   [cat] categories   [q] quit")
        choice = read_line("> ").strip().lower()
        action = None
        if choice == "q":
            raise QuitProgram()
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


def main():
    try:
        os.makedirs(VAULT_DIR, exist_ok=True)
        try:
            os.chmod(VAULT_DIR, 0o700)
        except OSError:
            pass
        if not os.path.isfile(VAULT_PATH):
            run_setup()
        elif not os.path.isfile(CONFIG_PATH):
            print("Error: vault data exists at %s but the config file %s is "
                  "missing. The vault cannot be opened without its salt."
                  % (VAULT_PATH, CONFIG_PATH))
            sys.exit(2)
        vault = unlock()
        main_menu(vault)
    except (QuitProgram, KeyboardInterrupt):
        print("Goodbye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
