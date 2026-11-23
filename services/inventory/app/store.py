"""Where Users, Devices and the Jamf / Intune connections are kept: one SQLite file (DATABASE_PATH,
/data/inventory.db in the container).

    devices      every device from Jamf and Intune, as of the last sync (id = "jamf:<id>" / "intune:<id>")
    users        the people imported by CSV (or added by hand), with their tags: department, division
                 (their team within it), line manager, user type (VIP / Standard) and primary device (a
                 device id)
    connections  Jamf and Intune: where to connect, the client ID, the secret (encrypted) and how the
                 last sync went

Connection secrets are encrypted with a key made from AUTH_SECRET, so the database alone (or a disk
snapshot) doesn't give anyone the Jamf or Intune credentials. Change AUTH_SECRET and they have to be
entered again.
"""
import base64
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from .recycle import Bin

DEVICE_FIELDS = ("platform", "kind", "name", "make", "model", "os", "os_version", "serial", "user_email", "user_name", "last_seen")
USER_FIELDS = ("name", "department", "division", "manager", "user_type", "primary_device", "title")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")



class Store:
    def __init__(self, path: str, secret: str = ""):
        self.path = path
        self._lock = threading.Lock()
        key = hashlib.sha256(b"cp-inventory-connections." + (secret or "dev-only").encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, platform TEXT NOT NULL, kind TEXT DEFAULT '', name TEXT DEFAULT '',
                    make TEXT DEFAULT '', model TEXT DEFAULT '', os TEXT DEFAULT '', os_version TEXT DEFAULT '',
                    serial TEXT DEFAULT '', user_email TEXT DEFAULT '', user_name TEXT DEFAULT '', last_seen TEXT DEFAULT '',
                    synced_at TEXT);
                CREATE INDEX IF NOT EXISTS devices_user ON devices(user_email);
                CREATE INDEX IF NOT EXISTS devices_serial ON devices(serial);
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', team TEXT DEFAULT '', manager TEXT DEFAULT '',
                    user_type TEXT DEFAULT 'Standard', primary_device TEXT DEFAULT '', title TEXT DEFAULT '',
                    department TEXT DEFAULT '', created_at TEXT, updated_at TEXT, updated_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS user_review (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL, name TEXT DEFAULT '',
                    fields TEXT NOT NULL DEFAULT '{}', changes TEXT NOT NULL DEFAULT '[]', created_at TEXT, created_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS connections (
                    kind TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}', secret TEXT DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT, last_ok TEXT, last_error TEXT DEFAULT '',
                    last_count INTEGER, last_note TEXT DEFAULT '', updated_at TEXT, updated_by TEXT DEFAULT '');
            """)
            have = {r[1] for r in db.execute("PRAGMA table_info(users)")}
            if "division" not in have:
                # A user's team is now their Division (within their Department): the old free-text
                # Team tag moves across.
                db.execute("ALTER TABLE users ADD COLUMN division TEXT DEFAULT ''")
                db.execute("UPDATE users SET division = team WHERE COALESCE(team, '') != ''")
        self.bin = Bin(self._db, self._lock)   # deleted users, kept 30 days (recycle.py; Settings → Recycle bin)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ----- devices -----

    def devices(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM devices ORDER BY name COLLATE NOCASE, id")]

    def device(self, did: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
            return dict(r) if r else None

    def replace_devices(self, platform: str, rows: list[dict]) -> int:
        """Everything this platform has now: new and changed devices are saved, and its devices that
        aren't there any more are removed. Only called after a complete, successful read."""
        t = now()
        with self._lock, self._db() as db:
            ids = []
            for d in rows:
                ids.append(d["id"])
                vals = [str(d.get(f) or "")[:300] for f in DEVICE_FIELDS]
                db.execute(f"INSERT INTO devices (id, {', '.join(DEVICE_FIELDS)}, synced_at) VALUES (?, {', '.join('?' * len(DEVICE_FIELDS))}, ?) "
                           f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{f} = excluded.{f}' for f in DEVICE_FIELDS)}, synced_at = excluded.synced_at",
                           (d["id"], *vals, t))
            db.execute("CREATE TEMP TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
            db.execute("DELETE FROM seen")
            db.executemany("INSERT OR IGNORE INTO seen (id) VALUES (?)", [(i,) for i in ids])
            db.execute("DELETE FROM devices WHERE platform = ? AND id NOT IN (SELECT id FROM seen)", (platform,))
            return len(set(ids))

    def drop_platform(self, platform: str) -> int:
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM devices WHERE platform = ?", (platform,)).rowcount

    # ----- users -----

    def users(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE, email")]

    def user(self, email: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            return dict(r) if r else None

    def save_users(self, rows: list[dict], by: str) -> tuple[int, int]:
        """Adds or updates each {email, ...fields}; only the fields given change. (added, updated)"""
        t = now()
        added = updated = 0
        with self._lock, self._db() as db:
            for u in rows:
                old = db.execute("SELECT * FROM users WHERE email = ?", (u["email"],)).fetchone()
                fields = {f: u[f] for f in USER_FIELDS if f in u}
                if old is None:
                    full = {f: "" for f in USER_FIELDS} | {"user_type": "Standard"} | fields
                    db.execute(f"INSERT INTO users (email, {', '.join(USER_FIELDS)}, created_at, updated_at, updated_by) "
                               f"VALUES (?, {', '.join('?' * len(USER_FIELDS))}, ?, ?, ?)",
                               (u["email"], *[full[f] for f in USER_FIELDS], t, t, by))
                    added += 1
                elif any((old[f] or "") != (v or "") for f, v in fields.items()):
                    db.execute(f"UPDATE users SET {', '.join(f'{f} = ?' for f in fields)}, updated_at = ?, updated_by = ? WHERE email = ?",
                               (*fields.values(), t, by, u["email"]))
                    updated += 1
        return added, updated

    def delete_users(self, emails: list[str], by: str = "") -> int:
        """by: who deleted them from the page; they go into the recycle bin. (An import replacing them doesn't.)"""
        with self._lock, self._db() as db:
            n = 0
            for e in emails:
                if by:
                    r = db.execute("SELECT name FROM users WHERE email = ?", (e,)).fetchone()
                    if r:
                        self.bin.put(db, kind="User", label=r["name"] or e, context=e, table="users", where={"email": e},
                                     by=by, tool="users")
                n += db.execute("DELETE FROM users WHERE email = ?", (e,)).rowcount
            return n

    # ----- review: what a CSV import would change, waiting for someone to accept it -----
    #   new      in the file, not on Users          fields: everything the file gives them
    #   changed  on Users, the file differs         fields: just what changes; changes: [{field, from, to}]
    #   left     on Users, not in the file          (only when the file was said to be everyone)

    def review(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM user_review ORDER BY kind, name COLLATE NOCASE, email").fetchall()
        return [{**dict(r), "fields": json.loads(r["fields"]), "changes": json.loads(r["changes"])} for r in rows]

    def review_count(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM user_review").fetchone()[0]

    def replace_review(self, items: list[dict], by: str) -> None:
        """A new import replaces whatever was still waiting from the last one."""
        t = now()
        with self._lock, self._db() as db:
            db.execute("DELETE FROM user_review")
            db.executemany("INSERT INTO user_review (id, kind, email, name, fields, changes, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(i["id"], i["kind"], i["email"], i.get("name", ""), json.dumps(i.get("fields", {})),
                             json.dumps(i.get("changes", [])), t, by) for i in items])

    def drop_review(self, ids: list[str]) -> None:
        with self._lock, self._db() as db:
            db.executemany("DELETE FROM user_review WHERE id = ?", [(i,) for i in ids])

    # ----- connections -----

    def connection(self, kind: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM connections WHERE kind = ?", (kind,)).fetchone()
        if not r:
            return None
        c = dict(r)
        c["settings"] = json.loads(c["settings"] or "{}")
        c["enabled"] = bool(c["enabled"])
        c["has_secret"] = bool(c.pop("secret"))
        return c

    def secret(self, kind: str) -> str:
        """The decrypted secret, or "" if there's none or it can't be read (AUTH_SECRET changed)."""
        with self._db() as db:
            r = db.execute("SELECT secret FROM connections WHERE kind = ?", (kind,)).fetchone()
        if not r or not r["secret"]:
            return ""
        try:
            return self._fernet.decrypt(r["secret"].encode()).decode()
        except InvalidToken:
            return ""

    def save_connection(self, kind: str, settings: dict, secret: str | None, enabled: bool, by: str) -> None:
        """secret None keeps the one saved; "" removes it."""
        t = now()
        enc = None if secret is None else (self._fernet.encrypt(secret.encode()).decode() if secret else "")
        with self._lock, self._db() as db:
            db.execute("INSERT INTO connections (kind, settings, secret, enabled, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(kind) DO UPDATE SET settings = excluded.settings, enabled = excluded.enabled, "
                       "updated_at = excluded.updated_at, updated_by = excluded.updated_by"
                       + ("" if enc is None else ", secret = excluded.secret"),
                       (kind, json.dumps(settings), enc or "", int(enabled), t, by))

    def delete_connection(self, kind: str) -> bool:
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM connections WHERE kind = ?", (kind,)).rowcount > 0

    def sync_result(self, kind: str, count: int | None, error: str = "", note: str = "") -> None:
        t = now()
        with self._lock, self._db() as db:
            if error:
                db.execute("UPDATE connections SET last_sync = ?, last_error = ? WHERE kind = ?", (t, error[:500], kind))
            else:
                db.execute("UPDATE connections SET last_sync = ?, last_ok = ?, last_error = '', last_count = ?, last_note = ? WHERE kind = ?",
                           (t, t, count, note[:500], kind))
