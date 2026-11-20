"""Storage for messages, devices and receipts: SQLite, at DATABASE_PATH (the service's Docker volume).

Records are plain dicts:

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
from contextlib import contextmanager

EVENT_FIELD = {"delivered": "delivered_at", "opened": "opened_at", "acknowledged": "acknowledged_at"}
# Opened implies delivered; acknowledged implies opened and delivered.
IMPLIES = {"delivered": ["delivered"], "opened": ["delivered", "opened"],
           "acknowledged": ["delivered", "opened", "acknowledged"]}


def device_key(device_id: str, username: str) -> str:
    return f"{device_id}|{username}".replace("/", "_")


class Store:
    """What the server needs from storage."""
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
    return SqliteStore(os.environ.get("DATABASE_PATH", "./notify.db"))


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
