"""Nscale IT Notifications server.

Devices:  POST /api/v1/checkin   (X-Nscale-Key)  -> new messages for this device; uploads receipts
Admins:   /                       branded page to send messages and see who has read them
          /api/admin/...          JSON API, signed in at the front door (see auth.py)

With BASE_PATH=/notifications every path above sits under it (as on clientplatforms.nscale.com), and
devices use https://clientplatforms.nscale.com/notifications as their server address.
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth, devicekeys, richtext
from .auth import Admin, current_admin, require_admin, require_device
from .store import device_key, make_store

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "72"))   # new devices don't get older messages
LOOKBACK_DAYS = int(os.environ.get("MESSAGE_LOOKBACK_DAYS", "14"))   # messages older than this are never delivered
LAST_SEEN_WRITE_SECONDS = int(os.environ.get("LAST_SEEN_WRITE_SECONDS", "240"))   # fewer database writes
ALLOWED_LINK_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_LINK_HOSTS", "nscale.com").split(",") if h.strip()]
ACTIVE_DAYS = 30                                                # devices seen within this count as active
ONLINE_SECONDS = max(POLL_SECONDS * 3, LAST_SEEN_WRITE_SECONDS + POLL_SECONDS * 2)
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Nscale IT Notifications", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
auth.install(app)
store = make_store()


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- Targeting ----------

def device_names(d) -> set[str]:
    names = {d.get("hostname"), d.get("username"), d.get("upn")}
    return {n.lower() for n in names if n}


def targets(message, device) -> bool:
    platforms = message.get("platforms") or []
    if platforms and device.get("platform") not in platforms:
        return False
    audience = message.get("audience") or []
    if not audience:
        return True
    names = device_names(device)
    # Entries match computer name, user name or UPN, and may use * wildcards (e.g. LT-LON-*).
    return any(fnmatchcase(n, a.lower()) for a in audience for n in names)


# ---------- Device API ----------

class Event(BaseModel):
    messageId: str = Field(max_length=120)
    event: Literal["delivered", "opened", "acknowledged"]
    at: datetime | None = None


class CheckIn(BaseModel):
    deviceId: str = Field(min_length=8, max_length=64)
    hostname: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=128)
    upn: str | None = Field(default=None, max_length=256)
    platform: Literal["windows", "macos"]
    osVersion: str | None = Field(default=None, max_length=128)
    appVersion: str | None = Field(default=None, max_length=32)
    events: list[Event] = Field(default_factory=list, max_length=500)


@app.post("/api/v1/checkin", dependencies=[Depends(require_device)])
def checkin(body: CheckIn):
    t = now()
    ts = iso(t)
    username = body.username.lower()
    key = device_key(body.deviceId, username)
    info = {"device_id": body.deviceId, "username": username, "hostname": body.hostname,
            "upn": (body.upn or "").lower() or None, "platform": body.platform,
            "os_version": body.osVersion, "app_version": body.appVersion}

    device = store.get_device(key)
    if device is None:
        device = {**info, "key": key, "first_seen": ts, "last_seen": ts}
        store.save_device(device)
    else:
        changed = any(device.get(k) != v for k, v in info.items())
        stale = (t - parse(device["last_seen"])).total_seconds() >= LAST_SEEN_WRITE_SECONDS
        if changed or stale or body.events:
            device.update(info, last_seen=ts)
            store.save_device(device)

    for e in body.events:
        if store.get_message(e.messageId):
            at = iso(e.at.astimezone(timezone.utc)) if e.at and e.at.tzinfo else ts
            store.record_event(device, e.messageId, e.event, at)

    delivered = store.delivered_ids(device)
    since = max(parse(device["first_seen"]) - timedelta(hours=MAX_AGE_HOURS), t - timedelta(days=LOOKBACK_DAYS))
    messages = [m for m in store.recent_messages(iso(since))
                if m["id"] not in delivered and not (m.get("expires_at") and m["expires_at"] <= ts)
                and targets(m, device)]
    return {"pollSeconds": POLL_SECONDS, "messages": [message_for_device(m) for m in messages]}


def rich_of(m) -> str:
    """Older messages have no rich text; their plain body is the same thing escaped."""
    return m.get("body_rich") or richtext.escape(m.get("body") or "")


def link_allowed(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (not ALLOWED_LINK_HOSTS or
                                    any(host == h or host.endswith("." + h) for h in ALLOWED_LINK_HOSTS))


def message_for_device(m) -> dict:
    out = {"id": m["id"], "subject": m["subject"], "body": m.get("body") or "", "bodyRich": rich_of(m),
           "priority": m["priority"], "sent": m["created_at"], "expires": m.get("expires_at"), "from": m["from_name"]}
    if m.get("link_url"):
        out["link"] = {"text": m["link_text"] or "Open link", "url": m["link_url"]}
    return out


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- Admin page ----------

@app.get("/")
def index(request: Request):
    if not current_admin(request):
        return auth.not_signed_in_page()
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ---------- Admin API ----------

class NewMessage(BaseModel):
    subject: str = Field(min_length=1, max_length=140)
    body: str = Field(default="", max_length=4000)        # plain text (used if bodyRich is empty)
    bodyRich: str = Field(default="", max_length=12000)   # <b> <i> <u> <a href> only, see richtext.py
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    fromName: str = Field(default="Nscale IT", min_length=1, max_length=60)
    expiresInHours: float | None = Field(default=None, gt=0, le=24 * 90)
    audience: list[str] = Field(default_factory=list, max_length=500)
    platforms: list[Literal["windows", "macos"]] = Field(default_factory=list)
    linkText: str | None = Field(default=None, max_length=40)
    linkUrl: str | None = Field(default=None, max_length=500)

    @field_validator("audience")
    @classmethod
    def _clean_audience(cls, v: list[str]) -> list[str]:
        return [a.strip() for a in v if a.strip()]

    @field_validator("linkUrl")
    @classmethod
    def _check_link(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not link_allowed(v):
            raise ValueError(f"Links must be https on {', '.join(ALLOWED_LINK_HOSTS)}")
        return v


class AudienceQuery(BaseModel):
    audience: list[str] = Field(default_factory=list)
    platforms: list[Literal["windows", "macos"]] = Field(default_factory=list)


def active_devices() -> list[dict]:
    return store.devices_seen_since(iso(now() - timedelta(days=ACTIVE_DAYS)))


def count_targets(audience: list[str], platforms: list[str], devices: list[dict] | None = None) -> int:
    fake = {"audience": audience, "platforms": platforms}
    return sum(1 for d in (devices if devices is not None else active_devices()) if targets(fake, d))


@app.get("/api/admin/me")
def me(admin: Admin = Depends(require_admin)):
    return {"name": admin.name, "email": admin.email, "pollSeconds": POLL_SECONDS, "onlineSeconds": ONLINE_SECONDS,
            "allowedLinkHosts": ALLOWED_LINK_HOSTS}


@app.post("/api/admin/audience")
def audience_preview(q: AudienceQuery, admin: Admin = Depends(require_admin)):
    return {"devices": count_targets(q.audience, q.platforms)}


@app.post("/api/admin/messages")
def create_message(m: NewMessage, admin: Admin = Depends(require_admin)):
    t = now()
    slug = re.sub(r"[^a-z0-9]+", "-", m.subject.lower()).strip("-")[:40].strip("-") or "message"
    mid = f"{t:%Y%m%d-%H%M%S}-{slug}-{secrets.token_hex(2)}"
    expires = iso(t + timedelta(hours=m.expiresInHours)) if m.expiresInHours else None
    if m.bodyRich.strip():
        rich, plain = richtext.sanitize(m.bodyRich, link_allowed)
    else:
        plain = m.body.strip()
        rich = richtext.escape(plain)
    if len(plain) > 4000:
        raise HTTPException(422, "The message is too long (4,000 characters at most).")
    store.create_message({
        "id": mid, "subject": m.subject.strip(), "body": plain, "body_rich": rich, "priority": m.priority,
        "from_name": m.fromName.strip(), "link_text": m.linkText if m.linkUrl else None, "link_url": m.linkUrl,
        "audience": m.audience, "platforms": m.platforms, "created_at": iso(t), "expires_at": expires,
        "sent_by": admin.email})
    return {"id": mid, "targetedDevices": count_targets(m.audience, m.platforms)}


def summarise(m: dict, devices: list[dict]) -> dict:
    d, o, a = store.counts(m)
    return {
        "id": m["id"], "subject": m["subject"], "body": m.get("body") or "", "bodyRich": rich_of(m),
        "priority": m["priority"], "from": m["from_name"],
        "link": {"text": m.get("link_text"), "url": m["link_url"]} if m.get("link_url") else None,
        "audience": m.get("audience") or [], "platforms": m.get("platforms") or [],
        "sent": m["created_at"], "expires": m.get("expires_at"), "sentBy": m["sent_by"],
        "withdrawn": m.get("withdrawn_at"), "withdrawnBy": m.get("withdrawn_by"),
        # Devices that went inactive or were renamed still count once they have a receipt.
        "targeted": max(count_targets(m.get("audience") or [], m.get("platforms") or [], devices), d),
        "delivered": d, "opened": o, "acknowledged": a,
    }


@app.get("/api/admin/messages")
def list_messages(admin: Admin = Depends(require_admin)):
    devices = active_devices()
    return {"messages": [summarise(m, devices) for m in store.list_messages(100)]}


@app.get("/api/admin/messages/{mid}")
def get_message(mid: str, admin: Admin = Depends(require_admin)):
    m = store.get_message(mid, fresh=True)
    if not m:
        raise HTTPException(404, "Message not found.")
    devices = active_devices()
    out = summarise(m, devices)
    receipts = store.receipts_for(mid)
    rows = []
    for d in devices:
        if not targets(m, d) and d["key"] not in receipts:
            continue
        r = receipts.get(d["key"]) or {}
        rows.append({"hostname": d["hostname"], "username": d["username"], "upn": d.get("upn"),
                     "platform": d["platform"], "lastSeen": d["last_seen"],
                     "delivered": r.get("delivered_at"), "opened": r.get("opened_at"),
                     "acknowledged": r.get("acknowledged_at")})
    out["devices"] = sorted(rows, key=lambda x: (x["acknowledged"] is not None, x["opened"] is not None,
                                                  x["delivered"] is not None, x["hostname"].lower()))
    return out


@app.post("/api/admin/messages/{mid}/withdraw")
def withdraw(mid: str, admin: Admin = Depends(require_admin)):
    if not store.withdraw(mid, admin.email, iso(now())):
        raise HTTPException(404, "Message not found or already withdrawn.")
    return {"ok": True}


@app.get("/api/admin/devices")
def list_devices(admin: Admin = Depends(require_admin)):
    return {"devices": [{"hostname": d["hostname"], "username": d["username"], "upn": d.get("upn"),
                         "platform": d["platform"], "osVersion": d.get("os_version"), "appVersion": d.get("app_version"),
                         "firstSeen": d["first_seen"], "lastSeen": d["last_seen"]} for d in store.list_devices(5000)],
            "pollSeconds": POLL_SECONDS, "onlineSeconds": ONLINE_SECONDS}


@app.exception_handler(ValueError)
def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


# ---------- Serving under a path on a shared host ----------
app.include_router(devicekeys.router(auth.KEYS, "notifications"))

# The page and its scripts use relative addresses, so the same app works at / and under BASE_PATH.
if auth.BASE_PATH:
    _app = app
    app = FastAPI(title="Nscale IT Notifications", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(auth.BASE_PATH, _app)

    @app.get(auth.BASE_PATH, include_in_schema=False)
    def _add_slash():
        return RedirectResponse(auth.BASE_PATH + "/")

    @app.get("/healthz", include_in_schema=False)
    def _healthz():
        return {"ok": True}
