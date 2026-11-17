"""Nscale Client Platforms: the home page at https://clientplatforms.nscale.com/.

Google sign-in is done by IAP at the load balancer for the whole site (see iap.py); each tool behind
it is its own Cloud Run service with its own access group. To add a tool, add it to TOOLS and route
its path to it in infra/ (url map).
"""
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import iap
from .iap import User, require_user

TOOLS = [
    {"name": "IT Notifications", "path": "/notifications/", "icon": "bell",
     "about": "Send branded notifications to Windows and Mac devices, and see who has read them."},
    {"name": "Admin elevation log", "path": "/elevation/", "icon": "shield",
     "about": "Every Windows admin elevation request: who, which device, why, and how it ended."},
]
STATIC = Path(__file__).parent / "static"

iap.check_settings()
app = FastAPI(title="Nscale Client Platforms", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"email": user.email, "name": user.name, "tools": TOOLS}


@app.get("/signout")
def signout():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/?gcp-iap-mode=CLEAR_LOGIN_COOKIE")


@app.get("/")
def index(request: Request):
    user = iap.current_user(request)
    if not user:
        return HTMLResponse("<!doctype html><title>Client Platforms</title><p style='font-family:system-ui;padding:40px'>"
                            f"Sign in with your {iap.ALLOWED_DOMAIN} Google account at the Client Platforms address.</p>",
                            status_code=403)
    from html import escape
    tiles = "".join(f"""
      <a class="tile" href="{escape(t['path'])}">
        <span class="ic">{ICONS[t['icon']]}</span>
        <b>{escape(t['name'])}</b><span>{escape(t['about'])}</span><i>Open →</i>
      </a>""" for t in TOOLS)
    first = escape(user.name.split(" ")[0])
    return HTMLResponse(PAGE.replace("{{TILES}}", tiles).replace("{{EMAIL}}", escape(user.email))
                        .replace("{{FIRST}}", first), headers={"Cache-Control": "no-store"})


ICONS = {
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nscale Client Platforms</title><link rel="icon" href="/static/nscale-icon.svg">
<style>
  :root { --bg:#FDFFF8; --text:#040E27; --text2:#3B404E; --primary:#0F41F3; --border:#C1CFF7; --surface:#E1EAF9; --page:#F3F5FB; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--page); color:var(--text); font:14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
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
  @media (max-width:600px) { .bar { padding:0 16px; } .bar .app { display:none; } main { padding:32px 16px 48px; } h1 { font-size:24px; } }
</style></head><body>
<header><div class="bar"><img src="/static/nscale-wordmark.svg" alt="Nscale"><span class="app">Client Platforms</span>
<div class="who">{{EMAIL}} · <a href="/signout">Sign out</a></div></div></header>
<main>
  <h1>Hi {{FIRST}}</h1>
  <p class="sub">Tools for managing Nscale's Windows and Mac devices.</p>
  <div class="tiles">{{TILES}}</div>
  <footer>Access to each tool is set by its Google group. If a tool says you don't have access, ask the Client Platforms team.</footer>
</main></body></html>"""
