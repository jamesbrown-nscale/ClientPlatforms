"""Nscale Users and Devices, on Client Platforms.

Devices (/devices/): every device in Jamf Pro and Intune, read through the connections set under
Settings → Connections (see connectors.py). They're read again every SYNC_MINUTES (60), or when
someone presses Sync now.

Users (/users/): the people we look after, imported from a CSV (or added by hand), each tagged with
a department and a division (their team within it), a line manager, a user type (VIP or Standard)
and a primary device, picked from Devices.

People:   /devices/api/devices[.csv]      devices, with whose primary device each is
          /devices/api/sync               sync now (edit)
          /devices/api/connections[/{kind}][/test]   Jamf and Intune (Devices admins)
          /users/api/users[.csv]          users and their tags; POST to add, PATCH/DELETE /{email}
          /users/api/import               a CSV: adds new users and updates the ones already here
          /users/api/match-devices        primary devices for users without one, from Jamf and Intune
          /users/api/devices              devices to pick a primary device from
Services: /internal/teams, /internal/users/{email}   departments and divisions, and one user, for
          the Team tool (projects service). Not routed by the front door: only reachable inside the stack.

Settings: DATABASE_PATH, SYNC_MINUTES (0 = only when asked), AUTH_MODE, AUTH_SECRET, PORTAL_URL.
"""
import csv
import io
import json
import logging
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, auth, connectors
from .auth import DEVICES, USERS, User
from .connectors import PLATFORMS, SyncError
from .store import Store, now

STATIC = Path(__file__).parent / "static"
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
SYNC_MINUTES = float(os.environ.get("SYNC_MINUTES", "60"))
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TYPES = ("Standard", "VIP")   # always there; any other user type can be added (Contractor, Executive …)
MAX_CSV = 5 * 1024 * 1024
MAX_ROWS = 20000

log = logging.getLogger("inventory")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_h)
log.propagate = False

auth.check_settings()
store = Store(os.environ.get("DATABASE_PATH", "/data/inventory.db"), access.AUTH_SECRET)


def make_client() -> httpx.Client:
    return httpx.Client(follow_redirects=False, headers={"User-Agent": "Nscale-Client-Platforms"})


# ---------- Syncing ----------

_sync_lock = threading.Lock()
_wake = threading.Event()
_syncing: set[str] = set()


def sync(kind: str) -> dict:
    """Reads every device from this connection and replaces what we had from it. {ok, count | error, note}"""
    conn = store.connection(kind)
    if not conn:
        return {"ok": False, "error": "Not connected."}
    secret = store.secret(kind)
    _syncing.add(kind)
    try:
        if conn["has_secret"] and not secret:
            raise SyncError("The saved secret can't be read any more (AUTH_SECRET changed). Enter it again.")
        with make_client() as client:
            rows, notes = connectors.READERS[kind](client, conn["settings"], secret)
        count = store.replace_devices(PLATFORMS[kind], rows)
        note = " ".join(notes)
        store.sync_result(kind, count, note=note)
        log.info(json.dumps({"severity": "INFO", "message": f"{PLATFORMS[kind]} sync: {count} devices", "note": note}))
        return {"ok": True, "count": count, "note": note}
    except (SyncError, ValueError) as e:
        store.sync_result(kind, None, error=str(e))
        log.info(json.dumps({"severity": "WARNING", "message": f"{PLATFORMS[kind]} sync failed", "error": str(e)}))
        return {"ok": False, "error": str(e)}
    except Exception as e:   # never let one odd answer stop the loop
        msg = f"Something unexpected went wrong ({type(e).__name__})."
        store.sync_result(kind, None, error=msg)
        log.exception(f"{kind} sync failed")
        return {"ok": False, "error": msg}
    finally:
        _syncing.discard(kind)


def sync_all(force: bool = False) -> dict:
    out = {}
    with _sync_lock:
        for kind in PLATFORMS:
            c = store.connection(kind)
            if not c or not c["enabled"]:
                continue
            due = SYNC_MINUTES > 0 and (not c["last_sync"] or _age_minutes(c["last_sync"]) >= SYNC_MINUTES)
            if force or due:
                out[kind] = sync(kind)
    return out


