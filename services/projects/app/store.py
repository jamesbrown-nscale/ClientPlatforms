"""Where projects are kept: one SQLite file (DATABASE_PATH, /data/projects.db in the container).

    projects  one row per project (name, description, status, currency, approved budget)
    members   who can see each project, and as what: Owner, Editor or Viewer
    items     every record in every section, as JSON, with its reference number (RSK-001 …)
    item_files   files kept with a record, every version (files.py; the Document Library)
    tasks, task_activity, pipeline_jobs   Team Planning (tasks.py; once called Tasks)
    team_*    the Team tool (team.py): people, responsibilities, 1-2-1s, feedback, manager notes
    notifications   each person's own notifications (inbox.py)
    change_*  Change management (changes.py): settings and flows, change requests, their activity
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .recycle import Bin


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
                CREATE TABLE IF NOT EXISTS item_files (
                    id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, data BLOB NOT NULL,
                    note TEXT DEFAULT '', created_at TEXT, created_by TEXT);
                CREATE INDEX IF NOT EXISTS item_files_item ON item_files(item_id, version);
            """)
            # Team Planning (see tasks.py), in the same database so a plan can belong to a project.
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, ref TEXT, title TEXT NOT NULL, description TEXT DEFAULT '',
                status TEXT NOT NULL, priority TEXT NOT NULL, assignee TEXT DEFAULT '', assignee_name TEXT DEFAULT '',
                due TEXT DEFAULT '', progress INTEGER, project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                created_by TEXT NOT NULL, created_by_name TEXT, created_at TEXT, updated_at TEXT, done_at TEXT);
            CREATE INDEX IF NOT EXISTS tasks_assignee ON tasks(assignee);
            CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id);
            CREATE TABLE IF NOT EXISTS task_activity (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, author TEXT, author_name TEXT, body TEXT DEFAULT '', progress INTEGER,
                waiting_on TEXT, answers TEXT, answered_at TEXT, answered_by TEXT, mentions TEXT DEFAULT '[]',
                data TEXT, created_at TEXT);
            CREATE INDEX IF NOT EXISTS activity_task ON task_activity(task_id, created_at);
            CREATE INDEX IF NOT EXISTS activity_waiting ON task_activity(waiting_on);
            CREATE TABLE IF NOT EXISTS team_leave (
                id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT DEFAULT '', start TEXT NOT NULL, end TEXT NOT NULL,
                note TEXT DEFAULT '', created_by TEXT, created_at TEXT, updated_at TEXT);
            CREATE INDEX IF NOT EXISTS leave_when ON team_leave(end, start);
            CREATE TABLE IF NOT EXISTS regular_work (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, duration REAL DEFAULT 1, recur TEXT DEFAULT '',
                description TEXT DEFAULT '', note TEXT DEFAULT '', created_by TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS regular_slots (
                id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES regular_work(id) ON DELETE CASCADE,
                email TEXT NOT NULL, name TEXT DEFAULT '', start TEXT NOT NULL, duration REAL DEFAULT 1, recur TEXT DEFAULT '',
                recur_until TEXT DEFAULT '', note TEXT DEFAULT '', created_by TEXT, created_at TEXT, updated_at TEXT);
            CREATE INDEX IF NOT EXISTS regular_slots_work ON regular_slots(work_id);
            CREATE TABLE IF NOT EXISTS pipeline_jobs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, position INTEGER DEFAULT 0,
                title TEXT NOT NULL, description TEXT DEFAULT '', note TEXT DEFAULT '', start TEXT DEFAULT '',
                duration INTEGER DEFAULT 1, assignee TEXT DEFAULT '', assignee_name TEXT DEFAULT '', status TEXT NOT NULL,
                created_by TEXT, created_at TEXT, updated_at TEXT, progress INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_task ON pipeline_jobs(task_id, position);
            CREATE INDEX IF NOT EXISTS jobs_start ON pipeline_jobs(start);
            """)
            self._add_columns(db, "pipeline_jobs", {"progress": "INTEGER DEFAULT 0",   # % complete, while In progress
                                                    "people": "TEXT DEFAULT '[]'",     # everyone on the job: [{email, name}]
                                                    "subs": "TEXT DEFAULT '[]'",       # its checklist: [{id, title, email, name, done}]
                                                    "recur": "TEXT DEFAULT ''",        # repeats: weekly, biweekly, monthly, quarterly, yearly
                                                    "recur_until": "TEXT DEFAULT ''",  # last day it can repeat on ('' = no end)
                                                    "reusable": "INTEGER DEFAULT 0",   # offered as a starting point when adding a job
                                                    "priority": "INTEGER"})            # 1 (most important) to 99; NULL = none
            self._add_columns(db, "regular_work", {"saved": "INTEGER DEFAULT 1"})   # in the + menu to add again (0: a one-off)
            # Jobs from before a job could have several people: their one assignee becomes the list.
            db.execute("UPDATE pipeline_jobs SET people = json_array(json_object('email', assignee, 'name', assignee_name)) "
                       "WHERE assignee != '' AND (people IS NULL OR people = '[]')")
            # Team (see team.py).
            db.executescript("""
            CREATE TABLE IF NOT EXISTS team_members (
                email TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT DEFAULT '', team TEXT DEFAULT '', manager TEXT DEFAULT '',
                start_date TEXT DEFAULT '', location TEXT DEFAULT '', about TEXT DEFAULT '',
                created_at TEXT, updated_at TEXT, updated_by TEXT);
            CREATE INDEX IF NOT EXISTS team_manager ON team_members(manager);
            CREATE TABLE IF NOT EXISTS team_responsibilities (
                id TEXT PRIMARY KEY, member TEXT NOT NULL REFERENCES team_members(email) ON DELETE CASCADE ON UPDATE CASCADE,
                title TEXT NOT NULL, detail TEXT DEFAULT '', position INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS team_entries (
                id TEXT PRIMARY KEY, member TEXT NOT NULL REFERENCES team_members(email) ON DELETE CASCADE ON UPDATE CASCADE,
                kind TEXT NOT NULL, date TEXT, body TEXT DEFAULT '', data TEXT,
                author TEXT, author_name TEXT, created_at TEXT, updated_at TEXT);
            CREATE INDEX IF NOT EXISTS team_entries_member ON team_entries(member, kind, date);
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, recipient TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT DEFAULT '',
                link TEXT DEFAULT '', actor TEXT, actor_name TEXT, created_at TEXT, read_at TEXT);
            CREATE INDEX IF NOT EXISTS notifications_recipient ON notifications(recipient, read_at, created_at);
            CREATE TABLE IF NOT EXISTS change_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT, updated_by TEXT);
            CREATE TABLE IF NOT EXISTS changes (
                id TEXT PRIMARY KEY, ref TEXT, title TEXT NOT NULL, flow TEXT NOT NULL, state TEXT NOT NULL, step INTEGER DEFAULT 0,
                step_started_at TEXT, started INTEGER DEFAULT 0, fields TEXT NOT NULL, requestor TEXT NOT NULL, requestor_name TEXT,
                created_at TEXT, updated_at TEXT, submitted_at TEXT, closed_at TEXT);
            CREATE INDEX IF NOT EXISTS changes_requestor ON changes(requestor);
            CREATE TABLE IF NOT EXISTS change_activity (
                id TEXT PRIMARY KEY, change_id TEXT NOT NULL REFERENCES changes(id) ON DELETE CASCADE, kind TEXT NOT NULL,
                author TEXT, author_name TEXT, body TEXT DEFAULT '', step INTEGER, mentions TEXT DEFAULT '[]', data TEXT, created_at TEXT);
            CREATE INDEX IF NOT EXISTS change_activity_change ON change_activity(change_id, created_at);
            """)
            self._add_columns(db, "team_members", {"department": "TEXT DEFAULT ''"})   # team is their sub team (Division) within it
        self.bin = Bin(self.db, self.lock)   # deleted things, kept 30 days (recycle.py; Settings → Recycle bin)

    @staticmethod
    def _add_columns(db, table: str, cols: dict) -> None:
        """Columns added since a table was first made (CREATE TABLE IF NOT EXISTS leaves older tables as they were)."""
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols.items():
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

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

    # ----- files kept with a record (each save a new version) -----

    @staticmethod
    def _file(r) -> dict:
        return {"id": r["id"], "version": r["version"], "name": r["name"], "size": r["size"], "note": r["note"] or "",
                "createdAt": r["created_at"], "createdBy": r["created_by"]}

    def add_file(self, iid: str, name: str, data: bytes, by: str, note: str = "", keep: int = 20) -> dict:
        with self.lock, self.db() as db:
            v = (db.execute("SELECT MAX(version) FROM item_files WHERE item_id = ?", (iid,)).fetchone()[0] or 0) + 1
            fid = uuid.uuid4().hex
            db.execute("INSERT INTO item_files (id, item_id, version, name, size, data, note, created_at, created_by) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (fid, iid, v, name, len(data), data, note, now(), by))
            db.execute("DELETE FROM item_files WHERE item_id = ? AND version <= ?", (iid, v - keep))
            return self._file(db.execute("SELECT * FROM item_files WHERE id = ?", (fid,)).fetchone())

    def files(self, iid: str) -> list[dict]:
        with self.db() as db:
            return [self._file(r) for r in db.execute("SELECT id, item_id, version, name, size, note, created_at, created_by "
                                                      "FROM item_files WHERE item_id = ? ORDER BY version DESC", (iid,))]

    def file_data(self, iid: str, version: int | None = None) -> tuple[dict, bytes] | None:
        with self.db() as db:
            r = db.execute("SELECT * FROM item_files WHERE item_id = ? AND version = ?", (iid, version)).fetchone() if version else \
                db.execute("SELECT * FROM item_files WHERE item_id = ? ORDER BY version DESC LIMIT 1", (iid,)).fetchone()
            return (self._file(r), bytes(r["data"])) if r else None

    def drop_files(self, iid: str) -> None:
        with self.lock, self.db() as db:
            db.execute("DELETE FROM item_files WHERE item_id = ?", (iid,))

    def items_from(self, pid: str, meeting_id: str) -> list[dict]:
        """Action Log records made from a meeting's actions."""
        return [i for i in self.items(pid, "actions") if (i["source"] or {}).get("meeting") == meeting_id]
