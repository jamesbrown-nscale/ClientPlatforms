"""Where elevation records are kept: SQLite, at DATABASE_PATH (the service's Docker volume).

    elevations   the current record for each event, merged from every send
    history      every snapshot exactly as received, never changed

Each upsert reads the record, merges the new snapshot (see merge.py) and writes the record and a
history entry together, in one transaction.
"""
import json
import os
import sqlite3
import threading
from typing import Callable

Merge = Callable[[dict | None], tuple[dict, dict]]   # existing record -> (new record, history entry)


class Store:
    def upsert(self, event_id: str, merge: Merge) -> tuple[dict | None, dict]: ...
    def get(self, event_id: str) -> dict | None: ...
    def history(self, event_id: str) -> list[dict]: ...
    def list_since(self, since: str, limit: int) -> list[dict]: ...


def make_store() -> Store:
    return SqliteStore(os.environ.get("DATABASE_PATH", "elevation.db"))


class SqliteStore(Store):
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS elevations (
                    event_id TEXT PRIMARY KEY, requested_at TEXT, doc TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS elevations_requested ON elevations(requested_at);
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, doc TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS history_event ON history(event_id);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def upsert(self, event_id, merge):
        with self._lock, self._db() as db:
            row = db.execute("SELECT doc FROM elevations WHERE event_id = ?", (event_id,)).fetchone()
            old = json.loads(row["doc"]) if row else None
            new, entry = merge(old)
            db.execute("INSERT OR REPLACE INTO elevations (event_id, requested_at, doc) VALUES (?, ?, ?)",
                       (event_id, new.get("requestedAt") or "", json.dumps(new)))
            db.execute("INSERT INTO history (event_id, doc) VALUES (?, ?)", (event_id, json.dumps(entry)))
            return old, new

    def get(self, event_id):
        with self._db() as db:
            row = db.execute("SELECT doc FROM elevations WHERE event_id = ?", (event_id,)).fetchone()
            return json.loads(row["doc"]) if row else None

    def history(self, event_id):
        with self._db() as db:
            rows = db.execute("SELECT doc FROM history WHERE event_id = ? ORDER BY id", (event_id,)).fetchall()
            return [json.loads(r["doc"]) for r in rows]

    def list_since(self, since, limit):
        with self._db() as db:
            rows = db.execute("SELECT doc FROM elevations WHERE requested_at >= ? ORDER BY requested_at DESC LIMIT ?",
                              (since, limit)).fetchall()
            return [json.loads(r["doc"]) for r in rows]