def _age_minutes(ts: str) -> float:
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


_force = threading.Event()


def _loop():
    while True:
        try:
            sync_all(force=_force.is_set())
        except Exception:
            log.exception("sync loop")
        _force.clear()
        _wake.wait(60)
        _wake.clear()


def sync_soon() -> None:
    """Sync every connection now, in the background."""
    if SYNC_MINUTES > 0:
        _force.set()
        _wake.set()
    else:
        threading.Thread(target=sync_all, kwargs={"force": True}, daemon=True).start()


@asynccontextmanager
async def lifespan(app):
    if SYNC_MINUTES > 0:
        threading.Thread(target=_loop, daemon=True, name="sync").start()
    yield


# ---------- Shared ----------

class _Static(StaticFiles):
    """The pages' CSS and Settings → Connections' script: browsers check for a newer copy each time
    (cheap, with ETags), so an update reaches everyone straight away."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


def _page(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Client Platforms</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#F3F5FB;color:#040E27;font:14px/1.5 "Segoe UI",system-ui,sans-serif}}
.c{{background:#FDFFF8;border:1px solid #E1EAF9;border-radius:12px;padding:32px;max-width:380px;margin:16px;text-align:center}}
p{{color:#3B404E;margin:0}}</style></head><body><div class="c"><p>{escape(message)}</p></div></body></html>""",
                        status_code=status_code)


def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv(cols: list[tuple[str, str]], rows: list[dict], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in cols])
    for r in rows:
        w.writerow([_cell(r.get(k)) for k, _ in cols])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _device_view(d: dict) -> dict:
    return {"id": d["id"], "platform": d["platform"], "kind": d["kind"], "name": d["name"], "make": d["make"], "model": d["model"],
            "os": d["os"], "osVersion": d["os_version"], "serial": d["serial"], "userEmail": d["user_email"],
            "userName": d["user_name"], "lastSeen": d["last_seen"], "syncedAt": d["synced_at"]}


def _connections_view(full: bool = False) -> list[dict]:
    out = []
    for kind, name in PLATFORMS.items():
        c = store.connection(kind)
        v = {"kind": kind, "name": name, "connected": bool(c), "enabled": bool(c and c["enabled"]),
             "lastSync": c and c["last_sync"], "lastOk": c and c["last_ok"], "lastError": (c and c["last_error"]) or "",
             "lastNote": (c and c["last_note"]) or "", "count": c and c["last_count"], "syncing": kind in _syncing}
        if full:
            s = (c or {}).get("settings") or {}
            v |= {"url": s.get("url", ""), "tenantId": s.get("tenant_id", ""), "clientId": s.get("client_id", ""),
                  "hasSecret": bool(c and c["has_secret"]), "updatedAt": c and c["updated_at"], "updatedBy": (c or {}).get("updated_by", "")}
        out.append(v)
    return out


# ---------- Devices ----------

devices_app = FastAPI(title="Nscale Devices", docs_url=None, redoc_url=None, openapi_url=None)
devices_app.mount("/static", _Static(directory=STATIC), name="static")
devices_app.add_exception_handler(HTTPException, _http_error)
devices_app.add_exception_handler(ValueError, _value_error)


@devices_app.get("/")
def devices_page(request: Request):
    if not auth.current_user(request, DEVICES):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "devices.html", headers={"Cache-Control": "no-store"})


@devices_app.get("/api/me")
def devices_me(user: User = Depends(auth.devices_viewer)):
    return {"email": user.email, "name": user.name, "level": user.level, "canEdit": user.can_edit,
            "admin": user.level == "admin", "portalUrl": PORTAL_URL, "syncMinutes": SYNC_MINUTES}


def _devices_with_owners() -> list[dict]:
    owners: dict[str, list[dict]] = {}
    for u in store.users():
        if u["primary_device"]:
            owners.setdefault(u["primary_device"], []).append({"email": u["email"], "name": u["name"] or access.display_name(u["email"]),
                                                               "type": u["user_type"]})
    return [{**_device_view(d), "primaryFor": owners.get(d["id"], [])} for d in store.devices()]


