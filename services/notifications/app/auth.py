"""Who is allowed to do what.

Admins sign in with Google (OpenID Connect), handled by the app itself:
  /login            -> Google sign-in, limited to the Workspace domain (ALLOWED_DOMAIN)
  /auth/callback    -> checks the ID token, then ADMIN_EMAILS, and starts a signed session cookie
  /logout
If ADMIN_EMAILS is set, only those addresses get in. If it's empty, anyone in ALLOWED_DOMAIN does.

On Client Platforms (AUTH_MODE=local, cloudflare or iap) people sign in at the front door before a
request reaches us, and access.py checks who it vouches for on every request. There is no session
cookie or OAuth client here then; ADMIN_EMAILS still narrows who can send, if set. AUTH_MODE=google
is the standalone setup above.

BASE_PATH (e.g. /notifications) is where the app is served on a shared host. Redirects and pages use it.

Devices send a shared enrollment key in the X-Nscale-Key header.
"""
import hmac
import os
from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import access

ENROLLMENT_KEY = os.environ.get("ENROLLMENT_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "nscale.com").lower()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")   # e.g. https://notify.nscale.com
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_HOURS = 12
# Local development only: pretend this person is signed in. Never set in production.
DEV_ADMIN_USER = os.environ.get("DEV_ADMIN_USER", "")
AUTH_MODE = os.environ.get("AUTH_MODE", "local").lower()     # local | cloudflare | iap: Client Platforms; google: standalone
FRONT_DOOR = AUTH_MODE in ("local", "cloudflare", "iap")
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")      # e.g. /notifications


def url(path: str) -> str:
    """An absolute path on this app, including BASE_PATH."""
    return f"{BASE_PATH}{path}"

oauth = OAuth()
oauth.register(
    "google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@dataclass
class Admin:
    name: str
    email: str


def install(app: FastAPI) -> None:
    """Adds the session cookie and the /login, /auth/callback and /logout routes."""
    if FRONT_DOOR:
        _install_front_door(app)
        return
    if not SESSION_SECRET and not DEV_ADMIN_USER:
        raise RuntimeError("Set SESSION_SECRET (a long random string).")
    app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET or "dev-only", session_cookie="nscale_notify",
                       max_age=SESSION_HOURS * 3600, same_site="lax", https_only=not DEV_ADMIN_USER)

    @app.get("/login")
    async def login(request: Request):
        if DEV_ADMIN_USER:
            return RedirectResponse(url("/"))
        redirect_uri = f"{PUBLIC_URL}{BASE_PATH}/auth/callback" if PUBLIC_URL else str(request.url_for("auth_callback"))
        # hd asks Google to show only accounts in our Workspace domain; the callback enforces it.
        return await oauth.google.authorize_redirect(request, redirect_uri, hd=ALLOWED_DOMAIN, prompt="select_account")

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.google.authorize_access_token(request)   # verifies the ID token
        except OAuthError as e:
            return _denied(f"Sign-in failed: {e.error}")
        info = token.get("userinfo") or {}
        email = (info.get("email") or "").lower()
        if not info.get("email_verified") or info.get("hd", "").lower() != ALLOWED_DOMAIN:
            return _denied(f"Sign in with your {ALLOWED_DOMAIN} Google account.")
        if ADMIN_EMAILS and email not in ADMIN_EMAILS:
            return _denied(f"{email} isn't allowed to send IT notifications. Ask IT to add you.")
        request.session.clear()
        request.session["admin"] = {"name": info.get("name") or email, "email": email}
        return RedirectResponse(url("/"))

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url("/signed-out"))

    @app.get("/signed-out")
    async def signed_out():
        return _page("You're signed out.", "Sign in again")


def _install_front_door(app: FastAPI) -> None:
    """Signing in and out happens on the Client Platforms home page, not here."""
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
<p>{escape(message)}</p><a href="{"/logout" if FRONT_DOOR else url("/login")}">{escape(button)}</a></div></body></html>""", status_code=status)


def _denied(message: str) -> HTMLResponse:
    return _page(message, "Try a different account", 403)


def not_signed_in_page() -> HTMLResponse:
    """What the admin page shows with no valid sign-in from the front door (only reachable by going round it)."""
    if FRONT_DOOR:
        return _denied(access.sign_in_message())
    return RedirectResponse(url("/login"))


def current_admin(request: Request) -> Admin | None:
    if DEV_ADMIN_USER:
        return Admin(name=DEV_ADMIN_USER, email=DEV_ADMIN_USER)
    if FRONT_DOOR:
        who = access.identity(request, "notifications")
        if not who or (ADMIN_EMAILS and who["email"] not in ADMIN_EMAILS):
            return None
        return Admin(name=who["name"], email=who["email"])
    a = request.session.get("admin") if "session" in request.scope else None
    if not a:
        return None
    # Re-check on every request, so removing someone from ADMIN_EMAILS takes effect at once.
    if ADMIN_EMAILS and a["email"] not in ADMIN_EMAILS:
        return None
    return Admin(name=a["name"], email=a["email"])


def require_admin(request: Request) -> Admin:
    """For admin API calls. Also blocks cross-site requests: browsers only send the custom
    header from our own page (no CORS is allowed)."""
    admin = current_admin(request)
    if not admin:
        raise HTTPException(401, access.sign_in_message() if FRONT_DOOR else "Sign in with your Nscale Google account.")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")
    return admin


def require_device(x_nscale_key: str = Header(default="")) -> None:
    if not ENROLLMENT_KEY or not hmac.compare_digest(x_nscale_key.encode(), ENROLLMENT_KEY.encode()):
        raise HTTPException(401, "Bad enrollment key.")
