"""Nscale Restart Reminder report, on Client Platforms.

Devices:  POST /api/reports        Authorization: Bearer <ingest key>
          {"reports": [ ... ]}, up to 200 per call (see records.Report for the fields). Answers
          {"results": [{"reportId", "status": created|updated|duplicate|rejected, "error"?}]},
          400 if the body isn't that shape, 401 for a missing or unknown key.
          GET /api/policy           the restart policy, so devices can follow the site's settings
People:   /                         the page (signed in at the front door, see auth.py)
          /api/devices              every device and its state; /api/devices.csv to download
          /api/devices/{key}        one device, its reports and its restarts
          /api/restarts             restarts in the last N days

All of it sits under BASE_PATH (/restart).

Settings: INGEST_KEYS (label:key, one per line or comma separated), DATABASE_PATH, MAX_UPTIME_DAYS,
COUNTDOWN_HOURS, LATEST_SCHEDULE_TIME, SILENT_HOURS, AUTH_MODE, AUTH_SECRET, PORTAL_URL, BASE_PATH.
"""
import csv
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

from . import access, auth, devicekeys, keyguard
from .auth import User, require_user
from .records import Policy, iso, merge, parse, status, utc_now, validate
from .store import Store

BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
MAX_BODY = 1024 * 1024
MAX_REPORTS = 200
STATIC = Path(__file__).parent / "static"
POLICY_SETTINGS = {
    "maxUptimeDays": float(os.environ.get("MAX_UPTIME_DAYS", "3")),
    "countdownHours": float(os.environ.get("COUNTDOWN_HOURS", "3")),
    "latestScheduleTime": os.environ.get("LATEST_SCHEDULE_TIME", "17:00"),
}
policy = Policy(POLICY_SETTINGS["maxUptimeDays"], POLICY_SETTINGS["countdownHours"],
                float(os.environ.get("SILENT_HOURS", "48")))


def _keys() -> list[tuple[str, str]]:
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

log = logging.getLogger("restart")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_h)
log.propagate = False