@devices_app.get("/api/devices")
def list_devices(user: User = Depends(auth.devices_viewer)):
    items = _devices_with_owners()
    stats = {"devices": len(items), **{p: sum(d["platform"] == p for d in items) for p in PLATFORMS.values()},
             "computers": sum(d["kind"] == "computer" for d in items), "mobiles": sum(d["kind"] == "mobile" for d in items),
             "primary": sum(bool(d["primaryFor"]) for d in items)}
    return {"items": items, "stats": stats, "connections": _connections_view()}


@devices_app.get("/api/devices.csv")
def devices_csv(user: User = Depends(auth.devices_viewer)):
    rows = [{**d, "primaryFor": ", ".join(o["email"] for o in d["primaryFor"])} for d in _devices_with_owners()]
    cols = [("name", "Name"), ("platform", "Platform"), ("make", "Make"), ("model", "Model"), ("os", "OS"), ("osVersion", "OS version"),
            ("serial", "Serial"), ("userEmail", "User"), ("lastSeen", "Last check-in"), ("primaryFor", "Primary device of")]
    return _csv(cols, rows, f"devices-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv")


@devices_app.post("/api/sync")
def sync_now(user: User = Depends(auth.devices_editor)):
    if not any((c := store.connection(k)) and c["enabled"] for k in PLATFORMS):
        raise HTTPException(400, "Nothing to sync yet: connect Jamf or Intune under Settings → Connections.")
    sync_soon()
    return {"ok": True}


# ----- Connections (Settings → Connections) -----

class ConnectionIn(BaseModel):
    url: str = Field("", max_length=300)
    tenantId: str = Field("", max_length=200)
    clientId: str = Field(min_length=1, max_length=300)
    secret: str | None = Field(None, max_length=2000)   # None keeps the saved one
    enabled: bool = True


def _kind(kind: str) -> str:
    if kind not in PLATFORMS:
        raise HTTPException(404, "No such connection.")
    return kind


@devices_app.get("/api/connections")
def get_connections(user: User = Depends(auth.devices_admin)):
    return {"items": _connections_view(full=True), "syncMinutes": SYNC_MINUTES}


@devices_app.put("/api/connections/{kind}")
def put_connection(kind: str, body: ConnectionIn, user: User = Depends(auth.devices_admin)):
    _kind(kind)
    if kind == "jamf":
        settings = {"url": connectors.jamf_url(body.url), "client_id": body.clientId.strip()}
    else:
        settings = {"tenant_id": connectors.tenant_id(body.tenantId), "client_id": body.clientId.strip()}
    old = store.connection(kind)
    secret = body.secret.strip() if body.secret is not None else None
    if secret == "":
        secret = None   # an empty box keeps the saved secret
    if secret is None and not (old and old["has_secret"]):
        raise ValueError("Enter the client secret.")
    store.save_connection(kind, settings, secret, body.enabled, user.email)
    log.info(json.dumps({"severity": "NOTICE", "message": f"{PLATFORMS[kind]} connection saved", "by": user.email,
                         "secretChanged": secret is not None}))
    return {"items": _connections_view(full=True)}


@devices_app.post("/api/connections/{kind}/test")
def test_connection(kind: str, user: User = Depends(auth.devices_admin)):
    """Signs in and reads every device: a sync, so the page shows them straight away."""
    _kind(kind)
    if not store.connection(kind):
        raise HTTPException(400, "Save the connection first.")
    with _sync_lock:
        result = sync(kind)
    return {**result, "items": _connections_view(full=True)}


@devices_app.delete("/api/connections/{kind}")
def delete_connection(kind: str, user: User = Depends(auth.devices_admin)):
    _kind(kind)
    if not store.delete_connection(kind):
        raise HTTPException(404, "Not connected.")
    gone = store.drop_platform(PLATFORMS[kind])
    log.info(json.dumps({"severity": "NOTICE", "message": f"{PLATFORMS[kind]} disconnected", "by": user.email, "devicesRemoved": gone}))
    return {"items": _connections_view(full=True), "removed": gone}


# ---------- Users ----------

