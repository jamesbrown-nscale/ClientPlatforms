"""Nscale Projects: small project workspaces, on Client Platforms.

Each project has the sections of TechOps' Project Management workspace (see modules.py): a
dashboard, status reports, timeline, RAID logs, delivery logs, people, budget, documents and
closure. Who can see a project is its own Members list:

    Owner   everything, including the project's details, its members and deleting it
    Editor  add, change and delete records
    Viewer  read only

Anyone given the Projects tool (portal Settings → People) can create a project and becomes its
owner. Client Platforms admins can open every project as an owner.

People:   /                          the page (signed in at the front door, see auth.py)
          /api/meta                  the section definitions the page draws from
          /api/projects[/{id}]       projects; /summary, /status-draft, /members, /items/{section}

All of it sits under BASE_PATH (/projects).
Settings: DATABASE_PATH, AUTH_MODE, AUTH_SECRET, PORTAL_URL, BASE_PATH.
"""
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, auth, modules
from .auth import User, require_user
from .modules import BY_KEY, ValidationError
from .store import Store

BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
STATIC = Path(__file__).parent / "static"
ROLES = ["Owner", "Editor", "Viewer"]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

auth.check_settings()
store = Store(os.environ.get("DATABASE_PATH", "/data/projects.db"))
app = FastAPI(title="Nscale Projects", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- Access ----------

def role_in(pid: str, user: User) -> str | None:
    if not store.project(pid):
        raise HTTPException(404, "Project not found.")
    r = store.role(pid, user.email)
    return r or ("Owner" if user.admin else None)


def need(pid: str, user: User, *allowed: str) -> str:
    r = role_in(pid, user)
    if not r:
        raise HTTPException(404, "Project not found.")   # not saying it exists
    if r not in allowed:
        raise HTTPException(403, "Only " + " or ".join(a.lower() + "s" for a in allowed) + " of this project can do that.")
    return r


def section(key: str) -> dict:
    m = BY_KEY.get(key)
    if not m:
        raise HTTPException(404, "No such section.")
    return m


# ---------- Pages ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    if not auth.current_user(request):
        return HTMLResponse(f"<!doctype html><title>Projects</title><p style='font-family:system-ui;padding:40px'>"
                            f"{access.sign_in_message()}</p>", status_code=403)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"email": user.email, "name": user.name, "admin": user.admin, "portalUrl": PORTAL_URL}


@app.get("/api/meta")
def meta(user: User = Depends(require_user)):
    return {"modules": modules.MODULES, "phases": modules.PHASES, "projectStatuses": modules.PROJECT_STATUSES,
            "currencies": modules.CURRENCIES, "roles": ROLES}


# ---------- Projects ----------

class ProjectIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    status: str | None = None
    currency: str | None = None
    approvedBudget: float | str | None = None


def _apply(doc: dict, body: ProjectIn) -> dict:
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(422, "Give the project a name.")
        doc["name"] = body.name.strip()
    if body.description is not None:
        doc["description"] = body.description.strip()
    if body.status is not None:
        if body.status not in modules.PROJECT_STATUSES:
            raise HTTPException(422, "Invalid project status.")
        doc["status"] = body.status
    if body.currency is not None:
        if body.currency not in modules.CURRENCIES:
            raise HTTPException(422, f"Currency must be one of {', '.join(modules.CURRENCIES)}.")
        doc["currency"] = body.currency
    if body.approvedBudget is not None:
        text = re.sub(r"[,\s£$€]", "", str(body.approvedBudget))
        try:
            doc["approvedBudget"] = "" if text == "" else round(float(text), 2)
        except ValueError:
            raise HTTPException(422, "Approved budget must be a number.")
    return doc


def _card(p: dict, user: User) -> dict:
    items = store.items(p["id"])
    s = modules.summarize(items, modules.today(), p)
    return {**p, "role": p.get("role") or ("Owner" if user.admin else None), "summary": {
        "overallRag": s["status"]["overallRag"], "phase": s["status"]["phase"], "next": s["milestones"]["next"],
        "openActions": s["actions"]["open"], "overdueActions": s["actions"]["overdue"],
        "openRisks": s["risks"]["open"], "openIssues": s["issues"]["open"], "members": len(store.members(p["id"]))}}


@app.get("/api/projects")
def list_projects(user: User = Depends(require_user)):
    rows = store.projects_for(user.email, every=user.admin)
    rows.sort(key=lambda p: (modules.PROJECT_STATUSES.index(p.get("status", "active")), p.get("name", "").lower()))
    return {"projects": [_card(p, user) for p in rows]}


@app.post("/api/projects")
def create_project(body: ProjectIn, user: User = Depends(require_user)):
    doc = _apply({"name": "", "description": "", "status": "active", "currency": "GBP", "approvedBudget": ""}, body)
    if not doc["name"]:
        raise HTTPException(422, "Give the project a name.")
    return store.create_project(doc, user.email, user.name)


