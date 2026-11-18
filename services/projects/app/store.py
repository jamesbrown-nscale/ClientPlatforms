"""Where projects are kept: one SQLite file (DATABASE_PATH, /data/projects.db in the container).

    projects  one row per project (name, description, status, currency, approved budget)
    members   who can see each project, and as what: Owner, Editor or Viewer
    items     every record in every section, as JSON, with its reference number (RSK-001 …)
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS members (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    email TEXT NOT NULL, name TEXT, role TEXT NOT NULL, added_by TEXT, added_at TEXT,
                    PRIMARY KEY (project_id, email));
                CREATE INDEX IF NOT EXISTS members_email ON members(email);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    module TEXT NOT NULL, ref TEXT, data TEXT NOT NULL, source TEXT,
                    created_at TEXT, created_by TEXT, updated_at TEXT, updated_by TEXT);
                CREATE INDEX IF NOT EXISTS items_project ON items(project_id, module);
            """)

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    # ----- projects -----

    def create_project(self, doc: dict, owner_email: str, owner_name: str) -> dict:
        pid = uuid.uuid4().hex
        doc = {**doc, "id": pid, "createdAt": now(), "updatedAt": now(), "createdBy": owner_email}
        with self.lock, self.db() as db:
            db.execute("INSERT INTO projects (id, doc) VALUES (?, ?)", (pid, json.dumps(doc)))
            db.execute("INSERT INTO members (project_id, email, name, role, added_by, added_at) VALUES (?, ?, ?, 'Owner', ?, ?)",
                       (pid, owner_email, owner_name, owner_email, now()))
        return doc

    def project(self, pid: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT doc FROM projects WHERE id = ?", (pid,)).fetchone()
            return json.loads(row["doc"]) if row else None

    def projects_for(self, email: str, every: bool = False) -> list[dict]:
        """The projects this person is a member of (or every project), with their role (None if not a member)."""
        join = "LEFT JOIN" if every else "JOIN"
        with self.db() as db:
            rows = db.execute(f"SELECT p.doc, m.role FROM projects p {join} members m "
                              "ON m.project_id = p.id AND m.email = ?", (email,)).fetchall()
            return [{**json.loads(r["doc"]), "role": r["role"]} for r in rows]

    def save_project(self, doc: dict) -> dict:
        doc = {**doc, "updatedAt": now()}
        with self.lock, self.db() as db:
            db.execute("UPDATE projects SET doc = ? WHERE id = ?", (json.dumps(doc), doc["id"]))
        return doc

    def delete_project(self, pid: str) -> bool:
        with self.lock, self.db() as db:
            return db.execute("DELETE FROM projects WHERE id = ?", (pid,)).rowcount > 0

    # ----- members -----

    def role(self, pid: str, email: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT role FROM members WHERE project_id = ? AND email = ?", (pid, email)).fetchone()
            return row["role"] if row else None

    def members(self, pid: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT email, name, role, added_by, added_at FROM members WHERE project_id = ? "
                              "ORDER BY CASE role WHEN 'Owner' THEN 0 WHEN 'Editor' THEN 1 ELSE 2 END, email",
                              (pid,)).fetchall()
            return [{"email": r["email"], "name": r["name"] or "", "role": r["role"], "addedBy": r["added_by"],
                     "addedAt": r["added_at"]} for r in rows]

    def set_member(self, pid: str, email: str, name: str, role: str, by: str) -> None:
        with self.lock, self.db() as db:
            db.execute("INSERT INTO members (project_id, email, name, role, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(project_id, email) DO UPDATE SET role = excluded.role, "
                       "name = COALESCE(NULLIF(excluded.name, ''), members.name)",
                       (pid, email, name, role, by, now()))

    def remove_member(self, pid: str, email: str) -> bool:
        with self.lock, self.db() as db:
            return db.execute("DELETE FROM members WHERE project_id = ? AND email = ?", (pid, email)).rowcount > 0

    # ----- items -----

    @staticmethod
    def _item(r) -> dict:
        return {"id": r["id"], "module": r["module"], "ref": r["ref"] or "", "data": json.loads(r["data"]),
                "source": json.loads(r["source"]) if r["source"] else None,
                "createdAt": r["created_at"], "createdBy": r["created_by"], "updatedAt": r["updated_at"],
                "updatedBy": r["updated_by"]}

    def items(self, pid: str, module: str | None = None) -> list[dict]:
        with self.db() as db:
            if module:
                rows = db.execute("SELECT * FROM items WHERE project_id = ? AND module = ? ORDER BY created_at, rowid",
                                  (pid, module)).fetchall()
            else:
                rows = db.execute("SELECT * FROM items WHERE project_id = ? ORDER BY created_at, rowid", (pid,)).fetchall()
            return [self._item(r) for r in rows]

    def item(self, pid: str, iid: str) -> dict | None:
        with self.db() as db:
            r = db.execute("SELECT * FROM items WHERE project_id = ? AND id = ?", (pid, iid)).fetchone()
            return self._item(r) if r else None

    def refs(self, pid: str, module: str) -> list[str]:
        with self.db() as db:
            return [r["ref"] for r in db.execute("SELECT ref FROM items WHERE project_id = ? AND module = ?", (pid, module))]

    def add_item(self, pid: str, module: str, ref: str, data: dict, by: str, source: dict | None = None) -> dict:
        iid = uuid.uuid4().hex
        t = now()
        with self.lock, self.db() as db:
            db.execute("INSERT INTO items (id, project_id, module, ref, data, source, created_at, created_by, updated_at, updated_by) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (iid, pid, module, ref, json.dumps(data), json.dumps(source) if source else None, t, by, t, by))
        return self.item(pid, iid)

    def update_item(self, pid: str, iid: str, data: dict, by: str) -> dict:
        with self.lock, self.db() as db:
            db.execute("UPDATE items SET data = ?, updated_at = ?, updated_by = ? WHERE project_id = ? AND id = ?",
                       (json.dumps(data), now(), by, pid, iid))
        return self.item(pid, iid)

    def delete_item(self, pid: str, iid: str) -> bool:
        with self.lock, self.db() as db:
            return db.execute("DELETE FROM items WHERE project_id = ? AND id = ?", (pid, iid)).rowcount > 0

    def items_from(self, pid: str, meeting_id: str) -> list[dict]:
        """Action Log records made from a meeting's actions."""
        return [i for i in self.items(pid, "actions") if (i["source"] or {}).get("meeting") == meeting_id]