users_app = FastAPI(title="Nscale Users", docs_url=None, redoc_url=None, openapi_url=None)
users_app.mount("/static", _Static(directory=STATIC), name="static")
users_app.add_exception_handler(HTTPException, _http_error)
users_app.add_exception_handler(ValueError, _value_error)


@users_app.get("/")
def users_page(request: Request):
    if not auth.current_user(request, USERS):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "users.html", headers={"Cache-Control": "no-store"})


@users_app.get("/org")
def org_page(request: Request):
    if not auth.current_user(request, USERS):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "org.html", headers={"Cache-Control": "no-store"})


@users_app.get("/departments")
def departments_page(request: Request):
    if not auth.current_user(request, USERS):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "departments.html", headers={"Cache-Control": "no-store"})


@users_app.get("/api/me")
def users_me(user: User = Depends(auth.users_viewer)):
    return {"email": user.email, "name": user.name, "level": user.level, "canEdit": user.can_edit, "portalUrl": PORTAL_URL}


def _user_view(u: dict, devices: dict[str, dict], names: dict[str, str], assigned: dict[str, list]) -> dict:
    pd = u["primary_device"]
    dev = devices.get(pd)
    mgr = u["manager"] or ""
    return {"email": u["email"], "name": u["name"] or access.display_name(u["email"]),
            "department": u["department"] or "", "division": u["division"] or "",
            "manager": mgr, "managerName": names.get(mgr.lower(), "") if "@" in mgr else mgr,
            "type": u["user_type"] or "Standard", "title": u["title"] or "",
            "primaryDevice": _device_view(dev) if dev else ({"id": pd, "missing": True} if pd else None),
            # Everything else Jamf or Intune says is theirs, latest check-in first.
            "otherDevices": [_device_view(d) for d in by_check_in(assigned.get(u["email"], [])) if d["id"] != pd],
            "devices": len(assigned.get(u["email"], [])), "updatedAt": u["updated_at"], "updatedBy": u["updated_by"]}


def _seen(d: dict) -> str:
    """A device's last check-in as a sortable UTC string ("" if none). Jamf and Graph both send ISO 8601."""
    v = d.get("last_seen") or ""
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""


def by_check_in(ds: list[dict]) -> list[dict]:
    return sorted(ds, key=_seen, reverse=True)


def pick_primary(ds: list[dict]) -> dict | None:
    """Of someone's devices: the computer that checked in most recently (or, with no computer, the device)."""
    pool = [d for d in ds if d["kind"] == "computer"] or ds
    return by_check_in(pool)[0] if pool else None


def _users() -> list[dict]:
    rows = store.users()
    devs = store.devices()
    devices = {d["id"]: d for d in devs}
    assigned: dict[str, list] = {}
    for d in devs:
        if d["user_email"]:
            assigned.setdefault(d["user_email"], []).append(d)
    names = {u["email"]: u["name"] or access.display_name(u["email"]) for u in rows}
    return [_user_view(u, devices, names, assigned) for u in rows]


@users_app.get("/api/users")
def list_users(user: User = Depends(auth.users_viewer)):
    items = _users()
    counts: dict[str, int] = {}
    for u in items:
        counts[u["type"]] = counts.get(u["type"], 0) + 1
    # VIP first, then the others A-Z, Standard last: each but Standard gets a tile on the page.
    types = [{"name": t, "count": counts.get(t, 0)}
             for t in ["VIP", *sorted((t for t in counts if t not in TYPES), key=str.lower), "Standard"]]
    stats = {"users": len(items), "vip": counts.get("VIP", 0), "departments": len({u["department"] for u in items if u["department"]}),
             "withDevice": sum(bool(u["primaryDevice"]) and not u["primaryDevice"].get("missing") for u in items),
             "noDevice": sum(not u["primaryDevice"] or bool(u["primaryDevice"].get("missing")) for u in items)}
    return {"items": items, "departments": departments(items), "types": types, "stats": stats}