@app.get("/api/projects/{pid}")
def get_project(pid: str, user: User = Depends(require_user)):
    r = need(pid, user, *ROLES)
    return {**store.project(pid), "role": r}


@app.patch("/api/projects/{pid}")
def update_project(pid: str, body: ProjectIn, user: User = Depends(require_user)):
    need(pid, user, "Owner")
    return store.save_project(_apply(store.project(pid), body))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, user: User = Depends(require_user)):
    need(pid, user, "Owner")
    store.delete_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/summary")
def summary(pid: str, user: User = Depends(require_user)):
    need(pid, user, *ROLES)
    return modules.summarize(store.items(pid), modules.today(), store.project(pid))


@app.get("/api/projects/{pid}/status-draft")
def status_draft(pid: str, user: User = Depends(require_user)):
    need(pid, user, "Owner", "Editor")
    return modules.status_draft(store.items(pid), modules.today(), store.project(pid))


@app.get("/api/projects/{pid}/people")
def people(pid: str, user: User = Depends(require_user)):
    """Names to suggest in person fields: members, stakeholders, and anyone already named."""
    need(pid, user, *ROLES)
    names = {m["name"] or m["email"] for m in store.members(pid)}
    for it in store.items(pid):
        for f in BY_KEY.get(it["module"], {}).get("fields", []):
            v = it["data"].get(f["key"])
            if f["type"] == "user" and v:
                names.add(v)
            elif f["type"] == "users" and isinstance(v, list):
                names.update(x for x in v if x)
    return {"people": sorted(names, key=str.lower)}


# ---------- Members ----------

