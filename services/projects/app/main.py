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
          …/items/{section}/{id}/file   a record's file: upload, download, versions, edit (files.py)

All of it sits under BASE_PATH (/projects).
Settings: DATABASE_PATH, AUTH_MODE, AUTH_SECRET, PORTAL_URL, BASE_PATH.
"""
import os
import re
from urllib.parse import quote, unquote
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import access, auth, changes, files, inbox, modules, tasks, team
from .auth import User, require_user
from .modules import BY_KEY, ValidationError
from .store import Store

BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
TASKS_PATH = os.environ.get("TASKS_PATH", "/tasks").rstrip("/")
TEAM_PATH = os.environ.get("TEAM_PATH", "/team").rstrip("/")
INBOX_PATH = os.environ.get("INBOX_PATH", "/inbox").rstrip("/")
CHANGES_PATH = os.environ.get("CHANGES_PATH", "/changes").rstrip("/")
PORTAL_URL = os.environ.get("PORTAL_URL", "/")
STATIC = Path(__file__).parent / "static"
ROLES = ["Owner", "Editor", "Viewer"]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

auth.check_settings()
store = Store(os.environ.get("DATABASE_PATH", "/data/projects.db"))
tasks.setup(store, STATIC)
team.setup(store, STATIC)
inbox.setup(store, STATIC)
changes.setup(store, STATIC)
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
    s = modules.summarize(store.items(pid), modules.today(), store.project(pid))
    with store.db() as db:   # tasks in this project (see tasks.py)
        r = db.execute("SELECT COUNT(*) AS n, SUM(status != 'Done') AS open FROM tasks WHERE project_id = ?", (pid,)).fetchone()
    s["tasks"] = {"total": r["n"] or 0, "open": r["open"] or 0}
    return s


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
    name = store.project(pid)["name"]
    if current != body.role:
        inbox.notify(email, "project_member", f"{user.name} {'added you to' if not current else 'changed your role on'} {name}",
                     user, f"You're {'an' if body.role[0] in 'AEIOU' else 'a'} {body.role.lower()}.", f"/projects/#/p/{pid}")
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
    if current != body.role:
        inbox.notify(email, "project_member", f"{user.name} changed your role on {store.project(pid)['name']}", user,
                     f"You're {'an' if body.role[0] in 'AEIOU' else 'a'} {body.role.lower()}.", f"/projects/#/p/{pid}")
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


# ---------- Files kept with a record (see files.py) ----------

def _record_with_file(pid: str, key: str, iid: str) -> dict:
    mod = section(key)
    old = store.item(pid, iid)
    if not old or old["module"] != key or not any(f["type"] == "file" for f in mod["fields"]):
        raise HTTPException(404, "Record not found.")
    return old


def _keep_file(pid: str, item: dict, name: str, data: bytes, user: User, note: str = "") -> dict:
    """Store the next version and note it on the record."""
    try:
        files.check(name, data)
    except files.FileError as e:
        raise HTTPException(422, str(e))
    v = store.add_file(item["id"], name, data, user.email, note, files.MAX_VERSIONS)
    info = {"name": name, "size": len(data), "kind": files.kind_of(name), "version": v["version"],
            "updatedAt": v["createdAt"], "updatedBy": user.email}
    return store.update_item(pid, item["id"], {**item["data"], "file": info}, user.email)


@app.post("/api/projects/{pid}/items/{key}/{iid}/file")
async def upload_file(pid: str, key: str, iid: str, request: Request, user: User = Depends(require_user)):
    """The file is the request body; its name is in X-Filename (URL-encoded). A new version each time."""
    need(pid, user, "Owner", "Editor")
    item = _record_with_file(pid, key, iid)
    if int(request.headers.get("content-length") or 0) > files.MAX_BYTES:
        raise HTTPException(413, f"Files can be at most {files.MAX_BYTES // (1024 * 1024)} MB.")
    base = request.headers.get("x-base-version")   # sent when saving an edit made in the page
    latest = (item["data"].get("file") or {}).get("version")
    if base and latest and str(latest) != base:
        raise HTTPException(409, f"Someone saved a newer version (version {latest}) while you were editing. "
                                 "Copy your changes, then open it again.")
    name = files.clean_name(unquote(request.headers.get("x-filename", "")))
    data = await request.body()
    return _keep_file(pid, item, name, data, user, unquote(request.headers.get("x-note", ""))[:200])


@app.get("/api/projects/{pid}/items/{key}/{iid}/file")
def download_file(pid: str, key: str, iid: str, v: int | None = None, view: bool = False, user: User = Depends(require_user)):
    """The file (or version v). view=1 shows PDFs, pictures and plain text in the page; everything else downloads."""
    need(pid, user, *ROLES)
    _record_with_file(pid, key, iid)
    got = store.file_data(iid, v)
    if not got:
        raise HTTPException(404, "No file.")
    info, data = got
    e = files.ext_of(info["name"])
    inline = view and e in files.INLINE
    headers = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(info['name'])}",
               "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}
    if inline:   # shown inside the page's own viewer, and in no other site's frames
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Content-Security-Policy"] = "frame-ancestors 'self'" if e == "pdf" else \
            "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
    return Response(data, media_type=files.INLINE[e] if inline else files.type_of(info["name"]), headers=headers)


@app.get("/api/projects/{pid}/items/{key}/{iid}/file/versions")
def file_versions(pid: str, key: str, iid: str, user: User = Depends(require_user)):
    need(pid, user, *ROLES)
    _record_with_file(pid, key, iid)
    return {"versions": store.files(iid), "keep": files.MAX_VERSIONS}


class RestoreIn(BaseModel):
    version: int


@app.post("/api/projects/{pid}/items/{key}/{iid}/file/restore")
def restore_file(pid: str, key: str, iid: str, body: RestoreIn, user: User = Depends(require_user)):
    """An older version becomes the newest (nothing is lost: the one it replaces stays a version)."""
    need(pid, user, "Owner", "Editor")
    item = _record_with_file(pid, key, iid)
    got = store.file_data(iid, body.version)
    if not got:
        raise HTTPException(404, "No such version.")
    info, data = got
    return _keep_file(pid, item, info["name"], data, user, f"Restored version {body.version}")


def _office(pid: str, key: str, iid: str, user: User, kind: str, *roles: str) -> tuple[dict, dict, bytes]:
    need(pid, user, *roles)
    item = _record_with_file(pid, key, iid)
    got = store.file_data(iid)
    if not got or files.kind_of(got[0]["name"]) != kind:
        raise HTTPException(404, f"No {kind} file.")
    return item, got[0], got[1]


class EditIn(BaseModel):
    version: int                                   # the version the page opened, so a newer save isn't overwritten
    blocks: list = Field(default_factory=list)     # Word
    edits: list = Field(default_factory=list)      # Excel


def _same_version(info: dict, body: EditIn) -> None:
    if info["version"] != body.version:
        raise HTTPException(409, f"Someone saved a newer version (version {info['version']}) while you were editing. "
                                 "Copy your changes, then open it again.")


@app.get("/api/projects/{pid}/items/{key}/{iid}/file/docx")
def read_docx(pid: str, key: str, iid: str, user: User = Depends(require_user)):
    _, info, data = _office(pid, key, iid, user, "docx", *ROLES)
    try:
        return {**files.docx_blocks(data), "version": info["version"], "name": info["name"]}
    except Exception:
        raise HTTPException(422, "This Word document can't be opened here. Download it to open it in Word.")


@app.put("/api/projects/{pid}/items/{key}/{iid}/file/docx")
def save_docx(pid: str, key: str, iid: str, body: EditIn, user: User = Depends(require_user)):
    item, info, data = _office(pid, key, iid, user, "docx", "Owner", "Editor")
    _same_version(info, body)
    try:
        out = files.docx_save(data, body.blocks)
    except files.FileError as e:
        raise HTTPException(422, str(e))
    return _keep_file(pid, item, info["name"], out, user, "Edited in the page")


@app.get("/api/projects/{pid}/items/{key}/{iid}/file/xlsx")
def read_xlsx(pid: str, key: str, iid: str, user: User = Depends(require_user)):
    _, info, data = _office(pid, key, iid, user, "xlsx", *ROLES)
    try:
        return {**files.xlsx_sheets(data), "version": info["version"], "name": info["name"]}
    except Exception:
        raise HTTPException(422, "This workbook can't be opened here. Download it to open it in Excel.")


@app.put("/api/projects/{pid}/items/{key}/{iid}/file/xlsx")
def save_xlsx(pid: str, key: str, iid: str, body: EditIn, user: User = Depends(require_user)):
    item, info, data = _office(pid, key, iid, user, "xlsx", "Owner", "Editor")
    _same_version(info, body)
    try:
        out = files.xlsx_save(data, body.edits)
    except files.FileError as e:
        raise HTTPException(422, str(e))
    return _keep_file(pid, item, info["name"], out, user, "Edited in the page")


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


# ---------- Serving under paths on a shared host ----------
# Projects at BASE_PATH (/projects), Tasks at TASKS_PATH (/tasks), Team at TEAM_PATH (/team) and
# each person's notifications at INBOX_PATH (/inbox) and Change management at CHANGES_PATH (/changes),
# from the same process and database.
tasks_app = FastAPI(title="Nscale Tasks", docs_url=None, redoc_url=None, openapi_url=None)
tasks_app.mount("/static", StaticFiles(directory=STATIC), name="static")
tasks_app.include_router(tasks.router)
tasks_app.add_exception_handler(HTTPException, _http_error)
team_app = FastAPI(title="Nscale Team", docs_url=None, redoc_url=None, openapi_url=None)
team_app.mount("/static", StaticFiles(directory=STATIC), name="static")
team_app.include_router(team.router)
team_app.add_exception_handler(HTTPException, _http_error)
inbox_app = FastAPI(title="Nscale notifications", docs_url=None, redoc_url=None, openapi_url=None)
inbox_app.mount("/static", StaticFiles(directory=STATIC), name="static")
inbox_app.include_router(inbox.router)
inbox_app.add_exception_handler(HTTPException, _http_error)
changes_app = FastAPI(title="Nscale Change management", docs_url=None, redoc_url=None, openapi_url=None)
changes_app.mount("/static", StaticFiles(directory=STATIC), name="static")
changes_app.include_router(changes.router)
changes_app.add_exception_handler(HTTPException, _http_error)

_projects = app
app = FastAPI(title="Nscale Projects and Tasks", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz", include_in_schema=False)
def _healthz():
    return {"ok": True}


for _path, _sub in ((TASKS_PATH, tasks_app), (TEAM_PATH, team_app), (INBOX_PATH, inbox_app), (CHANGES_PATH, changes_app),
                      (BASE_PATH, _projects)):
    if _path:
        app.add_api_route(_path, (lambda p: lambda: RedirectResponse(p + "/"))(_path), include_in_schema=False)
        app.mount(_path, _sub)
    else:
        app.mount("/", _sub)
