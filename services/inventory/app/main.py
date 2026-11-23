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
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, auth, connectors, recycle
from .auth import DEPARTMENTS, DEVICES, ORG, USERS, USERS_CT, User
from .connectors import PLATFORMS, SyncError
from .store import Store, clean_email, now

STATIC = Path(__file__).parent / "static"
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
SYNC_MINUTES = float(os.environ.get("SYNC_MINUTES", "60"))
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STALE_DAYS = 30   # a device that hasn't checked in for this long gets the "Not checked in" tile
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

# The lists of users: each its own page, access and tables; the same devices.
#   tool: (name on the site, table prefix, what a deleted one is called in the recycle bin)
LISTS = {USERS: ("Users", "", "User"), USERS_CT: ("Users - CT", "ct_", "User (CT)")}


def users_store() -> Store:
    """The Store for the list this request is for (Users unless it came in at /users-ct)."""
    key = auth.LIST.get()
    if key == USERS:
        return store
    name, prefix, kind = LISTS[key]
    return store.sibling(prefix, key, kind)


def all_lists() -> list[tuple[str, Store]]:
    return [(key, store if key == USERS else store.sibling(prefix, key, kind)) for key, (_, prefix, kind) in LISTS.items()]


def known_owners(skip: str = "") -> dict[str, str]:
    """Device email → whose it is on any list (but `skip`): their own email, or the one it's linked to."""
    out: dict[str, str] = {}
    for key, ls in all_lists():
        if key == skip:
            continue
        for u in ls.users():
            out.setdefault(u["email"], u["email"])
        for src, dst in ls.links().items():
            out.setdefault(src, dst)
    return out


class _Book:
    """`book` is whichever list of users this request is for: the users section uses it in place of
    `store`. Org chart, Departments and the Team tool always read Users (`store`)."""
    def __getattr__(self, name):
        return getattr(users_store(), name)


book = _Book()


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
    for key, ls in all_lists():
        for u in ls.users():
            if u["primary_device"]:
                owners.setdefault(u["primary_device"], []).append({"email": u["email"], "name": u["name"] or access.display_name(u["email"]),
                                                                   "type": u["user_type"], "list": LISTS[key][0], "url": f"/{key}/"})
    known = known_owners()
    out = []
    for d in store.devices():
        who = known.get(d["user_email"], "") if d["user_email"] else ""
        out.append({**_device_view(d), "primaryFor": owners.get(d["id"], []),
                    # Assigned in Jamf or Intune to an email that isn't on Users or Users - CT (nor linked to someone who is)
                    "notOnUsers": bool(d["user_email"]) and not who, "linkedTo": who if who and who != d["user_email"] else ""})
    return out


@devices_app.get("/api/devices")
def list_devices(user: User = Depends(auth.devices_viewer)):
    items = _devices_with_owners()
    stats = {"devices": len(items), **{p: sum(d["platform"] == p for d in items) for p in PLATFORMS.values()},
             "computers": sum(d["kind"] == "computer" for d in items), "mobiles": sum(d["kind"] == "mobile" for d in items),
             "primary": sum(bool(d["primaryFor"]) for d in items)}
    # Not checked in for 30 days (or never): likely lost, broken, in a drawer or gone without being retired.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    for d in items:
        d["stale"] = _seen({"last_seen": d["lastSeen"]}) < cutoff
    stats["stale"] = sum(d["stale"] for d in items)
    return {"items": items, "stats": stats, "staleDays": STALE_DAYS, "connections": _connections_view()}


@devices_app.get("/api/devices.csv")
def devices_csv(dep: str = "show", user: User = Depends(auth.devices_viewer)):
    """dep=hide leaves out DEP devices (named "DEP…"), as the page does with its DEP devices switch off."""
    devices = [d for d in _devices_with_owners() if dep != "hide" or not str(d["name"] or "").startswith("DEP")]
    rows = [{**d, "primaryFor": ", ".join(o["email"] for o in d["primaryFor"])} for d in devices]
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


def _bin_admin(request: Request) -> str:
    """The recycle bin's deleted users are for Users admins."""
    user = auth.current_user(request, auth.LIST.get())
    if not user:
        raise HTTPException(401, access.sign_in_message())
    if user.level != "admin":
        raise HTTPException(403, "Only admins can see the recycle bin.")
    return user.email


users_app.include_router(recycle.router(lambda: book.bin, _bin_admin))


