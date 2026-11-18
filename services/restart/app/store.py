"""Where restart reports are kept: one SQLite file (DATABASE_PATH, /data/restart.db in the container).

    devices   one row per device (serial, or name if there's no serial): its latest state
    reports   every report as received, once each (by reportId)
    restarts  one row each time a device came back on a new boot
"""
import json
import sqlite3
import threading
from typing import Callable


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (key TEXT PRIMARY KEY, last_received TEXT, doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY, device_key TEXT NOT NULL, at TEXT, doc TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reports_device ON reports(device_key, at);
                CREATE TABLE IF NOT EXISTS restarts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, device_key TEXT NOT NULL, restarted_at TEXT, doc TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS restarts_at ON restarts(restarted_at);
                CREATE INDEX IF NOT EXISTS restarts_device ON restarts(device_key);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add(self, key: str, report: dict, merge: Callable[[dict | None], tuple[dict, dict | None]]) -> str:
        """created, updated or duplicate. Merges the report into the device in one transaction."""
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM reports WHERE report_id = ?", (report["reportId"],)).fetchone():
                return "duplicate"
            row = db.execute("SELECT doc FROM devices WHERE key = ?", (key,)).fetchone()
            old = json.loads(row["doc"]) if row else None
            new, restart = merge(old)
            db.execute("INSERT OR REPLACE INTO devices (key, last_received, doc) VALUES (?, ?, ?)",
                       (key, new.get("lastReceivedAt"), json.dumps(new)))
            db.execute("INSERT INTO reports (report_id, device_key, at, doc) VALUES (?, ?, ?, ?)",
                       (report["reportId"], key, report.get("at"), json.dumps(report)))
            if restart:
                db.execute("INSERT INTO restarts (device_key, restarted_at, doc) VALUES (?, ?, ?)",
                           (key, restart.get("restartedAt"), json.dumps(restart)))
            return "updated" if old else "created"

    def devices(self, limit: int = 20000) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT key, doc FROM devices ORDER BY last_received DESC LIMIT ?", (limit,)).fetchall()
            return [{**json.loads(r["doc"]), "key": r["key"]} for r in rows]

    def device(self, key: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT doc FROM devices WHERE key = ?", (key,)).fetchone()
            return {**json.loads(row["doc"]), "key": key} if row else None

    def reports(self, key: str, limit: int = 200) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT doc FROM reports WHERE device_key = ? ORDER BY at DESC, rowid DESC LIMIT ?",
                              (key, limit)).fetchall()
            return [json.loads(r["doc"]) for r in rows]

    def restarts(self, key: str | None = None, since: str = "", limit: int = 5000) -> list[dict]:
        with self._db() as db:
            if key:
                rows = db.execute("SELECT doc, device_key FROM restarts WHERE device_key = ? ORDER BY restarted_at DESC LIMIT ?",
                                  (key, limit)).fetchall()
            else:
                rows = db.execute("SELECT doc, device_key FROM restarts WHERE restarted_at >= ? ORDER BY restarted_at DESC LIMIT ?",
                                  (since, limit)).fetchall()
            return [{**json.loads(r["doc"]), "key": r["device_key"]} for r in rows]

    def forget(self, key: str) -> bool:
        with self._lock, self._db() as db:
            n = db.execute("DELETE FROM devices WHERE key = ?", (key,)).rowcount
            db.execute("DELETE FROM reports WHERE device_key = ?", (key,))
            db.execute("DELETE FROM restarts WHERE device_key = ?", (key,))
            return n > 0
