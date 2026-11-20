"""Nscale Client Platforms: the home page, sign-in and settings.

AUTH_MODE=oidc (the Google Cloud VM): people sign in with the company's identity provider over OpenID
Connect (Google now, Okta later: see oidc.py). Only people on the people list get in, whatever the
provider says; admins add them under Settings, searching the directory (see directory.py). Passwords
are kept only for the break-glass admins in BREAK_GLASS_EMAILS (default: ADMIN_EMAIL), for when the
provider is down.

AUTH_MODE=local (the default, for running it on your own machine): everyone signs in here with an
email and password (see db.py).

Either way the front door (Caddy, see /Caddyfile) asks /auth/check before every request to a tool,
and passes on the signed X-CP-User token this answers with (see access.py). Admins manage people,
which tools each can use, the tool links and the site and device addresses on /settings.

Settings (environment): AUTH_SECRET, DATABASE_PATH, ADMIN_EMAIL + ADMIN_PASSWORD (first admin, only
used while there are no accounts), SESSION_HOURS, PUBLIC_URL / DEVICE_URL (defaults for the settings),
BREAK_GLASS_EMAILS and the OIDC_* and DIRECTORY_* settings (AUTH_MODE=oidc).
"""
import json
import logging
import os
import re
import secrets
import sys
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import access, directory, oidc, recycle
from .db import ADMIN_ROLE, DB, LEVELS, SITE_PERMS, TOOL_IDS, TOOLS, USES

STATIC = Path(__file__).parent / "static"
COOKIE = "cp_session"
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "12"))
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10
# The site's version (deploy/vm/version.sh: one more for every commit), shown in the sidebar.
VERSION = re.sub(r"[^0-9.]", "", os.environ.get("CP_VERSION", ""))[:20]

MODE = "dev" if access.DEV_USER else access.AUTH_MODE
if MODE not in ("local", "oidc", "dev"):
    raise RuntimeError(f"AUTH_MODE must be oidc or local, not {MODE!r}.")
ACCOUNTS = MODE in ("local", "oidc")                   # the people list is kept here
PASSWORDS = MODE == "local"                            # everyone signs in with a password
OIDC = MODE == "oidc"
SESSIONS = PASSWORDS or OIDC                           # the portal keeps its own sign-in sessions
# With OIDC, only these accounts can use a password: a way in when the identity provider is down.
BREAK_GLASS = {e.strip().lower() for e in (os.environ.get("BREAK_GLASS_EMAILS") or os.environ.get("ADMIN_EMAIL", "")).split(",") if e.strip()}

access.check_settings()
if OIDC:
    oidc.check_settings()
db = DB(os.environ.get("DATABASE_PATH", "/data/portal.db"))


def bootstrap() -> None:
    """The first admin, from ADMIN_EMAIL and ADMIN_PASSWORD, while there are no accounts at all."""
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "") if uses_password(email) else ""
    password = password or ("" if PASSWORDS else secrets.token_urlsafe(32))
    if ACCOUNTS and email and password and db.count_users() == 0:
        db.create_user(email, access.display_name(email), password, roles=[ADMIN_ROLE])


def uses_password(email: str) -> bool:
    """Whether this account signs in with a password: everyone with AUTH_MODE=local, only the
    break-glass admins with OIDC."""
    return PASSWORDS or (OIDC and (email or "").lower() in BREAK_GLASS)