@users_app.get("/")
def users_page(request: Request):
    if not auth.current_user(request, auth.LIST.get()):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "users.html", headers={"Cache-Control": "no-store"})


@users_app.get("/org", include_in_schema=False)
@users_app.get("/departments", include_in_schema=False)
def _moved(request: Request):
    """They were here before they had their own access: /org/ and /departments/ now."""
    return RedirectResponse("/" + request.url.path.rstrip("/").rsplit("/", 1)[-1] + "/")


@users_app.get("/api/me")
def users_me(user: User = Depends(auth.users_viewer)):
    key = auth.LIST.get()
    return {"email": user.email, "name": user.name, "level": user.level, "canEdit": user.can_edit, "portalUrl": PORTAL_URL,
            "list": key, "listName": LISTS[key][0]}


def os_family(os_name: str) -> str:
    """Mac, Windows or Linux from what Jamf or Intune calls the OS ("" for phones, tablets and the rest)."""
    o = (os_name or "").lower()
    if any(x in o for x in ("ios", "ipados", "iphone", "ipad", "tvos", "watchos", "android")):
        return ""
    if "mac" in o or "os x" in o:
        return "Mac"
    if "windows" in o:
        return "Windows"
    if any(x in o for x in ("linux", "ubuntu", "debian", "fedora", "red hat", "rhel", "centos", "suse")):
        return "Linux"
    return ""


def platform_of(primary: dict | None, others: list[dict]) -> str:
    """Their primary device's platform; if that's a phone or unknown, their latest computer's."""
    for d in ([primary] if primary else []) + by_check_in(others):
        if f := os_family(d["os"]):
            return f
    return ""


