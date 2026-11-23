"""The recycle bin: things people delete are kept here for 30 days, and can be put back (Settings → Recycle bin).

The same file is in every service with things to delete (projects, inventory, portal); a test checks the
copies match. Each service keeps its own bin in its own SQLite database, in a recycle_bin table.

How it keeps something: just before the delete, put() copies the row and everything the delete takes
with it, following the database's own links (a project's records, their files, its members; a plan's
jobs and comments). Links that the delete only clears (ON DELETE SET NULL, or a column the code empties
itself, given as `fields`) are noted too. restore() puts every row back as it was, with the same IDs,
and sets those links again where they're still empty. Something kept inside another row (a sub job in
its job's list) is kept with put_custom() and put back by a function the service registers.

Restoring fails, and the item stays in the bin, when what it belonged to has gone since (restore that
first), or something new now has the same ID or name.

    GET    /api/bin                 what's in the bin (no contents, just what each thing was)
    POST   /api/bin/{id}/restore    put it back
    DELETE /api/bin/{id}            delete it for good
Only site admins (the service's own admin check) can use these. A router made with `tool` lists and acts on
only what was deleted in that tool (so each tool's admins see their own, and nobody else's).
"""
import base64
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

KEEP_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(v):
    """A value as JSON can hold it: bytes (file contents) as base64."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(v)).decode()}
    return v


def _back(v):
    if isinstance(v, dict) and "__b64__" in v:
        return base64.b64decode(v["__b64__"])
    return v


class BinError(Exception):
    pass


class Bin:
    def __init__(self, connect: Callable[[], sqlite3.Connection], lock):
        """connect: opens the service's database (with foreign keys on, if it uses them). lock: its write lock."""
        self.connect, self.lock = connect, lock
        self._custom: dict[str, Callable] = {}
        with self.lock, self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS recycle_bin (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, context TEXT DEFAULT '',
                tool TEXT DEFAULT '', deleted_at TEXT NOT NULL, deleted_by TEXT DEFAULT '', payload TEXT NOT NULL)""")

    # ----- keeping -----

    @staticmethod
    def _links(db) -> dict[str, list[tuple[str, str, str, str]]]:
        """parent table → [(child table, child column, parent column, on delete)] from the schema."""
        out: dict[str, list] = {}
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            for fk in db.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():
                parent, frm, to, on_delete = fk[2], fk[3], fk[4], (fk[6] or "").upper()
                if not to:   # the parent's primary key
                    pk = [c[1] for c in db.execute(f'PRAGMA table_info("{parent}")') if c[5]]
                    to = pk[0] if pk else "rowid"
                out.setdefault(parent, []).append((t, frm, to, on_delete))
        return out

    @staticmethod
    def _key(db, table: str, row: dict) -> dict:
        pk = [c[1] for c in db.execute(f'PRAGMA table_info("{table}")') if c[5]]
        return {c: row[c] for c in pk} if pk else dict(row)

    def _capture(self, db, table: str, where: dict, fields: list, also: list = ()) -> dict:
        prev = db.row_factory
        db.row_factory = sqlite3.Row
        try:
            links, rows, keep, seen = self._links(db), [], [], set()

            def walk(t: str, cond: dict):
                sql = " AND ".join(f'"{c}" = ?' for c in cond)
                for r in db.execute(f'SELECT * FROM "{t}" WHERE {sql}', tuple(cond.values())).fetchall():
                    row = dict(r)
                    mark = (t, json.dumps(self._key(db, t, row), sort_keys=True, default=str))
                    if mark in seen:
                        continue
                    seen.add(mark)
                    rows.append([t, {k: _plain(v) for k, v in row.items()}])   # parents before children
                    for child, frm, to, on_delete in links.get(t, []):
                        if row.get(to) is None:
                            continue
                        if on_delete == "CASCADE":
                            walk(child, {frm: row[to]})
                        elif on_delete == "SET NULL":
                            for c in db.execute(f'SELECT * FROM "{child}" WHERE "{frm}" = ?', (row[to],)).fetchall():
                                keep.append([child, self._key(db, child, dict(c)), {frm: row[to]}])

            walk(table, where)
            for t, cond in also:   # rows that belong with it but aren't linked in the schema
                walk(t, cond)
            for t, cond, cols in fields:
                sql = " AND ".join(f'"{c}" = ?' for c in cond)
                for r in db.execute(f'SELECT * FROM "{t}" WHERE {sql}', tuple(cond.values())).fetchall():
                    row = dict(r)
                    keep.append([t, self._key(db, t, row), {c: _plain(row[c]) for c in cols}])
            return {"rows": rows, "fields": keep}
        finally:
            db.row_factory = prev

    def put(self, db, *, kind: str, label: str, table: str, where: dict, by: str, context: str = "", tool: str = "",
            fields: list = (), also: list = ()) -> str:
        """Keeps a row and everything its delete takes with it. Call with the connection that will do the
        delete, just before it. fields: [(table, where, [columns])] the code is about to clear itself.
        also: [(table, where)] other rows the code deletes with it (not linked in the schema)."""
        payload = self._capture(db, table, where, list(fields), list(also))
        if not payload["rows"]:
            return ""
        return self._insert(db, kind, label, context, tool, by, payload)

    def put_custom(self, db, *, kind: str, label: str, data: dict, by: str, context: str = "", tool: str = "") -> str:
        """Keeps something that lives inside another row; restore_custom(kind) puts it back."""
        return self._insert(db, kind, label, context, tool, by, {"custom": kind, "data": data})

    def restore_custom(self, kind: str):
        """Registers fn(db, data) that puts a put_custom() item back (raise BinError if it can't)."""
        def wrap(fn):
            self._custom[kind] = fn
            return fn
        return wrap

    def _insert(self, db, kind, label, context, tool, by, payload) -> str:
        bid = uuid.uuid4().hex
        db.execute("INSERT INTO recycle_bin (id, kind, label, context, tool, deleted_at, deleted_by, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (bid, kind, (label or kind)[:300], (context or "")[:300], tool, _iso(_now()), by or "", json.dumps(payload)))
        return bid

    # ----- looking, restoring, emptying -----

    def _expire(self, db) -> None:
        db.execute("DELETE FROM recycle_bin WHERE deleted_at < ?", (_iso(_now() - timedelta(days=KEEP_DAYS)),))

    def list(self, tool: str | None = None) -> list[dict]:
        with self.lock, self.connect() as db:
            self._expire(db)
            rows = db.execute("SELECT id, kind, label, context, tool, deleted_at, deleted_by, length(payload) FROM recycle_bin "
                              + ("WHERE tool = ? " if tool else "") + "ORDER BY deleted_at DESC", (tool,) if tool else ()).fetchall()
        return [{"id": r[0], "kind": r[1], "label": r[2], "context": r[3], "tool": r[4], "deletedAt": r[5], "deletedBy": r[6],
                 "size": r[7], "expiresAt": _iso(datetime.strptime(r[5], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                                  + timedelta(days=KEEP_DAYS))} for r in rows]

    def tool_of(self, bid: str) -> str | None:
        """Which tool something in the bin was deleted in ("" if not said), or None if it isn't there."""
        with self.connect() as db:
            r = db.execute("SELECT tool FROM recycle_bin WHERE id = ?", (bid,)).fetchone()
        return None if not r else (r[0] or "")

    def restore(self, bid: str) -> dict:
        with self.lock, self.connect() as db:
            r = db.execute("SELECT kind, label, payload FROM recycle_bin WHERE id = ?", (bid,)).fetchone()
            if not r:
                raise BinError("It isn't in the recycle bin any more.")
            kind, label, payload = r[0], r[1], json.loads(r[2])
            try:
                db.execute("SAVEPOINT restore")
                if payload.get("custom"):
                    fn = self._custom.get(payload["custom"])
                    if not fn:
                        raise BinError("This can't be restored here.")
                    fn(db, payload["data"])
                else:
                    for table, row in payload["rows"]:
                        cols = list(row)
                        db.execute(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join("?" * len(cols))})',
                                   tuple(_back(row[c]) for c in cols))
                    for table, key, vals in payload.get("fields", []):
                        cond = " AND ".join(f'"{c}" = ?' for c in key)
                        for col, v in vals.items():   # only where nothing else has been put there since
                            db.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE {cond} AND ("{col}" IS NULL OR "{col}" = \'\')',
                                       (_back(v), *key.values()))
                db.execute("DELETE FROM recycle_bin WHERE id = ?", (bid,))
                db.execute("RELEASE restore")
            except sqlite3.IntegrityError as e:
                db.execute("ROLLBACK TO restore")
                db.execute("RELEASE restore")
                msg = str(e)
                if "FOREIGN KEY" in msg:
                    raise BinError(f"“{label}” belonged to something that has been deleted since. Restore that first.")
                raise BinError(f"Something with the same name or ID as “{label}” exists now, so it can't come back as it was.")
            except BinError:
                db.execute("ROLLBACK TO restore")
                db.execute("RELEASE restore")
                raise
        return {"kind": kind, "label": label}

    def purge(self, bid: str) -> bool:
        with self.lock, self.connect() as db:
            return db.execute("DELETE FROM recycle_bin WHERE id = ?", (bid,)).rowcount > 0


def router(get_bin: Callable[[], Bin], admin: Callable, log: Callable | None = None, tool: str | None = None) -> APIRouter:
    """The bin's API. get_bin() returns the service's Bin. `admin` is a FastAPI dependency that returns
    the signed-in site admin's email, or raises (401 not signed in, 403 not an admin). tool: only what was
    deleted in that tool."""
    r = APIRouter()

    def mine(bid: str) -> None:
        if tool and get_bin().tool_of(bid) != tool:
            raise HTTPException(404, "It isn't in the recycle bin any more.")

    def changing(request: Request, who: str = Depends(admin)) -> str:
        if request.headers.get("x-requested-with") != "nscale-admin":
            raise HTTPException(403, "Missing request header.")
        return who

    @r.get("/api/bin")
    def list_bin(who: str = Depends(admin)):
        return {"items": get_bin().list(tool), "keepDays": KEEP_DAYS}

    @r.post("/api/bin/{bid}/restore")
    def restore(bid: str, who: str = Depends(changing)):
        mine(bid)
        try:
            done = get_bin().restore(bid)
        except BinError as e:
            raise HTTPException(409, str(e))
        if log:
            log(f"restored from the recycle bin: {done['kind']} {done['label']}", who)
        return {"restored": done, "items": get_bin().list(tool)}

    @r.delete("/api/bin/{bid}")
    def purge(bid: str, who: str = Depends(changing)):
        mine(bid)
        if not get_bin().purge(bid):
            raise HTTPException(404, "It isn't in the recycle bin any more.")
        if log:
            log(f"deleted for good from the recycle bin: {bid}", who)
        return {"items": get_bin().list(tool)}

    return r
