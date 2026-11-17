"""Nscale admin elevation log, on Client Platforms.

Devices:  POST /api/elevations/ingest   Authorization: Bearer <ingest key>
          The same request the worker already sends to TechOps: {"events": [ ... ]}, up to 500 per
          call. Answers {"results": [{"eventId", "status": created|updated|rejected, "error"?}]},
          400 if the body isn't that shape, 401 for a missing or unknown key.
People:   /                             the log (Google sign-in through IAP, see iap.py)
          /api/elevations               JSON for the page, /api/elevations.csv to download
          /api/elevations/{eventId}     one record and every send received for it

All of it sits under BASE_PATH (/elevation), so the worker's TechOps-style address is
https://clientplatforms.nscale.com/elevation.

Settings: INGEST_KEYS (label:key, one per line or comma separated; a secret), STORE, FIRESTORE_DATABASE,
IAP_AUDIENCE, ALLOWED_DOMAIN, VIEWER_EMAILS, PORTAL_URL, BASE_PATH.
"""
import csv
import hmac
import io
import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import iap, records
from .iap import User, require_user
from .records import iso, merge, status, utc_now, validate
from .store import make_store

BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
MAX_BODY = 2 * 1024 * 1024
MAX_EVENTS = 500
LIST_LIMIT = 5000
STATIC = Path(__file__).parent / "static"


def _keys() -> list[tuple[str, str]]:
    """INGEST_KEYS as (label, key). A line with no label gets 'default'."""
    out = []
    for part in os.environ.get("INGEST_KEYS", "").replace(",", "\n").splitlines():
        part = part.strip()
        if not part:
            continue
        label, sep, key = part.partition(":")
        if not sep:
            label, key = "default", part
        if len(key.strip()) >= 24:
            out.append((label.strip() or "default", key.strip()))
    return out


INGEST_KEYS = _keys()

# One JSON line per received record goes to stdout, so Cloud Logging holds every elevation too.
log = logging.getLogger("elevation")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_h)
log.propagate = False