def _user_view(u: dict, devices: dict[str, dict], names: dict[str, str], assigned: dict[str, list]) -> dict:
    pd = u["primary_device"]
    dev = devices.get(pd)
    mgr = u["manager"] or ""
    mine = assigned.get(u["email"], [])
    auto = platform_of(dev, [d for d in mine if d["id"] != pd])
    tag = u.get("platform_tag") or ""
    return {"email": u["email"], "name": u["name"] or access.display_name(u["email"]),
            "department": u["department"] or "", "division": u["division"] or "",
            "manager": mgr, "managerName": names.get(mgr.lower(), "") if "@" in mgr else mgr,
            "type": u["user_type"] or "Standard", "title": u["title"] or "", "location": u.get("location") or "",
            # Platform: set by hand if someone has, otherwise from their devices.
            "platform": tag or auto, "platformSet": tag, "platformAuto": auto,
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


def owner_email(d: dict, links: dict[str, str]) -> str:
    """Whose a device is on Users: the email Jamf or Intune gives, or the user it's linked to (Mismatches)."""
    return links.get(d["user_email"], d["user_email"])


def _users() -> list[dict]:
    rows = book.users()
    devs = book.devices()
    devices = {d["id"]: d for d in devs}
    assigned: dict[str, list] = {}
    links = book.links()
    for d in devs:
        if d["user_email"]:
            assigned.setdefault(owner_email(d, links), []).append(d)
    names = {u["email"]: u["name"] or access.display_name(u["email"]) for u in rows}
    return [_user_view(u, devices, names, assigned) for u in rows]


@users_app.get("/api/users")
def list_users(user: User = Depends(auth.users_viewer)):
    return users_payload()


def users_payload() -> dict:
    items = _users()
    counts: dict[str, int] = {}
    for u in items:
        counts[u["type"]] = counts.get(u["type"], 0) + 1
    # VIP first, then the others A-Z, Standard last: each but Standard gets a tile on the page.
    types = [{"name": t, "count": counts.get(t, 0)}
             for t in ["VIP", *sorted((t for t in counts if t not in TYPES), key=str.lower), "Standard"]]
    stats = {"users": len(items), "review": book.review_count(), "mismatches": len(_unmatched(items)), "vip": counts.get("VIP", 0), "departments": len({u["department"] for u in items if u["department"]}),
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
            ("manager", "Line manager"), ("location", "Location"), ("type", "User type"), ("platform", "Platform"), ("pd", "Primary device"), ("pdSerial", "Serial"),
            ("pdModel", "Model"), ("pdOs", "OS"), ("pdPlatform", "Managed by"), ("others", "Other devices")]
    return _csv(cols, rows, f"users-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv")


@users_app.get("/api/template.csv")
def users_template(user: User = Depends(auth.users_viewer)):
    cols = [("email", "Email"), ("name", "Name"), ("title", "Job title"), ("department", "Department"), ("division", "Division"),
            ("manager", "Line manager"), ("location", "Location"), ("type", "User type"), ("pd", "Primary device")]
    return _csv(cols, [{"email": "amy.chen@nscale.com", "name": "Amy Chen", "title": "Engineer", "department": "Engineering",
                        "division": "Platform", "manager": "sam.lee@nscale.com", "location": "London", "type": "Standard", "pd": "C02XL0AAJGH5"}],
                "users-template.csv")


@users_app.get("/api/devices")
def pick_devices(user: User = Depends(auth.users_viewer)):
    """Devices to pick a primary device from (the same devices as the Devices page)."""
    return {"items": [_device_view(d) for d in book.devices()]}


def _resolve_manager(v: str, by_name: dict[str, str], email: str = "") -> str:
    v = re.sub(r"\s+", " ", (v or "").strip())[:200]
    if not v:
        return ""
    if "@" in v:
        if not EMAIL.match(v):
            raise ValueError(f"\"{v}\" isn't an email address.")
        v = clean_email(v)
    else:
        v = by_name.get(v.lower(), v)
    if email and v == email:
        raise ValueError("Someone can't be their own line manager.")
    return v


def _names() -> dict[str, str]:
    return {(u["name"] or "").lower(): u["email"] for u in book.users() if u["name"]}


def known_types() -> dict[str, str]:
    """Every user type in use, by its lower-case name, so "contractor" finds "Contractor"."""
    return {t.lower(): t for t in [*TYPES, *(u["user_type"] for u in book.users() if u["user_type"])]}


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
    location: str = Field("", max_length=200)
    platform: str = Field("", max_length=20)   # Mac, Windows or Linux by hand; "" = from their devices


class UserChange(BaseModel):
    name: str | None = Field(None, max_length=200)
    division: str | None = Field(None, max_length=200)
    manager: str | None = Field(None, max_length=200)
    type: str | None = Field(None, max_length=20)
    primaryDevice: str | None = Field(None, max_length=200)
    title: str | None = Field(None, max_length=200)
    department: str | None = Field(None, max_length=200)
    location: str | None = Field(None, max_length=200)
    platform: str | None = Field(None, max_length=20)


PLATFORMS_OS = ("Mac", "Windows", "Linux")


def _platform(v: str) -> str:
    v = v.strip()
    if not v or v.lower() in ("auto", "automatic"):
        return ""
    hit = next((p for p in PLATFORMS_OS if p.lower() == v.lower()), "")
    if not hit:
        raise ValueError("Platform is Mac, Windows or Linux (or empty to take it from their devices).")
    return hit


def _fields(body: BaseModel, email: str) -> dict:
    d = body.model_dump(exclude_none=True)
    out = {}
    for k in ("name", "division", "title", "department", "location"):
        if k in d:
            out[k] = re.sub(r"\s+", " ", d[k]).strip()
    if "manager" in d:
        out["manager"] = _resolve_manager(d["manager"], _names(), email)
    if "type" in d:
        out["user_type"] = _type(d["type"])
    if "platform" in d:
        out["platform_tag"] = _platform(d["platform"])
    if "primaryDevice" in d:
        out["primary_device"] = DeviceIndex(book.devices()).find(d["primaryDevice"])
    return out


def _one(email: str) -> dict:
    return next(u for u in _users() if u["email"] == email)


@users_app.post("/api/users")
def add_user(body: UserIn, user: User = Depends(auth.users_editor)):
    email = clean_email(body.email)
    if not EMAIL.match(email):
        raise ValueError("Enter a valid email address.")
    if book.user(email):
        raise HTTPException(409, f"{email} is already a user.")
    book.save_users([{"email": email, **_fields(body, email)}], user.email)
    return _one(email)


@users_app.patch("/api/users/{email}")
def change_user(email: str, body: UserChange, user: User = Depends(auth.users_editor)):
    email = email.lower()
    if not book.user(email):
        raise HTTPException(404, "User not found.")
    book.save_users([{"email": email, **_fields(body, email)}], user.email)
    return _one(email)


@users_app.delete("/api/users/{email}")
def delete_user(email: str, user: User = Depends(auth.users_editor)):
    if not book.delete_users([email.lower()], by=user.email):
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
    "location": "location", "officelocation": "location", "office": "location", "worklocation": "location", "site": "location",
    "city": "location", "officecity": "location", "basedin": "location", "workplace": "location",
}