bootstrap()
app = FastAPI(title="Nscale Client Platforms", docs_url=None, redoc_url=None, openapi_url=None)
class _Static(StaticFiles):
    """Every page on the site loads nav.js and dialog.js from here: browsers check for a newer copy
    each time (cheap, with ETags), so an update reaches everyone straight away."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", _Static(directory=STATIC), name="static")


# ---------- Who is signed in ----------

def current(request: Request) -> dict | None:
    if access.DEV_USER:
        return {"email": access.DEV_USER, "name": access.display_name(access.DEV_USER), "admin": True,
                "tools": TOOL_IDS, "levels": {t: "admin" for t in TOOL_IDS}, "perms": list(SITE_PERMS), "roles": [ADMIN_ROLE]}
    return db.session_user(request.cookies.get(COOKIE, "")) if SESSIONS else None


def not_signed_in(request: Request, nxt: str = "/") -> Response:
    """The page for someone with no (usable) sign-in."""
    if SESSIONS:
        nxt = safe_next(nxt)
        return RedirectResponse("/login" if nxt == "/" else "/login?next=" + quote(nxt, safe="/"))
    return message_page(access.sign_in_message(), 403)


EVERYONE = {"inbox"}   # everyone signed in has their own notifications, whatever tools they have


def level(user: dict, tool: str) -> str:
    """none, view, edit or admin: the highest any of their roles gives them on this tool."""
    if tool in EVERYONE:
        return "edit"
    return user.get("levels", {}).get(USES.get(tool, tool), "none")


def can_use(user: dict, tool: str) -> bool:
    return level(user, tool) != "none"


def has(user: dict, perm: str) -> bool:
    return perm in user.get("perms", [])


def require_user(request: Request) -> dict:
    user = current(request)
    if not user:
        raise HTTPException(401, "Sign in first.")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "nscale-admin":
        raise HTTPException(403, "Missing request header.")   # only our own pages send it (no CORS)
    return user


def require_people(user: dict = Depends(require_user)) -> dict:
    if not has(user, "people"):
        raise HTTPException(403, "Only people who manage people and roles can do that.")
    return user


def require_settings(user: dict = Depends(require_user)) -> dict:
    if not has(user, "settings"):
        raise HTTPException(403, "Only people who manage the site settings can do that.")
    return user


def require_accounts():
    if not ACCOUNTS:
        raise HTTPException(400, "The people list is only kept here with AUTH_MODE=oidc or local.")


def require_passwords():
    if not SESSIONS:
        raise HTTPException(400, "Passwords are only used with AUTH_MODE=local, or by break-glass admins with oidc.")


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
def login_page(request: Request, next: str = "/", out: str = "", password: str = ""):
    if not SESSIONS:
        return RedirectResponse("/")
    if current(request):
        return RedirectResponse(safe_next(next))
    return login_html(next, notice="You're signed out." if out else "", show_password=bool(password))


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
    ok = SESSIONS and uses_password(email) and db.check(password, user)
    if not ok:
        _failed(*keys)
        msg = ("That email and password don't match an account." if PASSWORDS else
               "That email and password don't match a break-glass account. Everyone else signs in with " + oidc.PROVIDER + ".")
        return login_html(nxt, email, msg, 401, show_password=True)
    _failures.pop(keys[0], None)
    return _signed_in(request, email, nxt)


def _signed_in(request: Request, email: str, nxt: str) -> Response:
    """Starts their session here and sends them on."""
    db.signed_in(email)
    r = RedirectResponse(nxt, status_code=303)
    r.set_cookie(COOKIE, db.start_session(email, SESSION_HOURS), max_age=int(SESSION_HOURS * 3600), path="/",
                 httponly=True, samesite="lax", secure=secure(request))
    return r


def _redirect_uri(request: Request) -> str:
    if oidc.REDIRECT_URL:
        return oidc.REDIRECT_URL
    return (db.settings()["publicUrl"] or base_url(request)).rstrip("/") + "/auth/callback"


@app.get("/login/oidc")
def login_oidc(request: Request, next: str = "/"):
    """Off to the identity provider to sign in."""
    if not OIDC:
        return RedirectResponse("/login")
    try:
        where, flow = oidc.start(access.AUTH_SECRET, _redirect_uri(request), safe_next(next))
    except Exception:
        return message_page(f"{oidc.PROVIDER} sign-in isn't reachable just now. Try again in a minute.", 502, "Try again", "/login")
    r = RedirectResponse(where, status_code=303)
    r.set_cookie(oidc.COOKIE, flow, max_age=oidc.FLOW_SECONDS, path="/auth/callback", httponly=True, samesite="lax", secure=secure(request))
    return r


@app.get("/auth/callback")
def oidc_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Back from the identity provider: check who it says this is, then whether they're on the list."""
    if not OIDC:
        return RedirectResponse("/login")
    try:
        if error:
            raise oidc.SignInError("The sign-in was cancelled." if error == "access_denied" else "The sign-in provider didn't sign you in.")
        email, name, nxt = oidc.finish(access.AUTH_SECRET, request.cookies.get(oidc.COOKIE, ""), state, code)
    except oidc.SignInError as e:
        r = message_page(str(e), 400, "Try again", "/login")
        r.delete_cookie(oidc.COOKIE, path="/auth/callback")
        return r
    except Exception:
        r = message_page(f"{oidc.PROVIDER} sign-in isn't reachable just now. Try again in a minute.", 502, "Try again", "/login")
        r.delete_cookie(oidc.COOKIE, path="/auth/callback")
        return r
    user = db.get_user(email)
    if not user or user["disabled"]:
        r = message_page(f"{email} isn't on the Client Platforms list, or the account is turned off. "
                         "Ask a Client Platforms admin to add you.", 403, "Back to sign-in", "/login?out=1")
        r.delete_cookie(oidc.COOKIE, path="/auth/callback")
        return r
    if name and user["name"] == access.display_name(email) and name != user["name"]:
        db.update_user(email, name=name[:100])   # the provider's proper name beats one guessed from the email
    r = _signed_in(request, email, safe_next(nxt))
    r.delete_cookie(oidc.COOKIE, path="/auth/callback")
    return r