class MemberIn(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(default="", max_length=100)
    role: str = "Editor"


class RoleIn(BaseModel):
    role: str


def _owners(pid: str) -> int:
    return sum(m["role"] == "Owner" for m in store.members(pid))


@app.get("/api/projects/{pid}/members")
def list_members(pid: str, user: User = Depends(require_user)):
    r = need(pid, user, *ROLES)
    return {"members": store.members(pid), "role": r}


@app.post("/api/projects/{pid}/members")
def add_member(pid: str, body: MemberIn, user: User = Depends(require_user)):
    need(pid, user, "Owner")
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    if body.role not in ROLES:
        raise HTTPException(422, "Role must be Owner, Editor or Viewer.")
    current = store.role(pid, email)
    if current == "Owner" and body.role != "Owner" and _owners(pid) <= 1:
        raise HTTPException(400, "A project needs at least one owner.")
    store.set_member(pid, email, body.name.strip() or access.display_name(email), body.role, user.email)
    return {"members": store.members(pid)}


@app.patch("/api/projects/{pid}/members/{email}")
def change_member(pid: str, email: str, body: RoleIn, user: User = Depends(require_user)):
    need(pid, user, "Owner")
    email = email.lower()
    current = store.role(pid, email)
    if not current:
        raise HTTPException(404, "Not a member.")
    if body.role not in ROLES:
        raise HTTPException(422, "Role must be Owner, Editor or Viewer.")
    if current == "Owner" and body.role != "Owner" and _owners(pid) <= 1:
        raise HTTPException(400, "A project needs at least one owner.")
    store.set_member(pid, email, "", body.role, user.email)
    return {"members": store.members(pid)}


@app.delete("/api/projects/{pid}/members/{email}")
def remove_member(pid: str, email: str, user: User = Depends(require_user)):
    email = email.lower()
    if email != user.email:   # anyone can leave; only owners remove others
        need(pid, user, "Owner")
    else:
        need(pid, user, *ROLES)
    if store.role(pid, email) == "Owner" and _owners(pid) <= 1:
        raise HTTPException(400, "A project needs at least one owner.")
    if not store.remove_member(pid, email):
        raise HTTPException(404, "Not a member.")
    return {"members": store.members(pid)}


# ---------- Records ----------

@app.get("/api/projects/{pid}/items/{key}")
def list_items(pid: str, key: str, user: User = Depends(require_user)):
    need(pid, user, *ROLES)
    section(key)
    return {"items": store.items(pid, key)}


def _check(mod: dict, raw, existing=None) -> dict:
    try:
        return modules.sanitize(mod, raw, existing)
    except ValidationError as e:
        raise HTTPException(422, str(e))


@app.post("/api/projects/{pid}/items/{key}")
async def add_item(pid: str, key: str, request: Request, user: User = Depends(require_user)):
    need(pid, user, "Owner", "Editor")
    mod = section(key)
    data = _check(mod, await request.json())
    item = store.add_item(pid, key, modules.next_ref(mod, store.refs(pid, key)), data, user.email)
    if key == "meetings":
        _sync_meeting(pid, item, user)
    return item


@app.put("/api/projects/{pid}/items/{key}/{iid}")
async def update_item(pid: str, key: str, iid: str, request: Request, user: User = Depends(require_user)):
    need(pid, user, "Owner", "Editor")
    mod = section(key)
    old = store.item(pid, iid)
    if not old or old["module"] != key:
        raise HTTPException(404, "Record not found.")
    item = store.update_item(pid, iid, _check(mod, await request.json(), old["data"]), user.email)
    if key == "meetings":
        _sync_meeting(pid, item, user)
    if key == "actions" and (item["source"] or {}).get("meeting"):
        _back_to_meeting(pid, item, user)
    return item


@app.post("/api/projects/{pid}/items/{key}/{iid}/quick")
def quick_action(pid: str, key: str, iid: str, user: User = Depends(require_user)):
    """The one-click status change in a list (Complete, Mark met, Done …)."""
    need(pid, user, "Owner", "Editor")
    mod = section(key)
    qa = mod.get("quickAction")
    old = store.item(pid, iid)
    if not qa or not old or old["module"] != key:
        raise HTTPException(404, "Record not found.")
    item = store.update_item(pid, iid, _check(mod, {**old["data"], qa["field"]: qa["value"]}, old["data"]), user.email)
    if key == "actions" and (item["source"] or {}).get("meeting"):
        _back_to_meeting(pid, item, user)
    return item


@app.delete("/api/projects/{pid}/items/{key}/{iid}")
def delete_item(pid: str, key: str, iid: str, user: User = Depends(require_user)):
    need(pid, user, "Owner", "Editor")
    old = store.item(pid, iid)
    if not old or old["module"] != key:
        raise HTTPException(404, "Record not found.")
    store.delete_item(pid, iid)
    return {"ok": True}


class TemplateIn(BaseModel):
    start: str | None = None


@app.post("/api/projects/{pid}/items/{key}/template")
def add_template(pid: str, key: str, body: TemplateIn, user: User = Depends(require_user)):
    """The section's standard records in one go: the phase plan, or the closure checklist."""
    need(pid, user, "Owner", "Editor")
    mod = section(key)
    if not mod.get("template"):
        raise HTTPException(404, "This section has no standard records.")
    start = body.start if body.start and modules.DATE.match(body.start) else modules.today()
    added = []
    for rec in modules.template_records(mod, start):
        added.append(store.add_item(pid, key, modules.next_ref(mod, store.refs(pid, key)), _check(mod, rec), user.email))
    return {"items": added}


# ---------- Meeting actions ⇄ Action Log ----------

def _sync_meeting(pid: str, meeting: dict, user: User) -> None:
    """Each action in a meeting is also a record in the Action Log, linked back to the meeting."""
    mod = BY_KEY["actions"]
    linked = {(i["source"] or {}).get("action"): i for i in store.items_from(pid, meeting["id"])}
    title = f"{meeting['data'].get('title', '')} ({meeting['data'].get('date', '')})"
    keep = set()
    for a in meeting["data"].get("actions") or []:
        keep.add(a["id"])
        existing = linked.get(a["id"])
        fields = {"description": a["description"], "owner": a.get("assignedTo", ""), "dueDate": a.get("dueDate", ""),
                  "status": a.get("status", "Open")}
        if existing:
            if any(existing["data"].get(k) != v for k, v in fields.items()):
                store.update_item(pid, existing["id"], _check(mod, {**existing["data"], **fields}, existing["data"]), user.email)
        else:
            data = _check(mod, {**fields, "priority": "Medium", "notes": f"From the meeting {title}."})
            store.add_item(pid, "actions", modules.next_ref(mod, store.refs(pid, "actions")), data, user.email,
                           source={"meeting": meeting["id"], "action": a["id"], "meetingTitle": title})
    for aid, it in linked.items():
        if aid not in keep:   # taken out of the meeting
            store.delete_item(pid, it["id"])


def _back_to_meeting(pid: str, action: dict, user: User) -> None:
    """A change in the Action Log to an action from a meeting is made in the meeting too."""
    src = action["source"]
    meeting = store.item(pid, src["meeting"])
    if not meeting:
        return
    changed = False
    rows = []
    for a in meeting["data"].get("actions") or []:
        if a["id"] == src.get("action"):
            new = {**a, "description": action["data"].get("description", a["description"]),
                   "assignedTo": action["data"].get("owner", ""), "dueDate": action["data"].get("dueDate", ""),
                   "status": action["data"].get("status", a["status"])}
            changed = changed or new != a
            a = new
        rows.append(a)
    if changed:
        store.update_item(pid, meeting["id"], {**meeting["data"], "actions": rows}, user.email)


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


# ---------- Serving under a path on a shared host ----------
if BASE_PATH:
    _app = app
    app = FastAPI(title="Nscale Projects", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(BASE_PATH, _app)

    @app.get(BASE_PATH, include_in_schema=False)
    def _add_slash():
        return RedirectResponse(BASE_PATH + "/")

    @app.get("/healthz", include_in_schema=False)
    def _healthz():
        return {"ok": True}