class ImportIn(BaseModel):
    csv: str = Field(max_length=MAX_CSV)
    dryRun: bool = False
    addNew: bool = True   # False: only update people already on Users; new emails are left out
    everyone: bool = False   # the file is everyone: people on Users who aren't in it are listed as Left


LABELS = {"email": "Email", "name": "Name", "first": "First name", "last": "Last name", "division": "Division",
          "department": "Department", "title": "Job title", "manager": "Line manager", "location": "Location", "type": "User type", "device": "Primary device"}
FIELD_LABELS = {"name": "Name", "title": "Job title", "department": "Department", "division": "Division", "manager": "Line manager",
                "location": "Location", "user_type": "User type", "primary_device": "Primary device"}


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

    devices = DeviceIndex(book.devices())
    types = known_types()   # new types in this file are added as they come, so their case stays the same
    by_name = _names()
    for _, r in raw:   # managers named in this same file count too
        nm = r.get("name") or " ".join(filter(None, [r.get("first"), r.get("last")]))
        if nm and r.get("email"):
            by_name.setdefault(nm.lower(), clean_email(r["email"]))
    out: dict[str, dict] = {}
    problems = []
    for n, r in raw:
        email = clean_email(r.get("email") or "")
        if not EMAIL.match(email):
            problems.append({"row": n, "email": r.get("email", ""), "message": "Skipped: no valid email address."})
            continue
        if email in out:
            problems.append({"row": n, "email": email, "message": "This email is in the CSV more than once: the last row wins."})
        u = {"email": email}
        name = r.get("name") or " ".join(filter(None, [r.get("first"), r.get("last")]))
        if name:
            u["name"] = re.sub(r"\s+", " ", name)[:200]
        for k in ("division", "title", "department", "location"):
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
    devices = {d["id"]: d for d in book.devices()}
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
    """Reads the CSV's columns by their headings and changes only those fields. Nothing is saved: the
    changes go to Review (/users/review), where each is accepted or not. With dryRun, says what would go
    there. Either way the answer lists, per person, what changes (old → new)."""
    rows, problems, unused, columns = parse_csv(body.csv)
    existing = {u["email"]: u for u in book.users()}
    in_file = {r["email"] for r in rows}
    left = sorted((e for e in existing if e not in in_file), key=lambda e: (existing[e]["name"] or e).lower()) if body.everyone else []
    new = [r for r in rows if r["email"] not in existing]
    skipped = 0
    if not body.addNew:
        skipped = len(new)
        rows = [r for r in rows if r["email"] in existing]
    changes = _changes(rows, existing)
    report = {"rows": len(rows), "newInFile": len(new), "skipped": skipped, "columns": columns, "unusedColumns": unused,
              "problems": problems[:200], "moreProblems": max(0, len(problems) - 200),
              "changes": changes[:500], "moreChanges": max(0, len(changes) - 500)}
    added = sum(c["new"] for c in changes)
    updated = len(changes) - added
    report |= {"added": added, "updated": updated, "unchanged": len(rows) - added - updated, "left": len(left),
               "leftNames": [existing[e]["name"] or e for e in left[:20]]}
    if body.dryRun:
        return {**report, "dryRun": True}
    by_email = {r["email"]: r for r in rows}
    items = []
    for c in changes:
        r, old = by_email[c["email"]], existing.get(c["email"])
        if old:   # only what differs
            fields = {f: r[f] for f in FIELD_LABELS if f in r and (old.get(f) or ("Standard" if f == "user_type" else "")) != (r[f] or "")}
        else:
            fields = {k: v for k, v in r.items() if k != "email"}
        items.append({"id": uuid.uuid4().hex, "kind": "new" if c["new"] else "changed", "email": c["email"], "name": c["name"],
                      "fields": fields, "changes": c["fields"]})
    for e in left:
        u = existing[e]
        items.append({"id": uuid.uuid4().hex, "kind": "left", "email": e, "name": u["name"] or access.display_name(e),
                      "changes": [{"field": k, "from": u[f] or "", "to": ""} for f, k in (("department", "Department"), ("division", "Division"), ("title", "Job title"), ("location", "Location")) if u.get(f)]})
    book.replace_review(items, user.email)
    log.info(json.dumps({"severity": "NOTICE", "message": "users import sent to review", "by": user.email, "new": added,
                         "changed": updated, "left": len(left), "columns": [c["field"] for c in columns if c["field"]]}))
    return {**report, "review": {"new": added, "changed": updated, "left": len(left)}}