@app.get("/logout")
@app.get("/signout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        db.end_session(token)
    # With OIDC, a page that says you're signed out (not straight back to the provider, which would
    # sign you in again at once).
    r = RedirectResponse("/login?out=1" if OIDC else "/login")
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
    if (tool not in TOOL_IDS and tool not in EVERYONE) or not can_use(user, tool):
        name = next((t["name"] for t in TOOLS if t["id"] == tool), "this tool")
        msg = f"You don't have access to {name}. Ask a Client Platforms admin to add it to your account."
        return message_page(msg, 403, "Back to home", "/") if page else JSONResponse({"detail": msg}, status_code=403)
    lv = level(user, tool)
    if lv == "view" and method not in ("GET", "HEAD"):
        name = next((t["name"] for t in TOOLS if t["id"] == tool), "this tool")
        return JSONResponse({"detail": f"You can view {name} but not change anything in it. Ask for edit access."}, status_code=403)
    token = access.issue(user["email"], user["name"], tool, lv == "admin", level=lv)
    return Response(status_code=200, headers={"X-CP-User": token, "Cache-Control": "no-store"})


# ---------- Pages ----------

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION}


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
    links = ('<a href="/settings">Settings</a> · ' if user.get("perms") else
             '<a href="/settings">Account</a> · ' if uses_password(user["email"]) else "")
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
    # Every tool they can use is in their sidebar; "On home page" (enabled) only decides the home page tiles.
    tools = [{**t, "level": level(user, t["id"])} for t in db.settings()["tools"] if can_use(user, t["id"])]
    return {"email": user["email"], "name": user["name"], "admin": user["admin"], "perms": user.get("perms", []), "tools": tools,
            "inbox": ACCOUNTS or MODE == "dev", "authMode": MODE, "accounts": ACCOUNTS, "passwords": uses_password(user["email"]),
            "oidc": OIDC, "provider": oidc.PROVIDER if OIDC else "", "directory": directory.name() if OIDC else "",
            "minPassword": MIN_PASSWORD, "version": VERSION}


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=MIN_PASSWORD, max_length=200)


@app.post("/api/me/password", dependencies=[Depends(require_passwords)])
def change_password(body: PasswordChange, request: Request, user: dict = Depends(require_user)):
    if not uses_password(user["email"]):
        raise HTTPException(400, f"You sign in with {oidc.PROVIDER}, so there's no password here.")
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
def get_settings(request: Request, user: dict = Depends(require_settings)):
    s = db.settings()
    return {**s, "siteUrl": s["publicUrl"] or base_url(request), "devices": device_urls(request)}