auth.check_settings()
store = Store(os.environ.get("DATABASE_PATH", "/data/restart.db"))
# Ingest keys: INGEST_KEYS from .env, plus those made on the site (Settings → Applications).
KEYS = devicekeys.DeviceKeys(os.environ.get("DATABASE_PATH", "/data/restart.db"), INGEST_KEYS, "cpr_")
app = FastAPI(title="Nscale Restart Reminder", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- Devices ----------

guard = keyguard.KeyGuard("restart")


def key_label(request: Request) -> str:
    ip = keyguard.client_ip(request)
    if guard.blocked(ip):
        raise HTTPException(429, "Too many wrong ingest keys from this address. Try again later.")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() == "bearer" and token:
        label = KEYS.check(token)
        if label:
            return label
    guard.failed(ip, "unknown key" if token else "no key")
    raise HTTPException(401, "Missing or unknown ingest key.")


@app.post("/api/reports")
async def ingest(request: Request):
    label = key_label(request)
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Too much data in one request.")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "The body must be JSON.")
    reports = data.get("reports") if isinstance(data, dict) else None
    if not isinstance(reports, list):
        raise HTTPException(400, 'The body must be {"reports": [ ... ]}.')
    if len(reports) > MAX_REPORTS:
        raise HTTPException(400, f"At most {MAX_REPORTS} reports per request.")

    received = iso(utc_now())
    ip = request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (request.client.host if request.client else "")
    results = []
    for raw in reports:
        rep, error = validate(raw)
        rid = str(raw.get("reportId", "")) if isinstance(raw, dict) else ""
        if not rep:
            results.append({"reportId": rid, "status": "rejected", "error": error})
            continue
        snap = {**rep.snapshot(), "receivedAt": received, "keyLabel": label, "sourceIp": ip}
        result = store.add(rep.device_key(), snap, lambda old: merge(old, snap, received_at=received, key_label=label))
        results.append({"reportId": rep.reportId, "status": result})
        if result != "duplicate" and snap["event"] != "status":
            log.info(json.dumps({"severity": "INFO", "message": f"restart reminder {snap['event']}: {snap['deviceName']}",
                                 "report": snap}))
    return {"results": results}


@app.get("/api/policy")
def get_policy():
    """Public, like the device API: the policy devices should follow."""
    return POLICY_SETTINGS


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- People ----------

def _page(message: str, status_code: int) -> HTMLResponse:
    from html import escape
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Restart reminder</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#F3F5FB;color:#040E27;font:14px/1.5 "Segoe UI",system-ui,sans-serif}}
.c{{background:#FDFFF8;border:1px solid #E1EAF9;border-radius:12px;padding:32px;max-width:380px;margin:16px;text-align:center}}
p{{color:#3B404E;margin:0}}</style></head><body><div class="c"><p>{escape(message)}</p></div></body></html>""",
                        status_code=status_code)


@app.get("/")
def index(request: Request):
    if not auth.current_user(request):
        return _page(access.sign_in_message(), 403)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"email": user.email, "name": user.name, "portalUrl": PORTAL_URL, "policy": POLICY_SETTINGS,
            "silentHours": policy.silent.total_seconds() / 3600}


SHOW = ("all", "ok", "countdown", "overdue", "restart_failed", "silent", "postponed")


def _view(d: dict, now) -> dict:
    last = parse(d.get("lastReportAt")) or now
    boot = parse(d.get("bootTime")) or last
    st = status(d, policy, now)
    return {"key": d["key"], "deviceName": d.get("deviceName"), "serial": d.get("serial"), "user": d.get("user"),
            "platform": d.get("platform"), "appVersion": d.get("appVersion"), "bootTime": d.get("bootTime"),
            "uptimeHours": round((last - boot).total_seconds() / 3600, 1), "status": st,
            "deadline": d.get("deadline"), "userScheduled": d.get("userScheduled"),
            "countdownStartedAt": d.get("countdownStartedAt"), "restartIssuedAt": d.get("restartIssuedAt"),
            "postponedThisBoot": d.get("postponedThisBoot") or 0, "restarts": d.get("restarts") or 0,
            "lastRestartAt": d.get("lastRestartAt"), "lastEvent": d.get("lastEvent"),
            "lastReportAt": d.get("lastReportAt"), "lastReceivedAt": d.get("lastReceivedAt"),
            "firstSeenAt": d.get("firstSeenAt"), "keyLabel": d.get("keyLabel"), "dryRun": d.get("dryRun")}


def _select(show: str, q: str) -> tuple[list[dict], dict]:
    now = utc_now()
    rows = [_view(d, now) for d in store.devices()]
    week = iso(now - timedelta(days=7))
    stats = {
        "devices": len(rows),
        "ok": sum(r["status"] == "ok" for r in rows),
        "countdown": sum(r["status"] == "countdown" for r in rows),
        "overdue": sum(r["status"] == "overdue" for r in rows),
        "restartFailed": sum(r["status"] == "restart_failed" for r in rows),
        "silent": sum(r["status"] == "silent" for r in rows),
        "postponed": sum(r["postponedThisBoot"] > 0 for r in rows),
        "restartedThisWeek": sum((r["lastRestartAt"] or "") >= week for r in rows),
    }
    if show == "postponed":
        rows = [r for r in rows if r["postponedThisBoot"] > 0]
    elif show != "all":
        rows = [r for r in rows if r["status"] == show]
    words = [w for w in q.lower().split() if w]
    if words:
        def hay(r):
            return " ".join(str(r.get(k) or "") for k in ("deviceName", "serial", "user", "keyLabel")).lower()
        rows = [r for r in rows if all(w in hay(r) for w in words)]
    order = {"restart_failed": 0, "overdue": 1, "countdown": 2, "silent": 3, "ok": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 5), -r["uptimeHours"]))
    return rows, stats


def _check_show(show: str) -> None:
    if show not in SHOW:
        raise HTTPException(422, f"show must be one of {', '.join(SHOW)}")


@app.get("/api/devices")
def list_devices(show: str = Query("all"), q: str = Query("", max_length=200), user: User = Depends(require_user)):
    _check_show(show)
    rows, stats = _select(show, q)
    return {"items": rows[:3000], "matched": len(rows), "stats": stats}


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@app.get("/api/devices.csv")
def export_csv(show: str = Query("all"), q: str = Query("", max_length=200), user: User = Depends(require_user)):
    _check_show(show)
    rows, _ = _select(show, q)
    cols = ["deviceName", "serial", "user", "status", "uptimeHours", "bootTime", "deadline", "userScheduled",
            "postponedThisBoot", "restarts", "lastRestartAt", "lastEvent", "lastReportAt", "appVersion", "keyLabel"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    name = f"restart-reminder-{utc_now():%Y%m%d-%H%M}.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/devices/{key}")
def get_device(key: str, user: User = Depends(require_user)):
    d = store.device(key.lower())
    if not d:
        raise HTTPException(404, "Device not found.")
    return {**_view(d, utc_now()), "reports": store.reports(d["key"]), "restartHistory": store.restarts(d["key"])}


@app.delete("/api/devices/{key}")
def forget_device(key: str, request: Request, user: User = Depends(require_user)):
    """For a device that's been retired. It reappears if it reports again."""
    if request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")
    if not store.forget(key.lower()):
        raise HTTPException(404, "Device not found.")
    log.info(json.dumps({"severity": "NOTICE", "message": "restart reminder device forgotten", "device": key, "by": user.email}))
    return {"ok": True}


@app.get("/api/restarts")
def list_restarts(days: int = Query(7, ge=1, le=366), user: User = Depends(require_user)):
    return {"items": store.restarts(since=iso(utc_now() - timedelta(days=days)))}


app.include_router(devicekeys.router(KEYS, auth.TOOL, log=lambda m, by: log.info(json.dumps({"severity": "NOTICE", "message": m, "by": by}))))


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


# ---------- Serving under a path on a shared host ----------
if BASE_PATH:
    _app = app
    app = FastAPI(title="Nscale Restart Reminder", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(BASE_PATH, _app)

    @app.get(BASE_PATH, include_in_schema=False)
    def _add_slash():
        return RedirectResponse(BASE_PATH + "/")

    @app.get("/healthz", include_in_schema=False)
    def _healthz():
        return {"ok": True}