# ----- Review: an import's changes, accepted (or not) one by one or all at once -----

# ---------- Mismatches: devices whose email isn't on Users, and users with no device ----------
#   A device is someone's when the email Jamf or Intune gives is their email on Users. When it isn't
#   (an alias, an old surname, a sign-in name, a different domain), Mismatches suggests who it may be
#   and a link ("devices under this email are this user's") fixes it for good: links are kept across
#   syncs and imports.

def _tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).split()


# Tags people add to an email that aren't part of the name: adam.bullington.ct@, jo.smith-adm@, …
EMAIL_TAGS = {"ct", "ext", "external", "adm", "admin", "contractor", "contract", "temp", "tmp", "test", "old", "svc", "x"}


def _email_words(email: str) -> tuple[list[str], list[str]]:
    """The words before the @ (digits dropped), and which of them were tags like "ct"."""
    words = [re.sub(r"\d+", "", w) for w in _tokens(email.split("@")[0])]
    words = [w for w in words if w]
    core = [w for w in words if w not in EMAIL_TAGS]
    return (core or words), [w for w in words if w in EMAIL_TAGS and core]


def _name_forms(ut: list[str]) -> set[str]:
    """How a name is written as one word before the @: adambullington, abullington, bullingtona, …
    A surname of several words (van Dam) is written joined (vandam), or by its last word."""
    if len(ut) < 2:
        return set()
    first, lasts = ut[0], {ut[-1], "".join(ut[1:])}
    out = set()
    for last in lasts:
        out |= {first + last, last + first}
        if len(last) >= 4:   # an initial with a short surname is too easily something else (a + dam = adam)
            out |= {first[0] + last, last + first[0]}
    return out


def _features(email: str, name: str) -> dict:
    """What the matching looks at, worked out once per person (or device email)."""
    core, tags = _email_words(email)
    ut = _tokens(name)
    return {"core": core, "tags": tags, "dom": email.split("@")[-1], "words": ut, "forms": _name_forms(ut),
            "lasts": {ut[-1], "".join(ut[1:])} if len(ut) >= 2 else set()}


def _score(e: dict, dn: list[str], p: dict) -> tuple[int, str]:
    """How likely a device email (e: its features; dn: the name Jamf or Intune gives) is this person (p). 0 = no."""
    core, ut = e["core"], p["words"]
    tagged = f" (without “{'.'.join(e['tags'])}”)" if e["tags"] else ""
    if core and core == p["core"]:
        return 5, ("Same before the @" if e["dom"] != p["dom"] else "Same email") + tagged
    if dn and ut and dn == ut:
        return 4, "Same name"
    if len(ut) >= 2:
        first, lasts = ut[0], p["lasts"]
        # every word of their name in the email, in order: adam.bullington, adam.j.bullington, bullington.adam
        if len(core) >= 2 and ((core[0] == first and (core[-1] in lasts or "".join(core[1:]) in lasts))
                               or (core[-1] == first and core[0] in lasts)):
            return 4, "Their name in the email" + tagged
        if "".join(core) in p["forms"] or (len(core) == 2 and len(core[0]) == 1 and core[0] == first[0] and core[1] in lasts):
            return 3, "Their name in the email" + tagged
    if len(dn) >= 2 and len(ut) >= 2 and dn[0] == ut[0] and dn[-1] == ut[-1]:
        return 3, "Same first and last name"
    if len(dn) >= 2 and len(ut) >= 2 and len(ut[-1]) >= 3 and dn[-1] == ut[-1] and (dn[0].startswith(ut[0]) or ut[0].startswith(dn[0])):
        return 2, "Similar name"   # Jon / Jonathan Smith
    return 0, ""