def departments(users: list[dict]) -> list[dict]:
    """Department → divisions, with how many users are in each: [{name, count, divisions: [{name, count}]}].
    People with a division but no department are under "" (shown as No department)."""
    tree: dict[str, dict[str, int]] = {}
    for u in users:
        if u["department"] or u["division"]:
            divs = tree.setdefault(u["department"], {})
            if u["division"]:
                divs[u["division"]] = divs.get(u["division"], 0) + 1
            else:
                divs.setdefault("", 0)
                divs[""] += 1
    out = []
    for dep in sorted(tree, key=lambda x: (x == "", x.lower())):
        divs = tree[dep]
        out.append({"name": dep, "count": sum(divs.values()),
                    "divisions": [{"name": d, "count": n} for d, n in sorted(divs.items(), key=lambda x: x[0].lower()) if d]})
    return out


@users_app.get("/api/users.csv")
def users_csv(user: User = Depends(auth.users_viewer)):
    rows = [{**u, "pd": (u["primaryDevice"] or {}).get("name", ""), "pdSerial": (u["primaryDevice"] or {}).get("serial", ""),
             "pdModel": (u["primaryDevice"] or {}).get("model", ""), "pdOs": " ".join(filter(None, [(u["primaryDevice"] or {}).get("os"), (u["primaryDevice"] or {}).get("osVersion")])),
             "pdPlatform": (u["primaryDevice"] or {}).get("platform", ""),
             "others": ", ".join(f"{d['name'] or d['serial']} ({d['platform']})" for d in u["otherDevices"])} for u in _users()]
    cols = [("email", "Email"), ("name", "Name"), ("title", "Job title"), ("department", "Department"), ("division", "Division"),
            ("manager", "Line manager"), ("type", "User type"), ("pd", "Primary device"), ("pdSerial", "Serial"),
            ("pdModel", "Model"), ("pdOs", "OS"), ("pdPlatform", "Platform"), ("others", "Other devices")]
    return _csv(cols, rows, f"users-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv")


@users_app.get("/api/template.csv")
def users_template(user: User = Depends(auth.users_viewer)):
    cols = [("email", "Email"), ("name", "Name"), ("title", "Job title"), ("department", "Department"), ("division", "Division"),
            ("manager", "Line manager"), ("type", "User type"), ("pd", "Primary device")]
    return _csv(cols, [{"email": "amy.chen@nscale.com", "name": "Amy Chen", "title": "Engineer", "department": "Engineering",
                        "division": "Platform", "manager": "sam.lee@nscale.com", "type": "Standard", "pd": "C02XL0AAJGH5"}],
                "users-template.csv")


@users_app.get("/api/devices")
def pick_devices(user: User = Depends(auth.users_viewer)):
    """Devices to pick a primary device from (the same devices as the Devices page)."""
    return {"items": [_device_view(d) for d in store.devices()]}


def _resolve_manager(v: str, by_name: dict[str, str], email: str = "") -> str:
    v = re.sub(r"\s+", " ", (v or "").strip())[:200]
    if not v:
        return ""
    if "@" in v:
        if not EMAIL.match(v):
            raise ValueError(f"\"{v}\" isn't an email address.")
        v = v.lower()
    else:
        v = by_name.get(v.lower(), v)
    if email and v == email:
        raise ValueError("Someone can't be their own line manager.")
    return v


def _names() -> dict[str, str]:
    return {(u["name"] or "").lower(): u["email"] for u in store.users() if u["name"]}


def known_types() -> dict[str, str]:
    """Every user type in use, by its lower-case name, so "contractor" finds "Contractor"."""
    return {t.lower(): t for t in [*TYPES, *(u["user_type"] for u in store.users() if u["user_type"])]}


def _type(v: str, known: dict[str, str] | None = None) -> str:
    """VIP, Standard, a type already in use (whatever its case), or a new one. yes/no (a VIP column) mean VIP/Standard."""
    s = re.sub(r"\s+", " ", (v or "").strip())
    low = s.lower()
    if low in ("vip", "yes", "y", "true", "1"):
        return "VIP"
    if low in ("standard", "std", "no", "n", "false", "0", ""):
        return "Standard"
    if len(s) > 40 or not re.match(r"^[\w][\w &'./+-]*$", s):
        raise ValueError(f"\"{v}\" can't be a user type: up to 40 letters, numbers and spaces.")
    known = known if known is not None else known_types()
    if s.islower():
        s = s[0].upper() + s[1:]   # "contractor" → "Contractor"
    return known.setdefault(low, s)


