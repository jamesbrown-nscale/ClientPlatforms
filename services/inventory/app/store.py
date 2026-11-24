"""Where Users, Devices and the Jamf / Intune connections are kept: one SQLite file (DATABASE_PATH,
/data/inventory.db in the container).

    devices      every device from Jamf and Intune, as of the last sync (id = "jamf:<id>" / "intune:<id>")
    email_links  "devices under this email are this user's": made on Users → Mismatches, kept across syncs
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
import unicodedata
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from .recycle import Bin

DEVICE_FIELDS = ("platform", "kind", "name", "make", "model", "os", "os_version", "serial", "user_email", "user_name", "last_seen")
USER_FIELDS = ("name", "department", "division", "manager", "user_type", "primary_device", "title", "platform_tag", "location")


# Letters from other alphabets that look exactly like Latin ones: an email typed or pasted with one
# (a Cyrillic "о" in "anthоny") looks the same on screen but doesn't match. Cyrillic, then Greek.
LOOKALIKES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "һ": "h", "і": "i", "ї": "i", "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "ѕ": "s", "т": "t", "у": "y", "х": "x", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "ɡ": "g", "ı": "i",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
})


def clean_email(v: str) -> str:
    """An email as we compare it: lower case, plain Latin letters, no spaces or invisible characters.
    HR exports and Jamf / Intune fields sometimes carry a zero-width or non-breaking space, a direction
    mark, a letter from another alphabet that looks Latin, or an accent (josé@ for jose@): each looks
    the same, or nearly, on screen but stops two emails matching."""
    v = unicodedata.normalize("NFKD", v or "").lower()
    v = "".join(ch for ch in v if not unicodedata.combining(ch)).translate(LOOKALIKES)
    return "".join(ch for ch in v if not ch.isspace() and unicodedata.category(ch)[0] not in ("C", "Z"))


def device_email(user_email: str, user_name: str) -> str:
    """Whose a device is, as an email: Jamf or Intune's email, or, when that's empty, the user name if
    it is one (Jamf often puts the address in Username and leaves Email empty)."""
    e = clean_email(user_email or "")
    if not e and "@" in (user_name or "") and " " not in (user_name or "").strip():
        e = clean_email(user_name)
    return e


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")



class Store:
    def __init__(self, path: str, secret: str = "", prefix: str = "", tool: str = "users", kind: str = "User (FTE)", lock=None):
        """prefix: whose list of users this is ("" for Users, "ct_" for Users - CT). Every list has its
        own users, review and email links tables in the same file; devices and connections are shared."""
        self.path, self.secret_ = path, secret
        self.prefix, self.tool, self.kind = prefix, tool, kind
        self.U, self.R, self.L = prefix + "users", prefix + "user_review", prefix + "email_links"
        self._lock = lock or threading.Lock()
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
                CREATE TABLE IF NOT EXISTS connections (
                    kind TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}', secret TEXT DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT, last_ok TEXT, last_error TEXT DEFAULT '',
                    last_count INTEGER, last_note TEXT DEFAULT '', updated_at TEXT, updated_by TEXT DEFAULT '');
            """)
            U, R, L = self.U, self.R, self.L
            db.executescript(f"""
                CREATE TABLE IF NOT EXISTS {U} (
                    email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', team TEXT DEFAULT '', manager TEXT DEFAULT '',
                    user_type TEXT DEFAULT 'Standard', primary_device TEXT DEFAULT '', title TEXT DEFAULT '',
                    department TEXT DEFAULT '', created_at TEXT, updated_at TEXT, updated_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS {R} (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL, name TEXT DEFAULT '',
                    fields TEXT NOT NULL DEFAULT '{{}}', changes TEXT NOT NULL DEFAULT '[]', created_at TEXT, created_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS {L} (
                    from_email TEXT PRIMARY KEY, to_email TEXT NOT NULL, created_at TEXT, created_by TEXT DEFAULT '');
            """)
            have = {r[1] for r in db.execute(f"PRAGMA table_info({U})")}
            if "division" not in have:
                # A user's team is now their Division (within their Department): the old free-text
                # Team tag moves across.
                db.execute(f"ALTER TABLE {U} ADD COLUMN division TEXT DEFAULT ''")
                db.execute(f"UPDATE {U} SET division = team WHERE COALESCE(team, '') != ''")
            if "platform_tag" not in have:   # Mac, Windows or Linux set by hand ("" = from their devices)
                db.execute(f"ALTER TABLE {U} ADD COLUMN platform_tag TEXT DEFAULT ''")
            if "location" not in have:   # where they work: an office, city or "Remote"
                db.execute(f"ALTER TABLE {U} ADD COLUMN location TEXT DEFAULT ''")
            self._clean_emails(db)
        self._siblings: dict[str, "Store"] = {}
        self.bin = Bin(self._db, self._lock)   # deleted users, kept 30 days (recycle.py; Settings → Recycle bin)

    def sibling(self, prefix: str, tool: str, kind: str) -> "Store":
        """Another list of users in the same file (sharing its devices and connections)."""
        if prefix not in self._siblings:
            self._siblings[prefix] = Store(self.path, self.secret_, prefix, tool, kind, self._lock)
        return self._siblings[prefix]

    def _clean_emails(self, db) -> None:
        """Emails saved before clean_email: tidied in place, so they match their devices. A tidied
        email that another user already has is left alone (both show; delete the one you don't want)."""
        for r in db.execute("SELECT id, user_email, user_name FROM devices").fetchall():
            if (c := device_email(r["user_email"], r["user_name"])) != r["user_email"]:
                db.execute("UPDATE devices SET user_email = ? WHERE id = ?", (c, r["id"]))
        for r in db.execute(f"SELECT email, manager FROM {self.U}").fetchall():
            e = clean_email(r["email"])
            if e != r["email"] and not db.execute(f"SELECT 1 FROM {self.U} WHERE email = ?", (e,)).fetchone():
                db.execute(f"UPDATE {self.U} SET email = ? WHERE email = ?", (e, r["email"]))
                db.execute(f"UPDATE {self.U} SET manager = ? WHERE manager = ?", (e, r["email"]))
            m = r["manager"] or ""
            if "@" in m and (c := clean_email(m)) != m:
                db.execute(f"UPDATE {self.U} SET manager = ? WHERE manager = ?", (c, m))
        for r in db.execute(f"SELECT from_email, to_email FROM {self.L}").fetchall():
            f, t = clean_email(r["from_email"]), clean_email(r["to_email"])
            if (f, t) != (r["from_email"], r["to_email"]):
                db.execute(f"UPDATE OR IGNORE {self.L} SET from_email = ?, to_email = ? WHERE from_email = ?", (f, t, r["from_email"]))

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
                d = {**d, "user_email": device_email(d.get("user_email") or "", d.get("user_name") or "")}
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
            return [dict(r) for r in db.execute(f"SELECT * FROM {self.U} ORDER BY name COLLATE NOCASE, email")]

    def user(self, email: str) -> dict | None:
        with self._db() as db:
            r = db.execute(f"SELECT * FROM {self.U} WHERE email = ?", (email,)).fetchone()
            return dict(r) if r else None

    def save_users(self, rows: list[dict], by: str) -> tuple[int, int]:
        """Adds or updates each {email, ...fields}; only the fields given change. (added, updated)"""
        t = now()
        added = updated = 0
        with self._lock, self._db() as db:
            for u in rows:
                old = db.execute(f"SELECT * FROM {self.U} WHERE email = ?", (u["email"],)).fetchone()
                fields = {f: u[f] for f in USER_FIELDS if f in u}
                if old is None:
                    full = {f: "" for f in USER_FIELDS} | {"user_type": "Standard"} | fields
                    db.execute(f"INSERT INTO {self.U} (email, {', '.join(USER_FIELDS)}, created_at, updated_at, updated_by) "
                               f"VALUES (?, {', '.join('?' * len(USER_FIELDS))}, ?, ?, ?)",
                               (u["email"], *[full[f] for f in USER_FIELDS], t, t, by))
                    added += 1
                elif any((old[f] or "") != (v or "") for f, v in fields.items()):
                    db.execute(f"UPDATE {self.U} SET {', '.join(f'{f} = ?' for f in fields)}, updated_at = ?, updated_by = ? WHERE email = ?",
                               (*fields.values(), t, by, u["email"]))
                    updated += 1
        return added, updated

    def delete_users(self, emails: list[str], by: str = "") -> int:
        """by: who deleted them from the page; they go into the recycle bin. (An import replacing them doesn't.)"""
        with self._lock, self._db() as db:
            n = 0
            for e in emails:
                if by:
                    r = db.execute(f"SELECT name FROM {self.U} WHERE email = ?", (e,)).fetchone()
                    if r:
                        self.bin.put(db, kind=self.kind, label=r["name"] or e, context=e, table=self.U, where={"email": e},
                                     by=by, tool=self.tool)
                n += db.execute(f"DELETE FROM {self.U} WHERE email = ?", (e,)).rowcount
            return n

    # ----- email links: devices under one email belong to a user with another (Users → Mismatches) -----

    def links(self) -> dict[str, str]:
        """The links in use. One from an email that is itself on this list is ignored: that person's
        devices are theirs (the link was made before they were added)."""
        with self._db() as db:
            return {r["from_email"]: r["to_email"] for r in db.execute(
                f"SELECT from_email, to_email FROM {self.L} WHERE from_email NOT IN (SELECT email FROM {self.U})")}

    def link_rows(self) -> list[dict]:
        """Every link, with ignored = 1 when its email is on this list itself (see links())."""
        with self._db() as db:
            return [dict(r) for r in db.execute(
                f"SELECT l.*, EXISTS (SELECT 1 FROM {self.U} u WHERE u.email = l.from_email) AS ignored FROM {self.L} l ORDER BY from_email")]

    def add_link(self, from_email: str, to_email: str, by: str) -> None:
        with self._lock, self._db() as db:
            db.execute(f"INSERT INTO {self.L} (from_email, to_email, created_at, created_by) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(from_email) DO UPDATE SET to_email = excluded.to_email, created_at = excluded.created_at, "
                       "created_by = excluded.created_by", (from_email, to_email, now(), by))

    def drop_link(self, from_email: str) -> bool:
        with self._lock, self._db() as db:
            return db.execute(f"DELETE FROM {self.L} WHERE from_email = ?", (from_email,)).rowcount > 0

    # ----- review: what a CSV import would change, waiting for someone to accept it -----
    #   new      in the file, not on Users          fields: everything the file gives them
    #   changed  on Users, the file differs         fields: just what changes; changes: [{field, from, to}]
    #   left     on Users, not in the file          (only when the file was said to be everyone)

    def review(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM {self.R} ORDER BY kind, name COLLATE NOCASE, email").fetchall()
        return [{**dict(r), "fields": json.loads(r["fields"]), "changes": json.loads(r["changes"])} for r in rows]

    def review_count(self) -> int:
        with self._db() as db:
            return db.execute(f"SELECT COUNT(*) FROM {self.R}").fetchone()[0]

    def replace_review(self, items: list[dict], by: str) -> None:
        """A new import replaces whatever was still waiting from the last one."""
        t = now()
        with self._lock, self._db() as db:
            db.execute(f"DELETE FROM {self.R}")
            db.executemany(f"INSERT INTO {self.R} (id, kind, email, name, fields, changes, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(i["id"], i["kind"], i["email"], i.get("name", ""), json.dumps(i.get("fields", {})),
                             json.dumps(i.get("changes", [])), t, by) for i in items])

    def drop_review(self, ids: list[str]) -> None:
        with self._lock, self._db() as db:
            db.executemany(f"DELETE FROM {self.R} WHERE id = ?", [(i,) for i in ids])

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
