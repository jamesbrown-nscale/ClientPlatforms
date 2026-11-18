"""Storage for messages, devices and receipts.

    STORE=firestore   Google Cloud Firestore (production on Cloud Run)
    STORE=sqlite      a local file (development and tests), DATABASE_PATH=./notify.db

Records are plain dicts with the same fields in both stores:

  message  id subject body body_rich priority from_name link_text link_url audience[] platforms[]
           created_at expires_at sent_by withdrawn_at withdrawn_by
  device   key device_id username hostname upn platform os_version app_version first_seen last_seen
  receipt  message_id device_key delivered_at opened_at acknowledged_at

Times are ISO strings in UTC ("2026-11-17T21:30:00Z"), so they sort as text.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

EVENT_FIELD = {"delivered": "delivered_at", "opened": "opened_at", "acknowledged": "acknowledged_at"}
# Opened implies delivered; acknowledged implies opened and delivered.
IMPLIES = {"delivered": ["delivered"], "opened": ["delivered", "opened"],
           "acknowledged": ["delivered", "opened", "acknowledged"]}


def device_key(device_id: str, username: str) -> str:
    return f"{device_id}|{username}".replace("/", "_")


class Store:
    """Interface both stores implement."""
    def get_device(self, key: str) -> dict | None: ...
    def save_device(self, device: dict) -> None: ...
    def delivered_ids(self, device: dict) -> set[str]: ...
    def record_event(self, device: dict, message_id: str, event: str, at: str) -> None: ...
    def recent_messages(self, since: str) -> list[dict]: ...
    def get_message(self, mid: str, fresh: bool = False) -> dict | None: ...
    def create_message(self, m: dict) -> None: ...
    def list_messages(self, limit: int) -> list[dict]: ...
    def withdraw(self, mid: str, by: str, at: str) -> bool: ...
    def counts(self, m: dict) -> tuple[int, int, int]: ...
    def receipts_for(self, mid: str) -> dict[str, dict]: ...
    def devices_seen_since(self, cutoff: str) -> list[dict]: ...
    def list_devices(self, limit: int) -> list[dict]: ...


def make_store() -> Store:
    kind = os.environ.get("STORE", "sqlite").lower()
    if kind == "firestore":
        return FirestoreStore()
    return SqliteStore(os.environ.get("DATABASE_PATH", "./notify.db"))


# ======================================================================
# SQLite (local development and tests)
# ======================================================================

class SqliteStore(Store):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, subject TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
        body_rich TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL, from_name TEXT NOT NULL,
        link_text TEXT, link_url TEXT, audience TEXT NOT NULL DEFAULT '[]', platforms TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL, expires_at TEXT, sent_by TEXT NOT NULL, withdrawn_at TEXT, withdrawn_by TEXT);
    CREATE TABLE IF NOT EXISTS devices (
        key TEXT PRIMARY KEY, device_id TEXT NOT NULL, username TEXT NOT NULL, hostname TEXT NOT NULL,
        upn TEXT, platform TEXT NOT NULL, os_version TEXT, app_version TEXT,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS receipts (
        message_id TEXT NOT NULL, device_key TEXT NOT NULL,
        delivered_at TEXT, opened_at TEXT, acknowledged_at TEXT,
        PRIMARY KEY (message_id, device_key));
    CREATE INDEX IF NOT EXISTS ix_messages_created ON messages(created_at);
    CREATE INDEX IF NOT EXISTS ix_receipts_device ON receipts(device_key);
    CREATE INDEX IF NOT EXISTS ix_devices_seen ON devices(last_seen);
    """
    DEVICE_FIELDS = ("key", "device_id", "username", "hostname", "upn", "platform", "os_version",
                     "app_version", "first_seen", "last_seen")

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._db() as c:
            c.executescript(self.SCHEMA)

    @contextmanager
    def _db(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _msg(row) -> dict:
        m = dict(row)
        m["audience"] = json.loads(m["audience"])
        m["platforms"] = json.loads(m["platforms"])
        return m

    def get_device(self, key):
        with self._db() as c:
            r = c.execute("SELECT * FROM devices WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def save_device(self, d):
        with self._db() as c:
            c.execute(f"INSERT OR REPLACE INTO devices ({','.join(self.DEVICE_FIELDS)}) VALUES ({','.join('?' * len(self.DEVICE_FIELDS))})",
                      tuple(d.get(f) for f in self.DEVICE_FIELDS))

    def delivered_ids(self, device):
        with self._db() as c:
            return {r[0] for r in c.execute(
                "SELECT message_id FROM receipts WHERE device_key=? AND delivered_at IS NOT NULL", (device["key"],))}

    def record_event(self, device, message_id, event, at):
        with self._db() as c:
            c.execute("INSERT OR IGNORE INTO receipts (message_id, device_key) VALUES (?,?)", (message_id, device["key"]))
            for e in IMPLIES[event]:
                f = EVENT_FIELD[e]
                c.execute(f"UPDATE receipts SET {f}=COALESCE({f}, ?) WHERE message_id=? AND device_key=?",
                          (at, message_id, device["key"]))

    def recent_messages(self, since):
        with self._db() as c:
            return [self._msg(r) for r in c.execute(
                "SELECT * FROM messages WHERE withdrawn_at IS NULL AND created_at >= ? ORDER BY created_at", (since,))]

    def get_message(self, mid, fresh=False):
        with self._db() as c:
            r = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return self._msg(r) if r else None

    def create_message(self, m):
        row = {**m, "audience": json.dumps(m["audience"]), "platforms": json.dumps(m["platforms"])}
        cols = [k for k in row if k in {"id", "subject", "body", "body_rich", "priority", "from_name", "link_text",
                                        "link_url", "audience", "platforms", "created_at", "expires_at", "sent_by"}]
        with self._db() as c:
            c.execute(f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      tuple(row[k] for k in cols))

    def list_messages(self, limit):
        with self._db() as c:
            return [self._msg(r) for r in c.execute(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,))]

    def withdraw(self, mid, by, at):
        with self._db() as c:
            return c.execute("UPDATE messages SET withdrawn_at=?, withdrawn_by=? WHERE id=? AND withdrawn_at IS NULL",
                             (at, by, mid)).rowcount > 0

    def counts(self, m):
        with self._db() as c:
            r = c.execute("SELECT COUNT(delivered_at), COUNT(opened_at), COUNT(acknowledged_at) "
                          "FROM receipts WHERE message_id=?", (m["id"],)).fetchone()
        return r[0], r[1], r[2]

    def receipts_for(self, mid):
        with self._db() as c:
            return {r["device_key"]: dict(r) for r in c.execute("SELECT * FROM receipts WHERE message_id=?", (mid,))}

    def devices_seen_since(self, cutoff):
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM devices WHERE last_seen >= ?", (cutoff,))]

    def list_devices(self, limit):
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM devices ORDER BY last_seen DESC LIMIT ?", (limit,))]


# ======================================================================
# Firestore (production)
# ======================================================================

class _TTLCache:
    def __init__(self, seconds: float):
        self.seconds, self.value, self.at = seconds, None, 0.0

    def get(self):
        return self.value if time.monotonic() - self.at < self.seconds else None

    def set(self, value):
        self.value, self.at = value, time.monotonic()

    def clear(self):
        self.at = 0.0


def _where(query, field: str, op: str, value):
    """Real client: filter=FieldFilter(...). The in-memory test stand-in only knows the older form."""
    if query.__class__.__module__.startswith("google."):
        from google.cloud.firestore_v1.base_query import FieldFilter
        return query.where(filter=FieldFilter(field, op, value))
    return query.where(field, op, value)


class FirestoreStore(Store):
    """
    Collections:
      messages/{id}                       message fields + delivered_count, opened_count, acknowledged_count
      devices/{device_id|username}        device fields + delivered[] (ids of messages already delivered)
      receipts/{message_id|device_key}    receipt fields

    Every device checks in every minute, so reads are kept low: the recent-messages list is cached
    for a few seconds per server instance, and each device's record for a few minutes. A check-in
    normally costs no Firestore reads, and one write every few minutes. Only single-field indexes
    are used (Firestore creates those automatically).
    """
    LOOKBACK_DAYS = int(os.environ.get("MESSAGE_LOOKBACK_DAYS", "14"))
    DEVICE_CACHE_SECONDS = 300
    DELIVERED_KEEP = 300

    def __init__(self, client=None):
        if client is None:
            from google.cloud import firestore
            client = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
        self.db = client
        self.messages = client.collection("messages")
        self.devices = client.collection("devices")
        self.receipts = client.collection("receipts")
        self._recent = _TTLCache(float(os.environ.get("MESSAGE_CACHE_SECONDS", "15")))
        self._active_devices: dict[str, _TTLCache] = {}
        self._device_cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    # ---- helpers ----
    @staticmethod
    def _transforms():
        from google.cloud import firestore
        return firestore

    @staticmethod
    def _lookback_floor() -> str:
        t = time.gmtime(time.time() - FirestoreStore.LOOKBACK_DAYS * 86400)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)

    # ---- devices ----
    def get_device(self, key):
        with self._lock:
            hit = self._device_cache.get(key)
            if hit and time.monotonic() - hit[0] < self.DEVICE_CACHE_SECONDS:
                return dict(hit[1])
        snap = self.devices.document(key).get()
        d = snap.to_dict() if snap.exists else None
        if d is not None:
            d["key"] = key
            with self._lock:
                self._device_cache[key] = (time.monotonic(), d)
        return dict(d) if d else None

    def save_device(self, d):
        data = {k: v for k, v in d.items() if k != "key"}
        delivered = list(data.get("delivered") or [])
        if len(delivered) > self.DELIVERED_KEEP:
            data["delivered"] = delivered[-self.DELIVERED_KEEP:]
        self.devices.document(d["key"]).set(data, merge=True)
        with self._lock:
            self._device_cache[d["key"]] = (time.monotonic(), {**data, "key": d["key"]})

    def delivered_ids(self, device):
        return set(device.get("delivered") or [])

    def record_event(self, device, message_id, event, at):
        fs = self._transforms()
        ref = self.receipts.document(f"{message_id}|{device['key']}")
        snap = ref.get()
        current = snap.to_dict() if snap.exists else {}
        updates, counters = {}, {}
        for e in IMPLIES[event]:
            f = EVENT_FIELD[e]
            if not current.get(f):
                updates[f] = at
                counters[f"{e}_count"] = fs.Increment(1)
        if not updates:
            return
        ref.set({"message_id": message_id, "device_key": device["key"], **current, **updates}, merge=True)
        try:
            self.messages.document(message_id).update(counters)
        except Exception:
            pass   # message deleted; the receipt is still kept
        if "delivered_at" in updates:
            self.devices.document(device["key"]).update({"delivered": fs.ArrayUnion([message_id])})
            device.setdefault("delivered", [])
            if message_id not in device["delivered"]:
                device["delivered"].append(message_id)
            with self._lock:
                self._device_cache[device["key"]] = (time.monotonic(), dict(device))

    # ---- messages ----
    def _recent_all(self) -> list[dict]:
        cached = self._recent.get()
        if cached is not None:
            return cached
        docs = _where(self.messages, "created_at", ">=", self._lookback_floor()).order_by("created_at").stream()
        rows = [{**d.to_dict(), "id": d.id} for d in docs]
        self._recent.set(rows)
        return rows

    def recent_messages(self, since):
        return [m for m in self._recent_all() if m["created_at"] >= since and not m.get("withdrawn_at")]

    def get_message(self, mid, fresh=False):
        if not fresh:                       # check-ins: the cached list is fine
            for m in self._recent_all():
                if m["id"] == mid:
                    return m
        snap = self.messages.document(mid).get()
        return {**snap.to_dict(), "id": mid} if snap.exists else None

    def create_message(self, m):
        data = {k: v for k, v in m.items() if k != "id"}
        data.update(delivered_count=0, opened_count=0, acknowledged_count=0, withdrawn_at=None, withdrawn_by=None)
        self.messages.document(m["id"]).set(data)
        self._recent.clear()

    def list_messages(self, limit):
        from_query = self.messages.order_by("created_at", direction="DESCENDING").limit(limit).stream()
        return [{**d.to_dict(), "id": d.id} for d in from_query]

    def withdraw(self, mid, by, at):
        ref = self.messages.document(mid)
        snap = ref.get()
        if not snap.exists or snap.to_dict().get("withdrawn_at"):
            return False
        ref.update({"withdrawn_at": at, "withdrawn_by": by})
        self._recent.clear()
        return True

    def counts(self, m):
        return (int(m.get("delivered_count") or 0), int(m.get("opened_count") or 0),
                int(m.get("acknowledged_count") or 0))

    def receipts_for(self, mid):
        return {r.to_dict()["device_key"]: r.to_dict()
                for r in _where(self.receipts, "message_id", "==", mid).stream()}

    # ---- device lists (admin pages) ----
    def devices_seen_since(self, cutoff):
        cache = self._active_devices.setdefault(cutoff[:15], _TTLCache(60))   # same cutoff within ~10 min
        rows = cache.get()
        if rows is None:
            rows = [{**d.to_dict(), "key": d.id} for d in _where(self.devices, "last_seen", ">=", cutoff).stream()]
            cache.set(rows)
            if len(self._active_devices) > 20:
                self._active_devices = {cutoff[:15]: cache}
        return rows

    def list_devices(self, limit):
        docs = self.devices.order_by("last_seen", direction="DESCENDING").limit(limit).stream()
        return [{**d.to_dict(), "key": d.id} for d in docs]