@app.put("/api/settings")
def put_settings(body: Settings, request: Request, user: dict = Depends(require_settings)):
    current_tools = {t["id"]: t for t in db.settings()["tools"]}
    tools = {}
    for t in body.tools:
        if t.id in current_tools:
            tools[t.id] = {"url": "" if t.url == current_tools[t.id]["defaultUrl"] else t.url, "enabled": t.enabled}
    db.save_settings({"publicUrl": body.publicUrl, "deviceUrl": body.deviceUrl, "tools": tools})
    return get_settings(request, user)


@app.get("/api/people")
def people(tool: str = "", user: dict = Depends(require_user)):
    """Who can be assigned or mentioned: the switched-on accounts (with that tool, if one is given).
    Any signed-in person can read it; it's names and emails only."""
    if not ACCOUNTS:
        return {"people": []}
    if tool and tool not in TOOL_IDS:
        raise HTTPException(404, "No such tool.")
    rows = [u for u in db.list_users() if not u["disabled"] and (not tool or tool in u["levels"])]
    return {"people": sorted(({"email": u["email"], "name": u["name"]} for u in rows), key=lambda p: p["name"].lower())}


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


def _roles(v: list[str]) -> list[str]:
    known = {r["id"] for r in db.roles()}
    bad = [r for r in v if r not in known]
    if bad:
        raise ValueError(f"Unknown role: {', '.join(bad)}")
    return list(dict.fromkeys(v))


class NewUser(BaseModel):
    email: str
    name: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=200)   # AUTH_MODE=local only
    roles: list[str] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def _e(cls, v: str) -> str:
        return _email(v)

    @field_validator("roles")
    @classmethod
    def _r(cls, v: list[str]) -> list[str]:
        return _roles(v)


