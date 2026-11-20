"""The keys devices send: made, expired and deleted on the site (Settings → Applications).

The same file is in every service that takes device reports (notifications, elevation, restart); a
test checks the copies match. Each service keeps its own keys in its own database, in a
device_keys table, and checks them itself, so a device never waits on another service.

    a key made here    shown once when it's made (to put in the installer), then only its first
                       characters: just its SHA-256 is kept. It never expires, or works to the end
                       of an expiry date (UTC), which can be changed while it still works.
                       Expiring it stops it at once and keeps it listed; an expired key can't be
                       brought back (make a new one). Deleting it removes it.
    a key from .env    ENROLLMENT_KEY / INGEST_KEYS on the server: still works, listed read only
                       (change it in .env)

Admins of the tool manage its keys:  GET/POST /api/device-keys,
    PATCH /api/device-keys/{id}  {"expires": "YYYY-MM-DD" or "" for never} or {"expireNow": true},
    DELETE /api/device-keys/{id}
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import access

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USED_EVERY = 600   # "last used" is written at most every 10 minutes per key


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class DeviceKeys:
    def __init__(self, path: str, env_keys: list[tuple[str, str]], prefix: str):
        """path: the service's SQLite file. env_keys: (label, key) from its .env. prefix: starts every
        new key (e.g. "cpn_" for notifications), so a key says which app it's for."""
        self.path, self.env, self.prefix = path, env_keys, prefix
        self._lock = threading.Lock()
        self._used: dict[str, float] = {}
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS device_keys (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, start TEXT NOT NULL,
                expires TEXT DEFAULT '', created_at TEXT, created_by TEXT, last_used TEXT DEFAULT '')""")
            cols = {c["name"] for c in db.execute("PRAGMA table_info(device_keys)")}
            if "expired_at" not in cols:   # added later: when it was expired by hand, and by whom
                db.execute("ALTER TABLE device_keys ADD COLUMN expired_at TEXT DEFAULT ''")
                db.execute("ALTER TABLE device_keys ADD COLUMN expired_by TEXT DEFAULT ''")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _expired(expires: str, expired_at: str = "") -> bool:
        if expired_at:   # expired by hand
            return True
        return bool(expires) and expires < date.today().isoformat()   # works through its last day

    def check(self, token: str) -> str | None:
        """The key's label if it's one of ours and in date, otherwise None."""
        if not token:
            return None
        found = None
        for label, key in self.env:   # compare against every .env key, so timing says nothing
            if hmac.compare_digest(token.encode(), key.encode()):
                found = label
        if found:
            return found
        with self._db() as db:
            r = db.execute("SELECT id, label, expires, expired_at FROM device_keys WHERE hash = ?", (_hash(token),)).fetchone()
        if not r or self._expired(r["expires"], r["expired_at"]):
            return None
        if time.time() - self._used.get(r["id"], 0) > USED_EVERY:
            self._used[r["id"]] = time.time()
            with self._lock, self._db() as db:
                db.execute("UPDATE device_keys SET last_used = ? WHERE id = ?", (_now(), r["id"]))
        return r["label"]

    def list(self) -> list[dict]:
        out = [{"id": f"env-{i}", "label": label, "start": key[:6], "source": "env", "expires": "", "expired": False,
                "expiredAt": "", "expiredBy": "", "createdAt": "", "createdBy": "", "lastUsed": ""}
               for i, (label, key) in enumerate(self.env)]
        with self._db() as db:
            for r in db.execute("SELECT * FROM device_keys ORDER BY created_at DESC"):
                out.append({"id": r["id"], "label": r["label"], "start": r["start"], "source": "site", "expires": r["expires"] or "",
                            "expired": self._expired(r["expires"], r["expired_at"]), "expiredAt": r["expired_at"] or "",
                            "expiredBy": r["expired_by"] or "", "createdAt": r["created_at"], "createdBy": r["created_by"],
                            "lastUsed": r["last_used"] or ""})
        return out

    def add(self, label: str, expires: str, by: str) -> tuple[dict, str]:
        key = self.prefix + secrets.token_urlsafe(30)
        kid = secrets.token_hex(8)
        with self._lock, self._db() as db:
            db.execute("INSERT INTO device_keys (id, label, hash, start, expires, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (kid, label, _hash(key), key[:len(self.prefix) + 4], expires, _now(), by))
        return next(k for k in self.list() if k["id"] == kid), key

    def get(self, kid: str) -> dict | None:
        return next((k for k in self.list() if k["id"] == kid), None)

    def set_expiry(self, kid: str, expires: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE device_keys SET expires = ? WHERE id = ?", (expires, kid))

    def expire_now(self, kid: str, by: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE device_keys SET expired_at = ?, expired_by = ? WHERE id = ?", (_now(), by, kid))

    def delete(self, kid: str) -> bool:
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM device_keys WHERE id = ?", (kid,)).rowcount > 0


class KeyIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    expires: str = Field("", max_length=10)   # YYYY-MM-DD, or "" for never


class KeyChange(BaseModel):
    expires: str | None = Field(None, max_length=10)   # a new expiry date, or "" for never
    expireNow: bool = False                            # stop it now; it stays listed as expired


def _check_date(expires: str) -> None:
    if expires and (not DATE.match(expires) or expires < date.today().isoformat()):
        raise HTTPException(422, "The expiry date must be today or later.")


def router(keys: DeviceKeys, tool: str, log=None) -> APIRouter:
    """The admin API for this tool's keys. Only admins of the tool (as the front door vouches)."""
    r = APIRouter()

    def admin(request: Request) -> dict:
        who = access.identity(request, tool)
        if not who:
            raise HTTPException(401, access.sign_in_message())
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
            raise HTTPException(403, "Missing request header.")
        if who.get("level") != "admin":
            raise HTTPException(403, "Only admins of this tool can see or change its device keys.")
        return who

    @r.get("/api/device-keys")
    def list_keys(who: dict = Depends(admin)):
        return {"items": keys.list()}

    @r.post("/api/device-keys")
    def add_key(body: KeyIn, who: dict = Depends(admin)):
        label = re.sub(r"\s+", " ", body.label).strip()
        if not label:
            raise HTTPException(422, "Give the key a name, e.g. the group of devices it's for.")
        _check_date(body.expires)
        item, key = keys.add(label, body.expires, who["email"])
        if log:
            log(f"device key added: {label}", who["email"])
        return {"item": item, "key": key, "items": keys.list()}

    @r.patch("/api/device-keys/{kid}")
    def change_key(kid: str, body: KeyChange, who: dict = Depends(admin)):
        if kid.startswith("env-"):
            raise HTTPException(400, "This key is set on the server (.env): change it there.")
        item = keys.get(kid)
        if not item:
            raise HTTPException(404, "No such key.")
        if item["expired"]:
            raise HTTPException(409, "This key has already expired. Make a new one instead.")
        if body.expireNow:
            keys.expire_now(kid, who["email"])
            what = "expired"
        elif body.expires is not None:
            _check_date(body.expires)
            keys.set_expiry(kid, body.expires)
            what = f"expiry set to {body.expires or 'never'}"
        else:
            raise HTTPException(422, "Give a new expiry date, \"\" for never, or expireNow.")
        if log:
            log(f"device key {what}: {item['label']}", who["email"])
        return {"item": keys.get(kid), "items": keys.list()}

    @r.delete("/api/device-keys/{kid}")
    def delete_key(kid: str, who: dict = Depends(admin)):
        if kid.startswith("env-"):
            raise HTTPException(400, "This key is set on the server (.env): change it there.")
        if not keys.delete(kid):
            raise HTTPException(404, "No such key.")
        if log:
            log(f"device key deleted: {kid}", who["email"])
        return {"items": keys.list()}

    return r