class Matcher:
    """Suggestions against a set of people. Each person's words are worked out once and indexed, so a
    device email is only compared with people who share a word with it (every rule needs one)."""

    def __init__(self, people: list[dict]):
        self.people = people
        self.feats = [_features(u["email"], u["name"]) for u in people]
        self.index: dict[str, list[int]] = {}
        for i, f in enumerate(self.feats):
            for k in {*f["core"], *f["words"], *f["forms"], *f["lasts"]}:
                self.index.setdefault(k, []).append(i)

    def suggest(self, email: str, name: str) -> list[dict]:
        """The best three, strongest first; people may carry "list" (another list's people: say so)."""
        e, dn = _features(email, ""), _tokens(name)
        seen, out = set(), []
        for k in {*e["core"], "".join(e["core"]), *dn}:
            for i in self.index.get(k, ()):
                if i in seen:
                    continue
                seen.add(i)
                u = self.people[i]
                if u["email"] == email:
                    continue
                score, why = _score(e, dn, self.feats[i])
                if score:
                    out.append({**u, "why": why, "score": score})
        out.sort(key=lambda x: (-x["score"], x["name"].lower()))
        if out and out[0]["score"] >= 4:   # a strong match: don't crowd it with weak ones
            out = [x for x in out if x["score"] >= 3]
        return out[:3]


def suggest(email: str, name: str, people: list[dict]) -> list[dict]:
    return Matcher(people).suggest(email, name)


def _unmatched(items: list[dict]) -> dict[str, list[dict]]:
    """Device email → its devices, for each email Jamf or Intune gives that isn't on this list or the
    other one (nor linked)."""
    on_users = {u["email"] for u in items}
    links = book.links()
    elsewhere = known_owners(skip=auth.LIST.get())   # someone on the other list: not a mismatch here
    by_email: dict[str, list[dict]] = {}
    for d in book.devices():
        if d["user_email"] and owner_email(d, links) not in on_users and d["user_email"] not in elsewhere:
            by_email.setdefault(d["user_email"], []).append(d)
    return by_email


def mismatches(items: list[dict] | None = None) -> dict:
    """devices: each unmatched device email, with its devices and who it may be. users: everyone on
    this list with no device, with any device emails that may be theirs."""
    items = items if items is not None else _users()
    by_email = _unmatched(items)
    here = auth.LIST.get()
    people = [{"email": u["email"], "name": u["name"], "list": here} for u in items]
    for key, ls in all_lists():   # someone on the other list may be who it is (link it there)
        if key != here:
            people += [{"email": u["email"], "name": u["name"] or access.display_name(u["email"]), "list": key} for u in ls.users()]
    match = Matcher(people)
    devices, maybe = [], {}
    for email, ds in sorted(by_email.items()):
        name = next((d["user_name"] for d in ds if d["user_name"]), "")
        sug = [{**x, "listName": LISTS[x["list"]][0], "linkUrl": f"/{x['list']}/api/links"} for x in match.suggest(email, name)]
        devices.append({"email": email, "name": name, "devices": [_device_view(d) for d in by_check_in(ds)], "suggestions": sug})
        for s_ in sug:
            if s_["list"] != here:
                continue
            maybe.setdefault(s_["email"], []).append({"email": email, "name": name, "why": s_["why"], "score": s_["score"],
                                                      "devices": len(ds)})
    users = [{"email": u["email"], "name": u["name"], "department": u["department"], "type": u["type"],
              "suggestions": sorted(maybe.get(u["email"], []), key=lambda x: -x["score"])[:3]}
             for u in items if not u["devices"]]
    return {"devices": devices, "users": users}


class LinkIn(BaseModel):
    fromEmail: str = Field(max_length=254)   # the email on the devices
    toEmail: str = Field(max_length=254)     # the user on Users they belong to


@users_app.get("/mismatches")
def mismatches_page(request: Request):
    if not auth.current_user(request, auth.LIST.get()):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "mismatches.html", headers={"Cache-Control": "no-store"})


def _link_list() -> list[dict]:
    names = {u["email"]: u["name"] or access.display_name(u["email"]) for u in book.users()}
    devs = book.devices()
    return [{"fromEmail": r["from_email"], "toEmail": r["to_email"], "toName": names.get(r["to_email"], ""),
             "gone": r["to_email"] not in names, "createdAt": r["created_at"], "createdBy": r["created_by"],
             "devices": sum(d["user_email"] == r["from_email"] for d in devs)}
            for r in book.link_rows()]


@users_app.get("/api/mismatches")
def get_mismatches(user: User = Depends(auth.users_viewer)):
    return {**mismatches(), "links": _link_list()}


