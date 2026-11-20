"""Who is allowed to do what.

People sign in at the Client Platforms front door (the portal) before a request reaches us, and
access.py checks who it vouches for on every request. Signing in and out happens on the portal's
pages, not here. ADMIN_EMAILS, if set, narrows who can send to that list.

BASE_PATH (e.g. /notifications) is where the app is served on a shared host. Redirects and pages use it.

Devices send an enrollment key in the X-Nscale-Key header: ENROLLMENT_KEY from .env, or one made on
the site (Settings → Site → Device settings; see devicekeys.py).
"""
import os
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import access, devicekeys, keyguard

ENROLLMENT_KEY = os.environ.get("ENROLLMENT_KEY", "")
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
# Local development only: pretend this person is signed in. Never set in production.
DEV_ADMIN_USER = os.environ.get("DEV_ADMIN_USER", "")
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")      # e.g. /notifications
KEYS = devicekeys.DeviceKeys(os.environ.get("DATABASE_PATH", "./notify.db"),
                             [("server", ENROLLMENT_KEY)] if ENROLLMENT_KEY else [], "cpn_")


def url(path: str) -> str:
    """An absolute path on this app, including BASE_PATH."""
    return f"{BASE_PATH}{path}"


@dataclass
class Admin:
    name: str
    email: str


def install(app: FastAPI) -> None:
    """/login and /logout send people to the portal, where signing in and out happens."""
    if not DEV_ADMIN_USER:
        access.check_settings()

    @app.get("/login")
    async def login():
        return RedirectResponse(url("/"))

    @app.get("/logout")
    async def logout():
        return RedirectResponse("/logout")


def _page(message: str, button: str, status: int = 200) -> HTMLResponse:
    from html import escape
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nscale IT Notifications</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#F3F5FB;color:#040E27;font:14px/1.5 "Segoe UI",system-ui,sans-serif}}
.c{{background:#FDFFF8;border:1px solid #E1EAF9;border-radius:12px;padding:32px;max-width:380px;margin:16px;text-align:center}}
img{{height:16px;margin-bottom:20px}}p{{color:#3B404E;margin:0 0 20px}}
a{{display:inline-block;background:#0F41F3;color:#FDFFF8;text-decoration:none;font-weight:600;border-radius:8px;padding:10px 22px}}
a:hover{{background:#0B34C8}}</style></head><body><div class="c"><img src="{BASE_PATH}/static/nscale-wordmark.svg" alt="Nscale">
<p>{escape(message)}</p><a href="/logout">{escape(button)}</a></div></body></html>""", status_code=status)


def _denied(message: str) -> HTMLResponse:
    return _page(message, "Try a different account", 403)


def not_signed_in_page() -> HTMLResponse:
    """What the admin page shows with no valid sign-in from the front door (only reachable by going round it)."""
    return _denied(access.sign_in_message())


def current_admin(request: Request) -> Admin | None:
    if DEV_ADMIN_USER:
        return Admin(name=DEV_ADMIN_USER, email=DEV_ADMIN_USER)
    who = access.identity(request, "notifications")
    if not who or (ADMIN_EMAILS and who["email"] not in ADMIN_EMAILS):
        return None
    return Admin(name=who["name"], email=who["email"])


def require_admin(request: Request) -> Admin:
    """For admin API calls. Also blocks cross-site requests: browsers only send the custom
    header from our own page (no CORS is allowed)."""
    admin = current_admin(request)
    if not admin:
        raise HTTPException(401, access.sign_in_message())
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")
    return admin


guard = keyguard.KeyGuard("notifications")


def require_device(request: Request, x_nscale_key: str = Header(default="")) -> None:
    ip = keyguard.client_ip(request)
    if guard.blocked(ip):
        raise HTTPException(429, "Too many wrong enrollment keys from this address. Try again later.")
    if not KEYS.check(x_nscale_key):
        guard.failed(ip, "unknown key" if x_nscale_key else "no key")
        raise HTTPException(401, "Bad enrollment key.")
