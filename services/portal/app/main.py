"""Nscale Client Platforms: the home page, sign-in and settings.

AUTH_MODE=local (default): people sign in here with an email and password (see db.py). The front
door (Caddy, see /Caddyfile) asks /auth/check before every request to a tool, and passes on the
signed X-CP-User token this answers with (see access.py). Admins manage people, which tools each
can use, the tool links and the site and device addresses on /settings.

If CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are also set, people coming in through Cloudflare Access
are signed in by it as well, so the same site works by IP (password) and through Cloudflare (SSO).

AUTH_MODE=cloudflare: as local, but only Cloudflare Access signs people in (see cloudflare.py), so there
are no passwords here. Admins still list who may use which tool; someone Cloudflare lets in who isn't
on that list gets nothing.

AUTH_MODE=iap: Google IAP signs people in at the load balancer (Google Cloud). The home page lists
every tool; ADMIN_EMAILS can change the settings. There are no local accounts.

Settings (environment): AUTH_SECRET, DATABASE_PATH, ADMIN_EMAIL + ADMIN_PASSWORD (first admin, only
used while there are no accounts), SESSION_HOURS, PUBLIC_URL / DEVICE_URL (defaults for the settings).
"""
import os
import re
import secrets
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import access, cloudflare
from .db import DB, TOOL_IDS, TOOLS, iso, now

STATIC = Path(__file__).parent / "static"
COOKIE = "cp_session"
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "12"))
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10

MODE = "dev" if access.DEV_USER else access.AUTH_MODE
ACCOUNTS = MODE in ("local", "cloudflare")     # the people list is kept here
PASSWORDS = MODE == "local"
CLOUDFLARE = MODE == "cloudflare" or (MODE == "local" and bool(cloudflare.TEAM and cloudflare.AUD))

access.check_settings()
if MODE == "cloudflare":
    cloudflare.check_settings()
db = DB(os.environ.get("DATABASE_PATH", "/data/portal.db"))


def bootstrap() -> None:
    """The first admin, from ADMIN_EMAIL and ADMIN_PASSWORD, while there are no accounts at all."""
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "") if PASSWORDS else secrets.token_urlsafe(32)
    if ACCOUNTS and email and password and db.count_users() == 0:
        db.create_user(email, access.display_name(email), password, admin=True, tools=TOOL_IDS)