iap.check_settings()
store = make_store()
app = FastAPI(title="Nscale Elevation Log", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- Devices ----------

def key_label(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    token = token.strip()
    if scheme.lower() == "bearer" and token:
        for label, key in INGEST_KEYS:   # compare against every key, so timing says nothing
            if hmac.compare_digest(token.encode(), key.encode()):
                return label
    raise HTTPException(401, "Missing or unknown ingest key.")


def client_ip(request: Request) -> str:
    # The Google load balancer appends "<client>, <load balancer>" to X-Forwarded-For; anything
    # before those came from the client and can't be trusted.
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else (request.client.host if request.client else "")


@app.post("/api/elevations/ingest")
async def ingest(request: Request):
    label = key_label(request)
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Too much data in one request.")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "The body must be JSON.")
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise HTTPException(400, 'The body must be {"events": [ ... ]}.')
    if len(events) > MAX_EVENTS:
        raise HTTPException(400, f"At most {MAX_EVENTS} events per request.")

    received = iso(utc_now())
    ip = client_ip(request)
    results = []
    for raw in events:
        ev, error = validate(raw)
        eid = str(raw.get("eventId", "")) if isinstance(raw, dict) else ""
        if not ev:
            results.append({"eventId": eid, "status": "rejected", "error": error})
            log.info(json.dumps({"severity": "WARNING", "message": "elevation record rejected", "eventId": eid,
                                 "error": error, "keyLabel": label, "sourceIp": ip}))
            continue
        old, new = store.upsert(ev.eventId, lambda o: merge(o, ev, received_at=received, key_label=label,
                                                            source_ip=ip))
        results.append({"eventId": ev.eventId, "status": "updated" if old else "created"})
        flagged = new.get("changed") or new.get("outOfOrder")
        log.info(json.dumps({"severity": "WARNING" if flagged else "INFO",
                             "message": f"elevation {new.get('outcome')}: {new.get('user')} on {new.get('deviceName')}",
                             "elevation": {k: new.get(k) for k in records.FIELDS},
                             "changed": new.get("changed"), "outOfOrder": new.get("outOfOrder"),
                             "keyLabel": label, "sourceIp": ip}))
    return {"results": results}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- People ----------

def _page(message: str, status_code: int) -> HTMLResponse:
    from html import escape
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Elevation log</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#F3F5FB;color:#040E27;font:14px/1.5 "Segoe UI",system-ui,sans-serif}}
.c{{background:#FDFFF8;border:1px solid #E1EAF9;border-radius:12px;padding:32px;max-width:380px;margin:16px;text-align:center}}
p{{color:#3B404E;margin:0}}</style></head><body><div class="c"><p>{escape(message)}</p></div></body></html>""",
                        status_code=status_code)


@app.get("/")
def index(request: Request):
    if not iap.current_user(request):
        return _page(f"Sign in with your {iap.ALLOWED_DOMAIN} Google account at the Client Platforms address.", 403)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"email": user.email, "name": user.name, "portalUrl": PORTAL_URL,
            "notEndedGraceMinutes": int(records.NOT_ENDED_GRACE.total_seconds() // 60)}


FILTERS = ("all", "granted", "denied", "ended", "active", "not_ended", "flagged")


def _view(r: dict, now) -> dict:
    out = {k: r.get(k) for k in records.FIELDS}
    out.update(status=status(r, now), changed=r.get("changed") or [], outOfOrder=r.get("outOfOrder") or 0,
               sends=r.get("sends") or 1, firstReceivedAt=r.get("firstReceivedAt"),
               lastReceivedAt=r.get("lastReceivedAt"), keyLabel=r.get("keyLabel"))
    return out


def _select(days: int, show: str, q: str) -> tuple[list[dict], dict]:
    now = utc_now()
    rows = [_view(r, now) for r in store.list_since(iso(now - timedelta(days=days)), LIST_LIMIT)]
    stats = {
        "total": len(rows),
        "granted": sum(r["outcome"] != "denied" for r in rows),
        "denied": sum(r["outcome"] == "denied" for r in rows),
        "active": sum(r["status"] == "active" for r in rows),
        "notEnded": sum(r["status"] == "not_ended" for r in rows),
        "flagged": sum(bool(r["changed"] or r["outOfOrder"]) for r in rows),
        "people": len({(r["user"] or "").lower() for r in rows}),
        "devices": len({(r["serial"] or r["deviceName"] or "").lower() for r in rows}),
        "truncated": len(rows) >= LIST_LIMIT,
    }
    if show == "granted":
        rows = [r for r in rows if r["outcome"] != "denied"]
    elif show == "ended":
        rows = [r for r in rows if r["outcome"] in records.ENDED]
    elif show == "flagged":
        rows = [r for r in rows if r["changed"] or r["outOfOrder"]]
    elif show in ("denied", "active", "not_ended"):
        rows = [r for r in rows if r["status"] == show]
    words = [w for w in q.lower().split() if w]
    if words:
        def hay(r):
            return " ".join(str(r.get(k) or "") for k in ("user", "deviceName", "serial", "reason", "eventId")).lower()
        rows = [r for r in rows if all(w in hay(r) for w in words)]
    return rows, stats


@app.get("/api/elevations")
def list_elevations(days: int = Query(30, ge=1, le=366), show: str = Query("all"), q: str = Query("", max_length=200),
                    user: User = Depends(require_user)):
    if show not in FILTERS:
        raise HTTPException(422, f"show must be one of {', '.join(FILTERS)}")
    rows, stats = _select(days, show, q)
    return {"items": rows[:2000], "matched": len(rows), "stats": stats}


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s   # Excel must not run it as a formula


@app.get("/api/elevations.csv")
def export_csv(days: int = Query(30, ge=1, le=366), show: str = Query("all"), q: str = Query("", max_length=200),
               user: User = Depends(require_user)):
    if show not in FILTERS:
        raise HTTPException(422, f"show must be one of {', '.join(FILTERS)}")
    rows, _ = _select(days, show, q)
    cols = ["requestedAt", "user", "deviceName", "serial", "platform", "durationMinutes", "reason", "outcome",
            "status", "grantedAt", "expiresAt", "changed", "outOfOrder", "sends", "firstReceivedAt",
            "lastReceivedAt", "keyLabel", "eventId"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(" ".join(r[c]) if c == "changed" else r.get(c)) for c in cols])
    log.info(json.dumps({"severity": "NOTICE", "message": "elevation log exported", "by": user.email,
                         "rows": len(rows), "days": days, "show": show, "q": q}))
    name = f"elevations-{utc_now():%Y%m%d-%H%M}.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/elevations/{event_id}")
def get_elevation(event_id: str, user: User = Depends(require_user)):
    r = store.get(event_id.lower())
    if not r:
        raise HTTPException(404, "Record not found.")
    out = _view(r, utc_now())
    out["first"] = r.get("first") or {}
    out["firstKeyLabel"] = r.get("firstKeyLabel")
    out["history"] = store.history(event_id.lower())
    return out


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


# ---------- Serving under a path on a shared host ----------
if BASE_PATH:
    _app = app
    app = FastAPI(title="Nscale Elevation Log", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(BASE_PATH, _app)

    @app.get(BASE_PATH, include_in_schema=False)
    def _add_slash():
        return RedirectResponse(BASE_PATH + "/")

    @app.get("/healthz", include_in_schema=False)
    def _healthz():
        return {"ok": True}