class DeviceIndex:
    """Finds a device by its id, serial or name."""

    def __init__(self, devices: list[dict]):
        self.ids = {d["id"] for d in devices}
        self.serial: dict[str, list[str]] = {}
        self.name: dict[str, list[str]] = {}
        for d in devices:
            if d["serial"]:
                self.serial.setdefault(d["serial"].lower(), []).append(d["id"])
            if d["name"]:
                self.name.setdefault(d["name"].lower(), []).append(d["id"])

    def find(self, v: str) -> str:
        """A device id; ValueError if there isn't exactly one."""
        v = (v or "").strip()
        if not v:
            return ""
        if v in self.ids:
            return v
        hits = self.serial.get(v.lower()) or self.name.get(v.lower()) or []
        if len(hits) == 1:
            return hits[0]
        raise ValueError(f"No device with the serial or name \"{v}\"." if not hits else f"More than one device is called \"{v}\": use its serial.")


class UserIn(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field("", max_length=200)
    division: str = Field("", max_length=200)
    manager: str = Field("", max_length=200)
    type: str = Field("Standard", max_length=20)
    primaryDevice: str = Field("", max_length=200)
    title: str = Field("", max_length=200)
    department: str = Field("", max_length=200)


class UserChange(BaseModel):
    name: str | None = Field(None, max_length=200)
    division: str | None = Field(None, max_length=200)
    manager: str | None = Field(None, max_length=200)
    type: str | None = Field(None, max_length=20)
    primaryDevice: str | None = Field(None, max_length=200)
    title: str | None = Field(None, max_length=200)
    department: str | None = Field(None, max_length=200)


def _fields(body: BaseModel, email: str) -> dict:
    d = body.model_dump(exclude_none=True)
    out = {}
    for k in ("name", "division", "title", "department"):
        if k in d:
            out[k] = re.sub(r"\s+", " ", d[k]).strip()
    if "manager" in d:
        out["manager"] = _resolve_manager(d["manager"], _names(), email)
    if "type" in d:
        out["user_type"] = _type(d["type"])
    if "primaryDevice" in d:
        out["primary_device"] = DeviceIndex(store.devices()).find(d["primaryDevice"])
    return out


def _one(email: str) -> dict:
    return next(u for u in _users() if u["email"] == email)


@users_app.post("/api/users")
def add_user(body: UserIn, user: User = Depends(auth.users_editor)):
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise ValueError("Enter a valid email address.")
    if store.user(email):
        raise HTTPException(409, f"{email} is already a user.")
    store.save_users([{"email": email, **_fields(body, email)}], user.email)
    return _one(email)


@users_app.patch("/api/users/{email}")
def change_user(email: str, body: UserChange, user: User = Depends(auth.users_editor)):
    email = email.lower()
    if not store.user(email):
        raise HTTPException(404, "User not found.")
    store.save_users([{"email": email, **_fields(body, email)}], user.email)
    return _one(email)


@users_app.delete("/api/users/{email}")
def delete_user(email: str, user: User = Depends(auth.users_editor)):
    if not store.delete_users([email.lower()]):
        raise HTTPException(404, "User not found.")
    log.info(json.dumps({"severity": "NOTICE", "message": "user removed", "user": email.lower(), "by": user.email}))
    return {"ok": True}


# ----- CSV import -----

COLUMNS = {   # a CSV heading (lower case, letters and digits only) → the field it fills
    "email": "email", "emailaddress": "email", "mail": "email", "workemail": "email", "userprincipalname": "email", "upn": "email",
    "name": "name", "displayname": "name", "fullname": "name", "username": "name",
    "firstname": "first", "givenname": "first", "lastname": "last", "surname": "last", "familyname": "last",
    "division": "division", "team": "division", "subteam": "division", "dept": "department",
    "linemanager": "manager", "manager": "manager", "linemanageremail": "manager", "manageremail": "manager", "reportsto": "manager",
    "usertype": "type", "type": "type", "vip": "type",
    "primarydevice": "device", "device": "device", "primarydeviceserial": "device", "serial": "device", "serialnumber": "device",
    "devicename": "device",
    "jobtitle": "title", "title": "title", "department": "department",
}


class ImportIn(BaseModel):
    csv: str = Field(max_length=MAX_CSV)
    dryRun: bool = False
    addNew: bool = True   # False: only update people already on Users; new emails are left out


LABELS = {"email": "Email", "name": "Name", "first": "First name", "last": "Last name", "division": "Division",
          "department": "Department", "title": "Job title", "manager": "Line manager", "type": "User type", "device": "Primary device"}
FIELD_LABELS = {"name": "Name", "title": "Job title", "department": "Department", "division": "Division", "manager": "Line manager",
                "user_type": "User type", "primary_device": "Primary device"}


def parse_csv(text: str) -> tuple[list[dict], list[dict], list[str], list[dict]]:
    """(rows to save, problems, headings it didn't use, how each heading was read: [{heading, field}]).
    Each problem: {row, email, message}; a row with a bad email is skipped, and a bad tag on an
    otherwise good row leaves that tag as it was."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise ValueError("The CSV is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        head = next(reader)
    except StopIteration:
        raise ValueError("The CSV is empty.")
    keys = [COLUMNS.get(re.sub(r"[^a-z0-9]", "", h.lower())) for h in head]
    if "email" not in keys:
        raise ValueError("The CSV needs an Email column (the first row must be the column headings).")
    unused = [h for h, k in zip(head, keys) if not k and h.strip()]
    columns = [{"heading": h.strip(), "field": LABELS.get(k, "") if k and keys.index(k) == i else ""}
               for i, (h, k) in enumerate(zip(head, keys)) if h.strip()]
    raw = []
    for n, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(raw) >= MAX_ROWS:
            raise ValueError(f"At most {MAX_ROWS} users in one CSV.")
        r: dict[str, str] = {}
        for k, v in zip(keys, cells):
            if k and v.strip() and k not in r:
                r[k] = v.strip()
        raw.append((n, r))

    devices = DeviceIndex(store.devices())
    types = known_types()   # new types in this file are added as they come, so their case stays the same
    by_name = _names()
    for _, r in raw:   # managers named in this same file count too
        nm = r.get("name") or " ".join(filter(None, [r.get("first"), r.get("last")]))
        if nm and r.get("email"):
            by_name.setdefault(nm.lower(), r["email"].lower())
    out: dict[str, dict] = {}
    problems = []
    for n, r in raw:
        email = (r.get("email") or "").lower()
        if not EMAIL.match(email):
            problems.append({"row": n, "email": r.get("email", ""), "message": "Skipped: no valid email address."})
            continue
        if email in out:
            problems.append({"row": n, "email": email, "message": "This email is in the CSV more than once: the last row wins."})
        u = {"email": email}
        name = r.get("name") or " ".join(filter(None, [r.get("first"), r.get("last")]))
        if name:
            u["name"] = re.sub(r"\s+", " ", name)[:200]
        for k in ("division", "title", "department"):
            if r.get(k):
                u[k] = r[k][:200]
        for k, field, fn in (("manager", "manager", lambda v: _resolve_manager(v, by_name, email)),
                             ("type", "user_type", lambda v: _type(v, types)),
                             ("device", "primary_device", devices.find)):
            if r.get(k):
                try:
                    u[field] = fn(r[k])
                except ValueError as e:
                    problems.append({"row": n, "email": email, "message": str(e) + " Left as it was."})
        out[email] = u
    return list(out.values()), problems, unused, columns


def _changes(rows: list[dict], existing: dict[str, dict]) -> list[dict]:
    """What each row changes: [{email, name, new, fields: [{field, from, to}]}], only rows that change something."""
    devices = {d["id"]: d for d in store.devices()}
    names = {e: (u["name"] or access.display_name(e)) for e, u in existing.items()}
    names |= {r["email"]: r["name"] for r in rows if r.get("name")}

    def show(f, v):
        if not v:
            return ""
        if f == "primary_device":
            d = devices.get(v)
            return f"{d['name'] or d['serial']} ({d['platform']})" if d else v
        if f == "manager" and "@" in v:
            return f"{names[v]} ({v})" if v in names else v
        return v

    out = []
    for r in rows:
        old = existing.get(r["email"])
        fields = []
        for f, label in FIELD_LABELS.items():
            if f not in r:
                continue
            before = (old or {}).get(f) or ("Standard" if f == "user_type" and old else "")
            if (before or "") != (r[f] or ""):
                fields.append({"field": label, "from": show(f, before), "to": show(f, r[f])})
        if fields or not old:
            out.append({"email": r["email"], "name": r.get("name") or (old or {}).get("name") or access.display_name(r["email"]),
                        "new": not old, "fields": fields})
    return out


@users_app.post("/api/import")
def import_users(body: ImportIn, user: User = Depends(auth.users_editor)):
    """Reads the CSV's columns by their headings and changes only those fields. With dryRun, says what
    it would do without doing it; either way the answer lists, per person, what changes (old → new)."""
    rows, problems, unused, columns = parse_csv(body.csv)
    existing = {u["email"]: u for u in store.users()}
    new = [r for r in rows if r["email"] not in existing]
    skipped = 0
    if not body.addNew:
        skipped = len(new)
        rows = [r for r in rows if r["email"] in existing]
    changes = _changes(rows, existing)
    report = {"rows": len(rows), "newInFile": len(new), "skipped": skipped, "columns": columns, "unusedColumns": unused,
              "problems": problems[:200], "moreProblems": max(0, len(problems) - 200),
              "changes": changes[:500], "moreChanges": max(0, len(changes) - 500)}
    if body.dryRun:
        added = sum(c["new"] for c in changes)
        updated = len(changes) - added
        return {**report, "dryRun": True, "added": added, "updated": updated, "unchanged": len(rows) - added - updated}
    added, updated = store.save_users(rows, user.email)
    log.info(json.dumps({"severity": "NOTICE", "message": "users imported", "by": user.email, "added": added, "updated": updated,
                         "columns": [c["field"] for c in columns if c["field"]]}))
    return {**report, "added": added, "updated": updated, "unchanged": len(rows) - added - updated}


@users_app.post("/api/match-devices")
def match_devices(user: User = Depends(auth.users_editor)):
    """For each user without a primary device: of the devices Jamf or Intune says are theirs, the
    computer that checked in most recently (or, with no computer, the device that did). Their other
    devices are listed with them anyway (see _user_view)."""
    mine: dict[str, list[dict]] = {}
    for d in store.devices():
        if d["user_email"]:
            mine.setdefault(d["user_email"], []).append(d)
    ids = {d["id"] for ds in mine.values() for d in ds}
    rows, several = [], 0
    for u in store.users():
        if u["primary_device"] and u["primary_device"] in ids:
            continue
        ds = mine.get(u["email"], [])
        pick = pick_primary(ds)
        if pick:
            rows.append({"email": u["email"], "primary_device": pick["id"]})
            several += len(ds) > 1
    _, updated = store.save_users(rows, user.email)
    return {"matched": updated, "several": several}


# ---------- One app, two paths ----------

app = FastAPI(title="Nscale Users and Devices", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# For the Team tool (projects service), inside the stack only: the front door sends /users/* and
# /devices/* here, never /internal/*. Names and job details only, nothing about devices.
@app.get("/internal/teams", include_in_schema=False)
def internal_teams():
    return {"departments": departments(_users())}


@app.get("/internal/users/{email}", include_in_schema=False)
def internal_user(email: str):
    u = store.user(email.strip().lower())
    if not u:
        raise HTTPException(404, "Not a user.")
    v = next(x for x in _users() if x["email"] == u["email"])
    return {k: v[k] for k in ("email", "name", "title", "department", "division", "manager", "managerName")}


@app.get("/healthz", include_in_schema=False)
def _healthz():
    return {"ok": True}


for _path, _sub in (("/devices", devices_app), ("/users", users_app)):
    app.add_api_route(_path, (lambda p: lambda: RedirectResponse(p + "/"))(_path), include_in_schema=False)
    app.mount(_path, _sub)
