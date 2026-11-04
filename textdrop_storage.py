"""Pluggable storage backends for TextDrop (stdlib only).

Two backends are available, selected with a ``<type>:<config>`` spec string:

* ``local:<directory>`` -- notes are persisted as JSON files in the directory.
* ``object:<json>`` -- notes are persisted in an S3-compatible object store
  using path-style addressing: ``{url}/{container}/{noteID}.json``.
"""
import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import tempfile
import threading
from urllib.parse import quote, urlsplit

OBJECT_KEYS = ("url", "key_id", "key_secret", "container")
NOTE_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
LOCAL_SUBDIR = "notes"
HTTP_TIMEOUT = 15


class StoreConfigError(Exception):
    """Raised when a storage spec is invalid or the backend is unusable at startup."""


class StorageError(Exception):
    """Raised when a backend read or write fails at runtime."""


def now_timestamp():
    """Current UTC time as ISO 8601 with no fractional seconds, e.g. 2024-03-15T09:42:17Z."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_timestamp(value):
    """Normalise a stored timestamp to YYYY-MM-DDTHH:MM:SSZ (UTC); None if unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_record(content, mode, created_at):
    return {"content": content, "mode": mode or "plain", "created_at": created_at}


def parse_record(data):
    """Decode a stored JSON record into (content, mode, created_at)."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageError("stored note is not valid UTF-8") from exc
    try:
        obj = json.loads(data)
    except ValueError as exc:
        raise StorageError("stored note is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise StorageError("stored note is not a JSON object")
    content = obj.get("content")
    if not isinstance(content, str):
        raise StorageError("stored note has no content")
    mode = obj.get("mode")
    mode = "markdown" if isinstance(mode, str) and mode.lower() == "markdown" else ""
    created = obj.get("created_at")
    return content, mode, created if isinstance(created, str) else None


def _encode_record(content, mode, created_at):
    return json.dumps(make_record(content, mode, created_at),
                      ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Local file-backed store
# ---------------------------------------------------------------------------
class LocalStore:
    kind = "local"

    def __init__(self, directory):
        self.root = os.path.abspath(directory or ".")
        self.dir = os.path.join(self.root, LOCAL_SUBDIR)
        self._lock = threading.Lock()
        self._count = 0

    def validate(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:
            raise StoreConfigError(f"cannot create storage directory {self.dir!r}: {exc}")
        if not os.path.isdir(self.dir):
            raise StoreConfigError(f"storage path {self.dir!r} is not a directory")
        try:
            fd, probe = tempfile.mkstemp(prefix=".probe-", dir=self.dir)
            os.close(fd)
            os.unlink(probe)
        except OSError as exc:
            raise StoreConfigError(f"storage directory {self.dir!r} is not writable: {exc}")
        try:
            self._count = sum(1 for n in os.listdir(self.dir)
                              if n.endswith(".json") and not n.startswith("."))
        except OSError:
            self._count = 0

    def _path(self, nid):
        return os.path.join(self.dir, nid + ".json")

    def put_new(self, nid, content, mode, created_at):
        """Store a note under nid. Returns False if nid is already taken."""
        data = _encode_record(content, mode, created_at)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.dir)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, self._path(nid))
            except FileExistsError:
                return False
        except OSError as exc:
            raise StorageError(f"failed to write note: {exc}") from exc
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        with self._lock:
            self._count += 1
        return True

    def get(self, nid):
        if not NOTE_ID_RE.match(nid):
            return None
        try:
            with open(self._path(nid), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(f"failed to read note: {exc}") from exc
        return parse_record(data)

    def count(self):
        with self._lock:
            return self._count


# ---------------------------------------------------------------------------
# S3-compatible object store (AWS S3, GCS interoperability, generic)
# ---------------------------------------------------------------------------
_AWS_REGION_RE = re.compile(r"(?:^|\.)s3[.-]([a-z0-9-]+)\.amazonaws\.com$")


def _region_for(host):
    host = host.lower()
    m = _AWS_REGION_RE.search(host)
    if m and m.group(1) not in ("external-1", "accelerate", "dualstack"):
        return m.group(1)
    if host.endswith("storage.googleapis.com"):
        return "auto"
    return "us-east-1"


class ObjectStore:
    kind = "object"

    def __init__(self, url, key_id, key_secret, container):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise StoreConfigError(f"object storage url must be an http(s) URL: {url!r}")
        try:
            parts.port
        except ValueError as exc:
            raise StoreConfigError(f"invalid port in object storage url: {url!r}") from exc
        if not container:
            raise StoreConfigError("object storage container must not be empty")
        self.scheme = parts.scheme
        self.netloc = parts.netloc.rsplit("@", 1)[-1]
        self.host = parts.hostname
        self.port = parts.port
        self.base_path = parts.path.rstrip("/")
        self.key_id = key_id
        self.key_secret = key_secret
        self.container = container
        self.region = _region_for(self.host)
        self._lock = threading.Lock()
        self._count = 0

    # -- HTTP / signing ----------------------------------------------------
    def _container_path(self):
        return f"{self.base_path}/{quote(self.container, safe='-_.~')}"

    def _object_path(self, nid):
        return f"{self._container_path()}/{quote(nid, safe='-_.~')}.json"

    def _sign(self, method, path, headers, payload_hash):
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        headers["x-amz-date"] = amz_date
        headers["x-amz-content-sha256"] = payload_hash
        canon = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
        signed = ";".join(sorted(canon))
        canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in sorted(canon))
        canonical_request = "\n".join(
            [method, path or "/", "", canonical_headers, signed, payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])

        def _h(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k = _h(("AWS4" + self.key_secret).encode("utf-8"), datestamp)
        k = _h(k, self.region)
        k = _h(k, "s3")
        k = _h(k, "aws4_request")
        signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}")

    def _request(self, method, path, body=None, content_type=None):
        """Perform a signed request; returns (status, body bytes). Raises OSError etc."""
        payload = body or b""
        headers = {"Host": self.netloc}
        if content_type:
            headers["Content-Type"] = content_type
        self._sign(method, path, headers, hashlib.sha256(payload).hexdigest())
        if body is not None:
            headers["Content-Length"] = str(len(payload))
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=HTTP_TIMEOUT)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=HTTP_TIMEOUT)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read() if method != "HEAD" else b""
            return resp.status, data
        finally:
            conn.close()

    # -- store API -----------------------------------------------------------
    def validate(self):
        try:
            status, _ = self._request("HEAD", self._container_path())
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise StoreConfigError(
                f"cannot connect to object storage at {self.scheme}://{self.netloc}: {exc}")
        if status != 200:
            if status in (401, 403):
                reason = "credentials were rejected"
            elif status == 404:
                reason = "container does not exist"
            else:
                reason = "container is not accessible"
            raise StoreConfigError(
                f"object storage container {self.container!r}: {reason} (HTTP {status})")

    def put_new(self, nid, content, mode, created_at):
        data = _encode_record(content, mode, created_at)
        try:
            status, _ = self._request("PUT", self._object_path(nid), data,
                                      "application/json")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise StorageError(f"object storage write failed: {exc}") from exc
        if not 200 <= status < 300:
            raise StorageError(f"object storage write failed (HTTP {status})")
        with self._lock:
            self._count += 1
        return True

    def get(self, nid):
        if not NOTE_ID_RE.match(nid):
            return None
        try:
            status, data = self._request("GET", self._object_path(nid))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise StorageError(f"object storage read failed: {exc}") from exc
        if status == 404:
            return None
        if status != 200:
            raise StorageError(f"object storage read failed (HTTP {status})")
        return parse_record(data)

    def count(self):
        with self._lock:
            return self._count


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------
def parse_store_spec(spec):
    """Parse a ``<type>:<config>`` spec into an (unvalidated) backend instance."""
    if spec is None or ":" not in spec:
        raise StoreConfigError(
            f"malformed storage spec {spec!r}: expected '<type>:<config>'")
    kind, config = spec.split(":", 1)
    if kind == "local":
        return LocalStore(config)
    if kind == "object":
        try:
            obj = json.loads(config)
        except ValueError as exc:
            raise StoreConfigError(f"invalid JSON in object storage config: {exc}")
        if not isinstance(obj, dict):
            raise StoreConfigError("object storage config must be a JSON object")
        missing = [k for k in OBJECT_KEYS if k not in obj]
        if missing:
            raise StoreConfigError(
                "object storage config is missing required keys: " + ", ".join(missing))
        extra = sorted(k for k in obj if k not in OBJECT_KEYS)
        if extra:
            raise StoreConfigError(
                "object storage config has unexpected keys: " + ", ".join(extra))
        bad = [k for k in OBJECT_KEYS if not isinstance(obj[k], str)]
        if bad:
            raise StoreConfigError(
                "object storage config values must be strings: " + ", ".join(bad))
        return ObjectStore(obj["url"], obj["key_id"], obj["key_secret"], obj["container"])
    raise StoreConfigError(
        f"unknown storage type {kind!r}: expected 'local' or 'object'")


def open_store(spec):
    """Parse and validate a storage spec; raises StoreConfigError on failure."""
    store = parse_store_spec(spec)
    store.validate()
    return store


class NoteStore:
    """Note store facade: ID generation on top of a persistence backend."""

    def __init__(self, backend, make_id):
        self.backend = backend
        self._make_id = make_id

    def add(self, content, mode="", user=None):
        created_at = now_timestamp()
        for _ in range(64):
            nid = self._make_id(content, user)
            if self.backend.put_new(nid, content, mode, created_at):
                return nid
        raise StorageError("could not allocate a unique note ID")

    def get_entry(self, nid):
        """Return (content, mode, created_at) or None. Raises StorageError."""
        return self.backend.get(nid)

    def get(self, nid):
        entry = self.get_entry(nid)
        return entry[0] if entry else None

    def count(self):
        return self.backend.count()


__all__ = ["StoreConfigError", "StorageError", "NoteStore", "open_store",
           "parse_store_spec", "format_timestamp", "now_timestamp"]