@users_app.get("/api/mismatches.csv")
def mismatches_csv(tab: str = "devices", user: User = Depends(auth.users_viewer)):
    """One tab of Mismatches as a CSV: devices (a row per device), users or links."""
    stamp = f"{datetime.now(timezone.utc):%Y%m%d-%H%M}"
    prefix = "users-ct-" if auth.LIST.get() == USERS_CT else ""
    sug = lambda ss: "; ".join(f"{x['name'] or x['email']} <{x['email']}> ({x['why']}{', on ' + x['listName'] if x.get('listName') and x.get('list') != auth.LIST.get() else ''})" for x in ss)
    if tab == "users":
        rows = [{**u, "suggest": sug(u["suggestions"])} for u in mismatches()["users"]]
        cols = [("email", "Email"), ("name", "Name"), ("department", "Department"), ("type", "User type"),
                ("suggest", "Device emails that may be theirs")]
        return _csv(cols, rows, f"{prefix}mismatches-users-{stamp}.csv")
    if tab == "links":
        cols = [("fromEmail", "Device email"), ("toEmail", "Linked to"), ("toName", "Name"), ("devices", "Devices"),
                ("createdAt", "Linked at"), ("createdBy", "Linked by")]
        return _csv(cols, _link_list(), f"{prefix}mismatches-links-{stamp}.csv")
    if tab != "devices":
        raise HTTPException(422, "tab is devices, users or links.")
    rows = []
    for m in mismatches()["devices"]:
        first = (m["suggestions"] or [{}])[0]
        for d in m["devices"]:
            rows.append({"email": m["email"], "name": m["name"], "device": d["name"] or d["serial"], "serial": d["serial"],
                         "managedBy": d["platform"], "model": d["model"], "os": " ".join(filter(None, [d["os"], d["osVersion"]])),
                         "lastSeen": d["lastSeen"], "s1": first.get("email", ""), "s1name": first.get("name", ""),
                         "s1why": first.get("why", "") + (f" (on {first['listName']})" if first.get("list") and first["list"] != auth.LIST.get() else ""),
                         "more": sug(m["suggestions"][1:])})
    cols = [("email", "Device email"), ("name", "Name in Jamf / Intune"), ("device", "Device"), ("serial", "Serial"),
            ("managedBy", "Managed by"), ("model", "Model"), ("os", "OS"), ("lastSeen", "Last check-in"),
            ("s1", "Best suggestion"), ("s1name", "Suggested name"), ("s1why", "Why"), ("more", "Other suggestions")]
    return _csv(cols, rows, f"{prefix}mismatches-devices-{stamp}.csv")


@users_app.post("/api/links")
def add_link(body: LinkIn, user: User = Depends(auth.users_editor)):
    src, dst = clean_email(body.fromEmail), clean_email(body.toEmail)
    if not EMAIL.match(src) or not EMAIL.match(dst):
        raise ValueError("Both need to be email addresses.")
    if src == dst:
        raise ValueError("That's the same email.")
    if not book.user(dst):
        raise HTTPException(404, f"{dst} isn't on Users.")
    if book.user(src):
        raise ValueError(f"{src} is on Users itself, so its devices are already theirs.")
    book.add_link(src, dst, user.email)
    log.info(json.dumps({"severity": "NOTICE", "message": "email linked", "from": src, "to": dst, "by": user.email}))
    # Someone with no primary device gets the one that checked in last of their devices now.
    u = book.user(dst)
    ids = {d["id"] for d in book.devices()}
    if not (u["primary_device"] and u["primary_device"] in ids):
        links = book.links()
        if pick := pick_primary([d for d in book.devices() if d["user_email"] and owner_email(d, links) == dst]):
            book.save_users([{"email": dst, "primary_device": pick["id"]}], user.email)
    return {"ok": True}


@users_app.delete("/api/links/{email}")
def drop_link(email: str, user: User = Depends(auth.users_editor)):
    if not book.drop_link(clean_email(email)):
        raise HTTPException(404, "No such link.")
    log.info(json.dumps({"severity": "NOTICE", "message": "email unlinked", "from": email, "by": user.email}))
    return {"ok": True}


@users_app.get("/review")
def review_page(request: Request):
    if not auth.current_user(request, auth.LIST.get()):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "review.html", headers={"Cache-Control": "no-store"})