bootstrap()
app = FastAPI(title="Nscale Client Platforms", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- Who is signed in ----------

def current(request: Request) -> dict | None:
    if access.DEV_USER:
        return {"email": access.DEV_USER, "name": access.display_name(access.DEV_USER), "admin": True,
                "tools": TOOL_IDS}
    if MODE == "iap":
        who = access.identity(request, "portal")
        if not who:
            return None
        return {**who, "admin": who["email"] in ADMIN_EMAILS, "tools": TOOL_IDS}
    user = db.session_user(request.cookies.get(COOKIE, "")) if PASSWORDS else None
    if not user and CLOUDFLARE:
        email = cloudflare.email(request)
        user = db.get_user(email) if email else None
        if not user or user["disabled"]:
            return None
        if (user["lastSignIn"] or "")[:13] != iso(now())[:13]:
            db.signed_in(email)   # "last seen", to the hour
            user["lastSignIn"] = iso(now())
    return user


def not_signed_in(request: Request, nxt: str = "/") -> Response:
    """The page for someone with no (usable) sign-in."""
    if CLOUDFLARE:
        email = cloudflare.email(request)
        if email:
            return message_page(f"{email} doesn't have an account on Client Platforms yet, or it's turned off. "
                                "Ask a Client Platforms admin.", 403, "Sign out", "/logout")
    if PASSWORDS:
        nxt = safe_next(nxt)
        return RedirectResponse("/login" if nxt == "/" else "/login?next=" + quote(nxt, safe="/"))
    return message_page(access.sign_in_message(), 403)


def can_use(user: dict, tool: str) -> bool:
    return user["admin"] or tool in user["tools"]


def require_user(request: Request) -> dict:
    user = current(request)
    if not user:
        raise HTTPException(401, "Sign in first.")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")   # only our own pages send it (no CORS)
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user["admin"]:
        raise HTTPException(403, "Only admins can do that.")
    return user


def require_accounts():
    if not ACCOUNTS:
        raise HTTPException(400, "The people list is only kept here with AUTH_MODE=local or cloudflare.")


def require_passwords():
    if not PASSWORDS:
        raise HTTPException(400, "Passwords are only used with AUTH_MODE=local.")


def safe_next(n: str | None) -> str:
    n = n or "/"
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def secure(request: Request) -> bool:
    return request.url.scheme == "https"


# ---------- Sign-in ----------

_failures: dict[str, list[float]] = {}
WINDOW, MAX_PER_EMAIL, MAX_PER_ADDRESS = 15 * 60, 5, 30


def _too_many(*keys: str) -> bool:
    cutoff = time.time() - WINDOW
    for k in keys:
        _failures[k] = [t for t in _failures.get(k, []) if t > cutoff]
    return (len(_failures.get(keys[0], [])) >= MAX_PER_EMAIL or len(_failures.get(keys[1], [])) >= MAX_PER_ADDRESS)


def _failed(*keys: str) -> None:
    for k in keys:
        _failures.setdefault(k, []).append(time.time())


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if not PASSWORDS:
        return RedirectResponse("/")
    if current(request):
        return RedirectResponse(safe_next(next))
    return login_html(next)


@app.post("/login")
async def login(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
    email = (form.get("email") or "").strip().lower()[:254]
    password = form.get("password") or ""
    nxt = safe_next(form.get("next"))
    ip = request.client.host if request.client else ""
    keys = ("email:" + email, "ip:" + ip)
    if _too_many(*keys):
        return login_html(nxt, email, "Too many tries. Wait 15 minutes and try again.", 429)
    user = db.get_user(email) if email else None
    ok = PASSWORDS and db.check(password, user)
    if not ok:
        _failed(*keys)
        return login_html(nxt, email, "That email and password don't match an account.", 401)
    _failures.pop(keys[0], None)
    db.signed_in(email)
    r = RedirectResponse(nxt, status_code=303)
    r.set_cookie(COOKIE, db.start_session(email, SESSION_HOURS), max_age=int(SESSION_HOURS * 3600), path="/",
                 httponly=True, samesite="lax", secure=secure(request))
    return r


@app.get("/logout")
@app.get("/signout")
def logout(request: Request):
    if MODE == "iap":
        return RedirectResponse("/?gcp-iap-mode=CLEAR_LOGIN_COOKIE")
    if MODE == "cloudflare" or (CLOUDFLARE and request.headers.get(cloudflare.HEADER)):
        token = request.cookies.get(COOKIE)
        if token:
            db.end_session(token)
        r = RedirectResponse("/cdn-cgi/access/logout")   # Cloudflare's own sign-out
        r.delete_cookie(COOKIE, path="/")
        return r
    token = request.cookies.get(COOKIE)
    if token:
        db.end_session(token)
    r = RedirectResponse("/login")
    r.delete_cookie(COOKIE, path="/")
    return r


@app.get("/auth/check")
def auth_check(request: Request):
    """Caddy's forward_auth asks this before every request to a tool. 200 lets it through, with
    X-CP-User for the tool to check; anything else goes back to the browser as it is."""
    uri = request.headers.get("x-forwarded-uri", "/")
    method = request.headers.get("x-forwarded-method", "GET").upper()
    tool = uri.lstrip("/").split("?", 1)[0].split("/", 1)[0]
    page = method in ("GET", "HEAD") and "/api/" not in uri and "text/html" in request.headers.get("accept", "")
    user = current(request)
    if not user:
        if page:
            return not_signed_in(request, uri)
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    if tool not in TOOL_IDS or not can_use(user, tool):
        name = next((t["name"] for t in TOOLS if t["id"] == tool), "this tool")
        msg = f"You don't have access to {name}. Ask a Client Platforms admin to add it to your account."
        return message_page(msg, 403, "Back to home", "/") if page else JSONResponse({"detail": msg}, status_code=403)
    token = access.issue(user["email"], user["name"], tool, user["admin"])
    return Response(status_code=200, headers={"X-CP-User": token, "Cache-Control": "no-store"})


# ---------- Pages ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    user = current(request)
    if not user:
        return not_signed_in(request)
    tools = [t for t in db.settings()["tools"] if t["enabled"] and can_use(user, t["id"])]
    tiles = "".join(f"""
      <a class="tile" href="{escape(t['url'])}">
        <span class="ic">{ICONS[t['icon']]}</span>
        <b>{escape(t['name'])}</b><span>{escape(t['about'])}</span><i>Open →</i>
      </a>""" for t in tools) or "<p class='sub'>No tools have been given to your account yet. Ask a Client Platforms admin.</p>"
    links = ('<a href="/settings">Settings</a> · ' if user["admin"] else
             '<a href="/settings">Account</a> · ' if PASSWORDS else "")
    return HTMLResponse(PAGE.replace("{{TILES}}", tiles).replace("{{EMAIL}}", escape(user["email"]))
                        .replace("{{LINKS}}", links).replace("{{FIRST}}", escape(user["name"].split(" ")[0])),
                        headers={"Cache-Control": "no-store"})


@app.get("/settings")
def settings_page(request: Request):
    if not current(request):
        return not_signed_in(request, "/settings")
    return FileResponse(STATIC / "settings.html", headers={"Cache-Control": "no-store"})


# ---------- API: you ----------

@app.get("/api/me")
def me(user: dict = Depends(require_user)):
    tools = [t for t in db.settings()["tools"] if t["enabled"] and can_use(user, t["id"])]
    return {"email": user["email"], "name": user["name"], "admin": user["admin"], "tools": tools,
            "authMode": MODE, "accounts": ACCOUNTS, "passwords": PASSWORDS, "cloudflare": CLOUDFLARE, "minPassword": MIN_PASSWORD}


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=MIN_PASSWORD, max_length=200)


@app.post("/api/me/password", dependencies=[Depends(require_passwords)])
def change_password(body: PasswordChange, request: Request, user: dict = Depends(require_user)):
    if not db.check(body.current, db.get_user(user["email"])):
        raise HTTPException(400, "Your current password isn't right.")
    db.set_password(user["email"], body.new, keep_session=db.session_id(request.cookies.get(COOKIE, "")))
    return {"ok": True}


# ---------- API: settings ----------

URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[^\s?#]*)?$")


def _clean_base(v: str) -> str:
    v = (v or "").strip().rstrip("/")
    if v and not URL_RE.match(v):
        raise ValueError("Use a full address such as https://clientplatforms.nscale.com")
    return v


class ToolSetting(BaseModel):
    id: str
    url: str = Field(default="", max_length=500)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if v and not (URL_RE.match(v) or (v.startswith("/") and not v.startswith("//") and " " not in v)):
            raise ValueError("A tool link is a path on this site (/notifications/) or a full https:// address")
        return v


class Settings(BaseModel):
    publicUrl: str = Field(default="", max_length=300)
    deviceUrl: str = Field(default="", max_length=300)
    tools: list[ToolSetting] = Field(default_factory=list)

    @field_validator("publicUrl", "deviceUrl")
    @classmethod
    def _base(cls, v: str) -> str:
        return _clean_base(v)


def base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def device_urls(request: Request) -> dict:
    s = db.settings()
    base = s["deviceUrl"] or s["publicUrl"] or base_url(request)
    return {"server": base, **{t: f"{base}/{t}" for t in TOOL_IDS}}


@app.get("/api/settings")
def get_settings(request: Request, user: dict = Depends(require_admin)):
    s = db.settings()
    return {**s, "siteUrl": s["publicUrl"] or base_url(request), "devices": device_urls(request)}


@app.put("/api/settings")
def put_settings(body: Settings, request: Request, user: dict = Depends(require_admin)):
    current_tools = {t["id"]: t for t in db.settings()["tools"]}
    tools = {}
    for t in body.tools:
        if t.id in current_tools:
            tools[t.id] = {"url": "" if t.url == current_tools[t.id]["defaultUrl"] else t.url, "enabled": t.enabled}
    db.save_settings({"publicUrl": body.publicUrl, "deviceUrl": body.deviceUrl, "tools": tools})
    return get_settings(request, user)


@app.get("/api/device-config")
def device_config(request: Request):
    """Public: where devices should send to. Lets the device server move without repackaging apps
    that read it."""
    return device_urls(request)


# ---------- API: people ----------

def _email(v: str) -> str:
    v = v.strip().lower()
    if not EMAIL.match(v) or len(v) > 254:
        raise ValueError("Enter a valid email address")
    return v


def _tools(v: list[str]) -> list[str]:
    bad = [t for t in v if t not in TOOL_IDS]
    if bad:
        raise ValueError(f"Unknown tool: {', '.join(bad)}")
    return v


class NewUser(BaseModel):
    email: str
    name: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=200)   # not used with Cloudflare Access
    admin: bool = False
    tools: list[str] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def _e(cls, v: str) -> str:
        return _email(v)

    @field_validator("tools")
    @classmethod
    def _t(cls, v: list[str]) -> list[str]:
        return _tools(v)


class UserChange(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    admin: bool | None = None
    disabled: bool | None = None
    tools: list[str] | None = None

    @field_validator("tools")
    @classmethod
    def _t(cls, v: list[str] | None) -> list[str] | None:
        return v if v is None else _tools(v)


class NewPassword(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


@app.get("/api/users", dependencies=[Depends(require_accounts)])
def list_users(user: dict = Depends(require_admin)):
    return {"users": [db.public(u) for u in db.list_users()], "tools": [{"id": t["id"], "name": t["name"]} for t in TOOLS]}


@app.post("/api/users", dependencies=[Depends(require_accounts)])
def create_user(body: NewUser, user: dict = Depends(require_admin)):
    if db.get_user(body.email):
        raise HTTPException(409, f"{body.email} already has an account.")
    if PASSWORDS and not (body.password or CLOUDFLARE):
        raise HTTPException(422, "Give them a password.")
    if PASSWORDS and body.password and len(body.password) < MIN_PASSWORD:
        raise HTTPException(422, f"The password needs at least {MIN_PASSWORD} characters.")
    # Without a password (Cloudflare only) they get a random one nobody knows.
    password = body.password if PASSWORDS and body.password else secrets.token_urlsafe(32)
    u = db.create_user(body.email, body.name.strip() or access.display_name(body.email), password,
                       body.admin, body.tools)
    return db.public(u)


def _target(email: str) -> dict:
    u = db.get_user(email)
    if not u:
        raise HTTPException(404, "No such account.")
    return u


@app.patch("/api/users/{email}", dependencies=[Depends(require_accounts)])
def update_user(email: str, body: UserChange, user: dict = Depends(require_admin)):
    u = _target(email)
    losing_admin = u["admin"] and not u["disabled"] and (body.admin is False or body.disabled is True)
    if losing_admin and u["email"] == user["email"]:
        raise HTTPException(400, "You can't take away your own admin rights or turn off your own account.")
    if losing_admin and db.count_admins() <= 1:
        raise HTTPException(400, "There must always be at least one admin.")
    return db.public(db.update_user(u["email"], name=(body.name or "").strip() or None, admin=body.admin,
                                    disabled=body.disabled, tools=body.tools))


@app.post("/api/users/{email}/password", dependencies=[Depends(require_passwords)])
def reset_password(email: str, body: NewPassword, user: dict = Depends(require_admin)):
    u = _target(email)
    if u["email"] == user["email"]:
        raise HTTPException(400, "Change your own password under Your account.")
    db.set_password(u["email"], body.password)   # also signs them out everywhere
    return {"ok": True}


@app.delete("/api/users/{email}", dependencies=[Depends(require_accounts)])
def delete_user(email: str, user: dict = Depends(require_admin)):
    u = _target(email)
    if u["email"] == user["email"]:
        raise HTTPException(400, "You can't delete your own account.")
    if u["admin"] and not u["disabled"] and db.count_admins() <= 1:
        raise HTTPException(400, "There must always be at least one admin.")
    db.delete_user(u["email"])
    return {"ok": True}


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(ValueError)
def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


# ---------- HTML ----------

ICONS = {
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
    "restart": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></svg>',
}

BASE_CSS = """
  :root { --bg:#FDFFF8; --text:#040E27; --text2:#3B404E; --primary:#0F41F3; --primary-hover:#0B34C8; --border:#C1CFF7;
          --surface:#E1EAF9; --page:#F3F5FB; --bad:#C0262D; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--page); color:var(--text); font:14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
"""

CARD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nscale Client Platforms</title><link rel="icon" href="/static/nscale-icon.svg"><style>""" + BASE_CSS + """
  body { min-height:100vh; display:grid; place-items:center; }
  .c { background:var(--bg); border:1px solid var(--surface); border-radius:14px; padding:32px; width:min(380px, calc(100vw - 32px)); }
  .c > img { height:16px; display:block; margin-bottom:6px; }
  .app { color:var(--text2); font-weight:600; margin:0 0 24px; }
  label { display:block; font-weight:600; margin:14px 0 6px; }
  input { width:100%; font:inherit; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:#fff; color:var(--text); }
  input:focus { outline:none; border-color:var(--primary); box-shadow:0 0 0 3px rgba(15,65,243,.15); }
  button, a.btn { display:inline-block; margin-top:22px; width:100%; text-align:center; background:var(--primary); color:var(--bg); border:0;
           border-radius:8px; padding:11px 22px; font:inherit; font-weight:600; cursor:pointer; text-decoration:none; }
  button:hover, a.btn:hover { background:var(--primary-hover); }
  .err { background:#FDECEC; color:var(--bad); border-radius:8px; padding:10px 12px; margin:0 0 4px; }
  p { color:var(--text2); margin:0; }
</style></head><body><div class="c"><img src="/static/nscale-wordmark.svg" alt="Nscale"><p class="app">Client Platforms</p>{{BODY}}</div></body></html>"""


def login_html(nxt: str, email: str = "", error: str = "", status: int = 200) -> HTMLResponse:
    if PASSWORDS and db.count_users() == 0:
        return message_page("No accounts yet. Set ADMIN_EMAIL and ADMIN_PASSWORD on the portal service and "
                            "restart it to create the first admin.", 503)
    body = (f'<p class="err" role="alert">{escape(error)}</p>' if error else "") + f"""
<form method="post" action="/login">
  <input type="hidden" name="next" value="{escape(safe_next(nxt))}">
  <label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required value="{escape(email)}" {'' if email else 'autofocus'}>
  <label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required {'autofocus' if email else ''}>
  <button type="submit">Sign in</button>
</form>"""
    return HTMLResponse(CARD.replace("{{BODY}}", body), status_code=status, headers={"Cache-Control": "no-store"})


def message_page(message: str, status: int, button: str = "", href: str = "") -> HTMLResponse:
    body = f"<p>{escape(message)}</p>" + (f'<a class="btn" href="{escape(href)}">{escape(button)}</a>' if button else "")
    return HTMLResponse(CARD.replace("{{BODY}}", body), status_code=status, headers={"Cache-Control": "no-store"})


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nscale Client Platforms</title><link rel="icon" href="/static/nscale-icon.svg">
<script src="/static/nav.js" defer></script>
<style>""" + BASE_CSS + """
  header { background:var(--bg); border-bottom:1px solid var(--surface); }
  .bar { max-width:1080px; margin:0 auto; padding:0 24px; height:64px; display:flex; align-items:center; gap:16px; }
  .bar img { height:16px; display:block; }
  .bar .app { color:var(--text2); border-left:1px solid var(--border); padding-left:16px; font-weight:600; }
  .who { margin-left:auto; color:var(--text2); font-size:12.5px; }
  .who a { color:inherit; }
  main { max-width:1080px; margin:0 auto; padding:48px 24px 64px; }
  h1 { font-size:28px; font-weight:600; margin:0 0 6px; letter-spacing:-.01em; }
  .sub { color:var(--text2); margin:0 0 32px; font-size:15px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:16px; }
  .tile { display:flex; flex-direction:column; gap:6px; background:var(--bg); border:1px solid var(--surface); border-radius:14px;
          padding:24px; color:inherit; text-decoration:none; transition:border-color .15s, box-shadow .15s, transform .15s; }
  .tile:hover { border-color:var(--primary); box-shadow:0 8px 24px rgba(15,65,243,.10); transform:translateY(-1px); }
  .tile:focus-visible { outline:none; box-shadow:0 0 0 3px rgba(15,65,243,.3); }
  .ic { width:44px; height:44px; border-radius:10px; background:var(--surface); color:var(--primary); display:grid; place-items:center; margin-bottom:10px; }
  .ic svg { width:22px; height:22px; }
  .tile b { font-size:17px; font-weight:600; }
  .tile span { color:var(--text2); }
  .tile i { font-style:normal; color:var(--primary); font-weight:600; margin-top:10px; }
  footer { color:var(--text2); font-size:12.5px; margin-top:40px; }
  @media (max-width:600px) { .bar { padding:0 16px; } .bar .app { display:none; } main { padding:32px 16px 48px; } h1 { font-size:24px; }
    .tiles { grid-template-columns:1fr; } }
</style></head><body>
<header><div class="bar"><img src="/static/nscale-wordmark.svg" alt="Nscale"><span class="app">Client Platforms</span>
<div class="who">{{EMAIL}} · {{LINKS}}<a href="/logout">Sign out</a></div></div></header>
<main>
  <h1>Hi {{FIRST}}</h1>
  <p class="sub">Tools for managing Nscale's Windows and Mac devices.</p>
  <div class="tiles">{{TILES}}</div>
  <footer>Which tools you see is set on your account. If you need another, ask a Client Platforms admin.</footer>
</main></body></html>"""