class UserChange(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    disabled: bool | None = None
    roles: list[str] | None = None

    @field_validator("roles")
    @classmethod
    def _r(cls, v: list[str] | None) -> list[str] | None:
        return v if v is None else _roles(v)


class NewPassword(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


def _catalog() -> dict:
    return {"tools": [{"id": t["id"], "name": t["name"], "group": t["group"]} for t in TOOLS if not t.get("uses")], "levels": LEVELS,
            "sitePerms": [{"id": k, "name": v} for k, v in SITE_PERMS.items()]}


@app.get("/api/users", dependencies=[Depends(require_accounts)])
def list_users(user: dict = Depends(require_people)):
    return {"users": [db.public(u) for u in db.list_users()], "roles": db.roles(), **_catalog()}


@app.post("/api/users", dependencies=[Depends(require_accounts)])
def create_user(body: NewUser, user: dict = Depends(require_people)):
    if db.get_user(body.email):
        raise HTTPException(409, f"{body.email} already has an account.")
    if PASSWORDS and not body.password:
        raise HTTPException(422, "Give them a password.")
    if PASSWORDS and body.password and len(body.password) < MIN_PASSWORD:
        raise HTTPException(422, f"The password needs at least {MIN_PASSWORD} characters.")
    # With OIDC they get a random password nobody knows (only break-glass admins use one).
    password = body.password if PASSWORDS and body.password else secrets.token_urlsafe(32)
    u = db.create_user(body.email, body.name.strip() or access.display_name(body.email), password, body.roles)
    return db.public(u)


def _target(email: str) -> dict:
    u = db.get_user(email)
    if not u:
        raise HTTPException(404, "No such account.")
    return u


@app.patch("/api/users/{email}", dependencies=[Depends(require_accounts)])
def update_user(email: str, body: UserChange, user: dict = Depends(require_people)):
    u = _target(email)
    me = u["email"] == user["email"]
    if me and body.disabled:
        raise HTTPException(400, "You can't turn off your own account.")
    if body.roles is not None and me and "people" in u["perms"] and \
            "people" not in db._effective(body.roles, {r["id"]: r for r in db.roles()})["perms"]:
        raise HTTPException(400, "You can't take away your own right to manage people and roles.")
    if body.disabled or body.roles is not None:
        after = db.count_people_managers(without=u["email"] if body.disabled else None,
                                         roles_for=(u["email"], body.roles) if body.roles is not None else None)
        if after < 1:
            raise HTTPException(400, "Someone must always be able to manage people and roles.")
    return db.public(db.update_user(u["email"], name=(body.name or "").strip() or None, disabled=body.disabled, roles=body.roles))


@app.post("/api/users/{email}/password", dependencies=[Depends(require_passwords)])
def reset_password(email: str, body: NewPassword, user: dict = Depends(require_people)):
    u = _target(email)
    if u["email"] == user["email"]:
        raise HTTPException(400, "Change your own password under Your account.")
    if not uses_password(u["email"]):
        raise HTTPException(400, f"{u['email']} signs in with {oidc.PROVIDER}: only break-glass admins have passwords.")
    db.set_password(u["email"], body.password)   # also signs them out everywhere
    return {"ok": True}


@app.delete("/api/users/{email}", dependencies=[Depends(require_accounts)])
def delete_user(email: str, user: dict = Depends(require_people)):
    u = _target(email)
    if u["email"] == user["email"]:
        raise HTTPException(400, "You can't delete your own account.")
    if db.count_people_managers(without=u["email"]) < 1:
        raise HTTPException(400, "Someone must always be able to manage people and roles.")
    db.delete_user(u["email"], by=user["email"])
    return {"ok": True}


# ---------- People added from the Client Platform Team page ----------
# Adding someone to the team (projects service, team.py) also gives them a sign-in here, with the
# role chosen under Settings → People. The Team service proves it's asking with a 2-minute token
# signed with AUTH_SECRET, for a purpose no tool uses (so no person's own token can do this), naming
# whoever added them.

TEAM_ADDS = "team-adds-person"
log = logging.getLogger("portal")
if not log.handlers:   # one JSON line per event, for Cloud Logging / docker logs
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


class TeamRole(BaseModel):
    role: str = Field(default="", max_length=64)   # "" for no role: they can sign in but see no tools yet


@app.get("/api/people/team-role", dependencies=[Depends(require_accounts)])
def get_team_role(user: dict = Depends(require_people)):
    rid = db.setting("teamRole", "")
    return {"role": rid if any(r["id"] == rid for r in db.roles()) else ""}


@app.put("/api/people/team-role", dependencies=[Depends(require_accounts)])
def put_team_role(body: TeamRole, user: dict = Depends(require_people)):
    if body.role and not any(r["id"] == body.role for r in db.roles()):
        raise HTTPException(422, "No such role.")
    db.save_settings({"teamRole": body.role})
    return get_team_role(user)


class FromTeam(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(default="", max_length=100)


@app.post("/api/people/from-team")
def add_from_team(body: FromTeam, request: Request):
    claims = access.verify(request.headers.get("x-cp-service", ""), TEAM_ADDS)
    if not claims:
        raise HTTPException(401, "Only the Team service can do this.")
    if not ACCOUNTS:
        return {"status": "off"}
    return _add_team_person(body.email, body.name, claims["email"])


def _add_team_person(email: str, name: str, by: str) -> dict:
    """Gives a team member a sign-in with the team role, unless they have one."""
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(422, "That isn't an email address.")
    if db.get_user(email):
        return {"email": email, "status": "exists"}
    rid = db.setting("teamRole", "")
    roles = [rid] if rid and any(r["id"] == rid for r in db.roles()) else []
    # Nobody knows the password: they sign in with Google (an admin can set one with AUTH_MODE=local).
    db.create_user(email, name.strip() or access.display_name(email), secrets.token_urlsafe(32), roles)
    log.info(json.dumps({"severity": "NOTICE", "message": "person added from the team", "email": email, "by": by, "roles": roles}))
    return {"email": email, "status": "added", "roles": roles}


class TeamList(BaseModel):
    people: list[FromTeam] = Field(max_length=500)


@app.post("/api/people/from-team-list", dependencies=[Depends(require_accounts)])
def add_team_list(body: TeamList, user: dict = Depends(require_people)):
    """For team members added before this: Settings → People lists those without a sign-in, and adds them."""
    return {"results": [_add_team_person(p.email, p.name, user["email"]) for p in body.people]}


def _bin_admin(user: dict = Depends(require_people)) -> str:
    """Deleted people are for whoever manages people and roles."""
    require_accounts()
    return user["email"]


app.include_router(recycle.router(lambda: db.bin, _bin_admin))


@app.get("/api/directory", dependencies=[Depends(require_accounts)])
def search_directory(q: str = "", user: dict = Depends(require_people)):
    """People in the company directory matching q, for adding someone: name and email, and whether
    they're already on the list here."""
    if not directory.name():
        return {"directory": "", "people": []}
    q = q.strip()[:100]
    if len(q) < 2:
        return {"directory": directory.name(), "people": []}
    try:
        found = directory.search(q)
    except directory.DirectoryError as e:
        raise HTTPException(502, str(e))
    have = {u["email"] for u in db.list_users()}
    return {"directory": directory.name(), "people": [{**p, "added": p["email"] in have} for p in found]}


# ---------- API: roles ----------

class RoleIn(BaseModel):
    name: str = Field(max_length=60)
    description: str = Field(default="", max_length=300)
    levels: dict[str, str] = Field(default_factory=dict)
    perms: list[str] = Field(default_factory=list)

    @field_validator("levels")
    @classmethod
    def _lv(cls, v: dict) -> dict:
        bad = [f"{k}={x}" for k, x in v.items() if k not in TOOL_IDS or x not in LEVELS]
        if bad:
            raise ValueError(f"Unknown tool or level: {', '.join(bad)}")
        return v

    @field_validator("perms")
    @classmethod
    def _p(cls, v: list) -> list:
        bad = [p for p in v if p not in SITE_PERMS]
        if bad:
            raise ValueError(f"Unknown ability: {', '.join(bad)}")
        return v


@app.get("/api/roles", dependencies=[Depends(require_accounts)])
def list_roles(user: dict = Depends(require_people)):
    people = {u["email"]: u for u in db.list_users()}
    roles = [{**r, "people": [{"email": e, "name": people[e]["name"]} for e in db.role_members(r["id"]) if e in people]}
             for r in db.roles()]
    return {"roles": roles, **_catalog()}


@app.post("/api/roles", dependencies=[Depends(require_accounts)])
def create_role(body: RoleIn, user: dict = Depends(require_people)):
    if not body.name.strip():
        raise HTTPException(422, "Give the role a name.")
    if body.name.strip().lower() in {r["name"].lower() for r in db.roles()}:
        raise HTTPException(409, f"There's already a role called {body.name.strip()}.")
    return db.save_role(None, body.name.strip(), body.description.strip(), body.levels, body.perms)


@app.put("/api/roles/{rid}", dependencies=[Depends(require_accounts)])
def update_role(rid: str, body: RoleIn, user: dict = Depends(require_people)):
    r = db.role(rid)
    if not r:
        raise HTTPException(404, "No such role.")
    if r["system"]:
        raise HTTPException(400, "The Administrator role can't be changed.")
    if not body.name.strip():
        raise HTTPException(422, "Give the role a name.")
    if body.name.strip().lower() in {x["name"].lower() for x in db.roles() if x["id"] != rid}:
        raise HTTPException(409, f"There's already a role called {body.name.strip()}.")
    if "people" in r["perms"] and "people" not in body.perms:
        roles = {x["id"]: x for x in db.roles()}
        roles[rid] = {**r, "perms": body.perms}
        still = sum(1 for u in db.list_users() if not u["disabled"] and "people" in db._effective(u["roles"], roles)["perms"])
        if still < 1:
            raise HTTPException(400, "Someone must always be able to manage people and roles.")
    return db.save_role(rid, body.name.strip(), body.description.strip(), body.levels, body.perms)


@app.delete("/api/roles/{rid}", dependencies=[Depends(require_accounts)])
def delete_role(rid: str, user: dict = Depends(require_people)):
    r = db.role(rid)
    if not r:
        raise HTTPException(404, "No such role.")
    if r["system"]:
        raise HTTPException(400, "The Administrator role can't be deleted.")
    roles = {x["id"]: x for x in db.roles() if x["id"] != rid}
    if sum(1 for u in db.list_users() if not u["disabled"] and "people" in db._effective(u["roles"], roles)["perms"]) < 1:
        raise HTTPException(400, "Someone must always be able to manage people and roles.")
    db.delete_role(rid)
    return {"ok": True}


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(ValueError)
def _value_error(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


# ---------- HTML ----------

ICONS = {
    "org": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="5" rx="1"/><rect x="2" y="17" width="6" height="5" rx="1"/><rect x="16" y="17" width="6" height="5" rx="1"/><path d="M12 7v5M5 17v-2.5h14V17"/></svg>',
    "building": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16M16 9h2a2 2 0 0 1 2 2v10M2 21h20M8 7h4M8 11h4M8 15h4"/></svg>',
    "laptop": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="11" rx="1.5"/><path d="M2 19h20M9 19l.5-1.5h5L15 19"/></svg>',
    "users": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="11" r="2.5"/><path d="M5.5 17a3.5 3.5 0 0 1 7 0M15 9h3M15 13h3"/></svg>',
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
    "changes": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>',
    "team": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/></svg>',
    "tasks": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 2 2 4-4M3 17l2 2 4-4M13 7h8M13 17h8"/></svg>',
    "projects": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 13h3M8 16h6"/></svg>',
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
  .ok { background:var(--surface); color:var(--text); border-radius:8px; padding:10px 12px; margin:0 0 4px; }
  .c > a.btn { margin-top:8px; }
  details.bg { margin-top:22px; border-top:1px solid var(--surface); padding-top:14px; }
  details.bg summary { cursor:pointer; color:var(--text2); font-size:13px; }
  details.bg .hint { font-size:12.5px; margin-top:8px; }
  button.alt { background:#fff; color:var(--primary); border:1px solid var(--border); }
  button.alt:hover { background:var(--surface); }
  p { color:var(--text2); margin:0; }
</style></head><body><div class="c"><img src="/static/nscale-wordmark.svg" alt="Nscale"><p class="app">Client Platforms</p>{{BODY}}</div></body></html>"""


def login_html(nxt: str, email: str = "", error: str = "", status: int = 200, notice: str = "", show_password: bool = False) -> HTMLResponse:
    if PASSWORDS and db.count_users() == 0:
        return message_page("No accounts yet. Set ADMIN_EMAIL and ADMIN_PASSWORD on the portal service and "
                            "restart it to create the first admin.", 503)
    nxt = safe_next(nxt)
    form = f"""
<form method="post" action="/login">
  <input type="hidden" name="next" value="{escape(nxt)}">
  <label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required value="{escape(email)}" {'' if email or OIDC else 'autofocus'}>
  <label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required {'autofocus' if email else ''}>
  <button type="submit" class="{'alt' if OIDC else ''}">Sign in{' with a password' if OIDC else ''}</button>
</form>"""
    body = (f'<p class="err" role="alert">{escape(error)}</p>' if error else "") + \
           (f'<p class="ok" role="status">{escape(notice)}</p>' if notice else "")
    if OIDC:
        # Everyone signs in with the identity provider; the password form is only for break-glass admins.
        go = "/login/oidc" + ("" if nxt == "/" else "?next=" + quote(nxt, safe="/"))
        body += f'''<a class="btn" href="{escape(go)}" {'' if show_password else 'autofocus'}>Sign in with {escape(oidc.PROVIDER)}</a>
<details class="bg" {'open' if show_password or error else ''}><summary>Admin break-glass sign-in</summary>
<p class="hint">Only for named admins, when {escape(oidc.PROVIDER)} sign-in isn't working.</p>{form}</details>'''
    else:
        body += form
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
