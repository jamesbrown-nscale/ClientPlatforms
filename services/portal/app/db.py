"""The portal's own data: people who can sign in, their sessions, and the site settings.

Kept in one SQLite file (DATABASE_PATH, /data/portal.db in the container), on a Docker volume.

Passwords are stored as scrypt hashes. Sessions are random tokens; only their SHA-256 is stored, so
the database alone can't be used to sign in as anyone.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

TOOLS = [   # every tool on the site; the id is its path (/notifications/ etc.) and its access entry
    {"id": "notifications", "name": "IT Notifications", "icon": "bell", "url": "/notifications/",
     "about": "Send branded notifications to Windows and Mac devices, and see who has read them."},
    {"id": "elevation", "name": "Admin elevation log", "icon": "shield", "url": "/elevation/",
     "about": "Every Windows admin elevation request: who, which device, why, and how it ended."},
    {"id": "restart", "name": "Restart reminder", "icon": "restart", "url": "/restart/",
     "about": "Which Windows devices have gone too long without a restart, who put it off, and when they restarted."},
    {"id": "projects", "name": "Projects", "icon": "projects", "url": "/projects/",
     "about": "Small project workspaces: status reports, timeline, RAID, actions, decisions, budget and closure."},
]
TOOL_IDS = [t["id"] for t in TOOLS]


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Passwords ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        kind, salt, h = (stored or "").split("$")
    except ValueError:
        kind, salt, h = "", "00", "00"
    # Always do the work, so an unknown email takes as long as a wrong password.
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt) or b"x", n=2 ** 14, r=8, p=1, dklen=32)
    return kind == "scrypt" and hmac.compare_digest(got.hex(), h)


# ---------- Database ----------

class DB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY, name TEXT NOT NULL, password TEXT NOT NULL,
                    admin INTEGER NOT NULL DEFAULT 0, tools TEXT NOT NULL DEFAULT '[]',
                    disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT, last_sign_in TEXT);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_email ON sessions(email);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ----- people -----

    @staticmethod
    def _user(row) -> dict | None:
        if not row:
            return None
        return {"email": row["email"], "name": row["name"], "admin": bool(row["admin"]),
                "tools": [t for t in json.loads(row["tools"] or "[]") if t in TOOL_IDS],
                "disabled": bool(row["disabled"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "lastSignIn": row["last_sign_in"], "_password": row["password"]}

    @staticmethod
    def public(u: dict) -> dict:
        return {k: v for k, v in u.items() if not k.startswith("_")}

    def check(self, password: str, user: dict | None) -> bool:
        """True if this is the password of this account and it's switched on."""
        ok = check_password(password, user["_password"] if user else None)
        return ok and bool(user) and not user["disabled"]

    def count_users(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_admins(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users WHERE admin = 1 AND disabled = 0").fetchone()[0]

    def get_user(self, email: str) -> dict | None:
        with self._db() as db:
            return self._user(db.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone())

    def list_users(self) -> list[dict]:
        with self._db() as db:
            return [self._user(r) for r in db.execute("SELECT * FROM users ORDER BY admin DESC, email").fetchall()]

    def create_user(self, email: str, name: str, password: str, admin: bool, tools: list[str]) -> dict:
        t = iso(now())
        with self._lock, self._db() as db:
            db.execute("INSERT INTO users (email, name, password, admin, tools, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (email.lower(), name, hash_password(password), int(admin),
                        json.dumps([x for x in tools if x in TOOL_IDS]), t, t))
        return self.get_user(email)

    def update_user(self, email: str, **changes) -> dict | None:
        cols, vals = [], []
        for k in ("name", "admin", "tools", "disabled"):
            if k in changes and changes[k] is not None:
                v = changes[k]
                cols.append(f"{k} = ?")
                vals.append(json.dumps([x for x in v if x in TOOL_IDS]) if k == "tools" else
                            int(v) if isinstance(v, bool) else v)
        if cols:
            with self._lock, self._db() as db:
                db.execute(f"UPDATE users SET {', '.join(cols)}, updated_at = ? WHERE email = ?",
                           (*vals, iso(now()), email.lower()))
        if changes.get("disabled"):
            self.end_sessions(email)
        return self.get_user(email)

    def set_password(self, email: str, password: str, keep_session: str | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE users SET password = ?, updated_at = ? WHERE email = ?",
                       (hash_password(password), iso(now()), email.lower()))
        self.end_sessions(email, keep=keep_session)

    def delete_user(self, email: str) -> bool:
        self.end_sessions(email)
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM users WHERE email = ?", (email.lower(),)).rowcount > 0

    def signed_in(self, email: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE users SET last_sign_in = ? WHERE email = ?", (iso(now()), email.lower()))

    # ----- sessions -----

    @staticmethod
    def _sid(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def start_session(self, email: str, hours: float) -> str:
        token = secrets.token_urlsafe(32)
        t = now()
        with self._lock, self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(t),))
            db.execute("INSERT INTO sessions (id, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (self._sid(token), email.lower(), iso(t), iso(t + timedelta(hours=hours))))
        return token

    def session_user(self, token: str) -> dict | None:
        """The person signed in with this session token, if it's current and they're still allowed in."""
        if not token:
            return None
        with self._db() as db:
            row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.email = s.email "
                             "WHERE s.id = ? AND s.expires_at > ? AND u.disabled = 0",
                             (self._sid(token), iso(now()))).fetchone()
        return self._user(row)

    def session_id(self, token: str) -> str:
        return self._sid(token) if token else ""

    def end_session(self, token: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (self._sid(token),))

    def end_sessions(self, email: str, keep: str | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM sessions WHERE email = ? AND id != ?", (email.lower(), keep or ""))

    # ----- settings -----

    def settings(self) -> dict:
        with self._db() as db:
            stored = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key, value FROM settings")}
        tools = stored.get("tools") or {}
        return {
            "publicUrl": stored.get("publicUrl") or os.environ.get("PUBLIC_URL", ""),
            "deviceUrl": stored.get("deviceUrl") or os.environ.get("DEVICE_URL", ""),
            "tools": [{**t, "url": (tools.get(t["id"]) or {}).get("url") or t["url"],
                       "defaultUrl": t["url"],
                       "enabled": (tools.get(t["id"]) or {}).get("enabled", True)} for t in TOOLS],
        }

    def save_settings(self, values: dict) -> None:
        with self._lock, self._db() as db:
            for k, v in values.items():
                db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