@users_app.get("/api/review")
def get_review(user: User = Depends(auth.users_viewer)):
    items = book.review()
    counts = {k: sum(i["kind"] == k for i in items) for k in ("new", "changed", "left")}
    first = items[0] if items else {}
    return {"items": items, "counts": counts, "importedAt": first.get("created_at"), "importedBy": first.get("created_by", "")}


class ReviewAct(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_ROWS)
    action: str = Field(pattern="^(accept|delete|keep)$")


@users_app.post("/api/review/act")
def act_on_review(body: ReviewAct, user: User = Depends(auth.users_editor)):
    """accept: new → add them; changed → make the changes; left → delete them (they've gone).
    delete: new → don't add them; changed or left → delete the user (into the recycle bin).
    keep:   leave Users as it is and take it off the list."""
    want = set(body.ids)
    items = [i for i in book.review() if i["id"] in want]
    existing = {u["email"] for u in book.users()}
    save, remove = [], []
    for i in items:
        if body.action == "accept":
            if i["kind"] == "left":
                remove.append(i["email"])
            elif i["kind"] == "new" or i["email"] in existing:   # a changed user deleted since: nothing to change
                save.append({"email": i["email"], **i["fields"]})
        elif body.action == "delete" and i["kind"] != "new":
            remove.append(i["email"])
    added, updated = book.save_users(save, user.email) if save else (0, 0)
    deleted = book.delete_users(remove, by=user.email) if remove else 0
    book.drop_review([i["id"] for i in items])
    log.info(json.dumps({"severity": "NOTICE", "message": f"users review: {body.action}", "by": user.email,
                         "items": len(items), "added": added, "updated": updated, "deleted": deleted}))
    return {"done": len(items), "added": added, "updated": updated, "deleted": deleted, "left": book.review_count()}


@users_app.post("/api/match-devices")
def match_devices(user: User = Depends(auth.users_editor)):
    """For each user without a primary device: of the devices Jamf or Intune says are theirs, the
    computer that checked in most recently (or, with no computer, the device that did). Their other
    devices are listed with them anyway (see _user_view)."""
    mine: dict[str, list[dict]] = {}
    links = book.links()
    for d in book.devices():
        if d["user_email"]:
            mine.setdefault(owner_email(d, links), []).append(d)
    ids = {d["id"] for ds in mine.values() for d in ds}
    rows, several = [], 0
    for u in book.users():
        if u["primary_device"] and u["primary_device"] in ids:
            continue
        ds = mine.get(u["email"], [])
        pick = pick_primary(ds)
        if pick:
            rows.append({"email": u["email"], "primary_device": pick["id"]})
            several += len(ds) > 1
    _, updated = book.save_users(rows, user.email)
    return {"matched": updated, "several": several}


# ---------- Org chart and Departments: read-only views of Users, each with its own access ----------

def _view_app(title: str, tool: str, page: str, viewer) -> FastAPI:
    sub = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    sub.mount("/static", _Static(directory=STATIC), name="static")
    sub.add_exception_handler(HTTPException, _http_error)

    @sub.get("/")
    def _page_(request: Request):
        if not auth.current_user(request, tool):
            return _page(access.sign_in_message(), 403)
        return FileResponse(STATIC / page, headers={"Cache-Control": "no-store"})

    @sub.get("/api/me")
    def _me(user: User = Depends(viewer)):
        return {"email": user.email, "name": user.name, "level": user.level}

    @sub.get("/api/users")
    def _users_(user: User = Depends(viewer)):
        return users_payload()

    return sub


org_app = _view_app("Nscale Org chart", ORG, "org.html", auth.org_viewer)
departments_app = _view_app("Nscale Departments", DEPARTMENTS, "departments.html", auth.departments_viewer)


# ---------- One app, four paths ----------

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


def _for_list(sub, key: str):
    """The users app, for one list: every request through it reads and writes that list."""
    async def asgi(scope, receive, send):
        token = auth.LIST.set(key)
        try:
            await sub(scope, receive, send)
        finally:
            auth.LIST.reset(token)
    return asgi


for _path, _sub in (("/devices", devices_app), ("/users", _for_list(users_app, USERS)), ("/users-ct", _for_list(users_app, USERS_CT)),
                    ("/org", org_app), ("/departments", departments_app)):
    app.add_api_route(_path, (lambda p: lambda: RedirectResponse(p + "/"))(_path), include_in_schema=False)
    app.mount(_path, _sub)
