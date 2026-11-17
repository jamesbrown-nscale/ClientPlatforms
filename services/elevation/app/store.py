"""Where elevation records are kept.

    STORE=firestore   Google Cloud Firestore (Cloud Run). Database: FIRESTORE_DATABASE (elevation).
    STORE=sqlite      a local file, for development and tests (DATABASE_PATH).

Firestore layout:
    elevations/{eventId}                     the current record, merged from every send
    elevations/{eventId}/history/{autoId}    every snapshot exactly as received, never changed

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
    kind = os.environ.get("STORE", "sqlite").lower()
    if kind == "firestore":
        return FirestoreStore()
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


class FirestoreStore(Store):
    def __init__(self, client=None):
        if client is None:
            from google.cloud import firestore
            client = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE", "elevation"))
        self.db = client
        self.col = client.collection("elevations")

    def upsert(self, event_id, merge):
        from google.cloud import firestore
        ref = self.col.document(event_id)
        hist = ref.collection("history").document()

        @firestore.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            old = snap.to_dict() if snap.exists else None
            new, entry = merge(old)
            tx.set(ref, new)
            tx.set(hist, entry)
            return old, new

        return run(self.db.transaction())

    def get(self, event_id):
        snap = self.col.document(event_id).get()
        return snap.to_dict() if snap.exists else None

    def history(self, event_id):
        docs = self.col.document(event_id).collection("history").stream()
        return sorted((d.to_dict() for d in docs), key=lambda h: (h.get("receivedAt") or "", h.get("seq") or 0))

    def list_since(self, since, limit):
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = (self.col.where(filter=FieldFilter("requestedAt", ">=", since))
             .order_by("requestedAt", direction=firestore.Query.DESCENDING).limit(limit))
        return [d.to_dict() for d in q.stream()]
