"""The portal's own data: people who can sign in, their roles, their sessions, and the site settings.

Access comes from roles. A role sets each tool to none, view, edit or admin, and can grant the site
abilities (managing people and roles, managing site settings). Someone with several roles gets the
highest level each grants. The built-in Administrator role has admin on every tool, including ones
added later, and every site ability; it can't be changed or deleted.

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

TOOLS = [   # every tool on the site; the id is its path (/notifications/ etc.) and its access entry.
    # group is the heading it's listed under in the sidebar.
    {"id": "notifications", "group": "Device Management", "name": "IT Notifications", "icon": "bell", "url": "/notifications/",
     "about": "Send branded notifications to Windows and Mac devices, and see who has read them."},
    {"id": "elevation", "group": "Device Management", "name": "Admin elevation log", "icon": "shield", "url": "/elevation/",
     "about": "Every Windows admin elevation request: who, which device, why, and how it ended."},
    {"id": "changes", "group": "Device Management", "name": "Change management", "icon": "changes", "url": "/changes/",
     "about": "Raise, review and approve changes to devices and services, through the right flow for each change type."},
    {"id": "restart", "group": "Device Management", "name": "Restart reminder", "icon": "restart", "url": "/restart/",
     "about": "Which Windows devices have gone too long without a restart, who put it off, and when they restarted."},
    {"id": "projects", "group": "Project Management", "name": "Projects", "icon": "projects", "url": "/projects/",
     "about": "Small project workspaces: status reports, timeline, RAID, actions, decisions, budget and closure."},
    {"id": "tasks", "group": "Project Management", "name": "Team Planning", "icon": "tasks", "url": "/tasks/",
     "about": "Team plans of scheduled jobs: assign work, plan start dates and durations on a timeline, ask questions and post updates."},
    {"id": "team", "group": "People", "name": "Team", "icon": "team", "url": "/team/",
     "about": "Who's on the team, their roles and responsibilities, projects and activity, 1-2-1s and feedback."},
]
TOOL_IDS = [t["id"] for t in TOOLS]
LEVELS = ["none", "view", "edit", "admin"]
SITE_PERMS = {"people": "Manage people and roles", "settings": "Manage site settings"}
ADMIN_ROLE = "administrator"


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
                CREATE TABLE IF NOT EXISTS roles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', levels TEXT NOT NULL DEFAULT '{}',
                    perms TEXT NOT NULL DEFAULT '[]', system INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS user_roles (email TEXT NOT NULL, role_id TEXT NOT NULL, PRIMARY KEY (email, role_id));
            """)
        self._migrate()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _migrate(self) -> None:
        """First run with roles: the Administrator role for admins, and for everyone else an editor role
        per tool they had ticked, so nobody gains or loses anything."""
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM roles WHERE id = ?", (ADMIN_ROLE,)).fetchone():
                return
            t = iso(now())
            db.execute("INSERT INTO roles (id, name, description, levels, perms, system, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                       (ADMIN_ROLE, "Administrator", "Every tool, with admin rights, and every setting.", "{}",
                        json.dumps(list(SITE_PERMS)), t, t))
            names = {x["id"]: x["name"] for x in TOOLS}
            for r in db.execute("SELECT email, admin, tools FROM users").fetchall():
                if r["admin"]:
                    db.execute("INSERT OR IGNORE INTO user_roles (email, role_id) VALUES (?, ?)", (r["email"], ADMIN_ROLE))
                    continue
                for tool in json.loads(r["tools"] or "[]"):
                    if tool not in names:
                        continue
                    rid = f"{tool}-editor"
                    db.execute("INSERT OR IGNORE INTO roles (id, name, description, levels, perms, system, created_at, updated_at) "
                               "VALUES (?, ?, ?, ?, '[]', 0, ?, ?)",
                               (rid, f"{names[tool]} editor", f"Uses {names[tool]}.", json.dumps({tool: "edit"}), t, t))
                    db.execute("INSERT OR IGNORE INTO user_roles (email, role_id) VALUES (?, ?)", (r["email"], rid))

    # ----- roles -----

    @staticmethod
    def _role(r) -> dict:
        system = bool(r["system"])
        levels = {t: "admin" for t in TOOL_IDS} if r["id"] == ADMIN_ROLE else \
            {t: lv for t, lv in json.loads(r["levels"] or "{}").items() if t in TOOL_IDS and lv in LEVELS and lv != "none"}
        perms = list(SITE_PERMS) if r["id"] == ADMIN_ROLE else [p for p in json.loads(r["perms"] or "[]") if p in SITE_PERMS]
        return {"id": r["id"], "name": r["name"], "description": r["description"] or "", "levels": levels, "perms": perms,
                "system": system, "createdAt": r["created_at"], "updatedAt": r["updated_at"]}

    def roles(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM roles ORDER BY system DESC, name COLLATE NOCASE").fetchall()
            counts = {r["role_id"]: r["n"] for r in db.execute(
                "SELECT ur.role_id, COUNT(*) AS n FROM user_roles ur JOIN users u ON u.email = ur.email GROUP BY ur.role_id")}
        return [{**self._role(r), "members": counts.get(r["id"], 0)} for r in rows]

    def role(self, rid: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM roles WHERE id = ?", (rid,)).fetchone()
        return self._role(r) if r else None

    def save_role(self, rid: str | None, name: str, description: str, levels: dict, perms: list) -> dict:
        t = iso(now())
        levels = {k: v for k, v in levels.items() if k in TOOL_IDS and v in LEVELS and v != "none"}
        perms = [p for p in perms if p in SITE_PERMS]
        with self._lock, self._db() as db:
            if rid is None:
                base = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:40] or "role"
                rid, n = base, 2
                while db.execute("SELECT 1 FROM roles WHERE id = ?", (rid,)).fetchone():
                    rid, n = f"{base}-{n}", n + 1
                db.execute("INSERT INTO roles (id, name, description, levels, perms, system, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                           (rid, name, description, json.dumps(levels), json.dumps(perms), t, t))
            else:
                db.execute("UPDATE roles SET name = ?, description = ?, levels = ?, perms = ?, updated_at = ? WHERE id = ? AND system = 0",
                           (name, description, json.dumps(levels), json.dumps(perms), t, rid))
        return self.role(rid)

    def delete_role(self, rid: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM user_roles WHERE role_id = ?", (rid,))
            db.execute("DELETE FROM roles WHERE id = ? AND system = 0", (rid,))

    def role_members(self, rid: str) -> list[str]:
        with self._db() as db:
            return [r["email"] for r in db.execute("SELECT email FROM user_roles WHERE role_id = ? ORDER BY email", (rid,))]

    # ----- people -----

    def _effective(self, role_ids: list[str], roles: dict) -> dict:
        levels, perms = {}, set()
        for rid in role_ids:
            r = roles.get(rid)
            if not r:
                continue
            perms |= set(r["perms"])
            for t, lv in r["levels"].items():
                if LEVELS.index(lv) > LEVELS.index(levels.get(t, "none")):
                    levels[t] = lv
        return {"levels": levels, "perms": sorted(perms), "tools": [t for t in TOOL_IDS if t in levels],
                "admin": all(p in perms for p in SITE_PERMS)}

    def _users(self, where: str = "", args: tuple = ()) -> list[dict]:
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM users {where}", args).fetchall()
            ur = {}
            for r in db.execute("SELECT email, role_id FROM user_roles"):
                ur.setdefault(r["email"], []).append(r["role_id"])
            roles = {r["id"]: self._role(r) for r in db.execute("SELECT * FROM roles")}
        out = []
        for row in rows:
            ids = [i for i in ur.get(row["email"], []) if i in roles]
            out.append({"email": row["email"], "name": row["name"], "roles": ids, **self._effective(ids, roles),
                        "disabled": bool(row["disabled"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                        "lastSignIn": row["last_sign_in"], "_password": row["password"]})
        return out

    def _user(self, row) -> dict | None:
        if not row:
            return None
        found = self._users("WHERE email = ?", (row["email"],))
        return found[0] if found else None

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

    def count_people_managers(self, without: str | None = None, roles_for: tuple | None = None) -> int:
        """How many switched-on people can manage people and roles (optionally as if `without` were
        turned off, or as if `roles_for` = (email, role ids) were their roles)."""
        n = 0
        roles = {r["id"]: r for r in self.roles()}
        for u in self._users("WHERE disabled = 0"):
            if u["email"] == without:
                continue
            ids = roles_for[1] if roles_for and roles_for[0] == u["email"] else u["roles"]
            if "people" in self._effective(ids, roles)["perms"]:
                n += 1
        return n

    def get_user(self, email: str) -> dict | None:
        found = self._users("WHERE email = ?", (email.lower(),))
        return found[0] if found else None

    def list_users(self) -> list[dict]:
        return sorted(self._users(), key=lambda u: (not u["admin"], u["email"]))

    def create_user(self, email: str, name: str, password: str, roles: list[str]) -> dict:
        t = iso(now())
        with self._lock, self._db() as db:
            db.execute("INSERT INTO users (email, name, password, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                       (email.lower(), name, hash_password(password), t, t))
        self.set_roles(email, roles)
        return self.get_user(email)

    def set_roles(self, email: str, roles: list[str]) -> None:
        with self._lock, self._db() as db:
            known = {r["id"] for r in db.execute("SELECT id FROM roles")}
            db.execute("DELETE FROM user_roles WHERE email = ?", (email.lower(),))
            for rid in dict.fromkeys(roles):
                if rid in known:
                    db.execute("INSERT INTO user_roles (email, role_id) VALUES (?, ?)", (email.lower(), rid))

    def update_user(self, email: str, **changes) -> dict | None:
        cols, vals = [], []
        for k in ("name", "disabled"):
            if k in changes and changes[k] is not None:
                cols.append(f"{k} = ?")
                vals.append(int(changes[k]) if isinstance(changes[k], bool) else changes[k])
        if cols:
            with self._lock, self._db() as db:
                db.execute(f"UPDATE users SET {', '.join(cols)}, updated_at = ? WHERE email = ?",
                           (*vals, iso(now()), email.lower()))
        if changes.get("roles") is not None:
            self.set_roles(email, changes["roles"])
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
            db.execute("DELETE FROM user_roles WHERE email = ?", (email.lower(),))
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
